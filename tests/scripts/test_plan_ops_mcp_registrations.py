"""TASK-014 MCP registration/codegen conformance."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SERVER_PATH = SCRIPTS_DIR / "plan_ops_mcp_server.py"
CODEGEN_PATH = SCRIPTS_DIR / "_codegen" / "mcp_tool_registrations.py"
MCP_SCHEMA_DIR = SCRIPTS_DIR / "schemas" / "mcp"

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402

_mcp_available = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not _mcp_available, reason="mcp SDK not installed")


def _import_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server_mod():
    return _import_module(SERVER_PATH, "plan_ops_mcp_server_task014")


@pytest.fixture(scope="module")
def index() -> dict:
    return json.loads((MCP_SCHEMA_DIR / "_index.json").read_text(encoding="utf-8"))


def test_registry_matches_index_order_and_shape(server_mod, index: dict) -> None:
    expected_keys = [f"plan_ops__{name}" for name in index["tool_names_ordered"]]
    assert [entry["tool_key"] for entry in server_mod.TOOL_REGISTRY] == expected_keys
    assert len(server_mod.TOOL_REGISTRY) == len(expected_keys)

    for entry in server_mod.TOOL_REGISTRY:
        indexed = index["tools"][entry["tool_key"]]
        bare = entry["tool_key"].removeprefix("plan_ops__")
        assert entry["subcommand"] == indexed["subcommand"]
        assert entry["run_callable_name"] == f"_run_{bare}"
        assert entry["args_to_payload_callable_name"] == f"_args_to_payload_{bare}"
        assert isinstance(entry["input_schema"], dict)
        assert isinstance(entry["output_schema"], dict)
        assert entry["description_help"]


def test_registered_tools_have_valid_input_schemas(server_mod) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    for tool in server_mod._registered_mcp_tools():
        jsonschema.Draft202012Validator.check_schema(tool.inputSchema)
        assert tool.name.startswith("plan_ops__")
        assert tool.outputSchema is not None


def test_every_registration_dispatches_to_real_plan_ops_callables(server_mod) -> None:
    for entry in server_mod.TOOL_REGISTRY:
        assert callable(getattr(plan_ops, entry["run_callable_name"], None)), entry
        assert callable(getattr(plan_ops, entry["args_to_payload_callable_name"], None)), entry


def test_review_route_registration_is_orchestrator_contract(server_mod, index: dict) -> None:
    entry = index["tools"]["plan_ops__review_route"]
    assert entry["subcommand"] == "review-route"

    registered = server_mod._tool_registry_by_name()["plan_ops__review_route"]
    assert registered["subcommand"] == "review-route"
    assert registered["run_callable_name"] == "_run_review_route"
    assert registered["args_to_payload_callable_name"] == "_args_to_payload_review_route"
    assert registered["input_schema"]["$ref"].endswith("review_route_input_schema.json")
    assert registered["output_schema"]["$ref"].endswith("review_route_output_schema.json")


def test_codegen_block_is_byte_stable(server_mod) -> None:
    codegen = _import_module(CODEGEN_PATH, "mcp_tool_registrations_task014")
    server_bytes = SERVER_PATH.read_bytes()
    begin = codegen.BEGIN.encode("utf-8")
    end_marker = codegen.END.encode("utf-8")
    start = server_bytes.index(begin)
    end = server_bytes.index(end_marker, start) + len(end_marker) + 1

    rendered_bytes = codegen.render_block().encode("utf-8")
    assert server_bytes[start:end] == rendered_bytes


@requires_mcp
def test_tools_list_handler_returns_generated_registry(server_mod, index: dict) -> None:
    tools = server_mod._registered_mcp_tools()
    names = [tool.name for tool in tools]
    assert names == [f"plan_ops__{name}" for name in index["tool_names_ordered"]]


@requires_mcp
def test_dispatch_uses_args_builder_and_run_callable(server_mod, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(payload: dict) -> dict:
        captured["payload"] = payload
        return {"errors": [], "warnings": [], "ok": True}

    monkeypatch.setattr(plan_ops, "_run_preflight", fake_run)

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__preflight",
            {
                "plan_file": "/tmp/example-plan",
                "strict_branch": True,
                "strict_scope": False,
                "unattended_revert_policy": "pause",
            },
        )
    )

    assert result == {"errors": [], "warnings": [], "ok": True}
    assert captured["payload"]["plan_file"] == Path("/tmp/example-plan")
    assert captured["payload"]["strict_branch"] is True


@requires_mcp
def test_batch_next_mcp_dispatch_preserves_from_schedule_state_boolean(
    server_mod, tmp_path: Path,
) -> None:
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "state": {
                "done": ["001"],
                "failed": [],
                "blocked": [],
                "locked_files": [],
                "committed": [],
                "retries_used": {},
                "review_retry_counts": {},
            },
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        }),
        encoding="utf-8",
    )

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__batch_next",
            {
                "schedule_file": str(schedule),
                "from_schedule_state": True,
            },
        )
    )

    assert result["task_ids"] == ["002"]
    assert result["batch_index"] == 2


@requires_mcp
def test_batch_next_mcp_dispatch_accepts_native_state_lists(
    server_mod, tmp_path: Path,
) -> None:
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        }),
        encoding="utf-8",
    )

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__batch_next",
            {
                "schedule_file": str(schedule),
                "done": ["001"],
                "failed": [],
                "locked_files": [],
                "paused": [],
            },
        )
    )

    assert result["task_ids"] == ["002"]
    assert result["batch_index"] == 2


@requires_mcp
def test_batch_next_mcp_dispatch_false_from_schedule_state_does_not_read_state(
    server_mod, tmp_path: Path,
) -> None:
    schedule = tmp_path / "schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "state": {
                "done": ["001"],
                "failed": [],
                "blocked": [],
                "locked_files": [],
                "committed": [],
                "retries_used": {},
            },
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        }),
        encoding="utf-8",
    )

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__batch_next",
            {
                "schedule_file": str(schedule),
                "from_schedule_state": False,
            },
        )
    )

    assert result["task_ids"] == ["001"]
    assert result["batch_index"] == 1


@requires_mcp
def test_log_event_mcp_dispatch_passes_native_json_payloads(
    server_mod, monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_append(event: str, fields: dict) -> str:
        captured["event"] = event
        captured["fields"] = fields
        return json.dumps({"event": event, **fields})

    findings = [{
        "severity": "minor",
        "confidence": "medium",
        "file": "a.py",
        "line": 1,
        "issue": "x",
        "suggested_fix": "y",
    }]
    monkeypatch.setattr(plan_ops, "_append_run_log", fake_append)

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__log_event",
            {
                "event": "review_done",
                "fields_json": {
                    "run_id": "R1",
                    "task_id": "001",
                    "reviewer": "codex",
                    "verdict": "minor-findings",
                    "findings_count": 1,
                },
                "findings_json": findings,
            },
        )
    )

    assert result["ok"] is True
    assert captured["event"] == "review_done"
    assert captured["fields"]["run_id"] == "R1"
    assert captured["fields"]["findings"] == findings


@requires_mcp
def test_finalize_execution_log_mcp_dispatch_passes_native_rows(
    server_mod, tmp_path: Path,
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")
    rows = [{
        "task": "001",
        "agent": "codex",
        "reviewer": "gemini",
        "verdict": "clean",
        "commit": "abc123",
        "notes": "",
    }]

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__finalize_execution_log",
            {
                "plan_file": str(plan),
                "run_id": "R1",
                "starting_sha": "abc",
                "ending_sha": "def",
                "rows_json": rows,
            },
        )
    )

    assert plan_ops._public_result(result) == {"ok": True}
    text = plan.read_text(encoding="utf-8")
    assert "## Execution log" in text
    assert "| 001 | codex | gemini | clean | abc123 |  |" in text


@requires_mcp
def test_fail_task_mcp_dispatch_accepts_native_files_list(
    server_mod, tmp_path: Path, monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    plans = repo / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "sample.md"
    plan.write_text(
        "# Plan\n\n## Tasks\n\n### TASK-001: Smoke\n\n- **Status:** open\n",
        encoding="utf-8",
    )
    src = repo / "src"
    src.mkdir()
    (src / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (plans / "_run_log.jsonl").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    (src / "foo.py").write_text("x = 2\n", encoding="utf-8")
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans / "_run_log.jsonl")

    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__fail_task",
            {
                "plan_file": str(plan),
                "task_id": "001",
                "run_id": "R1",
                "files": ["src/foo.py"],
                "stage": "implement",
                "reason": "native files list",
                "authorization_source": "user-instruction",
                "reversion_guidance": "restore src/foo.py",
                "repo_root": str(repo),
            },
        )
    )

    assert result["status_updated"] is True
    assert result["restore_ok"] is True
    assert (src / "foo.py").read_text(encoding="utf-8") == "x = 1\n"


@requires_mcp
def test_body_level_errors_return_error_call_tool_result(server_mod, monkeypatch) -> None:
    def fake_run(payload: dict) -> dict:
        return {
            "errors": [{"path": "$.plan_file", "code": "invalid", "message": "bad"}],
            "warnings": [],
        }

    monkeypatch.setattr(plan_ops, "_run_preflight", fake_run)
    result = asyncio.run(
        server_mod._dispatch_registered_tool(
            "plan_ops__preflight",
            {
                "plan_file": "/tmp/example-plan",
                "unattended_revert_policy": "pause",
            },
        )
    )

    assert result.isError is True
    assert result.structuredContent["errors"][0]["code"] == "invalid"
