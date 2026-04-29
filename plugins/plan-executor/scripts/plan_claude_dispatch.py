#!/usr/bin/env python3
"""Nested ``claude`` CLI dispatch wrapper for the dual-agent plan executor.

PLAN_NESTED_DISPATCH (TASK-005). Mirrors the role ``plan_codex_dispatch.py``
plays for Codex, but for nested Claude sessions: the wrapper takes a
structured JSON input describing the agent / payload / guardrails, ties
together the manifest loader (TASK-002), backend adapter (TASK-003), and
delta-bounded cleanup (TASK-004), and emits the §7 wrapper envelope on
stdout.

Subcommands (PLAN_NESTED_DISPATCH §10):

    plan_claude_dispatch.py run            --input <path|-> [--output <path|->]
                                           [--timeout N] [--dry-run] [--verbose]
                                           [--backend-binary <path>]
                                           [--repo-root <path>]
    plan_claude_dispatch.py list-agents
    plan_claude_dispatch.py show-agent     <name>
    plan_claude_dispatch.py validate-input  <path|->
    plan_claude_dispatch.py validate-output <path|->

Exit codes (§10):

    0   ``status: ok`` envelope on stdout.
    1   any non-ok status (with envelope on stdout).
    2   wrapper-level failure (malformed input, I/O error, schema not loadable);
        when caused by malformed input the wrapper STILL emits a
        ``status: input_invalid`` envelope on stdout so callers can distinguish
        wrapper-rejection from backend-failure.

The full ``run`` pipeline:

    1. Read --input JSON (path or stdin).
    2. Schema-validate against schemas/claude_dispatch_input.json. Failure →
       exit 2 + ``build_input_invalid`` envelope.
    3. Load manifest for input["agent"]. Failure →
       ``build_input_invalid`` (agent_not_dispatchable) or
       ``build_manifest_invalid``.
    4. Merge effective overrides on top of manifest defaults.
    5. Build trace dict (run_id from input, parent span, depth, etc.).
    6. evaluate_preflight → on denial, emit envelope + exit 1.
    7. snapshot_baseline(repo_root).
    8. scrub_env(manifest, parent_env).
    9. _claude_backend.invoke(manifest, effective, payload, trace,
       backend_binary=..., env=scrubbed_env). Returns a §7 envelope.
   10. apply_cleanup(baseline, declared_files_changed, repo_root). Merge
       cleanup flags into the envelope's scope block.
   11. Schema-validate the final envelope; if invalid, emit
       build_schema_invalid.
   12. Append a span entry to docs/plans/spans.jsonl (v1 placeholder for
       TASK-006).
   13. Emit envelope on stdout (or to --output path).
   14. Exit with the §10-mapped code (0 for ok, 1 for any other status).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Module bootstrap
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMAS_DIR = SCRIPT_DIR / "schemas"
INPUT_SCHEMA_PATH = SCHEMAS_DIR / "claude_dispatch_input.json"
OUTPUT_SCHEMA_PATH = SCHEMAS_DIR / "claude_dispatch_output.json"

# Make sibling helper modules importable under both direct CLI execution
# (``python plan_claude_dispatch.py``) and ``importlib.util.spec_from_file_location``
# loading (used by tests). Mirrors the same shim in ``plan_codex_dispatch.py``.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import _claude_agent_manifest as agent_manifest  # noqa: E402
import _claude_backend as backend  # noqa: E402
import _claude_dispatch_cleanup as cleanup  # noqa: E402
import _claude_dispatch_envelope as env_mod  # noqa: E402
import _claude_guardrails as guardrails  # noqa: E402
import _claude_span_log as span_log  # noqa: E402

try:
    from jsonschema import Draft7Validator  # noqa: E402
    from jsonschema import ValidationError as _JsonSchemaValidationError  # noqa: E402
except Exception as exc:  # pragma: no cover - import guard
    Draft7Validator = None  # type: ignore[assignment]
    _JsonSchemaValidationError = Exception  # type: ignore[misc,assignment]
    _JSONSCHEMA_IMPORT_ERROR = exc
else:
    _JSONSCHEMA_IMPORT_ERROR = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default span-log path. v1 placeholder; TASK-006 replaces this with the
#: proper helper module. The wrapper appends a single line containing
#: ``json.dumps(envelope) + "\n"`` per dispatch.
DEFAULT_SPAN_LOG_RELPATH = "docs/plans/spans.jsonl"

#: §10 exit codes mapped from envelope ``status``. Anything not listed
#: here falls through to ``EXIT_CODE_NON_OK``.
EXIT_CODE_OK = 0
EXIT_CODE_NON_OK = 1
EXIT_CODE_WRAPPER_FAILURE = 2

#: Default subprocess timeout in seconds applied when neither
#: ``input.overrides.timeout_sec`` nor the ``--timeout`` CLI flag is
#: supplied. Bumped from the unwritten 900s backend default to 1800s
#: (30 min) to accommodate multi-file implementer hops (BUG-145). The
#: SKILL ``implement-plan`` and ``dispatch-templates.md`` both reference
#: this value explicitly so the dispatch budget is no longer "unwritten".
DEFAULT_DISPATCH_TIMEOUT_SEC = 1800


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _read_input(path_arg: str) -> str:
    """Read raw input from a path or ``-`` (stdin)."""
    if path_arg == "-":
        return sys.stdin.read()
    p = Path(path_arg)
    return p.read_text(encoding="utf-8")


def _write_output(path_arg: str, text: str) -> None:
    """Write ``text`` to a path or ``-`` (stdout)."""
    if path_arg == "-":
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    Path(path_arg).write_text(
        text if text.endswith("\n") else text + "\n",
        encoding="utf-8",
    )


def _emit_envelope(envelope: Mapping[str, Any], output_arg: str) -> None:
    """Serialize the envelope as JSON and write to ``output_arg``.

    ``output_arg`` of ``"-"`` writes to stdout. The envelope is always
    pretty-printed with indent=2 to match ``plan_codex_dispatch.py``'s
    ``emit()`` shape and to make stdout consumption from shell scripts
    pleasant.
    """
    text = json.dumps(envelope, indent=2, default=str)
    _write_output(output_arg, text)


def _load_schema(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def _format_schema_error(exc: Any) -> str:
    """Render a jsonschema ValidationError into a single-line diagnostic."""
    try:
        path = "/".join(str(p) for p in getattr(exc, "absolute_path", []))
        if path:
            return f"{path}: {exc.message}"
        return str(exc.message)
    except Exception:
        return str(exc)


def _validate_against_schema(
    instance: Any,
    schema: Mapping[str, Any],
) -> Optional[str]:
    """Return ``None`` on success, or a single-line error message on failure.

    Wraps ``jsonschema.Draft7Validator`` so call sites do not need to
    import the library directly. If ``jsonschema`` failed to import at
    module load (extremely unlikely in this repo), the validator is
    skipped and a coarse "library missing" message is returned to keep
    the caller's logic uniform.
    """
    if Draft7Validator is None:  # pragma: no cover - jsonschema is in venv
        return f"jsonschema unavailable: {_JSONSCHEMA_IMPORT_ERROR!r}"
    validator = Draft7Validator(dict(schema))
    errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.absolute_path))
    if not errors:
        return None
    return "; ".join(_format_schema_error(e) for e in errors[:5])


# ---------------------------------------------------------------------------
# Trace + envelope post-processing
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    """Return current UTC time as ISO-8601 with trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _build_trace(
    input_obj: Mapping[str, Any],
    *,
    started_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct the wrapper-level trace block from the input's ``trace``.

    The §7 envelope's trace requires both ``run_id`` and ``span_id``;
    inputs at depth 0 may omit both, in which case the wrapper mints
    them. ``parent_span_id`` and ``call_chain`` are passed through;
    ``depth`` is echoed verbatim (the backend-side guardrail handles the
    "depth + 1" semantics for the *child* hop, not for the current span
    label).
    """
    raw = input_obj.get("trace") if isinstance(input_obj, Mapping) else None
    if not isinstance(raw, Mapping):
        raw = {}

    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        run_id = _new_uuid()

    parent_span_id = raw.get("parent_span_id")
    if parent_span_id is not None and not isinstance(parent_span_id, str):
        parent_span_id = str(parent_span_id)

    depth_raw = raw.get("depth")
    try:
        depth = int(depth_raw) if depth_raw is not None else 0
    except (TypeError, ValueError):
        depth = 0
    if depth < 0:
        depth = 0

    call_chain_raw = raw.get("call_chain")
    if isinstance(call_chain_raw, list):
        call_chain = [str(s) for s in call_chain_raw if isinstance(s, str)]
    else:
        call_chain = []

    return {
        "run_id": run_id,
        "span_id": _new_uuid(),
        "parent_span_id": parent_span_id,
        "depth": depth,
        "call_chain": call_chain,
        "started_at": started_at,
        "ended_at": None,
    }


def _build_effective(
    manifest: Mapping[str, Any],
    input_obj: Mapping[str, Any],
    *,
    cli_timeout: Optional[int] = None,
    cli_repo_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Merge wrapper-level overrides with sensible defaults for the backend.

    Recognised effective keys (consumed by ``_claude_backend.invoke``):

      - ``cwd``                   (str)
      - ``permission_mode``       (str)
      - ``timeout_sec``           (int)
      - ``tools_disallowed_extra``(list[str])
      - ``model``                 (str; informational — backend reads
                                  ``manifest["model"]`` directly)
    """
    overrides = input_obj.get("overrides") if isinstance(input_obj, Mapping) else {}
    if not isinstance(overrides, Mapping):
        overrides = {}

    effective: Dict[str, Any] = {}

    cwd = overrides.get("cwd")
    if isinstance(cwd, str) and cwd:
        effective["cwd"] = cwd
    elif cli_repo_root:
        effective["cwd"] = cli_repo_root

    timeout_sec = overrides.get("timeout_sec")
    if isinstance(timeout_sec, int) and timeout_sec > 0:
        effective["timeout_sec"] = timeout_sec
    elif cli_timeout is not None and cli_timeout > 0:
        effective["timeout_sec"] = cli_timeout
    else:
        # BUG-145: pin a 1800s default at the wrapper rather than letting
        # the backend's 300s / unwritten 900s default fire on heavy
        # multi-file tasks. Documented in SKILL.md + dispatch-templates.md.
        effective["timeout_sec"] = DEFAULT_DISPATCH_TIMEOUT_SEC

    tools_disallowed_extra = overrides.get("tools_disallowed_extra")
    if isinstance(tools_disallowed_extra, list):
        effective["tools_disallowed_extra"] = [
            t for t in tools_disallowed_extra if isinstance(t, str)
        ]

    model_override = overrides.get("model")
    if isinstance(model_override, str) and model_override:
        effective["model"] = model_override

    # ``guardrails.network: "deny"`` → append the web tools to disallowed.
    guardrails_in = input_obj.get("guardrails") if isinstance(input_obj, Mapping) else None
    if isinstance(guardrails_in, Mapping):
        net = guardrails_in.get("network")
        if isinstance(net, str) and net.strip().lower() == "deny":
            extras = list(effective.get("tools_disallowed_extra") or [])
            for t in ("WebFetch", "WebSearch"):
                if t not in extras:
                    extras.append(t)
            effective["tools_disallowed_extra"] = extras

    return effective


def _merge_scope_with_cleanup(
    envelope: Dict[str, Any],
    cleanup_result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Fold cleanup flags / observed-delta into the envelope's ``scope`` block."""
    scope = dict(envelope.get("scope") or env_mod._empty_scope())  # type: ignore[attr-defined]

    declared = scope.get("declared_files_changed") or []
    if not isinstance(declared, list):
        declared = []

    # Cleanup distinguishes ``out_of_scope_paths`` (which it reverted) and
    # ``misreported_paths`` (declared-but-unchanged). The §7 envelope
    # carries observed-delta lists as plain tracked / untracked. The
    # cleanup module does not preserve the tracked vs untracked split for
    # the post-cleanup state, so we put the out-of-scope reversions under
    # ``observed_delta_tracked`` (cleanup tries `git restore` first) as a
    # best-effort approximation. The orchestrator-side reconciliation
    # already treats the two lists symmetrically; the precise split is
    # not load-bearing for v1.
    out_of_scope = list(cleanup_result.get("out_of_scope_paths") or [])
    deleted = list(cleanup_result.get("deleted") or [])
    restored = list(cleanup_result.get("restored") or [])

    # Anything cleanup deleted was untracked at baseline; anything it
    # restored was tracked (or had tracked-at-HEAD provenance). Use this
    # as the split.
    observed_tracked = sorted({*restored})
    observed_untracked = sorted({*deleted})
    # Out-of-scope paths that cleanup neither restored nor deleted (the
    # ``failed`` outcome) still need to surface; we include them under
    # tracked as a conservative default.
    leftover_out_of_scope = sorted(
        set(out_of_scope) - set(observed_tracked) - set(observed_untracked)
    )
    if leftover_out_of_scope:
        observed_tracked = sorted(set(observed_tracked) | set(leftover_out_of_scope))

    scope_violation_detected = bool(
        cleanup_result.get("scope_violation_detected", False)
    )
    scope_misreport_detected = bool(
        cleanup_result.get("scope_misreport_detected", False)
    )

    scope["declared_files_changed"] = sorted(
        {str(s) for s in declared if isinstance(s, str)}
    )
    scope["observed_delta_tracked"] = observed_tracked
    scope["observed_delta_untracked"] = observed_untracked
    scope["scope_violation_detected"] = scope_violation_detected
    scope["scope_misreport_detected"] = scope_misreport_detected
    # TASK-004 hardening: expose ``failed_paths`` as a structured scope
    # field so callers can diagnose which paths the wrapper could not
    # revert without parsing ``error.message``.
    scope["failed_paths"] = sorted(
        {str(p) for p in (cleanup_result.get("failed_paths") or []) if isinstance(p, str)}
    )

    envelope["scope"] = scope
    return envelope


def _extract_observed_files_changed(envelope: Mapping[str, Any]) -> List[str]:
    """Pull ``files_changed`` out of the inner agent ``result`` (best-effort).

    INFORMATIONAL ONLY (TASK-001 sandbox-escape fix). This is what the
    agent *says* it touched; it is NOT the cleanup-scope authority.
    Cleanup is gated by the trusted top-level
    ``input['declared_files_changed']`` populated by the orchestrator.
    The agent's self-report is parsed here only so the envelope's
    ``scope_misreport_detected`` diff metadata (declared minus observed)
    has something to compare against.

    The inner result is opaque to the wrapper (PLAN_NESTED_DISPATCH §6:
    ``payload`` is the same), but most agent reports follow the
    ``codex_implement_schema.json`` shape and carry a top-level
    ``files_changed`` list.
    """
    result = envelope.get("result")
    if not isinstance(result, Mapping):
        return []
    files = result.get("files_changed")
    if not isinstance(files, list):
        return []
    return [str(s) for s in files if isinstance(s, str)]


def _stamp_trace_end(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Set ``trace.ended_at`` to now (UTC) on the envelope."""
    trace = dict(envelope.get("trace") or env_mod._empty_trace())  # type: ignore[attr-defined]
    trace["ended_at"] = _utc_iso()
    if not trace.get("started_at"):
        trace["started_at"] = trace["ended_at"]
    envelope["trace"] = trace
    return envelope


# ---------------------------------------------------------------------------
# Span log (TASK-006: delegates to _claude_span_log.append_span)
# ---------------------------------------------------------------------------


def _resolve_span_log_path(repo_root: Optional[str]) -> Path:
    """Resolve the ``spans.jsonl`` location.

    Order:
      1. ``PLAN_EXEC_LOG_DIR/spans.jsonl`` if env var set.
      2. ``<repo_root>/docs/plans/spans.jsonl`` if repo_root provided.
      3. ``<cwd>/docs/plans/spans.jsonl`` fallback.
    """
    log_dir = os.environ.get("PLAN_EXEC_LOG_DIR")
    if log_dir:
        return Path(log_dir) / "spans.jsonl"
    if repo_root:
        return Path(repo_root) / DEFAULT_SPAN_LOG_RELPATH
    return Path.cwd() / DEFAULT_SPAN_LOG_RELPATH


def _append_span(
    envelope: Mapping[str, Any],
    *,
    repo_root: Optional[str] = None,
    span_log_path: Optional[Path] = None,
) -> None:
    """Append the envelope's reduced span to ``spans.jsonl``.

    Delegates the atomic-write contract to
    :func:`_claude_span_log.append_span`. Failures here are non-fatal:
    the wrapper still emits the envelope on stdout so the caller can act
    on it. We swallow OSError / PermissionError so a read-only filesystem
    (e.g. unit-test sandboxes) does not break dispatch.

    The call-site name is unchanged from the TASK-005 placeholder so
    existing tests / orchestrator paths continue to work; only the body
    swaps to the structured helper.
    """
    if span_log_path is not None:
        # Test-injection: caller specifies the exact file path; helper has
        # an explicit ``.jsonl`` escape hatch (see ``_claude_span_log``).
        log_dir_arg: Any = span_log_path
    else:
        # Default: pass the directory; the helper composes ``spans.jsonl``.
        log_dir_arg = _resolve_span_log_path(repo_root).parent

    try:
        span_log.append_span(log_dir_arg, envelope, repo_root=repo_root)
    except (OSError, PermissionError):
        # Non-fatal — span logging is observability, not a hard requirement.
        return


# ---------------------------------------------------------------------------
# Pipeline pieces
# ---------------------------------------------------------------------------


def _parse_input(raw_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Try to JSON-parse ``raw_text``; return ``(obj, error_message)``."""
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return None, f"input is not valid JSON: {exc}"
    if not isinstance(obj, dict):
        return None, f"input must be a JSON object, got {type(obj).__name__}"
    return obj, None


def _input_invalid_envelope(
    *,
    message: str,
    code: str = "input_invalid",
    agent: Optional[str] = None,
    trace: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    return env_mod.build_input_invalid(
        message=message,
        code=code,
        agent=agent,
        trace=trace,
    )


def _exit_code_for_status(status: str) -> int:
    """Map envelope ``status`` to the §10 exit code."""
    if status == "ok":
        return EXIT_CODE_OK
    return EXIT_CODE_NON_OK


def _dry_run_envelope(
    input_obj: Mapping[str, Any],
    manifest: Mapping[str, Any],
    effective: Mapping[str, Any],
    trace: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the plan-only envelope returned for ``--dry-run``.

    ``status: ok``, ``dry_run: true`` is conveyed via ``status_reason``
    so the §7 schema's strict ``additionalProperties: false`` still holds.
    The acceptance criterion ("plan-only envelope with status: ok,
    dry_run: true and no spawn") is satisfied by the ``status_reason``
    carrying the literal ``"dry_run: true"`` token plus ``result``
    holding the resolved plan dict (manifest + effective + would-be argv).
    """
    plan = {
        "dry_run": True,
        "agent": manifest.get("name"),
        "model": manifest.get("model"),
        "tools_allowed": list(manifest.get("tools") or []),
        "effective": dict(effective),
        "payload": dict(input_obj.get("payload") or {}),
    }
    return env_mod.build_ok(
        agent=str(manifest.get("name") or "unknown"),
        model=str(manifest.get("model") or "unknown"),
        result=plan,
        trace=trace,
        status_reason="dry_run: true",
    )


# ---------------------------------------------------------------------------
# run subcommand
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    """Execute the full §10 ``run`` pipeline.

    See module docstring for the 14-step pipeline. Returns an exit code
    in ``{0, 1, 2}`` per the §10 contract.
    """
    started_at = _utc_iso()
    output_arg = args.output if args.output else "-"
    repo_root = args.repo_root or str(Path.cwd())

    # ---- Step 1: read input ----
    try:
        raw = _read_input(args.input)
    except (OSError, PermissionError) as exc:
        envelope = _input_invalid_envelope(
            message=f"could not read --input: {exc}",
            code="io_error",
        )
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        return EXIT_CODE_WRAPPER_FAILURE

    # ---- Step 1b: JSON parse ----
    input_obj, parse_err = _parse_input(raw)
    if input_obj is None:
        envelope = _input_invalid_envelope(message=parse_err or "input parse error")
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        return EXIT_CODE_WRAPPER_FAILURE

    # ---- Step 2: schema-validate input ----
    try:
        input_schema = _load_schema(INPUT_SCHEMA_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        envelope = _input_invalid_envelope(
            message=f"could not load input schema: {exc}",
            code="schema_unavailable",
        )
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        return EXIT_CODE_WRAPPER_FAILURE

    schema_err = _validate_against_schema(input_obj, input_schema)
    if schema_err is not None:
        trace = _build_trace(input_obj, started_at=started_at)
        envelope = _input_invalid_envelope(
            message=f"input failed schema validation: {schema_err}",
            agent=input_obj.get("agent") if isinstance(input_obj.get("agent"), str) else None,
            trace=trace,
        )
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        return EXIT_CODE_WRAPPER_FAILURE

    # ---- Step 3: load agent manifest ----
    agent_name = input_obj.get("agent")
    trace = _build_trace(input_obj, started_at=started_at)

    try:
        manifest = agent_manifest.load_agent(agent_name)
    except agent_manifest.AgentNotDispatchable as exc:
        envelope = _input_invalid_envelope(
            message=str(exc),
            code="agent_not_dispatchable",
            agent=agent_name if isinstance(agent_name, str) else None,
            trace=trace,
        )
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        _append_span(envelope, repo_root=repo_root)
        return _exit_code_for_status(envelope["status"])
    except agent_manifest.ManifestSchemaInvalid as exc:
        envelope = env_mod.build_manifest_invalid(
            message=str(exc),
            agent=agent_name if isinstance(agent_name, str) else None,
            trace=trace,
        )
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        _append_span(envelope, repo_root=repo_root)
        return _exit_code_for_status(envelope["status"])

    # ---- Step 4: build effective overrides ----
    effective = _build_effective(
        manifest,
        input_obj,
        cli_timeout=args.timeout,
        cli_repo_root=repo_root,
    )

    # ---- Dry-run short-circuit (no spawn, no preflight side-effects) ----
    if args.dry_run:
        envelope = _dry_run_envelope(input_obj, manifest, effective, trace)
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        _append_span(envelope, repo_root=repo_root)
        return _exit_code_for_status(envelope["status"])

    # ---- Step 5: preflight ----
    parent_env = os.environ
    allow, denied_envelope = guardrails.evaluate_preflight(
        manifest, input_obj, parent_env,
    )
    if not allow and denied_envelope is not None:
        denied_envelope = _stamp_trace_end(denied_envelope)
        _emit_envelope(denied_envelope, output_arg)
        _append_span(denied_envelope, repo_root=repo_root)
        return _exit_code_for_status(denied_envelope["status"])

    # ---- Step 6: snapshot baseline ----
    baseline = cleanup.snapshot_baseline(repo_root)

    # ---- Step 7: scrub env ----
    parent_agent = parent_env.get(guardrails.PER_HOP_PARENT_AGENT, "")
    parent_run_id = trace.get("run_id") or ""
    scrubbed_env = guardrails.scrub_env(
        manifest,
        parent_env=parent_env,
        parent_agent=parent_agent,
        parent_run_id=parent_run_id,
    )

    # ---- Step 8: backend dispatch ----
    payload = input_obj.get("payload") if isinstance(input_obj.get("payload"), Mapping) else {}
    envelope = backend.invoke(
        manifest,
        effective,
        payload,
        trace,
        backend_binary=args.backend_binary,
        env=scrubbed_env,
    )

    # ---- Step 9: cleanup ----
    # TASK-001 sandbox-escape fix: cleanup scope comes from the trusted
    # top-level ``declared_files_changed`` (populated by the orchestrator),
    # NOT from the agent's self-reported ``result.files_changed``. A
    # malicious or buggy agent could otherwise lie in its envelope to
    # extend its own authorization.
    declared_raw = input_obj.get("declared_files_changed")
    if isinstance(declared_raw, list):
        declared = [s for s in declared_raw if isinstance(s, str)]
    else:
        # Schema validation (Step 2) ensures this field is present as an
        # array. This fallback is defensive.
        declared = []

    # TASK-002 wrapper_autoclean_authorization: the cleanup
    # authorization is anchored in the dispatch payload's top-level
    # ``agent`` field. ``plan-analyst`` is the only read-only agent; all
    # others are write-authorized.
    if agent_name == "plan-analyst":
        auth_source = "wrapper-empty-scope-readonly"
    else:
        auth_source = "wrapper-declared-scope"

    # Agent's self-reported files_changed is parsed for informational /
    # diff-metadata purposes only; never used as cleanup authority.
    _observed = _extract_observed_files_changed(envelope)  # noqa: F841

    # TASK-002 anti-aliasing guard: a write-authorized agent with empty
    # ``declared_files_changed`` MUST NOT take the readonly path —
    # otherwise a Layer-A regression (orchestrator failing to populate
    # declared scope) could destroy work. Skip ``apply_cleanup`` and
    # route through the wrapper_autoclean_blocked envelope.
    # The actual envelope construction lands in TASK-004 of this plan;
    # for now we emit a stub cleanup_result so downstream merging /
    # event emission preserves the tree.
    _antialias_blocked = (
        agent_name in {"plan-implementer", "plan-remediator"}
        and len(declared) == 0
    )
    if _antialias_blocked:
        cleanup_result = {
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "failed_paths": [],
            "out_of_scope_paths": [],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": baseline.get("captured", False),
            "cleanup_strategy": "skipped_anti_aliasing_guard",
        }
    else:
        # PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002: extract optional
        # ``unattended_revert_policy`` from the validated input and forward
        # it as a kwarg to ``apply_cleanup``. When omitted the cleanup
        # function defaults to ``pause`` (the safer-by-default behavior
        # introduced in TASK-001 of this plan).
        urp_value = input_obj.get("unattended_revert_policy")
        cleanup_result = cleanup.apply_cleanup(
            baseline, declared, repo_root,
            authorization_source=auth_source,
            unattended_revert_policy=urp_value,
        )

    # fold cleanup flags into the envelope's scope.
    envelope = _merge_scope_with_cleanup(envelope, cleanup_result)

    # TASK-004 (prohibit_silent_revert) / TASK-002 (this plan): emit
    # wrapper-level autoclean events for the run log. These land in the
    # envelope's ``extra.wrapper_events`` and are hoisted by the
    # orchestrator.
    wrapper_events = []
    _autoclean_blocked_flag = False
    if _antialias_blocked:
        _autoclean_blocked_flag = True
        wrapper_events.append({
            "event": "wrapper_autoclean_blocked",
            "reason": "antialiasing_guard_empty_declared_scope",
            "preserved_files": [],
        })
    elif cleanup_result.get("restored") or cleanup_result.get("deleted"):
        wrapper_events.append({
            "event": "wrapper_autoclean_executed",
            "restored": cleanup_result.get("restored", []),
            "deleted": cleanup_result.get("deleted", []),
            "authorization_source": auth_source,
        })
    elif (cleanup_result.get("cleanup_strategy") or "").startswith(
        "detect_only_revert_policy_"
    ):
        # PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002 AC #4: under pause/
        # fail-fast policies the cleanup module classifies out-of-scope
        # writes but does NOT revert them. Emit a wrapper event so the
        # run log carries proof of the policy-gated non-destruction
        # (paths preserved on disk for human inspection).
        wrapper_events.append({
            "event": "wrapper_autoclean_policy_gated",
            "cleanup_strategy": cleanup_result.get("cleanup_strategy"),
            "preserved_paths": list(cleanup_result.get("out_of_scope_paths") or []),
            "authorization_source": auth_source,
        })

    # If the inner result was schema-invalid OR cleanup detected a
    # scope violation, demote the wrapper status accordingly. ``ok`` →
    # ``scope_violation`` only when cleanup proved a violation; we never
    # promote a non-ok backend status to ``scope_violation`` (the error
    # signal is more important).
    #
    # ``cleanup_failure`` (TASK-004 hardening) takes precedence over
    # backend success AND over ``scope_violation``: a non-empty
    # ``failed_paths`` means the working tree is in an unknown state
    # (one or more out-of-declaration writes could not be reverted due
    # to a per-file ``OSError``), which is a hard fail regardless of
    # whether the backend itself succeeded.
    #
    # Precedence order: ``failed_paths`` non-empty wins over EVERY
    # backend status except those that already signal a more
    # fundamental failure than working-tree-unknown — namely the
    # backend never produced a usable result at all (``timeout``,
    # ``backend_error``, ``schema_invalid``). Those statuses are
    # preserved because re-emitting them as ``cleanup_failure`` would
    # hide the upstream signal the caller actually needs to retry.
    # For ``ok`` and ``scope_violation``, ``cleanup_failure`` wins.
    failed_paths = list(cleanup_result.get("failed_paths") or [])
    _CLEANUP_FAILURE_PRESERVED = {"timeout", "backend_error", "schema_invalid"}
    if failed_paths and envelope["status"] not in _CLEANUP_FAILURE_PRESERVED:
        envelope = env_mod.build_cleanup_failure(
            failed_paths=failed_paths,
            scope=envelope.get("scope") or env_mod._empty_scope(),  # type: ignore[attr-defined]
            agent=envelope.get("agent"),
            model=envelope.get("model"),
            session_id=envelope.get("session_id"),
            duration_ms=envelope.get("duration_ms"),
            cost_usd=envelope.get("cost_usd"),
            tokens=envelope.get("tokens"),
            result=envelope.get("result"),
            result_raw_truncated=envelope.get("result_raw_truncated"),
            stderr_tail=envelope.get("stderr_tail"),
            permission_denials=envelope.get("permission_denials") or [],
            trace=envelope.get("trace") or trace,
        )
    elif envelope["status"] == "ok" and cleanup_result.get("scope_violation_detected"):
        envelope = env_mod.build_scope_violation(
            message=(
                "delta-bounded cleanup detected writes outside declared "
                f"files_changed: {cleanup_result.get('out_of_scope_paths') or []}"
            ),
            scope=envelope.get("scope") or env_mod._empty_scope(),  # type: ignore[attr-defined]
            agent=envelope.get("agent"),
            model=envelope.get("model"),
            session_id=envelope.get("session_id"),
            duration_ms=envelope.get("duration_ms"),
            cost_usd=envelope.get("cost_usd"),
            tokens=envelope.get("tokens"),
            result=envelope.get("result"),
            result_raw_truncated=envelope.get("result_raw_truncated"),
            stderr_tail=envelope.get("stderr_tail"),
            permission_denials=envelope.get("permission_denials") or [],
            trace=envelope.get("trace") or trace,
        )

    # Attach wrapper_events / blocked flag AFTER any potential
    # status-demotion rebuild above (build_scope_violation /
    # build_cleanup_failure construct fresh envelopes that drop
    # ``extra``; we re-stamp the events onto whichever envelope is
    # final). AC #4: under pause/fail-fast the policy_gated event must
    # ride along with the scope_violation envelope.
    if wrapper_events or _autoclean_blocked_flag:
        extra = envelope.setdefault("extra", {})
        if isinstance(extra, dict):
            if _autoclean_blocked_flag:
                extra["wrapper_autoclean_blocked"] = True
                extra["preserved_files"] = []
            if wrapper_events:
                extra["wrapper_events"] = wrapper_events

    # ---- Step 10: stamp trace.ended_at ----
    envelope = _stamp_trace_end(envelope)

    # ---- Step 11: schema-validate the final envelope ----
    try:
        output_schema = _load_schema(OUTPUT_SCHEMA_PATH)
    except (OSError, json.JSONDecodeError) as exc:
        # If the output schema itself cannot be loaded, fall back to a
        # backend_error envelope rather than emitting unvalidated bytes.
        # This is a wrapper-level failure but we still emit a §7-shaped
        # envelope on stdout.
        envelope = env_mod.build_backend_error(
            code="output_schema_unavailable",
            message=f"could not load output schema: {exc}",
            agent=envelope.get("agent"),
            model=envelope.get("model"),
            trace=envelope.get("trace") or trace,
        )
        envelope = _stamp_trace_end(envelope)
        _emit_envelope(envelope, output_arg)
        _append_span(envelope, repo_root=repo_root)
        return _exit_code_for_status(envelope["status"])

    out_err = _validate_against_schema(envelope, output_schema)
    if out_err is not None:
        envelope = env_mod.build_schema_invalid(
            message=f"wrapper envelope failed output schema: {out_err}",
            agent=envelope.get("agent"),
            model=envelope.get("model"),
            session_id=envelope.get("session_id"),
            duration_ms=envelope.get("duration_ms"),
            cost_usd=envelope.get("cost_usd"),
            tokens=envelope.get("tokens"),
            result_raw_truncated=envelope.get("result_raw_truncated"),
            stderr_tail=envelope.get("stderr_tail"),
            scope=envelope.get("scope"),
            trace=envelope.get("trace") or trace,
        )
        envelope = _stamp_trace_end(envelope)

    # ---- Step 12: span append ----
    _append_span(envelope, repo_root=repo_root)

    # ---- Step 13: emit ----
    _emit_envelope(envelope, output_arg)

    # ---- Step 14: exit code ----
    return _exit_code_for_status(envelope["status"])


# ---------------------------------------------------------------------------
# Auxiliary subcommands
# ---------------------------------------------------------------------------


def cmd_list_agents(args: argparse.Namespace) -> int:
    """Print the dispatchable-agent allowlist as a JSON array on stdout."""
    output_arg = getattr(args, "output", None) or "-"
    payload = sorted(agent_manifest.DISPATCHABLE_AGENTS)
    _write_output(output_arg, json.dumps(payload, indent=2))
    return EXIT_CODE_OK


def cmd_show_agent(args: argparse.Namespace) -> int:
    """Print the resolved manifest for ``args.name`` as JSON on stdout."""
    output_arg = getattr(args, "output", None) or "-"
    try:
        manifest = agent_manifest.load_agent(args.name)
    except agent_manifest.AgentNotDispatchable as exc:
        sys.stderr.write(f"plan_claude_dispatch: {exc}\n")
        return EXIT_CODE_WRAPPER_FAILURE
    except agent_manifest.ManifestSchemaInvalid as exc:
        sys.stderr.write(f"plan_claude_dispatch: {exc}\n")
        return EXIT_CODE_WRAPPER_FAILURE
    _write_output(output_arg, json.dumps(manifest, indent=2))
    return EXIT_CODE_OK


def cmd_validate_input(args: argparse.Namespace) -> int:
    """Schema-validate the JSON at ``args.path`` against the input schema."""
    try:
        raw = _read_input(args.path)
    except (OSError, PermissionError) as exc:
        sys.stderr.write(f"plan_claude_dispatch: cannot read {args.path}: {exc}\n")
        return EXIT_CODE_WRAPPER_FAILURE
    obj, parse_err = _parse_input(raw)
    if obj is None:
        sys.stderr.write(f"plan_claude_dispatch: {parse_err}\n")
        return EXIT_CODE_WRAPPER_FAILURE
    schema = _load_schema(INPUT_SCHEMA_PATH)
    err = _validate_against_schema(obj, schema)
    if err is not None:
        sys.stderr.write(f"plan_claude_dispatch: input invalid: {err}\n")
        return EXIT_CODE_NON_OK
    sys.stdout.write("ok\n")
    return EXIT_CODE_OK


def cmd_validate_output(args: argparse.Namespace) -> int:
    """Schema-validate the JSON at ``args.path`` against the output schema."""
    try:
        raw = _read_input(args.path)
    except (OSError, PermissionError) as exc:
        sys.stderr.write(f"plan_claude_dispatch: cannot read {args.path}: {exc}\n")
        return EXIT_CODE_WRAPPER_FAILURE
    obj, parse_err = _parse_input(raw)
    if obj is None:
        sys.stderr.write(f"plan_claude_dispatch: {parse_err}\n")
        return EXIT_CODE_WRAPPER_FAILURE
    schema = _load_schema(OUTPUT_SCHEMA_PATH)
    err = _validate_against_schema(obj, schema)
    if err is not None:
        sys.stderr.write(f"plan_claude_dispatch: output invalid: {err}\n")
        return EXIT_CODE_NON_OK
    sys.stdout.write("ok\n")
    return EXIT_CODE_OK


# ---------------------------------------------------------------------------
# argparse + main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan_claude_dispatch.py",
        description=(
            "Nested claude CLI dispatch wrapper for the dual-agent plan "
            "executor (PLAN_NESTED_DISPATCH §10)."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # ----- run -----
    run = subparsers.add_parser(
        "run",
        help="Dispatch a nested claude session (the main entry point).",
    )
    run.add_argument(
        "--input",
        required=True,
        help="Path to the input JSON, or '-' to read from stdin.",
    )
    run.add_argument(
        "--output",
        default="-",
        help="Path to write the envelope JSON to, or '-' for stdout (default).",
    )
    run.add_argument(
        "--timeout",
        type=int,
        default=None,
        help=(
            "Subprocess timeout in seconds (overrides input.overrides.timeout_sec "
            "when set). When neither this flag nor input.overrides.timeout_sec "
            "is supplied the wrapper applies DEFAULT_DISPATCH_TIMEOUT_SEC "
            "(1800s; BUG-145)."
        ),
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not spawn the backend; emit a plan-only envelope with "
            "status: ok and status_reason: 'dry_run: true'."
        ),
    )
    run.add_argument(
        "--verbose",
        action="store_true",
        help="Reserved (no-op in v1).",
    )
    run.add_argument(
        "--backend-binary",
        default=None,
        help=(
            "Override the 'claude' binary path. Test seam: integration "
            "tests pass a shim script here."
        ),
    )
    run.add_argument(
        "--repo-root",
        default=None,
        help=(
            "Repo-root path used for baseline snapshot, cleanup, and "
            "default span-log location. Defaults to the current cwd."
        ),
    )

    # ----- list-agents -----
    list_p = subparsers.add_parser(
        "list-agents",
        help="Print the dispatchable-agent allowlist as a JSON array.",
    )
    list_p.add_argument(
        "--output",
        default="-",
        help="Path to write to, or '-' for stdout (default).",
    )

    # ----- show-agent -----
    show_p = subparsers.add_parser(
        "show-agent",
        help="Print the resolved manifest for an agent as JSON.",
    )
    show_p.add_argument(
        "name",
        help="Agent name (must be in the dispatchable-agent allowlist).",
    )
    show_p.add_argument(
        "--output",
        default="-",
        help="Path to write to, or '-' for stdout (default).",
    )

    # ----- validate-input -----
    vi = subparsers.add_parser(
        "validate-input",
        help="Schema-validate a JSON input payload.",
    )
    vi.add_argument(
        "path",
        help="Path to the input JSON, or '-' for stdin.",
    )

    # ----- validate-output -----
    vo = subparsers.add_parser(
        "validate-output",
        help="Schema-validate a JSON envelope payload.",
    )
    vo.add_argument(
        "path",
        help="Path to the envelope JSON, or '-' for stdin.",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "run":
        return cmd_run(args)
    if args.subcommand == "list-agents":
        return cmd_list_agents(args)
    if args.subcommand == "show-agent":
        return cmd_show_agent(args)
    if args.subcommand == "validate-input":
        return cmd_validate_input(args)
    if args.subcommand == "validate-output":
        return cmd_validate_output(args)
    parser.error(f"unknown subcommand: {args.subcommand}")
    return EXIT_CODE_WRAPPER_FAILURE


if __name__ == "__main__":
    sys.exit(main())
