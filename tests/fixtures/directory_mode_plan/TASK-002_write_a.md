# TASK-002 — Write scratch/a.txt

**Created:** 2026-04-23
**Status:** pending
**Base branch:** main

## Goal

Write `scratch/a.txt` after TASK-001 has seeded `scratch/`. Files are disjoint
from TASK-003 so the orchestrator batches both into the same parallel batch.

## Context

Second of three children in the TASK-004 directory-mode integration fixture.
Depends on TASK-001 and writes a file that is disjoint from TASK-003's output.
The acceptance test for TASK-004 verifies that `batch-next` returns both
TASK-002 and TASK-003 together when TASK-001 is marked `done` and neither's
`files[]` overlaps.

## Verification

1. `test -f scratch/a.txt` — the leaf file exists after TASK-002 commits.
2. The commit message's `Plan:` trailer names `TASK-002_write_a.md` (this
   child, not any sibling), proving per-task `plan_file` routing fired in
   `commit-task`.

## Tasks

### TASK-002: Write scratch/a.txt

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - scratch/a.txt (create)
- **Dependencies:** [001]
- **Test command:** `test -f scratch/a.txt`
- **Acceptance criteria:**
  - `scratch/a.txt` exists and is a non-empty regular file.
  - Does NOT touch `scratch/b.txt` (file-lock disjointness invariant that
    lets TASK-002 and TASK-003 batch in parallel).
- **Reversion guidance:** `rm -f scratch/a.txt`

**Description:**
Parallel-batch leaf A in the TASK-004 directory-mode integration fixture.
Depends on TASK-001 (scratch seeder) but has no dependency on TASK-003, and
writes a disjoint leaf file so the orchestrator can run TASK-002 and TASK-003
concurrently. Lives in its own child plan file so `commit-task --plan-file
<this-basename>` flips only this file's Status bullet.
