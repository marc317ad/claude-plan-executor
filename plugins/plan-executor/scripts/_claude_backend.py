"""``claude`` CLI backend adapter for ``plan_claude_dispatch.py``
(PLAN_NESTED_DISPATCH §8.1).

Single public entry point :func:`invoke` shells out to the nested
``claude -p --output-format json`` session and folds the CLI's JSON
top-level ``result`` envelope into the wrapper-level envelope shape
defined in :mod:`_claude_dispatch_envelope`.

Argv shape (PLAN_NESTED_DISPATCH §8.1; cross-checked against the live
probe test in ``tests/scripts/test_claude_permission_mode_probe.py``)::

    claude -p \
        --agent <manifest.name> \
        --permission-mode <effective.permission_mode | "acceptEdits"> \
        --allowedTools <csv from manifest.tools> \
        --disallowedTools Agent \
        --add-dir <effective.cwd> \
        --output-format json \
        <prompt>

Invariants:

  * ``Agent`` is *always* in ``--disallowedTools`` regardless of the
    manifest's tool list. This is the depth-limit invariant: the nested
    session must not be able to spawn a further ``Agent`` hop, otherwise
    the wrapper's depth counter (TASK-002 guardrails) is bypassed.
  * ``--permission-mode`` defaults to ``acceptEdits`` (TASK-007 Pass A).
  * ``--add-dir`` always points at the effective cwd so the nested
    session has read/write scope confined to the directory the wrapper
    expects (delta-bounded cleanup, TASK-004, runs against this dir).
  * ``--output-format json`` is mandatory — the parser asserts JSON
    stdout and surfaces ``backend_error / malformed_output`` otherwise.

Outcome mapping:

  * subprocess returns 0 + parseable JSON  → ``build_ok`` with
    ``result``/``duration_ms``/``cost_usd``/``session_id``/``tokens``
    drained from the JSON envelope.
  * ``subprocess.TimeoutExpired``          → ``build_timeout``.
  * stdout that does not parse as JSON     → ``build_backend_error
    (code=malformed_output)``.
  * non-zero exit code (with parseable JSON) → still ``build_ok`` if the
    JSON envelope is shaped, else ``build_backend_error
    (code=non_zero_exit)``.
  * ``FileNotFoundError`` on the binary    → ``build_backend_error
    (code=binary_not_found)``.

The ``backend_binary`` keyword is the test seam: tests pass a tiny shim
script that mimics ``claude -p --output-format json`` and emits a
hardcoded JSON envelope on stdout. Production callers leave it at the
default ``"claude"``.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import _claude_dispatch_envelope as env_mod

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default ``claude`` binary name (resolved against ``PATH``).
DEFAULT_BACKEND_BINARY = "claude"

#: Default permission mode if ``effective`` does not pin one.  TASK-007
#: Pass A established ``acceptEdits`` as the safe default; ``--add-dir``
#: + ``--allowedTools`` form the actual scope envelope, with delta-bounded
#: cleanup (TASK-004) as the fallback safety mechanism.
DEFAULT_PERMISSION_MODE = "acceptEdits"

#: Default subprocess timeout if ``effective.timeout_sec`` is absent.
#: Conservative — most plan-implementer hops finish in 30-180s; 300s
#: matches the codex-side default in ``plan_codex_dispatch.py``.
DEFAULT_TIMEOUT_SEC = 300

#: Cap for ``result_raw_truncated`` mirror of the raw stdout.
RAW_TRUNCATE_CHARS = 16_384

#: Cap for ``stderr_tail`` excerpt.
STDERR_TAIL_CHARS = 2_000

#: Tool that is always in ``--disallowedTools`` regardless of manifest.
DEPTH_LIMIT_DISALLOWED_TOOL = "Agent"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> str:
    """Best-effort string coercion (used for argv values)."""
    return value if isinstance(value, str) else str(value)


def _truncate(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]


def _stderr_tail(text: str, limit: int = STDERR_TAIL_CHARS) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:]


def _allowed_tools_csv(manifest: Mapping[str, Any]) -> str:
    """Comma-separated tool list for ``--allowedTools``.

    Empty string when manifest declares no tools (the wrapper still
    passes the flag with an empty value so the live ``claude`` CLI knows
    the allowlist is intentionally empty rather than absent).
    """
    tools = manifest.get("tools") if isinstance(manifest, Mapping) else None
    if not isinstance(tools, list):
        return ""
    cleaned = [str(t) for t in tools if isinstance(t, str) and t]
    return ",".join(cleaned)


def _disallowed_tools_csv(effective: Mapping[str, Any]) -> str:
    """Comma-separated disallowed-tool list.

    Always includes :data:`DEPTH_LIMIT_DISALLOWED_TOOL`. Caller-supplied
    ``effective.tools_disallowed_extra`` extras are appended after,
    deduplicated, preserving caller order.
    """
    extras = []
    if isinstance(effective, Mapping):
        raw = effective.get("tools_disallowed_extra")
        if isinstance(raw, list):
            extras = [str(t) for t in raw if isinstance(t, str) and t]
    out: List[str] = [DEPTH_LIMIT_DISALLOWED_TOOL]
    seen = {DEPTH_LIMIT_DISALLOWED_TOOL}
    for t in extras:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return ",".join(out)


def _resolve_cwd(effective: Mapping[str, Any]) -> str:
    """Return the effective cwd as a string suitable for ``--add-dir``.

    Falls back to the current working directory when ``effective`` lacks
    a ``cwd`` entry.
    """
    if isinstance(effective, Mapping):
        cwd = effective.get("cwd")
        if isinstance(cwd, (str, Path)) and str(cwd):
            return str(cwd)
    return str(Path.cwd())


def _resolve_permission_mode(effective: Mapping[str, Any]) -> str:
    if isinstance(effective, Mapping):
        mode = effective.get("permission_mode")
        if isinstance(mode, str) and mode.strip():
            return mode.strip()
    return DEFAULT_PERMISSION_MODE


def _resolve_timeout_sec(effective: Mapping[str, Any]) -> int:
    if isinstance(effective, Mapping):
        raw = effective.get("timeout_sec")
        if isinstance(raw, bool):
            raw = None
        if isinstance(raw, (int, float)) and raw > 0:
            return int(raw)
    return DEFAULT_TIMEOUT_SEC


def _resolve_prompt(payload: Mapping[str, Any]) -> str:
    """Pull the prompt text out of ``payload``.

    Conventions (best-effort, in order):

      * ``payload["prompt"]`` — explicit string from the caller;
      * ``payload["instructions"]`` — fall-back wording used by the
        plan-executor's existing fixtures;
      * else the JSON dump of ``payload`` (the nested session can still
        parse the structured form).
    """
    if not isinstance(payload, Mapping):
        return json.dumps(payload, default=str)
    for key in ("prompt", "instructions"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    return json.dumps(payload, default=str)


def _build_argv(
    *,
    manifest: Mapping[str, Any],
    effective: Mapping[str, Any],
    prompt: str,
    backend_binary: str,
) -> List[str]:
    """Assemble the argv per PLAN_NESTED_DISPATCH §8.1."""
    agent_name = _stringify(manifest.get("name") or "")
    cwd = _resolve_cwd(effective)
    permission_mode = _resolve_permission_mode(effective)
    allowed_csv = _allowed_tools_csv(manifest)
    disallowed_csv = _disallowed_tools_csv(effective)

    argv: List[str] = [
        backend_binary,
        "-p",
        "--agent", agent_name,
        "--permission-mode", permission_mode,
        "--allowedTools", allowed_csv,
        "--disallowedTools", disallowed_csv,
        "--add-dir", cwd,
        "--output-format", "json",
        prompt,
    ]
    return argv


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _normalize_tokens(usage: Any) -> Optional[Dict[str, int]]:
    """Coerce the nested ``claude``'s ``usage`` block into the §7 shape.

    The wire shape (per ``schemas/claude_dispatch_output.json``) requires
    ``input``/``output``/``cache_read``/``cache_creation`` integers. The
    nested CLI emits the same names. Any missing field is filled with
    ``0`` so the envelope still validates.
    """
    if not isinstance(usage, Mapping):
        return None
    keys = ("input", "output", "cache_read", "cache_creation")
    out: Dict[str, int] = {}
    for k in keys:
        v = _coerce_int(usage.get(k))
        out[k] = v if v is not None and v >= 0 else 0
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def invoke(
    manifest: Mapping[str, Any],
    effective: Mapping[str, Any],
    payload: Mapping[str, Any],
    trace: Optional[Mapping[str, Any]] = None,
    *,
    backend_binary: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> Dict[str, Any]:
    """Spawn ``claude -p`` and fold its JSON output into a §7 envelope.

    Parameters
    ----------
    manifest
        The dict returned by ``_claude_agent_manifest.load_agent``. Used
        for ``name`` (→ ``--agent``), ``model`` (echoed into the
        envelope), and ``tools`` (→ ``--allowedTools``).
    effective
        Caller-resolved invocation settings. Recognised keys:
          - ``cwd``               (str) → ``--add-dir``
          - ``permission_mode``   (str) → ``--permission-mode``
          - ``timeout_sec``       (int) → subprocess timeout
          - ``tools_disallowed_extra`` (list[str]) appended after ``Agent``
    payload
        Caller payload. ``prompt`` or ``instructions`` becomes the
        positional argv slot; otherwise the dict is JSON-encoded.
    trace
        Bookkeeping dict (``run_id``, ``span_id``, ``depth`` etc.).
        Echoed into every envelope.
    backend_binary
        Override the ``claude`` binary path. Tests pass a shim script
        path here. Defaults to :data:`DEFAULT_BACKEND_BINARY`.
    env
        Optional sanitised env dict (typically the output of
        :func:`_claude_guardrails.scrub_env`). When ``None`` the parent
        env is inherited.

    Returns
    -------
    dict
        One of the §7 envelopes built by ``_claude_dispatch_envelope``.
        ``status`` is always set; ``schema_version`` is always 1; no
        unknown top-level keys are introduced.
    """
    binary = backend_binary if backend_binary is not None else DEFAULT_BACKEND_BINARY
    agent_name = manifest.get("name") if isinstance(manifest, Mapping) else None
    model_name = manifest.get("model") if isinstance(manifest, Mapping) else None
    timeout_sec = _resolve_timeout_sec(effective)
    prompt_text = _resolve_prompt(payload)

    argv = _build_argv(
        manifest=manifest,
        effective=effective,
        prompt=prompt_text,
        backend_binary=binary,
    )

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        stderr_text = ""
        if exc.stderr is not None:
            try:
                stderr_text = exc.stderr.decode("utf-8", errors="replace") \
                    if isinstance(exc.stderr, (bytes, bytearray)) else str(exc.stderr)
            except Exception:
                stderr_text = ""
        return env_mod.build_timeout(
            duration_ms=elapsed_ms,
            agent=agent_name,
            model=model_name,
            stderr_tail=_stderr_tail(stderr_text),
            trace=trace,
        )
    except FileNotFoundError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return env_mod.build_backend_error(
            code="binary_not_found",
            message=f"backend binary not found: {binary!r} ({exc})",
            agent=agent_name,
            model=model_name,
            duration_ms=elapsed_ms,
            trace=trace,
        )
    except OSError as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return env_mod.build_backend_error(
            code="spawn_failed",
            message=f"failed to spawn backend: {exc}",
            agent=agent_name,
            model=model_name,
            duration_ms=elapsed_ms,
            trace=trace,
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    stdout_text = proc.stdout if isinstance(proc.stdout, str) else ""
    stderr_text = proc.stderr if isinstance(proc.stderr, str) else ""
    raw_truncated = _truncate(stdout_text, RAW_TRUNCATE_CHARS)
    stderr_tail = _stderr_tail(stderr_text)

    # Try to parse stdout as JSON. Non-JSON → backend_error / malformed_output.
    stripped = stdout_text.strip()
    if not stripped:
        return env_mod.build_backend_error(
            code="malformed_output",
            message=(
                f"backend produced empty stdout (rc={proc.returncode})"
            ),
            agent=agent_name,
            model=model_name,
            duration_ms=elapsed_ms,
            result_raw_truncated=raw_truncated,
            stderr_tail=stderr_tail,
            trace=trace,
        )
    try:
        cli_envelope = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return env_mod.build_backend_error(
            code="malformed_output",
            message=f"backend stdout is not JSON: {exc.msg}",
            agent=agent_name,
            model=model_name,
            duration_ms=elapsed_ms,
            result_raw_truncated=raw_truncated,
            stderr_tail=stderr_tail,
            trace=trace,
        )

    if not isinstance(cli_envelope, Mapping):
        return env_mod.build_backend_error(
            code="malformed_output",
            message=(
                f"backend stdout JSON was not an object "
                f"(got {type(cli_envelope).__name__})"
            ),
            agent=agent_name,
            model=model_name,
            duration_ms=elapsed_ms,
            result_raw_truncated=raw_truncated,
            stderr_tail=stderr_tail,
            trace=trace,
        )

    # Map the CLI's top-level fields into the §7 envelope.
    inner_result = cli_envelope.get("result")
    cli_duration = _coerce_int(cli_envelope.get("duration_ms"))
    cli_cost = _coerce_float(cli_envelope.get("total_cost_usd"))
    cli_session_id = cli_envelope.get("session_id")
    if cli_session_id is not None and not isinstance(cli_session_id, str):
        cli_session_id = str(cli_session_id)
    cli_tokens = _normalize_tokens(cli_envelope.get("usage"))
    permission_denials_raw = cli_envelope.get("permission_denials")
    permission_denials: Optional[List[Dict[str, Any]]] = None
    if isinstance(permission_denials_raw, list):
        permission_denials = [
            dict(d) for d in permission_denials_raw if isinstance(d, Mapping)
        ]

    # Prefer the CLI's self-reported duration when it's present and sane;
    # otherwise fall back to wall-clock measurement.
    duration_ms = cli_duration if cli_duration is not None and cli_duration >= 0 else elapsed_ms

    if proc.returncode != 0:
        # Non-zero exit but parseable JSON: surface as backend_error.
        return env_mod.build_backend_error(
            code="non_zero_exit",
            message=(
                f"backend exited with rc={proc.returncode}"
            ),
            agent=agent_name,
            model=model_name,
            session_id=cli_session_id,
            duration_ms=duration_ms,
            cost_usd=cli_cost,
            tokens=cli_tokens,
            result_raw_truncated=raw_truncated,
            stderr_tail=stderr_tail,
            trace=trace,
        )

    return env_mod.build_ok(
        agent=str(agent_name) if agent_name is not None else "unknown",
        model=str(model_name) if model_name is not None else "unknown",
        session_id=cli_session_id,
        duration_ms=duration_ms,
        cost_usd=cli_cost,
        tokens=cli_tokens,
        result=inner_result,
        result_raw_truncated=raw_truncated,
        stderr_tail=stderr_tail,
        permission_denials=permission_denials,
        trace=trace,
    )


__all__ = [
    "DEFAULT_BACKEND_BINARY",
    "DEFAULT_PERMISSION_MODE",
    "DEFAULT_TIMEOUT_SEC",
    "DEPTH_LIMIT_DISALLOWED_TOOL",
    "RAW_TRUNCATE_CHARS",
    "STDERR_TAIL_CHARS",
    "invoke",
]
