# Plan: E2E smoke fixture for SKILL_bash_dispatch_migration TASK-007

**Created:** 2026-04-26
**Status:** fixture
**Base branch:** main

Minimal 2-task plan used by `test_skill_dispatch_e2e.py` to drive the
A→E loop (analyst → implementer → D.1 review → commit) under stubbed
Claude dispatches. No real subprocess spawn — the stub from TASK-002
returns canned envelopes; Codex / D.5 reviewer envelopes are
synthesised in-test (those dispatch paths are intentionally NOT
migrated by this plan and stay on the `Agent` tool surface).

## Goal

Provide a pinned, minimal task graph the e2e test can iterate over.
Two tasks, both `agent=claude`, no inter-task dependencies — the test
asserts orchestrator-side routing decisions per envelope, not actual
file edits.

## Tasks

### TASK-001: Stub task A

- **Status:** pending
- **Priority:** high
- **Files:**
  - `foo/bar.py`
- **Dependencies:** none
- **Test command:** `true`
- **Acceptance criteria:**
  - Stub task — the e2e test does not execute its body; it only feeds
    fixture envelopes through the orchestrator-side routing helpers.

**Description:** Pinned task A for the matrix cases.

**Reversion guidance:** N/A (fixture).

---

### TASK-002: Stub task B

- **Status:** pending
- **Priority:** high
- **Files:**
  - `foo/baz.py`
- **Dependencies:** none
- **Test command:** `true`
- **Acceptance criteria:**
  - Stub task — same as TASK-001.

**Description:** Pinned task B for the matrix cases.

**Reversion guidance:** N/A (fixture).
