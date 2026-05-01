"""Spawn-level regression tests for the plan-ops MCP manifest launcher."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plan-executor"
MANIFEST_PATH = PLUGIN_ROOT / ".mcp.json"
VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"

_mcp_available = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not _mcp_available, reason="mcp SDK not installed")


def _manifest_entry() -> dict:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return payload["mcpServers"]["plan-ops"]


def _expand_plugin_root(value: str) -> str:
    return value.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))


def _resolved_manifest_command() -> tuple[Path, list[str]]:
    entry = _manifest_entry()
    command = Path(_expand_plugin_root(entry["command"]))
    args = [_expand_plugin_root(arg) for arg in entry["args"]]
    return command, args


def _read_json_line(stream) -> dict:
    assert stream is not None
    line = stream.readline()
    assert line, "expected a JSON-RPC response frame on stdout"
    return json.loads(line)


def test_manifest_command_resolves_to_executable_launcher():
    command, _args = _resolved_manifest_command()
    assert command.is_file()
    mode = command.stat().st_mode
    assert mode & stat.S_IXUSR, f"launcher is not user-executable: {command}"


@requires_mcp
def test_manifest_spawn_returns_valid_initialize_response():
    command, args = _resolved_manifest_command()
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "manifest-spawn-test", "version": "0.1"},
        },
    }
    proc = subprocess.Popen(
        [str(command), *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(PLUGIN_ROOT / "scripts")},
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.close()
        response = _read_json_line(proc.stdout)
        rc = proc.wait(timeout=10)
        stderr = proc.stderr.read() if proc.stderr else ""
    finally:
        if proc.poll() is None:
            proc.kill()

    assert rc == 0, f"server exited {rc}; stderr={stderr!r}; response={response!r}"
    result = response["result"]
    assert result["serverInfo"]["name"] == "plan-ops"
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]


def test_manifest_spawn_uses_project_venv_interpreter(tmp_path):
    command, _args = _resolved_manifest_command()
    probe = tmp_path / "print_executable.py"
    probe.write_text(
        "import sys\nprint(sys.executable)\n",
        encoding="utf-8",
    )
    cp = subprocess.run(
        [str(command), str(probe)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert Path(cp.stdout.strip()).resolve() == VENV_PYTHON.resolve()


def test_original_undefined_python_variable_would_not_spawn():
    """Negative control: the old manifest command would have failed at spawn time."""
    assert "${CLAUDE_PLUGIN_PYTHON}" not in _manifest_entry()["command"]
    assert "CLAUDE_PLUGIN_PYTHON" not in os.environ
