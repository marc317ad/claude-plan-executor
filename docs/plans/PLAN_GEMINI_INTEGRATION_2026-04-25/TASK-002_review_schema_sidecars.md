# TASK-002 — `gemini_*_schema.json` review + plan-review sidecars

## Goal

Author two new JSON-schema sidecars at `plugins/plan-executor/scripts/gemini_review_schema.json` and `plugins/plan-executor/scripts/gemini_plan_review_schema.json`, structurally mirroring the existing `codex_review_schema.json` and `codex_plan_review_schema.json` so downstream parsers (and the orchestrator's verdict-routing tables) do not need to discriminate by reviewer family.

## Context

`plan_codex_dispatch.py` ships two sidecar schemas that `parse-plan-review-report` and the cross-review parser bind to. The verdict vocabulary, `findings[]` shape, and envelope keys are the contract surface every downstream consumer (run-log writers, summary renderers, D.5 dispatchers) reads against. To keep that contract reviewer-agnostic, the Gemini wrapper must emit envelopes whose `parsed` field validates against schemas with **identical structural shape** to the Codex schemas — same field names, same enum values, same `required` arrays.

The shape is fixed by:

- `plugins/plan-executor/scripts/codex_review_schema.json` — `{task_id, verdict ∈ {clean, minor-findings, needs-rework}, findings[], notes[], scope_ok, acceptance_met, summary}`. Each finding carries `{severity, confidence, file, line, issue, suggested_fix}`.
- `plugins/plan-executor/scripts/codex_plan_review_schema.json` — `{plan_file, verdict ∈ {approved, approved-with-notes, needs-replan}, findings[], notes[], schedule_ok, summary}`. Each finding carries `{severity, blocking, section, concern, suggested_change, target_task_id}`.

**The mirror MUST be structural, not literal.** A literal copy is fine for v1 — there is no Gemini-specific field today — but the schemas live in their own files so future divergence (e.g. a Gemini-specific `tool_calls[]` block) does not require editing the Codex schemas.

The schemas are post-hoc validators because, per TASK-001's matrix, Gemini has no `--output-schema PATH` enforcement equivalent. The wrapper embeds the schema in the prompt for in-band guidance, then validates the parsed `response` field against the on-disk schema after Gemini returns. Schema-validation retry logic is TASK-003's scope; this task only authors the schemas.

The OpenAI strict-validator invariant from `CODEX_FRICTION_2026-04-25/TASK-001` (every nested object that declares `additionalProperties: false` has `required == set(properties.keys())`) MUST hold for these schemas too. TASK-008 of this plan extends the audit fixture to walk `gemini_*_schema.json` under the same invariant, so getting the schemas right at authoring time is cheap insurance.

**Out of scope.** Wrapper code (TASK-003 / TASK-004); audit-fixture extension (TASK-008); preflight (TASK-005).

## Verification

- New file `plugins/plan-executor/scripts/gemini_review_schema.json` exists with structural shape identical to `codex_review_schema.json` (top-level `required`, properties, nested `findings.items` shape).
- New file `plugins/plan-executor/scripts/gemini_plan_review_schema.json` exists with structural shape identical to `codex_plan_review_schema.json`.
- Both files load cleanly with `json.load`.
- For both files, every nested object that declares `additionalProperties: false` satisfies `set(node["required"]) == set(node["properties"].keys())` recursively.
- A new test at `tests/scripts/test_gemini_schemas_mirror_codex.py` parameterizes over the two pairs and asserts:
  - top-level `required` arrays are equal as sets;
  - top-level `properties` keys are equal as sets;
  - the `verdict` enum values are equal as sets;
  - the `findings.items.properties` keys are equal as sets;
  - the `findings.items.required` arrays are equal as sets.
- `venv/bin/pytest -q tests/scripts/test_gemini_schemas_mirror_codex.py` returns 0.

## Tasks

### TASK-002: `gemini_*_schema.json` review + plan-review sidecars

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/gemini_review_schema.json` (new)
  - `plugins/plan-executor/scripts/gemini_plan_review_schema.json` (new)
  - `tests/scripts/test_gemini_schemas_mirror_codex.py` (new)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_gemini_schemas_mirror_codex.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/codex_review_schema.json` — full file
  - `plugins/plan-executor/scripts/codex_plan_review_schema.json` — full file
- **Acceptance criteria:**
  - Both new schema files load as JSON without error.
  - Both new schema files satisfy the OpenAI-strict invariant (`additionalProperties: false ⟹ required == properties.keys()` at every nested object).
  - The mirror test passes — the Gemini schemas are structural mirrors of the Codex schemas.
  - The existing CODEX_FRICTION schema-audit test (`tests/scripts/test_plan_codex_dispatch_schema.py`) is NOT modified by this task; TASK-008 is the audit-extension task.
- **Reversion guidance:** revert the new files; no other modules import them yet.

**Description:**
Two new JSON-schema sidecars that mirror the Codex equivalents. The wrapper's structured-output contract is the schemas, so they need to land before the wrapper module is built. Mirror discipline (same field names, same enums, same `required` arrays) keeps downstream parsers reviewer-agnostic — the run-log, the summary renderer, the verdict-routing tables in SKILL.md all stay one code path regardless of reviewer family.

**Implementation notes:**
- Start by copying `codex_review_schema.json` to `gemini_review_schema.json` byte-for-byte, then verify the OpenAI-strict invariant. Same for the plan-review pair.
- Both schemas use `additionalProperties: false` at the top level AND on nested `findings.items` — the recursive walker in TASK-008 will visit both depths.
- Do NOT add Gemini-specific fields in v1. Future divergence is a separate task; this one mirrors.
- The mirror test reads both files via `json.load` and compares `set(top["required"])`, `set(top["properties"].keys())`, `set(top["properties"]["verdict"]["enum"])`, `set(top["properties"]["findings"]["items"]["properties"].keys())`, `set(top["properties"]["findings"]["items"]["required"])`. A single test function per (review, plan-review) pair with `pytest.mark.parametrize` is fine.
