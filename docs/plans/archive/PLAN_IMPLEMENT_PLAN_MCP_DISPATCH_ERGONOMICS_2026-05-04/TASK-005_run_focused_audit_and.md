# TASK-005 — Run focused audit and document operator guidance

## Goal

Run focused audit and document operator guidance

## Context

Auto-decomposed child for TASK-005. See the source plan for broader context.

## Verification

- Run `plan_ops__audit` after the skill/tool changes.
- Record whether any audit finding remains advisory or blocking.
- Add a numbered operator rule in the skill: in MCP mode, do not switch the whole run to CLI fallback because a builder result cannot be piped; materialize the dispatch input, run the Bash wrapper with `--input <file>`, then continue with MCP for extraction and later plan operations.

## Tasks

### TASK-005: Run focused audit and document operator guidance

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `docs/plans/PLAN_IMPLEMENT_PLAN_MCP_DISPATCH_ERGONOMICS_2026-05-04.md`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_implement_plan_mcp_e2e.py && venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`
- **Acceptance criteria:**
  - Run `plan_ops__audit` after the skill/tool changes.
  - Record whether any audit finding remains advisory or blocking.
  - Add a numbered operator rule in the skill: in MCP mode, do not switch the whole run to CLI fallback because a builder result cannot be piped; materialize the dispatch input, run the Bash wrapper with `--input <file>`, then continue with MCP for extraction and later plan operations.
- **Reversion guidance:** none

**Description:**

Run `plan_ops__audit` after the TASK-001…004 skill/tool changes land, capture whether any check is advisory vs blocking, and add a numbered operator rule to `SKILL.md` documenting the MCP-mode dispatch-input materialization workaround so operators do not flip the entire run to CLI fallback when a builder result cannot be piped.

## Execution log — 20260505T030128 (success)

Starting SHA: `7acafc671bf68705fb4cd7e1bfc11722ba66a94e`  → Ending SHA: `5ea50109815f3b171c8678c56e5eda9224e48416`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 005 | codex | claude | ship-with-fixes | 5ea5010 | Pre-dispatch format-only edit added missing **Description:** to satisfy build-tasks schema (per §Rules narrow plan-file body edits). 1 minor finding; rule-4 wording could cross-reference §Claude wrapper dispatch recipe to disambiguate MCP `output:` arg vs CLI `--output` flag. |
