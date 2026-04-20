---
name: "implement-plan"
description: "Dual-agent plan executor. Dispatches Claude-tier tasks via plan-implementer and Codex-tier tasks via plan_codex_dispatch.py wrapper, with per-batch cross-review. Interleaves implement -> review -> commit inside each batch to keep review diffs clean."
user_invocable: true
---

# Implement Plan

**Arguments:** $ARGUMENTS

## Dispatch rules (read before any subagent call)

1. **Parallel dispatch per batch.** Up to `--parallel N` dispatches in a SINGLE message inside Phase B. Claude-tier via Agent, Codex-tier via Bash — both kick off in the same message when a batch contains both.
2. **Trust the analyst's schedule.** Batches with disjoint `file_locks` are parallel-safe. Do not add extra safety reasoning.
3. **Your job is routing only.** Pick tasks from the analyst's schedule, dispatch, interpret reports, commit/revert. No code reading, no diff judgment, no scope inflation.
4. **Dispatch prompts must be self-contained.** Read `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md` for the templates. Every Agent prompt includes the full task block verbatim + "You do NOT have the Agent tool."
5. **Timeouts on Bash calls.** 5000ms for idioms (printf, git status); 180000ms (180s) for `plan_codex_dispatch.py review`; 300000ms (300s) for `plan_codex_dispatch.py implement`. Timeouts are enforced by the wrapper internally — pass `--timeout 180|300` as documented.
6. **Subagent errors.** If a dispatch returns `[Tool result missing due to internal error]` or no parseable report, treat as failure. Log it, restore partial changes, do NOT retry silently. Claude-implementer `malformed` outcome goes to `fail-task stage=implement reason=malformed_report`.
7. **Cross-review asymmetry.** Claude implements → Codex reviews; Codex implements → Claude reviews. Escalation path differs by direction — see Phase D.2.

## Promotion criteria

The executor promotes from dry-run to execute (and from execute to "certified-clean") via six canonical phase gates. Each gate returns `{name, status ∈ pass|fail|not_applicable, reason}`. Invoke them through `plan_ops.py gates` — never reimplement the predicates inline.

| Gate | Phase | What it asserts |
|---|---|---|
| `schema-valid` | Phase 0 preflight | Plan markdown conforms to §5: `## Goal`, a `## Context` or `## Scoped Context`, `## Verification`, and every `### TASK-NNN` block carries Status / Priority / Files / Test command / Acceptance criteria bullets + Description prose header. |
| `schedule-valid` | Phase 0 preflight | Analyst JSON passes `_validate_schedule` + `_validate_schedule_dag` (shape + DAG). |
| `fixture-valid` | Phase 0 preflight | The sample fixture (`sample_phase4.md`) itself passes `schema-valid` + `schedule-valid`. Pre-TASK-006 this is expected to fail — TASK-005 establishes the predicate; the rewrite is TASK-006's scope. |
| `execution-safe` | Phase 0 preflight | `plan_codex_dispatch.py` implement path carries the always-ignore / protected-paths seam, `_snapshot_baseline(` is called at implement + timeout sites, and there is no `git clean -fd` in executable code. Predicate-only — does NOT invoke the wrapper. |
| `review-safe` | Phase 0 preflight | `plan_codex_dispatch.py cmd_review` carries `_snapshot_baseline(` and respects `is_protected_path` / `PROTECTED_EXACT_PATHS`. |
| `commit-safe` | Phase D.3 (per commit) + End-of-run certification | `git show --name-only <sha>` minus TASK-NNN's declared `Files:` list and the always-ignore set is empty. Dry-run mode → `not_applicable`. Execute mode → verified post-hoc from every `commit_done` run-log event for the run. |

**Dry-run pass condition:** `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe` all `pass`; `commit-safe` is `not_applicable` (no commits in dry-run).

**Execute pass condition:** all six gates `pass`, with `commit-safe` re-verified per `commit_done` event via `plan_ops.py gates --certify --mode execute --run-id <id>`.

## Bash command idioms

Standard commands used by this skill:

- `venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" <subcommand> [--json]` — all plan parsing, schedule evaluation, batch selection, narrow commit, failure handling, status transitions, and run-log append verification.
- `venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement|review ...` — Codex-tier implementer and reviewer. Emits a single JSON envelope on stdout.
- `date -u +%Y%m%dT%H%M%S` — run_id fallback (preflight emits one authoritatively).

Never write inline Python for plan operations. Never `git stash` inside this skill — the wrapper restores Codex-side independently; the orchestrator restores Claude-side via `plan_ops.py fail-task` (which uses `git restore`).

## plan_ops.py CLI reference

All subcommands accept `--json` for machine-readable output.

| Command | Purpose |
|---|---|
| `plan_ops.py preflight --plan-file <abs> [--strict-branch]` | Smart dirty-tree + codex probe + starting_sha + run_id + base-branch check |
| `plan_ops.py parse-schedule --stdin [--strict]` | Validate analyst JSON; surface errors, tasks, batches, gaps, risks. `--strict` promotes unknown nested fields from warning to error. |
| `plan_ops.py compute-schedule --stdin [--strict]` | Recompute file-disjoint batches from `tasks[]`; use after any filter rewrite before persisting the schedule. |
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
  --codex-review-binding  Codex critical on Claude goes straight to fail-task;
                          no §8.4 third-opinion escalation
  --allow-gaps            Proceed past analyst outcome=needs-enrichment
  --strict-branch         Halt (not warn) if current branch != plan's Base branch
```

Mutual exclusions: `--codex-only` + `--claude-only` → error. Normalize `--task-ids` values via `plan_ops.py normalize-task-id` before filtering.

## Pre-flight (Phase 0)

First, bind the path placeholders used throughout this skill by querying the configured plan directory:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" path-info --json
```

Returns `{"plan_dir": "...", "run_log": "...", "run_lock": "...", "schedule_glob": "..."}`. Bind:
- `<plan_dir>` ← `plan_dir`
- `<run_log>` ← `run_log`
- `<run_lock>` ← `run_lock`
- `<schedule_file>` ← `<plan_dir>/<basename>.schedule.json` (constructed per plan from `<plan_dir>` and the plan-file basename without `.md`)

Use these placeholders verbatim in all subsequent commands; never hardcode `docs/plans`.

Then run preflight:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" preflight --plan-file <absolute plan> [--strict-branch]
```

Returns JSON with `pass`, `starting_sha`, `run_id`, `codex_available`, `dirty_files{source_blocking, infra_ignored, plan_doc}`, `base_branch_match`. Halts on `source_blocking` dirty. `plan_doc` churn (the plan file being executed) is allowed. `infra_ignored` warns but proceeds.

If `codex_available=false`, override `tasks[].agent = "claude"` throughout Phase 1 and warn; wrapper's own "codex binary not found on PATH" branch is the backstop.

Then run the mandatory cross-plan dependency gate:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" check-plan-deps \
  --plan-file <absolute plan> --plans-dir <dirname of plan-file> --json
```

Halts on `pass: false` with the `unresolved[]` list. Halts on non-empty `errors[]` as internal-error. There is no `--allow-gaps` override — cross-plan deps are hard blockers.

Then run the five pre-dispatch phase gates (`schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`). The schedule file is written later in Phase 1, so the preflight batch runs `schema-valid`, `fixture-valid`, `execution-safe`, `review-safe` here; `schedule-valid` moves just before Phase 2 dispatch — see below.

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
  --check schema-valid,fixture-valid,execution-safe,review-safe \
  --plan-file <absolute plan> --json
```

Halt on any `status: fail`, emitting the gate's `reason` verbatim and logging `run_end reason=preflight_gates_failed`. `fixture-valid` may legitimately fail against the pre-TASK-006 sample fixture — if the plan under execution is the sample itself (it will not be in production runs), demote to warning; for any other plan, halt as usual.

Then acquire the run-lock:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" acquire-lock --plan-file <absolute plan> --run-id <id>
```

Overlap on the same plan → halt with the conflicting run_id.

The lock file at `docs/plans/_run_lock.json` follows a strict canonical shape: a JSON object keyed by absolute plan path, with each entry containing exactly `{"run_id": "<id>", "acquired_at": "<opaque non-empty string>"}`. Any other shape (invalid JSON, extra keys, missing keys, top-level not an object, non-string or empty values) is rejected by `acquire-lock`. `--force` is a manual recovery tool — use it only to recover from a corrupted or stuck lock file. `--force` discards all existing entries (including entries for other plans), so do not run it while a legitimate run is in progress. `--run-id` must be a non-empty string; empty or non-string values are rejected before any file operation so a botched invocation cannot corrupt the lock file.

Append `run_start` via `plan_ops.py log-event`.

## Analysis (Phase 1)

Dispatch `plan-analyst` (Agent, `subagent_type: "plan-analyst"`, `model: "opus"`) with the Phase A template from `dispatch-templates.md`. Extract the fenced ```json block from the analyst's report and feed it to:

```bash
echo "<analyst_json>" | venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-schedule --stdin --json
```

Branch on `outcome`:
- `invalid` → halt; surface report; log `run_end reason=analyst_invalid`; release lock.
- `needs-enrichment` + no `--allow-gaps` → halt with gaps listed.
- `needs-enrichment` + `--allow-gaps` → warn + proceed.
- `valid` → proceed.

Apply filters:
- `--claude-only` or `codex_available=false` → rewrite `tasks[].agent = "claude"` in the in-memory schedule (persist to a scratch copy on disk for `batch-next`).
- `--codex-only` → drop claude tasks.
- `--task-ids` → pipe the analyst schedule through `filter-schedule | write-schedule` so the exact requested IDs and persistence happen in one shell pipeline (no inline Python):
  ```bash
  venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" filter-schedule \
    --schedule-file <schedule_file> --task-ids <csv> --json \
  | venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" write-schedule \
    --schedule-file <schedule_file> --stdin --json
  ```
  `filter-schedule` emits the requested IDs plus their transitive prerequisites in source order. Unknown requested ID halts with `unknown-task-id`; a transitive dep missing from `tasks[]` halts with `missing-dependency`; a cycle in the filtered subgraph halts with `dependency-cycle`.

After any filter rewrite, re-compute file-disjoint batches by piping the in-memory JSON through `venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" compute-schedule --stdin --json`, then replace the schedule's `batches` array with the returned `batches` before persisting.

Persist the final schedule (after filter rewrites and any required batch recomputation) by piping the in-memory JSON through `venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" write-schedule --schedule-file <schedule_file> --stdin --json`. This is the sole supported path for persistence; never write the file with the Write tool or inline Python (cf. rule at line 316). `write-schedule` runs the same shared validator as `parse-schedule` and refuses to write on any validation error.

Immediately after the schedule is persisted, run the `schedule-valid` phase gate so the dry-run / execute promotion bundle is complete (the Phase 0 preflight batch excluded this gate because the schedule file did not exist yet):

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
  --check schedule-valid --schedule-file <schedule_file> --json
```

Halt on `status: fail` with `run_end reason=schedule_gate_failed`. `write-schedule` already refuses to persist on validation errors, so this gate is a redundant belt-and-braces check that the gate model and the writer agree by construction.

### Phase 1.5 — Codex plan review (independent pre-dispatch gate)

The analyst (Claude/Opus) authored the plan *and* validated the schedule — the same family double-checking itself. Before any batch runs, dispatch Codex for an independent pre-dispatch review of the plan document + persisted schedule. Codex returns `approved | approved-with-notes | needs-replan`.

**Skip conditions** (take the first that applies):

- `--skip-plan-review` → log `plan_review_skipped {reason:"flag"}` and proceed. Final run summary MUST carry a loud banner: *"Plan review skipped via --skip-plan-review"*. This flag is parallel-safe with `--skip-cross-review` and works alongside `--dry-run`, `--codex-only`, `--claude-only`, and `--task-ids`.
- `codex_available=false` (from preflight) → log `plan_review_skipped {reason:"codex_unavailable"}` and proceed with a summary warning *"lacking independent plan review"*. This degrades to a warning rather than halting because plan review is a safety net, not a correctness gate.

Otherwise, proceed with the review:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_review_start \
  --fields-json '{"reviewer":"codex","plan_file":"<basename>"}' --json

venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" plan-review \
  --plan-file <absolute plan> \
  --schedule-file <schedule_file> \
  --plans-dir <dirname of plan-file> \
  --repo-root <absolute repo root> \
  --timeout 180 \
  [--allow-gaps]

```

The full wrapper envelope (produced by `plan_codex_dispatch.py plan-review`) has shape:

```json
{
  "task_id": "plan",
  "subcommand": "plan-review",
  "outcome": "success",
  "codex_exit_code": 0,
  "parsed": { "plan_file": "...", "verdict": "...", "findings": [...], "schedule_ok": true, "summary": "..." }
}
```

Pipe the entire envelope (not just `parsed`) into `parse-plan-review-report`:

```bash
printf '%s' "<envelope>" | venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" \
  parse-plan-review-report --stdin --json
```

`parse-plan-review-report` validates the envelope against `codex_plan_review_schema.json` and extracts `{plan_file, verdict, findings_count, findings, schedule_ok, summary}`. Cross-plan dependency resolution is verified by the orchestrator's Phase 0 `check-plan-deps` gate and is no longer surfaced by the reviewer. Schema violations halt with structured `errors[*]`. Wrapper timeout / parse_error / failure outcomes surface as `outcome ∈ {timeout, parse_error, failure}`; treat as `plan_review_skipped {reason:"codex_unavailable"}` for routing purposes — the pre-dispatch gate degrades on reviewer-side errors rather than blocking execution.

Append `plan_review_done {verdict, findings_count, summary}` and route by verdict:

| Verdict | Route |
|---|---|
| `approved` | Proceed to Phase 2 (batch dispatch). |
| `approved-with-notes` | Proceed to Phase 2. Carry `findings[]` into the final run summary under a *"Plan review notes"* section. Do not gate execution on notes. |
| `needs-replan` | Dispatch `plan-author` (if auto-revise on), then re-validate via `plan-analyst`, then re-run Codex `plan-review`. Second `needs-replan` halts. |

**`--allow-gaps` severity-aware demotion (TASK-003).** When the orchestrator was invoked with `--allow-gaps`, forward the flag to the wrapper by appending `--allow-gaps` to the `plan-review` command above. The wrapper inspects the persisted schedule and, **iff** `gaps[]` is non-empty AND every entry's `severity` is `"soft"` AND the schedule has no structural violations (`outcome == "needs-enrichment"` — `"valid"` by contract requires empty `gaps[]`, and missing/unknown outcomes suppress the demotion), injects a demotion clause into the Codex prompt. Codex then returns verdict `approved-with-notes` (with the demotion recorded in its `summary`) instead of `needs-replan`, which routes directly to Phase 2 and **explicitly short-circuits the `plan-author` auto-revise dispatch**. Any hard-severity gap (or a structural violation) suppresses the demotion clause — the reviewer applies the standard verdict vocabulary and `needs-replan` still dispatches `plan-author` per the routing table above. The wrapper never mutates the persisted schedule; the demotion is a pure function of the prompt inputs.

**`needs-replan` branch — auto-revise path (default).** Auto-revise is on unless `--no-auto-revise` is set. When on, the orchestrator runs a three-step author → analyst → review sequence before the second verdict is accepted:

1. **Dispatch `plan-author`** (Agent, `subagent_type: "plan-author"`, `model: "opus"`) using the Phase 1.5a template from `dispatch-templates.md`. Embed the plan path, the Codex findings array verbatim, the Codex summary verbatim, and the analyst annotations. The author edits the single plan file in place; its write scope is keyed on the input plan path, not a directory glob. Wrap the dispatch with `plan_author_start` before and `plan_author_done` after:

   ```bash
   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event plan_author_start \
     --fields-json '{"run_id":"<id>","plan_file":"<basename>","findings_count":<N>}' --json

   # Agent dispatch (plan-author, model: opus) — Phase 1.5a template.

   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event plan_author_done \
     --fields-json '{"run_id":"<id>","plan_file":"<basename>","files_edited":[...],"findings_actioned":[...],"findings_skipped":[...]}' --json
   ```

2. **Re-dispatch `plan-analyst`** (Phase A template) for structural re-validation of the revised plan file. The analyst reads the plan from disk — do NOT forward the author's edit report (that would invite ping-pong). Route on the re-validation outcome:

   - `invalid` → halt with `run_end reason=plan_review_failed reason_detail=author_introduced_structural_defect`. The author produced a structurally broken revision; do not run the second review against a malformed plan.
   - `needs-enrichment` → same allow-gaps routing as the first pass (halt if `--allow-gaps` is not set; warn and proceed otherwise).
   - `valid` → proceed to step 3.

3. **Re-run Codex `plan-review`** on the revised plan. The second verdict is binding: `approved | approved-with-notes` → proceed to batch dispatch; `needs-replan` → halt per the "Second `needs-replan`" block below.

**`needs-replan` branch — `--no-auto-revise` path (opt-out).** When `--no-auto-revise` is set, the first `needs-replan` verdict halts immediately without dispatching `plan-author`. This preserves the pre-TASK-025 behavior for users who prefer to apply revisions by hand; halt with `run_end reason=plan_review_failed` per the block below. No silent retry without revision.

**Second `needs-replan`** (after one author → analyst → review retry, on the auto-revise path), OR the first `needs-replan` when `--no-auto-revise` is set: halt before any batch runs.

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event run_end \
  --fields-json '{"run_id":"<id>","outcome":"failed","reason":"plan_review_failed","findings":[...]}' --json
```

Release the run-lock and print the failure envelope. Do NOT call `fail-task` (no task has started). Do NOT run batches. The run summary records `outcome=failed reason=plan_review_failed`.

**Retry failures** — if the `plan-author` dispatch itself fails (malformed report, out-of-scope writes) or the re-validation analyst returns `outcome=invalid`, halt with `run_end reason=plan_review_failed` same as the second-`needs-replan` path.

**Run-log event order** (V8): `run_start` → `analyst_done` → `schedule_written` → `plan_review_start {reviewer:"codex"}` → `plan_review_done {verdict, findings_count}` → (on `needs-replan` with auto-revise on) `plan_author_start` → `plan_author_done` → `analyst_done` → `plan_review_start` → `plan_review_done` → `batch_start` (only if verdict permits).

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
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" batch-next \
  --schedule-file <path> \
  --locked-files <comma-separated> \
  --done <comma-separated> \
  --failed <comma-separated> \
  --parallel N --json
```

Empty batch + non-empty ready → halt "scheduler stuck". Empty batch + empty ready → exit loop. Append `batch_start` event. Add the batch's files to `locked_files`.

### Phase B — Implement (parallel, one message)

Dispatch all batch tasks in a **single message** — Claude via Agent, Codex via Bash:

- Log `implement_start {task_id, agent, model?, batch_index}` per task (chain into the dispatch via `&&` when convenient).
- **Claude tasks** → `Agent(subagent_type: "plan-implementer", model: "opus", prompt: render(templates.PhaseB, ...))`.
- **Codex tasks** → `Bash: venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement --plan-file <abs> --task-id NNN --repo-root <abs> --timeout 300`.

Await all. For EVERY task (success or not) append `implement_done {task_id, outcome, files_changed[], test_outcome, wall_seconds}`.

**Classify per task:**

*Claude response (markdown):*

```bash
printf '%s' "<agent_output>" | venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-implementer-report --stdin --json
```

Gets `{outcome, files_changed, diff_summary, test_outcome, concerns, plan_adaptations, warnings, diagnostics, reversion_guidance?}`. `concerns` and `plan_adaptations` are `list[str]` (one entry per bullet); `warnings` is `list[str]` describing any legacy-alias label fallbacks (e.g., `**Concerns:**` instead of the canonical `**Concerns for reviewer:**`). `diagnostics` is a `list[dict]` of `{code, message}` entries flagging absent mandatory section headers (e.g. missing `**Plan adaptations:**`). Non-halting — surface to the reviewer for visibility; do not gate on it. Post-hoc scope check:

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

At the batch join barrier (after every wrapper in a batch returns, before per-task review/commit dispatch), the orchestrator reconciles observed out-of-scope writes via `plan_ops.py reconcile-batch --repo-root <repo> < envelopes.json`. Any `reconciliation_failed` result is a hard halt — do NOT advance to the next batch or dispatch review for any task in the affected batch.

### Phase C — Handle Phase B failures

For each non-success task:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" fail-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <touched files> --stage implement --reason "<short>" \
  [--reversion-guidance "<from implementer report>"] --json
```

This: (1) `git restore <files>` (Claude-side recovery; Codex-side restore was done inside the wrapper), (2) plan-status flip to `failed`, (3) run-log `failed {stage=implement, ...}` append.

Then cascade `blocked` onto the failed task's transitive dependents (source-of-truth invariant: the plan file, not just the run-log):

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" block-dependents \
  --schedule-file <path> --plan-file <abs> --failed NNN --run-id <id> --json
```

Release this task's file locks. Remove the task from `ready`. Peer tasks in the same and later batches proceed independently. Do NOT proceed to Phase D for this task.

### Phase D — Review + commit (serial per task, analyst batch order)

**If `--skip-cross-review`:** skip to D.3 immediately. Log `review_skipped`. Final summary shows a loud warning banner.

Otherwise, per successful task:

#### D.1 — Dispatch the opposite-side reviewer

*Claude-implemented → Codex review:*

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" review \
  --plan-file <abs> --task-id NNN --repo-root <abs> \
  --files <files_changed> --review-focus bugs --timeout 180
```

Parse `parsed.verdict ∈ {clean, minor-findings, needs-rework}`.

*Codex-implemented → Claude review:*

`Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD_Claude, ...))`.

Parse verdict `∈ {ship, ship-with-fixes, needs-rework}`. Verdict vocab is preserved verbatim — not normalized across reviewer types.

Log `review_start` then `review_done {task_id, reviewer, verdict, findings_count, minor_findings[]?, disagreement_tag?}`. Minor findings persist in `review_notes[task_id]` for the run summary.

When findings are non-empty, also pass `--findings-json "$(<json-array>)"` to the `review_done` `log-event` call so the line carries the full Codex payload verbatim under key `findings`. The audit trail depends on this — count-only `review_done` entries lose the finding text within ~20s.

#### D.2 — Route by verdict

| Implementer | Reviewer | clean / minor-findings (or ship / ship-with-fixes) | needs-rework |
|---|---|---|---|
| Claude | Codex | → D.3 commit | D.2a escalate (unless `--codex-review-binding`) |
| Codex | Claude | → D.3 commit | D.2b role-swap retry |

Minor findings in either direction → commit; record in run summary AND commit body tail. Never silently dropped.

#### D.2a — Escalation (§8.4, Codex critical on Claude work)

1. Log `disagreement {task_id, codex_findings[]}`. Pass the Codex findings verbatim via `--findings-json "$(<json-array>)"` so the `disagreement` line carries the full payload under key `findings` — D.5 dispatch happens right after, and audit retrieval of "what did Codex flag that D.5 then adjudicated?" depends on this.
2. Dispatch the Phase D.5 template: `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD5, codex_findings, task_block))`.
3. Parse verdict and route per the table below:

| Codex verdict | D.5 verdict | Route | Rationale |
|---|---|---|---|
| `needs-rework` | `ship` \| `ship-with-fixes` | → D.3 with `--disagreement-tag` (existing behavior, unchanged) | D.5 disagreed with Codex; commit wins. Summary row shows `[disagreement]`. |
| `needs-rework` | `partial-agreement` | → **D.2a.6** narrow-remediation retry | D.5 split Codex's findings into load-bearing and dismissed buckets; retry is scoped to the load-bearing subset only. Dismissed indices are recorded in the commit trailer. |
| `needs-rework` | `needs-rework` | → **D.2a.5** bounded remediation retry | Two independent reviewers agree the finding is load-bearing; give the implementer one chance to fix it narrowly. |

`--codex-review-binding` skips D.2a entirely — binding mode means `needs-rework` → immediate `fail-task` with NO D.5, NO D.2a.5, and NO D.2a.6.

#### D.2a.5 — Bounded remediation retry (default, non-binding path only)

Fires when Codex's `needs-rework` is independently confirmed by the D.5 code-reviewer. Strictly one attempt.

1. Log `remediation_start {task_id, findings_count, d5_summary}`.
2. Re-dispatch `plan-implementer` (Agent, `subagent_type: "plan-implementer"`, `model: "opus"`) using the **Phase B-rework** template from `dispatch-templates.md`. The template embeds `codex_findings_json` + `d5_summary` as a structured block and explicitly instructs the implementer to "fix narrowly, do not scope-inflate".
3. Classify the retry with the standard Phase B rules. `outcome ≠ success` → halt per step 6 below (same awaiting-user pause path; do NOT call `fail-task`).
4. On retry success, re-run D.1 (Codex review). The re-review is binding — no further retry regardless of verdict.
5. Route the re-review:
   - `clean | minor-findings` → D.3 commit with `--remediation-tag`. Summary row shows `[remediation]` (and `[disagreement]` if both apply).
   - `needs-rework` (second failure) → proceed to step 6.
6. **Awaiting-user pause** (second `needs-rework`, OR a failed retry implementer outcome):
   - Payload shape depends on which branch triggered the pause:
     - Second-review failure: `stage:"post_remediation_review"`, include `codex_findings:[...]` and `d5_summary:"..."`.
     - Retry-implement failure: `stage:"post_remediation_implement"`, include `retry_outcome`, `diagnostics`, and `reversion_guidance` from the implementer report; omit `codex_findings` (no second review ran).
   - `--ending-sha <sha>` MUST be `git rev-parse HEAD` at pause time — not the starting SHA. A paused run has uncommitted remediation edits in the working tree; the ending SHA captures the last committed state (which is typically the prior task's commit or the run's starting SHA if this is the first task). Log the paths of currently-dirty files in the `awaiting_user` event's `dirty_files` field so the next turn has a concrete handoff.
   ```bash
   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event awaiting_user \
     --fields-json '{"task_id":"NNN","stage":"post_remediation_review","codex_findings":[...],"d5_summary":"...","dirty_files":[...]}' --json

   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" finalize-execution-log \
     --run-id <id> --starting-sha <sha> --ending-sha "$(git rev-parse HEAD)" \
     --outcome paused --rows-json '[...]' --json

   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event run_end \
     --fields-json '{"run_id":"<id>","outcome":"paused","done":N,"failed":M,"paused_on_task":"NNN"}' --json
   ```
   After the paused `run_end`, the End-of-run sequence (update-plan-header, regular finalize, housekeeping commit) is SKIPPED — the paused branch emits its own `finalize-execution-log --outcome paused` and `run_end outcome=paused` instead. Print the failure envelope, release the run-lock, and return control to the user with pending edits **still in the working tree**. Do NOT call `fail-task`. Do NOT `git restore`. The user's next conversation turn decides disposition:
   - "revert" → user instructs orchestrator to run `fail-task`.
   - "keep as-is" → user instructs orchestrator to run `commit-task` with `--remediation-tag` and an override rationale.
   - "hand-fix" → user edits manually + re-runs review.

   **Hard rule:** D.2a.5's second `needs-rework` MUST NOT trigger `fail-task` automatically. `fail-task` on a paused run requires an explicit user instruction in the next turn. See the "Never auto-`fail-task` on the D.2a.5 halt path" rule below.

#### D.2a.6 — Narrow-remediation retry (partial-agreement path)

Fires when Codex's `needs-rework` verdict is split by the D.5 third-opinion reviewer into load-bearing + dismissed buckets (`partial-agreement`). Strictly one attempt — same bounding as D.2a.5. `--codex-review-binding` skips this entire section: binding mode means `needs-rework` → immediate `fail-task` with NO D.5, NO D.2a.5, and NO D.2a.6.

1. Log `narrow_remediation_start {task_id, load_bearing_count, dismissed_count, d5_summary}`. The `narrow_remediation_start` / `narrow_remediation_done` events are distinct from D.2a.5's `remediation_start` / `remediation_done`; the run log is the audit source of truth for which retry path fired.
2. Re-dispatch `plan-remediator` (Agent, `subagent_type: "plan-remediator"`, `model: "opus"`) using the **Phase B-narrow-remediation** template from `dispatch-templates.md`. The template embeds `load_bearing_findings_json` (filtered subset of Codex findings where the array index ∈ D.5's `load_bearing`), `dismissed_findings_json` (the complement, labeled "DO NOT fix — context only"), and `d5_summary`. The file:line touch-only scope rule in `plan-remediator.md` structurally bounds the retry; "fix narrowly, do not scope-inflate" remains a prompt-level hint.
3. Classify the retry with the standard Phase B rules plus the new `scope-violation` outcome. `outcome ≠ success` (including `scope-violation`) → halt per step 6 below (same awaiting-user pause path; do NOT call `fail-task`).
4. On retry success, re-run D.1 (Codex review). The re-review is binding — no further retry regardless of verdict.
5. Route the re-review:
   - `clean | minor-findings` → D.3 commit with `--narrow-remediation-tag --dismissed-finding-ids I,J,K` (comma-separated dismissed indices from the D.5 split). Commit body carries `[narrow-remediation]` followed immediately by `[disagreement: I,J,K]`; summary row shows both tags.
   - `needs-rework` (second failure) → proceed to step 6.
6. Log `narrow_remediation_done {task_id, outcome}` before entering the pause (on both the retry-implement-failure and second-review-failure branches, so the log records the attempt's terminal state either way).
7. **Awaiting-user pause** (second `needs-rework`, OR a failed/scope-violation retry implementer outcome):
   - Payload shape mirrors D.2a.5 with a flipped `stage` label and one added field on the review-failure branch:
     - Second-review failure: `stage:"post_narrow_remediation_review"`, include `codex_findings:[...]`, `d5_summary:"..."`, and `dismissed_finding_indices:[...]` (for round-tripping the D.5 split into the next turn).
     - Retry-implement failure: `stage:"post_narrow_remediation_implement"`, include `retry_outcome`, `diagnostics`, and `reversion_guidance` from the implementer report; omit `codex_findings` (no second review ran).
   - `--ending-sha <sha>` MUST be `git rev-parse HEAD` at pause time, same as D.2a.5. Log currently-dirty file paths in the `awaiting_user` event's `dirty_files` field so the next turn has a concrete handoff.
   ```bash
   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event awaiting_user \
     --fields-json '{"task_id":"NNN","stage":"post_narrow_remediation_review","codex_findings":[...],"d5_summary":"...","dismissed_finding_indices":[...],"dirty_files":[...]}' --json

   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" finalize-execution-log \
     --run-id <id> --starting-sha <sha> --ending-sha "$(git rev-parse HEAD)" \
     --outcome paused --rows-json '[...]' --json

   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
     --event run_end \
     --fields-json '{"run_id":"<id>","outcome":"paused","done":N,"failed":M,"paused_on_task":"NNN"}' --json
   ```
   After the paused `run_end`, the End-of-run sequence (update-plan-header, regular finalize, housekeeping commit) is SKIPPED — the paused branch emits its own `finalize-execution-log --outcome paused` and `run_end outcome=paused` instead. Print the failure envelope, release the run-lock, and return control to the user with pending edits **still in the working tree**. Do NOT call `fail-task`. Do NOT `git restore`. The user's next conversation turn decides disposition (same three options as D.2a.5: "revert" / "keep as-is" / "hand-fix").

   **Hard rule:** D.2a.6's second `needs-rework` or non-success retry MUST NOT trigger `fail-task` automatically. Same protocol as D.2a.5's halt path — the "Never auto-`fail-task` on the D.2a.5 halt path" rule below extends to D.2a.6.

#### D.2b — Role-swap retry (Codex implements + Claude reviewer needs-rework)

Per §8.3 line 692: Claude re-implements, Codex re-reviews. One attempt.

1. Re-dispatch Phase B template to `plan-implementer` (Agent, `model: "opus"`). Reviewer feedback is NOT forwarded in v1.
2. Classify the retry with the same Phase B rules. `outcome ≠ success` → D.4 reason `retry_implement_failed`.
3. On retry success, re-run D.1 using the **Codex** reviewer. Binding — no further retry.
4. Route re-review: `clean | minor-findings` → D.3. `needs-rework` → D.4.

#### D.3 — Commit

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" commit-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <files_changed> --title "<task.title>" \
  --diff-summary "<implementer.diff_summary>" \
  --reviewer <codex|claude|none> \
  --reviewer-verdict "<verdict>" \
  --reviewer-minor-findings '<json array>' \
  [--disagreement-tag] [--remediation-tag] \
  [--narrow-remediation-tag --dismissed-finding-ids I,J,K] --json
```

`--remediation-tag` appends a `[remediation]` line to the commit body; set it only when the commit follows a successful D.2a.5 retry. `--narrow-remediation-tag` (with a non-empty `--dismissed-finding-ids` list) appends `[narrow-remediation]` + `[disagreement: I,J,K]` on adjacent lines; set it only when the commit follows a successful D.2a.6 retry. Argparse enforces four constraints: (a) `--narrow-remediation-tag` XOR `--remediation-tag`, (b) `--dismissed-finding-ids` XOR `--disagreement-tag`, (c) `--dismissed-finding-ids` requires `--narrow-remediation-tag`, (d) `--narrow-remediation-tag` requires non-empty `--dismissed-finding-ids`.

This: (1) guard check for unexpected staged overlap, (2) plan-status flip to `done`, (3) `git commit --only <files> <plan-file> -m "feat(TASK-NNN): <title>\n\n<diff summary>\n\nPlan: <basename>"`, (4) SHA capture, (5) run-log `commit_done` append.

**After a D.2a disagreement (Codex `needs-rework` → D.5 `ship` | `ship-with-fixes`):** pass `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` (NOT `--reviewer codex --reviewer-verdict needs-rework` — that payload is rejected with `uncommittable-reviewer-verdict` because the D.5 third-opinion verdict is binding, not Codex's). Record Codex's original findings verbatim in `--reviewer-minor-findings`; set each dismissed finding's optional `disposition: "dismissed"` (with an optional `disposition_reason`) so the commit preserves the adjudication trail. For the D.2b role-swap path the same rule applies with the roles inverted: pass `--reviewer codex --reviewer-verdict clean|minor-findings --disagreement-tag`. D.2a.5 (bounded remediation) and D.2a.6 (narrow remediation) use `--remediation-tag` or `--narrow-remediation-tag --dismissed-finding-ids ...` respectively; the binding reviewer is always the one whose verdict satisfied the commit-allowed set (`ship` / `ship-with-fixes` for `--reviewer claude`, `clean` / `minor-findings` for `--reviewer codex`).

Commit hook failure → subcommand auto-rolls back (`git reset HEAD`, restore plan text) and exits non-zero → treat as D.4 `stage=commit`.

**Post-commit `commit-safe` gate.** On a successful `commit-task`, capture the returned `commit_sha` and verify that the commit touched only TASK-NNN's declared `Files:` list (plus the always-ignore set + the plan file itself):

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
  --check commit-safe \
  --plan-file <absolute plan> \
  --task-id NNN --commit-sha <sha> --json
```

`status: fail` here means the narrow-commit seam leaked — log `commit_safe_gate_failed {task_id, commit_sha, reason}` and halt before moving to the next batch. This is the post-hoc counterpart to `commit-task`'s pre-commit guard; the two together close the execute-bundle loop.

**Lint reference:** `plan_ops.py lint-plans --plans-dir docs/plans --run-log <run_log> --git-dir . --json` cross-references every `**Status:** done` (or `partial`) task against the run log and git history. A `done`/`partial` marker without a matching `commit_done` event AND `feat(TASK-NNN)` commit flags the task as a hand-edit. Run manually during review or before shipping a plan; the PR-gate wiring is a follow-up (TASK-020C).

#### D.4 — Phase D fail

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" fail-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <files> --stage review --reason "..." \
  --reviewer-findings '<json>' --json
```

Then cascade `blocked` onto transitive dependents (same contract as Phase C):

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" block-dependents \
  --schedule-file <path> --plan-file <abs> --failed NNN --run-id <id> --json
```

Same atomic shape as Phase C.

### Phase E — Next batch

Release this task's file locks. Loop to Phase A.

## End of run

1. `plan_ops.py update-plan-header --status <complete|partial>` (complete iff `failed == 0`; else partial).
2. `plan_ops.py finalize-execution-log --run-id <id> --starting-sha <sha> --ending-sha <sha> --outcome <success|partial|failed|paused> --rows-json '[...]'` — build the §5 table. `--rows-json` row schema: each row is an object with exactly these six required string keys — `task`, `agent`, `reviewer`, `verdict`, `commit`, `notes` (no extras; values must all be strings). Verdict cells should include any `[disagreement]` / `[remediation]` / `[narrow-remediation]` markers in prose. Missing or unknown keys exit 1 with the full allowed-field list in the error message. Use `--outcome paused` when exiting via the D.2a.5 OR D.2a.6 awaiting-user path; `success`/`partial`/`failed` otherwise per the usual done/failed accounting.
3. Log `run_end` event (counts `{done, failed}` + disagreement_count + minor_findings_total; include `outcome=paused` when halting via D.2a.5 or D.2a.6).
4. Print summary: counts `{done, failed}`, failures with reasons, disagreement-tagged commits, per-task minor-findings digest (from `review_notes`), `git log --oneline <starting_sha>..HEAD` hint.
5. Housekeeping commit (skip if `done == 0 AND failed == 0`):
   ```bash
   git add <plan-file> <run_log>
   git commit -m "chore(implement-plan): run <run_id> bookkeeping"
   ```
6. **Certify the execute bundle.** Before releasing the lock and after the housekeeping commit (if any), run the full phase-gate bundle in execute mode so the run's pass/fail determination is recorded in the run log and visible to downstream tooling:

   ```bash
   venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" gates \
     --certify --mode execute \
     --plan-file <absolute plan> \
     --schedule-file <schedule_file> \
     --run-id <id> --json
   ```

   The bundle re-checks `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, and re-verifies `commit-safe` against every `commit_done` event for this run. A run with zero commits reports `commit-safe: not_applicable` — that is not a failure. `certified: false` in the output means a gate failed; emit `certify_failed {run_id, gates: {...}}` via `log-event` and surface the failing gate name in the final summary, but do NOT retroactively reopen already-committed tasks. The certify step is a report, not a retry trigger.

7. `plan_ops.py release-lock` (finally-style; runs on early halt too).

Do NOT auto-push. Do NOT auto-PR.

## Rules

- **Never edit code files.** Orchestrator only touches plan files, `_run_log.jsonl`, `_run_lock.json`, and git staging. Implementer subagents / Codex wrapper own code changes.
- **Never commit a reviewer-flagged `needs-rework`.** Only clean / minor-findings / ship / ship-with-fixes commit automatically.
- **Never `git add -A` or `git add .`.** Stage specific files only — `commit-task` already uses `--only`.
- **Never retry a failed task inside the same run** beyond the one D.2b role-swap, the one D.2a.5 bounded remediation retry, the one D.2a.6 narrow-remediation retry, and the one Codex→Claude fallback. Terminal failures stay isolated — peers continue independently.
- **Never auto-`fail-task` on the D.2a.5 halt path.** A second `needs-rework` after a D.2a.5 remediation retry triggers `log-event type=awaiting_user` + `finalize-execution-log --outcome paused` and returns control to the user with pending edits left in the working tree. Calling `fail-task` on the paused run is allowed ONLY when the user's next conversation turn explicitly instructs it. Silent auto-revert on the post-remediation `needs-rework` path is a protocol violation.
- **Never modify plan-file body except `**Status:**` bullets and the tail execution-log section.** Append-only on the log.
- **One commit per task** plus at most one `chore:` housekeeping commit per run. Narrow `git commit --only` in Phase D.3 is mandatory.
- **Never auto-push, never auto-PR.**
- **`venv/bin/python`** for all Python invocations.
- **Every run-log append is verified** via `plan_ops.py log-event`'s tail re-verify (or via `commit-task` / `fail-task` which fsync + re-read internally).
- **Never write inline Python for plan ops.** Use `plan_ops.py`. Inline `python3 -c` scripts are a protocol violation.
- **Dispatch prompts must be self-contained.** Subagents do not see this conversation. Embed the full task block verbatim.

## Cleanup policy (wrapper-enforced, for reference)

The Codex dispatch wrapper operates **delta-bounded cleanup** against a pre-dispatch baseline. It never runs `git checkout -- .` or `git clean -fd`, so disjoint sibling work is not destroyed. Protected paths are never touched by cleanup; they land in `extra.protected_skipped_tracked` / `extra.protected_skipped_untracked` for observability.

- **Protected exact paths:** `_run_lock.json`, `.claude`, `.codex`
- **Protected prefixes:** `<plan_dir>/_run_log.jsonl`, `<plan_dir>/_run_lock.json`, `.claude/`, `.codex/`
- **Delta invariant:** cleanup only touches `(allowed_files ∪ new-delta-violations) − protected`. Files present in the baseline are never deleted or restored.
- **Scope misreport:** if Codex's `files_changed` disagrees with the post-dispatch delta, the wrapper emits `outcome="failure"` with `extra.reason="scope_misreport"` and `extra.test_result.result="not_run"`; the test command is skipped but delta-only restore still runs.
- **Review-path:** keeps "log but succeed" semantics by explicit design; a post-dispatch sandbox escape surfaces in `extra.sandbox_escape_detected` without changing outcome.
