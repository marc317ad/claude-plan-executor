# PLAN - reconcile_batch parallel-batch worktree race + schedule files[] prose drift

**Status:** Pending
**Created:** 2026-05-11
**Base branch:** main

## Goal

Eliminate the false-positive `scope_violation_paused` that fires when two tasks in the same parallel batch each see the union of writes (shared-tree wrapper race), even though every observed path is declared in some batchmate's `Files:` list. The orchestrator runs all parallel tasks in the orchestrator's working tree (worktree isolation is not viable today — Claude's worktree branching always splits off `main`, leaving the worktree behind on commits and producing incomplete implementations). Until that's solved separately, `reconcile_batch` must distinguish "in this task's scope" vs "in some batchmate's scope" vs "genuinely off-plan."

The specific symptom this prevents:

> Both TASK-001 and TASK-002 succeeded with tests passing, but `reconcile_batch` paused them because each task's wrapper saw the OTHER task's in-scope writes as out-of-scope (parallel-batch worktree race; reconciler couldn't auto-preserve because the schedule's `files[]` are descriptive prose strings).

## Findings

### 1. The schedule's `tasks[].files[]` carries raw prose, not normalized paths

`plugins/plan-executor/scripts/plan_ops.py:4028-4046` (in the `build-tasks` per-task emit) sets:

```python
task_entry["files"] = list(parsed.get("files") or [])
```

The block comment at `plan_ops.py:4055-4062` explicitly documents this:

> The helper is fed a projection of `tasks` whose `files[]` entries are normalized through `_normalize_files_entry` ... The returned `tasks[]` retains the raw, annotated `files` entries — only the helper's view is normalized.

The persisted schedule (`.schedule.json`) therefore stores entries like `` `src/foo.py` (modify the cache eviction branch) `` or `src/foo.py — replace L1 fallback` verbatim under `tasks[*].files[]`. Downstream consumers re-normalize (idempotently), with one critical exception: `reconcile_batch`.

### 2. `_normalize_reconcile_scope_entry` is intentionally narrower than the canonical normalizer

`plan_ops.py:4627-4657` strips only the strict word set `(create|modify|delete|edit)` from trailing parentheticals. Its docstring:

> This strips only recognized scope annotations. Unknown parentheticals are left attached so they cannot silently widen a task's allowed scope.

That means a schedule entry like `src/foo.py (modify the bar function)` normalizes to itself (not `src/foo.py`), and the subsequent `_path_in_reconcile_scope("src/foo.py", allowed)` check fails — the path is bucketed `actionable_*` even though it was declared in the task's `Files:` list. The canonical `_plan_paths.normalize_files_entry` (`_plan_paths.py:131`) handles arbitrary trailing parentheticals correctly; the wrapper allowlist and commit guard both use it. Reconcile is the only consumer that diverges.

### 3. `reconcile_batch` is task-scoped, not batch-scoped

`plan_ops.py:4791-4813` builds `allowed = schedule_files_by_task[task_id]` per envelope and partitions kept-vs-actionable against only that single task's `files[]`. When parallel tasks A and B run in the orchestrator's shared working tree and each wrapper's pre-flight `git status` observes the union of in-flight writes, A's envelope reports B's in-scope file as out-of-scope and reconcile flags it `actionable_tracked` because B's `files[]` is not in A's `allowed` set. The reconciler never consults batchmates' scopes. Under the default `pause` policy this fires `scope_violation_paused` on both tasks, freezing a batch in which every observed write is in fact declared by *some* batchmate in `files[]`.

Per-task worktree isolation would dissolve the race at root (each wrapper sees only its own tree), but is out of scope here (see Goal).

### 4. Existing test coverage anchors the touched surfaces

- `tests/scripts/test_plan_ops.py::test_reconcile_batch_*` (six tests around `plan_ops.py:7180-7431`) — pause / partition / nested-scope / missing-schedule paths.
- `tests/scripts/test_plan_ops.py::test_build_tasks_then_compute_schedule_is_no_op_on_batches` (`plan_ops.py:21623`) — pins byte-for-byte equality between `build-tasks`' `batches[]` and `compute-schedule`'s `batches[]` for the same input. Both code paths feed `_dependency_aware_batches` a normalized projection, so this test is unaffected by Fix 1 (which changes `tasks[].files[]`, not `batches[]`).

## Decisions

1. **Normalize `tasks[].files[]` at the source (build-tasks emit).** Use the canonical `_normalize_files_entry` exactly as `_compute_schedule_batches` already does at `plan_ops.py:928`. This collapses the three-tier drift (wrapper canonical / commit guard canonical / reconcile strict / schedule raw) to one tier.
2. **Keep `_normalize_reconcile_scope_entry` as defense in depth.** The schedule will carry clean paths after Fix 1, but the strict normalizer stays so a malformed older schedule (or a hand-edited one) cannot silently widen scope.
3. **Add a batchmate-union pass in `reconcile_batch`.** Compute `batch_allowed = ⋃ schedule_files_by_task[env.task_id] for env in batch_envelopes` once at function start. After the per-task `kept` / `actionable` partition, move any `actionable_*` entry whose path matches `batch_allowed` (but not the task's own `allowed`) into a new `reconcile_kept_batchmate_*` bucket. The outcome stays in the existing `scope_violation_preserved` / `scope_violation_reconciled` family (still ineligible for commit per the task that didn't declare the path), but `scope_violation_paused` does NOT fire when the actionable remainder is empty after batchmate filtering.
4. **No new pause options.** The four-option pause vocabulary (`widen-plan / in-place-fix / keep-and-commit / revert`) is unchanged. The batchmate-spillover case routes through the existing `scope_violation_preserved` outcome (which `commit-task` already refuses) — operator behavior is "review notes show batchmate spillover, no manual pause resolution required."
5. **Red-before-green, strict topo chain.** TASK-001 lands failing tests, TASK-002 (Fix 1) greens the prose-files path, TASK-003 (Fix 2) greens the batchmate-spillover path. All three are serial — no parallel batches, since the very bug we're fixing makes parallel batches dangerous on the shared tree.

## Scope

In scope:

- `plugins/plan-executor/scripts/plan_ops.py` (build-tasks emit, `reconcile_batch` batchmate union, possibly the result-shape docstring)
- `tests/scripts/test_plan_ops.py` (new red regressions + existing reconcile test bundle still green)

Out of scope:

- Per-task worktree isolation. Tracked separately; needs upstream Claude-CLI branch-handling fix.
- Changing `_normalize_reconcile_scope_entry` semantics. Stays strict; Fix 1 makes the strictness moot for fresh schedules without weakening hand-edited / legacy ones.
- Changing the pause vocabulary or `commit-task` semantics.
- `compute-schedule`'s `tasks[]` shape (it already normalizes — no change needed).
- MCP server / schema updates: `reconcile_batch` adds optional output fields (`reconcile_kept_batchmate_tracked`, `reconcile_kept_batchmate_untracked`); the additive shape is backwards-compatible with consumers that ignore unknown keys. If the output JSON schema pins the dict shape strictly, TASK-003 extends it.

## Tasks

### TASK-001: Add red regressions for prose-files bypass and batchmate-spillover

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** none
- **Acceptance criteria:**
  - Add `test_build_tasks_emits_normalized_files_paths`: feed `build_tasks` a synthetic per-child markdown whose `**Files:**` block contains entries with descriptive parentheticals (e.g., `` `src/foo.py` (modify the bar function) ``) and free-prose dash continuations (e.g., `src/baz.py — replace cache fallback`). Assert the returned `tasks[*].files[]` contains only normalized path tokens (`src/foo.py`, `src/baz.py`). Test MUST fail before TASK-002.
  - Add `test_reconcile_batch_batchmate_spillover_is_preserved`: build a schedule with two tasks in one batch (TASK-001 owns `src/a.py`, TASK-002 owns `src/b.py`) and submit two envelopes where each wrapper reports the union (`out_of_scope_observed=true`, `out_of_scope_tracked=["src/a.py","src/b.py"]`). Assert both envelopes' results have empty `actionable_tracked`, non-empty `reconcile_kept_batchmate_tracked`, outcome `scope_violation_preserved` (NOT `scope_violation_paused`). Test MUST fail before TASK-003.
  - Add `test_reconcile_batch_genuinely_off_plan_still_pauses`: build a one-task batch whose envelope reports `out_of_scope_tracked=["src/c.py"]` where `src/c.py` is in no task's `files[]`. Assert outcome is still `scope_violation_paused` (regression: batchmate-union must NOT swallow genuinely off-plan paths).
  - Do not modify or remove any existing `test_reconcile_batch_*` test in TASK-001. TASK-002/TASK-003 may relax fixture prose to satisfy Fix 1's normalization, but the assertion shape (outcome, kept/actionable bucket sizes) must remain semantically equivalent.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "test_build_tasks_emits_normalized_files_paths or test_reconcile_batch_batchmate or test_reconcile_batch_genuinely_off_plan"`
- **Implementation notes:** Two new tests are expected to fail (red); the third (`genuinely_off_plan_still_pauses`) MUST pass on the current code so it acts as a regression anchor against TASK-003 over-relaxing. If `genuinely_off_plan_still_pauses` is red against current code, stop and surface — the model of the existing reconcile path is wrong.
- **Reversion guidance:** Drop the three new test functions. No production code touched.

### TASK-002: Normalize tasks[].files[] at build-tasks emission

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
- **Dependencies:** 001
- **Acceptance criteria:**
  - At the per-task emit site in `build_tasks` (currently `plan_ops.py:4031`), replace `"files": list(parsed.get("files") or [])` with the canonical-normalized projection: `"files": [_normalize_files_entry(str(p)) for p in (parsed.get("files") or [])]`. Mirror exactly what `_compute_schedule_batches` does at `plan_ops.py:928`.
  - Update the surrounding block comment at `plan_ops.py:4055-4062` to record that `tasks[]` now carries the normalized form (not the raw, annotated form), and note that the helper projection at lines 4066-4075 is therefore idempotent. Do not delete the projection — keep it as defense in depth for callers that pass hand-crafted `tasks[]` lists.
  - `test_build_tasks_emits_normalized_files_paths` (added in TASK-001) is now green.
  - `test_build_tasks_then_compute_schedule_is_no_op_on_batches` is still green. The byte-equality is over `batches[]`, which is built from the already-normalized helper projection — Fix 1 only changes the surface-level `tasks[]`, not the batcher input — so this test is unaffected. Confirm by running it; if red, the diagnostic above is wrong and the task halts.
  - All existing `test_reconcile_batch_*` tests still green.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "build_tasks or reconcile_batch or compute_schedule"`
- **Implementation notes:** Single-line code change; the bulk of the diff is the comment update. `_normalize_files_entry` is already imported (used at line 928); no new imports.
- **Reversion guidance:** Restore `"files": list(parsed.get("files") or [])` at the emit site and revert the comment block.

### TASK-003: Add batchmate-union partition to reconcile_batch

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
- **Dependencies:** 001, 002
- **Acceptance criteria:**
  - In `reconcile_batch` (`plan_ops.py:4672`), after `schedule_files_by_task` is built (~line 4745), compute a `batch_allowed: set[str]` as the union of `schedule_files_by_task[env["task_id"]]` for every `env` in `batch_envelopes` whose `task_id` is present in the schedule. If no envelope's task is in the schedule, `batch_allowed` is empty (degrades to today's behaviour).
  - In the per-envelope partition loop (`plan_ops.py:4799-4816`), after the existing `_path_in_reconcile_scope(p, allowed)` check sorts paths into `kept_*` / `actionable_*`, run a second pass over `actionable_tracked` and `actionable_untracked`: any path matching `_path_in_reconcile_scope(p, batch_allowed)` moves into two NEW result fields, `reconcile_kept_batchmate_tracked` and `reconcile_kept_batchmate_untracked`, and is removed from `actionable_*`.
  - The new buckets appear in EVERY result dict (alongside `reconcile_kept_tracked` / `reconcile_kept_untracked`), defaulting to empty lists in the `no_op` and `error` early-returns and in any result where no batchmate spillover was observed.
  - When `out_of_scope_policy == "pause"` and `actionable_tracked` + `actionable_untracked` are BOTH empty after batchmate filtering (even if `reconcile_kept_batchmate_*` is non-empty), the pause branch at `plan_ops.py:4834` does NOT fire — fall through to the legacy preservation path (`scope_violation_preserved`).
  - When `actionable_*` is still non-empty after batchmate filtering (genuinely off-plan remainder), pause fires as today.
  - Update the `reconcile_batch` docstring (`plan_ops.py:4680-4710`) to document the new buckets and the batchmate-union pass. Add one sentence: "Paths that are in NO single task's `Files:` set but appear in SOME batchmate's `Files:` set (the parallel-batch shared-tree worktree race) are preserved into `reconcile_kept_batchmate_*` and never drive pause."
  - If `reconcile_batch`'s output JSON schema pins the result dict strictly (check `plugins/plan-executor/scripts/schemas/mcp/reconcile_batch.output.json` if present), extend it with the two new optional array fields. If the schema accepts arbitrary keys, leave it alone.
  - `test_reconcile_batch_batchmate_spillover_is_preserved` and `test_reconcile_batch_genuinely_off_plan_still_pauses` (TASK-001) are green; all existing `test_reconcile_batch_*` tests still green.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "reconcile_batch"`
- **Implementation notes:** The new buckets are additive — consumers that ignore unknown keys are unaffected. Do NOT rename or remove `reconcile_kept_tracked` / `reconcile_kept_untracked`; downstream commit-task / fail-task logic keys on those.
- **Reversion guidance:** Drop the `batch_allowed` computation, drop the second-pass partition over `actionable_*`, drop the two new buckets from every result dict, revert the docstring sentence, revert any schema extension.

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "reconcile_batch or build_tasks or compute_schedule"`
2. `venv/bin/pytest -q tests/scripts/test_plan_ops.py` (full file — guard against unintended fallout in any sibling reconcile / schedule test)
3. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`

Manual smoke (optional — exercises the shared-tree race end-to-end):

1. Construct a minimal plan with two file-disjoint tasks (TASK-001 owns `src/a.py`, TASK-002 owns `src/b.py`) eligible for one parallel batch.
2. Run `/implement-plan --dry-run` to confirm both tasks land in batch 1.
3. (Non-dry-run) Run with `--parallel 2`; observe `reconcile_batch` results show `reconcile_kept_batchmate_*` populated and outcome `scope_violation_preserved` for any envelope that observed the union — and that the run does NOT halt with `scope_violation_paused`.
