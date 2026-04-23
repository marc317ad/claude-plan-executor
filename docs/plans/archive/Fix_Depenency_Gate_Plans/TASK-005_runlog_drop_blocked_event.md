# TASK-005 — Drop `blocked` event and `blocked_count` field from run-log schema

**Base branch:** `main`
**Source plan:** `~/.claude/plans/polymorphic-foraging-scone.md` (section E)

---

## Goal

Remove the `blocked` event entry **and drop `blocked_count` from the `run_end` required-fields list** in `plugins/plan-executor/skills/implement-plan/run-log-schema.md`. With cascade-block deleted in TASK-001/TASK-004, neither the event nor its aggregate count has a producer.

## Verification

1. `grep -n '^- `blocked`\|| *blocked *|' plugins/plan-executor/skills/implement-plan/run-log-schema.md` — zero hits.
2. `grep -rn '"event": "blocked"\|"blocked"' plugins/plan-executor/` — zero hits in code, SKILL.md, and run-log schema (doc comments are the only permissible surface, and even those are gone after TASK-004).
3. `grep -n 'blocked_count' plugins/plan-executor/skills/implement-plan/run-log-schema.md` — zero hits.

---

## Tasks

### TASK-005: Delete the `blocked` event row and `blocked_count` field from run-log-schema.md

- **Status:** done
  > Landed 2026-04-17 in commit `68954dd`. `plugins/plan-executor/skills/implement-plan/run-log-schema.md` no longer references `blocked_count` or a `blocked` event row — verified by grep. Note: the `blocked` *plan-markdown status* (set by `cmd_block_dependents`) is unrelated and remains in effect as orchestrator bookkeeping per the two-layer model in `docs/analysis/TASK_DEPENDENCY_DAG_Architecture_Inconsistency.md`.
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md`
- **Dependencies:** none
- **Test command:** `none`
- **Acceptance criteria:**
  - Line 10: strike `blocked_count,` from the `run_end` field list → becomes `ts, run_id, ending_sha, done_count, failed_count, disagreement_count, minor_findings_total`.
  - Line 20 (the `blocked` event entry) is deleted from the run-log schema table or list.
  - No other event rows modified; no field-definition section entries to prune (none reference `blocked_count`).
  - `cross_plan_resolved` is not catalogued in the file today and no action is needed on that front — if a future caller adds it, it will be handled separately.

**Description:**
Strip the `blocked` event from the run-log schema now that its only emitter (`cmd_block_dependents`) is deleted in TASK-001. TASK-004 rewrites `SKILL.md:350`/`:351` so the orchestrator's `run_end` emission no longer includes `blocked_count` (summary counts reduced to `{done, failed}`); this task keeps the schema doc in lock-step. Historical rows already appended to `_run_log.jsonl` retain the field; JSONL is append-only and is not back-rewritten.

**Reversion guidance:**
If cascade-block is reintroduced, restore *both* the `blocked_count` field in the `run_end` row *and* the `blocked` event row; re-introduce them as a pair because one without the other leaves the schema internally inconsistent. Otherwise the entries have no emitter and their absence is the desired state.
