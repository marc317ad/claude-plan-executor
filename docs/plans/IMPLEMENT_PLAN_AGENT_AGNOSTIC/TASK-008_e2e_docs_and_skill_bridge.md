# TASK-008: E2E, Documentation, And SKILL Bridge

## Goal

Add end-to-end coverage, update docs, and make the Claude SKILL able to delegate to `implement_plan.py` when requested while preserving the current MCP-driven manual orchestration path.

## Context

The runner is intended to make the system agent-agnostic, not to remove Claude Code support or demote MCP. The final task proves that another agent can execute the workflow by running a script, while Claude and Codex can still use MCP tools directly when `plan_ops__*` is installed in their sessions.

## Scoped Context

This task validates the integrated runner. It should avoid broad rewrites of SKILL prose beyond adding a bridge section and references.

### TASK-008: E2E, Documentation, And SKILL Bridge

- **Status:** pending
- **Priority:** high
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `README.md`
  - `tests/scripts/test_implement_plan_runner_e2e.py` (new)
  - `tests/scripts/test_skill_cli_reference_drift.py`
- **Dependencies:** TASK-006, TASK-007
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_e2e.py tests/scripts/test_skill_cli_reference_drift.py`
- **Description:** Prove the integrated script runner with stub providers, document the new entry point, and add a small SKILL bridge for users who explicitly ask for script execution.
- **Acceptance criteria:**
  - E2E test runs a multi-task decomposed plan through the runner with `StubProvider` and asserts run-log event order through Phase 1.5, Phase B, Phase D, commits/fails, and finalization.
  - E2E test includes one pause/resume path.
  - E2E test includes explicit assignment arguments and auto assignment through provider preference.
  - README documents the script entry point, provider capability model, config file shape, and when to use MCP, the SKILL, or the script.
  - SKILL gains a short section: when the user asks to run the script runner, invoke `implement_plan.py` with the user's arguments instead of manually walking the MCP orchestration.
  - Slash command behavior remains backward-compatible; `/implement-plan` still invokes the SKILL unless explicitly changed by a future plan. Do not reference or edit `plugins/plan-executor/commands/implement-plan.md` unless that file exists by the time this task is implemented.
  - Drift tests assert SKILL references the runner command and does not claim the script replaces MCP tools or should be preferred over MCP in interactive Claude/Codex sessions where `plan_ops__*` tools are available.
  - Manual smoke command from `PLAN_IMPLEMENT_PLAN_AGENT_AGNOSTIC.md` exits successfully in dry-run/stub mode.

**Description:**
Prove the integrated script runner with stub providers, document the new entry
point, and add a small SKILL bridge for users who explicitly ask for script
execution.

## Verification

Run the task test command and the manual smoke from the parent plan.

## Non-goals

- Making the script the default slash-command implementation.
- Live paid model e2e in CI.
- Removing any MCP tool or wrapper CLI.
- Changing the preferred interactive path away from MCP when `plan_ops__*` tools are available.
