# TASK-002 — Wire orchestrator Phase 1 Step 2 to persist `**Agent:**`; document policy

## Goal

Wire orchestrator Phase 1 Step 2 to persist `**Agent:**`; document policy

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- In `SKILL.md` §Step 2 (around line 286-321), add a new sub-step **after** the per-child classifier extraction and **before** the merged-tasks shape-shift in Step 3. Exact text (copy verbatim; this is the contract): > After every per-child classifier dispatch returns `status==ok` with `result.agent ∈ {claude, codex}`, dispatch `Tool: plan_ops__set_task_agent with input {"plan_file": "<absolute child path>", "task_id": "<canonical NNN>", "agent": "<result.agent>"}` to persist `**Agent:**` into the child plan file. This is fan-out: one call per classified child, in parallel with sibling sets. On `status!=ok` for the underlying classifier dispatch, DO NOT call `set_task_agent` for that child — let Phase 1 fail through the existing analyst-invalid path.
- In `SKILL.md` §Dry-run mode (the recently-amended section around line 402+), add a third numbered exemption immediately after the Phase 1.5 plan-revision cycle entry: > 3. **Phase 1 Step 2 `**Agent:**` persistence** — `plan_ops__set_task_agent` fires under `--dry-run` so the classifier output reaches disk. Without this, dry-run leaves the plan amnesic and the next real run re-classifies from scratch, defeating the rehearsal value.
- In `SKILL.md` §Command idioms table (around line 119), add `set-task-agent` to the list of commands that take `--plan-file` (it mutates plan markdown, same family as `commit-task` / `update-plan-header`).
- In `CLAUDE.md` line 50, extend the "Allowed plan-file mutations" enumeration. Current text: ``Allowed plan-file mutations: `**Status:**` flips, append-only execution-log tail, ...``. New text: ``Allowed plan-file mutations: `**Status:**` flips, `**Agent:**` flips (post-classifier persistence; idempotent, enum-valued), append-only execution-log tail, ...``.
- Add `test_phase1_step2_persists_agent_to_child_file` in `tests/scripts/test_implement_plan_mcp_e2e.py`:
- Add `test_phase1_step2_skips_classifier_on_second_invocation`:
- Add `test_phase1_step2_persists_under_dry_run`:

## Tasks

### TASK-002: Wire orchestrator Phase 1 Step 2 to persist `**Agent:**`; document policy

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `CLAUDE.md`
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2 and (agent or skip_classifier or dry_run)"`
- **Acceptance criteria:**
  - In `SKILL.md` §Step 2 (around line 286-321), add a new sub-step **after** the per-child classifier extraction and **before** the merged-tasks shape-shift in Step 3. Exact text (copy verbatim; this is the contract): > After every per-child classifier dispatch returns `status==ok` with `result.agent ∈ {claude, codex}`, dispatch `Tool: plan_ops__set_task_agent with input {"plan_file": "<absolute child path>", "task_id": "<canonical NNN>", "agent": "<result.agent>"}` to persist `**Agent:**` into the child plan file. This is fan-out: one call per classified child, in parallel with sibling sets. On `status!=ok` for the underlying classifier dispatch, DO NOT call `set_task_agent` for that child — let Phase 1 fail through the existing analyst-invalid path.
  - In `SKILL.md` §Dry-run mode (the recently-amended section around line 402+), add a third numbered exemption immediately after the Phase 1.5 plan-revision cycle entry: > 3. **Phase 1 Step 2 `**Agent:**` persistence** — `plan_ops__set_task_agent` fires under `--dry-run` so the classifier output reaches disk. Without this, dry-run leaves the plan amnesic and the next real run re-classifies from scratch, defeating the rehearsal value.
  - In `SKILL.md` §Command idioms table (around line 119), add `set-task-agent` to the list of commands that take `--plan-file` (it mutates plan markdown, same family as `commit-task` / `update-plan-header`).
  - In `CLAUDE.md` line 50, extend the "Allowed plan-file mutations" enumeration. Current text: ``Allowed plan-file mutations: `**Status:**` flips, append-only execution-log tail, ...``. New text: ``Allowed plan-file mutations: `**Status:**` flips, `**Agent:**` flips (post-classifier persistence; idempotent, enum-valued), append-only execution-log tail, ...``.
  - Add `test_phase1_step2_persists_agent_to_child_file` in `tests/scripts/test_implement_plan_mcp_e2e.py`:
  - Add `test_phase1_step2_skips_classifier_on_second_invocation`:
  - Add `test_phase1_step2_persists_under_dry_run`:
- **Reversion guidance:** Remove the three SKILL.md additions (Step 2 sub-step, Dry-run exemption #3, command-idioms table row), revert the CLAUDE.md mutation enumeration, and delete the three new test functions. TASK-001's helper + tool remain installed but uncalled by the orchestrator; no operational impact, just dead surface.

**Description:**
Wire orchestrator Phase 1 Step 2 to persist `**Agent:**`; document policy. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-002. See the parent plan's `## Context` and `## Verification` sections for the full intent.)

## Execution log — 20260512T022637 (success)

Starting SHA: `2cd7e30e60e09f2df91fb31f7a15c2ce8966b2d9`  → Ending SHA: `36dcb5bcf144c675c8e7b2767646258021caa00a`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 002 | claude | claude | ship [disagreement] | 36dcb5bc | Codex round-1: needs-rework (test rename); D.5: ship (AC self-contradiction). Hand-fix Two->Three lead-in. Codex round-2: needs-rework (tests bypass orchestrator); D.5: ship (orchestrator is LLM, tests pin data contract). |
