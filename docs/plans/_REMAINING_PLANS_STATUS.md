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
| `PHASE_D_STATE_MACHINE` | 0 / 8 | **Defer — flag for user** | Big state-machine refactor; risky; want user steer before launching |
| `prohibit_silent_revert` | 0 / 9 | **Defer — flag for user** | User halted this one previously (run `20260425T002624`, "user_halt") |
| `decompose_plans_tasks/build-plan-decomposer-plugin` | 0 / 11 | **Defer — flag for user** | 2026-04-17, predates a lot of work; likely superseded by `plan_ops.py` decomposer |

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

(Filled in as runs complete.)

### 1. PLAN_GEMINI_INTEGRATION_2026-04-25 (TASKS 005..008)
- Status: **pending start**
- Branch: `plan/gemini-integration-finish`
