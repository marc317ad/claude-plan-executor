# TASK-007: Pause And Resume Protocol

## Goal

Add a durable pause/resume protocol for `implement_plan.py` that works for unknown states, binding pauses, wrapper cleanup blocks, reconcile pauses, and user-directed post-pause actions.

## Context

The SKILL can pause by returning control to the user. A script needs equivalent behavior that any calling agent can inspect and resume without relying on hidden conversation state.

## Scoped Context

This task adds runner-state persistence and resume commands. It must not duplicate schedule/run-log task status.

### TASK-007: Pause And Resume Protocol

- **Status:** done
- **Priority:** high
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `plugins/plan-executor/scripts/schemas/implement_plan_runner_state.json`
  - `tests/scripts/test_implement_plan_runner_resume.py` (new)
- **Dependencies:** TASK-006
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_resume.py`
- **Description:** Persist runner-only pause metadata and add status/resume commands that can be used by any shell-capable agent without conversation-local state.
- **Acceptance criteria:**
  - Runner writes `<schedule_file>.runner-state.json` or a documented adjacent sidecar on every phase transition.
  - Runner state contains `schema_version`, `run_id`, `plan_path`, `schedule_file`, `phase`, `status`, `assignments`, `active_task_id`, `active_batch`, `pause_reason`, `pause_payload`, and `resume_options`.
  - Runner state does not contain duplicated task done/failed/blocked lists.
  - CLI supports `implement_plan.py status --plan <plan>` and `implement_plan.py resume --plan <plan> --decision <decision>`.
  - Supported resume decisions include `retry`, `fail-fast`, `preserve-only`, `revert`, `keep-and-commit`, and `abort` only where the pause payload authorizes them.
  - Invalid resume decisions fail closed and do not mutate schedule, run-log, or working tree.
  - Resume rehydrates provider assignments from runner state and schedule/run-log, not from stale CLI defaults.
  - Resume after `unknown_state` can either abort cleanly or continue with an explicit user-supplied route decision payload.
  - Tests cover status output, valid resume, invalid resume, stale run id, missing lock, corrupted runner-state JSON, and no duplicate source of truth.

**Description:**
Persist runner-only pause metadata and add status/resume commands that can be
used by any shell-capable agent without conversation-local state.

## Verification

Run the task test command.

## Non-goals

- Interactive terminal prompts.
- Long-running daemon mode.
- Recovery from arbitrary git conflicts beyond existing wrapper/plan_ops behavior.

## Execution log — 20260503T201049 (paused)

Starting SHA: `072fbade61eadac69734dd8b58fe8e1385ec6019`  → Ending SHA: `072fbade61eadac69734dd8b58fe8e1385ec6019`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 007 | codex | gemini | needs-rework |  | Paused after Gemini review; implementation preserved for user-directed remediation. |
