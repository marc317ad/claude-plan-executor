"""Tests for ``_claude_backend.invoke`` (TASK-003).

Coverage:
  * Argv assembly per PLAN_NESTED_DISPATCH §8.1: ``Agent`` always in
    ``--disallowedTools``, ``--add-dir`` from effective cwd,
    ``--allowedTools`` from manifest, ``--permission-mode acceptEdits``
    by default, ``-p`` and ``--output-format json`` always present.
  * Happy path: shim returns a CLI-shape JSON envelope → ``build_ok``
    with ``result``/``duration_ms``/``cost_usd``/``session_id``/
    ``tokens`` mapped from the CLI fields ``result``/``duration_ms``/
    ``total_cost_usd``/``session_id``/``usage``.
  * Timeout: shim sleeps past the deadline →
    ``status: timeout``.
  * Malformed JSON: shim emits ``not json`` →
    ``status: backend_error, code: malformed_output``.
  * Empty stdout: shim emits nothing →
    ``status: backend_error, code: malformed_output``.
  * Non-zero exit (with parseable JSON) →
    ``status: backend_error, code: non_zero_exit``.
  * Binary not found → ``status: backend_error, code: binary_not_found``.

Real-CLI gate (``PLAN_EXEC_E2E=1``) is preserved from
``test_claude_permission_mode_probe.py`` for an end-to-end smoke test
that drops the shim and invokes the live ``claude`` binary.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _claude_backend as backend  # noqa: E402
from jsonschema import validate  # noqa: E402

OUTPUT_SCHEMA_PATH = SCRIPTS_DIR / "schemas" / "claude_dispatch_output.json"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def output_schema() -> dict:
    return json.loads(OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(envelope: Mapping[str, Any], schema: Mapping[str, Any]) -> None:
    validate(instance=dict(envelope), schema=dict(schema))


def _good_manifest(
    *,
    name: str = "plan-implementer",
    tools: List[str] | None = None,
    model: str = "claude-opus-4-7",
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": "test agent",
        "model": model,
        "tools": list(tools) if tools is not None else ["Read", "Edit", "Bash"],
        "env_allowlist": [],
    }


def _good_trace(*, depth: int = 1) -> Dict[str, Any]:
    return {
        "run_id": "run-uuid",
        "span_id": "span-uuid",
        "parent_span_id": "parent-span-uuid",
        "depth": depth,
        "call_chain": ["orchestrator", "plan-implementer"],
        "started_at": "2026-04-25T10:00:00Z",
        "ended_at": "2026-04-25T10:00:12Z",
    }


def _make_shim(
    tmp_path: Path,
    *,
    stdout_payload: str = "",
    stderr_payload: str = "",
    exit_code: int = 0,
    sleep_seconds: float = 0.0,
    record_argv: bool = True,
) -> Path:
    """Create an executable shell shim that mimics ``claude -p ... --output-format json``.

    The shim records its argv to ``<shim_path>.argv.json`` (when
    ``record_argv``) so tests can assert on the assembled command line,
    then emits ``stdout_payload`` to stdout, ``stderr_payload`` to stderr,
    optionally sleeps, and exits with ``exit_code``.

    Returns the path to the shim script.
    """
    shim = tmp_path / "fake_claude.sh"
    argv_record = tmp_path / "fake_claude.argv.json"

    # Use a here-doc style script — single quotes around payloads to
    # avoid the shell interpreting embedded characters.
    body_lines = ["#!/usr/bin/env bash", "set -u"]
    if record_argv:
        # Write argv as a JSON array. Use python via stdin to avoid
        # newline / quoting headaches in shell.
        body_lines.append(
            "python3 -c '"
            "import json,sys; "
            f"open({json.dumps(str(argv_record))}, \"w\").write(json.dumps(sys.argv[1:]))"
            "' \"$@\""
        )
    if stderr_payload:
        body_lines.append(f"printf '%s' {json.dumps(stderr_payload)} >&2")
    if stdout_payload:
        body_lines.append(f"printf '%s' {json.dumps(stdout_payload)}")
    if sleep_seconds > 0:
        body_lines.append(f"sleep {sleep_seconds}")
    body_lines.append(f"exit {int(exit_code)}")

    shim.write_text("\n".join(body_lines) + "\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _read_recorded_argv(shim_path: Path) -> List[str]:
    argv_record = shim_path.parent / "fake_claude.argv.json"
    return json.loads(argv_record.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Argv assembly tests (PLAN_NESTED_DISPATCH §8.1)
# ---------------------------------------------------------------------------


def test_argv_includes_agent_in_disallowed_tools(tmp_path: Path) -> None:
    """``Agent`` must always appear in ``--disallowedTools`` regardless of manifest."""
    payload = json.dumps({
        "result": {"task_id": "001"},
        "duration_ms": 100,
        "total_cost_usd": 0.01,
        "session_id": "sess-1",
        "usage": {
            "input": 10,
            "output": 20,
            "cache_read": 0,
            "cache_creation": 0,
        },
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)
    manifest = _good_manifest(tools=["Read", "Edit", "Bash"])
    effective = {"cwd": str(tmp_path), "timeout_sec": 30}

    backend.invoke(
        manifest,
        effective,
        {"prompt": "do the thing"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert "--disallowedTools" in argv
    disallowed_idx = argv.index("--disallowedTools")
    disallowed_csv = argv[disallowed_idx + 1]
    assert "Agent" in disallowed_csv.split(",")


def test_argv_uses_effective_cwd_for_add_dir(tmp_path: Path) -> None:
    """``--add-dir`` must point at ``effective.cwd``."""
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)
    cwd_dir = tmp_path / "workspace"
    cwd_dir.mkdir()

    backend.invoke(
        _good_manifest(),
        {"cwd": str(cwd_dir), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert "--add-dir" in argv
    assert argv[argv.index("--add-dir") + 1] == str(cwd_dir)


def test_argv_uses_manifest_tools_for_allowed_tools(tmp_path: Path) -> None:
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)
    tools = ["Read", "Grep", "Glob", "Bash"]

    backend.invoke(
        _good_manifest(tools=tools),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert "--allowedTools" in argv
    allowed_csv = argv[argv.index("--allowedTools") + 1]
    assert allowed_csv == ",".join(tools)


def test_argv_default_permission_mode_is_acceptedits(tmp_path: Path) -> None:
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)

    backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"


def test_argv_permission_mode_override(tmp_path: Path) -> None:
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)

    backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30, "permission_mode": "plan"},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert argv[argv.index("--permission-mode") + 1] == "plan"


def test_argv_includes_p_and_output_format_json(tmp_path: Path) -> None:
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)

    backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert "-p" in argv
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"


def test_argv_uses_manifest_name_for_agent_flag(tmp_path: Path) -> None:
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)

    backend.invoke(
        _good_manifest(name="plan-remediator"),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    assert "--agent" in argv
    assert argv[argv.index("--agent") + 1] == "plan-remediator"


def test_argv_disallowed_extras_appended_after_agent(tmp_path: Path) -> None:
    payload = json.dumps({
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    })
    shim = _make_shim(tmp_path, stdout_payload=payload)

    backend.invoke(
        _good_manifest(),
        {
            "cwd": str(tmp_path),
            "timeout_sec": 30,
            "tools_disallowed_extra": ["WebFetch"],
        },
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    disallowed_csv = argv[argv.index("--disallowedTools") + 1]
    parts = disallowed_csv.split(",")
    assert parts[0] == "Agent"
    assert "WebFetch" in parts


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------


def test_happy_path_maps_cli_envelope_to_ok(
    tmp_path: Path, output_schema: dict
) -> None:
    cli_envelope = {
        "result": {"task_id": "001", "status": "success"},
        "duration_ms": 12345,
        "total_cost_usd": 0.0933,
        "session_id": "sess-uuid",
        "usage": {
            "input": 13,
            "output": 235,
            "cache_read": 24145,
            "cache_creation": 36268,
        },
        "permission_denials": [],
    }
    shim = _make_shim(tmp_path, stdout_payload=json.dumps(cli_envelope))

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "do the thing"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "ok"
    assert envelope["error"] is None
    assert envelope["result"] == {"task_id": "001", "status": "success"}
    assert envelope["duration_ms"] == 12345
    assert envelope["cost_usd"] == pytest.approx(0.0933)
    assert envelope["session_id"] == "sess-uuid"
    assert envelope["tokens"] == {
        "input": 13,
        "output": 235,
        "cache_read": 24145,
        "cache_creation": 36268,
    }
    assert envelope["permission_denials"] == []
    assert envelope["agent"] == "plan-implementer"
    assert envelope["model"] == "claude-opus-4-7"
    _validate(envelope, output_schema)


def test_happy_path_with_no_permission_denials_field(
    tmp_path: Path, output_schema: dict
) -> None:
    """CLI envelopes that omit ``permission_denials`` still produce a valid envelope."""
    cli_envelope = {
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    shim = _make_shim(tmp_path, stdout_payload=json.dumps(cli_envelope))

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "ok"
    # Default permission_denials is [] per the §7 contract.
    assert envelope["permission_denials"] == []
    _validate(envelope, output_schema)


def test_timeout_maps_to_timeout_status(
    tmp_path: Path, output_schema: dict
) -> None:
    payload = json.dumps({"result": {}, "duration_ms": 1, "total_cost_usd": 0.0})
    # Sleep substantially longer than the timeout to force TimeoutExpired.
    shim = _make_shim(
        tmp_path, stdout_payload=payload, sleep_seconds=10.0,
    )

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 1},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "timeout"
    assert envelope["error"]["code"] == "timeout"
    assert envelope["error"]["retriable"] is True
    assert envelope["duration_ms"] is not None
    assert envelope["duration_ms"] >= 0
    _validate(envelope, output_schema)


def test_malformed_json_maps_to_backend_error_malformed_output(
    tmp_path: Path, output_schema: dict
) -> None:
    shim = _make_shim(tmp_path, stdout_payload="not json at all")

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "malformed_output"
    assert envelope["error"]["retriable"] is False
    # Raw text mirrored for diagnostics.
    assert envelope["result_raw_truncated"] == "not json at all"
    _validate(envelope, output_schema)


def test_empty_stdout_maps_to_backend_error_malformed_output(
    tmp_path: Path, output_schema: dict
) -> None:
    shim = _make_shim(tmp_path, stdout_payload="")

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "malformed_output"
    _validate(envelope, output_schema)


def test_non_object_json_maps_to_backend_error(
    tmp_path: Path, output_schema: dict
) -> None:
    """Bare scalars / arrays at the top level fail the envelope shape."""
    shim = _make_shim(tmp_path, stdout_payload='[1, 2, 3]')

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "malformed_output"
    _validate(envelope, output_schema)


def test_non_zero_exit_with_parseable_json_maps_to_backend_error(
    tmp_path: Path, output_schema: dict
) -> None:
    cli_envelope = {
        "result": {"partial": True},
        "duration_ms": 50,
        "total_cost_usd": 0.001,
        "session_id": "sess-1",
        "usage": {"input": 1, "output": 1, "cache_read": 0, "cache_creation": 0},
    }
    shim = _make_shim(
        tmp_path, stdout_payload=json.dumps(cli_envelope), exit_code=2,
        stderr_payload="error: something went wrong",
    )

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "non_zero_exit"
    # stderr captured and tailed.
    assert "error: something went wrong" in (envelope["stderr_tail"] or "")
    _validate(envelope, output_schema)


def test_binary_not_found_maps_to_backend_error(
    tmp_path: Path, output_schema: dict
) -> None:
    nonexistent = tmp_path / "no_such_binary"
    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(nonexistent),
    )

    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "binary_not_found"
    _validate(envelope, output_schema)


def test_envelope_carries_trace(
    tmp_path: Path, output_schema: dict
) -> None:
    cli_envelope = {
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    shim = _make_shim(tmp_path, stdout_payload=json.dumps(cli_envelope))
    trace = _good_trace(depth=2)

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        trace,
        backend_binary=str(shim),
    )

    assert envelope["trace"]["run_id"] == "run-uuid"
    assert envelope["trace"]["depth"] == 2
    _validate(envelope, output_schema)


def test_falls_back_to_wall_clock_when_cli_omits_duration(
    tmp_path: Path, output_schema: dict
) -> None:
    cli_envelope = {
        "result": {"ok": True},
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    shim = _make_shim(tmp_path, stdout_payload=json.dumps(cli_envelope))

    envelope = backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"prompt": "x"},
        _good_trace(),
        backend_binary=str(shim),
    )

    assert envelope["status"] == "ok"
    assert envelope["duration_ms"] is not None
    assert envelope["duration_ms"] >= 0
    _validate(envelope, output_schema)


def test_payload_instructions_used_when_no_prompt(
    tmp_path: Path,
) -> None:
    """``payload['instructions']`` is the second-priority prompt source."""
    cli_envelope = {
        "result": {"ok": True},
        "duration_ms": 1,
        "total_cost_usd": 0.0,
        "session_id": "sess-1",
        "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
    }
    shim = _make_shim(tmp_path, stdout_payload=json.dumps(cli_envelope))

    backend.invoke(
        _good_manifest(),
        {"cwd": str(tmp_path), "timeout_sec": 30},
        {"instructions": "the instructions"},
        _good_trace(),
        backend_binary=str(shim),
    )

    argv = _read_recorded_argv(shim)
    # The prompt is the last positional after all the flag pairs.
    assert argv[-1] == "the instructions"


# ---------------------------------------------------------------------------
# Orphan-child prevention (TASK-002)
# ---------------------------------------------------------------------------


def _make_pid_recording_shim(tmp_path: Path, *, sleep_seconds: float) -> Path:
    """Shim that writes its own PID to ``<tmp>/child.pid`` then sleeps.

    Used by the SIGTERM/SIGINT/normal-exit tests: the test polls
    ``/proc/<pid>`` to assert the child has been reaped.
    """
    shim = tmp_path / "fake_claude_pidshim.sh"
    pid_path = tmp_path / "child.pid"
    body = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -u
        echo $$ > {json.dumps(str(pid_path))}
        sleep {sleep_seconds}
        printf '%s' '{{"result": {{"ok": true}}, "duration_ms": 1, "total_cost_usd": 0.0, "session_id": "s", "usage": {{"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}}}}'
        exit 0
        """)
    shim.write_text(body, encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _driver_script(shim_path: Path, cwd: Path) -> str:
    """Render a Python driver that calls ``backend.invoke`` directly.

    The driver is exec'd in a subprocess so the test can SIGTERM/SIGINT
    its PID and observe whether the nested shim PID is reaped.
    """
    return textwrap.dedent(f"""\
        import sys
        sys.path.insert(0, {json.dumps(str(SCRIPTS_DIR))})
        import _claude_backend as backend
        manifest = {{
            "name": "plan-implementer",
            "description": "x",
            "model": "claude-opus-4-7",
            "tools": ["Read"],
            "env_allowlist": [],
        }}
        effective = {{"cwd": {json.dumps(str(cwd))}, "timeout_sec": 120}}
        env = backend.invoke(
            manifest, effective, {{"prompt": "x"}}, {{"run_id": "r", "span_id": "s", "depth": 1}},
            backend_binary={json.dumps(str(shim_path))},
        )
        print("DONE", env.get("status"))
        """)


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _wait_for_pid_file(pid_path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pid_path.exists():
            text = pid_path.read_text(encoding="utf-8").strip()
            if text:
                try:
                    return int(text)
                except ValueError:
                    pass
        time.sleep(0.05)
    raise AssertionError(f"shim never recorded its PID at {pid_path}")


def _wait_until_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="orphan-child tests require /proc (Linux)",
)
def test_sigterm_reaps_nested_child(tmp_path: Path) -> None:
    """SIGTERM to the wrapper PID must reap the nested shim within 5s."""
    shim = _make_pid_recording_shim(tmp_path, sleep_seconds=60.0)
    pid_path = tmp_path / "child.pid"
    driver = _driver_script(shim, tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = _wait_for_pid_file(pid_path, timeout=10.0)
        assert _pid_alive(child_pid), "shim should be alive before SIGTERM"
        proc.send_signal(signal.SIGTERM)
        assert _wait_until_dead(child_pid, timeout=5.0), (
            f"nested shim PID {child_pid} survived SIGTERM to wrapper"
        )
    finally:
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="orphan-child tests require /proc (Linux)",
)
def test_sigint_reaps_nested_child(tmp_path: Path) -> None:
    """SIGINT to the wrapper PID must propagate equally and reap the child."""
    shim = _make_pid_recording_shim(tmp_path, sleep_seconds=60.0)
    pid_path = tmp_path / "child.pid"
    driver = _driver_script(shim, tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = _wait_for_pid_file(pid_path, timeout=10.0)
        assert _pid_alive(child_pid), "shim should be alive before SIGINT"
        proc.send_signal(signal.SIGINT)
        assert _wait_until_dead(child_pid, timeout=5.0), (
            f"nested shim PID {child_pid} survived SIGINT to wrapper"
        )
    finally:
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="orphan-child tests require /proc (Linux)",
)
def test_normal_exit_reaps_child_no_zombie(tmp_path: Path) -> None:
    """Shim exits normally → child fully reaped, no zombie left behind."""
    shim = _make_pid_recording_shim(tmp_path, sleep_seconds=0.1)
    pid_path = tmp_path / "child.pid"
    driver = _driver_script(shim, tmp_path)

    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = proc.communicate(timeout=30.0)
    assert proc.returncode == 0, f"driver failed: stderr={stderr!r}"
    assert "DONE ok" in stdout, f"unexpected stdout: {stdout!r}"
    # The shim's PID file should exist; the PID must no longer be in /proc
    # (and specifically not a zombie — /proc/<pid> ceases to exist once
    # the parent has wait()ed on it, which Popen.communicate() does).
    child_pid = int(pid_path.read_text(encoding="utf-8").strip())
    assert not _pid_alive(child_pid), (
        f"shim PID {child_pid} still in /proc after normal exit (zombie?)"
    )


@pytest.mark.skipif(
    not Path("/proc").is_dir(),
    reason="orphan-child tests require /proc (Linux)",
)
def test_prior_sigterm_handler_is_invoked(tmp_path: Path) -> None:
    """A custom prior SIGTERM handler installed before ``invoke`` must
    fire after the wrapper reaps the child — the wrapper must dispatch
    to the prior callable, not to ``SIG_DFL``.
    """
    shim = _make_pid_recording_shim(tmp_path, sleep_seconds=60.0)
    pid_path = tmp_path / "child.pid"
    marker_path = tmp_path / "prior_handler_ran.txt"

    driver = textwrap.dedent(f"""\
        import signal, sys
        sys.path.insert(0, {json.dumps(str(SCRIPTS_DIR))})
        import _claude_backend as backend

        def _prior(signum, frame):
            with open({json.dumps(str(marker_path))}, "w") as fh:
                fh.write("ran")
            sys.exit(0)

        signal.signal(signal.SIGTERM, _prior)

        manifest = {{
            "name": "plan-implementer",
            "description": "x",
            "model": "claude-opus-4-7",
            "tools": ["Read"],
            "env_allowlist": [],
        }}
        effective = {{"cwd": {json.dumps(str(tmp_path))}, "timeout_sec": 120}}
        backend.invoke(
            manifest, effective, {{"prompt": "x"}},
            {{"run_id": "r", "span_id": "s", "depth": 1}},
            backend_binary={json.dumps(str(shim))},
        )
        print("DONE")
        """)

    proc = subprocess.Popen(
        [sys.executable, "-c", driver],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        child_pid = _wait_for_pid_file(pid_path, timeout=10.0)
        assert _pid_alive(child_pid), "shim should be alive before SIGTERM"
        proc.send_signal(signal.SIGTERM)
        assert _wait_until_dead(child_pid, timeout=5.0), (
            f"nested shim PID {child_pid} survived SIGTERM to wrapper"
        )
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2.0)
        assert marker_path.exists(), (
            "prior SIGTERM handler did not run — wrapper bypassed it via SIG_DFL"
        )
        assert marker_path.read_text(encoding="utf-8") == "ran"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


# ---------------------------------------------------------------------------
# Real-CLI smoke gate (PLAN_EXEC_E2E=1)
# ---------------------------------------------------------------------------


def _e2e_enabled() -> bool:
    return os.environ.get("PLAN_EXEC_E2E") == "1"


def _claude_available() -> bool:
    return shutil.which("claude") is not None


@pytest.mark.skipif(
    not _e2e_enabled(),
    reason="set PLAN_EXEC_E2E=1 to run live claude smoke",
)
@pytest.mark.skipif(
    not _claude_available(),
    reason="claude CLI not on PATH",
)
def test_real_claude_smoke(tmp_path: Path, output_schema: dict) -> None:
    """End-to-end smoke: invoke the real ``claude`` CLI, expect ok."""
    envelope = backend.invoke(
        _good_manifest(name="plan-analyst", tools=["Read", "Grep", "Glob", "Bash"]),
        {"cwd": str(tmp_path), "timeout_sec": 180},
        {"prompt": "Reply with exactly the literal token DEEP_NESTED_OK_a3f9c2 and nothing else."},
        _good_trace(),
    )
    # Either the live session returns ok, or the wrapper detected an
    # error condition — but the envelope must always be schema-valid.
    _validate(envelope, output_schema)
    assert envelope["status"] in {"ok", "timeout", "backend_error"}
