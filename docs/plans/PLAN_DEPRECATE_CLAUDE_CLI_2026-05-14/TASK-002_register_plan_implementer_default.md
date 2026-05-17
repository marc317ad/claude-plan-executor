# TASK-002 — Register `plan-implementer-default` Agent template, factor shared schema inliner, cut over Phase B to in-process Agent dispatch

## Goal

Register `plan-implementer-default` Agent template, factor shared schema inliner, cut over Phase B to in-process Agent dispatch

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- Register `plan-implementer-default` template_id in `_AGENT_DISPATCH_TEMPLATE_HEADING` (heading anchor in `dispatch-templates.md`) and `_AGENT_DISPATCH_TEMPLATE_MODEL` (`"opus"`). Mirror the `plan-remediator-narrow` registration shape at `plan_ops.py:12175–12197`.
- Factor `_inline_implementer_result_schema(prompt: str) -> tuple[str, dict]` in `plan_ops.py`. Move the schema-loading + prompt-injection logic from `build_claude_dispatch_input` (~`plan_ops.py:13055–13153`) into the helper. Both `build_claude_dispatch_input` AND the new `plan-implementer-default` Agent renderer call the helper. The schema dict the helper returns continues to be copied into the wrapper envelope at the existing site (`plan_ops.py:13155–13164`); the Agent renderer does NOT need the dict (the Agent path's caller validates via `claude_envelope_extract` which can parse the result without the envelope-side schema copy).
- Add `test_inline_implementer_result_schema_byte_stable_vs_wrapper_path`: build a minimal claude_dispatch_input and a minimal Agent-renderer payload through both code paths; assert the inlined-schema portion of the prompt is byte-identical. Guards against drift between the script-runner Claude path and the orchestrator Agent path.
- SKILL.md:447 (the "Claude tasks" bullet under §Phase B dispatch routing) is rewritten to reference §Canonical Agent dispatch recipe with `template_id:"plan-implementer-default"` and `model:"opus"`. The bullet documents the orchestrator-side cleanup wrap: call `cleanup.snapshot_baseline(repo_root)` immediately before the Agent dispatch and `cleanup.apply_cleanup(baseline, declared_files_changed, repo_root, authorization_source="orchestrator-declared-scope", unattended_revert_policy="preserve-only")` immediately after. Result dict feeds the existing scope-violation routing unchanged.
- SKILL.md gains a short §Cleanup-around-Agent-dispatch sub-recipe (5–10 lines) that the §Phase B and later TASKs reference. Canonical pattern: extract `declared_files_changed` via `_extract_task_files_from_plan`; snapshot; Agent-dispatch; apply_cleanup with `authorization_source="orchestrator-declared-scope"` (write-authorized) or `"orchestrator-empty-scope-readonly"` (analyst — but analyst skips the wrap entirely per Decision 7); route the cleanup result through the existing scope-violation handling.
- The Phase B template body in `dispatch-templates.md` (lines 423–533) is updated to the rephrased instruction (above). The existing `output_instructions.format: "json"` illustrative line is removed; the new line points the subagent at the inlined schema that will appear in its prompt.
- `agents/plan-implementer.md`'s line-105 paragraph is rewritten: trigger is now "in-process Agent dispatch via the `plan-implementer-default` template (the `/implement-plan` orchestration default) OR wrapper dispatch via `plan_claude_dispatch.py run` (the `implement_plan.py` script-runner path)". Both paths inline the same schema via the shared `_inline_implementer_result_schema` helper. The "you MUST emit" JSON-result requirement is unchanged.
- End-to-end smoke (manual, in §Verification): run `/implement-plan` against a one-task plan with a single Claude implementer; observe (i) no `claude -p` subprocess spawned during the SKILL/MCP path (`pgrep -af 'claude .*-p'` empty), (ii) cleanup result emitted in the run log with `authorization_source="orchestrator-declared-scope"`, (iii) commit lands cleanly.
- All existing `test_plan_ops*`, `test_claude_dispatch*`, and `test_plan_ops_build_agent_dispatch_prompt*` tests still green.

## Tasks

### TASK-002: Register `plan-implementer-default` Agent template, factor shared schema inliner, cut over Phase B to in-process Agent dispatch

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` — register `plan-implementer-default` in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` (~line 12175–12197); factor `_inline_implementer_result_schema(prompt) -> tuple[str, dict]` out of `build_claude_dispatch_input` (~line 13055–13164) and call it from both renderers.
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` — add `plan-implementer-default` to the template_id enum and add `planImplementerDefaultContext` $def (mechanically required by the new template registration).
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — Phase B template body (lines 423–533): replace illustrative `output_instructions.format` prose with a clear "the orchestrator's renderer inlines the implementer result schema below; emit one JSON object matching it" instruction. Do NOT duplicate the schema text in the template body — the inliner injects it at render time so the wire-format remains single-sourced from `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json`.
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — Phase B section (~line 447); add §Cleanup-around-Agent-dispatch sub-recipe.
  - `plugins/plan-executor/agents/plan-implementer.md` — BUG-146 paragraph at line ~105.
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` — add coverage for the new template_id and the shared inliner.
  - `tests/scripts/test_plan_ops.py` (or wherever `build_claude_dispatch_input` is tested) — pin that the shared inliner produces byte-equivalent output to the pre-refactor wrapper path (regression guard for the script-runner Claude path).
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt*.py tests/scripts/test_dispatch_cleanup*.py tests/scripts/test_plan_ops.py -k "claude_dispatch or build_agent or inline_implementer"`
- **Acceptance criteria:**
  - Register `plan-implementer-default` template_id in `_AGENT_DISPATCH_TEMPLATE_HEADING` (heading anchor in `dispatch-templates.md`) and `_AGENT_DISPATCH_TEMPLATE_MODEL` (`"opus"`). Mirror the `plan-remediator-narrow` registration shape at `plan_ops.py:12175–12197`.
  - Factor `_inline_implementer_result_schema(prompt: str) -> tuple[str, dict]` in `plan_ops.py`. Move the schema-loading + prompt-injection logic from `build_claude_dispatch_input` (~`plan_ops.py:13055–13153`) into the helper. Both `build_claude_dispatch_input` AND the new `plan-implementer-default` Agent renderer call the helper. The schema dict the helper returns continues to be copied into the wrapper envelope at the existing site (`plan_ops.py:13155–13164`); the Agent renderer does NOT need the dict (the Agent path's caller validates via `claude_envelope_extract` which can parse the result without the envelope-side schema copy).
  - Add `test_inline_implementer_result_schema_byte_stable_vs_wrapper_path`: build a minimal claude_dispatch_input and a minimal Agent-renderer payload through both code paths; assert the inlined-schema portion of the prompt is byte-identical. Guards against drift between the script-runner Claude path and the orchestrator Agent path.
  - SKILL.md:447 (the "Claude tasks" bullet under §Phase B dispatch routing) is rewritten to reference §Canonical Agent dispatch recipe with `template_id:"plan-implementer-default"` and `model:"opus"`. The bullet documents the orchestrator-side cleanup wrap: call `cleanup.snapshot_baseline(repo_root)` immediately before the Agent dispatch and `cleanup.apply_cleanup(baseline, declared_files_changed, repo_root, authorization_source="orchestrator-declared-scope", unattended_revert_policy="preserve-only")` immediately after. Result dict feeds the existing scope-violation routing unchanged.
  - SKILL.md gains a short §Cleanup-around-Agent-dispatch sub-recipe (5–10 lines) that the §Phase B and later TASKs reference. Canonical pattern: extract `declared_files_changed` via `_extract_task_files_from_plan`; snapshot; Agent-dispatch; apply_cleanup with `authorization_source="orchestrator-declared-scope"` (write-authorized) or `"orchestrator-empty-scope-readonly"` (analyst — but analyst skips the wrap entirely per Decision 7); route the cleanup result through the existing scope-violation handling.
  - The Phase B template body in `dispatch-templates.md` (lines 423–533) is updated to the rephrased instruction (above). The existing `output_instructions.format: "json"` illustrative line is removed; the new line points the subagent at the inlined schema that will appear in its prompt.
  - `agents/plan-implementer.md`'s line-105 paragraph is rewritten: trigger is now "in-process Agent dispatch via the `plan-implementer-default` template (the `/implement-plan` orchestration default) OR wrapper dispatch via `plan_claude_dispatch.py run` (the `implement_plan.py` script-runner path)". Both paths inline the same schema via the shared `_inline_implementer_result_schema` helper. The "you MUST emit" JSON-result requirement is unchanged.
  - End-to-end smoke (manual, in §Verification): run `/implement-plan` against a one-task plan with a single Claude implementer; observe (i) no `claude -p` subprocess spawned during the SKILL/MCP path (`pgrep -af 'claude .*-p'` empty), (ii) cleanup result emitted in the run log with `authorization_source="orchestrator-declared-scope"`, (iii) commit lands cleanly.
  - All existing `test_plan_ops*`, `test_claude_dispatch*`, and `test_plan_ops_build_agent_dispatch_prompt*` tests still green.
- **Reversion guidance:** Revert the template_id registration, inline the schema-loading code back into `build_claude_dispatch_input`, revert SKILL.md:447 and the §Cleanup-around-Agent-dispatch sub-recipe, revert dispatch-templates.md Phase B prose, revert plan-implementer.md:105, drop the byte-stable test.

**Description:**
Register `plan-implementer-default` Agent template, factor shared schema inliner, cut over Phase B to in-process Agent dispatch. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-002. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
