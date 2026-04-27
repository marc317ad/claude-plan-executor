# TASK-009 — dispatch-templates reinforcement + plan-implementer clause + design-doc propagation + audit drift check

## Goal

dispatch-templates reinforcement + plan-implementer clause + design-doc propagation + audit drift check

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

- `dispatch-templates.md` Phase B-rework template (around line 416, "the prior attempt is still in the working tree — it was NOT reverted" line) gets a new sentence appended: "Per the **Completed-Work Preservation Principle** (SKILL.md §Rules), you MUST NOT `git restore`, `git reset --hard`, delete files, or otherwise discard the prior implementation. If you cannot complete the rework without doing so, return `outcome=blocked` with a clear `reversion_guidance:` rationale; the orchestrator will halt for user instruction."
- `dispatch-templates.md` Phase B-narrow-remediation template (around line 466) gets the same addition, adapted to the touch-only contract (mention `scope-violation` outcome instead of `blocked`).
- `agents/plan-implementer.md` adds a new clause near the existing report-shape section: "**Phase B-rework preservation.** Per the Completed-Work Preservation Principle, on Phase B-rework you MUST NOT `git restore`, `git reset --hard`, delete files, or otherwise discard the prior implementation. If your rework cannot succeed without doing so, return `outcome=blocked` with `reversion_guidance:` describing what you would need; the orchestrator will halt for user instruction. Initial Phase B (no prior work to preserve) is unconstrained — this clause applies to rework only."
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` gains a top-level subsection (insert it near the existing §Hard rules / §Reversion content) titled "Completed-Work Preservation Principle" containing the §2 text from the design reference. A one-line cross-reference to SKILL.md `## Rules` is added so readers know where the canonical version lives.
- `plan_ops.py` `audit` subcommand gains two new strict-tier (or default-tier; implementer chooses based on existing convention — recommend strict for both since they're load-bearing) checks:
- The two new audit checks are listed in `plan_ops.py audit --list` output and round-trip through `--json` / `--report-file` outputs.
- New tests:

## Tasks

### TASK-009: dispatch-templates reinforcement + plan-implementer clause + design-doc propagation + audit drift check

- **Status:** pending
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (Phase B-rework template ~line 416 reinforcement; Phase B-narrow-remediation template ~line 466 reinforcement)
  - plugins/plan-executor/agents/plan-implementer.md (NEW Phase B-rework preservation clause)
  - plugins/plan-executor/scripts/plan_ops.py (`audit` subcommand: new `fail_task_authorization_source` and `principle_referenced` strict checks)
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md (propagate principle text)
  - tests/scripts/test_plan_ops.py (audit-check coverage)
- **Dependencies:** [001, 002, 003, 004, 005, 006, 007, 008]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json && python3 -m pytest tests/scripts/test_plan_ops.py -q -k "audit and (authorization_source or principle_referenced or AuditAuth)"`
- **Acceptance criteria:**
  - `dispatch-templates.md` Phase B-rework template (around line 416, "the prior attempt is still in the working tree — it was NOT reverted" line) gets a new sentence appended: "Per the **Completed-Work Preservation Principle** (SKILL.md §Rules), you MUST NOT `git restore`, `git reset --hard`, delete files, or otherwise discard the prior implementation. If you cannot complete the rework without doing so, return `outcome=blocked` with a clear `reversion_guidance:` rationale; the orchestrator will halt for user instruction."
  - `dispatch-templates.md` Phase B-narrow-remediation template (around line 466) gets the same addition, adapted to the touch-only contract (mention `scope-violation` outcome instead of `blocked`).
  - `agents/plan-implementer.md` adds a new clause near the existing report-shape section: "**Phase B-rework preservation.** Per the Completed-Work Preservation Principle, on Phase B-rework you MUST NOT `git restore`, `git reset --hard`, delete files, or otherwise discard the prior implementation. If your rework cannot succeed without doing so, return `outcome=blocked` with `reversion_guidance:` describing what you would need; the orchestrator will halt for user instruction. Initial Phase B (no prior work to preserve) is unconstrained — this clause applies to rework only."
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` gains a top-level subsection (insert it near the existing §Hard rules / §Reversion content) titled "Completed-Work Preservation Principle" containing the §2 text from the design reference. A one-line cross-reference to SKILL.md `## Rules` is added so readers know where the canonical version lives.
  - `plan_ops.py` `audit` subcommand gains two new strict-tier (or default-tier; implementer chooses based on existing convention — recommend strict for both since they're load-bearing) checks:
  - The two new audit checks are listed in `plan_ops.py audit --list` output and round-trip through `--json` / `--report-file` outputs.
  - New tests:
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/agents/plan-implementer.md plugins/plan-executor/scripts/plan_ops.py docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md tests/scripts/test_plan_ops.py`

**Description:**
Closes the loop: dispatch templates and the plan-implementer agent spec carry the principle so subagents internalize it; the design doc carries the principle so future contributors find it during architectural work; the audit subcommand provides drift protection so doc/CLI drift surfaces in CI before it bites a real run. The audit checks are deliberately strict — they assert the load-bearing invariants the principle relies on (every fail-task call site has authorization, principle is referenced from every phase that previously auto-reverted). Bundling the audit work into TASK-009 (rather than its own task) keeps the principle's drift protection landing in lockstep with its prose, avoiding a window where the principle exists but is unenforced.

**Scope boundary.** This task does NOT change runtime behavior — pure-doc + audit additions. The audit machinery in `plan_ops.py` already exists (the existing `CANONICAL_CONTRACT` patterns are the model); this task adds two new check entries.
