# TASK-004 — Scheduler, Dependency, and Blocked-State Semantics

> **⚠️ SUPERSEDED — DECOMPOSED INTO 5 SINGLE-ISSUE SUB-PLANS (2026-04-15).**
>
> After two consecutive D.2a review failures of the bundled TASK-004 plan (reviewers flagged regressions that were only visible because 7 issues were in scope at once), the parent plan was split into five single-issue sub-plans per the **Option C** directive. Each sub-plan has passed independent Codex review.
>
> **DO NOT implement this file.** It is retained as historical context. Implement the sub-plans instead:
>
> | ISSUE | Sub-plan | Path |
> |---|---|---|
> | ISSUE-006 (+ 020 secondary) | TASK-004A — `filter-schedule` subcommand | [`TASK-004A_filter_schedule.md`](TASK-004A_filter_schedule.md) |
> | ISSUE-010 | TASK-004B — `batch-next` batch-fidelity + stuck semantics | [`TASK-004B_batch_next.md`](TASK-004B_batch_next.md) |
> | ISSUE-011 | TASK-004C — `fail-task` tracked+untracked cleanup partition | [`TASK-004C_fail_task_cleanup.md`](TASK-004C_fail_task_cleanup.md) |
> | ISSUE-012 | TASK-004D — `block-dependents` plan-markdown mutation | [`TASK-004D_block_dependents_mutation.md`](TASK-004D_block_dependents_mutation.md) |
> | ISSUE-018 | TASK-004E — `acquire-lock` strict-shape + `--force` escape hatch | [`TASK-004E_acquire_lock_strict.md`](TASK-004E_acquire_lock_strict.md) |
>
> ISSUE-019 (secondary in the original scope) is addressed by TASK-003 (state isolation), which already shipped. The decomposition has no runtime change — each sub-plan is self-contained and passes its own Codex review.

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-004
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9.1-§9.6 (run phases), §8.3 (retry rules)
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract), TASK-002 (runtime validation, `write-schedule` subcommand).
**Issues absorbed:** ISSUE-006, 010, 011, 012, 018, 019 (secondary), 020 (secondary)
**Status:** superseded (2026-04-15) — see decomposition table above

---

## Goal

Make the orchestrator's scheduler, failure-propagation, and lock semantics actual invariants enforced by `plugins/plan-executor/scripts/plan_ops.py`, not best-effort helpers. `batch-next` selects only from the declared analyst batch; `fail-task` cleans up both tracked edits and created untracked files; `block-dependents` mutates the plan markdown's Status field to `blocked`; `acquire-lock` rejects malformed stale state; `filter-schedule` delivers the missing `--task-ids` path by writing through the `write-schedule` subcommand from TASK-002.

## Scoped Context

### ISSUE-006 (P1) — `--task-ids` flag documented but not implemented

- **Docs:** `plugins/plan-executor/skills/implement-plan/SKILL.md:64, 72, 111-113` specify the flag and its orphan-prerequisite behavior.
- **Helpers:** `plugins/plan-executor/scripts/plan_ops.py` has 13 subcommands, none of which filters a schedule by task ids.
- **Rule:** `plugins/plan-executor/skills/implement-plan/SKILL.md:316` forbids inline Python in the orchestrator, so the only path is a helper subcommand.
- **Fix:** add `plan_ops.py filter-schedule --schedule-file <path> --task-ids 1,3 --json`. Produces a new schedule containing the requested tasks plus their transitive prerequisites. Orphaned dependencies (request id depends on a task not in the reachable set) → exit 1 with a clear error. On success, writes via the `write-schedule` contract (TASK-002) — atomic, validated.

### ISSUE-010 (P1) — `batch-next` does not honor declared batches

- **Location:** `plugins/plan-executor/scripts/plan_ops.py:283-351` (`cmd_batch_next`).
- **Current behavior:** lines 299-303 build `tasks_by_id` from the full `tasks[]` list; line 315 computes `ready` globally; lines 322-329 iterate `data["batches"]` only to report a `batch_index`; lines 333-342 pick the first `--parallel` tasks from the global `ready` set regardless of whether they belong to the selected batch.
- **Defect:** the analyst's batch structure becomes advisory instead of authoritative. Interleaving guarantees the design promises are not actually enforced.
- **Fix:** within the selected batch's `task_ids`, intersect with `ready`, and draw only from that set. A ready task in a later batch is not eligible until the earlier batches complete.

### ISSUE-011 (P1) — `fail-task` leaves created untracked files

- **Location:** `plugins/plan-executor/scripts/plan_ops.py` `cmd_fail_task:482-523` (line 493 = the single `git restore` call).
- **Current behavior:** iterates `--files` and runs `git restore --` against each. Creates are untracked, so `git restore` is a no-op for them; the untracked file remains on disk.
- **Defect:** failed Claude `(create)` tasks leave artifacts behind that the next run inherits as noise.
- **Fix:** detect creates via `git ls-files --others --exclude-standard` intersected with the task's `allowed_files`, and `Path.unlink` each. Do not touch untracked files outside `allowed_files` — that is the sibling-safety invariant from TASK-003. Protect always-ignore paths regardless.

### ISSUE-012 (P1) — `block-dependents` does not mutate plan markdown

- **Location:** `plugins/plan-executor/scripts/plan_ops.py:526-563` (`cmd_block_dependents`).
- **Current behavior:** traverses the schedule to compute the blocked set (lines 539-553), appends `blocked` events to `_run_log.jsonl` (lines 555-561), but never calls `mutate_task_status` on the plan markdown.
- **Defect:** the plan document is no longer the source of truth after a cascade — it still shows affected tasks as `pending`.
- **Fix:** for each blocked id, mutate the plan markdown's `**Status:**` line to `blocked`, same pattern as `cmd_fail_task:499`.

### ISSUE-018 (P2) — `acquire-lock` tolerates orphan-shape JSON

- **Location:** `plugins/plan-executor/scripts/plan_ops.py:646-662`.
- **Current behavior:** parses existing lock JSON; only halts if `plan_abs in current and current[plan_abs].get("run_id") != args.run_id`. Orphan-shape JSON (e.g., `{"pid": 99999}`) merges silently; preflight classifies `_run_lock.json` as `infra_ignored` (ISSUE-008), so that path does not catch it either.
- **Defect:** a crashed prior run leaving a malformed lock does not block a new run. No PID liveness check, no age TTL.
- **Fix:** reject any pre-existing JSON that does not conform to `{<abs_plan_path>: {"run_id": "...", "acquired_at": "..."}}`. Support `--force` for explicit override. Document the lock shape in `SKILL.md`.

### ISSUE-019 (P2, secondary) — defensive DAG check

Primary fix lives in TASK-002 (`parse-schedule` now validates the DAG). TASK-004 adds the same defensive validation to `batch-next` and `filter-schedule`, since they are the other schedule consumers and can fail safer if they see a cycle the analyst missed.

### ISSUE-020 (P2, secondary) — `write-schedule` subcommand

Primary delivery in TASK-002. TASK-004 depends on it: `filter-schedule` emits the filtered JSON and either pipes it through `write-schedule` internally or returns the JSON on stdout for the orchestrator to pipe to `write-schedule`. Recommendation: return on stdout — keeps filter-schedule single-responsibility and makes the orchestrator composition obvious in `SKILL.md`.

---

## Verification

**V1 — `filter-schedule` happy path.**

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":["a"],"dependencies":[]},{"id":"002","agent":"claude","files":["b"],"dependencies":["001"]},{"id":"003","agent":"codex","files":["c"],"dependencies":[]}],"batches":[{"index":1,"task_ids":["001","003"],"file_locks":["a","c"]},{"index":2,"task_ids":["002"],"file_locks":["b"]}]}' \
  > /tmp/full.schedule.json

venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/full.schedule.json --task-ids 2 --json
```

Must return a schedule containing `tasks = [001, 002]` (002 requires 001) and appropriate batches. Exit 0.

**V2 — `filter-schedule` orphan detection.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/full.schedule.json --task-ids 999 --json
```

Exit 1, `errors` cites `unknown task id 999`.

**V3 — `batch-next` honors batch.**

```
def test_batch_next_honors_declared_batch():
    # Schedule: batch 1 = [001, 003], batch 2 = [002]. All tasks ready.
    # batch-next with --batch 2 returns only [002] even though 001 and 003 are also ready.
```

**V4 — `fail-task` cleans up untracked creates.**

```
def test_fail_task_removes_untracked_creates(tmp_path):
    # Init scratch repo; create untracked `foo.txt` in task's allowed_files.
    # Run fail-task --task-id 001 --files foo.txt; assert foo.txt no longer exists.
    # Also assert an untracked sibling file NOT in allowed_files is untouched.
```

**V5 — `block-dependents` mutates plan markdown.**

```
def test_block_dependents_mutates_plan_status(tmp_path):
    # Plan file with tasks 001 (failed) and 002 (depends on 001). Run block-dependents --failed 001.
    # Grep the plan file for `- **Status:** blocked` in the 002 task block.
```

**V6 — `acquire-lock` rejects orphan shape.**

```
def test_acquire_lock_rejects_orphan_shape(tmp_path):
    # Pre-write _run_lock.json = {"pid": 99999, "started_at": "..."}
    # Call acquire-lock; assert exit != 0 and no file mutation.
    # With --force, assert it overwrites.
```

**V7 — `acquire-lock` rejects same-plan conflict (regression).**

Existing `test_conflict_on_same_plan` still passes.

**V8 — `batch-next` and `filter-schedule` surface cycles.**

Feed the same cycle JSON from TASK-002 V3 through `batch-next` and `filter-schedule`; both halt with `dependency cycle`.

**V9 — Scenario 6 rerun.** With `filter-schedule` landed, the Phase 5 Scenario 6 path (`--task-ids 2,4` where `2` requires `1` and `1` was not requested) halts with the orphan error. See postmortem §2 Scenario 6.

**V10 — `batch-next` `scheduler_stuck` reflects cross-batch deadlock.** *(Added 2026-04-15 after run `20260415T000811` failed review.)*

```
def test_batch_next_scheduler_stuck_on_cross_batch_deadlock():
    # Schedule: batch 1 = [001 depends on 002], batch 2 = [002].
    # Initial state: nothing done, nothing failed, nothing in-flight.
    # batch-next selects active_batch = batch 1, but ready_in_batch is empty
    # because 001's dep (002) is in a later batch and unsatisfied.
    # Authoritative batch semantics make this a permanent deadlock — the
    # orchestrator must observe scheduler_stuck=True so it can halt with a
    # diagnostic instead of spinning. picked == [] AND active batch has
    # unfinished tasks AND those unfinished tasks are NOT in global `ready`.
```

The previous run computed `scheduler_stuck = len(picked) == 0 and len(ready_in_batch) > 0`, which returns `False` in the deadlock case (because `ready_in_batch` is empty). The correct formulation must also flag stuck when the active batch has unfinished tasks none of which are in the global ready set. Equivalent: `scheduler_stuck = (picked == []) and any(tid not in done and tid not in failed for tid in active_ids)`. Pair this test with a positive control where `picked != []` and assert `scheduler_stuck=False`.

**V11 — `filter-schedule` output is directly pipeable to `write-schedule`.** *(Added 2026-04-15 after run `20260415T000811` failed review.)*

The previous run wrote a test that stripped `warnings` and `errors` from `filter-schedule` stdout in Python before piping. That test is forbidden. The required test must use a literal shell pipe with **no Python-side reshaping**:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule \
  --schedule-file /tmp/full.schedule.json --task-ids 2 --json \
| venv/bin/python plugins/plan-executor/scripts/plan_ops.py write-schedule \
  --schedule-file /tmp/filtered.schedule.json --stdin --json
```

The `write-schedule` invocation must exit 0 and produce a file whose JSON parses back through `parse-schedule` cleanly (`outcome=valid`, no errors, no warnings about unknown top-level fields). The corresponding pytest test must invoke `subprocess.run(... shell=True)` (or equivalent two-`Popen` pipe) and only inspect the final exit code + the written file — never reshape stdout in Python before re-piping.

**Implementation implication:** `filter-schedule` success output MUST contain only the canonical schedule keys defined by `ALLOWED_SCHEDULE_TOP_LEVEL` (currently `outcome`, `tasks`, `batches`, `gaps`, `risks`). Any consumer-side warnings or errors must go to **stderr**, never to stdout, on success. On failure (orphan, cycle), `filter-schedule` continues to emit `errors` to stdout and exit 1 — that path is not piped.

---

## Tasks

### TASK-004: Enforce scheduler, dependency, and blocked-state semantics

- **Status:** failed
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - ISSUE-006, 010, 011, 012, 018, 019 (secondary), 020 (secondary) are resolved.
  - `batch-next` honors authoritative batch semantics — ready tasks in a later batch are not chosen until earlier batches complete.
  - `batch-next` `scheduler_stuck` correctly reflects cross-batch deadlock: returns `True` whenever the active batch has unfinished tasks and `picked` is empty (whether `ready_in_batch` is empty because of unsatisfied later-batch deps, or non-empty but file-locked). The previous formulation `picked == 0 AND ready_in_batch > 0` was insufficient — it returned `False` in the deadlock case and let the orchestrator spin. **(See V10.)**
  - `filter-schedule` subcommand exists, filters by `--task-ids`, computes transitive prerequisites, halts on orphans, emits valid filtered schedule JSON.
  - `filter-schedule` success output on stdout contains ONLY canonical schedule keys (`outcome`, `tasks`, `batches`, `gaps`, `risks`) — no `warnings`, no `errors`, no other consumer-side metadata. Direct shell pipe `filter-schedule | write-schedule` MUST succeed without any Python-side reshaping. Tests proving this must use a literal shell pipe (or two-`Popen`), never a Python dict.pop / del before re-piping. **(See V11.)**
  - Failed `(create)` tasks clean up predictably: `fail-task` removes allowed-file untracked creates; sibling untracked files untouched.
  - Blocked tasks are coherent across plan markdown (`**Status:** blocked`), in-memory state (schedule cache), and run-log events.
  - Lock-file format validated strictly enough to reject malformed stale state; `--force` bypass documented.
  - DAG defensive check present in every schedule consumer (`parse-schedule` from TASK-002, `batch-next`, `filter-schedule`).
  - All verification checks V1–V11 pass.

**Run history:**
- `20260415T000811` — FAILED at review (codex `needs-rework`; code-reviewer third-opinion `needs-rework`, agreed). Two acceptance-level bugs surfaced: `batch-next` `scheduler_stuck` regression (mishandled cross-batch deadlock) and `filter-schedule` success stdout polluted with `warnings`/`errors` keys that `ALLOWED_SCHEDULE_TOP_LEVEL` rejects, breaking the canonical pipe. The implementer also wrote `test_filter_schedule_pipes_to_write_schedule` that masked the second bug by Python-stripping the offending keys before piping. V10 and V11 added to lock in regression coverage. Bookkeeping commit: `686fabc`.

**Description:**
Make scheduling and failure propagation actual executor invariants rather than best-effort helper behavior.

**Implementation notes:**
Defensive DAG validation should exist downstream even if the analyst already does it upstream. Share the DAG helper with TASK-002's `parse-schedule`.

**Reversion guidance:**
If new scheduler behavior causes blocking regressions (e.g., a real plan legitimately wanted global ready-set behavior), preserve defensive validation and downgrade only the new enforcement path behind a `--legacy-batch-next` switch.

---

## Implementation Playbook

### Step 1 — `filter-schedule` subcommand

Add `cmd_filter_schedule` in `plugins/plan-executor/scripts/plan_ops.py`:

```
def cmd_filter_schedule(args):
    sched = _load_json(args.schedule_file)
    requested_ids = {_normalize_task_id(x) for x in args.task_ids.split(",") if x.strip()}
    if not requested_ids or None in requested_ids:
        _die(args, {"error": "invalid --task-ids"})

    tasks_by_id = {_normalize_task_id(t["id"]): t for t in sched["tasks"]}
    unknown = requested_ids - set(tasks_by_id.keys())
    if unknown:
        _die(args, {"error": f"unknown task ids: {sorted(unknown)}"})

    # Transitive closure over dependencies
    closed = set()
    stack = list(requested_ids)
    while stack:
        tid = stack.pop()
        if tid in closed: continue
        closed.add(tid)
        for dep in tasks_by_id[tid].get("dependencies", []):
            stack.append(_normalize_task_id(dep))

    # Build filtered tasks & batches preserving order
    out_tasks = [t for t in sched["tasks"] if _normalize_task_id(t["id"]) in closed]
    out_batches = []
    for b in sched["batches"]:
        bids = [_normalize_task_id(x) for x in b["task_ids"]]
        keep = [x for x in bids if x in closed]
        if keep:
            out_batches.append({**b, "task_ids": keep})

    # DAG defensive check (shared helper from TASK-002)
    errors = _validate_schedule_dag(out_tasks, out_batches)
    if errors:
        _die(args, {"errors": errors})

    # IMPORTANT: success stdout MUST contain ONLY the canonical schedule keys
    # so the caller can pipe `filter-schedule | write-schedule` without any
    # field stripping. Do NOT include `errors`, `warnings`, or any other
    # consumer-side metadata on the success path. If you need to surface
    # informational warnings, write them to sys.stderr — never to stdout
    # alongside the schedule. ALLOWED_SCHEDULE_TOP_LEVEL is the source of
    # truth; this emit must match it byte-for-byte on success.
    _emit(args, {
        "outcome": "valid",
        "tasks": out_tasks,
        "batches": out_batches,
        "gaps": sched.get("gaps", []),
        "risks": sched.get("risks", []),
    })
```

Register subparser:
```
parser_fs = subparsers.add_parser("filter-schedule", ...)
parser_fs.add_argument("--schedule-file", required=True)
parser_fs.add_argument("--task-ids", required=True, help="CSV of task ids or TASK-NNN forms")
parser_fs.add_argument("--json", action="store_true")
parser_fs.set_defaults(func=cmd_filter_schedule)
```

SKILL.md pipes: `plan_ops.py filter-schedule ... --json | plan_ops.py write-schedule --schedule-file ... --stdin --json`.

### Step 2 — `batch-next` batch fidelity

Rewrite the picking block in `cmd_batch_next` (lines 322-342):

```
# After computing `ready` globally (keep existing logic):
# 1. find the active batch: first batch whose task_ids are not all done.
# 2. intersect ready with active-batch task_ids.
# 3. pick from the intersection (respecting file locks and --parallel).

active_batch = None
for b in data.get("batches") or []:
    bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
    if all(tid in done for tid in bids if tid):
        continue
    active_batch = b
    break

if active_batch is None:
    _emit(args, {"batch_index": 0, "task_ids": [], "file_locks": [], "scheduler_stuck": False})
    return

active_ids = {_normalize_task_id(str(x)) for x in (active_batch.get("task_ids") or [])}
ready_in_batch = [t for t in ready if _normalize_task_id(str(t.get("id") or t.get("task_id"))) in active_ids]

picked = []; picked_files = []; claimed = set(locked)
for t in ready_in_batch:
    tid = _normalize_task_id(str(t.get("id") or t.get("task_id")))
    files = list(t.get("files") or [])
    if any(f in claimed for f in files):
        continue
    if len(picked) >= max(1, args.parallel):
        break
    picked.append(tid)
    picked_files.extend(files)
    claimed.update(files)

# scheduler_stuck must flag deadlock in BOTH cases:
#   (a) ready_in_batch is non-empty but every candidate is file-locked, AND
#   (b) ready_in_batch is empty because the active batch's unfinished tasks
#       have unsatisfied dependencies in a later batch (cross-batch deadlock).
# The earlier formulation `len(picked) == 0 and len(ready_in_batch) > 0`
# silently dropped case (b) — it returned False even when no progress was
# possible — and let the orchestrator spin. Compute it from the active batch's
# *unfinished* task set instead.
unfinished_active = [tid for tid in active_ids if tid not in done and tid not in failed]
scheduler_stuck = len(picked) == 0 and len(unfinished_active) > 0
_emit(args, {
    "batch_index": active_batch.get("index", active_batch.get("batch_index")),
    "task_ids": picked,
    "file_locks": picked_files,
    "scheduler_stuck": scheduler_stuck,
})
```

(`failed` here is the set already used elsewhere in `cmd_batch_next` for filtering ready tasks; if it is not a local in your branch, derive it from `args.failed` the same way `done` is derived.)

Also add a DAG defensive check at entry — share helper with TASK-002.

### Step 3 — `fail-task` untracked cleanup

In `cmd_fail_task` (`plugins/plan-executor/scripts/plan_ops.py:482`+):

After the existing `git restore` block, add:

```
allowed = set(files)
# Detect untracked creates inside allowed scope
untracked_result = subprocess.run(
    ["git", "ls-files", "--others", "--exclude-standard"],
    cwd=plan.parent.parent.parent,  # or use --repo-root arg if available
    capture_output=True, text=True
)
created_in_scope = [p for p in untracked_result.stdout.splitlines() if p in allowed]
# Skip always-ignore paths for safety
from scripts.plan_codex_dispatch import _in_always_ignore  # or inline the list
created_in_scope = [p for p in created_in_scope if not _in_always_ignore(p)]
for p in created_in_scope:
    try:
        Path(p).unlink()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        continue
```

Note: the always-ignore list lives in `plan_codex_dispatch.py` (TASK-003). Either duplicate the constant in `plan_ops.py` or factor into a shared module. Recommendation: new module `scripts/_plan_paths.py` with the constants.

Add a `--repo-root` argument to `fail-task` if one does not exist; resolve relative paths against it.

### Step 4 — `block-dependents` plan mutation

In `cmd_block_dependents` (`plugins/plan-executor/scripts/plan_ops.py:526`+), after the blocked-set traversal and before the run-log append loop:

```
# Mutate plan markdown status for each blocked id
plan = Path(args.plan_file)  # add --plan-file arg if missing
if plan.is_file():
    text = _load_text(plan)
    for bid in blocked:
        try:
            text, _ = mutate_task_status(text, bid, "blocked")
        except ValueError:
            # Task id not in plan body — log but continue
            continue
    _write_text(plan, text)
```

The existing `mutate_task_status` helper already handles the status-line rewrite; reuse it. Requires `cmd_block_dependents` to accept `--plan-file`; update argparse accordingly.

### Step 5 — `acquire-lock` strict shape

In `cmd_acquire_lock` (`plugins/plan-executor/scripts/plan_ops.py:646`):

```
current = {}
malformed = False
if RUN_LOCK_PATH.exists():
    try:
        raw = json.loads(RUN_LOCK_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            malformed = True
        else:
            # Every value must be {"run_id": str, "acquired_at": str}
            for k, v in raw.items():
                if not (isinstance(v, dict) and "run_id" in v and "acquired_at" in v):
                    malformed = True; break
            current = raw
    except json.JSONDecodeError:
        malformed = True

if malformed and not args.force:
    _die(args, {
        "acquired": False,
        "error": f"malformed lock file at {RUN_LOCK_PATH}; re-run with --force to overwrite",
    })
if malformed and args.force:
    current = {}
# existing conflict check + write
```

Add `--force` argparse flag. Document the lock-file shape at the top of `SKILL.md` (one paragraph under an "Lock file" subsection).

### Step 6 — DAG helper sharing

The `_validate_schedule_dag` helper from TASK-002 is now called from:
- `cmd_parse_schedule`
- `cmd_batch_next`
- `cmd_filter_schedule`

Cheap; runs in O(|tasks| + |dependencies|). Do not duplicate.

### Step 7 — tests

Add `tests/scripts/test_plan_ops.py` cases. Expected additions (at minimum):

- `test_filter_schedule_happy_path`
- `test_filter_schedule_transitive_prereqs`
- `test_filter_schedule_orphan_rejected`
- `test_filter_schedule_cycle_rejected`
- `test_filter_schedule_success_stdout_is_canonical_only` (V11 part 1) — runs `filter-schedule` and asserts the stdout JSON has exactly the keys `{"outcome","tasks","batches","gaps","risks"}`. No `warnings`, no `errors` on success.
- `test_filter_schedule_pipes_to_write_schedule_via_shell` (V11 part 2) — uses `subprocess.run(..., shell=True)` (or two `Popen` objects connected by `stdout=PIPE`) to perform a literal shell pipe `filter-schedule | write-schedule`, and asserts (a) the pipeline exits 0, (b) the resulting written file parses cleanly through `parse-schedule`. **Do NOT** read the `filter-schedule` JSON into Python and pop fields before piping; the whole point is to prove direct pipeability. If the test needs Python at all, it should only be to assert the result, not to reshape the intermediate.
- `test_batch_next_honors_declared_batch`
- `test_batch_next_waits_for_earlier_batch_completion`
- `test_batch_next_scheduler_stuck_on_cross_batch_deadlock` (V10) — schedule with `batch1=[001 dep 002]`, `batch2=[002]`, nothing done. Assert `picked == []` AND `scheduler_stuck == True`. Pair with a positive control where `picked != []` and `scheduler_stuck == False` to lock the inverse.
- `test_fail_task_removes_untracked_creates`
- `test_fail_task_preserves_sibling_untracked`
- `test_block_dependents_mutates_plan_status`
- `test_block_dependents_plan_missing_gracefully_handled`
- `test_acquire_lock_rejects_orphan_shape`
- `test_acquire_lock_force_overwrites`
- `test_acquire_lock_preserves_unrelated_entries` (regression — legitimate multi-plan lock)

**Do NOT** write a test that reshapes `filter-schedule` stdout in Python before piping (e.g., `data = json.loads(out); data.pop("warnings"); data.pop("errors"); pipe data`). That pattern hides the bug instead of catching it, and a previous run shipped exactly such a test. The reviewer will reject any retest of the canonical pipe that does not use a literal shell pipe (or `Popen | Popen`).

### Step 8 — `SKILL.md` updates

- Phase A analyst step: pipe through `filter-schedule | write-schedule` when `--task-ids` is set.
- Phase B batch loop: reference `batch-next` now honoring declared batches.
- Phase C failure path: reference `fail-task` handling creates and `block-dependents` mutating plan statuses.
- Lock semantics: document the lock-file shape and `--force`.

### Step 9 — `run-log-schema.md` updates

`blocked` events already logged; add a note that the plan markdown `**Status:**` also flips to `blocked` for each id.

### Step 10 — design doc updates

`DUAL_AGENT_PLAN_EXECUTOR.md`:
- §9.3 (from TASK-001 reservation): promote `filter-schedule` as the `--task-ids` path. Name both subcommands.
- §9.4 failure cascade: describe `block-dependents` mutating plan markdown.
- §9.6 lock file: document shape + `--force` policy.
- §15 appendix or §8.3: reference batch fidelity invariant.

### Step 11 — regression sweep

Run `venv/bin/pytest -q tests/scripts/test_plan_ops.py` and `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py`. Any prior test touching `batch-next` that relied on the global-ready behavior needs updating — this is not a regression, it is the fix for ISSUE-010.

---

## Out of Scope

- **Canonical contract**: TASK-001.
- **Generic runtime validation** (beyond DAG defensive check sharing): TASK-002.
- **Wrapper isolation**: TASK-003.
- **Phase gates**: TASK-005.
- **Fixture rewrite**: TASK-006.
- **Self-audit / drift detection**: TASK-007.
- **Preflight / portability**: TASK-008.
- **Scale-aware reads / global dep locks / bounded logs**: TASK-009, 010, 011.

## Reversion guidance

- **`filter-schedule`:** safe to revert; `--task-ids` goes back to being documented-but-unimplemented. Only SKILL.md needs a compensating edit.
- **`batch-next` fidelity:** if a real plan requires global-ready selection, gate behind `--legacy-batch-next`. Never restore global-ready as the default.
- **`fail-task` untracked cleanup:** if it misidentifies files (e.g., due to path normalization), scope the detection more narrowly (only `git ls-files` output); do not disable cleanup.
- **`block-dependents` plan mutation:** if `mutate_task_status` raises for a specific task id shape, add robustness there rather than disabling the mutation.
- **`acquire-lock` strictness:** if malformed-lock rejection is too aggressive for a specific legacy layout, add the legacy shape as an explicit accept-case, but require `--force` for truly unknown shapes.

## Execution log — 20260415T000811

Starting SHA: `34d3000934348708b62f9a06ae7159bb3bc72bfa`  → Ending SHA: `34d3000934348708b62f9a06ae7159bb3bc72bfa`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-004 | claude | codex+code-reviewer | needs-rework | FAILED | D.2a third-opinion agreed. Findings: (1) batch-next scheduler_stuck regression - computed from ready_in_batch, masks cross-batch deadlock; (2) filter-schedule emits warnings/errors top-level keys that write-schedule rejects, breaking pipe; test_filter_schedule_pipes_to_write_schedule masks by stripping fields before pipe. |

## Execution log — 20260415T022232

Starting SHA: `25d51a08b2c227a3b43fb7dbe0623bb5cb958385`  → Ending SHA: `25d51a08b2c227a3b43fb7dbe0623bb5cb958385`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 004 | claude | codex+code-reviewer-third-opinion | needs-rework | FAILED | Critical: batch-next active_batch ignores failed (line 1081); fail-task git restore over mixed tracked+untracked crashes (line 1462). Out-of-scope follow-ups: filter-schedule --stdin shape; acquire-lock shape. |
