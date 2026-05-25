# TASK-001 — Expose `set-task-agent` through MCP

## Goal

Expose `set-task-agent` through MCP

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- `_index.json` includes `set_task_agent` in `tool_names_ordered` and a `plan_ops__set_task_agent` entry pointing to `set_task_agent.input.json`, `set_task_agent.output.json`, and subcommand `set-task-agent`.
- `set_task_agent.input.json` is JSON Schema 2020-12, top-level object, `additionalProperties: false`, and requires:
- `set_task_agent.output.json` mirrors the standard plan_ops JSON envelope and explicitly permits/declares `prior_agent`.
- `plan_ops_mcp_server.py` is regenerated from `_codegen/mcp_tool_registrations.py`; the generated registry contains `plan_ops__set_task_agent` with `run_callable_name == "_run_set_task_agent"` and `args_to_payload_callable_name == "_args_to_payload_set_task_agent"`.
- Registration tests assert the tool is listed and dispatchable through the generated registry.
- Schema tests assert the sidecars exist, parse, validate against the meta-schema, and expose the expected required fields.
- Existing Phase 1 Step 2 MCP e2e tests no longer fail with `Unknown tool: 'plan_ops__set_task_agent'`.

## Tasks

### TASK-001: Expose `set-task-agent` through MCP

- **Status:** Pending
- **Priority:** high
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/schemas/mcp/_index.json`
  - `plugins/plan-executor/scripts/schemas/mcp/set_task_agent.input.json` (create)
  - `plugins/plan-executor/scripts/schemas/mcp/set_task_agent.output.json` (create)
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `tests/scripts/test_plan_ops_mcp_registrations.py`
  - `tests/scripts/test_plan_ops_mcp_schemas.py`
- **Dependencies:** []
- **Test command:** ````bash`
- **Acceptance criteria:**
  - `_index.json` includes `set_task_agent` in `tool_names_ordered` and a `plan_ops__set_task_agent` entry pointing to `set_task_agent.input.json`, `set_task_agent.output.json`, and subcommand `set-task-agent`.
  - `set_task_agent.input.json` is JSON Schema 2020-12, top-level object, `additionalProperties: false`, and requires:
  - `set_task_agent.output.json` mirrors the standard plan_ops JSON envelope and explicitly permits/declares `prior_agent`.
  - `plan_ops_mcp_server.py` is regenerated from `_codegen/mcp_tool_registrations.py`; the generated registry contains `plan_ops__set_task_agent` with `run_callable_name == "_run_set_task_agent"` and `args_to_payload_callable_name == "_args_to_payload_set_task_agent"`.
  - Registration tests assert the tool is listed and dispatchable through the generated registry.
  - Schema tests assert the sidecars exist, parse, validate against the meta-schema, and expose the expected required fields.
  - Existing Phase 1 Step 2 MCP e2e tests no longer fail with `Unknown tool: 'plan_ops__set_task_agent'`.
- **Reversion guidance:** Remove the two sidecar schemas, remove the `_index.json` entry/name, regenerate `plan_ops_mcp_server.py`, and remove any tests added for the MCP exposure.

**Description:**
Expose `set-task-agent` through MCP. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-001. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
