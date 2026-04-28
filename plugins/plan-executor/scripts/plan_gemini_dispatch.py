#!/usr/bin/env python3
"""Gemini CLI dispatch wrapper for the dual-agent plan executor.

Mirror of plan_codex_dispatch.py for the Gemini fallback path. Three
subcommands: implement (stub for TASK-007), review, and plan-review.
The argspec, envelope shape, and wrapper-side scope/cleanup semantics
mirror the Codex wrapper, with two non-trivial differences:

(1) Headless OAuth contract — refuse to run when neither
    ``GEMINI_API_KEY`` nor ``GOOGLE_APPLICATION_CREDENTIALS`` is set
    in the environment, BEFORE subprocess spawn. This blocks the
    Gemini binary from attempting an interactive browser launch.

(2) Schema-validation retry loop — Gemini has no ``--output-schema``
    flag, so the wrapper embeds the JSON Schema in the prompt and
    validates the parsed ``response`` field post-hoc with
    ``jsonschema``. Up to three total attempts; outcome=parse_error
    on exhaustion. The retry suffix tells Gemini "the previous
    attempt's JSON did not validate; here is the schema again, try
    again." The previous response is intentionally NOT echoed back
    to Gemini (avoids self-reinforcement of malformed output).

(3) Restrictive policy + isolation — ephemeral ``GEMINI_CLI_HOME`` per
    invocation under ``tempfile.mkdtemp(prefix="gemini_dispatch_")``,
    with ``<home>/.gemini/policies/restrictive.toml`` denying
    ``run_shell_command`` / ``edit_file`` / ``write_file`` /
    ``replace`` / ``glob`` / ``shell`` at priority 999. Torn down in
    ``finally``. Two concurrent invocations get distinct homes.

Usage:
    $PYTHON scripts/plan_gemini_dispatch.py review \
        --plan-file PATH --task-id N --repo-root PATH \
        --files f1,f2 [--review-focus bugs] [--dry-run] [--timeout SECS]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import jsonschema

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REVIEW_SCHEMA = SCRIPT_DIR / "gemini_review_schema.json"
PLAN_REVIEW_SCHEMA = SCRIPT_DIR / "gemini_plan_review_schema.json"

# Ensure the sibling ``_plan_paths`` module is importable when this file is
# loaded via ``importlib.util.spec_from_file_location`` (e.g., from tests).
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _plan_paths import (  # noqa: E402
    ALLOW_GAPS_DEMOTION_CLAUSE,
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
    _should_inject_allow_gaps_demotion,
    is_protected_path,
)

import plan_ops  # noqa: E402

# Re-use the Codex wrapper's plan-parsing helpers byte-for-byte. This is
# the explicit choice the plan calls for: import the helper directly so
# the two wrappers cannot diverge in how they slice up TASK blocks.
from plan_codex_dispatch import (  # noqa: E402
    normalize_task_id,
    parse_plan_context,
    parse_task_block,
    normalize_file_path,
    git_diff_for_files,
    git_changed_files,
    _snapshot_baseline,
    validate_scope,
    _handle_timeout_cleanup,
)

# Backward-compatibility alias mirroring plan_codex_dispatch.
_is_protected = is_protected_path

RAW_TRUNCATE_CHARS = 2000
DEFAULT_TIMEOUT_REVIEW = 180
DEFAULT_TIMEOUT_PLAN_REVIEW = 180
SCHEMA_RETRY_MAX_ATTEMPTS = 3

# Restrictive policy TOML written to <GEMINI_CLI_HOME>/.gemini/policies/
# restrictive.toml on every invocation. Denies tool execution at priority
# 999. A future broadening is one edit at one location -- callers must
# never inline-edit this string.
RESTRICTIVE_POLICY_TOML = """\
# Plan-executor restrictive policy. Generated per-invocation by
# plan_gemini_dispatch.py. Denies Gemini tool execution so the wrapper
# can rely on prompt-only output. Priority 999 ensures this overrides
# any default permissive policy.

[[rules]]
priority = 999
tool = "run_shell_command"
decision = "deny"

[[rules]]
priority = 999
tool = "edit_file"
decision = "deny"

[[rules]]
priority = 999
tool = "write_file"
decision = "deny"

[[rules]]
priority = 999
tool = "replace"
decision = "deny"

[[rules]]
priority = 999
tool = "glob"
decision = "deny"

[[rules]]
priority = 999
tool = "shell"
decision = "deny"
"""


# ---------------------------------------------------------------------------
# Headless OAuth contract — pre-spawn API-key short-circuit
# ---------------------------------------------------------------------------


def _check_api_key_env() -> str | None:
    """Return None when at least one of GEMINI_API_KEY /
    GOOGLE_APPLICATION_CREDENTIALS is set in the environment with a
    non-empty value, else return the canonical error string used in
    the envelope's ``error`` field. Called BEFORE any subprocess spawn
    or ``tempfile.mkdtemp`` so a misconfigured invocation does not
    litter ``/tmp``."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not api_key and not creds:
        return "missing GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS"
    return None


# ---------------------------------------------------------------------------
# GEMINI_CLI_HOME isolation
# ---------------------------------------------------------------------------


def _make_ephemeral_gemini_home() -> str:
    """Create a per-invocation GEMINI_CLI_HOME under tempfile.mkdtemp.

    Writes the restrictive policy TOML at
    ``<home>/.gemini/policies/restrictive.toml`` so the launched
    Gemini process refuses tool execution. Returns the absolute path
    of the newly created home directory; caller MUST tear it down in
    ``finally``.
    """
    home = tempfile.mkdtemp(prefix="gemini_dispatch_")
    policies_dir = Path(home) / ".gemini" / "policies"
    policies_dir.mkdir(parents=True, exist_ok=True)
    (policies_dir / "restrictive.toml").write_text(
        RESTRICTIVE_POLICY_TOML, encoding="utf-8",
    )
    return home


def _teardown_gemini_home(home: str) -> None:
    """Remove the ephemeral GEMINI_CLI_HOME tree. Idempotent — missing
    or partially deleted trees are silently swallowed because the
    invocation was already on its way out."""
    try:
        shutil.rmtree(home, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _load_review_schema() -> dict:
    """Load and parse the review JSON Schema. Cached per-process via the
    module-level constant once read; the wrapper runs once per dispatch
    so a per-call read is fine and keeps the test surface obvious."""
    return json.loads(REVIEW_SCHEMA.read_text(encoding="utf-8"))


def _load_plan_review_schema() -> dict:
    """Load and parse the plan-review JSON Schema. Same per-call-read
    rationale as ``_load_review_schema``."""
    return json.loads(PLAN_REVIEW_SCHEMA.read_text(encoding="utf-8"))


def render_plan_review_prompt(
    schedule_json: str,
    plan_basename: str | None = None,
    *,
    schema: dict | None = None,
    allow_gaps_demotion: bool = False,
    retry_suffix: str = "",
) -> str:
    """Build the Gemini plan-review prompt.

    Mirror of ``plan_codex_dispatch.render_plan_review_prompt`` with the
    schema embedded in the prompt body (Gemini has no
    ``--output-schema`` equivalent). Schedule-only — the plan markdown
    is never rendered into the prompt.

    The ``--allow-gaps`` demotion clause text is sourced from the
    shared ``ALLOW_GAPS_DEMOTION_CLAUSE`` constant in ``_plan_paths.py``
    so the Codex and Gemini reviewers receive byte-identical prose.

    When ``retry_suffix`` is non-empty it is appended to the bottom of
    the prompt; the suffix is the bounded retry escalation and
    intentionally does NOT include the previous attempt's response
    (avoids self-reinforcement of malformed output).
    """
    plan_label = (
        plan_basename if plan_basename else "(schedule-only; no plan file)"
    )
    demotion_clause = ALLOW_GAPS_DEMOTION_CLAUSE if allow_gaps_demotion else ""

    schema_obj = schema if schema is not None else _load_plan_review_schema()
    schema_str = json.dumps(schema_obj, indent=2)

    body = (
        f"Review the persisted schedule for this plan. The plan was authored "
        f"by a peer analyst and decomposed into a fat manifest by "
        f"`plan_ops.py build-tasks`; you are an independent pre-dispatch "
        f"reviewer working from the schedule JSON alone.\n\n"
        f"Plan file: {plan_label}\n\n"
        f"Cross-plan dependency resolution has already been verified by the "
        f"orchestrator in Phase 0 preflight. Do not check or report on "
        f"cross-plan dependencies. Focus only on schedule structure, task "
        f"intent, and coordination risk expressed within the supplied "
        f"schedule.\n\n"
        f"Your job is to determine whether this plan is workable to execute, "
        f"not whether it is perfect.\n\n"
        f"Review standard:\n"
        f"1. Report only concrete, text-supported issues visible in the "
        f"supplied schedule (including per-task `description` and "
        f"`acceptance_criteria`).\n"
        f"2. Before recording a finding, inspect the specific alleged gap, "
        f"contradiction, or risk in the schedule. Do not render an "
        f"uninformed verdict.\n"
        f"3. A finding is blocking only if it would likely cause execution "
        f"failure, invalid scheduling, ambiguous ownership, unbounded scope, "
        f"or acceptance criteria that cannot be executed or evaluated.\n"
        f"4. Minor omissions, polish improvements, or low-confidence concerns "
        f"are not blocking. Those belong in `approved-with-notes` at most.\n"
        f"5. If an issue is not explicit in the persisted schedule, do not "
        f"infer it into a blocking finding.\n\n"
        f"Output discipline:\n"
        f"- Put concrete execution-impact issues in `findings`.\n"
        f"- Put low-signal concerns, small polish suggestions, and "
        f"non-blocking observations in `notes` instead of `findings`.\n"
        f"- Each finding must include `blocking: true` only for issues that "
        f"justify `needs-replan`; otherwise use `blocking: false`.\n"
        f"- Section references in findings should use `tasks[i]` paths "
        f"(e.g. `tasks[002].test_command`, `tasks[000].description`, "
        f"`batches[1]`) rather than plan-markdown line numbers. The "
        f"schedule JSON is the single source of truth.\n"
        f"- Each finding must include `target_task_id: string | null` — the "
        f"task id this finding is about (e.g., \"002\" when the finding "
        f"concerns `tasks[002]`), or `null` for schedule-level findings "
        f"(batch ordering, roster completeness, cross-cutting issues with no "
        f"single task owner). The downstream triage + plan-author "
        f"dispatchers route per-child based on this field, so accuracy "
        f"matters: if the concern lives inside one task, name that task; "
        f"otherwise use `null`.\n\n"
        f"Check specifically:\n"
        f"1. DAG shape: does every `tasks[i].dependencies` entry resolve to "
        f"another task id in the schedule? Are there cycles? Is the "
        f"`batches[]` order a valid topological sort of the DAG?\n"
        f"2. File disjointness within a batch: do any two tasks scheduled in "
        f"the same batch share a path in their `files[]` lists? Concurrent "
        f"writers must be disjoint.\n"
        f"3. Classification sanity: does every task carry an `agent` field "
        f"matching the task's nature (Codex for large mechanical edits, "
        f"Claude for schema/prose/judgment work)? Flag obvious misfits, but "
        f"only when the mismatch is visible from the description + files + "
        f"test_command.\n"
        f"4. Test-command reachability: does `tasks[i].test_command` point "
        f"at a runnable invocation or an accepted deferred-testing signal? "
        f"Accepted deferred-testing signals — do NOT flag these:\n"
        f"   - Canonical: `deferred (TASK-NNN[A-Z]?)` with optional trailing "
        f"note, OR\n"
        f"   - Back-compat: `none` with a parenthetical that references a "
        f"sibling task in this schedule, e.g. `none (pure agent spec; "
        f"end-to-end exercise lands in TASK-NNN[A-Z]?)`.\n"
        f"   The referenced `TASK-NNN[A-Z]?` must resolve to a task declared "
        f"in this schedule. Treat these as deferred-testing notes, not "
        f"blocking gaps. Flag only bare `none` with no valid sibling-task "
        f"deferral.\n"
        f"5. AC-vs-files alignment: for each task, are the listed `files[]` "
        f"plausibly sufficient to satisfy `acceptance_criteria[]`? Flag "
        f"obvious mismatches (AC references a file absent from `files[]`; "
        f"AC describes behaviour the `files[]` list cannot plausibly reach).\n"
        f"6. Intent completeness: is `tasks[i].description` non-empty and "
        f"non-trivial? Is `tasks[i].acceptance_criteria[]` non-empty? A task "
        f"missing either field is a likely-blocking gap (implementer cannot "
        f"work without knowing what to build or how to know they are "
        f"done).\n\n"
        f"Output schema (JSON):\n"
        f"```json\n{schema_str}\n```\n\n"
        f"Persisted schedule JSON:\n\n"
        f"```json\n{schedule_json}\n```\n\n"
        f"{demotion_clause}"
        f"Verdict vocabulary (pick exactly one):\n"
        f"- `approved` — the schedule is workable as written and no "
        f"substantiated blocking issue is present.\n"
        f"- `approved-with-notes` — the schedule is workable but has "
        f"non-blocking issues, minor gaps, or operator-accepted soft gaps.\n"
        f"- `needs-replan` — the schedule has a concrete blocking defect "
        f"that should be fixed before dispatch.\n"
        f"Do not use `needs-replan` for nits, preferences, or weak "
        f"inferences.\n"
        f"If there are no non-blocking observations, return `notes: []`.\n\n"
        f"Return JSON conforming to the schema above, no markdown fences, "
        f"no trailing commentary. `plan_file` must be \"{plan_label}\".\n"
    )
    if retry_suffix:
        body = body + "\n" + retry_suffix.rstrip() + "\n"
    return body


def render_review_prompt(
    task: dict,
    diff: str,
    review_focus: str,
    review_files: list[str] | None = None,
    *,
    schema: dict | None = None,
    retry_suffix: str = "",
) -> str:
    """Build the Gemini review prompt.

    Mirror of ``plan_codex_dispatch.render_review_prompt`` with the
    schema embedded in the prompt body (Gemini has no
    ``--output-schema`` equivalent). When ``retry_suffix`` is
    non-empty, it is appended to the bottom of the prompt; the suffix
    is the bounded retry escalation and intentionally does NOT include
    the previous attempt's response (avoids self-reinforcement of
    malformed output)."""
    allowed = [normalize_file_path(f) for f in task["files"]]
    prompt_files = review_files if review_files is not None else allowed
    files_str = ", ".join(prompt_files) or "(none declared)"
    ac_bullets = "\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none specified)"
    description = task.get("description") or "(none provided)"
    impl_notes_text = task.get("implementation_notes") or "(none provided)"

    raw_block = task.get("raw_block", "") or ""
    pre_read_block = ""
    if raw_block:
        resolved = plan_ops.resolve_read_targets(raw_block)
        pre_read_block = plan_ops.render_pre_read_excerpts(resolved)
    pre_read_prefix = f"{pre_read_block}\n" if pre_read_block else ""

    schema_obj = schema if schema is not None else _load_review_schema()
    schema_str = json.dumps(schema_obj, indent=2)

    body = (
        f"{pre_read_prefix}"
        f"Review the implementation of TASK-{task['task_id']} in this repository.\n\n"
        f"Task objective: {task['title']}\n\n"
        f"Task requirements:\n{ac_bullets}\n\n"
        f"Description:\n{description}\n\n"
        f"Implementation notes:\n{impl_notes_text}\n\n"
        f"Changed files: {files_str}\n\n"
        f"Review focus: {review_focus}\n\n"
        f"Your job is to identify only concrete, material problems in the "
        f"provided diff.\n\n"
        f"Review standard:\n"
        f"1. Report only issues that are directly supported by the provided "
        f"diff.\n"
        f"2. A finding must be tied to a specific file and line and must "
        f"plausibly cause acceptance-criteria failure, incorrect behavior, "
        f"regression, meaningful scope violation, or missing coverage for a "
        f"clearly introduced new branch, contract, or failure mode.\n"
        f"3. Do not report stylistic preferences, speculative edge cases, "
        f"hypothetical refactors, or weak 'might want to' suggestions as "
        f"findings.\n"
        f"4. If a concern is plausible but not clearly demonstrated by the "
        f"diff, do not escalate it into a finding or let it affect the "
        f"verdict.\n"
        f"5. Before rendering a verdict, review the specific alleged issue "
        f"itself. Do not render an uninformed verdict.\n\n"
        f"Output discipline:\n"
        f"1. Put substantiated code issues in `findings`.\n"
        f"2. Put minor suggestions, nits, and low-confidence observations in "
        f"`notes` instead of `findings`.\n"
        f"3. Every finding must include a `confidence` value. Use `high` for "
        f"directly evidenced issues, `medium` for strong but slightly "
        f"indirect evidence, and `low` only for non-blocking items that still "
        f"belong in findings. If confidence is too low to support a finding, "
        f"use `notes` instead.\n\n"
        f"Output schema (JSON):\n"
        f"```json\n{schema_str}\n```\n\n"
        f"Here is the diff for the changed files:\n\n"
        f"```diff\n{diff}\n```\n\n"
        f"Return JSON conforming to the schema above. task_id must be "
        f"\"{task['task_id']}\".\n"
    )
    if retry_suffix:
        body = body + "\n" + retry_suffix.rstrip() + "\n"
    return body


def _retry_suffix_for_attempt(attempt: int, schema: dict) -> str:
    """Bounded escalation suffix appended to the prompt on retry. The
    previous attempt's response is intentionally NOT included to avoid
    self-reinforcement of malformed output."""
    schema_str = json.dumps(schema, indent=2)
    return (
        f"NOTE: the previous attempt did not produce JSON validating "
        f"against the required schema. This is attempt {attempt} of "
        f"{SCHEMA_RETRY_MAX_ATTEMPTS}. Return ONLY a JSON object that "
        f"validates against the schema below. No prose, no markdown "
        f"fences.\n\n"
        f"```json\n{schema_str}\n```\n"
    )


# ---------------------------------------------------------------------------
# Gemini invocation
# ---------------------------------------------------------------------------


def invoke_gemini(
    prompt: str,
    workdir: str,
    timeout_sec: int,
    gemini_home: str,
) -> dict:
    """Invoke the Gemini CLI with the prompt on stdin.

    Returns dict:
        status: 'ok' | 'timeout' | 'gemini_not_found'
        exit_code: int (-1 for timeout/not_found)
        stdout: str
        stderr: str
        wall_seconds: float
    """
    cmd = [
        "gemini",
        "-o", "json",
        "--approval-mode", "plan",
        "--policy-file", str(
            Path(gemini_home) / ".gemini" / "policies" / "restrictive.toml"
        ),
    ]
    env = os.environ.copy()
    env["GEMINI_CLI_HOME"] = gemini_home

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout_sec,
            cwd=workdir,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        elapsed = time.monotonic() - start
        raw_out = (e.stdout or b"").decode(errors="replace") if e.stdout else ""
        raw_err = (e.stderr or b"").decode(errors="replace") if e.stderr else ""
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": raw_out,
            "stderr": raw_err,
            "wall_seconds": elapsed,
        }
    except FileNotFoundError:
        return {
            "status": "gemini_not_found",
            "exit_code": -1,
            "stdout": "",
            "stderr": "gemini binary not found on PATH",
            "wall_seconds": 0.0,
        }

    elapsed = time.monotonic() - start
    return {
        "status": "ok",
        "exit_code": proc.returncode,
        "stdout": proc.stdout.decode(errors="replace"),
        "stderr": proc.stderr.decode(errors="replace"),
        "wall_seconds": elapsed,
    }


def _extract_response_json(stdout: str) -> tuple[dict | None, str | None]:
    """Pull the inner review JSON out of Gemini's ``-o json`` envelope.

    Gemini emits ``{"response": "<inner-json-as-string>", "stats": {...}}``
    on stdout. We FIRST parse stdout as the outer envelope, THEN read the
    ``response`` field (a JSON-encoded string), strip optional markdown
    fences from that string, and parse the result as the inner review
    JSON. The schema validation upstream then runs against the inner
    object only.

    Returns ``(parsed_dict, None)`` on success or
    ``(None, error_string)`` on failure (envelope parse error,
    missing ``response`` field, or inner JSON parse error).
    """
    text = stdout.strip()
    if not text:
        return None, "empty stdout"

    # Phase 1: parse the outer Gemini envelope.
    try:
        envelope = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"envelope parse error: {exc}"
    if not isinstance(envelope, dict):
        return None, "envelope is not a JSON object"
    if "response" not in envelope:
        return None, "envelope missing 'response' field"
    inner_text = envelope["response"]
    if not isinstance(inner_text, str):
        return None, "envelope 'response' field is not a string"

    # Phase 2: strip optional markdown fences from the inner string and
    # parse it as the review JSON.
    inner = inner_text.strip()
    if not inner:
        return None, "envelope 'response' field is empty"
    if inner.startswith("```"):
        first_nl = inner.find("\n")
        if first_nl != -1:
            inner = inner[first_nl + 1:]
        if inner.endswith("```"):
            inner = inner[: -3]
        inner = inner.strip()

    try:
        obj = json.loads(inner)
    except json.JSONDecodeError as exc:
        first_err = str(exc)
    else:
        if isinstance(obj, dict):
            return obj, None
        return None, "inner JSON is not an object"

    # Last-resort: locate the outermost {...} block inside the response
    # string. Handles minor preamble/trailer the model might emit.
    start = inner.find("{")
    end = inner.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None, first_err
    candidate = inner[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, str(exc)
    if not isinstance(obj, dict):
        return None, "extracted JSON is not an object"
    return obj, None


def _validate_against_schema(
    parsed: dict, schema: dict,
) -> str | None:
    """Validate `parsed` against `schema` with jsonschema. Returns
    None on success or a short error string on failure."""
    try:
        jsonschema.validate(instance=parsed, schema=schema)
    except jsonschema.ValidationError as exc:
        # exc.message is short; full path helps debugging.
        path = ".".join(str(p) for p in exc.absolute_path)
        if path:
            return f"{exc.message} (at {path})"
        return exc.message
    except jsonschema.SchemaError as exc:
        return f"schema error: {exc.message}"
    return None


def _validate_or_retry(
    *,
    schema: dict,
    workdir: str,
    timeout_sec: int,
    gemini_home: str,
    build_prompt,
) -> dict:
    """Schema-validation retry loop shared by cmd_review and cmd_plan_review.

    Invokes Gemini up to ``SCHEMA_RETRY_MAX_ATTEMPTS`` times. On each
    iteration the caller-supplied ``build_prompt(attempt, retry_suffix)``
    callable renders the prompt: attempt 1 receives an empty
    ``retry_suffix``, subsequent attempts receive the bounded escalation
    suffix from ``_retry_suffix_for_attempt``.

    Returns a dict carrying the loop's terminal state. The wrapper-level
    ``cmd_*`` paths interpret it and emit the envelope; the helper does
    not call ``emit`` itself so the per-subcommand envelope shape stays
    in the caller.

    Result schema (always present unless noted):
      ``status``: one of
        - ``"ok"``                 — Gemini ran, output parsed + validated.
        - ``"timeout"``           — Gemini timed out on the last attempt.
        - ``"gemini_not_found"``   — gemini binary missing on PATH.
        - ``"parse_error"``        — all attempts failed parse + validation.
      ``attempts``: int (the highest attempt number reached).
      ``gemini``: the most recent ``invoke_gemini`` result dict.
      ``parsed``: dict | None (only for ``status == "ok"``).
      ``last_validation_error``: str (only for ``status == "parse_error"``).
    """
    last_validation_error = ""
    last_gemini: dict = {}
    attempts = 0
    for attempt in range(1, SCHEMA_RETRY_MAX_ATTEMPTS + 1):
        attempts = attempt
        retry_suffix = (
            "" if attempt == 1
            else _retry_suffix_for_attempt(attempt, schema)
        )
        prompt = build_prompt(attempt, retry_suffix)

        gemini = invoke_gemini(
            prompt=prompt,
            workdir=workdir,
            timeout_sec=timeout_sec,
            gemini_home=gemini_home,
        )
        last_gemini = gemini

        if gemini["status"] == "timeout":
            return {
                "status": "timeout",
                "attempts": attempt,
                "gemini": gemini,
            }
        if gemini["status"] == "gemini_not_found":
            return {
                "status": "gemini_not_found",
                "attempts": attempt,
                "gemini": gemini,
            }

        parsed_obj, parse_err = _extract_response_json(gemini["stdout"])
        if parsed_obj is None:
            last_validation_error = parse_err or "JSON parse error"
            continue

        schema_err = _validate_against_schema(parsed_obj, schema)
        if schema_err is not None:
            last_validation_error = schema_err
            continue

        return {
            "status": "ok",
            "attempts": attempt,
            "gemini": gemini,
            "parsed": parsed_obj,
        }

    return {
        "status": "parse_error",
        "attempts": attempts,
        "gemini": last_gemini,
        "last_validation_error": last_validation_error,
    }


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def make_envelope(
    task_id: str,
    subcommand: str,
    outcome: str,
    exit_code: int = 0,
    raw: str | None = None,
    parsed: dict | None = None,
    error: str | None = None,
    extra: dict | None = None,
) -> dict:
    envelope = {
        "task_id": task_id,
        "subcommand": subcommand,
        "reviewer": "gemini",
        "outcome": outcome,
        "gemini_exit_code": exit_code,
        "gemini_output_raw": (raw[:RAW_TRUNCATE_CHARS] if raw else None),
        "parsed": parsed,
        "error": error,
    }
    if extra:
        envelope.update(extra)
    return envelope


def emit(envelope: dict) -> None:
    print(json.dumps(envelope, indent=2, default=str))


# ---------------------------------------------------------------------------
# Subcommand: implement (stub for TASK-007)
# ---------------------------------------------------------------------------


def cmd_implement(args) -> int:
    """Stub. The Gemini wrapper does not currently support the implement
    subcommand. Routing in TASK-007 lands separately. We exit nonzero
    with a structured stderr message so a missing subcommand produces a
    clear error rather than a silent ``usage:`` dump."""
    sys.stderr.write(
        "plan_gemini_dispatch: implement subcommand not supported "
        "(Gemini is reviewer-only in v1; see TASK-007 for routing).\n"
    )
    return 2


# ---------------------------------------------------------------------------
# Subcommand: plan-review
# ---------------------------------------------------------------------------


def cmd_plan_review(args) -> int:
    """Phase 1.5 — Gemini independently reviews the persisted schedule.

    Schedule-only contract (mirrors plan_codex_dispatch.cmd_plan_review):
    the plan markdown is never read or rendered into the prompt. The
    persisted schedule's fat manifest carries every per-task
    ``description`` + ``acceptance_criteria`` the reviewer needs.

    Verdict vocabulary: approved | approved-with-notes | needs-replan.
    Wrapper owns the sandbox baseline + cleanup, matching review.

    The ``--allow-gaps`` demotion clause is gated by the shared helper
    ``_should_inject_allow_gaps_demotion`` and the prose comes from the
    shared ``ALLOW_GAPS_DEMOTION_CLAUSE`` constant; the Codex and
    Gemini reviewers must never drift on either.
    """
    schedule_path = Path(args.schedule_file).resolve()
    repo_root = str(Path(args.repo_root).resolve())

    if not schedule_path.exists():
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=f"Schedule file not found: {schedule_path}",
        ))
        return 1

    # `plan_basename` populates envelope.plan_file. Mirror the Codex
    # wrapper's derivation rules (TASK-006/008 schedule-only contract):
    # prefer the parent directory's basename when the sidecar lives
    # alongside `00_INDEX.json`; otherwise fall back to the schedule's
    # own stem (peeling the full ``.schedule.json`` suffix).
    parent_dir = schedule_path.parent
    if (parent_dir / "00_INDEX.json").is_file() and parent_dir.name:
        plan_basename = parent_dir.name
    elif schedule_path.name.endswith(".schedule.json"):
        plan_basename = schedule_path.name[: -len(".schedule.json")]
    else:
        plan_basename = schedule_path.stem

    try:
        schedule_text = schedule_path.read_text(encoding="utf-8")
    except OSError as exc:
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=f"Cannot read schedule file: {exc}",
        ))
        return 1

    # Parse the schedule up-front: we surface a clean malformed-JSON
    # error before consulting gaps[]. This mirrors the Codex wrapper.
    try:
        schedule_obj = json.loads(schedule_text)
    except json.JSONDecodeError:
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error="malformed schedule JSON",
        ))
        return 1

    allow_gaps_demotion = _should_inject_allow_gaps_demotion(
        schedule_obj, bool(getattr(args, "allow_gaps", False)),
    )

    if not PLAN_REVIEW_SCHEMA.exists():
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=f"Schema file missing: {PLAN_REVIEW_SCHEMA}",
        ))
        return 1

    schema = _load_plan_review_schema()
    base_prompt = render_plan_review_prompt(
        schedule_text,
        plan_basename,
        schema=schema,
        allow_gaps_demotion=allow_gaps_demotion,
    )

    # Optional debug breadcrumb: when GEMINI_DISPATCH_DEBUG=1, write the
    # rendered prompt to a temp file for tests/operators to inspect.
    if os.environ.get("GEMINI_DISPATCH_DEBUG") == "1":
        try:
            debug_dir = Path(tempfile.gettempdir()) / "plan_gemini_dispatch_debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / f"plan_review_{os.getpid()}.txt").write_text(
                base_prompt, encoding="utf-8",
            )
        except OSError:
            pass

    if args.dry_run:
        emit({
            "plan_file": plan_basename,
            "subcommand": "plan-review",
            "reviewer": "gemini",
            "outcome": "dry_run",
            "dry_run": True,
            "prompt_preview": base_prompt,
        })
        return 0

    # Pre-spawn API-key short-circuit. Runs BEFORE tempfile.mkdtemp so a
    # misconfigured invocation does not litter /tmp.
    api_key_err = _check_api_key_env()
    if api_key_err is not None:
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=api_key_err,
        ))
        return 1

    gemini_home = _make_ephemeral_gemini_home()
    baseline = _snapshot_baseline(repo_root)
    try:
        def build_prompt(attempt: int, retry_suffix: str) -> str:
            if attempt == 1:
                return base_prompt
            return render_plan_review_prompt(
                schedule_text,
                plan_basename,
                schema=schema,
                allow_gaps_demotion=allow_gaps_demotion,
                retry_suffix=retry_suffix,
            )

        result = _validate_or_retry(
            schema=schema,
            workdir=repo_root,
            timeout_sec=args.timeout,
            gemini_home=gemini_home,
            build_prompt=build_prompt,
        )
        gemini = result.get("gemini") or {}

        if result["status"] == "timeout":
            cleanup_details = _handle_timeout_cleanup(
                repo_root, [], baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
            )
            envelope = make_envelope(
                "plan", "plan-review", "timeout",
                exit_code=-1,
                raw=gemini.get("stdout") or gemini.get("stderr"),
                error=f"Gemini plan review timed out after {args.timeout}s",
                extra={
                    "wall_seconds": gemini.get("wall_seconds"),
                    "attempts": result["attempts"],
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    "cleanup_details": cleanup_details,
                    "out_of_scope_tracked": cleanup_details.get(
                        "out_of_scope_tracked", []),
                    "out_of_scope_untracked": cleanup_details.get(
                        "out_of_scope_untracked", []),
                    "out_of_scope_observed": cleanup_details.get(
                        "out_of_scope_observed", False),
                },
            )
            envelope["plan_file"] = plan_basename
            emit(envelope)
            return 1

        if result["status"] == "gemini_not_found":
            envelope = make_envelope(
                "plan", "plan-review", "failure",
                error="gemini binary not found on PATH",
                extra={"attempts": result["attempts"]},
            )
            envelope["plan_file"] = plan_basename
            emit(envelope)
            return 1

        if result["status"] == "parse_error":
            envelope = make_envelope(
                "plan", "plan-review", "parse_error",
                exit_code=gemini.get("exit_code", 0),
                raw=gemini.get("stdout", ""),
                error=(
                    f"Gemini output failed schema validation after "
                    f"{SCHEMA_RETRY_MAX_ATTEMPTS} attempts"
                ),
                extra={
                    "attempts": SCHEMA_RETRY_MAX_ATTEMPTS,
                    "last_validation_error": result["last_validation_error"],
                },
            )
            envelope["plan_file"] = plan_basename
            emit(envelope)
            return 1

        # status == "ok": parsed + validated, but exit code may still be non-zero.
        parsed_obj = result["parsed"]
        if gemini["exit_code"] != 0:
            cleanup_details = _handle_timeout_cleanup(
                repo_root, [], baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
            )
            envelope = make_envelope(
                "plan", "plan-review", "failure",
                exit_code=gemini["exit_code"],
                raw=gemini["stdout"],
                error=(
                    f"gemini exited {gemini['exit_code']} despite "
                    f"parseable JSON output"
                ),
                extra={
                    "wall_seconds": gemini["wall_seconds"],
                    "attempts": result["attempts"],
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    "cleanup_details": cleanup_details,
                },
            )
            envelope["plan_file"] = plan_basename
            emit(envelope)
            return 1

        # Observe-only post-dispatch scope: Gemini should not have
        # written anything during plan-review. Pass empty allowed set;
        # any new delta becomes out-of-scope.
        scope = validate_scope(repo_root, [], baseline)
        sandbox_escape_detected = bool(
            scope["out_of_scope_observed"]
            or scope["protected_skipped_tracked"]
            or scope["protected_skipped_untracked"]
        )

        extra: dict = {
            "wall_seconds": gemini["wall_seconds"],
            "attempts": result["attempts"],
            "scope": scope,
            "sandbox_escape_detected": sandbox_escape_detected,
            "out_of_scope_tracked": scope["out_of_scope_tracked"],
            "out_of_scope_untracked": scope["out_of_scope_untracked"],
            "out_of_scope_observed": scope["out_of_scope_observed"],
            "cleanup_strategy": scope["cleanup_strategy"],
            "baseline_captured": baseline["captured"],
        }

        envelope = make_envelope(
            "plan", "plan-review", "success",
            exit_code=gemini["exit_code"],
            raw=gemini["stdout"],
            parsed=parsed_obj,
            extra=extra,
        )
        # Mirror plan_codex_dispatch: plan-review envelopes carry plan_file
        # alongside the canonical task_id="plan" identifier.
        envelope["plan_file"] = plan_basename
        emit(envelope)
        return 0
    finally:
        _teardown_gemini_home(gemini_home)


# ---------------------------------------------------------------------------
# Subcommand: review
# ---------------------------------------------------------------------------


def cmd_review(args) -> int:
    plan_path = Path(args.plan_file).resolve()
    repo_root = str(Path(args.repo_root).resolve())

    if not plan_path.exists():
        emit(make_envelope(
            args.task_id, "review", "failure",
            error=f"Plan file not found: {plan_path}",
        ))
        return 1

    plan_text = plan_path.read_text(encoding="utf-8")
    try:
        task = parse_task_block(plan_text, args.task_id)
    except ValueError as e:
        emit(make_envelope(
            args.task_id, "review", "failure",
            error=f"Plan parse error: {e}",
        ))
        return 1

    if args.files:
        review_files = [f.strip() for f in args.files.split(",") if f.strip()]
    else:
        review_files = [normalize_file_path(f) for f in task["files"]]

    diff = git_diff_for_files(repo_root, review_files)

    # Pre-spawn API-key short-circuit. Runs BEFORE tempfile.mkdtemp so a
    # misconfigured invocation does not litter /tmp.
    api_key_err = _check_api_key_env()
    if api_key_err is not None:
        emit(make_envelope(
            task["task_id"], "review", "failure",
            error=api_key_err,
        ))
        return 1

    if not REVIEW_SCHEMA.exists():
        emit(make_envelope(
            task["task_id"], "review", "failure",
            error=f"Schema file missing: {REVIEW_SCHEMA}",
        ))
        return 1

    schema = _load_review_schema()
    base_prompt = render_review_prompt(
        task, diff, args.review_focus, review_files,
        schema=schema,
    )

    if args.dry_run:
        emit({
            "task_id": task["task_id"],
            "subcommand": "review",
            "reviewer": "gemini",
            "outcome": "dry_run",
            "dry_run": True,
            "files_under_review": review_files,
            "diff_size_bytes": len(diff),
            "review_focus": args.review_focus,
            "prompt_preview": base_prompt,
        })
        return 0

    gemini_home = _make_ephemeral_gemini_home()
    baseline = _snapshot_baseline(repo_root)
    try:
        def build_prompt(attempt: int, retry_suffix: str) -> str:
            if attempt == 1:
                return base_prompt
            return render_review_prompt(
                task, diff, args.review_focus, review_files,
                schema=schema,
                retry_suffix=retry_suffix,
            )

        result = _validate_or_retry(
            schema=schema,
            workdir=repo_root,
            timeout_sec=args.timeout,
            gemini_home=gemini_home,
            build_prompt=build_prompt,
        )
        gemini = result.get("gemini") or {}

        if result["status"] == "timeout":
            cleanup_details = _handle_timeout_cleanup(
                repo_root, review_files, baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
            )
            emit(make_envelope(
                task["task_id"], "review", "timeout",
                exit_code=-1,
                raw=gemini.get("stdout") or gemini.get("stderr"),
                error=f"Gemini review timed out after {args.timeout}s",
                extra={
                    "wall_seconds": gemini.get("wall_seconds"),
                    "attempts": result["attempts"],
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    "cleanup_details": cleanup_details,
                },
            ))
            return 1

        if result["status"] == "gemini_not_found":
            emit(make_envelope(
                task["task_id"], "review", "failure",
                error="gemini binary not found on PATH",
                extra={"attempts": result["attempts"]},
            ))
            return 1

        if result["status"] == "parse_error":
            emit(make_envelope(
                task["task_id"], "review", "parse_error",
                exit_code=gemini.get("exit_code", 0),
                raw=gemini.get("stdout", ""),
                error=(
                    f"Gemini output failed schema validation after "
                    f"{SCHEMA_RETRY_MAX_ATTEMPTS} attempts"
                ),
                extra={
                    "attempts": SCHEMA_RETRY_MAX_ATTEMPTS,
                    "last_validation_error": result["last_validation_error"],
                },
            ))
            return 1

        # status == "ok": parsed + validated, but exit code may still be non-zero.
        parsed_obj = result["parsed"]
        if gemini["exit_code"] != 0:
            cleanup_details = _handle_timeout_cleanup(
                repo_root, review_files, baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
            )
            emit(make_envelope(
                task["task_id"], "review", "failure",
                exit_code=gemini["exit_code"],
                raw=gemini["stdout"],
                error=f"gemini exited {gemini['exit_code']} despite parseable JSON output",
                extra={
                    "wall_seconds": gemini["wall_seconds"],
                    "attempts": result["attempts"],
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    "cleanup_details": cleanup_details,
                },
            ))
            return 1

        scope = validate_scope(repo_root, review_files, baseline)
        sandbox_escape_detected = bool(
            scope["out_of_scope_observed"]
            or scope["protected_skipped_tracked"]
            or scope["protected_skipped_untracked"]
        )

        extra: dict = {
            "wall_seconds": gemini["wall_seconds"],
            "attempts": result["attempts"],
            "scope": scope,
            "sandbox_escape_detected": sandbox_escape_detected,
            "out_of_scope_tracked": scope["out_of_scope_tracked"],
            "out_of_scope_untracked": scope["out_of_scope_untracked"],
            "out_of_scope_observed": scope["out_of_scope_observed"],
            "cleanup_strategy": scope["cleanup_strategy"],
            "baseline_captured": baseline["captured"],
        }

        emit(make_envelope(
            task["task_id"], "review", "success",
            exit_code=gemini["exit_code"],
            raw=gemini["stdout"],
            parsed=parsed_obj,
            extra=extra,
        ))
        return 0
    finally:
        _teardown_gemini_home(gemini_home)


# ---------------------------------------------------------------------------
# Main / argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gemini CLI dispatch wrapper for the dual-agent plan executor. "
            "Subcommands: implement (stub), review, plan-review."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def add_common(p: argparse.ArgumentParser, default_timeout: int) -> None:
        p.add_argument("--plan-file", required=True,
                       help="Absolute path to plan document")
        p.add_argument("--task-id", required=True,
                       help="Task ID within the plan (e.g. 1, 001, TASK-001)")
        p.add_argument("--repo-root", required=True,
                       help="Absolute path to the repo root")
        p.add_argument("--json", action="store_true",
                       help=("Output structured JSON (always on; flag is a "
                             "no-op reserved for future-compat)"))
        p.add_argument("--dry-run", action="store_true",
                       help="Render prompt and metadata; do not invoke Gemini")
        p.add_argument("--timeout", type=int, default=default_timeout,
                       help=f"Gemini execution timeout in seconds "
                            f"(default: {default_timeout})")

    impl = subparsers.add_parser(
        "implement",
        help="(stub) Dispatch implementation to Gemini -- not supported in v1",
    )
    add_common(impl, DEFAULT_TIMEOUT_REVIEW)

    rev = subparsers.add_parser(
        "review", help="Dispatch a review task to Gemini",
    )
    add_common(rev, DEFAULT_TIMEOUT_REVIEW)
    rev.add_argument("--files", default="",
                     help="Comma-separated list of files to review "
                          "(default: task's Files list)")
    rev.add_argument("--review-focus", default="bugs",
                     choices=["bugs", "regressions", "security", "tests"],
                     help="Review focus area (default: bugs)")

    pr = subparsers.add_parser(
        "plan-review",
        help="Dispatch a plan-level review to Gemini (Phase 1.5, schedule-only)",
    )
    pr.add_argument("--schedule-file", required=True,
                    help="Absolute path to persisted schedule JSON. Sole "
                         "required input — schedule carries the fat manifest "
                         "(per-task description + acceptance_criteria).")
    pr.add_argument("--repo-root", required=True,
                    help="Absolute path to the repo root (used as Gemini "
                         "subprocess cwd and for sandbox baseline/cleanup).")
    pr.add_argument("--json", action="store_true",
                    help=("Output structured JSON (always on; flag is a "
                          "no-op reserved for future-compat)"))
    pr.add_argument("--dry-run", action="store_true",
                    help="Render prompt and metadata; do not invoke Gemini")
    pr.add_argument("--timeout", type=int,
                    default=DEFAULT_TIMEOUT_PLAN_REVIEW,
                    help=f"Gemini execution timeout in seconds "
                         f"(default: {DEFAULT_TIMEOUT_PLAN_REVIEW})")
    pr.add_argument("--allow-gaps", action="store_true",
                    help="Forward the operator's --allow-gaps opt-in. When "
                         "set AND the persisted schedule's gaps[] contains "
                         "only soft-severity entries (and no structural "
                         "violations), the rendered prompt instructs the "
                         "reviewer to demote what would have been "
                         "`needs-replan` into `approved-with-notes`. Hard "
                         "gaps still trigger the standard needs-replan "
                         "route (plan-author auto-revise).")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "implement":
        return cmd_implement(args)
    if args.subcommand == "review":
        return cmd_review(args)
    if args.subcommand == "plan-review":
        return cmd_plan_review(args)
    parser.error(f"Unknown subcommand: {args.subcommand}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
