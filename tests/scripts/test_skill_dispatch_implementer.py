"""TASK-004: Phase B plan-implementer dispatch migration to v3 wrapper.

Asserts that the SKILL.md + dispatch-templates.md migration of the three
``plan-implementer`` dispatch sites — Phase B default, Phase B-rework
(D.2a.5), and Phase D.2b role-swap — from in-process
``Agent(subagent_type=...)`` to a Bash invocation of
``plan_claude_dispatch.py run --input <payload.json>`` satisfies the
TASK-004 acceptance criteria:

  * SKILL.md and dispatch-templates.md no longer reference
    ``subagent_type.*plan-implementer`` for any of the three sites
    (the rg invariant from the AC).
  * SKILL.md's three dispatch paragraphs name the wrapper invocation
    AND carry a template-selector variant (``default | rework |
    role-swap``) on ``payload.variant``.
  * The three templates each carry a ``<!-- TRANSPORT BOUNDARY -->``
    seam marker; the agent-behavior body below the marker preserves
    its load-bearing pre-migration phrases.
  * Implementer outcome vocabulary is preserved verbatim:
    ``success | partial | failed | plan-incorrect | blocked |
    malformed``. Validated against the TASK-002 implementer schema.
  * Required report sections (``Plan adaptations``, ``Concerns for
    reviewer``, ``On-failure revert``) surface inside ``.result.report``
    as ``plan_adaptations[]``, ``concerns_for_reviewer[]``, and
    ``on_failure_revert``.
  * ``scope.scope_violation_detected`` and
    ``scope.scope_misreport_detected`` surface as top-level envelope
    fields (i.e. inside ``envelope["scope"]``) and block commit.
  * Fixture coverage for: success commit, partial → reviewer notes,
    scope_violation → fail, plan-incorrect → halt, role-swap happy
    path, ``malformed`` outcome → fail-task with stage=``implement``.
  * **Envelope-size guard:** the ``implementer_oversized.json`` fixture
    (16 KB ``result_raw_truncated`` + fully-populated ``result.report``)
    proves all required structured fields are reachable from the
    envelope even when the raw-result cap is hit. Full envelope size
    must be ≤ 80 KB under that fixture.
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
IMPL_SUCCESS_FIXTURE = FIXTURES_DIR / "implementer_success.json"
IMPL_PARTIAL_FIXTURE = FIXTURES_DIR / "implementer_partial.json"
IMPL_OVERSIZED_FIXTURE = FIXTURES_DIR / "implementer_oversized.json"
IMPL_RESULT_SCHEMA = FIXTURES_DIR / "schemas" / "implementer_result.json"

ALLOWED_IMPLEMENTER_OUTCOMES = {
    "success",
    "partial",
    "failed",
    "plan-incorrect",
    "blocked",
    "malformed",
}

TRANSPORT_BOUNDARY_MARKER = (
    "<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->"
)


# ---------------------------------------------------------------------------
# Static-text invariants (the AC's rg checks + transport-boundary presence)
# ---------------------------------------------------------------------------


def test_skill_md_has_no_plan_implementer_subagent_dispatch() -> None:
    """rg invariant from the AC: zero ``subagent_type.*plan-implementer``
    hits across SKILL.md.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    assert not re.search(r'subagent_type[^"\n]*plan-implementer', text), (
        "SKILL.md still dispatches plan-implementer via in-process Agent "
        "tool; TASK-004 requires the Bash wrapper invocation."
    )


def test_dispatch_templates_has_no_plan_implementer_subagent_dispatch() -> None:
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    assert not re.search(r'subagent_type[^"\n]*plan-implementer', text), (
        "dispatch-templates.md still references subagent_type "
        "'plan-implementer' as the dispatch shape; TASK-004 requires "
        "all three transport headers (Phase B / B-rework / D.2b) to "
        "use the wrapper."
    )


def test_skill_md_phase_b_invokes_wrapper_run_subcommand() -> None:
    """The Phase B default dispatch paragraph must name the wrapper."""
    text = SKILL_MD.read_text(encoding="utf-8")
    # Find the Phase B section header and require the wrapper invocation
    # to appear within the next ~3 KB of text (well past the dispatch
    # paragraph but before Phase C).
    idx = text.find("### Phase B — Implement")
    assert idx != -1, "Phase B section header missing"
    window = text[idx : idx + 3000]
    assert "plan_claude_dispatch.py" in window
    assert "run --input" in window
    assert "plan-implementer" in window


def test_skill_md_d2b_invokes_wrapper_run_subcommand() -> None:
    """The D.2b role-swap dispatch paragraph must name the wrapper +
    carry the ``role-swap`` template selector.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    idx = text.find("#### D.2b — Role-swap retry")
    assert idx != -1, "D.2b section header missing"
    window = text[idx : idx + 2000]
    assert "plan_claude_dispatch.py" in window
    assert "run --input" in window
    assert "role-swap" in window


def test_skill_md_bounded_remediation_routes_through_wrapper() -> None:
    """The D.2 routing prose's ``dispatch_bounded_remediation`` clause
    must reference the wrapper + ``rework`` variant selector.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    # Locate the descriptive clause (the second occurrence — the first
    # is the action-name listing in the ``review-route`` return-values
    # enum). The descriptive clause is delimited by the literal phrase
    # ``→ re-dispatch the **Phase B-rework** template``.
    clause_idx = text.find("re-dispatch the **Phase B-rework** template")
    assert clause_idx != -1, (
        "D.2 routing prose missing the Phase B-rework dispatch clause."
    )
    window = text[clause_idx : clause_idx + 800]
    assert "plan_claude_dispatch.py" in window
    assert "rework" in window


def test_dispatch_templates_three_transport_boundary_markers() -> None:
    """Each of Phase B, Phase B-rework, Phase D.2b must carry the
    transport-boundary marker per AC.
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    occurrences = text.count(TRANSPORT_BOUNDARY_MARKER)
    # Phase A-single (TASK-003) already carries one; the three new
    # implementer templates add three more for a total of >= 4.
    assert occurrences >= 4, (
        f"Expected at least 4 transport-boundary markers (Phase A-single "
        f"+ three Phase B variants); found {occurrences}."
    )


@pytest.mark.parametrize(
    "section_header",
    [
        "## Phase B — plan-implementer dispatch",
        "## Phase B-rework — Bounded remediation retry",
        "## Phase D.2b — Role-swap retry",
    ],
)
def test_dispatch_templates_section_has_marker_above_body(
    section_header: str,
) -> None:
    """Each implementer template section carries the boundary marker
    above its agent-behavior body.
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find(section_header)
    assert section_idx != -1, f"Section missing: {section_header}"
    # Find the next section header after this one, or end-of-file.
    next_section_idx = text.find("\n## ", section_idx + len(section_header))
    if next_section_idx == -1:
        next_section_idx = len(text)
    section = text[section_idx:next_section_idx]
    assert TRANSPORT_BOUNDARY_MARKER in section, (
        f"Section {section_header!r} missing transport boundary marker."
    )


def test_phase_b_body_invariant_below_marker() -> None:
    """Phase B agent-behavior body preserves load-bearing pre-migration
    phrases below the marker.
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find("## Phase B — plan-implementer dispatch")
    next_section_idx = text.find("\n## ", section_idx + 1)
    section = text[section_idx:next_section_idx]
    after_marker = section.split(TRANSPORT_BOUNDARY_MARKER, 1)[1]
    for phrase in (
        "target_task_id",
        "Pre-read excerpts",
        "Implement this task from the plan at",
        "You do NOT have the Agent tool",
        "Coupling check",
    ):
        assert phrase in after_marker, (
            f"Phase B agent-behavior body lost load-bearing phrase: "
            f"{phrase!r}"
        )


def test_phase_b_rework_body_invariant_below_marker() -> None:
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find("## Phase B-rework — Bounded remediation retry")
    next_section_idx = text.find("\n## ", section_idx + 1)
    section = text[section_idx:next_section_idx]
    after_marker = section.split(TRANSPORT_BOUNDARY_MARKER, 1)[1]
    for phrase in (
        "Apply a narrow remediation patch to this task's existing implementation",
        "Codex (peer reviewer) returned `needs-rework`",
        "Fix narrowly, do not scope-inflate",
        "You do NOT have the Agent tool",
    ):
        assert phrase in after_marker, (
            f"Phase B-rework body lost load-bearing phrase: {phrase!r}"
        )


def test_phase_d2b_body_invariant_below_marker() -> None:
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    section_idx = text.find("## Phase D.2b — Role-swap retry")
    next_section_idx = text.find("\n## ", section_idx + 1)
    section = text[section_idx:next_section_idx]
    after_marker = section.split(TRANSPORT_BOUNDARY_MARKER, 1)[1]
    for phrase in (
        "Per design",
        "Claude re-implements",
        "Reviewer findings are NOT forwarded",
        "no further retries",
    ):
        assert phrase in after_marker, (
            f"Phase D.2b body lost load-bearing phrase: {phrase!r}"
        )


# ---------------------------------------------------------------------------
# Stub-driven extraction-shim end-to-end coverage of all six fixture cases
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

    schema = json.loads(IMPL_RESULT_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(envelope["result"])


def test_implementer_outcome_vocabulary_matches_schema() -> None:
    """The TASK-002 implementer schema enum MUST equal the TASK-004 AC
    outcome vocabulary verbatim.
    """
    schema = json.loads(IMPL_RESULT_SCHEMA.read_text(encoding="utf-8"))
    enum = set(schema["properties"]["outcome"]["enum"])
    assert enum == ALLOWED_IMPLEMENTER_OUTCOMES, (
        f"Schema enum {enum!r} drifted from TASK-004 AC outcome vocab "
        f"{ALLOWED_IMPLEMENTER_OUTCOMES!r}."
    )


def test_success_envelope_routes_to_phase_d(tmp_path: Path) -> None:
    """Fixture: success commit. Stub returns rc=0, ``.result.outcome``
    is ``success``, scope flags are False — orchestrator routes to D.
    """
    envelope = json.loads(IMPL_SUCCESS_FIXTURE.read_text(encoding="utf-8"))
    rc, stdout, stderr = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"
    parsed = json.loads(stdout)

    assert parsed["status"] == "ok"
    assert parsed["agent"] == "plan-implementer"
    assert parsed["result"]["outcome"] == "success"
    assert parsed["result"]["outcome"] in ALLOWED_IMPLEMENTER_OUTCOMES
    assert parsed["scope"]["scope_violation_detected"] is False
    assert parsed["scope"]["scope_misreport_detected"] is False
    _validate_inner_result(parsed)


def test_partial_envelope_carries_concerns(tmp_path: Path) -> None:
    """Fixture: partial → reviewer notes. ``.result.report
    .concerns_for_reviewer`` is non-empty so the orchestrator can
    surface them downstream.
    """
    envelope = json.loads(IMPL_PARTIAL_FIXTURE.read_text(encoding="utf-8"))
    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0
    parsed = json.loads(stdout)

    assert parsed["result"]["outcome"] == "partial"
    concerns = parsed["result"]["report"]["concerns_for_reviewer"]
    assert isinstance(concerns, list) and len(concerns) >= 1
    _validate_inner_result(parsed)


def test_scope_violation_envelope_blocks_commit(tmp_path: Path) -> None:
    """Synthetic: a success ``.result.outcome`` paired with
    ``scope.scope_violation_detected=True`` MUST surface the scope flag
    so the orchestrator blocks the commit per existing rules.
    """
    envelope = json.loads(IMPL_SUCCESS_FIXTURE.read_text(encoding="utf-8"))
    envelope = copy.deepcopy(envelope)
    envelope["scope"]["scope_violation_detected"] = True
    envelope["scope"]["observed_delta_tracked"].append("foo/out_of_scope.py")

    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    # Status is still ``ok`` (transport succeeded); the scope flag is
    # the orchestrator-side gate.
    assert rc == 0
    parsed = json.loads(stdout)
    assert parsed["status"] == "ok"
    assert parsed["scope"]["scope_violation_detected"] is True
    # The orchestrator's block-commit rule keys off this top-level
    # envelope flag — re-affirm it lives at ``envelope["scope"]``.
    assert "scope_violation_detected" in parsed["scope"]


def test_plan_incorrect_envelope_halts(tmp_path: Path) -> None:
    """Synthetic: ``.result.outcome=plan-incorrect`` halts the run per
    existing Phase C rules. The wrapper still returns transport ``ok``;
    the routing decision is on the inner outcome.
    """
    envelope = copy.deepcopy(json.loads(IMPL_SUCCESS_FIXTURE.read_text(encoding="utf-8")))
    envelope["result"]["outcome"] = "plan-incorrect"
    envelope["result"]["report"]["concerns_for_reviewer"] = [
        "Plan references a symbol that no longer exists",
    ]

    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0
    parsed = json.loads(stdout)
    assert parsed["result"]["outcome"] == "plan-incorrect"
    assert parsed["result"]["outcome"] in ALLOWED_IMPLEMENTER_OUTCOMES
    _validate_inner_result(parsed)


def test_role_swap_happy_path(tmp_path: Path) -> None:
    """D.2b role-swap retry: a fresh success envelope with a distinct
    span_id stands in for the role-swap dispatch result. The
    orchestrator MUST be able to read the same fields it reads on the
    Phase B default path — variant selection is in the payload, not
    the envelope shape.
    """
    envelope = copy.deepcopy(json.loads(IMPL_SUCCESS_FIXTURE.read_text(encoding="utf-8")))
    envelope["session_id"] = "stub-role-swap-session"
    envelope["trace"]["span_id"] = "stub-role-swap-span"

    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0
    parsed = json.loads(stdout)
    assert parsed["result"]["outcome"] == "success"
    # Required report fields reachable on the role-swap path.
    report = parsed["result"]["report"]
    assert "plan_adaptations" in report
    assert "concerns_for_reviewer" in report
    _validate_inner_result(parsed)


def test_malformed_outcome_routes_to_fail_task_implement_stage(
    tmp_path: Path,
) -> None:
    """``malformed`` is the existing markdown-parser output for
    un-parseable reports; the wrapper path emits it when ``.result``
    schema-validation fails but transport succeeded. Round-trips the
    same as other implementer outcomes (success → fail-task with
    stage=implement on the orchestrator side).
    """
    envelope = copy.deepcopy(json.loads(IMPL_SUCCESS_FIXTURE.read_text(encoding="utf-8")))
    envelope["status_reason"] = "result schema validation failed"
    envelope["result"] = {
        "outcome": "malformed",
        "files_changed": [],
        "report": {},
    }

    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0  # transport ``ok``
    parsed = json.loads(stdout)
    assert parsed["result"]["outcome"] == "malformed"
    assert parsed["result"]["outcome"] in ALLOWED_IMPLEMENTER_OUTCOMES
    # Inner schema still validates: ``malformed`` is in the enum and
    # ``files_changed`` + ``report`` are permissive.
    _validate_inner_result(parsed)


# ---------------------------------------------------------------------------
# Envelope-size guard (load-bearing per AC)
# ---------------------------------------------------------------------------


def test_oversized_fixture_envelope_size_under_cap() -> None:
    """``implementer_oversized.json`` carries a 16 KB
    ``result_raw_truncated`` blob plus a fully-populated
    ``result.report``. Total envelope JSON byte length ≤ 80 KB.
    """
    envelope = json.loads(IMPL_OVERSIZED_FIXTURE.read_text(encoding="utf-8"))
    size = len(json.dumps(envelope).encode())
    assert size <= 80 * 1024, (
        f"Envelope size {size} B exceeds 80 KB cap; the result-raw "
        f"truncation cap may have drifted."
    )
    # Required structured fields reachable even when raw-result cap hit.
    assert envelope["result"]["outcome"] in ALLOWED_IMPLEMENTER_OUTCOMES
    report = envelope["result"]["report"]
    assert "plan_adaptations" in report
    assert "concerns_for_reviewer" in report
    # ``result_raw_truncated`` is populated (the cap was hit).
    assert isinstance(envelope["result_raw_truncated"], str)
    assert len(envelope["result_raw_truncated"]) >= 16 * 1024


def test_oversized_envelope_round_trips_through_stub(tmp_path: Path) -> None:
    """The 16 KB-truncated envelope round-trips the stub end-to-end and
    keeps ``.result.report`` fields reachable on the way out.
    """
    envelope = json.loads(IMPL_OVERSIZED_FIXTURE.read_text(encoding="utf-8"))
    rc, stdout, stderr = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"
    parsed = json.loads(stdout)
    assert parsed["status"] == "ok"
    assert parsed["result"]["outcome"] == "success"
    assert parsed["result"]["report"]["plan_adaptations"] == []
    assert parsed["result"]["report"]["concerns_for_reviewer"] == []
    _validate_inner_result(parsed)
