"""Preflight guardrail engine for ``plan_claude_dispatch.py``
(PLAN_NESTED_DISPATCH §9.2).

Two surfaces:

  1. :func:`evaluate_preflight(manifest, input, env)` — walks the §9.2
     refusal matrix in a fixed order and returns
     ``(allow: bool, envelope_if_denied: dict | None)``.  Each refusal
     reason maps to one of the ``build_*`` constructors in
     ``_claude_dispatch_envelope`` so the wrapper, the CLI, and the
     backend all agree on the wire shape.

  2. :func:`scrub_env(manifest, parent_env)` — filters the parent
     environment down to ``manifest["env_allowlist"] + PLAN_EXEC_*`` and
     injects the per-hop bookkeeping vars
     (``PLAN_EXEC_DISPATCH_DEPTH``, ``PLAN_EXEC_PARENT_AGENT``,
     ``PLAN_EXEC_PARENT_RUN_ID``) so child sessions cannot escape the
     sandbox via inherited state.

Refusal matrix walked by :func:`evaluate_preflight` (in order — first
match wins; subsequent rows are not evaluated):

  Row 1  depth >= max_depth                 → build_depth_exceeded
  Row 2  cost_so_far >= cost_cap_usd        → build_budget_exhausted
  Row 3  agent name not in allowlist        → build_input_invalid
                                              (code: agent_not_allowed)
  Row 4  manifest dict shape invalid        → build_manifest_invalid
  Row 5  requested tool not in manifest     → build_denied
                                              (code: tool_not_in_manifest)
  Row 6  killswitch env var set             → build_denied
                                              (code: killswitch)
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Tuple

import _claude_dispatch_envelope as env_mod
from _claude_agent_manifest import DISPATCHABLE_AGENTS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default maximum nesting depth.  Overridable via
#: ``PLAN_EXEC_MAX_DEPTH`` in the parent env or via input.guardrails.
DEFAULT_MAX_DEPTH = 2

#: Killswitch env var.  When set to a truthy value (``1``, ``true``,
#: ``yes``, case-insensitive), every preflight is denied.
KILLSWITCH_ENV = "PLAN_EXEC_CLAUDE_DISPATCH_KILLSWITCH"

#: Per-hop bookkeeping variables that :func:`scrub_env` always injects.
PER_HOP_DEPTH = "PLAN_EXEC_DISPATCH_DEPTH"
PER_HOP_PARENT_AGENT = "PLAN_EXEC_PARENT_AGENT"
PER_HOP_PARENT_RUN_ID = "PLAN_EXEC_PARENT_RUN_ID"

#: Prefix all PLAN_EXEC_* vars share — these are always inherited by
#: the child session regardless of the manifest's ``env_allowlist``.
_PLAN_EXEC_PREFIX = "PLAN_EXEC_"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_truthy(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUTHY


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort int coercion; ``default`` on any failure."""
    if value is None:
        return default
    if isinstance(value, bool):
        # bools are ints in Python but we never want True/False here.
        return default
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: Optional[float]) -> Optional[float]:
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _trace_block(input_obj: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract the input's ``trace`` block for echoing into denial envelopes."""
    raw = input_obj.get("trace") if isinstance(input_obj, Mapping) else None
    if isinstance(raw, Mapping):
        return dict(raw)
    return None


def _input_agent(input_obj: Mapping[str, Any]) -> Optional[str]:
    if not isinstance(input_obj, Mapping):
        return None
    a = input_obj.get("agent")
    return a if isinstance(a, str) else None


def _requested_tools(input_obj: Mapping[str, Any]) -> List[str]:
    """Pull ``input.overrides.tools_allowed_extra`` if present.

    These are the tools the caller wants ADDED on top of the manifest's
    base tool set.  Each requested extra tool must be present in the
    manifest's own ``tools`` list — otherwise the call is denied.
    """
    if not isinstance(input_obj, Mapping):
        return []
    overrides = input_obj.get("overrides")
    if not isinstance(overrides, Mapping):
        return []
    extras = overrides.get("tools_allowed_extra")
    if extras is None:
        return []
    if not isinstance(extras, list):
        return []
    return [t for t in extras if isinstance(t, str)]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_preflight(
    manifest: Optional[Mapping[str, Any]],
    input_obj: Mapping[str, Any],
    env: Mapping[str, str],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Walk the §9.2 refusal matrix; return ``(allow, envelope_if_denied)``.

    Parameters
    ----------
    manifest
        The dict returned by ``_claude_agent_manifest.load_agent``.  May
        be ``None`` only when the caller is reporting a manifest-load
        failure that should surface as ``status: manifest_invalid``.
    input_obj
        The wrapper-level input dict (the §6 schema).
    env
        The parent process environment (typically ``os.environ``).

    Returns
    -------
    (bool, dict | None)
        ``(True, None)`` when every row passes; ``(False, envelope)``
        on the first refusal.  The envelope is one of the §7
        ``build_*`` shapes and should be emitted verbatim by the
        caller; no further work should occur after a refusal.
    """
    agent_name = _input_agent(input_obj)
    trace = _trace_block(input_obj)

    guardrails_in = (
        input_obj.get("guardrails")
        if isinstance(input_obj, Mapping)
        else None
    )
    if not isinstance(guardrails_in, Mapping):
        guardrails_in = {}

    # ----- Row 1: depth -----
    depth = 0
    if trace is not None:
        depth = _coerce_int(trace.get("depth"), 0)

    max_depth_input = guardrails_in.get("max_depth")
    if max_depth_input is None:
        max_depth = _coerce_int(env.get("PLAN_EXEC_MAX_DEPTH"), DEFAULT_MAX_DEPTH)
    else:
        max_depth = _coerce_int(max_depth_input, DEFAULT_MAX_DEPTH)

    if depth >= max_depth:
        return False, env_mod.build_depth_exceeded(
            depth=depth,
            max_depth=max_depth,
            agent=agent_name,
            trace=trace,
        )

    # ----- Row 2: budget -----
    cost_cap = _coerce_float(guardrails_in.get("cost_cap_usd"), None)
    if cost_cap is None:
        cost_cap = _coerce_float(env.get("PLAN_EXEC_COST_CAP_USD"), None)
    cost_so_far = _coerce_float(env.get("PLAN_EXEC_COST_SO_FAR_USD"), 0.0)
    if cost_so_far is None:
        cost_so_far = 0.0
    if cost_cap is not None and cost_so_far >= cost_cap:
        return False, env_mod.build_budget_exhausted(
            cost_usd=cost_so_far,
            cost_cap_usd=cost_cap,
            agent=agent_name,
            trace=trace,
        )

    # ----- Row 3: agent allowlist -----
    if agent_name is None or agent_name not in DISPATCHABLE_AGENTS:
        return False, env_mod.build_input_invalid(
            message=(
                f"agent {agent_name!r} is not in the v1 dispatch allowlist "
                f"{sorted(DISPATCHABLE_AGENTS)}"
            ),
            code="agent_not_allowed",
            agent=agent_name,
            trace=trace,
        )

    # ----- Row 4: manifest schema -----
    if not isinstance(manifest, Mapping):
        return False, env_mod.build_manifest_invalid(
            message="manifest is missing or not a mapping",
            agent=agent_name,
            trace=trace,
        )
    manifest_tools = manifest.get("tools")
    if not isinstance(manifest_tools, list) or any(
        not isinstance(t, str) for t in manifest_tools
    ):
        return False, env_mod.build_manifest_invalid(
            message="manifest 'tools' must be a list[str]",
            agent=agent_name,
            trace=trace,
        )

    # ----- Row 5: tool-allowlist -----
    requested = _requested_tools(input_obj)
    extras_outside_manifest = [t for t in requested if t not in manifest_tools]
    if extras_outside_manifest:
        return False, env_mod.build_denied(
            code="tool_not_in_manifest",
            message=(
                f"requested tools {extras_outside_manifest!r} are not in "
                f"manifest tool allowlist {manifest_tools!r}"
            ),
            agent=agent_name,
            trace=trace,
        )

    # ----- Row 6: killswitch -----
    if _is_truthy(env.get(KILLSWITCH_ENV)):
        return False, env_mod.build_denied(
            code="killswitch",
            message=(
                f"{KILLSWITCH_ENV} is set; nested Claude dispatch is disabled"
            ),
            agent=agent_name,
            trace=trace,
        )

    return True, None


def scrub_env(
    manifest: Mapping[str, Any],
    parent_env: Mapping[str, str],
    *,
    parent_agent: Optional[str] = None,
    parent_run_id: Optional[str] = None,
    next_depth: Optional[int] = None,
) -> Dict[str, str]:
    """Return a sanitised env for the child ``claude -p`` invocation.

    The child env contains:

      - every ``parent_env`` key matching ``manifest["env_allowlist"]``
        (deny-by-default — keys not on the list are dropped);
      - every ``parent_env`` key prefixed ``PLAN_EXEC_`` (always kept);
      - the per-hop bookkeeping vars
        (:data:`PER_HOP_DEPTH`, :data:`PER_HOP_PARENT_AGENT`,
        :data:`PER_HOP_PARENT_RUN_ID`) which always overwrite any
        value the parent may have leaked.

    Parameters
    ----------
    manifest
        Loaded agent manifest.  ``env_allowlist`` defaults to ``[]``
        (deny-by-default) when missing.
    parent_env
        The parent process environment to scrub.
    parent_agent
        Agent name the orchestrator uses to identify the parent hop.
        Defaults to ``parent_env.get(PER_HOP_PARENT_AGENT, "")``.
    parent_run_id
        Run id the orchestrator wants the child to inherit.  Defaults
        to ``parent_env.get(PER_HOP_PARENT_RUN_ID, "")``.
    next_depth
        Depth value to set on the child.  When ``None`` (the common
        case) the parent's ``PLAN_EXEC_DISPATCH_DEPTH`` is incremented
        by one (default 0 → 1 if the parent did not set it).
    """
    if not isinstance(manifest, Mapping):
        raise TypeError(
            f"manifest must be a mapping, got {type(manifest).__name__}"
        )

    raw_allow = manifest.get("env_allowlist") or []
    if not isinstance(raw_allow, list):
        raise TypeError(
            f"manifest 'env_allowlist' must be a list, "
            f"got {type(raw_allow).__name__}"
        )
    allow_set = {str(k) for k in raw_allow if isinstance(k, str) and k}

    out: Dict[str, str] = {}
    for key, value in parent_env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            # Defensive — env mappings are always str/str in practice.
            continue
        if key in allow_set or key.startswith(_PLAN_EXEC_PREFIX):
            out[key] = value

    # Per-hop injection always wins.
    if next_depth is None:
        current_depth = _coerce_int(parent_env.get(PER_HOP_DEPTH), 0)
        next_depth = current_depth + 1
    out[PER_HOP_DEPTH] = str(int(next_depth))

    if parent_agent is None:
        parent_agent = parent_env.get(PER_HOP_PARENT_AGENT, "")
    out[PER_HOP_PARENT_AGENT] = str(parent_agent)

    if parent_run_id is None:
        parent_run_id = parent_env.get(PER_HOP_PARENT_RUN_ID, "")
    out[PER_HOP_PARENT_RUN_ID] = str(parent_run_id)

    return out


__all__ = [
    "DEFAULT_MAX_DEPTH",
    "KILLSWITCH_ENV",
    "PER_HOP_DEPTH",
    "PER_HOP_PARENT_AGENT",
    "PER_HOP_PARENT_RUN_ID",
    "evaluate_preflight",
    "scrub_env",
]
