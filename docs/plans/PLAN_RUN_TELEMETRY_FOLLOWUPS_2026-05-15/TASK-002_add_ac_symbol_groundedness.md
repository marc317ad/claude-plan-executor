# TASK-002 — Add `ac_symbol_groundedness` check to `lint_plans`

## Goal

Add `ac_symbol_groundedness` check to `lint_plans`

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- Extend the `lint_plans` subcommand body in `plan_ops.py` (search for `def cmd_lint_plans` or the `lint-plans` argparse branch — read the existing implementation end-to-end before editing) with a new check named `ac_symbol_groundedness`.
- The check, for each TASK-NNN block in the plan markdown:
- The check is opt-in via a new `--check ac_symbol_groundedness` flag OR runs always at `warning` severity (decision: runs always, since warnings do not block lint exit code).
- The check returns no findings when the AC's named symbol IS present in one of the declared files (positive case fixture).
- Add three test cases:
- The new check appears in the `lint-plans --json` output under the existing `checks[]` (or equivalent) array; do NOT introduce a new top-level key.

## Tasks

### TASK-002: Add `ac_symbol_groundedness` check to `lint_plans`

- **Status:** done
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops_lint_plans.py`
  - `tests/scripts/fixtures/lint_plans/ac_names_missing_symbol.md`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_lint_plans.py -k "ac_symbol or groundedness"`
- **Acceptance criteria:**
  - Extend the `lint_plans` subcommand body in `plan_ops.py` (search for `def cmd_lint_plans` or the `lint-plans` argparse branch — read the existing implementation end-to-end before editing) with a new check named `ac_symbol_groundedness`.
  - The check, for each TASK-NNN block in the plan markdown:
  - The check is opt-in via a new `--check ac_symbol_groundedness` flag OR runs always at `warning` severity (decision: runs always, since warnings do not block lint exit code).
  - The check returns no findings when the AC's named symbol IS present in one of the declared files (positive case fixture).
  - Add three test cases:
  - The new check appears in the `lint-plans --json` output under the existing `checks[]` (or equivalent) array; do NOT introduce a new top-level key.
- **Reversion guidance:** Remove the `ac_symbol_groundedness` check function and its registration in the lint-checks dispatch table; delete the three test cases and the two fixtures. Plan-authors lose the warning; AC-divergence findings return to surfacing only at Phase D cross-review time.

**Description:**
Add `ac_symbol_groundedness` check to `lint_plans`. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-002. See the parent plan's `## Context` and `## Verification` sections for the full intent.)

## Execution log — 20260515T142051 (success)

Starting SHA: `c2a6ea543b720cab3c26dfe9466517fd1786ce7b`  → Ending SHA: `f62836519ab0d1c992e13390d3e11dedef27d421`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 002 | claude | codex | clean | a89b1f1 | All ACs met; ac_symbol_groundedness warning check wired into existing findings[] envelope; warnings excluded from exit-code blocking set. |
