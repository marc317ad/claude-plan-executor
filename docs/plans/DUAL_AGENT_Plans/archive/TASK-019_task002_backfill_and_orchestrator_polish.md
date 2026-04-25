# TASK-019 — Backfill TASK-002 V3/V4 and close orchestrator paper-cuts from run `20260418T015713`

**Base branch:** `main`
**Audit anchor commit:** `4b96f89`
**Chunk dependencies:** TASK-002 (partially landed; this task closes its V3/V4 gap), TASK-004A (added inline cycle-detection to `cmd_filter_schedule`), TASK-004B (added inline cycle-detection to `cmd_batch_next`).
**Motivating run:** `20260418T015713` — TASK-004B execution surfaced six paper-cuts: (1) TASK-002's `_validate_schedule_dag` helper was never factored out and cycle/orphan-dep checks are missing from `parse-schedule`; (2–5) four orchestrator friction points hit during the run (commit-task reviewer flip, minor-findings schema rigidity, update-plan-header brittleness, finalize-execution-log row-schema undocumented); (6) TASK-002's plan status was marked `done` despite V3 and V4 acceptance criteria being empirically unmet.

---

## Goal

Close the TASK-002 acceptance-criteria gap (V3 cycle-in-`parse-schedule` and V4 orphan-dep) by factoring the Kahn's-algorithm + orphan-dep logic into a single `_validate_schedule_dag` helper called from all three consumers (`cmd_parse_schedule`, `cmd_batch_next`, `cmd_filter_schedule`). In the same pass, land four low-risk orchestrator polishes that came out of the TASK-004B run log: explicit D.2a commit-task guidance, optional `disposition` field on reviewer-minor-findings, graceful plan-header fallback, and a discoverable execution-log row schema.

The cycle-detection behavior itself does not change — the two inline Kahn's blocks emitted by TASK-004A and TASK-004B already satisfy `errors[*].code == "dependency-cycle"` at their respective call sites. This task consolidates them into one helper and extends the check to `cmd_parse_schedule`, which is the handoff point where cycles should have been caught in the first place (per TASK-002's original Step 1 playbook).

---

## Scoped Context

### Why TASK-002 slipped through

TASK-002's plan file carries `**Status:** done` but three independent checks show the key acceptance criteria for V3 (cycle rejection in `parse-schedule`) and V4 (orphan-dependency rejection) were never implemented:

1. **No `feat(TASK-002)` commit exists.** `git log --all --grep "TASK-002"` returns only `c0ea786` (this run's TASK-004B commit, which merely references TASK-002 in prose). There is no matching commit in the repo.
2. **No §5 Execution Log was appended to the plan file.** The plan ends at `## Reversion guidance`; no `finalize-execution-log` was ever called.
3. **V3 and V4 fail against the current code.** Running the literal V3 and V4 bash snippets from the plan's Verification section returns `exit=0 errors=[]` — cycles and orphan deps pass through `parse-schedule` silently.

What DOES exist in production code:
- `_validate_schedule` + `_validate_schedule_refs` (duplicate ids, unknown refs, batch-index uniqueness, batch file-scope disjointness — covers V2 and partial V3 but not cycles/orphans).
- `cmd_write_schedule` subcommand (V5, V6) ✓.
- `parse-implementer-report` diagnostic for missing `**Plan adaptations:**` section (V7) ✓.
- SKILL.md documents `write-schedule` as the persistence path (V8) ✓.

So TASK-002 is ~60% done: the `write-schedule` subcommand and implementer-report diagnostics landed, and most of `_validate_schedule_refs` landed. The **cycle** and **orphan-dependency** halves were never implemented, and the Implementation-Playbook-Step-1 refactor (factor out `_validate_schedule_dag`, wire from `parse-schedule` AND `batch-next`) was also never done. When TASK-004A later needed cycle detection, it inlined Kahn's algorithm into `cmd_filter_schedule`; TASK-004B did the same to `cmd_batch_next`. The result today: two near-duplicate Kahn's blocks in two functions, neither of which is the one the acceptance criteria said should hold the check.

**Why the status marker is wrong:**

- The `**Status:** done` line was hand-edited, not set by `plan_ops.py commit-task`. `commit-task` is the only path that atomically runs `git commit --only`, appends `commit_done` to the run log, and maintains the invariant that "done" implies "reviewer-gated + tests-green + committed". Hand-edited status markers bypass every one of those checks.
- Hand-editing also bypasses the reviewer gate. A reviewer looking at a PR containing Step-1 (the factor-out refactor) would have noticed the helper wasn't created. Without a `commit-task` invocation, no reviewer ever looked at this handoff.
- Nothing in the V1–V8 acceptance criteria is self-verifying at runtime. Plan-executor does not re-run acceptance-criteria V-checks at commit time; it trusts the implementer's self-report. So an implementer who reported "all V checks pass" — even if they'd only run the tests for V2 and V5-V8 — would be taken at face value. Empirically, `parse-schedule` today fails V3 and V4 but the implementer report recorded "pass".

This is a process failure, not a code failure. TASK-019 fixes the code gap but the deeper lesson belongs in a future TASK-020-class change: `commit-task` could optionally run a plan-supplied "V-check" shell script and halt if any V fails. Out of scope here — flagged in the reversion/follow-up notes below.

### Orchestrator paper-cuts from run `20260418T015713`

Each of the following surfaced during the commit + finalize phases of the TASK-004B run and added ~5–10 minutes of retry work. All are small, local, and non-behavior-changing.

#### (a) `commit-task` reviewer flip after D.2a disagreement is undocumented

In the D.2a disagreement path (Codex `needs-rework` → D.5 `ship`/`ship-with-fixes`), the binding verdict is D.5's, not Codex's. `commit-task`'s validator rejects `--reviewer codex --reviewer-verdict needs-rework` with `uncommittable-reviewer-verdict` (correct behavior — Codex never committed anything), but SKILL.md §D.3 doesn't state explicitly that the orchestrator must pass `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` instead. The orchestrator has to infer this from the argparse error.

**Evidence:** `_run_log.jsonl` entry at `2026-04-18T02:11:...` shows the orchestrator tried `--reviewer codex --reviewer-verdict needs-rework` first and got rejected.

**Fix:** add a one-paragraph note under SKILL.md §D.3 naming the reviewer/verdict pair for each disagreement outcome.

#### (b) `reviewer-minor-findings` schema rejects `disposition` field

The validator at `plugins/plan-executor/scripts/plan_ops.py:504-553` hard-rejects unknown keys. When D.5 dismisses a Codex finding, there's no structured place to record "D.5 adjudication: dismissed, reason: X" on the finding itself — the orchestrator has to fold the rationale into `issue`/`suggested_fix` prose. This is functional but not discoverable from the committed finding later.

**Fix:** add an OPTIONAL `disposition` field to the allowed-keys set; values constrained to `{null, "dismissed", "accepted", "deferred"}`; when present, also allow an optional `disposition_reason: str`. No semantic change when the field is absent (all existing callers continue to work unchanged).

#### (c) `update-plan-header --status complete` errors on plans without a top-level `**Status:**` line

The skill's End-of-run Step 1 invokes `update-plan-header` unconditionally. The plan file this run was executing (`TASK-004B_batch_next.md`) has no top-level `**Status:**` bullet — only the per-task `- **Status:** pending` line. `update-plan-header` returns `{"error": "plan header has no **Status:** line"}` and exits 1.

**Fix:** in `cmd_update_plan_header`, when the plan file has no top-level `**Status:**` bullet, emit `{"status": "absent", "warning": "no plan-level Status line to update; skipping"}` and exit 0 rather than erroring. The per-task status (updated by `commit-task`) is the authoritative signal anyway.

#### (d) `finalize-execution-log --rows-json` schema is undocumented

The validator at `_validate_execution_log_rows` (line 911) requires keys `{task, agent, reviewer, verdict, commit, notes}` and rejects extras. SKILL.md §End-of-run Step 2 does not list these keys; the orchestrator's first attempt used `task_id`/`title`/`implementer`/`commit_sha`/`tests` and got a 7-error response before figuring out the schema.

**Fix:** document the row schema in SKILL.md §End-of-run Step 2 (inline bullet list, no new section). Extend the `missing-execution-log-field` and `unknown-execution-log-field` error messages to include the full allowed-field list in the message body for discoverability at error time.

#### (f) TASK-002 status marker says `done` while V3/V4 fail

Flip `TASK-002_runtime_validation.md`'s `**Status:** done` to `**Status:** partial` and append a one-line note under the Status line: "V1/V2/V5/V6/V7/V8 landed; V3/V4 deferred to TASK-019." Once TASK-019 lands, a follow-on housekeeping edit can flip it back to `done`. Do NOT rewrite the acceptance criteria — they're correct; it's the status field that was inaccurate.

### Files this task edits

- `plugins/plan-executor/scripts/plan_ops.py` — new `_validate_schedule_dag`; `cmd_parse_schedule` gains the call; `cmd_batch_next` + `cmd_filter_schedule` drop their inline Kahn's blocks in favor of the helper; `_validate_reviewer_finding_item` gains optional `disposition`/`disposition_reason`; `cmd_update_plan_header` gains absent-header fallback; `_validate_execution_log_rows` error messages list allowed fields.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — §D.3 gains the D.2a reviewer-flip paragraph; §End-of-run Step 2 gains the row-schema inline list.
- `tests/scripts/test_plan_ops.py` — new tests V1-V11 below; update three existing `cmd_batch_next` cycle-detection tests to reference the shared helper path (not behavior-changing).
- `docs/plans/DUAL_AGENT_Plans/TASK-002_runtime_validation.md` — status flip + one-line partial note.

---

## Verification

**V1 — `parse-schedule` rejects dependency cycles.** *(Closes TASK-002 V3.)*

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":[],"dependencies":["002"]},{"id":"002","agent":"codex","files":[],"dependencies":["001"]}],"batches":[{"index":1,"task_ids":["001","002"],"file_locks":[]}]}' \
  | venv/bin/python plugins/plan-executor/scripts/plan_ops.py parse-schedule --stdin --json
```

Exit 1; `errors[*].code == "dependency-cycle"`; message names both cyclic ids.

**V2 — `parse-schedule` rejects orphan dependencies.** *(Closes TASK-002 V4.)*

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":[],"dependencies":["999"]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":[]}]}' \
  | venv/bin/python plugins/plan-executor/scripts/plan_ops.py parse-schedule --stdin --json
```

Exit 1; `errors[*].code == "unknown-dependency"`; message names the orphan id.

**V3 — `_validate_schedule_dag` is a single module-local helper called by all three consumers.**

```python
def test_validate_schedule_dag_shared_by_consumers():
    # grep plugins/plan-executor/scripts/plan_ops.py for `_validate_schedule_dag(`
    # expect exactly 4 occurrences: 1 def + 3 call sites
    # (cmd_parse_schedule, cmd_batch_next, cmd_filter_schedule)
```

Assertion keeps the refactor honest — if a fourth consumer appears later it must use the helper; inline Kahn's blocks are forbidden.

**V4 — `cmd_batch_next` cycle detection still emits `dependency-cycle`.** *(Regression of TASK-004B V8.)*

Re-run `test_batch_next_dag_cycle_rejected` after the refactor. Must still pass — the helper emits the same error code as the old inline block.

**V5 — `cmd_filter_schedule` cycle detection still emits `dependency-cycle`.** *(Regression of TASK-004A.)*

Re-run the filter-schedule cycle test from `test_plan_ops.py`. Must still pass.

**V6 — `disposition` field accepted when present.**

```python
def test_reviewer_minor_finding_disposition_accepted():
    finding = {"severity": "minor", "file": "a.py", "line": 1, "issue": "x", "suggested_fix": "y", "disposition": "dismissed", "disposition_reason": "D.5 override"}
    # expect _validate_reviewer_finding_item returns []
```

**V7 — Invalid `disposition` value rejected.**

```python
def test_reviewer_minor_finding_disposition_value_rejected():
    finding = {"severity": "minor", "file": "a.py", "line": 1, "issue": "x", "suggested_fix": "y", "disposition": "bogus"}
    # expect errors[*].code == "invalid-reviewer-finding-disposition"
```

**V8 — Minor-finding without `disposition` still accepted (backward-compat).**

```python
def test_reviewer_minor_finding_no_disposition_ok():
    finding = {"severity": "minor", "file": "a.py", "line": 1, "issue": "x", "suggested_fix": "y"}
    # expect _validate_reviewer_finding_item returns []
```

**V9 — `update-plan-header` gracefully handles plan without top-level Status.**

```python
def test_update_plan_header_absent_is_not_error(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("# Plan\n\n## Tasks\n\n### TASK-001\n- **Status:** pending\n")
    # invoke cmd_update_plan_header with --status complete
    # expect exit 0, result["status"] == "absent" or result["warning"] mentions skipping
```

**V10 — Execution-log error message lists allowed fields.**

```python
def test_execution_log_missing_field_message_lists_allowed():
    rows = [{"task": "001"}]  # missing agent/reviewer/verdict/commit/notes
    # expect errors[0].message contains all of ["agent", "reviewer", "verdict", "commit", "notes"]
```

**V11 — SKILL.md documents D.2a commit-task reviewer-flip and execution-log row schema.**

```bash
grep -n 'reviewer claude.*ship-with-fixes.*--disagreement-tag' plugins/plan-executor/skills/implement-plan/SKILL.md
grep -n 'rows-json.*task.*agent.*reviewer.*verdict.*commit.*notes' plugins/plan-executor/skills/implement-plan/SKILL.md
```

Both greps must hit. Exact wording may differ — the test asserts the presence of the key field names, not exact phrasing.

---

## Tasks

### TASK-019: Backfill TASK-002 V3/V4 and close orchestrator paper-cuts from run 20260418T015713

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_plan_ops.py`
  - `docs/plans/DUAL_AGENT_Plans/TASK-002_runtime_validation.md`
- **Dependencies:** TASK-002 (partially landed), TASK-004A, TASK-004B
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1-V11 pass.
  - `_validate_schedule_dag(tasks, batches) -> list[dict]` exists as a single module-local helper in `plan_ops.py`. It returns `errors[*]` with `code ∈ {"dependency-cycle", "unknown-dependency"}`; a task with `dependencies: ["999"]` where `999 ∉ tasks[].id` yields one `unknown-dependency` error per orphan; a cycle yields exactly one `dependency-cycle` error naming the cyclic ids.
  - `cmd_parse_schedule`, `cmd_batch_next`, `cmd_filter_schedule` all call `_validate_schedule_dag` and DO NOT contain inline Kahn's algorithm code. Per V3, the helper has exactly one definition and three call sites.
  - `_validate_reviewer_finding_item` accepts an optional `disposition` field constrained to `{null, "dismissed", "accepted", "deferred"}` and an optional `disposition_reason: str`. Findings without `disposition` remain valid (backward-compat).
  - `cmd_update_plan_header` returns exit 0 with a `warning` or `status: "absent"` field when the plan has no top-level `**Status:**` bullet. Existing behavior when the line IS present is unchanged.
  - `_validate_execution_log_rows` error messages for `missing-execution-log-field` and `unknown-execution-log-field` include the full allowed-field list in the message body.
  - SKILL.md §D.3 contains a paragraph naming the `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` combination for D.2a disagreement commits, and a parallel line for D.2b and D.2a.5/D.2a.6 if needed.
  - SKILL.md §End-of-run Step 2 lists the required `rows-json` keys inline: `task, agent, reviewer, verdict, commit, notes`.
  - `TASK-002_runtime_validation.md`'s `**Status:** done` is flipped to `**Status:** partial` with a one-line note referencing TASK-019 as the V3/V4 backfill. Acceptance criteria body is NOT modified.
- **Out of scope (deferred to future TASKs):**
  - Runtime V-check enforcement in `commit-task` (a plan-supplied shell script that halts `commit-task` on V-check failure — TASK-020-class future work; the process lesson from TASK-002's slip, not a code fix).
  - D.2b / D.2a.5 / D.2a.6 detailed commit-task envelope changes beyond the one-paragraph reviewer-flip note.
  - Linter that rejects hand-edited `**Status:** done` markers (would need a pre-commit hook or a `plan_ops.py` lint subcommand; separate concern).

**Description:**
Close the TASK-002 V3/V4 acceptance-criteria gap by factoring a shared `_validate_schedule_dag` helper that implements cycle detection + orphan-dependency detection, and wire it into all three consumers (`parse-schedule`, `batch-next`, `filter-schedule`). In the same pass, land four low-risk orchestrator polishes that surfaced during run `20260418T015713`: SKILL.md D.2a commit-task reviewer-flip guidance, optional reviewer-minor-finding `disposition` field, graceful `update-plan-header` fallback, and a discoverable execution-log row schema. Flip TASK-002's status marker to `partial` with a note.

**Implementation notes:**

- The helper body is the union of the two existing inline Kahn's blocks (`plan_ops.py:1786-1821` in `cmd_batch_next`, and the equivalent in `cmd_filter_schedule` around `2080-2120`) plus an explicit orphan-dep pre-check. Target signature: `_validate_schedule_dag(tasks: list, batches: list) -> list[dict]` returning error dicts in the same shape as `_validate_schedule_refs`. Do NOT call `_die` from inside the helper — return errors to the caller, which decides whether to `_die` or merge into its own error list. This matches `_validate_schedule_refs`'s contract.
- `cmd_parse_schedule` merges the helper's errors into its existing `errors` list (same pattern as the `_validate_schedule_refs` call at line 515). On any error, the existing `_die` path fires; no new exit code.
- `cmd_batch_next` and `cmd_filter_schedule` currently `_die` directly on cycle — keep that behavior, just replace the inline block with `errors = _validate_schedule_dag(tasks, batches); if errors: _die(args, {"errors": errors})`.
- The `disposition` field is additive only. No existing caller passes it; validator rejecting unknown keys today means the new key must also be added to the known-keys set. Constrain values via a set literal and an explicit `invalid-reviewer-finding-disposition` error code. If `disposition_reason` is present without `disposition`, emit `disposition-reason-without-disposition` error.
- The `update-plan-header` fallback check is a single `if status_match is None:` branch near the start of `cmd_update_plan_header`. Emit the absent-status envelope and return before touching the file.
- The execution-log error-message extension is one string-format change at each of the two error-emission sites. Do not change the error codes.
- For the TASK-002 status flip, edit only the `**Status:**` line and add one `> V3/V4 deferred to TASK-019` blockquote directly beneath it. Do not modify the Verification, Tasks, or Implementation Playbook sections.

**Reversion guidance:**

- The `_validate_schedule_dag` helper is strictly additive. Safe to revert by restoring the two inline Kahn's blocks; `parse-schedule` would regress to its current state (cycles accepted), which is the pre-TASK-019 baseline — undesirable but not destructive.
- The `disposition` field is additive and backward-compatible. Safe to revert — existing callers do not use it.
- The `update-plan-header` fallback is additive (new exit-0 branch). Safe to revert; orchestrator would re-see the current error.
- The TASK-002 status-flip is a docs-only change. Trivially revertable.
- **Never revert V1/V2** — cycles and orphan deps in `parse-schedule` were the primary TASK-002 deliverable; re-introducing the gap would silently re-enable the entire class of bugs TASK-002 was written to prevent.

---

## Implementation Playbook

### Step 1 — Factor `_validate_schedule_dag` helper

Create the helper in `plan_ops.py` next to `_validate_schedule_refs`:

```python
def _validate_schedule_dag(tasks: list, batches: list) -> list[dict]:
    """Cycle + orphan-dep detection. Returns errors[*]; does not halt.

    Contract:
      - `unknown-dependency` for each task.dependencies[j] not in known task ids
      - `dependency-cycle` (at most one) naming the residual cyclic task ids
    """
    errors: list[dict] = []
    known_ids: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = _normalize_task_id(str(raw))
        if tid:
            known_ids.add(tid)

    # 1. Orphan-dependency check
    dag_deps: dict[str, list[str]] = {}
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        raw = t.get("id") if "id" in t else t.get("task_id")
        if raw is None:
            continue
        tid = _normalize_task_id(str(raw))
        if not tid:
            continue
        deps_norm: list[str] = []
        for j, dep in enumerate(t.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None:
                continue
            if dep_norm not in known_ids:
                errors.append({
                    "path": f"$.tasks[{i}].dependencies[{j}]",
                    "code": "unknown-dependency",
                    "message": (
                        f"tasks[{i}].dependencies[{j}]={str(dep)!r} is not a known task id"
                    ),
                })
                continue
            deps_norm.append(dep_norm)
        dag_deps[tid] = deps_norm

    # 2. Kahn's algorithm — cycle detection over the cleaned dep graph
    indeg: dict[str, int] = {tid: 0 for tid in dag_deps}
    for tid, deps in dag_deps.items():
        for d in deps:
            if d in indeg:
                indeg[tid] += 1
    queue = [tid for tid, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        head = queue.pop(0)
        visited += 1
        for other, deps in dag_deps.items():
            if head in deps:
                indeg[other] -= 1
                if indeg[other] == 0:
                    queue.append(other)
    if visited != len(dag_deps):
        cyclic = sorted(tid for tid, n in indeg.items() if n > 0)
        errors.append({
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": f"dependency cycle in schedule involving tasks: {cyclic}",
        })
    return errors
```

### Step 2 — Wire into the three consumers

- `cmd_parse_schedule`: after the existing `_validate_schedule` call, if `errors` is empty, call `_validate_schedule_dag(tasks, batches)` and extend `errors`.
- `cmd_batch_next`: replace the inline Kahn's block (currently at `plan_ops.py:1786-1821`) with `dag_errors = _validate_schedule_dag(list(data.get("tasks") or []), list(data.get("batches") or [])); if dag_errors: _die(args, {"errors": dag_errors})`.
- `cmd_filter_schedule`: same replacement at its current inline-Kahn's location.

### Step 3 — `disposition` field on reviewer-minor-findings

Extend `_validate_reviewer_finding_item`:

```python
OPTIONAL_DISPOSITIONS = {"dismissed", "accepted", "deferred"}
OPTIONAL_FIELDS = {"disposition", "disposition_reason"}

# in _validate_reviewer_finding_item:
for key in item.keys():
    if key in required or key in OPTIONAL_FIELDS:
        continue
    errors.append({...unknown-reviewer-finding-field...})

disp = item.get("disposition")
if disp is not None and disp not in OPTIONAL_DISPOSITIONS:
    errors.append({"path": f"{path}.disposition", "code": "invalid-reviewer-finding-disposition", ...})
reason = item.get("disposition_reason")
if reason is not None and not isinstance(reason, str):
    errors.append({"path": f"{path}.disposition_reason", "code": "invalid-reviewer-finding-disposition-reason", ...})
if reason is not None and disp is None:
    errors.append({"path": f"{path}.disposition_reason", "code": "disposition-reason-without-disposition", ...})
```

### Step 4 — `update-plan-header` absent-status fallback

In `cmd_update_plan_header`:

```python
m = re.search(r"^\*\*Status:\*\*\s*(\S+)", text, flags=re.MULTILINE)
if m is None:
    _emit(args, {"status": "absent", "warning": "no plan-level **Status:** line; skipping"})
    return
```

### Step 5 — Execution-log error messages list allowed fields

In `_validate_execution_log_rows`, change the two error-emission sites:

```python
REQUIRED_FIELDS_DISPLAY = ["task", "agent", "reviewer", "verdict", "commit", "notes"]
# missing-field message:
f"execution-log row missing field {key!r}; required fields: {REQUIRED_FIELDS_DISPLAY}"
# unknown-field message:
f"execution-log row has unknown field {key!r}; allowed fields: {REQUIRED_FIELDS_DISPLAY}"
```

### Step 6 — SKILL.md updates

§D.3 — insert after the existing commit-task call-site documentation:

> **After D.2a disagreement (Codex `needs-rework` → D.5 `ship` | `ship-with-fixes`):** pass `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag`. The binding verdict is D.5's; `--reviewer codex --reviewer-verdict needs-rework` is rejected with `uncommittable-reviewer-verdict`. Record the original Codex findings verbatim in `--reviewer-minor-findings` with each finding's optional `disposition: "dismissed"` field set (see also D.2a.6 where dismissed indices also appear in `--dismissed-finding-ids`).

§End-of-run Step 2 — append a one-line list of required row keys:

> `--rows-json` row schema: each row must be an object with exactly these keys: `task` (str), `agent` (str, e.g. `"claude/opus"` or `"codex"`), `reviewer` (str), `verdict` (str, including any `[disagreement]` / `[remediation]` markers in prose), `commit` (str, short SHA), `notes` (str). No extras; all required. Missing or extra keys error out with the full allowed-field list in the error message.

### Step 7 — TASK-002 plan file status flip

Edit `TASK-002_runtime_validation.md` line 146 only:

- Change `- **Status:** done` to `- **Status:** partial`
- Insert directly beneath it: `  > V3 (cycle rejection in parse-schedule) and V4 (orphan-dep rejection) deferred to TASK-019. V1/V2/V5/V6/V7/V8 landed.`

### Step 8 — Tests

Add V1-V10 as individual test functions in `tests/scripts/test_plan_ops.py`. V11 is a grep-based SKILL.md regression test; add it alongside the existing SKILL.md-content tests if any exist, or as its own module-level function.

Run `venv/bin/pytest -q tests/scripts/test_plan_ops.py` after each step; the suite must remain green.

### Step 9 — Regression sweep

After all changes, rerun:

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

Expected: new V1-V11 tests pass; existing batch-next / filter-schedule / parse-schedule / commit-task tests unchanged. The pre-existing live-CLI roundtrip test (`test_analyst_to_parse_schedule_roundtrip`) is unrelated to this task and may continue to fail if the environment's `claude` binary returns diagnostic JSON; that is out of scope.

---

## Out of Scope

- **Runtime V-check enforcement in `commit-task`** — the deeper process lesson from TASK-002's slip. A plan-supplied V-check shell script that `commit-task` runs pre-commit and halts on failure. This is a TASK-020-class future work item; TASK-019 only addresses the code gap, not the process gap.
- **Linter that catches hand-edited `**Status:** done` markers** — would need a `plan_ops.py lint-plan` subcommand or a pre-commit hook. Separate concern.
- **Unifying the Phase D-Codex / Phase D-Claude verdict vocabulary** — TASK-015/TASK-018 territory.
- **`acquire-lock` strict-shape / `block-dependents` plan-mutation** — TASK-004D / TASK-004E.
- **Any behavior change to TASK-004A's `filter-schedule` subcommand or TASK-004B's `batch-next` contract** — TASK-019 is a pure refactor on those call sites; the observable behavior is unchanged.

## Reversion guidance

- See per-step reversion notes in the Description / Implementation notes. V1/V2 (parse-schedule cycle + orphan rejection) are the primary deliverables and must NEVER be reverted; doing so re-opens the exact silent-cycle bug that run `20260415T000811` caught inside `batch-next`.
- The `disposition` field, the `update-plan-header` fallback, the execution-log error-message extension, and the SKILL.md docs are all additive and trivially revertable.
- The TASK-002 status-flip is a docs-only change.

## Execution log — 20260419T200147 (success)

Starting SHA: `b4449ef893d2770fc4d3b468194f84bce4582458`  → Ending SHA: `df608016f5aa7c205fb0a7a932a6d11f58611c6c`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 019 | claude/opus | codex+claude/sonnet(D.5) | ship-with-fixes [disagreement] | df60801 | Codex needs-rework (3 findings) overridden by D.5 ship-with-fixes; all dismissed |
