# TASK-007 — D.2a binding-mode reinterpretation (no escape hatch)

## Goal

D.2a binding-mode reinterpretation (no escape hatch)

## Context

The current `/implement-plan` orchestrator silently destroys work in five gap paths: Phase C (implementer failure → `git restore` of touched files), Phase D.4 (review-stage failure → `git restore` even though implementation succeeded), D.2a binding-mode (Codex `needs-rework` under `--codex-review-binding` → immediate `fail-task`), D.2b retry failure (inherits D.4), and `reconcile_batch` (Codex out-of-scope writes auto-restored at the batch-join barrier). The D.2a.5 / D.2a.6 retry-failure paths already implement the correct halt-with-pause pattern (SKILL.md hard rule line 811); this plan extends that pattern across the remaining paths.

The design reference (`docs/analysis/2026-04-24_completed_work_preservation_principle.md`) was reviewed in v1 by both Codex (`needs-rework`) and Gemini (`ship-with-fixes`). Their findings landed in v2, which the user signed off on with explicit decisions on the four open questions:

1. **`--authorization-source`** is **required** at the CLI (no `legacy` default; missing flag is a hard error). Closes the regression vector where future contributors add a `fail-task` call without thinking through the principle.
2. **Non-TTY default** is **refuse-with-error** (`unattended-revert-policy-required`). Forces an explicit policy decision per run environment; loud failure beats silent destruction.
3. **Binding-mode reinterpretation** ships in this PR alongside its test/doc co-updates (single mental-model change, no doc-drift window).
4. **No `--codex-review-binding-destructive` back-compat flag.** The contract for `--codex-review-binding` changes cleanly: from "auto-fail-task" to "binding for the commit decision; pause the run and return control to the user". Anyone relying on the legacy fail-fast in cron/CI must adopt `--unattended-revert-policy fail-fast` instead.

Three structural additions support the new behavior:

- **Shared awaiting-user pause control-flow subroutine** (factored out of D.2a.5/D.2a.6 into one block). Per-stage payloads stay declared at each call site; only the control flow is shared.
- **First-class `paused` plan-status.** `batch-next` (`plan_ops.py:2738, 2805`), `block-dependents`, `update-plan-header`, `lint-plans`, and `mutate_task_status` all gain awareness of the new state. Without this, a paused task left as `pending` would be re-picked by the scheduler on the next batch round and overwrite the preserved work.
- **D.4 rescue (single-shot, terminal).** Before D.4 halts, the orchestrator dispatches `plan-remediator` once to attempt an in-place fix on the reviewer's findings. On rescue success → re-review → commit with `--d4-rescue-tag`. On rescue failure or post-rescue re-review failure → halt-with-pause. Explicitly distinct machinery from D.2a.6 narrow-remediation: separate dispatch template, separate event types (`d4_rescue_start` / `d4_rescue_done`), separate commit tag (`[d4-rescue]`), separate input key on the remediator (`rescue_findings[]`, NOT `load_bearing_findings[]`).

This plan does NOT touch: `cmd_commit_task`'s metadata-only rollback (preserves work — already correct), the Codex dispatch wrapper's bounded delta-cleanup (operates within a single dispatch — already correct), plan-stage halts (touch only plan markdown, never code), or dry-run mode.

### Decisions folded in

1. **Auth flag is a hard wall.** `cmd_fail_task` argparse declares `--authorization-source` as `required=True` with a closed `choices=[...]` enum. Missing → `errors[*].code = "authorization-source-required"`. Allowed values evolve as later tasks introduce new authorized paths; the initial enum (TASK-001) covers exactly what current call sites need, and TASK-005 / TASK-006 / TASK-007 / TASK-008 each extend the enum when they add a new path.
2. **`paused` status is first-class, not an event-derived computation.** `batch-next` reads task status directly; deferring to a run-log scan would add latency and a new failure mode. The plan-status enum gets one new value; consumers get a one-line update each.
3. **D.4 rescue is single-shot.** A second rescue is structurally forbidden — the rescue branch halts on any non-success outcome rather than recursing. This closes Gemini's infinite-loop concern.
4. **`reconcile_batch` out-of-scope handling exposes four user options** in the awaiting-user payload: widen-plan / in-place-fix / keep-and-commit / revert. The user selects in their next conversation turn; the orchestrator does not pre-decide.
5. **`--codex-review-binding` is a behavior-changing flag, not a deprecation.** TASK-007 updates the CLI help text, the SKILL.md prose at `:626` and `:666`, and the test at `test_plan_ops.py:8513–8524` in a single commit. No grace period.
6. **The auth flag enum is hand-maintained across tasks.** Each task that introduces a new authorized path appends its value to the `choices=[...]` list AND updates the comment block above it. There is no central registry — this is explicitly chosen for readability over factor-out tax. TASK-009's audit drift check enforces that every `fail-task` invocation in SKILL.md passes a value from the enum.
7. **`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` is the design-doc target.** Located under `docs/plans/` (not `docs/`) per existing repo layout.

## Verification

- SKILL.md line ~137–138 (CLI help table for `--codex-review-binding`) is rewritten. Old text: "Codex critical on Claude goes straight to fail-task; no §8.4 third-opinion escalation". New text: "Codex `needs-rework` on Claude code is binding for the commit decision (no §8.4 third-opinion escalation, no D.2a.5/D.2a.6 retry); orchestrator marks the task `paused` and halts for user instruction. Use `--unattended-revert-policy fail-fast` to opt into auto-fail-task instead." Exact wording is the implementer's call as long as it conveys: (a) binding for the commit decision, (b) no D.5/D.2a.5/D.2a.6, (c) pauses by default, (d) `--unattended-revert-policy fail-fast` is the cron/CI workaround.
- SKILL.md line ~626 (D.2a routing) is rewritten. Old text: "`--codex-review-binding` skips D.2a entirely — binding mode means `needs-rework` → immediate `fail-task` with NO D.5, NO D.2a.5, and NO D.2a.6." New text: "`--codex-review-binding` skips D.2a third-opinion + retries — binding mode means `needs-rework` is binding for the commit decision and the orchestrator MUST mark the task `paused` and call **Awaiting-user pause** subroutine with `stage=post_binding_block` (NO D.5, NO D.2a.5, NO D.2a.6). User decides disposition in next turn (revert / hand-fix / accept-as-is via `commit-task` with override rationale). Under `--unattended-revert-policy fail-fast`, the orchestrator calls `fail-task --authorization-source unattended-fail-fast --stage review --reason 'codex-review-binding fail-fast'` instead of pausing."
- SKILL.md line ~666 (D.2a.6 exemption phrasing — `--codex-review-binding skips this entire section: binding mode means needs-rework → immediate fail-task with NO D.5, NO D.2a.5, and NO D.2a.6.`) is rewritten to reflect the new behavior: "`--codex-review-binding` skips this entire section: binding mode means `needs-rework` is binding for commit and the orchestrator pauses for user instruction (NO D.5, NO D.2a.5, NO D.2a.6). Under `--unattended-revert-policy fail-fast`, fail-task fires instead of pausing."
- `cmd_fail_task` `--authorization-source` enum: no NEW value needed for binding mode (the unattended-fail-fast path uses the existing `unattended-fail-fast` value introduced in TASK-006; the user-instructed-revert path uses `user-instruction`). Add a comment line in the enum docstring documenting that binding-mode pauses authorize via these existing values, not a dedicated one.
- `tests/scripts/test_plan_ops.py:8513–8524` test `test_section_documents_binding_mode_exemption` is REWRITTEN to assert the new contract:
- There is NO new flag `--codex-review-binding-destructive`. The contract for `--codex-review-binding` changes in place. Anyone relying on the legacy fail-fast behavior must adopt `--unattended-revert-policy fail-fast`.

## Tasks

### TASK-007: D.2a binding-mode reinterpretation (no escape hatch)

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (`cmd_fail_task` enum extends with `binding-mode-user-instruction` if needed; otherwise no plan_ops.py changes)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (CLI help line 137–138; D.2a routing prose around line 626; D.2a.6 exemption phrasing around line 666)
  - tests/scripts/test_plan_ops.py (rewrite `test_section_documents_binding_mode_exemption` at lines ~8513–8524)
- **Dependencies:** [001, 002, 003, 004]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "binding_mode or BindingMode or codex_review_binding or D2aBinding or test_section_documents_binding_mode_exemption"`
- **Acceptance criteria:**
  - SKILL.md line ~137–138 (CLI help table for `--codex-review-binding`) is rewritten. Old text: "Codex critical on Claude goes straight to fail-task; no §8.4 third-opinion escalation". New text: "Codex `needs-rework` on Claude code is binding for the commit decision (no §8.4 third-opinion escalation, no D.2a.5/D.2a.6 retry); orchestrator marks the task `paused` and halts for user instruction. Use `--unattended-revert-policy fail-fast` to opt into auto-fail-task instead." Exact wording is the implementer's call as long as it conveys: (a) binding for the commit decision, (b) no D.5/D.2a.5/D.2a.6, (c) pauses by default, (d) `--unattended-revert-policy fail-fast` is the cron/CI workaround.
  - SKILL.md line ~626 (D.2a routing) is rewritten. Old text: "`--codex-review-binding` skips D.2a entirely — binding mode means `needs-rework` → immediate `fail-task` with NO D.5, NO D.2a.5, and NO D.2a.6." New text: "`--codex-review-binding` skips D.2a third-opinion + retries — binding mode means `needs-rework` is binding for the commit decision and the orchestrator MUST mark the task `paused` and call **Awaiting-user pause** subroutine with `stage=post_binding_block` (NO D.5, NO D.2a.5, NO D.2a.6). User decides disposition in next turn (revert / hand-fix / accept-as-is via `commit-task` with override rationale). Under `--unattended-revert-policy fail-fast`, the orchestrator calls `fail-task --authorization-source unattended-fail-fast --stage review --reason 'codex-review-binding fail-fast'` instead of pausing."
  - SKILL.md line ~666 (D.2a.6 exemption phrasing — `--codex-review-binding skips this entire section: binding mode means needs-rework → immediate fail-task with NO D.5, NO D.2a.5, and NO D.2a.6.`) is rewritten to reflect the new behavior: "`--codex-review-binding` skips this entire section: binding mode means `needs-rework` is binding for commit and the orchestrator pauses for user instruction (NO D.5, NO D.2a.5, NO D.2a.6). Under `--unattended-revert-policy fail-fast`, fail-task fires instead of pausing."
  - `cmd_fail_task` `--authorization-source` enum: no NEW value needed for binding mode (the unattended-fail-fast path uses the existing `unattended-fail-fast` value introduced in TASK-006; the user-instructed-revert path uses `user-instruction`). Add a comment line in the enum docstring documenting that binding-mode pauses authorize via these existing values, not a dedicated one.
  - `tests/scripts/test_plan_ops.py:8513–8524` test `test_section_documents_binding_mode_exemption` is REWRITTEN to assert the new contract:
  - There is NO new flag `--codex-review-binding-destructive`. The contract for `--codex-review-binding` changes in place. Anyone relying on the legacy fail-fast behavior must adopt `--unattended-revert-policy fail-fast`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Reinterprets `--codex-review-binding`: from "auto-fail-task on Codex `needs-rework`" to "binding for the commit decision; pause for user instruction". The user explicitly chose to break this contract cleanly (no back-compat escape-hatch flag). Cron/CI users who relied on the legacy fail-fast path must now pass `--unattended-revert-policy fail-fast` (introduced in TASK-003) to get the same behavior. The three SKILL.md sections that documented the old contract (CLI help, D.2a routing, D.2a.6 exemption) are co-updated in this single task to prevent doc-drift; the test that asserted the old contract is rewritten to assert the new one. No new auth-source enum value is needed because binding-mode pause uses the existing `unattended-fail-fast` (for the cron/CI fall-through) and `user-instruction` (for the user-resolves-the-pause path) values.

**Migration callout (for the run summary banner).** The first /implement-plan invocation that uses `--codex-review-binding` after this task lands SHOULD log a one-time `binding_mode_contract_changed_notice` event at run_start with a one-line message reminding the operator that binding-mode now pauses by default. Implementer's discretion whether to add this; nice-to-have, not load-bearing.
