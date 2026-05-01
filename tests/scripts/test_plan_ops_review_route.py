"""Unit tests for `plan_ops.py review-route` (TASK-001 PHASE_D_STATE_MACHINE).

The router is a pure function `plan_ops.route(payload: dict) -> dict`. These
tests call `route()` directly — no subprocess, no stdin monkey-patching.

Coverage:
  * Every routing cell from SKILL §§D.2 / D.2a / D.2a.5 / D.2a.6 / D.2b.
  * `--codex-review-binding` short-circuit (claim AC line 4).
  * `retries_used.bounded_remediation=true` + Codex `needs-rework` →
    `pause_awaiting_user` with `stage="post_remediation_review"` (AC line 5).
  * commit_flags XOR composition (AC line 3).
  * Input-schema violations exit non-zero with structured `errors[*]`
    (AC line 6) — exercised against the CLI shim via subprocess.
  * Unknown enum values → `action: unknown_state` with `reason` (AC line 7).
"""

from __future__ import annotations

import json
import os
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


# ---------------------------------------------------------------------------
# Fixture helpers — minimal envelopes, one per routing cell.
# ---------------------------------------------------------------------------


def _retries(**overrides: bool) -> dict:
    base = {
        "bounded_remediation": False,
        "narrow_remediation": False,
        "role_swap": False,
        "codex_fallback": False,
    }
    base.update(overrides)
    return base


def _flags(**overrides: bool) -> dict:
    base = {"codex_review_binding": False, "skip_cross_review": False}
    base.update(overrides)
    return base


def _payload(*, implementer: str, verdict: str,
             findings: list | None = None,
             d5: dict | None = None,
             retries: dict | None = None,
             flags: dict | None = None,
             unattended_revert_policy: str | None = None,
             task_id: str = "001") -> dict:
    payload = {
        "task_id": task_id,
        "implementer": implementer,
        "reviewer_envelope": {
            "verdict": verdict,
            "findings": findings if findings is not None else [],
            "summary": "",
        },
        "d5_envelope": d5,
        "retries_used": retries if retries is not None else _retries(),
        "flags": flags if flags is not None else _flags(),
    }
    if unattended_revert_policy is not None:
        payload["unattended_revert_policy"] = unattended_revert_policy
    return payload


# ---------------------------------------------------------------------------
# Branch 1 — Claude implementer, Codex reviewer (§D.2 + §D.2a ladder).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["clean", "minor-findings"])
def test_claude_codex_clean_or_minor_findings_commits(verdict: str) -> None:
    out = plan_ops.route(_payload(implementer="claude", verdict=verdict))
    assert out["action"] == "commit"
    cf = out["args"]["commit_flags"]
    assert cf == {
        "disagreement_tag": False,
        "remediation_tag": False,
        "narrow_remediation_tag": False,
        "dismissed_finding_ids": [],
    }


def test_claude_codex_needs_rework_no_d5_dispatches_d5() -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        findings=[{"index": 0, "message": "x"}],
    ))
    assert out["action"] == "dispatch_d5"
    ctx = out["args"]["dispatch_context"]
    assert ctx["template"] == "PhaseD5"
    assert ctx["findings_for_retry"] == [{"index": 0, "message": "x"}]
    assert "wrapper_checks" in ctx


def test_codex_review_binding_defaults_to_pause_no_d5() -> None:
    """Binding mode defaults to `pause` for backward compatibility."""
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        flags=_flags(codex_review_binding=True),
    ))
    assert out["action"] == "pause_awaiting_user"
    assert out["args"]["policy_kind"] == "binding_policy"
    assert out["args"]["unattended_revert_policy"] == "pause"
    assert out["args"]["pause_payload"]["stage"] == "post_binding_block"
    assert "dispatch_context" not in out["args"]


@pytest.mark.parametrize(
    ("policy", "action", "authorization_source"),
    [
        ("pause", "pause_awaiting_user", None),
        ("fail-fast", "fail", "unattended-fail-fast"),
        ("preserve-only", "fail", "unattended-preserve-only"),
    ],
)
def test_codex_review_binding_respects_unattended_policy(
    policy: str, action: str, authorization_source: str | None,
) -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        findings=[{"i": 0}],
        flags=_flags(codex_review_binding=True),
        unattended_revert_policy=policy,
    ))
    assert out["action"] == action
    assert out["action"] not in {
        "dispatch_d5",
        "dispatch_bounded_remediation",
        "dispatch_narrow_remediation",
    }
    assert out["args"]["policy_kind"] == "binding_policy"
    assert out["args"]["unattended_revert_policy"] == policy
    assert "dispatch_context" not in out["args"]
    if action == "pause_awaiting_user":
        assert out["args"]["pause_payload"]["stage"] == "post_binding_block"
        assert out["args"]["pause_payload"]["policy_kind"] == "binding_policy"
        assert out["args"]["pause_payload"]["codex_findings"] == [{"i": 0}]
        assert "authorization_source" not in out["args"]
    else:
        assert out["args"]["fail_stage"] == "review"
        assert out["args"]["authorization_source"] == authorization_source
        assert "codex-review-binding" in out["args"]["fail_reason"]


@pytest.mark.parametrize("d5_verdict", ["ship", "ship-with-fixes"])
def test_d2a_d5_ship_commits_with_disagreement_tag(d5_verdict: str) -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        findings=[{"i": 0}],
        d5={"verdict": d5_verdict, "load_bearing": [], "dismissed": [],
            "summary": "D5 disagrees"},
    ))
    assert out["action"] == "commit"
    cf = out["args"]["commit_flags"]
    assert cf["disagreement_tag"] is True
    assert cf["remediation_tag"] is False
    assert cf["narrow_remediation_tag"] is False
    assert cf["dismissed_finding_ids"] == []


def test_d2a6_partial_agreement_first_attempt_dispatches_narrow() -> None:
    findings = [{"i": 0, "msg": "load"}, {"i": 1, "msg": "dismiss"}, {"i": 2, "msg": "load2"}]
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework", findings=findings,
        d5={"verdict": "partial-agreement", "load_bearing": [0, 2],
            "dismissed": [1], "summary": "split"},
    ))
    assert out["action"] == "dispatch_narrow_remediation"
    ctx = out["args"]["dispatch_context"]
    assert ctx["template"] == "PhaseB-narrow-remediation"
    assert ctx["findings_for_retry"] == [findings[0], findings[2]]
    assert ctx["dismissed_for_context"] == [findings[1]]
    assert ctx["d5_summary"] == "split"


def test_d2a6_partial_agreement_after_retry_pauses() -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        findings=[{"i": 0}],
        d5={"verdict": "partial-agreement", "load_bearing": [0],
            "dismissed": [], "summary": "s"},
        retries=_retries(narrow_remediation=True),
    ))
    assert out["action"] == "pause_awaiting_user"
    pp = out["args"]["pause_payload"]
    assert pp["stage"] == "post_narrow_remediation_review"
    assert pp["codex_findings"] == [{"i": 0}]
    assert pp["d5_summary"] == "s"
    assert pp["dismissed_finding_indices"] == []


def test_d2a5_d5_needs_rework_first_attempt_dispatches_bounded() -> None:
    findings = [{"i": 0}]
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework", findings=findings,
        d5={"verdict": "needs-rework", "load_bearing": [], "dismissed": [],
            "summary": "agree"},
    ))
    assert out["action"] == "dispatch_bounded_remediation"
    ctx = out["args"]["dispatch_context"]
    assert ctx["template"] == "PhaseB-rework"
    assert ctx["findings_for_retry"] == findings
    assert ctx["d5_summary"] == "agree"


def test_d2a5_bounded_already_used_pauses_post_remediation_review() -> None:
    """AC: `retries_used.bounded_remediation=true` + Codex `needs-rework` →
    pause_awaiting_user with stage=post_remediation_review."""
    findings = [{"i": 0}]
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework", findings=findings,
        d5={"verdict": "needs-rework", "load_bearing": [], "dismissed": [],
            "summary": "agree"},
        retries=_retries(bounded_remediation=True),
    ))
    assert out["action"] == "pause_awaiting_user"
    pp = out["args"]["pause_payload"]
    assert pp["stage"] == "post_remediation_review"
    assert pp["codex_findings"] == findings
    assert pp["d5_summary"] == "agree"


# ---------------------------------------------------------------------------
# Branch 2 — Codex implementer, Claude reviewer (§D.2 + §D.2b).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["ship", "ship-with-fixes"])
def test_codex_claude_ship_or_ship_with_fixes_commits(verdict: str) -> None:
    out = plan_ops.route(_payload(implementer="codex", verdict=verdict))
    assert out["action"] == "commit"
    cf = out["args"]["commit_flags"]
    assert cf["disagreement_tag"] is False
    assert cf["remediation_tag"] is False
    assert cf["narrow_remediation_tag"] is False


def test_codex_claude_needs_rework_first_attempt_dispatches_role_swap() -> None:
    findings = [{"i": 0}]
    out = plan_ops.route(_payload(
        implementer="codex", verdict="needs-rework", findings=findings,
    ))
    assert out["action"] == "dispatch_role_swap"
    ctx = out["args"]["dispatch_context"]
    assert ctx["template"] == "PhaseB-rework"
    assert ctx["findings_for_retry"] == findings


def test_codex_claude_needs_rework_role_swap_used_fails() -> None:
    out = plan_ops.route(_payload(
        implementer="codex", verdict="needs-rework",
        retries=_retries(role_swap=True),
    ))
    assert out["action"] == "fail"
    assert out["args"]["fail_stage"] == "review"
    assert "role-swap" in out["args"]["fail_reason"]


# ---------------------------------------------------------------------------
# commit_flags XOR composition (AC line 3).
# ---------------------------------------------------------------------------


def test_commit_flags_xor_invariant_clean_path() -> None:
    """Clean / minor-findings → no tags at all."""
    out = plan_ops.route(_payload(implementer="claude", verdict="clean"))
    cf = out["args"]["commit_flags"]
    assert not (cf["narrow_remediation_tag"] and cf["remediation_tag"])
    assert not cf["dismissed_finding_ids"] or cf["narrow_remediation_tag"]


def test_commit_flags_xor_invariant_disagreement_path() -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        d5={"verdict": "ship", "load_bearing": [], "dismissed": [], "summary": ""},
    ))
    cf = out["args"]["commit_flags"]
    # disagreement_tag set; narrow XOR remediation respected; dismissed empty.
    assert cf["disagreement_tag"] is True
    assert not (cf["narrow_remediation_tag"] and cf["remediation_tag"])
    assert cf["dismissed_finding_ids"] == []
    # dismissed_finding_ids requires narrow_remediation_tag — empty so fine.


# ---------------------------------------------------------------------------
# unknown_state branches (AC line 7 + ≥1 unknown_state coverage).
# ---------------------------------------------------------------------------


def test_unknown_codex_verdict_routes_to_unknown_state() -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="completely-bogus-verdict",
    ))
    assert out["action"] == "unknown_state"
    assert "completely-bogus-verdict" in out["reason"]
    assert out["args"]["pause_payload"]["stage"] == "unknown_state"


def test_unknown_claude_verdict_routes_to_unknown_state() -> None:
    out = plan_ops.route(_payload(
        implementer="codex", verdict="not-a-real-verdict",
    ))
    assert out["action"] == "unknown_state"
    assert "not-a-real-verdict" in out["reason"]


def test_unknown_d5_verdict_routes_to_unknown_state() -> None:
    out = plan_ops.route(_payload(
        implementer="claude", verdict="needs-rework",
        d5={"verdict": "weird-d5-verdict", "load_bearing": [], "dismissed": [],
            "summary": ""},
    ))
    assert out["action"] == "unknown_state"
    assert "weird-d5-verdict" in out["reason"]


def test_unknown_implementer_routes_to_unknown_state() -> None:
    out = plan_ops.route({
        "task_id": "001",
        "implementer": "gemini",
        "reviewer_envelope": {"verdict": "clean"},
        "d5_envelope": None,
        "retries_used": _retries(),
        "flags": _flags(),
    })
    assert out["action"] == "unknown_state"
    assert "gemini" in out["reason"]


# ---------------------------------------------------------------------------
# CLI shim — schema violation → non-zero exit + structured errors[*].
# ---------------------------------------------------------------------------


def _run_cli(stdin_text: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PY), str(SCRIPT), "review-route", "--stdin", "--json"],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_cli_happy_path_emits_directive_on_stdout() -> None:
    payload = _payload(implementer="claude", verdict="clean")
    cp = _run_cli(json.dumps(payload))
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["action"] == "commit"


def test_cli_schema_violation_missing_required_fails_with_errors() -> None:
    cp = _run_cli(json.dumps({"task_id": "001"}))
    assert cp.returncode != 0
    out = json.loads(cp.stdout)
    assert "errors" in out
    paths = {e["path"] for e in out["errors"]}
    # Missing implementer, reviewer_envelope, retries_used, flags.
    assert "$.implementer" in paths
    assert "$.reviewer_envelope" in paths


def test_cli_invalid_json_fails_with_errors() -> None:
    cp = _run_cli("{not-json")
    assert cp.returncode != 0
    out = json.loads(cp.stdout)
    assert "errors" in out


def test_cli_unknown_state_exits_zero_with_action() -> None:
    """Unknown enum values are not a schema violation — they route to
    unknown_state with exit 0 so the orchestrator can read the directive
    and pause."""
    payload = _payload(implementer="claude", verdict="bogus")
    cp = _run_cli(json.dumps(payload))
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["action"] == "unknown_state"
    assert "bogus" in out["reason"]


# ---------------------------------------------------------------------------
# TASK-001 PHASE_D_STATE_MACHINE_COMPLETION — first-class reviewer identity
# and runtime mode (`reviewer`, `claude_only`).
# ---------------------------------------------------------------------------


def _payload_v2(*, implementer: str, reviewer: str | None, verdict: str,
                claude_only: bool | None = None,
                findings: list | None = None,
                d5: dict | None = None,
                retries: dict | None = None,
                flags: dict | None = None,
                unattended_revert_policy: str | None = None,
                task_id: str = "001") -> dict:
    payload = _payload(
        implementer=implementer, verdict=verdict, findings=findings,
        d5=d5, retries=retries, flags=flags,
        unattended_revert_policy=unattended_revert_policy,
        task_id=task_id,
    )
    if reviewer is not None:
        payload["reviewer"] = reviewer
    if claude_only is not None:
        payload["claude_only"] = claude_only
    return payload


# --- Backward compatibility (AC line 1) ----------------------------------


def test_legacy_payload_without_reviewer_field_still_routes() -> None:
    """AC line 1: payloads omitting the new `reviewer`/`claude_only` fields
    remain backward-compatible for one release window."""
    out = plan_ops.route(_payload(implementer="claude", verdict="clean"))
    assert out["action"] == "commit"
    out = plan_ops.route(_payload(implementer="codex", verdict="ship"))
    assert out["action"] == "commit"


# --- Claude-only Claude reviewer (AC line 2) ------------------------------


@pytest.mark.parametrize("verdict", ["ship", "ship-with-fixes"])
def test_claude_only_claude_reviewer_ship_commits(verdict: str) -> None:
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="claude", claude_only=True,
        verdict=verdict,
    ))
    assert out["action"] == "commit"
    assert out["args"]["commit_flags"]["disagreement_tag"] is False


def test_claude_only_claude_reviewer_needs_rework_routes_to_d4_fail() -> None:
    """AC line 2: `needs-rework` under claude_only=true routes to D.4 path
    without D.5/D.2a.5/D.2a.6 escalation."""
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="claude", claude_only=True,
        verdict="needs-rework", findings=[{"i": 0}],
    ))
    assert out["action"] == "fail"
    assert out["args"]["fail_stage"] == "review"
    assert out["args"]["policy_kind"] == "d4_review_failure"
    assert out["args"]["authorization_source"] == "phase-d4-review-failure"
    assert "claude_only" in out["args"]["fail_reason"]
    # D.5/D.2a.5/D.2a.6 are unreachable — directive carries no dispatch_context.
    assert "dispatch_context" not in out["args"]


def test_claude_reviewer_on_claude_work_without_claude_only_is_unknown() -> None:
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="claude", claude_only=False,
        verdict="ship",
    ))
    assert out["action"] == "unknown_state"
    assert "claude_only" in out["reason"]


# --- Gemini reviewer on Claude work (AC line 3) ---------------------------


@pytest.mark.parametrize("verdict", ["clean", "minor-findings"])
def test_gemini_reviewer_clean_or_minor_findings_commits(verdict: str) -> None:
    """AC line 3: Gemini review of Claude work uses Codex verdict vocabulary
    and Codex-equivalent routing under claude_only=false."""
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="gemini", claude_only=False,
        verdict=verdict,
    ))
    assert out["action"] == "commit"
    assert out["args"]["commit_flags"] == {
        "disagreement_tag": False,
        "remediation_tag": False,
        "narrow_remediation_tag": False,
        "dismissed_finding_ids": [],
    }


def test_gemini_reviewer_needs_rework_dispatches_d5_like_codex() -> None:
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="gemini", claude_only=False,
        verdict="needs-rework", findings=[{"i": 0}],
    ))
    assert out["action"] == "dispatch_d5"
    assert out["args"]["dispatch_context"]["template"] == "PhaseD5"


@pytest.mark.parametrize(
    ("policy", "action", "authorization_source"),
    [
        ("pause", "pause_awaiting_user", None),
        ("fail-fast", "fail", "unattended-fail-fast"),
        ("preserve-only", "fail", "unattended-preserve-only"),
    ],
)
def test_gemini_review_binding_matches_codex_policy_branch(
    policy: str, action: str, authorization_source: str | None,
) -> None:
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="gemini", claude_only=False,
        verdict="needs-rework", findings=[{"i": 0}],
        flags=_flags(codex_review_binding=True),
        unattended_revert_policy=policy,
    ))
    assert out["action"] == action
    assert out["action"] not in {
        "dispatch_d5",
        "dispatch_bounded_remediation",
        "dispatch_narrow_remediation",
    }
    assert out["args"]["policy_kind"] == "binding_policy"
    assert out["args"]["unattended_revert_policy"] == policy
    assert "dispatch_context" not in out["args"]
    if action == "pause_awaiting_user":
        assert out["args"]["pause_payload"]["stage"] == "post_binding_block"
    else:
        assert out["args"]["authorization_source"] == authorization_source


def test_gemini_reviewer_under_claude_only_is_unknown_state() -> None:
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="gemini", claude_only=True,
        verdict="clean",
    ))
    assert out["action"] == "unknown_state"
    assert "claude_only" in out["reason"]


# --- D.2b unchanged (AC line 4) -------------------------------------------


def test_codex_impl_claude_reviewer_role_swap_unchanged() -> None:
    """AC line 4: Codex-implemented work reviewed by Claude keeps existing
    D.2b role-swap behavior."""
    out = plan_ops.route(_payload_v2(
        implementer="codex", reviewer="claude", claude_only=False,
        verdict="needs-rework", findings=[{"i": 0}],
    ))
    assert out["action"] == "dispatch_role_swap"


# --- Skip-review path (AC line 5) -----------------------------------------


def test_reviewer_none_routes_to_commit_with_reviewer_metadata() -> None:
    """AC line 5: `reviewer:"none"` is the explicit skip-review path; the
    directive carries reviewer metadata for `commit-task --reviewer none`."""
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="none", verdict="clean",
    ))
    assert out["action"] == "commit"
    assert out["args"]["reviewer"] == "none"
    cf = out["args"]["commit_flags"]
    assert cf["disagreement_tag"] is False
    assert cf["remediation_tag"] is False
    assert cf["narrow_remediation_tag"] is False


def test_reviewer_none_with_codex_implementer_also_commits() -> None:
    out = plan_ops.route(_payload_v2(
        implementer="codex", reviewer="none", verdict="ship",
    ))
    assert out["action"] == "commit"
    assert out["args"]["reviewer"] == "none"


# --- Unknown reviewer / unknown combo (AC line 6) -------------------------


def test_unknown_reviewer_routes_to_unknown_state() -> None:
    """AC line 6: unknown reviewer/runtime combos return `unknown_state`,
    not a Python exception."""
    out = plan_ops.route(_payload_v2(
        implementer="claude", reviewer="bogus-reviewer", verdict="clean",
    ))
    assert out["action"] == "unknown_state"
    assert "bogus-reviewer" in out["reason"]


def test_codex_impl_with_codex_reviewer_routes_to_unknown_state() -> None:
    out = plan_ops.route(_payload_v2(
        implementer="codex", reviewer="codex", verdict="clean",
    ))
    assert out["action"] == "unknown_state"


def test_claude_only_non_bool_routes_to_unknown_state() -> None:
    payload = _payload(implementer="claude", verdict="clean")
    payload["claude_only"] = "yes"  # wrong type
    out = plan_ops.route(payload)
    assert out["action"] == "unknown_state"
    assert "claude_only" in out["reason"]


# --- CLI shim accepts the new fields --------------------------------------


def test_cli_accepts_new_reviewer_and_claude_only_fields() -> None:
    payload = _payload_v2(
        implementer="claude", reviewer="claude", claude_only=True,
        verdict="ship",
    )
    cp = _run_cli(json.dumps(payload))
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["action"] == "commit"


def test_cli_reviewer_none_skip_review_path() -> None:
    payload = _payload_v2(
        implementer="claude", reviewer="none", verdict="clean",
    )
    cp = _run_cli(json.dumps(payload))
    assert cp.returncode == 0, cp.stderr
    out = json.loads(cp.stdout)
    assert out["action"] == "commit"
    assert out["args"]["reviewer"] == "none"
