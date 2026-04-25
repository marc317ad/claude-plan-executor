# TASK-002 — Route `_compute_schedule_batches` through `_dependency_aware_batches`

## Goal

Stop `_compute_schedule_batches` (`plan_ops.py:436-547`) from clobbering topo-correct batches. After this task lands, the `compute-schedule --stdin` CLI subcommand (and any other caller of `_compute_schedule_batches`) returns batches that respect `tasks[].dependencies`. Concretely: a serial chain `001 → 002 → 003` with disjoint files yields three batches in order, not one. The function delegates to the TASK-001 shared helper rather than running its own packing loop. This is the load-bearing code change of the plan; SKILL.md cleanup (TASK-004) and validator hardening (TASK-006) are belt-and-braces, not the primary fix.

## Context

**The current shape.** `_compute_schedule_batches` lives at `plan_ops.py:436-547`. After input validation (id normalization, file-array shape, priority defaulting) it builds `ordered_tasks` sorted by `_task_order_key` and walks the list once, packing each task into the first open batch whose `_files` set is disjoint, otherwise opening a new batch. Global-lock tasks force a solitary batch (`_solitary=True`). Topology / `dependencies[]` is ignored — the function literally does not look at `task.get("dependencies")`.

**The CLI consumer that exposes the bug.** `cmd_compute_schedule` (`plan_ops.py:3995`) reads stdin JSON, calls `_compute_schedule_batches(tasks)`, returns `{tasks, topo, batches, errors, warnings}`. The orchestrator's `SKILL.md` Phase 1 step 3 pipes `_build_tasks`'s topo-correct output through this CLI; the returned `batches` overwrites the in-memory schedule. After TASK-002, that overwrite is a provable no-op (TASK-004 then deletes the redundant pipe; TASK-001's idempotence test pins the no-op contract).

**Why route through the helper rather than inline the topo logic.** A second batching loop is a second place to break. The helper (`_dependency_aware_batches`, TASK-001) is the canonical primitive going forward. `_compute_schedule_batches` keeps its existing input-validation surface (it must continue to reject malformed `tasks[]` with structured per-index errors so CLI consumers get the same diagnostics they get today) and only swaps the batching loop for a helper call.

**`dependencies` becomes a load-bearing input.** Today `_compute_schedule_batches` does not read `tasks[i].dependencies`. After this task it must — but it should validate the field's shape (it must be a list of strings; non-list is `invalid-type`, non-string entries are normalized via `_normalize_task_id` and dropped if invalid). The legacy CLI input contract did not require `dependencies`; absence is treated as an empty list. Tests must pin that absence is silently allowed (back-compat) but a malformed value (e.g. `dependencies: "001"` as a bare string) errors structurally.

**Test surgery required.** `tests/scripts/test_plan_ops.py:1650-1664` (`test_disjoint_files_single_batch`) PINS the bug: it asserts that three tasks `001, 002, 003` with serial deps and disjoint files yield ONE batch. This assertion is wrong — that exact shape is the regression we're fixing. The test must be rewritten to assert the corrected output (three batches, topo order). A new sibling test (e.g. `test_serial_chain_with_disjoint_files_respects_dependencies`) names the regression explicitly so a future grep finds it.

**What this task does NOT touch.** `_build_tasks` (TASK-003), the SKILL.md Phase 1 step 3 invocation (TASK-004), the dead `parallel_batches` field (TASK-005), and the `_validate_schedule_dag` topo-batch defense (TASK-006). Each of those is a separable change with its own task block.

## Verification

- `_compute_schedule_batches(tasks)` returns batches that respect `tasks[].dependencies`. Specifically:
  - Serial chain `001 → 002 → 003` with files `["a.py"]`, `["b.py"]`, `["c.py"]` yields three batches in topo order.
  - Diamond `A; B,C → A; D → B,C` with disjoint files at the B/C level yields three batches: `[A], [B,C], [D]`.
  - Independent file-disjoint tasks (no deps) still co-batch as today (no regression).
  - Global-lock task still occupies a solitary batch (no regression).
- The `compute-schedule --stdin` CLI subcommand reflects the same change. Existing CLI integration tests (`TestComputeSchedule` in `tests/scripts/test_plan_ops.py`) still pass after the targeted assertion rewrites.
- `tasks[i].dependencies` is validated:
  - Absent → treated as empty list (back-compat).
  - Non-list value → `invalid-type` error, same shape as the existing `files` validation.
  - Non-string entries inside the list are normalized via `_normalize_task_id` (consistent with how `_build_tasks` normalizes deps); ids that can't normalize are dropped silently (helper layer surfaces unresolvable deps later).
- Cycles among the input `tasks[]` produce a `cyclic-dependency` error (propagated from the helper); the CLI exits non-zero with the same envelope shape as other validation errors.
- Orphan deps (a `dependencies[]` entry not in the task id set) produce an `unresolvable-dep` error (propagated from the helper); the CLI exits non-zero.
- The existing test `test_disjoint_files_single_batch` is rewritten in place to assert THE CORRECT multi-batch output for a serial dep chain. The test's name is updated only if the new assertion no longer matches the original "single batch" claim.
- New regression test `test_serial_chain_with_disjoint_files_respects_dependencies` (or equivalent) cites the post-mortem (`docs/analysis/PROMPT_compute_schedule_topo_recompute_bug.md`) in its docstring so a future operator can trace the lineage.
- New regression test `test_orphan_dep_returns_unresolvable_dep_error` asserts the structured error code on a malformed dep input.
- New regression test `test_cycle_returns_cyclic_dependency_error` asserts the structured error code on a cyclic dep input.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "TestComputeSchedule or TestDependencyAwareBatches"` returns 0.
- Full suite (`venv/bin/pytest -q tests/scripts/test_plan_ops.py`) returns 0; no neighboring test regression.

## Tasks

### TASK-002: Route `_compute_schedule_batches` through `_dependency_aware_batches` + add dep validation + rewrite the regression-pinning test

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "TestComputeSchedule or TestDependencyAwareBatches"`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:436-547` — current `_compute_schedule_batches` (the function being rewritten)
  - `plugins/plan-executor/scripts/plan_ops.py:3995-4045` — `cmd_compute_schedule` (CLI envelope; verify error pass-through)
  - `tests/scripts/test_plan_ops.py:1640-1748` — `TestComputeSchedule` (the test class to extend + the assertion to rewrite at lines 1650-1664)
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_compute_schedule_batches`
  - `plugins/plan-executor/scripts/plan_ops.py::cmd_compute_schedule`
  - `plugins/plan-executor/scripts/plan_ops.py::_dependency_aware_batches`
- **Acceptance criteria:**
  - `_compute_schedule_batches` retains its existing input-validation surface (id normalization, duplicate-id check, file-array shape check, priority defaulting). The validation loop at `plan_ops.py:441-485` is preserved verbatim for those existing checks.
  - A new validation step is added: `tasks[i].dependencies` (when present) must be a list; non-list value emits a structured `invalid-type` error at `$.tasks[i].dependencies` with the same envelope shape as the existing `files` `invalid-type` error.
  - When `dependencies[]` is present, each entry is normalized via `_normalize_task_id`. The normalized list is added to the `normalized_tasks` entry under `"dependencies"` for the helper consumer.
  - The existing batch-packing loop at `plan_ops.py:498-545` is replaced with a single call to `_dependency_aware_batches(normalized_tasks, ordered_task_ids)`. The helper's returned `batches[]` becomes the function's `batches` return value.
  - The helper's `errors[]` (`unresolvable-dep`, `cyclic-dependency`) are appended to the function's existing `errors` list and returned via the existing third tuple position. CLI envelope shape (via `cmd_compute_schedule`) is preserved byte-for-byte for these new error codes (same `errors[*].path` / `code` / `message` keys).
  - The function's signature `(tasks: list) -> tuple[list[str], list[dict], list[dict]]` is unchanged; no caller signature breaks.
  - `tests/scripts/test_plan_ops.py:1650-1664` (`test_disjoint_files_single_batch`) is rewritten in place: assertion now claims three batches in topo order. The function name MAY be renamed to `test_serial_chain_with_disjoint_files_yields_n_batches` if the rewriter prefers; if renamed, leave a one-line breadcrumb comment at the original line citing the post-mortem.
  - Three new tests in `TestComputeSchedule`: (a) `test_serial_chain_with_disjoint_files_respects_dependencies` (regression pin), (b) `test_orphan_dep_returns_unresolvable_dep_error`, (c) `test_cycle_returns_cyclic_dependency_error`.
  - Existing tests `test_parallel_disjoint_files`, `test_file_conflict_forces_split`, `test_priority_ordering`, `test_alnum_suffix_ordering`, `test_compute_batches_defaults_missing_priority_to_low`, `test_accepts_analyst_json_wrapper` remain green without rewrites — none of them encode a dep-chain shape, so the topo change does not affect their assertions.
  - The global-lock test triplet (`test_compute_schedule_serializes_global_lock_task`, `test_compute_schedule_serializes_two_global_lock_tasks`, `test_compute_schedule_global_lock_respects_dependency_ordering`, `test_compute_schedule_glob_match_serializes`) at `tests/scripts/test_plan_ops.py:1863-1970` remains green.
- **Reversion guidance:** restore the original `_compute_schedule_batches` packing loop (lines 498-545 in pre-task state); revert the rewritten `test_disjoint_files_single_batch` assertion; delete the three new tests. No SKILL.md or fixture changes were made by this task.

**Description:**
Replace the broken in-loop packer in `_compute_schedule_batches` with a delegation to `_dependency_aware_batches` (TASK-001's shared helper), preserving the existing input-validation surface and CLI error envelope. Add `dependencies[]` shape validation to the input-normalization pass, since the helper now consumes it. Rewrite the test that pins the bug (`test_disjoint_files_single_batch`) so it asserts the corrected multi-batch output, and add three regression tests (serial chain, orphan dep, cycle) that name the post-mortem. After this task lands, the `compute-schedule --stdin` CLI subcommand returns topo-respecting batches, and the orchestrator's Phase 1 step 3 recompute is provably a no-op (TASK-001's idempotence test confirms this; TASK-004 then deletes the redundant pipe altogether).

**Implementation notes:**
- The existing validation loop is your reference for the new `dependencies` shape check: same path/code/message convention. Resist the urge to refactor the loop — you are adding ONE new field validation, not redesigning the function.
- `_normalize_task_id` returns `None` on malformed ids. Drop those silently (the helper's orphan check below will surface them as `unresolvable-dep` if they were referenced). Do NOT add a new error code for "dep id failed to normalize" — that decision belongs to the helper layer, not the input-validation layer.
- The helper returns `(batches, errors)` where `errors` may be empty. Append helper errors to the function's `errors` list AFTER the existing input-validation loop (so input-shape errors short-circuit before the helper is called). If `errors` is non-empty after the helper call, return `([], [], errors)` — same shape as today's early-return.
- Keep the `ordered_task_ids` computation (from `_task_order_key`-sorted `normalized_tasks`) — the helper expects pre-sorted ids per its TASK-001 contract.
- The rewritten `test_disjoint_files_single_batch` assertion change is the smallest possible diff: change the expected `batches` value from one batch to three. Don't widen the test's scope; the new sibling tests cover the broader regression surface.
- If you're tempted to update the docstring of `_compute_schedule_batches` to mention dep-respect: do so, in one line. Don't write a new module-level comment block.

**Reversion guidance:**
Restore the original in-loop packer (the lines 498-545 region in the pre-TASK-002 state); revert the dep-shape validation addition; revert the `test_disjoint_files_single_batch` assertion change; delete the three new regression tests.
