# CODEX FRICTION — Plan-executor friction inventory from run 20260425T023954

**Status:** pending
**Base branch:** main
**Created:** 2026-04-25

## Goal

Eliminate the six friction modes documented in `docs/analysis/plan_executor_codex_friction_2026-04-25.md` (sourced from the `algorithmic_trading_system` repo) so that the next `/implement-plan` run does not pay one Codex-fallback cycle per task. The friction modes are upstream-driven (Codex CLI / GPT 5.5 schema strictness, prompt-budget pressure on Codex's planning loop) plus our own wrapper drift (a Files-block parser that diverged from `plan_ops.py`'s, a reconcile-batch path that trusts that broken parser).

## Scoped Context

### Friction inventory (from the analysis doc)

| # | Symptom | Phase | Root cause |
|---|---|---|---|
| 1 | Codex CLI rejects `plan-review` schema with `Missing 'target_task_id'` | 1.5 | OpenAI structured-output now requires `properties` keys ⊆ `required` array under `additionalProperties: false`. Latent regression risk in every wrapper schema. |
| 2 | Codex `implement` 300 s timeout on 3-file mechanical task; `task008_sweep_coordinator.py` spillover escapes wrapper cleanup | 2 / B | Codex planning loop exceeds 300 s on ~1.6 KB Files-block prose; wrapper safety net does not restore declared-but-not-in-plan-files |
| 3 | Codex `review` 180 s timeout on 8-file diff | 2 / D.1 | Fixed 180 s budget too tight for multi-file mechanical renames |
| 4 | Wrapper allow-list extraction welds prose continuation to file path (`` `Makefile` — add audit ... `` reads as the path) | 2 / B + D | `plan_codex_dispatch.normalize_file_path` lacks the leading-backtick capture + dash-split logic that `plan_ops._normalize_files_entry` already has |
| 5 | `reconcile-batch` restores tracked-file edits on false-positive `out_of_scope_tracked`, wiping the implementer's correct edit | 2 / B post-classification | Orchestrator-side reconcile trusts wrapper's broken classification; never consults the plan's Files: list itself |
| 6 | Codex review on untracked `(create)` files = empty `git diff HEAD` = vacuous `verdict: clean` | 2 / D.1 | Wrapper review-input builder uses `git diff HEAD --` which excludes untracked files |

The cross-cutting observation in the analysis: items 4, 5, and the side-effect in 2 compound — the prose-laden Files block (4) makes the wrapper falsely report scope_violation, which makes `reconcile-batch` restore a correct edit (5), which forces a Claude fallback. Fix the parser, then the reconcile path, then the timeouts/diffs.

### Why we're not "just" raising every timeout

The 300 s implement ceiling exists for a reason: a runaway Codex deliberation is a real failure mode, not just slowness. The cheap-first fix from the analysis is to *trim the prompt* (strip prose continuation from `Files:` bullets — implicit once the parser is unified in TASK-002) before raising the cap. TASK-004 keeps the floors at 300/180 and adds file-count-aware scaling (`max(default, 30 * len(files))` for review; `max(default, 60 * len(files))` for implement) plus operator-controllable plumb-through.

### Why the reconcile-batch fix is defense-in-depth, not the primary fix

If TASK-002 lands cleanly the wrapper's `out_of_scope_tracked` will not contain in-scope files in the first place, and reconcile-batch's behavior becomes correct by construction. TASK-003 still hardens the orchestrator side because (a) the plugin cache that ships to consumer repos can lag the dev tree (the `algorithmic_trading_system` cache at `/home/mad/.claude/plugins/cache/claude-plan-executor/plan-executor/0.1.0/` was already several commits behind HEAD when the friction run captured the symptoms), and (b) reconcile-batch should not blindly trust a wrapper version it cannot pin. Adding a `reconcile_kept_tracked` field also disambiguates "kept the edit" vs "restored to baseline" in the run-log audit trail.

### Issue 1 caveat — the bug is *latent* in HEAD

`scripts/codex_plan_review_schema.json` was missing `target_task_id` from `required` between commits `56b5580` (added the property) and `8103a1c "fix fuck ups"` (added it to `required`). Both commits predate the friction run. The friction run almost certainly hit a stale `~/.claude/plugins/cache` copy, but the *class* of bug — `properties` keys drifting out of `required` under strict OpenAI validation — can recur on any schema change. TASK-001 lands a fixture-based test that fails the build whenever any wrapper schema's `properties` set is not a subset of its `required` array (with `additionalProperties: false`).

### Files-block parser drift (the root of issues 4 + 5)

The orchestrator-side `plan_ops._normalize_files_entry` (`plan_ops.py:6906-6934`) handles the prose-continuation case correctly: it uses a leading backtick capture (`re.match(r'`([^`]+)`', cleaned)`) before falling back to a dash-split. The wrapper-side `plan_codex_dispatch.normalize_file_path` (`plan_codex_dispatch.py:237-248`) only strips `(annotation)`, `:N-M`, `:N`, and surrounding backticks — the dash-split and leading-backtick-capture were never ported. TASK-002 lifts the orchestrator's helper into the shared `_plan_paths` module and re-imports it from both call sites so the two cannot drift again.

## Verification

After all six tasks land:

- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_schema.py tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_codex_dispatch.py tests/scripts/test_plan_ops.py` returns 0.
- A schema-properties-vs-required fixture asserts every JSON schema under `plugins/plan-executor/scripts/codex_*_schema.json` (recursively, every nested object with `additionalProperties: false`) has `set(properties.keys()) == set(required)`.
- Wrapper `parse_task_block` extracts `Makefile` (not `Makefile\` — add audit ...`) from a bullet of the form `` - `Makefile` — add `audit` to the `.PHONY` list (line 1) `` and from a bullet of the form `- Makefile — add audit to .PHONY`.
- Reconcile-batch given an envelope where `out_of_scope_tracked` lists a file *that is in the dispatched task's `tasks[i].files`* of the supplied schedule preserves the working-tree edit, emits `reconcile_kept_tracked: ["<that file>"]`, and `git diff HEAD -- <file>` is non-empty after the call.
- Wrapper `cmd_review` with a target list containing only untracked files produces a non-empty `diff` block in the rendered prompt (verified via `--dry-run`'s `prompt_preview`).
- SKILL.md §Phase D.1 documents an explicit `outcome ∈ {timeout, parse_error, failure}` route to `review_skipped {reason:"codex_review_timeout"|"codex_review_parse_error"|"codex_review_failure"}` plus `--reviewer none --reviewer-verdict ""` commit, mirroring Phase 1.5's degraded-reviewer treatment.
- `--timeout` plumbed through SKILL.md §Phase B-Codex / §Phase D.1 (review) / dispatch-templates.md Phase B-Codex / Phase 1.5 with file-count scaling documented; wrapper defaults remain 300 / 180 / 180 (back-compat).
- One `feat` commit per task on `main`, gates green, no `chore:` housekeeping commit needed beyond the standard end-of-run.

## Tasks

The plan is decomposed into six child files under `docs/plans/CODEX_FRICTION_2026-04-25/` with `00_INDEX.json` for the roster. Each child carries a single `### TASK-NNN:` H3 block with the standard metadata grammar.
