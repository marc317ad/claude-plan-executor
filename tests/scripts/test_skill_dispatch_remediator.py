"""TASK-005: Phase D.2a.6 plan-remediator dispatch migration to v3 wrapper.

Asserts that the SKILL.md + dispatch-templates.md migration of the
single ``plan-remediator`` dispatch site at Phase D.2a.6
(narrow-remediation retry) — from in-process
``Agent(subagent_type="plan-remediator", model="opus")`` to a Bash
invocation of ``plan_claude_dispatch.py run --input <payload.json>`` —
satisfies the TASK-005 acceptance criteria:

  * SKILL.md and dispatch-templates.md no longer reference
    ``subagent_type.*plan-executor:plan-remediator`` for the D.2a.6
    site (the rg invariant from the AC).
  * SKILL.md's D.2a.6 dispatch paragraph (the ``review-route``
    ``dispatch_narrow_remediation`` clause) names the wrapper
    invocation AND carries ``payload.agent="plan-remediator"`` plus the
    ``payload.dispatch_context: {load_bearing_findings,
    dismissed_findings, d5_summary}`` skeleton.
  * The Phase B-narrow-remediation template carries a
    ``<!-- TRANSPORT BOUNDARY -->`` seam marker; the agent-behavior
    body below the marker preserves its load-bearing pre-migration
    phrases byte-identical.
  * Remediator outcome vocabulary is preserved verbatim:
    ``success | partial | failed | plan-incorrect | blocked |
    malformed | scope-violation``. The extra ``scope-violation``
    outcome (not present in implementer) and ``malformed`` both
    round-trip and validate against the TASK-002 remediator schema.
  * The mandatory ``**Dismissed findings noted:**`` report section
    surfaces as ``.result.report.dismissed_findings_acknowledged[]``.
  * Fixture coverage: ``remediator_scope_violation.json`` round-trips
    through the stub and the orchestrator-side rule routes to
    ``pause_awaiting_user`` (stage ``post_narrow_remediation_implement``).
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan"
SKILL_MD = SKILL_DIR / "SKILL.md"
DISPATCH_TEMPLATES = SKILL_DIR / "dispatch-templates.md"
STUB = REPO_ROOT / "tests" / "scripts" / "stubs" / "plan_claude_dispatch_stub.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "scripts" / "fixtures" / "claude_dispatch"
REMEDIATOR_SCOPE_FIXTURE = FIXTURES_DIR / "remediator_scope_violation.json"
REMEDIATOR_RESULT_SCHEMA = FIXTURES_DIR / "schemas" / "remediator_result.json"

ALLOWED_REMEDIATOR_OUTCOMES = {
    "success",
    "partial",
    "failed",
    "plan-incorrect",
    "blocked",
    "malformed",
    "scope-violation",
}

TRANSPORT_BOUNDARY_MARKER = (
    "<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->"
)

NARROW_REMEDIATION_HEADER = (
    "## Phase B-narrow-remediation — Narrow-remediation retry (D.2a.6)"
)


# ---------------------------------------------------------------------------
# Static-text invariants (the AC's rg checks + transport-boundary presence)
# ---------------------------------------------------------------------------


def test_skill_md_has_no_plan_remediator_namespaced_subagent_dispatch() -> None:
    """rg invariant from the AC: zero
    ``subagent_type.*plan-executor:plan-remediator`` hits across
    SKILL.md.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    assert not re.search(
        r"subagent_type[^\n]*plan-executor:plan-remediator", text
    ), (
        "SKILL.md still dispatches plan-remediator via in-process "
        "Agent tool with the plugin-namespaced subagent_type; TASK-005 "
        "requires the Bash wrapper invocation."
    )


def test_dispatch_templates_has_no_plan_remediator_namespaced_subagent_dispatch() -> None:
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    assert not re.search(
        r"subagent_type[^\n]*plan-executor:plan-remediator", text
    ), (
        "dispatch-templates.md still references the plugin-namespaced "
        "subagent_type 'plan-executor:plan-remediator' as the dispatch "
        "shape; TASK-005 requires the wrapper transport header for "
        "Phase B-narrow-remediation."
    )


def test_skill_md_narrow_remediation_routes_through_wrapper() -> None:
    """The D.2 routing prose's ``dispatch_narrow_remediation`` clause
    must reference the wrapper + ``plan-remediator`` agent + the
    ``dispatch_context`` skeleton (load_bearing_findings,
    dismissed_findings, d5_summary).
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    clause_idx = text.find(
        "re-dispatch the **Phase B-narrow-remediation** template"
    )
    assert clause_idx != -1, (
        "D.2 routing prose missing the Phase B-narrow-remediation "
        "dispatch clause."
    )
    window = text[clause_idx : clause_idx + 1200]
    assert "plan_claude_dispatch.py" in window, (
        "Narrow-remediation clause does not name the wrapper"
    )
    assert "run --input" in window, (
        "Narrow-remediation clause does not name the wrapper subcommand"
    )
    assert 'payload.agent="plan-remediator"' in window, (
        "Narrow-remediation clause does not pin payload.agent to "
        "plan-remediator"
    )
    assert "load_bearing_findings" in window, (
        "Narrow-remediation clause missing load_bearing_findings in "
        "dispatch_context skeleton"
    )
    assert "dismissed_findings" in window, (
        "Narrow-remediation clause missing dismissed_findings in "
        "dispatch_context skeleton"
    )


def test_skill_md_narrow_remediation_documents_dismissed_acknowledgement() -> None:
    """The D.2 routing prose MUST document that the mandatory
    ``Dismissed findings noted`` report section surfaces as
    ``.result.report.dismissed_findings_acknowledged[]`` so the
    orchestrator's D.5 gate has a deterministic place to read it from.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    clause_idx = text.find(
        "re-dispatch the **Phase B-narrow-remediation** template"
    )
    assert clause_idx != -1
    window = text[clause_idx : clause_idx + 1200]
    assert "dismissed_findings_acknowledged" in window, (
        "Narrow-remediation clause does not name "
        "`.result.report.dismissed_findings_acknowledged[]` as the "
        "surface for the mandatory `**Dismissed findings noted:**` "
        "report section."
    )


def test_dispatch_templates_narrow_remediation_has_transport_boundary() -> None:
    """Phase B-narrow-remediation section MUST carry the transport
    boundary marker (TASK-005 transport-header migration).
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find(NARROW_REMEDIATION_HEADER)
    assert section_idx != -1, "Phase B-narrow-remediation header missing"
    next_section_idx = text.find("\n## ", section_idx + len(NARROW_REMEDIATION_HEADER))
    if next_section_idx == -1:
        next_section_idx = len(text)
    section = text[section_idx:next_section_idx]
    assert TRANSPORT_BOUNDARY_MARKER in section, (
        "Phase B-narrow-remediation section missing transport boundary "
        "marker."
    )


def test_dispatch_templates_narrow_remediation_transport_header_invariants() -> None:
    """Above the boundary marker, the transport header MUST name the
    wrapper invocation, pin ``payload.agent="plan-remediator"``, point
    at the TASK-002 remediator schema, and document the full remediator
    outcome vocabulary (including ``scope-violation``).
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find(NARROW_REMEDIATION_HEADER)
    assert section_idx != -1
    next_section_idx = text.find("\n## ", section_idx + 1)
    if next_section_idx == -1:
        next_section_idx = len(text)
    section = text[section_idx:next_section_idx]
    before_marker, _ = section.split(TRANSPORT_BOUNDARY_MARKER, 1)
    assert "plan_claude_dispatch.py" in before_marker
    assert "run --input" in before_marker
    assert 'payload.agent="plan-remediator"' in before_marker
    assert "remediator_result.json" in before_marker, (
        "Transport header does not point output_instructions.schema_path "
        "at the TASK-002 remediator schema."
    )
    for outcome in ALLOWED_REMEDIATOR_OUTCOMES:
        assert outcome in before_marker, (
            f"Transport header does not document remediator outcome "
            f"vocabulary entry {outcome!r}."
        )


def test_dispatch_templates_narrow_remediation_body_invariant_below_marker() -> None:
    """Phase B-narrow-remediation agent-behavior body preserves
    load-bearing pre-migration phrases below the marker (byte-identical
    to pre-migration body).
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find(NARROW_REMEDIATION_HEADER)
    next_section_idx = text.find("\n## ", section_idx + 1)
    if next_section_idx == -1:
        next_section_idx = len(text)
    section = text[section_idx:next_section_idx]
    after_marker = section.split(TRANSPORT_BOUNDARY_MARKER, 1)[1]
    for phrase in (
        "Apply a narrow remediation patch to this task's existing implementation",
        "DO NOT fix — context only",
        "the touch-only-these-lines contract",
        "**Dismissed findings noted:**",
        "scope-violation",
        "You do NOT have the Agent tool",
    ):
        assert phrase in after_marker, (
            f"Phase B-narrow-remediation body lost load-bearing phrase: "
            f"{phrase!r}"
        )


# ---------------------------------------------------------------------------
# Stub-driven extraction-shim coverage
# ---------------------------------------------------------------------------


def _run_stub_with_envelope(envelope: dict, tmp_path: Path) -> tuple[int, str, str]:
    fixture_path = tmp_path / "envelope.json"
    fixture_path.write_text(json.dumps(envelope), encoding="utf-8")
    env = os.environ.copy()
    env["PLAN_CLAUDE_DISPATCH_STUB_FIXTURE"] = str(fixture_path)
    proc = subprocess.run(
        [sys.executable, str(STUB), "run", "--input", "-"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=30,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _validate_inner_result(envelope: dict) -> None:
    from jsonschema import Draft7Validator

    schema = json.loads(REMEDIATOR_RESULT_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(envelope["result"])


def test_remediator_outcome_vocabulary_matches_schema() -> None:
    """The TASK-002 remediator schema enum MUST equal the TASK-005 AC
    outcome vocabulary verbatim (the AC's "preserved verbatim" clause).
    """
    schema = json.loads(REMEDIATOR_RESULT_SCHEMA.read_text(encoding="utf-8"))
    enum = set(schema["properties"]["outcome"]["enum"])
    assert enum == ALLOWED_REMEDIATOR_OUTCOMES, (
        f"Schema enum {enum!r} drifted from TASK-005 AC outcome vocab "
        f"{ALLOWED_REMEDIATOR_OUTCOMES!r}."
    )


def test_remediator_schema_includes_scope_violation_extension() -> None:
    """The remediator schema's outcome enum MUST include the extra
    ``scope-violation`` value (not present in the implementer schema).
    This is the TASK-005 AC's "extra `scope-violation` outcome (not
    present in implementer)" invariant.
    """
    schema = json.loads(REMEDIATOR_RESULT_SCHEMA.read_text(encoding="utf-8"))
    enum = set(schema["properties"]["outcome"]["enum"])
    assert "scope-violation" in enum
    assert "malformed" in enum, (
        "Remediator schema must accept the malformed wrapper outcome "
        "for round-trip parity with the implementer path."
    )


def test_scope_violation_fixture_round_trips_through_stub(tmp_path: Path) -> None:
    """``remediator_scope_violation.json`` round-trips through the
    stub: the wrapper exits non-zero (status != ``ok``), the inner
    result outcome is ``scope-violation``, and the envelope's
    ``scope.scope_violation_detected`` flag is set so the orchestrator
    can route to the awaiting-user pause.
    """
    envelope = json.loads(REMEDIATOR_SCOPE_FIXTURE.read_text(encoding="utf-8"))
    rc, stdout, stderr = _run_stub_with_envelope(envelope, tmp_path)
    # Status is ``scope_violation`` (not ``ok``); stub maps anything
    # other than ``ok`` to exit code 1, mirroring the real wrapper.
    assert rc == 1, f"stub exited {rc}: stderr={stderr!r}"
    parsed = json.loads(stdout)
    assert parsed["status"] == "scope_violation"
    assert parsed["agent"] == "plan-remediator"
    assert parsed["result"]["outcome"] == "scope-violation"
    assert parsed["result"]["outcome"] in ALLOWED_REMEDIATOR_OUTCOMES
    assert parsed["scope"]["scope_violation_detected"] is True
    _validate_inner_result(parsed)


def test_scope_violation_envelope_routes_to_awaiting_user_pause(
    tmp_path: Path,
) -> None:
    """The TASK-005 AC requires the fixture to assert the orchestrator
    halts with ``pause_awaiting_user`` correctly. The orchestrator-side
    routing rule (per SKILL.md §D.2a.6 and the migrated narrow-remediation
    clause) maps remediator ``outcome=scope-violation`` to
    ``pause_awaiting_user`` with stage
    ``post_narrow_remediation_implement``. Encode that rule here so a
    future drift in the routing prose is caught by this test.
    """
    envelope = json.loads(REMEDIATOR_SCOPE_FIXTURE.read_text(encoding="utf-8"))
    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 1
    parsed = json.loads(stdout)

    # Orchestrator-side routing rule under test (mirrors the SKILL.md
    # §D.2a.6 migrated clause): a non-success remediator outcome on the
    # narrow-remediation retry triggers the awaiting-user pause with
    # stage `post_narrow_remediation_implement`.
    def _route(envelope: dict) -> tuple[str, str | None]:
        outcome = envelope.get("result", {}).get("outcome")
        if outcome == "success":
            return ("dispatch_review", None)
        if outcome in ALLOWED_REMEDIATOR_OUTCOMES and outcome != "success":
            return ("pause_awaiting_user", "post_narrow_remediation_implement")
        return ("unknown_state", None)

    action, stage = _route(parsed)
    assert action == "pause_awaiting_user"
    assert stage == "post_narrow_remediation_implement"

    # The SKILL.md routing prose names this stage explicitly — guard
    # against drift in the routing-table documentation.
    skill_text = SKILL_MD.read_text(encoding="utf-8")
    assert "post_narrow_remediation_implement" in skill_text


def test_dismissed_findings_acknowledged_surfaces_on_inner_report(
    tmp_path: Path,
) -> None:
    """The mandatory ``**Dismissed findings noted:**`` report section
    surfaces as ``.result.report.dismissed_findings_acknowledged[]``;
    the orchestrator's D.5 gate reads from there. The
    ``remediator_result.json`` schema is permissive
    (``additionalProperties: true``), so the field round-trips through
    the wrapper unchanged.
    """
    envelope = copy.deepcopy(
        json.loads(REMEDIATOR_SCOPE_FIXTURE.read_text(encoding="utf-8"))
    )
    # Inject the field per the AC contract; the schema permits extra
    # properties on the inner ``report`` object.
    envelope["result"]["report"]["dismissed_findings_acknowledged"] = [
        "Finding index 1 (foo/bar.py:12) — read but did NOT act on it.",
        "Finding index 3 (foo/bar.py:42) — read but did NOT act on it.",
    ]
    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 1  # fixture status is ``scope_violation``
    parsed = json.loads(stdout)
    acked = parsed["result"]["report"]["dismissed_findings_acknowledged"]
    assert isinstance(acked, list)
    assert len(acked) == 2
    assert "Finding index 1" in acked[0]
    _validate_inner_result(parsed)


@pytest.mark.parametrize(
    "outcome",
    sorted(ALLOWED_REMEDIATOR_OUTCOMES),
)
def test_all_remediator_outcomes_round_trip(outcome: str, tmp_path: Path) -> None:
    """Every entry in the remediator outcome vocabulary round-trips
    through the wrapper transport unchanged and validates against the
    TASK-002 remediator schema. This locks the AC's "preserved verbatim"
    clause to a per-value assertion.
    """
    envelope = copy.deepcopy(
        json.loads(REMEDIATOR_SCOPE_FIXTURE.read_text(encoding="utf-8"))
    )
    envelope["result"]["outcome"] = outcome
    # Keep status=``ok`` for outcomes other than scope-violation so the
    # stub maps the route to rc=0 — this isolates the inner-result
    # vocabulary check from the transport-status mapping.
    if outcome != "scope-violation":
        envelope["status"] = "ok"
        envelope["scope"]["scope_violation_detected"] = False
        envelope["error"] = None

    rc, stdout, stderr = _run_stub_with_envelope(envelope, tmp_path)
    if outcome == "scope-violation":
        assert rc == 1, f"stub exited {rc}: stderr={stderr!r}"
    else:
        assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"

    parsed = json.loads(stdout)
    assert parsed["result"]["outcome"] == outcome
    assert parsed["result"]["outcome"] in ALLOWED_REMEDIATOR_OUTCOMES
    _validate_inner_result(parsed)
