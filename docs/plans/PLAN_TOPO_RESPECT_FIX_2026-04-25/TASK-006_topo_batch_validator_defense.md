# TASK-006 — Defense in depth: extend `_validate_schedule_dag` to reject batches that violate `dependencies[]`

## Goal

Add a structural check in `_validate_schedule_dag` (`plan_ops.py:648-727`) that emits a new error code `dependency-batch-violation` when `batches[]` places a dependent task in the same or earlier batch as a prereq. This is a defense-in-depth layer: even if both batchers regress in the future (or a hand-crafted schedule sneaks past), the persisted schedule cannot pass `parse-schedule` / `write-schedule` / `schedule-valid` gate validation. The validator catches at the wire-format level what `Codex plan-review` already catches at the LLM-review level — but earlier, deterministically, and as part of the `--strict` validation contract, so the error surfaces before a Codex round-trip is required.

## Context

**Today's `_validate_schedule_dag`.** Lines 648-727 of `plan_ops.py`. It does two things: (1) orphan-dependency detection (each `task.dependencies[j]` must normalize to a known task id) and (2) cycle detection via Kahn's algorithm over the cleaned dep graph. It does NOT check that `batches[]` respects dependencies. So a schedule with `tasks: [A (deps=[]), B (deps=[A])]` and `batches: [{task_ids: [A, B], ...}]` validates fine today — even though the batch shape places dependent + prereq in the same batch, which is what the bug produces.

**Why add this validator after fixing the batchers (TASK-002, TASK-003).** Both batchers will produce topo-correct output after this plan's main tasks. So the validator addition is belt-and-braces — a third independent line of defense. If a future PR reverts the helper, or if a future caller bypasses both batchers (hand-crafting a schedule, importing from a foreign tool, etc.), the validator catches the violation. It is structurally similar to `_validate_schedule_dag`'s existing orphan-dep check: a structural property of the graph that the validator enforces as a hard requirement.

**Algorithm.** For each task, compute its dependencies. For each batch, compute its `task_ids` set. For each pair (dependent, prereq) in the dep graph, find the batch index of each. If `batch_of(prereq) >= batch_of(dependent)`, emit `dependency-batch-violation` with a message naming both ids and both batch indexes. Walk pairs once; emit ALL violations (don't short-circuit on first), so the operator sees the full picture.

**New error code.** `dependency-batch-violation`. The path field follows the existing convention used by `dependency-cycle` and `unknown-dependency`: `$.tasks[i].dependencies[j]` for the offending edge, with the `message` naming `prereq` + `dependent` + `batch indexes`.

**Compatibility with existing schedule files in `docs/plans/`.** Pre-fix in-flight schedule files may carry batches that violate dependencies (the very bug we're fixing). After this task lands, those schedule files would fail validation. **This is desired behavior** — a buggy persisted schedule SHOULD fail validation. The remediation path is to regenerate the schedule via `build-tasks` + `write-schedule` (which post-TASK-002/003 produces topo-correct batches). This task does not need to mass-regenerate existing schedules; the in-flight ones either have already-passed `Codex plan-review` (so they're topo-correct) or will be regenerated on the next run.

**Edge case: batches with extra task_ids not in the schedule's `tasks[]`.** Already handled by `_validate_schedule_refs` (which checks reference integrity). The new check assumes refs are valid; if a batch references an unknown task id, `_validate_schedule_refs` errors first and the new check is short-circuited.

**Edge case: tasks in `tasks[]` not present in any batch.** If a task is declared but unbatched, the new check silently treats its dependencies as unsatisfiable (since there's no batch index to compare against). This is consistent with `_validate_schedule_refs`'s existing behavior — a task missing from batches[] is already a structural problem flagged elsewhere. The new check only fires when both prereq and dependent ARE in some batch and their relative ordering violates the dep.

**Test surface.** Three positive tests (catches the violation), three negative tests (does NOT trip on valid schedules), and one fixture test that takes a real-world buggy schedule (e.g., `docs/plans/CODEX_FRICTION_2026-04-25.schedule.json` if it has the bug — or a synthetic one constructed inline).

**Sequencing.** This task depends on TASK-002 and TASK-003 because the existing test fixtures must produce topo-correct schedules through the full pipeline before the validator change goes live. If the validator tightened first, the test suite would break (some fixtures have schedules that today violate the new check).

## Verification

- `_validate_schedule_dag(tasks, batches)` returns a `dependency-batch-violation` error when `batches[]` places a dependent task in the same or earlier batch as a prereq.
- Error shape: `{path: "$.tasks[i].dependencies[j]", code: "dependency-batch-violation", message: "TASK-{dependent} (in batch {batch_of_dependent}) depends on TASK-{prereq} which is in batch {batch_of_prereq}; dependents must run in a strictly later batch"}`.
- The check fires AFTER orphan-dependency detection (orphans take precedence) and BEFORE cycle detection (cycles are the fallback when topo can't be computed; if a cycle exists, batches that violate deps are a downstream concern). Implementation order inside the function: orphan-dep → batch-topo-violation → cycle-detection.
- `_validate_schedule_dag` does NOT short-circuit on first batch-topo violation — emits ALL violations in a single pass so the operator sees full extent.
- New tests in `tests/scripts/test_plan_ops.py` (recommended class: `TestValidateScheduleDag` if one exists, otherwise create a new class):
  - `test_dag_validator_rejects_dependent_in_same_batch_as_prereq`
  - `test_dag_validator_rejects_dependent_in_earlier_batch_than_prereq`
  - `test_dag_validator_emits_all_violations_not_just_first`
  - `test_dag_validator_accepts_topo_correct_batches` (negative control: no error on a valid schedule)
  - `test_dag_validator_orphan_dep_takes_precedence_over_batch_violation` (orphan-dep error is emitted; batch-violation is NOT emitted for the same edge)
  - `test_dag_validator_unbatched_task_does_not_falsely_trigger` (a task in `tasks[]` but not in any `batches[].task_ids` does not generate a phantom violation)
- Changes to `parse-schedule`, `write-schedule`, `compute-schedule`, `filter-schedule` CLI envelopes follow automatically — they all delegate to `_validate_schedule_dag` and propagate errors transparently. No CLI shape change.
- The `schedule-valid` gate (which calls `_validate_schedule_dag`) gains the new error surface for free.
- Existing tests for `_validate_schedule_dag` (cycle detection, orphan-dep) pass unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "validate_schedule_dag or schedule_valid"` returns 0.
- A new manual run of `parse-schedule` against a deliberately-broken synthetic schedule (e.g. `printf '%s' '{"outcome":"valid",...}' | plan_ops.py parse-schedule --stdin --strict`) where batches violate deps returns a non-zero exit and the new error code in the JSON envelope.

## Tasks

### TASK-006: Add `dependency-batch-violation` check to `_validate_schedule_dag` + tests

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** [002, 003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "validate_schedule_dag or schedule_valid"`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:648-727` — current `_validate_schedule_dag` (the function being extended)
  - `plugins/plan-executor/scripts/plan_ops.py::_validate_schedule_refs` — neighbor validator (reference integrity); confirm error-precedence ordering
  - `plugins/plan-executor/scripts/plan_ops.py::_validate_schedule` — top-level validator; confirm `_validate_schedule_dag`'s errors propagate via existing `errors.extend(...)` call
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_validate_schedule_dag`
  - `plugins/plan-executor/scripts/plan_ops.py::_validate_schedule`
- **Acceptance criteria:**
  - A new code path inside `_validate_schedule_dag` detects when a `tasks[i].dependencies[j]` edge `(dependent, prereq)` is placed such that `batch_of(prereq) >= batch_of(dependent)`. Emits one error per offending edge with code `dependency-batch-violation`.
  - The error path is `$.tasks[i].dependencies[j]` (matching the orphan-dependency error path convention at lines 693-697).
  - The error message names both task ids and both batch indexes: `f"TASK-{dependent} (batch {b_dep}) depends on TASK-{prereq} which is in batch {b_pre}; dependent must run in a strictly later batch"`.
  - Emit ALL violations in a single pass — do not short-circuit. The function's `errors[]` may carry both orphan-dep entries (from the existing pass) and the new batch-violation entries; both are surfaced.
  - Order of validations inside `_validate_schedule_dag`: orphan-dep first (existing), then batch-topo-violation (NEW), then cycle (existing). Inside the new batch-topo block, orphan-dep edges are SKIPPED (the orphan-dep error already names them; emitting `dependency-batch-violation` for an edge whose `prereq` doesn't exist would be redundant noise).
  - When two tasks share a batch and have a cyclic dep between them (`A in batch 1 deps=[B]`, `B in batch 1 deps=[A]`), the existing cycle-detection emits `cyclic-dependency` AND the new check emits two `dependency-batch-violation` entries (one for each edge). Both are surfaced; the operator sees both signals.
  - At least 6 new tests, listed in §Verification, all passing.
  - Existing tests (`tests/scripts/test_plan_ops.py` for `_validate_schedule_dag` orphan-dep + cycle behavior) pass unchanged.
  - The `parse-schedule --strict --stdin` CLI envelope returns non-zero exit + the new error code when fed a synthetic schedule whose batches violate deps. Verify via a subprocess test.
- **Reversion guidance:** delete the new code path inside `_validate_schedule_dag`; delete the six new tests. The existing orphan-dep + cycle behavior reverts to its pre-task form. No fixture data needs changes — buggy schedules that this validator would catch silently regain their pre-task pass-through.

**Description:**
Extend `_validate_schedule_dag` with a structural check that emits `dependency-batch-violation` when `batches[]` places a dependent task in the same or earlier batch as a prereq. The check runs after orphan-dep detection (orphans take precedence) and before cycle detection (cycles are the fallback). It walks every `tasks[i].dependencies[j]` edge and emits one error per violation — never short-circuits, never skips. Adds six tests pinning the positive (rejects bad schedules), negative (accepts good ones), precedence (orphan-dep first), and edge-case (unbatched tasks don't trigger phantoms) behaviors. After this task, any schedule that survives `parse-schedule --strict` / `write-schedule` / `schedule-valid` gate is provably topo-correct at the batch level, even if a future bug in either batcher re-introduces the regression. This is the third independent line of defense against the topo bug; TASK-002 (compute-schedule), TASK-003 (build-tasks), and now TASK-006 (validator) form a triangle of safeguards.

**Implementation notes:**
- The new check is read-only over `tasks[]` and `batches[]` — pure function, no I/O. Slot it into the existing `_validate_schedule_dag` body BETWEEN the orphan-dep block (lines 677-702) and the cycle-detection Kahn's pass (lines 704-727). Don't refactor the existing two blocks; just add the third in between.
- Build a dictionary `batch_of: dict[str, int]` from `batches[]` mapping each `task_id` to its `batch.index`. Use this for O(1) lookup.
- Skip edges where `prereq` or `dependent` is not present in `batch_of` (i.e., the task isn't placed in any batch). A task missing from batches is already a structural problem caught by `_validate_schedule_refs`; don't double-report.
- Skip edges where `prereq` is an orphan (already in the orphan-dep error list from the previous block). Track orphan ids during the orphan-dep pass and reuse the set; don't re-derive.
- The error message MUST be operator-readable; copy the phrasing in §Acceptance criteria verbatim (the message text is part of the contract — operators grep for it).
- Resist adding a `--lenient` mode or a config flag to suppress the new check. Defense-in-depth means it's always on. If a future caller wants to opt out, they bypass `_validate_schedule_dag` entirely; the validator does not negotiate.
- Keep the change inside `_validate_schedule_dag`. Don't add a new validator function. Don't move the orphan-dep or cycle logic. Don't widen scope.

**Reversion guidance:**
Delete the new check block from `_validate_schedule_dag` (revert to the pre-task body of orphan-dep → Kahn's cycle); delete the six new tests. Existing fixture data and other validators are unaffected.
