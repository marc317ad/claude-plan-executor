#!/usr/bin/env python3
"""Plan operations CLI for the /implement-plan workflow.

Scaffolding: argparse surface + shared helpers + subcommand stubs.
Stdlib-only (plan docs are pure markdown per DUAL_AGENT_PLAN_EXECUTOR.md §5).

Example invocations (resolve $PYTHON via `preflight --json`'s python_path):

    $PYTHON scripts/plan_ops.py preflight --plan-file <abs> [--strict-branch] [--strict-scope]
    $PYTHON scripts/plan_ops.py parse-schedule --stdin
    $PYTHON scripts/plan_ops.py compute-schedule --stdin
    $PYTHON scripts/plan_ops.py batch-next --schedule-file <path> ...
    $PYTHON scripts/plan_ops.py parse-implementer-report --stdin
    $PYTHON scripts/plan_ops.py parse-plan-review-report --stdin
    $PYTHON scripts/plan_ops.py commit-task --plan-file <abs> --task-id NNN ...
    $PYTHON scripts/plan_ops.py fail-task --plan-file <abs> --task-id NNN ...
    $PYTHON scripts/plan_ops.py update-plan-header --plan-file <abs> --status <s>
    $PYTHON scripts/plan_ops.py finalize-execution-log --plan-file <abs> ...
    $PYTHON scripts/plan_ops.py log-event --event E --fields-json '{...}'
    $PYTHON scripts/plan_ops.py normalize-task-id --id <1|001|TASK-001>
    $PYTHON scripts/plan_ops.py acquire-lock --plan-file <abs> --run-id RID
    $PYTHON scripts/plan_ops.py release-lock --plan-file <abs> --run-id RID
    $PYTHON scripts/plan_ops.py block-dependents --schedule-file <path> --plan-file <abs> --failed NNN --run-id RID
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Ensure the sibling ``_plan_paths`` module is importable when this file is
# loaded via ``importlib.util.spec_from_file_location`` (e.g., from tests).
# Direct CLI invocation already adds the script's directory to ``sys.path``.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _plan_paths import (  # noqa: E402
    COMMIT_ALWAYS_IGNORE,
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
    canonicalize_file,
    is_commit_always_ignore,
    is_protected_path,
)

def _load_plan_config() -> dict:
    """Read `.claude/plan-executor.json` from cwd; return {} if missing.

    Config schema (all optional):
        { "plan_dir": "docs/plans" }

    Missing file → defaults to {} so projects without the config keep working.
    Malformed JSON or unreadable file → SystemExit(2). The config gates every
    plan operation; silently falling back to defaults on a typo would route
    writes to the wrong directory.
    """
    cfg_path = Path(".claude/plan-executor.json")
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"plan_ops: malformed JSON in {cfg_path}: {exc}\n"
        )
        raise SystemExit(2)
    except OSError as exc:
        sys.stderr.write(
            f"plan_ops: cannot read {cfg_path}: {exc}\n"
        )
        raise SystemExit(2)


_PLAN_CFG = _load_plan_config()
PLAN_DIR = Path(_PLAN_CFG.get("plan_dir", "docs/plans"))
_PLAN_DIR_POSIX = PLAN_DIR.as_posix()
RUN_LOG_PATH = PLAN_DIR / "_run_log.jsonl"
RUN_LOCK_PATH = PLAN_DIR / "_run_lock.json"

TASK_HEADER_RE = re.compile(r"^### TASK-(\d{3}[A-Z]?):", re.MULTILINE)
TASK_ID_INPUT_RE = re.compile(r"^(?:TASK-)?(\d{1,3})([A-Z]?)$")
STATUS_BULLET_RE = re.compile(r"^(\s*-\s*\*\*Status:\*\*)\s*(.+?)\s*$", re.MULTILINE)
DEPENDENCIES_BULLET_RE = re.compile(
    r"^\s*-\s*\*\*Dependencies:\*\*\s*(.+?)\s*$", re.MULTILINE,
)
ALLOWED_TASK_STATUSES = {"pending", "open", "in-progress", "done", "failed", "blocked", "skipped"}
ALLOWED_INDEX_STATUSES = {"Done", "Pending", "Superseded"}
_INDEX_SUPERSEDED_BY: dict[str, list[str]] = {}
STATUS_ALIASES = {"open": "pending"}
SCHEDULE_FIELD_ALIASES = {"task_id": "id", "batch_index": "index"}
ALLOWED_PLAN_STATUSES = {"in-progress", "complete", "partial"}
ALLOWED_FAIL_STAGES = {"implement", "review", "commit"}
ALLOWED_CODEX_REVIEW_VERDICTS = {"clean", "minor-findings", "needs-rework"}
ALLOWED_CLAUDE_REVIEW_VERDICTS = {
    "ship",
    "ship-with-fixes",
    "partial-agreement",
    "needs-rework",
}
# Phase 1.5 Codex plan-review verdicts (TASK-014C). Distinct from the
# code-level review verdicts above because a plan review operates on plan
# markdown + schedule JSON, not a diff, and drives a different routing table
# (see SKILL.md §Phase 1.5).
ALLOWED_PLAN_REVIEW_VERDICTS = {"approved", "approved-with-notes", "needs-replan"}
ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES = {"critical", "important", "minor"}
# TASK-019: reviewer-minor-finding optional disposition vocabulary. Allows the
# D.5 adjudicator to attach a structured "what happened to this finding"
# annotation without stuffing it into the prose of `issue`/`suggested_fix`.
ALLOWED_REVIEWER_FINDING_DISPOSITIONS = {
    "dismissed",
    "accepted",
    "deferred",
    # TASK-022: `spec-deference` marks a Codex finding that has design merit
    # but contradicts the plan's explicit spec (acceptance criteria or
    # Implementation Playbook). D.5 uses it instead of `dismissed` so the
    # critique surfaces for a future plan-review pass rather than being
    # silently buried under "plan says so" reasoning.
    "spec-deference",
}
OPTIONAL_REVIEWER_FINDING_FIELDS = {"disposition", "disposition_reason"}
ALLOWED_ROW_FIELDS = {"task", "agent", "reviewer", "verdict", "commit", "notes"}
# Known run-log event types. The orchestrator owns the vocabulary; this set
# acts as a tripwire so typo'd events surface immediately rather than drifting
# silently into the log. `awaiting_user` is added per TASK-014A D.2a.5 to
# signal the bounded-remediation pause state to the next conversation turn.
ALLOWED_LOG_EVENTS = {
    "run_start",
    "run_end",
    "analyst_done",
    "batch_start",
    "implement_start",
    "implement_done",
    "review_start",
    "review_done",
    "commit_done",
    "failed",
    "disagreement",
    "fallback_used",
    "review_skipped",
    "remediation_start",
    # `narrow_remediation_start` / `narrow_remediation_done` are added per
    # TASK-016C D.2a.6. Distinct from `remediation_start` so the run log is
    # the audit source of truth for which retry path fired (D.2a.5 full
    # rework vs D.2a.6 narrow scope).
    "narrow_remediation_start",
    "narrow_remediation_done",
    "plan_review_start",
    "plan_review_done",
    "plan_review_skipped",
    # `plan_author_start` / `plan_author_done` are added per TASK-025 for the
    # needs-replan auto-revise path: when Codex returns `needs-replan` on the
    # first plan-review pass and auto-revise is on, the orchestrator dispatches
    # `plan-author` to apply the findings to the plan file in place before the
    # second binding review.
    "plan_author_start",
    "plan_author_done",
    "awaiting_user",
    "schedule_written",
    # TASK-020B: `acceptance_v_check` runtime enforcement. `v_check_passed`
    # is emitted by `cmd_commit_task` when a plan's opt-in YAML frontmatter
    # V-check succeeds pre-commit. `v_check_failed` is reserved for direct
    # `log-event` calls by external tooling (e.g., future CI jobs); v1
    # `cmd_commit_task` does NOT emit it — a failed V-check `_die`s silently
    # to preserve the "no run-log mutation on failure" invariant.
    "v_check_passed",
    "v_check_failed",
}
# Accepted values for `finalize-execution-log --outcome`. `paused` is added
# per TASK-014A for the D.2a.5 awaiting-user pause — the run halted mid-flight
# and the user's next turn decides disposition.
ALLOWED_RUN_OUTCOMES = {"success", "partial", "failed", "paused"}

# TASK-007: Canonical Contract decision table. Self-audit (`cmd_audit`)
# compares the shipped artifacts against this table and surfaces drift as
# structured findings. Keep this dict the single source of truth — the
# checks below import from it; the design doc §9.7 / §14 references it by
# name. ALIAS_WINDOWS names symbols / fields that callers may still see
# during a deprecation window; a check returns `pass_with_alias` (not
# `fail`) when the actual set is `canonical | aliases`.
CANONICAL_CONTRACT: dict[str, object] = {
    "status_vocabulary": [
        "pending", "in-progress", "done", "failed", "blocked", "skipped",
    ],
    "schedule_task_field": "id",
    "schedule_batch_field": "index",
    "implementer_concerns_label": "**Concerns for reviewer:**",
    "implementer_plan_adaptations_label": "**Plan adaptations:**",
    "execution_log_columns": [
        "Task", "Agent", "Reviewer", "Verdict", "Commit", "Notes",
    ],
    "wrapper_always_ignore_paths": [
        "docs/plans/_run_log.jsonl",
        "docs/plans/_run_lock.json",
    ],
    "wrapper_always_ignore_globs": [
        "docs/plans/*.schedule.json",
    ],
}

ALIAS_WINDOWS: dict[str, list[str]] = {
    # `open` is the legacy task-status name for `pending` (`STATUS_ALIASES`
    # in this module). Listed so the audit reports `pass_with_alias` rather
    # than masking the alias as silent tolerance.
    "status_vocabulary": ["open"],
    # `task_id` / `batch_index` are accepted alongside `id` / `index` in
    # `_validate_schedule` (`SCHEDULE_FIELD_ALIASES`); the parser warns
    # but does not error.
    "schedule_task_field": ["task_id"],
    "schedule_batch_field": ["batch_index"],
    # The pre-canonical implementer-report label was the bare `**Concerns:**`;
    # `cmd_parse_implementer_report` still falls back to it with a warning.
    "implementer_concerns_label": ["**Concerns:**"],
}

CANONICAL_ID_RE = re.compile(r"^\d{3}[A-Z]?$")
ALLOWED_SCHEDULE_TOP_LEVEL = {"outcome", "tasks", "batches", "gaps", "risks"}
ALLOWED_TASK_FIELDS = {
    "id", "task_id",
    "title", "agent", "priority", "files", "dependencies",
    "test_command", "classification_reason", "acceptance_criteria",
    "plan_file",
}
ALLOWED_BATCH_FIELDS = {"index", "batch_index", "task_ids", "file_locks"}
# TASK-002: formalize hard-vs-soft gap severity. Hard gaps block execution
# without explicit operator override; soft gaps are advisory and can be
# demoted to warnings by --allow-gaps (TASK-003). Unknown gap types default
# to "hard" as a fail-safe — better to over-block than silently allow an
# unclassified gap to flow through.
GAP_SEVERITY = {
    "stale-path": "hard",
    "missing-test-command": "hard",
    "vague-ac": "hard",
    "unresolvable-test": "soft",
    "empty-implementation-notes": "soft",
}


def classify_gap_severity(gap_type: str) -> str:
    """Return "hard" or "soft" for a gap type. Unknown types → "hard"."""
    return GAP_SEVERITY.get(gap_type, "hard")


def _is_valid_plan_file_basename(value: str) -> bool:
    if not value:
        return False
    if value.startswith("."):
        return False
    if "/" in value or "\\" in value:
        return False
    if ".." in value:
        return False
    if "\x00" in value:
        return False
    return len(value.encode("utf-8")) <= 255


PRIORITY_RANKS = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _task_order_key(task_id: str, priority: str) -> tuple[int, int, str]:
    normalized = _normalize_task_id(task_id)
    if normalized is None:
        return (PRIORITY_RANKS["low"], 999, "Z")
    match = re.match(r"^(\d{3})([A-Z]?)$", normalized)
    assert match is not None
    return (
        PRIORITY_RANKS.get(priority, PRIORITY_RANKS["low"]),
        int(match.group(1)),
        match.group(2),
    )


def _compute_schedule_batches(tasks: list) -> tuple[list[str], list[dict], list[dict]]:
    errors: list[dict] = []
    normalized_tasks: list[dict] = []
    seen_ids: dict[str, int] = {}

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append({
                "path": f"$.tasks[{i}]",
                "code": "invalid-type",
                "message": f"tasks[{i}] must be an object",
            })
            continue

        raw_id = task.get("id") if "id" in task else task.get("task_id")
        task_id = _normalize_task_id(raw_id)
        if task_id is None:
            errors.append({
                "path": f"$.tasks[{i}].id",
                "code": "invalid-task-id",
                "message": f"tasks[{i}].id={raw_id!r} is not a valid task id",
            })
            continue
        if task_id in seen_ids:
            errors.append({
                "path": f"$.tasks[{i}].id",
                "code": "duplicate-task-id",
                "message": f"duplicate id {task_id!r} (first at tasks[{seen_ids[task_id]}])",
            })
            continue
        seen_ids[task_id] = i

        raw_files = task.get("files") or []
        if not isinstance(raw_files, list):
            errors.append({
                "path": f"$.tasks[{i}].files",
                "code": "invalid-type",
                "message": "files must be an array",
            })
            continue

        priority = str(task.get("priority", "low")).strip().lower() or "low"
        if priority not in PRIORITY_RANKS:
            priority = "low"

        normalized_tasks.append({
            "id": task_id,
            "priority": priority,
            "files": [str(path) for path in raw_files],
        })

    if errors:
        return [], [], errors

    ordered_tasks = sorted(
        normalized_tasks,
        key=lambda task: _task_order_key(task["id"], task["priority"]),
    )
    ordered_task_ids = [task["id"] for task in ordered_tasks]
    batches: list[dict] = []
    next_batch_index = 1
    open_batches: list[dict] = []
    for task in ordered_tasks:
        task_files = set(task["files"])
        placed = False
        for batch in open_batches:
            if batch["_files"] & task_files:
                continue
            batch["task_ids"].append(task["id"])
            batch["_files"].update(task_files)
            placed = True
            break
        if not placed:
            open_batches.append({
                "index": next_batch_index,
                "task_ids": [task["id"]],
                "_files": set(task_files),
            })
            next_batch_index += 1
    for batch in open_batches:
        batches.append({
            "index": batch["index"],
            "task_ids": batch["task_ids"],
            "file_locks": sorted(batch["_files"]),
        })

    return ordered_task_ids, batches, []


def _validate_schedule_refs(tasks: list, batches: list) -> list[dict]:
    errors: list[dict] = []
    seen_ids: dict[str, int] = {}
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = str(raw)
        if tid in seen_ids:
            errors.append({
                "path": f"$.tasks[{i}].id",
                "code": "duplicate-task-id",
                "message": f"duplicate id {tid!r} (first at tasks[{seen_ids[tid]}])",
            })
        else:
            seen_ids[tid] = i

    seen_idx: dict = {}
    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            continue
        raw = b.get("index") if "index" in b else b.get("batch_index")
        if raw is None:
            continue
        if raw in seen_idx:
            errors.append({
                "path": f"$.batches[{i}].index",
                "code": "duplicate-batch-index",
                "message": f"duplicate index {raw!r} (first at batches[{seen_idx[raw]}])",
            })
        else:
            seen_idx[raw] = i

    known_ids = set(seen_ids)
    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            continue
        refs = b.get("task_ids") or []
        if not isinstance(refs, list):
            continue
        for j, r in enumerate(refs):
            if str(r) not in known_ids:
                errors.append({
                    "path": f"$.batches[{i}].task_ids[{j}]",
                    "code": "unknown-batch-task-ref",
                    "message": f"task_ids[{j}]={str(r)!r} is not a known task id",
                })

    # Batch file-scope disjointness (TASK-003B Fix D):
    # Tasks in the same batch must have pairwise-disjoint `files` lists.
    # Overlap means two wrappers would race on the same file; observe-only
    # cleanup protects sibling state, but legitimate overwrites would still
    # produce undefined results. Fail fast with the concrete colliding tasks
    # and files so the analyst can rebalance the schedule.
    task_files_by_id: dict[str, list[str]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = str(raw)
        raw_files = t.get("files") or []
        if not isinstance(raw_files, list):
            continue
        task_files_by_id[tid] = [str(f) for f in raw_files if isinstance(f, str)]

    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            continue
        refs_raw = b.get("task_ids") or []
        if not isinstance(refs_raw, list):
            continue
        refs = [str(r) for r in refs_raw]
        per_task_files: list[tuple[str, set[str]]] = []
        for tid in refs:
            files = set(task_files_by_id.get(tid, []))
            if files:
                per_task_files.append((tid, files))
        for a in range(len(per_task_files)):
            tid_a, files_a = per_task_files[a]
            for bi in range(a + 1, len(per_task_files)):
                tid_b, files_b = per_task_files[bi]
                overlap = sorted(files_a & files_b)
                if overlap:
                    errors.append({
                        "path": f"$.batches[{i}].task_ids",
                        "code": "batch-file-overlap",
                        "message": (
                            f"batch {i} tasks {tid_a!r} and {tid_b!r} share "
                            f"files: {overlap}"
                        ),
                    })
    return errors


def _validate_schedule_dag(tasks: list, batches: list) -> list[dict]:
    """Cycle + orphan-dep detection on a schedule's task graph.

    Returns errors[*]; never calls `_die` — callers decide whether to halt or
    merge into their own error list. Mirrors `_validate_schedule_refs`'s
    contract. Error codes emitted:

      * ``unknown-dependency`` — one per ``task.dependencies[j]`` entry that
        does not normalize to a known task id. The orphan is reported, but
        the dep is still dropped before cycle analysis so a cycle among the
        remaining known tasks still surfaces.
      * ``dependency-cycle`` — at most one entry; message names the residual
        cyclic task ids (sorted, canonical form).
    """
    errors: list[dict] = []
    known_ids: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = _normalize_task_id(str(raw))
        if tid:
            known_ids.add(tid)

    # 1. Orphan-dependency pre-check. A dep that doesn't resolve to any known
    # task is reported individually and then dropped from the cycle graph so
    # the subsequent Kahn's pass is over the cleaned subgraph.
    dag_deps: dict[str, list[str]] = {}
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = _normalize_task_id(str(raw))
        if not tid:
            continue
        deps_norm: list[str] = []
        for j, dep in enumerate(t.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None:
                continue
            if dep_norm not in known_ids:
                errors.append({
                    "path": f"$.tasks[{i}].dependencies[{j}]",
                    "code": "unknown-dependency",
                    "message": (
                        f"tasks[{i}].dependencies[{j}]={str(dep)!r} is not a known task id"
                    ),
                })
                continue
            deps_norm.append(dep_norm)
        dag_deps[tid] = deps_norm

    # 2. Kahn's algorithm — cycle detection over the cleaned dep graph.
    indeg: dict[str, int] = {tid: 0 for tid in dag_deps}
    for tid, deps in dag_deps.items():
        for d in deps:
            if d in indeg:
                indeg[tid] += 1
    queue = [tid for tid, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        head = queue.pop(0)
        visited += 1
        for other, deps in dag_deps.items():
            if head in deps:
                indeg[other] -= 1
                if indeg[other] == 0:
                    queue.append(other)
    if visited != len(dag_deps):
        cyclic = sorted(tid for tid, n in indeg.items() if n > 0)
        errors.append({
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": f"dependency cycle in schedule involving tasks: {cyclic}",
        })
    return errors


def _validate_schedule(data: dict, *, strict_nested: bool = False) -> tuple[list[dict], list[str]]:
    errors: list[dict] = []
    warnings: list[str] = []

    for key in data.keys():
        if key not in ALLOWED_SCHEDULE_TOP_LEVEL:
            errors.append({
                "path": f"$.{key}",
                "code": "unknown-top-level-field",
                "message": (
                    f"unknown top-level field {key!r}; "
                    f"allowed: {sorted(ALLOWED_SCHEDULE_TOP_LEVEL)}"
                ),
            })

    outcome = data.get("outcome")
    if outcome not in {"valid", "invalid", "needs-enrichment"}:
        errors.append({
            "path": "$.outcome",
            "code": "invalid-outcome",
            "message": (
                f"outcome must be valid|invalid|needs-enrichment, got {outcome!r}"
            ),
        })

    gaps = data.get("gaps") or []
    if outcome == "needs-enrichment" and len(gaps) == 0:
        errors.append({
            "path": "$.outcome",
            "code": "outcome-gap-mismatch",
            "message": "outcome='needs-enrichment' requires at least one entry in gaps[]; got empty list",
        })
    if outcome == "valid" and len(gaps) > 0:
        errors.append({
            "path": "$.outcome",
            "code": "outcome-gap-mismatch",
            "message": "outcome='valid' requires gaps[] to be empty; got non-empty list",
        })

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        errors.append({
            "path": "$.tasks",
            "code": "invalid-type",
            "message": "tasks must be an array",
        })
        tasks = []
    else:
        alias_task_id_warned = False
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                errors.append({
                    "path": f"$.tasks[{i}]",
                    "code": "invalid-type",
                    "message": f"tasks[{i}] must be an object",
                })
                continue
            if "id" not in t:
                if "task_id" in t:
                    if not alias_task_id_warned:
                        warnings.append(
                            "schedule uses legacy 'task_id' field; canonical is 'id'"
                        )
                        alias_task_id_warned = True
                else:
                    errors.append({
                        "path": f"$.tasks[{i}].id",
                        "code": "missing-field",
                        "message": f"tasks[{i}] missing field 'id'",
                    })
            for key in ("agent", "files"):
                if key not in t:
                    errors.append({
                        "path": f"$.tasks[{i}].{key}",
                        "code": "missing-field",
                        "message": f"tasks[{i}] missing field {key!r}",
                    })
            raw_id = t.get("id") if "id" in t else t.get("task_id")
            if raw_id is not None and not CANONICAL_ID_RE.match(str(raw_id)):
                errors.append({
                    "path": f"$.tasks[{i}].id",
                    "code": "non-canonical-id",
                    "message": (
                        f"tasks[{i}].id={raw_id!r} does not match canonical "
                        "form /^\\d{3}[A-Z]?$/"
                    ),
                })
            if "plan_file" in t:
                plan_file = t.get("plan_file")
                if not isinstance(plan_file, str) or not _is_valid_plan_file_basename(plan_file):
                    errors.append({
                        "path": f"$.tasks[{i}].plan_file",
                        "code": "invalid-plan-file",
                        "message": (
                            "plan_file must be a non-empty POSIX-portable basename "
                            "with no path separators, '..', leading dot, NUL byte, "
                            "or length > 255 bytes"
                        ),
                    })
            for key in t.keys():
                if key not in ALLOWED_TASK_FIELDS:
                    msg = f"tasks[{i}] unknown field {key!r}"
                    if strict_nested:
                        errors.append({
                            "path": f"$.tasks[{i}].{key}",
                            "code": "unknown-nested-field",
                            "message": msg,
                        })
                    else:
                        warnings.append(msg)

    batches = data.get("batches")
    if not isinstance(batches, list):
        errors.append({
            "path": "$.batches",
            "code": "invalid-type",
            "message": "batches must be an array",
        })
        batches = []
    else:
        alias_batch_index_warned = False
        for i, b in enumerate(batches):
            if not isinstance(b, dict):
                errors.append({
                    "path": f"$.batches[{i}]",
                    "code": "invalid-type",
                    "message": f"batches[{i}] must be an object",
                })
                continue
            if "index" not in b:
                if "batch_index" in b:
                    if not alias_batch_index_warned:
                        warnings.append(
                            "schedule uses legacy 'batch_index' field; canonical is 'index'"
                        )
                        alias_batch_index_warned = True
                else:
                    errors.append({
                        "path": f"$.batches[{i}].index",
                        "code": "missing-field",
                        "message": f"batches[{i}] missing field 'index'",
                    })
            for key in ("task_ids", "file_locks"):
                if key not in b:
                    errors.append({
                        "path": f"$.batches[{i}].{key}",
                        "code": "missing-field",
                        "message": f"batches[{i}] missing field {key!r}",
                    })
            for key in b.keys():
                if key not in ALLOWED_BATCH_FIELDS:
                    msg = f"batches[{i}] unknown field {key!r}"
                    if strict_nested:
                        errors.append({
                            "path": f"$.batches[{i}].{key}",
                            "code": "unknown-nested-field",
                            "message": msg,
                        })
                    else:
                        warnings.append(msg)

    if not errors:
        errors.extend(_validate_schedule_refs(tasks, batches))

    return errors, warnings


def _validate_reviewer_finding_item(item: object, *, path: str) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(item, dict):
        return [{
            "path": path,
            "code": "invalid-reviewer-finding",
            "message": "reviewer finding must be an object",
        }]

    required = {
        "severity": str,
        "file": str,
        "line": int,
        "issue": str,
        "suggested_fix": str,
    }
    for key, typ in required.items():
        if key not in item:
            errors.append({
                "path": f"{path}.{key}",
                "code": "missing-reviewer-finding-field",
                "message": f"reviewer finding missing field {key!r}",
            })
            continue
        value = item[key]
        if typ is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, typ)
        if not ok:
            errors.append({
                "path": f"{path}.{key}",
                "code": "invalid-reviewer-finding-field",
                "message": f"reviewer finding field {key!r} must be a {typ.__name__}",
            })
    severity = item.get("severity")
    if isinstance(severity, str) and severity not in {"critical", "important", "minor"}:
        errors.append({
            "path": f"{path}.severity",
            "code": "invalid-reviewer-finding-severity",
            "message": f"reviewer finding severity must be one of ['critical', 'important', 'minor'], got {severity!r}",
        })
    for key in item.keys():
        if key in required or key in OPTIONAL_REVIEWER_FINDING_FIELDS:
            continue
        errors.append({
            "path": f"{path}.{key}",
            "code": "unknown-reviewer-finding-field",
            "message": f"reviewer finding has unknown field {key!r}",
        })

    # TASK-019: optional disposition + disposition_reason fields. Both are
    # additive — absent is fine. Value constraints are enforced only when the
    # field is present, so existing callers continue to pass unchanged.
    disposition_present = "disposition" in item
    disposition = item.get("disposition") if disposition_present else None
    if disposition_present and disposition is not None:
        if disposition not in ALLOWED_REVIEWER_FINDING_DISPOSITIONS:
            errors.append({
                "path": f"{path}.disposition",
                "code": "invalid-reviewer-finding-disposition",
                "message": (
                    "reviewer finding disposition must be one of "
                    f"{sorted(ALLOWED_REVIEWER_FINDING_DISPOSITIONS)}, "
                    f"got {disposition!r}"
                ),
            })
    reason_present = "disposition_reason" in item
    reason = item.get("disposition_reason") if reason_present else None
    if reason_present and reason is not None and not isinstance(reason, str):
        errors.append({
            "path": f"{path}.disposition_reason",
            "code": "invalid-reviewer-finding-disposition-reason",
            "message": "reviewer finding disposition_reason must be a string",
        })
    if reason_present and reason is not None and (not disposition_present or disposition is None):
        errors.append({
            "path": f"{path}.disposition_reason",
            "code": "disposition-reason-without-disposition",
            "message": (
                "reviewer finding has disposition_reason but no disposition; "
                "the reason cannot be interpreted without an accompanying disposition"
            ),
        })
    return errors


def _validate_minor_findings_payload(minor: object, *, path: str) -> list[dict]:
    if not isinstance(minor, list):
        return [{
            "path": path,
            "code": "invalid-reviewer-minor-findings",
            "message": "reviewer minor findings must be a JSON array",
        }]
    errors: list[dict] = []
    for i, item in enumerate(minor):
        errors.extend(_validate_reviewer_finding_item(item, path=f"{path}[{i}]"))
    return errors


def _validate_review_success_payload(
    reviewer: str,
    reviewer_verdict: str,
    reviewer_minor_findings: object,
) -> list[dict]:
    errors: list[dict] = []
    allowed_verdicts: set[str]
    commit_allowed: set[str]
    if reviewer == "codex":
        allowed_verdicts = ALLOWED_CODEX_REVIEW_VERDICTS
        commit_allowed = {"clean", "minor-findings"}
    elif reviewer == "claude":
        allowed_verdicts = ALLOWED_CLAUDE_REVIEW_VERDICTS
        commit_allowed = {"ship", "ship-with-fixes"}
    elif reviewer == "none":
        allowed_verdicts = {""}
        commit_allowed = {""}
    else:
        return [{
            "path": "$.reviewer",
            "code": "invalid-reviewer",
            "message": f"reviewer must be one of ['claude', 'codex', 'none'], got {reviewer!r}",
        }]

    if reviewer_verdict not in allowed_verdicts:
        errors.append({
            "path": "$.reviewer_verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                f"reviewer {reviewer!r} verdict must be one of {sorted(allowed_verdicts)}, "
                f"got {reviewer_verdict!r}"
            ),
        })
    elif reviewer_verdict not in commit_allowed:
        errors.append({
            "path": "$.reviewer_verdict",
            "code": "uncommittable-reviewer-verdict",
            "message": (
                f"commit-task cannot accept reviewer verdict {reviewer_verdict!r}; "
                f"allowed commit verdicts for reviewer {reviewer!r}: {sorted(commit_allowed)}"
            ),
        })

    errors.extend(_validate_minor_findings_payload(
        reviewer_minor_findings,
        path="$.reviewer_minor_findings",
    ))
    return errors


def _validate_review_failure_payload(reviewer_findings: object) -> list[dict]:
    if not isinstance(reviewer_findings, dict):
        return [{
            "path": "$.reviewer_findings",
            "code": "invalid-reviewer-findings",
            "message": "reviewer findings must be a JSON object",
        }]

    errors: list[dict] = []
    required = {
        "task_id": str,
        "verdict": str,
        "findings": list,
        "scope_ok": bool,
        "acceptance_met": bool,
        "summary": str,
    }
    for key, typ in required.items():
        if key not in reviewer_findings:
            errors.append({
                "path": f"$.reviewer_findings.{key}",
                "code": "missing-reviewer-field",
                "message": f"reviewer findings missing field {key!r}",
            })
            continue
        value = reviewer_findings[key]
        if not isinstance(value, typ):
            errors.append({
                "path": f"$.reviewer_findings.{key}",
                "code": "invalid-reviewer-field",
                "message": f"reviewer findings field {key!r} must be a {typ.__name__}",
            })

    verdict = reviewer_findings.get("verdict")
    if isinstance(verdict, str) and verdict not in ALLOWED_CODEX_REVIEW_VERDICTS:
        errors.append({
            "path": "$.reviewer_findings.verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                "reviewer findings verdict must be one of "
                f"{sorted(ALLOWED_CODEX_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    findings = reviewer_findings.get("findings")
    if isinstance(findings, list):
        for i, item in enumerate(findings):
            errors.extend(_validate_reviewer_finding_item(
                item,
                path=f"$.reviewer_findings.findings[{i}]",
            ))

    for key in reviewer_findings.keys():
        if key not in required:
            errors.append({
                "path": f"$.reviewer_findings.{key}",
                "code": "unknown-reviewer-field",
                "message": f"reviewer findings has unknown field {key!r}",
            })
    return errors


def _validate_d5_adjudication_payload(
    payload: object,
    *,
    codex_findings_count: int,
) -> list[dict]:
    """Validate a D.5 adjudication payload per TASK-016A.

    Shape (verdict-dependent):
        {
          "verdict": "ship" | "ship-with-fixes" | "partial-agreement" | "needs-rework",
          "summary": "<one-line justification>",
          # required when verdict == "partial-agreement":
          "load_bearing": [int, ...],   # 0-based indices into codex_findings
          "dismissed":   [int, ...],    # 0-based indices into codex_findings
        }

    For `partial-agreement`, `load_bearing` and `dismissed` MUST be
    non-empty integer arrays, disjoint, and their union MUST be a
    subset of `range(codex_findings_count)`. The split must be clean
    (≥1 load-bearing AND ≥1 dismissed); an empty bucket collapses to
    `needs-rework` / `ship-with-fixes` and the reviewer should have
    chosen those verdicts instead.

    Errors:
      - `partial-agreement-invalid-split`: empty or overlapping buckets.
      - `partial-agreement-unknown-index`: index < 0 or >= findings count.

    The non-partial-agreement verdicts (`ship`, `ship-with-fixes`,
    `needs-rework`) pass through without requiring the split fields;
    their routing does not need finding-level indices.
    """
    errors: list[dict] = []
    if not isinstance(payload, dict):
        return [{
            "path": "$",
            "code": "invalid-type",
            "message": "D.5 adjudication payload must be a JSON object",
        }]

    verdict = payload.get("verdict")
    if not isinstance(verdict, str):
        errors.append({
            "path": "$.verdict",
            "code": "missing-field",
            "message": "D.5 payload missing required field 'verdict'",
        })
    elif verdict not in ALLOWED_CLAUDE_REVIEW_VERDICTS:
        errors.append({
            "path": "$.verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                f"D.5 verdict must be one of "
                f"{sorted(ALLOWED_CLAUDE_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        errors.append({
            "path": "$.summary",
            "code": "invalid-field",
            "message": "D.5 payload field 'summary' must be a string",
        })

    if verdict == "partial-agreement":
        # Pre-TASK-016C follow-up (non-blocking note from D.5 on TASK-016A #1):
        # the orchestrator forwards `d5_summary` to the plan-remediator
        # Phase B-narrow-remediation template; a missing or empty string
        # silently hands the remediator a blank justification for why the
        # load-bearing findings are load-bearing. Require non-empty summary
        # on the partial-agreement path.
        if not isinstance(summary, str) or summary.strip() == "":
            errors.append({
                "path": "$.summary",
                "code": "partial-agreement-missing-summary",
                "message": (
                    "partial-agreement payload requires a non-empty "
                    "'summary' string; the remediator template forwards "
                    "this to justify why the load-bearing findings are "
                    "load-bearing"
                ),
            })

        def _check_bucket(name: str) -> list[int] | None:
            value = payload.get(name)
            if value is None:
                errors.append({
                    "path": f"$.{name}",
                    "code": "missing-field",
                    "message": (
                        f"partial-agreement payload missing required field "
                        f"{name!r}"
                    ),
                })
                return None
            if not isinstance(value, list):
                errors.append({
                    "path": f"$.{name}",
                    "code": "invalid-type",
                    "message": (
                        f"partial-agreement field {name!r} must be an array"
                    ),
                })
                return None
            cleaned: list[int] = []
            had_type_error = False
            for i, item in enumerate(value):
                # bool is a subclass of int in Python; reject it explicitly so
                # `[True]` does not pass as `[1]`.
                if not isinstance(item, int) or isinstance(item, bool):
                    errors.append({
                        "path": f"$.{name}[{i}]",
                        "code": "invalid-type",
                        "message": (
                            f"partial-agreement field {name!r}[{i}] must be "
                            f"an integer index, got {type(item).__name__}"
                        ),
                    })
                    had_type_error = True
                    continue
                cleaned.append(item)
            # Pre-TASK-016C follow-up (non-blocking note from D.5 on
            # TASK-016A #2): short-circuit downstream split validation
            # on bucket-element type errors so callers don't see a
            # spurious `partial-agreement-invalid-split` stacked on top
            # of the underlying `invalid-type`. Return None signals
            # "structurally broken; skip split checks".
            if had_type_error:
                return None
            return cleaned

        load_bearing = _check_bucket("load_bearing")
        dismissed = _check_bucket("dismissed")

        # Only proceed with split validation when both buckets were
        # structurally valid; otherwise the type errors above are
        # enough signal for the caller.
        if load_bearing is not None and dismissed is not None:
            if len(load_bearing) == 0:
                errors.append({
                    "path": "$.load_bearing",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        "partial-agreement requires a non-empty "
                        "'load_bearing' bucket; empty collapses to "
                        "'ship-with-fixes' — pick that verdict instead"
                    ),
                })
            if len(dismissed) == 0:
                errors.append({
                    "path": "$.dismissed",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        "partial-agreement requires a non-empty "
                        "'dismissed' bucket; empty collapses to "
                        "'needs-rework' — pick that verdict instead"
                    ),
                })
            # Pre-TASK-016C follow-up (non-blocking note from D.5 on
            # TASK-016A #2): intra-bucket duplicates (e.g., `[0, 0]`)
            # are a contract violation — the remediator would see the
            # same index twice. Reject with the existing
            # `partial-agreement-invalid-split` code.
            for name, bucket in (
                ("load_bearing", load_bearing),
                ("dismissed", dismissed),
            ):
                seen: dict[int, int] = {}
                dup_indices: list[int] = []
                for i, idx in enumerate(bucket):
                    if idx in seen:
                        dup_indices.append(idx)
                    else:
                        seen[idx] = i
                if dup_indices:
                    errors.append({
                        "path": f"$.{name}",
                        "code": "partial-agreement-invalid-split",
                        "message": (
                            f"partial-agreement bucket {name!r} must not "
                            f"repeat indices; duplicates: "
                            f"{sorted(set(dup_indices))}"
                        ),
                    })
            overlap = sorted(set(load_bearing) & set(dismissed))
            if overlap:
                errors.append({
                    "path": "$.load_bearing",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        f"partial-agreement requires disjoint buckets; "
                        f"indices {overlap} appear in both 'load_bearing' "
                        f"and 'dismissed'"
                    ),
                })
            for name, bucket in (
                ("load_bearing", load_bearing),
                ("dismissed", dismissed),
            ):
                for i, idx in enumerate(bucket):
                    if idx < 0 or idx >= codex_findings_count:
                        errors.append({
                            "path": f"$.{name}[{i}]",
                            "code": "partial-agreement-unknown-index",
                            "message": (
                                f"partial-agreement index {idx} in {name!r} "
                                f"is out of range for codex_findings of "
                                f"length {codex_findings_count}"
                            ),
                        })

    return errors


def _validate_execution_log_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return [{
            "path": "$.rows",
            "code": "invalid-execution-log-rows",
            "message": "execution-log rows must be a JSON array",
        }]

    # TASK-019: render the allowed-field list inline on both missing-field
    # and unknown-field error messages so an orchestrator hitting the error
    # can see the full schema without consulting SKILL.md.
    allowed_display = sorted(ALLOWED_ROW_FIELDS)
    errors: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({
                "path": f"$.rows[{i}]",
                "code": "invalid-execution-log-row",
                "message": "execution-log row must be an object",
            })
            continue
        for key in ALLOWED_ROW_FIELDS:
            if key not in row:
                errors.append({
                    "path": f"$.rows[{i}].{key}",
                    "code": "missing-execution-log-field",
                    "message": (
                        f"execution-log row missing field {key!r}; "
                        f"required fields: {allowed_display}"
                    ),
                })
            elif not isinstance(row[key], str):
                errors.append({
                    "path": f"$.rows[{i}].{key}",
                    "code": "invalid-execution-log-field",
                    "message": f"execution-log row field {key!r} must be a string",
                })
        for key in row.keys():
            if key not in ALLOWED_ROW_FIELDS:
                errors.append({
                    "path": f"$.rows[{i}].{key}",
                    "code": "unknown-execution-log-field",
                    "message": (
                        f"execution-log row has unknown field {key!r}; "
                        f"allowed fields: {allowed_display}"
                    ),
                })
    return errors


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _resolve_python() -> str:
    """Resolve the interpreter path per documented precedence (TASK-008).

    Precedence (first match wins):
      1. $IMPLEMENT_PLAN_PYTHON if the path exists and is executable.
      2. <cwd>/venv/bin/python if present and executable.
      3. <cwd>/.venv/bin/python if present and executable.
      4. shutil.which('python3').
      5. sys.executable as final fallback.
    Returns an absolute path string. The result is echoed into
    `preflight --json` as `python_path` so the orchestrator can pin one
    interpreter for the rest of the run via ``{{python_path}}``
    substitution in dispatch templates. The skill surface (SKILL.md +
    dispatch-templates.md) uses `$PYTHON` as the placeholder; this
    helper is the single source of truth for the resolution rule.
    """
    env = os.environ.get("IMPLEMENT_PLAN_PYTHON")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return str(Path(env).resolve())
    for rel in ("venv/bin/python", ".venv/bin/python"):
        p = Path.cwd() / rel
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    which = shutil.which("python3")
    if which:
        return which
    return sys.executable


def _emit(args, result: dict, *, exit_code: int = 0) -> None:
    if getattr(args, "json", False):
        json.dump(result, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        if "error" in result:
            print(f"ERROR: {result['error']}", file=sys.stderr)
        else:
            for k, v in result.items():
                print(f"{k}: {v}")
    sys.exit(exit_code)


def _die(args, result: dict, *, exit_code: int = 1) -> None:
    _emit(args, result, exit_code=exit_code)


def _normalize_task_id(raw: str) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    m = TASK_ID_INPUT_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    if n < 0 or n > 999:
        return None
    return f"{n:03d}{m.group(2)}"


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _atomic_write_text(path: Path, text: str) -> int:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return len(text.encode("utf-8"))


def _split_task_blocks(plan_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split plan body on `^### TASK-NNN:` boundaries.

    Returns (preamble, [(task_id, block_text), ...]) where each block_text
    starts with `### TASK-NNN:` through the start of the next task or EOF.
    """
    matches = list(TASK_HEADER_RE.finditer(plan_text))
    if not matches:
        return plan_text, []
    preamble = plan_text[: matches[0].start()]
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plan_text)
        blocks.append((m.group(1), plan_text[m.start() : end]))
    return preamble, blocks


def _find_status_bullet(block: str) -> re.Match | None:
    return STATUS_BULLET_RE.search(block)


def mutate_task_status(plan_text: str, task_id: str, new_status: str) -> tuple[str, str]:
    """Structurally mutate TASK-NNN's `**Status:**` bullet to `new_status`.

    Returns (updated_plan_text, prior_status). Raises ValueError on drift:
    task block not found, Status bullet not found, or new_status invalid.
    """
    if new_status not in ALLOWED_TASK_STATUSES:
        raise ValueError(f"status {new_status!r} not in {sorted(ALLOWED_TASK_STATUSES)}")
    preamble, blocks = _split_task_blocks(plan_text)
    if not blocks:
        raise ValueError("no task blocks found in plan")
    updated: list[str] = []
    prior: str | None = None
    matched = False
    for tid, body in blocks:
        if tid == task_id and not matched:
            m = _find_status_bullet(body)
            if not m:
                raise ValueError(f"no **Status:** bullet in TASK-{task_id}")
            prior = m.group(2).strip()
            new_line = f"{m.group(1)} {new_status}"
            new_body = body[: m.start()] + new_line + body[m.end() :]
            updated.append(new_body)
            matched = True
        else:
            updated.append(body)
    if not matched:
        raise ValueError(f"TASK-{task_id} not found in plan")
    return preamble + "".join(updated), prior or ""


def _parse_index_roster(path: Path) -> dict[str, dict]:
    """Parse the JSON sidecar roster for DUAL_AGENT_Plans.

    Returns `{task_id: {"file": filename, "depends_on": deps, "status": status}}`.
    Supersession metadata is validated and stored in `_INDEX_SUPERSEDED_BY`.
    """
    if not path.is_file():
        raise FileNotFoundError(f"index file not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON in {path}: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"index sidecar must be a JSON object in {path}")
    if "schema_version" not in doc or "chunks" not in doc:
        raise ValueError(f"index sidecar missing schema_version or chunks in {path}")
    if doc["schema_version"] != 1:
        raise ValueError(f"unsupported index schema_version {doc['schema_version']!r} in {path}")
    chunks = doc["chunks"]
    if not isinstance(chunks, list):
        raise ValueError(f"index chunks must be a list in {path}")

    def _valid_normalized_id(raw: object, field: str, chunk_ref: str) -> str:
        if not isinstance(raw, str):
            raise ValueError(f"{field} for {chunk_ref} must be a string")
        normalized = _normalize_task_id(raw)
        if normalized is None or raw != normalized:
            raise ValueError(f"{field} for {chunk_ref} must be a normalized task id")
        return normalized

    global _INDEX_SUPERSEDED_BY
    _INDEX_SUPERSEDED_BY = {}
    roster: dict[str, dict] = {}
    for i, chunk in enumerate(chunks):
        chunk_ref = f"chunks[{i}]"
        if not isinstance(chunk, dict):
            raise ValueError(f"{chunk_ref} must be an object")
        missing = {"task_id", "file", "depends_on", "status", "superseded_by"} - set(chunk)
        if missing:
            raise ValueError(f"{chunk_ref} missing required fields: {sorted(missing)}")
        task_id = _valid_normalized_id(chunk["task_id"], "task_id", chunk_ref)
        if task_id in roster:
            raise ValueError(f"duplicate task_id {task_id} in {path}")
        filename = chunk["file"]
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"file for {task_id} must be a non-empty string")
        status = chunk["status"]
        if status not in ALLOWED_INDEX_STATUSES:
            raise ValueError(f"status for {task_id} must be one of {sorted(ALLOWED_INDEX_STATUSES)}")
        depends_raw = chunk["depends_on"]
        superseded_raw = chunk["superseded_by"]
        if not isinstance(depends_raw, list):
            raise ValueError(f"depends_on for {task_id} must be a list")
        if not isinstance(superseded_raw, list):
            raise ValueError(f"superseded_by for {task_id} must be a list")
        deps = [_valid_normalized_id(dep, "depends_on", task_id) for dep in depends_raw]
        superseded_by = [
            _valid_normalized_id(dep, "superseded_by", task_id)
            for dep in superseded_raw
        ]
        if status == "Superseded" and not superseded_by:
            raise ValueError(f"Superseded task {task_id} must have superseded_by targets")
        if status != "Superseded" and superseded_by:
            raise ValueError(f"non-Superseded task {task_id} must not have superseded_by targets")
        roster[task_id] = {"file": filename, "depends_on": deps, "status": status}
        if superseded_by:
            _INDEX_SUPERSEDED_BY[task_id] = superseded_by

    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError(f"supersession cycle involving {task_id}")
        visiting.add(task_id)
        for child in _INDEX_SUPERSEDED_BY.get(task_id, []):
            if child in _INDEX_SUPERSEDED_BY:
                _visit(child)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in list(_INDEX_SUPERSEDED_BY):
        _visit(task_id)
    return roster


def _update_index_status(
    index_path: Path, plan_basename: str, new_status: str
) -> tuple[str, dict | None]:
    """Flip the `status` of the chunk whose `file` matches `plan_basename`.

    TASK-014B: written atomically via `tempfile.NamedTemporaryFile(dir=parent)
    + os.replace(tmp, index_path)` so a mid-write interrupt leaves the prior
    roster intact. The write is idempotent — re-running for an already-at-
    target chunk produces byte-identical bytes on the second run.

    Returns `(outcome, error_dict_or_None)`:
      * `("updated", None)` — chunk found, status changed, file rewritten.
      * `("unchanged", None)` — chunk found, status already at target; no
        write performed (byte-identical idempotency is trivially satisfied).
      * `("missing-index", err)` — `00_INDEX.json` absent (tolerated by the
        caller as a soft skip).
      * `("invalid-index", err)` — roster present but unparseable or missing
        `chunks`. Caller must treat as a hard error so silently-invalid
        sidecars don't drift.
      * `("missing-entry", err)` — roster parsed but no chunk matches
        `plan_basename`.

    `err` is an `errors[0]`-shaped dict `{path, code, message}` ready to feed
    into `_die`.
    """
    if new_status not in ALLOWED_INDEX_STATUSES:
        raise ValueError(
            f"new_status must be one of {sorted(ALLOWED_INDEX_STATUSES)}, got {new_status!r}"
        )

    if not index_path.is_file():
        return "missing-index", {
            "path": f"$.<file:{index_path}>",
            "code": "index-not-found",
            "message": f"index file not found: {index_path}",
        }

    raw_text = index_path.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return "invalid-index", {
            "path": f"$.<file:{index_path}>",
            "code": "index-malformed",
            "message": f"malformed JSON in {index_path}: {exc}",
        }
    if not isinstance(doc, dict) or not isinstance(doc.get("chunks"), list):
        return "invalid-index", {
            "path": f"$.<file:{index_path}>",
            "code": "index-malformed",
            "message": f"index sidecar missing chunks in {index_path}",
        }

    target_idx = None
    for i, chunk in enumerate(doc["chunks"]):
        if isinstance(chunk, dict) and chunk.get("file") == plan_basename:
            target_idx = i
            break

    if target_idx is None:
        return "missing-entry", {
            "path": f"$.<file:{index_path}>.chunks",
            "code": "task-not-in-index",
            "message": (
                f"no chunk in 00_INDEX.json matches plan basename {plan_basename!r}"
            ),
        }

    current_status = doc["chunks"][target_idx].get("status")
    if current_status == new_status:
        # Idempotent: re-running commit-task on an already-Done chunk is a
        # no-op write. Skip the tempfile dance entirely so the on-disk bytes
        # are trivially byte-identical across runs.
        return "unchanged", None

    doc["chunks"][target_idx]["status"] = new_status

    # `json.dumps(..., indent=2)` mirrors the roster's existing layout (see
    # `_write_roster` in the test suite and the on-disk 00_INDEX.json files
    # in docs/plans/DUAL_AGENT_Plans/). Trailing newline for POSIX-friendly
    # diffs.
    new_text = json.dumps(doc, indent=2) + "\n"

    parent = index_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(parent),
            prefix=".00_INDEX.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(new_text)
            tmp_name = tmp.name
        os.replace(tmp_name, index_path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    return "updated", None


def _first_plan_task_id(plan_text: str) -> str | None:
    """Return the first TASK id declared in a plan file."""
    _, blocks = _split_task_blocks(plan_text)
    if not blocks:
        return None
    return blocks[0][0]


def _append_run_log(event: str, fields: dict) -> str:
    """Append a JSONL event and verify via tail. Returns the written line."""
    rec = {"ts": _now(), "event": event, **fields}
    line = json.dumps(rec, sort_keys=False)
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        with RUN_LOG_PATH.open("r", encoding="utf-8") as fh:
            last = ""
            for last in fh:
                pass
        if last.rstrip("\n") != line:
            with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as e:
        raise RuntimeError(f"run-log append verification failed: {e}")
    return line


def _git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# TASK-020B: opt-in `acceptance_v_check` runtime enforcement.
#
# `_parse_frontmatter` extracts a YAML block delimited by `---` lines at the
# VERY top of the plan (`\A---\n...\n---\n`). Missing/malformed → {}; the
# commit-task flow then behaves identically to pre-TASK-020B.
#
# `_run_v_check` executes the declared shell command with a bounded timeout
# and returns a structured dict: on success `{code: "v-check-passed",
# stdout_tail}`; on failure one of `acceptance-v-check-failed`,
# `v-check-timeout`, `v-check-subprocess-error` with matching
# {stdout_tail, stderr_tail, message} fields. Tails are capped at 2048 bytes
# to keep error envelopes bounded when a V-check produces MBs of output.
#
# Shell-injection surface: `shell=True` is intentional — plan authors declare
# shell commands like `venv/bin/pytest -q tests/scripts/test_foo.py`. Plans
# must not be edited by untrusted parties without review.
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict:
    """Return the top-of-file YAML frontmatter as a dict, or {} if absent.

    Uses `re.match` with `\\A` so only a frontmatter block that starts at
    position 0 is recognized; a `---` line mid-document is NOT picked up.
    """
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not m:
        return {}
    try:
        import yaml  # available per TASK-013
        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _run_v_check(cmd: str, cwd: Path, timeout: int) -> dict:
    """Execute `cmd` via shell with bounded timeout; return structured result.

    Success: `{code: "v-check-passed", stdout_tail: <last 2048 bytes>}`.
    Failure codes:
        - `acceptance-v-check-failed` — non-zero exit
        - `v-check-timeout` — subprocess.TimeoutExpired
        - `v-check-subprocess-error` — any other launch error
    Each failure dict carries `stdout_tail`, `stderr_tail`, `message`.
    """
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        stdout_val = e.stdout if isinstance(e.stdout, str) else (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, (bytes, bytearray)) else ""
        )
        stderr_val = e.stderr if isinstance(e.stderr, str) else (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, (bytes, bytearray)) else ""
        )
        return {
            "code": "v-check-timeout",
            "message": f"acceptance V-check exceeded {timeout}s",
            "stdout_tail": (stdout_val or "")[-2048:],
            "stderr_tail": (stderr_val or "")[-2048:],
        }
    except Exception as e:
        return {
            "code": "v-check-subprocess-error",
            "message": f"acceptance V-check failed to launch: {e}",
            "stdout_tail": "",
            "stderr_tail": "",
        }
    if proc.returncode != 0:
        return {
            "code": "acceptance-v-check-failed",
            "message": f"acceptance V-check exited with code {proc.returncode}",
            "stdout_tail": (proc.stdout or "")[-2048:],
            "stderr_tail": (proc.stderr or "")[-2048:],
        }
    return {
        "code": "v-check-passed",
        "stdout_tail": (proc.stdout or "")[-2048:],
    }


# ---------------------------------------------------------------------------
# Batch reconciliation (Fix E)
# ---------------------------------------------------------------------------
#
# Defensive even if a wrapper envelope somehow reports a protected path as
# out-of-scope: reconciliation never restores/unlinks executor
# infrastructure. The protection set + predicate are imported from
# ``_plan_paths`` (the single source of truth shared with the wrapper and
# ``cmd_fail_task``).


def _envelope_field(env: dict, key: str, default=None):
    """Fetch a field from a dispatch envelope.

    The wrapper spreads `extra` onto the envelope root, so the same key may
    appear at either level. Top-level wins if both are present.
    """
    if key in env:
        return env[key]
    extra = env.get("extra") or {}
    if isinstance(extra, dict):
        return extra.get(key, default)
    return default


def reconcile_batch(
    batch_envelopes: list[dict],
    repo_root: str,
) -> list[dict]:
    """Reconcile observed out-of-scope writes after a batch's join barrier.

    Called by the orchestrator in the single-writer phase between batch
    completion and per-task review/commit dispatch. No wrapper process is
    running at this point, so it is safe to mutate the working tree.

    For each envelope with ``out_of_scope_observed`` true:
      * Restore tracked paths (staged + worktree)
      * Unlink untracked paths
      * Skip any path matching the executor-infrastructure protection set
      * Verify the actioned paths no longer appear in `git diff`

    A task whose reconciliation succeeds is marked
    ``scope_violation_reconciled`` and remains **ineligible for review and
    commit**. A task where reconciliation fails or leaves residual dirt is
    marked ``reconciliation_failed`` — the orchestrator must surface a hard
    error and must not advance to the next batch until operator
    intervention. Unaffected tasks return ``no_op``.
    """
    results: list[dict] = []
    for env in batch_envelopes:
        task_id = env.get("task_id", "")
        if not _envelope_field(env, "out_of_scope_observed", False):
            results.append({
                "task_id": task_id,
                "outcome": "no_op",
                "reconciled_tracked": [],
                "reconciled_untracked": [],
                "skipped_protected": [],
                "residual_dirty": [],
                "error": None,
            })
            continue

        raw_tracked = _envelope_field(env, "out_of_scope_tracked") or []
        raw_untracked = _envelope_field(env, "out_of_scope_untracked") or []
        tracked = [p for p in raw_tracked if isinstance(p, str)]
        untracked = [p for p in raw_untracked if isinstance(p, str)]

        skipped: set[str] = set()
        actionable_tracked: list[str] = []
        actionable_untracked: list[str] = []
        for p in tracked:
            if is_protected_path(p):
                skipped.add(p)
            else:
                actionable_tracked.append(p)
        for p in untracked:
            if is_protected_path(p):
                skipped.add(p)
            else:
                actionable_untracked.append(p)

        errors: list[str] = []
        cwd = Path(repo_root)
        if actionable_tracked:
            r1 = _git(["restore", "--staged", "--"] + actionable_tracked, cwd=cwd)
            if r1.returncode != 0:
                errors.append(
                    f"git restore --staged failed: {r1.stderr.strip() or r1.stdout.strip()}"
                )
            r2 = _git(["restore", "--"] + actionable_tracked, cwd=cwd)
            if r2.returncode != 0:
                errors.append(
                    f"git restore failed: {r2.stderr.strip() or r2.stdout.strip()}"
                )

        for p in actionable_untracked:
            full = cwd / p
            try:
                full.unlink()
            except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
                errors.append(f"unlink {p!r} failed: {e}")

        expected_clean = set(actionable_tracked + actionable_untracked)
        residual: list[str] = []
        if expected_clean:
            still_dirty: set[str] = set()
            for args in (
                ["diff", "--name-only", "HEAD"],
                ["diff", "--name-only", "--cached"],
                ["ls-files", "--others", "--exclude-standard"],
            ):
                proc = _git(args, cwd=cwd)
                if proc.returncode == 0:
                    still_dirty.update(
                        ln.strip() for ln in proc.stdout.splitlines() if ln.strip()
                    )
            residual = sorted(expected_clean & still_dirty)

        if errors or residual:
            results.append({
                "task_id": task_id,
                "outcome": "reconciliation_failed",
                "reconciled_tracked": actionable_tracked,
                "reconciled_untracked": actionable_untracked,
                "skipped_protected": sorted(skipped),
                "residual_dirty": residual,
                "error": (
                    "; ".join(errors) if errors
                    else f"residual dirty paths: {residual}"
                ),
            })
        else:
            results.append({
                "task_id": task_id,
                "outcome": "scope_violation_reconciled",
                "reconciled_tracked": actionable_tracked,
                "reconciled_untracked": actionable_untracked,
                "skipped_protected": sorted(skipped),
                "residual_dirty": [],
                "error": None,
            })
    return results


def cmd_reconcile_batch(args: argparse.Namespace) -> None:
    """CLI wrapper over reconcile_batch.

    Reads an array of envelopes from stdin; writes the result list as JSON.
    Exits 0 if every task reconciled cleanly or was a no_op; exits 1 if any
    task reconciliation failed (so the orchestrator can halt the batch).
    """
    raw = sys.stdin.read()
    try:
        envelopes = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as e:
        _die(args, {"error": f"envelopes json decode: {e}"})
    if not isinstance(envelopes, list):
        _die(args, {"error": "envelopes payload must be a JSON array"})

    results = reconcile_batch(envelopes, args.repo_root)
    any_failed = any(r["outcome"] == "reconciliation_failed" for r in results)
    _emit(
        args,
        {"results": results, "reconciliation_failed": any_failed},
        exit_code=1 if any_failed else 0,
    )


def _resolve_plan_deps(plan_path: Path, plans_dir: Path) -> dict:
    """Core dep-resolution logic. Returns {pass, deps, unresolved, errors}.

    `errors[]` is non-empty on fatal issues (plan not found, bad roster,
    missing task block, task not in 00_INDEX.json); `pass` is False in
    that case and `deps`/`unresolved` are empty. Callers decide whether
    to halt or treat unresolvable as "leave the existing value in place".
    """
    if not plan_path.is_file():
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{plan_path}>",
                "code": "file-not-found",
                "message": f"plan file not found: {plan_path}",
            }],
        }

    index_path = plans_dir / "00_INDEX.json"
    try:
        roster = _parse_index_roster(index_path)
    except (FileNotFoundError, ValueError) as e:
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{index_path}>",
                "code": "index-not-found",
                "message": str(e),
            }],
        }

    plan_text = plan_path.read_text(encoding="utf-8")
    current_task_id = _first_plan_task_id(plan_text)
    if current_task_id is None:
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{plan_path}>",
                "code": "task-not-found",
                "message": f"no TASK block found in {plan_path}",
            }],
        }
    current_entry = roster.get(current_task_id)
    if current_entry is None:
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{index_path}>.chunks",
                "code": "task-not-in-index",
                "message": f"task {current_task_id} is not declared in 00_INDEX.json roster",
            }],
        }
    requested = current_entry["depends_on"]

    resolved: list[dict] = []
    unresolved: list[dict] = []

    def _resolve_dependency(dep_id: str, parent_id: str | None = None) -> None:
        entry = roster.get(dep_id)
        if entry is None:
            item = {
                "task_id": dep_id,
                "reason": "superseded-target-missing" if parent_id else "unresolved-dep",
                "detail": f"task {dep_id} is not declared in 00_INDEX.json roster",
            }
            if parent_id:
                item["parent_id"] = parent_id
            unresolved.append(item)
            return

        plan_file = entry["file"]
        status = entry["status"]
        if status == "Done":
            resolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "status": status,
                **({"parent_id": parent_id} if parent_id else {}),
            })
            return
        if status == "Pending":
            unresolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "reason": "dep-not-done",
                "status": status,
                "detail": f"roster status is {status!r}, expected 'Done'",
                **({"parent_id": parent_id} if parent_id else {}),
            })
            return
        if status == "Superseded":
            targets = _INDEX_SUPERSEDED_BY.get(dep_id, [])
            for target_id in targets:
                _resolve_dependency(target_id, parent_id=dep_id)
            return
        unresolved.append({
            "task_id": dep_id,
            "plan_file": plan_file,
            "reason": "dep-not-done",
            "status": status,
            "detail": f"roster status is {status!r}, expected 'Done'",
            **({"parent_id": parent_id} if parent_id else {}),
        })

    for dep_id in requested:
        _resolve_dependency(dep_id)

    return {
        "pass": len(unresolved) == 0,
        "deps": resolved,
        "unresolved": unresolved,
        "errors": [],
    }


def cmd_check_plan_deps(args: argparse.Namespace) -> None:
    result = _resolve_plan_deps(Path(args.plan_file), Path(args.plans_dir))
    if result["errors"]:
        _die(args, {"errors": result["errors"]})
    _emit(args, result, exit_code=0)


# ---------------------------------------------------------------------------
# lint-plans (TASK-020A)
# ---------------------------------------------------------------------------


# Top-level plan `**Status:**` bullet. Per §D.3, a plan whose header status is
# `superseded` has its decomposition tracked by children; duplicating the
# done-pairing gate at the parent level produces noise. Match case-insensitive
# (plan authors occasionally write `Superseded`). Unlike STATUS_BULLET_RE this
# matches bold prose lines without the leading hyphen bullet, because parent
# plan headers tend to use that shape (e.g. `**Status:** superseded`).
_SUPERSEDED_HEADER_RE = re.compile(
    r"^\s*(?:-\s*)?\*\*Status:\*\*\s*superseded\b",
    re.MULTILINE | re.IGNORECASE,
)

# Match a canonical `feat(TASK-NNN[A-Z]?):` commit subject. We grep subjects
# only (via `--pretty=%s`) so TASK-019's concern-prose references in commit
# bodies don't falsely mark a task as "shipped".
_FEAT_COMMIT_SUBJECT_RE = re.compile(r"^feat\(TASK-(\d{3}[A-Z]?)\):")


def _load_commit_done_ids(run_log: Path) -> set[str]:
    """Collect normalized task ids that have a `commit_done` event in run log.

    Missing file or malformed lines are tolerated: the lint's job is to flag
    missing pairings, not to validate the run log's integrity.
    """
    ids: set[str] = set()
    if not run_log.exists():
        return ids
    try:
        lines = run_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ids
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "commit_done":
            continue
        tid = _normalize_task_id(str(ev.get("task_id", "")))
        if tid:
            ids.add(tid)
    return ids


def _load_feat_commit_ids(git_dir: Path) -> set[str]:
    """Collect normalized task ids shipped via `feat(TASK-NNN):` commits.

    Uses `git log --all --pretty=%s` so commit bodies (which may reference
    other tasks in prose) are ignored.
    """
    ids: set[str] = set()
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--pretty=%s"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return ids
    if result.returncode != 0:
        return ids
    for line in result.stdout.splitlines():
        m = _FEAT_COMMIT_SUBJECT_RE.match(line.strip())
        if not m:
            continue
        tid = _normalize_task_id(m.group(1))
        if tid:
            ids.add(tid)
    return ids


def cmd_lint_plans(args: argparse.Namespace) -> None:
    """Flag `**Status:** done`/`partial` tasks without matching commit pairings.

    Read-only. Scans every `*.md` file under --plans-dir, enumerates tasks
    via `_split_task_blocks`, and for each task whose Status bullet reads
    `done` or `partial` asserts both (a) a `commit_done` event exists in the
    run log with a matching task_id and (b) a `feat(TASK-NNN):` commit exists
    in the repo's git log (across all refs).

    Parent plans whose top-level Status is `superseded` are skipped per the
    §D.3 guidance: their decomposition is tracked by the superseding children.
    """
    plans_dir = Path(args.plans_dir).resolve()
    run_log_path = Path(args.run_log).resolve() if args.run_log else None
    git_dir = Path(args.git_dir or ".").resolve()

    findings: list[dict] = []
    scanned = 0
    done_tasks = 0

    commit_done_ids = (
        _load_commit_done_ids(run_log_path) if run_log_path else set()
    )
    feat_commit_ids = _load_feat_commit_ids(git_dir)

    # Anchor the relative path against plans_dir's parent so findings carry
    # `docs/plans/<file>.md` rather than a bare basename. Falls back to the
    # absolute path if the plan file is outside the plans-dir subtree
    # (shouldn't happen via rglob, but defensive).
    anchor = plans_dir.parent

    for md in sorted(plans_dir.rglob("*.md")):
        scanned += 1
        try:
            text = _load_text(md)
        except (OSError, UnicodeDecodeError):
            # Skip unreadable files silently — lint is best-effort.
            continue
        # Skip parent plans whose top-level Status is `superseded`. The
        # preamble is the slice of `text` before the first `### TASK-NNN:`
        # header; that is where plan-level `**Status:**` lives.
        preamble, blocks_for_check = _split_task_blocks(text)
        if _SUPERSEDED_HEADER_RE.search(preamble):
            continue
        for raw_id, block in blocks_for_check:
            status_m = _find_status_bullet(block)
            if not status_m:
                continue
            status = status_m.group(2).strip().lower()
            if status not in {"done", "partial"}:
                continue
            done_tasks += 1
            tid = _normalize_task_id(raw_id)
            if tid is None:
                continue
            try:
                rel_path = str(md.relative_to(anchor))
            except ValueError:
                rel_path = str(md)
            if tid not in commit_done_ids:
                findings.append({
                    "plan_file": rel_path,
                    "task_id": tid,
                    "code": "missing-commit-done-event",
                    "message": (
                        f"plan marks TASK-{tid} as {status!r} but no "
                        f"commit_done event found in run log"
                    ),
                })
            if tid not in feat_commit_ids:
                findings.append({
                    "plan_file": rel_path,
                    "task_id": tid,
                    "code": "missing-feat-commit",
                    "message": (
                        f"plan marks TASK-{tid} as {status!r} but no "
                        f"'feat(TASK-{tid}):' commit found"
                    ),
                })

    result = {
        "scanned": scanned,
        "done_tasks": done_tasks,
        "findings": findings,
    }
    _emit(args, result, exit_code=1 if findings else 0)


# ---------------------------------------------------------------------------
# Subcommand stubs
# ---------------------------------------------------------------------------


def _allowed_files_union(plan_text: str) -> dict[str, str]:
    """Return ``{path: task_id}`` for every file under any task's Files: bullet.

    TASK-008 scope-aware preflight classifier: the union of declared
    ``allowed_files`` across every task in the plan is consulted to decide
    whether a dirty tree path is inside *this* plan's scope (``plan_scope_dirty``)
    or a blocking out-of-scope edit (``source_blocking``). Duplicate files
    across tasks keep the last-wins attribution; a plan that declares the
    same file under two tasks is already a smell the analyst should flag.
    """
    result: dict[str, str] = {}
    _, blocks = _split_task_blocks(plan_text)
    for task_id, block in blocks:
        files = _extract_task_files_from_plan(plan_text, task_id)
        if not files:
            continue
        for path in files:
            if path:
                result[path] = task_id
    return result


def _is_preflight_always_ignored(path: str, plan_dir: str, plan_basename: str) -> bool:
    """Match `ALWAYS_IGNORE` / `ALWAYS_IGNORE_GLOBS` for preflight classifier.

    Covers the orchestrator-state paths from TASK-003: ``_run_log.jsonl``,
    ``_run_lock.json``, and per-plan schedule sidecars — both at the
    default ``docs/plans/`` layout and at any configured ``plan_dir``.
    Rooted in the shared `is_commit_always_ignore` predicate so the
    preflight ignore set stays aligned with the commit-safe ignore set.
    """
    if is_commit_always_ignore(path, plan_basename=plan_basename, plan_dir=plan_dir):
        return True
    # Per-configured-plan-dir run-log / run-lock (covers non-default layouts).
    pd = plan_dir.rstrip("/")
    if pd:
        if path == f"{pd}/_run_log.jsonl" or path == f"{pd}/_run_lock.json":
            return True
    return False


def cmd_preflight(args: argparse.Namespace) -> None:
    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})

    plan_text = _load_text(plan)
    scope = _allowed_files_union(plan_text)

    dirty: dict[str, list] = {
        "plan_doc": [],
        "orchestrator_state": [],
        "plan_scope_dirty": [],
        "source_blocking": [],
    }
    warnings: list[str] = []

    status = _git(["status", "--porcelain"])
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if path == str(plan) or path.endswith(plan.name):
            dirty["plan_doc"].append(path)
        elif _is_preflight_always_ignored(path, _PLAN_DIR_POSIX, plan.name):
            dirty["orchestrator_state"].append(path)
        elif path in scope:
            tid = scope[path]
            dirty["plan_scope_dirty"].append({"path": path, "task_id": tid})
            warnings.append(f"{path} is dirty and TASK-{tid} will write to it")
        else:
            dirty["source_blocking"].append(path)

    codex_available = shutil.which("codex") is not None

    sha_cp = _git(["rev-parse", "HEAD"])
    starting_sha = sha_cp.stdout.strip() or ""

    branch_cp = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = branch_cp.stdout.strip()

    base_m = re.search(r"^\*\*Base branch:\*\*\s*(\S+)\s*$", plan_text, re.MULTILINE)
    base_branch = base_m.group(1).strip() if base_m else None
    base_branch_match = base_branch is None or current_branch == base_branch

    pass_flag = len(dirty["source_blocking"]) == 0
    if getattr(args, "strict_scope", False) and dirty["plan_scope_dirty"]:
        pass_flag = False
    if args.strict_branch and not base_branch_match:
        pass_flag = False

    result = {
        "pass": pass_flag,
        "starting_sha": starting_sha,
        "run_id": _run_id(),
        "codex_available": codex_available,
        "dirty_files": dirty,
        "scope_warnings": warnings,
        "base_branch": base_branch,
        "current_branch": current_branch,
        "base_branch_match": base_branch_match,
        "python_path": _resolve_python(),
    }
    if not pass_flag:
        _die(args, result)
    _emit(args, result)


def cmd_parse_schedule(args: argparse.Namespace) -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _die(args, {"errors": [{
            "path": "$",
            "code": "json-decode",
            "message": f"json decode: {e}",
        }]})
    if not isinstance(data, dict):
        _die(args, {"errors": [{
            "path": "$",
            "code": "top-level-not-object",
            "message": "top-level schedule must be an object",
        }]})

    errors, warnings = _validate_schedule(data, strict_nested=bool(getattr(args, "strict", False)))

    # TASK-019: cycle + orphan-dep detection in the canonical parse-schedule
    # seam. Matches `cmd_batch_next` / `cmd_filter_schedule` behavior so
    # cycles cannot silently flow through the analyst → orchestrator handoff.
    if not errors:
        tasks_list = data.get("tasks") if isinstance(data.get("tasks"), list) else []
        batches_list = data.get("batches") if isinstance(data.get("batches"), list) else []
        errors.extend(_validate_schedule_dag(tasks_list, batches_list))

    # TASK-002: backfill gaps[i].severity on legacy schedules. Dict-shaped
    # gap entries without a `severity` field get populated via
    # classify_gap_severity and a single `warnings` entry is appended,
    # matching the shape used by the `task_id` → `id` alias warning above.
    raw_gaps = data.get("gaps", [])
    backfilled_gaps = raw_gaps
    if isinstance(raw_gaps, list):
        legacy_backfilled = False
        new_gaps: list = []
        for g in raw_gaps:
            if isinstance(g, dict) and "severity" not in g:
                gtype = g.get("type")
                severity = classify_gap_severity(gtype if isinstance(gtype, str) else "")
                g = {**g, "severity": severity}
                legacy_backfilled = True
            new_gaps.append(g)
        backfilled_gaps = new_gaps
        if legacy_backfilled:
            warnings.append(
                "schedule gaps[] missing 'severity' field; backfilled via "
                "classify_gap_severity (unknown types default to 'hard')"
            )

    result = {
        "outcome": data.get("outcome"),
        "tasks": data.get("tasks") if isinstance(data.get("tasks"), list) else [],
        "batches": data.get("batches") if isinstance(data.get("batches"), list) else [],
        "gaps": backfilled_gaps,
        "risks": data.get("risks", []),
        "warnings": warnings,
        "errors": errors,
    }
    if errors:
        _die(args, result)
    _emit(args, result)


def cmd_compute_schedule(args: argparse.Namespace) -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _die(args, {"errors": [{
            "path": "$",
            "code": "json-decode",
            "message": f"json decode: {e}",
        }]})
    if not isinstance(data, dict):
        _die(args, {"errors": [{
            "path": "$",
            "code": "top-level-not-object",
            "message": "top-level schedule must be an object",
        }]})

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        _die(args, {"errors": [{
            "path": "$.tasks",
            "code": "invalid-type",
            "message": "tasks must be an array",
        }]})

    topo, batches, errors = _compute_schedule_batches(tasks)
    result = {
        "tasks": tasks,
        "topo": topo,
        "batches": batches,
        "errors": errors,
        "warnings": [],
    }
    if errors:
        _die(args, result)
    _emit(args, result)


def cmd_write_schedule(args: argparse.Namespace) -> None:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _die(args, {"errors": [{
            "path": "$",
            "code": "json-decode",
            "message": f"json decode: {e}",
        }]})
    if not isinstance(data, dict):
        _die(args, {"errors": [{
            "path": "$",
            "code": "top-level-not-object",
            "message": "top-level schedule must be an object",
        }]})

    errors, warnings = _validate_schedule(data, strict_nested=bool(getattr(args, "strict", False)))
    if errors:
        _die(args, {"errors": errors, "warnings": warnings})

    path = Path(args.schedule_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    try:
        nbytes = _atomic_write_text(path, text)
    except OSError as e:
        _die(args, {"errors": [{
            "path": f"$.<file:{path}>",
            "code": "write-failed",
            "message": f"atomic write failed: {e}",
        }]})
    _emit(args, {"written": str(path), "bytes": nbytes, "warnings": warnings})


def cmd_batch_next(args: argparse.Namespace) -> None:
    sched_path = Path(args.schedule_file)
    if not sched_path.is_file():
        _die(args, {"error": f"schedule file not found: {sched_path}"})
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(args, {"error": f"schedule json decode: {e}"})
    if not isinstance(data, dict):
        _die(args, {"errors": [{
            "path": "$",
            "code": "top-level-not-object",
            "message": "top-level schedule must be an object",
        }]})

    errors, warnings = _validate_schedule(data)
    if errors:
        _die(args, {"errors": errors, "warnings": warnings})

    def _split_csv(raw: str) -> list[str]:
        return [s for s in (raw or "").split(",") if s]

    locked = set(_split_csv(args.locked_files))
    done = set(_split_csv(args.done))
    failed = set(_split_csv(args.failed))

    tasks_by_id: dict[str, dict] = {}
    for t in data.get("tasks") or []:
        raw_tid = t.get("id") if "id" in t else t.get("task_id")
        tid = _normalize_task_id(str(raw_tid))
        if tid:
            tasks_by_id[tid] = t

    # Defensive DAG check (ISSUE-019; TASK-019 refactor). `_validate_schedule`
    # above covers shape and reference integrity, but does not catch cycles or
    # orphan deps. Delegates to the shared `_validate_schedule_dag` helper so
    # `parse-schedule`, `batch-next`, and `filter-schedule` emit identical
    # `dependency-cycle` / `unknown-dependency` payloads.
    dag_errors = _validate_schedule_dag(
        list(data.get("tasks") or []),
        list(data.get("batches") or []),
    )
    if dag_errors:
        _die(args, {"errors": dag_errors})

    def _files(task: dict) -> list[str]:
        return list(task.get("files") or [])

    def _ready(task: dict) -> bool:
        """A task is ready iff every declared dep is in `done`.

        Tasks with any dep in `failed` are NOT ready — V14 invariant. Unknown
        or not-yet-done deps also block readiness. Dep references that do not
        normalize (malformed) are ignored for readiness purposes but would
        have been caught by `_validate_schedule_refs` upstream.
        """
        for dep in (task.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None:
                continue
            if dep_norm not in done:
                return False
        return True

    remaining = [t for tid, t in tasks_by_id.items() if tid not in done and tid not in failed]
    ready = [t for t in remaining if _ready(t)]

    # 1. Find active batch: the FIRST batch whose tasks are NOT all
    # done|failed. The `or tid in failed` clause is MANDATORY — without it,
    # a mixed-resolution batch (one done + one failed) never advances and
    # blocks dependents forever. See V4 / Run 20260415T022232 regression.
    active_batch: dict | None = None
    for b in (data.get("batches") or []):
        bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
        bids = [tid for tid in bids if tid]
        if not bids:
            continue
        if all(tid in done or tid in failed for tid in bids):
            continue
        active_batch = b
        break

    def _batch_index_of(b: dict) -> int:
        raw = b.get("index") if "index" in b else b.get("batch_index")
        return raw if isinstance(raw, int) else 0

    if active_batch is None:
        # Guard against malformed batch structure masquerading as "all done"
        # (V11/V12/V13). Reasons active_batch may be None while work remains:
        #   * batches=[] entirely,
        #   * every batch has empty task_ids[] (or all entries fail to
        #     normalize),
        #   * an unresolved task is not listed in any batch.
        # Policy lock-in: surface as scheduler_stuck=True, never silently
        # succeed — the orchestrator relies on this to halt with a
        # diagnostic.
        unresolved = [tid for tid in tasks_by_id
                      if tid not in done and tid not in failed]
        if unresolved:
            _emit(args, {
                "batch_index": 0,
                "task_ids": [],
                "file_locks": [],
                "scheduler_stuck": True,
            })
            return
        _emit(args, {
            "batch_index": 0,
            "task_ids": [],
            "file_locks": [],
            "scheduler_stuck": False,
        })
        return

    active_ids = {tid for tid in (
        _normalize_task_id(str(x))
        for x in (active_batch.get("task_ids") or [])
    ) if tid}

    # Guard: active_batch was selected because not-all-done|failed, but if
    # active_ids is empty after normalization, treat as malformed batch data
    # and surface scheduler_stuck — never silently succeed.
    if not active_ids:
        _emit(args, {
            "batch_index": _batch_index_of(active_batch),
            "task_ids": [],
            "file_locks": [],
            "scheduler_stuck": True,
        })
        return

    # 2. Restrict ready candidates to the active batch. Tasks in later
    # batches are never selected even when globally ready.
    ready_in_batch: list[dict] = []
    for t in ready:
        raw_tid = t.get("id") if "id" in t else t.get("task_id")
        tid = _normalize_task_id(str(raw_tid))
        if tid in active_ids:
            ready_in_batch.append(t)

    # 3. Pick respecting file locks + --parallel.
    picked: list[str] = []
    picked_files: list[str] = []
    claimed = set(locked)
    for t in ready_in_batch:
        raw_tid = t.get("id") if "id" in t else t.get("task_id")
        tid = _normalize_task_id(str(raw_tid))
        files = _files(t)
        if any(f in claimed for f in files):
            continue
        if len(picked) >= max(1, args.parallel):
            break
        picked.append(tid)
        picked_files.extend(files)
        claimed.update(files)

    # 4. scheduler_stuck — cross-batch-deadlock-aware. True iff nothing was
    # picked AND the active batch still has at least one unfinished task
    # (not in done|failed). This covers:
    #   (a) ready_in_batch non-empty but every candidate is file-locked;
    #   (b) ready_in_batch empty because the active batch's unfinished
    #       tasks have unsatisfied deps in a later batch (cross-batch
    #       deadlock).
    # See V3 / Run 20260415T000811. The FORBIDDEN formulae
    # `len(picked)==0 and len(ready_in_batch)>0` (masks case b) and
    # `len(picked)==0 and len(ready)>0` (uses global ready) must not be
    # shipped.
    unfinished_active = [tid for tid in active_ids
                         if tid not in done and tid not in failed]
    scheduler_stuck = (len(picked) == 0) and (len(unfinished_active) > 0)

    _emit(args, {
        "batch_index": _batch_index_of(active_batch),
        "task_ids": picked,
        "file_locks": picked_files,
        "scheduler_stuck": scheduler_stuck,
    })


def cmd_filter_schedule(args: argparse.Namespace) -> None:
    sched_path = Path(args.schedule_file)
    if not sched_path.is_file():
        _die(args, {"errors": [{
            "path": "$",
            "code": "file-not-found",
            "message": f"schedule file not found: {sched_path}",
        }]})
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(args, {"errors": [{
            "path": "$",
            "code": "json-decode",
            "message": f"schedule json decode: {e}",
        }]})
    if not isinstance(data, dict):
        _die(args, {"errors": [{
            "path": "$",
            "code": "top-level-not-object",
            "message": "top-level schedule must be an object",
        }]})

    # 1. Source-schedule validation (mirror cmd_batch_next:1504-1506).
    errors, warnings = _validate_schedule(data)
    if errors:
        _die(args, {"errors": errors, "warnings": warnings})

    # 2. Source-not-valid rejection (V4). Filtering an already-broken schedule
    # is meaningless — the orchestrator should surface the source outcome
    # instead.
    if data.get("outcome") != "valid":
        _die(args, {"errors": [{
            "path": "$.outcome",
            "code": "source-not-valid",
            "message": (
                f"filter-schedule requires source outcome='valid', "
                f"got {data.get('outcome')!r}"
            ),
        }]})

    # 3. --task-ids parse + normalize. Empty fragments (e.g. "1,,3") are
    # skipped silently; all-empty input is a hard error.
    raw_ids = [s.strip() for s in (args.task_ids or "").split(",") if s.strip()]
    requested: list[str] = []
    for r in raw_ids:
        norm = _normalize_task_id(r)
        if norm is None:
            _die(args, {"errors": [{
                "path": "$.task_ids",
                "code": "invalid-task-ids",
                "message": f"could not normalize task id {r!r}",
            }]})
        requested.append(norm)
    if not requested:
        _die(args, {"errors": [{
            "path": "$.task_ids",
            "code": "invalid-task-ids",
            "message": "no task ids provided",
        }]})

    # Build tasks_by_id using the alias-tolerant pattern (the legacy
    # `task_id` field is valid per _validate_schedule, just warned).
    tasks_by_id: dict[str, dict] = {}
    for t in data.get("tasks") or []:
        raw_tid = t.get("id") if "id" in t else t.get("task_id")
        norm = _normalize_task_id(str(raw_tid)) if raw_tid is not None else None
        if norm:
            tasks_by_id[norm] = t

    # 4. Unknown requested id (V2). Case 1: "user typo".
    unknown = [tid for tid in requested if tid not in tasks_by_id]
    if unknown:
        _die(args, {"errors": [{
            "path": "$.task_ids",
            "code": "unknown-task-id",
            "message": f"unknown task id {tid}",
        } for tid in unknown]})

    # 5. Transitive closure with guarded indexing (V1, V3). Case 2: "schedule
    # is broken upstream" — a transitive dep that's absent from tasks[] MUST
    # produce a structured `missing-dependency` error, NOT a Python
    # KeyError. The guard on tasks_by_id.get(tid) is defensive in case
    # step 4 was bypassed; the real missing-dep check is on each dep ref.
    closed: set[str] = set()
    stack = list(requested)
    while stack:
        tid = stack.pop()
        if tid in closed:
            continue
        closed.add(tid)
        task = tasks_by_id.get(tid)
        if task is None:
            # Defensive: step 4 should have caught this for requested IDs;
            # for derived ones the dep-ref check below handles it first.
            _die(args, {"errors": [{
                "path": "$.tasks",
                "code": "missing-dependency",
                "message": f"task depends on missing id {tid}",
            }]})
        for dep in (task.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None or dep_norm not in tasks_by_id:
                _die(args, {"errors": [{
                    "path": f"$.tasks[id={tid}].dependencies",
                    "code": "missing-dependency",
                    "message": f"task {tid} depends on missing id {dep!r}",
                }]})
            stack.append(dep_norm)

    # 6. Build output tasks/batches in source order. Drop batches whose
    # task_ids become empty post-filter (V8); filter retained batch task_ids
    # to the closed set (V10); preserve original `index` values (V9).
    def _tid_of(t: dict) -> str | None:
        raw = t.get("id") if "id" in t else t.get("task_id")
        return _normalize_task_id(str(raw)) if raw is not None else None

    out_tasks = [t for t in (data.get("tasks") or []) if _tid_of(t) in closed]
    out_batches: list[dict] = []
    for b in (data.get("batches") or []):
        bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
        keep = [x for x in bids if x in closed]
        if keep:
            out_batches.append({**b, "task_ids": keep})

    # 7. DAG defensive check on the filtered subgraph (V6, ISSUE-019;
    # TASK-019 refactor). parse-schedule is the primary cycle detector, but
    # filter-schedule MUST also guard against a cycle surviving into its
    # output because the orchestrator pipes our stdout straight to
    # write-schedule. Delegates to the shared `_validate_schedule_dag` helper.
    dag_errors = _validate_schedule_dag(out_tasks, out_batches)
    if dag_errors:
        _die(args, {"errors": dag_errors})

    # 8. Reference-integrity check on the filtered schedule.
    ref_errors = _validate_schedule_refs(out_tasks, out_batches)
    if ref_errors:
        _die(args, {"errors": ref_errors})

    # 9. Emit canonical schedule. gaps=[] and risks=[] are intentional —
    # inheriting source-level gaps/risks would either contradict
    # outcome=valid (per _validate_schedule:381-386) or carry stale
    # references to filtered-out tasks. Success stdout MUST contain ONLY
    # these five keys so write-schedule --stdin accepts the output
    # byte-for-byte (V11, V12, V13). Do NOT add warnings/errors/other
    # metadata on the success path.
    _emit(args, {
        "outcome": "valid",
        "tasks": out_tasks,
        "batches": out_batches,
        "gaps": [],
        "risks": [],
    })


def cmd_parse_implementer_report(args: argparse.Namespace) -> None:
    raw = sys.stdin.read()
    warnings: list[str] = []
    diagnostics: list[dict] = []

    def _field(label: str) -> str | None:
        m = re.search(rf"^\*\*{re.escape(label)}:\*\*\s*(.+?)\s*$", raw, re.MULTILINE)
        return m.group(1).strip() if m else None

    def _has_header(label: str) -> bool:
        return re.search(rf"^\*\*{re.escape(label)}:\*\*", raw, re.MULTILINE) is not None

    outcome = _field("Outcome")
    if not outcome:
        _die(args, {"error": "report missing **Outcome:** line"})
    outcome = outcome.lower()

    allowed = {"success", "partial", "failed", "plan-incorrect", "blocked", "malformed"}
    if outcome not in allowed:
        _die(args, {"error": f"unknown outcome {outcome!r}; expected one of {sorted(allowed)}"})

    def _list_section(label: str) -> list[str]:
        pat = rf"^\*\*{re.escape(label)}:\*\*\s*\n((?:[ \t]*-[^\n]*\n?)+)"
        m = re.search(pat, raw, re.MULTILINE)
        if not m:
            return []
        return [ln.strip().lstrip("-").strip() for ln in m.group(1).splitlines() if ln.strip()]

    files_changed = _list_section("Files changed")

    concerns = _list_section("Concerns for reviewer")
    if not concerns:
        legacy = _list_section("Concerns")
        if legacy:
            concerns = legacy
            warnings.append(
                "implementer report used legacy 'Concerns:' label; "
                "canonical is 'Concerns for reviewer:'"
            )

    plan_adaptations = _list_section("Plan adaptations")

    if not _has_header("Plan adaptations"):
        diagnostics.append({
            "code": "missing-plan-adaptations",
            "message": (
                "**Plan adaptations:** section header is mandatory per "
                "plan-implementer contract"
            ),
        })
    if outcome not in {"failed", "blocked"} and not _has_header("Concerns for reviewer"):
        diagnostics.append({
            "code": "missing-concerns-for-reviewer",
            "message": (
                "**Concerns for reviewer:** section header is mandatory per "
                "plan-implementer contract"
            ),
        })

    diff_summary = ""
    ds_m = re.search(r"\*\*Diff summary:\*\*\s*\n(.+?)(?=\n\n|\n\*\*|\Z)", raw, re.DOTALL)
    if ds_m:
        diff_summary = ds_m.group(1).strip()

    test_outcome = _field("Test outcome") or "not-run"
    reversion = None
    rv_m = re.search(
        r"(?:On failure[^\n]*|Reversion guidance):\s*\n(.+?)(?=\n\n|\n\*\*|\Z)",
        raw,
        re.DOTALL | re.IGNORECASE,
    )
    if rv_m:
        reversion = rv_m.group(1).strip()

    result = {
        "outcome": outcome,
        "files_changed": files_changed,
        "diff_summary": diff_summary,
        "test_outcome": test_outcome,
        "concerns": concerns,
        "plan_adaptations": plan_adaptations,
        "warnings": warnings,
        "diagnostics": diagnostics,
    }
    if reversion:
        result["reversion_guidance"] = reversion
    _emit(args, result)


def _validate_plan_review_finding(item: object, *, path: str) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(item, dict):
        return [{
            "path": path,
            "code": "invalid-plan-review-finding",
            "message": "plan-review finding must be an object",
        }]
    required = {
        "severity": str,
        "section": str,
        "concern": str,
        "suggested_change": str,
    }
    for key, typ in required.items():
        if key not in item:
            errors.append({
                "path": f"{path}.{key}",
                "code": "missing-plan-review-finding-field",
                "message": f"plan-review finding missing field {key!r}",
            })
            continue
        value = item[key]
        if not isinstance(value, typ):
            errors.append({
                "path": f"{path}.{key}",
                "code": "invalid-plan-review-finding-field",
                "message": (
                    f"plan-review finding field {key!r} must be a "
                    f"{typ.__name__}"
                ),
            })
    severity = item.get("severity")
    if (
        isinstance(severity, str)
        and severity not in ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES
    ):
        errors.append({
            "path": f"{path}.severity",
            "code": "invalid-plan-review-finding-severity",
            "message": (
                f"plan-review finding severity must be one of "
                f"{sorted(ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES)}, "
                f"got {severity!r}"
            ),
        })
    for key in item.keys():
        if key not in required:
            errors.append({
                "path": f"{path}.{key}",
                "code": "unknown-plan-review-finding-field",
                "message": (
                    f"plan-review finding has unknown field {key!r}"
                ),
            })
    return errors


def _validate_plan_review_parsed(parsed: object) -> list[dict]:
    """Validate the `parsed` body of a plan-review envelope against the
    codex_plan_review_schema.json contract. Returns canonical `errors[*]`."""
    errors: list[dict] = []
    if not isinstance(parsed, dict):
        return [{
            "path": "$.parsed",
            "code": "invalid-type",
            "message": "parsed must be an object",
        }]

    required = {
        "plan_file": str,
        "verdict": str,
        "findings": list,
        "schedule_ok": bool,
        "summary": str,
    }
    for key, typ in required.items():
        if key not in parsed:
            errors.append({
                "path": f"$.parsed.{key}",
                "code": "missing-field",
                "message": f"parsed missing field {key!r}",
            })
            continue
        value = parsed[key]
        if typ is bool:
            ok = isinstance(value, bool)
        elif typ is list:
            ok = isinstance(value, list)
        else:
            ok = isinstance(value, typ)
        if not ok:
            errors.append({
                "path": f"$.parsed.{key}",
                "code": "invalid-type",
                "message": (
                    f"parsed field {key!r} must be a {typ.__name__}"
                ),
            })

    verdict = parsed.get("verdict")
    if (
        isinstance(verdict, str)
        and verdict not in ALLOWED_PLAN_REVIEW_VERDICTS
    ):
        errors.append({
            "path": "$.parsed.verdict",
            "code": "invalid-plan-review-verdict",
            "message": (
                f"verdict must be one of "
                f"{sorted(ALLOWED_PLAN_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    findings = parsed.get("findings")
    if isinstance(findings, list):
        for i, item in enumerate(findings):
            errors.extend(
                _validate_plan_review_finding(
                    item, path=f"$.parsed.findings[{i}]",
                )
            )

    allowed_keys = set(required.keys())
    for key in parsed.keys():
        if key not in allowed_keys:
            errors.append({
                "path": f"$.parsed.{key}",
                "code": "unknown-parsed-field",
                "message": f"parsed has unknown field {key!r}",
            })
    return errors


def cmd_parse_plan_review_report(args: argparse.Namespace) -> None:
    """Validate a Phase 1.5 Codex plan-review envelope from stdin.

    Input: full JSON envelope emitted by
    `plan_codex_dispatch.py plan-review`. Expected shape (minimum):
        {
          "plan_file": "...",
          "subcommand": "plan-review",
          "outcome": "success" | "failure" | "timeout" | "parse_error",
          "parsed": { ... },        # validated against codex_plan_review_schema
          ...
        }

    Exits non-zero with canonical `errors[*]` on schema violations so the
    orchestrator can halt the run before Phase 2. Successful validation
    extracts `{plan_file, verdict, findings_count, findings, summary,
    schedule_ok}` for the caller. Cross-plan dependency resolution is
    verified by the orchestrator in Phase 0 preflight; the reviewer no
    longer reports on it.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        _die(args, {"errors": [{
            "path": "$",
            "code": "empty-stdin",
            "message": "parse-plan-review-report expects a JSON envelope on stdin",
        }]})

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(args, {"errors": [{
            "path": "$",
            "code": "json-decode",
            "message": f"stdin is not valid JSON: {exc}",
        }]})

    if not isinstance(envelope, dict):
        _die(args, {"errors": [{
            "path": "$",
            "code": "invalid-type",
            "message": "envelope must be a JSON object",
        }]})

    # Envelope-level validation MUST run before the terminal-outcome shortcut
    # so a malformed envelope (e.g. `subcommand:"review"`) can't slip through
    # as a "degradation" signal. The terminal-outcome branch is only a
    # structural exemption from the `parsed`-body schema, not from envelope
    # contract checks.
    subcommand = envelope.get("subcommand")
    if subcommand != "plan-review":
        if subcommand is None:
            msg = (
                "envelope subcommand missing — did you pipe only the inner "
                "`parsed` object? Expected full wrapper envelope with "
                "top-level `task_id`, `subcommand`, `outcome`, and `parsed`."
            )
        else:
            msg = (
                f"envelope subcommand must be 'plan-review', got "
                f"{subcommand!r}"
            )
        _die(args, {"errors": [{
            "path": "$.subcommand",
            "code": "invalid-subcommand",
            "message": msg,
        }]})

    errors: list[dict] = []

    outcome = envelope.get("outcome")
    # Only success envelopes carry a schema-compliant parsed body.
    # Non-success outcomes (failure/timeout/parse_error) are permitted
    # structurally; the caller branches on outcome + error before reading
    # verdict.
    terminal_outcomes = {"failure", "timeout", "parse_error", "scope_violation"}
    if outcome in terminal_outcomes:
        result: dict = {
            "plan_file": envelope.get("plan_file") or envelope.get("task_id"),
            "outcome": outcome,
            "verdict": None,
            "findings_count": 0,
            "findings": [],
            "summary": "",
            "schedule_ok": None,
            "errors": [],
            "envelope_error": envelope.get("error"),
        }
        _emit(args, result)
        return

    if outcome != "success":
        errors.append({
            "path": "$.outcome",
            "code": "invalid-outcome",
            "message": (
                f"envelope outcome must be 'success' for a parseable plan "
                f"review; got {outcome!r}"
            ),
        })

    parsed = envelope.get("parsed")
    if parsed is None:
        errors.append({
            "path": "$.parsed",
            "code": "missing-field",
            "message": "envelope is missing required field 'parsed'",
        })
    else:
        errors.extend(_validate_plan_review_parsed(parsed))

    if errors:
        _die(args, {"errors": errors})

    assert isinstance(parsed, dict)
    findings = parsed.get("findings") or []
    result = {
        "plan_file": parsed.get("plan_file"),
        "outcome": outcome,
        "verdict": parsed.get("verdict"),
        "findings_count": len(findings),
        "findings": findings,
        "summary": parsed.get("summary", ""),
        "schedule_ok": parsed.get("schedule_ok"),
        "errors": [],
    }
    _emit(args, result)


def cmd_parse_d5_adjudication(args: argparse.Namespace) -> None:
    """Validate a D.5 adjudication payload from stdin per TASK-016A.

    Input: single JSON object
        {
          "verdict": "ship" | "ship-with-fixes" | "partial-agreement" | "needs-rework",
          "summary": "...",
          # required when verdict == "partial-agreement":
          "load_bearing": [0, 2],
          "dismissed":    [1, 3]
        }

    The `--codex-findings-count` flag is the length of the Codex
    `parsed.findings[]` array the D.5 reviewer was adjudicating;
    partial-agreement indices MUST stay within `range(0, count)`.

    On success emits the structured dispatch payload the orchestrator
    forwards to D.2a.6: `{verdict, summary, load_bearing, dismissed,
    errors:[]}`. The split fields are only present (as arrays) on
    `partial-agreement`; other verdicts leave them as `null` for
    explicit routing.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        _die(args, {"errors": [{
            "path": "$",
            "code": "empty-stdin",
            "message": "parse-d5-adjudication expects a JSON payload on stdin",
        }]})

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(args, {"errors": [{
            "path": "$",
            "code": "json-decode",
            "message": f"stdin is not valid JSON: {exc}",
        }]})

    count = args.codex_findings_count
    if count < 0:
        _die(args, {"errors": [{
            "path": "$",
            "code": "invalid-findings-count",
            "message": (
                f"--codex-findings-count must be non-negative, got {count}"
            ),
        }]})

    errors = _validate_d5_adjudication_payload(
        payload, codex_findings_count=count,
    )
    if errors:
        _die(args, {"errors": errors})

    assert isinstance(payload, dict)
    verdict = payload.get("verdict")
    load_bearing = (
        payload.get("load_bearing")
        if verdict == "partial-agreement"
        else None
    )
    dismissed = (
        payload.get("dismissed")
        if verdict == "partial-agreement"
        else None
    )
    result = {
        "verdict": verdict,
        "summary": payload.get("summary", ""),
        "load_bearing": load_bearing,
        "dismissed": dismissed,
        "errors": [],
    }
    _emit(args, result)


def cmd_commit_task(args: argparse.Namespace) -> None:
    tid = _normalize_task_id(args.task_id)
    if not tid:
        _die(args, {"error": f"bad --task-id: {args.task_id!r}"})

    files = [f.strip() for f in args.files.split(",") if f.strip()]
    if not files:
        _die(args, {"error": "--files must list at least one file"})

    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})

    try:
        minor = json.loads(args.reviewer_minor_findings) if args.reviewer_minor_findings else []
    except json.JSONDecodeError as e:
        _die(args, {"error": f"invalid --reviewer-minor-findings: {e}"})
    review_errors = _validate_review_success_payload(
        args.reviewer,
        args.reviewer_verdict,
        minor,
    )
    if review_errors:
        _die(args, {"errors": review_errors})

    original_plan = _load_text(plan)
    try:
        mutated, _prior = mutate_task_status(original_plan, tid, "done")
    except ValueError as e:
        _die(args, {"error": f"status mutation: {e}"})

    if args.dry_run:
        _emit(args, {
            "dry_run": True,
            "task_id": tid,
            "files": files,
            "would_commit": True,
        })

    # TASK-020B: opt-in `acceptance_v_check` YAML frontmatter runs the plan's
    # own declared V-check pre-commit. Runs AFTER the `--files` staging guard
    # (the `--files` validation above) but BEFORE the plan-status flip
    # (`_write_text` below). On failure we `_die` silently — no plan text
    # written, no git state touched, no `commit_done` event. Plans without
    # frontmatter or without the key: zero behavior change.
    fm = _parse_frontmatter(original_plan)
    v_check_cmd = fm.get("acceptance_v_check")
    if v_check_cmd:
        timeout = getattr(args, "v_check_timeout", None) or 300
        # `cmd_commit_task` has no `--git-dir` flag — the caller's CWD is the
        # repo root, matching the semantics of `_git()` above (which also
        # runs with no explicit cwd). Using `Path(".")` keeps the V-check
        # execution context consistent with the surrounding git operations.
        v_result = _run_v_check(str(v_check_cmd), Path("."), timeout)
        if v_result.get("code") != "v-check-passed":
            _die(args, {
                "errors": [{
                    "code": v_result["code"],
                    "message": v_result["message"],
                    "stdout_tail": v_result["stdout_tail"],
                    "stderr_tail": v_result["stderr_tail"],
                }],
            })
        # Log pass event BEFORE the commit so the audit record captures the
        # V-check outcome even if a later step (e.g., `git commit`) fails.
        # Per the annotation: `v_check_passed` is an audit event, NOT proof
        # of task completion — pairing proof remains `commit_done`.
        _append_run_log("v_check_passed", {
            "run_id": args.run_id,
            "task_id": tid,
            "command": str(v_check_cmd),
        })

    _write_text(plan, mutated)

    # TASK-016C (post-remediation): dismissed-finding-ids content parsing
    # lives in main() post-parse so empty/non-integer tokens fail via
    # parser.error() (exit 2) before any plan mutation. By the time we
    # reach the handler, args.dismissed_finding_ids is already a
    # list[int] (possibly empty) — see main().
    dismissed_ids: list[int] = list(
        getattr(args, "dismissed_finding_ids", []) or []
    )

    commit_msg = (
        f"feat(TASK-{tid}): {args.title}\n\n"
        f"{args.diff_summary}\n\n"
        f"Plan: {plan.name}\n"
    )
    if getattr(args, "remediation_tag", False):
        # D.2a.5 post-remediation commit: a trailing [remediation] tag so the
        # run summary and `git log --oneline` can distinguish retries from
        # clean first-pass commits. Kept on its own line adjacent to any
        # [disagreement] tag that D.2a might have already appended upstream.
        commit_msg = commit_msg.rstrip("\n") + "\n\n[remediation]\n"
    if getattr(args, "narrow_remediation_tag", False):
        # TASK-016C D.2a.6 post-narrow-remediation commit: a trailing
        # [narrow-remediation] tag plus a [disagreement: i,j] trailer
        # listing the dismissed finding indices, on adjacent lines with
        # [narrow-remediation] first. Distinct from D.2a.5's
        # [remediation] tag so `git log --oneline` can distinguish the
        # narrow retry from the full-rework retry. Argparse has already
        # excluded --remediation-tag and --disagreement-tag.
        trailer_ids = ",".join(str(i) for i in dismissed_ids)
        commit_msg = (
            commit_msg.rstrip("\n")
            + f"\n\n[narrow-remediation]\n[disagreement: {trailer_ids}]\n"
        )

    # TASK-014B — roster auto-update must happen BEFORE `git add` so the
    # updated `00_INDEX.json` is included in the same commit as the plan
    # status bullet and implementation files. Otherwise the roster change
    # is left as an uncommitted working-tree mutation, which is exactly
    # the drift this task was supposed to prevent. On `invalid-index` or
    # `missing-entry` we restore the plan text and exit with the structured
    # error before any git state changes. A missing `00_INDEX.json` is a
    # no-op — legacy plans that predate the roster MUST keep committing.
    index_path = plan.parent / "00_INDEX.json"
    # Capture pre-update roster bytes so downstream git failures can roll
    # the roster write back alongside the plan text. `None` means the file
    # did not exist beforehand — rollback in that branch is an unlink.
    original_index_bytes: bytes | None
    try:
        original_index_bytes = index_path.read_bytes()
    except FileNotFoundError:
        original_index_bytes = None

    outcome, err = _update_index_status(index_path, plan.name, "Done")
    if outcome in ("invalid-index", "missing-entry"):
        assert err is not None
        _write_text(plan, original_plan)
        _die(args, {"errors": [err]})

    def _restore_roster() -> None:
        """Roll the roster back through the same atomic pattern as the
        forward write (tempfile + os.replace). A direct `write_bytes`
        here would re-introduce a non-atomic write path — an interrupted
        rollback could truncate `00_INDEX.json`, which is exactly the
        failure mode V6's atomicity requirement forbids.
        """
        if outcome != "updated":
            return
        if original_index_bytes is None:
            try:
                index_path.unlink()
            except FileNotFoundError:
                pass
            return
        parent = index_path.parent
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(parent),
                prefix=".00_INDEX.rollback.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp.write(original_index_bytes)
                tmp_name = tmp.name
            os.replace(tmp_name, index_path)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    add_files = files + [str(plan)]
    if outcome == "updated":
        # `commit-task` is the sole authority for staging `00_INDEX.json`
        # on behalf of the orchestrator -- the roster update is not part
        # of any task's declared `Files:` list. Bind the pre-commit
        # staging seam to the post-commit `commit-safe` gate by
        # asserting the index path resolves to a member of the shared
        # always-ignore set via `is_commit_always_ignore` (with
        # `plan_dir` passed so bundles outside `docs/plans/` still
        # match). Failure here means the set and the staging logic
        # diverged, which would re-open the drift `commit-safe` is
        # meant to prevent; treat as an internal-error halt.
        try:
            rel_index = str(
                index_path.resolve().relative_to(Path.cwd().resolve())
            ).replace(os.sep, "/")
            rel_plan_dir: str | None = str(
                plan.resolve().parent.relative_to(Path.cwd().resolve())
            ).replace(os.sep, "/")
            if rel_plan_dir == ".":
                rel_plan_dir = ""
        except (ValueError, OSError):
            rel_index = index_path.as_posix()
            rel_plan_dir = None
        if not is_commit_always_ignore(
            rel_index, plan.name, rel_plan_dir,
        ):
            _write_text(plan, original_plan)
            _restore_roster()
            _die(args, {
                "error": (
                    "internal: 00_INDEX.json staged path "
                    f"{rel_index!r} is not in the shared "
                    "COMMIT_ALWAYS_IGNORE set; commit-task and "
                    "_gate_commit_safe would diverge"
                ),
            })
        add_files.append(str(index_path))
    add = _git(["add", "--", *add_files])
    if add.returncode != 0:
        _write_text(plan, original_plan)
        _restore_roster()
        _die(args, {"error": f"git add failed: {add.stderr.strip()}"})

    commit = _git(["commit", "-m", commit_msg, "--only", "--", *add_files])
    if commit.returncode != 0:
        _git(["reset", "HEAD", "--", *add_files])
        _write_text(plan, original_plan)
        _restore_roster()
        _die(args, {"error": f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"})

    sha_cp = _git(["rev-parse", "HEAD"])
    commit_sha = sha_cp.stdout.strip()

    event_fields = {
        "run_id": args.run_id,
        "task_id": tid,
        "commit_sha": commit_sha,
        "files": files,
        "reviewer_verdict": args.reviewer_verdict,
        "minor_findings_count": len(minor),
        # TASK-022: persist the full reviewer minor-findings payload on
        # every `commit_done` event so audits months later can retrieve
        # exactly what was flagged (and, via any `disposition` fields,
        # why it was dismissed/accepted/deferred). Key is always present:
        # an empty `--reviewer-minor-findings '[]'` yields `findings: []`.
        "findings": minor,
        "disagreement_tag": bool(args.disagreement_tag),
        "remediation_tag": bool(getattr(args, "remediation_tag", False)),
        # TASK-016C: surface the D.2a.6 flags in commit_done so the run
        # summary and downstream auditing can distinguish narrow
        # remediations from full D.2a.5 retries without re-parsing the
        # commit body.
        "narrow_remediation_tag": bool(
            getattr(args, "narrow_remediation_tag", False)
        ),
        "dismissed_finding_ids": dismissed_ids,
    }
    _append_run_log("commit_done", event_fields)

    _emit(args, {
        "commit_sha": commit_sha,
        "status_updated": True,
        "log_appended": True,
    })


def _is_inside_submodule(abs_path: Path, rel: str, repo_root: Path) -> bool:
    """True if ``abs_path`` is a gitlink OR sits inside a nested git repo
    distinct from ``repo_root``.

    Covers the nonexistent-leaf case: for ``submods/foo/new.txt`` where
    ``new.txt`` does not yet exist but ``submods/foo`` is a gitlink, the
    probe walks up from ``abs_path.parent`` to the nearest existing ancestor
    and runs ``git rev-parse --show-toplevel`` there. If that toplevel
    differs from ``repo_root``, the path is inside a submodule.
    """
    probe_gitlink = _git(
        ["ls-files", "--stage", "--", rel],
        cwd=repo_root,
    )
    if probe_gitlink.returncode == 0 and probe_gitlink.stdout.startswith("160000"):
        return True
    # Walk up to the nearest existing ancestor so nonexistent leaves under
    # a submodule still classify correctly.
    probe_dir = abs_path if abs_path.exists() else None
    if probe_dir is None:
        cursor = abs_path.parent
        try:
            repo_root_resolved = repo_root.resolve()
        except OSError:
            return False
        while cursor != cursor.parent:
            try:
                if cursor.resolve() == repo_root_resolved:
                    break
            except OSError:
                break
            if cursor.exists():
                probe_dir = cursor
                break
            cursor = cursor.parent
    if probe_dir is None:
        return False
    if probe_dir.is_file() or probe_dir.is_symlink():
        probe_dir = probe_dir.parent
    tl = _git(["rev-parse", "--show-toplevel"], cwd=probe_dir)
    if tl.returncode != 0:
        return False
    try:
        toplevel = Path(tl.stdout.strip()).resolve()
    except OSError:
        return False
    try:
        return toplevel != repo_root.resolve()
    except OSError:
        return False


def cmd_block_dependents(args: argparse.Namespace) -> None:
    """Cascade `blocked` status onto dependents of a failed task.

    TASK-004D / ISSUE-012: mutate the plan markdown (source of truth) as well
    as append run-log events (observability). Single plan read, single plan
    write; ordering is mutate-all-in-memory → single plan write → log each
    applied id. See TASK-004D_block_dependents_mutation.md for the full
    failure-stage semantics and double-failure precedence contract.
    """
    sched_path = Path(args.schedule_file)
    plan_path = Path(args.plan_file)
    if not sched_path.is_file():
        _die(args, {"error": f"schedule file not found: {sched_path}"})
    if not plan_path.is_file():
        _die(args, {"error": f"plan file not found: {plan_path}"})
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(args, {"error": f"schedule json decode: {e}"})

    failed_id = _normalize_task_id(args.failed)
    if not failed_id:
        _die(args, {"error": f"cannot normalize --failed: {args.failed!r}"})

    # ---- PHASE 1: compute cascade (BFS; sibling order = tasks[] order) ----
    blocked: list[str] = []
    queue = [failed_id]
    seen = set(queue)
    tasks = data.get("tasks") or []
    while queue:
        cur = queue.pop(0)
        for t in tasks:
            raw_tid = t.get("id") if "id" in t else t.get("task_id")
            if raw_tid is None:
                continue
            tid = _normalize_task_id(str(raw_tid))
            if not tid or tid in seen:
                continue
            deps = [
                _normalize_task_id(str(d))
                for d in (t.get("dependencies") or [])
            ]
            if cur in deps:
                blocked.append(tid)
                seen.add(tid)
                queue.append(tid)

    # ---- Empty cascade: no plan I/O at all. ----
    if not blocked:
        _emit(args, {
            "blocked_task_ids": [],
            "plan_mutations_applied": [],
            "run_log_appended": [],
        })
        return

    plan_mutations_applied: list[str] = []
    run_log_appended: list[str] = []
    remaining: list[str] = list(blocked)

    # ---- PHASE 2a: single plan read. ----
    try:
        original = _load_text(plan_path)
    except OSError as e:
        _die(args, {"errors": [{
            "failed_stage": "plan_read",
            "failed_id": None,
            "error": str(e),
            "plan_mutations_applied": [],
            "run_log_appended": [],
            "remaining": list(remaining),
        }]})

    # ---- PHASE 2b: in-memory mutate loop. Track ValueError; do NOT die yet. ----
    mutated_text = original
    mutated_in_memory: list[str] = []
    mutate_failure: dict | None = None
    for idx, bid in enumerate(remaining):
        try:
            mutated_text, _ = mutate_task_status(mutated_text, bid, "blocked")
        except ValueError as e:
            mutate_failure = {
                "failed_stage": "plan_mutate",
                "failed_id": bid,
                "error": str(e),
                "remaining": list(remaining[idx + 1:]),
            }
            break
        mutated_in_memory.append(bid)

    # ---- PHASE 2c: if NO id flipped, die now (nothing to persist or log). ----
    if not mutated_in_memory:
        assert mutate_failure is not None
        mutate_failure["plan_mutations_applied"] = []
        mutate_failure["run_log_appended"] = []
        _die(args, {"errors": [mutate_failure]})

    # ---- PHASE 2d: single plan write (at least one in-memory success). ----
    try:
        _write_text(plan_path, mutated_text)
    except OSError as e:
        # Write failed → nothing persisted. Per acceptance #5, a pending
        # mutate_failure is ALWAYS primary and the plan_write failure is
        # secondary. Otherwise plan_write is primary.
        if mutate_failure is not None:
            payload: dict = dict(mutate_failure)
            payload["plan_mutations_applied"] = []
            payload["run_log_appended"] = []
            payload["secondary_failed_stage"] = "plan_write"
            payload["secondary_failed_id"] = None
            payload["secondary_error"] = str(e)
        else:
            payload = {
                "failed_stage": "plan_write",
                "failed_id": None,
                "error": str(e),
                "plan_mutations_applied": [],
                "run_log_appended": [],
                "remaining": [],
            }
        _die(args, {"errors": [payload]})
    plan_mutations_applied = list(mutated_in_memory)

    # ---- PHASE 3: run-log append for every id persisted to plan. ----
    log_failure: dict | None = None
    for bid in plan_mutations_applied:
        try:
            _append_run_log("blocked", {
                "run_id": args.run_id,
                "task_id": bid,
                "blocker_task_id": failed_id,
                "reason": f"dependency TASK-{failed_id} failed",
            })
        except Exception as e:  # _append_run_log raises on tail-verify failure
            log_failure = {
                "failed_stage": "run_log_append",
                "failed_id": bid,
                "error": str(e),
            }
            break
        run_log_appended.append(bid)

    # ---- PHASE 4: decide primary vs secondary stage for any pending failures.
    # Per acceptance #5 double-failure precedence: a DEFERRED plan_mutate
    # failure is ALWAYS primary; a concurrent run_log_append failure is
    # secondary. run_log_appended truncates at the last success.
    if mutate_failure is not None:
        mutate_failure["plan_mutations_applied"] = list(plan_mutations_applied)
        mutate_failure["run_log_appended"] = list(run_log_appended)
        if log_failure is not None:
            mutate_failure["secondary_failed_stage"] = "run_log_append"
            mutate_failure["secondary_failed_id"] = log_failure["failed_id"]
        _die(args, {"errors": [mutate_failure]})

    if log_failure is not None:
        log_failure["plan_mutations_applied"] = list(plan_mutations_applied)
        log_failure["run_log_appended"] = list(run_log_appended)
        log_failure["remaining"] = []
        _die(args, {"errors": [log_failure]})

    # ---- Success. ----
    _emit(args, {
        "blocked_task_ids": blocked,
        "plan_mutations_applied": plan_mutations_applied,
        "run_log_appended": run_log_appended,
    })


def cmd_fail_task(args: argparse.Namespace) -> None:
    tid = _normalize_task_id(args.task_id)
    if not tid:
        _die(args, {"error": f"bad --task-id: {args.task_id!r}"})

    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})

    repo_root_arg = getattr(args, "repo_root", None)
    if repo_root_arg:
        repo_root = Path(repo_root_arg).resolve()
    else:
        repo_root = Path.cwd().resolve()

    raw_files = [f.strip() for f in (args.files or "").split(",") if f.strip()]

    tracked: list[str] = []
    untracked: list[str] = []
    protected_skipped: list[str] = []
    out_of_repo_skipped: list[str] = []
    directory_skipped: list[str] = []
    submodule_skipped: list[str] = []

    for raw in raw_files:
        rel = canonicalize_file(raw, repo_root)
        if rel is None:
            # canonicalize_file returns None for out-of-repo escapes.
            # Preserve the caller's original spelling in the emit so
            # operators can see what they passed.
            out_of_repo_skipped.append(raw)
            continue
        if rel == "":
            # Canonicalized to repo_root itself — a directory.
            directory_skipped.append(rel)
            continue
        abs_path = repo_root / rel
        # Order of checks matches the classification table -- submodule
        # BEFORE directory (a gitlink is a directory in the worktree, so
        # it must classify as submodule not directory).
        if _is_inside_submodule(abs_path, rel, repo_root):
            submodule_skipped.append(rel)
            continue
        if abs_path.is_dir() and not abs_path.is_symlink():
            directory_skipped.append(rel)
            continue
        if is_protected_path(rel):
            protected_skipped.append(rel)
            continue
        probe = _git(
            ["ls-files", "--error-unmatch", "--", rel],
            cwd=repo_root,
        )
        if probe.returncode == 0:
            tracked.append(rel)
        else:
            untracked.append(rel)

    restore_ok = True
    if tracked:
        restore = _git(
            ["restore", "--staged", "--worktree", "--", *tracked],
            cwd=repo_root,
        )
        if restore.returncode != 0:
            restore_ok = False

    removed_untracked: list[str] = []
    for rel in untracked:
        abs_path = repo_root / rel
        if not abs_path.exists() and not abs_path.is_symlink():
            continue
        try:
            abs_path.unlink()
        except FileNotFoundError:
            continue
        except (IsADirectoryError, PermissionError, OSError):
            continue
        removed_untracked.append(rel)

    original = _load_text(plan)
    try:
        mutated, _ = mutate_task_status(original, tid, "failed")
    except ValueError as e:
        _die(args, {"error": f"status mutation: {e}"})
    _write_text(plan, mutated)

    event_fields: dict = {
        "run_id": args.run_id,
        "task_id": tid,
        "stage": args.stage,
        "reason": args.reason,
    }
    if args.reversion_guidance:
        event_fields["reversion_guidance"] = args.reversion_guidance
    if args.reviewer_findings:
        try:
            parsed_findings = json.loads(args.reviewer_findings)
        except json.JSONDecodeError as e:
            _die(args, {"error": f"invalid --reviewer-findings: {e}"})
        if args.stage == "review":
            review_errors = _validate_review_failure_payload(parsed_findings)
            if review_errors:
                _die(args, {"errors": review_errors})
        event_fields["reviewer_findings"] = parsed_findings
    _append_run_log("failed", event_fields)

    _emit(args, {
        "restore_ok": restore_ok,
        "status_updated": True,
        "log_appended": True,
        "removed_untracked": removed_untracked,
        "protected_skipped": protected_skipped,
        "out_of_repo_skipped": out_of_repo_skipped,
        "directory_skipped": directory_skipped,
        "submodule_skipped": submodule_skipped,
    })


def cmd_update_plan_header(args: argparse.Namespace) -> None:
    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})
    text = _load_text(plan)
    header_end = TASK_HEADER_RE.search(text)
    header_slice = text[: header_end.start()] if header_end else text
    tail_slice = text[header_end.start():] if header_end else ""
    m = STATUS_BULLET_RE.search(header_slice)
    if not m:
        bold = re.search(r"^\*\*Status:\*\*\s*(.+?)\s*$", header_slice, re.MULTILINE)
        if not bold:
            # TASK-019 fallback: plans authored without a top-level Status
            # bullet (e.g. the DUAL_AGENT_Plans format, where per-task Status
            # is authoritative) are not an error — `commit-task` maintains the
            # per-task markers. Skip the header update rather than erroring.
            _emit(args, {
                "status": "absent",
                "warning": "no plan-level **Status:** line to update; skipping",
            })
            return
        new_header = header_slice[: bold.start()] + f"**Status:** {args.status}" + header_slice[bold.end():]
    else:
        new_header = header_slice[: m.start()] + f"{m.group(1)} {args.status}" + header_slice[m.end():]
    _write_text(plan, new_header + tail_slice)
    _emit(args, {"ok": True})


def cmd_finalize_execution_log(args: argparse.Namespace) -> None:
    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})
    try:
        rows = json.loads(args.rows_json)
    except json.JSONDecodeError as e:
        _die(args, {"error": f"invalid --rows-json: {e}"})
    row_errors = _validate_execution_log_rows(rows)
    if row_errors:
        _die(args, {"errors": row_errors})

    header = "| Task | Agent | Reviewer | Verdict | Commit | Notes |"
    sep = "|---|---|---|---|---|---|"
    run_id_heading = f"## Execution log — {args.run_id}"
    if args.outcome:
        run_id_heading += f" ({args.outcome})"
    lines = [
        "",
        run_id_heading,
        "",
        f"Starting SHA: `{args.starting_sha}`  → Ending SHA: `{args.ending_sha}`",
        "",
        header,
        sep,
    ]
    for row in rows:
        cells = [
            str(row.get("task", "")),
            str(row.get("agent", "")),
            str(row.get("reviewer", "")),
            str(row.get("verdict", "")),
            str(row.get("commit", "")),
            str(row.get("notes", "")),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    text = _load_text(plan).rstrip("\n") + "\n\n" + "\n".join(lines).lstrip("\n")
    _write_text(plan, text)
    _emit(args, {"ok": True})


def cmd_log_event(args: argparse.Namespace) -> None:
    try:
        fields = json.loads(args.fields_json)
    except json.JSONDecodeError as e:
        _die(args, {"error": f"invalid --fields-json: {e}"})
    if not isinstance(fields, dict):
        _die(args, {"error": "--fields-json must be a JSON object"})
    if args.event not in ALLOWED_LOG_EVENTS:
        _die(args, {
            "errors": [{
                "path": "$.event",
                "code": "unknown-event-type",
                "message": (
                    f"event {args.event!r} is not in the allowlist "
                    f"{sorted(ALLOWED_LOG_EVENTS)}"
                ),
            }],
        })
    # TASK-022: optional `--findings-json` attaches the full reviewer
    # finding payload to the event. Validated via the same helper
    # `commit-task` uses so the two seams share one schema. Collision with
    # a `findings` key already present in `--fields-json` is a structured
    # error — the orchestrator MUST pick one source of truth.
    findings: list | None = None
    if getattr(args, "findings_json", None) is not None:
        try:
            parsed = json.loads(args.findings_json)
        except json.JSONDecodeError as e:
            _die(args, {"errors": [{
                "path": "$.findings_json",
                "code": "invalid-json",
                "message": f"invalid --findings-json: {e}",
            }]})
        errs = _validate_minor_findings_payload(parsed, path="$.findings_json")
        if errs:
            _die(args, {"errors": errs})
        if "findings" in fields:
            _die(args, {"errors": [{
                "path": "$.findings",
                "code": "findings-json-collision",
                "message": (
                    "findings key is present in both --fields-json and "
                    "--findings-json; pick one source"
                ),
            }]})
        findings = parsed
    try:
        if findings is not None:
            merged = dict(fields)
            merged["findings"] = findings
            written = _append_run_log(args.event, merged)
        else:
            written = _append_run_log(args.event, fields)
    except RuntimeError as e:
        _die(args, {"error": str(e)})
    _emit(args, {"ok": True, "written_line": written})


def cmd_normalize_task_id(args: argparse.Namespace) -> None:
    normalized = _normalize_task_id(args.id)
    if normalized is None:
        _die(args, {"error": f"cannot normalize task id: {args.id!r}"})
    _emit(args, {"normalized": normalized})


LOCK_ENTRY_KEYS = frozenset({"run_id", "acquired_at"})


def _validate_lock_shape(raw: object) -> list[dict]:
    """Return [] if canonical; else a list of {code, message} violations.

    Canonical shape:
        {"<non-empty-string>": {"run_id": <non-empty str>,
                                "acquired_at": <non-empty str>}, ...}

    An empty top-level dict is canonical. Unicode is accepted in values.
    `acquired_at` is validated as an opaque non-empty string (no ISO-8601
    parse). Non-empty-string check is symmetric across run_id/acquired_at:
    both use `isinstance(x, str) and x != ""`.
    """
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return [{
            "code": "lock-toplevel-not-object",
            "message": f"lock file top-level is {type(raw).__name__}, expected object",
        }]
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            errors.append({
                "code": "lock-key-invalid",
                "message": f"entry key {key!r} must be non-empty string",
            })
            continue
        if not isinstance(value, dict):
            errors.append({
                "code": "lock-entry-not-object",
                "message": f"entry {key!r} value is {type(value).__name__}, expected object",
            })
            continue
        actual_keys = set(value.keys())
        missing = LOCK_ENTRY_KEYS - actual_keys
        extra = actual_keys - LOCK_ENTRY_KEYS
        if missing:
            errors.append({
                "code": "lock-entry-missing-keys",
                "message": f"entry {key!r} missing keys: {sorted(missing)}",
            })
        if extra:
            errors.append({
                "code": "lock-entry-extra-keys",
                "message": f"entry {key!r} has extra keys: {sorted(extra)}",
            })
        for req in ("run_id", "acquired_at"):
            if req in value and (not isinstance(value[req], str) or not value[req]):
                errors.append({
                    "code": "lock-entry-value-empty",
                    "message": f"entry {key!r} field {req!r} must be non-empty string",
                })
    return errors


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Torn-write-safe write.

    Uses mkstemp in the same directory (so os.replace is a same-filesystem
    atomic rename) and a unique suffix (so two concurrent acquires cannot
    collide on a fixed .tmp name). Does NOT fsync — durability after power
    loss is explicitly not a requirement here; the guarantee is that the
    canonical path never holds a partial JSON body visible to a concurrent
    reader.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def cmd_acquire_lock(args: argparse.Namespace) -> None:
    if not isinstance(args.run_id, str) or args.run_id == "":
        _die(args, {
            "acquired": False,
            "errors": [{
                "code": "lock-run-id-empty",
                "message": "--run-id must be a non-empty string",
            }],
        })
    plan_abs = os.path.abspath(args.plan_file)
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    pre_raw: object | None = None
    decode_err: str | None = None
    if RUN_LOCK_PATH.exists():
        raw_text = RUN_LOCK_PATH.read_text(encoding="utf-8")
        try:
            pre_raw = json.loads(raw_text)
        except json.JSONDecodeError as e:
            decode_err = str(e)

    if args.force:
        new_state = {plan_abs: {"run_id": args.run_id, "acquired_at": _now()}}
        _atomic_write_json(RUN_LOCK_PATH, new_state)
        _emit(args, {"acquired": True, "forced": True})
        return

    if decode_err is not None:
        _die(args, {
            "acquired": False,
            "errors": [{"code": "lock-json-decode", "message": decode_err}],
        })

    if pre_raw is not None:
        shape_errors = _validate_lock_shape(pre_raw)
        if shape_errors:
            _die(args, {"acquired": False, "errors": shape_errors})
        current = pre_raw
    else:
        current = {}

    if plan_abs in current and current[plan_abs].get("run_id") != args.run_id:
        _die(args, {
            "acquired": False,
            "conflict_run_id": current[plan_abs].get("run_id"),
        })

    current[plan_abs] = {"run_id": args.run_id, "acquired_at": _now()}
    _atomic_write_json(RUN_LOCK_PATH, current)
    _emit(args, {"acquired": True})


def cmd_release_lock(args: argparse.Namespace) -> None:
    plan_abs = os.path.abspath(args.plan_file)
    if not RUN_LOCK_PATH.exists():
        _emit(args, {"released": False, "reason": "no-lock-file"})
    try:
        current = json.loads(RUN_LOCK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        current = {}
    entry = current.get(plan_abs)
    if not entry or entry.get("run_id") != args.run_id:
        _emit(args, {"released": False, "reason": "run-id-mismatch"})
    current.pop(plan_abs, None)
    if current:
        RUN_LOCK_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    else:
        RUN_LOCK_PATH.unlink()
    _emit(args, {"released": True})


def cmd_path_info(args: argparse.Namespace) -> None:
    """Emit the configured plan-dir and derived paths so the orchestrator can
    template them into SKILL.md placeholders (`<plan_dir>`, `<run_log>`, etc.)."""
    payload = {
        "plan_dir": _PLAN_DIR_POSIX,
        "run_log": RUN_LOG_PATH.as_posix(),
        "run_lock": RUN_LOCK_PATH.as_posix(),
        "schedule_glob": f"{_PLAN_DIR_POSIX}/*.schedule.json",
    }
    _emit(args, payload)


# ---------------------------------------------------------------------------
# Phase gates (TASK-005)
# ---------------------------------------------------------------------------
#
# Six explicit gate predicates that the orchestrator uses to promote the run
# through the plan → schedule → execute → commit lifecycle. Each predicate is
# a small function returning a `{name, status, reason}` dict where status is
# one of `pass`, `fail`, `not_applicable`. The gate model is the vocabulary
# downstream chunks reference (self-audit, sample-fixture conformance, and
# the Phase 5 certification rerun). See DUAL_AGENT_PLAN_EXECUTOR.md §9.7.
#
# Dry-run pass condition: schema-valid + schedule-valid + fixture-valid must
# hold; execution-safe / review-safe are asserted by predicate against
# `plan_codex_dispatch.py`; commit-safe is `not_applicable` because no
# commits exist in dry-run.
#
# Execute pass condition: all six gates hold, with commit-safe re-verified
# post-hoc for every committed task via `--run-id` bundle mode.

GATE_NAMES: tuple[str, ...] = (
    "schema-valid",
    "schedule-valid",
    "fixture-valid",
    "execution-safe",
    "review-safe",
    "commit-safe",
)

# Fixture the `_gate_fixture_valid` predicate reads. The sample plan rewrite
# is TASK-006's scope; TASK-005 establishes the gate and its unit tests via
# synthetic fixtures. Running this gate against the live fixture pre-TASK-006
# is expected to return `fail` until the rewrite lands.
#
# Path is derived from `__file__` so the gate resolves the fixture at the
# repo root regardless of the process cwd. `plan_ops.py` lives at
# `plugins/plan-executor/scripts/plan_ops.py`, so the repo root is three
# parents up from the script dir. Phase 0 / skill invocations that export
# `CLAUDE_PLUGIN_ROOT` but don't chdir to the repo root still pick up the
# correct fixture, and unit tests that run from an arbitrary tmp_path are
# unaffected.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_RELATIVE_PATH = "docs/plans/sample_phase4.md"
_FIXTURE_ABSOLUTE_PATH = _REPO_ROOT / _FIXTURE_RELATIVE_PATH

# Canonical locations of wrapper predicates. `_gate_execution_safe` and
# `_gate_review_safe` grep this file — they never invoke the wrapper.
_WRAPPER_PATH = (
    Path(__file__).resolve().parent / "plan_codex_dispatch.py"
)


def _gate_result(name: str, status: str, reason: str) -> dict:
    """Shape helper for gate predicates."""
    if status not in {"pass", "fail", "not_applicable"}:
        raise ValueError(f"gate status must be pass|fail|not_applicable, got {status!r}")
    return {"name": name, "status": status, "reason": reason}


# Required top-level sections in a plan markdown file, per §5 of the design
# doc. Context accepts either `## Context` or `## Scoped Context` — the plan
# schema allows both (chunked plans use `## Scoped Context` per-TASK files).
_PLAN_TOP_LEVEL_REQUIRED = (
    ("## Goal", "Goal section"),
)
_PLAN_CONTEXT_SECTIONS = ("## Context", "## Scoped Context")


# Fields every `### TASK-NNN` block must declare (per §5). Bullets are
# required; Description and Reversion guidance are paragraph-form headers.
_TASK_REQUIRED_BULLETS = (
    "Status",
    "Priority",
    "Files",
    "Test command",
    "Acceptance criteria",
)
_TASK_REQUIRED_PROSE_HEADERS = (
    "Description",
)


def _gate_schema_valid(plan_file: str | Path) -> dict:
    """Plan markdown conforms to §5 of the design doc.

    Pass iff: `## Goal`, a context section (`## Context` or
    `## Scoped Context`), and `## Verification` top-level sections are
    present; every `### TASK-NNN` block carries the required bullets
    (Status, Priority, Files, Test command, Acceptance criteria) plus
    the required prose headers (Description). Returns `fail` on any
    missing element; the reason string names the first few problems
    concretely so an operator can fix them without guessing.
    """
    path = Path(plan_file)
    if not path.is_file():
        return _gate_result(
            "schema-valid",
            "fail",
            f"plan file not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "schema-valid",
            "fail",
            f"plan file unreadable: {e}",
        )

    problems: list[str] = []
    # Top-level sections. Matched on `^## <name>` (leading `##` exactly,
    # so a `### ...` subtask header does not satisfy `## ...`).
    for header, label in _PLAN_TOP_LEVEL_REQUIRED:
        if not re.search(rf"^{re.escape(header)}\b", text, re.MULTILINE):
            problems.append(f"missing {label} (`{header}`)")
    if not any(
        re.search(rf"^{re.escape(sec)}\b", text, re.MULTILINE)
        for sec in _PLAN_CONTEXT_SECTIONS
    ):
        problems.append(
            "missing Context section (`## Context` or `## Scoped Context`)"
        )
    # Verification: §5 authors this as `## Verification` in whole-plan files.
    # Per-chunk `## Verification` files under DUAL_AGENT_Plans/ satisfy it
    # too — grep is top-level so subsections do not match accidentally.
    if not re.search(r"^## Verification\b", text, re.MULTILINE):
        problems.append("missing Verification section (`## Verification`)")

    _, task_blocks = _split_task_blocks(text)
    if not task_blocks:
        problems.append("no `### TASK-NNN` blocks found")
    for tid, block in task_blocks:
        for field in _TASK_REQUIRED_BULLETS:
            if not re.search(
                rf"^\s*-\s*\*\*{re.escape(field)}:\*\*",
                block,
                re.MULTILINE,
            ):
                problems.append(f"TASK-{tid} missing bullet **{field}:**")
        for field in _TASK_REQUIRED_PROSE_HEADERS:
            if not re.search(
                rf"^\*\*{re.escape(field)}:\*\*",
                block,
                re.MULTILINE,
            ):
                problems.append(
                    f"TASK-{tid} missing prose header **{field}:**"
                )

    if problems:
        # Show up to five problems inline so the reason stays scannable.
        preview = "; ".join(problems[:5])
        if len(problems) > 5:
            preview += f"; … ({len(problems) - 5} more)"
        return _gate_result(
            "schema-valid",
            "fail",
            f"schema violations: {preview}",
        )
    return _gate_result(
        "schema-valid",
        "pass",
        f"plan {path.name} conforms to §5 schema",
    )


def _gate_schedule_valid(schedule_file: str | Path | None) -> dict:
    """Analyst schedule JSON passes shape + DAG validation.

    Pass iff the schedule file exists, parses as JSON, and `_validate_schedule`
    + `_validate_schedule_dag` both return no errors. Same validators used by
    `parse-schedule` so the gate and the helper agree by construction.
    """
    if schedule_file is None:
        return _gate_result(
            "schedule-valid",
            "fail",
            "schedule-file argument is required",
        )
    path = Path(schedule_file)
    if not path.is_file():
        return _gate_result(
            "schedule-valid",
            "fail",
            f"schedule file not found: {path}",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _gate_result(
            "schedule-valid",
            "fail",
            f"schedule json decode: {e}",
        )
    if not isinstance(data, dict):
        return _gate_result(
            "schedule-valid",
            "fail",
            "top-level schedule must be an object",
        )
    errors, _ = _validate_schedule(data)
    if not errors:
        tasks_raw = data.get("tasks")
        batches_raw = data.get("batches")
        # Preserve the validator contract: `_validate_schedule` currently
        # reports shape errors for non-list `tasks`/`batches`, but collapsing
        # them to `[]` here would silently pass an invalid schedule if that
        # contract ever regresses. Treat a missing or wrong-shape value as
        # its own DAG-level failure instead.
        if not isinstance(tasks_raw, list) or not isinstance(batches_raw, list):
            errors.append({
                "code": "invalid_shape",
                "message": "`tasks` and `batches` must both be lists for DAG validation",
            })
        else:
            errors.extend(_validate_schedule_dag(tasks_raw, batches_raw))
    if errors:
        first = errors[0]
        return _gate_result(
            "schedule-valid",
            "fail",
            f"{len(errors)} schedule error(s); first: {first.get('code')} — {first.get('message')}",
        )
    return _gate_result(
        "schedule-valid",
        "pass",
        f"schedule {path.name} passes shape + DAG validation",
    )


def _gate_fixture_valid(plan_file: str | Path | None = None) -> dict:
    """Sample fixture passes schema-valid and schedule-valid.

    TASK-006 rewrites `docs/plans/sample_phase4.md` to the canonical schema.
    Running this gate against the pre-rewrite fixture is **expected to
    return `fail`** — the gate vocabulary is established here, its live-tree
    green status lands with TASK-006. Unit tests drive this predicate via
    synthetic fixtures (see `tests/scripts/test_plan_ops.py`); the live
    fixture run is deferred.

    Invariant: fixture-valid is the aggregate of schema-valid AND
    schedule-valid on the fixture; a missing `<basename>.schedule.json`
    sidecar is a `fail` (the sidecar is the schedule input and cannot be
    skipped without defeating the aggregate).
    """
    # Default to the cwd-independent absolute fixture path so Phase 0 and
    # certification bundles resolve the same file regardless of how the
    # caller was invoked. Tests can still override by passing `plan_file`.
    path = Path(plan_file) if plan_file is not None else _FIXTURE_ABSOLUTE_PATH
    if not path.is_file():
        return _gate_result(
            "fixture-valid",
            "fail",
            f"fixture not found: {path}",
        )
    schema = _gate_schema_valid(path)
    if schema["status"] != "pass":
        return _gate_result(
            "fixture-valid",
            "fail",
            f"schema-valid failed: {schema['reason']}",
        )
    # Schedule sidecar convention: `<basename>.schedule.json` under plan_dir.
    # fixture-valid is the aggregate schema + schedule certification, so a
    # missing sidecar is a fail — otherwise dry-run/execute certification
    # could go green without exercising schedule validation on the fixture.
    sidecar = path.with_suffix(".schedule.json")
    if not sidecar.is_file():
        return _gate_result(
            "fixture-valid",
            "fail",
            f"schedule sidecar missing: {sidecar.name} "
            f"(fixture-valid requires schema + schedule; sidecar absent)",
        )
    sched = _gate_schedule_valid(sidecar)
    if sched["status"] != "pass":
        return _gate_result(
            "fixture-valid",
            "fail",
            f"schedule-valid failed on {sidecar.name}: {sched['reason']}",
        )
    return _gate_result(
        "fixture-valid",
        "pass",
        f"fixture {path.name} + sidecar pass schema + schedule gates",
    )


def _grep_file(path: Path, pattern: str) -> bool:
    """Return True iff `pattern` (regex) matches anywhere in `path`."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(pattern, text, re.MULTILINE) is not None


def _strip_python_comments_and_docstrings(text: str) -> str:
    """Elide line comments and triple-quoted string regions from Python
    source so executable-code-only grep targets (used by
    `execution-safe` / `review-safe`) cannot be fooled by commented-out
    or docstring-mentioned tokens. Returns text with elided regions
    replaced by blank lines so line offsets inside the remaining
    executable lines are preserved — callers that search with regex or
    substring will see only real call sites.

    Lightweight heuristic: toggles triple-quote state on triple-double
    or triple-single marks per-line and strips a single unquoted hash
    on each line. Does not handle f-string nesting or backslash-escaped
    quotes — the wrapper source stays within that tolerance.
    """
    out_lines: list[str] = []
    in_triple_double = False
    in_triple_single = False
    # Pre-compiled: non-greedy inline triple-quoted regions. Single-line
    # docstrings/strings (e.g. `"""one-liner"""`) never toggle the
    # per-line state tracker because their mark count is even; elide
    # their contents here so a token mentioned only inside such a
    # string cannot fool the grep targets.
    _TRIPLE_INLINE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'')
    for raw_line in text.splitlines():
        # Strip any self-contained triple-quoted regions first so the
        # state tracker below sees only the unbalanced marks that span
        # line boundaries.
        line = _TRIPLE_INLINE.sub("", raw_line)
        double_marks = line.count('"""')
        single_marks = line.count("'''")
        line_was_in_string = in_triple_double or in_triple_single
        if double_marks % 2 == 1:
            in_triple_double = not in_triple_double
        if single_marks % 2 == 1:
            in_triple_single = not in_triple_single
        if line_was_in_string or in_triple_double or in_triple_single:
            out_lines.append("")
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out_lines.append("")
            continue
        # Strip inline `# comment` suffix, respecting simple single/double
        # quoted strings on the same line. A bare `#` inside a string
        # literal is NOT a comment; a `#` outside any open quote is.
        in_s_single = False
        in_s_double = False
        cut_idx: int | None = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line) and (in_s_single or in_s_double):
                i += 2
                continue
            if ch == "'" and not in_s_double:
                in_s_single = not in_s_single
            elif ch == '"' and not in_s_single:
                in_s_double = not in_s_double
            elif ch == "#" and not in_s_single and not in_s_double:
                cut_idx = i
                break
            i += 1
        if cut_idx is not None:
            line = line[:cut_idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def _gate_execution_safe(wrapper_path: Path | None = None) -> dict:
    """Wrapper's implement path has baseline-snapshot + always-ignore.

    Predicate-only — does NOT invoke the wrapper. Greps for:
      (1) the shared always-ignore / protected-paths seam
          (`from _plan_paths import … PROTECTED_…`),
      (2) `_snapshot_baseline(` called at TWO distinct seams parsed as
          separate code regions -- inside `cmd_implement`'s body (the
          pre-dispatch implement seam) AND inside the timeout cleanup
          path. The cleanup seam is satisfied EITHER by an explicit
          `_snapshot_baseline(` call in the body of the
          `_handle_timeout_cleanup` helper, OR by
          `_handle_timeout_cleanup(..., baseline)` in `cmd_implement`'s
          timeout branch -- the kwarg propagates the snapshot without
          re-capturing it.
      (3) absence of `git clean -fd` outside comments (the observe-only
          cleanup contract — see §4 State Isolation Contract).

    A wrapper that captures a baseline at the implement seam but fails
    to propagate or re-capture it through the cleanup helper is rejected
    because the cleanup region cannot compare the post-dispatch delta
    against an authoritative pre-dispatch snapshot.
    """
    path = wrapper_path or _WRAPPER_PATH
    if not path.is_file():
        return _gate_result(
            "execution-safe",
            "fail",
            f"wrapper not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "execution-safe",
            "fail",
            f"wrapper unreadable: {e}",
        )

    # Strip `#` comments and triple-quoted docstrings before scanning for
    # executable call sites. Without this, a commented-out
    # `# _snapshot_baseline(...)` or a docstring mentioning the cleanup
    # helper would satisfy the predicate even with no real call. Raw
    # `text` is still used for the module-wide import seam check below
    # (imports are code, not comments) and the `git clean -fd` scan has
    # its own inline comment tracker.
    code_text = _strip_python_comments_and_docstrings(text)

    def _extract_function_body(func_name: str) -> str | None:
        """Slice the body of `def <func_name>(` up to the next top-level
        `def`. Returns None if no such definition exists. Used to parse
        the implement-dispatch and timeout-cleanup windows as separate
        regions so a snapshot call in one does not fool the check for
        the other.
        """
        m = re.search(
            rf"^def {re.escape(func_name)}\s*\(", code_text, re.MULTILINE,
        )
        if not m:
            return None
        tail = code_text[m.end():]
        next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
        return tail[: next_def.start()] if next_def else tail

    checks: list[tuple[str, bool]] = []
    # (1) always-ignore / protected-paths seam.
    checks.append((
        "PROTECTED_EXACT_PATHS import from _plan_paths",
        "PROTECTED_EXACT_PATHS" in code_text and "_plan_paths" in code_text,
    ))
    # (2) baseline snapshot at the implement dispatch seam AND within the
    # timeout-cleanup window, parsed as SEPARATE regions so a snapshot
    # in one window cannot satisfy the check for the other.
    impl_body = _extract_function_body("cmd_implement")
    cleanup_body = _extract_function_body("_handle_timeout_cleanup")
    if impl_body is None:
        checks.append((
            "`def cmd_implement(` definition present",
            False,
        ))
    else:
        # Implement-dispatch window: baseline must be captured inside
        # cmd_implement so the timeout branch has a pre-dispatch snapshot
        # to compare against. Scoped to the cmd_implement body only.
        checks.append((
            "_snapshot_baseline called in cmd_implement (implement-dispatch window)",
            "_snapshot_baseline(" in impl_body,
        ))
        # Timeout-cleanup window. Two satisfying seams, parsed as
        # separate regions:
        #   (a) `_handle_timeout_cleanup`'s own body calls
        #       `_snapshot_baseline(` -- the cleanup helper captures its
        #       own snapshot, independent of the caller.
        #   (b) cmd_implement's timeout branch invokes
        #       `_handle_timeout_cleanup(..., baseline)` -- the kwarg
        #       propagates the captured snapshot through.
        # Either form satisfies the cleanup-window invariant; both
        # missing fails the gate.
        timeout_match = re.search(
            r"if\s+codex\[[\"']status[\"']\]\s*==\s*[\"']timeout[\"']\s*:",
            impl_body,
        )
        if not timeout_match:
            checks.append((
                "cmd_implement has a timeout cleanup branch",
                False,
            ))
        else:
            region = impl_body[timeout_match.end(): timeout_match.end() + 2500]
            kwarg_propagates = bool(
                re.search(
                    r"_handle_timeout_cleanup\s*\([^)]*baseline",
                    region,
                    re.DOTALL,
                )
            )
            helper_captures = bool(
                cleanup_body is not None
                and "_snapshot_baseline(" in cleanup_body
            )
            checks.append((
                "_snapshot_baseline present in timeout-cleanup window "
                "(_handle_timeout_cleanup body OR cmd_implement baseline kwarg)",
                kwarg_propagates or helper_captures,
            ))
    # (3) `git clean -fd` must not appear in executable code. Lines starting
    # with `#` in the source are allowed — the wrapper's own comments document
    # the prohibition. Triple-quoted docstrings also legitimately reference the
    # token (e.g. "Never invokes `git clean -fd`.") and are skipped via a
    # single-pass string-state tracker that toggles on `"""`/`'''` opens and
    # closes. This is a lightweight parser — it does not handle f-strings or
    # nested quoting edge cases, but the wrapper source stays within its
    # tolerance.
    forbidden_hits: list[str] = []
    in_triple_double = False
    in_triple_single = False
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        # Track triple-quote state first so the skip logic matches reality.
        # Count unescaped occurrences per line; an odd count toggles state.
        double_marks = line.count('"""')
        single_marks = line.count("'''")
        line_was_in_string = in_triple_double or in_triple_single
        if double_marks % 2 == 1:
            in_triple_double = not in_triple_double
        if single_marks % 2 == 1:
            in_triple_single = not in_triple_single
        if line_was_in_string or in_triple_double or in_triple_single:
            continue
        if stripped.startswith("#"):
            continue
        if re.search(r"git\s+clean\s+-fd", line):
            forbidden_hits.append(f"line {idx}")
    checks.append((
        "no `git clean -fd` outside comments",
        not forbidden_hits,
    ))

    failures = [desc for desc, ok in checks if not ok]
    if failures:
        detail = "; ".join(failures)
        if forbidden_hits:
            detail += f" (forbidden hits at {', '.join(forbidden_hits)})"
        return _gate_result(
            "execution-safe",
            "fail",
            f"wrapper missing execution-safe invariants: {detail}",
        )
    return _gate_result(
        "execution-safe",
        "pass",
        f"wrapper {path.name} carries always-ignore + baseline-snapshot + no raw `git clean -fd`",
    )


def _gate_review_safe(wrapper_path: Path | None = None) -> dict:
    """Wrapper's review path has baseline-snapshot + always-ignore.

    Predicate-only. Greps for `_snapshot_baseline(` inside `cmd_review` and
    for the `is_protected_path` predicate the wrapper uses to skip protected
    paths in cleanup. Distinct from `_gate_execution_safe` because the
    review path has different failure modes (sibling state must be
    protected even when review dispatches in parallel with implement).
    """
    path = wrapper_path or _WRAPPER_PATH
    if not path.is_file():
        return _gate_result(
            "review-safe",
            "fail",
            f"wrapper not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "review-safe",
            "fail",
            f"wrapper unreadable: {e}",
        )

    # Strip comments/docstrings before scanning so the grep targets
    # identify real call sites, not commented-out tokens or docstring
    # mentions of the cleanup helpers.
    code_text = _strip_python_comments_and_docstrings(text)

    # Find `def cmd_review(` and take the body up to the next top-level `def`
    # so we can grep inside the review path specifically.
    m = re.search(r"^def cmd_review\s*\(", code_text, re.MULTILINE)
    if not m:
        return _gate_result(
            "review-safe",
            "fail",
            "wrapper has no `def cmd_review(` definition",
        )
    tail = code_text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    review_body = tail[: next_def.start()] if next_def else tail

    checks: list[tuple[str, bool]] = []
    checks.append((
        "_snapshot_baseline called in cmd_review",
        "_snapshot_baseline(" in review_body,
    ))
    checks.append((
        "is_protected_path or PROTECTED_ constants referenced module-wide",
        ("is_protected_path" in code_text or "PROTECTED_EXACT_PATHS" in code_text),
    ))

    failures = [desc for desc, ok in checks if not ok]
    if failures:
        return _gate_result(
            "review-safe",
            "fail",
            f"wrapper review path missing invariants: {'; '.join(failures)}",
        )
    return _gate_result(
        "review-safe",
        "pass",
        f"wrapper {path.name} cmd_review carries baseline-snapshot + protected-path respect",
    )


def _normalize_files_entry(raw: str) -> str:
    """Strip backticks, `(create|modify|delete)` annotations, and `:line`
    suffixes from a raw Files: entry. Mirrors the wrapper's
    `normalize_file_path` so the commit guard/gate agree on allowlist keys.
    """
    cleaned = raw.strip()
    # Strip trailing (create), (modify), (delete), ...
    cleaned = re.sub(r"\s*\([^)]+\)\s*$", "", cleaned)
    cleaned = cleaned.strip()
    # Strip wrapping backticks; `git show --name-only` never emits them.
    if cleaned.startswith("`") and cleaned.endswith("`") and len(cleaned) >= 2:
        cleaned = cleaned[1:-1]
    # Strip :N-M or :N–M ranges, then a single :N reference.
    cleaned = re.sub(r":\d+[-\u2013]\d+$", "", cleaned)
    cleaned = re.sub(r":\d+$", "", cleaned)
    return cleaned.strip()


def _extract_task_files_from_plan(plan_text: str, task_id: str) -> list[str] | None:
    """Extract the normalized allowed_files list for TASK-NNN.

    Returns the parsed list (possibly empty) or None if the task block is
    absent. Mirrors the wrapper's `parse_task_block` + `normalize_file_path`
    logic but stays inside `plan_ops.py` so the gate has no wrapper import
    dependency.

    Accepts both the multi-line form

        - **Files:**
          - path/a.py
          - `path/b.py` (modify)

    and the inline form

        - **Files:** path/a.py
        - **Files:** `path/a.py`, `path/b.py`

    In both cases each entry is normalized (backticks/annotations/line
    suffixes stripped) before being returned.
    """
    normalized = _normalize_task_id(task_id)
    if normalized is None:
        return None
    _, blocks = _split_task_blocks(plan_text)
    for tid, block in blocks:
        if tid != normalized:
            continue
        # Multi-line `- **Files:**\n  - path\n  - path` form.
        m = re.search(
            r"^-\s*\*\*Files:\*\*\s*$",
            block,
            re.MULTILINE,
        )
        if m:
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
                    raw = stripped[1:].strip()
                    items.append(_normalize_files_entry(raw))
                else:
                    break
            return items
        # Inline `- **Files:** path[, path, ...]` form. `_extract_inline_field`
        # semantics: value is the text after the `:**` marker on the same line.
        # Split on commas so comma-separated inline entries land as distinct
        # allowlist keys.
        m_inline = re.search(
            r"^-\s*\*\*Files:\*\*\s+(.+?)\s*$",
            block,
            re.MULTILINE,
        )
        if m_inline:
            raw_value = m_inline.group(1).strip()
            if not raw_value:
                return []
            return [
                _normalize_files_entry(piece)
                for piece in raw_value.split(",")
                if piece.strip()
            ]
        return []
    return None


def _gate_commit_safe(
    commit_sha: str | None,
    task_id: str | None,
    plan_file: str | Path | None,
    *,
    repo_root: Path | None = None,
) -> dict:
    """Post-hoc verification that `<commit_sha>` touched only allowed files.

    Fetches the commit's file list via `git show --name-only` and
    subtracts two distinct sets:

      - `allowed = task_files | {plan_file}` -- the task's declared
        `Files:` list (normalized) plus the plan file itself, which
        `commit-task` stages alongside each task commit per §D.3 step 3.
      - `ignored = {p for p in changed if is_commit_always_ignore(p,
        plan_basename)}` -- bookkeeping paths that `commit-task`
        authors on behalf of the orchestrator (run-log, run-lock,
        per-plan schedule sidecar, `00_INDEX.json` roster).

    Pass iff `changed - allowed - ignored` is empty. The ignore set is
    the shared `COMMIT_ALWAYS_IGNORE` constant from `_plan_paths.py`
    (plus the per-plan schedule sidecar matched via
    `is_commit_always_ignore`), used identically by `commit-task`'s
    own staging logic so the pre-commit and post-commit sides agree by
    construction. We deliberately do NOT fall back to
    `is_protected_path` / `PROTECTED_PATH_PREFIXES` as a broader
    always-ignore filter: those cover paths like `plan_ops.py` itself,
    which a task MUST declare in Files: to commit against. Using the
    protected-path predicate as an always-allow filter silently
    green-lit commits that mutated executor code without declaration.
    """
    if not commit_sha or not task_id or plan_file is None:
        return _gate_result(
            "commit-safe",
            "fail",
            "commit-safe requires commit_sha, task_id, and plan_file",
        )
    plan_path = Path(plan_file)
    if not plan_path.is_file():
        return _gate_result(
            "commit-safe",
            "fail",
            f"plan file not found: {plan_path}",
        )
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "commit-safe",
            "fail",
            f"plan file unreadable: {e}",
        )
    allowed_raw = _extract_task_files_from_plan(plan_text, task_id)
    if allowed_raw is None:
        return _gate_result(
            "commit-safe",
            "fail",
            f"TASK-{task_id} not found in plan {plan_path.name}",
        )
    allowed = set(allowed_raw)
    # Plan file itself is always allowed -- `commit-task` stages it
    # alongside every task commit (§D.3 step 3) so the `**Status:**`
    # bullet flip lands in the same commit as the code change. Cover
    # both layouts a plan_ops invocation might see (cwd repo root vs
    # cwd plan dir) by adding the repo-relative form (when resolvable)
    # and the raw forms.
    try:
        rel_plan = str(
            plan_path.resolve().relative_to(
                (repo_root or Path.cwd()).resolve()
            )
        )
        allowed.add(rel_plan)
    except ValueError:
        pass
    allowed.add(str(plan_path))
    allowed.add(plan_path.as_posix())

    # Derive plan_dir (repo-relative, forward-slash) so
    # `is_commit_always_ignore` can match the sibling `00_INDEX.json`
    # for plans that live outside the default `docs/plans/` bundle.
    # Falls back to None on non-repo layouts (e.g., plan path that
    # cannot be expressed relative to the repo root); the shared
    # constant's static entries still cover the default bundle layouts.
    plan_dir_rel: str | None
    try:
        plan_dir_rel = str(
            plan_path.resolve().parent.relative_to(
                (repo_root or Path.cwd()).resolve()
            )
        ).replace(os.sep, "/")
        if plan_dir_rel == ".":
            plan_dir_rel = ""
    except ValueError:
        plan_dir_rel = None

    proc = _git(
        ["show", "--name-only", "--pretty=format:", commit_sha],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        return _gate_result(
            "commit-safe",
            "fail",
            (
                f"git show failed for {commit_sha}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            ),
        )
    changed = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    offending: list[str] = []
    for path in changed:
        if path in allowed:
            continue
        # Shared always-ignore set (COMMIT_ALWAYS_IGNORE) covers the
        # run-log, run-lock, per-plan schedule sidecar, and
        # 00_INDEX.json roster -- the paths `commit-task` itself
        # writes during orchestration. Matching on plan_basename +
        # plan_dir lets the per-plan sidecar key and the alongside-plan
        # roster resolve for bundles outside the default layout.
        if is_commit_always_ignore(
            path, plan_path.name, plan_dir_rel,
        ):
            continue
        offending.append(path)
    if offending:
        return _gate_result(
            "commit-safe",
            "fail",
            (
                f"commit {commit_sha[:12]} touched {len(offending)} "
                f"file(s) outside TASK-{task_id} scope: {offending[:5]}"
            ),
        )
    return _gate_result(
        "commit-safe",
        "pass",
        f"commit {commit_sha[:12]} touched only TASK-{task_id} allowed files",
    )


def _certify_dry_run(plan_file: str | Path, schedule_file: str | Path | None = None) -> list[dict]:
    """Bundle of gates exercised in dry-run mode.

    schema-valid + schedule-valid + fixture-valid + execution-safe +
    review-safe run as predicates; commit-safe is `not_applicable`
    because no commits exist in dry-run. `schedule-valid` is required
    for certification — a missing `schedule_file` fails the bundle
    rather than collapsing to `not_applicable`.
    """
    gates: list[dict] = [
        _gate_schema_valid(plan_file),
        _gate_schedule_valid(schedule_file),
        _gate_fixture_valid(),
        _gate_execution_safe(),
        _gate_review_safe(),
        _gate_result(
            "commit-safe",
            "not_applicable",
            "dry-run mode; no commits to verify",
        ),
    ]
    return gates


def _certify_execute(
    plan_file: str | Path,
    run_id: str | None,
    schedule_file: str | Path | None = None,
) -> list[dict]:
    """Bundle of gates exercised in execute mode.

    Includes the dry-run set plus a post-hoc commit-safe check per
    `commit_done` event in the run log for this run_id. A run with zero
    commits (successful no-op) reports `commit-safe: not_applicable`.
    `schedule-valid` is required for certification — a missing
    `schedule_file` fails the bundle rather than collapsing to
    `not_applicable`.
    """
    gates: list[dict] = [
        _gate_schema_valid(plan_file),
        _gate_schedule_valid(schedule_file),
        _gate_fixture_valid(),
        _gate_execution_safe(),
        _gate_review_safe(),
    ]
    commits: list[tuple[str, str]] = []
    if run_id and RUN_LOG_PATH.is_file():
        try:
            for line in RUN_LOG_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("event") != "commit_done":
                    continue
                if ev.get("run_id") != run_id:
                    continue
                tid = _normalize_task_id(str(ev.get("task_id", "")))
                sha = ev.get("commit_sha") or ev.get("sha") or ""
                if tid and sha:
                    commits.append((tid, str(sha)))
        except OSError:
            pass
    if not commits:
        gates.append(_gate_result(
            "commit-safe",
            "not_applicable",
            f"execute mode: no commit_done events for run_id={run_id!r}",
        ))
    else:
        # Derive the repo root from the plan file's git toplevel so
        # `git show --name-only` inside `_gate_commit_safe` targets the
        # same repository that produced the run log, not the caller's
        # cwd. Falls back to Path.cwd() semantics on git failure, matching
        # the helper's existing default.
        plan_dir = Path(plan_file).resolve().parent
        toplevel = _git(["rev-parse", "--show-toplevel"], cwd=plan_dir)
        repo_root = (
            Path(toplevel.stdout.strip())
            if toplevel.returncode == 0 and toplevel.stdout.strip()
            else None
        )
        failures: list[str] = []
        for tid, sha in commits:
            res = _gate_commit_safe(sha, tid, plan_file, repo_root=repo_root)
            if res["status"] != "pass":
                failures.append(f"TASK-{tid}@{sha[:12]}: {res['reason']}")
        if failures:
            gates.append(_gate_result(
                "commit-safe",
                "fail",
                f"{len(failures)}/{len(commits)} commit(s) failed: {failures[:3]}",
            ))
        else:
            gates.append(_gate_result(
                "commit-safe",
                "pass",
                f"all {len(commits)} commit(s) for run_id={run_id} touched only allowed files",
            ))
    return gates


# ---------------------------------------------------------------------------
# TASK-007 self-audit: protocol-drift detection
# ---------------------------------------------------------------------------
#
# `cmd_audit` cross-references the shipped artifacts (`plan_ops.py`,
# `plan_codex_dispatch.py`, the schema sidecars, SKILL.md, dispatch
# templates, and the design doc) against the canonical decisions declared
# in `CANONICAL_CONTRACT` near the top of this module. Each check returns
# the documented finding shape (`{check, status, canonical, actual,
# reason, tier}`) and `cmd_audit` aggregates them into a JSON report or a
# Markdown table. Scope is intentionally narrow: cross-cutting integrity,
# not behavior. Runtime conditions for a specific run are TASK-005 gates'
# job; self-audit is the standing readiness check.
#
# Conventions:
#   * Every check has a `tier ∈ {"default", "advisory"}`. Default checks
#     contribute to the verdict; advisory checks (`portable_tier` until
#     TASK-008 lands) appear in the report but do not flip `overall` to
#     `fail` unless `--strict` is set.
#   * `pass_with_alias` is still a pass (alias windows from TASK-001 are
#     legitimate); `fail` is the only verdict-flipping status.
#   * Checks that grep source files report concrete path + line numbers
#     in their `reason` strings so an operator can repair the drift
#     without grep-spelunking.
#   * Lines wrapped in `<!-- portable_tier: legacy-example -->` /
#     `<!-- /portable_tier: legacy-example -->` markers are skipped by
#     the `portable_tier` check; the markers are how a deliberate
#     instructional reference to `venv/bin/python` is allowlisted.

_PORTABLE_LEGACY_OPEN = "<!-- portable_tier: legacy-example -->"
_PORTABLE_LEGACY_CLOSE = "<!-- /portable_tier: legacy-example -->"


def _audit_relpath(path: Path) -> str:
    """Stable relative path for finding `locations[].path`. Falls back to
    `str(path)` when the file lives outside the repo root (e.g. tmp_path
    fixtures). Repo-relative form is what tooling consumes."""
    try:
        repo_root = Path(__file__).resolve().parents[3]
        return str(path.resolve().relative_to(repo_root))
    except (OSError, ValueError):
        return str(path)


def _audit_locate_constant(name: str) -> int | None:
    """Best-effort line number of a top-level assignment to `name` in
    `plan_ops.py`. Used to populate `locations[].line` for constant-only
    checks (status vocabulary, schedule field aliases, schemas) so the
    structured report points operators at the owning declaration."""
    try:
        text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(name)}\s*[:=]", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    return text.count("\n", 0, m.start()) + 1


def _audit_locate_def(name: str, source_text: str | None = None) -> int | None:
    """Best-effort line number of `def <name>(` in `plan_ops.py` (or in
    the supplied `source_text`). Used by checks that grep a function
    body so the report points at the function header."""
    try:
        text = source_text if source_text is not None else (
            (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
        )
    except OSError:
        return None
    m = re.search(rf"^def {re.escape(name)}\s*\(", text, re.MULTILINE)
    if not m:
        return None
    return text.count("\n", 0, m.start()) + 1


def _audit_finding(
    *,
    check: str,
    status: str,
    canonical: dict,
    actual: dict,
    reason: str | None,
    tier: str = "default",
    locations: list[dict] | None = None,
) -> dict:
    """Shape helper for audit findings. `status ∈ {pass, pass_with_alias,
    fail}`; `tier ∈ {default, advisory}`. `reason` is None when the check
    is clean (canonical and actual agree exactly).

    `locations` is a list of `{"path": str, "line": int|None,
    "reason": str}` triples — the structured path-and-reason pairs the
    audit report exposes to tooling. Every finding carries it (empty
    list is allowed when nothing concrete needs flagging, e.g. a clean
    pass), so downstream consumers can iterate uniformly without
    branching on shape.
    """
    if status not in {"pass", "pass_with_alias", "fail"}:
        raise ValueError(
            f"audit status must be pass|pass_with_alias|fail, got {status!r}"
        )
    if tier not in {"default", "advisory"}:
        raise ValueError(f"audit tier must be default|advisory, got {tier!r}")
    return {
        "check": check,
        "status": status,
        "tier": tier,
        "canonical": canonical,
        "actual": actual,
        "reason": reason,
        "locations": list(locations) if locations else [],
    }


def _check_status_vocabulary() -> dict:
    """`ALLOWED_TASK_STATUSES` matches the canonical set, modulo aliases."""
    canonical = set(CANONICAL_CONTRACT["status_vocabulary"])
    actual = set(ALLOWED_TASK_STATUSES)
    aliases = set(ALIAS_WINDOWS.get("status_vocabulary", []))
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[status_vocabulary]",
        "value": sorted(canonical),
    }
    actual_payload = {
        "source": "ALLOWED_TASK_STATUSES",
        "value": sorted(actual),
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    if actual == canonical:
        return _audit_finding(
            check="status_vocabulary",
            status="pass",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=None,
        )
    if aliases and actual == canonical | aliases:
        return _audit_finding(
            check="status_vocabulary",
            status="pass_with_alias",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=f"alias window active: {sorted(aliases)}",
            locations=[
                {
                    "path": owning_path,
                    "line": _audit_locate_constant("ALLOWED_TASK_STATUSES"),
                    "reason": (
                        f"alias window active: {sorted(aliases)} accepted "
                        "alongside canonical status set"
                    ),
                },
            ],
        )
    extra = sorted(actual - canonical - aliases)
    missing = sorted(canonical - actual)
    return _audit_finding(
        check="status_vocabulary",
        status="fail",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=(
            f"ALLOWED_TASK_STATUSES drift: extra={extra}, missing={missing}"
        ),
        locations=[
            {
                "path": owning_path,
                "line": _audit_locate_constant("ALLOWED_TASK_STATUSES"),
                "reason": (
                    f"ALLOWED_TASK_STATUSES drift: extra={extra}, "
                    f"missing={missing}"
                ),
            },
        ],
    )


def _check_schedule_wire_format() -> dict:
    """`_validate_schedule` reads `id`/`index` (or the alias window).

    The check inspects the source of `_validate_schedule` for the
    `t.get("id")` / `b.get("index")` patterns and the
    `SCHEDULE_FIELD_ALIASES` mapping. Drift here would mean schedules
    written by callers using the canonical wire format silently fail to
    parse — exactly the Phase 5 failure mode this audit defends against.
    """
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[schedule_task_field, schedule_batch_field]",
        "value": {
            "task_field": CANONICAL_CONTRACT["schedule_task_field"],
            "batch_field": CANONICAL_CONTRACT["schedule_batch_field"],
        },
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    try:
        validator_src = inspect_validate_schedule_source()
    except OSError as e:
        return _audit_finding(
            check="schedule_wire_format",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(_SCRIPT_DIR / "plan_ops.py"), "value": None},
            reason=f"plan_ops.py unreadable: {e}",
            locations=[
                {
                    "path": owning_path,
                    "line": None,
                    "reason": f"plan_ops.py unreadable: {e}",
                },
            ],
        )

    # Match both shapes the validator uses:
    #   `t.get("id")` / `b.get("index")` (lookup form)
    #   `if "id" not in t` / `if "index" not in b` (presence form).
    # The point is that `_validate_schedule` *names* the canonical field
    # somewhere in its body — drift would mean it stopped looking for it.
    task_pattern_present = bool(
        re.search(r"""\.get\(\s*["']id["']\s*\)""", validator_src)
    ) or bool(
        re.search(r"""["']id["']\s+not\s+in\s+\w""", validator_src)
    )
    batch_pattern_present = bool(
        re.search(r"""\.get\(\s*["']index["']\s*\)""", validator_src)
    ) or bool(
        re.search(r"""["']index["']\s+not\s+in\s+\w""", validator_src)
    )
    aliases_present = (
        "task_id" in SCHEDULE_FIELD_ALIASES
        and SCHEDULE_FIELD_ALIASES["task_id"] == "id"
        and "batch_index" in SCHEDULE_FIELD_ALIASES
        and SCHEDULE_FIELD_ALIASES["batch_index"] == "index"
    )
    actual_payload = {
        "source": "_validate_schedule body + SCHEDULE_FIELD_ALIASES",
        "value": {
            "reads_id": task_pattern_present,
            "reads_index": batch_pattern_present,
            "aliases": dict(SCHEDULE_FIELD_ALIASES),
        },
    }
    validator_line = _audit_locate_def("_validate_schedule")
    aliases_line = _audit_locate_constant("SCHEDULE_FIELD_ALIASES")
    if not task_pattern_present or not batch_pattern_present:
        missing: list[str] = []
        if not task_pattern_present:
            missing.append("`.get(\"id\")` in _validate_schedule")
        if not batch_pattern_present:
            missing.append("`.get(\"index\")` in _validate_schedule")
        return _audit_finding(
            check="schedule_wire_format",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=f"missing canonical reads: {missing}",
            locations=[
                {
                    "path": owning_path,
                    "line": validator_line,
                    "reason": f"missing canonical reads: {missing}",
                },
            ],
        )
    if aliases_present:
        return _audit_finding(
            check="schedule_wire_format",
            status="pass_with_alias",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=(
                "alias window active: SCHEDULE_FIELD_ALIASES maps "
                "task_id->id, batch_index->index"
            ),
            locations=[
                {
                    "path": owning_path,
                    "line": aliases_line,
                    "reason": (
                        "alias window active: SCHEDULE_FIELD_ALIASES maps "
                        "task_id->id, batch_index->index"
                    ),
                },
            ],
        )
    return _audit_finding(
        check="schedule_wire_format",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def inspect_validate_schedule_source() -> str:
    """Return the source body of `_validate_schedule` from this module.

    Helper extracted so tests and the check share one extraction path.
    Raises OSError if the script file is unreadable.
    """
    text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    code_text = _strip_python_comments_and_docstrings(text)
    m = re.search(r"^def _validate_schedule\s*\(", code_text, re.MULTILINE)
    if not m:
        return ""
    tail = code_text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    return tail[: next_def.start()] if next_def else tail


def _check_implementer_report_labels() -> dict:
    """`cmd_parse_implementer_report` searches the canonical labels.

    Greps the function body for the literal `**Concerns for reviewer:**`
    and `**Plan adaptations:**` substrings (or the documented alias
    `**Concerns:**`). Drift here previously caused the orchestrator to
    silently lose the implementer's revert guidance.
    """
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[implementer_*_label]",
        "value": {
            "concerns": CANONICAL_CONTRACT["implementer_concerns_label"],
            "plan_adaptations": CANONICAL_CONTRACT[
                "implementer_plan_adaptations_label"
            ],
        },
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    try:
        text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="implementer_report_labels",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason=f"plan_ops.py unreadable: {e}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"plan_ops.py unreadable: {e}",
            }],
        )
    m = re.search(
        r"^def cmd_parse_implementer_report\s*\(", text, re.MULTILINE,
    )
    if not m:
        return _audit_finding(
            check="implementer_report_labels",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason="cmd_parse_implementer_report definition not found",
            locations=[{
                "path": owning_path, "line": None,
                "reason": "cmd_parse_implementer_report definition not found",
            }],
        )
    tail = text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    body = tail[: next_def.start()] if next_def else tail

    canonical_concerns = "Concerns for reviewer"
    canonical_plan_adapt = "Plan adaptations"
    legacy_concerns = "Concerns"
    aliases = set(ALIAS_WINDOWS.get("implementer_concerns_label", []))
    has_canonical_concerns = canonical_concerns in body
    has_plan_adapt = canonical_plan_adapt in body
    # The legacy `Concerns` literal is detected via a word-boundary match
    # so the canonical `Concerns for reviewer` substring does not double-count.
    has_legacy_concerns = bool(
        re.search(r'"\s*Concerns\s*"', body)
        or re.search(r"'\s*Concerns\s*'", body)
    )
    actual_payload = {
        "source": "cmd_parse_implementer_report body",
        "value": {
            "has_concerns_for_reviewer": has_canonical_concerns,
            "has_plan_adaptations": has_plan_adapt,
            "has_legacy_concerns": has_legacy_concerns,
        },
    }
    parser_def_line = _audit_locate_def(
        "cmd_parse_implementer_report", source_text=text,
    )
    if not has_canonical_concerns or not has_plan_adapt:
        missing = []
        if not has_canonical_concerns:
            missing.append(canonical_concerns)
        if not has_plan_adapt:
            missing.append(canonical_plan_adapt)
        return _audit_finding(
            check="implementer_report_labels",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=f"missing canonical labels in parser: {missing}",
            locations=[{
                "path": owning_path, "line": parser_def_line,
                "reason": f"missing canonical labels in parser: {missing}",
            }],
        )
    if has_legacy_concerns and "**Concerns:**" in aliases:
        return _audit_finding(
            check="implementer_report_labels",
            status="pass_with_alias",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=(
                "alias window active: parser still falls back to legacy "
                "'**Concerns:**' label"
            ),
            locations=[{
                "path": owning_path, "line": parser_def_line,
                "reason": (
                    "alias window active: parser still falls back to legacy "
                    "'**Concerns:**' label"
                ),
            }],
        )
    return _audit_finding(
        check="implementer_report_labels",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_execution_log_columns() -> dict:
    """`cmd_finalize_execution_log` writes the canonical column header.

    Greps for the literal `| Task | Agent | Reviewer | Verdict | Commit |
    Notes |` row inside the function body. Drift here previously broke
    downstream tooling that parses the table by column position.
    """
    canonical_columns = list(CANONICAL_CONTRACT["execution_log_columns"])
    canonical_header = "| " + " | ".join(canonical_columns) + " |"
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[execution_log_columns]",
        "value": canonical_columns,
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    try:
        text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="execution_log_columns",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason=f"plan_ops.py unreadable: {e}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"plan_ops.py unreadable: {e}",
            }],
        )
    m = re.search(
        r"^def cmd_finalize_execution_log\s*\(", text, re.MULTILINE,
    )
    if not m:
        return _audit_finding(
            check="execution_log_columns",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason="cmd_finalize_execution_log definition not found",
            locations=[{
                "path": owning_path, "line": None,
                "reason": "cmd_finalize_execution_log definition not found",
            }],
        )
    finalize_def_line = _audit_locate_def(
        "cmd_finalize_execution_log", source_text=text,
    )
    tail = text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    body = tail[: next_def.start()] if next_def else tail
    actual_payload = {
        "source": "cmd_finalize_execution_log header literal",
        "value": canonical_header if canonical_header in body else None,
    }
    if canonical_header not in body:
        return _audit_finding(
            check="execution_log_columns",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=(
                "canonical column header literal "
                f"{canonical_header!r} not present in "
                "cmd_finalize_execution_log body"
            ),
            locations=[{
                "path": owning_path, "line": finalize_def_line,
                "reason": (
                    "canonical column header literal "
                    f"{canonical_header!r} not present in "
                    "cmd_finalize_execution_log body"
                ),
            }],
        )
    return _audit_finding(
        check="execution_log_columns",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_schemas() -> dict:
    """Codex implement / review schemas match the design doc shape.

    `codex_implement_schema.json[blockers]` must be `array of strings`
    (not the legacy `array of objects` form that produced Phase 5
    parse failures). `codex_review_schema.json` must require the §7.3
    fields `task_id`, `verdict`, `findings`, `scope_ok`,
    `acceptance_met`, `summary` with the canonical verdict enum.
    """
    canonical_payload = {
        "source": "design doc §7.2 / §7.3",
        "value": {
            "implement_blockers_items_type": "string",
            "review_required": [
                "task_id", "verdict", "findings",
                "scope_ok", "acceptance_met", "summary",
            ],
            "review_verdict_enum": [
                "clean", "minor-findings", "needs-rework",
            ],
        },
    }
    impl_path = _SCRIPT_DIR / "codex_implement_schema.json"
    review_path = _SCRIPT_DIR / "codex_review_schema.json"
    impl_relpath = _audit_relpath(impl_path)
    review_relpath = _audit_relpath(review_path)
    problems: list[str] = []
    locations: list[dict] = []
    impl_actual: object = None
    review_actual: object = None
    try:
        impl_schema = json.loads(impl_path.read_text(encoding="utf-8"))
    except OSError as e:
        msg = f"codex_implement_schema.json unreadable: {e}"
        problems.append(msg)
        locations.append({"path": impl_relpath, "line": None, "reason": msg})
        impl_schema = {}
    except json.JSONDecodeError as e:
        msg = f"codex_implement_schema.json malformed JSON: {e}"
        problems.append(msg)
        locations.append({"path": impl_relpath, "line": None, "reason": msg})
        impl_schema = {}
    try:
        review_schema = json.loads(review_path.read_text(encoding="utf-8"))
    except OSError as e:
        msg = f"codex_review_schema.json unreadable: {e}"
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})
        review_schema = {}
    except json.JSONDecodeError as e:
        msg = f"codex_review_schema.json malformed JSON: {e}"
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})
        review_schema = {}

    blockers_items = (
        impl_schema.get("properties", {})
        .get("blockers", {})
        .get("items", {})
    )
    impl_actual = {
        "blockers_items_type": blockers_items.get("type"),
    }
    if blockers_items.get("type") != "string":
        msg = (
            "codex_implement_schema.json[properties.blockers.items.type] "
            f"is {blockers_items.get('type')!r}; canonical is 'string'"
        )
        problems.append(msg)
        locations.append({"path": impl_relpath, "line": None, "reason": msg})

    review_required = review_schema.get("required") or []
    verdict_enum = (
        review_schema.get("properties", {})
        .get("verdict", {})
        .get("enum")
    )
    review_actual = {
        "required": list(review_required),
        "verdict_enum": list(verdict_enum) if verdict_enum else None,
    }
    canonical_required = {
        "task_id", "verdict", "findings",
        "scope_ok", "acceptance_met", "summary",
    }
    missing_required = sorted(canonical_required - set(review_required))
    if missing_required:
        msg = (
            "codex_review_schema.json[required] missing canonical fields: "
            f"{missing_required}"
        )
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})
    canonical_verdicts = ["clean", "minor-findings", "needs-rework"]
    # Enum ordering is not part of the contract — reorder in the schema
    # should not flip the check. Compare as sets so only set-mismatch
    # (missing/extra members) counts as drift.
    if set(verdict_enum or []) != set(canonical_verdicts):
        msg = (
            "codex_review_schema.json[properties.verdict.enum] is "
            f"{verdict_enum!r}; canonical is {canonical_verdicts!r}"
        )
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})

    actual_payload = {
        "source": "codex_implement_schema.json + codex_review_schema.json",
        "value": {"implement": impl_actual, "review": review_actual},
    }
    if problems:
        return _audit_finding(
            check="schemas",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=locations,
        )
    return _audit_finding(
        check="schemas",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _scan_portable_tier_violations(path: Path) -> list[str]:
    """Return `path:lineno` strings for `venv/bin/python` literals outside
    legacy-example marker blocks. Used by `_check_portable_tier`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [f"{path}: unreadable"]
    hits: list[str] = []
    in_legacy = False
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if _PORTABLE_LEGACY_OPEN in line:
            in_legacy = True
            continue
        if _PORTABLE_LEGACY_CLOSE in line:
            in_legacy = False
            continue
        if in_legacy:
            continue
        if "venv/bin/python" in stripped:
            hits.append(f"{path}:{idx}")
    return hits


def _check_portable_tier() -> dict:
    """`SKILL.md` / `dispatch-templates.md` carry no `venv/bin/python`
    literals outside legacy-example marker blocks (TASK-008 fixes).

    Advisory tier: registered but excluded from the default verdict
    until TASK-008 lands; surfaces the exact path:line of every
    violation so the cleanup task has a precise punch list.
    """
    skill = (
        Path(__file__).resolve().parent.parent
        / "skills" / "implement-plan" / "SKILL.md"
    )
    templates = (
        Path(__file__).resolve().parent.parent
        / "skills" / "implement-plan" / "dispatch-templates.md"
    )
    canonical_payload = {
        "source": "TASK-008 portable-Python policy",
        "value": (
            "no `venv/bin/python` literals in SKILL.md / dispatch-templates.md "
            "outside <!-- portable_tier: legacy-example --> blocks"
        ),
    }
    hits: list[str] = []
    hits.extend(_scan_portable_tier_violations(skill))
    hits.extend(_scan_portable_tier_violations(templates))
    actual_payload = {
        "source": "SKILL.md + dispatch-templates.md",
        "value": {"violations": hits},
    }
    if hits:
        # `hits` are `path:lineno` strings (or `path: unreadable`); split
        # into structured locations so consumers don't have to re-parse.
        locations: list[dict] = []
        for hit in hits:
            head, sep, tail = hit.rpartition(":")
            if sep and tail.isdigit():
                hit_path = _audit_relpath(Path(head))
                hit_line: int | None = int(tail)
            else:
                hit_path = _audit_relpath(Path(hit.split(":", 1)[0]))
                hit_line = None
            locations.append({
                "path": hit_path,
                "line": hit_line,
                "reason": "`venv/bin/python` literal outside legacy-example marker block",
            })
        return _audit_finding(
            check="portable_tier",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=(
                f"{len(hits)} `venv/bin/python` literal(s) outside "
                f"legacy-example markers; first few: {hits[:5]}"
            ),
            tier="advisory",
            locations=locations,
        )
    return _audit_finding(
        check="portable_tier",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
        tier="advisory",
    )


def _check_wrapper_isolation() -> dict:
    """`plan_codex_dispatch.py` carries the State-Isolation invariants.

    Asserts (1) the always-ignore / protected-paths import seam from
    `_plan_paths`, (2) `_snapshot_baseline(` at the three required
    seams (implement, timeout-cleanup, review), and (3) no
    `git clean -fd` in executable code at repo scope. Distinct from
    `execution-safe`/`review-safe` gates because audit reports
    path:line evidence, not just pass/fail.
    """
    wrapper = _SCRIPT_DIR / "plan_codex_dispatch.py"
    canonical_payload = {
        "source": "TASK-003 State-Isolation Contract",
        "value": {
            "always_ignore_seam": (
                "from _plan_paths import PROTECTED_EXACT_PATHS, ..."
            ),
            "snapshot_baseline_seams": [
                "cmd_implement", "_handle_timeout_cleanup or kwarg",
                "cmd_review",
            ],
            "forbidden": "git clean -fd outside comments / docstrings",
        },
    }
    wrapper_relpath = _audit_relpath(wrapper)
    if not wrapper.is_file():
        return _audit_finding(
            check="wrapper_isolation",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(wrapper), "value": None},
            reason=f"wrapper not found: {wrapper}",
            locations=[{
                "path": wrapper_relpath, "line": None,
                "reason": f"wrapper not found: {wrapper}",
            }],
        )
    try:
        text = wrapper.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="wrapper_isolation",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(wrapper), "value": None},
            reason=f"wrapper unreadable: {e}",
            locations=[{
                "path": wrapper_relpath, "line": None,
                "reason": f"wrapper unreadable: {e}",
            }],
        )
    code_text = _strip_python_comments_and_docstrings(text)
    problems: list[str] = []
    wrapper_locations: list[dict] = []

    def _wrapper_def_line(name: str) -> int | None:
        return _audit_locate_def(name, source_text=text)

    # (1) always-ignore seam
    has_always_ignore = (
        "PROTECTED_EXACT_PATHS" in code_text and "_plan_paths" in code_text
    )
    if not has_always_ignore:
        msg = "missing _plan_paths protected-paths import seam"
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath, "line": None, "reason": msg,
        })

    # (2) snapshot-baseline seams (implement + cleanup + review)
    def _function_body(name: str) -> str | None:
        m = re.search(rf"^def {re.escape(name)}\s*\(", code_text, re.MULTILINE)
        if not m:
            return None
        tail = code_text[m.end():]
        next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
        return tail[: next_def.start()] if next_def else tail

    impl_body = _function_body("cmd_implement")
    cleanup_body = _function_body("_handle_timeout_cleanup")
    review_body = _function_body("cmd_review")
    snapshot_seams: list[str] = []
    if impl_body and "_snapshot_baseline(" in impl_body:
        snapshot_seams.append("cmd_implement")
    else:
        msg = "_snapshot_baseline(... missing in cmd_implement"
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath,
            "line": _wrapper_def_line("cmd_implement"),
            "reason": msg,
        })
    cleanup_seam_ok = False
    if cleanup_body and "_snapshot_baseline(" in cleanup_body:
        cleanup_seam_ok = True
        snapshot_seams.append("_handle_timeout_cleanup")
    elif impl_body and re.search(
        r"_handle_timeout_cleanup\s*\([^)]*baseline", impl_body, re.DOTALL,
    ):
        cleanup_seam_ok = True
        snapshot_seams.append("cmd_implement->_handle_timeout_cleanup(baseline)")
    if not cleanup_seam_ok:
        msg = (
            "_snapshot_baseline missing in timeout-cleanup window "
            "(neither _handle_timeout_cleanup body nor cmd_implement "
            "baseline kwarg)"
        )
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath,
            "line": _wrapper_def_line("_handle_timeout_cleanup"),
            "reason": msg,
        })
    if review_body and "_snapshot_baseline(" in review_body:
        snapshot_seams.append("cmd_review")
    else:
        msg = "_snapshot_baseline(... missing in cmd_review"
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath,
            "line": _wrapper_def_line("cmd_review"),
            "reason": msg,
        })

    # (3) `git clean -fd` outside comments/docstrings.
    forbidden_hits: list[str] = []
    forbidden_lines: list[int] = []
    in_triple_double = False
    in_triple_single = False
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        double_marks = line.count('"""')
        single_marks = line.count("'''")
        line_was_in_string = in_triple_double or in_triple_single
        if double_marks % 2 == 1:
            in_triple_double = not in_triple_double
        if single_marks % 2 == 1:
            in_triple_single = not in_triple_single
        if line_was_in_string or in_triple_double or in_triple_single:
            continue
        if stripped.startswith("#"):
            continue
        if re.search(r"git\s+clean\s+-fd", line):
            forbidden_hits.append(f"plan_codex_dispatch.py:{idx}")
            forbidden_lines.append(idx)
    if forbidden_hits:
        problems.append(
            f"`git clean -fd` outside comments at: {forbidden_hits}"
        )
        for ln in forbidden_lines:
            wrapper_locations.append({
                "path": wrapper_relpath, "line": ln,
                "reason": "`git clean -fd` outside comments / docstrings",
            })

    actual_payload = {
        "source": "plan_codex_dispatch.py",
        "value": {
            "always_ignore_seam_present": has_always_ignore,
            "snapshot_baseline_seams": snapshot_seams,
            "git_clean_fd_hits": forbidden_hits,
        },
    }
    if problems:
        return _audit_finding(
            check="wrapper_isolation",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=wrapper_locations,
        )
    return _audit_finding(
        check="wrapper_isolation",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_design_doc_orphans() -> dict:
    """`DUAL_AGENT_PLAN_EXECUTOR.md` references no deprecated stubs.

    Specifically: no `--skip-analysis` flag (renamed to `--skip-cross-review`
    long ago), and any reference to `task_id` / `batch_index` is paired
    with the canonical `id` / `index` literal so a reader is not left
    with a contradictory mental model. Reports the offending line ranges.
    """
    repo_root = Path(__file__).resolve().parents[3]
    doc = repo_root / "docs" / "plans" / "DUAL_AGENT_PLAN_EXECUTOR.md"
    canonical_payload = {
        "source": "TASK-001 canonical contract + design doc §5/§7.2",
        "value": {
            "deprecated_flags": ["--skip-analysis"],
            "alias_field_handling": (
                "schedule field aliases task_id/batch_index must be "
                "named alongside canonical id/index"
            ),
        },
    }
    doc_relpath = _audit_relpath(doc)
    if not doc.is_file():
        return _audit_finding(
            check="design_doc_orphans",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(doc), "value": None},
            reason=f"design doc not found: {doc}",
            locations=[{
                "path": doc_relpath, "line": None,
                "reason": f"design doc not found: {doc}",
            }],
        )
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="design_doc_orphans",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(doc), "value": None},
            reason=f"design doc unreadable: {e}",
            locations=[{
                "path": doc_relpath, "line": None,
                "reason": f"design doc unreadable: {e}",
            }],
        )
    problems: list[str] = []
    doc_locations: list[dict] = []
    skip_analysis_hits: list[int] = []
    # Lines that explicitly mark the reference as historical /
    # deferred / discussed-but-not-implemented are allowed; we are
    # hunting for stubs that contradict the canonical decision, not
    # for an honest design-doc note about a deferred flag. Heuristic
    # markers: "deferred", "deprecated", "removed", "legacy",
    # "do not add", "not implemented" — the design doc uses these
    # words consistently when discussing flags it explicitly chose
    # not to ship.
    _HISTORICAL_MARKERS = (
        "deferred", "deprecated", "removed", "legacy",
        "do not add", "not implemented", "do NOT add",
    )
    for idx, line in enumerate(text.splitlines(), 1):
        if "--skip-analysis" not in line:
            continue
        lower = line.lower()
        if any(marker in lower for marker in _HISTORICAL_MARKERS):
            continue
        skip_analysis_hits.append(idx)
    if skip_analysis_hits:
        problems.append(
            f"`--skip-analysis` references at lines: {skip_analysis_hits}"
        )
        for ln in skip_analysis_hits:
            doc_locations.append({
                "path": doc_relpath, "line": ln,
                "reason": "deprecated `--skip-analysis` flag reference",
            })
    # Alias field handling: `task_id` and `batch_index` are legitimate to
    # mention as aliases, but only if the canonical `id` / `index`
    # literals appear in the same document. (They do.)
    has_id_canonical = bool(re.search(r"\bid\b", text))
    has_index_canonical = bool(re.search(r"\bindex\b", text))
    if not (has_id_canonical and has_index_canonical):
        msg = (
            "design doc mentions schedule fields but canonical "
            "`id` / `index` literals are absent"
        )
        problems.append(msg)
        doc_locations.append({
            "path": doc_relpath, "line": None, "reason": msg,
        })
    actual_payload = {
        "source": "DUAL_AGENT_PLAN_EXECUTOR.md",
        "value": {
            "skip_analysis_hits": skip_analysis_hits,
            "has_id_literal": has_id_canonical,
            "has_index_literal": has_index_canonical,
        },
    }
    if problems:
        return _audit_finding(
            check="design_doc_orphans",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=doc_locations,
        )
    return _audit_finding(
        check="design_doc_orphans",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


# Ordered registry. The order is the canonical `--list` output and the
# row order in the Markdown report. Append new checks to the end so
# downstream tooling that snapshots `--list` does not drift.
AUDIT_CHECKS: tuple[tuple[str, object, str], ...] = (
    ("status_vocabulary", _check_status_vocabulary, "default"),
    ("schedule_wire_format", _check_schedule_wire_format, "default"),
    ("implementer_report_labels", _check_implementer_report_labels, "default"),
    ("execution_log_columns", _check_execution_log_columns, "default"),
    ("schemas", _check_schemas, "default"),
    ("portable_tier", _check_portable_tier, "advisory"),
    ("wrapper_isolation", _check_wrapper_isolation, "default"),
    ("design_doc_orphans", _check_design_doc_orphans, "default"),
)
AUDIT_CHECK_NAMES: tuple[str, ...] = tuple(name for name, _, _ in AUDIT_CHECKS)
AUDIT_CHECK_TIERS: dict[str, str] = {name: tier for name, _, tier in AUDIT_CHECKS}


def _render_audit_text(report: dict) -> str:
    """Render the audit report as a human-readable plaintext summary.

    Used by `cmd_audit` when `--json` is absent so stdout carries a
    readable digest instead of Python's default dict repr. Format:
      * Header `audit: <overall>`
      * One blank line
      * Per-finding bullet `  - [<tier>] <check>: <reason>` (reason is
        `ok` when clean)
      * Indented `locations:` block when the finding carries entries

    `<tier>` is `finding["severity"]` when present (forward-compatible
    with a future shape that adds one) else `finding["tier"]`
    (default/advisory) — the closest thing the existing
    `_audit_finding` shape has to a severity.
    """
    overall = report.get("overall", "fail")
    lines: list[str] = [f"audit: {overall}", ""]
    for finding in report.get("findings", []):
        check = finding.get("check", "")
        tier = finding.get("severity") or finding.get("tier", "")
        reason = finding.get("reason") or "ok"
        lines.append(f"  - [{tier}] {check}: {reason}")
        locations = finding.get("locations") or []
        if locations:
            lines.append("    locations:")
            for loc in locations:
                path = loc.get("path", "")
                line_no = loc.get("line")
                line_suffix = f":{line_no}" if line_no is not None else ""
                loc_reason = loc.get("reason", "")
                lines.append(f"      - {path}{line_suffix}: {loc_reason}")
    return "\n".join(lines) + "\n"


def _render_audit_markdown(report: dict) -> str:
    """Render the audit report as a Markdown table for `--report-file`.

    Columns: Check | Tier | Status | Canonical | Actual | Reason. The
    Canonical and Actual columns are JSON-serialized so the row order is
    stable across reruns; long values are truncated to keep the table
    readable in a terminal (the JSON output remains the source of truth).
    """
    lines: list[str] = [
        f"# Executor self-audit — {report.get('generated_at', '')}",
        "",
        f"**Overall:** `{report.get('overall', 'fail')}`",
        "",
        "| Check | Tier | Status | Canonical | Actual | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for finding in report.get("findings", []):
        canonical_str = json.dumps(finding.get("canonical", {}), sort_keys=True)
        actual_str = json.dumps(finding.get("actual", {}), sort_keys=True)
        if len(canonical_str) > 200:
            canonical_str = canonical_str[:197] + "..."
        if len(actual_str) > 200:
            actual_str = actual_str[:197] + "..."
        reason = finding.get("reason") or ""
        lines.append(
            "| `{check}` | {tier} | `{status}` | {canonical} | {actual} | {reason} |".format(
                check=finding.get("check", ""),
                tier=finding.get("tier", ""),
                status=finding.get("status", ""),
                canonical=canonical_str.replace("|", "\\|"),
                actual=actual_str.replace("|", "\\|"),
                reason=reason.replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def cmd_audit(args: argparse.Namespace) -> None:
    """Self-audit CLI: --list | --json | --report-file | --check <csv>.

    See the `## TASK-007` section in `DUAL_AGENT_PLAN_EXECUTOR.md §14` for
    the operator-facing documentation of what this surfaces and when to
    run it.
    """
    if args.list:
        annotated = [
            {"name": name, "tier": AUDIT_CHECK_TIERS[name]}
            for name in AUDIT_CHECK_NAMES
        ]
        _emit(args, {"checks": annotated})
        return

    requested: list[str] | None = None
    if args.check:
        requested = [s.strip() for s in args.check.split(",") if s.strip()]
        unknown = [name for name in requested if name not in AUDIT_CHECK_NAMES]
        if unknown:
            _die(args, {
                "error": (
                    f"unknown audit check name(s): {unknown}; "
                    f"known: {list(AUDIT_CHECK_NAMES)}"
                ),
            })

    findings: list[dict] = []
    for name, fn, tier in AUDIT_CHECKS:
        if requested is not None and name not in requested:
            continue
        # Default run includes ALL checks (default + advisory) so operators
        # see advisory drift in the report. Advisory findings are still
        # excluded from the verdict unless --strict or explicit --check
        # opted in by name (see `_is_verdict_finding`). This matches the
        # spec at TASK-007_self_audit.md §step-4 line 241: "Advisory-tier
        # findings still appear in the report regardless of whether they
        # were included in the verdict, with their tier annotated."
        finding = fn()
        findings.append(finding)

    # Verdict computation.
    #   * Explicit --check subset: every requested check counts toward the
    #     verdict regardless of tier (the operator opted in by name).
    #   * Default + --strict: advisory findings count too.
    #   * Default (no --strict, no --check): advisory findings reported
    #     but excluded from the verdict.
    def _is_verdict_finding(f: dict) -> bool:
        if requested is not None:
            return True
        if args.strict:
            return True
        return f.get("tier") != "advisory"

    overall = "pass"
    for f in findings:
        if _is_verdict_finding(f) and f.get("status") == "fail":
            overall = "fail"
            break

    report = {
        "overall": overall,
        "findings": findings,
        "generated_at": _now(),
    }

    if args.report_file:
        out = Path(args.report_file)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_render_audit_markdown(report), encoding="utf-8")
        except OSError as e:
            _die(args, {
                "error": f"failed to write --report-file {out}: {e}",
            })

    exit_code = 0 if overall == "pass" else 1
    # Non-JSON path uses a structured plaintext renderer instead of the
    # default dict-repr fallback in `_emit`; `--json` still emits the
    # canonical JSON document as before.
    if not getattr(args, "json", False):
        sys.stdout.write(_render_audit_text(report))
        sys.exit(exit_code)
    _emit(args, report, exit_code=exit_code)


def cmd_gates(args: argparse.Namespace) -> None:
    """Phase-gate CLI: --list | --check <csv> | --certify --mode <m>.

    --list emits the six canonical gate names.
    --check runs the subset named on the CLI; each gate returns the
      documented `{name, status, reason}` shape.
    --certify runs the dry-run or execute bundle against a plan file;
      emits `{certified: bool, gates: {...}}`.
    """
    if args.list:
        _emit(args, {"gates": list(GATE_NAMES)})
        return

    if args.certify:
        if args.mode not in {"dry-run", "execute"}:
            _die(args, {"error": "mode must be dry-run|execute"})
        if not args.plan_file:
            _die(args, {"error": "--certify requires --plan-file"})
        # `schedule-valid` is a required member of the certification bundle
        # (acceptance criteria: dry-run pass requires it green; execute is
        # the dry-run set plus commit-safe). Skipping it when --schedule-file
        # is absent produced a false-positive certification path, so fail
        # fast at the CLI seam instead of emitting `not_applicable`.
        if not args.schedule_file:
            _die(args, {"error": "--certify requires --schedule-file"})
        if args.mode == "dry-run":
            gates = _certify_dry_run(args.plan_file, args.schedule_file)
        else:
            # Execute certification re-verifies commit-safe per landed
            # commit; without a --run-id there is no way to identify the
            # bundle of commits to check, so certification would silently
            # report commit-safe: not_applicable and pass.
            if not args.run_id:
                _die(args, {
                    "error": "--certify --mode execute requires --run-id",
                })
            gates = _certify_execute(
                args.plan_file, args.run_id, args.schedule_file,
            )
        by_name = {g["name"]: {"status": g["status"], "reason": g["reason"]} for g in gates}
        # Canonical status vocabulary: pass | fail | not_applicable.
        # Certification passes iff every applicable gate is `pass`; a
        # `not_applicable` gate does not block certification.
        certified = all(g["status"] in {"pass", "not_applicable"} for g in gates)
        _emit(
            args,
            {"certified": certified, "mode": args.mode, "gates": by_name},
            exit_code=0 if certified else 1,
        )
        return

    if not args.check:
        _die(args, {
            "error": "gates requires one of --list, --check, or --certify",
        })
    requested = [s.strip() for s in args.check.split(",") if s.strip()]
    unknown = [g for g in requested if g not in GATE_NAMES]
    if unknown:
        _die(args, {"error": f"unknown gate name(s): {unknown}; known: {list(GATE_NAMES)}"})

    results: list[dict] = []
    for name in requested:
        if name == "schema-valid":
            results.append(_gate_schema_valid(args.plan_file))
        elif name == "schedule-valid":
            results.append(_gate_schedule_valid(args.schedule_file))
        elif name == "fixture-valid":
            # The gate invariant is about the canonical sample fixture
            # (`docs/plans/sample_phase4.md`), not the user's plan file.
            # Passing `args.plan_file` here caused Phase 0 to validate the
            # execution plan twice (via schema-valid and fixture-valid) and
            # skip the sample fixture gate entirely — the opposite of the
            # acceptance criteria.
            results.append(_gate_fixture_valid())
        elif name == "execution-safe":
            results.append(_gate_execution_safe())
        elif name == "review-safe":
            results.append(_gate_review_safe())
        elif name == "commit-safe":
            results.append(_gate_commit_safe(
                args.commit_sha, args.task_id, args.plan_file,
            ))
    any_failed = any(r["status"] == "fail" for r in results)
    _emit(
        args,
        {"gates": results, "failed": any_failed},
        exit_code=1 if any_failed else 0,
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan operations CLI for /implement-plan workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="Smart dirty-tree + codex + run-id")
    p_pre.add_argument("--plan-file", required=True, help="Absolute path to plan file")
    p_pre.add_argument("--strict-branch", action="store_true",
                       help="Halt (not warn) if current branch != plan's Base branch")
    p_pre.add_argument("--strict-scope", action="store_true",
                       help="Halt if any dirty file lies within the plan's declared scope")
    _add_json(p_pre)

    p_sched = sub.add_parser("parse-schedule", help="Validate analyst JSON shape")
    p_sched.add_argument("--stdin", action="store_true", required=True,
                         help="Read JSON schedule from stdin")
    p_sched.add_argument("--strict", action="store_true",
                         help="Promote unknown nested fields from warning to error")
    _add_json(p_sched)

    p_comp = sub.add_parser("compute-schedule", help="Compute priority order + disjoint batches")
    p_comp.add_argument("--stdin", action="store_true", required=True,
                        help="Read JSON schedule from stdin")
    p_comp.add_argument("--strict", action="store_true",
                        help="Accepted for interface parity; currently unused")
    _add_json(p_comp)

    p_ws = sub.add_parser("write-schedule",
                          help="Validate + atomically persist schedule JSON")
    p_ws.add_argument("--schedule-file", required=True,
                      help="Destination path for persisted schedule JSON")
    p_ws.add_argument("--stdin", action="store_true", required=True,
                      help="Read JSON schedule from stdin")
    p_ws.add_argument("--strict", action="store_true",
                      help="Promote unknown nested fields from warning to error")
    _add_json(p_ws)

    p_batch = sub.add_parser("batch-next", help="Select next batch respecting file locks")
    p_batch.add_argument("--schedule-file", required=True, help="Path to schedule JSON")
    p_batch.add_argument("--locked-files", default="", help="Comma-separated locked files")
    p_batch.add_argument("--done", default="", help="Comma-separated done task ids")
    p_batch.add_argument("--failed", default="", help="Comma-separated failed task ids")
    p_batch.add_argument("--parallel", type=int, default=2, help="Max concurrent tasks")
    _add_json(p_batch)

    p_fs = sub.add_parser(
        "filter-schedule",
        help="Filter schedule by --task-ids and emit canonical schedule on stdout",
    )
    p_fs.add_argument("--schedule-file", required=True,
                      help="Path to source schedule JSON")
    p_fs.add_argument("--task-ids", required=True,
                      help="CSV of task ids; canonical, plain, or TASK-NNN forms")
    _add_json(p_fs)

    p_rep = sub.add_parser("parse-implementer-report",
                           help="Parse plan-implementer markdown report")
    p_rep.add_argument("--stdin", action="store_true", required=True,
                       help="Read report markdown from stdin")
    _add_json(p_rep)

    p_prr = sub.add_parser(
        "parse-plan-review-report",
        help=(
            "Validate a Phase 1.5 Codex plan-review envelope against "
            "codex_plan_review_schema.json; surface verdict + findings"
        ),
    )
    p_prr.add_argument("--stdin", action="store_true", required=True,
                       help="Read plan-review envelope JSON from stdin")
    _add_json(p_prr)

    p_d5 = sub.add_parser(
        "parse-d5-adjudication",
        help=(
            "Validate a D.5 adjudication payload and surface the "
            "structured dispatch fields (verdict, summary, load_bearing, "
            "dismissed). Partial-agreement payloads are checked for "
            "disjoint, in-range index splits; malformed splits exit with "
            "partial-agreement-invalid-split / -unknown-index codes."
        ),
    )
    p_d5.add_argument("--stdin", action="store_true", required=True,
                      help="Read D.5 adjudication JSON payload from stdin")
    p_d5.add_argument(
        "--codex-findings-count", type=int, required=True,
        help=(
            "Length of the Codex parsed.findings[] array the D.5 reviewer "
            "was adjudicating; partial-agreement indices must stay within "
            "range(0, count)."
        ),
    )
    _add_json(p_d5)

    p_commit = sub.add_parser("commit-task", help="Narrow commit + status flip + run log append")
    p_commit.add_argument("--plan-file", required=True)
    p_commit.add_argument("--task-id", required=True)
    p_commit.add_argument("--run-id", required=True)
    p_commit.add_argument("--files", required=True, help="Comma-separated files to commit")
    p_commit.add_argument("--title", required=True)
    p_commit.add_argument("--diff-summary", required=True)
    p_commit.add_argument("--reviewer", choices=["codex", "claude", "none"], default="none")
    p_commit.add_argument("--reviewer-verdict", default="")
    p_commit.add_argument("--reviewer-minor-findings", default="[]",
                          help="JSON array of minor findings")
    # TASK-016C: a commit cannot be both a D.2a.5 full remediation AND a
    # D.2a.6 narrow remediation — they are parallel retry paths with
    # distinct trailer shapes. Enforced at argparse rather than the
    # log-event layer to catch operator error on the CLI, before a
    # malformed commit is written.
    p_commit_rem_grp = p_commit.add_mutually_exclusive_group()
    p_commit_rem_grp.add_argument(
        "--remediation-tag", action="store_true",
        help=(
            "Mark commit as a D.2a.5 post-remediation retry. "
            "Appends a [remediation] tag line to the commit body."
        ),
    )
    p_commit_rem_grp.add_argument(
        "--narrow-remediation-tag", action="store_true",
        help=(
            "Mark commit as a D.2a.6 narrow-remediation retry. "
            "Appends a [narrow-remediation] tag line to the commit "
            "body (adjacent to the [disagreement: i,j] trailer). "
            "Mutually exclusive with --remediation-tag; requires a "
            "non-empty --dismissed-finding-ids."
        ),
    )
    # TASK-016C: a commit cannot be both "D.5 disagrees with all Codex
    # findings" (bare --disagreement-tag) AND "D.5 disagrees with a
    # subset" (--dismissed-finding-ids i,j). The bare form is used by
    # D.5 verdicts `ship | ship-with-fixes`; the with-indices form only
    # appears on the partial-agreement path.
    p_commit_dis_grp = p_commit.add_mutually_exclusive_group()
    p_commit_dis_grp.add_argument(
        "--disagreement-tag", action="store_true",
        help="Mark commit as §8.4 disagreement (bare [disagreement] tag)",
    )
    p_commit_dis_grp.add_argument(
        "--dismissed-finding-ids", default="",
        help=(
            "Comma-separated integer indices of Codex findings that "
            "D.5 dismissed on the partial-agreement path (0-based "
            "into the original Codex findings array). Emits a "
            "[disagreement: I,J,K] trailer adjacent to the "
            "[narrow-remediation] tag. Requires --narrow-remediation-tag; "
            "mutually exclusive with --disagreement-tag."
        ),
    )
    p_commit.add_argument("--dry-run", action="store_true")
    # TASK-020B: cap the opt-in `acceptance_v_check` runtime. Default 300s;
    # plans that need longer pass `--v-check-timeout SECONDS` explicitly.
    # Only consulted when the plan carries the YAML frontmatter key.
    p_commit.add_argument(
        "--v-check-timeout", type=int, default=300,
        help=(
            "Timeout in seconds for the opt-in acceptance_v_check. "
            "Default 300. Ignored if the plan has no frontmatter key."
        ),
    )
    _add_json(p_commit)

    p_fail = sub.add_parser("fail-task", help="Restore files + status flip + run log append")
    p_fail.add_argument("--plan-file", required=True)
    p_fail.add_argument("--task-id", required=True)
    p_fail.add_argument("--run-id", required=True)
    p_fail.add_argument("--files", default="", help="Comma-separated files to git restore")
    p_fail.add_argument("--stage", required=True, choices=sorted(ALLOWED_FAIL_STAGES))
    p_fail.add_argument("--reason", required=True)
    p_fail.add_argument("--reviewer-findings", default="",
                        help="JSON blob of reviewer findings (stage=review)")
    p_fail.add_argument("--reversion-guidance", default="",
                        help="Implementer-supplied reversion guidance (stage=implement)")
    p_fail.add_argument("--repo-root", default=None,
                        help="Repo root for path resolution; defaults to CWD")
    p_fail.add_argument("--dry-run", action="store_true")
    _add_json(p_fail)

    p_block = sub.add_parser(
        "block-dependents",
        help=(
            "Cascade `blocked` status onto dependents of a failed task. "
            "Mutates the plan markdown (single read, single write) and "
            "appends `blocked` run-log events."
        ),
    )
    p_block.add_argument("--schedule-file", required=True,
                         help="Path to schedule JSON")
    p_block.add_argument("--plan-file", required=True,
                         help="Absolute path to plan file")
    p_block.add_argument("--failed", required=True,
                         help="Task id whose failure triggers the cascade")
    p_block.add_argument("--run-id", required=True, help="Run id for log events")
    _add_json(p_block)

    p_hdr = sub.add_parser("update-plan-header", help="Mutate **Status:** in plan header block")
    p_hdr.add_argument("--plan-file", required=True)
    p_hdr.add_argument("--status", required=True, choices=sorted(ALLOWED_PLAN_STATUSES))
    _add_json(p_hdr)

    p_fin = sub.add_parser("finalize-execution-log",
                           help="Append §5 execution-log markdown table to plan file")
    p_fin.add_argument("--plan-file", required=True)
    p_fin.add_argument("--run-id", required=True)
    p_fin.add_argument("--starting-sha", required=True)
    p_fin.add_argument("--ending-sha", required=True)
    p_fin.add_argument("--rows-json", required=True, help="JSON array of row dicts")
    p_fin.add_argument(
        "--outcome",
        default=None,
        choices=sorted(ALLOWED_RUN_OUTCOMES),
        help=(
            "Run outcome recorded in the execution-log header. "
            "`paused` signals a D.2a.5 awaiting-user halt; the next "
            "conversation turn decides disposition. Optional; omitted "
            "preserves the legacy (unlabelled) header for callers that "
            "have not yet migrated."
        ),
    )
    _add_json(p_fin)

    p_log = sub.add_parser("log-event", help="Append JSONL event with && tail -1 verification")
    p_log.add_argument("--event", required=True, help="Event name")
    p_log.add_argument("--fields-json", required=True, help="JSON fields dict")
    # TASK-022: optional full-finding payload for `review_done` /
    # `disagreement` events. Validated via `_validate_minor_findings_payload`
    # (same schema as `commit-task --reviewer-minor-findings`). Embedded
    # verbatim under key `findings` on the JSONL line when present.
    p_log.add_argument(
        "--findings-json",
        dest="findings_json",
        default=None,
        help=(
            "Optional JSON array of reviewer findings to embed verbatim "
            "under key 'findings' in the log line. Validated via the "
            "reviewer-minor-findings schema."
        ),
    )
    _add_json(p_log)

    p_norm = sub.add_parser("normalize-task-id", help="Canonicalize task id to 3-digit form")
    p_norm.add_argument("--id", required=True, help="Accepts 1 | 001 | TASK-001")
    _add_json(p_norm)

    p_acq = sub.add_parser("acquire-lock", help="Acquire run-lock for this plan file")
    p_acq.add_argument("--plan-file", required=True)
    p_acq.add_argument("--run-id", required=True)
    p_acq.add_argument(
        "--force",
        action="store_true",
        help="Overwrite lock file even if malformed / owned by other plans",
    )
    _add_json(p_acq)

    p_rel = sub.add_parser("release-lock", help="Release run-lock for this plan file")
    p_rel.add_argument("--plan-file", required=True)
    p_rel.add_argument("--run-id", required=True)
    _add_json(p_rel)

    p_rec = sub.add_parser(
        "reconcile-batch",
        help="Reconcile observed out-of-scope writes from a completed batch",
    )
    p_rec.add_argument("--repo-root", required=True, help="Absolute path to repo root")
    _add_json(p_rec)

    p_cpd = sub.add_parser(
        "check-plan-deps",
        help="Resolve cross-plan prerequisites via 00_INDEX.json",
    )
    p_cpd.add_argument("--plan-file", required=True, help="Absolute path to plan file")
    p_cpd.add_argument(
        "--plans-dir", required=True,
        help="Directory containing 00_INDEX.json and sibling plan files",
    )
    _add_json(p_cpd)

    p_pi = sub.add_parser(
        "path-info",
        help="Emit configured plan_dir + derived run_log/run_lock/schedule_glob paths",
    )
    _add_json(p_pi)

    # TASK-020A: read-only lint that cross-references `**Status:** done` /
    # `partial` task markers against the run log's `commit_done` events and
    # the git log's `feat(TASK-NNN):` commits. Hand-edited status markers
    # that bypassed `commit-task` flunk the lint. CI wiring is deferred to
    # TASK-020C.
    p_lint = sub.add_parser(
        "lint-plans",
        help=(
            "Cross-reference **Status:** done/partial task markers against "
            "commit_done run-log events and feat(TASK-NNN) git commits. "
            "Read-only; exit 1 on any finding."
        ),
    )
    p_lint.add_argument(
        "--plans-dir", required=True,
        help="Directory containing plan markdown files (scanned recursively)",
    )
    p_lint.add_argument(
        "--run-log", default=None,
        help="Path to _run_log.jsonl; omitted = empty run log",
    )
    p_lint.add_argument(
        "--git-dir", default=None,
        help="Path to git work tree root; defaults to CWD",
    )
    _add_json(p_lint)

    # TASK-007: protocol-drift self-audit. Inspects the shipped artifacts
    # (plan_ops.py, plan_codex_dispatch.py, schema sidecars, SKILL.md,
    # dispatch templates, design doc) against `CANONICAL_CONTRACT` and
    # surfaces drift as structured findings. Distinct from `gates` (which
    # is runtime-scoped to a specific execution); audit is standing /
    # cross-cutting and intended to run before any rerun.
    p_audit = sub.add_parser(
        "audit",
        help=(
            "Self-audit the executor for protocol drift "
            "(status vocabulary, schedule wire format, implementer-report "
            "labels, execution-log columns, schemas, portable tier, "
            "wrapper isolation, design-doc orphans)."
        ),
    )
    p_audit.add_argument(
        "--list", action="store_true",
        help="Emit the canonical list of audit check names + tiers",
    )
    p_audit.add_argument(
        "--check", default=None,
        help=(
            "Comma-separated subset of checks to run; overrides tier "
            "filtering so advisory checks may be requested by name"
        ),
    )
    p_audit.add_argument(
        "--strict", action="store_true",
        help=(
            "Include advisory checks (e.g. portable_tier pre-TASK-008) "
            "in the overall verdict"
        ),
    )
    p_audit.add_argument(
        "--report-file", default=None,
        help=(
            "Optional path; writes a Markdown table report alongside "
            "the stdout JSON/summary"
        ),
    )
    _add_json(p_audit)

    # TASK-005: phase-gate and promotion-criteria subcommand. Six canonical
    # gates — schema-valid, schedule-valid, fixture-valid, execution-safe,
    # review-safe, commit-safe — each returning {name, status, reason}.
    # --list enumerates names, --check runs one-or-more predicates, and
    # --certify bundles the dry-run or execute subset for end-of-phase
    # promotion. See §9.7 of DUAL_AGENT_PLAN_EXECUTOR.md.
    p_gates = sub.add_parser(
        "gates",
        help=(
            "Run the six canonical phase gates (schema-valid, schedule-valid, "
            "fixture-valid, execution-safe, review-safe, commit-safe) either "
            "individually (--check) or as a dry-run/execute certification "
            "bundle (--certify)."
        ),
    )
    mx_gates = p_gates.add_mutually_exclusive_group(required=True)
    mx_gates.add_argument(
        "--list", action="store_true",
        help="Emit the canonical list of gate names",
    )
    mx_gates.add_argument(
        "--check", default=None,
        help=(
            "Comma-separated gate names to evaluate; fails with exit 1 if "
            "any checked gate returns status=fail"
        ),
    )
    mx_gates.add_argument(
        "--certify", action="store_true",
        help=(
            "Certify the promotion bundle for --mode (dry-run|execute); "
            "exit 1 if any required gate in the bundle returns status=fail"
        ),
    )
    p_gates.add_argument(
        "--mode", choices=("dry-run", "execute"), default=None,
        help="Required with --certify; selects the promotion bundle",
    )
    p_gates.add_argument(
        "--plan-file", default=None,
        help="Path to plan markdown file (for schema-valid/fixture-valid)",
    )
    p_gates.add_argument(
        "--schedule-file", default=None,
        help="Path to schedule JSON file (for schedule-valid)",
    )
    p_gates.add_argument(
        "--commit-sha", default=None,
        help="Commit SHA to inspect for commit-safe",
    )
    p_gates.add_argument(
        "--task-id", default=None,
        help="Task id whose Files: list scopes commit-safe allowed paths",
    )
    p_gates.add_argument(
        "--run-id", default=None,
        help="Run identifier for certify-execute commit-safe verification",
    )
    _add_json(p_gates)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    # TASK-016C: cross-flag constraints that `add_mutually_exclusive_group`
    # cannot express directly. These are enforced here so `parser.error()`
    # produces the standard argparse exit-code-2 + usage banner; an
    # operator mistake on the CLI halts before any commit is written.
    if getattr(args, "command", None) == "commit-task":
        narrow = bool(getattr(args, "narrow_remediation_tag", False))
        dismissed_raw = getattr(args, "dismissed_finding_ids", "") or ""
        has_dismissed = bool(dismissed_raw.strip())
        # (c) --dismissed-finding-ids requires --narrow-remediation-tag.
        if has_dismissed and not narrow:
            parser.error(
                "--dismissed-finding-ids requires --narrow-remediation-tag; "
                "the [disagreement: i,j] trailer only appears on the "
                "D.2a.6 narrow-remediation path"
            )
        # (d) --narrow-remediation-tag requires non-empty
        # --dismissed-finding-ids. A narrow-remediation commit without
        # dismissed indices is a contradiction — there would be nothing
        # for the [disagreement: i,j] trailer to record.
        if narrow and not has_dismissed:
            parser.error(
                "--narrow-remediation-tag requires a non-empty "
                "--dismissed-finding-ids; the partial-agreement path "
                "always carries at least one dismissed index"
            )
        # TASK-016C post-remediation: content-validate the
        # --dismissed-finding-ids comma list at argparse layer so empty
        # tokens (`','`, `'1,,3'`) and non-integer tokens (`'1,x'`) fail
        # via parser.error() with exit code 2, before cmd_commit_task
        # writes the plan or touches git. Overwrites the raw string on
        # args with the parsed list[int] so handlers consume a typed
        # value. When narrow-remediation is not set and the flag is
        # empty, the attribute collapses to an empty list.
        dismissed_parsed: list[int] = []
        if has_dismissed:
            for token in dismissed_raw.split(","):
                stripped = token.strip()
                if not stripped:
                    parser.error(
                        "--dismissed-finding-ids must not contain empty "
                        "comma-separated tokens (got "
                        f"{dismissed_raw!r})"
                    )
                try:
                    dismissed_parsed.append(int(stripped))
                except ValueError:
                    parser.error(
                        "--dismissed-finding-ids entry "
                        f"{stripped!r} is not a valid integer"
                    )
        args.dismissed_finding_ids = dismissed_parsed
    handlers = {
        "preflight": cmd_preflight,
        "parse-schedule": cmd_parse_schedule,
        "compute-schedule": cmd_compute_schedule,
        "write-schedule": cmd_write_schedule,
        "batch-next": cmd_batch_next,
        "filter-schedule": cmd_filter_schedule,
        "parse-implementer-report": cmd_parse_implementer_report,
        "parse-plan-review-report": cmd_parse_plan_review_report,
        "parse-d5-adjudication": cmd_parse_d5_adjudication,
        "commit-task": cmd_commit_task,
        "fail-task": cmd_fail_task,
        "block-dependents": cmd_block_dependents,
        "update-plan-header": cmd_update_plan_header,
        "finalize-execution-log": cmd_finalize_execution_log,
        "log-event": cmd_log_event,
        "normalize-task-id": cmd_normalize_task_id,
        "acquire-lock": cmd_acquire_lock,
        "release-lock": cmd_release_lock,
        "reconcile-batch": cmd_reconcile_batch,
        "check-plan-deps": cmd_check_plan_deps,
        "path-info": cmd_path_info,
        "lint-plans": cmd_lint_plans,
        "gates": cmd_gates,
        "audit": cmd_audit,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
