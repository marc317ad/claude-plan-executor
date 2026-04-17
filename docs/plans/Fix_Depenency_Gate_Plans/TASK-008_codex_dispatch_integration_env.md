# TASK-008 — Fix or quarantine Codex dispatch integration environment failures

**Base branch:** `main`
**Source plan:** Follow-up from TASK-006 verification

---

## Goal

Resolve the remaining `tests/scripts/` failures after TASK-006. The surviving failures are all in `tests/scripts/test_plan_codex_dispatch_integration.py` and share the same root symptom: the test wrapper invokes the Codex CLI, but Codex cannot initialize a session because the environment reports a read-only filesystem.

This task is not about dependency-gating behavior. `tests/scripts/test_plan_ops.py` and `tests/scripts/test_plan_codex_dispatch_parsing.py` passed after TASK-006. Keep the TASK-006 test changes intact.

## Failing Tests

From the TASK-006 full-suite run:

- `tests/scripts/test_plan_codex_dispatch_integration.py::test_implement_creates_file_end_to_end`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_implement_siblings_preserve_each_other`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_implement_with_baseline_untracked_survives`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_preserves_run_log_jsonl`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_preserves_run_lock_json`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_preserves_schedule_json_tracked`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_preserves_schedule_json_untracked`
- `tests/scripts/test_plan_codex_dispatch_integration.py::test_timeout_and_success_interleaved_preserves_sibling`

Common observed output:

```text
WARNING: proceeding, even though we could not update PATH: Read-only file system (os error 30)
ERROR codex_core::codex: Failed to create session: Read-only file system (os error 30)
Error: thread/start: thread/start failed: error creating thread: Fatal error: Failed to initialize session: Read-only file system (os error 30)
```

The wrapper then returns `outcome: "failure"`, `codex_exit_code: 1`, and `error: "Codex produced no output file"`. The tests expect successful Codex-driven integration runs, so they fail before reaching the behavior they intend to verify.

## Guidance

Treat these as environment-dependent Codex CLI integration failures unless fresh evidence shows a production regression. The failure mode is session initialization, not parsing, scheduling, dependency handling, state isolation logic, or output validation.

Recommended direction:

1. Identify what writable home/session/cache path Codex requires during these integration tests.
2. Make the tests provide an isolated writable location for that state, preferably under `tmp_path`, without depending on the developer machine's global Codex state.
3. If the Codex CLI is genuinely unavailable or cannot be made to run in CI/sandboxed environments, mark these tests with a precise skip condition rather than letting them fail as product regressions.
4. Keep the skip narrow. Do not skip the parser, schema, state-isolation unit tests, or `test_plan_ops.py`; only skip tests that actually spawn Codex and require a live writable Codex session.
5. Preserve the intent of the integration tests where possible: they should still exercise end-to-end wrapper behavior when Codex can start successfully.

Do not solve this by weakening assertions around `outcome == "success"` after Codex fails to start. That would convert an environment failure into a false-positive integration pass.

## Verification

1. `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py`
   - Passes when Codex can initialize with the test-provided writable state, or skips only the live-Codex cases with a clear reason when it cannot.
2. `venv/bin/pytest -q tests/scripts/`
   - No TASK-006 regressions.
   - Remaining result is either fully green or has explicit, narrow skips for live-Codex integration tests.
3. Confirm the TASK-006 focused suites still pass:
   - `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
   - `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_parsing.py`

## Acceptance Criteria

- The eight listed integration tests no longer fail with `Read-only file system (os error 30)` session initialization errors.
- If skipped, each skip clearly identifies that the live Codex CLI cannot initialize in the current environment.
- No broad skip marker disables unrelated dispatch parsing, schema, state-isolation, or plan-ops tests.
- No assertions are weakened to accept `Codex produced no output file` as success.
- TASK-006 behavior remains intact: exact-selection `filter-schedule`, isolated failures, orphan `dependencies` tolerance, and priority defaulting continue to be covered.

## Reversion Guidance

Revert only the TASK-008 changes if the live-Codex integration strategy is wrong. Do not revert TASK-006 test updates as part of this task; those align the suite with the stripped dependency-gating semantics.
