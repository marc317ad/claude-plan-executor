# TASK-002 — Add template drift tests against parser constants

## Goal

Add template drift tests against parser constants

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

- Tests assert every member of `ALLOWED_TASK_STATUSES` appears in `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md`.
- Tests assert every member of `ALLOWED_INDEX_STATUSES` appears in `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` and the plugin-local `00_INDEX.json.template`.
- Tests assert every member of `_TASK_REQUIRED_BULLETS` and `_TASK_REQUIRED_PROSE_HEADERS` appears in both the root guide and `decomposed_child.md.template`.
- Tests assert `decomposed_child.md.template` contains the same top-level section contract as `_gate_schema_valid`: `## Goal`, either `## Context` or `## Scoped Context`, and `## Verification`.
- Drift tests import status and required-field constants from `plan_ops` rather than redeclaring them.
- Tests assert `00_INDEX.json.template` parses as JSON as-is, with placeholders appearing only inside JSON string values.

## Tasks

### TASK-002: Add template drift tests against parser constants

- **Status:** done
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops.py` (modify) - add drift tests for root and plugin-local templates
  - `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` (modify) - adjust wording only if tests expose a real contract mismatch
  - `plugins/plan-executor/templates/decomposed_child.md.template` (modify) - adjust template only if tests expose a real contract mismatch
  - `plugins/plan-executor/templates/00_INDEX.json.template` (modify) - adjust template only if tests expose a real contract mismatch
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k template`
- **Acceptance criteria:**
  - Tests assert every member of `ALLOWED_TASK_STATUSES` appears in `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md`.
  - Tests assert every member of `ALLOWED_INDEX_STATUSES` appears in `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` and the plugin-local `00_INDEX.json.template`.
  - Tests assert every member of `_TASK_REQUIRED_BULLETS` and `_TASK_REQUIRED_PROSE_HEADERS` appears in both the root guide and `decomposed_child.md.template`.
  - Tests assert `decomposed_child.md.template` contains the same top-level section contract as `_gate_schema_valid`: `## Goal`, either `## Context` or `## Scoped Context`, and `## Verification`.
  - Drift tests import status and required-field constants from `plan_ops` rather than redeclaring them.
  - Tests assert `00_INDEX.json.template` parses as JSON as-is, with placeholders appearing only inside JSON string values.
- **Reversion guidance:** Revert the test additions and any template wording adjustments made for those tests.

**Description:**
Pin the newly documented contract to the parser constants so future status,
field, or schema changes fail loudly. This is the main accuracy guard for the
refactor.
