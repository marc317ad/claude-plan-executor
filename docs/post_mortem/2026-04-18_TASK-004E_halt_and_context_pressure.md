# Post-mortem — TASK-004E halt + orchestrator context pressure

**Date:** 2026-04-18
**Run ID:** `20260418T042817`
**Plan:** `docs/plans/DUAL_AGENT_Plans/TASK-004E_acquire_lock_strict.md`
**Outcome:** halted before any batch ran; no code edits; lock released cleanly.

---

## TL;DR

Run halted in Phase 1 because the plan-analyst (Claude/Opus) emitted a `needs-enrichment` outcome over a single gap (`unresolvable-test`) that was a false positive — the plan's test command legitimately chains two pytest invocations with `&&`, both referenced files exist on disk, and the plan is otherwise well-formed. Skill protocol requires `--allow-gaps` to proceed past `needs-enrichment`; none was passed, so the orchestrator halted as specified. User flagged two deeper concerns: (1) the orchestrator should handle this class of non-issue autonomously rather than halting, and (2) Claude subagent dispatches via the Agent tool bloat the orchestrator's context, risking mid-run compaction.

---

## What happened (chronological)

1. `/implement-plan docs/plans/DUAL_AGENT_Plans/TASK-004E_acquire_lock_strict.md` invoked in auto mode, no flags.
2. Phase 0 preflight passed: clean source tree, `codex_available=true`, branch match, starting_sha captured, run_id `20260418T042817` minted.
3. `check-plan-deps` passed — TASK-004D (the only cross-plan dep) is Done.
4. Run-lock acquired; `run_start` appended.
5. Phase 1 plan-analyst dispatched via `Agent(subagent_type: "plan-executor:plan-analyst", model: "opus")`.
6. Analyst returned:
   - `outcome: needs-enrichment`
   - 1 task (`004E`, claude-tier, priority medium)
   - 1 batch, 4 file locks
   - 1 gap: `unresolvable-test` — "test command chains two pytest invocations with `&&` shell operator; analyst cannot statically resolve the chained command (both referenced test files exist on disk)"
   - 2 risks (TASK-001 cross-plan dep assumption, integration-test `@pytest.mark.slow` skip caveat)
7. `parse-schedule` accepted the envelope (no errors).
8. Per skill §Phase 1: `needs-enrichment` + no `--allow-gaps` → halt.
9. `analyst_done`, `run_end {outcome: halted, reason: analyst_needs_enrichment_no_allow_gaps}` logged; lock released.

No implementer, reviewer, or commit code paths ran. No files modified.

---

## Issues identified

### 1. Analyst flagged a non-issue as a gap

The plan's test command is:

```
venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestLock \
  && venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -k "preserves_run_lock_json" --no-header
```

`&&` is a legitimate shell idiom — it chains two pytest invocations so both must pass. The plan itself documents the integration-test leg as a regression guard. The analyst's static validator can't parse shell operators, so it tags the whole `test_command` field as unresolvable. Both referenced files exist on disk. This is purely an analyzer limitation, not a plan defect.

### 2. Halt protocol is binary, not severity-graded

Skill rule: `needs-enrichment` + no `--allow-gaps` → halt. All gaps are load-bearing under this rule, regardless of whether they represent a genuine defect (missing file, circular dep) or an advisory nit (analyzer couldn't parse a shell operator). The binary nature collapses judgment into a single flag.

### 3. Orchestrator autonomy was traded away to fix a past inconsistency problem

Historical LLM inconsistency in making "is this really a blocker?" judgments at runtime led to the rigid halt rule. The rule works, but it now over-halts on non-issues. Relaxing it via orchestrator-side "proceed anyway" judgment would re-introduce the inconsistency; tightening the analyst's classification rules is the safer direction.

### 4. Claude subagent dispatches bloat orchestrator context

Every `Agent(subagent_type: ...)` call returns the subagent's full markdown report into the orchestrator's context. Across a multi-batch plan, the orchestrator accumulates: one analyst report, one plan-review envelope, one implementer report per task, one reviewer verdict per task, plus any D.5 third-opinion and D.2a.5/D.2a.6 retry reports. A 5-task plan with retries easily feeds 15+ verbose markdown blocks into orchestrator context before end-of-run.

### 5. Asymmetric dispatch patterns between Claude and Codex

Codex tasks dispatch via `plan_codex_dispatch.py` — a Python subprocess wrapper that invokes `codex exec`, captures output, performs delta-bounded cleanup, and emits a single JSON envelope. Context cost: one bounded envelope per call.

Claude tasks dispatch via the Agent tool. Context cost: the full subagent report.

Same work, very different context footprint. This asymmetry is invisible in short runs and punishing in long ones.

### 6. Subagent reports already parsed to structured fields, but raw report stays in context

`parse-implementer-report` already extracts the load-bearing structured fields (outcome, files_changed, verdicts, concerns, diagnostics). The raw markdown report that those fields came from remains in orchestrator context for the rest of the run even though nothing downstream consumes it.

---

## Suggested resolutions

Ranked cheapest-first. A + B are the minimum to unblock this specific plan; C + D + E are the structural fixes that address the broader "orchestrator autonomy + context pressure" problem the user raised.

### A. Teach `parse-schedule` / plan-analyst to split shell-chained test commands

One regex split on `&&`, `;`, `||`; validate each leg independently. Deletes the specific `unresolvable-test` gap at the source. Minutes of work, low blast radius. **Solves issue #1.**

### B. Add gap severity: `blocker | advisory`

Analyst spec adds a `severity` field to each gap. `blocker` = missing file, cycle, missing required field. `advisory` = unresolvable-test when all files exist, terse classification_reason, etc. Skill halt rule flips to: halt on any `blocker`; emit `gap_auto_override` event + continue on `advisory`. **Solves issues #2, #3.**

### C. Deterministic `plan_ops.py gap-triage` subcommand

Takes the analyst JSON + filesystem; reclassifies each gap by pure Python rules (no LLM). Example rule: `unresolvable-test AND all files in task.files exist → advisory`. Moves the severity decision out of LLM judgment entirely. Orchestrator runs this after `parse-schedule`, then applies the blocker/advisory halt rule. **Deepens the fix for #3** — the judgment is no longer subject to analyst variance because it's a function, not a prompt.

### D. Python-wrapper Claude subagents (`plan_claude_dispatch.py`)

Mirror `plan_codex_dispatch.py`. Wrapper invokes `claude -p "<prompt>" --output-format json --model opus` headless, captures the JSON output, returns a bounded envelope (outcome + structured fields + path to a file on disk containing the full report). Orchestrator calls this via Bash, not the Agent tool. Context cost drops to one JSON line per dispatch. **Solves issues #4, #5.**

Open questions for design:
- Does `claude -p` honor subagent system prompts the way `Agent(subagent_type: ...)` does? Needs a probe.
- Retry/failure semantics — wrapper handles timeout, parse_error, sandbox_escape symmetrically to the Codex wrapper.
- Stdout streaming vs. final-JSON-only — the Codex wrapper uses final-only; same pattern probably fits.

### E. Report-to-disk pattern

Even if D is not adopted, subagents can be instructed to write the full report to `docs/plans/_run_reports/<run_id>-<task>-<phase>.md` and return only a pointer + the parsed structured envelope. Orchestrator reads the file only if a downstream step actually needs it (e.g., D.5 third-opinion needs the Codex findings verbatim). **Partial mitigation of #4, #6** without requiring D.

### F. Audited auto-override events

When the orchestrator proceeds past an `advisory` gap (under rule B) or a triage-reclassified gap (under rule C), it emits `gap_auto_override {task_id, gap_type, triage_rule, reasoning}` to `_run_log.jsonl`. Final run summary shows a banner listing every auto-override. If the banner grows run-over-run, triage rules need tightening. **Keeps behavior inspectable and bounded.**

---

## Meta-observation: the autonomy tradeoff

The user's framing — "how do we give the orchestrator more autonomy without reintroducing inconsistency" — is the central question. The honest answer is: **don't expand runtime LLM judgment; expand the deterministic rulebook.** Every decision that's currently a runtime LLM call ("is this gap real?") is a candidate for extraction into a reviewed, codified rule (`gap-triage`), with the orchestrator narrowing to "apply the rule correctly." This is the shape of a reliable agentic system — the LLM's job is to follow the rulebook, not to redecide the rules on each invocation. Orchestrator autonomy grows by growing the rulebook, not by loosening it.

---

## Immediate user-facing path for this run

Re-invoke with `--allow-gaps`:

```
/plan-executor:implement-plan docs\plans\DUAL_AGENT_Plans\TASK-004E_acquire_lock_strict.md --allow-gaps
```

This unblocks the run today. Fixes A + B above remove the need for the flag on future runs of the same family of plan.

---

## Proposed resolution plan (sketch — not executed)

1. **TASK-POSTMORTEM-A**: teach analyst/parse-schedule to split shell-chained test commands. Delete the specific false-positive gap. (analyst spec + `scripts/plan_ops.py`)
2. **TASK-POSTMORTEM-B**: add gap `severity` field + halt-on-blocker-only semantics. (analyst spec + `plugins/plan-executor/skills/implement-plan/SKILL.md` + `plan_ops.py parse-schedule`)
3. **TASK-POSTMORTEM-C**: add `plan_ops.py gap-triage` subcommand. (`plan_ops.py`)
4. **TASK-POSTMORTEM-D**: prototype `plan_claude_dispatch.py` wrapper; probe whether `claude -p` honors subagent system prompts. If yes, migrate plan-analyst and plan-implementer dispatches to the wrapper pattern. (scripts/ + SKILL.md)
5. **TASK-POSTMORTEM-E**: report-to-disk pattern for Agent-tool dispatches that remain after D (if any).
6. **TASK-POSTMORTEM-F**: `gap_auto_override` event type + final-summary banner.

Sequence: A + B first (unblocks this plan family), C next (deterministic triage), D in parallel (context pressure fix), E + F as follow-ups.
