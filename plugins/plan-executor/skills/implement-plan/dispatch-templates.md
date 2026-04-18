# Dispatch Templates

Self-contained prompts for every subagent and wrapper dispatch in the /implement-plan workflow. Each subagent dispatch must embed the full task (or full analyst scope) verbatim — dispatched agents do not see the orchestrator's conversation.

All Agent dispatches include the **"You do NOT have the Agent tool"** constraint: subagents must not spawn further subagents.

## Phase A — plan-analyst dispatch

> Analyze this plan and emit the structured schedule defined by your agent spec. The plan file is at `<absolute plan path>`. Repo root: `<repo_root>`.
>
> Read the plan verbatim, validate task headers / dependencies / files / acceptance criteria, classify each task as `claude` or `codex`, and produce the fenced JSON schedule block plus the markdown gap report. Do NOT modify any files.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash.

## Phase 1.5 — Codex plan review (pre-dispatch gate)

Dispatched after schedule persist, before any batch runs. Codex is the reviewer because the plan was authored by Claude/Opus (analyst); this is the independent pre-exec check. Skipped entirely if `--skip-plan-review` is set OR `codex_available=false` from preflight.

Bash command template:

```
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" plan-review \
  --plan-file <absolute plan path> \
  --schedule-file <absolute schedule path> \
  --plans-dir <plan_dir> \
  --repo-root <absolute repo root> \
  --timeout 180
```

Timeout **180s**. Wrapper captures a pre-dispatch baseline and performs delta-bounded cleanup; the plan-review path runs Codex under `-s read-only` sandbox (advisory) because Codex has no legitimate reason to write during a plan-level review. Any sandbox escape surfaces in `extra.sandbox_escape_detected` without changing the outcome — this matches the `review` subcommand's observe-only semantics.

Wrapper emits one JSON envelope on stdout with `outcome ∈ {success, failure, timeout, parse_error}`. On `success`, `parsed` conforms to `scripts/codex_plan_review_schema.json`:

```json
{
  "plan_file": "<basename>",
  "verdict": "approved | approved-with-notes | needs-replan",
  "findings": [
    {
      "severity": "critical | important | minor",
      "section": "<where in the plan>",
      "concern": "<what is wrong or risky>",
      "suggested_change": "<how to fix>"
    }
  ],
  "schedule_ok": true,
  "summary": "<one-paragraph rationale>"
}
```

Orchestrator routes by verdict (see SKILL.md §Phase 1.5). On `needs-replan`, re-dispatch Phase A (plan-analyst) once with the Codex findings appended to the analyst prompt as a *"Prior plan-review findings"* block, then re-run plan-review. A second `needs-replan` halts with `run_end reason=plan_review_failed`; no batches execute.

## Phase B — plan-implementer dispatch (Claude tier)

> Implement this task from the plan at `<absolute plan path>`.
>
> Plan context:
>
> ```markdown
> <## Context section verbatim>
> ```
>
> Task (verbatim from plan):
>
> ```markdown
> <entire TASK-NNN block>
> ```
>
> Base commit SHA: `<starting_sha>`
>
> Analyst annotations (verbatim, may be empty):
>
> ```json
> <analyst_annotations_json>
> ```
>
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `venv/bin/python ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec. Do not commit. Do not use `git stash`.
>
> **Parallel-mode caveat:** other implementers may be running concurrently on disjoint files. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

## Phase B-Codex — implement via wrapper (Codex tier)

Bash command template — orchestrator issues this directly, wrapper fully owns Codex session lifecycle:

```
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement \
  --plan-file <absolute plan path> \
  --task-id <NNN> \
  --repo-root <absolute repo root> \
  --timeout 300
```

Timeout is **300s** per Appendix D.5. The wrapper captures a pre-dispatch baseline snapshot immediately before invoking Codex and cleans up only the delta against it (plus a protected-path allowlist) — never repo-wide. The wrapper emits a single JSON envelope on stdout with `outcome ∈ {success, failure, timeout, parse_error, scope_violation, dry_run}`; see `scripts/plan_codex_dispatch.py` for the full schema. Orchestrator treats any outcome ≠ `success` as a fallback trigger (fallback = re-dispatch to Claude via the Phase B template above).

## Phase D-Codex — review via wrapper (reviews Claude-implemented work)

Bash command template:

```
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" review \
  --plan-file <absolute plan path> \
  --task-id <NNN> \
  --repo-root <absolute repo root> \
  --files <comma-separated files_changed from implementer> \
  --review-focus bugs \
  --timeout 180
```

Timeout **180s** per Appendix D.5. Wrapper captures a pre-dispatch baseline and performs delta-bounded post-review cleanup against it; any sandbox escape surfaces in `extra.sandbox_escape_detected` without changing outcome. Wrapper embeds the task-scoped diff (`git diff HEAD -- <files>`) — per-batch interleaving keeps that diff scoped exclusively to this task's work.

### Verdict decision ladder (Codex reviewer calibration)

When you render the reviewer prompt for Codex, include this guidance verbatim before the schema reference. Codex historically overuses `needs-rework` on advisory nits; the ladder below reserves `needs-rework` for actual ship-blockers.

Decide your verdict using this ladder in order. Stop at the first rung that fits — do NOT escalate to the next rung unless the criterion is actually met.

1. **`clean`** — no issues, or only forward-looking suggestions that a human reviewer would file as follow-ups without asking the author to revise this commit. A clean scope check, acceptance criteria satisfied, tests pass.
2. **`minor-findings`** — issues a human reviewer would merge with a follow-up note rather than block on. Examples: style drift, typos, out-of-date comment, non-load-bearing naming choice, unused import, a docstring that undersells the code, a log message that could be clearer.
3. **`needs-rework`** — contract violation, acceptance-criterion miss, semantic bug, missing test for a declared verification criterion, or scope inflation past the task's file allow-list. This verdict returns work to the implementer; use it only when a human reviewer would block merge on the finding alone.

Heuristic when unsure: ask "would a human reviewer block merge on this finding alone?" If no → `minor-findings`. If yes → `needs-rework`. Do not bundle several nits together and escalate their sum to `needs-rework`; list each as a minor finding instead.

Worked examples (terse, synthetic):

- Finding: "`# TODO: refactor this later` comment in `foo.py:42` is stale; the refactor already happened." Verdict: **`minor-findings`**. Justification: outdated comment, no behavior impact, trivial follow-up.
- Finding: "Acceptance criterion V2 requires a regression test covering the empty-input branch; the diff adds the branch but no test asserts it." Verdict: **`needs-rework`**. Justification: declared verification criterion is unmet — a ship-blocker.

Wrapper returns `parsed.verdict ∈ {clean, minor-findings, needs-rework}` per `scripts/codex_review_schema.json`; orchestrator routes by verdict.

## Phase D-Claude — code-reviewer on Codex work

Agent dispatch, `model: "sonnet"` (explicit v1 choice — see Open risks 3):

> Scope: `<comma-separated files from Codex wrapper's files_changed>`
>
> Intent — the task's Description and Acceptance criteria (verbatim from the plan):
>
> **Description:**
> <verbatim from TASK-NNN Description>
>
> **Acceptance criteria:**
> <verbatim from TASK-NNN Acceptance criteria>
>
> Verify this change addresses the task without regressions or scope creep. Focus on correctness and domain-specific patterns from the project. Flag pre-existing issues in Minor/nits only.
>
> **Parallel-tree caveat:** other batch-mates' unstaged changes to disjoint files may be in the working tree — focus strictly on the scope files listed above.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash.

The parallel-tree caveat is a deliberate divergence from design §10 line 889; mirrors `.claude/skills/fix-bugs/dispatch-templates.md` Phase D.1. Do not align back without updating both.

## Phase D.5 — code-reviewer third opinion (§8.4 escalation)

Dispatched only when Codex reviewing a Claude-implemented task returns `needs-rework` AND the user did not pass `--codex-review-binding`. Agent dispatch, `model: "sonnet"`:

> Scope: `<comma-separated files from Claude implementer's files_changed>`.
>
> Codex (a peer reviewer) returned `needs-rework` on this task and flagged the findings below. Independently review the change and decide whether each Codex finding is load-bearing (a ship-blocker) or a nitpick that should be dismissed.
>
> Task (verbatim from plan):
>
> ```markdown
> <entire TASK-NNN block>
> ```
>
> Codex findings (verbatim from `parsed.findings` of the wrapper envelope):
>
> ```json
> <codex_findings_json>
> ```
>
> Return your verdict (`ship | ship-with-fixes | partial-agreement | needs-rework`) and a brief justification.
>
> **Verdict decision rubric — pick the verdict that matches the split, not a stronger one:**
>
> 1. `ship` — you disagree with Codex entirely; none of the findings are load-bearing. Commit proceeds with a bare `[disagreement]` tag.
> 2. `ship-with-fixes` — you disagree with Codex about ship-blockers; any residual concerns are minor follow-ups. Commit proceeds with a bare `[disagreement]` tag.
> 3. `partial-agreement` — the findings split cleanly: at least one is load-bearing AND at least one can be safely dismissed. Use this verdict ONLY when both buckets are non-empty. Triggers the narrow-remediation retry (D.2a.6) scoped to the load-bearing subset; dismissed indices are recorded in the commit trailer.
> 4. `needs-rework` — all findings are load-bearing. Triggers the full bounded-remediation retry (D.2a.5).
>
> **Hard rules for `partial-agreement`:**
>
> - Emit this verdict only when BOTH `load_bearing` and `dismissed` are non-empty. If every finding is load-bearing → use `needs-rework`. If no finding is → use `ship-with-fixes`. A unanimous split (empty bucket on either side) is a contract violation — the parser rejects it with `partial-agreement-invalid-split`.
> - Indices in `load_bearing` and `dismissed` MUST be 0-based positions into the Codex `findings[]` array above, disjoint, and in range. There is no `id` field on Codex findings; array index is the reference.
>
> **Output shape:**
>
> - For `ship` / `ship-with-fixes` / `needs-rework`:
>
>   ```json
>   {"verdict": "ship", "summary": "<one-line justification>"}
>   ```
>
> - For `partial-agreement` (findings array of length 4, indices 0..3):
>
>   ```json
>   {
>     "verdict": "partial-agreement",
>     "load_bearing": [0, 2],
>     "dismissed": [1, 3],
>     "summary": "<one-line justification naming which findings fall in which bucket>"
>   }
>   ```
>
> **Parallel-tree caveat:** other batch-mates' unstaged changes to disjoint files may be in the working tree — focus strictly on the scope files listed above.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash.

Same deliberate parallel-tree divergence from §10 as Phase D-Claude. Call out in the Phase 4 commit body if an alignment pass lands.

## Phase D.2b — Role-swap retry (Codex-implements + Claude-reviews needs-rework)

Per design §8.3 line 692: Claude re-implements, Codex re-reviews. One attempt.

**Retry implement** — re-use the Phase B template above verbatim with the same TASK-NNN block. Reviewer findings are NOT forwarded in v1 (see Open risks 1); Claude re-implements from the plan spec. After retry success, re-run Phase D-Codex (wrapper review) on the re-implementation. `needs-rework` on the re-review is terminal for this task — no further retries.

## Phase B-rework — Bounded remediation retry (D.2a.5)

Dispatched only when Codex reviewing Claude-implemented work returns `needs-rework` AND the Phase D.5 third-opinion code-reviewer independently agreed (verdict `needs-rework`). Strictly one attempt. Use `Agent(subagent_type: "plan-implementer", model: "opus")`.

Unlike Phase D.2b, reviewer findings ARE forwarded here — the risk of the implementer blindly doing whatever Codex said is mitigated because D.5 already confirmed the findings are load-bearing. Keep the forwarded prompt structured; do NOT paraphrase into a free-form "fix what Codex flagged".

> Apply a narrow remediation patch to this task's existing implementation at `<absolute plan path>`. The prior attempt is **still in the working tree** — it was NOT reverted. Codex (peer reviewer) returned `needs-rework` and the third-opinion code-reviewer independently agreed that the findings below are load-bearing. Your job is to patch the current working-tree edits with a minimal, surgical fix that addresses each finding. Do NOT rebuild from the base commit; do NOT re-do the work that is already correct; only edit what is necessary to resolve the findings.
>
> Plan context:
>
> ```markdown
> <## Context section verbatim>
> ```
>
> Task (verbatim from plan):
>
> ```markdown
> <entire TASK-NNN block>
> ```
>
> Base commit SHA: `<starting_sha>`
>
> Codex findings (verbatim from `parsed.findings` of the wrapper envelope):
>
> ```json
> <codex_findings_json>
> ```
>
> Third-opinion (D.5) summary — why these findings are load-bearing:
>
> ```
> <d5_summary>
> ```
>
> Analyst annotations (verbatim, may be empty):
>
> ```json
> <analyst_annotations_json>
> ```
>
> **Fix narrowly, do not scope-inflate.** Address each finding directly. Do NOT refactor unrelated code, do NOT add docstrings to untouched regions, do NOT tidy formatting outside the edited scope. If a finding cannot be reconciled with the plan's acceptance criteria, report `plan-incorrect` — do not invent a compromise.
>
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `venv/bin/python ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec. Do not commit. Do not use `git stash`.
>
> **Parallel-mode caveat:** other implementers may be running concurrently on disjoint files. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

After retry success, re-run Phase D-Codex (wrapper review) on the re-implementation. `clean | minor-findings` → D.3 commit with `--remediation-tag`. `needs-rework` on the re-review triggers the D.2a.5 awaiting-user pause (see SKILL.md §D.2a.5 step 6); the orchestrator does NOT call `fail-task`.

## Phase B-narrow-remediation — Narrow-remediation retry (D.2a.6)

Dispatched only when Codex reviewing Claude-implemented work returns `needs-rework` AND the Phase D.5 third-opinion code-reviewer returned `partial-agreement` (findings split cleanly into load-bearing + dismissed buckets). Strictly one attempt. Use `Agent(subagent_type: "plan-remediator", model: "opus")` — a dedicated subagent role (not `plan-implementer`) so the touch-only-these-lines scope rule is structurally enforced by the agent's system prompt, and the retry is visible in the run log as a distinct dispatch.

Unlike Phase B-rework, the forwarded findings are **filtered** to the load-bearing subset only. The dismissed subset is supplied separately as context-only, explicitly labeled "DO NOT fix — context only"; the remediator acknowledges them in a `**Dismissed findings noted:**` report section but MUST NOT act on them. The `(file, line)` union of the load-bearing findings is the remediator's permitted edit region; unjustified edits outside that region return outcome `scope-violation`.

> Apply a narrow remediation patch to this task's existing implementation at `<absolute plan path>`. The prior attempt is **still in the working tree** — it was NOT reverted. Codex (peer reviewer) returned `needs-rework` and the third-opinion code-reviewer (Phase D.5) adjudicated the findings into two buckets: `load_bearing` (ship-blockers you MUST fix) and `dismissed` (non-blockers you MUST NOT fix). Patch the current working-tree edits with a minimal, surgical fix that addresses each load-bearing finding only. Do NOT rebuild from the base commit; do NOT re-do work that is already correct; do NOT act on dismissed findings even if you notice them along the way.
>
> Plan context:
>
> ```markdown
> <## Context section verbatim>
> ```
>
> Task (verbatim from plan):
>
> ```markdown
> <entire TASK-NNN block>
> ```
>
> Base commit SHA: `<starting_sha>`
>
> Load-bearing findings (D.5 ruled these ship-blockers — fix each one):
>
> ```json
> <load_bearing_findings_json>
> ```
>
> Dismissed findings — **DO NOT fix — context only**. D.5 ruled these non-load-bearing; acting on them silently re-inflates the retry's scope. Echo each in the mandatory `**Dismissed findings noted:**` report section without acting on it:
>
> ```json
> <dismissed_findings_json>
> ```
>
> Third-opinion (D.5) summary — why the load-bearing findings are load-bearing:
>
> ```
> <d5_summary>
> ```
>
> Analyst annotations (verbatim, may be empty):
>
> ```json
> <analyst_annotations_json>
> ```
>
> **Scope rule (the touch-only-these-lines contract):** the union of `(file, line)` coordinates across `load_bearing_findings[]` defines your permitted edit region. Unjustified edits outside that region flip the outcome to `scope-violation`. **Fix narrowly, do not scope-inflate.** Address each load-bearing finding directly. Do NOT refactor unrelated code, do NOT add docstrings to untouched regions, do NOT tidy formatting outside the edited scope. If a finding cannot be reconciled with the plan's acceptance criteria, report `plan-incorrect` — do not invent a compromise.
>
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `venv/bin/python ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec (including the mandatory `**Dismissed findings noted:**` and `**Scope violations:**` sections). Do not commit. Do not use `git stash`.
>
> **Parallel-tree caveat:** other implementers and reviewers may be running concurrently on disjoint files; unstaged changes to disjoint files may be in the working tree. Focus strictly on the scope files listed in the task's `Files:` field. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

After retry success, re-run Phase D-Codex (wrapper review) on the re-implementation. `clean | minor-findings` → D.3 commit with `--narrow-remediation-tag --dismissed-finding-ids <comma-separated indices from D.5's dismissed bucket>`. `needs-rework` on the re-review triggers the D.2a.6 awaiting-user pause (see SKILL.md §D.2a.6 step 7); the orchestrator does NOT call `fail-task`. A retry outcome of `scope-violation` — or any other non-success outcome — triggers the same awaiting-user pause with `stage:"post_narrow_remediation_implement"`.
