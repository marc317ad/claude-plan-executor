# TASK-003 — Rewrite SKILL.md Claude-wrapper recipes for MCP mode

## Goal

Rewrite SKILL.md Claude-wrapper recipes for MCP mode

## Context

Auto-decomposed child for TASK-003. See the source plan for broader context.

## Verification

- Replace or qualify `Pipe stdout into plan_claude_dispatch.py run --input -` wording so it applies only to CLI fallback.
- Add a canonical MCP-mode recipe: 1. Call `plan_ops__build_claude_dispatch_input` with `output:"<tmp dispatch input>"`. 2. Verify the tool response says the output was written. 3. Run `plan_claude_dispatch.py run --input <tmp dispatch input>` in Bash. 4. Feed the wrapper JSON to `plan_ops__claude_envelope_extract` with native `payload`. 5. Route only the normalized extractor output.
- Update Phase D role-swap, bounded remediation, narrow remediation, and analyst/implementer dispatch references to point at the same recipe instead of ad hoc `--input -` wording.
- Add drift tests that fail if SKILL.md says to pipe MCP builder output directly into Bash or routes Claude wrapper envelopes without `plan_ops__claude_envelope_extract`.
- Confirm every named test file exists before implementation; if a transport test has been renamed, update this plan and scope the actual file.

## Tasks

### TASK-003: Rewrite SKILL.md Claude-wrapper recipes for MCP mode

- **Status:** done
- **Priority:** critical
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
  - `tests/scripts/test_implement_plan_transport_bootstrap.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py tests/scripts/test_implement_plan_transport_bootstrap.py -k "claude_envelope_extract or skill or build_claude_dispatch_input"`
- **Acceptance criteria:**
  - Replace or qualify `Pipe stdout into plan_claude_dispatch.py run --input -` wording so it applies only to CLI fallback.
  - Add a canonical MCP-mode recipe: 1. Call `plan_ops__build_claude_dispatch_input` with `output:"<tmp dispatch input>"`. 2. Verify the tool response says the output was written. 3. Run `plan_claude_dispatch.py run --input <tmp dispatch input>` in Bash. 4. Feed the wrapper JSON to `plan_ops__claude_envelope_extract` with native `payload`. 5. Route only the normalized extractor output.
  - Update Phase D role-swap, bounded remediation, narrow remediation, and analyst/implementer dispatch references to point at the same recipe instead of ad hoc `--input -` wording.
  - Add drift tests that fail if SKILL.md says to pipe MCP builder output directly into Bash or routes Claude wrapper envelopes without `plan_ops__claude_envelope_extract`.
  - Confirm every named test file exists before implementation; if a transport test has been renamed, update this plan and scope the actual file.
- **Reversion guidance:** none

**Description:**

## Execution log — 20260505T020741 (paused)

Starting SHA: `8532454b36bd055cab8da841ac5824d2e427e06b`  → Ending SHA: `8532454b36bd055cab8da841ac5824d2e427e06b`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003 | claude | codex | needs-rework [remediation] (D.5-binding) | (paused, no commit) | Bounded remediation applied dispatch_context note; second Codex review surfaced new finding on AC4 drift test thoroughness (only enumerates 4 sections, lacks global negative scan); D.5 concurred binding needs-rework. |
