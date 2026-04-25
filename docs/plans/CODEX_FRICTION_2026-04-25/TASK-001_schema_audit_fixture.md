# TASK-001 — Schema audit + structural fixture (Issue 1)

## Goal

Prevent the OpenAI structured-output schema regression that fired on the friction run from recurring. The wrapper sends each schema verbatim to the Codex CLI; OpenAI's validator rejects any schema where `additionalProperties: false` is set on an object whose `required` array is not exactly `set(properties.keys())`. A regression test is the cheapest insurance.

## Context

**The latent bug.** Between commits `56b5580` (added `target_task_id` to `findings.items.properties`) and `8103a1c` ("fix fuck ups", added it to `findings.items.required`) the on-disk `codex_plan_review_schema.json` was structurally invalid for OpenAI's strict structured-output validator. Both commits predate the friction run (2026-04-25), but the ATS run almost certainly hit a stale plugin cache copy at `~/.claude/plugins/cache/claude-plan-executor/plan-executor/0.1.0/scripts/codex_plan_review_schema.json` (which on inspection still lacks `target_task_id` entirely — far older than HEAD). The class of bug — a property added without being added to `required` under `additionalProperties: false` — is a one-line oversight that the file-format does nothing to prevent.

**What "valid for OpenAI structured output" means here.** For every nested object that declares `additionalProperties: false`, the validator requires `required` to include every key in `properties`. Optional fields express optionality via `"type": ["string", "null"]` (or similar) while still being required. The four wrapper schemas under `plugins/plan-executor/scripts/codex_*_schema.json` need to honour this rule recursively at every depth (top-level object, `findings.items`, `tests_run.items`).

**Where the test lives.** `tests/scripts/test_plan_codex_dispatch_schema.py` already exists with a single `test_implement_schema_matches_task_001_report_contract` test. Extend it with a parameterised structural test that walks every schema file in `plugins/plan-executor/scripts/` matching `codex_*_schema.json`, and at every nested object checks the invariant.

**Out of scope.** Refreshing the plugin cache install on consumer machines (e.g. `~/.claude/plugins/cache/...`); that is an operator action, not a source-tree change. Likewise, surfacing the validation as a `plan_ops.py gates` predicate is a follow-up.

## Verification

- `tests/scripts/test_plan_codex_dispatch_schema.py` gains a `test_codex_wrapper_schemas_required_matches_properties` test (or similarly named) that walks `Path(plugins/plan-executor/scripts).glob('codex_*_schema.json')`, loads each as JSON, and at every nested object that declares `additionalProperties: false` asserts `set(node['required']) == set(node['properties'].keys())`. The walk is recursive so `findings.items` and `tests_run.items` are covered, not just the top-level object.
- The test fixture iterates over a discovered list, not a hard-coded set of filenames, so a future fifth schema is covered automatically.
- All four current schemas (`codex_implement_schema.json`, `codex_review_schema.json`, `codex_plan_review_schema.json`, `codex_plan_review_triage_schema.json`) pass the new test as committed.
- The existing `test_implement_schema_matches_task_001_report_contract` continues to pass unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_schema.py` returns 0.

## Tasks

### TASK-001: Schema audit + structural fixture

- **Status:** done
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_codex_dispatch_schema.py` (edit)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_schema.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/codex_implement_schema.json` — full file
  - `plugins/plan-executor/scripts/codex_review_schema.json` — full file
  - `plugins/plan-executor/scripts/codex_plan_review_schema.json` — full file
  - `plugins/plan-executor/scripts/codex_plan_review_triage_schema.json` — full file
- **Acceptance criteria:**
  - New test `test_codex_wrapper_schemas_required_matches_properties` (or similarly named) discovers every `codex_*_schema.json` file under `plugins/plan-executor/scripts/` via glob (no hard-coded filename list) and, for every nested object that declares `additionalProperties: false`, asserts `set(node['required']) == set(node['properties'].keys())`.
  - The walk handles at least: top-level object, every `items` schema under `properties.<arr>.items`, and any further nested objects under `properties.<obj>.properties`. A recursive helper (e.g. `_walk_schema_objects(node)`) is acceptable.
  - The four current schemas pass the new test as committed.
  - **Regression-pin assertion:** the implementer demonstrates in their report that the new test FAILS when run against a transiently-broken schema (e.g. `target_task_id` removed from `findings.items.required` in `codex_plan_review_schema.json`); the assertion message includes the offending file path AND JSON pointer (e.g. `properties.findings.items: required={...} != properties keys={..., target_task_id}`). The transient edit is reverted before commit.
  - `test_implement_schema_matches_task_001_report_contract` keeps passing unchanged.
  - `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_schema.py` returns 0.
- **Reversion guidance:** revert the test additions; no production code paths were touched.

**Description:**
Land a structural regression test that fails the build whenever any wrapper schema's `properties` set drifts away from its `required` array under `additionalProperties: false`. The OpenAI structured-output validator imposes this invariant per nested object, and a one-line oversight (adding a property without `required`) silently breaks the entire Phase 1.5 review path on the next dispatch. The test reads every `codex_*_schema.json` file by glob so future schemas are auto-covered. No production code paths change in this task — the four current schemas already satisfy the invariant on HEAD; the test pins the invariant in place.

**Implementation notes:**
- Use `pathlib.Path.glob('codex_*_schema.json')` against the plugins/plan-executor/scripts directory derived from `Path(__file__).resolve().parents[2]` (matching the existing `REPO_ROOT` constant in the file).
- Recursive walker: at each dict node, if `node.get('type') == 'object'` and `node.get('additionalProperties') is False`, assert `set(node['required']) == set(node['properties'].keys())` with a failure message that includes the JSON pointer path. Recurse into `properties.<k>` and `items` (if present and a dict).
- The test should clearly print the offending file path AND JSON pointer in the assertion message so a regressing change is one-line obvious to fix.
- Do NOT add any helper module under `plugins/plan-executor/scripts/`; the validator is test-only.

## Execution log — 20260425T115138 (success)

Starting SHA: `8a62fc279f1b70368fa5050b2de7938c0aa888ac`  → Ending SHA: `c013da2b5eb3b08d3388bc179a246c0ff159169c`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude (fallback from codex) | codex | clean | 1568f43 | [fallback: shell-backtick command-substitution in test_command — Issue 4] |
