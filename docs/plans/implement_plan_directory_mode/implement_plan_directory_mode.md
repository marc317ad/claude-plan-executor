# Directory-mode execution for `/implement-plan` via per-task `plan_file` routing

**Created:** 2026-04-23
**Status:** pending
**Base branch:** main

## Goal

Teach `/implement-plan <dir>/` to run a decomposed plan directory as a single logical plan: one analyst pass, one Codex plan-review, one schedule, one run-lock, cross-child parallel batches where file-disjoint, and status mutations routed to the correct child file. Achieved by threading a per-task `plan_file` string through the schedule and substituting it at the five orchestrator write sites. No new subcommand. No concatenation. No change to `00_INDEX.json` schema or the Codex wrapper.

## Context

A decomposed plan lives at `<dir>/{TASK-NNN_*.md, 00_INDEX.json}`. The canonical 18-child shape is `docs/plans/decompose_plans_tasks/build-plan-decomposer-plugin/`. Today `/implement-plan` accepts a single file only; decomposed plans ship by invoking the executor once per child — 18 children = 18 invocations, each with its own redundant preflight / analyst / plan-review / lock / schedule, and zero cross-child parallelism.

The analyst, scheduler, dispatcher, reviewer, and commit-guard machinery is layout-agnostic: it operates on `task_id`, `files[]`, and `depends_on[]`. Where tasks physically live in markdown is irrelevant to scheduling, dispatch, review, and commit-safety. Only five subcommands write to plan markdown and therefore care which child holds a given task's `**Status:**` bullet:

1. `commit-task --plan-file <abs>` — D.3 narrow commit + status flip to `done`.
2. `fail-task --plan-file <abs>` — Phase C + D.4 status flip to `failed`.
3. `block-dependents --plan-file <abs>` — Phase C + D.4 cascade of `blocked` onto transitive dependents.
4. `update-plan-header --plan-file <abs>` — end-of-run plan-level header flip.
5. `plan-author` auto-revise dispatch — prose edits on `needs-replan` from Codex plan-review.

Four of these five (`commit-task`, `fail-task`, `update-plan-header`, `plan-author`) already accept `--plan-file` on every call and need no internal change — the orchestrator just passes the per-task value. The fifth (`block-dependents`) cascades across multiple dependents in a single invocation and currently assumes they all live in the one `--plan-file`; its internal loop needs to resolve each dependent's target file from the schedule.

Threading a per-task `plan_file: "<child-basename>"` through the schedule (validated in TASK-001, emitted by the analyst in TASK-003, consumed by the orchestrator in TASK-004) is the integration point. `block-dependents`'s multi-file cascade (TASK-002) is the only substantive internals change.

**In scope**

- `tasks[].plan_file` in the schedule wire format + validator + preservation through `compute-schedule` / `filter-schedule` / `batch-next` / `write-schedule`.
- `block-dependents` resolving each dependent's target file from the schedule.
- Plan-analyst subagent accepting a directory path as input and emitting a unified schedule.
- SKILL.md Phase 0 directory detection, per-child `schema-valid` loop, directory-keyed run-lock, per-task `plan_file` substitution at the five write sites, per-child `update-plan-header` at end-of-run.
- An integration fixture that exercises cross-child parallel batching and cross-child `block-dependents` cascade.

**Out of scope (v1 boundaries, each called out so the reviewer does not re-litigate)**

- Concatenating child markdown into a scratch monolith (keeps child files as the sole source-of-truth for status bullets).
- Changes to `00_INDEX.json` schema — the loader `_parse_index_roster` is reused as-is.
- Cross-directory `check-plan-deps` (v1 keeps today's one-roster semantics; `depends_on_plans` at the roster root is still advisory).
- Making `fixture-valid` directory-aware — the shipped `sample_phase4.md` fixture remains single-file and continues to certify the single-file path.
- A run-level aggregate execution-log table — v1 writes one §5 table per child plan file (the audit surface is per-child, as today).
- `--task-ids` cross-child filtering semantics beyond "globally unique id" (the analyst already rejects duplicate ids in TASK-003's acceptance criteria).

## Verification

1. **Single-file regression.** `/implement-plan docs/plans/sample_phase4.md` produces a byte-identical schedule, commit set, and run log to pre-change behavior. `plan_ops.py gates --certify --mode execute --run-id <id>` returns `certified: true`.
2. **Directory smoke (this plan against itself).** `/implement-plan docs/plans/implement_plan_directory_mode/ --dry-run` emits a schedule where each task carries `plan_file: "implement_plan_directory_mode.md"`. Phase 0 binds `<schedule_file>` to `docs/plans/implement_plan_directory_mode.schedule.json` and `<run_lock>` to the directory's absolute path. Phase 0 halts cleanly (`pass`) because the single child passes `schema-valid`.
3. **Cross-child parallel batching.** Against the TASK-004 integration fixture (3 children, file-disjoint TASK-002 + TASK-003 after TASK-001), the persisted schedule batches TASK-002 and TASK-003 together.
4. **Per-task `plan_file` substitution at write sites.** Wet-run against the fixture yields three `feat(TASK-NNN):` commits whose trailer `Plan: <basename>` names the correct child. `git log --name-only <run>` shows each commit modifying only its task's child file + the task's declared `Files:` list.
5. **Multi-file `block-dependents` cascade.** Injecting a failed TASK-001 in the fixture causes one `block-dependents` invocation to flip TASK-002's and TASK-003's `**Status:**` bullets in two distinct child files. Two `blocked` run-log events land, one per dependent, with correct `plan_file` attribution.
6. **End-of-run per-child header.** Partial run (one task failed): `update-plan-header` is invoked once per child present in completed+failed task set. Each affected child's top-level `**Status:**` bullet reflects that child's local outcome (`complete` if all its tasks passed, `partial` if any failed).
7. **Audit + gates.** `plan_ops.py audit --json` passes. `plan_ops.py gates --check schema-valid --plan-file <fixture-child>` passes for every child. `commit-safe` passes per commit.

## Tasks

### TASK-001: Schedule carries per-task `plan_file`

- **Status:** open
- **Priority:** high
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "schedule_plan_file" -x`
- **Acceptance criteria:**
  - `_validate_schedule` accepts an optional `plan_file` string per task entry. When present, value must be a non-empty string with POSIX-portable basename semantics: no `/`, no `\`, no `..`, no leading dot, no NUL byte, length ≤ 255 bytes.
  - When absent, the field is treated as "inherits the run's single plan file" — back-compat path for every existing single-file schedule.
  - Passthrough is verified byte-identically across `compute-schedule`, `filter-schedule`, `batch-next`, and `write-schedule`: each subcommand reads every `plan_file` it receives and emits it unchanged on every surviving task entry.
  - `parse-schedule` echoes `plan_file` in its output for observability.
  - Validator rejects with `{code: "invalid-plan-file", path: "$.tasks[i].plan_file"}`: non-string, empty string, value containing `/` or `\` or `..`, value starting with `.`, values > 255 bytes.
  - Tests cover: accept matrix (valid basenames, missing field, mixed presence across tasks in one schedule), reject matrix (each prohibited form), `compute-schedule` passthrough, `filter-schedule` preservation on both directly-filtered and transitive-dep entries, `batch-next` preservation, `write-schedule` round-trip.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Pure schema-addition. No caller is gated on the new field yet; this task just lets it ride through the validator and the four schedule-mutating subcommands so TASK-002, TASK-003, and TASK-004 can assume the wire format is stable. The rejection list is the standard POSIX-basename containment rule — we never want a schedule to carry a path that could escape the plan directory at write time.

**Implementation notes:**
`_validate_schedule` lives around line 542 of `plan_ops.py` (locate via `grep -n "^def _validate_schedule"` rather than trusting line numbers). The basename predicate belongs next to it as a small helper — do not reuse `os.path.basename`, which silently accepts path separators. A dedicated regex or character-class check is the cheapest correct option.

### TASK-002: `block-dependents` multi-file cascade

- **Status:** open
- **Priority:** high
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "block_dependents_multi_file" -x`
- **Acceptance criteria:**
  - `cmd_block_dependents` resolves each transitive-dependent's target file from the schedule's `tasks[].plan_file` when present. When absent on a given dependent, falls back to the `--plan-file` argument (unchanged single-file behavior).
  - Multi-file cascade: failed task in `child-a.md` with transitive dependents in `child-b.md` and `child-c.md` produces three distinct atomic writes (one per file), each preserving the per-file tempfile + `os.replace` idempotency contract already used by `block-dependents`.
  - Emits one `blocked` run-log event per dependent; each event carries `plan_file` in its fields dict so the audit trail records which child was mutated.
  - Mixed schedule — some dependents carry `plan_file`, some do not — routes per-dependent and does not drop either cohort.
  - A `plan_file` value that does not resolve under `--plan-file`'s parent directory halts with `{code: "dependent-file-not-in-plan-dir", path: "$.tasks[i].plan_file"}` before any file is written. No partial cascade.
  - A `plan_file` value that resolves but whose referenced task-id block is not found inside it halts with `{code: "dependent-block-missing", path: "$.tasks[i]"}` — same pre-write halt.
  - Tests cover: (a) single-file unchanged (back-compat), (b) two-file cascade, (c) three-file cascade with one dependent in each, (d) mixed-schedule fallback to `--plan-file`, (e) unresolvable `plan_file` (escape attempt), (f) resolvable `plan_file` but missing task block.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
The only subcommand among the five write sites that needs internal-logic changes. `commit-task`, `fail-task`, and `update-plan-header` each mutate one plan file per call and already accept `--plan-file` — the orchestrator handles per-task routing in TASK-004 by passing the right value. `block-dependents` is different: one invocation can flip many dependents' bullets, and in directory mode those dependents span multiple files. Its cascade needs to resolve each dependent's file individually; the pre-write containment check mirrors TASK-001's POSIX-basename rule but resolves against `<plan-file>.parent` (the plan directory) so an escape attempt halts loudly before any mutation.

**Implementation notes:**
`cmd_block_dependents` lives around line 3426 (locate via `grep -n`). The existing per-file atomic-write helper is already in place; the change is at the loop level — replace the single-file assumption with a `dict[plan_file_path, list[dependent_id]]` grouping, iterate per group, one atomic write per group. The `--plan-file` arg stays required (it names the failed task's own file, which is still needed for the schedule DAG lookup and for back-compat).

### TASK-003: Plan-analyst directory-mode input

- **Status:** open
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/agents/plan-analyst.md
- **Dependencies:** TASK-001
- **Test command:** none
- **Acceptance criteria:**
  - Prompt documents a new input contract: the analyst accepts either a single `.md` file path (existing behavior) or a directory path containing `00_INDEX.json` + one-or-more child `.md` files (new).
  - Directory-input flow: (1) read `<dir>/00_INDEX.json`, (2) read every child plan file named in `chunks[].file`, (3) extract `### TASK-NNN` blocks from every child, (4) emit a unified `tasks[]` in which every entry carries `plan_file: "<child-basename>"` (basename only — NOT an absolute path; the orchestrator resolves against `<dir>` at write time), (5) emit `batches[]` derived from the union of per-child `**Dependencies:**` bullets AND roster `chunks[].depends_on`.
  - Conflict rule: when a task's `**Dependencies:**` bullet disagrees with its roster `depends_on` entry, the roster wins AND the analyst emits a `diagnostics[]` entry with `{code: "dep-conflict", task_id, bullet: [...], roster: [...]}`. The analyst still emits `outcome: valid` — this is a warning, not a structural defect.
  - Duplicate task-id rule: a `task_id` that appears in two distinct child files halts with `outcome: invalid` and a `diagnostics[]` entry `{code: "duplicate-task-id", task_id, files: [basename_a, basename_b]}`.
  - Single-file input is unchanged: existing behavior preserved byte-identically. The analyst MAY emit `plan_file: "<input-basename>"` on every task for uniformity (TASK-001's validator accepts it either way), or omit it entirely — both are compatible.
  - Outcome vocabulary (`valid` / `needs-enrichment` / `invalid`), gap severity, schedule shape top-level, and all other analyst contracts are preserved exactly.
- **Reversion guidance:** `git restore plugins/plan-executor/agents/plan-analyst.md`

**Description:**
Documentation-only edit to the subagent prompt. The schedule wire format validated in TASK-001 is the sole interop point between analyst and orchestrator, so this task's only job is teaching the analyst to read a directory + roster and emit the new field correctly. No `plan_ops.py` changes. The roster-wins conflict rule matches what a careful human plan author would do — the `00_INDEX.json` is the topology source of truth, per-file `**Dependencies:**` bullets are the human-readable narrative; when they drift, the topology wins and the drift is surfaced so it can be fixed.

**Implementation notes:**
The analyst subagent reads files directly (it has the Read tool). `00_INDEX.json` shape is documented in `_parse_index_roster` in `plan_ops.py` — keep the prompt's loader description short and reference the existing loader as the authoritative parser.

### TASK-004: Orchestrator directory-mode driver

- **Status:** open
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - tests/fixtures/directory_mode_plan/00_INDEX.json
  - tests/fixtures/directory_mode_plan/TASK-001_seed.md
  - tests/fixtures/directory_mode_plan/TASK-002_write_a.md
  - tests/fixtures/directory_mode_plan/TASK-003_write_b.md
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001, TASK-002, TASK-003
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "directory_mode" -x`
- **Acceptance criteria:**
  - SKILL.md Phase 0 detects directory input via `pathlib.Path.is_dir()` semantics (no stringly checks on trailing slashes). When directory: bind `<schedule_file>` to `<plan_dir>/<dir-basename>.schedule.json`, bind the analyst's input to the directory path, and acquire the run-lock against the directory's absolute path (one lock per directory-run, not one per child).
  - Phase 0 `schema-valid` iterates every `chunks[].file` in `<dir>/00_INDEX.json` and halts on the first failure, naming the offending child file in the halt message.
  - Phase 0 `check-plan-deps` runs once against the directory's roster with current single-file semantics (v1 scope boundary, documented in the SKILL).
  - Phase 1 analyst dispatch passes the directory path. Phase 1.5 Codex plan-review dispatch passes `--plans-dir <dir>` and the persisted schedule; review semantics unchanged.
  - Orchestrator write sites substitute `<plan-file>` with the current task's `plan_file` resolved against `<dir>`. Sites updated: `commit-task` (D.3), `fail-task` (Phase C + D.4), `block-dependents` (Phase C + D.4), `plan-author` auto-revise dispatch (Phase 1.5 `needs-replan` branch). Each site's command-line remains otherwise unchanged.
  - End-of-run: `update-plan-header` iterates the distinct `plan_file` values present in completed + failed tasks. Each child's own top-level `**Status:**` flips to `complete` when every task in that child passed, `partial` otherwise. A run-level aggregate header is NOT synthesized (v1 boundary, documented).
  - `finalize-execution-log` appends one §5 table to each distinct child plan file, scoped to that child's tasks. No run-level aggregate table (v1 boundary, documented).
  - Run-log events (`run_start`, `batch_start`, `implement_start`, `commit_done`, `run_end`) carry `plan_file` in `fields` for directory-mode tasks; single-file runs emit existing shape unchanged.
  - File input path remains byte-identical to pre-TASK-004 behavior: when `<plan-path>` resolves to a file, none of the new branches fire.
  - Integration fixture at `tests/fixtures/directory_mode_plan/`: three minimal child plans + `00_INDEX.json`. TASK-001 seeds a scratch directory (no deps). TASK-002 writes `scratch/a.txt` (depends on TASK-001). TASK-003 writes `scratch/b.txt` (depends on TASK-001, disjoint files from TASK-002). Each child is a single-task plan passing `schema-valid`.
  - Integration test verifies: (a) analyst emits per-task `plan_file`, (b) `batch-next` batches TASK-002 + TASK-003 together, (c) `commit-task` flips the right child's header, (d) multi-file `block-dependents` when TASK-001 is seeded to fail.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py && rm -rf tests/fixtures/directory_mode_plan`

**Description:**
The integration point where TASK-001 / TASK-002 / TASK-003 come together. SKILL.md edits are prose-heavy but localized: one new Phase 0 branch for directory detection + lock/schedule binding, one per-child `schema-valid` loop, five `<plan-file>` substitution sites in Phase C / D / end-of-run. The fixture exercises the payoff: two sibling children's tasks running in parallel because they write disjoint files, which is the thing the prior single-child-at-a-time design could not do.

**Implementation notes:**
Phase 0 pseudocode sketch (SKILL.md will render this as prose):

```
plan_path = Path(argv)
if plan_path.is_dir():
    mode = "directory"
    plans_dir = plan_path
    schedule_file = <plan_dir> / f"{plan_path.name}.schedule.json"
    run_lock_key = str(plan_path.resolve())
    for chunk in load_00_index(plan_path)["chunks"]:
        assert gates_schema_valid(plans_dir / chunk["file"])
else:
    mode = "file"
    plans_dir = plan_path.parent
    schedule_file = <plan_dir> / f"{plan_path.stem}.schedule.json"
    run_lock_key = str(plan_path.resolve())
    assert gates_schema_valid(plan_path)
```

At every write site, resolve `task.plan_file` (basename) against `plans_dir` to get the absolute path; if the schedule entry omits `plan_file`, fall back to the single-file `plan_path`. Both paths flow through the same five commands — the only difference is which `<plan-file>` they receive.

## Expected outcome

- Four `feat` commits (`feat(TASK-001)` through `feat(TASK-004)`) plus one `chore(implement-plan):` housekeeping commit.
- `/implement-plan <directory>/` runs a decomposed plan as one unified run: one analyst pass, one Codex plan-review, one schedule, one run-lock, cross-child parallel batches where disjoint.
- Plan-level `**Status:**` flips to `complete`.
