# Missing-metadata decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand's default-filling path when a
task block is missing the routinely-omitted `**Priority:**` metadata
field.

## Context

TASK-001 is intentionally missing its `**Priority:**` bullet so the
decomposer can demonstrate its default-fill behavior: rather than halting
the run on a routine omission, it backfills the field with a sensible
default (`medium`) and surfaces the substitution in the result's
`defaults_applied` list. Downstream `_gate_schema_valid` against the
produced child file should still pass, since the renderer always emits
every required bullet.

## Verification

`decompose-plan` returns exit code 0 and a `defaults_applied` list
containing at least one entry with `field: "Priority"` and
`task_id: "001"`. The produced child markdown must satisfy
`_gate_schema_valid` (every required bullet/header present).

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
