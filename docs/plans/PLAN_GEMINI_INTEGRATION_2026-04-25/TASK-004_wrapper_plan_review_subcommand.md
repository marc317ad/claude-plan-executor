# TASK-004 — `plan_gemini_dispatch.py plan-review` subcommand

## Goal

Add the `plan-review` subcommand to the wrapper from TASK-003, mirroring the surface and envelope shape of `plan_codex_dispatch.py plan-review` so the orchestrator's Phase 1.5 fallback path (TASK-006) can swap reviewer families with a one-line dispatch change.

## Context

`plan_codex_dispatch.py plan-review` accepts only a persisted schedule JSON path (post-TASK-008 schedule-only contract; the former `--plan-file` / `--plans-dir` shims were removed). It reads the schedule, constructs a Codex prompt, dispatches, and emits a JSON envelope with `parsed` validating against `codex_plan_review_schema.json`. Verdict vocabulary is `{approved, approved-with-notes, needs-replan}`; findings carry `{severity, blocking, section, concern, suggested_change, target_task_id}`.

The Gemini analogue:

```
plan_gemini_dispatch.py plan-review \
  --schedule-file <abs> --repo-root <abs> [--allow-gaps] [--timeout SECS]
```

Same flags, same envelope shape, `parsed` validating against `gemini_plan_review_schema.json` (TASK-002). The two non-obvious carry-overs from the Codex wrapper:

1. **`--allow-gaps` demotion clause.** When `--allow-gaps` is passed AND the persisted schedule's `gaps[]` is non-empty AND every entry's `severity == "soft"` AND the schedule has no structural violations, the wrapper appends the demotion clause to the prompt (verbatim from SKILL.md §Phase 1.5). The reviewer then returns `approved-with-notes` instead of `needs-replan`. Hard gaps suppress the demotion. The wrapper never mutates the schedule.
2. **Schedule-only contract.** The reviewer reads the persisted schedule's unified fat `tasks[]` for description + acceptance_criteria. NO `--plan-file` / `--plans-dir` flags — those were retired in CODEX_FRICTION-era TASK-008 and never come back.

The schema-retry loop from TASK-003 applies here too — the `plan-review` path uses the same `_validate_or_retry` helper authored for `review`. Authoring the helper at TASK-003 + reusing it here is the cheap path; if TASK-003 inlined the loop, this task lifts it out as the first step.

The restrictive policy TOML, `GEMINI_CLI_HOME` isolation, missing-API-key short-circuit, and protected-paths cleanup are all reused from TASK-003. No new structural concerns.

**Out of scope.** Orchestrator wiring (TASK-006). Preflight detection (TASK-005). Schema-audit (TASK-008).

## Verification

- The wrapper module from TASK-003 is extended with a working `plan-review` subcommand at `cmd_plan_review`.
- Invoking `python plan_gemini_dispatch.py plan-review --schedule-file <s> --repo-root <r> --timeout 180` with a fake `gemini` shim on PATH emits a JSON envelope on stdout with shape `{task_id: "plan", subcommand: "plan-review", reviewer: "gemini", outcome: "success", gemini_exit_code: 0, parsed: {...}}` where `parsed` validates against `gemini_plan_review_schema.json`.
- The argparse error from TASK-003's `cmd_plan_review` stub (`"plan-review subcommand not yet implemented; see TASK-004"`) is REMOVED by this task.
- The `--allow-gaps` demotion clause is injected exactly as in `plan_codex_dispatch.py` — verify by reading the dispatched prompt (the wrapper writes the prompt to a temp file when `GEMINI_DISPATCH_DEBUG=1` is set; tests inspect that file).
- Schema-retry loop applies: 3 attempts max; on exhaustion the envelope's `outcome` is `parse_error`.
- Missing API key short-circuits before subprocess spawn.
- New test `tests/scripts/test_plan_gemini_dispatch_plan_review.py` exercises:
  - happy path (success first attempt);
  - `--allow-gaps` demotion clause is present in the rendered prompt iff the schedule has only soft gaps;
  - hard-gap schedule does NOT inject the demotion clause even with `--allow-gaps`;
  - schema-retry exhaustion → `parse_error`;
  - missing API key → no subprocess.
- `venv/bin/pytest -q tests/scripts/test_plan_gemini_dispatch_plan_review.py` returns 0.
- The existing TASK-003 test (`test_plan_gemini_dispatch_review.py`) continues to pass unchanged.

## Tasks

### TASK-004: `plan_gemini_dispatch.py plan-review` subcommand

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_gemini_dispatch.py` (edit — extends TASK-003's module)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit — switch inline `--allow-gaps` demotion clause to the shared constant; consume the shared `_should_inject_allow_gaps_demotion` helper)
  - `plugins/plan-executor/scripts/_plan_paths.py` (edit — add `ALLOW_GAPS_DEMOTION_CLAUSE` constant and `_should_inject_allow_gaps_demotion(schedule_dict, allow_gaps_flag) -> bool` helper for both wrappers to import; or, if the implementer prefers a dedicated module, a new `plugins/plan-executor/scripts/_review_prompts.py` may be added in lieu of editing `_plan_paths.py` — pick exactly one home)
  - `tests/scripts/test_plan_gemini_dispatch_plan_review.py` (new)
- **Dependencies:** [003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_gemini_dispatch_plan_review.py tests/scripts/test_plan_gemini_dispatch_review.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` lines 1-50 (header) — confirm constants
  - `plugins/plan-executor/scripts/plan_gemini_dispatch.py` — full file (TASK-003 output; the module being extended)
  - `plugins/plan-executor/scripts/gemini_plan_review_schema.json` — full file (TASK-002 output)
  - `plugins/plan-executor/scripts/codex_plan_review_schema.json` — full file (structural reference)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (§Phase 1.5 — `--allow-gaps` demotion clause text)
- **Symbol targets:**
  - `cmd_plan_review` in `plan_codex_dispatch.py` — surface mirror reference
- **Acceptance criteria:**
  - `cmd_plan_review` is implemented and the test cases pass.
  - The `--allow-gaps` demotion clause text is byte-identical to the Codex wrapper's (extract it as a module-level constant `ALLOW_GAPS_DEMOTION_CLAUSE` in `plan_codex_dispatch.py` at the same time so both wrappers reference the SAME constant — DO NOT duplicate the prose between modules; pull the constant to `_plan_paths.py` if neither wrapper-module is the right home).
  - Both wrappers' `cmd_plan_review` paths use the SAME helper for the demotion-clause-injection decision (`_should_inject_allow_gaps_demotion(schedule_dict, allow_gaps_flag) -> bool`). Add the helper to `_plan_paths.py` (or a new shared module) and import from both wrappers.
  - Schema-validation retry uses the same `_validate_or_retry` helper as TASK-003. If TASK-003 inlined the loop, this task lifts it out first.
  - The `task_id` field of the envelope is the literal string `"plan"` (matches `plan_codex_dispatch.py` convention).
- **Reversion guidance:** revert the test file and revert the `cmd_plan_review` body in `plan_gemini_dispatch.py` back to the TASK-003 stub. The shared-helper extraction may need a separate revert if it landed in `_plan_paths.py`.

**Description:**
Add `plan-review` to the Gemini wrapper. The trickiest part is keeping the `--allow-gaps` demotion clause in lockstep with the Codex wrapper — the same text in two places will drift, so the right hygiene is a single shared constant + a single shared decision helper, both used by both wrappers. This task lifts that hygiene work in addition to adding the Gemini path; the Codex wrapper is touched only to switch from inline text to the shared constant.

**Implementation notes:**
- Lift `ALLOW_GAPS_DEMOTION_CLAUSE` and `_should_inject_allow_gaps_demotion` into `_plan_paths.py` (or a new `_review_prompts.py` if you prefer a dedicated module — check what the project convention prefers; `_plan_paths.py` is fine if no other shared review-prompt code is added in this batch).
- Re-run the existing Codex `plan-review` tests after the Codex wrapper is changed to consume the shared constant — the prose must be byte-identical post-extraction. If a Codex test golden references the literal text, update the golden in the same commit.
- The schedule is read via `json.load`; on parse failure the wrapper returns `outcome=failure, error="malformed schedule JSON"` BEFORE checking `gaps[]`.
- The "schedule has no structural violations" check is `outcome == "needs-enrichment"` (the schedule validator's contract). Missing or unknown `outcome` suppresses the demotion. Mirror exactly.
- The Gemini prompt for `plan-review` embeds the schema verbatim, then the schedule's `tasks[]` and `batches[]`, then the `--allow-gaps` clause if applicable, then the verdict-vocabulary block. Reuse `plan_codex_dispatch.py`'s prompt-construction helper if it factors cleanly; otherwise mirror its output.
- The shim for the new test reads `GEMINI_SHIM_MODE` env var to choose the response. Reuse the shim from TASK-003 if it was placed in `tests/scripts/_gemini_shim.py`; if it was inlined per-test, lift it to a shared fixture in this task.
