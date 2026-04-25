# TASK-007 — Probe 2b — `acceptEdits` allowlist enforcement (build gate)

## Goal

Probe 2b — `acceptEdits` allowlist enforcement (build gate)

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- Test is skipped by default; runs only when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
- Invokes `claude -p --agent plan-executor:plan-analyst --permission-mode acceptEdits --allowedTools Read,Grep,Glob,Bash --output-format json` with a prompt to `Write` a file in a tmp dir.
- **Pass case A:** `permission_denials` non-empty AND no file on disk → proceed with `acceptEdits` as default permission mode.
- **Pass case B:** file created but delta-bounded cleanup reverts it → proceed; README elevates cleanup as the primary safety mechanism and notes allowlist is advisory only.
- **Fail case:** file created and not reverted → ship-blocker; revise v3 to use strict cwd sandbox + cleanup as sole mechanism.

## Tasks

### TASK-007: Probe 2b — `acceptEdits` allowlist enforcement (build gate)

- **Status:** complete
- **Priority:** high
- **Files:**
  - `tests/scripts/test_claude_permission_mode_probe.py` (create)
- **Dependencies:** []
- **Test command:** `PLAN_EXEC_E2E=1 venv/bin/python -m pytest tests/scripts/test_claude_permission_mode_probe.py`
- **Acceptance criteria:**
  - Test is skipped by default; runs only when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
  - Invokes `claude -p --agent plan-executor:plan-analyst --permission-mode acceptEdits --allowedTools Read,Grep,Glob,Bash --output-format json` with a prompt to `Write` a file in a tmp dir.
  - **Pass case A:** `permission_denials` non-empty AND no file on disk → proceed with `acceptEdits` as default permission mode.
  - **Pass case B:** file created but delta-bounded cleanup reverts it → proceed; README elevates cleanup as the primary safety mechanism and notes allowlist is advisory only.
  - **Fail case:** file created and not reverted → ship-blocker; revise v3 to use strict cwd sandbox + cleanup as sole mechanism.
- **Reversion guidance:** none

**Description:**

Build-gate probe that empirically determines whether `--permission-mode acceptEdits` plus `--allowedTools` constitutes a real safety boundary, or only an advisory hint to be backstopped by delta-bounded cleanup. Invokes `claude -p --agent plan-executor:plan-analyst --permission-mode acceptEdits --allowedTools Read,Grep,Glob,Bash --output-format json` with a prompt to `Write` a file in a tmp dir (Write is NOT in the allowlist). Pass case A: nested session refuses (`permission_denials` non-empty, no file on disk) → README ships acceptEdits as the default. Pass case B: file created but TASK-004 cleanup reverts it → README ships cleanup as primary mechanism, allowlist as advisory. Fail case: file created and not reverted → ship-blocker; v3 must redesign with strict cwd sandbox + cleanup as sole mechanism. Gated on `PLAN_EXEC_E2E=1` AND `claude` on PATH so CI and local dev runs skip cleanly.
