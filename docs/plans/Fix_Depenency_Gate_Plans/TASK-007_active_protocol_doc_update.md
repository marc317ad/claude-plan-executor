# TASK-007 - Update active protocol doc for de-gated dependency model

**Base branch:** `main`
**Source plan:** Follow-up from dependency-gate audit; updates `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`

---

## Goal

Update the active protocol/design document `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` so it matches the revised dependency model: `00_INDEX.json` pre-flight is the only dependency-completion gate; downstream helpers do not use intra-plan dependencies for readiness, topological sorting, cycle/orphan validation, or cascade blocking. Batch scheduling is file-lock based and priority ordered. Terminal task failures are isolated, and peer tasks continue independently.

This task deliberately edits only the active protocol doc. Historical completed plans under `docs/plans/DUAL_AGENT_Plans/` are left untouched.

**Scope clarifications:**
- Sections 7, 8, and 10 are already dependency-clean and are intentionally not touched.
- Appendix C is not modified by this task.
- The implementer-reported agent status `blocked` (§7.2, §8.3, §15.2, Appendix C.3) is a separate concept from the scheduler's removed `blocked` state. It is retained in v1 and not modified by this task.

## Verification

1. `grep -nE '\bDAG\b|topological|topo order|dependency ordering is enforced|cascade-block|blocked dependents|Dependents blocked|dependency cycle|dependencies respected|<K> blocked|blocked\s*:\s*dict|blocked\s*\|\s*skipped' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` returns zero hits.
2. `grep -nE '00_INDEX.json|check-plan-deps|pre-flight dependency' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` shows the surviving dependency gate is documented as pre-flight only.
3. `grep -nE 'file-disjoint|file-lock|priority' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` shows the active scheduling model is documented in the architecture, analyst process, and verification sections.
4. Manual read-through of sections 1, 2, 4, 5, 6, 9, 13, 14, and Appendix A confirms there is no contradiction between the active design doc and `plugins/plan-executor/skills/implement-plan/SKILL.md`.

---

## Tasks

### TASK-007: Update active protocol doc for file-lock scheduling and pre-flight-only dependency gating

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
- **Dependencies:** none
- **Test command:** `grep -nE '\bDAG\b|topological|topo order|dependency ordering is enforced|cascade-block|blocked dependents|Dependents blocked|dependency cycle|dependencies respected|<K> blocked|blocked\s*:\s*dict|blocked\s*\|\s*skipped' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
- **Acceptance criteria:**
  - Section 1, "What makes this different from single-agent execution", changes the structured-plan-contract bullet from "parsed, validated plan with dependencies" to "parsed, validated plan and pre-flight dependency gate".
  - Section 2, "Design Principles", changes failure handling from "blocked dependents" to isolated task failures; it explicitly says peer tasks continue independently after terminal task failure.
  - Section 4 architecture diagram says Phase 0 resolves cross-plan dependencies via `00_INDEX.json`, and Phase 1 no longer says "Build dependency DAG, detect cycles". Phase 1 instead says the analyst classifies tasks and produces file-disjoint execution batches and risks.
  - Section 4 "Batch Scheduling" no longer says dependency ordering is enforced between batches. Replace the subsection body with text that says batches are file-disjoint; ordering comes from priority and file-lock conflicts only; intra-plan `Dependencies:` fields are not used for readiness, topological sorting, or cascade blocking; cross-plan dependency completion is checked once during pre-flight against `00_INDEX.json`.
  - The Batch Scheduling example no longer uses "depends on TASK-001"; use file-lock split / priority ordering wording instead.
  - Section 5 plan-document schema (line 208) removes `blocked` from the `Status:` enum, leaving `pending | in-progress | done | failed | skipped`.
  - Section 5 plan-document schema (line 215) deletes the `- **Dependencies:** none | TASK-NNN, TASK-NNN` bullet from the task template.
  - Section 5 required-fields table (line 248) deletes the `Dependencies` row entirely.
  - Section 6 plan-analyst process no longer says dependencies must reference existing tasks, no longer builds a dependency DAG, and no longer runs cycle detection. Replace the old DAG step with file-scope normalization. Replace topo-level batch computation with priority order plus file-disjoint batching.
  - Section 6 report format removes `<K> blocked` from the task summary.
  - Section 6 execution schedule example removes "sequential dep" and "depends on TASK-001"; use "file-lock split" or equivalent.
  - Section 6 risk examples no longer mention integration tests covering dependent tasks. Use combined behavior / file-conflict risk wording.
  - Section 6 JSON schema example removes `tasks[*].dependencies`. Contract notes delete the required `tasks[*].dependencies` bullet or replace it with: "`tasks[*].dependencies`, if present from a legacy schedule, is tolerated but ignored by scheduler helpers."
  - Add a dependency-gate footnote near the canonical contract explaining: `Dependencies:` in plan markdown and `tasks[*].dependencies` in legacy schedule JSON are not scheduler inputs; the only dependency-completion gate is `plan_ops.py check-plan-deps` against `00_INDEX.json` during pre-flight; scheduler helpers tolerate dependency fields for compatibility but must not use them for readiness, topological sorting, cycle detection, or cascade blocking.
  - Section 9.4 runtime state removes `blocked`.
  - Section 9.4 result handling replaces "cascade-block any tasks that depend on it" with: record the failure, release its file locks, and continue with peer tasks independently. Terminal failures do not cascade-block other tasks.
  - Section 9.5 replaces "For each successful implementation in topo order" with "schedule order".
  - Section 9.6 replaces "For each reviewed-clean task, in topo order" with "schedule order".
  - Section 13 Phase 2 plan-analyst testing replaces "dependencies respected" and "Cycle detection" with "priority order and file locks correct" and "No dependency DAG, cycle, or orphan-dependency errors emitted by the analyst".
  - Section 13 Phase 5 test-plan mix replaces "1 task with dependency on another" with file-lock scheduling coverage: at least two disjoint tasks that can run in parallel, and at least two tasks touching the same file so batching splits them by file lock.
  - Section 14 verification row 5 changes "valid schedule" to "file-disjoint schedule".
  - Section 14 verification row 11 changes "Dependency cascade" to "Failure isolation"; pass criteria says failed task is recorded, peer tasks continue independently, and no cascade-block event is emitted.
  - Appendix A comparison row changes "DAG computation" to "Batch computation"; implement-plan side says file-disjoint batching plus `00_INDEX.json` pre-flight gating.

**Description:**
Bring the active design document up to date with the stripped dependency-gate architecture. The implementation plans TASK-001 through TASK-006 remove downstream dependency gates from code, agents, skill text, run-log schema, and tests. This task removes the remaining normative design-doc claims that batches are topological dependency levels, that cycles/orphans are scheduler errors, and that failed tasks cascade-block dependents.

**Implementation notes:**
Use targeted edits around the known stale sections:

- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:18` - structured-plan-contract bullet.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:34` - failure-handling principle.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:89-97` - architecture phase diagram.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:123-133` - Batch Scheduling subsection.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:208`, `:215`, `:248` - Section 5 schema: `Status:` enum, `Dependencies:` template bullet, `Dependencies` required-field row.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:309`, `:315`, `:323` - analyst process.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:335`, `:344-348`, `:357-358`, `:385`, `:404` - analyst report / JSON contract examples.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:849-857`, `:882`, `:890`, `:911` - orchestrator runtime state and ordering.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:1087-1089`, `:1112-1116`, `:1140`, `:1146` - implementation and verification plans.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:1258` - Appendix A.

Suggested replacement snippets:

```md
4. **Structured plan contract** -- all work derives from a parsed, validated plan and pre-flight dependency gate, not ad-hoc requests
```

```md
7. **Failure handling is first-class.** Mandatory reversion guidance, explicit fail stages, isolated task failures, no blind retry. Peer tasks continue independently after a terminal task failure.
```

```md
Phase 0: Preflight
    | Parse arguments, validate plan structure, check dirty tree
    | Resolve cross-plan dependencies via 00_INDEX.json
    | Record starting SHA, generate run_id
    |
Phase 1: Plan Analysis (Claude Code -> plan-analyst agent)
    | Read plan, validate fields, verify files exist
    | Classify each task -> claude | codex
    | Produce file-disjoint execution batches and risks
```

```md
Tasks are processed in batches. Within a batch, tasks have disjoint file scopes and can execute in parallel. Between batches, ordering is derived from priority and file-lock conflicts only; intra-plan `Dependencies:` fields are not used for downstream readiness, topological sorting, or cascade blocking.

Cross-plan dependency completion is checked once during pre-flight against `00_INDEX.json`. If any required cross-plan dependency is unresolved, execution halts before the analyst runs.
```

```md
**Dependency-gate footnote.** In v1, `Dependencies:` in plan markdown and `tasks[*].dependencies` in legacy schedule JSON are not scheduler inputs. The only dependency-completion gate is `plan_ops.py check-plan-deps` against `00_INDEX.json` during pre-flight. Scheduler helpers may tolerate dependency fields for compatibility, but must not use them for readiness, topological sorting, cycle detection, or cascade blocking.
```

```md
For each failed task: record the failure, release its file locks, and continue with peer tasks independently. Terminal failures do not cascade-block other tasks.
```

Do not edit historical completed plan files under `docs/plans/DUAL_AGENT_Plans/`.

**Reversion guidance:**
Revert `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` to HEAD. This task is documentation-only; reverting it does not alter runtime behavior, but it will reintroduce active-protocol drift against TASK-001 through TASK-006.
