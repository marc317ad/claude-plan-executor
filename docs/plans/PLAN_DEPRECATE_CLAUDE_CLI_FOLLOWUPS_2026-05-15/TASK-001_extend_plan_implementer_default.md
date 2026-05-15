# TASK-001 — Extend `plan-implementer-default` renderer + schema to carry optional D.2a.5 rework context, and re-wire SKILL.md's D.2a.5 routing

## Goal

Extend `plan-implementer-default` renderer + schema to carry optional D.2a.5 rework context, and re-wire SKILL.md's D.2a.5 routing

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- Extend the `planImplementerDefaultContext` $def in `schemas/mcp/build_agent_dispatch_prompt.input.json` with three optional fields: `prior_findings` (array of `{severity, file, line, issue, suggested_fix}` objects, matching the reviewer-finding shape already used by `plan_ops__parse_reviewer_envelope`), `prior_summary` (string), and `attempt_count` (integer, ≥1). All three are optional; absence means "fresh implement" and the renderer's output is byte-identical to today.
- Extend the `plan-implementer-default` renderer branch in `plan_ops.py:12747` (`if template_id == "plan-implementer-default":`) to read the optional fields from the context dict. When `prior_findings` is non-empty OR `prior_summary` is non-empty, inline a clearly delimited "Prior attempt — D.2a.5 bounded remediation context" section above the existing fresh-implement instructions. The section enumerates each finding (severity, file:line, issue, suggested_fix) and emits the summary verbatim. When all three are absent, the rendered prompt is byte-identical to the pre-change output (regression-pinned in tests).
- Add a conditional block to the Phase B template body in `dispatch-templates.md` (the same heading anchor used by `_AGENT_DISPATCH_TEMPLATE_HEADING["plan-implementer-default"]` at `plan_ops.py:12201`). Use the renderer's existing context-substitution machinery — do not duplicate finding-formatting logic in the markdown.
- Rewrite SKILL.md:557 (`dispatch_bounded_remediation`) and SKILL.md:575 (`### Phase D.2a.5 — bounded remediation`) so they explicitly state: D.2a.5 dispatches `plan-implementer-default` with `prior_findings`, `prior_summary`, and `attempt_count` populated in the `planImplementerDefaultContext` payload. Cross-link the §Cleanup-around-Agent-dispatch sub-recipe — D.2a.5 is file-editing, so the cleanup wrap applies with `authorization_source="orchestrator-declared-scope"`.
- Add two test cases in `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`:
- Schema validation: `venv/bin/python -m jsonschema -i tests/scripts/fixtures/build_agent_dispatch_prompt/implementer_default_with_rework_context.json plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (or equivalent) must pass — add the fixture alongside the test.

## Tasks

### TASK-001: Extend `plan-implementer-default` renderer + schema to carry optional D.2a.5 rework context, and re-wire SKILL.md's D.2a.5 routing

- **Status:** Pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py -k "implementer_default or rework"`
- **Acceptance criteria:**
  - Extend the `planImplementerDefaultContext` $def in `schemas/mcp/build_agent_dispatch_prompt.input.json` with three optional fields: `prior_findings` (array of `{severity, file, line, issue, suggested_fix}` objects, matching the reviewer-finding shape already used by `plan_ops__parse_reviewer_envelope`), `prior_summary` (string), and `attempt_count` (integer, ≥1). All three are optional; absence means "fresh implement" and the renderer's output is byte-identical to today.
  - Extend the `plan-implementer-default` renderer branch in `plan_ops.py:12747` (`if template_id == "plan-implementer-default":`) to read the optional fields from the context dict. When `prior_findings` is non-empty OR `prior_summary` is non-empty, inline a clearly delimited "Prior attempt — D.2a.5 bounded remediation context" section above the existing fresh-implement instructions. The section enumerates each finding (severity, file:line, issue, suggested_fix) and emits the summary verbatim. When all three are absent, the rendered prompt is byte-identical to the pre-change output (regression-pinned in tests).
  - Add a conditional block to the Phase B template body in `dispatch-templates.md` (the same heading anchor used by `_AGENT_DISPATCH_TEMPLATE_HEADING["plan-implementer-default"]` at `plan_ops.py:12201`). Use the renderer's existing context-substitution machinery — do not duplicate finding-formatting logic in the markdown.
  - Rewrite SKILL.md:557 (`dispatch_bounded_remediation`) and SKILL.md:575 (`### Phase D.2a.5 — bounded remediation`) so they explicitly state: D.2a.5 dispatches `plan-implementer-default` with `prior_findings`, `prior_summary`, and `attempt_count` populated in the `planImplementerDefaultContext` payload. Cross-link the §Cleanup-around-Agent-dispatch sub-recipe — D.2a.5 is file-editing, so the cleanup wrap applies with `authorization_source="orchestrator-declared-scope"`.
  - Add two test cases in `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`:
  - Schema validation: `venv/bin/python -m jsonschema -i tests/scripts/fixtures/build_agent_dispatch_prompt/implementer_default_with_rework_context.json plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (or equivalent) must pass — add the fixture alongside the test.
- **Reversion guidance:** Drop the three new fields from the $def, revert the renderer branch to its current shape (the `if template_id == "plan-implementer-default":` block stays, but the rework-context branch is removed), revert SKILL.md:557 and :575 to the current text, drop the conditional block in dispatch-templates.md, delete the two new test cases and the fixture file.

**Description:**
Extend `plan-implementer-default` renderer + schema to carry optional D.2a.5 rework context, and re-wire SKILL.md's D.2a.5 routing. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-001. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
