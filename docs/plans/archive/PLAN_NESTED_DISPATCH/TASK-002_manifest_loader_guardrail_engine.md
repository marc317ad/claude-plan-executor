# TASK-002 — Manifest loader + guardrail engine

## Goal

Manifest loader + guardrail engine

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- `load_agent(name)` reads existing `plugins/plan-executor/agents/<name>.md` frontmatter, returns dict with `tools` (list[str]), `model` (str), `description` (str).
- Rejects names outside `{plan-analyst, plan-implementer, plan-remediator}` with `AgentNotDispatchable`.
- `evaluate_preflight(manifest, input, env)` returns `(allow: bool, envelope_if_denied: dict | None)` covering every row in §9.2.
- `scrub_env(manifest, parent_env)` keeps `env_allowlist + PLAN_EXEC_*`, injects per-hop vars.

## Tasks

### TASK-002: Manifest loader + guardrail engine

- **Status:** complete
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_agent_manifest.py` (create)
  - `plugins/plan-executor/scripts/_claude_guardrails.py` (create)
  - `tests/scripts/test_claude_agent_manifest.py` (create)
  - `tests/scripts/test_claude_guardrails.py` (create)
- **Dependencies:** [001]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_agent_manifest.py tests/scripts/test_claude_guardrails.py`
- **Acceptance criteria:**
  - `load_agent(name)` reads existing `plugins/plan-executor/agents/<name>.md` frontmatter, returns dict with `tools` (list[str]), `model` (str), `description` (str).
  - Rejects names outside `{plan-analyst, plan-implementer, plan-remediator}` with `AgentNotDispatchable`.
  - `evaluate_preflight(manifest, input, env)` returns `(allow: bool, envelope_if_denied: dict | None)` covering every row in §9.2.
  - `scrub_env(manifest, parent_env)` keeps `env_allowlist + PLAN_EXEC_*`, injects per-hop vars.
- **Reversion guidance:** none

**Description:**

Implements the agent-manifest loader and the preflight guardrail engine for nested Claude dispatch. `_claude_agent_manifest.load_agent(name)` reads `plugins/plan-executor/agents/<name>.md` frontmatter and returns the `{tools, model, description}` dict; only `{plan-analyst, plan-implementer, plan-remediator}` are dispatchable in v1, others raise `AgentNotDispatchable`. `_claude_guardrails.evaluate_preflight(manifest, input, env)` walks the §9.2 refusal matrix (depth limit, budget, allowlist, etc.) and returns `(allow, envelope_if_denied)`. `scrub_env(manifest, parent_env)` filters the parent env down to `env_allowlist + PLAN_EXEC_*` and injects per-hop bookkeeping vars so child sessions cannot escape the sandbox via inherited state.
