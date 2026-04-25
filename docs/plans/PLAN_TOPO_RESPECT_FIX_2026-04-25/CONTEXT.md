# Plan Context — Restore topo-respect to `compute-schedule`

## What broke

Run `20260425T120429` of `PLAN_NESTED_DISPATCH` halted on a binding second `needs-replan` from Codex `plan-review`. Root cause: the persisted schedule's `batches[]` violated `tasks[].dependencies` — a serial chain `001 → 002 → 003` with disjoint files was collapsed into a single batch. Codex correctly flagged this as a DAG violation; the orchestrator's auto-revise loop then failed to re-converge and halted on the binding second verdict.

The execution layer was never at risk: the runtime `batch-next --done <set>` enforces dependencies via the `--done` set, so no DAG violation ever actually executed. The bug is in the **persisted schedule shape** (and therefore in plan-review correctness), not in execution safety.

## Why it broke

`_build_tasks` at `plan_ops.py:2484-2834` produces topo-layered + file-disjoint batches correctly. After it runs, `SKILL.md` Phase 1 step 3 pipes the result through `compute-schedule --stdin` which calls `_compute_schedule_batches` at `plan_ops.py:436-547`. **That function ignores `tasks[].dependencies` entirely** — sort by priority/id, greedy file-disjoint pack. The recompute silently overwrites topo-correct batches with file-disjoint-only batches.

The justification given in `SKILL.md` for the recomputation — "so the classifier-populated `agent` assignments are reflected in batch boundaries" — is wrong: `agent` is not an input to either batcher. Only `files`, `dependencies`, and `priority` matter. The recompute is a no-op recompute that silently regresses topo correctness.

History: archived `docs/plans/archive/Fix_Depenency_Gate_Plans/TASK-001_plan_ops_strip_dep_gates.md` deliberately stripped the topo logic from `_compute_schedule_batches` on the assumption "each plan file holds one task." That assumption is now violated by every multi-task chunked plan in `docs/plans/` (this plan included).

## Affected line ranges

| Location | Concern |
|---|---|
| `plugins/plan-executor/scripts/plan_ops.py:436-547` | `_compute_schedule_batches` — broken file-disjoint-only batcher |
| `plugins/plan-executor/scripts/plan_ops.py:2484-2834` | `_build_tasks` — correct in-loop dep-aware batcher (logic to lift into shared helper) |
| `plugins/plan-executor/scripts/plan_ops.py:2425, 2457` | `_decompose_plan` — emits dead `parallel_batches` field (no reader anywhere) |
| `plugins/plan-executor/scripts/plan_ops.py:648-727` | `_validate_schedule_dag` — does not currently check that batches respect deps |
| `plugins/plan-executor/skills/implement-plan/SKILL.md:317-343` | Phase 1 step 3 — the redundant `compute-schedule --stdin` recompute pipe (default + `--task-ids` filter branches) |
| `plugins/plan-executor/skills/implement-plan/dispatch-templates.md:89, 197` | Re-run prose mentioning the recompute step |
| `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md:30-33` | `compute-schedule` cheatsheet entry |
| `tests/scripts/test_plan_ops.py:1640-1748` | `TestComputeSchedule` — `test_disjoint_files_single_batch` PINS the bug; needs rewrite |
| `tests/scripts/test_plan_ops.py:17216-17247` | `test_build_tasks_batches_respect_dependencies` — already pins correct `_build_tasks` behavior; reused as the "golden" output for the idempotence test |

## Fix shape — Path C (canonicalize)

Three options were considered (per the source analysis):

- **A. Skill-only.** Delete the `compute-schedule --stdin` calls from `SKILL.md`. Trust `_build_tasks` output. Zero code change. **Risk:** future drift could re-introduce; broken function is a footgun for any direct CLI caller.
- **B. Code-fix `_compute_schedule_batches`.** Port topo-layer logic from `_build_tasks` into `_compute_schedule_batches`. **Risk:** two batchers can drift; same logic in two places.
- **C. Hybrid (DRY shared helper).** Extract the dep-aware batcher into `_dependency_aware_batches(tasks, ordered_task_ids)`; both `_build_tasks` and `_compute_schedule_batches` call it. Then delete the SKILL.md recompute pipe (the recompute is provably a no-op).

**This plan picks C.** Justification: it eliminates the root cause (duplicate logic), prevents future drift between the two batchers, makes any future caller of either function safe by default, and produces a no-op recompute that the SKILL.md cleanup can confidently remove. The slightly larger blast radius is offset by three independent lines of defense (the helper, the deleted recompute, the validator).

## Plan structure

| Task | What it lands | Depends on |
|---|---|---|
| TASK-001 | Pure-addition shared helper `_dependency_aware_batches` + 8+ unit tests | — |
| TASK-002 | Wire `_compute_schedule_batches` to the helper + dep-shape validation + rewrite the bug-pinning test | TASK-001 |
| TASK-003 | Wire `_build_tasks` to the helper + global-lock regression test + idempotence test (build-tasks output == compute-schedule output) | TASK-001 |
| TASK-004 | Delete `compute-schedule --stdin` from SKILL.md Phase 1 step 3 (default + filter branches) + update `dispatch-templates.md` + `plan_ops_cheatsheet.md` + add fence test | TASK-002, TASK-003 |
| TASK-005 | Remove the dead `parallel_batches` roster field; update emission, fixtures, tests, live rosters; add back-compat parse test | — |
| TASK-006 | Defense in depth: add `dependency-batch-violation` check to `_validate_schedule_dag` | TASK-002, TASK-003 |

`build-tasks` (post-fix) on this plan's roster yields five batches: `[001], [005], [002], [003], [004 + 006]`.

## Out of scope (follow-up notes)

- `cmd_filter_schedule` preserves stale `file_locks` after task_id trimming. Tracked as a separate ergonomic bug; does not affect topo correctness; address in a follow-up plan.
- `gates --check schema-valid` does not yet support directory-mode `--plan-file`. Tracked as `POSTMORTEM_FIXES_2026-04-25/TASK-005`; this plan's directory passes schema-valid per-child but the directory-mode aggregate check is the open issue's lane.
- The CODEX_FRICTION_2026-04-25 in-flight schedule (and any other pre-fix persisted schedule) may carry batches violating deps. After TASK-006 lands, those schedules will fail re-validation. Operators re-running the affected plans will regenerate via `build-tasks` + `write-schedule` (post-fix, topo-correct).

## Verification at the end of the plan

Once all six tasks land:

1. `venv/bin/pytest -q tests/scripts/test_plan_ops.py tests/scripts/test_skill_md_invariants.py` returns 0.
2. `python3 plugins/plan-executor/scripts/plan_ops.py build-tasks --plans-dir <any chunked plan with serial deps and disjoint files> --json` returns batches that respect topo order.
3. The same plan piped through `compute-schedule --stdin --json` returns the same batches byte-for-byte (idempotence pin).
4. A deliberately-broken synthetic schedule (where batches violate deps) fails `parse-schedule --strict --stdin` with `dependency-batch-violation`.
5. `SKILL.md` no longer contains `compute-schedule --stdin` in Phase 1 step 3 (fence-tested).
