# TASK-003 — `--unattended-revert-policy` preflight + TTY refuse + orchestrator pin

## Goal

`--unattended-revert-policy` preflight + TTY refuse + orchestrator pin

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

- `cmd_preflight` argparse adds `--unattended-revert-policy` with `choices=["pause", "fail-fast", "preserve-only"]`. NOT required at the argparse layer — the requirement is conditional on TTY.
- Behavior: if `--unattended-revert-policy` is not provided AND `sys.stdin.isatty()` is False, `cmd_preflight` exits non-zero with `errors[*].code = "unattended-revert-policy-required"` and a message instructing the caller to pass an explicit value. If stdin IS a TTY and the flag is absent, default to `pause` and continue.
- `preflight --json` output adds the field `unattended_revert_policy` (string, one of the three values; reflects the resolved policy).
- SKILL.md Phase 0 preflight section documents the new flag, the TTY-refuse behavior, and instructs the orchestrator to pin the value as `$UNATTENDED_REVERT_POLICY` for the rest of the run (parallel to the existing `$PYTHON` pin pattern). Adds a one-paragraph note explaining what each value does (referencing the principle for the `pause` semantics; pointing later tasks for `fail-fast` and `preserve-only` semantics).
- The pin propagates to the awaiting-user pause subroutine (introduced in TASK-004) and to TASK-005/006/007/008 dispatch logic. In this task, only the preflight and the SKILL.md doc need to land; downstream consumers come online in their own tasks.
- New tests:

## Tasks

### TASK-003: `--unattended-revert-policy` preflight + TTY refuse + orchestrator pin

- **Status:** done
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase 0 preflight section; pin reference)
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [001]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "preflight and (unattended or revert_policy or UnattendedRevert)"`
- **Acceptance criteria:**
  - `cmd_preflight` argparse adds `--unattended-revert-policy` with `choices=["pause", "fail-fast", "preserve-only"]`. NOT required at the argparse layer — the requirement is conditional on TTY.
  - Behavior: if `--unattended-revert-policy` is not provided AND `sys.stdin.isatty()` is False, `cmd_preflight` exits non-zero with `errors[*].code = "unattended-revert-policy-required"` and a message instructing the caller to pass an explicit value. If stdin IS a TTY and the flag is absent, default to `pause` and continue.
  - `preflight --json` output adds the field `unattended_revert_policy` (string, one of the three values; reflects the resolved policy).
  - SKILL.md Phase 0 preflight section documents the new flag, the TTY-refuse behavior, and instructs the orchestrator to pin the value as `$UNATTENDED_REVERT_POLICY` for the rest of the run (parallel to the existing `$PYTHON` pin pattern). Adds a one-paragraph note explaining what each value does (referencing the principle for the `pause` semantics; pointing later tasks for `fail-fast` and `preserve-only` semantics).
  - The pin propagates to the awaiting-user pause subroutine (introduced in TASK-004) and to TASK-005/006/007/008 dispatch logic. In this task, only the preflight and the SKILL.md doc need to land; downstream consumers come online in their own tasks.
  - New tests:
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Adds the per-run policy that controls how the new pause paths behave under unattended execution. Per user decision: refuse-with-error when stdin is not a TTY and no policy is provided — loud failure beats silent destruction in cron/CI contexts. The three values (`pause | fail-fast | preserve-only`) are defined in the design reference §4.1.j; `pause` is the default for interactive runs and the new behavior across G1/G2/G3/G10. `fail-fast` restores the legacy auto-fail-task behavior for unattended runs that prefer fast failure over stalls. `preserve-only` is a compromise (logs the discarded diff to a salvage ref before fail-task). This task only wires up preflight and the SKILL.md pin; consumers (Phase C, D.4, D.2a, reconcile-batch) come online in their respective tasks.
