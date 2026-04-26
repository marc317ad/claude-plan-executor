# TASK-001 — Verify unified path normalizer covers the post-mortem's TASK-027B scope-rejection scenario

## Goal

Confirm that the wrapper-side `out_of_scope_observed: true` false positive that bit TASK-027B during run 20260425T041800 is actually closed by the unified path normalizer that CODEX_FRICTION_2026-04-25 TASK-002 lands. Add a targeted regression test that pins the post-mortem's specific failure shape — backtick-wrapped declared file (`` `plugins/plan-executor/agents/plan-implementer.md` ``) compared against the wrapper's bare-path observed-writes set — so a future regression in the unified helper is caught at this concrete bullet shape, not just at the parametric forms TASK-002 already covers.

This task does NOT re-implement the backtick fix. CODEX_FRICTION TASK-002 unifies `plan_ops._normalize_files_entry` and `plan_codex_dispatch.normalize_file_path` into a single shared `_plan_paths.normalize_files_entry` and lands its own parameterized parsing tests. This task verifies that the unification, applied at the post-write scope-check site (`validate_scope` / `_handle_timeout_cleanup`), eliminates the post-mortem's specific false positive.

## Context

**The bug (post-mortem Issue 1).** During run 20260425T041800, TASK-027B's first Codex `implement` dispatch wrote correctly to its two declared files but the wrapper reported failure with `out_of_scope_observed: true`. Cause: `plan_codex_dispatch.normalize_file_path` did not strip surrounding backticks; the schedule's `tasks[].files[]` entries carry markdown backticks; the post-write scope check then compared backtick-wrapped allowed paths against backtick-free observed writes.

**The CODEX_FRICTION TASK-002 fix.** TASK-002 in `docs/plans/CODEX_FRICTION_2026-04-25/` unifies the two normalizers (`_normalize_files_entry` and `normalize_file_path`) into a single shared `_plan_paths.normalize_files_entry`. Its tests cover several backticked + prose forms (`Makefile` em-dash bullet etc.) parameterically.

**Why a separate task.** TASK-002's test surface is parameterized over Files-block parsing forms; this task verifies the END-TO-END scope check (the consumer of normalization, not the helper itself) doesn't regress at the post-mortem's specific bullet shape. The two tests are complementary: TASK-002 pins the helper output; this task pins the integration result.

**Sequencing.** This task assumes CODEX_FRICTION TASK-002 has merged. If TASK-002 is still pending when this task is dispatched, the implementer is expected to (a) confirm TASK-002 has merged, OR (b) fail with a structured `dependency_not_merged: CODEX_FRICTION TASK-002` report and stop. No re-implementation of TASK-002's helper is permitted in this task.

## Verification

- A regression test exercises the wrapper's `validate_scope` (or `_handle_timeout_cleanup` post-write classifier) end-to-end with:
  - Allowed files set declared as `` ["`plugins/plan-executor/agents/plan-implementer.md`", "`plugins/plan-executor/skills/implement-plan/SKILL.md`"] `` (backtick-wrapped, matching the schedule's `tasks[].files[]` shape).
  - Observed writes: `["plugins/plan-executor/agents/plan-implementer.md", "plugins/plan-executor/skills/implement-plan/SKILL.md"]` (bare paths).
  - Expected result: `out_of_scope_observed: false`, no entries in the out-of-scope sets.
- The same test, with one observed write swapped to a NON-allowed bare path, yields `out_of_scope_observed: true` with the unallowed path in the appropriate out-of-scope set. (Negative-control assertion ensuring the test isn't trivially true.)
- The test imports `validate_scope` (or its successor under TASK-002) directly — no subprocess — and runs in <1s.
- The test file references the post-mortem (`docs/analysis/2026-04-25_run_20260425T041800_postmortem.md` Issue 1) and the CODEX_FRICTION TASK-002 dependency in its docstring, so a future operator reading the test sees the lineage.
- All existing wrapper tests stay green. The unified helper from TASK-002 is unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py -k "scope_check or out_of_scope_observed"` returns 0.

## Tasks

### TASK-001: Verify unified normalizer eliminates TASK-027B false positive

- **Status:** done
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_codex_dispatch.py` (regression test only — no production code touched)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py -k "scope_check or out_of_scope_observed"`
- **Read targets:**
  - `docs/analysis/2026-04-25_run_20260425T041800_postmortem.md:28-41` — Issue 1 description
  - `docs/plans/CODEX_FRICTION_2026-04-25/TASK-002_files_block_parser_unification.md` — full file
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::validate_scope`
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::_handle_timeout_cleanup`
- **Acceptance criteria:**
  - **Pre-flight check:** the implementer's report names the merge SHA (or commit short-hash) of CODEX_FRICTION TASK-002 and confirms the unified `_plan_paths.normalize_files_entry` helper is in place. If TASK-002 has NOT merged, the implementer fails with a structured `dependency_not_merged` report and does not proceed.
  - New test `test_validate_scope_accepts_backticked_declared_files_with_bare_observed_writes` (or similarly named) that exercises the wrapper's post-write scope check against the post-mortem's exact bullet shape (backtick-wrapped declared, bare-path observed) and asserts `out_of_scope_observed: false`.
  - Negative control: `test_validate_scope_rejects_unallowed_bare_path_under_backticked_declarations` confirms the test isn't trivially true — an unallowed bare-path write yields `out_of_scope_observed: true`.
  - Both tests' docstrings cite the post-mortem and the CODEX_FRICTION TASK-002 dependency.
  - No production code is modified by this task. The implementer's report explicitly states "no production code touched" if the unified helper already passes the test.
  - If the test FAILS against the merged TASK-002 helper (i.e., TASK-002's unification didn't cover the integration site), the implementer escalates with a `coverage_gap` finding rather than fixing it under this task — the fix belongs in a TASK-002 follow-up.
- **Reversion guidance:** revert the test additions; no production code paths are touched. The test is pure pin-coverage.

**Description:**
Pin the post-mortem's specific TASK-027B scope-rejection scenario as a regression test against the unified path normalizer that CODEX_FRICTION TASK-002 lands. The test exercises the integration site (`validate_scope` / `_handle_timeout_cleanup`), not just the helper output, complementing TASK-002's parameterized parsing tests.

**Implementation notes:**
- Resist the urge to re-fix `normalize_file_path` if the test fails. The fix path is CODEX_FRICTION TASK-002 (or a follow-up to it). This task's lane is verification.
- The negative-control assertion is load-bearing — without it the test could pass tautologically if `validate_scope` returns `false` unconditionally.
- Keep the test small and direct. One success case + one negative control is sufficient; broader matrix coverage is TASK-002's lane.

## Execution log — 20260426T032452 (success)

Starting SHA: `4220c0aa16b60579f826d4d8a8ed3150cc7d39a4`  → Ending SHA: `5d5a0637e945998fe546965c0f7764ebf3b53b71`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude (codex fallback) | codex | ship-with-fixes [disagreement] | 8f25b174 | D.5 dismissed Codex finding — wrapper.normalize_file_path is the unified _plan_paths.normalize_files_entry alias. |
