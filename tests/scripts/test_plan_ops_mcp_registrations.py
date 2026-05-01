"""TASK-014 MCP registration/codegen conformance."""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
    assert len(server_mod.TOOL_REGISTRY) == 38

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


def test_codegen_block_is_byte_stable(server_mod) -> None:
    codegen = _import_module(CODEGEN_PATH, "mcp_tool_registrations_task014")
    text = SERVER_PATH.read_text(encoding="utf-8")
    start = text.index(codegen.BEGIN)
    end = text.index(codegen.END, start) + len(codegen.END) + 1
    assert text[start:end] == codegen.render_block()


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
