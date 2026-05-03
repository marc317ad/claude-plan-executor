# TASK-006: Phase B And Phase D Execution Loop

## Goal

Implement batch execution, implementation dispatch, review dispatch, `review-route` handling, narrow commits/failures, and batch reconciliation.

## Context

Phase D is complete and should be the only source of truth for post-review routing. The runner's job is to build route payloads from provider results and comply with the returned directive.

## Scoped Context

This task implements the happy path and documented Phase D branches using stubbed providers in tests. Live model e2e remains TASK-008.

### TASK-006: Phase B And Phase D Execution Loop

- **Status:** pending
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_runner_phase_d.py` (new)
  - `tests/scripts/fixtures/implement_plan_runner/phase_d/` (new fixture directory as needed)
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_phase_d.py`
- **Description:** Execute implementation and review batches through provider adapters, feed parsed review outputs to `review-route`, and comply with commit, fail, remediation, role-swap, and pause directives.
- **Acceptance criteria:**
  - Runner uses `batch_next` with schedule state rather than maintaining its own done/failed/locked file sets.
  - Runner dispatches up to `--parallel N` tasks in a batch but never dispatches tasks with conflicting file locks.
  - Runner logs `batch_start`, `implement_start`, `implement_done`, `review_start`, `review_done`, `review_skipped`, `review_route_called`, `commit_done`, `failed`, and `run_end` through currently allowed public event paths.
  - Runner does not emit `batch_done` through `log-event` unless a separate event-contract change first adds it to `plan_ops.ALLOWED_LOG_EVENTS`.
  - Claude, Codex, Gemini, and reviewer `none` route payloads include canonical route identities for `implementer` and `reviewer`, plus `claude_only`, `unattended_revert_policy`, `flags`, `retries_used`, `reviewer_envelope`, and optional `d5_envelope`.
  - `review_route` actions `commit`, `fail`, `dispatch_d5`, `dispatch_bounded_remediation`, `dispatch_narrow_remediation`, `dispatch_role_swap`, `pause_awaiting_user`, and `unknown_state` are all handled.
  - Commit path calls `commit_task` with route-provided flags and `--update-schedule-state`.
  - Fail path calls `fail_task` only when the route output carries a valid authorization source or the failure branch is otherwise documented by existing `plan_ops` enums.
  - Remediation and role-swap dispatches use provider adapters and route-provided `dispatch_context` without reconstructing Phase D logic.
  - `reconcile_batch` runs after each batch and can pause the run on out-of-scope changes.
  - Tests cover clean commit, minor findings commit, skipped review via reviewer `none`, Gemini fallback review, Claude-only review, binding pause, binding fail-fast, D.5 disagreement, bounded remediation, narrow remediation, role swap, and unknown_state pause.

**Description:**
Execute implementation and review batches through provider adapters, feed
parsed review outputs to `review-route`, and comply with commit, fail,
remediation, role-swap, and pause directives.

## Verification

Run the task test command.

## Non-goals

- Crash resume after process termination.
- Live Codex/Claude/Gemini calls.
- Changing route semantics.
