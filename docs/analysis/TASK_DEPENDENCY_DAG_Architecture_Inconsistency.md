# Architecture Analysis: Task Dependency DAG Inconsistency

**Date:** 2026-04-23
**Status:** Audit of `Fix_Depenency_Gate_Plans` implementation

## Executive Summary

An audit of the `Fix_Depenency_Gate_Plans` (sic) execution shows the project is currently in a state of **architectural drift**. While the high-level agent instructions (`plan-analyst.md`) and documentation have been partially updated to a de-gated dependency model, the core execution logic (`plan_ops.py`) and the test suite (`test_plan_ops.py`) remain heavily dependent on intra-plan DAG reasoning, cycle detection, and cascade-blocking.

## Implementation Status by Task

| Task | Component | Status | Finding |
| :--- | :--- | :--- | :--- |
| **TASK-001** | `plan_ops.py` | **FAILED** | `cmd_block_dependents` and `_validate_schedule_dag` (cycle/orphan detection) are still active. `cmd_filter_schedule` still performs transitive closure. |
| **TASK-002** | `plan_codex_dispatch.py` | **PASSED** | The `dependencies` field was successfully removed from `parse_task_block` return dict. |
| **TASK-003** | `plan-analyst.md` | **PASSED** | The analyst has been fully rewritten to use file-lock disjointness and priority instead of DAG reasoning. |
| **TASK-004** | `SKILL.md` | **PARTIAL** | Phase 0 now contains the `check-plan-deps` gate, but Phase C and D still explicitly call `block-dependents` on failure. |
| **TASK-005** | `run-log-schema.md` | **PASSED** | `blocked_count` and the `blocked` event have been removed from the schema documentation. |
| **TASK-006** | `test_plan_ops.py` | **FAILED** | The suite still pins legacy behaviors. `test_batch_next_blocks_dependent_on_failed` explicitly asserts blocking, which the plan intended to remove. |
| **TASK-007** | `DUAL_AGENT_PLAN_EXECUTOR.md` | **FAILED** | The design document still specifies `_validate_schedule_dag` as a required gate. |
| **TASK-008** | `test_..._integration.py` | **PASSED** | Integration tests successfully implement skips for environment-specific Codex initialization failures. |

## Detailed Findings

### 1. Conflict with TASK-019
The implementation of **TASK-019** (Consolidated Orchestrator Polish) appears to have landed *after* or in contradiction to the `Fix_Depenency_Gate_Plans` goals. TASK-019 explicitly consolidated Kahn’s algorithm into a shared `_validate_schedule_dag` helper and wired it into `parse-schedule`, `batch-next`, and `filter-schedule`. This directly conflicts with the goal of **TASK-001**, which was to "strip" these exact gates.

### 2. Executor Inconsistency
The system is currently internally inconsistent:
- **Analyst:** Emits a schedule based on priority and file-locks (ignoring `Dependencies:`).
- **Executor (`batch-next`):** Still checks the `dependencies` field in the schedule JSON and refuses to pick tasks if a dependency is not `done`.
- **Skill (`SKILL.md`):** Operates in a hybrid mode, calling a pre-flight cross-plan check but then attempting intra-plan cascade blocking that the Analyst no longer provides data for.

### 3. Test Suite Regression
The test suite `tests/scripts/test_plan_ops.py` has been updated to include tests labeled for TASK-019 which enforce the very dependency gates that the de-gating plan intended to remove. For example, `test_batch_next_blocks_dependent_on_failed` asserts that a task is **not** picked if its dependency failed, whereas the de-gating plan (TASK-006) explicitly required a new test `test_batch_next_ignores_upstream_failure` asserting the opposite.

## Recommendation

The `Fix_Depenency_Gate_Plans` implementation should be considered **stalled or regressed**. To achieve the intended architecture:
1.  **TASK-001** must be re-executed to actually remove the logic from `plan_ops.py`.
2.  **TASK-004** must be completed to remove the `block-dependents` calls from `SKILL.md`.
3.  **TASK-006** must be re-executed to align the test suite with the de-gated model, specifically reversing the changes introduced by TASK-019 regarding cycle and orphan detection.
4.  **TASK-007** must be completed to align the `DUAL_AGENT_PLAN_EXECUTOR.md` with the code.
