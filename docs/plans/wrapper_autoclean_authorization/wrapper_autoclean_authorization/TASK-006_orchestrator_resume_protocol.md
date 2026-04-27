# TASK-006 — Orchestrator-side resume protocol — `Awaiting-user pause` extension to `post_wrapper_autoclean_blocked`

## Goal

Orchestrator-side resume protocol — `Awaiting-user pause` extension to `post_wrapper_autoclean_blocked`

## Context

The `prohibit_silent_revert` plan (committed `8bb0b7b`) added the **Awaiting-user pause** subroutine (TASK-004) as the canonical control flow for any `/implement-plan` halt that preserves work pending user instruction. Its TASK-008 then registered the `reconcile-batch out-of-scope` use of that subroutine. TASK-009 of this plan registers a new use: the wrapper's `wrapper_autoclean_blocked` envelope (introduced by TASK-004) must be routed through the same subroutine, so the user gets a consistent resume UX regardless of which seam triggered the pause.

The wrapper does not own run-log mutation; it carries the signal in the envelope. The orchestrator parses the envelope at the dispatch site (Phase B / B-rework / D.2b / B-narrow-remediation) and routes accordingly. This task wires that routing into SKILL.md and extends `cmd_fail_task --authorization-source` enum with one new value (`wrapper-autoclean-user-instruction`) for the user-instructed-revert disposition.

### Decisions folded in

1. **Reuse the existing pause subroutine.** Same control flow (log `awaiting_user`, `finalize-execution-log --outcome paused`, `run_end outcome=paused`, mark task `paused`, skip housekeeping). Only the per-stage payload contract row is new.
2. **Four user options at next turn**, mirroring `reconcile-batch`'s framing:
   - `(a) re-dispatch with declared_files_changed populated correctly` — the orchestrator re-builds the wrapper input via `plan_ops.py build-claude-dispatch-input` (TASK-001).
   - `(b) keep-and-commit` — user explicitly authorizes the preserved deltas.
   - `(c) revert` — orchestrator calls `cmd_fail_task --authorization-source wrapper-autoclean-user-instruction --stage <site>`.
   - `(d) abort the run` — exits non-zero with no further changes.
3. **One new auth-source enum value.** `wrapper-autoclean-user-instruction` is added to `cmd_fail_task --authorization-source` choices for option (c). No other auth-source paths are added.
4. **`review-route` (the deterministic state machine) does NOT need to change.** The wrapper-autoclean-blocked path is at the dispatch-site level (Phase B classify shim), not inside `review-route`'s post-implement classification. The orchestrator's existing routing ladder reads the wrapper envelope's `error.code` to discriminate; `review-route` consumes already-classified inputs and is unaffected.
5. **Pause is per-task, not per-batch.** Mirrors prohibit_silent_revert TASK-008's behavior: peer tasks in the same batch proceed to review/commit independently. A wrapper-autoclean-blocked pause on TASK-N does not stall TASK-M.

## Verification

- SKILL.md `Awaiting-user pause (shared control flow)` subsection (added by prohibit_silent_revert TASK-004) gains a new row in the per-stage payload contract table for `stage=post_wrapper_autoclean_blocked`.
- The four call sites in SKILL.md (Phase B default `:502`, Phase B-rework `:640`'s `dispatch_bounded_remediation` branch, Phase D.2b `:650`, Phase B-narrow-remediation `:640`'s `dispatch_narrow_remediation` branch) each cross-reference the new stage with one line.
- `cmd_fail_task --authorization-source wrapper-autoclean-user-instruction --stage implement --reason "user-instructed revert"` succeeds (the new enum value is accepted).
- `cmd_fail_task --authorization-source wrapper-autoclean-user-instruction --stage implement --reason "<short>"` produces a `failed` run-log event with `authorization_source: "wrapper-autoclean-user-instruction"`.
- New tests in `tests/scripts/test_plan_ops.py` (two tests below).

## Tasks

### TASK-006: Orchestrator-side resume protocol — `Awaiting-user pause` extension to `post_wrapper_autoclean_blocked`

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Awaiting-user pause stage table — add `post_wrapper_autoclean_blocked` row; Phase B classify shim at `:512`; Phase B-rework / D.2a.5 routing at `:640`; Phase B-narrow-remediation routing at `:640`)
  - plugins/plan-executor/scripts/plan_ops.py (`cmd_fail_task --authorization-source` enum extends with `wrapper-autoclean-user-instruction`)
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [004]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "wrapper_autoclean_user_instruction or post_wrapper_autoclean_blocked"`
- **Acceptance criteria:**
  - SKILL.md `Awaiting-user pause (shared control flow)` subsection (added by prohibit_silent_revert TASK-004) — the per-stage payload contract table gains a new row:
    - **Stage:** `post_wrapper_autoclean_blocked`.
    - **Trigger:** Phase B / B-rework / D.2b / B-narrow-remediation dispatch site receives a `claude_dispatch_failed` event with `error.code: wrapper_autoclean_blocked` from the wrapper.
    - **Payload (in addition to `dirty_files`):** `{task_id, agent, preserved_files: [...], wrapper_error_code: "wrapper_autoclean_blocked", wrapper_error_message: <verbatim from envelope.error.message>}`. The `preserved_files` list is the union of `scope.observed_delta_tracked` and `scope.observed_delta_untracked` from the wrapper envelope.
    - **User options at next turn:** four explicit options documented in prose:
      - `(a) re-dispatch with declared_files_changed populated correctly` — instructs the orchestrator to re-build the wrapper input via `plan_ops.py build-claude-dispatch-input` (TASK-001) and re-dispatch. Suitable when the pause was caused by a Layer-A regression (orchestrator forgot to populate the field).
      - `(b) keep-and-commit` — user explicitly authorizes the preserved deltas. Orchestrator runs `commit-task` with the preserved files. Suitable when the implementer's work is correct and the user wants to skip the re-dispatch.
      - `(c) revert` — orchestrator calls `cmd_fail_task --authorization-source wrapper-autoclean-user-instruction --stage <site> --reason "user-instructed revert"`. Suitable when the implementer's work is wrong and the user wants to start over.
      - `(d) abort the run` — orchestrator emits `run_end outcome=aborted` and exits.
  - SKILL.md `## Dispatch error handling (Claude wrapper)` table at `:74` gains a new sub-bullet under the `status != ok` row: ``status == "scope_violation" AND error.code == "wrapper_autoclean_blocked" → invoke the **Awaiting-user pause** subroutine with stage="post_wrapper_autoclean_blocked"; the existing scope_violation route (error.code == "scope_violation", file-level out-of-scope writes the wrapper reverted) routes the same as it does today (post_<site>_implement).``
  - The four call sites in SKILL.md (Phase B default `:502`, Phase B-rework `:640`'s `dispatch_bounded_remediation` branch, Phase D.2b `:650`, Phase B-narrow-remediation `:640`'s `dispatch_narrow_remediation` branch) each cross-reference the new stage with one line: ``Wrapper `error.code: "wrapper_autoclean_blocked"` → Awaiting-user pause stage=`post_wrapper_autoclean_blocked` (NOT `post_<site>_implement`); see §Awaiting-user pause table for payload contract.``
  - `cmd_fail_task` `--authorization-source` enum (in `plan_ops.py`) extends with `wrapper-autoclean-user-instruction`. The docstring comment block above the argparse declaration documents the new value:
    ```
    # wrapper-autoclean-user-instruction — sanctioned by the user via
    # next-turn instruction after an Awaiting-user pause whose stage is
    # post_wrapper_autoclean_blocked. Authorizes reverting the implementer's
    # preserved deltas. Issued only when option (c) is selected from the
    # four-option resume UX (see SKILL.md §Awaiting-user pause).
    ```
  - `review-route` (the deterministic state machine in `plan_ops.py` documented at SKILL.md `:640`) does NOT need to change: the wrapper-autoclean-blocked path is at the dispatch-site level (Phase B classify shim), not inside `review-route`'s post-implement classification.
  - New tests:
    - `test_fail_task_authorization_source_accepts_wrapper_autoclean_user_instruction` — invocation with the new enum value succeeds; the `failed` run-log event has `authorization_source: "wrapper-autoclean-user-instruction"`.
    - `test_skill_md_documents_post_wrapper_autoclean_blocked_stage` — SKILL.md doc-only assertion: the Awaiting-user pause subsection contains the literal string `post_wrapper_autoclean_blocked` AND the four-option list.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Wires the wrapper's signal back into the orchestrator's existing pause infrastructure. The user gets four explicit options (re-dispatch with corrected scope, keep-and-commit, revert, abort) and the work stays in the working tree until disposition. Mirrors prohibit_silent_revert TASK-008's reconcile-batch four-options framing — same UX, different trigger.

**Implementation notes.** SKILL.md is the single source of truth for the orchestrator's runtime behavior. The Awaiting-user pause stage table is already maintained as a list per prohibit_silent_revert TASK-004; extending it is a one-row addition. The four cross-reference one-liners at the dispatch sites are pure prose; they don't change the parser's behavior on the existing `error.code: scope_violation` path (which routes through `post_<site>_implement` as documented today).

The auth-source enum extension follows the same pattern prohibit_silent_revert tasks used: append the value to the `choices=[...]` list in `cmd_fail_task` argparse, update the docstring comment block above. The test pattern from prohibit_silent_revert TASK-008 (`test_fail_task_authorization_source_accepts_reconcile_out_of_scope_user_instruction`) is the template — the new test is structurally identical but with the new value.
