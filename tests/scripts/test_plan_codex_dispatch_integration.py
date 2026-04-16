"""Integration test for scripts/plan_codex_dispatch.py.

Dispatches a trivial throwaway task to the real Codex CLI end-to-end.
Validates envelope structure, success outcome, exit code, Codex status,
and that reported files_changed matches the actual git diff.

Marked @pytest.mark.slow: runs only under `pytest -m slow`. Skipped if
the codex binary is not on PATH.

Cost per run: one real Codex CLI call, ~15-30s wall clock.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_codex_dispatch.py"


SCRATCH_PLAN = """# Plan: Scratch integration test

**Created:** 2026-04-13
**Status:** ready
**Base branch:** main

## Goal
Verify that the Codex dispatch wrapper can drive Codex end-to-end
against a trivial throwaway task.

## Context
This is an isolated test repository with no real code. The only task
is to create a single file with fixed content. Follow the instructions
verbatim.

## Verification
Check that SCRATCH.txt exists and contains the expected content.

---

## Tasks

### TASK-001: Create SCRATCH.txt

- **Status:** pending
- **Priority:** low
- **Files:**
  - SCRATCH.txt (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - SCRATCH.txt exists in the repository root
  - SCRATCH.txt contents equal the string "hello" (no trailing newline required)

**Description:**
Create a new file named `SCRATCH.txt` in the repository root containing
the text "hello". Do not create any other files. Do not modify any
other files.

**Implementation notes:**
Use a simple file write. The file does not need a trailing newline.

**Reversion guidance:**
Delete SCRATCH.txt from the repository root.
"""


def _codex_available() -> bool:
    try:
        r = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git"] + args,
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
    # Pre-create .codex to mirror the main repo layout. Codex CLI will
    # create a 0-byte .codex placeholder in a fresh repo if one is absent,
    # which would register as an out-of-scope write. Committing an empty
    # .codex up front matches production usage and avoids that artifact.
    (repo / ".codex").write_text("")
    _git(["add", ".codex"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    return repo


@pytest.mark.slow
def test_implement_creates_file_end_to_end(tmp_path):
    """Full wrapper path: plan parse -> Codex call -> scope check -> envelope."""
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)

    plan_path = repo / "plan.md"
    plan_path.write_text(SCRATCH_PLAN)
    _git(["add", "plan.md"], cwd=repo)
    _git(["commit", "-q", "-m", "add plan"], cwd=repo)

    result = subprocess.run(
        [
            sys.executable,
            str(WRAPPER),
            "implement",
            "--plan-file", str(plan_path),
            "--task-id", "1",
            "--repo-root", str(repo),
            "--timeout", "180",
        ],
        capture_output=True,
        text=True,
        timeout=240,
    )

    # Wrapper must not crash
    assert result.returncode == 0, (
        f"wrapper exit={result.returncode}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )

    # Envelope must be valid JSON on stdout
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"wrapper stdout not valid JSON: {e}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    # Required envelope keys (section 7.4)
    required_keys = {
        "task_id",
        "subcommand",
        "outcome",
        "codex_exit_code",
        "codex_output_raw",
        "parsed",
        "error",
    }
    missing = required_keys - set(envelope.keys())
    assert not missing, (
        f"envelope missing keys: {missing}\n"
        f"envelope: {json.dumps(envelope, indent=2)[:2000]}"
    )

    # Outcome must be success
    assert envelope["outcome"] == "success", (
        f"outcome={envelope['outcome']}\n"
        f"error={envelope.get('error')}\n"
        f"envelope: {json.dumps(envelope, indent=2)[:2000]}"
    )

    # Codex exit code clean
    assert envelope["codex_exit_code"] == 0, (
        f"codex_exit_code={envelope['codex_exit_code']}"
    )

    # Parsed block present and status completed
    parsed = envelope.get("parsed")
    assert parsed is not None, "parsed block missing"
    assert parsed.get("status") == "completed", (
        f"parsed.status={parsed.get('status')!r}\n"
        f"parsed: {json.dumps(parsed, indent=2)[:2000]}"
    )

    # Reported files_changed must match actual git diff
    reported = set(parsed.get("files_changed") or [])

    tracked_r = _git(["diff", "--name-only", "HEAD"], cwd=repo, check=False)
    tracked = {ln.strip() for ln in tracked_r.stdout.splitlines() if ln.strip()}

    untracked_r = _git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=repo, check=False,
    )
    untracked = {ln.strip() for ln in untracked_r.stdout.splitlines() if ln.strip()}

    actual = tracked | untracked

    assert "SCRATCH.txt" in actual, (
        f"SCRATCH.txt not created. actual={actual}\n"
        f"envelope: {json.dumps(envelope, indent=2)[:2000]}"
    )

    assert reported == actual, (
        f"reported files_changed != actual git diff\n"
        f"reported={reported}\n"
        f"actual={actual}"
    )

    # Best-effort cleanup; tmp_path will be removed by pytest anyway
    try:
        shutil.rmtree(repo)
    except OSError:
        pass


SIBLING_PLAN_TEMPLATE = """# Plan: Parallel sibling fixture

**Created:** 2026-04-14
**Status:** ready
**Base branch:** main

## Context
Isolated scratch repo for parallel sibling regression tests.

---

## Tasks

### TASK-{task_id}: Create {file}

- **Status:** pending
- **Priority:** low
- **Files:**
  - {file} (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - {file} exists with contents "sibling-{file}"

**Description:**
Create a new file named `{file}` in the repository root containing the
text "sibling-{file}". Do not create any other files. Do not modify any
other files.

**Implementation notes:**
Single file write. No trailing newline required.

**Reversion guidance:**
Delete `{file}`.
"""


def _launch_wrapper(repo: Path, plan: Path, task_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(WRAPPER),
            "implement",
            "--plan-file", str(plan),
            "--task-id", task_id,
            "--repo-root", str(repo),
            "--timeout", "180",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.slow
def test_parallel_implement_siblings_preserve_each_other(tmp_path):
    """Two real wrapper subprocesses on disjoint tasks in the same repo.

    Each writes its own file; neither may erase the other's work. The
    delta-bounded cleanup must ensure both siblings finish with outcome
    success and both target files exist.
    """
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)

    plan_a = repo / "plan_a.md"
    plan_a.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="001", file="a.py"))
    plan_b = repo / "plan_b.md"
    plan_b.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="002", file="b.py"))
    _git(["add", "plan_a.md", "plan_b.md"], cwd=repo)
    _git(["commit", "-q", "-m", "add sibling plans"], cwd=repo)

    proc_a = _launch_wrapper(repo, plan_a, "001")
    proc_b = _launch_wrapper(repo, plan_b, "002")

    out_a, err_a = proc_a.communicate(timeout=240)
    out_b, err_b = proc_b.communicate(timeout=240)

    assert proc_a.returncode == 0, (
        f"sibling A failed rc={proc_a.returncode}\nSTDOUT:\n{out_a}\nSTDERR:\n{err_a}"
    )
    assert proc_b.returncode == 0, (
        f"sibling B failed rc={proc_b.returncode}\nSTDOUT:\n{out_b}\nSTDERR:\n{err_b}"
    )

    env_a = json.loads(out_a)
    env_b = json.loads(out_b)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert (repo / "a.py").exists(), "sibling A's file was erased"
    assert (repo / "b.py").exists(), "sibling B's file was erased"


@pytest.mark.slow
def test_parallel_implement_with_baseline_untracked_survives(tmp_path):
    """A pre-populated untracked file must survive concurrent sibling dispatches.

    notes.txt is created before dispatch. Both siblings capture baselines
    that include notes.txt as untracked, so neither sibling's cleanup may
    delete it.
    """
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)

    plan_a = repo / "plan_a.md"
    plan_a.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="001", file="a.py"))
    plan_b = repo / "plan_b.md"
    plan_b.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="002", file="b.py"))
    _git(["add", "plan_a.md", "plan_b.md"], cwd=repo)
    _git(["commit", "-q", "-m", "add sibling plans"], cwd=repo)

    notes = repo / "notes.txt"
    notes.write_text("pre-existing sibling state\n")

    proc_a = _launch_wrapper(repo, plan_a, "001")
    proc_b = _launch_wrapper(repo, plan_b, "002")

    out_a, _ = proc_a.communicate(timeout=240)
    out_b, _ = proc_b.communicate(timeout=240)

    env_a = json.loads(out_a)
    env_b = json.loads(out_b)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert notes.exists(), "pre-existing untracked notes.txt was erased"
    assert notes.read_text() == "pre-existing sibling state\n"


def _stage_sibling_plans(repo: Path) -> tuple[Path, Path]:
    plan_a = repo / "plan_a.md"
    plan_a.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="001", file="a.py"))
    plan_b = repo / "plan_b.md"
    plan_b.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="002", file="b.py"))
    _git(["add", "plan_a.md", "plan_b.md"], cwd=repo)
    _git(["commit", "-q", "-m", "add sibling plans"], cwd=repo)
    return plan_a, plan_b


def _run_parallel(repo: Path, plan_a: Path, plan_b: Path) -> tuple[dict, dict]:
    proc_a = _launch_wrapper(repo, plan_a, "001")
    proc_b = _launch_wrapper(repo, plan_b, "002")
    out_a, err_a = proc_a.communicate(timeout=240)
    out_b, err_b = proc_b.communicate(timeout=240)
    env_a = json.loads(out_a) if out_a.strip() else {
        "outcome": "no_stdout", "stderr": err_a,
    }
    env_b = json.loads(out_b) if out_b.strip() else {
        "outcome": "no_stdout", "stderr": err_b,
    }
    return env_a, env_b


@pytest.mark.slow
def test_parallel_preserves_run_log_jsonl(tmp_path):
    """A pre-populated docs/plans/_run_log.jsonl must survive parallel
    dispatches untouched (protected path, observe-only)."""
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    run_log = plans_dir / "_run_log.jsonl"
    original = '{"event":"baseline","ts":"2026-04-14T00:00:00Z"}\n'
    run_log.write_text(original)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert run_log.exists(), "protected _run_log.jsonl was erased"
    assert run_log.read_text() == original


@pytest.mark.slow
def test_parallel_preserves_run_lock_json(tmp_path):
    """A pre-populated docs/plans/_run_lock.json must survive untouched."""
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    run_lock = plans_dir / "_run_lock.json"
    original = '{"run_id":"RUN-BASELINE","owner":"test","acquired_at":"2026-04-14T00:00:00Z"}\n'
    run_lock.write_text(original)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert run_lock.exists(), "protected _run_lock.json was erased"
    assert run_lock.read_text() == original


@pytest.mark.slow
def test_parallel_preserves_schedule_json_tracked(tmp_path):
    """A tracked docs/plans/*.schedule.json must survive untouched."""
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    # plans_dir already exists from _stage_sibling_plans commits into repo root,
    # not docs/plans — create it now.
    if not plans_dir.exists():
        plans_dir.mkdir(parents=True)
    sched = plans_dir / "mixed_batch.schedule.json"
    original = '{"batches":[{"batch_id":"B-1","task_ids":["001","002"]}]}\n'
    sched.write_text(original)
    _git(["add", "docs/plans/mixed_batch.schedule.json"], cwd=repo)
    _git(["commit", "-q", "-m", "add schedule"], cwd=repo)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert sched.exists(), "protected schedule.json was erased"
    assert sched.read_text() == original


@pytest.mark.slow
def test_parallel_preserves_schedule_json_untracked(tmp_path):
    """An untracked docs/plans/*.schedule.json must survive parallel dispatches."""
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    if not plans_dir.exists():
        plans_dir.mkdir(parents=True)
    sched = plans_dir / "fresh_batch.schedule.json"
    original = '{"batches":[]}\n'
    sched.write_text(original)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert sched.exists(), "untracked schedule.json was erased"
    assert sched.read_text() == original


@pytest.mark.slow
def test_timeout_and_success_interleaved_preserves_sibling(tmp_path):
    """A sibling that times out must not erase the other sibling's completed
    work. Sibling A uses a 1-second timeout (guaranteed to fire before Codex
    can finish a real task) while sibling B uses a normal 180s timeout. After
    the timeout path cleans up A's partial state, B's declared file must still
    exist."""
    if not _codex_available():
        pytest.skip("codex CLI not available")

    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    # A: aggressive timeout — guaranteed to time out
    proc_a = subprocess.Popen(
        [
            sys.executable, str(WRAPPER),
            "implement",
            "--plan-file", str(plan_a),
            "--task-id", "001",
            "--repo-root", str(repo),
            "--timeout", "1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # B: normal timeout — should succeed
    proc_b = _launch_wrapper(repo, plan_b, "002")

    out_a, err_a = proc_a.communicate(timeout=60)
    out_b, err_b = proc_b.communicate(timeout=240)

    env_a = json.loads(out_a) if out_a.strip() else {
        "outcome": "no_stdout", "stderr": err_a,
    }
    env_b = json.loads(out_b) if out_b.strip() else {
        "outcome": "no_stdout", "stderr": err_b,
    }

    # A timed out (expected); B succeeded regardless
    assert env_a["outcome"] in {"timeout", "failure"}, env_a
    assert env_b["outcome"] == "success", env_b
    # B's declared file must still exist — A's cleanup must not have erased it
    assert (repo / "b.py").exists(), (
        "sibling B's file was erased by sibling A's timeout cleanup"
    )
