### TASK-001: Render reviewer prompt

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Acceptance criteria:**
  - The renderer returns the code-reviewer Agent prompt.
  - The Scope line lists the changed files from the wrapper context.
  - Description and acceptance criteria are copied from the selected task block.

**Description:**
Implement the first Agent prompt renderer variant for Phase D-Claude review.
Keep the output stable so the orchestrator cutover can compare exact bytes.

**Reversion guidance:** Revert the renderer and schema additions.
