# TASK-001 — Add canonical decomposed-plan template assets

## Goal

Add canonical decomposed-plan template assets

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

- `decomposed_child.md.template` includes `## Goal`, `## Context`, `## Verification`, `## Tasks`, one `### TASK-001:` block, all required task bullets, `Dependencies`, and `Reversion guidance`.
- `00_INDEX.json.template` includes `schema_version`, `chunks`, and per-chunk `task_id`, `file`, `depends_on`, `status`, and `superseded_by`.
- `plugins/plan-executor/templates/README.md` explicitly points decomposer agents to `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` for the full human guide and to plugin-local templates for reusable assets.
- Existing `TASK.md.template` remains the H3-block-only authoring scaffold for hand-authored plans; new `decomposed_child.md.template` is the full child-file scaffold intended to mirror `_render_child_task_file`. README distinguishes those audiences.
- No runtime behavior changes in this task.

## Tasks

### TASK-001: Add canonical decomposed-plan template assets

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/templates/decomposed_child.md.template` (create) - reusable child-plan file scaffold matching `_render_child_task_file`
  - `plugins/plan-executor/templates/00_INDEX.json.template` (create) - minimal roster scaffold matching `_parse_index_roster`
  - `plugins/plan-executor/templates/README.md` (modify) - document which template covers a task block versus a directory-mode plan
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k template`
- **Acceptance criteria:**
  - `decomposed_child.md.template` includes `## Goal`, `## Context`, `## Verification`, `## Tasks`, one `### TASK-001:` block, all required task bullets, `Dependencies`, and `Reversion guidance`.
  - `00_INDEX.json.template` includes `schema_version`, `chunks`, and per-chunk `task_id`, `file`, `depends_on`, `status`, and `superseded_by`.
  - `plugins/plan-executor/templates/README.md` explicitly points decomposer agents to `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` for the full human guide and to plugin-local templates for reusable assets.
  - Existing `TASK.md.template` remains the H3-block-only authoring scaffold for hand-authored plans; new `decomposed_child.md.template` is the full child-file scaffold intended to mirror `_render_child_task_file`. README distinguishes those audiences.
  - No runtime behavior changes in this task.
- **Reversion guidance:** Delete the two new template files and revert `plugins/plan-executor/templates/README.md`.

**Description:**
Create plugin-local template assets that mirror the root directory guide without
moving runtime behavior yet. This gives decomposer agents and future code a
stable place to find child-plan and roster scaffolds while keeping
`plan_ops.py` as the source of truth.
