# TASK-026 — Phase-Gate Review Drift Follow-up (post-TASK-005)

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-005
**Originating chunk:** [`./TASK-005_phase_gates.md`](./TASK-005_phase_gates.md)
**Base branch:** `main`
**Audit anchor commit:** `f5e8951`
**Chunk dependencies:** TASK-005 (phase gates shipped; this task closes review-drift findings that surfaced after commit).
**Issues absorbed:** none (new follow-up).
**Origin note:** Surfaced during TASK-005 run `20260420T220109` Codex review pass-4 (`post-narrow-remediation-2`) after three rounds of hand-fixes against earlier passes already landed. User elected option A (commit TASK-005 as "technically complete" and file a separate follow-up) rather than a fourth hand-fix round under the same run. The TASK-005 `commit_done` record carries these five findings as `disposition: deferred` with `disposition_reason` pointing to this chunk.

---

## Goal

Close the five Codex review findings surfaced in TASK-005 pass-4 that fall on the boundary between "genuine gap" and "reviewer drift vs plan acceptance criteria." Two are load-bearing invariant gaps (`execution-safe` seam fidelity, `commit-safe` always-ignore consistency). One reconciles a doc/skill drift in the Phase 0 preflight halt set (now post-TASK-006: `fixture-valid` returns to the strict halt set alongside `schema-valid` / `schedule-valid`). Two are small polish fixes (promotion-table phase cell typo, V3/V4 test strategy).

Each finding below is addressed on its own merits. Where the reviewer conflicted with the plan's acceptance criteria (e.g. V3/V4 deferred-to-TASK-003), the reconciliation is to pin the test strategy to fixture-based assertions (as the plan originally specified) rather than widen live-tree assertions.

---

## Scoped Context

### Why this chunk exists (and why not as pass-4 rework)

TASK-005 passed through four Codex review cycles under run `20260420T220109`:

| Pass | Verdict | Findings | Resolution |
|---|---|---|---|
| initial | needs-rework | 5 | narrow-remediation-1 hand-fix + commit-safe protected-path narrowing |
| post-narrow-remediation | needs-rework | 5 | narrow-remediation-2 hand-fix + comment-hardening + schedule-shape contract |
| post-narrow-remediation-2 | needs-rework | 5 | deferred to TASK-026 (this plan) |

The diminishing-returns pattern — each pass surfaced five new findings touching different parts of the gate surface, never the same parts twice — indicates the plan's acceptance criteria was not tight enough to converge a single run. TASK-005 is "technically complete" against its own V1–V7 acceptance checks: six gate predicates exist, the `gates` subcommand surface is wired, and the design doc + SKILL.md document the promotion criteria. What the pass-4 review flagged is a mix of:

1. **Invariant-fidelity gaps.** The gates exist but some predicates can be fooled. Specifically `execution-safe` requires two distinct snapshot call sites per the design doc (implement dispatch seam AND timeout cleanup), but the current predicate only asserts generic presence. Similarly `commit-safe` claims "always-ignore subtraction" in the doc/skill but implements a narrower allowlist.
2. **Doc/skill/impl divergence.** SKILL.md's promotion table lists `schedule-valid` under Phase 0 preflight, but the workflow correctly writes the schedule in Phase 1. The Phase 0 hard-halt text also still carries leftover wording from TASK-005's deferral of `fixture-valid` live-green to TASK-006; with TASK-006 landed, the wording needs to be reconciled to the canonical halt-on-fail semantics for all three preflight gates.
3. **Test strategy alignment.** V3/V4 in the plan explicitly defer live-tree green until TASK-003 lands, and prescribe fixture-based assertions. A subset of tests in `test_plan_ops.py` still assert against the live wrapper, not fixtures.

The cost of collapsing these into a fourth hand-fix round under the same run is: (a) every edit restarts the review loop, (b) the diminishing-returns pattern suggests the fifth pass would surface another five findings elsewhere, (c) the run is already ~2.5 hours in and has consumed its useful work budget. Filing a targeted follow-up chunk lets each finding be addressed on its own merits with proper acceptance criteria.

### What NOT to do in this chunk

- Do NOT re-open the six-gate vocabulary. Adding a seventh gate or renaming an existing one is out of scope — the gate model shipped in TASK-005 and downstream chunks already reference it.
- Do NOT rewrite `_gate_schema_valid`. Its predicates are correct; pass-4 did not flag it.
- Do NOT touch the sample fixture (`docs/plans/sample_phase4.md`). That rewrite is TASK-006's scope.
- Do NOT change the `{name, status, reason}` shape or `pass|fail|not_applicable` vocabulary. Those are canonicalized.
- Do NOT widen the gate CLI beyond the existing `--list`, `--check`, `--certify` surface.

### The five findings, restated with scope calls

#### Finding 1 — `execution-safe` does not assert two distinct snapshot call sites (important)

Plan acceptance V3 says `execution-safe` is predicate-based and asserts baseline-snapshot pattern "at all three seams" (implement-dispatch + timeout cleanup + review). Pass-4 found the current predicate only requires a generic `_snapshot_baseline(` presence in `cmd_implement` plus `_handle_timeout_cleanup(...baseline)` anywhere in the timeout branch. A wrapper with only a pre-dispatch snapshot (no cleanup snapshot) would still pass.

**Fix scope:** Tighten `_gate_execution_safe` to require a snapshot call inside the implement-dispatch region AND a snapshot call inside the timeout-cleanup region (parsed as separate windows, not module-wide). Pass-3 already added comment-stripping; this pass adds window-scoped assertions.

#### Finding 2 — `commit-safe` does not subtract the documented always-ignore set (important)

The design doc (§9 commit-safe) and SKILL.md both say commit-safe subtracts `{task files ∪ always-ignore ∪ plan file}`. Pass-3 correctly removed the `is_protected_path()` fallback (because "protected" is broader than "always-ignore"), but the replacement allowlist only adds the plan file and `00_INDEX.json`. Legitimate run-log or state bookkeeping changes that `commit-task` itself authors (e.g. `docs/plans/_run_log.jsonl`) will now fail `commit-safe`.

**Fix scope:** Reconcile the two by introducing a shared `COMMIT_ALWAYS_IGNORE` set in `_plan_paths.py` (the narrow set of paths `commit-task` itself writes: `docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `docs/plans/*.schedule.json` for the current plan's basename, and the 00_INDEX.json sidecar). Use it in `_gate_commit_safe` AND in `commit-task`'s self-authored-path allowlist check. Update the design doc + SKILL.md to name the set explicitly.

The reviewer's alternate suggestion ("use the same allowlist as `commit-task`") is accepted in spirit; the implementation approach (shared constant) eliminates drift.

#### Finding 3 — Phase 0 preflight halt set reconciliation (important)

SKILL.md §Phase-0 preflight currently still carries TASK-005's interim wording that treats `fixture-valid` as a potential self-reference hazard ("if the plan under execution is the sample fixture itself, demote to warning"). With TASK-006 landed, `sample_phase4.md` is now schema-conformant and `fixture-valid` returns `pass` globally; the interim demotion clause is inert and adds ambiguity about the canonical halt contract.

**Target behavior (post-TASK-006, durable):** Phase 0 preflight runs `schema-valid`, `schedule-valid`, and `fixture-valid`; all three are strict halt-on-fail. There is no `--warn-only` flag, no `warn` status, and no fixture-version detection inside the predicate. The gate status vocabulary remains the frozen `pass|fail|not_applicable`.

**Fix scope:** Remove the residual interim demotion wording from SKILL.md so the Phase 0 instruction block cleanly states the strict halt-on-fail contract for all three preflight gates. No predicate changes, no CLI-surface additions, no status-vocabulary changes.

#### Finding 4 — Promotion table phase cell for `schedule-valid` (minor)

SKILL.md promotion table line 27 says `schedule-valid` runs in Phase 0 preflight. The detailed workflow correctly says `write-schedule` runs in Phase 1, and the gate runs immediately after persistence. Fix the table cell; no code change.

**Fix scope:** One-line edit to the promotion table.

#### Finding 5 — V3/V4 tests assert against live wrapper (minor)

TASK-005 acceptance criterion explicitly says: "V3 / V4 are implemented as predicates and pass against a known-good `plan_codex_dispatch.py` (asserted via unit tests with a fixture file); the predicates become green in the live tree once TASK-003 is applied." The pass-4 review correctly noted that `test_gate_execution_safe_predicate` and `test_gate_review_safe_predicate` still assert green against the live wrapper, which works today only because pass-3 confirmed the wrapper already satisfies the invariants — but this coupling will break if TASK-003 is rolled back or if the wrapper evolves.

**Fix scope:** Convert the live-wrapper positive tests to fixture-based: construct a minimal known-good `plan_codex_dispatch.py` fixture that hits the predicate invariants, run the gate against that fixture, assert green. Keep the live-wrapper negative tests (assertions that a stripped-down wrapper fails the gate) as-is — those already use fixture files.

### Files this task edits

- `plugins/plan-executor/scripts/plan_ops.py` — `_gate_execution_safe` window-scoped assertions (finding 1); `_gate_commit_safe` uses shared `COMMIT_ALWAYS_IGNORE` (finding 2).
- `plugins/plan-executor/scripts/_plan_paths.py` — new `COMMIT_ALWAYS_IGNORE` constant (finding 2).
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — Phase 0 preflight interim demotion clause removed so all three preflight gates are strict halt-on-fail (finding 3); promotion-table cell fix (finding 4); promotion-criteria section notes the `COMMIT_ALWAYS_IGNORE` set.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` — §9.7 commit-safe text names `COMMIT_ALWAYS_IGNORE` (finding 2); §9.7 execution-safe names the two-seam invariant (finding 1); Phase 0 preflight scope reaffirmed as strict halt-on-fail for `schema-valid` / `schedule-valid` / `fixture-valid` (finding 3).
- `tests/scripts/test_plan_ops.py` — add window-scoped negative tests for `execution-safe` (finding 1); add positive-fixture + negative-real-protected-path tests for `commit-safe` (finding 2); convert V3/V4 positive tests to fixture-based (finding 5).

### Files this task does NOT edit

- `docs/plans/sample_phase4.md` — TASK-006's scope; already landed and out of scope here.
- `plugins/plan-executor/agents/plan-analyst.md` — not in the gate path.
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — definitively read-only for this task. The `commit-task` subcommand (and its self-authored-path allowlist check) lives in `plugins/plan-executor/scripts/plan_ops.py`, not in `plan_codex_dispatch.py`. Finding 2's shared-constant adoption therefore only touches `_plan_paths.py` (definition) and `plan_ops.py` (`_gate_commit_safe` plus `commit-task`'s allowlist check); `plan_codex_dispatch.py` has no `commit-task` guard to update. The schedule's `file_locks` intentionally exclude it.

---

## Verification

**V1 — `execution-safe` predicate fails when only one seam has `_snapshot_baseline()`.**

Build a fixture wrapper with a `_snapshot_baseline()` call in `cmd_implement` but none in the timeout-cleanup region. `plan_ops.py gates --check execution-safe --wrapper-file <fixture>` returns `fail`. Build a second fixture with both seams present; returns `pass`.

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestGateExecutionSafeWindowScoped
```

**V2 — `commit-safe` uses `COMMIT_ALWAYS_IGNORE` consistently.**

Construct a scratch repo commit touching `docs/plans/_run_log.jsonl` + `docs/plans/foo.schedule.json` + the plan + one declared file. `plan_ops.py gates --check commit-safe --commit-sha <sha>` returns `pass`. Construct a second commit touching `docs/plans/_run_log.jsonl` + the plan + one declared file + an undeclared `plugins/foo.py` (protected but not allowed). Returns `fail` with the undeclared path in the reason.

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestGateCommitSafeAlwaysIgnoreConsistency
```

**V3 — Phase 0 preflight halt set is strict (post-TASK-006).**

SKILL.md's Phase 0 preflight instruction lists `schema-valid`, `schedule-valid`, and `fixture-valid` as strict halt-on-fail gates with no interim demotion clause. A smoke test reads the skill text and fails if the interim wording (for example "demote to warning", "pre-TASK-006", or "self-reference") is still present in the Phase 0 block. A second smoke test confirms the CLI surface does not expose a `--warn-only` flag and the emitted gate status vocabulary remains `pass|fail|not_applicable`.

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestSkillPreflightStrictHalt
```

**V4 — Promotion-table phase cell for `schedule-valid` matches workflow.**

```
grep -n 'schedule-valid' plugins/plan-executor/skills/implement-plan/SKILL.md
```

The table row for `schedule-valid` lists Phase 1 (post-write), not Phase 0.

**V5 — V3/V4 positive tests use fixture wrappers.**

```
grep -n 'test_gate_execution_safe_predicate\|test_gate_review_safe_predicate' tests/scripts/test_plan_ops.py
```

Both positive-path tests reference fixture files under `tests/fixtures/` (or equivalent) rather than opening `plugins/plan-executor/scripts/plan_codex_dispatch.py` directly. Negative-path tests continue to use fixture files (no change).

**V6 — Shared `COMMIT_ALWAYS_IGNORE` constant.**

```
grep -n 'COMMIT_ALWAYS_IGNORE' plugins/plan-executor/scripts/_plan_paths.py plugins/plan-executor/scripts/plan_ops.py
```

The constant is defined in `_plan_paths.py` and imported + used in `plan_ops.py`. If adopted by `plan_codex_dispatch.py`'s commit-task guard, it appears there too with a single import statement.

**V7 — Design doc §9.7 names the invariants.**

```
grep -n 'two distinct snapshot\|COMMIT_ALWAYS_IGNORE' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
```

Both the two-seam invariant and the always-ignore set are named explicitly, not left as prose generalizations.

---

## Tasks

### TASK-026: Close phase-gate review drift from TASK-005 pass-4

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/_plan_paths.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `tests/scripts/test_plan_ops.py`
- **Read-only context (not edited by TASK-026):**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` — definitively read-only. `commit-task` lives in `plan_ops.py`, not here, so Finding 2's shared-constant adoption does not require any change to this file. Schedule `file_locks` therefore MUST NOT include it.
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "gates or commit_safe or execution_safe or review_safe or preflight"`
- **Acceptance criteria:**
  - Finding 1 (execution-safe two-seam): `_gate_execution_safe` asserts a `_snapshot_baseline()` call inside the implement-dispatch window AND inside the timeout-cleanup window, parsed as separate regions. Negative tests cover each-seam-missing case.
  - Finding 2 (commit-safe always-ignore): `COMMIT_ALWAYS_IGNORE` constant defined in `_plan_paths.py`; `_gate_commit_safe` uses it as the sole ignore set (no protected-path fallback); `commit-task`'s self-authored-path allowlist check (in `plan_ops.py`, same file) uses the same constant. Tests assert the set's members are ignored while protected-but-not-declared paths fail the gate.
  - Finding 3 (Phase 0 preflight halt set): SKILL.md Phase 0 preflight instruction lists `schema-valid`, `schedule-valid`, and `fixture-valid` as strict halt-on-fail gates, with no interim demotion clause, no `--warn-only` flag introduction, and no change to the `pass|fail|not_applicable` status vocabulary. Smoke test asserts this.
  - Finding 4 (promotion-table cell): SKILL.md promotion-criteria table row for `schedule-valid` cites Phase 1 (post-write), not Phase 0.
  - Finding 5 (V3/V4 test strategy): `test_gate_execution_safe_predicate` and `test_gate_review_safe_predicate` positive paths use fixture-file wrappers, not the live `plan_codex_dispatch.py`. Negative paths already use fixtures; unchanged.
  - All verification checks V1–V7 pass.
- **Deferred to downstream chunks (tracked, not required for TASK-026 merge):**
  - Live-tree green for V3/V4 against the production `plan_codex_dispatch.py` is TASK-003's scope.

**Description:**
This task closes five review findings surfaced after TASK-005 was committed. Two are invariant-fidelity gaps in existing gate predicates; two are doc/skill alignment fixes; one is a test-strategy alignment. None of these change the gate vocabulary or the promotion model; they tighten predicates and reconcile text.

**Implementation notes:**
Keep the changes minimal — each finding is addressed in the one file it affects plus its regression test. Avoid re-opening TASK-005's acceptance surface; the gate CLI shape, status vocabulary, and certification mode bundles are frozen.

For finding 2, audit `commit-task`'s guard (inside `plan_ops.py`, same file as `_gate_commit_safe`) to confirm the current self-authored-path set matches the proposed `COMMIT_ALWAYS_IGNORE` before writing the constant. If they diverge, the constant must be the union (or `commit-task`'s set explicitly scoped) so both ends agree. Document the decision in §9.7. Note: `plan_codex_dispatch.py` has no `commit-task` guard and is not touched by this task.

For finding 3, simply remove the interim demotion wording from SKILL.md's Phase 0 block; all three preflight gates are strict halt-on-fail. Do not add a `--warn-only` CLI flag and do not introduce a `warn` status — the frozen gate status vocabulary is `pass|fail|not_applicable`.

**Reversion guidance:**
If finding 3's reconciliation (strict halt-on-fail for all three preflight gates) later surfaces a legitimate fixture-version-skew scenario, handle it by repairing the fixture or by a targeted plan-side exception note — do NOT re-introduce a `--warn-only` CLI flag or a `warn` gate status, since those would widen the frozen gate surface.

If the shared `COMMIT_ALWAYS_IGNORE` set introduces friction (e.g. a legitimate bookkeeping path needs adding later), extend the set in a small follow-up rather than re-introducing the broader `is_protected_path` fallback.

---

## Implementation Playbook

### Step 0 — Confirm the TASK-006 bootstrap carve-out is already gone

The one-shot SKILL.md bootstrap exception that TASK-006 used during its own run was removed as part of TASK-006's cleanup; `grep -n 'TASK-006 bootstrap carve-out' plugins/plan-executor/skills/implement-plan/SKILL.md` must return zero matches before proceeding. If for any reason a stray sentinel span still exists, delete the span inclusive of both `<!-- BEGIN ... -->` / `<!-- END ... -->` comments and re-run the grep to confirm zero matches. No further edit is expected in this step; it exists as a safety check before Step 4 rewrites the surrounding Phase 0 preflight text.

### Step 1 — Shared `COMMIT_ALWAYS_IGNORE` constant

Add to `plugins/plan-executor/scripts/_plan_paths.py`:

```python
# Paths that commit-task itself writes during orchestration. These are allowed
# in commit-safe without being declared in a task's Files: list, because the
# orchestrator — not the implementer — authors them.
COMMIT_ALWAYS_IGNORE: frozenset[str] = frozenset({
    "docs/plans/_run_log.jsonl",
    "docs/plans/_run_lock.json",
    # 00_INDEX.json sidecars live alongside each plan bundle
    "docs/plans/DUAL_AGENT_Plans/00_INDEX.json",
    # Schedule sidecars are per-plan; the plan file's basename drives match
})

def is_commit_always_ignore(path: str, plan_basename: str | None = None) -> bool:
    """True if `path` is a commit-task-authored bookkeeping path.

    `plan_basename` (e.g. "TASK-005_phase_gates.md") lets us match the
    per-plan schedule sidecar `docs/plans/<basename>.schedule.json` without
    hardcoding every plan.
    """
    if path in COMMIT_ALWAYS_IGNORE:
        return True
    if plan_basename and path == f"docs/plans/{Path(plan_basename).stem}.schedule.json":
        return True
    return False
```

Import in `plan_ops.py` and use from `_gate_commit_safe`.

### Step 2 — `_gate_execution_safe` window-scoped assertion

Use the existing `_find_function_body` helper (or equivalent) to extract `cmd_implement`'s body and `_handle_timeout_cleanup`'s body (or the `except` block that calls it). Run the `_snapshot_baseline(` substring check against each body independently after `_strip_python_comments_and_docstrings`. Gate passes only if both windows have a call.

If the timeout-cleanup helper is called from `cmd_implement` via `_handle_timeout_cleanup(..., baseline=...)`, treat the `baseline=` kwarg as evidence that the cleanup path propagates the snapshot — so the cleanup window check is satisfied by the kwarg in `cmd_implement` OR by a snapshot in `_handle_timeout_cleanup`'s body.

### Step 3 — `_gate_commit_safe` always-ignore consistency

Replace the current two-path allowlist (plan + 00_INDEX.json) with a computed set:

```python
allowed = task_files | {plan_basename}
ignored = {p for p in changed_files if is_commit_always_ignore(p, plan_basename)}
out_of_scope = changed_files - allowed - ignored
```

Pass iff `out_of_scope` is empty. On fail, include the offending paths in the reason.

### Step 4 — SKILL.md Phase 0 preflight halt-on-fail reconciliation

Find the Phase 0 instruction block in `SKILL.md` and delete the interim demotion sentence that reads "If the plan under execution is the sample fixture itself (`sample_phase4.md`, should not occur in production runs), demote `fixture-valid: fail` to a warning to avoid a self-reference halt; for any other plan, halt as usual." The canonical instruction becomes:

> Run `plan_ops.py gates --check schema-valid,schedule-valid,fixture-valid` before dispatching the analyst. Halt on any `status: fail`, emitting the gate's `reason` verbatim and logging `run_end reason=preflight_gates_failed`.

No CLI-surface change: do NOT add a `--warn-only` flag. No status-vocabulary change: the emitted gate result vocabulary remains the frozen `pass|fail|not_applicable`. No fixture-version detection inside the predicate.

If any other part of the Phase 0 block references "pre-TASK-006", "deferred-pending-task-006", "demote to warning", or "self-reference halt", delete or rephrase those references in the same edit so the block speaks only the post-TASK-006 strict-halt contract.

### Step 5 — SKILL.md promotion-table cell fix

In the promotion-criteria table near line 27, change `schedule-valid`'s phase cell from `Phase 0 preflight` to `Phase 1 (post-write-schedule)`.

### Step 6 — Design doc §9.7 updates

`DUAL_AGENT_PLAN_EXECUTOR.md` §9.7:
- Name `COMMIT_ALWAYS_IGNORE` explicitly in the commit-safe paragraph. Reference the `_plan_paths.py` location.
- Name the two-seam invariant explicitly in the execution-safe paragraph: "the predicate asserts a `_snapshot_baseline()` call at the implement dispatch seam AND at the timeout cleanup path, parsed as separate windows."
- Reaffirm the Phase 0 preflight halt set: "Phase 0 preflight runs `schema-valid`, `schedule-valid`, and `fixture-valid` as strict halt-on-fail; there is no warning tier and no `--warn-only` flag." If the §9.7 text carries leftover `pre-TASK-006` / `deferred` wording for the preflight, delete it in the same pass.

### Step 7 — Test suite updates

`tests/scripts/test_plan_ops.py`:

- New class `TestGateExecutionSafeWindowScoped`:
  - `test_fails_when_implement_has_snapshot_but_cleanup_does_not`
  - `test_fails_when_cleanup_has_snapshot_but_implement_does_not`
  - `test_passes_when_both_seams_have_snapshot`
  - `test_passes_when_cleanup_kwarg_propagates_baseline`
- New class `TestGateCommitSafeAlwaysIgnoreConsistency`:
  - `test_run_log_change_is_ignored`
  - `test_schedule_sidecar_for_current_plan_is_ignored`
  - `test_undeclared_protected_path_still_fails`
  - `test_undeclared_non_protected_path_fails`
- New class `TestSkillPreflightStrictHalt`:
  - `test_skill_md_phase_0_halt_set_includes_schema_schedule_and_fixture_valid`
  - `test_skill_md_phase_0_has_no_interim_demotion_wording`
  - `test_gates_cli_has_no_warn_only_flag`
- Convert existing V3/V4 positive tests to fixture-based:
  - `test_gate_execution_safe_predicate`: replace `plan_codex_dispatch.py` path with a `tests/fixtures/execution_safe_good_wrapper.py` that carries the two-seam invariant.
  - `test_gate_review_safe_predicate`: likewise for review-safe.

### Step 8 — (removed) `cmd_gates` `--warn-only` flag

Originally this step proposed adding a `--warn-only` flag and a `warn` emitted status. That is explicitly NOT done: the canonical gate status vocabulary remains the frozen `pass|fail|not_applicable`, and the Phase 0 halt set is strict for all three preflight gates (see Step 4). No CLI-surface change lands from this task. This entry is retained as a placeholder so downstream step numbering matches prior review discussion.

---

## Rollout

Single commit that lands all five findings together. No feature-flag needed — the changes are additive (new constant, tighter predicates, doc updates). If any single finding is controversial, split it out as a TASK-026A/B during planning; otherwise keep as one chunk.

---

## Run notes (for future reference)

- Originating run: `20260420T220109`
- TASK-005 commit: `<filled in by commit-task>`
- Pass-4 findings in full: see `docs/plans/_run_log.jsonl` line containing `"pass": "post-narrow-remediation-2"` for the five-finding array. The wrapper review response (which the run log only meta-logged) is in the session transcript.
- User decision rationale (option A): committing TASK-005 as technically complete avoids collapsing the diminishing-returns review cycle into this run; the five findings are load-bearing enough to warrant their own chunk with proper acceptance criteria rather than a fifth narrow-remediation round.

### Historical: first execution attempt blocked by Finding 3 (2026-04-20, pre-TASK-006)

On 2026-04-20 the orchestrator attempted to run TASK-026 and halted at Phase 0 preflight because `sample_phase4.md` (then in its pre-rewrite shape) failed `fixture-valid`:

```
fixture-valid: fail
  schema-valid failed: schema violations: missing Goal section (## Goal);
  missing Verification section (## Verification); TASK-001 missing bullet
  **Priority:**; TASK-001 missing prose header **Description:**; ...
```

This chicken-and-egg block is why TASK-006 was sequenced ahead of TASK-026. TASK-006 has since landed (commit `a65350e`), rewriting `sample_phase4.md` into a canonical conformance fixture; `fixture-valid` now returns `pass` against the live sample. The interim bootstrap carve-out that demoted `fixture-valid` specifically for TASK-006's own run was also reverted (commit `bb83ac5`). TASK-026's Step 4 therefore targets the durable post-TASK-006 semantics (strict halt-on-fail for all three preflight gates) rather than any transitional warning tier.

No lock was acquired and no `run_start` event was logged on the blocked attempt, so there is no cleanup debt carried forward.

## Execution log — 20260421T015310 (success)

Starting SHA: `62e90057df13c29603e8ced3ea5af6138d139cf7`  → Ending SHA: `97a6e46455b0b46f1af950daea964cdea4492977`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-026 | claude | codex→claude (D.5) | ship-with-fixes [disagreement] | 97a6e46 | 4 Codex findings; D.5 ruled 3 spec-deference (Playbook/acceptance-criterion support) + 1 minor coverage gap |
