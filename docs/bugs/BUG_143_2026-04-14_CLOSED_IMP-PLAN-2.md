---
bug_id: 143
status: CLOSED
group: IMP-PLAN-2
severity: minor
source_fix_id: null
source_plan: null
source_date: 2026-04-14
origin: surfaced during /implement-plan TASK-004 run 20260414T212013
decomposed_at: 2026-04-14
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
content_fingerprint: null
change_history: []
---

# BUG-143: parse-schedule / write-schedule do not enforce outcome ⟺ gaps invariant

**Status:** CLOSED
**Severity:** minor
**Group:** IMP-PLAN-2
**Depends on:** none
**Test command:** venv/bin/pytest -q tests/scripts/test_plan_ops.py

## Acceptance criteria

- `parse-schedule` rejects any schedule where `outcome == "needs-enrichment"` and `gaps == []` with a clear error.
- `parse-schedule` rejects any schedule where `outcome == "valid"` and `gaps != []` with a clear error.
- `write-schedule` refuses to persist a schedule violating either invariant (shares the same validator).
- `invalid` outcome is unconstrained — structural failures can emit with any `gaps` list on a best-effort basis (per plan-analyst spec line 52).
- Test cases in `tests/scripts/test_plan_ops.py`:
  - `test_parse_schedule_rejects_needs_enrichment_with_empty_gaps`
  - `test_parse_schedule_rejects_valid_with_nonempty_gaps`
  - `test_parse_schedule_accepts_invalid_with_any_gaps`

## Problem

`scripts/plan_ops.py:222-228` (`cmd_parse_schedule`) validates that `outcome` is one of `{"valid", "invalid", "needs-enrichment"}` but does NOT enforce the spec-level invariant from `.claude/agents/plan-analyst.md:212-222`:

- `needs-enrichment` ⟹ `gaps` is non-empty (contains at least one enumerated gap type)
- `valid` ⟹ `gaps` is empty

TASK-002 ("runtime validation") added strict checks for unknown top-level fields, duplicate `id`, orphan dependencies, and dependency cycles (per `.claude/agents/plan-analyst.md:319`), but missed this outcome-gap invariant.

Consequence: a malformed analyst payload (see BUG-142) passes validation and reaches the orchestrator, which then either halts cryptically (no gaps listed) or proceeds with `--allow-gaps` (semantically misleading since no gaps exist).

## Recommended fix

In `cmd_parse_schedule` (and the helper shared with `cmd_write_schedule`), after the existing outcome-value check:

```python
outcome = data.get("outcome")
gaps = data.get("gaps") or []
if outcome == "needs-enrichment" and len(gaps) == 0:
    errors.append({
        "path": "$.outcome",
        "code": "outcome-gap-mismatch",
        "message": "outcome='needs-enrichment' requires at least one entry in gaps[]; got empty list"
    })
if outcome == "valid" and len(gaps) > 0:
    errors.append({
        "path": "$.outcome",
        "code": "outcome-gap-mismatch",
        "message": "outcome='valid' requires gaps[] to be empty; got non-empty list"
    })
```

Fail closed — these are contract violations, not warnings.

Add three regression tests in `tests/scripts/test_plan_ops.py` per the acceptance criteria.

## Reversion guidance

Remove the two new invariant blocks from `cmd_parse_schedule` / shared validator. Delete the three regression tests. No migrations.

## Run history

### Run 20260414T235613 — CLOSED

- Files: scripts/plan_ops.py, tests/scripts/test_plan_ops.py
- Reviewer verdict: ship
- Reviewer advisories: none

### Run 20260414T235613 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** scripts/plan_ops.py, tests/scripts/test_plan_ops.py
- **Reviewer verdict:** ship
- **Reviewer advisories:** none
