"""Unit-tier tests for TASK-003 state isolation in plan_codex_dispatch.

Covers the protected-path matrix (including schedule.json glob), the
pre-dispatch baseline snapshot, observe-only validate_scope, in-scope-only
timeout cleanup, fatal scope_misreport handling, sandbox-escape
observability on review, and the Fix-C lock-removal regression test.

All tests monkeypatch `invoke_codex` to avoid the real Codex CLI. Kept in a
separate file from `test_plan_codex_dispatch_integration.py` so that the
default `make test` run exercises state-isolation logic without gating on
the `@pytest.mark.slow` integration tier.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import importlib.util
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_codex_dispatch.py"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "plan_codex_dispatch", WRAPPER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Repo / plan fixtures
# ---------------------------------------------------------------------------


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    return repo


def _plan(task_id: str, files: list[str], test_command: str = "none") -> str:
    files_block = "\n".join(f"  - {f} (create)" for f in files)
    return f"""# Plan: state-isolation fixture

**Created:** 2026-04-14
**Status:** ready
**Base branch:** main

## Goal
Fixture plan for state-isolation unit tests.

## Context
Minimal plan for exercising the dispatch wrapper with a monkeypatched Codex.

---

## Tasks

### TASK-{task_id}: Isolated test task

- **Status:** pending
- **Priority:** low
- **Files:**
{files_block}
- **Dependencies:** none
- **Test command:** {test_command}
- **Acceptance criteria:**
  - All declared files exist.

**Description:**
Create the declared files.

**Implementation notes:**
None.

**Reversion guidance:**
Delete the declared files.
"""


def _write_plan(repo: Path, task_id: str, files: list[str], test_command: str = "none") -> Path:
    plan = repo / f"plan_{task_id}.md"
    plan.write_text(_plan(task_id, files, test_command))
    _git(["add", plan.name], cwd=repo)
    _git(["commit", "-q", "-m", f"add plan {task_id}"], cwd=repo)
    return plan


def _args(plan: Path, repo: Path, task_id: str, timeout: int = 300,
          dry_run: bool = False, files: str = "", review_focus: str = "bugs"):
    return argparse.Namespace(
        plan_file=str(plan),
        task_id=task_id,
        repo_root=str(repo),
        json=True,
        dry_run=dry_run,
        timeout=timeout,
        files=files,
        review_focus=review_focus,
    )


def _run_implement(monkeypatch, args, fake_codex, capsys, fake_test=None):
    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)
    if fake_test is not None:
        monkeypatch.setattr(wrapper, "run_test_command", fake_test)
    rc = wrapper.cmd_implement(args)
    captured = capsys.readouterr()
    assert captured.out.strip(), "wrapper emitted no stdout"
    return rc, json.loads(captured.out)


def _run_review(monkeypatch, args, fake_codex, capsys):
    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)
    rc = wrapper.cmd_review(args)
    captured = capsys.readouterr()
    assert captured.out.strip(), "wrapper emitted no stdout"
    return rc, json.loads(captured.out)


def _codex_writes(files: dict[str, str], *, status: str = "completed",
                  report_files: list[str] | None = None,
                  timeout: bool = False):
    """Return a fake invoke_codex that writes `files` in the workdir and
    declares `report_files` (defaults to files.keys()) in the JSON output.
    """

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
            sandbox=None):
        if timeout:
            return {
                "status": "timeout",
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "file_changes": [],
                "wall_seconds": 1.0,
            }
        for rel, content in files.items():
            target = Path(workdir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        payload = {
            "task_id": "001",
            "status": status,
            "files_changed": (
                report_files if report_files is not None else list(files.keys())
            ),
            "summary": "did the thing",
            "concerns": [],
            "plan_adaptations": [],
        }
        Path(output_path).write_text(json.dumps(payload))
        return {
            "status": "ok",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "file_changes": list(files.keys()),
            "wall_seconds": 1.0,
        }

    return fake


# ---------------------------------------------------------------------------
# _is_protected
# ---------------------------------------------------------------------------


def test_is_protected_exact_root_file_codex():
    assert wrapper._is_protected(".codex") is True


def test_is_protected_exact_root_file_claude():
    assert wrapper._is_protected(".claude") is True


def test_is_protected_prefix_dot_codex_dir():
    assert wrapper._is_protected(".codex/session.json") is True


def test_is_protected_prefix_dot_claude_dir():
    assert wrapper._is_protected(".claude/skills/implement-plan/SKILL.md") is True


def test_is_protected_prefix_run_log():
    assert wrapper._is_protected("docs/plans/_run_log.jsonl") is True


def test_is_protected_matches_schedule_json_in_docs_plans():
    """Fix A: plan_ops emits `*.schedule.json` sidecars that must be protected."""
    assert wrapper._is_protected("docs/plans/foo.schedule.json") is True
    assert wrapper._is_protected("docs/plans/DUAL_AGENT_PLAN_EXECUTOR.schedule.json") is True


def test_is_protected_matches_named_schedule_json():
    """Named plans commonly use hyphen/underscore — both must match the glob."""
    assert wrapper._is_protected("docs/plans/my-plan.schedule.json") is True
    assert wrapper._is_protected("docs/plans/my_plan.schedule.json") is True


def test_is_protected_rejects_schedule_json_outside_docs_plans():
    """Glob is anchored to docs/plans — no matches elsewhere."""
    assert wrapper._is_protected("foo.schedule.json") is False
    assert wrapper._is_protected("src/foo.schedule.json") is False
    assert wrapper._is_protected("docs/foo.schedule.json") is False


def test_is_protected_excludes_unrelated_paths():
    for p in [
        "README.md",
        "src/foo.py",
        "notes.txt",
        "docs/plans/some_plan.md",
        "scripts/other.py",
        "_run_log.jsonl",
    ]:
        assert wrapper._is_protected(p) is False, p


# ---------------------------------------------------------------------------
# _snapshot_baseline
# ---------------------------------------------------------------------------


def test_snapshot_baseline_captures_tracked_and_untracked(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("dirty\n")
    (repo / "notes.txt").write_text("floating\n")

    baseline = wrapper._snapshot_baseline(str(repo))
    assert baseline["captured"] is True
    assert "README.md" in baseline["tracked"]
    assert "notes.txt" in baseline["untracked"]


def test_snapshot_baseline_marks_uncaptured_on_git_failure(monkeypatch, tmp_path):
    def boom(repo_root):
        raise subprocess.SubprocessError("git broken")

    monkeypatch.setattr(wrapper, "git_changed_files", boom)
    baseline = wrapper._snapshot_baseline(str(tmp_path))
    assert baseline["captured"] is False
    assert baseline["tracked"] == frozenset()
    assert baseline["untracked"] == frozenset()


# ---------------------------------------------------------------------------
# validate_scope — observe-only semantics
# ---------------------------------------------------------------------------


def test_validate_scope_delta_preserves_baseline_tracked_dirt(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("sibling-dirty\n")
    baseline = wrapper._snapshot_baseline(str(repo))
    (repo / "a.py").write_text("task writes this\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert scope["out_of_scope_tracked"] == []
    assert scope["out_of_scope_untracked"] == []
    assert scope["out_of_scope_observed"] is False
    assert scope["baseline_captured"] is True
    assert scope["cleanup_strategy"] == "observe_only"
    assert (repo / "README.md").read_text() == "sibling-dirty\n"


def test_validate_scope_delta_preserves_baseline_untracked(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "notes.txt").write_text("sibling work\n")
    baseline = wrapper._snapshot_baseline(str(repo))
    (repo / "a.py").write_text("task writes this\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert scope["out_of_scope_untracked"] == []
    assert scope["out_of_scope_observed"] is False
    assert (repo / "notes.txt").exists()


def test_validate_scope_observes_out_of_scope_untracked_without_deleting(tmp_path):
    """Observe-only: out-of-scope writes are reported but NEVER mutated."""
    repo = _make_repo(tmp_path)
    (repo / "keep.txt").write_text("pre-existing\n")
    baseline = wrapper._snapshot_baseline(str(repo))
    (repo / "a.py").write_text("allowed\n")
    (repo / "escape.txt").write_text("new violation\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert "escape.txt" in scope["out_of_scope_untracked"]
    assert scope["out_of_scope_observed"] is True
    assert scope["cleanup_strategy"] == "observe_only"
    # Orchestrator will reconcile — file must still exist on disk
    assert (repo / "escape.txt").exists()
    assert (repo / "escape.txt").read_text() == "new violation\n"
    # Pre-existing untracked untouched
    assert (repo / "keep.txt").exists()


def test_validate_scope_observes_out_of_scope_tracked_without_restoring(tmp_path):
    """Observe-only: new tracked modifications outside scope are observed, not
    restored. Baseline dirt is kept (delta-bounded observation)."""
    repo = _make_repo(tmp_path)
    (repo / "baseline.py").write_text("v1\n")
    _git(["add", "baseline.py"], cwd=repo)
    _git(["commit", "-q", "-m", "add baseline"], cwd=repo)
    (repo / "other.py").write_text("v1\n")
    _git(["add", "other.py"], cwd=repo)
    _git(["commit", "-q", "-m", "add other"], cwd=repo)

    # Sibling dirt in baseline
    (repo / "baseline.py").write_text("sibling-edit\n")
    baseline = wrapper._snapshot_baseline(str(repo))

    # Codex writes allowed file and mutates an OTHER committed file
    (repo / "a.py").write_text("allowed\n")
    (repo / "other.py").write_text("codex-touched\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert "other.py" in scope["out_of_scope_tracked"]
    assert scope["out_of_scope_observed"] is True
    # Observe-only: out-of-scope tracked modification is still on disk
    assert (repo / "other.py").read_text() == "codex-touched\n"
    # Sibling edit to baseline.py preserved — it was in baseline set
    assert (repo / "baseline.py").read_text() == "sibling-edit\n"


def test_validate_scope_skips_protected_tracked(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "docs" / "plans").mkdir(parents=True)
    logp = repo / "docs" / "plans" / "_run_log.jsonl"
    logp.write_text("pre\n")
    _git(["add", "docs/plans/_run_log.jsonl"], cwd=repo)
    _git(["commit", "-q", "-m", "add log"], cwd=repo)
    baseline = wrapper._snapshot_baseline(str(repo))
    logp.write_text("pre\ntouched\n")
    (repo / "a.py").write_text("allowed\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert scope["protected_skipped_tracked"] == ["docs/plans/_run_log.jsonl"]
    assert "docs/plans/_run_log.jsonl" not in scope["out_of_scope_tracked"]
    assert logp.read_text() == "pre\ntouched\n"


def test_validate_scope_skips_protected_untracked(tmp_path):
    repo = _make_repo(tmp_path)
    baseline = wrapper._snapshot_baseline(str(repo))
    (repo / ".claude").mkdir()
    (repo / ".claude" / "state.json").write_text("{}\n")
    (repo / "a.py").write_text("allowed\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert ".claude/state.json" in scope["protected_skipped_untracked"]
    assert (repo / ".claude" / "state.json").exists()


def test_validate_scope_skips_protected_schedule_json_untracked(tmp_path):
    """Fix A: schedule.json glob gates observe-only protection symmetrically."""
    repo = _make_repo(tmp_path)
    (repo / "docs" / "plans").mkdir(parents=True)
    baseline = wrapper._snapshot_baseline(str(repo))
    sched = repo / "docs" / "plans" / "myplan.schedule.json"
    sched.write_text('{"batches":[]}\n')
    (repo / "a.py").write_text("allowed\n")

    scope = wrapper.validate_scope(str(repo), ["a.py"], baseline)

    assert "docs/plans/myplan.schedule.json" in scope["protected_skipped_untracked"]
    assert sched.exists()


def test_validate_scope_populates_changed_in_scope_new(tmp_path):
    repo = _make_repo(tmp_path)
    # Pre-dirty an allowed file in the baseline
    (repo / "a.py").write_text("baseline dirt\n")
    baseline = wrapper._snapshot_baseline(str(repo))
    # Codex also touches another allowed file fresh
    (repo / "b.py").write_text("fresh\n")

    scope = wrapper.validate_scope(
        str(repo), ["a.py", "b.py"], baseline,
    )
    assert scope["changed_in_scope_new"] == ["b.py"]


def test_validate_scope_skipped_no_baseline(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "a.py").write_text("x\n")

    scope = wrapper.validate_scope(
        str(repo),
        ["a.py"],
        {"captured": False, "tracked": frozenset(), "untracked": frozenset()},
    )
    assert scope["cleanup_strategy"] == "skipped_no_baseline"
    assert scope["out_of_scope_observed"] is False
    assert scope["baseline_captured"] is False


# ---------------------------------------------------------------------------
# cmd_implement — sibling preservation, protected paths
# ---------------------------------------------------------------------------


def test_implement_preserves_pre_existing_untracked_file(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "notes.txt").write_text("sibling untracked\n")

    fake = _codex_writes({"a.py": "hello\n"})
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )

    assert env["outcome"] == "success", env
    assert rc == 0
    assert (repo / "notes.txt").exists()


def test_implement_preserves_tracked_mod_outside_task(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "README.md").write_text("sibling tracked edit\n")

    fake = _codex_writes({"a.py": "hello\n"})
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )

    assert env["outcome"] == "success", env
    assert (repo / "README.md").read_text() == "sibling tracked edit\n"


def test_implement_preserves_out_of_scope_on_scope_violation(tmp_path, monkeypatch, capsys):
    """Observe-only: when Codex writes outside scope, the wrapper emits
    `scope_violation` but leaves the out-of-scope file on disk for the
    orchestrator to reconcile at the batch barrier."""
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "keep.txt").write_text("sibling\n")

    fake = _codex_writes({
        "a.py": "hello\n",
        "sneaky.txt": "violation\n",
    }, report_files=["a.py", "sneaky.txt"])
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )

    assert env["outcome"] == "scope_violation", env
    assert rc == 1
    assert "sneaky.txt" in env["out_of_scope_untracked"]
    assert env["out_of_scope_observed"] is True
    # Observe-only: sneaky.txt is preserved for orchestrator reconciliation
    assert (repo / "sneaky.txt").exists()
    assert (repo / "sneaky.txt").read_text() == "violation\n"
    # Pre-existing sibling file untouched
    assert (repo / "keep.txt").exists()


def test_implement_protected_paths_skip_cleanup_and_log(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])

    # Codex writes the allowed file AND an undeclared side effect to a
    # protected path. The wrapper must log the protected write under
    # scope.protected_skipped_untracked and NOT delete it. The protected
    # write is not in files_changed — Codex honestly declares only the
    # allowed file — so the dishonesty check does not fire.
    def fake(prompt, workdir, schema_path, output_path, timeout_sec, sandbox=None):
        (Path(workdir) / "a.py").write_text("hello\n")
        (Path(workdir) / ".claude").mkdir(exist_ok=True)
        (Path(workdir) / ".claude" / "scratch.md").write_text("protected side\n")
        payload = {
            "task_id": "001",
            "status": "completed",
            "files_changed": ["a.py"],
            "summary": "ok",
            "concerns": [],
            "plan_adaptations": [],
        }
        Path(output_path).write_text(json.dumps(payload))
        return {
            "status": "ok", "exit_code": 0, "stdout": "",
            "stderr": "", "file_changes": ["a.py", ".claude/scratch.md"],
            "wall_seconds": 1.0,
        }

    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )

    assert env["outcome"] == "success", env
    assert (repo / ".claude" / "scratch.md").exists()
    assert ".claude/scratch.md" in env["scope"]["protected_skipped_untracked"]


# ---------------------------------------------------------------------------
# cmd_implement — scope_misreport fatal
# ---------------------------------------------------------------------------


def test_implement_failure_with_reason_scope_misreport_on_undeclared(
    tmp_path, monkeypatch, capsys,
):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py", "b.py"])
    # Codex writes both files but only reports a.py
    fake = _codex_writes(
        {"a.py": "x\n", "b.py": "y\n"},
        report_files=["a.py"],
    )
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )

    assert env["outcome"] == "failure", env
    assert env["reason"] == "scope_misreport"
    assert "b.py" in env["undeclared_changes"]


def test_implement_failure_with_reason_scope_misreport_on_phantom(
    tmp_path, monkeypatch, capsys,
):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py", "b.py"])
    # Codex only writes a.py but reports both
    fake = _codex_writes(
        {"a.py": "x\n"},
        report_files=["a.py", "b.py"],
    )
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )

    assert env["outcome"] == "failure", env
    assert env["reason"] == "scope_misreport"
    assert "b.py" in env["phantom_declarations"]


def test_implement_test_not_run_on_scope_misreport(
    tmp_path, monkeypatch, capsys,
):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py", "b.py"],
                       test_command="echo should-not-run")
    called = {"n": 0}

    def fake_test(cmd, repo_root, timeout_sec=300, max_attempts=2):
        called["n"] += 1
        return {
            "result": "passed", "attempts": 1, "output_tail": "",
            "flaky": False, "command": cmd,
        }

    fake = _codex_writes(
        {"a.py": "x\n", "b.py": "y\n"},
        report_files=["a.py"],
    )
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
        fake_test=fake_test,
    )

    assert env["outcome"] == "failure"
    assert env["reason"] == "scope_misreport"
    assert env["test_result"]["result"] == "not_run"
    assert called["n"] == 0


def test_dishonesty_check_catches_undeclared_out_of_scope_write(
    tmp_path, monkeypatch, capsys,
):
    """Defense-in-depth: if the scope_violation branch were ever bypassed, the
    dishonesty check must still detect an undeclared out-of-scope file.

    Simulated here by declaring a.py honestly and writing a truly in-scope
    b.py that Codex omits from files_changed (Codex misreport) — triggers
    scope_misreport rather than scope_violation."""
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py", "b.py"])
    fake = _codex_writes(
        {"a.py": "x\n", "b.py": "y\n"},
        report_files=["a.py"],
    )
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001"), fake, capsys,
    )
    # Both files are in allowed_files, so no scope_violation fires. The
    # dishonesty path catches the omission.
    assert env["outcome"] == "failure", env
    assert env["reason"] == "scope_misreport"
    assert "b.py" in env["undeclared_changes"]


# ---------------------------------------------------------------------------
# cmd_implement — timeout cleanup
# ---------------------------------------------------------------------------


def test_timeout_uses_baseline_not_repo_scope(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    # Pre-existing sibling work that repo-wide cleanup would destroy
    (repo / "sibling.txt").write_text("must survive\n")
    (repo / "README.md").write_text("sibling mod\n")

    fake = _codex_writes({}, timeout=True)
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001", timeout=5), fake, capsys,
    )

    assert env["outcome"] == "timeout"
    assert env["cleanup_strategy"] == "in_scope_only"
    assert env["baseline_captured"] is True
    # Both sibling artifacts survive
    assert (repo / "sibling.txt").exists()
    assert (repo / "README.md").read_text() == "sibling mod\n"


def test_timeout_without_baseline_skips_cleanup(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])

    def boom(repo_root):
        raise subprocess.SubprocessError("git broken")

    monkeypatch.setattr(wrapper, "git_changed_files", boom)

    fake = _codex_writes({}, timeout=True)
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001", timeout=5), fake, capsys,
    )

    assert env["outcome"] == "timeout"
    assert env["cleanup_strategy"] == "skipped_no_baseline"
    assert env["baseline_captured"] is False


def test_timeout_deletes_partial_writes_on_allowed_files_only(
    tmp_path, monkeypatch, capsys,
):
    """In-scope cleanup: only allowed_files are restored/deleted on timeout.
    Out-of-scope partial writes are observed and left alone for the orchestrator."""
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        # Simulate partial writes before timeout
        (Path(workdir) / "a.py").write_text("partial\n")
        (Path(workdir) / "escape.txt").write_text("partial escape\n")
        return {
            "status": "timeout", "exit_code": -1, "stdout": "",
            "stderr": "", "file_changes": [], "wall_seconds": 1.0,
        }

    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001", timeout=5), fake, capsys,
    )

    assert env["outcome"] == "timeout"
    # In-scope: partial a.py is deleted
    assert not (repo / "a.py").exists()
    # Out-of-scope: escape.txt is preserved for orchestrator reconciliation
    assert (repo / "escape.txt").exists()
    assert "escape.txt" in env["out_of_scope_untracked"]
    assert env["out_of_scope_observed"] is True


def test_timeout_cleanup_preserves_unrelated_untracked(tmp_path, monkeypatch, capsys):
    """Sibling's pre-existing untracked file survives this task's timeout
    cleanup because it is outside allowed_files."""
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "sibling_work.txt").write_text("in progress\n")

    fake = _codex_writes({}, timeout=True)
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001", timeout=5), fake, capsys,
    )

    assert env["outcome"] == "timeout"
    assert env["cleanup_strategy"] == "in_scope_only"
    assert (repo / "sibling_work.txt").exists()
    assert (repo / "sibling_work.txt").read_text() == "in progress\n"


def test_timeout_cleanup_preserves_unrelated_tracked_modification(
    tmp_path, monkeypatch, capsys,
):
    """Pre-existing sibling modification to a tracked file outside allowed_files
    survives timeout cleanup."""
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "other.py").write_text("v1\n")
    _git(["add", "other.py"], cwd=repo)
    _git(["commit", "-q", "-m", "add other"], cwd=repo)
    (repo / "other.py").write_text("sibling-modified\n")

    fake = _codex_writes({}, timeout=True)
    rc, env = _run_implement(
        monkeypatch, _args(plan, repo, "001", timeout=5), fake, capsys,
    )

    assert env["outcome"] == "timeout"
    assert (repo / "other.py").read_text() == "sibling-modified\n"


# ---------------------------------------------------------------------------
# cmd_review — sandbox escape observability
# ---------------------------------------------------------------------------


def _codex_review_output(output_path: str, verdict: str = "clean") -> None:
    payload = {
        "task_id": "001",
        "verdict": verdict,
        "findings": [],
        "summary": "looked good",
    }
    Path(output_path).write_text(json.dumps(payload))


def test_review_preserves_pre_existing_untracked(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "a.py").write_text("under review\n")
    _git(["add", "a.py"], cwd=repo)
    _git(["commit", "-q", "-m", "add a.py"], cwd=repo)
    (repo / "sibling.txt").write_text("sibling work\n")

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        _codex_review_output(output_path, verdict="clean")
        return {
            "status": "ok", "exit_code": 0, "stdout": "",
            "stderr": "", "file_changes": [], "wall_seconds": 1.0,
        }

    args = _args(plan, repo, "001", timeout=180, files="a.py",
                 review_focus="bugs")
    rc, env = _run_review(monkeypatch, args, fake, capsys)

    assert env["outcome"] == "success"
    assert (repo / "sibling.txt").exists()


def test_review_flags_sandbox_escape_via_out_of_scope_observed(
    tmp_path, monkeypatch, capsys,
):
    """Review now surfaces sandbox escape via out_of_scope_observed, which
    derives from Fix-B's observe-only scope field."""
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "a.py").write_text("under review\n")
    _git(["add", "a.py"], cwd=repo)
    _git(["commit", "-q", "-m", "add a.py"], cwd=repo)

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        # Simulate sandbox escape — writes outside the review scope
        (Path(workdir) / "escape.txt").write_text("oops\n")
        _codex_review_output(output_path, verdict="clean")
        return {
            "status": "ok", "exit_code": 0, "stdout": "",
            "stderr": "", "file_changes": ["escape.txt"], "wall_seconds": 1.0,
        }

    args = _args(plan, repo, "001", timeout=180, files="a.py",
                 review_focus="bugs")
    rc, env = _run_review(monkeypatch, args, fake, capsys)

    assert env["sandbox_escape_detected"] is True
    assert env["out_of_scope_observed"] is True
    # Observe-only: escape.txt remains for orchestrator reconciliation
    assert (repo / "escape.txt").exists()


def test_review_outcome_remains_success_on_sandbox_escape(
    tmp_path, monkeypatch, capsys,
):
    repo = _make_repo(tmp_path)
    plan = _write_plan(repo, "001", ["a.py"])
    (repo / "a.py").write_text("under review\n")
    _git(["add", "a.py"], cwd=repo)
    _git(["commit", "-q", "-m", "add a.py"], cwd=repo)

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        (Path(workdir) / "escape.txt").write_text("oops\n")
        _codex_review_output(output_path, verdict="clean")
        return {
            "status": "ok", "exit_code": 0, "stdout": "",
            "stderr": "", "file_changes": ["escape.txt"], "wall_seconds": 1.0,
        }

    args = _args(plan, repo, "001", timeout=180, files="a.py",
                 review_focus="bugs")
    rc, env = _run_review(monkeypatch, args, fake, capsys)

    # Review keeps log-but-succeed semantics by explicit TASK-003 design
    assert env["outcome"] == "success"
    assert rc == 0


# ---------------------------------------------------------------------------
# Fast-tier parallel sibling regression
# ---------------------------------------------------------------------------


def test_parallel_implement_siblings_preserve_each_other(tmp_path):
    """Two concurrent cmd_implement calls against disjoint allowed_files in
    the same scratch repo must both succeed without erasing each other.

    Under observe-only semantics (Fix B/C) this holds without any
    inter-sibling gating: neither sibling ever mutates the other's files
    because cleanup is bounded by each sibling's own allowed_files."""
    repo = _make_repo(tmp_path)
    plan_a = _write_plan(repo, "001", ["a.py"])
    plan_b = _write_plan(repo, "002", ["b.py"])

    sibling_a_finished = threading.Event()

    def make_fake(declared: str):
        def fake(prompt, workdir, schema_path, output_path, timeout_sec,
                 sandbox=None):
            target = Path(workdir) / declared
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"content for {declared}\n")
            payload = {
                "task_id": "001" if declared == "a.py" else "002",
                "status": "completed",
                "files_changed": [declared],
                "summary": "ok",
                "concerns": [],
                "plan_adaptations": [],
            }
            Path(output_path).write_text(json.dumps(payload))
            return {
                "status": "ok", "exit_code": 0, "stdout": "",
                "stderr": "", "file_changes": [declared], "wall_seconds": 0.2,
            }
        return fake

    results: dict[str, dict] = {}

    def run_a():
        mp = _load_wrapper()
        mp.invoke_codex = make_fake("a.py")
        captured: list[dict] = []
        mp.emit = lambda envelope: captured.append(envelope)
        rc = mp.cmd_implement(_args(plan_a, repo, "001"))
        sibling_a_finished.set()
        results["001"] = {"rc": rc, "env": captured[-1] if captured else {}}

    def run_b():
        mp = _load_wrapper()
        real_snap = mp._snapshot_baseline

        def gated_snap(repo_root):
            sibling_a_finished.wait(timeout=10)
            return real_snap(repo_root)

        mp._snapshot_baseline = gated_snap
        mp.invoke_codex = make_fake("b.py")
        captured: list[dict] = []
        mp.emit = lambda envelope: captured.append(envelope)
        rc = mp.cmd_implement(_args(plan_b, repo, "002"))
        results["002"] = {"rc": rc, "env": captured[-1] if captured else {}}

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(run_a)
        fb = ex.submit(run_b)
        fa.result(timeout=30)
        fb.result(timeout=30)

    assert results["001"]["env"]["outcome"] == "success", results["001"]["env"]
    assert results["002"]["env"]["outcome"] == "success", results["002"]["env"]
    assert (repo / "a.py").exists()
    assert (repo / "b.py").exists()


def test_parallel_implement_dispatches_actually_overlap(tmp_path):
    """Fix C regression: removing _dispatch_lock means two dispatches on the
    same repo can run concurrently inside invoke_codex. A threading.Barrier
    with a tight 2s timeout proves real temporal overlap — if the wrapper
    still held a repo-wide lock, one thread would never enter invoke_codex
    while the other was inside, barrier.wait() would time out, and
    BrokenBarrierError would propagate out of cmd_implement.

    Outcome can be either success (if the per-sibling baselines serialize
    cleanly) or scope_violation (if sibling writes race into each other's
    post-baseline delta) — both are proof the barrier was cleared. The
    pre-Fix-C codebase would raise BrokenBarrierError instead."""
    repo = _make_repo(tmp_path)
    plan_a = _write_plan(repo, "001", ["a.py"])
    plan_b = _write_plan(repo, "002", ["b.py"])

    barrier = threading.Barrier(parties=2, timeout=2.0)
    entered = threading.Event()
    entered_count = {"n": 0}
    entered_lock = threading.Lock()

    def make_fake(declared: str):
        def fake(prompt, workdir, schema_path, output_path, timeout_sec,
                 sandbox=None):
            # This call blocks until the sibling thread also reaches it.
            # Under a repo-wide lock only one sibling enters at a time and
            # the other times out → BrokenBarrierError → test fails.
            with entered_lock:
                entered_count["n"] += 1
            barrier.wait()
            entered.set()
            target = Path(workdir) / declared
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"content for {declared}\n")
            payload = {
                "task_id": "001" if declared == "a.py" else "002",
                "status": "completed",
                "files_changed": [declared],
                "summary": "ok",
                "concerns": [],
                "plan_adaptations": [],
            }
            Path(output_path).write_text(json.dumps(payload))
            return {
                "status": "ok", "exit_code": 0, "stdout": "",
                "stderr": "", "file_changes": [declared], "wall_seconds": 0.1,
            }
        return fake

    results: dict[str, dict] = {}
    errors: dict[str, BaseException] = {}

    def run(task_id: str, plan: Path, declared: str):
        mp = _load_wrapper()
        mp.invoke_codex = make_fake(declared)
        captured: list[dict] = []
        mp.emit = lambda envelope: captured.append(envelope)
        try:
            rc = mp.cmd_implement(_args(plan, repo, task_id))
            results[task_id] = {"rc": rc, "env": captured[-1] if captured else {}}
        except BaseException as e:  # noqa: BLE001
            errors[task_id] = e

    with ThreadPoolExecutor(max_workers=2) as ex:
        fa = ex.submit(run, "001", plan_a, "a.py")
        fb = ex.submit(run, "002", plan_b, "b.py")
        fa.result(timeout=30)
        fb.result(timeout=30)

    # Both threads must have entered invoke_codex concurrently.
    assert entered_count["n"] == 2, (
        f"only {entered_count['n']} siblings entered invoke_codex — "
        f"repo-wide lock still present?"
    )
    assert entered.is_set(), "barrier never released — siblings serialized"
    # Neither thread may have raised BrokenBarrierError.
    assert not errors, f"unexpected errors from overlap test: {errors}"
    # Both envelopes must exist; outcome is either success or scope_violation.
    for tid in ("001", "002"):
        assert tid in results, f"sibling {tid} produced no envelope"
        outcome = results[tid]["env"].get("outcome")
        assert outcome in {"success", "scope_violation"}, (
            f"sibling {tid} unexpected outcome={outcome!r}: {results[tid]}"
        )
    # Both declared files made it to disk (observe-only preserves siblings).
    assert (repo / "a.py").exists()
    assert (repo / "b.py").exists()
