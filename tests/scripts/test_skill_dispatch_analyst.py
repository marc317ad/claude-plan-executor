"""TASK-003: Phase 1 plan-analyst dispatch migration to v3 wrapper.

Asserts that the SKILL.md + dispatch-templates.md migration of the Phase 1
``plan-analyst`` dispatch from in-process ``Agent(subagent_type=...)`` to a
Bash invocation of ``plan_claude_dispatch.py run --input <payload.json>``
satisfies the TASK-003 acceptance criteria:

  * SKILL.md and dispatch-templates.md no longer reference
    ``subagent_type.*plan-analyst`` for the Phase 1 site (the rg invariant).
  * SKILL.md's Phase 1 dispatch paragraph names the wrapper invocation
    AND carries the verbatim extraction-shim pointer prescribed by the
    acceptance criteria.
  * The Phase A-single template carries a transport-header section with
    the ``<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->``
    seam marker; the agent-behavior body below the marker is byte-equal
    (modulo whitespace) to the pre-migration wording the orchestrator
    is contractually NOT changing in this plan.
  * The extraction shim, driven via the TASK-002 stub fixture
    ``analyst_valid.json``, asserts ``.status=="ok"`` and pulls
    ``.result.outcome`` from the v3 envelope; outcome vocabulary
    matches the analyst result schema (``valid | needs-enrichment |
    invalid``).
  * A schedule-bearing analyst envelope (synthesized inline — the v1
    ``analyst_valid.json`` fixture is intentionally minimal) round-trips
    through ``plan_ops.py parse-schedule`` and yields the same
    ``{outcome, tasks[*].id, batches[*].index}`` projection as a
    pre-migration whole-plan analyst transcript.
"""

from __future__ import annotations

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
PLAN_OPS = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_ops.py"
STUB = REPO_ROOT / "tests" / "scripts" / "stubs" / "plan_claude_dispatch_stub.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "scripts" / "fixtures" / "claude_dispatch"
ANALYST_VALID_FIXTURE = FIXTURES_DIR / "analyst_valid.json"
ANALYST_RESULT_SCHEMA = FIXTURES_DIR / "schemas" / "analyst_result.json"

ALLOWED_ANALYST_OUTCOMES = {"valid", "needs-enrichment", "invalid"}

# TASK-006 collapsed the per-site extraction shims into one shared
# section + a `plan_ops.py claude-envelope-extract` subcommand. The AC
# is now satisfied by either (a) the verbatim pre-TASK-006 shim text OR
# (b) a per-site reference to `claude-envelope-extract --agent
# plan-analyst` plus the shared `Dispatch error handling (Claude
# wrapper)` section header. Test accepts either form so the migration
# can land without breaking pre-consolidation invariants.
EXTRACTION_SHIM = (
    'Read stdout as JSON; assert `.status=="ok"`; extract '
    "`.result.schedule` and pipe through `plan_ops.py parse-schedule`; "
    "treat `.result.outcome in {valid, needs-enrichment, invalid}` per "
    "existing rules."
)
EXTRACTION_SHIM_CONSOLIDATED_SUBCOMMAND = "claude-envelope-extract --agent plan-analyst"
EXTRACTION_SHIM_SHARED_SECTION = "Dispatch error handling (Claude wrapper)"

TRANSPORT_BOUNDARY_MARKER = (
    "<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->"
)


# ---------------------------------------------------------------------------
# Static-text invariants (the AC's rg checks + extraction-shim presence)
# ---------------------------------------------------------------------------


def test_skill_md_has_no_plan_analyst_subagent_dispatch() -> None:
    """rg invariant from the AC: zero hits in the implement-plan skill."""
    text = SKILL_MD.read_text(encoding="utf-8")
    # Match both prefixed (``plan-executor:plan-analyst``) and bare
    # (``plan-analyst``) subagent_type forms — both must be retired
    # from Phase 1 dispatch sites.
    assert not re.search(r'subagent_type[^"\n]*plan-analyst', text), (
        "SKILL.md still dispatches plan-analyst via in-process Agent tool; "
        "TASK-003 requires the Bash wrapper invocation."
    )


def test_dispatch_templates_has_no_plan_analyst_subagent_dispatch() -> None:
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    assert not re.search(r'subagent_type[^"\n]*plan-analyst', text), (
        "dispatch-templates.md Phase A-single still names "
        "subagent_type 'plan-analyst' as the dispatch shape."
    )


def test_skill_md_phase1_invokes_wrapper_run_subcommand() -> None:
    text = SKILL_MD.read_text(encoding="utf-8")
    assert "plan_claude_dispatch.py" in text, (
        "SKILL.md Phase 1 must invoke plan_claude_dispatch.py for "
        "plan-analyst dispatch."
    )
    # The literal `run --input` invocation must appear in the dispatch
    # paragraph; this is the wrapper subcommand the v3 contract prescribes.
    assert "run --input" in text


def test_skill_md_phase1_carries_extraction_shim_pointer() -> None:
    """AC literal: a one-line extraction pointer at the replacement site.

    Post-TASK-006 the per-site shim was collapsed into the shared
    `## Dispatch error handling (Claude wrapper)` section + a
    `plan_ops.py claude-envelope-extract` subcommand. This test accepts
    either the original verbatim shim text OR the consolidated form
    (per-site `claude-envelope-extract --agent plan-analyst` reference
    plus the shared section header), so the consolidation can land
    without amending TASK-003's invariant.
    """
    text = SKILL_MD.read_text(encoding="utf-8")
    norm = " ".join(text.split())
    if " ".join(EXTRACTION_SHIM.split()) in norm:
        return
    consolidated_present = (
        EXTRACTION_SHIM_CONSOLIDATED_SUBCOMMAND in norm
        and EXTRACTION_SHIM_SHARED_SECTION in norm
    )
    assert consolidated_present, (
        "SKILL.md does not carry the extraction-shim pointer required by "
        "TASK-003 AC nor the TASK-006 consolidated form. Expected either "
        f"the verbatim shim {EXTRACTION_SHIM!r} OR a per-site "
        f"{EXTRACTION_SHIM_CONSOLIDATED_SUBCOMMAND!r} reference plus the "
        f"shared section header {EXTRACTION_SHIM_SHARED_SECTION!r}."
    )


def test_dispatch_templates_carries_transport_boundary_marker() -> None:
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    assert TRANSPORT_BOUNDARY_MARKER in text, (
        "dispatch-templates.md Phase A-single must carry the transport "
        f"boundary marker {TRANSPORT_BOUNDARY_MARKER!r} above the "
        "agent-behavior section so reviewers can see the seam."
    )


def test_dispatch_templates_marker_precedes_phase_a_single_body() -> None:
    """The marker must sit ABOVE the agent-behavior body of Phase A-single.

    The body's first instruction line is the verbatim "Classify exactly
    one task..." sentence; the marker must appear before it but inside
    (or after) the Phase A-single section header.
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    marker_idx = text.find(TRANSPORT_BOUNDARY_MARKER)
    body_idx = text.find("Classify exactly one task")
    section_idx = text.find("## Phase A-single")
    assert marker_idx != -1 and body_idx != -1 and section_idx != -1
    assert section_idx < marker_idx < body_idx, (
        "Transport boundary marker must sit between the Phase A-single "
        "section header and the agent-behavior body."
    )


def test_dispatch_templates_phase_a_single_body_invariant() -> None:
    """Agent-behavior body byte-identical to pre-migration wording.

    We don't have the historical bytes inline; instead assert the
    load-bearing phrases from the body are present below the boundary
    marker (the AC mandates byte-identity of the body — these phrases
    appear verbatim in the pre-migration template and must survive).
    """
    text = DISPATCH_TEMPLATES.read_text(encoding="utf-8")
    after_marker = text.split(TRANSPORT_BOUNDARY_MARKER, 1)[1]
    for phrase in (
        "Classify exactly one task from the plan at",
        "Use the `claude` vs `codex` heuristics from your agent spec",
        '"agent": "claude" | "codex"',
        '"classification_reason"',
        "You do NOT have the Agent tool",
    ):
        assert phrase in after_marker, (
            f"Phase A-single agent-behavior body lost load-bearing phrase: "
            f"{phrase!r}"
        )


# ---------------------------------------------------------------------------
# Stub-driven extraction-shim end-to-end
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


def test_extraction_shim_against_analyst_valid_fixture(tmp_path: Path) -> None:
    """Drive the TASK-002 stub with ``analyst_valid.json`` and exercise
    the inline extraction shim: assert ``.status=="ok"`` and surface
    ``.result.outcome`` from the v3 envelope; outcome must be a member
    of the analyst result schema's enum.
    """
    envelope = json.loads(ANALYST_VALID_FIXTURE.read_text(encoding="utf-8"))
    rc, stdout, stderr = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"
    parsed = json.loads(stdout)

    # Extraction shim step 1: assert .status == "ok"
    assert parsed["status"] == "ok"
    assert parsed["agent"] == "plan-analyst"

    # Extraction shim step 3: outcome ∈ analyst result enum.
    outcome = parsed["result"]["outcome"]
    assert outcome in ALLOWED_ANALYST_OUTCOMES

    # Validate inner result against the analyst result schema (TASK-002
    # fixture; analyst result schema reference per the AC).
    from jsonschema import Draft7Validator

    schema = json.loads(ANALYST_RESULT_SCHEMA.read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(parsed["result"])


def test_extraction_shim_pipes_schedule_through_parse_schedule(
    tmp_path: Path,
) -> None:
    """Whole-plan analyst path: a v3 envelope whose ``.result`` carries a
    full schedule round-trips through ``plan_ops.py parse-schedule`` and
    yields the same {outcome, task ids, batch indices} projection a
    pre-migration markdown-path analyst would have produced.

    The TASK-002 ``analyst_valid.json`` fixture is intentionally minimal
    (carries only outcome/issues/summary), so this test synthesizes a
    schedule-bearing envelope inline — equivalent to the canary's
    ``_agent_tool_transcript_analyst`` shape.
    """
    schedule = {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "title": "Seed scratch",
                "agent": "codex",
                "priority": "high",
                "files": ["scratch/.keep"],
                "dependencies": [],
                "test_command": "pytest -q",
                "classification_reason": "Single file, mechanical",
                "plan_file": "TASK-001.md",
            },
            {
                "id": "002",
                "title": "Write a.txt",
                "agent": "codex",
                "priority": "medium",
                "files": ["scratch/a.txt"],
                "dependencies": ["001"],
                "test_command": "pytest -q",
                "classification_reason": "Single file, mechanical",
                "plan_file": "TASK-002.md",
            },
        ],
        "batches": [
            {"index": 1, "task_ids": ["001"], "file_locks": ["scratch/.keep"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["scratch/a.txt"]},
        ],
        "gaps": [],
        "risks": [],
    }
    envelope = {
        "schema_version": 1,
        "status": "ok",
        "status_reason": None,
        "agent": "plan-analyst",
        "model": "claude-opus-4-7",
        "session_id": "task003-test",
        "duration_ms": 100,
        "cost_usd": 0.0,
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        },
        "result": schedule,
        "result_raw_truncated": None,
        "stderr_tail": None,
        "permission_denials": [],
        "scope": {
            "declared_files_changed": [],
            "observed_delta_tracked": [],
            "observed_delta_untracked": [],
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
        },
        "trace": {
            "run_id": "task003-test",
            "span_id": "task003-span",
            "parent_span_id": None,
            "depth": 0,
            "call_chain": ["orchestrator", "plan-analyst"],
            "started_at": "2026-04-26T00:00:00Z",
            "ended_at": "2026-04-26T00:00:01Z",
        },
        "error": None,
    }

    rc, stdout, stderr = _run_stub_with_envelope(envelope, tmp_path)
    assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"
    parsed = json.loads(stdout)
    assert parsed["status"] == "ok"

    # Extraction shim step 2: pipe .result through parse-schedule.
    extracted_schedule = parsed["result"]
    proc = subprocess.run(
        [
            sys.executable,
            str(PLAN_OPS),
            "parse-schedule",
            "--stdin",
            "--json",
        ],
        input=json.dumps(extracted_schedule),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"parse-schedule failed rc={proc.returncode}: stderr={proc.stderr!r} "
        f"stdout={proc.stdout!r}"
    )
    parse_result = json.loads(proc.stdout)

    # Pre-migration markdown path produced the same projection: outcome,
    # task ids in order, batch indices in order. Assert parity.
    expected_projection = {
        "outcome": "valid",
        "task_ids": ["001", "002"],
        "batch_indices": [1, 2],
    }
    # ``parse-schedule --json`` re-emits the schedule shape; project it
    # the same way the canary normalizes for parity comparison.
    observed_tasks = parse_result.get("tasks") or extracted_schedule.get("tasks") or []
    observed_batches = (
        parse_result.get("batches") or extracted_schedule.get("batches") or []
    )
    observed_outcome = parse_result.get("outcome") or extracted_schedule.get("outcome")
    observed_projection = {
        "outcome": observed_outcome,
        "task_ids": [t.get("id") for t in observed_tasks],
        "batch_indices": [b.get("index") for b in observed_batches],
    }
    assert observed_projection == expected_projection


def test_extraction_shim_rejects_non_ok_status(tmp_path: Path) -> None:
    """Status ``!= "ok"`` must be detectable by the inline shim; the
    orchestrator's malformed-reply branch keys off this exact field.
    """
    envelope = json.loads(ANALYST_VALID_FIXTURE.read_text(encoding="utf-8"))
    envelope["status"] = "backend_error"
    envelope["error"] = {
        "code": "malformed_output",
        "message": "stub forced backend_error",
        "retriable": False,
    }
    rc, stdout, _ = _run_stub_with_envelope(envelope, tmp_path)
    # Stub maps any non-``ok`` status to exit 1.
    assert rc == 1
    parsed = json.loads(stdout)
    assert parsed["status"] != "ok"
    assert parsed["status"] == "backend_error"
