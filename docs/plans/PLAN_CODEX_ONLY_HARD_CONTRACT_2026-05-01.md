# PLAN: `--codex-only` Hard Implementation Contract

**Status:** Draft  
**Date:** 2026-05-01  
**Owner:** plan-executor  
**Target:** `/implement-plan` orchestration contract  
**Primary files:**  
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
- `plugins/plan-executor/scripts/plan_ops.py`
- `plugins/plan-executor/scripts/review_route_input_schema.json`
- `plugins/plan-executor/scripts/review_route_output_schema.json`
- `plugins/plan-executor/skills/implement-plan/run-log-schema.md`
- `tests/scripts/test_plan_ops.py`
- `tests/scripts/test_implement_plan_directory_smoke.py`
- `tests/scripts/test_skill_dispatch_implementer.py`

## Problem

Today `--codex-only` is documented as a schedule filter:

> `--codex-only            Filter schedule to codex tasks`

That wording and the current Phase 1 step-3 prose mean two things that are not strong enough for operator intent:

1. The flag drops tasks classified as Claude instead of forcing selected tasks to run through Codex.
2. The Phase B Codex failure path still permits the orchestrator to log `fallback_used` and re-dispatch implementation to Claude once.

The second behavior is the immediate problem. A run can be invoked with:

```bash
/implement-plan docs\plans\PHASE_1_5_STATE_MACHINE --task-ids 001 --skip-plan-review --codex-only
```

and still fall back to Claude implementation after a Codex timeout because the Phase B rule says:

> `outcome ∈ {failure, timeout, parse_error, scope_violation}` → log `fallback_used`; re-dispatch to Claude once.

That makes `--codex-only` unsafe as an operator assertion. It is only a selection hint, not a hard implementation contract.

The deeper issue is that "regardless of retries" covers more than Phase B fallback. Existing D.2b routing for Codex-implemented work rejected by a Claude reviewer performs a Claude role-swap re-implementation. Under a true `--codex-only` contract, that retry is also forbidden.

## Goal

Make `--codex-only` mean:

> Every implementation attempt in this run MUST be performed by Codex. Claude may still be used for non-implementation roles that are part of the normal cross-family workflow, such as `code-reviewer`, `plan-reviewer` under `claude_only`, `plan-review-triage`, `plan-author`, or `plan-analyst`, but Claude MUST NOT implement, remediate, role-swap, rescue, or otherwise write task code.

The flag must override task metadata, classifier output, and retry routing. If the operator selects a task with `--task-ids`, every selected task and every transitive prerequisite included by the filter must be forced to Codex implementation for that run.

## Non-Goals

- Do not implement automatic repeated Codex retries inside one `/implement-plan` run.
- Do not change `--claude-only` semantics.
- Do not change Codex plan-review or Codex cross-review fallback to Gemini. `--codex-only` is about implementation only; `--allow-gemini-fallback` remains a reviewer fallback feature.
- Do not disable Claude as reviewer for Codex-implemented work. Cross-review still matters.
- Do not bypass plan review, cross-review, commit gates, scope checks, or test execution.
- Do not create a new slash-command flag if the existing `--codex-only` can carry the intended semantics.

## Design Principles

1. **Operator intent beats classifier output.** If the user passes `--codex-only`, classifier decisions and `Implementer:` metadata are advisory at most. The persisted schedule must show the effective implementer as Codex.
2. **No silent family switch.** A Codex implementation failure under `--codex-only` must not become Claude implementation through fallback, role-swap, remediation, or rescue.
3. **Preserve work on timeout/failure.** If Codex leaves a non-empty diff and cannot complete, pause through the existing awaiting-user flow. The next operator action can rerun `/implement-plan --skip-plan-review --codex-only`.
4. **Make the contract auditable.** The schedule, run log, and final summary must make it clear that `codex_only=true` was active and that Claude implementation fallback was suppressed if suppression occurred.
5. **Prefer central routing over prose-only exceptions.** Phase B still has prose routing, but D.2b already routes through `plan_ops.py review-route`; the hard contract must be represented in that route payload so tests can pin it.

## Current Behavior Summary

Relevant current contract excerpts:

- Parse arguments: `--codex-only` is a filter.
- Phase 1 step 3: `--codex-only` drops Claude tasks.
- Phase B Codex envelope failure: logs `fallback_used` and dispatches Claude once.
- D.2b: Codex implementation + Claude reviewer `needs-rework` dispatches `dispatch_role_swap`, which is a Claude implementation retry.
- `review-route` input has `claude_only`, but no `codex_only`.

The existing `claude_only` design is a useful precedent. `claude_only=true` is a hard no-Codex shell-out contract. This plan adds the symmetric hard no-Claude-implementation contract for `codex_only=true`, but intentionally keeps Claude review and triage roles available.

## Desired Semantics

### Flag Binding

Bind `codex_only` during Phase 0 from the literal `--codex-only` flag. Unlike `claude_only`, it must not be inferred from preflight.

If both `--codex-only` and `--claude-only` are present, halt before lock acquisition or before any schedule mutation. This is already documented as a mutex; this plan strengthens the wording but does not require a structural argparse checker unless tests show the prose-only mutex is insufficient.

If `--codex-only` is present and preflight reports `codex_available=false`, halt before Phase 1 schedule dispatch with a clear reason such as:

```text
codex_only_requested_but_codex_unavailable
```

Do not rewrite tasks to Claude in this case. Do not write a schedule file, dispatch the classifier, or mutate task status before this halt. The failure must happen after preflight has established `codex_available=false` and before the first schedule persistence or implementation dispatch.

### Schedule Forcing

The Phase 1 step-3 filter/override seam must become:

- `--claude-only` or `codex_available=false` without `--codex-only` -> rewrite `tasks[].agent = "claude"`.
- `--codex-only` -> rewrite `tasks[].agent = "codex"` for every task that remains after `--task-ids` filtering and dependency closure.
- `--task-ids` remains a task selection filter, not an agent filter.

Ordering:

1. Build merged manifest.
2. Run per-child classifier only if needed for normal mode metadata.
3. Apply `--task-ids` closure if present.
4. Apply `--codex-only` / `--claude-only` effective-agent rewrite to the filtered schedule.
5. Persist through `plan_ops__write_schedule`.

This ordering ensures a selected Claude-classified prerequisite is not dropped under `--codex-only`; it is included and forced to Codex.

### Phase B Implementation Failure

When `codex_only=true` and a Codex implementation wrapper returns:

- `failure`
- `timeout`
- `parse_error`
- `scope_violation`

the orchestrator must not log `fallback_used` and must not dispatch the Phase B Claude template.

Instead:

- If there is no non-empty work product to preserve, route through the existing Phase C failure path.
- If there is a non-empty tracked or untracked diff in or near the task scope, use the existing Awaiting-user pause pattern. The pause payload should explain that Claude fallback was suppressed because `codex_only=true`.

Preferred audit event:

- Add a new run-log event `fallback_suppressed` with fields:
  - `task_id`
  - `from: "codex"`
  - `to: "claude"`
  - `reason: <codex_outcome>`
  - `policy: "codex_only"`

If adding the event is too much for this plan, the minimum acceptable audit trail is an `awaiting_user` event whose payload contains `fallback_suppressed: true` and `policy: "codex_only"`. The stronger plan below includes `fallback_suppressed` because it makes logs easier to inspect.

### Phase D.2b Role-Swap

When `codex_only=true`, the D.2b role-swap path is forbidden. A Claude reviewer `needs-rework` on Codex-implemented work must not dispatch Claude implementation.

`plan_ops.py review-route` must receive `codex_only` in its input payload and route:

```json
{
  "implementer": "codex",
  "reviewer": "claude",
  "reviewer_envelope": {"verdict": "needs-rework"},
  "flags": {"codex_only": true}
}
```

to `pause_awaiting_user`, not `dispatch_role_swap`.

Suggested pause payload:

```json
{
  "stage": "post_codex_only_rework_block",
  "policy_kind": "codex_only",
  "reviewer": "claude",
  "reviewer_verdict": "needs-rework",
  "claude_role_swap_suppressed": true,
  "summary": "<review summary>",
  "findings": [...]
}
```

If `unattended_revert_policy=fail-fast` is active, this may route to `fail` instead of pause, but only if that is consistent with the Completed-Work Preservation Principle. Default `pause` must preserve work.

### Remediation and D.4 Rescue

This plan must explicitly forbid Claude implementation through these paths under `codex_only=true`:

- Phase B fallback to Claude.
- D.2b role-swap.
- D.2a.5 bounded remediation by `plan-implementer`.
- D.2a.6 narrow remediation by `plan-remediator` if that agent writes code.
- D.4 rescue by `plan-remediator`.

The common rule:

> When `codex_only=true`, no Claude agent may be dispatched in a write-authorized implementation/remediation/rescue role. Review-only, triage-only, and plan-author roles are allowed if they do not edit task code.

Practically, D.2a.5/D.2a.6 apply to Claude-implemented work rejected by Codex/Gemini, so they should be structurally unreachable after the schedule is forced to Codex. Still, the SKILL must state the defensive rule so a bad route payload cannot silently violate the flag.

## Task Breakdown

### TASK-001: Document `codex_only` as a hard implementation contract

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (modify, if needed for callouts)
  - `tests/scripts/test_skill_dispatch_implementer.py` (modify or add focused assertions)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_dispatch_implementer.py`
- **Acceptance criteria:**
  - CLI surface describes `--codex-only` as "force selected tasks and prerequisites to Codex implementation; no Claude implementation fallback or role-swap."
  - Mutual exclusions section states:
    - `--codex-only` + `--claude-only` halts.
    - `--codex-only` + `codex_available=false` halts instead of rewriting tasks to Claude.
  - Rules section adds a hard rule:
    - When `codex_only=true`, the orchestrator MUST NOT invoke `plan_claude_dispatch.py` for implementation, role-swap, remediation, narrow remediation, or rescue.
    - Claude review/triage/plan-author roles are not forbidden by this flag unless they write task code.
  - Phase B Codex failure prose says fallback to Claude is suppressed under `codex_only=true`.
  - D.2b prose says role-swap is suppressed under `codex_only=true`.
  - Tests pin that SKILL.md contains the hard-rule language and no longer describes `--codex-only` as only "drop claude tasks."

**Implementation notes:**  
Use `claude_only` wording as the model, but avoid saying `codex_only` forbids every Claude call. It forbids Claude implementation. A total Claude ban would incorrectly disable the standard Claude reviewer for Codex work.

**Reversion guidance:**  
Revert the SKILL/dispatch prose and the prose-pin tests. No runtime state is changed by this task alone.

---

### TASK-002: Force effective schedule agents to Codex under `--codex-only`

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify)
  - `tests/scripts/test_implement_plan_directory_smoke.py` (modify)
  - `tests/scripts/test_plan_ops.py` (modify only if a helper-level test is a better fit)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_directory_smoke.py -k "codex_only or task_ids or classifier"`
- **Acceptance criteria:**
  - Phase 1 step 3 changes from "`--codex-only` -> drop claude tasks" to "`--codex-only` -> rewrite all selected tasks' effective `agent` to `codex`."
  - `--task-ids` closure remains intact: dependencies pulled in by `filter-schedule` remain present and are also forced to Codex.
  - `codex_available=false` no longer rewrites tasks to Claude when `--codex-only` is set; it halts with `codex_only_requested_but_codex_unavailable`.
  - Existing `--claude-only` behavior remains unchanged when `--codex-only` is absent.
  - Tests cover a mixed-agent schedule where:
    - TASK-001 is Claude-classified.
    - TASK-002 is Codex-classified and depends on TASK-001.
    - Running the in-memory `--task-ids 002 --codex-only` path retains both tasks and rewrites both to `agent="codex"`.
  - Tests assert no prose path still instructs the orchestrator to drop Claude tasks under `--codex-only`.

**Implementation notes:**  
This is mostly orchestrator prose because the current schedule rewrite happens in SKILL.md, not in a single Python helper. If there is an existing test helper that simulates Phase 1 filtering, prefer adding the executable assertion there. Otherwise, add a prose-pin test to prevent regression.

**Reversion guidance:**  
Revert SKILL.md step-3 wording and the tests. This would restore selection-filter semantics.

---

### TASK-003: Suppress Phase B Claude fallback when `codex_only=true`

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify)
  - `plugins/plan-executor/scripts/plan_ops.py` (modify if adding `fallback_suppressed`)
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md` (modify if adding `fallback_suppressed`)
  - `tests/scripts/test_plan_ops.py` (modify if adding `fallback_suppressed`)
  - `tests/scripts/test_skill_dispatch_implementer.py` (modify)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_skill_dispatch_implementer.py -k "fallback_suppressed or fallback_used or codex_only"`
- **Acceptance criteria:**
  - Phase B Codex envelope classification has two separate branches:
    - Normal mode: existing `fallback_used` -> Claude fallback once.
    - `codex_only=true`: no Claude fallback; route to Phase C or awaiting-user pause.
  - If adding an event, `ALLOWED_LOG_EVENTS` includes `fallback_suppressed`.
  - If adding an event, `run-log-schema.md` documents `fallback_suppressed` with the same field contract as `plan_ops.py`.
  - `fallback_suppressed` validates fields:
    - `task_id`: non-empty string.
    - `from`: `"codex"`.
    - `to`: `"claude"`.
    - `reason`: one of `failure|timeout|parse_error|scope_violation`.
    - `policy`: `"codex_only"`.
  - Tests prove `fallback_used` remains allowed for normal mode and `fallback_suppressed` is allowed for codex-only mode.
  - SKILL.md explicitly tells the orchestrator to rerun `/implement-plan --skip-plan-review --codex-only` in a later operator turn if the user wants another Codex attempt.
  - No test or prose suggests automatic same-run Codex retry.

**Implementation notes:**  
The event addition is small but useful. If implementation discovers that adding `fallback_suppressed` causes a broad schema sweep, fall back to encoding the same fields in `awaiting_user.pause_payload` and document that choice in the task report.

**Reversion guidance:**  
Remove `fallback_suppressed` from `ALLOWED_LOG_EVENTS`, remove its `run-log-schema.md` entry, and restore normal Phase B fallback wording.

---

### TASK-004: Thread `codex_only` into `review-route` and block D.2b role-swap

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (modify)
  - `plugins/plan-executor/scripts/review_route_input_schema.json` (modify)
  - `plugins/plan-executor/scripts/review_route_output_schema.json` (modify)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify)
  - `tests/scripts/test_plan_ops.py` (modify)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "review_route and codex_only"`
- **Acceptance criteria:**
  - `review_route_input_schema.json` accepts `flags.codex_only: boolean`.
  - `plan_ops.route()` reads `codex_only = bool(flags.get("codex_only", False))`.
  - For `implementer="codex"`, `reviewer="claude"`, `verdict="needs-rework"`, `role_swap=false`, and `flags.codex_only=true`, route returns `action="pause_awaiting_user"` instead of `dispatch_role_swap`.
  - Pause payload has:
    - `stage: "post_codex_only_rework_block"` or another explicitly documented stage.
    - `policy_kind: "codex_only"`.
    - `claude_role_swap_suppressed: true`.
    - Reviewer findings and summary preserved.
  - `review_route_output_schema.json` explicitly allows the new pause stage and policy kind:
    - `args.pause_payload.stage` includes `post_codex_only_rework_block`.
    - `args.policy_kind` includes `codex_only`.
    - `args.pause_payload.policy_kind` includes `codex_only`.
  - Existing D.2b behavior remains unchanged when `flags.codex_only=false`.
  - Existing `claude_only=true` route behavior is unchanged.
  - Tests cover:
    - Normal D.2b still dispatches role-swap.
    - `codex_only=true` suppresses role-swap.
    - Already-used role-swap still routes to existing terminal failure in normal mode.
    - Unknown or non-boolean `flags.codex_only` is either schema-rejected or defensively routed to `unknown_state`, matching local validator style.

**Implementation notes:**  
Do not introduce a new top-level `codex_only` field unless the existing `flags` object cannot carry it cleanly. `flags.codex_only` matches the current design where routing-affecting booleans live under `flags`.

**Reversion guidance:**  
Remove `flags.codex_only` schema support and restore D.2b role-swap routing.

---

### TASK-005: Add end-to-end contract smoke for `--codex-only`

- **Status:** Pending
- **Implementer:** codex
- **Priority:** medium
- **Files:**
  - `tests/scripts/test_implement_plan_directory_smoke.py` (modify)
  - `tests/scripts/test_skill_dispatch_e2e.py` (modify if better suited)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify only for small clarifications found by tests)
- **Dependencies:** TASK-002, TASK-003, TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_skill_dispatch_e2e.py -k "codex_only"`
- **Acceptance criteria:**
  - A smoke fixture or prose-pin test asserts the full contract:
    - Mixed classifier output plus `--codex-only` yields effective Codex agents.
    - Codex timeout/failure path does not dispatch Claude Phase B fallback.
    - D.2b role-swap is blocked by `review-route` when `flags.codex_only=true`.
  - The final summary contract is documented:
    - If any Claude implementation fallback was suppressed, include a short line naming suppressed task IDs and advising rerun with `--skip-plan-review --codex-only`.
  - The test suite continues to pass for the existing `--claude-only` tests.

**Implementation notes:**  
Prefer narrow tests over broad live-wrapper e2e. This is a contract smoke, not an actual Codex timeout integration test.

**Reversion guidance:**  
Remove the smoke tests and summary wording. Earlier tasks remain independently revertible.

## Verification Matrix

Run focused tests after each task:

```bash
venv/bin/python -m pytest tests/scripts/test_skill_dispatch_implementer.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_directory_smoke.py -k "codex_only or task_ids or classifier"
venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "review_route and codex_only"
```

Run broader regression after all tasks:

```bash
venv/bin/python -m pytest tests/scripts/test_skill_dispatch_implementer.py tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_skill_dispatch_e2e.py
venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "review_route or fallback_used or log_event"
```

Manual dry-run smoke after implementation:

```bash
claude -p --permission-mode auto "/implement-plan docs\plans\PHASE_1_5_STATE_MACHINE --task-ids 001 --skip-plan-review --codex-only --dry-run"
```

Expected dry-run properties:

- Schedule contains TASK-001.
- Effective task agent is `codex`.
- Run metadata includes `codex_only=true`.
- No Claude implementer dispatch is planned.

Manual unavailable-Codex smoke after implementation, if the preflight can be forced or stubbed:

```bash
claude -p --permission-mode auto "/implement-plan <fixture-plan> --task-ids <known-task> --skip-plan-review --codex-only"
```

with `codex_available=false` in the preflight fixture.

Expected unavailable-Codex properties:

- Run halts with `codex_only_requested_but_codex_unavailable`.
- No schedule file is written for the run.
- No classifier dispatch occurs.
- No task status is mutated.
- No Claude implementation dispatch occurs.

Manual failure-path smoke, if a deterministic Codex-timeout fixture exists:

```bash
claude -p --permission-mode auto "/implement-plan <fixture-plan> --task-ids <timeout-task> --skip-plan-review --codex-only"
```

Expected failure-path properties:

- No `fallback_used {from:"codex", to:"claude"}` event.
- Either `fallback_suppressed` appears or `awaiting_user` carries `fallback_suppressed: true`.
- No `claude_dispatch_start` for `agent:"plan-implementer"` on that task.

## Open Questions

1. Should `--codex-only` also disable Claude `plan-remediator` rescue when the rescue is purely mechanical and scoped? This plan says yes because rescue writes code.
2. Should a structural flag validator be added for `--codex-only` + `codex_available=false`? This plan keeps validation prose-driven unless implementation discovers an existing helper seam.
3. Should `fallback_suppressed` be a new log event or should suppression be represented only through `awaiting_user`? This plan prefers the new event for auditability.
4. Should `plan-author` be allowed under `--codex-only`? This plan allows it because it edits plans, not task code. If the operator wants "Codex only for every writer of any kind," that is a different flag.

## Risks

- **Risk: prose-only orchestration drift.** `--codex-only` behavior is partly specified in SKILL.md rather than enforced by a single Python state machine. Mitigation: add prose-pin tests and route-level tests where possible.
- **Risk: stale classifier expectations.** Some tests may assume `--codex-only` filters out Claude tasks. Mitigation: update tests to the new forced-agent semantics.
- **Risk: role-swap suppression creates more pauses.** This is intentional. The operator asked for Codex implementation even if completion requires rerun.
- **Risk: `codex_available=false` conflicts with user intent.** Under this plan, `--codex-only` wins by halting. This is safer than silently switching to Claude.

## Success Criteria

The plan is complete when:

- `--codex-only` is documented as a hard Codex implementation contract.
- Selected tasks and transitive prerequisites are forced to Codex implementation in the persisted schedule.
- Codex implementation failures under `--codex-only` do not dispatch Claude fallback.
- D.2b role-swap is suppressed under `--codex-only`.
- Tests pin the contract at prose, route, and schedule-smoke levels.
- Existing `--claude-only`, normal fallback, and normal D.2b behavior are preserved when `--codex-only` is absent.

## Gemini Validation

Validated with Gemini CLI (`gemini 0.40.1`) on 2026-05-01. Gemini's verdict was "approved for implementation with minor schema updates." The review called out three changes, all folded into this draft:

- Add `run-log-schema.md` to the `fallback_suppressed` task so the new event is documented as well as accepted by `plan_ops.py`.
- Make TASK-004 explicitly update `review_route_output_schema.json` enum values for `post_codex_only_rework_block` and `policy_kind: "codex_only"`.
- Add a verification path for `--codex-only` plus `codex_available=false` that halts before schedule writes or dispatch.

## Execution Log

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
