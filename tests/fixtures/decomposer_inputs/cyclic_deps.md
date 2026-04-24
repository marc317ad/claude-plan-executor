# Cyclic-deps decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand's cycle detection. TASK-001
depends on TASK-002 and TASK-002 depends on TASK-001.

## Context

A dependency cycle prevents topological sort and therefore prevents
`parallel_batches` computation. The decomposer must detect the cycle
and surface a structured error rather than looping or emitting a
partial result.

## Verification

`decompose-plan` returns a non-zero exit code with at least one error
of `code: "cyclic-dependency"` (or equivalent) naming both TASK-001 and
TASK-002 in the cycle.

## Tasks

## TASK-001: First in cycle

- **Status:** pending
- **Priority:** high
- **Files:**
  - scratch/alpha.txt (create)
- **Dependencies:** [002]
- **Test command:** `test -f scratch/alpha.txt`
- **Acceptance criteria:**
  - `scratch/alpha.txt` exists.
- **Reversion guidance:** `rm -f scratch/alpha.txt`

**Description:**
First task in the cycle. Depends on TASK-002.

## TASK-002: Second in cycle

- **Status:** pending
- **Priority:** medium
- **Files:**
  - scratch/beta.txt (create)
- **Dependencies:** [001]
- **Test command:** `test -f scratch/beta.txt`
- **Acceptance criteria:**
  - `scratch/beta.txt` exists.
- **Reversion guidance:** `rm -f scratch/beta.txt`

**Description:**
Second task in the cycle. Depends on TASK-001 — closing the cycle back
on the first task.
