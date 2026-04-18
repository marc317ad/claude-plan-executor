# TASK-004A — `filter-schedule` subcommand

**Parent plan:** [`TASK-004_scheduler_semantics.md`](TASK-004_scheduler_semantics.md) (superseded — split into A/B/C/D/E)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9.1-§9.6
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract), TASK-002 (`write-schedule` subcommand + `_validate_schedule` / `_validate_schedule_dag` helpers).
**Issues absorbed:** ISSUE-006 (P1, primary), ISSUE-020 (P2, secondary), ISSUE-019 (P2, secondary — DAG defensive check on this consumer).

---

## Goal

Add `plan_ops.py filter-schedule --schedule-file <path> --task-ids 1,3 --json`. Produces a new schedule containing the requested tasks plus their transitive prerequisites. Success stdout is **itself a complete, valid schedule that `write-schedule --stdin` accepts byte-for-byte without any reshaping**, so the orchestrator can pipe `filter-schedule | write-schedule` directly.

`filter-schedule` does NOT write any file itself. It only emits the filtered JSON to stdout. The orchestrator owns persistence by piping to `write-schedule`.

## Scoped Context

### ISSUE-006 (P1) — `--task-ids` flag documented but not implemented

- **Docs:** `plugins/plan-executor/skills/implement-plan/SKILL.md:64, 72, 111-113` specify the flag and its orphan-prerequisite behavior.
- **Helpers:** `plugins/plan-executor/scripts/plan_ops.py` has 13 subcommands, none of which filters a schedule by task ids.
- **Rule:** `plugins/plan-executor/skills/implement-plan/SKILL.md:316` forbids inline Python in the orchestrator, so the only path is a helper subcommand.
- **Fix:** add `plan_ops.py filter-schedule --schedule-file <path> --task-ids 1,3 --json`. Produces a new schedule containing the requested tasks plus their transitive prerequisites. Two distinct failure modes (see below).

### ISSUE-020 (P2, secondary) — `write-schedule` subcommand

Primary delivery in TASK-002. TASK-004A depends on it: `filter-schedule` returns the filtered JSON on stdout for the orchestrator to pipe to `write-schedule`. Keeps `filter-schedule` single-responsibility and makes the orchestrator composition obvious in `SKILL.md`.

### ISSUE-019 (P2, secondary) — defensive DAG check on this consumer

Primary fix lives in TASK-002 (`parse-schedule` now validates the DAG). TASK-004A adds the same defensive validation to `filter-schedule` (cycle in the filtered subgraph → halt).

### Hard-won regression coverage from prior runs

Two prior runs of the unsplit TASK-004 failed at this surface area:

- **Run `20260415T000811`** — `filter-schedule` success stdout included `warnings` and `errors` keys that `ALLOWED_SCHEDULE_TOP_LEVEL` rejects. The pipe `filter-schedule | write-schedule` failed because `write-schedule` rejected the unknown top-level keys. The implementer also wrote `test_filter_schedule_pipes_to_write_schedule` that *masked* the bug by Python-stripping the offending keys before piping.
- **Codex review of TASK-004A draft (2026-04-15)** — caught two more cousin bugs in the original implementation sketch:
  - Hard-coding `outcome="valid"` while copying `gaps` from source can produce output that fails `_validate_schedule` (rule at `plugins/plan-executor/scripts/plan_ops.py:239-244`: `outcome="valid"` requires `gaps=[]`).
  - Iterating the dependency stack with `tasks_by_id[tid]` indexing crashes with `KeyError` when a transitive dep is missing from `tasks[]`. Two distinct failure modes were conflated under "orphan".

V11 (canonical-only stdout) and V12 (full write-schedule round-trip) below are the regression coverage. Both are hard ship-blockers.

### Two distinct failure modes (resolves Codex CRITICAL #2)

`filter-schedule` MUST distinguish these and report each separately:

1. **Unknown requested ID** — a value passed via `--task-ids` is not present in `sched["tasks"]`. Error code: `unknown-task-id`. Message: `unknown task id NNN`.
2. **Broken schedule (transitive dep missing)** — during transitive closure, a task's `dependencies[]` references an ID that is not in `sched["tasks"]`. This is rare (parse-schedule should have caught it) but `filter-schedule` MUST guard against it explicitly rather than crashing with `KeyError`. Error code: `missing-dependency`. Message: `task NNN depends on missing id MMM`.

The user-facing distinction: case 1 is "user typo"; case 2 is "schedule is broken upstream".

### Output contract (resolves Codex CRITICAL #3)

The success-path output is a **fresh canonical schedule built from the filtered subgraph**. Specifically:

- `outcome` — always `"valid"` on success.
- `tasks` — filtered task objects, source order preserved.
- `batches` — filtered batches, source order preserved; batches whose `task_ids` becomes empty are dropped.
- `gaps` — always `[]` (a filtered subgraph cannot inherit source-level gaps without re-evaluation; emitting them would either contradict `outcome="valid"` per `_validate_schedule` or invite stale references).
- `risks` — always `[]` (same reasoning; source risks may reference filtered-out tasks).

`filter-schedule` MUST refuse to operate on a source schedule whose own `outcome != "valid"` — it is meaningless to filter an already-broken schedule. Error code: `source-not-valid`.

This contract guarantees the success-path output is byte-for-byte acceptable to `write-schedule --stdin` (V11, V12).

### Error shape unification (resolves Codex IMPORTANT #4)

Use the structured `errors` list shape that `cmd_write_schedule` and `cmd_batch_next` already use (`plugins/plan-executor/scripts/plan_ops.py:1004-1013, 1037-1046`):

```json
{"errors": [{"path": "$.task_ids", "code": "unknown-task-id", "message": "unknown task id 999"}]}
```

NOT the loose `{"error": "..."}` shape. `_die` will serialize whatever dict you pass; pick `errors` and stay consistent so existing test patterns and reviewers can pattern-match.

---

## Verification

**V1 — `filter-schedule` happy path.**

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":["a"],"dependencies":[]},{"id":"002","agent":"claude","files":["b"],"dependencies":["001"]},{"id":"003","agent":"codex","files":["c"],"dependencies":[]}],"batches":[{"index":1,"task_ids":["001","003"],"file_locks":["a","c"]},{"index":2,"task_ids":["002"],"file_locks":["b"]}]}' \
  > /tmp/full.schedule.json

venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/full.schedule.json --task-ids 2 --json
```

Must return a schedule containing `tasks = [001, 002]` (002 requires 001) and appropriate batches. Exit 0. Stdout JSON keys exactly `{"outcome","tasks","batches","gaps","risks"}`. `gaps` and `risks` are both `[]` regardless of source.

**V2 — Unknown requested ID.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/full.schedule.json --task-ids 999 --json
```

Exit 1, stdout has `errors[*].code == "unknown-task-id"`, message includes `unknown task id 999`.

**V3 — Broken schedule (missing transitive dep).**

```bash
echo '{"outcome":"valid","tasks":[{"id":"002","agent":"claude","files":["b"],"dependencies":["001"]}],"batches":[{"index":1,"task_ids":["002"],"file_locks":["b"]}]}' \
  > /tmp/broken.schedule.json

venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/broken.schedule.json --task-ids 2 --json
```

Exit 1, stdout has `errors[*].code == "missing-dependency"`, message names both ids (e.g., `task 002 depends on missing id 001`). MUST NOT raise `KeyError` from Python.

**V4 — Source-not-valid rejection.**

```bash
echo '{"outcome":"needs-enrichment","tasks":[{"id":"001","agent":"codex","files":["a"],"dependencies":[]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":["a"]}],"gaps":[{"id":"G1","description":"x"}]}' \
  > /tmp/needs_enr.schedule.json

venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/needs_enr.schedule.json --task-ids 1 --json
```

Exit 1, stdout has `errors[*].code == "source-not-valid"`.

**V5 — Source schedule fails validator.**

If the source schedule itself fails `_validate_schedule` (duplicate IDs, malformed batches, etc.), `filter-schedule` MUST run `_validate_schedule` upfront and exit 1 with the validator's `errors` list — same pattern as `cmd_batch_next` (`plugins/plan-executor/scripts/plan_ops.py:1044-1046`). Do not attempt to filter an invalid schedule.

**V5a — Malformed source: missing file.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/does_not_exist.json --task-ids 1 --json
```

Exit 1, `errors[0].code == "file-not-found"`.

**V5b — Malformed source: invalid JSON.**

```bash
echo '{not json' > /tmp/bad.json
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/bad.json --task-ids 1 --json
```

Exit 1, `errors[0].code == "json-decode"`.

**V5c — Malformed source: top-level not an object.**

```bash
echo '[]' > /tmp/list.json
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule --schedule-file /tmp/list.json --task-ids 1 --json
```

Exit 1, `errors[0].code == "top-level-not-object"`.

**V6 — `filter-schedule` surfaces cycles in the filtered subgraph.**

Feed a cycle JSON (001 → 002 → 001) through `filter-schedule --task-ids 1`; halts with `errors[*].code == "dependency-cycle"`.

**V7 — `--task-ids` parsing.** Accept canonical `001`, plain `1`, and `TASK-001` forms; normalize via `_normalize_task_id`. Empty string entries (e.g. `--task-ids 1,,3`) skipped silently. All-empty input → `errors[*].code == "invalid-task-ids"`.

**V8 — Batches with empty post-filter `task_ids` are dropped.** A source batch whose tasks are all filtered out MUST NOT appear in output `batches[]`.

**V9 — Batch ordering and `index` preservation.** Output `batches` are in source order with their original `index` values. (No renumbering — the orchestrator's batch-next semantics depend on the ordering, not on contiguous index numbering.)

**V10 — `task_ids` in retained batches are filtered to the closed set.** A batch that had `[001, 003]` and the filter retains only `001` MUST emit `task_ids=["001"]` for that batch (not `["001","003"]`).

**V11 — Success stdout has ONLY canonical keys.** *(Hard-won regression from run `20260415T000811`.)*

A pytest case MUST run `filter-schedule` and assert `set(json.loads(stdout).keys()) == {"outcome","tasks","batches","gaps","risks"}`. No `warnings`, no `errors`, no other keys on success.

**V12 — `filter-schedule` output is directly pipeable to `write-schedule` AND survives full round-trip.** *(Hard-won regression from run `20260415T000811` + Codex review IMPORTANT #5.)*

The required test must use a literal shell pipe with **no Python-side reshaping**:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py filter-schedule \
  --schedule-file /tmp/full.schedule.json --task-ids 2 --json \
| venv/bin/python plugins/plan-executor/scripts/plan_ops.py write-schedule \
  --schedule-file /tmp/filtered.schedule.json --stdin --json
```

The `write-schedule` invocation MUST exit 0. The written file MUST then parse back through `parse-schedule` cleanly: `outcome=valid`, no errors, no warnings about unknown top-level fields. The corresponding pytest test must invoke `subprocess.run(... shell=True)` (or equivalent two-`Popen` pipe) and only inspect the final exit code + the written file — never reshape stdout in Python before re-piping.

**Forbidden test pattern (rejection criterion):** any test that reshapes `filter-schedule` stdout in Python before piping (e.g., `data = json.loads(out); data.pop("warnings"); data.pop("errors"); pipe data`). That pattern hides the bug instead of catching it; a previous run shipped exactly such a test. The reviewer will reject any retest of the canonical pipe that does not use a literal shell pipe (or `Popen | Popen`).

**V13 — Round-trip drops source `risks`.** The validator requires `gaps=[]` whenever `outcome="valid"` (`plugins/plan-executor/scripts/plan_ops.py:239-244`), so the only source-side metadata that can survive is `risks`. The test fixture: source schedule with `outcome="valid"`, `gaps=[]`, **non-empty `risks`**. Run `filter-schedule | write-schedule | parse-schedule` as a literal shell pipe (no Python reshaping). Pipeline MUST exit 0 throughout. The persisted file MUST have `risks=[]` regardless of the source's `risks` content. This is the test that catches the Codex CRITICAL #3 outcome/gaps bug — if the implementer copies `risks` from source instead of emitting `[]`, the pipe still works but the test fails on the persisted-file content assertion. (The test name should reflect `risks`, not `gaps`; the earlier draft had a stale title that suggested `gaps` was the surviving field — it isn't, because canonical-success requires `gaps=[]`.)

---

## Tasks

### TASK-004A: Implement `filter-schedule` subcommand

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - `filter-schedule` subcommand exists, filters by `--task-ids`, computes transitive prerequisites, halts on the two distinct failure modes (unknown requested id; broken schedule with missing transitive dep) with separate error codes.
  - **Refuses to operate on a source schedule whose own `outcome != "valid"`.** Validates the source schedule via `_validate_schedule` upfront — same pattern as `cmd_batch_next`.
  - **Success stdout is itself a schedule that `write-schedule --stdin` accepts byte-for-byte without any Python-side reshaping.** Specifically: keys exactly `{"outcome","tasks","batches","gaps","risks"}`; `outcome="valid"`; `gaps=[]`; `risks=[]`; tasks/batches in source order; batches with empty post-filter `task_ids` dropped. **(See V11 + V12 + V13.)**
  - DAG defensive check present in the filtered subgraph (cycle → halt with `dependency-cycle`).
  - Error shape uses the structured `errors[*]` list (same shape as `cmd_write_schedule`), NOT the loose `{"error": "..."}` shape.
  - All verification checks V1–V13 pass.
- **Out of scope (handled by sibling sub-plans):**
  - `batch-next` batch fidelity → TASK-004B
  - `fail-task` untracked cleanup → TASK-004C
  - `block-dependents` plan-markdown mutation → TASK-004D
  - `acquire-lock` strict shape → TASK-004E

**Description:**
Add the missing `filter-schedule` subcommand so the orchestrator's `--task-ids` path actually works. Output is byte-for-byte compatible with `write-schedule --stdin` so the orchestrator can compose them with a literal shell pipe.

**Implementation notes:**
Defensive DAG validation should exist downstream even if the analyst already does it upstream. Reuse `_validate_schedule_dag` from TASK-002. Use the same source-validate-then-filter pattern as `cmd_batch_next` (`plugins/plan-executor/scripts/plan_ops.py:1029-1046`).

**Reversion guidance:**
Safe to revert; `--task-ids` goes back to being documented-but-unimplemented. Only `SKILL.md` needs a compensating edit.

---

## Implementation Playbook

### Step 1 — Read source, validate, then filter (mirror `cmd_batch_next`)

**Convention note:** the existing codebase inlines the read/parse/type-check pattern in every consumer (`cmd_batch_next:1029-1046`, `cmd_write_schedule:994-1013`). This sub-plan deliberately follows that convention rather than introducing a `_load_json` helper — extracting that helper is a sibling refactor that would touch every consumer at once and is out of scope here. Mirror the existing inline pattern verbatim. **All `t["id"]` / `t["task_id"]` access MUST use the alias-tolerant pattern** `t.get("id") if "id" in t else t.get("task_id")` (used at `plugins/plan-executor/scripts/plan_ops.py:66, 129, 169, 284, 1057, 1091`). Direct `t["id"]` indexing will `KeyError` on a schedule that uses the legacy `task_id` alias even after `_validate_schedule` passes (the validator warns but does not normalize).

The `cmd_filter_schedule` body MUST follow this order:

```python
def cmd_filter_schedule(args: argparse.Namespace) -> None:
    sched_path = Path(args.schedule_file)
    if not sched_path.is_file():
        _die(args, {"errors": [{"path": "$", "code": "file-not-found",
                                "message": f"schedule file not found: {sched_path}"}]})
    try:
        data = json.loads(sched_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(args, {"errors": [{"path": "$", "code": "json-decode",
                                "message": f"schedule json decode: {e}"}]})
    if not isinstance(data, dict):
        _die(args, {"errors": [{"path": "$", "code": "top-level-not-object",
                                "message": "top-level schedule must be an object"}]})

    # 1. Source-schedule validation (mirror cmd_batch_next:1044-1046)
    errors, warnings = _validate_schedule(data)
    if errors:
        _die(args, {"errors": errors, "warnings": warnings})

    # 2. Source-not-valid rejection (V4)
    if data.get("outcome") != "valid":
        _die(args, {"errors": [{"path": "$.outcome", "code": "source-not-valid",
                                "message": f"filter-schedule requires source outcome='valid', got {data.get('outcome')!r}"}]})

    # 3. --task-ids parse + normalize
    raw_ids = [s.strip() for s in (args.task_ids or "").split(",") if s.strip()]
    requested = []
    for r in raw_ids:
        norm = _normalize_task_id(r)
        if norm is None:
            _die(args, {"errors": [{"path": "$.task_ids", "code": "invalid-task-ids",
                                    "message": f"could not normalize task id {r!r}"}]})
        requested.append(norm)
    if not requested:
        _die(args, {"errors": [{"path": "$.task_ids", "code": "invalid-task-ids",
                                "message": "no task ids provided"}]})

    tasks_by_id: dict[str, dict] = {}
    for t in data["tasks"]:
        raw_tid = t.get("id") if "id" in t else t.get("task_id")
        norm = _normalize_task_id(str(raw_tid)) if raw_tid is not None else None
        if norm:
            tasks_by_id[norm] = t

    # 4. Unknown requested id (V2)
    unknown = [tid for tid in requested if tid not in tasks_by_id]
    if unknown:
        _die(args, {"errors": [{"path": "$.task_ids", "code": "unknown-task-id",
                                "message": f"unknown task id {tid}"} for tid in unknown]})

    # 5. Transitive closure WITH guarded indexing (V3)
    closed: set[str] = set()
    stack = list(requested)
    while stack:
        tid = stack.pop()
        if tid in closed:
            continue
        closed.add(tid)
        task = tasks_by_id.get(tid)
        if task is None:
            # Should not happen if step 4 passed, but defensive.
            _die(args, {"errors": [{"path": "$.tasks", "code": "missing-dependency",
                                    "message": f"task depends on missing id {tid}"}]})
        for dep in (task.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None or dep_norm not in tasks_by_id:
                _die(args, {"errors": [{"path": f"$.tasks[id={tid}].dependencies",
                                        "code": "missing-dependency",
                                        "message": f"task {tid} depends on missing id {dep!r}"}]})
            stack.append(dep_norm)

    # 6. Build output tasks/batches in source order
    def _tid_of(t: dict) -> str | None:
        raw = t.get("id") if "id" in t else t.get("task_id")
        return _normalize_task_id(str(raw)) if raw is not None else None
    out_tasks = [t for t in data["tasks"] if _tid_of(t) in closed]
    out_batches = []
    for b in (data.get("batches") or []):
        bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
        keep = [x for x in bids if x in closed]
        if keep:
            out_batches.append({**b, "task_ids": keep})

    # 7. DAG defensive check on filtered subgraph (V6)
    dag_errors = _validate_schedule_dag(out_tasks, out_batches)
    if dag_errors:
        _die(args, {"errors": dag_errors})

    # 8. Emit canonical schedule. gaps=[] and risks=[] are intentional —
    #    inheriting source-level gaps/risks would either contradict outcome=valid
    #    (per _validate_schedule:239-244) or carry stale references to filtered-out tasks.
    #    Success stdout MUST contain ONLY these five keys so write-schedule --stdin
    #    accepts the output byte-for-byte. Do NOT add warnings, errors, or any other
    #    metadata on the success path; if you need to surface informational warnings,
    #    write them to sys.stderr — never to stdout alongside the schedule.
    _emit(args, {
        "outcome": "valid",
        "tasks": out_tasks,
        "batches": out_batches,
        "gaps": [],
        "risks": [],
    })
```

### Step 2 — Register subparser

```python
parser_fs = subparsers.add_parser("filter-schedule", help="Filter schedule by --task-ids and emit canonical schedule on stdout")
parser_fs.add_argument("--schedule-file", required=True)
parser_fs.add_argument("--task-ids", required=True, help="CSV of task ids; canonical, plain, or TASK-NNN forms")
parser_fs.add_argument("--json", action="store_true")
parser_fs.set_defaults(func=cmd_filter_schedule)
```

### Step 3 — `SKILL.md` updates

Add to Phase A analyst step (Analysis section, around line 111-113): when `--task-ids` is set, the orchestrator pipes the analyst's schedule through `filter-schedule | write-schedule`. Use the literal pipe form. Keep wording minimal.

### Step 4 — Tests in `tests/scripts/test_plan_ops.py`

At minimum (one test per V):

- `test_filter_schedule_happy_path` — V1.
- `test_filter_schedule_transitive_prereqs` — request `[002]`, assert `[001, 002]` are returned and source order preserved.
- `test_filter_schedule_unknown_id_rejected` — V2; assert exit 1 and `errors[0].code == "unknown-task-id"`.
- `test_filter_schedule_missing_dep_rejected` — V3; assert exit 1, `errors[0].code == "missing-dependency"`, AND no `KeyError` traceback in stderr.
- `test_filter_schedule_source_not_valid_rejected` — V4; source has `outcome="needs-enrichment"`.
- `test_filter_schedule_invalid_source_rejected` — V5; source fails validator (e.g. duplicate IDs).
- `test_filter_schedule_missing_file_rejected` — V5a; assert exit 1, `errors[0].code == "file-not-found"`.
- `test_filter_schedule_malformed_json_rejected` — V5b; assert exit 1, `errors[0].code == "json-decode"`.
- `test_filter_schedule_top_level_not_object_rejected` — V5c; assert exit 1, `errors[0].code == "top-level-not-object"`.
- `test_filter_schedule_alias_task_id_field_supported` — source schedule uses legacy `task_id` instead of `id`; `filter-schedule` MUST process it without `KeyError` (matches the alias-tolerance pattern at `plugins/plan-executor/scripts/plan_ops.py:66, 1057`).
- `test_filter_schedule_cycle_rejected` — V6 cycle JSON; assert exit 1, `errors[0].code == "dependency-cycle"`.
- `test_filter_schedule_id_form_normalization` — V7; mix of `1`, `001`, `TASK-001`.
- `test_filter_schedule_drops_empty_batches` — V8; batch whose tasks are all filtered out is absent from output.
- `test_filter_schedule_preserves_batch_index` — V9; output batches have original `index` values, source order.
- `test_filter_schedule_filters_batch_task_ids` — V10; retained batch has only the kept ids.
- `test_filter_schedule_success_stdout_is_canonical_only` (V11) — runs `filter-schedule` and asserts the stdout JSON has exactly the keys `{"outcome","tasks","batches","gaps","risks"}`. No `warnings`, no `errors` on success.
- `test_filter_schedule_pipes_to_write_schedule_via_shell` (V12) — uses `subprocess.run(..., shell=True)` (or two `Popen` objects connected by `stdout=PIPE`) to perform a literal shell pipe `filter-schedule | write-schedule`, and asserts (a) the pipeline exits 0, (b) the resulting written file parses cleanly through `parse-schedule`. **Do NOT** read the `filter-schedule` JSON into Python and pop fields before piping; the whole point is to prove direct pipeability.
- `test_filter_schedule_full_round_trip_drops_source_risks` (V13) — source schedule has `outcome="valid"`, `gaps=[]`, **non-empty `risks`**. Run `filter-schedule | write-schedule | parse-schedule` shell pipeline. Assert exit 0 throughout AND assert the persisted file has `risks=[]` regardless of source. (Source-side `gaps` cannot be tested under `outcome="valid"` because the validator forbids non-empty gaps with that outcome.)

**Forbidden test patterns (rejection criteria):**

- Any test that reshapes `filter-schedule` stdout in Python before piping (e.g., `data = json.loads(out); data.pop("warnings"); data.pop("errors"); pipe data`). That pattern hides the bug. The reviewer will reject any retest of the canonical pipe that does not use a literal shell pipe (or `Popen | Popen`).
- Any test that tolerates a `KeyError` traceback from `filter-schedule` for the missing-dependency case. The implementation MUST guard the dict access explicitly and emit a structured error.

### Step 5 — Regression sweep

Run `venv/bin/pytest -q tests/scripts/test_plan_ops.py`. No prior tests should break.

---

## Out of Scope

- `batch-next` batch fidelity → TASK-004B
- `fail-task` untracked cleanup → TASK-004C
- `block-dependents` plan-markdown mutation → TASK-004D
- `acquire-lock` strict shape → TASK-004E
- Canonical contract (TASK-001), generic runtime validation (TASK-002), wrapper isolation (TASK-003), phase gates (TASK-005), fixture rewrite (TASK-006), self-audit (TASK-007), preflight (TASK-008), scale/locks/logs (TASK-009/010/011).

## Reversion guidance

Safe to revert; `--task-ids` goes back to being documented-but-unimplemented. Only `SKILL.md` needs a compensating edit.

## Execution log — 20260417T153553

Starting SHA: `7751d0f49ebd6a77144fb1a9c5b0498e0dfca816`  → Ending SHA: `7751d0f49ebd6a77144fb1a9c5b0498e0dfca816`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 004A | claude | codex+code-reviewer-d5 | needs-rework | — | reverted: SKILL.md:147 compute-schedule-after-filter conflicts with V9/V12 |
