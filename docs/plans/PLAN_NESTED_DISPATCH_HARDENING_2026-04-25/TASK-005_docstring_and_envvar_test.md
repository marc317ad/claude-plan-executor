# TASK-005 — Docstring consistency + env-var inheritance test

## Goal

Docstring consistency (`PLAN_EXEC_DEPTH` → `PLAN_EXEC_DISPATCH_DEPTH`) + env-var-based parent inheritance test for span log.

## Context

Run 20260425T124346 shipped v1 of `_claude_dispatch_envelope.py` and `_claude_span_log.py`. Two small but real issues survived:

1. **Stale docstring** at `plugins/plan-executor/scripts/_claude_dispatch_envelope.py:455` — the `build_depth_exceeded` docstring reads `PLAN_EXEC_DEPTH >= PLAN_EXEC_MAX_DEPTH`, but the canonical env var name used everywhere else (in `_claude_guardrails.scrub_env`, `_claude_guardrails.evaluate_preflight`, `_claude_span_log.build_span`) is `PLAN_EXEC_DISPATCH_DEPTH`. Operators reading the docstring will hunt for the wrong variable.

2. **Missing test for env-var inheritance** — the multi-hop chain test in `tests/scripts/test_claude_span_log.py` (`TestMultiHopChain::test_three_hops_produce_chain` and `test_three_hops_via_subprocesses`) passes parent state via explicit `parent_span_id` kwargs. The PRODUCTION code path (`_claude_span_log.build_span` lines 161-162) reads `PLAN_EXEC_PARENT_RUN_ID` and `PLAN_EXEC_PARENT_AGENT` from `os.environ` directly. A test must exercise that env-var inheritance path so a regression there does not silently break the audit trail.

## Verification

- `_claude_dispatch_envelope.py:455` (and any other stale `PLAN_EXEC_DEPTH` references found by grep) now reads `PLAN_EXEC_DISPATCH_DEPTH`.
- New test `TestEnvVarInheritance::test_span_captures_env_var_parent_when_no_kwarg` spawns a `multiprocessing.Process` with `PLAN_EXEC_PARENT_RUN_ID=parent-run-X` + `PLAN_EXEC_PARENT_AGENT=parent-agent-X` set in the child env, but does NOT pass these via explicit kwargs; asserts the resulting span's `parent_run_id == 'parent-run-X'` and `parent_agent == 'parent-agent-X'`.
- Existing tests continue to pass.

## Tasks

### TASK-005: Docstring consistency + env-var inheritance test

- **Status:** pending
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_envelope.py` (modify)
  - `tests/scripts/test_claude_span_log.py` (modify)
- **Dependencies:** []
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_span_log.py`
- **Acceptance criteria:**
  - Fix stale `PLAN_EXEC_DEPTH` reference in `_claude_dispatch_envelope.py` line 455 (the `build_depth_exceeded` docstring) → `PLAN_EXEC_DISPATCH_DEPTH` (the canonical name used in `_claude_guardrails.scrub_env`, `_claude_guardrails.evaluate_preflight`, `_claude_span_log.build_span`, etc.).
  - Grep `PLAN_EXEC_DEPTH` (without the `_DISPATCH_` infix) across the entire `plugins/plan-executor/scripts/` tree to verify no other stale references hide; if any are found, fix them in the same change.
  - New test in `test_claude_span_log.py` (`TestEnvVarInheritance::test_span_captures_env_var_parent_when_no_kwarg`): spawn a `multiprocessing.Process` (spawn context) with `PLAN_EXEC_PARENT_RUN_ID=parent-run-X` + `PLAN_EXEC_PARENT_AGENT=parent-agent-X` set in the child env. Inside the process, build a minimal envelope (no `parent_span_id` kwarg, no `parent_run_id` kwarg) and call `append_span(...)`. After the process exits, read `spans.jsonl` and assert the resulting span's `parent_run_id == 'parent-run-X'` and `parent_agent == 'parent-agent-X'` — proves the production env-var inheritance path works, not just the explicit-kwarg path.
  - Existing tests continue to pass.
- **Reversion guidance:** none

**Description:**

Fixes a stale docstring + adds a missing test. The docstring at `_claude_dispatch_envelope.py:455` (the `build_depth_exceeded` constructor) reads `PLAN_EXEC_DEPTH >= PLAN_EXEC_MAX_DEPTH`, but the canonical env var name used throughout the rest of the wrapper is `PLAN_EXEC_DISPATCH_DEPTH` — operators tracing the depth limit by grep would hunt for the wrong name. The grep step in the AC is defense-in-depth in case other stale references hide. The new test closes a coverage gap: the multi-hop chain tests in `test_claude_span_log.py` pass parent state via explicit kwargs, but the production code path (`build_span` lines 161-162) reads `PLAN_EXEC_PARENT_RUN_ID` and `PLAN_EXEC_PARENT_AGENT` from `os.environ`. A regression in that env-var read would silently break the audit trail without any test detecting it. The new test uses `multiprocessing.Process` (spawn context) with the env vars set in the child env and asserts the span captures them — exercising the production path end-to-end.
