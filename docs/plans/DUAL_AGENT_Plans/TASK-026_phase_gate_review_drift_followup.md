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

Close the five Codex review findings surfaced in TASK-005 pass-4 that fall on the boundary between "genuine gap" and "reviewer drift vs plan acceptance criteria." Two are load-bearing invariant gaps (`execution-safe` seam fidelity, `commit-safe` always-ignore consistency). One is a doc/skill mismatch that can halt downstream runs (Phase 0 preflight hard-halt vs pre-TASK-006 `fixture-valid` deferred status). Two are small polish fixes (promotion-table phase cell typo, V3/V4 test strategy).

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
2. **Doc/skill/impl divergence.** SKILL.md's promotion table lists `schedule-valid` under Phase 0 preflight, but the workflow correctly writes the schedule in Phase 1. The Phase 0 hard-halt instruction also predates TASK-005's deferral of `fixture-valid` live-green to TASK-006 — normal plans can legitimately have `fixture-valid: fail` right now and should not block.
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

#### Finding 3 — Phase 0 preflight hard-halt conflicts with pre-TASK-006 `fixture-valid` deferral (important)

SKILL.md §Phase-0 preflight: "Run `plan_ops.py gates --check schema-valid,schedule-valid,fixture-valid` before dispatching the analyst. Halt on fail." But TASK-005 acceptance also says: `fixture-valid` may return `fail` until TASK-006 lands against the current `sample_phase4.md`.

This is a live footgun: any run against any plan, today, hits `fixture-valid: fail` at Phase 0 preflight and halts. The skill must not halt on this specific pre-TASK-006 condition.

**Fix scope:** Two options, pick one:

- **(a) — Narrower preflight.** Phase 0 runs only `schema-valid` + `schedule-valid` (both gated on the user's plan, not the fixture). `fixture-valid` is demoted to a tracked warning, not a halt, until TASK-006 lands. The `--certify --mode dry-run` path still requires `fixture-valid: pass` (post-TASK-006), but preflight halts on schema-valid/schedule-valid only.
- **(b) — Deferred-status allowance.** Phase 0 invokes `fixture-valid` but the gate itself returns `not_applicable` with reason `"deferred-pending-task-006"` when it can detect that the current fixture is the pre-rewrite version. Post-TASK-006, the detection flips and the gate becomes enforceable.

Prefer **(a)** — it's the smaller, safer change and matches the plan's stated deferral. **(b)** leaks fixture-version detection into the gate predicate and is harder to reason about.

#### Finding 4 — Promotion table phase cell for `schedule-valid` (minor)

SKILL.md promotion table line 27 says `schedule-valid` runs in Phase 0 preflight. The detailed workflow correctly says `write-schedule` runs in Phase 1, and the gate runs immediately after persistence. Fix the table cell; no code change.

**Fix scope:** One-line edit to the promotion table.

#### Finding 5 — V3/V4 tests assert against live wrapper (minor)

TASK-005 acceptance criterion explicitly says: "V3 / V4 are implemented as predicates and pass against a known-good `plan_codex_dispatch.py` (asserted via unit tests with a fixture file); the predicates become green in the live tree once TASK-003 is applied." The pass-4 review correctly noted that `test_gate_execution_safe_predicate` and `test_gate_review_safe_predicate` still assert green against the live wrapper, which works today only because pass-3 confirmed the wrapper already satisfies the invariants — but this coupling will break if TASK-003 is rolled back or if the wrapper evolves.

**Fix scope:** Convert the live-wrapper positive tests to fixture-based: construct a minimal known-good `plan_codex_dispatch.py` fixture that hits the predicate invariants, run the gate against that fixture, assert green. Keep the live-wrapper negative tests (assertions that a stripped-down wrapper fails the gate) as-is — those already use fixture files.

### Files this task edits

- `plugins/plan-executor/scripts/plan_ops.py` — `_gate_execution_safe` window-scoped assertions (finding 1); `_gate_commit_safe` uses shared `COMMIT_ALWAYS_IGNORE` (finding 2).
- `plugins/plan-executor/scripts/_plan_paths.py` — new `COMMIT_ALWAYS_IGNORE` constant (finding 2).
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — none required *unless* finding 2 reveals a commit-task self-authored path currently blocked that should be in the shared set; if so, update `commit-task`'s allowlist to reuse the constant.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — Phase 0 preflight narrowed to schema-valid + schedule-valid (finding 3); promotion-table cell fix (finding 4); promotion-criteria section notes the `COMMIT_ALWAYS_IGNORE` set.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` — §9.7 commit-safe text names `COMMIT_ALWAYS_IGNORE` (finding 2); §9.7 execution-safe names the two-seam invariant (finding 1); Phase 0 preflight scope narrowed to pre-TASK-006 reality (finding 3).
- `tests/scripts/test_plan_ops.py` — add window-scoped negative tests for `execution-safe` (finding 1); add positive-fixture + negative-real-protected-path tests for `commit-safe` (finding 2); convert V3/V4 positive tests to fixture-based (finding 5).

### Files this task does NOT edit

- `docs/plans/sample_phase4.md` — TASK-006's scope.
- `plugins/plan-executor/agents/plan-analyst.md` — not in the gate path.
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — only if finding 2 forces a shared-set adoption; most likely unchanged.

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

**V3 — Phase 0 preflight no longer hard-halts on `fixture-valid` pre-TASK-006.**

With the live `sample_phase4.md` still being the pre-rewrite version, run an executor against any TASK-NNN plan. The Phase 0 preflight output shows `schema-valid: pass`, `schedule-valid: pass`, `fixture-valid: warn` (or absent from the hard-halt set) and proceeds to Phase 1 without halting. Verified by inspecting SKILL.md's Phase 0 instruction text and a smoke test that reads the skill text and fails if it lists `fixture-valid` under the halt set.

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestSkillPreflightPreTask006
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

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/_plan_paths.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `tests/scripts/test_plan_ops.py`
- **Read-only context (not edited by TASK-026):**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` — read to confirm `commit-task`'s self-authored-path set; only edited if finding 2's shared-constant adoption requires it. Schedule file_locks should NOT include this unless a concrete edit is planned.
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "gates or commit_safe or execution_safe or review_safe or preflight"`
- **Acceptance criteria:**
  - Bootstrap carve-out cleanup (prerequisite, Step 0): the TASK-006-specific `fixture-valid` demotion span added to `SKILL.md` Phase 0 preflight as a bootstrap exception for TASK-006's run is reverted. Verified by `grep -n 'TASK-006 bootstrap carve-out' plugins/plan-executor/skills/implement-plan/SKILL.md` returning zero matches. Must land before (or in the same commit as) Finding 3's broader preflight narrowing.
  - Finding 1 (execution-safe two-seam): `_gate_execution_safe` asserts a `_snapshot_baseline()` call inside the implement-dispatch window AND inside the timeout-cleanup window, parsed as separate regions. Negative tests cover each-seam-missing case.
  - Finding 2 (commit-safe always-ignore): `COMMIT_ALWAYS_IGNORE` constant defined in `_plan_paths.py`; `_gate_commit_safe` uses it as the sole ignore set (no protected-path fallback); if `commit-task`'s guard is updated, it uses the same constant. Tests assert the set's members are ignored while protected-but-not-declared paths fail the gate.
  - Finding 3 (Phase 0 preflight): SKILL.md Phase 0 preflight instruction lists only `schema-valid` + `schedule-valid` as halt-on-fail gates. `fixture-valid` is either moved to a warning-tier block or annotated as "tracked; not halt-on-fail until TASK-006 lands." Smoke test asserts this.
  - Finding 4 (promotion-table cell): SKILL.md promotion-criteria table row for `schedule-valid` cites Phase 1 (post-write), not Phase 0.
  - Finding 5 (V3/V4 test strategy): `test_gate_execution_safe_predicate` and `test_gate_review_safe_predicate` positive paths use fixture-file wrappers, not the live `plan_codex_dispatch.py`. Negative paths already use fixtures; unchanged.
  - All verification checks V1–V7 pass.
- **Deferred to downstream chunks (tracked, not required for TASK-026 merge):**
  - Full `fixture-valid: pass` end-to-end certification is still TASK-006's scope.
  - Live-tree green for V3/V4 against the production `plan_codex_dispatch.py` is TASK-003's scope.

**Description:**
This task closes five review findings surfaced after TASK-005 was committed. Two are invariant-fidelity gaps in existing gate predicates; two are doc/skill alignment fixes; one is a test-strategy alignment. None of these change the gate vocabulary or the promotion model; they tighten predicates and reconcile text.

**Implementation notes:**
Keep the changes minimal — each finding is addressed in the one file it affects plus its regression test. Avoid re-opening TASK-005's acceptance surface; the gate CLI shape, status vocabulary, and certification mode bundles are frozen.

For finding 2, audit `commit-task`'s guard to confirm the current self-authored-path set matches the proposed `COMMIT_ALWAYS_IGNORE` before writing the constant. If they diverge, the constant must be the union (or `commit-task`'s set explicitly scoped) so both ends agree. Document the decision in §9.7.

For finding 3, the safer path is option (a) — narrow the halt set. Option (b) requires fixture-version detection inside the gate predicate, which adds surface area.

**Reversion guidance:**
If finding 3's preflight narrowing turns out to mask real `fixture-valid` regressions post-TASK-006, re-add `fixture-valid` to the halt set in the same edit where TASK-006 lands (as part of its acceptance criteria, not a separate revert commit).

If the shared `COMMIT_ALWAYS_IGNORE` set introduces friction (e.g. a legitimate bookkeeping path needs adding later), extend the set in a small follow-up rather than re-introducing the broader `is_protected_path` fallback.

---

## Implementation Playbook

### Step 0 — Revert TASK-006 bootstrap carve-out in SKILL.md

TASK-006 landed under a one-shot `SKILL.md` bootstrap exception that demoted `fixture-valid` to warning specifically for `TASK-006_conformance_fixture.md`. With TASK-006 merged, `sample_phase4.md` conforms to the schema and `fixture-valid` returns `pass` globally — the carve-out is inert and must be removed before Step 4 rewrites the surrounding Phase 0 preflight text (otherwise the two edits collide).

Find the sentinel-bracketed span in `plugins/plan-executor/skills/implement-plan/SKILL.md` under the Phase 0 preflight block:

```
<!-- BEGIN TASK-006 bootstrap carve-out — revert in TASK-026 Step 0 -->
... TASK-006-specific demotion clause ...
<!-- END TASK-006 bootstrap carve-out -->
```

Delete the span inclusive of both sentinel HTML comments. The surrounding sentence returns to its pre-carve-out shape (`...demote to warning; for any other plan, halt as usual.`). Verify:

```bash
grep -n 'TASK-006 bootstrap carve-out' plugins/plan-executor/skills/implement-plan/SKILL.md
```

Zero matches after the edit. Proceed to Step 1 only after verification.

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

### Step 4 — SKILL.md Phase 0 preflight narrowing

Find the Phase 0 instruction block in `SKILL.md`. Change:

> Run `plan_ops.py gates --check schema-valid,schedule-valid,fixture-valid` before dispatching the analyst. Halt on fail.

to:

> Run `plan_ops.py gates --check schema-valid,schedule-valid` before dispatching the analyst. Halt on fail.
> Run `plan_ops.py gates --check fixture-valid --warn-only`; surface the result but do not halt until TASK-006 lands. (Post-TASK-006, this moves back into the halt set.)

(The `--warn-only` flag is new; add it to `cmd_gates` as a pass-through that flips fail → warn in the JSON output and exit 0. Keep the predicate unchanged.)

### Step 5 — SKILL.md promotion-table cell fix

In the promotion-criteria table near line 27, change `schedule-valid`'s phase cell from `Phase 0 preflight` to `Phase 1 (post-write-schedule)`.

### Step 6 — Design doc §9.7 updates

`DUAL_AGENT_PLAN_EXECUTOR.md` §9.7:
- Name `COMMIT_ALWAYS_IGNORE` explicitly in the commit-safe paragraph. Reference the `_plan_paths.py` location.
- Name the two-seam invariant explicitly in the execution-safe paragraph: "the predicate asserts a `_snapshot_baseline()` call at the implement dispatch seam AND at the timeout cleanup path, parsed as separate windows."
- Note the Phase 0 preflight narrowing: "Pre-TASK-006, `fixture-valid` is surfaced as a warning, not a halt."

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
- New class `TestSkillPreflightPreTask006`:
  - `test_skill_md_phase_0_halt_set_excludes_fixture_valid`
  - `test_skill_md_fixture_valid_appears_as_warn_block`
- Convert existing V3/V4 positive tests to fixture-based:
  - `test_gate_execution_safe_predicate`: replace `plan_codex_dispatch.py` path with a `tests/fixtures/execution_safe_good_wrapper.py` that carries the two-seam invariant.
  - `test_gate_review_safe_predicate`: likewise for review-safe.

### Step 8 — `cmd_gates` `--warn-only` flag

Add `--warn-only` to `gates --check`. When present, any `fail` status is emitted in the JSON as `{"status": "warn", "reason": "...", "severity_override": "warn"}` and the process exits 0. Does not apply to `--certify` (certification must remain strict).

---

## Rollout

Single commit that lands all five findings together. No feature-flag needed — the changes are additive (new constant, tighter predicates, doc updates). If any single finding is controversial, split it out as a TASK-026A/B during planning; otherwise keep as one chunk.

---

## Run notes (for future reference)

- Originating run: `20260420T220109`
- TASK-005 commit: `<filled in by commit-task>`
- Pass-4 findings in full: see `docs/plans/_run_log.jsonl` line containing `"pass": "post-narrow-remediation-2"` for the five-finding array. The wrapper review response (which the run log only meta-logged) is in the session transcript.
- User decision rationale (option A): committing TASK-005 as technically complete avoids collapsing the diminishing-returns review cycle into this run; the five findings are load-bearing enough to warrant their own chunk with proper acceptance criteria rather than a fifth narrow-remediation round.

### First execution attempt blocked by Finding 3 (2026-04-20)

On 2026-04-20 the orchestrator attempted to run TASK-026 and halted at Phase 0 preflight with exactly the footgun Finding 3 describes:

```
fixture-valid: fail
  schema-valid failed: schema violations: missing Goal section (## Goal);
  missing Verification section (## Verification); TASK-001 missing bullet
  **Priority:**; TASK-001 missing prose header **Description:**; ...
```

The failure is in `docs/plans/sample_phase4.md` (pre-TASK-006 shape), not in TASK-026's plan file. Per current SKILL.md Phase 0 semantics — "`fixture-valid` may legitimately fail against the pre-TASK-006 sample fixture — if the plan under execution is the sample itself, demote to warning; for any other plan, halt as usual" — the run halts because TASK-026 ≠ the sample.

Chicken-and-egg: the plan that would fix the halt cannot run because of the halt. User's chosen path: land TASK-006 first (rewrites `sample_phase4.md` so `fixture-valid` can pass), then retry TASK-026. TASK-006 now cites this attempt as the concrete motivating example.

No lock was acquired and no `run_start` event was logged on the blocked attempt, so there is no cleanup debt carried forward.
