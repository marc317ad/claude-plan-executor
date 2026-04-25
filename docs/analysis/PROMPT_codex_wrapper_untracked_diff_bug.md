# Prompt: fix `plan_codex_dispatch.py review` empty-diff on new files

Run this in a fresh session (any branch) to author OR implement the fix.

---

## The bug

`plugins/plan-executor/scripts/plan_codex_dispatch.py` builds the per-task review prompt by running `git diff HEAD -- <files>` (see `render_review_prompt`). For tasks whose `files_changed` are entirely **new (untracked) files**, `git diff HEAD -- new_file.py` returns an empty diff, and Codex receives no content to review.

Observed during run `20260425T124346` of `PLAN_NESTED_DISPATCH`, batch 1:

- TASK-001 created 4 new files (3 module + 1 test). Codex returned `verdict: clean` with note `"No diff content was provided, so there are no directly supported implementation issues to report."`
- TASK-007 created 1 new test file. Same result: `clean`, note `"The provided diff is empty, so there are no directly supported implementation issues to review."`

The wrapper's scope check (`scope.changed_in_scope`) correctly identified the 4 + 1 files were touched. The substance review is what skipped.

This is a silent false-negative: the verdict is `clean` not because the code is correct, but because there was nothing to look at. The orchestrator commits on `clean`, so any bugs Codex would have caught get past the gate.

## Fix shape

Three options, in increasing footprint:

**A. `git add -N` for new files before diff.** In `render_review_prompt` (or its diff-building helper), for each path in `files_changed` that does not yet exist in `HEAD`, run `git add --intent-to-add -- <path>` before `git diff HEAD -- <files>`. `--intent-to-add` causes new files to appear in the diff as add-from-empty, but does NOT actually stage them (working-tree state is preserved). Risk: tiny — `--intent-to-add` is reversible with `git reset -- <path>` and is exactly what `git diff` was designed to reveal.

**B. Build the diff manually for new files.** Detect new files via `git ls-files --others --exclude-standard --error-unmatch -- <path>`, and for each emit a synthesized unified diff (`--- /dev/null` / `+++ b/<path>` / file content prefixed with `+`). Risk: re-implementing diff format; edge cases (binary files, large files, mixed line endings).

**C. Stage and immediately unstage.** `git add -- <files>` then `git diff --cached -- <files>` then `git reset -- <files>`. Risk: small race window where files are staged; if the wrapper crashes mid-review the next operation sees staged state.

Recommend **A**. Simplest, idiomatic, no synthesized output.

## Tests to require

- Unit test: a fixture with one tracked file (modified) and one untracked file in `files_changed[]`. Assert the diff text passed to Codex contains both file paths AND a `+` line per file.
- Regression test: empty `files_changed[]` should not crash; should emit a clear "no files in scope" sentinel that Codex can act on (different from the current "empty diff" silent path).
- Sibling consideration: `plan_claude_dispatch.py` (in flight on `plan/nested-dispatch`) will inherit the same diff-construction shape if it uses git. If yes, fix both wrappers in the same PR.

## Constraints

- Do not regress the existing scope-check / cleanup behavior.
- Do not stage files for real (the operator's working tree should be untouched after review).
- Codex sandbox does not have shell access to the live worktree; the diff is constructed by the wrapper before invocation.

## Affected line ranges (from current `plan/nested-dispatch` HEAD)

- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — `render_review_prompt` (search the function) and any `_diff` / `_build_diff` helper it calls.
- `tests/scripts/test_codex_review_prompt.py` — existing test file, add the new-file diff case here.

## Why now

Run `20260425T124346` proceeds to commit on the false-negative `clean` because the implementer self-tests pass. The fix is a one-line `--intent-to-add` plus a regression test; without it, every plan whose tasks predominantly create new files will get rubber-stamp reviews.
