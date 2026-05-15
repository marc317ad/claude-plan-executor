# TASK-003 — Wrapper State Isolation (Implement / Review / Timeout)

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-003
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §7.5 recovery, Appendix D F1-F4
**Postmortem:** [`../../analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md`](../../analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md) §3 Defect C, Gap H
**Base branch:** `phase-7b5-bug-fixes`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contracts — `_run_log.jsonl`, `_run_lock.json`, `*.schedule.json` paths are only canonical once TASK-001 locks them in the design).
**Issues absorbed:** ISSUE-003, 004, 005, 015, 017, 021

---

## Goal

Make `scripts/plan_codex_dispatch.py` safe under parallel and interleaved execution. Three separate code paths (implement post-check, review post-check, implement timeout cleanup) must all operate on deltas scoped to the task's own `allowed_files` plus a pre-dispatch baseline — never on repo-wide state. Orchestrator infrastructure files (`_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`) are explicitly protected at all three seams. Dishonesty checks (ISSUE-015) are enforced, not just observed. Regression coverage proves two parallel implementers do not destroy each other's output.

## Scoped Context

This is the state-safety cluster. It is the load-bearing defect from Phase 5: Scenario 7 destroyed orchestrator state files because both Codex dispatches, running in parallel, called post-invocation `validate_scope` and classified each other's untracked outputs and the run-log/lock/schedule sidecar as violations.

### ISSUE-003 (P0) — implement path `validate_scope` has no baseline

- **Location:** `scripts/plan_codex_dispatch.py:414-445` (function `validate_scope`), called from `scripts/plan_codex_dispatch.py:653` in `cmd_implement`.
- **Current behavior:** post-invocation `git_changed_files(repo_root)` returns every untracked file in the repo; everything outside `allowed_files` is classified as a violation and passed to `restore_out_of_scope` which deletes untracked entries and restores tracked ones.
- **Defect:** in parallel batches, each implementer's `validate_scope` sees the other's legitimate output as "untracked outside my scope" and destroys it. Orchestrator state files (`docs/plans/_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`) are also destroyed.
- **Observed:** Phase 5 Scenario 7 — both Codex workers completed successfully; wrapper converted both to `scope_violation` and destroyed orchestrator state.
- **Fix:** snapshot `baseline_untracked` and `baseline_tracked` via `git_changed_files` **before** `invoke_codex` runs. `validate_scope(..., baseline=baseline)` classifies violations as `(post_untracked - baseline_untracked) - allowed_set`. Also add a permanent always-ignore list for orchestrator state paths.

### ISSUE-004 (P0) — review path `validate_scope` has no pre-dispatch baseline

- **Location:** `scripts/plan_codex_dispatch.py:875-896`, in `cmd_review`.
- **Current behavior:** post-Codex-invocation, the wrapper reads `changed = git_changed_files(repo_root)` and treats the result as a "baseline" even though it was sampled **after** Codex ran. The variable is named `baseline_tracked` but semantically is post-invocation state.
- **Defect:** same structural bug as ISSUE-003. Masked because reviews run serially, but orchestrator state files (created after the review path starts) would still be destroyed. If sibling implementation writes are still in flight, the review's cleanup deletes them.
- **Fix:** take the snapshot before `invoke_codex`. Apply the same always-ignore list for orchestrator state.

### ISSUE-005 (P0) — timeout branch is repo-wide destructive

- **Location:** `scripts/plan_codex_dispatch.py:609-611`:
  ```
  if codex["status"] == "timeout":
      _git(["checkout", "--", "."], cwd=repo_root)
      _git(["clean", "-fd"], cwd=repo_root)
  ```
- **Defect:** `git clean -fd` at repo scope deletes all untracked files including orchestrator state, sibling Codex workers' in-flight outputs, and any unrelated scratch the user has in the repo. Design §7.5 specifies "outcome=timeout, fallback to Claude" — not repo-wide destruction.
- **Fix:** replace with the same baseline-scoped cleanup as ISSUE-003. Only restore files in `allowed_files` (tracked) and delete new untracked files that are (a) created during the Codex run (delta, not baseline) and (b) not in the orchestrator always-ignore list.

### ISSUE-015 (P1) — dishonesty check computed but not enforced

- **Location:** `scripts/plan_codex_dispatch.py:707-713`:
  ```
  reported = {normalize_file_path(f) for f in parsed.get("files_changed", [])}
  actual = set(scope["changed_in_scope"])
  undeclared = sorted(actual - reported)
  phantom = sorted(reported - actual)
  ```
- **Defect:** both `undeclared` and `phantom` flow only into the `extra` dict in the envelope emitted at lines 734 and 751. Design §7.5 says "if mismatch, outcome=failure" — the wrapper currently only observes the mismatch.
- **Fix:** if `undeclared` or `phantom` is non-empty, emit `outcome=failure` with `error="dishonest files_changed: undeclared=[...], phantom=[...]"`. Do this **before** the test re-run block, so dishonest implementations do not get to claim green tests. Keep the diagnostic lists in `extra` for debugging.

### ISSUE-017 (P1) — no regression coverage for parallel sibling destruction

- **Location:** `tests/scripts/test_plan_codex_dispatch_integration.py` has one single-dispatch test (`test_implement_dry_run`), no parallel coverage.
- **Defect:** Phase 4 shipped without this coverage; Scenario 7 was the first time the defect showed.
- **Fix:** add a scratch-repo integration test that spawns two `plan_codex_dispatch.py implement` processes in parallel against adjacent untracked files in the same directory. Assert:
  - Both output files exist on disk post-run.
  - Neither outcome is `scope_violation`.
  - `docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `docs/plans/*.schedule.json` (if pre-created in the scratch repo) are untouched.
  - Pre-existing unrelated untracked files (e.g., `scratch_noise.txt`) are untouched.

### ISSUE-021 (P1) — scope_violation outcome co-opted by orchestrator-state destruction

Auto-resolves when ISSUE-003 is fixed. Fixing the baseline means orchestrator state is no longer classified as a violation, so `scope_violation` is emitted only when Codex genuinely writes outside `allowed_files` — which is its intended meaning.

---

## Verification

**V1 — Unit: implement pre-baseline.**

```
# tests/scripts/test_plan_codex_dispatch_integration.py
def test_implement_preserves_pre_existing_untracked(tmp_repo):
    # Create untracked file `b.txt` in tmp_repo BEFORE invoking.
    # Dispatch implement with allowed_files=["a.txt"] using dry-run
    # or a stub that simulates Codex writing a.txt.
    # Assert b.txt still exists and is not in scope.violations_untracked.
```

**V2 — Unit: review pre-baseline.**

Same as V1 but for `cmd_review`. Assert sibling untracked file outside `review_files` is not deleted.

**V3 — Unit: timeout bounded cleanup.**

```
def test_timeout_preserves_orchestrator_state_and_siblings(tmp_repo):
    # Pre-create: docs/plans/_run_log.jsonl, docs/plans/_run_lock.json,
    #             sibling_scratch.txt
    # Force a simulated timeout (mock invoke_codex to return status=timeout).
    # Assert all three pre-existing files still exist.
```

**V4 — Unit: dishonesty enforcement (strengthened).**

```
def test_implement_dishonest_files_changed_fails(tmp_repo):
    # Stub Codex to declare files_changed=["a.py"] while actually writing
    # a.py AND b.py (both in allowed_files). The dishonesty check compares
    # the reported set against (changed_in_scope_new ∪ out_of_scope_tracked
    # ∪ out_of_scope_untracked) so undeclared writes fail the task even if
    # they fall inside allowed_files.
    # Assert outcome == "failure", reason == "scope_misreport",
    # "b.py" in undeclared_changes, test_result.result == "not_run".
```

**V5 — Integration: parallel siblings + observe-only cleanup.**

Observe-only semantics mean the wrapper never mutates files outside
`allowed_files`; the orchestrator reconciles at the batch join barrier. The
parallel-sibling tier asserts preservation under concurrent dispatches with
no repo-wide lock.

```
@pytest.mark.slow
def test_parallel_preserves_run_log_jsonl(tmp_repo):
    # Pre-populate docs/plans/_run_log.jsonl, spawn two real-Codex sibling
    # dispatches on disjoint allowed_files, assert both envelopes succeed
    # and the protected log is byte-identical after both complete.

@pytest.mark.slow
def test_parallel_preserves_schedule_json_tracked(tmp_repo):
    # Same pattern with a tracked docs/plans/*.schedule.json sidecar.

@pytest.mark.slow
def test_timeout_and_success_interleaved_preserves_sibling(tmp_repo):
    # One sibling times out (1s timeout), the other succeeds; the
    # succeeding sibling's declared file must remain on disk.

def test_parallel_implement_dispatches_actually_overlap(tmp_repo):
    # Unit-tier regression for Fix C (lock removal). A threading.Barrier
    # with a 2s timeout is injected into invoke_codex — if the repo-wide
    # lock were still present, one sibling would time out at the barrier.
```

**V6 — Always-ignore list in code.**

```
grep -n 'ALWAYS_IGNORE' scripts/plan_codex_dispatch.py
```

Must match a declared constant that includes:
- `docs/plans/_run_log.jsonl`
- `docs/plans/_run_lock.json`
- `docs/plans/*.schedule.json` (or equivalent glob handling)

**V7 — Timeout no longer uses `git clean -fd`.**

```
grep -n 'git.*clean.*-fd' scripts/plan_codex_dispatch.py
```

Must return no matches outside comments. The only clean-path is the new bounded one.

**V8 — Scenario 7 rerun.** Once TASK-001, 002, 003 all land, re-run Phase 5 Scenario 7 per the postmortem script. Both parallel dispatches must return `outcome=success`; orchestrator state must persist.

---

## Tasks

### TASK-003: Make implement, review, and timeout state isolation safe

- **Status:** done
- **Priority:** critical
- **Files:**
  - `scripts/plan_codex_dispatch.py`
  - `tests/scripts/test_plan_codex_dispatch_integration.py`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `.claude/skills/implement-plan/SKILL.md`
  - `.claude/skills/implement-plan/dispatch-templates.md`
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py`
- **Acceptance criteria:**
  - ISSUE-003, 004, 005, 015, 017, 021 are resolved.
  - Implement-path scope validation uses pre-dispatch tracked and untracked baselines.
  - Review-path cleanup also uses pre-dispatch baselines and does not delete sibling task state or executor infrastructure.
  - Timeout cleanup is bounded to task-owned deltas and never performs repo-wide destructive cleanup.
  - **Observe-only discipline**: the wrapper NEVER mutates files outside `allowed_files`. Out-of-scope writes are classified and reported under `out_of_scope_tracked` / `out_of_scope_untracked` / `out_of_scope_observed` in the envelope. The orchestrator reconciles these at the batch join barrier via `plan_ops.py reconcile-batch` in its single-writer phase; reconciliation failure must block advancement to the next batch.
  - **No repo-wide dispatch lock**: parallel dispatches on the same repo are permitted. The orchestrator enforces pairwise-disjoint `allowed_files` within each batch (`batch-file-overlap` schema error) so disjointness, not a lock, keeps siblings safe.
  - Executor infrastructure files (`docs/plans/_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`) are protected by an explicit always-ignore list (exact paths, prefixes, suffixes, and fnmatch globs) matched symmetrically across tracked and untracked deltas.
  - Dishonesty check (undeclared / phantom) fails the task instead of only decorating the envelope, and compares reported `files_changed` against the full observed delta (`changed_in_scope_new ∪ out_of_scope_tracked ∪ out_of_scope_untracked`) as defense in depth.
  - Regression test coverage exists for parallel sibling implementers and dishonest `files_changed`.
  - Verification checks V1–V7 all pass; V8 (Scenario 7 rerun) passes once TASK-001 and TASK-002 also land.

**Description:**
This is the state-safety cluster. It fixes the known wrapper isolation defects while establishing the executor's rule that cleanup must always be delta-scoped, never repo-scoped.

**Implementation notes:**
Reject the tempting fallback of repo-wide `git clean -fd`. It solves the wrong problem and breaks executor safety. The baseline-snapshot pattern is used three times in this chunk; factor it into a single helper `def snapshot_baseline(repo_root) -> dict[str, set[str]]` and call it from all three entry points.

**Reversion guidance:**
If the new cleanup logic regresses, fall back only behind an explicit compatibility flag (`--legacy-scope-cleanup`). Never restore global destructive cleanup as the default. If the always-ignore list causes issues, narrow the list carefully; do not disable it entirely.

---

## Implementation Playbook

### Step 1 — baseline helper

Add to `scripts/plan_codex_dispatch.py` near the existing `git_changed_files`:

```
ALWAYS_IGNORE = (
    "docs/plans/_run_log.jsonl",
    "docs/plans/_run_lock.json",
)
ALWAYS_IGNORE_GLOBS = ("docs/plans/*.schedule.json",)


def snapshot_baseline(repo_root: str) -> dict[str, set[str]]:
    changed = git_changed_files(repo_root)
    return {
        "tracked": set(changed["tracked"]),
        "untracked": set(changed["untracked"]),
    }


def _in_always_ignore(path: str) -> bool:
    if path in ALWAYS_IGNORE:
        return True
    import fnmatch
    return any(fnmatch.fnmatch(path, g) for g in ALWAYS_IGNORE_GLOBS)
```

### Step 2 — revise `validate_scope` signature

Replace `def validate_scope(repo_root, allowed_files)` with `def validate_scope(repo_root, allowed_files, baseline)`.

Inside:
- `post = git_changed_files(repo_root)`
- `post_untracked = set(post["untracked"])` and similarly for tracked
- `new_untracked = post_untracked - baseline["untracked"]`
- `changed_tracked = set(post["tracked"]) ^ baseline["tracked"]` — tracked files whose diff state changed vs baseline
- `violations_untracked = sorted((new_untracked - set(allowed_files)) - {p for p in new_untracked if _in_always_ignore(p)})`
- `violations_tracked = sorted((changed_tracked - set(allowed_files)) - {p for p in changed_tracked if _in_always_ignore(p)})`
- `cleanup_performed` / `restore_out_of_scope` call shape is unchanged; only the inputs shrink.

All callers must now pass `baseline`. Update both call sites.

### Step 3 — `cmd_implement` wiring (observe-only)

In `scripts/plan_codex_dispatch.py` `cmd_implement`:

```
# Take baseline BEFORE invoke_codex (and NO repo-wide lock — parallel
# dispatches are expected and enforced safe via batch disjointness).
baseline = _snapshot_baseline(repo_root)

codex = invoke_codex(...)

# Timeout path (ISSUE-005 fix): bounded to in-scope cleanup only.
if codex["status"] == "timeout":
    cleanup_details = _handle_timeout_cleanup(repo_root, allowed_files, baseline)
    # cleanup_details includes out_of_scope_tracked / out_of_scope_untracked
    # observed but not mutated — surfaced for orchestrator reconciliation.
    emit(make_envelope(..., outcome="timeout",
                       extra={"cleanup_details": cleanup_details, ...}))
    return 1

# Success path: observe-only validate_scope never mutates out-of-scope.
scope = validate_scope(repo_root, allowed_files, baseline)
if scope["out_of_scope_observed"]:
    emit(make_envelope(..., outcome="scope_violation",
                       extra={"scope": scope,
                              "out_of_scope_tracked": scope["out_of_scope_tracked"],
                              "out_of_scope_untracked": scope["out_of_scope_untracked"],
                              "out_of_scope_observed": True, ...}))
    return 1  # orchestrator reconciles at batch barrier

# Dishonesty check: reported files_changed vs full observed delta.
actual_all = (set(scope["changed_in_scope_new"])
              | set(scope["out_of_scope_tracked"])
              | set(scope["out_of_scope_untracked"]))
if undeclared or phantom: emit(failure, reason="scope_misreport"); return 1
```

`_handle_timeout_cleanup` restricts restore/delete to `allowed_files` — it
never touches out-of-scope paths, which are recorded in the envelope for
orchestrator reconciliation. The repo-wide `git checkout -- .` and
`git clean -fd` calls are removed. The `_dispatch_lock` context manager is
removed: parallel dispatches run without a repo lock; disjointness is
enforced by `_validate_schedule_dag`'s `batch-file-overlap` check on the
schedule JSON at persistence time.

After the batch join barrier the orchestrator invokes:

```
plan_ops.py reconcile-batch --repo-root <repo> < envelopes.json
```

which for each envelope with `out_of_scope_observed=True`:
- Restores out-of-scope tracked paths (`git restore --staged --`, `git restore --`).
- Unlinks out-of-scope untracked paths.
- Skips any path matching the executor-infrastructure protection set.
- Verifies no residual dirt remains (re-runs `git diff` + `ls-files --others`).

Each result is one of `scope_violation_reconciled` (safe to advance),
`reconciliation_failed` (hard halt), or `no_op`. A failing reconciliation
MUST block advancement to the next batch and surface to the operator.

### Step 4 — `cmd_review` wiring

In `cmd_review` (scripts/plan_codex_dispatch.py):

```
baseline = snapshot_baseline(repo_root)

codex = invoke_codex(...)

# After parse, apply same validate_scope discipline — BUT review path
# treats the target files as having legitimate diffs vs HEAD already.
# Only flag writes to files outside review_files AND outside baseline.
```

Replace the post-review block at lines 875-896 with a call to a thin `validate_review_scope(repo_root, review_files, baseline)` that:
- Computes `post_untracked - baseline["untracked"]`: new untracked files.
- Any such file not in `review_files` and not in always-ignore is `post_review_violations["unexpected_untracked"]`.
- Similarly for tracked changes.
- Restores violations (same pattern), omitting always-ignore paths.

### Step 5 — enforce dishonesty check (ISSUE-015)

In `cmd_implement`, between the scope-check success branch and the test re-run (around line 707):

```
reported = {normalize_file_path(f) for f in parsed.get("files_changed", [])}
actual = set(scope["changed_in_scope"])
undeclared = sorted(actual - reported)
phantom = sorted(reported - actual)

if undeclared or phantom:
    emit(make_envelope(
        task["task_id"], "implement", "failure",
        exit_code=codex["exit_code"],
        raw=output_text,
        parsed=parsed,
        error=(
            "dishonest files_changed: "
            f"undeclared={undeclared}, phantom={phantom}"
        ),
        extra={
            "scope": scope,
            "undeclared_changes": undeclared,
            "phantom_declarations": phantom,
            "jsonl_file_changes": codex["file_changes"],
            "wall_seconds": codex["wall_seconds"],
        },
    ))
    return 1

# Only then run tests
test_result = run_test_command(...)
```

### Step 6 — parallel integration test (ISSUE-017)

Add `tests/scripts/test_plan_codex_dispatch_integration.py::test_parallel_implement_preserves_sibling_state`:

- Use `tmp_path` + `git init` for a throwaway repo.
- Set up two minimal plans OR use two task blocks in a single minimal plan. Allowed files: adjacent paths in the same directory (e.g., `scratch/a.txt`, `scratch/b.txt`).
- Pre-create orchestrator state files at `docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `docs/plans/test.schedule.json` inside the scratch repo.
- Spawn two `subprocess.Popen` invocations of `plan_codex_dispatch.py implement --dry-run` in parallel (dry-run avoids the real Codex binary dependency; the scope-check logic still exercises because the dry-run branch in the wrapper can be extended to simulate untracked writes for this test).
  - Or: stub `invoke_codex` to write the allowed file directly, bypassing the real Codex.
- Join both; parse envelopes.
- Assert:
  - `envelope_a["outcome"] == "success"`
  - `envelope_b["outcome"] == "success"`
  - `(tmp / "scratch/a.txt").exists()` and `b.txt` exists
  - Orchestrator state files still exist with original content (capture content hashes before and after).

Mark `@pytest.mark.slow`; CI can skip by default.

### Step 7 — dishonesty unit test

Add `test_implement_dishonest_files_changed_fails`:

- Stub `invoke_codex` to emit JSON declaring `files_changed=["a.txt"]` while the test harness pre-writes `b.txt` inside the scratch repo as if Codex wrote it.
- Assert envelope `outcome == "failure"` and `error` contains `"dishonest"`.

### Step 8 — baseline unit tests

Add `test_implement_preserves_pre_existing_untracked`, `test_review_preserves_pre_existing_untracked`, `test_timeout_preserves_orchestrator_state_and_siblings`.

Each follows the pattern: initialize scratch repo, pre-create the "victim" file, dispatch the wrapper subcommand with a scope that explicitly does not include the victim, assert the victim still exists after the wrapper returns.

### Step 9 — design doc update

Update `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`:

- §7.5 recovery rules: add "timeout cleanup is bounded to the task's allowed_files plus new-untracked delta; never repo-wide".
- Appendix D F1 (timeout): replace the "clean partial writes" language with the bounded cleanup semantics.
- Appendix D F2 (sandbox unreliable): note that `validate_scope` now requires a pre-dispatch baseline.
- Add an "Always-ignored state paths" list naming `_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`.

### Step 10 — SKILL.md / dispatch-templates spot check

Grep both for references to "timeout", "cleanup", "clean -fd"; update any prose that describes the old destructive behavior.

---

## Out of Scope

- **Canonical contract**: TASK-001.
- **Runtime validation** at non-wrapper seams: TASK-002.
- **Scheduler semantics** (`batch-next`, `fail-task`, `block-dependents`): TASK-004.
- **Phase gates / self-audit**: TASK-005, TASK-007.
- **Fixture rewrite**: TASK-006.
- **Portability**: TASK-008.
- **Large-file reads / global locks / bounded logs**: TASK-009, 010, 011.

## Reversion guidance

- **Baseline-scoped `validate_scope`:** if it regresses under obscure sandbox behaviors, gate behind `--legacy-scope-cleanup` rather than reverting. Do not restore the pre-baseline default.
- **Bounded timeout cleanup:** if it misses real cleanup needs, widen the delta — never fall back to `git clean -fd` at repo scope.
- **Dishonesty enforcement:** if it produces false positives from path normalization bugs, add targeted normalization (e.g., case handling, trailing-slash handling) rather than disabling the check.
- **Always-ignore list:** if a legitimate task needs to write to one of the listed paths, add a per-task opt-in flag (`--allow-orchestrator-state-write`) rather than deleting the list entry.
- **Parallel integration test:** if it is flaky (e.g., file-system race), mark `@pytest.mark.slow` and gate to nightly. Do not delete.
