# TASK-027 — Codex review prompt context + adaptation-flagging tightening

**Base branch:** `main`
**Audit anchor commit:** `f60d808`
**Chunk dependencies:** none (formerly TASK-015, TASK-024 — now archived/complete).
**Issues absorbed:** none (new follow-up).
**Motivating run:** `20260421T031650` — Codex review of TASK-020B returned `needs-rework` with 3 findings; D.5 adjudicated 1 as dismissed (hallucinated `_add_git_dir(p_commit)` — symbol does not exist), 1 as spec-deference (loosely applied), 1 as a genuine minor test-coverage gap. Root-cause review exposed four prompt/spec gaps, all fixable by this chunk.

---

## Goal

Close four narrow gaps in the dispatch wrappers, agent spec, and review template that caused avoidable `needs-rework` noise and ambiguous D.5 labeling on run `20260421T031650`:

1. The Phase D-Codex review prompt parses `description` and `implementation_notes` from the plan but does NOT forward them to Codex — the reviewer sees only `title`, `acceptance_criteria`, `files`, and the diff. This starves the reviewer of exactly the context that explains "why this pattern here."
2. Nothing downstream of Codex verifies that a finding's cited symbol (a function name, an argparse flag, a helper) actually exists in the cited file. Codex confabulated `_add_git_dir(p_commit)` in Finding 0 with no structural backstop.
3. The plan-implementer agent spec scopes **Plan adaptations** narrowly to "live code differs or annotations require it." It does not require flagging cases where the plan names one mechanism (a subcommand, a CLI invocation) and the implementer used an equivalent internal mechanism (a direct helper call) that produces the same output. This hides defensible-but-real spec deviations from the reviewer.
4. The D.5 `dismissal-evidence gate` rubric distinguishes `dismissed` (concrete verification) from `spec-deference` (plan mandates the disputed behavior and implementer followed it) — but the worked-example wording permits the `spec-deference` label to drift onto cases where the implementer *deviated* from the plan citing a surrounding pattern. That class is properly `dismissed` with concrete-verification evidence, not `spec-deference`.

None of these fixes change verdict vocabulary, schema shape, or runtime routing. They tighten the context Codex sees, annotate hallucinated findings, require the implementer to surface literal-wording substitutions, and sharpen D.5's label boundary.

---

## Scoped Context

### What TASK-015 / TASK-024 already did, and why they don't cover this

- **TASK-015** (shipped) added the `clean | minor-findings | needs-rework` decision ladder with two worked examples. It calibrates *which rung a finding sits on*, not what context Codex has when deciding.
- **TASK-024** (shipped — index status `Pending` is stale; the evidence-gate wording is live at `dispatch-templates.md:445`) added the Evidence gate subsection telling Codex to cite observations before `needs-rework`. The gate is prose; Codex can skip the verification moves. TASK-027 does NOT re-tune the gate — it adds a post-Codex structural check in the wrapper that catches the specific failure mode (cited-symbol hallucination) the gate's verification-move language cannot be enforced against.

Neither task covers: (a) the review prompt's context surface, (b) post-envelope symbol verification, (c) the plan-implementer's adaptation-flagging scope, (d) the D.5 `spec-deference` boundary.

### Exact surfaces this chunk edits

| File | Change |
|---|---|
| `plugins/plan-executor/scripts/plan_codex_dispatch.py` | `render_review_prompt` forwards description + implementation_notes; `cmd_review` success path post-processes `parsed.findings[]` and attaches results to the wrapper envelope via `extra["wrapper_checks"]` |
| `plugins/plan-executor/agents/plan-implementer.md` | Broaden Step 1 adaptation trigger + Plan adaptations section wording; add one worked example |
| `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` | (027A) Add a `Wrapper checks` block to the Phase D.5 prompt so the reviewer sees `envelope.wrapper_checks` alongside `parsed.findings`. (027C) Tighten dismissal-evidence gate wording so `spec-deference` applies only when the implementer followed the plan's literal wording. The two edits are in different subsections (dispatch prompt vs dismissal-evidence gate) and do not conflict. |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | (027A) Extend the Phase D.5 `render(templates.PhaseD5, ...)` call at `SKILL.md:692` to also pass `wrapper_checks` from the Codex review envelope so the template's new `<wrapper_checks_json>` placeholder resolves at dispatch time. |
| `tests/scripts/test_codex_review_prompt.py` | Add new tests (prompt-context, symbol-check helper, dotted-exclusion, wrapper-envelope shape, D.5 template forwarding, D.5 SKILL render argument, D.5 rubric) |
| `tests/scripts/test_plan_implementer_spec.py` (new, ~40 lines) | Regression-lock the plan-implementer adaptation-flagging wording |

> **Note:** `plugins/plan-executor/scripts/codex_review_schema.json` is NOT modified. That schema constrains Codex's own output (`additionalProperties: false` at line 39). `wrapper_checks` is wrapper-envelope metadata attached by `cmd_review` via the `extra` parameter of `make_envelope`; adding it to the Codex output schema would either be ignored (if attached at wrapper top level) or misplaced inside `parsed` (wrong home; would also trip `_validate_review_failure_payload` in `plan_ops.py:960`).

### Non-goals

- **Do not change the verdict vocabulary** (`clean | minor-findings | needs-rework`) or the D.5 verdict vocabulary (`ship | ship-with-fixes | partial-agreement | needs-rework`).
- **Do not add runtime enforcement that blocks findings** from reaching the orchestrator. The wrapper annotates; routing stays verdict-driven.
- **Do not re-tune the Phase D-Codex prose evidence gate** (TASK-024's surface). The structural post-check is additive.
- **Do not expand the review prompt by including file contents beyond description + implementation_notes.** Scope creep; Codex can still Read files via its sandbox if it needs them.
- **Do not touch `render_implement_prompt`** — the implement path already forwards description + implementation_notes (line 248-267). This chunk only adjusts the review path.
- **Do not edit TASK-020B's plan text** to resolve the `plan_ops.py log-event` ambiguity. That ambiguity is a plan-authoring concern for a future plan-review pass; fixing it here would conflate with runtime-behavior surfaces.
- **Do not modify `plugins/plan-executor/scripts/codex_review_schema.json`.** `wrapper_checks` is wrapper-owned metadata, not part of Codex's output contract. Editing the schema would either have no effect (field attached at wrapper top level) or actively misplace the data inside `parsed` where downstream validators would reject it.
- **Do not add dotted-method-call detection** (`obj.method(`) to the symbol regex set in v1. Definitions commonly appear as `def method(` or `Class.method` rather than `obj.method(`; grepping the literal call-site expression produces noisy false-negatives. Defer to a follow-up if the hallucination pattern surfaces.
- **Do not persist `wrapper_checks` into run-log `review_done` events** via `--fields-json` in v1. The annotation lives in the envelope artifact (on disk); the audit trail is already durable. Adding run-log payload is a deferrable follow-up if D.5 or cross-task aggregation needs it.

### Design constraints

- All changes are additive to existing contracts. Orchestrators and implementers that do not read the new `wrapper_checks` field or new adaptation wording continue to work unchanged.
- Hallucinated-symbol detection must be **best-effort and non-blocking** — if a candidate symbol is extracted but cannot be verified (file missing, path resolves outside `repo_root`, read error), annotate `status: "unchecked"` rather than aborting. If NO candidate symbol is extractable from the finding text (prose-only findings like "the cleanup branch is wrong"), emit NO entry for that finding — not an `unchecked` entry. This keeps the annotation list signal-rich and prevents D.5 from reading an `unchecked` warning as a false positive on Codex.
- Symbol-regex set (the only three patterns in scope for v1). The negative-lookbehind character class **must include `.`** so that dotted expressions (`obj.method(`, `obj._helper(`) do NOT match — the v1 scope is bare symbols only:
  - `r"(?<![A-Za-z0-9_.])(_[A-Za-z][A-Za-z0-9_]*)\("` — underscore-prefixed identifiers followed by `(`. Matches `_add_git_dir(`, `_append_run_log(`. Does NOT match `obj._helper(` (the `.` in the lookbehind blocks it).
  - `r"(?<![A-Za-z0-9_.])(--[a-z][a-z0-9-]+)(?=\b)"` — long CLI flags. Matches `--git-dir`, `--v-check-timeout`.
  - `r"(?<![A-Za-z0-9_.])([a-z][a-z0-9_]{3,})\([a-zA-Z_]"` — snake_case function calls with at least one arg identifier. Matches bare `function_name(arg`; does NOT match `obj.method(arg` because `.` in the lookbehind blocks the preceding-dot case.
- `wrapper_checks` lives at the top of the wrapper envelope (sibling of `parsed`, `outcome`, `error`), attached via the existing `extra` parameter of `make_envelope`. It does NOT belong inside `parsed` — that is Codex's output contract and is governed by `codex_review_schema.json`.
- Test assertions on prompt/template text use substring or regex matches, not exact-string equality, so incidental rewording in future passes does not break these tests.

---

## Verification

**V1 — Review prompt forwards description + implementation_notes.**

`render_review_prompt(task, diff, review_focus)` MUST include both the plan's `Description:` paragraph and its `Implementation notes:` paragraph in the rendered prompt text, each under a clearly labeled heading. Empty / absent in the source plan → the heading is emitted with the literal fallback `(none provided)`. Regression test: `tests/scripts/test_codex_review_prompt.py::test_review_prompt_forwards_description_and_impl_notes` asserts both headings appear and a sentinel string from each field is present when supplied.

**V2 — Wrapper post-check annotates hallucinated symbol citations via wrapper envelope metadata.**

`cmd_review` MUST invoke `_verify_cited_symbols(parsed: dict, repo_root: str) -> list[dict]` after the Codex envelope is parsed. The helper iterates `parsed["findings"]`; for each finding, it extracts candidate symbol citations from the `issue` text using the three regex patterns listed in Design constraints and greps the finding's cited `file` for each candidate. The result list is attached to the wrapper envelope at top-level via `extra["wrapper_checks"] = {"symbol_warnings": <result>}`. The raw `parsed` object (Codex's output) is NOT mutated.

Entry shape: `{finding_index: int, cited_symbol: str, file: str, status: "not-found" | "unchecked"}`.

Status semantics:
- `"not-found"` — a candidate symbol was extracted and is absent from the cited file (the hallucination case).
- `"unchecked"` — a candidate symbol was extracted BUT the verification could not run (file missing on disk, cited path resolves outside `repo_root`, read error).
- **No candidate extractable → NO entry is emitted.** Abstract prose findings ("the cleanup branch is wrong") produce zero entries. This keeps D.5 from mistaking prose-only findings for unverifiable claims.

`wrapper_checks` is attached **only on the success path** of `cmd_review` (the path where `parsed` is extracted and `make_envelope` is called with `extra={...}`). On timeout/failure/parse-error paths where `parsed` is absent, the field is OMITTED from the envelope; D.5 dispatch treats absence by substituting `{"symbol_warnings": []}` in the prompt. This keeps `cmd_review` changes minimal and avoids enumerating every early-return branch.

Regression tests in `tests/scripts/test_codex_review_prompt.py`:
- `test_symbol_verification_flags_missing_citation` — construct `parsed` with a finding citing `_does_not_exist(foo)` against a real repo file; assert one entry with `status: "not-found"`.
- `test_symbol_verification_passes_real_citation` — construct `parsed` citing `_append_run_log(` against `plugins/plan-executor/scripts/plan_ops.py`; assert empty list.
- `test_symbol_verification_no_candidate_produces_no_entry` — construct `parsed` with prose-only finding text; assert empty list (NOT an `unchecked` entry).
- `test_symbol_verification_unchecked_when_file_missing` — construct `parsed` citing `_some_symbol(` against a nonexistent path (e.g., `tmp/nonexistent.py`); assert one entry with `status: "unchecked"`.
- `test_symbol_verification_skips_dotted_identifiers` — construct `parsed` whose `issue` text contains ONLY dotted expressions (`obj.method(arg)`, `obj._helper(arg)`, `self.foo(bar)`) and whose cited file does NOT contain those literals; assert empty list. Locks in the F4 out-of-scope boundary (the regex set's `.` in negative lookbehinds).
- `test_review_envelope_includes_wrapper_checks` — drive the success-path envelope construction (using `make_envelope` with the computed `extra`) and assert `envelope["wrapper_checks"]["symbol_warnings"]` is present as a list.

**V3 — D.5 dispatch template forwards `wrapper_checks` alongside `parsed.findings`.**

`plugins/plan-executor/skills/implement-plan/dispatch-templates.md` Phase D.5 section MUST include a new `Wrapper checks (verbatim from envelope.wrapper_checks):` JSON block positioned immediately after the existing `Codex findings (verbatim from parsed.findings of the wrapper envelope):` block (currently at `dispatch-templates.md:445`). The orchestrator populates the placeholder from the envelope at dispatch time; when `wrapper_checks.symbol_warnings` is empty, the block renders `{"symbol_warnings": []}` so the reviewer sees "checked, clean" rather than "not checked."

`codex_review_schema.json` is NOT modified. `wrapper_checks` is wrapper-envelope metadata only.

Regression tests:
- `test_fixture_matches_review_schema` (existing) continues to pass unchanged — the schema is untouched.
- `test_d5_dispatch_template_forwards_wrapper_checks` — extract the Phase D.5 section (helper `_phase_d5_section()`) and assert the new `Wrapper checks` block appears between the Codex findings block and the verdict decision rubric.

**V4 — Plan-implementer spec surfaces literal-wording substitutions.**

`plugins/plan-executor/agents/plan-implementer.md` Step 1 paragraph and the **Plan adaptations:** report-format paragraph both explicitly require flagging the class of substitution where the plan names a specific mechanism (a subcommand, a CLI invocation, a named helper) and the implementer used an equivalent mechanism that produces the same output. The spec MUST include at least one worked example of this substitution class.

Regression tests in `tests/scripts/test_plan_implementer_spec.py`:
- `test_literal_wording_substitution_trigger_present` — asserts the Step 1 paragraph contains the broadened trigger wording (substring match on "specific mechanism" and the Plan-adaptations trailing sentence on "literal-wording substitutions").
- `test_plan_adaptations_worked_example_present` — asserts the worked-example paragraph appears in Step 1 (substring match on the worked-example phrasing).

Step 7 elaborates a third test (`test_parse_implementer_report_contract_preserved`) that confirms the existing `parse-implementer-report` CLI contract is not regressed by the spec edits.

**V5 — D.5 `spec-deference` rubric disambiguated.**

`plugins/plan-executor/skills/implement-plan/dispatch-templates.md` Phase D.5 dismissal-evidence gate MUST state that `spec-deference` applies **only** when the implementer followed the plan's literal wording AND Codex is disputing what the plan mandated. If the implementer deviated from the plan's literal wording (even for defensible reasons — surrounding-code pattern, factually-cleaner approach), the disposition is `dismissed` (with concrete-verification evidence) or surfaced at `minor-findings`, NOT `spec-deference`.

Regression test: `tests/scripts/test_codex_review_prompt.py::test_d5_spec_deference_rubric_scoped` asserts the dismissal-evidence gate section contains the "implementer followed the plan" precondition wording.

**V6 — Test suite green; no regressions to existing tests.**

`venv/bin/pytest -q tests/scripts/test_codex_review_prompt.py tests/scripts/test_plan_implementer_spec.py` exits 0. The broader suite `venv/bin/pytest -q tests/scripts/test_plan_ops.py` exits 0 or with the known pre-existing `test_analyst_to_parse_schedule_roundtrip` failure only (external CLI dispatch; unrelated to this chunk).

---

## Tasks

### TASK-027A: Review prompt context + symbol-verification post-check + D.5 forwarding

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (D.5 dispatch prompt — adds `Wrapper checks` block; distinct from TASK-027C's dismissal-evidence gate edit in the same file)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (Phase D.5 render call at `SKILL.md:692` — must pass `wrapper_checks` from the Codex review envelope into the template substitution alongside `codex_findings` and `task_block`)
  - `tests/scripts/test_codex_review_prompt.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_codex_review_prompt.py`
- **Acceptance criteria:**
  - V1, V2, V3 pass.
  - `render_review_prompt` at `plan_codex_dispatch.py:404` is extended to emit two new prompt sections, inserted between the `Task requirements:` bullets and the `Changed files:` line: `Description:\n<description or "(none provided)">` and `Implementation notes:\n<implementation_notes or "(none provided)">`. Both fields are already parsed into the `task` dict by `parse_task_block` (`plan_codex_dispatch.py:221` extracts `implementation_notes`; `description` is already on the task dict); this task wires them through. (Line numbers verified at audit anchor commit; verify with `grep -n` before editing if HEAD has drifted.)
  - A module-level helper `_verify_cited_symbols(parsed: dict, repo_root: str) -> list[dict]` is added to `plan_codex_dispatch.py` near the other helpers (e.g., after `git_diff_for_files` at `plan_codex_dispatch.py:526`). It iterates `parsed.get("findings") or []` (the raw Codex output, NOT the wrapper envelope), extracts candidate symbol citations from each finding's `issue` text using the three regex patterns in Design constraints, and greps the finding's cited `file` for each candidate. Returns a list of `{finding_index, cited_symbol, file, status}` entries. (Line number verified at HEAD; verify with `grep -n '^def git_diff_for_files'` before editing if HEAD has drifted.)
  - Helper enforces these four status semantics: (a) `"not-found"` — a candidate was extracted and is absent from the cited file; (b) `"unchecked"` — a candidate was extracted BUT verification could not run (file missing, cited path escapes `repo_root`, read error); (c) no candidate extractable → no entry emitted (abstract/prose findings produce zero entries); (d) dotted expressions (`obj.method(`, `obj._helper(`) produce no entry — excluded by the `.` in each regex's negative lookbehind, dotted calls are explicitly out of v1 scope.
  - `cmd_review` at `plan_codex_dispatch.py:1236` calls `_verify_cited_symbols(parsed, repo_root)` after `parsed` is extracted and attaches the result to the wrapper envelope via the existing `extra` dict (success-path `extra` construction begins around `plan_codex_dispatch.py:1311` and is passed via `extra=extra` to `make_envelope` at `plan_codex_dispatch.py:1388`): `extra["wrapper_checks"] = {"symbol_warnings": warnings}`. This is attached **only on the success path**; timeout/failure/parse-error branches omit the field and D.5 dispatch treats absence by substituting `{"symbol_warnings": []}`. No enumeration of every early-return branch is required. (Line numbers verified at HEAD; verify with `grep -n` before editing.)
  - The raw `parsed` object is NOT mutated. Finding text, severity, line number, suggested_fix are preserved verbatim.
  - `plugins/plan-executor/scripts/codex_review_schema.json` is NOT modified. `wrapper_checks` is wrapper-envelope metadata only and must not appear inside `parsed` (where `_validate_review_failure_payload` at `plan_ops.py:960` would reject it).
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` Phase D.5 section gains a new `Wrapper checks (verbatim from envelope.wrapper_checks):` JSON block positioned immediately after the existing `Codex findings (verbatim from parsed.findings of the wrapper envelope):` block (currently at `dispatch-templates.md:445`). The orchestrator substitutes `<wrapper_checks_json>` from the envelope at dispatch time.
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` Phase D.5 render call at `SKILL.md:692` (currently `render(templates.PhaseD5, codex_findings, task_block)`) is extended to also pass `wrapper_checks` from the Codex review envelope: `render(templates.PhaseD5, codex_findings, task_block, wrapper_checks)`. When the envelope lacks `wrapper_checks`, pass `{"symbol_warnings": []}` as the default. The precise signature/argument naming follows the existing `render()` call convention in the skill; the substantive requirement is that `<wrapper_checks_json>` placeholder in the template resolves from envelope data at dispatch time, not from static template text. (Line number verified at HEAD; verify with `grep -n 'templates.PhaseD5'` before editing.)
  - Adds these eight new regression tests to `tests/scripts/test_codex_review_prompt.py`, each described by name and assertion: (1) `test_review_prompt_forwards_description_and_impl_notes` — calls `render_review_prompt` with a synthetic `task` dict carrying sentinel strings in `description` and `implementation_notes`, asserts both `Description:` and `Implementation notes:` headings and their sentinels appear in the rendered prompt; (2) `test_symbol_verification_flags_missing_citation` — imports `_verify_cited_symbols`, builds `parsed` with a finding citing `_definitely_not_real_xyz(` against `plugins/plan-executor/scripts/plan_codex_dispatch.py`, asserts one entry with `status: "not-found"` and `cited_symbol: "_definitely_not_real_xyz"`; (3) `test_symbol_verification_passes_real_citation` — builds `parsed` citing `_append_run_log(` against `plugins/plan-executor/scripts/plan_ops.py`, asserts empty list; (4) `test_symbol_verification_no_candidate_produces_no_entry` — builds `parsed` with prose-only `issue` text (e.g., "the cleanup control-flow path is unreachable"), asserts empty list (NOT an `unchecked` entry); (5) `test_symbol_verification_unchecked_when_file_missing` — builds `parsed` citing `_some_symbol(` against `tmp/does_not_exist.py`, asserts one entry with `status: "unchecked"`; (6) `test_symbol_verification_skips_dotted_identifiers` — builds `parsed` whose `issue` text contains ONLY dotted expressions (`self.foo(bar)`, `obj.method(arg)`, `obj._helper(arg)`) against a real file that does NOT contain those literals, asserts empty list (locks F4 out-of-scope boundary); (7) `test_review_envelope_includes_wrapper_checks` — drives `make_envelope` with the computed `extra` dict, asserts the resulting envelope contains `envelope["wrapper_checks"]["symbol_warnings"]` as a list at top-level; (8) `test_d5_dispatch_template_forwards_wrapper_checks` — extracts the Phase D.5 section via new helper `_phase_d5_section()`, asserts the `Wrapper checks` JSON block appears between the Codex findings block and the verdict decision rubric, AND asserts `SKILL.md` Phase D.5 render call text (substring: `render(templates.PhaseD5`) includes `wrapper_checks` in its argument list (closes plumbing gap between template and skill).
- **Out of scope:**
  - Mutating `parsed.findings[]` based on the symbol check. Annotation only.
  - Rewriting the finding issue text to flag hallucination inline. Envelope annotations are enough; orchestrator surfaces them to D.5 via the new `Wrapper checks` block.
  - Modifying `codex_review_schema.json`. `wrapper_checks` does NOT belong inside `parsed`; it is wrapper-envelope metadata.
  - Running Codex to re-dispatch on hallucination. A one-shot annotation is v1; a re-prompt retry can be a follow-up.
  - Extending the symbol regex to parse camelCase, dotted method calls (`obj.method(`), or Class references. The three regex rules in Design constraints cover the recurring failure modes without false-positive prose matches.
  - Persisting `wrapper_checks` into run-log `review_done` events via `--fields-json`. The envelope artifact is durable; run-log bloat is deferred.

**Description:**
The Phase D-Codex review prompt gives Codex the diff plus acceptance bullets — not the plan's Description or Implementation notes, even though both are already parsed. Forward them. Additionally, in run `20260421T031650` Codex confabulated `_add_git_dir(p_commit)` — a helper that does not exist — and the wrapper had no structural check to catch it. Add a post-parse grep pass that flags cited symbols missing from their cited file, attach the result as wrapper-envelope metadata via `extra`, and extend the D.5 dispatch template so the third-opinion reviewer sees the annotations alongside the Codex findings. Annotate, do not block.

- **Implementation notes:**
  - Extract the post-check into a module-level helper `_verify_cited_symbols(parsed: dict, repo_root: str) -> list[dict]` so tests can call it directly without spinning up Codex. The helper consumes the raw Codex `parsed` object (the dict returned from JSON-parsing Codex's output), NOT the wrapper envelope.
  - Place the new prompt sections AFTER `Task requirements:` and BEFORE `Changed files:` so the reviewer sees "what the task is" → "why/how the task is" → "what changed."
  - In `cmd_review`, compute `warnings = _verify_cited_symbols(parsed, repo_root)` after `parsed` is extracted, then attach to the existing `extra` dict that is passed to `make_envelope` (see `plan_codex_dispatch.py:1311 (success-path extra construction; passed via extra=extra to make_envelope at 1388)` for the existing `extra` construction pattern). `make_envelope` merges `extra` into the top-level envelope alongside `parsed` via `envelope.update(extra)` at `plan_codex_dispatch.py:952`. Do NOT place `wrapper_checks` inside `parsed`.
  - Success path only: attach `extra["wrapper_checks"]` in the success-path `extra` dict (the one currently being built around `plan_codex_dispatch.py:1270-1279`). Timeout/failure/parse-error branches are NOT modified; D.5 dispatch treats missing `wrapper_checks` as "check not run" and substitutes `{"symbol_warnings": []}` in the prompt.
  - The D.5 template edit is the smaller of two distinct edits to `dispatch-templates.md` in this chunk (TASK-027C makes the other). The 027A edit targets the Phase D.5 dispatch prompt's findings-rendering block (around line 230); the 027C edit targets the dismissal-evidence gate (around line 245). Sequence the two edits so they do not collide on the same lines — easiest by applying 027A first (insertion after line 234) then 027C (insertion inside the gate subsection around line 248).
  - `SKILL.md:692` currently reads `render(templates.PhaseD5, codex_findings, task_block)`. Extend it to `render(templates.PhaseD5, codex_findings, task_block, wrapper_checks)` so the `<wrapper_checks_json>` placeholder the template now references actually receives a value at dispatch time. If the envelope's `wrapper_checks` key is absent (failure/timeout/parse-error path), pass `{"symbol_warnings": []}` as the default. Without this edit, the template-only change is plumbing that dead-ends. (Line number verified at HEAD; verify with `grep -n 'templates.PhaseD5'` before editing.)
- **Reversion guidance:**
  Safe to revert. Removing the new prompt sections restores the pre-027A `render_review_prompt`. Removing the post-check helper and the `extra["wrapper_checks"]` assignment in `cmd_review` restores the envelope shape; existing envelopes that lack the field remain valid because no schema change was made. Removing the `Wrapper checks` block from `dispatch-templates.md` and reverting `SKILL.md:692` to the two-arg `render(templates.PhaseD5, codex_findings, task_block)` form restores the pre-027A D.5 dispatch surface.

### TASK-027B: Plan-implementer adaptation-flagging scope

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/agents/plan-implementer.md`
  - `tests/scripts/test_plan_implementer_spec.py` (new)
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_implementer_spec.py`
- **Acceptance criteria:**
  - V4 passes.
  - `agents/plan-implementer.md` Step 1 paragraph (currently at line 36-38) is extended to explicitly state: "If the plan names a specific mechanism (a subcommand, a CLI invocation, a helper function, a flag) and you use a different mechanism that produces the same output, flag it under Plan adaptations with a one-line justification — even when the emitted behavior is identical." The new sentence sits alongside the existing "live code differs" trigger; the existing trigger is not removed or weakened.
  - The **Plan adaptations:** section of the report-format block (currently at line 98-99) gains a trailing sentence: "This includes literal-wording substitutions where you used an equivalent mechanism (e.g., plan says `foo.py subcommand` but you called an internal helper that produces the same output); such cases MUST be flagged even when the emitted behavior is identical."
  - A short worked example is added to Step 1 immediately after the new trigger sentence: a synthetic case describing the `plan_ops.py log-event` vs `_append_run_log` class of substitution (using generic names, not verbatim TASK-020B). Format: one paragraph, ≤3 sentences.
  - Creates new test file `tests/scripts/test_plan_implementer_spec.py` containing exactly these three tests, each described by name and assertion: (1) `test_literal_wording_substitution_trigger_present` — reads `plugins/plan-executor/agents/plan-implementer.md`, asserts the Step 1 paragraph contains the new trigger wording (substring match on "specific mechanism" AND "flag it under") AND the Plan-adaptations report-format paragraph contains the substitution wording (substring match on "literal-wording substitutions" OR "mechanism A"/"mechanism B"-style phrasing); (2) `test_plan_adaptations_worked_example_present` — asserts the worked example paragraph appears in Step 1 (substring match on "internal helper" AND "identical"); (3) `test_parse_implementer_report_contract_preserved` — fixture-based smoke test that constructs a synthetic implementer-report string with `**Plan adaptations:**\n- None` and feeds it through `plan_ops.py parse-implementer-report --stdin` (or `--report-path <tmp>`), asserts exit 0 and the parsed `plan_adaptations` field equals `["None"]` or `[]` per the existing contract (lock against accidental contract breakage from the spec edits).
- **Out of scope:**
  - Runtime enforcement that parses implementer reports and rejects those missing adaptation flags for detected literal-wording substitutions. Prompt-level guidance is the cheapest first intervention; automated detection is a follow-up if this doesn't take.
  - Changing the `parse-implementer-report` CLI contract or the `missing-plan-adaptations` diagnostic — existing shape is preserved.
  - Editing other agent specs (plan-analyst, plan-author, plan-remediator). Out of scope; if the same pattern emerges there, file a follow-up.

**Description:**
The plan-implementer spec only triggers adaptation-flagging for "live code differs" cases. Run `20260421T031650` showed a legitimate spec deviation (CLI subcommand substituted with internal helper) go unflagged because the emitted output was identical to the plan's spec. Broaden the trigger to require flagging literal-wording substitutions.

- **Implementation notes:**
  - Keep the Step 1 edit additive — append the new trigger sentence rather than replacing the existing "live code differs" language.
  - The worked example should be generic (not name `plan_ops.py` or `_append_run_log`) so it reads as guidance rather than a post-hoc justification for TASK-020B.
  - The new test file should live at `tests/scripts/test_plan_implementer_spec.py` alongside the existing `test_codex_review_prompt.py` — same testing pattern (substring assertions on the spec text).
- **Reversion guidance:**
  Safe to revert. Removing the new sentences restores the pre-027B spec. Removing the test file clears the regression lock. Existing parse-implementer-report behavior is not touched.

### TASK-027C: D.5 `spec-deference` rubric disambiguation

- **Status:** pending
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `tests/scripts/test_codex_review_prompt.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_codex_review_prompt.py`
- **Acceptance criteria:**
  - V5 passes.
  - `dispatch-templates.md` Phase D.5 dismissal-evidence gate section (currently at line 460) gains a precondition clause explicitly stating: `spec-deference` applies **only** when the implementer's implementation followed the plan's literal wording AND Codex is disputing what the plan mandated. If the implementer deviated from the plan's literal wording (even defensibly — surrounding-code pattern match, internal-helper substitution, semantically-equivalent alternative), the disposition is `dismissed` (with concrete-verification evidence showing the deviation is correct) OR surfaced at `minor-findings` — NOT `spec-deference`.
  - A short worked example is added immediately after the clause: a synthetic case where the plan names mechanism A, implementer used mechanism B that produces identical output, Codex flagged the deviation → disposition is `dismissed` with verification (pointing to the surrounding-pattern evidence), NOT `spec-deference`. Contrast with: plan mandates behavior X, implementer implemented X, Codex objects to X → disposition IS `spec-deference`.
  - Adds new regression test `test_d5_spec_deference_rubric_scoped` to `tests/scripts/test_codex_review_prompt.py` with these specific assertions: (1) loads the `dispatch-templates.md` Phase D.5 section via new helper `_phase_d5_section()` (modeled on existing `_phase_d_codex_section`-style helper; extracts `## Phase D.5` through the next `## ` heading); (2) asserts the dismissal-evidence gate subsection contains the precondition wording — substring match requiring all three terms `implementer`, `followed`, and `literal wording` (or equivalent phrasing — `literal-wording` hyphenated also accepted); (3) asserts the contrasting worked example paragraph contains BOTH the strings `dismissed` AND `spec-deference` within 200 characters of each other (locks the contrasting-example structure, not just keyword presence).
- **Out of scope:**
  - Re-tuning the dismissal-evidence gate's "concrete verification move" list. The existing moves (trace control flow, run pytest, read cited symbol) are correct and unchanged.
  - Changing the D.5 verdict vocabulary (`ship | ship-with-fixes | partial-agreement | needs-rework`). Unchanged.
  - Editing other Phase D.5 subsections (verdict decision rubric, hard rules for partial-agreement, output shape). Only the dismissal-evidence gate is touched.

**Description:**
The D.5 rubric defines `spec-deference` as "plan mandates X, Codex disputes X," but the worked-example wording permits the label to drift onto cases where the implementer deviated from the plan's literal wording citing a surrounding pattern. Disambiguate the boundary: `spec-deference` = implementer followed plan; `dismissed` = implementer deviated for a verified-correct reason.

- **Implementation notes:**
  - The clause goes INSIDE the existing dismissal-evidence gate section, between the numbered list (items 1 and 2) and the "Plan says so without a spec-deference label" paragraph. Do not split the section into a new heading.
  - The worked example should be terse (≤80 words) and explicit about the two contrasting cases.
  - Test helper `_phase_d5_section()` mirrors the existing `_phase_d_codex_section()` pattern — scan for `## Phase D.5` header and the next `## ` heading.
- **Reversion guidance:**
  Safe to revert. Removing the new clause and example restores the prior rubric. Orchestrator routing is unchanged; the clarification is prompt-level guidance only.

---

## Out of Scope

- Editing TASK-020B's plan text to resolve the `plan_ops.py log-event` vs `_append_run_log` wording ambiguity. A retroactive edit to a shipped plan is not this chunk's job. A future plan-review pass can flag the plan-authoring convention.
- Updating `00_INDEX.json` TASK-020B status from `Pending` to `Done` (it shipped as `bc9ea17` + `489995d`). Separate housekeeping concern; do not bundle with this chunk.
- Adding a runtime linter that parses Codex envelopes and demotes verdicts based on hallucinated-symbol count. Annotation is enough for v1; automated demotion is a TASK-028-class follow-up if this chunk's annotation doesn't meaningfully improve D.5 speed.
- Tuning the Phase D-Claude (code-reviewer) prompt. TASK-020B did not expose a Claude-reviewer failure; do not pre-emptively edit a working surface.
- Adding wrapper-side context (surrounding code snippets, git blame, previous commit messages) to the review prompt. Codex has Read access to the repo inside its sandbox; inflating the prompt with pre-fetched context has diminishing returns and hits token budgets. Description + implementation_notes are the minimum-useful addition.

---

## Reversion guidance

- TASK-027A is additive: revert restores the diff-only review prompt, removes the wrapper-envelope `wrapper_checks` field, and removes the `Wrapper checks` block from the D.5 dispatch template. No schema change was made, so existing envelopes remain valid post-revert without additional work.
- TASK-027B is additive: revert restores the narrower adaptation trigger. The new test file and its imports are isolated to one file.
- TASK-027C is additive: revert restores the prior dismissal-evidence gate wording. D.5 continues to emit the same verdict vocabulary.
- None of the three tasks change runtime routing, verdict vocabulary, or the Codex output schema that downstream validators depend on. Revert is always safe; the cost is re-exposure to the specific failure modes observed in run `20260421T031650`.

---

## Implementation Playbook

### Step 1 (TASK-027A) — Forward description + implementation_notes

Edit `render_review_prompt` at `plan_codex_dispatch.py:404`:

```python
def render_review_prompt(task: dict, diff: str, review_focus: str) -> str:
    allowed = [normalize_file_path(f) for f in task["files"]]
    files_str = ", ".join(allowed) or "(none declared)"
    ac_bullets = "\n".join(f"- {c}" for c in task["acceptance_criteria"]) or "- (none specified)"
    description = task.get("description") or "(none provided)"
    impl_notes = task.get("implementation_notes") or "(none provided)"
    return (
        f"Review the implementation of TASK-{task['task_id']} in this repository.\n\n"
        f"Task objective: {task['title']}\n\n"
        f"Task requirements:\n{ac_bullets}\n\n"
        f"Description:\n{description}\n\n"
        f"Implementation notes:\n{impl_notes}\n\n"
        f"Changed files: {files_str}\n\n"
        f"Review focus: {review_focus}\n\n"
        # ... rest unchanged
    )
```

### Step 2 (TASK-027A) — Add `_verify_cited_symbols` helper

New module-level function near the other helpers (e.g. after `git_diff_for_files` at line 426). The helper consumes the raw Codex `parsed` dict, NOT the wrapper envelope:

```python
_SYMBOL_PATTERNS = [
    # Negative-lookbehind character class includes `.` so dotted expressions
    # like obj._helper( and obj.method( do NOT match — v1 scope is bare
    # symbols only; dotted identifiers are explicitly out of scope.
    re.compile(r"(?<![A-Za-z0-9_.])(_[A-Za-z][A-Za-z0-9_]*)\("),
    re.compile(r"(?<![A-Za-z0-9_.])(--[a-z][a-z0-9-]+)(?=\b)"),
    re.compile(r"(?<![A-Za-z0-9_.])([a-z][a-z0-9_]{3,})\([a-zA-Z_]"),
]

def _verify_cited_symbols(parsed: dict, repo_root: str) -> list[dict]:
    """Best-effort grep of each finding's cited symbols against its cited file.

    Consumes the raw Codex `parsed` object (not the wrapper envelope).
    Returns a list of {finding_index, cited_symbol, file, status} entries.

    Status semantics:
      "not-found"  — candidate extracted, absent from the cited file.
      "unchecked"  — candidate extracted, but file missing, path escapes
                     repo_root, or read failed.
      No candidate extractable → NO entry emitted for that finding.
    """
    warnings: list[dict] = []
    findings = (parsed or {}).get("findings") or []
    repo_root_resolved = Path(repo_root).resolve()
    for idx, finding in enumerate(findings):
        issue_text = finding.get("issue") or ""
        cited_file = finding.get("file") or ""
        candidates: set[str] = set()
        for pat in _SYMBOL_PATTERNS:
            for m in pat.finditer(issue_text):
                candidates.add(m.group(1))
        if not candidates:
            continue  # prose-only finding → no entry

        if not cited_file:
            for sym in candidates:
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": "", "status": "unchecked",
                })
            continue

        abs_path = (repo_root_resolved / cited_file).resolve()
        try:
            abs_path.relative_to(repo_root_resolved)
        except ValueError:
            for sym in candidates:
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "unchecked",
                })
            continue

        if not abs_path.is_file():
            for sym in candidates:
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "unchecked",
                })
            continue

        try:
            file_text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            for sym in candidates:
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "unchecked",
                })
            continue

        for sym in candidates:
            if sym not in file_text:
                warnings.append({
                    "finding_index": idx, "cited_symbol": sym,
                    "file": cited_file, "status": "not-found",
                })
    return warnings
```

### Step 3 (TASK-027A) — Wire post-check into `cmd_review` via `extra` (success path only)

`wrapper_checks` is wrapper-envelope metadata — attach via the existing `extra` dict that `cmd_review` already builds before the success-path `make_envelope` call (see `plan_codex_dispatch.py:1311 (success-path extra construction; passed via extra=extra to make_envelope at 1388)`). After `parsed` is populated and before the `make_envelope(..., extra=extra)` call, add:

```python
extra["wrapper_checks"] = {
    "symbol_warnings": _verify_cited_symbols(parsed, repo_root),
}
```

**Only the success path is modified.** Timeout, failure, and parse-error branches are NOT touched — they emit envelopes without a `wrapper_checks` field, and D.5 dispatch treats absence as "check not run" by substituting `{"symbol_warnings": []}` at render time (handled in Step 4.5). This keeps the `cmd_review` change to a single insertion and avoids enumerating every early-return site.

Do NOT mutate `envelope["wrapper_checks"]` directly. Do NOT place the field inside `parsed`. `make_envelope` merges `extra` into the top-level envelope via `envelope.update(extra)` at `plan_codex_dispatch.py:952`, so `extra["wrapper_checks"]` surfaces as `envelope["wrapper_checks"]` automatically.

### Step 4 (TASK-027A) — D.5 dispatch template forwarding

Edit `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` Phase D.5 section. Immediately AFTER the existing block (currently at `dispatch-templates.md:445`):

```
> Codex findings (verbatim from `parsed.findings` of the wrapper envelope):
>
> ```json
> <codex_findings_json>
> ```
```

insert a new block:

```
>
> Wrapper checks (verbatim from `envelope.wrapper_checks`):
>
> ```json
> <wrapper_checks_json>
> ```
>
> The `symbol_warnings` list flags cases where Codex cited a specific function, helper, or flag (e.g., `_some_helper(`, `--some-flag`) that does NOT exist in the cited file — a hallucination signal. An empty list means no hallucinated-symbol warnings are surfaced (either the wrapper ran the check and found none, or this envelope came from a failure/timeout/parse-error path where the check did not run and the orchestrator substituted the empty-list default). A non-empty list is what carries information; an empty list tells you nothing either way. Use a `not-found` warning as a **tiebreaker**, not a decision rule: it is a strong dismissal signal for that individual finding but still requires the dismissal-evidence gate below.
```

`<wrapper_checks_json>` is a placeholder the orchestrator substitutes at dispatch time from `envelope["wrapper_checks"]` (or `{"symbol_warnings": []}` if the envelope lacks the key — see Step 4.5). `codex_review_schema.json` is NOT touched.

**Note on sequencing with TASK-027C:** TASK-027C also edits `dispatch-templates.md`, targeting the dismissal-evidence gate subsection at `dispatch-templates.md:460`. The 027A insertion above is at line ~445 and does not overlap. Apply 027A first, then 027C; line numbers shift by ~12-15 lines after 027A, which 027C's search patterns must accommodate (use substring match on `Dismissal-evidence gate`, not line numbers).

### Step 4.5 (TASK-027A) — SKILL.md render-call plumbing

Edit `plugins/plan-executor/skills/implement-plan/SKILL.md` line 692. Current text:

```
2. Dispatch the Phase D.5 template: `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD5, codex_findings, task_block))`.
```

Change to:

```
2. Dispatch the Phase D.5 template: `Agent(subagent_type: "code-reviewer", model: "sonnet", prompt: render(templates.PhaseD5, codex_findings, task_block, wrapper_checks))`. `wrapper_checks` is taken from the Codex review envelope's `wrapper_checks` field; if the field is absent (failure/timeout/parse-error envelopes), pass `{"symbol_warnings": []}` as the default so the template's `<wrapper_checks_json>` placeholder always resolves to a valid JSON object.
```

This is the plumbing that connects the new template placeholder (Step 4) to the envelope data (Step 3). Without Step 4.5, the template would dead-end on an unresolved placeholder and the wrapper-side hallucination check would be invisible to D.5 despite all the code being in place.

### Step 5 (TASK-027A) — Tests

Add to `tests/scripts/test_codex_review_prompt.py`. Eight new test functions:

- `test_review_prompt_forwards_description_and_impl_notes` — call `plan_codex_dispatch.render_review_prompt` with a synthetic `task` dict carrying unique sentinel strings in `description` and `implementation_notes`; assert both sentinels appear in the rendered prompt under `Description:` / `Implementation notes:` headings.
- `test_symbol_verification_flags_missing_citation` — import `_verify_cited_symbols` from `plan_codex_dispatch`; build `parsed = {"findings": [{"file": "plugins/plan-executor/scripts/plan_codex_dispatch.py", "issue": "This calls _definitely_not_real_xyz(foo) internally...", ...}]}`; assert returned list has one entry with `status: "not-found"` and `cited_symbol: "_definitely_not_real_xyz"`.
- `test_symbol_verification_passes_real_citation` — build `parsed` citing `_append_run_log(` against `plugins/plan-executor/scripts/plan_ops.py`; assert empty list.
- `test_symbol_verification_no_candidate_produces_no_entry` — build `parsed` with prose-only `issue` text ("the cleanup control-flow path is unreachable in this branch"); assert empty list — specifically NOT an `unchecked` entry.
- `test_symbol_verification_unchecked_when_file_missing` — build `parsed` citing `_some_symbol(` against `tmp/does_not_exist_xyz.py`; assert one entry with `status: "unchecked"`.
- `test_symbol_verification_skips_dotted_identifiers` — build `parsed` whose `issue` text contains ONLY dotted expressions (e.g., `"Calls self.foo(bar) and obj.method(arg) and obj._helper(arg) internally"`) against a real file (e.g., `plugins/plan-executor/scripts/plan_codex_dispatch.py`) that does NOT contain those literals; assert empty list. Locks the F4 out-of-scope boundary.
- `test_review_envelope_includes_wrapper_checks` — call `make_envelope(..., extra={"wrapper_checks": {"symbol_warnings": []}, ...})` with a minimal `extra` dict and assert the returned envelope contains `envelope["wrapper_checks"]["symbol_warnings"] == []` at top-level.
- `test_d5_dispatch_template_and_skill_forward_wrapper_checks` — helper `_phase_d5_section()` reads `dispatch-templates.md` and extracts the `## Phase D.5` section through the next `## ` heading; assert the section contains `Wrapper checks` AND `<wrapper_checks_json>` AND that the new block appears in the template after the `Codex findings` block and before the verdict decision rubric. Additionally, read `plugins/plan-executor/skills/implement-plan/SKILL.md` and assert the Phase D.5 render-call line contains the substring `render(templates.PhaseD5` AND the argument `wrapper_checks` — closes the template/skill plumbing gap.

The existing `test_fixture_matches_review_schema` continues to run as-is (schema untouched); it serves as a regression check that no one accidentally added fields to `codex_review_schema.json`.

### Step 6 (TASK-027B) — Plan-implementer spec edits

Edit `plugins/plan-executor/agents/plan-implementer.md` Step 1 paragraph (line 36-38). Append to the existing paragraph (do NOT replace the "live code differs" trigger):

> If the plan names a specific mechanism — a subcommand, a CLI invocation, a helper function, or a flag — and you chose to use a different mechanism that produces the same output (for example, because the surrounding code uses an internal helper where the plan names the CLI form), flag it under **Plan adaptations** with a one-line justification. This applies even when the emitted behavior is identical; the reviewer needs to see the substitution to judge whether it was the right call.
>
> Worked example: the plan says "emit the audit event via `scripts/foo.py log-event`." The surrounding code in the function you are editing uses an internal helper `_emit_event(...)` for every other event kind. You call `_emit_event("audit", ...)` to stay consistent. That substitution MUST be flagged under Plan adaptations, even though the emitted event shape is identical.

Edit the **Plan adaptations:** section in the report-format block (line 98-99). Append to the existing paragraph:

> This includes literal-wording substitutions where you used an equivalent mechanism (plan names mechanism A, you used mechanism B that produces the same output). Such cases MUST be flagged even when the emitted behavior is identical.

### Step 7 (TASK-027B) — Plan-implementer spec tests

Create `tests/scripts/test_plan_implementer_spec.py`:

- Module-level `REPO_ROOT = Path(__file__).resolve().parents[2]`, `SPEC = REPO_ROOT / "plugins" / "plan-executor" / "agents" / "plan-implementer.md"`.
- `_spec_text()` helper returns the file content.
- `test_literal_wording_substitution_trigger_present` — assert the Step 1 paragraph contains the new trigger wording (substring match on e.g. "specific mechanism" AND "flag it under") and the Plan-adaptations paragraph contains the substitution wording (substring match on "literal-wording substitutions" OR "mechanism A … mechanism B"-style phrasing).
- `test_plan_adaptations_worked_example_present` — assert the worked example appears (substring on "internal helper" AND "identical").
- `test_parse_implementer_report_contract_preserved` — construct an in-memory implementer report string with `**Plan adaptations:**\n- None` and feed it to `plan_ops.py parse-implementer-report --report-path <tmp>`; assert exit 0 and the parsed `plan_adaptations` field is `["None"]` or `[]` per the existing contract.

### Step 8 (TASK-027C) — D.5 rubric disambiguation

Edit `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` Phase D.5 dismissal-evidence gate section (currently at line 460). Insert a new clause BETWEEN the existing numbered items (items 1 and 2) and the "Plan says so without a `spec-deference` label" paragraph:

> **Boundary between `dismissed` and `spec-deference`.** `spec-deference` applies **only** when the implementer's change followed the plan's literal wording AND Codex is disputing what the plan mandated. If the implementer deviated from the plan's literal wording — even defensibly, citing surrounding-code patterns, internal-helper substitution, or a semantically-equivalent alternative — the correct disposition is `dismissed` (with concrete-verification evidence showing the deviation is correct) OR surfaced at `minor-findings` for a future plan-review pass. Do NOT label implementer-deviation cases `spec-deference`; that would silently bury the implementer's judgment call instead of surfacing the substitution for review.
>
> Worked example: the plan says "emit the event via `scripts/foo.py log-event`." The implementer used an internal helper that produces the same JSONL line (matching surrounding-code pattern). Codex flagged the deviation. Correct D.5 disposition: `dismissed` with verification pointing to the surrounding-code pattern. Incorrect: `spec-deference` — the implementer did not follow the plan's literal wording. Contrast: if the plan said "use `shell=True`" and the implementer used `shell=True` and Codex objected to `shell=True` itself, the disposition IS `spec-deference`.

### Step 9 (TASK-027C) — D.5 rubric test

Add to `tests/scripts/test_codex_review_prompt.py`:

- Helper `_phase_d5_section()` — extract `## Phase D.5` through next `## ` heading.
- `test_d5_spec_deference_rubric_scoped` — assert the section contains the precondition wording (substring match on "implementer" AND "followed" AND "literal wording") and the contrasting worked example (both `dismissed` and `spec-deference` appear within 200 characters of each other in the example paragraph).

### Step 10 — Run the full test suite

```
venv/bin/pytest -q tests/scripts/test_codex_review_prompt.py tests/scripts/test_plan_implementer_spec.py
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

First command must be fully green. Second command may fail `test_analyst_to_parse_schedule_roundtrip` (pre-existing, unrelated); no other failures allowed.

Expected: prompt includes description + impl_notes; wrapper annotates hallucinated symbols; plan-implementer surfaces literal-wording substitutions; D.5 `spec-deference` label is scoped to implementer-follows-plan cases.
