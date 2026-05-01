# TASK-001 - MCP E2E smoke: edit foo.py

**Created:** 2026-04-30
**Status:** pending
**Base branch:** main

## Goal

Single-task fixture for the TASK-018 MCP end-to-end smoke. The task declares
one writable source file so the harness can exercise commit, commit-safe,
reconcile, and failure paths without spawning real Codex or Claude agents.

## Tasks

### TASK-001: MCP smoke single task

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - src/foo.py
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - `src/foo.py` is committed on the clean path.
  - Plan **Status:** flips to `done` after commit.
- **Reversion guidance:** restore the single changed line in `src/foo.py`.

**Description:**
Smoke-only single task. The test harness injects implementer and reviewer
stub envelopes and routes every plan operation through registered MCP tools on
the orchestrator path.
