---
task_id: "017"
task_type: gate
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Phase B — Shadow run against pinwheel plan"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-017
depends_on: ["011", "012", "013", "014", "015", "016"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [prose_omitted_gate] -->

# TASK-017 — GATE: Phase B shadow run on pinwheel plan

**Source section:** Phase B — Shadow run against pinwheel plan
**Base branch:** main
**Chunk dependencies:** 011, 012, 013, 014, 015, 016

---

## Goal

Shadow-run the decomposer against the pinwheel plan. All Phase B hard gates pass before Phase C.

## Tasks

### TASK-017: Phase B shadow run

- **Status:** open
- **Priority:** high
- **Files:**
  - `docs/plans/we-need-to-make-compressed-pinwheel.md` — first-run parent-plan copy (from `~/.claude/plans/`)
- **Dependencies:** 011, 012, 013, 014, 015, 016
- **Test command:** none
- **Acceptance criteria:**
  - `/decompose-plan docs/plans/we-need-to-make-compressed-pinwheel.md --dry-run` exits 0 with staging dirs populated and no production writes.
  - Live run exits 0; `00_INDEX.json` chunk count == TASK files on disk.
  - Three emitted TASK files each return `outcome: valid` from `plan_ops.py preflight` (plan-analyst).
  - Supersede smoke test: hand-edit one TASK body, re-run on that TASK file, parent transitions to `Superceeded` with `superseded_by: [<child_ids>]`; 2–5 children emitted; all children plan-analyst-valid; manifest history records the supersede entry.
  - Recovery smoke test (general mid-swap crash): second run reads journal `committed_state.phase`, completes the protocol; end state byte-identical to a single successful run.
  - Recovery W0 smoke test: crash after parent rename, before phase-2 journal write → `recover` detects `staging_parent ABSENT AND F_prod == planned_parent_fingerprint`, writes phase-2, completes.
  - Parent-copy drift smoke test: hand-edit production parent → re-run aborts `parent-copy-divergent` with all three fingerprints; re-run with `--force-parent` succeeds and records audit entry.
  - Phase B hard gates all pass: reason-code coverage (split + merge + prose per TASK), dependency integrity, zero gate-TASK `missing-test-command` noise, zero hard prose-budget violations.
  - `validate-output` exits 0 on the live production run.

**Description:**
Manual gate; no code changes. Operator verifies every acceptance criterion above.

**Reversion guidance:**
If a gate criterion fails, fix the underlying implementer TASK (typically 003–015) and re-run the gate.

## Verification

- `/plugin list` shows `plan-decomposer` (via `claude --plugin-dir` during shadow run).
- `venv/bin/python plugins/plan-decomposer/scripts/decomp_ops.py validate-output --tasks-dir docs/plans/decompose_plans_tasks/we-need-to-make-compressed-pinwheel/ --plan-dir docs/plans/` exits 0.
- `jq '.chunks | length' docs/plans/decompose_plans_tasks/we-need-to-make-compressed-pinwheel/00_INDEX.json` equals the TASK file count.
- Manifest `history[]` contains at least one `supersede` entry and one `parent_override` audit entry after the smoke tests.
