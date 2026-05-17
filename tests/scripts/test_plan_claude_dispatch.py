"""Tests for ``plan_claude_dispatch.py`` default-timeout resolution.

BUG-145: the wrapper's ``run`` subcommand must resolve a 1800s default
timeout when neither ``input.overrides.timeout_sec`` nor ``--timeout``
is supplied (previously fell through to the unwritten 900s backend
default and timed out heavy multi-file tasks at exactly 15 min).

Lives alongside ``test_plan_claude_dispatch_cli.py``; the focused
filename matches the BUG-145 acceptance-criteria pointer
(``-k "timeout"`` filters down to these tests).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import plan_claude_dispatch as cli  # noqa: E402


def _minimal_input() -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "agent": "plan-implementer",
        "payload": {"task_id": "001"},
        "overrides": {
            "model": None,
            "timeout_sec": None,
            "tools_allowed_extra": None,
            "tools_disallowed_extra": None,
            "cwd": None,
        },
    }


def _minimal_manifest() -> Dict[str, Any]:
    return {
        "name": "plan-implementer",
        "model": "claude-opus-4-7",
        "tools": ["Read", "Edit", "Bash"],
    }


def test_default_dispatch_timeout_constant_is_1800():
    """BUG-145: documented default is 1800s (30 min) at the wrapper layer."""
    assert cli.DEFAULT_DISPATCH_TIMEOUT_SEC == 1800


def test_build_effective_default_timeout_when_no_override_supplied():
    """No ``overrides.timeout_sec`` and no ``--timeout`` → 1800s default."""
    effective = cli._build_effective(
        _minimal_manifest(),
        _minimal_input(),
        cli_timeout=None,
        cli_repo_root=None,
    )
    assert effective.get("timeout_sec") == cli.DEFAULT_DISPATCH_TIMEOUT_SEC
    assert effective["timeout_sec"] == 1800
    # Specifically: must NOT fall through to the legacy 900s wall.
    assert effective["timeout_sec"] != 900


def test_build_effective_input_override_wins_over_default_timeout():
    """``input.overrides.timeout_sec`` still takes precedence over the default."""
    input_obj = _minimal_input()
    input_obj["overrides"]["timeout_sec"] = 600
    effective = cli._build_effective(
        _minimal_manifest(),
        input_obj,
        cli_timeout=None,
        cli_repo_root=None,
    )
    assert effective.get("timeout_sec") == 600


def test_build_effective_cli_timeout_wins_when_input_override_absent():
    """``--timeout N`` overrides the default when input override is absent."""
    effective = cli._build_effective(
        _minimal_manifest(),
        _minimal_input(),
        cli_timeout=120,
        cli_repo_root=None,
    )
    assert effective.get("timeout_sec") == 120


def test_build_effective_input_override_wins_over_cli_timeout():
    """Input-side override takes precedence over CLI ``--timeout`` (existing wiring)."""
    input_obj = _minimal_input()
    input_obj["overrides"]["timeout_sec"] = 750
    effective = cli._build_effective(
        _minimal_manifest(),
        input_obj,
        cli_timeout=120,
        cli_repo_root=None,
    )
    assert effective.get("timeout_sec") == 750


# ---------------------------------------------------------------------------
# TASK-001 (PLAN_RUN_TELEMETRY_FOLLOWUPS_2026-05-15): narrow the wrapper's
# published observed-delta lists to the intersection of the whole-tree diff
# and (declared ∪ agent-reported writes). Cross-batch leakage from sibling
# tasks in parallel batches must not pollute the envelope telemetry.
# ---------------------------------------------------------------------------


def _make_envelope_with_scope(
    declared: list, result_files_changed: list | None = None,
) -> Dict[str, Any]:
    scope: Dict[str, Any] = {"declared_files_changed": list(declared)}
    env: Dict[str, Any] = {"scope": scope}
    if result_files_changed is not None:
        env["result"] = {"files_changed": list(result_files_changed)}
    return env


def test_observed_delta_excludes_cross_batch_leakage():
    """Sibling-task writes (cross-batch leakage) are dropped from the
    published ``observed_delta_*`` while the underlying ``cleanup_result``
    is untouched."""
    declared = ["src/task_a.py"]
    cleanup_result = {
        # cleanup observed restoration of both the task-declared file
        # and a sibling-task file (the cross-batch leak from a parallel
        # batch peer).
        "restored": ["src/task_a.py", "src/sibling_task.py"],
        "deleted": [],
        "failed_paths": [],
        "out_of_scope_paths": [],
        "scope_violation_detected": False,
        "scope_misreport_detected": False,
    }
    envelope = _make_envelope_with_scope(declared, result_files_changed=[])

    merged = cli._merge_scope_with_cleanup(
        envelope, cleanup_result, agent_reported_files_changed=[]
    )
    scope = merged["scope"]

    # Published envelope drops the sibling file.
    assert scope["observed_delta_tracked"] == ["src/task_a.py"]
    assert scope["observed_delta_untracked"] == []
    # Underlying cleanup_result is unchanged (verify same test).
    assert cleanup_result["restored"] == ["src/task_a.py", "src/sibling_task.py"]


def test_observed_delta_includes_agent_self_reported_writes():
    """Agent self-reports of files outside ``declared_files_changed``
    (scope misreports) are retained in the published ``observed_delta_*``
    so misreports still surface in telemetry."""
    declared = ["src/task_a.py"]
    # Agent self-reported an undeclared path (scope misreport).
    agent_reported = ["src/undeclared_write.py"]
    cleanup_result = {
        "restored": ["src/task_a.py"],
        "deleted": ["src/undeclared_write.py"],
        "failed_paths": [],
        "out_of_scope_paths": ["src/undeclared_write.py"],
        "scope_violation_detected": False,
        "scope_misreport_detected": True,
    }
    envelope = _make_envelope_with_scope(
        declared, result_files_changed=agent_reported
    )

    merged = cli._merge_scope_with_cleanup(
        envelope, cleanup_result,
        agent_reported_files_changed=agent_reported,
    )
    scope = merged["scope"]

    # Both the declared restore and the agent-self-reported delete
    # remain visible in the published envelope.
    assert "src/task_a.py" in scope["observed_delta_tracked"]
    assert "src/undeclared_write.py" in scope["observed_delta_untracked"]
    # Misreport flag is preserved (computed off unfiltered cleanup_result).
    assert scope["scope_misreport_detected"] is True
