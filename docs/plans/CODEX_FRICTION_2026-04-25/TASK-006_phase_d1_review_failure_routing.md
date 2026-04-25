# TASK-006 — Phase D.1 review-failure routing in SKILL.md (Issue 3 + ad-hoc gap)

## Goal

Document an explicit Phase D.1 routing rule for wrapper outcomes `{timeout, parse_error, failure}`. Today the rule is buried inside the orchestrator's behavioural lore — the friction run treated a 180 s review timeout ad-hoc as `review_skipped {reason: "codex_review_timeout"}` and committed with `--reviewer none --reviewer-verdict ""`. That is the right move; SKILL.md should say so. Mirroring Phase 1.5's already-documented degraded-reviewer rule (`SKILL.md:450`) makes the routing legible to the orchestrator LLM and to humans reading the spec.

## Scoped Context

**Today's prose at `SKILL.md:658-678` (Phase D.1).**

```
*Claude-implemented → Codex review:*

```bash
$PYTHON .../plan_codex_dispatch.py review \
  --plan-file <abs> --task-id NNN --repo-root <abs> \
  --files <files_changed> --review-focus bugs --timeout 180
```

Parse `parsed.verdict ∈ {clean, minor-findings, needs-rework}`.

*Codex-implemented → Claude review:*

`Agent(subagent_type: "code-reviewer", ...)`.

Parse verdict `∈ {ship, ship-with-fixes, needs-rework}`. ...

Log `review_start` then `review_done {task_id, reviewer, verdict, ...}`.
```

Notice — no branch covers wrapper `outcome ∈ {timeout, parse_error, failure}`. `cmd_review` emits these envelopes today (the wrapper code at lines 1422-1474 handles them all), but SKILL.md does not document the orchestrator's response. Phase 1.5's parallel section (line 450) already says: *"Wrapper timeout / parse_error / failure outcomes surface as `outcome ∈ {timeout, parse_error, failure}`; treat as `plan_review_skipped {reason:"codex_unavailable"}` for routing purposes — the pre-dispatch gate degrades on reviewer-side errors rather than blocking execution."* This task lands the same shape of rule for D.1.

**The proposed routing.**

When `subcommand=review` and `outcome ∈ {timeout, parse_error, failure}`:
1. Log `review_skipped {task_id, reviewer:"codex", reason}` where `reason` is one of:
   - `"codex_review_timeout"` — `outcome=timeout`
   - `"codex_review_parse_error"` — `outcome=parse_error`
   - `"codex_review_failure"` — `outcome=failure`
2. Proceed straight to D.3 commit with `--reviewer none --reviewer-verdict ""`. The commit body's reviewer line records "review skipped (reason)" so the audit trail is preserved.
3. Final run summary carries a *"Cross-review skipped on N tasks (codex unavailable)"* banner listing the affected `task_id`s and reasons.
4. The Codex-implemented → Claude-review direction's `Agent`-side failures are NOT covered by this rule — that path already has the D.5 ladder for substantive disagreement; an Agent dispatch failure is an orchestrator-side LLM error and falls under the existing `Agent` retry semantics (out of scope for this task).

**Why "skip with --reviewer none" is the correct response, not retry.** Retrying a wrapper timeout is a per-call decision the orchestrator currently does not have a budget for. The cross-review safety net is independent of the reviewer's existence — the implementer's own self-tests already ran in Phase B (Codex implements + runs `test_command`). Skipping the cross-review preserves forward progress; retrying would either succeed on a faster path (already a Phase 1.5-style "best effort" reviewer call) or burn time on the same failure mode. The friction run made the right call ad-hoc; this task documents it.

**`--reviewer none` semantics (existing).** `commit-task --reviewer none --reviewer-verdict ""` is already a documented mode (`SKILL.md:1075`, used by `--skip-cross-review` runs). Reusing it for `review_skipped` keeps the commit-side surface unchanged.

**Run-log / event-list updates.**

- `review_skipped` is already in the event list at `SKILL.md:1001`. Document the new `reason` enum values: `codex_review_timeout`, `codex_review_parse_error`, `codex_review_failure`.

**Out of scope.**

- Adding wrapper-side retries inside `cmd_review` for transient timeouts. The wrapper's contract is "single dispatch, one envelope." Multi-attempt strategies belong in the orchestrator if anywhere.
- Changing the cross-review asymmetry (Claude impl → Codex review, Codex impl → Claude review). The route table at `SKILL.md:684` is correct as-is.
- Touching `dispatch-templates.md` — this is a SKILL.md prose addition, not a template change.

## Verification

- `SKILL.md` §Phase D.1 grows a new subsection (placed between the "Parse verdict ..." paragraph at line ~676 and the §D.2 "Route by verdict" header at line ~680) titled *"Wrapper failure outcomes (Codex side)"* (or similar) that documents the three `outcome ∈ {timeout, parse_error, failure}` reasons mapping to `review_skipped` with the reason enum above, plus the `--reviewer none --reviewer-verdict ""` commit form.
- The new subsection explicitly references the parallel Phase 1.5 rule at line 450 to make the symmetry visible.
- `SKILL.md` §run-log event list at line ~1001 (`review_skipped`) gains a documented `reason` enum: `flag` (existing — `--skip-cross-review`), `codex_review_timeout`, `codex_review_parse_error`, `codex_review_failure`.
- `SKILL.md` §Final run summary banner section documents the *"Cross-review skipped on N tasks ..."* banner, listing `task_id` and reason per skip. (If a "summary banners" section does not yet exist, place this prose adjacent to the existing `--skip-cross-review` banner clause at line ~654.)
- The §run-log event list's `review_done` documentation (line ~676) is NOT changed — `review_skipped` is the alternative event when no review took place.
- A grep for `Wrapper timeout / parse_error / failure outcomes` in `SKILL.md` returns at least two hits: the existing one at line ~450 (Phase 1.5) and the new one in §D.1.
- `tests/scripts/test_plan_ops.py` grows a small smoke test that asserts the SKILL.md prose contains the new strings (`codex_review_timeout`, `codex_review_parse_error`, `codex_review_failure`) so the documentation rule is regression-protected. (This mirrors the pattern of existing prose-presence smoke tests in the repo if any exist; if none exist, add a single straightforward `assert "codex_review_timeout" in skill_md_text` style test under a new `TestSkillRoutingDocumentation` class.)
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "skill_routing or routing_documentation"` returns 0 (or `tests/scripts/test_plan_ops.py` overall returns 0 when no `-k` filter is applied).

## Tasks

### TASK-006: Phase D.1 review-failure routing in SKILL.md

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit)
  - `tests/scripts/test_plan_ops.py` (edit)
- **Dependencies:** [004]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "skill_routing or routing_documentation"`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:400-460` (Phase 1.5 wrapper-failure routing — the model to mirror)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:652-790` (Phase D — review + commit, the section that needs the new prose)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:990-1080` (run-log event list)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1422-1474` (`cmd_review` failure paths — confirms the three outcome strings)
- **Symbol targets:**
  - `cmd_review` in `plan_codex_dispatch.py` (read-only — only to confirm the wrapper's outcome enum)
- **Acceptance criteria:**
  - SKILL.md §Phase D.1 documents the new "Wrapper failure outcomes (Codex side)" subsection with the three `outcome ∈ {timeout, parse_error, failure}` reasons and their mapping to `review_skipped {reason: "codex_review_<reason>"}` + `commit-task --reviewer none --reviewer-verdict ""`.
  - The new subsection cites the parallel Phase 1.5 rule at line ~450 to make the symmetry visible.
  - SKILL.md §run-log event list at line ~1001 documents the `review_skipped.reason` enum: `flag`, `codex_review_timeout`, `codex_review_parse_error`, `codex_review_failure`.
  - SKILL.md final-summary banner prose documents the *"Cross-review skipped on N tasks (codex unavailable)"* banner (with task_id + reason listing) adjacent to the existing `--skip-cross-review` banner clause near line ~654.
  - A documentation smoke test in `tests/scripts/test_plan_ops.py` asserts the new strings (`codex_review_timeout`, `codex_review_parse_error`, `codex_review_failure`) are present in `SKILL.md`. The test is small and self-contained (load file once, assert each substring).
  - All existing tests pass unchanged.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "skill_routing or routing_documentation"` returns 0.
- **Reversion guidance:** revert the SKILL.md prose additions and drop the documentation smoke test. The wrapper's emit semantics are unaffected; only the documented orchestrator response changes.

**Description:**
Document the orchestrator's response to wrapper review failures (`outcome ∈ {timeout, parse_error, failure}`) in SKILL.md §Phase D.1, mirroring Phase 1.5's already-documented degraded-reviewer rule. Add the matching reason enum values to the `review_skipped` run-log event documentation and the cross-review-skipped banner clause. Pin the new vocabulary with a small documentation smoke test so a future SKILL refactor cannot quietly delete the routing rule.

**Implementation notes:**
- Place the new subsection between the "Parse verdict ..." paragraph and the §D.2 "Route by verdict" header so the routing context is fresh in the reader's mind.
- Match the Phase 1.5 rule's tone and length — the precedent is two sentences plus the reason enum. Avoid expanding it into a multi-paragraph essay.
- The documentation smoke test reads `SKILL.md` once via `pathlib.Path(__file__).resolve().parents[2] / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md"`. Use `read_text(encoding="utf-8")` and three `assert "<string>" in text` lines.
- Do NOT add a new event name; `review_skipped` is the established event. Only the `reason` field's enum grows.
- The `commit-task --reviewer none --reviewer-verdict ""` form is already documented at line ~1075. Cite it; do not duplicate the bash example.
