# Decomposed Template Refactor Plan

**Created:** 2026-05-04
**Status:** pending
**Base branch:** main

## Goal

Make the decomposed-plan directory contract easier to maintain by reducing
drift between runtime parser behavior, generated child-plan output, and human
authoring templates.

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

The completed refactor should pass these checks:

- `venv/bin/pytest tests/scripts/test_plan_ops.py -k "template or decompose"`.
- `venv/bin/pytest tests/scripts/test_implement_plan_directory_smoke.py`.
- `venv/bin/python plugins/plan-executor/scripts/plan_ops.py build-tasks --plans-dir tests/fixtures/directory_mode_plan --json`.
- `for child in tests/fixtures/directory_mode_plan/TASK-*.md; do venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --check schema-valid --plan-file "$child" --json; done`.

## Tasks

## TASK-001: Add canonical decomposed-plan template assets

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

**Implementation notes:**
Use `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` as the content guide, but
keep the plugin-local files short and reusable. Do not add `Implementation
notes` to `decomposed_child.md.template`; the active renderer does not emit that
section, while `TASK.md.template` and the root guide remain the human-authoring
places that recommend it. Do not include the historical `plan-decomposer`
plugin-only fields as required fields; the active executor requires only the
`00_INDEX.json` fields parsed by `_parse_index_roster`.

## TASK-002: Add template drift tests against parser constants

- **Status:** pending
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

**Implementation notes:**
Prefer direct imports from `plan_ops` where existing tests already do so. Keep
the assertions about documented values and required fields, not about exact
prose, so the guide remains editable without brittle test churn.

## TASK-003: Wire child rendering through the plugin-local template

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

**Implementation notes:**
Template the static skeleton: `# TASK`, `## Goal`, `## Context`,
`## Verification`, `## Tasks`, and the `### TASK-NNN:` heading. Keep conditional
metadata bullets such as `Priority`, `Agent`, `Test command`, and the
`Reversion guidance` sentinel as Python-assembled strings substituted into a
single task-block placeholder, so byte-identical output against
`tests/fixtures/decomposer_inputs/canonical.md` remains practical. Use only
standard-library templating, such as `string.Template`, unless an existing
project helper already fits. Keep escaping simple: the rendered child is
markdown, not JSON. Do not wire `00_INDEX.json.template` into manifest
generation in this task unless it can be done without weakening JSON typing;
`json.dumps` should remain the safest manifest writer.

## TASK-004: Document validation workflow and refactor boundary

- **Status:** pending
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

**Implementation notes:**
Keep this task documentation-only. Avoid expanding the scope into workflow
changes or new CLI behavior.

## TASK-005: Run end-to-end decomposer and directory-mode regression checks

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

**Implementation notes:**
Treat this as a verification and cleanup task. If the full listed pytest command
is too slow in a local sandbox, run the narrower commands from `## Verification`
first and record any deferred test command explicitly in the implementer report.
