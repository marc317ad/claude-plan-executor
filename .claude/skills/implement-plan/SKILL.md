---
name: "implement-plan"
description: "Dual-agent plan executor. Dispatches Claude-tier tasks via plan-implementer and Codex-tier tasks via plan_codex_dispatch.py wrapper, with per-batch cross-review. Interleaves implement -> review -> commit inside each batch to keep review diffs clean."
user_invocable: true
---

# Implement Plan

**Arguments:** $ARGUMENTS

## Dispatch rules (read before any subagent call)

1. **Parallel dispatch per batch.** Up to `--parallel N` dispatches in a SINGLE message inside Phase B. Claude-tier via Agent, Codex-tier via Bash — both kick off in the same message when a batch contains both.
2. **Trust the analyst's schedule.** Batches with disjoint `file_locks` and no cross-batch deps are parallel-safe. Do not add extra safety reasoning.
3. **Your job is routing only.** Pick tasks from the analyst's schedule, dispatch, interpret reports, commit/revert. No code reading, no diff judgment, no scope inflation.
4. **Dispatch prompts must be self-contained.** Read `.claude/skills/implement-plan/dispatch-templates.md` for the templates. Every Agent prompt includes the full task block verbatim + "You do NOT have the Agent tool."
5. **Timeouts on Bash calls.** 5000ms for idioms (printf, git status); 180000ms (180s) for `plan_codex_dispatch.py review`; 300000ms (300s) for `plan_codex_dispatch.py implement`. Timeouts are enforced by the wrapper internally — pass `--timeout 180|300` as documented.
6. **Subagent errors.** If a dispatch returns `[Tool result missing due to internal error]` or no parseable report, treat as failure. Log it, restore partial changes, do NOT retry silently. Claude-implementer `malformed` outcome goes to `fail-task stage=implement reason=malformed_report`.
7. **Cross-review asymmetry.** Claude implements → Codex reviews; Codex implements → Claude reviews. Escalation path differs by direction — see Phase D.2.

## Bash command idioms

Standard commands used by this skill:

- `venv/bin/python scripts/plan_ops.py <subcommand> [--json]` — all plan parsing, schedule evaluation, batch selection, narrow commit, failure handling, status transitions, and run-log append verification.
- `venv/bin/python scripts/plan_codex_dispatch.py implement|review ...` — Codex-tier implementer and reviewer. Emits a single JSON envelope on stdout.
- `date -u +%Y%m%dT%H%M%S` — run_id fallback (preflight emits one authoritatively).

Never write inline Python for plan operations. Never `git stash` inside this skill — the wrapper restores Codex-side independently; the orchestrator restores Claude-side via `plan_ops.py fail-task` (which uses `git restore`).

## plan_ops.py CLI reference

All subcommands accept `--json` for machine-readable output.

| Command | Purpose |
|---|---|
| `plan_ops.py preflight --plan-file <abs> [--strict-branch]` | Smart dirty-tree + codex probe + starting_sha + run_id + base-branch check |
| `plan_ops.py parse-schedule --stdin [--strict]` | Validate analyst JSON; surface errors, tasks, batches, gaps, risks. `--strict` promotes unknown nested fields from warning to error. |
| `plan_ops.py compute-schedule --stdin [--strict]` | Recompute topo order + file-disjoint batches from `tasks[]`; use after any filter rewrite before persisting the schedule. |
| `plan_ops.py write-schedule --schedule-file <path> --stdin [--strict]` | Validate + atomically persist schedule JSON. Refuses to write on any validation error. |
| `plan_ops.py batch-next --schedule-file ... --locked-files ... --done ... --failed ... --parallel N` | Pick next batch respecting file locks + deps; flags `scheduler_stuck` |
| `plan_ops.py parse-implementer-report --stdin` | Extract outcome / files_changed / diff_summary / test_outcome / concerns / plan_adaptations / reversion_guidance / warnings / **diagnostics** from the plan-implementer markdown report. `concerns` and `plan_adaptations` are lists of strings (one bullet each). |
| `plan_ops.py commit-task ...` | Full D.3: guard, plan-status mutate (→ done), narrow `git commit --only`, SHA capture, run-log `commit_done` append |
| `plan_ops.py fail-task --stage implement\|review\|commit ...` | Full Phase C / D.4: git restore (if files), plan-status mutate (→ failed), run-log `failed` append |
| `plan_ops.py block-dependents --schedule-file ... --failed NNN --run-id RID` | Transitive cascade-block; logs `blocked` events per dependent |
| `plan_ops.py update-plan-header --status in-progress\|complete\|partial` | Mutate the plan-file top-level `**Status:**` |
| `plan_ops.py finalize-execution-log ...` | Append §5 execution-log markdown table to plan |
| `plan_ops.py log-event --event E --fields-json '{...}'` | Append JSONL event with tail re-verify |
| `plan_ops.py normalize-task-id --id 1\|001\|TASK-001\|004A\|TASK-004A` | Canonicalize to `^\d{3}[A-Z]?$` form |
| `plan_ops.py acquire-lock / release-lock --plan-file ... --run-id ...` | Per-plan-file run-lock against `docs/plans/_run_lock.json` |
| `plan_ops.py check-plan-deps --plan-file ... --plans-dir ...` | Resolve the target plan's cross-plan `Dependencies:` entries against `<plans-dir>/00_INDEX.md` + sibling `- **Status:**` bullets; emits `{pass, deps, unresolved, errors}` |

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
  --task-ids 1,2,3        Restrict; halt if filter breaks deps
  --skip-cross-review     Commit without review (loud banner in summary)
  --codex-review-binding  Codex critical on Claude goes straight to fail-task;
                          no §8.4 third-opinion escalation
  --allow-gaps            Proceed past analyst outcome=needs-enrichment
  --strict-branch         Halt (not warn) if current branch != plan's Base branch
```

Mutual exclusions: `--codex-only` + `--claude-only` → error. Normalize `--task-ids` values via `plan_ops.py normalize-task-id` before filtering.

## Pre-flight (Phase 0)

```bash
venv/bin/python scripts/plan_ops.py preflight --plan-file <absolute plan> [--strict-branch]
```

Returns JSON with `pass`, `starting_sha`, `run_id`, `codex_available`, `dirty_files{source_blocking, infra_ignored, plan_doc}`, `base_branch_match`. Halts on `source_blocking` dirty. `plan_doc` churn (the plan file being executed) is allowed. `infra_ignored` warns but proceeds.

If `codex_available=false`, override `tasks[].agent = "claude"` throughout Phase 1 and warn; wrapper's own "codex binary not found on PATH" branch is the backstop.

Then acquire the run-lock:

```bash
venv/bin/python scripts/plan_ops.py acquire-lock --plan-file <absolute plan> --run-id <id>
```

Overlap on the same plan → halt with the conflicting run_id.

Append `run_start` via `plan_ops.py log-event`.

## Analysis (Phase 1)

Dispatch `plan-analyst` (Agent, `subagent_type: "plan-analyst"`, `model: "opus"`) with the Phase A template from `dispatch-templates.md`. Extract the fenced ```json block from the analyst's report and feed it to:

```bash
echo "<analyst_json>" | venv/bin/python scripts/plan_ops.py parse-schedule --stdin --json
```

Branch on `outcome`:
- `invalid` → halt; surface report; log `run_end reason=analyst_invalid`; release lock.
- `needs-enrichment` AND every gap has `type == "external-dep"` → invoke `scripts/plan_ops.py check-plan-deps` to resolve against the sibling-plan manifest (see "Cross-plan dependency resolution" below). On `pass: true`, upgrade the outcome to `valid`, append a `cross_plan_resolved` event, and proceed. On `pass: false`, halt with the named blockers from `unresolved[]` (no `--allow-gaps` override — unresolved cross-plan deps are hard blockers).
- `needs-enrichment` with any non-`external-dep` gap + no `--allow-gaps` → halt with gaps listed.
- `needs-enrichment` with any non-`external-dep` gap + `--allow-gaps` → warn + proceed.
- `valid` → proceed.

### Cross-plan dependency resolution

When every gap has `type == "external-dep"`, run:

```bash
venv/bin/python scripts/plan_ops.py check-plan-deps \
  --plan-file <absolute plan> \
  --plans-dir <directory containing 00_INDEX.md + sibling plans> \
  --json
```

`--plans-dir` is the directory of the target plan — the subcommand expects `00_INDEX.md` there alongside the sibling plans referenced by the roster. Output:

```json
{
  "pass": true|false,
  "deps": [{"task_id": "001", "plan_file": "TASK-001_*.md", "status": "done"}, ...],
  "unresolved": [{"task_id": "002", "reason": "dep-not-done|unresolved-dep|file-not-found", ...}],
  "errors": []
}
```

Result-shape contract: `errors[]` is non-empty only for malformed inputs (missing plan file, missing `00_INDEX.md`, roster table not found) — the subcommand exits non-zero in those cases and the orchestrator halts with an internal-error message. `unresolved[]` carries successfully parsed but not-done sibling deps — exit code is zero, `pass` is `false`, and the orchestrator halts with the blocker list. When `pass: true`, append `cross_plan_resolved {resolved_deps: [...], run_id, plan_file}` via `log-event` and treat the analyst outcome as `valid` for the rest of Phase 1.

Apply filters:
- `--claude-only` or `codex_available=false` → rewrite `tasks[].agent = "claude"` in the in-memory schedule (persist to a scratch copy on disk for `batch-next`).
- `--codex-only` → drop claude tasks; halt if orphans deps.
- `--task-ids` → restrict; halt if filter orphans deps.

After any filter rewrite, re-compute schedule topology and batches by piping the in-memory JSON through `venv/bin/python scripts/plan_ops.py compute-schedule --stdin --json`, then replace the schedule's `batches` array with the returned `batches` before persisting. Treat this as the sole supported path for batch recomputation; never use inline Python for plan ops.

Persist the final schedule (after filter rewrites and any required batch recomputation) by piping the in-memory JSON through `venv/bin/python scripts/plan_ops.py write-schedule --schedule-file docs/plans/<basename>.schedule.json --stdin --json`. This is the sole supported path for persistence; never write the file with the Write tool or inline Python (cf. rule at line 316). `write-schedule` runs the same shared validator as `parse-schedule` and refuses to write on any validation error.

### Dry-run mode

If `--dry-run`: print the schedule + intended dispatches. Release lock. Exit. Dry-run scope is sticky — a follow-up "actually run it" message requires a fresh invocation without `--dry-run`, or explicit user instruction.

## Execute mode — per-batch A→E loop

```
ready          : topo order from analyst JSON
done           : set[task_id] = {}
failed         : set[task_id] = {}
blocked        : dict[task_id -> reason] = {}
locked_files   : set[str] = {}
committed      : list[(task_id, sha)]
review_notes   : dict[task_id -> list[minor findings]] = {}
```

Loop until `ready` is empty OR scheduler stuck.

### Phase A — Select batch

```bash
venv/bin/python scripts/plan_ops.py batch-next \
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
- **Codex tasks** → `Bash: venv/bin/python scripts/plan_codex_dispatch.py implement --plan-file <abs> --task-id NNN --repo-root <abs> --timeout 300`.

Await all. For EVERY task (success or not) append `implement_done {task_id, outcome, files_changed[], test_outcome, wall_seconds}`.

**Classify per task:**

*Claude response (markdown):*

```bash
printf '%s' "<agent_output>" | venv/bin/python scripts/plan_ops.py parse-implementer-report --stdin --json
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
venv/bin/python scripts/plan_ops.py fail-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <touched files> --stage implement --reason "<short>" \
  [--reversion-guidance "<from implementer report>"] --json
```

This: (1) `git restore <files>` (Claude-side recovery; Codex-side restore was done inside the wrapper), (2) plan-status flip to `failed`, (3) run-log `failed {stage=implement, ...}` append.

Then cascade:

```bash
venv/bin/python scripts/plan_ops.py block-dependents \
  --schedule-file <path> --failed NNN --run-id <id> --json
```

Remove blocked dependents from `ready`, add to `blocked`. Release this task's file locks. Do NOT proceed to Phase D for this task.

### Phase D — Review + commit (serial per task, topo order)

**If `--skip-cross-review`:** skip to D.3 immediately. Log `review_skipped`. Final summary shows a loud warning banner.

Otherwise, per successful task:

#### D.1 — Dispatch the opposite-side reviewer

*Claude-implemented → Codex review:*

```bash
venv/bin/python scripts/plan_codex_dispatch.py review \
  --plan-file <abs> --task-id NNN --repo-root <abs> \
  --files <files_changed> --review-focus bugs --timeout 180
```

Parse `parsed.verdict ∈ {clean, minor-findings, needs-rework}`.

*Codex-implemented → Claude review:*

`Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD_Claude, ...))`.

Parse verdict `∈ {ship, ship-with-fixes, needs-rework}`. Verdict vocab is preserved verbatim — not normalized across reviewer types.

Log `review_start` then `review_done {task_id, reviewer, verdict, findings_count, minor_findings[]?, disagreement_tag?}`. Minor findings persist in `review_notes[task_id]` for the run summary.

#### D.2 — Route by verdict

| Implementer | Reviewer | clean / minor-findings (or ship / ship-with-fixes) | needs-rework |
|---|---|---|---|
| Claude | Codex | → D.3 commit | D.2a escalate (unless `--codex-review-binding`) |
| Codex | Claude | → D.3 commit | D.2b role-swap retry |

Minor findings in either direction → commit; record in run summary AND commit body tail. Never silently dropped.

#### D.2a — Escalation (§8.4, Codex critical on Claude work)

1. Log `disagreement {task_id, codex_findings[]}`.
2. Dispatch the Phase D.5 template: `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD5, codex_findings, task_block))`.
3. Parse verdict:
   - `ship | ship-with-fixes` → code-reviewer disagreed → D.3 with `--disagreement-tag`. Summary row shows `[disagreement]`.
   - `needs-rework` → code-reviewer agreed → D.4 with reason `codex+code-reviewer: critical findings`.

`--codex-review-binding` skips D.2a entirely.

#### D.2b — Role-swap retry (Codex implements + Claude reviewer needs-rework)

Per §8.3 line 692: Claude re-implements, Codex re-reviews. One attempt.

1. Re-dispatch Phase B template to `plan-implementer` (Agent, `model: "opus"`). Reviewer feedback is NOT forwarded in v1.
2. Classify the retry with the same Phase B rules. `outcome ≠ success` → D.4 reason `retry_implement_failed`.
3. On retry success, re-run D.1 using the **Codex** reviewer. Binding — no further retry.
4. Route re-review: `clean | minor-findings` → D.3. `needs-rework` → D.4.

#### D.3 — Commit

```bash
venv/bin/python scripts/plan_ops.py commit-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <files_changed> --title "<task.title>" \
  --diff-summary "<implementer.diff_summary>" \
  --reviewer <codex|claude|none> \
  --reviewer-verdict "<verdict>" \
  --reviewer-minor-findings '<json array>' \
  [--disagreement-tag] --json
```

This: (1) guard check for unexpected staged overlap, (2) plan-status flip to `done`, (3) `git commit --only <files> <plan-file> -m "feat(TASK-NNN): <title>\n\n<diff summary>\n\nPlan: <basename>"`, (4) SHA capture, (5) run-log `commit_done` append.

Commit hook failure → subcommand auto-rolls back (`git reset HEAD`, restore plan text) and exits non-zero → treat as D.4 `stage=commit`.

#### D.4 — Phase D fail

```bash
venv/bin/python scripts/plan_ops.py fail-task \
  --plan-file <abs> --task-id NNN --run-id <id> \
  --files <files> --stage review --reason "..." \
  --reviewer-findings '<json>' --json
```

Same atomic shape as Phase C. Cascade-block dependents via `block-dependents`.

### Phase E — Unblock + next batch

For each newly committed task: release its file locks, check reverse dependents, push satisfied ones into `ready`, re-sort. Loop to Phase A.

## End of run

1. `plan_ops.py update-plan-header --status <complete|partial>` (complete iff `failed == 0 AND blocked == 0`; else partial).
2. `plan_ops.py finalize-execution-log --run-id <id> --starting-sha <sha> --ending-sha <sha> --rows-json '[...]' ` — build the §5 table.
3. Log `run_end` event (counts + disagreement_count + minor_findings_total).
4. Print summary: counts, failures with reasons, disagreement-tagged commits, per-task minor-findings digest (from `review_notes`), `git log --oneline <starting_sha>..HEAD` hint.
5. Housekeeping commit (skip if `done == 0 AND failed == 0`):
   ```bash
   git add <plan-file> docs/plans/_run_log.jsonl
   git commit -m "chore(implement-plan): run <run_id> bookkeeping"
   ```
6. `plan_ops.py release-lock` (finally-style; runs on early halt too).

Do NOT auto-push. Do NOT auto-PR.

## Rules

- **Never edit code files.** Orchestrator only touches plan files, `_run_log.jsonl`, `_run_lock.json`, and git staging. Implementer subagents / Codex wrapper own code changes.
- **Never commit a reviewer-flagged `needs-rework`.** Only clean / minor-findings / ship / ship-with-fixes commit automatically.
- **Never `git add -A` or `git add .`.** Stage specific files only — `commit-task` already uses `--only`.
- **Never retry a failed task inside the same run** beyond the one D.2b role-swap and the one Codex→Claude fallback. Terminal failures cascade-block.
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
- **Protected prefixes:** `docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `.claude/`, `.codex/`, `scripts/plan_ops.py`, `scripts/plan_codex_dispatch.py`
- **Delta invariant:** cleanup only touches `(allowed_files ∪ new-delta-violations) − protected`. Files present in the baseline are never deleted or restored.
- **Scope misreport:** if Codex's `files_changed` disagrees with the post-dispatch delta, the wrapper emits `outcome="failure"` with `extra.reason="scope_misreport"` and `extra.test_result.result="not_run"`; the test command is skipped but delta-only restore still runs.
- **Review-path:** keeps "log but succeed" semantics by explicit design; a post-dispatch sandbox escape surfaces in `extra.sandbox_escape_detected` without changing outcome.
