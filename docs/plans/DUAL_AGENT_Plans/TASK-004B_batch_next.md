# TASK-004B — `batch-next` batch fidelity + deadlock detection

**Parent plan:** [`TASK-004_scheduler_semantics.md`](TASK-004_scheduler_semantics.md) (superseded — split into A/B/C/D/E)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9.1-§9.6
**Base branch:** `phase-7b5-bug-fixes`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract), TASK-002 (`_validate_schedule_dag` helper).
**Issues absorbed:** ISSUE-010 (P1, primary), ISSUE-019 (P2, secondary — DAG defensive check on this consumer).

---

## Goal

Make `batch-next` honor the analyst's declared batch structure as authoritative — ready tasks in a later batch are not chosen until the earlier batch is fully resolved (every task `done` OR `failed`). And make `scheduler_stuck` correctly flag deadlock in the cross-batch case where the active batch has unfinished tasks but none are in the global `ready` set.

## Scoped Context

### ISSUE-010 (P1) — `batch-next` does not honor declared batches

- **Location:** `scripts/plan_ops.py:1029-1109` (`cmd_batch_next`).
- **Current behavior:**
  - Lines 1055-1060 build `tasks_by_id` from the full `tasks[]` list.
  - Lines 1071-1072 compute `ready` globally over the entire schedule.
  - Lines 1079-1086 iterate `data["batches"]` only to report a `batch_index`. The active-batch selector at line 1081 has its own bug: `if any(tid in done or tid in failed for tid in bids): continue` skips a batch as soon as **any** of its tasks resolves, instead of when **all** resolve. A batch with one done + one failed task is silently skipped, leaving its dependents permanently un-blockable.
  - Lines 1090-1100 pick from the global `ready` set regardless of batch membership.
- **Defect:** the analyst's batch structure is advisory rather than authoritative. Interleaving guarantees the design promises are not actually enforced.
- **Fix:** select the active batch as the first one whose tasks are not **all** `done|failed`. Within that batch, intersect with `ready`. Pick only from the intersection (respecting file locks and `--parallel`). A ready task in a later batch is not eligible until the active batch is fully resolved.

### ISSUE-019 (P2, secondary) — defensive DAG check

Primary fix lives in TASK-002 (`parse-schedule` now validates the DAG). TASK-004B adds the same `_validate_schedule_dag` defensive check to `cmd_batch_next` so a cycle introduced after parse-time fails fast. The current code calls `_validate_schedule` (line 1044) which itself calls `_validate_schedule_dag` — confirm that TASK-002 has the call wired up and just keep the existing `_validate_schedule(data)` invocation here. No additional code needed if TASK-002 is already complete.

### Hard-won regression coverage from prior runs

- **Run `20260415T000811`** — `scheduler_stuck` formula was `len(picked) == 0 and len(ready_in_batch) > 0`. Returns `False` in the cross-batch deadlock case (where `ready_in_batch` is empty because the active batch's task depends on a later-batch task), so the orchestrator spins indefinitely. V3 below locks in the correct formula.
- **Run `20260415T022232`** — `active_batch` selector used `all(tid in done for tid in bids)` (no `or tid in failed`). A batch with one `done` + one `failed` task never advances past, blocking the next batch indefinitely. V4 below locks in the `done|failed` semantics.

Both bugs surfaced in plain post-implementation review — the implementer in each case was working from a plan that did not specify the cross-cutting invariant explicitly enough.

---

## Verification

**V1 — `batch-next` honors declared batch.**

```python
def test_batch_next_honors_declared_batch(tmp_path):
    # Schedule: batch 1 = [001, 003], batch 2 = [002]. 001 and 003 have no
    # deps; 002 has no deps either (independent). All three are therefore in
    # the global `ready` set. With empty done/failed, batch-next MUST return
    # only [001, 003] from batch 1 and MUST NOT include 002 from batch 2,
    # even though 002 is globally ready and has no file-lock conflict.
    # This is the pure batch-fidelity test: later-batch ready tasks are
    # ineligible until the active batch is fully resolved.
```

**V2 — `batch-next` waits for earlier batch completion.**

```python
def test_batch_next_waits_for_earlier_batch_completion(tmp_path):
    # Same schedule as V1. Mark 001 done, 003 done. batch-next must now return
    # [002] from batch 2. Confirms advance happens on full resolution of batch 1.
```

**V3 — `scheduler_stuck` reflects cross-batch deadlock.** *(Hard-won regression from run `20260415T000811`.)*

```python
def test_batch_next_scheduler_stuck_on_cross_batch_deadlock(tmp_path):
    # Schedule: batch 1 = [001 depends on 002], batch 2 = [002].
    # Initial state: nothing done, nothing failed, nothing in-flight.
    # batch-next selects active_batch = batch 1, but ready_in_batch is empty
    # because 001's dep (002) is in a later batch and unsatisfied.
    # Authoritative batch semantics make this a permanent deadlock — the
    # orchestrator must observe scheduler_stuck=True so it can halt with a
    # diagnostic instead of spinning. picked == [] AND active batch has
    # unfinished tasks AND those unfinished tasks are NOT in global `ready`.
    # Pair with positive control: same schedule with 002 done — batch-next
    # should now return [001] and scheduler_stuck=False.
```

The previous run computed `scheduler_stuck = len(picked) == 0 and len(ready_in_batch) > 0`, which returns `False` in the deadlock case (because `ready_in_batch` is empty). The correct formulation: `scheduler_stuck = (picked == []) and any(tid not in done and tid not in failed for tid in active_batch_ids)`.

**V4 — `active_batch` advances past mixed `done|failed` batches.** *(Hard-won regression from run `20260415T022232`.)*

```python
def test_batch_next_advances_past_done_or_failed_batch(tmp_path):
    # Schedule: batch 1 = [001, 002], batch 2 = [003]. 001 done, 002 failed,
    # nothing in 003. batch-next must select batch 2 and return [003].
    # The previous formulation `all(tid in done for tid in bids)` returned
    # False (002 is not in done), so active_batch never advanced past batch 1.
    # Correct formulation: `all(tid in done or tid in failed for tid in bids)`.
    # Pair with positive control: batch 1 = [001, 002] both done — batch-next
    # should also advance to batch 2 (proves the `or failed` extension didn't
    # break the all-done case).
```

**V5 — `batch-next` respects `--parallel` cap within active batch.**

```python
def test_batch_next_respects_parallel_cap(tmp_path):
    # batch 1 = [001, 002, 003], all ready, no file overlap. --parallel 2 returns
    # exactly 2 task_ids from batch 1.
```

**V6 — `batch-next` respects file locks within active batch.**

```python
def test_batch_next_respects_file_locks(tmp_path):
    # Case A (partial pick, NOT stuck): batch 1 = [001 (files=[a]),
    # 002 (files=[a])]. Both ready. batch-next returns exactly one of them
    # (the other is file-locked by the first pick). picked != [], so
    # scheduler_stuck=False per the formula (stuck requires picked == []).
    # Case B (full lock, IS stuck): mark 001 as "in-flight" via
    # --locked-files=a so the active batch has 002 remaining with its files
    # blocked; batch-next returns picked=[] and scheduler_stuck=True because
    # the only unfinished active-batch task cannot run.
```

**V7 — `batch-next` returns empty when all batches done.**

```python
def test_batch_next_empty_when_all_batches_done(tmp_path):
    # All tasks done. batch-next returns task_ids=[], scheduler_stuck=False,
    # batch_index=0 (sentinel — no active batch).
```

**V8 — DAG defensive check.**

Feed a cycle JSON through `batch-next`; halts with `errors[*].code == "dependency-cycle"`.

**V9 — File locks from `--locked-files` block selection.**

```python
def test_batch_next_external_lock_blocks_pick(tmp_path):
    # batch 1 = [001 (files=[a]), 002 (files=[b])]. Both ready.
    # --locked-files=a → batch-next returns [002]; scheduler_stuck=False.
    # --locked-files=a,b → batch-next returns []; scheduler_stuck=True (because
    # active batch has unfinished tasks).
```

**V10 — Output shape regression.**

```python
def test_batch_next_output_shape(tmp_path):
    # Assert returned JSON has exactly the keys
    # {"batch_index", "task_ids", "file_locks", "scheduler_stuck"}.
    # No extra keys, no missing keys.
```

**V11 — No `batches[]` at all, with unresolved tasks.**

```python
def test_batch_next_emits_stuck_when_batches_missing(tmp_path):
    # Schedule has tasks=[{id=001, ...}] and batches=[]. Nothing done.
    # MUST emit scheduler_stuck=True with picked=[] and task_ids=[].
    # The silent-success path (scheduler_stuck=False) is forbidden —
    # it would let the orchestrator spin forever thinking work is done.
    # Policy lock-in: this plan commits to the scheduler_stuck=True
    # variant (NOT exit 1). Do not relax to "either outcome OK".
```

**V12 — Batch with empty `task_ids`, with unresolved tasks.**

```python
def test_batch_next_emits_stuck_when_active_batch_is_empty(tmp_path):
    # Schedule has tasks=[{id=001}] and batches=[{index=1, task_ids=[]}].
    # MUST emit scheduler_stuck=True (same policy as V11).
```

**V13 — Unresolved task omitted from all batches.**

```python
def test_batch_next_emits_stuck_when_task_is_not_in_any_batch(tmp_path):
    # Schedule has tasks=[{id=001}, {id=002}] and batches=[{index=1,
    # task_ids=["001"]}]. 001 done, 002 pending but not in any batch.
    # MUST emit scheduler_stuck=True (same policy as V11).
```

**V14 — Task in active batch whose dep is failed is not picked.**

```python
def test_batch_next_skips_active_batch_task_with_failed_dep(tmp_path):
    # batch 1 = [001], batch 2 = [002 depends on 001]. 001 failed.
    # batch-next advances past batch 1 (001 resolved as failed), active_batch
    # becomes batch 2. 002's dep is in `failed`, so `_ready(002)` is False;
    # 002 is NOT picked. scheduler_stuck MUST be True (batch 2 has an
    # unfinished task that cannot run). This locks the cousin invariant
    # from _ready() at scripts/plan_ops.py:1062-1066: tasks with ANY failed
    # dependency are never ready.
```

---

## Tasks

### TASK-004B: Enforce batch fidelity and cross-batch deadlock detection in `batch-next`

- **Status:** pending
- **Priority:** high
- **Files:**
  - `scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
  - `.claude/skills/implement-plan/SKILL.md` (one-line update to Phase A→B description if needed)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - `batch-next` selects the active batch as the **first** batch whose tasks are NOT all `done OR failed`. The selector MUST use `all(tid in done or tid in failed for tid in bids)` — the `or failed` clause is mandatory. **(See V4.)**
  - Within the active batch, `batch-next` picks ONLY from tasks that are (a) in the active batch's `task_ids`, (b) ready (deps satisfied, none failed), (c) not file-locked. Tasks in later batches are never selected even if globally ready. **(See V1, V2.)**
  - `scheduler_stuck` is `True` whenever `picked == []` AND the active batch has at least one unfinished task (`tid not in done and tid not in failed`). This includes:
    - (a) `ready_in_batch` non-empty but every candidate is file-locked.
    - (b) `ready_in_batch` empty because the active batch's unfinished tasks have unsatisfied dependencies in a later batch (cross-batch deadlock).
    The previous formulation `picked == [] and len(ready_in_batch) > 0` is forbidden — it returned `False` in case (b) and let the orchestrator spin. **(See V3.)**
  - DAG defensive check present at entry (already provided by `_validate_schedule(data)` from TASK-002 — confirm the call is wired and add an explicit assertion in tests).
  - **Malformed batch structure MUST emit `scheduler_stuck=True`, never silent `scheduler_stuck=False`.** If `active_batch` cannot be selected (e.g., `batches=[]`, all batches have empty `task_ids`, or unresolved tasks are not listed in any batch) AND unresolved tasks remain, `batch-next` MUST emit `scheduler_stuck=True` with `picked=[]` / `task_ids=[]`. Same rule applies when `active_ids` becomes empty after normalization. Exit 1 with a malformed-batch error is explicitly NOT chosen here — V11/V12/V13 assert `scheduler_stuck=True` only. **(See V11, V12, V13.)**
  - **Tasks with any failed dependency are never picked**, even when they are in the active batch. This preserves the existing `_ready()` invariant at `scripts/plan_ops.py:1062-1066`. **(See V14.)**
  - All verification checks V1–V14 pass.
- **Out of scope (handled by sibling sub-plans):**
  - `filter-schedule` subcommand → TASK-004A
  - `fail-task` untracked cleanup → TASK-004C
  - `block-dependents` plan-markdown mutation → TASK-004D
  - `acquire-lock` strict shape → TASK-004E

**Description:**
Make the analyst's batch structure authoritative in `batch-next`. Fix the cross-batch deadlock blind spot in `scheduler_stuck`. Fix the active-batch advancement bug that ignores `failed` tasks.

**Implementation notes:**
The cleanest implementation re-uses `_validate_schedule_dag` (TASK-002) and the existing `_normalize_task_id` / `done|failed` set-membership pattern (`scripts/plan_ops.py:1055-1066`). Do not regress existing tests for `cmd_batch_next` that exercise the global-ready behavior — those tests are documenting the current bug; update them to assert the new batch-fidelity contract.

**Reversion guidance:**
If a real plan legitimately requires global-ready selection, gate the new behavior behind a `--legacy-batch-next` switch — never restore global-ready as the default. The cross-batch deadlock detection (V3) and the `done|failed` advancement (V4) are independent bug fixes and must NEVER be reverted.

---

## Implementation Playbook

### Step 1 — Replace the picking block in `cmd_batch_next`

Current code (`scripts/plan_ops.py:1071-1108`) does:
- compute global `ready`
- iterate batches only to set `batch_index`
- pick from global `ready` regardless of batch
- compute `scheduler_stuck = len(picked) == 0 and len(ready) > 0`

Replace with:

```python
# Keep existing setup: tasks_by_id, _ready(), _files(), remaining, ready (global).

# 1. Find active batch: first batch where NOT all tasks are done|failed.
active_batch: dict | None = None
for b in (data.get("batches") or []):
    bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
    bids = [tid for tid in bids if tid]
    if not bids:
        continue
    # MANDATORY: `or tid in failed` — without it, a mixed-resolution batch
    # never advances. See V4 / Run 20260415T022232 regression.
    if all(tid in done or tid in failed for tid in bids):
        continue
    active_batch = b
    break

if active_batch is None:
    # Guard against malformed batch structure masquerading as "all done".
    # If there are still tasks not in done|failed, we must NOT report
    # scheduler_stuck=False — that would let the orchestrator spin. See
    # V11, V12, V13. Possible reasons active_batch is None despite
    # unresolved work: batches=[], every batch has empty task_ids[], or a
    # task is not listed in any batch.
    unresolved = [tid for tid in tasks_by_id.keys()
                  if tid not in done and tid not in failed]
    if unresolved:
        # Policy: surface as scheduler_stuck (do NOT silently succeed).
        # Keep batch_index=0 sentinel to signal "no active batch found".
        _emit(args, {
            "batch_index": 0,
            "task_ids": [],
            "file_locks": [],
            "scheduler_stuck": True,
        })
        return
    # Genuinely all done/failed — no work remains.
    _emit(args, {
        "batch_index": 0,
        "task_ids": [],
        "file_locks": [],
        "scheduler_stuck": False,
    })
    return

active_ids = {tid for tid in (
    _normalize_task_id(str(x)) for x in (active_batch.get("task_ids") or [])
) if tid}

# Guard: active_batch was selected because not-all-done|failed, but if
# active_ids is empty after normalization (every entry in task_ids
# normalized to None), treat as malformed batch data and surface
# scheduler_stuck — never silently succeed.
if not active_ids:
    _emit(args, {
        "batch_index": active_batch.get("index") if "index" in active_batch
            else active_batch.get("batch_index") or 0,
        "task_ids": [],
        "file_locks": [],
        "scheduler_stuck": True,
    })
    return

# 2. Restrict candidates to active batch.
ready_in_batch = []
for t in ready:
    raw_tid = t.get("id") if "id" in t else t.get("task_id")
    tid = _normalize_task_id(str(raw_tid))
    if tid in active_ids:
        ready_in_batch.append(t)

# 3. Pick respecting file locks + --parallel.
picked: list[str] = []
picked_files: list[str] = []
claimed = set(locked)
for t in ready_in_batch:
    raw_tid = t.get("id") if "id" in t else t.get("task_id")
    tid = _normalize_task_id(str(raw_tid))
    files = _files(t)
    if any(f in claimed for f in files):
        continue
    if len(picked) >= max(1, args.parallel):
        break
    picked.append(tid)
    picked_files.extend(files)
    claimed.update(files)

# 4. scheduler_stuck — cross-batch-deadlock-aware.
# MANDATORY formula: True iff `picked == []` AND active_batch has any
# unfinished task. The unfinished task's situation can be (a) file-locked or
# (b) not in `ready` because of a cross-batch dep — both are deadlock for
# authoritative batch semantics. See V3 / Run 20260415T000811 regression.
unfinished_active = [tid for tid in active_ids
                     if tid not in done and tid not in failed]
scheduler_stuck = (len(picked) == 0) and (len(unfinished_active) > 0)

batch_index = active_batch.get("index") if "index" in active_batch \
    else active_batch.get("batch_index")
if batch_index is None:
    batch_index = 0

_emit(args, {
    "batch_index": batch_index,
    "task_ids": picked,
    "file_locks": picked_files,
    "scheduler_stuck": scheduler_stuck,
})
```

**FORBIDDEN formulae (do NOT ship any of these):**

- `scheduler_stuck = len(picked) == 0 and len(ready_in_batch) > 0` — masks cross-batch deadlock (V3 regression).
- `scheduler_stuck = len(picked) == 0 and len(ready) > 0` — uses GLOBAL ready, not batch-scoped, so a globally-ready later-batch task sets `scheduler_stuck=False` even though the active batch is deadlocked.
- `if all(tid in done for tid in bids): continue` — without `or tid in failed`, a batch with one done + one failed never advances (V4 regression).
- `if any(tid in done or tid in failed for tid in bids): continue` — the CURRENT production bug at `scripts/plan_ops.py:1081`. This skips a batch as soon as ANY task resolves, jumping past it while unfinished tasks remain. Must be replaced with the `all(... done or failed)` form.
- Silently emitting `scheduler_stuck=False` when `active_batch is None` but `tasks_by_id` has unresolved tasks. Must emit `scheduler_stuck=True` (V11/V12/V13). Policy lock-in: this sub-plan commits to the `scheduler_stuck=True` variant. An alternative "exit non-zero with malformed-batch error" policy is explicitly NOT in scope; the tests MUST assert `scheduler_stuck=True` (not "either outcome OK").
- Silently emitting `scheduler_stuck=False` when `active_ids` is empty after normalization. Must emit `scheduler_stuck=True` (malformed batch data).

### Step 2 — DAG defensive check

`_validate_schedule(data)` (called at line 1044) already invokes `_validate_schedule_dag` per TASK-002. No new code needed. Add a test that confirms feeding a cycle through `batch-next` halts with `errors[*].code == "dependency-cycle"` (V8).

### Step 3 — Tests in `tests/scripts/test_plan_ops.py`

At minimum (one test per V):

- `test_batch_next_honors_declared_batch` — V1.
- `test_batch_next_waits_for_earlier_batch_completion` — V2.
- `test_batch_next_scheduler_stuck_on_cross_batch_deadlock` (V3, hard-won regression) — schedule with `batch1=[001 dep 002]`, `batch2=[002]`, nothing done. Assert `picked == []` AND `scheduler_stuck == True`. Pair with positive control where `picked != []` and assert `scheduler_stuck == False` to lock the inverse.
- `test_batch_next_advances_past_done_or_failed_batch` (V4, hard-won regression) — schedule with batch1=[001,002], batch2=[003], 001 done, 002 failed. Assert batch-next selects batch 2 and returns [003]. Pair with positive control where batch1 tasks are both done and assert advance also works (proves the `or failed` extension didn't break the all-done path).
- `test_batch_next_respects_parallel_cap` — V5.
- `test_batch_next_respects_file_locks` — V6 (assert `scheduler_stuck=True` when file-locked).
- `test_batch_next_empty_when_all_batches_done` — V7.
- `test_batch_next_dag_cycle_rejected` — V8.
- `test_batch_next_external_lock_blocks_pick` — V9.
- `test_batch_next_output_shape` — V10.
- `test_batch_next_rejects_missing_batches_when_work_remains` — V11; schedule has `batches=[]` but `tasks=[{id=001}]` pending.
- `test_batch_next_rejects_empty_batch_task_ids_when_work_remains` — V12.
- `test_batch_next_rejects_orphan_task_not_in_any_batch` — V13; unresolved task omitted from all batches.
- `test_batch_next_skips_active_batch_task_with_failed_dep` — V14; active batch contains task with failed dep; not picked, `scheduler_stuck=True`.

### Step 4 — Existing test updates (regression sweep)

Run `venv/bin/pytest -q tests/scripts/test_plan_ops.py`. Any prior test for `cmd_batch_next` that asserted the global-ready behavior is documenting the ISSUE-010 bug; update it to assert the new batch-fidelity contract. This is not a regression — it is the fix.

### Step 5 — `SKILL.md` update (optional, only if wording changes)

If the existing Phase A→B description in `SKILL.md` implies global-ready behavior, tighten it to say "batch-next picks only from the active batch's tasks". Keep edit minimal.

---

## Out of Scope

- `filter-schedule` subcommand → TASK-004A
- `fail-task` untracked cleanup → TASK-004C
- `block-dependents` plan-markdown mutation → TASK-004D
- `acquire-lock` strict shape → TASK-004E
- Canonical contract (TASK-001), generic runtime validation (TASK-002), wrapper isolation (TASK-003), phase gates (TASK-005), fixture rewrite (TASK-006), self-audit (TASK-007), preflight (TASK-008), scale/locks/logs (TASK-009/010/011).

## Reversion guidance

If a real plan requires global-ready selection, gate behind `--legacy-batch-next`. Never restore global-ready as the default. The cross-batch deadlock detection (V3) and the `done|failed` advancement (V4) MUST NEVER be reverted — both fix bugs that broke prior runs.
