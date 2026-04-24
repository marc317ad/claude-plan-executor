# Per-Task Dispatch Refactor (v2 — post-hotfix)

**Created:** 2026-04-24
**Supersedes:** `docs/plans/archive/per_task_dispatch_refactor.md` (2026-04-24 v1)
**Status:** pending
**Base branch:** main
**Preconditions:** `directory_mode_hotfix_2026-04-24` has landed (wrapper + preflight accept dir inputs; `parse-plan-review-report` emits `notes[]`). This plan refactors the architecture; it does not fix the defects.

## Goal

Reshape `/implement-plan` so every agent and wrapper dispatch targets exactly one decomposed child file. The orchestrator accepts decomposed directories as the canonical input; single-file plans are transparently auto-promoted to directory mode via a heuristic Python subcommand (`plan_ops.py decompose-plan`) before any downstream phase runs. The directory path never appears as `--plan-file` on any subagent/wrapper call; it is used only as the `acquire-lock` key and as run-level log metadata.

## Context

The hotfix closed three symptomatic defects (wrapper protocol hole, preflight file-only guard, `notes[]` consumer leak), unblocking `/implement-plan <dir>`. It did not simplify the surface. SKILL.md still carries dual-track file-mode / directory-mode bindings, `plan-review` Codex still receives a directory path as `--plan-file`, and `tasks[]` construction still happens inside the whole-plan analyst — which prevents per-child parallelism and leaks sibling-task visibility to implementers.

Decomposition is fundamentally a text-splitting operation: plan markdown follows a strict template (`## TASK-NNN: Title` whole-plan headings OR `### TASK-NNN: Title` child-plan sub-headings, typed metadata lines, bulleted AC, backtick-delimited test commands). A heuristic Python parser is sufficient — no LLM is required. This plan adds a `plan_ops.py decompose-plan` subcommand that produces the canonical `{00_INDEX.json, TASK-NNN_*.md}` shape deterministically, and wires SKILL.md Phase 0 to invoke it transparently when input is a file. From the skill's perspective there is only one mode; file inputs become directory inputs before Phase 0 yields.

Whole-plan agent roles reshape:

- **plan-analyst** → per-child classifier (parallel fan-out: N discrete Agent tool calls emitted in a single assistant turn, one per child). DAG + batch computation stays in the orchestrator via the existing `plan_ops.py compute-schedule`.
- **plan-review Codex** → schedule-only review. The wrapper reviews the persisted schedule JSON — no plan markdown passes to Codex. This requires the schedule to carry per-task description + acceptance_criteria so the reviewer can validate intent, not just wire shape.
- **plan-review-triage / plan-author** → findings carry a `target_task_id`; dispatches resolve against that child file only.

The directory-as-argument surface survives only as: (a) the `/implement-plan <dir>` CLI input, (b) the `acquire-lock` key (one run-lock per directory-run), (c) the `plan_file: "<dir-basename>"` metadata on `run_start` / `run_end` events. Every other `--plan-file` argument resolves to a specific child.

### Corrections folded in from v1 archive + v2 review passes

1. **TASK-001 is heuristic decomposition (not a plugin dependency).** The archived v1 plan halted single-file input with a pointer to a not-yet-built `plan-decomposer` plugin. v2 replaces that cross-track dependency with a deterministic Python subcommand inside `plan_ops.py`. File inputs are auto-promoted to directory mode before the skill ever sees them. The `plan-decomposer` plugin (separate track, `docs/plans/build-plan-decomposer-plugin.md`) is no longer a Phase 2 precondition — it can ship later as a user-facing alias if wanted.
2. **Shared parsing helpers are a TASK-001 deliverable, not a TASK-004 afterthought.** TASK-001 extracts `_parse_task_block`, `_extract_bullet_list`, `_extract_metadata_field` (or equivalent) into reusable helpers; TASK-004's `build-tasks` imports them. This prevents grammar drift between decompose-plan (reads whole-plan `## TASK-NNN:`) and build-tasks (reads child-plan `### TASK-NNN:`) — both consume the same primitives over the same token classes.
3. **TASK-004 emits a "fat" manifest.** `build-tasks` extracts per-child `description` and `acceptance_criteria` text into the schedule. Without this, TASK-006's schedule-only plan-review has no task intent to validate.
4. **TASK-007 schema change is backward-compatible.** `target_task_id` is added as `string | null` to `codex_plan_review_schema.json` (not in `required`; `additionalProperties: false` is relaxed or extended to include the field). Default is `null` (schedule-level finding, no specific child). Old Codex outputs without the field are accepted for one version via a migration gate in `parse-plan-review-report`.
5. **`notes`-field gap is already closed.** v2 pass-2 review initially flagged a terminal-outcome gap in `cmd_parse_plan_review_report`; pass-3 verification against HEAD (lines 3320 and 3360) confirms both result-dict sites already emit `notes: []`. No plan task required. **`notes[]` does NOT exist on findings** — the schema restricts it to top-level `parsed.notes` only with `additionalProperties: false` on findings. TASK-006's AC reflects this.
6. **TASK-002 / TASK-003 framed as architectural deletions, not defect fixes.** Hotfix already taught the wrapper + preflight about directories; Phase 2 removes the file-mode branches those now-unreachable code paths still carry.
7. **Directory-mode sidecar convention is IN-directory.** Per `_plan_paths.py:69`, the per-plan schedule sidecar for a directory plan lives at `<plan_dir>/<dir-stem>.schedule.json` (inside the directory, filename = dir stem + `.schedule.json`). v2 verification and TASK-009 use this convention — NOT a sibling-of-parent layout, which would conflict with protected-path logic, SKILL bindings, and existing tests.
8. **TASK-001 / TASK-002 split: code vs prose.** TASK-001 adds the `decompose-plan` subcommand, the shared parsing helpers, and the minimum SKILL.md Phase 0 wire-up to invoke it (one concise note pointing forward to TASK-002 for the prose consolidation). TASK-002 owns all dual-mode prose deletion and single-track "Input shape" section rewrite. No overlap in the touched SKILL.md regions.
9. **Pre-existing test failure in `test_analyst_to_parse_schedule_roundtrip` (test_plan_ops.py:5194)** is unrelated to this plan but will surface during Phase 2 CI. Flagged as a follow-up — not blocking, but worth a surgical fix before Phase 2 runs to clean up the baseline.

## Verification

1. `/implement-plan docs/plans/<any-single-file>.md` auto-promotes: the orchestrator invokes `plan_ops.py decompose-plan --plan-file <input>` as a Phase 0 preamble, producing `docs/plans/<stem>/{00_INDEX.json, TASK-NNN_*.md}`; all downstream phases operate on the produced directory. Run-log `decompose_auto_promote` event records source file and produced directory paths.
2. `/implement-plan tests/fixtures/directory_mode_plan/ --dry-run` runs end-to-end without any subagent / wrapper dispatch receiving the directory path as `--plan-file`. Run-log events carry `plan_file: "<dir-basename>"` only on run-level events (`run_start`, `run_end`); all per-task events carry `plan_file: "<child-basename>"`.
3. `plan_codex_dispatch.py plan-review --schedule-file tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json` (no `--plan-file` argument; sidecar IN the plan directory per `_plan_paths.py` convention) reviews the schedule JSON and emits an envelope with `parsed.verdict ∈ {approved, approved-with-notes, needs-replan}`.
4. Per-child classifier fan-out: when at least one child omits `**Agent:**`, the orchestrator dispatches N discrete Agent tool calls in a single assistant turn (not an array-prompt in one call), merges classifications into the synthesized `tasks[]`, and persists the schedule.
5. `git grep -n "file mode\|file-mode\|single.file plan\|fall back to the single-file plan" plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md` returns zero matches — dual-track language is gone.
6. `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json` passes; the `CANONICAL_CONTRACT` reflects directory-only shape, no `pass_with_alias` for file-mode aliases; `decompose-plan` is a first-class subcommand entry.
7. Schedule JSON for the fixture (persisted at `tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json` — IN the plan directory per sidecar convention) carries `tasks[i].description` (non-empty string) and `tasks[i].acceptance_criteria` (non-empty list) for every task. Plan-review Codex prompt includes these fields verbatim.
8. `plan_ops.py decompose-plan --plan-file <file.md>` is idempotent under `--force`: rerunning against an already-decomposed directory produces byte-identical output. Malformed inputs (missing headers, duplicate ids, unresolvable deps, cycles) surface structured `errors[*]` with source line numbers and non-zero exit code.
9. Shared parsing helpers (from TASK-001) have a single test module exercising them directly; both `decompose-plan` and `build-tasks` tests reuse the same expectation fixtures for the atomic parsing primitives — any grammar drift shows up as a failing primitive test, not as mismatched outputs downstream.

## Tasks

### TASK-001: Heuristic plan-decomposition subcommand + shared parsing helpers

- **Status:** done
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase 0 invocation note only — full prose cleanup is TASK-002)
  - tests/scripts/test_plan_ops.py
  - tests/fixtures/decomposer_inputs/ (create — canonical + malformed markdown fixtures)
- **Dependencies:** []
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "decompose_plan or DecomposePlan or parse_task_block"`
- **Acceptance criteria:**
  - Shared parsing helpers are extracted into reusable, testable functions in `plan_ops.py` (names illustrative): `_parse_task_block(markdown, level) -> dict` (where `level=2` for whole-plan `## TASK-NNN:`, `level=3` for child `### TASK-NNN:`); `_extract_metadata_field(block, key) -> str`; `_extract_bullet_list(block, heading) -> list[str]`. These helpers are the single source of truth for markdown → task-dict conversion; TASK-004's `build-tasks` imports them.
  - New subcommand `plan_ops.py decompose-plan --plan-file <file.md> [--out-dir <dir>] [--force]` reads a single markdown plan (whole-plan grammar: `## TASK-NNN: Title` headings) and emits a sibling directory of the canonical shape: `{00_INDEX.json, TASK-NNN_<slug>.md, ...}`.
  - **Child-file emission grammar is pinned.** Each `TASK-NNN_<slug>.md` output carries a `### TASK-NNN: <title>` sub-heading (H3, not H2) followed by the standard metadata block and sections (`**Status:**`, `**Priority:**`, `**Agent:**` (iff present in source), `**Files:**`, `**Dependencies:**`, `**Test command:**`, `**Acceptance criteria:**`, `**Reversion guidance:**`, `**Description:**`). This matches TASK-004's `build-tasks` input grammar exactly — both subcommands consume the same helpers against `level=3`.
  - Default `--out-dir` is `<file-parent>/<file-stem>/` (e.g., `docs/plans/foo.md` → `docs/plans/foo/`). Refuses to overwrite if target exists and is non-empty, unless `--force` is passed.
  - Parser is heuristic (regex + bullet extraction), NOT LLM-driven. No network calls. <100ms wall-clock for a 10-task plan.
  - Per-task extraction (via shared helpers): `id`, `title`, `priority`, `depends_on` (list), `test_command`, `agent` (iff present; else omitted), `files`, `description`, `acceptance_criteria` (ordered `string[]`).
  - Emits `00_INDEX.json` matching the existing manifest schema (same shape as `docs/plans/directory_mode_hotfix_2026-04-24/00_INDEX.json`). `parallel_batches` computed via topo-sort from per-task `depends_on`; cycles surface as structured errors.
  - Idempotent under `--force`: re-running against an already-decomposed directory produces byte-identical output.
  - Malformed plans error loudly (non-zero exit, structured `errors[*]` with source line numbers): missing `## TASK-NNN:` headers, duplicate ids, missing required metadata (priority, test_command), unresolvable dependency ids, cyclic dependencies. No LLM fix-up.
  - SKILL.md Phase 0 integration (scoped narrowly — no prose cleanup here): detect `is_file()` input, invoke `plan_ops.py decompose-plan --plan-file <input>`, capture the produced directory path, proceed with the existing directory-mode flow. Emit `decompose_auto_promote` run-log event with `{source_file, produced_dir, task_count}`. The orchestrator does NOT read the decomposed child files into its context. A one-sentence forward-pointer to TASK-002 for full "Input shape" prose consolidation is acceptable; the bulk of the dual-mode prose removal is deferred.
  - Dispatch invariant: the produced directory is treated identically to a user-authored decomposed directory. Run-log `plan_file` metadata on `run_start` reflects the directory basename (post-decomposition), not the original file.
  - New test fixtures under `tests/fixtures/decomposer_inputs/`:
    - `canonical.md` (well-formed 3-task whole-plan)
    - `missing_metadata.md` (task missing `**Priority:**`)
    - `duplicate_ids.md` (two `## TASK-001:` headings)
    - `unresolvable_deps.md` (task depends on non-existent id)
    - `cyclic_deps.md` (A → B → A)
  - New tests:
    - shared-helper unit tests: `_parse_task_block`, `_extract_metadata_field`, `_extract_bullet_list` each exercised directly with fixture inputs and expected outputs.
    - canonical fixture → round-trip produces valid `00_INDEX.json` + N child files; each child carries the `### TASK-NNN:` sub-heading and all required metadata sections.
    - decompose → build-tasks round-trip: the canonical fixture decomposed then passed through `build-tasks` (TASK-004) produces a valid fat schedule (`outcome=valid`, every `tasks[i].description` non-empty, `tasks[i].acceptance_criteria` is a non-empty list).
    - force-rerun is idempotent (byte-identical file contents + directory listing).
    - each malformed fixture produces its specific structured error with a correct source line number.
    - orchestrator smoke: `/implement-plan <file.md>` in dry-run mode triggers `decompose_auto_promote` event and proceeds as if a directory were passed.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md tests/scripts/test_plan_ops.py; rm -rf tests/fixtures/decomposer_inputs/`

**Description:**
Replace the archived v1 TASK-001 "halt with pointer to plan-decomposer plugin" approach with a deterministic in-repo subcommand plus the shared parsing primitives that TASK-004 will also consume. Extracting the helpers here (not later) is load-bearing: `decompose-plan` and `build-tasks` parse the same token classes at different heading levels, and drift between them would regress any plan round-trip. The helpers are the contract that keeps them aligned.

**Execution mode.** The subcommand is standalone-runnable (users can invoke it directly to preview the decomposition, or decompose plans offline for inspection) AND invoked transparently by SKILL.md Phase 0 when input is a file. The Python transformation runs outside LLM context; the orchestrator only sees the resulting directory path. Context overhead is bounded by the `ls` of the produced directory — the orchestrator does not read individual child files upfront. For plans large enough that authoring already strains context, users can run `decompose-plan` as a separate CLI step before invoking `/implement-plan <dir>`; both inputs are handled identically downstream.

**Scope boundary vs TASK-002.** This task touches SKILL.md only at Phase 0, adding the minimal invocation logic + a forward-pointer sentence. The dual-mode bindings table, "Input shape" section rewrite, and all "In file mode: … / In directory mode: …" callout deletions are TASK-002's scope. No text region is edited by both tasks.

### TASK-002: SKILL prose consolidation (single-track "Input shape")

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
- **Dependencies:** [001]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --json`
- **Acceptance criteria:**
  - SKILL.md "Directory-mode input" bindings table is removed — replaced by a single "Input shape" section stating "decomposed directory" is the canonical form and that file inputs are transparently auto-promoted via `plan_ops.py decompose-plan` (TASK-001) before Phase 0 yields.
  - The "v1 scope boundaries for directory mode" callout is rewritten as standing design (no longer labeled v1).
  - Dual-track "In file mode: … / In directory mode: …" callouts throughout SKILL.md (not in `dispatch-templates.md` yet — that's TASK-008) are replaced with single-track prose.
  - The Phase 0 auto-promotion step (wired in TASK-001) is documented as a named precondition in the "Input shape" section so readers understand why all downstream phases see a directory.
  - The TASK-001 forward-pointer sentence is removed (now that this task lands the prose consolidation).
  - No `plan_ops.py` changes in this task — pure SKILL prose. File-mode code paths in `plan_ops.py` remain reachable via direct CLI calls until TASK-008 removes them.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md`

**Description:**
With auto-promotion landed in TASK-001, every downstream phase can assume `plan_path.is_dir()`. This task retires dual-mode prose and callouts from SKILL.md, replacing them with single-track directory-only language. Deletes the largest source of "context tax" in the skill (the paired file-mode / directory-mode callouts) without changing any dispatch behavior. `dispatch-templates.md` cleanup is deferred to TASK-008 alongside the file-mode code removal, to keep the prose changes reviewable in a single sweep. No touch-region overlap with TASK-001's Phase 0 wire-up.

### TASK-003: Preflight directory branch (remove file-branch fallthrough)

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [002]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k preflight`
- **Acceptance criteria:**
  - `cmd_preflight` directory branch (added in hotfix TASK-002) becomes canonical. The file-branch code path remains for now (legacy callers) but is marked deprecated in a one-line comment referencing TASK-008.
  - `plan_doc` classification recognizes any `chunks[].file` basename as plan text (not source-blocking) — verify this is already in the hotfix output.
  - `base_branch` is read from any child that declares it; first non-null wins.
  - New test: preflight against the shipped `tests/fixtures/directory_mode_plan/` fixture with a tracked-dirty file inside one child's `Files:` list attributes the warning to the correct task id (hotfix test covers clean tree; this adds dirty-tree coverage).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
The hotfix taught preflight about directories. This task hardens the directory branch with the remaining dirty-tree coverage and marks the file branch as deprecated (removal in TASK-008). Minimal code change — mostly tests + a deprecation comment.

### TASK-004: Roster-driven fat `tasks[]` synthesis

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [003]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "build_tasks or BuildTasks"`
- **Acceptance criteria:**
  - New subcommand `plan_ops.py build-tasks --plans-dir <dir> --json` reads `00_INDEX.json` + each `chunks[].file` and emits `{tasks: [...], warnings: [...], errors: [...]}` matching the schedule's `tasks[]` wire format.
  - **Input grammar uses the `### TASK-NNN: <title>` child sub-heading convention** (produced by TASK-001's `decompose-plan` and used by hand-authored child plans). Parsing reuses the shared helpers introduced in TASK-001 (`_parse_task_block` with `level=3`, `_extract_metadata_field`, `_extract_bullet_list`). No parsing primitives are re-implemented here.
  - Per-task fields extracted: `id`, `title`, `files`, `dependencies`, `test_command`, `priority`, `plan_file` (child basename), `agent` (iff child carries `**Agent:**`), **`description`** (paragraph text following `**Description:**` heading, trimmed), **`acceptance_criteria`** (bulleted list following `**Acceptance criteria:**`, preserved as ordered `string[]`).
  - `description` is a single string (multi-line allowed); `acceptance_criteria` is `string[]` preserving bullet order.
  - Missing roster, malformed JSON, or a child file named in `chunks[]` that doesn't exist on disk surfaces as structured `errors[*]`.
  - Missing `**Description:**` or `**Acceptance criteria:**` in a child surfaces as `warnings[*]` with task_id (non-fatal — plan-review will flag downstream).
  - New tests:
    - fixture round-trip (`tests/fixtures/directory_mode_plan/` → `build-tasks` → passes `parse-schedule --stdin` with `outcome=valid`; `tasks[0].description` is non-empty; `tasks[0].acceptance_criteria` is a non-empty list)
    - decompose → build-tasks round-trip: decompose `tests/fixtures/decomposer_inputs/canonical.md`, then run `build-tasks` on the produced directory; output is a valid fat schedule
    - missing-child
    - malformed-roster
    - cycle-in-dependencies
    - missing-description-in-child (warning, not error)
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Move `tasks[]` construction from the `plan-analyst` agent into a deterministic `plan_ops.py` subcommand. The analyst still owns the semantic "classify as claude vs codex" judgment (TASK-005), but the mechanical wire-up is now orchestrator-local. Crucially, this task emits a **fat** manifest — including per-task `description` and `acceptance_criteria` text — so that TASK-006's schedule-only plan-review has the context it needs to validate task intent. Parsing reuses TASK-001's shared helpers against the child `### TASK-NNN:` grammar — no drift risk vs `decompose-plan` because both subcommands are thin wrappers over the same primitives.

### TASK-005: Per-child classifier fan-out

- **Status:** done
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
  - plugins/plan-executor/agents/plan-analyst.md
- **Dependencies:** [004]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --json`
- **Acceptance criteria:**
  - SKILL.md Phase 1 is rewritten: orchestrator calls `build-tasks`, then iff any task lacks `agent`, fans out one classifier dispatch per missing-agent child.
  - **Parallel-dispatch semantics pinned explicitly:** the fan-out is emitted as N discrete `Agent` tool calls inside a single assistant turn (multiple tool-use blocks in one response), NOT as an array-prompt wrapped inside a single Agent tool call. SKILL.md includes an inline example (pseudo-syntax): `[Agent(child=A), Agent(child=B), ...]` within one turn. This matches the existing Agent-tool contract in the parent agent's API — no new tool-call shape is introduced.
  - New Phase A-single dispatch template in `dispatch-templates.md`: takes one child file path; instructs the agent to return a minimal JSON `{agent: "claude"|"codex", classification_reason: "..."}` — no full-schedule JSON.
  - `plan-analyst.md` agent spec documents the per-child classifier as the default invocation; whole-plan analysis is dropped or marked legacy-only.
  - Orchestrator merges classifier outputs into the `tasks[]` array, then runs `compute-schedule --stdin` to produce batches. No mid-pipeline schedule file exists until `write-schedule` at the end of Phase 1.
  - When every child already declares `**Agent:**`, Phase 1 skips the classifier entirely (no Agent dispatches; `build-tasks` + `compute-schedule` + `write-schedule` is the whole Phase 1).
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/agents/plan-analyst.md`

**Description:**
Replace the whole-plan analyst dispatch with a fan-out. Parallelism is a free by-product — N children classify in one assistant turn via N tool-use blocks. Gap detection and risk surfacing move into the per-child prompt; cross-task gaps (missing dependency targets, file-overlap conflicts) are caught by `compute-schedule`'s existing DAG + file-disjoint validators.

### TASK-006: Schedule-only plan-review

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
  - plugins/plan-executor/scripts/codex_plan_review_schema.json
  - tests/scripts/test_plan_codex_dispatch.py (create)
- **Dependencies:** [004, 005]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_codex_dispatch.py -q -k plan_review`
- **Acceptance criteria:**
  - `plan_codex_dispatch.py plan-review` drops the `--plan-file` required argument; `--schedule-file` is the sole required input. Passing `--plan-file` prints a deprecation notice and ignores the value (one-version transition; removed in TASK-008).
  - **`render_plan_review_prompt` helper signature updated.** The prompt-render helper in `plan_codex_dispatch.py` (currently takes `plan_text`, `plan_basename`, `plan_path` as required args) is updated to either (a) drop those args entirely, or (b) make them optional with `None` defaults and unused when present. Prompt body drops the plan-markdown block; schedule JSON becomes the sole content rendered into the prompt. Failing to update this signature would produce a `TypeError` on every `plan-review` dispatch.
  - The rendered Codex prompt contains the schedule JSON (full, including fat `description` + `acceptance_criteria` per task) + review instructions (DAG shape, file-disjointness, classification sanity, test-command reachability, AC-vs-files alignment heuristics).
  - Envelope schema unchanged at the top level: `parsed.verdict ∈ {approved, approved-with-notes, needs-replan}`. `parsed.findings[*]` continue to carry `severity`, `blocking`, `section`, `concern`, `suggested_change` only — **no `notes[]` on findings** (the schema's `additionalProperties: false` and the existing top-level `parsed.notes` list are the single source of notes; this task does NOT add per-finding notes).
  - `codex_plan_review_schema.json.parsed.plan_file` stays (identifies which plan the review targets) — populated with the directory basename in dir mode. No `schedule_file` field yet; revisit in TASK-008 cleanup.
  - Section references in findings now point at `tasks[i]` paths, e.g. `"tasks[002].test_command"` — guidance added to the Codex prompt.
  - New test: dispatch against the fixture's persisted schedule (fat manifest from TASK-004, at `tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json`) returns `verdict=approved` or `approved-with-notes` with no `findings[*].severity=critical`.
  - New test: dispatch against a schedule with a deliberately-missing `description` on one task returns a finding with `section` pointing at that task and non-empty `suggested_change`.
  - New test: argv assertion — `--plan-file` absent from the invoking command; prompt-render helper called without `plan_text`/`plan_basename`/`plan_path` actuals; no `TypeError`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_codex_dispatch.py plugins/plan-executor/scripts/codex_plan_review_schema.json tests/scripts/test_plan_codex_dispatch.py`

**Description:**
Make plan-review schedule-only. Codex no longer needs to read plan markdown to validate the pre-dispatch gate; with the fat manifest from TASK-004, DAG + file-disjointness + classification-sanity + task-intent validation are all derivable from the schedule alone. This eliminates the "directory as `--plan-file`" protocol gap by removing the argument entirely. The internal `render_plan_review_prompt` signature update is bundled here so the dispatch never fires against a `plan_text=None` call site.

### TASK-007: Triage + author per-child targeting (backward-compatible)

- **Status:** done
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/scripts/codex_plan_review_schema.json
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
  - plugins/plan-executor/agents/plan-author.md
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [006]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "plan_review or triage or author"`
- **Acceptance criteria:**
  - `codex_plan_review_schema.json` `findings[*]` gains an optional `target_task_id: string | null` field (not in `required`). `additionalProperties: false` is relaxed minimally — either the property is added to the schema's `properties` (so `additionalProperties: false` still permits it) OR the setting is loosened for `findings[*]` alone. Default value for producers is `null` (schedule-level finding). Codex prompt guidance added requesting the field.
  - `parse-plan-review-report` surfaces `target_task_id` when present; synthesizes `null` when absent (backward-compat with old Codex outputs — same-version migration gate).
  - Phase 1.5.5 triage dispatch template embeds per-finding `{target_task_id, blocking, severity}` so the triage agent can reason about prioritization: `blocking=true` findings route first, then `severity=critical`, then `severity=important`.
  - Phase 1.5a `plan-author` dispatch receives per-finding `{finding, target_task_id, child_plan_file}` triples and edits only the named child files (write-scope tightened — one child per finding).
  - **`plan-author.md` agent spec updated.** The spec currently assumes a whole-plan markdown file with `### TASK-NNN` blocks and a single `plan_path` edit target. Updates: (a) accept `child_plan_file` paths (a specific TASK-NNN_*.md file) as the edit target, not only whole-plan paths; (b) for schedule-level findings (`target_task_id=null`), edit target may be `00_INDEX.json` (roster) OR empty (emit `files_edited: []` with a justification note); (c) parser guidance updated for the `### TASK-NNN:` child sub-heading grammar.
  - Schedule-level findings with `target_task_id=null` still route to `plan-author` but with an explicit "schedule-level — no child file" note; the author edits the roster or no file.
  - New tests:
    - triage routing: mixed-blocking findings reorder so `blocking=true` comes first in the dispatch payload.
    - author per-child targeting: two findings with different `target_task_id` produce two separate author dispatches, each scoped to one child file.
    - backward-compat: old Codex envelope without `target_task_id` produces `null` in parser output, routes to "schedule-level" path.
    - schema compliance: envelope with `target_task_id` present validates; envelope without it still validates (not in `required`).
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/scripts/codex_plan_review_schema.json plugins/plan-executor/scripts/plan_codex_dispatch.py plugins/plan-executor/agents/plan-author.md`

**Description:**
Tighten the plan-review → triage → author chain to per-child scope. Each finding names which task it targets; the author's write surface is correspondingly narrower. Schedule-level findings remain supported but are the exception, not the default. Also wires the `blocking` field (added in hotfix) into triage prioritization — previously carried in the schema but unused by consumers. `plan-author.md` agent spec is updated in the same commit so the agent behavior matches the dispatcher's new expectations.

### TASK-008: SKILL + dispatch-templates cleanup + file-mode removal

- **Status:** done
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
  - plugins/plan-executor/scripts/plan_ops.py
  - plugins/plan-executor/scripts/plan_codex_dispatch.py
- **Dependencies:** [002, 003, 004, 005, 006, 007]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json`
- **Acceptance criteria:**
  - Every "In file mode: …" / "In directory mode: …" paired callout in SKILL.md and `dispatch-templates.md` is deleted. Remaining text reads as single-track directory-only prose.
  - "Per-task `<plan-file>` resolution" section simplified: `task.plan_file` is always set (no fallback to the single-file plan path).
  - `CANONICAL_CONTRACT` in `plan_ops.py` updated so every entry reflects the directory-only shape; no `pass_with_alias` status for file-mode aliases remains; `decompose-plan` (TASK-001) is a first-class subcommand entry.
  - `cmd_preflight` file-branch (marked deprecated in TASK-003) is deleted.
  - `cmd_plan_review` `--plan-file` deprecation shim (added in TASK-006) is deleted; argparse removes the flag entirely.
  - `git grep -n "file mode\|file-mode\|single.file plan\|fall back to the single-file plan"` in the two SKILL / dispatch-templates files returns zero matches.
  - `audit --strict --json` passes — no `fail` or `pass_with_alias` findings.
  - **Constraint:** `plan_ops.py decompose-plan` (TASK-001) is explicitly preserved — it is the bridge that makes single-file input compatible with the directory-only architecture, not a file-mode remnant.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/scripts/plan_codex_dispatch.py`

**Description:**
Final prose + canonical-contract sweep, plus deletion of the now-unreachable file-mode code branches in `plan_ops.py` and `plan_codex_dispatch.py`. By this point every behavior task has landed and the dual-track language is dead weight. Collapsing it makes the SKILL readable top-to-bottom as one protocol instead of two interleaved ones. `decompose-plan` is explicitly preserved — it is part of the directory-only architecture.

### TASK-009: End-to-end directory-mode smoke

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - tests/scripts/test_implement_plan_directory_smoke.py (create)
  - tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json (create — canonical schedule sidecar IN the plan directory per `_plan_paths.py` convention)
- **Dependencies:** [008]
- **Test command:** `python3 -m pytest tests/scripts/test_implement_plan_directory_smoke.py -q`
- **Acceptance criteria:**
  - New test file drives the `/implement-plan` flow against `tests/fixtures/directory_mode_plan/` end-to-end in dry-run mode (no actual Codex dispatch — mock `invoke_codex` at the wrapper boundary to return `{verdict:"approved", findings:[], notes:[], schedule_ok:true, summary:"..."}`).
  - Fixture layout:
    - `tests/fixtures/directory_mode_plan/` — the plan directory (00_INDEX.json + TASK-NNN_*.md children, already shipped)
    - `tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json` — the schedule sidecar (IN the plan directory per `_plan_paths.py:69` convention; filename = dir stem + `.schedule.json`)
  - Assertions:
    - (a) preflight passes;
    - (b) `build-tasks` produces a schedule matching the roster, with every task carrying non-empty `description` and `acceptance_criteria`;
    - (c) per-child classifier fan-out fires when a fixture variant omits `**Agent:**` from a child; assert N discrete Agent tool-use blocks appear in the single assistant turn for N missing-agent children;
    - (d) schedule-only plan-review dispatch receives only `--schedule-file tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json` (no `--plan-file` in argv; in-directory sidecar per convention); `render_plan_review_prompt` is called without `plan_text` actuals;
    - (e) dry-run logs show per-task `plan_file` metadata for every event after `run_start` — `run_start` and `run_end` carry the directory basename, all other events carry the child basename;
    - (f) single-file input triggers `decompose_auto_promote` event at Phase 0, produces a decomposed sibling directory, and proceeds with directory-mode dispatch (TASK-001 auto-promotion check).
  - Test is pure — no git writes, no network, no Codex invocation. Isolation via `tmp_path` + fixture copy.
- **Reversion guidance:** `git restore tests/scripts/test_implement_plan_directory_smoke.py tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json; rm -f tests/scripts/test_implement_plan_directory_smoke.py`

**Description:**
Lock in the new directory-only path with an integration test that stays green as the refactor lands. Uses the already-shipped fixture (adds only the sidecar schedule in-directory) and mocks the Codex wrapper boundary — the SKILL's behavior is deterministic once the wrapper returns a canned verdict. Exercises both the auto-promotion path (single-file input, assertion f) and the native directory path (assertions a-e), plus the TASK-005 parallel dispatch semantics (assertion c) and the TASK-006 render signature (assertion d).

## Expected outcome

- Nine `feat(TASK-NNN):` commits plus one `chore(implement-plan):` housekeeping commit.
- `/implement-plan` accepts decomposed directories as canonical input; single-file inputs auto-promote transparently via `plan_ops.py decompose-plan` before Phase 0 yields. No user-facing halt/error on file inputs.
- Every `--plan-file` argument to any subagent or wrapper is a specific child basename — the directory path stays in the orchestrator.
- Schedule JSON carries fat per-task metadata (description + AC), enabling schedule-only plan-review.
- plan-review findings carry `target_task_id` + `blocking`; triage + author dispatches route on both. `plan-author` agent spec accepts child paths and schedule-level (roster / empty) edits.
- Shared parsing helpers in `plan_ops.py` are the single source of markdown→task-dict conversion; both `decompose-plan` and `build-tasks` import them. No grammar drift possible without a primitive test breaking.
- SKILL.md + `dispatch-templates.md` read as single-track directory-only protocol; dual-mode language is gone.
- `audit --strict --json` passes with no alias-windows referencing file-mode bindings; `decompose-plan` is recognized as a first-class subcommand.
- The `plan-decomposer` plugin (separate track) is no longer a Phase 2 precondition — it can ship later as a user-facing alias.

## Follow-ups (out of scope)

- Fix the pre-existing `test_analyst_to_parse_schedule_roundtrip` failure at `test_plan_ops.py:5194`. The analyst subprocess returns a Claude Agent SDK response wrapper (`type`, `subtype`, `duration_ms`, …) instead of the inner schedule JSON. Unrelated to this plan but needs to be green before Phase 2 CI-gates start reporting reliable red/green.
- Remove the deprecated `--plan-file` shim on `plan-review` after one release cycle (TASK-008 removes the argparse declaration; the implementation-ignore path stays for the deprecation window).
- Optionally build the `plan-decomposer` plugin (separate plan: `docs/plans/build-plan-decomposer-plugin.md`) as a user-facing CLI alias for `plan_ops.py decompose-plan`. No longer load-bearing for Phase 2 — `decompose-plan` is the authoritative implementation.

## Execution log — 20260424T122957 (paused)

Starting SHA: `0bc13335f72787ca8e2b4fbc87c6d0b31b437b4e`  → Ending SHA: `0bc13335f72787ca8e2b4fbc87c6d0b31b437b4e`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-001 | claude | codex | needs-rework (binding re-review after D.2a.5 remediation) | (uncommitted — paused) | Round-1 findings (schema-valid, $PYTHON bootstrap, idempotency) all fixed in remediation. Round-2 binding re-review surfaced 3 NEW concerns: permissive task-header regex, hardcoded chunk status=Pending, 00_INDEX.json missing canonical manifest fields. Awaiting user decision per D.2a.5 hard rule. |
