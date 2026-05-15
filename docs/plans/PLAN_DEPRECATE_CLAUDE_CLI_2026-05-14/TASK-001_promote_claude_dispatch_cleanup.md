# TASK-001 — Promote `_claude_dispatch_cleanup.py` to first-class `_dispatch_cleanup.py` library

## Goal

Promote `_claude_dispatch_cleanup.py` to first-class `_dispatch_cleanup.py` library

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- `git mv` the cleanup module to `_dispatch_cleanup.py`. Update the one importer (`plan_claude_dispatch.py:85`) to `import _dispatch_cleanup as cleanup`. Verify with `grep -rn "_claude_dispatch_cleanup" plugins/ tests/` that no other in-tree importer exists; update any that surface.
- Extend `ALLOWED_CLEANUP_AUTHORIZATION_SOURCES` (`_dispatch_cleanup.py:141–144`) with `"orchestrator-declared-scope"` AND `"orchestrator-empty-scope-readonly"`. Mirror the existing wrapper-token semantics (write-authorized vs read-only).
- Extend `_RESTORE_GATE_TRANSLATION` (`_dispatch_cleanup.py:161–164`) with both new tokens, each mapping to `"wrapper_internal_cleanup_explicit_declaration"` (the same per-file restore gate the wrapper tokens use). Without this, `apply_cleanup` under `unattended_revert_policy="preserve-only"` will KeyError at the per-file restore call (per Finding 3).
- Update the docstring at `_dispatch_cleanup.py:115–138` to document the new orchestrator-direct caller and the shape of the new tokens.
- Add `test_apply_cleanup_accepts_orchestrator_declared_scope`: snapshot a tiny tmp repo, write a file outside the declared set, call `apply_cleanup(..., authorization_source="orchestrator-declared-scope", declared_files_changed=[<in-scope path>], unattended_revert_policy="preserve-only")`, assert the out-of-scope file is reverted and the result dict carries `cleanup_strategy="delta_bounded"`. NOTE: must pass `unattended_revert_policy="preserve-only"` — the default `pause` semantics return `cleanup_strategy="detect_only_revert_policy_pause"` and do NOT revert (per Finding 3a).
- Add `test_apply_cleanup_accepts_orchestrator_empty_scope_readonly`: snapshot, write any file, call `apply_cleanup(..., authorization_source="orchestrator-empty-scope-readonly", declared_files_changed=[], unattended_revert_policy="preserve-only")`, assert the file is reverted (read-only contract: any delta is a violation).
- Add `test_apply_cleanup_rejects_unknown_authorization_source`: call with `authorization_source="rogue"`, assert it raises ValueError (current behavior — pin it as a contract test now that the allowlist has four entries).
- All existing `test_claude_dispatch_cleanup*` tests still green under the new module name.

## Tasks

### TASK-001: Promote `_claude_dispatch_cleanup.py` to first-class `_dispatch_cleanup.py` library

- **Status:** Pending
- **Priority:** high
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` → `plugins/plan-executor/scripts/_dispatch_cleanup.py` (rename)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (update import)
  - `tests/scripts/test_claude_dispatch_cleanup*.py` → `tests/scripts/test_dispatch_cleanup*.py` (rename + extend)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_dispatch_cleanup`
- **Acceptance criteria:**
  - `git mv` the cleanup module to `_dispatch_cleanup.py`. Update the one importer (`plan_claude_dispatch.py:85`) to `import _dispatch_cleanup as cleanup`. Verify with `grep -rn "_claude_dispatch_cleanup" plugins/ tests/` that no other in-tree importer exists; update any that surface.
  - Extend `ALLOWED_CLEANUP_AUTHORIZATION_SOURCES` (`_dispatch_cleanup.py:141–144`) with `"orchestrator-declared-scope"` AND `"orchestrator-empty-scope-readonly"`. Mirror the existing wrapper-token semantics (write-authorized vs read-only).
  - Extend `_RESTORE_GATE_TRANSLATION` (`_dispatch_cleanup.py:161–164`) with both new tokens, each mapping to `"wrapper_internal_cleanup_explicit_declaration"` (the same per-file restore gate the wrapper tokens use). Without this, `apply_cleanup` under `unattended_revert_policy="preserve-only"` will KeyError at the per-file restore call (per Finding 3).
  - Update the docstring at `_dispatch_cleanup.py:115–138` to document the new orchestrator-direct caller and the shape of the new tokens.
  - Add `test_apply_cleanup_accepts_orchestrator_declared_scope`: snapshot a tiny tmp repo, write a file outside the declared set, call `apply_cleanup(..., authorization_source="orchestrator-declared-scope", declared_files_changed=[<in-scope path>], unattended_revert_policy="preserve-only")`, assert the out-of-scope file is reverted and the result dict carries `cleanup_strategy="delta_bounded"`. NOTE: must pass `unattended_revert_policy="preserve-only"` — the default `pause` semantics return `cleanup_strategy="detect_only_revert_policy_pause"` and do NOT revert (per Finding 3a).
  - Add `test_apply_cleanup_accepts_orchestrator_empty_scope_readonly`: snapshot, write any file, call `apply_cleanup(..., authorization_source="orchestrator-empty-scope-readonly", declared_files_changed=[], unattended_revert_policy="preserve-only")`, assert the file is reverted (read-only contract: any delta is a violation).
  - Add `test_apply_cleanup_rejects_unknown_authorization_source`: call with `authorization_source="rogue"`, assert it raises ValueError (current behavior — pin it as a contract test now that the allowlist has four entries).
  - All existing `test_claude_dispatch_cleanup*` tests still green under the new module name.
- **Reversion guidance:** `git mv` back, revert the importer change(s), revert the allowlist + translation-table extensions, drop the three new tests.

**Description:**
Promote `_claude_dispatch_cleanup.py` to first-class `_dispatch_cleanup.py` library. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-001. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
