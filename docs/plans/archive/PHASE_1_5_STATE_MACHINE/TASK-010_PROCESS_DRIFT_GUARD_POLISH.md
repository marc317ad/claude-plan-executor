# TASK-010 — Process drift-guard polish

## Goal

Expand the drift guards so the SKILL, argparse, MCP registry, schemas, and run-log process expectations stay aligned as more routing decisions move into deterministic code.

## Context

TASK-005 proved the value of drift guards: it caught the missing MCP `plan-review-route` registration and missing SKILL CLI flags immediately. Its reviewer also noted a latent risk: the argparse AST scanner is scope-blind. The process would benefit from one more layer of cheap conformance tests so future prompt/code drift is caught before a live `/implement-plan` run.

The intended outcome is one coherent polish pass, but the work should be reviewed as three independently defensible subareas: AST scanner, cross-surface conformance, and run-log process conformance.

## Verification

- Drift tests detect a missing SKILL CLI row for a new public subcommand.
- Drift tests detect a missing MCP registry/schema entry for subcommands that are supposed to be MCP-exposed.
- Drift tests detect a renamed or missing public flag, not just a changed subcommand count.
- The AST scanner no longer depends on globally unique local variable names.
- CLI-only or MCP-only exceptions live in one code constant and are asserted by tests; there is no prose-only exception list.
- A fixture run log with `commit_done` before review fails process conformance.
- A fixture run log with resumed review then commit then success passes process conformance.
- A fixture run log with a TASK-008 `test_deferred` event still requires review before commit and preserves the deferred owner metadata.
- `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_implement_plan_process_conformance.py` passes.
- `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_conformance.py` passes.

## Tasks

### TASK-010: Process drift-guard polish

- **Status:** done
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - tests/scripts/test_skill_cli_reference_drift.py (edit)
  - tests/scripts/test_plan_ops_mcp_registrations.py (edit)
  - tests/scripts/test_plan_ops_mcp_conformance.py (edit)
  - tests/scripts/test_implement_plan_process_conformance.py (create/edit)
  - plugins/plan-executor/scripts/plan_ops.py (edit)
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (edit, if needed for shared constants or generated registry)
  - plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py (edit, if needed for shared constants or check mode)
  - plugins/plan-executor/scripts/schemas/mcp/_index.json (edit, if needed)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit, if needed)
- **Dependencies:** [005, 008, 009]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_implement_plan_process_conformance.py && venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_conformance.py`
- **Acceptance criteria:**
  - `_argparse_subcommand_flags` or its replacement is scope-aware enough that reused parser variable names cannot over-collect flags from unrelated subcommands.
  - Public CLI/MCP/SKILL conformance verifies every public `plan_ops.py` subcommand has consistent representation across argparse subcommands and flags, SKILL CLI reference table, MCP `TOOL_REGISTRY` entries where applicable, and MCP input/output schema index where applicable.
  - CLI-only or MCP-only exceptions are encoded in a single Python module-level constant consumed by the conformance test and, where practical, the MCP registry/codegen path.
  - Drift detection fails when a public flag is renamed, not only when command counts change.
  - MCP registry/codegen byte stability is checked deterministically. If the codegen script does not have a read-only check mode, tests assert `render_block()` against the committed server block.
  - Process conformance tests for resumed paused runs assert review events precede commit events, `commit_done.reviewer_verdict` is populated consistently when a review occurred, and resumed runs close with a final success or paused event that supersedes earlier paused `run_end` entries.
  - Deferred-test events from TASK-008 use the frozen shape and do not weaken review-before-commit checks.
- **Reversion guidance:** Revert conformance tests and any shared exception/codegen constants together; do not leave SKILL, MCP, or argparse surfaces with divergent source-of-truth assumptions.

**Description:**
Polish the process-level drift guards around the Phase 1.5 state machine and transport surfaces. This task should make future prompt/code drift cheap to detect without adding new runtime routing behavior.

## Execution log — 20260503T162018 (paused)

Starting SHA: `c5d8b582765ab60c0584d601b3bc156eeda1b428`  → Ending SHA: `c5d8b582765ab60c0584d601b3bc156eeda1b428`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 010 | codex | gemini | needs-rework |  | Paused at post_gemini_review; Gemini reported 3 important findings and 1 minor finding. |

## Execution log — 20260503T162018 (paused)

Starting SHA: `c5d8b582765ab60c0584d601b3bc156eeda1b428`  → Ending SHA: `c5d8b582765ab60c0584d601b3bc156eeda1b428`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 010 | codex | gemini | needs-rework |  | Paused at post_gemini_review after scoped remediation and passing declared tests; Gemini returned 2 important and 2 minor findings. |
