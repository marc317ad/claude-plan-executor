# PLAN — Deprecate `claude -p` nested-dispatch wrapper for in-process Agent dispatch

**Status:** Pending
**Created:** 2026-05-14
**Revised:** 2026-05-14 (post Codex + Gemini review)
**Base branch:** main

## Goal

Remove the plugin's dependency on the `claude -p` CLI subprocess for all Claude-tier dispatches inside `/implement-plan`. Anthropic is removing `claude -p` from subscription billing, so any further use becomes uneconomical. Re-route the three wrapper-dispatched Claude sites — `plan-analyst` (Phase 1 Step 2), `plan-implementer` (Phase B), `plan-remediator` (Phase B-rework / B-narrow / D.4-rescue) — back to in-process Agent dispatches via `plan_ops__build_agent_dispatch_prompt`. Preserve the wrapper's delta-bounded cleanup safety net by promoting `_claude_dispatch_cleanup.py` to a first-class library that the orchestrator (and any other caller) can invoke directly around an Agent dispatch. Leave the Codex and Gemini wrapper paths untouched — Claude remains the preferred implementer per the user's "Claude is technically better at the hard stuff" rationale; only the *transport* changes.

## Non-goals

- Switching the default implementer to Codex or Gemini. Out of scope; tracked separately.
- Removing the Codex / Gemini wrappers. Both retain their existing roles (Codex implement / cross-review, Gemini fallback review).
- Rewriting the cleanup algorithm. The TASK-008 delta-bounded snapshot+revert logic in `_claude_dispatch_cleanup.py` is correct and stays as-is; only the *caller* changes.
- Removing the schema-validated envelope shape. `claude_envelope_extract` continues to be the canonical parser for the implementer's JSON-result; the Agent's prompt embeds the same schema the wrapper inlined.
- Worktree isolation, parallel-batch race fixes, or any other run-state work — handled by separate plans.

## Findings

### 1. The wrapper is the only place `claude -p` is spawned, and has TWO callers

`plugins/plan-executor/scripts/_claude_backend.py:498–507` constructs the argv (`backend_binary, "-p", "--agent", …, "--output-format", "json"`) and `_claude_backend.py:687–694` does the `subprocess.Popen`. Confirmed (Codex Finding 3) as the single production `claude -p` site.

The wrapper is reached from **two** callers, not one:

a. **The `/implement-plan` SKILL/MCP orchestration** (this plan's primary target) — three sites:
- §Phase 1 Step 2 plan-analyst per-child fan-out (SKILL.md:308–316).
- §Phase B plan-implementer (SKILL.md:447) — every Claude-tier task on the default path.
- §Claude wrapper dispatch recipe (canonical) (SKILL.md:97, 105–111) — referenced by `plan-remediator` variants `rework` and `narrow-remediation` and by the D.4-rescue flow.

b. **The standalone script runner `plugins/plan-executor/scripts/implement_plan.py`** (Gemini Finding 1) — `ClaudeProvider._claude` (`implement_plan.py:1173–1213`) calls `self.plan_ops.build_claude_dispatch_input(...)` and then `self._run("run", "--input", "-", ...)` against `plan_claude_dispatch.py` (per the `dispatch_command="plan_claude_dispatch.py"` constant at `implement_plan.py:228`). This runs as a standalone Python process — it has NO access to the harness Agent tool. Gemini correctly flagged that shimming the wrapper unconditionally would break this caller.

Every other Claude subagent in the SKILL — `code-reviewer` (Phase D / D.5), `plan-reviewer` (Phase 1.5-Claude), `plan-review-triage`, `plan-author-*` — already dispatches in-process via `plan_ops__build_agent_dispatch_prompt`. No CLI subprocess, no billing impact.

**Implication for the plan.** The shim approach (TASK-005 as originally drafted) is wrong — it would break the script runner. Revised: the `/implement-plan` SKILL/MCP orchestration cuts over to in-process Agent dispatch (no longer calls the wrapper); the wrapper itself stays functional for `implement_plan.py`'s `ClaudeProvider`. We document loud-and-clearly that the script-runner Claude path remains a paid `claude -p` invocation post-Anthropic-deprecation, and recommend operators of the script runner switch to `--implementer codex` if cost is a concern. A separate follow-up plan can decide whether to deprecate the script-runner Claude provider entirely.

### 2. The wrapper's value-add decomposes into three components

`plan_claude_dispatch.py` provides four behaviors on top of `claude -p`:

a. **Schema-validated I/O envelope** — input `claude_dispatch_input.json`, output `claude_dispatch_output.json`. The output shape is consumed by `plan_ops__claude_envelope_extract` which already understands both wrapper-envelope and bare-result variants (it is invoked unconditionally on §Canonical Agent dispatch recipe sites today). This is portable.

b. **Delta-bounded cleanup** — `_claude_dispatch_cleanup.py` snapshots the working tree before dispatch and reverts every post-dispatch path that is not in `declared_files_changed` after dispatch. This is the wrapper's primary safety property and the only one that materially protects against scope-violating subagents. The module already has a clean Python API: `snapshot_baseline(repo_root) -> dict` and `apply_cleanup(baseline, declared_files_changed, repo_root, *, authorization_source, unattended_revert_policy) -> dict` (`_claude_dispatch_cleanup.py:286, 597`). Both functions are pure orchestration over `git` plumbing — they have no dependency on the `claude -p` subprocess. They are already importable from outside the wrapper.

c. **Hard timeout** — `DEFAULT_DISPATCH_TIMEOUT_SEC = 1800s` enforced via `subprocess.run(timeout=...)`. The in-process Agent tool has no equivalent. Loss is acceptable: Agent dispatches inherit the parent CLI session's existing UX (operator can interrupt; no silent hangs because the orchestrator is interactive).

d. **Sanitized child env** (`_claude_guardrails.py`) — strips most of the parent env before spawning `claude -p`. Moot for in-process Agent: there is no child process; the subagent runs inside the orchestrator's own process and inherits its env regardless. This is acceptable because (i) the orchestrator is already trusted, and (ii) the in-process Agent path has been the reviewer / triage / author transport for months without incident.

### 3. The `_claude_dispatch_cleanup.py` authorization gate already exists, but uses TWO tokens that preserve a write-vs-readonly distinction

Codex Finding 1 corrected my initial reading. The live allowlist at `_claude_dispatch_cleanup.py:141–144` is:

- `"wrapper-declared-scope"` — write-authorized agents (`plan-implementer` / `plan-remediator`). Reverts the observed delta minus `declared_files_changed` minus protected paths.
- `"wrapper-empty-scope-readonly"` — read-only agents (`plan-analyst`). Any observed delta is a contract violation and reverted.

Both tokens map to the same per-file restore gate at `_RESTORE_GATE_TRANSLATION` (`_claude_dispatch_cleanup.py:161–164`), which in turn is consumed by `_restore_path` in `preserve-only` policy mode. **A new authorization source MUST also be added to `_RESTORE_GATE_TRANSLATION`, otherwise `apply_cleanup` under `preserve-only` will KeyError at the per-file restore call.**

Decision (revised): add **two** new tokens, mirroring the existing distinction — `"orchestrator-declared-scope"` (for implementer / remediator orchestrator-direct dispatches) and `"orchestrator-empty-scope-readonly"` (for analyst, even though analyst is exempted from the cleanup wrap by Decision 7 below — registering the token preserves symmetry and keeps the door open for future read-only Agent dispatches that might want defensive cleanup).

### 3a. `apply_cleanup` does NOT default to revert behavior

Codex Finding 2: `apply_cleanup(..., unattended_revert_policy=None)` defaults to `pause` semantics — it returns `cleanup_strategy="detect_only_revert_policy_pause"` and does NOT revert files. Reversion only happens under `unattended_revert_policy="preserve-only"` (`_claude_dispatch_cleanup.py:842–847`). The orchestrator-side cleanup wrap MUST pass `unattended_revert_policy="preserve-only"` to actually scrub out-of-scope writes; passing `"pause"` (the default) only detects them and leaves them in the working tree for the orchestrator's existing scope-violation routing to handle. Both behaviors are valid — the plan picks `"preserve-only"` as the orchestrator-direct default to match the wrapper's existing behavior under the same default operator policy.

### 4. SKILL inconsistency on `plan-remediator` is real, AND the wrapper builder still supports a `narrow-remediation` variant

Codex Finding 4 confirmed and refined this:

- SKILL.md:97 lists `plan-remediator` under the wrapper recipe (stale).
- SKILL.md:559, 579, 595 describe `plan-remediator` variants `narrow` and `rescue` as Agent-dispatched.
- Live Agent registry (`plan_ops.py:12171–12184`) registers `plan-remediator-narrow` and `plan-remediator-rescue` template_ids — confirming the Agent path is the live one.
- BUT `build_claude_dispatch_input` (`plan_ops.py:12836–12857`) still supports a wrapper variant `"narrow-remediation"` mapped to `plan-remediator`. This is reachable from the script runner's `ClaudeProvider` (TASK-004 below treats it as legacy/script-runner-only and leaves it functional; if a follow-up plan deprecates the script-runner Claude provider, that wrapper variant can be removed then).

This plan reconciles the SKILL prose and documents the wrapper variant as legacy-script-runner-only; it does NOT remove the variant from the builder.

### 5. Schema inlining for Phase B happens in the BUILDER, not the template — and `output_instructions.format` is template prose only

Codex Finding 6 corrected my initial reading. The Phase B template body (`dispatch-templates.md:423–533`, particularly :456–461) shows `output_instructions.format: "json"` as illustrative prose, but the actual JSON-output enforcement comes from two unrelated places:

- The CLI flag `--output-format json` baked into `_claude_backend.py:498–507`.
- Dynamic schema inlining inside `build_claude_dispatch_input` at `plan_ops.py:13055–13153`, which loads `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` and embeds it into `payload.prompt`. The same code copies the schema into the wrapper envelope at `plan_ops.py:13155–13164` so `claude_envelope_extract` can validate.

Decision (revised): factor the schema-inlining helper out of `build_claude_dispatch_input` into a shared private helper (e.g., `_inline_implementer_result_schema(prompt: str) -> tuple[str, dict]`). Both `build_claude_dispatch_input` AND `build_agent_dispatch_prompt`'s new `plan-implementer-default` template_id renderer call it. This avoids the double-inlining hazard Gemini Finding 7 flagged, and ensures the Agent path emits the same schema-bearing prompt the wrapper does today.

Phase A-single (`dispatch-templates.md:23–99`) already inlines its classifier-result schema in the template body — no helper needed for analyst.

### 5a. Required Agent template_ids are NOT yet registered

Both reviewers agreed (Codex Finding 5, Gemini Finding 3): `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at `plan_ops.py:12175–12197` register `code-reviewer`, `plan-reviewer`, `plan-author-*`, `plan-review-triage`, `plan-remediator-narrow`, and `plan-remediator-rescue` — but NOT `plan-implementer-default` or `plan-analyst-per-child`. TASK-002 and TASK-003 must explicitly register these (heading + model + template body anchor), not "verify and add if missing."

### 6. Existing test coverage anchors the touched surfaces

- `tests/scripts/test_claude_dispatch*.py` — wrapper subprocess path + envelope shape. After cutover, the subset that exercises `plan_claude_dispatch.py run` end-to-end becomes irrelevant; the cleanup-library tests stay (and grow).
- `tests/scripts/test_plan_ops_build_agent_dispatch_prompt*.py` — Agent-prompt builder. Gains coverage for the new Phase B, Phase A-single, and `plan-remediator` template-id variants if not already present.
- `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` — implementer JSON-result schema. Keep as the authoritative copy referenced from both the wrapper-deprecation shim (if retained) and the inline-in-template copy.

## Decisions

1. **Promote `_claude_dispatch_cleanup.py` to a first-class library, callable directly by the orchestrator.** Rename to `_dispatch_cleanup.py`. Extend the `authorization_source` allowlist with `"orchestrator-declared-scope"` AND `"orchestrator-empty-scope-readonly"` (preserving the existing write-vs-readonly distinction per Finding 3). Extend `_RESTORE_GATE_TRANSLATION` (`_claude_dispatch_cleanup.py:161–164`) so both new tokens map to `"wrapper_internal_cleanup_explicit_declaration"` — without this the `preserve-only` per-file restore path KeyErrors. No algorithm change.

2. **Cut over each of the three SKILL/MCP wrapper-dispatched sites to §Canonical Agent dispatch recipe.** Each site swaps `build_claude_dispatch_input + Bash plan_claude_dispatch.py run` for `build_agent_dispatch_prompt + Agent(subagent_type=...)`. For implementer / remediator: wrap with `cleanup.snapshot_baseline()` before and `cleanup.apply_cleanup(..., authorization_source="orchestrator-declared-scope", unattended_revert_policy="preserve-only", declared_files_changed=<extracted Files: list>)` after. For analyst: skip the cleanup wrap entirely (analyst is read-only by contract; Decision 7).

3. **Factor implementer JSON-result schema inlining into a shared helper called by both renderers.** Today `build_claude_dispatch_input` (`plan_ops.py:13055–13164`) loads `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` and embeds it into `payload.prompt` AND copies it into the wrapper envelope. Extract a private `_inline_implementer_result_schema(prompt: str) -> tuple[str, dict]` (or equivalent shape) that returns `(prompt_with_schema, schema_dict)`. Both `build_claude_dispatch_input` and the new `plan-implementer-default` Agent renderer call it. The Phase B template body in `dispatch-templates.md` is updated to reference the helper as the canonical inliner (replacing illustrative `output_instructions.format` prose) — but the schema text itself is NOT duplicated into the template body, avoiding the double-inlining hazard Gemini Finding 7 flagged. `plan-implementer.md`'s line-105 paragraph is rewritten: trigger is now "in-process Agent dispatch via the `plan-implementer-default` template" OR "wrapper dispatch from the script runner"; the schema requirement is unchanged.

4. **Register the missing Agent template_ids.** Add `plan-implementer-default` and `plan-analyst-per-child` (or equivalent names) to `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at `plan_ops.py:12175–12197`. Mirror the existing `plan-remediator-narrow` registration shape. Without this, the Agent renderer cannot resolve the new template_ids and TASK-002 / TASK-003 cannot land.

5. **Keep `plan_claude_dispatch.py run` FUNCTIONAL** — do NOT shim it as originally drafted. Gemini Finding 1 surfaced that `implement_plan.py:1173–1213` (`ClaudeProvider`) calls `plan_claude_dispatch.py run` from a standalone Python process that has no access to the harness Agent tool. Shimming would silently break the script-runner Claude path. Instead: TASK-005 narrows to (a) deleting the SKILL §Claude wrapper dispatch recipe section, (b) adding a top-of-file deprecation note to `README_claude_dispatch.md` clarifying that `run` is now used only by the script runner and shares billing with the parent `claude` session (so post-Anthropic-deprecation it becomes paid usage), and (c) recommending script-runner operators switch to `--implementer codex` if cost is a concern. A separate follow-up plan can decide whether to deprecate the script-runner Claude provider entirely.

6. **Reconcile SKILL prose for `plan-remediator`.** Remove `plan-remediator` from the §Claude wrapper dispatch recipe (canonical) enumeration at SKILL.md:97. The narrow-remediation and rescue paths (SKILL.md:559, 579, 595) already use Agent dispatch and gain the cleanup wrap from Decision 2.

7. **Analyst is exempt from the cleanup wrap.** Classifier-shaped dispatches (`plan-analyst-per-child` Phase 1 Step 2) do not edit files; wrapping them with snapshot+apply is wasted work. Document the carve-out explicitly in the §Cleanup-around-Agent-dispatch sub-recipe.

8. **Red-before-green, strict topo chain.** TASK-001 promotes the cleanup library, adds the new tokens to the allowlist AND to `_RESTORE_GATE_TRANSLATION`, and adds orchestrator-side authorization tests (with `unattended_revert_policy="preserve-only"` per Finding 3a). TASK-002 registers `plan-implementer-default`, factors the shared schema inliner, and cuts over Phase B `plan-implementer` (highest-traffic site, biggest economic impact). TASK-003 registers `plan-analyst-per-child` and cuts over the Phase 1 Step 2 fan-out. TASK-004 cuts over `plan-remediator` (or verifies it is already cut over) and reconciles the SKILL inconsistency. TASK-005 lands the SKILL deletion + README banner WITHOUT shimming the wrapper. All five are serial.

## Scope

In scope:

- `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` → rename to `_dispatch_cleanup.py`, extend `authorization_source` allowlist, expose stable `snapshot_baseline` / `apply_cleanup` API as the orchestrator-callable surface.
- `plugins/plan-executor/scripts/plan_ops.py` — extend `build_agent_dispatch_prompt` template registry if a new template_id variant is needed for `plan-implementer` or `plan-analyst` (likely the existing IDs already work; verify in TASK-002 / TASK-003).
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — rewrite §Phase 1 Step 2, §Phase B, and §Claude wrapper dispatch recipe sections; reconcile :97 / :559 prose; add a §Cleanup-around-Agent-dispatch sub-recipe documenting the snapshot/apply pattern.
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — inline the implementer JSON-result schema into the Phase B template body.
- `plugins/plan-executor/agents/plan-implementer.md` — rewrite the "JSON-dispatch path (BUG-146)" paragraph; trigger is now "in-process Agent dispatch" not "wrapper-dispatched run".
- `plugins/plan-executor/scripts/plan_claude_dispatch.py` — `run` subcommand becomes a deprecation shim; other subcommands unchanged.
- `tests/scripts/test_plan_ops_build_agent_dispatch_prompt*.py` — extend coverage for Phase B / Phase A-single / `plan-remediator` template-id variants if gaps exist.
- `tests/scripts/test_dispatch_cleanup*.py` (renamed from `test_claude_dispatch_cleanup*.py`) — add `authorization_source="orchestrator"` cases.

Out of scope:

- Removing `_claude_backend.py`. Stays for now; called only by the deprecated `plan_claude_dispatch.py run`. Removed in the same follow-up plan that removes the shim.
- Removing `_claude_guardrails.py`. Same rationale.
- Removing or migrating Codex / Gemini wrappers.
- Touching `plan-reviewer`, `code-reviewer`, `plan-review-triage`, `plan-author-*` dispatches — they are already in-process and unaffected.
- Adding a hard timeout to in-process Agent dispatch. Documented as a known regression in §Verification.
- `claude_envelope_extract` semantics. Stays; its existing dual-shape support already covers both transports.

## Tasks

### TASK-001: Promote `_claude_dispatch_cleanup.py` to first-class `_dispatch_cleanup.py` library

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` → `plugins/plan-executor/scripts/_dispatch_cleanup.py` (rename)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (update import)
  - `tests/scripts/test_claude_dispatch_cleanup*.py` → `tests/scripts/test_dispatch_cleanup*.py` (rename + extend)
- **Dependencies:** none
- **Acceptance criteria:**
  - `git mv` the cleanup module to `_dispatch_cleanup.py`. Update the one importer (`plan_claude_dispatch.py:85`) to `import _dispatch_cleanup as cleanup`. Verify with `grep -rn "_claude_dispatch_cleanup" plugins/ tests/` that no other in-tree importer exists; update any that surface.
  - Extend `ALLOWED_CLEANUP_AUTHORIZATION_SOURCES` (`_dispatch_cleanup.py:141–144`) with `"orchestrator-declared-scope"` AND `"orchestrator-empty-scope-readonly"`. Mirror the existing wrapper-token semantics (write-authorized vs read-only).
  - Extend `_RESTORE_GATE_TRANSLATION` (`_dispatch_cleanup.py:161–164`) with both new tokens, each mapping to `"wrapper_internal_cleanup_explicit_declaration"` (the same per-file restore gate the wrapper tokens use). Without this, `apply_cleanup` under `unattended_revert_policy="preserve-only"` will KeyError at the per-file restore call (per Finding 3).
  - Update the docstring at `_dispatch_cleanup.py:115–138` to document the new orchestrator-direct caller and the shape of the new tokens.
  - Add `test_apply_cleanup_accepts_orchestrator_declared_scope`: snapshot a tiny tmp repo, write a file outside the declared set, call `apply_cleanup(..., authorization_source="orchestrator-declared-scope", declared_files_changed=[<in-scope path>], unattended_revert_policy="preserve-only")`, assert the out-of-scope file is reverted and the result dict carries `cleanup_strategy="delta_bounded"`. NOTE: must pass `unattended_revert_policy="preserve-only"` — the default `pause` semantics return `cleanup_strategy="detect_only_revert_policy_pause"` and do NOT revert (per Finding 3a).
  - Add `test_apply_cleanup_accepts_orchestrator_empty_scope_readonly`: snapshot, write any file, call `apply_cleanup(..., authorization_source="orchestrator-empty-scope-readonly", declared_files_changed=[], unattended_revert_policy="preserve-only")`, assert the file is reverted (read-only contract: any delta is a violation).
  - Add `test_apply_cleanup_rejects_unknown_authorization_source`: call with `authorization_source="rogue"`, assert it raises ValueError (current behavior — pin it as a contract test now that the allowlist has four entries).
  - All existing `test_claude_dispatch_cleanup*` tests still green under the new module name.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_dispatch_cleanup`
- **Implementation notes:** Pure rename + allowlist + translation-table extension. The algorithm is unchanged. Do NOT inline a backward-compat re-export at `_claude_dispatch_cleanup.py` — the in-tree importer count is small and we update them all in the same TASK.
- **Reversion guidance:** `git mv` back, revert the importer change(s), revert the allowlist + translation-table extensions, drop the three new tests.

### TASK-002: Register `plan-implementer-default` Agent template, factor shared schema inliner, cut over Phase B to in-process Agent dispatch

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` — register `plan-implementer-default` in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` (~line 12175–12197); factor `_inline_implementer_result_schema(prompt) -> tuple[str, dict]` out of `build_claude_dispatch_input` (~line 13055–13164) and call it from both renderers.
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — Phase B template body (lines 423–533): replace illustrative `output_instructions.format` prose with a clear "the orchestrator's renderer inlines the implementer result schema below; emit one JSON object matching it" instruction. Do NOT duplicate the schema text in the template body — the inliner injects it at render time so the wire-format remains single-sourced from `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json`.
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — Phase B section (~line 447); add §Cleanup-around-Agent-dispatch sub-recipe.
  - `plugins/plan-executor/agents/plan-implementer.md` — BUG-146 paragraph at line ~105.
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt*.py` — add coverage for the new template_id and the shared inliner.
  - `tests/scripts/test_plan_ops.py` (or wherever `build_claude_dispatch_input` is tested) — pin that the shared inliner produces byte-equivalent output to the pre-refactor wrapper path (regression guard for the script-runner Claude path).
- **Dependencies:** 001
- **Acceptance criteria:**
  - Register `plan-implementer-default` template_id in `_AGENT_DISPATCH_TEMPLATE_HEADING` (heading anchor in `dispatch-templates.md`) and `_AGENT_DISPATCH_TEMPLATE_MODEL` (`"opus"`). Mirror the `plan-remediator-narrow` registration shape at `plan_ops.py:12175–12197`.
  - Factor `_inline_implementer_result_schema(prompt: str) -> tuple[str, dict]` in `plan_ops.py`. Move the schema-loading + prompt-injection logic from `build_claude_dispatch_input` (~`plan_ops.py:13055–13153`) into the helper. Both `build_claude_dispatch_input` AND the new `plan-implementer-default` Agent renderer call the helper. The schema dict the helper returns continues to be copied into the wrapper envelope at the existing site (`plan_ops.py:13155–13164`); the Agent renderer does NOT need the dict (the Agent path's caller validates via `claude_envelope_extract` which can parse the result without the envelope-side schema copy).
  - Add `test_inline_implementer_result_schema_byte_stable_vs_wrapper_path`: build a minimal claude_dispatch_input and a minimal Agent-renderer payload through both code paths; assert the inlined-schema portion of the prompt is byte-identical. Guards against drift between the script-runner Claude path and the orchestrator Agent path.
  - SKILL.md:447 (the "Claude tasks" bullet under §Phase B dispatch routing) is rewritten to reference §Canonical Agent dispatch recipe with `template_id:"plan-implementer-default"` and `model:"opus"`. The bullet documents the orchestrator-side cleanup wrap: call `cleanup.snapshot_baseline(repo_root)` immediately before the Agent dispatch and `cleanup.apply_cleanup(baseline, declared_files_changed, repo_root, authorization_source="orchestrator-declared-scope", unattended_revert_policy="preserve-only")` immediately after. Result dict feeds the existing scope-violation routing unchanged.
  - SKILL.md gains a short §Cleanup-around-Agent-dispatch sub-recipe (5–10 lines) that the §Phase B and later TASKs reference. Canonical pattern: extract `declared_files_changed` via `_extract_task_files_from_plan`; snapshot; Agent-dispatch; apply_cleanup with `authorization_source="orchestrator-declared-scope"` (write-authorized) or `"orchestrator-empty-scope-readonly"` (analyst — but analyst skips the wrap entirely per Decision 7); route the cleanup result through the existing scope-violation handling.
  - The Phase B template body in `dispatch-templates.md` (lines 423–533) is updated to the rephrased instruction (above). The existing `output_instructions.format: "json"` illustrative line is removed; the new line points the subagent at the inlined schema that will appear in its prompt.
  - `agents/plan-implementer.md`'s line-105 paragraph is rewritten: trigger is now "in-process Agent dispatch via the `plan-implementer-default` template (the `/implement-plan` orchestration default) OR wrapper dispatch via `plan_claude_dispatch.py run` (the `implement_plan.py` script-runner path)". Both paths inline the same schema via the shared `_inline_implementer_result_schema` helper. The "you MUST emit" JSON-result requirement is unchanged.
  - End-to-end smoke (manual, in §Verification): run `/implement-plan` against a one-task plan with a single Claude implementer; observe (i) no `claude -p` subprocess spawned during the SKILL/MCP path (`pgrep -af 'claude .*-p'` empty), (ii) cleanup result emitted in the run log with `authorization_source="orchestrator-declared-scope"`, (iii) commit lands cleanly.
  - All existing `test_plan_ops*`, `test_claude_dispatch*`, and `test_plan_ops_build_agent_dispatch_prompt*` tests still green.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt tests/scripts/test_dispatch_cleanup tests/scripts/test_plan_ops.py -k "claude_dispatch or build_agent or inline_implementer"`
- **Implementation notes:** The shared-helper refactor is the load-bearing piece — without it, the wrapper and Agent paths drift. The byte-stable test pins this. Do NOT remove any wrapper code; TASK-005 handles the (now-narrower) deprecation messaging.
- **Reversion guidance:** Revert the template_id registration, inline the schema-loading code back into `build_claude_dispatch_input`, revert SKILL.md:447 and the §Cleanup-around-Agent-dispatch sub-recipe, revert dispatch-templates.md Phase B prose, revert plan-implementer.md:105, drop the byte-stable test.

### TASK-003: Register `plan-analyst-per-child` Agent template and cut over Phase 1 Step 2 fan-out to in-process Agent dispatch

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` — register `plan-analyst-per-child` in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at lines 12175–12197.
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (Phase 1 Step 2 section, lines 304–316)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (Phase A-single template, lines 23–99 — verify it already targets the Agent path; if it references the wrapper's output-format flag, drop that. The classifier-result schema is already inlined in the template body — Codex Finding 5 confirms — so no shared-inliner work needed for analyst.)
  - `plugins/plan-executor/agents/plan-analyst.md` (verify the JSON-output instruction is wrapper-agnostic; revise if it references `plan_claude_dispatch.py`)
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt*.py` (add coverage for the new template_id)
- **Dependencies:** 002
- **Acceptance criteria:**
  - SKILL.md:308–316 (the Phase 1 Step 2 pseudo-syntax block) is rewritten to dispatch each child via `Agent(subagent_type:"plan-analyst", prompt:<rendered>)` rather than per-child `Bash(plan_claude_dispatch.py run --input <payload_i>)`. Parallelism is achieved by emitting N `Agent` tool calls in a single message — document this explicitly in the §Phase 1 Step 2 prose.
  - Register `plan-analyst-per-child` template_id in `_AGENT_DISPATCH_TEMPLATE_HEADING` and `_AGENT_DISPATCH_TEMPLATE_MODEL` at `plan_ops.py:12175–12197`. Model: `"sonnet"` (matches the existing analyst classifier model tier). Heading anchor: the Phase A-single template at `dispatch-templates.md:23–99`.
  - Cleanup wrap is NOT applied to `plan-analyst` — analyst is read-only by contract (it classifies; it does not edit files). Document this exception in the §Cleanup-around-Agent-dispatch sub-recipe as the single carve-out: classifier-shaped dispatches skip cleanup.
  - The Phase A-single template body in `dispatch-templates.md:23–99` is verified Agent-path-clean; if it references `output_instructions.format` or other wrapper-only knobs, those are dropped.
  - `agents/plan-analyst.md`'s JSON-output instruction is verified wrapper-agnostic; if it mentions `plan_claude_dispatch.py`, those references are removed.
  - End-to-end smoke (manual, in §Verification): run `/implement-plan` against a 3-child plan with no `**Agent:**` annotations; observe (i) Phase 1 Step 2 fan-out emits 3 in-process Agent dispatches in one message, (ii) no `claude -p` subprocess, (iii) classifier results land in plan files via the existing post-classifier persistence path.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt`
- **Implementation notes:** Parallel Agent dispatch (multiple Agent tool calls in one assistant message) has been the in-process pattern for `plan-author-*` per-finding fan-out for months — there is established precedent and no protocol risk. The carve-out for analyst (no cleanup wrap) avoids paying for snapshot/apply on a no-op path.
- **Reversion guidance:** Revert SKILL.md:308–316 to the per-child wrapper Bash block, revert any analyst template body / agent-doc edits, drop any new template_id registration.

### TASK-004: Cut over `plan-remediator` (rework / narrow-remediation / rescue) to confirm in-process Agent dispatch and reconcile SKILL prose

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (lines 97 — drop `plan-remediator` from the wrapper-recipe enumeration; lines 559, 579, 595 — verify already-Agent prose is accurate)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (Phase B-rework lines 778–836, Phase B-narrow-remediation lines 838–905, Phase D.4-rescue lines 906+ — verify each template body is Agent-path-clean)
  - `plugins/plan-executor/agents/plan-remediator.md` (revise any wrapper references)
- **Dependencies:** 002, 003
- **Acceptance criteria:**
  - SKILL.md:97 (the §Claude wrapper dispatch recipe (canonical) opening sentence) is rewritten to omit `plan-remediator` from the enumeration. After this TASK the recipe section retains only the analyst+implementer wrapper-historical context (and is itself flagged as wrapper-historical pending TASK-005 deletion).
  - SKILL.md:559, 579, 595 (the rework / narrow / rescue dispatch instructions) are verified accurate vs. the live code path: each calls `plan_ops__build_agent_dispatch_prompt` with the appropriate `template_id`, dispatches via `Agent(subagent_type:"plan-remediator", ...)`, and is wrapped by the §Cleanup-around-Agent-dispatch sub-recipe (remediator IS file-editing, unlike analyst — cleanup applies).
  - Each `plan-remediator` dispatch site (rework, narrow, rescue) is wrapped with `cleanup.snapshot_baseline()` / `cleanup.apply_cleanup(..., authorization_source="orchestrator")` against the originating task's `declared_files_changed` per the §Cleanup-around-Agent-dispatch sub-recipe.
  - Templates lines 778–905+ are Agent-path-clean (no wrapper-only knobs).
  - `agents/plan-remediator.md` has no remaining `plan_claude_dispatch.py` references.
  - End-to-end smoke (manual, in §Verification): force a Phase D `needs-rework` outcome on a one-task plan; observe rework dispatch is in-process, cleanup result is logged with orchestrator source, no `claude -p` subprocess.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt tests/scripts/test_dispatch_cleanup`
- **Implementation notes:** This TASK is mostly verification + prose reconciliation. If verification finds the rework / narrow / rescue paths are *already* Agent-dispatched and cleanup-wrapped (consistent with SKILL.md:559/579/595), the TASK reduces to deleting `plan-remediator` from SKILL.md:97 and confirming the cleanup wrap. If verification finds those paths are still wrapper-dispatched in code (i.e., :97 is right and :559/579/595 are aspirational), this TASK grows to include the full cutover analogous to TASK-002.
- **Reversion guidance:** Restore `plan-remediator` to SKILL.md:97 enumeration; revert any cleanup-wrap additions at the rework / narrow / rescue dispatch sites.

### TASK-005: Delete §Claude wrapper dispatch recipe SKILL section, banner the README, document script-runner-only residency

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (delete §Claude wrapper dispatch recipe (canonical) section entirely; cross-references already removed by TASK-002/003/004)
  - `plugins/plan-executor/scripts/README_claude_dispatch.md` (add a deprecation/scope banner at the top)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (NO behavior change to `run` — it stays functional for the script runner; optionally add a `--called-from` audit field if cheap)
  - `tests/scripts/test_claude_dispatch*.py` (NO skip — the wrapper still serves the script-runner caller; tests stay live)
- **Dependencies:** 002, 003, 004
- **Acceptance criteria:**
  - SKILL.md's §Claude wrapper dispatch recipe (canonical) section (currently around lines 97–112) is deleted in full. The orchestrator no longer references it (verified by TASK-002/003/004). Confirm with `grep -n "Claude wrapper dispatch recipe" plugins/plan-executor/skills/implement-plan/SKILL.md` returning empty.
  - `README_claude_dispatch.md` gains a top-of-file scope banner explaining (a) the `/implement-plan` SKILL/MCP orchestration NO LONGER calls this wrapper as of 2026-05-14 (in-process Agent dispatch via PLAN_DEPRECATE_CLAUDE_CLI_2026-05-14), (b) the wrapper REMAINS in active use by the standalone `implement_plan.py` script runner's `ClaudeProvider`, and (c) post-Anthropic-`claude -p` subscription deprecation, the script-runner Claude path becomes paid usage — operators are advised to switch to `--implementer codex` if cost is a concern. The banner explicitly does NOT mark the script as deprecated; only the SKILL/MCP usage is deprecated.
  - `plan_claude_dispatch.py run` continues to function unchanged. No shim, no error exit, no `claude -p` removal.
  - All existing `tests/scripts/test_claude_dispatch*.py` cases stay live and green — the wrapper subprocess path remains a supported transport for the script runner.
  - Run `/implement-plan` end-to-end against a small Claude-only plan; assert (via `pgrep -af 'claude .*-p'` during the run, captured into the run log) that no `claude -p` subprocess spawns at any phase. (This proves the SKILL/MCP cutover is complete; the script runner is exercised separately by its own existing test suite.)
  - Optional: add an audit-only `--called-from {orchestrator,script_runner}` flag to `plan_claude_dispatch.py run` and have `implement_plan.py:ClaudeProvider` pass `--called-from script_runner`. This makes future deprecation of the script-runner Claude path visible in run logs without affecting behavior. Skip if it bloats the diff.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch tests/scripts/test_plan_ops_build_agent_dispatch_prompt tests/scripts/test_dispatch_cleanup`
- **Implementation notes:** TASK-005 is now mostly documentation. The substantive cutover work lives in TASK-002/003/004. The script-runner Claude path is left intact — a follow-up plan (`PLAN_DEPRECATE_SCRIPT_RUNNER_CLAUDE_PROVIDER`) can decide whether to remove `ClaudeProvider` from `implement_plan.py`, which would then enable removing `_claude_backend.py`, `_claude_guardrails.py`, and the wrapper itself.
- **Reversion guidance:** Restore SKILL.md's §Claude wrapper dispatch recipe section from history; revert the README banner; revert any optional `--called-from` flag.

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_dispatch_cleanup tests/scripts/test_plan_ops_build_agent_dispatch_prompt tests/scripts/test_claude_dispatch` — all green; deprecated `run` end-to-end suite skipped with documented reason.
2. `venv/bin/pytest -q tests/scripts/test_plan_ops.py` — full file; guard against unintended fallout in any sibling dispatch / schedule test.
3. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json` — clean.
4. `grep -rn "claude -p" plugins/plan-executor/skills plugins/plan-executor/agents` returns empty (subprocess invocation references gone from the operator-facing docs; historical README banner is the only allowed survivor).
5. `grep -rn "plan_claude_dispatch.py run" plugins/plan-executor/skills plugins/plan-executor/agents` returns empty.

Manual smoke (required, exercises the cutover end-to-end):

1. Run `/implement-plan` against a small two-task Claude-only plan in a scratch worktree. Capture `pgrep -af 'claude .*-p'` snapshots every 2 seconds for the run's duration. Assert zero hits.
2. Confirm the run log carries `cleanup_strategy="delta_bounded"` entries with `authorization_source` indicating the orchestrator path for both implementer dispatches (the Agent path tags cleanup events with the new orchestrator source).
3. Force a Phase D `needs-rework` outcome on a third one-task scratch plan; observe the rework dispatch is in-process and cleanup-wrapped.
4. Force a Phase 1 Step 2 fan-out on a third scratch plan with 3 unannotated children; observe 3 parallel Agent dispatches in a single message and zero `claude -p` invocations.

## Known regressions (accepted)

- **No hard timeout on Agent-dispatched implementer / remediator.** The wrapper enforced `DEFAULT_DISPATCH_TIMEOUT_SEC=1800s`; the in-process Agent has no equivalent. Mitigation: operator can interrupt; the orchestrator is interactive. Gemini Finding 5 also flagged the cost-accumulation risk on a runaway subagent — accepted but worth monitoring. If a hang materializes in practice, a follow-up plan can add an Agent-side watchdog.
- **No env sanitization on Agent-dispatched subagents.** The wrapper stripped most parent env via `_claude_guardrails.py`; in-process Agent inherits the orchestrator's env wholesale. Acceptable per §Findings 2(d) — the orchestrator is already trusted, and the in-process Agent path has been the reviewer / triage / author transport for months without incident. Gemini Finding 6 flagged this as a security property being dropped; documented here for transparency.
- **Script-runner Claude path remains a paid `claude -p` invocation post-Anthropic-deprecation.** Out-of-scope for this plan — the SKILL/MCP orchestration is the primary economic exposure and is fully addressed. Operators of the standalone script runner who want to avoid `claude -p` cost should pass `--implementer codex`. A separate follow-up plan can deprecate the script-runner `ClaudeProvider` if that becomes the consensus.

## Reviewer audit trail

Plan was passed through Codex (codex-rescue subagent) and Gemini (`gemini -p`) on 2026-05-14 before finalization. Findings reconciled into the revisions above:

- **Codex Finding 1** (auth tokens are `wrapper-declared-scope` / `wrapper-empty-scope-readonly`, must update `_RESTORE_GATE_TRANSLATION`) → Decision 1, Finding 3, TASK-001 acceptance criteria.
- **Codex Finding 2** (`apply_cleanup` defaults to `pause`, returns `detect_only_revert_policy_pause`) → Finding 3a, TASK-001 test specification, Decision 2 (`unattended_revert_policy="preserve-only"`).
- **Codex Finding 3** (`_claude_backend.py:498–507` is the sole `claude -p` invocation) → Finding 1.
- **Codex Finding 4** (`build_claude_dispatch_input` still supports wrapper variant `narrow-remediation`) → Finding 4, TASK-004 (left functional for script runner).
- **Codex Finding 5** (`plan-implementer-default` and `plan-analyst-per-child` template_ids not registered) → Finding 5a, Decision 4, TASK-002 / TASK-003 acceptance criteria.
- **Codex Finding 6** (Phase B schema inlining lives in the BUILDER, not the template) → Finding 5, Decision 3, TASK-002 shared-helper extraction + byte-stable test.
- **Codex Finding 7** (plan-implementer.md:105 stale reference) → TASK-002 acceptance criteria.
- **Gemini Finding 1** (`implement_plan.py` `ClaudeProvider` is a second wrapper caller — would break under shim) → Finding 1, Decision 5, TASK-005 narrowed to docs-only (no shim).
- **Gemini Finding 2** (SKILL inconsistency confirmation) → Finding 4.
- **Gemini Finding 3** (template_id registration confirmation) → Finding 5a.
- **Gemini Finding 4** (cleanup library API verification) → Finding 3.
- **Gemini Finding 5** (timeout regression cost risk) → Known regressions.
- **Gemini Finding 6** (env sanitization loss) → Known regressions.
- **Gemini Finding 7** (avoid double-inlining schema) → Decision 3 (shared helper, schema NOT duplicated in template body).
