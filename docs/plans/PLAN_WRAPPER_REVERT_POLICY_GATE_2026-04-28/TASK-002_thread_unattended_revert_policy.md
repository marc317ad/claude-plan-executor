# TASK-002 — Thread `unattended_revert_policy` from orchestrator → wrapper payload → `apply_cleanup` for all three wrappers

## Goal

Thread `unattended_revert_policy` from orchestrator → wrapper payload → `apply_cleanup` for all three wrappers

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- All three wrapper-input schema files (or inlined schemas) declare `unattended_revert_policy` as an optional top-level property with the closed enum `{"pause", "fail-fast", "preserve-only"}`. `description` cross-references the orchestrator-level flag.
- All three wrapper scripts read the field from the validated input and pass it to `cleanup.apply_cleanup(...)` (or analogous Codex/Gemini cleanup call) as the `unattended_revert_policy` keyword argument.
- All three `build-*-dispatch-input` subcommands populate the field from `os.environ.get("UNATTENDED_REVERT_POLICY")`. If unset, the field is omitted (wrapper defaults to non-destructive). If set to a value not in the enum, the subcommand exits non-zero with `errors[*].code = "unattended-revert-policy-invalid"` (parallel to `cmd_preflight`'s validation at line 4960).
- End-to-end test: `python3 plan_claude_dispatch.py run --input <payload-with-pause-and-an-out-of-scope-write>` returns a `scope_violation` envelope; `git status --porcelain` confirms the out-of-scope file's edit is still on disk; `envelope.extra.wrapper_events` carries an event documenting the policy-gated non-destruction.
- The schema-audit fixture (per TASK-008 of PLAN_GEMINI_INTEGRATION) covers the new field across all three wrapper-input schemas.
- Direct CLI smoke (one per wrapper): a payload with `unattended_revert_policy: "preserve-only"` + an out-of-scope write reverts the file (existing destructive behavior is preserved when the operator explicitly opts in).

## Tasks

### TASK-002: Thread `unattended_revert_policy` from orchestrator → wrapper payload → `apply_cleanup` for all three wrappers

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json` (add `unattended_revert_policy` as optional top-level field; closed enum `["pause", "fail-fast", "preserve-only"]`; default unset; description references `--unattended-revert-policy`)
  - `plugins/plan-executor/scripts/schemas/codex_dispatch_input.json` (same field, same shape)
  - `plugins/plan-executor/scripts/schemas/gemini_dispatch_input.json` (same field, same shape) — only if this schema file exists; if Gemini wrapper inlines its schema, update the inline version
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (extract `unattended_revert_policy` from validated input at the same level as `declared_files_changed`; pass it to `cleanup.apply_cleanup` at line 748)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (analogous edit; if codex has its own cleanup call, mirror the threading)
  - `plugins/plan-executor/scripts/plan_gemini_dispatch.py` (analogous edit; the four `_handle_timeout_cleanup` sites I patched on commit a7be6c5 also feed into this — verify they propagate the policy)
  - `plugins/plan-executor/scripts/plan_ops.py` (the `build-claude-dispatch-input`, `build-codex-dispatch-input`, and `build-gemini-dispatch-input` subcommands populate the field from `$UNATTENDED_REVERT_POLICY` env var, falling back to the value pinned by `cmd_preflight`; if no value is resolvable, omit the field — let the wrapper default kick in)
  - `tests/scripts/test_plan_claude_dispatch_cli.py` (1 new test: end-to-end policy threading — payload with `unattended_revert_policy=pause` + an out-of-scope write yields a `scope_violation` envelope with the working tree retaining the write)
  - `tests/scripts/test_plan_codex_dispatch_cli.py` if extant, or whichever fixture file covers the Codex wrapper CLI (1 analogous test)
  - `tests/scripts/test_plan_gemini_dispatch_cli.py` if extant (1 analogous test)
  - `tests/scripts/test_plan_ops.py` (1 new test per builder subcommand: emitted JSON's top-level `unattended_revert_policy` matches the env-var input)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch_cli.py tests/scripts/test_plan_codex_dispatch_cli.py tests/scripts/test_plan_gemini_dispatch_cli.py tests/scripts/test_plan_ops.py -k "unattended_revert_policy or revert_policy_gate or RevertPolicy"`
- **Acceptance criteria:**
  - All three wrapper-input schema files (or inlined schemas) declare `unattended_revert_policy` as an optional top-level property with the closed enum `{"pause", "fail-fast", "preserve-only"}`. `description` cross-references the orchestrator-level flag.
  - All three wrapper scripts read the field from the validated input and pass it to `cleanup.apply_cleanup(...)` (or analogous Codex/Gemini cleanup call) as the `unattended_revert_policy` keyword argument.
  - All three `build-*-dispatch-input` subcommands populate the field from `os.environ.get("UNATTENDED_REVERT_POLICY")`. If unset, the field is omitted (wrapper defaults to non-destructive). If set to a value not in the enum, the subcommand exits non-zero with `errors[*].code = "unattended-revert-policy-invalid"` (parallel to `cmd_preflight`'s validation at line 4960).
  - End-to-end test: `python3 plan_claude_dispatch.py run --input <payload-with-pause-and-an-out-of-scope-write>` returns a `scope_violation` envelope; `git status --porcelain` confirms the out-of-scope file's edit is still on disk; `envelope.extra.wrapper_events` carries an event documenting the policy-gated non-destruction.
  - The schema-audit fixture (per TASK-008 of PLAN_GEMINI_INTEGRATION) covers the new field across all three wrapper-input schemas.
  - Direct CLI smoke (one per wrapper): a payload with `unattended_revert_policy: "preserve-only"` + an out-of-scope write reverts the file (existing destructive behavior is preserved when the operator explicitly opts in).
- **Reversion guidance:** none

**Description:**
