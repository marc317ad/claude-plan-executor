# TASK-001 — Phase D smoke: edit foo.py

**Created:** 2026-04-26
**Status:** pending
**Base branch:** main

## Goal

Single-task fixture used by the Phase D end-to-end smoke harness. The task
declares one writable file (`src/foo.py`) so `commit-task` has something to
stage and `fail-task` has something to restore. The acceptance test never
actually mutates `src/foo.py` — the smoke harness writes its own bytes
before invoking `commit-task` per scenario.

## Tasks

### TASK-001: Smoke single task

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - src/foo.py
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - `src/foo.py` is staged and committed.
  - Plan **Status:** flips to `done` post-commit.
- **Reversion guidance:** revert the single changed line in `src/foo.py`.

**Description:**
Smoke-only single task. Drives the Phase D state-machine end-to-end loop
(preflight → parse-schedule → write-schedule → batch-next → review-route →
commit-task | fail-task) against a one-task plan. The test owns the
implementer + reviewer stubs; no Codex or Claude subprocess is spawned.
