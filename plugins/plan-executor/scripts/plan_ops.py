#!/usr/bin/env python3
"""Plan operations CLI for the /implement-plan workflow.

Scaffolding: argparse surface + shared helpers + subcommand stubs.
Stdlib-only (plan docs are pure markdown per DUAL_AGENT_PLAN_EXECUTOR.md §5).

Usage:
    venv/bin/python scripts/plan_ops.py preflight --plan-file <abs> [--strict-branch]
    venv/bin/python scripts/plan_ops.py parse-schedule --stdin
    venv/bin/python scripts/plan_ops.py compute-schedule --stdin
    venv/bin/python scripts/plan_ops.py batch-next --schedule-file <path> ...
    venv/bin/python scripts/plan_ops.py parse-implementer-report --stdin
    venv/bin/python scripts/plan_ops.py parse-plan-review-report --stdin
    venv/bin/python scripts/plan_ops.py commit-task --plan-file <abs> --task-id NNN ...
    venv/bin/python scripts/plan_ops.py fail-task --plan-file <abs> --task-id NNN ...
    venv/bin/python scripts/plan_ops.py update-plan-header --plan-file <abs> --status <s>
    venv/bin/python scripts/plan_ops.py finalize-execution-log --plan-file <abs> ...
    venv/bin/python scripts/plan_ops.py log-event --event E --fields-json '{...}'
    venv/bin/python scripts/plan_ops.py normalize-task-id --id <1|001|TASK-001>
    venv/bin/python scripts/plan_ops.py acquire-lock --plan-file <abs> --run-id RID
    venv/bin/python scripts/plan_ops.py release-lock --plan-file <abs> --run-id RID
    venv/bin/python scripts/plan_ops.py block-dependents --schedule-file <path> --plan-file <abs> --failed NNN --run-id RID
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
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
    canonicalize_file,
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
    "awaiting_user",
    "schedule_written",
}
# Accepted values for `finalize-execution-log --outcome`. `paused` is added
# per TASK-014A for the D.2a.5 awaiting-user pause — the run halted mid-flight
# and the user's next turn decides disposition.
ALLOWED_RUN_OUTCOMES = {"success", "partial", "failed", "paused"}

CANONICAL_ID_RE = re.compile(r"^\d{3}[A-Z]?$")
ALLOWED_SCHEDULE_TOP_LEVEL = {"outcome", "tasks", "batches", "gaps", "risks"}
ALLOWED_TASK_FIELDS = {
    "id", "task_id",
    "title", "agent", "priority", "files", "dependencies",
    "test_command", "classification_reason", "acceptance_criteria",
}
ALLOWED_BATCH_FIELDS = {"index", "batch_index", "task_ids", "file_locks"}
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
        if key not in required:
            errors.append({
                "path": f"{path}.{key}",
                "code": "unknown-reviewer-finding-field",
                "message": f"reviewer finding has unknown field {key!r}",
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
                    "message": f"execution-log row missing field {key!r}",
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
                    "message": f"execution-log row has unknown field {key!r}",
                })
    return errors


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


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
# Subcommand stubs
# ---------------------------------------------------------------------------


def cmd_preflight(args: argparse.Namespace) -> None:
    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})

    dirty: dict[str, list[str]] = {"source_blocking": [], "infra_ignored": [], "plan_doc": []}
    status = _git(["status", "--porcelain"])
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if path == str(plan) or path.endswith(plan.name):
            dirty["plan_doc"].append(path)
        elif is_protected_path(path) or path.startswith(f"{_PLAN_DIR_POSIX}/"):
            dirty["infra_ignored"].append(path)
        else:
            dirty["source_blocking"].append(path)

    codex_available = shutil.which("codex") is not None

    sha_cp = _git(["rev-parse", "HEAD"])
    starting_sha = sha_cp.stdout.strip() or ""

    branch_cp = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    current_branch = branch_cp.stdout.strip()

    plan_text = _load_text(plan)
    base_m = re.search(r"^\*\*Base branch:\*\*\s*(\S+)\s*$", plan_text, re.MULTILINE)
    base_branch = base_m.group(1).strip() if base_m else None
    base_branch_match = base_branch is None or current_branch == base_branch

    pass_flag = len(dirty["source_blocking"]) == 0
    if args.strict_branch and not base_branch_match:
        pass_flag = False

    result = {
        "pass": pass_flag,
        "starting_sha": starting_sha,
        "run_id": _run_id(),
        "codex_available": codex_available,
        "dirty_files": dirty,
        "base_branch": base_branch,
        "current_branch": current_branch,
        "base_branch_match": base_branch_match,
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

    result = {
        "outcome": data.get("outcome"),
        "tasks": data.get("tasks") if isinstance(data.get("tasks"), list) else [],
        "batches": data.get("batches") if isinstance(data.get("batches"), list) else [],
        "gaps": data.get("gaps", []),
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

    # Defensive DAG check (ISSUE-019). `_validate_schedule` above covers shape
    # and reference integrity, but does not catch dependency cycles. Run
    # Kahn's algorithm over the full task graph so a cycle surfaces with a
    # concrete error code rather than silently deadlocking the scheduler.
    dag_deps: dict[str, list[str]] = {}
    for tid, t in tasks_by_id.items():
        deps: list[str] = []
        for dep in (t.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is not None and dep_norm in tasks_by_id:
                deps.append(dep_norm)
        dag_deps[tid] = deps
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
        _die(args, {"errors": [{
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": (
                f"dependency cycle in schedule involving tasks: {cyclic}"
            ),
        }]})

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

    # 7. DAG defensive check on the filtered subgraph (V6, ISSUE-019).
    # parse-schedule is the primary cycle detector, but filter-schedule MUST
    # also guard against a cycle surviving into its output because the
    # orchestrator pipes our stdout straight to write-schedule and any cycle
    # would then execute. Kahn's algorithm over the filtered closed set.
    out_deps: dict[str, list[str]] = {}
    for t in out_tasks:
        raw = t.get("id") if "id" in t else t.get("task_id")
        norm = _normalize_task_id(str(raw)) if raw is not None else None
        if norm is None:
            continue
        deps: list[str] = []
        for dep in (t.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is not None and dep_norm in closed:
                deps.append(dep_norm)
        out_deps[norm] = deps
    indeg: dict[str, int] = {tid: 0 for tid in out_deps}
    for tid, deps in out_deps.items():
        for d in deps:
            if d in indeg:
                indeg[tid] += 1
    queue = [tid for tid, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        head = queue.pop(0)
        visited += 1
        for other, deps in out_deps.items():
            if head in deps:
                indeg[other] -= 1
                if indeg[other] == 0:
                    queue.append(other)
    if visited != len(out_deps):
        remaining = sorted(tid for tid, n in indeg.items() if n > 0)
        _die(args, {"errors": [{
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": (
                f"dependency cycle in filtered subgraph involving tasks: "
                f"{remaining}"
            ),
        }]})

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
        _die(args, {"errors": [{
            "path": "$.subcommand",
            "code": "invalid-subcommand",
            "message": (
                f"envelope subcommand must be 'plan-review', got "
                f"{subcommand!r}"
            ),
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
            _die(args, {"error": "plan header has no **Status:** line"})
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
    try:
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
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
