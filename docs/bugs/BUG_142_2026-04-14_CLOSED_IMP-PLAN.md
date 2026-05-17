---
bug_id: 142
status: CLOSED
group: IMP-PLAN
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
  - .claude/agents/plan-analyst.md
content_fingerprint: null
change_history: []
---

# BUG-142: plan-analyst emits outcome=needs-enrichment with empty gaps when only risks are present

**Status:** CLOSED
**Severity:** minor
**Group:** IMP-PLAN
**Depends on:** none
**Test command:** none

## Acceptance criteria

- A plan that has zero `gaps` entries MUST receive `outcome: valid` from plan-analyst, regardless of how many `risks` entries exist.
- `outcome: needs-enrichment` MUST only be emitted when at least one of the enumerated gap types applies (`stale-path`, `unresolvable-test`, `missing-test-command`, `vague-ac`, `empty-implementation-notes`).
- The analyst spec has an explicit, enforceable rule in Step 8 that makes this impossible to get wrong — e.g., a literal conditional check like "if `gaps` is empty, outcome MUST be `valid`".

## Problem

`.claude/agents/plan-analyst.md` Step 8 (lines 212-222) defines:

> - `needs-enrichment` — structural integrity is fine but one or more gaps exist: stale paths, unresolvable test commands, missing test commands on Claude-tier, vague acceptance criteria, or empty implementation notes on Claude-tier.
> - `valid` — all required fields present, DAG acyclic, classification computable, gap list is empty.

Gap types are enumerated in Step 7 (lines 197-204) as a closed set of 5. `risks` is a separate category (lines 206-210) that does NOT affect outcome.

During `/implement-plan` on `docs/plans/DUAL_AGENT_Plans/TASK-004_scheduler_semantics.md`, the analyst returned `outcome: needs-enrichment` with `gaps: []` but three entries in `risks` (cross-plan dependencies, large scope, implicit shared helpers). None of those map to any gap type per the spec. Per the spec, this should have been `valid`.

The halt caused by this forces the orchestrator to either stop or run with `--allow-gaps`, which conflates "real gaps the analyst flagged" with "analyst misclassified its own output." It also makes the `--allow-gaps` escape hatch feel like a default-on switch, which erodes its signal value.

## Recommended fix

Add an explicit invariant at the top of Step 8:

> **Before selecting outcome, compute `has_gaps = len(gaps) > 0`.**
> - If `has_gaps` is False and no structural failures from Step 2 / Step 5, outcome MUST be `valid`.
> - If `has_gaps` is True, outcome MUST be `needs-enrichment`.
> - Structural failures always take precedence: outcome is `invalid`.
>
> `risks` never affects outcome.

Alternatively (stronger): rewrite Step 8 as a decision tree that has no path from `gaps: []` to `needs-enrichment`.

## Reversion guidance

Revert the Step 8 wording. The validator enforcement in BUG-143 is the downstream backstop if this regresses.

## Run history

### Run 20260414T235613 — CLOSED

- Files: .claude/agents/plan-analyst.md
- Reviewer verdict: ship
- Reviewer advisories: none

### Run 20260414T235613 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** .claude/agents/plan-analyst.md
- **Reviewer verdict:** ship
- **Reviewer advisories:** none
