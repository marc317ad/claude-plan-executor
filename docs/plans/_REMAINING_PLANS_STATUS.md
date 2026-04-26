# Remaining Plans — Execution Status

**Owner:** Claude (Opus 4.7), running on `main` while user is away.
**Started:** 2026-04-25 evening.
**Authority:** User authorized me to (a) commit, (b) make administrative decisions, (c) defer plans I can't safely run, (d) NOT push to remote — they will review before push.

---

## Triage summary

After surveying `docs/plans/`:

| Plan | Tasks done/total | Decision | Why |
|---|---|---|---|
| `PLAN_GEMINI_INTEGRATION_2026-04-25` | 4 / 8 | **Run next** — finish in-flight (TASK-005..008) | Continuation of just-merged work |
| `PLAN_TOPO_RESPECT_FIX_2026-04-25` | 0 / 6 | **Run** | Scheduler bug fix; benefits later plans |
| `CLAUDE_ONLY_FIX_2026-04-24` | 0 / 3 | **Run** | Narrow no-Codex contract surface |
| `POSTMORTEM_FIXES_2026-04-25` | 0 / 10 | **Run** | Cross-cutting friction fixes |
| `narrow_run_filter_ids` | 0 / 5 | **Run** | `--task-ids` scope fix |
| `SKILL_bash_dispatch_migration` | 0 / 7 | **Run** | Wrapper now exists (PLAN_NESTED_DISPATCH merged); SKILL.md still has 12 Agent dispatches and 0 wrapper calls — migration is next-in-line |
| `PHASE_D_STATE_MACHINE` | 0 / 8 | **Run** (was deferred — was over-cautious) | Same orchestrator + Codex review + pause protocol applies |
| `prohibit_silent_revert` | 0 / 9 | **Run** | Prior halt (`20260425T002624`) was due to issues since fixed; user cleared to proceed |
| `decompose_plans_tasks/build-plan-decomposer-plugin` | 0 / 11 | **Don't run yet — needs re-evaluation** | Builds a NEW `plan-decomposer` plugin (Canonical Contract constants, fingerprint, render templates, validate-output gates, atomic 7-phase commit-swap, recover crash windows, sync-status, plan-decomposer agent + skill). Plan dates from 2026-04-17; significant plan-executor evolution since then (per_task_dispatch_refactor_v2, directory-mode, build-tasks fat manifest, etc.). Plan needs review for compatibility with the current canonical contract before dispatch — likely needs author updates so the new plugin's interface matches what `/implement-plan` consumes today |

## Execution order (rationale)

1. `PLAN_GEMINI_INTEGRATION` first — already 50% done, lowest risk continuation.
2. `PLAN_TOPO_RESPECT_FIX` next — scheduler fix; everything downstream benefits.
3. `CLAUDE_ONLY_FIX` — narrow surface, easy to audit.
4. `narrow_run_filter_ids` — narrow-run scope fix.
5. `POSTMORTEM_FIXES` — cross-cutting; runs after the topo fix lands so it picks up corrected scheduler.
6. `SKILL_bash_dispatch_migration` — orchestrator-side migration; runs last because it touches the file every other plan execution depends on.

Each plan gets its own `plan/<short-name>` topic branch off `main`. On success: `git merge --no-ff` back to `main` (no push). On pause/fail: leave branch in place, document below, move on.

---

## Per-plan log

### 1. PLAN_GEMINI_INTEGRATION_2026-04-25 (TASKS 005..008)
- Status: **deferred** — first attempt aborted on `build-tasks` dependency-resolution gap
- Branch: `plan/gemini-integration-finish` (left intact, no commits)
- Run id: `20260425T221154` → `aborted` (`build_tasks_done_dep_block`)
- Root cause: `_build_tasks` filters out Done chunks (line 2683) before dep resolution, so pending TASK-005..008's body-level `**Dependencies:**` on already-Done TASK-001..004 surfaces as `unresolvable-dep` errors. No flag exists today to declare Done deps as satisfied.
- Resolution path: run `narrow_run_filter_ids` first — TASK-002 of that plan adds `--filter-ids` to `build-tasks` with closure-aware skip (in-closure Done chunks stay visible to the dep validator). After it lands, resume Gemini with `--filter-ids 005,006,007,008`.
- Switching to: re-order — run `narrow_run_filter_ids` next (it has no Done deps in its own roster).

### Re-ordered execution queue
1. `narrow_run_filter_ids` (5 tasks, no Done deps) — lands `--filter-ids` plumbing
2. `PLAN_GEMINI_INTEGRATION_2026-04-25` (resumed with `--filter-ids 005,006,007,008`)
3. `PLAN_TOPO_RESPECT_FIX_2026-04-25` (6 tasks)
4. `CLAUDE_ONLY_FIX_2026-04-24` (3 tasks)
5. `POSTMORTEM_FIXES_2026-04-25` (10 tasks)
6. `SKILL_bash_dispatch_migration` (7 tasks)

### 2. narrow_run_filter_ids (5 tasks)
- Status: **TASK-001 cherry-picked to main** (`0139ed1`); TASK-002 paused (WIP on topic branch); TASK-003..005 not yet attempted
- Branch: `plan/narrow-run-filter-ids` (HEAD: `a2a63cb` WIP, parent `15ef5efc` TASK-001 ship — cherry-picked)
- Run id: `20260425T221732`
- TASK-001 shipped — `15ef5efc` (`_compute_index_closure` helper + `index-closure` CLI; D.5 dismissed Codex frontier-set finding as misread).
- TASK-002 paused — first-pass needs-rework on 3 important+high findings (msg field omits offending body line; non-dict + un-normalizable task_id chunks silently skipped in scoped mode). D.5 confirmed all 3 load-bearing. D.2a.5 remediation succeeded. Binding re-review surfaced ONE NEW finding: trust-roster fallback applies even when roster `depends_on` is empty list — spec requires fallback only when roster deps resolve to in-closure task. Per D.2a.5 protocol no further remediation. WIP commit on topic branch preserves work for user disposition (hand-fix the empty-roster-deps gate, drop and revert, or override commit).
- Gemini still blocked: TASK-001's helper alone is insufficient — needs TASK-002's `--filter-ids` plumbing on `build-tasks` to be wired to actually skip Done-but-in-closure chunks (currently the closure helper exists but `_build_tasks` still filters Done before dep validation).

### 3. PLAN_TOPO_RESPECT_FIX_2026-04-25 (6 tasks)
- Status: **MERGED to main** (`bf8cc40`); branch `plan/topo-respect-fix` retained for history
- Branch: `plan/topo-respect-fix` (HEAD `0a02111`); 8 commits + bookkeeping; tip is ahead of `main` by 8 commits
- Run id: `20260425T231744` → `success` (6 done, 0 failed, 3 disagreements, 2 remediations, 1 orchestrator hand-fix)
- Commits (in order):
  - `b90eb802` feat(TASK-001) `_dependency_aware_batches` alias + 10-test class — D.5 needs-rework on name (spec); D.2a.5 alias remediation; binding re-review clean. `[disagreement] [remediation]`
  - `c605536a` feat(TASK-005) drop dead `parallel_batches` from 5 live rosters + 1 fixture + tests — clean (archive untouched)
  - `979b6dfe` feat(TASK-002) `_compute_schedule_batches` routes through helper alias + envelope normalization + dep validation — D.5 needs-rework on envelope shape; D.2a.5 normalization remediation; binding re-review surfaced new finding (falsy-coercion); orchestrator-authorized hand-fix per user broad-authority + run_log `20260423T094734` precedent. `[remediation]`
  - `389bd5fc` feat(TASK-003) `_build_tasks` helper alias + global-lock + idempotence test — clean
  - `9864624` feat(TASK-004) drop redundant `compute-schedule --stdin` from SKILL.md Phase 1 step 3 + 5-test fence in `tests/scripts/test_skill_md_invariants.py`
  - `cc7a32c0` feat(TASK-006) `_validate_schedule_dag` emits `dependency-batch-violation` (defense in depth) + 7 tests
  - `caa5685` chore(implement-plan) bookkeeping (run 20260425T231744)
  - `0a02111` chore(implement-plan) track schedule sidecar
- Pre-existing test failures in `test_plan_ops.py` are baseline (8 in `TestAudit*`/`TestGlobalLockPaths`/`TestTask007PlanReview`; require `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`); 2 collateral failures in `TestBatchNextBatchFidelity` flagged by TASK-006 implementer (the V3 hand-crafted fixtures violate the new validator and need rewriting — follow-up).

---

### 5. POSTMORTEM_FIXES_2026-04-25 (10 tasks)
- Status: **HALTED — author pass required** (cannot dispatch as-authored)
- Run id: `20260426T015555` → `failed` reason `analyst_invalid` (build-tasks `unresolvable-dep`)
- Root cause: body `Dependencies:` line in TASK-008 reads `["001"]` (with literal quotes — parser surfaces dep_id as the string `"001"` rather than `001`). Beyond the parse error, **body deps disagree with roster deps across TASK-004..009**:
  - TASK-004 body `[]` vs roster `["003"]`
  - TASK-005 body `[]` vs roster `["004"]`
  - TASK-006 body `[]` vs roster `["002"]`
  - TASK-007 body `[]` vs roster `["002","005","006"]`
  - TASK-008 body `["001"]` vs roster `["001","005","007"]`
  - TASK-009 body `[]` vs roster `["008"]`
- Per SKILL.md §Rules ("Plan-file body edits ... Dependencies ... MUST NOT be altered; the orchestrator is not a plan author"), the orchestrator may not auto-fix this. **User: please align each child's body `**Dependencies:**` line with the corresponding `00_INDEX.json` `chunks[].depends_on`** and re-invoke `/implement-plan`. The trust-roster fallback only fires when body deps parse to the empty list AND roster deps are non-empty — TASK-008's `["001"]` is non-empty so the fallback does not engage.
- Branch `plan/postmortem-fixes` was deleted (no commits landed)

---

### 4. CLAUDE_ONLY_FIX_2026-04-24 (3 tasks)
- Status: **MERGED to main**; branch `plan/claude-only-fix` retained for history
- Branch: `plan/claude-only-fix` merged via `--no-ff`
- Run id: `20260426T010342` → `success` (3 done, 0 failed, 0 disagreements, 0 remediations)
- Commits (in order):
  - `281c6fb` feat(TASK-001) `claude_only` routing flag and mutex prose plumbing — codex implement timed out 300s; claude fallback succeeded; codex review clean
  - `0e6c005` feat(TASK-002) Phase 1.5 Claude plan-review path + new `plan-reviewer` Sonnet agent + `--from-claude` parser flag + 13 new parser tests — claude implement; codex review clean
  - `4817f6f` feat(TASK-003) Phase D.1 cross-review Claude route-switch + D.2 ladder collapse + run-summary banner — claude implement; codex review clean
  - `242416b` chore(implement-plan) bookkeeping (run 20260426T010342)
- All 3 Codex reviews returned `clean` — no findings, no D.5 escalations, no remediation cycles
- Bundle certify: 5/6 gates pass; `commit-safe` certify hit directory-mode limitation (only validates one child file per call); per-commit `commit-safe` was verified inline for all 3 commits (all passed)
- 10 pre-existing test failures persist (`TestAudit*`, `TestGlobalLockPaths`, `TestBatchNextBatchFidelity`, `TestTask007PlanReviewSchemaTargetTaskIdOptional`); follow-up: rewrite to point at archive path or update fixtures

---

## Remaining queue (run via `/implement-plan` Skill invocation per plan)

5. `POSTMORTEM_FIXES_2026-04-25` (10 tasks)
6. `PHASE_D_STATE_MACHINE` (8 tasks)
7. `SKILL_bash_dispatch_migration` (7 tasks)
8. `prohibit_silent_revert` (9 tasks) — prior halt was for issues since fixed
9. After narrow_run_filter_ids TASK-002 unblocks (user hand-fix or instruct to resume): finish narrow_run TASK-003..005, then resume PLAN_GEMINI_INTEGRATION TASK-005..008 with `--filter-ids`

**Don't run yet (needs user re-evaluation before dispatch):**
- `decompose_plans_tasks/build-plan-decomposer-plugin` (11 tasks; 2026-04-17). Builds NEW separate plan-decomposer plugin. Significant plan-executor evolution since plan was authored — plan needs compatibility review against the current canonical contract before dispatch. User to update the plan, then run.
