---
title: Gemini post-hoc review — PLAN_GEMINI_INTEGRATION_2026-04-25 paused run 20260428T121041
date: 2026-04-28
run_id: 20260428T121041
plan: docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25
orchestrator: claude -p --claude-only
reviewer: gemini-2.5-pro (post-hoc, headless)
status: report-only — recommendations NOT executed
---

# Gemini post-hoc review

This report captures Gemini 2.5 Pro's independent review of the partial output of a `/implement-plan` run that paused after Phase D Batch 1. **No fixes were applied based on these findings.** The plan remains paused awaiting recovery decision; this document is to be revisited when the plan continues.

## Run summary

- **Plan**: `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25` (adds Gemini CLI as adversarial-review fallback for plan-executor)
- **Orchestrator command**: `claude --print --permission-mode auto --model opus --effort max --output-format stream-json --include-partial-messages --verbose --add-dir /mnt/d/claude-plan-executor -- /implement-plan docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25 --claude-only --unattended-revert-policy=pause`
- **Run ID**: `20260428T121041`
- **Started**: 2026-04-28T12:10:01Z
- **Ended**: 2026-04-28T12:39:46Z (paused, lock released, no commits)
- **Cost**: $6.79 USD (orchestrator); plus inner subagent costs
- **Phases reached**: Phase 0 ✓ → Phase 1 (4 children classified valid) ✓ → Phase 1.5 plan-reviewer **approved** ✓ → Phase D Batch 1 (parallel TASK-005 + TASK-008) → **paused before commit**

## Why it paused

The orchestrator's `awaiting_user` record (run-log event at 12:38:27Z) recorded:

> v3 wrapper parallel-batch race: TASK-005 wrapper's delta-bounded autoclean reverted TASK-008's already-completed file `tests/scripts/test_plan_codex_dispatch_schema.py` (which is in TASK-008's declared scope, NOT TASK-005's). TASK-005 implementer's own changes (plan_ops.py, SKILL.md, test_plan_ops.py) are intact in the working tree. Cross-task scope partitioning that reconcile-batch is supposed to provide was bypassed because the wrapper auto-cleaned before envelope emission.

Outcomes recorded for batch 1:

- TASK-005 implementer → `outcome=scope_violation` (the wrapper detected a write to an out-of-declared-scope file and rolled it back; the file in question was actually TASK-008's deliverable — NOT a real scope violation by TASK-005)
- TASK-008 implementer → `outcome=success_then_destroyed` (the implementer correctly produced its edit; the sibling wrapper destroyed it)

The orchestrator paused per `--unattended-revert-policy=pause`, released the run-lock, and surfaced full reversion guidance. **No commits were made**; HEAD remains at the pre-run baseline `2f1542210304cd2301fc500f60694a672cd7c9e4`.

## Working tree at pause

```
modified:   docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/TASK-005_preflight_gemini_available.md
modified:   docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/TASK-008_schema_audit_extension.md
modified:   docs/plans/_run_log.jsonl
modified:   plugins/plan-executor/scripts/plan_ops.py
modified:   plugins/plan-executor/skills/implement-plan/SKILL.md
modified:   tests/scripts/test_plan_ops.py
untracked:  docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/PLAN_GEMINI_INTEGRATION_2026-04-25.schedule.json
```

(TASK-008's surviving deliverable would have been edits to `tests/scripts/test_plan_codex_dispatch_schema.py`; that file was reverted to HEAD.)

---

## Gemini's findings (verbatim, organized A/B/C/D)

### (A) Correctness of the surviving TASK-005 code

The code changes in `task005_code_diff.patch` correctly and comprehensively implement the requirements for TASK-005.

1. **Helper function:** The new `_resolve_gemini_available` function in `plan_ops.py` correctly implements the specified logic. It checks for the `gemini` executable on the `$PATH` and verifies that at least one of `GEMINI_API_KEY` or `GOOGLE_APPLICATION_CREDENTIALS` is set to a non-empty string. The use of `.strip()` to handle whitespace-only values is robust.
2. **Preflight integration:** The `gemini_available` boolean is correctly added to the `cmd_preflight` JSON output by calling the new helper function. The module-level constants `GEMINI_API_KEY_ENV` and `GOOGLE_APP_CRED_ENV` are a good practice, ensuring a single source of truth that is also available to tests.
3. **Audit check:** The new advisory audit check, `_check_gemini_available`, is correctly implemented. It is properly registered in `AUDIT_CHECKS` at the "advisory" tier, ensuring it provides information without affecting run verdicts. The payload in the finding correctly structures the detailed availability state (`binary_present`, `api_key_present`, etc.), which is valuable for diagnostics.
4. **CLI flag documentation:** The `--allow-gemini-fallback` flag is clearly documented in `SKILL.md`, matching the expected functionality.
5. **Tests:** The accompanying tests in `test_plan_ops.py` are thorough. `TestResolveGeminiAvailable` covers all branches of the helper's logic (the full "truth table"), including edge cases like empty environment variables. `TestGeminiAvailableAuditCheck` validates the check's registration, payload, and non-blocking advisory status.

**No defects were found in the surviving code for TASK-005. It meets all documented acceptance criteria.**

### (B) Soundness of the orchestrator's diagnosis of the wrapper race

The orchestrator's diagnosis of a wrapper race condition is sound and well-supported by the evidence in `run_log_excerpt.jsonl`.

1. **Event timeline:** The logs show that `implement_done` for TASK-008 (outcome: `success_then_destroyed`) occurred at the same timestamp (`12:38:06Z`) as the `implement_done` event for TASK-005 (outcome: `scope_violation`).
2. **Root cause:** The wrapper error message for TASK-005 explicitly states: `"delta-bounded cleanup detected writes outside declared files_changed: [tests/scripts/test_plan_codex_dispatch_schema.py]"`. This file is the one and only file in TASK-008's declared scope.
3. **Diagnosis confirmation:** The `awaiting_user` event's `diagnostics` field correctly concludes: "The wrapper's apply_cleanup() does not consult sibling-task scopes, only the local declared_files_changed of the dispatch it served."

This sequence confirms the diagnosis: TASK-008 finished its work first. Then, the wrapper for the still-running TASK-005 performed a cleanup. Seeing a modification to a file outside its *own* scope (`test_plan_codex_dispatch_schema.py`), it reverted that file, thereby destroying the work of its sibling task.

**This is a real bug in the `apply_cleanup()` implementation.** It lacks awareness of the wider execution context (i.e., the scopes of parallel sibling tasks), making it unsafe for parallel batches. **The minimal upstream fix would be for the cleanup logic to ignore changes to any file declared in the scope of *any* task within the current batch, not just its own.**

### (C) Recovery recommendation

Of the three recovery options suggested by the orchestrator, Gemini recommends **(a) commit TASK-005 manually, then rerun TASK-008**.

- **Rationale:** The evidence strongly supports that the work for TASK-005 is correct and complete. The `task005_code_diff.patch` is clean, and the logs show its tests passed before the pause. Committing these correct changes manually is the most efficient path forward, as it avoids the cost and time of re-running a task that has already succeeded. A subsequent, focused run (`/implement-plan ... --task-ids 008`) is a clean and simple way to redo the lost work from TASK-008.
- **Alternative (b) — rerun with `--parallel 1`:** This is a safe and viable alternative. It would prevent the race condition by serializing the tasks. However, it is less efficient, as it would re-execute the already-completed work of TASK-005. It should be considered a good fallback if the operator is not comfortable performing the manual `git commit` required by option (a).
- **Alternative (c) — wrapper fix:** This is the necessary **long-term solution** to prevent the bug from recurring in future parallel runs. However, it is not a "recovery" action for the current paused run. The bug should be fixed upstream, but the immediate priority is to complete the in-flight plan.

**Therefore, for immediate recovery, (a) is the superior choice for its efficiency, with (b) being a safer, simpler, but slower alternative.**

### (D) Other red flags

1. **The wrapper race condition:** The most significant red flag is the bug itself. A cleanup mechanism that can cause data loss between parallel processes is a critical issue. However, the system's ability to detect this anomaly, halt the execution, preserve the surviving work, and provide a detailed diagnostic is a major mitigating factor. The "delta-bounded autoclean" feature caused the issue due to a lack of context, but it also prevented a silent failure by flagging the unexpected change.
2. **Audit row format:** The execution log format appended to the plan markdown files (`plan_md_pause_rows.patch`) is not a red flag; on the contrary, it is a positive feature. The rows clearly and concisely capture the `paused` status, the responsible task, and a human-readable reason. Embedding this audit trail directly in the relevant task files is excellent for traceability. **No issues were noted with this format.**

**Overall, the partial state appears well-diagnosed. The primary concern is the underlying wrapper bug, but the system's response to the failure was robust.**

---

## (E) Operator-flagged concern — `--unattended-revert-policy=pause` does NOT gate the wrapper-level revert

This finding is from the operator (user) on review of the report above, not from Gemini's review. It is a distinct concern from the parallel-batch race in (B) — even in serial execution, this concern would still apply if an implementer ever wrote to a file outside its declared scope.

The orchestrator was invoked with `--unattended-revert-policy=pause`. Per the documentation in `plugins/plan-executor/skills/implement-plan/SKILL.md` (lines 161 / 247) and the consumers in `plan_ops.py:4952` (preflight) and `plan_ops.py:11703` / §`review-route`, the flag governs the **orchestrator's** pause-vs-fail-fast-vs-preserve-only behavior in:

- the awaiting-user pause subroutine,
- `review-route` (the deterministic post-D.1 routing state machine),
- reconcile-batch.

The destructive call that reverted TASK-008's file is at `plugins/plan-executor/scripts/plan_claude_dispatch.py:748`:

```python
cleanup_result = cleanup.apply_cleanup(
    baseline, declared, repo_root,
    authorization_source=auth_source,
)
```

This runs **inside the wrapper subprocess for the TASK-005 dispatch**, before the envelope is returned to the orchestrator. `apply_cleanup` is gated only by `authorization_source` (anti-aliasing guard for empty declared scope; cf. `plan_claude_dispatch.py:730`). For a write-authorized agent (`plan-implementer`) with a non-empty `declared_files_changed`, the cleanup proceeds and reverts every file in the post-dispatch diff that is not in the local declared list.

**`apply_cleanup` does not read `--unattended-revert-policy`.** The wrapper has no path through which the orchestrator's policy choice can reach this codepath. The orchestrator only saw the post-revert state via the `scope_violation` envelope and `success_then_destroyed` recognition; its `awaiting_user` pause is the flag working **after** destruction, not preventing it.

This is a non-trivial gap because the operator's mental model of `--unattended-revert-policy=pause` is "no unauthorized revert without operator approval," whereas the implementation only delivers "no unauthorized **orchestrator-level** revert without approval." Wrapper-level reverts are a separate, ungated authority.

**Two reasonable upstream remediations:**
1. Thread `--unattended-revert-policy` into the wrapper input payload and have `apply_cleanup` pause-and-emit a `wrapper_autoclean_blocked`-style envelope under `pause` policy (no destruction inside the wrapper; the orchestrator chooses).
2. Per Gemini's (B): make `apply_cleanup` batch-scope-aware so it ignores deltas to any file declared by any sibling task in the batch. This narrows the failure case but still leaves wrapper-level autoclean as an ungated authority outside parallel batches.

(1) is the more conservative fix vs. the operator's stated intent of the flag. (2) is the minimum change to fix the specific race. Doing both is consistent.

This concern was NOT visible to the orchestrator's own diagnostic (which framed the bypass narrowly as "reconcile-batch was bypassed because the wrapper auto-cleaned before envelope emission") nor to Gemini's review. The operator's question — "Did you approve the reversion?" — surfaced it.

---

## Action register (NOT executed by this report)

The following items are surfaced for the user's later decision; this document does NOT trigger any of them:

1. **Recovery for this plan** — Gemini and the orchestrator both prefer option (a): manually commit the surviving TASK-005 edits, then re-run `/implement-plan` with `--task-ids 008` to redo the destroyed work, then continue normally for TASK-006 / TASK-007.
2. **Upstream wrapper fix (race)** — file a bug for `apply_cleanup()` in `plugins/plan-executor/scripts/plan_claude_dispatch.py` (and likely the analogous `plan_codex_dispatch.py` if it shares the pattern) to make delta-bounded autoclean batch-scope-aware. Minimal patch shape per Gemini: ignore deltas to any file declared by any sibling task in the current batch, not just `declared_files_changed` of the local dispatch.
3. **Upstream wrapper fix (policy bypass)** — per (E): thread `--unattended-revert-policy` into the wrapper input payload so `apply_cleanup` can defer destruction to the orchestrator under `pause`. This addresses the operator's stated expectation of the flag.
4. **`--parallel 1` as an interim mitigation** — until the wrapper fix lands, `/implement-plan` runs that involve parallel batches with multi-task `declared_files_changed` partitions are at risk; serialized runs avoid the race. Note: `--parallel 1` does NOT mitigate the policy-bypass concern in (E) on its own — only the race.

## Inputs reviewed by Gemini

- `/tmp/gemini-analysis/task005_code_diff.patch` (surviving code edits, 213 lines)
- `/tmp/gemini-analysis/plan_md_pause_rows.patch` (paused-row §5 audit entries appended to TASK-005 / TASK-008 plan files, 32 lines)
- `/tmp/gemini-analysis/run_log_excerpt.jsonl` (24-event timeline for run `20260428T121041`)
- `/tmp/gemini-analysis/review_prompt.md` (the briefing above, asking for sections A/B/C/D and explicitly forbidding fix application)

Bundle is ephemeral (in `/tmp`) and may be regenerated from the working-tree diff against `2f15422` and the run-log filtered by run id `20260428T121041`.

## Provenance

- **Reviewer**: `gemini -p ... --approval-mode plan -o text -m gemini-2.5-pro` (read-only headless mode; no MCP servers, no tools, no fixes proposed).
- **Bundle**: assembled by orchestrator from the working tree at the pause point, the matching run-log slice, and a structured prompt that explicitly told Gemini "Do NOT propose to apply fixes — this is report-only."
- **Orchestrator's pre-run baseline SHA**: `2f1542210304cd2301fc500f60694a672cd7c9e4`.
