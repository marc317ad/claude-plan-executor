# TASK-005 — CLI entrypoint `plan_claude_dispatch.py`

## Goal

CLI entrypoint `plan_claude_dispatch.py`

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- Subcommands per §10.
- `run` wires: preflight → baseline → backend dispatch → cleanup → schema-validate inner result → envelope emit → span append.
- Stdin/stdout `-` syntax works for `--input` and `--output`.
- Exit codes per §10.
- `--dry-run` returns a plan-only envelope with `status: ok, dry_run: true` and no spawn.
- Malformed input → exit 2 + `status: input_invalid` envelope on stdout.

## Tasks

### TASK-005: CLI entrypoint `plan_claude_dispatch.py`

- **Status:** complete
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (create)
  - `tests/scripts/test_plan_claude_dispatch_cli.py` (create)
- **Dependencies:** [002, 003, 004]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_claude_dispatch_cli.py`
- **Acceptance criteria:**
  - Subcommands per §10.
  - `run` wires: preflight → baseline → backend dispatch → cleanup → schema-validate inner result → envelope emit → span append.
  - Stdin/stdout `-` syntax works for `--input` and `--output`.
  - Exit codes per §10.
  - `--dry-run` returns a plan-only envelope with `status: ok, dry_run: true` and no spawn.
  - Malformed input → exit 2 + `status: input_invalid` envelope on stdout.
- **Reversion guidance:** none

**Description:**

Implements the `plan_claude_dispatch.py` CLI entrypoint that ties together the schemas (TASK-001), manifest/guardrails (TASK-002), backend adapter (TASK-003), and cleanup (TASK-004). Subcommands per §10. The `run` subcommand wires the full pipeline: preflight → baseline snapshot → backend dispatch → delta-bounded cleanup → schema-validate the inner result → emit the wrapper envelope → append a span entry. Supports `--input -` / `--output -` for stdin/stdout I/O, `--dry-run` (returns a plan-only envelope with `status: ok, dry_run: true` and no spawn), and the §10 exit-code contract. Malformed input fails fast: exit 2 plus a `status: input_invalid` envelope on stdout so callers can distinguish wrapper-level rejection from backend failure.
