# Dual-Agent Plan Executor — Consolidated Remediation Plan

**Date:** 2026-04-14
**Branch:** `phase-7b5-bug-fixes`
**Head:** `d0f9740`
**Source reports:**
- `docs/analysis/DUAL_AGENT_EXECUTOR_DESIGN_AUDIT_2026-04-14.md` (Codex)
- `docs/analysis/DUAL_AGENT_EXECUTOR_Design_vs_Implementation_Gap_Report.md` (Claude)
- `docs/analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md` (runtime postmortem)

**Status:** the two audits agree on every P0 finding and diverge only at P1/P2 where each caught items the other missed. The consolidation below merges them, adds canonical IDs, and sequences the fixes. Codex cross-check was attempted via `codex exec` but the session stalled on an inherited stdin pipe; findings were instead reconciled by file-level comparison.

---

## 1. Agreed issues (both reports flagged)

| ID | Priority | Description | Codex label | Claude label |
|---|---|---|---|---|
| **ISSUE-001** | P0 | Analyst emits `id`/`index`; `plan_ops.py parse-schedule` and `batch-next` require `task_id`/`batch_index`. No live analyst output can pass schedule validation. | P0-AnalystJSON | Defect A |
| **ISSUE-002** | P0 | `sample_phase4.md` lacks Priority/Description/Reversion guidance, uses `## Purpose` not `## Goal`, no `## Verification`, hardcodes `**Agent:** codex` (bypasses capability-routing). | P0-SampleFixture | Defect B |
| **ISSUE-003** | P0 | Codex wrapper `validate_scope` on the **implement** path takes no pre-invocation baseline → parallel siblings delete each other's untracked outputs and orchestrator state. | P0-ImplementScope | Defect C |
| **ISSUE-004** | P0 | Same class of defect on the **review** path — `baseline_tracked` is sampled post-invocation, not pre. Masked only because reviews run serially. | P0-ReviewScope | Defect C (extended) |
| **ISSUE-005** | P0 | Codex timeout branch runs repo-wide `git checkout -- .` + `git clean -fd`, destroying any untracked work in the repo. | P0-TimeoutDestructive | Gap H |
| **ISSUE-006** | P1 | `--task-ids` flag documented in SKILL.md; no helper subcommand implements schedule filtering + orphan check. | P1-TaskIds | Defect D |
| **ISSUE-007** | P1 | `venv/bin/python` hardcoded across `SKILL.md` (14×), `dispatch-templates.md` (3×), and `plan_ops.py` usage header — violates "portable tier" rule. | P1-Portability | Defect E |
| **ISSUE-008** | P2 | `preflight` classifies `tests/` under `.claude/` / `docs/` as `infra_ignored` regardless of task scope → unrelated test-file drift doesn't halt. | P2-PreflightScope | Gap M |

---

## 2. Codex-only findings (verified against code; all kept)

| ID | Priority | Description | Verification |
|---|---|---|---|
| **ISSUE-009** | P1 | Task status vocabulary drift: design says `pending`, `plan_ops.py:40` accepts `open`; fixture uses `open`. | `plan_ops.py:40` has `{"open", "in-progress", ...}`; design §5 line 165 prescribes `pending`. |
| **ISSUE-010** | P1 | `batch-next` computes all ready tasks across the schedule and picks `--parallel` from the global ready set; it does not constrain selection to the declared batch. | `plan_ops.py:301` iterates `data.get("tasks")`, not `data["batches"][k]["task_ids"]`; `batch_index` is only reported, not enforced. |
| **ISSUE-011** | P1 | `fail-task` only runs `git restore` on listed files → untracked `(create)` outputs from failed Claude tasks remain on disk. | `plan_ops.py cmd_fail_task:497` calls `_git(["restore","--",p])` only; no untracked-file cleanup. |
| **ISSUE-012** | P1 | `block-dependents` appends `blocked` events but does not mutate task **Status** field in the plan markdown → plan document is not the source of truth after a cascade. | `plan_ops.py cmd_block_dependents:533` only writes to `_run_log.jsonl`; no markdown edit call. |
| **ISSUE-013** | P2 | Execution-log table column shape drift: design `Task \| Agent \| Outcome \| Reviewer \| Commit` vs `finalize_execution_log` writes `Task \| Agent \| Reviewer \| Verdict \| Commit \| Notes`. | `plan_ops.py:601` row format differs from `DUAL_AGENT_PLAN_EXECUTOR.md:224`. |
| **ISSUE-014** | P2 | `parse-implementer-report` looks for `**Concerns:**`; implementer spec emits `**Concerns for reviewer:**`. Reviewer concerns are dropped silently. | `plan_ops.py:391` literal `"**Concerns:**"`; `plan-implementer.md:126` emits `**Concerns for reviewer:**`. |

---

## 3. Claude-only findings (verified against code; all kept)

| ID | Priority | Description | Verification |
|---|---|---|---|
| **ISSUE-015** | P1 | `validate_scope` computes `undeclared`/`phantom` files to detect dishonest `files_changed` — never fails the task on mismatch; observability only. | `plan_codex_dispatch.py:707-713` assigns both lists into `extra` at 734/751 with no control-flow fork on non-empty. |
| **ISSUE-016** | P1 | `parse-implementer-report` does not extract the `**Plan adaptations:**` section (mandatory in implementer contract). Reviewer has no way to see plan deviations. | `plan_ops.py:354-403` parses six fields; no Plan adaptations handler. |
| **ISSUE-017** | P1 | No regression test covers parallel sibling-destruction (two Codex implement dispatches against adjacent untracked files). Phase 4 shipped without this; Scenario 7 was the first surface. | `tests/scripts/test_plan_codex_dispatch_integration.py` has 1 single-dispatch test only; no parallel fixture. |
| **ISSUE-018** | P2 | `acquire-lock` tolerates orphan-shape JSON in `_run_lock.json` — merges a new plan entry into a lock file without a `{plan_abs: {...}}` key. No PID liveness / TTL check. | `plan_ops.py cmd_acquire_lock:646-662` only compares on `plan_abs in current`; orphan files pass through. |
| **ISSUE-019** | P2 | `parse-schedule` trusts the analyst's `outcome` without running its own DAG check → a cycle labeled `valid` by a buggy analyst slips through. | `plan_ops.py:227-280` validates shape/fields only; no topological-sort verify. |
| **ISSUE-020** | P2 | No `plan_ops.py write-schedule` / `restrict-schedule` subcommand. SKILL.md line 113 tells the orchestrator to persist the schedule, but the only mechanism is an orchestrator-authored JSON write — brittle once `--task-ids` / `--codex-only` filters mutate it. | `plan_ops.py build_parser():693-794` — thirteen subparsers, none for schedule write/filter. |
| **ISSUE-021** | P1 | `scope_violation` outcome is generated by ISSUE-003 when orchestrator-state deletion happens — correct Codex runs get converted to failures, consuming the one Claude fallback. Auto-resolves with ISSUE-003. | `plan_codex_dispatch.py:673-689` — any violation in the un-baselined delta emits `scope_violation`. |
| **ISSUE-022** | P3 | Design-doc self-contradiction: §7.2 shows `"blockers": []` (strings); §15.2 shows `blockers` as object array. Implementation follows §7.2. Doc-only cleanup. | `codex_implement_schema.json:30-33` = array-of-strings. |
| **ISSUE-023** | P3 | Verdict vocabulary asymmetry (Codex `clean/minor-findings/needs-rework`, Claude `ship/ship-with-fixes/needs-rework`) documented but never exercised end-to-end — Scenario 7 stopped before any review ran. | `code-reviewer.md:54, 72-75` + `run-log-schema.md:27` consistent on paper; untested at runtime. |

---

## 4. Design-spec gaps (markdown edits, not code)

| ID | Priority | Description |
|---|---|---|
| **ISSUE-024** | P3 | `DUAL_AGENT_PLAN_EXECUTOR.md` §9.1 drops `--skip-analysis` from v1 without reconciling the schedule-sidecar gap (ISSUE-020). |
| **ISSUE-025** | P3 | §8.3 Codex-implement retry rule is silent on whether reviewer findings are forwarded to the retry implement. SKILL.md fills this in ("NOT forwarded in v1"); promote that choice into the design doc. |
| **ISSUE-026** | P3 | §6.1 specifies the schedule JSON contract but does not name the file path or owning subcommand for persistence. Add a §9.3 bullet. |

---

## 5. Disagreements

No priority disagreements.

Two priority bumps worth capturing explicitly:

- **ISSUE-004 (review-path scope)** — Codex promoted this to its own P0 line item; Claude treated it as a sub-point of ISSUE-003. Codex is right: the review path is masked by serial scheduling, not actually safe. Promote.
- **ISSUE-019 (DAG defensive check)** — Claude marked P2; Codex did not flag. Keep at P2 (defense-in-depth; analyst currently does catch cycles).

Two scope-of-fix interpretations:

- **ISSUE-014 vs ISSUE-016** — both are `parse-implementer-report` bugs but different labels (`**Concerns:**` vs missing `**Plan adaptations:**`). Treat as **one fix, two assertions** in the same patch.
- **ISSUE-002 scope** — Codex's "fixture hardcodes Agent: and bypasses capability-routing" is logically a sub-issue of Claude's Defect B. Merged here under ISSUE-002; the acceptance criterion in §8 covers both.

---

## 6. Fix-order dependency graph

```
                ┌──────────────────────────────────────────────┐
                │         ISSUE-003 + ISSUE-004 + ISSUE-005    │   ← wrapper state
                │   (baseline discipline on implement,         │     isolation cluster
                │    review, and timeout — one patch)          │     — all three share
                └────────────┬─────────────────────────────────┘     the same fix
                             │ unblocks
                             ▼
    ┌────────────────────────────────────────────────────────────┐
    │   ISSUE-001 (schedule wire format)                         │   ← contract alignment
    │   ISSUE-009 (status vocabulary)                            │     cluster — pick one
    │   ISSUE-002 (sample fixture rewrite)                       │     canonical schema,
    │   ISSUE-013 (execution-log column shape)                   │     apply everywhere
    │   ISSUE-014 + ISSUE-016 (implementer report parser)        │
    └────────────┬───────────────────────────────────────────────┘
                 │ unblocks
                 ▼
    ┌────────────────────────────────────────────────────────────┐
    │   ISSUE-010 (batch-next honors declared batch)             │   ← orchestrator
    │   ISSUE-011 (fail-task cleans created files)               │     semantics cluster
    │   ISSUE-012 (block-dependents mutates plan statuses)       │
    │   ISSUE-006 (--task-ids) + ISSUE-020 (write-schedule)      │
    └────────────┬───────────────────────────────────────────────┘
                 │ unblocks
                 ▼
    ┌────────────────────────────────────────────────────────────┐
    │   ISSUE-017 (parallel-sibling regression test)             │   ← tests + safety
    │   ISSUE-015 (enforce dishonesty check)                     │     cluster — land
    │   ISSUE-018 (acquire-lock strictness)                      │     after earlier
    │   ISSUE-019 (parse-schedule DAG defensive check)           │     clusters
    │   ISSUE-008 (preflight scope-aware tests/)                 │
    └────────────┬───────────────────────────────────────────────┘
                 │ independent of above
                 ▼
    ┌────────────────────────────────────────────────────────────┐
    │   ISSUE-007 (portability / $PYTHON parameterization)       │   ← polish
    │   ISSUE-022, 023 (design-doc consistency)                  │
    │   ISSUE-024, 025, 026 (design-doc gaps)                    │
    └────────────────────────────────────────────────────────────┘

ISSUE-021 (scope_violation co-opt)  ──── auto-resolves with ISSUE-003
```

---

## 7. Minimum viable unblock set for Phase 5 rerun

The smallest set that restores end-to-end executability for the twelve-scenario matrix:

1. **Wrapper state isolation** — ISSUE-003 + ISSUE-004 + ISSUE-005 in one patch.
   - Pre-invocation baseline snapshot on both `implement` and `review` paths.
   - Always-ignore list for orchestrator state (`docs/plans/_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`).
   - Timeout cleanup bounded to task's `allowed_files` + new-untracked delta. Never `git clean -fd` at repo scope.

2. **Contract alignment** — ISSUE-001 + ISSUE-009 in one patch.
   - Update `plan_ops.py` to read `id` / `index` / `pending` (match design doc).
   - Update `tests/scripts/test_plan_ops.py` to use the design-doc field names.
   - Add an integration test that pipes a real plan-analyst dispatch through `parse-schedule`.

3. **Fixture rewrite** — ISSUE-002.
   - Rewrite `docs/plans/sample_phase4.md` with `## Goal` / `## Context` / `## Verification` + per-task Priority/Description/Reversion guidance.
   - Remove `**Agent:** codex` from all tasks (routing belongs to the analyst classifier).
   - Normalize dependency format to `TASK-NNN, TASK-NNN`.

4. **Parallel regression test** — ISSUE-017.
   - Scratch-repo fixture spawning two `plan_codex_dispatch.py implement` in parallel against adjacent files; assert neither deletes the other's output nor the orchestrator state files.

After these four land, re-run Scenarios 1–12. ISSUE-006, 010, 011, 012, 014/016 are recommended for the same rerun (cheap, unblock Scenarios 6, 8, 11, 12 specifically) but are not required for the twelve-scenario matrix to *start*.

---

## 8. Bundling recommendation

One PR per row. Rationale: isolate blast radius, keep reviewable diffs, retain revert granularity.

| PR | Contents | Rationale |
|---|---|---|
| **PR-A: wrapper state isolation** | ISSUE-003, 004, 005 + unit test for each baseline path | One cohesive concern (file-system safety); all three share the baseline-snapshot pattern; ISSUE-021 auto-fixes. |
| **PR-B: schedule/status contract alignment** | ISSUE-001, 009, 013 + analyst→parse-schedule integration test + migrate `tests/scripts/test_plan_ops.py` | All three are "pick one canonical schema and apply it." Changing `plan_ops.py` without updating the tests would break CI. |
| **PR-C: fixture rewrite** | ISSUE-002 | Single-file edit; lands last in the contract-alignment trio to avoid churn from earlier passes. |
| **PR-D: orchestrator semantics — statuses / cascades** | ISSUE-010, 011, 012 | All three are "make `plan_ops` honor the plan-as-source-of-truth invariant." Shared test surface (plan-file mutation). |
| **PR-E: implementer report parser** | ISSUE-014 + ISSUE-016 | Same function body; two assertions. Trivially bundled. |
| **PR-F: task-ids + schedule persistence** | ISSUE-006, 020 | ISSUE-020 is the prerequisite infra for ISSUE-006; split would be artificial. |
| **PR-G: safety + defense-in-depth** | ISSUE-015, 018, 019 | All three are "enforce something we currently only observe." Compatible surface. |
| **PR-H: portability parameterization** | ISSUE-007 | Cross-cutting find/replace with a small `$PYTHON` abstraction; best in isolation. |
| **PR-I: coverage + preflight scope** | ISSUE-008, 017 | Test-only; low risk; can land alongside or after anything. |
| **PR-J: design-doc cleanup** | ISSUE-022, 023, 024, 025, 026 | Doc-only; no code review pressure. |

**Recommended merge order:** PR-A → PR-B → PR-C → PR-I (at least ISSUE-017) → (gate: rerun Phase 5). Then PR-D → PR-E → PR-F → PR-G → PR-H → PR-J.

---

## 9. Acceptance criteria

Each issue has one concrete verification.

| ID | Acceptance criterion |
|---|---|
| **ISSUE-001** | `venv/bin/python scripts/plan_ops.py parse-schedule --stdin --json` accepts `{"tasks":[{"id":"001",...}],"batches":[{"index":1,...}]}` and returns exit 0 with `errors: []`. Add integration test `test_analyst_to_parse_schedule_roundtrip` that dispatches `plan-analyst` on `sample_phase4.md` and pipes the raw JSON through `parse-schedule`. |
| **ISSUE-002** | `plan-analyst` dispatched on rewritten `sample_phase4.md` returns `outcome: valid`; all four TASK blocks contain `**Priority:**`, `**Description:**`, `**Reversion guidance:**`; no `**Agent:**` field; has `## Goal`, `## Context`, `## Verification` sections. |
| **ISSUE-003** | Unit test: invoke `validate_scope(repo_root, ["a.txt"])` in a scratch repo where an untracked file `b.txt` existed **before** invocation; assert `b.txt` is preserved and does not appear in `violations_untracked`. |
| **ISSUE-004** | Same as 003 for the review path. |
| **ISSUE-005** | Unit test: force a simulated timeout; assert `docs/plans/_run_log.jsonl` is still present and any pre-existing untracked file outside `allowed_files` is untouched. |
| **ISSUE-006** | `plan_ops.py filter-schedule --task-ids 1,3 --schedule-file <path>` produces a schedule JSON containing only tasks 1,3 and their transitive prerequisites; rejects orphaned dependencies with exit 1. |
| **ISSUE-007** | Grep `venv/bin/python` over `.claude/skills/implement-plan/SKILL.md` + `dispatch-templates.md` + `plan_ops.py` usage header returns zero matches. One-line note in SKILL.md points at the project-instruction variable. |
| **ISSUE-008** | `cmd_preflight` with a dirty `tests/` file that **is** in the current plan's task scope returns `pass=false` with `source_blocking` classification. |
| **ISSUE-009** | `plan_ops.py` and fixture accept `pending` as the default status; `open` remains as a compatibility alias or is deleted. Existing 33 tests migrated to `pending`. |
| **ISSUE-010** | Unit test: schedule has three batches; `batch-next --batch 2` returns tasks only from batch 2 even if batch 3 has ready tasks. |
| **ISSUE-011** | Integration test: seed a Claude task that creates `foo.txt` (untracked) then fails; `fail-task` removes `foo.txt` from disk. |
| **ISSUE-012** | After `block-dependents`, grep `^- \*\*Status:\*\* blocked` in the plan file against blocked task sections returns a match for each blocked task ID. |
| **ISSUE-013** | `finalize_execution_log` writes the column shape specified in `DUAL_AGENT_PLAN_EXECUTOR.md:224`. Update either doc or code; must match. |
| **ISSUE-014** | Unit test: `parse-implementer-report` on a report containing `**Concerns for reviewer:**\n- x\n- y` extracts `concerns=["x","y"]`. |
| **ISSUE-015** | Unit test: dispatch implement where reported `files_changed=["a.txt"]` but diff shows `b.txt` changed. Wrapper returns `outcome=failure` not `success`. |
| **ISSUE-016** | Unit test: `parse-implementer-report` on a report containing `**Plan adaptations:**\n- deviation` extracts `plan_adaptations=["deviation"]`. |
| **ISSUE-017** | Scratch-repo integration test: two parallel `plan_codex_dispatch.py implement` calls; assert both output files exist, orchestrator state files (`_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`) preserved, neither outcome is `scope_violation`. |
| **ISSUE-018** | `cmd_acquire_lock` rejects pre-existing `_run_lock.json` of unexpected shape with a diagnostic exit (unless `--force`). Document lock shape in SKILL.md. |
| **ISSUE-019** | Unit test: feed `parse-schedule` valid-outcome JSON with a dependency cycle; assert exit 1 with `errors: ["dependency cycle: ..."]`. |
| **ISSUE-020** | `plan_ops.py write-schedule --schedule-file <path> --stdin` accepts JSON on stdin and writes it. All `SKILL.md` references to direct JSON writes replaced. |
| **ISSUE-021** | Implicit — verified by ISSUE-003's acceptance criterion plus a rerun of Scenario 7. |
| **ISSUE-022** | Design doc §7.2 ↔ §15.2 reconciled to one blockers shape. |
| **ISSUE-023** | Phase 5 rerun includes at least one scenario where `code-reviewer` agent reviews Codex-implemented work and emits one of `{ship, ship-with-fixes, needs-rework}`; event logged in `_run_log.jsonl`. |
| **ISSUE-024** | Design §9.1 note removed or updated to reference ISSUE-020 as prerequisite. |
| **ISSUE-025** | Design §8.3 explicitly states whether retry forwards reviewer findings and why. |
| **ISSUE-026** | Design §9.3 adds a bullet naming `docs/plans/<basename>.schedule.json` and the owning subcommand. |

---

## 10. Summary

The two independent audits converge on five P0 ship-blockers (ISSUE-001 through ISSUE-005) and diverge productively at P1/P2 — each report caught items the other missed, and all divergences verified against current code. The root cause is contract drift across three phases masked by a test suite that validated the implementation against itself.

**Minimum viable path to a Phase 5 rerun is four PRs: A (wrapper isolation) → B (contract alignment) → C (fixture rewrite) → I's ISSUE-017 (parallel regression test).** Everything else is cleanup and can land in follow-up passes without blocking the executor.
