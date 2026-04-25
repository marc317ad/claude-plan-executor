# TASK-007 — Phase D.1 fallback wiring (Codex review of Claude work fails → Gemini)

## Goal

Wire the Phase D.1 cross-review path so that, when `--allow-gemini-fallback` is set and Codex's `review` of a Claude-implemented task returns a transient failure, the orchestrator re-dispatches the same review to `plan_gemini_dispatch.py review` exactly once before treating the task as failed. Update `SKILL.md §Phase D — Review + commit` and `dispatch-templates.md §Phase D-Codex` to document the new path. The downstream verdict-routing tables (D.2 / D.2a / D.2a.5 / D.2a.6) remain unchanged because the verdict vocabulary is reviewer-agnostic.

## Context

Today's Phase D.1 flow for Claude-implemented tasks (per `SKILL.md §Phase D — Review + commit`):

- Dispatch `plan_codex_dispatch.py review --plan-file <p> --task-id NNN --repo-root <r> --files <f> --review-focus bugs --timeout 180`.
- Parse `parsed.verdict ∈ {clean, minor-findings, needs-rework}`.
- Wrapper-level transient failures (`outcome ∈ {timeout, parse_error, failure, scope_violation}`) currently surface as `failure` in the orchestrator's classification. The orchestrator does NOT have a defined fallback today — these failures cascade into the task's failure path. (Compare with Phase B's Codex implementer fallback to Claude — `outcome ∈ {failure, timeout, parse_error, scope_violation}` triggers a Claude re-dispatch via the Phase B template per `SKILL.md §Phase B`.)

The new fallback path for Phase D.1, gated behind `--allow-gemini-fallback`:

- **When** `--allow-gemini-fallback` is set AND Codex `review` returns `outcome ∈ {timeout, parse_error, failure}` AND `gemini_available=true` (TASK-005) → re-dispatch the same review to `plan_gemini_dispatch.py review` ONCE before classifying as a review-stage failure. Log `review_fallback_used {task_id, from:"codex", to:"gemini", reason:"<codex_outcome>"}` BEFORE the Gemini dispatch.
- **When** the Gemini dispatch ALSO returns a transient failure → classify as today's review-stage failure path. Log a final `review_fallback_failed {task_id, from:"codex", to:"gemini", reason:"<gemini_outcome>"}` event for audit symmetry.
- **When** `--allow-gemini-fallback` is NOT set, behavior is unchanged from today — Codex transient failures cascade into review-stage failure.

`scope_violation` is intentionally NOT a fallback trigger — a scope violation is a structural defect in the implementer's output (per Phase B), not a transient reviewer failure. Routing it to Gemini would mask the underlying issue. Keep `scope_violation` on its existing path.

**Asymmetry vs. Phase D.1 for Codex-implemented tasks (Claude reviewing).** When Codex implements and Claude reviews, the reviewer is Claude-Sonnet via Agent dispatch — there is no wrapper transient-failure surface. A malformed Agent reply already routes through `fail-task stage=implement reason=malformed_report` per the existing dispatch-rule contract. Gemini does NOT substitute for Claude's reviewer role here; the lattice is preserved.

**Verdict-routing tables (D.2 / D.2a / D.2a.5 / D.2a.6) are reviewer-agnostic.** The `clean | minor-findings | needs-rework` vocabulary applies regardless of which family produced it. The D.5 third-opinion (Phase D.5) stays Claude-Sonnet by design; Gemini findings flow into `<codex_findings_json>` (placeholder name unchanged for v1). The D.5 template prose at the boundary `Codex (a peer reviewer) returned needs-rework` may need a one-line widening to `<reviewer> (a peer reviewer)` so it reads correctly when Gemini is the source — make this widening in this task. The wrapper-checks placeholder `<wrapper_checks_json>` is Codex-specific (symbol-warnings); when Gemini is the reviewer, pass `{"symbol_warnings": []}` as the default (the existing default for failure-path Codex envelopes).

The `D.2b role-swap retry` for Codex-implemented tasks is unaffected — Gemini does not enter the implementation tier under any condition.

**Out of scope.** Phase 1.5 fallback (TASK-006). Wrapper code (TASK-003 / TASK-004). Preflight and flag plumbing (TASK-005). Schema audit (TASK-008).

## Verification

- `SKILL.md §Phase D — Review + commit` describes the new fallback path with a routing table addition for the `--allow-gemini-fallback` Codex-transient-failure case.
- `dispatch-templates.md` gains a `## Phase D-Gemini` section parallel to `## Phase D-Codex` describing the Bash command template for `plan_gemini_dispatch.py review` with the same flag set as the Codex path. The verdict-decision-ladder prose is referenced — NOT duplicated — because the same calibration prose applies to both reviewer families (lift the ladder to a shared `### Verdict decision ladder (cross-family reviewer calibration)` heading and have both `## Phase D-Codex` and `## Phase D-Gemini` reference it).
- `dispatch-templates.md §Phase D.5` prose updates the lead-in from `Codex (a peer reviewer)` to `<reviewer> (a peer reviewer)`. The placeholder `<reviewer>` is rendered as `Codex` or `Gemini` per the active dispatch.
- New run-log events documented in `run-log-schema.md`:
  - `review_fallback_used {task_id, from:"codex", to:"gemini", reason:"<codex_outcome>"}` — emitted BEFORE the second dispatch.
  - `review_fallback_failed {task_id, from:"codex", to:"gemini", reason:"<gemini_outcome>"}` — emitted on second-failure path.
- The `_route_plan_review` helper from TASK-006 has a sibling `_route_review` helper for the Phase D.1 truth table: `_route_review(allow_gemini_fallback: bool, codex_available: bool, gemini_available: bool, codex_outcome: Optional[str]) -> Literal["codex", "gemini", "fail"]`. Same signature shape as `_route_plan_review`; different output vocabulary (`"fail"` instead of `"skip"` because Phase D.1 has no skip surface — review failure cascades into task failure unless fallback succeeds).
- `parse-implementer-report` and the per-reviewer parsing path accept Gemini envelopes via the same `reviewer` discriminator as TASK-006. The cross-review parse path may already be covered by TASK-006's parser update if the function is shared; verify and de-duplicate.
- New tests in `tests/scripts/test_plan_ops.py` (or a sibling file) cover the `_route_review` truth table, parallel to TASK-006's `_route_plan_review` test suite.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` returns 0.
- A documentation note in `SKILL.md §Phase D — Review + commit` clarifies that `--codex-review-binding` continues to work even with `--allow-gemini-fallback`: when the Codex verdict is `needs-rework` AND `--codex-review-binding` is set, the orchestrator goes straight to `fail-task` regardless of the Gemini fallback flag (binding wins). Gemini is a fallback for *transient failures*, not for verdict disagreements.

## Tasks

### TASK-007: Phase D.1 fallback wiring (Codex review of Claude work fails → Gemini)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — `## Phase D — Review + commit` section + log-event table)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — add `## Phase D-Gemini` section + lift the verdict-decision ladder + widen Phase D.5 lead-in)
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md` (edit — add `review_fallback_used` and `review_fallback_failed` events)
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — add `_route_review` helper)
  - `tests/scripts/test_plan_ops.py` (edit — append routing-helper tests)
- **Dependencies:** [003, 005, 006]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:650-740` (§Phase D + §D.1 + §D.2 — the prose this task extends)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md:386-525` (§Phase D-Codex + §Phase D.5 — the templates this task parallels and widens)
  - `plugins/plan-executor/scripts/plan_ops.py` — find `_route_plan_review` from TASK-006 (the sibling helper this task adds)
- **Symbol targets:**
  - `_route_plan_review` in `plan_ops.py`
- **Acceptance criteria:**
  - The new `_route_review` helper exists and the truth-table tests pass.
  - SKILL.md §Phase D describes the fallback row AND clarifies that `--codex-review-binding` is independent of `--allow-gemini-fallback`.
  - dispatch-templates.md `## Phase D-Gemini` Bash template uses `plan_gemini_dispatch.py review` with the same flag set; the verdict-decision ladder is lifted to a shared heading with both Phase D-Codex and Phase D-Gemini referencing it.
  - dispatch-templates.md `## Phase D.5` widens the lead-in to `<reviewer> (a peer reviewer)`.
  - run-log-schema.md documents both new events.
  - No behavior change for runs without `--allow-gemini-fallback` — verify via the truth-table tests.
- **Reversion guidance:** revert all five files. Behavior is opt-in via the same flag from TASK-005, so reversion is safe mid-flight.

**Description:**
Phase D.1 fallback wiring, parallel structure to TASK-006. The verdict-decision-ladder lift is the only mechanical refactor in this task — the rest is prose updates and a small helper. The asymmetry call-out (Gemini does not substitute for Claude as reviewer of Codex work) is documented prose so a future operator does not over-extend the fallback model.

**Implementation notes:**
- Helper truth table is parallel to TASK-006's:
  ```
  _route_review(False, *, *, *)                         → "codex"  (orchestrator dispatches Codex; result drives next step)
  _route_review(False, *, *, "timeout"|"parse_error"|"failure") → "fail"
  _route_review(True,  *, True,  "timeout"|"parse_error"|"failure") → "gemini"
  _route_review(True,  *, False, "timeout"|"parse_error"|"failure") → "fail"
  _route_review(*,     *, *,     "scope_violation")     → "fail"  (scope violation is structural; no fallback)
  ```
  Document `scope_violation` is NOT a fallback trigger inline at the helper.
- The verdict-decision-ladder lift: today the ladder lives inside `## Phase D-Codex` (`dispatch-templates.md:402-422`). Move it to a new section `### Verdict decision ladder (cross-family reviewer calibration)` immediately above both `## Phase D-Codex` and `## Phase D-Gemini`. Both family templates reference it via a one-line `(see "Verdict decision ladder" above)` cross-reference. The prose itself is byte-for-byte the same — no calibration drift between families.
- Phase D.5's wrapper-checks placeholder: when the active reviewer is Gemini, pass the structural default `{"symbol_warnings": []}`. The Gemini wrapper (TASK-003) does NOT emit `wrapper_checks` in its envelope today; v1 keeps the field reviewer-specific. Document this in `dispatch-templates.md` so future readers don't expect symmetry.
- DO NOT change the `<codex_findings_json>` placeholder name in v1. It's a name, not a contract; the agent reads it as "the active reviewer's findings". Renaming is a separate cleanup task across multiple templates and is not load-bearing for the fallback to work.
- The `--codex-review-binding` interaction note in SKILL.md is the only operator-facing behavior clarification — it costs nothing and prevents future confusion about why a `needs-rework` Codex verdict isn't getting "rescued" by Gemini (it shouldn't be — binding means the verdict is binding, full stop).
