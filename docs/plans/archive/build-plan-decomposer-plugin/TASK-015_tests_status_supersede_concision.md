---
task_id: "015"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Tests cases 47–61"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-015
depends_on: ["005", "006", "009"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-015 — Tests: cross-plugin status + supersede-after-exec + concision (cases 47–61)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Tests cases 47–61
**Base branch:** main
**Chunk dependencies:** 005, 006, 009

---

## Goal

Extend `tests/scripts/test_decomp_ops.py` with test cases 47–61 (15 cases) covering plan-analyst `task_type: gate` suppression, `sync-status` promotion/demotion and idempotency, the `supersede-after-execution` prohibition, and the 11 concision rules (trivial elision, gate slim body, parent-plan inline emission both directions, hard/soft budget violations at both render-side and validator-side, overlap merge, supersede re-minimization, prose-decision coverage).

## Scoped Context

Case 47 invokes `plan-analyst` (or its equivalent unit-level check) — bridges the TASK-012 suppression rule back to the decomposer's output format. The fixture MUST declare `task_type: gate` + `Test command: none`, run plan-analyst, and assert the gap list EXCLUDES `missing-test-command`.

Case 55 (render-side hard budget): synthetic TASK candidate with 500-token implementer-facing prose → agent workflow step 11 aborts `prose-budget-exceeded`, leaves staging intact, does NOT call `commit-swap`. Reducing to 350 tokens succeeds.

Case 57 (validator-side hard budget): hand-crafted TASK in staging with 500-token implementer-facing body but all other checks passing → `validate-output` exits non-zero `prose-budget-exceeded`.

Case 58 (soft budget): 250-token body (200 < x < 400) → `validate-output --json` exits 0 with `warnings[]` containing one `prose-budget-soft` entry.

Case 60 (supersede re-minimization): parent TASK with 600-token legacy body → supersede run emits 3 children each with implementer-facing body ≤ 400 tokens; acceptance criteria preserved semantically; narrative prose rebuilt fresh.

Case 61 (coverage): hand-corrupt `_manifest.json.history[]` to remove `prose_decisions` entries → `validate-output` exits non-zero `prose-decisions-missing`.

## Verification

- `python -m pytest tests/scripts/test_decomp_ops.py -k "sync_status or task_type or supersede_after or concision or prose_budget" -x` green.
- At least 15 new test functions (47–61).

---

## Tasks

### TASK-015: Tests — status + supersede-after-exec + concision

- **Status:** open
- **Priority:** high
- **Files:**
  - `tests/scripts/test_decomp_ops.py` — append 15 test functions for cases 47–61
  - `tests/fixtures/plan-decomposer/gate_task.md` + `standard_task.md` — fixtures for cases 47–48
  - `tests/fixtures/plan-decomposer/concision_*.md` — fixtures for cases 51–59
- **Dependencies:** 005, 006, 009
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "sync_status or task_type or supersede_after or concision or prose_budget" -x`
- **Acceptance criteria:**
  - Case 47 (plan-analyst gate suppression): fixture with `task_type: gate` + `Test command: none` → `outcome: valid`, NO `missing-test-command` gap; standard fixture still emits the gap.
  - Cases 48–49 (sync-status): promotion happy path + refusal-to-demote without `--force` + `--force` demotion.
  - Case 50 (supersede-after-execution): body `- **Status:** done` → abort `supersede-after-execution`; `--force-supersede` records audit entry.
  - Case 51 (trivial elision): 1-file 2-criteria TASK → no Scoped Context / no Playbook / no Out of Scope / no inline Parent-plan; manifest records `self_evident + trivial_scope + bounded_by_files + self_contained`.
  - Case 52 (gate slim body): `task_type: gate` render has only Goal + Tasks + Verification; token count ≤ 60; records `prose_omitted_gate`.
  - Cases 53–54 (parent-plan inline): emitted on cross-cutting dep; omitted when self-contained with `prose_omitted_self_contained` recorded.
  - Case 55 (render hard-budget): 500-token body → step 11 aborts `prose-budget-exceeded`; staging retained; `commit-swap` never invoked. 350 tokens succeeds.
  - Case 56 (gate budget): 100-token gate TASK → abort `prose-budget-exceeded` (ceiling 60); 50 tokens succeeds.
  - Case 57 (validator hard gate): 500-token staging body + other checks passing → `validate-output` exits non-zero `prose-budget-exceeded`.
  - Case 58 (validator soft gate): 250-token body → exit 0 with `warnings[]` containing `prose-budget-soft`.
  - Case 59 (overlap merge): candidate with Description+Playbook paraphrasing same step → merged into Description; Playbook absent; `prose_merged_overlap` recorded.
  - Case 60 (supersede re-minimization): parent 600-token → children ≤ 400 each; acceptance criteria preserved; narrative rebuilt.
  - Case 61 (prose-decision coverage): manifest missing `prose_decisions[]` for some `task_id` → `validate-output` exits non-zero `prose-decisions-missing`.

**Description:**
Closes the test matrix. Covers both production-code paths added in TASK-009 (sync-status) and the render + validator concision gates, plus the supersede-after-execution prohibition.

**Reversion guidance:**
Revert the 15 appended test functions and the new fixtures.
