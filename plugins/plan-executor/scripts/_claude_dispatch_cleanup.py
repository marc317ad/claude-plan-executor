"""Delta-bounded cleanup for the nested ``claude`` dispatch wrapper
(PLAN_NESTED_DISPATCH §8.1, TASK-004).

Mirrors the proven baseline-snapshot + delta-cleanup pattern in
``plan_codex_dispatch.py`` while exposing a *flatter* API focused on the
two flags the Claude envelope reports:

  * ``scope_violation_detected`` — the nested session wrote outside its
    declared ``files_changed`` (after subtracting protected paths).
  * ``scope_misreport_detected`` — the nested session declared a path in
    ``files_changed`` that was not actually touched relative to the
    pre-dispatch baseline.

Public API
----------

  * :func:`snapshot_baseline(repo_root) -> dict`
        Capture pre-dispatch tracked + untracked state via git plumbing.
        Returns a *process-local*, in-memory dict the wrapper stashes
        for the duration of the nested session. State lives only in the
        returned object; nothing is written to ``~/.claude`` or any
        other shared location, so concurrent dispatches on disjoint
        file sets cannot cross-contaminate.

  * :func:`apply_cleanup(baseline, declared_files_changed, repo_root) -> dict`
        Compute the post-dispatch delta vs ``baseline``. Restore /
        delete every path in
        ``(observed_delta − declared_files_changed − protected_paths)``.
        Returns the result dict shape documented under
        :func:`apply_cleanup`.

Factoring note
--------------

The TASK-004 acceptance criteria asks: if sharing logic with
``plan_codex_dispatch.py`` is a ≤100-line extraction, do it now;
otherwise duplicate. The two micro-helpers ``_git`` (~10 lines) and
``git_changed_files`` (~30 lines) are textually shared, but the *result
schemas* of the two wrappers diverge sharply:

  * Codex's ``validate_scope`` / ``_handle_timeout_cleanup`` distinguish
    ``out_of_scope_tracked`` / ``out_of_scope_untracked`` /
    ``protected_skipped_*`` / ``cleanup_strategy`` taxonomies and never
    *mutate* outside-scope files (observe-only at the wrapper layer);
    the orchestrator reconciles at the batch join barrier.
  * The Claude wrapper here actively *restores / deletes* the
    out-of-declaration delta inside the wrapper, returning the flatter
    ``scope_violation_detected`` / ``restored`` / ``deleted`` shape.

The semantic mismatch means a shared cleanup helper would have to fork
its return type by caller — defeating the point. The micro-helpers are
duplicated here (≤45 lines) and a follow-up task is filed to extract
them into ``_plan_paths.py`` once a third caller appears.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

# Ensure the sibling ``_plan_paths`` module is importable when this file
# is loaded via ``importlib.util.spec_from_file_location`` (e.g., from
# tests). Direct CLI invocation already adds SCRIPT_DIR to ``sys.path``
# automatically. Mirrors the same shim in ``plan_codex_dispatch.py``.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _plan_paths import is_protected_path  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Subprocess timeout for any single ``git`` plumbing call. Mirrors
#: :data:`plan_codex_dispatch.GIT_TIMEOUT`.
GIT_TIMEOUT = 30

#: Maximum byte size of a single file we will snapshot in-memory for
#: restore. Files above this threshold are recorded by their content
#: hash only; if such a file shows up under cleanup we delete it (if
#: untracked) or fall back to ``git restore --source=HEAD`` (if tracked
#: and clean at HEAD), but we do NOT attempt a full byte-restore. This
#: keeps a single dispatch's baseline bounded even in repos with large
#: blobs.
MAX_BASELINE_BLOB_BYTES = 10 * 1024 * 1024  # 10 MiB

# ---------------------------------------------------------------------------
# Tiny git plumbing helpers (duplicated from plan_codex_dispatch — see the
# module docstring "Factoring note").
# ---------------------------------------------------------------------------


def _git(
    args: List[str],
    cwd: str,
    timeout: int = GIT_TIMEOUT,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run ``git`` with text-mode capture. ``check=False`` by default
    because callers handle non-zero rc explicitly (corrupted repo,
    detached HEAD edge cases, etc.).
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def _git_bytes(
    args: List[str],
    cwd: str,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Same as :func:`_git` but returns bytes (for ``cat-file`` / blob
    fetches that may contain non-UTF-8 content).
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        timeout=timeout,
    )


def _changed_paths(repo_root: str) -> Dict[str, List[str]]:
    """Return ``{'tracked': [...], 'untracked': [...]}`` of paths whose
    working-tree content currently differs from HEAD (tracked side) or
    which are untracked-but-not-ignored.

    ``tracked`` includes both staged and unstaged modifications. Sorted
    for deterministic test assertions.
    """
    tracked: Set[str] = set()
    r = _git(["diff", "--name-only", "HEAD"], cwd=repo_root)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                tracked.add(ln)
    r = _git(["diff", "--name-only", "--cached"], cwd=repo_root)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                tracked.add(ln)
    untracked: Set[str] = set()
    r = _git(["ls-files", "--others", "--exclude-standard"], cwd=repo_root)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                untracked.add(ln)
    return {"tracked": sorted(tracked), "untracked": sorted(untracked)}


def _all_tracked_paths(repo_root: str) -> Set[str]:
    """Return the set of every path tracked by HEAD, regardless of
    whether it is currently dirty. Used by the baseline snapshot so
    cleanup can distinguish "tracked-and-clean-at-baseline" (restore
    via ``git restore --source=HEAD``) from "newly-created out of
    scope" (delete).
    """
    out: Set[str] = set()
    r = _git(["ls-files"], cwd=repo_root)
    if r.returncode == 0:
        for ln in r.stdout.splitlines():
            ln = ln.strip()
            if ln:
                out.add(ln)
    return out


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _read_bytes_safe(repo_root: str, rel: str) -> Optional[bytes]:
    """Read working-tree bytes for ``rel`` if the file exists and is not
    above :data:`MAX_BASELINE_BLOB_BYTES`. Returns ``None`` on any
    error (missing, oversized, IO failure) — caller must treat ``None``
    as "no byte-level restore available for this path".
    """
    full = Path(repo_root) / rel
    try:
        if not full.is_file():
            return None
        try:
            size = full.stat().st_size
        except OSError:
            return None
        if size > MAX_BASELINE_BLOB_BYTES:
            return None
        return full.read_bytes()
    except (OSError, PermissionError):
        return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_baseline(repo_root: str) -> dict:
    """Capture pre-dispatch tracked + untracked state via git plumbing.

    The returned dict is *process-local* and *in-memory*; callers stash
    it for the duration of the nested session and pass it back to
    :func:`apply_cleanup`. Nothing is persisted to disk or to any
    shared location, so two wrapper processes operating on disjoint
    file sets cannot cross-contaminate baselines.

    Shape (stable for the duration of TASK-004; unspecified beyond
    what :func:`apply_cleanup` consumes)::

        {
            "captured":              bool,
            "repo_root":             str (resolved absolute path),
            "tracked_at_head":       set[str] of every path tracked
                                     by HEAD at snapshot time
                                     (regardless of whether dirty);
                                     used by cleanup to distinguish
                                     "tracked-and-clean — restore via
                                     git restore --source=HEAD" from
                                     "newly created out-of-scope —
                                     delete",
            "tracked_changed":       sorted list[str] of repo-relative
                                     paths whose working-tree content
                                     differs from HEAD at snapshot time
                                     (staged + unstaged unioned),
            "untracked":             sorted list[str] of repo-relative
                                     paths present in the working tree
                                     but not tracked / not ignored at
                                     snapshot time,
            "tracked_blobs":         { rel: bytes }   for each
                                     tracked_changed path that fits
                                     under MAX_BASELINE_BLOB_BYTES,
            "untracked_blobs":       { rel: bytes }   for each
                                     untracked path that fits under
                                     MAX_BASELINE_BLOB_BYTES,
            "tracked_hashes":        { rel: sha256 }  parallel to
                                     tracked_blobs (kept even when the
                                     blob is omitted, for diagnostics),
            "untracked_hashes":      { rel: sha256 }  parallel to
                                     untracked_blobs,
        }

    On unrecoverable git failure (corrupted repo, missing ``git`` on
    PATH, etc.) returns a sentinel ``{"captured": False, ...}``;
    :func:`apply_cleanup` short-circuits to a no-op when called with an
    uncaptured baseline.
    """
    repo_resolved = str(Path(repo_root).resolve())
    try:
        snap = _changed_paths(repo_resolved)
        tracked_at_head = _all_tracked_paths(repo_resolved)
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return {
            "captured": False,
            "repo_root": repo_resolved,
            "tracked_at_head": set(),
            "tracked_changed": [],
            "untracked": [],
            "tracked_blobs": {},
            "untracked_blobs": {},
            "tracked_hashes": {},
            "untracked_hashes": {},
        }

    tracked_blobs: Dict[str, bytes] = {}
    tracked_hashes: Dict[str, str] = {}
    for rel in snap["tracked"]:
        data = _read_bytes_safe(repo_resolved, rel)
        if data is not None:
            tracked_blobs[rel] = data
            tracked_hashes[rel] = _sha256(data)

    untracked_blobs: Dict[str, bytes] = {}
    untracked_hashes: Dict[str, str] = {}
    for rel in snap["untracked"]:
        data = _read_bytes_safe(repo_resolved, rel)
        if data is not None:
            untracked_blobs[rel] = data
            untracked_hashes[rel] = _sha256(data)

    return {
        "captured": True,
        "repo_root": repo_resolved,
        "tracked_at_head": tracked_at_head,
        "tracked_changed": list(snap["tracked"]),
        "untracked": list(snap["untracked"]),
        "tracked_blobs": tracked_blobs,
        "untracked_blobs": untracked_blobs,
        "tracked_hashes": tracked_hashes,
        "untracked_hashes": untracked_hashes,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _normalize_declared(declared: Iterable[str]) -> Set[str]:
    """Coerce a declared-files iterable to a set of forward-slash
    repo-relative strings. Empty / non-string entries are dropped.
    """
    out: Set[str] = set()
    for entry in declared or []:
        if not isinstance(entry, str):
            continue
        s = entry.strip().replace("\\", "/")
        while s.startswith("./"):
            s = s[2:]
        if s:
            out.add(s)
    return out


def _file_unchanged_vs_baseline(
    repo_root: str,
    rel: str,
    baseline: Mapping[str, Any],
    post_dirty: Set[str] | None = None,
) -> bool:
    """True if the working-tree content of ``rel`` matches its baseline
    state.

    Used to suppress ``scope_misreport_detected`` for paths that the
    nested session declared as changed but which the wrapper observed
    no real diff for (some sessions over-declare).

    Cases:

      * Captured baseline blob → compare current bytes to blob.
      * Tracked at HEAD but clean at baseline → "unchanged" iff
        ``rel`` is NOT in the post-dispatch dirty set (``post_dirty``)
        AND the working-tree file still exists. If ``post_dirty`` is
        not supplied, fall back to comparing current bytes to HEAD via
        ``git cat-file``.
      * Untracked oversized at baseline → cannot prove unchanged,
        conservative False.
      * Path absent both at baseline and now → vacuously unchanged
        (was nothing, is nothing).
    """
    full = Path(repo_root) / rel
    in_tracked_blobs = rel in baseline.get("tracked_blobs", {})
    in_untracked_blobs = rel in baseline.get("untracked_blobs", {})
    in_tracked_changed = rel in baseline.get("tracked_changed", [])
    in_untracked = rel in baseline.get("untracked", [])
    in_tracked_at_head = rel in baseline.get("tracked_at_head", set())
    repo_resolved = baseline.get("repo_root") or repo_root

    if not full.exists():
        # Was tracked / known at baseline → it was deleted by the
        # session → "touched". Wasn't known at baseline → vacuously
        # unchanged.
        return not (
            in_tracked_blobs
            or in_untracked_blobs
            or in_tracked_changed
            or in_untracked
            or in_tracked_at_head
        )
    try:
        if not full.is_file():
            return False
        cur_bytes = full.read_bytes()
    except (OSError, PermissionError):
        # Cannot read → cannot prove unchanged → conservative.
        return False
    cur_hash = _sha256(cur_bytes)
    if rel in baseline.get("tracked_hashes", {}):
        return baseline["tracked_hashes"][rel] == cur_hash
    if rel in baseline.get("untracked_hashes", {}):
        return baseline["untracked_hashes"][rel] == cur_hash
    if in_tracked_changed or in_untracked:
        # Known at baseline but blob omitted (oversized) → cannot
        # prove unchanged; conservative.
        return False
    if in_tracked_at_head:
        # Tracked at HEAD, clean at baseline. If we have the
        # post-dispatch dirty set, use it (cheap). Otherwise fall back
        # to comparing against HEAD via git cat-file.
        if post_dirty is not None:
            return rel not in post_dirty
        r = _git_bytes(["cat-file", "-p", f"HEAD:{rel}"], cwd=repo_resolved)
        if r.returncode != 0:
            return False
        return _sha256(r.stdout) == cur_hash
    # Not in baseline at all and exists now → newly created → touched.
    return False


def _restore_path(
    repo_root: str,
    rel: str,
    baseline: Mapping[str, Any],
) -> str:
    """Restore ``rel`` to its baseline state. Returns one of:

    * ``"restored_bytes"``   — wrote captured baseline bytes back.
    * ``"restored_head"``    — fell back to ``git restore --source=HEAD``
                               (tracked-and-clean-at-baseline, or
                               oversized-blob path).
    * ``"deleted"``          — path was absent at baseline; removed it.
    * ``"failed"``           — restore attempt errored; left as-is.

    Resolution order:

      1. Captured baseline bytes (tracked or untracked) — write back.
      2. Knew about it at baseline as dirty-but-oversized:
         - Tracked → ``git restore --source=HEAD``.
         - Untracked → delete (no recoverable source).
      3. Tracked at HEAD but clean at baseline → ``git restore --source=HEAD``.
      4. Otherwise → newly-created out-of-scope file → delete.
    """
    full = Path(repo_root) / rel
    in_tracked_blobs = rel in baseline.get("tracked_blobs", {})
    in_untracked_blobs = rel in baseline.get("untracked_blobs", {})
    in_tracked_changed = rel in baseline.get("tracked_changed", [])
    in_untracked = rel in baseline.get("untracked", [])
    in_tracked_at_head = rel in baseline.get("tracked_at_head", set())

    if in_tracked_blobs:
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(baseline["tracked_blobs"][rel])
            return "restored_bytes"
        except (OSError, PermissionError):
            return "failed"

    if in_untracked_blobs:
        try:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(baseline["untracked_blobs"][rel])
            return "restored_bytes"
        except (OSError, PermissionError):
            return "failed"

    if in_tracked_changed or in_untracked:
        # We knew about it at baseline but the blob was omitted
        # (oversized or unreadable). Best we can do for tracked is
        # ``git restore --source=HEAD``; for untracked there is no
        # source to fall back to, so we delete (the cleanup contract
        # is "out-of-declaration writes get reverted" — a partial
        # restore is preferable to a phantom commit).
        if in_tracked_changed:
            r = _git(["restore", "--source=HEAD", "--", rel], cwd=repo_root)
            if r.returncode == 0:
                _git(["restore", "--staged", "--", rel], cwd=repo_root)
                _git(["restore", "--", rel], cwd=repo_root)
                return "restored_head"
            return "failed"
        # Untracked oversized at baseline → delete current.
        try:
            if full.is_file():
                full.unlink()
            return "deleted"
        except (OSError, PermissionError):
            return "failed"

    if in_tracked_at_head:
        # Tracked at HEAD but clean at baseline (no diff captured).
        # Restore via HEAD; this also clears any staging added by the
        # nested session.
        r = _git(["restore", "--source=HEAD", "--", rel], cwd=repo_root)
        if r.returncode == 0:
            _git(["restore", "--staged", "--", rel], cwd=repo_root)
            _git(["restore", "--", rel], cwd=repo_root)
            return "restored_head"
        return "failed"

    # Path was NOT in baseline at all → newly-created out-of-scope
    # file. Delete it (and its now-empty parent dirs are left alone
    # to avoid wiping unrelated structure).
    try:
        if full.is_file():
            full.unlink()
            return "deleted"
        if full.is_dir():
            # Defensive: cleanup is path-by-path. A directory in the
            # delta means a tracked-file's parent dir is what shows up
            # — leave it.
            return "failed"
        # Already absent.
        return "deleted"
    except (OSError, PermissionError):
        return "failed"


def apply_cleanup(
    baseline: Mapping[str, Any],
    declared_files_changed: Iterable[str],
    repo_root: str,
) -> dict:
    """Compute the post-dispatch delta vs ``baseline`` and revert any
    paths in ``(observed_delta − declared_files_changed − protected)``.

    Returns a dict with the following stable keys (consumed by the
    Claude dispatch envelope, TASK-008)::

        {
            "scope_violation_detected": bool,
            "scope_misreport_detected": bool,
            "restored":                 sorted list[str],
            "deleted":                  sorted list[str],
            "failed_paths":             sorted list[str],
            "out_of_scope_paths":       sorted list[str],
            "misreported_paths":        sorted list[str],
            "protected_skipped":        sorted list[str],
            "baseline_captured":        bool,
            "cleanup_strategy":         "delta_bounded" |
                                        "skipped_no_baseline" |
                                        "skipped_git_failed",
        }

    ``failed_paths`` collects per-file ``OSError`` victims observed by
    :func:`_restore_path` (read-only target file, permission denied,
    parent dir not writable, etc.). Each per-file error is caught — the
    path is appended to ``failed_paths`` and processing continues — but
    the failure is now visible to the caller. A non-empty
    ``failed_paths`` is a hard fail at the wrapper boundary because the
    working tree is in an unknown state.

    Behavior:

      * If ``baseline["captured"]`` is False → no-op,
        ``cleanup_strategy="skipped_no_baseline"``.
      * If post-snapshot ``git`` plumbing fails →
        ``cleanup_strategy="skipped_git_failed"``, no mutations.
      * Otherwise: for each path in ``observed_delta`` (everything
        currently changed vs HEAD or untracked-and-unignored):

          - If the path is in :func:`_plan_paths.is_protected_path`,
            classify under ``protected_skipped`` and DO NOT mutate.
          - Else if the path is in ``declared_files_changed``, leave
            it alone (the nested session is allowed to write it).
          - Else: revert via :func:`_restore_path`, recording the
            outcome under ``restored`` (bytes / HEAD restore) or
            ``deleted`` (path was absent at baseline) and flagging
            ``scope_violation_detected=True``.

      * ``scope_misreport_detected`` flips True when any path in
        ``declared_files_changed`` shows no actual diff vs baseline
        (the nested session declared a phantom write).

    Concurrency: this function only ever mutates paths it computed
    from the baseline + observed delta + declared scope passed in by
    the caller; it does not touch any global / shared state. Two
    concurrent callers operating on disjoint declared scopes will see
    each other's writes as "out of scope" relative to their own
    baseline — which is the correct safe behavior. Tests must arrange
    disjoint baselines (one per process) to verify isolation.
    """
    # Use baseline's repo_root if present so callers cannot accidentally
    # point cleanup at a different worktree than the one the snapshot
    # was taken from. This also makes the function safe under
    # ``os.chdir`` race conditions in the parent process.
    repo_resolved = baseline.get("repo_root") or str(
        Path(repo_root).resolve()
    )

    if not baseline.get("captured", False):
        return {
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "failed_paths": [],
            "out_of_scope_paths": [],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": False,
            "cleanup_strategy": "skipped_no_baseline",
        }

    declared = _normalize_declared(declared_files_changed)

    try:
        post = _changed_paths(repo_resolved)
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return {
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
            "restored": [],
            "deleted": [],
            "failed_paths": [],
            "out_of_scope_paths": [],
            "misreported_paths": [],
            "protected_skipped": [],
            "baseline_captured": True,
            "cleanup_strategy": "skipped_git_failed",
        }

    # Observed delta = anything that currently differs from HEAD or is
    # untracked-and-unignored. We further intersect with "actually
    # different from baseline" so that paths which were ALREADY dirty
    # at baseline (and which the nested session left untouched) are
    # not mistakenly reported as a violation.
    post_tracked = set(post["tracked"])
    post_untracked = set(post["untracked"])
    post_dirty = post_tracked | post_untracked
    baseline_tracked = set(baseline.get("tracked_changed", []))
    baseline_untracked = set(baseline.get("untracked", []))

    new_tracked = post_tracked - baseline_tracked
    new_untracked = post_untracked - baseline_untracked
    # A path that was in baseline_tracked (already dirty pre-dispatch)
    # is only an "observed delta" if its current bytes differ from the
    # baseline blob we captured. Otherwise it's the same dirty state
    # that was there before the nested session ran.
    same_tracked_changed: Set[str] = set()
    for rel in post_tracked & baseline_tracked:
        if _file_unchanged_vs_baseline(
            repo_resolved, rel, baseline, post_dirty=post_dirty,
        ):
            same_tracked_changed.add(rel)
    same_untracked_changed: Set[str] = set()
    for rel in post_untracked & baseline_untracked:
        if _file_unchanged_vs_baseline(
            repo_resolved, rel, baseline, post_dirty=post_dirty,
        ):
            same_untracked_changed.add(rel)

    observed_delta: Set[str] = (
        new_tracked
        | new_untracked
        | ((post_tracked & baseline_tracked) - same_tracked_changed)
        | ((post_untracked & baseline_untracked) - same_untracked_changed)
    )

    # Also: paths that existed at baseline (tracked-changed / untracked)
    # but are now ABSENT — the nested session deleted them.
    deleted_by_session = (baseline_tracked | baseline_untracked) - (
        post_tracked | post_untracked
    )
    # Filter to those actually missing on disk (baseline_tracked clean
    # at HEAD-but-restored is not a delta).
    truly_deleted: Set[str] = set()
    for rel in deleted_by_session:
        full = Path(repo_resolved) / rel
        if not full.exists():
            truly_deleted.add(rel)
    observed_delta |= truly_deleted

    # Classify.
    protected_skipped: List[str] = []
    restored: List[str] = []
    deleted: List[str] = []
    failed: List[str] = []
    out_of_scope: List[str] = []

    for rel in sorted(observed_delta):
        if is_protected_path(rel):
            protected_skipped.append(rel)
            continue
        if rel in declared:
            continue
        # Out of scope and not protected → revert.
        outcome = _restore_path(repo_resolved, rel, baseline)
        out_of_scope.append(rel)
        if outcome in ("restored_bytes", "restored_head"):
            restored.append(rel)
        elif outcome == "deleted":
            deleted.append(rel)
        elif outcome == "failed":
            # Per-file OSError caught inside _restore_path. Surface the
            # path to the caller so the wrapper can emit
            # ``cleanup_failure`` (TASK-004 hardening). Processing
            # continues so the remaining out-of-scope paths still get
            # a best-effort revert; we never silence the failure.
            failed.append(rel)

    # Misreport: declared paths that show no actual change.
    misreported: List[str] = []
    for rel in sorted(declared):
        if is_protected_path(rel):
            # Declaring a protected path is itself a violation, but
            # the wrapper does not classify it as misreport; that is
            # an orchestrator concern.
            continue
        if _file_unchanged_vs_baseline(
            repo_resolved, rel, baseline, post_dirty=post_dirty,
        ):
            misreported.append(rel)

    return {
        "scope_violation_detected": bool(out_of_scope),
        "scope_misreport_detected": bool(misreported),
        "restored": sorted(restored),
        "deleted": sorted(deleted),
        "failed_paths": sorted(failed),
        "out_of_scope_paths": sorted(out_of_scope),
        "misreported_paths": sorted(misreported),
        "protected_skipped": sorted(protected_skipped),
        "baseline_captured": True,
        "cleanup_strategy": "delta_bounded",
    }
