# TASK-001 — Extract `_dependency_aware_batches` shared helper (single source of truth for topo + file-disjoint batching)

## Goal

Land a single shared batching helper that performs dependency-aware (topo-layered) + file-disjoint partitioning + global-lock solitary placement, so both `_compute_schedule_batches` (`plan_ops.py:436-547`) and `_build_tasks` (`plan_ops.py:2484-2834`) can route through the same logic in TASK-002 and TASK-003. Lifting the in-loop batcher out of `_build_tasks` is the load-bearing first step of the **fix-shape C (canonicalize)** decision documented in this plan's `## Goal`. Adding the helper alone — with its own unit tests — is a no-behavior-change refactor that fails closed if the topo invariant is broken.

## Context

**The failure mode.** Run `20260425T120429` of `PLAN_NESTED_DISPATCH` halted on a binding second `needs-replan` from Codex `plan-review`. Root cause: `SKILL.md` Phase 1 step 3 pipes `_build_tasks`'s topo-correct `batches[]` through `compute-schedule --stdin`, which calls `_compute_schedule_batches` — a function that ignores `tasks[].dependencies` entirely (sort by `priority`/`id`, greedy file-disjoint pack). A serial chain `001 → 002 → 003` whose files are disjoint collapses into a single batch after the recompute. The runtime `batch-next --done <set>` masks the violation at execution time (it enforces deps via `--done`), but the persisted shape is wrong, Codex `plan-review` correctly flags the DAG layering, and the orchestrator halts.

**Why this happens.** Archived `docs/plans/archive/Fix_Depenency_Gate_Plans/TASK-001_plan_ops_strip_dep_gates.md` deliberately stripped the topo logic from `_compute_schedule_batches` on the assumption "each plan file holds one task." That assumption is now violated by every multi-task chunked plan in `docs/plans/` (`POSTMORTEM_FIXES_2026-04-25`, `CODEX_FRICTION_2026-04-25`, `PLAN_GEMINI_INTEGRATION_2026-04-25`, this plan, etc.). The strip was correct for its time; the regression appeared when chunked-plan shape grew multi-task children with declared `Dependencies:`. `_build_tasks` carries an in-loop dep-aware batcher (`plan_ops.py:2786-2825`) that already does the right thing, but it is duplicated logic — when `compute-schedule --stdin` runs after `build-tasks`, the `batches[]` array is overwritten with file-disjoint-only output.

**Why a shared helper rather than two parallel fixes.** Two batchers can drift; one cannot. By extracting the in-loop logic from `_build_tasks` (lines 2786-2825) into `_dependency_aware_batches(tasks, ordered_task_ids)`, both `_compute_schedule_batches` (TASK-002) and `_build_tasks` (TASK-003) can call the same helper. The helper becomes the canonical batching primitive; future callers (filter-schedule re-batch, plan-author re-validation, any downstream consumer) get topo-respect for free.

**Helper signature.** The helper takes (a) the schedule's `tasks[]` (each entry must carry at minimum `id`, `files`, `dependencies`, `priority`) and (b) a list of task ids to schedule. It returns `(batches, errors)` where `batches[]` is the canonical schedule wire shape (`{index, task_ids, file_locks}`) and `errors[]` carries `unresolvable-dep` (orphan dep) and `cyclic-dependency` entries. The caller chooses how to surface those errors (e.g. `_compute_schedule_batches` returns them inline; `_build_tasks` extends its own `errors[]`).

**Scope of THIS task.** Only the helper + its direct unit tests. `_compute_schedule_batches` and `_build_tasks` continue to use their existing logic — TASK-002 and TASK-003 wire them to the helper. Splitting the helper introduction from the call-site rewrites is intentional: it isolates "did the helper compute correct batches in isolation?" from "did wiring it in regress an existing call site?", and lets reviewers diff each step independently.

## Verification

- A new module-level helper `_dependency_aware_batches(tasks, ordered_task_ids)` (or equivalent name) exists in `plugins/plan-executor/scripts/plan_ops.py` and is unit-tested in isolation.
- The helper produces topo-layered batches: tasks sorted into layers by `_compute_decompose_batches` (Kahn's), then each layer partitioned into file-disjoint sub-batches via the same packing rule used today by `_build_tasks` (lines 2799-2825) and by `_compute_schedule_batches` (lines 506-537) — including the global-lock solitary-batch carve-out (`_is_global_lock_path`).
- Cycle detection lives inside the helper (delegating to `_compute_decompose_batches`) and emits a `cyclic-dependency` error structure on cycles.
- Orphan-dep detection lives inside the helper and emits `unresolvable-dep` errors when a `dependencies[]` entry references an id not in `ordered_task_ids`. (Caller may wrap the error to add roster-specific message text — see TASK-003 for the wrapper pattern; this task ships the canonical error code only.)
- Helper batch indexes are 1-based and contiguous (`1, 2, 3, ...`), matching the existing wire format.
- Helper handles the empty-input edge case (`tasks=[]` or `ordered_task_ids=[]`) by returning `([], [])`.
- New unit tests in `tests/scripts/test_plan_ops.py` (new class `TestDependencyAwareBatches`) cover at minimum:
  - Single task → single batch.
  - Independent file-disjoint tasks → single batch.
  - Independent file-overlapping tasks → multiple batches.
  - **Serial chain with disjoint files → N batches in topo order** (the regression-pinning case).
  - Diamond dependency (A → B,C → D where B and C share no files) → 3 batches; B+C co-batch.
  - Single global-lock task → solitary batch.
  - Cycle (`A → B → A`) → returns `cyclic-dependency` error.
  - Orphan dep (`A depends on Z`, Z not in id set) → returns `unresolvable-dep` error.
  - Mixed priority within a layer → higher-priority task fills its sub-batch first (existing `_task_order_key` semantics preserved).
- All existing tests pass unchanged. `_compute_schedule_batches` and `_build_tasks` continue to operate on their existing logic — no call-site rewrite in this task.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "TestDependencyAwareBatches"` returns 0 with at least 8 new tests.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` returns 0 (full file), so the addition does not break neighboring suites.

## Tasks

### TASK-001: Add `_dependency_aware_batches(tasks, ordered_task_ids)` helper + direct unit tests

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "TestDependencyAwareBatches"`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:436-547` — current `_compute_schedule_batches` (file-disjoint only — the broken caller)
  - `plugins/plan-executor/scripts/plan_ops.py:1999-2035` — `_compute_decompose_batches` (topo-only Kahn's helper, reused by the new helper)
  - `plugins/plan-executor/scripts/plan_ops.py:2786-2825` — current `_build_tasks` in-loop dep-aware batcher (the correct logic to lift)
  - `plugins/plan-executor/scripts/plan_ops.py:_is_global_lock_path` — global-lock solitary-batch detector (preserved)
  - `tests/scripts/test_plan_ops.py:1640-1748` — `TestComputeSchedule` (existing harness; new class sits alongside)
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_compute_schedule_batches`
  - `plugins/plan-executor/scripts/plan_ops.py::_build_tasks`
  - `plugins/plan-executor/scripts/plan_ops.py::_compute_decompose_batches`
  - `plugins/plan-executor/scripts/plan_ops.py::_is_global_lock_path`
- **Acceptance criteria:**
  - New module-level helper `_dependency_aware_batches(tasks, ordered_task_ids)` is added to `plan_ops.py` near the existing batchers (recommended: directly above `_compute_schedule_batches`).
  - Helper input contract: `tasks` is a list of dicts each carrying `id`, `files`, `dependencies` (optional, defaults `[]`), `priority` (optional, defaults `"low"`); `ordered_task_ids` is the priority-sorted id list (caller's responsibility to pre-sort).
  - Helper output contract: `(batches: list[dict], errors: list[dict])`. On error, `batches` is `[]`. Error codes used: `unresolvable-dep` (orphan dep) and `cyclic-dependency` (cycle detected).
  - Helper internals: (a) collect `deps_map` from `tasks[]`; (b) call `_compute_decompose_batches(ordered_task_ids, deps_map)` to obtain topo layers; (c) for each topo layer, partition into file-disjoint sub-batches using the packing rule from `_build_tasks:2807-2818` (greedy first-fit), with global-lock tasks (`_is_global_lock_path` over the task's files) forced into solitary sub-batches; (d) flatten sub-batches across layers into the canonical wire shape with monotonically increasing 1-based `index`.
  - Helper does NOT mutate its inputs.
  - Helper does NOT call `_die`, write files, or print — pure function.
  - At least 8 new unit tests in `TestDependencyAwareBatches` covering the cases enumerated under §Verification.
  - The serial-chain regression test (e.g. `test_serial_chain_disjoint_files_yields_n_batches`) constructs three tasks with files `["a.py"]`, `["b.py"]`, `["c.py"]` and dependencies `[]`, `["001"]`, `["002"]`, then asserts `len(batches) == 3` and the order `[["001"], ["002"], ["003"]]`.
  - The diamond test (e.g. `test_diamond_dependency_yields_three_batches`) constructs A (no deps), B and C (both depend on A, file-disjoint), D (depends on B and C), then asserts batches `[["A"], ["B","C"], ["D"]]`.
  - All existing tests in `tests/scripts/test_plan_ops.py` pass unchanged. `_compute_schedule_batches` and `_build_tasks` are not modified by this task — call-site refactors land in TASK-002 and TASK-003.
  - The helper's docstring names the regression it prevents (one-line reference to the post-mortem file path is sufficient).
- **Reversion guidance:** delete the new helper + new test class. No behavior change is induced by this task in isolation, so reverting it cannot affect downstream callers. (Reverting after TASK-002/003 land would break those call sites — handle reversion bottom-up if the cascade ever needs to be undone.)

**Description:**
Lift the dep-aware batching logic that already lives inside `_build_tasks` (`plan_ops.py:2786-2825`) into a free-standing module-level helper `_dependency_aware_batches(tasks, ordered_task_ids)`, and pin its behavior with a focused unit-test class in `tests/scripts/test_plan_ops.py::TestDependencyAwareBatches`. The helper combines `_compute_decompose_batches` (Kahn's topo layering) with the existing greedy file-disjoint packer and the `_is_global_lock_path` solitary-batch carve-out, returning the canonical schedule wire shape `[{index, task_ids, file_locks}, ...]` plus a structured `errors[]` for orphans/cycles. This task is a pure addition — no existing batcher is rewritten yet; TASK-002 and TASK-003 do that wiring once the helper is independently verified. The unit tests carry the regression cases that would have caught the bug if they had existed pre-strip: serial chain with disjoint files, diamond dependency, mixed-priority layer, global-lock solitary placement.

**Implementation notes:**
- Reuse `_compute_decompose_batches` (lines 1999-2035) verbatim for cycle detection — do NOT inline a second Kahn's. The helper's responsibility is composition: topo layers + file-disjoint partitioning + global-lock solitary, layered cleanly.
- The greedy packer in `_build_tasks:2807-2818` is the reference. Copy its shape (don't reinvent) — `for sb in sub_batches: if sb["_files"] & t_files: continue; ...`. Preserve the "first sub-batch with no file overlap" semantics; tasks that don't fit start a new sub-batch.
- Global-lock semantics MUST be preserved: any task whose `files` intersects `_is_global_lock_path` (default `requirements.txt`, `package.json`, `.github/workflows/*.yml`, etc., plus YAML override) gets its own sub-batch — even if file-disjoint with siblings.
- The helper does NOT need to renumber batch indexes across topo layers; it allocates a single counter starting at 1 and increments per sub-batch.
- Don't introduce a new error code beyond `unresolvable-dep` and `cyclic-dependency` — existing schedule consumers know those codes.
- Resist the urge to add a `--strict` mode or any other knob to the helper signature. Keep it pure.

**Reversion guidance:**
Delete the new helper + new test class. No call sites are touched in this task, so reverting affects nothing else.
