"""Shared path constants, predicate, and canonicalization for the
/implement-plan executor. Imported by:

  - plugins/plan-executor/scripts/plan_codex_dispatch.py (wrapper delta-cleanup)
  - plugins/plan-executor/scripts/plan_ops.py (reconcile-batch AND fail-task)

Duplicating any of these in the two consumers is forbidden -- the three
historical copies drifted and the drift caused real regressions.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Optional

PROTECTED_EXACT_PATHS = frozenset({
    "_run_lock.json",
    ".claude",
    ".codex",
})
PROTECTED_PATH_PREFIXES = (
    "docs/plans/_run_log.jsonl",
    "docs/plans/_run_lock.json",
    ".claude/",
    ".codex/",
    "plugins/plan-executor/scripts/plan_ops.py",
    "plugins/plan-executor/scripts/plan_codex_dispatch.py",
)
PROTECTED_PATH_SUFFIXES: tuple[str, ...] = ()
PROTECTED_PATH_GLOBS: tuple[str, ...] = (
    "docs/plans/*.schedule.json",
)

# Paths that commit-task itself writes during orchestration. These are
# allowed in commit-safe without being declared in a task's Files: list,
# because the orchestrator -- not the implementer -- authors them. The
# set is intentionally narrower than `PROTECTED_PATH_PREFIXES`: it covers
# only run-log / run-lock bookkeeping, the per-plan schedule sidecar
# (matched dynamically via `is_commit_always_ignore`), and the
# `00_INDEX.json` roster sibling that `commit-task` auto-updates next to
# the plan file. Executor scripts such as `plan_ops.py` are protected
# from delta-cleanup but must still be declared in Files: to commit
# against -- do NOT add them here.
#
# The static entries below cover the default plan layouts shipped in
# this repo (`docs/plans/` and `docs/plans/DUAL_AGENT_Plans/`). Callers
# that need to accept an `00_INDEX.json` next to a plan outside those
# layouts (tests and alternative bundle shapes) should pass `plan_dir`
# to `is_commit_always_ignore` so the match extends to
# `<plan_dir>/00_INDEX.json` at runtime.
COMMIT_ALWAYS_IGNORE: frozenset[str] = frozenset({
    "docs/plans/_run_log.jsonl",
    "docs/plans/_run_lock.json",
    "docs/plans/00_INDEX.json",
    "docs/plans/DUAL_AGENT_Plans/00_INDEX.json",
})


def is_commit_always_ignore(
    path: str,
    plan_basename: str | None = None,
    plan_dir: str | None = None,
) -> bool:
    """True if `path` is a commit-task-authored bookkeeping path.

    `plan_basename` (e.g. ``"TASK-005_phase_gates.md"``) lets us match
    the per-plan schedule sidecar ``<plan_dir>/<stem>.schedule.json``
    without hardcoding every plan name. The match is stem-based so
    callers that pass either the bare basename or the full file name
    land on the same sidecar key.

    `plan_dir` (repo-relative directory of the plan file, forward-slash
    form) extends the match to ``<plan_dir>/00_INDEX.json`` for plans
    that live outside the default `docs/plans/` bundle. When `plan_dir`
    is None the match is restricted to the static members of
    ``COMMIT_ALWAYS_IGNORE`` and the default-layout schedule sidecars.
    """
    if path in COMMIT_ALWAYS_IGNORE:
        return True
    if plan_basename:
        stem = Path(plan_basename).stem or plan_basename
        schedule_leaf = f"{stem}.schedule.json"
        # Default-layout sidecars anywhere under docs/plans/.
        if path.startswith("docs/plans/") and (
            path == f"docs/plans/{schedule_leaf}"
            or path.endswith(f"/{schedule_leaf}")
        ):
            return True
        # Caller-supplied plan_dir: match the sidecar that
        # `commit-task` writes alongside the plan file.
        if plan_dir is not None:
            pd = plan_dir.rstrip("/")
            if pd == "":
                if path == schedule_leaf:
                    return True
            elif path == f"{pd}/{schedule_leaf}":
                return True
    if plan_dir is not None:
        pd = plan_dir.rstrip("/")
        # An empty `plan_dir` (plan at repo root) matches the bare
        # `00_INDEX.json`; a nested plan_dir matches the sibling roster.
        if pd == "":
            if path == "00_INDEX.json":
                return True
        elif path == f"{pd}/00_INDEX.json":
            return True
    return False


def is_protected_path(rel_path: str) -> bool:
    """Return True if rel_path (repo-relative, forward-slash, canonicalized)
    must not be touched by fail-task, wrapper cleanup, or reconcile."""
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


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")


def _looks_like_windows_drive_absolute(raw: str) -> bool:
    return bool(_WINDOWS_DRIVE_RE.match(raw))


def canonicalize_file(raw: str, repo_root: Path) -> Optional[str]:
    """Normalize raw --files entry to a repo-relative forward-slash path.
    Returns None if the entry escapes repo_root (absolute outside, .. escape,
    Windows-drive absolute on POSIX, or symlink escape after resolve()).

    Rules:
    - Windows-drive absolute paths (regex ``^[A-Za-z]:[/\\\\]``) are REJECTED
      on POSIX hosts. We do NOT silently translate them to relative paths;
      that would misclassify ``C:\\repo\\file`` (an out-of-repo absolute
      Windows path) as an in-repo relative path once backslashes are
      normalized. WSL callers must pass the POSIX equivalent
      (``/mnt/c/repo/file``), which then resolves normally.
    - Windows-drive absolute paths on Windows hosts fall through to
      ``Path().is_absolute()`` naturally and behave like other absolute
      paths.
    - Plain backslashes inside a filename are ambiguous. We normalize
      backslashes to forward-slashes on BOTH hosts because ``fnmatch`` /
      prefix matches below use forward-slashes and legitimate repo paths
      never contain literal backslashes. Callers needing to target a file
      whose name contains a literal backslash on POSIX are unsupported by
      this subcommand.
    """
    if not isinstance(raw, str):
        return None
    if _looks_like_windows_drive_absolute(raw) and os.name != "nt":
        return None
    s = raw.replace("\\", "/")
    # Strip a leading "./" to keep canonicalization robust against callers
    # that prefix relative paths; Path() handles these too, but being
    # explicit keeps the intent obvious.
    while s.startswith("./"):
        s = s[2:]
    if not s:
        # Empty or pure "./" entry: treat as repo_root itself, which is a
        # directory, not a file. Return the empty-string relative path so
        # downstream classification sees it as the root and marks it
        # ``directory``.
        return ""
    candidate = Path(s)
    try:
        if candidate.is_absolute():
            abs_ = candidate.resolve()
        else:
            abs_ = (repo_root / candidate).resolve()
    except OSError:
        # Resolve() can fail on pathological inputs (e.g. symlink loops).
        # Treat as out-of-repo rather than crash.
        return None
    try:
        rel = abs_.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return rel.as_posix()
