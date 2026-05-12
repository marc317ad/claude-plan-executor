# TASK-004 — Cut over the remaining eight dispatch sites + final SKILL pass

## Goal

Cut over the remaining eight dispatch sites + final SKILL pass

## Context

Final cutover. Each of the seven non-D-Claude in-process Agent dispatch sites in SKILL.md is rewritten to invoke `plan_ops__build_agent_dispatch_prompt` via the §Canonical Agent dispatch recipe. Each of the seven `dispatch-templates.md` section bodies gains the same header note pattern TASK-002 added to Phase D-Claude. The CLAUDE.md `Subagent dispatch contract` line is refreshed to point at both MCP builder families. The smoke test assertion is widened to cover all eight phases. After this task lands, the orchestrator never reads `dispatch-templates.md` at dispatch time on any path.

## Verification

- Each of the seven non-D-Claude phase sections in SKILL.md is rewritten to invoke the §Canonical Agent dispatch recipe with the appropriate `template_id`. Specifically: §Phase 1.5-Claude → `template_id="plan-reviewer"`; §Phase 1.5a → one of the three `plan-author-*` variants depending on the dispatch path; §Phase 1-triage → `template_id="plan-review-triage"` with `context.source="analyst"`; §Phase 1.5.5 → `template_id="plan-review-triage"` with `context.source="codex"`; §Phase D.5 → `template_id="code-reviewer-d5"`; §Phase D.2a.6 → `template_id="plan-remediator-narrow"`; §Phase D.4-rescue → `template_id="plan-remediator-rescue"`.
- Each of the seven `dispatch-templates.md` section bodies gains the same header note pattern TASK-002 added to Phase D-Claude: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="<id>", ...). The orchestrator does not read this section at runtime — see SKILL.md §Canonical Agent dispatch recipe.`
- Template bodies are otherwise byte-identical (verified by `git diff --shortstat` showing only header-note additions per section).
- The smoke test assertion from TASK-002 is extended to cover all eight phase paths. The assertion now reads: "across a dry-run that exercises Phase 1.5-Claude, Phase 1.5a, Phase 1-triage, Phase 1.5.5, Phase D-Claude, Phase D.5, Phase D.2a.6, and Phase D.4-rescue, zero Bash invocations match `awk.*dispatch-templates` and zero Read calls target `dispatch-templates.md`." Phases that do not naturally fire on the smoke fixture (e.g., D.4-rescue requires a specific failure pattern) are exercised via targeted mock-driven sub-tests rather than by widening the smoke fixture itself.
- **Final SKILL doc pass:** the line in CLAUDE.md (`Subagents do not see this conversation — embed the full task block verbatim plus the dispatch-template skeleton.`) is updated to: "Subagents do not see this conversation. For wrapper dispatches use `plan_ops__build_*_dispatch_input`; for in-process Agent dispatches use `plan_ops__build_agent_dispatch_prompt`. Both render the dispatch payload from `dispatch-templates.md` in Python — the orchestrator never reads template bodies at dispatch time."
- `grep -nE 'awk.*dispatch-templates|Read.*dispatch-templates' plugins/plan-executor/skills/implement-plan/SKILL.md` returns no results outside the §Canonical Agent dispatch recipe documentation block.
- `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0.

## Tasks

### TASK-004: Cut over the remaining eight dispatch sites + final SKILL pass

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — Phase 1.5-Claude, Phase 1.5a, Phase 1-triage, Phase 1.5.5, Phase D.5, Phase D.2a.6, Phase D.4-rescue sections)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — add header notes to the seven section bodies)
  - `tests/scripts/test_implement_plan_directory_smoke.py` (edit — extend the no-awk-against-dispatch-templates assertion to cover all eight phases)
- **Dependencies:** [002, 003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Acceptance criteria:**
  - Each of the seven non-D-Claude phase sections in SKILL.md is rewritten to invoke the §Canonical Agent dispatch recipe with the appropriate `template_id`. Specifically: §Phase 1.5-Claude → `template_id="plan-reviewer"`; §Phase 1.5a → one of the three `plan-author-*` variants depending on the dispatch path; §Phase 1-triage → `template_id="plan-review-triage"` with `context.source="analyst"`; §Phase 1.5.5 → `template_id="plan-review-triage"` with `context.source="codex"`; §Phase D.5 → `template_id="code-reviewer-d5"`; §Phase D.2a.6 → `template_id="plan-remediator-narrow"`; §Phase D.4-rescue → `template_id="plan-remediator-rescue"`.
  - Each of the seven `dispatch-templates.md` section bodies gains the same header note pattern TASK-002 added to Phase D-Claude: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="<id>", ...). The orchestrator does not read this section at runtime — see SKILL.md §Canonical Agent dispatch recipe.`
  - Template bodies are otherwise byte-identical (verified by `git diff --shortstat` showing only header-note additions per section).
  - The smoke test assertion from TASK-002 is extended to cover all eight phase paths. The assertion now reads: "across a dry-run that exercises Phase 1.5-Claude, Phase 1.5a, Phase 1-triage, Phase 1.5.5, Phase D-Claude, Phase D.5, Phase D.2a.6, and Phase D.4-rescue, zero Bash invocations match `awk.*dispatch-templates` and zero Read calls target `dispatch-templates.md`." Phases that do not naturally fire on the smoke fixture (e.g., D.4-rescue requires a specific failure pattern) are exercised via targeted mock-driven sub-tests rather than by widening the smoke fixture itself.
  - **Final SKILL doc pass:** the line in CLAUDE.md (`Subagents do not see this conversation — embed the full task block verbatim plus the dispatch-template skeleton.`) is updated to: "Subagents do not see this conversation. For wrapper dispatches use `plan_ops__build_*_dispatch_input`; for in-process Agent dispatches use `plan_ops__build_agent_dispatch_prompt`. Both render the dispatch payload from `dispatch-templates.md` in Python — the orchestrator never reads template bodies at dispatch time."
  - `grep -nE 'awk.*dispatch-templates|Read.*dispatch-templates' plugins/plan-executor/skills/implement-plan/SKILL.md` returns no results outside the §Canonical Agent dispatch recipe documentation block.
  - `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0.
- **Reversion guidance:** revert the SKILL.md, dispatch-templates.md, CLAUDE.md, and smoke-test edits. The MCP tool variants from TASK-003 remain in place but are unused on the seven non-D-Claude branches; those branches fall back to the markdown-read pattern automatically.

**Description:**
Final cutover. Each of the seven non-D-Claude in-process Agent dispatch sites in SKILL.md is rewritten to invoke `plan_ops__build_agent_dispatch_prompt` via the §Canonical Agent dispatch recipe. Each of the seven `dispatch-templates.md` section bodies gains the same header note pattern TASK-002 added to Phase D-Claude. The CLAUDE.md `Subagent dispatch contract` line is refreshed to point at both MCP builder families. The smoke test assertion is widened to cover all eight phases. After this task lands, the orchestrator never reads `dispatch-templates.md` at dispatch time on any path.
