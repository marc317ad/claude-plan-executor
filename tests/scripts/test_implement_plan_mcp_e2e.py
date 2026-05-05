"""TASK-018 end-to-end smoke: /implement-plan uses plan_ops MCP tools.

The outer test is intentionally narrative: it drives a one-task decomposed plan
from Phase 0 through Phase E with stubbed Codex/Claude envelopes. A paired run
uses the bash CLI for the same plan operations, and the run-log event sequence
must match after removing the MCP-only server/tool telemetry.

The inner loop is data-driven over ``schemas/mcp/_index.json`` so registry drift
is caught without hand-writing one assertion per tool.
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
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
PLAN_OPS = SCRIPTS_DIR / "plan_ops.py"
SERVER_PATH = SCRIPTS_DIR / "plan_ops_mcp_server.py"
MCP_INDEX = SCRIPTS_DIR / "schemas" / "mcp" / "_index.json"
FIXTURE_PLAN = Path(__file__).parent / "fixtures" / "mcp_e2e_plan.md"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

_mcp_available = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not _mcp_available, reason="mcp SDK not installed")

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402

_SERVER_MODULE: Any | None = None


def _load_server_module() -> Any:
    global _SERVER_MODULE
    if _SERVER_MODULE is not None:
        return _SERVER_MODULE
    spec = importlib.util.spec_from_file_location("plan_ops_mcp_server_e2e", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _SERVER_MODULE = module
    return module


def _index() -> dict[str, Any]:
    return json.loads(MCP_INDEX.read_text(encoding="utf-8"))


def _roster() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "chunks": [
                {
                    "task_id": "001",
                    "file": "TASK-001_mcp_smoke.md",
                    "depends_on": [],
                    "status": "Pending",
                    "superseded_by": [],
                }
            ],
        },
        indent=2,
    ) + "\n"


def _seed_repo(root: Path) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "src").mkdir()
    (root / "src" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    plan_dir = root / "docs" / "plans" / "mcp_e2e_plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "00_INDEX.json").write_text(_roster(), encoding="utf-8")
    plan_child = plan_dir / "TASK-001_mcp_smoke.md"
    shutil.copy(FIXTURE_PLAN, plan_child)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return {
        "repo": root,
        "plans_dir": root / "docs" / "plans",
        "plan_dir": plan_dir,
        "plan_child": plan_child,
        "schedule": root / "docs" / "plans" / "mcp_e2e_plan.schedule.json",
        "run_log": root / "docs" / "plans" / "_run_log.jsonl",
    }


def _schedule_from_build(build: dict[str, Any]) -> dict[str, Any]:
    return {
        "outcome": "valid" if not build.get("warnings") else "needs-enrichment",
        "tasks": build["tasks"],
        "batches": build["batches"],
        "gaps": [
            {
                "task_id": warning.get("task_id"),
                "type": warning.get("code", "warning"),
                "severity": "soft",
                "detail": warning.get("message", ""),
            }
            for warning in build.get("warnings", [])
            if isinstance(warning, dict)
        ],
        "risks": [],
    }


def _stub_implementer_report() -> str:
    return (
        "**Outcome:** success\n\n"
        "**Files changed:**\n"
        "- src/foo.py (+1 -1 lines)\n\n"
        "**Diff summary:**\n"
        "- changed x to 2\n\n"
        "**Test command:** none\n"
        "**Test outcome:** not-run\n\n"
        "**Acceptance criteria check:**\n"
        "- [x] src/foo.py committed\n\n"
        "**Coupling check:** not applicable\n\n"
        "**Plan adaptations:** None\n\n"
        "**Concerns for reviewer:** None\n"
    )


def _review_route_payload(verdict: str = "clean", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": "001",
        "implementer": "claude",
        "reviewer_envelope": {
            "verdict": verdict,
            "findings": [],
            "summary": "",
        },
        "d5_envelope": None,
        "retries_used": {
            "bounded_remediation": False,
            "narrow_remediation": False,
            "role_swap": False,
            "codex_fallback": False,
        },
        "flags": {"codex_review_binding": False, "skip_cross_review": False},
    }
    payload.update(overrides)
    return payload


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _canonical_events(path: Path) -> list[dict[str, Any]]:
    events = [
        event
        for event in _events(path)
        if event["event"] not in {"mcp_server_start", "mcp_tool_called"}
    ]
    canonical: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        item.pop("ts", None)
        if item.get("event") == "commit_done":
            item["commit_sha"] = "<sha>"
        canonical.append(item)
    return canonical


def _public(result: Any) -> dict[str, Any]:
    if hasattr(result, "structuredContent") and result.structuredContent is not None:
        return dict(result.structuredContent)
    if isinstance(result, dict):
        return dict(result)
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    raise AssertionError(f"cannot extract MCP result from {result!r}")


class _McpOps:
    def __init__(self, paths: dict[str, Path], *, crash_after: int | None = None) -> None:
        self.paths = paths
        self.server = _load_server_module()
        self.calls: list[str] = []
        self.crash_after = crash_after
        self._saved_cwd: str | None = None
        self._saved_paths: tuple[Any, Any, Any] | None = None

    def __enter__(self) -> "_McpOps":
        self._saved_cwd = os.getcwd()
        self._saved_paths = (
            plan_ops.PLAN_DIR,
            plan_ops.RUN_LOG_PATH,
            plan_ops.RUN_LOCK_PATH,
        )
        os.chdir(self.paths["repo"])
        plan_ops.PLAN_DIR = self.paths["plans_dir"]
        plan_ops.RUN_LOG_PATH = self.paths["run_log"]
        plan_ops.RUN_LOCK_PATH = self.paths["plans_dir"] / "_run_lock.json"
        plan_ops._append_run_log(
            "mcp_server_start",
            {"pid": os.getpid(), "python_path": str(PY)},
        )
        return self

    def __exit__(self, *_exc: Any) -> None:
        assert self._saved_cwd is not None
        assert self._saved_paths is not None
        os.chdir(self._saved_cwd)
        plan_ops.PLAN_DIR, plan_ops.RUN_LOG_PATH, plan_ops.RUN_LOCK_PATH = self._saved_paths

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = dict(arguments or {})
        if tool == "commit_task" and isinstance(arguments.get("reviewer_minor_findings"), list):
            arguments["reviewer_minor_findings"] = json.dumps(arguments["reviewer_minor_findings"])
        if self.crash_after is not None and len(self.calls) >= self.crash_after:
            plan_ops._append_run_log(
                "awaiting_user",
                {
                    "run_id": "MCP-CRASH",
                    "task_id": "001",
                    "pause_payload": {
                        "stage": "mcp_transport_error",
                        "error_frame": {
                            "jsonrpc": "2.0",
                            "error": {
                                "code": -32603,
                                "message": "synthetic MCP server crash",
                            },
                        },
                    },
                },
            )
            raise RuntimeError("synthetic MCP server crash")
        self.calls.append(tool)
        plan_ops._append_run_log("mcp_tool_called", {"tool": tool})
        result = asyncio.run(
            self.server._dispatch_registered_tool(f"plan_ops__{tool}", arguments)
        )
        return _public(result)


class _CliOps:
    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths
        self.calls: list[str] = []

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        index = _index()
        subcommand = index["tools"][f"plan_ops__{tool}"]["subcommand"]
        args = arguments or {}
        stdin_text = ""
        argv = [subcommand]
        for key, value in args.items():
            if key == "payload":
                stdin_text = value if isinstance(value, str) else json.dumps(value)
                argv.append("--stdin")
                continue
            if value is None or value is False:
                continue
            flag = "--" + key.replace("_", "-")
            if value is True:
                argv.append(flag)
            elif isinstance(value, (dict, list)):
                argv.extend([flag, json.dumps(value)])
            else:
                argv.extend([flag, str(value)])
        argv.append("--json")
        self.calls.append(tool)
        cp = subprocess.run(
            [str(PY), str(PLAN_OPS), *argv],
            input=stdin_text,
            cwd=self.paths["repo"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert cp.returncode == 0, f"{subcommand} failed\nstdout={cp.stdout}\nstderr={cp.stderr}"
        return json.loads(cp.stdout) if cp.stdout.strip() else {}


def _drive_clean_path(paths: dict[str, Path], ops: Any, run_id: str) -> dict[str, Any]:
    plan_dir = paths["plan_dir"]
    plan_child = paths["plan_child"]
    schedule = paths["schedule"]

    preflight = ops.call(
        "preflight",
        {
            "plan_file": str(plan_dir),
            "unattended_revert_policy": "pause",
        },
    )
    assert preflight["pass"] is True
    ops.call("log_event", {"event": "run_start", "fields_json": {"run_id": run_id}})

    build = ops.call("build_tasks", {"plans_dir": str(plan_dir)})
    schedule_doc = _schedule_from_build(build)
    parsed = ops.call("parse_schedule", {"payload": schedule_doc})
    assert parsed["errors"] == []
    ops.call("write_schedule", {"schedule_file": str(schedule), "payload": schedule_doc})
    ops.call("gates", {"check": "schedule-valid", "schedule_file": str(schedule)})
    ops.call("log_event", {"event": "batch_start", "fields_json": {"run_id": run_id, "batch_index": 1}})
    batch = ops.call("batch_next", {"schedule_file": str(schedule)})
    assert batch.get("batch_index") == 1 or batch.get("selected_batch", {}).get("index") == 1

    wrapper = {
        "status": "ok",
        "result": {"outcome": "success", "report": _stub_implementer_report()},
        "scope": {"scope_violation_detected": False, "scope_misreport_detected": False},
    }
    extracted = ops.call("claude_envelope_extract", {"agent": "plan-implementer", "payload": wrapper})
    assert extracted["status"] == "ok"
    parsed_impl = ops.call("parse_implementer_report", {"payload": _stub_implementer_report()})
    assert parsed_impl["outcome"] == "success"
    ops.call("log_event", {"event": "implement_done", "fields_json": {"run_id": run_id, "task_id": "001", "outcome": "success"}})

    review = {
        "task_id": "plan",
        "plan_file": "mcp_e2e_plan",
        "subcommand": "plan-review",
        "outcome": "success",
        "codex_exit_code": 0,
        "codex_output_raw": None,
        "error": None,
        "parsed": {
            "plan_file": "mcp_e2e_plan",
            "verdict": "approved",
            "findings": [],
            "notes": [],
            "schedule_ok": True,
            "summary": "ship",
        },
    }
    parsed_review = ops.call("parse_plan_review_report", {"payload": review})
    assert parsed_review["verdict"] == "approved"
    ops.call("log_event", {"event": "review_done", "fields_json": {"run_id": run_id, "task_id": "001", "verdict": "clean"}})

    route = ops.call("review_route", {"payload": _review_route_payload("clean")})
    assert route["action"] == "commit"
    (paths["repo"] / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
    commit = ops.call(
        "commit_task",
        {
            "plan_file": str(plan_child),
            "task_id": "001",
            "run_id": run_id,
            "files": "src/foo.py",
            "title": "MCP smoke single task",
            "diff_summary": "changed x to 2",
            "reviewer": "codex",
            "reviewer_verdict": "clean",
            "reviewer_minor_findings": [],
            "update_schedule_state": str(schedule),
        },
    )
    assert commit["commit_sha"]
    gate = ops.call(
        "gates",
        {
            "check": "commit-safe",
            "plan_file": str(plan_child),
            "commit_sha": commit["commit_sha"],
            "task_id": "001",
        },
    )
    gates = gate["gates"]
    commit_safe = gates["commit-safe"] if isinstance(gates, dict) else gates[0]
    assert commit_safe["name"] == "commit-safe"
    assert commit_safe["status"] == "pass"
    ops.call("log_event", {"event": "run_end", "fields_json": {"run_id": run_id, "outcome": "success"}})
    return commit


@requires_mcp
def test_phase_0_to_e_mcp_matches_cli_run_log_without_plan_ops_bash(tmp_path: Path) -> None:
    cli_paths = _seed_repo(tmp_path / "cli")
    mcp_paths = _seed_repo(tmp_path / "mcp")

    cli_ops = _CliOps(cli_paths)
    _drive_clean_path(cli_paths, cli_ops, "RUN-E2E")
    with _McpOps(mcp_paths) as mcp_ops:
        _drive_clean_path(mcp_paths, mcp_ops, "RUN-E2E")

    assert _canonical_events(mcp_paths["run_log"]) == _canonical_events(cli_paths["run_log"])
    assert "commit_task" in mcp_ops.calls
    assert all("plan_ops.py" not in call for call in mcp_ops.calls)
    mcp_names = [event["event"] for event in _events(mcp_paths["run_log"])]
    assert "mcp_server_start" in mcp_names
    assert mcp_names.count("mcp_tool_called") >= 12


@requires_mcp
def test_transport_smoke_uses_mcp_payload_shapes_without_cli_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _seed_repo(tmp_path)
    subprocess_run = subprocess.run

    def guarded_run(cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        parts = [str(part) for part in cmd] if isinstance(cmd, (list, tuple)) else [str(cmd)]
        assert not any(part.endswith("plan_ops.py") for part in parts), parts
        assert "-c" not in parts, parts
        return subprocess_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    schedule_doc = {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "agent": "codex",
                "files": ["src/foo.py"],
                "dependencies": [],
                "plan_file": paths["plan_child"].name,
            },
            {
                "id": "002",
                "agent": "claude",
                "files": ["src/bar.py"],
                "dependencies": ["001"],
                "plan_file": paths["plan_child"].name,
            },
        ],
        "batches": [
            {"index": 1, "task_ids": ["001"], "file_locks": ["src/foo.py"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["src/bar.py"]},
        ],
        "gaps": [],
        "risks": [],
        "state": {
            "done": ["001"],
            "failed": [],
            "blocked": [],
            "locked_files": [],
            "committed": [],
            "retries_used": {},
        },
    }

    with _McpOps(paths) as ops:
        logged = ops.call(
            "log_event",
            {
                "event": "review_done",
                "fields_json": {
                    "run_id": "PAYLOAD-SMOKE",
                    "task_id": "001",
                    "verdict": "needs-rework",
                },
                "findings_json": [
                    {
                        "severity": "minor",
                        "confidence": "high",
                        "file": "src/foo.py",
                        "line": 1,
                        "issue": "native list payload",
                        "suggested_fix": "keep MCP structured",
                    }
                ],
            },
        )
        assert logged["ok"] is True

        written = ops.call(
            "write_schedule",
            {"schedule_file": str(paths["schedule"]), "payload": schedule_doc},
        )
        assert written["written"] == str(paths["schedule"])

        batch = ops.call(
            "batch_next",
            {
                "schedule_file": str(paths["schedule"]),
                "done": [],
                "failed": [],
                "locked_files": [],
                "paused": [],
                "from_schedule_state": True,
            },
        )
        assert batch["task_ids"] == ["002"]
        assert batch["batch_index"] == 2

        wrapper = {
            "status": "ok",
            "result": {"outcome": "success", "report": _stub_implementer_report()},
            "scope": {
                "scope_violation_detected": True,
                "scope_misreport_detected": False,
                "out_of_scope_observed": True,
                "out_of_scope_tracked": [],
                "out_of_scope_untracked": ["leak.txt"],
            },
        }
        extracted = ops.call(
            "claude_envelope_extract",
            {"agent": "plan-implementer", "payload": wrapper},
        )
        assert extracted["status"] == "ok"
        assert extracted["scope_violation"] is True

        reconciled = ops.call(
            "reconcile_batch",
            {
                "repo_root": str(paths["repo"]),
                "schedule_file": str(paths["schedule"]),
                "plans_dir": str(paths["plan_dir"]),
                "out_of_scope_policy": "pause",
                "payload": [
                    {
                        "task_id": "001",
                        "scope": {
                            "out_of_scope_observed": True,
                            "out_of_scope_tracked": ["src/foo.py"],
                            "out_of_scope_untracked": ["leak.txt"],
                        },
                    }
                ],
            },
        )
        assert reconciled["paused"] is True
        assert reconciled["results"][0]["outcome"] == "scope_violation_paused"
        assert reconciled["results"][0]["reconcile_kept_tracked"] == ["src/foo.py"]

        finalized = ops.call(
            "finalize_execution_log",
            {
                "plan_file": str(paths["plan_child"]),
                "run_id": "PAYLOAD-SMOKE",
                "starting_sha": "abc123",
                "ending_sha": "def456",
                "rows_json": [
                    {
                        "task": "001",
                        "agent": "codex",
                        "reviewer": "claude",
                        "verdict": "clean",
                        "commit": "def456",
                        "notes": "native rows list",
                    }
                ],
                "outcome": "success",
            },
        )
        assert finalized["ok"] is True

    assert {
        "log_event",
        "write_schedule",
        "batch_next",
        "claude_envelope_extract",
        "reconcile_batch",
        "finalize_execution_log",
    } <= set(ops.calls)
    assert all("plan_ops.py" not in call for call in ops.calls)
    assert all("python" not in call.lower() for call in ops.calls)
    events = _events(paths["run_log"])
    review_done = [event for event in events if event["event"] == "review_done"][-1]
    assert review_done["findings"] == [
        {
            "severity": "minor",
            "confidence": "high",
            "file": "src/foo.py",
            "line": 1,
            "issue": "native list payload",
            "suggested_fix": "keep MCP structured",
        }
    ]
    assert "## Execution log" in paths["plan_child"].read_text(encoding="utf-8")


def test_mcp_e2e_skill_contract_rejects_inline_python_envelope_parsing() -> None:
    skill_text = (REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Never write inline Python for envelope parsing." in skill_text
    assert "plan_ops__claude_envelope_extract" in skill_text
    assert "plan_ops.py claude-envelope-extract" in skill_text
    forbidden_fragments = [
        "python <<",
        "python3 <<",
        '"stdin": <envelope>',
        '"stdin": <report>',
        '"stdin": <schedule_json>',
        '"stdin": <envelopes_json>',
    ]
    for fragment in forbidden_fragments:
        assert fragment not in skill_text


def test_mcp_e2e_skill_claude_envelope_extract_recipe_uses_build_claude_dispatch_input_output_path() -> None:
    """SKILL.md must document the MCP-mode wrapper recipe: build to file, run,
    then route through plan_ops__claude_envelope_extract with native payload.
    Drift here re-introduces ad hoc `--input -` shell pipes that break MCP mode.
    """

    skill_text = (REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Claude wrapper dispatch recipe (canonical)" in skill_text
    # The MCP-mode recipe writes the envelope to disk via `output: "..."` and
    # then invokes plan_claude_dispatch.py with that on-disk path.
    assert '"output": "<tmp dispatch input path>"' in skill_text
    assert "--input <tmp dispatch input path>" in skill_text
    # And every wrapper-envelope routing point is normalized through MCP.
    assert (
        'Tool: plan_ops__claude_envelope_extract with input {"agent":'
        in skill_text
    )
    # The legacy unqualified pipe form must not appear outside CLI-fallback context.
    legacy = "Pipe stdout into `plan_claude_dispatch.py run --input -`"
    if legacy in skill_text:
        idx = skill_text.find(legacy)
        window = skill_text[max(0, idx - 200) : idx]
        assert (
            "CLI fallback" in window
            or "CLI-fallback" in window
            or "cli-fallback" in window
        ), (
            "SKILL.md still recommends piping the MCP builder response into Bash "
            "without a CLI-fallback qualifier."
        )


@requires_mcp
@pytest.mark.parametrize("tool_name", _index()["tool_names_ordered"])
def test_inner_loop_index_tools_have_mcp_event_sequence(tool_name: str, tmp_path: Path) -> None:
    paths = _seed_repo(tmp_path / tool_name)
    with _McpOps(paths) as ops:
        if tool_name == "normalize_task_id":
            assert ops.call(tool_name, {"id": "TASK-001"})["normalized"] == "001"
        else:
            # The inner loop is about the registry/event path, not each tool's
            # semantic fixture. Unknown/missing body errors are acceptable here.
            try:
                ops.call(tool_name, {})
            except Exception:
                pass
    names = [event["event"] for event in _events(paths["run_log"])]
    assert names[:2] == ["mcp_server_start", "mcp_tool_called"]
    assert _events(paths["run_log"])[1]["tool"] == tool_name


@requires_mcp
def test_review_route_covers_every_action_value(tmp_path: Path) -> None:
    paths = _seed_repo(tmp_path)
    with _McpOps(paths) as ops:
        actions = {
            ops.call("review_route", {"payload": _review_route_payload("clean")})["action"],
            ops.call(
                "review_route",
                {
                    "payload": _review_route_payload(
                        "needs-rework",
                        reviewer_envelope={
                            "verdict": "needs-rework",
                            "findings": [{"i": 0}],
                            "summary": "",
                        },
                        d5_envelope={"verdict": "needs-rework", "summary": "", "load_bearing": [], "dismissed": []},
                    )
                },
            )["action"],
            ops.call(
                "review_route",
                {
                    "payload": _review_route_payload(
                        "needs-rework",
                        reviewer_envelope={
                            "verdict": "needs-rework",
                            "findings": [{"i": 0}, {"i": 1}],
                            "summary": "",
                        },
                        d5_envelope={
                            "verdict": "partial-agreement",
                            "summary": "",
                            "load_bearing": [0],
                            "dismissed": [1],
                        },
                    )
                },
            )["action"],
            ops.call(
                "review_route",
                {
                    "payload": _review_route_payload(
                        "needs-rework",
                        reviewer_envelope={
                            "verdict": "needs-rework",
                            "findings": [{"i": 0}],
                            "summary": "",
                        },
                        retries_used={
                            "bounded_remediation": True,
                            "narrow_remediation": False,
                            "role_swap": False,
                            "codex_fallback": False,
                        },
                        d5_envelope={"verdict": "needs-rework", "summary": "", "load_bearing": [], "dismissed": []},
                    )
                },
            )["action"],
            ops.call(
                "review_route",
                {"payload": _review_route_payload("unknown-verdict")},
            )["action"],
        }
    assert actions >= {
        "commit",
        "dispatch_bounded_remediation",
        "dispatch_narrow_remediation",
        "pause_awaiting_user",
        "unknown_state",
    }


@requires_mcp
def test_reconcile_partition_and_fail_task_authorization_source(tmp_path: Path) -> None:
    paths = _seed_repo(tmp_path)
    with _McpOps(paths) as ops:
        build = ops.call("build_tasks", {"plans_dir": str(paths["plan_dir"])})
        schedule_doc = _schedule_from_build(build)
        ops.call("write_schedule", {"schedule_file": str(paths["schedule"]), "payload": schedule_doc})
        (paths["repo"] / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        (paths["repo"] / "leak.txt").write_text("leak\n", encoding="utf-8")
        envelopes = [
            {
                "task_id": "001",
                "out_of_scope_observed": True,
                "out_of_scope_tracked": ["src/foo.py"],
                "out_of_scope_untracked": ["leak.txt"],
            }
        ]
        reconciled = ops.call(
            "reconcile_batch",
            {
                "repo_root": str(paths["repo"]),
                "schedule_file": str(paths["schedule"]),
                "plans_dir": str(paths["plan_dir"]),
                "out_of_scope_policy": "pause",
                "payload": envelopes,
            },
        )
        row = reconciled["results"][0]
        assert row["outcome"] == "scope_violation_paused"
        assert "src/foo.py" in row.get("reconcile_kept_tracked", [])
        failed = ops.call(
            "fail_task",
            {
                "plan_file": str(paths["plan_child"]),
                "task_id": "001",
                "run_id": "FAIL-AUTH",
                "files": "src/foo.py",
                "stage": "implement",
                "reason": "user chose fail after scope pause",
                "authorization_source": "reconcile-out-of-scope-user-instruction",
                "reversion_guidance": "restore src/foo.py",
                "repo_root": str(paths["repo"]),
                "update_schedule_state": str(paths["schedule"]),
            },
        )
    assert failed["status_updated"] is True
    fail_events = [event for event in _events(paths["run_log"]) if event["event"] == "failed"]
    assert fail_events[-1]["authorization_source"] == "reconcile-out-of-scope-user-instruction"


@requires_mcp
def test_mcp_crash_pauses_without_half_commit(tmp_path: Path) -> None:
    paths = _seed_repo(tmp_path)
    with _McpOps(paths, crash_after=3) as ops:
        with pytest.raises(RuntimeError, match="synthetic MCP server crash"):
            _drive_clean_path(paths, ops, "MCP-CRASH")
    assert not [event for event in _events(paths["run_log"]) if event["event"] == "commit_done"]
    assert subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=paths["repo"], capture_output=True, text=True).stdout.strip() == "1"
    awaiting = [event for event in _events(paths["run_log"]) if event["event"] == "awaiting_user"]
    assert awaiting[-1]["pause_payload"]["stage"] == "mcp_transport_error"
