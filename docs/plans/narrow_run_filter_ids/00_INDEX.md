# Plan: Narrow runs against partially-broken rosters (`--task-ids` scope fix)

**Base branch:** `main`
**Status:** Pending

---

## Goal

Fix the executor so `/implement-plan <dir> --task-ids X,Y` succeeds against a roster where unrelated sibling task files have malformed `**Dependencies:**` prose or other body-level defects. Today `build-tasks` runs roster-wide BEFORE `--task-ids` filtering, so a single broken sibling halts every narrow run. We want validation scoped to the requested IDs + their transitive prerequisites; unrelated siblings outside the closure are silently skipped.

## Motivation

Run `20260425T011751` against `docs/plans/DUAL_AGENT_Plans --task-ids 009,017` halted at Phase 1 Step 1 with 20 `unresolvable-dep` errors, none of which were in the filter set. The dependency parser at `plan_ops.py:1839-1845` splits free-form English prose like `TASK-001 (schema) and TASK-007 (audit can then verify ...)` on commas and feeds the pieces to `_normalize_task_id`'s anchored regex; non-matches are kept verbatim and produce doubled-prefix errors like `TASK-TASK-001 (schema) and TASK-007 (audit can then verify new fields are documented`. Half the roster has this prose pattern. A narrow run should not require fixing 20 unrelated children.

## Out-of-scope

- Tightening `_normalize_task_id` to accept commentary. Lower blast radius to keep the parser strict and route around it via the structured roster (`chunks[].depends_on`) instead.
- New schema-version bumps. Backward compatible.
- Auto-cleaning the malformed `**Dependencies:**` prose across the existing plans. That's an authoring task, not an executor task.
- Changing the canonical fixture (`tests/fixtures/directory_mode_plan/sample_phase4.md`). The new broken-sibling fixture lands beside it.

## Verification

The `/implement-plan docs/plans/DUAL_AGENT_Plans --task-ids 009,017 --claude-only --skip-plan-review --skip-cross-review` invocation that originally failed runs to completion (modulo unrelated-to-this-plan halts) once TASK-001 through TASK-004 land. TASK-005 adds a soft-warn audit trail so the deferred prose-cleanup work has a tracked surface.

## Open design questions

The plan bakes in defaults below, but call out before implementation if you want to change them:

1. **Triage scope visibility (TASK-004).** When a scoped run produces warnings inside the closure, should the analyst-triage prompt be told it's seeing a scoped subset? Default: pass warnings verbatim, no metadata.
2. **`unresolvable-dep` envelope semantics (TASK-002).** Should sibling-outside-closure malformed deps surface as a new `unresolvable-dep-out-of-scope` code, or stay silent? Default: silent skip; the audit check from TASK-005 covers visibility.
3. **`build-tasks --filter-ids` flag vs synthetic roster (TASK-002).** Flag-based (chosen) keeps the closure logic single-sourced inside `_compute_index_closure`. Alternative: orchestrator writes a synthetic `00_INDEX.json` to `/tmp` and hands it to vanilla `build-tasks`. Default: flag-based.
4. **Closure-internal strictness override.** Should there be `--task-ids-relaxed` to demote halts when the requested task itself is malformed? Default: no — broken-filter-set runs halt loudly; relaxing invites silent shipping.

## Roster

| ID | Title | Depends on |
|----|-------|------------|
| TASK-001 | Roster-aware closure helper in `plan_ops.py` | — |
| TASK-002 | Add `--filter-ids` to `build-tasks` and gate roster-wide validation | 001 |
| TASK-003 | Broken-sibling fixture and regression tests | 002 |
| TASK-004 | `filter-schedule` + orchestrator wiring (SKILL.md narrow-run path) | 002, 003 |
| TASK-005 | Soft-warn `body-deps-unparseable` + `body_deps_canonical` audit check | 002 |

The pinch-point is TASK-002: it carries the behavior change. TASK-001 isolates the closure helper so unit tests can exercise it without invoking `build-tasks`. TASK-003 lands the fixture before TASK-004 so the smoke test can run end-to-end. TASK-005 is non-blocking polish; can ship in any subsequent run.
