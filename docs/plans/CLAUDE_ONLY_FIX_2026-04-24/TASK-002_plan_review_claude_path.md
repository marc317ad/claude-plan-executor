# TASK-002 — Plan review Claude path

## Goal

Plan review Claude path

## Context

Replace the wrapper shell-out in Phase 1.5 with an Agent dispatch when `claude_only=true`. New `plan-reviewer` agent (Sonnet) produces the same envelope shape as the wrapper; `parse-plan-review-report` gains `--from-claude` to strip the envelope wrapping. Verdict-routing ladder (`approved | approved-with-notes | needs-replan`), `--codex-plan-review-binding` (now mutex with `--claude-only` per TASK-001), the auto-revise `plan-author` path, and the `--allow-gaps` demotion all keep working unchanged because they consume the parsed verdict, not the dispatch mechanism.

The `plan-reviewer` agent's prompt mirrors the Codex prompt body 1:1 except for: (a) replacing Codex-specific calibration ("Codex historically overuses `needs-rework`" guidance from `dispatch-templates.md:387` is dropped — Sonnet's calibration is different), (b) "You do NOT have the Agent tool" constraint, (c) inputs are passed as Agent prompt placeholders rather than CLI args. The output schema is identical to `codex_plan_review_schema.json`.

## Verification

- File `plugins/plan-executor/agents/plan-reviewer.md` exists with frontmatter `model: sonnet`, `tools: Read, Grep, Glob, Bash`, and a system prompt that mirrors the Codex plan-review prompt structure. Output contract is one fenced ```json block conforming to `codex_plan_review_schema.json` (`{plan_file, verdict ∈ {approved, approved-with-notes, needs-replan}, findings[], notes[], schedule_ok, summary}`). The prompt explicitly states "You do NOT have the Agent tool" and forbids editing the plan or schedule.
- `dispatch-templates.md` gains a new section "Phase 1.5-Claude — plan-reviewer dispatch (claude_only path)" that renders the Agent dispatch with `subagent_type: "plan-reviewer", model: "sonnet"`. The template embeds the schedule path, plan directory path, repo root, `findings_count` placeholder, and the `--allow-gaps` demotion clause when applicable.
- `SKILL.md` §Phase 1.5 grows a route-switch at the top: when `claude_only=true`, dispatch via the Phase 1.5-Claude template; otherwise, dispatch via the existing wrapper path. Both branches feed the same `parse-plan-review-report` parser; the only difference is the dispatch mechanism and the `reviewer` field in the run-log events.
- `SKILL.md` §Phase 1.5 retires the `codex_available=false → plan_review_skipped {reason:"codex_unavailable"}` skip clause. That case now flows through `claude_only=true` instead (per TASK-001's binding) and dispatches the Claude reviewer.
- `plan_ops.py parse-plan-review-report` accepts a new `--from-claude` flag. When set, the parser treats stdin as the bare `parsed` payload (matching `codex_plan_review_schema.json`) instead of expecting the wrapper envelope `{task_id, subcommand, outcome, codex_exit_code, parsed}`. All existing callers continue to work without the flag.
- Run-log events on the Claude path: `plan_review_start {reviewer:"claude", plan_file:"<basename>"}`, `plan_review_done {reviewer:"claude", verdict, findings_count, summary}`. The Codex path remains `reviewer:"codex"` unchanged.
- Tests added to `tests/scripts/test_plan_ops.py`:
- The test command `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "parse_plan_review_report"` returns 0.

## Tasks

### TASK-002: Plan review Claude path

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/agents/plan-reviewer.md (create)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (edit)
  - plugins/plan-executor/scripts/plan_ops.py (edit)
  - tests/scripts/test_plan_ops.py (edit)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "parse_plan_review_report"`
- **Acceptance criteria:**
  - File `plugins/plan-executor/agents/plan-reviewer.md` exists with frontmatter `model: sonnet`, `tools: Read, Grep, Glob, Bash`, and a system prompt that mirrors the Codex plan-review prompt structure. Output contract is one fenced ```json block conforming to `codex_plan_review_schema.json` (`{plan_file, verdict ∈ {approved, approved-with-notes, needs-replan}, findings[], notes[], schedule_ok, summary}`). The prompt explicitly states "You do NOT have the Agent tool" and forbids editing the plan or schedule.
  - `dispatch-templates.md` gains a new section "Phase 1.5-Claude — plan-reviewer dispatch (claude_only path)" that renders the Agent dispatch with `subagent_type: "plan-reviewer", model: "sonnet"`. The template embeds the schedule path, plan directory path, repo root, `findings_count` placeholder, and the `--allow-gaps` demotion clause when applicable.
  - `SKILL.md` §Phase 1.5 grows a route-switch at the top: when `claude_only=true`, dispatch via the Phase 1.5-Claude template; otherwise, dispatch via the existing wrapper path. Both branches feed the same `parse-plan-review-report` parser; the only difference is the dispatch mechanism and the `reviewer` field in the run-log events.
  - `SKILL.md` §Phase 1.5 retires the `codex_available=false → plan_review_skipped {reason:"codex_unavailable"}` skip clause. That case now flows through `claude_only=true` instead (per TASK-001's binding) and dispatches the Claude reviewer.
  - `plan_ops.py parse-plan-review-report` accepts a new `--from-claude` flag. When set, the parser treats stdin as the bare `parsed` payload (matching `codex_plan_review_schema.json`) instead of expecting the wrapper envelope `{task_id, subcommand, outcome, codex_exit_code, parsed}`. All existing callers continue to work without the flag.
  - Run-log events on the Claude path: `plan_review_start {reviewer:"claude", plan_file:"<basename>"}`, `plan_review_done {reviewer:"claude", verdict, findings_count, summary}`. The Codex path remains `reviewer:"codex"` unchanged.
  - Tests added to `tests/scripts/test_plan_ops.py`:
  - The test command `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "parse_plan_review_report"` returns 0.
- **Reversion guidance:** none

**Description:**
Replace the wrapper shell-out in Phase 1.5 with an Agent dispatch when `claude_only=true`. New `plan-reviewer` agent (Sonnet) produces the same envelope shape as the wrapper; `parse-plan-review-report` gains `--from-claude` to strip the envelope wrapping. Verdict-routing ladder (`approved | approved-with-notes | needs-replan`), `--codex-plan-review-binding` (now mutex with `--claude-only` per TASK-001), the auto-revise `plan-author` path, and the `--allow-gaps` demotion all keep working unchanged because they consume the parsed verdict, not the dispatch mechanism.

The `plan-reviewer` agent's prompt mirrors the Codex prompt body 1:1 except for: (a) replacing Codex-specific calibration ("Codex historically overuses `needs-rework`" guidance from `dispatch-templates.md:387` is dropped — Sonnet's calibration is different), (b) "You do NOT have the Agent tool" constraint, (c) inputs are passed as Agent prompt placeholders rather than CLI args. The output schema is identical to `codex_plan_review_schema.json`.
