---
task_id: "008"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Recovery crash-window table W0–W5; CLI Surface item 9"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-008
depends_on: ["007"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-008 — Implement recover subcommand (W0–W5 crash windows)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Recovery crash-window table; CLI Surface item 9
**Base branch:** main
**Chunk dependencies:** 007

---

## Goal

Add `recover`, the idempotent resume subcommand. It reads `<slug>.journal.json.committed_state.phase`, disambiguates crash windows W0–W5 by comparing on-disk state against `{planned_parent_fingerprint, prior_parent_fingerprint, parent_fingerprint_post_swap, task_fingerprints}`, and re-runs the necessary `commit-swap` phases to reach `phase: "committed"`.

## Scoped Context

W0 is the hardest window — `phase == "pre-parent-rename"` can mean three distinct on-disk states that require DIFFERENT recovery actions:

- **W0-post** (`staging_parent ABSENT AND F_prod == planned_parent_fingerprint`): the parent rename succeeded; the phase-2 journal write was lost. Write phase-2 then resume from step 5.
- **W0-pre** (`staging_parent PRESENT AND F_prod == prior_parent_fingerprint (or both null)`): the phase-1 journal was written but nothing else moved. Resume from step 3 (atomic parent rename).
- **W0-ambig** (anything else): abort `pre-rename-state-ambiguous` — recovery-state corruption that NO force flag bypasses.

Windows W1–W5 have single-path recoveries described in the crash-window table. W1 re-stages tasks if staging is lost; W2 resumes from step 7; W3 verifies `task_fingerprints` then deletes backup; W4 finalizes journal; W5 regenerates the cache manifest from production state.

If journal reports `phase: "committed"`, exit 0 with `{"noop": true}`.

Hand-edit detection during recovery MUST hard-abort:
- `parent-hand-edit-during-recovery` when W1 finds `F_prod != parent_fingerprint_post_swap`.
- `tasks-hand-edit-during-recovery` when W3 finds current task fingerprints differ from `journal.task_fingerprints`.

Both are recovery-state corruption errors (no `--force` bypass). Human triage required.

## Verification

- Inject crash after phase-1 journal write but before `os.rename`; run `recover` → W0-pre path; final state identical to a non-interrupted run.
- Inject crash after `os.rename(staging_parent, production_parent)` but before phase-2 write; run `recover` → W0-post path; phase-2 written, tasks flow completes.
- Modify production parent manually after W1 crash; run `recover` → exit non-zero with `parent-hand-edit-during-recovery`.
- Delete `<slug>.journal.json`; `recover` exits non-zero with a diagnostic that the journal is missing (main flow's initial-run path is handled by `commit-swap`, not `recover`).

---

## Tasks

### TASK-008: Implement recover subcommand

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add `recover` handler dispatching on `committed_state.phase` → W0 disambiguation + W1/W2/W3/W4/W5 resume paths
- **Dependencies:** 007
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "recover or W0 or W1 or W2 or W3 or W4 or W5" -x`
- **Acceptance criteria:**
  - `phase: "committed"` → exit 0 with `{"noop": true}`; no writes.
  - W0 disambiguation exactly matches the three-way rule above; W0-ambig never attempts a write.
  - W1/W2/W3/W4/W5 recovery paths match the crash-window table.
  - `parent-hand-edit-during-recovery` and `tasks-hand-edit-during-recovery` are hard aborts with non-zero exit and NO bypass flag.
  - `--staging-dirs-json <json>` optional: reuse existing staging when present; re-render from schedule when missing.
  - On any recoverable phase, re-enters `commit-swap` at the correct step — no duplication of the 7-phase state machine.
  - Final journal state is `phase: "committed"` at success exit.

**Description:**
Makes partial failure of `commit-swap` recoverable. Reads the journal, compares disk state to the fingerprint triad, and finishes the protocol. All unsafe states (hand-edit during recovery, ambiguous pre-rename) hard-abort instead of guessing.

**Implementation notes:**
Do NOT duplicate phase logic — call back into `commit-swap`'s phase functions with the correct entry point. Journal file atomic-write is shared.

**Reversion guidance:**
Revert the `recover` hunk in `decomp_ops.py`.
