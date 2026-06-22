---
bug_id: 157
status: CLOSED
group: DOCS
severity: minor
source_fix_id: null
source_plan: null
source_date: '2026-06-22'
origin: BUG-156 reviewer nit
decomposed_at: '2026-06-22'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/skills/implement-plan/SKILL.md
content_fingerprint: null
change_history: []
---

# BUG-157: Phase B Codex bullet understates timeout inputs vs Dispatch rules rule 5

**Status:** CLOSED
**Severity:** minor
**Group:** DOCS
**Depends on:** none
**Test command:** `none`

## Acceptance criteria
  - Fix described in ## Problem is applied and verified.

## Problem

The Phase B Codex-tasks bullet (L450) says the wrapper 'computes the timeout default from len(task["files"])', implying file count is the only input. Dispatch rules rule 5 (L50) lists five inputs: declared file count, acceptance-criteria count, non-trivial test command, directory-scoped file entries, and complex test/router/parser/state markers. An operator reading the bullet alone would not know to pass a larger --timeout N when ACs or test complexity dominate.

## Recommended fix

Expand the parenthetical to match rule 5's vocabulary, e.g. 'Wrapper computes the timeout default from declared file count, AC count, test complexity, and other task-shape signals per §Dispatch rules rule 5; pass --timeout N to override.'

## Reversion guidance

Revert the changes described in Recommended fix.

## Run history

### Run 20260622T191639 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** plugins/plan-executor/skills/implement-plan/SKILL.md
- **Reviewer verdict:** ship
- **Reviewer advisories:** No findings (Critical/Major/Minor/Nits all none); umbrella phrase 'other task-shape signals' adjudicated accurate against rule 5, no further enumeration needed.
