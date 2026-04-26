---
name: "implement-plan"
description: "Dual-agent plan executor. Dispatches Claude-tier tasks via plan-implementer and Codex-tier tasks via plan_codex_dispatch.py wrapper, with per-batch cross-review. Interleaves implement -> review -> commit inside each batch to keep review diffs clean."
user_invocable: true
---

# Implement Plan

**Arguments:** $ARGUMENTS

## Python interpreter resolution

All Python invocations in this skill use `$PYTHON`. The orchestrator resolves `$PYTHON` once at Phase 0 preflight via `plan_ops.py preflight --json`'s `python_path` field and pins that absolute path for the remainder of the run. After preflight, set `PYTHON=<python_path>` from its JSON output; every subsequent `$PYTHON ...` command line uses the pinned value. Do NOT hardcode a specific interpreter path — the resolver inside `plan_ops.py:_resolve_python()` consults `$IMPLEMENT_PLAN_PYTHON`, then `./venv/bin/` (repo-local virtualenv), then `./.venv/bin/` (alternate virtualenv convention), then `python3` on `$PATH`, so repos with non-default Python layouts work without hand-edits to this skill. See §11.1 of `DUAL_AGENT_PLAN_EXECUTOR.md` for the full precedence.

## Dispatch rules (read before any subagent call)

1. **Parallel dispatch per batch.** Up to `--parallel N` dispatches in a SINGLE message inside Phase B. Claude-tier via Agent, Codex-tier via Bash — both kick off in the same message when a batch contains both.
2. **Trust the analyst's schedule.** Batches with disjoint `file_locks` are parallel-safe. Do not add extra safety reasoning.
3. **Your job is routing only.** Pick tasks from the analyst's schedule, dispatch, interpret reports, commit/revert. No code reading, no diff judgment, no scope inflation.
4. **Dispatch prompts must be self-contained.** Read `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md` for the templates. Every Agent prompt includes the full task block verbatim + "You do NOT have the Agent tool."
5. **Timeouts on Bash calls.** 5000ms for idioms (printf, git status). Bash-call outer timeout MUST cover the wrapper's effective internal timeout plus a small buffer. Implement: `max(300_000ms, 60_000 * len(files))`. Review: `max(180_000ms, 30_000 * len(files))`. Plan-review: `180_000ms` (flat). The wrapper enforces its own internal timeout; pass `--timeout N` only to override.
6. **Subagent errors.** If a dispatch returns `[Tool result missing due to internal error]` or no parseable report, treat as failure. Log it, restore partial changes, do NOT retry silently. Claude-implementer `malformed` outcome goes to `fail-task stage=implement reason=malformed_report`.
7. **Cross-review asymmetry.** Claude implements → Codex reviews; Codex implements → Claude reviews. Escalation path differs by direction — see Phase D.2.

## Readiness check (TASK-007)

Before running `/implement-plan` on a new plan — and after any substantive change to `plan_ops.py`, the Codex wrapper, the schema sidecars, the dispatch templates, or the design doc — run the executor self-audit so protocol drift surfaces before it bites a real run:

```bash
python3 plugins/plan-executor/scripts/plan_ops.py audit --json
```

The audit cross-references the shipped artifacts against the canonical decisions declared in `plan_ops.py:CANONICAL_CONTRACT`. Any finding with `status: fail` MUST be resolved before proceeding with a real run. TASK-008 removed every legacy alias window, so post-cleanup checks emit plain `pass` when the runtime artifact matches the canonical set and `fail` otherwise.

`audit` is advisory at the Phase 0 preflight seam — it is NOT a hard gate (gates are TASK-005's job and are runtime-scoped to a specific execution; audit is standing / cross-cutting). The intent is to catch executor drift between the editor's terminal and the next orchestrator run. The `portable_tier` check is registered but advisory / `--strict`-only until TASK-008 lands; pass `--strict` to include it in the verdict.

Markdown report (for sharing in PRs / postmortems):

```bash
python3 plugins/plan-executor/scripts/plan_ops.py audit --report-file /tmp/audit.md
```

## Promotion criteria

The executor promotes from dry-run to execute (and from execute to "certified-clean") via six canonical phase gates. Each gate returns `{name, status ∈ pass|fail|not_applicable, reason}`. Invoke them through `plan_ops.py gates` — never reimplement the predicates inline.

| Gate | Phase | What it asserts |
|---|---|---|
| `schema-valid` | Phase 0 preflight | Plan markdown conforms to §5: `## Goal`, a `## Context` or `## Scoped Context`, `## Verification`, and every `### TASK-NNN` block carries Status / Priority / Files / Test command / Acceptance criteria bullets + Description prose header. |
| `schedule-valid` | Phase 1 (post-write-schedule) | Analyst JSON passes `_validate_schedule` + `_validate_schedule_dag` (shape + DAG). Runs immediately after `write-schedule` persists the analyst output in Phase 1 -- the schedule file does not exist during Phase 0 preflight. |
| `fixture-valid` | Phase 0 preflight | The sample fixture (`sample_phase4.md`) itself passes `schema-valid` + `schedule-valid`. |
| `execution-safe` | Phase 0 preflight | `plan_codex_dispatch.py` implement path carries the always-ignore / protected-paths seam, `_snapshot_baseline(` is called at implement + timeout sites, and there is no `git clean -fd` in executable code. Predicate-only — does NOT invoke the wrapper. |
| `review-safe` | Phase 0 preflight | `plan_codex_dispatch.py cmd_review` carries `_snapshot_baseline(` and respects `is_protected_path` / `PROTECTED_EXACT_PATHS`. |
| `commit-safe` | Phase D.3 (per commit) + End-of-run certification | `git show --name-only <sha>` minus TASK-NNN's declared `Files:` list and the always-ignore set is empty. Dry-run mode → `not_applicable`. Execute mode → verified post-hoc from every `commit_done` run-log event for the run. |

The always-ignore set named by `commit-safe` is the shared `COMMIT_ALWAYS_IGNORE` constant in `plugins/plan-executor/scripts/_plan_paths.py` -- the narrow set of bookkeeping paths that `commit-task` itself writes during orchestration (`docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, the per-plan `*.schedule.json` sidecar, and the `00_INDEX.json` roster next to the plan). `commit-task`'s staging logic and the post-commit `_gate_commit_safe` predicate both key on this same constant, so the pre-commit and post-commit sides cannot drift. Executor scripts such as `plan_ops.py` are protected from delta-cleanup but must still be declared in Files: to commit against; they are NOT members of `COMMIT_ALWAYS_IGNORE`.

**Dry-run pass condition:** `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe` all `pass`; `commit-safe` is `not_applicable` (no commits in dry-run).

**Execute pass condition:** all six gates `pass`, with `commit-safe` re-verified per `commit_done` event via `plan_ops.py gates --certify --mode execute --run-id <id>`.

## Bash command idioms

Standard commands used by this skill:

- `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" <subcommand> [--json]` — all plan parsing, schedule evaluation, batch selection, narrow commit, failure handling, status transitions, and run-log append verification.
- `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement|review ...` — Codex-tier implementer and reviewer. Emits a single JSON envelope on stdout.
- `date -u +%Y%m%dT%H%M%S` — run_id fallback (preflight emits one authoritatively).

Never write inline Python for plan operations. Never `git stash` inside this skill — the wrapper restores Codex-side independently; the orchestrator restores Claude-side via `plan_ops.py fail-task` (which uses `git restore`).

## plan_ops.py CLI reference

All subcommands accept `--json` for machine-readable output.

| Command | Purpose |
|---|---|
| `plan_ops.py preflight --plan-file <abs> [--strict-branch]` | Smart dirty-tree + codex probe + starting_sha + run_id + base-branch check |
| `plan_ops.py parse-schedule --stdin [--strict]` | Validate analyst JSON; surface errors, tasks, batches, gaps, risks. `--strict` promotes unknown nested fields from warning to error. |
| `plan_ops.py compute-schedule --stdin [--strict]` | Recompute file-disjoint + topo-respecting batches from `tasks[]`; standalone helper, not invoked by `/implement-plan` Phase 1. |
| `plan_ops.py write-schedule --schedule-file <path> --stdin [--strict]` | Validate + atomically persist schedule JSON. Refuses to write on any validation error. |
| `plan_ops.py batch-next --schedule-file ... --locked-files ... --done ... --failed ... --parallel N` | Pick next batch respecting file locks; flags `scheduler_stuck` |
| `plan_ops.py parse-implementer-report --stdin` | Extract outcome / files_changed / diff_summary / test_outcome / concerns / plan_adaptations / reversion_guidance / warnings / **diagnostics** from the plan-implementer markdown report. `concerns` and `plan_adaptations` are lists of strings (one bullet each). |
| `plan_ops.py parse-plan-review-report --stdin` | Validate a Phase 1.5 Codex plan-review envelope against `codex_plan_review_schema.json`; surface `{plan_file, verdict ∈ {approved, approved-with-notes, needs-replan}, findings_count, findings, schedule_ok, summary}`. Halts with structured `errors[*]` on schema violations. |
| `plan_ops.py commit-task ...` | Full D.3: guard, plan-status mutate (→ done), narrow `git commit --only`, SHA capture, run-log `commit_done` append |
| `plan_ops.py fail-task --stage implement\|review\|commit ...` | Full Phase C / D.4: git restore (if files), plan-status mutate (→ failed), run-log `failed` append |
| `plan_ops.py block-dependents --schedule-file <path> --plan-file <abs> --failed NNN --run-id <id>` | Cascade `blocked` onto transitive dependents: single plan read/write flips each dependent's `**Status:**` to `blocked`, then appends `blocked` run-log events. Source-of-truth invariant is the plan file. |
| `plan_ops.py update-plan-header --status in-progress\|complete\|partial` | Mutate the plan-file top-level `**Status:**` |
| `plan_ops.py finalize-execution-log ...` | Append §5 execution-log markdown table to plan |
| `plan_ops.py log-event --event E --fields-json '{...}'` | Append JSONL event with tail re-verify |
| `plan_ops.py normalize-task-id --id 1\|001\|TASK-001\|004A\|TASK-004A` | Canonicalize to `^\d{3}[A-Z]?$` form |
| `plan_ops.py acquire-lock / release-lock --plan-file ... --run-id ...` | Per-plan-file run-lock against `<run_lock>` |
| `plan_ops.py path-info` | Emit configured `plan_dir` + derived `run_log` / `run_lock` / `schedule_glob` paths. Run once at Phase 0 to bind the `<plan_dir>` / `<run_log>` / `<run_lock>` / `<schedule_file>` placeholders used throughout this skill. |
| `plan_ops.py gates --list\|--check <csv>\|--certify --mode dry-run\|execute` | Phase-gate predicates. The six canonical gates — `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, `commit-safe` — return `{name, status ∈ pass\|fail\|not_applicable, reason}`. Used at Phase 0 preflight (schema + schedule + fixture + execution-safe + review-safe), after each Phase D.3 commit (`commit-safe` for that SHA), and at End-of-run (`--certify --mode execute --run-id <id>` for the full bundle). See §9.7 of `DUAL_AGENT_PLAN_EXECUTOR.md`. |
| `plan_ops.py audit --list\|--json\|--report-file <path>\|--check <csv>\|--strict` | Standing self-audit (TASK-007). Cross-references shipped artifacts (`plan_ops.py`, `plan_codex_dispatch.py`, schema sidecars, SKILL.md, dispatch templates, design doc) against `CANONICAL_CONTRACT`. Findings carry `{check, tier ∈ default\|advisory, status ∈ pass\|fail, canonical, actual, reason}`. TASK-008 retired the legacy alias windows, so audit findings are now plain `pass`/`fail` only. Default verdict excludes advisory tier; `--strict` includes it. Run before any rerun and after substantive protocol changes. See §14 of `DUAL_AGENT_PLAN_EXECUTOR.md`. |

## Per-task `<plan-file>` resolution (TASK-004 write sites)

Five commands mutate plan markdown and therefore take `--plan-file`: `commit-task` (Phase D.3), `fail-task` (Phase C + D.4), `block-dependents` (Phase C + D.4), `update-plan-header` (End-of-run), and the `plan-author` auto-revise dispatch (Phase 1.5a). One read-only command also honours directory-mode `<plan-file>` resolution: `gates --certify` (End-of-run). For mutators, each call passes the current task's child file; for `gates --certify`, a directory `--plan-file` aggregates `schema-valid` per chunk (over `00_INDEX.json`) and re-checks `commit-safe` per `commit_done` event using each event's recorded `plan_file` field. Resolution rule:

```
child_basename = task.plan_file
child_path = <plans_dir> / child_basename
```

Every schedule entry carries `plan_file: "<child-basename>"` (the fat manifest from `build-tasks` populates it per directory-mode contract); resolve against `<plans_dir>` to get the absolute child path and hand that to `--plan-file`. `block-dependents`'s internal cascade does this per-dependent lookup natively; the orchestrator just passes `--plan-file <failed-task's child path>` and lets the subcommand route each dependent's mutation to its own file.

Run-log events carry a `plan_file` field in their `fields` dict so the audit trail records which child each event mutated. The events with this field are:

- `run_start` — `plan_file: "<dir-basename>"`. Also carries `claude_only: <bool>` in `fields` — the routing boolean bound at Phase 0 preflight per §Pre-flight (Phase 0). The field itself is wired into `run_start` payloads when TASK-002 (plan review) and TASK-003 (cross-review) land; TASK-001 documents the field's presence so downstream consumers know to expect it.
- `batch_start` — `plan_file` per batch entry (always-on is cheap and consistent).
- `implement_start` — `plan_file: "<child-basename>"` for the current task.
- `commit_done` — `plan_file: "<child-basename>"` (already captured by `commit-task` from the `--plan-file` argument; basename is derived automatically).
- `run_end` — `plan_file: "<dir-basename>"`. Matches `run_start` so the run-bracket pair is symmetric and consumers don't have to special-case the closing event.

## Parse arguments

CLI surface:

```
/implement-plan <plan-path> [flags]

Required:  <plan-path>

Optional:
  --dry-run               Analyze + print; no dispatch, no edits, no commits (sticky)
  --parallel N            Max concurrent tasks per batch (default: 2)
  --codex-only            Filter schedule to codex tasks
  --claude-only           Filter schedule to claude tasks
  --task-ids 1,2,3        Restrict to exactly these task IDs; halt if any ID is unknown.
  --skip-cross-review     Commit without review (loud banner in summary)
  --skip-plan-review      Skip Phase 1.5 Codex plan review (loud banner in summary);
                          parallel-safe with --skip-cross-review
  --no-auto-revise        Disable auto-revise on needs-replan — halt instead of dispatching plan-author.
  --codex-review-binding  Codex `needs-rework` on Claude code is binding for
                          the commit decision (no §8.4 third-opinion
                          escalation, no D.2a.5/D.2a.6 retry); orchestrator
                          marks the task `paused` and halts for user
                          instruction. Use `--unattended-revert-policy
                          fail-fast` to opt into auto-fail-task instead.
  --codex-plan-review-binding
                          Codex `needs-replan` on plan review goes straight to halt;
                          no plan-review-triage third-opinion escalation.
  --analyst-binding       Plan-analyst `needs-enrichment` (today: non-empty
                          `build-tasks warnings[]`) goes straight to halt;
                          no plan-review-triage third-opinion escalation.
                          Does NOT affect `--allow-gaps` short-circuit.
  --allow-gaps            Proceed past analyst outcome=needs-enrichment
                          (today: proceed past non-empty `build-tasks warnings[]`)
  --strict-branch         Halt (not warn) if current branch != plan's Base branch
```

Mutual exclusions: `--codex-only` + `--claude-only` → error. `--codex-review-binding` + `--claude-only` → error (Codex review-binding requires Codex availability and is incompatible with the Claude-only routing flag bound at Phase 0). `--codex-plan-review-binding` + `--claude-only` → error (same rationale, applied to the plan-review seam). The orchestrator LLM reads this prose and halts pre-dispatch; there is no structural checker in `plan_ops.py` for these mutexes (consistent with the existing `--codex-only` ⊕ `--claude-only` enforcement). Normalize `--task-ids` values via `plan_ops.py normalize-task-id` before filtering.

## Pre-flight (Phase 0)

**Run-state source of truth.** `_run_log.jsonl` is authoritative for run state.
The harness `TaskList` is only a visibility mirror for the operator.
On phase transitions, the orchestrator MUST mirror the run-log state into `TaskList`
so visible progress follows the actual run.
If that mirror update fails or drifts, keep progressing from `_run_log.jsonl`;
mirror failure does not block the run.

**Auto-promote single-file input to directory mode (TASK-001).** Before any other Phase 0 step, if `pathlib.Path(plan_path).is_file()` — i.e. the user passed a single markdown plan, not a decomposed directory — invoke the heuristic decomposer so every downstream phase can assume directory mode. This call runs **before** `$PYTHON` is bound (`$PYTHON` is pinned from the `preflight --json` output that runs later in this phase), so it uses the literal bootstrap interpreter `python3` on `$PATH`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" decompose-plan --plan-file <absolute plan> --json
```

The subcommand reads the whole-plan `## TASK-NNN:` markdown and writes a sibling directory at `<file-parent>/<file-stem>/` containing `00_INDEX.json` plus one `TASK-NNN_<slug>.md` child per task (each child uses the `### TASK-NNN:` H3 sub-heading that `build-tasks` and every downstream parser expects). On success the result JSON carries `produced_dir` + `task_count`; rebind `<plan-path>` ← `produced_dir` and proceed as if the user had passed a directory from the start. On malformed input the subcommand exits 1 with a structured `errors[*]` list (missing `## TASK-NNN:` headers, duplicate ids, missing required metadata, unresolvable deps, cycles) — surface the errors verbatim and halt without acquiring the lock. Append a run-log event (still via the bootstrap interpreter — `$PYTHON` is bound only after preflight):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event decompose_auto_promote \
  --fields-json '{"source_file":"<original plan-path>","produced_dir":"<produced_dir>","task_count":<n>}'
```

`preflight` runs next (see below) and pins `$PYTHON` for every subsequent `$PYTHON ...` command line in this skill.

Do NOT read the produced child files into the orchestrator's context — the `ls` of the directory is enough; every subsequent phase opens children on demand. The produced directory is treated identically to a user-authored decomposed directory: run-log `plan_file` metadata on `run_start` reflects the directory basename, not the original file. The single `decompose-plan` invocation is the orchestrator's only direct interaction with the whole-plan markdown — after this point everything downstream sees a directory. See §Input shape below for the canonical bindings and the auto-promotion precondition that makes every downstream phase assume `plan_path.is_dir()`.

First, bind the path placeholders used throughout this skill by querying the configured plan directory:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" path-info --json
```

Returns `{"plan_dir": "...", "run_log": "...", "run_lock": "...", "schedule_glob": "..."}`. Bind:
- `<plan_dir>` ← `plan_dir`
- `<run_log>` ← `run_log`
- `<run_lock>` ← `run_lock`
- `<schedule_file>` ← `<plan_dir>/<plan_path.name>.schedule.json` (constructed per plan from `<plan_dir>` and the directory basename `plan_path.name` — see the Input shape table below for the canonical binding)

Use these placeholders verbatim in all subsequent commands; never hardcode `docs/plans`.

**Input shape.** `<plan-path>` is a directory containing `00_INDEX.json` + one-or-more `### TASK-NNN` child `.md` files — a decomposed plan. This is the canonical form every downstream phase assumes. Single-file markdown inputs are transparently auto-promoted to this shape by the Phase 0 `decompose-plan` step above (the **Phase 0 auto-promotion precondition**): the orchestrator rebinds `<plan-path>` to the produced directory before any other Phase 0 step runs, so by the time preflight / gates / analyst dispatch fire, `pathlib.Path(plan_path).is_dir()` is always true. Path bindings:

| Binding | Value |
|---|---|
| `<plans_dir>` | `plan_path` |
| `<schedule_file>` | `<plan_dir>/{plan_path.name}.schedule.json` (directory basename, verbatim — no `.md` suffix to strip) |
| `<run_lock>` key | `str(plan_path.resolve())` (absolute directory path) |

One run-lock entry per directory-run, not one per child — parallel children inside a single directory-run share the lock; a second `/implement-plan` invocation against the same directory halts on overlap. The `<run_lock>` key is whatever `acquire-lock --plan-file` is handed, so passing the directory path to `acquire-lock` achieves this without any subcommand change.

**Directory-mode design boundaries** (documented here so the reviewer does not re-litigate):

- `check-plan-deps` runs **once** against the directory's roster with single-file semantics (the roster's `depends_on_plans` is advisory at the roster root). No cross-directory dependency gate.
- `fixture-valid` stays single-file — the shipped `sample_phase4.md` fixture continues to certify the executor's parse path; there is no directory-aware fixture.
- `finalize-execution-log` appends one §5 table **per child** (scoped to that child's tasks). No run-level aggregate table.
- `update-plan-header` iterates the distinct `plan_file` values present in completed + failed tasks and flips each child's own top-level `**Status:**` independently. No synthesized run-level aggregate header.
- `--task-ids` filtering relies on analyst-emitted globally-unique ids (the analyst rejects duplicate ids across children per TASK-003). No cross-child filtering semantics beyond "id is unique."

Then run preflight:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" preflight --plan-file <absolute plan> [--strict-branch] [--strict-scope] [--unattended-revert-policy pause|fail-fast|preserve-only]
```

Returns JSON with `pass`, `starting_sha`, `run_id`, `codex_available`, `python_path`, `unattended_revert_policy`, `dirty_files{plan_doc, orchestrator_state, plan_scope_dirty, source_blocking}`, `scope_warnings[]`, `base_branch`, `current_branch`, `base_branch_match`. Halts on `source_blocking` dirty. `plan_doc` churn (the plan file being executed) is allowed. `orchestrator_state` (run-log / run-lock / schedule sidecar) warns but proceeds. `plan_scope_dirty` (paths declared in some task's `Files:` list) is surfaced with per-task attribution and mirrored into `scope_warnings[]`; in non-strict mode it is advisory only (does NOT flip `pass`). Pass `--strict-scope` to promote it to a blocking condition.

**After preflight**, pin `$PYTHON` for the rest of the run by exporting `PYTHON=<python_path>` from the preflight JSON. Every subsequent `$PYTHON ...` command line in this skill uses the pinned value. If you need to re-dispatch from a fresh shell context, re-export from the same preflight result — do NOT re-resolve in templates.

**Pin `$UNATTENDED_REVERT_POLICY`** the same way: export `UNATTENDED_REVERT_POLICY=<unattended_revert_policy>` from the preflight JSON and use the pinned value for every subsequent pause-path consumer in this run (the awaiting-user pause subroutine introduced in TASK-004, plus the dispatch logic in TASK-005 / TASK-006 / TASK-007 / TASK-008 / reconcile-batch). Do NOT re-resolve from a fresh argparse invocation. The flag is NOT argparse-required: when stdin is a TTY and the operator omits `--unattended-revert-policy`, preflight defaults to `pause` and continues; when stdin is NOT a TTY (cron/CI/wrapped invocation) and the flag is absent, preflight refuses with `errors[*].code = "unattended-revert-policy-required"` so unattended execution cannot silently discard work on a pause path. The three values mean: `pause` — block on the human-in-the-loop seam (the first-principles default for interactive runs; the orchestrator surfaces the pause record and waits); `fail-fast` — emit a structured failure record and halt the run instead of pausing (consumer wiring lands in TASK-005 / TASK-006 / TASK-007 / TASK-008); `preserve-only` — preserve/log the discarded diff to a salvage ref (side location), THEN fail-task / halt the run (consumer wiring also lands in TASK-005 / TASK-006 / TASK-007 / TASK-008). Until those downstream tasks land, only the preflight handshake and this pin are wired up.

If `codex_available=false`, override `tasks[].agent = "claude"` throughout Phase 1 and warn; wrapper's own "codex binary not found on PATH" branch is the backstop.

**Bind `claude_only` (single routing boolean).** Immediately after preflight returns, bind a single boolean `claude_only` for the rest of the run, defined as the OR of (a) the operator opt-in `--claude-only` flag and (b) the preflight signal `codex_available == false`:

```
claude_only := (--claude-only is set) OR (preflight.codex_available == false)
```

This is the canonical routing flag that every downstream phase consults (Phase 1.5 plan review, Phase D cross-review). Hoisting it to one place keeps the routing decision stateless across phases — no phase recomputes it from the underlying inputs. Behavior wiring on this flag lands in TASK-002 (plan review skip) and TASK-003 (cross-review skip); TASK-001 documents the binding only. Once bound, `claude_only` flows into the `run_start` event's `fields` as `claude_only: <bool>` (the field is added when TASK-002 / TASK-003 wire the downstream phases).

Then run the mandatory cross-plan dependency gate:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" check-plan-deps \
  --plan-file <absolute plan> --plans-dir <dirname of plan-file> --json
```

Halts on `pass: false` with the `unresolved[]` list. Halts on non-empty `errors[]` as internal-error. There is no `--allow-gaps` override — cross-plan deps are hard blockers.

Run `check-plan-deps` **once** against the directory's roster: pass `--plan-file <plans_dir>/<first chunks[].file>` and `--plans-dir <plans_dir>`. `check-plan-deps` uses single-roster semantics inside the directory; cross-directory `depends_on_plans` at the roster root is advisory and is NOT a hard gate.

Then run the five pre-dispatch phase gates. The Phase 0 preflight halt set is `schema-valid`, `schedule-valid`, `fixture-valid` -- all three are strict halt-on-fail, with no warning tier and no demotion path. `schedule-valid` cannot actually run here because the schedule file is written later in Phase 1; it runs immediately after `write-schedule` persists the analyst output (see below) and carries the same strict halt-on-fail contract. Phase 0 therefore runs the four gates whose inputs exist now (`schema-valid`, `fixture-valid`, `execution-safe`, `review-safe`):

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
  --check schema-valid,fixture-valid,execution-safe,review-safe \
  --plan-file <absolute plan> --json
```

Halt on any `status: fail`, emitting the gate's `reason` verbatim and logging `run_end reason=preflight_gates_failed`. The halt is strict for every gate in the preflight set -- including `fixture-valid` -- with no per-plan exception and no demotion to warning. The frozen gate status vocabulary is `pass|fail|not_applicable`; there is no `warn` status and no `--warn-only` flag on the `gates` subcommand.

**Per-child `schema-valid` loop.** Run `schema-valid` once per `chunks[].file` in `<plans_dir>/00_INDEX.json`, halting on the first failure. Emit the halt message as `schema-valid failed for child <basename>: <gate.reason>` so the operator can fix the offending child without scanning the full set. `fixture-valid` / `execution-safe` / `review-safe` still run once (they validate the executor itself and the shipped sample fixture, not the user's plan). Pseudocode sketch:

```
roster = plan_ops.load_00_index(plans_dir)
for chunk in roster["chunks"]:
    child = plans_dir / chunk["file"]
    r = gates_check(["schema-valid"], plan_file=child)
    if r.failed:
        halt("schema-valid failed for child " + chunk["file"] + ": " + r.reason)
gates_check(["fixture-valid", "execution-safe", "review-safe"])  # once
```

Then acquire the run-lock:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" acquire-lock --plan-file <absolute plan> --run-id <id>
```

`<absolute plan>` is the directory's absolute path — the lock file stores one entry per directory-run, not per child.

Overlap on the same plan (or directory) → halt with the conflicting run_id.

The lock file at `docs/plans/_run_lock.json` follows a strict canonical shape: a JSON object keyed by absolute plan path, with each entry containing exactly `{"run_id": "<id>", "acquired_at": "<opaque non-empty string>"}`. Any other shape (invalid JSON, extra keys, missing keys, top-level not an object, non-string or empty values) is rejected by `acquire-lock`. `--force` is a manual recovery tool — use it only to recover from a corrupted or stuck lock file. `--force` discards all existing entries (including entries for other plans), so do not run it while a legitimate run is in progress. `--run-id` must be a non-empty string; empty or non-string values are rejected before any file operation so a botched invocation cannot corrupt the lock file.

Append `run_start` via `plan_ops.py log-event`. Include `plan_file: "<dir-basename>"` in the event's `fields` (dir-basename is the directory's own basename, e.g. `implement_plan_directory_mode`).

## Analysis (Phase 1)

Phase 1 now runs as a four-step protocol: a deterministic `build-tasks` synthesis, an optional per-child classifier fan-out (only when some children lack `**Agent:**`), an in-memory filter / agent-override seam, and a `write-schedule` persist + `schedule-valid` gate. The whole-plan `plan-analyst` dispatch is retired from the default path — the agent's primary role in this skill is now the narrow per-child classifier of step 2. Append `analyst_done` only after `write-schedule` persists, to preserve the existing event order on the Phase 1.5 downstream path.

**Outcome vocabulary on this path.** The new Phase 1 surfaces structural failures as `errors[*]` from `build-tasks` (analogue of the old `outcome=invalid`) and quality gaps (missing `**Description:**` / `**Acceptance criteria:**` in a child) as `warnings[*]` (analogue of the old `outcome=needs-enrichment`). The allow-gaps / analyst-binding / analyst-triage seams below remain structurally intact but now key on `len(build_tasks.warnings) > 0` instead of an analyst-emitted `needs-enrichment` verdict. Full triage-payload wiring (what `warnings[]` look like when handed to the triage subagent) is deferred to TASK-007 of this refactor; until then treat the triage path as "present in the skill, behaviourally untouched by TASK-005".

### Step 1 — Build the fat `tasks[]` manifest (deterministic)

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" build-tasks \
  --plans-dir <plans_dir> --json
```

Capture `{ok, outcome, tasks, batches, warnings, errors}`. Every `tasks[i]` carries `id`, `title`, `files`, `dependencies`, `priority`, `plan_file` (child basename), `description` (non-empty string per child), `acceptance_criteria` (ordered `string[]`), and optionally `agent` (emitted iff the child file declares `**Agent:**`). `batches` from this call is the topo + file-lock ordering `_build_tasks` computed and is carried verbatim into the persisted schedule (no orchestrator-side rebatch — `compute-schedule` and `build-tasks` share the same canonical batcher and any post-step-2 re-pipe would be a no-op).

Branch on the structured result:

- `ok == false` (non-empty `errors[]`) → halt; surface `errors[*]` verbatim; log `run_end reason=analyst_invalid`; release lock. This is the direct analogue of the retired analyst `outcome=invalid` branch.
- `ok == true` AND `len(warnings) > 0` + `--allow-gaps` → log `analyst_triage_skipped {reason:"allow_gaps"}` and warn + proceed to step 2. NO triage dispatch. **`--allow-gaps` short-circuits proceed and wins over `--analyst-binding` when both are set** — evaluate this branch before `--analyst-binding`.
- `ok == true` AND `len(warnings) > 0` + `--analyst-binding` (and `--allow-gaps` NOT set) → log `analyst_triage_skipped {reason:"binding_flag"}` and halt with `run_end reason=plan_analyst_failed`; release lock. NO triage dispatch.
- `ok == true` AND `len(warnings) > 0` + neither flag set → dispatch Phase 1-triage (see below); triage verdict determines whether to proceed to step 2. Triage payload wiring onto `build-tasks warnings[]` is TASK-007's scope; v1 behaviour is structurally preserved but may short-circuit until TASK-007 lands.
- `ok == true` AND `len(warnings) == 0` → proceed directly to step 2.

**`--allow-gaps` semantics preserved.** `--allow-gaps` remains a pre-triage short-circuit on the analyst path: when set, the orchestrator bypasses analyst triage entirely and proceeds with today's soft-severity demotion behavior. Triage replaces the demotion *default* when `build-tasks` surfaces warnings and neither `--analyst-binding` nor `--allow-gaps` is set; operators who want triage instead of the demotion simply omit `--allow-gaps`.

### Step 2 — Per-child classifier fan-out (conditional)

Inspect the in-memory `tasks[]` array. Let `missing_agent_children = [t for t in tasks if "agent" not in t]` — one entry per child that did NOT declare `**Agent:**` in its source markdown.

**Skip-classifier case.** If `missing_agent_children` is empty (every child already declares `**Agent:**`), the classifier step is skipped entirely. No Agent dispatches are issued in Phase 1, no network roundtrip, no model latency — Phase 1 collapses to `build-tasks + write-schedule`. Proceed directly to step 3.

**Fan-out case.** If `missing_agent_children` is non-empty, emit **N discrete `Agent` tool calls in a single assistant turn** — one per missing-agent child. The fan-out is N parallel tool-use blocks inside one response, NOT an array-prompt wrapped inside a single Agent tool call; this matches the existing Agent-tool contract in the parent agent's API and introduces no new tool-call shape.

Pseudo-syntax (for illustration; the actual tool-call shape is the standard Agent tool, one per child):

```
# Inside ONE assistant turn, the orchestrator emits N separate tool-use blocks:
[
  Agent(subagent_type: "plan-analyst", model: "sonnet", prompt: render(templates.PhaseASingle, child=missing_agent_children[0])),
  Agent(subagent_type: "plan-analyst", model: "sonnet", prompt: render(templates.PhaseASingle, child=missing_agent_children[1])),
  ...
  Agent(subagent_type: "plan-analyst", model: "sonnet", prompt: render(templates.PhaseASingle, child=missing_agent_children[N-1])),
]
# ^ N discrete Agent tool calls, emitted together in one response. The Agent-tool
#   runtime dispatches them in parallel; the orchestrator receives N replies.
```

Each dispatch uses the **Phase A-single** template from `dispatch-templates.md` (subagent_type `plan-analyst`, model `sonnet` — narrower scope than the retired whole-plan opus dispatch). Each child agent reads exactly its one child file and returns minimal JSON `{agent: "claude"|"codex", classification_reason: "<one-line justification>"}`.

Parse each reply and merge into the in-memory `tasks[]` array: for each child reply, set `tasks[i].agent = reply.agent` and `tasks[i].classification_reason = reply.classification_reason` on the matching entry (match by `plan_file` basename — the orchestrator knows which dispatch corresponds to which child).

Malformed reply handling — a reply that does not parse as `{agent, classification_reason}` is treated the same as the analyst's historical malformed-report path: halt with `run_end reason=analyst_invalid`; surface the offending child basename + raw reply; release lock.

Apply the `codex_available=false` preflight override here too — if the preflight flag was false, rewrite every `tasks[i].agent` to `"claude"` in the merged manifest before step 3 (consistent with the retired whole-plan analyst override, which the orchestrator used to apply after parsing the whole-plan JSON).

### Step 3 — Apply filters and per-task overrides

After the merged `tasks[]` has an `agent` field on every entry (either declared in source or filled by step 2), the orchestrator synthesizes the canonical schedule shape in memory and applies any filter / agent-override flags. **No re-batch happens here**: `build-tasks` already emitted topo + file-lock-correct `batches[]` in step 1 using the same canonical batcher the standalone batch-recompute helper would call, so any post-step-2 batcher re-pipe would be a provable no-op. The merged schedule flows directly from this step into step 4's `write-schedule` without any further batching call.

**Shape-shift between `build-tasks` and `write-schedule`.** `build-tasks` emits `{ok, outcome, tasks, batches, warnings, errors}` — six keys, not the canonical schedule shape. The orchestrator synthesizes the canonical 5-key top-level schedule `{outcome, tasks, batches, gaps, risks}` in memory by (a) keeping `tasks` from `build-tasks` (merged with step 2's `agent` populations), (b) keeping `batches` from `build-tasks` verbatim, (c) discarding `ok` / `errors` (the caller already halted on errors in step 1), (d) **mapping `warnings[]` → `gaps[]`** via the transform `{task_id: w.task_id, type: w.code, severity: "soft", detail: w.message}` so the warning signal is preserved in the persisted schedule (each `build-tasks` warning carries `{code, task_id, plan_file, message}` — the `plan_file` field is dropped because `gaps[]` does not carry it; downstream lookup goes through `tasks[].plan_file` instead), and (e) defaulting `risks: []` (risks were legacy analyst output and are not synthesized on this path). The persisted `outcome` is derived from the mapped `gaps[]` to satisfy the schedule validator (which enforces `outcome='valid'` ⟺ `gaps == []` and `outcome='needs-enrichment'` ⟺ `gaps != []`): emit `outcome='needs-enrichment'` when `warnings[]` is non-empty (and the caller is on the warn+proceed branch — allow-gaps short-circuit, or analyst-triage `ship`/`ship-with-fixes` verdict), and `outcome='valid'` when `warnings[]` is empty. `write-schedule --stdin` in step 4 rejects any top-level key outside the canonical five, so this shape-shift is a hard pre-step — do not pipe the raw `build-tasks` output into `write-schedule`.

Apply filters and overrides at this seam (on the in-memory schedule, before persistence):

- `--claude-only` or `codex_available=false` → rewrite `tasks[].agent = "claude"` in the in-memory schedule.
- `--codex-only` → drop claude tasks.
- `--task-ids` → now stays fully in-memory alongside every other branch. `filter-schedule` supports `--stdin` (user-authorized TASK-005 scope expansion), so the round-3 "pre-persist / filter / re-persist" workaround is retired. On the `--stdin` path `filter-schedule` accepts any schedule `outcome` that parses — including `outcome='needs-enrichment'` emitted by the Step 3 warnings→gaps mapping — so the in-memory schedule flows through filtering without a round-trip to disk. Sequence:
  ```bash
  # Filter the in-memory schedule directly; no mid-pipeline persistence.
  in_memory_schedule=$(printf '%s' "$in_memory_schedule" \
    | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" filter-schedule \
      --stdin --task-ids <csv> --json)
  ```
  `filter-schedule` emits the requested IDs plus their transitive prerequisites in source order, preserving the source schedule's batch boundaries (it trims `task_ids` within surviving batches; it does not re-batch). Unknown requested ID halts with `unknown-task-id`; a transitive dep missing from `tasks[]` halts with `missing-dependency`; a cycle in the filtered subgraph halts with `dependency-cycle`. The in-memory invariant is now universal — **every branch waits for Step 4's lone `write-schedule`**. `--task-ids` no longer writes a mid-pipeline schedule. (The `--schedule-file` path of `filter-schedule` is retained for backward compatibility and still requires `outcome='valid'`; only `--stdin` relaxes that gate.)

The filtered in-memory schedule flows directly into Step 4 — no `compute-schedule` re-pipe in either branch. Persistence still happens exclusively in Step 4.

### Step 4 — Persist the schedule and gate

Persist the final schedule (after filter rewrites and any required batch recomputation) by piping the in-memory JSON through `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" write-schedule --schedule-file <schedule_file> --stdin --json`. This is the sole supported path for persistence; never write the file with the Write tool or inline Python. `write-schedule` runs the same shared validator as `parse-schedule` and refuses to write on any validation error. **No mid-pipeline schedule file exists before this call on any branch** — steps 1-3 run entirely in memory; the first on-disk schedule is the `write-schedule` output. The `--task-ids` branch matches the default: `filter-schedule --stdin` (user-authorized TASK-005 scope expansion) closes the round-3 gap that previously required a one-time early persist. Step 4's call is the single canonical persistence point and the `schedule-valid` gate anchor for every branch.

Immediately after the schedule is persisted, run the `schedule-valid` phase gate so the dry-run / execute promotion bundle is complete (the Phase 0 preflight batch excluded this gate because the schedule file did not exist yet):

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
  --check schedule-valid --schedule-file <schedule_file> --json
```

Halt on `status: fail` with `run_end reason=schedule_gate_failed`. `write-schedule` already refuses to persist on validation errors, so this gate is a redundant belt-and-braces check that the gate model and the writer agree by construction.

### Phase 1-triage — plan-analyst triage (build-tasks warnings third opinion)

**Fire conditions.** `build-tasks` returned `ok: true` with at least one entry in `warnings[]` (the new-flow analogue of the retired `outcome=needs-enrichment`), AND `--allow-gaps` NOT set AND `--analyst-binding` NOT set. Evaluate `--allow-gaps` first: if set, skip triage and warn + proceed regardless of `--analyst-binding`. Only if `--allow-gaps` is NOT set does `--analyst-binding` take effect (halt). Otherwise take the skip branches above (allow-gaps → warn + proceed; binding → halt).

**Dispatch.** `Agent(subagent_type: "plan-review-triage", model: "sonnet", prompt: render(templates.PlanTriage, source="plan-analyst", plan_text, warnings, analyst_outcome, findings_count))`. Uses the Phase 1-triage / Phase 1.5.5 dispatch template from `dispatch-templates.md` with `source="plan-analyst"`. `schedule_path` is intentionally absent — no schedule file exists at this seam (Phase 1-triage fires from Step 1, before Step 4 `write-schedule`); the triage subagent operates solely on the plan text and `warnings[]`. The full shape of the `warnings[]` payload (field names, per-entry schema, renderer placeholder substitution) is TASK-007's scope; until that lands the triage template continues to reference "analyst gaps" in its prose — treat `warnings[]` as the new source that the template's legacy "gaps" placeholder binds to.

**Parse.**

```bash
printf '%s' "<agent_output>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" \
  parse-plan-review-triage-report --stdin --source plan-analyst --findings-count <N> --json
```

`<N>` is the length of the `warnings[]` array from the prior `build-tasks --json` call (the new analogue of the retired analyst `gaps[]`). The parser returns `{verdict, load_bearing, dismissed, summary, findings_count, source}`.

**Run-log events.** Wrap the dispatch:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_triage_start \
  --fields-json '{"run_id":"<id>","plan_file":"<basename>","source":"plan-analyst","findings_count":<N>}' --json

# Agent dispatch (plan-review-triage, model: sonnet) — Phase 1-triage template.

$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_triage_done \
  --fields-json '{"run_id":"<id>","plan_file":"<basename>","source":"plan-analyst","verdict":"<v>","load_bearing_count":<k>,"dismissed_count":<m>,"summary":"..."}' --json
```

**Route by verdict.** (Verdict-routing language is shared with Phase 1.5.5 — see the routing ladder there. The concrete per-source actions are:)

| Verdict | Action (analyst source) |
|---|---|
| `ship` | **Continue Phase 1 at Step 2** (classifier/skip), then Step 3 (filter / agent-override seam), then Step 4 (write-schedule + `schedule-valid` gate). **Then proceed to Phase 1.5.** Triage treats the warnings as green-lit, so the persisted schedule preserves them under `gaps[]` via the Step 3 shape-shift mapping (`outcome='needs-enrichment'`, `severity: "soft"` on every mapped entry). No `plan-author`, no re-run of Phase 1. Summary carries bare `[analyst-triage-disagreement]` banner listing the `build-tasks warnings[]` verbatim. |
| `ship-with-fixes` | **Continue Phase 1 at Step 2** (classifier/skip), then Step 3, then Step 4 (write-schedule + `schedule-valid` gate). **Then proceed to Phase 1.5.** Same schedule-shape behavior as `ship` (warnings preserved under `gaps[]`, `outcome='needs-enrichment'`); the difference versus `ship` is downstream routing: `build-tasks warnings[]` carried to the summary's "Analyst triage notes" section (rather than the bare `[analyst-triage-disagreement]` banner). |
| `partial-agreement` | Dispatch `plan-author` with a warnings payload filtered to the `load_bearing` indices only (new enrichment path). After plan-author edits the child file in place, **re-run Phase 1 end-to-end** (`build-tasks → classifier → write-schedule + schedule-valid gate`); the second-pass verdict is binding — no second triage. Dismissed warning indices carried to summary. |
| `needs-rework` | Dispatch `plan-author` with the full `warnings[]` array (new auto-enrichment path). After plan-author + **full Phase 1 re-run** (`build-tasks → classifier → write-schedule + schedule-valid gate`), the second-pass verdict is binding — no second triage. |

**Routing rationale.** Every verdict MUST land in a persisted, gate-validated schedule before Phase 1.5 fires — Phase 1.5 (Codex plan-review) requires `<schedule_file>` as mandatory input. The prior routing had `ship` / `ship-with-fixes` skip Step 2/3/4 directly to Phase 1.5, which left no persisted schedule on disk for the reviewer. The new routing keeps every verdict on the same Step 2 → Step 3 → Step 4 → Phase 1.5 rail; the verdict only controls (a) whether `plan-author` fires first and (b) the summary-banner text.

On `partial-agreement` and `needs-rework`, re-run the Phase 1 `build-tasks → classifier → write-schedule + schedule-valid gate` sequence after the author edit (TASK-007 wires the concrete re-run; v1 mirrors today's "re-run the analyst" loop against the refactored entrypoint). If the second pass still surfaces `warnings[*]`, halt with `run_end reason=plan_analyst_failed` — NO second analyst-source triage is dispatched. See the re-source-verdict-is-binding rule under `## Rules`.

### Phase 1.5 — Independent plan review (pre-dispatch gate)

The analyst (Claude/Opus) authored the plan *and* validated the schedule — the same family double-checking itself. Before any batch runs, dispatch an independent reviewer for a pre-dispatch review of the persisted schedule. Reviewer returns `approved | approved-with-notes | needs-replan`.

**Route-switch (TASK-002): pick the reviewer mechanism by `claude_only`.**

- **`claude_only=true`** → Phase 1.5-Claude path: dispatch the `plan-reviewer` Agent (`subagent_type: "plan-reviewer", model: "sonnet"`) using the Phase 1.5-Claude template from `dispatch-templates.md`. The agent emits a markdown report whose body concludes with a single fenced ```json block conforming to `codex_plan_review_schema.json`; the orchestrator pipes the JSON through `parse-plan-review-report --stdin --from-claude --json`. Run-log events on this path carry `reviewer:"claude"`.
- **`claude_only=false`** → existing Codex wrapper path: shell out to `plan_codex_dispatch.py plan-review` (block below). The wrapper envelope flows through `parse-plan-review-report --stdin --json` (no `--from-claude` flag). Run-log events on this path carry `reviewer:"codex"`.

Both branches feed the **same** `parse-plan-review-report` parser and produce the same `{plan_file, verdict, findings_count, findings, notes, schedule_ok, summary}` shape — the verdict-routing table below, the `--codex-plan-review-binding` mutex (which is mutex with `--claude-only` per TASK-001), the auto-revise `plan-author` path, and the `--allow-gaps` demotion all consume the parsed verdict, not the dispatch mechanism. The only differences between the two branches are the dispatch invocation and the `reviewer` field in the run-log events.

**Skip condition** (single condition; the legacy `codex_available=false → plan_review_skipped {reason:"codex_unavailable"}` clause was retired in TASK-002 — that case now flows through the `claude_only=true` route-switch above and dispatches the Claude reviewer):

- `--skip-plan-review` → log `plan_review_skipped {reason:"flag"}` and proceed. Final run summary MUST carry a loud banner: *"Plan review skipped via --skip-plan-review"*. This flag is parallel-safe with `--skip-cross-review` and works alongside `--dry-run`, `--codex-only`, `--claude-only`, and `--task-ids`.

Otherwise, proceed with the route-switched review.

**Phase 1.5-Claude path (`claude_only=true`).** Wrap the Agent dispatch with `plan_review_start {reviewer:"claude", plan_file:"<basename>"}` before and `plan_review_done {reviewer:"claude", verdict, findings_count, summary}` after:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_start \
  --fields-json '{"reviewer":"claude","plan_file":"<basename>"}' --json

# Agent dispatch (plan-reviewer, model: sonnet) — Phase 1.5-Claude template
# from dispatch-templates.md. Inputs: plan_path, schedule_path, repo_root,
# plan_basename, findings_count, allow_gaps_demotion.

# Pipe the agent's emitted JSON block (extracted from its markdown report)
# through the parser with --from-claude:
printf '%s' "<agent_output_extracted_json>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" \
  parse-plan-review-report --stdin --from-claude --json

$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_done \
  --fields-json '{"reviewer":"claude","plan_file":"<basename>","verdict":"<v>","findings_count":<n>,"summary":"..."}' --json
```

**Phase 1.5-Codex path (`claude_only=false`).** Wrap the wrapper shell-out with `plan_review_start {reviewer:"codex", plan_file:"<basename>"}` before and `plan_review_done {reviewer:"codex", verdict, findings_count, summary}` after:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_start \
  --fields-json '{"reviewer":"codex","plan_file":"<basename>"}' --json

$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" plan-review \
  --schedule-file <schedule_file> \
  --repo-root <absolute repo root> \
  --timeout 180 \
  [--allow-gaps]

# Schedule-only review (post-TASK-006/008): the reviewer reads only the persisted
# <schedule_file> for the unified fat tasks[] (description + acceptance_criteria
# included) and validates DAG / file-disjointness / classification / AC-vs-files
# alignment from the schedule alone. --plan-file and --plans-dir were removed
# in TASK-008. Codex returns approved|approved-with-notes|needs-replan against
# the unified schedule.

```

The full wrapper envelope (produced by `plan_codex_dispatch.py plan-review`) has shape:

```json
{
  "task_id": "plan",
  "subcommand": "plan-review",
  "outcome": "success",
  "codex_exit_code": 0,
  "parsed": { "plan_file": "...", "verdict": "...", "findings": [...], "notes": [...], "schedule_ok": true, "summary": "..." }
}
```

Pipe the entire envelope (not just `parsed`) into `parse-plan-review-report` (no `--from-claude` flag on this path):

```bash
printf '%s' "<envelope>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" \
  parse-plan-review-report --stdin --json
```

`parse-plan-review-report` validates the envelope against `codex_plan_review_schema.json` and extracts `{plan_file, verdict, findings_count, findings, notes, schedule_ok, summary}`. Cross-plan dependency resolution is verified by the orchestrator's Phase 0 `check-plan-deps` gate and is no longer surfaced by the reviewer. Schema violations halt with structured `errors[*]`. Wrapper timeout / parse_error / failure outcomes surface as `outcome ∈ {timeout, parse_error, failure}`; treat as `plan_review_skipped {reason:"codex_unavailable"}` for routing purposes — the pre-dispatch gate degrades on reviewer-side errors rather than blocking execution. (The Phase 1.5-Claude path's Agent dispatch failures are treated symmetrically — degrade to `plan_review_skipped {reason:"claude_review_failure"}` for routing purposes per the `dispatch-templates.md` §Phase 1.5-Claude note.)

Append `plan_review_done {reviewer, verdict, findings_count, summary}` and route by verdict:

| Verdict | Route |
|---|---|
| `approved` | Proceed to Phase 2 (batch dispatch). |
| `approved-with-notes` | Proceed to Phase 2. Carry `findings[]` into the final run summary under a *"Plan review notes"* section. Do not gate execution on notes. |
| `needs-replan` | Dispatch Phase 1.5.5 plan-review-triage (default, unless `--codex-plan-review-binding` or `--no-auto-revise`). Triage verdict routes per the table in §Phase 1.5.5 below: `ship`/`ship-with-fixes` proceed to Phase 2 directly; `partial-agreement`/`needs-rework` dispatch `plan-author` with a filtered or full findings payload, then re-analyst, then re-run Codex `plan-review`. Second `needs-replan` halts. |

**`--allow-gaps` severity-aware demotion (TASK-003).** When the orchestrator was invoked with `--allow-gaps`, forward the flag to the wrapper by appending `--allow-gaps` to the `plan-review` command above. The wrapper inspects the persisted schedule and, **iff** `gaps[]` is non-empty AND every entry's `severity` is `"soft"` AND the schedule has no structural violations (`outcome == "needs-enrichment"` — `"valid"` by contract requires empty `gaps[]`, and missing/unknown outcomes suppress the demotion), injects a demotion clause into the Codex prompt. Codex then returns verdict `approved-with-notes` (with the demotion recorded in its `summary`) instead of `needs-replan`, which routes directly to Phase 2 and **explicitly short-circuits the `plan-author` auto-revise dispatch**. Any hard-severity gap (or a structural violation) suppresses the demotion clause — the reviewer applies the standard verdict vocabulary and `needs-replan` still dispatches `plan-author` per the routing table above. The wrapper never mutates the persisted schedule; the demotion is a pure function of the prompt inputs.

### Phase 1.5.5 — plan-review triage (needs-replan third opinion)

**Fire conditions.** Codex verdict `needs-replan` AND `--codex-plan-review-binding` NOT set AND `--no-auto-revise` NOT set.

**Skip conditions.**

- `--codex-plan-review-binding` → halt immediately with `run_end reason=plan_review_failed`; NO triage dispatch, NO plan-author dispatch. Parallel to `--codex-review-binding` at task level.
- `--no-auto-revise` → halt immediately (today's behavior, unchanged); NO triage dispatch.

**Dispatch.** `Agent(subagent_type: "plan-review-triage", model: "sonnet", prompt: render(templates.PlanTriage, source="codex-plan-review", plan_text, findings, codex_summary, schedule_path, findings_count))`. Uses the same Phase 1-triage / Phase 1.5.5 dispatch template from `dispatch-templates.md` with `source="codex-plan-review"` — single template, two seams.

**Parse.**

```bash
printf '%s' "<agent_output>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" \
  parse-plan-review-triage-report --stdin --source codex-plan-review --findings-count <N> --json
```

`<N>` is the `findings_count` from the prior `parse-plan-review-report --json` call.

**Run-log events.** Wrap the dispatch:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_triage_start \
  --fields-json '{"run_id":"<id>","plan_file":"<basename>","source":"codex-plan-review","findings_count":<N>}' --json

# Agent dispatch (plan-review-triage, model: sonnet) — Phase 1.5.5 template.

$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_triage_done \
  --fields-json '{"run_id":"<id>","plan_file":"<basename>","source":"codex-plan-review","verdict":"<v>","load_bearing_count":<k>,"dismissed_count":<m>,"summary":"..."}' --json
```

**Route by verdict.**

| Verdict | Action (Codex source) |
|---|---|
| `ship` | Proceed to Phase 2 (batch dispatch). Skip `plan-author`, skip re-analyst, skip second plan-review. Summary carries bare `[plan-review-disagreement]` banner listing Codex findings verbatim. |
| `ship-with-fixes` | Proceed to Phase 2. Codex findings carried to the summary's "Plan review notes" section. |
| `partial-agreement` | Dispatch `plan-author` with a findings payload filtered to the `load_bearing` indices only. Dismissed findings carried verbatim to the summary's "Plan review notes" section with the dismissed indices called out. Then re-analyst, then re-run Codex `plan-review`; the second verdict is binding — no second triage. |
| `needs-rework` | Dispatch `plan-author` with the full findings array (today's auto-revise behavior). Then re-analyst, then re-run Codex `plan-review`; the second verdict is binding — no second triage. |

On `partial-agreement` and `needs-rework` the routing then falls through to the existing author → analyst → review sequence documented below; the triage selects the payload the author receives (filtered vs full) but does NOT change the three-step sequence.

**Per-finding embeds in the triage dispatch (TASK-007).** Before dispatching the Phase 1.5.5 triage, the orchestrator enriches the Codex `findings[]` payload so the triage agent can reason about prioritization: every finding in the embedded JSON carries `{target_task_id, blocking, severity}` alongside the existing `section`, `concern`, `suggested_change`. The triage template (see `dispatch-templates.md` §Phase 1-triage / Phase 1.5.5) instructs the agent to evaluate the findings in priority order: `blocking=true` first, then `severity=critical`, then `severity=important`, then `severity=minor`. Schedule-level findings (`target_task_id=null`) are evaluated against the schedule JSON + roster rather than a single task block; per-task findings (`target_task_id="NNN"`) are evaluated against the corresponding `tasks[NNN]` entry.

**`needs-replan` branch — auto-revise path (default).** Auto-revise is on unless `--no-auto-revise` is set. When on, the orchestrator runs a three-step author → analyst → review sequence before the second verdict is accepted:

1. **Dispatch `plan-author` per finding (fan-out, TASK-007)** (Agent, `subagent_type: "plan-author"`, `model: "opus"`) using the Phase 1.5a template from `dispatch-templates.md`. **The payload has shifted from a single whole-plan author dispatch to N per-finding dispatches.** For every finding in the filtered-or-full findings array (filtered to triage `load_bearing` indices on `partial-agreement`; full on `needs-rework`) the orchestrator dispatches a separate `plan-author` agent carrying exactly ONE finding plus the inputs needed to locate its edit target. **The fan-out is one-per-finding for both task-targeted AND schedule-level findings — schedule-level findings are NOT collapsed into a single dispatch.** The author's write scope is the single named file for that dispatch — NOT the whole plan, NOT sibling children. The dispatch input shape depends on `target_task_id`:
   - **Task-targeted (`target_task_id="NNN"`)** — triple is `{finding, target_task_id, child_plan_file}`; resolve `finding.target_task_id → child_plan_file` via the schedule's `tasks[].plan_file` (same resolution rule as §Per-task `<plan-file>` resolution). `roster_file` is absent on this path. The author edits that one child file in place; `00_INDEX.json` is off-limits.
   - **Schedule-level (`target_task_id=null`)** — triple is `{finding, target_task_id=null, roster_file}` with `child_plan_file` absent or explicitly `null`. The orchestrator passes `roster_file=<plan_dir>/00_INDEX.json` (absolute path to the schedule roster) so the author has a concrete file target. The triple still gets its own dispatch per schedule-level finding. The author's allowed edit surface is `roster_file` (roster edit) OR empty (`files_edited: []` with a justification note that the finding does not warrant a file change). If multiple schedule-level findings arrive in the same review pass, each dispatches separately; the author may choose to touch `roster_file` idempotently across those dispatches — overlapping roster edits across sibling schedule-level dispatches are expected and fine. The Phase 1.5a template (`dispatch-templates.md` §Phase 1.5a) renders two visually distinct variants — Variant A (task-targeted) and Variant B (schedule-level) — selected per dispatch; the orchestrator never renders both variants in the same dispatch.

   Dismissed findings on `partial-agreement` are NOT forwarded to any author — they are carried only into the summary per the route table above. Wrap EACH per-finding dispatch with `plan_author_start` before and `plan_author_done` after:

   ```bash
   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event plan_author_start \
     --fields-json '{"run_id":"<id>","plan_file":"<basename>","findings_count":<N>}' --json

   # Agent dispatch (plan-author, model: opus) — Phase 1.5a template.

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event plan_author_done \
     --fields-json '{"run_id":"<id>","plan_file":"<basename>","files_edited":[...],"findings_actioned":[...],"findings_skipped":[...]}' --json
   ```

2. **Re-run Phase 1** (`build-tasks → classifier → write-schedule`) for structural re-validation of the revised plan file. The build step reads the plan from disk — do NOT forward the author's edit report (that would invite ping-pong). Route on the re-validation outcome:

   - `build-tasks errors[*] non-empty` → halt with `run_end reason=plan_review_failed reason_detail=author_introduced_structural_defect`. The author produced a structurally broken revision; do not run the second review against a malformed plan.
   - `build-tasks warnings[*] non-empty` → same allow-gaps routing as the first pass (halt if `--allow-gaps` is not set; warn and proceed otherwise).
   - `build-tasks ok: true` with empty `warnings[]` → proceed to step 3.

3. **Re-run Codex `plan-review`** on the revised plan. The second verdict is binding: `approved | approved-with-notes` → proceed to batch dispatch; `needs-replan` → halt per the "Second `needs-replan`" block below.

**`needs-replan` branch — `--no-auto-revise` path (opt-out).** When `--no-auto-revise` is set, the first `needs-replan` verdict halts immediately without dispatching `plan-author`. This preserves the pre-TASK-025 behavior for users who prefer to apply revisions by hand; halt with `run_end reason=plan_review_failed` per the block below. No silent retry without revision.

**Second `needs-replan`** (after one author → analyst → review retry, on the auto-revise path), OR the first `needs-replan` when `--no-auto-revise` is set: halt before any batch runs.

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event run_end \
  --fields-json '{"run_id":"<id>","outcome":"failed","reason":"plan_review_failed","findings":[...]}' --json
```

Release the run-lock and print the failure envelope. Do NOT call `fail-task` (no task has started). Do NOT run batches. The run summary records `outcome=failed reason=plan_review_failed`.

**Retry failures** — if the `plan-author` dispatch itself fails (malformed report, out-of-scope writes) or the re-validation analyst returns `outcome=invalid`, halt with `run_end reason=plan_review_failed` same as the second-`needs-replan` path.

**Run-log event order** (V8, with plan-review triage interleaved on both pedantic-flag paths):

- **Analyst branch** (Phase 1): `run_start` → (if `build-tasks` returns `warnings[*]` non-empty AND triage fires; neither `--analyst-binding` nor `--allow-gaps` set) `plan_review_triage_start {source:"plan-analyst"}` → `plan_review_triage_done {source:"plan-analyst", verdict}` → (branch by verdict) `schedule_written` → `analyst_done {outcome:"needs-enrichment"}` (for `ship` / `ship-with-fixes` — the `outcome` string preserves the legacy vocabulary for v1 log consumers) | `plan_author_start` → `plan_author_done` → `schedule_written` → `analyst_done` (for `partial-agreement` / `needs-rework`). When `build-tasks` returns with empty `warnings[]`, the triage events are skipped and the order is `run_start` → `schedule_written` → `analyst_done {outcome:"valid"}`. When `--analyst-binding` is set, emit `analyst_triage_skipped {reason:"binding_flag"}` and halt; when `--allow-gaps` is set, emit `analyst_triage_skipped {reason:"allow_gaps"}` and proceed to `schedule_written` → `analyst_done` with today's demotion. In every variant, **`analyst_done` is emitted only after `write-schedule` persists** — it marks end-of-Phase-1, not mid-pipeline. Classifier fan-out dispatches are not represented in this top-level order — they happen inside the single Phase 1 "step" between `run_start` and `schedule_written`.
- **Codex branch** (Phase 1.5): ... → `schedule_written` → `analyst_done` → `plan_review_start {reviewer:"codex"}` → `plan_review_done {verdict:"needs-replan", findings_count}` → (if triage fires; neither `--codex-plan-review-binding` nor `--no-auto-revise` set) `plan_review_triage_start {source:"codex-plan-review"}` → `plan_review_triage_done {source:"codex-plan-review", verdict}` → (branch by verdict) `batch_start` (for `ship` / `ship-with-fixes`) | `plan_author_start` → `plan_author_done` → `schedule_written` → `analyst_done` → `plan_review_start` → `plan_review_done` → `batch_start` (for `partial-agreement` / `needs-rework`, only if the second verdict permits).

Both source variants of the triage events share the event names `plan_review_triage_start` / `plan_review_triage_done`; the `source ∈ {plan-analyst, codex-plan-review}` field discriminates them in the log.

### Dry-run mode

If `--dry-run`: print the schedule + intended dispatches. Release lock. Exit. Dry-run scope is sticky — a follow-up "actually run it" message requires a fresh invocation without `--dry-run`, or explicit user instruction.

## Execute mode — per-batch A→E loop

```
ready          : analyst batch order; within a batch, batch-next serializes by file-lock availability
done           : set[task_id] = {}
failed         : set[task_id] = {}
locked_files   : set[str] = {}
committed      : list[(task_id, sha)]
review_notes   : dict[task_id -> list[minor findings]] = {}
```

Loop until `ready` is empty OR scheduler stuck.

### Phase A — Select batch

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" batch-next \
  --schedule-file <path> \
  --locked-files <comma-separated> \
  --done <comma-separated> \
  --failed <comma-separated> \
  --parallel N --json
```

Empty batch + non-empty ready → halt "scheduler stuck". Empty batch + empty ready → exit loop. Append `batch_start` event. Add the batch's files to `locked_files`. Include each picked task's `plan_file` alongside its `task_id` in the `batch_start` event (e.g., `fields.tasks: [{"task_id": "002", "plan_file": "TASK-002_write_a.md"}, ...]`), so a cross-child parallel batch is visible in the run log.

### Phase B — Implement (parallel, one message)

Dispatch all batch tasks in a **single message** — Claude via Agent, Codex via Bash:

- Log `implement_start {task_id, agent, model?, batch_index}` per task (chain into the dispatch via `&&` when convenient). Include `plan_file: "<child-basename>"` for the task so the run-log records which child file the implementer's commit will land in.
- **Claude tasks** → `Agent(subagent_type: "plan-implementer", model: "opus", prompt: render(templates.PhaseB, ...))`.
- **Codex tasks** → Wrapper computes the timeout default from `len(task["files"])` per the formula in §Bash-call idioms; pass `--timeout N` to override.
  `Bash: $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement --plan-file <abs> --task-id NNN --repo-root <abs>`.

Await all. For EVERY task (success or not) append `implement_done {task_id, outcome, files_changed[], test_outcome, wall_seconds}`.

**Classify per task:**

*Claude response (markdown):*

```bash
printf '%s' "<agent_output>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-implementer-report --stdin --json
```

Gets `{outcome, files_changed, diff_summary, test_outcome, concerns, plan_adaptations, warnings, diagnostics, reversion_guidance?}`. `concerns` and `plan_adaptations` are `list[str]` (one entry per bullet); `warnings` is `list[str]` reserved for future non-halting parser advisories (TASK-008 retired the legacy-alias warning channel — the deprecated `**Concerns:**` fallback was REMOVED). Missing `**Concerns for reviewer:**` section now surfaces as a `missing-concerns-for-reviewer` diagnostic. `diagnostics` is a `list[dict]` of `{code, message}` entries flagging absent mandatory section headers (e.g. missing `**Plan adaptations:**`). Non-halting — surface to the reviewer for visibility; do not gate on it. Post-hoc scope check:

```bash
git diff --name-only HEAD -- <task.files>
```

If the intersection with `files_changed` does not equal `files_changed`, reclassify as `failed reason=scope_violation`.

`outcome=success` → Phase D. `partial | failed | plan-incorrect | blocked | malformed` → Phase C.

*Codex envelope (JSON from wrapper):*

- `outcome=success` AND `parsed.test_result.result=passed` AND `out_of_scope_observed=false` → Phase D.
- `outcome ∈ {failure, timeout, parse_error, scope_violation}` → log `fallback_used`; re-dispatch to Claude once (Phase B template). Fallback success → Phase D as Claude-implemented. Fallback failure → Phase C.
- `outcome=dry_run` outside `--dry-run` → halt with internal-error.

No `codex_not_found` branch — wrapper emits `outcome=failure` + `error="codex binary not found on PATH"`; preflight already catches it.

At the batch join barrier (after every wrapper in a batch returns, before per-task review/commit dispatch), the orchestrator reconciles observed out-of-scope writes via `plan_ops.py reconcile-batch --repo-root <repo> --schedule-file <path> < envelopes.json`. The `--schedule-file` argument is the persisted schedule JSON path bound during Phase 1.5 (`<plan_dir>/<plan>.schedule.json` or the directory-mode `<plan_dir>/<basename>.schedule.json`). With it, each envelope's `out_of_scope_tracked` / `out_of_scope_untracked` is partitioned against the dispatched task's normalised `Files:` list: declared-in-scope entries are PRESERVED (recorded in `reconcile_kept_tracked` / `reconcile_kept_untracked` with outcome `scope_violation_preserved` if nothing else was restored), genuinely out-of-scope entries are restored as before. WITHOUT `--schedule-file` (or on schedule-load failure), the orchestrator falls back to restore-everything behaviour and may wipe a correct in-scope edit when a stale wrapper produces a buggy `out_of_scope_*` envelope — always pass `--schedule-file` for `/implement-plan` runs. Any `reconciliation_failed` result is a hard halt — do NOT advance to the next batch or dispatch review for any task in the affected batch.

### Phase C — Handle Phase B failures

#### Sandbox divergence escape hatch (TASK-008)

Before classifying a Codex `implement` failure with `cause: independent_test_run_failed`, the orchestrator runs the auto-validate branch — re-execute the task's declared `Test command:` in the target env (cwd = repo root, env inherited). The wrapper's recorded `sandbox_test_command` is NOT used (it may carry an environment-specific prefix that breaks in target).

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" auto-validate-divergence \
  --envelope-file <wrapper-envelope.json> \
  --test-command "<task Test command:>" \
  --repo-root <repo> --run-id <id> --task-id NNN --json
```

- **Target-passes** (`divergence: true`): treat the work as success. The handler appends a structured `sandbox_divergence` event to the run log carrying both the wrapper's sandbox stdout/stderr captures (truncated at 32 KB with `truncated_to` markers) and the target-env captures. The next `commit-task` invocation MUST pass `--sandbox-divergence-tag` so the commit body shows `[sandbox-divergence]` alongside any existing `[disagreement]` / `[remediation]` tags. Cross-review (Phase D) proceeds as if the implementer succeeded.
- **Target-fails** (`divergence: false`, `applicable: true`): treat as a real failure. The existing failure path (Codex→Claude fallback OR `fail-task`) runs unchanged.
- **Non-matching cause** (`applicable: false`): the envelope did not surface `cause: independent_test_run_failed`; the handler is a no-op and the existing failure path runs unchanged.

The `[sandbox-divergence]` tag is informational. It does NOT relax the reviewer-verdict whitelist — `commit-task --reviewer codex --reviewer-verdict <bogus>` still fails the same way it always did. `--reviewer none` remains the final-resort override (used only when human judgement decides cross-review is unobtainable); the auto-validate branch is the sanctioned recovery for sandbox divergences and must be preferred over `--reviewer none` when the divergence is the failure cause.

End-of-run summary: `plan_ops.py run-summary --section sandbox-divergences --run-id <id>` emits the "Sandbox divergences" subsection listing every task that hit the auto-validate branch under the run id.

For each non-success task, the orchestrator FIRST runs the empty-diff probe to decide whether the failure left preservable work in the working tree. The probe is the gate between auto-revert (cheap and correct when the diff is empty) and halt-with-pause (mandatory when the diff is non-empty — see **Completed-Work Preservation Principle** in §Rules).

```bash
git diff HEAD --quiet -- <touched files>
```

`<touched files>` is the union of the implementer-report `files_changed` and the task's declared `Files:` list. Exit code `0` → empty diff (no preservable work); exit code `1` → non-empty diff (preservable work present).

**Empty-diff branch (`git diff HEAD --quiet` exits 0).** Nothing to lose; auto fail-task is correct.

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" fail-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <touched files> --stage implement --reason "<short>" \
  --authorization-source phase-c-empty-diff \
  [--reversion-guidance "<from implementer report>"] --json
```

`<abs>` here is the current task's child file resolved per the rule in §Per-task `<plan-file>` resolution.

This: (1) `git restore <files>` (Claude-side recovery; Codex-side restore was done inside the wrapper — typically a no-op on the empty-diff branch but kept for idempotency), (2) plan-status flip to `failed`, (3) run-log `failed {stage=implement, ...}` append.

**Non-empty-diff branch (`git diff HEAD --quiet` exits 1).** Preservable work is in the working tree. The orchestrator MUST NOT call `fail-task` on the implicit `phase-c-empty-diff` authorization (which would silently destroy the diff). Routing is decided by the pinned `$UNATTENDED_REVERT_POLICY` (set at preflight per TASK-003):

- **`$UNATTENDED_REVERT_POLICY = pause`** (the interactive default) → invoke the **Awaiting-user pause** subroutine (see §Awaiting-user pause). Per the call-site table, this Phase C site uses `stage:"post_implement_failure"` and the payload fields `implementer_outcome`, `diagnostics`, `reversion_guidance`, `nonempty_diff_files[]`. Halt-with-pause: do NOT call `fail-task`; do NOT `git restore`; do NOT mutate plan-status to `failed`. Return control to the user with the diff still in the working tree.
- **`$UNATTENDED_REVERT_POLICY = fail-fast`** → emit a structured failure record and halt the run instead of pausing. Operationally this is the same `fail-task` invocation as the empty-diff branch (`--authorization-source phase-c-empty-diff`, since the operator pinned fail-fast they have explicitly authorized destruction in the unattended environment), then halt the run with `run_end outcome=failed reason=phase_c_unattended_fail_fast`. The diff is destroyed; the structured failure record names the policy pin so the run history makes the decision auditable.
- **`$UNATTENDED_REVERT_POLICY = preserve-only`** → salvage-then-fail. Stash/log the diff to a salvage ref (per the salvage helper introduced alongside TASK-003), then run the same `fail-task --authorization-source phase-c-empty-diff` invocation as the empty-diff branch. The salvage ref preserves the diff for later inspection while the working tree returns to a clean state for downstream batches.

After fail-task lands (empty-diff branch OR fail-fast / preserve-only sub-branches of the non-empty-diff branch — but NOT the pause sub-branch, which exits via the awaiting-user subroutine), cascade `blocked` onto the failed task's transitive dependents (source-of-truth invariant: the plan file, not just the run-log):

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" block-dependents \
  --schedule-file <path> --plan-file <abs> --failed NNN --run-id <id> --json
```

Pass the **failed task's** child file as `--plan-file` (same resolution rule). `block-dependents` reads the schedule's per-task `plan_file` for each dependent internally and routes each mutation to the right child file; siblings in other children flip there, not in the failed task's file. The `--plan-file` argument serves as the DAG-lookup anchor; per-dependent mutation routes through each dependent's own `tasks[].plan_file`.

Release this task's file locks. Remove the task from `ready`. Peer tasks in the same and later batches proceed independently. Do NOT proceed to Phase D for this task.

See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn. The non-empty-diff `pause` sub-branch is the canonical Phase C application of that principle; `fail-fast` and `preserve-only` are operator-pinned overrides authorized only in unattended contexts.

### Phase D — Review + commit (serial per task, analyst batch order)

**If `--skip-cross-review`:** skip to D.3 immediately. Log `review_skipped`. Final summary shows a loud warning banner.

**Final-summary cross-review banner.** When `review_skipped` events fire (for any reason — `--skip-cross-review`, or the Codex-side wrapper-failure routing in §D.1 below), the run summary MUST carry a loud banner: *"Cross-review skipped on N tasks (codex unavailable)"* listing each affected `task_id` and its `reason`. This complements the `--skip-cross-review` banner clause; both surface the same `review_skipped` event with different `reason` values.

Otherwise, per successful task:

#### D.1 — Dispatch the opposite-side reviewer

**Route-switch (TASK-003): pick the reviewer mechanism by `claude_only`.** Mirrors §Phase 1.5's TASK-002 route-switch — same shape, different seam.

- **`claude_only=true`** → Phase D-Claude path: regardless of which side implemented, dispatch the `code-reviewer` Agent (`subagent_type: "code-reviewer", model: "sonnet"`) using the existing **Phase D-Claude** template from `dispatch-templates.md` (single Claude-cross-review template, used for both Codex-impl→Claude review AND Claude-impl→Claude review under `claude_only=true`). Verdict vocabulary is `{ship, ship-with-fixes, needs-rework}` (matching the existing Codex-impl→Claude review path); the Codex-side `{clean, minor-findings, needs-rework}` vocab is NOT synthesized on this branch. Run-log events on this path carry `reviewer:"claude"`.
- **`claude_only=false`** → existing wrapper / cross-side path below: Claude-implemented work routes to the Codex wrapper review; Codex-implemented work routes to the Claude `code-reviewer` Agent. Run-log events carry `reviewer:"codex"` or `reviewer:"claude"` respectively.

Both branches feed the **same** `review_done` event shape `{task_id, reviewer, verdict, findings_count, minor_findings[]?, disagreement_tag?}` — the only differences are the dispatch invocation, the verdict vocabulary parsed, and the `reviewer` field. The verdict-routing table at §D.2 below consumes the parsed verdict; routing under `claude_only=true` is documented in §D.2a (D.5 / D.2a.5 / D.2a.6 ladder collapses) and §D.2b (role-swap retry uses `code-reviewer` for the re-review).

*Phase D-Claude path (`claude_only=true`):*

`Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD_Claude, ...))`.

Parse verdict `∈ {ship, ship-with-fixes, needs-rework}`. Wrap with `review_start {reviewer:"claude", task_id, ...}` before and `review_done {reviewer:"claude", task_id, verdict, findings_count, ...}` after.

*Phase D-Codex path (`claude_only=false`, Claude-implemented → Codex review):*

Wrapper computes the timeout default from `len(files)` per the review formula in §Bash-call idioms; pass `--timeout N` to override.

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" review \
  --plan-file <abs> --task-id NNN --repo-root <abs> \
  --files <files_changed> --review-focus bugs
```

Parse `parsed.verdict ∈ {clean, minor-findings, needs-rework}`.

*Phase D-Claude path (`claude_only=false`, Codex-implemented → Claude review):*

`Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD_Claude, ...))`.

Parse verdict `∈ {ship, ship-with-fixes, needs-rework}`. Verdict vocab is preserved verbatim — not normalized across reviewer types.

Log `review_start` then `review_done {task_id, reviewer, verdict, findings_count, minor_findings[]?, disagreement_tag?}`. Minor findings persist in `review_notes[task_id]` for the run summary.

When findings are non-empty, also pass `--findings-json "$(<json-array>)"` to the `review_done` `log-event` call so the line carries the full Codex payload verbatim under key `findings`. The audit trail depends on this — count-only `review_done` entries lose the finding text within ~20s.

**Wrapper failure outcomes (Codex side, `claude_only=false` only).** Wrapper timeout / parse_error / failure outcomes from `subcommand=review` (Codex review path only — `cmd_review` emits these envelopes today) log `review_skipped {task_id, reviewer:"codex", reason}` where `reason` maps as: `timeout` → `"codex_review_timeout"`, `parse_error` → `"codex_review_parse_error"`, `failure` → `"codex_review_failure"`. Then proceed straight to D.3 commit with `--reviewer none --reviewer-verdict ""` (the existing `commit-task` form documented at §`commit-task` example (e); do not duplicate the bash). The commit body's reviewer line records "review skipped (reason)" preserving the audit trail. This mirrors the parallel Phase 1.5 rule at line 450 — wrapper-side errors degrade to a documented skip rather than blocking execution. The Codex-implemented → Claude-review direction's `Agent`-side failures are NOT covered by this rule; that path falls under the existing `Agent` retry semantics and the D.5 ladder for substantive disagreement. Under `claude_only=true` this Codex-side wrapper-failure clause does not apply (no Codex shell-out fires); Agent-side dispatch failures degrade through the existing `Agent` retry semantics.

#### D.2 — Route by verdict

| Implementer | Reviewer | clean / minor-findings (or ship / ship-with-fixes) | needs-rework |
|---|---|---|---|
| Claude | Codex | → D.3 commit | D.2a escalate (unless `--codex-review-binding`) |
| Codex | Claude | → D.3 commit | D.2b role-swap retry |
| Claude | Claude (`claude_only=true`) | → D.3 commit | → D.4 fail-task (D.5 / D.2a.5 / D.2a.6 ladder collapses; see §D.2a) |
| Codex | Claude (`claude_only=true`) | → D.3 commit | D.2b role-swap retry — re-review uses `code-reviewer` Agent, NOT the Codex wrapper (see §D.2b) |

Minor findings in either direction → commit; record in run summary AND commit body tail. Never silently dropped.

#### D.2a — Escalation (§8.4, Codex critical on Claude work)

**`claude_only=true` ladder collapse (TASK-003).** Under `claude_only=true`, D.2a (D.5 escalation), D.2a.5 (bounded remediation), and D.2a.6 (narrow-remediation) are **unreachable** for the Claude-impl→Claude-review path: there is no Codex verdict to adjudicate, so the third-opinion ladder has nothing to split. A `code-reviewer` `needs-rework` verdict on a Claude-implemented task under `claude_only=true` goes **straight to D.4 fail-task** — no D.5 third-opinion dispatch, no D.2a.5 bounded remediation, no D.2a.6 narrow remediation, no `--codex-review-binding` interaction. The Codex-impl→Claude-review path under `claude_only=false` (which still hits this section unchanged) is unaffected; the role-swap retry path (§D.2b) is documented separately. The §Rules section carries the corresponding hard rule.

1. Log `disagreement {task_id, codex_findings[]}`. Pass the Codex findings verbatim via `--findings-json "$(<json-array>)"` so the `disagreement` line carries the full payload under key `findings` — D.5 dispatch happens right after, and audit retrieval of "what did Codex flag that D.5 then adjudicated?" depends on this.
2. Dispatch the Phase D.5 template: `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD5, codex_findings, task_block, wrapper_checks))`. `wrapper_checks` is taken from the Codex review envelope's `wrapper_checks` field; if the field is absent (failure/timeout/parse-error envelopes), pass `{"symbol_warnings": []}` as the default so the template's `<wrapper_checks_json>` placeholder always resolves to a valid JSON object.
3. Parse verdict and route per the table below:

| Codex verdict | D.5 verdict | Route | Rationale |
|---|---|---|---|
| `needs-rework` | `ship` \| `ship-with-fixes` | → D.3 with `--disagreement-tag` (existing behavior, unchanged) | D.5 disagreed with Codex; commit wins. Summary row shows `[disagreement]`. |
| `needs-rework` | `partial-agreement` | → **D.2a.6** narrow-remediation retry | D.5 split Codex's findings into load-bearing and dismissed buckets; retry is scoped to the load-bearing subset only. Dismissed indices are recorded in the commit trailer. |
| `needs-rework` | `needs-rework` | → **D.2a.5** bounded remediation retry | Two independent reviewers agree the finding is load-bearing; give the implementer one chance to fix it narrowly. |

`--codex-review-binding` skips D.2a third-opinion + retries — binding mode means `needs-rework` is binding for the commit decision and the orchestrator MUST mark the task `paused` and call **Awaiting-user pause** subroutine with `stage=post_binding_block` (NO D.5, NO D.2a.5, NO D.2a.6). User decides disposition in next turn (revert / hand-fix / accept-as-is via `commit-task` with override rationale). Under `--unattended-revert-policy fail-fast`, the orchestrator calls `fail-task --authorization-source unattended-fail-fast --stage review --reason 'codex-review-binding fail-fast'` instead of pausing. (This flag is mutually exclusive with `--claude-only` per TASK-001's mutex prose; under `claude_only=true` the ladder collapse documented above subsumes the binding mode's effect.) See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn.

#### D.2a.5 — Bounded remediation retry (default, non-binding path only)

Fires when Codex's `needs-rework` is independently confirmed by the D.5 code-reviewer. Strictly one attempt.

1. Log `remediation_start {task_id, findings_count, d5_summary}`.
2. Re-dispatch `plan-implementer` (Agent, `subagent_type: "plan-implementer"`, `model: "opus"`) using the **Phase B-rework** template from `dispatch-templates.md`. The template embeds `codex_findings_json` + `d5_summary` as a structured block and explicitly instructs the implementer to "fix narrowly, do not scope-inflate".
3. Classify the retry with the standard Phase B rules. `outcome ≠ success` → halt per step 6 below (same awaiting-user pause path; do NOT call `fail-task`).
4. On retry success, re-run D.1 (Codex review). The re-review is binding — no further retry regardless of verdict.
5. Route the re-review:
   - `clean | minor-findings` → D.3 commit with `--remediation-tag`. Summary row shows `[remediation]` (and `[disagreement]` if both apply).
   - `needs-rework` (second failure) → proceed to step 6.
6. **Awaiting-user pause** (second `needs-rework`, OR a failed retry implementer outcome): (See **Awaiting-user pause** subroutine — same control flow.)
   - Payload shape depends on which branch triggered the pause:
     - Second-review failure: `stage:"post_remediation_review"`, include `codex_findings:[...]` and `d5_summary:"..."`.
     - Retry-implement failure: `stage:"post_remediation_implement"`, include `retry_outcome`, `diagnostics`, and `reversion_guidance` from the implementer report; omit `codex_findings` (no second review ran).
   - `--ending-sha <sha>` MUST be `git rev-parse HEAD` at pause time — not the starting SHA. A paused run has uncommitted remediation edits in the working tree; the ending SHA captures the last committed state (which is typically the prior task's commit or the run's starting SHA if this is the first task). Log the paths of currently-dirty files in the `awaiting_user` event's `dirty_files` field so the next turn has a concrete handoff.
   ```bash
   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event awaiting_user \
     --fields-json '{"task_id":"NNN","stage":"post_remediation_review","codex_findings":[...],"d5_summary":"...","dirty_files":[...]}' --json

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" finalize-execution-log \
     --run-id <id> --starting-sha <sha> --ending-sha "$(git rev-parse HEAD)" \
     --outcome paused --rows-json '[...]' --json

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event run_end \
     --fields-json '{"run_id":"<id>","outcome":"paused","done":N,"failed":M,"paused_on_task":"NNN"}' --json
   ```
   After the paused `run_end`, the End-of-run sequence (update-plan-header, regular finalize, housekeeping commit) is SKIPPED — the paused branch emits its own `finalize-execution-log --outcome paused` and `run_end outcome=paused` instead. Print the failure envelope, release the run-lock, and return control to the user with pending edits **still in the working tree**. Do NOT call `fail-task`. Do NOT `git restore`. The user's next conversation turn decides disposition:
   - "revert" → user instructs orchestrator to run `fail-task`.
   - "keep as-is" → user instructs orchestrator to run `commit-task` with `--remediation-tag` and an override rationale.
   - "hand-fix" → user edits manually + re-runs review.

   **Hard rule:** D.2a.5's second `needs-rework` MUST NOT trigger `fail-task` automatically. `fail-task` on a paused run requires an explicit user instruction in the next turn. See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn.

#### D.2a.6 — Narrow-remediation retry (partial-agreement path)

Fires when Codex's `needs-rework` verdict is split by the D.5 third-opinion reviewer into load-bearing + dismissed buckets (`partial-agreement`). Strictly one attempt — same bounding as D.2a.5. `--codex-review-binding` skips this entire section: binding mode means `needs-rework` is binding for commit and the orchestrator pauses for user instruction (NO D.5, NO D.2a.5, NO D.2a.6). Under `--unattended-revert-policy fail-fast`, fail-task fires instead of pausing.

1. Log `narrow_remediation_start {task_id, load_bearing_count, dismissed_count, d5_summary}`. The `narrow_remediation_start` / `narrow_remediation_done` events are distinct from D.2a.5's `remediation_start` / `remediation_done`; the run log is the audit source of truth for which retry path fired.
2. Re-dispatch `plan-remediator` (Agent, `subagent_type: "plan-remediator"`, `model: "opus"`) using the **Phase B-narrow-remediation** template from `dispatch-templates.md`. The template embeds `load_bearing_findings_json` (filtered subset of Codex findings where the array index ∈ D.5's `load_bearing`), `dismissed_findings_json` (the complement, labeled "DO NOT fix — context only"), and `d5_summary`. The file:line touch-only scope rule in `plan-remediator.md` structurally bounds the retry; "fix narrowly, do not scope-inflate" remains a prompt-level hint.
3. Classify the retry with the standard Phase B rules plus the new `scope-violation` outcome. `outcome ≠ success` (including `scope-violation`) → halt per step 6 below (same awaiting-user pause path; do NOT call `fail-task`).
4. On retry success, re-run D.1 (Codex review). The re-review is binding — no further retry regardless of verdict.
5. Route the re-review:
   - `clean | minor-findings` → D.3 commit with `--narrow-remediation-tag --dismissed-finding-ids I,J,K` (comma-separated dismissed indices from the D.5 split). Commit body carries `[narrow-remediation]` followed immediately by `[disagreement: I,J,K]`; summary row shows both tags.
   - `needs-rework` (second failure) → proceed to step 6.
6. Log `narrow_remediation_done {task_id, outcome}` before entering the pause (on both the retry-implement-failure and second-review-failure branches, so the log records the attempt's terminal state either way).
7. **Awaiting-user pause** (second `needs-rework`, OR a failed/scope-violation retry implementer outcome): (See **Awaiting-user pause** subroutine — same control flow.)
   - Payload shape mirrors D.2a.5 with a flipped `stage` label and one added field on the review-failure branch:
     - Second-review failure: `stage:"post_narrow_remediation_review"`, include `codex_findings:[...]`, `d5_summary:"..."`, and `dismissed_finding_indices:[...]` (for round-tripping the D.5 split into the next turn).
     - Retry-implement failure: `stage:"post_narrow_remediation_implement"`, include `retry_outcome`, `diagnostics`, and `reversion_guidance` from the implementer report; omit `codex_findings` (no second review ran).
   - `--ending-sha <sha>` MUST be `git rev-parse HEAD` at pause time, same as D.2a.5. Log currently-dirty file paths in the `awaiting_user` event's `dirty_files` field so the next turn has a concrete handoff.
   ```bash
   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event awaiting_user \
     --fields-json '{"task_id":"NNN","stage":"post_narrow_remediation_review","codex_findings":[...],"d5_summary":"...","dismissed_finding_indices":[...],"dirty_files":[...]}' --json

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" finalize-execution-log \
     --run-id <id> --starting-sha <sha> --ending-sha "$(git rev-parse HEAD)" \
     --outcome paused --rows-json '[...]' --json

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event run_end \
     --fields-json '{"run_id":"<id>","outcome":"paused","done":N,"failed":M,"paused_on_task":"NNN"}' --json
   ```
   After the paused `run_end`, the End-of-run sequence (update-plan-header, regular finalize, housekeeping commit) is SKIPPED — the paused branch emits its own `finalize-execution-log --outcome paused` and `run_end outcome=paused` instead. Print the failure envelope, release the run-lock, and return control to the user with pending edits **still in the working tree**. Do NOT call `fail-task`. Do NOT `git restore`. The user's next conversation turn decides disposition (same three options as D.2a.5: "revert" / "keep as-is" / "hand-fix").

   **Hard rule:** D.2a.6's second `needs-rework` or non-success retry MUST NOT trigger `fail-task` automatically. Same protocol as D.2a.5's halt path. See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn.

#### D.2b — Role-swap retry (Codex implements + Claude reviewer needs-rework)

Per §8.3 line 692: Claude re-implements, Codex re-reviews. One attempt.

1. Re-dispatch Phase B template to `plan-implementer` (Agent, `model: "opus"`). Reviewer feedback is NOT forwarded in v1.
2. Classify the retry with the same Phase B rules. `outcome ≠ success` → D.4 reason `retry_implement_failed`.
3. On retry success, re-run D.1 using the **Codex** reviewer. Binding — no further retry.
4. Route re-review: `clean | minor-findings` → D.3. `needs-rework` → D.4.

**`claude_only=true` re-review variant (TASK-003).** Under `claude_only=true`, the implementer side is rewritten to Claude in Phase 1 (per §Phase 1 Step 3's `--claude-only` / `codex_available=false` rewrite), so the Codex-implemented entry condition for this path is structurally unreachable. Documented defensively for contract clarity: were the path ever reachable, step 3's re-review would use the `code-reviewer` Agent (Phase D-Claude template) — NOT the Codex wrapper, because Codex shell-out is forbidden under `claude_only=true` (see §Rules). The retry implement step is unchanged (`plan-implementer` Agent, `model: "opus"`); only the re-review dispatch swaps. Step 4's verdict mapping uses the Claude verdict vocabulary on this branch: `ship | ship-with-fixes` → D.3; `needs-rework` → D.4. Re-review is binding — no further retry, no D.5 escalation.

#### D.3 — Commit

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" commit-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <files_changed> --title "<task.title>" \
  --diff-summary "<implementer.diff_summary>" \
  --reviewer <codex|claude|none> \
  --reviewer-verdict "<verdict>" \
  --reviewer-minor-findings '<json array>' \
  [--disagreement-tag] [--remediation-tag] \
  [--narrow-remediation-tag --dismissed-finding-ids I,J,K] --json
```

`<abs>` is the current task's child file — resolve per §Per-task `<plan-file>` resolution. `commit-task`'s `Plan:` commit trailer is derived from the `--plan-file` basename, so the resolution automatically attributes each commit to the correct child.

`--remediation-tag` appends a `[remediation]` line to the commit body; set it only when the commit follows a successful D.2a.5 retry. `--narrow-remediation-tag` (with a non-empty `--dismissed-finding-ids` list) appends `[narrow-remediation]` + `[disagreement: I,J,K]` on adjacent lines; set it only when the commit follows a successful D.2a.6 retry. `--d4-rescue-tag` (TASK-005) appends a bare `[d4-rescue]` line; set it only when the commit follows a successful Phase D.4 single-shot rescue. Argparse enforces six constraints: (a) `--narrow-remediation-tag` XOR `--remediation-tag` XOR `--d4-rescue-tag` (one mutually-exclusive group), (b) `--dismissed-finding-ids` XOR `--disagreement-tag`, (c) `--dismissed-finding-ids` requires `--narrow-remediation-tag`, (d) `--narrow-remediation-tag` requires non-empty `--dismissed-finding-ids`, (e) `--d4-rescue-tag` XOR `--disagreement-tag` (D.4 rescue does not invoke D.5), (f) `--d4-rescue-tag` XOR `--dismissed-finding-ids` (D.4 rescue does not carry dismissed findings).

This: (1) guard check for unexpected staged overlap, (2) plan-status flip to `done`, (3) `git commit --only <files> <plan-file> -m "feat(TASK-NNN): <title>\n\n<diff summary>\n\nPlan: <basename>"`, (4) SHA capture, (5) run-log `commit_done` append.

**After a D.2a disagreement (Codex `needs-rework` → D.5 `ship` | `ship-with-fixes`):** pass `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` (NOT `--reviewer codex --reviewer-verdict needs-rework` — that payload is rejected with `uncommittable-reviewer-verdict` because the D.5 third-opinion verdict is binding, not Codex's). Record Codex's original findings verbatim in `--reviewer-minor-findings`; set each dismissed finding's optional `disposition: "dismissed"` (with an optional `disposition_reason`) so the commit preserves the adjudication trail. For the D.2b role-swap path the same rule applies with the roles inverted: pass `--reviewer codex --reviewer-verdict clean|minor-findings --disagreement-tag`. D.2a.5 (bounded remediation) and D.2a.6 (narrow remediation) use `--remediation-tag` or `--narrow-remediation-tag --dismissed-finding-ids ...` respectively; the binding reviewer is always the one whose verdict satisfied the commit-allowed set (`ship` / `ship-with-fixes` for `--reviewer claude`, `clean` / `minor-findings` for `--reviewer codex`).

Commit hook failure → subcommand auto-rolls back (`git reset HEAD`, restore plan text) and exits non-zero → treat as D.4 `stage=commit`.

**Post-commit `commit-safe` gate.** On a successful `commit-task`, capture the returned `commit_sha` and verify that the commit touched only TASK-NNN's declared `Files:` list (plus the always-ignore set + the plan file itself):

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
  --check commit-safe \
  --plan-file <absolute plan> \
  --task-id NNN --commit-sha <sha> --json
```

`status: fail` here means the narrow-commit seam leaked — log `commit_safe_gate_failed {task_id, commit_sha, reason}` and halt before moving to the next batch. This is the post-hoc counterpart to `commit-task`'s pre-commit guard; the two together close the execute-bundle loop.

**Lint reference:** `plan_ops.py lint-plans --plans-dir docs/plans --run-log <run_log> --git-dir . --json` cross-references every `**Status:** done` (or `partial`) task against the run log and git history. A `done`/`partial` marker without a matching `commit_done` event AND `feat(TASK-NNN)` commit flags the task as a hand-edit. Run manually during review or before shipping a plan; the PR-gate wiring is a follow-up (TASK-020C).

**Opt-in V-check gate (TASK-020B).** Plans may declare `acceptance_v_check: <shell command>` in YAML frontmatter (delimited by `---` lines at the very top of the file). When present, `commit-task` runs the command with `cwd=<repo-root>` immediately before the git commit, bounded by `--v-check-timeout SECONDS` (default 300). On success a `v_check_passed` run-log event is appended and the commit proceeds. On non-zero exit, timeout, or subprocess error `commit-task` halts with an `errors[*].code ∈ {"acceptance-v-check-failed", "v-check-timeout", "v-check-subprocess-error"}` envelope (including `stdout_tail` and `stderr_tail`, last 2048 bytes each) — no plan mutation, no git commit, no `commit_done` event. Plans without frontmatter (or without the key) behave identically to pre-TASK-020B. Example:

```yaml
---
acceptance_v_check: venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestMyTask
---
# TASK-NNN — ...
```

Shell-injection surface: `shell=True` is intentional — plans must not be edited by untrusted parties without review.

#### D.4 — Try rescue, then pause (TASK-005)

D.4 is **no longer a destructive seam**. The reviewer's `needs-rework` (or other halt-worthy verdict) does NOT immediately call `fail-task`; the orchestrator dispatches a **single-shot terminal rescue** first. Rescue-success commits via D.3 with `--d4-rescue-tag`; any non-success outcome of the rescue path falls into the awaiting-user pause and the user's next turn decides disposition.

Sequence:

1. **Log `d4_rescue_start`** with `{task_id, reviewer_findings_count}`. The reviewer findings are forwarded verbatim to the rescue dispatch as `rescue_findings[]` (a key distinct from D.2a.6's `load_bearing_findings[]` so the audit log can tell which retry path fired). `dismissed_findings: []` is passed as a literal empty list — D.4 rescue treats every reviewer finding as load-bearing and does NOT consume D.5 adjudication.

   ```bash
   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event d4_rescue_start \
     --fields-json '{"run_id":"<id>","task_id":"NNN","reviewer_findings_count":<N>}' --json
   ```

2. **Dispatch `plan-remediator`** (Agent, `subagent_type: "plan-remediator"`, `model: "opus"`) using the **Phase D.4-rescue** template from `dispatch-templates.md`. Inputs: `rescue_findings[]` (verbatim reviewer findings), `dismissed_findings: []` (literal empty list — matches the agent's empty-marker contract for the `**Dismissed findings noted:**` section), the task block, the reviewer source, and analyst annotations. The remediator's touch-only-these-lines scope rule applies.

3. **Classify the rescue outcome.** The dispatch is **strictly single-shot**: `outcome != success` → log `d4_rescue_done {task_id, outcome}` and proceed to step 6 (awaiting-user pause). Do NOT recurse, do NOT dispatch a second rescue.

4. **On rescue success, re-run D.1** (the original reviewer — Codex for Claude-implemented work, the `code-reviewer` Agent on the `claude_only=true` branch). The re-review is **binding** — no further retry regardless of verdict.

5. **Route the re-review.** `clean | minor-findings` (or `ship | ship-with-fixes`) → log `d4_rescue_done {task_id, outcome:"success", post_review_verdict:"<v>"}`, then D.3 commit with `--d4-rescue-tag` (no companion flag — bare `--d4-rescue-tag` is the canonical rescue-success signature). `needs-rework` on the re-review → log `d4_rescue_done {task_id, outcome:"post_review_failed", post_review_verdict:"needs-rework"}` and proceed to step 6.

6. **Awaiting-user pause** (rescue dispatch failed OR post-rescue re-review failed): (See **Awaiting-user pause** subroutine — same control flow.)
   - `stage:"post_d4_rescue_failed"`, payload includes `reviewer_findings[]`, `rescue_attempt_outcome`, and `rescue_diagnostics` per the call-site table below. The pre-rescue working tree edits are preserved verbatim — do NOT `git restore`, do NOT call `fail-task`.
   - `--ending-sha <sha>` MUST be `git rev-parse HEAD` at pause time. Log currently-dirty file paths in the `awaiting_user` event's `dirty_files` field.

   ```bash
   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event awaiting_user \
     --fields-json '{"task_id":"NNN","stage":"post_d4_rescue_failed","reviewer_findings":[...],"rescue_attempt_outcome":"<outcome>","rescue_diagnostics":"...","dirty_files":[...]}' --json

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" finalize-execution-log \
     --run-id <id> --starting-sha <sha> --ending-sha "$(git rev-parse HEAD)" \
     --outcome paused --rows-json '[...]' --json

   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event run_end \
     --fields-json '{"run_id":"<id>","outcome":"paused","done":N,"failed":M,"paused_on_task":"NNN"}' --json
   ```

   The user's next conversation turn decides disposition (same three options as D.2a.5/D.2a.6: "revert" / "keep as-is" / "hand-fix"). A user-instructed revert at this point invokes `fail-task --authorization-source phase-d4-rescue-failed` — that is the *only* sanctioned route from this pause to a destructive action.

**Hard rule:** D.4 is single-shot. The orchestrator MUST NOT dispatch a second rescue, MUST NOT call `fail-task` automatically on rescue failure, MUST NOT `git restore` the pre-rescue working tree. Recursion on the rescue branch would re-introduce the infinite-loop concern the single-shot rule closes. See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn.

After a paused run is resumed by user instruction (either a `commit-task --d4-rescue-tag` to keep the rescued work, or a `fail-task --authorization-source user-instruction` to discard, or hand-edit + re-review), the orchestrator cascades `blocked` onto transitive dependents only on the failure path:

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" block-dependents \
  --schedule-file <path> --plan-file <abs> --failed NNN --run-id <id> --json
```

Same atomic shape as Phase C, and the same per-task `<plan-file>` resolution: `<abs>` is the failed task's child file; `block-dependents` internally routes each dependent's mutation to its own child per the schedule's `tasks[].plan_file`. The `block-dependents` call is skipped on the rescue-success commit path — a successfully rescued task's dependents are not blocked.

#### Awaiting-user pause (shared control flow)

Shared subroutine factored out of D.2a.5, D.2a.6, and (per future tasks) Phase C / Phase D.4 / D.2a binding-mode / `reconcile_batch` out-of-scope. **Control flow only** — per-stage payload contracts are intentionally distinct and stay declared at each call site. The subroutine spec lists the call-site contracts as a table:

| Call site | `stage` value | Required payload fields (additional to `dirty_files`) |
|---|---|---|
| D.2a.5 second-review fail | `post_remediation_review` | `codex_findings[]`, `d5_summary` |
| D.2a.5 retry-implement fail | `post_remediation_implement` | `retry_outcome`, `diagnostics`, `reversion_guidance` |
| D.2a.6 second-review fail | `post_narrow_remediation_review` | `codex_findings[]`, `d5_summary`, `dismissed_finding_indices[]` |
| D.2a.6 retry-implement fail | `post_narrow_remediation_implement` | `retry_outcome`, `diagnostics`, `reversion_guidance` |
| Phase C (non-empty diff) | `post_implement_failure` | `implementer_outcome`, `diagnostics`, `reversion_guidance`, `nonempty_diff_files[]` |
| Phase D.4 after rescue | `post_d4_rescue_failed` | `reviewer_findings[]`, `rescue_attempt_outcome`, `rescue_diagnostics` |
| Phase D.4 commit-seam | `post_commit_seam_failure` | `commit_seam_reason`, `seam_diagnostics` |
| D.2a binding-mode block | `post_binding_block` | `codex_findings[]`, `binding_flag` |
| `reconcile_batch` out-of-scope | `post_reconcile_out_of_scope` | `out_of_scope_tracked[]`, `out_of_scope_untracked[]`, `task_id`, `wrapper_envelope_summary` |

The subroutine itself does the same five things at every call site:

1. `log-event awaiting_user --fields-json '{"task_id":"NNN","stage":"<see table>",...,"dirty_files":[...]}'` (per-site payload merged in).
2. `finalize-execution-log --outcome paused --ending-sha "$(git rev-parse HEAD)"`.
3. `log-event run_end --fields-json '{"outcome":"paused","paused_on_task":"NNN",...}'`.
4. Skip End-of-run housekeeping; print failure envelope; release the run-lock.
5. **Never** call `fail-task`; **never** `git restore`; **never** mutate plan-status to `failed`.

See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn.

### Phase E — Next batch

Release this task's file locks. Loop to Phase A.

## End of run

1. `plan_ops.py update-plan-header --status <complete|partial>` (complete iff `failed == 0`; else partial). Partition completed + failed tasks by their `plan_file` value, and call `update-plan-header` once per distinct child basename: `--plan-file <plans_dir>/<child-basename> --status <per-child status>`. Per-child status is `complete` iff every task that lived in that child passed, else `partial`. Do NOT synthesize a run-level aggregate header — design boundary (documented in §Directory-mode design boundaries). Children with no tasks in completed+failed are untouched.
2. `plan_ops.py finalize-execution-log --run-id <id> --starting-sha <sha> --ending-sha <sha> --outcome <success|partial|failed|paused> --rows-json '[...]'` — build the §5 table. `--rows-json` row schema: each row is an object with exactly these six required string keys — `task`, `agent`, `reviewer`, `verdict`, `commit`, `notes` (no extras; values must all be strings). Verdict cells should include any `[disagreement]` / `[remediation]` / `[narrow-remediation]` markers in prose. Missing or unknown keys exit 1 with the full allowed-field list in the error message. Use `--outcome paused` when exiting via the D.2a.5 OR D.2a.6 awaiting-user path; `success`/`partial`/`failed` otherwise per the usual done/failed accounting. Partition rows by `plan_file` (same basenames as step 1) and call `finalize-execution-log --plan-file <plans_dir>/<child-basename> --rows-json '<child-scoped rows>'` once per distinct child. No run-level aggregate table — design boundary.
3. Log `run_end` event (counts `{done, failed}` + disagreement_count + minor_findings_total; include `outcome=paused` when halting via D.2a.5 or D.2a.6). Include `plan_file: "<dir-basename>"` in the event's `fields` (same value as the `run_start` pair).
4. Print summary: counts `{done, failed}`, failures with reasons, disagreement-tagged commits, per-task minor-findings digest (from `review_notes`), `git log --oneline <starting_sha>..HEAD` hint.
   - `TaskList mirror state: {synced | drifted}`. Compare run-log `commit_done` / `task_failed` counts against the `TaskList` completed-task counts; any mismatch is `drifted` and includes the count delta.

   **`claude_only=true` loud banner contract (TASK-003).** When the run had `claude_only=true` for any reason (operator opt-in `--claude-only` OR preflight `codex_available=false` — the OR-binding from §Pre-flight (Phase 0)), the final run summary MUST carry a loud banner: *"Claude-only mode: Phase 1.5 plan review and Phase D cross-review ran via the `code-reviewer`/`plan-reviewer` Agents (Sonnet); no Codex shell-out fired this run."* Include the discriminator `(--claude-only flag)` or `(codex_available=false)` so the operator can tell which input flipped the binding. The banner is parallel to the `--skip-plan-review` and `--skip-cross-review` summary banners and complements the `run_start.fields.claude_only: <bool>` field — both surface the same routing decision in different audit channels.
5. Housekeeping commit (skip if `done == 0 AND failed == 0`):
   ```bash
   git add <plan-file> <run_log>
   git commit -m "chore(implement-plan): run <run_id> bookkeeping"
   ```
6. **Certify the execute bundle.** Before releasing the lock and after the housekeeping commit (if any), run the full phase-gate bundle in execute mode so the run's pass/fail determination is recorded in the run log and visible to downstream tooling:

   ```bash
   $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
     --certify --mode execute \
     --plan-file <absolute plan> \
     --schedule-file <schedule_file> \
     --run-id <id> --json
   ```

   The bundle re-checks `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, and re-verifies `commit-safe` against every `commit_done` event for this run. A run with zero commits reports `commit-safe: not_applicable` — that is not a failure. `certified: false` in the output means a gate failed; emit `certify_failed {run_id, gates: {...}}` via `log-event` and surface the failing gate name in the final summary, but do NOT retroactively reopen already-committed tasks. The certify step is a report, not a retry trigger.

7. `plan_ops.py release-lock` (finally-style; runs on early halt too).

Do NOT auto-push. Do NOT auto-PR.

## Rules

- **Completed-Work Preservation Principle.** No agent, subagent, wrapper, or orchestrator step may silently revert, `git restore`, `git reset --hard`, delete, `unlink`, or otherwise discard implementer work product — defined as **any non-empty diff against the run's `starting_sha` present in the working tree (tracked or untracked) inside the task's declared `Files:` set OR the wrapper-observed `out_of_scope_*` sets** — without first (a) surfacing the situation to the user via an `awaiting_user` event + paused `run_end` and (b) receiving an explicit user instruction in the next conversation turn. The default response to a downstream obstacle on such work is **halt-with-pause**, not auto-`fail-task`. When a follow-up fix is required, the orchestrator MUST first attempt an in-place patch via `plan-remediator` (touch-only) before considering reversion, even when the required fix appears "out of scope" of the original task — preserving implementer effort takes precedence over scope tidiness. Reversion is allowed only on (i) explicit user instruction in the next turn, (ii) commit-time guard failures whose rollback is bounded to staging metadata (`git reset HEAD` + plan/roster restore, see §G7 below), or (iii) implementer-failure paths where the working tree contains **no** non-empty diff (nothing to preserve). Auto-revert in any other path is a protocol violation.
  - **Specific instance: D.2a.5 / D.2a.6 halt path —** A second `needs-rework` after a D.2a.5 remediation retry (or a D.2a.6 narrow-remediation retry) triggers `log-event type=awaiting_user` + `finalize-execution-log --outcome paused` and returns control to the user with pending edits left in the working tree. Calling `fail-task` on the paused run is allowed ONLY when the user's next conversation turn explicitly instructs it. Silent auto-revert on the post-remediation `needs-rework` path is a protocol violation.
- **Never edit code files.** Orchestrator only touches plan files, `_run_log.jsonl`, `_run_lock.json`, and git staging. Implementer subagents / Codex wrapper own code changes.
- **When `claude_only=true`, the orchestrator MUST NOT invoke `plan_codex_dispatch.py` for ANY subcommand (`plan-review`, `review`, `implement`). Codex shell-out under `claude_only` is a protocol violation.** The `claude_only` boolean is bound at Phase 0 preflight from `--claude-only OR (codex_available == false)`; see §Pre-flight (Phase 0) and §Parse arguments mutex prose.
- **Under `claude_only=true`, the D.2a third-opinion ladder collapses.** D.5 escalation, D.2a.5 bounded remediation, and D.2a.6 narrow-remediation are unreachable — there is no Codex verdict to adjudicate. A `code-reviewer` `needs-rework` verdict on a Claude-implemented task under `claude_only=true` goes straight to D.4 fail-task. The D.2b role-swap retry path (Codex-implemented + Claude-reviewer `needs-rework`) is also structurally unreachable under `claude_only=true` because Phase 1's `--claude-only` / `codex_available=false` rewrite forces every task's implementer side to Claude; §D.2b documents the defensive re-review contract were the path ever reachable. See §Phase D.2a.
- **Never commit a reviewer-flagged `needs-rework`.** Only clean / minor-findings / ship / ship-with-fixes commit automatically.
- **Never `git add -A` or `git add .`.** Stage specific files only — `commit-task` already uses `--only`.
- **Never retry a failed task inside the same run** beyond the one D.2b role-swap, the one D.2a.5 bounded remediation retry, the one D.2a.6 narrow-remediation retry, and the one Codex→Claude fallback. Terminal failures stay isolated — peers continue independently.
- **Re-source-verdict is binding after plan-review triage + plan-author.** When a Phase 1-triage or Phase 1.5.5 triage returns `partial-agreement` or `needs-rework`, the subsequent `plan-author` → re-source-verdict sequence runs EXACTLY ONCE and that second source verdict is binding. On the analyst source (`source="plan-analyst"`), the re-source-verdict is a re-run of Phase 1 (`build-tasks → classifier → write-schedule`); a second round of non-empty `build-tasks warnings[]` halts per `run_end reason=plan_analyst_failed` (equivalent to the retired "second `needs-enrichment`"). On the Codex source (`source="codex-plan-review"`), the re-source-verdict is the re-dispatched Codex `plan-review`; a second `needs-replan` halts per `run_end reason=plan_review_failed`. NO second plan-review triage is dispatched on either path — mirrors the D.5 "re-review is binding" rule at task level.
- **Plan-file body edits are narrowly allowed.** Allowed: (a) `**Status:**` bullet mutations; (b) append-only mutation of the tail execution-log section; (c) pre-dispatch format-only corrections needed to satisfy schema / phase-gate predicates (e.g., promoting a bulleted `- **Description:**` to the required prose-header `**Description:**`). Task semantics — prose, acceptance criteria, Files, Test command, Implementation notes, Reversion guidance, Dependencies, Scope boundaries — MUST NOT be altered; the orchestrator is not a plan author. Any format correction under (c) MUST be mentioned in the execution-log tail so the edit is auditable. The run log remains append-only.
- **One commit per task** plus at most one `chore:` housekeeping commit per run. Narrow `git commit --only` in Phase D.3 is mandatory.
- **Never auto-push, never auto-PR.**
- **`$PYTHON`** for all Python invocations.
- **Every run-log append is verified** via `plan_ops.py log-event`'s tail re-verify (or via `commit-task` / `fail-task` which fsync + re-read internally).
- **Never write inline Python for plan ops.** Use `plan_ops.py`. Inline `python3 -c` scripts are a protocol violation.
- **Dispatch prompts must be self-contained.** Subagents do not see this conversation. Embed the full task block verbatim.

### Pre-invocation checklist

Four process rules that would have prevented every doc-fixable error in run `20260417T214309`. Run these before re-invoking `plan_ops.py` or dispatching a subagent — especially after a context compaction.

1. **Run `--help` on any `plan_ops.py` subcommand before re-invoking it after a context compaction.** The cache is cheap; flag-name drift isn't. **Why:** prevents error 5 (`--payload` vs `--fields-json` flag amnesia on `log-event`).
2. **Grep `ALLOWED_*` constants in `plan_ops.py` before any `log-event` or `commit-task` call that uses enum-valued flags** (event names, reviewer verdicts, severity values, outcome values). **Why:** prevents error 2 (invented `log-event type=d5_review_done` outside `ALLOWED_LOG_EVENTS`).
3. **Read the relevant `_validate_*` function in `plan_ops.py` before piping into a `parse-*` subcommand.** The validator checks the envelope shape, not what downstream code consumes. **Why:** prevents error 1 (fed bare `parsed` object to `parse-plan-review-report` instead of the full `{subcommand,outcome,parsed}` envelope).
4. **Never Agent-dispatch a subagent file created during the current run.** The Claude Code Agent registry **snapshots at session start**; newly-created subagent files become available in the next fresh session, not the one that created them. If you must retry within the current session, fall back to the closest existing subagent with an inlined prompt matching the new subagent's contract. **Why:** prevents error 4 (Agent-registry miss on the same-session-created `plan-remediator`).

## Cleanup policy (wrapper-enforced, for reference)

The Codex dispatch wrapper operates **delta-bounded cleanup** against a pre-dispatch baseline. It never runs `git checkout -- .` or `git clean -fd`, so disjoint sibling work is not destroyed. Protected paths are never touched by cleanup; they land in `extra.protected_skipped_tracked` / `extra.protected_skipped_untracked` for observability.

- **Protected exact paths:** `_run_lock.json`, `.claude`, `.codex`
- **Protected prefixes:** `<plan_dir>/_run_log.jsonl`, `<plan_dir>/_run_lock.json`, `.claude/`, `.codex/`
- **Delta invariant:** cleanup only touches `(allowed_files ∪ new-delta-violations) − protected`. Files present in the baseline are never deleted or restored.
- **Scope misreport:** if Codex's `files_changed` disagrees with the post-dispatch delta, the wrapper emits `outcome="failure"` with `extra.reason="scope_misreport"` and `extra.test_result.result="not_run"`; the test command is skipped but delta-only restore still runs.
- **Review-path:** keeps "log but succeed" semantics by explicit design; a post-dispatch sandbox escape surfaces in `extra.sandbox_escape_detected` without changing outcome.

## Command reference

For a one-page lifecycle-ordered CLI summary (especially after a context compaction), Read `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md` first — it rehydrates the full subcommand vocabulary in a single tool call. This appendix is the deep reference: full JSON payload shapes, validator pointers, and worked examples for the non-obvious subcommands. Examples were verified against `$PYTHON plan_ops.py <sub> --help` at audit anchor `1456687`. Substitute placeholders (`<plan-file>`, `<task-id>`, `<run-id>`, `<sha>`, file paths) only — flag names and enum values are literal.

### `parse-plan-review-report`

Validates the **full envelope** the Codex plan-review wrapper emits, not the inner `parsed` object. `_validate_plan_review_envelope` requires the top-level keys `{subcommand, outcome, parsed}` — feeding a bare `parsed` payload exits 1 with `unknown-top-level-key` errors. This was error 1 in run `20260417T214309`.

Correct shape:

```bash
echo '{
  "subcommand": "plan-review",
  "outcome": "success",
  "parsed": {
    "plan_file": "<plan-file>",
    "verdict": "approved-with-notes",
    "findings": [],
    "schedule_ok": true,
    "summary": "..."
  }
}' | $PYTHON plan_ops.py parse-plan-review-report --stdin --json
```

Incorrect (rejected):

```bash
# Bare `parsed` — fails with unknown-top-level-key on every key.
echo '{"plan_file":"...","verdict":"approved","findings":[]}' \
  | $PYTHON plan_ops.py parse-plan-review-report --stdin
```

If unsure about the envelope, grep `_validate_plan_review_envelope` in `plan_ops.py` for the live shape.

### `parse-d5-adjudication`

Stdin payload shape: `{verdict, summary, load_bearing, dismissed}`. `--codex-findings-count N` is **mandatory** — partial-agreement indices are validated against `range(0, N)`. One example per verdict:

```bash
# ship — load_bearing/dismissed omitted (or empty arrays).
echo '{"verdict":"ship","summary":"D.5 sides with implementer; Codex findings dismissed."}' \
  | $PYTHON plan_ops.py parse-d5-adjudication --stdin --codex-findings-count 3 --json

# ship-with-fixes — Codex findings stand; commit with [disagreement] tag.
echo '{"verdict":"ship-with-fixes","summary":"All Codex findings load-bearing; ship + fix later."}' \
  | $PYTHON plan_ops.py parse-d5-adjudication --stdin --codex-findings-count 3 --json

# needs-rework — D.5 escalates to D.2a.5 remediation retry.
echo '{"verdict":"needs-rework","summary":"D.5 confirms blockers; remediation needed."}' \
  | $PYTHON plan_ops.py parse-d5-adjudication --stdin --codex-findings-count 3 --json

# partial-agreement — disjoint, in-range index splits required; both buckets non-empty.
echo '{"verdict":"partial-agreement","summary":"Findings 0,2 load-bearing; finding 1 dismissed.","load_bearing":[0,2],"dismissed":[1]}' \
  | $PYTHON plan_ops.py parse-d5-adjudication --stdin --codex-findings-count 3 --json
```

Empty-bucket or overlapping-bucket payloads exit with `partial-agreement-invalid-split`; out-of-range indices exit with `partial-agreement-unknown-index`.

### `log-event`

Use `--fields-json '{...}'`, **not `--payload`** (error 5 in run `20260417T214309` was post-compaction flag amnesia). The event name MUST be in `ALLOWED_LOG_EVENTS`; inventing a name (e.g., `d5_review_done`) exits 1 (error 2 in run `20260417T214309`).

`ALLOWED_LOG_EVENTS` (alphabetized — grep this block after compaction; matches `plan_ops.py` exactly):

```
analyst_done
analyst_triage_skipped
awaiting_user
batch_start
commit_done
d4_rescue_done
d4_rescue_start
decompose_auto_promote
disagreement
fallback_used
failed
implement_done
implement_start
narrow_remediation_done
narrow_remediation_start
plan_author_done
plan_author_start
plan_review_done
plan_review_skipped
plan_review_start
plan_review_triage_done
plan_review_triage_start
remediation_start
review_done
review_skipped
review_start
run_end
run_start
schedule_written
v_check_failed
v_check_passed
```

One example per class:

```bash
# Lifecycle (run-bracket).
$PYTHON plan_ops.py log-event --event run_start \
  --fields-json '{"run_id":"<run-id>","plan_file":"<dir-basename>","starting_sha":"<sha>"}'

# Phase (per-task / per-batch checkpoint).
$PYTHON plan_ops.py log-event --event implement_start \
  --fields-json '{"run_id":"<run-id>","task_id":"<task-id>","plan_file":"<child-basename>","agent":"claude"}'

# Outcome (commit / fail / disagreement).
$PYTHON plan_ops.py log-event --event disagreement \
  --fields-json '{"run_id":"<run-id>","task_id":"<task-id>","reviewer":"codex","verdict":"needs-rework","d5_verdict":"ship-with-fixes"}'
```

**`review_skipped.reason` enum.** The `review_skipped` event's `reason` field uses a fixed vocabulary:

- `flag` — operator passed `--skip-cross-review` (existing).
- `codex_review_timeout` — wrapper `subcommand=review` returned `outcome=timeout` (§D.1 routing).
- `codex_review_parse_error` — wrapper `subcommand=review` returned `outcome=parse_error` (§D.1 routing).
- `codex_review_failure` — wrapper `subcommand=review` returned `outcome=failure` (§D.1 routing).

### `commit-task`

`--reviewer-minor-findings` is a JSON **array** of finding objects with required keys `{severity ∈ {critical, important, minor}, confidence ∈ {high, medium, low}, file, line, issue, suggested_fix}`. The severity enum is fixed; `_validate_reviewer_finding` exits 1 on any other value.

(a) Standard `minor-findings` commit (Codex reviewer, ship despite advisory findings):

```bash
$PYTHON plan_ops.py commit-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --files "a.py,b.py" --title "feat(TASK-NNN): <one-line>" --diff-summary "<one-line>" \
  --reviewer codex --reviewer-verdict minor-findings \
  --reviewer-minor-findings '[{"severity":"minor","confidence":"high","file":"a.py","line":42,"issue":"Magic number","suggested_fix":"Extract constant"}]' \
  --dry-run
```

(b) **D.5-disagreement commit (post-§8.4 third opinion).** D.5 is the third-opinion tie-breaker; **D.5's verdict (`ship | ship-with-fixes | partial-agreement`) is passed as `--reviewer-verdict`; Codex's `needs-rework` is NOT** — that was the confusion behind error 3 in run `20260417T214309`. `--disagreement-tag` records that Codex disagreed; the verdict that ships is D.5's:

```bash
$PYTHON plan_ops.py commit-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --files "a.py" --title "feat(TASK-NNN): <one-line>" --diff-summary "<one-line>" \
  --reviewer claude --reviewer-verdict ship-with-fixes \
  --disagreement-tag --dry-run
```

The `[disagreement]` bare trailer encodes "Codex flagged needs-rework, D.5 overrode to ship-with-fixes." Do NOT pass `--reviewer-verdict needs-rework` — the commit guard rejects it because needs-rework never auto-commits.

(c) **D.2a.5 remediation retry** — full rework, single retry; `[remediation]` trailer:

```bash
$PYTHON plan_ops.py commit-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --files "a.py" --title "feat(TASK-NNN): <one-line>" --diff-summary "<one-line>" \
  --reviewer codex --reviewer-verdict clean \
  --remediation-tag --dry-run
```

(d) **D.2a.6 narrow remediation** — dismissed indices recorded as `[disagreement: I,J,K]` adjacent to `[narrow-remediation]`:

```bash
$PYTHON plan_ops.py commit-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --files "a.py" --title "feat(TASK-NNN): <one-line>" --diff-summary "<one-line>" \
  --reviewer codex --reviewer-verdict minor-findings \
  --reviewer-minor-findings '[]' \
  --narrow-remediation-tag --dismissed-finding-ids "0,2" --dry-run
```

(e) **User-override / post-pause keep-as-is** — no reviewer signal:

```bash
$PYTHON plan_ops.py commit-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --files "a.py" --title "feat(TASK-NNN): <one-line>" --diff-summary "<one-line>" \
  --reviewer none --reviewer-verdict "" \
  --remediation-tag --dry-run
```

### `finalize-execution-log`

`--rows-json` is a JSON array; each row carries `{task, agent, reviewer, verdict, commit, notes}` (the keys in `ALLOWED_ROW_FIELDS`). `--outcome` is optional (omitted preserves the legacy unlabelled header) and accepts `{success, partial, failed, paused}`.

`--outcome` semantics:

| value | meaning |
|---|---|
| `success` | All tasks committed cleanly (or with reviewer-approved minor-findings / ship-with-fixes). |
| `partial` | At least one task succeeded, at least one failed/blocked. The §5 table records the mix. |
| `failed` | No tasks succeeded — preflight or batch 1 halted before any commit landed. |
| `paused` | D.2a.5 awaiting-user halt — second `needs-rework` after remediation retry. Pending edits remain in the working tree; the user's next conversation turn decides disposition. |

```bash
$PYTHON plan_ops.py finalize-execution-log --plan-file <plan-file> --run-id <run-id> \
  --starting-sha <sha-start> --ending-sha <sha-end> \
  --rows-json '[
    {"task":"001","agent":"claude","reviewer":"codex","verdict":"clean","commit":"<sha>","notes":""},
    {"task":"002","agent":"codex","reviewer":"claude","verdict":"ship-with-fixes","commit":"<sha>","notes":"[disagreement]"}
  ]' \
  --outcome success
```

