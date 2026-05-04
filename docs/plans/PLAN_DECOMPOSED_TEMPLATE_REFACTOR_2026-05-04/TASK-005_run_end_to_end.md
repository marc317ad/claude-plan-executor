# TASK-005 — Run end-to-end decomposer and directory-mode regression checks

## Goal

Run end-to-end decomposer and directory-mode regression checks

## Context

The active executor already has the contract, but it is fragmented. The runtime
authority lives in `plugins/plan-executor/scripts/plan_ops.py`, especially
`_task_header_re`, `_extract_metadata_field`, `_extract_bullet_list`,
`_parse_task_block`, `_render_child_task_file`, `_parse_index_roster`,
`_build_tasks`, and `_gate_schema_valid`. The reusable authoring snippet lives
in `plugins/plan-executor/templates/TASK.md.template`, while the fuller
directory-level guide now lives in
`templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md`.

This refactor should keep `plan_ops.py` as the executable authority. The goal
is not to replace parser validation with documentation. The goal is to make the
renderer and templates share the same contract, then add drift tests that fail
when the documented template stops matching the parser constants.

## Verification

- Existing decomposer fixture tests pass.
- Existing directory-mode smoke tests pass.
- `build-tasks` still emits a valid fat manifest for `tests/fixtures/directory_mode_plan`.
- Per-child `schema-valid` checks still pass for `tests/fixtures/directory_mode_plan/TASK-*.md`.
- No new tests are added unless they protect a concrete behavior gap found while running the regression suite.

## Tasks

### TASK-005: Run end-to-end decomposer and directory-mode regression checks

- **Status:** pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops.py` (modify) - add focused regression coverage only if an uncovered bug is found during verification
  - `tests/scripts/test_implement_plan_directory_smoke.py` (modify) - add focused regression coverage only if an uncovered bug is found during verification
- **Dependencies:** [004]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py tests/scripts/test_implement_plan_directory_smoke.py`
- **Acceptance criteria:**
  - Existing decomposer fixture tests pass.
  - Existing directory-mode smoke tests pass.
  - `build-tasks` still emits a valid fat manifest for `tests/fixtures/directory_mode_plan`.
  - Per-child `schema-valid` checks still pass for `tests/fixtures/directory_mode_plan/TASK-*.md`.
  - No new tests are added unless they protect a concrete behavior gap found while running the regression suite.
- **Reversion guidance:** Revert any regression-test additions made in this task.

**Description:**
Verify that the template refactor did not change the decomposer contract,
directory-mode schedule synthesis, or child-plan schema gates.
