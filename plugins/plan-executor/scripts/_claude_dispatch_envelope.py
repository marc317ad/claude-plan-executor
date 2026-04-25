"""Envelope builders for ``plan_claude_dispatch.py`` (PLAN_NESTED_DISPATCH §7).

Every envelope emitted by the wrapper flows through one of the ``build_*``
constructors here. Centralising construction guarantees that downstream
tasks (TASK-003 backend, TASK-005 CLI) cannot accidentally drift from
the §7 wire shape — schema validation runs against the same JSON schema
file that ``schemas/claude_dispatch_output.json`` describes, and the
underlying ``_build_envelope`` rejects unknown top-level keys before
the dict is ever returned.

Status vocabulary (PLAN_NESTED_DISPATCH §7):
    ok | schema_invalid | timeout | denied | backend_error |
    budget_exhausted | depth_exceeded | manifest_invalid |
    input_invalid | scope_violation | cleanup_failure
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

STATUS_VOCABULARY = frozenset(
    {
        "ok",
        "schema_invalid",
        "timeout",
        "denied",
        "backend_error",
        "budget_exhausted",
        "depth_exceeded",
        "manifest_invalid",
        "input_invalid",
        "scope_violation",
        "cleanup_failure",
    }
)

ALLOWED_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "status_reason",
        "agent",
        "model",
        "session_id",
        "duration_ms",
        "cost_usd",
        "tokens",
        "result",
        "result_raw_truncated",
        "stderr_tail",
        "permission_denials",
        "scope",
        "trace",
        "error",
    }
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_scope() -> Dict[str, Any]:
    """Default ``scope`` block for envelopes that never reached the spawn step."""
    return {
        "declared_files_changed": [],
        "observed_delta_tracked": [],
        "observed_delta_untracked": [],
        "scope_violation_detected": False,
        "scope_misreport_detected": False,
        "failed_paths": [],
    }


def _empty_trace() -> Dict[str, Any]:
    """Default ``trace`` block for envelopes that lack caller-supplied trace data."""
    return {
        "run_id": "unknown",
        "span_id": "unknown",
        "parent_span_id": None,
        "depth": 0,
        "call_chain": [],
        "started_at": None,
        "ended_at": None,
    }


def _normalize_scope(scope: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if scope is None:
        return _empty_scope()
    out = _empty_scope()
    out.update(scope)
    return out


def _normalize_trace(trace: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if trace is None:
        return _empty_trace()
    out = _empty_trace()
    out.update(trace)
    return out


def _build_envelope(
    *,
    status: str,
    status_reason: Optional[str] = None,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    result: Any = None,
    result_raw_truncated: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    permission_denials: Optional[Iterable[Mapping[str, Any]]] = None,
    scope: Optional[Mapping[str, Any]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    error: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Single chokepoint for envelope construction.

    Validates the status against the §7 vocabulary and refuses any
    caller-supplied ``extra`` keys that are not in
    ``ALLOWED_TOP_LEVEL_KEYS``. Returns a dict whose keys are exactly
    the §7 envelope keys.
    """
    if status not in STATUS_VOCABULARY:
        raise ValueError(
            f"unknown status {status!r}; expected one of {sorted(STATUS_VOCABULARY)}"
        )

    if extra:
        unknown = [k for k in extra if k not in ALLOWED_TOP_LEVEL_KEYS]
        if unknown:
            raise ValueError(
                f"unknown top-level keys for envelope: {sorted(unknown)}"
            )

    envelope: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "status_reason": status_reason,
        "agent": agent,
        "model": model,
        "session_id": session_id,
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
        "tokens": dict(tokens) if tokens is not None else None,
        "result": result,
        "result_raw_truncated": result_raw_truncated,
        "stderr_tail": stderr_tail,
        "permission_denials": (
            [dict(d) for d in permission_denials]
            if permission_denials is not None
            else []
        ),
        "scope": _normalize_scope(scope),
        "trace": _normalize_trace(trace),
        "error": dict(error) if error is not None else None,
    }

    if extra:
        for k, v in extra.items():
            envelope[k] = v

    # Defensive: enforce the invariant after merging ``extra``.
    final_unknown = [k for k in envelope if k not in ALLOWED_TOP_LEVEL_KEYS]
    if final_unknown:
        raise ValueError(
            f"unknown top-level keys for envelope: {sorted(final_unknown)}"
        )

    return envelope


def _error(code: str, message: str, retriable: bool) -> Dict[str, Any]:
    return {"code": code, "message": message, "retriable": retriable}


# ---------------------------------------------------------------------------
# Public constructors — one per §7 status code
# ---------------------------------------------------------------------------


def build_ok(
    *,
    agent: str,
    model: str,
    session_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    result: Any = None,
    result_raw_truncated: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    permission_denials: Optional[Iterable[Mapping[str, Any]]] = None,
    scope: Optional[Mapping[str, Any]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: ok`` — backend produced a schema-valid inner result."""
    return _build_envelope(
        status="ok",
        status_reason=status_reason,
        agent=agent,
        model=model,
        session_id=session_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tokens=tokens,
        result=result,
        result_raw_truncated=result_raw_truncated,
        stderr_tail=stderr_tail,
        permission_denials=permission_denials,
        scope=scope,
        trace=trace,
        error=None,
        extra=extra,
    )


def build_denied(
    *,
    code: str,
    message: str,
    agent: Optional[str] = None,
    trace: Optional[Mapping[str, Any]] = None,
    scope: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: denied`` — pre-spawn refusal (killswitch, agent_not_allowed,
    cwd_out_of_scope, ...)."""
    return _build_envelope(
        status="denied",
        status_reason=status_reason,
        agent=agent,
        trace=trace,
        scope=scope,
        error=_error(code, message, retriable=False),
        extra=extra,
    )


def build_timeout(
    *,
    duration_ms: int,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    stderr_tail: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: timeout`` — ``subprocess.TimeoutExpired`` from the backend."""
    return _build_envelope(
        status="timeout",
        status_reason=status_reason,
        agent=agent,
        model=model,
        session_id=session_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tokens=tokens,
        stderr_tail=stderr_tail,
        scope=scope,
        trace=trace,
        error=_error(
            "timeout",
            f"backend exceeded timeout (duration_ms={duration_ms})",
            retriable=True,
        ),
        extra=extra,
    )


def build_schema_invalid(
    *,
    message: str,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    result_raw_truncated: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: schema_invalid`` — backend returned JSON but it failed the
    inner-result schema."""
    return _build_envelope(
        status="schema_invalid",
        status_reason=status_reason,
        agent=agent,
        model=model,
        session_id=session_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tokens=tokens,
        result_raw_truncated=result_raw_truncated,
        stderr_tail=stderr_tail,
        scope=scope,
        trace=trace,
        error=_error("schema_invalid", message, retriable=False),
        extra=extra,
    )


def build_backend_error(
    *,
    code: str,
    message: str,
    retriable: bool = False,
    agent: Optional[str] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    result_raw_truncated: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    scope: Optional[Mapping[str, Any]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: backend_error`` — non-JSON stdout, spawn failure, or any
    other backend-side malfunction."""
    return _build_envelope(
        status="backend_error",
        status_reason=status_reason,
        agent=agent,
        model=model,
        session_id=session_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tokens=tokens,
        result_raw_truncated=result_raw_truncated,
        stderr_tail=stderr_tail,
        scope=scope,
        trace=trace,
        error=_error(code, message, retriable=retriable),
        extra=extra,
    )


def build_scope_violation(
    *,
    message: str,
    scope: Mapping[str, Any],
    agent: Optional[str] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    result: Any = None,
    result_raw_truncated: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    permission_denials: Optional[Iterable[Mapping[str, Any]]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: scope_violation`` — observed delta strayed outside the
    declared file set (delta-bounded cleanup detected the breach)."""
    return _build_envelope(
        status="scope_violation",
        status_reason=status_reason,
        agent=agent,
        model=model,
        session_id=session_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tokens=tokens,
        result=result,
        result_raw_truncated=result_raw_truncated,
        stderr_tail=stderr_tail,
        permission_denials=permission_denials,
        scope=scope,
        trace=trace,
        error=_error("scope_violation", message, retriable=False),
        extra=extra,
    )


def build_input_invalid(
    *,
    message: str,
    code: str = "input_invalid",
    agent: Optional[str] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: input_invalid`` — input JSON failed the wrapper-level schema
    or the agent identifier is not in the dispatchable set."""
    return _build_envelope(
        status="input_invalid",
        status_reason=status_reason,
        agent=agent,
        trace=trace,
        error=_error(code, message, retriable=False),
        extra=extra,
    )


def build_manifest_invalid(
    *,
    message: str,
    code: str = "manifest_invalid",
    agent: Optional[str] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: manifest_invalid`` — agent frontmatter / manifest could not
    be loaded or failed validation."""
    return _build_envelope(
        status="manifest_invalid",
        status_reason=status_reason,
        agent=agent,
        trace=trace,
        error=_error(code, message, retriable=False),
        extra=extra,
    )


def build_depth_exceeded(
    *,
    depth: int,
    max_depth: int,
    agent: Optional[str] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: depth_exceeded`` — ``PLAN_EXEC_DISPATCH_DEPTH >= PLAN_EXEC_MAX_DEPTH``."""
    return _build_envelope(
        status="depth_exceeded",
        status_reason=status_reason,
        agent=agent,
        trace=trace,
        error=_error(
            "depth_exceeded",
            f"call depth {depth} >= max_depth {max_depth}",
            retriable=False,
        ),
        extra=extra,
    )


def build_budget_exhausted(
    *,
    cost_usd: float,
    cost_cap_usd: float,
    agent: Optional[str] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: budget_exhausted`` — projected cumulative cost would exceed
    ``PLAN_EXEC_COST_CAP_USD``."""
    return _build_envelope(
        status="budget_exhausted",
        status_reason=status_reason,
        agent=agent,
        cost_usd=cost_usd,
        trace=trace,
        error=_error(
            "budget_exhausted",
            f"cumulative cost {cost_usd} would exceed cap {cost_cap_usd}",
            retriable=False,
        ),
        extra=extra,
    )


def build_cleanup_failure(
    *,
    failed_paths: Iterable[str],
    agent: Optional[str] = None,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
    tokens: Optional[Mapping[str, int]] = None,
    result: Any = None,
    result_raw_truncated: Optional[str] = None,
    stderr_tail: Optional[str] = None,
    permission_denials: Optional[Iterable[Mapping[str, Any]]] = None,
    scope: Optional[Mapping[str, Any]] = None,
    trace: Optional[Mapping[str, Any]] = None,
    status_reason: Optional[str] = None,
    extra: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """``status: cleanup_failure`` — delta-bounded cleanup could not revert
    one or more out-of-declaration writes (per-file ``OSError`` on the
    revert path: read-only target, permission denied, parent dir not
    writable, etc.). The working tree is in an unknown state — this
    takes precedence over backend success because the orchestrator must
    not silently accept a dispatch whose cleanup failed.
    """
    paths = sorted({str(p) for p in (failed_paths or []) if isinstance(p, str)})
    message = (
        "delta-bounded cleanup could not revert "
        f"{len(paths)} out-of-declaration path(s): {paths}"
    )
    return _build_envelope(
        status="cleanup_failure",
        status_reason=status_reason,
        agent=agent,
        model=model,
        session_id=session_id,
        duration_ms=duration_ms,
        cost_usd=cost_usd,
        tokens=tokens,
        result=result,
        result_raw_truncated=result_raw_truncated,
        stderr_tail=stderr_tail,
        permission_denials=permission_denials,
        scope=scope,
        trace=trace,
        error=_error("cleanup_failure", message, retriable=False),
        extra=extra,
    )


__all__: List[str] = [
    "SCHEMA_VERSION",
    "STATUS_VOCABULARY",
    "ALLOWED_TOP_LEVEL_KEYS",
    "build_ok",
    "build_denied",
    "build_timeout",
    "build_schema_invalid",
    "build_backend_error",
    "build_scope_violation",
    "build_input_invalid",
    "build_manifest_invalid",
    "build_depth_exceeded",
    "build_budget_exhausted",
    "build_cleanup_failure",
]
