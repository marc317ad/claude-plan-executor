# TASK-001 — Seed scratch directory

**Created:** 2026-04-23
**Status:** pending
**Base branch:** main

## Goal

Seed a scratch directory used by the sibling tasks in this decomposed plan.
Exists to give TASK-002 and TASK-003 a common parent directory to write into
while keeping their file-lock sets disjoint.

## Context

This fixture is the three-child integration harness for TASK-004 (directory-mode
driver). Sibling tasks TASK-002 and TASK-003 depend on TASK-001 and each write
to a disjoint leaf file under `scratch/`, which lets the orchestrator batch them
together in a single parallel batch. The acceptance test for TASK-004 verifies
that `plan-analyst` emits per-task `plan_file`, `batch-next` batches TASK-002 +
TASK-003 together, `commit-task` flips the right child's header, and
`block-dependents` cascades to the right child files on a seeded failure.

## Verification

1. `test -d scratch` — the scratch directory exists after TASK-001 commits.
2. The plan's top-level `**Status:**` flips to `complete` once this task is the
   only one in its child plan file and has committed.

## Tasks

### TASK-001: Seed scratch directory

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - scratch/.gitkeep (create)
- **Dependencies:** none
- **Test command:** `test -d scratch`
- **Acceptance criteria:**
  - `scratch/` exists at repo root.
  - Directory is committed via `scratch/.gitkeep` so git tracks it.
- **Reversion guidance:** `rm -rf scratch`

**Description:**
Minimal seeder task for the directory-mode integration fixture. Creates the
scratch parent directory that TASK-002 and TASK-003 write into. Lives in its
own child plan file so the integration harness has three distinct `plan_file`
basenames to route `commit-task` / `fail-task` / `block-dependents` against.
