# TASK-002 — Runtime Contract Validation at Every Executor Seam

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-002
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md)
**Base branch:** `phase-7b5-bug-fixes`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contracts must be chosen first).
**Issues absorbed:** ISSUE-001 (runtime enforcement), 014 (validation), 016 (validation), 019 (defensive DAG), 020 (schedule persistence)

---

## Goal

Upgrade the executor from trusting its neighbors to validating them. Every handoff point — plan markdown → analyst, analyst → schedule parser, implementer → parse-implementer-report, reviewer → parse-reviewer, execution-log row → writer — validates the payload shape against the canonical contract set in TASK-001 and halts (not warns) on invalid input. Validation runs at real handoff points in the orchestrator path, backed by real producer→consumer integration tests, not helper-local synthetic fixtures.

## Scoped Context

TASK-001 established *what* the shapes are. TASK-002 enforces *that* the shapes hold at runtime. The Phase 4 test suite validated the implementation against itself — that is how three independent contract mismatches survived into Phase 5. The antidote is integration tests that pipe real producers (analyst agent, implementer agent) through the consumer helpers.

### ISSUE-001 (P0) — runtime enforcement of canonical schedule shape

After TASK-001, `parse-schedule` accepts `id`/`index`. TASK-002 adds:
- Halt (exit 1, `errors` non-empty) on unknown top-level fields — reject silently-extended schedules.
- Halt on duplicate `id` values in `tasks[]` or duplicate `index` values in `batches[]`.
- Halt on batch entries that reference a `task_ids[i]` that is not in `tasks[].id`.
- Halt on task entries whose `dependencies` reference unknown ids.
- Emit structured `errors` with path-and-reason tuples, not bare strings.

Add a real-producer integration test: dispatch `plan-analyst` on `docs/plans/sample_phase4.md` (post TASK-006 rewrite — or the spec-compliant plan embedded in `tests/scripts/test_plan_codex_dispatch_integration.py:27-71` until TASK-006 lands), capture the JSON, pipe it through `parse-schedule --stdin --json`, assert exit 0 with `errors: []`.

### ISSUE-014 (P2) — implementer report concerns-label validation

After TASK-001, parser accepts `**Concerns for reviewer:**`. TASK-002 adds: if the report contains no such label **and** the outcome is not `failed`/`blocked`, emit a diagnostic (not halt — concerns can legitimately be empty) so missing labels are visible. Add a test that ensures concerns extraction is robust to:
- missing label (empty list returned, diagnostic emitted)
- empty bullet list under label (empty list, no diagnostic)
- nested bullets (flatten or halt — pick one and document)

### ISSUE-016 (P1) — implementer plan-adaptations validation

After TASK-001, parser extracts `**Plan adaptations:**`. TASK-002 adds: if the section is absent, emit a diagnostic. The implementer contract (`.claude/agents/plan-implementer.md:98-99`) marks plan-adaptations mandatory, so absence indicates contract-break. Cover with a test.

### ISSUE-019 (P2) — defensive DAG check in `parse-schedule`

`scripts/plan_ops.py:227-280` currently trusts the analyst's `outcome` field. Feeding JSON with `outcome: valid` + a dependency cycle passes `parse-schedule` with exit 0.

Add a topological-sort validator in `parse-schedule`:
- Build adjacency from `tasks[].dependencies`.
- Run Kahn's or DFS; if a residual set remains, halt with `errors: ["dependency cycle: <nodes>"]`.
- The topo logic can be factored out from `cmd_batch_next` (same file) rather than duplicated.

### ISSUE-020 (P2) — schedule persistence subcommand

`SKILL.md:113` prescribes persisting the schedule at `docs/plans/<basename>.schedule.json` but no `plan_ops.py` subcommand owns the write. Add `plan_ops.py write-schedule --schedule-file <path> --stdin`:
- Reads JSON on stdin.
- Validates it via `parse-schedule` logic (shared code path).
- Writes to `--schedule-file` atomically (write to tmp, `os.replace`).
- Emits `{"written": <path>, "bytes": N}` on success; halts on validation failure.

Replace the SKILL.md direct-write with a reference to this subcommand. Any future `--codex-only` / `--claude-only` / `--task-ids` filters (TASK-004) also write through this subcommand.

---

## Verification

**V1 — Real producer integration test exists and passes.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::test_analyst_to_parse_schedule_roundtrip
```

Test dispatches `plan-analyst` via the subagent machinery (see `tests/scripts/test_plan_codex_dispatch_integration.py` for the pattern; may require marking `@pytest.mark.slow`), captures the JSON, pipes through `parse-schedule`. Assert exit 0 and `errors: []`.

**V2 — Duplicate id rejected.**

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":[],"dependencies":[]},{"id":"001","agent":"claude","files":[],"dependencies":[]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":[]}]}' \
  | venv/bin/python scripts/plan_ops.py parse-schedule --stdin --json
```

Exit 1, `errors` contains `"duplicate task id"` or equivalent.

**V3 — Cycle rejected even when analyst says `valid`.**

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":[],"dependencies":["002"]},{"id":"002","agent":"codex","files":[],"dependencies":["001"]}],"batches":[{"index":1,"task_ids":["001","002"],"file_locks":[]}]}' \
  | venv/bin/python scripts/plan_ops.py parse-schedule --stdin --json
```

Exit 1, `errors` contains `"dependency cycle"`.

**V4 — Orphan dependency rejected.**

Task depending on a non-existent id must halt:

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":[],"dependencies":["999"]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":[]}]}' \
  | venv/bin/python scripts/plan_ops.py parse-schedule --stdin --json
```

Exit 1, errors mention `"unknown dependency"`.

**V5 — `write-schedule` round-trip.**

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":["a.txt"],"dependencies":[]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":["a.txt"]}]}' \
  | venv/bin/python scripts/plan_ops.py write-schedule --schedule-file /tmp/test.schedule.json --stdin --json

diff <(cat /tmp/test.schedule.json) <(echo ...)  # verify bytes match input
rm /tmp/test.schedule.json
```

Exit 0 and output file equals input JSON (modulo pretty-printing).

**V6 — `write-schedule` rejects invalid JSON.**

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":[],"dependencies":["002"]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":[]}]}' \
  | venv/bin/python scripts/plan_ops.py write-schedule --schedule-file /tmp/test.schedule.json --stdin --json
```

Exit 1; `/tmp/test.schedule.json` must NOT have been written (no partial file).

**V7 — Implementer parser flags missing plan-adaptations.**

```bash
printf '**Outcome:** success\n**Files changed:**\n- a.txt\n**Diff summary:** noop\n**Test outcome:** not_run\n**Concerns for reviewer:**\n**Reversion guidance:** revert\n' \
  | venv/bin/python scripts/plan_ops.py parse-implementer-report --json
```

Output contains a `warnings` or `diagnostics` list citing `missing **Plan adaptations:** section`.

**V8 — SKILL.md no longer directs orchestrator to write the schedule JSON directly.**

```bash
grep -n 'Write.*schedule.json' .claude/skills/implement-plan/SKILL.md
# Matches should reference `plan_ops.py write-schedule`, not a raw Write tool call
```

---

## Tasks

### TASK-002: Add runtime contract validation at every executor seam

- **Status:** done
- **Priority:** critical
- **Files:**
  - `scripts/plan_ops.py`
  - `scripts/plan_codex_dispatch.py`
  - `.claude/skills/implement-plan/SKILL.md`
  - `.claude/agents/plan-analyst.md`
  - `.claude/agents/plan-implementer.md`
  - `tests/scripts/test_plan_ops.py`
  - `tests/scripts/test_plan_codex_dispatch_integration.py`
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py && venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -m 'not slow'`
- **Acceptance criteria:**
  - Every handoff point (plan → analyst, analyst → `parse-schedule`, implementer → `parse-implementer-report`, reviewer → consumer, execution-log writer) validates payload shape and halts on invalid input.
  - Validation happens at real handoff points, not only inside helper-local synthetic tests.
  - ISSUE-001, 014, 016, 019, 020 are covered by runtime behavior and by at least one test each.
  - At least one real producer→consumer integration test exists per handoff (analyst→parse-schedule and implementer→parse-implementer-report).
  - `write-schedule` subcommand exists and is the only documented path for persisting the schedule JSON.
  - All verification checks V1–V8 pass.

**Description:**
Upgrade the executor from trusting neighboring components to validating them as protocol participants.

**Implementation notes:**
Validation should happen at real handoff points, not only inside helper-local synthetic tests. If strict validation creates false positives for historic inputs, introduce an alias window (TASK-001) rather than loosening the validator. Factor DAG logic out of `cmd_batch_next` into a shared helper so `parse-schedule` and `batch-next` see the same cycle.

**Reversion guidance:**
If strict validation creates real false positives (not contract bugs), temporarily allow only the documented compatibility variants. Do not remove seam validation. Extract any problematic check behind a `--strict` / `--lenient` flag rather than deleting it.

---

## Implementation Playbook

### Step 1 — factor out schedule DAG validation

Create a module-local helper in `scripts/plan_ops.py`:

```
def _validate_schedule_dag(tasks, batches) -> list[str]:
    errors = []
    # 1. duplicate ids
    # 2. orphan dependencies
    # 3. topological sort (Kahn's). Residual nodes => "dependency cycle: <sorted>"
    # 4. batch.task_ids reference only known ids
    # 5. batch.index uniqueness
    return errors
```

`cmd_parse_schedule` (`scripts/plan_ops.py:227-280`) calls `_validate_schedule_dag` after the existing shape checks. Merge returned errors into the result's `errors` list. `cmd_batch_next` also calls `_validate_schedule_dag` as a defensive check (cheap, cycle may have been missed by analyst).

### Step 2 — promote shape checks to "halt on unknown fields"

`parse-schedule` currently only checks presence of required fields. Add:

- Unknown top-level field → error.
- Unknown field inside `tasks[i]` or `batches[i]` → warning (tolerate forward-compat extension), unless a `--strict` CLI flag is passed in which case it errors. Default tolerant.
- `tasks[i].id` must match the normalized form `\d{3}` (zero-padded); reject non-conforming ids. Analyst already emits this form.

### Step 3 — implementer report validation

In `cmd_parse_implementer_report` (`scripts/plan_ops.py:354-403`):

- After the existing field extraction, if `outcome in {"success", "partial"}` and `plan_adaptations` list is missing (not empty, but the section header itself was absent), add `{"code": "missing_plan_adaptations", "message": "**Plan adaptations:** section is mandatory per plan-implementer contract"}` to a new `diagnostics: []` field in the result. This is observability, not a halt — the orchestrator decides whether to halt or warn.
- Same for a missing `**Concerns for reviewer:**` section (empty bullet list is fine; missing header is a diagnostic).

### Step 4 — `write-schedule` subcommand

Add to `plan_ops.py`:

```
parser_ws = subparsers.add_parser("write-schedule", help="Persist schedule JSON after validation")
parser_ws.add_argument("--schedule-file", required=True)
parser_ws.add_argument("--stdin", action="store_true")
parser_ws.add_argument("--json", action="store_true")
parser_ws.set_defaults(func=cmd_write_schedule)
```

`cmd_write_schedule` must:
1. Read JSON from stdin (require `--stdin`; no file input — this is a pipe tool).
2. Run the same validation path as `parse-schedule` (share a `_validate_schedule` helper; both subcommands call it).
3. On validation failure: `_die` with the errors; DO NOT write the file.
4. On success: write to `<schedule-file>.tmp`, then `os.replace()` to final. Atomic to avoid half-written state on crash.
5. Emit `{"written": str(path), "bytes": len(serialized)}`.

### Step 5 — SKILL.md integration

Update `.claude/skills/implement-plan/SKILL.md`:

- Phase A or wherever the orchestrator persists the analyst schedule: replace the "Write ..." instruction with "Run `venv/bin/python scripts/plan_ops.py write-schedule --schedule-file docs/plans/<basename>.schedule.json --stdin --json`, piping the analyst JSON."
- Ensure line 316's "Never write inline Python for plan ops" rule still stands; no regression.

### Step 6 — real producer integration test

In `tests/scripts/test_plan_ops.py`:

```
@pytest.mark.slow
def test_analyst_to_parse_schedule_roundtrip(tmp_path):
    # Use a minimal spec-compliant plan (borrow the embedded one from
    # test_plan_codex_dispatch_integration.py:27-71 until TASK-006 lands).
    # Dispatch plan-analyst via the test harness (subprocess or SDK).
    # Pipe the emitted JSON through plan_ops.py parse-schedule --stdin --json.
    # Assert exit 0 and errors == [].
```

If dispatching the analyst in CI is slow or flaky, mark `@pytest.mark.slow` and gate in nightly. Prefer a real dispatch over a stub because the whole point of TASK-002 is to validate at real handoff points.

### Step 7 — implementer report integration test

In `tests/scripts/test_plan_ops.py`:

```
def test_implementer_report_missing_plan_adaptations_diagnostic():
    report = _report_with_no_plan_adaptations()
    result = _run("parse-implementer-report", stdin=report, json=True)
    assert any(d["code"] == "missing_plan_adaptations" for d in result["diagnostics"])
```

### Step 8 — `write-schedule` tests

Cover:
- Valid JSON → file exists with expected content + exit 0.
- Invalid JSON (cycle) → file does not exist + exit 1.
- Concurrent write safety is out of scope; atomic `os.replace` is sufficient.

### Step 9 — regression sweep

Run full test suite: `venv/bin/pytest -q tests/scripts/`. Expected: all tests green. New tests added in this chunk account for at least 6 additional cases.

---

## Out of Scope

- **Picking the canonical shape itself:** TASK-001 did this.
- **Wrapper state isolation**: TASK-003.
- **Scheduler semantics fixes** (`batch-next` honoring declared batches, `fail-task` cleanup, `block-dependents` plan mutation): TASK-004.
- **`--task-ids` filtering and `filter-schedule` subcommand**: TASK-004 (builds on `write-schedule`).
- **Phase gates** and **self-audit**: TASK-005, TASK-007.
- **Sample fixture rewrite**: TASK-006 — this chunk may use the embedded spec-compliant plan in `tests/scripts/test_plan_codex_dispatch_integration.py:27-71` as a stand-in.
- **Preflight / portability**: TASK-008.
- **Large-file reads / global locks / bounded logs**: TASK-009, 010, 011.

## Reversion guidance

- **`write-schedule` subcommand:** Safe to revert; SKILL.md temporarily reverts to direct-write. This regresses ISSUE-020 only.
- **DAG defensive check in `parse-schedule`:** Safe to gate behind a `--strict-dag` flag if it creates false positives, but the default must remain strict because the alternative is silent cycle-tolerance (ISSUE-019 regression).
- **Integration test:** If the test is flaky due to live analyst dispatch, mark `@pytest.mark.slow` and move to nightly. Do not delete.
- **Halt on unknown top-level field:** If legitimate forward-compat extensions must survive, downgrade to warning. Do not remove the validator.
