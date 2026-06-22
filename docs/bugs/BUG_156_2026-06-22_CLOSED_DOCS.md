---
bug_id: 156
status: CLOSED
group: DOCS
severity: minor
source_fix_id: null
source_plan: null
source_date: '2026-06-22'
origin: BUG-153 reviewer nit (pre-existing)
decomposed_at: '2026-06-22'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/skills/implement-plan/SKILL.md
content_fingerprint: null
change_history: []
---

# BUG-156: Dangling section reference to non-existent heading in SKILL.md

**Status:** CLOSED
**Severity:** minor
**Group:** DOCS
**Depends on:** none
**Test command:** `none`

## Acceptance criteria
  - Fix described in ## Problem is applied and verified.

## Problem

A SKILL.md bullet (~:452) references 'the formula in the Bash-call idioms section', a heading that does not exist; the timeout formula actually lives under the Command idioms section and Dispatch-rules rule 5. Pre-existing — the BUG-153 edit only appended a sentence to this bullet.

## Recommended fix

Re-point the reference to the Command idioms section (or Dispatch rules rule 5, where the implement timeout formula is actually stated).

## Reversion guidance

Revert the changes described in Recommended fix.

## Run history

### Run 20260622T155202 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** plugins/plan-executor/skills/implement-plan/SKILL.md
- **Reviewer verdict:** ship
- **Reviewer advisories:** No in-scope findings (Critical/Major/Minor all none). One pre-existing nit (L450 timeout formula understates inputs) filed separately as a new bug.
