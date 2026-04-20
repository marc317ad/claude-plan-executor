# /implement-plan context bloat — critical response to Gemini's analysis

**Date:** 2026-04-20
**Scope:** `plugins/plan-executor/` (SKILL.md, dispatch-templates.md, agents/*, scripts/plan_ops.py, scripts/plan_codex_dispatch.py)
**Prior art reviewed:** `docs/analysis/Gemini_Implement_Plan_agent_bloat_20260420.md`, `docs/analysis/Gemini_Implement_Plan_SKILL_Analysis_20260418.md`, `docs/post_mortem/2026-04-18_TASK-004E_halt_and_context_pressure.md`, `docs/plans/PLAN_PHASE_D_STATE_MACHINE_2026-04-18.md`, `docs/plans/PLAN_NESTED_DISPATCH_2026-04-18_v3.md`

**Addendum 2026-04-20:** `PLAN_PHASE_D_STATE_MACHINE_2026-04-18.md` (draft) covers my Tier-1 #4 (`route-review`) and Tier-2 #6 (SKILL.md condensation) directly, and exceeds my Tier-1 #2 by persisting full orchestrator state into `.schedule.json` instead of merely eliding log fields. It also closes a **prompt-injection surface this analysis missed** (wrapper-side envelope sanitization + optional unprivileged content-sanitizer subagent). See §7 below.

---

## TL;DR

Gemini is directionally right that the orchestrator LLM is bearing deterministic state-machine load it shouldn't, and that the system is context-heavy. But the specific playbook it offered is a mix of good ideas, confidently-wrong ones, and surface-level pattern matches that misread the architecture. Roughly **40% of its recommendations are load-bearing, 35% are either wrong or net-regressions, and 25% restate ideas the post-mortem already identified with better fidelity.**

The biggest thing Gemini missed: **the orchestrator is Claude Code itself**. You cannot "replace the LLM orchestrator with Python" — the skill file IS the orchestrator's system prompt for a Claude Code conversation turn. Every "just rewrite it in a Python loop" proposal is architecturally incoherent at the harness level. The realistic lever is *narrowing what the orchestrator LLM decides*, not *removing it*.

The single highest-ROI fix is **`plan_claude_dispatch.py`** (already sketched in the 2026-04-18 post-mortem, §Resolutions D). It symmetrizes Claude and Codex dispatches behind a subprocess wrapper, collapsing the Agent-tool markdown-report dump into a bounded JSON envelope + on-disk report pointer. That one change wipes out the dominant source of context accumulation.

---

## 1. Gemini's claims — verdict by claim

### Where Gemini is right (keep these)

| # | Claim | Verdict | Notes |
|---|---|---|---|
| G1 | Rules duplicated across `plan-implementer.md`, `plan-remediator.md`, `plan-analyst.md` (~12 bullets of "no Agent", "no git mutation", "venv/bin/python", "no stash") | **Correct** | Real DRY violation. Fix with a `_shared_worker_rules.md` include pattern, referenced once per agent. |
| G2 | `needs-rework` escalation ladder (D.2 / D.2a / D.2a.5 / D.2a.6 / D.2b) is too much state for a prompt to carry reliably | **Correct in spirit** | The routing table is real and complex. The right fix is `plan_ops.py route-review` returning `{action, flags}` — not "delete the routing". |
| G3 | Template redundancy — Phase B, B-rework, B-narrow-remediation share ~80% boilerplate | **Correct** | Templates are already in one file; a shared preamble block would cut ~400 tokens off the dispatch bundle. Low-effort. |
| G4 | Manual JSON validation in `plan_ops.py` (`_validate_schedule`, `_validate_reviewer_finding_item`, etc.) is ~1,000 lines of hand-rolled type-checking | **Correct** | Codex outputs already have JSON Schema files (`codex_*_schema.json`). Author an analyst schedule schema, replace manual validators with `jsonschema.validate()` + structured error extraction. Keep the current path-shaped error vocabulary for caller continuity. |
| G5 | Markdown-regex mutation (`_split_task_blocks`, `mutate_task_status`) is fragile | **Correct but misdiagnosed fix** | Regex on `**Status:**` bullets IS brittle. Gemini's fix (make JSON the SoT, render markdown) is wrong (see §2 below). The right fix is harden the parser + add a `lint-plans` gate that catches drift at CI time. |
| G6 | Context window fills with intermediate bash JSON stdout across a 10-task run | **Correct** | This is exactly what the post-mortem flagged. The fix is the Claude-dispatch wrapper (§3) — not "stop using the LLM". |

### Where Gemini is wrong or off-target

| # | Claim | Verdict | Why |
|---|---|---|---|
| G7 | "Just centralize global constraints into `GLOBAL_SYSTEM_RULES.md` injected into every subagent" | **Incoherent** | Claude Code subagents get their system prompt from the agent spec frontmatter file. There is no orchestrator-level "inject into every agent" primitive. The realistic fix is a shared-rules include pattern at author time (G1). |
| G8 | "Remove `You do NOT have the Agent tool` from dispatch prompts — enforce via API binding" | **Partially wrong** | Agent frontmatter's `tools:` field DOES permission-restrict. The prompt-level reminder exists because the model can still *try* to call `Agent` and crash the session in the attempt; the reminder is a defensive belt-and-suspenders. The line is cheap (≈15 tokens) — keep it. |
| G9 | "LLMs cannot count words; `≤400 words` is a fallacy" | **Overclaims** | True that LLMs don't count tokens, but soft targets still bias the output length. The real issue is that 400 is opaque; replace with *structural* caps ("≤3 bullets in Diff summary", "≤30-line test-output tail"). Current specs mostly already do this — the word cap is only applied to narrative sections. Nit, not bloat. |
| G10 | "`Appendix C.4`, `CLAUDE.md`, `DUAL_AGENT_PLAN_EXECUTOR.md §5` references will be hallucinated" | **Mostly wrong, partially right** | The analyst AND implementer agents both reproduce C.4 semantics *inline and verbatim* — the reference is just a provenance pointer, not a dynamic dep. BUT the `DUAL_AGENT_PLAN_EXECUTOR.md §5` reference in `plan-analyst.md` IS a genuine phantom dep in the agent's context. Fix: strip the external reference or inline the 2-sentence snippet it points to. |
| G11 | "Inline Python heredoc in plan-analyst is a massive token sink; move to a bash tool or trust the model to batch natively" | **Wrong** | Inline Python is ~25 lines. The alternative ("trust the model") was tried in earlier iterations and produced inconsistent batch ordering. The justification on `plan-analyst.md:124` is explicit: deterministic algorithms run in Python, and the heredoc keeps the agent portable (no dependency on `plan_ops.py`). Replacing this with "trust the model" would be a regression. |
| G12 | "Shift orchestrator loop entirely into Python (main.py / LangGraph)" | **Architecturally incoherent** | The orchestrator IS a Claude Code skill. You cannot `main.py` it — the skill is invoked by a user typing `/implement-plan`, with the LLM acting as the dispatch loop. Proposals that assume a raw Python orchestrator are treating this as a generic agent framework; it's not. |
| G13 | "Migrate workers to local inference (Ollama / vLLM / qwen2.5-coder / llama3)" | **Off-mission** | This is a Claude Code plugin. Switching to self-hosted local models would rebuild the entire runtime. No evidence the user wants this; memory says *"MCP/JSON-schema tools are the long-term target"* — that is the opposite direction (tighter Anthropic integration, not forking to OSS). Discard. |
| G14 | "Invert the markdown database — make JSON authoritative and render markdown" | **Wrong for this system** | The plan file is a human-authored artifact. It is reviewed as a PR, hand-edited, diffed visually. Making it a generated artifact removes the plan-author's agency AND creates a new sync surface (JSON drift vs. markdown drift). The hard-won invariant is "markdown is canonical, JSON is derived where needed." Harden the parser; do not invert. |
| G15 | "Deprecate SKILL.md and replace with Python main.py" | **Same as G12** | Same category error. SKILL.md is the system prompt. |
| G16 | "LLM should only be invoked for cognitive tasks (Analyze, Implement, Review, Adjudicate)" | **Restates the post-mortem** | This is literally the post-mortem's Meta-observation ("don't expand runtime LLM judgment; expand the deterministic rulebook"). Not a new finding. |
| G17 | "`parse-schedule` is 800 lines of manual type-checking — use `jsonschema`" | **Correct on substance, wrong on scale** | ~250 lines of the Python validators are manual shape checks (G4). The other ~550 cover DAG/ref/semantics validation that `jsonschema` fundamentally can't express (transitive-dep cycles, `outcome↔gaps` consistency, `file_locks = sorted(union(tasks.files))` invariants). Those have to stay imperative. |

### Where Gemini rediscovered the post-mortem (without attribution)

The 2026-04-18 post-mortem on TASK-004E already identified:

- **Claude subagent dispatches bloat context** (post-mortem §4) — Gemini's §1 "Context Window Nightmare" restates this.
- **Symmetric Claude/Codex dispatch via a Python wrapper** (post-mortem §Resolutions D) — Gemini's "Elevate Python to the Controller" restates this without the concrete `plan_claude_dispatch.py` design sketch.
- **Report-to-disk pattern** (post-mortem §Resolutions E) — Gemini doesn't mention this even though it's the minimum viable mitigation when the full wrapper isn't yet ready.
- **Gap severity grading** (post-mortem §Resolutions B) — not in Gemini's analysis at all, and it's a material orchestrator-autonomy lever independent of context bloat.

---

## 2. What Gemini missed or under-weighted

### 2.1 The orchestrator LLM is the Claude Code conversation turn

SKILL.md (512 lines) IS loaded as the system prompt every turn the orchestrator operates. Any bloat there is multiplied by the number of orchestrator turns in a run — typically 1 per task + batch transitions. Gemini treats SKILL.md as "a document the LLM reads" — it's not, it's the full prompt every dispatch cycle.

**Implication:** shaving 200 lines off SKILL.md compounds. Moving reference material (CLI table, run-log schema, end-of-run checklist, Phase D.2a details) out of SKILL.md and into either `plan_ops.py --help` emissions OR a tight `quick-reference.md` that the orchestrator reads only once at Phase 0 (and can then drop from context) is a direct win.

### 2.2 Asymmetric dispatch is the dominant bloat source, not prompt size

Per the post-mortem, the orchestrator accumulates one full markdown report **per Claude subagent dispatch** (analyst, implementer, reviewer-Claude, D.5 reviewer, remediator, retry implementer). A 5-task run easily buffers 15+ verbose reports into orchestrator context. Each report is ~600–1500 tokens of prose the orchestrator already parsed structured fields out of and no longer needs.

By contrast, every Codex dispatch returns a single JSON envelope (~300–500 tokens) via the wrapper. The orchestrator stays thin.

The fix — a `plan_claude_dispatch.py` wrapper that invokes `claude -p --output-format json --model opus` headless — is the **single largest context-saving move available**. Gemini didn't articulate this as a concrete lever; it buried the idea inside "elevate Python to the controller" without proposing the specific shim.

### 2.3 Diagnostic surfaces leak into orchestrator context without load-bearing use

`parse-implementer-report` currently returns `concerns`, `plan_adaptations`, `warnings`, `diagnostics`, `reversion_guidance` to the orchestrator. Of these:

- `concerns` / `plan_adaptations` / `warnings` — feed the reviewer and end-of-run summary. They do NOT drive routing. Could be persisted in the run log and referenced by the reviewer dispatch (which reads the log) instead of carried in orchestrator context.
- `diagnostics` — the existence of a `missing-plan-adaptations` flag is appended to the reviewer prompt but is otherwise non-halting. Again, push to run log.
- `reversion_guidance` — only load-bearing when `outcome ≠ success`. When `outcome=success`, strip it from the orchestrator-visible payload.

Filtering the `parse-implementer-report` output to routing-relevant fields-only (outcome, files_changed, test_outcome, scope_check inputs) saves ~40% of the per-task payload.

### 2.4 JSON Schema exists for Codex but not for the analyst schedule

`codex_implement_schema.json`, `codex_review_schema.json`, `codex_plan_review_schema.json` already constrain Codex outputs. The analyst schedule — which drives the entire DAG — has no schema; it's validated by ~500 lines of Python. Authoring `analyst_schedule_schema.json` (for the shape checks) and keeping `_validate_schedule_refs` / `_validate_schedule_dag` as semantic validators is a clean split.

### 2.5 Rule duplication between `plan-implementer.md` and `plan-remediator.md` is structural, not accidental

These two agents share:
- The entire Step 1 (Read context) narrative
- Step 3 (Run the test command) verbatim except for the word "implementer"/"remediator"
- All 13 rules in the Rules section
- The file-annotation reference
- The status-vocabulary definitions

Total duplicated surface: ~90 lines per agent. Extracting to `_shared_worker_protocol.md` and referencing from both agents via the `description:` hint or via the dispatch template (orchestrator reads once, subagent gets it inline) saves ~180 lines of agent context on every dispatch that reads the agent spec.

### 2.6 The orchestrator DOES need to make some judgment calls Gemini would outsource

- **Scope reconcile** after a Codex batch: the wrapper emits `out_of_scope_observed` flags. Deciding whether to halt is currently orchestrator-side, and that's appropriate because the judgment involves reading the wrapper's `extra.reconciliation_failed` payload AND cross-referencing the batch's `file_locks`. Could move to `plan_ops.py reconcile-batch` (which already exists) + a boolean halt flag. Gemini didn't notice this was already half-done.
- **Verdict verbatim-preservation across reviewer types** (`clean/minor-findings/needs-rework` vs. `ship/ship-with-fixes/needs-rework`). The current SKILL.md explicitly says vocabulary is not normalized, and the D.5 + D.2a table in SKILL.md encodes the routing. This IS a good `plan_ops.py route-review` candidate — returns `{action, flags}`; the orchestrator stops carrying the verdict-to-route mapping in its head.

### 2.7 Context cost of the dispatch templates file

`dispatch-templates.md` is 333 lines, referenced by `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md` — meaning the orchestrator reads it on-demand, not automatically. Good pattern. But the orchestrator typically reads it multiple times per run (once per phase kind). Post-Phase-0, it could be cached in working memory via a summary pass. Marginal win (~500 tokens amortized).

---

## 3. Prioritized remediation (ordered by ROI)

### Tier 1 — high impact, low-to-moderate effort

1. **`plan_claude_dispatch.py` wrapper** (post-mortem §Resolutions D)
   - Invokes `claude -p "<prompt>" --output-format json --model opus` headless.
   - Probe: does `claude -p` honor `subagent_type:` system prompts the way `Agent(subagent_type=...)` does? If no, synthesize the full system prompt from the agent spec file and inject.
   - Returns `{outcome, files_changed, report_path, structured_fields}` — full markdown report lives on disk, not in orchestrator context.
   - **Expected context reduction: 40–60% per run.**

2. **Elide non-load-bearing fields from `parse-implementer-report` output (§2.3)**
   - Return only routing-relevant fields. Push `concerns`, `plan_adaptations`, `warnings`, `diagnostics` to the run log.
   - Reviewer dispatches fetch these from `_run_log.jsonl` via a scoped read, if needed.
   - **Expected context reduction: 15–20% per task.**

3. **Extract shared worker rules → `_shared_worker_protocol.md`** (§2.5)
   - Dedupes ~180 lines between `plan-implementer.md` and `plan-remediator.md`.
   - Referenced inline at dispatch time; `plan-analyst.md` shares a subset (git-mutation rules, venv invocation).
   - **Expected reduction: 180 lines × dispatch count per run.**

4. **`plan_ops.py route-review` subcommand** (§2.6, G2)
   - Takes reviewer envelope + current flags; returns `{action ∈ {commit, fail, remediate, narrow-remediate, role-swap-retry, third-opinion}, args: {...}}`.
   - Orchestrator drops the Phase D.2/D.2a tables from working memory and just dispatches the returned action.
   - **Expected reduction: ~150 lines from SKILL.md Phase D narrative.**

### Tier 2 — material, moderate effort

5. **JSON Schema for analyst schedule (§2.4, G4)**
   - Author `analyst_schedule_schema.json`; replace manual shape validators with `jsonschema.validate()`.
   - Keep `_validate_schedule_refs` / `_validate_schedule_dag` (semantic + DAG checks `jsonschema` can't express).
   - **Expected reduction: ~250 lines of `plan_ops.py`; improves error quality (structured `jsonpath` vs. hand-rolled paths).**

6. **Condense SKILL.md (§2.1)**
   - Move the 13-row CLI reference table out of SKILL.md into either `plan_ops.py --help` (always available) or a companion `cli-reference.md` read once at Phase 0.
   - Collapse Phase D.2a/D.2a.5/D.2a.6 narrative into pseudo-code (per Gemini G2, correctly applied).
   - **Expected reduction: SKILL.md from 512 → ~280 lines.**

7. **Report-to-disk fallback (§2.2, post-mortem §Resolutions E)**
   - If §3.1 (full Claude wrapper) is deferred, retrofit the Agent-tool path to instruct subagents to write the full report to `docs/plans/_run_reports/<run_id>-<task>-<phase>.md` and return only a pointer + structured envelope.
   - Lower-fidelity version of §3.1 usable today with zero new Python.
   - **Expected reduction: 30–40% per run (not as clean as §3.1, but ships in a day).**

### Tier 3 — quality-of-life, low priority

8. **Harden markdown parser (G5, correctly-fixed)**
   - Tighter regex + a `plan_ops.py lint-plans` gate run in CI.
   - Detects drift (Status bullet variants, missing Files field, etc.) at author time, not at execution time.
   - Do NOT invert the database (§G14).

9. **Strip `You do NOT have the Agent tool` — NO, keep it (G8)**
   - This is the "don't do this" Tier 3: do NOT apply Gemini's suggestion. The line is cheap defensive insurance.

10. **Prompt-engineering word cap → structural cap (G9)**
    - Replace "≤400 words" with "≤3 bullets per narrative section" in `plan-implementer.md` / `plan-remediator.md`.
    - Marginal, but aligns with how the model actually biases length.

---

## 4. Anti-patterns — Gemini suggestions NOT to apply

1. **"Invert the markdown database"** (G14). Would remove plan-author agency and double the drift surface. Harden the parser; add a lint gate.
2. **"Move analyst's inline Python to a bash tool / trust the model"** (G11). Inline Python is the right tool for deterministic batching. Portability (no `plan_ops.py` dep for the analyst) is explicitly a design goal.
3. **"Deprecate SKILL.md / rewrite the orchestrator in Python"** (G12, G15). Architecturally incoherent — the skill IS the orchestrator. Narrow what the orchestrator decides; don't replace it.
4. **"Migrate Phase B/D workers to Ollama/vLLM"** (G13). Off-mission. This is a Claude Code plugin.
5. **"Remove `You do NOT have the Agent tool`"** (G8). Cheap defensive line; keep.
6. **"Centralize globals via orchestrator-level system prompt"** (G7). That primitive doesn't exist in Claude Code's subagent model. Use shared-rules files + include pattern instead.

---

## 5. Post-mortem alignment

The 2026-04-18 post-mortem on TASK-004E anticipated ~70% of the substantive bloat critique Gemini has since restated, and proposed concrete Python-side remediations (A–F). The post-mortem's §Meta-observation — *"don't expand runtime LLM judgment; expand the deterministic rulebook"* — is the right frame. Gemini's analysis is largely an external confirmation of that thesis, with the noted divergences.

**Recommendation:** prioritize post-mortem Resolution D (`plan_claude_dispatch.py`) as the canonical fix for §3.1 above. It subsumes Gemini's "elevate Python to the controller" proposal with a concrete design and is already scoped. Resolutions A+B+C (gap severity + triage) are orthogonal but complementary — they reduce orchestrator-autonomy friction without touching the context-bloat lever.

---

## 6. Open questions for the user

- **Context budget hard target.** Is the goal "fit a 10-task plan in one conversation turn without compaction" or "halve the current token spend"? The Tier-1 stack delivers ~55–70% reduction; Tier-2 gets to ~75–80%. Choosing the target tells us where to stop investing.
- **`claude -p` + subagent type probe.** Before committing to §3.1 as the headline fix, someone needs to verify that headless `claude -p --model opus` honors the plan-implementer / plan-remediator / plan-analyst system prompts. Post-mortem flagged this as an open design question; it's still open.
- **MCP tool migration cadence.** Per your memory, MCP/JSON-schema tools are the long-term target. Is the Tier-2 work (schedule schema, `route-review` subcommand) worth doing inside the bash-CLI v1, or is the ROI higher doing it directly as MCP tools once the migration starts? Current read: bash-CLI v1 work IS the foundation MCP tools will wrap, so it's not wasted effort.

---

## 7. Addendum — intersection with `PLAN_PHASE_D_STATE_MACHINE_2026-04-18.md`

This draft plan (pending review, 8 tasks) materially supersedes parts of §3 above and closes a gap §1–§5 missed.

**Supersedes / subsumes:**

| This doc's item | Plan task | Verdict |
|---|---|---|
| Tier-1 #4 (`plan_ops.py route-review`) | **TASK-001 `review-route`** | Same design, more thorough. Use the plan's spec. |
| Tier-1 #2 (elide non-routing `parse-implementer-report` fields) | **TASK-002 (schedule-state persistence)** | Strictly better. Persist `done`/`failed`/`locked_files`/`committed`/`review_notes`/`retries_used` into `.schedule.json`; orchestrator stops tracking state at all. Drop my Tier-1 #2 in favor of TASK-002. |
| Tier-2 #6 (SKILL.md condensation) | **TASK-005** | Same target (≥30% reduction), mechanically gated on TASK-001/002/003 landing first. |
| — (not in this doc) | **TASK-003 (wrapper envelope sanitizer)** | **Gap I missed.** Three-layer defense in `plan_codex_dispatch.py` — strict envelope boundary + free-text length-cap/markdown-strip + known-shape flagging (`<system>` tags, `<tool_calls>`, "Ignore previous instructions", etc.). Aligns with memory `feedback_injection_defense_at_wrapper`. |
| — (not in this doc) | **TASK-004 (content-sanitizer subagent)** | LLM-as-sanitizer done correctly: `tools: (empty)`, cheap tier, verdict-only output. Orchestrator never sees the untrusted payload. |
| — (not in this doc) | **TASK-006 (SKILL ↔ argparse drift guard)** | Correct answer to "CLI reference is dead weight" — keep it, mechanize its freshness. |

**Composition with `PLAN_NESTED_DISPATCH_2026-04-18_v3.md`:** the two plans target orthogonal axes.

- Nested dispatch v3 = *transport* (per-dispatch bloat): bounded JSON envelopes instead of full markdown report dumps from Agent-tool calls.
- Phase D state machine = *routing + state + envelope hygiene* (per-turn bloat + injection defense): SKILL.md shrinks, orchestrator state moves to disk, envelope content sanitized.

They compose without conflict. After both ship, the orchestrator loop is: `dispatch via wrapper → bounded envelope → review-route → comply → mutate .schedule.json`. Per-turn and per-dispatch bloat both collapse.

**Revised sequencing (supersedes §3 ordering):**

1. **TASK-003 (wrapper sanitizer) immediately** — no deps, security-critical, closes gap §1–§5 missed. Do not wait.
2. **TASK-001 (`review-route`) + PLAN_NESTED_DISPATCH v3 in parallel** — independent, both strictly additive.
3. **TASK-002 (schedule state)** — depends on TASK-001.
4. **TASK-005 (SKILL.md rewrite)** — after TASK-001/002/003 land AND after nested-dispatch lands, so both changes land in one SKILL edit rather than two sequential rewrites.
5. **TASK-004 / TASK-006 / TASK-007 / TASK-008** — tails.
6. **Follow-up task neither plan currently owns:** migrate remaining `Agent(subagent_type: ...)` call sites in SKILL.md to bash-dispatch through `plan_claude_dispatch.py`. v3 ships the wrapper as escape-hatch-only; without this follow-up, the wrapper exists but isn't reducing context because SKILL.md still uses the Agent tool.

**Watch-outs:**

- TASK-005's ≥30% reduction only lands cleanly *after* its upstream deps. Doing it first deletes rules the orchestrator still needs.
- TASK-004's content-sanitizer dispatch is gated on `extra.sanitizer_flags` being non-empty — verify this gating survives implementation. Unconditional sanitizer dispatch would cost more than it saves.
- Plan contains no `(create)` path existence audit — run preflight before executing, since the analyst's C.4 check will halt the run if any create-annotated file already exists on disk.
