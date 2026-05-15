"""``claude`` CLI backend adapter for ``plan_claude_dispatch.py``
(PLAN_NESTED_DISPATCH §8.1).

Single public entry point :func:`invoke` shells out to the nested
``claude -p --output-format json`` session and folds the CLI's JSON
top-level ``result`` envelope into the wrapper-level envelope shape
defined in :mod:`_claude_dispatch_envelope`.

Argv shape (PLAN_NESTED_DISPATCH §8.1; cross-checked against the live
probe test in ``tests/scripts/test_claude_permission_mode_probe.py``)::

    claude -p \
        --agent plan-executor:<manifest.name> \
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

import atexit
import json
import os
import signal
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

#: Plugin namespace used by Claude Code's ``--agent`` flag for plugin-provided
#: subagents. Passing the bare manifest name can miss the plugin agent and
#: leave the nested session without the subagent's system prompt.
DEFAULT_AGENT_NAMESPACE = "plan-executor"

#: Grace period (seconds) between SIGTERM and SIGKILL when reaping the
#: nested ``claude`` process group on parent termination.
TERMINATE_GRACE_SEC = 5.0


def _killpg_safely(pgid: int, sig: int) -> None:
    """Best-effort ``os.killpg``; swallows ESRCH/permission errors.

    Used by the orphan-prevention path: when the wrapper is itself being
    torn down, we never want a secondary OSError to mask the original
    cause of termination.
    """
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _terminate_process_group(proc: "subprocess.Popen[Any]", grace_sec: float = TERMINATE_GRACE_SEC) -> None:
    """Reap the entire process group rooted at ``proc``.

    Sends SIGTERM to the child's process group, waits up to ``grace_sec``
    for it to exit, then escalates to SIGKILL. The child was spawned with
    ``start_new_session=True`` so its PID is also its PGID; killing the
    group catches any subagents the inner ``claude`` may have spawned.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        # Child already gone or unreachable — nothing to reap.
        return
    _killpg_safely(pgid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_sec)
        return
    except subprocess.TimeoutExpired:
        pass
    _killpg_safely(pgid, signal.SIGKILL)
    try:
        proc.wait(timeout=grace_sec)
    except subprocess.TimeoutExpired:
        # Genuinely stuck (e.g., uninterruptible kernel state). Caller
        # gets control back; the orchestrator will surface the timeout.
        pass


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


def _resolve_agent_cli_name(manifest: Mapping[str, Any]) -> str:
    """Return the subagent identifier passed to ``claude -p --agent``.

    Claude Code addresses plugin agents as ``<plugin-name>:<agent-name>``.
    The wrapper's public JSON contract intentionally uses bare agent names
    (``plan-implementer``) for allowlisting and schema routing, but the live
    CLI invocation must be namespaced or the nested process will not reliably
    load the plugin subagent system prompt.

    Tests and future direct callers may pass ``qualified_name`` explicitly; an
    already-qualified value is preserved.
    """
    raw = manifest.get("qualified_name") or manifest.get("name") or ""
    agent_name = _stringify(raw).strip()
    if not agent_name:
        return agent_name
    if ":" in agent_name:
        return agent_name
    return f"{DEFAULT_AGENT_NAMESPACE}:{agent_name}"


def _resolve_timeout_sec(effective: Mapping[str, Any]) -> int:
    if isinstance(effective, Mapping):
        raw = effective.get("timeout_sec")
        if isinstance(raw, bool):
            raw = None
        if isinstance(raw, (int, float)) and raw > 0:
            return int(raw)
    return DEFAULT_TIMEOUT_SEC


def _render_phase_a_single_classifier_body(
    plan_path: str, repo_root: str, payload: Mapping[str, Any]
) -> str:
    """Render the Phase A-single ``plan-analyst`` classifier body.

    Mirrors the verbatim body below the ``<!-- TRANSPORT BOUNDARY -->``
    marker in ``plugins/plan-executor/skills/implement-plan/dispatch-templates.md``
    (`## Phase A-single` section). The agent spec on its own does NOT
    pin the per-child contract — the legacy whole-plan contract is also
    documented there for direct CLI callers — so without this framing
    the nested plan-analyst can latch onto the wrong shape and either
    time out (legacy whole-plan rationalization) or implement the task
    (Opus-shaped over-reach), per BUG-144.
    """
    lines = [
        f"Classify exactly one task from the plan at `{plan_path}`. "
        f"Repo root: `{repo_root}`.",
        "",
        (
            "Read the child plan file verbatim (it typically carries a "
            "single `### TASK-NNN:` H3 heading plus the standard "
            "metadata block — Status, Implementer, Priority, Files, Dependencies, "
            "Test command, Acceptance criteria, Description, "
            "Implementation notes, Reversion guidance — but **may "
            "carry >1 H3 heading** when several sibling sub-tasks "
            "share a single child file; in that case the orchestrator "
            "passes `target_task_id` as a first-class dispatch field "
            "and the renderer auto-injects an \"Implement specifically "
            "`### TASK-NNN:`\" first-instruction line). If the selected "
            "task's metadata block declares `Implementer: claude` or "
            "`Implementer: codex`, emit that exact value as `agent` and "
            "do not reclassify it. Otherwise: Use the `claude` vs "
            "`codex` heuristics from your agent spec's classification "
            "rubric (default is `codex`; route to `claude` only when "
            "AC is underspecified, implementation notes describe a "
            "tradeoff to weigh, `(create)` covers a non-leaf module "
            "with un-enumerated call sites, the task mutates a "
            "cross-cutting state-machine contract — `plan_ops` / "
            "dispatch-templates / schedule shape — or priority is "
            "`critical` AND one of the above also holds; scope, file "
            "count, line count, and `Test command: none` are NOT "
            "discriminators on their own). Do NOT emit a schedule, "
            "gaps, risks, or a batch table."
        ),
        "",
        (
            "You are classifying ONE task — return only the minimal "
            "JSON below. Do not re-validate structure, do not compute "
            "batches, do not surface cross-task gaps "
            "(`compute-schedule` handles DAG + file-disjointness "
            "downstream)."
        ),
        "",
        "**Output (required fenced `json` block, no prose outside it):**",
        "",
        "```json",
        "{",
        '  "agent": "claude" | "codex",',
        '  "classification_reason": "<one-line justification, ≤10 words>"',
        "}",
        "```",
        "",
        (
            "Emit nothing else — no markdown report, no `tasks[]`, no "
            "`batches[]`, no `gaps[]`. Any additional fields or prose "
            "outside the fenced JSON block will be rejected by the "
            "orchestrator as a malformed classifier reply."
        ),
        "",
        (
            "**You do NOT have the Agent tool.** Do all work directly "
            "with Read, Grep, Glob, Bash."
        ),
    ]
    target_task_id = payload.get("target_task_id") if isinstance(payload, Mapping) else None
    if isinstance(target_task_id, str) and target_task_id:
        lines.insert(
            0,
            (
                f"Classify specifically `### TASK-{target_task_id}:` "
                f"from the child plan file (the file declares >1 H3 "
                f"`### TASK-NNN:` heading; the `target_task_id` "
                f"dispatch field disambiguates which one to score)."
            ),
        )
        lines.insert(1, "")
    return "\n".join(lines)


def _resolve_prompt(
    payload: Mapping[str, Any],
    *,
    agent: Optional[str] = None,
) -> str:
    """Pull the prompt text out of ``payload``.

    Conventions (best-effort, in order):

      * ``payload["prompt"]`` — explicit string from the caller (the
        canonical wrapper-input builder ``plan_ops.py
        build-claude-dispatch-input`` populates this for non-analyst
        variants since BUG-146 — it inlines the result schema and the
        canonical implementer/remediator framing directly so the agent
        always sees the canonical ``{outcome, files_changed, report}``
        shape);
      * ``payload["instructions"]`` — fall-back wording used by the
        plan-executor's existing fixtures;
      * ``agent == "plan-analyst"`` with a ``plan_path`` payload — render
        the Phase A-single classifier body verbatim; if ``repo_root`` is
        missing, use the current working directory as a defensive fallback
        from ``dispatch-templates.md`` (BUG-144). Without this frame
        the nested plan-analyst latches onto the documented legacy
        whole-plan contract or implements the task outright;
      * structured plan-implementer dispatch shape (``task_id`` +
        ``plan_path``) — render a directive prose mirroring the Phase B
        template body. Largely superseded by the BUG-146 explicit
        ``payload["prompt"]`` injection, retained as a defense in depth
        for direct CLI callers who hand-build a payload;
      * else the JSON dump of ``payload`` (the nested session can still
        parse the structured form).

    Parameters
    ----------
    payload
        The dispatch envelope's inner payload.
    agent
        The dispatch envelope's top-level ``agent`` field. Threaded so
        the analyst-body branch can fire before the implementer fallback.
    """
    if not isinstance(payload, Mapping):
        return json.dumps(payload, default=str)
    for key in ("prompt", "instructions"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    plan_path = payload.get("plan_path")
    repo_root = payload.get("repo_root")
    # BUG-144 / BUG-147: Phase A-single plan-analyst classifier body. Fires when
    # the dispatch envelope's top-level ``agent`` is ``plan-analyst``
    # and the payload carries a plan_path. Without this frame the
    # nested plan-analyst received a raw JSON dump of the payload,
    # latched onto the legacy whole-plan contract documented in its
    # own agent spec, and either timed out at 300s or implemented the
    # task outright (TASK-006 of the 20260426 dry-run wrote 13 files
    # before the wrapper's delta-bounded cleanup caught it). The analyst
    # branch must also win when task_id is present but repo_root is missing;
    # otherwise malformed hand-built classifier payloads fall through to the
    # implementer prompt below.
    if (
        agent == "plan-analyst"
        and isinstance(plan_path, str)
        and plan_path
    ):
        repo_root_for_prompt = (
            repo_root if isinstance(repo_root, str) and repo_root else os.getcwd()
        )
        return _render_phase_a_single_classifier_body(
            plan_path, repo_root_for_prompt, payload
        )
    # Structured-payload fallback for plan-implementer dispatches.
    task_id = payload.get("task_id")
    if (
        isinstance(task_id, str)
        and task_id
        and isinstance(plan_path, str)
        and plan_path
    ):
        lines = [
            f"Implement TASK-{task_id} from the plan at `{plan_path}` per your plan-implementer agent specification.",
            "",
            (
                "Read the plan to find the `## Context` section and the verbatim "
                "`### TASK-NNN: <title>` block (with Status / Priority / Files / "
                "Test command / Acceptance criteria / Description / Reversion "
                "guidance). Apply the minimum change satisfying the AC, run the "
                "test command, and return your structured JSON report. Do not "
                "commit, do not modify the plan file, do not use `git stash`."
            ),
            "",
            "Structured dispatch payload (verbatim, for reference):",
            "```json",
            json.dumps(payload, indent=2, default=str),
            "```",
        ]
        return "\n".join(lines)
    return json.dumps(payload, default=str)


def _resolve_model(
    effective: Mapping[str, Any], manifest: Mapping[str, Any]
) -> Optional[str]:
    """Return the model alias to thread into ``--model``.

    BUG-144: ``_build_argv`` previously omitted ``--model`` entirely,
    so the nested ``claude -p`` session inherited the parent CLI's
    model regardless of what the dispatch envelope or the agent
    manifest declared. Captured envelope from the 20260426 dry-run:
    ``payload.overrides.model: "sonnet"`` and ``manifest.model:
    "sonnet"`` both ignored — the nested session ran on
    ``claude-opus-4-7[1m]``, doubling cost and broadening the
    instruction-overriding surface.

    Resolution order (first non-empty wins; ``effective`` mirrors the
    CLI's payload-level overrides which always wins over manifest):

      * ``effective["model"]`` (from ``payload.overrides.model``);
      * ``manifest["model"]`` (from the agent frontmatter).

    Returns ``None`` when neither is set, in which case the caller
    omits ``--model`` and the nested CLI inherits the parent's model
    selection (today's status-quo).
    """
    if isinstance(effective, Mapping):
        v = effective.get("model")
        if isinstance(v, str) and v.strip():
            return v.strip()
    if isinstance(manifest, Mapping):
        v = manifest.get("model")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _build_argv(
    *,
    manifest: Mapping[str, Any],
    effective: Mapping[str, Any],
    prompt: str,
    backend_binary: str,
) -> List[str]:
    """Assemble the argv per PLAN_NESTED_DISPATCH §8.1."""
    agent_name = _resolve_agent_cli_name(manifest)
    cwd = _resolve_cwd(effective)
    permission_mode = _resolve_permission_mode(effective)
    allowed_csv = _allowed_tools_csv(manifest)
    disallowed_csv = _disallowed_tools_csv(effective)
    model = _resolve_model(effective, manifest)

    argv: List[str] = [
        backend_binary,
        "-p",
        "--agent", agent_name,
        "--permission-mode", permission_mode,
        "--allowedTools", allowed_csv,
        "--disallowedTools", disallowed_csv,
        "--add-dir", cwd,
        "--output-format", "json",
    ]
    if model:
        argv.extend(["--model", model])
    argv.append(prompt)
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
    prompt_text = _resolve_prompt(
        payload,
        agent=str(agent_name) if isinstance(agent_name, str) else None,
    )

    argv = _build_argv(
        manifest=manifest,
        effective=effective,
        prompt=prompt_text,
        backend_binary=binary,
    )

    start = time.monotonic()
    proc: Optional[subprocess.Popen[str]] = None
    prior_sigterm = None
    prior_sigint = None
    handlers_installed = False
    atexit_registered = False
    returncode: int = 0
    stdout_text: str = ""
    stderr_text: str = ""

    def _atexit_cleanup() -> None:
        # Defense in depth: if the wrapper exits via uncaught exception
        # or normal return without explicit cleanup, still reap the
        # child process group so the nested ``claude`` does not become
        # an orphan with full repo write access.
        local_proc = proc
        if local_proc is not None and local_proc.poll() is None:
            _terminate_process_group(local_proc)

    def _signal_handler(signum, frame):  # noqa: ANN001 — signal API
        # Tear down the nested ``claude`` and its subprocess tree, then
        # dispatch to whatever handler the orchestrator had installed
        # before we took over for the duration of ``invoke``.
        local_proc = proc
        if local_proc is not None:
            _terminate_process_group(local_proc)
        # Restore previously installed handlers before dispatching so we
        # don't recurse, and so the prior handler is the one in effect
        # when it (or the default disposition) takes over.
        prior = prior_sigint if signum == signal.SIGINT else prior_sigterm
        try:
            if prior_sigterm is not None:
                signal.signal(signal.SIGTERM, prior_sigterm)
            if prior_sigint is not None:
                signal.signal(signal.SIGINT, prior_sigint)
        except (ValueError, OSError):
            pass
        # Dispatch to the prior handler:
        #   - callable (custom orchestrator handler) → invoke it directly
        #     so the orchestrator's handling fires;
        #   - SIG_IGN → preserve ignore semantics (do nothing);
        #   - SIG_DFL or default Python behavior → emulate the default
        #     disposition (KeyboardInterrupt for SIGINT, self-signal with
        #     SIG_DFL for SIGTERM).
        if callable(prior) and prior not in (signal.SIG_DFL, signal.SIG_IGN):
            # ``signal.default_int_handler`` is callable and is Python's
            # default for SIGINT — fall through to the KeyboardInterrupt
            # branch below so behavior matches an unhandled SIGINT.
            if not (signum == signal.SIGINT and prior is signal.default_int_handler):
                prior(signum, frame)
                return
        if prior is signal.SIG_IGN:
            return
        if signum == signal.SIGINT:
            raise KeyboardInterrupt()
        # SIGTERM (or anything else routed here) with default disposition:
        # re-raise via SIG_DFL.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)

    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=dict(env) if env is not None else None,
                start_new_session=True,
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

        # Install signal handlers only for the duration of this call.
        # ``signal.signal`` raises ValueError off the main thread; in that
        # case we silently skip (the atexit hook still provides defense
        # in depth). Capture prior handlers so we can restore them.
        try:
            prior_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
            prior_sigint = signal.signal(signal.SIGINT, _signal_handler)
            handlers_installed = True
        except (ValueError, OSError):
            handlers_installed = False

        atexit.register(_atexit_cleanup)
        atexit_registered = True

        try:
            stdout_text, stderr_text = proc.communicate(timeout=timeout_sec)
            returncode = proc.returncode
        except subprocess.TimeoutExpired as exc:
            # Reap the entire group (not just the immediate child) so any
            # subagents the inner ``claude`` spawned are torn down too.
            _terminate_process_group(proc)
            # Drain whatever the child managed to emit before being killed.
            try:
                stdout_remainder, stderr_remainder = proc.communicate(timeout=1.0)
            except Exception:
                stdout_remainder, stderr_remainder = "", ""
            elapsed_ms = int((time.monotonic() - start) * 1000)
            stderr_text_local = ""
            if exc.stderr is not None:
                try:
                    stderr_text_local = (
                        exc.stderr.decode("utf-8", errors="replace")
                        if isinstance(exc.stderr, (bytes, bytearray))
                        else str(exc.stderr)
                    )
                except Exception:
                    stderr_text_local = ""
            if not stderr_text_local and stderr_remainder:
                stderr_text_local = (
                    stderr_remainder
                    if isinstance(stderr_remainder, str)
                    else stderr_remainder.decode("utf-8", errors="replace")
                )
            return env_mod.build_timeout(
                duration_ms=elapsed_ms,
                agent=agent_name,
                model=model_name,
                stderr_tail=_stderr_tail(stderr_text_local),
                trace=trace,
            )
    finally:
        # Restore prior signal handlers so the orchestrator's handling
        # is not clobbered globally.
        if handlers_installed:
            try:
                if prior_sigterm is not None:
                    signal.signal(signal.SIGTERM, prior_sigterm)
                else:
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                if prior_sigint is not None:
                    signal.signal(signal.SIGINT, prior_sigint)
                else:
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
            except (ValueError, OSError):
                pass
        # Defense in depth: if we somehow exit this block with the child
        # still running (uncaught exception path), reap the group now.
        if proc is not None and proc.poll() is None:
            _terminate_process_group(proc)
        if atexit_registered:
            try:
                atexit.unregister(_atexit_cleanup)
            except Exception:
                pass

    elapsed_ms = int((time.monotonic() - start) * 1000)
    stdout_text = stdout_text if isinstance(stdout_text, str) else ""
    stderr_text = stderr_text if isinstance(stderr_text, str) else ""
    raw_truncated = _truncate(stdout_text, RAW_TRUNCATE_CHARS)
    stderr_tail = _stderr_tail(stderr_text)

    # Try to parse stdout as JSON. Non-JSON → backend_error / malformed_output.
    stripped = stdout_text.strip()
    if not stripped:
        return env_mod.build_backend_error(
            code="malformed_output",
            message=(
                f"backend produced empty stdout (rc={returncode})"
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

    if returncode != 0:
        # Non-zero exit but parseable JSON: surface as backend_error.
        return env_mod.build_backend_error(
            code="non_zero_exit",
            message=(
                f"backend exited with rc={returncode}"
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
    "TERMINATE_GRACE_SEC",
    "invoke",
]
