# TASK-003 — `plan_codex_dispatch.py:_restore_in_scope` authorization gate + `_handle_timeout_cleanup` call-site update

## Goal

`plan_codex_dispatch.py:_restore_in_scope` authorization gate + `_handle_timeout_cleanup` call-site update

## Context

The `prohibit_silent_revert` plan (committed `8bb0b7b`) hardened the orchestrator surface against silent destruction of completed work. Its TASK-009 then ran via the bash-dispatched plan-implementer wrapper and **the wrapper destroyed 4 of the 5 implementer-written files** — at the Claude wrapper layer (TASK-002 closes that). The Codex wrapper has its own `_restore_in_scope` helper (`plan_codex_dispatch.py:894`) that calls `git restore` on observed deltas; today it is reachable only from `_handle_timeout_cleanup` at `:1003` (the post-Codex-timeout in-scope cleanup) but it has no authorization gate, no closed enum, and no audit-log entry. The wrapper-layer Completed-Work Preservation Principle must hold at every wrapper-side mutation surface, not just the Claude one.

The Codex wrapper has fewer mutation surfaces than the Claude wrapper: `validate_scope` (`:918`) is observe-only by design (the function docstring says "the wrapper NEVER mutates files outside `allowed_files`"); only `_handle_timeout_cleanup` actually calls `git restore`. So the authorization enum is single-valued (`wrapper-codex-timeout-cleanup`) at v1. The asymmetry between this and TASK-002's two-value enum is documented so future contributors who add a new mutating cleanup path know to extend the enum.

### Decisions folded in

1. **Mirror TASK-002's sentinel-default keyword-only shape.** Same diagnostic ergonomics (`TypeError` on missing, `ValueError` on unknown). The two wrappers share no state but use the same gate pattern so reviewers can recognize the contract on sight.
2. **Single-value enum at v1.** `wrapper-codex-timeout-cleanup` is the only authorized call site today. Extending to multiple values is a future task when a new Codex-side mutating path appears.
3. **`validate_scope` stays observe-only and gateless.** The function does not mutate files outside `allowed_files`; adding an authorization gate would be cargo-culted noise. A one-line comment documents this so future contributors don't add the gate by reflex.
4. **Codex timeout cleanup is destructive by design.** The path was added in PLAN_NESTED_DISPATCH §F1 to clean up a partial Codex run after a 300-second timeout. The cleanup restores in-scope files (those in the task's `allowed_files` list) — i.e., it reverts the partial work the Codex agent wrote. After this task, that destruction continues to be authorized via the new enum value, so Codex-timeout behavior is preserved. Codex's second-order risk callout: do NOT gate it so strictly that timed-out Codex runs leave partial edits in task files; the existing in-scope restore is the correct behavior, and the gate just makes that authorization explicit.

## Verification

- `python3 -c "from plan_codex_dispatch import _restore_in_scope; _restore_in_scope([], [], '/tmp')"` exits with `TypeError` whose message contains the literal "expected one of" and the value `['wrapper-codex-timeout-cleanup']`.
- `_restore_in_scope([], [], '/tmp', authorization_source="bogus")` exits with `ValueError`.
- `_handle_timeout_cleanup(repo_root, allowed_files, baseline)` continues to work and passes `authorization_source="wrapper-codex-timeout-cleanup"` to `_restore_in_scope` internally. Verified by `monkeypatch` capturing the kwargs.
- `validate_scope` is unchanged. A one-line comment is added documenting it is observe-only and gateless.
- The Codex wrapper module docstring (or section comment block above `_restore_in_scope`) gains a one-paragraph note documenting the new authorization contract and the asymmetry: the Codex wrapper has only one mutating cleanup call site (`_handle_timeout_cleanup`), so the authorization enum is a single value; `validate_scope` is observe-only and gateless. Future contributors who add a new mutating cleanup path MUST extend the enum.
- Existing Codex wrapper integration tests pass (the timeout-cleanup behavior is unchanged; only the authorization plumbing is new).

## Tasks

### TASK-003: `plan_codex_dispatch.py:_restore_in_scope` authorization gate + `_handle_timeout_cleanup` call-site update

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_codex_dispatch.py (`_restore_in_scope` at `:894`; `_handle_timeout_cleanup` at `:1003`; call-site at `:1084`; `validate_scope` at `:918` — one-line observe-only comment)
  - tests/scripts/test_plan_codex_dispatch.py (or the cleanup-specific test file; whichever currently exercises `_restore_in_scope` / `_handle_timeout_cleanup`)
- **Dependencies:** [002]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_codex_dispatch*.py -q -k "restore_in_scope or RestoreInScope or timeout_cleanup or TimeoutCleanup"`
- **Acceptance criteria:**
  - `plan_codex_dispatch.py` module-level constants (mirroring TASK-002 in shape):
    - `_MISSING_AUTHORIZATION_SOURCE = object()` (separate sentinel from the Claude wrapper's; the two wrappers share no state).
    - `ALLOWED_RESTORE_AUTHORIZATION_SOURCES: frozenset[str] = frozenset({"wrapper-codex-timeout-cleanup"})`.
    - A docstring comment block above the constants enumerates the value and what it authorizes. Explicitly notes that `validate_scope` does NOT need a gate because it is observe-only.
  - `_restore_in_scope` signature gains a keyword-only param: `*, authorization_source: object = _MISSING_AUTHORIZATION_SOURCE`. Body raises `TypeError` (missing) / `ValueError` (unknown) with the same shape as TASK-002. The check fires **before** any `_git(["restore", ...])` (line 906–908) or `Path.unlink()` (line 913) call in the function body.
  - `_handle_timeout_cleanup` (`:1003`) call-site at `:1084` is updated:
    ```python
    if restore_tracked or delete_untracked:
        _restore_in_scope(
            restore_tracked, delete_untracked, repo_root,
            authorization_source="wrapper-codex-timeout-cleanup",
        )
    ```
    A one-line comment above the call documents that timeout cleanup is the only authorized call site for `_restore_in_scope`.
  - `validate_scope` (`:918`) gains a one-line comment at the top of its body documenting that the function is observe-only and does not require an authorization gate. Mirrors the existing wording in the docstring ("the wrapper NEVER mutates files outside `allowed_files`") so the comment and docstring agree.
  - The Codex wrapper module's `# Scope validation` comment block (around line 889) gains one paragraph after the existing comments documenting the new authorization contract:
    ```
    # Authorization gate (v1, TASK-003 of wrapper_autoclean_authorization).
    # `_restore_in_scope` is the wrapper's only file-mutating cleanup helper.
    # It requires an explicit `authorization_source` keyword argument from
    # the closed enum `ALLOWED_RESTORE_AUTHORIZATION_SOURCES`. v1 has one
    # value (`wrapper-codex-timeout-cleanup`) because timeout cleanup is the
    # only mutating call site today. New mutating paths MUST extend the
    # enum and add a corresponding entry to the audit drift check in
    # `plan_ops.py audit` (TASK-005). `validate_scope` is observe-only and
    # gateless.
    ```
  - New tests:
    - `test_restore_in_scope_raises_typeerror_without_authorization_source` — direct call without the param; assert `TypeError` with "expected one of" in message.
    - `test_restore_in_scope_raises_valueerror_on_unknown_authorization_source` — bogus value; assert `ValueError`.
    - `test_restore_in_scope_accepts_wrapper_codex_timeout_cleanup` — call with the legal value; assert it runs (the test fixture sets up a tmp git repo with one tracked file dirty + one untracked file, asserts both are reverted).
    - `test_handle_timeout_cleanup_passes_authorization_source` — invoke `_handle_timeout_cleanup` end-to-end with a fixture timeout; assert it succeeds AND the captured `_restore_in_scope` call carried `authorization_source="wrapper-codex-timeout-cleanup"` (use `monkeypatch` on `_restore_in_scope` to capture kwargs).
    - `test_validate_scope_does_not_call_restore_in_scope` — invoke `validate_scope` with a non-empty observed delta outside `allowed_files`; assert `_restore_in_scope` is never called (observe-only invariant).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_codex_dispatch.py tests/scripts/test_plan_codex_dispatch.py`

**Description:**
Mirrors TASK-002 at the Codex wrapper. The Codex wrapper has fewer mutation surfaces than the Claude wrapper (its `validate_scope` is observe-only; only `_handle_timeout_cleanup` mutates), so the authorization enum is single-valued at v1. The asymmetry is documented so future contributors who add a Codex-side cleanup path know to extend the enum.

**Implementation notes.** The Codex wrapper's `_restore_in_scope` (`:894`) takes positional `tracked, untracked, repo_root` arguments. Adding a keyword-only param is a clean append; existing call sites at `:1084` get the kwarg added. The function is private (single underscore) and never called from tests directly except via `_handle_timeout_cleanup`, so the test-update surface is small. Ensure the new tests use a real `git init -q -b main` fixture (mirrors the existing `tests/scripts/test_plan_codex_dispatch_*.py` patterns) so the `_git("restore", ...)` calls actually execute against a real repo and we verify behavior end-to-end.
