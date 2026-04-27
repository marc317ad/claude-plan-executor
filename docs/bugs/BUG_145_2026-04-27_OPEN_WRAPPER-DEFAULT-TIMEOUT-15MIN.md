# BUG-145 — Wrapper backend default timeout (15 min) causes large-task pauses

**Status:** OPEN
**Date observed:** 2026-04-27
**Severity:** medium (procedural — every multi-file task now risks pause-and-resume cycle)

## Summary

`/implement-plan` TASK-001 of the `wrapper_autoclean_authorization` plan hit `status=timeout` at exactly `duration_ms=900362`, i.e. ~900s (15 min). The implementer had completed the bulk of the work (+254 lines plan_ops.py, +158/57 tests, schema/SKILL/templates updates) but had not emitted the structured JSON report when the backend default timeout fired.

Wrapper correctly took the documented Completed-Work Preservation path: non-empty-diff pause, no `git restore`, no `fail-task`, `awaiting_user post_implement_failure`, `run_end outcome=paused`, lock released. After `keep-and-commit` resume confirmed the work was complete (TASK-001 test_command passed both halves), no functional damage was done — but the run lost ~3 minutes on resume orchestration.

## Where the timeout comes from

- `plan_claude_dispatch.py run --timeout` defaults to `None` (line 951–957)
- Falls back to either `input.overrides.timeout_sec` or "backend default"
- SKILL.md / dispatch-templates.md do not reference `900` anywhere — the 15-minute cap is unwritten
- `run_log.jsonl` shows `status=timeout duration_ms=900362` matching exactly the 900s mark, confirming the backend default is 900s

## Impact

Any task that legitimately needs more than 15 minutes (TASK-001 was 6 files including a +254-line subcommand and 215 lines of tests) will trigger:
1. wrapper timeout
2. preserved-diff pause
3. orchestrator-level keep-and-commit verification
4. fresh run_id resume

This is correct safety behavior, but it pessimizes the dispatch budget for any heavy task.

## Suggested fix (for a follow-up plan, not in scope here)

1. Either bump the backend default to 1800s (30 min) — covers all observed task sizes.
2. Or expose `dispatch_timeout_sec` per-task in `00_INDEX.json` so the SKILL/wrapper picks it up.
3. Or expose an environment override (`CLAUDE_PLAN_DISPATCH_DEFAULT_TIMEOUT_SEC`) that the SKILL pins at preflight.

## Reproduction (forensic)

- Run id `20260427T213500`, task 001
- `run_log.jsonl` line: `{"event":"claude_dispatch_failed", ..., "status":"timeout", "duration_ms":900362}`
- Diff captured in `git status` after pause showed all expected TASK-001 files modified

## Notes

- The `--unattended-revert-policy=pause` setting + non-empty-diff branch worked exactly as documented (prohibit_silent_revert TASK-004).
- Verified after the fact: tests pass, work is complete. Confirms the timeout was budget-only, not a stuck implementer.
