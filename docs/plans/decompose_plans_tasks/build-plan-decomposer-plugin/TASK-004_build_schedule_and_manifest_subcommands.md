---
task_id: "004"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "CLI Surface items 3, 6; Agent Spec step 10; Context Budget Rules"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-004
depends_on: ["003"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-004 — Implement build-schedule + manifest subcommands

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** CLI Surface items 3, 6; Context Budget Rules
**Base branch:** main
**Chunk dependencies:** 003

---

## Goal

Add `build-schedule` (the single canonical intermediate object every renderer consumes) and `manifest` (init / atomic commit). `build-schedule` fuses parsed tree + manifest + complexity scoring + topological sort + Kahn ID assignment + cycle detection into ONE output — no downstream phase re-derives ordering.

## Scoped Context

Step 10 in the Agent Spec requires that all renderers consume the OUTPUT of a single `build-schedule` call — this is the explicit anti-drift fix (Codex critical #3). The schedule object shape: `{tasks: [...], batches: [...], supersedes: [], new_parent_plan_copy: bool, warnings: [], split_reasons: [], prose_decisions: []}`.

Complexity scoring uses `CEILING_*` constants from TASK-002. Tie-breaker priority: `critical(0) < high(1) < medium(2) < low(3)`, then source-order index. Matched tasks preserve existing IDs; only `new_pending` consume `manifest.next_id++`.

Supersede mode (triggered via `--supersede-parent NNN`) produces sub-tasks `NNNA..NNNE`, caps depth at 1, inherits parent `depends_on`, and rejects parents already in `Superceeded / Done / Cancelled` with `supersede-illegal-state` + `supersede-depth-exceeded` when parent id already matches `^\d{3}[A-Z]$`.

`manifest commit` MUST write via temp-file + `os.replace` (atomic). Corrupt stdin MUST leave the on-disk manifest untouched.

## Verification

- `build-schedule --parsed-file <p> --manifest-file <m> --json` produces deterministic output (same inputs → same bytes).
- Cycle-injected parsed tree → exit non-zero with `ring` in JSON, no writes.
- `manifest --action init` → empty `{schema_version:1, next_id:1, fingerprints:{}, slug:"", history:[]}`.
- Feeding corrupt JSON to `manifest --action commit` leaves the target file bytes unchanged.

---

## Tasks

### TASK-004: Implement build-schedule + manifest subcommands

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add `build_schedule` handler (+ complexity scorer, Kahn sort, cycle detector, supersede branch) and `manifest` handler
- **Dependencies:** 003
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "build_schedule or manifest_" -x`
- **Acceptance criteria:**
  - `build-schedule` emits one canonical schedule object containing `tasks`, `batches`, `supersedes`, `warnings`, `split_reasons`, `new_parent_plan_copy`.
  - Ordering is Kahn's topological sort with priority tie-break then source-order; stable across runs.
  - Every `split_*` / `merged_*` decision records a reason code from `PROSE_REASON_CODES`-adjacent split/merge code set (`split_ceiling_files`, `split_ceiling_loc`, `split_new_module`, `split_cross_cutting`, `split_async_boundary`, `split_api_surface`, `merged_trivial_scaffold`, `merged_same_file`, `merged_mechanical_rename`).
  - Cycle → non-zero exit; JSON payload includes `ring: [task_id, task_id, ...]`; no file writes attempted.
  - `build-schedule --supersede-parent NNN` inherits parent `depends_on`, generates single-letter children, rejects `Superceeded/Done/Cancelled` parents with `supersede-illegal-state`, rejects parents matching `^\d{3}[A-Z]$` with `supersede-depth-exceeded`.
  - `manifest --action init` outputs the canonical empty shape.
  - `manifest --action commit` uses `os.replace` on a `.tmp` sibling; malformed stdin fails before touching the target file.

**Description:**
`build-schedule` is the anti-drift single-pass: schedule shape is decided exactly once, downstream render/validate/commit consume it unchanged. `manifest` provides the stable-ID store with atomic writes so a crashed run cannot corrupt ID allocation.

**Implementation notes:**
`new_pending` IDs are allocated AFTER the topological sort pop order — this way new IDs naturally respect topological order and remain stable on re-run when only ordering shifts.

**Reversion guidance:**
Revert the two handlers and their helper functions in `decomp_ops.py`.

---

## Implementation Playbook

1. Add `score_complexity(spec) -> float` consuming `CEILING_*` constants.
2. Add `build_dag(tasks) -> adjacency`; add `detect_cycle(dag) -> ring | None`.
3. Add `topological_sort(dag, priority_rank, source_idx) -> [task_id,...]` (Kahn).
4. Add `assign_stable_ids(sorted_tasks, manifest) -> manifest'`.
5. Add `build_schedule` subparser; glue parser output through complexity/cycle/sort/ID phases.
6. Add `manifest` subparser with `--action {init,commit}`; implement atomic write-then-replace on `commit`.
