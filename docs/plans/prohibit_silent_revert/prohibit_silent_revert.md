# Prohibit Silent Revert of Completed Work — `/implement-plan` Hardening

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main
**Design reference:** `docs/analysis/2026-04-24_completed_work_preservation_principle.md` (read v2 first — codifies the principle, the gap analysis G1–G10, and the user-confirmed decisions on §6 questions 3, 4, 7, plus the no-back-compat-flag stance on `--codex-review-binding`)
**Preconditions:** None — applies on top of current `main` (HEAD `7b06505`).

## Goal

Add a Completed-Work Preservation Principle to `/implement-plan`: prohibit any agent or process from silently reverting work an implementer produced — defined as any non-empty diff against the run's `starting_sha` in the working tree, including out-of-scope writes flagged by the Codex wrapper — without first surfacing the situation to the user via an `awaiting_user` event + paused `run_end`. Replace the four destructive auto-`fail-task` paths (Phase C with non-empty diff, Phase D.4, D.2a `--codex-review-binding` fail-fast, `reconcile_batch` out-of-scope writes) with halt-with-pause that returns control to the user with the work intact. When a follow-up fix is needed, prefer in-place repair via `plan-remediator` (touch-only) over reversion. Add a first-class `paused` plan-status, a required `--authorization-source` audit gate on `cmd_fail_task`, and a `--unattended-revert-policy` preflight argument that refuses non-TTY runs without an explicit policy choice. Break the `--codex-review-binding` contract cleanly — no escape-hatch flag.

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

1. `python3 plugins/plan-executor/scripts/plan_ops.py fail-task --plan-file <any.md> --task-id 001 --run-id r --files foo.py --stage implement --reason "test"` exits non-zero with `errors[*].code = "authorization-source-required"`. Adding `--authorization-source user-instruction` succeeds.
2. `git grep -n "fail-task" plugins/plan-executor/skills/implement-plan/SKILL.md` returns N matches, AND `git grep -nB0 -A8 "fail-task" plugins/plan-executor/skills/implement-plan/SKILL.md | grep -c "authorization-source"` returns N (every documented invocation passes the flag). The `plan_ops.py audit --strict --json` adds a `fail_task_authorization_source` check that asserts this invariant.
3. Plan-status enum: `python3 -c "from plugins.plan_executor.scripts import plan_ops; print('paused' in plan_ops._PLAN_STATUS_VALUES)"` (or the equivalent constant name) prints `True`. `mutate_task_status(text, '001', 'paused')` succeeds; `batch-next` excludes a task with `**Status:** paused` from `remaining`.
4. `cmd_preflight` invoked with stdin not a TTY and no `--unattended-revert-policy` flag exits non-zero with `errors[*].code = "unattended-revert-policy-required"`. With `--unattended-revert-policy pause` (or `fail-fast` or `preserve-only`) the field appears in `preflight --json` output as `unattended_revert_policy: "<value>"`.
5. SKILL.md `## Rules` carries the Completed-Work Preservation Principle text. The shared "Awaiting-user pause" subroutine subsection is referenced (by section anchor or named link) from Phase C, Phase D.4, D.2a binding-mode, `reconcile_batch` integration, and the existing D.2a.5/D.2a.6 sections.
6. D.4 rescue end-to-end: a synthetic test where Codex review returns `needs-rework`, the orchestrator dispatches `plan-remediator` with `rescue_findings[]` (and explicit `dismissed_findings: []`), the remediator returns success, the re-review returns `clean`, `commit-task --d4-rescue-tag` succeeds, and the resulting commit body contains a `[d4-rescue]` line. Argparse rejects `--d4-rescue-tag` combined with `--remediation-tag` or `--narrow-remediation-tag` with a structured error.
7. Phase C empty-diff probe: when an implementer returns failure with no working-tree diff against `starting_sha`, the orchestrator calls `fail-task --authorization-source phase-c-empty-diff`. With non-empty diff, the orchestrator marks the task `paused` and emits `awaiting_user` with `stage=post_implement_failure`.
8. D.2a binding-mode: `/implement-plan ... --codex-review-binding` on a Codex `needs-rework` verdict no longer calls `fail-task` — it marks the task `paused` and emits `awaiting_user` with `stage=post_binding_block`. Test `test_section_documents_binding_mode_exemption` (`tests/scripts/test_plan_ops.py:8513–8524`) asserts the new wording.
9. `reconcile_batch` out-of-scope: an envelope with `out_of_scope_observed=true` produces a per-task result with `outcome: "out_of_scope_paused"` (not `scope_violation_reconciled`), no `git restore` runs against the out-of-scope paths, the task's `**Status:**` is `paused`, and the orchestrator emits `awaiting_user` with `stage=post_reconcile_out_of_scope`.
10. `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json` passes with no `fail` or `pass_with_alias` findings, including the new `fail_task_authorization_source`, `paused_status_recognized`, and `principle_referenced` checks.

## Tasks

### TASK-001: `cmd_fail_task` requires `--authorization-source` + initial enum + SKILL co-updates

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/skills/implement-plan/SKILL.md (only the existing `fail-task` invocation lines in Phase C ~555 and Phase D.4 ~757; no other prose changes)
  - tests/scripts/test_plan_ops.py
- **Dependencies:** []
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "fail_task and (authorization_source or AuthorizationSource or required_flag)"`
- **Acceptance criteria:**
  - `cmd_fail_task` argparse adds `--authorization-source` with `required=True` and `choices=["phase-c-impl-failure", "phase-d4-review-failure", "user-instruction"]` (initial enum; later tasks extend). A docstring comment block above the argparse declaration enumerates the values and what each authorizes.
  - Missing `--authorization-source` exits non-zero and emits `{"errors":[{"code":"authorization-source-required","message":"<helpful>"}]}` to stdout under `--json` mode (or to stderr otherwise).
  - The recorded value lands in the `failed` run-log event under key `authorization_source` (string). The existing event_fields dict (around line 4272) gets `event_fields["authorization_source"] = args.authorization_source`.
  - SKILL.md is updated at the existing `fail-task` invocation lines (Phase C around line 555–560 and Phase D.4 around line 757–761) so the documented invocations include `--authorization-source <value>`. Phase C uses `phase-c-impl-failure`; Phase D.4 uses `phase-d4-review-failure`. No other prose is changed in this task — strictly the literal CLI invocation lines.
  - `_validate_review_failure_payload` and other downstream helpers continue to work unchanged.
  - New tests in `tests/scripts/test_plan_ops.py`:
    - `test_fail_task_requires_authorization_source` — invocation without the flag exits non-zero with the structured error.
    - `test_fail_task_authorization_source_round_trips_in_event` — invocation with `--authorization-source user-instruction` produces a `failed` run-log event with `authorization_source: "user-instruction"`.
    - `test_fail_task_authorization_source_rejects_unknown_value` — `--authorization-source bogus-value` exits non-zero with argparse `choices` rejection.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Foundation task: makes `cmd_fail_task` refuse to run without an explicit authorization-source value. This is the audit gate that closes the regression vector — every future contributor who adds a `fail-task` call must consciously declare the path that authorizes the destruction. The SKILL.md changes are scoped narrowly to the literal invocation lines so this task does not collide with the prose rewrites in TASK-004 / TASK-005 / TASK-006 / TASK-007 / TASK-008. The initial enum has only three values (the current authorized paths plus user-instruction); each subsequent task extends `choices=[...]` when it introduces a new path.

### TASK-002: First-class `paused` plan-status + scheduler / mutator awareness

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [001]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "paused or PausedStatus or batch_next_paused or mutate_status_paused"`
- **Acceptance criteria:**
  - The plan-status enum that `mutate_task_status` validates against (search the file for `_PLAN_STATUS_VALUES` or the equivalent constant — e.g. `_TASK_STATUS_VALUES`, `STATUS_VALUES`) gains the literal value `"paused"`. The constant's docstring or comment block documents the new state alongside `pending | in-progress | done | failed | blocked`.
  - `mutate_task_status(text, task_id, "paused")` succeeds and produces the expected `**Status:** paused` mutation, byte-identical in shape to the existing transitions.
  - `cmd_batch_next` (lines around `2738, 2805–2812`) excludes any task whose status is `paused` from both `remaining` and `ready_in_batch`. A paused task is treated like `failed` for picking purposes (skip), but unlike `failed` it does NOT cascade `block-dependents`.
  - `cmd_block_dependents` is unchanged — it cascades only on `failed`. Add a one-line comment confirming paused tasks do not cascade.
  - `cmd_update_plan_header` accepts `paused` as a valid task status when computing `partial` vs `complete`: presence of any `paused` task → header is `partial` (same as `failed`).
  - `cmd_lint_plans` recognizes `paused` as a valid status and does NOT flag it as drift; however, a `paused` status without a corresponding `awaiting_user` run-log event for the same task in the same run IS flagged (new check `paused_without_awaiting_user_event`).
  - `_validate_schedule` (and any sibling validators) accept `paused` in any task `status` field.
  - New tests:
    - `test_mutate_task_status_accepts_paused` — round-trips `pending → paused → done` and `pending → paused → failed`.
    - `test_batch_next_skips_paused_task` — schedule with one `paused` task in the active batch returns the next ready task; the paused task does not appear.
    - `test_batch_next_paused_does_not_block_dependents` — a paused task with two pending dependents returns the dependents as not-yet-ready (because their dep isn't `done`), but does not mark them `blocked`.
    - `test_update_plan_header_partial_on_paused` — a plan with one paused task and N done produces `**Status:** partial` (not `complete`).
    - `test_lint_plans_paused_without_event_flagged` — a plan with `**Status:** paused` but no matching `awaiting_user` event emits a `paused_without_awaiting_user_event` finding.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Adds `paused` as a first-class plan-status so the scheduler does not re-pick a paused task on the next batch round. This is the foundation that TASK-005 / TASK-006 / TASK-007 / TASK-008 build on — each of them flips a task to `paused` when entering an awaiting-user pause. Without this state, a paused task left as `pending` would be re-picked and overwrite the preserved work (the Codex Q5/Q6 + Gemini #1 finding from the design-doc cross-review).

**Implementation notes.** Locate the constant by `git grep -n '_PLAN_STATUS_VALUES\|_TASK_STATUS_VALUES\|STATUS_VALUES\|status_values' plugins/plan-executor/scripts/plan_ops.py`. The constant may be a frozenset, list, or a tuple — extend in place. If the validation logic is split across multiple functions (e.g., parser + mutator + lint), update each to accept the new value. Do NOT introduce a new state machine module — the existing structure suffices.

### TASK-003: `--unattended-revert-policy` preflight + TTY refuse + orchestrator pin

- **Status:** pending
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
    - `test_preflight_refuses_non_tty_without_policy` — stub `sys.stdin.isatty()` to return False, omit the flag, assert non-zero exit + structured error.
    - `test_preflight_accepts_non_tty_with_policy` — same TTY stub, pass `--unattended-revert-policy pause`, assert success and `preflight --json` output carries the field.
    - `test_preflight_tty_defaults_to_pause` — stub `isatty()` True, omit flag, assert success and field value `pause`.
    - `test_preflight_rejects_unknown_policy_value` — bogus value rejected by argparse `choices`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Adds the per-run policy that controls how the new pause paths behave under unattended execution. Per user decision: refuse-with-error when stdin is not a TTY and no policy is provided — loud failure beats silent destruction in cron/CI contexts. The three values (`pause | fail-fast | preserve-only`) are defined in the design reference §4.1.j; `pause` is the default for interactive runs and the new behavior across G1/G2/G3/G10. `fail-fast` restores the legacy auto-fail-task behavior for unattended runs that prefer fast failure over stalls. `preserve-only` is a compromise (logs the discarded diff to a salvage ref before fail-task). This task only wires up preflight and the SKILL.md pin; consumers (Phase C, D.4, D.2a, reconcile-batch) come online in their respective tasks.

### TASK-004: Completed-Work Preservation Principle text + shared awaiting-user pause subroutine

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
- **Dependencies:** [001, 002, 003]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json`
- **Acceptance criteria:**
  - SKILL.md `## Rules` section gains a new top bullet titled **Completed-Work Preservation Principle**, with the exact text from §2 of the design reference (`docs/analysis/2026-04-24_completed_work_preservation_principle.md`). The existing line-811 rule "Never auto-`fail-task` on the D.2a.5 halt path" is reworded into a sub-bullet labeled "Specific instance: D.2a.5 / D.2a.6 halt path — …" to cross-reference the new principle.
  - A new subsection titled **Awaiting-user pause (shared control flow)** is inserted between Phase D.4 (`### D.4 — Phase D fail`) and Phase E (`### Phase E — Next batch`). The subsection body:
    - Documents the five canonical control-flow steps (log `awaiting_user`, `finalize-execution-log --outcome paused --ending-sha "$(git rev-parse HEAD)"`, log `run_end outcome=paused`, skip End-of-run housekeeping + release run-lock, never call `fail-task` / `git restore` / mutate plan-status to `failed`).
    - Includes the per-stage payload contract table from §4.1.b of the design reference, listing all stage values: `post_remediation_review`, `post_remediation_implement`, `post_narrow_remediation_review`, `post_narrow_remediation_implement`, `post_implement_failure`, `post_d4_rescue_failed`, `post_commit_seam_failure`, `post_binding_block`, `post_reconcile_out_of_scope`. For each, lists required additional payload fields beyond `dirty_files`.
    - Explicitly states: "the subroutine factors out **control flow only**; per-stage payloads are intentionally distinct and remain declared at each call site." (Codex Q2 clarification.)
    - Includes the resume-protocol paragraph: when the user re-runs `/implement-plan` after a pause, Phase 0 preflight surfaces paused tasks via a new `paused_tasks[]` field; the orchestrator does NOT auto-resume — the user explicitly instructs disposition for each paused task.
  - Phase D.2a.5 (lines ~628–663) and Phase D.2a.6 (lines ~664–696) sections add a one-line cross-reference at their pause-step bullets: "(See **Awaiting-user pause** subroutine — same control flow.)" The literal command-block examples in those sections are kept (back-compat audit references); the cross-reference flags them as the canonical instance, not the only one.
  - In every other phase that documents destructive auto-revert (Phase C, Phase D.4, D.2a binding-mode), add a sentence: "See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn." (Phase-specific behavior changes are TASK-005 / TASK-006 / TASK-007's job; this task only adds the cross-reference sentence.)
  - `plan_ops.py audit` adds a check `principle_referenced` that scans SKILL.md for the literal string `Completed-Work Preservation Principle` and asserts (a) it appears in `## Rules`, (b) it appears in at least 4 other phase sections by cross-reference. Existing audit machinery in `plan_ops.py` provides the pattern; this is a non-strict check (`tier="default"`).
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/scripts/plan_ops.py`

**Description:**
Pure-prose task that establishes the canonical principle text, the shared subroutine spec, and the cross-references that subsequent tasks will lean on. Lands before the behavior changes so that TASK-005 / TASK-006 / TASK-007 / TASK-008 each have a single source of truth to point at when rewriting their phase-specific prose. The audit check enforces that the principle stays referenced from the relevant phase sections — drift protection. No behavior changes in this task; only documentation and the non-strict audit check.

**Scope boundary.** This task does NOT modify Phase C (TASK-006), Phase D.4 (TASK-005), D.2a binding-mode (TASK-007), or `reconcile_batch` (TASK-008). It only adds the principle text, the shared subroutine spec, the cross-reference sentences in those phases, and the audit check.

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
    - `--d4-rescue-tag` mutually exclusive with `--remediation-tag` and `--narrow-remediation-tag`.
    - `--d4-rescue-tag` may co-occur with `--disagreement-tag` (reviewer split is unrelated to rescue tagging).
    - Unknown combinations exit non-zero with `errors[*].code = "uncommittable-tag-combination"` and a message listing the conflicting flags.
  - On successful commit with `--d4-rescue-tag`, the commit body carries a `[d4-rescue]` line (separate from `[remediation]` and `[narrow-remediation]`). The `commit_done` run-log event gains `d4_rescue_tag: true` (new key alongside existing `disagreement_tag`, `remediation_tag`, `narrow_remediation_tag`).
  - `ALLOWED_LOG_EVENTS` (search by name) gains `d4_rescue_start` and `d4_rescue_done`. The existing per-event field validators (if any) accept the new event names.
  - `cmd_fail_task` `--authorization-source` enum extends with `phase-d4-rescue-failed` (alongside the TASK-001 initial set).
  - SKILL.md Phase D.4 (lines ~755–771) is rewritten:
    - The header "### D.4 — Phase D fail" is renamed to "### D.4 — Phase D rescue + halt".
    - Body documents the trigger split: commit-seam failures (e.g., `commit_safe_gate_failed`, `v_check` failure, post-commit gate mismatch) → mark task `paused` and call Awaiting-user pause stage=`post_commit_seam_failure` (no remediator attempt — failure is at the staging seam, not in code).
    - Reviewer-findings triggers → single-shot D.4 rescue protocol: log `d4_rescue_start`, dispatch `plan-remediator` with the new D.4-rescue template, classify retry. On `success` → re-dispatch the original reviewer (binding) → on `clean | minor-findings | ship | ship-with-fixes` → Phase D.3 commit with `--d4-rescue-tag`. On `needs-rework` re-review → mark `paused` + Awaiting-user pause stage=`post_d4_rescue_failed`. On `scope-violation | failure` from the rescue itself → mark `paused` + Awaiting-user pause stage=`post_d4_rescue_failed` with `rescue_attempt_outcome` distinguishing the two sub-reasons.
    - Hard rule appended: "D.4 rescue is single-shot. Post-rescue re-review failure halts for user — do NOT dispatch a second rescue, do NOT chain into D.2a.5/D.2a.6 (those are pre-D.4 paths)."
    - Cross-reference: "(Per the **Completed-Work Preservation Principle** — see §Rules. Uses the **Awaiting-user pause** subroutine for halts.)"
  - `dispatch-templates.md` gains a new `**Phase D.4-rescue**` template subsection. Inputs documented: `rescue_findings[]` (NOTE: distinct key from `load_bearing_findings[]`), `dismissed_findings: []` (literal empty list), task block, reviewer-source. Output contract: same shape as the existing plan-remediator report, with the `**Dismissed findings noted:**` section literally `(none — D.4 rescue does not carry dismissed findings)`.
  - `agents/plan-remediator.md` updates the **Inputs** section to document dual modes:
    - **D.2a.6 narrow-remediation mode:** `load_bearing_findings[]` + `dismissed_findings[]` + `d5_summary` (existing).
    - **D.4 rescue mode** (new): `rescue_findings[]` + `dismissed_findings: []` (literal empty) + (no `d5_summary`).
    - The Scope rule (line ~37) is updated: "the union of `(file, line)` coordinates across `load_bearing_findings[]` OR `rescue_findings[]` (whichever is provided) defines your permitted edit region." Both keys cannot be provided in the same dispatch — orchestrator enforces.
    - The `**Dismissed findings noted:**` section is required in D.2a.6 mode; in D.4-rescue mode it is the literal "(none — D.4 rescue does not carry dismissed findings)" line.
    - Adds a NEW report section `**Proposed-fix-scope:**` (used on `scope-violation`): lists files/lines that would need broader edits if scope were widened. Empty/omitted on success.
  - New tests in `tests/scripts/test_plan_ops.py`:
    - `test_commit_task_accepts_d4_rescue_tag` — invocation with `--d4-rescue-tag`, no other tags, succeeds; commit body contains `[d4-rescue]`; `commit_done` event has `d4_rescue_tag: true`.
    - `test_commit_task_d4_rescue_xor_with_remediation` — `--d4-rescue-tag --remediation-tag` rejected with `uncommittable-tag-combination`.
    - `test_commit_task_d4_rescue_xor_with_narrow_remediation` — same with `--narrow-remediation-tag`.
    - `test_commit_task_d4_rescue_with_disagreement_allowed` — `--d4-rescue-tag --disagreement-tag` succeeds; body contains both.
    - `test_log_event_accepts_d4_rescue_start_and_done` — both events validate against `ALLOWED_LOG_EVENTS`.
    - `test_fail_task_authorization_source_accepts_phase_d4_rescue_failed` — enum extension verified.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/agents/plan-remediator.md tests/scripts/test_plan_ops.py`

**Description:**
Implements the highest-value behavior change: D.4 stops being a destructive seam and becomes a try-rescue-then-pause seam. The rescue is single-shot and terminal — the structural protection against infinite loops Gemini flagged. The new commit-tag (`[d4-rescue]`) and event types (`d4_rescue_start` / `d4_rescue_done`) are deliberately distinct from D.2a.6's narrow-remediation machinery so audit trails stay legible. `plan-remediator` accepts both input modes via a key-based discriminator (`load_bearing_findings[]` vs `rescue_findings[]`), keeping the agent's contract explicit rather than overloading the existing key.

**Scope boundary.** This task does NOT touch Phase C (TASK-006), D.2a binding-mode (TASK-007), or `reconcile_batch` (TASK-008). It does NOT add `--d4-rescue-tag` cross-references in `dispatch-templates.md` outside the new Phase D.4-rescue template subsection.

### TASK-006: Phase C empty-diff probe + non-empty-diff pause

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (`cmd_fail_task` enum extends with `phase-c-empty-diff`; remove `phase-c-impl-failure` placeholder once unused)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase C section rewrite, lines ~551–575)
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [001, 002, 003, 004]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "phase_c or PhaseC or empty_diff_probe or impl_failure_pause"`
- **Acceptance criteria:**
  - SKILL.md Phase C section (lines ~551–575) is rewritten:
    - Trigger documented: any non-success outcome from a Phase B implementer dispatch (Claude or Codex, after the Codex-fallback-to-Claude already ran).
    - Probe step documented: orchestrator runs `git diff --quiet --no-ext-diff <starting_sha> -- <touched_files ∪ task.Files:>` to detect any non-empty diff. The touched-files set is the implementer's reported `files_changed` (when present in the report) ∪ the task's declared `Files:` list.
    - Empty diff branch: orchestrator calls `fail-task --authorization-source phase-c-empty-diff --stage implement --reason "<short>"` (current behavior preserved; nothing to lose).
    - Non-empty diff branch: orchestrator calls `mutate_task_status` to flip the task to `paused`, then calls **Awaiting-user pause** subroutine with `stage=post_implement_failure`, payload includes `implementer_outcome`, `diagnostics` (from the implementer report), `reversion_guidance` (from the implementer report), `nonempty_diff_files[]` (the actual paths with diff).
    - Cascade-`block-dependents`: only fires on the empty-diff (fail-task) branch. On the pause branch, dependents stay `pending` and are skipped by `batch-next` until the user resolves the pause in their next turn.
    - Cross-reference: "(Per the **Completed-Work Preservation Principle** — see §Rules. Uses the **Awaiting-user pause** subroutine for the non-empty-diff branch.)"
  - `cmd_fail_task` enum is updated: `phase-c-impl-failure` (placeholder from TASK-001) is REMOVED, replaced by `phase-c-empty-diff` (more precise — names what the path actually authorizes). All SKILL.md references to `phase-c-impl-failure` are updated.
  - The `--unattended-revert-policy` value (pinned by TASK-003) routes the non-empty-diff branch:
    - `pause` → halt-with-pause (default).
    - `fail-fast` → call `fail-task --authorization-source unattended-fail-fast --stage implement --reason "unattended fail-fast policy"`. (The enum gains `unattended-fail-fast` in this task.)
    - `preserve-only` → log a `salvage_branch_ref` event recording the diff (e.g., `git stash create`), THEN call `fail-task --authorization-source unattended-fail-fast`. The salvage ref is documented but the implementer is free to use any non-destructive recording mechanism (`git stash create` is one option).
  - New tests:
    - `test_phase_c_empty_diff_probe_returns_clean` — synthesize an implementer-failure report with no working-tree diff against starting_sha; assert orchestrator state matches the empty-diff branch (this may be a unit test of a helper function rather than full orchestrator integration — keep it tight).
    - `test_phase_c_nonempty_diff_triggers_pause_helper` — same with non-empty diff; assert the helper that decides pause-vs-fail-task returns `pause`.
    - `test_fail_task_authorization_source_accepts_phase_c_empty_diff` — enum extension verified.
    - `test_fail_task_authorization_source_accepts_unattended_fail_fast` — same.
    - `test_fail_task_authorization_source_rejects_legacy_phase_c_impl_failure` — the placeholder value is no longer accepted.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Implements the Phase C gap (G1): when an implementer fails but left a non-empty diff in the working tree, preserve that diff via halt-with-pause instead of `git restore`. The empty-diff branch is preserved (nothing to lose; auto-fail-task is correct). The `--unattended-revert-policy` pin from TASK-003 controls behavior in non-interactive contexts. The placeholder `phase-c-impl-failure` enum value is replaced by the more precise `phase-c-empty-diff` since after this task the only authorized Phase C destruction is the empty-diff branch.

**Implementation notes.** The orchestrator-side branching logic is documented in SKILL.md prose (the orchestrator agent is the executor); plan_ops.py contributes only the auth-source enum extension and the new pause-helper test fixtures. There is no new plan_ops.py subcommand needed — the existing `mutate_task_status`, `log-event`, `finalize-execution-log` commands suffice.

### TASK-007: D.2a binding-mode reinterpretation (no escape hatch)

- **Status:** pending
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
    - Test still asserts presence of `codex-review-binding` literal.
    - Old assertion `assert ("skip" in body.lower() or "NO D.2a.6" in body or "not entered" in body.lower())` is REPLACED with a new assertion checking that the exemption text mentions BOTH "pause" (or "paused") AND "user instruction" (or "user decision") to confirm the new pause behavior is documented.
    - A second new test `test_section_documents_binding_mode_unattended_fallback` asserts the section mentions `unattended-revert-policy fail-fast` as the cron/CI escape route (since there is no `--codex-review-binding-destructive` flag — per user decision 4, the contract breaks cleanly).
  - There is NO new flag `--codex-review-binding-destructive`. The contract for `--codex-review-binding` changes in place. Anyone relying on the legacy fail-fast behavior must adopt `--unattended-revert-policy fail-fast`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Reinterprets `--codex-review-binding`: from "auto-fail-task on Codex `needs-rework`" to "binding for the commit decision; pause for user instruction". The user explicitly chose to break this contract cleanly (no back-compat escape-hatch flag). Cron/CI users who relied on the legacy fail-fast path must now pass `--unattended-revert-policy fail-fast` (introduced in TASK-003) to get the same behavior. The three SKILL.md sections that documented the old contract (CLI help, D.2a routing, D.2a.6 exemption) are co-updated in this single task to prevent doc-drift; the test that asserted the old contract is rewritten to assert the new one. No new auth-source enum value is needed because binding-mode pause uses the existing `unattended-fail-fast` (for the cron/CI fall-through) and `user-instruction` (for the user-resolves-the-pause path) values.

**Migration callout (for the run summary banner).** The first /implement-plan invocation that uses `--codex-review-binding` after this task lands SHOULD log a one-time `binding_mode_contract_changed_notice` event at run_start with a one-line message reminding the operator that binding-mode now pauses by default. Implementer's discretion whether to add this; nice-to-have, not load-bearing.

### TASK-008: `reconcile_batch` out-of-scope pause (G10)

- **Status:** pending
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
    - Do NOT `git restore --staged` or `git restore` the tracked out-of-scope paths.
    - Do NOT `unlink` untracked out-of-scope paths.
    - Mark the task `**Status:** paused` (calls `mutate_task_status` against the task's plan_file).
    - Append to results: `{task_id, outcome: "out_of_scope_paused", out_of_scope_tracked: [...], out_of_scope_untracked: [...], skipped_protected: [...], residual_dirty: [], error: null}`. The `outcome` value `"out_of_scope_paused"` is NEW (alongside `no_op`, `scope_violation_reconciled`, `reconciliation_failed`).
  - Under `out_of_scope_policy == "reconcile-and-revert"`, current behavior is preserved unchanged (existing tests stay green).
  - `reconciliation_failed` outcome (residual dirt after reconcile-and-revert) continues to hard-halt the run regardless of policy. No change to that path.
  - The Phase 0 `--unattended-revert-policy` pin (from TASK-003) determines the orchestrator's default for this flag: `pause` → `--out-of-scope-policy pause`; `fail-fast` and `preserve-only` → `--out-of-scope-policy reconcile-and-revert`. Documented in SKILL.md.
  - SKILL.md Phase B reconciliation section (around line 549, the "At the batch join barrier..." paragraph) is updated:
    - Documents the new flag and the policy-pin defaulting.
    - Documents the new `out_of_scope_paused` outcome and the orchestrator's response: call **Awaiting-user pause** subroutine with `stage=post_reconcile_out_of_scope`, payload includes `out_of_scope_tracked[]`, `out_of_scope_untracked[]`, `task_id`, `wrapper_envelope_summary`. Other tasks in the same batch proceed to review/commit independently — the pause is per-task, not per-batch.
    - Documents the four user options at next-turn (widen-plan / in-place-fix / keep-and-commit / revert) with one-sentence each.
    - Cross-reference: "(Per the **Completed-Work Preservation Principle** — see §Rules. Uses the **Awaiting-user pause** subroutine.)"
  - `cmd_fail_task` enum extends with `reconcile-out-of-scope-user-instruction` (used when the user instructs revert in their next turn for an out-of-scope-paused task).
  - New tests:
    - `test_reconcile_batch_out_of_scope_pauses_by_default` — invoke `reconcile-batch` (no `--out-of-scope-policy` flag, defaults to `pause`) with an envelope where `out_of_scope_observed=true`; assert no `git restore` runs, files remain in working tree, and the result entry has `outcome=out_of_scope_paused` and the task's plan-status is `paused`.
    - `test_reconcile_batch_legacy_policy_still_reverts` — same envelope with `--out-of-scope-policy reconcile-and-revert`; assert current behavior preserved (files restored, `outcome=scope_violation_reconciled`).
    - `test_reconcile_batch_no_op_unchanged_under_pause_policy` — envelope with `out_of_scope_observed=false` returns `no_op` regardless of policy.
    - `test_reconcile_batch_reconciliation_failed_unchanged` — residual-dirt scenario still produces `reconciliation_failed` regardless of policy.
    - `test_fail_task_authorization_source_accepts_reconcile_out_of_scope_user_instruction` — enum extension verified.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py`

**Description:**
Implements G10, the flagship case: when the Codex implementer wrote files outside the task's declared `Files:` list, the wrapper-bounded cleanup is replaced by a per-task pause that gives the user four explicit options (widen the plan, fix-in-place, keep-and-commit, or revert). This directly matches the user's framing — "doesn't matter how 'out of scope' the necessary changes might be, we need to try to fix them in place to preserve effort". The legacy reconcile-and-revert behavior remains available via the new `--out-of-scope-policy reconcile-and-revert` flag for the cron/CI case (when `--unattended-revert-policy` is `fail-fast` or `preserve-only`). Other tasks in the same batch proceed independently — the pause is per-task, not per-batch, so a single out-of-scope write doesn't stall the whole batch.

**Implementation notes.** `reconcile_batch` is a Python function that returns a results list; the per-task `mutate_task_status` mutation requires knowing the task's plan_file. The current signature takes `batch_envelopes` and `repo_root`; extend it with `plans_dir` (or per-envelope `plan_file` references) so the mutation can target the right file in directory mode. Inspect the existing code to determine the cleanest extension.

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
    - `fail_task_authorization_source` — scans SKILL.md for every `fail-task` invocation block (heuristic: lines containing `plan_ops.py" fail-task` or `plan_ops.py fail-task`) and asserts each one passes `--authorization-source <value>` where `<value>` is one of the closed enum from `cmd_fail_task`. Failure surface lists the SKILL.md line numbers where the invariant is violated.
    - `principle_referenced` — asserts SKILL.md `## Rules` contains the literal "Completed-Work Preservation Principle" header AND that at least 4 other phase sections (Phase C, Phase D.4, D.2a binding-mode, reconcile-batch — at minimum) cite the principle by reference. Failure surface lists which sections are missing the cross-reference.
  - The two new audit checks are listed in `plan_ops.py audit --list` output and round-trip through `--json` / `--report-file` outputs.
  - New tests:
    - `test_audit_fail_task_authorization_source_passes_on_clean_skill` — uses the shipped SKILL.md (after all prior tasks land) and asserts the check passes.
    - `test_audit_fail_task_authorization_source_fails_on_drifted_skill` — temporarily writes a SKILL.md to a tmp_path with a `fail-task` line missing the flag; asserts the check produces a failure with that line number.
    - `test_audit_principle_referenced_passes` — same with the principle-referenced check.
    - `test_audit_principle_referenced_fails_when_missing` — tmp_path SKILL.md without the principle text; assert failure.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/agents/plan-implementer.md plugins/plan-executor/scripts/plan_ops.py docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md tests/scripts/test_plan_ops.py`

**Description:**
Closes the loop: dispatch templates and the plan-implementer agent spec carry the principle so subagents internalize it; the design doc carries the principle so future contributors find it during architectural work; the audit subcommand provides drift protection so doc/CLI drift surfaces in CI before it bites a real run. The audit checks are deliberately strict — they assert the load-bearing invariants the principle relies on (every fail-task call site has authorization, principle is referenced from every phase that previously auto-reverted). Bundling the audit work into TASK-009 (rather than its own task) keeps the principle's drift protection landing in lockstep with its prose, avoiding a window where the principle exists but is unenforced.

**Scope boundary.** This task does NOT change runtime behavior — pure-doc + audit additions. The audit machinery in `plan_ops.py` already exists (the existing `CANONICAL_CONTRACT` patterns are the model); this task adds two new check entries.

## Expected outcome

- Nine `feat(TASK-NNN):` commits plus one `chore(implement-plan):` housekeeping commit.
- `cmd_fail_task` requires `--authorization-source` at the CLI; missing flag is a hard error. Every documented `fail-task` invocation in SKILL.md passes a valid value.
- `paused` is a first-class plan-status. `batch-next`, `block-dependents` (no-op for paused), `update-plan-header`, `lint-plans`, and `mutate_task_status` all recognize it. A paused task in a schedule does not get re-picked.
- `cmd_preflight` refuses non-TTY runs without `--unattended-revert-policy`. The pin propagates through the orchestrator.
- SKILL.md `## Rules` carries the Completed-Work Preservation Principle. The shared **Awaiting-user pause** control-flow subroutine is documented and referenced from all five gap paths (G1, G2, G3, G10) plus the existing D.2a.5/D.2a.6 paths.
- Phase C with non-empty diff pauses for user; with empty diff, auto-fail-task. Phase D.4 attempts in-place rescue first (single-shot, terminal); on failure, pauses for user. D.2a `--codex-review-binding` pauses for user (no escape-hatch flag); cron/CI users adopt `--unattended-revert-policy fail-fast`. `reconcile_batch` out-of-scope writes pause per-task with four user options.
- `commit-task` accepts `--d4-rescue-tag` with XOR rules against `--remediation-tag` / `--narrow-remediation-tag`; commit body carries `[d4-rescue]` line.
- `dispatch-templates.md` and `plan-implementer.md` reinforce the principle. `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` carries it as standing design.
- `plan_ops.py audit --strict --json` passes, including the two new drift checks (`fail_task_authorization_source`, `principle_referenced`).

## Follow-ups (out of scope)

- **`--unattended-revert-policy preserve-only` salvage-ref convention.** TASK-006 documents that `preserve-only` should record the discarded diff to a salvage ref before fail-task, but leaves the exact mechanism to the implementer's discretion (`git stash create` is one option). A follow-up plan can codify the salvage-ref naming scheme (`refs/salvage/<run_id>/<task_id>` etc.), the lifecycle (cleanup policy, max age), and a `plan_ops.py salvage-list` / `salvage-recover` UX. Out of scope here because the principle does not require any specific recording mechanism; just non-destruction.
- **Per-finding `target_task_id` integration with D.4 rescue.** The hotfix introduced `target_task_id` on plan-review findings; this plan's D.4 rescue carries `rescue_findings[]` per task. A follow-up could thread `target_task_id` through D.4 rescue too if cross-task review findings ever surface there. Today, Phase D reviews are per-task by construction, so the integration isn't load-bearing.
- **Migration banner for `--codex-review-binding`.** TASK-007's "implementer's discretion" note about a one-time `binding_mode_contract_changed_notice` event at run_start can be hardened into a structural requirement in a follow-up if operators report missing the contract change.
- **`reconcile_batch` per-task in-place-fix UX.** TASK-008 documents four user options including "in-place fix"; the orchestrator handoff for that option (re-dispatching `plan-remediator` to revise the task within the original `Files:` and revert the out-of-scope writes) is currently a manual user-driven flow. A follow-up could add a one-liner orchestrator subcommand or template to streamline it.
- **Pre-existing test failure in `test_analyst_to_parse_schedule_roundtrip` (`test_plan_ops.py:5194`).** Same caveat as the per-task-dispatch-refactor-v2 plan — unrelated to this work but worth a surgical fix before the integration test surface grows further.
