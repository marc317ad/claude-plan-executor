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

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` (add keyword-only `unattended_revert_policy` arg to `apply_cleanup`; add the gate; update return-shape docs and `cleanup_strategy` enum)
  - `tests/scripts/test_claude_dispatch_cleanup.py` (3 new tests: detect-only under `pause`, detect-only under `fail-fast`, salvage-then-revert under `preserve-only` continues to behave as today)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_cleanup.py`
- **Acceptance criteria:**
  - `apply_cleanup` signature gains a keyword-only argument `unattended_revert_policy: Optional[str] = None`. When `None`, the function behaves as `pause` (non-destructive). When the value is not in the closed set `{"pause", "fail-fast", "preserve-only"}` the function raises `ValueError` with a message naming the unknown value (parallel to the existing `authorization_source` validation at line 662).
  - When `unattended_revert_policy in {"pause", "fail-fast", None}`: the function MUST detect the out-of-scope set exactly as today (same `observed_delta` math, same protected-skip handling, same misreport logic, same `out_of_scope_paths` population) but MUST NOT call `_restore_path` on any out-of-scope path. `restored=[]`, `deleted=[]`, `failed_paths=[]`. `scope_violation_detected` is computed from `out_of_scope_paths` (unchanged). `cleanup_strategy` is `"detect_only_revert_policy_pause"` when policy is `pause` (and `None`-default), `"detect_only_revert_policy_fail_fast"` when `fail-fast`.
  - When `unattended_revert_policy == "preserve-only"`: the function behaves identically to today (existing `_restore_path` loop + `restored`/`deleted`/`failed_paths` population; `cleanup_strategy="delta_bounded"`).
  - The return-dict shape grows by ZERO keys; only the values of `restored` / `deleted` / `failed_paths` / `cleanup_strategy` differ across policies. Downstream consumers (`plan_claude_dispatch.py:771–781`, the wrapper-events emission) require no changes.
  - Module docstring at `_claude_dispatch_cleanup.py:646–652` is rewritten to reflect the new contract: under `pause`/`fail-fast`, two concurrent callers' writes are NOT mutated; the orchestrator's reconcile-batch is authoritative for cross-task scope partitioning.
  - Tests:
- **Reversion guidance:** none

**Description:**
