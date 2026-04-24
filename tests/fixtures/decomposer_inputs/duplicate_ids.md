# Duplicate-ids decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand's duplicate-id detection.

## Context

Two task blocks share the same `TASK-001` id. The decomposer must
refuse to emit and surface a structured error naming both source line
numbers.

## Verification

`decompose-plan` returns a non-zero exit code with at least one error
of `code: "duplicate-id"` (or equivalent) referencing both duplicate
headings' source line numbers.

## Tasks

## TASK-001: First

- **Status:** pending
- **Priority:** high
- **Files:**
  - scratch/alpha.txt (create)
- **Dependencies:** []
- **Test command:** `test -f scratch/alpha.txt`
- **Acceptance criteria:**
  - `scratch/alpha.txt` exists.
- **Reversion guidance:** `rm -f scratch/alpha.txt`

**Description:**
First copy of TASK-001.

## TASK-001: Second

- **Status:** pending
- **Priority:** medium
- **Files:**
  - scratch/beta.txt (create)
- **Dependencies:** []
- **Test command:** `test -f scratch/beta.txt`
- **Acceptance criteria:**
  - `scratch/beta.txt` exists.
- **Reversion guidance:** `rm -f scratch/beta.txt`

**Description:**
Second copy of TASK-001 with the same id. The decomposer should refuse
to emit.
