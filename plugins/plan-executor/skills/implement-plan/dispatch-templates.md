# Dispatch Templates

Self-contained prompts for every subagent and wrapper dispatch in the /implement-plan workflow. Each subagent dispatch must embed the full task (or full analyst scope) verbatim — dispatched agents do not see the orchestrator's conversation.

All Agent dispatches include the **"You do NOT have the Agent tool"** constraint: subagents must not spawn further subagents.

**Python interpolation.** Templates reference `{{python_path}}` as a text-level placeholder. The orchestrator substitutes the absolute interpreter path from `plan_ops.py preflight --json`'s `python_path` field at dispatch time (see SKILL.md §"Python interpreter resolution"). Do NOT hardcode a specific interpreter path here; the resolver in `plan_ops.py:_resolve_python` is the single source of truth.

## `target_task_id` auto-injection rule (TASK-007, shared across templates)

`target_task_id` is a **first-class dispatch field** for every section that names a child plan file (Phase A-single, Phase B, Phase D-Codex, Phase D-Claude, Phase D.5, Phase B-rework, Phase B-narrow-remediation). The renderer (Codex wrapper `plan_codex_dispatch.py:render_implement_prompt` / `render_review_prompt`, AND the orchestrator-side Claude render path) MUST apply this rule uniformly:

1. **Detect heading count.** Count `### TASK-NNN:` H3 headings in the resolved child plan file via `plan_ops.count_task_headings(plan_text)`.
2. **>1 heading + `target_task_id` set →** emit ``Implement specifically `### TASK-NNN:` (this child plan file declares N `### TASK-NNN:` H3 headings; read only the matching block).`` as the **first instruction line** of the dispatch prompt (before any pre-read excerpts and before the implement / review body).
3. **1 heading + `target_task_id` set →** emit nothing extra. The heading is unambiguous; the injection would only add noise.
4. **>1 heading + `target_task_id` is `None` →** the renderer raises `plan_ops.MissingTargetTaskIdError`. The error envelope identifies the offending plan file. The orchestrator is contractually required to supply `target_task_id` for shared-file children — a missing value is a bug at the dispatch call site, not a runtime ambiguity for the agent to resolve.
5. **0 or 1 heading + `target_task_id` is `None` →** emit nothing (single-task plan files render unchanged; backward compatible with pre-TASK-007 dispatchers).

The single shared helper `plan_ops.render_target_task_id_injection(plan_text, target_task_id, plan_file=...)` encodes all four cases. Both the Codex wrapper and the Claude orchestrator-side render path call it; the rule stays in lockstep across the two render mechanisms. `build-tasks` emits an `extra-task-heading` warning whose message names `target_task_id` as the disambiguator the orchestrator MUST set on the chunk's dispatches.

## Phase A-single — plan-analyst per-child classifier (default)

Default Phase 1 invocation as of the per-task-dispatch refactor (v2), now dispatched via the v3 wrapper as of TASK-003 (`SKILL_bash_dispatch_migration`). The orchestrator emits one dispatch per child file that did NOT declare `**Agent:**` in its source markdown; when every child already declares an agent, Phase 1 skips this template entirely (see SKILL.md §Analysis (Phase 1) step 2). The orchestrator emits N of these dispatches as **N discrete `Bash` tool-use blocks inside a single assistant turn** — each invoking `plan_claude_dispatch.py run --input <payload.json>` — not as an array-prompt wrapped inside one tool call.

Wrapper dispatch, `agent: "plan-analyst"`, `overrides.model: "sonnet"` (narrower scope than the retired whole-plan opus dispatch — a single-child classification is within Sonnet's reliable envelope). The wrapper returns the v3 envelope on stdout `{schema_version, status, status_reason, agent, model, session_id, duration_ms, cost_usd, tokens, result, result_raw_truncated, stderr_tail, permission_denials, scope, trace, error}`; the orchestrator asserts `.status == "ok"` and reads the classifier reply from `.result`. Status `!= "ok"` halts with `run_end reason=analyst_invalid` (see SKILL.md §Step 2 malformed-reply handling).

Bash command template:

```
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input <payload.json>
```

Payload skeleton (`<payload.json>`, conforming to `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json`):

```json
{
  "schema_version": 1,
  "agent": "plan-analyst",
  "payload": {
    "plan_path": "<absolute child plan path>",
    "repo_root": "<repo_root>"
  },
  "output_instructions": {
    "format": "json",
    "schema_path": "plugins/plan-executor/scripts/schemas/claude_dispatch_output.json",
    "schema_inline": null,
    "max_bytes": 65536
  },
  "overrides": {
    "model": "sonnet",
    "timeout_sec": null,
    "tools_allowed_extra": null,
    "tools_disallowed_extra": null,
    "cwd": null
  },
  "guardrails": {
    "max_depth": 1,
    "cost_cap_usd": null,
    "network": "deny"
  },
  "trace": {
    "run_id": "<orchestrator run_id>",
    "parent_span_id": null,
    "depth": 0,
    "call_chain": ["orchestrator"]
  }
}
```

The agent-behavior body below is byte-identical to the pre-migration wording — only the **transport header** above (how to invoke + envelope handling) was rewritten by TASK-003.

<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->

> Classify exactly one task from the plan at `<absolute child plan path>`. Repo root: `<repo_root>`.
>
> Read the child plan file verbatim (it typically carries a single `### TASK-NNN:` H3 heading plus the standard metadata block — Status, Priority, Files, Dependencies, Test command, Acceptance criteria, Description, Implementation notes, Reversion guidance — but **may carry >1 H3 heading** when several sibling sub-tasks share a single child file; in that case the orchestrator passes `target_task_id` as a first-class dispatch field and the renderer auto-injects an "Implement specifically `### TASK-NNN:`" first-instruction line per the §`target_task_id` auto-injection rule above). Use the `claude` vs `codex` heuristics from your agent spec's classification rubric (scope ≤30 lines and ≤3 files plus a concrete test command → codex; multi-file coordination, async/routing/API contract changes, new module creation, priority `critical`, `Test command: none`, or underspecified acceptance criteria → claude). Do NOT emit a schedule, gaps, risks, or a batch table.
>
> You are classifying ONE task — return only the minimal JSON below. Do not re-validate structure, do not compute batches, do not surface cross-task gaps (`compute-schedule` handles DAG + file-disjointness downstream).
>
> **Output (required fenced `json` block, no prose outside it):**
>
> ```json
> {
>   "agent": "claude" | "codex",
>   "classification_reason": "<one-line justification, ≤10 words>"
> }
> ```
>
> Emit nothing else — no markdown report, no `tasks[]`, no `batches[]`, no `gaps[]`. Any additional fields or prose outside the fenced JSON block will be rejected by the orchestrator as a malformed classifier reply.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash.

## Phase A — plan-analyst whole-plan dispatch (LEGACY — retained for direct CLI callers)

**Legacy-only.** The orchestrator no longer dispatches this template by default. The per-task-dispatch refactor (v2) replaced whole-plan analyst dispatch with `plan_ops.py build-tasks` (deterministic fat-manifest synthesis) plus the per-child classifier fan-out above. This template is retained so direct CLI callers who still want a whole-plan analyst report (for offline inspection, debugging, or legacy integration) have a documented prompt. Do not wire it from SKILL.md.

> Analyze this plan and emit the structured schedule defined by your agent spec. The plan file is at `<absolute plan path>`. Repo root: `<repo_root>`.
>
> Read the plan verbatim, validate task headers / dependencies / files / acceptance criteria, classify each task as `claude` or `codex`, and produce the fenced JSON schedule block plus the markdown gap report. Do NOT modify any files.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash.

## Phase 1.5 — Codex plan review (pre-dispatch gate)

Dispatched after schedule persist, before any batch runs. Codex is the reviewer because the plan was authored by Claude/Opus (analyst); this is the independent pre-exec check. Skipped entirely if `--skip-plan-review` is set OR `codex_available=false` from preflight.

Bash command template:

```
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" plan-review \
  --plan-file <absolute plan path> \
  --schedule-file <absolute schedule path> \
  --plans-dir <plan_dir> \
  --repo-root <absolute repo root> \
  --timeout 180 \
  [--allow-gaps]
```

Timeout **180s**. Wrapper captures a pre-dispatch baseline and performs delta-bounded cleanup; the plan-review path runs Codex under `-s read-only` sandbox (advisory) because Codex has no legitimate reason to write during a plan-level review. Any sandbox escape surfaces in `extra.sandbox_escape_detected` without changing the outcome — this matches the `review` subcommand's observe-only semantics.

**`--allow-gaps` (TASK-003).** Pass-through of the orchestrator's `--allow-gaps` opt-in. When supplied AND the persisted schedule's `gaps[]` is non-empty with every entry's `severity == "soft"` AND the schedule has no structural violations (`outcome == "needs-enrichment"` — `"valid"` by contract requires empty `gaps[]`, and missing/unknown outcomes suppress the demotion), the wrapper appends this literal demotion clause to the rendered Codex prompt (just before the "Verdict vocabulary" block):

> Operator override (--allow-gaps): the user explicitly opted in to soft gaps. The persisted schedule's gaps[] contains only soft-severity entries and no structural violations. If schedule_ok would otherwise be false for this reason alone, demote the verdict from `needs-replan` to `approved-with-notes` and mention that demotion in the `summary`. Hard gaps or structural violations are not covered by this override.

The demotion yields `approved-with-notes`, which routes straight to Phase 2 and bypasses the `plan-author` auto-revise dispatch. **Hard gaps still trigger plan-author auto-revise** — any `severity: "hard"` entry (or any structural schedule violation) suppresses the demotion clause entirely, so the reviewer selects the standard verdict and `needs-replan` routes through the normal Phase 1.5a path. The wrapper never mutates the persisted schedule; demotion is a pure function of the prompt inputs.

Wrapper emits one JSON envelope on stdout with `outcome ∈ {success, failure, timeout, parse_error}`. On `success`, `parsed` conforms to `scripts/codex_plan_review_schema.json`:

```json
{
  "plan_file": "<basename>",
  "verdict": "approved | approved-with-notes | needs-replan",
  "findings": [
    {
      "severity": "critical | important | minor",
      "blocking": true,
      "section": "<where in the plan>",
      "concern": "<what is wrong or risky>",
      "suggested_change": "<how to fix>"
    }
  ],
  "notes": ["<non-blocking observation>", "..."],
  "schedule_ok": true,
  "summary": "<one-paragraph rationale>"
}
```

Orchestrator routes by verdict (see SKILL.md §Phase 1.5). On `needs-replan` (when auto-revise is on — default), dispatch `plan-author` to apply findings to the plan file in place, then re-run Phase 1 end-to-end (`build-tasks` → classifier fan-out → `write-schedule` + `schedule-valid` gate) for structural re-validation of the revised plan, then re-run plan-review. A second `needs-replan` halts with `run_end reason=plan_review_failed`; no batches execute. If `--no-auto-revise` is set, the `needs-replan` route halts immediately with `run_end reason=plan_review_failed` instead of dispatching the author. (The legacy whole-plan `plan-analyst` re-dispatch is retained for back-compat but is NOT the post-author re-validation path anymore — TASK-005 replaced it with the full Phase 1 re-run.)

## Phase 1.5-Claude — plan-reviewer dispatch (claude_only path)

Dispatched in place of the Codex wrapper above whenever `claude_only=true` (bound at Phase 0 preflight from `--claude-only OR (codex_available == false)` per SKILL.md §Pre-flight). The verdict-routing ladder, `--codex-plan-review-binding` mutex, the auto-revise `plan-author` path, and the `--allow-gaps` demotion all consume the parsed verdict — they are agnostic to the dispatch mechanism. The only behavioral differences vs the Codex wrapper path are (a) the dispatch is an Agent invocation rather than a `plan_codex_dispatch.py` shell-out, and (b) the run-log events carry `reviewer:"claude"` instead of `reviewer:"codex"`.

Agent dispatch, `subagent_type: "plan-reviewer"`, `model: "sonnet"`. The agent produces one markdown report whose body concludes with a single fenced ```json block conforming to `scripts/codex_plan_review_schema.json` (the same schema the Codex wrapper validates against). The orchestrator pipes the full report through `parse-plan-review-report --stdin --from-claude --json` to extract the verdict; the `--from-claude` flag tells the parser to treat stdin as the bare `parsed` payload (no wrapper envelope).

> Review the persisted schedule for this plan. The plan was authored by a peer analyst and decomposed into a fat manifest by `plan_ops.py build-tasks`; you are an independent pre-dispatch reviewer working from the schedule JSON alone.
>
> Dispatch inputs:
>
> - `plan_path`: `<absolute plan path>` (the directory containing `00_INDEX.json` and the per-task child files — read for context only, never edit)
> - `schedule_path`: `<absolute schedule path>` (your primary input — read this for the unified fat `tasks[]` array; never edit)
> - `repo_root`: `<absolute repo root>`
> - `plan_basename`: `<plan_basename>` (the directory basename; emit this verbatim in your output `plan_file` field)
> - `findings_count`: `<N>` (length of the prior pass's `findings[]` if this is a re-dispatch — `0` on the first pass; advisory only, your output `findings[]` is not bounded by it)
> - `allow_gaps_demotion`: `<true|false>` (when `true`, apply the demotion clause from your agent spec)
>
> Cross-plan dependency resolution has already been verified by the orchestrator in Phase 0 preflight. Do not check or report on cross-plan dependencies. Focus only on schedule structure, task intent, and coordination risk expressed within the supplied schedule.
>
> Your job is to determine whether this plan is workable to execute, not whether it is perfect. Apply the review standard, output discipline, "Check specifically" rubric, and verdict vocabulary documented in your agent spec (`plan-reviewer.md`). Emit a markdown report whose body concludes with a single fenced ```json block conforming to `scripts/codex_plan_review_schema.json` (`{plan_file, verdict ∈ {approved, approved-with-notes, needs-replan}, findings[], notes[], schedule_ok, summary}`). Each finding MUST include `target_task_id: string | null` (task id or `null` for schedule-level findings) — the downstream triage + plan-author dispatchers route per-child based on this field.
>
> `plan_file` MUST equal the `plan_basename` value above; the parser pins it to that value.
>
> **Allow-gaps demotion clause (rendered ONLY when `allow_gaps_demotion: true`):**
>
> > Operator override (--allow-gaps): the user explicitly opted in to soft gaps. The persisted schedule's gaps[] contains only soft-severity entries and no structural violations. If `schedule_ok` would otherwise be false for this reason alone, demote the verdict from `needs-replan` to `approved-with-notes` and mention that demotion in the `summary`. Hard gaps or structural violations are not covered by this override.
>
> When `allow_gaps_demotion: false` the orchestrator omits this clause entirely; apply the standard verdict vocabulary unchanged.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash. Do NOT edit the plan or the schedule. Do NOT run tests. Do NOT read source code. Do NOT mutate the git index (read-only git is fine).

The orchestrator pipes the agent's markdown report through:

```bash
printf '%s' "<agent_output_extracted_json>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" \
  parse-plan-review-report --stdin --from-claude --json
```

`parse-plan-review-report --from-claude` validates the bare `parsed` payload against `codex_plan_review_schema.json` and emits the same `{plan_file, verdict, findings_count, findings, notes, schedule_ok, summary}` result the Codex path emits, so the Phase 1.5 verdict-routing table and the Phase 1.5.5 triage entry continue to consume the same parser output regardless of which reviewer mechanism produced it. Outcome on this path is always `success` — Agent-side errors bubble up as Agent dispatch failures, not envelope-level outcomes; treat such failures the same way the Codex path treats `outcome ∈ {timeout, parse_error, failure}` (degrade to `plan_review_skipped {reason:"claude_review_failure"}` for routing rather than blocking execution).

## Phase 1.5a — plan-author dispatch (needs-replan auto-revise)

Dispatched only when the first Phase 1.5 Codex `plan-review` returns `needs-replan` AND auto-revise is on (default; disabled by `--no-auto-revise`). The author revises the plan text in place so a second review can proceed. Agent dispatch, `subagent_type: "plan-author"`, `model: "opus"`.

**Per-finding fan-out (TASK-007).** Phase 1.5a is no longer a single author dispatch over the whole plan. For each finding in the payload (filtered on `partial-agreement` to `load_bearing` indices, full on `needs-rework`), the orchestrator dispatches a separate `plan-author` agent. Each dispatch carries exactly ONE finding plus the inputs needed to locate its edit target; the author's write scope is locked to that single target. **The one-dispatch-per-finding rule applies uniformly to BOTH task-targeted AND schedule-level findings — schedule-level findings are NOT batched into a single dispatch.** Resolution per finding depends on `target_task_id`:

- **Task-targeted (`target_task_id="NNN"`)** — triple is `{finding, target_task_id, child_plan_file}`. The orchestrator resolves `finding.target_task_id → child_plan_file` via the schedule's `tasks[].plan_file`; the author edits that one child file in place. `roster_file` is absent from this dispatch.
- **Schedule-level (`target_task_id=null`)** — triple is `{finding, target_task_id=null, roster_file}`, with `child_plan_file` absent or explicitly `null`. The orchestrator passes `roster_file=<plan_dir>/00_INDEX.json` (absolute path to the schedule roster) so the author has a concrete file target. Each schedule-level finding still gets its own dispatch. The author's write scope is `roster_file` (roster edit) OR empty (emit `files_edited: []` with a justification note). If multiple schedule-level findings target the roster, each still dispatches separately; the author may touch `roster_file` idempotently across those dispatches.

Orchestrator-side log emission wraps each per-child dispatch:

```bash
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_author_start \
  --fields-json '{"run_id":"<id>","plan_file":"<basename>","findings_count":<N>}' --json

# Agent dispatch (plan-author, model: opus) using the template below.

{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" log-event \
  --event plan_author_done \
  --fields-json '{"run_id":"<id>","plan_file":"<basename>","files_edited":[...],"findings_actioned":[...],"findings_skipped":[...]}' --json
```

Two visually distinct prompt variants render based on `target_task_id`. The orchestrator picks ONE per dispatch; never render both in the same dispatch. The variants differ in their input line block AND in the prose describing the edit target — operators reading the dispatched prompt can tell at a glance which form was selected.

---

### Variant A — Task-targeted dispatch (`target_task_id != null`)

Renders when the finding carries a non-null `target_task_id`. Inputs: `child_plan_file` (required), `target_task_id` (required). `roster_file` is NOT rendered on this path.

> Apply a Codex plan-review finding to the child plan at `<absolute child_plan_file path>`. The first plan-review pass returned `needs-replan`; your job is to revise THIS child plan file so a second review can proceed. Your edit target is exactly one `### TASK-NNN:` sub-heading block in this file.
>
> Dispatch inputs:
>
> - `child_plan_file`: `<absolute child_plan_file path>` (edit this file in place)
> - `target_task_id`: `<target_task_id>` (e.g., `"002"`)
>
> Codex finding (single entry from `parsed.findings[]` of the wrapper envelope):
>
> ```json
> <codex_finding_json>
> ```
>
> Codex summary (verbatim from `parsed.summary`):
>
> ```
> <codex_summary>
> ```
>
> Analyst annotations (verbatim, may be empty):
>
> ```json
> <analyst_annotations_json>
> ```
>
> Apply a minimum-change edit to resolve the finding. Preserve untouched sections verbatim — do not re-flow or re-format text the finding does not reference. If the finding is vague, contradictory, or contradicts the child's existing acceptance criteria, skip it with a written rationale in your report rather than invent intent. Your write scope is **exactly the child plan path above** — do NOT edit any other file, including other child plans under the same directory, the schedule roster at `00_INDEX.json`, source code, tests, or configuration.
>
> Emit a markdown report with three sections: `**Findings actioned:**` (one bullet per finding applied, with the file:line anchor), `**Findings skipped:**` (one bullet per finding not applied, with rationale), `**Files edited:**` (the list of paths you touched — typically just the one input child plan).
>
> The `--no-auto-revise` orchestrator flag exists for users who prefer to apply revisions by hand; if you are seeing this prompt, auto-revise is on and you are expected to revise.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

---

### Variant B — Schedule-level dispatch (`target_task_id == null`)

Renders when the finding carries `target_task_id=null`. Inputs: `roster_file` (required — absolute path to the schedule's `00_INDEX.json`), `target_task_id=null`. `child_plan_file` is NOT rendered on this path (there is no individual child file target for schedule-level concerns).

> Apply a Codex plan-review **schedule-level** finding. The first plan-review pass returned `needs-replan` with a concern that targets the schedule as a whole (batch ordering, roster composition, cross-cutting structural issue) rather than a single `### TASK-NNN:` child block. This is a schedule-level finding — there is no individual child file target. Your allowed edit surface is the schedule roster file OR empty (no file edit).
>
> Dispatch inputs:
>
> - `roster_file`: `<absolute path to 00_INDEX.json>` (the schedule roster — edit this file in place only if a roster change resolves the finding)
> - `target_task_id`: `null` (schedule-level — no individual child file target)
> - `child_plan_file`: (absent on this path — do NOT edit any `### TASK-NNN:` child file)
>
> Codex finding (single entry from `parsed.findings[]` of the wrapper envelope):
>
> ```json
> <codex_finding_json>
> ```
>
> Codex summary (verbatim from `parsed.summary`):
>
> ```
> <codex_summary>
> ```
>
> Analyst annotations (verbatim, may be empty):
>
> ```json
> <analyst_annotations_json>
> ```
>
> This is a schedule-level finding — no individual child file targets. You may edit `roster_file` (the absolute path named above) OR emit `files_edited: []` with a justification note if no roster change is warranted for this finding. Do NOT edit any `### TASK-NNN:` child file, source code, tests, or configuration on this path.
>
> Apply a minimum-change roster edit (adjust `chunks[]` ordering, `depends_on` wiring, or metadata fields) when the finding translates concretely into a roster change. Preserve untouched sections of the roster verbatim — do not re-flow or re-format `chunks[]` entries the finding does not reference. If the finding is vague, contradictory, or does not translate into a concrete roster change, skip it with a written rationale in your report under `**Findings skipped:**` rather than invent intent; in that case `**Files edited:**` MUST be the empty list `[]`.
>
> Emit a markdown report with three sections: `**Findings actioned:**` (one bullet per finding applied, with the file:line anchor into `roster_file`), `**Findings skipped:**` (one bullet per finding not applied, with rationale), `**Files edited:**` (`[roster_file]` for a roster edit, or `[]` for a no-op).
>
> The `--no-auto-revise` orchestrator flag exists for users who prefer to apply revisions by hand; if you are seeing this prompt, auto-revise is on and you are expected to revise.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

After all per-child authors return, the orchestrator re-runs Phase 1 end-to-end (`build-tasks` → classifier fan-out → `write-schedule` + `schedule-valid` gate) for structural re-validation of the revised plan. If the second-pass `build-tasks` surfaces fatal `errors[]` the orchestrator halts with `run_end reason=plan_review_failed reason_detail=author_introduced_structural_defect`. Otherwise (clean tasks, or tasks with warnings — same allow-gaps / binding / analyst-triage routing as the first pass) Codex `plan-review` runs once more; that second verdict is binding. The legacy whole-plan `plan-analyst` re-dispatch is retained for back-compat but is NOT the post-author re-validation path anymore.

## Phase 1-triage / Phase 1.5.5 — plan-review-triage dispatch (source-parameterized)

Dispatched at TWO orchestrator seams sharing one template, one agent, one parser, one schema:

- **Phase 1-triage (analyst-source).** After Phase A (plan-analyst) returns `outcome=needs-enrichment`, before any halt or `--allow-gaps` demotion path. Skipped when `--analyst-binding` is set (halt with `run_end reason=plan_analyst_failed`) or `--allow-gaps` is set (today's pre-triage short-circuit preserved).
- **Phase 1.5.5 (Codex-plan-review-source).** After Phase 1.5 (Codex plan-review) returns `verdict=needs-replan`, before the Phase 1.5a `plan-author` auto-revise dispatch. Skipped when `--codex-plan-review-binding` is set (halt with `run_end reason=plan_review_failed`) or `--no-auto-revise` is set (today's halt behavior preserved).

One template, one `render(templates.PlanTriage, source=<src>, ...)` call from the orchestrator; the `{source}` placeholder discriminates the embedded evidence block, the verification-move examples, and the analyst-only same-family caveat. Agent dispatch, `subagent_type: "plan-review-triage"`, `model: "sonnet"` (parity with Phase D.5 — NOT opus).

Structurally this is a plan-level clone of Phase D.5 (`dispatch-templates.md` lines 218-280): the verdict rubric, the dismissal-evidence gate, and the output shape port across verbatim, re-scoped from "diff + task block" to "plan + schedule + source-specific evidence".

> Scope: `plan_path=<absolute plan path>`, `schedule_path=<absolute schedule path>`, `source={source}`, `findings_count=<N>`.
>
> {source} (a plan-stage reviewer) flagged the plan and you are the third-opinion adjudicator. Independently review the plan against the reviewer's evidence array and decide whether each item is load-bearing (a ship-blocker) or can be safely dismissed. You are read-only on both the plan and the schedule — do NOT edit either file.
>
> Plan (verbatim):
>
> ```markdown
> <full plan text>
> ```
>
> **Reviewer evidence (source-discriminated):**
>
> *If `source == codex-plan-review`:*
>
> > Codex findings (derived from `parsed.findings` of the Phase 1.5 envelope). Each finding carries `{severity, blocking, section, concern, suggested_change, target_task_id, source_index}` — `source_index` is the finding's 0-based position in the ORIGINAL Codex `parsed.findings[]` array (pre-sort), and the remaining fields are the TASK-007 per-child targeting shape: `target_task_id` names the child file the downstream `plan-author` will edit (or `null` for a schedule-level finding that targets `00_INDEX.json` or no file at all):
> >
> > ```json
> > <codex_findings_json>
> > ```
> >
> > **Findings are already presorted (TASK-007).** The orchestrator pre-sorts this array by (a) `blocking=true` first, (b) then `severity=critical`, (c) then `severity=important`, (d) then `severity=minor`, with stable source-order tie-breaking. The ordering is produced by `plan_ops.py order-triage-findings` before this template is rendered; you do NOT need to re-sort. Items with `target_task_id=null` are schedule-level concerns — evaluate them against the schedule JSON + roster rather than a single task block.
> >
> > **Index contract — use `source_index`, NOT array positions.** Your output indices (`load_bearing` / `dismissed`) MUST reference the `source_index` values carried on each finding above, NOT positions in this presorted array. `source_index` corresponds to the original Codex `parsed.findings[]` order (the downstream parser `parse-plan-review-triage-report --findings-count <N>` validates indices against that original array). Example: if the presorted array begins with a finding whose `source_index` is `2`, emitting `load_bearing: [0]` is WRONG — emit `load_bearing: [2]` to refer to that finding.
> >
> > Codex summary (verbatim from `parsed.summary`):
> >
> > ```
> > <codex_summary>
> > ```
>
> *If `source == plan-analyst`:*
>
> > Analyst gaps (verbatim from the analyst's outcome payload `gaps[]`; each entry carries at minimum `location`, `severity`, `missing_field` or `detail`):
> >
> > ```json
> > <analyst_gaps_json>
> > ```
> >
> > Analyst outcome narrative (verbatim — `outcome`, `summary`, and any other non-array fields the analyst emitted):
> >
> > ```json
> > <analyst_outcome_json>
> > ```
> >
> > Analyst summary (verbatim from the analyst outcome `summary` field):
> >
> > ```
> > <analyst_summary>
> > ```
>
> Return your verdict (`ship | ship-with-fixes | partial-agreement | needs-rework`) and a brief justification.
>
> **Verdict decision rubric — pick the verdict that matches the split, not a stronger one (shared across sources):**
>
> 1. `ship` — you disagree with the reviewer entirely; none of the items are load-bearing. Orchestrator proceeds to the next phase with a bare `[plan-review-disagreement]` (Codex source) or `[analyst-triage-disagreement]` (analyst source) tag in the run summary.
> 2. `ship-with-fixes` — you disagree about blockers; any residual concerns are minor notes. Orchestrator proceeds to the next phase with items carried to the summary's "Plan review notes" (Codex source) or "Analyst triage notes" (analyst source) section.
> 3. `partial-agreement` — the items split cleanly: at least one is load-bearing AND at least one can be safely dismissed. Use this verdict ONLY when both buckets are non-empty. Dispatches `plan-author` on the load-bearing subset only; dismissed indices are recorded verbatim in the run summary.
> 4. `needs-rework` — all items are load-bearing. Dispatches `plan-author` with the full evidence array (today's auto-revise behavior on the Codex path; a new auto-enrichment path on the analyst path).
>
> **Dismissal-evidence gate (required before labeling any item `dismissed`).** The decision rubric above answers *what verdict fits the split*; the dismissal-evidence gate answers *do you know the item is wrong*. Before labeling a reviewer item `dismissed`, you MUST cite one of:
>
> 1. **A concrete verification move** against the plan text or schedule (v1: NO source-code reading).
> 2. **An explicit spec contradiction:** the plan's stated Acceptance criteria or Implementation Playbook mandates the behavior the reviewer objects to.
>
> Unverified dismissals MUST downgrade to a `minor-findings`-style note and the item still surfaces in the summary rather than being buried.
>
> *Source-specific verification moves — if `source == codex-plan-review`:*
>
> - Re-read the cited plan section and confirm Codex's finding misreads it.
> - Check the schedule's `tasks[]` for the claimed missing entry.
> - Verify the Acceptance criteria bullet Codex says is absent is actually present.
>
> *Source-specific verification moves — if `source == plan-analyst`:*
>
> - Re-read the task block cited by `gaps[i].location` and confirm the `missing_field` is in fact present.
> - Check the plan's narrative Dependencies prose for an implicit reference the analyst missed.
> - Verify the `severity:'hard'` classification against what the orchestrator would actually halt on — soft gaps with `severity:'hard'` mis-tagging are dismissible.
>
> *Same-family caveat (included ONLY when `source == plan-analyst`; omitted for `source == codex-plan-review`):*
>
> > **Same-family caveat.** The analyst is Claude/Opus and you (the triage) are Claude/Sonnet — same-family grading itself. This is epistemically weaker than cross-family review. The dismissal-evidence gate above is the primary mitigation: you MUST cite plan text or schedule evidence, not vibe-dismiss. An unverified dismissal on the analyst path is epistemically weaker than on the Codex path and MUST downgrade per the rule above. If in doubt, prefer surfacing the item as a minor note over silently dismissing it.
>
> **Hard rules for `partial-agreement`:**
>
> - Emit this verdict only when BOTH `load_bearing` and `dismissed` are non-empty. If every item is load-bearing → use `needs-rework`. If no item is → use `ship-with-fixes`. A unanimous split (empty bucket on either side) is a contract violation — the parser rejects it with `partial-agreement-invalid-split`.
> - Indices in `load_bearing` and `dismissed` MUST be 0-based and in range `[0, findings_count)`, and the two buckets MUST be disjoint. **When `source == codex-plan-review`, use the `source_index` value carried on each presorted finding** (the original Codex `parsed.findings[]` position). When `source == plan-analyst`, use positions into the `analyst_gaps_json` array above (analyst gaps are not presorted and carry no `source_index`). There is no `id` field on items; `source_index` (Codex path) or array position (analyst path) is the reference.
>
> **Output shape (shared across sources — the source discriminator lives in the dispatch input and the orchestrator's run-log event, NOT in your output):**
>
> - For `ship` / `ship-with-fixes` / `needs-rework`:
>
>   ```json
>   {"verdict": "ship", "summary": "<one-line justification>"}
>   ```
>
> - For `partial-agreement` (evidence array of length 4, indices 0..3):
>
>   ```json
>   {
>     "verdict": "partial-agreement",
>     "load_bearing": [0, 2],
>     "dismissed": [1, 3],
>     "summary": "<one-line justification naming which items fall in which bucket>"
>   }
>   ```
>
> **Hard rules (v1):**
>
> - No source-code reading. The triage adjudicates against the plan prose + schedule only.
> - No Edit / Write / Agent tools. You are read-only on the plan and the schedule.
> - No plan-file mutation. No schedule-file mutation.
> - `load_bearing` / `dismissed` indices MUST be into the reviewer evidence array you were given — on the `codex-plan-review` source, use the per-finding `source_index` value; on the `plan-analyst` source, use the 0-based position in `analyst_gaps_json`. Do NOT fabricate indices or reference items not in that array.
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Bash.

The orchestrator pipes the triage subagent's markdown report through `parse-plan-review-triage-report --stdin --source <src> --findings-count <N> --json` to extract the verdict and index buckets; routing is by the parser's output `{verdict, load_bearing, dismissed, summary, source, findings_count}`. See SKILL.md §Phase 1-triage and §Phase 1.5.5 for the two insertion-point wirings.

## Phase B — plan-implementer dispatch (Claude tier)

Default Phase B implementer dispatch, migrated to the v3 wrapper as of TASK-004 (`SKILL_bash_dispatch_migration`). The orchestrator emits the dispatch as a `Bash` tool-use block invoking `plan_claude_dispatch.py run --input <payload.json>` (formerly `Agent(subagent_type: "plan-implementer", model: "opus", ...)`). The wrapper returns a v3 envelope on stdout `{schema_version, status, status_reason, agent, model, session_id, duration_ms, cost_usd, tokens, result, result_raw_truncated, stderr_tail, permission_denials, scope, trace, error}`; the orchestrator asserts `.status == "ok"` and reads the implementer outcome + report from `.result` per the **Phase B classify** extraction shim in `SKILL.md`. Implementer outcome vocabulary (`success | partial | failed | plan-incorrect | blocked | malformed`) is preserved verbatim; `malformed` is emitted by the wrapper when transport succeeded but `.result` failed schema validation against the implementer result schema.

Bash command template:

```
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" run --input <payload.json>
```

Payload skeleton (`<payload.json>`, conforming to `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json`):

```json
{
  "schema_version": 1,
  "agent": "plan-implementer",
  "variant": "default",
  "payload": {
    "plan_path": "<absolute child plan path>",
    "repo_root": "<repo_root>",
    "task_id": "<NNN>",
    "target_task_id": "<NNN-or-null>",
    "starting_sha": "<orchestrator starting_sha>",
    "analyst_annotations": "<analyst_annotations_json-or-null>",
    "pre_read_excerpts": "<rendered pre-read excerpts-or-null>"
  },
  "output_instructions": {
    "format": "json",
    "schema_path": "tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json",
    "schema_inline": null,
    "max_bytes": 65536
  },
  "overrides": {
    "model": "opus",
    "timeout_sec": null,
    "tools_allowed_extra": null,
    "tools_disallowed_extra": null,
    "cwd": null
  },
  "guardrails": {
    "max_depth": 1,
    "cost_cap_usd": null,
    "network": "deny"
  },
  "trace": {
    "run_id": "<orchestrator run_id>",
    "parent_span_id": null,
    "depth": 0,
    "call_chain": ["orchestrator"]
  }
}
```

`payload.variant="default"` is the template selector; `"rework"` selects the Phase B-rework body, `"role-swap"` selects the Phase D.2b body. No new wrapper subcommand — the variant is carried in the payload. The wrapper extracts `.scope.scope_violation_detected` and `.scope.scope_misreport_detected` from the v3 §7 scope sub-object; both surface as top-level envelope fields the orchestrator reads directly to gate commits (existing rules apply). The required report sections (`Plan adaptations`, `Concerns for reviewer`, `On-failure revert`) surface inside `.result.report` as structured arrays (`plan_adaptations[]`, `concerns_for_reviewer[]`, `on_failure_revert`); the orchestrator reads only those fields plus commit-scope metadata and does NOT ingest `.result_raw_truncated` on the success path. The agent-behavior body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical to the pre-migration wording — only the transport header above (how to invoke + envelope handling) was rewritten by TASK-004.

<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->

**`target_task_id` (TASK-007).** First-class dispatch field. When the resolved child plan file carries >1 `### TASK-NNN:` H3 heading (shared-file siblings), the orchestrator's render path prepends ``Implement specifically `### TASK-NNN:` ...`` as the first instruction line per §`target_task_id` auto-injection rule. Single-heading files render unchanged. Omitting `target_task_id` for a >1-heading file is a render-time error.

**Pre-read excerpts (TASK-009).** When the task block declares `**Read targets:**` (line ranges) or `**Symbol targets:**` (symbol extraction) optional fields, the orchestrator resolves them up-front and prepends a `## Pre-read excerpts` section to the dispatch prompt. The resolution helper:

```
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" resolve-read-targets \
  --task-file <absolute path to task-block markdown> --json
```

emits `{reads, symbols, errors}`. The orchestrator either renders the structured output via `plan_ops.render_pre_read_excerpts(resolved)` (canonical formatter) or substitutes the rendered string at the `{{pre_read_excerpts}}` interpolation point below. When the task carries no targets the field is empty and the section is omitted entirely (no empty heading).

> {{pre_read_excerpts}}
>
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
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `{{python_path}} ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec. Do not commit. Do not use `git stash`.
>
> The `## Pre-read excerpts` block above (when present) is a seed, not a gag — you MAY issue additional `Read` calls with different offsets when the excerpts are insufficient.
>
> **Coupling check (TASK-002, mandatory).** Before declaring complete, run the Step 4.5 coupling-detector grep from your agent spec when the AC mentions any of: a `_RE`-suffixed regex name, an `ALLOWED_`-prefixed allowlist constant, a `**...**` markdown-header pattern, or any symbol that appears ≥2 times in the touched files. Report the result in the mandatory `**Coupling check:**` block of your report. When no trigger fires, emit `**Coupling check:** not applicable — <one-line reason>` so the cross-reviewer can confirm the check was considered. The block carries `pattern_family`, `siblings_checked` (list of `{file, line, disposition}`), with `disposition ∈ {uniformly_applied, excluded_with_reason, not_applicable}`.
>
> **N-state contract check (TASK-006, optional).** Sibling of the Coupling check above — both refine AC reading. When the AC enumerates ≥3 distinct outcome states for a helper, function, branch, or test set, the return shape MUST distinguish all N enumerated states (do NOT collapse failure states into a single sentinel like `None` or empty string). Worked example: TASK-028's `_unwrap_cli_envelope` 3-state contract (`unwrapped` / `unexpected` / `non-json`) was correctly modeled as `_classify_cli_envelope(stdout) -> tuple[str, object]` returning `("unwrapped", str)`, `("unexpected", body)`, or `("non-json", None)` — a tuple shape, not `str | None`. When the trigger fires, emit the optional `**Outcome states:**` report block carrying `count` (int) and `branches` (list of `{name, return_value, condition}`); omit the block entirely when the trigger does not fire. This check does NOT change the implementer verdict allowlist or D.2a routing.
>
> **Parallel-mode caveat:** other implementers may be running concurrently on disjoint files. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

## Phase B-Codex — implement via wrapper (Codex tier)

Bash command template — orchestrator issues this directly, wrapper fully owns Codex session lifecycle:

```
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" implement \
  --plan-file <absolute plan path> \
  --task-id <NNN> \
  --repo-root <absolute repo root> \
  [--target-task-id <NNN>]   # required when child plan declares >1 `### TASK-NNN:` H3 heading (TASK-007)
```

**`target_task_id` (TASK-007).** First-class dispatch field for shared-file children. The wrapper's `render_implement_prompt` calls `plan_ops.render_target_task_id_injection(...)` and prepends the disambiguator line per §`target_task_id` auto-injection rule. Single-heading child files render unchanged; omitting the flag for a >1-heading file emits a `failure` envelope with a `MissingTargetTaskIdError`-derived message.

The wrapper derives its internal timeout from `len(task["files"])` per the implement formula in **SKILL.md §Bash-call idioms** (`max(300s, 60 * len(files))`); pass `--timeout N` only when the operator has a concrete reason to override. The wrapper captures a pre-dispatch baseline snapshot immediately before invoking Codex and cleans up only the delta against it (plus a protected-path allowlist) — never repo-wide. The wrapper emits a single JSON envelope on stdout with `outcome ∈ {success, failure, timeout, parse_error, scope_violation, dry_run}`; see `scripts/plan_codex_dispatch.py` for the full schema. The envelope carries `effective_timeout: int` reporting the cap actually used, and on `outcome: "timeout"` it carries `baseline_error: str | null` capturing the `_snapshot_baseline` failure message (truncated to 200 chars) when baseline capture failed. Orchestrator treats any outcome ≠ `success` as a fallback trigger (fallback = re-dispatch to Claude via the Phase B template above).

**Pre-read excerpts (TASK-009).** The Codex wrapper auto-resolves `**Read targets:**` / `**Symbol targets:**` from the task block and embeds the rendered `## Pre-read excerpts` section at the top of Codex's prompt. No orchestrator-side templating is required; the excerpts surface inside the wrapper's prompt construction in `render_implement_prompt`. The same auto-resolution runs for `Phase D-Codex` reviews via `render_review_prompt`.

## Phase D-Codex — review via wrapper (reviews Claude-implemented work)

**`target_task_id` (TASK-007).** Pass `--target-task-id <NNN>` to the wrapper for shared-file children (child plan declares >1 `### TASK-NNN:` H3 heading). The wrapper's `render_review_prompt` calls `plan_ops.render_target_task_id_injection(plan_text, target_task_id, plan_file=...)` and prepends the disambiguator line per §`target_task_id` auto-injection rule. Omitting `--target-task-id` when the child file carries >1 heading raises `MissingTargetTaskIdError` and the wrapper emits a `failure` envelope.

Bash command template:

```
{{python_path}} "${CLAUDE_PLUGIN_ROOT}/scripts/plan_codex_dispatch.py" review \
  --plan-file <absolute plan path> \
  --task-id <NNN> \
  --repo-root <absolute repo root> \
  --files <comma-separated files_changed from implementer> \
  --review-focus bugs \
  [--target-task-id <NNN>]   # required when child plan declares >1 `### TASK-NNN:` H3 heading (TASK-007)
```

The wrapper derives its internal timeout from `len(files)` per the review formula in **SKILL.md §Bash-call idioms** (`max(180s, 30 * len(files))`); pass `--timeout N` only when the operator has a concrete reason to override. The envelope carries `effective_timeout: int` reporting the cap actually used. Wrapper captures a pre-dispatch baseline and performs delta-bounded post-review cleanup against it; any sandbox escape surfaces in `extra.sandbox_escape_detected` without changing outcome. Wrapper embeds the task-scoped diff (`git diff HEAD -- <files>`) — per-batch interleaving keeps that diff scoped exclusively to this task's work.

### Verdict decision ladder (Codex reviewer calibration)

When you render the reviewer prompt for Codex, include this guidance verbatim before the schema reference. Codex historically overuses `needs-rework` on advisory nits; the ladder below reserves `needs-rework` for actual ship-blockers.

Decide your verdict using this ladder in order. Stop at the first rung that fits — do NOT escalate to the next rung unless the criterion is actually met.

1. **`clean`** — no issues, or only forward-looking suggestions that a human reviewer would file as follow-ups without asking the author to revise this commit. A clean scope check, acceptance criteria satisfied, tests pass.
2. **`minor-findings`** — issues a human reviewer would merge with a follow-up note rather than block on. Examples: style drift, typos, out-of-date comment, non-load-bearing naming choice, unused import, a docstring that undersells the code, a log message that could be clearer.
3. **`needs-rework`** — contract violation, acceptance-criterion miss, semantic bug, missing test for a declared verification criterion, or scope inflation past the task's file allow-list. This verdict returns work to the implementer; use it only when a human reviewer would block merge on the finding alone.

Heuristic when unsure: ask "would a human reviewer block merge on this finding alone?" If no → `minor-findings`. If yes → `needs-rework`. Do not bundle several nits together and escalate their sum to `needs-rework`; list each as a minor finding instead.

**Evidence gate (required before assigning `needs-rework`).** The heuristic answers *how severe if real*; the evidence gate answers *do you know it's real*. Before assigning `needs-rework` for a claimed semantic bug, contract violation, or acceptance-criterion miss, you MUST cite a concrete observation: a reproduced failure, a traced control-flow path through the cited symbol, or a cited invariant violation in the diff. A finding phrased as *"if X is true, then..."* or *"this is only safe if..."* that you did not verify is a hypothesis, not an observation. In-bounds verification moves for the Codex sandbox: run the task's declared test command (`pytest -k <name>`, etc.), read the cited symbol in the repo, trace a short control-flow path by hand. Out of bounds: spinning up external services. **Downgrade rule:** if you cannot verify with those moves, downgrade to `minor-findings` phrased as a question. Hypotheses still surface; they just don't gate the commit.

Worked examples (terse, synthetic):

- Finding: "`# TODO: refactor this later` comment in `foo.py:42` is stale; the refactor already happened." Verdict: **`minor-findings`**. Justification: outdated comment, no behavior impact, trivial follow-up.
- Finding: "Acceptance criterion V2 requires a regression test covering the empty-input branch; the diff adds the branch but no test asserts it." Verdict: **`needs-rework`**. Justification: declared verification criterion is unmet — a ship-blocker.
- Finding: "Transitive closure may loop forever on cycles; cycle check only runs after." Verdict without verification: **`minor-findings`** phrased as a question. Justification: hypothesis — tracing `001→002→001` by hand or running the cycle test would have confirmed or refuted it. When unverified, downgrade and ask.

Wrapper returns `parsed.verdict ∈ {clean, minor-findings, needs-rework}` per `scripts/codex_review_schema.json`; findings carry `severity`, `confidence`, `file`, `line`, `issue`, and `suggested_fix`, plus top-level `notes[]` for non-blocking observations. Orchestrator routes by verdict.

## Phase D-Claude — code-reviewer on Codex work

**`target_task_id` (TASK-007).** First-class dispatch field. When the resolved child plan file carries >1 `### TASK-NNN:` H3 heading, the orchestrator's render path prepends ``Reviewing specifically `### TASK-NNN:` ...`` as the first instruction line per §`target_task_id` auto-injection rule. Single-heading files render unchanged. Omitting the field for a >1-heading file is a render-time error.

**TASK-003 reuse callout.** This is now the **single Claude-cross-review template**, used for both (a) Codex-impl→Claude-review (the original purpose, retained verbatim) AND (b) Claude-impl→Claude-review under `claude_only=true` (new TASK-003 routing — see SKILL.md §Phase D.1's route-switch). The template body is reused as-is on both branches; the orchestrator picks the dispatch via `claude_only`. The verdict vocabulary `{ship, ship-with-fixes, needs-rework}` is preserved on both branches; Codex-side `{clean, minor-findings, needs-rework}` is NOT synthesized when this template is used as the `claude_only=true` cross-review path. No new agent file, no new template — `code-reviewer` (Sonnet) is the sole reviewer for both directions on the Claude path.

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

**`target_task_id` (TASK-007).** First-class dispatch field. When the resolved child plan file carries >1 `### TASK-NNN:` H3 heading, the orchestrator's render path prepends an "Adjudicate specifically `### TASK-NNN:`" first-instruction line per §`target_task_id` auto-injection rule. Single-heading files render unchanged. Omitting the field for a >1-heading file is a render-time error.

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
> Wrapper checks (verbatim from `envelope.wrapper_checks`):
>
> ```json
> <wrapper_checks_json>
> ```
>
> The `symbol_warnings` list flags cases where Codex cited a specific function, helper, or flag (e.g., `_some_helper(`, `--some-flag`) that does NOT exist in the cited file — a hallucination signal. An empty list means no hallucinated-symbol warnings are surfaced (either the wrapper ran the check and found none, or this envelope came from a failure/timeout/parse-error path where the check did not run and the orchestrator substituted the empty-list default). A non-empty list is what carries information; an empty list tells you nothing either way. Use a `not-found` warning as a **tiebreaker**, not a decision rule: it is a strong dismissal signal for that individual finding but still requires the dismissal-evidence gate below.
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
> **Dismissal-evidence gate (required before labeling any finding `dismissed`).** The decision rubric above answers *what verdict fits the split*; the dismissal-evidence gate answers *do you know the finding is wrong*. Before labeling a Codex finding `dismissed`, you MUST cite one of:
>
> 1. **A concrete verification move:** trace the cited control-flow path by hand, run the task's declared test command (`pytest -k <name>`, etc.), read the cited symbol in the repo, or reproduce the failure Codex describes. If the verification refutes the finding, `dismissed` is appropriate.
> 2. **An explicit spec contradiction:** the plan's acceptance criteria or Implementation Playbook mandates the behavior Codex objects to. In this case, label the disposition `spec-deference` (not `dismissed`) and include a one-line note on why the critique has merit despite the spec conflict. `spec-deference` surfaces the finding for a future plan-review pass rather than silently burying it.
>
> `spec-deference` applies only when the implementer followed the plan's literal wording AND Codex is disputing what the plan mandated. If the implementer deviated from the plan's literal wording, even for a verified-correct surrounding-code pattern match, internal-helper substitution, or semantically equivalent alternative, use `dismissed` with concrete verification evidence (or surface it as `minor-findings`) — NOT `spec-deference`.
>
> Worked example: Plan names mechanism A; implementer uses mechanism B with identical output because surrounding code already uses B. Codex flags the deviation. With pattern evidence, disposition is `dismissed`, not `spec-deference`. Contrast: plan mandates behavior X, implementer implemented X, and Codex objects to X; disposition is `spec-deference`.
>
> "Plan says so" without a `spec-deference` label is a protocol violation — it pretends the spec is unimpeachable. If you cannot verify the finding AND there is no explicit spec conflict, downgrade to a `minor-findings`-style note and let the commit proceed with the finding recorded rather than dismissed.
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

Role-swap retry dispatch, migrated to the v3 wrapper as of TASK-004. The orchestrator emits the dispatch as a `Bash` tool-use block invoking `plan_claude_dispatch.py run --input <payload.json>` with `payload.agent="plan-implementer"`, `payload.variant="role-swap"` (template selector), `overrides.model="opus"` — replacing the prior `Agent(subagent_type: "plan-implementer", ...)` call. The wrapper returns a v3 envelope on stdout; orchestrator asserts `.status=="ok"` and reads `.result` per the Phase B classify shim. The `retries_used.role_swap` budget check happens orchestrator-side BEFORE this dispatch — it is not enforced inside the wrapper.

The dispatch payload mirrors the Phase B skeleton above; substitute `payload.variant="role-swap"`. Reviewer findings are NOT forwarded (Open risks 1). Required report sections surface as structured arrays inside `.result.report`; `.scope.scope_violation_detected` and `.scope.scope_misreport_detected` are read directly from the envelope. The agent-behavior body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical to the pre-migration wording.

<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->

Per design §8.3 line 692: Claude re-implements, Codex re-reviews. One attempt.

**Retry implement** — re-use the Phase B template above verbatim with the same TASK-NNN block. Reviewer findings are NOT forwarded in v1 (see Open risks 1); Claude re-implements from the plan spec. After retry success, re-run Phase D-Codex (wrapper review) on the re-implementation. `needs-rework` on the re-review is terminal for this task — no further retries.

## Phase B-rework — Bounded remediation retry (D.2a.5)

Bounded-remediation dispatch, migrated to the v3 wrapper as of TASK-004. The orchestrator emits the dispatch as a `Bash` tool-use block invoking `plan_claude_dispatch.py run --input <payload.json>` with `payload.agent="plan-implementer"`, `payload.variant="rework"` (template selector), `overrides.model="opus"` — replacing the prior `Agent(subagent_type: "plan-implementer", model: "opus")` call. Strictly one attempt. The wrapper returns a v3 envelope on stdout; the orchestrator asserts `.status=="ok"` and reads `.result.outcome` + `.result.report.{plan_adaptations,concerns_for_reviewer,on_failure_revert}` per the Phase B classify shim. The agent-behavior body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical to the pre-migration wording.

The dispatch payload mirrors the Phase B skeleton above; substitute `payload.variant="rework"` and add `payload.dispatch_context: {findings_for_retry, d5_summary}` (forwarded verbatim from `review-route`'s `dispatch_context`). The required report sections (`Plan adaptations`, `Concerns for reviewer`, `On-failure revert`) surface inside `.result.report` as structured arrays. `.scope.scope_violation_detected` is read directly from the envelope. `malformed` outcomes — emitted when transport succeeded but `.result` failed schema validation — route the same as `failed` for D.2a.5 stage `implement`.

<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->

**`target_task_id` (TASK-007).** First-class dispatch field. When the resolved child plan file carries >1 `### TASK-NNN:` H3 heading, the orchestrator's render path prepends ``Apply the narrow remediation specifically to `### TASK-NNN:` ...`` as the first instruction line per §`target_task_id` auto-injection rule. Single-heading files render unchanged. Omitting the field for a >1-heading file is a render-time error.

Dispatched only when Codex reviewing Claude-implemented work returns `needs-rework` AND the Phase D.5 third-opinion code-reviewer independently agreed (verdict `needs-rework`). Strictly one attempt.

Unlike Phase D.2b, reviewer findings ARE forwarded here — the risk of the implementer blindly doing whatever Codex said is mitigated because D.5 already confirmed the findings are load-bearing. Keep the forwarded prompt structured; do NOT paraphrase into a free-form "fix what Codex flagged".

The `<findings_for_retry>` and `<d5_summary>` placeholders below are received from `review-route`'s `dispatch_context` payload (TASK-001) — the orchestrator no longer composes the JSON inline; it forwards the values verbatim. These names match the `dispatch_context` shape `review-route` emits.

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
> Codex findings (verbatim from `parsed.findings` of the wrapper envelope, forwarded as `dispatch_context.findings_for_retry`):
>
> ```json
> <findings_for_retry>
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
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `{{python_path}} ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec. Do not commit. Do not use `git stash`.
>
> **Parallel-mode caveat:** other implementers may be running concurrently on disjoint files. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

After retry success, re-run Phase D-Codex (wrapper review) on the re-implementation. `clean | minor-findings` → D.3 commit with `--remediation-tag`. `needs-rework` on the re-review triggers the D.2a.5 awaiting-user pause (see SKILL.md §D.2a.5 step 6); the orchestrator does NOT call `fail-task`.

## Phase B-narrow-remediation — Narrow-remediation retry (D.2a.6)

**`target_task_id` (TASK-007).** First-class dispatch field. When the resolved child plan file carries >1 `### TASK-NNN:` H3 heading, the orchestrator's render path prepends ``Apply the narrow-remediation patch specifically to `### TASK-NNN:` ...`` as the first instruction line per §`target_task_id` auto-injection rule. Single-heading files render unchanged. Omitting the field for a >1-heading file is a render-time error.

Dispatched only when Codex reviewing Claude-implemented work returns `needs-rework` AND the Phase D.5 third-opinion code-reviewer returned `partial-agreement` (findings split cleanly into load-bearing + dismissed buckets). Strictly one attempt. Use `Agent(subagent_type: "plan-remediator", model: "opus")` — a dedicated subagent role (not `plan-implementer`) so the touch-only-these-lines scope rule is structurally enforced by the agent's system prompt, and the retry is visible in the run log as a distinct dispatch.

Unlike Phase B-rework, the forwarded findings are **filtered** to the load-bearing subset only. The dismissed subset is supplied separately as context-only, explicitly labeled "DO NOT fix — context only"; the remediator acknowledges them in a `**Dismissed findings noted:**` report section but MUST NOT act on them. The `(file, line)` union of the load-bearing findings is the remediator's permitted edit region; unjustified edits outside that region return outcome `scope-violation`.

The `<findings_for_retry>`, `<dismissed_for_context>`, and `<d5_summary>` placeholders below are received from `review-route`'s `dispatch_context` payload (TASK-001) — the orchestrator no longer composes the JSON inline; it forwards the values verbatim. These names match the `dispatch_context` shape `review-route` emits.

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
> Load-bearing findings (D.5 ruled these ship-blockers — fix each one; forwarded as `dispatch_context.findings_for_retry`):
>
> ```json
> <findings_for_retry>
> ```
>
> Dismissed findings — **DO NOT fix — context only**. D.5 ruled these non-load-bearing; acting on them silently re-inflates the retry's scope. Echo each in the mandatory `**Dismissed findings noted:**` report section without acting on it (forwarded as `dispatch_context.dismissed_for_context`):
>
> ```json
> <dismissed_for_context>
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
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `{{python_path}} ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec (including the mandatory `**Dismissed findings noted:**` and `**Scope violations:**` sections). Do not commit. Do not use `git stash`.
>
> **Parallel-tree caveat:** other implementers and reviewers may be running concurrently on disjoint files; unstaged changes to disjoint files may be in the working tree. Focus strictly on the scope files listed in the task's `Files:` field. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

After retry success, re-run Phase D-Codex (wrapper review) on the re-implementation. `clean | minor-findings` → D.3 commit with `--narrow-remediation-tag --dismissed-finding-ids <comma-separated indices from D.5's dismissed bucket>`. `needs-rework` on the re-review triggers the D.2a.6 awaiting-user pause (see SKILL.md §D.2a.6 step 7); the orchestrator does NOT call `fail-task`. A retry outcome of `scope-violation` — or any other non-success outcome — triggers the same awaiting-user pause with `stage:"post_narrow_remediation_implement"`.

## Phase D.4-rescue — plan-remediator dispatch (single-shot rescue)

**`target_task_id` (TASK-007).** First-class dispatch field. When the resolved child plan file carries >1 `### TASK-NNN:` H3 heading, the orchestrator's render path prepends ``Apply the D.4 rescue specifically to `### TASK-NNN:` ...`` as the first instruction line per §`target_task_id` auto-injection rule. Single-heading files render unchanged. Omitting the field for a >1-heading file is a render-time error.

Dispatched only as the **single-shot terminal rescue** before a Phase D.4 halt (TASK-005). Strictly one attempt — the rescue branch never recurses; any non-success outcome falls into the awaiting-user pause per SKILL.md §D.4. Use `Agent(subagent_type: "plan-remediator", model: "opus")` — the same role used by D.2a.6, but invoked through a distinct input-key contract.

**Distinct from D.2a.6.** D.4 rescue does NOT consume D.5 adjudication: there is no `load_bearing` / `dismissed` split, no `d5_summary`. Every reviewer finding is treated as load-bearing for the rescue attempt. The dispatch uses `rescue_findings[]` (NOT `load_bearing_findings[]`) as the input key, and `dismissed_findings: []` is passed as a literal empty list — the remediator's empty-marker contract for the mandatory `**Dismissed findings noted:**` report section requires the literal output `(none — D.4 rescue does not carry dismissed findings)`. The `(file, line)` union of `rescue_findings[]` is the remediator's permitted edit region; the touch-only-these-lines scope rule applies unchanged.

> Apply a single-shot D.4 rescue to this task's existing implementation at `<absolute plan path>`. The prior attempt is **still in the working tree** — it was NOT reverted. The reviewer (`<reviewer_source>` — `codex` or `claude` per the original Phase D dispatch) returned `needs-rework` and the orchestrator is invoking the single-shot rescue path before halting. There is no D.5 third-opinion adjudication on this branch — every reviewer finding below is treated as load-bearing for the rescue attempt. Patch the current working-tree edits with a minimal, surgical fix that addresses each rescue finding. Do NOT rebuild from the base commit; do NOT re-do work that is already correct.
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
> Reviewer source: `<reviewer_source>` (`codex` or `claude` — the role of the reviewer whose verdict triggered the rescue).
>
> Rescue findings (verbatim from the reviewer's findings array — every entry is load-bearing for this rescue):
>
> ```json
> <rescue_findings_json>
> ```
>
> Dismissed findings — D.4 rescue does NOT carry a dismissed bucket. The literal value below is the empty list:
>
> ```json
> []
> ```
>
> Echo the empty-marker literal `(none — D.4 rescue does not carry dismissed findings)` in the mandatory `**Dismissed findings noted:**` report section — do NOT enumerate per-index acks (there are no indices to ack on this branch).
>
> Analyst annotations (verbatim, may be empty):
>
> ```json
> <analyst_annotations_json>
> ```
>
> **Scope rule (the touch-only-these-lines contract):** the union of `(file, line)` coordinates across `rescue_findings[]` defines your permitted edit region. Unjustified edits outside that region flip the outcome to `scope-violation`. **Fix narrowly, do not scope-inflate.** Address each rescue finding directly. Do NOT refactor unrelated code, do NOT add docstrings to untouched regions, do NOT tidy formatting outside the edited scope. If a finding cannot be reconciled with the plan's acceptance criteria, report `plan-incorrect` — do not invent a compromise.
>
> You may read the plan file for reference but do not modify it. Run the test command if specified — use `{{python_path}} ...` (this repo requires the virtualenv). Return your report in the structured format from your agent spec (including the mandatory `**Dismissed findings noted:**` and `**Scope violations:**` sections). Do not commit. Do not use `git stash`.
>
> **Parallel-tree caveat:** other implementers and reviewers may be running concurrently on disjoint files; unstaged changes to disjoint files may be in the working tree. Focus strictly on the scope files listed in the task's `Files:` field. Trust the diff when classifying test failures; do NOT use `git stash` (it would collide).
>
> **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

After rescue success, re-run the original D.1 reviewer (Codex wrapper for `reviewer_source=codex`, `code-reviewer` Agent for `reviewer_source=claude`) on the rescued working tree. The re-review is binding — no further retry. `clean | minor-findings` (or `ship | ship-with-fixes`) → D.3 commit with bare `--d4-rescue-tag` (no `--remediation-tag`, no `--narrow-remediation-tag`, no `--disagreement-tag`, no `--dismissed-finding-ids` — argparse rejects any companion flag). `needs-rework` on the re-review — or any non-success rescue outcome (`partial`, `failed`, `plan-incorrect`, `blocked`, `scope-violation`) — triggers the D.4 awaiting-user pause (see SKILL.md §D.4 step 6) with `stage:"post_d4_rescue_failed"`; the orchestrator does NOT call `fail-task`.
