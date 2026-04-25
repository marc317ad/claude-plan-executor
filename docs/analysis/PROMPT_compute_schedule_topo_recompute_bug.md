# Prompt: author plan to fix `compute-schedule` topo-recompute bug

Run this in a fresh session on `main` (not the `plan/nested-dispatch` worktree). Author a plan only — do NOT implement the fix in this session.

---

You are a senior engineer authoring a plan for the plan-executor. The bug below was discovered during run `20260425T120429` of `PLAN_NESTED_DISPATCH` and is fully diagnosed; you do not need to re-investigate the failure mode. Your job is to write the plan that fixes it.

## The bug

`plugins/plan-executor/scripts/plan_ops.py:_build_tasks` (line 2484) already produces topo-layered + file-disjoint batches correctly. See lines 2786–2825, where the function names the bug it works around:

> Dependency-aware batching: topo layers first, file-lock partitioning within each layer. Plain `_compute_schedule_batches` ignores `dependencies[]` and would place prereq + dependent in the same batch when their files are disjoint; that would let `batch-next` dispatch a dependent before its prereq finishes.

`plugins/plan-executor/skills/implement-plan/SKILL.md` Phase 1 step 3 ("Recompute file-disjoint batches") then pipes the build-tasks output through `compute-schedule --stdin`, which calls `_compute_schedule_batches` (line 436). That function ignores `tasks[].dependencies` entirely (sort by `priority`/`id`, then greedy file-disjoint pack). It overwrites the topo-correct batches with file-disjoint-only batches.

The justification given in SKILL.md for the recomputation — "so the classifier-populated `agent` assignments are reflected in batch boundaries" — is wrong: `agent` is not an input to either batcher. Only `files`, `dependencies`, and `priority` matter. The recompute is a no-op recompute that silently regresses topo correctness.

Codex plan-review on the persisted (broken) schedule then correctly flags the DAG layering, leading to spurious `needs-replan` cycles, halting the orchestrator on the binding second verdict. Run `20260425T120429` halted on this; the operator unblocked it manually by replacing the schedule's `batches[]` with `_build_tasks`'s output before re-running. The runtime `batch-next --done <set>` masks the bug at dispatch time (it enforces deps via `--done`), so no actual DAG violation occurs at execution — but the persisted shape is wrong, the plan-review verdict is wrong, and the no-op recompute should not exist.

## Sites that need to be touched (the plan should audit and decide)

1. **`SKILL.md` Phase 1 step 3** — the primary offender. Search for "Step 3 — Recompute file-disjoint batches" + the `compute-schedule --stdin` invocation that follows. The spurious recompute lives here.

2. **`SKILL.md` Phase 1 step 3 `--task-ids` re-run** — same overwrite. The skill says: "After filtering, re-run `compute-schedule --stdin` on the filtered in-memory schedule to recompute file-disjoint batches." Same bug, different invocation.

3. **`cmd_filter_schedule`** in `plan_ops.py` — verify whether it re-batches internally. If yes, same bug surface.

4. **Any direct CLI caller of `plan_ops.py compute-schedule`** outside the orchestrator. Grep `compute-schedule` across the repo + skill markdown + tests.

5. **The roster's `parallel_batches` field** (`plan_ops.py:_decompose_plan` lines ~2425, 2457) — currently write-only. Nothing reads it back. Decide whether to (a) make it the canonical batch source-of-truth and have `_build_tasks` honor it, (b) remove it from the roster (dead field), or (c) keep it for human-readability + treat `_build_tasks`'s recomputed batches as authoritative.

## Fix-shape choices (the plan should pick one and justify in §Goal / §Verification)

**A. Skill-only fix.** Delete the `compute-schedule --stdin` calls from SKILL.md Phase 1 step 3 (default branch + `--task-ids` branch). Trust `_build_tasks`'s output. Code change footprint: zero. Skill change footprint: small. Risk: any future skill drift could re-introduce the recompute. Mitigation: add a regression test that asserts the persisted schedule's batches match `_build_tasks`'s output when no filter is applied.

**B. Code-fix `_compute_schedule_batches`.** Port the topo-layer logic from `_build_tasks` into `_compute_schedule_batches` so calling it is safe regardless of caller. Skill stays unchanged. Risk: two batchers drift; future bugs land in only one of them.

**C. Hybrid / canonicalize.** Extract `_build_tasks`'s in-loop batcher into a shared helper (`_dependency_aware_batches(tasks)`); have both `_build_tasks` and `_compute_schedule_batches` call it. Then either delete the `compute-schedule --stdin` calls in the skill (recommended; the recompute is still a no-op) or leave them (now safe). Risk: largest blast radius — touches both the function and the skill.

The plan should pick one of A / B / C, justify in §Goal, and document the affected files in each task block.

## Tests to require

- `tests/scripts/test_plan_ops.py` — add a regression test that constructs a plan where every task has unique files but a serial dependency chain (002 → 001, 003 → 002, ...). Assert `_build_tasks` returns N batches in topo order, NOT one batch with all tasks. (This test would have caught the regression.)
- If choosing fix B or C: assert `_compute_schedule_batches(tasks)` produces the same batches as `_build_tasks`'s output for the same `tasks[]`.
- If choosing fix A: assert that the SKILL.md does NOT contain a `compute-schedule --stdin` invocation in Phase 1 step 3 (text match against the markdown). Cheap fence against re-introduction.

## Plan deliverable

Output a decomposed-plan directory at `docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25/` (use `python3 plugins/plan-executor/scripts/plan_ops.py decompose-plan --plan-file <whole-plan.md>` if you author as a single file first; otherwise hand-craft `00_INDEX.json` + `TASK-NNN_<slug>.md` children). Schema must satisfy `schema-valid` + `fixture-valid`:

```bash
python3 plugins/plan-executor/scripts/plan_ops.py gates --check schema-valid,fixture-valid --plan-file docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25 --json
```

**Every task block must carry a populated `**Description:**` prose body** — the original PLAN_NESTED_DISPATCH plan tripped a 9-task `missing-description` warning cascade because every task left the field empty. Don't repeat that pattern.

## Constraints

- Do not implement the fix in this session. Author the plan only.
- Use `Read` for source inspection. Do not edit `plan_ops.py` or `SKILL.md` here.
- Add a short "Context" preamble at the top of the plan summarizing the failure mode for downstream reviewers (they won't have read this prompt).
- Reference the affected line ranges (`plan_ops.py:436-547`, `plan_ops.py:2484-2834`, `plan_ops.py:2425/2457`) so the implementer doesn't have to re-locate them.

## Key facts (for the plan's Context section)

- `_build_tasks` is correct (topo + file-disjoint). `_compute_schedule_batches` is broken (file-disjoint only).
- The runtime `batch-next` enforces deps via `--done`, so no executed plan ever violates the DAG even when the persisted shape claims it would. The bug is observability + plan-review correctness, not execution safety.
- Codex plan-review reads the persisted shape literally and (correctly) refuses plans whose `batches[]` violate `dependencies[]`. The protocol's no-op recompute therefore turns valid plans into rejected ones.
- The roster's `parallel_batches` field is currently dead weight (write-only).
