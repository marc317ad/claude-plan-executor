# TASK-002 — Persist resolved agents in the Python runner path

## Goal

Persist resolved agents in the Python runner path

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- `PlanOpsFacade` exposes a narrow `set_task_agent(**payload)` method that delegates to direct plan_ops execution, matching the existing `build_tasks` / `write_schedule` style.
- After assignment resolution and before schedule write, the runner persists resolved per-task implementer agents to child plan files when: the task has a resolvable child `plan_file`; the resolved implementer provider is `claude` or `codex`; the existing task metadata lacked `agent` or differs from the resolved provider.
- The persistence runs under `--dry-run`, matching the SKILL dry-run exemption.
- Explicit assignment overrides are handled deliberately: either persist the explicit assignment to the child file, making the next run deterministic without the flag; or do not persist explicit overrides and document that decision in a test name/comment.
- Runner tests cover: a dry-run on a child without `**Agent:**` writes the bullet; a second dry-run observes the persisted bullet and does not need to re-resolve from missing task metadata; schedule `tasks[*].agent` matches the persisted child-file value after the run.

## Tasks

### TASK-002: Persist resolved agents in the Python runner path

- **Status:** Pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_runner_e2e.py` or closest focused runner test file
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_e2e.py -k "agent and dry_run"`
- **Acceptance criteria:**
  - `PlanOpsFacade` exposes a narrow `set_task_agent(**payload)` method that delegates to direct plan_ops execution, matching the existing `build_tasks` / `write_schedule` style.
  - After assignment resolution and before schedule write, the runner persists resolved per-task implementer agents to child plan files when: the task has a resolvable child `plan_file`; the resolved implementer provider is `claude` or `codex`; the existing task metadata lacked `agent` or differs from the resolved provider.
  - The persistence runs under `--dry-run`, matching the SKILL dry-run exemption.
  - Explicit assignment overrides are handled deliberately: either persist the explicit assignment to the child file, making the next run deterministic without the flag; or do not persist explicit overrides and document that decision in a test name/comment.
  - Runner tests cover: a dry-run on a child without `**Agent:**` writes the bullet; a second dry-run observes the persisted bullet and does not need to re-resolve from missing task metadata; schedule `tasks[*].agent` matches the persisted child-file value after the run.
- **Reversion guidance:** Remove the facade method, remove the post-assignment persistence call, and delete the runner tests added in this task. TASK-001 remains valid independently.

**Description:**
Persist resolved agents in the Python runner path. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-002. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
