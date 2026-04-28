# TASK-001 — `apply_cleanup` policy gate in `_claude_dispatch_cleanup.py`

## Goal

`apply_cleanup` policy gate in `_claude_dispatch_cleanup.py`

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- `apply_cleanup` signature gains a keyword-only argument `unattended_revert_policy: Optional[str] = None`. When `None`, the function behaves as `pause` (non-destructive). When the value is not in the closed set `{"pause", "fail-fast", "preserve-only"}` the function raises `ValueError` with a message naming the unknown value (parallel to the existing `authorization_source` validation at line 662).
- When `unattended_revert_policy in {"pause", "fail-fast", None}`: the function MUST detect the out-of-scope set exactly as today (same `observed_delta` math, same protected-skip handling, same misreport logic, same `out_of_scope_paths` population) but MUST NOT call `_restore_path` on any out-of-scope path. `restored=[]`, `deleted=[]`, `failed_paths=[]`. `scope_violation_detected` is computed from `out_of_scope_paths` (unchanged). `cleanup_strategy` is `"detect_only_revert_policy_pause"` when policy is `pause` (and `None`-default), `"detect_only_revert_policy_fail_fast"` when `fail-fast`.
- When `unattended_revert_policy == "preserve-only"`: the function behaves identically to today (existing `_restore_path` loop + `restored`/`deleted`/`failed_paths` population; `cleanup_strategy="delta_bounded"`).
- The return-dict shape grows by ZERO keys; only the values of `restored` / `deleted` / `failed_paths` / `cleanup_strategy` differ across policies. Downstream consumers (`plan_claude_dispatch.py:771–781`, the wrapper-events emission) require no changes.
- Module docstring at `_claude_dispatch_cleanup.py:646–652` is rewritten to reflect the new contract: under `pause`/`fail-fast`, two concurrent callers' writes are NOT mutated; the orchestrator's reconcile-batch is authoritative for cross-task scope partitioning.
- Tests:

## Tasks

### TASK-001: `apply_cleanup` policy gate in `_claude_dispatch_cleanup.py`

- **Status:** done
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` (add keyword-only `unattended_revert_policy` arg to `apply_cleanup`; add the gate; update return-shape docs and `cleanup_strategy` enum)
  - `tests/scripts/test_claude_dispatch_cleanup.py` (5 new tests: detect-only under `pause`, detect-only under `fail-fast`, salvage-then-revert under `preserve-only`, default-None routes to `pause` semantics, unknown-policy raises `ValueError`)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_cleanup.py`
- **Acceptance criteria:**
  - `apply_cleanup` signature gains a keyword-only argument `unattended_revert_policy: Optional[str] = None`. When `None`, the function behaves as `pause` (non-destructive). When the value is not in the closed set `{"pause", "fail-fast", "preserve-only"}` the function raises `ValueError` with a message naming the unknown value (parallel to the existing `authorization_source` validation at line 662).
  - When `unattended_revert_policy in {"pause", "fail-fast", None}`: the function MUST detect the out-of-scope set exactly as today (same `observed_delta` math, same protected-skip handling, same misreport logic, same `out_of_scope_paths` population) but MUST NOT call `_restore_path` on any out-of-scope path. `restored=[]`, `deleted=[]`, `failed_paths=[]`. `scope_violation_detected` is computed from `out_of_scope_paths` (unchanged). `cleanup_strategy` is `"detect_only_revert_policy_pause"` when policy is `pause` (and `None`-default), `"detect_only_revert_policy_fail_fast"` when `fail-fast`.
  - When `unattended_revert_policy == "preserve-only"`: the function behaves identically to today (existing `_restore_path` loop + `restored`/`deleted`/`failed_paths` population; `cleanup_strategy="delta_bounded"`).
  - The return-dict shape grows by ZERO keys; only the values of `restored` / `deleted` / `failed_paths` / `cleanup_strategy` differ across policies. Downstream consumers (`plan_claude_dispatch.py:771–781`, the wrapper-events emission) require no changes.
  - Module docstring at `_claude_dispatch_cleanup.py:646–652` is rewritten to reflect the new contract: under `pause`/`fail-fast`, two concurrent callers' writes are NOT mutated; the orchestrator's reconcile-batch is authoritative for cross-task scope partitioning.
  - Five new tests land in `tests/scripts/test_claude_dispatch_cleanup.py`:
    - `test_apply_cleanup_pause_policy_detects_but_does_not_revert` — write a file outside declared scope, call `apply_cleanup(..., unattended_revert_policy="pause")`, assert `out_of_scope_paths == [path]`, `restored == []`, `deleted == []`, file content on disk is unchanged from the test-induced write, `cleanup_strategy == "detect_only_revert_policy_pause"`.
    - `test_apply_cleanup_fail_fast_policy_detects_but_does_not_revert` — same setup as the `pause` test but with `fail-fast`; assert `cleanup_strategy == "detect_only_revert_policy_fail_fast"`.
    - `test_apply_cleanup_preserve_only_policy_unchanged` — `unattended_revert_policy="preserve-only"`; assert behavior identical to today's no-policy-arg call (file reverted, `restored` non-empty, `cleanup_strategy == "delta_bounded"`).
    - `test_apply_cleanup_default_policy_is_pause` — call without the policy kwarg; assert non-destructive behavior matching the `pause` test. Documents the safer-by-default contract.
    - `test_apply_cleanup_unknown_policy_raises_valueerror` — `unattended_revert_policy="yolo"`; assert `ValueError` whose message contains the literal `"yolo"` and the closed enum.
- **Reversion guidance:** none

**Description:**

Closes the destructive-action half of the wrapper-revert-policy bypass. `apply_cleanup`'s job is to revert any post-dispatch write that lies outside the local task's `declared_files_changed`, and the existing implementation does that unconditionally for write-authorized agents — which is what destroyed TASK-008's correct work in run `20260428T121041`. This task adds a single keyword-only argument (`unattended_revert_policy`) to the function and gates the destructive branch on its value. Under `pause` (and the `None` default) and `fail-fast`, the function performs all the same delta classification it does today (computing `out_of_scope_paths`, populating `scope_violation_detected`, detecting `misreported_paths`) but skips every call to `_restore_path`, leaving the working tree exactly as the inner agent left it. Under `preserve-only` the existing salvage-then-revert behavior is preserved unchanged — operators who explicitly opt into destruction get the documented behavior. Because the return-dict shape is unchanged, downstream consumers in `plan_claude_dispatch.py:771–781` (the `wrapper_events` emission) and the envelope builder need no edits — they already key off `restored`/`deleted`/`failed_paths` and `scope_violation_detected`, which all behave correctly under the new branches.

**Implementation notes.** The gate is roughly 8 lines: one `Optional[str]` arg with `None` default, one closed-enum validation block (parallel to the existing `authorization_source` validation at line 662), one branch around the `_restore_path` loop, two new `cleanup_strategy` enum values. The bulk of the diff is in tests — five new tests in `test_claude_dispatch_cleanup.py` covering each policy + the default + the unknown-value error. Existing tests must continue to pass; the safer-by-default contract means tests that don't pass `unattended_revert_policy` get the new non-destructive behavior, so any test that asserts on `restored`/`deleted` content has to be updated to either pass `unattended_revert_policy="preserve-only"` (to keep its assertion) or reword the assertion to match the new default.

Test-fixture update strategy: prefer extending the existing `_call_cleanup` helper (or whatever the suite uses) over per-test rewrites. Add a single keyword-arg passthrough so existing call-sites can opt into `preserve-only` without touching their assertion lines.
