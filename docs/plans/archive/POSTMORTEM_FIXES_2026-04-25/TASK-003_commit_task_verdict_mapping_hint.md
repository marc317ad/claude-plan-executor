# TASK-003 — `commit-task` D.5 verdict-mapping help hint + sugar form

## Goal

Reduce the friction the orchestrator hit on the first commit attempt for TASK-017: `commit-task --reviewer codex --reviewer-verdict needs-rework` was rejected with `uncommittable-reviewer-verdict` because, per `SKILL.md:802`, the binding reviewer when D.5 overrules Codex is `claude` with verdict `ship-with-fixes` (or `ship`). The mapping is documented but discoverable only by following the cross-reference chain. Surface it inline in `--help` and accept a sugar form.

## Context

**The friction.** When D.5 returns `ship` over Codex's `needs-rework`, the orchestrator must invoke `commit-task --reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` (Codex's `needs-rework` is recorded as a minor finding with `disposition: dismissed`). On a fresh run the operator naturally tries `--reviewer codex --reviewer-verdict needs-rework --disagreement-tag` and hits the whitelist rejection.

**The two complementary fixes.**

1. **Inline help hint.** When `_validate_reviewer_payload` rejects a `needs-rework` Codex verdict, include a one-line tip in the error message pointing at the D.2a routing rule and the `--reviewer claude --reviewer-verdict ship-with-fixes` form.
2. **Sugar form (optional).** Accept `--reviewer codex --reviewer-verdict needs-rework --disagreement-tag` as a shorthand that internally remaps to the binding D.5 reviewer + ship-class verdict, with the Codex `needs-rework` recorded as a `disposition: dismissed` finding. This requires `--d5-verdict {ship|ship-with-fixes}` to be present so the remap is unambiguous.

**Scope.** Implementer can ship (1) alone (low risk, high value) or (1)+(2). The AC requires (1); (2) is gated on a "no-regression" check against existing commit-task tests.

## Verification

- Running `commit-task --reviewer codex --reviewer-verdict needs-rework --disagreement-tag <args>` without a D.5 verdict surfaces an error message that names the correct binding form: `--reviewer claude --reviewer-verdict ship-with-fixes` (plus the `--d5-verdict` argument) and cites `SKILL.md:802`.
- (Optional sugar) Running `commit-task --reviewer codex --reviewer-verdict needs-rework --disagreement-tag --d5-verdict ship` succeeds and produces the same commit shape as the canonical form: commit body annotated with `[disagreement]`, the Codex `needs-rework` recorded in the finding ledger as `disposition: dismissed`, the binding reviewer recorded as `claude` with verdict `ship`.
- New unit tests in `tests/scripts/test_plan_ops.py` cover both the error-message hint and (if shipped) the sugar form.
- `commit-task --help` text mentions the D.5 / D.2a routing in a one-line "see also" pointing at the SKILL anchor.
- All existing `test_plan_ops.py` commit-task tests stay green.

## Tasks

### TASK-003: commit-task verdict-mapping hint + sugar form

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — `_validate_reviewer_payload` error message; optional `cmd_commit_task` sugar branch)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — anchor at line ~802 if any wording is sharpened)
  - `tests/scripts/test_plan_ops.py` (regression tests)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k commit_task`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:790-820`
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_validate_reviewer_payload`
  - `plugins/plan-executor/scripts/plan_ops.py::cmd_commit_task`
- **Acceptance criteria:**
  - When `_validate_reviewer_payload` rejects `--reviewer codex --reviewer-verdict needs-rework` with `uncommittable-reviewer-verdict`, the error message includes a `hint:` key naming the canonical form (`--reviewer claude --reviewer-verdict ship-with-fixes` or `ship`) AND citing the SKILL.md anchor (`§D.2a routing` or equivalent line reference).
  - The hint is structured (`hint` field in the JSON error envelope) AND human-readable (suffixed to the stderr message).
  - (Optional, scope-gated) `commit-task` accepts `--reviewer codex --reviewer-verdict needs-rework --disagreement-tag --d5-verdict {ship|ship-with-fixes}` as a sugar form. The sugar form internally remaps to the canonical binding form and records the Codex `needs-rework` in the finding ledger as `disposition: dismissed`. If the implementer ships the sugar form, all existing commit-task tests continue to pass and a new test covers the sugar→canonical equivalence (same commit body, same finding ledger, same plan-file status update).
  - If the sugar form is NOT shipped (scope deferral), the AC is satisfied by the hint alone — the implementer records the deferral in the report.
  - `commit-task --help` text gains a one-line "see also: SKILL.md §D.2a for D.5-verdict-driven binding-reviewer mapping" pointer.
- **Reversion guidance:** revert hint string + sugar branch (if shipped); whitelist rejection is unchanged on revert.

**Description:**
Surface the D.5 / D.2a binding-reviewer rule inline in `commit-task` error output so the next operator who hits the rejection knows the canonical form without chasing cross-references. Optionally accept the natural-but-rejected form as a sugar input that internally remaps to the canonical binding form.

**Implementation notes:**
- The hint is structured: emit it on the JSON error envelope so machine consumers (and the orchestrator's run-log) capture it.
- The sugar form's `--d5-verdict` requirement is non-negotiable: without it, the remap is ambiguous.
- Resist the temptation to silently coerce the rejection without the `--d5-verdict` argument; that would mask cases where the operator genuinely meant the canonical form.
- If the sugar form expands the test surface uncomfortably, ship the hint alone — it captures most of the friction-reduction value.

## Execution log — 20260426T032452 (success)

Starting SHA: `4220c0aa16b60579f826d4d8a8ed3150cc7d39a4`  → Ending SHA: `5d5a0637e945998fe546965c0f7764ebf3b53b71`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003 | claude | codex | clean | a877371e |  |
