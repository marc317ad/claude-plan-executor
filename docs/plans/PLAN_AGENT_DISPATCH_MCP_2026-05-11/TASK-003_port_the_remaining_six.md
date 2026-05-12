# TASK-003 — Port the remaining six template variants

## Goal

Port the remaining six template variants

## Context

Extend the MCP tool added in TASK-001 with the remaining eight `template_id` values covering Phase D.5, Phase 1.5-Claude, Phase 1.5a (three sub-variants), Phase 1-triage / 1.5.5 (one variant with `source` discrimination), Phase D.2a.6 narrow remediation, and Phase D.4-rescue. Each variant gets a per-context schema branch (`oneOf` on `template_id`), a golden test asserting byte-equality to a fixture rendering, and negative tests for missing required fields. No SKILL changes here — TASK-004 cuts each phase over.

## Verification

- The eight new `template_id` values are added to the input schema enum: `code-reviewer-d5`, `plan-reviewer`, `plan-author-task-targeted`, `plan-author-schedule-level`, `plan-author-legacy-whole-plan`, `plan-review-triage`, `plan-remediator-narrow`, `plan-remediator-rescue`. Each gets a corresponding `oneOf` branch in the `context` schema documenting its required fields.
- For each variant, a golden test asserts byte-equality between the rendered prompt and the fixture `golden_prompt.txt`. Per-variant fixtures cover both the single-H3-heading and >1-H3-heading cases where the variant supports `target_task_id` auto-injection (`code-reviewer-d5`, `plan-author-task-targeted`, `plan-remediator-narrow`, `plan-remediator-rescue` per the same §`target_task_id` rule documented in dispatch-templates.md). Variants that do NOT take `target_task_id` (`plan-reviewer`, `plan-author-schedule-level`, `plan-author-legacy-whole-plan`, `plan-review-triage`) get one fixture each.
- Negative tests per variant: missing required context fields surface as `{ok: false, errors: [{code: "<specific>", path: "<json pointer>", ...}]}`. Each variant has its own required-field set (e.g., `plan-review-triage` requires `source`, `findings`, `gaps`; `code-reviewer-d5` requires `wrapper_checks_json`, `reviewer_findings`; `plan-author-task-targeted` requires `child_plan_file`, `finding`).
- `plan-review-triage` source discrimination: the `source` field accepts `analyst | codex`. The render swaps the embedded evidence label, the verification-move examples, and the analyst-only same-family caveat per dispatch-templates.md:314. The golden fixture covers both source values.
- `plan-author` legacy-whole-plan variant exists explicitly even though most callers use task-targeted or schedule-level. The legacy CLI path still calls it (per `plan-author.md` agent doc); deprecation is a separate plan.
- The Phase D.5 variant accepts a `wrapper_checks_json` placeholder that may default to `{"symbol_warnings": []}` per the dispatch-templates.md:607 wrapper-checks-asymmetry note. The schema documents this default.
- **No SKILL or dispatch-templates.md edits in this task.** Cutover for the eight variants is TASK-004. The new variants sit unused after this task lands.
- `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0 (golden + negative coverage for all nine variants).

## Tasks

### TASK-003: Port the remaining six template variants

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — extend `_AGENT_DISPATCH_TEMPLATES` dict and per-variant render helpers)
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (edit — widen `template_id` enum and add `oneOf` branches for each new context shape)
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` (edit — add per-variant golden tests + negative tests)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d5/` (create — golden_prompt.txt, plan_file.md, context.json)
  - `tests/fixtures/agent_dispatch_prompt/plan-reviewer/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-author-task-targeted/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-author-schedule-level/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-author-legacy-whole-plan/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-review-triage/` (create — with `source: analyst` and `source: codex` sub-fixtures)
  - `tests/fixtures/agent_dispatch_prompt/plan-remediator-narrow/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-remediator-rescue/` (create)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Acceptance criteria:**
  - The eight new `template_id` values are added to the input schema enum: `code-reviewer-d5`, `plan-reviewer`, `plan-author-task-targeted`, `plan-author-schedule-level`, `plan-author-legacy-whole-plan`, `plan-review-triage`, `plan-remediator-narrow`, `plan-remediator-rescue`. Each gets a corresponding `oneOf` branch in the `context` schema documenting its required fields.
  - For each variant, a golden test asserts byte-equality between the rendered prompt and the fixture `golden_prompt.txt`. Per-variant fixtures cover both the single-H3-heading and >1-H3-heading cases where the variant supports `target_task_id` auto-injection (`code-reviewer-d5`, `plan-author-task-targeted`, `plan-remediator-narrow`, `plan-remediator-rescue` per the same §`target_task_id` rule documented in dispatch-templates.md). Variants that do NOT take `target_task_id` (`plan-reviewer`, `plan-author-schedule-level`, `plan-author-legacy-whole-plan`, `plan-review-triage`) get one fixture each.
  - Negative tests per variant: missing required context fields surface as `{ok: false, errors: [{code: "<specific>", path: "<json pointer>", ...}]}`. Each variant has its own required-field set (e.g., `plan-review-triage` requires `source`, `findings`, `gaps`; `code-reviewer-d5` requires `wrapper_checks_json`, `reviewer_findings`; `plan-author-task-targeted` requires `child_plan_file`, `finding`).
  - `plan-review-triage` source discrimination: the `source` field accepts `analyst | codex`. The render swaps the embedded evidence label, the verification-move examples, and the analyst-only same-family caveat per dispatch-templates.md:314. The golden fixture covers both source values.
  - `plan-author` legacy-whole-plan variant exists explicitly even though most callers use task-targeted or schedule-level. The legacy CLI path still calls it (per `plan-author.md` agent doc); deprecation is a separate plan.
  - The Phase D.5 variant accepts a `wrapper_checks_json` placeholder that may default to `{"symbol_warnings": []}` per the dispatch-templates.md:607 wrapper-checks-asymmetry note. The schema documents this default.
  - **No SKILL or dispatch-templates.md edits in this task.** Cutover for the eight variants is TASK-004. The new variants sit unused after this task lands.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0 (golden + negative coverage for all nine variants).
- **Reversion guidance:** revert the schema enum widening, the per-variant render helpers, and the new fixture directories. The TASK-001 `code-reviewer-d-claude` variant remains intact and the TASK-002 cutover continues to function.

**Description:**
Extend the MCP tool added in TASK-001 with the remaining eight `template_id` values covering Phase D.5, Phase 1.5-Claude, Phase 1.5a (three sub-variants), Phase 1-triage / 1.5.5 (one variant with `source` discrimination), Phase D.2a.6 narrow remediation, and Phase D.4-rescue. Each variant gets a per-context schema branch (`oneOf` on `template_id`), a golden test asserting byte-equality to a fixture rendering, and negative tests for missing required fields. No SKILL changes here — TASK-004 cuts each phase over.
