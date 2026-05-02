# PLAN - MCP Tool Payload Wiring Fix

**Status:** Pending
**Created:** 2026-05-01
**Base branch:** main
**Investigation:** Codex local review plus Gemini CLI read-only investigation, 2026-05-01.

## Goal

Fix the MCP-vs-CLI drift that caused `/implement-plan` to fall back to `plan_ops.py` early and then keep using CLI / inline Python when MCP tools should have handled later calls.

The target outcome is simple: when `PLAN_OPS_TRANSPORT=mcp`, the skill examples and real MCP dispatch path use structured tool payloads consistently, including wrapper-envelope extraction, schedule persistence, run-log events, batch selection, and batch reconciliation.

## Gemini Findings

Gemini was run in read-only plan mode against the repo and confirmed the same core issues:

- `batch_next` is miswired: `_args_to_payload_batch_next` casts the boolean `from_schedule_state` flag to `pathlib.Path` at `plugins/plan-executor/scripts/plan_ops.py:5401`.
- `SKILL.md` has stale MCP examples that still pass `"stdin": <json>` for tools whose schemas now expose `"payload": <json>`.
- `log_event` needs explicit coverage for structured MCP payloads. Gemini proposed allowing both object and JSON-string forms in the MCP schema; this plan takes the stricter MCP-first route unless reproduction proves Claude Code requires the compatibility form.

Codex local review found one additional stale-stdin breakage in the same class:

- `plugins/plan-executor/scripts/schemas/mcp/reconcile_batch.input.json` lacks a `payload` property even though `_run_reconcile_batch` reads `stdin_text` at `plan_ops.py:4461`. The current SKILL example passes `"stdin": <envelopes_json>`, which the generic MCP dispatcher does not translate into stdin text.

Follow-up evidence from the first TASK-001 execution attempt added two more concrete bugs to this plan:

- `reconcile-batch` misses out-of-scope writes when wrappers report them under `scope.*` (`scope.out_of_scope_observed`, `scope.out_of_scope_tracked`, `scope.out_of_scope_untracked`) instead of legacy top-level fields. The reconciler returned `no_op` when it should have paused.
- `finalize_execution_log`, like `log_event`, can raise `TypeError('the JSON object must be str, bytes or bytearray, not <list|dict>')` when MCP passes native JSON list/dict values.

## Evidence

- `plugins/plan-executor/scripts/plan_ops.py:5393-5402`:
  `_args_to_payload_batch_next` currently builds `"from_schedule_state": pathlib.Path(args.from_schedule_state) if args.from_schedule_state else None`, but argparse defines `--from-schedule-state` as `action="store_true"` at `plan_ops.py:12050`.
- `plugins/plan-executor/scripts/schemas/mcp/batch_next.input.json` advertises `"from_schedule_state": {"type": "boolean"}`.
- `plugins/plan-executor/scripts/schemas/mcp/log_event.input.json` advertises structured `fields_json` as an object and `findings_json` as an array.
- `plugins/plan-executor/scripts/schemas/mcp/finalize_execution_log.input.json` advertises structured `rows_json` as an array.
- `plugins/plan-executor/scripts/schemas/mcp/claude_envelope_extract.input.json` requires `"payload"`, not `"stdin"`.
- `plugins/plan-executor/scripts/schemas/mcp/write_schedule.input.json` requires `"payload"`, not `"stdin"`.
- `plugins/plan-executor/scripts/schemas/mcp/reconcile_batch.input.json` currently has no `"payload"` field, despite the pure core reading stdin text.
- `plugins/plan-executor/scripts/plan_ops.py:_envelope_field` currently drives `reconcile_batch`'s `out_of_scope_observed` routing. It must treat nested wrapper `scope.*` fields as equivalent to the legacy top-level envelope fields.
- `plugins/plan-executor/skills/implement-plan/SKILL.md:85`, `:301`, `:311`, and `:411` still document MCP calls with `"stdin": <json>`.

## Scope

In scope:

- `plugins/plan-executor/scripts/plan_ops.py`
- `plugins/plan-executor/scripts/schemas/mcp/reconcile_batch.input.json`
- `plugins/plan-executor/scripts/plan_ops_mcp_server.py` generated registry block
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- MCP registration / implement-plan transport tests under `tests/scripts/`

Out of scope:

- Reworking Claude Code plugin loading or `.mcp.json`.
- Replacing wrapper subprocess calls (`plan_claude_dispatch.py`, `plan_codex_dispatch.py`, `plan_gemini_dispatch.py`) with MCP.
- Changing the Phase D state machine semantics.
- Adding schedule `state` blocks to legacy schedules. This plan fixes the `--from-schedule-state` crash and verifies behavior when `state` exists; legacy no-state schedules may continue to warn/no-op until the schedule-state plan ships.
- Implementing the fixes in this planning step.

## Tasks

### TASK-001: Add failing MCP dispatch regressions

- **Status:** DONE
- **Priority:** critical
- **Files:**
  - `tests/scripts/test_plan_ops_mcp_registrations.py`
  - `tests/scripts/test_implement_plan_transport_bootstrap.py`
- **Acceptance criteria:**
  - Add a test that calls `server_mod._dispatch_registered_tool("plan_ops__batch_next", {"schedule_file": ..., "from_schedule_state": true})` against a temp schedule with `state.done`. Before the fix, this must expose the boolean conversion bug; after the fix, the dispatch returns the expected next task.
  - Add a test that calls `server_mod._dispatch_registered_tool("plan_ops__log_event", {"event": "run_start", "fields_json": {"run_id": "R1", "plan_file": "P.md"}})` with `_append_run_log` monkeypatched. The assertion proves dict payloads reach `_run_log_event` unchanged and no CLI fallback is required.
  - Add a test that calls `server_mod._dispatch_registered_tool("plan_ops__finalize_execution_log", {"plan_file": ..., "rows_json": [{"task": "TASK-001", "agent": "codex", "reviewer": "gemini", "verdict": "approved", "commit": "abc1234", "notes": "ok"}], "outcome": "success"})`. The assertion proves native list payloads do not get reparsed with `json.loads`.
  - Add a test for `plan_ops__claude_envelope_extract` with `{"agent": "plan-implementer", "payload": <wrapper envelope>}` and assert it returns `status`, `outcome`, and `result`.
  - Add a reconcile regression with an envelope whose out-of-scope data appears only under `scope.*`; `reconcile_batch` must return a pause/action-required result instead of `no_op`.
  - Add a SKILL drift test that fails if MCP examples for `claude_envelope_extract`, `write_schedule`, `parse_plan_review_triage_report`, or `reconcile_batch` use `"stdin":` instead of `"payload":`.
  - Generalize the SKILL drift test where practical: enumerate MCP schemas with a `payload` property and assert `SKILL.md` examples for those tool names do not use `"stdin":`.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_implement_plan_transport_bootstrap.py tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py -q`

### TASK-002: Fix `batch_next` boolean payload conversion

- **Status:** pending
- **Priority:** critical
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
- **Acceptance criteria:**
  - Replace the `Path(...)` conversion for `from_schedule_state` with a boolean:
    `bool(getattr(args, "from_schedule_state", False))`.
  - Direct CLI behavior remains unchanged for `plan_ops.py batch-next --from-schedule-state --json`.
  - MCP dispatch with `from_schedule_state: true` reads the persisted schedule `state` block and does not require caller-maintained in-message `done` / `failed` / `locked_files`.
  - Add an explicit CLI regression for `plan_ops.py batch-next --from-schedule-state --json` so the `store_true` flag cannot regress into a `pathlib.Path(True)` TypeError.
  - Legacy schedules without a `state` block remain tolerated; schedule-state creation is not part of this plan.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_plan_ops.py -k "test_cli_batch_next_from_schedule_state_boolean or batch_next" -q`

### TASK-003: Reconcile nested wrapper scope fields

- **Status:** pending
- **Priority:** critical
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py`
- **Acceptance criteria:**
  - Update the envelope accessor used by `reconcile_batch` so legacy top-level fields and wrapper-native nested `scope.*` fields are both recognized.
  - An envelope with only `scope.out_of_scope_observed=true` and nested tracked/untracked lists must not return `no_op`.
  - When both legacy top-level and nested `scope.*` fields are present, booleans are ORed and path lists are unioned; the reconciliation summary should preserve enough detail to identify both sources.
  - Under `out_of_scope_policy="pause"`, the same envelope returns an action-required/pause result and preserves the wrapper scope details in the reconciliation summary.
  - Prefer centralizing this in `_envelope_field()` or the closest shared accessor instead of making each wrapper duplicate fields at the top level.
  - Audit `_envelope_field()` callers before broadening behavior; if any caller intentionally requires top-level-only fields, keep the nested fallback local to the reconciler path instead.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py -k "reconcile" -q`

### TASK-004: Normalize MCP payload schemas for stdin-backed tools

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/schemas/mcp/reconcile_batch.input.json`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
- **Acceptance criteria:**
  - `reconcile_batch.input.json` gets a required or optional `payload` array property representing the envelopes formerly delivered on stdin. The property should be required for MCP unless a deliberate empty-envelope default is documented.
  - Rely on the existing MCP adapter path (`_payload_from_mcp_arguments` / `_stdin_text_for_mcp_payload`) to convert schema `payload` into patched stdin text; do not add a second bespoke stdin bridge.
  - Regenerate the server registry with `venv/bin/python plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py`.
  - `test_codegen_block_is_byte_stable` remains green.
  - Do not add a generic `"stdin"` property to MCP schemas. MCP callers use `payload`; CLI fallback uses stdin.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_schemas.py tests/scripts/test_plan_ops_mcp_registrations.py -q`

### TASK-005: Update `SKILL.md` examples and fallback discipline

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_implement_plan_transport_bootstrap.py`
- **Acceptance criteria:**
  - Replace stale MCP examples:
    - `plan_ops__claude_envelope_extract`: use `{"agent": "...", "payload": <envelope>}`.
    - `plan_ops__write_schedule`: use `{"schedule_file": "...", "payload": <schedule_json>}`.
    - `plan_ops__parse_plan_review_triage_report`: use `{"payload": <report>, "source": "...", "findings_count": N}`.
    - `plan_ops__reconcile_batch`: use `{"repo_root": "...", ..., "payload": <envelopes_json>}`.
  - Add explicit fallback discipline: a single MCP tool failure may use CLI only for that one call, then the orchestrator must retry subsequent plan operations through MCP when `PLAN_OPS_TRANSPORT=mcp` and tools remain visible. Whole-run `cli-fallback` is only for bootstrap/session exposure failure or server disconnect.
  - Add an explicit ban on inline Python for envelope parsing; wrapper envelopes must go through `plan_ops__claude_envelope_extract` or the exact CLI fallback subcommand.
  - Preserve the existing wrapper-internal Bash exception for dispatch wrappers.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_transport_bootstrap.py -q`

### TASK-006: Fix native MCP JSON payload handling for run-log tools

- **Status:** pending
- **Priority:** critical
- **Files:**
  - `plugins/plan-executor/scripts/schemas/mcp/log_event.input.json`
  - `plugins/plan-executor/scripts/schemas/mcp/finalize_execution_log.input.json`
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `tests/scripts/test_plan_ops_mcp_schemas.py`
  - `tests/scripts/test_plan_ops_mcp_registrations.py`
- **Acceptance criteria:**
  - Preferred outcome: keep MCP structured. `log_event.fields_json` remains an object, `log_event.findings_json` remains an array, and `finalize_execution_log.rows_json` remains an array.
  - `_run_log_event` and `_run_finalize_execution_log` parse JSON only when the incoming value is a string. Native MCP dict/list values pass through without `json.loads(...)`.
  - Regression tests cover native dict/list MCP dispatch for `log_event` and `finalize_execution_log`.
  - Regression tests also prove string forms still parse through `json.loads`, preserving CLI compatibility for `--fields-json`, `--findings-json`, and `--rows-json`.
  - If real Claude Code MCP invocation rejects structured dict/list payloads despite the schema, broaden schemas to `oneOf` native-or-string forms, regenerate the registry, and add tests for both forms. Do this only after confirming the native path cannot work.
  - In either case, `SKILL.md` must tell orchestrators to pass structured MCP values, not stringified JSON, when using MCP.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_schemas.py tests/scripts/test_plan_ops_mcp_registrations.py -q`

### TASK-007: End-to-end MCP transport smoke

- **Status:** pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
- **Acceptance criteria:**
  - Extend the existing MCP e2e harness so at least one flow uses:
    - `log_event` with `fields_json` as a dict.
    - `finalize_execution_log` with `rows_json` as a list.
    - `batch_next` with `from_schedule_state: true`.
    - `claude_envelope_extract` with `payload`.
    - `write_schedule` with `payload`.
    - `reconcile_batch` with `payload`.
  - The harness must assert no captured operation call contains `plan_ops.py` for these plan operations.
  - The test should fail if inline Python is introduced for envelope parsing.
  - Prefer exercising the real stdio MCP runtime, or the closest existing harness that catches Claude Code schema/transport rejection, so native dict/list payload validation is not limited to in-process `_dispatch_registered_tool`.
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_mcp_e2e.py -q`

## Verification

Run the focused set:

```bash
venv/bin/python -m pytest \
  tests/scripts/test_plan_ops_mcp_schemas.py \
  tests/scripts/test_plan_ops_mcp_registrations.py \
  tests/scripts/test_implement_plan_transport_bootstrap.py \
  tests/scripts/test_implement_plan_mcp_e2e.py \
  -q
```

Then run the broader plan-ops tests if the focused set is green:

```bash
venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_entrypoints_tier_a.py tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py -q
```

Manual smoke after implementation:

1. Restart Claude Code or reload plugins.
2. Confirm `plan_ops__preflight` and `plan_ops__gates` are visible.
3. Invoke one real `plan_ops__log_event` with `fields_json` as an object.
4. Invoke one real `plan_ops__finalize_execution_log` with `rows_json` as an array.
5. Invoke `plan_ops__claude_envelope_extract` with `payload` as an object.
6. Confirm no inline Python or whole-run CLI fallback is needed after a successful MCP call.

## Reversion Guidance

Revert the small code change in `_args_to_payload_batch_next`, the nested `scope.*` envelope accessor change, the native-json run-log parsing changes, the MCP schema sidecars and regenerated registry block, and the SKILL.md example changes. Remove only the tests added by this plan. Do not touch unrelated run-log, schedule, or user worktree changes.
