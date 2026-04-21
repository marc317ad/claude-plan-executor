# Sample Phase 4 Plan — /implement-plan verification fixture

**Created:** 2026-04-13
**Status:** in-progress
**Base branch:** main

## Goal

End-to-end conformance fixture for the `/implement-plan` dual-agent executor.
Exercises mixed routing via the plan-analyst classifier, dependency ordering,
a seeded failure, per-batch interleaving, and parallel-sibling state
isolation. Certifies that the executor's six promotion gates hold before
execution (dry-run bundle) and after execution (execute bundle).

## Context

This fixture is the canonical conformance artifact referenced by
`DUAL_AGENT_PLAN_EXECUTOR.md` §14. It is validated automatically by
`plan_ops.py gates --check fixture-valid`, which aggregates
`schema-valid` + `schedule-valid` against the fixture and its sidecar
`sample_phase4.schedule.json`. Phase 0 preflight runs the gate before
every `/implement-plan` dispatch; the gate is expected to return `pass`
here so upstream plans do not inherit a fixture-validation halt.

All file work happens under `docs/plans/sample_phase4_scratch/`. Running
the skill against this plan creates throwaway files that can be deleted
post-run. Do NOT point real source files at this fixture.

### Scenario mapping (Phase 5 matrix reference)

- **TASK-001 — mechanical create.** Routes to Codex via the classifier
  heuristic (pure file creation, no judgment). Exercises the Phase B
  happy path and scope enforcement (scenarios 2, 3, 8-dry-run).
- **TASK-002 — judgment edit.** Routes to Claude via the classifier
  (naming and idiom choices, not mechanical transformation). Depends on
  TASK-001 and lives in its own batch, so it exercises the cross-review
  direction Claude→Codex (scenario 7). Its diff must not include
  TASK-004's edits (scenario 8 — review cleanliness / interleaving
  regression).
- **TASK-003 — parallel mechanical create.** Batch 1 sibling of
  TASK-001. Disjoint files, so the two run in parallel and exercise the
  parallel-sibling state-isolation contract (scenario 7).
- **TASK-004 — seeded failure.** Depends on TASK-003 and edits the same
  `settings.py` that TASK-002 wrote. Acceptance criteria deliberately
  contradict TASK-002's post-state, so the implementer cannot satisfy
  both. Exercises Phase C `fail-task stage=implement` and
  `block-dependents` (scenarios 9–12: retry, binding, third-opinion,
  role-swap).

### Downstream impact

Pre-rewrite, the `fixture-valid` gate failed with schema violations and
blocked Phase 0 for every plan run. Post-rewrite, the gate returns
`pass`, unblocking all downstream `/implement-plan` dispatches.

## Verification

After a successful run against this fixture:

- **Three `feat` commits** (TASK-001, TASK-002, TASK-003). No commit for
  TASK-004.
- **TASK-004 status** is `failed` in this plan file; a `failed` event
  with `stage=implement` is present in `docs/plans/_run_log.jsonl`.
- **Plan-level status:** the top-level `**Status:**` bullet flips to
  `partial` (not `complete`, because TASK-004 failed).
- **Review cleanliness:** the diff shown to the reviewer for TASK-002
  contains only TASK-002's edits, not TASK-004's (the per-batch
  interleaving contract).
- **Gate certification:**
  - `plan_ops.py gates --certify --mode dry-run` returns
    `certified: true` with `commit-safe: not_applicable`.
  - `plan_ops.py gates --certify --mode execute --run-id <id>` returns
    `certified: true` with `commit-safe: pass` for every `commit_done`
    event in the run.

The `fixture-valid` gate itself is run in Phase 0 preflight and must
return `pass` for this plan as a precondition to any execute-bundle
certification.

## Tasks

### TASK-001: Create rename helper

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/rename_helper.py (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/rename_helper.py` exists.
  - Defines `def rename(old, new): return {"old": old, "new": new}` at
    module level.
  - No other files are touched.

**Description:**
Mechanical create of a trivial helper module. The file does not exist
yet and must be created with a single two-line function body and nothing
else. No imports, no surrounding prose, no tests in the same file. The
analyst classifier should route this to Codex because the task is pure
file creation with a fixed body — no naming or structural judgment is
required.

**Implementation notes:**
The target body is exactly:

```python
def rename(old, new):
    return {"old": old, "new": new}
```

Create the parent directory `docs/plans/sample_phase4_scratch/` if it
does not exist.

**Reversion guidance:**
Delete `docs/plans/sample_phase4_scratch/rename_helper.py`. No
downstream callers exist at this point in the schedule.

### TASK-002: Wire rename helper into scratch settings

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/settings.py (create)
- **Dependencies:** TASK-001
- **Test command:** venv/bin/python -c "import ast; ast.parse(open('docs/plans/sample_phase4_scratch/settings.py').read())"
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/settings.py` exists.
  - The module imports `rename` from the sibling `rename_helper`
    module using the exact flat-directory form
    `from rename_helper import rename`.
  - The module defines a module-level dictionary
    `SETTINGS = {"version": "phase4-sample"}`.
  - The module parses as valid Python (test command above exits 0).

**Description:**
Wire the new helper into a tiny settings surface. The scratch directory
is a flat folder (no `__init__.py`), so the required import form is
`from rename_helper import rename` — a relative import would fail at
runtime against that layout. Pick a module-level constant name
consistent with project naming conventions. The analyst classifier
should route this to Claude because the work involves naming and
structural judgment, not mechanical transformation.

**Implementation notes:**
The scratch directory is a flat folder, not a Python package (no
`__init__.py`). Use the flat import form. Keep the module body small —
one import line and one dict assignment.

**Reversion guidance:**
Delete `docs/plans/sample_phase4_scratch/settings.py`. TASK-004 would
otherwise attempt to edit this file; that edit will be rolled back by
Phase C `fail-task` when TASK-004 fails.

### TASK-003: Create scratch constants module

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/constants.py (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/constants.py` exists.
  - Defines `VERSION = "phase4-sample"` at module level.
  - No other files are touched.

**Description:**
Parallel sibling of TASK-001 in batch 1. Mechanical create of a fixed
constant — no judgment required. The analyst classifier should route
this to Codex. Exercises parallel-sibling state isolation: TASK-001 and
TASK-003 run in the same batch, under separate wrapper invocations, and
neither may observe the other's intermediate files.

**Implementation notes:**
Target body is exactly:

```python
VERSION = "phase4-sample"
```

Nothing else.

**Reversion guidance:**
Delete `docs/plans/sample_phase4_scratch/constants.py`.

### TASK-004: Seed failure — wire constants into settings (contradictory)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/settings.py
- **Dependencies:** TASK-002, TASK-003
- **Test command:** none
- **Acceptance criteria:**
  - `settings.py` MUST contain `from constants import VERSION` as its
    first non-blank line.
  - `settings.py` MUST retain the
    `SETTINGS = {"version": "phase4-sample"}` line installed by
    TASK-002 (the prerequisite edit must be preserved).
  - `settings.py` MUST NOT contain any `SETTINGS` dictionary
    assignment.
  - The retention and removal clauses above are mutually exclusive by
    construction: no implementation can satisfy both at once, so this
    task is expected to fail regardless of upstream state.

**Description:**
Seeded failure. TASK-004's acceptance criteria contain an internal
contradiction — one clause requires retaining the `SETTINGS` dict
TASK-002 installed, another forbids any `SETTINGS` assignment — so the
implementer cannot satisfy the criteria under any prior state. Making
the contradiction internal (rather than resting it on TASK-002's
post-state alone) keeps the seeded failure robust even if the batch
ordering changes. TASK-004 declares TASK-002 as a prerequisite so the
fixture still exercises the per-batch interleaving contract (two
tasks touching `settings.py` across different batches), but the
failure itself is driven by this task's own criteria. The wrapper's
post-execution acceptance check surfaces the contradiction as
`failure`; Phase C then runs `fail-task stage=implement` and
`block-dependents` (no downstream task depends on TASK-004, so no
cascade blocks land).

Expected run-log entry:
`{"event":"failed","task_id":"004","stage":"implement","reason":"..."}`.

This task exists specifically to exercise scenarios 9–12 (retry,
binding, third-opinion, role-swap) without committing broken code to
the tree.

**Implementation notes:**
Do not attempt to paper over the contradiction — it is the point of the
fixture. If the implementer "fixes" the acceptance criteria to make the
task pass, the fixture loses its seeded-failure scenario and the
executor's Phase C path goes untested.

**Reversion guidance:**
`fail-task` already handles cleanup: `git restore` for tracked files,
`unlink` for untracked creates inside `allowed_files`. No manual
reversion required.

## Expected outcome

Captured in ## Verification above.
