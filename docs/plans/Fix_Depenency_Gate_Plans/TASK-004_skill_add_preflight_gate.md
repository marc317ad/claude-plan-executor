# TASK-004 — Add pre-flight dep gate + strip downstream dep logic from `SKILL.md`

**Base branch:** `main`
**Source plan:** `~/.claude/plans/polymorphic-foraging-scone.md` (section D)

---

## Goal

Rewrite `plugins/plan-executor/skills/implement-plan/SKILL.md` so cross-plan dependency resolution is a mandatory Phase 0 pre-flight call — `plan_ops.py check-plan-deps` between `preflight` and `acquire-lock`. All intra-plan dep logic (Phase 1 cross-plan branch, cascade-block after failures, dependent re-queue, `blocked` state) is stripped; task failures are isolated.

## Verification

1. Manual read of Phase 0: the mandatory `check-plan-deps --plan-file ... --plans-dir ... --json` invocation sits between preflight and acquire-lock; halts on `pass: false` with the unresolved[] list; halts on non-empty `errors[]`.
2. `grep -nE 'block-dependents|blocked_task_ids|external-dep|check-plan-deps' plugins/plan-executor/skills/implement-plan/SKILL.md` — `check-plan-deps` appears only inside Phase 0 (one call site); `block-dependents` (both the CLI row at line 45 and the Phase C/D.4 call sites), `blocked_task_ids`, and `external-dep` are absent.
3. `grep -nE '\btopo\b' plugins/plan-executor/skills/implement-plan/SKILL.md` — zero hits. Every "topo order" / "schedule topology" phrase is replaced per the acceptance criteria below.
4. `grep -nE '\bblocked\b' plugins/plan-executor/skills/implement-plan/SKILL.md` — zero hits describing `blocked` as a scheduler state, state-block field, count, or completion condition. (The implementer-report `outcome=blocked` failure-mode reference inside the Phase B outcome matrix is allowed; it routes through `fail-task` per D4.)
5. Phase E no longer references "reverse dependents" or "push satisfied ones into `ready`".
6. End-of-run completion rule reads `complete iff failed == 0; else partial`. Summary-print `counts` is explicitly `{done, failed}` — no `blocked` count.

---

## Tasks

### TASK-004: Insert pre-flight dep gate + rewire SKILL.md for isolated failures

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
- **Dependencies:** none
- **Test command:** `none`
- **Acceptance criteria:**
  - **Phase 0 (lines 78–112):** inserts the mandatory cross-plan gate between `preflight` and `acquire-lock`:

    ```bash
    venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" check-plan-deps \
      --plan-file <absolute plan> --plans-dir <dirname of plan-file> --json
    ```

    Halts on `pass: false` with the `unresolved[]` list. Halts on non-empty `errors[]` as internal-error. No `--allow-gaps` override — cross-plan deps are hard blockers.

  - **Phase 1 (lines 122–151):** the entire "Cross-plan dependency resolution" branch is deleted. Line 124 (the `needs-enrichment AND external-dep gaps` bullet) is struck. Lines 129–151 (the subheading, bash example, and result-shape paragraph) are removed. Remaining analyst outcomes: `invalid` halts; `needs-enrichment` halts unless `--allow-gaps`; `valid` proceeds.
  - **CLI reference table — `compute-schedule` row (line 39):** rewritten to `Recompute file-disjoint batches from tasks[]; use after any filter rewrite before persisting the schedule.` The words "topo order" are removed; batches are the only scheduling primitive that survives.
  - **CLI reference table — `batch-next` row (line 41):** rewritten to `Pick next batch respecting file locks; flags scheduler_stuck` (removes `+ deps`).
  - **CLI reference table — `block-dependents` row (line 45):** deleted. The subcommand is removed by TASK-001; the SKILL.md CLI row must not outlive it.
  - **Phase C (lines 238–258):** the `fail-task` call (242–249) is preserved unchanged (`git restore <files>` + plan-status→failed + `failed` run-log event). The `block-dependents` call block (252–256) AND the follow-up paragraph ("Remove blocked dependents from `ready`…") are deleted. Replacement text: "Release this task's file locks. Remove the task from `ready`. Peer tasks in the same and later batches proceed independently. Do NOT proceed to Phase D for this task." Implementer-report `outcome=blocked` remains an implementer failure mode that routes through `fail-task` like any other non-success outcome; it is NOT a scheduler state and does NOT mark dependents.
  - **`--task-ids` flow (lines 68 and 156–163):** line 68 rewritten to `Restrict to exactly these task IDs; halt if any ID is unknown.` Lines 156–163 rewritten to: "`filter-schedule` emits exactly the requested IDs in source order. Unknown task ID halts with `unknown-task-id`. Missing dep references in the filtered subgraph are not an error — schedule dependencies are no longer interpreted."
  - **Post-filter recompute prose (line 165):** rewritten to `After any filter rewrite, re-compute file-disjoint batches by piping the in-memory JSON through venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" compute-schedule --stdin --json, then replace the schedule's batches array with the returned batches before persisting.` The phrase "schedule topology" is removed.
  - **Execute-mode state block (lines 173–183):** line 176 (`ready`) comment becomes `analyst batch order; within a batch, batch-next serializes by file-lock availability` — this disambiguates source-plan D6 ("priority order from analyst JSON") vs. D7 ("topo order is the only readiness signal"), both of which become wrong once topo is banished. Line 179 (`blocked`) is deleted. `done`, `failed`, `locked_files`, `committed`, and `review_notes` remain.
  - **Phase D section heading (line 260):** rewritten from `Phase D — Review + commit (serial per task, topo order)` to `Phase D — Review + commit (serial per task, analyst batch order)`.
  - **Phase E (lines 342–344):** the "check reverse dependents, push satisfied ones into `ready`" clause is deleted. The Phase C/D.4 "Cascade-block dependents via `block-dependents`" sentence at line 340 is also deleted. New wording: "Release this task's file locks. Loop to Phase A."
  - **End-of-run completion (line 348):** rewritten to `complete iff failed == 0; else partial`. `blocked` is removed from the completion calculus.
  - **Summary print (line 351):** the `counts` reference is rewritten to enumerate `{done, failed}` explicitly — no `blocked` count, no "blocked dependents" phrase. Remaining items (`failures with reasons`, `disagreement-tagged commits`, `per-task minor-findings digest (from review_notes)`, `git log --oneline <starting_sha>..HEAD` hint) are preserved verbatim.
  - **Rules block line 366:** rewritten to `Never retry a failed task inside the same run beyond the one D.2b role-swap and the one Codex→Claude fallback. Terminal failures stay isolated — peers continue independently.`

**Description:**
Promote `check-plan-deps` to a mandatory Phase 0 gate so cross-plan dependencies are resolved before the analyst ever runs. Strip every downstream dep gate (cascade-block, dependent re-queue, `blocked` state). Task failures are now isolated — peer tasks in the same or later batches proceed independently.

**Implementation notes:**
Edit the specific line ranges enumerated above. Do not rewrite unrelated phases (A/B/D core flow). Preserve the `fail-task` call as-is — only the `block-dependents` follow-up is removed. The `--allow-gaps` flag still applies to analyst `needs-enrichment`; it does NOT override the Phase 0 `check-plan-deps` gate.

**Reversion guidance:**
Revert to HEAD. Without this file's edits, the orchestrator double-calls dep checks (harmless but noisy) and re-introduces the cascade-block / `blocked` state references — which will mismatch the `plan_ops.py` surface after TASK-001 ships. The two plans are typically reverted together.
