"""Tests for the plan_claude_dispatch stub harness (TASK-002).

Validates that:

  * Each fixture envelope conforms to the v3 envelope shape (the same
    ``schema_version, status, agent, result, scope, error, trace`` keys
    emitted by the shipped wrapper at
    ``plugins/plan-executor/scripts/plan_claude_dispatch.py``).
  * Each agent's ``result`` payload conforms to its per-agent JSON
    schema under ``fixtures/claude_dispatch/schemas/``.
  * The stub records argv + stdin to
    ``$PLAN_CLAUDE_DISPATCH_STUB_RECORD_PATH`` (one JSON line per
    invocation).
  * Fixture-not-found and malformed-fixture errors produce non-zero
    exit codes with diagnostic stderr.
  * Schema-validation failure is detected for each agent by mutating
    ``result.outcome`` to a value outside the per-agent enum.
  * The ``implementer_oversized.json`` fixture's
    ``result_raw_truncated`` is exactly 16384 bytes and its
    ``result.report`` carries every expected sub-field.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

# jsonschema is a runtime dep of the shipped wrapper; reuse it here.
from jsonschema import Draft7Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
STUB = REPO_ROOT / "tests" / "scripts" / "stubs" / "plan_claude_dispatch_stub.py"
FIX_DIR = REPO_ROOT / "tests" / "scripts" / "fixtures" / "claude_dispatch"
SCHEMAS_DIR = FIX_DIR / "schemas"
WRAPPER_OUTPUT_SCHEMA = (
    REPO_ROOT
    / "plugins"
    / "plan-executor"
    / "scripts"
    / "schemas"
    / "claude_dispatch_output.json"
)


_V3_REQUIRED_KEYS = {
    "schema_version",
    "status",
    "agent",
    "result",
    "scope",
    "error",
    "trace",
}


_FIXTURES = {
    "analyst_valid.json": "analyst_result.json",
    "implementer_success.json": "implementer_result.json",
    "implementer_partial.json": "implementer_result.json",
    "remediator_scope_violation.json": "remediator_result.json",
    "implementer_oversized.json": "implementer_result.json",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _run_stub(
    *,
    fixture_path: str | None,
    record_path: str | None = None,
    argv_extras: list | None = None,
    stdin_text: str = "",
    real_wrapper_path: str | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if fixture_path is None:
        env.pop("PLAN_CLAUDE_DISPATCH_STUB_FIXTURE", None)
    else:
        env["PLAN_CLAUDE_DISPATCH_STUB_FIXTURE"] = fixture_path
    if record_path is not None:
        env["PLAN_CLAUDE_DISPATCH_STUB_RECORD_PATH"] = record_path
    else:
        env.pop("PLAN_CLAUDE_DISPATCH_STUB_RECORD_PATH", None)
    if real_wrapper_path is not None:
        env["PLAN_CLAUDE_DISPATCH_STUB_REAL_WRAPPER"] = real_wrapper_path
    else:
        env.pop("PLAN_CLAUDE_DISPATCH_STUB_REAL_WRAPPER", None)

    cmd = [sys.executable, str(STUB)] + list(argv_extras or [])
    return subprocess.run(
        cmd,
        env=env,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Fixture / schema parity (v3 envelope)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", list(_FIXTURES.keys()))
def test_fixture_has_v3_required_keys(fixture_name: str) -> None:
    env = _load_json(FIX_DIR / fixture_name)
    missing = _V3_REQUIRED_KEYS - set(env.keys())
    assert not missing, f"{fixture_name} missing v3 keys: {missing}"


@pytest.mark.parametrize("fixture_name", list(_FIXTURES.keys()))
def test_fixture_validates_against_wrapper_output_schema(fixture_name: str) -> None:
    env = _load_json(FIX_DIR / fixture_name)
    schema = _load_json(WRAPPER_OUTPUT_SCHEMA)
    errors = sorted(
        Draft7Validator(schema).iter_errors(env),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


@pytest.mark.parametrize(
    "fixture_name,schema_name",
    sorted(_FIXTURES.items()),
)
def test_fixture_result_validates_against_agent_schema(
    fixture_name: str, schema_name: str
) -> None:
    env = _load_json(FIX_DIR / fixture_name)
    schema = _load_json(SCHEMAS_DIR / schema_name)
    errors = sorted(
        Draft7Validator(schema).iter_errors(env["result"]),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, "; ".join(f"{list(e.absolute_path)}: {e.message}" for e in errors)


# ---------------------------------------------------------------------------
# Per-agent enum coverage (AC: outcome enum membership)
# ---------------------------------------------------------------------------


def test_analyst_schema_enforces_outcome_enum() -> None:
    schema = _load_json(SCHEMAS_DIR / "analyst_result.json")
    enum = schema["properties"]["outcome"]["enum"]
    assert set(enum) == {"valid", "needs-enrichment", "invalid"}


def test_implementer_schema_enforces_outcome_enum() -> None:
    schema = _load_json(SCHEMAS_DIR / "implementer_result.json")
    enum = schema["properties"]["outcome"]["enum"]
    assert set(enum) == {
        "success", "partial", "failed", "plan-incorrect", "blocked", "malformed",
    }


def test_remediator_schema_enforces_outcome_enum() -> None:
    schema = _load_json(SCHEMAS_DIR / "remediator_result.json")
    enum = schema["properties"]["outcome"]["enum"]
    assert set(enum) == {
        "success", "partial", "failed", "plan-incorrect", "blocked",
        "malformed", "scope-violation",
    }


@pytest.mark.parametrize(
    "schema_name,bad_outcome",
    [
        ("analyst_result.json", "success"),
        ("implementer_result.json", "valid"),
        ("remediator_result.json", "needs-enrichment"),
    ],
)
def test_schema_rejects_outcome_outside_enum(schema_name: str, bad_outcome: str) -> None:
    schema = _load_json(SCHEMAS_DIR / schema_name)
    errors = list(Draft7Validator(schema).iter_errors({"outcome": bad_outcome}))
    assert errors, f"{schema_name} should reject outcome={bad_outcome!r}"


# ---------------------------------------------------------------------------
# Oversized fixture invariant (TASK-004 input)
# ---------------------------------------------------------------------------


def test_oversized_fixture_truncated_at_16kb() -> None:
    env = _load_json(FIX_DIR / "implementer_oversized.json")
    truncated = env["result_raw_truncated"]
    assert isinstance(truncated, str)
    assert len(truncated) == 16384, f"expected 16384 bytes, got {len(truncated)}"


def test_oversized_fixture_report_has_required_subfields() -> None:
    env = _load_json(FIX_DIR / "implementer_oversized.json")
    report = env["result"]["report"]
    for field in (
        "diff_summary",
        "test_command",
        "test_outcome",
        "acceptance_criteria_check",
        "plan_adaptations",
        "concerns_for_reviewer",
    ):
        assert field in report, f"oversized fixture report missing '{field}'"


# ---------------------------------------------------------------------------
# Stub harness behavior
# ---------------------------------------------------------------------------


def test_stub_unset_fixture_delegates_to_real_wrapper(tmp_path: Path) -> None:
    """When the fixture env var is unset, the stub must execvp the real
    wrapper with the original argv/stdin/env preserved (AC: "real
    wrapper is used when unset"). We point the delegation override at a
    sentinel Python script that echoes its argv + stdin + a marker, and
    assert the stub's stdout matches the sentinel's output exactly.
    """
    sentinel = tmp_path / "fake_real_wrapper.py"
    sentinel.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "marker = 'REAL_WRAPPER_INVOKED'\n"
        "out = {\n"
        "    'marker': marker,\n"
        "    'argv': sys.argv[1:],\n"
        "    'stdin': sys.stdin.read() if not sys.stdin.isatty() else '',\n"
        "}\n"
        "sys.stdout.write(json.dumps(out))\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    cp = _run_stub(
        fixture_path=None,
        argv_extras=["run", "--input", "-"],
        stdin_text='{"agent": "plan-analyst"}',
        real_wrapper_path=str(sentinel),
    )
    assert cp.returncode == 0, cp.stderr
    payload = json.loads(cp.stdout)
    assert payload["marker"] == "REAL_WRAPPER_INVOKED"
    assert payload["argv"] == ["run", "--input", "-"]
    assert json.loads(payload["stdin"])["agent"] == "plan-analyst"


def test_stub_unset_fixture_real_wrapper_not_found(tmp_path: Path) -> None:
    """If the real-wrapper override points at a missing path, the stub
    surfaces a diagnostic on stderr with a non-zero exit (defensive,
    not part of the AC but prevents silent execvp failures)."""
    missing = tmp_path / "no_such_wrapper.py"
    cp = _run_stub(
        fixture_path=None,
        real_wrapper_path=str(missing),
    )
    assert cp.returncode != 0
    assert "real-wrapper-not-found" in cp.stderr


def test_stub_fixture_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "no_such_fixture.json"
    cp = _run_stub(fixture_path=str(missing))
    assert cp.returncode == 2
    assert "fixture-not-found" in cp.stderr


def test_stub_malformed_fixture(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    cp = _run_stub(fixture_path=str(bad))
    assert cp.returncode == 2
    assert "malformed-fixture" in cp.stderr


def test_stub_malformed_fixture_missing_status(tmp_path: Path) -> None:
    bad = tmp_path / "no_status.json"
    bad.write_text(json.dumps({"agent": "plan-analyst"}), encoding="utf-8")
    cp = _run_stub(fixture_path=str(bad))
    assert cp.returncode == 2
    assert "malformed-fixture" in cp.stderr


@pytest.mark.parametrize(
    "fixture_name,expected_exit",
    [
        ("analyst_valid.json", 0),
        ("implementer_success.json", 0),
        ("implementer_partial.json", 0),
        ("remediator_scope_violation.json", 1),  # status: scope_violation
        ("implementer_oversized.json", 0),
    ],
)
def test_stub_happy_path_per_fixture(
    tmp_path: Path, fixture_name: str, expected_exit: int
) -> None:
    fixture = FIX_DIR / fixture_name
    record = tmp_path / "record.jsonl"
    cp = _run_stub(
        fixture_path=str(fixture),
        record_path=str(record),
        argv_extras=["run", "--input", "-"],
        stdin_text=json.dumps({"agent": "plan-analyst", "payload": {"x": 1}}),
    )
    assert cp.returncode == expected_exit, cp.stderr
    emitted = json.loads(cp.stdout)
    assert emitted["status"] == _load_json(fixture)["status"]
    # Record file should have one JSON line.
    lines = record.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["argv"][-2:] == ["--input", "-"]
    assert json.loads(entry["stdin"])["agent"] == "plan-analyst"


def test_stub_records_appended_across_invocations(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    for fixture_name in ("analyst_valid.json", "implementer_success.json"):
        cp = _run_stub(
            fixture_path=str(FIX_DIR / fixture_name),
            record_path=str(record),
            argv_extras=["run"],
            stdin_text="{}",
        )
        assert cp.returncode == 0, cp.stderr
    lines = record.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
