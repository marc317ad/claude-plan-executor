Initalize another session of codex to Use /mnt/d/claude-plan-executor/plugins/plan-executor/skills/implement-plan/CODEX_GEMINI_MCP.md as the process guide to run /implement-plan on docs\plans\IMPLEMENT_PLAN_AGENT_AGNOSTIC --task-ids <2 through 8> in the current repo.

You are the orchestrator, not the implementer. Monitor the progress of your agent, do not intervene unless absolutely necessary. Run the tasks one at a time in individual sessions sequentially. When they pass and commit, run the next one, and continue until the plan has been completed.

Before starting, verify:
  1. The required plan_ops MCP tools are visible.
  2. `gemini --approval-mode plan -o json -p 'Return {"ok":true}'` works.
  3. The working tree state is acceptable for the plan scope.

Prompt your agent with necessary context from above and then to run:
  docs\plans\IMPLEMENT_PLAN_AGENT_AGNOSTIC --task-ids <TASK_IDS>

Example:

  Use /mnt/d/claude-plan-executor/plugins/plan-executor/skills/implement-plan/CODEX_GEMINI_MCP.md as the process guide to run /implement-plan on docs\plans\IMPLEMENT_PLAN_AGENT_AGNOSTIC --task-ids 002 in the current repo.

  Important:
  - Read the process guide from /mnt/d/claude-plan-executor/plugins/plan-executor/skills/implement-plan/CODEX_GEMINI_MCP.md.
  - Treat docs/plans/MY_PLAN, schedule files, source edits, tests, commits, and run logs as belonging to the current repo, not /mnt/d/claude-plan-executor.
  - Do not assume this repo has a local plugins/ directory.
  - Use the plan_ops MCP tools for orchestration.
  - Use the dispatch wrappers from /mnt/d/claude-plan-executor/plugins/plan-executor/scripts when wrapper subprocesses are needed.
  - Use plan_codex_dispatch.py for implementation.
  - Use plan_gemini_dispatch.py for plan review and code review.
  - The Gemini wrapper has been updated to support local Gemini CLI OAuth; do not bypass it with direct Gemini CLI unless diagnosing a wrapper failure.
  - If Gemini review fails, diagnose/fix the wrapper or pause; do not silently commit as reviewer=none.

Don't interrupt it unless it goes rogue or fails. If it prompts you for guidance, the path should always be toward the most correct and complete implementation. Do not cut corners, no band-aids and code debt. If gemini fails due to api restrictions, use gemini --model with the next lower model (gemini-3-pro, gemini-2.5-pro, etc..).
