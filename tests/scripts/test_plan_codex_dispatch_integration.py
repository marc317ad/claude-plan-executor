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
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_codex_dispatch.py"

CODEX_SESSION_INIT_FAILURE_NEEDLES = (
    "Failed to create session",
    "Failed to initialize session",
    "error creating thread",
    "Read-only file system",
    "os error 30",
    "failed to refresh available models",
    "error sending request for url",
    "failed to connect to websocket",
    "dns error",
    "Operation not permitted",
)


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


def _codex_available(env: dict[str, str] | None = None) -> bool:
    try:
        r = subprocess.run(
            ["codex", "--version"],
            env=env,
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _make_codex_env(tmp_path: Path) -> dict[str, str]:
    """Return an environment with writable Codex/session state under tmp_path."""
    env = os.environ.copy()

    codex_state = tmp_path / "codex_state"
    home = codex_state / "home"
    codex_home = codex_state / "codex_home"
    xdg_config = codex_state / "xdg_config"
    xdg_cache = codex_state / "xdg_cache"
    xdg_data = codex_state / "xdg_data"
    tmp = codex_state / "tmp"
    for path in (home, codex_home, xdg_config, xdg_cache, xdg_data, tmp):
        path.mkdir(parents=True, exist_ok=True)

    # Keep live authentication/config readable when present, but force logs,
    # sessions, caches, and PATH-update probes into per-test writable storage.
    source_codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    for name in ("auth.json", "config.toml", "installation_id"):
        source = source_codex_home / name
        if source.is_file():
            shutil.copy2(source, codex_home / name)

    env.update({
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_CACHE_HOME": str(xdg_cache),
        "XDG_DATA_HOME": str(xdg_data),
        "TMPDIR": str(tmp),
    })
    return env


@pytest.fixture(scope="session")
def live_codex_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    tmp_path = tmp_path_factory.mktemp("live_codex")
    env = _make_codex_env(tmp_path)
    if not _codex_available(env):
        pytest.skip("codex CLI not available")
    if env.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        pytest.skip(
            "live Codex CLI cannot run in this environment: network is disabled"
        )
    return env


def _skip_if_live_codex_environment_failure(*texts: str) -> None:
    combined = "\n".join(text for text in texts if text)
    if any(needle in combined for needle in CODEX_SESSION_INIT_FAILURE_NEEDLES):
        pytest.skip(
            "live Codex CLI cannot initialize or reach its service in this environment"
        )


def _load_envelope_or_skip(stdout: str, stderr: str) -> dict:
    _skip_if_live_codex_environment_failure(stdout, stderr)
    envelope = json.loads(stdout)
    _skip_if_live_codex_environment_failure(
        envelope.get("error") or "",
        envelope.get("codex_output_raw") or "",
        stderr,
    )
    return envelope


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
def test_implement_creates_file_end_to_end(tmp_path, live_codex_env):
    """Full wrapper path: plan parse -> Codex call -> scope check -> envelope."""
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
        env=live_codex_env,
        capture_output=True,
        text=True,
        timeout=240,
    )

    # Envelope must be valid JSON on stdout
    try:
        envelope = _load_envelope_or_skip(result.stdout, result.stderr)
    except json.JSONDecodeError as e:
        pytest.fail(
            f"wrapper stdout not valid JSON: {e}\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )

    # Wrapper must not crash
    assert result.returncode == 0, (
        f"wrapper exit={result.returncode}\n"
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


def _launch_wrapper(
    repo: Path,
    plan: Path,
    task_id: str,
    env: dict[str, str],
) -> subprocess.Popen:
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
        env=env,
    )


@pytest.mark.slow
def test_parallel_implement_siblings_preserve_each_other(tmp_path, live_codex_env):
    """Two real wrapper subprocesses on disjoint tasks in the same repo.

    Each writes its own file; neither may erase the other's work. The
    delta-bounded cleanup must ensure both siblings finish with outcome
    success and both target files exist.
    """
    repo = _make_repo(tmp_path)

    plan_a = repo / "plan_a.md"
    plan_a.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="001", file="a.py"))
    plan_b = repo / "plan_b.md"
    plan_b.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="002", file="b.py"))
    _git(["add", "plan_a.md", "plan_b.md"], cwd=repo)
    _git(["commit", "-q", "-m", "add sibling plans"], cwd=repo)

    proc_a = _launch_wrapper(repo, plan_a, "001", live_codex_env)
    proc_b = _launch_wrapper(repo, plan_b, "002", live_codex_env)

    out_a, err_a = proc_a.communicate(timeout=240)
    out_b, err_b = proc_b.communicate(timeout=240)

    env_a = _load_envelope_or_skip(out_a, err_a)
    env_b = _load_envelope_or_skip(out_b, err_b)
    assert proc_a.returncode == 0, (
        f"sibling A failed rc={proc_a.returncode}\nSTDOUT:\n{out_a}\nSTDERR:\n{err_a}"
    )
    assert proc_b.returncode == 0, (
        f"sibling B failed rc={proc_b.returncode}\nSTDOUT:\n{out_b}\nSTDERR:\n{err_b}"
    )
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert (repo / "a.py").exists(), "sibling A's file was erased"
    assert (repo / "b.py").exists(), "sibling B's file was erased"


@pytest.mark.slow
def test_parallel_implement_with_baseline_untracked_survives(
    tmp_path,
    live_codex_env,
):
    """A pre-populated untracked file must survive concurrent sibling dispatches.

    notes.txt is created before dispatch. Both siblings capture baselines
    that include notes.txt as untracked, so neither sibling's cleanup may
    delete it.
    """
    repo = _make_repo(tmp_path)

    plan_a = repo / "plan_a.md"
    plan_a.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="001", file="a.py"))
    plan_b = repo / "plan_b.md"
    plan_b.write_text(SIBLING_PLAN_TEMPLATE.format(task_id="002", file="b.py"))
    _git(["add", "plan_a.md", "plan_b.md"], cwd=repo)
    _git(["commit", "-q", "-m", "add sibling plans"], cwd=repo)

    notes = repo / "notes.txt"
    notes.write_text("pre-existing sibling state\n")

    proc_a = _launch_wrapper(repo, plan_a, "001", live_codex_env)
    proc_b = _launch_wrapper(repo, plan_b, "002", live_codex_env)

    out_a, err_a = proc_a.communicate(timeout=240)
    out_b, err_b = proc_b.communicate(timeout=240)

    env_a = _load_envelope_or_skip(out_a, err_a)
    env_b = _load_envelope_or_skip(out_b, err_b)
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


def _run_parallel(
    repo: Path,
    plan_a: Path,
    plan_b: Path,
    env: dict[str, str],
) -> tuple[dict, dict]:
    proc_a = _launch_wrapper(repo, plan_a, "001", env)
    proc_b = _launch_wrapper(repo, plan_b, "002", env)
    out_a, err_a = proc_a.communicate(timeout=240)
    out_b, err_b = proc_b.communicate(timeout=240)
    env_a = _load_envelope_or_skip(out_a, err_a) if out_a.strip() else {
        "outcome": "no_stdout", "stderr": err_a,
    }
    env_b = _load_envelope_or_skip(out_b, err_b) if out_b.strip() else {
        "outcome": "no_stdout", "stderr": err_b,
    }
    return env_a, env_b


@pytest.mark.slow
def test_parallel_preserves_run_log_jsonl(tmp_path, live_codex_env):
    """A pre-populated docs/plans/_run_log.jsonl must survive parallel
    dispatches untouched (protected path, observe-only)."""
    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    run_log = plans_dir / "_run_log.jsonl"
    original = '{"event":"baseline","ts":"2026-04-14T00:00:00Z"}\n'
    run_log.write_text(original)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b, live_codex_env)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert run_log.exists(), "protected _run_log.jsonl was erased"
    assert run_log.read_text() == original


@pytest.mark.slow
def test_parallel_preserves_run_lock_json(tmp_path, live_codex_env):
    """A pre-populated docs/plans/_run_lock.json must survive untouched."""
    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    run_lock = plans_dir / "_run_lock.json"
    original = '{"run_id":"RUN-BASELINE","owner":"test","acquired_at":"2026-04-14T00:00:00Z"}\n'
    run_lock.write_text(original)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b, live_codex_env)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert run_lock.exists(), "protected _run_lock.json was erased"
    assert run_lock.read_text() == original


@pytest.mark.slow
def test_parallel_preserves_schedule_json_tracked(tmp_path, live_codex_env):
    """A tracked docs/plans/*.schedule.json must survive untouched."""
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

    env_a, env_b = _run_parallel(repo, plan_a, plan_b, live_codex_env)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert sched.exists(), "protected schedule.json was erased"
    assert sched.read_text() == original


@pytest.mark.slow
def test_parallel_preserves_schedule_json_untracked(tmp_path, live_codex_env):
    """An untracked docs/plans/*.schedule.json must survive parallel dispatches."""
    repo = _make_repo(tmp_path)
    plan_a, plan_b = _stage_sibling_plans(repo)

    plans_dir = repo / "docs" / "plans"
    if not plans_dir.exists():
        plans_dir.mkdir(parents=True)
    sched = plans_dir / "fresh_batch.schedule.json"
    original = '{"batches":[]}\n'
    sched.write_text(original)

    env_a, env_b = _run_parallel(repo, plan_a, plan_b, live_codex_env)
    assert env_a["outcome"] == "success", env_a
    assert env_b["outcome"] == "success", env_b
    assert sched.exists(), "untracked schedule.json was erased"
    assert sched.read_text() == original


@pytest.mark.slow
def test_timeout_and_success_interleaved_preserves_sibling(tmp_path, live_codex_env):
    """A sibling that times out must not erase the other sibling's completed
    work. Sibling A uses a 1-second timeout (guaranteed to fire before Codex
    can finish a real task) while sibling B uses a normal 180s timeout. After
    the timeout path cleans up A's partial state, B's declared file must still
    exist."""
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
        env=live_codex_env,
    )
    # B: normal timeout — should succeed
    proc_b = _launch_wrapper(repo, plan_b, "002", live_codex_env)

    out_a, err_a = proc_a.communicate(timeout=60)
    out_b, err_b = proc_b.communicate(timeout=240)

    env_a = _load_envelope_or_skip(out_a, err_a) if out_a.strip() else {
        "outcome": "no_stdout", "stderr": err_a,
    }
    env_b = _load_envelope_or_skip(out_b, err_b) if out_b.strip() else {
        "outcome": "no_stdout", "stderr": err_b,
    }

    # A timed out (expected); B succeeded regardless
    assert env_a["outcome"] in {"timeout", "failure"}, env_a
    assert env_b["outcome"] == "success", env_b
    # B's declared file must still exist — A's cleanup must not have erased it
    assert (repo / "b.py").exists(), (
        "sibling B's file was erased by sibling A's timeout cleanup"
    )


# ---------------------------------------------------------------------------
# TASK-003 — --allow-gaps severity-aware demotion in plan-review prompt.
#
# These tests run the wrapper under --dry-run so they do not require live
# Codex. They validate the rendered prompt contract (demotion clause text,
# presence/absence conditions, and byte-level invariance when the flag
# is not passed) across three branches:
#   - soft-only gaps + --allow-gaps        → demotion clause present
#   - any hard gap + --allow-gaps          → demotion clause absent
#   - no --allow-gaps (control case)       → byte-identical to current output
# ---------------------------------------------------------------------------


_DEMOTION_CLAUSE_NEEDLE = "Operator override (--allow-gaps)"


def _write_plan_review_inputs(
    tmp_path: Path,
    gaps: list[dict],
    outcome: str = "needs-enrichment",
) -> tuple[Path, Path]:
    plan = tmp_path / "sample.md"
    plan.write_text(
        "# plan\n\n## Context\n\nprose\n",
        encoding="utf-8",
    )
    schedule = tmp_path / "sample.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": outcome,
            "tasks": [],
            "batches": [],
            "gaps": gaps,
        }),
        encoding="utf-8",
    )
    return plan, schedule


def _run_plan_review_dry_run(
    plan: Path,
    schedule: Path,
    tmp_path: Path,
    extra_args: list[str] | None = None,
) -> dict:
    # TASK-008: --plan-file / --plans-dir removed. schedule-only argv.
    # ``plan`` remains a parameter for backward compatibility with existing
    # callers but is no longer forwarded to the wrapper.
    del plan  # unused after TASK-008 shim removal
    cmd = [
        sys.executable, str(WRAPPER), "plan-review",
        "--schedule-file", str(schedule),
        "--repo-root", str(tmp_path),
        "--dry-run",
        "--timeout", "180",
    ]
    if extra_args:
        cmd.extend(extra_args)
    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert cp.returncode == 0, (
        f"wrapper dry-run failed rc={cp.returncode}\n"
        f"STDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"
    )
    return json.loads(cp.stdout)


def test_plan_review_allow_gaps_demotes_soft_gap_verdict(tmp_path):
    """When --allow-gaps is set and gaps are soft-only, the rendered
    prompt carries the demotion clause instructing the reviewer to treat
    schedule_ok=false as approved-with-notes rather than needs-replan."""
    soft_gaps = [
        {"type": "unresolvable-test", "detail": "foo", "severity": "soft"},
        {"type": "empty-implementation-notes", "detail": "bar", "severity": "soft"},
    ]
    plan, schedule = _write_plan_review_inputs(tmp_path, soft_gaps)

    body = _run_plan_review_dry_run(
        plan, schedule, tmp_path, extra_args=["--allow-gaps"],
    )
    prompt = body["prompt_preview"]

    assert _DEMOTION_CLAUSE_NEEDLE in prompt, (
        "demotion clause missing from prompt when --allow-gaps + soft gaps"
    )
    assert "approved-with-notes" in prompt
    assert "mention that demotion in the `summary`" in prompt


def test_plan_review_allow_gaps_hard_gaps_block(tmp_path):
    """--allow-gaps with any hard-severity gap must NOT inject the demotion
    clause — the reviewer proceeds with standard verdict selection so
    needs-replan still routes through plan-author auto-revise."""
    mixed_gaps = [
        {"type": "unresolvable-test", "detail": "foo", "severity": "soft"},
        {"type": "stale-path", "detail": "baz", "severity": "hard"},
    ]
    plan, schedule = _write_plan_review_inputs(tmp_path, mixed_gaps)

    body = _run_plan_review_dry_run(
        plan, schedule, tmp_path, extra_args=["--allow-gaps"],
    )
    prompt = body["prompt_preview"]

    assert _DEMOTION_CLAUSE_NEEDLE not in prompt, (
        "demotion clause must be suppressed when any gap is hard-severity"
    )
    # Verdict vocab remains intact.
    assert "needs-replan" in prompt


def test_plan_review_allow_gaps_absent_prompt_unchanged(tmp_path):
    """Without --allow-gaps, the rendered prompt carries no demotion clause
    and the core schedule-only scaffold is intact (TASK-006 prompt body).

    Updated for TASK-006: the prompt is now schedule-only, so the frozen
    invariants pin the schedule-only scaffold rather than the retired
    `Plan document (verbatim):` block. `plan_file` is derived from the
    schedule sidecar stem (see `cmd_plan_review`) — the legacy
    `plan.name` basename no longer round-trips since the wrapper does
    not read the plan markdown.
    """
    soft_gaps = [
        {"type": "unresolvable-test", "detail": "foo", "severity": "soft"},
    ]
    plan, schedule = _write_plan_review_inputs(tmp_path, soft_gaps)

    # First run: no --allow-gaps, soft gaps present → control case.
    body_no_flag = _run_plan_review_dry_run(plan, schedule, tmp_path)
    prompt_no_flag = body_no_flag["prompt_preview"]
    assert _DEMOTION_CLAUSE_NEEDLE not in prompt_no_flag

    # TASK-006 envelope-shape invariant: the dry-run envelope stays with
    # its pre-TASK-006 shape — plan_file, subcommand, outcome, dry_run,
    # prompt_preview. No new top-level `schedule_file` field. TASK-008
    # may revisit this.
    assert "schedule_file" not in body_no_flag, (
        f"dry-run envelope must not carry schedule_file; got keys: "
        f"{list(body_no_flag.keys())}"
    )
    # Positive contract: required pre-TASK-006 keys remain present.
    for required in ("plan_file", "subcommand", "outcome", "dry_run",
                     "prompt_preview"):
        assert required in body_no_flag, (
            f"dry-run envelope missing {required!r}; got keys: "
            f"{list(body_no_flag.keys())}"
        )

    # Derive plan_basename the way `cmd_plan_review` does in TASK-006:
    # sidecar stem (schedule.name without trailing `.schedule.json`).
    expected_plan_basename = schedule.name[: -len(".schedule.json")]

    frozen_invariants = [
        # Plan basename round-trips from schedule stem.
        f"Plan file: {expected_plan_basename}",
        f'`plan_file` must be "{expected_plan_basename}".',
        # Full verdict vocabulary present verbatim (schedule-only phrasing).
        "Verdict vocabulary (pick exactly one):",
        "`approved` — the schedule is workable as written and no "
        "substantiated blocking issue is present.",
        "`approved-with-notes` — the schedule is workable but has "
        "non-blocking issues, minor gaps, or operator-accepted soft gaps.",
        "`needs-replan` — the schedule has a concrete blocking defect "
        "that should be fixed before dispatch.",
        # Core prompt scaffold — schedule-only after TASK-006.
        "Review the persisted schedule for this plan.",
        "Cross-plan dependency resolution has already been verified",
        "Persisted schedule JSON:",
        # New tasks[i]-path guidance landed in TASK-006.
        "tasks[i]",
        "Return schema-compliant JSON only, no markdown fences",
    ]
    for needle in frozen_invariants:
        assert needle in prompt_no_flag, (
            f"frozen no-flag invariant missing from prompt: {needle!r}"
        )
    # The retired plan-markdown block must not appear in the schedule-only
    # prompt.
    assert "Plan document (verbatim):" not in prompt_no_flag, (
        "TASK-006 schedule-only prompt must not carry the plan-markdown block"
    )
    # Demotion clause opener must be absent in the no-flag path.
    assert "Operator override (--allow-gaps)" not in prompt_no_flag, (
        "demotion clause leaked into prompt when --allow-gaps is not set"
    )


# ---------------------------------------------------------------------------
# TASK-006 — cmd_plan_review is schedule-only.
#
# Dry-run only; no live Codex. TASK-001 originally validated that the
# wrapper read a plan directory's 00_INDEX.json and concatenated each
# chunk's markdown into the prompt. TASK-006 removes that path entirely:
# the reviewer reads only the persisted schedule JSON (fat manifest).
# These tests were updated in place to pin the schedule-only contract
# instead of the retired concat path.
# ---------------------------------------------------------------------------


DIRECTORY_MODE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "directory_mode_plan"
)


def _write_dummy_schedule(tmp_path: Path, stem: str = "directory_mode_plan") -> Path:
    """Minimal schedule JSON that round-trips through the wrapper's parse.

    Sidecar naming follows the in-directory convention per `_plan_paths.py:69`:
    ``<plan_dir>/<plan_dir-stem>.schedule.json``. The wrapper derives
    ``plan_basename`` from the sidecar's parent directory (when it matches
    the stem) or the sidecar stem itself.
    """
    schedule = tmp_path / f"{stem}.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "gaps": [],
        }),
        encoding="utf-8",
    )
    return schedule


def test_plan_review_directory_sidecar_plan_file_derived_from_directory(tmp_path):
    """When the schedule sidecar lives inside the plan directory
    (``<plan_dir>/<plan_dir-stem>.schedule.json``), the wrapper derives
    ``envelope.plan_file`` from the directory's own basename — matching
    the identity used by every other downstream consumer.

    Replaces the retired TASK-001 directory-concat test: the wrapper no
    longer reads chunks[].file, so there is nothing to concatenate.
    """
    # Drop the sidecar inside the fixture-equivalent copy so the wrapper's
    # parent-name derivation fires.
    plan_dir = tmp_path / DIRECTORY_MODE_FIXTURE.name
    plan_dir.mkdir()
    schedule = _write_dummy_schedule(plan_dir, stem=plan_dir.name)

    cp = subprocess.run(
        [
            sys.executable, str(WRAPPER), "plan-review",
            "--schedule-file", str(schedule),
            "--repo-root", str(tmp_path),
            "--dry-run",
            "--timeout", "180",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert cp.returncode == 0, (
        f"dry-run exited non-zero rc={cp.returncode}\n"
        f"STDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"
    )
    body = json.loads(cp.stdout)
    assert body["outcome"] == "dry_run", body
    # plan_file identifies the plan directory, derived from the sidecar
    # parent (NOT from --plan-file, which the wrapper no longer accepts).
    assert body["plan_file"] == plan_dir.name, body["plan_file"]

    prompt = body["prompt_preview"]
    # The prompt's Plan file header echoes the directory basename.
    assert f"Plan file: {plan_dir.name}" in prompt
    # Schedule-only prompt: no plan-markdown block, no child-file
    # concat markers.
    assert "Plan document (verbatim):" not in prompt
    for child_name in (
        "TASK-001_seed.md",
        "TASK-002_write_a.md",
        "TASK-003_write_b.md",
    ):
        assert f"<!-- {child_name} -->" not in prompt, (
            "TASK-006 schedule-only prompt must not concat child markdown"
        )


def test_plan_review_argparse_rejects_retired_plan_file_flag(tmp_path):
    """TASK-008: `--plan-file` is removed from the plan-review argparse
    surface entirely. Passing it is now a hard failure (argparse rejects
    the unknown argument), making accidental usage by stale callers a
    loud error rather than a silent no-op. Replaces the former
    one-version deprecation-shim test."""
    schedule = _write_dummy_schedule(tmp_path, stem="sample_plan")
    bogus_plan_file = tmp_path / "missing_directory"  # never created

    cp = subprocess.run(
        [
            sys.executable, str(WRAPPER), "plan-review",
            "--plan-file", str(bogus_plan_file),  # retired flag
            "--schedule-file", str(schedule),
            "--repo-root", str(tmp_path),
            "--dry-run",
            "--timeout", "180",
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    # argparse exits 2 on unrecognized arguments.
    assert cp.returncode != 0, (
        f"expected non-zero exit for retired --plan-file, got rc={cp.returncode}\n"
        f"STDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"
    )
    assert "--plan-file" in cp.stderr or "unrecognized" in cp.stderr.lower(), (
        cp.stderr
    )
