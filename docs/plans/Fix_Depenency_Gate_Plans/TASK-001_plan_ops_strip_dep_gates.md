# TASK-001 — Strip intra-plan dep gates from `plan_ops.py`

**Base branch:** `main`
**Source plan:** `~/.claude/plans/polymorphic-foraging-scone.md` (section A)

---

## Goal

Remove every intra-plan dependency gate from `plugins/plan-executor/scripts/plan_ops.py` so the file ships only one dep gate (`cmd_check_plan_deps`, unchanged) plus file-lock-based batching. Task-order becomes the sole ordering signal; cascade-blocking is gone; `filter-schedule` becomes exact-selection.

## Scoped Context

Six dep-gates exist today: `cmd_check_plan_deps` (kept, cross-plan), `cmd_batch_next._ready`, `cmd_block_dependents`, `cmd_filter_schedule` transitive closure, `_compute_schedule_batches` topo + cycle check, `_validate_schedule_dag` unknown-dep + cycle check. Only `cmd_check_plan_deps` survives — the orchestrator will call it unconditionally at pre-flight (delivered in TASK-004). Each plan file holds one task under the chunked-layout convention, so intra-plan ordering is moot.

The `ALLOWED_TASK_FIELDS` constant (line 90) keeps `"dependencies"` so legacy schedule JSON continues to parse without warning; the field is unread.

## Verification

1. `venv/bin/pytest -q tests/scripts/test_plan_ops.py` — green after TASK-006 test edits land; before that, expect pre-identified failures in the stripped suites only.
2. `grep -n "dependencies\|depends_on" plugins/plan-executor/scripts/plan_ops.py` — hits ONLY inside `_parse_index_roster`, `cmd_check_plan_deps`, the `ALLOWED_TASK_FIELDS` constant, and the `_validate_schedule` tolerated-key loop. Zero hits inside `cmd_batch_next`, `cmd_filter_schedule`, `_compute_schedule_batches`, `_validate_schedule_refs`.
3. `grep -n "block-dependents\|cmd_block_dependents\|_topo_sort" plugins/plan-executor/scripts/plan_ops.py` — zero hits.
4. `grep -rn "_validate_schedule_dag" plugins/plan-executor/ tests/scripts/` — zero hits. The rename is complete; no stale references remain in production code, test code, or test-file comments.

---

## Tasks

### TASK-001: Strip intra-plan dep gates from plan_ops.py

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - `_compute_schedule_batches` (lines 149–286) is simplified: `raw_deps` parsing + per-dep validation (190–213), `"dependencies": deps` normalization (223), known-ids / missing-dependency check (229–239), topo_sort call + residual/cycle emit (241–248), and levels computation (250–254) are deleted. Batch loop (256–284) is rewritten to iterate tasks sorted by `_task_order_key` and place each into the first open batch whose file-lock set is disjoint — start a new batch when none fits. No levels, no topo.
  - Within `_compute_schedule_batches`, the priority normalization at lines 215–217 (`priority = str(task.get("priority", "low")).strip().lower() or "low"` plus the `if priority not in PRIORITY_RANKS: priority = "low"` fallback) is PRESERVED unchanged. Tasks with missing, empty, or unknown priority strings continue to default to `"low"` so `_task_order_key` sorts them in the lowest priority band. The second-layer default inside `_task_order_key` itself (line 109, `PRIORITY_RANKS.get(priority, PRIORITY_RANKS["low"])`) also stays — defense in depth against any future caller that bypasses the normalization.
  - The post-strip shape of each entry in `normalized_tasks` (built around line 219) is `{"id": str, "priority": str, "files": list[str]}` — the `"dependencies"` key (line 223) is removed; the `"priority"` key is retained because `_task_order_key` reads it. `_compute_schedule_batches` still returns the tuple `(ordered_task_ids, batches, errors)`; the first element is now the priority-sorted id list rather than a topo order.
  - `_topo_sort` (115–146) is deleted; no remaining callers.
  - `_validate_schedule_dag` is RENAMED to `_validate_schedule_refs` at its definition (line 289) and at both call sites — inside `_validate_schedule` (line 586) and inside `cmd_filter_schedule` (line 1700). The function loses the unknown-dep loop (324–337) and the indeg/adj + cycle residual emit (353–386). Duplicate-id (292–306), duplicate-batch-index (308–322), unknown-batch-task-ref (339–351), and batch-file-overlap (388–432) checks survive. Rationale: the surviving checks are reference-integrity invariants, not DAG invariants — keeping `_dag` in the name would mislead a future maintainer into reintroducing topological semantics. The stale comment at `tests/scripts/test_plan_ops.py:2944` that references `_validate_schedule_dag` is updated in the same pass (replace with `_validate_schedule_refs`).
  - `_validate_schedule` line 506 required-field loop becomes `for key in ("agent", "files")` (drops `"dependencies"`). Line 90 `ALLOWED_TASK_FIELDS` still contains `"dependencies"` for legacy-JSON tolerance.
  - `cmd_batch_next._ready` (1527–1537) is deleted. Line 1537 becomes `ready = remaining`. File-lock claiming at 1555–1565 stays as the only per-task gate.
  - `cmd_filter_schedule` (1577–1717) collapses to exact-selection semantics: steps 1 (source schedule validation), 2 (source-outcome-valid guard), 3 (task-id parse + normalize — empty fragments still skipped; all-empty input still errors with `invalid-task-ids`), 4 (unknown-requested-id halt with `unknown-task-id`), 7 (post-filter `_validate_schedule_dag`), and 8 (canonical five-key emit) are preserved. Step 5 transitive-closure (1656–1682) is deleted entirely; `closed = set(requested)`. Step 6 (1684–1697) preserves source order: `out_tasks = [t for t in (data.get("tasks") or []) if _tid_of(t) in closed]`; `out_batches` keep each source batch's `task_ids` intersected with `closed`, drop batches that become empty, preserve original `index` values. Task-level `dependencies` fields pass through untouched but unexamined.
  - `cmd_block_dependents` (1939–1977) is deleted.
  - The `block-dependents` argparse subparser registration (~2258) and the `"block-dependents": cmd_block_dependents` dispatch map entry (~2298) are removed. `grep -n "block-dependents" plugins/plan-executor/scripts/plan_ops.py` returns zero hits.
  - `cmd_check_plan_deps` (1229–1331) is UNCHANGED — it is the surviving gate.

**Description:**
Strip five downstream intra-plan dep gates and the cascade-block subcommand from `plan_ops.py`. Leave `cmd_check_plan_deps` untouched — the orchestrator will call it at pre-flight (TASK-004). File-lock-disjoint batching plus `_task_order_key` become the sole scheduling signals.

**Implementation notes:**
Work top-down through the file so later helpers exist when earlier ones are rewritten. Verify the argparse/dispatch edits with a grep pass before committing. The `_validate_schedule` tolerated-key loop and `ALLOWED_TASK_FIELDS` intentionally keep `"dependencies"` as a passthrough field — do not delete it there. Perform the `_validate_schedule_dag` → `_validate_schedule_refs` rename as a single mechanical pass across the definition + 2 call sites + 1 test-file comment (use editor rename-symbol or a targeted `sed`); confirm with the verification #4 grep before committing. Do NOT conflate the rename with the dep-strip logic edits — the rename is literal; the strip touches specific line ranges.

**Reversion guidance:**
Revert this file to HEAD; the orchestrator's pre-flight gate (TASK-004) and test strips (TASK-006) are the compensating edits. If only this file is reverted, the pre-flight gate double-runs dep checks (harmless) and the test suite fails on residual block-dependents assertions (already removed in TASK-006).
