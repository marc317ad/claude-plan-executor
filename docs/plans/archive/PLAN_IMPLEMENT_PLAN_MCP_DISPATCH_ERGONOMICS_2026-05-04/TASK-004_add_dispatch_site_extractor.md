# TASK-004 — Add dispatch-site extractor ordering regression tests

## Goal

Add dispatch-site extractor ordering regression tests

## Context

Auto-decomposed child for TASK-004. See the source plan for broader context.

## Verification

- Cover the canonical sequence `build_claude_dispatch_input -> plan_claude_dispatch.py run -> claude_envelope_extract -> route` for at least implementer and one remediation path.
- Include the MCP file-output path where `build_claude_dispatch_input` returns a slim acknowledgement, and assert the runner does not mistake that acknowledgement for a wrapper envelope.
- Verify routing consumes normalized extractor fields (`status`, `outcome`, `scope_violation`, `error`) rather than raw wrapper `.status` / `.result`.
- Include a non-`ok` wrapper status case and assert commit is forbidden before any Phase D route can mark the task committable.
- Include a `scope_violation` case and assert the normalized `scope_violation` field drives pause/reconcile behavior.

## Tasks

### TASK-004: Add dispatch-site extractor ordering regression tests

- **Status:** done
- **Priority:** high
- **Files:**
  - `tests/scripts/test_implement_plan_runner_phase_d.py`
  - `tests/scripts/test_implement_plan_provider_adapters.py`
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_phase_d.py tests/scripts/test_implement_plan_provider_adapters.py tests/scripts/test_implement_plan_mcp_e2e.py -k "claude_envelope_extract or remediation or scope_violation"`
- **Acceptance criteria:**
  - Cover the canonical sequence `build_claude_dispatch_input -> plan_claude_dispatch.py run -> claude_envelope_extract -> route` for at least implementer and one remediation path.
  - Include the MCP file-output path where `build_claude_dispatch_input` returns a slim acknowledgement, and assert the runner does not mistake that acknowledgement for a wrapper envelope.
  - Verify routing consumes normalized extractor fields (`status`, `outcome`, `scope_violation`, `error`) rather than raw wrapper `.status` / `.result`.
  - Include a non-`ok` wrapper status case and assert commit is forbidden before any Phase D route can mark the task committable.
  - Include a `scope_violation` case and assert the normalized `scope_violation` field drives pause/reconcile behavior.
- **Reversion guidance:** none

**Description:**

Add regression tests covering the canonical Claude wrapper dispatch sequence (`build_claude_dispatch_input` → `plan_claude_dispatch.py run` → `claude_envelope_extract` → route) for the implementer and one remediation path. Cover the MCP file-output acknowledgement path, normalized extractor field consumption, non-`ok` wrapper status (commit forbidden), and `scope_violation` driving pause/reconcile behavior.

## Execution log — 20260505T023642 (paused)

Starting SHA: `c97d14dad71ccbd85b4d4980fe9d340d0deaa3a2`  → Ending SHA: `c97d14dad71ccbd85b4d4980fe9d340d0deaa3a2`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 004 | claude | codex | needs-rework [narrow-remediation] | (uncommitted — paused) | Phase D narrow-remediation re-review still needs-rework on 2 findings; paused per protocol awaiting user instruction. |
