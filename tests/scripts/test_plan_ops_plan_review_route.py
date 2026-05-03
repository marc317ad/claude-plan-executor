from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

sys.path.insert(0, str(SCRIPTS_DIR))

import plan_ops  # noqa: E402


def _flags(**overrides: bool) -> dict:
    flags = {
        "skip_plan_review": False,
        "codex_plan_review_binding": False,
        "no_auto_revise": False,
        "allow_gaps": False,
    }
    flags.update(overrides)
    return flags


def _finding(task_id: str | None, concern: str) -> dict:
    return {
        "severity": "important",
        "blocking": True,
        "section": "TASK",
        "concern": concern,
        "suggested_change": "Fix it",
        "target_task_id": task_id,
    }


FINDINGS = [
    _finding("001", "task finding"),
    _finding(None, "schedule finding"),
    _finding("002", "second task finding"),
]


def _review(verdict: str | None = "approved", *, outcome: str = "success",
            reviewer: str = "codex") -> dict:
    return {
        "outcome": outcome,
        "reviewer": reviewer,
        "verdict": verdict,
        "findings": list(FINDINGS),
        "notes": ["note"],
        "summary": "summary",
        "schedule_ok": verdict != "needs-replan",
    }


def _payload(stage: str, **overrides: object) -> dict:
    payload = {
        "stage": stage,
        "claude_only": False,
        "attempt": 1,
        "flags": _flags(),
        "plan_review_envelope": _review(),
        "plan_review_state": {
            "task_plan_file_map": {
                "001": "plans/TASK-001.md",
                "002": "plans/TASK-002.md",
            },
        },
    }
    payload.update(overrides)
    return payload


def test_pre_dispatch_skip_flag_routes_to_skip_plan_review() -> None:
    out = plan_ops.route(_payload(
        "pre_dispatch",
        flags=_flags(skip_plan_review=True),
    ))
    assert out["action"] == "skip_plan_review"
    assert out["reason"] == "flag"
    assert out["args"]["reason"] == "flag"


@pytest.mark.parametrize(
    ("claude_only", "action"),
    [
        (True, "dispatch_claude_reviewer"),
        (False, "dispatch_codex_reviewer"),
    ],
)
def test_pre_dispatch_selects_reviewer(claude_only: bool, action: str) -> None:
    out = plan_ops.route(_payload(
        "pre_dispatch",
        claude_only=claude_only,
        flags=_flags(allow_gaps=True),
    ))
    assert out["action"] == action
    if action == "dispatch_codex_reviewer":
        assert out["dispatch_context"]["allow_gaps_demotion"] is True


def test_pre_dispatch_can_select_gemini_reviewer_for_no_claude_path() -> None:
    out = plan_ops.route(_payload(
        "pre_dispatch",
        claude_only=False,
        reviewer="gemini",
        flags=_flags(allow_gaps=True),
    ))
    assert out["action"] == "dispatch_gemini_reviewer"
    assert out["dispatch_context"]["allow_gaps_demotion"] is True


@pytest.mark.parametrize("outcome", ["timeout", "parse_error", "failure"])
def test_post_review_codex_transient_outcomes_skip(outcome: str) -> None:
    out = plan_ops.route(_payload(
        "post_review",
        plan_review_envelope=_review(None, outcome=outcome, reviewer="codex"),
    ))
    assert out["action"] == "skip_plan_review"
    assert out["reason"] == "codex_unavailable"


def test_post_review_claude_failure_uses_claude_failure_reason() -> None:
    out = plan_ops.route(_payload(
        "post_review",
        claude_only=True,
        plan_review_envelope=_review(None, outcome="failure", reviewer="claude"),
    ))
    assert out["action"] == "skip_plan_review"
    assert out["reason"] == "claude_review_failure"


@pytest.mark.parametrize("verdict", ["approved", "approved-with-notes"])
def test_post_review_approved_verdicts_proceed(verdict: str) -> None:
    out = plan_ops.route(_payload(
        "post_review",
        plan_review_envelope=_review(verdict),
    ))
    assert out["action"] == "proceed_to_phase_2"
    assert out["summary_section"]["findings_count"] == len(FINDINGS)


@pytest.mark.parametrize(
    ("flags", "reason_detail"),
    [
        (_flags(codex_plan_review_binding=True), "binding_flag"),
        (_flags(no_auto_revise=True), "no_auto_revise"),
    ],
)
def test_post_review_needs_replan_halt_gates(flags: dict, reason_detail: str) -> None:
    out = plan_ops.route(_payload(
        "post_review",
        flags=flags,
        plan_review_envelope=_review("needs-replan"),
    ))
    assert out["action"] == "halt_plan_review_failed"
    assert out["args"]["reason_detail"] == reason_detail


def test_post_review_needs_replan_first_attempt_dispatches_triage() -> None:
    out = plan_ops.route(_payload(
        "post_review",
        attempt=1,
        plan_review_envelope=_review("needs-replan"),
    ))
    assert out["action"] == "dispatch_triage"
    assert out["dispatch_context"]["findings_for_payload"] == FINDINGS


def test_post_review_second_needs_replan_halts_without_second_triage() -> None:
    out = plan_ops.route(_payload(
        "post_review",
        attempt=2,
        plan_review_envelope=_review("needs-replan"),
    ))
    assert out["action"] == "halt_plan_review_failed"
    assert out["args"]["reason_detail"] == "second_needs_replan"


def test_post_review_second_needs_replan_can_derive_from_state() -> None:
    out = plan_ops.route(_payload(
        "post_review",
        attempt=None,
        plan_review_envelope=_review("needs-replan"),
        plan_review_state={"auto_revise_round_completed": True},
    ))
    assert out["action"] == "halt_plan_review_failed"
    assert out["args"]["reason_detail"] == "second_needs_replan"


def test_post_triage_ship_proceeds_with_disagreement_banner() -> None:
    out = plan_ops.route(_payload(
        "post_triage",
        plan_review_envelope=_review("needs-replan"),
        triage_envelope={"verdict": "ship", "summary": "disagree"},
    ))
    assert out["action"] == "proceed_to_phase_2"
    assert out["summary_section"]["banner"] == "[plan-review-disagreement]"


def test_post_triage_ship_with_fixes_proceeds_with_notes_section() -> None:
    out = plan_ops.route(_payload(
        "post_triage",
        plan_review_envelope=_review("needs-replan"),
        triage_envelope={"verdict": "ship-with-fixes", "summary": "notes"},
    ))
    assert out["action"] == "proceed_to_phase_2"
    assert out["summary_section"]["notes_section"] == "Plan review notes"


def test_post_triage_partial_agreement_filters_load_bearing_and_context() -> None:
    out = plan_ops.route(_payload(
        "post_triage",
        plan_review_envelope=_review("needs-replan"),
        triage_envelope={
            "verdict": "partial-agreement",
            "load_bearing": [0, 2],
            "dismissed": [1],
            "summary": "split",
        },
    ))
    assert out["action"] == "dispatch_plan_author_per_finding"
    ctx = out["dispatch_context"]
    assert ctx["findings_for_payload"] == [FINDINGS[0], FINDINGS[2]]
    assert ctx["dismissed_for_context"] == [FINDINGS[1]]
    dispatches = ctx["per_finding_dispatches"]
    assert len(dispatches) == 2
    assert dispatches[0]["target_task_id"] == "001"
    assert dispatches[0]["variant"] == "A"
    assert dispatches[0]["child_plan_file"] == "plans/TASK-001.md"
    assert dispatches[1]["target_task_id"] == "002"
    assert dispatches[1]["variant"] == "A"


def test_post_triage_needs_rework_forwards_full_findings_with_variants() -> None:
    out = plan_ops.route(_payload(
        "post_triage",
        plan_review_envelope=_review("needs-replan"),
        triage_envelope={"verdict": "needs-rework", "summary": "agree"},
    ))
    assert out["action"] == "dispatch_plan_author_per_finding"
    ctx = out["dispatch_context"]
    assert ctx["findings_for_payload"] == FINDINGS
    dispatches = ctx["per_finding_dispatches"]
    assert [d["variant"] for d in dispatches] == ["A", "B", "A"]
    assert dispatches[1]["target_task_id"] is None
    assert dispatches[1]["child_plan_file"] is None


def test_post_plan_author_routes_to_second_pass_rerun() -> None:
    out = plan_ops.route(_payload("post_plan_author"))
    assert out["action"] == "rerun_analyst_then_review"
    assert out["args"]["next_attempt"] == 2
    assert out["args"]["binding_second_pass"] is True


def test_manual_pause_action_is_reachable() -> None:
    out = plan_ops.route(_payload("manual_pause", reason="operator check"))
    assert out["action"] == "pause_awaiting_user"
    assert out["reason"] == "operator check"


def test_unknown_plan_review_verdict_routes_to_unknown_state() -> None:
    out = plan_ops.route(_payload(
        "post_review",
        plan_review_envelope=_review("surprising"),
    ))
    assert out["action"] == "unknown_state"
    assert "surprising" in out["reason"]


def test_unknown_triage_verdict_routes_to_unknown_state() -> None:
    out = plan_ops.route(_payload(
        "post_triage",
        triage_envelope={"verdict": "surprising"},
    ))
    assert out["action"] == "unknown_state"
    assert "surprising" in out["reason"]


def _run_cli(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PY), str(SCRIPT), "plan-review-route", "--stdin", "--json"],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_cli_happy_path_emits_route() -> None:
    cp = _run_cli(json.dumps(_payload("pre_dispatch", claude_only=False)))
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["action"] == "dispatch_codex_reviewer"


def test_cli_schema_violation_fails_with_structured_errors() -> None:
    cp = _run_cli(json.dumps({"stage": "pre_dispatch", "flags": []}))
    assert cp.returncode != 0
    out = json.loads(cp.stdout)
    assert "errors" in out
    assert out["errors"][0]["path"] == "$.flags"
