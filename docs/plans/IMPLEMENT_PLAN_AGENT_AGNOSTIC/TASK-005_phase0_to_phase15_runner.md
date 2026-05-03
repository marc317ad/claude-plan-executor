# TASK-005: Phase 0 Through Phase 1.5 Runner

## Goal

Implement the deterministic front half of the runner: preflight, decomposition, gates, task synthesis, assignment, schedule write, and plan review via `plan-review-route`.

## Context

Phase 1.5 is now a Python state machine. The runner should call it directly rather than reusing the old SKILL prose ladder.

## Scoped Context

This task ends before implementation batches. It may dispatch plan-review providers and plan-author/triage through adapters only when a test fixture requests those paths.

### TASK-005: Phase 0 Through Phase 1.5 Runner

- **Status:** pending
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_runner_phase15.py` (new)
  - `tests/scripts/fixtures/implement_plan_runner/phase15/` (new fixture directory as needed)
- **Dependencies:** TASK-002, TASK-003, TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_phase15.py`
- **Description:** Execute the deterministic front half of the workflow through schedule write and Phase 1.5 plan review, using `plan-review-route` for every plan-review decision.
- **Acceptance criteria:**
  - Runner auto-promotes single-file plans by calling `decompose_plan` and then operates on directory mode.
  - Runner calls preflight, check-plan-deps, pre-dispatch gates, acquire-lock, build-tasks, assignment policy, write-schedule, and schedule-valid gate in the documented order.
  - Runner logs `run_start`, `schedule_written`, `analyst_done`, `plan_review_start`, `plan_review_done`, `plan_review_skipped`, and `plan_review_route_called` through `PlanOpsFacade.log_event` where those events occur.
  - Runner calls `plan_review_route` for `pre_dispatch`, `post_review`, `post_triage`, `post_plan_author`, and `manual_pause` as needed by the current schema.
  - Second plan review uses `stage=post_review` with `attempt=2`; the runner does not emit a nonexistent `post_second_review` stage.
  - `--skip-plan-review` reaches Phase 2 without dispatching a plan reviewer and records the route decision.
  - Codex/Gemini/Claude plan-review provider selection is driven by assignment policy and capabilities.
  - Plan-review `unknown_state` returns a paused JSON summary and writes runner state.
  - Plan-review `halt_plan_review_failed` releases the run lock and returns non-zero status unless `--dry-run`.
  - Tests cover approved path, skip path, reviewer unavailable degradation, needs-replan + triage ship, needs-replan + author retry + second approved, and second needs-replan halt using stub providers.
  - Existing `plan_review_state` in the schedule remains the route state source; runner-only sidecar stores only current phase and pause metadata.

**Description:**
Execute the deterministic front half of the workflow through schedule write
and Phase 1.5 plan review, using `plan-review-route` for every plan-review
decision.

## Verification

Run the task test command.

## Non-goals

- Phase B implementation.
- Phase D task review routing.
- Real provider calls in tests.
