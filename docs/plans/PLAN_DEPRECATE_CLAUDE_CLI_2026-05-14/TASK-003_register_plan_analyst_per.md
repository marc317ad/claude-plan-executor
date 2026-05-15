# TASK-003 — Register `plan-analyst-per-child` Agent template and cut over Phase 1 Step 2 fan-out to in-process Agent dispatch

## Goal

Register `plan-analyst-per-child` Agent template and cut over Phase 1 Step 2 fan-out to in-process Agent dispatch

## Context

Auto-decomposed child for TASK-003. See the source plan for broader context.

## Verification

- SKILL.md:308–316 (the Phase 1 Step 2 pseudo-syntax block) is rewritten to dispatch each child via `Agent(subagent_type:"plan-analyst", prompt:<rendered>)` rather than per-child `Bash(plan_claude_dispatch.py run --input <payload_i>)`. Parallelism is achieved by emitting N `Agent` tool calls in a single message — document this explicitly in the §Phase 1 Step 2 prose.
- Register `plan-analyst-per-child` template_id in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at `plan_ops.py:12175–12197`. Model: `"sonnet"` (matches the existing analyst classifier model tier). Heading anchor: the Phase A-single template at `dispatch-templates.md:23–99`.
- Cleanup wrap is NOT applied to `plan-analyst` — analyst is read-only by contract (it classifies; it does not edit files). Document this exception in the §Cleanup-around-Agent-dispatch sub-recipe as the single carve-out: classifier-shaped dispatches skip cleanup.
- The Phase A-single template body in `dispatch-templates.md:23–99` is verified Agent-path-clean; if it references `output_instructions.format` or other wrapper-only knobs, those are dropped.
- `agents/plan-analyst.md`'s JSON-output instruction is verified wrapper-agnostic; if it mentions `plan_claude_dispatch.py`, those references are removed.
- End-to-end smoke (manual, in §Verification): run `/implement-plan` against a 3-child plan with no `**Agent:**` annotations; observe (i) Phase 1 Step 2 fan-out emits 3 in-process Agent dispatches in one message, (ii) no `claude -p` subprocess, (iii) classifier results land in plan files via the existing post-classifier persistence path.

## Tasks

### TASK-003: Register `plan-analyst-per-child` Agent template and cut over Phase 1 Step 2 fan-out to in-process Agent dispatch

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` — register `plan-analyst-per-child` in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at lines 12175–12197.
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (Phase 1 Step 2 section, lines 304–316)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (Phase A-single template, lines 23–99 — verify it already targets the Agent path; if it references the wrapper's output-format flag, drop that. The classifier-result schema is already inlined in the template body — Codex Finding 5 confirms — so no shared-inliner work needed for analyst.)
  - `plugins/plan-executor/agents/plan-analyst.md` (verify the JSON-output instruction is wrapper-agnostic; revise if it references `plan_claude_dispatch.py`)
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt*.py` (add coverage for the new template_id)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt`
- **Acceptance criteria:**
  - SKILL.md:308–316 (the Phase 1 Step 2 pseudo-syntax block) is rewritten to dispatch each child via `Agent(subagent_type:"plan-analyst", prompt:<rendered>)` rather than per-child `Bash(plan_claude_dispatch.py run --input <payload_i>)`. Parallelism is achieved by emitting N `Agent` tool calls in a single message — document this explicitly in the §Phase 1 Step 2 prose.
  - Register `plan-analyst-per-child` template_id in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at `plan_ops.py:12175–12197`. Model: `"sonnet"` (matches the existing analyst classifier model tier). Heading anchor: the Phase A-single template at `dispatch-templates.md:23–99`.
  - Cleanup wrap is NOT applied to `plan-analyst` — analyst is read-only by contract (it classifies; it does not edit files). Document this exception in the §Cleanup-around-Agent-dispatch sub-recipe as the single carve-out: classifier-shaped dispatches skip cleanup.
  - The Phase A-single template body in `dispatch-templates.md:23–99` is verified Agent-path-clean; if it references `output_instructions.format` or other wrapper-only knobs, those are dropped.
  - `agents/plan-analyst.md`'s JSON-output instruction is verified wrapper-agnostic; if it mentions `plan_claude_dispatch.py`, those references are removed.
  - End-to-end smoke (manual, in §Verification): run `/implement-plan` against a 3-child plan with no `**Agent:**` annotations; observe (i) Phase 1 Step 2 fan-out emits 3 in-process Agent dispatches in one message, (ii) no `claude -p` subprocess, (iii) classifier results land in plan files via the existing post-classifier persistence path.
- **Reversion guidance:** Revert SKILL.md:308–316 to the per-child wrapper Bash block, revert any analyst template body / agent-doc edits, drop any new template_id registration.

**Description:**
Register `plan-analyst-per-child` Agent template and cut over Phase 1 Step 2 fan-out to in-process Agent dispatch. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-003. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
