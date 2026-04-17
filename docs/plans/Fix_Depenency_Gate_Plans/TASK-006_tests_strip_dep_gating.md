# TASK-006 — Strip dep-gating suites and rewrite `filter-schedule` tests

**Base branch:** `main`
**Source plan:** `~/.claude/plans/polymorphic-foraging-scone.md` (section F)

---

## Goal

Update `tests/scripts/test_plan_ops.py` to match the stripped `plan_ops.py`: delete tests that pin cascade-block, dep-cycle, transitive-closure, and orphan-dep behaviors (including `TestBatchNextDagDefense.test_rejects_cycle_in_schedule_file`, the `write-schedule` unknown-dep case, and `test_filter_schedule_cycle_rejected`); rewrite the `filter-schedule` suite for exact-selection semantics; rewrite `test_linear_chain` in `TestComputeSchedule` for file-disjoint single-batch packing; add four new tests that prove isolation (failure does not cascade), orphan-tolerance (filter does not interpret dep references), priority-default (missing/empty/unknown priority collapses to `low`), and positive round-trip tolerance of a legacy `dependencies` field. Inspect `tests/scripts/test_plan_codex_dispatch_parsing.py` to confirm B1's removal of the unread `dependencies` field does not break parse-block tests — no edits expected.

`cmd_check_plan_deps` / `_parse_index_roster` tests are untouched — the cross-plan gate is preserved.

## Verification

1. `venv/bin/pytest -q tests/scripts/` — all surviving tests pass; the four new tests pass. Broader than `test_plan_ops.py` alone so that `test_plan_codex_dispatch_parsing.py` runs against the B1 edit in `plan_codex_dispatch.parse_task_block`.
2. `grep -nE 'test_missing_dependency|test_cycle_detected|test_dependency_respected|block-dependents|test_rejects_dependency_cycle|test_rejects_orphan_dependency|test_dependency_references_accept_suffix|test_dependency_reference_to_missing_suffix_is_orphan|test_rejects_cycle_in_schedule_file|test_filter_schedule_cycle_rejected' tests/scripts/test_plan_ops.py` — zero hits.
3. `grep -n 'test_batch_next_ignores_upstream_failure\|test_filter_returns_exact_selection_even_with_orphan_deps\|test_compute_batches_defaults_missing_priority_to_low\|test_parse_and_write_schedule_tolerate_orphan_dependencies_field' tests/scripts/test_plan_ops.py` — all four names present.
4. `filter-schedule --task-ids 002` on a source with 001+002 returns `tasks=[{id:"002",...}]` and batches restricted to 002; 002's `dependencies` field passes through untouched. `filter-schedule --task-ids 002` with 002 carrying `dependencies: ["999"]` succeeds (no halt). `filter-schedule --task-ids 002` with 002 absent halts with `unknown-task-id`.

---

## Tasks

### TASK-006: Strip dep-gating tests, rewrite filter-schedule suite, add isolation tests

- **Status:** pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops.py`
  - `tests/scripts/test_plan_codex_dispatch_parsing.py` — inspect only; B1 deletes the unread `dependencies` field in `plan_codex_dispatch.parse_task_block`, but the parse-block tests (`test_wrapper_parse_task_block_*`) only assert on `task_id`, `title`, `files`, `test_command`, `description`, so no edits are expected. Flag any assertion against `dependencies` that surfaces during inspection.
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/`
- **Acceptance criteria:**
  - **Deleted tests** (identify by name; line numbers approximate):
    - `test_missing_dependency` (~678).
    - `test_cycle_detected` (~689) in `TestComputeSchedule`. Prior plan drafts called this `test_dependency_cycle`; the real name in the file is `test_cycle_detected` and the grep verification above searches for the real name.
    - `test_dependency_respected` (~787) in `TestBatchNext`.
    - The entire `block-dependents` subcommand suite (~816 onward; also ~2276, ~2295 mutation-independence cases).
    - `test_rejects_dependency_cycle` (~1109) in the `cmd_parse_schedule` / `cmd_write_schedule` suite.
    - `test_rejects_orphan_dependency` (~1125) in the same suite.
    - `test_dependency_references_accept_suffix` (~2142).
    - `test_dependency_reference_to_missing_suffix_is_orphan` (~2167).
    - `TestBatchNextDagDefense.test_rejects_cycle_in_schedule_file` (~1507-1526). After A2 strips the indeg/adj cycle loop from `_validate_schedule_dag`, `batch-next` no longer emits `dependency-cycle`; this test's `assert "dependency-cycle" in codes` would go red. Delete the whole test. The sibling `test_rejects_duplicate_task_id_in_schedule_file` (~1528) and other duplicate/batch structural tests in the same class are kept — those codes survive.
    - `test_does_not_write_on_validation_failure` (~1377-1392) in the `write-schedule` suite. Its payload asserts `"unknown-dependency" in codes`; A2 removes the unknown-dep loop, so that code is no longer emitted. **Preferred:** rewrite the payload and assertion to a surviving structural failure (e.g., give the schedule a batch referencing a missing task-id and assert `"unknown-batch-task-ref" in codes`) so the "write is atomic and does not leave `.tmp`" invariant still gets coverage; rename the test accordingly. **Acceptable alternative:** delete the test if the atomicity invariant is already covered elsewhere in the suite. Do not leave it unchanged.
    - `test_filter_schedule_cycle_rejected` (~2943-2964) in `TestFilterSchedule`. Named explicitly so it is not missed under the generic "any test expecting `missing-dependency` or `dependency-cycle` on the filtered subgraph" bullet in the filter-schedule rewrite block below.
  - **Rewritten `filter-schedule` suite** (~2770 onward) covers exact-selection and keeps pipe-to-`write-schedule` coverage:
    - `filter --task-ids 002` on a source schedule with 001+002 returns `tasks=[{id:"002",...}]`, no 001. Batches intersected to `{002}` only. The 002 task's `dependencies: ["001"]` field passes through untouched.
    - `filter --task-ids 002` on a source where 002 lists `dependencies: ["999"]` (orphan) returns success (not a halt). The filter no longer interprets dep references.
    - `filter --task-ids 002` with 002 absent halts with `unknown-task-id`.
    - Any test expecting transitive prereq inclusion (e.g., `filter 003` pulls in 001+002) is deleted.
    - Any test expecting `missing-dependency` or `dependency-cycle` to surface on the filtered subgraph is deleted — **including `test_filter_schedule_cycle_rejected` (~2943)**, which is also listed in the top-level delete list above. Listed in both places intentionally as a double-check.
    - Pipe tests (`filter | write-schedule`) are kept; expected persisted shape is the exact selection.
    - Structural-rejection tests are kept: duplicate-id, unknown-batch-task-ref, duplicate-batch-index, batch-file-overlap.
  - **Rewritten `TestComputeSchedule` entries:**
    - `test_linear_chain` (~612). Critical: this currently asserts `[batch["task_ids"] for batch in body["batches"]] == [["001"], ["002"], ["003"]]` because the chain `001 → 002 → 003` forces separate dep-levels. After A1 strips dep-based batching, the three tasks (disjoint files `a.py`, `b.py`, `c.py`) collapse into a single batch. Update the assertion to `body["batches"] == [{"index": 1, "task_ids": ["001", "002", "003"], "file_locks": ["a.py", "b.py", "c.py"]}]`. Keep the `assert body["topo"] == ["001", "002", "003"]` assertion — `_task_order_key` still yields that order by numeric-id ascending within priority `high`. Either rename the test (e.g., `test_disjoint_files_single_batch`) or add a one-line comment clarifying that the test now pins priority-ordered, file-disjoint packing — not chain semantics. Without this rewrite the test silently green-lights the wrong behavior.
  - **New test `test_batch_next_ignores_upstream_failure`** in `TestBatchNext`: build a schedule with TASK-001 and TASK-002 where 002 has `dependencies: ["001"]`; pass `--failed 001`; assert 002 appears in the picked batch. Proves cascade removal.
  - **New test `test_filter_returns_exact_selection_even_with_orphan_deps`** in `TestFilterSchedule`: source has 002 with `dependencies: ["999"]`; `filter --task-ids 002`; assert success and `tasks=[002]`.
  - **New test `test_compute_batches_defaults_missing_priority_to_low`** in `TestComputeSchedule` (class at line 602; place the new method alongside the existing `_compute_schedule_batches` unit tests and use the same harness as neighboring tests). Build a task list with four tasks that share disjoint file lists so batching does not interfere: `001` with no `priority` key at all, `002` with `priority: ""`, `003` with `priority: "banana"` (unknown string), and `004` with `priority: "high"`. Assert the first element of the returned tuple (the ordered task-id list) is `["004", "001", "002", "003"]` — `high` leads, and the three low-equivalent tasks sort by numeric id suffix within the `low` band. Proves that missing, empty, and unknown priority strings all collapse to `"low"` and that the second-layer default inside `_task_order_key` is never reached from this code path (the 215–217 normalization has already coerced them to `"low"` before `_task_order_key` sees them).
  - **New test `test_parse_and_write_schedule_tolerate_orphan_dependencies_field`** in the `write-schedule` / `parse-schedule` suite (place it alongside `test_rejects_orphan_dependency` once that sibling is deleted, so the positive and negated contracts live in the same spot). Build a schedule with `tasks=[{"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["999"]}]` and `batches=[{"index": 1, "task_ids": ["001"], "file_locks": ["a"]}]`. Assert `write-schedule` returns `returncode == 0`; read the persisted file back and assert the task literal still carries `"dependencies": ["999"]` byte-identical. Run `parse-schedule` on the same input and assert `returncode == 0` with no error codes mentioning `orphan`, `unknown-dependency`, or `dependency`. Proves the `ALLOWED_TASK_FIELDS` tolerance of a legacy `dependencies` field is a positive, observable contract — not a side-effect of the now-deleted negative tests — and locks in the round-trip guarantee the filter-schedule pass-through relies on.
  - **Untouched (explicitly preserved):**
    - `TestCheckPlanDeps_check_plan_deps` class (2527–2765).
    - `_parse_index_roster` tests (2538, 2702, 2712, 2732, 2742).
    - `test_supersession_cycle_rejected` (2734) — this is the supersession-cycle check inside `_parse_index_roster` in `00_INDEX.json`, unrelated to task-DAG cycles.
    - All fixture schedules that still carry `"dependencies": [...]` inside task literals — the field is tolerated; no edits required.

**Description:**
Align the test suite with the stripped `plan_ops.py` and orchestrator. Delete tests that pinned the removed dep-gating behaviors (including `test_rejects_cycle_in_schedule_file`, the `write-schedule` unknown-dep case, and `test_filter_schedule_cycle_rejected` — three tests that would otherwise go red silently on the new `plan_ops.py`). Rewrite the `filter-schedule` suite for exact-selection. Update `test_linear_chain` in `TestComputeSchedule` so it stops asserting chain-derived batch splits. Add four tests that lock the new guarantees: isolated failure (peers continue after a dep fails), orphan-tolerant filter (the filter does not read `dependencies`), priority-default collapse to `low`, and positive round-trip tolerance of a legacy `dependencies` field through parse-schedule/write-schedule. Also inspect `tests/scripts/test_plan_codex_dispatch_parsing.py` to confirm B1's deletion of the unread `dependencies` field in `parse_task_block` does not break anything — no edits expected.

**Implementation notes:**
Work through the file top-to-bottom. For the `filter-schedule` suite, keep the pipeable-to-`write-schedule` coverage — it's a hard-won regression from prior runs. Do NOT reshape `filter-schedule` stdout in Python before piping to `write-schedule`; use a literal shell pipe via `subprocess.run(..., shell=True)` or two connected `Popen` objects.

**Reversion guidance:**
Revert to HEAD. The deleted tests only pass against the pre-strip `plan_ops.py`; if this file alone is reverted while TASK-001/004 land, the suite will fail the moment cascade-block or transitive-closure tests run against the new behaviors.
