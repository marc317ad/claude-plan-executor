# TASK-006 — Phase 1.5 fallback wiring (Codex unavailable / transient failure → Gemini)

## Goal

Wire the Phase 1.5 plan-review path so that, when `--allow-gemini-fallback` is set, an unavailable or transiently-failed Codex `plan-review` re-routes to `plan_gemini_dispatch.py plan-review` exactly once before the orchestrator degrades to the existing summary-warning behavior. Update `SKILL.md §Phase 1.5` and `dispatch-templates.md` to describe the new path.

## Context

Today's Phase 1.5 flow (per `SKILL.md §Phase 1.5` and `dispatch-templates.md §Phase 1.5`):

- **Skip when** `--skip-plan-review` set → `plan_review_skipped {reason:"flag"}` + summary banner.
- **Skip when** `codex_available=false` → `plan_review_skipped {reason:"codex_unavailable"}` + summary warning *"lacking independent plan review"*.
- **Otherwise** dispatch `plan_codex_dispatch.py plan-review`. On `outcome ∈ {timeout, parse_error, failure}` the orchestrator treats the result as `plan_review_skipped {reason:"codex_unavailable"}` for routing — the pre-dispatch gate degrades on reviewer-side errors rather than blocking execution.

The new fallback path:

- **When** `--allow-gemini-fallback` is set AND `codex_available=false` AND `gemini_available=true` (the new TASK-005 preflight field) → dispatch `plan_gemini_dispatch.py plan-review` instead of skipping. Log a new run-log event `plan_review_fallback_used {from:"codex", to:"gemini", reason:"codex_unavailable"}` BEFORE `plan_review_start`. The rest of the verdict-routing table (`approved | approved-with-notes | needs-replan` → existing routes) applies unchanged.
- **When** Codex `plan-review` returns `outcome ∈ {timeout, parse_error, failure}` AND `--allow-gemini-fallback` is set AND `gemini_available=true` → re-dispatch ONCE to `plan_gemini_dispatch.py plan-review`. Log `plan_review_fallback_used {from:"codex", to:"gemini", reason:"<codex_outcome>"}`. If the Gemini dispatch ALSO returns `outcome ∈ {timeout, parse_error, failure}`, fall through to today's degraded behavior (`plan_review_skipped {reason:"codex_unavailable"}`-style; rename the reason to `"all_reviewers_unavailable"` so the audit trail is clear).
- **When** `--allow-gemini-fallback` is NOT set, behavior is unchanged from today — degraded skip with summary warning. The flag is opt-in.

The verdict vocabulary (`approved | approved-with-notes | needs-replan`) is reviewer-agnostic. The Gemini-emitted findings flow through the existing `parse-plan-review-report` parser, which already validates against `codex_plan_review_schema.json`. To accept Gemini envelopes, the parser must validate against `gemini_plan_review_schema.json` when the envelope's `reviewer: "gemini"` field is present, OR — cheaper — accept either schema in v1 and let the structural mirror (TASK-002) make the discrimination unnecessary. **Choose the second path**: the parser dispatches by `reviewer` field but uses a single shared schema-validation helper that accepts either schema and notes which one validated. This keeps the routing code reviewer-agnostic.

The Phase 1.5.5 triage path is unchanged. Triage is Claude-Sonnet by design; Gemini findings flow into `<codex_findings_json>` (rename internally to `<reviewer_findings_json>` if you want to scrub the Codex-specific naming, but DO NOT change the dispatch-template's external interface — the template's input contract is observed by the `plan-review-triage` agent and is already shipped). The simplest intervention: keep the placeholder name `codex_findings_json` for v1 (it's a name, not a contract) and add a one-line note in `dispatch-templates.md` clarifying that the placeholder receives the active reviewer's findings regardless of family.

The `--allow-gaps` demotion clause (TASK-004) flows through identically — the wrapper-level decision was already centralized in `_should_inject_allow_gaps_demotion`, so both reviewers honor the operator's opt-in.

**Out of scope.** Phase D.1 fallback (TASK-007). Wrapper code (TASK-003 / TASK-004). Preflight and flag plumbing (TASK-005). Schema audit (TASK-008).

## Verification

- `SKILL.md §Phase 1.5` describes the new fallback path with a routing table that adds rows for the `--allow-gemini-fallback` cases:
  - `--allow-gemini-fallback` + `codex_available=false` + `gemini_available=true` → dispatch Gemini.
  - `--allow-gemini-fallback` + Codex returns `{timeout, parse_error, failure}` + `gemini_available=true` → re-dispatch Gemini once.
  - `--allow-gemini-fallback` set + `gemini_available=false` → degrade exactly as today (no fallback possible).
  - `--allow-gemini-fallback` NOT set → identical to today regardless of preflight gemini state.
- `dispatch-templates.md` gains a `## Phase 1.5-Gemini` section (parallel to the existing `## Phase 1.5`) describing the Bash command template for `plan_gemini_dispatch.py plan-review` with the same envelope shape, the same `--allow-gaps` pass-through, and a one-line note about the schema-validation retry's `parse_error` outcome being treated as a transient failure (so the orchestrator can fall through to "all reviewers unavailable" instead of looping).
- New run-log events are documented in `SKILL.md §Phase 1.5` and the `run-log-schema.md` reference:
  - `plan_review_fallback_used {from, to, reason}` — emitted BEFORE the second dispatch.
  - `plan_review_skipped {reason: "all_reviewers_unavailable"}` — emitted when both reviewers fail (or one is absent and the other fails).
- The Phase 1.5.5 triage path is documented as reviewer-agnostic — the placeholder `codex_findings_json` receives the active reviewer's findings regardless of family.
- A new test in `tests/scripts/test_plan_ops.py` (or a new file `tests/scripts/test_plan_review_fallback_routing.py`) validates the routing-decision helper's truth table for the four orchestrator-state combinations above. The test does NOT exercise the wrapper end-to-end (that's covered by TASK-003 / TASK-004 wrapper tests); it tests the routing function in isolation with synthetic inputs.
- The routing-decision helper is named `_route_plan_review(allow_gemini_fallback: bool, codex_available: bool, gemini_available: bool, codex_outcome: Optional[str]) -> Literal["codex", "gemini", "skip"]` and lives in `plan_ops.py` so its truth table is unit-testable.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` returns 0 (or the new file if you split it).

## Tasks

### TASK-006: Phase 1.5 fallback wiring (Codex unavailable / transient failure → Gemini)

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — `## Phase 1.5` section + log-event table + `## Parse arguments` cross-refs)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — add `## Phase 1.5-Gemini` section + Phase 1.5.5 reviewer-agnostic note)
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md` (edit — add `plan_review_fallback_used` event)
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — add `_route_plan_review` helper + integrate with `parse-plan-review-report` so the parser accepts envelopes from either reviewer)
  - `tests/scripts/test_plan_ops.py` (edit — append routing-helper tests; ALSO append parse-plan-review-report tests for Gemini envelopes)
- **Dependencies:** [004, 005]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:400-560` (§Phase 1.5 + §Phase 1.5.5 — the prose this task extends)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md:44-90` (§Phase 1.5 — the template this task parallels)
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md` — full file (document the new event in the same idiom as existing entries)
  - `plugins/plan-executor/scripts/plan_ops.py` — the `cmd_parse_plan_review_report` definition (find via `grep -n "parse_plan_review_report\|parse-plan-review-report"`)
- **Symbol targets:**
  - `cmd_parse_plan_review_report` in `plan_ops.py`
- **Acceptance criteria:**
  - The new `_route_plan_review` helper exists and the truth-table tests pass.
  - `parse-plan-review-report` accepts envelopes with `reviewer: "gemini"` and validates them against `gemini_plan_review_schema.json`. Envelopes without a `reviewer` field default to Codex (preserves backward compat for in-flight runs whose envelopes were emitted by today's wrapper).
  - SKILL.md §Phase 1.5 prose describes both fallback rows AND the `--allow-gemini-fallback` operator opt-in. The summary-warning text on the all-reviewers-unavailable path reads *"lacking independent plan review (both Codex and Gemini unavailable or failed)"*.
  - dispatch-templates.md `## Phase 1.5-Gemini` Bash template uses `plan_gemini_dispatch.py plan-review` with the same `--schedule-file --repo-root --timeout [--allow-gaps]` flag set as the Codex path.
  - run-log-schema.md documents `plan_review_fallback_used {from, to, reason}` with `from ∈ {codex}`, `to ∈ {gemini}`, `reason ∈ {codex_unavailable, codex_timeout, codex_parse_error, codex_failure}`.
  - No behavior change for runs invoked WITHOUT `--allow-gemini-fallback` — verify by running an existing fixture's preflight + simulated Phase 1.5 routing through `_route_plan_review` and asserting the verdict is `"skip"` regardless of `gemini_available`.
- **Reversion guidance:** revert all five files. The fallback path is opt-in via flag; no existing run is affected by the absence of the flag's behavior, so reversion is safe mid-flight.

**Description:**
Phase 1.5 fallback wiring. The orchestrator-side decision (route to Codex / Gemini / skip) is centralized in `_route_plan_review` so the truth table is unit-testable in isolation; the SKILL.md prose tells the orchestrator (Claude-Opus running the skill) to invoke the helper. The parser update lets a single `parse-plan-review-report` call validate either family's envelope without the orchestrator having to discriminate.

**Implementation notes:**
- The helper's truth table:
  ```
  _route_plan_review(allow_fallback=False, codex_avail=True,  gemini_avail=*,    codex_outcome=None)             → "codex"
  _route_plan_review(allow_fallback=False, codex_avail=False, gemini_avail=*,    codex_outcome=None)             → "skip"
  _route_plan_review(allow_fallback=True,  codex_avail=True,  gemini_avail=*,    codex_outcome=None)             → "codex"
  _route_plan_review(allow_fallback=True,  codex_avail=False, gemini_avail=True, codex_outcome=None)             → "gemini"
  _route_plan_review(allow_fallback=True,  codex_avail=False, gemini_avail=False, codex_outcome=None)            → "skip"
  _route_plan_review(allow_fallback=True,  codex_avail=True,  gemini_avail=True, codex_outcome="timeout")        → "gemini"
  _route_plan_review(allow_fallback=True,  codex_avail=True,  gemini_avail=False, codex_outcome="timeout")       → "skip"
  _route_plan_review(allow_fallback=False, codex_avail=True,  gemini_avail=*,    codex_outcome="timeout")        → "skip"
  _route_plan_review(allow_fallback=*,     codex_avail=*,     gemini_avail=*,    codex_outcome="success")        → "codex"  (no-op — caller wouldn't invoke helper here, but defensive)
  ```
- Returning `"gemini"` after a Codex failure means the orchestrator dispatches to Gemini ONCE; the helper does not loop. If Gemini also fails, the caller invokes the helper a second time with `codex_outcome=<gemini_outcome>` and `codex_available=False, gemini_available=False` (or pass a separate `gemini_outcome` arg) — the helper then returns `"skip"`. The exact secondary-call signature is the implementer's call; document it in the helper's docstring.
- The parser update: `parse-plan-review-report` reads the envelope, extracts `reviewer` (default `"codex"`), picks the matching schema, validates `parsed`, returns the same shape it does today (the parser's output contract is unchanged because the schemas are structural mirrors).
- Do NOT mention the legacy Codex-specific schema name in user-facing prose — say "the plan-review schema" and let the implementation pick. This keeps the SKILL.md prose family-agnostic.
- The new test does NOT spawn subprocesses or read real Gemini envelopes — it tests the helper's truth table with literal arguments.
