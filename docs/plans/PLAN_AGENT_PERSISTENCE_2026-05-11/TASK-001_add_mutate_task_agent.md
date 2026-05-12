# TASK-001 — Add `mutate_task_agent` pure helper + `plan_ops__set_task_agent` MCP tool

## Goal

Add `mutate_task_agent` pure helper + `plan_ops__set_task_agent` MCP tool

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- In `plan_ops.py`, define `ALLOWED_AGENTS = ("claude", "codex")` near the existing `ALLOWED_TASK_STATUSES` constant.
- In `plan_ops.py`, add `mutate_task_agent(plan_text: str, task_id: str, new_agent: str) -> tuple[str, str]` modeled byte-for-byte on `mutate_task_status` (`plan_ops.py:4173`). Returns `(updated_plan_text, prior_agent)` — `prior_agent` is empty string when no `**Agent:**` bullet existed.
- Idempotent replace when `**Agent:**` is present; insert at the canonical slot (between `**Priority:**` and `**Files:**`, matching `_emit_child`'s order at `plan_ops.py:3187-3197`) when absent. If `**Priority:**` is absent, insert before `**Files:**`; if `**Files:**` is also absent, insert at the end of the metadata-bullet run (before the first non-bullet line).
- Raises `ValueError` on: unknown `new_agent`, task block not found, malformed task block (no metadata bullets at all).
- Add `cmd_set_task_agent`, `_run_set_task_agent`, `_args_to_payload_set_task_agent` mirroring the `update-plan-header` trio (`plan_ops.py:7997-8024`). Subcommand name: `set-task-agent`. Required args: `--plan-file`, `--task-id`, `--agent`.
- Register `plan_ops__set_task_agent` in `plan_ops_mcp_server.py` mirroring the `plan_ops__update_plan_header` block at line 1958. Input schema: `{plan_file: string, task_id: string, agent: enum["claude","codex"]}`, all required, `additionalProperties: false`. Output schema: same envelope shape as `update_plan_header.output` plus an optional `prior_agent: string` field.
- Unit tests in `tests/scripts/test_plan_ops.py` (place next to existing `test_mutate_task_status_*` block if present; otherwise immediately after the `mutate_task_status` test block — locate via `grep -n "mutate_task_status" tests/scripts/test_plan_ops.py` before implementing):

## Tasks

### TASK-001: Add `mutate_task_agent` pure helper + `plan_ops__set_task_agent` MCP tool

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "mutate_task_agent or set_task_agent"`
- **Acceptance criteria:**
  - In `plan_ops.py`, define `ALLOWED_AGENTS = ("claude", "codex")` near the existing `ALLOWED_TASK_STATUSES` constant.
  - In `plan_ops.py`, add `mutate_task_agent(plan_text: str, task_id: str, new_agent: str) -> tuple[str, str]` modeled byte-for-byte on `mutate_task_status` (`plan_ops.py:4173`). Returns `(updated_plan_text, prior_agent)` — `prior_agent` is empty string when no `**Agent:**` bullet existed.
  - Idempotent replace when `**Agent:**` is present; insert at the canonical slot (between `**Priority:**` and `**Files:**`, matching `_emit_child`'s order at `plan_ops.py:3187-3197`) when absent. If `**Priority:**` is absent, insert before `**Files:**`; if `**Files:**` is also absent, insert at the end of the metadata-bullet run (before the first non-bullet line).
  - Raises `ValueError` on: unknown `new_agent`, task block not found, malformed task block (no metadata bullets at all).
  - Add `cmd_set_task_agent`, `_run_set_task_agent`, `_args_to_payload_set_task_agent` mirroring the `update-plan-header` trio (`plan_ops.py:7997-8024`). Subcommand name: `set-task-agent`. Required args: `--plan-file`, `--task-id`, `--agent`.
  - Register `plan_ops__set_task_agent` in `plan_ops_mcp_server.py` mirroring the `plan_ops__update_plan_header` block at line 1958. Input schema: `{plan_file: string, task_id: string, agent: enum["claude","codex"]}`, all required, `additionalProperties: false`. Output schema: same envelope shape as `update_plan_header.output` plus an optional `prior_agent: string` field.
  - Unit tests in `tests/scripts/test_plan_ops.py` (place next to existing `test_mutate_task_status_*` block if present; otherwise immediately after the `mutate_task_status` test block — locate via `grep -n "mutate_task_status" tests/scripts/test_plan_ops.py` before implementing):
- **Reversion guidance:** Delete the helper, the three new CLI functions, the argparse subcommand registration, the MCP tool registration block, and the test functions added in `tests/scripts/test_plan_ops.py`. No call sites elsewhere depend on this surface until TASK-002 lands.

**Description:**
Add `mutate_task_agent` pure helper + `plan_ops__set_task_agent` MCP tool. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-001. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
