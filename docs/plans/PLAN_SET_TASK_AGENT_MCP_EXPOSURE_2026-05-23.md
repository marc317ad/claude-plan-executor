# PLAN - Expose `set-task-agent` through MCP and restore dry-run agent persistence

**Status:** Pending
**Created:** 2026-05-23
**Base branch:** main

## Goal

Fix the Phase 1 Step 2 dry-run persistence gap where classifier output resolves an implementation agent but does not persist `**Agent:**` back to child plan files through MCP. The immediate failure is that the orchestrator contract calls `plan_ops__set_task_agent`, but MCP does not expose that tool, so dry-run leaves the plan amnesic and the next real run classifies the same children again.

This is a targeted repair suitable for a Codex implementation and Claude review. It does not require a comprehensive `/implement-plan` run unless the executor itself needs to be exercised end to end.

## Findings

### 1. The CLI/direct helper exists, but MCP does not expose it

`plan_ops.py` already contains the implementation:

1. `ALLOWED_AGENTS = ("claude", "codex")`
2. `mutate_task_agent(plan_text, task_id, new_agent)`
3. `_args_to_payload_set_task_agent`
4. `_run_set_task_agent`
5. `cmd_set_task_agent`
6. argparse subcommand `set-task-agent`

Focused verification passes:

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "mutate_task_agent or set_task_agent"
```

Observed result on 2026-05-23: `12 passed`.

### 2. MCP registry source of truth omits `set_task_agent`

The generated MCP server registry is driven by:

- `plugins/plan-executor/scripts/schemas/mcp/_index.json`
- sidecar schemas under `plugins/plan-executor/scripts/schemas/mcp/`
- `plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py`

Current `_index.json` has no `set_task_agent` entry, and the schema directory has no `set_task_agent.input.json` / `set_task_agent.output.json`. Consequently, `plan_ops_mcp_server.py` does not register `plan_ops__set_task_agent`.

Reproduction:

```bash
venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2"
```

Observed result on 2026-05-23: all three selected tests fail with `Unknown tool: 'plan_ops__set_task_agent'`.

### 3. The SKILL contract already expects MCP persistence under dry-run

`plugins/plan-executor/skills/implement-plan/SKILL.md` already instructs the orchestrator to call `plan_ops__set_task_agent` after successful per-child classifier dispatches, and the dry-run section explicitly exempts this mutation from dry-run gating.

That means the immediate gap is not the documented protocol. The gap is the missing MCP tool exposure needed to execute the protocol.

### 4. The Python runner likely has a separate persistence gap

`plugins/plan-executor/scripts/implement_plan.py` resolves assignments in memory and writes the schedule, but there is no facade method for `set_task_agent` and no call that mutates child plan files before `write_schedule`.

This is separate from the immediate MCP failure. It should either be fixed in this plan as a second task or deliberately deferred with a test that documents the remaining behavior. The recommended path is to include it as a narrow second task after the MCP exposure lands, because otherwise the direct runner can still reproduce the "dry-run classifies again" symptom even after MCP sessions are fixed.

## Decisions

1. **Minimal first repair:** expose the already-implemented `set-task-agent` command through MCP by adding schema sidecars, `_index.json` entries, and regenerated `plan_ops_mcp_server.py`.
2. **Preserve generated-code discipline:** do not hand-edit the generated MCP registry block. Update `_index.json` and sidecars, then run `plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py`.
3. **Keep schema shape consistent with existing mutators:** model the input/output schemas on `update_plan_header`, with required `plan_file`, `task_id`, and `agent`, and a permissive output envelope that includes `prior_agent`.
4. **Add a direct regression assertion:** pin that `plan_ops__set_task_agent` appears in the registered tool list and dispatches to `_run_set_task_agent`.
5. **Runner wiring is second-order but real:** after MCP exposure is fixed, add direct-runner persistence or record a deliberate follow-up. Do not mix broad assignment-policy refactors into the MCP fix.

## Scope

In scope:

- `plugins/plan-executor/scripts/schemas/mcp/_index.json`
- `plugins/plan-executor/scripts/schemas/mcp/set_task_agent.input.json` (new)
- `plugins/plan-executor/scripts/schemas/mcp/set_task_agent.output.json` (new)
- `plugins/plan-executor/scripts/plan_ops_mcp_server.py` (regenerated only)
- `tests/scripts/test_plan_ops_mcp_registrations.py`
- `tests/scripts/test_plan_ops_mcp_schemas.py`
- `tests/scripts/test_implement_plan_mcp_e2e.py` (existing failing tests should pass; add assertions only if needed)
- `plugins/plan-executor/scripts/implement_plan.py` (runner follow-up task only)
- `tests/scripts/test_implement_plan_runner_e2e.py` or closest runner test file (runner follow-up task only)

Out of scope:

- Rewriting `mutate_task_agent` or changing its CLI behavior; it already passes focused tests.
- Changing the `plan-analyst` classifier prompt.
- Persisting `classification_reason` to child plan files.
- Caching agent assignments in `00_INDEX.json`.
- Retroactively filling `**Agent:**` in existing plan directories.
- Broad assignment-policy redesign.

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
- **Dependencies:** none
- **Acceptance criteria:**
  - `_index.json` includes `set_task_agent` in `tool_names_ordered` and a `plan_ops__set_task_agent` entry pointing to `set_task_agent.input.json`, `set_task_agent.output.json`, and subcommand `set-task-agent`.
  - `set_task_agent.input.json` is JSON Schema 2020-12, top-level object, `additionalProperties: false`, and requires:
    - `plan_file: string`
    - `task_id: string`
    - `agent: enum ["claude", "codex"]`
  - `set_task_agent.output.json` mirrors the standard plan_ops JSON envelope and explicitly permits/declares `prior_agent`.
  - `plan_ops_mcp_server.py` is regenerated from `_codegen/mcp_tool_registrations.py`; the generated registry contains `plan_ops__set_task_agent` with `run_callable_name == "_run_set_task_agent"` and `args_to_payload_callable_name == "_args_to_payload_set_task_agent"`.
  - Registration tests assert the tool is listed and dispatchable through the generated registry.
  - Schema tests assert the sidecars exist, parse, validate against the meta-schema, and expose the expected required fields.
  - Existing Phase 1 Step 2 MCP e2e tests no longer fail with `Unknown tool: 'plan_ops__set_task_agent'`.
- **Test command:**
  ```bash
  venv/bin/python plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py
  venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_plan_ops_mcp_schemas.py -k "set_task_agent or registry or schema"
  venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2"
  ```
- **Implementation notes:** The sidecar schemas should follow the style of `update_plan_header.input.json` and `update_plan_header.output.json`. Do not edit the generated registry block by hand.
- **Review focus:** Claude should verify the schema and registry shape, confirm generated code stability, and confirm the fix addresses the MCP unknown-tool failure without widening mutator permissions.
- **Reversion guidance:** Remove the two sidecar schemas, remove the `_index.json` entry/name, regenerate `plan_ops_mcp_server.py`, and remove any tests added for the MCP exposure.

### TASK-002: Persist resolved agents in the Python runner path

- **Status:** Pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_runner_e2e.py` or closest focused runner test file
- **Dependencies:** 001
- **Acceptance criteria:**
  - `PlanOpsFacade` exposes a narrow `set_task_agent(**payload)` method that delegates to direct plan_ops execution, matching the existing `build_tasks` / `write_schedule` style.
  - After assignment resolution and before schedule write, the runner persists resolved per-task implementer agents to child plan files when:
    - the task has a resolvable child `plan_file`;
    - the resolved implementer provider is `claude` or `codex`;
    - the existing task metadata lacked `agent` or differs from the resolved provider.
  - The persistence runs under `--dry-run`, matching the SKILL dry-run exemption.
  - Explicit assignment overrides are handled deliberately:
    - either persist the explicit assignment to the child file, making the next run deterministic without the flag;
    - or do not persist explicit overrides and document that decision in a test name/comment.
  - Runner tests cover:
    - a dry-run on a child without `**Agent:**` writes the bullet;
    - a second dry-run observes the persisted bullet and does not need to re-resolve from missing task metadata;
    - schedule `tasks[*].agent` matches the persisted child-file value after the run.
- **Test command:**
  ```bash
  venv/bin/pytest -q tests/scripts/test_implement_plan_runner_e2e.py -k "agent and dry_run"
  ```
- **Implementation notes:** Keep this narrowly scoped. Do not refactor assignment policy. If child `plan_file` resolution is ambiguous, fail with a clear runner error rather than silently skipping persistence.
- **Review focus:** Claude should verify the runner change matches the MCP/SKILL semantics, especially dry-run behavior and explicit override handling.
- **Reversion guidance:** Remove the facade method, remove the post-assignment persistence call, and delete the runner tests added in this task. TASK-001 remains valid independently.

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "mutate_task_agent or set_task_agent"`
2. `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_plan_ops_mcp_schemas.py -k "set_task_agent or registry or schema"`
3. `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2"`
4. `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_e2e.py -k "agent and dry_run"`
5. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`

Manual smoke:

1. Use a small decomposed plan with one child lacking `**Agent:**`.
2. Run the MCP/SKILL path through dry-run; confirm `plan_ops__set_task_agent` is available and writes `- **Agent:** <agent>` into the child.
3. Run dry-run again; confirm the classifier fan-out is skipped because `build-tasks` now sees `agent`.
4. Run the Python runner dry-run path on the same kind of fixture; confirm it persists the same bullet or document why TASK-002 was intentionally deferred.

