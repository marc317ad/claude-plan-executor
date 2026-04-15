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
    venv/bin/python scripts/plan_ops.py commit-task --plan-file <abs> --task-id NNN ...
    venv/bin/python scripts/plan_ops.py fail-task --plan-file <abs> --task-id NNN ...
    venv/bin/python scripts/plan_ops.py block-dependents --schedule-file <path> --failed NNN --run-id RID
    venv/bin/python scripts/plan_ops.py update-plan-header --plan-file <abs> --status <s>
    venv/bin/python scripts/plan_ops.py finalize-execution-log --plan-file <abs> ...
    venv/bin/python scripts/plan_ops.py log-event --event E --fields-json '{...}'
    venv/bin/python scripts/plan_ops.py normalize-task-id --id <1|001|TASK-001>
    venv/bin/python scripts/plan_ops.py acquire-lock --plan-file <abs> --run-id RID
    venv/bin/python scripts/plan_ops.py release-lock --plan-file <abs> --run-id RID
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

def _load_plan_config() -> dict:
    """Read `.claude/plan-executor.json` from cwd; return {} if missing/invalid.

    Config schema (all optional):
        { "plan_dir": "docs/plans" }

    When absent, defaults preserve legacy behavior so projects that never
    adopt the config file keep working unchanged.
    """
    cfg_path = Path(".claude/plan-executor.json")
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


_PLAN_CFG = _load_plan_config()
PLAN_DIR = Path(_PLAN_CFG.get("plan_dir", "docs/plans"))
RUN_LOG_PATH = PLAN_DIR / "_run_log.jsonl"
RUN_LOCK_PATH = PLAN_DIR / "_run_lock.json"

TASK_HEADER_RE = re.compile(r"^### TASK-(\d{3}[A-Z]?):", re.MULTILINE)
TASK_ID_INPUT_RE = re.compile(r"^(?:TASK-)?(\d{1,3})([A-Z]?)$")
STATUS_BULLET_RE = re.compile(r"^(\s*-\s*\*\*Status:\*\*)\s*(.+?)\s*$", re.MULTILINE)
DEPENDENCIES_BULLET_RE = re.compile(
    r"^\s*-\s*\*\*Dependencies:\*\*\s*(.+?)\s*$", re.MULTILINE,
)
INDEX_ROSTER_HEADER_RE = re.compile(r"^\|\s*#\s*\|\s*File\s*\|", re.MULTILINE)
ALLOWED_TASK_STATUSES = {"pending", "open", "in-progress", "done", "failed", "blocked", "skipped"}
STATUS_ALIASES = {"open": "pending"}
SCHEDULE_FIELD_ALIASES = {"task_id": "id", "batch_index": "index"}
ALLOWED_PLAN_STATUSES = {"in-progress", "complete", "partial"}
ALLOWED_FAIL_STAGES = {"implement", "review", "commit"}
ALLOWED_CODEX_REVIEW_VERDICTS = {"clean", "minor-findings", "needs-rework"}
ALLOWED_CLAUDE_REVIEW_VERDICTS = {"ship", "ship-with-fixes", "needs-rework"}
ALLOWED_ROW_FIELDS = {"task", "agent", "reviewer", "verdict", "commit", "notes"}

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


def _topo_sort(tasks: list[dict]) -> tuple[list[str], dict[str, list[str]], dict[str, int], list[str]]:
    indeg: dict[str, int] = {}
    adj: dict[str, list[str]] = {}
    task_meta: dict[str, dict] = {}
    for task in tasks:
        task_id = task["id"]
        indeg[task_id] = 0
        adj[task_id] = []
        task_meta[task_id] = task

    for task in tasks:
        task_id = task["id"]
        for dep in task["dependencies"]:
            adj[dep].append(task_id)
            indeg[task_id] += 1

    ready = sorted(
        [task_id for task_id, deg in indeg.items() if deg == 0],
        key=lambda task_id: _task_order_key(task_id, task_meta[task_id]["priority"]),
    )
    topo: list[str] = []
    while ready:
        current = ready.pop(0)
        topo.append(current)
        for nxt in adj[current]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda task_id: _task_order_key(task_id, task_meta[task_id]["priority"]))

    residual = sorted(task_id for task_id, deg in indeg.items() if deg > 0)
    return topo, adj, indeg, residual


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

        raw_deps = task.get("dependencies") or []
        if not isinstance(raw_deps, list):
            errors.append({
                "path": f"$.tasks[{i}].dependencies",
                "code": "invalid-type",
                "message": "dependencies must be an array",
            })
            continue

        deps: list[str] = []
        dep_invalid = False
        for j, dep in enumerate(raw_deps):
            dep_id = _normalize_task_id(dep)
            if dep_id is None:
                errors.append({
                    "path": f"$.tasks[{i}].dependencies[{j}]",
                    "code": "invalid-task-id",
                    "message": f"dependencies[{j}]={dep!r} is not a valid task id",
                })
                dep_invalid = True
                continue
            deps.append(dep_id)
        if dep_invalid:
            continue

        priority = str(task.get("priority", "low")).strip().lower() or "low"
        if priority not in PRIORITY_RANKS:
            priority = "low"

        normalized_tasks.append({
            "id": task_id,
            "priority": priority,
            "files": [str(path) for path in raw_files],
            "dependencies": deps,
        })

    if errors:
        return [], [], errors

    known_ids = {task["id"] for task in normalized_tasks}
    for task in normalized_tasks:
        for dep in task["dependencies"]:
            if dep not in known_ids:
                errors.append({
                    "path": f"$.tasks[{seen_ids[task['id']]}].dependencies",
                    "code": "missing-dependency",
                    "message": f"dependency {dep!r} is not a known task id",
                })
    if errors:
        return [], [], errors

    topo, _, _, residual = _topo_sort(normalized_tasks)
    if residual:
        return [], [], [{
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": f"dependency cycle among tasks: {residual}",
            "cycle_nodes": residual,
        }]

    task_map = {task["id"]: task for task in normalized_tasks}
    levels: dict[str, int] = {}
    for task_id in topo:
        deps = task_map[task_id]["dependencies"]
        levels[task_id] = 1 if not deps else 1 + max(levels[dep] for dep in deps)

    batches: list[dict] = []
    next_batch_index = 1
    max_level = max(levels.values(), default=0)
    for level in range(1, max_level + 1):
        level_task_ids = [task_id for task_id in topo if levels[task_id] == level]
        open_batches: list[dict] = []
        for task_id in level_task_ids:
            task_files = set(task_map[task_id]["files"])
            placed = False
            for batch in open_batches:
                if batch["_files"] & task_files:
                    continue
                batch["task_ids"].append(task_id)
                batch["_files"].update(task_files)
                placed = True
                break
            if not placed:
                open_batches.append({
                    "index": next_batch_index,
                    "task_ids": [task_id],
                    "_files": set(task_files),
                })
                next_batch_index += 1
        for batch in open_batches:
            batches.append({
                "index": batch["index"],
                "task_ids": batch["task_ids"],
                "file_locks": sorted(batch["_files"]),
            })

    return topo, batches, []


def _validate_schedule_dag(tasks: list, batches: list) -> list[dict]:
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
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        deps = t.get("dependencies") or []
        if not isinstance(deps, list):
            continue
        for j, d in enumerate(deps):
            if str(d) not in known_ids:
                errors.append({
                    "path": f"$.tasks[{i}].dependencies[{j}]",
                    "code": "unknown-dependency",
                    "message": f"dependency {str(d)!r} is not a known task id",
                })

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

    indeg: dict[str, int] = {tid: 0 for tid in known_ids}
    adj: dict[str, list[str]] = {tid: [] for tid in known_ids}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = str(raw)
        if tid not in indeg:
            continue
        for d in t.get("dependencies") or []:
            ds = str(d)
            if ds in indeg:
                adj[ds].append(tid)
                indeg[tid] += 1
    queue = [tid for tid, deg in indeg.items() if deg == 0]
    visited = 0
    head = 0
    while head < len(queue):
        cur = queue[head]
        head += 1
        visited += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if visited < len(known_ids):
        residual = sorted(tid for tid, deg in indeg.items() if deg > 0)
        errors.append({
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": f"dependency cycle among tasks: {residual}",
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
            for key in ("agent", "files", "dependencies"):
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
        errors.extend(_validate_schedule_dag(tasks, batches))

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
    """Parse the chunk-roster table in a DUAL_AGENT_Plans 00_INDEX.md.

    Locates the header row `| # | File | ... | Depends on chunks |`, walks the
    subsequent `|`-delimited rows until it hits a non-table line, and returns
    `{task_id: {"file": <filename>, "depends_on": [task_id,...]}}`. Keys are
    canonical 3-digit ids produced by `_normalize_task_id`.

    Raises FileNotFoundError if `path` does not exist and ValueError if the
    roster table cannot be located or lacks the columns we need.
    """
    if not path.is_file():
        raise FileNotFoundError(f"index file not found: {path}")
    text = path.read_text(encoding="utf-8")
    header = INDEX_ROSTER_HEADER_RE.search(text)
    if not header:
        raise ValueError(f"no chunk-roster table in {path}")

    header_end = text.find("\n", header.start())
    if header_end == -1:
        raise ValueError(f"malformed roster header in {path}")
    header_line = text[header.start() : header_end]
    columns = [c.strip() for c in header_line.strip().strip("|").split("|")]

    def _col_index(candidates: tuple[str, ...]) -> int | None:
        for name in candidates:
            if name in columns:
                return columns.index(name)
        return None

    file_idx = _col_index(("File",))
    task_idx = _col_index(("v3 Task", "Task", "Task ID"))
    if file_idx is None or task_idx is None:
        raise ValueError(f"roster missing File / Task columns in {path}")
    depends_idx = _col_index(("Depends on chunks", "Depends on", "Dependencies"))

    roster: dict[str, dict] = {}
    saw_separator = False
    for line in text[header_end + 1 :].splitlines():
        stripped = line.strip()
        if not stripped:
            if saw_separator:
                break
            continue
        if not stripped.startswith("|"):
            if saw_separator:
                break
            continue
        if stripped.startswith("|---") or stripped.startswith("| ---"):
            saw_separator = True
            continue
        if not saw_separator:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        max_idx = max(file_idx, task_idx, depends_idx if depends_idx is not None else 0)
        if len(cells) <= max_idx:
            continue
        task_id = _normalize_task_id(cells[task_idx])
        if not task_id:
            continue
        file_raw = cells[file_idx]
        link = re.match(r"\[\s*`?([^`\]]+?)`?\s*\]\(([^)]+)\)", file_raw)
        if link:
            filename = link.group(2).strip()
        else:
            filename = file_raw.strip().strip("`")
        deps: list[str] = []
        if depends_idx is not None and len(cells) > depends_idx:
            deps_raw = cells[depends_idx]
            if deps_raw and deps_raw not in {"—", "–", "-", "none", "None"}:
                for part in deps_raw.split(","):
                    d = _normalize_task_id(part.strip())
                    if d:
                        deps.append(d)
        roster[task_id] = {"file": filename, "depends_on": deps}
    return roster


def _read_plan_task_status(plan_path: Path, task_id: str) -> str | None:
    """Return the `- **Status:**` bullet value for `### TASK-<id>:` in a plan file.

    Returns `None` when the plan file, the task heading, or the status bullet is
    absent.
    """
    if not plan_path.is_file():
        return None
    text = plan_path.read_text(encoding="utf-8")
    _, blocks = _split_task_blocks(text)
    for tid, body in blocks:
        if tid == task_id:
            m = _find_status_bullet(body)
            if m:
                return m.group(2).strip()
            return None
    return None


def _collect_plan_dependencies(plan_text: str) -> tuple[set[str], set[str]]:
    """Return `(in_plan_ids, requested_deps)` for a plan body.

    `in_plan_ids` is the set of canonical task ids declared in the plan;
    `requested_deps` is the normalized union of every task-block
    `- **Dependencies:**` bullet value.
    """
    in_plan: set[str] = set()
    requested: set[str] = set()
    _, blocks = _split_task_blocks(plan_text)
    for tid, body in blocks:
        in_plan.add(tid)
        for m in DEPENDENCIES_BULLET_RE.finditer(body):
            raw = m.group(1).strip()
            if raw.lower() in {"none", "—", "–", "-"}:
                continue
            raw = raw.strip("[]")
            for part in raw.split(","):
                normalized = _normalize_task_id(part.strip())
                if normalized:
                    requested.add(normalized)
    return in_plan, requested


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
# Mirrored from scripts/plan_codex_dispatch.py. Defensive even if a wrapper
# envelope somehow reports a protected path as out-of-scope: reconciliation
# never restores/unlinks executor infrastructure.
RECONCILE_PROTECTED_EXACT = frozenset({"_run_lock.json", ".claude", ".codex"})
_PLAN_DIR_POSIX = PLAN_DIR.as_posix()
RECONCILE_PROTECTED_PREFIXES = (
    f"{_PLAN_DIR_POSIX}/_run_log.jsonl",
    f"{_PLAN_DIR_POSIX}/_run_lock.json",
    ".claude/",
    ".codex/",
    "scripts/plan_ops.py",
    "scripts/plan_codex_dispatch.py",
)
RECONCILE_PROTECTED_GLOBS = (f"{_PLAN_DIR_POSIX}/*.schedule.json",)


def _is_reconcile_protected(rel_path: str) -> bool:
    if rel_path in RECONCILE_PROTECTED_EXACT:
        return True
    for prefix in RECONCILE_PROTECTED_PREFIXES:
        if rel_path == prefix or rel_path.startswith(prefix):
            return True
    for pattern in RECONCILE_PROTECTED_GLOBS:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


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
            if _is_reconcile_protected(p):
                skipped.add(p)
            else:
                actionable_tracked.append(p)
        for p in untracked:
            if _is_reconcile_protected(p):
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


def cmd_check_plan_deps(args: argparse.Namespace) -> None:
    plan_path = Path(args.plan_file)
    plans_dir = Path(args.plans_dir)

    if not plan_path.is_file():
        _die(args, {"errors": [{
            "path": f"$.<file:{plan_path}>",
            "code": "file-not-found",
            "message": f"plan file not found: {plan_path}",
        }]})

    index_path = plans_dir / "00_INDEX.md"
    try:
        roster = _parse_index_roster(index_path)
    except FileNotFoundError as e:
        _die(args, {"errors": [{
            "path": f"$.<file:{index_path}>",
            "code": "index-not-found",
            "message": str(e),
        }]})
    except ValueError as e:
        _die(args, {"errors": [{
            "path": f"$.<file:{index_path}>",
            "code": "index-not-found",
            "message": str(e),
        }]})

    plan_text = plan_path.read_text(encoding="utf-8")
    in_plan_ids, requested = _collect_plan_dependencies(plan_text)
    cross_plan = sorted(requested - in_plan_ids)

    resolved: list[dict] = []
    unresolved: list[dict] = []
    for dep_id in cross_plan:
        entry = roster.get(dep_id)
        if entry is None:
            unresolved.append({
                "task_id": dep_id,
                "reason": "unresolved-dep",
                "detail": f"task {dep_id} is not declared in 00_INDEX.md roster",
            })
            continue
        plan_file = entry["file"]
        sibling_path = plans_dir / plan_file
        if not sibling_path.is_file():
            unresolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "reason": "file-not-found",
                "detail": f"roster points to missing file: {sibling_path}",
            })
            continue
        status = _read_plan_task_status(sibling_path, dep_id)
        if status == "done":
            resolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "status": "done",
            })
        else:
            unresolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "reason": "dep-not-done",
                "status": status or "unknown",
                "detail": f"sibling status is {status!r}, expected 'done'",
            })

    _emit(args, {
        "pass": len(unresolved) == 0,
        "deps": resolved,
        "unresolved": unresolved,
        "errors": [],
    }, exit_code=0)


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
        elif path.startswith((".claude/", "docs/", "tests/")):
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

    def _ready(task: dict) -> bool:
        deps = [_normalize_task_id(str(d)) for d in (task.get("dependencies") or [])]
        if any(d in failed for d in deps):
            return False
        return all(d in done for d in deps if d)

    def _files(task: dict) -> list[str]:
        return list(task.get("files") or [])

    remaining = [t for tid, t in tasks_by_id.items() if tid not in done and tid not in failed]
    ready = [t for t in remaining if _ready(t)]

    picked: list[str] = []
    picked_files: list[str] = []
    claimed = set(locked)
    batch_index: int | None = None

    for b in data.get("batches") or []:
        bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
        if any(tid in done or tid in failed for tid in bids):
            continue
        if all(tid in done for tid in bids if tid):
            continue
        batch_index = b.get("index") if "index" in b else b.get("batch_index")
        break
    if batch_index is None:
        batch_index = 0

    for t in ready:
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

    scheduler_stuck = len(picked) == 0 and len(ready) > 0

    _emit(args, {
        "batch_index": batch_index,
        "task_ids": picked,
        "file_locks": picked_files,
        "scheduler_stuck": scheduler_stuck,
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

    commit_msg = (
        f"feat(TASK-{tid}): {args.title}\n\n"
        f"{args.diff_summary}\n\n"
        f"Plan: {plan.name}\n"
    )

    add_files = files + [str(plan)]
    add = _git(["add", "--", *add_files])
    if add.returncode != 0:
        _write_text(plan, original_plan)
        _die(args, {"error": f"git add failed: {add.stderr.strip()}"})

    commit = _git(["commit", "-m", commit_msg, "--only", "--", *add_files])
    if commit.returncode != 0:
        _git(["reset", "HEAD", "--", *add_files])
        _write_text(plan, original_plan)
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
    }
    _append_run_log("commit_done", event_fields)

    _emit(args, {
        "commit_sha": commit_sha,
        "status_updated": True,
        "log_appended": True,
    })


def cmd_fail_task(args: argparse.Namespace) -> None:
    tid = _normalize_task_id(args.task_id)
    if not tid:
        _die(args, {"error": f"bad --task-id: {args.task_id!r}"})

    files = [f.strip() for f in (args.files or "").split(",") if f.strip()]
    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})

    restore_ok = True
    if files:
        restore = _git(["restore", "--", *files])
        if restore.returncode != 0:
            restore_ok = False

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
    })


def cmd_block_dependents(args: argparse.Namespace) -> None:
    sched_path = Path(args.schedule_file)
    if not sched_path.is_file():
        _die(args, {"error": f"schedule file not found: {sched_path}"})
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(args, {"error": f"schedule json decode: {e}"})

    failed_id = _normalize_task_id(args.failed)
    if not failed_id:
        _die(args, {"error": f"cannot normalize --failed: {args.failed!r}"})

    blocked: list[str] = []
    queue = [failed_id]
    seen = set(queue)
    tasks = data.get("tasks") or []
    while queue:
        cur = queue.pop(0)
        for t in tasks:
            raw_tid = t.get("id") if "id" in t else t.get("task_id")
            tid = _normalize_task_id(str(raw_tid))
            if not tid or tid in seen:
                continue
            deps = [_normalize_task_id(str(d)) for d in (t.get("dependencies") or [])]
            if cur in deps:
                blocked.append(tid)
                seen.add(tid)
                queue.append(tid)

    for bid in blocked:
        _append_run_log("blocked", {
            "run_id": args.run_id,
            "task_id": bid,
            "blocker_task_id": failed_id,
            "reason": f"dependency TASK-{failed_id} failed",
        })

    _emit(args, {"blocked_task_ids": blocked})


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
    lines = [
        "",
        f"## Execution log — {args.run_id}",
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


def cmd_acquire_lock(args: argparse.Namespace) -> None:
    plan_abs = os.path.abspath(args.plan_file)
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    current: dict = {}
    if RUN_LOCK_PATH.exists():
        try:
            current = json.loads(RUN_LOCK_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            current = {}
    if plan_abs in current and current[plan_abs].get("run_id") != args.run_id:
        _die(args, {
            "acquired": False,
            "conflict_run_id": current[plan_abs].get("run_id"),
        })
    current[plan_abs] = {"run_id": args.run_id, "acquired_at": _now()}
    RUN_LOCK_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
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

    p_comp = sub.add_parser("compute-schedule", help="Compute topo order + disjoint batches")
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

    p_rep = sub.add_parser("parse-implementer-report",
                           help="Parse plan-implementer markdown report")
    p_rep.add_argument("--stdin", action="store_true", required=True,
                       help="Read report markdown from stdin")
    _add_json(p_rep)

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
    p_commit.add_argument("--disagreement-tag", action="store_true",
                          help="Mark commit as §8.4 disagreement")
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
    p_fail.add_argument("--dry-run", action="store_true")
    _add_json(p_fail)

    p_block = sub.add_parser("block-dependents", help="Cascade-block downstream tasks")
    p_block.add_argument("--schedule-file", required=True)
    p_block.add_argument("--failed", required=True, help="Failed task id")
    p_block.add_argument("--run-id", required=True)
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
        help="Resolve cross-plan dependencies via 00_INDEX.md + sibling statuses",
    )
    p_cpd.add_argument("--plan-file", required=True, help="Absolute path to plan file")
    p_cpd.add_argument(
        "--plans-dir", required=True,
        help="Directory containing 00_INDEX.md and sibling plan files",
    )
    _add_json(p_cpd)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "preflight": cmd_preflight,
        "parse-schedule": cmd_parse_schedule,
        "compute-schedule": cmd_compute_schedule,
        "write-schedule": cmd_write_schedule,
        "batch-next": cmd_batch_next,
        "parse-implementer-report": cmd_parse_implementer_report,
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
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
