# TASK-003 — Route `_build_tasks` through `_dependency_aware_batches` + add idempotence test

## Goal

Replace the in-loop dep-aware batcher inside `_build_tasks` (`plan_ops.py:2786-2825`) with a delegation to `_dependency_aware_batches` (TASK-001's shared helper), and pin the contract that `build-tasks` and `compute-schedule` agree byte-for-byte on `batches[]` for the same input. After this task and TASK-002 land, the orchestrator's Phase 1 step 3 recompute is a provable no-op — which is what authorizes TASK-004 to delete the redundant pipe from SKILL.md.

## Context

**Why touch `_build_tasks` at all when it's already correct?** Two reasons. First, eliminating duplicate batching logic is the whole point of fix-shape C — leaving `_build_tasks`'s in-loop batcher in place reintroduces the drift risk the helper exists to prevent. Second, the in-loop batcher and the helper must produce identical output byte-for-byte, otherwise the SKILL.md cleanup in TASK-004 (which deletes the recompute pipe) depends on a coincidence rather than a guarantee. Routing both call sites through one helper makes the equality structural.

**The current shape inside `_build_tasks`.** After roster + child-file parsing (lines 2569-2757), the function does its own cycle detection (lines 2761-2785) and then a custom in-loop batcher (lines 2786-2825). The custom batcher reads `tasks[].dependencies` (the schedule wire field, plural), groups into topo layers via `_compute_decompose_batches`, then packs each layer into file-disjoint sub-batches. **Crucially, this batcher does NOT honor global-lock solitary placement** — there is no `_is_global_lock_path` call inside `_build_tasks`'s loop. The helper from TASK-001 DOES honor global-lock. So routing `_build_tasks` through the helper is a behavior change for global-lock-bearing roster tasks: they will now get a solitary batch in `_build_tasks`'s output, which previously they did not. This is a strict improvement (it matches `_compute_schedule_batches`'s behavior, which is the single-source-of-truth target).

**Why the existing test passes today.** `test_build_tasks_batches_respect_dependencies` (`tests/scripts/test_plan_ops.py:17216-17247`) asserts that TASK-001 in `directory_mode_plan` lands in an earlier batch than TASK-002 and TASK-003 (which depend on it), and that 002 + 003 share a batch. None of those tasks touch a global-lock file, so the current `_build_tasks` in-loop batcher passes the test even without the global-lock carve-out. After the helper switch, the test continues to pass — the global-lock change is invisible to this particular fixture.

**Idempotence is the load-bearing post-condition.** The end-to-end claim "build-tasks output already has the right batches; compute-schedule's recompute is a no-op" must be PROVABLE — not just "we believe it from reading the code." TASK-003 adds a test that runs `build-tasks` against a non-trivial decomposed plan, pipes the result through `_compute_schedule_batches`, and asserts `batches[]` is bytewise identical. That test is the contract that TASK-004's SKILL.md deletion relies on.

**Error-message preservation.** `_build_tasks` currently emits `unresolvable-dep` errors with the message text `"TASK-{tid} depends on TASK-{dep} which is not declared in the roster"`. The helper emits the more generic `"TASK-{tid} depends on TASK-{dep} which is not in the schedule"`. The roster-specific message is load-bearing for operators reading errors from `build-tasks` (it tells them to look in `00_INDEX.json`'s `chunks[]`). The wiring code in this task must REWRITE the helper's error messages to preserve the roster-specific text — same pattern as the working-directory experimental diff at `plan_ops.py` (which rewrites the message via `for e in batch_errors: if e.get("code") == "unresolvable-dep": e["message"] = ...`).

**What this task does NOT touch.** TASK-002's `_compute_schedule_batches` rewrite, the SKILL.md `compute-schedule --stdin` deletion (TASK-004), the dead `parallel_batches` removal (TASK-005), and the `_validate_schedule_dag` topo defense (TASK-006).

## Verification

- The in-loop batcher in `_build_tasks` (`plan_ops.py:2786-2825`) is replaced with a single call to `_dependency_aware_batches(tasks, [str(t["id"]) for t in tasks])`. The function's output shape is unchanged: `{ok, outcome, tasks, batches, warnings, errors}`.
- The cycle-detection / orphan-dep block at `plan_ops.py:2761-2785` is also delegated to the helper (since the helper performs both checks). The roster-specific error message for `unresolvable-dep` (`"TASK-{tid} depends on TASK-{dep} which is not declared in the roster"`) is preserved by post-processing the helper's `errors[]` before returning.
- Global-lock tasks are now correctly placed in solitary batches by `_build_tasks` — a behavior-change improvement that aligns `_build_tasks` with `_compute_schedule_batches` (and matches the runtime expectation that global-lock files like `requirements.txt` are never co-batched).
- `test_build_tasks_batches_respect_dependencies` (`tests/scripts/test_plan_ops.py:17216-17247`) passes unchanged. (The fixture has no global-lock files, so the global-lock behavior change is invisible there.)
- A new test `test_build_tasks_global_lock_task_is_solitary_in_batch` (or similar) exercises a small synthetic decomposed-plan fixture with one task whose `Files:` includes `requirements.txt` and asserts that task lands in its own batch.
- A new idempotence test `test_build_tasks_then_compute_schedule_is_no_op_on_batches` exercises a non-trivial decomposed plan (recommended fixture: `tests/fixtures/directory_mode_plan/`), runs `build-tasks` via the CLI, pipes the result through `compute-schedule --stdin` via the CLI, and asserts `compute_result["batches"] == build_result["batches"]` byte-for-byte (sorted-key JSON equality).
- All existing tests in `tests/scripts/test_plan_ops.py` pass (full file).
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "build_tasks or build_tasks_then_compute"` returns 0.

## Tasks

### TASK-003: Route `_build_tasks` through `_dependency_aware_batches` + idempotence test + global-lock regression test

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "build_tasks"`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:2484-2834` — current `_build_tasks` (in-loop batcher to replace)
  - `plugins/plan-executor/scripts/plan_ops.py:1999-2035` — `_compute_decompose_batches` (used by helper; verify error-message preservation by re-reading)
  - `tests/scripts/test_plan_ops.py:17216-17247` — `test_build_tasks_batches_respect_dependencies` (existing pin)
  - `tests/fixtures/directory_mode_plan/` — fixture used by the idempotence test
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_build_tasks`
  - `plugins/plan-executor/scripts/plan_ops.py::cmd_build_tasks`
  - `plugins/plan-executor/scripts/plan_ops.py::_dependency_aware_batches`
- **Acceptance criteria:**
  - `_build_tasks`'s body at `plan_ops.py:2761-2825` (the cycle-detection block + the in-loop batcher) is replaced with a single call to `_dependency_aware_batches(tasks, [str(t["id"]) for t in tasks])`. Both topo+file-disjoint batching AND cycle/orphan detection are handled by the helper.
  - The helper's `errors[]` are appended to `_build_tasks`'s own `errors` list. Before appending, any error with `code == "unresolvable-dep"` has its `message` rewritten to the roster-specific form: `f"TASK-{e['task_id']} depends on TASK-{e['dep_id']} which is not declared in the roster"`. (This preserves the operator-facing message contract that pre-existed inside `_build_tasks`.)
  - The function's return shape `{ok, outcome, tasks, batches, warnings, errors}` is unchanged.
  - Global-lock tasks are now placed in solitary batches inside `_build_tasks`'s output (behavior-change improvement aligned with `_compute_schedule_batches`).
  - The existing test `test_build_tasks_batches_respect_dependencies` passes WITHOUT modification.
  - New test `test_build_tasks_global_lock_task_is_solitary_in_batch` constructs a small fixture (preferred: synthesize via `tmp_path` + `decompose-plan` rather than committing a new on-disk fixture) with one task whose `Files:` includes `requirements.txt` and asserts the task is alone in its batch.
  - New test `test_build_tasks_then_compute_schedule_is_no_op_on_batches` runs both CLIs against the existing `tests/fixtures/directory_mode_plan/` fixture and asserts `build_result["batches"] == compute_result["batches"]` (where the comparison uses canonical JSON serialization for byte-equality).
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` (full file) returns 0.
- **Reversion guidance:** restore the original cycle-detection block + in-loop batcher (lines 2761-2825 in pre-task state); revert the helper-error message rewrite; delete the two new tests. The global-lock behavior reverts to the pre-task "no carve-out" form.

**Description:**
Swap `_build_tasks`'s in-loop dep-aware batcher (`plan_ops.py:2786-2825`) for a single call to TASK-001's shared `_dependency_aware_batches` helper, preserving the operator-facing roster error message for `unresolvable-dep` via post-processing of the helper's error list. Add a regression test that pins the global-lock solitary-batch behavior (which `_build_tasks` previously omitted) and an idempotence test that runs `build-tasks` and `compute-schedule` over the canonical `directory_mode_plan` fixture and asserts the two `batches[]` arrays are byte-identical. The idempotence test is the load-bearing pin — it converts the SKILL.md recompute from "presumed no-op" to "machine-checked no-op," which is what authorizes TASK-004 to delete the recompute pipe with confidence.

**Implementation notes:**
- The cycle-detection and orphan-dep blocks at `plan_ops.py:2761-2785` are now redundant with the helper. Delete them entirely; the helper performs both checks. Don't leave them as a "defense in depth" layer — having two cycle detectors guarantees the eventual divergence the helper-merge is designed to prevent.
- Roster-specific error-message preservation is the only post-processing required. Rewrite ONLY `unresolvable-dep`'s `message` field; leave `cyclic-dependency`'s message alone (its current text is already generic and works for both call sites).
- The idempotence test must invoke both CLIs as subprocesses (`subprocess.run([PY, SCRIPT, ...])`) — direct function calls would not exercise the JSON serialization layer where byte-equality matters. Use the existing `_run` helper if present; otherwise mirror the subprocess pattern from the existing CLI tests in the same file.
- Compare batches via `json.dumps(batches, sort_keys=True)` on both sides — this normalizes any incidental key-order difference and pins a strict byte-equality contract.
- The global-lock fixture for the new test does NOT need to be a committed file. Build it inline in the test: write a minimal whole-plan markdown to `tmp_path / "plan.md"`, call `decompose-plan`, then call `build-tasks` and assert the global-lock task's batch.
- Resist the urge to also touch `_compute_schedule_batches` here — that's TASK-002's lane. This task only modifies `_build_tasks`.

**Reversion guidance:**
Restore the original cycle-detection block (lines 2761-2785) and the in-loop batcher (lines 2786-2825) in their pre-TASK-003 form; remove the helper invocation and the error-message rewrite; delete the two new tests.

## Execution log — 20260425T231744 (success)

Starting SHA: `7a9a34fc70347d2f30362de25dc6852170e77858`  → Ending SHA: `cc7a32c01bbe12884f8c97913b35d2ee954e7e58`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003 | claude | codex | clean | 389bd5fc | helper alias + global-lock + idempotence test |
