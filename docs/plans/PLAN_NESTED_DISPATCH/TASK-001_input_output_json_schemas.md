# TASK-001 — Input / output JSON schemas + envelope builder module

## Goal

Input / output JSON schemas + envelope builder module

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- Schemas validate the examples in §6 / §7; `additionalProperties: false`.
- `_claude_dispatch_envelope.py` exposes `build_ok()`, `build_denied()`, `build_timeout()`, `build_schema_invalid()`, `build_backend_error()`, `build_scope_violation()`, `build_input_invalid()`, `build_manifest_invalid()`, `build_depth_exceeded()`, `build_budget_exhausted()` matching the §7 status vocabulary.
- Constructor rejects unknown top-level keys.

## Tasks

### TASK-001: Input / output JSON schemas + envelope builder module

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json` (create)
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_output.json` (create)
  - `plugins/plan-executor/scripts/_claude_dispatch_envelope.py` (create)
  - `tests/scripts/test_claude_dispatch_envelope.py` (create)
- **Dependencies:** []
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_dispatch_envelope.py`
- **Acceptance criteria:**
  - Schemas validate the examples in §6 / §7; `additionalProperties: false`.
  - `_claude_dispatch_envelope.py` exposes `build_ok()`, `build_denied()`, `build_timeout()`, `build_schema_invalid()`, `build_backend_error()`, `build_scope_violation()`, `build_input_invalid()`, `build_manifest_invalid()`, `build_depth_exceeded()`, `build_budget_exhausted()` matching the §7 status vocabulary.
  - Constructor rejects unknown top-level keys.
- **Reversion guidance:** none

**Description:**
