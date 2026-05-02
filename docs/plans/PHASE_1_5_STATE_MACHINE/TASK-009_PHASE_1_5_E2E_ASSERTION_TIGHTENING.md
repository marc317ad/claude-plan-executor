# TASK-009 — Phase 1.5 E2E assertion tightening

## Goal

Tighten the TASK-007 smoke coverage into stronger behavioral guarantees for the Phase 1.5 / 1.5.5 / Phase 2 routing loop.

## Context

TASK-007 shipped with reviewer findings preserved for follow-up. The E2E smoke demonstrates the routing loop, but several failure-path assertions are too shallow. This task converts those reviewer findings into durable tests without changing the router contract unless a test exposes a real implementation bug.

## Verification

- TASK-007 reviewer findings MAJ-1 through MAJ-5 are each addressed by at least one assertion or an explicit documented dismissal in the test file.
- E2E tests fail if unknown verdicts accidentally proceed to `batch_start`.
- E2E tests fail if `--update-schedule-state` stops writing state.
- E2E tests fail if triage `ship` and `ship-with-fixes` lose their user-facing summary semantics.
- The `plan_review_state` round-trip test proves disk state is authoritative by mutating the schedule file between route invocations and not supplying equivalent explicit state in the route payload.
- Triage parser fixtures cover at minimum `ship`, `ship-with-fixes` with notes, `partial-agreement` with agreed and dismissed findings, `needs-rework` with the full findings payload, and one malformed or unknown verdict envelope.
- Runtime remains reasonable for the smoke suite, targeting under 45 seconds on the current dev machine.
- `venv/bin/python -m pytest tests/scripts/test_phase_1_5_e2e.py tests/scripts/test_plan_review_state.py tests/scripts/test_plan_ops_plan_review_route.py` passes.
- `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "parse_plan_review or plan_review_triage"` passes, or equivalent focused parser tests pass if the implementation splits parser tests into new files.

## Tasks

### TASK-009: Phase 1.5 E2E assertion tightening

- **Status:** Pending
- **Priority:** high
- **Files:**
  - tests/scripts/test_phase_1_5_e2e.py (edit)
  - tests/scripts/fixtures/phase_1_5_plan/ (edit)
  - tests/scripts/test_plan_review_state.py (edit)
  - tests/scripts/test_plan_ops_plan_review_route.py (edit)
  - tests/scripts/test_plan_ops.py (edit, unless parser cases are split into focused parser test files)
- **Dependencies:** [007, 008]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_phase_1_5_e2e.py tests/scripts/test_plan_review_state.py tests/scripts/test_plan_ops_plan_review_route.py && venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "parse_plan_review or plan_review_triage"`
- **Acceptance criteria:**
  - Unknown-verdict / unknown-state coverage asserts event order through `plan_review_route_called`, the pause or `run_end` semantics expected by the SKILL, and that no batch starts after an unknown route.
  - A real on-disk `plan_review_state` round-trip uses `--update-schedule-state`, reads state from the schedule file, writes state back to the schedule file, mutates the schedule file between two route calls, and asserts the second call observes the mutation.
  - The round-trip test does not pass equivalent in-memory state in the payload in a way that bypasses writer-side behavior.
  - The `claude_only=true` reviewer failure path is covered through the same parser shape used by `parse-plan-review-report`.
  - Triage verdict checks assert summary/banner/notes behavior for `ship` and `ship-with-fixes`, the `parse-plan-review-triage-report` agreed/dismissed partition for `partial-agreement`, and full findings payload semantics for `needs-rework`.
  - Parser-shape coverage includes realistic reviewer and triage envelopes plus malformed or unknown verdicts, preferably through fixture-driven `pytest.mark.parametrize` coverage.
- **Reversion guidance:** Revert the assertion/fixture changes together; do not remove existing TASK-007 smoke coverage unless the replacement covers the same happy path plus the new failure assertions.

**Description:**
Strengthen the Phase 1.5 smoke tests so they prove the routing loop's failure and triage semantics, not only the happy-path action names. This task is primarily test hardening; implementation changes should be made only when the stronger tests expose a real router or parser bug.
