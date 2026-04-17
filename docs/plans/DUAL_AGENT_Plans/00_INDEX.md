# DUAL AGENT EXECUTOR HARDENING — Chunked Execution Index

**Created:** 2026-04-14
**Base branch:** `main`
**Parent plan:** [`docs/plans/DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md)
**Consolidated defect inventory:** [`docs/analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md)
**Phase 5 postmortem:** [`docs/analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md`](../../analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md)
**Audit — Codex:** [`docs/analysis/DUAL_AGENT_EXECUTOR_DESIGN_AUDIT_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_DESIGN_AUDIT_2026-04-14.md)
**Audit — Claude:** [`docs/analysis/DUAL_AGENT_EXECUTOR_Design_vs_Implementation_Gap_Report.md`](../../analysis/DUAL_AGENT_EXECUTOR_Design_vs_Implementation_Gap_Report.md)

---

## Repo reorganization note (2026-04-16)

This repo was decoupled from its original parent and the orchestrator source was moved under `plugins/plan-executor/` (commit `d211c6b`). All **pending** and **proposed** plan files in this directory have been rewritten to reference the new paths:

- `.claude/skills/implement-plan/*` → `plugins/plan-executor/skills/implement-plan/*`
- `.claude/agents/plan-*.md` → `plugins/plan-executor/agents/plan-*.md`
- `scripts/plan_ops.py`, `scripts/plan_codex_dispatch.py`, `scripts/codex_*_schema.json` → `plugins/plan-executor/scripts/...`

Tests live at repo root (`tests/scripts/test_plan_ops.py`) so they can prove plugin modifications from the outside. `pytest` is installed in the repo-level `venv/`.

The **done** plans (`TASK-001`, `TASK-002`, `TASK-003`) are intentionally left unedited — they are a historical record of work executed against the old layout. Cross-references to them may use either path form.

`Base branch:` is `main` across all remaining work; sub-branches may be cut later for individual improvements.

---

## Why this directory exists

The v3 hardening plan defines 11 workstream tasks. Each one mixes defect fixes (ISSUE-001…026 from the consolidated report) with new runtime-governance capabilities (phase gates, self-audit, global locks, bounded log handling). Run end-to-end, the v3 plan is ~460 lines of context, and the full context pack (v3 + consolidated report + two audits + postmortem + design doc) is ~5000 lines.

Each file in this directory carves out **one v3 task** and inlines the minimum context required to execute it without re-reading the full audit chain. Each chunk is self-sufficient; every chunk also declares the prior chunks it depends on so execution order is explicit.

## Shared background (read once, then skip)

**System under test.** The `/implement-plan` skill is a dual-agent plan executor that dispatches work to either Claude (via `plan-implementer` subagent) or Codex (via `plugins/plan-executor/scripts/plan_codex_dispatch.py`), with asymmetric cross-review. Key artifacts:

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — orchestrator (317 lines)
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — phase A/B/D prompts
- `plugins/plan-executor/skills/implement-plan/run-log-schema.md` — event catalogue
- `plugins/plan-executor/agents/plan-analyst.md` — produces schedule JSON from plan markdown
- `plugins/plan-executor/agents/plan-implementer.md` — Claude-side implementer
- `plugins/plan-executor/scripts/plan_ops.py` — 13 stdlib-only helper subcommands (819 lines)
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — Codex CLI wrapper (980 lines)
- `plugins/plan-executor/scripts/codex_implement_schema.json`, `plugins/plan-executor/scripts/codex_review_schema.json`
- `docs/plans/sample_phase4.md` — end-to-end fixture
- `tests/scripts/test_plan_ops.py` — 33/42 helper unit tests
- `tests/scripts/test_plan_codex_dispatch_integration.py` — single-dispatch integration test

**Git state.** Audit, postmortem, and this directory are all anchored at commit `d0f9740` on `main`. Line numbers cited in chunks are against that commit.

**Core failure mode.** Three independent wire contracts (analyst JSON shape, task status vocabulary, implementer report labels) drifted across Phases 1–4. The Phase 4 test suite validated the implementation against itself rather than against the shipped contracts, so the drift went undetected until Phase 5. Phase 5 Scenario 7 also surfaced a wrapper-isolation bug that makes parallel Codex dispatches delete each other's output and orchestrator state (`_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`).

**Canonical contract (per design doc).**

| Concept | Design/analyst spec | Current helper behavior |
|---|---|---|
| Task status vocabulary | `pending \| in-progress \| done \| failed \| blocked \| skipped` | `open \| in-progress \| done \| failed \| blocked \| skipped` (`plan_ops.py:40`) |
| Analyst task field | `id` | Helper expects `task_id` (`plan_ops.py:250`) |
| Analyst batch field | `index` | Helper expects `batch_index` (`plan_ops.py:263`, `301`, `322-328`) |
| Implementer concerns label | `**Concerns for reviewer:**` | Parser looks for `**Concerns:**` (`plan_ops.py:391`) |
| Execution-log columns | `Task \| Agent \| Outcome \| Reviewer \| Commit` (design §224) | Writer emits `Task \| Agent \| Reviewer \| Verdict \| Commit \| Notes` (`plan_ops.py:601`) |

TASK-001 resolves the contract split by picking one canonical version per row. Everything downstream assumes TASK-001 landed first.

---

## Chunk roster

| # | File | v3 Task | Priority | Issues absorbed | Depends on chunks |
|---|---|---|---|---|---|
| 1 | [`TASK-001_canonical_contracts.md`](TASK-001_canonical_contracts.md) | TASK-001 | critical | 001, 009, 013, 014, 016, 022, 024, 025, 026 | — |
| 2 | [`TASK-002_runtime_validation.md`](TASK-002_runtime_validation.md) | TASK-002 | critical | 001, 014, 016, 019, 020 | TASK-001 |
| 3 | [`TASK-003_state_isolation.md`](TASK-003_state_isolation.md) | TASK-003 | critical | 003, 004, 005, 015, 017, 021 | TASK-001 |
| 4 | [`TASK-004_scheduler_semantics.md`](TASK-004_scheduler_semantics.md) | TASK-004 | high | 006, 010, 011, 012, 018, 019, 020 | TASK-001, TASK-002 |
| 5 | [`TASK-005_phase_gates.md`](TASK-005_phase_gates.md) | TASK-005 | high | — (new capability) | TASK-001, TASK-002, TASK-004 |
| 6 | [`TASK-006_conformance_fixture.md`](TASK-006_conformance_fixture.md) | TASK-006 | high | 002 | TASK-001, TASK-005 |
| 7 | [`TASK-007_self_audit.md`](TASK-007_self_audit.md) | TASK-007 | medium | — (new capability) | TASK-001, TASK-002, TASK-005 |
| 8 | [`TASK-008_portability_preflight.md`](TASK-008_portability_preflight.md) | TASK-008 | medium | 007, 008 | TASK-001, TASK-005 |
| 9 | [`TASK-009_scale_aware_reads.md`](TASK-009_scale_aware_reads.md) | TASK-009 | medium | — (new capability) | TASK-001, TASK-002 |
| 10 | [`TASK-010_global_dep_locks.md`](TASK-010_global_dep_locks.md) | TASK-010 | medium | — (new capability) | TASK-001, TASK-004, TASK-005 |
| 11 | [`TASK-011_bounded_log_handling.md`](TASK-011_bounded_log_handling.md) | TASK-011 | medium | — (new capability) | TASK-001, TASK-002 |
| 14A | [`TASK-014_remediation_and_plan_review.md`](TASK-014_remediation_and_plan_review.md) | TASK-014A | high | — (new capability) | TASK-001, TASK-002 |
| 14B | [`TASK-014_remediation_and_plan_review.md`](TASK-014_remediation_and_plan_review.md) | TASK-014B | medium | — (new capability) | TASK-001 |
| 14C | [`TASK-014_remediation_and_plan_review.md`](TASK-014_remediation_and_plan_review.md) | TASK-014C | high | — (new capability) | TASK-001, TASK-002 |
| 15 | [`TASK-015_codex_reviewer_prompt_tuning.md`](TASK-015_codex_reviewer_prompt_tuning.md) | TASK-015 | medium | — (new capability) | TASK-014C |

## Proposed follow-on specs

These are design notes for gaps discovered after the v3 chunk set was written. They are not yet part of the committed execution order above.

- [`TASK-012_reviewer_parser_spec.md`](TASK-012_reviewer_parser_spec.md) — defines the missing `parse-reviewer-report` contract and explains why it is not already owned by TASK-002/003/005.
- [`TASK-013_plan_review_and_remediation.md`](TASK-013_plan_review_and_remediation.md) — proposes a pre-implementation independent plan-review gate plus one bounded reviewer-driven remediation loop instead of immediate revert on every actionable review failure.

## Execution order

Two passes:

**Pass 1 — rerun gate.** These must all complete before any Phase 5 rerun. Land in this order, with parallelism where dependencies allow:

```
TASK-001 ─┬─> TASK-002 ─┬─> TASK-004 ─> TASK-005 ─> TASK-006
          │             │                │
          └─> TASK-003 ─┘                └─> (gate: Phase 5 rerun)
```

After TASK-001 through TASK-006 are green the executor has: one canonical contract, seam-validated inputs, baseline-scoped wrapper isolation, honest scheduler semantics, explicit phase gates, and a fixture that exercises the real contract. The consolidated rerun-gate (v3 §"Consolidated Rerun Gate") is satisfied.

**Pass 2 — production hardening.** After the rerun gate is satisfied but before declaring the executor production-ready:

```
TASK-007 ─> self-audit / drift detection
TASK-008 ─> portability parameterization + scope-aware preflight
TASK-009 ─> scale-aware large-file reads
TASK-010 ─> global dep/env manifest locks
TASK-011 ─> bounded untrusted log handling
```

These can land in any order; none blocks the others. TASK-009/010/011 only matter for runs that involve large files, manifest edits, or heavy test-log feedback loops — they do not have to block the first rerun if Phase 5 does not touch those paths.

## How each chunk is structured

Every chunk file has this skeleton:

1. **Header** — pointers to parent v3 plan, consolidated report, design doc, and chunk dependencies.
2. **Goal** — one paragraph.
3. **Scoped Context** — the ISSUE numbers this chunk resolves, each with file:line pointers to the broken behavior and the expected state. Use this instead of re-reading the audits.
4. **Verification** — concrete pass criteria (grep commands, unit-test selectors, integration-test scenarios).
5. **Tasks** (plan-schema compatible `### TASK-NNN` block) — ready for the `/implement-plan` skill to consume. Files/Dependencies/Priority match v3.
6. **Implementation Playbook** — concrete step-by-step edit sketches (not code dumps; pointers plus semantic requirements).
7. **Out of Scope** — things that look related but belong to a different chunk. Keeps each chunk narrow.

The plan-schema `### TASK-NNN` block in each chunk is valid against `plan-analyst.md:49` required fields, so a chunk can be handed to `/implement-plan` directly if needed.

## Reference pointers (once, for all chunks)

### Files modified across multiple chunks

- `plugins/plan-executor/scripts/plan_ops.py` — TASK-001, 002, 004, 005, 007, 008, 010, 011
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — TASK-001, 003, 011
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — TASK-001, 002, 003, 004, 005, 007, 008, 009, 010, 011
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — TASK-001, 003, 008, 011
- `plugins/plan-executor/agents/plan-analyst.md` — TASK-001, 002, 009, 010
- `plugins/plan-executor/agents/plan-implementer.md` — TASK-001, 002, 009, 011
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` — TASK-001, 003, 004, 005, 007, 008, 009, 010, 011
- `tests/scripts/test_plan_ops.py` — TASK-002, 004, 005, 006, 007, 008, 009, 010, 011
- `tests/scripts/test_plan_codex_dispatch_integration.py` — TASK-002, 003, 006, 011

### ISSUE→chunk reverse map

| ISSUE | Priority | Chunk |
|---|---|---|
| 001 | P0 | TASK-001 (primary), TASK-002 (validation) |
| 002 | P0 | TASK-006 |
| 003 | P0 | TASK-003 |
| 004 | P0 | TASK-003 |
| 005 | P0 | TASK-003 |
| 006 | P1 | TASK-004 |
| 007 | P1 | TASK-008 |
| 008 | P2 | TASK-008 |
| 009 | P1 | TASK-001 |
| 010 | P1 | TASK-004 |
| 011 | P1 | TASK-004 |
| 012 | P1 | TASK-004 |
| 013 | P2 | TASK-001 |
| 014 | P2 | TASK-001 (primary), TASK-002 (validation) |
| 015 | P1 | TASK-003 |
| 016 | P1 | TASK-001 (primary), TASK-002 (validation) |
| 017 | P1 | TASK-003 |
| 018 | P2 | TASK-004 |
| 019 | P2 | TASK-002 (primary), TASK-004 (secondary) |
| 020 | P2 | TASK-002 (primary), TASK-004 (subcommand) |
| 021 | P1 | auto-resolves with TASK-003 |
| 022 | P3 | TASK-001 |
| 023 | P3 | verified at rerun, not a fix chunk |
| 024 | P3 | TASK-001 |
| 025 | P3 | TASK-001 |
| 026 | P3 | TASK-001 |

### Key design-doc sections

All `DUAL_AGENT_PLAN_EXECUTOR.md` references in the chunks assume commit `d0f9740`. Particularly load-bearing sections:

- §5 (plan schema) — required task fields: Priority, Description, Reversion guidance; required plan sections: Goal, Context, Verification
- §6.1 (schedule JSON) — lines 306-346
- §7.2 (Codex implement envelope) — line 508
- §7.5 (Codex recovery rules) — timeout, dishonesty, no-edit-success
- §8.3 (retry rules) — Codex-implements row
- §8.4 (third-opinion escalation) — Claude critical on Codex work
- §9.1 (run phases) — preflight, acquire-lock, analyst dispatch, batch loop
- §9.3 (schedule persistence) — TBD (ISSUE-026 prescribes adding this)
- §11 (portable vs repo-specific tier split) — §11.1, §11.2
- §14 (testing and conformance)
- Appendix C.3-C.7 (Codex dispatch details)
- Appendix D (Codex failure modes F1-F4)

---

## Workflow for executing a chunk

1. Read the chunk file. Ignore the rest of this directory.
2. If chunk dependencies are not all in `done` status, stop.
3. Run the verification commands to confirm starting state (in scope for the chunk).
4. Follow the Implementation Playbook.
5. Run the verification commands to confirm ending state.
6. Commit narrowly per chunk; one PR per chunk unless the chunk explicitly authorizes bundling (none currently do).

## When to update this index

- A new chunk is added (after v3 acquires a TASK-012+).
- A chunk is merged into another (update roster + ISSUE reverse map).
- Execution order changes (rerun-gate boundary moves).

Do not put TASK content in this index; keep it a pure navigation doc.
