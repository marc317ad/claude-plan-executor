# PLAN - Implement-plan plan-ops transport bootstrap

**Status:** Complete
**Created:** 2026-05-01
**Base branch:** main
**Implementer:** codex

## Goal

Make `/implement-plan` handle the observed Claude Code transport split correctly: prefer `plan_ops__*` MCP tools when the session exposes them, but allow the canonical `plan_ops.py` CLI as a narrow fallback when the MCP server is connected and Claude Code does not surface those tools to the noninteractive model session.

## Context

`PLAN_MCP_MANIFEST_FIX_2026-05-01.md` fixed the server bootstrap. `claude mcp list` now reports `plugin:plan-executor:plan-ops` as connected, and direct MCP client tests list the expected `plan_ops__*` tools. The remaining failure is session exposure: `claude -p` can still show only built-in tools while `/implement-plan` requires plan operations through `plan_ops__*`.

The current skill text treats MCP visibility as mandatory and tells the orchestrator to halt when tools are not exposed. That is too strict for a healthy connected server hidden from the current session. The correct fallback is not inline Python or ad hoc parsing; it is the same canonical `plan_ops.py` subcommands behind the MCP server.

## Scope

In scope:

- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- A regression test under `tests/scripts/`

Out of scope:

- Changes to `plan_ops.py`, `plan_ops_mcp_server.py`, or MCP registration code.
- Reworking Claude Code plugin loading.
- Running the Phase D / Phase 1.5 task plans.

## Tasks

### TASK-001: Document the active plan-ops transport bootstrap

- **Status:** done
- **Priority:** high
- **Implementer:** codex
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
- **Dependencies:** []
- **Test command:** deferred to TASK-002
- **Acceptance criteria:**
  - `SKILL.md` defines an explicit "Plan-ops transport bootstrap" before readiness / Phase 0 work.
  - The bootstrap requires ToolSearch for `plan_ops__preflight` and `plan_ops__gates`.
  - If tools are visible, the active transport is MCP.
  - If tools are hidden, the orchestrator must run `claude mcp list`.
  - If `plugin:plan-executor:plan-ops` is connected, the active transport may become `cli-fallback`.
  - If the server is not connected, the run halts with `mcp_unavailable`.
  - The CLI fallback is limited to `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" <subcommand> ... --json`.
  - The text explicitly forbids inline Python, ad hoc parsing, schema bypasses, and shelling around plan operations.

### TASK-002: Pin the transport bootstrap with a regression test

- **Status:** done
- **Priority:** high
- **Implementer:** codex
- **Files:**
  - `tests/scripts/test_implement_plan_transport_bootstrap.py`
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_transport_bootstrap.py`
- **Acceptance criteria:**
  - The test fails if the skill omits the bootstrap section.
  - The test requires the exact concepts that make the fallback safe: ToolSearch, `plan_ops__preflight`, `plan_ops__gates`, `claude mcp list`, `plugin:plan-executor:plan-ops`, `PLAN_OPS_TRANSPORT=mcp`, `PLAN_OPS_TRANSPORT=cli-fallback`, `plan_ops.py`, `--json`, and "Never write inline Python".
  - The test requires the rules section to reference the active plan-ops transport rather than an MCP-only contract.

## Verification

Run:

```bash
venv/bin/pytest -q tests/scripts/test_implement_plan_transport_bootstrap.py
```

Optionally run the skill drift guard:

```bash
venv/bin/pytest -q tests/scripts/test_skill_cli_reference_drift.py
```

## Reversion Guidance

Delete `tests/scripts/test_implement_plan_transport_bootstrap.py` and revert the `SKILL.md` wording to the prior MCP-only contract.
