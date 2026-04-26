# TASK-010 — Document `_run_log.jsonl` as source of truth for run state; TaskList is mirror-only

## Goal

Capture the lesson from the post-mortem's Issue 10 (the harness `TaskList` drifted from `_run_log.jsonl` for ~6 hours during run 20260425T041800; the user noticed and asked "why are we still in plan-review?"). This is operational, not a code defect. Document the rule explicitly in `SKILL.md` and the orchestrator-facing dispatch documentation so the next operator knows the run-log is authoritative and the TaskList is a visibility mirror.

## Context

**The friction.** The harness `TaskList` carried five static placeholder tasks (Phase 0, Phase 1, Phase 1.5, Phase 2, End-of-run) seeded by an earlier session. The orchestrator drove the run from `_run_log.jsonl` events and forgot to flip TaskList entries until the user asked at 07:32 (~6h after Phase 1-triage had returned `ship`).

**Why doc-only.** No code defect: the run-log was correct throughout; the TaskList was just stale. The post-mortem proposes either (1) a doc fix making the rule explicit, or (2) a `plan_ops.py task-list-mirror` helper as a nice-to-have. This task ships only (1) — the helper is deferred unless future runs show the doc fix is insufficient.

**Where to document.**

1. **SKILL.md** — add a short subsection ("Run-state source of truth") near Phase 0 stating `_run_log.jsonl` is authoritative; TaskList is a visibility mirror; on phase transitions the orchestrator MUST mirror the run-log state into the TaskList for user visibility, but mirror failures do not block run progress.
2. **`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`** (the design contract) — one-line cross-reference to the SKILL section.
3. **End-of-run summary** — the SKILL.md `## End of run` section (line 852, step 4: "Print summary: counts {done, failed}...") gains an explicit bullet directing the orchestrator to include a `TaskList mirror state: {synced | drifted}` field alongside the existing summary fields. Drift is detected by comparing run-log `commit_done` / `task_failed` counts against TaskList completed-task counts; mismatch ⇒ `drifted` with the count delta noted.

**Scope.** Documentation only — no code changes, no tests, no schema changes.

## Verification

- `SKILL.md` contains a "Run-state source of truth" (or similar) subsection that states the rule and names `_run_log.jsonl` as authoritative.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` cross-references the SKILL anchor.
- A grep for "source of truth" + "run_log" returns the new subsection plus the cross-reference.
- The end-of-run summary documentation (wherever it currently lives — likely SKILL.md Phase E or equivalent) explicitly mentions the TaskList mirror state alongside the certify result.
- No code changes; tests are not required. Run summary mention is documentation only.

## Tasks

### TASK-010: Document run-log as source of truth

- **Status:** done
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — new subsection + end-of-run summary documentation update)
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` (edit — one-line cross-reference)
- **Dependencies:** []
- **Test command:** `grep -nE 'source of truth|run_log\.jsonl' plugins/plan-executor/skills/implement-plan/SKILL.md docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
- **Acceptance criteria:**
  - `SKILL.md` gains a "Run-state source of truth" subsection (heading text MAY differ if a more idiomatic phrasing exists in the doc) stating: (a) `_run_log.jsonl` is authoritative for run state; (b) the harness `TaskList` is a visibility mirror; (c) the orchestrator MUST mirror run-log state into TaskList on phase transitions, but mirror failures do not block run progress.
  - `DUAL_AGENT_PLAN_EXECUTOR.md` adds a one-line cross-reference to the SKILL anchor.
  - The SKILL.md `## End of run` section, step 4 ("Print summary: counts {done, failed}, failures with reasons, disagreement-tagged commits, per-task minor-findings digest...") gains a `TaskList mirror state: {synced | drifted}` bullet. The location is exact: `plugins/plan-executor/skills/implement-plan/SKILL.md` step 4 of `## End of run` (currently at line 857; pin by the step content, not the line number, since other tasks may shift it).
  - No code, schema, or test changes.
  - The grep test command surfaces the new content (>=2 hits).
- **Reversion guidance:** revert the doc additions; no behavior change either way.

**Description:**
Document explicitly that `_run_log.jsonl` is the source of truth for run state and the harness TaskList is a visibility mirror. This captures the post-mortem lesson without committing to the heavier "task-list-mirror" helper option (deferred unless drift recurs).

**Implementation notes:**
- Keep the new subsection short — a tight rule lands better than a long explanation. ~5–8 lines is the right size.
- Resist scope creep into specifying mirror frequency, retry behavior, or harness internals. The rule is operational guidance, not a protocol clause.
