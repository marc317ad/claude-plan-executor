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
