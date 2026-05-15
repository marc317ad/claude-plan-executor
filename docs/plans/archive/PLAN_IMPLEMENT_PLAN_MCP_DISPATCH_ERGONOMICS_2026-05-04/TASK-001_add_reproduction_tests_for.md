# TASK-001 — Add reproduction tests for MCP builder file-output acknowledgement

## Goal

Add reproduction tests for MCP builder file-output acknowledgement

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- Add or extend a test that calls `plan_ops__build_claude_dispatch_input` with `output` set to a temp file.
- Assert the file is written under MCP.
- Assert the MCP structured response contains an explicit file-output acknowledgement such as `output_written: true` and `output: "<path>"`.
- Assert no public MCP response contains keys beginning with `__plan_ops_`.
- Assert CLI `--output <path> --json` still writes the file and emits no stdout.
- Assert output schema documents the acknowledgement fields.
- This task is expected to fail before TASK-002 and must not be implemented in parallel with TASK-002 by a separate worker.

## Tasks

### TASK-001: Add reproduction tests for MCP builder file-output acknowledgement

- **Status:** done
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops_mcp_conformance.py`
  - `tests/scripts/test_plan_ops_mcp_schemas.py`
  - `tests/scripts/fixtures/plan_ops_pure_core/tier_b/build-claude-dispatch-input__happy_file_mode.expected.json`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_schemas.py -k "build_claude_dispatch_input or stdout_suppressed or output"`
- **Acceptance criteria:**
  - Add or extend a test that calls `plan_ops__build_claude_dispatch_input` with `output` set to a temp file.
  - Assert the file is written under MCP.
  - Assert the MCP structured response contains an explicit file-output acknowledgement such as `output_written: true` and `output: "<path>"`.
  - Assert no public MCP response contains keys beginning with `__plan_ops_`.
  - Assert CLI `--output <path> --json` still writes the file and emits no stdout.
  - Assert output schema documents the acknowledgement fields.
  - This task is expected to fail before TASK-002 and must not be implemented in parallel with TASK-002 by a separate worker.
- **Reversion guidance:** none

**Description:**

## Execution log — 20260504T234846 (paused)

Starting SHA: `02bd0682e7e46a32ead527b2c63b3ec8616e0d86`  → Ending SHA: `02bd0682e7e46a32ead527b2c63b3ec8616e0d86`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | codex | none | paused | — | implementer outcome=failure cause=independent_test_run_failed; intentional red-before-green per acceptance criterion 7; awaiting user instruction |
