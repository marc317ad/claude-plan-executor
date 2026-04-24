# TASK-003 — Write scratch/b.txt

**Created:** 2026-04-23
**Status:** pending
**Base branch:** main

## Goal

Write `scratch/b.txt` after TASK-001 has seeded `scratch/`. Files are disjoint
from TASK-002 so the orchestrator batches both into the same parallel batch.

## Context

Third of three children in the TASK-004 directory-mode integration fixture.
Depends on TASK-001 and writes a file disjoint from TASK-002's output. The
orchestrator therefore picks TASK-002 and TASK-003 into the same parallel
batch. On a seeded TASK-001 failure, `block-dependents` cascades `blocked`
onto TASK-003 in this child file (and onto TASK-002 in its sibling),
exercising the multi-file cascade path from TASK-002's internals change.

## Verification

1. `test -f scratch/b.txt` — the leaf file exists after TASK-003 commits.
2. On a seeded TASK-001 failure, this child's `**Status:**` flips to
   `blocked` for TASK-003 via `block-dependents`, with the matching child
   file (`TASK-002_write_a.md`) flipping its own TASK-002 independently.

## Tasks

### TASK-003: Write scratch/b.txt

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - scratch/b.txt (create)
- **Dependencies:** [001]
- **Test command:** `test -f scratch/b.txt`
- **Acceptance criteria:**
  - `scratch/b.txt` exists and is a non-empty regular file.
  - Does NOT touch `scratch/a.txt` (file-lock disjointness invariant that
    lets TASK-002 and TASK-003 batch in parallel).
- **Reversion guidance:** `rm -f scratch/b.txt`

**Description:**
Parallel-batch leaf B in the TASK-004 directory-mode integration fixture.
Sibling to TASK-002; both depend on TASK-001 only and neither depends on the
other. Their `Files:` lists are disjoint, so the orchestrator batches them
together. Lives in its own child plan file so `block-dependents` in the
directory-mode cascade routes this task's `blocked` flip to this file while
TASK-002's flip lands in `TASK-002_write_a.md`.
