"""TASK-006 / TASK-008 tests: schedule-only plan-review contract.

Covers the acceptance criteria added by TASK-006 and finalized by
TASK-008 (deprecation shim removal):

  1. ``plan-review`` dispatched against the fixture's fat manifest schedule
     (produced by TASK-004 `build-tasks`) returns a reviewer verdict of
     ``approved`` / ``approved-with-notes`` with no ``critical`` findings.
  2. ``plan-review`` dispatched against a schedule whose fat-manifest
     ``description`` is deliberately blank for one task surfaces a finding
     pointing at ``tasks[<id>].description`` with a non-empty
     ``suggested_change``.
  3. The invoking argv never carries ``--plan-file``; the internal
     ``render_plan_review_prompt`` helper accepts the new signature with
     no ``plan_text`` / ``plan_basename`` / ``plan_path`` actuals and
     does not raise ``TypeError``.
  4. TASK-008: argparse no longer recognizes ``--plan-file`` or
     ``--plans-dir`` on ``plan-review``; render helper no longer accepts
     the legacy ``plan_text`` / ``plan_abs_path`` / ``plans_dir`` kwargs.

These tests monkeypatch ``invoke_codex`` to avoid the real Codex CLI
(mirrors the pattern in ``test_plan_codex_dispatch_state_isolation.py``).
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_codex_dispatch.py"
)
DIRECTORY_MODE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "directory_mode_plan"
)
# Shipped fat-manifest schedule sidecar for the directory_mode_plan fixture.
# Lives at the documented in-directory convention path
# (``<plan_dir>/<plan_dir>.schedule.json``, see ``_plan_paths.py:69``) so
# the wrapper's plan_basename derivation from the sidecar's parent folder
# round-trips back to ``directory_mode_plan``. Committed to git; tests that
# mutate the schedule must copy it into ``tmp_path`` first to avoid
# touching the committed artifact.
FIXTURE_SCHEDULE = DIRECTORY_MODE_FIXTURE / "directory_mode_plan.schedule.json"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "plan_codex_dispatch", WRAPPER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Schedule helpers — dispatch against the shipped fat-manifest schedule
# sidecar. Read-only tests point directly at the committed fixture path;
# mutation tests copy the fixture directory (including the sidecar) into
# tmp_path first so the committed artifact is never touched.
# ---------------------------------------------------------------------------


def _copy_fixture_into(plan_dir: Path) -> None:
    """Copy the shipped fixture (including the sidecar) into ``plan_dir``
    so tests can mutate a per-test copy without touching the committed
    fixture. The sidecar file is copied alongside the chunk markdown so the
    copy retains the in-directory sidecar convention
    (``<plan_dir>/<plan_dir>.schedule.json``)."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    for src in DIRECTORY_MODE_FIXTURE.iterdir():
        dst = plan_dir / src.name
        dst.write_bytes(src.read_bytes())


def _fixture_sidecar_in(plan_dir: Path) -> Path:
    """Return the expected sidecar path for a tmp plan_dir copy. The
    sidecar filename follows the committed fixture's own basename so the
    wrapper's plan_basename-derivation assertions in the tests keep
    targeting ``directory_mode_plan``."""
    return plan_dir / FIXTURE_SCHEDULE.name


# ---------------------------------------------------------------------------
# Fake invoke_codex — returns a canned envelope body into ``output_path``.
# The wrapper's cmd_plan_review path reads the file back, decodes it, and
# puts the body under envelope.parsed, so the fake simulates Codex's
# behaviour without any live CLI.
# ---------------------------------------------------------------------------


def _fake_codex_returning(parsed_body: dict):
    """Return a fake invoke_codex that writes `parsed_body` as the Codex
    structured-output JSON file. Mirrors the real dispatcher's
    `--output-schema + -o` contract."""

    captured: dict = {"prompt": None}

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        captured["prompt"] = prompt
        Path(output_path).write_text(
            json.dumps(parsed_body), encoding="utf-8",
        )
        return {
            "status": "ok",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    fake.captured = captured  # type: ignore[attr-defined]
    return fake


def _plan_review_args(
    schedule_file: Path,
    repo_root: Path,
    *,
    allow_gaps: bool = False,
) -> argparse.Namespace:
    """Build a Namespace matching the `plan-review` argparse contract
    (schedule-only; TASK-006, finalized in TASK-008). The retired
    ``plan_file`` / ``plans_dir`` kwargs are no longer accepted."""
    return argparse.Namespace(
        schedule_file=str(schedule_file),
        repo_root=str(repo_root),
        json=True,
        dry_run=False,
        timeout=180,
        allow_gaps=allow_gaps,
    )


def _review_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _git_stdout(repo: Path, args: list[str]) -> str:
    cp = subprocess.run(
        ["git"] + args,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout


def _git_index_snapshot(repo: Path) -> tuple[str, str]:
    return (
        _git_stdout(repo, ["status", "--porcelain=v1"]),
        _git_stdout(repo, ["ls-files", "--others", "--exclude-standard"]),
    )


def _review_plan(path: Path, file_path: str) -> None:
    path.write_text(
        "\n".join([
            "### TASK-005: Review untracked file",
            "- **Status:** pending",
            "- **Priority:** P1",
            "- **Files:**",
            f"  - {file_path}",
            "",
            "**Description:**",
            "Review the changed file.",
            "",
        ]),
        encoding="utf-8",
    )


def _cmd_review_args(
    plan_file: Path,
    repo: Path,
    *,
    files: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_file=str(plan_file),
        repo_root=str(repo),
        task_id="005",
        files=files,
        review_focus="",
        dry_run=True,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# TASK-005: review diffs include untracked files via intent-to-add.
# ---------------------------------------------------------------------------


def test_git_diff_for_files_default_omits_untracked_file(tmp_path):
    sig = inspect.signature(wrapper.git_diff_for_files)
    include = sig.parameters["include_untracked"]
    assert include.kind is inspect.Parameter.KEYWORD_ONLY
    assert include.default is False

    repo = _review_git_repo(tmp_path)
    (repo / "new.txt").write_text("new content\n", encoding="utf-8")

    diff = wrapper.git_diff_for_files(str(repo), ["new.txt"])

    assert diff == ""


def test_git_diff_for_files_include_untracked_preserves_index(tmp_path):
    repo = _review_git_repo(tmp_path)
    (repo / "new.txt").write_text("new content\n", encoding="utf-8")
    before = _git_index_snapshot(repo)

    diff = wrapper.git_diff_for_files(
        str(repo), ["new.txt"], include_untracked=True,
    )
    after = _git_index_snapshot(repo)

    assert "diff --git a/new.txt b/new.txt" in diff
    assert "+new content" in diff
    assert after == before


def test_git_diff_for_files_empty_file_list_returns_empty_without_git(
    monkeypatch,
):
    calls = []

    def fake_git(args, cwd, timeout=wrapper.GIT_TIMEOUT):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(wrapper, "_git", fake_git)

    assert wrapper.git_diff_for_files(
        "/unused", [], include_untracked=True,
    ) == ""
    assert calls == []


def test_git_diff_for_files_add_failure_does_not_raise_and_resets(
    monkeypatch,
):
    calls = []

    def fake_git(args, cwd, timeout=wrapper.GIT_TIMEOUT):
        calls.append(args)
        if args[:2] == ["add", "-N"]:
            return subprocess.CompletedProcess(args, 1, "", "missing\n")
        if args[:2] == ["diff", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "diff body\n", "")
        if args[:2] == ["reset", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(wrapper, "_git", fake_git)

    diff = wrapper.git_diff_for_files(
        "/repo", ["missing.txt"], include_untracked=True,
    )

    assert diff == "diff body\n"
    assert calls == [
        ["add", "-N", "--", "missing.txt"],
        ["diff", "HEAD", "--", "missing.txt"],
        ["reset", "HEAD", "--", "missing.txt"],
    ]


def test_git_diff_for_files_resets_when_diff_raises(monkeypatch):
    import pytest as _pytest

    calls = []

    def fake_git(args, cwd, timeout=wrapper.GIT_TIMEOUT):
        calls.append(args)
        if args[:2] == ["add", "-N"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["diff", "HEAD"]:
            raise RuntimeError("diff failed")
        if args[:2] == ["reset", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(wrapper, "_git", fake_git)

    with _pytest.raises(RuntimeError):
        wrapper.git_diff_for_files(
            "/repo", ["new.txt"], include_untracked=True,
        )

    assert calls == [
        ["add", "-N", "--", "new.txt"],
        ["diff", "HEAD", "--", "new.txt"],
        ["reset", "HEAD", "--", "new.txt"],
    ]


def test_cmd_review_dry_run_prompt_includes_untracked_file_diff(
    tmp_path, capsys,
):
    repo = _review_git_repo(tmp_path)
    (repo / "new.txt").write_text("new content\n", encoding="utf-8")
    before = _git_index_snapshot(repo)
    plan = tmp_path / "plan.md"
    _review_plan(plan, "new.txt")

    rc = wrapper.cmd_review(_cmd_review_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    after = _git_index_snapshot(repo)

    assert rc == 0, envelope
    assert envelope["outcome"] == "dry_run"
    assert envelope["diff_size_bytes"] > 0
    assert "diff --git a/new.txt b/new.txt" in envelope["prompt_preview"]
    assert "+new content" in envelope["prompt_preview"]
    assert after == before


# ---------------------------------------------------------------------------
# TASK-001: TASK-027B backticked Files entries reach scope checks normalized.
# ---------------------------------------------------------------------------


def test_scope_check_accepts_backticked_declared_files_with_bare_observed_writes(
    monkeypatch,
):
    """Regression for post-mortem Issue 1 from run 20260425T041800.

    Depends on CODEX_FRICTION TASK-002: wrapper declarations must flow
    through the shared ``_plan_paths.normalize_files_entry`` helper before
    ``validate_scope`` compares them with git's bare observed paths.
    """
    declared = [
        "`plugins/plan-executor/agents/plan-implementer.md`",
        "`tests/scripts/test_plan_implementer_spec.py` (new)",
    ]
    observed = [
        "plugins/plan-executor/agents/plan-implementer.md",
        "tests/scripts/test_plan_implementer_spec.py",
    ]

    monkeypatch.setattr(
        wrapper,
        "git_changed_files",
        lambda repo_root: {"tracked": observed, "untracked": []},
    )

    scope = wrapper.validate_scope(
        "/unused",
        [wrapper.normalize_file_path(path) for path in declared],
        {"captured": True, "tracked": [], "untracked": []},
    )

    assert scope["out_of_scope_observed"] is False
    assert scope["out_of_scope_tracked"] == []
    assert scope["out_of_scope_untracked"] == []
    assert scope["changed_in_scope_new"] == observed


def test_scope_check_rejects_unallowed_bare_path_under_backticked_declarations(
    monkeypatch,
):
    """Negative control for post-mortem Issue 1 from run 20260425T041800.

    Depends on CODEX_FRICTION TASK-002: normalized backticked declarations
    must not make ``validate_scope`` trivially accept unrelated bare paths.
    """
    declared = [
        "`plugins/plan-executor/agents/plan-implementer.md`",
        "`tests/scripts/test_plan_implementer_spec.py` (new)",
    ]
    allowed_observed = [
        "plugins/plan-executor/agents/plan-implementer.md",
        "tests/scripts/test_plan_implementer_spec.py",
    ]
    unallowed = "tests/scripts/test_unallowed_scope_escape.py"

    monkeypatch.setattr(
        wrapper,
        "git_changed_files",
        lambda repo_root: {
            "tracked": allowed_observed + [unallowed],
            "untracked": [],
        },
    )

    scope = wrapper.validate_scope(
        "/unused",
        [wrapper.normalize_file_path(path) for path in declared],
        {"captured": True, "tracked": [], "untracked": []},
    )

    assert scope["out_of_scope_observed"] is True
    assert scope["out_of_scope_tracked"] == [unallowed]
    assert scope["out_of_scope_untracked"] == []
    assert scope["changed_in_scope_new"] == allowed_observed


def test_scope_check_accepts_new_files_under_declared_directory(monkeypatch):
    declared = [
        "`tests/scripts/fixtures/implement_plan_runner/` "
        "(new fixture directory as needed)",
    ]
    observed = [
        "tests/scripts/fixtures/implement_plan_runner/00_INDEX.json",
        "tests/scripts/fixtures/implement_plan_runner/TASK-001_fixture.md",
    ]
    outside = "tests/scripts/fixtures/other_runner/TASK-001_fixture.md"

    monkeypatch.setattr(
        wrapper,
        "git_changed_files",
        lambda repo_root: {
            "tracked": [],
            "untracked": observed + [outside],
        },
    )

    scope = wrapper.validate_scope(
        "/unused",
        [wrapper.normalize_file_path(path) for path in declared],
        {"captured": True, "tracked": [], "untracked": []},
    )

    assert scope["out_of_scope_observed"] is True
    assert scope["out_of_scope_tracked"] == []
    assert scope["out_of_scope_untracked"] == [outside]
    assert scope["changed_in_scope_new"] == observed


def test_reported_scope_accepts_files_under_declared_directory():
    declared = [
        wrapper.normalize_file_path(
            "`tests/scripts/fixtures/implement_plan_runner/` "
            "(new fixture directory as needed)",
        ),
    ]

    assert wrapper._path_in_allowed_scope(
        "tests/scripts/fixtures/implement_plan_runner/00_INDEX.json",
        declared,
    )
    assert not wrapper._path_in_allowed_scope(
        "tests/scripts/fixtures/implement_plan_runner_extra/00_INDEX.json",
        declared,
    )


# ---------------------------------------------------------------------------
# 1. Fixture schedule → approved verdict, no critical findings.
# ---------------------------------------------------------------------------


def test_plan_review_fixture_schedule_approved(tmp_path, monkeypatch, capsys):
    """Acceptance: dispatch against the shipped fat-manifest schedule
    (``tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json``)
    and assert the envelope carries `verdict in {approved, approved-with-notes}`
    with no critical findings. Codex is mocked; no live CLI. The shipped
    sidecar is read-only for this test — we point ``--schedule-file`` at
    the committed path directly."""
    sidecar = FIXTURE_SCHEDULE
    assert sidecar.is_file(), (
        f"fixture sidecar missing at {sidecar}; TASK-006 ships this file"
    )

    # Canned envelope body: reviewer approves the fixture schedule outright.
    canned = {
        "plan_file": DIRECTORY_MODE_FIXTURE.name,
        "verdict": "approved",
        "findings": [],
        "notes": [],
        "schedule_ok": True,
        "summary": "Fat manifest schedule parses cleanly; DAG + file-lock "
                   "batches are consistent; every task carries description + "
                   "acceptance_criteria.",
    }
    fake = _fake_codex_returning(canned)
    monkeypatch.setattr(wrapper, "invoke_codex", fake)

    args = _plan_review_args(sidecar, tmp_path)
    rc = wrapper.cmd_plan_review(args)
    out = capsys.readouterr().out
    envelope = json.loads(out)

    assert rc == 0, envelope
    assert envelope["outcome"] == "success", envelope
    parsed = envelope["parsed"]
    assert parsed["verdict"] in {"approved", "approved-with-notes"}, parsed
    # No critical findings allowed (AC6 contract).
    critical = [
        f for f in parsed.get("findings", [])
        if f.get("severity") == "critical"
    ]
    assert critical == [], (
        f"fixture schedule should not elicit critical findings, got: "
        f"{critical}"
    )
    # envelope.plan_file identifies the plan directory (directory-mode
    # sidecar convention puts the sidecar inside the plan dir).
    assert envelope["plan_file"] == DIRECTORY_MODE_FIXTURE.name, envelope


# ---------------------------------------------------------------------------
# 2. Schedule with a missing description → reviewer surfaces a finding
#    whose `section` points at that task and `suggested_change` is non-empty.
# ---------------------------------------------------------------------------


def test_plan_review_schedule_with_missing_description_surfaces_finding(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance: when a task's `description` is deliberately blank, the
    reviewer surfaces a finding whose `section` points at that task and
    `suggested_change` is non-empty. The finding section should follow the
    new `tasks[i].description` path convention added to the prompt.

    Copies the shipped fixture (including the committed sidecar) into
    ``tmp_path`` so the mutation never touches the on-disk artifact."""
    plan_dir = tmp_path / DIRECTORY_MODE_FIXTURE.name
    _copy_fixture_into(plan_dir)
    sidecar = _fixture_sidecar_in(plan_dir)
    assert sidecar.is_file(), (
        f"copied sidecar missing at {sidecar}; the shipped fixture must "
        f"include {FIXTURE_SCHEDULE.name}"
    )

    # Mutate the persisted schedule: blank description on TASK-002.
    schedule_doc = json.loads(sidecar.read_text(encoding="utf-8"))
    target_id = None
    for task in schedule_doc.get("tasks", []):
        if task.get("id") == "002":
            task["description"] = ""
            target_id = task["id"]
            break
    assert target_id == "002", schedule_doc
    sidecar.write_text(
        json.dumps(schedule_doc, indent=2), encoding="utf-8",
    )

    # Canned envelope: reviewer flags the missing-description task.
    canned = {
        "plan_file": plan_dir.name,
        "verdict": "needs-replan",
        "findings": [
            {
                "severity": "important",
                "blocking": True,
                "section": f"tasks[{target_id}].description",
                "concern": (
                    "Task description is empty; implementer has no "
                    "statement of intent to work against."
                ),
                "suggested_change": (
                    "Populate tasks[002].description with a 2-3 sentence "
                    "statement of what TASK-002 is supposed to accomplish "
                    "and why."
                ),
            }
        ],
        "notes": [],
        "schedule_ok": False,
        "summary": (
            "Schedule is well-shaped structurally but TASK-002 has a blank "
            "description; implementer cannot work without intent."
        ),
    }
    fake = _fake_codex_returning(canned)
    monkeypatch.setattr(wrapper, "invoke_codex", fake)

    args = _plan_review_args(sidecar, tmp_path)
    rc = wrapper.cmd_plan_review(args)
    out = capsys.readouterr().out
    envelope = json.loads(out)

    assert rc == 0, envelope
    parsed = envelope["parsed"]
    findings = parsed.get("findings", [])
    assert findings, "expected at least one finding"
    target_finding = next(
        (
            f for f in findings
            if f.get("section", "").startswith(f"tasks[{target_id}]")
        ),
        None,
    )
    assert target_finding is not None, (
        f"no finding points at tasks[{target_id}]; got sections: "
        f"{[f.get('section') for f in findings]}"
    )
    assert target_finding.get("section") == (
        f"tasks[{target_id}].description"
    ), target_finding
    assert target_finding.get("suggested_change", "").strip(), (
        "suggested_change must be non-empty"
    )


# ---------------------------------------------------------------------------
# 3. Argv + helper-signature invariants — no --plan-file; no TypeError
#    when the render helper is called without plan_text/plan_basename/
#    plan_path actuals.
# ---------------------------------------------------------------------------


def test_plan_review_argv_no_plan_file_and_render_prompt_no_plan_text(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance: the argv that the orchestrator constructs for
    `plan-review` dispatch MUST NOT carry `--plan-file`. Additionally, the
    internal `render_plan_review_prompt` helper accepts the new signature
    with no `plan_text` / `plan_basename` / `plan_path` actuals without
    raising `TypeError`."""
    # Part A — argv composition (constructed the same way the orchestrator
    # would assemble it): schedule-only, no --plan-file.
    schedule = tmp_path / "some_plan.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "gaps": [],
        }),
        encoding="utf-8",
    )
    argv_under_test = [
        "plan-review",
        "--schedule-file", str(schedule),
        "--repo-root", str(tmp_path),
    ]
    # Explicit positive and negative assertions on the argv shape.
    assert "--plan-file" not in argv_under_test, (
        "argv must not carry --plan-file (TASK-006 schedule-only contract)"
    )
    assert "--schedule-file" in argv_under_test
    assert "--repo-root" in argv_under_test

    # The wrapper's argparse must accept this argv — no required-argument
    # error on the missing --plan-file / --plans-dir.
    parser = wrapper._build_parser()
    args = parser.parse_args(argv_under_test)
    assert args.subcommand == "plan-review"
    assert args.schedule_file == str(schedule)
    assert getattr(args, "plan_file", None) is None
    assert getattr(args, "plans_dir", None) is None

    # Part B — render helper signature: calling with only schedule_json
    # (positional), and optionally plan_basename, must succeed.
    # Failing this would be the TypeError the AC warns against.
    schedule_json = schedule.read_text(encoding="utf-8")
    # No plan_text / plan_basename / plan_path actuals — the helper must
    # accept this call shape without raising.
    prompt_only_schedule = wrapper.render_plan_review_prompt(schedule_json)
    assert isinstance(prompt_only_schedule, str)
    assert "Persisted schedule JSON:" in prompt_only_schedule
    # Plan-markdown block must NOT appear in the rendered prompt (the
    # schedule-only contract). Historical pre-TASK-006 prompts carried a
    # "Plan document (verbatim):" section.
    assert "Plan document (verbatim):" not in prompt_only_schedule, (
        "schedule-only prompt must not carry the plan-markdown block"
    )

    # Signature introspection: the required positional is schedule_json;
    # the plan-markdown actuals are optional (kwargs with None defaults).
    sig = inspect.signature(wrapper.render_plan_review_prompt)
    params = sig.parameters
    assert "schedule_json" in params, params
    # Per AC, any legacy plan_* params must default to None (or be absent).
    for legacy in ("plan_text", "plan_basename", "plan_path", "plan_abs_path"):
        if legacy in params:
            p = params[legacy]
            assert p.default is None, (
                f"legacy param {legacy!r} must default to None "
                f"(got {p.default!r})"
            )

    # Part C — dispatch the wrapper with the argv-under-test and assert the
    # prompt the fake Codex received does NOT embed any plan-markdown block
    # and that envelope.plan_file round-trips from the sidecar stem.
    canned = {
        "plan_file": schedule.stem.replace(".schedule", ""),
        "verdict": "approved",
        "findings": [],
        "notes": [],
        "schedule_ok": True,
        "summary": "empty fixture",
    }
    fake = _fake_codex_returning(canned)
    monkeypatch.setattr(wrapper, "invoke_codex", fake)

    rc = wrapper.cmd_plan_review(args)
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["outcome"] == "success", envelope
    # The fake recorded the prompt: it must not carry the legacy
    # plan-markdown block.
    assert fake.captured["prompt"] is not None
    assert "Plan document (verbatim):" not in fake.captured["prompt"], (
        "cmd_plan_review should render schedule-only prompt"
    )
    # The prompt must still carry the schedule JSON block and the task
    # section-reference guidance (new to TASK-006).
    assert "Persisted schedule JSON:" in fake.captured["prompt"]
    assert "tasks[i]" in fake.captured["prompt"], (
        "prompt should instruct reviewer to use tasks[i] section paths"
    )


# ---------------------------------------------------------------------------
# 4. TASK-008 — --plan-file / --plans-dir removed from argparse; the legacy
#    positional / kwarg signature of render_plan_review_prompt is gone too.
#    The transition tests (pre-TASK-008) are retired.
# ---------------------------------------------------------------------------


def test_plan_review_argv_rejects_plan_file_flag():
    """TASK-008: `--plan-file` is no longer a recognized argparse flag on
    the `plan-review` subcommand. argparse must reject any argv that tries
    to pass it, making accidental usage by stale callers a loud failure
    rather than a silent no-op.
    """
    import pytest as _pytest  # local import: keep top of module lean

    parser = wrapper._build_parser()
    argv = [
        "plan-review",
        "--plan-file", "/tmp/stale.md",  # retired flag
        "--schedule-file", "/tmp/x.json",
        "--repo-root", "/tmp",
    ]
    with _pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_render_plan_review_prompt_rejects_legacy_kwargs():
    """TASK-008: the render helper no longer accepts the legacy `plan_text`
    / `plan_abs_path` / `plans_dir` keyword arguments. Stale callers get a
    `TypeError` rather than a silent no-op, making migration obvious."""
    import pytest as _pytest

    schedule_json = json.dumps({
        "outcome": "valid",
        "tasks": [],
        "batches": [],
        "gaps": [],
    })
    for legacy_kwarg in ("plan_text", "plan_abs_path", "plans_dir"):
        with _pytest.raises(TypeError):
            wrapper.render_plan_review_prompt(
                schedule_json, **{legacy_kwarg: "stale"},
            )


# ---------------------------------------------------------------------------
# TASK-004/TASK-009: timeout scaling helpers + envelope plumbing.
# ---------------------------------------------------------------------------


import pytest  # noqa: E402  (intentional late import — keeps top of module lean)


@pytest.mark.parametrize(
    "num_files, expected",
    [
        (0, 600),
        (1, 690),
        (2, 780),
        (5, 1050),
        (6, 1140),
        (7, 1230),
        (10, 1500),
    ],
)
def test_compute_implement_timeout_baseline_weight(num_files, expected):
    """The implement default grows from a 600 s base by declared file count."""
    assert wrapper.compute_implement_timeout(num_files) == expected


def test_compute_implement_timeout_uses_task_shape_for_complex_test_work():
    """TASK-009 regression: a multi-surface E2E/parser hardening task should
    not be capped at the old 300 s floor just because it declares five file
    entries."""
    files = [
        "tests/scripts/test_phase_1_5_e2e.py (edit)",
        "tests/scripts/fixtures/phase_1_5_plan/ (edit)",
        "tests/scripts/test_plan_review_state.py (edit)",
        "tests/scripts/test_plan_ops_plan_review_route.py (edit)",
        "tests/scripts/test_plan_ops.py (edit)",
    ]
    timeout = wrapper.compute_implement_timeout(
        len(files),
        acceptance_criteria_count=6,
        test_command=(
            'venv/bin/python -m pytest tests/scripts/test_phase_1_5_e2e.py '
            'tests/scripts/test_plan_review_state.py && '
            'venv/bin/python -m pytest tests/scripts/test_plan_ops.py '
            '-k "parse_plan_review or plan_review_triage"'
        ),
        files=files,
        description=(
            "Tighten E2E assertion coverage for parser, route, fixture, "
            "and plan_review_state round-trip behavior."
        ),
    )
    assert timeout == wrapper.MAX_TIMEOUT_IMPLEMENT
    assert timeout == 1800


@pytest.mark.parametrize(
    "num_files, expected",
    [
        (0, 180),
        (1, 180),
        (2, 180),
        (6, 180),
        (7, 210),
        (8, 240),
        (10, 300),
    ],
)
def test_compute_review_timeout_floor(num_files, expected):
    """``compute_review_timeout(N)`` returns ``max(180, 30*N)``. The
    friction run's 8-file diff hit 180 s mid-verification; the new
    scaling yields 240 s."""
    assert wrapper.compute_review_timeout(num_files) == expected


def test_compute_timeouts_clamp_negative_inputs():
    """Defensive: a negative/garbage ``num_files`` should clamp to 0
    and return the floor rather than yielding a sub-floor timeout."""
    assert wrapper.compute_implement_timeout(-3) == 600
    assert wrapper.compute_review_timeout(-3) == 180


def test_add_common_timeout_default_is_none():
    """``--timeout`` on implement/review parsers defaults to ``None``;
    the cmd handler derives the effective default from the task shape.
    The plan-review subparser also defaults to ``None`` (resolved to the
    flat ``DEFAULT_TIMEOUT_PLAN_REVIEW`` inside ``cmd_plan_review``)."""
    parser = wrapper._build_parser()

    impl_args = parser.parse_args([
        "implement",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
    ])
    assert impl_args.timeout is None, (
        f"--timeout default must be None for cmd_implement to derive "
        f"task-shape-aware default; got {impl_args.timeout!r}"
    )

    rev_args = parser.parse_args([
        "review",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
    ])
    assert rev_args.timeout is None, rev_args.timeout

    pr_args = parser.parse_args([
        "plan-review",
        "--schedule-file", "/tmp/s.json",
        "--repo-root", "/tmp",
    ])
    assert pr_args.timeout is None, pr_args.timeout


def _impl_plan(plan: Path, task_id: str, files: list[str]) -> None:
    """Build a minimal plan markdown for cmd_implement to parse. The
    plan only needs to satisfy ``parse_task_block`` — the test fakes
    Codex so the fields beyond Files / Test command don't influence
    the timeout-resolution path under test."""
    bullets = "\n".join(f"  - {f}" for f in files)
    plan.write_text(
        "\n".join([
            "# Plan",
            "",
            "## Context",
            "",
            "(unused).",
            "",
            "## Tasks",
            "",
            f"### TASK-{task_id}: scaling test",
            "",
            "- **Status:** pending",
            "- **Priority:** medium",
            "- **Files:**",
            bullets,
            "- **Test command:** `none`",
            "- **Acceptance criteria:**",
            "  - It works.",
            "",
            "**Description:**",
            "Test fixture.",
            "",
        ]),
        encoding="utf-8",
    )


def _impl_args(
    plan: Path,
    repo: Path,
    task_id: str = "001",
    *,
    timeout=None,
    dry_run: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_file=str(plan),
        task_id=task_id,
        repo_root=str(repo),
        json=True,
        dry_run=dry_run,
        timeout=timeout,
    )


def _impl_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_implement_envelope_carries_effective_timeout_and_baseline_error(
    tmp_path, monkeypatch, capsys,
):
    """On a Codex timeout, the implement envelope MUST carry both:
      * ``effective_timeout`` — the task-shape-aware cap actually used.
      * ``baseline_error`` — the truncated ``_snapshot_baseline`` error
        message when baseline capture failed (synthetic git boom here).
    """
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # 7 files → compute_implement_timeout(7) == 1230.
    seven_files = [f"file_{i}.py" for i in range(7)]
    _impl_plan(plan, "001", seven_files)

    # Synthetic baseline failure: forces _snapshot_baseline to capture
    # the underlying error message and forward it through the envelope.
    boom_msg = "git broken: fake snapshot failure"

    def boom(repo_root):
        raise subprocess.SubprocessError(boom_msg)

    monkeypatch.setattr(wrapper, "git_changed_files", boom)

    # Fake Codex returns timeout immediately; nothing else needs to be
    # exercised — the envelope shape under test is the timeout branch.
    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.5,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)

    assert rc == 1, envelope
    assert envelope["outcome"] == "timeout", envelope
    # effective_timeout matches compute_implement_timeout(len(files)).
    assert envelope["effective_timeout"] == wrapper.compute_implement_timeout(
        len(seven_files),
        acceptance_criteria_count=1,
        test_command="none",
        files=seven_files,
        description="Test fixture.",
    ), envelope
    assert envelope["effective_timeout"] == 1575, envelope
    # baseline_error carries the truncated message; baseline_captured False.
    assert envelope["baseline_captured"] is False, envelope
    assert envelope["baseline_error"] == boom_msg, envelope
    # Truncation cap: 200 chars max.
    assert envelope["baseline_error"] is not None
    assert len(envelope["baseline_error"]) <= 200


def test_implement_envelope_baseline_error_truncated_to_200_chars(
    tmp_path, monkeypatch, capsys,
):
    """``baseline_error`` is truncated at 200 chars to keep envelopes
    bounded even on pathological git error messages."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _impl_plan(plan, "001", ["a.py"])

    long_msg = "X" * 500

    def boom(repo_root):
        raise subprocess.SubprocessError(long_msg)

    monkeypatch.setattr(wrapper, "git_changed_files", boom)

    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["outcome"] == "timeout", envelope
    assert envelope["baseline_error"] is not None
    assert len(envelope["baseline_error"]) == 200, len(envelope["baseline_error"])


def test_implement_envelope_baseline_error_null_when_capture_succeeds(
    tmp_path, monkeypatch, capsys,
):
    """When ``_snapshot_baseline`` succeeds, ``baseline_error`` is
    ``None`` — the field must still be present in the timeout envelope."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _impl_plan(plan, "001", ["a.py"])

    # Baseline capture succeeds (real git_changed_files runs against a
    # fresh repo). Codex still times out.
    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["outcome"] == "timeout", envelope
    assert envelope["baseline_captured"] is True, envelope
    assert envelope["baseline_error"] is None, envelope
    # effective_timeout still recorded.
    assert "effective_timeout" in envelope, envelope


def test_explicit_timeout_override_skips_scaling(
    tmp_path, monkeypatch, capsys,
):
    """When the operator passes ``--timeout N``, the scaling is bypassed:
    ``effective_timeout`` MUST be exactly N regardless of len(files), and
    the resolved value MUST be the timeout actually passed to
    ``invoke_codex``."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # 10 files would scale to 1500 s under the default formula; the
    # operator override of 100 s must win.
    ten_files = [f"file_{i}.py" for i in range(10)]
    _impl_plan(plan, "001", ten_files)

    seen = {"timeout_sec": None}

    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        seen["timeout_sec"] = timeout_sec
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(
        _impl_args(plan, repo, timeout=100),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["effective_timeout"] == 100, envelope
    # The wrapper-derived default for 10 files would be 1500 — assert
    # the override is genuinely shorter so we know scaling was skipped.
    assert envelope["effective_timeout"] != wrapper.compute_implement_timeout(10)
    # And the same value flowed into the subprocess timeout.
    assert seen["timeout_sec"] == 100


def test_implement_default_timeout_derives_from_task_shape(
    tmp_path, monkeypatch, capsys,
):
    """When ``--timeout`` is not passed (``args.timeout is None``),
    ``cmd_implement`` resolves the effective timeout from the parsed task
    shape and forwards that same value into ``invoke_codex``."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    eight_files = [f"file_{i}.py" for i in range(8)]
    _impl_plan(plan, "001", eight_files)

    seen = {"timeout_sec": None}

    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        seen["timeout_sec"] = timeout_sec
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    # No timeout override → wrapper-derived default.
    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    expected = wrapper.compute_implement_timeout(
        8,
        acceptance_criteria_count=1,
        test_command="none",
        files=eight_files,
        description="Test fixture.",
    )
    assert expected == 1665, expected
    assert envelope["effective_timeout"] == expected, envelope
    assert seen["timeout_sec"] == expected


def _review_args_for_timeout(
    plan: Path,
    repo: Path,
    *,
    timeout=None,
    files: str = "",
    dry_run: bool = True,
) -> argparse.Namespace:
    """Args helper for cmd_review timeout-scaling tests. Defaults to
    ``dry_run=True`` so the test never hits ``invoke_codex``."""
    return argparse.Namespace(
        plan_file=str(plan),
        repo_root=str(repo),
        task_id="005",
        files=files,
        review_focus="bugs",
        dry_run=dry_run,
        timeout=timeout,
    )


def test_review_default_timeout_derives_from_files_count(
    tmp_path, capsys,
):
    """``cmd_review`` derives ``effective_timeout`` from
    ``len(review_files)`` when ``--timeout`` is unset. The dry-run
    envelope surfaces the resolved value so the operator can validate
    the scaling without dispatching Codex."""
    repo = _review_git_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _review_plan(plan, "new.txt")
    # Pass --files with 7 entries → compute_review_timeout(7) == 210.
    seven = ",".join(f"f{i}.py" for i in range(7))

    rc = wrapper.cmd_review(
        _review_args_for_timeout(plan, repo, files=seven, dry_run=True),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    assert envelope["outcome"] == "dry_run", envelope
    assert envelope["effective_timeout"] == 210, envelope


def test_review_explicit_timeout_override_wins(tmp_path, capsys):
    """Operator override of ``--timeout 50`` short-circuits the
    file-count scaling for ``cmd_review`` too."""
    repo = _review_git_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _review_plan(plan, "new.txt")
    seven = ",".join(f"f{i}.py" for i in range(7))

    rc = wrapper.cmd_review(
        _review_args_for_timeout(
            plan, repo, files=seven, timeout=50, dry_run=True,
        ),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    assert envelope["effective_timeout"] == 50, envelope


def test_plan_review_dry_run_envelope_carries_effective_timeout(
    tmp_path, capsys,
):
    """``cmd_plan_review`` surfaces ``effective_timeout`` even though
    plan-review keeps a flat default — the run-log records what cap
    actually applied. Default is ``DEFAULT_TIMEOUT_PLAN_REVIEW``;
    operator override still wins."""
    schedule = tmp_path / "x.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "gaps": [],
        }),
        encoding="utf-8",
    )

    # No --timeout override → flat default.
    args_default = argparse.Namespace(
        schedule_file=str(schedule),
        repo_root=str(tmp_path),
        json=True,
        dry_run=True,
        timeout=None,
        allow_gaps=False,
    )
    rc = wrapper.cmd_plan_review(args_default)
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["effective_timeout"] == wrapper.DEFAULT_TIMEOUT_PLAN_REVIEW
    assert envelope["effective_timeout"] == 180

    # Explicit --timeout override still wins.
    args_override = argparse.Namespace(
        schedule_file=str(schedule),
        repo_root=str(tmp_path),
        json=True,
        dry_run=True,
        timeout=42,
        allow_gaps=False,
    )
    rc = wrapper.cmd_plan_review(args_override)
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["effective_timeout"] == 42, envelope


# ---------------------------------------------------------------------------
# TASK-007 — target_task_id first-class field for shared-file children.
# Tests cover (a) >1-heading + target_task_id → injection, (b) 1-heading
# + target_task_id → no injection, (c) >1-heading + None target_task_id →
# structured renderer error, (d) backward compat: single-task plan files
# render unchanged, (e) cmd_implement / cmd_review forward --target-task-id.
# ---------------------------------------------------------------------------


SHARED_FILE_PLAN = """# Shared-file plan

## Context

Three sibling sub-tasks share one child file (TASK-007 fixture).

## Tasks

### TASK-027A: first sibling

- **Status:** pending
- **Priority:** high
- **Files:**
  - `foo.py`
- **Test command:** `pytest -q tests/test_foo.py`
- **Acceptance criteria:**
  - foo behaviour
- **Dependencies:** []

**Description:**
Do the foo work.

### TASK-027B: second sibling

- **Status:** pending
- **Priority:** high
- **Files:**
  - `bar.py`
- **Test command:** `pytest -q tests/test_bar.py`
- **Acceptance criteria:**
  - bar behaviour
- **Dependencies:** []

**Description:**
Do the bar work.

### TASK-027C: third sibling

- **Status:** pending
- **Priority:** high
- **Files:**
  - `baz.py`
- **Test command:** `pytest -q tests/test_baz.py`
- **Acceptance criteria:**
  - baz behaviour
- **Dependencies:** []

**Description:**
Do the baz work.
"""


SINGLE_TASK_PLAN = """# Single-task plan

## Context

One task, one heading.

## Tasks

### TASK-001: only sibling

- **Status:** pending
- **Priority:** high
- **Files:**
  - `solo.py`
- **Test command:** `pytest -q tests/test_solo.py`
- **Acceptance criteria:**
  - solo behaviour
- **Dependencies:** []

**Description:**
Do the solo work.
"""


def test_target_task_id_render_implement_prompt_injects_when_multi_heading(
    tmp_path,
):
    """(a) >1 heading + target_task_id → "Implement specifically `### TASK-NNN:`"
    is the FIRST instruction line of the rendered prompt."""
    plan_path = tmp_path / "shared.md"
    plan_path.write_text(SHARED_FILE_PLAN, encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")
    task = wrapper.parse_task_block(plan_text, "027B")
    prompt = wrapper.render_implement_prompt(
        task,
        context="ctx",
        plan_text=plan_text,
        target_task_id="027B",
        plan_file=str(plan_path),
    )
    # The injection MUST be the first non-empty content of the prompt
    # (no pre-read excerpts in this fixture, so it's literally line 0).
    assert prompt.startswith("Implement specifically `### TASK-027B:`"), prompt[:200]


def test_target_task_id_render_implement_prompt_noop_when_single_heading(
    tmp_path,
):
    """(b) 1 heading + target_task_id → no injection (the heading is
    unambiguous; the line would only add noise)."""
    plan_path = tmp_path / "solo.md"
    plan_path.write_text(SINGLE_TASK_PLAN, encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")
    task = wrapper.parse_task_block(plan_text, "001")
    prompt = wrapper.render_implement_prompt(
        task,
        context="ctx",
        plan_text=plan_text,
        target_task_id="001",
        plan_file=str(plan_path),
    )
    assert "Implement specifically `### TASK-" not in prompt, prompt[:200]
    assert prompt.startswith("Implement TASK-001"), prompt[:200]


def test_target_task_id_render_implement_prompt_raises_when_missing(
    tmp_path,
):
    """(c) >1 heading + target_task_id is None → structured renderer
    error naming the offending plan file."""
    import plan_ops  # type: ignore  # imported via the wrapper's sys.path

    plan_path = tmp_path / "shared.md"
    plan_path.write_text(SHARED_FILE_PLAN, encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")
    task = wrapper.parse_task_block(plan_text, "027B")
    try:
        wrapper.render_implement_prompt(
            task,
            context="ctx",
            plan_text=plan_text,
            target_task_id=None,
            plan_file=str(plan_path),
        )
    except plan_ops.MissingTargetTaskIdError as e:
        assert e.plan_file == str(plan_path)
        assert e.heading_count == 3, e.heading_count
        assert "target_task_id" in str(e)
    else:
        raise AssertionError(
            "render_implement_prompt should have raised "
            "MissingTargetTaskIdError for >1 heading + None target_task_id"
        )


def test_target_task_id_backward_compat_single_task_plan_renders_unchanged(
    tmp_path,
):
    """(d) Backward compat: single-task plan files render unchanged when
    target_task_id is None (the pre-TASK-007 default for non-shared
    children). No injection, no error."""
    plan_path = tmp_path / "solo.md"
    plan_path.write_text(SINGLE_TASK_PLAN, encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")
    task = wrapper.parse_task_block(plan_text, "001")
    prompt_with_text = wrapper.render_implement_prompt(
        task,
        context="ctx",
        plan_text=plan_text,
        target_task_id=None,
        plan_file=str(plan_path),
    )
    # Compare against the pre-TASK-007 call (no plan_text → no injection
    # path engaged at all). Both must produce identical output.
    prompt_legacy = wrapper.render_implement_prompt(task, "ctx")
    assert prompt_with_text == prompt_legacy, (
        "single-task render with target_task_id=None must equal the "
        "pre-TASK-007 legacy render"
    )
    assert "Implement specifically" not in prompt_with_text


def test_target_task_id_render_review_prompt_injects_when_multi_heading(
    tmp_path,
):
    """The review render path applies the same auto-injection rule."""
    plan_path = tmp_path / "shared.md"
    plan_path.write_text(SHARED_FILE_PLAN, encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")
    task = wrapper.parse_task_block(plan_text, "027C")
    prompt = wrapper.render_review_prompt(
        task,
        diff="(diff)",
        review_focus="bugs",
        review_files=["baz.py"],
        plan_text=plan_text,
        target_task_id="027C",
        plan_file=str(plan_path),
    )
    assert prompt.startswith("Implement specifically `### TASK-027C:`"), prompt[:200]


def test_target_task_id_render_review_prompt_raises_when_missing(tmp_path):
    """Review path also raises MissingTargetTaskIdError on a shared-file
    child with target_task_id=None."""
    import plan_ops  # type: ignore

    plan_path = tmp_path / "shared.md"
    plan_path.write_text(SHARED_FILE_PLAN, encoding="utf-8")
    plan_text = plan_path.read_text(encoding="utf-8")
    task = wrapper.parse_task_block(plan_text, "027A")
    try:
        wrapper.render_review_prompt(
            task,
            diff="(diff)",
            review_focus="bugs",
            review_files=["foo.py"],
            plan_text=plan_text,
            target_task_id=None,
            plan_file=str(plan_path),
        )
    except plan_ops.MissingTargetTaskIdError:
        pass
    else:
        raise AssertionError("review path must raise on missing target_task_id")


def test_target_task_id_cmd_implement_dry_run_carries_injection(
    tmp_path, capsys,
):
    """cmd_implement (--target-task-id=NNN, dry-run) emits the
    auto-injection line at the top of prompt_preview when the plan file
    declares >1 heading."""
    plan_path = tmp_path / "shared.md"
    plan_path.write_text(SHARED_FILE_PLAN, encoding="utf-8")
    args = argparse.Namespace(
        plan_file=str(plan_path),
        task_id="027B",
        repo_root=str(tmp_path),
        json=True,
        dry_run=True,
        timeout=None,
        target_task_id="027B",
    )
    rc = wrapper.cmd_implement(args)
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    preview = envelope["prompt_preview"]
    assert preview.startswith("Implement specifically `### TASK-027B:`"), preview[:200]


def test_target_task_id_cmd_implement_missing_emits_failure_envelope(
    tmp_path, capsys,
):
    """cmd_implement on a >1-heading plan with no --target-task-id emits
    a `failure` envelope referencing MissingTargetTaskIdError."""
    plan_path = tmp_path / "shared.md"
    plan_path.write_text(SHARED_FILE_PLAN, encoding="utf-8")
    args = argparse.Namespace(
        plan_file=str(plan_path),
        task_id="027A",
        repo_root=str(tmp_path),
        json=True,
        dry_run=True,
        timeout=None,
        target_task_id=None,
    )
    rc = wrapper.cmd_implement(args)
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["outcome"] == "failure"
    assert "target_task_id" in envelope["error"]
    assert envelope.get("heading_count") == 3
    assert envelope.get("plan_file") == str(plan_path)


def test_target_task_id_argparse_exposes_flag():
    """`--target-task-id` is accepted on both `implement` and `review`
    subcommands. argparse default is None (omission ≡ 'no override')."""
    parser = wrapper._build_parser()
    impl = parser.parse_args([
        "implement",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
        "--target-task-id", "027B",
    ])
    assert impl.target_task_id == "027B"

    rev = parser.parse_args([
        "review",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
        "--target-task-id", "027C",
    ])
    assert rev.target_task_id == "027C"

    # Default is None when the flag is omitted.
    impl_default = parser.parse_args([
        "implement",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
    ])
    assert impl_default.target_task_id is None


# ---------------------------------------------------------------------------
# TASK-008 (POSTMORTEM_FIXES) — sandbox-divergence escape hatch.
#
# The Codex wrapper's `implement` failure envelope (cause:
# independent_test_run_failed) MUST carry sandbox_test_stdout,
# sandbox_test_stderr, sandbox_test_command, sandbox_test_exit_code,
# and sandbox_test_attempt_count so the orchestrator's auto-validate
# branch can distinguish a "real test red" from a sandbox-only
# divergence (missing dep, permission, path, etc.) before classifying
# the failure.
#
# Each stream is capped at SANDBOX_TEST_CAPTURE_CAP (32 KB); when
# truncation fires the original byte length is recorded as
# `sandbox_test_*_truncated_to`.
# ---------------------------------------------------------------------------


def _impl_plan_with_test_cmd(
    plan: Path, task_id: str, files: list[str], test_cmd: str,
) -> None:
    """Variant of `_impl_plan` that emits an explicit Test command:.

    The TASK-008 sandbox-divergence path requires a real (failing)
    test command in the parsed task block so `cmd_implement`'s
    independent test re-run reaches the failure branch under test.
    """
    bullets = "\n".join(f"  - {f}" for f in files)
    plan.write_text(
        "\n".join([
            "# Plan",
            "",
            "## Context",
            "",
            "(unused).",
            "",
            "## Tasks",
            "",
            f"### TASK-{task_id}: sandbox divergence test",
            "",
            "- **Status:** pending",
            "- **Priority:** medium",
            "- **Files:**",
            bullets,
            f"- **Test command:** {test_cmd}",
            "- **Acceptance criteria:**",
            "  - It works.",
            "",
            "**Description:**",
            "Test fixture.",
            "",
        ]),
        encoding="utf-8",
    )


def _make_codex_writer(parsed_body: dict, on_disk_files: dict[str, str]):
    """Return a fake invoke_codex that writes ``parsed_body`` to the
    output file AND mutates the repo to create ``on_disk_files`` so
    the wrapper's scope/dishonesty checks see a consistent picture.

    Keys of ``on_disk_files`` are paths relative to ``workdir``;
    values are the file contents.
    """

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        for rel, content in on_disk_files.items():
            target = Path(workdir) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        Path(output_path).write_text(
            json.dumps(parsed_body), encoding="utf-8",
        )
        return {
            "status": "ok",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "file_changes": list(on_disk_files.keys()),
            "wall_seconds": 0.1,
        }

    return fake


def _codex_completed_body(task_id: str, files_changed: list[str]) -> dict:
    """Minimal Codex parsed body that satisfies the implement schema.

    Includes the five new TASK-008 fields as null so the wrapper's
    structured-output schema validates AND the wrapper does not surface
    them on the success path (no
    ``sandbox_test_stdout`` / ``sandbox_test_stderr`` etc. in the
    envelope unless the failure branch fires).
    """
    return {
        "task_id": task_id,
        "status": "completed",
        "summary": "ok",
        "files_changed": files_changed,
        "tests_run": [],
        "blockers": [],
        "concerns": [],
        "plan_adaptations": [],
        "sandbox_test_stdout": None,
        "sandbox_test_stderr": None,
        "sandbox_test_command": None,
        "sandbox_test_exit_code": None,
        "sandbox_test_attempt_count": None,
    }


def test_implement_independent_test_run_failure_envelope_carries_sandbox_fields(
    tmp_path, monkeypatch, capsys,
):
    """Failure envelope (cause: independent_test_run_failed) MUST carry
    all five sandbox_test_* fields plus `cause`.

    Sandbox stdout/stderr come from the test re-run's captures; command,
    exit_code, attempt_count come from the same `test_result` dict.
    """
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # Test command always exits 1 with stdout marker; flaky retry caps at 2.
    test_cmd = "echo SANDBOX_OUT && echo SANDBOX_ERR 1>&2 && exit 1"
    _impl_plan_with_test_cmd(plan, "001", ["a.py"], test_cmd)

    body = _codex_completed_body("001", ["a.py"])
    monkeypatch.setattr(
        wrapper, "invoke_codex",
        _make_codex_writer(body, {"a.py": "x = 1\n"}),
    )

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)

    assert rc == 1, envelope
    assert envelope["outcome"] == "failure", envelope
    assert envelope.get("cause") == "independent_test_run_failed", envelope
    # All five new fields present with concrete values.
    assert "sandbox_test_stdout" in envelope, envelope
    assert "sandbox_test_stderr" in envelope, envelope
    assert "sandbox_test_command" in envelope, envelope
    assert "sandbox_test_exit_code" in envelope, envelope
    assert "sandbox_test_attempt_count" in envelope, envelope
    # parse_task_block strips backticks from `Test command:` but the
    # exact form depends on the wrapper's normalization rule; assert
    # the substantive command text round-trips.
    assert "echo SANDBOX_OUT" in envelope["sandbox_test_command"]
    # Two attempts (flaky-retry) before classifying as failed.
    assert envelope["sandbox_test_attempt_count"] == 2, envelope
    # Exit code is non-zero.
    assert envelope["sandbox_test_exit_code"] == 1, envelope
    # Stdout / stderr captured separately (mixing happens in output_tail).
    assert "SANDBOX_OUT" in envelope["sandbox_test_stdout"], envelope
    assert "SANDBOX_ERR" in envelope["sandbox_test_stderr"], envelope
    # Truncation markers absent on small captures.
    assert "sandbox_test_stdout_truncated_to" not in envelope, envelope
    assert "sandbox_test_stderr_truncated_to" not in envelope, envelope


def test_implement_independent_test_run_envelope_truncates_at_32kb(
    tmp_path, monkeypatch, capsys,
):
    """Stdout / stderr captures larger than SANDBOX_TEST_CAPTURE_CAP (32 KB)
    are truncated and surfaced with the `sandbox_test_*_truncated_to`
    marker carrying the original byte length."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # Emit a stream much larger than 32 KB on stdout and stderr, then
    # fail. Write the test driver to a script file inside the repo to
    # sidestep shell-quote escaping (the parsed `Test command:` field
    # round-trips through markdown's backtick stripping).
    big_chars = 64 * 1024  # 64 KB worth of 1-byte chars
    driver = repo / "noisy_fail.py"
    driver.write_text(
        "import sys\n"
        f"sys.stdout.write('A' * {big_chars})\n"
        f"sys.stderr.write('B' * {big_chars})\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    test_cmd = f"{sys.executable} {driver}"
    _impl_plan_with_test_cmd(plan, "001", ["a.py"], test_cmd)

    body = _codex_completed_body("001", ["a.py"])
    monkeypatch.setattr(
        wrapper, "invoke_codex",
        _make_codex_writer(body, {"a.py": "x = 1\n"}),
    )

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)

    cap = wrapper.SANDBOX_TEST_CAPTURE_CAP
    assert rc == 1, envelope
    assert envelope["outcome"] == "failure", envelope
    # Captured streams capped at 32 KB exactly.
    assert len(envelope["sandbox_test_stdout"].encode("utf-8")) == cap, envelope
    assert len(envelope["sandbox_test_stderr"].encode("utf-8")) == cap, envelope
    # Truncation markers carry the original byte length (= big_chars).
    assert envelope["sandbox_test_stdout_truncated_to"] == big_chars, envelope
    assert envelope["sandbox_test_stderr_truncated_to"] == big_chars, envelope


def test_implement_success_envelope_omits_sandbox_divergence_fields(
    tmp_path, monkeypatch, capsys,
):
    """The wrapper-level sandbox_test_* fields are added only on the
    independent-test-run failure path. A success envelope MUST NOT
    surface them at the top level (they may still exist on the inner
    `parsed.sandbox_test_*` as `null` from the structured-output
    contract — those are Codex-side, not wrapper-side)."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # `none` short-circuits run_test_command to result=not_run, which
    # the wrapper treats as success-path (no failure classification).
    _impl_plan_with_test_cmd(plan, "001", ["a.py"], "none")

    body = _codex_completed_body("001", ["a.py"])
    monkeypatch.setattr(
        wrapper, "invoke_codex",
        _make_codex_writer(body, {"a.py": "x = 1\n"}),
    )

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)

    assert rc == 0, envelope
    assert envelope["outcome"] == "success", envelope
    # The wrapper-level top-level surfaces are absent on success.
    for fld in (
        "sandbox_test_stdout", "sandbox_test_stderr",
        "sandbox_test_command", "sandbox_test_exit_code",
        "sandbox_test_attempt_count",
    ):
        assert fld not in envelope, (fld, envelope)
    assert envelope.get("cause") is None or "cause" not in envelope


def test_implement_schema_declares_sandbox_divergence_fields_optional_but_required():
    """codex_implement_schema.json declares the five new TASK-008 fields
    as optional-but-required (typed as nullable) and they appear in
    `required` so the OpenAI strict-output invariant
    set(required) == set(properties.keys()) holds."""
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "plugins" / "plan-executor" / "scripts"
        / "codex_implement_schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    properties = schema["properties"]
    required = set(schema["required"])

    for fld in (
        "sandbox_test_stdout", "sandbox_test_stderr",
        "sandbox_test_command",
    ):
        assert fld in properties, fld
        assert "null" in properties[fld]["type"], (fld, properties[fld])
        assert "string" in properties[fld]["type"], (fld, properties[fld])
        assert fld in required, fld
    for fld in ("sandbox_test_exit_code", "sandbox_test_attempt_count"):
        assert fld in properties, fld
        assert "null" in properties[fld]["type"], (fld, properties[fld])
        assert "integer" in properties[fld]["type"], (fld, properties[fld])
        assert fld in required, fld

    # The structural invariant from CODEX_FRICTION TASK-001 still holds.
    assert required == set(properties.keys())


# ---------------------------------------------------------------------------
# TASK-009 (POSTMORTEM_FIXES): canonical verdict allowlists embedded in Codex
# review + plan-review prompts. Tests assert (a) the allowlist section is
# present, (b) all four role names appear, (c) the role-disambiguation
# paragraph is present, (d) for plan-review the per-task ownership block is
# present, and (e) the renderer reads the canonical constants dynamically
# (load-bearing — without this guarantee a future module could embed a
# snapshot at import time).
# ---------------------------------------------------------------------------


SHARED_PLAN_FOR_VERDICT_TEST = (
    "### TASK-005: Verdict allowlist render fixture\n"
    "- **Status:** pending\n"
    "- **Priority:** P1\n"
    "- **Files:**\n"
    "  - foo.py\n"
    "\n"
    "**Description:**\n"
    "Fixture for verdict-allowlist tests.\n"
    "\n"
)


def _render_review_prompt_for_verdict_tests():
    task = wrapper.parse_task_block(SHARED_PLAN_FOR_VERDICT_TEST, "005")
    return wrapper.render_review_prompt(
        task,
        diff="(diff)",
        review_focus="bugs",
        review_files=["foo.py"],
    )


def _render_plan_review_prompt_for_verdict_tests(tasks=None):
    schedule = {
        "outcome": "valid",
        "tasks": tasks if tasks is not None else [
            {"task_id": "001", "files": ["a.py", "b.py"]},
            {"task_id": "002", "files": ["c.py"]},
        ],
        "batches": [],
        "gaps": [],
    }
    return wrapper.render_plan_review_prompt(json.dumps(schedule))


def _assert_canonical_verdict_section(prompt: str) -> None:
    """All four role names + the disambiguation paragraph must appear."""
    assert "## Canonical verdict allowlists" in prompt, prompt[:500]
    assert "Codex review" in prompt
    assert "Codex plan-review" in prompt
    assert "Claude review" in prompt
    assert "D.5 third opinion" in prompt
    # Role-disambiguation paragraph (load-bearing per the post-mortem).
    assert (
        "Do NOT flag a plan-text reference to another role's verdict as "
        "invalid"
    ) in prompt


def test_verdict_allowlist_in_review_prompt_contains_all_four_roles():
    prompt = _render_review_prompt_for_verdict_tests()
    _assert_canonical_verdict_section(prompt)
    # The allowlist text uses the canonical constants directly — verify a
    # representative verdict from each set appears.
    assert "needs-rework" in prompt
    assert "minor-findings" in prompt
    assert "ship-with-fixes" in prompt
    assert "approved-with-notes" in prompt


def test_verdict_allowlist_in_plan_review_prompt_contains_all_four_roles():
    prompt = _render_plan_review_prompt_for_verdict_tests()
    _assert_canonical_verdict_section(prompt)


def test_verdict_allowlist_in_plan_review_prompt_embeds_task_file_ownership():
    """Plan-review must embed a `Task-level file ownership` subsection
    enumerating each `tasks[].files[]` entry, one bullet per task."""
    prompt = _render_plan_review_prompt_for_verdict_tests(
        tasks=[
            {"task_id": "001", "files": ["a.py", "b.py"]},
            {"task_id": "002", "files": ["c.py"]},
        ],
    )
    assert "## Task-level file ownership" in prompt
    assert "TASK-001" in prompt
    assert "a.py" in prompt and "b.py" in prompt
    assert "TASK-002" in prompt
    assert "c.py" in prompt


def test_verdict_allowlist_review_prompt_dynamic_read_from_canonical_constants(
    monkeypatch,
):
    """LOAD-BEARING dynamic-read assertion: temporarily extend
    `ALLOWED_CODEX_REVIEW_VERDICTS` with a synthetic verdict and re-render
    the prompt. The synthetic verdict MUST appear in the rendered text —
    this pins that the renderer reads the constants at render time rather
    than embedding a snapshot at module-import time. Override is reverted
    automatically by monkeypatch."""
    import plan_ops  # type: ignore

    synthetic = "synthetic-test-verdict"
    extended = set(plan_ops.ALLOWED_CODEX_REVIEW_VERDICTS) | {synthetic}
    monkeypatch.setattr(plan_ops, "ALLOWED_CODEX_REVIEW_VERDICTS", extended)

    prompt = _render_review_prompt_for_verdict_tests()
    assert synthetic in prompt, (
        "renderer must read ALLOWED_CODEX_REVIEW_VERDICTS dynamically"
    )


def test_verdict_allowlist_plan_review_prompt_dynamic_read_from_constants(
    monkeypatch,
):
    """Same dynamic-read guarantee on the plan-review render path, against
    `ALLOWED_PLAN_REVIEW_VERDICTS`."""
    import plan_ops  # type: ignore

    synthetic = "synthetic-plan-review-verdict"
    extended = set(plan_ops.ALLOWED_PLAN_REVIEW_VERDICTS) | {synthetic}
    monkeypatch.setattr(plan_ops, "ALLOWED_PLAN_REVIEW_VERDICTS", extended)

    prompt = _render_plan_review_prompt_for_verdict_tests()
    assert synthetic in prompt, (
        "renderer must read ALLOWED_PLAN_REVIEW_VERDICTS dynamically"
    )


def test_canonical_verdicts_d5_alias_matches_claude_review_set():
    """`ALLOWED_D5_VERDICTS` is the named alias for the D.5 third-opinion
    role and resolves to the same vocabulary as the Claude reviewer."""
    import plan_ops  # type: ignore

    assert plan_ops.ALLOWED_D5_VERDICTS == plan_ops.ALLOWED_CLAUDE_REVIEW_VERDICTS
