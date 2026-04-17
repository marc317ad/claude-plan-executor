---
task_id: "012"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Phase A Step 6a; task_type vocabulary"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-012
depends_on: []
superseded_by: []
change_history: []
priority: medium
---
<!-- prose_decisions: [prose_omitted_self_evident, prose_omitted_trivial_scope] -->

# TASK-012 — Surgical plan-analyst rule: suppress missing-test-command when task_type:gate

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Phase A Step 6a
**Base branch:** main
**Chunk dependencies:** none

---

## Goal

Add ONE rule to `plugins/plan-executor/agents/plan-analyst.md`: when a TASK file's frontmatter contains `task_type: gate`, do NOT emit `missing-test-command` even if `Test command: none`. All other gap detections remain. Add a one-case regression test proving the suppression only fires for gate tasks.

## Verification

- `grep -n "task_type" plugins/plan-executor/agents/plan-analyst.md` returns at least one line referencing the new rule.
- The new test passes; a `task_type: standard` (or absent) fixture with `Test command: none` still reports `missing-test-command`.

---

## Tasks

### TASK-012: Plan-analyst frontmatter-aware gate suppression

- **Status:** open
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/agents/plan-analyst.md` — add single rule: `task_type: gate` suppresses `missing-test-command`
  - `tests/scripts/test_plan_analyst_gate_suppression.py` — new regression test with two fixtures (gate + standard)
- **Dependencies:** none
- **Test command:** `python -m pytest tests/scripts/test_plan_analyst_gate_suppression.py -x`
- **Acceptance criteria:**
  - `plan-analyst.md` documents the rule in plain text next to the existing `missing-test-command` check.
  - Fixture A (`task_type: gate` + `Test command: none`) → `outcome: valid` with NO `missing-test-command` gap.
  - Fixture B (`task_type: standard` + `Test command: none`) → reports `missing-test-command` gap (unchanged behavior).
  - Fixture C (no `task_type` field + `Test command: none`) → reports `missing-test-command` gap (backwards-compat fallback).
  - **Chain, don't overwrite.** If the Step 4 `none`-branch already contains a deferred-testing signal check (the `Test command: none (... TASK-NNN ...)` / `deferred (TASK-NNN)` form that emits a `test-deferred` risk instead of a gap), the new `task_type: gate` suppression MUST be inserted AHEAD of it — do not delete or rewrite the deferred-testing logic. Final decision order: (1) frontmatter `task_type: gate` → suppress; (2) deferred-testing signal → `test-deferred` risk; (3) otherwise → `missing-test-command` gap. Add Fixture D (`task_type: standard` + `Test command: none (deferred to TASK-NNN)` where the reference resolves) asserting `test-deferred` still appears in `risks` and no `missing-test-command` gap fires — proves the chain didn't regress.

**Description:**
Unblocks Phase A's single out-of-plugin change: every gate TASK emitted by the decomposer must pass plan-analyst without `missing-test-command` noise. Backwards-compatible via the absent-field fallback.

**Reversion guidance:**
Revert both the prose edit in `plan-analyst.md` and the new test file.
