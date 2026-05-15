# TASK-008 — Wrapper/reconcile edge hardening

## Goal

Remove the operator friction that Phase 1.5 exposed around correct work being paused by wrapper/process edge cases. Deterministic wrapper code should recognize declared plan conventions, while LLMs focus on implementation and review decisions.

## Context

TASK-004 and TASK-006 both paused because `Test command: deferred (TASK-NNN)` was executed as a shell command. TASK-007 paused because `reconcile-batch` treated files under a declared created directory as out of scope. In all three cases, the implementation itself was correct enough to continue, but the wrapper needed operator intervention.

These are heuristic-code responsibilities, not LLM judgment calls.

## Verification

- A declared file scope entry like `tests/scripts/fixtures/example/ (create)` authorizes `tests/scripts/fixtures/example/00_INDEX.json`.
- A task with `Test command: deferred (TASK-007)` does not produce `/bin/sh` syntax errors.
- Deferred tests are represented in run logs using a stable event shape that includes `event: test_deferred`, `task_id`, `deferred_to`, and optional `note`.
- Malformed deferred markers fail closed with an actionable wrapper pause/error instead of being executed by the shell or treated as success.
- Scope annotations are stripped only from recognized suffixes; unknown annotations do not authorize additional paths.
- Review-before-commit remains mandatory after a deferred-test implementation.
- Existing real scope-violation tests still fail or pause as before.
- `venv/bin/python -m pytest tests/scripts/test_reconcile_batch_scope.py tests/scripts/test_implement_plan_deferred_tests.py` passes, or the equivalent focused tests pass if the implementation chooses existing test-file homes.
- `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "reconcile_batch"` passes.

## Tasks

### TASK-008: Wrapper/reconcile edge hardening

- **Status:** done
- **Priority:** high
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (edit)
  - plugins/plan-executor/scripts/plan_codex_dispatch.py (edit, if the independent test runner path lives there)
  - tests/scripts/test_plan_ops.py (edit)
  - tests/scripts/test_reconcile_batch_scope.py (create/edit, or use an existing focused reconcile scope test file)
  - tests/scripts/test_implement_plan_deferred_tests.py (create/edit, or use an existing focused implement-plan wrapper test file)
  - tests/scripts/fixtures/runlog/deferred_test.jsonl (create, or equivalent fixture documenting deferred-test event shape)
- **Dependencies:** [004, 006, 007]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_reconcile_batch_scope.py tests/scripts/test_implement_plan_deferred_tests.py && venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "reconcile_batch"`
- **Acceptance criteria:**
  - The independent test runner recognizes `deferred (TASK-NNN)` as a first-class deferral marker and does not execute it as shell.
  - Deferred test commands record a structured deferred-test run-log event with `event: test_deferred`, `task_id`, `deferred_to`, and optional `note`.
  - Deferred test commands allow the normal review/commit path to continue when the implementer reports success and no other failure is present.
  - Malformed deferred markers, including `deferred` without a task reference, fail closed with a clear wrapper pause/error.
  - `reconcile-batch` treats declared directory scope entries as covering files below them, including trailing-slash entries and entries rendered with recognized suffix annotations.
  - `reconcile-batch` strips recognized scope annotations before path normalization; at minimum `(create)`, `(modify)`, and `(delete)` are supported case-insensitively with optional surrounding whitespace.
  - Unknown scope annotations do not silently widen scope.
  - Regression tests cover nested tracked and untracked files under a declared directory, deferred markers not invoking `/bin/sh`, malformed deferred markers, and deferred test handling preserving review-before-commit.
  - Real out-of-scope files and real shell test failures still pause or fail as before.
- **Reversion guidance:** Revert the wrapper/reconcile changes and their tests together; do not leave the task documents relying on deferred-test or annotated-directory behavior unless the code supports it.

**Description:**
Teach the wrapper and reconcile logic to handle the conventions that Phase 1.5 already uses in task documents: deferred test ownership and declared directory scopes with annotations. This task should reduce operator prompts caused by false positives without weakening the existing fail-closed behavior for malformed markers, real out-of-scope edits, or real test failures.

## Execution log — 20260502T210128 (success)

Starting SHA: `a3b2dea897d760cc10ca693cb537f090e4b3581b`  → Ending SHA: `c443e6b9235294964773c1ed48787f0c9e3d533c`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 008 | codex | claude | ship-with-fixes | c443e6b | 6 minor findings recorded; one important (M1) — _append_run_log in plan_codex_dispatch bypasses tail-verify + ALLOWED_LOG_EVENTS allowlist. Suggested follow-up. |
