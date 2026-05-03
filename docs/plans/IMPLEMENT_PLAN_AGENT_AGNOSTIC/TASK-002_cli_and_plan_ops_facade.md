# TASK-002: CLI Skeleton And PlanOps Facade

## Goal

Add the executable `implement_plan.py` CLI and a narrow `PlanOpsFacade` that calls existing `plan_ops.py` pure cores or CLI-equivalent functions without duplicating their logic.

## Context

The runner must be scriptable by any agent that can invoke shell commands. It should not require MCP access, but it must preserve parity with the MCP contracts by using the same `plan_ops` entry points. When an interactive Claude or Codex session has `plan_ops__*` tools available, MCP remains the preferred transport; this CLI runner is for portability, automation, and sessions where MCP is unavailable.

## Scoped Context

This task stops at deterministic preflight/dry-run plumbing. It must not dispatch providers.

### TASK-002: CLI Skeleton And PlanOps Facade

- **Status:** pending
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_runner_cli.py` (new)
  - `tests/scripts/fixtures/implement_plan_runner/` (new fixture directory as needed)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_cli.py`
- **Description:** Add the script entry point and a narrow `PlanOpsFacade` so the runner can call existing plan operations without reimplementing MCP or `plan_ops.py` behavior.
- **Acceptance criteria:**
  - CLI supports both `implement_plan.py <plan>` and `implement_plan.py run --plan <plan>` forms.
  - CLI supports `--config`, `--dry-run`, `--parallel`, `--provider-preference`, `--assign`, `--reviewer`, `--plan-reviewer`, `--allow-provider-fallback`, `--skip-cross-review`, `--skip-plan-review`, `--task-ids`, and `--unattended-revert-policy`.
  - `PlanOpsFacade` exposes methods for `path_info`, `preflight`, `decompose_plan`, `check_plan_deps`, `gates`, `acquire_lock`, `release_lock`, `build_tasks`, `write_schedule`, `batch_next`, `review_route`, `plan_review_route`, `log_event`, `commit_task`, `fail_task`, `block_dependents`, `reconcile_batch`, `update_plan_header`, `finalize_execution_log`, `parse_implementer_report`, `parse_plan_review_report`, `parse_plan_review_triage_report`, `parse_d5_adjudication`, `claude_envelope_extract`, `order_triage_findings`, `resolve_read_targets`, `build_claude_dispatch_input`, `build_codex_dispatch_input`, and `build_gemini_dispatch_input`.
  - Facade methods call `plan_ops._run_*` functions when safe and available; fallback to subprocess CLI is allowed only behind one helper with JSON parsing and timeout handling.
  - `--dry-run --stop-after preflight` or equivalent test seam runs through path resolution and preflight against fixtures without provider dispatch.
  - Facade tests include at least one subprocess fallback case for a stdin-driven route command and one direct pure-core call for a builder command, matching the current mixed `plan_ops.py` surfaces.
  - CLI emits a final JSON summary with `status`, `run_id`, `plan_path`, `dry_run`, `completed_phase`, `warnings`, and `errors`.
  - Invalid flag combinations fail before lock acquisition.
  - Tests cover config-file merge precedence: defaults < config < CLI flags.

**Description:**
Add the script entry point and a narrow `PlanOpsFacade` so the runner can call
existing plan operations without reimplementing MCP or `plan_ops.py` behavior.

## Verification

Run the task test command. Also run `venv/bin/python plugins/plan-executor/scripts/implement_plan.py --help`.

## Non-goals

- Provider capability probing.
- Task assignment policy.
- Batch execution.
- Replacing MCP or changing the preferred transport for interactive agents that already have `plan_ops__*` tools installed.
