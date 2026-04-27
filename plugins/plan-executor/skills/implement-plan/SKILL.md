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

The always-ignore set is the shared `COMMIT_ALWAYS_IGNORE` constant in `_plan_paths.py` (run-log / run-lock / schedule sidecar / `00_INDEX.json`); `commit-task` staging and the post-commit `_gate_commit_safe` predicate key on the same constant. Executor scripts (`plan_ops.py`) are protected from delta-cleanup but must still be declared in `Files:` to commit against — they are NOT members of `COMMIT_ALWAYS_IGNORE`.

**Dry-run pass condition:** `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe` all `pass`; `commit-safe` is `not_applicable` (no commits in dry-run).

**Execute pass condition:** all six gates `pass`, with `commit-safe` re-verified per `commit_done` event via `plan_ops.py gates --certify --mode execute --run-id <id>`.

## Bash command idioms

Standard commands used by this skill:

- `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" <subcommand> [--json]` — all plan parsing, schedule evaluation, batch selection, narrow commit, failure handling, status transitions, and run-log append verification.
- `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement|review ...` — Codex-tier implementer and reviewer. Emits a single JSON envelope on stdout.
- `date -u +%Y%m%dT%H%M%S` — run_id fallback (preflight emits one authoritatively).

Never write inline Python for plan operations. Never `git stash` inside this skill — the wrapper restores Codex-side independently; the orchestrator restores Claude-side via `plan_ops.py fail-task` (which uses `git restore`).

## Dispatch error handling (Claude wrapper)

Every Claude-wrapper dispatch (`plan_claude_dispatch.py run` for `plan-analyst` / `plan-implementer` / `plan-remediator`) is invoke → extract → route. Wrap with `claude_dispatch_start` before, `claude_dispatch_done` on `status==ok`, `claude_dispatch_failed` on any non-`ok` status. Pipe the envelope through `$PYTHON plan_ops.py claude-envelope-extract --stdin --agent <plan-analyst|plan-implementer|plan-remediator> --json` to normalize to `{status, outcome, result, scope_violation, scope_misreport, error}`. **Universal invariant:** `status != ok` ⟹ `commit-task` is forbidden for this task. Status routing: `scope_violation` (with `wrapper_autoclean_blocked: true`) → Awaiting-user pause (`stage:"post_<site>_implement_wrapper_blocked"`); `scope_violation` (autoclean executed) → Awaiting-user pause (`stage:"post_<site>_implement"`); `cleanup_failure` → halt `run_end reason=wrapper_cleanup_failed`; every other non-`ok` value (`schema_invalid | timeout | denied | backend_error | budget_exhausted | depth_exceeded | manifest_invalid | input_invalid`) collapses to `outcome="malformed"` (analyst → halt `analyst_invalid`; implementer/remediator → Phase C). Full status mapping + per-site stage labels in `docs/plans/SKILL_bash_dispatch_migration/run-log-events.md`.

## plan_ops.py CLI reference

One-line purpose per subcommand. All accept `--json`. **Run `$PYTHON plan_ops.py <subcommand> --help` for full flag surfaces, validators, and JSON envelope shapes — `--help` is the source of truth.**

| Command | Purpose |
|---|---|
| `preflight` | Dirty-tree + codex probe + starting_sha + run_id + base-branch check. |
| `check-plan-deps` | Mandatory cross-plan dependency gate (Phase 0). |
| `decompose-plan` | Auto-promote a single-file plan to a directory of per-task children. |
| `filter-schedule` | Trim a schedule to a `--task-ids` subset preserving topo + transitive deps. |
| `index-closure` | Compute the dependency closure of a chunk in `00_INDEX.json`. |
| `list-global-lock-paths` | Emit the canonical `<run_log>` / `<run_lock>` / `<plan_dir>` paths. |
| `order-triage-findings` | Order plan-review-triage findings by `blocking` then `severity`. |
| `parse-plan-review-triage-report` | Validate Phase 1-triage / Phase 1.5.5 triage output envelope. |
| `resolve-read-targets` | Resolve a task's `Read targets:` / `Symbol targets:` into pre-read excerpts. |
| `parse-schedule` | Validate analyst JSON shape. |
| `compute-schedule` | Recompute file-disjoint + topo-respecting batches (standalone). |
| `write-schedule` | Atomically persist analyst schedule JSON. |
| `batch-next` | Pick next file-disjoint batch from `.schedule.json` state. |
| `review-route` | Map a (verdict, claude_only, flags) tuple to a routing action. |
| `reconcile-batch` | Batch-join out-of-scope reconciliation across wrapper envelopes. |
| `parse-implementer-report` | Extract outcome/files_changed/diagnostics from implementer markdown. |
| `parse-plan-review-report` | Validate Phase 1.5 Codex plan-review envelope. |
| `claude-envelope-extract` | Normalize a v3 Claude wrapper envelope to `{status, outcome, result, scope_violation, scope_misreport, error}`. |
| `parse-d5-adjudication` | Validate Phase D.5 third-opinion verdict. |
| `commit-task` | Phase D.3: guard, status→done, narrow commit, run-log append. |
| `fail-task` | Phase C / D.4 reversion with explicit authorization-source. |
| `block-dependents` | Cascade `blocked` onto transitive dependents. |
| `update-plan-header` | Mutate plan-file top-level `**Status:**`. |
| `finalize-execution-log` | Append §5 execution-log table to plan. |
| `log-event` | Append JSONL run-log event with tail re-verify. |
| `normalize-task-id` | Canonicalize task id to `^\d{3}[A-Z]?$`. |
| `acquire-lock` / `release-lock` | Per-plan-file run-lock. |
| `path-info` | Emit `plan_dir` + derived `run_log` / `run_lock` / `schedule_glob`. |
| `gates` | Phase-gate predicates (`schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, `commit-safe`). |
| `audit` | Standing self-audit against `CANONICAL_CONTRACT`. |
| `auto-validate-divergence` | Phase C sandbox-divergence escape-hatch. |
| `lint-plans` | Cross-reference plan `**Status:**` markers against run-log + git history. |
| `run-summary` | Emit per-section run summary (e.g., `sandbox-divergences`). |
| `build-tasks` | Build the fat `tasks[]` manifest from per-child plan files. |

Footer: every flag, enum, and JSON shape is documented in `$PYTHON plan_ops.py <sub> --help`. Re-grep `--help` after any context compaction; do not rely on hand-curated examples in this file.

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

The subcommand reads whole-plan `## TASK-NNN:` markdown and writes a sibling directory at `<file-parent>/<file-stem>/` with `00_INDEX.json` + one `TASK-NNN_<slug>.md` child per task (each child uses the `### TASK-NNN:` H3 heading downstream parsers expect). Success → rebind `<plan-path>` ← `produced_dir`. Malformed input → exit 1 with structured `errors[*]` (missing headers, duplicate ids, unresolvable deps, cycles) — halt without acquiring the lock. Append a run-log event (still via the bootstrap interpreter — `$PYTHON` binds after preflight):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event decompose_auto_promote \
  --fields-json '{"source_file":"<original plan-path>","produced_dir":"<produced_dir>","task_count":<n>}'
```

`preflight` runs next (see below) and pins `$PYTHON` for every subsequent `$PYTHON ...` command line in this skill.

Do NOT read the produced child files into context — `ls` of the directory is enough; downstream phases open children on demand. The produced directory is treated identically to a user-authored decomposed directory; `run_start.plan_file` reflects the directory basename. See §Input shape below for canonical bindings.

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

Returns JSON with `pass`, `starting_sha`, `run_id`, `codex_available`, `python_path`, `unattended_revert_policy`, `dirty_files{plan_doc, orchestrator_state, plan_scope_dirty, source_blocking}`, `scope_warnings[]`, `base_branch`, `current_branch`, `base_branch_match`. Halts on `source_blocking`. `plan_doc` churn allowed; `orchestrator_state` warns + proceeds; `plan_scope_dirty` is advisory unless `--strict-scope` is set.

**After preflight**, pin `$PYTHON` for the rest of the run by exporting `PYTHON=<python_path>` from the preflight JSON. Every subsequent `$PYTHON ...` command line in this skill uses the pinned value. If you need to re-dispatch from a fresh shell context, re-export from the same preflight result — do NOT re-resolve in templates.

**Pin `$UNATTENDED_REVERT_POLICY`** the same way: export `UNATTENDED_REVERT_POLICY=<unattended_revert_policy>` from the preflight JSON and use the pinned value for every pause-path consumer (Awaiting-user pause subroutine, reconcile-batch). The flag is not argparse-required: TTY stdin defaults to `pause`; non-TTY stdin without the flag refuses with `errors[*].code = "unattended-revert-policy-required"`. Values: `pause` (interactive default — surfaces pause record and waits); `fail-fast` (structured failure record + halt instead of pausing); `preserve-only` (salvage the diff to a side ref, then fail-task / halt).

If `codex_available=false`, override `tasks[].agent = "claude"` throughout Phase 1 and warn; wrapper's own "codex binary not found on PATH" branch is the backstop.

**Bind `claude_only` (single routing boolean).** `claude_only := (--claude-only is set) OR (preflight.codex_available == false)`. The canonical routing flag every downstream phase consults (Phase 1.5 plan review, Phase D cross-review); hoisted once to keep the decision stateless across phases. Flows into `run_start.fields.claude_only` for audit.

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

**Per-child `schema-valid` loop.** Run `schema-valid` once per `chunks[].file` in `<plans_dir>/00_INDEX.json`, halting on the first failure with `schema-valid failed for child <basename>: <gate.reason>`. `fixture-valid` / `execution-safe` / `review-safe` run once each (they validate the executor itself and the shipped fixture, not the user's plan).

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

Pseudo-syntax (for illustration; the actual tool-call shape is N parallel Bash invocations of `plan_claude_dispatch.py run --input <payload.json>`, one per child — the v3 wrapper replaces the in-process Agent tool dispatch as of TASK-003):

```
# Inside ONE assistant turn, the orchestrator emits N separate Bash tool-use blocks:
[
  Bash($PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input <payload_0.json>),
  Bash($PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input <payload_1.json>),
  ...
  Bash($PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input <payload_{N-1}.json>),
]
# ^ N discrete Bash tool calls, emitted together in one response. The wrapper
#   dispatches them in parallel; the orchestrator receives N v3 envelopes on stdout.
```

Each payload sets `agent="plan-analyst"`, carries the absolute child plan path under `payload.plan_path`, the orchestrator-known `trace.run_id`, and an `output_instructions.schema_path` reference to the analyst result schema (`plugins/plan-executor/scripts/schemas/claude_dispatch_output.json`'s inner `result` shape — see `dispatch-templates.md` §Phase A-single transport header for the canonical payload skeleton). Each dispatch uses the **Phase A-single** template from `dispatch-templates.md` (agent `plan-analyst`, model `sonnet` — narrower scope than the retired whole-plan opus dispatch); the per-child agent reads exactly its one child file and the wrapper returns a v3 envelope whose inner `result` is `{agent: "claude"|"codex", classification_reason: "<one-line justification>"}`.

**Extraction:** invoke `claude-envelope-extract --agent plan-analyst --json` per §Dispatch error handling (Claude wrapper). On `status==ok` the per-child classifier seam's `result` is `{agent, classification_reason}` (no `parse-schedule` round-trip); the whole-plan analyst seam's `result.schedule` flows through `parse-schedule`.

Parse each reply and merge into the in-memory `tasks[]` array: for each child reply, set `tasks[i].agent = reply.agent` and `tasks[i].classification_reason = reply.classification_reason` on the matching entry (match by `plan_file` basename — the orchestrator knows which dispatch corresponds to which child).

Malformed reply handling — a wrapper envelope whose `.status != "ok"`, OR whose `.result` does not parse as `{agent, classification_reason}`, is the analyst-site instance of the shared error-status table at §Dispatch error handling (Claude wrapper): halt with `run_end reason=analyst_invalid`; surface the offending child basename + the `error` field returned by `claude-envelope-extract`; release lock.

Apply the `codex_available=false` preflight override here too — if the preflight flag was false, rewrite every `tasks[i].agent` to `"claude"` in the merged manifest before step 3 (consistent with the retired whole-plan analyst override, which the orchestrator used to apply after parsing the whole-plan JSON).

### Step 3 — Apply filters and per-task overrides

After the merged `tasks[]` has an `agent` field on every entry (either declared in source or filled by step 2), the orchestrator synthesizes the canonical schedule shape in memory and applies any filter / agent-override flags. **No re-batch happens here**: `build-tasks` already emitted topo + file-lock-correct `batches[]` in step 1 using the same canonical batcher the standalone batch-recompute helper would call, so any post-step-2 batcher re-pipe would be a provable no-op. The merged schedule flows directly from this step into step 4's `write-schedule` without any further batching call.

**Shape-shift `build-tasks` → `write-schedule`.** `build-tasks` emits `{ok, outcome, tasks, batches, warnings, errors}`; the canonical schedule shape is `{outcome, tasks, batches, gaps, risks}`. The orchestrator synthesizes it in memory: keep `tasks` (merged with step 2 `agent`), keep `batches` verbatim, drop `ok`/`errors`, **map `warnings[] → gaps[]`** as `{task_id, type:w.code, severity:"soft", detail:w.message}` (drop `plan_file`; downstream lookup is via `tasks[].plan_file`), default `risks:[]`. Persisted `outcome` is derived per the validator: `outcome='valid'` ⟺ `gaps==[]`, else `outcome='needs-enrichment'`. `write-schedule --stdin` rejects any top-level key outside the canonical five — the shape-shift is mandatory.

Apply filters and overrides at this seam (on the in-memory schedule, before persistence):

- `--claude-only` or `codex_available=false` → rewrite `tasks[].agent = "claude"` in the in-memory schedule.
- `--codex-only` → drop claude tasks.
- `--task-ids` → in-memory: pipe the schedule through `filter-schedule --stdin --task-ids <csv> --json` (emits requested IDs + transitive prerequisites preserving source batch boundaries). Unknown ID → halt `unknown-task-id`; missing dep → `missing-dependency`; cycle → `dependency-cycle`. The `--stdin` path accepts `outcome='needs-enrichment'`; the legacy `--schedule-file` path still requires `outcome='valid'`.

Filtered in-memory schedule flows directly into Step 4. No `compute-schedule` re-pipe; persistence happens exclusively in Step 4.

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

**Dispatch.** Agent (`plan-review-triage`, `model: "sonnet"`) using the Phase 1-triage / Phase 1.5.5 template with `source="plan-analyst"`. `schedule_path` is intentionally absent — no schedule exists at this seam (Phase 1-triage fires from Step 1, pre-Step 4 `write-schedule`); the agent operates on plan text + `warnings[]`. Wrap with `plan_review_triage_start {source:"plan-analyst", findings_count:<N>}` before and `plan_review_triage_done {source, verdict, load_bearing_count, dismissed_count, summary}` after. Pipe the agent output through `parse-plan-review-triage-report --stdin --source plan-analyst --findings-count <N> --json` — returns `{verdict, load_bearing, dismissed, summary, findings_count, source}`.

**Route by verdict.**

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

**Both paths.** Wrap the dispatch with `plan_review_start {reviewer, plan_file}` before and `plan_review_done {reviewer, plan_file, verdict, findings_count, summary}` after.

- **`claude_only=true` (Phase 1.5-Claude path).** Dispatch the `plan-reviewer` Agent (`model: "sonnet"`) using the Phase 1.5-Claude template; the agent emits a markdown report ending in a fenced ```json block. Pipe that JSON through `parse-plan-review-report --stdin --from-claude --json`.
- **`claude_only=false` (Phase 1.5-Codex path).** Shell out to `$PYTHON plan_codex_dispatch.py plan-review --schedule-file <schedule_file> --repo-root <repo> --timeout 180 [--allow-gaps]`. The wrapper emits the full envelope `{task_id, subcommand, outcome, codex_exit_code, parsed}`; pipe the entire envelope (NOT just `parsed`) through `parse-plan-review-report --stdin --json` (no `--from-claude` flag). Schedule-only review post-TASK-008: the reviewer reads only `<schedule_file>` (unified fat `tasks[]`); `--plan-file` and `--plans-dir` were removed.

`parse-plan-review-report` validates the envelope against `codex_plan_review_schema.json` and extracts `{plan_file, verdict, findings_count, findings, notes, schedule_ok, summary}`. Schema violations halt with structured `errors[*]`. Wrapper timeout / parse_error / failure outcomes degrade to `plan_review_skipped {reason:"codex_unavailable"}`; Agent dispatch failures degrade to `plan_review_skipped {reason:"claude_review_failure"}` (see `dispatch-templates.md` §Phase 1.5-Claude). Cross-plan dependency resolution is enforced by Phase 0 `check-plan-deps` and is no longer surfaced by the reviewer.

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

**Dispatch.** Agent (`plan-review-triage`, `model: "sonnet"`) using the same Phase 1-triage / Phase 1.5.5 template with `source="codex-plan-review"` (single template, two seams). Wrap with `plan_review_triage_start {source:"codex-plan-review", findings_count:<N>}` / `plan_review_triage_done {source, verdict, load_bearing_count, dismissed_count, summary}`. `<N>` comes from the prior `parse-plan-review-report --json`. Pipe agent output through `parse-plan-review-triage-report --stdin --source codex-plan-review --findings-count <N> --json`.

**Route by verdict.**

| Verdict | Action (Codex source) |
|---|---|
| `ship` | Proceed to Phase 2 (batch dispatch). Skip `plan-author`, skip re-analyst, skip second plan-review. Summary carries bare `[plan-review-disagreement]` banner listing Codex findings verbatim. |
| `ship-with-fixes` | Proceed to Phase 2. Codex findings carried to the summary's "Plan review notes" section. |
| `partial-agreement` | Dispatch `plan-author` with a findings payload filtered to the `load_bearing` indices only. Dismissed findings carried verbatim to the summary's "Plan review notes" section with the dismissed indices called out. Then re-analyst, then re-run Codex `plan-review`; the second verdict is binding — no second triage. |
| `needs-rework` | Dispatch `plan-author` with the full findings array (today's auto-revise behavior). Then re-analyst, then re-run Codex `plan-review`; the second verdict is binding — no second triage. |

On `partial-agreement` and `needs-rework` the routing then falls through to the existing author → analyst → review sequence documented below; the triage selects the payload the author receives (filtered vs full) but does NOT change the three-step sequence.

**Per-finding embeds in the triage dispatch (TASK-007).** Before dispatching the Phase 1.5.5 triage, the orchestrator enriches the Codex `findings[]` payload so the triage agent can reason about prioritization: every finding in the embedded JSON carries `{target_task_id, blocking, severity}` alongside the existing `section`, `concern`, `suggested_change`. The triage template (see `dispatch-templates.md` §Phase 1-triage / Phase 1.5.5) instructs the agent to evaluate the findings in priority order: `blocking=true` first, then `severity=critical`, then `severity=important`, then `severity=minor`. Schedule-level findings (`target_task_id=null`) are evaluated against the schedule JSON + roster rather than a single task block; per-task findings (`target_task_id="NNN"`) are evaluated against the corresponding `tasks[NNN]` entry.

**`needs-replan` branch — auto-revise path (default).** Auto-revise is on unless `--no-auto-revise` is set. Three-step author → analyst → review sequence before the second verdict is accepted:

1. **Dispatch `plan-author` per finding (fan-out, TASK-007)** — Agent (`plan-author`, `model: "opus"`) using the Phase 1.5a template (Variant A task-targeted / Variant B schedule-level). One dispatch per finding, NOT one whole-plan dispatch. Filtered-or-full findings array: filtered to triage `load_bearing` on `partial-agreement`; full on `needs-rework`. Per-dispatch input shape: task-targeted → `{finding, target_task_id, child_plan_file}` (resolve `target_task_id → child_plan_file` via schedule's `tasks[].plan_file`; `00_INDEX.json` off-limits on this path); schedule-level (`target_task_id=null`) → `{finding, target_task_id:null, roster_file:<plan_dir>/00_INDEX.json}` (allowed edit surface is `roster_file` or empty). Author write scope is the single named file per dispatch. Overlapping roster edits across sibling schedule-level dispatches are fine. Dismissed findings on `partial-agreement` are NOT forwarded — they ride the summary per the route table. Wrap EACH dispatch with `plan_author_start` before and `plan_author_done {files_edited[], findings_actioned[], findings_skipped[]}` after.

2. **Re-run Phase 1** (`build-tasks → classifier → write-schedule`) reading from disk (do NOT forward the author's report — invites ping-pong). Route: `build-tasks errors[*] non-empty` → halt `plan_review_failed reason_detail=author_introduced_structural_defect`; `warnings[*] non-empty` → allow-gaps routing as first pass; `ok: true` empty `warnings[]` → step 3.

3. **Re-run Codex `plan-review`** on the revised plan. Second verdict is binding: `approved | approved-with-notes` → batch dispatch; `needs-replan` → halt per "Second `needs-replan`" block below.

**`needs-replan` branch — `--no-auto-revise` path (opt-out).** When `--no-auto-revise` is set, the first `needs-replan` verdict halts immediately without dispatching `plan-author`. This preserves the pre-TASK-025 behavior for users who prefer to apply revisions by hand; halt with `run_end reason=plan_review_failed` per the block below. No silent retry without revision.

**Second `needs-replan`** (after one author → analyst → review retry, on the auto-revise path), OR the first `needs-replan` when `--no-auto-revise` is set: halt before any batch runs.

```bash
$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event run_end \
  --fields-json '{"run_id":"<id>","outcome":"failed","reason":"plan_review_failed","findings":[...]}' --json
```

Release the run-lock and print the failure envelope. Do NOT call `fail-task` (no task has started). Do NOT run batches. The run summary records `outcome=failed reason=plan_review_failed`.

**Retry failures** — if the `plan-author` dispatch itself fails (malformed report, out-of-scope writes) or the re-validation analyst returns `outcome=invalid`, halt with `run_end reason=plan_review_failed` same as the second-`needs-replan` path.

**Run-log event order (V8).** Analyst (Phase 1) and Codex (Phase 1.5) branches interleave triage events on the pedantic paths. Canonical sequence: `run_start` → (Phase 1 with optional `plan_review_triage_{start,done} source:"plan-analyst"` + author retry) → `schedule_written` → `analyst_done` → (Phase 1.5 with optional `plan_review_triage_{start,done} source:"codex-plan-review"` + author retry) → `batch_start`. `analyst_done` is emitted only after `write-schedule` persists. Triage events share names across both seams; the `source` field discriminates. Skip events: `analyst_triage_skipped {reason:"binding_flag"|"allow_gaps"}` (Phase 1), `plan_review_skipped` (Phase 1.5 wrapper failures or `--skip-plan-review`).

### Dry-run mode

If `--dry-run`: print the schedule + intended dispatches. Release lock. Exit. Dry-run scope is sticky — a follow-up "actually run it" message requires a fresh invocation without `--dry-run`, or explicit user instruction.

## Execute mode — per-batch A→E loop

Run state (`done` / `failed` / `locked_files` / `blocked` / `ready`) is sourced from `plan_ops.py batch-next --from-schedule-state` (single source of truth — `.schedule.json`'s state block, mutated atomically by `commit-task --update-schedule-state`, `fail-task --update-schedule-state`, and `block-dependents --update-schedule-state`). The orchestrator does NOT track this state in-message; it asks `batch-next` for the next pickable batch each iteration. `review_notes` (per-task minor findings for the run summary) is the only thing the orchestrator carries across turns, and only because it is appended-not-mutated narrative material.

Loop until `batch-next` returns an empty batch with `ready_remaining=0` OR `scheduler_stuck=true`.

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
- **Claude tasks** → build the canonical wrapper input via `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" build-claude-dispatch-input --plan-file <abs> --task-id NNN --variant default [--target-task-id NNN] [--analyst-annotations <path>] --starting-sha "$STARTING_SHA"` and pipe its stdout into `Bash: $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input -`. The subcommand emits the canonical `claude_dispatch_input.json` shape — `agent: "plan-implementer"`, `overrides.model: "opus"`, `payload: {plan_path, repo_root, task_id, target_task_id?, analyst_annotations, starting_sha}`, and the now-required top-level `declared_files_changed` derived from the task's `Files:` list via `_extract_task_files_from_plan` (the same canonical helper `_gate_commit_safe` uses). See §dispatch-templates §Phase B for the full skeleton. The wrapper renders the **Phase B** template from `dispatch-templates.md` and returns the v3 envelope (see §Dispatch error handling (Claude wrapper) for shape). Migrated from `Agent(subagent_type: "plan-implementer", ...)` per TASK-004.
- **Codex tasks** → Wrapper computes the timeout default from `len(task["files"])` per the formula in §Bash-call idioms; pass `--timeout N` to override.
  `Bash: $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement --plan-file <abs> --task-id NNN --repo-root <abs>`.

Await all. For EVERY task (success or not) append `implement_done {task_id, outcome, files_changed[], test_outcome, wall_seconds}`.

**Classify per task:**

*Claude envelope (JSON from wrapper, post-TASK-004):*

**Extraction:** invoke `claude-envelope-extract --agent plan-implementer --json` per §Dispatch error handling (Claude wrapper). On `status==ok` route on `outcome ∈ {success, partial, failed, plan-incorrect, blocked, malformed}` and surface `.result.report.{plan_adaptations[], concerns_for_reviewer[], on_failure_revert}`. Validate `.result` against `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` (TASK-002 fixture). Stage label is `implement`. Post-hoc scope check (legacy markdown-only path; on the wrapper path the extract's `scope_violation` is authoritative):

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

At the batch join barrier, reconcile observed out-of-scope writes via `plan_ops.py reconcile-batch --repo-root <repo> --schedule-file <path> --plans-dir <plan_dir> --out-of-scope-policy <pause|reconcile-and-revert> < envelopes.json`. With `--schedule-file`, each envelope's `out_of_scope_tracked` / `out_of_scope_untracked` is partitioned against the dispatched task's normalised `Files:` list: declared-in-scope entries are PRESERVED (recorded in `reconcile_kept_*` with `scope_violation_preserved`), genuinely out-of-scope entries follow the policy. Always pass `--schedule-file` for `/implement-plan` runs — without it (or on schedule-load failure) the fallback is restore-everything under `reconcile-and-revert`, which may wipe a correct in-scope edit when a stale wrapper emits a buggy envelope.

**`--out-of-scope-policy pause` (default).** Per envelope with `out_of_scope_observed=true`, mutate the task's plan-status to `paused` and invoke the **Awaiting-user pause** subroutine (call-site `post_reconcile_out_of_scope`). Surfaces four user-facing options: `widen-plan`, `in-place-fix`, `keep-and-commit`, `revert` (authorizes `fail-task --authorization-source reconcile-out-of-scope-user-instruction`, the only sanctioned post-pause revert). Pause is per-task — peer tasks in the same batch proceed.

**`--out-of-scope-policy reconcile-and-revert` (legacy / unattended).** Restores tracked entries via `git restore` and unlinks untracked entries. Used when interactive disposition is unavailable; `reconciliation_failed` is a hard halt regardless of policy.

**Policy mapping** from `$UNATTENDED_REVERT_POLICY`: `pause` → `--out-of-scope-policy pause`; `fail-fast` and `preserve-only` → `--out-of-scope-policy reconcile-and-revert`.

### Phase C — Handle Phase B failures

#### Sandbox divergence escape hatch (TASK-008)

Before classifying a Codex `implement` failure with `cause: independent_test_run_failed`, run `plan_ops.py auto-validate-divergence --envelope-file <wrapper-envelope.json> --test-command "<task Test command:>" --repo-root <repo> --run-id <id> --task-id NNN --json` (re-executes the task's declared `Test command:` in the target env; the wrapper's recorded `sandbox_test_command` is NOT used).

- **Target-passes** (`divergence: true`): treat as success. Handler appends `sandbox_divergence` event with sandbox + target captures (truncated at 32 KB). Next `commit-task` MUST pass `--sandbox-divergence-tag`. Phase D proceeds as if the implementer succeeded.
- **Target-fails** (`divergence: false, applicable: true`): real failure; existing path (Codex→Claude fallback OR `fail-task`) unchanged.
- **Non-matching** (`applicable: false`): no-op; existing path unchanged.

The `[sandbox-divergence]` tag is informational and does NOT relax the reviewer-verdict whitelist. `--reviewer none` remains the final-resort override; prefer the auto-validate branch when divergence is the cause. End-of-run summary surfaces affected tasks via `run-summary --section sandbox-divergences`.

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

**Wrapper failure outcomes (Codex side, `claude_only=false`).** `subcommand=review` `outcome ∈ {timeout, parse_error, failure}` → log `review_skipped {task_id, reviewer:"codex", reason}` mapping `reason ∈ {codex_review_timeout, codex_review_parse_error, codex_review_failure}`, then commit with `--reviewer none --reviewer-verdict ""`. Wrapper-side errors degrade to a documented skip rather than blocking. Agent-side failures (Claude reviewer, either direction) fall under existing Agent retry semantics + the D.5 ladder; not covered by this clause.

#### D.2 — Route by verdict (call-and-comply via `review-route`)

Routing across D.2 → D.2a (D.5 escalation) → D.2a.5 (bounded remediation) → D.2a.6 (narrow remediation) → pause is **deterministic and lives in `plan_ops.py review-route`**. The orchestrator does NOT inline this state machine — it builds the routing inputs (implementer side, reviewer side, reviewer verdict, D.5 verdict if dispatched, `claude_only`, `--codex-review-binding`, `--unattended-revert-policy`) and calls `$PYTHON plan_ops.py review-route --json`; the subcommand returns one of `commit | fail | dispatch_d5 | dispatch_bounded_remediation | dispatch_narrow_remediation | dispatch_role_swap | pause_awaiting_user | unknown_state`. The orchestrator complies with the returned action: `commit` → §D.3 (with the appropriate `--disagreement-tag` / `--remediation-tag` / `--narrow-remediation-tag` per the route payload's `commit_flags`); `fail` → §D.4 (which itself is rescue-then-pause per the Completed-Work Preservation Principle — the `fail` action enters §D.4's rescue/pause flow, NOT a direct `fail-task` call; only an explicit `authorization_source` in the route payload sanctions destruction at all); `dispatch_d5` → re-dispatch the **Phase D.5** template (Agent, `code-reviewer`, `model: "sonnet"`, `wrapper_checks` defaulted to `{"symbol_warnings": []}` if absent) and re-call `review-route` with the parsed D.5 verdict; `dispatch_bounded_remediation` → re-dispatch the **Phase B-rework** template via the v3 wrapper (build the wrapper input via `plan_ops.py build-claude-dispatch-input --variant rework --dispatch-context <findings_for_retry+d5_summary.json>` and pipe stdout into `plan_claude_dispatch.py run --input -`; the subcommand emits `agent: "plan-implementer"`, `overrides.model: "opus"`, and the schema-required top-level `declared_files_changed` populated from the task's `Files:` list; one attempt; re-call D.1 then `review-route` with the re-review verdict); `dispatch_narrow_remediation` → re-dispatch the **Phase B-narrow-remediation** template via the v3 wrapper (build the wrapper input via `plan_ops.py build-claude-dispatch-input --variant narrow-remediation --dispatch-context <load_bearing+dismissed+d5_summary.json>` and pipe stdout into `plan_claude_dispatch.py run --input -`; the subcommand emits `agent: "plan-remediator"`, `overrides.model: "opus"`, the schema-required top-level `declared_files_changed`, and `payload.dispatch_context: {load_bearing_findings, dismissed_findings, d5_summary}` forwarded verbatim from `review-route`'s `dispatch_context`; one attempt; envelope flows through `claude-envelope-extract --agent plan-remediator` per §Dispatch error handling (Claude wrapper); on `status==ok` route on the returned `outcome ∈ {success, partial, failed, plan-incorrect, blocked, malformed, scope-violation}` — `scope-violation` routes to `pause_awaiting_user` with `stage:"post_narrow_remediation_implement"`; the mandatory `**Dismissed findings noted:**` report section surfaces as `.result.report.dismissed_findings_acknowledged[]`; the orchestrator's D.5 gate reads from there; on success re-call D.1 then `review-route`); `dispatch_role_swap` → §D.2b; `pause_awaiting_user` → invoke the **Awaiting-user pause** subroutine with the route payload's `stage` (one of `post_remediation_review`, `post_remediation_implement`, `post_narrow_remediation_review`, `post_narrow_remediation_implement`, `post_binding_block`, etc. — see the call-site table at §Awaiting-user pause for the per-stage payload contract).

**Unknown-state pause rule (mandatory).** If `review-route` returns `unknown_state`, the orchestrator MUST invoke the **Awaiting-user pause** subroutine immediately — do NOT guess at the routing, do NOT fall through to a default branch, do NOT call `fail-task`. `unknown_state` is the routing seam's escape hatch for an input tuple the call-and-comply table does not cover; surfacing it to the user is the only sanctioned response. The pause carries the verbatim route inputs in `awaiting_user.fields.review_route_inputs` so the next turn can diagnose. Bounding rules (one D.5 dispatch, one bounded-remediation retry, one narrow-remediation retry, no recursion) are enforced inside `review-route` itself; the orchestrator never re-asks beyond what the subcommand authorizes. Log `disagreement {task_id, codex_findings}` (with `--findings-json "$(<json-array>)"`) before any `dispatch_d5` so the audit trail records what Codex flagged.

Minor findings in either direction → `review-route` returns `commit`; record in run summary AND commit body tail. Never silently dropped. Under `claude_only=true`, on a Claude-impl→Claude-review `needs-rework` the D.5/D.2a.5/D.2a.6 ladder collapses (no third-opinion adjudication, no bounded/narrow retry); the route enters §D.4 rescue/pause, NOT direct `fail-task`. Documented in §Rules. `--codex-review-binding` makes Codex `needs-rework` binding for commit: `review-route` returns `pause_awaiting_user` with `stage=post_binding_block` (or `fail` under `--unattended-revert-policy fail-fast`). The full per-stage `awaiting_user` payload contract — `post_remediation_review` (codex_findings + d5_summary), `post_remediation_implement` (retry_outcome + diagnostics + reversion_guidance), `post_narrow_remediation_review` (+ dismissed_finding_indices), `post_narrow_remediation_implement`, `post_binding_block`, etc. — is defined in the table at §Awaiting-user pause and remains the authoritative payload contract; the route paragraph above only governs *which* action the orchestrator dispatches.

#### D.2b — Role-swap retry (Codex implements + Claude reviewer needs-rework)

Per §8.3 line 692: Claude re-implements, Codex re-reviews. One attempt.

1. Re-dispatch the Phase B template to `plan-implementer` via the v3 wrapper: build the wrapper input via `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" build-claude-dispatch-input --plan-file <abs> --task-id NNN --variant role-swap --starting-sha "$STARTING_SHA"` and pipe its stdout into `Bash: $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input -`. The subcommand emits the canonical `claude_dispatch_input.json` shape with `agent: "plan-implementer"`, `overrides.model: "opus"`, and the now-required top-level `declared_files_changed` populated from the task's `Files:` list. Reviewer feedback is NOT forwarded in v1. Migrated from `Agent(subagent_type: "plan-implementer", ...)` per TASK-004. The `retries_used.role_swap` budget check is performed orchestrator-side BEFORE this dispatch — it is not enforced inside the wrapper.
2. Classify the retry with the same Phase B rules. `outcome ≠ success` → D.4 reason `retry_implement_failed`.
3. On retry success, re-run D.1 using the **Codex** reviewer. Binding — no further retry.
4. Route re-review: `clean | minor-findings` → D.3. `needs-rework` → D.4.

**`claude_only=true` re-review variant.** Structurally unreachable (Phase 1 rewrites `tasks[].agent="claude"`); documented defensively. Were it reachable, step 3's re-review would use the `code-reviewer` Agent (NOT the Codex wrapper); step 4's verdict vocabulary would be `ship | ship-with-fixes` → D.3, `needs-rework` → D.4. Re-review remains binding.

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

Tag flags: `--remediation-tag` (post-D.2a.5), `--narrow-remediation-tag --dismissed-finding-ids I,J,K` (post-D.2a.6), `--d4-rescue-tag` (post-D.4 rescue). Argparse enforces XOR / requires constraints between these tags and `--disagreement-tag` / `--dismissed-finding-ids` — see `commit-task --help` for the full constraint matrix.

Subcommand flow: (1) pre-commit guard for staged overlap, (2) plan-status → `done`, (3) `git commit --only <files> <plan-file>`, (4) SHA capture, (5) `commit_done` run-log append.

**Post-D.5 disagreement commits.** When `review-route` returns `commit` after a D.5 third-opinion (Codex `needs-rework` → D.5 `ship | ship-with-fixes`), pass `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` (NOT `--reviewer codex --reviewer-verdict needs-rework` — that's rejected as `uncommittable-reviewer-verdict`). Record Codex's original findings verbatim in `--reviewer-minor-findings`; mark dismissed entries with `disposition:"dismissed"` (+ optional `disposition_reason`). D.2b inverts the roles: `--reviewer codex --reviewer-verdict clean|minor-findings --disagreement-tag`. The binding reviewer is always whichever side satisfied the commit-allowed set.

Commit hook failure → subcommand auto-rolls back (`git reset HEAD`, restore plan text) → treat as D.4 `stage=commit`.

**Post-commit `commit-safe` gate.** On success, capture `commit_sha` and verify the commit touched only TASK-NNN's declared `Files:` (plus always-ignore + plan file): `$PYTHON plan_ops.py gates --check commit-safe --plan-file <abs> --task-id NNN --commit-sha <sha> --json`. `status: fail` → log `commit_safe_gate_failed` and halt before next batch.

**Opt-in V-check gate (TASK-020B).** Plans may declare `acceptance_v_check: <shell command>` in YAML frontmatter. When present, `commit-task` runs it (cwd=repo-root, `--v-check-timeout` default 300s) before the git commit. Success → `v_check_passed`. Failure (non-zero, timeout, subprocess error) → halts with `errors[*].code ∈ {"acceptance-v-check-failed", "v-check-timeout", "v-check-subprocess-error"}` (including `stdout_tail` / `stderr_tail`); no plan mutation, no commit, no `commit_done`. `shell=True` is intentional — plans must not be edited by untrusted parties.

#### D.4 — Try rescue, then pause (TASK-005)

D.4 is **no longer a destructive seam**. The reviewer's `needs-rework` (or other halt-worthy verdict) does NOT immediately call `fail-task`; the orchestrator dispatches a **single-shot terminal rescue** first. Rescue-success commits via D.3 with `--d4-rescue-tag`; any non-success outcome falls into the **Awaiting-user pause** subroutine.

Sequence: (1) log `d4_rescue_start {task_id, reviewer_findings_count}`; (2) dispatch `plan-remediator` (Agent, `model: "opus"`) using the **Phase D.4-rescue** template — inputs are `rescue_findings[]` (verbatim reviewer findings — a distinct key from D.2a.6's `load_bearing_findings[]` so the audit log can tell which retry path fired), `dismissed_findings: []` (literal empty list; D.4 treats every reviewer finding as load-bearing), the task block, reviewer source, and analyst annotations; (3) **strictly single-shot** — `outcome != success` → log `d4_rescue_done` and pause; (4) on rescue success, re-run D.1 (the original reviewer — Codex for Claude-implemented work, `code-reviewer` Agent under `claude_only=true`); the re-review is binding; (5) `clean | minor-findings | ship | ship-with-fixes` → D.3 with bare `--d4-rescue-tag`; `needs-rework` → pause via the subroutine with `stage:"post_d4_rescue_failed"` and payload `{reviewer_findings[], rescue_attempt_outcome, rescue_diagnostics}` (full per-stage contract at §Awaiting-user pause).

A user-instructed revert from this pause invokes `fail-task --authorization-source phase-d4-rescue-failed` — the *only* sanctioned route from the D.4 pause to a destructive action.

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

1. **Per-child `update-plan-header --status <complete|partial>`** — partition completed + failed tasks by `plan_file`; per-child `complete` iff every task in that child passed. No run-level aggregate header (design boundary). Children with no tasks untouched.
2. **Per-child `finalize-execution-log`** — `--rows-json` row schema is `{task, agent, reviewer, verdict, commit, notes}` (six required string keys; no extras). Verdict cells include `[disagreement]` / `[remediation]` / `[narrow-remediation]` markers. `--outcome` ∈ `{success, partial, failed, paused}`; use `paused` when exiting via the awaiting-user path. Partition rows by `plan_file`; call once per distinct child. No run-level aggregate table.
3. **Log `run_end`** with counts `{done, failed, disagreement_count, minor_findings_total}` + `outcome=paused` when halting on a pause path. Include `plan_file: "<dir-basename>"` (matches `run_start`).
4. **Print summary** — counts, failure reasons, disagreement-tagged commits, per-task minor-findings digest, `git log --oneline <starting_sha>..HEAD`. Include `TaskList mirror state: {synced|drifted}` from comparing run-log `commit_done`/`task_failed` counts against `TaskList`. **`claude_only=true` loud banner (mandatory):** *"Claude-only mode: Phase 1.5 plan review and Phase D cross-review ran via Agents (Sonnet); no Codex shell-out fired."* with discriminator `(--claude-only flag)` or `(codex_available=false)`.
5. **Housekeeping commit** (skip if `done == 0 AND failed == 0`): `git add <plan-file> <run_log>; git commit -m "chore(implement-plan): run <run_id> bookkeeping"`.
6. **Certify the execute bundle.** `$PYTHON plan_ops.py gates --certify --mode execute --plan-file <abs> --schedule-file <schedule_file> --run-id <id> --json`. Re-checks all six gates; re-verifies `commit-safe` per `commit_done` event. Zero commits → `commit-safe: not_applicable` (not a failure). `certified: false` → emit `certify_failed` and surface the failing gate; do NOT retroactively reopen committed tasks. Report, not retry trigger.
7. `plan_ops.py release-lock` (finally-style; runs on early halt too).

Do NOT auto-push. Do NOT auto-PR.

## Rules

### Orchestrator LLM responsibilities

The orchestrator's job after Phase B/D mechanical work is THREE narrative duties — everything else is delegated to `plan_ops.py` subcommands and the wrapper.

**Preserved duties (the orchestrator MUST do these):**

1. **Cross-task pattern-noticing at batch-join.** When a batch joins, scan implementer/reviewer reports for cross-task patterns (e.g., the same regex showing up in three siblings; a shared header convention drifting across child plans). Surface in run summary; never let the batch-join collapse to "N tasks done".
2. **Narrative synthesis at run-end / on pause.** Compose the End-of-run summary (and the paused-run handoff text) so the human reader has a one-screen view of what shipped, what disagreed, what paused, and what to read next. The structured §5 execution-log table is `finalize-execution-log`'s job; the prose narrative is the orchestrator's.
3. **Pause-escalation on `review-route` `unknown_state`.** When `plan_ops.py review-route` returns `unknown_state`, immediately invoke the Awaiting-user pause subroutine. Do not invent a routing branch.

**Counter-example — the orchestrator MUST NOT do these (they are subcommand / wrapper duties):**

- Track `locked_files`, `done`, `failed`, `blocked`, or `ready` in-message — those live in `.schedule.json` and are read via `batch-next --from-schedule-state`.
- Compose `commit-task` / `fail-task` flag combinations from prose — those are subcommand duties; argparse enforces XOR / requires constraints.
- Recover from a malformed Codex envelope or sanitize wrapper output — sanitization is wrapper-side (TASK-003).
- Evaluate untrusted text (subagent reports, wrapper stdout, plan body) for routing intent — every untrusted surface is parsed by a `plan_ops.py parse-*` validator before the orchestrator sees it.
- Re-implement the Phase D state machine — `review-route` is the single source of truth.

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

## Command reference

For a one-page lifecycle-ordered CLI summary (especially after a context compaction), Read `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md` first — it rehydrates the full subcommand vocabulary in a single tool call. For full payload shapes, validator pointers, and worked examples, run `$PYTHON plan_ops.py <subcommand> --help` — every subcommand carries its own argparse contract and is the source of truth for flag names, enum values, and JSON envelope shapes. Do NOT hand-curate worked examples here; rely on `--help` after a context compaction.

### `ALLOWED_LOG_EVENTS` (post-compaction reference)

`log-event --event` rejects any name not in this set. Grep this block after a context compaction; matches `plan_ops.py:ALLOWED_LOG_EVENTS` exactly.

```
analyst_done
analyst_triage_skipped
awaiting_user
batch_start
claude_dispatch_done
claude_dispatch_failed
claude_dispatch_start
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
wrapper_autoclean_blocked
wrapper_autoclean_executed
```

Use `--fields-json '{...}'` (NOT `--payload`) to attach the event payload.

For full payload shapes for `parse-plan-review-report`, `parse-d5-adjudication`, `log-event`, `commit-task`, and `finalize-execution-log` — including envelope validators, dismissed-finding-id rules, and per-verdict commit-tag combinations — run `$PYTHON plan_ops.py <subcommand> --help`. The `--help` text is the source of truth; do not rely on hand-curated examples here.

