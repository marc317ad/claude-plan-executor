"""Tests for ``_claude_guardrails`` (TASK-002).

Coverage:
  * :func:`evaluate_preflight` — table-driven walk of the §9.2 refusal
    matrix; every row maps to the correct ``build_*`` envelope.
  * Allow path: every row passes → ``(True, None)``.
  * :func:`scrub_env` — keeps ``env_allowlist`` + ``PLAN_EXEC_*``,
    drops everything else, always overwrites the per-hop bookkeeping
    vars, increments depth by default.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _claude_guardrails as guard  # noqa: E402
from _claude_guardrails import (  # noqa: E402
    DEFAULT_MAX_DEPTH,
    KILLSWITCH_ENV,
    PER_HOP_DEPTH,
    PER_HOP_PARENT_AGENT,
    PER_HOP_PARENT_RUN_ID,
    evaluate_preflight,
    scrub_env,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _good_input(
    *,
    agent: str = "plan-implementer",
    depth: int = 0,
    max_depth: Optional[int] = 4,
    cost_cap_usd: Optional[float] = 1.0,
    extras: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "agent": agent,
        "payload": {"task_id": "001"},
        "output_instructions": {
            "format": "json",
            "schema_path": None,
            "schema_inline": None,
            "max_bytes": 1024,
        },
        "overrides": {
            "model": None,
            "timeout_sec": None,
            "tools_allowed_extra": extras,
            "tools_disallowed_extra": None,
            "cwd": None,
        },
        "guardrails": {
            "max_depth": max_depth,
            "cost_cap_usd": cost_cap_usd,
            "network": None,
        },
        "trace": {
            "run_id": "run-1",
            "parent_span_id": None,
            "depth": depth,
            "call_chain": ["orchestrator"],
        },
    }


def _good_manifest(
    *,
    name: str = "plan-implementer",
    tools: Optional[List[str]] = None,
    env_allowlist: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "name": name,
        "description": "test",
        "model": "opus",
        "tools": list(tools) if tools is not None else ["Read", "Edit", "Bash"],
        "env_allowlist": list(env_allowlist) if env_allowlist is not None else [],
    }


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


def test_evaluate_preflight_allows_when_every_row_passes() -> None:
    allow, denied = evaluate_preflight(
        _good_manifest(),
        _good_input(),
        env={},
    )
    assert allow is True
    assert denied is None


def test_evaluate_preflight_allows_with_extras_present_in_manifest() -> None:
    allow, denied = evaluate_preflight(
        _good_manifest(tools=["Read", "Bash"]),
        _good_input(extras=["Bash"]),
        env={},
    )
    assert allow is True
    assert denied is None


# ---------------------------------------------------------------------------
# Refusal matrix — table-driven
# ---------------------------------------------------------------------------


def _depth_row() -> Tuple[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, str]]:
    return (
        "depth_exceeded",
        _good_manifest(),
        _good_input(depth=4, max_depth=4),
        {},
    )


def _budget_row() -> (
    Tuple[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, str]]
):
    return (
        "budget_exhausted",
        _good_manifest(),
        _good_input(cost_cap_usd=0.5),
        {"PLAN_EXEC_COST_SO_FAR_USD": "0.75"},
    )


def _agent_not_allowed_row() -> (
    Tuple[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, str]]
):
    return (
        "input_invalid",
        _good_manifest(),
        _good_input(agent="plan-author"),
        {},
    )


def _manifest_invalid_row() -> (
    Tuple[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, str]]
):
    bad = _good_manifest()
    bad["tools"] = "not-a-list"
    return ("manifest_invalid", bad, _good_input(), {})


def _tool_not_in_manifest_row() -> (
    Tuple[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, str]]
):
    return (
        "denied",
        _good_manifest(tools=["Read"]),
        _good_input(extras=["Bash"]),
        {},
    )


def _killswitch_row() -> (
    Tuple[str, Mapping[str, Any], Mapping[str, Any], Mapping[str, str]]
):
    return (
        "denied",
        _good_manifest(),
        _good_input(),
        {KILLSWITCH_ENV: "1"},
    )


@pytest.mark.parametrize(
    "expected_status,manifest,input_obj,env",
    [
        pytest.param(*_depth_row(), id="depth_exceeded"),
        pytest.param(*_budget_row(), id="budget_exhausted"),
        pytest.param(
            *_agent_not_allowed_row(),
            id="input_invalid_agent_not_allowed",
        ),
        pytest.param(
            *_manifest_invalid_row(),
            id="manifest_invalid_tools_shape",
        ),
        pytest.param(
            *_tool_not_in_manifest_row(),
            id="denied_tool_not_in_manifest",
        ),
        pytest.param(*_killswitch_row(), id="denied_killswitch"),
    ],
)
def test_evaluate_preflight_refusal_matrix(
    expected_status: str,
    manifest: Mapping[str, Any],
    input_obj: Mapping[str, Any],
    env: Mapping[str, str],
) -> None:
    allow, envelope = evaluate_preflight(manifest, input_obj, env)
    assert allow is False
    assert envelope is not None
    assert envelope["status"] == expected_status
    # Every refusal envelope must carry a non-null ``error`` block per
    # the §7 contract.
    assert envelope["error"] is not None
    assert isinstance(envelope["error"]["code"], str)
    assert isinstance(envelope["error"]["message"], str)
    # The trace block from the input must be echoed (non-null).
    assert envelope["trace"]["run_id"] == "run-1"


# ---------------------------------------------------------------------------
# Refusal matrix — fine-grained surface checks
# ---------------------------------------------------------------------------


def test_depth_exceeded_uses_max_depth_from_input_first() -> None:
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        _good_input(depth=2, max_depth=2),
        env={"PLAN_EXEC_MAX_DEPTH": "99"},  # input wins
    )
    assert not allow
    assert envelope["status"] == "depth_exceeded"
    assert "2" in envelope["error"]["message"]


def test_depth_exceeded_falls_back_to_env_var() -> None:
    inp = _good_input(depth=3)
    inp["guardrails"]["max_depth"] = None
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        inp,
        env={"PLAN_EXEC_MAX_DEPTH": "3"},
    )
    assert not allow
    assert envelope["status"] == "depth_exceeded"


def test_depth_exceeded_falls_back_to_default_when_unset() -> None:
    inp = _good_input(depth=DEFAULT_MAX_DEPTH)
    inp["guardrails"]["max_depth"] = None
    allow, envelope = evaluate_preflight(_good_manifest(), inp, env={})
    assert not allow
    assert envelope["status"] == "depth_exceeded"


def test_budget_exhausted_uses_input_cap_first() -> None:
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        _good_input(cost_cap_usd=1.0),
        env={
            "PLAN_EXEC_COST_SO_FAR_USD": "1.5",
            "PLAN_EXEC_COST_CAP_USD": "999",  # input wins
        },
    )
    assert not allow
    assert envelope["status"] == "budget_exhausted"
    assert envelope["error"]["code"] == "budget_exhausted"


def test_budget_exhausted_falls_back_to_env_cap() -> None:
    inp = _good_input(cost_cap_usd=None)
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        inp,
        env={
            "PLAN_EXEC_COST_SO_FAR_USD": "1.5",
            "PLAN_EXEC_COST_CAP_USD": "1.0",
        },
    )
    assert not allow
    assert envelope["status"] == "budget_exhausted"


def test_budget_no_cap_does_not_deny_even_with_high_spend() -> None:
    inp = _good_input(cost_cap_usd=None)
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        inp,
        env={"PLAN_EXEC_COST_SO_FAR_USD": "9999"},
    )
    assert allow is True
    assert envelope is None


def test_agent_not_allowed_envelope_uses_agent_not_allowed_code() -> None:
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        _good_input(agent="plan-author"),
        env={},
    )
    assert not allow
    assert envelope["status"] == "input_invalid"
    assert envelope["error"]["code"] == "agent_not_allowed"
    assert "plan-author" in envelope["error"]["message"]


def test_missing_agent_field_is_input_invalid() -> None:
    inp = _good_input()
    inp.pop("agent")
    allow, envelope = evaluate_preflight(_good_manifest(), inp, env={})
    assert not allow
    assert envelope["status"] == "input_invalid"


def test_manifest_invalid_when_manifest_is_none() -> None:
    allow, envelope = evaluate_preflight(None, _good_input(), env={})
    assert not allow
    assert envelope["status"] == "manifest_invalid"


def test_tool_not_in_manifest_lists_offending_tools() -> None:
    allow, envelope = evaluate_preflight(
        _good_manifest(tools=["Read", "Edit"]),
        _good_input(extras=["Bash", "Glob"]),
        env={},
    )
    assert not allow
    assert envelope["status"] == "denied"
    assert envelope["error"]["code"] == "tool_not_in_manifest"
    assert "Bash" in envelope["error"]["message"]
    assert "Glob" in envelope["error"]["message"]


def test_killswitch_truthy_variants_all_deny() -> None:
    for value in ["1", "true", "yes", "TRUE", "On"]:
        allow, envelope = evaluate_preflight(
            _good_manifest(),
            _good_input(),
            env={KILLSWITCH_ENV: value},
        )
        assert not allow, f"value {value!r} should have denied"
        assert envelope["status"] == "denied"
        assert envelope["error"]["code"] == "killswitch"


def test_killswitch_falsey_variants_pass() -> None:
    for value in ["", "0", "false", "no"]:
        allow, _ = evaluate_preflight(
            _good_manifest(),
            _good_input(),
            env={KILLSWITCH_ENV: value},
        )
        assert allow, f"value {value!r} should NOT have denied"


# ---------------------------------------------------------------------------
# Refusal-order: first match wins
# ---------------------------------------------------------------------------


def test_depth_check_runs_before_budget_check() -> None:
    # Both depth and budget would fail; depth must be returned first
    # so the caller sees the cheaper / earlier reason.
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        _good_input(depth=10, max_depth=2, cost_cap_usd=0.0),
        env={"PLAN_EXEC_COST_SO_FAR_USD": "999"},
    )
    assert not allow
    assert envelope["status"] == "depth_exceeded"


def test_budget_check_runs_before_agent_allowlist() -> None:
    allow, envelope = evaluate_preflight(
        _good_manifest(),
        _good_input(agent="plan-author", cost_cap_usd=0.0),
        env={"PLAN_EXEC_COST_SO_FAR_USD": "1.0"},
    )
    assert not allow
    assert envelope["status"] == "budget_exhausted"


def test_agent_allowlist_check_runs_before_manifest_check() -> None:
    allow, envelope = evaluate_preflight(
        None,  # manifest invalid
        _good_input(agent="plan-author"),
        env={},
    )
    assert not allow
    assert envelope["status"] == "input_invalid"


def test_manifest_check_runs_before_tool_allowlist_check() -> None:
    bad_manifest = {"name": "x", "description": "x", "model": "x", "tools": "no"}
    allow, envelope = evaluate_preflight(
        bad_manifest,
        _good_input(extras=["Bash"]),
        env={},
    )
    assert not allow
    assert envelope["status"] == "manifest_invalid"


def test_tool_allowlist_check_runs_before_killswitch() -> None:
    allow, envelope = evaluate_preflight(
        _good_manifest(tools=["Read"]),
        _good_input(extras=["Bash"]),
        env={KILLSWITCH_ENV: "1"},
    )
    assert not allow
    assert envelope["status"] == "denied"
    assert envelope["error"]["code"] == "tool_not_in_manifest"


# ---------------------------------------------------------------------------
# scrub_env
# ---------------------------------------------------------------------------


def test_scrub_env_keeps_allowlisted_and_plan_exec_drops_rest() -> None:
    parent = {
        "HOME": "/home/u",
        "PATH": "/usr/bin",
        "SECRET_TOKEN": "leakme",
        "AWS_KEY": "leakme2",
        "PLAN_EXEC_RUN_ID": "run-1",
        "PLAN_EXEC_COST_SO_FAR_USD": "0.10",
    }
    out = scrub_env(
        _good_manifest(env_allowlist=["HOME", "PATH"]),
        parent,
    )
    assert out["HOME"] == "/home/u"
    assert out["PATH"] == "/usr/bin"
    assert out["PLAN_EXEC_RUN_ID"] == "run-1"
    assert out["PLAN_EXEC_COST_SO_FAR_USD"] == "0.10"
    assert "SECRET_TOKEN" not in out
    assert "AWS_KEY" not in out


def test_scrub_env_default_env_allowlist_is_empty() -> None:
    # ``env_allowlist`` missing from the manifest entirely.
    manifest = _good_manifest()
    manifest.pop("env_allowlist")
    parent = {
        "HOME": "/home/u",
        "SECRET": "x",
        "PLAN_EXEC_RUN_ID": "run-1",
    }
    out = scrub_env(manifest, parent)
    assert "HOME" not in out
    assert "SECRET" not in out
    assert out["PLAN_EXEC_RUN_ID"] == "run-1"


def test_scrub_env_injects_per_hop_bookkeeping_vars() -> None:
    parent = {
        "PLAN_EXEC_RUN_ID": "run-x",
        "PLAN_EXEC_DISPATCH_DEPTH": "1",
        "PLAN_EXEC_PARENT_AGENT": "should-be-overwritten",
        "PLAN_EXEC_PARENT_RUN_ID": "should-be-overwritten",
    }
    out = scrub_env(
        _good_manifest(),
        parent,
        parent_agent="plan-implementer",
        parent_run_id="run-y",
    )
    assert out[PER_HOP_DEPTH] == "2"  # incremented from parent's 1
    assert out[PER_HOP_PARENT_AGENT] == "plan-implementer"
    assert out[PER_HOP_PARENT_RUN_ID] == "run-y"


def test_scrub_env_per_hop_depth_defaults_to_one_when_parent_unset() -> None:
    out = scrub_env(_good_manifest(), {})
    assert out[PER_HOP_DEPTH] == "1"
    assert out[PER_HOP_PARENT_AGENT] == ""
    assert out[PER_HOP_PARENT_RUN_ID] == ""


def test_scrub_env_explicit_next_depth_overrides_increment() -> None:
    parent = {"PLAN_EXEC_DISPATCH_DEPTH": "5"}
    out = scrub_env(_good_manifest(), parent, next_depth=0)
    assert out[PER_HOP_DEPTH] == "0"


def test_scrub_env_does_not_mutate_parent() -> None:
    parent = {"HOME": "/h", "PLAN_EXEC_X": "y"}
    snapshot = dict(parent)
    scrub_env(_good_manifest(env_allowlist=["HOME"]), parent)
    assert parent == snapshot


def test_scrub_env_rejects_non_mapping_manifest() -> None:
    with pytest.raises(TypeError):
        scrub_env("not-a-mapping", {})  # type: ignore[arg-type]


def test_scrub_env_rejects_bad_env_allowlist_type() -> None:
    bad = _good_manifest()
    bad["env_allowlist"] = "HOME,PATH"  # must be list at this point
    with pytest.raises(TypeError):
        scrub_env(bad, {})


# ---------------------------------------------------------------------------
# Module surface sanity
# ---------------------------------------------------------------------------


def test_module_exports_expected_public_surface() -> None:
    expected = {
        "DEFAULT_MAX_DEPTH",
        "KILLSWITCH_ENV",
        "PER_HOP_DEPTH",
        "PER_HOP_PARENT_AGENT",
        "PER_HOP_PARENT_RUN_ID",
        "evaluate_preflight",
        "scrub_env",
    }
    assert expected.issubset(set(guard.__all__))
