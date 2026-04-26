# TASK-004 — Fixture path read from `CANONICAL_CONTRACT` + pre-archive lint

## Goal

Stop the `fixture-valid` Phase 0 gate from halting when `docs/plans/sample_phase4.md` is moved (e.g., into `archive/`) without simultaneously updating the gate. Read the fixture path from a single source of truth in `CANONICAL_CONTRACT`, and add a one-line lint that refuses to archive the fixture without updating the source-of-truth field.

## Context

**The friction.** During run 20260425T041800, Phase 0 preflight halted on `fixture not found: docs/plans/sample_phase4.md` because a prior commit had relocated the canonical fixture to `docs/plans/archive/sample_phase4.md`. The orchestrator restored the file at the canonical path with a `cp` and re-tracked it in commit `9b520c9`, but the gate's hardcoded path is the underlying footgun.

**The two corrective measures (post-mortem).**

1. **Single source of truth.** Hoist the fixture path to a field in `CANONICAL_CONTRACT` (or a sibling constant referenced by it). The gate, the audit, and any future relocations all key on it.
2. **Pre-archive lint.** Add a `lint-plans` (or audit-check) step that refuses to move `sample_phase4.md` (or its `.schedule.json` sidecar) under `archive/` without simultaneously updating the source-of-truth field.

Both are cheap; ship both. The lint catches the future-self error; the SoT removes the second-place that needs updating.

**Scope.** Code change to `plan_ops.py` (constant + gate read site + audit-check addition); tests covering both the "fixture moved" path (gate reports the canonical path from the constant) and the lint failure mode.

## Verification

- Grepping `plan_ops.py` for the literal string `'docs/plans/sample_phase4.md'` finds it in exactly one place: the `CANONICAL_CONTRACT` (or a sibling source-of-truth constant). The gate reads from there.
- The fixture-valid gate's error message names the path it expected AND attributes it to the source-of-truth constant (`expected_path: <path> (from CANONICAL_CONTRACT.fixture_path)`).
- A new audit check `_check_canonical_fixture_present` (in the AUDIT_CHECKS registry, "default" tier) fails when the path resolved from `CANONICAL_CONTRACT` is not present at the working tree root.
- A new audit check `_check_canonical_fixture_not_archived` fails when the resolved path lives under `docs/plans/archive/` — i.e., the SoT has been pointed at an archive location.
- New unit tests cover: (a) gate succeeds when the fixture is at the canonical path, (b) gate fails with the SoT-attributed message when missing, (c) the archive-lint audit-check fires when the SoT points under `archive/`.
- All existing `_gate_fixture_valid` tests continue to pass.

## Tasks

### TASK-004: Fixture path canonicalization + pre-archive lint

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — `CANONICAL_CONTRACT` constant + `_gate_fixture_valid` read site + new audit-check `_check_canonical_fixture_not_archived`)
  - `tests/scripts/test_plan_ops.py` (regression tests)
- **Dependencies:** [003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "fixture_valid or canonical_fixture"`
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::CANONICAL_CONTRACT`
  - `plugins/plan-executor/scripts/plan_ops.py::_gate_fixture_valid`
  - `plugins/plan-executor/scripts/plan_ops.py::AUDIT_CHECKS`
- **Acceptance criteria:**
  - `CANONICAL_CONTRACT` (or a clearly-named adjacent constant) gains a `fixture_path` field whose value is `'docs/plans/sample_phase4.md'`. Optional: also `fixture_schedule_path` for the sidecar.
  - `_gate_fixture_valid` reads the path from this field rather than from a string literal. The literal does not appear elsewhere in `plan_ops.py`.
  - The gate's failure-message JSON envelope adds an `expected_from` key naming the constant (`"expected_from": "CANONICAL_CONTRACT.fixture_path"`).
  - A new audit check `_check_canonical_fixture_not_archived` registers in `AUDIT_CHECKS` (tier `default`). It resolves the SoT path; if the path begins with `docs/plans/archive/`, the check returns a violation with a one-line remediation tip ("update `CANONICAL_CONTRACT.fixture_path` to point at the live fixture before archiving").
  - Tests: gate-success, gate-failure-with-SoT-attribution, archive-lint-fires, all green.
  - Existing fixture-valid tests pass unmodified (or are updated to assert the new `expected_from` key — the implementer chooses, but the choice is recorded in the report).
- **Reversion guidance:** revert the constant + audit-check + read-site change. The hardcoded literal can be reintroduced if the SoT abstraction proves heavy.

**Description:**
Hoist the fixture path to a single source of truth and add a pre-archive lint. Neither the gate nor the audit will silently break the next time the fixture moves.

**Implementation notes:**
- Prefer a single field under `CANONICAL_CONTRACT` over a sibling top-level constant — the contract is the natural home.
- The archive-lint audit-check belongs in the `default` tier (always run) so a failed lint surfaces in every Phase 0 audit cycle.
- Don't add the lint as a Phase 0 gate predicate (which would halt preflight) — it's an audit (advisory) so a deliberate archival isn't blocked. The remediation tip in the violation message is the user-facing affordance.
