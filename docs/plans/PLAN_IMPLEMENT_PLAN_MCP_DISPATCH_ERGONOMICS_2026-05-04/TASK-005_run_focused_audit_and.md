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

- **Status:** Pending
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
