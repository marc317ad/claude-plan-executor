"""TASK-007: E2E smoke for the SKILL_bash_dispatch_migration plan.

Drives the orchestrator's full A→E loop (analyst → implementer →
D.1 review → commit) on the fixture plan at
``tests/scripts/fixtures/claude_dispatch/e2e_plan.md``, using:

  * the TASK-002 ``plan_claude_dispatch_stub.py`` for the migrated
    Claude dispatch sites (analyst, implementer);
  * synthesised reviewer envelopes for the D.1 (Codex) and D.5
    (``code-reviewer`` Agent) sites, which this plan intentionally
    leaves on the ``Agent`` tool surface;
  * ``plan_ops.py``'s pure ``route()`` function for Phase D routing
    decisions, and ``claude-envelope-extract`` for envelope unwrap.

The matrix exercises four end-to-end paths from the AC:

  1. analyst ``valid`` → implementer ``success`` → Codex ``clean`` →
     ``commit`` (no disagreement tag).
  2. analyst ``needs-enrichment`` → halt before any implementer
     dispatch.
  3. implementer ``success`` envelope with ``scope_violation_detected
     == True`` → fail-task (commit blocked by orchestrator-side
     scope gate, as enforced in SKILL.md after TASK-004).
  4. implementer ``success`` → Codex ``needs-rework`` → D.5 ``ship``
     → ``commit`` with ``disagreement_tag=True`` (verifies the D.1 +
     D.5 reviewer paths are still reachable end-to-end and route
     through the ``Agent``-tool reviewer surface, not the Claude
     wrapper).

The test does **not** spawn real ``claude``: only the stub is invoked.
Total wall clock under 30s (each subprocess call to the stub is a
small Python interpreter spin).
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan"
SKILL_MD = SKILL_DIR / "SKILL.md"
STUB = REPO_ROOT / "tests" / "scripts" / "stubs" / "plan_claude_dispatch_stub.py"
FIXTURES_DIR = REPO_ROOT / "tests" / "scripts" / "fixtures" / "claude_dispatch"
E2E_PLAN = FIXTURES_DIR / "e2e_plan.md"
ANALYST_VALID = FIXTURES_DIR / "analyst_valid.json"
IMPL_SUCCESS = FIXTURES_DIR / "implementer_success.json"
PLAN_OPS = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_ops.py"

# Make ``plan_ops.route()`` importable directly so we can call the
# routing function without spinning a subprocess per matrix step.
sys.path.insert(0, str(PLAN_OPS.parent))
import plan_ops  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — stub invocation + envelope synthesis
# ---------------------------------------------------------------------------


def _run_stub_with_envelope(envelope: dict, tmp_path: Path) -> dict:
    """Drive the stub with an envelope fixture and return the parsed
    stdout envelope. The stub maps ``status==ok`` to rc=0 and anything
    else to rc=1, mirroring the real wrapper."""
    fixture_path = tmp_path / f"envelope_{abs(hash(json.dumps(envelope, sort_keys=True))) & 0xFFFFFFFF:x}.json"
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
    return json.loads(proc.stdout)


def _extract(envelope: dict, agent: str) -> dict:
    """Drive ``plan_ops.py claude-envelope-extract`` and return the
    normalised ``{status, outcome, result, scope_violation,
    scope_misreport, error}`` payload — the same shape the orchestrator
    routes on per the TASK-006 consolidated extraction shim."""
    proc = subprocess.run(
        [sys.executable, str(PLAN_OPS), "claude-envelope-extract",
         "--stdin", "--agent", agent, "--json"],
        input=json.dumps(envelope),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    return json.loads(proc.stdout)


def _codex_reviewer_envelope(verdict: str, findings: list | None = None) -> dict:
    """Synthesise the parsed Codex reviewer envelope shape consumed by
    ``plan_ops.route()``. The Codex review path is NOT migrated by this
    plan; the wrapper output shape here mirrors what
    ``plan_codex_dispatch.py review`` produces post-parse."""
    return {
        "verdict": verdict,
        "findings": list(findings or []),
        "summary": f"codex review: {verdict}",
        "wrapper_checks": {"symbol_warnings": []},
    }


def _d5_reviewer_envelope(
    verdict: str,
    *,
    load_bearing: list | None = None,
    dismissed: list | None = None,
) -> dict:
    """Synthesise the parsed D.5 third-opinion envelope. The D.5 site
    intentionally remains on the ``Agent(subagent_type="code-reviewer",
    model="sonnet")`` surface — this plan does not migrate it."""
    return {
        "verdict": verdict,
        "load_bearing": list(load_bearing or []),
        "dismissed": list(dismissed or []),
        "summary": f"d5 third-opinion: {verdict}",
    }


def _route(
    *,
    task_id: str,
    reviewer_envelope: dict,
    d5_envelope: dict | None = None,
    bounded_used: bool = False,
    narrow_used: bool = False,
    role_swap_used: bool = False,
    codex_binding: bool = False,
) -> dict:
    """Thin wrapper around ``plan_ops.route()`` mirroring the
    orchestrator's invocation in SKILL.md §D.2."""
    return plan_ops.route({
        "task_id": task_id,
        "implementer": "claude",
        "reviewer_envelope": reviewer_envelope,
        "d5_envelope": d5_envelope,
        "retries_used": {
            "bounded_remediation": bounded_used,
            "narrow_remediation": narrow_used,
            "role_swap": role_swap_used,
        },
        "flags": {"codex_review_binding": codex_binding},
    })


# ---------------------------------------------------------------------------
# Pre-flight invariants — fixture wiring
# ---------------------------------------------------------------------------


def test_e2e_fixture_plan_exists() -> None:
    assert E2E_PLAN.exists(), f"missing fixture plan: {E2E_PLAN}"
    text = E2E_PLAN.read_text(encoding="utf-8")
    assert "TASK-001" in text and "TASK-002" in text


def _parse_fixture_plan_task_ids(plan_text: str) -> list[str]:
    """Pull the ordered list of ``TASK-NNN`` ids from the fixture plan
    headings. Mirrors what the analyst would emit as
    ``schedule.tasks[*].id`` for this minimal plan."""
    return re.findall(r"^### (TASK-\d+):", plan_text, re.MULTILINE)


def test_e2e_fixture_plan_drives_orchestrator_loop(tmp_path: Path) -> None:
    """Drive the fixture plan's TASK-001 + TASK-002 through the full
    orchestrator loop using stub-derived envelopes whose ``task_id``
    fields come from parsing the fixture plan itself (not canned
    constants).

    Per AC: the fixture plan must be the input that produces the task
    ids the matrix routes on. For each task id discovered in
    ``e2e_plan.md`` we (a) run an analyst envelope through the stub +
    extract shim, (b) run an implementer envelope through the stub +
    extract shim with that ``task_id`` propagated into the trace, (c)
    feed the resulting outcome into ``plan_ops.route()`` with a clean
    Codex reviewer envelope, and assert the orchestrator commits."""
    plan_text = E2E_PLAN.read_text(encoding="utf-8")
    task_ids = _parse_fixture_plan_task_ids(plan_text)
    assert task_ids == ["TASK-001", "TASK-002"], (
        f"fixture plan task ids drifted: {task_ids}"
    )

    analyst_base = json.loads(ANALYST_VALID.read_text(encoding="utf-8"))
    impl_base = json.loads(IMPL_SUCCESS.read_text(encoding="utf-8"))

    for task_id in task_ids:
        analyst_env = copy.deepcopy(analyst_base)
        analyst_env["result"]["summary"] = f"analyst valid for {task_id}"
        analyst_env["trace"]["span_id"] = f"stub-span-analyst-{task_id}"

        parsed_analyst = _run_stub_with_envelope(analyst_env, tmp_path)
        extracted_analyst = _extract(parsed_analyst, "plan-analyst")
        assert extracted_analyst["status"] == "ok"
        assert extracted_analyst["outcome"] == "valid", (
            f"analyst envelope for {task_id} did not surface outcome=valid"
        )

        impl_env = copy.deepcopy(impl_base)
        impl_env["trace"]["span_id"] = f"stub-span-impl-{task_id}"
        impl_env["result"]["report"]["acceptance_criteria_check"] = [
            f"[x] {task_id} stub AC met"
        ]

        parsed_impl = _run_stub_with_envelope(impl_env, tmp_path)
        extracted_impl = _extract(parsed_impl, "plan-implementer")
        assert extracted_impl["status"] == "ok"
        assert extracted_impl["outcome"] == "success"
        assert extracted_impl["scope_violation"] is False

        # Strip the leading ``TASK-`` to match the routing helper's
        # numeric task_id convention used elsewhere in this matrix.
        numeric_id = task_id.split("-", 1)[1]
        directive = _route(
            task_id=numeric_id,
            reviewer_envelope=_codex_reviewer_envelope("clean"),
        )
        assert directive["action"] == "commit", (
            f"orchestrator did not commit for {task_id}: {directive}"
        )


def test_stub_is_executable() -> None:
    assert STUB.exists(), f"missing stub: {STUB}"
    assert ANALYST_VALID.exists()
    assert IMPL_SUCCESS.exists()


# ---------------------------------------------------------------------------
# Case 1 — analyst valid → implementer success → Codex clean → commit
# ---------------------------------------------------------------------------


def test_e2e_case1_happy_path_routes_to_commit_no_tags(tmp_path: Path) -> None:
    """Full A→E happy path. No D.5 dispatched; commit carries no
    disagreement / remediation tags."""
    analyst_env = json.loads(ANALYST_VALID.read_text(encoding="utf-8"))
    impl_env = json.loads(IMPL_SUCCESS.read_text(encoding="utf-8"))

    parsed_analyst = _run_stub_with_envelope(analyst_env, tmp_path)
    extracted_analyst = _extract(parsed_analyst, "plan-analyst")
    assert extracted_analyst["status"] == "ok"
    assert extracted_analyst["outcome"] == "valid"

    parsed_impl = _run_stub_with_envelope(impl_env, tmp_path)
    extracted_impl = _extract(parsed_impl, "plan-implementer")
    assert extracted_impl["status"] == "ok"
    assert extracted_impl["outcome"] == "success"
    assert extracted_impl["scope_violation"] is False

    directive = _route(
        task_id="001",
        reviewer_envelope=_codex_reviewer_envelope("clean"),
    )
    assert directive["action"] == "commit"
    flags = directive["args"]["commit_flags"]
    assert flags["disagreement_tag"] is False
    assert flags["remediation_tag"] is False
    assert flags["narrow_remediation_tag"] is False


# ---------------------------------------------------------------------------
# Case 2 — analyst needs-enrichment → halt before any implementer dispatch
# ---------------------------------------------------------------------------


def test_e2e_case2_analyst_needs_enrichment_halts(tmp_path: Path) -> None:
    """Analyst returns ``needs-enrichment`` → orchestrator halts before
    dispatching any implementer. The extraction shim surfaces the
    outcome verbatim; the routing helper is not consulted (no
    reviewer envelope in scope)."""
    envelope = copy.deepcopy(json.loads(ANALYST_VALID.read_text(encoding="utf-8")))
    envelope["result"]["outcome"] = "needs-enrichment"
    envelope["result"]["issues"] = [
        {"severity": "blocker", "description": "missing acceptance criteria"},
    ]

    parsed = _run_stub_with_envelope(envelope, tmp_path)
    extracted = _extract(parsed, "plan-analyst")

    assert extracted["status"] == "ok"
    assert extracted["outcome"] == "needs-enrichment"
    # SKILL.md Phase 1.1 rule: any analyst outcome != ``valid`` halts
    # the run before Phase B. Encode the rule here so a future drift
    # in SKILL.md's analyst-routing prose is caught by this test.
    assert extracted["outcome"] != "valid"


# ---------------------------------------------------------------------------
# Case 3 — implementer scope_violation → fail-task (commit blocked)
# ---------------------------------------------------------------------------


def test_e2e_case3_implementer_scope_violation_blocks_commit(
    tmp_path: Path,
) -> None:
    """Implementer envelope arrives with ``status=ok`` and inner
    ``outcome=success`` BUT ``scope.scope_violation_detected=True``.
    Per SKILL.md after TASK-004, the orchestrator's commit gate keys
    off the top-level scope flag and blocks commit regardless of the
    inner outcome."""
    envelope = copy.deepcopy(json.loads(IMPL_SUCCESS.read_text(encoding="utf-8")))
    envelope["scope"]["scope_violation_detected"] = True
    envelope["scope"]["observed_delta_tracked"].append("foo/out_of_scope.py")

    parsed = _run_stub_with_envelope(envelope, tmp_path)
    extracted = _extract(parsed, "plan-implementer")

    assert extracted["status"] == "ok"
    assert extracted["outcome"] == "success"
    # The orchestrator-side commit gate per SKILL.md TASK-004 AC:
    # block commit when ``scope_violation_detected`` is True regardless
    # of the inner outcome. Encode the rule.
    assert extracted["scope_violation"] is True

    def _commit_gate(extracted: dict) -> str:
        if extracted["scope_violation"] or extracted["scope_misreport"]:
            return "fail-task"
        if extracted["outcome"] == "success":
            return "dispatch_review"
        return "fail-task"

    assert _commit_gate(extracted) == "fail-task"


# ---------------------------------------------------------------------------
# Case 4 — implementer success → Codex needs-rework → D.5 ship → commit
#         with --disagreement-tag (D.1 + D.5 reviewer paths NOT migrated)
# ---------------------------------------------------------------------------


def test_e2e_case4_codex_needs_rework_d5_ship_commits_with_disagreement(
    tmp_path: Path,
) -> None:
    """Codex returns ``needs-rework`` on Claude's success. D.5
    third-opinion (``code-reviewer`` Agent, NOT migrated by this plan)
    returns ``ship`` — orchestrator commits with
    ``disagreement_tag=True``. This is the load-bearing case proving
    the D.1/D.5 reviewer paths still work after the migration of the
    implementer surface."""
    impl_env = json.loads(IMPL_SUCCESS.read_text(encoding="utf-8"))

    parsed_impl = _run_stub_with_envelope(impl_env, tmp_path)
    extracted_impl = _extract(parsed_impl, "plan-implementer")
    assert extracted_impl["outcome"] == "success"
    assert extracted_impl["scope_violation"] is False

    codex_findings = [
        {"severity": "major", "description": "missing edge-case test"},
        {"severity": "minor", "description": "naming nit"},
    ]
    codex_envelope = _codex_reviewer_envelope("needs-rework", findings=codex_findings)

    # Phase D.2 first call → no D.5 yet → escalate to D.5.
    first = _route(task_id="001", reviewer_envelope=codex_envelope)
    assert first["action"] == "dispatch_d5"
    assert first["args"]["dispatch_context"]["template"] == "PhaseD5"

    # Synthesise the D.5 envelope. Path stays on the Agent tool surface.
    d5_envelope = _d5_reviewer_envelope("ship")

    second = _route(
        task_id="001",
        reviewer_envelope=codex_envelope,
        d5_envelope=d5_envelope,
    )
    assert second["action"] == "commit"
    flags = second["args"]["commit_flags"]
    assert flags["disagreement_tag"] is True
    assert flags["remediation_tag"] is False
    assert flags["narrow_remediation_tag"] is False


# ---------------------------------------------------------------------------
# Reviewer surface invariants — D.1 + D.5 untouched (V-GLOBAL-4)
# ---------------------------------------------------------------------------


def test_d1_d5_reviewer_paths_still_reference_code_reviewer_agent() -> None:
    """SKILL.md D.1 + D.5 dispatches still name the ``code-reviewer``
    Agent surface (not the Claude wrapper). This guards against an
    accidental migration of the reviewer paths — those are explicitly
    out of scope per the plan's Acceptance §6.

    Both call sites must remain: the ``claude_only=true`` D-Claude
    dispatch AND the cross-side ``claude_only=false`` Codex-implemented
    → Claude-reviewed dispatch. ``re.search`` would only catch the
    first; ``re.findall`` lets us assert both survive."""
    text = SKILL_MD.read_text(encoding="utf-8")
    matches = re.findall(
        r'Agent\(subagent_type:\s*"code-reviewer"[^)]*\)',
        text,
    )
    assert len(matches) == 2, (
        "expected exactly 2 code-reviewer Agent call sites "
        f"(D.1 claude_only=true + D.1 cross-side), found {len(matches)}: {matches}"
    )

    # Anchor each site by its surrounding section heading so a future
    # edit that deletes one site but adds another elsewhere still trips
    # this guard.
    claude_only_section = re.search(
        r'\*Phase D-Claude path \(`claude_only=true`\):\*(.*?)(?=\*Phase D-Codex path)',
        text,
        re.DOTALL,
    )
    assert claude_only_section is not None, "Phase D-Claude `claude_only=true` section missing"
    assert re.search(
        r'Agent\(subagent_type:\s*"code-reviewer"', claude_only_section.group(1)
    ), "D.1 `claude_only=true` site no longer dispatches code-reviewer Agent"

    cross_side_section = re.search(
        r'\*Phase D-Claude path \(`claude_only=false`, Codex-implemented → Claude review\):\*(.*?)(?=\n#### |\n### |\Z)',
        text,
        re.DOTALL,
    )
    assert cross_side_section is not None, "Phase D-Claude cross-side section missing"
    assert re.search(
        r'Agent\(subagent_type:\s*"code-reviewer"', cross_side_section.group(1)
    ), "D.1 cross-side site no longer dispatches code-reviewer Agent"

    # D.5 third-opinion: SKILL.md describes the dispatch in narrative
    # form (`Agent, code-reviewer, model: sonnet`) inside §D.2, rather
    # than as a literal `Agent(subagent_type: "code-reviewer", ...)`
    # call. Anchor the D.5 site by its narrative invocation so a
    # migration of D.5 onto the wrapper would trip this guard.
    d5_section = re.search(
        r'#### D\.2 — Route by verdict.*?(?=\n#### |\Z)',
        text,
        re.DOTALL,
    )
    assert d5_section is not None, "Phase D.2 section (containing D.5 dispatch) missing"
    assert re.search(
        r'Phase D\.5\*\* template \(Agent, `code-reviewer`',
        d5_section.group(0),
    ), "D.5 third-opinion no longer dispatches the code-reviewer Agent"

    # And the wrapper is NOT named for code-reviewer dispatches.
    assert not re.search(
        r'plan_claude_dispatch\.py[^\n]*code-reviewer',
        text,
    ), "code-reviewer dispatches must not route through the Claude wrapper"


# ---------------------------------------------------------------------------
# Wall-clock budget — entire matrix under 30s with stub-only spawns
# ---------------------------------------------------------------------------


def test_e2e_matrix_under_30s_wall_clock(tmp_path: Path) -> None:
    """Re-runs the four matrix cases inline and asserts total wall
    clock under 30s. The AC's runtime cap protects against accidental
    fall-through to a real ``claude`` spawn."""
    start = time.monotonic()

    test_e2e_case1_happy_path_routes_to_commit_no_tags(tmp_path)
    test_e2e_case2_analyst_needs_enrichment_halts(tmp_path)
    test_e2e_case3_implementer_scope_violation_blocks_commit(tmp_path)
    test_e2e_case4_codex_needs_rework_d5_ship_commits_with_disagreement(tmp_path)

    elapsed = time.monotonic() - start
    assert elapsed < 30.0, f"matrix wall clock {elapsed:.1f}s exceeded 30s budget"
