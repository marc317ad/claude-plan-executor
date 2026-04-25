# TASK-007 — Promote `target_task_id` to first-class field across implementer/reviewer templates

## Goal

Eliminate the manual prompt augmentation that the orchestrator performed nine times during run 20260425T041800 (TASK-027A/B/C × classifier/implement/review). When a child plan file declares >1 `### TASK-NNN:` H3 heading (shared-file siblings), the dispatch templates must auto-inject "specifically `### TASK-NNN:`" so the agent reads the right block without orchestrator hand-holding.

## Context

**The friction.** TASK-027 ships three sibling sub-tasks (027A/B/C) under a single markdown file (`TASK-027_codex_review_prompt_and_adaptation_flagging.md`). The Phase A-single classifier template currently says "Read the child plan file verbatim (it carries a single `### TASK-NNN:` H3 heading...)". For TASK-027 the file carries three. The orchestrator manually injected "specifically `### TASK-027B:`" prose into nine dispatch prompts.

**Existing precedent.** The Phase 1.5a `plan-author` template already conditions on `target_task_id` (Variant A vs Variant B). Replicate that convention in:

- §Phase A-single (classifier)
- §Phase B (Claude implementer + Codex implementer dispatch render path)
- §Phase D-Codex (Codex cross-review)
- §Phase D-Claude (Claude cross-review)
- §Phase D.5 (third-opinion)
- §Phase B-rework (D.2a.5 remediation)
- §Phase B-narrow-remediation (D.2a.6 narrow remediation)

**The render contract.** The renderer (orchestrator-side for Claude templates; `plan_codex_dispatch.py` for Codex templates) must:

1. Detect when the resolved child plan file declares >1 `### TASK-` H3 heading.
2. If `target_task_id` is provided AND the file declares >1 heading, emit "Implement specifically `### TASK-{target_task_id}:`" as the first instruction line of the dispatch prompt.
3. If `target_task_id` is provided AND the file declares exactly 1 heading, emit nothing extra (the heading is unambiguous).
4. If `target_task_id` is absent AND the file declares >1 heading, emit a hard error from the renderer; the orchestrator should always supply the field for shared-file children.

**`build-tasks` integration.** Today `build-tasks` emits one `extra-task-heading` warning per shared-file chunk. With this task in place, the warning becomes a hard hint to the orchestrator: "this chunk MUST be dispatched with `target_task_id`." Tighten the warning text accordingly.

**Scope.** Spec edits to seven dispatch-template sections + render-path code in `plan_codex_dispatch.py` (Codex) and the orchestrator-side template renderer (Claude). Tests cover the auto-injection for both >1-heading and 1-heading cases.

## Verification

- For every dispatch template that names a child plan file (the seven sections above), the template document declares a `{{target_task_id}}` interpolation point and documents the auto-injection rule.
- `parse_task_block` in `plan_codex_dispatch.py` already accepts a `target_task_id` argument; the render path now passes it through and emits the auto-injection line when the heading count condition fires.
- The orchestrator-side Claude renderer (in `plan_ops.py` or wherever templates render) honours the same rule.
- `build-tasks --print-warnings` for a shared-file plan emits an `extra-task-heading` warning whose message names `target_task_id` as the disambiguator.
- New tests:
  - Render a Phase B Codex implement prompt for a shared-file task with `target_task_id` set; assert "Implement specifically `### TASK-XXX:`" is the first instruction line.
  - Render the same prompt for a single-heading file; assert no extra line is emitted.
  - Render with `target_task_id=None` for a shared-file file; assert the renderer raises a structured error naming the missing argument.
- Existing dispatch-template tests stay green.

## Tasks

### TASK-007: target_task_id first-class field

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — seven sections)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit — `cmd_implement` / `cmd_review` render path; `parse_task_block` already accepts the arg)
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — Claude template renderer, `build-tasks` warning text, any `dispatch-prompt` subcommands)
  - `tests/scripts/test_plan_codex_dispatch.py` (regression tests)
  - `tests/scripts/test_plan_ops.py` (regression tests)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py tests/scripts/test_plan_ops.py -k "target_task_id or extra_task_heading"`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — full file
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::parse_task_block`
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::cmd_implement`
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::cmd_review`
- **Acceptance criteria:**
  - Each of the seven dispatch-template sections (Phase A-single, Phase B, Phase D-Codex, Phase D-Claude, Phase D.5, Phase B-rework, Phase B-narrow-remediation) names `target_task_id` as a first-class field and documents the auto-injection rule (heading count condition + first-instruction-line emission).
  - `plan_codex_dispatch.py` `cmd_implement` and `cmd_review` (and any other dispatch entry points) accept and forward `--target-task-id` and emit the auto-injection line when the conditions fire.
  - The Claude-side render path in `plan_ops.py` (or its current home) does the same.
  - Renderer raises `MissingTargetTaskIdError` (or named-equivalent) when `target_task_id` is None AND the resolved child file declares >1 heading. The error envelope identifies the offending file.
  - `build-tasks` warning text for `extra-task-heading` names `target_task_id` as the disambiguator.
  - Tests cover: (a) >1-heading + target_task_id → injection; (b) 1-heading + target_task_id → no injection; (c) >1-heading + None target_task_id → structured renderer error; (d) backward compat: single-task plan files render unchanged.
  - SKILL.md (or the dispatch-templates section that documents Phase A-single onwards) states the rule plainly so the orchestrator reads it on-protocol.
- **Reversion guidance:** revert template edits + render-path conditionals + warning-text change. Pre-change behavior (no auto-injection; orchestrator-side manual prose) returns. The `parse_task_block` argument was already present on entry, so no signature shrink is needed.

**Description:**
Make `target_task_id` a first-class dispatch field for shared-file children. The renderer auto-injects "Implement specifically `### TASK-XXX:`" when the child file carries multiple H3 headings. This eliminates the orchestrator-side manual prose injection that ran nine times during run 20260425T041800.

**Implementation notes:**
- The seven sections share enough structure that a single canonical "auto-injection rule" paragraph in `dispatch-templates.md` referenced by each section is the cheapest spec form.
- The renderer should detect heading count by parsing the child file's H3 headings (cheap line-prefix scan, no AST needed).
- Resist the temptation to ALWAYS emit the line — when the file has exactly one heading, the injection adds noise. The condition is the load-bearing part.
