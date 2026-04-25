"""Atomic span-log writer for ``plan_claude_dispatch.py`` (PLAN_NESTED_DISPATCH TASK-006).

The wrapper appends one JSON line per dispatch to ``$log_dir/spans.jsonl``,
defaulting to ``docs/plans/`` (sibling of ``_run_log.jsonl``). The Codex wrapper
gained the same audit trail via ``_run_log.jsonl`` from day one; nested-Claude
dispatches need an equivalent trace so multi-hop chains
(wrapper -> nested wrapper -> ...) leave behind a parent-linked breadcrumb.

The on-disk format is JSON Lines (one independent JSON object per line, newline
separated). Each line is a *reduced* envelope (top-level metadata, status, key
trace links) rather than the full §7 envelope — large ``result.files_changed``
arrays would balloon the log for no observability gain. The reduced shape is
deliberately additive: future fields can be appended without breaking existing
readers.

Concurrency model
-----------------

Parallel wrapper invocations on disjoint file sets share ``spans.jsonl``.
We rely on POSIX ``O_APPEND`` semantics — atomic per-write for payloads
``<= PIPE_BUF`` bytes (4096 on Linux). For lines that exceed ``PIPE_BUF``,
the writer falls back to ``fcntl.flock`` to serialise the write window so
no torn lines can interleave on disk.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Optional, Union

try:
    import fcntl  # POSIX-only; gracefully degraded if unavailable.
except ImportError:  # pragma: no cover - Windows fallback (we ship POSIX targets).
    fcntl = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default sibling of ``docs/plans/_run_log.jsonl``.
DEFAULT_LOG_RELDIR = "docs/plans"
SPAN_LOG_FILENAME = "spans.jsonl"

#: POSIX guarantees atomic ``O_APPEND`` writes only up to ``PIPE_BUF``
#: (4096 on Linux). Lines longer than that need an explicit lock.
_PIPE_BUF_BYTES = 4096

#: Reduced-envelope keys, in canonical order. Future-extend additively;
#: never remove a key without bumping a documented version.
_SPAN_KEYS = (
    "ts",                          # envelope.trace.ended_at (or .started_at fallback)
    "task_id",                     # envelope.result.task_id (best-effort)
    "agent",                       # envelope.agent
    "status",                      # envelope.status
    "status_reason",               # envelope.status_reason
    "run_id",                      # envelope.trace.run_id
    "span_id",                     # envelope.trace.span_id
    "parent_run_id",               # PLAN_EXEC_PARENT_RUN_ID at dispatch time
    "parent_agent",                # PLAN_EXEC_PARENT_AGENT at dispatch time
    "parent_span_id",              # envelope.trace.parent_span_id
    "dispatch_depth",              # envelope.trace.depth
    "duration_ms",                 # envelope.duration_ms
    "cost_usd",                    # envelope.cost_usd
    "tokens_total",                # sum(envelope.tokens.values())
    "scope_violation_detected",    # envelope.scope.scope_violation_detected
    "scope_misreport_detected",    # envelope.scope.scope_misreport_detected
    "out_of_scope_observed",       # union(observed_delta_tracked, _untracked) - declared
    "session_id",                  # envelope.session_id
)


# ---------------------------------------------------------------------------
# Span construction
# ---------------------------------------------------------------------------


def _coerce_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _tokens_total(tokens: Any) -> Optional[int]:
    if not isinstance(tokens, Mapping):
        return None
    total = 0
    saw_any = False
    for v in tokens.values():
        n = _coerce_int(v)
        if n is None:
            continue
        total += n
        saw_any = True
    return total if saw_any else None


def _out_of_scope_observed(scope: Any) -> int:
    """Count observed-delta paths not in declared_files_changed."""
    if not isinstance(scope, Mapping):
        return 0
    declared = scope.get("declared_files_changed") or []
    tracked = scope.get("observed_delta_tracked") or []
    untracked = scope.get("observed_delta_untracked") or []
    if not isinstance(declared, list):
        declared = []
    if not isinstance(tracked, list):
        tracked = []
    if not isinstance(untracked, list):
        untracked = []
    declared_set = {str(x) for x in declared if isinstance(x, str)}
    observed_set = {str(x) for x in tracked + untracked if isinstance(x, str)}
    return len(observed_set - declared_set)


def build_span(envelope: Mapping[str, Any]) -> dict:
    """Reduce a §7 envelope to its span representation.

    Pulls only metadata + trace + key flags. Inner ``result`` is *not*
    embedded (it can be large; consumers wanting the full envelope should
    capture stdout from ``plan_claude_dispatch.py run``). The single best-
    effort exception is ``result.task_id`` (a short string), since spans
    are most useful when grep-able by task.
    """
    if not isinstance(envelope, Mapping):
        # Defensive: a non-mapping envelope shouldn't reach here, but if
        # it does, emit a minimal span rather than raising.
        return {k: None for k in _SPAN_KEYS}

    trace = envelope.get("trace") if isinstance(envelope.get("trace"), Mapping) else {}
    scope = envelope.get("scope") if isinstance(envelope.get("scope"), Mapping) else {}
    result = envelope.get("result") if isinstance(envelope.get("result"), Mapping) else {}

    # Prefer trace.ended_at; fall back to trace.started_at; then None.
    ts = trace.get("ended_at") or trace.get("started_at")

    # Per-hop bookkeeping isn't on the envelope — the dispatcher injects it
    # via env vars consumed by the *child* hop. For span emission we read
    # the live env (set by ``scrub_env`` for the child process; for the
    # current wrapper itself, this captures the depth at which it was
    # invoked). The wrapper writes its own span using its own trace.run_id;
    # parent_run_id below is the run the *parent* hop owned.
    parent_run_id = os.environ.get("PLAN_EXEC_PARENT_RUN_ID") or None
    parent_agent = os.environ.get("PLAN_EXEC_PARENT_AGENT") or None

    span = {
        "ts": ts,
        "task_id": result.get("task_id") if isinstance(result.get("task_id"), str) else None,
        "agent": envelope.get("agent"),
        "status": envelope.get("status"),
        "status_reason": envelope.get("status_reason"),
        "run_id": trace.get("run_id"),
        "span_id": trace.get("span_id"),
        "parent_run_id": parent_run_id,
        "parent_agent": parent_agent,
        "parent_span_id": trace.get("parent_span_id"),
        "dispatch_depth": _coerce_int(trace.get("depth")),
        "duration_ms": _coerce_int(envelope.get("duration_ms")),
        "cost_usd": _coerce_float(envelope.get("cost_usd")),
        "tokens_total": _tokens_total(envelope.get("tokens")),
        "scope_violation_detected": bool(scope.get("scope_violation_detected", False)),
        "scope_misreport_detected": bool(scope.get("scope_misreport_detected", False)),
        "out_of_scope_observed": _out_of_scope_observed(scope),
        "session_id": envelope.get("session_id"),
    }
    return span


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _envelope_repo_root(envelope: Mapping[str, Any]) -> Optional[str]:
    """Best-effort lookup of ``envelope.scope.repo_root`` if present."""
    scope = envelope.get("scope") if isinstance(envelope, Mapping) else None
    if not isinstance(scope, Mapping):
        return None
    rr = scope.get("repo_root")
    return rr if isinstance(rr, str) and rr else None


def resolve_log_path(
    log_dir: Union[Path, str, None],
    *,
    envelope: Optional[Mapping[str, Any]] = None,
    repo_root: Union[Path, str, None] = None,
) -> Path:
    """Compute the absolute ``spans.jsonl`` path.

    Resolution order:

    1. Explicit ``log_dir`` (treated as the directory containing
       ``spans.jsonl``). If it's a file path ending in ``.jsonl``, it's
       used verbatim.
    2. Explicit ``repo_root`` -> ``<repo_root>/docs/plans/spans.jsonl``.
    3. ``envelope.scope.repo_root`` if present.
    4. ``Path.cwd()/docs/plans/spans.jsonl``.
    """
    if log_dir is not None:
        p = Path(log_dir)
        if p.suffix == ".jsonl":
            return p
        return p / SPAN_LOG_FILENAME

    if repo_root is not None:
        return Path(repo_root) / DEFAULT_LOG_RELDIR / SPAN_LOG_FILENAME

    if envelope is not None:
        rr = _envelope_repo_root(envelope)
        if rr:
            return Path(rr) / DEFAULT_LOG_RELDIR / SPAN_LOG_FILENAME

    return Path.cwd() / DEFAULT_LOG_RELDIR / SPAN_LOG_FILENAME


# ---------------------------------------------------------------------------
# Atomic append
# ---------------------------------------------------------------------------


def _atomic_append_line(path: Path, line: str) -> None:
    """Append one line to ``path`` atomically.

    Strategy:
      - Open with ``O_WRONLY | O_CREAT | O_APPEND`` (POSIX guarantees that
        each ``write`` syscall is atomic for payloads <= PIPE_BUF bytes).
      - For lines longer than PIPE_BUF, take an exclusive ``fcntl.flock``
        for the write window so concurrent writers do not interleave.
      - File mode 0o644 — readable by humans, writable only by owner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    encoded = line.encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    fd = os.open(str(path), flags, 0o644)
    try:
        if len(encoded) > _PIPE_BUF_BYTES and fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                # Filesystems that don't support flock (some network FS)
                # silently fall back to O_APPEND-only atomicity.
                pass
            try:
                _full_write(fd, encoded)
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        else:
            _full_write(fd, encoded)
    finally:
        os.close(fd)


def _full_write(fd: int, data: bytes) -> None:
    """Write ``data`` to ``fd`` in full, retrying on partial writes."""
    view = memoryview(data)
    total = 0
    while total < len(view):
        n = os.write(fd, view[total:])
        if n <= 0:
            raise OSError(f"os.write returned {n}; refusing to spin")
        total += n


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_span(
    log_dir: Union[Path, str, None],
    envelope: Mapping[str, Any],
    *,
    repo_root: Union[Path, str, None] = None,
) -> Path:
    """Atomically write one JSON line to ``$log_dir/spans.jsonl``.

    Parameters
    ----------
    log_dir
        Directory that should contain ``spans.jsonl``. ``None`` selects
        the default (``<repo_root>/docs/plans/`` -> sibling of
        ``_run_log.jsonl``). May also be a full path ending in ``.jsonl``,
        in which case it is used verbatim (escape hatch for tests).
    envelope
        The §7 wrapper envelope. The function reduces it to a span
        (see :func:`build_span`) before serialising — the on-disk row is
        compact even when ``envelope.result`` is large.
    repo_root
        Optional repo root used to derive the default ``log_dir`` when
        ``log_dir`` is ``None``. Falls back to
        ``envelope.scope.repo_root`` (if present) and finally
        ``Path.cwd()``.

    Returns
    -------
    Path
        The absolute path to ``spans.jsonl`` that was appended to. Useful
        for tests; production callers may ignore it.

    Notes
    -----
    Writes are atomic at the line level under concurrency:
    ``O_WRONLY | O_CREAT | O_APPEND`` plus ``fcntl.flock`` for lines
    longer than PIPE_BUF (4096 on Linux).
    """
    span = build_span(envelope)
    line = json.dumps(span, default=str, sort_keys=False) + "\n"

    target = resolve_log_path(log_dir, envelope=envelope, repo_root=repo_root)
    _atomic_append_line(target, line)
    return target


__all__ = [
    "DEFAULT_LOG_RELDIR",
    "SPAN_LOG_FILENAME",
    "append_span",
    "build_span",
    "resolve_log_path",
]
