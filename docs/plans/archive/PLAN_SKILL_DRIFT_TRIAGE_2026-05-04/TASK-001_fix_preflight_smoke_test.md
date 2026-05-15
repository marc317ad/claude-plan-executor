# TASK-001 — Fix preflight smoke test invocation

## Goal

Fix preflight smoke test invocation

## Context

The TASK-005 verification step of `PLAN_DECOMPOSED_TEMPLATE_REFACTOR_2026-05-04`
surfaced 21 pre-existing failures in the regression suite, untouched by that
plan. They cluster into three groups:

1. **Preflight CLI hard-flag (1 failure).**
   `tests/scripts/test_implement_plan_directory_smoke.py::TestPreflight::test_preflight_passes_on_clean_directory_copy`
   invokes `plan_ops.py preflight` without `--unattended-revert-policy`, which
   `plan_ops.py` now requires (validation error
   `unattended-revert-policy-required`). One-line fix in the test invocation.

2. **`dispatch-templates.md` slot drift (2 failures).**
   `TestTask007TriageRoutingBlockingFirst::test_triage_template_documents_prioritization_ladder`
   and
   `TestPhaseBNarrowRemediationTemplate::test_template_embeds_the_three_json_slots`
   pin specific JSON slot patterns and a triage prioritization ladder inside
   `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`. Each
   needs a per-test judgment: did the template legitimately drop the slot, or
   did the slot get renamed / restructured?

3. **`SKILL.md` content drift (~18 failures).** `SKILL.md` was deliberately
   refactored to point at the canonical MCP schema JSON files under
   `plugins/plan-executor/scripts/schemas/mcp/*.input.json` and
   `*.output.json` for payload shapes, instead of inlining them in prose.
   This was a context-saving design move and is the intended direction. The
   refactor removed protocol-pinning strings the existing tests assert on
   because those tests were authored against the older prose-heavy SKILL.md.
   Each failure requires classification AND a no-information-lost check:
   - **(a) moved-to-schema:** the asserted shape (field name, enum value,
     JSON key, payload structure) now lives in an MCP schema JSON. The
     ledger row MUST cite the specific schema file path AND confirm the
     asserted token appears in that schema (no silent drop). Rewrite the
     test to grep the schema JSON. This is the default for any assertion
     that pins a payload-shape pattern.
   - **(b) retired:** the asserted protocol no longer exists in `plan_ops.py`
     or the wrapper scripts at all. The ledger row MUST cite a
     `git log -S<token>` reference confirming the token was deliberately
     removed (not just renamed). Delete the test.
   - **(c) restore-prose:** the asserted content is genuinely narrative
     (protocol words, ladder ordering, route-table rows, invariants) with no
     JSON home, AND the SKILL.md line was lost incidentally rather than
     deliberately. Restore the line. Use sparingly — re-inlining payload
     shapes here would undo the refactor.
   - **(d) genuine-loss:** the asserted protocol IS still load-bearing in
     `plan_ops.py` or the wrappers, but the token is NOT present in any
     schema JSON AND NOT present in current SKILL.md. The information was
     dropped during the refactor without a home. The ledger row MUST flag
     this as a refactor-bug; TASK-004 will surface it in
     `concerns_for_reviewer[]` rather than silently fixing it (the user
     decides whether to add to a schema, restore to SKILL.md, or accept the
     gap).

The classification must be code-grounded: confirm against `plan_ops.py`,
`_plan_paths.py`, the wrapper scripts, or the schema sidecars under
`plugins/plan-executor/scripts/schemas/`. Do not classify by "what the test
expects feels reasonable." The ordering above is also the priority order:
default to `moved-to-schema` if the token is in any schema; else check
`retired`; else check `restore-prose`; else flag as `genuine-loss`. The
refactor's intent is that SKILL.md not duplicate what the schemas already
pin, so re-inlining is a last resort.

The full failing-test inventory at run start (run 20260504T123438):

```
tests/scripts/test_plan_ops.py::TestPlanReviewDocumentation::test_skill_md_has_phase_1_5_section
tests/scripts/test_plan_ops.py::TestPlanReviewDocumentation::test_phase_1_5_inserted_before_dry_run_mode
tests/scripts/test_plan_ops.py::TestTask007PlanReviewSchemaTargetTaskIdOptional::test_schema_target_task_id_not_in_required
tests/scripts/test_plan_ops.py::TestTask007TriageRoutingBlockingFirst::test_triage_template_documents_prioritization_ladder
tests/scripts/test_plan_ops.py::TestTask007TriageRoutingBlockingFirst::test_triage_dispatch_uses_source_index_and_ordering_helper
tests/scripts/test_plan_ops.py::TestTask007AuthorPerChildTargeting::test_skill_md_documents_per_child_author_fanout
tests/scripts/test_plan_ops.py::TestD5RouteTableDocumentation::test_route_table_has_partial_agreement_row
tests/scripts/test_plan_ops.py::TestPhaseBNarrowRemediationTemplate::test_template_embeds_the_three_json_slots
tests/scripts/test_plan_ops.py::TestD2a6SkillMdSection (5 sub-cases)
tests/scripts/test_plan_ops.py::TestD2aBindingModeContract::test_d2a_routing_prose_uses_post_binding_block_stage
tests/scripts/test_plan_ops.py::TestTask019SkillMdGrepRegressions::test_skill_md_documents_d2a_reviewer_flip_and_row_schema
tests/scripts/test_plan_ops.py::TestAuditDocReferences::test_skill_md_references_audit_subcommand
tests/scripts/test_plan_ops.py::TestSkillAutoPromoteBootstrapInterpreter::test_auto_promote_uses_python3_not_pinned
tests/scripts/test_plan_ops.py::TestSkillRoutingDocumentation::test_skill_routing_documentation_lists_codex_review_reasons
tests/scripts/test_implement_plan_directory_smoke.py::TestPreflight::test_preflight_passes_on_clean_directory_copy
tests/scripts/test_implement_plan_directory_smoke.py::TestClassifierFanOut::test_skill_md_pins_n_discrete_fan_out_language
tests/scripts/test_implement_plan_directory_smoke.py::TestSingleFileAutoPromote::test_skill_md_pins_auto_promote_phase_0_step
```

## Verification

- `test_preflight_passes_on_clean_directory_copy` passes.
- The invocation passes a value drawn from the same allowlist `plan_ops.py` advertises in its preflight error message: `pause`, `fail-fast`, or `preserve-only`. Use `fail-fast` (matches the unattended-execution intent of a smoke test).
- No other test in the file is modified.

## Tasks

### TASK-001: Fix preflight smoke test invocation

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `tests/scripts/test_implement_plan_directory_smoke.py` (modify) - update `test_preflight_passes_on_clean_directory_copy` to pass `--unattended-revert-policy`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/scripts/test_implement_plan_directory_smoke.py::TestPreflight::test_preflight_passes_on_clean_directory_copy`
- **Acceptance criteria:**
  - `test_preflight_passes_on_clean_directory_copy` passes.
  - The invocation passes a value drawn from the same allowlist `plan_ops.py` advertises in its preflight error message: `pause`, `fail-fast`, or `preserve-only`. Use `fail-fast` (matches the unattended-execution intent of a smoke test).
  - No other test in the file is modified.
- **Reversion guidance:** Revert the test edit.

**Description:**
The preflight CLI now hard-requires `--unattended-revert-policy` so unattended
runs cannot silently discard work on a pause path. The smoke test predates
that requirement and invokes preflight without the flag, producing
`unattended-revert-policy-required` and a non-zero exit. Add the flag with a
value matching the allowlist.
