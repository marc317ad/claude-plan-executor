# TASK-006 — Sample & Verification Plans as Conformance Artifacts

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-006
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §5 (plan schema), §14 (testing & conformance)
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract), TASK-005 (`fixture-valid` gate — the acceptance predicate).
**Issues absorbed:** ISSUE-002

---

## Goal

Rewrite `docs/plans/sample_phase4.md` so it conforms to the canonical plan schema established in TASK-001. The rewritten fixture must (a) pass plan-analyst's required-field check, (b) pass `parse-schedule` in the canonical `id`/`index` shape, (c) preserve the Phase 4 scenario richness — mixed routing by analyst classifier, dependency ordering, seeded failure, review cleanliness, interleaving regression — and (d) certify via the `fixture-valid` gate from TASK-005. The executor can then use this fixture as a conformance artifact during every Phase 5 rerun and every self-audit.

## Scoped Context

### ISSUE-002 (P0) — `sample_phase4.md` fails the plan schema

Three independent non-conformances (see `../../analysis/DUAL_AGENT_EXECUTOR_Design_vs_Implementation_Gap_Report.md` §2 Defect B):

**Top-level sections.**
- Current `docs/plans/sample_phase4.md:7` uses `## Purpose`; design §5 (line 149) specifies `## Goal`.
- `sample_phase4.md:23` uses `## How to run`; design does not prescribe this section (optional prose is fine but should not replace required sections).
- No `## Verification` section; design §5 line 156 requires one.
- `sample_phase4.md:96` uses `## Expected outcome`; can remain as optional prose.

**Per-task fields (each of TASK-001 through TASK-004 in the fixture).**
- Missing `**Priority:** critical|high|medium|low`.
- Missing `**Description:**` prose block.
- Missing `**Reversion guidance:**` prose block.
- `**Dependencies:**` uses bracket form `[001]`; design §5 line 172 prescribes `none | TASK-NNN, TASK-NNN`.
- Contains `**Agent:** codex|claude` — a field not in the canonical schema; routing is classifier-owned (`plugins/plan-executor/agents/plan-analyst.md` classifies during Step 3).

**Status vocabulary.**
- `**Status:** open`; TASK-001 moves canonical vocabulary to `pending`. The fixture must align.

### Scenario preservation (critical — do not lose on rewrite)

The fixture's *purpose* is to exercise the Phase 4 contract surface. The rewrite must keep each of the following behaviors intact:

1. **Mixed agent routing via classifier.** Some tasks should route to Codex, some to Claude, based on the analyst's Step 3 heuristic (mechanical → Codex; judgment → Claude). Do not hardcode `**Agent:**`; shape the task descriptions so the classifier picks different agents. E.g., "create a file with fixed content" is mechanical (Codex); "wire a helper into a config with non-obvious naming conventions" is judgment (Claude).
2. **Dependency ordering.** At least one task depends on another; used to validate `batch-next` batch fidelity (TASK-004).
3. **Seeded failure.** One task contains a contradictory acceptance criterion so the implementer fails, exercising Phase C cascade-block (`fail-task`, `block-dependents` — TASK-004).
4. **Review cleanliness / interleaving regression.** Two tasks should touch the same file in different batches; the reviewer's diff for the earlier-batch task must contain only that task's edits, not the later task's. Phase 5 Scenario 8.

### Reference: `tests/scripts/test_plan_codex_dispatch_integration.py:27-71`

A spec-compliant plan is already embedded in the integration test — use it as the shape reference. It proves the schema *is* understood; the fixture just was not upgraded. The rewrite does not copy that plan verbatim (test-embedded plan is minimal) but follows the same structural pattern.

### Downstream impact — real blocked run (2026-04-20)

TASK-026 (`docs/plans/DUAL_AGENT_Plans/TASK-026_phase_gate_review_drift_followup.md`) is the concrete motivating example of why this rewrite is on the critical path. On 2026-04-20 the orchestrator attempted to run TASK-026 and halted at Phase 0 preflight:

```
fixture-valid: fail
  schema-valid failed: schema violations: missing Goal section (## Goal);
  missing Verification section (## Verification); TASK-001 missing bullet
  **Priority:**; TASK-001 missing prose header **Description:**; ...
```

The failure is entirely in `sample_phase4.md` (not TASK-026's plan file), but per current SKILL.md Phase 0 semantics the halt applies because the plan under execution is not the sample itself. TASK-026 is blocked until TASK-006 lands and the fixture passes `fixture-valid`.

This is a hard-ordering signal: TASK-006 must merge before TASK-026 can run — and TASK-026's own Finding 3 will close the loop by narrowing the preflight halt set so the failure mode cannot recur for future plans even if the fixture regresses.

---

## Verification

**V1 — `fixture-valid` gate passes.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --check fixture-valid --json
```

Exit 0, status pass. Requires TASK-005 landed.

**V2 — Required sections present.**

```bash
grep -nE '^## (Goal|Context|Verification)$' docs/plans/sample_phase4.md
```

All three match exactly once.

**V3 — Required task fields present on every task.**

```bash
for field in Priority Description 'Reversion guidance'; do
  count=$(grep -c "\\*\\*${field}:" docs/plans/sample_phase4.md)
  echo "$field: $count"
done
```

All three must report 4 (one per task; fixture has 4 tasks per the Phase 4 scenario design).

**V4 — No `**Agent:**` field on any task.**

```bash
grep -nE '^\s*-\s*\*\*Agent:\*\*' docs/plans/sample_phase4.md
```

Must return zero matches.

**V5 — Dependency format canonical.**

```bash
grep -nE '^\s*-\s*\*\*Dependencies:\*\*' docs/plans/sample_phase4.md
```

Each match should read either `- **Dependencies:** none` or `- **Dependencies:** TASK-NNN[, TASK-NNN]...` — not `[001]`.

**V6 — Status vocabulary canonical.**

```bash
grep -nE '^\s*-\s*\*\*Status:\*\*\s*open\b' docs/plans/sample_phase4.md
```

Zero matches (post TASK-001).

**V7 — Analyst classifies tasks without hardcoded routing.**

Dispatch `plan-analyst` on the rewritten fixture. Expected: at least one task classified as `codex`, at least one as `claude`, based on task descriptions — not on any `**Agent:**` field (there are none).

**V8 — Scenario preservation checklist.**

Manually verify the rewritten fixture still exercises:
- [ ] Mixed routing (classifier picks ≥1 Codex and ≥1 Claude).
- [ ] Dependency graph with at least one non-trivial edge (TASK-B depends on TASK-A; batch 2 depends on batch 1).
- [ ] Seeded failure on one task (contradictory acceptance criterion).
- [ ] Per-batch interleaving: two tasks touching the same file across two batches.

Document the scenario-preservation mapping inside the fixture's `## Context` section (one paragraph explaining which task exercises which scenario).

**V9 — `parse-schedule` via real analyst round-trip.**

```bash
# After TASK-002 the integration test exists:
venv/bin/pytest -q tests/scripts/test_plan_ops.py::test_analyst_to_parse_schedule_roundtrip
```

Passes on the rewritten fixture.

---

## Tasks

### TASK-006: Rebuild sample and verification plans as conformance artifacts

- **Status:** pending
- **Priority:** high
- **Files:**
  - `docs/plans/sample_phase4.md`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `tests/scripts/test_plan_ops.py`
  - `tests/scripts/test_plan_codex_dispatch_integration.py` (only if the embedded fixture is promoted or cross-referenced)
- **Dependencies:** TASK-001, TASK-005
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k fixture`
- **Acceptance criteria:**
  - ISSUE-002 is fully resolved.
  - `sample_phase4.md` conforms to the canonical plan schema: `## Goal`, `## Context`, `## Verification`; per-task `**Priority:**`, `**Description:**`, `**Reversion guidance:**`; no `**Agent:**` field; `**Dependencies:**` in `TASK-NNN, TASK-NNN | none` form; `**Status:** pending` (canonical vocabulary).
  - The sample fixture still exercises mixed routing, dependency order, seeded failure, and review cleanliness.
  - Sample and verification plans are validated automatically as conformance artifacts via the `fixture-valid` gate before they are used to certify executor behavior.
  - Verification checks V1–V9 pass.

**Description:**
Reference fixtures must test the real contract, not a nearby approximation.

**Implementation notes:**
Keep the scenario richness; remove undocumented shortcuts. The rewrite is almost entirely mechanical once the schema is clear — but scenario preservation is where mistakes happen. Before finalizing, walk through the Phase 4 scenario list and confirm each scenario still has a task exercising it.

**Reversion guidance:**
If fixture migration causes disruption (e.g., a scenario no longer reachable), preserve the old artifact as `docs/plans/sample_phase4_legacy.md` with a header comment explaining "adversarial / non-conforming — not for conformance gates" so it can stay as a historical reference. Do not revert the canonical rewrite.

---

## Implementation Playbook

### Step 1 — draft the rewritten fixture

Open a fresh copy of `docs/plans/sample_phase4.md`. Replace the header block plus every section with the canonical schema. Target structure:

```
# Sample Phase 4 Plan — /implement-plan verification fixture

**Created:** 2026-04-13
**Status:** in-progress
**Base branch:** main

## Goal

End-to-end conformance fixture for the `/implement-plan` dual-agent executor. Exercises mixed routing, dependency ordering, a seeded failure, and per-batch interleaving. Certifies that the executor's six promotion gates hold before and after execution.

## Context

...one paragraph on the fixture's purpose and scenario-mapping...

Scenario mapping (Phase 5 matrix reference):
- TASK-001: mechanical create — routes to Codex via classifier; exercises Phase B happy path + scope enforcement.
- TASK-002: judgment edit — routes to Claude via classifier; depends on TASK-001; exercises cross-review.
- TASK-003: mechanical create — parallel with TASK-001 in batch 1; exercises parallel-sibling state isolation (Scenario 7).
- TASK-004: seeded failure — depends on TASK-003; contradictory acceptance criterion; exercises Phase C fail-task + block-dependents.

All tasks operate under `docs/plans/sample_phase4_scratch/`.

## Verification

After a successful run:
- 3 feat commits (TASK-001, 002, 003); no commit for 004.
- TASK-004 status is `failed` in this plan file; `failed` event in `_run_log.jsonl`.
- Plan-level `**Status:**` is `partial`.
- TASK-002's Codex review diff contains only TASK-002's edits (no TASK-004 bleed).
- `plan_ops.py gates --certify --mode execute --run-id <id>` returns green on all six gates.

## Tasks

### TASK-001: Create rename helper

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/rename_helper.py (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/rename_helper.py` exists with a `def rename(old, new): return {"old": old, "new": new}` function.
  - No other files touched.

**Description:**
Mechanical create of a trivial helper module. Classifier should route this to Codex (pure file creation, no judgment required).

**Reversion guidance:**
Delete the file; no downstream callers.

### TASK-002: Wire rename helper into scratch settings

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/settings.py (create)
- **Dependencies:** TASK-001
- **Test command:** venv/bin/python -c "import ast; ast.parse(open('docs/plans/sample_phase4_scratch/settings.py').read())"
- **Acceptance criteria:**
  - `settings.py` imports `rename` from the local `rename_helper` module.
  - Defines a module-level `SETTINGS = {"version": "phase4-sample"}` dictionary.
  - Module parses as valid Python (test command above exits 0).

**Description:**
Wires the new helper into a tiny settings surface, choosing an idiomatic import name and a reasonable module-level constant. Classifier should route this to Claude because the work involves a naming/structural judgment, not mechanical transformation.

**Reversion guidance:**
Delete the file.

### TASK-003: Create scratch constants module

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/constants.py (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/constants.py` defines `VERSION = "phase4-sample"` at module level.
  - No other files touched.

**Description:**
Parallel with TASK-001 in batch 1. Mechanical create, routes to Codex. Exercises the parallel-sibling state-isolation contract from TASK-003 of the hardening plan.

**Reversion guidance:**
Delete the file.

### TASK-004: Seed failure — wire constants into settings (contradictory)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/sample_phase4_scratch/settings.py (edit)
- **Dependencies:** TASK-003
- **Test command:** none
- **Acceptance criteria:**
  - `settings.py` contains `from .constants import VERSION` at the top.
  - `settings.py` MUST NOT contain any `SETTINGS` dictionary (deliberately contradicts TASK-002's output).
  - This contradiction is intended: the implementer cannot satisfy both constraints, so the task is expected to fail and cascade-block via Phase C.

**Description:**
Seeded failure. The acceptance criteria contradict TASK-002's post-state. Implementer attempts; the wrapper's independent test and scope checks should classify this as failure (either `failure` outcome or `scope_violation`). Phase C then runs `fail-task stage=implement` and `block-dependents` (no downstream task here, so no blocks cascade).

Expected run-log entry: `{"event":"failed","task_id":"004","stage":"implement","reason":"..."}`

**Reversion guidance:**
`fail-task` already handles the cleanup per TASK-004 of the hardening plan (`git restore` for tracked, unlink for untracked creates inside allowed_files).

## Expected outcome

Captured in ## Verification above.
```

### Step 2 — manual scenario walkthrough

With the rewritten fixture drafted, walk the Phase 5 scenario list (postmortem §2):
- Scenario 1 — portability spot check: unchanged by the fixture.
- Scenario 2 — dry-run stickiness: requires the fixture to pass analyst (not `invalid`). After rewrite, yes.
- Scenario 3 — preflight: unchanged.
- Scenario 4 — analyst cycle: unchanged.
- Scenario 5 — lock collision: unchanged.
- Scenario 6 — `--task-ids`: after TASK-004 of hardening delivers `filter-schedule`, this works against the rewritten fixture.
- Scenario 7 — parallel sibling: exercised by TASK-001 + TASK-003 in batch 1.
- Scenario 8 — interleaving regression: exercised by TASK-002 and TASK-004 both writing to `settings.py` across two batches.
- Scenario 9-12 — retry, binding, third-opinion, role-swap: exercised by the TASK-004 seeded failure.

Record the mapping in the fixture's `## Context` section (already shown above).

### Step 3 — update the design doc

`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` §14:
- Reference the rewritten fixture as the canonical conformance artifact.
- Cross-reference `plan_ops.py gates --check fixture-valid`.
- Note that the fixture passes `fixture-valid` is a precondition for any `--certify` run.

### Step 4 — tests

`tests/scripts/test_plan_ops.py`:
- `test_sample_phase4_passes_fixture_valid_gate` — invoke `gates --check fixture-valid` and assert pass.
- `test_sample_phase4_has_no_hardcoded_agent_field` — grep the file; assert zero matches.
- `test_sample_phase4_has_required_task_fields` — parse the file; assert every `### TASK-NNN` block has all mandatory bullets.

### Step 5 — CI sanity

Run full suite. All tests green.

---

## Out of Scope

- **Canonical contract choices (including status vocabulary):** TASK-001.
- **`fixture-valid` gate implementation:** TASK-005 delivers the gate; this chunk only uses it as the acceptance predicate.
- **Wrapper isolation / parallel test:** TASK-003 delivers the test; this chunk only guarantees the fixture exercises the scenario.
- **Scheduler semantics:** TASK-004.
- **Portability / preflight:** TASK-008.
- **New capabilities** (self-audit, scale-aware reads, global locks, bounded logs): TASK-007 through 011.

## Reversion guidance

- **Scenario richness loss:** if in the rewrite a scenario is accidentally dropped, restore only the TASK block that exercises it, preserving the canonical schema. Never re-add `**Agent:**` as a workaround.
- **Classifier routing surprise:** if the analyst routes a task to an unexpected agent, tune the task's `**Description:**` (e.g., add or remove "judgment" cues) rather than adding `**Agent:**`.
- **Fallback artifact:** keep the pre-rewrite fixture as `docs/plans/sample_phase4_legacy.md` with a header note explaining why it is non-conforming; use it only for adversarial tests. Do not let anything in the executor rely on it.
