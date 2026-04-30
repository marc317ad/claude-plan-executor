---
bug_id: 147
status: OPEN
group: WRAPPER-PLAN-REVIEW-LATENT
severity: minor
source_fix_id: null
source_plan: null
source_date: 2026-04-30
origin: surfaced during /implement-plan TASK-011 of MCP_MIGRATION (run 20260430T033005) and the subsequent v2 refinement of PLAN_PURE_CORE_CODEMOD; do not address until PLAN_PURE_CORE_CODEMOD completes
decomposed_at: 2026-04-30
absorbed_fix_ids: []
dependencies:
  - "PLAN_PURE_CORE_CODEMOD must land first"
dependency_fix_ids: []
files:
  - plugins/plan-executor/scripts/plan_claude_dispatch.py
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/skills/implement-plan/SKILL.md
content_fingerprint: null
change_history: []
---

# BUG-147: Wrapper schema-validation accepts markdown-prose-wrapped JSON; Codex plan-review blind to body-level scope risk

**Status:** OPEN
**Severity:** minor (procedural — neither bug masks correctness, but both let drift through that should have been caught upstream)
**Group:** WRAPPER-PLAN-REVIEW-LATENT
**Depends on:** `PLAN_PURE_CORE_CODEMOD` must complete first — these two bugs surfaced during that plan's v1 refinement and are tracked here so the codemod plan continues unaffected. Address after the codemod plan's TASK-006 lands.
**Test command:** `venv/bin/pytest tests/scripts/test_plan_claude_dispatch.py tests/scripts/test_plan_review.py -v -k "schema or scope_risk"`

## Acceptance criteria

### Sub-bug A — Wrapper schema accepts markdown-prose-wrapped JSON
- `plan_claude_dispatch.py` (and the Codex/Gemini equivalents that consume the same wrapper schema) MUST reject an implementer-result body where the JSON envelope is wrapped in markdown prose / a fenced code block, returning a `malformed` outcome instead of `status: ok`.
- A unit test in `tests/scripts/test_plan_claude_dispatch.py` MUST feed a body of the form:
  ```
  Here is my report:

  ```json
  {"outcome":"success","files_changed":[...],"report":{...}}
  ```

  Let me know if you need anything else!
  ```
  …and assert the wrapper classifies this as `malformed` (currently it returns `status: ok` because the JSON is parseable somewhere in the body).
- Existing happy-path schema tests MUST continue to pass — the fix is to require the body be a single JSON document with no wrapping prose, not to add JSON-extraction tolerance.

### Sub-bug B — Codex plan-review blind to body-level scope risk
- `plan_codex_dispatch.py` (or whichever module formats the `plan-review` prompt for Codex) MUST surface body-level scope-risk findings when the plan body explicitly names them. Today, schedule-level review returns `findings: []` because the prompt asks Codex to validate the **schedule** (DAG ordering, file-ownership disjointness, AC presence) and Codex correctly answers "the schedule is fine" — but the actual risk is in the plan **body** (e.g. "this task touches 38 functions across 13796 lines, AC underestimates scope by 5x").
- A new test in `tests/scripts/test_plan_review.py` MUST exercise a plan whose body contains an explicit body-scope-risk callout (e.g. a "## Risks" section flagging "AC underestimates plan_ops.py refactor scope") and assert that Codex's `plan-review` returns at least one `findings[]` entry referencing that risk, OR that the prompt format has been changed so body-level scope is in-scope for the review.
- Pick one of these two paths and document it in `SKILL.md` Phase 1.5:
  1. Extend the prompt to explicitly include "scan the plan body for risk callouts" as a review responsibility.
  2. Add a separate `body-review` Codex pass (similar to `plan-review` but body-scoped) that gates Phase 2.

## Problem

### Sub-bug A symptom

During the v1 refinement of `PLAN_PURE_CORE_CODEMOD`, while reviewing run `20260430T033005`'s implementer dispatch (TASK-011), we observed that the wrapper accepts an implementer body containing JSON wrapped in markdown prose without flagging it. The dispatch envelope at `/tmp/dispatch_011.json` documents the schema (`tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json`) but the wrapper's actual parser is forgiving: if the body parses as JSON anywhere (or contains an extractable JSON block), it returns `status: ok`. This means a Claude implementer that emits prose-then-JSON gets a green light when it should be classified `malformed`. Today the only safety net is the implementer's own self-discipline plus the schema's `additionalProperties: true`.

### Sub-bug B symptom

The same v1 refinement run had Codex `plan-review` return `findings: []` on a `PLAN_MCP_MIGRATION` schedule whose **body** explicitly named "TASK-011/012/013 are large code/test refactors assigned to Claude even though the rubric suggests Codex for large mechanical edits" (see `/tmp/plan_review.json` line 14, captured as a `notes[]` entry rather than a `findings[]` finding). The reviewer flagged it as a non-blocking `notes[]` entry and approved the schedule — yet the body-level mismatch is exactly what caused TASK-011 to be dispatched to Claude, hit a 15-minute timeout, and ultimately be marked `plan-incorrect` after the implementer documented that AC underestimates the refactor scope by 5x. The plan-review pass had the information in front of it but the prompt's framing (validate-schedule, not validate-body) led it to demote the finding.

### Why this is a single bug doc

Both sub-bugs are about the **plan-review and dispatch validation perimeter** failing to catch failure modes that the v1 refinement run subsequently surfaced via a plan-incorrect implementer. Either taken alone is minor; together they explain why a plan can pass Phase 1.5 review and still need a v2 refinement pass to fix.

### Impact

- Sub-bug A: a misformatted implementer body could ship `success` despite being malformed, breaking downstream parsers (commit-task, code-reviewer subagent dispatch).
- Sub-bug B: plan-review's verdict can be `approved-with-notes` while the body has a load-bearing scope-risk callout, leading to a wasted dispatch + plan-incorrect cycle.

## Recommended fix

- **Sub-bug A:** Tighten the wrapper's body parser. Require the body to be a single JSON document (after optional whitespace trimming), reject prose-wrapping. Update SKILL.md's Phase 2 envelope-emission section to remove any "you may add prose around the JSON" tolerance.
- **Sub-bug B:** Path 2 (separate `body-review` pass) is preferable — keeps the `plan-review` prompt's schedule focus intact and adds a body-scope reviewer that can catch the plan-incorrect-shaped risks. Path 1 is cheaper but expands an already-busy prompt and risks Codex de-prioritizing the new clause.

## Reproduction (forensic)

- Run id `20260430T033005` — `/tmp/dispatch_011.json` is the dispatch envelope; the implementer's response body lived in `_run_log.jsonl` for that run (look up `claude_dispatch_completed` event for run-id `20260430T033005`, task `011`).
- Plan-review verdict: `/tmp/plan_review.json` — `notes[2]` is the body-level scope risk that should have been a `findings[]` entry.

## Notes

- Both bugs surfaced during refinement of `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md` v2; the codemod plan itself is unaffected (it does not depend on either of these layers being fixed).
- The decision to file them here rather than block the codemod plan is documented in `PLAN_PURE_CORE_CODEMOD.md` § "Resolved during v2 user pass (2026-04-30)" — Q4.

## Run history

(none yet — bug filed 2026-04-30)
