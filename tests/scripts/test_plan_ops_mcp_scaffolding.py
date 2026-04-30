"""Tests for the MCP server scaffolding (TASK-001).

Covers the acceptance criteria from
``docs/plans/MCP_MIGRATION/PLAN_MCP_MIGRATION.md`` TASK-001:

* Server starts, ``tools/list`` returns ``[]``, ``tools/call`` for any
  name returns ``MethodNotFound``, and the process exits 0 on stdin
  EOF.
* ``$PYTHON`` is resolved via ``plan_ops._resolve_python`` and surfaced
  under a ``python_path`` server capability.
* Uncaught crashes emit a single JSON error frame and exit non-zero.
* The plugin manifest registers the server under name ``plan-ops`` with
  the canonical ``${CLAUDE_PLUGIN_PYTHON}`` / ``${CLAUDE_PLUGIN_ROOT}``
  command shape.
* ``_subcommand_to_mcp_tool_name`` is a unit-tested pure helper.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plan-executor"
SERVER_PATH = PLUGIN_ROOT / "scripts" / "plan_ops_mcp_server.py"
MANIFEST_PATH = PLUGIN_ROOT / ".mcp.json"
SCHEMAS_KEEP = PLUGIN_ROOT / "scripts" / "schemas" / "mcp" / ".keep"


# Skip the live-server tests if the SDK isn't installed in this venv;
# the unit tests below still run.
_mcp_available = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not _mcp_available, reason="mcp SDK not installed")


def _import_server_module():
    """Import the server module by file path (it lives outside ``tests/``)."""
    spec = importlib.util.spec_from_file_location(
        "plan_ops_mcp_server", SERVER_PATH
    )
    assert spec and spec.loader, "could not load plan_ops_mcp_server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# File-shape acceptance criteria (do not require the mcp SDK at runtime).
# --------------------------------------------------------------------------


def test_scaffolding_files_exist():
    assert SERVER_PATH.is_file(), "plan_ops_mcp_server.py not created"
    assert MANIFEST_PATH.is_file(), ".mcp.json manifest not created"
    assert SCHEMAS_KEEP.is_file(), "schemas/mcp/.keep not created"


def test_manifest_registers_plan_ops_server():
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert "mcpServers" in payload
    servers = payload["mcpServers"]
    assert "plan-ops" in servers, f"expected 'plan-ops' entry, got {list(servers)!r}"
    entry = servers["plan-ops"]
    # Acceptance criterion (verbatim from TASK-001):
    #   command ["${CLAUDE_PLUGIN_PYTHON}",
    #            "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"]
    # Encoded canonically as Claude Code expects: command + args.
    assert entry["command"] == "${CLAUDE_PLUGIN_PYTHON}"
    assert entry["args"] == [
        "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"
    ]


# --------------------------------------------------------------------------
# Pure helper unit tests.
# --------------------------------------------------------------------------


@requires_mcp
def test_subcommand_to_mcp_tool_name_basic():
    mod = _import_server_module()
    f = mod._subcommand_to_mcp_tool_name
    assert f("review-route") == "review_route"
    assert f("claude-envelope-extract") == "claude_envelope_extract"
    assert f("batch-next") == "batch_next"
    assert f("commit-task") == "commit_task"
    # Idempotent on already-canonical input.
    assert f("review_route") == "review_route"
    assert f("preflight") == "preflight"


@requires_mcp
def test_subcommand_to_mcp_tool_name_rejects_bad_input():
    mod = _import_server_module()
    f = mod._subcommand_to_mcp_tool_name
    with pytest.raises(TypeError):
        f(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        f("")


@requires_mcp
def test_python_path_capability_matches_resolve_python():
    mod = _import_server_module()
    import plan_ops  # type: ignore[import-not-found]

    caps = mod.server_capabilities()
    assert caps.get("python_path") == plan_ops._resolve_python()
    assert mod.PYTHON_PATH == plan_ops._resolve_python()
    # Surface is an absolute, non-empty string.
    assert isinstance(caps["python_path"], str) and caps["python_path"]


# --------------------------------------------------------------------------
# Live-server tests via the in-process MCP stdio client.
# --------------------------------------------------------------------------


def _run_server_stdio_session(coro):
    """Run an async coroutine that drives the server over a stdio client."""
    return asyncio.run(coro())


@requires_mcp
def test_tools_list_returns_empty_array_and_exits_clean_on_eof():
    """Drive the server end-to-end: connect, list tools, disconnect."""
    from mcp.client.session import ClientSession  # type: ignore[import-not-found]
    from mcp.client.stdio import (  # type: ignore[import-not-found]
        StdioServerParameters,
        stdio_client,
    )

    async def _drive():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_PATH)],
            env={**os.environ, "PYTHONPATH": str(SERVER_PATH.parent)},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                # Tools registry is empty at TASK-001 scaffolding time.
                assert list(result.tools) == [], (
                    f"expected empty tools list at scaffolding stage, got {result.tools!r}"
                )

    _run_server_stdio_session(_drive)


@requires_mcp
def test_tools_call_returns_method_not_found_for_any_name():
    from mcp.client.session import ClientSession  # type: ignore[import-not-found]
    from mcp.client.stdio import (  # type: ignore[import-not-found]
        StdioServerParameters,
        stdio_client,
    )
    from mcp.shared.exceptions import McpError  # type: ignore[import-not-found]
    from mcp import types as mcp_types  # type: ignore[import-not-found]

    async def _drive():
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_PATH)],
            env={**os.environ, "PYTHONPATH": str(SERVER_PATH.parent)},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                # Any tool name → MethodNotFound. The SDK surfaces server
                # errors as either an McpError or a CallToolResult with
                # ``isError=True``; accept either shape.
                try:
                    result = await session.call_tool("plan_ops__nonexistent", {})
                except McpError as exc:
                    assert exc.error.code == mcp_types.METHOD_NOT_FOUND, (
                        f"expected METHOD_NOT_FOUND, got {exc.error.code}"
                    )
                    return
                # If we got back a result, it must be flagged as an error.
                assert getattr(result, "isError", False), (
                    f"call_tool should have errored, got {result!r}"
                )

    _run_server_stdio_session(_drive)


@requires_mcp
def test_server_exits_zero_on_stdin_eof():
    """Spawn the server as a subprocess; close stdin; expect exit 0."""
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(SERVER_PATH.parent)},
    )
    try:
        # No initialize handshake — just close stdin and let the
        # stdio_server loop unwind on EOF.
        assert proc.stdin is not None
        proc.stdin.close()
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        # Clean shutdown on EOF: no JSON error frame on stdout.
        stdout = proc.stdout.read() if proc.stdout else b""
        assert rc == 0, (
            f"expected exit 0 on stdin EOF, got rc={rc}; stdout={stdout!r}; "
            f"stderr={proc.stderr.read() if proc.stderr else b''!r}"
        )
        assert stdout.strip() == b"", (
            f"clean-shutdown path should not emit a JSON error frame; got: {stdout!r}"
        )
    finally:
        if proc.poll() is None:
            proc.kill()


@requires_mcp
def test_uncaught_crash_emits_json_error_frame_and_exits_nonzero(tmp_path):
    """Force an uncaught exception inside ``main()`` → JSON error frame on stdout."""
    # Wrap the real server module and force its ``_serve`` coroutine to
    # raise; verify the top-level crash handler emits a JSON-RPC error
    # frame on stdout and exits non-zero.
    runner = tmp_path / "crash_runner.py"
    runner.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(SERVER_PATH.parent)!r})
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "plan_ops_mcp_server", {str(SERVER_PATH)!r}
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            async def _boom():
                raise RuntimeError("synthetic crash for TASK-001 test")

            mod._serve = _boom
            sys.exit(mod.main([]))
            """
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(runner)],
        capture_output=True,
        timeout=15,
    )
    assert proc.returncode != 0, "crash path must exit non-zero"
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    assert stdout, f"expected JSON error frame on stdout, got empty; stderr={proc.stderr!r}"
    # Frame must be valid JSON with a JSON-RPC error envelope.
    # A single line / single object — never a partial-write.
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one frame on stdout, got {len(lines)}: {lines!r}"
    frame = json.loads(lines[0])
    assert frame.get("jsonrpc") == "2.0"
    assert "error" in frame and isinstance(frame["error"], dict)
    err = frame["error"]
    assert isinstance(err.get("code"), int)
    assert "synthetic crash" in err.get("message", "")
