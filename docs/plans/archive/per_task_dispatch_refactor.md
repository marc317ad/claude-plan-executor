# Per-Task Dispatch Refactor

**Created:** 2026-04-24
**Status:** pending
**Base branch:** main

## Goal

Reshape `/implement-plan` so every agent and wrapper dispatch targets exactly one decomposed child file. The orchestrator accepts decomposed directories only — single-file plans halt with a pointer to the `plan-decomposer` plugin. The directory path never appears as `--plan-file` on any subagent/wrapper call; it is used only as the `acquire-lock` key and as run-level log metadata.

## Context

Today's SKILL carries dual-track file-mode / directory-mode bindings, and the directory-mode branch still passes the directory path to `plan-analyst` and `plan-review` Codex. This leaks sibling-task visibility to implementers, forces `plan_codex_dispatch.py plan-review` and `plan_ops.py preflight` to support directory inputs they don't actually handle, and blocks per-task parallelism for classification.

The `plan-decomposer` plugin (separate track, see `docs/plans/build-plan-decomposer-plugin.md`) already produces the canonical `{00_INDEX.json, TASK-NNN_*.md}` shape: each TASK file is a self-contained mini-plan with its own `## Goal` / `## Context` / `## Verification` + a single `### TASK-NNN` block. `/implement-plan` can trust that contract and operate strictly per-child: read the roster, synthesize `tasks[]` from per-child metadata, dispatch implementer/reviewer/commit per child.

Whole-plan agent roles reshape as follows:

- **plan-analyst** → per-child classifier (parallel fan-out, one dispatch per child). DAG + batch computation moves to the orchestrator via `plan_ops.py compute-schedule`, which already exists.
- **plan-review Codex** → schedule-only review. The wrapper reviews the persisted schedule JSON — no plan markdown passes to Codex. Cross-task concerns that previously justified whole-plan review (DAG shape, file-disjointness, classification sanity) are all derivable from the schedule alone.
- **plan-review-triage / plan-author** → findings carry a `target_task_id`; dispatches resolve against that child file only.

The directory-as-argument surface survives only as: (a) the `/implement-plan <dir>` CLI input, (b) the `acquire-lock` key (one run-lock per directory-run), (c) the `plan_file: "<dir-basename>"` metadata on `run_start` / `run_end` events. Every other `--plan-file` argument resolves to a specific child.

## Verification

1. `/implement-plan docs/plans/<any-single-file>.md` halts immediately with a message naming the `plan-decomposer` plugin; no preflight runs, no lock acquired, no files mutated.
2. `/implement-plan tests/fixtures/directory_mode_plan/ --dry-run` runs end-to-end without any subagent / wrapper dispatch receiving the directory path as `--plan-file`. Run-log events carry `plan_file: "<dir-basename>"` only on run-level events (`run_start`, `run_end`); all per-task events carry `plan_file: "<child-basename>"`.
3. `plan_codex_dispatch.py plan-review --schedule-file <path>` (no `--plan-file` argument) reviews the schedule JSON and emits an envelope with `parsed.verdict ∈ {approved, approved-with-notes, needs-replan}`.
4. Per-child classifier fan-out: when at least one child omits `**Agent:**`, the orchestrator dispatches N parallel classifier agents in a single Agent tool-call batch, merges classifications into the synthesized `tasks[]`, and persists the schedule.
5. `git grep -n "file mode\|file-mode\|single.file plan\|fall back to the single-file plan" plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md` returns zero matches — dual-track language is gone.
6. `python3 plugins/plan-executor/scripts/plan_ops.py audit --json` passes; the `CANONICAL_CONTRACT` has been updated so no alias windows reference file-mode bindings.

## Tasks

### TASK-001: Strict directory-only entry gate

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
- **Dependencies:** []
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --json`
- **Acceptance criteria:**
  - SKILL.md Phase 0 detects `is_file()` input and halts with a literal message referencing the `plan-decomposer` plugin.
  - The dual-track "Directory-mode input" bindings table is removed — replaced by a single "Input shape" section stating decomposed-directory is the sole accepted form.
  - The "v1 scope boundaries for directory mode" callout is rewritten as standing design (no longer labeled v1).
  - No changes to `plan_ops.py` in this task — the gate is purely orchestrator-level.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md`

**Description:**
Close the file-mode ingress path at the orchestrator's Phase 0 seam. Single-file input currently falls through to `preflight` which then tries to classify `Files:` etc.; after this task it halts before any tool runs. Every downstream phase can assume `plan_path.is_dir()`, which lets subsequent tasks drop their file-mode branches without breaking behavior.

### TASK-002: Preflight directory branch

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [001]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k preflight`
- **Acceptance criteria:**
  - `cmd_preflight` accepts a directory path; detects via `plan.is_dir()`; reads `00_INDEX.json` + every child file; unions each child's `Files:` declarations for the `plan_scope_dirty` classifier.
  - `plan_doc` classification recognizes any `chunks[].file` basename as plan text (not source-blocking).
  - `base_branch` is read from any child that declares it (typically all children carry the same value; use the first non-null found).
  - New test: preflight against the shipped `tests/fixtures/directory_mode_plan/` fixture returns `pass: true`, no `source_blocking`, empty `plan_scope_dirty` on a clean tree.
  - New test: preflight against the same fixture with a tracked-dirty file inside one child's `Files:` list attributes the warning to the correct task id.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Replace the current `plan.is_file()` guard in `cmd_preflight` with a two-branch dispatch. File-branch behavior is unchanged but unreachable from `/implement-plan` after TASK-001 (still callable directly for legacy tooling until TASK-007 drops it). Directory branch walks the roster, reads children, and unions scope for the existing dirty-tree classifier. Output envelope shape is identical — the orchestrator consumes the same fields either way.

### TASK-003: Roster-driven `tasks[]` synthesis

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [002]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "build_tasks or BuildTasks"`
- **Acceptance criteria:**
  - New subcommand `plan_ops.py build-tasks --plans-dir <dir> --json` reads `00_INDEX.json` + each `chunks[].file` and emits `{tasks: [...], warnings: [...], errors: [...]}` matching the schedule's `tasks[]` wire format.
  - Per-task fields extracted: `id`, `title` (from the child's `### TASK-NNN: <title>` line), `files`, `dependencies` (from `**Dependencies:**`), `test_command`, `priority`, `plan_file` (the child basename), and `agent` iff the child carries `**Agent:**`.
  - Missing roster, malformed JSON, or a child file named in `chunks[]` that doesn't exist on disk surface as structured `errors[*]`.
  - New tests: fixture round-trip (fixture → `build-tasks` → passes `parse-schedule --stdin` with `outcome=valid`); missing-child, malformed-roster, cycle-in-dependencies cases.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Move `tasks[]` construction from the `plan-analyst` agent into a deterministic `plan_ops.py` subcommand. The analyst still owns the semantic "classify as claude vs codex" judgment (TASK-004), but the mechanical wire-up is now orchestrator-local. This unblocks skipping the analyst entirely when every child already carries `**Agent:**`.

### TASK-004: Per-child classifier fan-out

- **Status:** pending
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
  - plugins/plan-executor/agents/plan-analyst.md
- **Dependencies:** [003]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --json`
- **Acceptance criteria:**
  - SKILL.md Phase 1 is rewritten: orchestrator calls `build-tasks`, then iff any task lacks `agent`, fans out one classifier dispatch per missing-agent child (single Agent message, `subagent_type: "plan-analyst"`, `model: "opus"`).
  - New Phase A-single dispatch template in `dispatch-templates.md`: takes one child file path; instructs the agent to return a minimal JSON `{agent: "claude"|"codex", classification_reason: "..."}` — no full-schedule JSON.
  - `plan-analyst.md` agent spec documents the per-child classifier as the default invocation; whole-plan analysis is dropped or marked legacy-only.
  - Orchestrator merges classifier outputs into the `tasks[]` array, then runs `compute-schedule --stdin` to produce batches. No mid-pipeline schedule file exists until `write-schedule` at the end of Phase 1.
  - When every child already declares `**Agent:**`, Phase 1 skips the classifier entirely (no Agent dispatches).
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/agents/plan-analyst.md`

**Description:**
Replace the whole-plan analyst dispatch with a fan-out. Parallelism is a free by-product — N children classify in one Agent message. Gap detection and risk surfacing move into the per-child prompt; cross-task gaps (missing dependency targets, file-overlap conflicts) are caught by `compute-schedule`'s existing DAG + file-disjoint validators.

### TASK-005: Schedule-only plan-review

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
  - plugins/plan-executor/scripts/codex_plan_review_schema.json
  - tests/scripts/test_plan_codex_dispatch.py
- **Dependencies:** [003]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_codex_dispatch.py -q -k plan_review`
- **Acceptance criteria:**
  - `plan_codex_dispatch.py plan-review` drops the `--plan-file` argument; `--schedule-file` is the sole required input. Passing `--plan-file` prints a deprecation notice and ignores the value (one-version transition; removed in a follow-up).
  - The rendered Codex prompt contains only the schedule JSON + review instructions (DAG shape, file-disjointness, classification sanity, test-command reachability heuristics).
  - Envelope schema unchanged: `parsed.verdict ∈ {approved, approved-with-notes, needs-replan}`; `parsed.findings[*]` continue to carry `section` (now pointing at `tasks[i]` paths, e.g. `"tasks[002].test_command"`) and `suggested_change`.
  - `codex_plan_review_schema.json` updated if `parsed.plan_file` is no longer meaningful (likely replaced with `parsed.schedule_file`).
  - New test: dispatch against the fixture's persisted schedule returns `verdict=approved` or `approved-with-notes` with no `findings[*].severity=critical`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_codex_dispatch.py plugins/plan-executor/scripts/codex_plan_review_schema.json tests/scripts/test_plan_codex_dispatch.py`

**Description:**
Make plan-review schedule-only. Codex no longer needs to read plan markdown to validate the pre-dispatch gate; DAG + file-disjointness + classification-sanity are all derivable from the schedule alone. This eliminates the "directory as `--plan-file`" protocol gap by removing the argument entirely.

### TASK-006: Triage + author per-child targeting

- **Status:** pending
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
  - plugins/plan-executor/scripts/plan_ops.py
- **Dependencies:** [005]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "plan_review or triage or author"`
- **Acceptance criteria:**
  - `codex_plan_review_schema.json` `findings[*]` gains a required `target_task_id: string | null` field. Schedule-level findings (no specific task) use `null`.
  - `parse-plan-review-report` surfaces `target_task_id` alongside each finding.
  - Phase 1.5.5 triage dispatch template embeds per-finding `target_task_id` so the triage agent can reason about which child(ren) each finding impacts.
  - Phase 1.5a `plan-author` dispatch receives per-finding `{finding, target_task_id, child_plan_file}` triples and edits only the named child files (write-scope tightened — one child per finding).
  - Schedule-level findings with `target_task_id=null` still route to `plan-author` but with an explicit "schedule-level — no child file" note; the author edits the roster or no file (emits `files_edited: []` with a justification).
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/scripts/plan_ops.py`

**Description:**
Tighten the plan-review → triage → author chain to per-child scope. Each finding now names which task it targets; the author's write surface is correspondingly narrower. Schedule-level findings remain supported but are the exception, not the default.

### TASK-007: SKILL + dispatch-templates cleanup

- **Status:** pending
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
  - plugins/plan-executor/scripts/plan_ops.py
- **Dependencies:** [001, 002, 003, 004, 005, 006]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json`
- **Acceptance criteria:**
  - Every "In file mode: …" / "In directory mode: …" paired callout in SKILL.md and `dispatch-templates.md` is deleted. The remaining text reads as single-track directory-only prose.
  - The "Per-task `<plan-file>` resolution" section is simplified: `task.plan_file` is always set (no fallback to the single-file plan path).
  - `CANONICAL_CONTRACT` in `plan_ops.py` is updated so every entry reflects the directory-only shape; no `pass_with_alias` status for file-mode aliases remains.
  - `git grep -n "file mode\|file-mode\|single.file plan\|fall back to the single-file plan"` in the two files above returns zero matches.
  - `audit --strict --json` passes — no `fail` or `pass_with_alias` findings.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/scripts/plan_ops.py`

**Description:**
Final prose + canonical-contract sweep. By this point every behavior task has landed and the dual-track language is dead weight. Collapsing it makes the SKILL readable top-to-bottom as one protocol instead of two interleaved ones.

### TASK-008: End-to-end directory-mode smoke

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - tests/scripts/test_implement_plan_directory_smoke.py
- **Dependencies:** [007]
- **Test command:** `python3 -m pytest tests/scripts/test_implement_plan_directory_smoke.py -q`
- **Acceptance criteria:**
  - New test file drives the `/implement-plan` flow against `tests/fixtures/directory_mode_plan/` end-to-end in dry-run mode (no actual Codex dispatch — mock the wrapper to return `{verdict:"approved", findings:[]}`).
  - Assertions: (a) preflight passes; (b) `build-tasks` produces a schedule matching the roster; (c) per-child classifier fan-out fires when a fixture variant omits `**Agent:**` from a child; (d) schedule-only plan-review dispatch receives only `--schedule-file`; (e) dry-run logs show per-task `plan_file` metadata for every event after `run_start`.
  - The test is pure — no git writes, no network, no Codex invocation. Isolation via `tmp_path` + fixture copy.
- **Reversion guidance:** `git restore tests/scripts/test_implement_plan_directory_smoke.py; rm -f tests/scripts/test_implement_plan_directory_smoke.py`

**Description:**
Lock in the new directory-only path with an integration test that stays green as the refactor lands. The test uses the already-shipped fixture (no new fixtures required) and mocks the Codex wrapper boundary — the SKILL's behavior is deterministic once the wrapper returns a canned verdict.

## Expected outcome

- Eight `feat(TASK-NNN):` commits plus one `chore(implement-plan):` housekeeping commit.
- `/implement-plan` accepts only decomposed directories; single-file inputs halt with a pointer to the `plan-decomposer` plugin.
- Every `--plan-file` argument to any subagent or wrapper is a specific child basename — the directory path stays in the orchestrator.
- SKILL.md + `dispatch-templates.md` read as single-track directory-only protocol; dual-mode language is gone.
- `audit --strict --json` passes with no alias-windows referencing file-mode bindings.
