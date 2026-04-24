# Unresolvable-deps decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand's unresolvable-dependency
detection. TASK-001 depends on TASK-999 which is not defined.

## Context

Dependency ids that do not match any task heading in the same plan are
a loud error — the decomposer cannot compute `parallel_batches` for a
DAG whose edges point to missing nodes.

## Verification

`decompose-plan` returns a non-zero exit code with at least one error
naming TASK-999 as an unresolvable dependency of TASK-001.

## Tasks

## TASK-001: Has bogus dep

- **Status:** pending
- **Priority:** high
- **Files:**
  - scratch/alpha.txt (create)
- **Dependencies:** [999]
- **Test command:** `test -f scratch/alpha.txt`
- **Acceptance criteria:**
  - `scratch/alpha.txt` exists.
- **Reversion guidance:** `rm -f scratch/alpha.txt`

**Description:**
Depends on TASK-999 which does not exist in this plan. The decomposer
should refuse to emit and surface a structured error.
