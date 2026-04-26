# TASK-006 — N-state contract reminder in plan-implementer prompt

## Goal

Reduce the friction class behind the post-mortem's Issue 6 (TASK-028 D-Codex needs-rework on `_unwrap_cli_envelope`): the AC named a 3-state contract ("unwrapped" / "unexpected" / "non-json") but the implementer collapsed it to 2 by returning `None` for both failure states. Add a brief, generalizable reminder to the plan-implementer prompt so an AC enumerating N distinct outcomes yields a return shape that distinguishes all N.

## Context

**The friction.** TASK-028's AC required the live tests to call `pytest.skip(f"unexpected claude JSON envelope shape: ...")` on the parsed-but-unexpected branch — a 3-state contract on the helper. The first implementation flattened to 2 (`str | None`); Codex's review correctly flagged this; the orchestrator refactored to `_classify_cli_envelope(stdout) -> tuple[str, object]` returning `("unwrapped", str)`, `("unexpected", body)`, or `("non-json", None)`. ~3 minutes lost; cleanly avoidable.

**The mechanic.** A short subsection in the implementer prompt: "If the AC enumerates ≥3 distinct outcome states for a helper or tests, the helper's return type must distinguish all N — do NOT collapse states into a sentinel like `None` or empty string." The reminder includes one worked example (the TASK-028 case, abbreviated) so the trigger shape is concrete.

**Pairing with TASK-002.** The "Coupling check" step (TASK-002) and this "N-state contract" step are siblings — both refine the implementer's reading of the AC. Keep them as two separate prompt sections so each can fire independently. Resist the temptation to merge into a generic "read the AC carefully" — concrete trigger heuristics outperform open-ended exhortations.

**Scope.** Spec-only — agent prompt edit. No `plan_ops.py` change.

## Verification

- `plugins/plan-executor/agents/plan-implementer.md` gains an "N-state contract" subsection (alongside or near the "Coupling check" subsection from TASK-002).
- The subsection names the trigger ("AC enumerates ≥3 distinct outcomes for a helper or tests"), the rule ("the return shape must distinguish all N"), and one worked example.
- The implementer report schema (per `dispatch-templates.md`) gains an optional `outcome_states` block: `{count: int, branches: [...]}` populated when the trigger fires; absent otherwise.
- A unit test in `tests/scripts/test_plan_ops.py` parses a sample implementer report containing the `outcome_states` block and asserts round-trip integrity.
- An integration smoke check (or doc-only assertion) confirms the prompt section is present and the example references the TASK-028 motif (3-state CLI envelope unwrap).

## Tasks

### TASK-006: N-state contract reminder

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/agents/plan-implementer.md` (edit — N-state subsection + optional report-schema field)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — surface `outcome_states` field in §Phase B report shape)
  - `tests/scripts/test_plan_ops.py` (round-trip test for the new report field)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k outcome_states`
- **Read targets:**
  - `plugins/plan-executor/agents/plan-implementer.md` — full file
- **Acceptance criteria:**
  - `plan-implementer.md` gains an "N-state contract" subsection that fires when the AC enumerates ≥3 outcome states for a helper, function, or test set.
  - The rule is one sentence: "the return shape must distinguish all N enumerated states; do not collapse failure states into a single sentinel."
  - One worked example cites the TASK-028 case (`_unwrap_cli_envelope` 3-state vs `_classify_cli_envelope` tuple-shape) — abbreviated to the essential motif.
  - The implementer report schema gains an optional `outcome_states` block with `count` (int) and `branches` (list of `{name, return_value, condition}`). Absent when not triggered.
  - The round-trip test confirms a sample report with the block parses cleanly and surfaces the count + branches in the orchestrator-side report parser.
  - The new prompt section does NOT change implementer verdict allowlist or D.2a routing.
  - The reminder is placed adjacent to (or in the same group as) the TASK-002 "Coupling check" subsection so both AC-reading-heuristic prompts live together.
- **Reversion guidance:** revert prompt subsection + report-schema field; routing untouched.

**Description:**
Add a brief, concrete reminder to the plan-implementer prompt that an AC enumerating ≥3 outcome states demands a return shape distinguishing all N. Include one worked example so the trigger shape is recognizable.

**Implementation notes:**
- Resist the temptation to generalize the rule beyond return shapes; the specific failure mode is "collapsed N states into a sentinel," not "ignored the spec."
- The `outcome_states` report block should be optional (emitted only when triggered) so non-triggered tasks don't carry the field.
- One worked example is enough — two would make the prompt section disproportionately long for the value it provides.
