# Rollback: SKILL_bash_dispatch_migration

**Created:** 2026-04-26
**Status:** authoritative
**Plan:** `PLAN_SKILL_bash_dispatch_migration.md`

This document is the exit-ramp for the migration from
`Agent(subagent_type="plan-executor:*", ...)` calls in
`plugins/plan-executor/skills/implement-plan/SKILL.md` to bounded-envelope
`plan_claude_dispatch.py run --input <payload.json>` Bash invocations.

It covers:

1. Per-task revert order (most-to-least dependent).
2. The single `git revert` range that undoes all migrated dispatches in one
   shot.
3. The env-var fallback that gates the migration if a partial rollout
   becomes necessary mid-flight.
4. An appendix enumerating the `Agent` tool call sites that **intentionally
   remain** post-migration so future readers do not mistake them for
   migration misses.

---

## 1. Per-task revert order

Revert in reverse-dependency order. TASK-001 / TASK-002 are infra-only
(probe + stub) and have no SKILL.md edits, so reverting them last is purely
cleanup.

| Order | Task     | Commit (HEAD as of 2026-04-26) | What it touches                                                                  |
|-------|----------|--------------------------------|----------------------------------------------------------------------------------|
| 1     | TASK-007 | (this task)                    | E2E smoke test + ROLLBACK.md + e2e fixture plan. Test-only — safe to drop first. |
| 2     | TASK-006 | `69102e4`                      | Error-handling consolidation, `claude-envelope-extract` subcommand, run-log events. |
| 3     | TASK-005 | `f68dbfe`                      | Phase D.2a.6 plan-remediator dispatch.                                           |
| 4     | TASK-004 | `c83168b`                      | Phase B plan-implementer dispatches (default + B-rework + D.2b).                 |
| 5     | TASK-003 | `0f2153a`                      | Phase 1 plan-analyst (per-child classifier) dispatch.                            |
| 6     | TASK-002 | (in earlier commit range)      | Stub + envelope fixtures + per-agent JSON schemas. Test-only.                    |
| 7     | TASK-001 | (in earlier commit range)      | A/B canary probe + `probe_results.md`. Test-only.                                |

The infra tasks (TASK-001, TASK-002) MAY be retained even if the SKILL.md
migration is rolled back — they impose no production runtime cost (no
SKILL.md import) and the stub/fixtures remain useful for a future re-attempt.

### Per-task revert verification

After each revert, re-run that task's verification command:

- TASK-007: `venv/bin/pytest -q tests/scripts/test_skill_dispatch_e2e.py` →
  expected to be deleted.
- TASK-006: `venv/bin/pytest -q tests/scripts/test_claude_dispatch_run_log.py`.
- TASK-005: `venv/bin/pytest -q tests/scripts/test_skill_dispatch_remediator.py`.
- TASK-004: `venv/bin/pytest -q tests/scripts/test_skill_dispatch_implementer.py`.
- TASK-003: `venv/bin/pytest -q tests/scripts/test_skill_dispatch_analyst.py`.

Plus the plan's V-GLOBAL checks:

- V-GLOBAL-1 / V-GLOBAL-2 should remain green throughout.
- V-GLOBAL-3 (`rg -n "subagent_type.*plan-executor:(plan-analyst|plan-implementer|plan-remediator)"`)
  flips from zero hits → non-zero hits as TASK-003/004/005 are reverted; that
  is the *expected* signal of a successful rollback.
- V-GLOBAL-4 must remain at the pre-migration count (D.1 + D.5 reviewer
  dispatches) at every step — those paths are never touched.

---

## 2. Single `git revert` range

To undo the entire migration in one changeset:

```
git revert --no-commit <TASK-003 commit>^..<TASK-007 commit>
git commit -m "revert: roll back SKILL_bash_dispatch_migration"
```

Concretely, with current HEAD SHAs:

```
git revert --no-commit 0f2153a^..<TASK-007 commit>
```

The range is contiguous because the plan was executed sequentially with no
interleaving commits from other workstreams between TASK-003 and TASK-007.
If interleaving commits exist at rollback time, prefer the per-task order in
§1 over the bulk revert.

After the bulk revert:

- Re-run `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
  (V-GLOBAL-2 — orchestrator helpers should be unchanged).
- Re-run `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch_*.py`
  (V-GLOBAL-1 — wrapper itself was never modified by this plan, so this
  remains green).
- Confirm V-GLOBAL-3 returns the pre-migration call-site count (5 hits:
  Phase 1, Phase B default, B-rework, D.2b role-swap, D.2a.6).
- Confirm V-GLOBAL-4 is unchanged.

---

## 3. Env-var fallback (deferred implementation)

Per TASK-007 acceptance #4, this plan documents — but does not wire — an
env-var-gated fallback so a partial rollout can flip back to the `Agent`
tool surface without a `git revert`:

- **Variable:** `PLAN_EXEC_USE_CLAUDE_WRAPPER`
- **Default:** unset → wrapper path (post-migration default).
- **Set to `0`:** orchestrator would re-route the analyst / implementer /
  remediator dispatches back to `Agent(subagent_type="plan-executor:*", ...)`.
- **Set to `1`:** explicit opt-in to wrapper path (same as default).

Implementation is intentionally deferred. SKILL.md after TASK-003..006 has
exactly one Bash invocation per migrated site, so the fallback would require
adding a parallel `Agent` block at each site behind an `if env_var == "0"`
guard. That is non-trivial added prose and doubles each call site's byte
count, partially undoing the TASK-006 consolidation. We pay that cost only
if a real rollout actually hits a snag — at which point the plan should be
amended to add the wiring as a new follow-on task, not retroactively
backfilled into TASK-007.

If the fallback is ever wired, the env var must be honoured at every
migrated site (analyst, implementer-default, B-rework, D.2b role-swap,
D.2a.6 remediator) — partial wiring would split the orchestrator's behaviour
across the same run and is worse than no fallback.

---

## 4. Appendix — `Agent` tool call sites that intentionally remain

The migration's scope (per the plan's "Scoped Context" §Out of scope)
explicitly excludes the `code-reviewer` subagent. Both reviewer call sites
in SKILL.md continue to use the `Agent` tool surface and are NOT migration
misses. Future readers must not "fix" these by routing them through
`plan_claude_dispatch.py`.

| Phase | Location in `SKILL.md` | Form                                                                               |
|-------|------------------------|------------------------------------------------------------------------------------|
| D.1   | line 610 (approx)      | `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD_Claude, ...))` |
| D.5   | line 628 (approx)      | `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD_Claude, ...))` |

Migration of these to the wrapper requires expanding the v3 wrapper's
dispatchable set from `{plan-analyst, plan-implementer, plan-remediator}` to
include `code-reviewer`. That is a follow-on plan, not a TASK-007 item.

V-GLOBAL-4 is the regression guard:

```
rg -n "subagent_type.*plan-executor:code-reviewer" plugins/plan-executor/skills/implement-plan/
```

The pre-migration call-site count (D.1 + D.5 = 2 hits) is the post-migration
count too. Drift in either direction is a bug.

---

## 5. What rollback does NOT undo

- The wrapper itself (`plan_claude_dispatch.py`) — owned by the v3 plan
  (`PLAN_NESTED_DISPATCH_2026-04-18_v3.md`), not this one. Reverting this
  plan does not delete the wrapper.
- Codex review path — never touched by this plan; uses
  `plan_codex_dispatch.py review` throughout.
- Run-log event types added in TASK-006 (`claude_dispatch_start/done/failed`)
  — once a real run has emitted them, downstream log readers may have
  schema-locked against their presence. Reverting TASK-006 removes the
  emit-side; readers should be tolerant of unknown event types regardless,
  but check before bulk-reverting in production.
