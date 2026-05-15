# TASK-005 — Delete §Claude wrapper dispatch recipe SKILL section, banner the README, document script-runner-only residency

## Goal

Delete §Claude wrapper dispatch recipe SKILL section, banner the README, document script-runner-only residency

## Context

Auto-decomposed child for TASK-005. See the source plan for broader context.

## Verification

- SKILL.md's §Claude wrapper dispatch recipe (canonical) section (currently around lines 97–112) is deleted in full. The orchestrator no longer references it (verified by TASK-002/003/004). Confirm with `grep -n "Claude wrapper dispatch recipe" plugins/plan-executor/skills/implement-plan/SKILL.md` returning empty.
- `README_claude_dispatch.md` gains a top-of-file scope banner explaining (a) the `/implement-plan` SKILL/MCP orchestration NO LONGER calls this wrapper as of 2026-05-14 (in-process Agent dispatch via PLAN_DEPRECATE_CLAUDE_CLI_2026-05-14), (b) the wrapper REMAINS in active use by the standalone `implement_plan.py` script runner's `ClaudeProvider`, and (c) post-Anthropic-`claude -p` subscription deprecation, the script-runner Claude path becomes paid usage — operators are advised to switch to `--implementer codex` if cost is a concern. The banner explicitly does NOT mark the script as deprecated; only the SKILL/MCP usage is deprecated.
- `plan_claude_dispatch.py run` continues to function unchanged. No shim, no error exit, no `claude -p` removal.
- All existing `tests/scripts/test_claude_dispatch*.py` cases stay live and green — the wrapper subprocess path remains a supported transport for the script runner.
- Run `/implement-plan` end-to-end against a small Claude-only plan; assert (via `pgrep -af 'claude .*-p'` during the run, captured into the run log) that no `claude -p` subprocess spawns at any phase. (This proves the SKILL/MCP cutover is complete; the script runner is exercised separately by its own existing test suite.)
- Optional: add an audit-only `--called-from {orchestrator,script_runner}` flag to `plan_claude_dispatch.py run` and have `implement_plan.py:ClaudeProvider` pass `--called-from script_runner`. This makes future deprecation of the script-runner Claude path visible in run logs without affecting behavior. Skip if it bloats the diff.

## Tasks

### TASK-005: Delete §Claude wrapper dispatch recipe SKILL section, banner the README, document script-runner-only residency

- **Status:** Pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (delete §Claude wrapper dispatch recipe (canonical) section entirely; cross-references already removed by TASK-002/003/004)
  - `plugins/plan-executor/scripts/README_claude_dispatch.md` (add a deprecation/scope banner at the top)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (NO behavior change to `run` — it stays functional for the script runner; optionally add a `--called-from` audit field if cheap)
  - `tests/scripts/test_claude_dispatch*.py` (NO skip — the wrapper still serves the script-runner caller; tests stay live)
- **Dependencies:** [002, 003, 004]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch tests/scripts/test_plan_ops_build_agent_dispatch_prompt tests/scripts/test_dispatch_cleanup`
- **Acceptance criteria:**
  - SKILL.md's §Claude wrapper dispatch recipe (canonical) section (currently around lines 97–112) is deleted in full. The orchestrator no longer references it (verified by TASK-002/003/004). Confirm with `grep -n "Claude wrapper dispatch recipe" plugins/plan-executor/skills/implement-plan/SKILL.md` returning empty.
  - `README_claude_dispatch.md` gains a top-of-file scope banner explaining (a) the `/implement-plan` SKILL/MCP orchestration NO LONGER calls this wrapper as of 2026-05-14 (in-process Agent dispatch via PLAN_DEPRECATE_CLAUDE_CLI_2026-05-14), (b) the wrapper REMAINS in active use by the standalone `implement_plan.py` script runner's `ClaudeProvider`, and (c) post-Anthropic-`claude -p` subscription deprecation, the script-runner Claude path becomes paid usage — operators are advised to switch to `--implementer codex` if cost is a concern. The banner explicitly does NOT mark the script as deprecated; only the SKILL/MCP usage is deprecated.
  - `plan_claude_dispatch.py run` continues to function unchanged. No shim, no error exit, no `claude -p` removal.
  - All existing `tests/scripts/test_claude_dispatch*.py` cases stay live and green — the wrapper subprocess path remains a supported transport for the script runner.
  - Run `/implement-plan` end-to-end against a small Claude-only plan; assert (via `pgrep -af 'claude .*-p'` during the run, captured into the run log) that no `claude -p` subprocess spawns at any phase. (This proves the SKILL/MCP cutover is complete; the script runner is exercised separately by its own existing test suite.)
  - Optional: add an audit-only `--called-from {orchestrator,script_runner}` flag to `plan_claude_dispatch.py run` and have `implement_plan.py:ClaudeProvider` pass `--called-from script_runner`. This makes future deprecation of the script-runner Claude path visible in run logs without affecting behavior. Skip if it bloats the diff.
- **Reversion guidance:** Restore SKILL.md's §Claude wrapper dispatch recipe section from history; revert the README banner; revert any optional `--called-from` flag.

**Description:**
Delete §Claude wrapper dispatch recipe SKILL section, banner the README, document script-runner-only residency. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-005. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
