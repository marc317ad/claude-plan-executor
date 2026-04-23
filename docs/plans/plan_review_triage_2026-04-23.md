# Phase 1.5.5 — plan-review triage third-opinion for Codex `needs-replan`

**Created:** 2026-04-23
**Status:** pending
**Base branch:** main

## Goal

Insert a **Phase 1.5.5 triage** step between Phase 1.5 (Codex plan-review) and the `plan-author` auto-revise dispatch, so Codex's pedantic `needs-replan` verdicts no longer automatically halt the run or trigger a plan-prose rewrite. A fresh Claude `plan-review-triage` subagent (sonnet) reads the plan text and Codex's findings and returns one of `ship | ship-with-fixes | partial-agreement | needs-rework`. The orchestrator routes by that verdict — `ship | ship-with-fixes` proceed to Phase 2 with Codex findings carried to the summary as notes; `partial-agreement` dispatches `plan-author` with the load-bearing subset only; `needs-rework` dispatches `plan-author` with the full findings array (today's behavior). Mirrors the existing §8.4 Phase D.5 task-level escalation mechanism, adapted for plan-level input shape and plan-level routing.

## Context

`/implement-plan`'s Phase 1.5 Codex plan-review already has partial escape valves:

- `--skip-plan-review` bypasses the review entirely.
- `--allow-gaps` wires a prompt-level demotion clause that flips `needs-replan` → `approved-with-notes` when schedule gaps are soft-only — a single-axis workaround.
- `--no-auto-revise` halts on first `needs-replan` instead of auto-dispatching `plan-author`.

What does not exist: any mechanism to adjudicate whether a Codex `needs-replan` verdict is actually load-bearing versus a pedantic flag. The auto-revise path blindly dispatches `plan-author` with Codex's findings verbatim, the plan gets edited, and a second review runs. Empirical run-log data: 14/36 plan reviews (~39%) return `needs-replan`; many second passes flip to `approved-with-notes` after cosmetic edits that did not materially change the plan.

The §8.4 / Phase D.5 task-level escalation already solved this exact pattern for task reviews: when Codex returns `needs-rework` on a committed diff, a fresh `code-reviewer` subagent (sonnet) adjudicates Codex's findings into load-bearing + dismissed buckets under a dismissal-evidence gate, and routes four verdicts (`ship | ship-with-fixes | partial-agreement | needs-rework`). Porting the shape of that mechanism to plan-level review is mechanical: same verdict vocab, same dismissal-evidence gate text, same parse-and-route contract. The three asymmetries are local and don't affect the spine:

1. **Input is plan markdown + findings**, not a diff + task block. Different dispatch template body; same wrapper envelope shape at the parser seam.
2. **`ship-with-fixes` at plan level has no commit trailer** to carry a `[disagreement]` tag. Carryover is via the run summary's existing "Plan review notes" section.
3. **No awaiting-user pause.** Phase 1.5 runs before any batch, so a second `needs-replan` after triage + plan-author + re-review halts via today's `run_end reason=plan_review_failed` path — no pending working-tree edits to adjudicate.

The triage subagent is a fresh, no-conversation-context dispatch. Source-code reading is explicitly out of scope for v1 — the triage adjudicates Codex findings against the plan prose only, so the evidence gate resolves to "explicit spec contradiction" or "concrete verification move via re-reading the plan". This mirrors D.5's constraint that the reviewer works from the diff + task block, not exploratory code reading.

**Gemini integration note.** `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-21.md` plans a Gemini wrapper that would add a third-family reviewer with codebase-investigation tools. v1 ships Claude-as-triage (mirroring D.5); the dispatch seam (Agent subagent_type + markdown report + JSON parser) is kept pluggable so a `--triage-model gemini` flag can drop in once the wrapper lands. v1 does not block on the Gemini wrapper.

**In scope**
- New JSON schema sidecar `codex_plan_review_triage_schema.json`.
- New `plan_ops.py parse-plan-review-triage-report --stdin` subcommand.
- New agent spec `plugins/plan-executor/agents/plan-review-triage.md` (sonnet).
- New dispatch-template section in `dispatch-templates.md` for Phase 1.5.5 (paralleling Phase D.5).
- SKILL.md §Phase 1.5 wiring: insert the triage step before plan-author; route by triage verdict; add `--codex-plan-review-binding` CLI flag; add run-log events `plan_review_triage_start` / `plan_review_triage_done`.
- Regression + unit tests for the parser and the four-verdict routing contract.

**Out of scope (v1)**
- Gemini-backed triage. Deferred until the Gemini wrapper is shipped; keep the dispatch pluggable, do NOT implement `--triage-model` selection in v1.
- Changes to `--no-auto-revise` semantics. Triage is on the auto-revise path only; `--no-auto-revise` still halts on first `needs-replan` without triage.
- Changes to `approved` / `approved-with-notes` routing. Triage fires only on `needs-replan`.
- Code-reading triage. The triage subagent has Read / Grep / Glob / Bash for plan + schema cross-reference only; it does NOT exploratorily read source files to confirm or refute Codex findings. (v2 can extend this via the Gemini `Investigator` role if needed.)
- Multi-pass triage. The triage runs once per review; the second plan-review pass (after plan-author revision on the `partial-agreement` / `needs-rework` routes) is binding and is NOT re-triaged — matches D.5's "re-review is binding" rule.
- Awaiting-user pause. Phase 1.5 halts have no pending working-tree state; the existing `run_end reason=plan_review_failed` path is reused unchanged for second-`needs-replan` failures.

## Verification

1. **Regression: approved path.** A plan that Codex returns `approved` on runs identically to today. No triage dispatch, no new events in the run log. `plan_ops.py audit --json` passes.
2. **Regression: approved-with-notes.** Identical to today. Notes carried to summary; no triage dispatch.
3. **Regression: --skip-plan-review.** Entire Phase 1.5 + 1.5.5 skipped; loud banner unchanged.
4. **Regression: --no-auto-revise.** First `needs-replan` halts without triage, same as today (`run_end reason=plan_review_failed`).
5. **New path: `ship`.** Codex `needs-replan` → triage returns `ship` → orchestrator proceeds to Phase 2. `plan-author` NOT dispatched; no re-analyst, no second plan-review. Run log sequence: `plan_review_done {verdict:"needs-replan"}` → `plan_review_triage_start` → `plan_review_triage_done {verdict:"ship"}` → `batch_start`. Summary carries `[plan-review-disagreement]` banner listing Codex findings verbatim.
6. **New path: `ship-with-fixes`.** Same routing as `ship` at plan level — proceed to Phase 2, findings carried to summary under a "Plan review notes" section. Run log `plan_review_triage_done {verdict:"ship-with-fixes"}`.
7. **New path: `partial-agreement`.** `plan-author` dispatched with a findings payload filtered to the `load_bearing` indices only. Dismissed findings carried to summary verbatim. After plan-author + re-analyst + second plan-review, the second verdict is binding per today's contract (no re-triage on the second pass). Summary carries `[partial-agreement]` marker listing dismissed finding indices.
8. **New path: `needs-rework`.** `plan-author` dispatched with the full findings array — identical to today's auto-revise path. Run log shows the triage events interleaved between Codex plan-review and plan-author dispatch.
9. **`--codex-plan-review-binding`.** First `needs-replan` → halt immediately with `run_end reason=plan_review_failed`; NO triage dispatch, NO plan-author dispatch. Parallel to the existing `--codex-review-binding` flag semantics at task level.
10. **Dismissal-evidence gate.** Triage agent spec enforces the same "concrete verification move OR explicit spec contradiction" gate as D.5 (§dispatch-templates.md line 247). A dismissal without a cited spec or verification anchor must downgrade to `minor-findings`-style phrasing and the finding still surfaces in the summary rather than being silently buried.
11. **Schema + audit.** `plan_ops.py audit --json` passes. `parse-plan-review-triage-report --stdin` validates the envelope against `codex_plan_review_triage_schema.json` and rejects malformed payloads with canonical `errors[*]` (same shape as `parse-plan-review-report`).
12. **Parser regression matrix.** Unit tests cover: each of the four verdicts round-trips; `partial-agreement` with empty `load_bearing` or empty `dismissed` rejects as `partial-agreement-invalid-split`; out-of-range / duplicate / non-disjoint indices reject; envelope-level errors (missing `subcommand`, wrong `subcommand`, malformed JSON, terminal outcomes) route per the existing `parse-plan-review-report` pattern.

## Tasks

### TASK-001: Triage schema + parser subcommand

- **Status:** pending
- **Priority:** high
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/codex_plan_review_triage_schema.json
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "plan_review_triage" -x`
- **Acceptance criteria:**
  - New schema file `codex_plan_review_triage_schema.json` with fields `{verdict ∈ {ship, ship-with-fixes, partial-agreement, needs-rework}, load_bearing: int[], dismissed: int[], summary: string}`. `load_bearing` / `dismissed` are required but may be empty arrays for `ship | ship-with-fixes | needs-rework`; for `partial-agreement` both MUST be non-empty.
  - New `plan_ops.py` subcommand `parse-plan-review-triage-report --stdin [--json]`. Reads the full subagent-report envelope from stdin (markdown + embedded ```json block, matching how D.5's report is consumed — see `dispatch-templates.md` line 259 for the D.5 output shape).
  - Extraction: scan the stdin buffer for the last fenced ```json block and parse it as the triage payload. If zero fenced JSON blocks are present, halt with `{code: "triage-report-missing-json", path: "$"}`. If the outer payload is not an object, halt with `{code: "invalid-type", path: "$"}`.
  - Schema validation against `codex_plan_review_triage_schema.json`: per-field type errors, enum violations, missing required fields all surface as canonical `errors[*]` entries (`{path, code, message}` shape matching `parse-plan-review-report`).
  - `partial-agreement` invariant enforcement: both buckets non-empty, disjoint, every index in range `[0, findings_count)` where `findings_count` is supplied as a required `--findings-count N` argument (the parser needs to know the bucket-index domain; it does not re-read the original Codex envelope). Violations surface as `{code: "partial-agreement-invalid-split"}` or `{code: "triage-index-out-of-range"}` or `{code: "triage-buckets-not-disjoint"}`.
  - Successful validation emits `{verdict, load_bearing, dismissed, summary, findings_count}` on stdout.
  - Subcommand wired into the existing argparse dispatch table in `plan_ops.py` (follow the `parse-plan-review-report` pattern at lines 2940 / 6545 / 6960).
  - Tests in `tests/scripts/test_plan_ops.py` cover: (a) each of the four verdicts round-trips cleanly; (b) `partial-agreement` with `load_bearing=[]` rejects; (c) `partial-agreement` with `dismissed=[]` rejects; (d) `partial-agreement` with overlapping indices rejects; (e) out-of-range index rejects; (f) missing fenced JSON block rejects; (g) multiple fenced JSON blocks — last one wins; (h) malformed outer JSON rejects.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py && rm plugins/plan-executor/scripts/codex_plan_review_triage_schema.json`

**Description:**
Pure parser + schema addition; no orchestrator wiring yet. Mirror `parse-plan-review-report`'s shape closely — the orchestrator will pipe the triage subagent's markdown report through this subcommand the same way it pipes Codex's plan-review envelope through `parse-plan-review-report`. The `--findings-count N` arg is needed because the triage report references Codex findings by 0-based array index but the parser has no way to know how many findings existed in the original envelope; the orchestrator supplies the count from the prior `parse-plan-review-report` call.

**Implementation notes:**
`parse-plan-review-report` lives at `plan_ops.py:2940` with the argparse wiring at line 6545 and the dispatch table entry at line 6960 — match that pattern. The schema file sits next to `codex_plan_review_schema.json` at `plugins/plan-executor/scripts/`. D.5 accepts `partial-agreement` with `load_bearing=[0, 2], dismissed=[1, 3]` as canonical (see `dispatch-templates.md` lines 270-275) — use the same array-of-integer shape. The markdown-report-with-embedded-JSON extraction pattern matches how D.5's output is parsed; grep the existing codebase for how D.5's verdict gets pulled from `code-reviewer` agent output (the orchestrator handles this inline in SKILL.md, but a parser is preferred for TASK-001 because it centralizes the validation contract).

### TASK-002: `plan-review-triage` subagent spec

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/agents/plan-review-triage.md
- **Dependencies:** TASK-001
- **Test command:** none (agent spec; exercised by TASK-004's integration)
- **Acceptance criteria:**
  - New agent spec file at `plugins/plan-executor/agents/plan-review-triage.md` with frontmatter `{name: plan-review-triage, description: ..., tools: Read, Grep, Glob, Bash, model: sonnet}`.
  - Document the input contract: `plan_path` (absolute path to the plan being reviewed), `codex_findings_json` (verbatim `parsed.findings` array from the Phase 1.5 envelope), `codex_summary` (verbatim `parsed.summary` string), `schedule_path` (absolute path to the persisted schedule — the triage may read it but MUST NOT edit it).
  - Document the four-verdict output vocab `ship | ship-with-fixes | partial-agreement | needs-rework` with the same rubric D.5 uses (`dispatch-templates.md` lines 240-246), re-worded for plan review. Verdict semantics at plan level:
    - `ship` — disagree with Codex entirely; no finding is load-bearing; proceed to Phase 2 with a bare `[plan-review-disagreement]` tag in the run summary.
    - `ship-with-fixes` — disagree on blockers; residual concerns are minor notes. Proceed to Phase 2 with findings carried to the summary's "Plan review notes" section.
    - `partial-agreement` — findings split cleanly; at least one is load-bearing AND at least one can be safely dismissed. Dispatches `plan-author` on the load-bearing subset only; dismissed indices carried verbatim to the summary.
    - `needs-rework` — all findings are load-bearing. Dispatches `plan-author` with the full findings array — today's auto-revise behavior.
  - Document the dismissal-evidence gate verbatim from `dispatch-templates.md` line 247 (Phase D.5), re-scoped for plan-level: the two permitted dismissal justifications are (1) a concrete verification move against the plan text (e.g. re-read the cited section and confirm the finding misreads it; check the schedule's `tasks[]` for the claimed missing entry) or (2) an explicit spec contradiction (the plan's stated acceptance criteria or its Implementation Playbook mandates the behavior Codex objects to). Unverified dismissals MUST downgrade to `minor-findings`-style phrasing and the finding still surfaces.
  - Document the JSON output shape matching `codex_plan_review_triage_schema.json`: `{verdict, load_bearing, dismissed, summary}` for `partial-agreement`; `{verdict, load_bearing: [], dismissed: [], summary}` for the other three.
  - Explicit hard rules: (a) no source-code reading (v1); (b) no Edit / Write / Agent tools; (c) no plan-file mutation (triage is read-only on the plan); (d) word cap ≤500 on the rationale / summary sections.
  - The spec mentions the "You do NOT have the Agent tool" constraint verbatim, per existing agent spec convention.
- **Reversion guidance:** `git restore plugins/plan-executor/agents/plan-review-triage.md`

**Description:**
A fresh-conversation-context subagent spec that parallels `code-reviewer.md`'s shape but is plan-scoped. The key discipline is the dismissal-evidence gate: D.5's gate text translates directly — replace "trace the cited control-flow path" with "re-read the cited plan section" and "read the cited symbol in the repo" with "check the schedule's `tasks[]` for the claimed missing entry". Everything else is structurally the same.

**Implementation notes:**
Use `plan-author.md` and `plan-remediator.md` as the shape templates — they're the closest existing agent specs to plan-scoped work. The dismissal-evidence gate text lives at `dispatch-templates.md:247-252` — copy verbatim and re-scope. Frontmatter `tools` list excludes `Edit`/`Write` because the triage is strictly read-only on the plan. Model choice is sonnet, matching D.5 — NOT opus, to keep cost parity with task-level review.

### TASK-003: Phase 1.5.5 dispatch template

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
- **Dependencies:** TASK-002
- **Test command:** none (template documentation; exercised by TASK-004's integration)
- **Acceptance criteria:**
  - New section `## Phase 1.5.5 — plan-review-triage dispatch (needs-replan third opinion)` inserted between the existing `## Phase 1.5a — plan-author dispatch` and `## Phase B — plan-implementer dispatch` sections.
  - Template body mirrors Phase D.5's body at `dispatch-templates.md:218-280`, re-scoped:
    - Scope line: `plan_path`, `schedule_path`.
    - Embed the plan text verbatim in a fenced ```markdown block.
    - Embed Codex findings verbatim in a fenced ```json block.
    - Embed Codex summary verbatim in a fenced code block.
    - Verdict decision rubric (the four-verdict ladder from TASK-002's agent spec) and the dismissal-evidence gate from D.5, verbatim re-scoped.
    - Output shape specification: same two JSON shapes as D.5 (`{verdict, summary}` for three verdicts; `{verdict, load_bearing, dismissed, summary}` for `partial-agreement`).
  - The Agent dispatch header names `subagent_type: "plan-review-triage"` and `model: "sonnet"`, paralleling D.5's `code-reviewer` header.
  - Includes the "You do NOT have the Agent tool" closer verbatim, per template convention.
  - Explicit instruction that the triage is NOT permitted to dispatch subagents, open Edit/Write, or read source code in v1.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/dispatch-templates.md`

**Description:**
Insert a Phase 1.5.5 section in the dispatch templates file. Structurally it's a D.5 clone — the prompt body, verdict rubric, and dismissal-evidence gate are copy-adapted from `dispatch-templates.md:218-280`. The only substantive content change is the scope declaration (plan + schedule, not diff + task block) and the verdict semantics labels.

**Implementation notes:**
Place the new section AFTER Phase 1.5a (plan-author) and BEFORE Phase B (plan-implementer). Maintain the file's ordering convention: numbered phase → alphabetic sub-phase. Insertion point: search for the `## Phase B — plan-implementer dispatch` heading and insert before it. Do NOT re-number any downstream sections — Phase D.5 retains its name; the new section is `Phase 1.5.5` specifically to signal "between Phase 1.5 and Phase 1.5a" in the existing numbering scheme.

### TASK-004: SKILL.md orchestrator wiring + CLI flag + run-log events

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
- **Dependencies:** TASK-003
- **Test command:** none (orchestrator prose; validated by `plan_ops.py audit --json` reading canonical-contract cross-references)
- **Acceptance criteria:**
  - New `--codex-plan-review-binding` flag added to the `## Parse arguments` CLI surface (currently at `SKILL.md:102-120`). Docstring mirrors `--codex-review-binding`: "Codex `needs-replan` on plan review goes straight to halt; no Phase 1.5.5 third-opinion escalation."
  - `## Analysis (Phase 1)` § — the `needs-replan` routing block (currently at `SKILL.md:275` in the verdict table + the `**needs-replan branch — auto-revise path**` prose) is amended: insert a Phase 1.5.5 step BEFORE the `plan-author` dispatch. New text documents:
    - Skip conditions: `--codex-plan-review-binding` → halt immediately; `--no-auto-revise` → halt immediately (both paths unchanged from today, just made explicit).
    - Dispatch: `Agent(subagent_type: "plan-review-triage", model: "sonnet", prompt: render(templates.Phase1_5_5, plan_text, findings, summary, schedule_path))`.
    - Parse: `printf '%s' "<agent_output>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-plan-review-triage-report --stdin --findings-count <N> --json` where `<N>` is the `findings_count` from the prior `parse-plan-review-report` call.
    - Route: `ship | ship-with-fixes` → proceed to Phase 2 (skip plan-author, skip re-analyst, skip re-review); `partial-agreement` → dispatch `plan-author` with findings filtered to the `load_bearing` indices AND carry `dismissed` findings into the run summary; `needs-rework` → dispatch `plan-author` with the full findings array (today's behavior).
    - Run-log event ordering update to the V8 sequence at `SKILL.md:317`: `plan_review_done {verdict:"needs-replan"}` → `plan_review_triage_start {run_id, plan_file, findings_count}` → `plan_review_triage_done {run_id, plan_file, verdict, load_bearing_count, dismissed_count, summary}` → (branch by verdict) batch_start | plan_author_start.
  - New `## Rules` entry documenting the "re-review is binding after triage + plan-author" invariant: after a `partial-agreement` or `needs-rework` triage verdict, the `plan-author` → re-analyst → re-review sequence is run ONCE and the second plan-review verdict is binding. A second `needs-replan` on the re-review halts per the existing `run_end reason=plan_review_failed` path; NO second triage is dispatched.
  - Summary carryover documented: the existing "Plan review notes" section in the end-of-run summary receives Codex findings verbatim for `ship-with-fixes` verdicts, with dismissed-finding indices called out for `partial-agreement`. A `[plan-review-disagreement]` banner is added for `ship` verdicts.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md`

**Description:**
The orchestrator wiring task. SKILL.md is the operational specification of `/implement-plan`, so this task amends the Phase 1 routing prose, the CLI flag list, the run-log event vocabulary, and the `## Rules` section. No Python or schema changes — TASK-001 already shipped the parser + schema, TASK-002 shipped the agent spec, TASK-003 shipped the dispatch template. TASK-004 connects them.

**Implementation notes:**
Insert the Phase 1.5.5 routing block as a new subsection between `### Phase 1.5 — Codex plan review (independent pre-dispatch gate)` and the existing `**needs-replan branch — auto-revise path (default).**` block. Re-order the `needs-replan` block so the triage step is the first action on the auto-revise path, and plan-author is dispatched only if the triage verdict calls for it. Update the verdict routing table at `SKILL.md:271-276` to reference the new triage step. The `--codex-plan-review-binding` CLI entry goes after `--codex-review-binding` in the Optional flags list at `SKILL.md:117`. The V8 run-log ordering at `SKILL.md:317` needs the two new event names interleaved in the needs-replan branch.

### TASK-005: Regression harness for the triage contract

- **Status:** pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "plan_review_triage_contract" -x`
- **Acceptance criteria:**
  - A new test class `TestPlanReviewTriageContract` in `tests/scripts/test_plan_ops.py` exercises the parser → routing contract without requiring live orchestrator execution:
    - Build four synthetic triage envelopes (one per verdict); parse each via `parse-plan-review-triage-report` with `--findings-count 4`; assert the extracted `{verdict, load_bearing, dismissed, summary}` values.
    - Build a synthetic `partial-agreement` envelope where `load_bearing=[0, 2]` and `dismissed=[1, 3]` for a findings array of length 4; assert parser output round-trips.
    - Assert the four invariants of `partial-agreement` (empty bucket, duplicate index, out-of-range index, overlap) all surface as parser errors with the documented `code` values from TASK-001.
  - The harness uses the same fixture-factory pattern as the existing `_plan_review_envelope` helper at `test_plan_ops.py:6827` — build a helper `_plan_review_triage_envelope(verdict, load_bearing=None, dismissed=None, summary="...")` and parameterize tests over the verdict matrix.
  - No subprocess dispatch of a live triage agent; the tests validate the parser + schema contract only. Orchestrator routing is validated at spec level via the verification items in this plan's `## Verification` section — the SKILL.md audit run (`plan_ops.py audit --json`) catches drift between the SKILL.md routing documentation and the parser contract.
- **Reversion guidance:** `git restore tests/scripts/test_plan_ops.py`

**Description:**
Defense-in-depth beyond TASK-001's unit tests. TASK-001 tests the parser in isolation; TASK-005 tests the parser's output against the contract the orchestrator (SKILL.md Phase 1.5.5) will consume. If TASK-004's SKILL.md routing ever disagrees with TASK-001's parser output shape, this harness is where the drift surfaces — before a live run hits it.

**Implementation notes:**
The `_plan_review_envelope` helper at `test_plan_ops.py:6827` is the shape to mirror. Use the existing CLI-subprocess pattern (`self._run_parser(envelope)`) — live subprocess-invocations of `plan_ops.py` are how the existing plan-review parser tests are structured, and they catch CLI wiring regressions that pure-Python unit tests would miss. No triage subagent dispatch in the test — construct the markdown-report-with-embedded-JSON payload directly via a helper.

## Expected outcome

After this plan is executed: a Codex `needs-replan` verdict on Phase 1.5 plan review no longer automatically halts the run or auto-dispatches a plan-prose rewrite. Instead, a `plan-review-triage` subagent adjudicates Codex's findings against the plan; the run proceeds to Phase 2 when the triage judges Codex wrong (the common case for the empirical ~39% `needs-replan` rate); a filtered `plan-author` dispatch handles the genuine partial-agreement case; and the full plan-author dispatch fires only when the triage concurs with Codex across the board. Mirrors the Phase D.5 mechanism that task-level review already uses successfully.

**Future work (v2, not in this plan):** `--triage-model {claude|gemini}` flag once `PLAN_GEMINI_INTEGRATION_2026-04-21.md` is shipped. The Gemini Investigator role's codebase-investigation tools would widen the triage evidence gate beyond plan-text cross-reference into actual source-code verification — genuinely strengthening the dismissal-evidence rule rather than just adding a third model family.
