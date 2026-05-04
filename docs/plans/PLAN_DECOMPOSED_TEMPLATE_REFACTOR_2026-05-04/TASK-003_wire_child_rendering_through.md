# TASK-003 — Wire child rendering through the plugin-local template

## Goal

Wire child rendering through the plugin-local template

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

- `_render_child_task_file` produces byte-identical output for the existing canonical decomposer fixture, except for deliberate placeholder-format changes approved in this task.
- Auto-decomposed children still pass `_gate_schema_valid`.
- `plan_ops.py decompose-plan --force` remains idempotent for unchanged input.
- Missing source `Reversion guidance` still renders the `none` sentinel.
- Runtime failures to read an external template cannot break installed plugin execution; prefer a module-level constant generated from the asset, with filesystem-relative loading only if the implementation proves runtime file edits are required.

## Tasks

### TASK-003: Wire child rendering through the plugin-local template

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (modify) - load or embed the child template through a small renderer helper
  - `plugins/plan-executor/templates/decomposed_child.md.template` (modify) - adjust placeholders to support byte-stable rendering
  - `tests/scripts/test_plan_ops.py` (modify) - pin generated child output and schema-valid behavior
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k "decompose or template"`
- **Acceptance criteria:**
  - `_render_child_task_file` produces byte-identical output for the existing canonical decomposer fixture, except for deliberate placeholder-format changes approved in this task.
  - Auto-decomposed children still pass `_gate_schema_valid`.
  - `plan_ops.py decompose-plan --force` remains idempotent for unchanged input.
  - Missing source `Reversion guidance` still renders the `none` sentinel.
  - Runtime failures to read an external template cannot break installed plugin execution; prefer a module-level constant generated from the asset, with filesystem-relative loading only if the implementation proves runtime file edits are required.
- **Reversion guidance:** Revert `plan_ops.py`, `decomposed_child.md.template`, and the tests changed by this task.

**Description:**
Move the child markdown scaffold out of ad hoc string assembly while preserving
the decomposer's current output contract. This should make future template
changes visible and reviewable without burying the entire child-plan shape in
Python append calls.
