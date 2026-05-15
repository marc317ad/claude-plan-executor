# SKILL Drift Triage And Repair Plan

**Created:** 2026-05-04
**Status:** pending
**Base branch:** main

## Goal

Restore the green-test baseline of `tests/scripts/test_plan_ops.py` and
`tests/scripts/test_implement_plan_directory_smoke.py` by reconciling
documentation-assertion drift between three surfaces: `SKILL.md`,
`dispatch-templates.md`, and the preflight CLI. Triage each failing
documentation assertion against current `plan_ops.py` behavior and either
restore the lost prose, point the assertion at the canonical MCP schema JSON,
or delete a test that pins a retired protocol.

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

The completed plan should pass these checks:

- `venv/bin/pytest tests/scripts/test_plan_ops.py tests/scripts/test_implement_plan_directory_smoke.py`.
- `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`
  remains advisory-clean (no new failures introduced by the SKILL.md edits).
- `venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --check schema-valid,fixture-valid,execution-safe,review-safe --plan-file plugins/plan-executor/fixtures/sample_phase4.md --json`
  still passes.

## Tasks

## TASK-001: Fix preflight smoke test invocation

- **Status:** pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_implement_plan_directory_smoke.py` (modify) - update `test_preflight_passes_on_clean_directory_copy` to pass `--unattended-revert-policy`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/scripts/test_implement_plan_directory_smoke.py::TestPreflight::test_preflight_passes_on_clean_directory_copy`
- **Acceptance criteria:**
  - `test_preflight_passes_on_clean_directory_copy` passes.
  - The invocation passes a value drawn from the same allowlist `plan_ops.py`
    advertises in its preflight error message: `pause`, `fail-fast`, or
    `preserve-only`. Use `fail-fast` (matches the unattended-execution intent
    of a smoke test).
  - No other test in the file is modified.
- **Reversion guidance:** Revert the test edit.

**Description:**
The preflight CLI now hard-requires `--unattended-revert-policy` so unattended
runs cannot silently discard work on a pause path. The smoke test predates
that requirement and invokes preflight without the flag, producing
`unattended-revert-policy-required` and a non-zero exit. Add the flag with a
value matching the allowlist.

**Implementation notes:**
This is a one-line test edit. Do not modify `plan_ops.py` to make the flag
optional again — the strict requirement is intentional per the existing
universal-invariant guardrails. Confirm the test's other assertions
(directory shape, returncode, etc.) still match by reading them once before
editing.

## TASK-002: Reconcile dispatch-templates.md slot and ladder assertions

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (modify) - restore or update the Phase B narrow-remediation slot block and the triage prioritization ladder if the slot/row contract is still active in `plan_ops.py`
  - `tests/scripts/test_plan_ops.py` (modify) - rewrite or delete the two assertions only if the contract has demonstrably retired
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py::TestPhaseBNarrowRemediationTemplate tests/scripts/test_plan_ops.py::TestTask007TriageRoutingBlockingFirst`
- **Acceptance criteria:**
  - Both `TestPhaseBNarrowRemediationTemplate::test_template_embeds_the_three_json_slots`
    and the two `TestTask007TriageRoutingBlockingFirst` cases pass.
  - For each failing assertion, the implementer's report names which side was
    wrong (template lost the slot/row vs. test pins a retired contract) and
    cites the `plan_ops.py` symbol or schema file that confirms the
    classification (for example `_render_narrow_remediation_template`,
    `_order_triage_findings`, or the relevant MCP schema under
    `plugins/plan-executor/scripts/schemas/`).
  - No new tests are added.
  - No edits to `SKILL.md` (that is TASK-004's surface).
- **Reversion guidance:** Revert the edits to `dispatch-templates.md` and the
  two test classes.

**Description:**
Two failing tests pin protocol-shape patterns inside `dispatch-templates.md`
(Phase B narrow-remediation slot block; Task-007 triage prioritization ladder
including the `blocking=true` row). Each is small but needs a code-grounded
judgment: was the template legitimately reshaped, or did the slim drop a
load-bearing slot? Restore or update the right side accordingly.

**Implementation notes:**
Run each failing test with `-x -v` and read the assertion message. Then grep
the named contract symbol (slot name, row label, helper function) in
`plan_ops.py`, `plan_codex_dispatch.py`, or the schema sidecars to confirm
whether the contract is still active. If active and the template slimmed it
out, restore the prose; if retired, delete the test. Do not pre-empt
TASK-004's classification of `SKILL.md` cases here — TASK-002 covers the two
`dispatch-templates.md` failures only.

## TASK-003: Triage SKILL.md drift failures and produce a per-test ledger

- **Status:** pending
- **Priority:** high
- **Files:** []
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py tests/scripts/test_implement_plan_directory_smoke.py -k "skill_md or SkillMd or Documentation or AuditDocReferences or AutoPromote or ClassifierFanOut or D2a6 or D5Route or D2aBinding or PlanReviewDocumentation or Task007PlanReviewSchema or Task019SkillMdGrep or Task007AuthorPerChildTargeting or SkillRoutingDocumentation"`
- **Acceptance criteria:**
  - Implementer report contains a ledger row for every failing assertion
    under the SKILL.md cluster (~18 rows), each with: `test_node_id`,
    `assertion_text`, `classification` ∈ {`moved-to-schema`, `retired`,
    `restore-prose`, `genuine-loss`}, `token_searched` (the literal string
    grep'd from the assertion), `schema_hits` (list of schema JSON files
    where the token appears, empty if none), `code_hits` (list of
    `plan_ops.py` / wrapper file:line references where the underlying
    protocol is still active, empty if retired), `evidence_path` (the
    primary citation supporting the classification), and `proposed_fix`
    (one of: "rewrite test to grep <schema_path>", "delete test",
    "restore SKILL.md prose: <verbatim line>", or "FLAG genuine-loss; do
    not auto-fix").
  - The ledger MUST include a "no information silently lost" certification:
    every row classified `moved-to-schema` cites a schema JSON that
    actually contains the asserted token (the implementer ran
    `grep -r '<token>' plugins/plan-executor/scripts/schemas/` and pasted
    the matching line); every row classified `retired` cites a `git log -S`
    or `git log --diff-filter=D` reference confirming deliberate removal;
    every row classified `genuine-loss` cites the still-active code path
    that the lost prose was describing.
  - No source files, no test files, and no `SKILL.md` are modified by this
    task.
  - Classification ordering (priority): `moved-to-schema` first, else
    `retired`, else `restore-prose`, else `genuine-loss`. Default to
    `moved-to-schema` whenever the token appears in any schema JSON. Use
    `restore-prose` only when the asserted content is narrative (ladder
    ordering, protocol words, route-table rows) with no schema home AND
    SKILL.md has the surrounding section but lost the specific line. Use
    `genuine-loss` when the protocol is still active in code but the token
    is absent from both schemas and current SKILL.md — this is a refactor
    bug to surface, not silently patch.
  - Test command runs all SKILL.md drift tests and is allowed to report
    failures — this task does not fix them.
- **Reversion guidance:** No changes to revert; ledger lives in the
  implementer report only.

**Description:**
Catalogue every SKILL.md drift failure with a code-grounded classification so
TASK-004 can apply a deterministic fix per row. Read both sides — the failing
assertion in the test file and the asserted span in `SKILL.md` (or its
absence) — then verify against `plan_ops.py` and the MCP schemas under
`plugins/plan-executor/scripts/schemas/mcp/` whether the protocol the
assertion pins is still active.

**Implementation notes:**
Use `git log -p plugins/plan-executor/skills/implement-plan/SKILL.md` and
`git log --diff-filter=D` to identify recent slim commits and confirm what
was deliberately removed versus accidentally lost. The audit subcommand
(`plan_ops.py audit`) is a useful cross-reference for currently load-bearing
protocol items. Do not edit any file in this task — produce the ledger in
the report's `report.ledger[]` array and surface concerns inline.

## TASK-004: Apply SKILL.md drift ledger

- **Status:** pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops.py` (modify) - rewrite assertions to grep schema JSON for `moved-to-schema` rows; delete assertions for `retired` rows
  - `tests/scripts/test_implement_plan_directory_smoke.py` (modify) - same scope as above for the three SKILL.md drift cases under this file
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify) - restore narrative content only for `restore-prose` rows (sparingly — payload shapes belong in schema JSON, not re-inlined here)
- **Dependencies:** [003]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py tests/scripts/test_implement_plan_directory_smoke.py -k "skill_md or SkillMd or Documentation or AuditDocReferences or AutoPromote or ClassifierFanOut or D2a6 or D5Route or D2aBinding or PlanReviewDocumentation or Task007PlanReviewSchema or Task019SkillMdGrep or Task007AuthorPerChildTargeting or SkillRoutingDocumentation"`
- **Acceptance criteria:**
  - Every failing assertion enumerated in TASK-003's ledger that was
    classified `moved-to-schema`, `retired`, or `restore-prose` now passes
    or has been deleted per the ledger.
  - For `genuine-loss` rows: the test stays failing AND the implementer
    surfaces each row in `concerns_for_reviewer[]` with the still-active
    code path. Do NOT silently restore the prose or invent a schema entry —
    the user adjudicates these.
  - Edits follow the ledger row's `proposed_fix` field verbatim; deviations
    are listed in `plan_adaptations[]` with a one-line rationale.
  - `dispatch-templates.md` is NOT touched by this task (TASK-002's surface).
  - `plan_ops.py` and the MCP schemas under
    `plugins/plan-executor/scripts/schemas/` are NOT modified.
  - The `plan_ops.py audit` subcommand still reports advisory-clean after
    these edits (no new findings introduced).
  - For every test deletion, the implementer cites the corresponding ledger
    row's `retired` evidence path in `concerns_for_reviewer[]`.
- **Reversion guidance:** Revert `SKILL.md`, `tests/scripts/test_plan_ops.py`,
  and `tests/scripts/test_implement_plan_directory_smoke.py`.

**Description:**
Apply the per-row classification produced by TASK-003. For `moved-to-schema`
rows, rewrite the test to grep the cited schema JSON instead of `SKILL.md`.
For `retired` rows, delete the test. For `restore-prose` rows (expected to
be the minority), restore the narrative line to `SKILL.md`. The refactor's
intent is that `SKILL.md` does not duplicate payload shapes that already
live in MCP schema JSON; do not re-inline payload structures.

**Implementation notes:**
The bulk of edits should land on the test files, not `SKILL.md`. Run the
verification test command after each cluster of edits to verify progress
incrementally. Use the implementer report's `summary` to enumerate which
ledger rows were applied and which (if any) needed adaptation away from the
proposed_fix. If a `moved-to-schema` row's schema JSON does not actually
contain the asserted token (ledger error), surface in
`concerns_for_reviewer[]` and reclassify in the report rather than silently
restoring prose.

## TASK-005: Verify full regression suite green

- **Status:** pending
- **Priority:** high
- **Files:** []
- **Dependencies:** [001, 002, 004]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py tests/scripts/test_implement_plan_directory_smoke.py`
- **Acceptance criteria:**
  - `tests/scripts/test_plan_ops.py` passes with zero failures EXCEPT any
    rows TASK-003 classified `genuine-loss`. Such rows remain failing by
    design and are enumerated in the implementer's report under
    `concerns_for_reviewer[]` with their still-active code paths, copied
    forward from TASK-004's report.
  - `tests/scripts/test_implement_plan_directory_smoke.py` passes with zero
    failures except `genuine-loss` carve-outs, same rule as above.
  - The implementer report includes a `genuine_loss_summary[]` listing each
    such row's `test_node_id`, `token_searched`, and `code_hits` so the
    user has the adjudication queue in one place.
  - `plan_ops.py audit --json` reports advisory-clean (no findings introduced
    by this plan; pre-existing audit findings, if any, must be the same set
    as before this plan started).
  - `plan_ops.py gates --check schema-valid,fixture-valid,execution-safe,review-safe --plan-file plugins/plan-executor/fixtures/sample_phase4.md --json`
    passes.
  - No new tests are added unless a concrete behavior gap surfaces during
    verification (in which case the implementer report MUST cite the gap).
- **Reversion guidance:** No edits expected; revert any verification-only
  test additions if they were necessary.

**Description:**
End-to-end verification that TASK-001, TASK-002, and TASK-004 collectively
restored the green baseline without introducing new drift. The implementer
should run the full pytest commands listed and capture the output tail.

**Implementation notes:**
Treat this as a verification-only task. If the full pytest command surfaces a
failure that is NOT in the original 21-failure baseline, halt and surface
the regression in `concerns_for_reviewer[]` rather than adding a quick-fix
test. The plan's scope is reconciliation, not extension.
