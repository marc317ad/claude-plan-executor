# TASK-004: Provider Assignment Policy

## Goal

Implement deterministic assignment of tasks and review roles from explicit arguments, task metadata, provider capabilities, and safe fallback policy.

## Context

The objective is to pass agent arguments into the runner and also allow the tool to figure out where to send effort when the user does not pin every task. This policy must be explicit and testable so an agent can predict routing before any model call.

## Scoped Context

This task computes assignments only. It must not dispatch providers or mutate plans.

### TASK-004: Provider Assignment Policy

- **Status:** done
- **Priority:** high
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_assignment_policy.py` (new)
- **Dependencies:** TASK-001, TASK-003
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_assignment_policy.py`
- **Description:** Resolve task implementers and reviewers from explicit arguments, task metadata, provider capabilities, and fallback policy before any model dispatch occurs.
- **Acceptance criteria:**
  - Add `resolve_assignments(tasks, config, capabilities) -> AssignmentPlan`.
  - Precedence is documented and enforced: CLI/config explicit assignment > task `agent` field > provider preference auto-policy > fail with actionable error.
  - `--codex-only` and `--claude-only` compatibility is preserved as aliases for assignment constraints.
  - Implementation assignment requires both `roles` containing `implement` and a non-null route-supported `route_implementer`; otherwise assignment fails before dispatch.
  - Explicit `TASK-NNN=gemini` implementation assignment fails because Gemini implementation is unsupported.
  - Review assignment can select Gemini only for review/plan-review roles.
  - Auto-policy prefers providers in `provider_preference` order but skips unavailable or role-unsupported providers.
  - If `allow_provider_fallback=false`, loss of the assigned provider fails before dispatch.
  - If `allow_provider_fallback=true`, fallback is recorded in the assignment output with `fallback_from`, `fallback_to`, and `reason`.
  - Assignment output includes implementer provider, reviewer provider, plan reviewer provider, classifier provider, author/triage providers, and the route identities that will be sent to `review-route` / `plan-review-route`.
  - Tests cover explicit assignment, task-declared `**Agent:**`, auto assignment, unavailable providers, fallback disabled, fallback enabled, `--task-ids` subset, and unknown task IDs.

**Description:**
Resolve task implementers and reviewers from explicit arguments, task metadata,
provider capabilities, and fallback policy before any model dispatch occurs.

## Verification

Run the task test command.

## Non-goals

- Provider probing implementation beyond consuming capability objects from TASK-003.
- Dispatch.
- Route decisions.

## Execution log — 20260503T182851 (paused)

Starting SHA: `91a2223220c23c101af981a073a90a4d54f08d8b`  → Ending SHA: `91a2223220c23c101af981a073a90a4d54f08d8b`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 004 | codex | gemini | needs-rework |  | Paused at post_gemini_review. Blocking finding: --assign strings are passed into assignments without parsing before resolve_assignments. |
