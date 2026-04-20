# TASK-005 — Executor Phase Gates and Promotion Criteria

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-005
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9 (run phases), §14 (testing & conformance)
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract), TASK-002 (runtime validation — gates reuse these validators), TASK-004 (scheduler semantics — `execution-safe` requires correct batch fidelity).
**Issues absorbed:** none (new executor capability). This chunk promotes previously-informal checkpoints into enforced gates.

---

## Goal

Turn "ready to rerun" and "phase complete" from informal judgments into explicit runtime states with promotion criteria. The executor exposes six named gates — `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, `commit-safe` — each backed by a concrete predicate. Dry-run and execute mode have distinct pass conditions, documented and enforced. The Phase 5 rerun after TASK-001 through TASK-006 is a gateable certification event, not merely "try the script again".

## Scoped Context

The Phase 5 postmortem describes the current system as having scenario-passes and scenario-fails with no explicit definition of overall "ready". The rerun gate described in the consolidated report's §7 is the minimum viable unblock set, but "minimum viable" is not the same as "production-ready". This chunk introduces the gate *model* — a vocabulary and promotion function — that every downstream chunk (self-audit, sample fixture conformance, production rollout) can reference.

**The six gates.**

| Gate | Predicate | Implementation surface |
|---|---|---|
| `schema-valid` | Plan markdown conforms to §5 of design doc (required sections + task fields). | `plan_ops.py validate-plan` (new) or `plan-analyst` upstream checks. |
| `schedule-valid` | Analyst schedule passes `parse-schedule` (shape, DAG, no orphans, no duplicates). | TASK-002 `parse-schedule` already does this. Gate wraps it. |
| `fixture-valid` | Sample fixture (`docs/plans/sample_phase4.md`) passes both schema-valid and schedule-valid gates. | TASK-006 rewrites fixture; this gate certifies it. |
| `execution-safe` | Wrapper isolation + always-ignore + bounded timeout are in place. | TASK-003 implements; this gate asserts the presence of the baseline-snapshot pattern at all three seams. |
| `review-safe` | Review-path baseline is pre-dispatch; sibling state is protected; asymmetric review verdict vocabulary is consumed correctly. | TASK-003 implements; gate asserts it. |
| `commit-safe` | A successful task ends in a narrow commit that touches only `allowed_files` (+ orchestrator state updates authored by plan_ops). | Implementation partly in TASK-003 (scope enforcement), partly new in TASK-005 (post-commit check). |

**Dry-run vs execute pass conditions.**

Dry-run:
- `schema-valid` + `schedule-valid` + `fixture-valid` MUST hold.
- `execution-safe` / `review-safe` / `commit-safe` MUST be asserted by predicate (not invoked in dry-run).
- Pass condition: all six gates hold; no side effects on disk outside `docs/plans/<basename>.schedule.json` write (post TASK-002).

Execute:
- All six gates hold through the end of the run.
- Every committed task's commit-safety is re-verified post-hoc.
- Any gate violation during run halts the loop at the next batch boundary.

**Current state.** None of these gates are named or promoted. The orchestrator (`SKILL.md`) runs through the phases and relies on ad-hoc halts. The design doc describes the phases but not the gate promotion function.

---

## Verification

**V1 — Gate names appear in the helper.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --list --json
```

Returns a JSON array with the six gate names as members. Exit 0.

**V2 — Dry-run gate bundle.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --check schema-valid,schedule-valid,fixture-valid \
  --plan-file docs/plans/sample_phase4.md --json
```

After TASK-001 through TASK-006, returns all three green. Before TASK-006, returns `fixture-valid: fail` (fixture still pre-rewrite).

**V3 — Execution-safe / review-safe predicates.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --check execution-safe,review-safe --json
```

Predicate-based: greps `plugins/plan-executor/scripts/plan_codex_dispatch.py` for the always-ignore constant, baseline-snapshot call pattern, absence of `git clean -fd`. Returns green iff TASK-003 is applied.

**V4 — Commit-safe predicate.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --check commit-safe --commit-sha <sha> --task-id 001 --plan-file <plan> --json
```

Post-hoc verification: compare the commit's file list against the task's declared `files` (plus orchestrator always-ignore). Returns green iff the commit touched no files outside the allowed set.

**V5 — Gate surface in SKILL.md.**

```
grep -n 'gate' plugins/plan-executor/skills/implement-plan/SKILL.md
```

SKILL.md references the gate model explicitly (at least in phase A preflight and phase E post-commit).

**V6 — Phase 5 rerun as a certification event.**

The rerun procedure described in `DUAL_AGENT_PLAN_EXECUTOR.md §14` is now described as "certify all six gates hold through scenarios 1-12" rather than "run the scenarios".

**V7 — Promotion function round-trip.**

```
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --certify --plan-file docs/plans/sample_phase4.md --mode dry-run --json
```

After TASK-001-006, returns `{"certified": true, "gates": {"schema-valid": "pass", ..., "commit-safe": {"status": "not_applicable", "reason": "dry-run mode; no commits to verify"}}}`. Exit 0. The canonical status vocabulary is `pass` | `fail` | `not_applicable`; the dry-run qualifier lives in the `reason` field, not the status.

```
venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates --certify --plan-file docs/plans/sample_phase4.md --mode execute --run-id <id> --json
```

Requires a completed run (i.e., run after execute mode); returns gate status across the run.

---

## Tasks

### TASK-005: Add executor phase gates and promotion criteria

- **Status:** done
- **Priority:** high
- **Files:**
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Read-only context (not owned, not edited by TASK-005):**
  - `docs/plans/sample_phase4.md` — read as a fixture by `_gate_fixture_valid` at runtime and by unit tests. The rewrite is TASK-006's scope; TASK-005 must not edit this file. Therefore it must not appear in TASK-005's schedule `file_locks`.
- **Dependencies:** TASK-001, TASK-002, TASK-004
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k gates`
- **Acceptance criteria:**
  - **Must pass when TASK-005 lands (gate implementation + pass/fail test coverage):**
    - Six explicit gate predicates exist as `_gate_<name>` functions (`schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, `commit-safe`), each returning the documented `{name, status, reason}` shape.
    - Dry-run and execute mode have distinct pass conditions, documented in `DUAL_AGENT_PLAN_EXECUTOR.md` and implemented in `plan_ops.py gates` subcommand.
    - Phase 5 rerun is treated as a gateable certification event in the design doc, not merely a retry.
    - The consolidated rerun-gate minimum unblock set (TASK-001 through TASK-006) is preserved but phase gates are promoted into required executor behavior.
    - Verification checks V1, V5, V6, V7 (dry-run invocation shape) pass. V2 passes with `schema-valid` + `schedule-valid` green against the current fixture; `fixture-valid` may return `fail` until TASK-006 lands. V3 / V4 are implemented as predicates and pass against a known-good `plan_codex_dispatch.py` (asserted via unit tests with a fixture file); the predicates become green in the live tree once TASK-003 is applied.
  - **Deferred to a later certification run (tracked, not required for TASK-005 merge):**
    - Full fixture certification — i.e., V2 and V7 returning `fixture-valid: pass` end-to-end against the rewritten `sample_phase4.md` — is deferred to TASK-006 landing (see Step 7).
    - Live-tree green for V3 / V4 against the production `plan_codex_dispatch.py` is deferred to TASK-003 landing.
    - End-to-end Phase 5 certification ("all six gates hold across scenarios 1-12") is a Phase 5 rerun concern, not a TASK-005 merge concern.

**Description:**
This task ensures "ready to rerun" and "phase complete" are explicit runtime states, not informal judgments.

**Implementation notes:**
These gates should be enforced or surfaced by the executor itself, not left as documentation-only process advice. Gate predicates are small functions — avoid over-engineering into a framework.

**Reversion guidance:**
If gates are too strict initially, temporarily downgrade non-critical gates to warnings behind explicit override flags (`--allow-gate-failure <name>`). Keep the gate model itself. Never remove the gate vocabulary even if individual checks are softened.

---

## Implementation Playbook

### Step 1 — `plan_ops.py gates` subcommand

New subcommand with operations:

```
plan_ops.py gates --list [--json]
plan_ops.py gates --check <gate1,gate2> [--plan-file ...] [--commit-sha ...] [--task-id ...] [--json]
plan_ops.py gates --certify --plan-file <path> --mode (dry-run|execute) [--run-id ...] [--json]
```

Internal layout: one `_gate_<name>(context)` function per gate. Each returns `{"name": ..., "status": "pass"|"fail"|"not_applicable", "reason": "..."}`. The `--certify` path runs the set applicable for the given mode.

### Step 2 — gate predicates

**`_gate_schema_valid(plan_file)`**
- Read plan markdown; check `## Goal`, a context section (accept either `## Context` or `## Scoped Context` — match the canonical schema defined in TASK-001; if TASK-001 narrows the name, follow TASK-001), and `## Verification` top-level sections exist.
- For each `### TASK-NNN` block, check required bullets: `**Status:**`, `**Priority:**`, `**Files:**`, `**Dependencies:**`, `**Test command:**`, `**Acceptance criteria:**`; check `**Description:**` and `**Reversion guidance:**` prose sections.
- Pass if all checks hold.
- Reuses logic from `plugins/plan-executor/agents/plan-analyst.md` Step 2 heuristic where possible; do not duplicate.

**`_gate_schedule_valid(schedule_json)`**
- Call into the shared `_validate_schedule_dag` helper (factored in TASK-002).
- Run shape validation from `parse-schedule`.
- Pass iff no errors.

**`_gate_fixture_valid(plan_file=sample_phase4.md)`**
- Equivalent to `_gate_schema_valid + _gate_schedule_valid` on the sample fixture.
- Used by TASK-006 as its acceptance criterion; lives in this chunk because the gate vocabulary is established here.

**`_gate_execution_safe()`**
- Predicate against `plugins/plan-executor/scripts/plan_codex_dispatch.py`: grep for `ALWAYS_IGNORE`, grep for `snapshot_baseline(` calls at both `cmd_implement` and timeout paths, grep for absence of `git.*clean.*-fd` outside comments.
- Predicate-only — does not invoke the wrapper. Regresses together with TASK-003.

**`_gate_review_safe()`**
- Predicate against `plugins/plan-executor/scripts/plan_codex_dispatch.py`: grep for `snapshot_baseline(` at `cmd_review`, grep for always-ignore respect in the review cleanup path.
- Pass iff all checks hold.

**`_gate_commit_safe(commit_sha, task_id, plan_file)`**
- `git show --name-only <commit_sha>` to get changed file list.
- Look up task's `allowed_files` from the plan.
- `changed - allowed - always_ignore` must be empty.
- If empty, pass; else fail with the offending files.

### Step 3 — `--certify` mode bundles

```
def _certify_dry_run(plan_file):
    return _gate_schema_valid(plan_file) + _gate_schedule_valid(...) + _gate_fixture_valid() + _gate_execution_safe() + _gate_review_safe()
    # commit-safe is not_applicable

def _certify_execute(plan_file, run_id):
    # Load run log for run_id, iterate completed commits, call _gate_commit_safe per commit.
    # Include the dry-run set as well.
```

### Step 4 — `SKILL.md` wire-up

At the top of `plugins/plan-executor/skills/implement-plan/SKILL.md`, add a "Promotion criteria" subsection listing the six gates. In each phase:

- Phase A (preflight): "Run `plan_ops.py gates --check schema-valid,schedule-valid,fixture-valid` before dispatching the analyst. Halt on fail."
- Phase E (post-commit): "After every successful task commit, run `plan_ops.py gates --check commit-safe --commit-sha <sha> --task-id <id> --plan-file <plan>`. On fail, revert the commit and mark the task failed."
- End of run: "Run `plan_ops.py gates --certify --mode execute --run-id <id>`; include the report in the execution log."

### Step 5 — design doc updates

`DUAL_AGENT_PLAN_EXECUTOR.md`:
- New §9.7 "Promotion criteria and gates" — defines the six gates, their predicates, and their use in dry-run vs execute mode.
- §14 (testing): reference the gate model. Phase 5 certification = "all six gates hold across scenarios 1-12".

### Step 6 — tests

`tests/scripts/test_plan_ops.py`:
- `test_gates_list_returns_six_names`
- `test_gate_schema_valid_passes_on_spec_plan`
- `test_gate_schema_valid_fails_on_missing_verification_section`
- `test_gate_schedule_valid_reuses_parse_schedule_validators`
- `test_gate_execution_safe_predicate` — assert a known-good `plan_codex_dispatch.py` passes (may skip or mock)
- `test_gate_commit_safe_happy_path` — set up tiny scratch repo with a known commit
- `test_gate_commit_safe_detects_scope_violation` — commit touches unrelated file; gate reports it
- `test_certify_dry_run_bundle`
- `test_certify_execute_bundle` (may mark slow)

### Step 7 — interaction with TASK-006

TASK-006 rewrites `sample_phase4.md` to the canonical schema. `_gate_fixture_valid` is the acceptance predicate TASK-006 targets. TASK-005 lands first — it establishes the gate vocabulary and the `_gate_schema_valid` / `_gate_schedule_valid` predicates that `_gate_fixture_valid` composes. At TASK-005 merge time, `_gate_fixture_valid` is present as code and exercised by unit tests over synthetic fixtures, but running it against the live `sample_phase4.md` is **expected to return `fail`** until TASK-006 rewrites the fixture; see the Acceptance criteria split above. No circularity: TASK-005 does not require the live fixture to be green, and TASK-006's verification depends on TASK-005's predicates existing.

### Step 8 — regression sweep

`venv/bin/pytest -q tests/scripts/test_plan_ops.py`. All pre-existing tests green; new gate tests green.

---

## Out of Scope

- **Self-audit / drift detection**: TASK-007 builds on the gate vocabulary but adds a different capability.
- **Canonical contract**: TASK-001.
- **Runtime validation** at handoff seams: TASK-002.
- **Wrapper isolation**: TASK-003 — gates merely *assert* it, they do not implement it.
- **Scheduler semantics**: TASK-004.
- **Fixture rewrite**: TASK-006 — the `fixture-valid` gate asserts it, but the rewrite itself is TASK-006.
- **Portability / preflight scope**: TASK-008.
- **Large-file reads / global locks / bounded logs**: TASK-009, 010, 011.

## Reversion guidance

- **Gate subcommand:** safe to revert; SKILL.md temporarily falls back to implicit checks. Do not delete the design §9.7 text unless the gate vocabulary is abandoned; the vocabulary itself is reusable even without the subcommand.
- **`commit-safe` predicate:** if its scope-checking misbehaves on commits that legitimately include orchestrator-owned files (e.g., run-log updates), extend the always-ignore list rather than disabling the gate.
- **Phase 5 rerun as certification:** safe to revert the description; the scenarios themselves do not depend on the framing.
- **Individual gate predicates:** if one regresses for an edge case, prefer `--skip-gate <name>` over deletion so the operator can temporarily proceed while diagnosing.

## Execution log — 20260420T220109 (paused)

Starting SHA: `f5e8951d179201a842916ce38352c0d34d0adbe2`  → Ending SHA: `f5e8951d179201a842916ce38352c0d34d0adbe2`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 005 | claude | codex+d5+codex | needs-rework -> partial-agreement -> needs-rework (binding) | (paused; pending edits in working tree) | D.2a.6 narrow-remediation retry [narrow-remediation] [disagreement: 4]; second Codex review flagged 5 new findings (4 important, 1 minor); paused per D.2a.6 step 7 for user decision |
