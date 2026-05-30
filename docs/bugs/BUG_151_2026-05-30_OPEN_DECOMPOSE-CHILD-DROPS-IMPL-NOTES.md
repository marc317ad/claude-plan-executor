---
bug_id: 151
status: OPEN
group: DECOMPOSE-CHILD-DROPS-IMPL-NOTES
severity: minor
source_fix_id: null
source_plan: null
source_date: '2026-05-30'
origin: BUG-149 reviewer nit (run 20260530T210941)
decomposed_at: '2026-05-30'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/scripts/plan_ops.py
content_fingerprint: null
change_history: []
---

# BUG-151: _parse_task_block docstring omits implementation_notes return key

**Status:** OPEN
**Severity:** minor
**Group:** DECOMPOSE-CHILD-DROPS-IMPL-NOTES
**Depends on:** none
**Test command:** `none`

## Acceptance criteria
  - Fix described in ## Problem is applied and verified.

## Problem

_parse_task_block (plan_ops.py ~L2990-2996) docstring enumerating returned dict keys was not updated to include the newly added implementation_notes key (code returns it at ~L3069; docstring still lists description, reversion_guidance, status).

## Recommended fix

Add 'implementation_notes (str | None)' to the _parse_task_block docstring return-key list for parity with the actual return shape.

## Reversion guidance

Revert the changes described in Recommended fix.
