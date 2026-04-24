# Missing-metadata decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand's loud-error path when a task
block is missing the required `**Priority:**` metadata field.

## Context

TASK-001 is intentionally missing its `**Priority:**` bullet so the
decomposer emits a structured error naming the offending task and the
source line number of its heading.

## Verification

`decompose-plan` returns a non-zero exit code and a structured error
list containing at least one entry with `code:
"missing-required-metadata"` (or equivalent) and `task_id: "001"`.

## Tasks

## TASK-001: Missing priority

- **Status:** pending
- **Files:**
  - scratch/alpha.txt (create)
- **Dependencies:** []
- **Test command:** `test -f scratch/alpha.txt`
- **Acceptance criteria:**
  - `scratch/alpha.txt` exists.
- **Reversion guidance:** `rm -f scratch/alpha.txt`

**Description:**
Task missing its `**Priority:**` bullet. The decomposer should surface a
structured error for this case.
