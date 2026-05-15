# Codex + Gemini MCP Bridge for `/implement-plan`

This is the Codex-facing bridge for running the existing `/implement-plan`
workflow without Claude. It adapts `SKILL.md` to an interactive Codex session
where the `plan-ops` MCP tools are visible. It does not replace the Claude
plugin path in `SKILL.md`.

## Transport Rules

- Use visible `plan_ops__*` MCP tools directly for plan operations. Treat MCP
  schemas as the interactive source of truth.
- Before starting, confirm these MCP tools are visible in the Codex tool list:
  `plan_ops__preflight`, `plan_ops__gates`, `plan_ops__build_tasks`,
  `plan_ops__review_route`, and `plan_ops__plan_review_route`.
- If those tools are not visible, stop and report the MCP registration issue.
  The no-Claude bridge does not use the `plan_ops.py` CLI as an interactive
  fallback.
- The `plan_ops.py` CLI is only acceptable for diagnostics or wrapper
  subprocess internals. Do not reimplement plan state machines in prose or in a
  separate runner.

## Forbidden Claude Primitives

Do not use ToolSearch, `claude mcp list`, Claude `Agent`, or `/reload-plugins`.
Do not use `plan_claude_dispatch.py` or any Claude dispatch primitive in this
Codex+Gemini path.

## Wrapper Subprocesses

- Use `plan_codex_dispatch.py implement` for task implementation subprocesses.
- Use `plan_gemini_dispatch.py review` for independent code validation when
  Gemini is available.
- Use `plan_gemini_dispatch.py plan-review` for independent plan validation
  when Gemini is available.
- Parse wrapper envelopes with the existing `plan_ops__parse_*` MCP tools where
  applicable, then route with the deterministic MCP routers.

## Routing Authorities

- Phase D code-review routing goes through `plan_ops__review_route`.
  For this bridge, send `implementer: "codex"` and `reviewer: "gemini"`.
  Gemini code-review verdicts are `clean`, `minor-findings`, and
  `needs-rework`.
- `clean` and `minor-findings` route to commit.
- `needs-rework` must not dispatch Claude role-swap. The current minimum route
  is `pause_awaiting_user` so the operator can choose a Codex remediation pass
  or manual handling.
- Phase 1.5 plan-review routing goes through `plan_ops__plan_review_route`.
  For a Gemini-first no-Claude plan review, call the pre-dispatch route with
  `claude_only: false` and `reviewer: "gemini"`; dispatch
  `plan_gemini_dispatch.py plan-review` when the route returns
  `dispatch_gemini_reviewer`.

## Operator Flow

1. Read `SKILL.md` for the canonical phase structure, but apply this bridge's
   transport and dispatch substitutions.
2. Run preflight, gates, task building, schedule writes, batch selection,
   route decisions, commits, failures, locks, and run-log writes through
   `plan_ops__*` MCP tools.
3. Dispatch implementation with `plan_codex_dispatch.py implement`.
4. Dispatch validation with Gemini wrappers, then hand parsed results to
   `plan_ops__plan_review_route` or `plan_ops__review_route`.
5. Execute the returned directive exactly. If a route returns a pause, stop and
   report the pause payload rather than guessing a remediation.
