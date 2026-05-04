# TASK-004 — Document validation workflow and refactor boundary

## Goal

Document validation workflow and refactor boundary

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

- The root guide states that `plan_ops.py` remains the runtime authority.
- Plugin template README states which files are human authoring aids and which file is consumed by `_render_child_task_file`.
- Top-level README points users to the decomposed-plan template guide when authoring plans for `implement_plan.py`.
- Documentation does not imply the historical `plugins/plan-decomposer` plugin is present or required in this checkout.

## Tasks

### TASK-004: Document validation workflow and refactor boundary

- **Status:** done
- **Priority:** medium
- **Files:**
  - `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` (modify) - update refactor guidance to reflect completed assets and tests
  - `plugins/plan-executor/templates/README.md` (modify) - document template ownership and drift-test expectations
  - `README.md` (modify) - add or update a short pointer to the decomposed-plan template guide
- **Dependencies:** [003]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k template`
- **Acceptance criteria:**
  - The root guide states that `plan_ops.py` remains the runtime authority.
  - Plugin template README states which files are human authoring aids and which file is consumed by `_render_child_task_file`.
  - Top-level README points users to the decomposed-plan template guide when authoring plans for `implement_plan.py`.
  - Documentation does not imply the historical `plugins/plan-decomposer` plugin is present or required in this checkout.
- **Reversion guidance:** Revert the documentation edits in this task.

**Description:**
Close the loop by documenting the new ownership model. The docs should make it
clear that runtime behavior is parser-owned, human guidance is template-owned,
and drift tests enforce the contract between them.
