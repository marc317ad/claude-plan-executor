# TASK-005 — Phase D.4 rescue + `--d4-rescue-tag` commit + dispatch template + plan-remediator dual input mode

## Goal

Phase D.4 rescue + `--d4-rescue-tag` commit + dispatch template + plan-remediator dual input mode

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

- `cmd_commit_task` argparse adds `--d4-rescue-tag` (boolean flag). Argparse / post-parse validation enforces the following XOR rules:
- On successful commit with `--d4-rescue-tag`, the commit body carries a `[d4-rescue]` line (separate from `[remediation]` and `[narrow-remediation]`). The `commit_done` run-log event gains `d4_rescue_tag: true` (new key alongside existing `disagreement_tag`, `remediation_tag`, `narrow_remediation_tag`).
- `ALLOWED_LOG_EVENTS` (search by name) gains `d4_rescue_start` and `d4_rescue_done`. The existing per-event field validators (if any) accept the new event names.
- `cmd_fail_task` `--authorization-source` enum extends with `phase-d4-rescue-failed` (alongside the TASK-001 initial set).
- SKILL.md Phase D.4 (lines ~755–771) is rewritten:
- `dispatch-templates.md` gains a new `**Phase D.4-rescue**` template subsection. Inputs documented: `rescue_findings[]` (NOTE: distinct key from `load_bearing_findings[]`), `dismissed_findings: []` (literal empty list), task block, reviewer-source. Output contract: same shape as the existing plan-remediator report, with the `**Dismissed findings noted:**` section literally `(none — D.4 rescue does not carry dismissed findings)`.
- `agents/plan-remediator.md` updates the **Inputs** section to document dual modes:
- New tests in `tests/scripts/test_plan_ops.py`:

## Tasks

### TASK-005: Phase D.4 rescue + `--d4-rescue-tag` commit + dispatch template + plan-remediator dual input mode

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (`cmd_commit_task` argparse: new `--d4-rescue-tag` + XOR rules; `event_fields` extension for new tag; `ALLOWED_LOG_EVENTS` adds `d4_rescue_start` / `d4_rescue_done`; `cmd_fail_task` enum extends with `phase-d4-rescue-failed`)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase D.4 prose rewrite; new D.4 rescue protocol subsection; cross-reference to dispatch-templates)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (NEW Phase D.4-rescue dispatch template)
  - plugins/plan-executor/agents/plan-remediator.md (dual input mode: `load_bearing_findings[] + dismissed_findings[] + d5_summary` OR `rescue_findings[] + dismissed_findings: []`)
  - tests/scripts/test_plan_ops.py (commit-task tag, argparse XOR, run-log event allowlist)
- **Dependencies:** [001, 002, 003, 004]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "d4_rescue or D4Rescue or commit_task and rescue or commit_task_xor"`
- **Acceptance criteria:**
  - `cmd_commit_task` argparse adds `--d4-rescue-tag` (boolean flag). Argparse / post-parse validation enforces the following XOR rules:
  - On successful commit with `--d4-rescue-tag`, the commit body carries a `[d4-rescue]` line (separate from `[remediation]` and `[narrow-remediation]`). The `commit_done` run-log event gains `d4_rescue_tag: true` (new key alongside existing `disagreement_tag`, `remediation_tag`, `narrow_remediation_tag`).
  - `ALLOWED_LOG_EVENTS` (search by name) gains `d4_rescue_start` and `d4_rescue_done`. The existing per-event field validators (if any) accept the new event names.
  - `cmd_fail_task` `--authorization-source` enum extends with `phase-d4-rescue-failed` (alongside the TASK-001 initial set).
  - SKILL.md Phase D.4 (lines ~755–771) is rewritten:
  - `dispatch-templates.md` gains a new `**Phase D.4-rescue**` template subsection. Inputs documented: `rescue_findings[]` (NOTE: distinct key from `load_bearing_findings[]`), `dismissed_findings: []` (literal empty list), task block, reviewer-source. Output contract: same shape as the existing plan-remediator report, with the `**Dismissed findings noted:**` section literally `(none — D.4 rescue does not carry dismissed findings)`.
  - `agents/plan-remediator.md` updates the **Inputs** section to document dual modes:
  - New tests in `tests/scripts/test_plan_ops.py`:
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/agents/plan-remediator.md tests/scripts/test_plan_ops.py`

**Description:**
Implements the highest-value behavior change: D.4 stops being a destructive seam and becomes a try-rescue-then-pause seam. The rescue is single-shot and terminal — the structural protection against infinite loops Gemini flagged. The new commit-tag (`[d4-rescue]`) and event types (`d4_rescue_start` / `d4_rescue_done`) are deliberately distinct from D.2a.6's narrow-remediation machinery so audit trails stay legible. `plan-remediator` accepts both input modes via a key-based discriminator (`load_bearing_findings[]` vs `rescue_findings[]`), keeping the agent's contract explicit rather than overloading the existing key.

**Scope boundary.** This task does NOT touch Phase C (TASK-006), D.2a binding-mode (TASK-007), or `reconcile_batch` (TASK-008). It does NOT add `--d4-rescue-tag` cross-references in `dispatch-templates.md` outside the new Phase D.4-rescue template subsection.
