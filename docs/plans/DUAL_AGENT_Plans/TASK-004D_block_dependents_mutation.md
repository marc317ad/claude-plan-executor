# TASK-004D — `block-dependents` must mutate plan markdown

**Parent plan:** TASK-004_scheduler_semantics.md (decomposed into A/B/C/D/E sub-plans after the bundled TASK-004 failed D.2a review twice due to scope/blast-radius issues).

**Status:** pending
**Base branch:** phase-7b5-bug-fixes
**Scope:** Single-issue sub-plan. Addresses **ISSUE-012** ONLY.

**Siblings (do not touch here):**
- TASK-004A: `filter-schedule` subcommand + DAG defensive check (ISSUE-006/020).
- TASK-004B: `batch-next` batch-fidelity + cross-batch deadlock (ISSUE-010).
- TASK-004C: `fail-task` tracked + untracked cleanup partition (ISSUE-011).
- TASK-004E: `acquire-lock` strict-shape rejection (ISSUE-018).

---

## Issue

**ISSUE-012 (P1) — `block-dependents` does not mutate plan markdown.**

- **Location:** `scripts/plan_ops.py:1331-1369` (`cmd_block_dependents`).
- **Current behavior:** traverses the schedule to compute the blocked set (lines 1344-1359), appends `blocked` events to `_run_log.jsonl` via `_append_run_log("blocked", ...)` (lines 1361-1367), but **never calls `mutate_task_status` on the plan markdown**. Emits `{"blocked_task_ids": [...]}` and exits.
- **Defect:** the plan document is no longer the source of truth after a cascade. Run-log says task X is blocked; plan markdown still shows it as `pending`. Any subsequent orchestrator re-entry reads the stale `pending` status from the plan, re-queues the task, and dispatches it — undoing the cascade.
- **Fix (spec):** for each blocked id, call `mutate_task_status(plan_text, blocked_id, "blocked")` and persist. Pattern already exists in `cmd_fail_task` at `scripts/plan_ops.py:1297-1302`.
- **Evidence the invariant is real:** `ALLOWED_TASK_STATUSES` at `scripts/plan_ops.py:41` includes `"blocked"`. The status is a first-class state; the helper is there; `cmd_block_dependents` just never calls it.

---

## Background — why this landed in its own sub-plan

The parent TASK-004 bundled 7 ISSUE-### (006, 010, 011, 012, 018 + 019/020 secondary) into one 600+-line diff. Two D.2a review rounds flagged cousin-bug regressions that only became visible because the reviewer had to hold all 7 fixes in context simultaneously. Decomposing gives each issue its own V-section, its own acceptance tests, and its own commit — so `block-dependents` can land on green independent of `batch-next` churn.

---

## Source-of-truth invariant

**The plan file is the source of truth for task state.** `_run_log.jsonl` is an observability stream. If the two diverge (plan shows `blocked`, log has no `blocked` event, or vice-versa) the plan wins. This governs the crash-window and fail-fast semantics below.

## Acceptance criteria

1. **Single plan read, single plan write.** One `_load_text(plan_path)` call before the mutate loop; one `_write_text(plan_path, mutated_text)` call after the mutate loop finishes (whether by running to completion OR by a `ValueError` from `mutate_task_status`). Per-id writes are forbidden. Combined with acceptance #4 this means: if mutations succeed for ids [A, B] and then raise for C, the single write reflects exactly A and B's flips — ids before the failure persist, ids at or after the failure (including C) do not. **(See V1, V2, V6.)**
2. **Ordering: mutate-all-in-memory → single plan write → log each applied id.** All plan mutations are applied to in-memory text first. The single file write happens once. THEN the run-log `blocked` event is appended, iterating ONLY the ids whose flip is reflected in the write. Run-log entries MUST NOT be written for ids whose plan flip did not persist. **(See V5, V6.)**
3. **Idempotent on re-block.** If a task is already `blocked` in the plan, calling `mutate_task_status(..., "blocked")` is allowed and MUST NOT fail. `mutate_task_status` at `scripts/plan_ops.py:657-685` already supports this (status set allows `blocked`; helper is write-same-value tolerant). The cascade MUST still emit a run-log `blocked` event for observability. **(See V7.)**
4. **Emit payload has TWO separate lists (they can diverge).**
   - `plan_mutations_applied: list[str]` — ids whose plan-markdown status was flipped AND whose flip was persisted by the single `_write_text` call.
   - `run_log_appended: list[str]` — ids for which a `blocked` run-log event was successfully appended via `_append_run_log`.
   If `_append_run_log` fails on id B after succeeding on id A, `plan_mutations_applied=[A,B,...]` but `run_log_appended=[A]`. The `_die()` payload on ANY failure path MUST include BOTH lists plus `failed_id`, `failed_stage` (`"plan_read" | "plan_mutate" | "plan_write" | "run_log_append"`), and `remaining` (ids not yet processed). **(See V5, V6, V10, V11, V12, V13.)**
5. **Fail-fast across ALL failure stages — with one DEFERRED exception for partial mutate failure.**
   - `plan_read` (`_load_text` raises) → `_die()` immediately. Nothing on disk yet.
   - `plan_write` (`_write_text` raises after the mutate loop) → `_die()` immediately. Partial in-memory flips discarded.
   - `run_log_append` (raises mid-loop after plan write) → `_die()` immediately on the FIRST failing id; do NOT continue appending subsequent ids. Earlier successful appends are captured in `run_log_appended`.
   - `plan_mutate` (`mutate_task_status` raises `ValueError` on id N after ids 0..N-1 succeeded) → **DEFERRED die**. Record `failed_id=N`, `failed_stage="plan_mutate"`, then complete the single plan write for ids 0..N-1 AND complete the run-log append loop for those same ids. `_die()` fires AFTER the write+log finish so that the plan file reflects applied mutations and the observability stream catches up. This is the V6 semantic.
   - **Double-failure precedence (explicit):** if a DEFERRED `plan_mutate` failure is already pending AND a subsequent `run_log_append` or `plan_write` call ALSO raises, the `_die()` payload reports the EARLIER `plan_mutate` stage/id as `failed_stage`/`failed_id`, and the LATER stage is captured as a nested secondary diagnostic (`secondary_failed_stage`, `secondary_failed_id`). The `run_log_appended` list truncates at the last successful append regardless. This makes the root cause (mutate failure) the primary signal and the downstream consequence (log failure masking) the secondary signal; otherwise review and post-mortem can diverge on which failure to trust.
   - No retry. No skip-and-continue beyond the deferred-die case described. **(See V5, V6, V10, V11, V12, V13.)**
6. **Crash window between plan-write and log-append is an observability gap, not state corruption.** If the process dies (SIGKILL / power loss / OOM) AFTER `_write_text` returns but BEFORE all `_append_run_log` calls complete, the plan has the correct `blocked` statuses and the log has an incomplete event sequence. On orchestrator re-entry, the plan state is authoritative and the cascade is correctly reflected. Transactional plan+log atomicity requires a journal and is explicitly out of scope.
7. **CLI argument surface.** New REQUIRED arg `--plan-file <path>` (absolute). Old args `--schedule-file`, `--failed`, `--run-id`, `--json` preserved. No other new args. **(See V8.)**
8. **JSON output shape — ADDITIVE, not exact-set.** Existing key `blocked_task_ids` preserved verbatim. New keys `plan_mutations_applied` and `run_log_appended`. V4 asserts the output CONTAINS these three keys (subset assertion), each being a list of strings. V4 does NOT assert an exact keyset — future additive metadata must remain non-breaking. **(See V4.)**
9. **BFS ordering preserved; sibling order is `tasks[]` array order.** The current BFS at `scripts/plan_ops.py:1344-1359` traverses dependents level-by-level, and within a level iterates in the order tasks appear in the schedule JSON's `tasks[]` array. This EXACT order MUST be preserved in `blocked_task_ids`, `plan_mutations_applied`, `run_log_appended`, and in the sequence of mutate + log calls. Two schedules that differ only in `tasks[]` ordering produce different emit orderings — do not impose a numeric sort. **(See V3.)**
10. **Skill callsite update — THREE places in SKILL.md.** `.claude/skills/implement-plan/SKILL.md` has block-dependents callsites at:
    - Line ~44 (CLI reference table row)
    - Line ~201 (Phase C command block)
    - Line ~287 (Phase D.4 prose reference)
    All three MUST be updated to include `--plan-file <abs>`. No other SKILL.md changes.
11. **Module banner update in `scripts/plan_ops.py`.** The module-level docstring banner near line 14 lists the `block-dependents` invocation. Update it to include `--plan-file <abs>`. One line.
12. **No-dependent success path is still a success.** If `--failed NNN` names an id that no other task depends on, BFS returns `[]`. The subcommand emits `{"blocked_task_ids": [], "plan_mutations_applied": [], "run_log_appended": []}` with exit 0. No plan read or write occurs in this path (the plan is untouched). **(See V14.)**
13. All verification checks V1–V16 pass.

---

## Out of scope — do NOT touch

- `cmd_fail_task` (TASK-004C handles its cleanup bug)
- `cmd_batch_next` (TASK-004B handles its batch-fidelity bug)
- `cmd_acquire_lock` (TASK-004E)
- `cmd_filter_schedule` (TASK-004A)
- Adding a `cascade_blocked` status distinct from `blocked` — out of scope; single blocked status is the contract.
- Auto-unblocking when the blocker is retried and succeeds — out of scope; the orchestrator rebuilds state from the plan on re-entry.
- Transitively emitting `failed` for blocked tasks — out of scope; `blocked` is a first-class status distinct from `failed`.

---

## Verification

**V1 — Single-level cascade mutates plan markdown.**

```python
def test_block_dependents_mutates_plan_status_single_level(tmp_path, isolated_plan):
    # Schedule: 001 failed, 002 depends on 001, 003 independent.
    # Before: plan shows 002 Status = pending.
    # After block-dependents --failed=001:
    #   - plan-markdown status for 002 is "blocked" (exact match on
    #     `**Status:** blocked` bullet)
    #   - plan-markdown status for 001 is unchanged (fail-task owns 001)
    #   - plan-markdown status for 003 is unchanged (not a dependent)
    #   - stdout JSON has blocked_task_ids == ["002"]
    #   - stdout JSON has plan_mutations_applied == ["002"]
    #   - stdout JSON has run_log_appended == ["002"]
    #   - run-log has exactly one `blocked` event with task_id=002
```

**V2 — Multi-level transitive cascade: single plan-read / single plan-write.**

```python
def test_block_dependents_transitive_chain_single_io(tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001, 003→002, 004→003.
    # Instrument by monkeypatching plan_ops._load_text and plan_ops._write_text
    # to count calls (wrap-and-delegate, not replace).
    # After block-dependents --failed=001:
    #   - _load_text called EXACTLY once on plan_path
    #   - _write_text called EXACTLY once on plan_path
    #   - Plan status for 002, 003, 004 all == "blocked"
    #   - blocked_task_ids == ["002","003","004"] (BFS order)
    #   - plan_mutations_applied == ["002","003","004"]
    #   - run_log_appended == ["002","003","004"]
```

**V3 — BFS ordering preserved across plan + run-log; sibling order is tasks[] array order.**

```python
def test_block_dependents_preserves_bfs_and_sibling_order(tmp_path, isolated_plan):
    # Schedule tasks[] ordering is intentional (NOT numeric sort):
    # [001, 003, 002, 005, 004] where
    #   001 failed; 002→001; 003→001; 004→002; 005→003.
    # BFS yields direct dependents first (iterating tasks[] in-order, so
    # 003 precedes 002) then transitive (005 precedes 004):
    #   blocked_task_ids == ["003", "002", "005", "004"]
    # Assert:
    #   - blocked_task_ids matches that sibling-insertion order EXACTLY
    #   - run-log `blocked` events appear in THAT order (not numeric sort)
    #   - plan_mutations_applied == blocked_task_ids
    #   - run_log_appended == blocked_task_ids
    # This locks the current traversal; a future "stable sort by id" would
    # fail V3 by design.
```

**V4 — Output shape: SUBSET assertion, not exact keyset.**

```python
def test_block_dependents_output_shape_subset(tmp_path, isolated_plan):
    # Assert stdout JSON:
    #   - Contains at least the keys
    #     {"blocked_task_ids", "plan_mutations_applied", "run_log_appended"}
    #   - Each of those three values is a list of strings
    # Do NOT assert an exact keyset. Future additive metadata is explicitly
    # permitted and non-breaking.
```

**V5 — Mutate failure: ValueError from mutate_task_status halts with full payload.**

```python
def test_block_dependents_mutate_failure_structured_die(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001.
    # Plan markdown is intentionally missing the TASK-002 block.
    # mutate_task_status will raise ValueError.
    # Expect:
    #   - Exits NON-ZERO (_die).
    #   - Payload has errors[0] with keys: failed_id, failed_stage, 
    #     plan_mutations_applied, run_log_appended, remaining.
    #   - failed_id == "002"; failed_stage == "plan_mutate".
    #   - plan_mutations_applied == []; run_log_appended == []; remaining == [].
    #   - run-log has NO `blocked` event for 002.
    #   - Plan file on disk: unchanged (no ids had successful mutations to write).
```

**V6 — Partial-failure: first mutate succeeds, second raises, single write captures partial.**

```python
def test_block_dependents_partial_mutate_failure_partial_persists(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed; 002, 003 both direct dependents in tasks[] order.
    # Plan markdown has TASK-002 block; TASK-003 block missing.
    # Behavior:
    #   - In-memory mutate succeeds for 002, raises for 003.
    #   - Single _write_text fires with text containing ONLY 002's flip.
    #   - Run-log loop runs for [002], not [003].
    #   - Exits NON-ZERO via _die.
    # Assertions:
    #   - Plan on disk: 002 Status = "blocked"; 003 block still missing.
    #   - errors[0].failed_id == "003"; failed_stage == "plan_mutate"
    #   - errors[0].plan_mutations_applied == ["002"]
    #   - errors[0].run_log_appended == ["002"]
    #   - errors[0].remaining == []  (only 2 direct dependents)
    #   - run-log has `blocked` event for 002; no event for 003.
```

**V7 — Idempotent re-block.**

```python
def test_block_dependents_is_idempotent_on_already_blocked_task(
        tmp_path, isolated_plan):
    # Schedule: 001 failed, 002 depends on 001.
    # Pre-condition: plan markdown already shows 002 Status = blocked
    # (simulating a prior partial run).
    # Run block-dependents --failed 001:
    #   - Exits 0.
    #   - plan markdown for 002 remains "blocked" (no drift).
    #   - blocked_task_ids == ["002"].
    #   - plan_mutations_applied == ["002"] (applied again, same value = no-op
    #     but counted).
    #   - run-log has a new `blocked` event for 002 (observability: the
    #     cascade fired, even if plan state was already correct).
```

**V8 — CLI argument surface.**

```python
def test_block_dependents_cli_signature(capsys):
    # Invoking without --plan-file must exit non-zero with a clear error
    # pointing at the missing argument. Old signature (without --plan-file)
    # is explicitly not supported — breaking change that SKILL.md Phase C
    # and Phase D.4 MUST both be updated to match.
```

**V9 — Schedule-file path: unchanged dependents are NOT flipped.**

```python
def test_block_dependents_leaves_non_dependents_untouched(
        tmp_path, isolated_plan):
    # Schedule: 001 failed. 002 depends on 001. 003, 004, 005 are independent,
    # status = pending in plan markdown.
    # After:
    #   - 003, 004, 005 plan-status still "pending" (not modified).
    #   - Only 002 flipped to "blocked".
    #   - plan_mutations_applied == ["002"]; run_log_appended == ["002"].
```

**V10 — Plan READ failure halts before any mutation.**

```python
def test_block_dependents_plan_read_failure(tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001.
    # Monkeypatch plan_ops._load_text to raise OSError("simulated read fail").
    # Assert:
    #   - Exits NON-ZERO (_die)
    #   - errors[0].failed_stage == "plan_read"
    #   - errors[0].failed_id is None  (no id had been touched)
    #   - plan_mutations_applied == []; run_log_appended == []
    #   - remaining == ["002"]  (the full cascade)
    #   - run-log has NO `blocked` event for 002
    #   - Plan file on disk: unchanged
```

**V11 — Plan WRITE failure after successful mutations: no log appends fire.**

```python
def test_block_dependents_plan_write_failure(tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001, 003→002 (two ids in cascade: 002 and 003).
    # All mutations succeed in-memory.
    # Monkeypatch plan_ops._write_text to raise OSError("simulated write fail").
    # Assert:
    #   - Exits NON-ZERO (_die)
    #   - errors[0].failed_stage == "plan_write"
    #   - errors[0].failed_id is None  (the write is an atomic batch)
    #   - plan_mutations_applied == []  (write failed, so no ids persisted)
    #   - run_log_appended == []  (log appends happen AFTER write; write failed)
    #   - remaining == []  (all ids were mutated in-memory, none pending)
    #   - Plan file on disk: unchanged
    #   - run-log has NO `blocked` events for 002 or 003
```

**V12 — Log APPEND failure mid-loop: plan has all flips; log has partial.**

```python
def test_block_dependents_log_append_failure_mid_loop(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001, 003→002.
    # All mutations succeed; plan-write succeeds; log append for 002 succeeds;
    # log append for 003 raises (monkeypatch: count calls, raise on second).
    # Assert:
    #   - Exits NON-ZERO (_die)
    #   - errors[0].failed_stage == "run_log_append"
    #   - errors[0].failed_id == "003"
    #   - plan_mutations_applied == ["002","003"]  (written to plan)
    #   - run_log_appended == ["002"]  (only first log succeeded)
    #   - remaining == []
    #   - Plan file on disk: BOTH 002 and 003 flipped to "blocked"
    #     (plan-write happened before log-loop)
    #   - run-log has ONE `blocked` event for 002, none for 003
```

**V13 — Log APPEND failure on FIRST id: plan already has all flips.**

```python
def test_block_dependents_log_append_failure_first_id(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001.
    # All mutations succeed; plan-write succeeds; log append for 002 raises.
    # Assert:
    #   - Exits NON-ZERO (_die)
    #   - errors[0].failed_stage == "run_log_append"
    #   - errors[0].failed_id == "002"
    #   - plan_mutations_applied == ["002"]  (persisted to plan)
    #   - run_log_appended == []  (never appended)
    #   - Plan on disk: 002 Status = "blocked"
    #   - run-log has NO `blocked` events
    # Locks the contract: plan is source-of-truth; log failure is observability
    # gap; on re-entry the orchestrator reads "blocked" from plan and proceeds.
```

**V14 — No-dependent success: empty cascade is exit 0 with no plan I/O.**

```python
def test_block_dependents_no_dependents_no_plan_io(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002 and 003 both INDEPENDENT (neither depends
    # on 001; neither on the other). BFS returns [].
    # Instrument _load_text and _write_text to count calls.
    # Assert:
    #   - Exits 0
    #   - blocked_task_ids == []
    #   - plan_mutations_applied == []
    #   - run_log_appended == []
    #   - _load_text NOT called; _write_text NOT called (plan is untouched)
    #   - No `blocked` run-log events appended
```

**V15 — Double-failure precedence: deferred `plan_mutate` + `plan_write` → mutate primary.**

```python
def test_block_dependents_double_failure_mutate_then_write(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001, 003→002, 004→003 (three in cascade).
    # Monkeypatch mutate_task_status to raise ValueError on id "004" only
    #   → mutate_failure captured for 004 with 002, 003 successfully flipped
    #     in memory.
    # Monkeypatch _write_text to raise OSError("write refused").
    # Assert acceptance #5 double-failure precedence:
    #   - Exits NON-ZERO (_die)
    #   - errors[0].failed_stage == "plan_mutate"  (PRIMARY)
    #   - errors[0].failed_id == "004"
    #   - errors[0].secondary_failed_stage == "plan_write"  (SECONDARY)
    #   - errors[0].secondary_failed_id is None  (plan_write is an atomic
    #     batch; no single id owns the write failure)
    #   - errors[0].secondary_error contains "write refused"
    #   - plan_mutations_applied == []  (write never persisted)
    #   - run_log_appended == []        (log loop never reached)
    #   - Plan file on disk: unchanged (write failed)
```

**V16 — Double-failure precedence: deferred `plan_mutate` + `run_log_append` → mutate primary.**

```python
def test_block_dependents_double_failure_mutate_then_log(
        tmp_path, isolated_plan, monkeypatch):
    # Schedule: 001 failed, 002→001, 003→002, 004→003 (three in cascade).
    # Monkeypatch mutate_task_status to raise ValueError on id "004" only
    #   → mutate_failure captured; 002, 003 flipped in memory.
    # Plan write succeeds; log append for 002 succeeds; log append for 003
    # raises (monkeypatch: count calls, raise on second).
    # Assert acceptance #5 double-failure precedence:
    #   - Exits NON-ZERO (_die)
    #   - errors[0].failed_stage == "plan_mutate"  (PRIMARY — the EARLIER failure)
    #   - errors[0].failed_id == "004"
    #   - errors[0].secondary_failed_stage == "run_log_append"
    #   - errors[0].secondary_failed_id == "003"
    #   - plan_mutations_applied == ["002","003"]  (flipped + written)
    #   - run_log_appended == ["002"]              (truncates at last success)
    #   - Plan file on disk: 002 and 003 both "blocked"
    #   - run-log has ONE `blocked` event for 002 only
    # Locks in acceptance #5 contract: root-cause mutate failure stays primary;
    # downstream log failure is a secondary diagnostic.
```

---

## Tasks

### TASK-004D: Mutate plan markdown in `block-dependents`

- **Status:** pending
- **Priority:** high
- **Files:**
  - `scripts/plan_ops.py` (rewrite `cmd_block_dependents`; add `--plan-file` argparse entry; update module-level usage banner near line 14)
  - `tests/scripts/test_plan_ops.py` (replace/extend `TestBlockDependents`; current coverage at `tests/scripts/test_plan_ops.py:629-647` is one happy-path test only — V1 through V16)
  - `.claude/skills/implement-plan/SKILL.md` (update THREE callsites: CLI reference table row near line 44, Phase C command near line 201, Phase D.4 reference near line 287 — all to include `--plan-file <abs>`)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestBlockDependents`
- **Acceptance criteria:** all sixteen above (V1–V16).
- **Out of scope:** see top-of-plan list.

**Description:**
Make `block-dependents` write the cascade into plan markdown, not just the run-log. The helper `mutate_task_status` exists and already supports `"blocked"` as a status. The run-log append already has tail-verified fsync semantics. This task is about ordering (mutate plan, then log), I/O discipline (single plan read, single plan write), and CLI surface (adding `--plan-file`).

**Implementation sketch:**

```python
def cmd_block_dependents(args: argparse.Namespace) -> None:
    sched_path = Path(args.schedule_file)
    plan_path = Path(args.plan_file)
    if not sched_path.is_file():
        _die(args, {"error": f"schedule file not found: {sched_path}"})
    if not plan_path.is_file():
        _die(args, {"error": f"plan file not found: {plan_path}"})
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(args, {"error": f"schedule json decode: {e}"})

    failed_id = _normalize_task_id(args.failed)
    if not failed_id:
        _die(args, {"error": f"cannot normalize --failed: {args.failed!r}"})

    # ---- PHASE 1: compute cascade (BFS; sibling order = tasks[] order) ----
    blocked: list[str] = []
    queue = [failed_id]
    seen = set(queue)
    tasks = data.get("tasks") or []
    while queue:
        cur = queue.pop(0)
        for t in tasks:
            raw_tid = t.get("id") if "id" in t else t.get("task_id")
            tid = _normalize_task_id(str(raw_tid))
            if not tid or tid in seen:
                continue
            deps = [_normalize_task_id(str(d)) for d in (t.get("dependencies") or [])]
            if cur in deps:
                blocked.append(tid)
                seen.add(tid)
                queue.append(tid)

    # ---- Empty cascade: no plan I/O at all. ----
    if not blocked:
        _emit(args, {
            "blocked_task_ids": [],
            "plan_mutations_applied": [],
            "run_log_appended": [],
        })
        return

    plan_mutations_applied: list[str] = []
    run_log_appended: list[str] = []
    remaining: list[str] = list(blocked)

    # ---- PHASE 2a: single plan read. ----
    try:
        original = _load_text(plan_path)
    except OSError as e:
        _die(args, {"errors": [{
            "failed_stage": "plan_read",
            "failed_id": None,
            "error": str(e),
            "plan_mutations_applied": [],
            "run_log_appended": [],
            "remaining": list(remaining),
        }]})

    # ---- PHASE 2b: in-memory mutate loop. Track ValueError but do NOT die yet. ----
    mutated_text = original
    mutated_in_memory: list[str] = []
    mutate_failure: dict | None = None
    for idx, bid in enumerate(remaining):
        try:
            mutated_text, _ = mutate_task_status(mutated_text, bid, "blocked")
        except ValueError as e:
            mutate_failure = {
                "failed_stage": "plan_mutate",
                "failed_id": bid,
                "error": str(e),
                "remaining": list(remaining[idx + 1:]),
            }
            break
        mutated_in_memory.append(bid)

    # ---- PHASE 2c: if NO id flipped, die now (nothing to persist or log). ----
    if not mutated_in_memory:
        assert mutate_failure is not None
        mutate_failure["plan_mutations_applied"] = []
        mutate_failure["run_log_appended"] = []
        _die(args, {"errors": [mutate_failure]})

    # ---- PHASE 2d: single plan write (at least one in-memory success). ----
    try:
        _write_text(plan_path, mutated_text)
    except OSError as e:
        # Write failed → nothing persisted. Per acceptance #5, a pending
        # mutate_failure is ALWAYS primary and the plan_write failure becomes
        # secondary. Otherwise plan_write is primary.
        if mutate_failure is not None:
            payload: dict = dict(mutate_failure)
            payload["plan_mutations_applied"] = []
            payload["run_log_appended"] = []
            payload["secondary_failed_stage"] = "plan_write"
            payload["secondary_failed_id"] = None
            payload["secondary_error"] = str(e)
        else:
            payload = {
                "failed_stage": "plan_write",
                "failed_id": None,
                "error": str(e),
                "plan_mutations_applied": [],
                "run_log_appended": [],
                "remaining": [],
            }
        _die(args, {"errors": [payload]})
    plan_mutations_applied = list(mutated_in_memory)

    # ---- PHASE 3: run-log append for every id persisted to plan. ----
    log_failure: dict | None = None
    for bid in plan_mutations_applied:
        try:
            _append_run_log("blocked", {
                "run_id": args.run_id,
                "task_id": bid,
                "blocker_task_id": failed_id,
                "reason": f"dependency TASK-{failed_id} failed",
            })
        except Exception as e:  # _append_run_log raises on tail-verify failure
            log_failure = {
                "failed_stage": "run_log_append",
                "failed_id": bid,
                "error": str(e),
            }
            break
        run_log_appended.append(bid)

    # ---- PHASE 4: decide primary vs secondary stage for any pending failures. ----
    # Per acceptance #5 double-failure precedence: if a DEFERRED plan_mutate
    # failure is pending, it is ALWAYS primary. A concurrent run_log_append
    # failure is secondary. run_log_appended truncates at the last success.
    if mutate_failure is not None:
        mutate_failure["plan_mutations_applied"] = list(plan_mutations_applied)
        mutate_failure["run_log_appended"] = list(run_log_appended)
        if log_failure is not None:
            mutate_failure["secondary_failed_stage"] = "run_log_append"
            mutate_failure["secondary_failed_id"] = log_failure["failed_id"]
        _die(args, {"errors": [mutate_failure]})

    if log_failure is not None:
        log_failure["plan_mutations_applied"] = list(plan_mutations_applied)
        log_failure["run_log_appended"] = list(run_log_appended)
        log_failure["remaining"] = []
        _die(args, {"errors": [log_failure]})

    # ---- Success. ----
    _emit(args, {
        "blocked_task_ids": blocked,
        "plan_mutations_applied": plan_mutations_applied,
        "run_log_appended": run_log_appended,
    })
```

**Failure-stage reference table** (for test-writers and reviewers):

| Stage | Trigger | `plan_mutations_applied` | `run_log_appended` | Plan on disk |
|---|---|---|---|---|
| `plan_read` | `_load_text` raises `OSError` | `[]` | `[]` | unchanged |
| `plan_mutate` (ALL ids raise) | first id raises `ValueError` | `[]` | `[]` | unchanged |
| `plan_mutate` (partial) | id N raises after N-1 successes | `[ids 0..N-1]` | `[ids 0..N-1]` | ids 0..N-1 flipped |
| `plan_write` | `_write_text` raises `OSError` | `[]` | `[]` | unchanged |
| `run_log_append` | log N raises after N-1 successes | `[all ids]` | `[ids 0..N-1]` | all ids flipped |

Argparse update (in the same file, where block-dependents subparser is defined):

```python
p_block = sub.add_parser("block-dependents", help="...")
p_block.add_argument("--schedule-file", required=True)
p_block.add_argument("--plan-file", required=True)   # NEW
p_block.add_argument("--failed", required=True)
p_block.add_argument("--run-id", required=True)
# ... --json flag preserved
```

**SKILL.md update (surgical):**

Phase C block, current:
```bash
venv/bin/python scripts/plan_ops.py block-dependents \
  --schedule-file <path> --failed NNN --run-id <id> --json
```

Change to:
```bash
venv/bin/python scripts/plan_ops.py block-dependents \
  --schedule-file <path> --plan-file <abs> --failed NNN --run-id <id> --json
```

Same update in Phase D.4 (same subcommand invocation).

**Forbidden patterns:**

- Calling `mutate_task_status` inside the BFS loop (Phase 1) — that couples cascade traversal to plan mutation and makes the failure mode in V6 incoherent.
- Writing the plan file inside the per-id loop — wastes I/O and makes partial-failure recovery ambiguous.
- Appending the run-log entry BEFORE the plan mutation for that id — violates acceptance #2 (mutate-then-log ordering).
- Using `git stash` or any git command inside this subcommand — no git interaction at all.
- Adding auto-retry on `mutate_task_status` failure — fail-fast per V5/V6.
- Silencing the ValueError from `mutate_task_status` (e.g., `try/except ValueError: pass`) — drops the cascade on the floor.

**Test scaffolding notes:**

- Use the existing `isolated_plan` fixture at the top of `tests/scripts/test_plan_ops.py` as the template. The parent class `TestBlockDependents` already exists (one test) — extend it with V1–V9 rather than replacing it.
- Use `monkeypatch.setattr(plan_ops, "_load_text", ...)` and `_write_text` to count I/O in V2.
- Use `_parse_json(cp)` helper (already in the test file) for stdout parsing.
- Reset `plan_ops.RUN_LOG_PATH` per-test via the existing fixture (check `tests/scripts/test_plan_ops.py` top-of-file for the pattern — `isolated_plan` fixture likely handles it).
