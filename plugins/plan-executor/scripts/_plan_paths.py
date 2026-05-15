"""Shared path constants, predicate, and canonicalization for the
/implement-plan executor. Imported by:

  - plugins/plan-executor/scripts/plan_codex_dispatch.py (wrapper delta-cleanup)
  - plugins/plan-executor/scripts/plan_gemini_dispatch.py (wrapper delta-cleanup)
  - plugins/plan-executor/scripts/plan_ops.py (reconcile-batch AND fail-task)

Duplicating any of these in the two consumers is forbidden -- the three
historical copies drifted and the drift caused real regressions.

Also home to the shared ``--allow-gaps`` plan-review prompt clause and
the gating predicate that decides when to inject it. Both
``plan_codex_dispatch.py`` and ``plan_gemini_dispatch.py`` import the
constant + helper from here so the two wrappers cannot diverge in either
the prose handed to the reviewer or the condition under which the prose
appears.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Optional

PROTECTED_EXACT_PATHS = frozenset({
    "_run_lock.json",
    ".claude",
    ".codex",
})
# Note: plan_claude_dispatch.py and _dispatch_cleanup.py are intentionally NOT in this prefix list — they are the wrapper layer itself, not consumers. Wrapper-self-protection is provided by the authorization-source gate in apply_cleanup, not by this allowlist.
PROTECTED_PATH_PREFIXES = (
    "docs/plans/_run_log.jsonl",
    "docs/plans/_run_lock.json",
    # Wrapper-side span audit log (PLAN_NESTED_DISPATCH TASK-006). Concurrent
    # plan_claude_dispatch invocations (e.g. the 9 parallel plan-analyst
    # classifiers in Phase 1) all append to this file at Step 12, AFTER each
    # wrapper's own apply_cleanup at Step 9. Without this entry, sibling
    # writes that land inside any single wrapper's baseline->cleanup window
    # show up as observed delta and -- for plan-analyst's empty declared
    # scope -- trip scope_violation_detected, surfacing as status=schema_invalid
    # and the orchestrator's analyst_invalid halt. Mirrors _run_log.jsonl.
    "docs/plans/spans.jsonl",
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
    "docs/plans/spans.jsonl",
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


def normalize_files_entry(raw: str) -> str:
    """Strip backticks, ``(create|modify|delete)`` annotations, prose
    continuations, and ``:line`` suffixes from a raw ``Files:`` bullet entry.

    Single canonical implementation shared by both ``plan_ops.py`` (commit
    guard / gate) and ``plan_codex_dispatch.py`` (wrapper allowlist /
    Codex prompt rendering). The two used to drift; consolidating here
    eliminates the drift surface (TASK-002).

    Semantics (in order):

    1. Strip a trailing ``(annotation)`` parenthetical, em-dash and
       en-dash tolerant inside the parens (e.g.,
       ``(create — canonical + malformed markdown fixtures)``).
       Done BEFORE any dash-split so a parenthetical containing an
       em-dash is removed as a unit.
    2. If the cleaned string starts with a backticked region (```path```),
       capture just the contents of that first backticked region. This
       prevents prose continuations like
       ```Makefile` — add `audit` target...`` from welding to the
       path.
    3. Otherwise split on space-dash-space (``\\s+[-–—]\\s+``) and take
       the head.
    4. Strip wrapping backticks; ``git show --name-only`` never emits them.
    5. Strip ``:N-M`` / ``:N–M`` ranges, then a single ``:N`` reference.
    6. Return the stripped result.
    """
    cleaned = raw.strip()
    # Strip trailing (create), (modify), (delete), (create — rationale),
    # ... BEFORE the dash-split below. A parenthetical that itself contains
    # an em-dash (`(create — canonical + malformed markdown fixtures)`)
    # must be removed as a unit, otherwise the dash-split truncates the
    # entry mid-parenthetical and the trailing-paren strip no longer sees
    # a closing `)` to anchor on.
    cleaned = re.sub(r"\s*\([^)]*(?:\([^)]*\)[^)]*)*\)\s*$", "", cleaned).strip()
    # Prefer leading backtick-quoted path so descriptive prose after
    # the path (e.g., "`foo.py` -- description") doesn't weld to the
    # path and break commit-safe.
    m_backtick = re.match(r"`([^`]+)`", cleaned)
    if m_backtick:
        cleaned = m_backtick.group(1).strip()
    else:
        cleaned = re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)[0].strip()
    # A common task Files: form is "path.py (create) — rationale".
    # The first parenthetical strip intentionally runs before dash-split
    # to preserve parentheticals that contain dashes; after splitting a
    # prose tail, strip a now-trailing create/modify/delete annotation too.
    cleaned = re.sub(r"\s*\([^)]*(?:\([^)]*\)[^)]*)*\)\s*$", "", cleaned).strip()
    cleaned = cleaned.strip()
    # Strip wrapping backticks; `git show --name-only` never emits them.
    if cleaned.startswith("`") and cleaned.endswith("`") and len(cleaned) >= 2:
        cleaned = cleaned[1:-1]
    # Strip :N-M or :N–M ranges, then a single :N reference.
    cleaned = re.sub(r":\d+[-–]\d+$", "", cleaned)
    cleaned = re.sub(r":\d+$", "", cleaned)
    return cleaned.strip()


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


# ---------------------------------------------------------------------------
# Shared plan-review prompt fragment + gating predicate
#
# Both wrappers (``plan_codex_dispatch.cmd_plan_review`` and
# ``plan_gemini_dispatch.cmd_plan_review``) inject the same demotion
# clause when ``--allow-gaps`` is set AND the persisted schedule is
# soft-gap-only. The clause text MUST be byte-identical across the two
# reviewers, so it lives here as a single module-level constant.
#
# Gating semantics (from PLAN_GEMINI_INTEGRATION_2026-04-25 / TASK-003 of
# the dual-agent run that introduced --allow-gaps):
#   - operator passed --allow-gaps, AND
#   - schedule.gaps[] is non-empty, AND
#   - schedule.outcome == "needs-enrichment" (the schedule contract
#     requires a non-empty gaps[] only with this outcome; outcome
#     "valid" mandates gaps[]==[], so the combination (valid, non-empty
#     gaps) is itself a contract violation and must NOT be demoted), AND
#   - every gap entry carries severity == "soft".
# A missing/unknown outcome suppresses the demotion. The wrapper never
# mutates the persisted schedule; the signal flows into the prompt only.
# ---------------------------------------------------------------------------

ALLOW_GAPS_DEMOTION_CLAUSE = (
    "\nOperator override (--allow-gaps): the user explicitly opted "
    "in to soft gaps. The persisted schedule's gaps[] contains only "
    "soft-severity entries and no structural violations. If "
    "schedule_ok would otherwise be false for this reason alone, "
    "demote the verdict from `needs-replan` to `approved-with-notes` "
    "and mention that demotion in the `summary`. Hard gaps or "
    "structural violations are not covered by this override.\n\n"
)


def _should_inject_allow_gaps_demotion(
    schedule_dict: Any,
    allow_gaps_flag: bool,
) -> bool:
    """Decide whether the plan-review prompt should carry the demotion clause.

    Parameters
    ----------
    schedule_dict:
        The parsed persisted schedule. Anything that is not a ``dict``
        (None, list, scalar) suppresses the demotion — the schedule
        contract requires a top-level object, and a malformed schedule
        cannot satisfy the soft-only invariant.
    allow_gaps_flag:
        ``True`` if the operator passed ``--allow-gaps`` to the wrapper.

    Returns
    -------
    bool
        ``True`` iff the gating semantics described in the module header
        are all satisfied, otherwise ``False``.
    """
    if not allow_gaps_flag:
        return False
    if not isinstance(schedule_dict, dict):
        return False
    gaps = schedule_dict.get("gaps")
    outcome = schedule_dict.get("outcome")
    if not (isinstance(gaps, list) and gaps and outcome == "needs-enrichment"):
        return False
    return all(
        isinstance(g, dict) and g.get("severity") == "soft"
        for g in gaps
    )
