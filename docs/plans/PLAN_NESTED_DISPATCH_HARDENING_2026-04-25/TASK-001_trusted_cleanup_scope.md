# TASK-001 — Trusted cleanup-scope source (sandbox-escape fix)

## Goal

Trusted cleanup-scope source (sandbox-escape fix) for `plan_claude_dispatch.py`.

## Context

Run 20260425T124346 shipped v1 of `plan_claude_dispatch.py` (commits eadc1a8..2252d1f on `plan/nested-dispatch`). The wrapper's delta-bounded cleanup is supposed to be the security backstop against agent misbehavior: anything the agent wrote outside its declared scope gets reverted.

A subsequent Gemini review surfaced a real P0 sandbox-escape: the cleanup scope is sourced from the **agent's own self-reported `result.files_changed` field** (envelope output), not from a trusted wrapper input. A malicious or buggy agent can write `evil.py` and then declare `result.files_changed = ['/etc/passwd', 'evil.py']` to extend the "authorized" set — cleanup will treat `evil.py` as in-scope and leave it on disk.

The fix is to add a top-level `declared_files_changed` field to the wrapper's input schema, populated by the orchestrator (the trusted caller), and use that as the cleanup authority instead of the agent's report. The agent's `result.files_changed` becomes informational only (used for `scope_misreport_detected` diff metadata).

The current sites:

- `plugins/plan-executor/scripts/plan_claude_dispatch.py:379-392` — `_extract_declared_files_changed(envelope)` pulls `result.files_changed` from the inner agent's output envelope.
- `plugins/plan-executor/scripts/plan_claude_dispatch.py:671-672` — `cmd_run` calls `cleanup.apply_cleanup(baseline, declared, repo_root)` where `declared` came from `_extract_declared_files_changed`.

## Verification

- `claude_dispatch_input.json` gains `declared_files_changed: array of string` (top-level optional, default `[]` when omitted). `additionalProperties: false` stays.
- `cmd_run` extracts `input['declared_files_changed']` and passes that as the cleanup authority. Agent output is no longer trusted for scope.
- `_extract_declared_files_changed` is renamed to `_extract_observed_files_changed` (informational only).
- A test demonstrates that an agent that lies about `result.files_changed` cannot bypass cleanup.
- A test demonstrates that omitting `declared_files_changed` defaults to `[]` (deny-by-default; cleanup reverts everything the agent wrote).
- Backward-compat: existing CLI tests that pass payloads without `declared_files_changed` continue to pass.

## Tasks

### TASK-001: Trusted cleanup-scope source (sandbox-escape fix)

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json` (modify)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (modify)
  - `tests/scripts/test_plan_claude_dispatch_cli.py` (modify)
- **Dependencies:** []
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_claude_dispatch_cli.py`
- **Acceptance criteria:**
  - Add `declared_files_changed: array of string` (top-level optional, default `[]`) to `claude_dispatch_input.json`. `additionalProperties: false` stays.
  - `cmd_run` extracts `input['declared_files_changed']` and passes it as the `declared_files_changed` arg to `cleanup.apply_cleanup` — NOT `_extract_declared_files_changed(envelope)`.
  - `_extract_declared_files_changed` is renamed to `_extract_observed_files_changed` (informational only — used for diff metadata / `scope_misreport_detected` logic).
  - Test: agent reports `result.files_changed = ['/etc/passwd']` (lying) but actually writes `evil.py`; with `declared_files_changed = []`, cleanup reverts `evil.py`. The agent's lie does NOT authorize anything.
  - Test: `declared_files_changed = ['a.py']`, agent writes `a.py` + `b.py` → `b.py` is reverted, `scope_violation_detected = true`.
  - Test: omitted field defaults to `[]` (deny-by-default for read-only agents like plan-analyst).
  - Backward-compat: existing CLI tests that pass payloads without `declared_files_changed` continue to pass.
- **Reversion guidance:** none

**Description:**

Closes the P0 sandbox-escape vulnerability that the v1 wrapper shipped with: cleanup scope was defined by the agent's self-report at `plan_claude_dispatch.py:671-672`, where `declared = _extract_declared_files_changed(envelope)` extracted `result.files_changed` from the agent's output envelope (function at lines 379-392). A malicious or buggy agent could lie in its report and the wrapper would treat the lie as authorization. This task adds a top-level `declared_files_changed` field to `claude_dispatch_input.json` (optional, default `[]`), routes it through `cmd_run` as the trusted cleanup authority, and renames the now-informational helper to `_extract_observed_files_changed`. The agent's `result.files_changed` continues to be parsed for `scope_misreport_detected` diff metadata (declared minus observed) but is no longer the security boundary. Tests demonstrate that (a) agent lies do not authorize, (b) over-writes outside the declared set are reverted, and (c) omitted-field deny-by-default behavior holds for read-only agents.
