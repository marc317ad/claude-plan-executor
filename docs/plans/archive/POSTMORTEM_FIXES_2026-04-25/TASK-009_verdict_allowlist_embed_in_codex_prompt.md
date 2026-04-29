# TASK-009 — Embed canonical verdict allowlists in Codex review/plan-review prompts

## Goal

Reduce the Codex-reviewer false-positive rate by embedding the four canonical verdict allowlists directly in the Codex `review` and `plan-review` prompts as a "DO NOT confuse these sets" reference. Three of seven Codex reviews in run 20260425T041800 returned verdicts the D.5 third opinion overruled; one (TASK-017) was outright vocabulary confusion between the Codex-reviewer set and the D.5 set.

## Context

**The friction.** The post-mortem's "Cross-cutting observation 1" enumerates three Codex review false positives:

- **TASK-017:** Codex flagged `parse-d5-adjudication` example using verdict `needs-rework` as "not a real D.5 verdict." `needs-rework` IS in `ALLOWED_D5_VERDICTS` (`plan_ops.py:110–113`). Codex conflated the Codex-reviewer set (`clean | minor-findings | needs-rework`) with the D.5 set (`ship | ship-with-fixes | partial-agreement | needs-rework`).
- **TASK-009 Phase 1.5:** Codex flagged a Batch 1 file collision between TASK-009 and TASK-017. Per-task `tasks[].files[]` shows the file sets are disjoint; Codex misread the batch `file_locks` aggregate.
- **TASK-010 finding 0:** Codex flagged that `write-schedule` doesn't tag `global_lock`. By design, `compute-schedule` does the tagging upstream.

All three were correctly caught and dismissed by D.5 / plan-review-triage, but each cost a triage dispatch + orchestrator unblock.

**The mechanic.** Embed in the Codex `review` and `plan-review` prompts (as a section near the top, BEFORE the task-specific content):

```
## Canonical verdict allowlists

You are running as the {role}. Your verdict MUST come from your role's allowlist below. Do NOT confuse role allowlists.

- Codex review (your verdict in `review`): clean | minor-findings | needs-rework
- Codex plan-review (your verdict in `plan-review`): clean | minor-findings | needs-replan
- Claude review (NOT YOUR ROLE): ship | ship-with-fixes | partial-agreement | needs-rework
- D.5 third opinion (NOT YOUR ROLE): ship | ship-with-fixes | partial-agreement | needs-rework

When the plan or task content references verdicts from another role's allowlist, treat that as expected (the plan documents the cross-role mapping). Do NOT flag a plan-text reference to e.g. `needs-rework` as an "invalid verdict" — it is valid in another role's set.
```

**Optional pairing.** For schedule-level Codex `plan-review` findings, additionally embed the canonical `tasks[].files[]` listing (per-task ownership) in the prompt — not just the batch `file_locks` union — so Codex can verify per-task ownership without inferring from the union. This addresses the TASK-009 Phase 1.5 false positive directly.

**Scope.** Spec-only edits to the prompt-render path in `plan_codex_dispatch.py` (the helper that constructs the Codex review/plan-review prompts). No allowlist constants need to move — they stay in `plan_ops.py`; the prompt-render path imports/loads them and embeds the rendered text. Tests verify the embedding is present in the rendered prompt.

## Verification

- Codex `review` rendered prompt (via `cmd_review --dry-run --emit-prompt` or equivalent) contains a "Canonical verdict allowlists" section near the top, with all four role allowlists enumerated.
- Codex `plan-review` rendered prompt similarly contains the allowlist section, plus a "Task-level file ownership" subsection enumerating each `tasks[].files[]` entry (when the schedule sidecar is available).
- The allowlist text reads from the canonical constants in `plan_ops.py` (`ALLOWED_CODEX_REVIEW_VERDICTS` / `ALLOWED_CLAUDE_REVIEW_VERDICTS` / `ALLOWED_D5_VERDICTS` / `ALLOWED_PLAN_REVIEW_VERDICTS`) so a future allowlist edit propagates automatically.
- A unit test renders both prompts against a fixture task/plan and asserts: (a) the allowlist section is present, (b) all four role names appear, (c) the role-disambiguation paragraph ("Do NOT flag a plan-text reference...") is present.
- Existing wrapper tests stay green.
- This task does NOT change verdict-allowlist contents anywhere; it only embeds existing constants in the prompt.

## Tasks

### TASK-009: Verdict allowlist embed in Codex prompts

- **Status:** done
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit — `cmd_review` and `cmd_plan_review` prompt-render paths; reads canonical constants)
  - `plugins/plan-executor/scripts/plan_ops.py` (light edit — expose canonical allowlists if not already module-level; optional helper to format them)
  - `tests/scripts/test_plan_codex_dispatch.py` (regression tests)
- **Dependencies:** [008]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py -k "verdict_allowlist or canonical_verdicts"`
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::ALLOWED_CODEX_REVIEW_VERDICTS`
  - `plugins/plan-executor/scripts/plan_ops.py::ALLOWED_CLAUDE_REVIEW_VERDICTS`
  - `plugins/plan-executor/scripts/plan_ops.py::ALLOWED_D5_VERDICTS`
  - `plugins/plan-executor/scripts/plan_ops.py::ALLOWED_PLAN_REVIEW_VERDICTS`
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::cmd_review`
- **Acceptance criteria:**
  - Codex `review` prompt rendering injects a "Canonical verdict allowlists" section listing all four role allowlists. The four lists read from the canonical `plan_ops.py` constants.
  - Codex `plan-review` prompt rendering injects the same section AND, when a schedule sidecar is available, a "Task-level file ownership" subsection enumerating each `tasks[].files[]` entry (one bullet per task).
  - The role-disambiguation paragraph ("Do NOT flag a plan-text reference to another role's verdict as invalid") is present in both prompts.
  - Tests assert the rendered prompt contains the allowlist section + all four role names + the disambiguation paragraph.
  - **Dynamic-read assertion (load-bearing):** at least one test uses `unittest.mock.patch` (or equivalent) to temporarily override one of the canonical allowlist constants (e.g. add a synthetic verdict like `"synthetic-test-verdict"` to `ALLOWED_CODEX_REVIEW_VERDICTS`), re-renders the prompt, and asserts the synthetic verdict appears in the rendered text. This pins that the renderer reads the constants dynamically rather than embedding a snapshot at module-import time. The override is reverted after the test.
  - The allowlist text uses the constants directly — no string duplication that could drift.
  - The injected section is bounded (one short paragraph + four bullet lists); no nested explanations beyond the role-disambiguation note.
- **Reversion guidance:** revert the prompt-render injection. The allowlist constants stay; no semantic regression.

**Description:**
Embed the canonical verdict allowlists in Codex review/plan-review prompts so Codex stops conflating role-specific verdict sets. Optionally embed per-task `tasks[].files[]` ownership in plan-review prompts to prevent batch-aggregate misreads.

**Implementation notes:**
- Read the constants once at render time; do not hardcode the verdict strings in the prompt template — they would drift when the constants change.
- The role-disambiguation paragraph is the load-bearing sentence; the bullet lists alone proved insufficient (Codex still misread on TASK-017 even though the lists existed in `plan_ops.py`).
- Resist scope creep: do NOT add allowlist embeds to Claude reviewer prompts in this task. Claude has not exhibited the same vocabulary confusion.

## Execution log — 20260426T032452 (success)

Starting SHA: `4220c0aa16b60579f826d4d8a8ed3150cc7d39a4`  → Ending SHA: `5d5a0637e945998fe546965c0f7764ebf3b53b71`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 009 | claude | codex | clean | 5d5a0637 |  |
