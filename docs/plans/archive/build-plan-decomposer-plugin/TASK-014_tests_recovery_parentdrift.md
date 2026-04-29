---
task_id: "014"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Tests cases 30–43"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-014
depends_on: ["007", "008"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-014 — Tests: recovery + cross-device + parent-drift (cases 30–43)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Tests cases 30–43
**Base branch:** main
**Chunk dependencies:** 007, 008

---

## Goal

Extend `tests/scripts/test_decomp_ops.py` with test cases 30–43 (14 cases) covering the crash-window matrix (W0 happy/ambiguous/W1/W2/W3/W4/W5), `--force-parent` override paths, cross-device rename guard (simulated via `os.stat().st_dev` mock), journal-lost mid-crash, missing-production-parent, and untracked-parent divergence.

## Scoped Context

Crash simulation: inject a synthetic abort between named `commit-swap` phase functions via monkey-patch (`raise SystemExit` after phase-N journal write OR after named `os.rename`). Tests then invoke `recover` on the resulting disk state and assert the final state is byte-identical to a non-interrupted run — this is the idempotent-recovery guarantee.

Case 30a (W0-post): crash after `os.rename(staging_parent, production_parent)` but before phase-2 journal write. Recovery must detect `staging_parent ABSENT AND F_prod == planned_parent_fingerprint`, write phase-2, resume step 5.
Case 30b (W0-ambig): crash leaves ABSENT staging with `F_prod != planned` → MUST abort `pre-rename-state-ambiguous` without writing.
Case 33 (hand-edit during recovery): user edits production parent between crash and re-run → hard abort `parent-hand-edit-during-recovery`.
Case 37 (cross-device): monkey-patch `os.stat` to return differing `st_dev` → abort `cross-device-rename-unsafe` exit 4, zero rename attempts.
Case 41 (journal lost): delete `<slug>.journal.json` between phases → decomposer warns, treats as initial run; pre-swap invariant check catches any divergence.

## Verification

- `python -m pytest tests/scripts/test_decomp_ops.py -k "recover or crash or cross_device or parent_" -x` green.
- At least 14 new test functions (30, 30a, 30b, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43 — including the 30a/30b split).

---

## Tasks

### TASK-014: Tests — recovery + cross-device + parent-drift

- **Status:** open
- **Priority:** high
- **Files:**
  - `tests/scripts/test_decomp_ops.py` — append 14 test functions for cases 30–43
  - `tests/scripts/conftest.py` — add `commit_swap_with_crash_at_phase` helper fixture if not already present
- **Dependencies:** 007, 008
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "recover or crash or cross_device or parent_" -x`
- **Acceptance criteria:**
  - Case 30 (W0-pre genuine): crash before `os.rename` → `recover` resumes from step 3; final fingerprints identical.
  - Case 30a (W0-post): crash after parent rename, before phase-2 → `recover` writes phase-2 and completes; final fingerprints identical.
  - Case 30b (W0-ambig): crash state outside the two valid rows → `recover` exits non-zero with `pre-rename-state-ambiguous`.
  - Cases 31–32 (W1, W4): post-parent-rename and post-tasks-rename → `recover` completes; end state identical.
  - Case 33 (parent hand-edit during recovery): `recover` exits non-zero with `parent-hand-edit-during-recovery`; no writes.
  - Cases 34–36 (parent-copy divergent): production hand-edit without `--force-parent` aborts; with `--force-parent` succeeds and records override audit entry; source-also-edited case reports all three fingerprints.
  - Case 37 (cross-device): exit 4 `cross-device-rename-unsafe`; zero renames attempted (assert on a spy).
  - Cases 38–39 (W2, W3): resume from correct step; W3 validates `task_fingerprints` before backup cleanup.
  - Case 40 (journal vs manifest cache divergence): `recover` regenerates cache manifest; no staging work.
  - Case 41 (journal lost): warn + treat as initial; pre-swap invariant catches divergence.
  - Case 42 (parent missing + journal has fingerprint): abort `parent-copy-missing`; `--force-parent` records `parent_override_reason: missing`.
  - Case 43 (parent exists + journal untracked): `F_prod == F_source` → adopt; `F_prod != F_source` → abort `parent-copy-untracked` unless `--force-parent`.

**Description:**
The crash-window tests are the safety net for the 7-phase protocol. Without them, recovery drift in later refactors would go unnoticed until a real partial failure corrupted a consumer's output.

**Reversion guidance:**
Revert the 14 appended test functions and the `conftest.py` helper.
