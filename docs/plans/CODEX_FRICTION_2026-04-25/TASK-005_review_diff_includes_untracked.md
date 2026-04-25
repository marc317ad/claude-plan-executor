# TASK-005 — Wrapper review diff includes untracked `(create)` files (Issue 6)

## Goal

Stop the Codex review of `(create)`-task work from being a vacuous no-op. Today the wrapper builds the review input via `git diff HEAD -- <files>`, which excludes untracked files; Codex receives an empty diff and returns `verdict: "clean"` with the rationale "No diff content was provided". The reviewer is bypassed by accident, not by design. Fix: stage untracked-but-declared files with `git add -N` before the diff, then unstage immediately so the orchestrator's downstream staging logic is not perturbed.

## Scoped Context

**Where the bug is (`plan_codex_dispatch.py:565-573`).**

```
def git_diff_for_files(repo_root: str, files: list[str]) -> str:
    if not files:
        return ""
    r = _git(["diff", "HEAD", "--"] + files, cwd=repo_root)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout
    r = _git(["diff", "--"] + files, cwd=repo_root)
    return r.stdout if r.returncode == 0 else ""
```

For an untracked path, both `git diff HEAD` and the unstaged-fallback `git diff` produce an empty string. Codex sees an empty diff block, returns `clean`, and the gate is silent. The analysis lists three plausible fixes; the cheapest and least invasive is `git add -N <files>` (intent-to-add) before the diff, which makes untracked files appear in `git diff HEAD` as full-content additions. Reset the index immediately afterwards so the orchestrator's later staging in `commit-task` is not surprised.

**Caller surface — only `cmd_review` (line 1356).**

`git_diff_for_files` is called once, from `cmd_review`. The fix can either land inside `git_diff_for_files` (cleaner, single behaviour change) or wrap it in `cmd_review` (more localised, easier to revert). The cleaner landing is inside `git_diff_for_files` with a new optional parameter `include_untracked: bool = False` so direct callers (none today, but future) keep the existing semantics by default.

**The intent-to-add + reset pattern.**

```
# before the diff
git add -N -- <file1> <file2> ...
# read the diff
git diff HEAD -- <files>
# unwind the intent-to-add so the orchestrator's later staging is unaffected
git reset HEAD -- <file1> <file2> ...
```

`git reset HEAD --` removes the intent-to-add marker without touching the working tree. `git add -N` of an untracked file is a no-op for tracked files (they remain tracked), so the same call sequence works for mixed file lists.

**Failure-mode robustness.** If `git add -N` fails (e.g. file does not exist on disk yet, permission error), fall through to today's behaviour — return whatever `git diff` produces, even if empty. The reviewer then receives the empty diff and returns vacuous-clean as before, but the orchestrator at least logs the wrapper's `_git` failure on stderr. This keeps the fix safe under unexpected file states.

**The optional alternative — synthetic diff via `git diff --no-index /dev/null <file>`.** The analysis lists this as an option. Pro: works without any git index mutation. Con: produces a different diff format (no `index` line, different `+++/---` framing) that Codex's reviewer prompt may receive as visually distinct. The intent-to-add approach is closer to what Codex normally sees, so it stays the primary fix.

**SKILL.md / dispatch-templates docs.** No SKILL prose changes are required — the existing prose says the wrapper sends the diff for `--files`; the change is internal. dispatch-templates.md likewise.

**Out of scope.**
- Untracked-file diffs in `cmd_implement` — the implement path does not read a diff (Codex writes; the wrapper observes deltas). No symptom there.
- Modifying the orchestrator-side `commit-task` staging logic. This task is bounded to the wrapper's review-input builder.

## Verification

- `git_diff_for_files(repo_root, files, *, include_untracked=False)` accepts a new keyword-only parameter. Default `False` preserves today's semantics.
- `cmd_review` calls it with `include_untracked=True`.
- When `include_untracked=True` AND any file in `files` is untracked: the function runs `git add -N -- <files>`, reads `git diff HEAD -- <files>`, then runs `git reset HEAD -- <files>` regardless of diff result. The diff returned is non-empty for the untracked addition.
- When all files are tracked: behaviour is identical to today (the `git add -N` is a no-op, the diff is the same, the reset is a no-op).
- When `git add -N` fails (e.g. file missing): the function still attempts the diff (best-effort), still attempts the reset, and returns whatever the diff produced. No exception is raised.
- The post-call git index state matches the pre-call state — `git status` before and after the function call is identical for the listed files. This is what makes the change safe under parallel sibling dispatches.
- `cmd_review --dry-run` for a task whose `Files:` list contains an untracked path renders a non-empty `prompt_preview` block under `Here is the diff for the changed files:` (verified end-to-end in a new test).
- New unit tests in `tests/scripts/test_plan_codex_dispatch.py`:
  - `test_git_diff_for_files_includes_untracked_when_requested`
  - `test_git_diff_for_files_excludes_untracked_by_default`
  - `test_git_diff_for_files_resets_intent_to_add_after_diff`
  - `test_cmd_review_dry_run_renders_diff_for_untracked_files` (end-to-end through `cmd_review --dry-run`)
- All existing wrapper tests pass unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py` returns 0.

## Tasks

### TASK-005: Wrapper review diff includes untracked `(create)` files

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit)
  - `tests/scripts/test_plan_codex_dispatch.py` (edit)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:516-575` (`_git`, `git_changed_files`, `git_diff_for_files`)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1356-1410` (`cmd_review` head — `review_files` derivation + diff call)
- **Symbol targets:**
  - `git_diff_for_files` in `plan_codex_dispatch.py`
  - `cmd_review` in `plan_codex_dispatch.py`
- **Acceptance criteria:**
  - `git_diff_for_files(repo_root, files, *, include_untracked=False)` keyword-only argument added; default preserves today's semantics.
  - When `include_untracked=True` AND `files` is non-empty, the function executes `git add -N -- <files>` before the diff and `git reset HEAD -- <files>` immediately after, regardless of diff result.
  - `cmd_review` invokes `git_diff_for_files(... include_untracked=True)`.
  - Untracked-file path produces a non-empty diff string in the `cmd_review --dry-run` `prompt_preview`.
  - The git index is unchanged before vs after the call (verified by snapshotting `git status --porcelain=v1` and `git ls-files --others --exclude-standard` in the test).
  - When `git add -N` fails the function does not raise; it best-effort runs the diff and the reset.
  - New tests added (≥4 cases — see Verification).
  - All existing wrapper tests pass unchanged.
  - `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py` returns 0.
- **Reversion guidance:** revert `git_diff_for_files` to its pre-fix body and drop the `include_untracked` parameter; revert `cmd_review` to call without the keyword. The pre-fix vacuous-clean behaviour is documented (Issue 6, Memory item #3) and well-known to operators; reverting is safe under awareness.

**Description:**
Make the wrapper's review diff cover untracked files. The fix is a `git add -N <files>` intent-to-add immediately before the existing diff call, plus `git reset HEAD -- <files>` immediately after to leave the index untouched. The `git_diff_for_files` helper grows a keyword-only `include_untracked` parameter (default `False`); `cmd_review` calls it with `True`. No SKILL/dispatch-templates prose changes — the visible behaviour is "Codex actually sees the new file's content now."

**Implementation notes:**
- Use `try/finally` so the reset runs even if the diff itself raises.
- The reset must be `git reset HEAD -- <files>`, not `git reset HEAD` (no path) — the latter would unstage every staged file in the worktree, which is dangerous in a parallel-batch context.
- `git add -N` for an already-tracked file is a no-op; the function does not need to partition tracked vs untracked before calling.
- `git add -N` for a file that does not exist on disk fails with non-zero exit; the function should swallow that failure (we have no `--quiet` for `add -N`, so capture stderr and continue).
- When `files == []` the function continues to return `""` immediately as today.
- The test helper that builds a tiny git repo (similar to `_reconcile_git_repo` in `test_plan_ops.py`) can be re-used — copy the pattern; do NOT cross-import test scaffolding between modules.
