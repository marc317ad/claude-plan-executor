# Directory-mode hotfix — unblock `/implement-plan <dir>` at the protocol layer

**Created:** 2026-04-24
**Status:** draft
**Base branch:** main

## Goal

Land the minimum diff that makes `/implement-plan <directory>` execute end-to-end without reverting directory-mode infrastructure. Three concrete defects are in scope, all surfaced by live runs and independent second-opinion review (codex + gemini):

1. **Protocol hole in the plan-review wrapper.** `plan_codex_dispatch.py cmd_plan_review` calls `plan_path.read_text()` on whatever `--plan-file` it receives, so directory inputs raise `IsADirectoryError` and the orchestrator logs `plan_review_skipped {reason:"codex_unavailable"}`. Reproduced in `docs/plans/_run_log.jsonl` at 2026-04-24T02:40:44Z.
2. **Preflight's file-only assumption.** `plan_ops.py cmd_preflight` assumes a single plan file, so directory-mode runs that reach preflight silently lose the `plan_scope_dirty` check across sibling children. Latent today because the orchestrator short-circuits on the wrapper failure before preflight's gap matters, but the gap will bite as soon as #1 lands.
3. **Consumer-side leak of Codex `notes[]`.** Commit `e8945b0` added `notes[]` to the plan-review envelope and the validator at `plan_ops.py:3080-3094` requires it, but `cmd_parse_plan_review_report` at `plan_ops.py:3262-3271` omits `notes` from its emitted result. `SKILL.md:381` promises extraction-and-routing of `notes` that never actually happens.

Out of scope: removing dual-mode prose from SKILL.md, moving `tasks[]` synthesis into `plan_ops.py build-tasks`, per-child classifier fan-out, schedule-only plan-review. Those belong in the longer-form refactor (`docs/plans/per_task_dispatch_refactor.md`) and are orthogonal to unblocking directory-mode today.

## Context

Five commits on `main` landed directory-mode support (`5ddb35e` → `e76750e`) plus one orthogonal codex-contract alignment (`e8945b0`). Neither the schema infrastructure (per-task `plan_file` passthrough, `block-dependents` multi-file cascade, directory fixture) nor the codex prompt changes are broken. The gaps are at the two boundaries the original directory-mode plan did not cover: the wrapper's file-only reader and preflight's file-only branch. Plus a tiny consumer omission inside the codex-contract work.

Hotfix strategy: patch the two directory-unaware entry points in place and close the `notes[]` leak. Keep dual-mode prose intact — this is a protocol fix, not a rewrite. The separate refactor document is the path to a single-track architecture; that refactor assumes the infrastructure this hotfix leaves in place.

**In scope**

- `cmd_plan_review` accepts either a file or a directory. For directories: read `00_INDEX.json`, concatenate `chunks[].file` contents into a single `plan_text` for the prompt. No change to the schedule input, the envelope shape, or the Codex prompt structure itself (Codex already reads whatever markdown you feed it).
- `cmd_preflight` accepts either a file or a directory. For directories: read `00_INDEX.json`, union every child's `Files:` declarations into the `plan_scope_dirty` classifier's scope; recognize any `chunks[].file` basename as plan text for the `plan_doc` classifier; use the first non-null `base_branch` declared by any child.
- `cmd_parse_plan_review_report` preserves `notes[]` in its emitted result (one-line dict addition).
- Test coverage: one integration test for each of the three fixes, using the shipped `tests/fixtures/directory_mode_plan/` fixture. No new fixtures.

**Out of scope (deliberately deferred)**

- Dropping `--plan-file` from `cmd_plan_review` — that's TASK-005 of the refactor, and is strictly better than this hotfix but requires the fat-schedule prerequisite.
- Extracting `acceptance_criteria` text into the schedule — prerequisite for schedule-only plan-review (refactor TASK-003); not needed while plan-review still reads plan markdown.
- SKILL.md prose cleanup — refactor TASK-007.
- Downstream routing on `blocking` / `confidence` fields — the codex contract added them but no code currently branches on them. A separate "which verdicts should promote on `blocking=true` findings?" decision belongs outside this plan.
- `commit-task` end-to-end CLI test gap flagged by Codex in the `0c78a3e` review — deferred to the refactor; the hotfix does not touch `commit-task` and TASK-004 below is an in-process chain, not a full orchestrator smoke. The pre-existing `TestDirectoryModeBatchNext` / `TestDirectoryModeFixture` suites at `test_plan_ops.py:13040+` still hold the fixture-level coverage.

## Verification

Two verification paths: pytest (automated, mocked — the CI gate) and an optional operator spot-check via `/implement-plan` (exercises all three fixes end-to-end against real Codex).

**Automated (pytest):**

1. **Wrapper directory input.** `python3 -m pytest tests/scripts/test_plan_codex_dispatch_integration.py -q -k "plan_review and directory"` passes (new tests from TASK-001). The tests assert dry-run emits `outcome: "dry_run"`, the prompt preview contains all three `### TASK-00N` headings, and directory-without-00_INDEX.json returns exit 1 with a missing-roster error.
2. **Preflight directory input.** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "preflight and directory"` passes (new tests from TASK-002). Tests cover clean-tree pass, scope-dirty attribution to the correct child's task_id, and missing-child-file halt.
3. **`parse-plan-review-report` emits `notes`.** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "parse_plan_review and notes"` passes (new tests from TASK-003). Covers both the success-outcome result-dict branch at line 3262 and the terminal-outcome result-dict branch at line 3222.
4. **End-to-end directory smoke (in-process).** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "directory_mode_hotfix_smoke"` passes (new test from TASK-004). In-process chain across `cmd_preflight` → `cmd_plan_review` (with `invoke_codex` monkey-patched) → `cmd_parse_plan_review_report`, exercising the data flow between the three fixes.
5. **No regressions.** `python3 -m pytest tests/scripts -q` shows the baseline 714 previously-passing tests still pass, plus the ~9 new tests added by TASK-001/002/003/004 (breakdown: TASK-001 +2, TASK-002 +3, TASK-003 +3, TASK-004 +1). The pre-existing `test_analyst_to_parse_schedule_roundtrip` CLI-envelope failure (from the 2026-04-24 pytest baseline) is out of scope and remains failing unchanged; 1 skipped test remains skipped.

**Operator spot-check (optional, requires `codex` on PATH):**

6. **End-to-end via orchestrator.** Invoke `/implement-plan tests/fixtures/directory_mode_plan/ --dry-run`. Phase 1 analyst writes the schedule sidecar as a side effect (so no pre-existing `.schedule.json` fixture is required); Phase 1.5 then reaches the fixed `cmd_plan_review` via directory input. Assert `_run_log.jsonl` contains `plan_review_done {verdict, ...}` for this run and does NOT contain `plan_review_skipped {reason: "codex_unavailable"}`. This is the real-world analogue of TASK-004 — same chain, real Codex, orchestrator-written schedule. Skip this in CI (Codex-on-PATH is not guaranteed); the pytest tests are authoritative for merge gating.

## Tasks

### TASK-001: `cmd_plan_review` accepts directory input

- **Status:** done
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
- **Dependencies:** none
- **Test command:** `python3 -m pytest tests/scripts/test_plan_codex_dispatch_integration.py -q -k "plan_review and directory"`
- **Acceptance criteria:**
  - `cmd_plan_review` detects `plan_path.is_dir()` at the top of the function (after `plan_path.exists()` check). Directory branch: read `00_INDEX.json`, iterate `chunks[].file` in roster order, read each child, concatenate into a single `plan_text` with one blank line between children and a short marker (e.g. `<!-- {basename} -->`) before each section so Codex can tell them apart.
  - File branch preserves byte-identical behavior — existing tests pass unchanged.
  - Missing `00_INDEX.json` in directory input emits `outcome: "failure"` with `error: "Missing 00_INDEX.json in plan directory: <path>"` and returns 1 (same shape as the existing missing-plan-file error at line 1386).
  - A `chunks[].file` entry that does not exist on disk emits `outcome: "failure"` with `error: "Roster chunk not found: <basename>"` and returns 1. No partial reads.
  - The rendered prompt's `plan_basename` is the directory's own basename (e.g. `"directory_mode_plan"`), preserving today's "plan_file field in output envelope" contract.
  - New test: dry-run against `tests/fixtures/directory_mode_plan/` emits `outcome: "dry_run"` and the `prompt_preview` contains all three `### TASK-00N` headings from the fixture children.
  - New test: `--plan-file` pointing at a directory without `00_INDEX.json` returns exit 1 and a `missing-roster` shaped error.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_codex_dispatch.py tests/scripts/test_plan_codex_dispatch_integration.py`

**Description:**
Close the protocol hole at its source. The wrapper does one thing — read plan markdown, render a prompt, invoke Codex, parse the envelope. Teaching it to concatenate a decomposed plan is a few lines of logic at the top of `cmd_plan_review` and does not touch any prompt template, schema, or orchestrator call site. The orchestrator's existing directory-mode call (`SKILL.md:345-357`) already passes the directory path — this task makes the wrapper respect what it's being handed.

**Implementation notes:**
Do NOT import `_parse_index_roster` from `plan_ops.py` — that function runs full roster validation (superseded-by tracking, cycle detection, status enum enforcement) which the wrapper does not need for a read-and-concat. A minimal inline JSON read against `chunks[].file` is enough. The simplest concat shape:

```python
if plan_path.is_dir():
    roster_path = plan_path / "00_INDEX.json"
    if not roster_path.exists():
        emit(make_envelope("plan", "plan-review", "failure",
                           error=f"Missing 00_INDEX.json in plan directory: {plan_path}"))
        return 1
    try:
        roster = json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        emit(make_envelope("plan", "plan-review", "failure",
                           error=f"Cannot parse roster: {exc}"))
        return 1
    chunks = roster.get("chunks") or []
    if not chunks:
        emit(make_envelope("plan", "plan-review", "failure",
                           error=f"Roster has no chunks: {roster_path}"))
        return 1
    pieces = []
    for chunk in chunks:
        child_name = chunk.get("file")
        if not isinstance(child_name, str) or not child_name:
            emit(make_envelope("plan", "plan-review", "failure",
                               error=f"Roster chunk missing 'file' key"))
            return 1
        child = plan_path / child_name
        if not child.exists():
            emit(make_envelope("plan", "plan-review", "failure",
                               error=f"Roster chunk not found: {child_name}"))
            return 1
        pieces.append(f"<!-- {child_name} -->\n{child.read_text(encoding='utf-8')}")
    plan_text = "\n\n".join(pieces)
    plan_basename = plan_path.name
else:
    plan_text = plan_path.read_text(encoding="utf-8")
    plan_basename = plan_path.name
```

Keep `plan_basename` as the directory name in the directory branch so the envelope's `plan_file` field stays meaningful. Superseded children (chunks whose `status == "Superseded"`) are intentionally included in the concat — Codex reviews all children for cross-plan consistency; skipping them at the wrapper boundary would hide supersession chains from the reviewer.

### TASK-002: `cmd_preflight` accepts directory input

- **Status:** done
- **Priority:** high
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** none
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "preflight and directory"`
- **Acceptance criteria:**
  - `cmd_preflight` (around line 2379) detects `plan_path.is_dir()` and takes a directory branch. File branch is byte-identical to today.
  - Directory branch: read `00_INDEX.json` via the existing `_parse_index_roster` helper; iterate children; for each, extract the task-block `Files:` declarations using the same parser the file branch uses (factor the inner parse out if needed — do not reimplement).
  - Union all children's declared files into the `plan_scope_dirty` classifier. Per-task attribution in the output mirrors file-mode: each dirty path names the task whose `Files:` list contained it.
  - `plan_doc` classifier: any path matching `<plan_dir>/00_INDEX.json` OR any `chunks[].file` under `<plan_dir>` is classified as plan text (not `source_blocking`).
  - `base_branch` is read from the first child that declares one (typically all children declare the same value). None-declared falls back to the existing default.
  - New test: preflight against the clean `tests/fixtures/directory_mode_plan/` returns `pass: true`, empty `plan_scope_dirty`, and no `source_blocking`.
  - New test: preflight against the fixture with a tracked-dirty `scratch/a.txt` (in TASK-002's `Files:` list) attributes the warning to `task_id: "002"`.
  - New test: preflight against the fixture with a missing child (`chunks[0].file` deleted on disk) halts with a clear `missing-roster-chunk` error.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Preflight is the second boundary the original directory-mode plan did not cover. Its output envelope is the same shape either way — the orchestrator consumes `pass`, `plan_scope_dirty`, `scope_warnings`, etc. without knowing whether the input was a file or a directory. This task is a structural copy of what TASK-001 does for the wrapper, but in preflight's own parse/classify pipeline.

**Implementation notes:**
`_parse_index_roster` (`plan_ops.py:1531`) already performs roster validation inside plan_ops — reuse it here (same-module call, no coupling concern). The existing file-branch uses `_allowed_files_union(plan_text)` which composes `_split_task_blocks` + `_extract_task_files_from_plan` on a single-plan string. For directories, call `_allowed_files_union(child_text)` per child and union the resulting `{path: task_id}` dicts. Do NOT concatenate child texts before calling `_allowed_files_union` — per-child attribution matters for the "TASK-NNN will write to <path>" warning message, and the task_id namespace is guaranteed unique across children by the roster.

For the `plan_doc` classifier (file-branch code: `if path == str(plan) or path.endswith(plan.name)`), directory mode needs different logic: classify a path as `plan_doc` iff it matches `<plan_dir>/00_INDEX.json` OR it matches `<plan_dir>/<chunk_name>` for any chunk in the roster. Implementation shape:

```python
if plan.is_dir():
    roster = _parse_index_roster(plan / "00_INDEX.json")
    child_paths = {str(plan / entry["file"]) for entry in roster.values()}
    index_path = str(plan / "00_INDEX.json")
    plan_doc_set = child_paths | {index_path}
    # ... later, when classifying dirty paths:
    if path in plan_doc_set:
        dirty["plan_doc"].append(path)
    elif ...
```

For `base_branch`: iterate children in roster order and use the first `**Base branch:**` declaration found. If no child declares one, fall back to `None` (same as today's no-plan-marker case).

Keep the output envelope schema unchanged; only the input pre-processing differs.

### TASK-003: `parse-plan-review-report` preserves `notes[]`

- **Status:** pending
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** none
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "parse_plan_review and notes"`
- **Acceptance criteria:**
  - `cmd_parse_plan_review_report` has TWO result-dict construction sites; both must emit `notes`:
    - **Success branch** at `plan_ops.py:3262-3271` adds `"notes": parsed.get("notes") or []`. Populated input `notes` pass through verbatim; empty/missing `notes` emit `[]`.
    - **Terminal-outcome branch** at `plan_ops.py:3222-3235` (covers `failure|timeout|parse_error|scope_violation`) adds `"notes": []`. Terminal envelopes have no `parsed` body so notes is always `[]` — the key is that it is always *present*, so downstream consumers never see KeyError.
  - Result shape now carries: `plan_file, outcome, verdict, findings_count, findings, notes, summary, schedule_ok, errors` (success) / `…, envelope_error` (terminal). Field ordering unchanged except for the new `notes` key, which is inserted adjacent to `findings`.
  - New test: feed a success envelope with `parsed.notes: ["n1", "n2"]` through the subcommand; assert the emitted JSON contains `"notes": ["n1", "n2"]`.
  - New test: feed a success envelope with `parsed.notes: []`; assert the emitted JSON contains `"notes": []`.
  - New test: feed a terminal envelope (`outcome: "failure"`, no `parsed`); assert the emitted JSON contains `"notes": []` (not missing, not null).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Two-line fix to the consumer-side gap in `e8945b0`. The validator already required `notes[]`; the emitter silently dropped it in both success and terminal branches. This task is pure plumbing — the downstream routing story (do orchestrator/SKILL.md consume `notes[]` for anything?) is explicitly out of scope. Surfacing the field is the precondition for any future routing; without that, the field is literally unreachable. The terminal-branch fix matters because TASK-004's mock path exercises the success branch, but real-world `codex_not_found` and `timeout` paths hit the terminal branch — emitting `notes: []` there keeps the contract uniform for any future code that does `result["notes"]` without guarding.

### TASK-004: End-to-end directory-mode plan-review smoke (in-process)

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [001, 002, 003]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "directory_mode_hotfix_smoke"`
- **Acceptance criteria:**
  - New test `test_directory_mode_hotfix_smoke` is **in-process** (no subprocess fork to the CLI), chaining the three touched command functions by direct import against `tests/fixtures/directory_mode_plan/` copied via the existing `directory_mode_sandbox` fixture.
  - Chain (each step's input / assertion):
    1. Call `plan_ops.cmd_preflight(args)` with `args.plan_file = <sandbox_dir>`. Assert the emitted result has `pass: true` and empty `source_blocking`. Exercises TASK-002.
    2. Build a minimal in-test schedule JSON (no fixture file changes): `{"tasks": [{"task_id": "001", "plan_file": "TASK-001_seed.md", …}, …]}` matching the fixture's three chunks; write it to `<sandbox_dir>/directory_mode_plan.schedule.json`. This synthesizes the sidecar the wrapper requires instead of shipping a new fixture file.
    3. Monkey-patch `plan_codex_dispatch.invoke_codex` to a fake that writes the canned envelope JSON to `output_path` and returns `{"status": "ok", "exit_code": 0, "stdout": "", "stderr": "", "file_changes": [], "wall_seconds": 0.01}`. Canned envelope content: `{"plan_file": "directory_mode_plan", "subcommand": "plan-review", "outcome": "success", "parsed": {"plan_file": "directory_mode_plan", "verdict": "approved-with-notes", "findings": [], "notes": ["cross-child parallelism ok"], "summary": "ok", "schedule_ok": true}}`.
    4. Call `plan_codex_dispatch.cmd_plan_review(args)` with `args.plan_file = <sandbox_dir>`, `args.schedule_file = <sandbox_dir>/directory_mode_plan.schedule.json`, `args.repo_root = <sandbox_dir>`. Capture the emitted envelope via the existing `capsys`/`emit`-capture idiom. Exercises TASK-001.
    5. Pipe the captured envelope JSON into `plan_ops.cmd_parse_plan_review_report(args)` (stdin-via-monkeypatch-pattern already used in other tests in the file). Assert the emitted result contains `"notes": ["cross-child parallelism ok"]`. Exercises TASK-003.
  - End-to-end assertion: the fake `invoke_codex` was called exactly once; the captured wrapper envelope has `outcome: "success"` (not `"failure"`); no `plan_review_skipped` event appears in the sandboxed `_run_log.jsonl`.
  - Mock boundary rationale: this is an in-process integration test, not a CLI-subprocess test. Monkey-patching `plan_codex_dispatch.invoke_codex` works because both test and target live in the same Python process. The orchestrator's actual subprocess-based CLI path is exercised by the pre-existing `test_directory_mode_*` suites at `test_plan_ops.py:13040+` (fixture/phase-0/batch-next coverage); TASK-004 is the scoped hotfix-specific smoke, not a full orchestrator re-test.
- **Reversion guidance:** `git restore tests/scripts/test_plan_ops.py`

**Description:**
In-process integration test that chains TASK-001's wrapper, TASK-002's preflight, and TASK-003's parse into one sequence against the shipped fixture. The chain tests the data flowing between the three fixes, which no single-task test can do alone. Regressions in any of TASK-001/002/003 that break the interface between them (e.g., TASK-001 emits a wrapper envelope shape that TASK-003 can't parse) would land silently without this test. The subprocess/CLI end-to-end is deliberately not tested here — that would double-mock at the subprocess boundary, require a schedule-file fixture, and cover ground the pre-existing `TestDirectoryMode*` suites already hold.

## Expected outcome

- Three `feat(TASK-NNN):` commits for the behavior fixes (TASK-001, TASK-002, TASK-003) plus one `feat(TASK-004):` for the integration test.
- `/implement-plan tests/fixtures/directory_mode_plan/ --dry-run` reaches `plan_review_done` with a real verdict, not `plan_review_skipped`.
- `notes[]` is emitted by `parse-plan-review-report` and is available for future routing decisions.
- Dual-mode prose in SKILL.md is unchanged — that's Phase 2 work.
- Pre-existing test failure (`test_analyst_to_parse_schedule_roundtrip`) remains untouched; no new failures.

## Follow-up — not in this plan

The longer-form refactor in `docs/plans/per_task_dispatch_refactor.md` is the architectural path to a single-track directory-only harness. After this hotfix lands, the refactor needs three corrections before it is executable (per the codex + gemini review of 2026-04-24):

1. **TASK-003 of the refactor (`build-tasks`) must also extract `acceptance_criteria`** from each child's `- **Acceptance criteria:**` bullet list into a string array on the emitted schedule task. Without AC text in the schedule, TASK-005's schedule-only plan-review loses the per-task AC sanity check that the current plan-review performs against plan markdown.
2. **TASK-008 of the refactor (mock envelope)** must match the full plan-review schema: `{plan_file, verdict, findings, notes, schedule_ok, summary}` — not the abbreviated `{verdict, findings}` the draft currently names.
3. **Latent bug to bundle into refactor TASK-003** (optional): `_compute_schedule_batches` at `plan_ops.py:299` ignores `task.dependencies` when building batches; runtime `batch-next._ready()` at `plan_ops.py:2631` is the actual dependency gate. The persisted `batches[]` are therefore advisory, not authoritative. Either make `_compute_schedule_batches` dependency-aware, or document this invariant explicitly in `plan_ops.py`'s module docstring.

## Execution log — 20260424T103924 (paused)

Starting SHA: `c92d42097c8a1c154ddca9806280968572cf9b26`  → Ending SHA: `c92d42097c8a1c154ddca9806280968572cf9b26`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-001 | claude | codex | needs-rework (re-review after narrow-remediation; D.5 partial-agreement on first review) | (paused) | [narrow-remediation] first pass OK on Finding 0 (basename); re-review re-flagged Finding 1 (partial reads) that D.5 dismissed; also flagged missing-chunk error path has no test coverage. |
| TASK-002 | claude | (not run) | (not run) | (paused) | Implementation succeeded and tests passed; review not dispatched because TASK-001 paused first in serial per-task review order. |
| TASK-003 | codex | (not run) | (not run) | (not run) | Batch 2 not started. |
| TASK-004 | claude | (not run) | (not run) | (not run) | Batch 3 not started (depends on 001,002,003). |
