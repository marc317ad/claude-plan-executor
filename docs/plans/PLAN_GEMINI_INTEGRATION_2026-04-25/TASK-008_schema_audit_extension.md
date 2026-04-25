# TASK-008 — Schema audit extension (cover `gemini_*_schema.json`)

## Goal

Extend the schema audit fixture from `CODEX_FRICTION_2026-04-25/TASK-001` (which today globs `codex_*_schema.json` under `plugins/plan-executor/scripts/`) to also walk `gemini_*_schema.json` under the same `additionalProperties: false ⟹ required == set(properties.keys())` invariant.

## Context

The CODEX_FRICTION TASK-001 audit fixture lives at `tests/scripts/test_plan_codex_dispatch_schema.py` (per the friction plan's verification clause). Its purpose is to fail the build whenever any wrapper schema's `properties` set drifts away from its `required` array under `additionalProperties: false` — the OpenAI structured-output validator's invariant. The Gemini wrapper does NOT use OpenAI's validator (per TASK-001 of this plan: Gemini has no `--output-schema` enforcement equivalent), but the same hygiene rule is cheap insurance against the most common JSON-schema authoring mistake regardless of consumer family.

The fix is a one-line glob extension: change the discovery from `codex_*_schema.json` to `*_schema.json` (so any future schema under `plugins/plan-executor/scripts/` matching the pattern is auto-covered) OR keep two parallel globs and union the results. The latter is more explicit but more verbose; the former is more defensive against future naming.

**Choose the unioned approach** — discover both `codex_*_schema.json` and `gemini_*_schema.json` explicitly. Rationale: a wildcard `*_schema.json` would also pick up unrelated schemas added later (e.g. a `plan_input_schema.json` for some other purpose); the union is opt-in per family and makes the audit's coverage visible at the glob site.

The test file's existing function `test_codex_wrapper_schemas_required_matches_properties` (or whatever the friction-plan TASK-001 commit named it) is renamed to `test_wrapper_schemas_required_matches_properties` so it doesn't carry a Codex-only label. Inside, the discovery is the union of two globs.

The existing `test_implement_schema_matches_task_001_report_contract` test stays unchanged — it tests the Codex-specific implementer report contract, which has no Gemini analogue (Gemini does not implement).

**Out of scope.** Wrapper code (TASK-003 / TASK-004). Schema authoring (TASK-002). Orchestrator routing (TASK-006 / TASK-007).

## Verification

- `tests/scripts/test_plan_codex_dispatch_schema.py` is updated:
  - The walker function discovers `codex_*_schema.json` AND `gemini_*_schema.json` (union of two `pathlib.Path.glob` calls).
  - The test function's name is family-agnostic (`test_wrapper_schemas_required_matches_properties` or similar — drop the `codex_` prefix from the function name only; do NOT rename the file because the file is shared with the Codex-specific implementer-report test).
  - The recursive walker structurally identical to the friction-plan TASK-001 implementation — at every nested object that declares `additionalProperties: false`, asserts `set(node['required']) == set(node['properties'].keys())`.
  - The walk handles top-level objects, every `items` schema under `properties.<arr>.items`, and any further nested objects under `properties.<obj>.properties`.
- All four Codex schemas + both new Gemini schemas pass the test. Six schemas total at the time TASK-002 + TASK-008 land: `codex_implement_schema.json`, `codex_review_schema.json`, `codex_plan_review_schema.json`, `codex_plan_review_triage_schema.json`, `gemini_review_schema.json`, `gemini_plan_review_schema.json`.
- The existing `test_implement_schema_matches_task_001_report_contract` continues to pass unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_schema.py` returns 0.

## Tasks

### TASK-008: Schema audit extension (cover `gemini_*_schema.json`)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `tests/scripts/test_plan_codex_dispatch_schema.py` (edit)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_schema.py`
- **Read targets:**
  - `tests/scripts/test_plan_codex_dispatch_schema.py` — full file (the test being extended)
  - `plugins/plan-executor/scripts/gemini_review_schema.json` — full file (TASK-002 output)
  - `plugins/plan-executor/scripts/gemini_plan_review_schema.json` — full file (TASK-002 output)
- **Acceptance criteria:**
  - Test function is renamed (drop `codex_` from the name) and its discovery union is `codex_*_schema.json` ∪ `gemini_*_schema.json`.
  - All six schemas pass the recursive invariant.
  - The existing implementer-report contract test stays unchanged.
  - The discovery is implemented as `list(scripts_dir.glob("codex_*_schema.json")) + list(scripts_dir.glob("gemini_*_schema.json"))` (or equivalent), NOT as `glob("*_schema.json")` — explicit families per the rationale above.
- **Reversion guidance:** revert the test file. The two new Gemini schemas remain on disk after TASK-002; this task only widens the test's coverage glob.

**Description:**
Cheap insurance: extend the existing schema-audit fixture to cover the new Gemini schemas under the same OpenAI-strict invariant. The OpenAI-strict-validator concern is Codex-specific (Gemini has no equivalent), but the underlying invariant ("every property under `additionalProperties: false` must be `required`") catches the most common JSON-schema authoring mistake regardless of consumer. Single-test, single-file edit; no production code changes.

**Implementation notes:**
- The function rename keeps the test parameterization unchanged — the current implementation (per the friction-plan TASK-001 description) uses a single test that walks discovered schemas in a loop. The rename is purely cosmetic.
- If the existing test is parameterized via `pytest.mark.parametrize` per file, the union of globs flows in naturally; if it's a single test that loops internally, same.
- Do NOT add a Gemini-specific equivalent of `test_implement_schema_matches_task_001_report_contract`. There is no Gemini-implement contract; the orchestrator does not dispatch Gemini for implementation. The Codex-specific test stays Codex-specific.
- After this task, six schemas are covered. Future schemas added to `plugins/plan-executor/scripts/` matching neither glob will NOT be auto-covered — the next family's onboarding adds its own glob to the union.
