# TASK-003: Provider Registry And Adapters

## Goal

Wrap the existing Codex, Claude, Gemini, and test-stub dispatch mechanisms behind a provider-neutral adapter interface.

## Context

The dispatch wrappers already enforce the important backend-specific behavior: schema output, timeouts, cleanup, depth/budget guardrails, and read-only restrictions. The runner should use those wrappers as adapters rather than absorbing their internals.

## Scoped Context

This task adds adapter classes and tests them with dry-run/stubbed subprocess calls. It does not integrate the adapters into the full runner loop.

### TASK-003: Provider Registry And Adapters

- **Status:** done
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_provider_adapters.py` (new)
  - `tests/scripts/fixtures/implement_plan_runner/providers/` (new fixture directory as needed)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_provider_adapters.py`
- **Description:** Wrap existing Codex, Claude, Gemini, and stub execution paths behind a single provider adapter interface with capability probing and structured dispatch results.
- **Acceptance criteria:**
  - Define a provider adapter interface with `probe()`, `classify()`, `implement()`, `review()`, `plan_review()`, `triage()`, and `author()` methods, where unsupported roles return structured `unsupported` results.
  - Adapter capabilities expose both provider id and current route identity (`route_implementer`, `route_reviewer`) so `review-route` receives only schema-supported values.
  - `CodexProvider` wraps `plan_codex_dispatch.py implement|review|plan-review` and includes `--target-task-id` when dispatching shared-file children.
  - `ClaudeProvider` wraps `plan_claude_dispatch.py run` and obtains canonical input with `PlanOpsFacade.build_claude_dispatch_input`.
  - `GeminiProvider` wraps `plan_gemini_dispatch.py review|plan-review` and returns unsupported for implementation, despite the wrapper exposing an implementation stub.
  - `StubProvider` supports all roles through deterministic fixture envelopes for e2e tests.
  - Provider `probe()` reports availability without mutating the repo and without requiring live dispatch.
  - Provider subprocess invocation is centralized in one timeout-aware helper that captures stdout/stderr and preserves wrapper JSON envelopes.
  - Adapter outputs normalize only transport fields (`provider`, `role`, `status`, `parsed`, `raw_envelope`, `error`); they do not translate verdict vocabularies except where existing parsers already do so.
  - Tests verify command construction for each provider role, including `--allow-gaps` on Codex/Gemini plan-review, Gemini implementation unsupported, and Claude `declared_files_changed` populated by the build input path.
  - Tests use fake subprocess runners; no live Codex, Claude, or Gemini calls.

**Description:**
Wrap existing Codex, Claude, Gemini, and stub execution paths behind a single
provider adapter interface with capability probing and structured dispatch
results.

## Verification

Run the task test command.

## Non-goals

- Choosing which provider should handle a task.
- Calling `review-route` or `plan-review-route`.
- Modifying dispatch wrapper scripts.
