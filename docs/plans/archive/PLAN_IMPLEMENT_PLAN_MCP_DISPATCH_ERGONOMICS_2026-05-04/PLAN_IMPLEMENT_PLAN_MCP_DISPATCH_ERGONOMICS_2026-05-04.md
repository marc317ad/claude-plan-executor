# PLAN - /implement-plan MCP Dispatch Ergonomics

**Status:** Pending
**Created:** 2026-05-04
**Base branch:** main
**Investigation:** Codex local reproduction, with Claude CLI review requested before finalization.

## Goal

Tighten the `/implement-plan` Claude-wrapper dispatch path so an orchestrator with `plan_ops__*` MCP tools visible uses MCP for all plan-operation steps that have MCP equivalents, while still using Bash for the actual wrapper subprocess invocation.

The specific operator failure to prevent is:

1. `plan_ops__build_claude_dispatch_input` is called with an `output` path.
2. The tool returns the full dispatch JSON in the MCP response, so the orchestrator assumes the file was not written or cannot be piped.
3. The orchestrator switches to `plan_ops.py build-claude-dispatch-input | plan_claude_dispatch.py run --input -`.
4. Later wrapper envelopes are read directly instead of normalized through `plan_ops__claude_envelope_extract`.

## Findings

### 1. Wrapper execution genuinely remains Bash

`plan_claude_dispatch.py run` is a subprocess wrapper around `claude -p`. MCP cannot directly pipe a tool result into Bash stdin in the same way a shell pipeline can. The correct MCP-first sequence is therefore:

1. Build dispatch input with `plan_ops__build_claude_dispatch_input`.
2. Materialize that returned JSON to a file or use the tool's `output` side effect.
3. Run `plan_claude_dispatch.py run --input <file>` in Bash.
4. Normalize the wrapper envelope with `plan_ops__claude_envelope_extract`.

The actual wrapper dispatch is out of scope for MCP conversion.

### 2. `build_claude_dispatch_input` file-output behavior is confusing, not absent

Local CLI reproduction:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py build-claude-dispatch-input \
  --plan-file docs/plans/wrapper_autoclean_authorization/wrapper_autoclean_authorization/TASK-001_build_claude_dispatch_input.md \
  --task-id 001 \
  --variant default \
  --repo-root /mnt/d/claude-plan-executor \
  --output /tmp/bcdi-cli-test.json \
  --json
```

Result: `/tmp/bcdi-cli-test.json` was written and CLI stdout was suppressed.

Local MCP tool reproduction:

```json
{
  "plan_file": "/mnt/d/claude-plan-executor/docs/plans/wrapper_autoclean_authorization/wrapper_autoclean_authorization/TASK-001_build_claude_dispatch_input.md",
  "task_id": "001",
  "variant": "default",
  "repo_root": "/mnt/d/claude-plan-executor",
  "output": "/tmp/bcdi-mcp-tool-test.json"
}
```

Result: `/tmp/bcdi-mcp-tool-test.json` was written, but the MCP tool response still returned the full dispatch envelope. In the in-process MCP server path, the pure-core response can also carry internal `__plan_ops_*` markers unless the server strips them before returning public structured content.

That means Claude's observation is understandable but incomplete. The current MCP surface preserves the file side effect, but it gives no explicit acknowledgement such as `{ok:true, output:"..."}` and still displays the large JSON payload. In some MCP dispatch paths it can additionally expose internal stdout-suppression markers. The side effect is easy to miss, and the operator naturally reaches for a shell pipeline.

Relevant code:

- `plugins/plan-executor/scripts/plan_ops.py::_bcdi_to_envelope_result` writes the file when `output != "-"`.
- `plugins/plan-executor/scripts/plan_ops.py::_emit_or_die` suppresses CLI stdout when `__plan_ops_stdout_suppressed__` is set.
- `plugins/plan-executor/scripts/plan_ops_mcp_server.py::_dispatch_registered_tool` returns the pure-core result directly; it does not model CLI stdout suppression as a different structured MCP response and should defensively strip internal `__plan_ops_*` markers.
- `tests/scripts/fixtures/plan_ops_pure_core/tier_b/build-claude-dispatch-input__happy_file_mode.expected.json` covers CLI stdout suppression and expected file write.

### 3. `claude_envelope_extract` has the right MCP shape, but the skill relies on prose discipline

`plugins/plan-executor/scripts/schemas/mcp/claude_envelope_extract.input.json` requires:

```json
{
  "agent": "plan-implementer",
  "payload": {}
}
```

`plugins/plan-executor/skills/implement-plan/SKILL.md` already states:

- "Never write inline Python for envelope parsing."
- "Wrapper envelopes must be normalized with `plan_ops__claude_envelope_extract` over MCP."
- "Every Claude-wrapper dispatch ... is invoke -> extract -> route."

There is also e2e smoke coverage in `tests/scripts/test_implement_plan_mcp_e2e.py` that calls `claude_envelope_extract` with a native MCP `payload`.

The remaining gap is enforcement at the most tempting dispatch sites. The skill still contains CLI-pipeline phrasing for `build-claude-dispatch-input` and Phase D role-swap examples, but does not give an MCP-native "write temp file then Bash wrapper then extract" recipe with explicit checks.

## Decisions

1. **Do not try to make MCP pipe to Bash.** Treat Bash wrapper execution as a necessary process boundary.
2. **Make the MCP builder response unambiguous.** When `output` is a real path, the MCP response should expose a small acknowledgement with the output path and suppress or move the full envelope out of the primary response. CLI behavior must remain byte-for-byte compatible where tests require stdout suppression.
3. **Prefer `--input <file>` over `--input -` in MCP mode.** This avoids the "tool result cannot be piped" trap while keeping wrapper execution in Bash.
4. **Require extractor after every Claude-wrapper dispatch.** Strengthen tests around `SKILL.md` and, where possible, runner/provider code so wrapper envelopes cannot be routed by directly reading `.status` / `.result`.
5. **Make `TASK-001` red-before-green.** The acknowledgement tests intentionally fail before `TASK-002`; avoid parallelizing those tasks.

## Scope

In scope:

- `plugins/plan-executor/scripts/plan_ops.py`
- `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
- `plugins/plan-executor/scripts/schemas/mcp/build_claude_dispatch_input.output.json`
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- MCP conformance / implement-plan transport tests under `tests/scripts/`

Out of scope:

- Replacing `plan_claude_dispatch.py run` with MCP.
- Changing `plan_claude_dispatch.py` wrapper envelope semantics.
- Changing Codex or Gemini wrapper dispatch.
- Reworking the full `/implement-plan` state machine.
- Adding explicit acknowledgement envelopes to Codex/Gemini builder tools. A generic internal-marker strip in the MCP server should protect those tools from marker leaks, but matching ergonomic acknowledgement shapes can be handled by a sibling follow-up if needed.

## Tasks

### TASK-001: Add reproduction tests for MCP builder file-output acknowledgement

- **Status:** done
- **Priority:** high
- **Files:**
  - `tests/scripts/test_plan_ops_mcp_conformance.py`
  - `tests/scripts/test_plan_ops_mcp_schemas.py`
  - `tests/scripts/fixtures/plan_ops_pure_core/tier_b/build-claude-dispatch-input__happy_file_mode.expected.json`
- **Acceptance criteria:**
  - Add or extend a test that calls `plan_ops__build_claude_dispatch_input` with `output` set to a temp file.
  - Assert the file is written under MCP.
  - Assert the MCP structured response contains an explicit file-output acknowledgement such as `output_written: true` and `output: "<path>"`.
  - Assert no public MCP response contains keys beginning with `__plan_ops_`.
  - Assert CLI `--output <path> --json` still writes the file and emits no stdout.
  - Assert output schema documents the acknowledgement fields.
  - This task is expected to fail before TASK-002 and must not be implemented in parallel with TASK-002 by a separate worker.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_schemas.py -k "build_claude_dispatch_input or stdout_suppressed or output"`

### TASK-002: Implement MCP-only acknowledgement for file-output builder calls

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `plugins/plan-executor/scripts/schemas/mcp/build_claude_dispatch_input.output.json`
- **Acceptance criteria:**
  - Preserve CLI behavior: file-output mode writes the dispatch JSON to the requested path and suppresses stdout.
  - Preserve default MCP behavior when `output` is omitted or `"-"`: return the dispatch envelope as today.
  - In MCP mode with `output != "-"`, return a small structured acknowledgement such as `{ok:true, output_written:true, output:"<path>"}` instead of the dispatch envelope.
  - Add a generic MCP-server defensive strip for public results so no internal `__plan_ops_*` marker can leak from any plan_ops pure core.
  - Audit in-tree call sites and tests for code that reads the dispatch envelope out of the MCP response while also setting `output`; migrate those sites to read the file or to omit `output`.
  - Keep side-effect conformance tests green.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_schemas.py -k "build_claude_dispatch_input or build-claude-dispatch-input"`

### TASK-003: Rewrite SKILL.md Claude-wrapper recipes for MCP mode

- **Status:** Pending
- **Priority:** critical
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
  - `tests/scripts/test_implement_plan_transport_bootstrap.py`
- **Acceptance criteria:**
  - Replace or qualify `Pipe stdout into plan_claude_dispatch.py run --input -` wording so it applies only to CLI fallback.
  - Add a canonical MCP-mode recipe:
    1. Call `plan_ops__build_claude_dispatch_input` with `output:"<tmp dispatch input>"`.
    2. Verify the tool response says the output was written.
    3. Run `plan_claude_dispatch.py run --input <tmp dispatch input>` in Bash.
    4. Feed the wrapper JSON to `plan_ops__claude_envelope_extract` with native `payload`.
    5. Route only the normalized extractor output.
  - Update Phase D role-swap, bounded remediation, narrow remediation, and analyst/implementer dispatch references to point at the same recipe instead of ad hoc `--input -` wording.
  - Add drift tests that fail if SKILL.md says to pipe MCP builder output directly into Bash or routes Claude wrapper envelopes without `plan_ops__claude_envelope_extract`.
  - Confirm every named test file exists before implementation; if a transport test has been renamed, update this plan and scope the actual file.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py tests/scripts/test_implement_plan_transport_bootstrap.py -k "claude_envelope_extract or skill or build_claude_dispatch_input"`

### TASK-004: Add dispatch-site extractor ordering regression tests

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_implement_plan_runner_phase_d.py`
  - `tests/scripts/test_implement_plan_provider_adapters.py`
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
- **Acceptance criteria:**
  - Cover the canonical sequence `build_claude_dispatch_input -> plan_claude_dispatch.py run -> claude_envelope_extract -> route` for at least implementer and one remediation path.
  - Include the MCP file-output path where `build_claude_dispatch_input` returns a slim acknowledgement, and assert the runner does not mistake that acknowledgement for a wrapper envelope.
  - Verify routing consumes normalized extractor fields (`status`, `outcome`, `scope_violation`, `error`) rather than raw wrapper `.status` / `.result`.
  - Include a non-`ok` wrapper status case and assert commit is forbidden before any Phase D route can mark the task committable.
  - Include a `scope_violation` case and assert the normalized `scope_violation` field drives pause/reconcile behavior.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_phase_d.py tests/scripts/test_implement_plan_provider_adapters.py tests/scripts/test_implement_plan_mcp_e2e.py -k "claude_envelope_extract or remediation or scope_violation"`

### TASK-005: Run focused audit and document operator guidance

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `docs/plans/PLAN_IMPLEMENT_PLAN_MCP_DISPATCH_ERGONOMICS_2026-05-04.md`
- **Acceptance criteria:**
  - Run `plan_ops__audit` after the skill/tool changes.
  - Record whether any audit finding remains advisory or blocking.
  - Add a numbered operator rule in the skill: in MCP mode, do not switch the whole run to CLI fallback because a builder result cannot be piped; materialize the dispatch input, run the Bash wrapper with `--input <file>`, then continue with MCP for extraction and later plan operations.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_implement_plan_mcp_e2e.py && venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_schemas.py`
2. `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py tests/scripts/test_implement_plan_transport_bootstrap.py`
3. `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_phase_d.py tests/scripts/test_implement_plan_provider_adapters.py`
4. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`

Manual smoke:

1. Call `plan_ops__build_claude_dispatch_input` with `output` set to `/tmp/dispatch-input.json`.
2. Confirm `/tmp/dispatch-input.json` exists and the MCP response explicitly acknowledges the write.
3. Run `plan_claude_dispatch.py run --input /tmp/dispatch-input.json` against a harmless fixture or stubbed backend.
4. Call `plan_ops__claude_envelope_extract` with the wrapper envelope as native `payload`.
5. Confirm no step uses inline Python or ad hoc JSON parsing.

## Claude CLI Review

Ran `claude -p` against this plan before finalization. Claude agreed the main diagnosis is technically sound:

- Wrapper execution remains a Bash process boundary.
- The skill's `--input -` wording is a real source of drift in MCP mode.
- `claude_envelope_extract` is the right mandatory normalization step.

Claude also identified one important strengthening: MCP file-output mode can leak internal `__plan_ops_*` markers because the server returns pure-core dictionaries directly. This plan now includes a generic marker-strip requirement, a red-before-green builder acknowledgement test, a callsite audit for any code that reads the dispatch envelope from a file-output MCP response, and an explicit regression that a slim acknowledgement is not treated as a wrapper envelope.
