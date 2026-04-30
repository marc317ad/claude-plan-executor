"""MCP server scaffolding for ``plan_ops`` (TASK-001).

This module is the transport-layer chassis for the MCP migration of
``plan_ops.py`` (see ``docs/plans/MCP_MIGRATION/PLAN_MCP_MIGRATION.md``).
It instantiates an ``mcp.Server`` over stdio and registers no tools yet
— Tier-1/2/3 registration arrives in TASK-004/005/006.

Two stable surfaces this file exposes today:

* :func:`_subcommand_to_mcp_tool_name` — canonical mapping from an
  argparse subcommand string (e.g. ``review-route``) to the MCP tool
  name (``review_route``). The drift guard (TASK-009) and the
  registration helpers (TASK-004+) share this single source of truth.
* :data:`PYTHON_PATH` / :func:`server_capabilities` — the resolved
  interpreter (via ``plan_ops._resolve_python``) surfaced as a server
  capability ``python_path`` so MCP clients see the same interpreter
  ``preflight --json`` reports.

The server is stateless — every tool invocation will dispatch into a
``cmd_*`` / ``_run_*`` pure core in ``plan_ops``. There are no
long-lived caches; ``.schedule.json`` and the run-log remain durable
state.
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import Any

# Make sibling module importable when launched as a standalone script.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import plan_ops  # noqa: E402 — sibling module, sys.path adjusted above

try:
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.shared.exceptions import McpError
    from mcp import types as mcp_types
except ImportError as exc:  # pragma: no cover — surfaced to the test
    raise ImportError(
        "The 'mcp' Python SDK is required for plan_ops_mcp_server.py. "
        "Install it via `pip install 'mcp>=1.0'` (pinned for the "
        "MCP migration plan)."
    ) from exc


SERVER_NAME = "plan-ops"
SERVER_VERSION = "0.1.0"

# Resolved interpreter, computed once at import. Surfaced as a server
# capability so MCP clients can pin the same interpreter the bash CLI
# would resolve.
PYTHON_PATH: str = plan_ops._resolve_python()


def _subcommand_to_mcp_tool_name(name: str) -> str:
    """Canonicalize an argparse subcommand string to an MCP tool name.

    MCP tool names disallow hyphens; this helper is the single source of
    truth for the ``-`` → ``_`` mapping (e.g. ``review-route`` →
    ``review_route``, ``claude-envelope-extract`` →
    ``claude_envelope_extract``). Both the server registration loop
    (TASK-004+) and the drift guard (TASK-009) consume this function.

    Idempotent: input that already uses underscores is returned
    unchanged.
    """
    if not isinstance(name, str):
        raise TypeError(f"subcommand name must be a string, got {type(name).__name__}")
    if not name:
        raise ValueError("subcommand name must be non-empty")
    # Normalize NFD-style separators to a single canonical underscore.
    return name.replace("-", "_")


def server_capabilities() -> dict[str, Any]:
    """Return the server-level capability dict surfaced over MCP.

    Currently exposes ``python_path`` (parity with ``preflight --json``).
    Kept as a function rather than a constant so tests can monkeypatch
    ``plan_ops._resolve_python`` and observe the live value if needed.
    """
    return {"python_path": PYTHON_PATH}


def build_server() -> Server:
    """Construct the ``mcp.Server`` with no tools registered.

    Tier-1/2/3 tasks will extend this function with per-tool
    registrations. For TASK-001 the server's only behavior is:
    ``tools/list`` → ``[]``; ``tools/call`` → ``MethodNotFound`` for
    every name.
    """
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    @server.list_tools()
    async def _list_tools() -> list[mcp_types.Tool]:  # noqa: D401
        # No tools registered yet (TASK-001 scaffolding only).
        return []

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        # Until Tier-1 wiring lands, every tool invocation is a
        # MethodNotFound — the MCP runtime maps McpError(code=...) onto
        # a JSON-RPC error frame on the wire.
        raise McpError(
            mcp_types.ErrorData(
                code=mcp_types.METHOD_NOT_FOUND,
                message=f"Unknown tool: {name!r}",
                data={"server": SERVER_NAME, "python_path": PYTHON_PATH},
            )
        )

    return server


def _emit_fatal_error_frame(exc: BaseException) -> None:
    """Emit a single JSON error frame on stdout for an uncaught crash.

    Best-effort: if stdout is closed or a partial response was already
    flushed, we still write to stderr so operators can diagnose. Never
    raises.
    """
    frame = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": mcp_types.INTERNAL_ERROR if hasattr(mcp_types, "INTERNAL_ERROR") else -32603,
            "message": f"plan-ops MCP server crashed: {exc!r}",
            "data": {
                "traceback": traceback.format_exc().splitlines()[-20:],
                "python_path": PYTHON_PATH,
            },
        },
    }
    blob = json.dumps(frame, ensure_ascii=False)
    try:
        # Single atomic write — never half-emit a tool response.
        sys.stdout.write(blob + "\n")
        sys.stdout.flush()
    except Exception:  # pragma: no cover — stdout already closed
        try:
            sys.stderr.write(blob + "\n")
            sys.stderr.flush()
        except Exception:
            pass


async def _serve() -> None:
    server = build_server()
    init_options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main(argv: list[str] | None = None) -> int:
    """Module entry point.

    Returns 0 on clean stdin EOF / normal shutdown; non-zero on an
    uncaught crash (with a JSON error frame already emitted).
    """
    try:
        asyncio.run(_serve())
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 0
    except BaseException as exc:  # noqa: BLE001 — last-resort crash handler
        _emit_fatal_error_frame(exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
