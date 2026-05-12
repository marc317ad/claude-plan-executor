# TASK-002 — Cut over Phase D-Claude in SKILL + dispatch-templates

## Goal

Cut over Phase D-Claude in SKILL + dispatch-templates

## Context

Cut over the Phase D-Claude dispatch site from the markdown-read pattern to the new MCP tool. Update both branches of SKILL.md §Phase D.1 (the `claude_only=true` route and the Codex-implemented Claude-cross-review route) to use `plan_ops__build_agent_dispatch_prompt`. Add a new SKILL.md section `## Canonical Agent dispatch recipe` mirroring the existing `## Claude wrapper dispatch recipe (canonical)` so the pattern is documented once and referenced from each phase. Add a header note to the Phase D-Claude section in `dispatch-templates.md` directing readers to the SKILL recipe. The template body is unchanged. A new smoke-test assertion confirms the orchestrator does not read `dispatch-templates.md` at dispatch time on the cut-over path.

## Verification

- SKILL.md gains a new section `## Canonical Agent dispatch recipe` immediately after the existing `## Claude wrapper dispatch recipe (canonical)`. The new section documents the five-step recipe for in-process Agent dispatches in the same numbered-list shape: (1) call `plan_ops__build_agent_dispatch_prompt` with `template_id` + `context`, (2) assert `ok == true`, (3) extract `agent`, `model`, `prompt`, (4) dispatch `Agent(subagent_type=agent, model=model, prompt=prompt)`, (5) parse the result via the appropriate `plan_ops__parse_*` tool (no ad-hoc parsing).
- The new section explicitly states: "Do NOT read `dispatch-templates.md` from Bash or `Read` at dispatch time. The MCP tool reads it on the orchestrator's behalf and returns the rendered string. The template file remains the canonical authoring location."
- SKILL.md §Phase D.1 — both branches that today instruct the orchestrator to render the Phase D-Claude template manually — are rewritten to invoke the new recipe. Specifically: line 520 (`claude_only=true` → `code-reviewer` Agent) and the Codex-implemented Claude-cross-review branch (the `Phase D.1` table row for `Codex implemented, claude_only=false`).
- SKILL.md line 49 (`Dispatch prompts must be self-contained. Read ${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md for the templates.`) is updated to note the asymmetry resolution: wrapper dispatches use the wrapper-input builders; in-process Agent dispatches use the new agent-prompt builder. The `Read dispatch-templates.md` instruction is preserved as a fallback for direct inspection but explicitly NOT the runtime path.
- `dispatch-templates.md` Phase D-Claude section (line 611) gains a header note immediately under the `##` heading: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="code-reviewer-d-claude", ...). The orchestrator does not read this section at runtime — see SKILL.md §Canonical Agent dispatch recipe.`
- The Phase D-Claude template body (lines 619-635) is otherwise byte-identical (verified by `git diff --shortstat` showing only the new header note line + the heading addition).
- `tests/scripts/test_implement_plan_directory_smoke.py` gains a new assertion `test_phase_d_claude_uses_mcp_render_path` (or similar): the smoke test runs a dry-run `/implement-plan` against a fixture plan with one Codex-implemented task, captures the orchestrator's tool-use trace, and asserts (a) at least one `plan_ops__build_agent_dispatch_prompt` call with `template_id == "code-reviewer-d-claude"` is recorded, (b) zero Bash invocations matching `awk.*dispatch-templates` or `Read` calls against `dispatch-templates.md` are recorded after the Phase B implementer commit. If the smoke harness does not yet expose a tool-use trace, add the minimal hook needed (the existing harness already captures wrapper subprocess invocations — extend that to MCP tool calls).
- `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0.

## Tasks

### TASK-002: Cut over Phase D-Claude in SKILL + dispatch-templates

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — Phase D.1 sections, plus add `## Canonical Agent dispatch recipe` near line 101)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — add header note to Phase D-Claude section)
  - `tests/scripts/test_implement_plan_directory_smoke.py` (edit — add assertion that no `awk` against `dispatch-templates.md` runs in the dry-run trace)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Acceptance criteria:**
  - SKILL.md gains a new section `## Canonical Agent dispatch recipe` immediately after the existing `## Claude wrapper dispatch recipe (canonical)`. The new section documents the five-step recipe for in-process Agent dispatches in the same numbered-list shape: (1) call `plan_ops__build_agent_dispatch_prompt` with `template_id` + `context`, (2) assert `ok == true`, (3) extract `agent`, `model`, `prompt`, (4) dispatch `Agent(subagent_type=agent, model=model, prompt=prompt)`, (5) parse the result via the appropriate `plan_ops__parse_*` tool (no ad-hoc parsing).
  - The new section explicitly states: "Do NOT read `dispatch-templates.md` from Bash or `Read` at dispatch time. The MCP tool reads it on the orchestrator's behalf and returns the rendered string. The template file remains the canonical authoring location."
  - SKILL.md §Phase D.1 — both branches that today instruct the orchestrator to render the Phase D-Claude template manually — are rewritten to invoke the new recipe. Specifically: line 520 (`claude_only=true` → `code-reviewer` Agent) and the Codex-implemented Claude-cross-review branch (the `Phase D.1` table row for `Codex implemented, claude_only=false`).
  - SKILL.md line 49 (`Dispatch prompts must be self-contained. Read ${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md for the templates.`) is updated to note the asymmetry resolution: wrapper dispatches use the wrapper-input builders; in-process Agent dispatches use the new agent-prompt builder. The `Read dispatch-templates.md` instruction is preserved as a fallback for direct inspection but explicitly NOT the runtime path.
  - `dispatch-templates.md` Phase D-Claude section (line 611) gains a header note immediately under the `##` heading: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="code-reviewer-d-claude", ...). The orchestrator does not read this section at runtime — see SKILL.md §Canonical Agent dispatch recipe.`
  - The Phase D-Claude template body (lines 619-635) is otherwise byte-identical (verified by `git diff --shortstat` showing only the new header note line + the heading addition).
  - `tests/scripts/test_implement_plan_directory_smoke.py` gains a new assertion `test_phase_d_claude_uses_mcp_render_path` (or similar): the smoke test runs a dry-run `/implement-plan` against a fixture plan with one Codex-implemented task, captures the orchestrator's tool-use trace, and asserts (a) at least one `plan_ops__build_agent_dispatch_prompt` call with `template_id == "code-reviewer-d-claude"` is recorded, (b) zero Bash invocations matching `awk.*dispatch-templates` or `Read` calls against `dispatch-templates.md` are recorded after the Phase B implementer commit. If the smoke harness does not yet expose a tool-use trace, add the minimal hook needed (the existing harness already captures wrapper subprocess invocations — extend that to MCP tool calls).
  - `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0.
- **Reversion guidance:** revert the SKILL.md and dispatch-templates.md edits and the new smoke-test assertion. The new MCP tool from TASK-001 remains in place but is no longer invoked from the SKILL. The orchestrator falls back to the markdown-read pattern automatically (no code change needed) because TASK-001 left the Read path intact.

**Description:**
Cut over the Phase D-Claude dispatch site from the markdown-read pattern to the new MCP tool. Update both branches of SKILL.md §Phase D.1 (the `claude_only=true` route and the Codex-implemented Claude-cross-review route) to use `plan_ops__build_agent_dispatch_prompt`. Add a new SKILL.md section `## Canonical Agent dispatch recipe` mirroring the existing `## Claude wrapper dispatch recipe (canonical)` so the pattern is documented once and referenced from each phase. Add a header note to the Phase D-Claude section in `dispatch-templates.md` directing readers to the SKILL recipe. The template body is unchanged. A new smoke-test assertion confirms the orchestrator does not read `dispatch-templates.md` at dispatch time on the cut-over path.
