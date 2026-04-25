"""Tests for ``_claude_dispatch_envelope`` and the
``schemas/claude_dispatch_*.json`` wire contracts (TASK-001).

Coverage:
  * Each ``build_*`` constructor produces the expected §7 status.
  * Each constructor's emitted envelope validates against
    ``schemas/claude_dispatch_output.json``.
  * The §6 / §7 plan examples themselves validate against the schemas.
  * ``additionalProperties: false`` invariant is enforced both by the
    schema and by the in-Python ``_build_envelope`` chokepoint.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator, ValidationError, validate

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCHEMAS_DIR = SCRIPTS_DIR / "schemas"
INPUT_SCHEMA_PATH = SCHEMAS_DIR / "claude_dispatch_input.json"
OUTPUT_SCHEMA_PATH = SCHEMAS_DIR / "claude_dispatch_output.json"

sys.path.insert(0, str(SCRIPTS_DIR))

import _claude_dispatch_envelope as env  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def input_schema() -> dict:
    return json.loads(INPUT_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def output_schema() -> dict:
    return json.loads(OUTPUT_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def good_input_payload() -> dict:
    """Mirrors the §6 input example verbatim."""
    return {
        "schema_version": 1,
        "agent": "plan-implementer",
        "payload": {"task_id": "001", "instructions": "do the thing"},
        "output_instructions": {
            "format": "json",
            "schema_path": "plugins/plan-executor/scripts/schemas/implementer_report.json",
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
            "run_id": "run-uuid",
            "parent_span_id": "span-uuid",
            "depth": 0,
            "call_chain": ["orchestrator"],
        },
    }


@pytest.fixture
def good_output_envelope() -> dict:
    """Mirrors the §7 output example verbatim."""
    return {
        "schema_version": 1,
        "status": "ok",
        "status_reason": None,
        "agent": "plan-implementer",
        "model": "claude-opus-4-7",
        "session_id": "sess-uuid",
        "duration_ms": 12345,
        "cost_usd": 0.0933,
        "tokens": {
            "input": 13,
            "output": 235,
            "cache_read": 24145,
            "cache_creation": 36268,
        },
        "result": {"task_id": "001", "status": "success"},
        "result_raw_truncated": "first 16 KB of raw result text",
        "stderr_tail": "last 2 KB of stderr",
        "permission_denials": [],
        "scope": {
            "declared_files_changed": ["plugins/foo.py"],
            "observed_delta_tracked": ["plugins/foo.py"],
            "observed_delta_untracked": [],
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
        },
        "trace": {
            "run_id": "run-uuid",
            "span_id": "span-uuid",
            "parent_span_id": "parent-span-uuid",
            "depth": 1,
            "call_chain": ["orchestrator", "plan-implementer"],
            "started_at": "2026-04-20T10:00:00Z",
            "ended_at": "2026-04-20T10:00:12Z",
        },
        "error": None,
    }


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


def test_input_schema_is_valid_draft7(input_schema: dict) -> None:
    Draft7Validator.check_schema(input_schema)


def test_output_schema_is_valid_draft7(output_schema: dict) -> None:
    Draft7Validator.check_schema(output_schema)


def test_input_schema_accepts_section_6_example(
    input_schema: dict, good_input_payload: dict
) -> None:
    validate(instance=good_input_payload, schema=input_schema)


def test_output_schema_accepts_section_7_example(
    output_schema: dict, good_output_envelope: dict
) -> None:
    validate(instance=good_output_envelope, schema=output_schema)


def test_input_schema_rejects_unknown_top_level_key(
    input_schema: dict, good_input_payload: dict
) -> None:
    bad = copy.deepcopy(good_input_payload)
    bad["sneaky_extra"] = "nope"
    with pytest.raises(ValidationError):
        validate(instance=bad, schema=input_schema)


def test_output_schema_rejects_unknown_top_level_key(
    output_schema: dict, good_output_envelope: dict
) -> None:
    bad = copy.deepcopy(good_output_envelope)
    bad["sneaky_extra"] = "nope"
    with pytest.raises(ValidationError):
        validate(instance=bad, schema=output_schema)


def test_input_schema_has_additional_properties_false(input_schema: dict) -> None:
    assert input_schema["additionalProperties"] is False
    assert input_schema["properties"]["output_instructions"]["additionalProperties"] is False
    assert input_schema["properties"]["overrides"]["additionalProperties"] is False
    assert input_schema["properties"]["guardrails"]["additionalProperties"] is False
    assert input_schema["properties"]["trace"]["additionalProperties"] is False


def test_output_schema_has_additional_properties_false(output_schema: dict) -> None:
    assert output_schema["additionalProperties"] is False
    assert output_schema["properties"]["scope"]["additionalProperties"] is False
    assert output_schema["properties"]["trace"]["additionalProperties"] is False


def test_output_schema_status_enum_matches_section_7(output_schema: dict) -> None:
    assert set(output_schema["properties"]["status"]["enum"]) == env.STATUS_VOCABULARY


# ---------------------------------------------------------------------------
# Constructor coverage — one test per build_*
# ---------------------------------------------------------------------------


@pytest.fixture
def trace() -> dict:
    return {
        "run_id": "run-uuid",
        "span_id": "span-uuid",
        "parent_span_id": "parent-uuid",
        "depth": 1,
        "call_chain": ["orchestrator", "plan-implementer"],
        "started_at": "2026-04-20T10:00:00Z",
        "ended_at": "2026-04-20T10:00:12Z",
    }


def _validate(envelope: dict, output_schema: dict) -> None:
    validate(instance=envelope, schema=output_schema)


def test_build_ok_status_and_validates(output_schema: dict, trace: dict) -> None:
    envelope = env.build_ok(
        agent="plan-implementer",
        model="claude-opus-4-7",
        session_id="sess-uuid",
        duration_ms=12345,
        cost_usd=0.09,
        tokens={
            "input": 13,
            "output": 235,
            "cache_read": 24145,
            "cache_creation": 36268,
        },
        result={"task_id": "001", "status": "success"},
        trace=trace,
    )
    assert envelope["status"] == "ok"
    assert envelope["error"] is None
    assert envelope["schema_version"] == env.SCHEMA_VERSION
    _validate(envelope, output_schema)


def test_build_denied_status_and_validates(output_schema: dict, trace: dict) -> None:
    envelope = env.build_denied(
        code="killswitch",
        message="PLAN_EXEC_DISABLE_NESTED=1",
        agent="plan-implementer",
        trace=trace,
    )
    assert envelope["status"] == "denied"
    assert envelope["error"] == {
        "code": "killswitch",
        "message": "PLAN_EXEC_DISABLE_NESTED=1",
        "retriable": False,
    }
    _validate(envelope, output_schema)


def test_build_timeout_status_and_validates(output_schema: dict, trace: dict) -> None:
    envelope = env.build_timeout(
        duration_ms=300000,
        agent="plan-implementer",
        model="claude-opus-4-7",
        trace=trace,
        stderr_tail="killed after 300s",
    )
    assert envelope["status"] == "timeout"
    assert envelope["duration_ms"] == 300000
    assert envelope["error"]["retriable"] is True
    _validate(envelope, output_schema)


def test_build_schema_invalid_status_and_validates(
    output_schema: dict, trace: dict
) -> None:
    envelope = env.build_schema_invalid(
        message="missing required field 'task_id'",
        agent="plan-implementer",
        result_raw_truncated='{"status": "success"}',
        trace=trace,
    )
    assert envelope["status"] == "schema_invalid"
    assert envelope["result"] is None
    assert envelope["result_raw_truncated"] == '{"status": "success"}'
    _validate(envelope, output_schema)


def test_build_backend_error_status_and_validates(
    output_schema: dict, trace: dict
) -> None:
    envelope = env.build_backend_error(
        code="malformed_output",
        message="stdout was not JSON",
        retriable=False,
        agent="plan-implementer",
        stderr_tail="error: claude binary crashed",
        trace=trace,
    )
    assert envelope["status"] == "backend_error"
    assert envelope["error"]["code"] == "malformed_output"
    _validate(envelope, output_schema)


def test_build_scope_violation_status_and_validates(
    output_schema: dict, trace: dict
) -> None:
    scope = {
        "declared_files_changed": ["foo.py"],
        "observed_delta_tracked": ["foo.py", "bar.py"],
        "observed_delta_untracked": [],
        "scope_violation_detected": True,
        "scope_misreport_detected": False,
    }
    envelope = env.build_scope_violation(
        message="bar.py modified but not declared",
        scope=scope,
        agent="plan-implementer",
        trace=trace,
    )
    assert envelope["status"] == "scope_violation"
    assert envelope["scope"]["scope_violation_detected"] is True
    _validate(envelope, output_schema)


def test_build_input_invalid_status_and_validates(output_schema: dict) -> None:
    envelope = env.build_input_invalid(
        message="agent 'foo' not dispatchable",
        code="agent_not_dispatchable",
        agent="foo",
    )
    assert envelope["status"] == "input_invalid"
    assert envelope["error"]["code"] == "agent_not_dispatchable"
    _validate(envelope, output_schema)


def test_build_manifest_invalid_status_and_validates(output_schema: dict) -> None:
    envelope = env.build_manifest_invalid(
        message="frontmatter missing 'tools:' field",
        agent="plan-implementer",
    )
    assert envelope["status"] == "manifest_invalid"
    assert envelope["error"]["code"] == "manifest_invalid"
    _validate(envelope, output_schema)


def test_build_depth_exceeded_status_and_validates(
    output_schema: dict, trace: dict
) -> None:
    envelope = env.build_depth_exceeded(
        depth=3,
        max_depth=2,
        agent="plan-implementer",
        trace=trace,
    )
    assert envelope["status"] == "depth_exceeded"
    assert "3" in envelope["error"]["message"]
    assert "2" in envelope["error"]["message"]
    _validate(envelope, output_schema)


def test_build_budget_exhausted_status_and_validates(
    output_schema: dict, trace: dict
) -> None:
    envelope = env.build_budget_exhausted(
        cost_usd=10.5,
        cost_cap_usd=10.0,
        agent="plan-implementer",
        trace=trace,
    )
    assert envelope["status"] == "budget_exhausted"
    assert envelope["cost_usd"] == 10.5
    _validate(envelope, output_schema)


# ---------------------------------------------------------------------------
# Public surface — the constructors mandated by the acceptance criteria
# ---------------------------------------------------------------------------


REQUIRED_BUILDERS = [
    "build_ok",
    "build_denied",
    "build_timeout",
    "build_schema_invalid",
    "build_backend_error",
    "build_scope_violation",
    "build_input_invalid",
    "build_manifest_invalid",
    "build_depth_exceeded",
    "build_budget_exhausted",
]


@pytest.mark.parametrize("name", REQUIRED_BUILDERS)
def test_module_exposes_required_builder(name: str) -> None:
    assert hasattr(env, name), f"missing required constructor: {name}"
    assert callable(getattr(env, name))


def test_status_vocabulary_matches_section_7() -> None:
    assert env.STATUS_VOCABULARY == frozenset(
        {
            "ok",
            "schema_invalid",
            "timeout",
            "denied",
            "backend_error",
            "budget_exhausted",
            "depth_exceeded",
            "manifest_invalid",
            "input_invalid",
            "scope_violation",
        }
    )


# ---------------------------------------------------------------------------
# Constructor rejects unknown top-level keys
# ---------------------------------------------------------------------------


def test_constructor_rejects_unknown_extra_key() -> None:
    with pytest.raises(ValueError, match="unknown top-level keys"):
        env.build_ok(
            agent="plan-implementer",
            model="claude-opus-4-7",
            extra={"sneaky_extra": "nope"},
        )


def test_constructor_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown status"):
        env._build_envelope(status="fabricated")


def test_constructor_accepts_known_extra_key() -> None:
    """``extra`` is only meant for hard-to-reach fields; passing a known key
    via ``extra`` must succeed (it just overwrites)."""
    envelope = env.build_ok(
        agent="plan-implementer",
        model="claude-opus-4-7",
        extra={"status_reason": "promoted via extra"},
    )
    assert envelope["status_reason"] == "promoted via extra"


# ---------------------------------------------------------------------------
# Schema-version constant is the single source of truth
# ---------------------------------------------------------------------------


def test_schema_version_constant_matches_input_schema(input_schema: dict) -> None:
    assert input_schema["properties"]["schema_version"]["const"] == env.SCHEMA_VERSION


def test_schema_version_constant_matches_output_schema(output_schema: dict) -> None:
    assert output_schema["properties"]["schema_version"]["const"] == env.SCHEMA_VERSION
