# TASK-002 — Implement MCP-only acknowledgement for file-output builder calls

## Goal

Implement MCP-only acknowledgement for file-output builder calls

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- Preserve CLI behavior: file-output mode writes the dispatch JSON to the requested path and suppresses stdout.
- Preserve default MCP behavior when `output` is omitted or `"-"`: return the dispatch envelope as today.
- In MCP mode with `output != "-"`, return a small structured acknowledgement such as `{ok:true, output_written:true, output:"<path>"}` instead of the dispatch envelope.
- Add a generic MCP-server defensive strip for public results so no internal `__plan_ops_*` marker can leak from any plan_ops pure core.
- Audit in-tree call sites and tests for code that reads the dispatch envelope out of the MCP response while also setting `output`; migrate those sites to read the file or to omit `output`.
- Keep side-effect conformance tests green.

## Tasks

### TASK-002: Implement MCP-only acknowledgement for file-output builder calls

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `plugins/plan-executor/scripts/schemas/mcp/build_claude_dispatch_input.output.json`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_schemas.py -k "build_claude_dispatch_input or build-claude-dispatch-input"`
- **Acceptance criteria:**
  - Preserve CLI behavior: file-output mode writes the dispatch JSON to the requested path and suppresses stdout.
  - Preserve default MCP behavior when `output` is omitted or `"-"`: return the dispatch envelope as today.
  - In MCP mode with `output != "-"`, return a small structured acknowledgement such as `{ok:true, output_written:true, output:"<path>"}` instead of the dispatch envelope.
  - Add a generic MCP-server defensive strip for public results so no internal `__plan_ops_*` marker can leak from any plan_ops pure core.
  - Audit in-tree call sites and tests for code that reads the dispatch envelope out of the MCP response while also setting `output`; migrate those sites to read the file or to omit `output`.
  - Keep side-effect conformance tests green.
- **Reversion guidance:** none

**Description:**
Implement the green half of the MCP dispatch-input acknowledgement change. The task updates the plan-ops pure core, MCP server result shaping, and public MCP output schema so file-output builder calls keep their side effect while returning a small acknowledgement instead of a full dispatch envelope.

## Execution log — 20260505T015332 (success)

Starting SHA: `a2a0546ecc568931c9657edf3ccd164f67019261`  → Ending SHA: `f9fa5d2907b7a7e6e036045db044172a5c2151fc`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-002 | claude | codex | needs-rework [disagreement] | f9fa5d2 | D.5 (sonnet) overruled Codex needs-rework: _error_tool_result already calls _public_result first, so the claimed marker-strip gap does not exist. |
