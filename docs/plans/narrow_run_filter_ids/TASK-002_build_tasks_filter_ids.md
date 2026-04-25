# TASK-002 — `build-tasks --filter-ids` and scoped roster validation

**Base branch:** `main`
**Chunk dependencies:** TASK-001 (closure helper is the seam)

---

## Goal

Plumb `--filter-ids <csv>` through `cmd_build_tasks` → `_build_tasks(plans_dir, *, filter_ids: set[str] | None = None)` so a narrow run validates only the requested IDs + their transitive prerequisites. Default-mode (no flag) MUST be byte-identical to today's behavior.

## Scoped Context

This is the load-bearing behavior change. When `filter_ids` is set:

1. Compute the closure via `_compute_index_closure` (TASK-001).
2. Skip every chunk whose normalized `task_id` is not in the closure — no per-child parse, no warnings, no errors for skipped chunks.
3. The closing `unresolvable-dep` check (today at `plan_ops.py` ~line 2642) trusts the **roster's** `depends_on` for in-closure tasks: a child body that says `**Dependencies:** TASK-001 (schema) and TASK-007 (audit ...)` no longer surfaces parser garbage as long as the roster says `"depends_on": ["001"]`. The body's malformed deps surface as a per-task `body-deps-unparseable` warning instead (TASK-005 wires the audit-side companion).
4. Surface a top-level `scope` key on the result so the orchestrator can record what was scoped: `{filter_ids: [...sorted], closure: [...sorted], skipped_chunk_count: <int>}`.

### Existing surfaces we touch

- `plugins/plan-executor/scripts/plan_ops.py` — modify `_build_tasks` (~lines 2371-2715), `cmd_build_tasks` (~lines 2718-2738), and the argparse registration (~lines 8371-8384).
- `tests/scripts/test_plan_ops.py` — extend `TestBuildTasks` with broken-sibling and filter-ids cases (depends on the fixture from TASK-003 for the end-to-end smoke; can land first using inline-string fixtures for the unit cases).

### Non-goals

- Changing the parser strictness. `_normalize_task_id` stays anchored.
- Changing the existing `unresolvable-dep` envelope shape for full-roster runs. Default mode must regress zero tests.
- Wiring the orchestrator to USE the new flag — that's TASK-004.

## Verification

**V1.** `_build_tasks(plans_dir)` with `filter_ids=None` is byte-for-byte identical to today. The full existing `TestBuildTasks` suite (28+ cases) passes unchanged.

**V2.** `_build_tasks(plans_dir, filter_ids={"009"})` against a fixture with broken siblings outside the closure returns `ok: true` with `tasks[]` containing only TASK-009 + transitive prereqs.

**V3.** The result includes `scope: {filter_ids: ["009"], closure: ["001", "002", "009"], skipped_chunk_count: <N>}` when `filter_ids` is set; the key is absent on full-roster runs.

**V4.** A child whose body has malformed `**Dependencies:**` prose BUT whose roster `depends_on` is well-formed AND in-closure surfaces a non-fatal `body-deps-unparseable` warning, NOT an `unresolvable-dep` error. The build still succeeds.

**V5.** `_build_tasks(plans_dir, filter_ids={"002"})` where TASK-002 is itself the broken child surfaces the per-child error (`unresolvable-dep` against TASK-002's own body) and halts. Filter-set-internal breakage is loud.

**V6.** `_build_tasks(plans_dir, filter_ids={"999"})` (unknown id) halts with closure-level `unknown-requested-id`.

**V7.** New CLI invocation `python plan_ops.py build-tasks --plans-dir <d> --filter-ids 009,017 --json` returns scoped output (`scope` key present, `tasks[]` filtered, exit 0).

**V8.** Cycle detection inside the closure subset still fires. Cycles entirely outside the closure are silenced.

---

## Tasks

### TASK-002: `build-tasks --filter-ids` and scoped roster validation

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "BuildTasks"`
- **Acceptance criteria:**
  - V1–V8 pass.
  - `_build_tasks` keyword-only `filter_ids` parameter; default `None` preserves today's behavior verbatim.
  - When `filter_ids` is set, `_build_tasks` first calls `_compute_index_closure` from TASK-001; closure errors halt before any child markdown is read.
  - Skipped chunks (those outside the closure) contribute zero tasks, zero warnings, zero errors. Their child files are never opened.
  - In-closure deps are validated against the **closure set** (which is derived from the roster). A dep that resolves via the roster but cannot be parsed from the body becomes a `body-deps-unparseable` warning; a dep that doesn't resolve via the roster is still a hard `unresolvable-dep` error.
  - The `scope` key is present on the result iff `filter_ids` is non-None.
  - Cycle detection: closure-internal cycles surface; cycles touching skipped chunks do not (a cycle that crosses the closure boundary is by definition closure-internal once you walk the dep, so this case is structurally rare; document the edge case in a one-line code comment).
  - Argparse registers `--filter-ids` (csv-of-task-ids) on `build-tasks` only. Reuse the same csv-parsing helper that `--task-ids` uses on `filter-schedule`.
  - At least 6 new tests in `TestBuildTasks`: happy filter, transitive filter, broken-sibling-outside-closure (silent), malformed-body-deps-inside-closure (warning), broken-task-itself (halt), unknown filter id (halt). The fixture from TASK-003 powers the end-to-end smoke; inline string fixtures power the unit cases that don't need it.

**Description:**

The shape of `_build_tasks` after this change:

```python
def _build_tasks(plans_dir: Path, *, filter_ids: set[str] | None = None) -> dict:
    chunks = _load_index_chunks(plans_dir)
    if filter_ids is not None:
        closure_ids, closure_errors = _compute_index_closure(chunks, filter_ids)
        if closure_errors:
            return {"ok": False, "errors": closure_errors, ...}  # halt
        chunks_in_scope = [c for c in chunks if _normalize_task_id(c["task_id"]) in closure_ids]
        skipped = len(chunks) - len(chunks_in_scope)
    else:
        chunks_in_scope = chunks
        closure_ids = None
        skipped = 0

    # ... existing _parse_task_block walk over chunks_in_scope only ...
    # ... existing closing _validate_dependencies block, but trust roster deps when filter_ids is set ...

    result = {"ok": ..., "tasks": [...], "warnings": [...], "errors": [...], "batches": [...]}
    if filter_ids is not None:
        result["scope"] = {
            "filter_ids": sorted(filter_ids),
            "closure": sorted(closure_ids),
            "skipped_chunk_count": skipped,
        }
    return result
```

The trust-the-roster behavior on in-closure deps: when scoring a dep that doesn't normalize from the body's `**Dependencies:**` line, check if the chunk's roster-side `depends_on` contains anything that resolves to an in-closure task. If yes, the dep is satisfied via the roster — surface as `body-deps-unparseable` warning. If no, the dep is genuinely unresolvable — surface as `unresolvable-dep` error.

**Implementation notes:**

- The `body-deps-unparseable` warning shape: `{code: "body-deps-unparseable", task_id, plan_file, message: "<offending Dependencies line>", roster_deps: [<sorted closure ids from roster>]}`. The `roster_deps` field gives the operator the canonical replacement at a glance.
- Do NOT change the body-deps parser at `_extract_bullet_list`. The point of this task is to route around it via the roster, not to make it smarter.
- Argparse: csv-of-task-ids should accept `"9,17"`, `"009,017"`, `"TASK-009,TASK-017"`, and mixed forms. Normalize each via `_normalize_task_id` before passing to the helper.
- Default-mode regression guard: add one explicit test asserting that omitting `--filter-ids` produces a result identical (modulo timestamps if any) to a synthetic baseline pickled from the current implementation.
- The `scope` key MUST be absent (not `null`) on full-roster runs so consumers can check `"scope" in result` cleanly.

**Reversion guidance:**

`git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`. The change is internal to `_build_tasks`; rolling back leaves TASK-001's helper and CLI in place but unused.
