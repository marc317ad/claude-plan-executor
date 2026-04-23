# Task Dependency DAG: Two-Layer Architecture (Resolution Note)

**Date:** 2026-04-23
**Status:** Resolves the open question "Is `Fix_Depenency_Gate_Plans` complete?"
**Verdict:** No — and that is correct. The plan was partially superseded mid-flight by TASK-019 and TASK-004D after the chunked-layout assumption it was built on proved too narrow.

## TL;DR

The repo intentionally runs a **two-layer dependency model**. `Fix_Depenency_Gate_Plans` removed dep-handling at both layers; subsequent work re-introduced it at the orchestrator layer only. The current state is the intended target.

| Layer | Component | Sees `dependencies`? | Source |
|---|---|---|---|
| **Worker** (in-task agents) | `plan_codex_dispatch.parse_task_block`, `plan-analyst.md`, Codex review prompt | No | `Fix_Depenency_Gate_Plans` TASK-002, TASK-003; commit `f13d016` |
| **Orchestrator** (scheduling + bookkeeping) | `cmd_batch_next._ready`, `_validate_schedule_dag`, `cmd_block_dependents`, `cmd_filter_schedule` | Yes | `TASK-019` (commit `df60801`), `TASK-004D` (commit `0ee85c6`) |

Worker de-gating fixed the original problem (Codex review threw errors on dep checks). Orchestrator gating is required to support **monolithic plans with multiple inter-dependent tasks dispatched in parallel batches** — file-lock disjointness alone cannot express logical ordering between tasks that touch disjoint files.

## Timeline

1. **2026-04-17** — commit `68954dd` lands `Fix_Depenency_Gate_Plans`. Strips `_topo_sort`, `cmd_block_dependents`, `_validate_schedule_dag` (renamed to `_validate_schedule_refs` with cycle/orphan checks removed), `cmd_batch_next._ready`, and `cmd_filter_schedule` transitive closure. Drops `dependencies` from `parse_task_block`. The plan's stated assumption (TASK-001:14): *"each plan file holds one task under the chunked-layout convention, so intra-plan ordering is moot."*
2. **2026-04-18** — commit `0ee85c6` (TASK-004D) re-introduces `cmd_block_dependents` for plan-markdown mutation and run-log bookkeeping.
3. **2026-04-19** — commit `df60801` (TASK-019) re-introduces `_validate_schedule_dag` as a single shared helper called by `cmd_parse_schedule`, `cmd_batch_next`, and `cmd_filter_schedule`. Closes the V3/V4 acceptance-criteria gap from TASK-002 surfaced by run `20260418T015713`.

The `Fix_Depenency_Gate_Plans` tasks that touched the orchestrator layer (TASK-001, TASK-006, and the `block-dependents` removals from TASK-004's SKILL.md edits) are therefore **superseded**, not stalled.

## Why both layers are needed

**Worker layer (no deps):** The worker agents — Codex during review, plan-analyst, plan-implementer — operate on a single task envelope. They have no view of sibling tasks, so any dep reasoning they attempted was redundant with the orchestrator's gate and produced spurious "missing dependency" errors when the orchestrator had already satisfied the dep upstream. Removing dep awareness from the worker envelope eliminated that whole class of false negatives.

**Orchestrator layer (deps + DAG):** The orchestrator (`SKILL.md` flow + `plan_ops.py`) consumes the analyst's full schedule. For a multi-task plan it must:

- Reject malformed schedules at handoff (`_validate_schedule_dag` catches cycles and orphan deps in `parse-schedule` before any task runs).
- Refuse to dispatch a task whose declared dependency is not yet `done` (`cmd_batch_next._ready`), so logical ordering survives even when file scopes are disjoint.
- Mark transitive dependents as `blocked` in the plan markdown when an upstream task fails (`cmd_block_dependents`), giving end-of-run reports a coherent terminal state instead of leaving downstream tasks as `pending` forever.

Removing any of these would silently regress the orchestrator's ability to handle anything other than the chunked one-task-per-plan layout.

## What changed in the docs

- `Fix_Depenency_Gate_Plans/TASK-001_plan_ops_strip_dep_gates.md` — status flipped to `superseded` with a pointer to TASK-019/TASK-004D.
- `Fix_Depenency_Gate_Plans/TASK-006_tests_strip_dep_gating.md` — status flipped to `superseded` with the same pointer.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` — Batch Scheduling, Canonical Contract dependency-gate footnote, and analyst contract notes rewritten to describe the two-layer model.

## What the code already does (no change required)

- `_validate_schedule_dag` is wired into `parse-schedule`, `batch-next`, `filter-schedule`, and the `schedule-valid` phase gate.
- `cmd_batch_next._ready` enforces the V14 invariant (a task with any dep in `failed` is not ready) before file-lock claiming.
- `cmd_block_dependents` cascades `blocked` status onto transitive dependents and emits `blocked` run-log events.
- The worker layer parses zero `dependencies` data: `parse_task_block` does not include the field, and `plan-analyst.md` contains no dep references.

## Open follow-up (optional, not done in this pass)

If the `blocked` plan-status bookkeeping turns out to be unused in any end-of-run consumer, `cmd_block_dependents` can be retired without behavioral consequence — `cmd_batch_next._ready` already prevents dispatch on its own. Decide based on whether the `blocked` count appears in any report or downstream tooling. Keep otherwise.
