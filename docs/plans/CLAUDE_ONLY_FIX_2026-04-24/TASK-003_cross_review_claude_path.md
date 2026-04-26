# TASK-003 — Cross-review Claude path and D.2 ladder collapse

## Goal

Cross-review Claude path and D.2 ladder collapse

## Context

Route Phase D.1 cross-review through the existing `code-reviewer` agent when `claude_only=true`, mirroring TASK-002's Phase 1.5 route-switch. The `code-reviewer` agent and Phase D-Claude template are reused as-is. The D.2a third-opinion ladder collapses because there is no Codex verdict to adjudicate; `needs-rework` goes straight to D.4 fail-task. The D.2b role-swap retry uses `code-reviewer` for the re-review.

This task is prose-only edits to `SKILL.md` and `dispatch-templates.md`. No new agent file. No code changes in `plan_ops.py`. No new tests. The run summary banner contract from TASK-001/TASK-002's `claude_only` plumbing fires here — the End-of-run summary section gains a one-paragraph rule that emits the banner when `claude_only=true`.

## Verification

- `SKILL.md` §Phase D.1 grows a route-switch identical in shape to TASK-002's: `claude_only=true` → `Agent(subagent_type:"code-reviewer", model:"sonnet", ...)` with the existing Phase D-Claude template (`dispatch-templates.md:409`); `claude_only=false` → existing wrapper path. Both feed the same `review_done` event shape with the appropriate `reviewer` field.
- `SKILL.md` §Phase D.2a documents that under `claude_only=true`, D.5 escalation, D.2a.5 bounded remediation, and D.2a.6 narrow remediation are unreachable. `code-reviewer` `needs-rework` under `claude_only=true` goes straight to D.4 fail-task, with no D.5 third-opinion ladder. The §Rules section gains a corresponding bullet.
- `SKILL.md` §Phase D.2b documents that under `claude_only=true`, the role-swap retry uses `code-reviewer` for the re-review (not the Codex wrapper). The retry implement step is unchanged (`plan-implementer` Opus).
- `SKILL.md` §End of run final summary documents the loud banner contract from §Scoped Context. The banner appears whenever the run had `claude_only=true` for any reason (operator flag OR `codex_available=false`).
- `dispatch-templates.md` §Phase D-Claude has a brief callout that this template is now the single Claude-cross-review template, used for both Codex-impl→Claude review (existing) AND Claude-impl→Claude review under `claude_only=true` (new).
- Run-log events under `claude_only=true`: `review_start {reviewer:"claude", task_id, ...}`, `review_done {reviewer:"claude", task_id, verdict, findings_count, ...}`. Verdict vocabulary is `{ship, ship-with-fixes, needs-rework}` (matching the existing Codex-impl→Claude review path); the Codex-side `{clean, minor-findings, needs-rework}` vocab is not synthesized.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (regression check; no new tests are required because the change is prose-only routing).

## Tasks

### TASK-003: Cross-review Claude path and D.2 ladder collapse

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (edit)
- **Dependencies:** [001, 002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - `SKILL.md` §Phase D.1 grows a route-switch identical in shape to TASK-002's: `claude_only=true` → `Agent(subagent_type:"code-reviewer", model:"sonnet", ...)` with the existing Phase D-Claude template (`dispatch-templates.md:409`); `claude_only=false` → existing wrapper path. Both feed the same `review_done` event shape with the appropriate `reviewer` field.
  - `SKILL.md` §Phase D.2a documents that under `claude_only=true`, D.5 escalation, D.2a.5 bounded remediation, and D.2a.6 narrow remediation are unreachable. `code-reviewer` `needs-rework` under `claude_only=true` goes straight to D.4 fail-task, with no D.5 third-opinion ladder. The §Rules section gains a corresponding bullet.
  - `SKILL.md` §Phase D.2b documents that under `claude_only=true`, the role-swap retry uses `code-reviewer` for the re-review (not the Codex wrapper). The retry implement step is unchanged (`plan-implementer` Opus).
  - `SKILL.md` §End of run final summary documents the loud banner contract from §Scoped Context. The banner appears whenever the run had `claude_only=true` for any reason (operator flag OR `codex_available=false`).
  - `dispatch-templates.md` §Phase D-Claude has a brief callout that this template is now the single Claude-cross-review template, used for both Codex-impl→Claude review (existing) AND Claude-impl→Claude review under `claude_only=true` (new).
  - Run-log events under `claude_only=true`: `review_start {reviewer:"claude", task_id, ...}`, `review_done {reviewer:"claude", task_id, verdict, findings_count, ...}`. Verdict vocabulary is `{ship, ship-with-fixes, needs-rework}` (matching the existing Codex-impl→Claude review path); the Codex-side `{clean, minor-findings, needs-rework}` vocab is not synthesized.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (regression check; no new tests are required because the change is prose-only routing).
- **Reversion guidance:** none

**Description:**
Route Phase D.1 cross-review through the existing `code-reviewer` agent when `claude_only=true`, mirroring TASK-002's Phase 1.5 route-switch. The `code-reviewer` agent and Phase D-Claude template are reused as-is. The D.2a third-opinion ladder collapses because there is no Codex verdict to adjudicate; `needs-rework` goes straight to D.4 fail-task. The D.2b role-swap retry uses `code-reviewer` for the re-review.

This task is prose-only edits to `SKILL.md` and `dispatch-templates.md`. No new agent file. No code changes in `plan_ops.py`. No new tests. The run summary banner contract from TASK-001/TASK-002's `claude_only` plumbing fires here — the End-of-run summary section gains a one-paragraph rule that emits the banner when `claude_only=true`.

## Execution log — 20260426T010342 (success)

Starting SHA: `70103df4c2f5aa70d5233ecf4ec6cf1d0133a0bd`  → Ending SHA: `4817f6f8652704fe22a1e6da5cd912f09a6f313d`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003 | claude | codex | clean | 4817f6f8 | prose-only D.1 route-switch + D.2 ladder collapse + run-summary banner |
