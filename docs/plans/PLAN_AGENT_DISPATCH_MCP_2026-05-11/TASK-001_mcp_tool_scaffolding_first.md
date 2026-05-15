# TASK-001 — MCP tool scaffolding + first variant (code-reviewer-d-claude)

## Goal

MCP tool scaffolding + first variant (code-reviewer-d-claude)

## Context

Add a new MCP tool `plan_ops__build_agent_dispatch_prompt` that accepts `{template_id, context}` and returns `{ok, agent, model, prompt}`. Implement only the first variant (`code-reviewer-d-claude`) in this task. The tool reads `dispatch-templates.md`, extracts the Phase D-Claude section, substitutes placeholders sourced from the supplied plan file (Description + Acceptance criteria) and the supplied context (`files_changed`, optional `target_task_id`), and returns the rendered prompt string. JSON schema validates the input shape; a per-variant `oneOf` branch validates the context payload. Golden test pins the rendered output byte-for-byte to today's hand-substituted form so the cutover in TASK-002 cannot drift the prompt.

## Verification

- `plan_ops.py` gains `cmd_build_agent_dispatch_prompt(args)` returning a JSON envelope `{ok: true, agent: "<subagent>", model: "<model>", prompt: "<rendered>"}` on success and `{ok: false, errors: [...]}` on validation failure. The function is registered as both a CLI subcommand (`build-agent-dispatch-prompt`) and an MCP tool (`plan_ops__build_agent_dispatch_prompt`) per the existing wiring pattern in `_register_mcp_tools` (or whatever the equivalent registration site is).
- The new MCP input schema `build_agent_dispatch_prompt.input.json` requires `template_id` and `context` at the top level, with `template_id` constrained to enum `["code-reviewer-d-claude"]` for now. (The enum widens in TASK-003.) `context` is an object with `oneOf` discriminating on `template_id`. For `code-reviewer-d-claude`, the context schema requires `plan_file` (abs path), `task_id` (`TASK-NNN` or `NNN` or `N`), `target_task_id` (optional, REQUIRED when the resolved child plan file carries >1 H3 task heading per the §`target_task_id` auto-injection rule), and `files_changed` (array of strings — the wrapper's `files_changed` list).
- The new MCP output schema `build_agent_dispatch_prompt.output.json` documents both shapes: success `{ok: true, agent, model, prompt}` and failure `{ok: false, errors: [...], warnings: []}`. `additionalProperties: true` for forward compat.
- `_index.json` lists the new tool under the same shape as existing entries. The MCP server picks it up without further wiring (verified by `venv/bin/python -m plugins.plan-executor.scripts.mcp.plan_ops_server --list-tools | grep build-agent-dispatch-prompt`).
- The renderer reads `dispatch-templates.md` from `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md` (resolved via the same constant `cmd_build_claude_dispatch_input` uses). It extracts the section bounded by `## Phase D-Claude — code-reviewer on Codex work` (inclusive) and the next `## Phase ` heading (exclusive), strips the orchestrator-facing header notes (`> Rendered by ...`), and substitutes the four placeholders documented in the template (`<comma-separated files from Codex wrapper's files_changed>`, `<verbatim from TASK-NNN Description>`, `<verbatim from TASK-NNN Acceptance criteria>`, plus the conditional `target_task_id` first-instruction line per §613).
- Description and Acceptance criteria are extracted from the resolved child plan file using grammar-respecting helpers — NOT regex over the raw file. If `_extract_task_description` / `_extract_task_acceptance_criteria` do not yet exist, add them next to `_extract_task_files_from_plan` in `plan_ops.py` with the same parsing primitives (the implementer must verify the chosen primitives by reading the existing helper before writing new ones).
- **Golden test:** `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py::test_code_reviewer_d_claude_renders_to_golden` reads the fixture plan + context, calls the tool, and asserts the returned `prompt` is byte-for-byte equal to `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/golden_prompt.txt`. The fixture exercises (a) single-H3-heading plan file (no `target_task_id` first-instruction line), (b) >1-H3 plan file (the auto-injection line is prepended). Two test cases.
- **Negative tests:** missing `files_changed` raises a schema validation error with a non-empty `errors[]` array and `ok: false`. Missing `target_task_id` against a multi-heading file raises `errors:[{code: "target-task-id-required", ...}]`.
- **No SKILL or dispatch-templates.md modifications in this task.** The new tool sits unused. SKILL cutover happens in TASK-002.
- `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py tests/scripts/test_plan_ops.py` returns 0.

## Tasks

### TASK-001: MCP tool scaffolding + first variant (code-reviewer-d-claude)

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — add `cmd_build_agent_dispatch_prompt`, helpers, MCP wiring)
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py` (regenerate via `plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py`)
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (create)
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.output.json` (create)
  - `plugins/plan-executor/scripts/schemas/mcp/_index.json` (edit — register the new tool)
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` (create)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/golden_prompt.txt` (create)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/plan_file.md` (create)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/context.json` (create)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - `plan_ops.py` gains `cmd_build_agent_dispatch_prompt(args)` returning a JSON envelope `{ok: true, agent: "<subagent>", model: "<model>", prompt: "<rendered>"}` on success and `{ok: false, errors: [...]}` on validation failure. The function is registered as both a CLI subcommand (`build-agent-dispatch-prompt`) and an MCP tool (`plan_ops__build_agent_dispatch_prompt`) per the existing wiring pattern in `_register_mcp_tools` (or whatever the equivalent registration site is).
  - The new MCP input schema `build_agent_dispatch_prompt.input.json` requires `template_id` and `context` at the top level, with `template_id` constrained to enum `["code-reviewer-d-claude"]` for now. (The enum widens in TASK-003.) `context` is an object with `oneOf` discriminating on `template_id`. For `code-reviewer-d-claude`, the context schema requires `plan_file` (abs path), `task_id` (`TASK-NNN` or `NNN` or `N`), `target_task_id` (optional, REQUIRED when the resolved child plan file carries >1 H3 task heading per the §`target_task_id` auto-injection rule), and `files_changed` (array of strings — the wrapper's `files_changed` list).
  - The new MCP output schema `build_agent_dispatch_prompt.output.json` documents both shapes: success `{ok: true, agent, model, prompt}` and failure `{ok: false, errors: [...], warnings: []}`. `additionalProperties: true` for forward compat.
  - `_index.json` lists the new tool under the same shape as existing entries. The MCP server picks it up without further wiring (verified by `venv/bin/python -m plugins.plan-executor.scripts.mcp.plan_ops_server --list-tools | grep build-agent-dispatch-prompt`).
  - The renderer reads `dispatch-templates.md` from `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md` (resolved via the same constant `cmd_build_claude_dispatch_input` uses). It extracts the section bounded by `## Phase D-Claude — code-reviewer on Codex work` (inclusive) and the next `## Phase ` heading (exclusive), strips the orchestrator-facing header notes (`> Rendered by ...`), and substitutes the four placeholders documented in the template (`<comma-separated files from Codex wrapper's files_changed>`, `<verbatim from TASK-NNN Description>`, `<verbatim from TASK-NNN Acceptance criteria>`, plus the conditional `target_task_id` first-instruction line per §613).
  - Description and Acceptance criteria are extracted from the resolved child plan file using grammar-respecting helpers — NOT regex over the raw file. If `_extract_task_description` / `_extract_task_acceptance_criteria` do not yet exist, add them next to `_extract_task_files_from_plan` in `plan_ops.py` with the same parsing primitives (the implementer must verify the chosen primitives by reading the existing helper before writing new ones).
  - **Golden test:** `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py::test_code_reviewer_d_claude_renders_to_golden` reads the fixture plan + context, calls the tool, and asserts the returned `prompt` is byte-for-byte equal to `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/golden_prompt.txt`. The fixture exercises (a) single-H3-heading plan file (no `target_task_id` first-instruction line), (b) >1-H3 plan file (the auto-injection line is prepended). Two test cases.
  - **Negative tests:** missing `files_changed` raises a schema validation error with a non-empty `errors[]` array and `ok: false`. Missing `target_task_id` against a multi-heading file raises `errors:[{code: "target-task-id-required", ...}]`.
  - **No SKILL or dispatch-templates.md modifications in this task.** The new tool sits unused. SKILL cutover happens in TASK-002.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py tests/scripts/test_plan_ops.py` returns 0.
- **Reversion guidance:** revert the new test file, the two new schema files, the `_index.json` registration line, and the new function plus helpers in `plan_ops.py`. No SKILL-level changes were made.

**Description:**
Add a new MCP tool `plan_ops__build_agent_dispatch_prompt` that accepts `{template_id, context}` and returns `{ok, agent, model, prompt}`. Implement only the first variant (`code-reviewer-d-claude`) in this task. The tool reads `dispatch-templates.md`, extracts the Phase D-Claude section, substitutes placeholders sourced from the supplied plan file (Description + Acceptance criteria) and the supplied context (`files_changed`, optional `target_task_id`), and returns the rendered prompt string. JSON schema validates the input shape; a per-variant `oneOf` branch validates the context payload. Golden test pins the rendered output byte-for-byte to today's hand-substituted form so the cutover in TASK-002 cannot drift the prompt.
