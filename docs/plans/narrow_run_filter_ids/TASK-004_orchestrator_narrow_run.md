# TASK-004 — Orchestrator narrow-run wiring (SKILL.md + per-child schema-valid loop)

**Base branch:** `main`
**Chunk dependencies:** TASK-002 (the flag must exist before the orchestrator can call it), TASK-003 (smoke test depends on the broken-sibling fixture)

---

## Goal

Rewrite Phase 0 + Phase 1 prose in `SKILL.md` so the orchestrator computes the closure once and threads it through `index-closure → build-tasks --filter-ids → filter-schedule --stdin`. Scope the per-child schema-valid loop to the closure subset when `--task-ids` is set. Add `build_tasks_scoped` run-log telemetry.

## Scoped Context

The behavior change in TASK-002 is invisible until the orchestrator USES the new flag. This task is the wiring task — it doesn't change `plan_ops.py` semantics, only the SKILL.md prose that drives the orchestrator's invocation order. There are three call sites to update:

1. **Phase 0 per-child schema-valid loop** (SKILL.md ~lines 236-246). Today it iterates `chunks[].file` from `00_INDEX.json`. New behavior: when `--task-ids` is set, iterate the closure subset; otherwise iterate the full roster.
2. **Phase 1 Step 1 build-tasks invocation** (SKILL.md ~lines 268-283). Today it calls `build-tasks --plans-dir <dir> --json`. New behavior: when `--task-ids` is set, prepend an `index-closure --plans-dir <dir> --task-ids <csv>` call to capture the closure, then pass `--filter-ids <closure-csv>` to `build-tasks`.
3. **Phase 1 Step 3 filter-schedule** (SKILL.md ~lines 330-341). Behavior unchanged in code, but the prose needs to clarify that on the new path `tasks[]` arrives pre-filtered to the closure, so `filter-schedule --task-ids <requested-csv>` is a defensive `requested ⊆ closure` check + transitive-prereq drop, not a filter-from-scratch.

### Run-log telemetry (Goal 5 from the original design)

Emit `build_tasks_scoped {filter_ids:[...], closure:[...], skipped_chunk_count:N}` immediately after the scoped `build-tasks` call returns. Add the event to `ALLOWED_LOG_EVENTS` in `plan_ops.py`. The existing `analyst_done` event is unchanged and still emitted after `write-schedule` per today's contract.

### Existing surfaces we touch

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — Phase 0 and Phase 1 prose only. No changes to Phase 2+.
- `plugins/plan-executor/scripts/plan_ops.py` — append `build_tasks_scoped` to `ALLOWED_LOG_EVENTS`. (One-line addition; no other code changes here.)
- `tests/scripts/test_implement_plan_directory_smoke.py` (or equivalent integration test file) — add a scoped-narrow-run scenario.

### Non-goals

- Changing how `filter-schedule` works internally. The behavior is fine; only the prose around it needs updating.
- Introducing a new run-log event for the closure step itself. `build_tasks_scoped` rolls up everything; emitting `index_closure_done` separately is noise.
- Forwarding `--task-ids` to Phase 1.5 Codex plan-review. The reviewer reads the persisted schedule, which already contains only the scoped tasks — no new flag plumbing needed.

## Verification

**V1.** Updated SKILL.md describes the narrow-run path explicitly: when `--task-ids` is set, Phase 0 schema-valid iterates the closure; Phase 1 calls `index-closure` then `build-tasks --filter-ids <closure>`; Phase 1 Step 3 still runs `filter-schedule --stdin --task-ids <requested>` defensively. When `--task-ids` is unset, behavior is byte-identical.

**V2.** A new smoke test against `tests/fixtures/directory_mode_plan_broken_siblings/` invoked with `--task-ids 003 --skip-plan-review --skip-cross-review --dry-run` exits successfully with a printed schedule. Without `--task-ids`, the same invocation halts with `build_tasks_invalid`.

**V3.** The run-log emits a `build_tasks_scoped` event immediately after the scoped `build-tasks` call, with fields `{run_id, plan_file, filter_ids, closure, skipped_chunk_count}`.

**V4.** `build_tasks_scoped` is in `ALLOWED_LOG_EVENTS` (asserted via a unit test that grep's the constant).

**V5.** The Phase 1-triage routing for in-closure warnings is unchanged. Existing `--allow-gaps` / `--analyst-binding` semantics fire the same way; the only difference is that the warning set itself is closure-scoped.

**V6.** Re-running the original failing invocation `/implement-plan docs/plans/DUAL_AGENT_Plans --task-ids 009,017 --claude-only --skip-plan-review --skip-cross-review --dry-run` against the live `DUAL_AGENT_Plans` directory now reaches Phase 1 Step 4's `schedule-valid` gate. (The full execute path may halt later for other reasons unrelated to this plan, but the originally-reported `unresolvable-dep` halt is gone.)

---

## Tasks

### TASK-004: Orchestrator narrow-run wiring

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/scripts/plan_ops.py` (single-line `ALLOWED_LOG_EVENTS` append)
  - `tests/scripts/test_implement_plan_directory_smoke.py` (or the closest existing smoke-test file)
- **Dependencies:** TASK-002, TASK-003
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py -k "narrow or scoped"`
- **Acceptance criteria:**
  - V1–V6 pass.
  - SKILL.md edits are localized to Phase 0 (~lines 236-246) and Phase 1 (~lines 268-356). No other phase prose touched.
  - The new prose explicitly addresses the four open design questions from `00_INDEX.md` with the chosen defaults (silent skip, flag-based, no relaxed mode, no triage scope-aware metadata).
  - `ALLOWED_LOG_EVENTS` gains exactly one entry: `"build_tasks_scoped"`. No reordering of existing entries.
  - The smoke test materializes the broken-siblings fixture (TASK-003), invokes `/implement-plan` via the same harness as existing smoke tests, and asserts (a) the dry-run exit success, (b) the printed schedule includes the closure tasks only, (c) the run-log written to a temp dir contains a `build_tasks_scoped` event.
  - SKILL.md edits avoid changes to surrounding line numbers — append new conditional branches inside the existing Phase prose blocks rather than restructuring them, so future skill diffs stay reviewable.

**Description:**

This is a wiring task. The actual semantic change is in TASK-002; this task makes the orchestrator USE it. The SKILL.md edits are surgical — preserve existing prose, add `**When `--task-ids` is set:**` sub-paragraphs that override the default invocation. Concrete addition under Phase 1 Step 1:

> **When `--task-ids <csv>` is set:** before invoking `build-tasks`, capture the closure:
>
> ```bash
> $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" index-closure \
>   --plans-dir <plans_dir> --task-ids <csv> --json
> ```
>
> Halt on closure errors (`unknown-requested-id`, `closure-malformed-dep`, `duplicate-roster-id`). Then invoke `build-tasks` with `--filter-ids <closure-csv>` (NOT `<requested-csv>` — the closure includes transitive prereqs):
>
> ```bash
> $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" build-tasks \
>   --plans-dir <plans_dir> --filter-ids <closure-csv> --json
> ```
>
> Capture the `scope` key from the result and emit `build_tasks_scoped`:
>
> ```bash
> $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
>   --event build_tasks_scoped \
>   --fields-json '{"run_id":"<id>","plan_file":"<dir-basename>","filter_ids":[...],"closure":[...],"skipped_chunk_count":<N>}' --json
> ```
>
> Validation behavior on the scoped path: in-closure children parse strictly (today's behavior); skipped chunks contribute zero tasks/warnings/errors. `--allow-gaps` and `--analyst-binding` apply only to in-closure warnings.

The Phase 0 schema-valid loop edit is similarly conditional:

> **When `--task-ids <csv>` is set:** iterate the closure (computed once via `index-closure` and reused) instead of `chunks[].file`. The narrow run should not halt on a sibling's schema breach if the sibling is outside scope.

**Implementation notes:**

- Cache the `index-closure` result in the orchestrator's working state so Phase 0 schema-valid + Phase 1 Step 1 don't both call it. (The skill is procedural prose, not code; "cache" here means "describe the call once and refer back to its output in subsequent steps.")
- The `build_tasks_scoped` event payload reuses the result's `scope` dict; no new computation.
- Keep `analyst_done` after `write-schedule` per today's contract — `build_tasks_scoped` is additive, not a replacement.
- The smoke test should NOT couple to the exact line numbers in SKILL.md; assert behavior, not prose.

**Reversion guidance:**

`git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_implement_plan_directory_smoke.py`. The new SKILL.md branches are conditional, so reverting them collapses the orchestrator back to today's invocation order without affecting full-roster runs.
