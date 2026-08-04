"""Spawn-level regression tests for the plan-ops MCP manifest launcher.

The manifest command is ``${PLAN_OPS_LAUNCHER:-<plugin>/scripts/run_mcp_server.sh}``:
with the override unset (every POSIX host today), Claude Code's env
expansion resolves it to the .sh launcher, so the POSIX tests below
exercise exactly the default branch. The Windows tests exercise the
``run_mcp_server.cmd`` counterpart that a host wires in via the
``PLAN_OPS_LAUNCHER`` environment variable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plan-executor"
MANIFEST_PATH = PLUGIN_ROOT / ".mcp.json"
VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"
WINDOWS_LAUNCHER = PLUGIN_ROOT / "scripts" / "run_mcp_server.cmd"
WINDOWS_VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"

_mcp_available = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not _mcp_available, reason="mcp SDK not installed")
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX .sh launcher path"
)
windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows .cmd launcher path"
)

# ${PLAN_OPS_LAUNCHER:-<default>} — the default is the canonical launcher.
_OVERRIDE_RE = re.compile(r"^\$\{PLAN_OPS_LAUNCHER:-(?P<default>.+)\}$")


def _manifest_entry() -> dict:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return payload["mcpServers"]["plan-ops"]


def _expand_plugin_root(value: str) -> str:
    return value.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT))


def _manifest_default_command() -> str:
    """Unwrap the ${PLAN_OPS_LAUNCHER:-...} override to its default."""
    command = _manifest_entry()["command"]
    match = _OVERRIDE_RE.match(command)
    assert match, f"unexpected manifest command shape: {command!r}"
    return match.group("default")


def _resolved_manifest_command() -> tuple[Path, list[str]]:
    command = Path(_expand_plugin_root(_manifest_default_command()))
    args = [_expand_plugin_root(arg) for arg in _manifest_entry()["args"]]
    return command, args


def _read_json_line(stream) -> dict:
    assert stream is not None
    line = stream.readline()
    assert line, "expected a JSON-RPC response frame on stdout"
    return json.loads(line)


def _initialize_request() -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "manifest-spawn-test", "version": "0.1"},
        },
    }


def _spawn_initialize(command: Path, args: list[str]) -> tuple[int, dict, str]:
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
        proc.stdin.write(json.dumps(_initialize_request()) + "\n")
        proc.stdin.close()
        response = _read_json_line(proc.stdout)
        rc = proc.wait(timeout=10)
        stderr = proc.stderr.read() if proc.stderr else ""
    finally:
        if proc.poll() is None:
            proc.kill()
    return rc, response, stderr


def test_manifest_default_is_canonical_sh_launcher():
    """The override's default must stay the pre-override .sh command."""
    assert _manifest_default_command() == (
        "${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp_server.sh"
    )


@posix_only
def test_manifest_command_resolves_to_executable_launcher():
    command, _args = _resolved_manifest_command()
    assert command.is_file()
    mode = command.stat().st_mode
    assert mode & stat.S_IXUSR, f"launcher is not user-executable: {command}"


@posix_only
@requires_mcp
def test_manifest_spawn_returns_valid_initialize_response():
    command, args = _resolved_manifest_command()
    rc, response, stderr = _spawn_initialize(command, args)
    assert rc == 0, f"server exited {rc}; stderr={stderr!r}; response={response!r}"
    result = response["result"]
    assert result["serverInfo"]["name"] == "plan-ops"
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]


@posix_only
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


# --------------------------------------------------------------------------
# Windows launcher (${PLAN_OPS_LAUNCHER} override target).
# --------------------------------------------------------------------------


def test_windows_launcher_ships_next_to_sh():
    """The .cmd override target is part of the plugin on every platform."""
    assert WINDOWS_LAUNCHER.is_file()


@windows_only
def test_windows_launcher_uses_repo_venv_interpreter(tmp_path):
    probe = tmp_path / "print_executable.py"
    probe.write_text(
        "import sys\nprint(sys.executable)\n",
        encoding="utf-8",
    )
    env = {k: v for k, v in os.environ.items() if k != "IMPLEMENT_PLAN_PYTHON"}
    cp = subprocess.run(
        [str(WINDOWS_LAUNCHER), str(probe)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env=env,
    )
    assert Path(cp.stdout.strip()).resolve() == WINDOWS_VENV_PYTHON.resolve()


@windows_only
def test_windows_launcher_honors_implement_plan_python(tmp_path):
    probe = tmp_path / "print_executable.py"
    probe.write_text(
        "import sys\nprint(sys.executable)\n",
        encoding="utf-8",
    )
    env = {**os.environ, "IMPLEMENT_PLAN_PYTHON": sys.executable}
    cp = subprocess.run(
        [str(WINDOWS_LAUNCHER), str(probe)],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
        env=env,
    )
    assert Path(cp.stdout.strip()).resolve() == Path(sys.executable).resolve()


@windows_only
@requires_mcp
def test_windows_launcher_spawn_returns_valid_initialize_response():
    args = [_expand_plugin_root(arg) for arg in _manifest_entry()["args"]]
    rc, response, stderr = _spawn_initialize(WINDOWS_LAUNCHER, args)
    assert rc == 0, f"server exited {rc}; stderr={stderr!r}; response={response!r}"
    result = response["result"]
    assert result["serverInfo"]["name"] == "plan-ops"
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]
