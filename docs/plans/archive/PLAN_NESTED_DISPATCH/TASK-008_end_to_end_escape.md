# TASK-008 — End-to-end escape-hatch integration test

## Goal

End-to-end escape-hatch integration test

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- Skipped by default; runs when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
- Case 1: `agent: plan-analyst` against a stub plan → `status: ok`, valid inner result, a `spans.jsonl` entry.
- Case 2: malformed input → exit 2 + `status: input_invalid`.
- Case 3: two parallel wrapper invocations on disjoint file sets succeed; baselines do not cross-contaminate.

## Tasks

### TASK-008: End-to-end escape-hatch integration test

- **Status:** complete
- **Priority:** medium
- **Files:**
  - `tests/scripts/test_claude_dispatch_e2e.py` (create)
  - `tests/fixtures/claude_dispatch/minimal_payload.json` (create)
- **Dependencies:** [005, 007]
- **Test command:** `PLAN_EXEC_E2E=1 venv/bin/python -m pytest tests/scripts/test_claude_dispatch_e2e.py`
- **Acceptance criteria:**
  - Skipped by default; runs when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
  - Case 1: `agent: plan-analyst` against a stub plan → `status: ok`, valid inner result, a `spans.jsonl` entry.
  - Case 2: malformed input → exit 2 + `status: input_invalid`.
  - Case 3: two parallel wrapper invocations on disjoint file sets succeed; baselines do not cross-contaminate.
- **Reversion guidance:** none

**Description:**

End-to-end integration test that exercises the wrapper against a real `claude` binary, validating the escape hatch end to end. Case 1: dispatch `agent: plan-analyst` against a stub plan and assert `status: ok`, valid inner result, and a `spans.jsonl` entry. Case 2: malformed input → exit 2 + `status: input_invalid` (covers TASK-005 fast-fail path). Case 3: two parallel wrapper invocations on disjoint file sets succeed concurrently with no baseline cross-contamination (TASK-004 invariant). Skipped by default; runs when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
