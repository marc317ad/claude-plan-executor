# TASK-001: Runner Contracts And Schemas

## Goal

Define the stable contracts for an agent-agnostic `implement_plan.py` runner before any orchestration logic is implemented.

## Context

The completed Phase D and Phase 1.5 state machines make the runner feasible because review routing is now deterministic. This task creates the runner-facing schemas and typed contract objects so subsequent tasks do not bake provider-specific assumptions into the execution loop.

## Scoped Context

Touch only contract/schema modules and tests. Do not add live dispatch or phase execution in this task.

### TASK-001: Runner Contracts And Schemas

- **Status:** done
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py` (new, contract definitions only)
  - `plugins/plan-executor/scripts/schemas/implement_plan_runner_config.json` (new)
  - `plugins/plan-executor/scripts/schemas/implement_plan_runner_state.json` (new)
  - `plugins/plan-executor/scripts/schemas/implement_plan_provider_capability.json` (new)
  - `tests/scripts/test_implement_plan_runner_contracts.py` (new)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_contracts.py`
- **Description:** Establish the runner's JSON and Python contracts before orchestration code exists, with import-safe schema validation and provider capability fixtures.
- **Acceptance criteria:**
  - `implement_plan.py` exists and is importable without importing live wrapper backends or launching subprocesses.
  - Contract dataclasses or typed dicts exist for `RunnerConfig`, `RunnerState`, `ProviderCapability`, `TaskAssignment`, and `DispatchResult`.
  - JSON schemas exist for runner config, runner state, and provider capabilities.
  - Runner config supports `plan`, `parallel`, `provider_preference`, `assignments`, `reviewers`, `plan_reviewer`, `allow_provider_fallback`, `dry_run`, `skip_cross_review`, `skip_plan_review`, `unattended_revert_policy`, and `agent_args`.
  - Provider capability schema represents roles: `classify`, `implement`, `review`, `plan_review`, `triage`, and `author`.
  - Provider capability schema includes route identity fields for the current route schemas: `route_implementer` is nullable or one of `claude|codex`, and `route_reviewer` is nullable or one of `codex|gemini|claude|none`.
  - Capability validation rejects providers that claim `implement` support without a supported `route_implementer`.
  - Gemini capability fixtures mark implementation unsupported, `route_implementer=null`, `route_reviewer=gemini`, and review/plan-review supported.
  - Claude capability fixtures mark implementation/classification/authoring supported through `plan_claude_dispatch.py`.
  - Codex capability fixtures mark implementation/review/plan-review supported through `plan_codex_dispatch.py`.
  - Tests validate happy and invalid config examples, including unsupported provider names and invalid task assignment syntax.
  - No phase execution code is added beyond validation helpers.

**Description:**
Establish the runner's JSON and Python contracts before orchestration code
exists, with import-safe schema validation and provider capability fixtures.

## Verification

Run the task test command and confirm importing `implement_plan.py` has no side effects.

## Non-goals

- Calling `plan_ops.py`.
- Spawning Codex, Claude, or Gemini.
- Adding CLI behavior beyond `--help` being import-safe if implemented.

## Execution log — 20260503T145424 (success)

Starting SHA: `258ae58a0c0437487c91b82e4411ab6240800a90`  → Ending SHA: `368e49b82ccc1e22487ff2bd0665a655da76293d`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | codex | none | review skipped: gemini unavailable | 368e49b | Implemented runner contracts and schemas; task-local pytest passed (13 passed). Independent Gemini review skipped because credentials were unavailable. |
