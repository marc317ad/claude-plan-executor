# TASK-009 — README + security doc

## Goal

README + security doc

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- `README_claude_dispatch.md` documents: escape-hatch use case; input / output shape; every `PLAN_EXEC_*` env var; the refusal matrix (§9.2); inner CLI shares billing with parent; the v1 dispatchable-agents set; the outcome of Probe 2b (Pass A or B) and what it means for safety invariants.
- Quickstart example: ≤10-line JSON payload for `plan-implementer`.
- Top-level `README.md` gains a one-paragraph pointer to the new wrapper.

## Tasks

### TASK-009: README + security doc

- **Status:** complete
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/README_claude_dispatch.md` (create)
  - `README.md` (modify)
- **Dependencies:** [005, 007]
- **Test command:** none (documentation-only task; behavior validated by sibling `TASK-008` E2E suite)
- **Acceptance criteria:**
  - `README_claude_dispatch.md` documents: escape-hatch use case; input / output shape; every `PLAN_EXEC_*` env var; the refusal matrix (§9.2); inner CLI shares billing with parent; the v1 dispatchable-agents set; the outcome of Probe 2b (Pass A or B) and what it means for safety invariants.
  - Quickstart example: ≤10-line JSON payload for `plan-implementer`.
  - Top-level `README.md` gains a one-paragraph pointer to the new wrapper.
- **Reversion guidance:** none

**Description:**

Documents the nested Claude dispatch wrapper for callers and operators. `README_claude_dispatch.md` covers the escape-hatch use case (why a subagent with only Bash needs to shell out to `claude -p`), the input / output JSON shapes from TASK-001, every `PLAN_EXEC_*` env var, the §9.2 refusal matrix from TASK-002, the billing model (the inner CLI shares billing with the parent session), the v1 dispatchable-agents allowlist (`{plan-analyst, plan-implementer, plan-remediator}`), and the outcome of Probe 2b (Pass A or Pass B per TASK-007) with what it implies for the safety story. Includes a ≤10-line JSON quickstart payload for `plan-implementer`. The top-level `README.md` gains a one-paragraph pointer to the new wrapper so readers discover it from the project root.
