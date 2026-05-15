# Run Log Schema (`<run_log>`, default `docs/plans/_run_log.jsonl`)

Append-only JSONL. One JSON object per line. **Never rewrite — only append via `Bash` with `printf` and `>>`.** Every append is verified with `&& tail -1`; if tail does not contain the just-written line, retry once.

All events carry `ts` (ISO-8601 UTC, e.g. `2026-04-13T17:02:04Z`) and `run_id`.

| `event` | Required fields |
|---|---|
| `run_start` | `ts, run_id, plan_file, mode, flags, starting_sha, candidate_count` + optional `claude_only?, task_ids?` (`task_ids?` is a list of canonicalized ids when `--task-ids` was passed; never a scalar; never named `task_ids_filter`) |
| `run_end` | `ts, run_id, ending_sha, done_count, failed_count, disagreement_count, minor_findings_total` |
| `batch_start` | `ts, run_id, batch_index, task_ids, file_locks` |
| `implement_start` | `ts, run_id, task_id, agent, model?, batch_index` |
| `implement_done` | `ts, run_id, task_id, outcome, files_changed, test_outcome, wall_seconds` |
| `fallback_used` | `ts, run_id, task_id, from_agent, to_agent, reason` |
| `review_start` | `ts, run_id, task_id, reviewer, review_focus?` |
| `review_done` | `ts, run_id, task_id, reviewer, verdict, findings_count, minor_findings?, disagreement_tag?` |
| `disagreement` | `ts, run_id, task_id, codex_findings, code_reviewer_verdict` |
| `commit_done` | `ts, run_id, task_id, commit_sha, files, reviewer_verdict, minor_findings_count, disagreement_tag` |
| `failed` | `ts, run_id, task_id, stage, reason, reversion_guidance?, reviewer_findings?` |
| `review_skipped` | `ts, run_id, task_id, reason` |
| `plan_review_fallback_used` | `ts, run_id, plan_file, from, to, reason` |
| `review_fallback_used` | `ts, run_id, task_id, from, to, reason` |
| `review_fallback_failed` | `ts, run_id, task_id, from, to, reason` |
| `plan_author_start` | `ts, run_id, plan_file, findings_count` |
| `plan_author_done` | `ts, run_id, plan_file` + optional `files_edited[], findings_actioned[], findings_skipped[]` |

## Field definitions

- **`outcome`** (on `implement_done`): one of `success | partial | failed | plan-incorrect | malformed` for Claude-tier implementers; one of `success | failure | timeout | parse_error | scope_violation | dry_run` for Codex-tier (wrapper-emitted; see `scripts/plan_codex_dispatch.py`). The event fires on **every** implementer return, success or not.
- **`stage`** (on `failed`): `implement | review | commit`. A `failed` event fires *additionally* on non-success, paired with an `implement_done outcome≠success`, a `review_done verdict=needs-rework`, or a commit rollback.
- **`verdict`** (on `review_done`): one of `clean | minor-findings | needs-rework` for Codex reviewers (matches `scripts/codex_review_schema.json`); one of `ship | ship-with-fixes | needs-rework` for Claude reviewers (`code-reviewer` vocab). Preserved verbatim — not normalized across reviewer types.
- **`test_outcome`** (on `implement_done`): `passed | failed | not-run | pre-existing-failure`.
- **`agent`** / **`reviewer`**: `claude | codex`.
- **`disagreement_tag`** (on `review_done` and `commit_done`): `true` when §8.4 escalation produced a code-reviewer verdict disagreeing with Codex. Default absent / `false`.
- **`minor_findings`** (on `review_done`): array of `{severity, file, line, issue, suggested_fix}` objects. `minor_findings_count` (on `commit_done`) is the integer length of that array.
- **`reviewer_findings`** (on `failed` with `stage=review`): full findings blob that drove the failure, for post-mortem inspection.
- **`reversion_guidance`** (on `failed` with `stage=implement`): text from the Claude implementer's "On failure — what to revert" section, captured as-written; not replayed automatically.
- **`plan_review_fallback_used`** (per TASK-006): emitted by the orchestrator whenever `--allow-gemini-fallback` is set AND the Phase 1.5 routing helper (`plan_ops._route_plan_review`) returns `"gemini"` — fired BEFORE the matching Gemini `plan_review_start`. This covers BOTH trigger cases: (a) the `codex_unavailable` row where Gemini is the first and only `plan_review_start` on the run (Codex preflight failed, no prior dispatch), and (b) the post-Codex-failure row where the Codex `plan_review_start` already fired and Gemini's is the second. Fields: `from ∈ {codex}`, `to ∈ {gemini}`, `reason ∈ {codex_unavailable, codex_timeout, codex_parse_error, codex_failure}`. The companion degraded path (both reviewers fail or one absent + the other fails) emits `plan_review_skipped {reason:"all_reviewers_unavailable"}` instead of the legacy `codex_unavailable` so the audit trail records the fallback attempt.
- **`review_fallback_used`** (per TASK-007): emitted by the orchestrator whenever `--allow-gemini-fallback` is set AND the Phase D.1 routing helper (`plan_ops._route_review`) returns `"gemini"` after a Codex `review` returned `outcome ∈ {timeout, parse_error, failure}` — fired BEFORE the matching Gemini `review_start`. Sibling event to `plan_review_fallback_used`; the only structural difference is the per-task scope (`task_id` instead of `plan_file`). Fields: `from ∈ {codex}`, `to ∈ {gemini}`, `reason ∈ {codex_review_timeout, codex_review_parse_error, codex_review_failure}` — naming follows the existing `review_skipped {reason:"codex_review_<outcome>"}` mapping in SKILL.md §Phase D so the audit trail uses one vocabulary. `scope_violation` is intentionally NOT a trigger (it is structural per Phase B; routing it to Gemini would mask the underlying issue).
- **`review_fallback_failed`** (per TASK-007): emitted by the orchestrator on the second-failure path — when the Gemini `review` re-dispatch following a `review_fallback_used` event ALSO returns `outcome ∈ {timeout, parse_error, failure}`. Fired BEFORE the task is classified as a review-stage failure. Audit-symmetry companion to TASK-006's `plan_review_skipped {reason:"all_reviewers_unavailable"}` event — Phase D.1 has no skip surface (review failure cascades into task failure unless fallback succeeds), so the second-failure marker is its own event rather than a `review_skipped` reason. Fields: `from ∈ {codex}`, `to ∈ {gemini}`, `reason ∈ {gemini_review_timeout, gemini_review_parse_error, gemini_review_failure}`.
- **`plan_author_start` / `plan_author_done`** (per TASK-025): emitted by the orchestrator around the Phase 1.5a `plan-author` dispatch that runs on the first `needs-replan` verdict when auto-revise is on (default; disabled by `--no-auto-revise`). `plan_author_start` carries `{run_id, plan_file, findings_count}` where `findings_count` is the length of Codex's first-pass `findings[]`. `plan_author_done` carries `{run_id, plan_file}` required and `{files_edited[], findings_actioned[], findings_skipped[]}` optional — `files_edited[]` is the list of plan paths the author touched (typically the single input plan), `findings_actioned[]` and `findings_skipped[]` are arrays of finding identifiers (index or section+concern) from the author's report.

## Events not logged

- Per-file diffs — use `git show <commit_sha>` instead.
- Implementer / reviewer prompts — reconstructable from task block + template.
- `batch_end` — implicit from the next `batch_start` or `run_end`.
