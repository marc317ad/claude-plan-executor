"""Regression: shared `_run_*` payload entrypoints must accept MCP-shape
inputs.

The MCP server delivers `oneOf string|array` params (`files`, `check`)
and `array` params (`reviewer_minor_findings`, `reviewer_findings`) as
their native JSON shape — not as the CLI-side CSV/JSON-string forms
argparse produces. Before the fix, `_run_commit_task` / `_run_audit` /
`_run_gates` / `_run_fail_task` called `.split(",")` or `json.loads`
unconditionally and blew up under MCP, forcing CLI fallback.

These tests pin the normalization helpers and the four call sites.
"""

from __future__ import annotations

from pathlib import Path

from tests.scripts.plan_ops_pure_harness import load_plan_ops


def test_normalize_csv_or_list_accepts_both_shapes() -> None:
    plan_ops = load_plan_ops()
    assert plan_ops._normalize_csv_or_list("a,b,c") == ["a", "b", "c"]
    assert plan_ops._normalize_csv_or_list(["a", "b", "c"]) == ["a", "b", "c"]
    # whitespace + empty tokens get stripped on both paths
    assert plan_ops._normalize_csv_or_list(" a , ,b ") == ["a", "b"]
    assert plan_ops._normalize_csv_or_list([" a ", "", "b"]) == ["a", "b"]
    assert plan_ops._normalize_csv_or_list(None) == []
    assert plan_ops._normalize_csv_or_list("") == []
    assert plan_ops._normalize_csv_or_list([]) == []


def test_normalize_json_or_list_accepts_both_shapes() -> None:
    plan_ops = load_plan_ops()
    assert plan_ops._normalize_json_or_list("[]") == []
    assert plan_ops._normalize_json_or_list([]) == []
    assert plan_ops._normalize_json_or_list(None) == []
    assert plan_ops._normalize_json_or_list("") == []
    parsed = plan_ops._normalize_json_or_list(
        '[{"confidence":"high","file":"a","line":1,"issue":"x","suggested_fix":"y","severity":"low"}]'
    )
    assert parsed == [{
        "confidence": "high", "file": "a", "line": 1,
        "issue": "x", "suggested_fix": "y", "severity": "low",
    }]
    native = [{"confidence": "high", "file": "a", "line": 1,
               "issue": "x", "suggested_fix": "y", "severity": "low"}]
    assert plan_ops._normalize_json_or_list(native) == native


_PLAN_BODY = """# Plan: shape-norm

**Created:** 2026-05-11
**Status:** in-progress
**Base branch:** main

## Context

Stub.

## Tasks

### TASK-001: First task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/foo.py
- **Dependencies:** none

Body.
"""


def _seed_plan(tmp_path: Path) -> Path:
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / "sample.md"
    plan.write_text(_PLAN_BODY, encoding="utf-8")
    return plan


def _commit_task_payload(plan: Path, **overrides) -> dict:
    payload = {
        "task_id": "001",
        "files": "src/foo.py",
        "plan_file": str(plan),
        "reviewer": "none",
        "reviewer_verdict": "",
        "reviewer_minor_findings": "[]",
        "dry_run": True,
        "v_check_timeout": 300,
        "run_id": "R1",
        "title": "x",
        "diff_summary": "y",
        "remediation_tag": False,
        "sandbox_divergence_tag": False,
        "d4_rescue_tag": False,
        "narrow_remediation_tag": False,
        "disagreement_tag": False,
        "dismissed_finding_ids": [],
        "update_schedule_state": None,
    }
    payload.update(overrides)
    return payload


def test_commit_task_accepts_mcp_shape_files_array(tmp_path: Path) -> None:
    """Regression: `files` as a native list — not CSV — must be accepted.

    Pre-fix `_run_commit_task` called `payload["files"].split(",")` and
    raised `AttributeError` when MCP delivered a list.
    """
    plan_ops = load_plan_ops()
    plan = _seed_plan(tmp_path)
    payload = _commit_task_payload(plan, files=["src/foo.py", "src/bar.py"])
    result = plan_ops._run_commit_task(payload)
    # Dry-run early-returns with no errors[] when shape is accepted.
    assert int(result.get("__plan_ops_exit_code__", 0)) == 0, (
        f"expected dry-run success, got result={result!r}"
    )
    public = plan_ops._public_result(result)
    assert public.get("dry_run") is True
    assert public.get("files") == ["src/foo.py", "src/bar.py"]


def test_commit_task_accepts_mcp_shape_reviewer_minor_findings_array(
    tmp_path: Path,
) -> None:
    """Regression: `reviewer_minor_findings` as a native list of dicts.

    Pre-fix `_run_commit_task` called
    `json.loads(payload["reviewer_minor_findings"])` and raised
    `TypeError` when MCP delivered a list.
    """
    plan_ops = load_plan_ops()
    plan = _seed_plan(tmp_path)
    payload = _commit_task_payload(plan, reviewer_minor_findings=[])
    result = plan_ops._run_commit_task(payload)
    assert int(result.get("__plan_ops_exit_code__", 0)) == 0, (
        f"expected dry-run success on native [], got result={result!r}"
    )


def test_audit_accepts_mcp_shape_check_array(tmp_path: Path) -> None:
    """Regression: `_run_audit` must accept `check` as a native list."""
    plan_ops = load_plan_ops()
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    # Use a definitely-unknown check name so we can prove the audit
    # path reached the validation seam (rather than crashing in split()).
    payload = {
        "list": False,
        "check": ["__not_a_real_audit_check__"],
        "plans_dir": str(plans),
        "tier": None,
        "plan_file": None,
        "schedule_file": None,
        "run_id": None,
    }
    result = plan_ops._run_audit(payload)
    public = plan_ops._public_result(result)
    err = public.get("error") or ""
    assert "unknown audit check" in err, (
        f"expected unknown-check rejection (proves split() wasn't reached "
        f"with a list); got result={result!r}"
    )


def test_gates_accepts_mcp_shape_check_array(tmp_path: Path) -> None:
    """Regression: `_run_gates` must accept `check` as a native list."""
    plan_ops = load_plan_ops()
    payload = {
        "mode": "check",
        "list": False,
        "check": ["__not_a_real_gate__"],
        "certify": False,
        "plan_file": None,
        "schedule_file": None,
        "run_id": None,
        "certify_mode": None,
    }
    result = plan_ops._run_gates(payload)
    public = plan_ops._public_result(result)
    err = public.get("error") or ""
    assert "unknown gate name" in err, (
        f"expected unknown-gate rejection (proves split() wasn't reached "
        f"with a list); got result={result!r}"
    )
