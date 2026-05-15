# PLAN AGENT DISPATCH MCP — Render in-process Agent dispatch prompts via plan_ops MCP

**Status:** pending
**Base branch:** main
**Created:** 2026-05-11

## Goal

Eliminate the orchestrator-side "read `dispatch-templates.md`, hand-substitute placeholders, dispatch Agent" pattern that fires once per in-process Agent dispatch. Replace it with a single MCP tool — `plan_ops__build_agent_dispatch_prompt` — that mirrors the existing `plan_ops__build_*_dispatch_input` builders but returns a rendered prompt string instead of writing a wrapper-input envelope to disk. Five subagent dispatch sites (covering seven phase templates) move off the markdown-read-at-runtime path: `code-reviewer` (Phase D-Claude + D.5), `plan-reviewer` (Phase 1.5-Claude), `plan-author` (Phase 1.5a, A/B/legacy), `plan-review-triage` (Phase 1-triage + Phase 1.5.5), and `plan-remediator` (Phase D.2a.6 + Phase D.4-rescue).

## Scoped Context

### The current asymmetry

For wrapper dispatches (Phase B implementer, Phase D-Codex/Gemini reviewers), the orchestrator already calls one structured MCP tool:

```
plan_ops__build_claude_dispatch_input  → writes claude_dispatch_input.json (schema-validated)
plan_ops__build_codex_dispatch_input   → writes codex_dispatch_input.json
plan_ops__build_gemini_dispatch_input  → writes gemini_dispatch_input.json
```

Each tool reads the appropriate `dispatch-templates.md` section, substitutes placeholders in Python, validates against a JSON schema, and writes a ready-to-feed envelope. Zero markdown reading in the orchestrator's conversation. Zero substitution risk.

For **in-process Agent dispatches** (the five subagents listed above), the orchestrator still does this dance once per dispatch:

1. Locate the right phase section in `dispatch-templates.md` (one 870-line file, 17 sections).
2. Slice it out — typically `awk '/^## Phase X/,/^## Phase Y/'` or a ranged `Read`.
3. Identify the placeholders the template documents (`<verbatim from TASK-NNN Description>`, `<comma-separated files from Codex wrapper's files_changed>`, `<wrapper_checks_json>`, etc.).
4. Substitute by string concatenation in conversation.
5. Pass the resulting string to the `Agent` tool as `prompt`.

The pattern works but compounds five soft costs across long runs: substitution-correctness risk (orchestrator does it by hand), no schema validation on placeholder inputs, context-token burn (25–100 lines per template per dispatch), compaction sensitivity (templates loaded mid-conversation get evicted on `/compact` and must be re-read), and architectural asymmetry (every protocol decision has to remember both dispatch classes).

### The dispatch sites being migrated

Per `grep "subagent_type" plugins/plan-executor/skills/implement-plan/{SKILL,dispatch-templates}.md`, the in-process sites are:

| Subagent | Phase(s) | Template id | dispatch-templates.md section |
|---|---|---|---|
| `code-reviewer` | Phase D-Claude (Codex-impl review under both routes) | `code-reviewer-d-claude` | `## Phase D-Claude — code-reviewer on Codex work` (line 611) |
| `code-reviewer` | Phase D.5 (third-opinion adjudicator) | `code-reviewer-d5` | `## Phase D.5 — code-reviewer third opinion` (line 637) |
| `plan-reviewer` | Phase 1.5-Claude (claude_only path) | `plan-reviewer` | `## Phase 1.5-Claude — plan-reviewer dispatch` (line 158) |
| `plan-author` | Phase 1.5a Variant A (task-targeted) | `plan-author-task-targeted` | `### Variant A — Task-targeted dispatch` (line 248) |
| `plan-author` | Phase 1.5a Variant B (schedule-level) | `plan-author-schedule-level` | `### Variant B — Schedule-level dispatch` (line 275) |
| `plan-author` | Legacy whole-plan dispatch | `plan-author-legacy-whole-plan` | `## Phase 1.5a — plan-author dispatch` body (line 219) |
| `plan-review-triage` | Phase 1-triage + Phase 1.5.5 | `plan-review-triage` | `## Phase 1-triage / Phase 1.5.5 — plan-review-triage dispatch` (line 305) |
| `plan-remediator` | Phase D.2a.6 narrow remediation | `plan-remediator-narrow` | `## Phase D.2a.6 — narrow-remediation Agent dispatch` (referenced in `## Phase B-narrow-remediation` cluster, line 791) |
| `plan-remediator` | Phase D.4-rescue single-shot | `plan-remediator-rescue` | `## Phase D.4-rescue — plan-remediator dispatch` (line 857) |

Nine `template_id` values, seven section bodies (Phase D-Claude is reused under both `claude_only=true` and `claude_only=false`; the plan-author variants share an outer section with sub-headings).

### Design choices pinned in the question round (user-approved)

- **One tool with a `template_id` discriminator** rather than seven per-phase tools. All seven templates dispatch to the same family (in-process Claude `Agent` calls), so a single discriminated tool fits cleaner than splitting by phase.
- **Return-in-result (not write-to-file).** Wrapper builders write to disk because subprocess input has size limits; Agent prompts have no such limit. The MCP response carries `{ok, agent, model, prompt}` directly.
- **One JSON schema with `oneOf`** discriminating on `template_id`. Mirrors how `claude_dispatch_input.json` already discriminates by `agent` (`plan-implementer | plan-analyst | code-reviewer | plan-author | ...`).
- **Migration order: land tool + first variant first, then port one cutover, then port the rest.** Avoids landing seven untested variants in one shot. The first variant is `code-reviewer-d-claude` because it's the variant the user most recently observed friction on.

### What does NOT change

- Same subagents get dispatched (same `subagent_type`, same `model`).
- Same prompt text reaches each subagent — render is byte-for-byte equal to today's hand-substituted output (golden tests pin this in TASK-001).
- Templates still live in `dispatch-templates.md` for human readability — the new tool reads from the same file, just in Python.
- The SKILL state machine (`review-route`, `plan-review-route`, the D.2a / D.5 ladder, the Phase 1.5 fix loop) is unchanged. Only the *render step* moves.

### Out of scope for this plan

- Migrating wrapper dispatch builders to a unified tool. Those already work; they're a separate refactor.
- Caching rendered prompts inside the MCP server. The MCP layer is stateless by design; templates re-render every call. Render cost is negligible (single file read + string substitution).
- Removing `dispatch-templates.md` or restructuring it into per-phase files. The orchestrator stops reading it at dispatch time, but humans (and the new MCP tool) still need the canonical location.
- Adding a `gates` predicate that asserts the SKILL never reads `dispatch-templates.md` from Bash. Useful follow-up; tracked separately.

## Verification

After all four tasks land:

- `venv/bin/pytest -q tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py tests/scripts/test_implement_plan_directory_smoke.py` returns 0.
- `plan_ops__build_agent_dispatch_prompt` accepts every `template_id` in `{code-reviewer-d-claude, code-reviewer-d5, plan-reviewer, plan-author-task-targeted, plan-author-schedule-level, plan-author-legacy-whole-plan, plan-review-triage, plan-remediator-narrow, plan-remediator-rescue}` and returns `{ok: true, agent, model, prompt}` with `prompt` byte-for-byte equal to a golden fixture per variant.
- Missing required placeholder fields surface as schema validation errors (not silent malformed prompts). Verified by per-variant negative tests.
- SKILL.md §Phase D.1 / §Phase D.5 / §Phase 1.5-Claude / §Phase 1.5a / §Phase 1-triage / §Phase 1.5.5 / §Phase D.2a.6 / §Phase D.4-rescue each instruct the orchestrator to call `plan_ops__build_agent_dispatch_prompt` and pass the returned `prompt` string verbatim to the `Agent` tool. No remaining instruction in those sections directs the orchestrator to read `dispatch-templates.md` at dispatch time.
- A new SKILL.md section `## Canonical Agent dispatch recipe` mirrors the existing `## Claude wrapper dispatch recipe (canonical)` (currently at SKILL.md:101) with the new `plan_ops__build_agent_dispatch_prompt` flow. The wrapper-recipe section is unchanged.
- `dispatch-templates.md` template bodies are unchanged in wording (the new tool reads them as-is). Each section gains a one-line header note: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="<id>", ...). The orchestrator does not read this section at runtime.`
- `grep -nE 'awk.*dispatch-templates|Read.*dispatch-templates' plugins/plan-executor/skills/implement-plan/SKILL.md` returns no results outside the §Canonical Agent dispatch recipe documentation block.
- A dry-run end-to-end smoke (single Codex-implemented task, single Claude-implemented task under `claude_only=true`) shows zero `awk` invocations against `dispatch-templates.md` in the orchestrator's tool-use log. Verified by adding an assertion to the directory smoke test.
- One `feat` commit per task; gates green; no `chore:` housekeeping commit needed beyond the standard end-of-run.

## Tasks

The plan is decomposed into four child files under `docs/plans/PLAN_AGENT_DISPATCH_MCP_2026-05-11/` with `00_INDEX.json` for the roster. Each child carries a single `### TASK-NNN:` H3 block with the standard metadata grammar. Below is the inline source the decomposer will split.

### TASK-001: MCP tool scaffolding + first variant (code-reviewer-d-claude)

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — add `cmd_build_agent_dispatch_prompt`, helpers, MCP wiring)
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (create)
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.output.json` (create)
  - `plugins/plan-executor/scripts/schemas/mcp/_index.json` (edit — register the new tool)
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` (create)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/golden_prompt.txt` (create)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/plan_file.md` (create)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/context.json` (create)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py tests/scripts/test_plan_ops.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py` — lines 12333-12500 (`cmd_build_claude_dispatch_input` and helpers, for the pattern to mirror)
  - `plugins/plan-executor/scripts/schemas/mcp/build_claude_dispatch_input.input.json` — full file
  - `plugins/plan-executor/scripts/schemas/mcp/build_claude_dispatch_input.output.json` — full file
  - `plugins/plan-executor/scripts/schemas/mcp/_index.json` — full file
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — lines 611-635 (Phase D-Claude template body, the substitution target)
  - `plugins/plan-executor/scripts/plan_ops.py` — grep for `_extract_task_description`, `_extract_task_acceptance_criteria`, or equivalent helpers; reuse if they exist, otherwise add minimal grammar-respecting parsers next to `_extract_task_files_from_plan`
- **Acceptance criteria:**
  - `plan_ops.py` gains `cmd_build_agent_dispatch_prompt(args)` returning a JSON envelope `{ok: true, agent: "<subagent>", model: "<model>", prompt: "<rendered>"}` on success and `{ok: false, errors: [...]}` on validation failure. The function is registered as both a CLI subcommand (`build-agent-dispatch-prompt`) and an MCP tool (`plan_ops__build_agent_dispatch_prompt`) per the existing wiring pattern in `_register_mcp_tools` (or whatever the equivalent registration site is).
  - The new MCP input schema `build_agent_dispatch_prompt.input.json` requires `template_id` and `context` at the top level, with `template_id` constrained to enum `["code-reviewer-d-claude"]` for now. (The enum widens in TASK-003.) `context` is an object with `oneOf` discriminating on `template_id`. For `code-reviewer-d-claude`, the context schema requires `plan_file` (abs path), `task_id` (`TASK-NNN` or `NNN` or `N`), `target_task_id` (optional, REQUIRED when the resolved child plan file carries >1 H3 task heading per the §`target_task_id` auto-injection rule), and `files_changed` (array of strings — the wrapper's `files_changed` list).
  - The new MCP output schema `build_agent_dispatch_prompt.output.json` documents both shapes: success `{ok: true, agent, model, prompt}` and failure `{ok: false, errors: [...], warnings: []}`. `additionalProperties: true` for forward compat.
  - `_index.json` lists the new tool under the same shape as existing entries. The MCP server picks it up without further wiring (verified by `venv/bin/python -m plugins.plan-executor.scripts.mcp.plan_ops_server --list-tools | grep build-agent-dispatch-prompt`).
  - The renderer reads `dispatch-templates.md` from `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md` (resolved via the same constant `cmd_build_claude_dispatch_input` uses). It extracts the section bounded by `## Phase D-Claude — code-reviewer on Codex work` (inclusive) and the next `## Phase ` heading (exclusive), strips the orchestrator-facing header notes (`> Rendered by ...`), and substitutes the four placeholders documented in the template (`<comma-separated files from Codex wrapper's files_changed>`, `<verbatim from TASK-NNN Description>`, `<verbatim from TASK-NNN Acceptance criteria>`, plus the conditional `target_task_id` first-instruction line per §613).
  - Description and Acceptance criteria are extracted from the resolved child plan file using grammar-respecting helpers — NOT regex over the raw file. If `_extract_task_description` / `_extract_task_acceptance_criteria` do not yet exist, add them next to `_extract_task_files_from_plan` in `plan_ops.py` with the same parsing primitives (the implementer must verify the chosen primitives by reading the existing helper before writing new ones).
  - **Golden test:** `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py::test_code_reviewer_d_claude_renders_to_golden` reads the fixture plan + context, calls the tool, and asserts the returned `prompt` is byte-for-byte equal to `tests/fixtures/agent_dispatch_prompt/code-reviewer-d-claude/golden_prompt.txt`. The fixture exercises (a) single-H3-heading plan file (no `target_task_id` first-instruction line), (b) >1-H3 plan file (the auto-injection line is prepended). Two test cases.
  - **Negative tests:** missing `files_changed` raises a schema validation error with a non-empty `errors[]` array and `ok: false`. Missing `target_task_id` against a multi-heading file raises `errors:[{code: "target-task-id-required", ...}]`.
  - **No SKILL or dispatch-templates.md modifications in this task.** The new tool sits unused. SKILL cutover happens in TASK-002.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py tests/scripts/test_plan_ops.py` returns 0.
- **Reversion guidance:** revert the new test file, the two new schema files, the `_index.json` registration line, and the new function plus helpers in `plan_ops.py`. No SKILL-level changes were made.

**Description:**
Add a new MCP tool `plan_ops__build_agent_dispatch_prompt` that accepts `{template_id, context}` and returns `{ok, agent, model, prompt}`. Implement only the first variant (`code-reviewer-d-claude`) in this task. The tool reads `dispatch-templates.md`, extracts the Phase D-Claude section, substitutes placeholders sourced from the supplied plan file (Description + Acceptance criteria) and the supplied context (`files_changed`, optional `target_task_id`), and returns the rendered prompt string. JSON schema validates the input shape; a per-variant `oneOf` branch validates the context payload. Golden test pins the rendered output byte-for-byte to today's hand-substituted form so the cutover in TASK-002 cannot drift the prompt.

**Implementation notes:**
- Mirror the existing `cmd_build_claude_dispatch_input` (line 12333) for the function shape, the `errors[]` / `warnings[]` envelope, and the schema-validation flow. Do NOT introduce a new envelope shape.
- The variant dispatch table can be a single `_AGENT_DISPATCH_TEMPLATES` dict keyed by `template_id` with values `{section_anchor, agent, model, context_schema_branch, render_fn}`. TASK-003 adds the other eight entries.
- `section_anchor` is the literal H2 heading text. The extractor finds the line `^## <anchor>` and reads through (but excluding) the next `^## Phase ` heading. Strip lines starting with `> Rendered by plan_ops__build_agent_dispatch_prompt` (the cutover banner TASK-002 adds — defensive against ordering, since TASK-001 lands before that banner exists).
- Reuse the `_extract_task_files_from_plan` parsing primitives (`scripts/_plan_paths.py`) for Description/AC extraction. The grammar is documented in `dispatch-templates.md`'s plan-grammar section; the existing helper is the canonical reader.
- Do NOT hard-code the `${CLAUDE_PLUGIN_ROOT}` value. Read it from the same constant `cmd_build_claude_dispatch_input` uses (likely `_PLUGIN_ROOT` or similar — verify by grep before writing).
- The golden fixture's plan file should be intentionally minimal: one task block with Description, Acceptance criteria, Files, Test command, Implementation notes. Do NOT reference real-project file paths in the fixture — use `fixtures/foo.py` style placeholders.
- Validate the input JSON against the new schema BEFORE any file I/O. A malformed `template_id` should fail fast without touching `dispatch-templates.md`.

---

### TASK-002: Cut over Phase D-Claude in SKILL + dispatch-templates

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — Phase D.1 sections, plus add `## Canonical Agent dispatch recipe` near line 101)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — add header note to Phase D-Claude section)
  - `tests/scripts/test_implement_plan_directory_smoke.py` (edit — add assertion that no `awk` against `dispatch-templates.md` runs in the dry-run trace)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — lines 95-115 (existing `## Claude wrapper dispatch recipe (canonical)` section, the structural twin)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — lines 510-560 (Phase D.1 routing table — both `claude_only=true` and Codex-implemented Claude-review branches)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — lines 611-635 (Phase D-Claude section — the only template touched by this task)
  - `tests/scripts/test_implement_plan_directory_smoke.py` — full file
- **Acceptance criteria:**
  - SKILL.md gains a new section `## Canonical Agent dispatch recipe` immediately after the existing `## Claude wrapper dispatch recipe (canonical)`. The new section documents the five-step recipe for in-process Agent dispatches in the same numbered-list shape: (1) call `plan_ops__build_agent_dispatch_prompt` with `template_id` + `context`, (2) assert `ok == true`, (3) extract `agent`, `model`, `prompt`, (4) dispatch `Agent(subagent_type=agent, model=model, prompt=prompt)`, (5) parse the result via the appropriate `plan_ops__parse_*` tool (no ad-hoc parsing).
  - The new section explicitly states: "Do NOT read `dispatch-templates.md` from Bash or `Read` at dispatch time. The MCP tool reads it on the orchestrator's behalf and returns the rendered string. The template file remains the canonical authoring location."
  - SKILL.md §Phase D.1 — both branches that today instruct the orchestrator to render the Phase D-Claude template manually — are rewritten to invoke the new recipe. Specifically: line 520 (`claude_only=true` → `code-reviewer` Agent) and the Codex-implemented Claude-cross-review branch (the `Phase D.1` table row for `Codex implemented, claude_only=false`).
  - SKILL.md line 49 (`Dispatch prompts must be self-contained. Read ${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md for the templates.`) is updated to note the asymmetry resolution: wrapper dispatches use the wrapper-input builders; in-process Agent dispatches use the new agent-prompt builder. The `Read dispatch-templates.md` instruction is preserved as a fallback for direct inspection but explicitly NOT the runtime path.
  - `dispatch-templates.md` Phase D-Claude section (line 611) gains a header note immediately under the `##` heading: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="code-reviewer-d-claude", ...). The orchestrator does not read this section at runtime — see SKILL.md §Canonical Agent dispatch recipe.`
  - The Phase D-Claude template body (lines 619-635) is otherwise byte-identical (verified by `git diff --shortstat` showing only the new header note line + the heading addition).
  - `tests/scripts/test_implement_plan_directory_smoke.py` gains a new assertion `test_phase_d_claude_uses_mcp_render_path` (or similar): the smoke test runs a dry-run `/implement-plan` against a fixture plan with one Codex-implemented task, captures the orchestrator's tool-use trace, and asserts (a) at least one `plan_ops__build_agent_dispatch_prompt` call with `template_id == "code-reviewer-d-claude"` is recorded, (b) zero Bash invocations matching `awk.*dispatch-templates` or `Read` calls against `dispatch-templates.md` are recorded after the Phase B implementer commit. If the smoke harness does not yet expose a tool-use trace, add the minimal hook needed (the existing harness already captures wrapper subprocess invocations — extend that to MCP tool calls).
  - `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0.
- **Reversion guidance:** revert the SKILL.md and dispatch-templates.md edits and the new smoke-test assertion. The new MCP tool from TASK-001 remains in place but is no longer invoked from the SKILL. The orchestrator falls back to the markdown-read pattern automatically (no code change needed) because TASK-001 left the Read path intact.

**Description:**
Cut over the Phase D-Claude dispatch site from the markdown-read pattern to the new MCP tool. Update both branches of SKILL.md §Phase D.1 (the `claude_only=true` route and the Codex-implemented Claude-cross-review route) to use `plan_ops__build_agent_dispatch_prompt`. Add a new SKILL.md section `## Canonical Agent dispatch recipe` mirroring the existing `## Claude wrapper dispatch recipe (canonical)` so the pattern is documented once and referenced from each phase. Add a header note to the Phase D-Claude section in `dispatch-templates.md` directing readers to the SKILL recipe. The template body is unchanged. A new smoke-test assertion confirms the orchestrator does not read `dispatch-templates.md` at dispatch time on the cut-over path.

**Implementation notes:**
- The `## Canonical Agent dispatch recipe` section should sit immediately after the `## Claude wrapper dispatch recipe (canonical)` block (currently at SKILL.md:101) so the two recipes appear back-to-back. Use the same numbered-list / fenced-code shape so the visual parallel is obvious.
- When updating SKILL.md line 49, do NOT remove the `${CLAUDE_PLUGIN_ROOT}` path entirely — operators may still want to grep the file. Reframe it: "Templates are authored in `${CLAUDE_PLUGIN_ROOT}/skills/implement-plan/dispatch-templates.md`. At dispatch time, use `plan_ops__build_*_dispatch_input` for wrapper dispatches and `plan_ops__build_agent_dispatch_prompt` for in-process Agent dispatches; both read the file in Python on the orchestrator's behalf."
- The smoke-test extension is the load-bearing acceptance gate for "no awk after this lands." If the existing harness lacks tool-use trace capture, add it as a narrow change scoped to the new test — do NOT broaden the harness for unrelated needs in this task.
- After the cutover, the only remaining `dispatch-templates.md` Read path on the Phase D-Claude branch should be inside `cmd_build_agent_dispatch_prompt` itself (Python-side). Verify with `grep -n "dispatch-templates" plugins/plan-executor/skills/implement-plan/SKILL.md` and confirm the only matches are inside the §Canonical recipe documentation block.

---

### TASK-003: Port the remaining six template variants

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — extend `_AGENT_DISPATCH_TEMPLATES` dict and per-variant render helpers)
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (edit — widen `template_id` enum and add `oneOf` branches for each new context shape)
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` (edit — add per-variant golden tests + negative tests)
  - `tests/fixtures/agent_dispatch_prompt/code-reviewer-d5/` (create — golden_prompt.txt, plan_file.md, context.json)
  - `tests/fixtures/agent_dispatch_prompt/plan-reviewer/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-author-task-targeted/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-author-schedule-level/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-author-legacy-whole-plan/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-review-triage/` (create — with `source: analyst` and `source: codex` sub-fixtures)
  - `tests/fixtures/agent_dispatch_prompt/plan-remediator-narrow/` (create)
  - `tests/fixtures/agent_dispatch_prompt/plan-remediator-rescue/` (create)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — line 158 (Phase 1.5-Claude), line 219 (Phase 1.5a outer + Variant A line 248 + Variant B line 275), line 305 (Phase 1-triage / 1.5.5), line 637 (Phase D.5), line 791 (Phase B-narrow-remediation cluster — confirm where the D.2a.6 template body actually lives), line 857 (Phase D.4-rescue)
  - `plugins/plan-executor/scripts/plan_ops.py` — the `_AGENT_DISPATCH_TEMPLATES` dict and `cmd_build_agent_dispatch_prompt` added in TASK-001
- **Acceptance criteria:**
  - The eight new `template_id` values are added to the input schema enum: `code-reviewer-d5`, `plan-reviewer`, `plan-author-task-targeted`, `plan-author-schedule-level`, `plan-author-legacy-whole-plan`, `plan-review-triage`, `plan-remediator-narrow`, `plan-remediator-rescue`. Each gets a corresponding `oneOf` branch in the `context` schema documenting its required fields.
  - For each variant, a golden test asserts byte-equality between the rendered prompt and the fixture `golden_prompt.txt`. Per-variant fixtures cover both the single-H3-heading and >1-H3-heading cases where the variant supports `target_task_id` auto-injection (`code-reviewer-d5`, `plan-author-task-targeted`, `plan-remediator-narrow`, `plan-remediator-rescue` per the same §`target_task_id` rule documented in dispatch-templates.md). Variants that do NOT take `target_task_id` (`plan-reviewer`, `plan-author-schedule-level`, `plan-author-legacy-whole-plan`, `plan-review-triage`) get one fixture each.
  - Negative tests per variant: missing required context fields surface as `{ok: false, errors: [{code: "<specific>", path: "<json pointer>", ...}]}`. Each variant has its own required-field set (e.g., `plan-review-triage` requires `source`, `findings`, `gaps`; `code-reviewer-d5` requires `wrapper_checks_json`, `reviewer_findings`; `plan-author-task-targeted` requires `child_plan_file`, `finding`).
  - `plan-review-triage` source discrimination: the `source` field accepts `analyst | codex`. The render swaps the embedded evidence label, the verification-move examples, and the analyst-only same-family caveat per dispatch-templates.md:314. The golden fixture covers both source values.
  - `plan-author` legacy-whole-plan variant exists explicitly even though most callers use task-targeted or schedule-level. The legacy CLI path still calls it (per `plan-author.md` agent doc); deprecation is a separate plan.
  - The Phase D.5 variant accepts a `wrapper_checks_json` placeholder that may default to `{"symbol_warnings": []}` per the dispatch-templates.md:607 wrapper-checks-asymmetry note. The schema documents this default.
  - **No SKILL or dispatch-templates.md edits in this task.** Cutover for the eight variants is TASK-004. The new variants sit unused after this task lands.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0 (golden + negative coverage for all nine variants).
- **Reversion guidance:** revert the schema enum widening, the per-variant render helpers, and the new fixture directories. The TASK-001 `code-reviewer-d-claude` variant remains intact and the TASK-002 cutover continues to function.

**Description:**
Extend the MCP tool added in TASK-001 with the remaining eight `template_id` values covering Phase D.5, Phase 1.5-Claude, Phase 1.5a (three sub-variants), Phase 1-triage / 1.5.5 (one variant with `source` discrimination), Phase D.2a.6 narrow remediation, and Phase D.4-rescue. Each variant gets a per-context schema branch (`oneOf` on `template_id`), a golden test asserting byte-equality to a fixture rendering, and negative tests for missing required fields. No SKILL changes here — TASK-004 cuts each phase over.

**Implementation notes:**
- The `_AGENT_DISPATCH_TEMPLATES` dict from TASK-001 grows from 1 entry to 9. Keep it sorted by `template_id` for readability.
- The `plan-author` variants share a section anchor at line 219 with sub-headings at 248 and 275. The extractor needs to slice by sub-heading (`### Variant A`, `### Variant B`) for the per-variant variants, and slice the body before `### Variant A` for the legacy-whole-plan variant. Do not duplicate the section text in the dict — store the anchor + slicing rule.
- For `plan-review-triage`, the source discrimination is a runtime substitution, not a different section. One template body, two sets of placeholder values.
- For `plan-remediator-narrow` (line 791 cluster) and `plan-remediator-rescue` (line 857), these are technically `plan-remediator` Agent calls with different input-key contracts — a single subagent role, two distinct dispatches. The template_id splits them; the rendered prompt differs.
- Validate fixture goldens by hand-running `cmd_build_agent_dispatch_prompt` against each fixture once and copying the output to `golden_prompt.txt`. Then re-run via pytest to confirm byte equality. Document this bootstrap step in a fixture-level README so future regressions are easy to fix.

---

### TASK-004: Cut over the remaining eight dispatch sites + final SKILL pass

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — Phase 1.5-Claude, Phase 1.5a, Phase 1-triage, Phase 1.5.5, Phase D.5, Phase D.2a.6, Phase D.4-rescue sections)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — add header notes to the seven section bodies)
  - `tests/scripts/test_implement_plan_directory_smoke.py` (edit — extend the no-awk-against-dispatch-templates assertion to cover all eight phases)
- **Dependencies:** [002, 003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — full file (the cutover touches eight separate phase sections; the implementer needs full-file context to avoid missing one)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — the seven section bodies enumerated in the table at the top of this plan
  - `tests/scripts/test_implement_plan_directory_smoke.py` — the assertion added in TASK-002 (the new assertion extends it)
- **Acceptance criteria:**
  - Each of the seven non-D-Claude phase sections in SKILL.md is rewritten to invoke the §Canonical Agent dispatch recipe with the appropriate `template_id`. Specifically: §Phase 1.5-Claude → `template_id="plan-reviewer"`; §Phase 1.5a → one of the three `plan-author-*` variants depending on the dispatch path; §Phase 1-triage → `template_id="plan-review-triage"` with `context.source="analyst"`; §Phase 1.5.5 → `template_id="plan-review-triage"` with `context.source="codex"`; §Phase D.5 → `template_id="code-reviewer-d5"`; §Phase D.2a.6 → `template_id="plan-remediator-narrow"`; §Phase D.4-rescue → `template_id="plan-remediator-rescue"`.
  - Each of the seven `dispatch-templates.md` section bodies gains the same header note pattern TASK-002 added to Phase D-Claude: `> Rendered by plan_ops__build_agent_dispatch_prompt(template_id="<id>", ...). The orchestrator does not read this section at runtime — see SKILL.md §Canonical Agent dispatch recipe.`
  - Template bodies are otherwise byte-identical (verified by `git diff --shortstat` showing only header-note additions per section).
  - The smoke test assertion from TASK-002 is extended to cover all eight phase paths. The assertion now reads: "across a dry-run that exercises Phase 1.5-Claude, Phase 1.5a, Phase 1-triage, Phase 1.5.5, Phase D-Claude, Phase D.5, Phase D.2a.6, and Phase D.4-rescue, zero Bash invocations match `awk.*dispatch-templates` and zero Read calls target `dispatch-templates.md`." Phases that do not naturally fire on the smoke fixture (e.g., D.4-rescue requires a specific failure pattern) are exercised via targeted mock-driven sub-tests rather than by widening the smoke fixture itself.
  - **Final SKILL doc pass:** the line in CLAUDE.md (`Subagents do not see this conversation — embed the full task block verbatim plus the dispatch-template skeleton.`) is updated to: "Subagents do not see this conversation. For wrapper dispatches use `plan_ops__build_*_dispatch_input`; for in-process Agent dispatches use `plan_ops__build_agent_dispatch_prompt`. Both render the dispatch payload from `dispatch-templates.md` in Python — the orchestrator never reads template bodies at dispatch time."
  - `grep -nE 'awk.*dispatch-templates|Read.*dispatch-templates' plugins/plan-executor/skills/implement-plan/SKILL.md` returns no results outside the §Canonical Agent dispatch recipe documentation block.
  - `venv/bin/pytest -q tests/scripts/test_implement_plan_directory_smoke.py tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` returns 0.
- **Reversion guidance:** revert the SKILL.md, dispatch-templates.md, CLAUDE.md, and smoke-test edits. The MCP tool variants from TASK-003 remain in place but are unused on the seven non-D-Claude branches; those branches fall back to the markdown-read pattern automatically.

**Description:**
Final cutover. Each of the seven non-D-Claude in-process Agent dispatch sites in SKILL.md is rewritten to invoke `plan_ops__build_agent_dispatch_prompt` via the §Canonical Agent dispatch recipe. Each of the seven `dispatch-templates.md` section bodies gains the same header note pattern TASK-002 added to Phase D-Claude. The CLAUDE.md `Subagent dispatch contract` line is refreshed to point at both MCP builder families. The smoke test assertion is widened to cover all eight phases. After this task lands, the orchestrator never reads `dispatch-templates.md` at dispatch time on any path.

**Implementation notes:**
- The eight phase-section rewrites are mechanically similar — read the existing prose, identify the "render the template manually" instruction, replace with the recipe invocation. Keep prose changes surgical: the rewrite should be 3-5 lines per section, not a section-level reorganization.
- For Phase 1.5a, the per-variant dispatch logic (which `plan-author-*` template_id to pick) lives in SKILL.md, not in the new MCP tool. The tool is content-agnostic about which variant the orchestrator chose; the SKILL still owns the routing decision.
- The smoke-test widening may require introducing per-phase mock fixtures because Phase D.4-rescue and Phase D.2a.6 only fire on specific failure paths. Use the existing `tests/fixtures/` patterns (e.g., the way `test_implement_plan_directory_smoke.py` already handles failure-path coverage) and do NOT introduce a new test framework for this.
- The CLAUDE.md edit is a one-line update — it's not a TASK on its own because the change is trivially reversible and tightly coupled to the cutover. If the implementer feels the CLAUDE.md change merits a separate commit, that's acceptable, but the `feat` commit for TASK-004 should be the load-bearing one.
- After this task lands, file a follow-up bug for the `gates` predicate that asserts the SKILL never reads `dispatch-templates.md` from Bash. The acceptance test in this task is run-time-only (it asserts no awk during a smoke); a static gate would catch SKILL edits that re-introduce the pattern. That's a separate plan.
