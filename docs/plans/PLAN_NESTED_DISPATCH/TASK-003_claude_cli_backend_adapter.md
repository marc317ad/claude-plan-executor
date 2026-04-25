# TASK-003 — claude-cli backend adapter

## Goal

claude-cli backend adapter

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- `invoke(manifest, effective, payload, trace)` returns an envelope.
- Argv built per §8.1; `Agent` always in `--disallowedTools`; `--add-dir` from effective cwd; `--permission-mode acceptEdits`.
- Captures stdout, parses JSON, maps `result` / `duration_ms` / `total_cost_usd` / `session_id` / `usage` into envelope.
- `TimeoutExpired` → `status: timeout`; non-JSON stdout → `status: backend_error, code: malformed_output`.
- `--backend-binary` test seam (tiny shim script); real-CLI path gated on `PLAN_EXEC_E2E=1`.

## Tasks

### TASK-003: claude-cli backend adapter

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_backend.py` (create)
  - `tests/scripts/test_claude_backend.py` (create)
- **Dependencies:** [001, 002]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_backend.py`
- **Acceptance criteria:**
  - `invoke(manifest, effective, payload, trace)` returns an envelope.
  - Argv built per §8.1; `Agent` always in `--disallowedTools`; `--add-dir` from effective cwd; `--permission-mode acceptEdits`.
  - Captures stdout, parses JSON, maps `result` / `duration_ms` / `total_cost_usd` / `session_id` / `usage` into envelope.
  - `TimeoutExpired` → `status: timeout`; non-JSON stdout → `status: backend_error, code: malformed_output`.
  - `--backend-binary` test seam (tiny shim script); real-CLI path gated on `PLAN_EXEC_E2E=1`.
- **Reversion guidance:** none

**Description:**
