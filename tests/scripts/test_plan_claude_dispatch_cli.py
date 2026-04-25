"""Tests for ``plan_claude_dispatch.py`` CLI entrypoint (TASK-005).

Coverage:
  * ``run`` happy path: backend.invoke is monkeypatched to return a
    canned ``build_ok`` envelope; wrapper validates input + envelope,
    appends a span, and exits 0.
  * Malformed input → exit 2 + ``status: input_invalid`` envelope on
    stdout (the §10 acceptance criterion).
  * ``--dry-run`` returns a plan-only envelope with ``status: ok`` and
    ``status_reason: 'dry_run: true'`` and never invokes the backend.
  * Unknown / undispatchable agent → exit 1 + ``status: input_invalid``
    envelope (``code: agent_not_dispatchable``).
  * Manifest load failure → exit 1 + ``status: manifest_invalid``.
  * Preflight denial (killswitch) → exit 1 + ``status: denied``.
  * Schema-invalid backend output → exit 1 + ``status: schema_invalid``.
  * stdin/stdout pipe round-trip: ``--input -`` reads from stdin,
    ``--output -`` writes to stdout (also the default).
  * Subcommands per §10 are wired (``run``, ``list-agents``,
    ``show-agent``, ``validate-input``, ``validate-output``).
  * Exit code mapping per §10.

Heavy use of monkeypatching against ``_claude_backend.invoke`` so no
real ``claude`` subprocess ever runs. The end-to-end shim test is
TASK-008's responsibility.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _claude_agent_manifest as agent_manifest  # noqa: E402
import _claude_backend as backend_mod  # noqa: E402
import _claude_dispatch_cleanup as cleanup_mod  # noqa: E402
import _claude_dispatch_envelope as env_mod  # noqa: E402
import _claude_guardrails as guardrails  # noqa: E402

import plan_claude_dispatch as cli  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_span_log(monkeypatch, tmp_path):
    """Redirect span-log writes to a tmp dir for every test.

    The wrapper's v1 placeholder appends to ``docs/plans/spans.jsonl``;
    we don't want test runs polluting the real path. ``monkeypatch.setenv``
    of ``PLAN_EXEC_LOG_DIR`` redirects the resolver in the wrapper.
    """
    monkeypatch.setenv("PLAN_EXEC_LOG_DIR", str(tmp_path))
    yield


@pytest.fixture(autouse=True)
def _reset_killswitch(monkeypatch):
    """Make sure no ambient killswitch from the host env trips guardrails."""
    monkeypatch.delenv(guardrails.KILLSWITCH_ENV, raising=False)
    monkeypatch.delenv("PLAN_EXEC_MAX_DEPTH", raising=False)
    monkeypatch.delenv("PLAN_EXEC_COST_CAP_USD", raising=False)
    monkeypatch.delenv("PLAN_EXEC_COST_SO_FAR_USD", raising=False)
    yield


@pytest.fixture
def good_input_obj() -> Dict[str, Any]:
    """A §6-shaped input dict that passes input-schema validation."""
    return {
        "schema_version": 1,
        "agent": "plan-implementer",
        "payload": {"task_id": "001", "instructions": "stub"},
        "output_instructions": {
            "format": "json",
            "schema_path": "plugins/plan-executor/scripts/codex_implement_schema.json",
            "schema_inline": None,
            "max_bytes": 65536,
        },
        "overrides": {
            "model": None,
            "timeout_sec": None,
            "tools_allowed_extra": None,
            "tools_disallowed_extra": None,
            "cwd": None,
        },
        "guardrails": {
            "max_depth": None,
            "cost_cap_usd": None,
            "network": None,
        },
        "trace": {
            "run_id": "run-uuid-1",
            "parent_span_id": None,
            "depth": 0,
            "call_chain": ["orchestrator"],
        },
    }


@pytest.fixture
def fake_manifest() -> Dict[str, Any]:
    return {
        "name": "plan-implementer",
        "description": "stub",
        "model": "claude-opus-4-7",
        "tools": ["Read", "Edit", "Bash"],
        "env_allowlist": ["PATH"],
    }


@pytest.fixture
def patch_manifest(monkeypatch, fake_manifest):
    """Replace ``agent_manifest.load_agent`` with a stub that returns
    ``fake_manifest`` for ``plan-implementer`` and behaves correctly
    for unknown / invalid names."""
    def _load(name, *, agents_dir=None):
        if name not in agent_manifest.DISPATCHABLE_AGENTS:
            raise agent_manifest.AgentNotDispatchable(
                f"agent {name!r} is not dispatchable"
            )
        return dict(fake_manifest)
    monkeypatch.setattr(cli.agent_manifest, "load_agent", _load)
    return _load


@pytest.fixture
def patch_cleanup_noop(monkeypatch):
    """Stub baseline + cleanup so tests don't need a real git repo."""
    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda repo_root: {"captured": False, "repo_root": str(repo_root)},
    )
    monkeypatch.setattr(
        cli.cleanup,
        "apply_cleanup",
        lambda baseline, declared, repo_root: {
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "out_of_scope_paths": [],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": False,
            "cleanup_strategy": "skipped_no_baseline",
        },
    )


def _ok_envelope(**overrides) -> Dict[str, Any]:
    """Build a canned ``status: ok`` envelope for the fake backend."""
    base = env_mod.build_ok(
        agent="plan-implementer",
        model="claude-opus-4-7",
        session_id="sess-uuid",
        duration_ms=42,
        cost_usd=0.001,
        tokens={"input": 10, "output": 20, "cache_read": 0, "cache_creation": 0},
        result={
            "task_id": "001",
            "status": "completed",
            "files_changed": [],
            "summary": "ok",
        },
        result_raw_truncated="{}",
        stderr_tail="",
    )
    base.update(overrides)
    return base


def _patch_backend_returning(monkeypatch, envelope: Dict[str, Any]):
    calls: List[Dict[str, Any]] = []

    def _invoke(manifest, effective, payload, trace, *, backend_binary=None, env=None):
        calls.append({
            "manifest": dict(manifest),
            "effective": dict(effective),
            "payload": dict(payload) if isinstance(payload, Mapping) else payload,
            "trace": dict(trace) if isinstance(trace, Mapping) else trace,
            "backend_binary": backend_binary,
            "env": dict(env) if env else None,
        })
        # Mirror the production backend's behavior: forward the wrapper-
        # supplied trace into the returned envelope so the wrapper sees
        # its own run_id / span_id / parent_span_id round-tripped.
        out = dict(envelope)
        if isinstance(trace, Mapping):
            out["trace"] = dict(trace)
        return out

    monkeypatch.setattr(cli.backend, "invoke", _invoke)
    return calls


def _run_cli(argv, stdin_text=None, capsys=None, monkeypatch=None) -> tuple[int, str, str]:
    """Invoke ``cli.main(argv)``, return ``(exit_code, stdout, stderr)``."""
    if stdin_text is not None and monkeypatch is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _parse_envelope(stdout: str) -> Dict[str, Any]:
    """Parse a JSON envelope from CLI stdout. Tolerates trailing newline."""
    text = stdout.strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_happy_path_emits_ok_envelope(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """The full ``run`` pipeline: input → preflight → backend → cleanup → emit."""
    canned = _ok_envelope()
    calls = _patch_backend_returning(monkeypatch, canned)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")
    output_path = tmp_path / "out.json"

    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", str(output_path),
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 0
    assert output_path.exists()
    envelope = json.loads(output_path.read_text(encoding="utf-8"))
    assert envelope["status"] == "ok"
    assert envelope["agent"] == "plan-implementer"
    assert envelope["model"] == "claude-opus-4-7"
    assert envelope["schema_version"] == 1
    assert envelope["trace"]["run_id"] == "run-uuid-1"
    assert envelope["trace"]["span_id"]  # minted by wrapper
    # backend was invoked exactly once
    assert len(calls) == 1
    # backend received the merged effective dict and the trace
    assert calls[0]["trace"]["run_id"] == "run-uuid-1"


def test_run_happy_path_writes_span_log(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """Span log is appended on success (one JSONL line per dispatch)."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 0
    span_log = tmp_path / "spans.jsonl"
    assert span_log.exists()
    lines = [ln for ln in span_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    span = json.loads(lines[0])
    assert span["status"] == "ok"


# ---------------------------------------------------------------------------
# Malformed input — §10 acceptance criterion
# ---------------------------------------------------------------------------


def test_run_malformed_json_emits_input_invalid_envelope_exit2(
    tmp_path, capsys,
):
    """Malformed input → exit 2 + ``status: input_invalid`` envelope on stdout."""
    bad = tmp_path / "bad.json"
    bad.write_text("not json {", encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(bad), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 2
    envelope = _parse_envelope(out)
    assert envelope["status"] == "input_invalid"
    assert envelope["error"]["code"] == "input_invalid"
    assert "not valid JSON" in envelope["error"]["message"]


def test_run_input_schema_violation_exit2_with_envelope(
    tmp_path, good_input_obj, capsys,
):
    """Input that parses as JSON but fails schema → exit 2 + input_invalid."""
    bad = dict(good_input_obj)
    bad["schema_version"] = 99  # const: 1
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 2
    envelope = _parse_envelope(out)
    assert envelope["status"] == "input_invalid"
    assert "schema validation" in envelope["error"]["message"]


def test_run_input_missing_required_field_exit2(
    tmp_path, good_input_obj, capsys,
):
    """Schema requires `agent`; absence → input_invalid + exit 2."""
    bad = dict(good_input_obj)
    del bad["agent"]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 2
    envelope = _parse_envelope(out)
    assert envelope["status"] == "input_invalid"


def test_run_io_error_on_input_path_exit2(tmp_path, capsys):
    """Unreadable input path → exit 2 + input_invalid (code: io_error)."""
    code, out, _err = _run_cli(
        ["run", "--input", str(tmp_path / "does_not_exist.json"), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 2
    envelope = _parse_envelope(out)
    assert envelope["status"] == "input_invalid"
    assert envelope["error"]["code"] == "io_error"


# ---------------------------------------------------------------------------
# --dry-run — §10 acceptance criterion
# ---------------------------------------------------------------------------


def test_run_dry_run_returns_plan_envelope_no_spawn(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """``--dry-run`` returns ok + status_reason 'dry_run: true' and never spawns."""
    calls: List[int] = []

    def _spy(*a, **kw):
        calls.append(1)
        raise AssertionError("backend.invoke must not be called under --dry-run")

    monkeypatch.setattr(cli.backend, "invoke", _spy)
    monkeypatch.setattr(cli.cleanup, "snapshot_baseline", lambda r: (_ for _ in ()).throw(
        AssertionError("baseline must not run under --dry-run"),
    ))

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-", "--dry-run",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 0
    assert calls == []
    envelope = _parse_envelope(out)
    assert envelope["status"] == "ok"
    assert envelope["status_reason"] == "dry_run: true"
    # The plan dict appears in result with dry_run:true.
    assert envelope["result"]["dry_run"] is True
    assert envelope["result"]["agent"] == "plan-implementer"


# ---------------------------------------------------------------------------
# Unknown agent / manifest invalid
# ---------------------------------------------------------------------------


def test_run_unknown_agent_emits_input_invalid_with_code(
    tmp_path, good_input_obj, monkeypatch, capsys,
):
    """An agent outside the dispatchable allowlist → input_invalid w/ code agent_not_dispatchable."""
    bad = dict(good_input_obj)
    bad["agent"] = "definitely-not-an-agent"
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    # The wrapper input schema accepts any agent string ≥1 char; the
    # rejection happens at load_agent (or guardrails) — either way the
    # envelope is input_invalid and the exit is 1.
    envelope = _parse_envelope(out)
    assert envelope["status"] == "input_invalid"
    assert code == 1


def test_run_manifest_schema_invalid_emits_manifest_invalid(
    tmp_path, good_input_obj, monkeypatch, capsys,
):
    """A manifest whose YAML is malformed → exit 1 + manifest_invalid envelope."""
    def _bad_load(name, *, agents_dir=None):
        raise agent_manifest.ManifestSchemaInvalid(
            "frontmatter YAML parse error in plan-implementer.md"
        )

    monkeypatch.setattr(cli.agent_manifest, "load_agent", _bad_load)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "manifest_invalid"
    assert "YAML parse error" in envelope["error"]["message"]


# ---------------------------------------------------------------------------
# Preflight denial
# ---------------------------------------------------------------------------


def test_run_killswitch_emits_denied_envelope_exit1(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """``PLAN_EXEC_CLAUDE_DISPATCH_KILLSWITCH=1`` → status: denied, code: killswitch."""
    monkeypatch.setenv(guardrails.KILLSWITCH_ENV, "1")
    # Backend MUST NOT be called when preflight denies.
    monkeypatch.setattr(
        cli.backend,
        "invoke",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("backend.invoke must not run after preflight denial"),
        ),
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "denied"
    assert envelope["error"]["code"] == "killswitch"


def test_run_depth_exceeded_emits_correct_envelope(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """``trace.depth >= max_depth`` → status: depth_exceeded."""
    bad = dict(good_input_obj)
    bad["trace"] = dict(good_input_obj["trace"], depth=5)
    bad["guardrails"] = dict(good_input_obj["guardrails"], max_depth=2)

    monkeypatch.setattr(
        cli.backend,
        "invoke",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not spawn")),
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "depth_exceeded"


# ---------------------------------------------------------------------------
# Schema-invalid backend output
# ---------------------------------------------------------------------------


def test_run_backend_returns_schema_invalid_envelope_passthrough(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """If the backend itself emits ``status: schema_invalid``, the wrapper
    passes it through (after stamping trace.ended_at)."""
    bad_backend = env_mod.build_schema_invalid(
        message="inner result missing 'task_id'",
        agent="plan-implementer",
        model="claude-opus-4-7",
    )
    _patch_backend_returning(monkeypatch, bad_backend)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "schema_invalid"


def test_run_backend_returns_backend_error_passthrough(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """Backend ``backend_error`` flows through to exit 1."""
    bad = env_mod.build_backend_error(
        code="malformed_output",
        message="backend stdout is not JSON",
        agent="plan-implementer",
        model="claude-opus-4-7",
    )
    _patch_backend_returning(monkeypatch, bad)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "malformed_output"


# ---------------------------------------------------------------------------
# Scope violation from cleanup demotes ok → scope_violation
# ---------------------------------------------------------------------------


def test_run_cleanup_violation_demotes_ok_to_scope_violation(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """If cleanup detects out-of-scope writes, an ok backend envelope is
    demoted to ``scope_violation`` at the wrapper boundary."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )
    monkeypatch.setattr(
        cli.cleanup,
        "apply_cleanup",
        lambda baseline, declared, repo_root: {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": ["evil.txt"],
            "deleted": [],
            "out_of_scope_paths": ["evil.txt"],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        },
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "scope_violation"
    assert envelope["scope"]["scope_violation_detected"] is True
    assert "evil.txt" in envelope["scope"]["observed_delta_tracked"]


# ---------------------------------------------------------------------------
# TASK-004: cleanup_failure takes precedence over backend success
# ---------------------------------------------------------------------------


def test_run_cleanup_failed_paths_emits_cleanup_failure(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """If ``apply_cleanup`` returns non-empty ``failed_paths``, the wrapper
    emits ``status: cleanup_failure`` even when the backend produced
    ``ok``. The working tree is in an unknown state — this is a hard
    fail."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )
    monkeypatch.setattr(
        cli.cleanup,
        "apply_cleanup",
        lambda baseline, declared, repo_root: {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "failed_paths": ["readonly.txt"],
            "out_of_scope_paths": ["readonly.txt"],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        },
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "cleanup_failure"
    assert envelope["error"]["code"] == "cleanup_failure"
    assert "readonly.txt" in envelope["error"]["message"]


def test_run_cleanup_mixed_failed_and_restored_emits_cleanup_failure(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """Mixed outcome (some restored, some failed) → ``cleanup_failure``
    takes precedence and the envelope's diagnostics still carry both
    lists in the scope block."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )
    monkeypatch.setattr(
        cli.cleanup,
        "apply_cleanup",
        lambda baseline, declared, repo_root: {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": ["was_dirty.py"],
            "deleted": ["new.txt"],
            "failed_paths": ["readonly.txt"],
            "out_of_scope_paths": [
                "was_dirty.py", "new.txt", "readonly.txt",
            ],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        },
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "cleanup_failure"
    # Successful reverts still surface in the scope block; the failed
    # path is exposed as a STRUCTURED scope field (TASK-004 hardening
    # remediation: callers must not have to grep ``error.message`` for
    # diagnostics).
    assert "was_dirty.py" in envelope["scope"]["observed_delta_tracked"]
    assert "new.txt" in envelope["scope"]["observed_delta_untracked"]
    assert envelope["scope"]["failed_paths"] == ["readonly.txt"]
    # Message keeps the path summary too (human-readable diagnostic).
    assert "readonly.txt" in envelope["error"]["message"]


def test_run_cleanup_failure_takes_precedence_over_scope_violation(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """``cleanup_failure`` outranks ``scope_violation``: even when both
    a violation was detected and a path failed to revert, the envelope
    emits ``cleanup_failure`` (the working-tree-unknown signal is the
    more important one)."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )
    monkeypatch.setattr(
        cli.cleanup,
        "apply_cleanup",
        lambda baseline, declared, repo_root: {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "failed_paths": ["x.txt"],
            "out_of_scope_paths": ["x.txt"],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        },
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "cleanup_failure"


def test_run_cleanup_failure_overrides_non_ok_backend_status(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """TASK-004 hardening remediation: ``failed_paths`` non-empty must
    surface as ``cleanup_failure`` regardless of the backend envelope's
    pre-cleanup status (except for backend-never-produced-result
    statuses like ``timeout`` / ``backend_error`` / ``schema_invalid``,
    which are preserved). A backend ``scope_violation`` was previously
    a hidden cleanup-OSError victim — this regression locks in the new
    precedence."""
    canned = env_mod.build_scope_violation(
        message="agent strayed",
        scope=env_mod._empty_scope(),
        agent="plan-implementer",
        model="claude-opus-4-7",
    )
    _patch_backend_returning(monkeypatch, canned)

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )
    monkeypatch.setattr(
        cli.cleanup,
        "apply_cleanup",
        lambda baseline, declared, repo_root: {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "failed_paths": ["readonly.txt"],
            "out_of_scope_paths": ["readonly.txt"],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        },
    )

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 1
    envelope = _parse_envelope(out)
    # Backend produced ``scope_violation``; cleanup failure must
    # override and the structured ``failed_paths`` must surface.
    assert envelope["status"] == "cleanup_failure"
    assert envelope["scope"]["failed_paths"] == ["readonly.txt"]


# ---------------------------------------------------------------------------
# TASK-001: trusted cleanup-scope source (sandbox-escape fix)
# ---------------------------------------------------------------------------


def test_run_agent_lies_in_files_changed_does_not_authorize_cleanup(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """Agent claims to have touched ``/etc/passwd`` in ``result.files_changed``
    but actually wrote ``evil.py``. With trusted ``declared_files_changed=[]``
    from the orchestrator, cleanup MUST revert ``evil.py`` and treat the
    agent's lie as non-authoritative."""
    canned = _ok_envelope()
    # Agent lies: declares /etc/passwd in its self-reported envelope.
    canned["result"] = dict(canned["result"], files_changed=["/etc/passwd"])
    _patch_backend_returning(monkeypatch, canned)

    # Spy on apply_cleanup to assert it is invoked with the trusted
    # declared list (which is [] when the input omits it / passes []),
    # NOT with the agent-reported ``["/etc/passwd"]``.
    seen: List[Any] = []

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )

    def _spy_cleanup(baseline, declared, repo_root):
        seen.append(list(declared))
        # Simulate that the agent actually wrote evil.py and cleanup
        # reverts it because it is out-of-scope vs the trusted []
        # declared set.
        return {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": ["evil.py"],
            "out_of_scope_paths": ["evil.py"],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        }

    monkeypatch.setattr(cli.cleanup, "apply_cleanup", _spy_cleanup)

    # Input with declared_files_changed explicitly set to [].
    payload = dict(good_input_obj)
    payload["declared_files_changed"] = []
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "scope_violation"
    # The cleanup module saw the trusted [] declared, not the agent lie.
    assert seen == [[]]
    assert "/etc/passwd" not in seen[0]
    # evil.py was reverted (deleted) and surfaces in the scope block.
    assert "evil.py" in envelope["scope"]["observed_delta_untracked"]


def test_run_overwrite_outside_declared_set_is_reverted(
    tmp_path, good_input_obj, patch_manifest, monkeypatch, capsys,
):
    """With ``declared_files_changed=['a.py']``, agent writes both ``a.py``
    and ``b.py`` → ``b.py`` is reverted and ``scope_violation_detected``
    flips True."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    seen: List[Any] = []

    monkeypatch.setattr(
        cli.cleanup,
        "snapshot_baseline",
        lambda r: {"captured": True, "repo_root": str(r)},
    )

    def _spy_cleanup(baseline, declared, repo_root):
        seen.append(list(declared))
        return {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": ["b.py"],
            "out_of_scope_paths": ["b.py"],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "delta_bounded",
        }

    monkeypatch.setattr(cli.cleanup, "apply_cleanup", _spy_cleanup)

    payload = dict(good_input_obj)
    payload["declared_files_changed"] = ["a.py"]
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 1
    envelope = _parse_envelope(out)
    assert envelope["status"] == "scope_violation"
    assert envelope["scope"]["scope_violation_detected"] is True
    # Cleanup was passed the trusted ['a.py'].
    assert seen == [["a.py"]]
    assert "b.py" in envelope["scope"]["observed_delta_untracked"]


def test_run_omitted_declared_files_changed_defaults_to_empty(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """Omitted top-level field → cleanup is invoked with ``[]``
    (deny-by-default for read-only agents like plan-analyst)."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    seen: List[Any] = []
    real_apply = cli.cleanup.apply_cleanup

    def _spy(baseline, declared, repo_root):
        seen.append(list(declared))
        return real_apply(baseline, declared, repo_root)

    monkeypatch.setattr(cli.cleanup, "apply_cleanup", _spy)

    # good_input_obj does NOT carry declared_files_changed.
    assert "declared_files_changed" not in good_input_obj
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )

    assert code == 0
    assert seen == [[]]


def test_validate_input_accepts_declared_files_changed_field(
    tmp_path, good_input_obj, capsys,
):
    """Backward-compat: payloads with ``declared_files_changed`` set
    are accepted by the input schema; payloads without it are too."""
    payload = dict(good_input_obj)
    payload["declared_files_changed"] = ["foo.py", "bar/baz.py"]
    p = tmp_path / "in.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    code = cli.main(["validate-input", str(p)])
    out = capsys.readouterr().out
    assert code == 0
    assert "ok" in out


# ---------------------------------------------------------------------------
# stdin / stdout pipe round-trip
# ---------------------------------------------------------------------------


def test_run_reads_stdin_and_writes_stdout(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """``--input -`` reads from stdin, ``--output -`` writes to stdout."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    code, out, _err = _run_cli(
        ["run", "--input", "-", "--output", "-",
         "--repo-root", str(tmp_path)],
        stdin_text=json.dumps(good_input_obj),
        capsys=capsys,
        monkeypatch=monkeypatch,
    )

    assert code == 0
    envelope = _parse_envelope(out)
    assert envelope["status"] == "ok"


def test_run_default_output_is_stdout(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """Omitting --output writes to stdout (default)."""
    canned = _ok_envelope()
    _patch_backend_returning(monkeypatch, canned)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, out, _err = _run_cli(
        ["run", "--input", str(input_path), "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 0
    envelope = _parse_envelope(out)
    assert envelope["status"] == "ok"


# ---------------------------------------------------------------------------
# Auxiliary subcommands per §10
# ---------------------------------------------------------------------------


def test_list_agents_prints_dispatchable_set(capsys):
    code = cli.main(["list-agents"])
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert set(parsed) == set(agent_manifest.DISPATCHABLE_AGENTS)


def test_show_agent_prints_manifest_json(monkeypatch, fake_manifest, capsys):
    monkeypatch.setattr(
        cli.agent_manifest,
        "load_agent",
        lambda name, *, agents_dir=None: dict(fake_manifest),
    )
    code = cli.main(["show-agent", "plan-implementer"])
    out = capsys.readouterr().out
    assert code == 0
    parsed = json.loads(out)
    assert parsed["name"] == "plan-implementer"
    assert parsed["model"] == "claude-opus-4-7"


def test_show_agent_unknown_returns_2(monkeypatch, capsys):
    def _bad(name, *, agents_dir=None):
        raise agent_manifest.AgentNotDispatchable("nope")
    monkeypatch.setattr(cli.agent_manifest, "load_agent", _bad)
    code = cli.main(["show-agent", "no-such-agent"])
    err = capsys.readouterr().err
    assert code == 2
    assert "nope" in err


def test_validate_input_ok_returns_0(tmp_path, good_input_obj, capsys):
    p = tmp_path / "in.json"
    p.write_text(json.dumps(good_input_obj), encoding="utf-8")
    code = cli.main(["validate-input", str(p)])
    out = capsys.readouterr().out
    assert code == 0
    assert "ok" in out


def test_validate_input_invalid_returns_1(tmp_path, good_input_obj, capsys):
    bad = dict(good_input_obj)
    bad["schema_version"] = 99
    p = tmp_path / "in.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    code = cli.main(["validate-input", str(p)])
    err = capsys.readouterr().err
    assert code == 1
    assert "input invalid" in err


def test_validate_output_ok_returns_0(tmp_path, capsys):
    envelope = env_mod.build_ok(
        agent="plan-implementer",
        model="claude-opus-4-7",
        result={"task_id": "001"},
    )
    p = tmp_path / "env.json"
    p.write_text(json.dumps(envelope), encoding="utf-8")
    code = cli.main(["validate-output", str(p)])
    out = capsys.readouterr().out
    assert code == 0
    assert "ok" in out


def test_validate_output_invalid_returns_1(tmp_path, capsys):
    envelope = {
        "schema_version": 99,  # violates const:1
        "status": "ok",
    }
    p = tmp_path / "env.json"
    p.write_text(json.dumps(envelope), encoding="utf-8")
    code = cli.main(["validate-output", str(p)])
    err = capsys.readouterr().err
    assert code == 1
    assert "output invalid" in err


def test_validate_input_unparseable_returns_2(tmp_path, capsys):
    p = tmp_path / "in.json"
    p.write_text("not json", encoding="utf-8")
    code = cli.main(["validate-input", str(p)])
    err = capsys.readouterr().err
    assert code == 2
    assert "JSON" in err or "json" in err


# ---------------------------------------------------------------------------
# argparse / subcommand wiring
# ---------------------------------------------------------------------------


def test_main_requires_subcommand(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main([])
    # argparse exits 2 when required positional is missing
    assert exc_info.value.code == 2


def test_run_requires_input_flag(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["run", "--output", "-"])
    assert exc_info.value.code == 2


def test_run_unknown_subcommand_exits(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["nonexistent"])
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# Effective overrides + env scrubbing wiring
# ---------------------------------------------------------------------------


def test_run_passes_timeout_override_to_backend(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """``--timeout`` flag flows into ``effective.timeout_sec``."""
    canned = _ok_envelope()
    calls = _patch_backend_returning(monkeypatch, canned)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--timeout", "55", "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 0
    assert calls[0]["effective"].get("timeout_sec") == 55


def test_run_passes_backend_binary_override(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """``--backend-binary`` flag flows into ``backend.invoke``."""
    canned = _ok_envelope()
    calls = _patch_backend_returning(monkeypatch, canned)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")
    shim = tmp_path / "fake_claude.sh"
    shim.write_text("#!/bin/sh\nexit 0\n")
    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--backend-binary", str(shim), "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 0
    assert calls[0]["backend_binary"] == str(shim)


def test_run_network_deny_appends_web_tools_to_disallowed(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """``guardrails.network: 'deny'`` → ``effective.tools_disallowed_extra``
    grows ``WebFetch`` + ``WebSearch``."""
    bad = dict(good_input_obj)
    bad["guardrails"] = dict(good_input_obj["guardrails"], network="deny")
    canned = _ok_envelope()
    calls = _patch_backend_returning(monkeypatch, canned)

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(bad), encoding="utf-8")

    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 0
    extras = calls[0]["effective"].get("tools_disallowed_extra") or []
    assert "WebFetch" in extras
    assert "WebSearch" in extras


def test_run_invokes_backend_with_scrubbed_env(
    tmp_path, good_input_obj, patch_manifest, patch_cleanup_noop, monkeypatch, capsys,
):
    """The backend receives a sanitised env (PLAN_EXEC_* + manifest allowlist
    + per-hop bookkeeping)."""
    canned = _ok_envelope()
    calls = _patch_backend_returning(monkeypatch, canned)

    monkeypatch.setenv("PLAN_EXEC_RUN_ID", "outer-run")
    monkeypatch.setenv("SOME_SECRET", "should-be-dropped")

    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(good_input_obj), encoding="utf-8")

    code, _out, _err = _run_cli(
        ["run", "--input", str(input_path), "--output", "-",
         "--repo-root", str(tmp_path)],
        capsys=capsys,
    )
    assert code == 0
    env_passed = calls[0]["env"]
    assert env_passed is not None
    assert env_passed.get("PLAN_EXEC_RUN_ID") == "outer-run"
    assert "SOME_SECRET" not in env_passed
    # Per-hop bookkeeping always overwrites.
    assert guardrails.PER_HOP_DEPTH in env_passed
    assert guardrails.PER_HOP_PARENT_RUN_ID in env_passed
