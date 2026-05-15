# PLAN — Follow-ups surfaced by PLAN_DEPRECATE_CLAUDE_CLI_2026-05-14

**Status:** Pending
**Created:** 2026-05-15
**Base branch:** main
**Parent run:** `20260515T020053` against `docs/plans/PLAN_DEPRECATE_CLAUDE_CLI_2026-05-14/`

## Goal

Close three loose ends that the parent plan's D.5 adjudications and Codex Phase D cross-reviews surfaced but explicitly punted out of scope:

1. The D.2a.5 `bounded_remediation` path still references a `plan-implementer` Agent template that has no `*-rework` variant registered — the parent plan's TASK-004 AC enumerated only the remediator-shaped templates (`narrow`, `rescue`) and the SKILL.md routing at line ~575 (`### Phase D.2a.5 — bounded remediation`) instructs the orchestrator to dispatch `plan-implementer` with the rework context. There is no registered template for that variant today, so the in-process Agent path for D.2a.5 is documented but unreachable.
2. A pre-existing test, `test_canary_probe_results_md_present_and_records_v3_keys` (`tests/scripts/test_claude_dispatch_canary.py:589`), asserts the presence of `docs/plans/SKILL_bash_dispatch_migration/probe_results.md`. The directory does not exist on disk; the parent plan's TASK-002 and TASK-005 implementers both flagged this regression but it was outside any task's `Files:` scope.
3. The MCP tool-surface enum for `plan_ops__build_agent_dispatch_prompt` is sourced from a JSON schema sidecar. There is no smoke test that fails when the schema enum diverges from a Python-side registry (today the JSON file IS the registry, but a defensive test would catch future refactors that introduce a Python-side constant out of sync with the schema). The parent plan's TASK-002 D.5 disagreement event recorded this as a follow-up.

## Findings

### 1. D.2a.5 bounded-remediation Agent template is unregistered

`plan_ops.py:12171–12245` registers four template_ids in the `_AGENT_DISPATCH_TEMPLATE_HEADING` / `_AGENT_DISPATCH_TEMPLATE_MODEL` / `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` maps that touch the `plan-implementer` / `plan-remediator` family:

- `plan-implementer-default` (model `opus`) — Phase B fresh implement.
- `plan-remediator-narrow` (model `opus`) — Phase D.2a.6 narrow remediation.
- `plan-remediator-rescue` (model `opus`) — Phase D.4 rescue.

There is **no** `plan-implementer-rework` or `plan-remediator-rework` template. SKILL.md:557 explicitly directs D.2a.5 to use `plan-implementer-default` with rework context forwarded into the renderer:

> Dispatch `plan-implementer` via §Canonical Agent dispatch recipe with `template_id:"plan-implementer-default"` (`model:"opus"`), forwarding the rework context into the renderer

But `plan-implementer-default`'s renderer in `plan_ops.py:12747` (`if template_id == "plan-implementer-default":`) consumes the `planImplementerDefaultContext` $def, which is the fresh-implement payload shape — it does NOT carry `prior_findings[]` / `prior_summary` / `attempt_count`. The wrapper-path `plan_claude_dispatch.py` `--variant rework` already carries this context (`build_claude_dispatch_input` with `variant="rework"`, see SKILL.md's recipe step requiring `dispatch_context` for `variant=rework`). The Agent path's default renderer doesn't.

So today D.2a.5 either (a) silently sends a fresh-implement prompt without the rework findings (correctness bug), or (b) the recipe is unreachable through the documented in-process Agent path and falls through to the wrapper path. The parent plan's TASK-004 was scoped to remediator-shaped templates and explicitly declared D.2a.5 plan-implementer rework as out of scope; D.5 adjudicated `ship-with-fixes` on TASK-004 with the explicit note that this is a separate plan-review finding.

### 2. Missing canary probe doc breaks an otherwise-green test suite

`tests/scripts/test_claude_dispatch_canary.py:74–76` declares:

```python
PROBE_RESULTS_PATH = (
    REPO_ROOT / "docs" / "plans" / "SKILL_bash_dispatch_migration" / "probe_results.md"
)
```

`test_canary_probe_results_md_present_and_records_v3_keys` (line 589) asserts the file exists and contains every key in `V3_REQUIRED_TOP_LEVEL_KEYS` (line 85, the canonical v3 wrapper envelope key set: `schema_version`, `status`, `status_reason`, `agent`, `model`, `session_id`, `duration_ms`, `cost_usd`, `tokens`, `result`, `result_raw_truncated`, `stderr_tail`, `permission_denials`, `scope`, ...).

The directory `docs/plans/SKILL_bash_dispatch_migration/` does not exist. The plan file `docs/plans/SKILL_bash_dispatch_migration.schedule.json` exists at the plans root but the decomposed-child directory was never materialized. The test was scaffolded for a future migration plan that never landed in this form.

Two viable fixes:

- (a) Create the doc with the v3 envelope key catalog and a short companion narrative tying each key to the wrapper-side emission site.
- (b) Delete the canary test (or move it to skipif when the doc is absent).

(a) is preferred because the test asserts a real wrapper contract — the v3 keys ARE the contract, and a written reference doc is cheap insurance against future drift. The parent plan's TASK-005 implementer report explicitly noted the doc as a documentation gap, not test scaffolding to be removed.

### 3. Schema-vs-registry divergence is structurally impossible today but worth a guard

Today `plan_ops.py:12171` (`_AGENT_DISPATCH_TEMPLATE_HEADING`), `:12177` (`_AGENT_DISPATCH_TEMPLATE_MODEL`), and `:12243` (`_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY`) ARE the Python-side registry. The JSON schema at `schemas/mcp/build_agent_dispatch_prompt.input.json` carries the same enum on the `template_id` field. The MCP server reads the JSON schema; the renderer in `plan_ops.py` reads the Python maps. A drift between the two would manifest as either:

- A JSON template_id that the Python renderer doesn't recognize → renderer raises with "unknown template_id".
- A Python template_id that the JSON schema doesn't list → MCP input validation rejects before the renderer sees it.

Both failure modes are loud, but they fail at runtime, not at test time. A schema-vs-registry smoke test would convert that runtime failure into a fast CI failure. Parent run 20260515T020053 hit a session-stale variant of this when Codex reviewer reported the MCP tool surface still rejected `plan-implementer-default` after the schema was updated — the root cause was session-snapshot staleness, not actual divergence, but the false positive itself argues for a defensive test.

## Decisions

1. **Three independent tasks.** No shared file scope between the three follow-ups; trivially parallel-safe.
2. **TASK-001 picks the renderer-extension path, not a new template_id.** Extend `plan-implementer-default`'s renderer to accept optional `prior_findings[]` / `prior_summary` / `attempt_count` fields in `planImplementerDefaultContext` (additive, schema-permissive). When present, the renderer inlines a "Prior attempt findings (D.2a.5 bounded remediation)" block above the fresh-implement instructions. When absent, the renderer emits the existing fresh-implement prompt unchanged. This avoids template_id proliferation, keeps the wrapper-path and Agent-path schemas aligned (the wrapper already accepts an optional `dispatch_context`), and the inliner is the natural single source of truth — same approach as the parent plan's `_inline_implementer_result_schema` helper. Alternative considered: register a separate `plan-implementer-rework` template_id. Rejected because (i) the prompt body is 95% identical to `plan-implementer-default`, (ii) it would duplicate the dispatch-templates.md section, and (iii) the variant axis already exists on the wrapper side as a payload field, not a separate dispatch ID.
3. **TASK-002 ships the doc, not a test deletion.** Per Finding 2 — the canary asserts a real contract.
4. **TASK-003 adds the cross-file invariant test.** A single test that imports both the JSON schema and the Python maps, asserts the template_id sets match exactly. Cheap, fast, catches the next refactor's drift.

## Scope

In scope:

- `plugins/plan-executor/scripts/plan_ops.py` — extend the `plan-implementer-default` renderer to consume optional rework context.
- `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` — extend `planImplementerDefaultContext` $def with optional `prior_findings`, `prior_summary`, `attempt_count` fields.
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — extend the Phase B template body so it carries the conditional rework-findings block.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — clarify line ~557 / line ~575 (the D.2a.5 routing prose) to point at the extended renderer; cross-link the §Cleanup-around-Agent-dispatch sub-recipe carve-out for rework variant.
- `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` — new file documenting the v3 wrapper envelope keys.
- `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` — new test cases for the rework-context branch and the schema-vs-registry invariant.

Out of scope:

- Registering new template_ids for D.4 rescue or D.2b role-swap variants (those have their own existing templates).
- Changes to the wrapper-side `plan_claude_dispatch.py run --variant rework` path — already correct.
- Renaming or relocating the canary test.
- Migrating `plan-remediator-narrow` / `plan-remediator-rescue` to consume `planImplementerDefaultContext` (different contract).
- A Python-side registry constant (the JSON schema remains the source of truth; the new test pins the invariant on schema-equals-maps, not on a new constant).

## Tasks

### TASK-001: Extend `plan-implementer-default` renderer + schema to carry optional D.2a.5 rework context, and re-wire SKILL.md's D.2a.5 routing

- **Status:** Pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py -k "implementer_default or rework"`
- **Acceptance criteria:**
  - Extend the `planImplementerDefaultContext` $def in `schemas/mcp/build_agent_dispatch_prompt.input.json` with three optional fields: `prior_findings` (array of `{severity, file, line, issue, suggested_fix}` objects, matching the reviewer-finding shape already used by `plan_ops__parse_reviewer_envelope`), `prior_summary` (string), and `attempt_count` (integer, ≥1). All three are optional; absence means "fresh implement" and the renderer's output is byte-identical to today.
  - Extend the `plan-implementer-default` renderer branch in `plan_ops.py:12747` (`if template_id == "plan-implementer-default":`) to read the optional fields from the context dict. When `prior_findings` is non-empty OR `prior_summary` is non-empty, inline a clearly delimited "Prior attempt — D.2a.5 bounded remediation context" section above the existing fresh-implement instructions. The section enumerates each finding (severity, file:line, issue, suggested_fix) and emits the summary verbatim. When all three are absent, the rendered prompt is byte-identical to the pre-change output (regression-pinned in tests).
  - Add a conditional block to the Phase B template body in `dispatch-templates.md` (the same heading anchor used by `_AGENT_DISPATCH_TEMPLATE_HEADING["plan-implementer-default"]` at `plan_ops.py:12201`). Use the renderer's existing context-substitution machinery — do not duplicate finding-formatting logic in the markdown.
  - Rewrite SKILL.md:557 (`dispatch_bounded_remediation`) and SKILL.md:575 (`### Phase D.2a.5 — bounded remediation`) so they explicitly state: D.2a.5 dispatches `plan-implementer-default` with `prior_findings`, `prior_summary`, and `attempt_count` populated in the `planImplementerDefaultContext` payload. Cross-link the §Cleanup-around-Agent-dispatch sub-recipe — D.2a.5 is file-editing, so the cleanup wrap applies with `authorization_source="orchestrator-declared-scope"`.
  - Add two test cases in `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`:
    - `test_plan_implementer_default_fresh_implement_prompt_byte_stable`: build the renderer payload with no rework fields. Capture the rendered prompt. Pin it as a golden string (or assert byte-equivalence against a captured baseline). Regression guard — the fresh-implement Phase B path must not drift.
    - `test_plan_implementer_default_emits_prior_findings_block_when_supplied`: build the renderer payload with two findings, a summary, and `attempt_count: 2`. Assert: (i) the rendered prompt contains every finding's `issue` text verbatim, (ii) the summary string appears, (iii) the attempt counter appears, (iv) the fresh-implement section is still present below the rework block.
  - Schema validation: `venv/bin/python -m jsonschema -i tests/scripts/fixtures/build_agent_dispatch_prompt/implementer_default_with_rework_context.json plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` (or equivalent) must pass — add the fixture alongside the test.
- **Implementation notes:** Read `plan_ops.py:12700–12800` (the renderer branch dispatch table) and `plan_ops.py:12920–13000` (the `_inline_implementer_result_schema` helper) end-to-end before editing. The renderer pattern for conditional blocks is already established by the `dispatch_context` handling in `build_claude_dispatch_input` (search for `"prior_findings"` — the wrapper-side renderer for `--variant rework` is the reference implementation). Mirror that prose so the wrapper-path and Agent-path rework prompts converge.
- **Reversion guidance:** Drop the three new fields from the $def, revert the renderer branch to its current shape (the `if template_id == "plan-implementer-default":` block stays, but the rework-context branch is removed), revert SKILL.md:557 and :575 to the current text, drop the conditional block in dispatch-templates.md, delete the two new test cases and the fixture file.

### TASK-002: Create the missing `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` to unblock the canary test

- **Status:** Pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `docs/plans/SKILL_bash_dispatch_migration/probe_results.md`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_canary.py::test_canary_probe_results_md_present_and_records_v3_keys`
- **Acceptance criteria:**
  - The file `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` exists.
  - The file contains every string in `tests/scripts/test_claude_dispatch_canary.py:V3_REQUIRED_TOP_LEVEL_KEYS` (the canonical v3 wrapper envelope key set: `schema_version`, `status`, `status_reason`, `agent`, `model`, `session_id`, `duration_ms`, `cost_usd`, `tokens`, `result`, `result_raw_truncated`, `stderr_tail`, `permission_denials`, `scope`, and any other entries in the constant at test-time — read the source of truth, do not transcribe from memory). Each key appears at least once as plain text in the markdown body.
  - The doc is a short reference (≤2 pages) tying each v3 envelope key to its wrapper-side emission site in `plan_claude_dispatch.py`. Format: a markdown table or definition list with columns/labels "Key", "Type", "Emission site", "Notes". The notes column documents protocol invariants (e.g., `status` is the universal commit-forbidding sentinel when not `ok`; `cost_usd` may be `null` on dry-run; `result` carries the inlined-schema-validated agent output).
  - The doc's title line is `# v3 Claude wrapper envelope — key reference` (matches the SKILL bash-dispatch migration's vocabulary).
  - The canary test (named in the test command) passes.
- **Implementation notes:** Read `plan_claude_dispatch.py` and grep for each key in `V3_REQUIRED_TOP_LEVEL_KEYS` to identify the emission site (look for `envelope[<key>] =` or similar assignment patterns). The wrapper's v3 envelope assembly is centralized — there should be a single function that builds the full envelope; document that as the canonical emission point in the "Notes" column for keys without a more specific site. Do NOT invent invariants — pull them from `plan_ops.py:claude_envelope_extract` and the `agents/plan-implementer.md` / `agents/plan-remediator.md` agent contracts.
- **Reversion guidance:** Delete the file. The canary test goes back to failing; the parent issue resurfaces for a future plan.

### TASK-003: Add a smoke test pinning the JSON-schema template_id enum to the Python-side renderer maps

- **Status:** Pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py -k "template_id_registry_invariant"`
- **Acceptance criteria:**
  - Add a new test `test_template_id_enum_matches_renderer_registry` to `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`.
  - The test loads `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` and extracts the `template_id` field's enum (the JSON path is `properties.template_id.enum` — verify the exact path by reading the schema file before writing the test).
  - The test imports `_AGENT_DISPATCH_TEMPLATE_HEADING`, `_AGENT_DISPATCH_TEMPLATE_MODEL`, and `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` from `plugins.plan_executor.scripts.plan_ops` (adjust import path if `plan_ops.py` is imported a different way in this test file — read existing imports first).
  - The test asserts that the JSON enum, the keys of `_AGENT_DISPATCH_TEMPLATE_HEADING`, the keys of `_AGENT_DISPATCH_TEMPLATE_MODEL`, and the keys of `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` are all the same set (use `set(...) == set(...)` four-way equality with a clear assertion message that names the diff on failure).
  - The test passes against `main` as of TASK-001 merge (the rework-context extension does not add new template_ids, so the invariant holds).
  - Per-template renderer branches: optionally extend the test to assert that every template_id in the JSON enum has a corresponding `if template_id == "...":` branch in `plan_ops.py`'s render function. If detection is fragile (regex over Python source), the four-way set equality is sufficient — the per-branch assertion is a stretch goal.
- **Implementation notes:** Read `plan_ops.py:12171–12245` to confirm the three map names and the convention. Read `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`'s existing imports so the new test's import statement matches the file's convention (some test files import `plan_ops` as a module, others import specific symbols). Use `importlib.util.spec_from_file_location` only if direct import is not possible — direct import is strongly preferred.
- **Reversion guidance:** Delete the new test function. The defensive guard goes away; runtime failures resurface (loud but late).

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py` — TASK-001 + TASK-003.
2. `venv/bin/pytest -q tests/scripts/test_claude_dispatch_canary.py` — TASK-002 (the canary file is now present).
3. `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "claude_dispatch or build_agent or inline_implementer"` — regression on the parent plan's TASK-002 invariants.
4. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json` — standing audit must still pass.

Manual smoke for TASK-001:

1. Build a 1-task plan with a Claude implementer.
2. Run `/implement-plan` against it with a forced D.2a.5 path (e.g., a Codex reviewer that returns `needs-rework` once, then `ship`).
3. Inspect the second `claude_dispatch_start` event in `_run_log.jsonl` — its rendered prompt MUST contain the "Prior attempt — D.2a.5 bounded remediation context" block with the first-round Codex findings inlined.
4. Confirm the run lands a clean commit and no `claude -p` subprocess was spawned during the SKILL/MCP path (`pgrep -af 'claude .*-p'` empty).
