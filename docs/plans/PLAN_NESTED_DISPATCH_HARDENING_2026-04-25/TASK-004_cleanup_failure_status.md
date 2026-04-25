# TASK-004 — cleanup_failure envelope status

## Goal

`cleanup_failure` envelope status surfaces silent OSError during delta-bounded cleanup.

## Context

Run 20260425T124346 shipped v1 of `_claude_dispatch_cleanup.py`. The cleanup module's `apply_cleanup` catches per-file `OSError` (read-only filesystem, permission-denied target, parent dir not writable, etc.) and continues silently. The wrapper's `cmd_run` then emits `status: ok` (or `scope_violation` if out-of-scope writes were observed and *attempted* to be reverted), even when the actual revert failed and the dangerous file is still on disk.

The fix adds a `cleanup_failure` status to the envelope vocabulary, a `build_cleanup_failure(failed_paths, ...)` constructor in `_claude_dispatch_envelope.py`, a `failed_paths: list[str]` return field from `apply_cleanup`, and a check in `cmd_run` that emits `cleanup_failure` (taking precedence over backend success) when any paths could not be reverted. A failed cleanup is a hard fail because the working tree is in an unknown state — the orchestrator must not silently accept the dispatch as successful.

This task depends on TASK-001 (which renames `_extract_declared_files_changed` and changes how cleanup is wired in `cmd_run`) and TASK-005 (which touches the envelope module's docstring) for clean rebasing — declare both as dependencies.

## Verification

- `cleanup_failure` is a new value in the `status` enum of `claude_dispatch_output.json`.
- `build_cleanup_failure(failed_paths, **extra)` exists in `_claude_dispatch_envelope.py`; module exports list grows from 10 to 11 builders.
- `_claude_dispatch_cleanup.apply_cleanup` returns a new `failed_paths: list[str]` field collecting per-file OSError victims.
- `plan_claude_dispatch.cmd_run` checks `cleanup_result['failed_paths']` and emits `build_cleanup_failure` when non-empty (taking precedence over `build_ok` / `build_scope_violation`).
- Tests exercise read-only target, mixed success/failure, and constructor schema-validation.
- All existing tests continue to pass (additive change).

## Tasks

### TASK-004: cleanup_failure envelope status

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_envelope.py` (modify)
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_output.json` (modify)
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` (modify)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (modify)
  - `tests/scripts/test_claude_dispatch_cleanup.py` (modify)
  - `tests/scripts/test_claude_dispatch_envelope.py` (modify)
  - `tests/scripts/test_plan_claude_dispatch_cli.py` (modify)
- **Dependencies:** [001, 005]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_dispatch_cleanup.py tests/scripts/test_claude_dispatch_envelope.py tests/scripts/test_plan_claude_dispatch_cli.py`
- **Acceptance criteria:**
  - Add `cleanup_failure` to the `status` enum in `claude_dispatch_output.json` (alongside `ok` / `timeout` / `backend_error` / `denied` / `schema_invalid` / `scope_violation` / `input_invalid` / `manifest_invalid` / `depth_exceeded` / `budget_exhausted`).
  - Add `build_cleanup_failure(failed_paths: list[str], **extra)` constructor in `_claude_dispatch_envelope.py`. Module exports list grows from 10 to 11 `build_*` constructors. Routed through the same `_build_envelope` chokepoint as siblings; rejects unknown top-level keys.
  - `_claude_dispatch_cleanup.apply_cleanup` returns a new `failed_paths: list[str]` field — paths that could not be reverted (revert failed) or deleted (newly-created out-of-scope file with read-only parent dir, etc.) due to OSError. Each per-file OSError is caught + the path appended + processing continues; never silenced.
  - `plan_claude_dispatch.cmd_run`: after `apply_cleanup`, if `cleanup_result['failed_paths']` is non-empty, emit `build_cleanup_failure(failed_paths=...)` instead of `build_ok` / `build_scope_violation`. Cleanup-failure status takes precedence over backend success — a failed cleanup is a hard fail because the working tree is in an unknown state.
  - Test: read-only target file in cleanup scope → `apply_cleanup` returns `failed_paths` non-empty; pipeline emits `cleanup_failure` envelope.
  - Test: mixed (some files reverted, some failed) → `cleanup_failure` status; envelope contains both successful (`restored`/`deleted`) and failed (`failed_paths`) lists for diagnostics.
  - Test: `build_cleanup_failure` is exposed; constructor validates against output schema; rejects unknown top-level keys (consistent with sibling constructors).
  - Existing tests continue to pass (additive change; no existing path regresses).
- **Reversion guidance:** none

**Description:**

Adds a `cleanup_failure` envelope status to surface silent `OSError` victims during delta-bounded cleanup. The v1 wrapper catches per-file `OSError` in `_claude_dispatch_cleanup.apply_cleanup` and continues, then `cmd_run` emits `status: ok` even when the dangerous file is still on disk because the revert failed (read-only filesystem, permission denied, etc.). This task: (1) extends the envelope status enum in `claude_dispatch_output.json` with `cleanup_failure`, (2) adds a `build_cleanup_failure(failed_paths, **extra)` constructor in `_claude_dispatch_envelope.py` routed through the same `_build_envelope` chokepoint as the other 10 builders, (3) makes `apply_cleanup` return a new `failed_paths: list[str]` field collecting per-file `OSError` victims (the per-file errors are still caught — processing continues — but they are now visible to the caller), and (4) makes `cmd_run` emit `cleanup_failure` (precedence over backend success) when `failed_paths` is non-empty. A failed cleanup is a hard fail because the working tree is in an unknown state. Tests cover the read-only-target case, the mixed (some reverted, some failed) case, and the constructor's schema-validation surface. Depends on TASK-001 (cleanup wiring change in `cmd_run`) and TASK-005 (envelope docstring touch) so that `cmd_run`'s post-cleanup branch and the envelope module's docstring are stable when this work lands.
