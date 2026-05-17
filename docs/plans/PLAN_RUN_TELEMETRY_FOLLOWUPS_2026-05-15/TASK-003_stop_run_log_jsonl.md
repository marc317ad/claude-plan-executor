# TASK-003 — Stop `_run_log.jsonl` from accruing CRLF churn

## Goal

Stop `_run_log.jsonl` from accruing CRLF churn

## Context

Auto-decomposed child for TASK-003. See the source plan for broader context.

## Verification

- In `plan_ops.py:_append_run_log` (currently `plan_ops.py:4539`), change all three `RUN_LOG_PATH.open(...)` calls (lines 4544, 4547, 4552) to pass `newline="\n"` in addition to the existing `encoding="utf-8"`. The write call's `fh.write(line + "\n")` is unchanged.
- In `.gitattributes`, add a new line `*.jsonl text eol=lf` immediately after the existing `*.sh   text eol=lf` line (keep alphabetical / file-extension grouping consistent with the surrounding lines). Do NOT change any other line in `.gitattributes`.
- The implementer runs `git add --renormalize "*.jsonl"` once on the same commit so any historical CRLF in tracked `.jsonl` files (including past `_run_log.jsonl` entries and `tests/scripts/fixtures/runlog/deferred_test.jsonl`) is flattened in this commit's diff. Document the renormalize step in the commit body. The tracked `.jsonl` paths (`docs/plans/_run_log.jsonl`, `tests/scripts/fixtures/runlog/deferred_test.jsonl`) are listed under `Files:` so any renormalize-produced diff falls within the task's declared ownership; `_run_log.jsonl` is `commit-task`'s always-ignore set member, so its renormalize diff stays in the working tree and will land via the orchestrator's housekeeping commit at end-of-run.
- The implementer manually verifies the fix in the same dispatch: append a one-line test event via the canonical path (`plan_ops__log_event` or `plan_ops.py log-event`), then `git status --short`; the `_run_log.jsonl` line MUST be the only change AND the diff hunk MUST contain no `\r` markers (run `git diff -- '*.jsonl' | grep -c $'\r'` and assert zero).
- No other behavior changes: the run-log append + verify-on-read shape is unchanged, the event schema is unchanged, the audit trail is unchanged.

## Tasks

### TASK-003: Stop `_run_log.jsonl` from accruing CRLF churn

- **Status:** done
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `.gitattributes`
  - `docs/plans/_run_log.jsonl`
  - `tests/scripts/fixtures/runlog/deferred_test.jsonl`
- **Dependencies:** []
- **Test command:** `deferred (TASK-003) — one-shot renormalize commit + a manual reproduction is the verification; no automated regression test is added (the .gitattributes pin is self-verifying via git's normalization-on-commit, and the newline= bind is a one-line correctness fix that does not benefit from a Python-side test on a non-Windows CI host).`
- **Acceptance criteria:**
  - In `plan_ops.py:_append_run_log` (currently `plan_ops.py:4539`), change all three `RUN_LOG_PATH.open(...)` calls (lines 4544, 4547, 4552) to pass `newline="\n"` in addition to the existing `encoding="utf-8"`. The write call's `fh.write(line + "\n")` is unchanged.
  - In `.gitattributes`, add a new line `*.jsonl text eol=lf` immediately after the existing `*.sh   text eol=lf` line (keep alphabetical / file-extension grouping consistent with the surrounding lines). Do NOT change any other line in `.gitattributes`.
  - The implementer runs `git add --renormalize "*.jsonl"` once on the same commit so any historical CRLF in tracked `.jsonl` files (including past `_run_log.jsonl` entries and `tests/scripts/fixtures/runlog/deferred_test.jsonl`) is flattened in this commit's diff. Document the renormalize step in the commit body. The tracked `.jsonl` paths (`docs/plans/_run_log.jsonl`, `tests/scripts/fixtures/runlog/deferred_test.jsonl`) are listed under `Files:` so any renormalize-produced diff falls within the task's declared ownership; `_run_log.jsonl` is `commit-task`'s always-ignore set member, so its renormalize diff stays in the working tree and will land via the orchestrator's housekeeping commit at end-of-run.
  - The implementer manually verifies the fix in the same dispatch: append a one-line test event via the canonical path (`plan_ops__log_event` or `plan_ops.py log-event`), then `git status --short`; the `_run_log.jsonl` line MUST be the only change AND the diff hunk MUST contain no `\r` markers (run `git diff -- '*.jsonl' | grep -c $'\r'` and assert zero).
  - No other behavior changes: the run-log append + verify-on-read shape is unchanged, the event schema is unchanged, the audit trail is unchanged.
- **Reversion guidance:** Remove the `newline="\n"` kwarg from the three `open()` calls; remove the `*.jsonl text eol=lf` line from `.gitattributes`; run `git checkout HEAD~1 -- '*.jsonl'` only if the operator explicitly authorizes restoring the pre-renormalized blobs (otherwise the renormalized state stays as the new baseline).

**Description:**
Stop `_run_log.jsonl` from accruing CRLF churn. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-003. See the parent plan's `## Context` and `## Verification` sections for the full intent.)

## Execution log — 20260515T142051 (success)

Starting SHA: `c2a6ea543b720cab3c26dfe9466517fd1786ce7b`  → Ending SHA: `f62836519ab0d1c992e13390d3e11dedef27d421`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003 | codex | claude | ship-with-fixes | f628365 | Hand-fix: orchestrator ran git add --renormalize *.jsonl (Codex sandbox blocked from writing .git/index.lock). 3 minor non-blocking findings: read-path newline= semantics, no-op renormalize staging gap, .gitattributes comment polish. |
