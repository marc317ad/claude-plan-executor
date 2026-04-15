#!/usr/bin/env python3
"""Codex CLI dispatch wrapper for the dual-agent plan executor.

Standardizes Codex invocation with structured output, scope validation,
and error handling. Two subcommands: implement and review.

Based on the Phase 0 CLI contract (docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
Appendix D): exit code is unreliable, -s read-only is advisory only,
timeout leaves no output file, --output-schema + -o produces directly
json.load()-able output, prompts go via stdin.

Usage:
    venv/bin/python scripts/plan_codex_dispatch.py implement \
        --plan-file PATH --task-id N --repo-root PATH [--dry-run] [--timeout SECS]

    venv/bin/python scripts/plan_codex_dispatch.py review \
        --plan-file PATH --task-id N --repo-root PATH \
        --files f1,f2 [--review-focus bugs] [--dry-run] [--timeout SECS]
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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
IMPLEMENT_SCHEMA = SCRIPT_DIR / "codex_implement_schema.json"
REVIEW_SCHEMA = SCRIPT_DIR / "codex_review_schema.json"

RAW_TRUNCATE_CHARS = 2000
DEFAULT_TIMEOUT_IMPLEMENT = 300
DEFAULT_TIMEOUT_REVIEW = 180
GIT_TIMEOUT = 30
TEST_TIMEOUT = 300

def _load_plan_config() -> dict:
    """Read `.claude/plan-executor.json` from cwd; return {} if missing/invalid."""
    cfg_path = Path(".claude/plan-executor.json")
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


_PLAN_CFG = _load_plan_config()
PLAN_DIR = Path(_PLAN_CFG.get("plan_dir", "docs/plans"))
_PLAN_DIR_POSIX = PLAN_DIR.as_posix()

# Executor-infrastructure paths that wrapper cleanup must never touch.
# Root-file and directory forms are both covered: the integration test
# commits `.codex` as a 0-byte root file, while production may have a
# `.codex/` directory; same dual form for `.claude`.
PROTECTED_EXACT_PATHS = frozenset({
    "_run_lock.json",
    ".claude",
    ".codex",
})
PROTECTED_PATH_PREFIXES = (
    f"{_PLAN_DIR_POSIX}/_run_log.jsonl",
    f"{_PLAN_DIR_POSIX}/_run_lock.json",
    ".claude/",
    ".codex/",
    "scripts/plan_ops.py",
    "scripts/plan_codex_dispatch.py",
)
PROTECTED_PATH_SUFFIXES: tuple[str, ...] = ()
# Path globs (fnmatch) matched against repo-relative paths. Used for shapes
# where exact name varies per plan (e.g. schedule sidecars).
PROTECTED_PATH_GLOBS: tuple[str, ...] = (
    f"{_PLAN_DIR_POSIX}/*.schedule.json",
)

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

    return {
        "task_id": task_id,
        "title": title,
        "status": _extract_inline_field(block, "Status"),
        "priority": _extract_inline_field(block, "Priority"),
        "dependencies": _extract_inline_field(block, "Dependencies"),
        "test_command": _extract_inline_field(block, "Test command"),
        "files": _extract_bullet_list(block, "Files"),
        "acceptance_criteria": _extract_bullet_list(block, "Acceptance criteria"),
        "description": _extract_paragraph(block, "Description"),
        "implementation_notes": _extract_paragraph(block, "Implementation notes"),
        "reversion_guidance": _extract_paragraph(block, "Reversion guidance"),
    }


def normalize_file_path(raw: str) -> str:
    """Strip :line_range and (operation) annotations from a file path."""
    cleaned = raw
    # Strip trailing (create), (modify), (delete), ...
    cleaned = re.sub(r"\s*\([^)]+\)\s*$", "", cleaned)
    # Strip :N-M or :N–M ranges
    cleaned = re.sub(r":\d+[-\u2013]\d+$", "", cleaned)
    # Strip single :N reference
    cleaned = re.sub(r":\d+$", "", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------


def render_implement_prompt(task: dict, context: str) -> str:
    allowed = [normalize_file_path(f) for f in task["files"]]
    impl_notes = task.get("implementation_notes") or (
        "None provided -- follow existing patterns in the target files."
    )
    context_block = context or "(no context provided)"
    ac_bullets = "\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none specified)"
    return (
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


def render_review_prompt(task: dict, diff: str, review_focus: str) -> str:
    allowed = [normalize_file_path(f) for f in task["files"]]
    files_str = ", ".join(allowed) or "(none declared)"
    ac_bullets = "\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none specified)"
    return (
        f"Review the implementation of TASK-{task['task_id']} in this repository.\n\n"
        f"Task objective: {task['title']}\n\n"
        f"Task requirements:\n{ac_bullets}\n\n"
        f"Changed files: {files_str}\n\n"
        f"Review focus: {review_focus}\n\n"
        f"Check specifically:\n"
        f"1. Does the implementation satisfy all acceptance criteria?\n"
        f"2. Are there regressions -- changed control flow, missing error handling, broken contracts?\n"
        f"3. Does the change stay within declared scope ({files_str})?\n"
        f"4. Are there missing tests for new branches or edge cases?\n"
        f"5. Any risky assumptions around null/None/empty/default values?\n\n"
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


def git_diff_for_files(repo_root: str, files: list[str]) -> str:
    """Compute the diff for the specified files (vs HEAD, then unstaged fallback)."""
    if not files:
        return ""
    r = _git(["diff", "HEAD", "--"] + files, cwd=repo_root)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout
    r = _git(["diff", "--"] + files, cwd=repo_root)
    return r.stdout if r.returncode == 0 else ""


def _is_protected(rel_path: str) -> bool:
    """True iff the given repo-relative path is executor infrastructure.

    Git emits forward-slash relative paths; we match those literally with no
    normalization. Protection is symmetric across tracked + untracked sides.
    """
    if rel_path in PROTECTED_EXACT_PATHS:
        return True
    for prefix in PROTECTED_PATH_PREFIXES:
        if rel_path == prefix or rel_path.startswith(prefix):
            return True
    for suffix in PROTECTED_PATH_SUFFIXES:
        if rel_path.endswith(suffix):
            return True
    for pattern in PROTECTED_PATH_GLOBS:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
    return False


def _snapshot_baseline(repo_root: str) -> dict:
    """Pre-dispatch baseline of tracked + untracked diff.

    Returns {'tracked': frozenset, 'untracked': frozenset, 'captured': bool}.
    On git failure returns captured=False; downstream cleanup paths skip
    cleanup entirely in that case.
    """
    try:
        pre = git_changed_files(repo_root)
    except (subprocess.SubprocessError, OSError):
        return {
            "tracked": frozenset(),
            "untracked": frozenset(),
            "captured": False,
        }
    return {
        "tracked": frozenset(pre["tracked"]),
        "untracked": frozenset(pre["untracked"]),
        "captured": True,
    }


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
        status: 'ok' | 'timeout' | 'codex_not_found'
        exit_code: int (-1 for timeout/not_found)
        stdout: str (JSONL event stream)
        stderr: str
        file_changes: list[str] (parsed from JSONL file_change events)
        wall_seconds: float
    """
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
    stdout_text = proc.stdout.decode(errors="replace")
    stderr_text = proc.stderr.decode(errors="replace")

    # Parse JSONL for file_change events (Appendix D B2)
    file_changes: list[str] = []
    for line in stdout_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
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
    }


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


def _restore_in_scope(
    tracked: list[str],
    untracked: list[str],
    repo_root: str,
) -> None:
    """Restore/delete changes that are inside allowed_files.

    Safe because caller has already filtered inputs to the task's own scope;
    out-of-scope paths must never be passed here. Sibling work on disjoint
    files is preserved by construction.
    """
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
    allowed_set = set(allowed_files)
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
                (post_tracked | post_untracked) & allowed_set,
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

    out_of_scope_tracked_raw = new_tracked - allowed_set
    out_of_scope_untracked_raw = new_untracked - allowed_set

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
        (new_tracked | new_untracked) & allowed_set,
    )
    changed_in_scope = sorted(
        (post_tracked | post_untracked) & allowed_set,
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


def _handle_timeout_cleanup(
    repo_root: str,
    allowed_files: list[str],
    baseline: dict,
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

    allowed_set = set(allowed_files)
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
    tracked_candidates_raw = (post_tracked & allowed_set) - baseline_tracked
    delete_candidates_raw = new_untracked & allowed_set

    # Out-of-scope: observed only, never mutated
    out_of_scope_tracked_raw = new_tracked - allowed_set
    out_of_scope_untracked_raw = new_untracked - allowed_set

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
        _restore_in_scope(restore_tracked, delete_untracked, repo_root)

    return {
        "cleanup_strategy": "in_scope_only",
        "restored_tracked": restore_tracked,
        "deleted_untracked": delete_untracked,
        "out_of_scope_tracked": out_of_scope_tracked,
        "out_of_scope_untracked": out_of_scope_untracked,
        "out_of_scope_observed": out_of_scope_observed,
        "protected_skipped_tracked": protected_skipped_tracked,
        "protected_skipped_untracked": protected_skipped_untracked,
    }


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------


def run_test_command(
    test_cmd: str,
    repo_root: str,
    timeout_sec: int = TEST_TIMEOUT,
    max_attempts: int = 2,
) -> dict:
    """Run a test command with flaky-detection retry."""
    cmd = (test_cmd or "").strip()
    if not cmd or cmd.lower() == "none":
        return {
            "result": "not_run",
            "attempts": 0,
            "output_tail": "",
            "flaky": False,
            "command": cmd,
        }

    last_tail = ""
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
            return {
                "result": "failed",
                "attempts": attempt,
                "output_tail": f"TIMEOUT after {timeout_sec}s\n{tail}",
                "flaky": False,
                "command": cmd,
            }

        out = (proc.stdout + proc.stderr).decode(errors="replace")
        last_tail = out[-2000:]
        if proc.returncode == 0:
            return {
                "result": "passed",
                "attempts": attempt,
                "output_tail": last_tail,
                "flaky": attempt > 1,
                "command": cmd,
            }

    return {
        "result": "failed",
        "attempts": max_attempts,
        "output_tail": last_tail,
        "flaky": False,
        "command": cmd,
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


def emit(envelope: dict) -> None:
    print(json.dumps(envelope, indent=2, default=str))


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
    prompt = render_implement_prompt(task, context)
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

    try:
        baseline = _snapshot_baseline(repo_root)
        codex = invoke_codex(
            prompt=prompt,
            workdir=repo_root,
            schema_path=str(IMPLEMENT_SCHEMA),
            output_path=output_path,
            timeout_sec=args.timeout,
            sandbox=None,
        )

        # Timeout handling (Appendix D F1): in-scope cleanup only; anything
        # outside allowed_files is observed for orchestrator reconciliation.
        # Never run `git checkout -- .` or `git clean -fd`.
        if codex["status"] == "timeout":
            cleanup_details = _handle_timeout_cleanup(
                repo_root, allowed_files, baseline,
            )
            emit(make_envelope(
                task["task_id"], "implement", "timeout",
                exit_code=-1,
                raw=codex["stdout"] or codex["stderr"],
                error=f"Codex timed out after {args.timeout}s",
                extra={
                    "wall_seconds": codex["wall_seconds"],
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
            ))
            return 1

        if codex["status"] == "codex_not_found":
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                error="codex binary not found on PATH",
            ))
            return 1

        # Missing output file (Appendix D A2: exit code unreliable)
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=codex["exit_code"],
                raw=codex["stderr"] or codex["stdout"],
                error="Codex produced no output file",
                extra={"wall_seconds": codex["wall_seconds"]},
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
                extra={"wall_seconds": codex["wall_seconds"]},
            ))
            return 1

        # Scope validation (Appendix D F2: sandbox unreliable) — observe-only
        # against pre-dispatch baseline. Wrapper never mutates out-of-scope;
        # orchestrator reconciles at batch boundary (Fix E).
        scope = validate_scope(repo_root, allowed_files, baseline)
        if scope["out_of_scope_observed"]:
            emit(make_envelope(
                task["task_id"], "implement", "scope_violation",
                exit_code=codex["exit_code"],
                raw=output_text,
                parsed=parsed,
                error=(
                    f"Codex wrote files outside scope. "
                    f"Out-of-scope tracked: {scope['out_of_scope_tracked']}, "
                    f"untracked: {scope['out_of_scope_untracked']}. "
                    f"Orchestrator will reconcile."
                ),
                extra={
                    "scope": scope,
                    "out_of_scope_tracked": scope["out_of_scope_tracked"],
                    "out_of_scope_untracked": scope["out_of_scope_untracked"],
                    "out_of_scope_observed": True,
                    "wall_seconds": codex["wall_seconds"],
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
                },
            ))
            return 1

        # Dishonesty check: compare reported files_changed against the
        # full observed delta (in-scope plus any out-of-scope observation).
        # We include out-of-scope observations so undeclared writes outside
        # `allowed_files` are caught as a defense-in-depth layer even if the
        # scope_violation branch above is ever bypassed. Runs BEFORE the test
        # command — misreport is fatal.
        reported = {
            normalize_file_path(f) for f in parsed.get("files_changed", [])
        }
        actual_all = (
            set(scope["changed_in_scope_new"])
            | set(scope["out_of_scope_tracked"])
            | set(scope["out_of_scope_untracked"])
        )
        undeclared = sorted(actual_all - reported)
        phantom = sorted(reported - actual_all)
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
                },
            ))
            return 1

        # Independent test re-run (Appendix D-aligned)
        test_result = run_test_command(
            task["test_command"],
            repo_root,
            timeout_sec=TEST_TIMEOUT,
        )
        if test_result["result"] == "failed":
            emit(make_envelope(
                task["task_id"], "implement", "failure",
                exit_code=codex["exit_code"],
                raw=output_text,
                parsed=parsed,
                error=(
                    f"Independent test run failed after "
                    f"{test_result['attempts']} attempt(s)"
                ),
                extra={
                    "scope": scope,
                    "test_result": test_result,
                    "undeclared_changes": undeclared,
                    "phantom_declarations": phantom,
                    "jsonl_file_changes": codex["file_changes"],
                    "wall_seconds": codex["wall_seconds"],
                },
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

    diff = git_diff_for_files(repo_root, review_files)
    prompt = render_review_prompt(task, diff, args.review_focus)

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
            timeout_sec=args.timeout,
            sandbox="read-only",  # Advisory (Appendix D F2)
        )

        if codex["status"] == "timeout":
            cleanup_details = _handle_timeout_cleanup(
                repo_root, review_files, baseline,
            )
            emit(make_envelope(
                task["task_id"], "review", "timeout",
                exit_code=-1,
                raw=codex["stdout"] or codex["stderr"],
                error=f"Codex review timed out after {args.timeout}s",
                extra={
                    "wall_seconds": codex["wall_seconds"],
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
            ))
            return 1

        if codex["status"] == "codex_not_found":
            emit(make_envelope(
                task["task_id"], "review", "failure",
                error="codex binary not found on PATH",
            ))
            return 1

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            emit(make_envelope(
                task["task_id"], "review", "failure",
                exit_code=codex["exit_code"],
                raw=codex["stderr"] or codex["stdout"],
                error="Codex produced no output file",
                extra={"wall_seconds": codex["wall_seconds"]},
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
                extra={"wall_seconds": codex["wall_seconds"]},
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
# Main / argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Codex CLI dispatch wrapper for the dual-agent plan executor. "
            "Subcommands: implement, review."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def add_common(p: argparse.ArgumentParser, default_timeout: int) -> None:
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
        p.add_argument("--timeout", type=int, default=default_timeout,
                       help=f"Codex execution timeout in seconds "
                            f"(default: {default_timeout})")

    impl = subparsers.add_parser(
        "implement", help="Dispatch an implementation task to Codex",
    )
    add_common(impl, DEFAULT_TIMEOUT_IMPLEMENT)

    rev = subparsers.add_parser(
        "review", help="Dispatch a review task to Codex",
    )
    add_common(rev, DEFAULT_TIMEOUT_REVIEW)
    rev.add_argument("--files", default="",
                     help="Comma-separated list of files to review "
                          "(default: task's Files list)")
    rev.add_argument("--review-focus", default="bugs",
                     choices=["bugs", "regressions", "security", "tests"],
                     help="Review focus area (default: bugs)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.subcommand == "implement":
        return cmd_implement(args)
    if args.subcommand == "review":
        return cmd_review(args)
    parser.error(f"Unknown subcommand: {args.subcommand}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
