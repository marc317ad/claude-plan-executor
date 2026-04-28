---
bug_id: 145
status: CLOSED
group: WRAPPER-DEFAULT-TIMEOUT-15MIN
severity: minor
source_fix_id: null
source_plan: null
source_date: 2026-04-27
origin: surfaced during /implement-plan TASK-001 of wrapper_autoclean_authorization plan, run 20260427T213500
decomposed_at: 2026-04-27
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
  - plugins/plan-executor/scripts/plan_claude_dispatch.py
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
content_fingerprint: null
change_history: []
---

# BUG-145: Wrapper backend default timeout (15 min) causes large-task pauses

**Status:** OPEN
**Severity:** minor (procedural — every multi-file task now risks pause-and-resume cycle)
**Group:** WRAPPER-DEFAULT-TIMEOUT-15MIN
**Depends on:** none
**Test command:** `venv/bin/pytest tests/scripts/test_plan_claude_dispatch.py -v -k "timeout"`

## Acceptance criteria

- `plan_claude_dispatch.py run` MUST resolve a default timeout above the observed 900s wall when neither `input.overrides.timeout_sec` nor `--timeout` is supplied. Pick one of the three options below and document it in `SKILL.md` + `dispatch-templates.md`:
  1. Bump the backend default to `1800` (30 min) inline in `plan_claude_dispatch.py` and document in SKILL.
  2. Add a `dispatch_timeout_sec` field per-task in `00_INDEX.json` and have the SKILL thread it through.
  3. Honor `CLAUDE_PLAN_DISPATCH_DEFAULT_TIMEOUT_SEC` env var, pinned by the SKILL at preflight.
- A unit test in `tests/scripts/test_plan_claude_dispatch.py` MUST assert that with no `timeout_sec` override the resolved effective timeout is the new documented default (not 900).
- `SKILL.md` and/or `dispatch-templates.md` MUST reference the chosen timeout default explicitly so the value is no longer "unwritten".

## Problem

### Symptom

`/implement-plan` TASK-001 of the `wrapper_autoclean_authorization` plan hit `status=timeout` at exactly `duration_ms=900362`, i.e. ~900s (15 min). The implementer had completed the bulk of the work (+254 lines plan_ops.py, +158/57 tests, schema/SKILL/templates updates) but had not emitted the structured JSON report when the backend default timeout fired.

Wrapper correctly took the documented Completed-Work Preservation path: non-empty-diff pause, no `git restore`, no `fail-task`, `awaiting_user post_implement_failure`, `run_end outcome=paused`, lock released. After `keep-and-commit` resume confirmed the work was complete (TASK-001 test_command passed both halves), no functional damage was done — but the run lost ~3 minutes on resume orchestration.

### Where the timeout comes from

- `plan_claude_dispatch.py run --timeout` defaults to `None` (line 951–957)
- Falls back to either `input.overrides.timeout_sec` or "backend default"
- `SKILL.md` / `dispatch-templates.md` do not reference `900` anywhere — the 15-minute cap is unwritten
- `run_log.jsonl` shows `status=timeout duration_ms=900362` matching exactly the 900s mark, confirming the backend default is 900s

### Impact

Any task that legitimately needs more than 15 minutes (TASK-001 was 6 files including a +254-line subcommand and 215 lines of tests) will trigger:
1. wrapper timeout
2. preserved-diff pause
3. orchestrator-level keep-and-commit verification
4. fresh run_id resume

This is correct safety behavior, but it pessimizes the dispatch budget for any heavy task.

## Recommended fix

Pick option 1 (bump default to 1800s) as the smallest-surface change unless the team wants per-task tuning. The change is:

1. In `plan_claude_dispatch.py` `run` subcommand, change the default-resolution branch to `1800` instead of relying on the unwritten 900s backend default.
2. Document the value in `SKILL.md` Phase B / Phase A-single sections ("default dispatch timeout: 1800s").
3. Add a unit test asserting the resolved effective timeout when no override is supplied.

If the team prefers options 2 or 3 (per-task or env-var), expand the AC accordingly before implementing.

## Reproduction (forensic)

- Run id `20260427T213500`, task 001
- `run_log.jsonl` line: `{"event":"claude_dispatch_failed", ..., "status":"timeout", "duration_ms":900362}`
- Diff captured in `git status` after pause showed all expected TASK-001 files modified

## Notes

- The `--unattended-revert-policy=pause` setting + non-empty-diff branch worked exactly as documented (prohibit_silent_revert TASK-004).
- Verified after the fact: tests pass, work is complete. Confirms the timeout was budget-only, not a stuck implementer.

## Run history

(none yet — bug filed 2026-04-27)
