# TASK-008 — `reconcile_batch` out-of-scope pause (G10)

## Goal

`reconcile_batch` out-of-scope pause (G10)

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

- `reconcile_batch` (`plan_ops.py` lines ~1882–1958) gains a new `out_of_scope_policy` parameter (default `"pause"`; alternative `"reconcile-and-revert"` preserves legacy behavior). The function reads the parameter from a new CLI flag `--out-of-scope-policy <pause|reconcile-and-revert>` on the `reconcile-batch` subcommand.
- Under `out_of_scope_policy == "pause"` (default), for each envelope with `out_of_scope_observed == true`:
- Under `out_of_scope_policy == "reconcile-and-revert"`, current behavior is preserved unchanged (existing tests stay green).
- `reconciliation_failed` outcome (residual dirt after reconcile-and-revert) continues to hard-halt the run regardless of policy. No change to that path.
- The Phase 0 `--unattended-revert-policy` pin (from TASK-003) determines the orchestrator's default for this flag: `pause` → `--out-of-scope-policy pause`; `fail-fast` and `preserve-only` → `--out-of-scope-policy reconcile-and-revert`. Documented in SKILL.md.
- SKILL.md Phase B reconciliation section (around line 549, the "At the batch join barrier..." paragraph) is updated:
- `cmd_fail_task` enum extends with `reconcile-out-of-scope-user-instruction` (used when the user instructs revert in their next turn for an out-of-scope-paused task).
- New tests:

## Tasks

### TASK-008: `reconcile_batch` out-of-scope pause (G10)

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (`reconcile_batch` function lines ~1882–1958; new outcome value; `cmd_fail_task` enum extends with `reconcile-out-of-scope-user-instruction`; `ALLOWED_LOG_EVENTS` if needed; new flag `--out-of-scope-policy` on the `reconcile-batch` subcommand)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase B reconciliation section, currently around line 549; orchestrator integration)
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [001, 002, 003, 004]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "reconcile_batch and (out_of_scope or paused or OutOfScopePause)"`
- **Acceptance criteria:**
  - `reconcile_batch` (`plan_ops.py` lines ~1882–1958) gains a new `out_of_scope_policy` parameter (default `"pause"`; alternative `"reconcile-and-revert"` preserves legacy behavior). The function reads the parameter from a new CLI flag `--out-of-scope-policy <pause|reconcile-and-revert>` on the `reconcile-batch` subcommand.
  - Under `out_of_scope_policy == "pause"` (default), for each envelope with `out_of_scope_observed == true`:
  - Under `out_of_scope_policy == "reconcile-and-revert"`, current behavior is preserved unchanged (existing tests stay green).
  - `reconciliation_failed` outcome (residual dirt after reconcile-and-revert) continues to hard-halt the run regardless of policy. No change to that path.
  - The Phase 0 `--unattended-revert-policy` pin (from TASK-003) determines the orchestrator's default for this flag: `pause` → `--out-of-scope-policy pause`; `fail-fast` and `preserve-only` → `--out-of-scope-policy reconcile-and-revert`. Documented in SKILL.md.
  - SKILL.md Phase B reconciliation section (around line 549, the "At the batch join barrier..." paragraph) is updated:
  - `cmd_fail_task` enum extends with `reconcile-out-of-scope-user-instruction` (used when the user instructs revert in their next turn for an out-of-scope-paused task).
  - New tests:
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Implements G10, the flagship case: when the Codex implementer wrote files outside the task's declared `Files:` list, the wrapper-bounded cleanup is replaced by a per-task pause that gives the user four explicit options (widen the plan, fix-in-place, keep-and-commit, or revert). This directly matches the user's framing — "doesn't matter how 'out of scope' the necessary changes might be, we need to try to fix them in place to preserve effort". The legacy reconcile-and-revert behavior remains available via the new `--out-of-scope-policy reconcile-and-revert` flag for the cron/CI case (when `--unattended-revert-policy` is `fail-fast` or `preserve-only`). Other tasks in the same batch proceed independently — the pause is per-task, not per-batch, so a single out-of-scope write doesn't stall the whole batch.

**Implementation notes.** `reconcile_batch` is a Python function that returns a results list; the per-task `mutate_task_status` mutation requires knowing the task's plan_file. The current signature takes `batch_envelopes` and `repo_root`; extend it with `plans_dir` (or per-envelope `plan_file` references) so the mutation can target the right file in directory mode. Inspect the existing code to determine the cleanest extension.

## Execution log — 20260426T062115 (paused)

Starting SHA: `321251f3434e600abae8e021802ebe8ceb1df976`  → Ending SHA: `021ff7f8cf73829ed93de3f236b9116fef2d5a75`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-008 | claude | codex | needs-rework (binding 2nd pass: path-traversal) | (paused — work in tree) | D.2a.5 awaiting-user halt |
