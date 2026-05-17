#!/usr/bin/env python3
"""Codex CLI dispatch wrapper for the dual-agent plan executor.

Standardizes Codex invocation with structured output, scope validation,
and error handling. Three subcommands: implement, review, and plan-review.

Based on the Phase 0 CLI contract (docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
Appendix D): exit code is unreliable, -s read-only is advisory only,
timeout leaves no output file, --output-schema + -o produces directly
json.load()-able output, prompts go via stdin.

Usage (resolve $PYTHON via `plan_ops.py preflight --json`'s python_path):
    $PYTHON scripts/plan_codex_dispatch.py implement \
        --plan-file PATH --task-id N --repo-root PATH [--dry-run] [--timeout SECS]

    $PYTHON scripts/plan_codex_dispatch.py review \
        --plan-file PATH --task-id N --repo-root PATH \
        --files f1,f2 [--review-focus bugs] [--dry-run] [--timeout SECS]

    $PYTHON scripts/plan_codex_dispatch.py plan-review \
        --schedule-file PATH --repo-root PATH [--dry-run] [--timeout SECS]
        # Schedule-only (TASK-006, finalized in TASK-008). The former
        # --plan-file / --plans-dir deprecation shims were removed;
        # callers pass the persisted schedule JSON path and nothing else.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
IMPLEMENT_SCHEMA = SCRIPT_DIR / "codex_implement_schema.json"
REVIEW_SCHEMA = SCRIPT_DIR / "codex_review_schema.json"
PLAN_REVIEW_SCHEMA = SCRIPT_DIR / "codex_plan_review_schema.json"

# Ensure the sibling ``_plan_paths`` module is importable when this file is
# loaded via ``importlib.util.spec_from_file_location`` (e.g., from tests).
# Direct CLI invocation already adds SCRIPT_DIR to ``sys.path`` automatically.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from _codex_envelope_sanitizer import (  # noqa: E402
    sanitize as _sanitize_envelope,
    trim_stdout_to_envelope as _trim_stdout_to_envelope,
)
from _plan_paths import (  # noqa: E402
    ALLOW_GAPS_DEMOTION_CLAUSE,
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
    _should_inject_allow_gaps_demotion,
    is_protected_path,
    normalize_files_entry as normalize_file_path,
)

# TASK-009: scale-aware large-file reads. The implementer / reviewer
# prompt grows a `## Pre-read excerpts` block when the task declares
# `**Read targets:**` or `**Symbol targets:**`; both helpers live in
# plan_ops.py to keep the resolver and the dispatcher integration
# referencing one canonical implementation.
import plan_ops  # noqa: E402

# Backward-compatibility alias: callers (including
# tests/scripts/test_plan_codex_dispatch_state_isolation.py) reference
# ``wrapper._is_protected``. The shared module exposes the helper as
# ``is_protected_path``; this alias is the sanctioned migration path.
_is_protected = is_protected_path

RAW_TRUNCATE_CHARS = 2000
# TASK-004 introduced file-count-aware timeout floors. TASK-009 makes the
# implement-side default task-shape-aware: file count alone was too blunt for
# test-hardening / router / parser work where the reading and verification
# cost dominates the number of declared file entries.
DEFAULT_TIMEOUT_IMPLEMENT = 600
DEFAULT_TIMEOUT_REVIEW = 180
DEFAULT_TIMEOUT_PLAN_REVIEW = 180
MAX_TIMEOUT_IMPLEMENT = 1800
# Weighted growth in seconds for the implement default. Explicit --timeout
# still bypasses every derived value.
IMPLEMENT_TIMEOUT_PER_FILE = 90
IMPLEMENT_TIMEOUT_PER_ACCEPTANCE = 45
IMPLEMENT_TIMEOUT_TEST_COMMAND_BONUS = 180
IMPLEMENT_TIMEOUT_DIRECTORY_BONUS = 180
IMPLEMENT_TIMEOUT_COMPLEXITY_BONUS = 300
REVIEW_TIMEOUT_PER_FILE = 30
GIT_TIMEOUT = 30
TEST_TIMEOUT = 300


def _contains_complexity_marker(text: str) -> bool:
    markers = (
        "e2e",
        "end-to-end",
        "fixture",
        "parser",
        "route",
        "router",
        "state",
        "round-trip",
        "test-hardening",
        "assertion",
        "behavioral",
    )
    haystack = text.lower()
    return any(marker in haystack for marker in markers)


def compute_implement_timeout(
    num_files: int,
    *,
    acceptance_criteria_count: int = 0,
    test_command: str = "",
    files: list[str] | None = None,
    description: str = "",
) -> int:
    """Task-shape-aware default for the ``implement`` timeout.

    The old ``max(300, 60 * len(files))`` rule under-budgeted tasks whose
    complexity lives in E2E assertions, parser/router semantics, fixtures, or
    verification. This weighted default keeps small mechanical edits bounded,
    gives multi-surface test work enough time to finish, and caps at the
    Claude wrapper's 1800 s default. Operators override via ``--timeout N``.
    """
    if num_files < 0:
        num_files = 0
    if acceptance_criteria_count < 0:
        acceptance_criteria_count = 0
    files = files or []
    directory_entries = sum(
        1 for f in files
        if f.rstrip().endswith("/") or "/ (" in f or "(create)" in f
    )
    score = (
        DEFAULT_TIMEOUT_IMPLEMENT
        + IMPLEMENT_TIMEOUT_PER_FILE * num_files
        + IMPLEMENT_TIMEOUT_PER_ACCEPTANCE * acceptance_criteria_count
        + IMPLEMENT_TIMEOUT_DIRECTORY_BONUS * directory_entries
    )
    if test_command and test_command.strip().lower() not in {"none", "n/a"}:
        score += IMPLEMENT_TIMEOUT_TEST_COMMAND_BONUS
    complexity_text = " ".join([description, test_command, " ".join(files)])
    if _contains_complexity_marker(complexity_text):
        score += IMPLEMENT_TIMEOUT_COMPLEXITY_BONUS
    return min(MAX_TIMEOUT_IMPLEMENT, max(DEFAULT_TIMEOUT_IMPLEMENT, score))


def compute_review_timeout(num_files: int) -> int:
    """File-count-aware default for the ``review`` subcommand timeout.

    ``max(DEFAULT_TIMEOUT_REVIEW, REVIEW_TIMEOUT_PER_FILE * num_files)``
    — a 1- to 6-file diff gets the 180 s floor; a 7-file diff gets 210 s,
    a 10-file diff 300 s. Mirrors the implement-side scaling but with the
    smaller per-file budget Codex needs to read a diff vs. plan and write
    a fresh edit. Operators override via the wrapper's ``--timeout N``
    flag.
    """
    if num_files < 0:
        num_files = 0
    return max(DEFAULT_TIMEOUT_REVIEW, REVIEW_TIMEOUT_PER_FILE * num_files)

def _load_plan_config() -> dict:
    """Read `.claude/plan-executor.json` from cwd; return {} if missing.

    Malformed JSON or unreadable file → SystemExit(2). See plan_ops._load_plan_config.
    """
    cfg_path = Path(".claude/plan-executor.json")
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"plan_codex_dispatch: malformed JSON in {cfg_path}: {exc}\n"
        )
        raise SystemExit(2)
    except OSError as exc:
        sys.stderr.write(
            f"plan_codex_dispatch: cannot read {cfg_path}: {exc}\n"
        )
        raise SystemExit(2)


_PLAN_CFG = _load_plan_config()
PLAN_DIR = Path(_PLAN_CFG.get("plan_dir", "docs/plans"))
_PLAN_DIR_POSIX = PLAN_DIR.as_posix()
RUN_LOG_PATH = PLAN_DIR / "_run_log.jsonl"


def _append_run_log(event: str, fields: dict) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, sort_keys=False) + "\n")

# Executor-infrastructure protection set (``PROTECTED_EXACT_PATHS`` /
# ``PROTECTED_PATH_PREFIXES`` / ``PROTECTED_PATH_SUFFIXES`` /
# ``PROTECTED_PATH_GLOBS``) and the ``is_protected_path`` predicate are
# imported from ``_plan_paths`` above. Three copies of these constants used
# to live in this module, in ``plan_ops.reconcile-batch``, and in
# ``plan_ops.cmd_fail_task``; they drifted. TASK-004C consolidates all three
# into the single shared module.

# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------


_WRAPPER_TASK_ID_RE = re.compile(r"^(?:TASK-)?(\d{1,3})([A-Z]?)$")


def normalize_task_id(arg: str) -> str:
    """Normalize task id to canonical form (e.g. '1' -> '001', '4a' -> '004A').

    Canonical form: ``^\\d{3}[A-Z]?$``. Lowercase letter suffixes on CLI input
    are accepted and uppercased; canonical stored/emitted ids are uppercase
    only. Mirrors ``_normalize_task_id`` in ``scripts/plan_ops.py``.
    """
    m = _WRAPPER_TASK_ID_RE.match(arg.strip().upper())
    if not m:
        raise ValueError(f"Invalid task_id: {arg!r}")
    n = int(m.group(1))
    if n < 0 or n > 999:
        raise ValueError(f"Invalid task_id: {arg!r}")
    return f"{n:03d}{m.group(2)}"


def parse_plan_context(plan_text: str) -> str:
    """Extract the `## Context` section from a plan document."""
    m = re.search(
        r"^## Context\s*\n(.*?)(?=^## |\Z)",
        plan_text,
        re.MULTILINE | re.DOTALL,
    )
    return m.group(1).strip() if m else ""


def _extract_inline_field(block: str, field: str) -> str:
    """Extract `- **Field:** value` single-line form."""
    m = re.search(
        rf"^-\s*\*\*{re.escape(field)}:\*\*\s*(.+?)\s*$",
        block,
        re.MULTILINE,
    )
    return m.group(1).strip() if m else ""


def _extract_bullet_list(block: str, field: str) -> list[str]:
    """Extract `- **Field:**\n  - item` multi-line form."""
    m = re.search(
        rf"^-\s*\*\*{re.escape(field)}:\*\*\s*$",
        block,
        re.MULTILINE,
    )
    if not m:
        return []
    items: list[str] = []
    for line in block[m.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        is_indented_bullet = (
            stripped.startswith("-")
            and (line.startswith(" ") or line.startswith("\t"))
        )
        if is_indented_bullet:
            items.append(stripped[1:].strip())
        else:
            break
    return items


def _extract_paragraph(block: str, field: str) -> str:
    """Extract `**Field:**\n<content>` paragraph form."""
    m = re.search(
        rf"^\*\*{re.escape(field)}:\*\*\s*$",
        block,
        re.MULTILINE,
    )
    if not m:
        return ""
    tail = block[m.end():]
    # Stop at next **Label:** heading
    stop = re.search(r"^\*\*[\w\s]+?:\*\*\s*$", tail, re.MULTILINE)
    end = stop.start() if stop else len(tail)
    return tail[:end].strip()


def parse_task_block(plan_text: str, task_id_arg: str) -> dict:
    """Extract a TASK-NNN block with its fields. Raises ValueError if absent."""
    task_id = normalize_task_id(task_id_arg)
    header_re = re.compile(
        rf"^### TASK-{task_id}:\s*(.+?)\s*$",
        re.MULTILINE,
    )
    m = header_re.search(plan_text)
    if not m:
        raise ValueError(f"TASK-{task_id} not found in plan")

    title = m.group(1).strip()
    tail = plan_text[m.end():]
    end_match = re.search(r"^### TASK-\d+[A-Z]?:|^## ", tail, re.MULTILINE)
    end = len(tail) if end_match is None else end_match.start()
    block = tail[:end]

    # Strip markdown-wrapping backticks from `test_command` to match the
    # canonical helper in `plan_ops._parse_task_block`. Without this, sh
    # treats the whole `` `cmd` `` string as command substitution: it
    # execs the inner command's stdout, yielding exit 127.
    test_cmd_raw = _extract_inline_field(block, "Test command")
    if (
        len(test_cmd_raw) >= 2
        and test_cmd_raw.startswith("`")
        and test_cmd_raw.endswith("`")
    ):
        test_cmd_raw = test_cmd_raw[1:-1]

    return {
        "task_id": task_id,
        "title": title,
        "status": _extract_inline_field(block, "Status"),
        "priority": _extract_inline_field(block, "Priority"),
        "test_command": test_cmd_raw,
        "files": _extract_bullet_list(block, "Files"),
        "acceptance_criteria": _extract_bullet_list(block, "Acceptance criteria"),
        "description": _extract_paragraph(block, "Description"),
        "implementation_notes": _extract_paragraph(block, "Implementation notes"),
        "reversion_guidance": _extract_paragraph(block, "Reversion guidance"),
        # TASK-009: raw block markdown so the dispatcher can resolve
        # `**Read targets:**` / `**Symbol targets:**` into a
        # `## Pre-read excerpts` block prepended to the prompt.
        "raw_block": block,
    }


# ``normalize_file_path`` is the wrapper-public alias for
# ``_plan_paths.normalize_files_entry`` (canonical helper, TASK-002).
# The local definition that used to live here drifted from the
# orchestrator's ``_normalize_files_entry`` -- the wrapper missed
# the leading-backtick capture and the prose-aware dash-split, so
# bullets like ``- `Makefile` -- add `audit` target...`` welded
# the prose continuation to the path. Both consumers now share
# one helper.


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def render_implement_prompt(
    task: dict,
    context: str,
    *,
    plan_text: str | None = None,
    target_task_id: str | None = None,
    plan_file: str | None = None,
) -> str:
    allowed = [normalize_file_path(f) for f in task["files"]]
    impl_notes = task.get("implementation_notes") or (
        "None provided -- follow existing patterns in the target files."
    )
    context_block = context or "(no context provided)"
    ac_bullets = "\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none specified)"
    # TASK-009: optional pre-read excerpts. Resolved here (not in
    # cmd_implement) so the prompt-rendering surface remains the single
    # entry point and dry-run / emit-prompt paths see the same string.
    raw_block = task.get("raw_block", "") or ""
    pre_read_block = ""
    if raw_block:
        resolved = plan_ops.resolve_read_targets(raw_block)
        pre_read_block = plan_ops.render_pre_read_excerpts(resolved)
    pre_read_prefix = f"{pre_read_block}\n" if pre_read_block else ""
    # TASK-007: auto-inject "Implement specifically `### TASK-NNN:`" as
    # the first instruction line when the resolved child plan file
    # declares >1 `### TASK-NNN:` H3 heading. The single shared helper
    # in `plan_ops` raises MissingTargetTaskIdError when target_task_id
    # is None AND >1 heading is declared (the orchestrator is required
    # to supply target_task_id for shared-file children).
    target_injection = plan_ops.render_target_task_id_injection(
        plan_text or "",
        target_task_id,
        plan_file=plan_file,
    ) if plan_text is not None else ""
    return (
        f"{target_injection}"
        f"{pre_read_prefix}"
        f"Implement TASK-{task['task_id']} from the project plan.\n\n"
        f"Objective: {task['title']}\n\n"
        f"Plan context:\n{context_block}\n\n"
        f"Scope:\n"
        f"- Allowed files: {', '.join(allowed)}\n"
        f"- Forbidden: all other files\n\n"
        f"Requirements:\n{task['description']}\n\n"
        f"Implementation notes:\n{impl_notes}\n\n"
        f"Non-goals:\n"
        f"- Do not refactor code outside the listed files\n"
        f"- Do not modify the plan document\n"
        f"- Do not commit or use git stash\n\n"
        f"Validation:\n"
        f"- Test command: {task['test_command'] or 'none'}\n"
        f"- Acceptance criteria:\n{ac_bullets}\n\n"
        f"On ambiguity: follow the nearest existing pattern in the codebase.\n\n"
        f"Output: Return schema-compliant JSON only, no markdown fences, "
        f"no trailing commentary. task_id must be \"{task['task_id']}\".\n"
    )


def render_plan_review_prompt(
    schedule_json: str,
    plan_basename: str | None = None,
    *,
    allow_gaps_demotion: bool = False,
) -> str:
    """Prompt template for Phase 1.5 — Codex reviews the persisted schedule.

    Schedule-only (TASK-006, finalized in TASK-008): the plan markdown is
    never rendered into the prompt. With the fat manifest from TASK-004,
    every per-task `description` + `acceptance_criteria` the reviewer
    needs to validate intent lives inside the schedule JSON directly.
    Callers pass the plan directory basename as ``plan_basename`` so the
    reviewer's envelope.plan_file round-trips correctly. The TASK-006-era
    legacy ``plan_text`` / ``plan_abs_path`` / ``plans_dir`` parameters
    were removed in TASK-008.

    When ``allow_gaps_demotion`` is True, the prompt carries an extra
    demotion clause instructing the reviewer to treat schedule_ok=false
    caused solely by soft-severity gaps as ``approved-with-notes`` rather
    than ``needs-replan`` (TASK-003). The caller computes the boolean
    condition from the persisted schedule + the operator's ``--allow-gaps``
    flag; this function is a pure prompt renderer.
    """
    plan_label = plan_basename if plan_basename else "(schedule-only; no plan file)"
    demotion_clause = ALLOW_GAPS_DEMOTION_CLAUSE if allow_gaps_demotion else ""
    # TASK-009 (POSTMORTEM_FIXES): embed the canonical verdict allowlists +
    # per-task ownership block so Codex stops conflating role allowlists and
    # stops misreading batch-level file aggregates.
    allowlist_block = plan_ops.render_canonical_verdict_allowlists(
        "codex-plan-review"
    )
    ownership_block = ""
    try:
        schedule_obj = json.loads(schedule_json)
    except (TypeError, ValueError):
        schedule_obj = None
    if isinstance(schedule_obj, dict):
        ownership_block = plan_ops.render_task_file_ownership(schedule_obj)
    return (
        f"{allowlist_block}"
        f"{ownership_block}"
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
        f"Persisted schedule JSON:\n\n"
        f"```json\n{schedule_json}\n```\n\n"
        f"Return schema-compliant JSON only, no markdown fences, no trailing "
        f"commentary. `plan_file` must be \"{plan_label}\".\n"
    )


def render_review_prompt(
    task: dict,
    diff: str,
    review_focus: str,
    review_files: list[str] | None = None,
    *,
    plan_text: str | None = None,
    target_task_id: str | None = None,
    plan_file: str | None = None,
) -> str:
    allowed = [normalize_file_path(f) for f in task["files"]]
    prompt_files = review_files if review_files is not None else allowed
    files_str = ", ".join(prompt_files) or "(none declared)"
    ac_bullets = "\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none specified)"
    # TASK-027A: forward Description + Implementation notes so the reviewer
    # has the same "why this pattern here" context the implementer received.
    description = task.get("description") or "(none provided)"
    impl_notes_text = task.get("implementation_notes") or "(none provided)"
    # TASK-009: pre-read excerpts apply to reviewers too — the reviewer
    # often needs the same windowed context as the implementer to judge
    # whether the diff lands inside the declared symbol/range.
    raw_block = task.get("raw_block", "") or ""
    pre_read_block = ""
    if raw_block:
        resolved = plan_ops.resolve_read_targets(raw_block)
        pre_read_block = plan_ops.render_pre_read_excerpts(resolved)
    pre_read_prefix = f"{pre_read_block}\n" if pre_read_block else ""
    # TASK-007: shared auto-injection rule (same helper as the implement
    # path) — see `render_target_task_id_injection` for the heading-count
    # condition + structured-error contract.
    target_injection = plan_ops.render_target_task_id_injection(
        plan_text or "",
        target_task_id,
        plan_file=plan_file,
    ) if plan_text is not None else ""
    # TASK-009 (POSTMORTEM_FIXES): embed the canonical verdict allowlists so
    # Codex stops conflating role-specific verdict sets. Read dynamically
    # from plan_ops module-level constants — no string duplication.
    allowlist_block = plan_ops.render_canonical_verdict_allowlists(
        "codex-review"
    )
    return (
        f"{target_injection}"
        f"{pre_read_prefix}"
        f"{allowlist_block}"
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
        f"Check specifically:\n"
        f"1. Does the implementation satisfy the stated acceptance criteria?\n"
        f"2. Is there a concrete regression in control flow, data flow, "
        f"error handling, or interface/contract behavior?\n"
        f"3. Does the change stay within declared scope?\n"
        f"4. Did the diff clearly introduce a new branch, failure mode, or "
        f"contract without corresponding validation or test coverage?\n"
        f"5. Are there concrete risky assumptions around "
        f"null/None/empty/default values that are actually exercised by the "
        f"changed logic?\n\n"
        f"Verdict rule:\n"
        f"- Use `clean` if there is no substantiated material issue in the "
        f"diff.\n"
        f"- Use `minor-findings` only for real but non-blocking issues.\n"
        f"- Use `needs-rework` only if there is at least one substantiated "
        f"issue that would likely break correctness, violate acceptance "
        f"criteria, or create meaningful execution risk.\n"
        f"- Nits, preferences, and low-confidence concerns must not produce "
        f"`needs-rework`.\n"
        f"If there are no non-blocking observations, return `notes: []`.\n\n"
        f"Here is the diff for the changed files:\n\n"
        f"```diff\n{diff}\n```\n\n"
        f"Return schema-compliant JSON only. task_id must be \"{task['task_id']}\".\n"
    )


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(
    args: list[str],
    cwd: str,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git_changed_files(repo_root: str) -> dict:
    """Return dict with sorted {'tracked': [...], 'untracked': [...]}."""
    tracked: set[str] = set()
    # Unstaged changes vs HEAD
    r = _git(["diff", "--name-only", "HEAD"], cwd=repo_root)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                tracked.add(ln)
    # Staged changes
    r = _git(["diff", "--name-only", "--cached"], cwd=repo_root)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                tracked.add(ln)

    untracked: set[str] = set()
    r = _git(
        ["ls-files", "--others", "--exclude-standard"],
        cwd=repo_root,
    )
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                untracked.add(ln)

    return {
        "tracked": sorted(tracked),
        "untracked": sorted(untracked),
    }


def git_diff_for_files(
    repo_root: str,
    files: list[str],
    *,
    include_untracked: bool = False,
) -> str:
    """Compute the diff for the specified files (vs HEAD, then unstaged fallback)."""
    if not files:
        return ""
    if include_untracked:
        _git(["add", "-N", "--"] + files, cwd=repo_root)
    try:
        r = _git(["diff", "HEAD", "--"] + files, cwd=repo_root)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        r = _git(["diff", "--"] + files, cwd=repo_root)
        return r.stdout if r.returncode == 0 else ""
    finally:
        if include_untracked:
            _git(["reset", "HEAD", "--"] + files, cwd=repo_root)


# TASK-027A: hallucinated-symbol post-check. Negative-lookbehind character
# class includes `.` so dotted expressions like obj._helper( and obj.method(
# do NOT match — v1 scope is bare symbols only.
_SYMBOL_PATTERNS = [
    re.compile(r"(?<![A-Za-z0-9_.])(_[A-Za-z][A-Za-z0-9_]*)\("),
    re.compile(r"(?<![A-Za-z0-9_.])(--[a-z][a-z0-9-]+)(?=\b)"),
    re.compile(r"(?<![A-Za-z0-9_.])([a-z][a-z0-9_]{3,})\([a-zA-Z_]"),
]


def _verify_cited_symbols(parsed: dict, repo_root: str) -> list[dict]:
    """Best-effort grep of each finding's cited symbols against its cited file.

    Consumes the raw Codex `parsed` object (not the wrapper envelope).
    Returns a list of {finding_index, cited_symbol, file, status} entries.

    Status semantics:
      "not-found"  — candidate extracted, absent from the cited file.
      "unchecked"  — candidate extracted, but file missing, path escapes
                     repo_root, or read failed.
      No candidate extractable -> NO entry emitted for that finding.
    """
    warnings: list[dict] = []
    findings = (parsed or {}).get("findings") or []
    repo_root_resolved = Path(repo_root).resolve()
    for idx, finding in enumerate(findings):
        issue_text = finding.get("issue") or ""
        cited_file = finding.get("file") or ""
        candidates: set[str] = set()
        for pat in _SYMBOL_PATTERNS:
            for m in pat.finditer(issue_text):
                candidates.add(m.group(1))
        if not candidates:
            continue  # prose-only finding -> no entry

        if not cited_file:
            for sym in sorted(candidates):
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": "", "status": "unchecked",
                })
            continue

        abs_path = (repo_root_resolved / cited_file).resolve()
        try:
            abs_path.relative_to(repo_root_resolved)
        except ValueError:
            for sym in sorted(candidates):
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "unchecked",
                })
            continue

        if not abs_path.is_file():
            for sym in sorted(candidates):
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "unchecked",
                })
            continue

        try:
            file_text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            for sym in sorted(candidates):
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "unchecked",
                })
            continue

        for sym in sorted(candidates):
            if sym not in file_text:
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "not-found",
                })
    return warnings


def _snapshot_baseline(repo_root: str) -> dict:
    """Pre-dispatch baseline of tracked + untracked diff.

    Returns ``{'tracked': frozenset, 'untracked': frozenset, 'captured': bool,
    'error': str | None}``. On git failure ``captured=False`` and ``error``
    carries the underlying ``SubprocessError`` / ``OSError`` message
    truncated to 200 chars; downstream cleanup paths skip cleanup entirely
    in that case (see ``_handle_timeout_cleanup``). ``cmd_implement``
    forwards ``error`` into the timeout envelope's ``baseline_error``
    field so the orchestrator can route differently when baseline capture
    itself failed (TASK-004).
    """
    try:
        pre = git_changed_files(repo_root)
    except (subprocess.SubprocessError, OSError) as exc:
        return {
            "tracked": frozenset(),
            "untracked": frozenset(),
            "captured": False,
            "error": str(exc)[:200],
        }
    return {
        "tracked": frozenset(pre["tracked"]),
        "untracked": frozenset(pre["untracked"]),
        "captured": True,
        "error": None,
    }


def _walk_schema_nodes(node: object, pointer: str = "$"):
    if isinstance(node, dict):
        yield pointer, node
        for key, child in node.items():
            if isinstance(child, (dict, list)):
                yield from _walk_schema_nodes(child, f"{pointer}.{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            if isinstance(child, (dict, list)):
                yield from _walk_schema_nodes(child, f"{pointer}[{index}]")


def validate_openai_strict_output_schema(schema_path: str | Path) -> list[str]:
    """Return OpenAI/Codex structured-output schema compatibility errors."""
    path = Path(schema_path)
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"{path}: cannot read schema: {exc}"]
    except json.JSONDecodeError as exc:
        return [f"{path}: schema is not valid JSON: {exc}"]

    errors: list[str] = []
    for pointer, node in _walk_schema_nodes(schema):
        if not isinstance(node, dict):
            continue
        if node.get("type") != "object" or node.get("additionalProperties") is not False:
            continue
        properties = node.get("properties")
        required = node.get("required")
        if not isinstance(properties, dict):
            errors.append(f"{pointer}: object schema must define properties")
            continue
        if not isinstance(required, list):
            errors.append(f"{pointer}: object schema must define required as a list")
            continue
        property_keys = set(properties.keys())
        required_keys = set(required)
        missing = sorted(property_keys - required_keys)
        extra = sorted(required_keys - property_keys)
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"missing required entries for properties: {missing}")
            if extra:
                detail.append(f"required entries without properties: {extra}")
            errors.append(f"{pointer}: " + "; ".join(detail))
    return errors


# ---------------------------------------------------------------------------
# Codex invocation
# ---------------------------------------------------------------------------


def invoke_codex(
    prompt: str,
    workdir: str,
    schema_path: str,
    output_path: str,
    timeout_sec: int,
    sandbox: str | None = None,
) -> dict:
    """Invoke Codex CLI with structured output + JSONL monitoring.

    Returns dict:
        status: 'ok' | 'timeout' | 'codex_not_found' | 'output_schema_invalid'
        exit_code: int (-1 for timeout/not_found)
        stdout: str (JSONL event stream)
        stderr: str
        file_changes: list[str] (parsed from JSONL file_change events)
        wall_seconds: float
    """
    schema_errors = validate_openai_strict_output_schema(schema_path)
    if schema_errors:
        return {
            "status": "output_schema_invalid",
            "exit_code": -1,
            "stdout": "",
            "stderr": (
                "Output schema is not compatible with Codex structured output:\n"
                + "\n".join(f"- {error}" for error in schema_errors)
            ),
            "file_changes": [],
            "wall_seconds": 0.0,
            "dropped_bytes": 0,
        }

    cmd = [
        "codex", "exec",
        "--full-auto",
        "--ephemeral",
        "--json",  # Appendix D B2: coexists with -o
    ]
    if sandbox:
        cmd.extend(["-s", sandbox])
    cmd.extend([
        "-C", workdir,
        "--output-schema", schema_path,
        "-o", output_path,
        "-",  # Appendix D E1/E2: stdin delivery
    ])

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout_sec,
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
            "file_changes": [],
            "wall_seconds": elapsed,
        }
    except FileNotFoundError:
        return {
            "status": "codex_not_found",
            "exit_code": -1,
            "stdout": "",
            "stderr": "codex binary not found on PATH",
            "file_changes": [],
            "wall_seconds": 0.0,
        }

    elapsed = time.monotonic() - start
    raw_stdout_text = proc.stdout.decode(errors="replace")
    stderr_text = proc.stderr.decode(errors="replace")

    # TASK-003 (Phase D, layer 1): trim bytes outside the JSON envelope
    # BEFORE returning. The returned ``stdout`` is the source of
    # ``codex_output_raw`` in downstream envelopes — leaving non-JSON
    # bytes in violates the sanitizer perimeter (a downstream envelope
    # carrying ``codex_output_raw`` would re-emit the dropped bytes).
    # ``_trim_stdout_to_envelope`` keeps only lines that parse as JSON
    # and returns the dropped byte count for ``extra.dropped_bytes``.
    stdout_text, dropped_bytes = _trim_stdout_to_envelope(raw_stdout_text)

    # Parse JSONL for file_change events (Appendix D B2). The trimmed
    # stdout excludes non-JSON lines by construction, so the JSON parse
    # below cannot raise; we keep the explicit isinstance/key guards
    # because trimming filters by parseability, not event shape.
    file_changes: list[str] = []
    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        event = json.loads(stripped)
        if isinstance(event, dict) and event.get("type") == "file_change":
            path = event.get("path")
            if path and isinstance(path, str):
                file_changes.append(path)

    return {
        "status": "ok",
        "exit_code": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "file_changes": file_changes,
        "wall_seconds": elapsed,
        "dropped_bytes": dropped_bytes,
    }


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


def _path_in_allowed_scope(path: str, allowed_files: list[str]) -> bool:
    """Return true when path is exactly allowed or below a declared dir."""
    rel = path.strip().strip("/")
    if not rel:
        return False
    allowed_set = set(allowed_files)
    if rel in allowed_set:
        return True
    for entry in allowed_files:
        base = entry.strip().strip("/")
        if entry.endswith("/") and base and rel.startswith(f"{base}/"):
            return True
    return False


def _restore_in_scope(
    tracked: list[str],
    untracked: list[str],
    repo_root: str,
    *,
    authorization_source: str,
) -> None:
    """Restore/delete changes that are inside allowed_files.

    Safe because caller has already filtered inputs to the task's own scope;
    out-of-scope paths must never be passed here. Sibling work on disjoint
    files is preserved by construction.
    """
    # Gated restore (TASK-002 prohibit_silent_revert extension).
    _ALLOWED = {
        "wrapper_internal_cleanup_explicit_declaration",
        "wrapper_observe_only_blocked_by_status",
    }
    if authorization_source not in _ALLOWED:
        raise RuntimeError(
            f"Unauthorized wrapper restore attempt. "
            f"Expected authorization_source in {sorted(_ALLOWED)}, "
            f"got {authorization_source!r}."
        )

    if authorization_source == "wrapper_observe_only_blocked_by_status":
        # Observe-only mode (TASK-003): caller wants to detect scope
        # violations but NOT mutate the working tree.
        return

    if tracked:
        _git(["restore", "--source=HEAD", "--"] + tracked, cwd=repo_root)
        _git(["restore", "--staged", "--"] + tracked, cwd=repo_root)
        _git(["restore", "--"] + tracked, cwd=repo_root)

    for f in untracked:
        full = Path(repo_root) / f
        try:
            full.unlink()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            continue


def validate_scope(
    repo_root: str,
    allowed_files: list[str],
    baseline: dict,
) -> dict:
    """Observe the delta vs pre-dispatch baseline.

    Observe-only semantics: the wrapper NEVER mutates files outside
    `allowed_files`. Out-of-scope writes are reported via
    `out_of_scope_observed` plus `out_of_scope_tracked`/`out_of_scope_untracked`
    lists so the orchestrator can reconcile at the batch join barrier
    (single-writer phase). Sibling tasks on disjoint files cannot erase
    each other's work because no mutation happens here.

    Protected executor-infrastructure paths (`_run_log.jsonl`, `.claude/`,
    `.codex/`, `*.schedule.json`, etc.) are classified under
    `protected_skipped_*` and treated as never-touched by design.
    """
    post = git_changed_files(repo_root)
    post_tracked = set(post["tracked"])
    post_untracked = set(post["untracked"])

    if not baseline.get("captured", False):
        # No baseline — cannot safely classify deltas; treat as fully observed
        # but with no violations computed. Caller sees baseline_captured=False
        # and proceeds without reconciliation hints.
        return {
            "out_of_scope_tracked": [],
            "out_of_scope_untracked": [],
            "out_of_scope_observed": False,
            "changed_in_scope": sorted(
                p for p in (post_tracked | post_untracked)
                if _path_in_allowed_scope(p, allowed_files)
            ),
            "changed_in_scope_new": [],
            "protected_skipped_tracked": [],
            "protected_skipped_untracked": [],
            "cleanup_strategy": "skipped_no_baseline",
            "baseline_captured": False,
        }

    baseline_tracked = set(baseline["tracked"])
    baseline_untracked = set(baseline["untracked"])

    new_tracked = post_tracked - baseline_tracked
    new_untracked = post_untracked - baseline_untracked

    out_of_scope_tracked_raw = {
        p for p in new_tracked
        if not _path_in_allowed_scope(p, allowed_files)
    }
    out_of_scope_untracked_raw = {
        p for p in new_untracked
        if not _path_in_allowed_scope(p, allowed_files)
    }

    protected_skipped_tracked = sorted(
        p for p in out_of_scope_tracked_raw if _is_protected(p)
    )
    protected_skipped_untracked = sorted(
        p for p in out_of_scope_untracked_raw if _is_protected(p)
    )
    out_of_scope_tracked = sorted(
        p for p in out_of_scope_tracked_raw if not _is_protected(p)
    )
    out_of_scope_untracked = sorted(
        p for p in out_of_scope_untracked_raw if not _is_protected(p)
    )

    out_of_scope_observed = bool(out_of_scope_tracked or out_of_scope_untracked)

    changed_in_scope_new = sorted(
        p for p in (new_tracked | new_untracked)
        if _path_in_allowed_scope(p, allowed_files)
    )
    changed_in_scope = sorted(
        p for p in (post_tracked | post_untracked)
        if _path_in_allowed_scope(p, allowed_files)
    )

    return {
        "out_of_scope_tracked": out_of_scope_tracked,
        "out_of_scope_untracked": out_of_scope_untracked,
        "out_of_scope_observed": out_of_scope_observed,
        "changed_in_scope": changed_in_scope,
        "changed_in_scope_new": changed_in_scope_new,
        "protected_skipped_tracked": protected_skipped_tracked,
        "protected_skipped_untracked": protected_skipped_untracked,
        "cleanup_strategy": "observe_only",
        "baseline_captured": True,
    }


def _resolve_unattended_revert_policy(args) -> Optional[str]:
    """Resolve the policy from --unattended-revert-policy or $UNATTENDED_REVERT_POLICY.

    Argparse choices already enforce the closed enum on the CLI side. The env
    var is the orchestrator-pinned fallback (Phase 0 preflight); we silently
    drop any out-of-enum env value so a stale shell does not crash the
    wrapper. None means "let the cleanup helper apply its own default".
    """
    val = getattr(args, "unattended_revert_policy", None)
    if val is None:
        env_val = os.environ.get("UNATTENDED_REVERT_POLICY")
        if env_val in {"pause", "fail-fast", "preserve-only"}:
            val = env_val
    return val


def _handle_timeout_cleanup(
    repo_root: str,
    allowed_files: list[str],
    baseline: dict,
    *,
    authorization_source: str,
    unattended_revert_policy: Optional[str] = None,
) -> dict:
    """In-scope cleanup after a Codex timeout; observe-only outside scope.

    Restores/deletes only files inside `allowed_files` (new tracked plus
    new untracked intersected with the task's declared scope). Anything
    observed outside `allowed_files` is reported via `out_of_scope_*`
    lists for orchestrator reconciliation and is NEVER mutated here —
    concurrent siblings' work and executor infrastructure are safe.

    Never invokes `git checkout -- .` or `git clean -fd`. Skipped entirely
    if no baseline was captured.
    """
    if not baseline.get("captured", False):
        return {
            "cleanup_strategy": "skipped_no_baseline",
            "restored_tracked": [],
            "deleted_untracked": [],
            "out_of_scope_tracked": [],
            "out_of_scope_untracked": [],
            "out_of_scope_observed": False,
            "protected_skipped_tracked": [],
            "protected_skipped_untracked": [],
        }

    baseline_tracked = set(baseline["tracked"])
    baseline_untracked = set(baseline["untracked"])
    try:
        post = git_changed_files(repo_root)
    except (subprocess.SubprocessError, OSError):
        return {
            "cleanup_strategy": "skipped_git_failed",
            "restored_tracked": [],
            "deleted_untracked": [],
            "out_of_scope_tracked": [],
            "out_of_scope_untracked": [],
            "out_of_scope_observed": False,
            "protected_skipped_tracked": [],
            "protected_skipped_untracked": [],
        }

    post_tracked = set(post["tracked"])
    post_untracked = set(post["untracked"])
    new_tracked = post_tracked - baseline_tracked
    new_untracked = post_untracked - baseline_untracked

    # In-scope: safe to restore/delete because they are the task's own files
    tracked_candidates_raw = {
        p for p in (post_tracked - baseline_tracked)
        if _path_in_allowed_scope(p, allowed_files)
    }
    delete_candidates_raw = {
        p for p in new_untracked
        if _path_in_allowed_scope(p, allowed_files)
    }

    # Out-of-scope: observed only, never mutated
    out_of_scope_tracked_raw = {
        p for p in new_tracked
        if not _path_in_allowed_scope(p, allowed_files)
    }
    out_of_scope_untracked_raw = {
        p for p in new_untracked
        if not _path_in_allowed_scope(p, allowed_files)
    }

    protected_skipped_tracked = sorted(
        {p for p in tracked_candidates_raw if _is_protected(p)}
        | {p for p in out_of_scope_tracked_raw if _is_protected(p)}
    )
    protected_skipped_untracked = sorted(
        {p for p in delete_candidates_raw if _is_protected(p)}
        | {p for p in out_of_scope_untracked_raw if _is_protected(p)}
    )
    restore_tracked = sorted(
        p for p in tracked_candidates_raw if not _is_protected(p)
    )
    delete_untracked = sorted(
        p for p in delete_candidates_raw if not _is_protected(p)
    )
    out_of_scope_tracked = sorted(
        p for p in out_of_scope_tracked_raw if not _is_protected(p)
    )
    out_of_scope_untracked = sorted(
        p for p in out_of_scope_untracked_raw if not _is_protected(p)
    )
    out_of_scope_observed = bool(out_of_scope_tracked or out_of_scope_untracked)

    if restore_tracked or delete_untracked:
        _restore_in_scope(
            restore_tracked,
            delete_untracked,
            repo_root,
            authorization_source="wrapper_internal_cleanup_explicit_declaration",
        )

    return {
        "cleanup_strategy": "in_scope_only",
        "restored_tracked": restore_tracked,
        "deleted_untracked": delete_untracked,
        "out_of_scope_tracked": out_of_scope_tracked,
        "out_of_scope_untracked": out_of_scope_untracked,
        "out_of_scope_observed": out_of_scope_observed,
        "protected_skipped_tracked": protected_skipped_tracked,
        "protected_skipped_untracked": protected_skipped_untracked,
        "unattended_revert_policy": unattended_revert_policy,
    }


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


# TASK-008 (POSTMORTEM_FIXES): the sandbox-divergence escape hatch caps
# the per-stream capture surfaced in the failure envelope at 32 KB. Larger
# outputs are truncated keeping the trailing window (most relevant to a
# test failure) and a `truncated_to` byte marker is recorded alongside.
SANDBOX_TEST_CAPTURE_CAP = 32 * 1024
_DEFERRED_TEST_RE = re.compile(
    r"^deferred\s*\(\s*TASK-(?P<task_id>\d{3}[A-Z]?)\s*\)\s*(?P<note>.*)$",
    re.IGNORECASE,
)


def _truncate_stream(text: str, cap: int = SANDBOX_TEST_CAPTURE_CAP) -> tuple[str, int | None]:
    """Cap ``text`` to ``cap`` bytes, preserving the trailing window.

    Returns ``(text, None)`` when no truncation was needed; otherwise
    ``(tail, original_len_bytes)`` where ``tail`` is the last ``cap``
    bytes of the UTF-8 encoded form, decoded back with replacement on
    UTF-8 boundary mid-codepoint splits. The integer is the wrapper's
    ``truncated_to`` marker (the original byte length) so consumers can
    tell how much was elided.
    """
    if text is None:
        return "", None
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return text, None
    tail = encoded[-cap:].decode("utf-8", errors="replace")
    return tail, len(encoded)


def run_test_command(
    test_cmd: str,
    repo_root: str,
    timeout_sec: int = TEST_TIMEOUT,
    max_attempts: int = 2,
) -> dict:
    """Run a test command with flaky-detection retry.

    Returns a dict with ``result`` ∈ {passed, failed, not_run, deferred},
    ``attempts``, ``flaky``, ``command``, ``output_tail`` (combined
    stdout+stderr last 2KB — preserved for back-compat) plus
    ``stdout`` / ``stderr`` / ``exit_code`` capturing the LAST attempt's
    captured streams (used by TASK-008's sandbox-divergence envelope to
    surface sandbox vs target-env test divergence).
    """
    cmd = (test_cmd or "").strip()
    # Defensive unwrap: parse_task_block already strips markdown-wrapping
    # backticks, but any direct caller handing us raw `` `cmd` `` would
    # otherwise hit shell command-substitution and exit 127.
    if len(cmd) >= 2 and cmd.startswith("`") and cmd.endswith("`"):
        cmd = cmd[1:-1].strip()
    if not cmd or cmd.lower() == "none":
        return {
            "result": "not_run",
            "attempts": 0,
            "output_tail": "",
            "flaky": False,
            "command": cmd,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
        }
    if cmd.lower().startswith("deferred"):
        m = _DEFERRED_TEST_RE.match(cmd)
        if not m:
            return {
                "result": "failed",
                "attempts": 0,
                "output_tail": (
                    "malformed deferred test marker; expected "
                    "`deferred (TASK-NNN)` with a task reference"
                ),
                "flaky": False,
                "command": cmd,
                "stdout": "",
                "stderr": (
                    "malformed deferred test marker; expected "
                    "`deferred (TASK-NNN)` with a task reference"
                ),
                "exit_code": None,
            }
        note = (m.group("note") or "").strip()
        result = {
            "result": "deferred",
            "attempts": 0,
            "output_tail": "",
            "flaky": False,
            "command": cmd,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "deferred_to": m.group("task_id").upper(),
        }
        if note:
            result["note"] = note
        return result

    last_tail = ""
    last_stdout = ""
    last_stderr = ""
    last_exit_code: int | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=repo_root,
                capture_output=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as e:
            tail = (e.stdout or b"").decode(errors="replace")[-2000:]
            timeout_stdout = (e.stdout or b"").decode(errors="replace")
            timeout_stderr = (e.stderr or b"").decode(errors="replace")
            return {
                "result": "failed",
                "attempts": attempt,
                "output_tail": f"TIMEOUT after {timeout_sec}s\n{tail}",
                "flaky": False,
                "command": cmd,
                "stdout": timeout_stdout,
                "stderr": (
                    f"TIMEOUT after {timeout_sec}s\n{timeout_stderr}"
                ),
                "exit_code": None,
            }

        last_stdout = proc.stdout.decode(errors="replace")
        last_stderr = proc.stderr.decode(errors="replace")
        last_exit_code = proc.returncode
        out = last_stdout + last_stderr
        last_tail = out[-2000:]
        if proc.returncode == 0:
            return {
                "result": "passed",
                "attempts": attempt,
                "output_tail": last_tail,
                "flaky": attempt > 1,
                "command": cmd,
                "stdout": last_stdout,
                "stderr": last_stderr,
                "exit_code": last_exit_code,
            }

    return {
        "result": "failed",
        "attempts": max_attempts,
        "output_tail": last_tail,
        "flaky": False,
        "command": cmd,
        "stdout": last_stdout,
        "stderr": last_stderr,
        "exit_code": last_exit_code,
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
        "outcome": outcome,
        "codex_exit_code": exit_code,
        "codex_output_raw": (raw[:RAW_TRUNCATE_CHARS] if raw else None),
        "parsed": parsed,
        "error": error,
    }
    if extra:
        envelope.update(extra)
    return envelope


# TASK-003 (Phase D, layer 1): the layer-1 outer-boundary trim happens
# inside ``invoke_codex`` (non-JSON lines on Codex stdout are dropped and
# the byte count is returned). The most recent count is held here so
# ``emit()`` can stamp ``extra.dropped_bytes`` on every envelope emitted
# during the same dispatch turn — sanitizer audit lives next to the
# envelope it audits, regardless of which control-flow branch produced it.
_LAST_DROPPED_BYTES: int | None = None


def _record_dropped_bytes(count: int | None) -> None:
    global _LAST_DROPPED_BYTES
    _LAST_DROPPED_BYTES = count


# TASK-004 (Phase D, layer 4): optional content-sanitizer subagent.
# When the operator passes ``--content-sanitizer-check`` to any subcommand,
# ``main()`` flips this flag. ``emit()`` then dispatches the unprivileged
# ``content-sanitizer`` agent for every envelope whose
# ``extra.sanitizer_flags`` is non-empty, and stamps the verdict at
# ``extra.content_sanitizer_verdict``. The orchestrator sees the verdict;
# never the suspect text. Dispatch failures degrade to
# ``{"status": "error", "reason": "..."}`` so routing can continue.
_CONTENT_SANITIZER_CHECK_ENABLED: bool = False


def _set_content_sanitizer_check(enabled: bool) -> None:
    global _CONTENT_SANITIZER_CHECK_ENABLED
    _CONTENT_SANITIZER_CHECK_ENABLED = bool(enabled)


def _extract_suspect_text(sanitized_envelope: dict) -> str:
    """Collect the post-redaction free-text fields the wrapper just sanitized.

    Mirrors ``_codex_envelope_sanitizer.SCALAR_FIELDS`` and ``FINDING_FIELDS``.
    Returned as a single newline-delimited blob with field-path headers so
    the sanitizer agent can attribute its verdict to the originating field.
    The blob is NEVER returned to the orchestrator — it is consumed by the
    sanitizer subagent only.
    """
    parsed = sanitized_envelope.get("parsed")
    if not isinstance(parsed, dict):
        return ""
    chunks: list[str] = []
    for key in ("summary", "diff_summary"):
        val = parsed.get(key)
        if isinstance(val, str) and val:
            chunks.append(f"## parsed.{key}\n{val}")
    findings = parsed.get("findings")
    if isinstance(findings, list):
        for idx, item in enumerate(findings):
            if not isinstance(item, dict):
                continue
            for key in ("message", "issue", "suggested_fix"):
                val = item.get(key)
                if isinstance(val, str) and val:
                    chunks.append(
                        f"## parsed.findings[{idx}].{key}\n{val}"
                    )
    return "\n\n".join(chunks)


def _dispatch_content_sanitizer(
    suspect_text: str,
    sanitizer_flags: list[dict],
) -> dict:
    """Invoke the unprivileged ``content-sanitizer`` agent and return verdict.

    Default implementation degrades to ``{"status": "error", "reason": ...}``
    because this wrapper has no in-process Claude backend wired (the
    orchestrator owns subagent dispatch). Tests monkeypatch this function
    directly to exercise the four verdict paths (clean / suspicious /
    malicious / error).

    A future production wiring would route through
    ``_claude_backend.invoke()`` with the ``content-sanitizer`` manifest,
    cheap model tier, and an empty tools list. That production path is
    deliberately not built here — TASK-004's AC requires only that the
    integration test exercise the verdict shapes via a stubbed dispatcher
    and that dispatch failures degrade gracefully.
    """
    return {
        "status": "error",
        "reason": (
            "content-sanitizer dispatch not wired in this wrapper "
            "(orchestrator owns subagent dispatch); stub-only path"
        ),
    }


def _maybe_run_content_sanitizer(sanitized_envelope: dict) -> None:
    """Conditionally dispatch the content-sanitizer and stamp the verdict.

    Gated by the module-level ``_CONTENT_SANITIZER_CHECK_ENABLED`` flag and
    by the presence of ``extra.sanitizer_flags`` on the sanitized envelope.
    Mutates ``sanitized_envelope`` in place by adding
    ``extra.content_sanitizer_verdict``. The raw suspect text is consumed
    locally and is NEVER attached to the envelope.
    """
    if not _CONTENT_SANITIZER_CHECK_ENABLED:
        return
    extra = sanitized_envelope.get("extra")
    if not isinstance(extra, dict):
        return
    flags = extra.get("sanitizer_flags")
    if not isinstance(flags, list) or not flags:
        return
    suspect_text = _extract_suspect_text(sanitized_envelope)
    try:
        verdict = _dispatch_content_sanitizer(suspect_text, flags)
    except Exception as exc:
        verdict = {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(verdict, dict):
        verdict = {
            "status": "error",
            "reason": (
                f"content-sanitizer returned non-dict verdict "
                f"(type={type(verdict).__name__})"
            ),
        }
    # Defensive: never attach raw suspect text under the verdict key.
    verdict.pop("suspect_text", None)
    extra["content_sanitizer_verdict"] = verdict


def emit(envelope: dict) -> None:
    # TASK-003 (Phase D): wrapper-side envelope sanitizer perimeter.
    # All envelopes routed through ``sanitize()`` before stdout emission so
    # the orchestrator never sees raw injection shapes in free-text fields.
    # The pre-redaction payload is recorded once to the run-log as a
    # ``sanitizer_redaction`` event (sha256 only) by the sanitizer; the raw
    # payload is never re-emitted downstream.
    try:
        sanitized, _flagged = _sanitize_envelope(envelope)
    except Exception as exc:
        # Fail-closed: a sanitizer bug MUST NOT cause the original (raw)
        # envelope to reach stdout — that would violate the perimeter
        # invariant ("raw payload is never re-emitted downstream").
        # Strip every known free-text field, replace with a redaction
        # marker, and stamp ``extra.sanitizer_flags`` so the orchestrator
        # can detect the failure.
        sanitized = {
            "task_id": envelope.get("task_id"),
            "subcommand": envelope.get("subcommand"),
            "outcome": envelope.get("outcome"),
            "codex_exit_code": envelope.get("codex_exit_code"),
            "codex_output_raw": "[redacted:sanitizer-error]",
            "parsed": None,
            "error": "[redacted:sanitizer-error]",
            "extra": {
                "sanitizer_flags": [{
                    "shape": "sanitizer_error",
                    "field": "<envelope>",
                    "count": 1,
                    "error_type": type(exc).__name__,
                }],
            },
        }
    if _LAST_DROPPED_BYTES is not None:
        extra = sanitized.setdefault("extra", {})
        if isinstance(extra, dict):
            extra.setdefault("dropped_bytes", _LAST_DROPPED_BYTES)
    # TASK-004 (Phase D, layer 4): optional content-sanitizer verdict.
    # Stamps ``extra.content_sanitizer_verdict`` when the gate is enabled
    # AND the wrapper-side sanitizer flagged at least one shape. The raw
    # suspect text is consumed inside ``_maybe_run_content_sanitizer`` and
    # NEVER reaches stdout.
    _maybe_run_content_sanitizer(sanitized)
    print(json.dumps(sanitized, indent=2, default=str))


# ---------------------------------------------------------------------------
# Subcommand: implement
# ---------------------------------------------------------------------------


def cmd_implement(args) -> int:
    plan_path = Path(args.plan_file).resolve()
    repo_root = str(Path(args.repo_root).resolve())

    if not plan_path.exists():
        emit(make_envelope(
            args.task_id, "implement", "failure",
            error=f"Plan file not found: {plan_path}",
        ))
        return 1

    plan_text = plan_path.read_text(encoding="utf-8")
    try:
        task = parse_task_block(plan_text, args.task_id)
    except ValueError as e:
        emit(make_envelope(
            args.task_id, "implement", "failure",
            error=f"Plan parse error: {e}",
        ))
        return 1

    context = parse_plan_context(plan_text)
    # TASK-007: forward --target-task-id so the renderer auto-injects the
    # "Implement specifically `### TASK-NNN:`" line when the child plan
    # file carries >1 H3 heading. ``getattr`` keeps the attribute optional
    # for tests that build argparse Namespaces by hand.
    target_task_id = getattr(args, "target_task_id", None)
    try:
        prompt = render_implement_prompt(
            task, context,
            plan_text=plan_text,
            target_task_id=target_task_id,
            plan_file=str(plan_path),
        )
    except plan_ops.MissingTargetTaskIdError as e:
        emit(make_envelope(
            args.task_id, "implement", "failure",
            error=f"Missing target_task_id: {e}",
            extra={"plan_file": e.plan_file, "heading_count": e.heading_count},
        ))
        return 1
    allowed_files = [normalize_file_path(f) for f in task["files"]]

    if args.dry_run:
        emit({
            "task_id": task["task_id"],
            "subcommand": "implement",
            "outcome": "dry_run",
            "dry_run": True,
            "allowed_files": allowed_files,
            "test_command": task["test_command"],
            "prompt_preview": prompt,
        })
        return 0

    if not IMPLEMENT_SCHEMA.exists():
        emit(make_envelope(
            task["task_id"], "implement", "failure",
            error=f"Schema file missing: {IMPLEMENT_SCHEMA}",
        ))
        return 1

    tmp_out = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="codex_impl_",
    )
    tmp_out.close()
    output_path = tmp_out.name

    # TASK-009: task-shape-aware default. Operator override via --timeout
    # short-circuits the derivation — None means "use the wrapper-derived
    # default for this task's declared shape". The resolved value flows into
    # both the subprocess timeout and the envelope's ``effective_timeout``.
    if args.timeout is None:
        effective_timeout = compute_implement_timeout(
            len(task["files"]),
            acceptance_criteria_count=len(task.get("acceptance_criteria", [])),
            test_command=task.get("test_command", ""),
            files=task.get("files", []),
            description=task.get("description", ""),
        )
    else:
        effective_timeout = args.timeout

    try:
        baseline = _snapshot_baseline(repo_root)
        codex = invoke_codex(
            prompt=prompt,
            workdir=repo_root,
            schema_path=str(IMPLEMENT_SCHEMA),
            output_path=output_path,
            timeout_sec=effective_timeout,
            sandbox=None,
        )
        _record_dropped_bytes(codex.get("dropped_bytes"))

        # Timeout handling (Appendix D F1): in-scope cleanup only; anything
        # outside allowed_files is observed for orchestrator reconciliation.
        # Never run `git checkout -- .` or `git clean -fd`.
        if codex["status"] == "timeout":
            cleanup_details = _handle_timeout_cleanup(
                repo_root, allowed_files, baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
                unattended_revert_policy=_resolve_unattended_revert_policy(args),
            )
            emit(make_envelope(
                task["task_id"], "implement", "timeout",
                exit_code=-1,
                raw=codex["stdout"] or codex["stderr"],
                error=f"Codex timed out after {effective_timeout}s",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    # TASK-004: surface ``_snapshot_baseline``'s error
                    # (truncated to 200 chars) so the orchestrator can
                    # route differently when baseline capture itself
                    # failed and cleanup was therefore skipped. ``None``
                    # when capture succeeded.
                    "baseline_error": baseline.get("error"),
                    "cleanup_details": cleanup_details,
                    "wrapper_events": [{
                        "event": "wrapper_autoclean_executed",
                        "restored": cleanup_details.get("restored_tracked", []),
                        "deleted": cleanup_details.get("deleted_untracked", []),
                        "authorization_source": "wrapper_internal_cleanup_explicit_declaration",
                    }] if cleanup_details.get("restored_tracked") or cleanup_details.get("deleted_untracked") else [],
                    "out_of_scope_tracked": cleanup_details.get(
                        "out_of_scope_tracked", []),
                    "out_of_scope_untracked": cleanup_details.get(
                        "out_of_scope_untracked", []),
                    "out_of_scope_observed": cleanup_details.get(
                        "out_of_scope_observed", False),
                },
            ))
            return 1

        if codex["status"] == "codex_not_found":
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                error="codex binary not found on PATH",
            ))
            return 1

        if codex["status"] == "output_schema_invalid":
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=-1,
                raw=codex["stderr"],
                error=codex["stderr"],
                extra={"effective_timeout": effective_timeout},
            ))
            return 1

        # Missing output file (Appendix D A2: exit code unreliable)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=codex["exit_code"],
                raw=codex["stderr"] or codex["stdout"],
                error="Codex produced no output file",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        output_text = Path(output_path).read_text(encoding="utf-8")
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as e:
            emit(make_envelope(
                task["task_id"], "implement", "parse_error",
                exit_code=codex["exit_code"],
                raw=output_text,
                error=f"Failed to parse Codex output as JSON: {e}",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        # Observe-only scope check (Appendix D F2: sandbox unreliable) against
        # pre-dispatch baseline. Wrapper never mutates out-of-scope; orchestrator
        # reconciles at batch boundary (Fix E).
        scope = validate_scope(repo_root, allowed_files, baseline)

        # scope_violation is gated on Codex's SELF-DECLARED scope. If Codex
        # reports `files_changed` containing paths outside `allowed_files`,
        # that is an explicit escape and the wrapper reports scope_violation.
        #
        # Observed-but-unreported out-of-scope writes (Codex didn't mention
        # them) are retained in scope.out_of_scope_{tracked,untracked} for
        # orchestrator reconciliation but do NOT fail the wrapper. Two
        # parallel sibling dispatches writing their own declared files race
        # into each other's post-dispatch diff, and under the pre-fix rule
        # every sibling flipped to scope_violation. The orchestrator has
        # whole-batch context to distinguish a sibling race from a silent
        # Codex escape; the wrapper does not.
        reported = {
            normalize_file_path(f) for f in parsed.get("files_changed", [])
        }
        reported_out_of_scope = sorted(
            f for f in reported
            if not _path_in_allowed_scope(f, allowed_files)
        )
        if reported_out_of_scope:
            emit(make_envelope(
                task["task_id"], "implement", "scope_violation",
                exit_code=codex["exit_code"],
                raw=output_text,
                parsed=parsed,
                error=(
                    f"Codex declared writes outside scope. "
                    f"Reported out-of-scope: {reported_out_of_scope}, "
                    f"observed out-of-scope tracked: {scope['out_of_scope_tracked']}, "
                    f"untracked: {scope['out_of_scope_untracked']}. "
                    f"Orchestrator will reconcile."
                ),
                extra={
                    "scope": scope,
                    "reported_out_of_scope": reported_out_of_scope,
                    "out_of_scope_tracked": scope["out_of_scope_tracked"],
                    "out_of_scope_untracked": scope["out_of_scope_untracked"],
                    "out_of_scope_observed": True,
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        # Codex status mapping (Appendix C.3)
        codex_status = parsed.get("status", "")
        if codex_status != "completed":
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=codex["exit_code"],
                raw=output_text,
                parsed=parsed,
                error=f"Codex reported status: {codex_status!r}",
                extra={
                    "scope": scope,
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        # Dishonesty check: compare reported files_changed against the
        # IN-SCOPE delta only. Out-of-scope observations (a parallel sibling's
        # own declared file appearing mid-dispatch, or a silent Codex escape)
        # live in scope["out_of_scope_*"] for orchestrator reconciliation and
        # are intentionally excluded here so sibling races do not false-flag
        # this task. Runs BEFORE the test command — misreport is fatal.
        actual_in_scope_new = set(scope["changed_in_scope_new"])
        actual_in_scope_all = set(scope["changed_in_scope"])
        undeclared = sorted(actual_in_scope_new - reported)
        phantom = sorted(reported - actual_in_scope_all)
        if undeclared or phantom:
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=codex["exit_code"],
                raw=output_text,
                parsed=parsed,
                error=(
                    "Codex misreported files_changed. "
                    f"Undeclared: {undeclared}, Phantom: {phantom}."
                ),
                extra={
                    "reason": "scope_misreport",
                    "scope": scope,
                    "undeclared_changes": undeclared,
                    "phantom_declarations": phantom,
                    "test_result": {
                        "result": "not_run",
                        "command": task["test_command"],
                        "details": "skipped due to scope_misreport",
                    },
                    "jsonl_file_changes": codex["file_changes"],
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        # Independent test re-run (Appendix D-aligned)
        test_result = run_test_command(
            task["test_command"],
            repo_root,
            timeout_sec=TEST_TIMEOUT,
        )
        if test_result["result"] == "deferred":
            event_fields = {
                "task_id": task["task_id"],
                "deferred_to": test_result["deferred_to"],
            }
            if test_result.get("note"):
                event_fields["note"] = test_result["note"]
            _append_run_log("test_deferred", event_fields)
        if test_result["result"] == "failed":
            # TASK-008 (POSTMORTEM_FIXES): surface sandbox stdout/stderr
            # so the orchestrator's auto-validate branch can distinguish
            # "real test red" from "sandbox missing dep / permission /
            # path divergence" before classifying the failure. Each
            # stream is capped at SANDBOX_TEST_CAPTURE_CAP bytes; when
            # truncation fires the original byte length is recorded as
            # `sandbox_test_*_truncated_to` (the wrapper's
            # `truncated_to` marker convention).
            sandbox_stdout, stdout_truncated = _truncate_stream(
                test_result.get("stdout") or "",
            )
            sandbox_stderr, stderr_truncated = _truncate_stream(
                test_result.get("stderr") or "",
            )
            extra: dict = {
                "cause": "independent_test_run_failed",
                "scope": scope,
                "test_result": test_result,
                "undeclared_changes": undeclared,
                "phantom_declarations": phantom,
                "jsonl_file_changes": codex["file_changes"],
                "wall_seconds": codex["wall_seconds"],
                "effective_timeout": effective_timeout,
                "sandbox_test_stdout": sandbox_stdout,
                "sandbox_test_stderr": sandbox_stderr,
                "sandbox_test_command": test_result.get("command"),
                "sandbox_test_exit_code": test_result.get("exit_code"),
                "sandbox_test_attempt_count": test_result.get("attempts"),
            }
            if stdout_truncated is not None:
                extra["sandbox_test_stdout_truncated_to"] = stdout_truncated
            if stderr_truncated is not None:
                extra["sandbox_test_stderr_truncated_to"] = stderr_truncated
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=codex["exit_code"],
                raw=output_text,
                parsed=parsed,
                error=(
                    f"Independent test run failed after "
                    f"{test_result['attempts']} attempt(s)"
                ),
                extra=extra,
            ))
            return 1

        emit(make_envelope(
            task["task_id"], "implement", "success",
            exit_code=codex["exit_code"],
            raw=output_text,
            parsed=parsed,
            extra={
                "scope": scope,
                "test_result": test_result,
                "undeclared_changes": undeclared,
                "phantom_declarations": phantom,
                "jsonl_file_changes": codex["file_changes"],
                "wall_seconds": codex["wall_seconds"],
                "effective_timeout": effective_timeout,
            },
        ))
        return 0
    finally:
        try:
            os.unlink(output_path)
        except (FileNotFoundError, OSError):
            pass


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

    # TASK-004: file-count-aware default. ``len(review_files)`` after
    # ``--files`` parsing is the file count Codex must read. Operator
    # override via ``--timeout`` short-circuits scaling.
    if args.timeout is None:
        effective_timeout = compute_review_timeout(len(review_files))
    else:
        effective_timeout = args.timeout

    diff = git_diff_for_files(repo_root, review_files, include_untracked=True)
    # TASK-007: forward --target-task-id (shared rule with cmd_implement).
    target_task_id = getattr(args, "target_task_id", None)
    try:
        prompt = render_review_prompt(
            task, diff, args.review_focus, review_files,
            plan_text=plan_text,
            target_task_id=target_task_id,
            plan_file=str(plan_path),
        )
    except plan_ops.MissingTargetTaskIdError as e:
        emit(make_envelope(
            args.task_id, "review", "failure",
            error=f"Missing target_task_id: {e}",
            extra={"plan_file": e.plan_file, "heading_count": e.heading_count},
        ))
        return 1

    if args.dry_run:
        emit({
            "task_id": task["task_id"],
            "subcommand": "review",
            "outcome": "dry_run",
            "dry_run": True,
            "files_under_review": review_files,
            "diff_size_bytes": len(diff),
            "review_focus": args.review_focus,
            "prompt_preview": prompt,
            "effective_timeout": effective_timeout,
        })
        return 0

    if not REVIEW_SCHEMA.exists():
        emit(make_envelope(
            task["task_id"], "review", "failure",
            error=f"Schema file missing: {REVIEW_SCHEMA}",
        ))
        return 1

    tmp_out = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="codex_review_",
    )
    tmp_out.close()
    output_path = tmp_out.name

    try:
        baseline = _snapshot_baseline(repo_root)
        codex = invoke_codex(
            prompt=prompt,
            workdir=repo_root,
            schema_path=str(REVIEW_SCHEMA),
            output_path=output_path,
            timeout_sec=effective_timeout,
            sandbox="read-only",  # Advisory (Appendix D F2)
        )
        _record_dropped_bytes(codex.get("dropped_bytes"))

        if codex["status"] == "timeout":
            cleanup_details = _handle_timeout_cleanup(
                repo_root, review_files, baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
                unattended_revert_policy=_resolve_unattended_revert_policy(args),
            )
            emit(make_envelope(
                task["task_id"], "review", "timeout",
                exit_code=-1,
                raw=codex["stdout"] or codex["stderr"],
                error=f"Codex review timed out after {effective_timeout}s",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    "cleanup_details": cleanup_details,
                    "wrapper_events": [{
                        "event": "wrapper_autoclean_executed",
                        "restored": cleanup_details.get("restored_tracked", []),
                        "deleted": cleanup_details.get("deleted_untracked", []),
                        "authorization_source": "wrapper_internal_cleanup_explicit_declaration",
                    }] if cleanup_details.get("restored_tracked") or cleanup_details.get("deleted_untracked") else [],
                    "out_of_scope_tracked": cleanup_details.get(
                        "out_of_scope_tracked", []),
                    "out_of_scope_untracked": cleanup_details.get(
                        "out_of_scope_untracked", []),
                    "out_of_scope_observed": cleanup_details.get(
                        "out_of_scope_observed", False),
                },
            ))
            return 1

        if codex["status"] == "codex_not_found":
            emit(make_envelope(
                task["task_id"], "review", "failure",
                error="codex binary not found on PATH",
            ))
            return 1

        if codex["status"] == "output_schema_invalid":
            emit(make_envelope(
                task["task_id"], "review", "failure",
                exit_code=-1,
                raw=codex["stderr"],
                error=codex["stderr"],
                extra={"effective_timeout": effective_timeout},
            ))
            return 1

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            emit(make_envelope(
                task["task_id"], "review", "failure",
                exit_code=codex["exit_code"],
                raw=codex["stderr"] or codex["stdout"],
                error="Codex produced no output file",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        output_text = Path(output_path).read_text(encoding="utf-8")
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as e:
            emit(make_envelope(
                task["task_id"], "review", "parse_error",
                exit_code=codex["exit_code"],
                raw=output_text,
                error=f"Failed to parse Codex output as JSON: {e}",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        # Post-review scope check: Codex should not have written anything.
        # Observe-only against the pre-dispatch baseline — sibling dispatches
        # cannot delete each other's untracked work because no mutation
        # happens here. Review keeps "log but succeed" semantics by explicit
        # design (TASK-003 targets cleanup safety, not detection severity);
        # any sandbox escape surfaces via extra.sandbox_escape_detected and
        # the orchestrator reconciles out-of-scope writes at batch boundary.
        scope = validate_scope(repo_root, review_files, baseline)
        sandbox_escape_detected = bool(
            scope["out_of_scope_observed"]
            or scope["protected_skipped_tracked"]
            or scope["protected_skipped_untracked"]
        )

        extra: dict = {
            "wall_seconds": codex["wall_seconds"],
            "effective_timeout": effective_timeout,
            "scope": scope,
            "sandbox_escape_detected": sandbox_escape_detected,
            "out_of_scope_tracked": scope["out_of_scope_tracked"],
            "out_of_scope_untracked": scope["out_of_scope_untracked"],
            "out_of_scope_observed": scope["out_of_scope_observed"],
            "cleanup_strategy": scope["cleanup_strategy"],
            "baseline_captured": baseline["captured"],
        }
        if codex["file_changes"]:
            extra["jsonl_file_changes"] = codex["file_changes"]

        # TASK-027A: hallucinated-symbol post-check. Wrapper-envelope
        # metadata only — does NOT mutate `parsed` (Codex output contract).
        extra["wrapper_checks"] = {
            "symbol_warnings": _verify_cited_symbols(parsed, repo_root),
        }

        emit(make_envelope(
            task["task_id"], "review", "success",
            exit_code=codex["exit_code"],
            raw=output_text,
            parsed=parsed,
            extra=extra,
        ))
        return 0
    finally:
        try:
            os.unlink(output_path)
        except (FileNotFoundError, OSError):
            pass


# ---------------------------------------------------------------------------
# Subcommand: plan-review
# ---------------------------------------------------------------------------


def cmd_plan_review(args) -> int:
    """Phase 1.5 — Codex independently reviews the persisted schedule.

    Schedule-only (TASK-006, finalized in TASK-008). The plan markdown is
    no longer read or rendered into the prompt; with the fat manifest
    from TASK-004, every per-task `description` + `acceptance_criteria`
    the reviewer needs lives inside the schedule JSON directly.
    ``--schedule-file`` is the sole required input. TASK-008 removed the
    deprecated ``--plan-file`` / ``--plans-dir`` flags entirely.

    Verdict vocabulary: approved | approved-with-notes | needs-replan.
    Wrapper owns the sandbox baseline + cleanup, matching implement/review.
    """
    schedule_path = Path(args.schedule_file).resolve()
    repo_root = str(Path(args.repo_root).resolve())

    if not schedule_path.exists():
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=f"Schedule file not found: {schedule_path}",
        ))
        return 1

    # `plan_basename` populates envelope.plan_file (the schema still carries
    # this identifier so downstream triage/author routing can look up the
    # plan). Preferred source: the schedule's own parent-directory basename,
    # which for directory-mode sidecars (``<plan_dir>/<stem>.schedule.json``
    # per ``_plan_paths.py:69``) IS the plan-directory basename. For plans
    # outside the directory-mode layout we fall back to the schedule file's
    # own stem.
    #
    # Directory-mode signal: the sibling ``00_INDEX.json`` roster, which
    # plan_ops writes as the canonical marker for decomposed plans. Keying
    # off that file (rather than a ``parent_name == sidecar_stem`` match)
    # preserves the plan-directory identity when the sidecar has been
    # renamed away from its parent directory (e.g., a copied/renamed plan
    # directory still containing ``directory_mode_plan.schedule.json``).
    parent_dir = schedule_path.parent
    if (parent_dir / "00_INDEX.json").is_file() and parent_dir.name:
        plan_basename = parent_dir.name
    elif schedule_path.name.endswith(".schedule.json"):
        # File-mode fallback: strip the full ``.schedule.json`` double
        # suffix (``Path.stem`` only peels ``.json``, leaving ``.schedule``).
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

    # Validate schedule JSON up-front so we surface a clean parse error
    # before dispatching Codex on garbage.
    try:
        schedule_obj = json.loads(schedule_text)
    except json.JSONDecodeError as exc:
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=f"Schedule file is not valid JSON: {exc}",
        ))
        return 1

    # TASK-003: severity-aware demotion under --allow-gaps. The gating
    # predicate now lives in `_plan_paths._should_inject_allow_gaps_demotion`
    # so this wrapper and `plan_gemini_dispatch.py` cannot drift on the
    # injection condition. We do NOT mutate the persisted schedule; the
    # signal flows into the prompt only.
    allow_gaps_demotion = _should_inject_allow_gaps_demotion(
        schedule_obj, bool(getattr(args, "allow_gaps", False)),
    )

    prompt = render_plan_review_prompt(
        schedule_text,
        plan_basename,
        allow_gaps_demotion=allow_gaps_demotion,
    )

    # TASK-004: plan-review keeps a flat default — the schedule is
    # bounded so per-task scaling does not apply. Operator override via
    # ``--timeout`` still wins; resolve the value once so it flows into
    # both the subprocess timeout and ``effective_timeout`` envelope
    # field.
    if args.timeout is None:
        effective_timeout = DEFAULT_TIMEOUT_PLAN_REVIEW
    else:
        effective_timeout = args.timeout

    if args.dry_run:
        emit({
            "plan_file": plan_basename,
            "subcommand": "plan-review",
            "outcome": "dry_run",
            "dry_run": True,
            "prompt_preview": prompt,
            "effective_timeout": effective_timeout,
        })
        return 0

    if not PLAN_REVIEW_SCHEMA.exists():
        emit(make_envelope(
            "plan", "plan-review", "failure",
            error=f"Schema file missing: {PLAN_REVIEW_SCHEMA}",
        ))
        return 1

    tmp_out = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="codex_plan_review_",
    )
    tmp_out.close()
    output_path = tmp_out.name

    try:
        baseline = _snapshot_baseline(repo_root)
        codex = invoke_codex(
            prompt=prompt,
            workdir=repo_root,
            schema_path=str(PLAN_REVIEW_SCHEMA),
            output_path=output_path,
            timeout_sec=effective_timeout,
            sandbox="read-only",  # Advisory (Appendix D F2); plan review reads only
        )
        _record_dropped_bytes(codex.get("dropped_bytes"))

        if codex["status"] == "timeout":
            # No allowed-files list for plan review — pass empty list so any
            # observed write lands in out_of_scope_* for orchestrator visibility.
            cleanup_details = _handle_timeout_cleanup(
                repo_root, [], baseline,
                authorization_source="wrapper_internal_cleanup_explicit_declaration",
                unattended_revert_policy=_resolve_unattended_revert_policy(args),
            )
            emit(make_envelope(
                "plan", "plan-review", "timeout",
                exit_code=-1,
                raw=codex["stdout"] or codex["stderr"],
                error=f"Codex plan review timed out after {effective_timeout}s",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                    "cleanup_strategy": cleanup_details["cleanup_strategy"],
                    "baseline_captured": baseline["captured"],
                    "cleanup_details": cleanup_details,
                    "wrapper_events": [{
                        "event": "wrapper_autoclean_executed",
                        "restored": cleanup_details.get("restored_tracked", []),
                        "deleted": cleanup_details.get("deleted_untracked", []),
                        "authorization_source": "wrapper_internal_cleanup_explicit_declaration",
                    }] if cleanup_details.get("restored_tracked") or cleanup_details.get("deleted_untracked") else [],
                    "out_of_scope_tracked": cleanup_details.get(
                        "out_of_scope_tracked", []),
                    "out_of_scope_untracked": cleanup_details.get(
                        "out_of_scope_untracked", []),
                    "out_of_scope_observed": cleanup_details.get(
                        "out_of_scope_observed", False),
                },
            ))
            return 1

        if codex["status"] == "codex_not_found":
            emit(make_envelope(
                "plan", "plan-review", "failure",
                error="codex binary not found on PATH",
            ))
            return 1

        if codex["status"] == "output_schema_invalid":
            emit(make_envelope(
                "plan", "plan-review", "failure",
                exit_code=-1,
                raw=codex["stderr"],
                error=codex["stderr"],
                extra={"effective_timeout": effective_timeout},
            ))
            return 1

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raw_parts = []
            if codex["stdout"]:
                raw_parts.append(f"[stdout]\n{codex['stdout']}")
            if codex["stderr"]:
                raw_parts.append(f"[stderr]\n{codex['stderr']}")
            emit(make_envelope(
                "plan", "plan-review", "failure",
                exit_code=codex["exit_code"],
                raw="\n".join(raw_parts),
                error="Codex produced no output file",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        output_text = Path(output_path).read_text(encoding="utf-8")
        try:
            parsed = json.loads(output_text)
        except json.JSONDecodeError as e:
            emit(make_envelope(
                "plan", "plan-review", "parse_error",
                exit_code=codex["exit_code"],
                raw=output_text,
                error=f"Failed to parse Codex output as JSON: {e}",
                extra={
                    "wall_seconds": codex["wall_seconds"],
                    "effective_timeout": effective_timeout,
                },
            ))
            return 1

        # Observe-only post-dispatch scope: Codex should not have written
        # anything. Pass empty allowed set; any new delta becomes out-of-scope.
        scope = validate_scope(repo_root, [], baseline)
        sandbox_escape_detected = bool(
            scope["out_of_scope_observed"]
            or scope["protected_skipped_tracked"]
            or scope["protected_skipped_untracked"]
        )

        extra: dict = {
            "wall_seconds": codex["wall_seconds"],
            "effective_timeout": effective_timeout,
            "scope": scope,
            "sandbox_escape_detected": sandbox_escape_detected,
            "out_of_scope_tracked": scope["out_of_scope_tracked"],
            "out_of_scope_untracked": scope["out_of_scope_untracked"],
            "out_of_scope_observed": scope["out_of_scope_observed"],
            "cleanup_strategy": scope["cleanup_strategy"],
            "baseline_captured": baseline["captured"],
        }
        if codex["file_changes"]:
            extra["jsonl_file_changes"] = codex["file_changes"]

        envelope = make_envelope(
            "plan", "plan-review", "success",
            exit_code=codex["exit_code"],
            raw=output_text,
            parsed=parsed,
            extra=extra,
        )
        # Override the default `task_id` field with `plan_file` for plan-review
        # envelopes; keeps the contract distinct from implement/review.
        envelope["plan_file"] = plan_basename
        emit(envelope)
        return 0
    finally:
        try:
            os.unlink(output_path)
        except (FileNotFoundError, OSError):
            pass


# ---------------------------------------------------------------------------
# Main / argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Codex CLI dispatch wrapper for the dual-agent plan executor. "
            "Subcommands: implement, review, plan-review."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--plan-file", required=True,
                       help="Absolute path to plan document")
        p.add_argument("--task-id", required=True,
                       help="Task ID within the plan (e.g. 1, 001, TASK-001)")
        p.add_argument("--repo-root", required=True,
                       help="Absolute path to the repo root passed as `codex -C`")
        p.add_argument("--json", action="store_true",
                       help=("Output structured JSON (always on; flag is a "
                             "no-op reserved for future-compat)"))
        p.add_argument("--dry-run", action="store_true",
                       help="Render prompt and metadata; do not invoke Codex")
        # ``--timeout`` defaults to ``None`` so the cmd handler can derive
        # a wrapper default from the parsed task/review shape when the
        # operator does not override.
        p.add_argument("--timeout", type=int, default=None,
                       help=("Codex execution timeout in seconds "
                             "(default: task-shape-aware for implement; "
                             "max(180, 30 * len(files)) for review). "
                             "Pass an explicit value to override the "
                             "wrapper-derived default."))
        # TASK-007: first-class target_task_id field for shared-file
        # children. When the resolved child plan file declares >1
        # `### TASK-NNN:` H3 heading, the renderer auto-injects the
        # "Implement specifically `### TASK-NNN:`" disambiguator line.
        # When the file declares exactly 1 heading the field is a no-op.
        # When the file declares >1 heading AND this flag is absent, the
        # renderer raises a structured MissingTargetTaskIdError.
        p.add_argument(
            "--target-task-id", default=None,
            help=(
                "Disambiguator for shared-file children. When the child "
                "plan file carries >1 `### TASK-NNN:` H3 heading, the "
                "renderer auto-injects 'Implement specifically `### "
                "TASK-NNN:`' as the first instruction line. Required "
                "for shared-file children (omitting it when >1 heading "
                "is declared raises a structured renderer error)."
            ),
        )
        # TASK-004 (Phase D, layer 4): optional content-sanitizer subagent.
        # When set AND ``extra.sanitizer_flags`` is non-empty on an emitted
        # envelope, the wrapper dispatches the unprivileged
        # ``content-sanitizer`` agent (verdict-only, no payload) and stamps
        # ``extra.content_sanitizer_verdict``. Dispatch failures degrade to
        # ``{"status":"error","reason":...}`` so routing continues.
        p.add_argument(
            "--content-sanitizer-check", action="store_true",
            help=(
                "Enable the optional content-sanitizer subagent. When set, "
                "envelopes whose extra.sanitizer_flags is non-empty are "
                "passed to the unprivileged content-sanitizer agent for "
                "verdict-only intent classification "
                "(extra.content_sanitizer_verdict). The orchestrator never "
                "sees the suspect text. Dispatch failures degrade to "
                "{status:'error', reason:...}; routing continues."
            ),
        )
        # PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002: cross-wrapper revert
        # policy. Optional; when omitted the wrapper falls back to
        # ``$UNATTENDED_REVERT_POLICY`` (env, set by Phase 0 preflight) and
        # ultimately to the safer-by-default ``pause`` semantics inside
        # the cleanup helper.
        p.add_argument(
            "--unattended-revert-policy",
            choices=["pause", "fail-fast", "preserve-only"],
            default=None,
            help=(
                "Cross-wrapper revert policy mirrored across the three "
                "dispatch wrappers. When omitted, the wrapper reads "
                "$UNATTENDED_REVERT_POLICY; if neither is set, falls "
                "back to the safer-by-default 'pause' behavior."
            ),
        )

    impl = subparsers.add_parser(
        "implement", help="Dispatch an implementation task to Codex",
    )
    add_common(impl)

    rev = subparsers.add_parser(
        "review", help="Dispatch a review task to Codex",
    )
    add_common(rev)
    rev.add_argument("--files", default="",
                     help="Comma-separated list of files to review "
                          "(default: task's Files list)")
    rev.add_argument("--review-focus", default="bugs",
                     choices=["bugs", "regressions", "security", "tests"],
                     help="Review focus area (default: bugs)")

    # plan-review subcommand: Phase 1.5 pre-dispatch plan-level review.
    # Schedule-only as of TASK-006, finalized in TASK-008 — the reviewer
    # reads the persisted schedule JSON (including the fat manifest's
    # per-task description + acceptance_criteria) and NEVER the plan
    # markdown. Verdict vocab is (approved | approved-with-notes |
    # needs-replan), so it is a sibling subcommand rather than a mode of
    # `review`.
    pr = subparsers.add_parser(
        "plan-review",
        help="Dispatch a plan-level review to Codex (Phase 1.5, schedule-only)",
    )
    pr.add_argument("--schedule-file", required=True,
                    help="Absolute path to persisted schedule JSON. Sole "
                         "required input — schedule carries the fat manifest "
                         "(per-task description + acceptance_criteria).")
    pr.add_argument("--repo-root", required=True,
                    help="Absolute path to the repo root passed as `codex -C`. "
                         "Required — plans typically live in a subdirectory "
                         "(e.g. docs/plans/…), so the plan's parent is NOT a "
                         "safe default for sandbox baseline/cleanup.")
    pr.add_argument("--json", action="store_true",
                    help=("Output structured JSON (always on; flag is a "
                          "no-op reserved for future-compat)"))
    pr.add_argument("--dry-run", action="store_true",
                    help="Render prompt and metadata; do not invoke Codex")
    pr.add_argument(
        "--unattended-revert-policy",
        choices=["pause", "fail-fast", "preserve-only"],
        default=None,
        help=(
            "Cross-wrapper revert policy mirrored across the three "
            "dispatch wrappers (PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002)."
        ),
    )
    # TASK-004: plan-review keeps a flat default (the schedule is
    # bounded so per-task scaling does not apply). Default is ``None``
    # for parity with implement/review; ``cmd_plan_review`` resolves
    # the unset case to ``DEFAULT_TIMEOUT_PLAN_REVIEW``.
    pr.add_argument("--timeout", type=int, default=None,
                    help=(f"Codex execution timeout in seconds "
                          f"(default: {DEFAULT_TIMEOUT_PLAN_REVIEW}, "
                          f"flat — plan-review is bounded). Pass an "
                          f"explicit value to override."))
    # TASK-004 (Phase D, layer 4): mirror the implement/review flag on
    # plan-review so every Codex dispatch entry point honors the operator's
    # opt-in to the content-sanitizer subagent gate.
    pr.add_argument(
        "--content-sanitizer-check", action="store_true",
        help=(
            "Enable the optional content-sanitizer subagent. See "
            "implement/review subcommands for the full contract."
        ),
    )
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
    # TASK-004 (Phase D, layer 4): flip the module-level gate from the
    # parsed CLI flag so ``emit()`` activates the content-sanitizer
    # dispatch on every envelope from this run.
    _set_content_sanitizer_check(
        bool(getattr(args, "content_sanitizer_check", False))
    )
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
