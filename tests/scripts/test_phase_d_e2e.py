"""End-to-end smoke for the Phase D state machine (TASK-008 PHASE_D_STATE_MACHINE).

Drives the full A→E loop against a one-task fixture plan with subagent
outputs injected via stub -- no real Codex or Claude spawn. Exercises:

    plan_ops.py preflight
        → parse-schedule
        → write-schedule
        → batch-next
        → review-route
        → commit-task | fail-task

The matrix below covers nine routing cells (clean, minor-findings, D.5
ship, D.5 partial-agreement → narrow remediation success/fail, D.2a.5
bounded-remediation success/fail, sanitizer-flag pass-through). The
matrix routes through ``plan_ops.route()``, with one smoke cell using the
local MCP dispatch harness for ``plan_ops__review_route``, then exercises
the ``commit-task`` / ``fail-task`` subprocess seams against a real tmp git
repo, with run-log events emitted via ``log-event`` / ``_append_run_log``.

The test owns the run-log path via monkeypatch (`plan_ops.RUN_LOG_PATH`),
so cross-scenario isolation is enforced and no production run log is
touched. The harness must complete in < 30s — preflight runs once per
scenario; the schedule/batch-next subprocess chain runs once at the
top of each scenario; review-route + commit-task/fail-task per cell.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"
SERVER_PATH = SCRIPTS_DIR / "plan_ops_mcp_server.py"
FIXTURE_PLAN_MD = Path(__file__).parent / "fixtures" / "phase_d_plan.md"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)
_mcp_available = importlib.util.find_spec("mcp") is not None

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402

_SERVER_MODULE: Any | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(*args: str, cwd: Path, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PY), str(SCRIPT), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        input=input_text,
    )


def _parse_json(cp: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"stdout is not JSON:\nstdout={cp.stdout!r}\n"
            f"stderr={cp.stderr!r}\nerr={e}"
        )


def _load_mcp_server_module() -> Any:
    global _SERVER_MODULE
    if _SERVER_MODULE is not None:
        return _SERVER_MODULE
    spec = importlib.util.spec_from_file_location(
        "plan_ops_mcp_server_phase_d_e2e", SERVER_PATH,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _SERVER_MODULE = module
    return module


def _public_mcp_result(result: Any) -> dict:
    if hasattr(result, "structuredContent") and result.structuredContent is not None:
        return dict(result.structuredContent)
    if isinstance(result, dict):
        return dict(result)
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    raise AssertionError(f"cannot extract MCP result from {result!r}")


def _review_route_via_mcp(payload: dict) -> dict:
    if not _mcp_available:
        pytest.skip("mcp SDK not installed")
    server = _load_mcp_server_module()
    result = asyncio.run(
        server._dispatch_registered_tool(
            "plan_ops__review_route",
            {"payload": payload},
        )
    )
    return _public_mcp_result(result)


def _index_roster() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "chunks": [
                {
                    "task_id": "001",
                    "file": "TASK-001_smoke.md",
                    "depends_on": [],
                    "status": "Pending",
                    "superseded_by": [],
                }
            ],
        },
        indent=2,
    ) + "\n"


def _bare_schedule(plan_dirname: str) -> dict:
    """One-task valid schedule shape for write-schedule + batch-next."""
    return {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "title": "Smoke single task",
                "files": ["src/foo.py"],
                "dependencies": [],
                "test_command": "none",
                "priority": "high",
                "plan_file": "TASK-001_smoke.md",
                "description": "smoke",
                "acceptance_criteria": ["foo.py committed"],
                "agent": "claude",
            }
        ],
        "batches": [
            {"index": 1, "task_ids": ["001"], "file_locks": ["src/foo.py"]}
        ],
        "gaps": [],
        "risks": [],
    }


def _retries(**overrides: bool) -> dict:
    base = {
        "bounded_remediation": False,
        "narrow_remediation": False,
        "role_swap": False,
        "codex_fallback": False,
    }
    base.update(overrides)
    return base


def _flags(**overrides: bool) -> dict:
    base = {"codex_review_binding": False, "skip_cross_review": False}
    base.update(overrides)
    return base


def _route_input(
    *,
    verdict: str,
    reviewer: str = "codex",
    claude_only: bool = False,
    findings: list | None = None,
    d5: dict | None = None,
    retries: dict | None = None,
    flags: dict | None = None,
    unattended_revert_policy: str = "pause",
) -> dict:
    return {
        "task_id": "001",
        "implementer": "claude",
        "reviewer": reviewer,
        "claude_only": claude_only,
        "unattended_revert_policy": unattended_revert_policy,
        "reviewer_envelope": {
            "verdict": verdict,
            "findings": findings if findings is not None else [],
            "summary": "",
        },
        "d5_envelope": d5,
        "retries_used": retries if retries is not None else _retries(),
        "flags": flags if flags is not None else _flags(),
    }


def _review_route_reason(payload: dict, directive: dict) -> str:
    reviewer = payload.get("reviewer")
    if reviewer is None:
        reviewer = "codex" if payload.get("implementer") == "claude" else "claude"
    envelope = payload.get("reviewer_envelope") or {}
    verdict = envelope.get("verdict", "unknown")
    d5 = payload.get("d5_envelope") or {}
    d5_verdict = d5.get("verdict")
    reason = (
        f"{payload.get('implementer')}->{reviewer} {verdict}"
        f"{' d5=' + d5_verdict if d5_verdict else ''}"
        f" => {directive.get('action')}"
    )
    return reason[:160]


def _stub_implementer_report() -> str:
    """Pre-baked plan-implementer markdown report.

    Subagent dispatch is stubbed: the test feeds this verbatim into
    ``parse-implementer-report`` instead of spawning a real Claude / Codex.
    Outcome is `success` so ``parse-implementer-report`` returns a clean
    pass-through; the smoke does not exercise the parse-error branches.
    """
    return (
        "## TASK-001 implementation report\n\n"
        "**Outcome:** success\n\n"
        "**Files changed:**\n"
        "- src/foo.py (+1 -1 lines)\n\n"
        "**Diff summary:**\n"
        "- bumped x to 2\n\n"
        "**Test command:** none\n"
        "**Test outcome:** not-run\n\n"
        "**Acceptance criteria check:**\n"
        "- [x] foo.py committed — value bumped\n\n"
        "**Coupling check:** not applicable — single-line edit\n\n"
        "**Plan adaptations:** None\n\n"
        "**Concerns for reviewer:** None\n"
    )


# ---------------------------------------------------------------------------
# Fixture: per-scenario tmp git repo + decomposed plan + isolated run log
# ---------------------------------------------------------------------------


@pytest.fixture()
def smoke_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Build a fresh tmp git repo with the one-task decomposed plan.

    Returns a dict with keys: repo_root, plan_dir, plan_child, schedule_file,
    run_log, src_foo. Resets `plan_ops.PLAN_DIR` / `RUN_LOG_PATH` to point
    inside the tmp tree so direct-call helpers (``route``, ``_append_run_log``)
    sandbox cleanly. Subprocess invocations of ``plan_ops.py`` pick up the
    same isolation via ``cwd=tmp_path`` (PLAN_DIR resolves relative to cwd at
    import time inside the child process).
    """
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)

    # Source file the task edits.
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src_foo = src_dir / "foo.py"
    src_foo.write_text("x = 1\n", encoding="utf-8")

    # Decomposed plan: docs/plans/phase_d_plan/{00_INDEX.json,TASK-001_smoke.md}
    plans_dir = tmp_path / "docs" / "plans"
    plan_dir = plans_dir / "phase_d_plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "00_INDEX.json").write_text(_index_roster(), encoding="utf-8")
    plan_child = plan_dir / "TASK-001_smoke.md"
    shutil.copy(FIXTURE_PLAN_MD, plan_child)

    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

    # Sandbox plan_ops module-level paths to the tmp tree so direct helper
    # calls (``_append_run_log``, ``read_schedule_state``) write under tmp.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_dir)
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans_dir / "_run_log.jsonl")
    monkeypatch.setattr(plan_ops, "RUN_LOCK_PATH", plans_dir / "_run_lock.json")

    return {
        "repo_root": tmp_path,
        "plan_dir": plan_dir,
        "plan_child": plan_child,
        "schedule_file": plans_dir / "phase_d_plan.schedule.json",
        "run_log": plans_dir / "_run_log.jsonl",
        "src_foo": src_foo,
    }


# ---------------------------------------------------------------------------
# Chain test: preflight → parse-schedule → write-schedule → batch-next.
#
# Runs once to validate the chain composes. Per-scenario tests reuse the
# same fixture but skip the chain (it's deterministic; one pass is enough
# for the smoke contract).
# ---------------------------------------------------------------------------


def test_chain_preflight_parse_write_batch_next(smoke_repo: dict) -> None:
    """Drive the full subcommand chain via subprocess against the fixture."""
    repo = smoke_repo["repo_root"]
    plan_dir = smoke_repo["plan_dir"]
    schedule_file = smoke_repo["schedule_file"]

    # 1. preflight — directory-mode + non-TTY → must pass --unattended-revert-policy.
    cp = _run(
        "preflight",
        "--plan-file", str(plan_dir),
        "--unattended-revert-policy", "pause",
        "--json",
        cwd=repo,
    )
    assert cp.returncode == 0, f"preflight failed: {cp.stderr}\n{cp.stdout}"
    pre = _parse_json(cp)
    assert pre["pass"] is True
    assert pre["unattended_revert_policy"] == "pause"

    # 2. parse-schedule — feed the bare schedule on stdin.
    sched_text = json.dumps(_bare_schedule(plan_dir.name))
    cp = _run("parse-schedule", "--stdin", "--json", cwd=repo, input_text=sched_text)
    assert cp.returncode == 0, f"parse-schedule failed: {cp.stderr}\n{cp.stdout}"
    parsed = _parse_json(cp)
    assert parsed.get("errors") in ([], None)
    assert any(t["id"] == "001" for t in parsed["tasks"])

    # 3. write-schedule — persist canonical JSON.
    cp = _run(
        "write-schedule",
        "--schedule-file", str(schedule_file),
        "--stdin", "--json",
        cwd=repo,
        input_text=sched_text,
    )
    assert cp.returncode == 0, f"write-schedule failed: {cp.stderr}\n{cp.stdout}"
    assert schedule_file.is_file()

    # 4. batch-next — picks TASK-001 against an empty done/failed set.
    cp = _run(
        "batch-next",
        "--schedule-file", str(schedule_file),
        "--json",
        cwd=repo,
    )
    assert cp.returncode == 0, f"batch-next failed: {cp.stderr}\n{cp.stdout}"
    batch = _parse_json(cp)
    picked_ids = batch.get("picked") or batch.get("ready_in_batch") or batch.get("task_ids") or []
    if isinstance(picked_ids, list) and picked_ids and isinstance(picked_ids[0], dict):
        picked_ids = [t.get("id") for t in picked_ids]
    assert "001" in picked_ids, f"batch-next did not pick 001: {batch!r}"


# ---------------------------------------------------------------------------
# review-route + commit-task | fail-task matrix (9 scenarios).
#
# Each scenario:
#   1. Stages + commit-prep src/foo.py (per-scenario different bytes).
#   2. Calls the route contract with a stubbed reviewer envelope.
#   3. Asserts the routed action matches expectations.
#   4. Drives the routed terminal — commit-task (via subprocess) or
#      fail-task / awaiting-user log-event — and asserts the run log.
#
# The harness uses subprocess for commit-task / fail-task so the real git
# seams (status flip, atomic 00_INDEX.json roster write, run-log append)
# are exercised end-to-end. One cell routes through the local MCP server
# dispatcher so the orchestrator-facing tool contract is covered here too.
# ---------------------------------------------------------------------------


def _emit_basic_events(run_log: Path, *, run_id: str, task_id: str = "001",
                       sanitizer_flags: list | None = None) -> None:
    """Emit run_start -> batch_start -> implement_done -> review_done.

    The route decision itself is emitted by `_emit_review_route_called` via
    the public `log-event` path after `plan_ops.route()` returns.
    """
    plan_ops._append_run_log("run_start", {"run_id": run_id})
    plan_ops._append_run_log("batch_start", {"run_id": run_id, "batch_index": 1})
    plan_ops._append_run_log(
        "implement_done",
        {"run_id": run_id, "task_id": task_id, "outcome": "success"},
    )
    review_fields: dict = {
        "run_id": run_id,
        "task_id": task_id,
        "verdict": "needs-rework",
    }
    if sanitizer_flags is not None:
        review_fields["extra"] = {"sanitizer_flags": sanitizer_flags}
    plan_ops._append_run_log("review_done", review_fields)


def _emit_review_route_called(
    repo: Path,
    *,
    run_id: str,
    payload: dict,
    directive: dict,
    task_id: str = "001",
) -> None:
    reviewer = payload.get("reviewer")
    if reviewer is None:
        reviewer = "codex" if payload.get("implementer") == "claude" else "claude"
    fields = {
        "run_id": run_id,
        "task_id": task_id,
        "action": directive["action"],
        "reviewer": reviewer,
        "implementer": payload["implementer"],
        "route_reason": _review_route_reason(payload, directive),
    }
    cp = _run(
        "log-event",
        "--event", "review_route_called",
        "--fields-json", json.dumps(fields),
        "--json",
        cwd=repo,
    )
    assert cp.returncode == 0, f"log-event failed: {cp.stderr}\n{cp.stdout}"


def _commit_via_subprocess(repo: Path, plan_child: Path, *,
                           run_id: str, schedule_file: Path,
                           extra_args: list[str] | None = None) -> dict:
    """Invoke commit-task via subprocess. Mutates src/foo.py first so the
    commit has staged content. Returns the parsed commit-task envelope.
    """
    (repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    args = [
        "commit-task",
        "--plan-file", str(plan_child),
        "--task-id", "001",
        "--run-id", run_id,
        "--files", "src/foo.py",
        "--title", "Smoke single task",
        "--diff-summary", "bump x",
        "--reviewer", "codex",
        "--reviewer-verdict", "clean",
        "--reviewer-minor-findings", "[]",
        "--update-schedule-state", str(schedule_file),
        "--json",
    ]
    if extra_args:
        args.extend(extra_args)
    cp = _run(*args, cwd=repo)
    assert cp.returncode == 0, f"commit-task failed: {cp.stderr}\n{cp.stdout}"
    return _parse_json(cp)


def _events(run_log: Path) -> list[dict]:
    if not run_log.exists():
        return []
    return [
        json.loads(line) for line in run_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---- Scenario 1: clean verdict → commit ----------------------------------


def test_scenario_clean_verdict_commits(smoke_repo: dict) -> None:
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    # Pre-write the schedule (chain test runs separately; we still need the
    # state-write target for --update-schedule-state).
    schedule_file.parent.mkdir(parents=True, exist_ok=True)
    sched = _bare_schedule(smoke_repo["plan_dir"].name)
    sched["state"] = plan_ops._empty_schedule_state()
    schedule_file.write_text(json.dumps(sched), encoding="utf-8")

    _emit_basic_events(run_log, run_id="R1")

    payload = _route_input(verdict="clean")
    out = _review_route_via_mcp(payload)
    assert out["action"] == "commit"
    assert out["args"]["commit_flags"]["disagreement_tag"] is False
    _emit_review_route_called(repo, run_id="R1", payload=payload, directive=out)

    body = _commit_via_subprocess(
        repo, smoke_repo["plan_child"], run_id="R1", schedule_file=schedule_file,
    )
    assert body["commit_sha"]
    plan_ops._append_run_log("batch_done", {"run_id": "R1", "batch_index": 1})
    plan_ops._append_run_log("run_end", {"run_id": "R1", "outcome": "success"})

    events = _events(run_log)
    names = [e["event"] for e in events]
    # AC: order = run_start, batch_start, implement_done, review_done,
    # review_route_called, commit_done, batch_done, run_end.
    assert names == [
        "run_start", "batch_start", "implement_done", "review_done",
        "review_route_called", "commit_done", "batch_done", "run_end",
    ], f"event order mismatch: {names}"
    route_event = events[names.index("review_route_called")]
    assert route_event["run_id"] == "R1"
    assert route_event["task_id"] == "001"
    assert route_event["action"] == "commit"
    assert route_event["reviewer"] == "codex"
    assert route_event["implementer"] == "claude"
    assert route_event["route_reason"] == "claude->codex clean => commit"
    assert "summary" not in route_event
    assert "reviewer_text" not in route_event


# ---- Scenario 2: minor-findings → commit-with-notes ----------------------


def test_scenario_minor_findings_commits_with_notes(smoke_repo: dict) -> None:
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    schedule_file.write_text(json.dumps({**_bare_schedule(""), "state": plan_ops._empty_schedule_state()}), encoding="utf-8")

    _emit_basic_events(run_log, run_id="R2")
    payload = _route_input(
        verdict="minor-findings",
        findings=[{"index": 0, "message": "nit"}],
    )
    out = plan_ops.route(payload)
    assert out["action"] == "commit"
    _emit_review_route_called(repo, run_id="R2", payload=payload, directive=out)

    minor = json.dumps([{
        "severity": "minor",
        "confidence": "high",
        "file": "src/foo.py",
        "line": 1,
        "issue": "trailing whitespace",
        "suggested_fix": "strip trailing whitespace",
        "disposition": "accepted",
    }])
    args = [
        "commit-task",
        "--plan-file", str(smoke_repo["plan_child"]),
        "--task-id", "001",
        "--run-id", "R2",
        "--files", "src/foo.py",
        "--title", "Smoke single task",
        "--diff-summary", "bump x with nit",
        "--reviewer", "codex",
        "--reviewer-verdict", "minor-findings",
        "--reviewer-minor-findings", minor,
        "--update-schedule-state", str(schedule_file),
        "--json",
    ]
    (repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    cp = _run(*args, cwd=repo)
    assert cp.returncode == 0, f"commit-task failed: {cp.stderr}\n{cp.stdout}"
    body = _parse_json(cp)
    assert body["commit_sha"]
    # Find the commit_done event, confirm it carries the minor finding.
    commit_events = [e for e in _events(run_log) if e["event"] == "commit_done"]
    assert commit_events, "no commit_done event"
    assert commit_events[-1]["minor_findings_count"] == 1


# ---- Scenario 3: D.5 ship → commit with disagreement-tag -----------------


def test_scenario_d5_ship_commits_with_disagreement_tag(smoke_repo: dict) -> None:
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    schedule_file.write_text(json.dumps({**_bare_schedule(""), "state": plan_ops._empty_schedule_state()}), encoding="utf-8")

    _emit_basic_events(run_log, run_id="R3")
    payload = _route_input(
        verdict="needs-rework",
        findings=[{"i": 0, "msg": "x"}],
        d5={"verdict": "ship", "load_bearing": [], "dismissed": [],
            "summary": "D5 disagrees"},
    )
    out = plan_ops.route(payload)
    assert out["action"] == "commit"
    assert out["args"]["commit_flags"]["disagreement_tag"] is True
    _emit_review_route_called(repo, run_id="R3", payload=payload, directive=out)

    body = _commit_via_subprocess(
        repo, smoke_repo["plan_child"], run_id="R3",
        schedule_file=schedule_file, extra_args=["--disagreement-tag"],
    )
    assert body["commit_sha"]
    commit_events = [e for e in _events(run_log) if e["event"] == "commit_done"]
    assert commit_events[-1]["disagreement_tag"] is True


# ---- Scenario 4: D.5 partial-agreement → narrow-remediation dispatch -----


def test_scenario_d5_partial_dispatches_narrow_remediation(smoke_repo: dict) -> None:
    findings = [{"i": 0, "msg": "load"}, {"i": 1, "msg": "dismiss"}]
    out = plan_ops.route(_route_input(
        verdict="needs-rework", findings=findings,
        d5={"verdict": "partial-agreement", "load_bearing": [0],
            "dismissed": [1], "summary": "split"},
    ))
    assert out["action"] == "dispatch_narrow_remediation"
    ctx = out["args"]["dispatch_context"]
    assert ctx["template"] == "PhaseB-narrow-remediation"
    assert ctx["dismissed_for_context"] == [findings[1]]


# ---- Scenario 5: narrow remediation success → commit-with-narrow-tag -----


def test_scenario_narrow_remediation_success_commits_with_narrow_tag(
    smoke_repo: dict,
) -> None:
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    schedule_file.write_text(json.dumps({**_bare_schedule(""), "state": plan_ops._empty_schedule_state()}), encoding="utf-8")

    _emit_basic_events(run_log, run_id="R5")
    # First-pass narrow dispatch happens in scenario 4. Here the retry
    # cleared all load-bearing findings; the binding-clean re-review now
    # routes to commit with --narrow-remediation-tag + dismissed indices.
    payload = _route_input(verdict="clean")
    out = plan_ops.route(payload)
    assert out["action"] == "commit"
    _emit_review_route_called(repo, run_id="R5", payload=payload, directive=out)

    args = [
        "commit-task",
        "--plan-file", str(smoke_repo["plan_child"]),
        "--task-id", "001",
        "--run-id", "R5",
        "--files", "src/foo.py",
        "--title", "Smoke single task",
        "--diff-summary", "bump x post narrow remediation",
        "--reviewer", "codex",
        "--reviewer-verdict", "clean",
        "--reviewer-minor-findings", "[]",
        "--narrow-remediation-tag",
        "--dismissed-finding-ids", "1",
        "--update-schedule-state", str(schedule_file),
        "--json",
    ]
    (repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    cp = _run(*args, cwd=repo)
    assert cp.returncode == 0, f"commit-task failed: {cp.stderr}\n{cp.stdout}"
    body = _parse_json(cp)
    assert body["commit_sha"]
    commit_events = [e for e in _events(run_log) if e["event"] == "commit_done"]
    last = commit_events[-1]
    assert last["narrow_remediation_tag"] is True
    assert last["dismissed_finding_ids"] == [1]


# ---- Scenario 6: narrow second-failure → pause_awaiting_user -------------


def test_scenario_narrow_second_failure_pauses_awaiting_user(
    smoke_repo: dict,
) -> None:
    run_log = smoke_repo["run_log"]
    _emit_basic_events(run_log, run_id="R6")
    payload = _route_input(
        verdict="needs-rework",
        findings=[{"i": 0}],
        d5={"verdict": "partial-agreement", "load_bearing": [0],
            "dismissed": [], "summary": "still loaded"},
        retries=_retries(narrow_remediation=True),
    )
    out = plan_ops.route(payload)
    assert out["action"] == "pause_awaiting_user"
    _emit_review_route_called(
        smoke_repo["repo_root"], run_id="R6", payload=payload, directive=out,
    )
    pp = out["args"]["pause_payload"]
    assert pp["stage"] == "post_narrow_remediation_review"
    assert pp["codex_findings"] == [{"i": 0}]

    # Orchestrator emits awaiting_user (TASK-014A allowlist) carrying the
    # pause payload; the smoke verifies the event is recorded.
    cp = _run(
        "log-event",
        "--event", "awaiting_user",
        "--fields-json", json.dumps({"run_id": "R6", "task_id": "001",
                                     "pause_payload": pp}),
        "--json",
        cwd=smoke_repo["repo_root"],
    )
    assert cp.returncode == 0, f"log-event failed: {cp.stderr}\n{cp.stdout}"
    awaiting = [e for e in _events(run_log) if e["event"] == "awaiting_user"]
    assert awaiting, "no awaiting_user event recorded"
    assert awaiting[-1]["pause_payload"]["stage"] == "post_narrow_remediation_review"


# ---- Scenario 7: D.5 needs-rework + retry success → commit + remediation -


def test_scenario_d5_needs_rework_retry_success_commits_with_remediation_tag(
    smoke_repo: dict,
) -> None:
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    schedule_file.write_text(json.dumps({**_bare_schedule(""), "state": plan_ops._empty_schedule_state()}), encoding="utf-8")

    _emit_basic_events(run_log, run_id="R7")
    # First pass: D.5 agrees → bounded remediation dispatched.
    first_payload = _route_input(
        verdict="needs-rework", findings=[{"i": 0}],
        d5={"verdict": "needs-rework", "load_bearing": [], "dismissed": [],
            "summary": "agree"},
    )
    first = plan_ops.route(first_payload)
    assert first["action"] == "dispatch_bounded_remediation"
    _emit_review_route_called(
        repo, run_id="R7", payload=first_payload, directive=first,
    )

    # Retry pass: post-remediation review now clean.
    second_payload = _route_input(verdict="clean")
    second = plan_ops.route(second_payload)
    assert second["action"] == "commit"
    _emit_review_route_called(
        repo, run_id="R7", payload=second_payload, directive=second,
    )

    body = _commit_via_subprocess(
        repo, smoke_repo["plan_child"], run_id="R7",
        schedule_file=schedule_file, extra_args=["--remediation-tag"],
    )
    assert body["commit_sha"]
    commit_events = [e for e in _events(run_log) if e["event"] == "commit_done"]
    assert commit_events[-1]["remediation_tag"] is True


# ---- Scenario 8: D.5 needs-rework + retry fail → pause_awaiting_user -----


def test_scenario_d5_needs_rework_retry_fail_pauses_awaiting_user(
    smoke_repo: dict,
) -> None:
    run_log = smoke_repo["run_log"]
    _emit_basic_events(run_log, run_id="R8")
    payload = _route_input(
        verdict="needs-rework",
        findings=[{"i": 0}],
        d5={"verdict": "needs-rework", "load_bearing": [], "dismissed": [],
            "summary": "still bad"},
        retries=_retries(bounded_remediation=True),
    )
    out = plan_ops.route(payload)
    assert out["action"] == "pause_awaiting_user"
    _emit_review_route_called(
        smoke_repo["repo_root"], run_id="R8", payload=payload, directive=out,
    )
    pp = out["args"]["pause_payload"]
    assert pp["stage"] == "post_remediation_review"

    cp = _run(
        "log-event",
        "--event", "awaiting_user",
        "--fields-json", json.dumps({"run_id": "R8", "task_id": "001",
                                     "pause_payload": pp}),
        "--json",
        cwd=smoke_repo["repo_root"],
    )
    assert cp.returncode == 0, f"log-event failed: {cp.stderr}\n{cp.stdout}"
    awaiting = [e for e in _events(run_log) if e["event"] == "awaiting_user"]
    assert awaiting[-1]["pause_payload"]["stage"] == "post_remediation_review"


# ---- Scenario 9: sanitizer-flag pass-through → commit + flags in log -----


def test_scenario_sanitizer_flags_surface_in_run_log(smoke_repo: dict) -> None:
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    schedule_file.write_text(json.dumps({**_bare_schedule(""), "state": plan_ops._empty_schedule_state()}), encoding="utf-8")

    sanitizer_flags = [
        {"shape": "looks-like-prompt-injection", "field": "summary", "count": 2},
    ]
    _emit_basic_events(run_log, run_id="R9", sanitizer_flags=sanitizer_flags)

    payload = _route_input(verdict="clean")
    out = plan_ops.route(payload)
    assert out["action"] == "commit"
    _emit_review_route_called(repo, run_id="R9", payload=payload, directive=out)

    body = _commit_via_subprocess(
        repo, smoke_repo["plan_child"], run_id="R9", schedule_file=schedule_file,
    )
    assert body["commit_sha"]
    plan_ops._append_run_log("batch_done", {"run_id": "R9", "batch_index": 1})
    plan_ops._append_run_log("run_end", {"run_id": "R9", "outcome": "success"})

    events = _events(run_log)
    review_done = [e for e in events if e["event"] == "review_done"]
    assert review_done, "no review_done event"
    assert review_done[-1]["extra"]["sanitizer_flags"] == sanitizer_flags
    # The raw payload (e.g. tampered summary string) MUST NOT appear in the
    # log line — only the sanitizer_flags metadata.
    raw = run_log.read_text(encoding="utf-8")
    assert "looks-like-prompt-injection" in raw
    assert "TAMPERED-PAYLOAD" not in raw


# ---------------------------------------------------------------------------
# Combined run-log event-order verification (AC: full order check).
# ---------------------------------------------------------------------------


def test_full_run_log_event_order_clean_path(smoke_repo: dict) -> None:
    """End-to-end event order on the clean path:
    run_start, batch_start, implement_done, review_done, review_route_called,
    commit_done, batch_done, run_end.
    """
    repo = smoke_repo["repo_root"]
    schedule_file = smoke_repo["schedule_file"]
    run_log = smoke_repo["run_log"]
    schedule_file.write_text(json.dumps({**_bare_schedule(""), "state": plan_ops._empty_schedule_state()}), encoding="utf-8")

    _emit_basic_events(run_log, run_id="ORDER")
    payload = _route_input(verdict="clean")
    out = plan_ops.route(payload)
    assert out["action"] == "commit"
    _emit_review_route_called(repo, run_id="ORDER", payload=payload, directive=out)
    _commit_via_subprocess(repo, smoke_repo["plan_child"], run_id="ORDER",
                           schedule_file=schedule_file)
    plan_ops._append_run_log("batch_done", {"run_id": "ORDER", "batch_index": 1})
    plan_ops._append_run_log("run_end", {"run_id": "ORDER", "outcome": "success"})

    expected = [
        "run_start", "batch_start", "implement_done", "review_done",
        "review_route_called", "commit_done", "batch_done", "run_end",
    ]
    actual = [e["event"] for e in _events(run_log)]
    assert actual == expected, f"event order mismatch: {actual}"


# ---------------------------------------------------------------------------
# parse-implementer-report stub: the AC says subagent outputs are injected
# via stub. We exercise the parse seam against a known-good report so the
# orchestrator-side parse path is not regressed when commit-task is wired
# downstream of it.
# ---------------------------------------------------------------------------


def test_parse_implementer_report_stub(smoke_repo: dict) -> None:
    cp = _run(
        "parse-implementer-report",
        "--stdin", "--json",
        cwd=smoke_repo["repo_root"],
        input_text=_stub_implementer_report(),
    )
    assert cp.returncode == 0, f"parse failed: {cp.stderr}\n{cp.stdout}"
    parsed = _parse_json(cp)
    assert parsed["outcome"] == "success"
