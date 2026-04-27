# TASK-004 — Schema-fail short-circuit + `error.code: wrapper_autoclean_blocked` in `plan_claude_dispatch.py:cmd_run`

## Goal

Schema-fail short-circuit + `error.code: wrapper_autoclean_blocked` in `plan_claude_dispatch.py:cmd_run`

## Context

The `prohibit_silent_revert` plan (committed `8bb0b7b`) hardened the orchestrator surface against silent destruction of completed work. Its TASK-009 then ran via the bash-dispatched plan-implementer wrapper and **the wrapper destroyed 4 of the 5 implementer-written files**. After TASK-001 (Layer A: orchestrator populates `declared_files_changed`) and TASK-002 (Layer B: wrapper requires explicit authorization), the most likely remaining failure mode is a **Layer-A regression OR an in-flight dispatch from before TASK-001 lands** — the orchestrator submits a wrapper input with empty `declared_files_changed` for a write-authorized agent (`plan-implementer | plan-remediator`).

TASK-002 added an anti-aliasing guard so the wrapper does NOT silently fall back to `wrapper-empty-scope-readonly` in that case. This task implements the actual recovery path: the wrapper detects the situation pre-cleanup and surfaces a structured envelope with `status: scope_violation` and `error.code: wrapper_autoclean_blocked`, while preserving every observed delta in the working tree. The orchestrator's resume protocol (TASK-006) routes that envelope through the existing **Awaiting-user pause** subroutine.

The wrapper's status enum (`schemas/claude_dispatch_output.json:11`) is closed and any addition would require coordinated edits across the schema, `_claude_dispatch_envelope.STATUS_VOCABULARY`, and SKILL.md routing. Codex's recommendation: **don't add a new status value**; instead reuse `status: "scope_violation"` and distinguish the new failure mode via `error.code: "wrapper_autoclean_blocked"`. Relax `build_scope_violation()` to accept an `error_code` parameter (default preserves existing behavior). The orchestrator's routing ladder reads `error.code` to distinguish the new "preserve everything pending user" path from the existing "wrapper reverted out-of-scope writes" path.

### Decisions folded in

1. **Reuse `status: scope_violation`; distinguish via `error.code`.** Closed status enum stays closed. The semantic distinction is "the wrapper observed deltas but was not authorized to clean them" (new) vs. "the wrapper observed deltas and reverted out-of-scope ones" (existing). Both are forms of scope violation in the wire-protocol sense; `error.code` is the orchestrator's discriminator.
2. **`build_scope_violation()` accepts `error_code`; default preserves back-compat.** One signature change; default `"scope_violation"` keeps every existing call site behavior-identical. New callers pass `error_code="wrapper_autoclean_blocked"`. Adding a new envelope builder (`build_wrapper_autoclean_blocked()`) was considered and rejected because it diverges only in `error.code`; keeping one builder makes the constructor surface narrower.
3. **Short-circuit logic lives in `apply_cleanup`, not in `cmd_run`.** Codex's option (b): the cleanup module is the single source of truth for "what would have been cleaned"; making `cmd_run` re-derive `_changed_paths` would duplicate the logic. `apply_cleanup` returns a new `cleanup_strategy: "skipped_authorization_blocked"` value; `cmd_run` translates that into the envelope. This keeps the wrapper-level orchestration thin.
4. **Status precedence stays sane.** Documented order: `cleanup_failure > scope_violation(error_code=wrapper_autoclean_blocked) > scope_violation(error_code=scope_violation) > schema_invalid`. Rationale: `cleanup_failure` continues to win because a non-empty `failed_paths` means the working tree is in an unknown state; `wrapper_autoclean_blocked` outranks the legacy `scope_violation` because it represents a more cautious "we preserved everything pending user input" outcome that the orchestrator must not overwrite with a stale schema-validation failure.
5. **`scope` sub-object carries the preserved deltas.** The §7 envelope's `scope.observed_delta_tracked` and `observed_delta_untracked` already exist; the new path populates them with the post-baseline observed deltas (the paths the orchestrator now needs to act on). `scope.scope_violation_detected` is `False` (not a scope violation in the sense of "agent wrote outside declared"; the wrapper detected a missing authorization). `scope.scope_misreport_detected` is `False`.

## Verification

- `_claude_dispatch_envelope.build_scope_violation(message=m, scope=s, error_code="wrapper_autoclean_blocked")` returns an envelope with `error.code == "wrapper_autoclean_blocked"`.
- `_claude_dispatch_envelope.build_scope_violation(message=m, scope=s)` (no `error_code` kwarg) returns an envelope with `error.code == "scope_violation"` (back-compat preserved).
- `_claude_dispatch_cleanup.apply_cleanup` returns `cleanup_strategy: "skipped_authorization_blocked"` AND `out_of_scope_paths: [<observed deltas>]` AND `restored: []` AND `deleted: []` when invoked with `authorization_source="wrapper-declared-scope"` AND `declared=[]` AND non-empty observed delta. (The new branch fires upstream of the classification loop; no path is reverted.)
- `plan_claude_dispatch.py:cmd_run` translates that cleanup result into an envelope with `status: "scope_violation"`, `error.code: "wrapper_autoclean_blocked"`, `scope.observed_delta_tracked` populated with the preserved tracked deltas, `scope.observed_delta_untracked` populated with the preserved untracked deltas, working tree unchanged.
- The wrapper's status precedence is documented in a comment block above `cmd_run` step 9.
- New tests in `tests/scripts/test_claude_dispatch_envelope.py` and `tests/scripts/test_plan_claude_dispatch_cli.py` (five tests total).

## Tasks

### TASK-004: Schema-fail short-circuit + `error.code: wrapper_autoclean_blocked` in `plan_claude_dispatch.py:cmd_run`

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/_claude_dispatch_envelope.py (`build_scope_violation` at `:366` — relax to accept `error_code` keyword param with default `"scope_violation"`)
  - plugins/plan-executor/scripts/_claude_dispatch_cleanup.py (`apply_cleanup` — new short-circuit branch; new `cleanup_strategy: "skipped_authorization_blocked"` value)
  - plugins/plan-executor/scripts/plan_claude_dispatch.py (`cmd_run` step 9 around `:697` — new branch detecting the cleanup short-circuit and constructing the new envelope; comment block documenting status precedence)
  - tests/scripts/test_claude_dispatch_envelope.py (assert `build_scope_violation` accepts the new kwarg; default preserves behavior)
  - tests/scripts/test_claude_dispatch_cleanup.py (new test for the short-circuit branch in `apply_cleanup`)
  - tests/scripts/test_plan_claude_dispatch_cli.py (new fixtures covering the schema-fail short-circuit path)
- **Dependencies:** [002]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_claude_dispatch_cli.py -q -k "wrapper_autoclean_blocked or WrapperAutocleanBlocked or schema_fail_short_circuit"`
- **Acceptance criteria:**
  - `_claude_dispatch_envelope.build_scope_violation` (`:366`) gains a keyword-only param `error_code: str = "scope_violation"`. The internal `_error()` call at `:401` uses the supplied code instead of the hardcoded `"scope_violation"`. All existing callers (none today; the function is the only call site of the constant) continue to work without change because the default value preserves behavior.
  - `_claude_dispatch_cleanup.apply_cleanup` adds a new branch immediately after the authorization-gate check (added by TASK-002):
    - When `authorization_source == "wrapper-declared-scope"` AND `len(declared) == 0` AND the post-baseline observed delta is non-empty (i.e., the agent wrote files but the orchestrator declared no scope), short-circuit and return:
      ```python
      return {
          "scope_violation_detected": False,  # not a scope violation; missing authorization
          "scope_misreport_detected": False,
          "restored": [],
          "deleted": [],
          "failed_paths": [],
          "out_of_scope_paths": sorted(observed_delta),  # preserved
          "misreported_paths": [],
          "protected_skipped": [],
          "baseline_captured": True,
          "cleanup_strategy": "skipped_authorization_blocked",
      }
      ```
    - The branch fires AFTER the existing `cleanup_strategy: "skipped_no_baseline"` and `"skipped_git_failed"` branches (both pre-existing). The `_changed_paths` call already runs before the classification loop at line 662; the new branch can sit between the post-snapshot at line 589 and the classification loop, after `observed_delta` has been computed but before any `_restore_path` call.
    - Reading `wrapper-empty-scope-readonly` with empty declared continues to revert (a read-only agent wrote anything → contract violation; the existing classification loop handles this).
  - `plan_claude_dispatch.py:cmd_run` step 9 (around `:697`) is restructured:
    1. Compute `authorization_source` per TASK-002 (agent identity discriminator).
    2. Call `apply_cleanup(baseline, declared, repo_root, authorization_source=...)`.
    3. Inspect `cleanup_result["cleanup_strategy"]`:
       - `"skipped_authorization_blocked"` → build envelope via `build_scope_violation(message="wrapper autoclean blocked: write-authorized agent dispatched with empty declared_files_changed; preserving N observed deltas pending user disposition", error_code="wrapper_autoclean_blocked", scope=<...>)`. The `scope` sub-object is constructed via `_merge_scope_with_cleanup` as today, then the function picks the appropriate envelope builder.
       - `cleanup_failure` (existing) → `build_cleanup_failure(...)` (existing precedence).
       - `scope_violation_detected` (existing) → `build_scope_violation(message=..., scope=...)` (no `error_code`; defaults to `"scope_violation"`).
       - Otherwise → existing happy-path envelope.
  - The wrapper's status precedence is documented in a comment block above `cmd_run` step 9:
    ```python
    # Status precedence (TASK-004 of wrapper_autoclean_authorization).
    # cleanup_failure > scope_violation(error_code=wrapper_autoclean_blocked)
    #   > scope_violation(error_code=scope_violation) > schema_invalid.
    # Rationale: cleanup_failure continues to win because a non-empty
    # failed_paths means the working tree is in an unknown state.
    # wrapper_autoclean_blocked outranks scope_violation because it represents
    # a more cautious "we preserved everything pending user input" outcome
    # that the orchestrator must not overwrite with a stale schema-validation
    # failure. The orchestrator's routing ladder reads error.code to
    # distinguish the new "preserve everything" path from the legacy
    # "wrapper reverted out-of-scope writes" path.
    ```
  - The schema-validation failure path (around `:784`) is NOT changed — `schema_invalid` continues to fire when the inner agent result fails its output-schema validation. The new short-circuit is upstream of that, at the cleanup step, and is independent of inner-result schema validation.
  - New tests:
    - `test_build_scope_violation_default_error_code` — call without `error_code`; assert `envelope["error"]["code"] == "scope_violation"` (back-compat).
    - `test_build_scope_violation_custom_error_code` — call with `error_code="wrapper_autoclean_blocked"`; assert envelope's error code is the new value.
    - `test_apply_cleanup_short_circuits_on_authorization_blocked_path` — `wrapper-declared-scope` + `declared=[]` + observed delta on `x.py`; assert `cleanup_strategy == "skipped_authorization_blocked"` AND `out_of_scope_paths == ["x.py"]` AND working tree still has `x.py`.
    - `test_cmd_run_emits_wrapper_autoclean_blocked_when_declared_empty_for_implementer` — fixture: `agent="plan-implementer"`, `declared_files_changed=[]` (or omitted), backend writes 3 files; assert envelope `status == "scope_violation"`, `error.code == "wrapper_autoclean_blocked"`, `scope.observed_delta_tracked` contains the 3 paths, working tree still has the 3 files.
    - `test_cmd_run_emits_normal_scope_violation_when_declared_nonempty_with_oos` — fixture: `declared_files_changed=["x.py"]`, backend writes `x.py` (in scope) and `y.py` (out of scope); assert envelope `status == "scope_violation"`, `error.code == "scope_violation"` (NOT autoclean-blocked), `y.py` is reverted, `x.py` is preserved.
    - `test_cmd_run_does_not_short_circuit_for_plan_analyst` — fixture: `agent="plan-analyst"`, `declared_files_changed=[]`, backend writes 1 file (contract violation); assert envelope `status == "scope_violation"`, `error.code == "scope_violation"` (the readonly path runs `apply_cleanup`, which reverts the file).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/_claude_dispatch_envelope.py plugins/plan-executor/scripts/_claude_dispatch_cleanup.py plugins/plan-executor/scripts/plan_claude_dispatch.py tests/scripts/test_claude_dispatch_envelope.py tests/scripts/test_claude_dispatch_cleanup.py tests/scripts/test_plan_claude_dispatch_cli.py`

**Description:**
Implements the recoverable-failure path. When the orchestrator forgets to populate `declared_files_changed` for a write-authorized agent (Layer A regression OR an in-flight dispatch from before TASK-001 lands), the wrapper detects the situation pre-cleanup and surfaces a structured `error.code: wrapper_autoclean_blocked` envelope while preserving the working tree. The orchestrator's resume protocol (TASK-006) routes that envelope through the existing `Awaiting-user pause` subroutine. This is the load-bearing recovery path for the TASK-009 destruction event: with this in place, any future occurrence of "implementer wrote files, orchestrator didn't populate declared scope" pauses cleanly instead of destroying work.

**Implementation notes.** The "compute observed_delta pre-cleanup" step lives inside `apply_cleanup` already (`_changed_paths()` at line 589, then the classification loop at 662). The new branch sits between those two — `observed_delta` is computed, then if the new conditions hold (`wrapper-declared-scope` + empty declared + non-empty observed), return early with the new `cleanup_strategy` value before any `_restore_path` call. This keeps the gate logic colocated with the rest of the cleanup module's contract.

The wrapper's `cmd_run` change is a `match`/`if-elif` over `cleanup_strategy` values. Today the wrapper translates `cleanup_failure` and `scope_violation_detected` into specific envelope builders; this task adds one more branch for `skipped_authorization_blocked`.

The "compute observed_delta" computation is already cheap (two `git diff --name-only` calls in `_changed_paths`); no new cost. The branch fires only when the conditions hold, so existing callers see no behavior change.
