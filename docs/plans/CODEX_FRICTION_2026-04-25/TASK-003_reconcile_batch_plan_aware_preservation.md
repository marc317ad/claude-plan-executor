# TASK-003 — `reconcile-batch` plan-aware preservation (Issue 5)

## Goal

Defense-in-depth: stop `reconcile-batch` from wiping a correct implementer edit when the wrapper's `out_of_scope_tracked` is wrong. Today the orchestrator-side reconcile path trusts the wrapper's classification verbatim and runs `git restore` against every listed tracked file. After this task, the orchestrator consults the persisted schedule's per-task `Files:` list and PRESERVES any `out_of_scope_tracked` entry that is in fact declared by the dispatched task. The output envelope grows a new `reconcile_kept_tracked` field that disambiguates "preserved" from "restored" in the run-log audit trail.

## Scoped Context

**Where the bug bites today (`plan_ops.py:3260-3377`).**

```
for env in batch_envelopes:
    raw_tracked = _envelope_field(env, "out_of_scope_tracked") or []
    actionable_tracked = [p for p in raw_tracked if not is_protected_path(p)]
    ...
    _git(["restore", "--staged", "--"] + actionable_tracked, cwd=cwd)
    _git(["restore", "--"] + actionable_tracked, cwd=cwd)
```

If the wrapper falsely flags `Makefile` as `out_of_scope_tracked` (because of Issue 4's parser bug), the orchestrator hard-restores the wrapper's correct edit. The single `reconciled_tracked` output field labels both "restored to baseline" and "actually-out-of-scope, restored" identically — the run log cannot tell them apart after the fact.

**Why TASK-002 alone isn't enough.** TASK-002 fixes the wrapper's parser, so on dev-tree HEAD the wrong-classification trigger is gone. But the plugin distribution model (consumer repos pull from `~/.claude/plugins/cache/...`) means a stale wrapper can still send broken envelopes to a fresh orchestrator. The orchestrator should not blindly trust a wrapper version it cannot pin. Adding plan-aware preservation here is the second layer that closes the door even against an outdated wrapper.

**Schedule as the single source of truth.** The persisted schedule JSON (`<plan_dir>/<plan>.schedule.json`, or the directory-mode `<plan_dir>/<basename>.schedule.json`) carries the fat manifest from TASK-004's `build-tasks` work, including `tasks[i].files` for every task. `reconcile-batch` already operates between Phase B and Phase D (per `SKILL.md:624`), so the schedule is on disk and stable. The orchestrator wires `--schedule-file <abs>` into each `reconcile-batch` invocation; the function looks up `tasks[task_id].files`, normalises each entry through `_plan_paths.normalize_files_entry` (the unified helper from TASK-002), and uses the resulting set as the per-task in-scope allow-list.

**Preservation rule.** For each envelope:
1. Look up the dispatched task's `tasks[task_id].files` (normalised) from the schedule.
2. Partition `out_of_scope_tracked` into:
   - `kept` — paths that are in the dispatched task's normalised `Files:` set. These are wrapper-side false positives; do not restore.
   - `restored` — paths NOT in the task's `Files:` set. These are genuinely out-of-scope; restore as today.
3. Apply the same partition to `out_of_scope_untracked` (a `(create)` file declared in the task should be kept; a stray `.tmp` should still be deleted).

**Envelope output additions.**
- `reconcile_kept_tracked: list[str]` — paths preserved by the new rule.
- `reconcile_kept_untracked: list[str]` — analogous for untracked.
- `reconciled_tracked` / `reconciled_untracked` keep their existing meaning (paths that were actually restored / unlinked).

**Outcome label.**
- `outcome: "scope_violation_reconciled"` when the partition produced any restoration.
- `outcome: "scope_violation_preserved"` when the partition resulted in only kept paths and nothing was restored — the wrapper's classification was wrong but no harm was done. (New label so the orchestrator can route differently if it wants to; for v1 it routes the same as `scope_violation_reconciled`.)
- `outcome: "no_op"` unchanged.
- `outcome: "reconciliation_failed"` unchanged (errors during restoration).

**Schedule lookup details.**
- `tasks[task_id].files` is the canonical list. For tasks using the directory-mode child-plan layout, the schedule's per-task `files` list is already the unified, normalised view.
- If the schedule is missing or the task id is absent from it (defensive case), fall back to today's behaviour — no plan-aware filter — and log a warning in the result entry: `warning: "schedule_lookup_failed"`. This keeps the function safe-by-default for direct CLI callers who do not pass a schedule.
- The `--schedule-file` CLI flag is OPTIONAL (with the documented fallback); the orchestrator wires it in for normal `/implement-plan` runs but tests and ad-hoc CLI usage do not have to.

**SKILL.md update.** §Phase B's batch-join-barrier paragraph (line 624) currently invokes `reconcile-batch --repo-root <repo>`; add `--schedule-file <path>` to that invocation. The schedule path is already known to the orchestrator (it was the input to Phase 1.5), so wiring it through is a one-liner.

**Out of scope.** Restoring files that the wrapper missed entirely (i.e. that are NOT in `out_of_scope_tracked` because the wrapper's `validate_scope` did not detect them). The analysis recommends an additional wrapper-side guard for that case in Issue 2's side effect; that is a separate concern — TASK-004 owns it as part of timeout/spillover hardening if it lands there at all. This task is bounded to the reconcile-side preservation rule.

## Verification

- `plan_ops.reconcile_batch(batch_envelopes, repo_root, *, schedule_file=None)` accepts an optional schedule path. When supplied AND parseable, the function loads `tasks[].files` per task and partitions each envelope's `out_of_scope_tracked` / `out_of_scope_untracked` into `kept` and `restored` buckets per the rule above.
- `cmd_reconcile_batch` accepts `--schedule-file <path>` and forwards it. Absent flag → today's behaviour.
- Each result dict gains:
  - `reconcile_kept_tracked: list[str]`
  - `reconcile_kept_untracked: list[str]`
  - `outcome` may be `"scope_violation_preserved"` when the partition kept everything and restored nothing.
- The on-disk file is NOT mutated for kept paths (no `git restore` issued).
- Stale wrapper case: an envelope with `out_of_scope_tracked: ["Makefile"]` when the schedule's `tasks["003"].files == ["Makefile"]` results in `outcome: "scope_violation_preserved"`, `reconcile_kept_tracked: ["Makefile"]`, `reconciled_tracked: []`, and `git diff HEAD -- Makefile` still non-empty post-call.
- Genuine out-of-scope case: an envelope with `out_of_scope_tracked: ["other.py"]` when the schedule's `tasks["003"].files == ["Makefile"]` results in `outcome: "scope_violation_reconciled"`, `reconciled_tracked: ["other.py"]`, `reconcile_kept_tracked: []`, and `git diff HEAD -- other.py` empty post-call.
- Mixed case: `out_of_scope_tracked: ["Makefile", "other.py"]` for the same schedule → `reconcile_kept_tracked: ["Makefile"]`, `reconciled_tracked: ["other.py"]`, `outcome: "scope_violation_reconciled"`.
- Schedule-missing fallback: omitting `--schedule-file` (or pointing at a non-existent path) reverts to today's behaviour and emits `warning: "schedule_lookup_failed"` in the result; existing tests keep passing.
- `SKILL.md` §Phase B batch-join-barrier paragraph at line 624 documents the new `--schedule-file <path>` argument. The wider `--repo-root` plus `--schedule-file` invocation pattern lands in the prose verbatim.
- Existing `TestReconcileBatch` cases in `tests/scripts/test_plan_ops.py` (no_op, untracked unlink, tracked restore, multi-envelope, protected-skip, residual_dirty failure path) keep passing unchanged.
- New test cases added to `tests/scripts/test_plan_ops.py`'s `TestReconcileBatch` (or a dedicated `TestReconcileBatchPlanAware` class):
  - `test_preserves_tracked_when_in_dispatched_task_files`
  - `test_restores_tracked_when_outside_dispatched_task_files`
  - `test_preserves_untracked_create_when_in_dispatched_task_files`
  - `test_mixed_kept_and_restored_in_one_envelope`
  - `test_falls_back_when_schedule_file_missing` (asserts the warning + today's behaviour)
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "reconcile"` returns 0.

## Tasks

### TASK-003: `reconcile-batch` plan-aware preservation

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit)
  - `tests/scripts/test_plan_ops.py` (edit)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "reconcile"`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:3260-3402` (`reconcile_batch` + `cmd_reconcile_batch`)
  - `plugins/plan-executor/scripts/plan_ops.py:6906-7012` (the `_normalize_files_entry` / `_extract_task_files_from_plan` neighbourhood — reuse where possible)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:614-625` (the Phase B batch-join-barrier paragraph)
  - `tests/scripts/test_plan_ops.py:5462-5580` (existing reconcile test scaffolding)
- **Symbol targets:**
  - `reconcile_batch` in `plan_ops.py`
  - `cmd_reconcile_batch` in `plan_ops.py`
- **Acceptance criteria:**
  - `reconcile_batch(batch_envelopes, repo_root, *, schedule_file=None)` accepts and uses an optional schedule path. With the path supplied, the function reads `tasks[task_id].files` from the loaded schedule for each envelope, normalises each entry through `_plan_paths.normalize_files_entry`, and partitions the envelope's `out_of_scope_tracked` / `out_of_scope_untracked` into `kept` and `restored` buckets per the rule in §Scoped Context. Without the path (or when the lookup fails) the function falls back to today's behaviour and emits `warning: "schedule_lookup_failed"` in the result entry.
  - `cmd_reconcile_batch` exposes `--schedule-file <path>` and forwards it. Absent flag → today's behaviour.
  - Each result dict gains `reconcile_kept_tracked: list[str]` and `reconcile_kept_untracked: list[str]`.
  - The `outcome` enum extends with `"scope_violation_preserved"` when the partition kept paths and restored nothing.
  - `SKILL.md` §Phase B batch-join-barrier paragraph at line 624 documents the new `--schedule-file <path>` argument and notes that without it the orchestrator may wipe a correct in-scope edit on a stale-wrapper envelope.
  - Existing `TestReconcileBatch` cases pass unchanged.
  - New test cases added to `tests/scripts/test_plan_ops.py` with these exact (or near-identical) names:
    - `test_preserves_tracked_when_in_dispatched_task_files` — asserts that a file present in BOTH `out_of_scope_tracked` AND the dispatched task's `tasks[task_id].files` is NOT restored: post-call `git diff HEAD -- <file>` is non-empty AND the result emits `outcome: "scope_violation_preserved"`, `reconcile_kept_tracked: ["<file>"]`, `reconciled_tracked: []`.
    - `test_restores_tracked_when_outside_dispatched_task_files` — file in `out_of_scope_tracked` but NOT in the schedule's task files is restored as today: `outcome: "scope_violation_reconciled"`, `reconciled_tracked: ["<file>"]`, `reconcile_kept_tracked: []`, post-call `git diff HEAD -- <file>` is empty.
    - `test_preserves_untracked_create_when_in_dispatched_task_files` — analogous case for untracked: declared `(create)` file in `out_of_scope_untracked` is preserved on disk.
    - `test_mixed_kept_and_restored_in_one_envelope` — single envelope with one declared and one undeclared path produces both `reconcile_kept_tracked` AND `reconciled_tracked` entries; outcome is `scope_violation_reconciled` (any restoration trumps preserved-only).
    - `test_falls_back_when_schedule_file_missing` — `--schedule-file` pointing at a non-existent path emits `warning: "schedule_lookup_failed"` and reverts to today's restore-everything behaviour; this is the back-compat smoke.
  - Each new test sets up a tiny git repo + a tiny `schedule.json` fixture file under `tmp_path`, builds an envelope, invokes `reconcile-batch` via the existing `_run_reconcile` helper (extended with the new flag), and asserts both the result-dict shape and the on-disk state via `git diff HEAD -- <file>` / `Path.exists()`.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "reconcile"` returns 0.
- **Reversion guidance:** revert the `reconcile_batch` signature change, drop the `--schedule-file` CLI argument, and remove the new test cases. The fallback path keeps the function safe under partial revert (existing callers without the flag continue to work).

**Description:**
Make `reconcile-batch` plan-aware. The orchestrator passes the persisted schedule path; for each envelope, the function consults the dispatched task's normalised `Files:` list and PRESERVES any `out_of_scope_tracked` / `out_of_scope_untracked` entry that is in fact declared in scope. This closes the wiping-a-correct-edit failure mode that fired in run `20260425T023954` even when the wrapper's classification is buggy. The new `reconcile_kept_*` fields and the `scope_violation_preserved` outcome label disambiguate "preserved" from "restored" so the run log records what actually happened.

**Implementation notes:**
- Reuse `_plan_paths.normalize_files_entry` (landing in TASK-002) for the per-task `Files:` normalisation. Do NOT re-implement the parser inline.
- The schedule file is JSON. `json.loads(Path(schedule_file).read_text(encoding='utf-8'))['tasks']` is a list of task dicts; build `{task['task_id']: set(map(normalize_files_entry, task.get('files', [])))}` once at the top of the function.
- Task-id matching uses the canonical 3-digit form. Envelopes carry `task_id` in canonical form already; the schedule's `tasks[].task_id` is also canonical (the build-tasks pipeline normalises).
- The fallback path (no schedule, parse error, missing task) is intentionally permissive: emit a per-envelope `warning` field, keep today's restore behaviour. This preserves direct-CLI usage and protects test scaffolding.
- The protected-path check (`is_protected_path`) runs BEFORE the plan-aware filter — protected-infrastructure paths must never be touched, declared or not.
- When forwarding `--schedule-file` from SKILL.md, the orchestrator already has the schedule path bound from Phase 1.5; no new derivation logic is needed.
