---
bug_id: 149
status: CLOSED
group: DECOMPOSE-CHILD-DROPS-IMPL-NOTES
severity: major
source_fix_id: null
source_plan: null
source_date: '2026-05-30'
origin: Surfaced independently by two Opus-4.8 plan audits (PHASE_0 and PHASE_1 reviewers) of the trading-system pre-earnings pipeline decomposed plans. Auto-decomposed child task files lose the parent plan's per-task Implementation notes; the implementer dispatch then renders impl_notes as "None provided".
decomposed_at: '2026-05-30'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/templates/decomposed_child.md.template
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
content_fingerprint: null
change_history: []
---

# BUG-149: `_DECOMPOSED_CHILD_SCAFFOLD` / `_render_child_task_file` omit the `**Implementation notes:**` section, so auto-decomposed children lose all per-task implementation guidance and the implementer is dispatched with `Implementation notes: None provided`

**Status:** CLOSED
**Severity:** major (silently strips load-bearing implementation guidance from every auto-decomposed child; the implementer runs with `Implementation notes: None provided` even when the parent plan authored detailed notes)
**Group:** DECOMPOSE-CHILD-DROPS-IMPL-NOTES
**Depends on:** none
**Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k "decompose or child or template_drift or render_child" -v`

## Acceptance criteria
- `_render_child_task_file` (plan_ops.py:3231) MUST emit a `**Implementation notes:**` section carrying the source task's implementation-notes body when present, and a stable sentinel (e.g. `none`) when absent — mirroring how `**Reversion guidance:**` is handled UNCONDITIONALLY at lines 3305-3315.
- `_DECOMPOSED_CHILD_SCAFFOLD` (plan_ops.py:3206-3228) MUST include an `**Implementation notes:**${...}` slot (it currently ends at `**Description:**${DESCRIPTION}`).
- The decompose parse MUST capture `implementation_notes` from each parent `### TASK-NNN:` block into the task dict so the render can emit it (confirm in `_parse_task_block` / build-tasks alongside description/acceptance_criteria).
- `TestDecomposedTemplateDrift` MUST assert the rendered child contains `**Implementation notes:**`. The `decomposed_child.md.template` already declares it (lines 54-58); today the runtime scaffold and the template are out of sync and the drift test does not catch it.
- Regression: a parent task with a multi-line Implementation-notes body round-trips into the child verbatim, and `plan_codex_dispatch.render_implement_prompt` renders those notes (not the `None provided` fallback at plan_codex_dispatch.py:465-467).

## Problem
`_DECOMPOSED_CHILD_SCAFFOLD` (plan_ops.py:3206-3228) is:
```
# TASK-${TID} — ${TITLE}
## Goal … ## Context … ## Verification … ## Tasks
### TASK-${TID}: ${TITLE}
${METADATA}
**Description:**${DESCRIPTION}
```
There is no `**Implementation notes:**` slot, and `_render_child_task_file` (3231-3345) assembles only Goal / Context / Verification / metadata / Description. So every auto-decomposed child drops the parent's per-task Implementation notes.

Downstream, `plan_codex_dispatch.py` extracts `implementation_notes` from the (child) task block (`_extract_paragraph(block, "Implementation notes")`, line 432) and `render_implement_prompt` falls back to `"None provided -- follow existing patterns in the target files."` (lines 465-467) — so the Codex implementer is dispatched WITHOUT the notes the parent plan authored.

This contradicts three parts of the system that expect children to carry the notes:
1. `templates/decomposed_child.md.template` declares `**Implementation notes:**` (lines 54-58) and states the analyst "flags empty Implementation notes as an enrichment gap."
2. `plan_codex_dispatch.render_implement_prompt` reads `implementation_notes` and only falls back when it is absent.
3. `_render_child_task_file`'s docstring stresses carrying parent context into the child (it copies the parent `## Context` verbatim) — but never the per-task notes.

## Reproduction (forensic)
- Trading-system repo, plan family `docs/plans/20260527_pre_earnings_pipeline/`. The parent `PHASE_1_preearnings_backend_and_ab.md` authors detailed Implementation notes per task (e.g. TASK-004's SCREEN_GENERATORS constant, the cadence-cache algorithm, and the verbatim per-producer entry-gate code block at `signal_engine.py:1635-1639`).
- After `/implement-plan … --dry-run` auto-promoted each plan to directory mode, all 32 decomposed children across PHASE_0/1/2/4 contain ZERO `**Implementation notes:**` sections (`grep -lE '^\**Implementation notes' …/TASK-*.md` → 0), while the parents retain them (parent TASK-004 → 8 hits, child → 0).
- PHASE_0 TASK-002 carries dangling pointers — "see Implementation notes" / "see the no-fallback note below" — whose referent only exists in the parent, because the source was authored expecting the section to survive decomposition.
- Net effect: dispatching any of these tasks renders `Implementation notes: None provided` to the implementer, dropping the load-bearing recipe (e.g. TASK-001's `--source-interval 10min` flag, without which the backfill returns empty).

## Recommended fix
- Add an `**Implementation notes:**${IMPLEMENTATION_NOTES}` slot to `_DECOMPOSED_CHILD_SCAFFOLD` and populate it in `_render_child_task_file` from `task.get("implementation_notes")`, emitting a `none` sentinel when absent (mirror the unconditional `**Reversion guidance:**` handling at 3305-3315).
- Ensure the decompose parse captures `implementation_notes` into the task dict.
- Extend `TestDecomposedTemplateDrift` to assert the section is present so template/runtime cannot silently diverge again.
- (Optional defence-in-depth) have the implementer dispatch fall back to the PARENT plan's per-task Implementation notes when the child lacks them.

## Notes
- Independently surfaced by two Opus-4.8 reviewers auditing PHASE_0 and PHASE_1.
- Sibling decompose bug filed same day: BUG-148 (fenced multi-line `**Test command:**` ` && `-join corrupts backslash continuations).
- Whether the omission is intentional (a minimal scaffold meant for later enrichment) vs a regression should be confirmed by the maintainer; the template + the dispatch both expecting the section is strong evidence it is unintended.

## Run history
(none yet — bug filed 2026-05-30)

### Run 20260530T210941 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** plugins/plan-executor/scripts/plan_ops.py, plugins/plan-executor/scripts/plan_codex_dispatch.py, tests/scripts/test_plan_ops.py
- **Reviewer verdict:** ship
- **Reviewer advisories:** D.2.5 remediation then re-review=ship (mutation-tested). Load-bearing path (children carry parent notes verbatim) fixed; first-pass absent-notes regression corrected by normalizing the dispatch none/n-a/empty sentinel to the verbose fallback (mirrors test_command line 154). Schema gate untouched. To file: 2 out-of-scope nits (render_review_prompt fallback/sentinel divergence; _parse_task_block docstring missing implementation_notes key). Pre-existing unrelated failures observed: test_plan_ops_mcp_conformance collection error (build-agent-dispatch-prompt fixture) and test_codex_review_prompt d5 SKILL-text assertion.
