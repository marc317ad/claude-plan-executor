# TASK-006 — Span log (`spans.jsonl`)

## Goal

Span log (`spans.jsonl`)

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- `append_span(log_dir, envelope)` atomically writes one JSON line to `$log_dir/spans.jsonl`.
- Default log dir: `docs/plans/` (sibling of `_run_log.jsonl`).
- Multi-hop test (wrapper calling wrapper) produces N spans with correct parent links.

## Tasks

### TASK-006: Span log (`spans.jsonl`)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/_claude_span_log.py` (create)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (modify — add `append_span` call)
  - `tests/scripts/test_claude_span_log.py` (create)
- **Dependencies:** [005]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_span_log.py`
- **Acceptance criteria:**
  - `append_span(log_dir, envelope)` atomically writes one JSON line to `$log_dir/spans.jsonl`.
  - Default log dir: `docs/plans/` (sibling of `_run_log.jsonl`).
  - Multi-hop test (wrapper calling wrapper) produces N spans with correct parent links.
- **Reversion guidance:** none

**Description:**
