# TASK-004 — Completed-Work Preservation Principle text + shared awaiting-user pause subroutine

## Goal

Completed-Work Preservation Principle text + shared awaiting-user pause subroutine

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

- SKILL.md `## Rules` section gains a new top bullet titled **Completed-Work Preservation Principle**, with the exact text from §2 of the design reference (`docs/analysis/2026-04-24_completed_work_preservation_principle.md`). The existing line-811 rule "Never auto-`fail-task` on the D.2a.5 halt path" is reworded into a sub-bullet labeled "Specific instance: D.2a.5 / D.2a.6 halt path — …" to cross-reference the new principle.
- A new subsection titled **Awaiting-user pause (shared control flow)** is inserted between Phase D.4 (`### D.4 — Phase D fail`) and Phase E (`### Phase E — Next batch`). The subsection body:
- Phase D.2a.5 (lines ~628–663) and Phase D.2a.6 (lines ~664–696) sections add a one-line cross-reference at their pause-step bullets: "(See **Awaiting-user pause** subroutine — same control flow.)" The literal command-block examples in those sections are kept (back-compat audit references); the cross-reference flags them as the canonical instance, not the only one.
- In every other phase that documents destructive auto-revert (Phase C, Phase D.4, D.2a binding-mode), add a sentence: "See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn." (Phase-specific behavior changes are TASK-005 / TASK-006 / TASK-007's job; this task only adds the cross-reference sentence.)
- `plan_ops.py audit` adds a check `principle_referenced` that scans SKILL.md for the literal string `Completed-Work Preservation Principle` and asserts (a) it appears in `## Rules`, (b) it appears in at least 4 other phase sections by cross-reference. Existing audit machinery in `plan_ops.py` provides the pattern; this is a non-strict check (`tier="default"`).

## Tasks

### TASK-004: Completed-Work Preservation Principle text + shared awaiting-user pause subroutine

- **Status:** done
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/scripts/plan_ops.py
- **Dependencies:** [001, 002, 003]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json`
- **Acceptance criteria:**
  - SKILL.md `## Rules` section gains a new top bullet titled **Completed-Work Preservation Principle**, with the exact text from §2 of the design reference (`docs/analysis/2026-04-24_completed_work_preservation_principle.md`). The existing line-811 rule "Never auto-`fail-task` on the D.2a.5 halt path" is reworded into a sub-bullet labeled "Specific instance: D.2a.5 / D.2a.6 halt path — …" to cross-reference the new principle.
  - A new subsection titled **Awaiting-user pause (shared control flow)** is inserted between Phase D.4 (`### D.4 — Phase D fail`) and Phase E (`### Phase E — Next batch`). The subsection body:
  - Phase D.2a.5 (lines ~628–663) and Phase D.2a.6 (lines ~664–696) sections add a one-line cross-reference at their pause-step bullets: "(See **Awaiting-user pause** subroutine — same control flow.)" The literal command-block examples in those sections are kept (back-compat audit references); the cross-reference flags them as the canonical instance, not the only one.
  - In every other phase that documents destructive auto-revert (Phase C, Phase D.4, D.2a binding-mode), add a sentence: "See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn." (Phase-specific behavior changes are TASK-005 / TASK-006 / TASK-007's job; this task only adds the cross-reference sentence.)
  - `plan_ops.py audit` adds a check `principle_referenced` that scans SKILL.md for the literal string `Completed-Work Preservation Principle` and asserts (a) it appears in `## Rules`, (b) it appears in at least 4 other phase sections by cross-reference. Existing audit machinery in `plan_ops.py` provides the pattern; this is a non-strict check (`tier="default"`).
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/scripts/plan_ops.py`

**Description:**
Pure-prose task that establishes the canonical principle text, the shared subroutine spec, and the cross-references that subsequent tasks will lean on. Lands before the behavior changes so that TASK-005 / TASK-006 / TASK-007 / TASK-008 each have a single source of truth to point at when rewriting their phase-specific prose. The audit check enforces that the principle stays referenced from the relevant phase sections — drift protection. No behavior changes in this task; only documentation and the non-strict audit check.

**Scope boundary.** This task does NOT modify Phase C (TASK-006), Phase D.4 (TASK-005), D.2a binding-mode (TASK-007), or `reconcile_batch` (TASK-008). It only adds the principle text, the shared subroutine spec, the cross-reference sentences in those phases, and the audit check.
