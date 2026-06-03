# Orphan-body decomposer fixture

**Created:** 2026-06-03
**Status:** pending
**Base branch:** main

## Goal

Exercise the BUG-152 residual-body carry-through path: a parent task whose
load-bearing substance is authored OUTSIDE any recognized `**Marker:**`
section must be carried into the decomposed child rather than silently dropped
and masked by the `Auto-filled by decompose-plan` boilerplate.

## Context

TASK-001 carries its real requirement as a numbered-steps body with no
`**Description:**` marker. The decomposer must carry that content through and
surface a loud `residual_body_carried` warning distinct from the benign
`defaults_applied` routine-omission note.

## Verification

After `decompose-plan` runs, the produced child for TASK-001 carries the
numbered steps in its `**Description:**` body (not the auto-fill boilerplate),
and the CLI result reports a `residual_body_carried` warning for TASK-001.

## Tasks

## TASK-001: Persist per-symbol universe snapshot

- **Status:** pending
- **Priority:** high
- **Files:**
  - src/snapshot.py (modify)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/test_snapshot.py`
- **Acceptance criteria:**
  - the per-symbol snapshot is durably persisted across backtest runs.
- **Reversion guidance:** revert src/snapshot.py.

Steps:
1. Add the `universe_snapshot` observation kind.
2. For each symbol in the universe, call
   `record_observation(observation_kind='universe_snapshot', symbol=sym)`.
3. Confirm the snapshot is persisted to the backtest store.
