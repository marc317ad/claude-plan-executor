# TASK-004C — `fail-task` tracked + untracked cleanup

**Parent plan:** [`TASK-004_scheduler_semantics.md`](TASK-004_scheduler_semantics.md) (superseded — split into A/B/C/D/E)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9.1-§9.6
**Base branch:** `phase-7b5-bug-fixes`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract), TASK-003 (wrapper-isolation baseline — protected-paths list).
**Issues absorbed:** ISSUE-011 (P1, primary).

---

## Goal

Make `fail-task` fully restore the working tree when a task fails, regardless of whether the task's `--files` are tracked edits, untracked creates, or a mix of both. The current implementation runs `git restore -- <files>` over the full list, which (a) is a no-op for untracked files (leaves them on disk) and (b) exits non-zero on the first untracked entry, leaving tracked edits unreverted too.

## Scoped Context

### ISSUE-011 (P1) — `fail-task` leaves created untracked files AND crashes on mixed lists

- **Location:** `scripts/plan_ops.py:1281-1328` (`cmd_fail_task`). The single cleanup call is at line 1293.
- **Current behavior:** `git restore -- <files>` is called once with every entry in `--files`. Two failure modes:
  1. **Untracked files are no-ops.** A `(create)` task that left a new file on disk has that file persist after `fail-task`.
  2. **Mixed tracked+untracked crashes without restoring tracked.** Verified empirically: `git restore -- tracked_file untracked_file` emits `error: pathspec 'untracked_file' did not match any file(s) known to git`, exits 1, and **does not restore the tracked file** either.
- **Defect:** failed `(create)` tasks leave artifacts behind that the next run inherits as noise; mixed tasks leave edits unreverted.
- **Fix:** partition `--files` into tracked vs untracked vs protected vs rejected, run the correct cleanup per partition, thread a single `repo_root` through the whole subcommand.

### Empirical confirmation of the mixed-crash behavior

```bash
$ mkdir /tmp/gitest && cd /tmp/gitest && git init -q
$ echo tracked > tracked.txt && git add tracked.txt && git commit -q -m init
$ echo modded > tracked.txt && echo untracked > untracked.txt
$ git restore -- tracked.txt untracked.txt
error: pathspec 'untracked.txt' did not match any file(s) known to git
$ echo "exit=$?"
exit=1
$ cat tracked.txt
modded       # <-- NOT restored to "tracked"
$ test -f untracked.txt && echo yes
yes          # <-- still present
```

This is the exact failure pattern that motivated TASK-004C. V3 locks the regression.

### Hard-won regression coverage from prior run

- **Run `20260415T022232`** — Codex review caught that `cmd_fail_task` calls `git restore` over the full `--files` list without partitioning. Third-opinion code-reviewer agreed it was in-scope ship-blocker. V2 + V3 below lock the regression.

### Protected-paths policy

The cleanup MUST skip always-ignore paths even if they appear in `--files`. The same protection is enforced by `scripts/plan_codex_dispatch.py:52-70` (wrapper cleanup) AND by `scripts/plan_ops.py:720-745` (reconcile-batch). **Three copies currently exist.** Any pair drifting silently is a real hazard. TASK-004C consolidates ALL THREE into one shared module.

Canonical protection set (identical across all three current callsites):

- `PROTECTED_EXACT_PATHS`: `_run_lock.json`, `.claude`, `.codex`
- `PROTECTED_PATH_PREFIXES`: `docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `.claude/`, `.codex/`, `scripts/plan_ops.py`, `scripts/plan_codex_dispatch.py`
- `PROTECTED_PATH_GLOBS`: `docs/plans/*.schedule.json`
- `PROTECTED_PATH_SUFFIXES`: currently empty `()` — preserve the slot for future use; do not remove the tuple.

Predicate semantics (single normalized form across all three callers):

```
is_protected(rel_path) =
    rel_path in EXACT
    OR any(rel_path == p or rel_path.startswith(p) for p in PREFIXES)
    OR any(rel_path.endswith(s) for s in SUFFIXES)
    OR any(fnmatch(rel_path, g) for g in GLOBS)
```

The wrapper at `plan_codex_dispatch.py:320-331` and reconcile at `plan_ops.py:736-745` use this shape today. The shared helper MUST be byte-for-byte equivalent.

---

## Path canonicalization contract

Before any predicate or filesystem operation, every `--files` entry is normalized to a **repo-relative, forward-slash, resolved** form:

```
rel = _canonicalize_file(raw, repo_root):
    # 1. Normalize separators: s = raw.replace("\\", "/")
    # 2. Strip leading "./"
    # 3. If s is absolute: compute rel = Path(s).resolve().relative_to(repo_root.resolve())
    #    Raises ValueError if outside repo_root → classified "out_of_repo".
    # 4. Else: compute abs_ = (repo_root / s).resolve()
    #    If abs_ is NOT under repo_root.resolve() → classified "out_of_repo".
    #    Else rel = abs_.relative_to(repo_root.resolve()).as_posix()
    # Result is always forward-slash, no "./", no "..", relative to repo_root.
```

`a/../scripts/plan_ops.py` and `./scripts/plan_ops.py` MUST both canonicalize to `scripts/plan_ops.py`. The predicate then matches protection rules correctly.

---

## Per-file classification

Every entry in `--files` goes through exactly one branch:

**Check order is authoritative (top-to-bottom). Earlier checks win; a gitlink IS a directory in the worktree and MUST classify as `submodule`, not `directory` — so submodule precedes directory.**

| Classification | Entry condition | Cleanup action | Emit bucket |
|---|---|---|---|
| `out_of_repo` | Canonicalization escapes `repo_root` (absolute-outside, `../../x`, symlink escape) | **None** — no mutating filesystem or git operations | `out_of_repo_skipped` |
| `submodule` | `(repo_root / rel)` is a gitlink entry itself (`git ls-files --stage -- <rel>` emits mode `160000`) OR sits inside a nested git repo distinct from `repo_root` (probe `git rev-parse --show-toplevel` with `cwd=parent`) | **None** — no mutating filesystem or git operations | `submodule_skipped` |
| `directory` | `(repo_root / rel).is_dir()` and not a symlink (already confirmed NOT a submodule gitlink) | **None** — no mutating filesystem or git operations; implementer must not recurse | `directory_skipped` |
| `protected` | `is_protected(rel)` returns True (AFTER canonicalization) | **None** — no mutating filesystem or git operations | `protected_skipped` |
| `tracked` | `git ls-files --error-unmatch -- <rel>` exits 0 AND none of the above | `git restore --staged --worktree -- <rel>` (batched with other tracked) | no emit list (used for `restore_ok`) |
| `untracked` | All other cases — includes "untracked create", "ignored-but-present", "never existed" | `Path(repo_root / rel).unlink(missing_ok=True)` | `removed_untracked` iff file actually existed and was removed |

Notes:

- **Ignored-but-present files** are classified as `untracked` because `git ls-files --error-unmatch` exits non-zero for them. This is the documented behavior of this subcommand — if a test writes a file matched by `.gitignore`, it is still subject to cleanup. Spelled out so implementers don't add a special-case.
- **Never-existed paths** classify as `untracked` (ls-files fails) and produce a no-op unlink (`missing_ok=True`). They MUST NOT appear in `removed_untracked`.
- **Tracked-but-already-deleted paths** (`git status` would show as "deleted"): `ls-files --error-unmatch` still exits 0. `git restore --staged --worktree` will re-materialize from HEAD. Correct and desired.
- **Nonexistent paths INSIDE an existing submodule** (e.g. `submods/foo/new.txt` where `submods/foo` is a gitlink but `new.txt` is not yet created): these are classified as `submodule` because the submodule probe walks up from `(repo_root / rel).parent` and finds the nested `.git` root BEFORE the existence check on the leaf path. The `_is_inside_submodule(abs_path, rel, repo_root)` helper MUST probe the nearest existing ancestor directory, not only the leaf. This covers the "anything rooted inside a submodule is skipped" policy even for new/untracked files. Any nonexistent path rooted in a non-submodule directory falls through to `untracked` as before.

---

## `restore_ok` semantics

`restore_ok` is a single boolean in the emit:

- `True` if:
  - no `tracked` partition entries OR
  - `git restore --staged --worktree -- <tracked_list>` exited 0
- `False` if:
  - the `git restore` call exited non-zero (per-file granularity not tracked; the batched call is atomic from git's perspective — either all tracked files restore or none do)
- **Independent of**: out_of_repo, directory, submodule, protected, removed_untracked. Classification-only skips do not degrade `restore_ok`.
- **Independent of**: `unlink` results. A missing untracked file → still `restore_ok=True`.

This keeps the boolean meaningful: it reports ONLY whether the tracked-side restore succeeded.

---

## Verification

**V1 — `fail-task` restores tracked edits.**

```python
def test_fail_task_restores_tracked_edit(tmp_path_git_repo):
    # Scratch repo. Commit a file with known content. Modify it.
    # Call fail-task --files <that file> --repo-root <tmp>.
    # Assert: file content restored; restore_ok=True; removed_untracked=[].
```

**V2 — `fail-task` removes untracked creates inside `--files`.** *(ISSUE-011 primary fix.)*

```python
def test_fail_task_removes_untracked_creates(tmp_path_git_repo):
    # Scratch repo. Create a new untracked file under repo_root.
    # Call fail-task --files <that file> --repo-root <tmp>.
    # Assert: file no longer exists; removed_untracked == [rel_path];
    # restore_ok=True (no tracked partition).
```

**V3 — MIXED tracked+untracked list restores tracked AND removes untracked.** *(Hard-won regression — do NOT relax.)*

```python
def test_fail_task_mixed_tracked_untracked_cleanup(tmp_path_git_repo):
    # Scratch repo. Commit tracked.txt. Modify it. Create untracked.txt.
    # Call fail-task --files "tracked.txt,untracked.txt" --repo-root <tmp>.
    # Assert ALL of:
    #   (a) tracked.txt restored to committed content
    #   (b) untracked.txt removed from disk
    #   (c) result["restore_ok"] is True  (not merely truthy — `is True`)
    #   (d) set(result["removed_untracked"]) == {"untracked.txt"}
    #   (e) result["protected_skipped"] == []
    #   (f) result["out_of_repo_skipped"] == []
    # The (c) + (d) assertions specifically lock out the one-shot
    # `git restore -- <mixed>` regression: that path exits 1, so
    # restore_ok would be False AND removed_untracked would be empty.
```

**V4 — Untracked siblings outside `--files` are NOT touched.**

```python
def test_fail_task_preserves_sibling_untracked(tmp_path_git_repo):
    # in_scope.txt and sibling.txt both untracked.
    # fail-task --files in_scope.txt.
    # Assert: in_scope.txt removed; sibling.txt preserved.
```

**V5 — Protected paths are NEVER touched.**

```python
def test_fail_task_skips_protected_paths(tmp_path_git_repo):
    # Modify docs/plans/_run_log.jsonl (tracked) and create
    # docs/plans/sample.schedule.json (untracked, matches glob).
    # fail-task --files "docs/plans/_run_log.jsonl,docs/plans/sample.schedule.json".
    # Assert:
    #   - _run_log.jsonl still "modded" on disk
    #   - sample.schedule.json still present
    #   - sorted(protected_skipped) == sorted([both rel paths])
    #   - restore_ok=True (no tracked partition ran)
    #   - removed_untracked=[]
```

**V6 — Idempotent on already-clean tracked file.**

```python
def test_fail_task_idempotent_no_op_when_clean(tmp_path_git_repo):
    # Commit, do NOT modify. fail-task --files <file>.
    # Assert: exit 0; file unchanged; restore_ok=True; removed_untracked=[].
```

**V7 — Plan-markdown status mutation runs even when EVERY --files entry is skip-classified.**

```python
def test_fail_task_mutates_plan_status_when_no_cleanup_fires(tmp_path_git_repo):
    # --files contains only paths that classify as skip buckets:
    # a directory, a path outside repo_root, and a protected path.
    # Assert: plan markdown flipped to "failed"; run-log event appended;
    # restore_ok=True (tracked list was empty → no restore attempted);
    # removed_untracked=[]; protected_skipped contains the protected one;
    # directory_skipped contains the dir; out_of_repo_skipped contains the escape.
    # Exit code 0 — skip classifications are not errors.
```

**V8 — Output shape is complete and stable.**

```python
def test_fail_task_output_shape(tmp_path_git_repo):
    # Assert exact top-level keyset:
    expected = {"restore_ok", "status_updated", "log_appended",
                "removed_untracked", "protected_skipped",
                "out_of_repo_skipped", "directory_skipped",
                "submodule_skipped"}
    assert set(result.keys()) == expected
```

**V9 — `--repo-root` threaded through coherently.**

```python
def test_fail_task_repo_root_threaded(tmp_path_git_repo, monkeypatch):
    # Run fail-task from a DIFFERENT cwd (not tmp_path_git_repo). Pass
    # --repo-root <tmp_path_git_repo>. --files entries are repo-relative.
    # Assert: ls-files probe, git restore, and unlink all operate on paths
    # rooted at --repo-root, NOT cwd. Verify by pre-seeding a decoy file at
    # cwd/in_scope.txt — it MUST remain untouched.
```

**V10 — Protected-path predicate parity across all three callsites.**

```python
def test_protected_path_predicate_is_shared(tmp_path_git_repo):
    # Import is_protected_path from scripts._plan_paths.
    # For N fixture paths (exact, prefix, suffix, glob, Windows backslash,
    # ./-prefixed, a/../-containing), assert:
    #   - fail-task skips each one (integration-style)
    #   - plan_codex_dispatch.is_protected_path (or whatever it imports)
    #     returns True for each
    #   - plan_ops._is_reconcile_protected (or whatever replaces it) also
    #     returns True for each
    # Three-way parity prevents any of the three copies from re-introducing
    # drift. If the implementer chose the "shared module" refactor, all three
    # callsites MUST alias the same function.
```

**V11 — Absolute path outside repo_root is classified `out_of_repo`, not unlinked.**

```python
def test_fail_task_refuses_absolute_path_outside_repo(tmp_path_git_repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("important")
    # fail-task --files <abs path to outside.txt> --repo-root <repo>.
    # Assert:
    #   - outside.txt STILL EXISTS with original content (never touched)
    #   - result["out_of_repo_skipped"] contains the abs path (verbatim)
    #   - removed_untracked=[]
    #   - restore_ok=True (no tracked entries)
    #   - status_updated=True
    # This locks the CRITICAL path-escape hazard from Codex review of 004C v1.
```

**V12 — Relative path with `..` escape is classified `out_of_repo`.**

```python
def test_fail_task_refuses_dotdot_escape(tmp_path_git_repo):
    # --files="../outside.txt" (the repo's parent-dir sibling) --repo-root=<repo>.
    # The parent dir must have such a file seeded.
    # Assert: outside file preserved; entry in out_of_repo_skipped;
    # removed_untracked=[].
```

**V13 — Directory entry is classified `directory`, not recursed.**

```python
def test_fail_task_refuses_directory(tmp_path_git_repo):
    # mkdir some_dir/, populate with two files (tracked and untracked).
    # fail-task --files=some_dir --repo-root=<repo>.
    # Assert:
    #   - some_dir/ still present, both children untouched
    #   - directory_skipped == ["some_dir"]
    #   - restore_ok=True; removed_untracked=[]
```

**V14 — Submodule path is classified `submodule`, not touched.**

```python
def test_fail_task_refuses_submodule_path(tmp_path_git_repo_with_submodule):
    # Create a scratch submodule inside the repo. A file lives inside it.
    # fail-task --files=<rel path into submodule> --repo-root=<super>.
    # Assert: file untouched; submodule_skipped contains the path;
    # removed_untracked=[]; restore_ok=True.
    # If submodule fixture is too heavy, MAY substitute a nested `git init`
    # inside the repo (not tracked as a submodule) — same classification.
```

**V14b — Submodule ROOT (the gitlink directory itself) is classified `submodule`, NOT `directory`.**

```python
def test_fail_task_classifies_submodule_root_as_submodule(tmp_path_git_repo_with_submodule):
    # This test locks in the gitlink-vs-directory ordering.
    # Pre-condition: `submods/foo` is a submodule whose super-project records it
    # via mode 160000 (gitlink). In the working tree, `submods/foo` IS an actual
    # directory, which would match `.is_dir()` first if classification order
    # were directory-before-submodule.
    # fail-task --files="submods/foo" --repo-root=<super>.
    # Assert:
    #   - submodule_skipped == ["submods/foo"]
    #   - directory_skipped == []   (critical — NOT "submods/foo")
    #   - removed_untracked == []
    #   - restore_ok = True
    #   - submods/foo still exists in the worktree, unchanged
    # If this test fails with submods/foo in directory_skipped, the gitlink
    # check is running AFTER the directory check — regression.
```

**V15 — Path normalization: `./foo` and `a/../scripts/plan_ops.py` canonicalize.**

```python
def test_fail_task_normalizes_path_forms(tmp_path_git_repo):
    # --files="./docs/plans/_run_log.jsonl,scripts/./plan_ops.py".
    # Both should canonicalize to their protected form and land in
    # protected_skipped. An un-normalized predicate would miss the second.
```

**V16 — Tracked-deleted path is restored from HEAD.**

```python
def test_fail_task_restores_tracked_deleted(tmp_path_git_repo):
    # Commit tracked.txt. `os.remove(tracked.txt)` before calling fail-task.
    # fail-task --files="tracked.txt" --repo-root=<repo>.
    # Assert: tracked.txt present with committed content; restore_ok=True;
    # removed_untracked=[].
```

**V17 — Never-existed path is a silent no-op.**

```python
def test_fail_task_silent_on_nonexistent_path(tmp_path_git_repo):
    # --files="nonexistent.txt" (never tracked, never present).
    # Assert: restore_ok=True; removed_untracked=[] (NOT listed — file
    # didn't exist so nothing was removed); status_updated=True; exit 0.
```

---

## Tasks

### TASK-004C: Implement tracked+untracked partition cleanup in `fail-task` + extract shared protected-paths module

- **Status:** pending
- **Priority:** high
- **Files:**
  - `scripts/plan_ops.py` (rewrite `cmd_fail_task`; delete `RECONCILE_PROTECTED_*` + `_is_reconcile_protected` at lines 720-745 and replace with import from shared module; add `--repo-root` arg if absent)
  - `scripts/plan_codex_dispatch.py` (delete `PROTECTED_EXACT_PATHS` / `PROTECTED_PATH_PREFIXES` / `PROTECTED_PATH_SUFFIXES` / `PROTECTED_PATH_GLOBS` at lines 52-70 and the `is_protected_path` function body at 316-331; replace with import from shared module; constants re-exported for any legacy import)
  - `scripts/_plan_paths.py` (**new**) — shared constants + `is_protected_path` + `canonicalize_file`
  - `tests/scripts/test_plan_ops.py` (V1–V17)
- **Dependencies:** TASK-001, TASK-003
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py tests/scripts/test_plan_codex_dispatch_state_isolation.py tests/scripts/test_plan_codex_dispatch_integration.py`
- **Acceptance criteria:**
  - Every `--files` entry is canonicalized per the "Path canonicalization contract" section before classification.
  - Each entry gets exactly one classification: `out_of_repo` | `protected` | `submodule` | `directory` | `tracked` | `untracked`. Classification is mutually exclusive; the order of checks matches the classification table above (submodule BEFORE directory — a gitlink root IS a directory in the worktree, so the directory check would misclassify it if run first).
  - Tracked files restored via `git restore --staged --worktree -- <tracked_list>` in a single batched call, with `cwd=repo_root`.
  - Untracked files unlinked via `Path(repo_root / rel).unlink(missing_ok=True)`. Only files that actually existed at unlink time are added to `removed_untracked`.
  - Protected, directory, submodule, and out_of_repo classifications MUST NOT perform any **mutating** filesystem or git operation (no `git restore`, no `unlink`, no directory traversal). They may — and do — perform read-only probes (`.is_dir()`, `git ls-files --stage`, `git rev-parse --show-toplevel`) as part of classification.
  - `restore_ok` semantics per the "`restore_ok` semantics" section above — independent of skip classifications and of unlink outcomes.
  - Emit shape: `{"restore_ok", "status_updated", "log_appended", "removed_untracked", "protected_skipped", "out_of_repo_skipped", "directory_skipped", "submodule_skipped"}` — exactly these eight keys.
  - Plan-markdown status mutation to `failed` ALWAYS runs (preserves existing contract at `scripts/plan_ops.py:1299`).
  - Shared module `scripts/_plan_paths.py` is the sole source of `PROTECTED_*` constants AND of `is_protected_path` AND of `canonicalize_file`. All three current callsites (`plan_codex_dispatch.py` wrapper cleanup, `plan_ops.py` reconcile-batch, `plan_ops.py` fail-task) import from it. No duplicated constants anywhere. **Wrapper helper renaming:** the existing `scripts/plan_codex_dispatch.py:_is_protected` helper is removed from that module. The shared-module function is named `is_protected_path` (public). To avoid breaking `tests/scripts/test_plan_codex_dispatch_state_isolation.py:197-220` which references `wrapper._is_protected`, add a module-level compatibility alias at the top of `plan_codex_dispatch.py` immediately after the import: `_is_protected = is_protected_path`. That alias is the single surviving reference in the wrapper module — all internal call sites (`plan_codex_dispatch.py:533-643`) continue to reference `_is_protected` unchanged. The alias is the sanctioned migration path; do NOT edit the test file. Likewise, the existing `scripts/plan_ops.py:_is_reconcile_protected` helper (if present) is removed and replaced by `is_protected_path` at the reconcile callsite — no alias needed there because no external test imports it.
  - V1–V17 all pass.
- **Out of scope (handled by sibling sub-plans):**
  - `filter-schedule` subcommand → TASK-004A
  - `batch-next` batch fidelity → TASK-004B
  - `block-dependents` plan-markdown mutation → TASK-004D
  - `acquire-lock` strict shape → TASK-004E

**Description:**
Partition `--files` before cleanup. Thread `--repo-root` coherently through probe + restore + unlink. Extract the protected-paths predicate into `scripts/_plan_paths.py` and replace BOTH current copies (wrapper at `plan_codex_dispatch.py:52-70`+`316-331` and reconcile at `plan_ops.py:720-745`) plus the new `fail-task` call. No duplicated constants.

**Argument convention — `--files` CSV caveat:**

`--files` is CSV-split at the CLI boundary. Filenames containing literal `,` (commas) are NOT supported by this subcommand — they already aren't supported by the existing impl either. If a caller needs to pass such a file, they cannot; this is documented but not enforced as an error. Normal filenames with spaces, unicode, `./`, `../`, and backslashes ARE supported.

**Implementation sketch (Step 2 of playbook):**

```python
def cmd_fail_task(args: argparse.Namespace) -> None:
    from scripts._plan_paths import is_protected_path, canonicalize_file

    tid = _normalize_task_id(args.task_id)
    if not tid:
        _die(args, {"error": f"bad --task-id: {args.task_id!r}"})

    plan = Path(args.plan_file)
    if not plan.is_file():
        _die(args, {"error": f"plan file not found: {plan}"})

    repo_root = Path(args.repo_root).resolve() if getattr(args, "repo_root", None) \
                else Path.cwd().resolve()

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
            # canonicalize_file returns None for out-of-repo escapes
            out_of_repo_skipped.append(raw)
            continue
        abs_path = (repo_root / rel)
        # Order of checks matches the classification table — submodule BEFORE
        # directory (a gitlink is a directory in the worktree, must classify
        # as submodule not directory).
        if _is_inside_submodule(abs_path, rel, repo_root):
            submodule_skipped.append(rel)
            continue
        if abs_path.is_dir() and not abs_path.is_symlink():
            directory_skipped.append(rel)
            continue
        if is_protected_path(rel):
            protected_skipped.append(rel)
            continue
        probe = _git(["ls-files", "--error-unmatch", "--", rel], cwd=str(repo_root))
        if probe.returncode == 0:
            tracked.append(rel)
        else:
            untracked.append(rel)

    restore_ok = True
    if tracked:
        restore = _git(["restore", "--staged", "--worktree", "--", *tracked],
                       cwd=str(repo_root))
        if restore.returncode != 0:
            restore_ok = False

    removed_untracked: list[str] = []
    for rel in untracked:
        abs_path = repo_root / rel
        if not abs_path.exists() and not abs_path.is_symlink():
            continue
        try:
            abs_path.unlink()
        except (FileNotFoundError,):
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


def _is_inside_submodule(abs_path: Path, rel: str, repo_root: Path) -> bool:
    """True if abs_path is a gitlink OR sits inside a nested git repo distinct
    from repo_root. Called with the already-canonicalized `rel` to avoid a
    second canonicalization pass inside the probe.

    Covers the nonexistent-leaf case: for `submods/foo/new.txt` where `new.txt`
    does not yet exist but `submods/foo` is a gitlink, the probe walks up from
    `abs_path.parent` to the nearest existing ancestor and runs
    `git rev-parse --show-toplevel` there. If that toplevel differs from
    `repo_root`, the path is inside a submodule.
    """
    probe_gitlink = _git(["ls-files", "--stage", "--", rel], cwd=str(repo_root))
    if probe_gitlink.returncode == 0 and probe_gitlink.stdout.startswith("160000"):
        return True
    # Walk up to the nearest existing ancestor so nonexistent leaves under a
    # submodule still classify correctly.
    probe_dir = abs_path if abs_path.exists() else None
    if probe_dir is None:
        cursor = abs_path.parent
        repo_root_resolved = repo_root.resolve()
        while cursor != cursor.parent and cursor.resolve() != repo_root_resolved:
            if cursor.exists():
                probe_dir = cursor
                break
            cursor = cursor.parent
    if probe_dir is None:
        return False  # entire ancestor chain absent — treat as non-submodule
    if probe_dir.is_file() or probe_dir.is_symlink():
        probe_dir = probe_dir.parent
    tl = _git(["rev-parse", "--show-toplevel"], cwd=str(probe_dir))
    if tl.returncode != 0:
        return False
    return Path(tl.stdout.strip()).resolve() != repo_root.resolve()
```

**Reversion guidance:**

- If `canonicalize_file` breaks for exotic but in-repo paths (e.g., `~` expansion), tighten the canonicalization; never widen the predicate to let out-of-repo entries through.
- If the shared `_plan_paths.py` module causes a circular import (it shouldn't — the module's only imports are `fnmatch`, `os`, `re`, `pathlib`, and `typing`, all stdlib), inline the constants in BOTH `plan_ops.py` AND `plan_codex_dispatch.py` temporarily AND file a follow-up bug. **Do NOT "fix" circularity by making the wrapper import from `plan_ops.py`** — the wrapper must stay independent of the plan-ops CLI.
- If submodule detection misfires in an unexpected environment (e.g., bare repo), prefer skipping the path (false-positive submodule classification) over restoring/unlinking; an unexpected skip is observable, a wrong restore is not.

---

## Implementation Playbook

### Step 1 — Create shared module `scripts/_plan_paths.py`

```python
"""Shared path constants, predicate, and canonicalization for the
/implement-plan executor. Imported by:

  - scripts/plan_codex_dispatch.py (wrapper delta-cleanup)
  - scripts/plan_ops.py (reconcile-batch AND fail-task)

Duplicating any of these in the two consumers is forbidden — the three
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
    "scripts/plan_ops.py",
    "scripts/plan_codex_dispatch.py",
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


def canonicalize_file(raw: str, repo_root: Path) -> Optional[str]:
    """Normalize raw --files entry to a repo-relative forward-slash path.
    Returns None if the entry escapes repo_root (absolute outside, .. escape,
    Windows-drive absolute on POSIX, or symlink escape after resolve()).

    Rules:
    - Windows-drive absolute paths (regex `^[A-Za-z]:[/\\\\]`) are REJECTED on
      POSIX hosts. We do NOT silently translate them to relative paths; that
      would misclassify `C:\\repo\\file` (an out-of-repo absolute Windows path)
      as an in-repo relative path once backslashes are normalized. WSL callers
      must pass the POSIX equivalent (`/mnt/c/repo/file`), which then gets
      resolved normally.
    - Windows-drive absolute paths on Windows hosts (`pathlib.PureWindowsPath`
      or `os.name == "nt"`) fall through to `Path().is_absolute()` naturally
      and behave like other absolute paths.
    - Plain backslashes inside a filename (`foo\\bar.txt` meaning a file
      named `bar.txt` in directory `foo` on Windows, or a file containing a
      literal backslash on POSIX) are ambiguous. We normalize backslashes to
      forward-slashes on BOTH hosts because `fnmatch`/prefix matches below use
      forward-slashes, and legitimate repo paths never contain literal
      backslashes. Callers that need to target a file whose name contains a
      literal backslash on POSIX are unsupported by this subcommand.
    """
    if _looks_like_windows_drive_absolute(raw) and os.name != "nt":
        return None  # out_of_repo; caller will bucket accordingly
    s = raw.replace("\\", "/")
    candidate = Path(s)
    if candidate.is_absolute():
        abs_ = candidate.resolve()
    else:
        abs_ = (repo_root / candidate).resolve()
    try:
        rel = abs_.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return rel.as_posix()


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")


def _looks_like_windows_drive_absolute(raw: str) -> bool:
    return bool(_WINDOWS_DRIVE_RE.match(raw))
```

### Step 2 — Replace wrapper copy in `plan_codex_dispatch.py`

Delete lines 52-70 (constants) and the body of `is_protected_path` at 316-331. Replace with:

```python
from _plan_paths import (
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
    is_protected_path,
)
```

(Or `from scripts._plan_paths import ...` — use whatever matches the existing import style in the wrapper.)

Preserve any module-level re-exports if the wrapper's own tests import the constants by name from `plan_codex_dispatch`. Grep for `plan_codex_dispatch.PROTECTED` in tests; if matches, leave a trivial re-export.

### Step 3 — Replace reconcile copy in `plan_ops.py:720-745`

Delete `RECONCILE_PROTECTED_EXACT`, `RECONCILE_PROTECTED_PREFIXES`, `RECONCILE_PROTECTED_GLOBS`, and `_is_reconcile_protected`. Any internal caller of `_is_reconcile_protected` now calls `is_protected_path` from the shared module. Grep before deleting:

```bash
grep -n "_is_reconcile_protected\|RECONCILE_PROTECTED_" scripts/plan_ops.py
```

### Step 4 — Rewrite `cmd_fail_task`

Per the Implementation sketch above. Add `--repo-root` to the argparse for `fail-task` if it is absent:

```python
p_fail.add_argument("--repo-root", default=None,
                    help="Repo root for path resolution; defaults to CWD")
```

### Step 5 — Tests

Follow V1–V17 from the Verification section. Use the `tmp_path_git_repo` fixture pattern:

```python
@pytest.fixture
def tmp_path_git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "T"], check=True)
    (tmp_path / "tracked.txt").write_text("original\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "init"], check=True)
    yield tmp_path
```

For V14 (submodule), either register a scratch submodule via `git submodule add` (slow but accurate) or substitute a `git init` inside a subdir (nested-repo, faster, same classification — `_is_inside_submodule` returns True for both).

Do NOT pass `--repo-root` in tests that verify backward compat (V6); rely on `monkeypatch.chdir(tmp_path)` in those cases.

### Step 6 — Regression sweep

```bash
venv/bin/pytest -q \
  tests/scripts/test_plan_ops.py \
  tests/scripts/test_plan_codex_dispatch_state_isolation.py \
  tests/scripts/test_plan_codex_dispatch_integration.py
```

The state-isolation test (`test_plan_codex_dispatch_state_isolation.py`) is the one that pins wrapper protected-path behavior — it MUST still pass after the shared-module extraction. The integration test sometimes depends on repo shape and may be slow; it should also pass because the wrapper's behavior doesn't change, only its source of truth.

### Forbidden patterns

- Inlining the constants in `plan_ops.py` OR `plan_codex_dispatch.py` after the shared module exists. Three-copy drift is the exact bug this fixes.
- `shutil.rmtree` on classified directories. Directories are `directory_skipped`, period.
- Recursing into submodules to clean up their files. Submodules are `submodule_skipped`.
- Running `git restore` with `cwd=None` (inherits caller's cwd). Always pass `cwd=str(repo_root)`.
- Using `Path.cwd()` inside the classification/cleanup logic. Only the initial `repo_root` computation may reference CWD.
- Swallowing a non-zero exit from the batched `git restore` without flipping `restore_ok=False`.
- Adding any path to `removed_untracked` that was not actually unlinked on this call (a "never existed" path must not appear).
- Making `plan_codex_dispatch.py` import from `plan_ops.py` "to resolve circularity". The wrapper must remain CLI-independent of the plan-ops tool.

---

## Out of Scope

- `filter-schedule` subcommand → TASK-004A
- `batch-next` batch fidelity → TASK-004B
- `block-dependents` plan-markdown mutation → TASK-004D
- `acquire-lock` strict shape → TASK-004E
- Canonical contract (TASK-001), generic runtime validation (TASK-002), wrapper isolation (TASK-003), phase gates (TASK-005), fixture rewrite (TASK-006), self-audit (TASK-007), preflight (TASK-008), scale/locks/logs (TASK-009/010/011).

## Reversion guidance (consolidated)

- If `canonicalize_file` breaks for an exotic in-repo path form, tighten canonicalization; never widen by letting out-of-repo entries through.
- If the shared `_plan_paths.py` module can't be imported by the wrapper (shouldn't happen — only depends on stdlib), TEMPORARILY inline constants in both consumers AND file a bug. Never make the wrapper import from `plan_ops.py`.
- If the submodule predicate misfires, prefer false-positive `submodule_skipped` (observable) over wrong restore/unlink (silent data loss).
- If partition classification misfires on edge inputs, add a test mirroring V1–V17 before adjusting; this plan already enumerates the accepted edge cases.
