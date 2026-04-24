# Canonical decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand against a well-formed three-task
whole-plan. Every task carries the full required metadata block plus an
ordered acceptance-criteria list, a description paragraph, and explicit
reversion guidance.

## Context

The three tasks form a small DAG (001 seeds, 002 and 003 both depend on
001). This shape is enough to exercise topological sort, parallel-batch
computation, and per-task metadata extraction. All three headings are
H2 (`## TASK-NNN:`) matching the whole-plan grammar spec.

## Verification

After `decompose-plan` runs against this fixture the produced directory
contains `00_INDEX.json` plus three child files
(`TASK-001_seed_scratch.md`, `TASK-002_write_alpha.md`,
`TASK-003_write_beta.md`), each using H3 (`### TASK-NNN:`) sub-headings.

## Tasks

## TASK-001: Seed scratch directory

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - scratch/.gitkeep (create)
- **Dependencies:** []
- **Test command:** `test -d scratch`
- **Acceptance criteria:**
  - `scratch/` exists at repo root.
  - Directory is committed via `scratch/.gitkeep` so git tracks it.
- **Reversion guidance:** `rm -rf scratch`

**Description:**
Create the scratch parent directory that the sibling tasks write into.
Minimal seeder so TASK-002 and TASK-003 can share a common parent
without stepping on each other's file locks.

## TASK-002: Write alpha

- **Status:** pending
- **Priority:** medium
- **Files:**
  - scratch/alpha.txt (create)
- **Dependencies:** [001]
- **Test command:** `test -f scratch/alpha.txt`
- **Acceptance criteria:**
  - `scratch/alpha.txt` exists.
  - File contains the literal string `alpha`.
- **Reversion guidance:** `rm -f scratch/alpha.txt`

**Description:**
Write the alpha leaf file. Exercises the dependency edge (001 → 002)
and the case where `**Agent:**` is omitted — the analyst is supposed to
classify the task at Phase 1.

## TASK-003: Write beta

- **Status:** pending
- **Priority:** low
- **Agent:** codex
- **Files:**
  - scratch/beta.txt (create)
- **Dependencies:** [001]
- **Test command:** `test -f scratch/beta.txt`
- **Acceptance criteria:**
  - `scratch/beta.txt` exists.
  - File contains the literal string `beta`.
- **Reversion guidance:** `rm -f scratch/beta.txt`

**Description:**
Write the beta leaf file. Sibling of TASK-002 — both depend on 001 and
are expected to land in the same parallel batch because their file sets
are disjoint.
