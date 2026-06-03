---
bug_id: 152
status: CLOSED
group: DCMP-BODY
severity: critical
source_fix_id: manual
source_plan: manual
source_date: '2026-06-03'
origin: Forensic docs/analysis/20260602_universe_snapshot_persistence_nonimplementation.md
  sec4/sec6.1; maintainer decompose-fidelity review (distinct from CLOSED BUG-148/149)
decomposed_at: '2026-06-03'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/scripts/plan_ops.py
- plugins/plan-executor/templates/decomposed_child.md.template
- tests/scripts/test_plan_ops.py
content_fingerprint: sha256:manual-152
change_history: []
---

# BUG-152: decompose drops parent task-body outside named markers; auto-fill hides it

**Status:** CLOSED
**Severity:** critical
**Group:** DCMP-BODY
**Depends on:** none
**Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k 'decompose or render_child or template_drift or orphan_body' -v`

## Acceptance criteria
- A parent task whose load-bearing substance is authored as bare body prose (no Description marker) round-trips that substance into the decomposed child -- carried into the child Description body or an equivalent section -- rather than being replaced by the Auto-filled by decompose-plan boilerplate.
- A parent task whose substance lives under a non-allow-list subsection (for example Steps or Approach) or as numbered/bulleted steps is likewise preserved into the child, or the decompose surfaces a structural error naming the task id -- never silently dropped.
- _decompose_plan emits an operator-visible warning, distinct from the existing benign defaults_applied routine-omission note, whenever a parent block had substantive residual body content not captured by any recognized marker.
- lint_plans flags a task block carrying substantive body content outside recognized markers (or a non-trivial task with an empty Description) before decomposition.
- tests/scripts/test_plan_ops.py asserts the Auto-filled by decompose-plan boilerplate only for a genuinely empty task body, and a new regression covers the bare-prose and Steps-block carry-through; TestDecomposedTemplateDrift stays green with the template mirror updated.
- BUG-148 (test-command continuation) and BUG-149 (implementation-notes carry-through) regressions remain green.

## Problem
`_decompose_plan` (plan_ops.py:3382) builds every child solely from `_parse_task_block` (:2977) -> `_render_child_task_file` (:3256). `_parse_task_block` captures only a fixed allow-list of `**Marker:**` fields (Priority, Dependencies, Files, Acceptance criteria, Agent, Status, Test command, Description, Implementation notes, Reversion guidance). The Description / Implementation-notes bodies come from `_extract_prose_section` (:2955), which matches ONLY an explicit `**Description:**` / `**Implementation notes:**` marker. Any parent task-body substance authored OUTSIDE those markers -- bare prose under the `### TASK-NNN:` heading, numbered/bulleted steps, a `Steps:` / `Approach:` subsection, an embedded code block -- is consumed by nothing and silently dropped.

When the parent block has no `**Description:**` marker, `_render_child_task_file` (:3354-3361) substitutes plausible boilerplate -- "(Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-NNN ...)" -- and records only a benign `defaults_applied` entry (:3552) that the surrounding comment frames as a "routine omission" (:3484). The auto-fill MASKS the loss: the produced child looks complete.

No gate catches it. `lint_plans` (`_run_lint_plans`, :6247) checks only Status done/partial commit-pairing, paused/awaiting-user drift, and `ac_symbol_groundedness` -- there is no Description-presence or orphaned-body check. Test `tests/scripts/test_plan_ops.py:21089` currently PINS the auto-fill (`assert "Auto-filled by decompose-plan" in rendered_empty`) as intended behaviour, so the masking is asserted-correct.

Net effect: a load-bearing P0 step in a parent task can evaporate into a clean-looking decomposed child with no error, no operator-readable data-loss signal, and no failing test. Forensic: `docs/analysis/20260602_universe_snapshot_persistence_nonimplementation.md` (section 4) -- a per-symbol `record_observation(observation_kind='universe_snapshot', ...)` persistence step was dropped from an auto-decomposed child; the child's Description was exactly the auto-fill boilerplate; the requirement was then implemented/reviewed/tested against the de-scoped child and never built across 411 backtest runs.

Distinct from the CLOSED siblings in the same family: BUG-149 added the single `**Implementation notes:**` allow-list slot but did NOT make the decomposer body-complete (substance outside the recognized markers is still dropped); BUG-148 fixed `**Test command:**` backslash-continuation corruption. This bug is the general fidelity gap: the decomposer is an allow-list transformer with no fail-loud path for un-captured parent body content.

## Recommended fix
Make `_decompose_plan` body-complete and fail-loud instead of silently dropping + masking:

1. Residual-body capture/guard. In `_parse_task_block` / `_decompose_plan`, compute each parent task block's "residual" -- the block text with the heading line and every captured `**Marker:**` section removed. If the residual holds substantive content (non-whitespace beyond a small threshold, ignoring pure metadata bullets), either (a) carry it into the child `**Description:**` body (append to / become the captured description) so it survives, or (b) surface a structural error so the run halts rather than dropping it. Prefer (a) for forward bias; reserve (b) for genuinely ambiguous cases.

2. De-trivialize the missing-Description path. At `_render_child_task_file` (:3354) emit a LOUD, operator-visible warning (NOT the benign `defaults_applied` "routine omission" note at :3552) whenever residual body content existed but no `**Description:**` marker was found -- auto-fill over real content is data loss, not a routine default.

3. Pre-decompose lint gate. Add a `lint_plans` (`_run_lint_plans`, :6247) check that flags a task block carrying substantive body content outside recognized `**Marker:**` fields (and/or a non-trivial-priority task with an empty Description), so authors are warned before decomposition.

4. Fix the test that pins the masking. Update `tests/scripts/test_plan_ops.py:21089` so the `Auto-filled by decompose-plan` boilerplate is asserted ONLY for a genuinely empty task body; add a regression where a parent task whose substance is bare prose / a `Steps:` subsection / numbered steps round-trips that content into the child (not replaced by boilerplate).

Mirror any scaffold/parse change into `plugins/plan-executor/templates/decomposed_child.md.template` so `TestDecomposedTemplateDrift` stays green (same lockstep BUG-149 required). Keep edits surgical; do not regress the captured-marker round-trips BUG-148/149 fixed.

## Verification
- Add a unit test in `tests/scripts/test_plan_ops.py` that decomposes a parent task whose load-bearing step is authored as (a) bare body prose with no `**Description:**` marker, and (b) a `Steps:` subsection / numbered steps, and asserts the child carries that content (in `**Description:**` or an equivalent section) -- NOT the `Auto-filled by decompose-plan` boilerplate. Confirm the new regression FAILS on current `main` and PASSES after the fix.
- Confirm `_decompose_plan` returns a loud warning (or structural error) for the no-marker-but-residual case, observable in the CLI/MCP result, distinct from the existing `defaults_applied` note.
- Run the bug Test command; confirm green, and that BUG-148 (test-command continuation) and BUG-149 (impl-notes carry-through) regressions remain green.
- Manual: decompose a minimal plan with a no-`**Description:**` task that has a numbered-steps body; confirm the child carries the steps and the CLI result surfaces the loud warning rather than a routine `defaults_applied` note.

## Reversion guidance
Revert the `_decompose_plan` / `_parse_task_block` / `_render_child_task_file` and `_run_lint_plans` changes in `plugins/plan-executor/scripts/plan_ops.py`, the matching `plugins/plan-executor/templates/decomposed_child.md.template` mirror edit, and the new/updated tests in `tests/scripts/test_plan_ops.py`. No data migration or schema change is involved; reverting restores the prior allow-list-only decompose behaviour. Revert as a unit so the runtime scaffold and the template mirror stay in lockstep (`TestDecomposedTemplateDrift`).

---

## Provenance
- **Source plan:** manual
- **Source fix ID:** manual
- **Original review:** Forensic docs/analysis/20260602_universe_snapshot_persistence_nonimplementation.md sec4/sec6.1; maintainer decompose-fidelity review (distinct from CLOSED BUG-148/149)
- **Consolidation date:** 2026-06-03
- **First decomposed:** 2026-06-03
- **Group:** DCMP-BODY
- **Absorbed from:** none

## Run history

### Run 20260603T020528 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** plugins/plan-executor/scripts/plan_ops.py, tests/scripts/test_plan_ops.py, plugins/plan-executor/skills/implement-plan/SKILL.md, tests/fixtures/decomposer_inputs/orphan_body.md
- **Reviewer verdict:** ship
- **Reviewer advisories:** Re-review post-remediation (GAP-1 H2 lint coverage, GAP-2 SKILL operator-surfacing, M1 fence-after-metadata regression, N1 lockstep warn predicate). All 6 ACs PASS by execution: 43 targeted + 5 BUG-148/149 + 1092 full-suite pass / 0 fail; 7-case warn/carry lockstep sweep zero-divergence; no nits.
