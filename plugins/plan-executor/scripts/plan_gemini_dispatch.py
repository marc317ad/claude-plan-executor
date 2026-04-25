#!/usr/bin/env python3
"""Gemini CLI dispatch wrapper for the dual-agent plan executor.

Mirror of plan_codex_dispatch.py for the Gemini fallback path. Three
subcommands: implement (stub for TASK-007), review (this task), and
plan-review (stub for TASK-004). The argspec, envelope shape, and
wrapper-side scope/cleanup semantics mirror the Codex wrapper, with two
non-trivial differences:

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
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
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
# Subcommand: plan-review (stub for TASK-004)
# ---------------------------------------------------------------------------


def cmd_plan_review(args) -> int:
    """Stub. The plan-review subcommand lands in TASK-004."""
    sys.stderr.write(
        "plan_gemini_dispatch: plan-review subcommand not yet "
        "implemented; see TASK-004.\n"
    )
    return 2


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
    last_exit_code = 0
    last_validation_error = ""
    last_raw = ""
    baseline = _snapshot_baseline(repo_root)
    try:
        for attempt in range(1, SCHEMA_RETRY_MAX_ATTEMPTS + 1):
            if attempt == 1:
                prompt = base_prompt
            else:
                prompt = render_review_prompt(
                    task, diff, args.review_focus, review_files,
                    schema=schema,
                    retry_suffix=_retry_suffix_for_attempt(attempt, schema),
                )

            gemini = invoke_gemini(
                prompt=prompt,
                workdir=repo_root,
                timeout_sec=args.timeout,
                gemini_home=gemini_home,
            )

            if gemini["status"] == "timeout":
                cleanup_details = _handle_timeout_cleanup(
                    repo_root, review_files, baseline,
                )
                emit(make_envelope(
                    task["task_id"], "review", "timeout",
                    exit_code=-1,
                    raw=gemini["stdout"] or gemini["stderr"],
                    error=f"Gemini review timed out after {args.timeout}s",
                    extra={
                        "wall_seconds": gemini["wall_seconds"],
                        "attempts": attempt,
                        "cleanup_strategy": cleanup_details["cleanup_strategy"],
                        "baseline_captured": baseline["captured"],
                        "cleanup_details": cleanup_details,
                    },
                ))
                return 1

            if gemini["status"] == "gemini_not_found":
                emit(make_envelope(
                    task["task_id"], "review", "failure",
                    error="gemini binary not found on PATH",
                    extra={"attempts": attempt},
                ))
                return 1

            last_exit_code = gemini["exit_code"]
            last_raw = gemini["stdout"] or ""

            parsed_obj, parse_err = _extract_response_json(gemini["stdout"])
            if parsed_obj is None:
                last_validation_error = parse_err or "JSON parse error"
                continue

            schema_err = _validate_against_schema(parsed_obj, schema)
            if schema_err is not None:
                last_validation_error = schema_err
                continue

            if gemini["exit_code"] != 0:
                cleanup_details = _handle_timeout_cleanup(
                    repo_root, review_files, baseline,
                )
                emit(make_envelope(
                    task["task_id"], "review", "failure",
                    exit_code=gemini["exit_code"],
                    raw=gemini["stdout"],
                    error=f"gemini exited {gemini['exit_code']} despite parseable JSON output",
                    extra={
                        "wall_seconds": gemini["wall_seconds"],
                        "attempts": attempt,
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
                "attempts": attempt,
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

        # Retries exhausted -> parse_error envelope.
        emit(make_envelope(
            task["task_id"], "review", "parse_error",
            exit_code=last_exit_code,
            raw=last_raw,
            error=(
                f"Gemini output failed schema validation after "
                f"{SCHEMA_RETRY_MAX_ATTEMPTS} attempts"
            ),
            extra={
                "attempts": SCHEMA_RETRY_MAX_ATTEMPTS,
                "last_validation_error": last_validation_error,
            },
        ))
        return 1
    finally:
        _teardown_gemini_home(gemini_home)


# ---------------------------------------------------------------------------
# Main / argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Gemini CLI dispatch wrapper for the dual-agent plan executor. "
            "Subcommands: implement (stub), review, plan-review (stub)."
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
        help="(stub) Plan-level review -- lands in TASK-004",
    )
    pr.add_argument("--schedule-file", required=False, default="",
                    help="(stub) Persisted schedule JSON path")
    pr.add_argument("--repo-root", required=False, default="",
                    help="(stub) Repo root")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_REVIEW)
    pr.add_argument("--allow-gaps", action="store_true")

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
