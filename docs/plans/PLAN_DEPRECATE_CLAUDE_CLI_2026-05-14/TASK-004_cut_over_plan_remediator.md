# TASK-004 — Cut over `plan-remediator` (rework / narrow-remediation / rescue) to confirm in-process Agent dispatch and reconcile SKILL prose

## Goal

Cut over `plan-remediator` (rework / narrow-remediation / rescue) to confirm in-process Agent dispatch and reconcile SKILL prose

## Context

Auto-decomposed child for TASK-004. See the source plan for broader context.

## Verification

- SKILL.md:97 (the §Claude wrapper dispatch recipe (canonical) opening sentence) is rewritten to omit `plan-remediator` from the enumeration. After this TASK the recipe section retains only the analyst+implementer wrapper-historical context (and is itself flagged as wrapper-historical pending TASK-005 deletion).
- SKILL.md:559, 579, 595 (the rework / narrow / rescue dispatch instructions) are verified accurate vs. the live code path: each calls `plan_ops__build_agent_dispatch_prompt` with the appropriate `template_id`, dispatches via `Agent(subagent_type:"plan-remediator", ...)`, and is wrapped by the §Cleanup-around-Agent-dispatch sub-recipe (remediator IS file-editing, unlike analyst — cleanup applies).
- Each `plan-remediator` dispatch site (rework, narrow, rescue) is wrapped with `cleanup.snapshot_baseline()` / `cleanup.apply_cleanup(..., authorization_source="orchestrator")` against the originating task's `declared_files_changed` per the §Cleanup-around-Agent-dispatch sub-recipe.
- Templates lines 778–905+ are Agent-path-clean (no wrapper-only knobs).
- `agents/plan-remediator.md` has no remaining `plan_claude_dispatch.py` references.
- End-to-end smoke (manual, in §Verification): force a Phase D `needs-rework` outcome on a one-task plan; observe rework dispatch is in-process, cleanup result is logged with orchestrator source, no `claude -p` subprocess.

## Tasks

### TASK-004: Cut over `plan-remediator` (rework / narrow-remediation / rescue) to confirm in-process Agent dispatch and reconcile SKILL prose

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (lines 97 — drop `plan-remediator` from the wrapper-recipe enumeration; lines 559, 579, 595 — verify already-Agent prose is accurate)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (Phase B-rework lines 778–836, Phase B-narrow-remediation lines 838–905, Phase D.4-rescue lines 906+ — verify each template body is Agent-path-clean)
  - `plugins/plan-executor/agents/plan-remediator.md` (revise any wrapper references)
- **Dependencies:** [002, 003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt tests/scripts/test_dispatch_cleanup`
- **Acceptance criteria:**
  - SKILL.md:97 (the §Claude wrapper dispatch recipe (canonical) opening sentence) is rewritten to omit `plan-remediator` from the enumeration. After this TASK the recipe section retains only the analyst+implementer wrapper-historical context (and is itself flagged as wrapper-historical pending TASK-005 deletion).
  - SKILL.md:559, 579, 595 (the rework / narrow / rescue dispatch instructions) are verified accurate vs. the live code path: each calls `plan_ops__build_agent_dispatch_prompt` with the appropriate `template_id`, dispatches via `Agent(subagent_type:"plan-remediator", ...)`, and is wrapped by the §Cleanup-around-Agent-dispatch sub-recipe (remediator IS file-editing, unlike analyst — cleanup applies).
  - Each `plan-remediator` dispatch site (rework, narrow, rescue) is wrapped with `cleanup.snapshot_baseline()` / `cleanup.apply_cleanup(..., authorization_source="orchestrator")` against the originating task's `declared_files_changed` per the §Cleanup-around-Agent-dispatch sub-recipe.
  - Templates lines 778–905+ are Agent-path-clean (no wrapper-only knobs).
  - `agents/plan-remediator.md` has no remaining `plan_claude_dispatch.py` references.
  - End-to-end smoke (manual, in §Verification): force a Phase D `needs-rework` outcome on a one-task plan; observe rework dispatch is in-process, cleanup result is logged with orchestrator source, no `claude -p` subprocess.
- **Reversion guidance:** Restore `plan-remediator` to SKILL.md:97 enumeration; revert any cleanup-wrap additions at the rework / narrow / rescue dispatch sites.

**Description:**
Cut over `plan-remediator` (rework / narrow-remediation / rescue) to confirm in-process Agent dispatch and reconcile SKILL prose. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-004. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
