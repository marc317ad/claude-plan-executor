# Malformed-header decomposer fixture

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Exercise the `decompose-plan` subcommand's rejection of malformed task
headers. The pinned whole-plan grammar is `## TASK-NNN:` (three digits,
optional single alpha suffix). Short ids like `## TASK-1:` must NOT be
silently normalized or dropped — they must surface a structured
`malformed-task-header` error pinned to the offending source line.

## Context

Historically the shared parser accepted one- and two-digit task ids via
an alternation `\d{3}[A-Z]?|\d{1,2}[A-Z]?`, which let malformed plans
slip through as if they were well-formed. The tightened regex now
accepts only `\d{3}[A-Z]?`, and a loose-match pre-scan flags anything
else as a grammar error.

## Verification

`decompose-plan` returns a non-zero exit code with at least one error
whose `code` is `malformed-task-header`, whose `raw_id` is the
one-digit `1`, and whose `source_line` names the offending heading.

## Tasks

## TASK-1: Short id

- **Status:** pending
- **Priority:** high
- **Files:**
  - scratch/short.txt (create)
- **Dependencies:** []
- **Test command:** `test -f scratch/short.txt`
- **Acceptance criteria:**
  - `scratch/short.txt` exists.
- **Reversion guidance:** `rm -f scratch/short.txt`

**Description:**
A task with a one-digit id. The decomposer must reject this with a
`malformed-task-header` error rather than normalize it to `001`.
