# Plan triage — third-opinion adjudication for plan-analyst `needs-enrichment` and Codex plan-review `needs-replan`

**Created:** 2026-04-23
**Status:** pending
**Base branch:** main

## Goal

Introduce a **source-parameterized plan-triage step** that fires on two pedantic-flag outcomes in Phase 1 / Phase 1.5:

1. **Phase 1 analyst `needs-enrichment`** — today halts (or proceeds under `--allow-gaps` via a prompt-level demotion). Triage becomes the default adjudicator.
2. **Phase 1.5 Codex plan-review `needs-replan`** — today auto-dispatches `plan-author` with the findings verbatim. Triage adjudicates first.

A fresh Claude `plan-review-triage` subagent (sonnet) reads the plan text plus the source-specific evidence payload (analyst `gaps[]` OR Codex `findings[]`) and returns one of `ship | ship-with-fixes | partial-agreement | needs-rework`. The orchestrator routes by that verdict — `ship | ship-with-fixes` proceed to the next phase with the original reviewer's items carried to the summary as notes; `partial-agreement` dispatches `plan-author` with the load-bearing subset only; `needs-rework` dispatches `plan-author` with the full array (today's auto-revise behavior on the Codex path; the analyst path gains `plan-author`-enrichment routing for the first time). Mirrors the existing §8.4 Phase D.5 task-level escalation mechanism, adapted for plan-level input shapes and plan-level routing.

The triage is a **single agent** parameterized by `source ∈ {plan-analyst, codex-plan-review}`; one schema, one parser (via `--source` flag), one agent spec documenting two input contracts, one dispatch template with a source-discriminated body. Two SKILL.md insertion points share the triage machinery — one after analyst `needs-enrichment`, one after Codex `needs-replan`.

## Context

### The two pedantic-flag outcomes

`/implement-plan` has two plan-stage outcomes whose naive handling punishes pedantic flags with disproportionate work:

**(A) Phase 1 analyst `needs-enrichment`.** Plan-analyst (Claude/Opus) produces the schedule. If it flags gaps (missing Files, missing Test commands, missing Acceptance criteria bullets, missing Dependencies), it sets `outcome=needs-enrichment` with a `gaps[]` array. Today the orchestrator either halts or — if `--allow-gaps` is set — proceeds via a prompt-level severity-aware demotion (the wrapper injects a demotion clause when every gap's `severity="soft"`). Missing mechanism: any adjudication of whether a gap is genuinely load-bearing. The analyst can flag cosmetic omissions (e.g. implicit Dependencies on TASK-001 that a human reader would infer from narrative prose) alongside genuinely plan-blocking gaps, and the orchestrator treats both identically.

**(B) Phase 1.5 Codex plan-review `needs-replan`.** Codex reviews the persisted plan + schedule independently. If it returns `needs-replan`, the auto-revise path blindly dispatches `plan-author` with Codex's findings verbatim, the plan gets edited, and a second review runs. Empirical run-log data: 14/36 plan reviews (~39%) return `needs-replan`; many second passes flip to `approved-with-notes` after cosmetic edits that did not materially change the plan.

### Partial escape valves that exist today

- `--skip-plan-review` bypasses Phase 1.5 entirely.
- `--allow-gaps` wires a prompt-level demotion clause that flips `needs-replan` → `approved-with-notes` when schedule gaps are soft-only — and also causes the orchestrator to proceed past analyst `needs-enrichment` with a warning.
- `--no-auto-revise` halts on first `needs-replan` instead of auto-dispatching `plan-author`.

These flags are binary — skip or don't skip, halt or don't halt. None of them adjudicate *which* items in the gaps/findings array are load-bearing.

### The triage shape already exists at task level

The §8.4 / Phase D.5 task-level escalation already solved this exact pattern for task reviews: when Codex returns `needs-rework` on a committed diff, a fresh `code-reviewer` subagent (sonnet) adjudicates Codex's findings into load-bearing + dismissed buckets under a dismissal-evidence gate, and routes four verdicts (`ship | ship-with-fixes | partial-agreement | needs-rework`). Porting the shape of that mechanism to plan-level adjudication is mechanical: same verdict vocab, same dismissal-evidence gate text, same parse-and-route contract. The asymmetries are local and don't affect the spine:

1. **Input is plan markdown + source-specific evidence**, not a diff + task block. Analyst source embeds `gaps[]` (each entry carries `location`, `missing_field`, `severity`, optional `detail`); Codex plan-review source embeds `findings[]` (free-form objects with `severity`, `message`, optional `location`). Two wire shapes, one common triage rubric.
2. **`ship-with-fixes` at plan level has no commit trailer** to carry a `[disagreement]` tag. Carryover is via the run summary's existing "Plan review notes" section.
3. **No awaiting-user pause.** Phase 1 / Phase 1.5 run before any batch, so a second `needs-replan` after triage + plan-author + re-review halts via today's `run_end reason=plan_review_failed` path — no pending working-tree edits to adjudicate.
4. **Same-family caveat on the analyst path.** The analyst is Claude/Opus and the triage is Claude/Sonnet — same family grading itself, which is epistemically weaker than Codex-reviewing-Codex (which has a separate model) or Claude-triage-over-Codex-review (cross-family). This is a *known and accepted* limitation for v1: the analyst-triage path still wins because (a) the dismissal-evidence gate constrains the triage to cite plan text or schema contradictions rather than vibe-dismiss, and (b) v2's Gemini integration (see below) adds cross-family triage to close this gap. Document the caveat explicitly in the agent spec so the operator knows what they're trusting.

The triage subagent is a fresh, no-conversation-context dispatch. Source-code reading is explicitly out of scope for v1 — the triage adjudicates against the plan prose + schedule only, so the evidence gate resolves to "explicit spec contradiction" or "concrete verification move via re-reading the plan / schedule". This mirrors D.5's constraint that the reviewer works from the diff + task block, not exploratory code reading.

**Gemini integration note.** `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-21.md` plans a Gemini wrapper that would add a third-family reviewer with codebase-investigation tools. v1 ships Claude-as-triage (mirroring D.5); the dispatch seam (Agent subagent_type + markdown report + JSON parser) is kept pluggable so a `--triage-model gemini` flag can drop in once the wrapper lands. v1 does not block on the Gemini wrapper. Gemini is especially valuable on the analyst-source path because it eliminates the same-family caveat above.

**In scope**
- New JSON schema sidecar `codex_plan_review_triage_schema.json` (the schema governs the triage *output*, which is source-agnostic; the schema file keeps the Codex-review-originated name for continuity with `codex_plan_review_schema.json`).
- New `plan_ops.py parse-plan-review-triage-report --stdin --source {plan-analyst|codex-plan-review}` subcommand. The `--source` flag disambiguates the bucket-index domain (analyst `gaps[]` count vs Codex `findings[]` count) and controls which error codes the parser can emit, but does NOT change the output shape.
- New agent spec `plugins/plan-executor/agents/plan-review-triage.md` (sonnet) documenting *both* input contracts (analyst-source and Codex-review-source) with source-specific dismissal-evidence moves.
- New dispatch-template section in `dispatch-templates.md` for plan triage, parameterized by `{source}` — one section with branch-conditional body (analyst branch embeds `gaps[]`, Codex-review branch embeds `findings[]`), shared verdict rubric and dismissal-evidence gate.
- SKILL.md wiring at **two** insertion points:
  - Phase 1 — after analyst returns `needs-enrichment`, before the existing halt/`--allow-gaps` demotion path: insert the triage step. New CLI flag `--analyst-binding` (skip triage, halt on first `needs-enrichment`) parallels `--codex-plan-review-binding`.
  - Phase 1.5 — after Codex returns `needs-replan`, before the existing `plan-author` auto-revise dispatch: insert the triage step. New CLI flag `--codex-plan-review-binding` (skip triage, halt on first `needs-replan`) parallels `--codex-review-binding`.
- New run-log events `plan_review_triage_start` / `plan_review_triage_done` with a `source ∈ {plan-analyst, codex-plan-review}` field — one event vocabulary, two source values.
- Regression + unit tests for the parser (both sources) and the four-verdict routing contract.

**Out of scope (v1)**
- Gemini-backed triage. Deferred until the Gemini wrapper is shipped; keep the dispatch pluggable, do NOT implement `--triage-model` selection in v1. (Gemini will most immediately benefit the analyst-source path by closing the same-family caveat.)
- Changes to `--no-auto-revise` semantics. Triage is on the auto-revise path only; `--no-auto-revise` still halts on first `needs-replan` without triage.
- Changes to `--allow-gaps` semantics. `--allow-gaps` remains as a pre-triage short-circuit on the analyst path: if set, the orchestrator skips analyst triage and proceeds (preserving today's behavior for operators who already know they want to ignore gaps). Triage replaces the demotion *default*; the flag-based opt-out stays.
- Changes to `approved` / `approved-with-notes` routing. Triage fires only on `needs-replan` (Codex path) or `needs-enrichment` (analyst path). `valid` / `approved` / `approved-with-notes` flow unchanged.
- Code-reading triage. The triage subagent has Read / Grep / Glob / Bash for plan + schema cross-reference only; it does NOT exploratorily read source files to confirm or refute gaps or findings. (v2 can extend this via the Gemini `Investigator` role if needed.)
- Multi-pass triage. The triage runs once per source per invocation; the second pass (after plan-author revision on the `partial-agreement` / `needs-rework` routes) is binding and is NOT re-triaged — matches D.5's "re-review is binding" rule. On the analyst path specifically: after `plan-author` enrichment and re-analyst, if the re-analyst still returns `needs-enrichment`, halt per the existing `run_end reason=plan_review_failed` path; no second analyst-source triage.
- Awaiting-user pause. Phase 1 / Phase 1.5 halts have no pending working-tree state; the existing `run_end reason=plan_review_failed` path is reused unchanged for second-failure cases.
- Splitting the agent spec or schema per source. One agent, one schema, one parser, one template — source-parameterized. Splitting would double the surface area without strengthening the contract.

## Verification

### Codex plan-review source (Phase 1.5)

1. **Regression: approved path.** A plan that Codex returns `approved` on runs identically to today. No triage dispatch, no new events in the run log. `plan_ops.py audit --json` passes.
2. **Regression: approved-with-notes.** Identical to today. Notes carried to summary; no triage dispatch.
3. **Regression: --skip-plan-review.** Entire Phase 1.5 + plan-review triage skipped; loud banner unchanged.
4. **Regression: --no-auto-revise.** First `needs-replan` halts without triage, same as today (`run_end reason=plan_review_failed`).
5. **New path: `ship` (Codex source).** Codex `needs-replan` → triage returns `ship` → orchestrator proceeds to Phase 2. `plan-author` NOT dispatched; no re-analyst, no second plan-review. Run log sequence: `plan_review_done {verdict:"needs-replan"}` → `plan_review_triage_start {source:"codex-plan-review"}` → `plan_review_triage_done {source:"codex-plan-review", verdict:"ship"}` → `batch_start`. Summary carries `[plan-review-disagreement]` banner listing Codex findings verbatim.
6. **New path: `ship-with-fixes` (Codex source).** Same routing as `ship` at plan level — proceed to Phase 2, findings carried to summary under a "Plan review notes" section. Run log `plan_review_triage_done {source:"codex-plan-review", verdict:"ship-with-fixes"}`.
7. **New path: `partial-agreement` (Codex source).** `plan-author` dispatched with a findings payload filtered to the `load_bearing` indices only. Dismissed findings carried to summary verbatim. After plan-author + re-analyst + second plan-review, the second verdict is binding per today's contract (no re-triage on the second pass). Summary carries `[partial-agreement]` marker listing dismissed finding indices.
8. **New path: `needs-rework` (Codex source).** `plan-author` dispatched with the full findings array — identical to today's auto-revise path. Run log shows the triage events interleaved between Codex plan-review and plan-author dispatch.
9. **`--codex-plan-review-binding`.** First `needs-replan` → halt immediately with `run_end reason=plan_review_failed`; NO triage dispatch, NO plan-author dispatch. Parallel to the existing `--codex-review-binding` flag semantics at task level.

### Plan-analyst source (Phase 1)

10. **Regression: analyst `valid`.** Analyst returns `outcome=valid` → schedule persisted → Phase 1.5 runs → no analyst-source triage dispatched. Run log has no `plan_review_triage_start {source:"plan-analyst"}` event.
11. **Regression: --allow-gaps pre-triage skip.** Analyst returns `outcome=needs-enrichment` AND `--allow-gaps` is set → orchestrator skips analyst triage entirely and proceeds with a warning (today's behavior preserved). Run log records `analyst_triage_skipped {reason:"allow_gaps"}` for auditability. `plan_author` NOT dispatched.
12. **Regression: --analyst-binding.** Analyst returns `needs-enrichment` AND `--analyst-binding` is set → halt immediately with `run_end reason=plan_analyst_failed`; NO triage dispatch, NO plan-author dispatch. Parallel to `--codex-plan-review-binding` on the Codex path.
13. **New path: `ship` (analyst source).** Analyst `needs-enrichment` → triage returns `ship` → orchestrator proceeds to Phase 1.5 as if the analyst had returned `valid`. No `plan-author` dispatch, no re-analyst. Run log sequence: `analyst_done {outcome:"needs-enrichment"}` → `plan_review_triage_start {source:"plan-analyst"}` → `plan_review_triage_done {source:"plan-analyst", verdict:"ship"}` → `schedule_written` → `plan_review_start`. Summary carries `[analyst-triage-disagreement]` banner listing analyst gaps verbatim.
14. **New path: `ship-with-fixes` (analyst source).** Proceed to Phase 1.5 with analyst gaps carried to the summary's "Plan review notes" section. Run log `plan_review_triage_done {source:"plan-analyst", verdict:"ship-with-fixes"}`.
15. **New path: `partial-agreement` (analyst source).** `plan-author` dispatched with a gaps payload filtered to the `load_bearing` indices only. Dismissed gaps carried to summary verbatim. After plan-author + re-analyst, the re-analyst verdict is binding per today's contract (no re-triage on the second pass). Summary carries `[partial-agreement]` marker listing dismissed gap indices.
16. **New path: `needs-rework` (analyst source).** `plan-author` dispatched with the full gaps array — a new auto-enrichment path (today this outcome halts or proceeds with `--allow-gaps`). Run log shows the triage events interleaved between `analyst_done` and `plan_author_start`.

### Shared invariants

17. **Dismissal-evidence gate.** Triage agent spec enforces the same "concrete verification move OR explicit spec contradiction" gate as D.5 (§dispatch-templates.md line 247) for *both* sources. Analyst-source verification moves: re-read the cited plan section, check schedule's `tasks[]` for the claimed missing field, cross-reference the gap's `missing_field` against the task block's rendered markdown. Codex-source verification moves: re-read the cited plan section, check schedule for the claimed missing entry. A dismissal without a cited spec or verification anchor must downgrade to `minor-findings`-style phrasing and the item still surfaces in the summary.
18. **Schema + audit.** `plan_ops.py audit --json` passes. `parse-plan-review-triage-report --stdin --source <src>` validates the envelope against `codex_plan_review_triage_schema.json` and rejects malformed payloads with canonical `errors[*]` (same shape as `parse-plan-review-report`). `--source` is required (no default) — an omitted `--source` rejects with `triage-source-missing`.
19. **Parser regression matrix (per source).** Unit tests cover, for both `--source plan-analyst` and `--source codex-plan-review`: each of the four verdicts round-trips; `partial-agreement` with empty `load_bearing` or empty `dismissed` rejects as `partial-agreement-invalid-split`; out-of-range / duplicate / non-disjoint indices reject; envelope-level errors (missing `subcommand`, wrong `subcommand`, malformed JSON, terminal outcomes) route per the existing `parse-plan-review-report` pattern. Source-discrimination: omitted or unknown `--source` value rejects with the canonical error code.
20. **Same-family caveat visibility.** The `plan-review-triage.md` agent spec contains an explicit paragraph naming the analyst-source same-family weakness and the dismissal-evidence gate as the mitigation. Verified by grep of the agent spec file for the phrase `same-family` or equivalent.

## Tasks

### TASK-001: Triage schema + source-parameterized parser subcommand

- **Status:** done
- **Priority:** high
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/codex_plan_review_triage_schema.json (create)
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "plan_review_triage" -x`
- **Acceptance criteria:**
  - New schema file `codex_plan_review_triage_schema.json` with fields `{verdict ∈ {ship, ship-with-fixes, partial-agreement, needs-rework}, load_bearing: int[], dismissed: int[], summary: string}`. `load_bearing` / `dismissed` are required but may be empty arrays for `ship | ship-with-fixes | needs-rework`; for `partial-agreement` both MUST be non-empty. The schema is **source-agnostic** — the output shape does not encode which source (analyst gaps vs Codex findings) the triage was adjudicating; the orchestrator tracks that out of band via the `--source` parser flag and the run-log event's `source` field.
  - New `plan_ops.py` subcommand `parse-plan-review-triage-report --stdin --source {plan-analyst|codex-plan-review} --findings-count N [--json]`. Reads the full subagent-report envelope from stdin (markdown + embedded ```json block, matching how D.5's report is consumed — see `dispatch-templates.md` line 259 for the D.5 output shape).
  - **`--source` required flag.** Accepts `plan-analyst` or `codex-plan-review`. Omitted `--source` → halt with `{code: "triage-source-missing", path: "$"}`. Unknown `--source` value → halt with `{code: "triage-source-unknown", path: "$"}`. The flag has NO default — the caller must declare which source they are adjudicating so the parser can (a) size-check `load_bearing`/`dismissed` indices against the correct evidence-array, and (b) emit source-specific error code wording.
  - **`--findings-count N` required flag.** Non-negative integer. For `--source plan-analyst` the count is the analyst's `gaps[]` length; for `--source codex-plan-review` the count is the Codex envelope's `findings[]` length. The orchestrator supplies the count from the prior `parse-schedule --json` call (for analyst source) or `parse-plan-review-report --json` call (for Codex source). Parser has no way to read the original evidence array; the count is the bucket-index domain.
  - Extraction: scan the stdin buffer for the last fenced ```json block and parse it as the triage payload. If zero fenced JSON blocks are present, halt with `{code: "triage-report-missing-json", path: "$"}`. If the outer payload is not an object, halt with `{code: "invalid-type", path: "$"}`.
  - Schema validation against `codex_plan_review_triage_schema.json`: per-field type errors, enum violations, missing required fields all surface as canonical `errors[*]` entries (`{path, code, message}` shape matching `parse-plan-review-report`).
  - `partial-agreement` invariant enforcement: both buckets non-empty, disjoint, every index in range `[0, findings_count)`. Violations surface as `{code: "partial-agreement-invalid-split"}` or `{code: "triage-index-out-of-range"}` or `{code: "triage-buckets-not-disjoint"}`. Error messages should read "gap index X out of range" when `source=plan-analyst` and "finding index X out of range" when `source=codex-plan-review` — same error codes, source-aware message wording (implementation detail: parameterize the message format string by source).
  - Successful validation emits `{verdict, load_bearing, dismissed, summary, findings_count, source}` on stdout. The `source` field on output echoes the input flag — downstream consumers (run-log events, orchestrator routing) read it verbatim.
  - Subcommand wired into the existing argparse dispatch table in `plan_ops.py` (follow the `parse-plan-review-report` pattern at lines 2940 / 6545 / 6960).
  - Tests in `tests/scripts/test_plan_ops.py` cover, for BOTH sources (parameterized): (a) each of the four verdicts round-trips cleanly; (b) `partial-agreement` with `load_bearing=[]` rejects; (c) `partial-agreement` with `dismissed=[]` rejects; (d) `partial-agreement` with overlapping indices rejects; (e) out-of-range index rejects; (f) missing fenced JSON block rejects; (g) multiple fenced JSON blocks — last one wins; (h) malformed outer JSON rejects. Source-flag specific: (i) omitted `--source` rejects with `triage-source-missing`; (j) unknown `--source` value rejects with `triage-source-unknown`; (k) source-aware error messaging asserted for out-of-range test ("gap index" vs "finding index" in the message).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py && rm plugins/plan-executor/scripts/codex_plan_review_triage_schema.json`

**Description:**
Pure parser + schema addition; no orchestrator wiring yet. Mirror `parse-plan-review-report`'s shape closely — the orchestrator will pipe the triage subagent's markdown report through this subcommand the same way it pipes Codex's plan-review envelope through `parse-plan-review-report`. The `--findings-count N` arg is needed because the triage report references evidence items by 0-based array index but the parser has no way to know how many items existed in the original envelope; the orchestrator supplies the count from the prior analyst or plan-review parse call. The `--source` flag is required (no default) so the parser's contract is explicit at the CLI seam — silent default would let the caller accidentally validate an analyst-triage report against Codex semantics or vice versa.

**Implementation notes:**
`parse-plan-review-report` lives at `plan_ops.py:2940` with the argparse wiring at line 6545 and the dispatch table entry at line 6960 — match that pattern. The schema file sits next to `codex_plan_review_schema.json` at `plugins/plan-executor/scripts/`. D.5 accepts `partial-agreement` with `load_bearing=[0, 2], dismissed=[1, 3]` as canonical (see `dispatch-templates.md` lines 270-275) — use the same array-of-integer shape. The markdown-report-with-embedded-JSON extraction pattern matches how D.5's output is parsed. The `source` parameterization is intentionally minimal: a CLI flag + a pair of per-source error message format strings. Do NOT split the parser into two subcommands (`parse-plan-analyst-triage-report` + `parse-codex-plan-review-triage-report`) — that doubles the maintenance surface for near-identical code paths and makes the shared verdict contract less obvious to readers.

### TASK-002: `plan-review-triage` subagent spec (dual-source)

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/agents/plan-review-triage.md (create)
- **Dependencies:** TASK-001
- **Test command:** none (agent spec; exercised by TASK-004's integration)
- **Acceptance criteria:**
  - New agent spec file at `plugins/plan-executor/agents/plan-review-triage.md` with frontmatter `{name: plan-review-triage, description: ..., tools: Read, Grep, Glob, Bash, model: sonnet}`.
  - Document TWO input contracts (one per source), each as a labeled subsection:
    - **`source: codex-plan-review`** — `plan_path` (absolute path to the plan being reviewed), `codex_findings_json` (verbatim `parsed.findings` array from the Phase 1.5 envelope), `codex_summary` (verbatim `parsed.summary` string), `schedule_path` (absolute path to the persisted schedule — the triage may read it but MUST NOT edit it), `findings_count` (length of the findings array — used to range-check `partial-agreement` buckets).
    - **`source: plan-analyst`** — `plan_path` (absolute path to the plan being reviewed), `analyst_gaps_json` (verbatim `gaps[]` array from the analyst's outcome payload; each entry carries at minimum `location`, `severity`, and `detail` or `missing_field`), `analyst_outcome_json` (verbatim outcome object including `outcome`, `summary`, and any narrative fields the analyst emitted), `schedule_path` (absolute path to the pending schedule — may not yet be persisted; if present, read-only; if absent, the triage adjudicates on plan text alone), `findings_count` (length of the gaps array).
  - Document the four-verdict output vocab `ship | ship-with-fixes | partial-agreement | needs-rework` with the same rubric D.5 uses (`dispatch-templates.md` lines 240-246), re-worded for plan adjudication. Verdict semantics at plan level, shared across both sources:
    - `ship` — disagree with the reviewer entirely; no item is load-bearing; proceed to the next phase with a bare `[plan-review-disagreement]` or `[analyst-triage-disagreement]` tag in the run summary (orchestrator picks the tag by source).
    - `ship-with-fixes` — disagree on blockers; residual concerns are minor notes. Proceed to the next phase with items carried to the summary's "Plan review notes" section.
    - `partial-agreement` — items split cleanly; at least one is load-bearing AND at least one can be safely dismissed. Dispatches `plan-author` on the load-bearing subset only; dismissed indices carried verbatim to the summary.
    - `needs-rework` — all items are load-bearing. Dispatches `plan-author` with the full array.
  - Document the **dismissal-evidence gate** verbatim from `dispatch-templates.md` line 247 (Phase D.5), re-scoped for plan-level. The two permitted dismissal justifications are (1) a concrete verification move against the plan text or schedule, or (2) an explicit spec contradiction (the plan's stated acceptance criteria or its Implementation Playbook mandates the behavior the reviewer objects to). Unverified dismissals MUST downgrade to `minor-findings`-style phrasing and the item still surfaces. Provide source-specific verification-move examples:
    - **Codex plan-review source:** "re-read the cited plan section and confirm Codex's finding misreads it"; "check the schedule's `tasks[]` for the claimed missing entry"; "verify the Acceptance criteria bullet Codex says is absent is actually present".
    - **Plan-analyst source:** "re-read the task block cited by `gaps[i].location` and confirm the `missing_field` is in fact present"; "check the plan's narrative Dependencies prose for an implicit reference the analyst missed"; "verify the `severity:'hard'` classification against what the orchestrator would actually halt on — soft gaps with `severity:'hard'` mis-tagging are dismissible".
  - Document a required **same-family caveat paragraph** on the analyst-source branch: the analyst is Claude/Opus and the triage is Claude/Sonnet. The dismissal-evidence gate is the primary mitigation — the triage MUST cite plan text or schedule evidence, not vibe-dismiss. An unverified dismissal on the analyst path is epistemically weaker than on the Codex path and MUST downgrade per the rule above. The paragraph should contain the literal phrase `same-family` so verification item 20 can grep for it.
  - Document the JSON output shape matching `codex_plan_review_triage_schema.json`: `{verdict, load_bearing, dismissed, summary}` for `partial-agreement`; `{verdict, load_bearing: [], dismissed: [], summary}` for the other three. The shape is identical across sources — the source discriminator lives in the dispatch template input and the orchestrator's run-log event, NOT in the triage's output.
  - Explicit hard rules: (a) no source-code reading (v1); (b) no Edit / Write / Agent tools; (c) no plan-file mutation (triage is read-only on the plan); (d) no schedule-file mutation; (e) word cap ≤500 on the rationale / summary sections; (f) `load_bearing` / `dismissed` indices MUST be into the input evidence array (Codex findings[] OR analyst gaps[]) — the triage must not fabricate indices or reference items it was not given.
  - The spec mentions the "You do NOT have the Agent tool" constraint verbatim, per existing agent spec convention.
- **Reversion guidance:** `git restore plugins/plan-executor/agents/plan-review-triage.md`

**Description:**
A fresh-conversation-context subagent spec that parallels `code-reviewer.md`'s shape but is plan-scoped AND dual-source. The key discipline is the dismissal-evidence gate: D.5's gate text translates directly — replace "trace the cited control-flow path" with "re-read the cited plan section" and "read the cited symbol in the repo" with "check the schedule's `tasks[]` for the claimed missing entry". The spec documents both sources as two labeled subsections sharing the same verdict rubric, dismissal gate, and output shape. The same-family caveat on the analyst-source path is explicit so operators reading the spec (and the triage agent itself, at dispatch time) understand the epistemic asymmetry between the two sources.

**Implementation notes:**
Use `plan-author.md` and `plan-remediator.md` as the shape templates — they're the closest existing agent specs to plan-scoped work. The dismissal-evidence gate text lives at `dispatch-templates.md:247-252` — copy verbatim and re-scope. Frontmatter `tools` list excludes `Edit`/`Write` because the triage is strictly read-only on the plan and schedule. Model choice is sonnet, matching D.5 — NOT opus, to keep cost parity with task-level review. The spec file itself is a single markdown file with the two input contracts as labeled subsections; do NOT split into two agent specs.

### TASK-003: Plan-triage dispatch template (source-parameterized)

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md
- **Dependencies:** TASK-002
- **Test command:** none (template documentation; exercised by TASK-004's integration)
- **Acceptance criteria:**
  - New section `## Phase 1-triage / Phase 1.5.5 — plan-review-triage dispatch (source-parameterized)` inserted between the existing `## Phase 1.5a — plan-author dispatch` and `## Phase B — plan-implementer dispatch` sections. The section heading explicitly names both insertion points so readers know one template serves two orchestrator seams.
  - Template body mirrors Phase D.5's body at `dispatch-templates.md:218-280`, re-scoped and parameterized by `{source}`:
    - Scope line: `plan_path`, `schedule_path`, `source ∈ {plan-analyst, codex-plan-review}`, `findings_count`.
    - Embed the plan text verbatim in a fenced ```markdown block (shared across sources).
    - **Source-discriminated evidence block** — one fenced ```json block labeled per source:
      - `source: codex-plan-review` → embed Codex `findings[]` verbatim under heading "Codex findings".
      - `source: plan-analyst` → embed analyst `gaps[]` verbatim under heading "Analyst gaps" AND the analyst's outcome narrative (summary + any other non-array fields) in a sibling block.
    - Reviewer summary block: Codex `summary` for Codex source; analyst outcome summary for analyst source. Same block, source-discriminated body.
    - Verdict decision rubric (the four-verdict ladder from TASK-002's agent spec) — shared, source-agnostic.
    - Dismissal-evidence gate — shared text with TWO appended source-specific verification-move examples subsections (the concrete moves lifted from TASK-002's spec).
    - Same-family caveat paragraph — included verbatim when `source=plan-analyst`, omitted when `source=codex-plan-review` (the two specs have different reviewer families, so the caveat only applies on the analyst path).
    - Output shape specification: same two JSON shapes as D.5 (`{verdict, summary}` for three verdicts; `{verdict, load_bearing, dismissed, summary}` for `partial-agreement`) — shared across sources.
  - The Agent dispatch header names `subagent_type: "plan-review-triage"` and `model: "sonnet"`, paralleling D.5's `code-reviewer` header.
  - Includes the "You do NOT have the Agent tool" closer verbatim, per template convention.
  - Explicit instruction that the triage is NOT permitted to dispatch subagents, open Edit/Write, or read source code in v1.
  - The template is a SINGLE section with source-discriminated body — do NOT create two parallel sections "Phase 1-triage" and "Phase 1.5.5" with duplicated rubric/gate text. Use a `{source}` placeholder and conditional blocks inside one section.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/dispatch-templates.md`

**Description:**
Insert a source-parameterized plan-triage template. Structurally it's a D.5 clone — the prompt body, verdict rubric, and dismissal-evidence gate are copy-adapted from `dispatch-templates.md:218-280`. The content changes are (a) the scope declaration (plan + schedule + source, not diff + task block), (b) source-discriminated evidence blocks, (c) source-specific verification-move examples on the dismissal gate, (d) the analyst-source same-family caveat, and (e) shared output shape.

**Implementation notes:**
Place the new section AFTER Phase 1.5a (plan-author) and BEFORE Phase B (plan-implementer). Maintain the file's ordering convention: numbered phase → alphabetic sub-phase. Insertion point: search for the `## Phase B — plan-implementer dispatch` heading and insert before it. Do NOT re-number any downstream sections — Phase D.5 retains its name. The dual-heading "Phase 1-triage / Phase 1.5.5" signals the single-template-two-seams design to readers; the orchestrator (TASK-004) calls the same render function with a `source` arg for either seam. Conditional blocks inside the template are rendered inline per source — prefer an `if {source} == "plan-analyst": ... else: ...` branch pattern over two parallel verbose sections, to keep the shared contract (verdict rubric, output shape) visibly shared.

### TASK-004: SKILL.md orchestrator wiring — dual insertion points + CLI flags + run-log events

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
- **Dependencies:** TASK-003
- **Test command:** none (orchestrator prose; exercised by TASK-006's integration harness)
- **Acceptance criteria:**
  - **Two new CLI flags** added to the `## Parse arguments` CLI surface (currently at `SKILL.md:102-120`):
    - `--codex-plan-review-binding` — docstring: "Codex `needs-replan` on plan review goes straight to halt; no plan-review-triage third-opinion escalation."
    - `--analyst-binding` — docstring: "Plan-analyst `needs-enrichment` goes straight to halt; no plan-review-triage third-opinion escalation. Does NOT affect `--allow-gaps` short-circuit."
  - Place both flags after the existing `--codex-review-binding` entry in the Optional flags list, paralleling the naming convention.
  - **Insertion point A — Phase 1 (analyst-source triage).** In `## Analysis (Phase 1)`, after the existing outcome-branching paragraph ("`needs-enrichment` + no `--allow-gaps` → halt..." at `SKILL.md:193-196`), insert a new subsection `### Phase 1-triage — plan-analyst triage (needs-enrichment third opinion)`. The subsection documents:
    - Fire conditions: `outcome=needs-enrichment` AND `--analyst-binding` NOT set AND `--allow-gaps` NOT set. (`--allow-gaps` remains a pre-triage short-circuit with today's behavior.)
    - Skip conditions: `--analyst-binding` → log `analyst_triage_skipped {reason:"binding_flag"}` and halt with `run_end reason=plan_analyst_failed`. `--allow-gaps` → log `analyst_triage_skipped {reason:"allow_gaps"}` and proceed with warning (today's demotion path).
    - Dispatch: `Agent(subagent_type: "plan-review-triage", model: "sonnet", prompt: render(templates.PlanTriage, source="plan-analyst", plan_text, gaps, analyst_outcome, schedule_path, findings_count))`.
    - Parse: `printf '%s' "<agent_output>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-plan-review-triage-report --stdin --source plan-analyst --findings-count <N> --json` where `<N>` is the length of the analyst's `gaps[]` from the prior `parse-schedule` call.
    - Route: `ship | ship-with-fixes` → proceed to Phase 1.5 (persist the schedule as-if the analyst returned `valid`; `ship-with-fixes` carries gaps to the summary); `partial-agreement` → dispatch `plan-author` on load-bearing gaps only (new enrichment path), then re-analyst, then proceed to Phase 1.5 with the binding re-analyst verdict; `needs-rework` → dispatch `plan-author` with the full gaps array (new enrichment path), then re-analyst, then proceed.
    - Run-log events: `plan_review_triage_start {run_id, plan_file, source:"plan-analyst", findings_count}` → `plan_review_triage_done {run_id, plan_file, source:"plan-analyst", verdict, load_bearing_count, dismissed_count, summary}`.
  - **Insertion point B — Phase 1.5 (Codex-review-source triage).** In `### Phase 1.5 — Codex plan review`, amend the `needs-replan` routing block (currently at `SKILL.md:275` in the verdict table + the `**needs-replan branch — auto-revise path**` prose) to insert a new sub-step `### Phase 1.5.5 — plan-review triage (needs-replan third opinion)` BEFORE the `plan-author` dispatch. This is the original plan's insertion, expanded from a single-source block to a source-parameterized block that shares template + parser with insertion point A. The subsection documents:
    - Fire conditions: Codex verdict `needs-replan` AND `--codex-plan-review-binding` NOT set AND `--no-auto-revise` NOT set.
    - Skip conditions: `--codex-plan-review-binding` → halt immediately with `run_end reason=plan_review_failed`. `--no-auto-revise` → halt immediately (today's behavior, unchanged).
    - Dispatch: `Agent(subagent_type: "plan-review-triage", model: "sonnet", prompt: render(templates.PlanTriage, source="codex-plan-review", plan_text, findings, codex_summary, schedule_path, findings_count))`.
    - Parse: `printf '%s' "<agent_output>" | $PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-plan-review-triage-report --stdin --source codex-plan-review --findings-count <N> --json` where `<N>` is the `findings_count` from the prior `parse-plan-review-report` call.
    - Route: `ship | ship-with-fixes` → proceed to Phase 2 (skip plan-author, skip re-analyst, skip re-review); `partial-agreement` → dispatch `plan-author` with findings filtered to the `load_bearing` indices AND carry `dismissed` findings into the run summary; `needs-rework` → dispatch `plan-author` with the full findings array.
    - Run-log events: `plan_review_triage_start {run_id, plan_file, source:"codex-plan-review", findings_count}` → `plan_review_triage_done {run_id, plan_file, source:"codex-plan-review", verdict, load_bearing_count, dismissed_count, summary}`.
  - **Run-log V8 ordering update** at `SKILL.md:317`. Add two source variants of the triage events interleaved in their respective branches:
    - Analyst branch: `analyst_done {outcome:"needs-enrichment"}` → (if triage fires) `plan_review_triage_start {source:"plan-analyst"}` → `plan_review_triage_done {source:"plan-analyst"}` → (branch by verdict) `schedule_written` | `plan_author_start`.
    - Codex branch: `plan_review_done {verdict:"needs-replan"}` → (if triage fires) `plan_review_triage_start {source:"codex-plan-review"}` → `plan_review_triage_done {source:"codex-plan-review"}` → (branch by verdict) `batch_start` | `plan_author_start`.
  - **New `## Rules` entry** documenting the "re-source-verdict is binding after triage + plan-author" invariant: after a `partial-agreement` or `needs-rework` triage verdict, the `plan-author` → re-source-verdict (re-analyst for analyst source, re-Codex-review for Codex source) sequence is run ONCE and that second source verdict is binding. A second `needs-enrichment` on the re-analyst OR a second `needs-replan` on the re-Codex-review halts per the existing `run_end reason=plan_analyst_failed` / `reason=plan_review_failed` paths; NO second triage is dispatched for either source.
  - **Summary carryover documented** per source:
    - Codex-review source: existing "Plan review notes" section receives Codex findings verbatim for `ship-with-fixes`; dismissed-finding indices called out for `partial-agreement`; `[plan-review-disagreement]` banner for `ship`.
    - Analyst source: a new "Analyst triage notes" section in the end-of-run summary receives analyst gaps verbatim for `ship-with-fixes`; dismissed-gap indices called out for `partial-agreement`; `[analyst-triage-disagreement]` banner for `ship`.
  - **`--allow-gaps` semantics preserved** explicitly in SKILL.md. A new paragraph in `## Analysis (Phase 1)` documents: `--allow-gaps` remains a pre-triage short-circuit on the analyst path; it bypasses analyst triage entirely and proceeds with today's soft-severity demotion behavior. Operators who want to use triage instead of `--allow-gaps` simply omit the flag — triage is the default when `outcome=needs-enrichment` and neither `--analyst-binding` nor `--allow-gaps` is set.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md`

**Description:**
The orchestrator wiring task, expanded from the original single-source plan to cover two insertion points sharing TASK-001's parser + TASK-002's agent spec + TASK-003's template. SKILL.md is the operational specification of `/implement-plan`, so this task amends Phase 1 and Phase 1.5 routing prose, the CLI flag list, the run-log event vocabulary, and the `## Rules` section. No Python or schema changes — TASK-001 already shipped the parser + schema with `--source` flag, TASK-002 shipped the dual-source agent spec, TASK-003 shipped the source-parameterized dispatch template. TASK-004 connects them at both seams.

**Implementation notes:**
The two insertion points share the triage machinery. Keep the SKILL.md prose DRY where possible — the verdict-routing language ("`ship | ship-with-fixes` → proceed; `partial-agreement` → ...") is identical across sources; consider a shared paragraph referenced by both subsections. The CLI flag entries (`--codex-plan-review-binding` / `--analyst-binding`) are parallel in structure and should be documented adjacently. The V8 run-log ordering update at `SKILL.md:317` needs both branches documented — don't skip the analyst branch because the original plan focused on Codex. The `## Rules` entry should name both sources explicitly so the invariant is unambiguous.

### TASK-005: Regression harness for the triage contract (both sources)

- **Status:** pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "plan_review_triage_contract" -x`
- **Acceptance criteria:**
  - A new test class `TestPlanReviewTriageContract` in `tests/scripts/test_plan_ops.py` exercises the parser → routing contract without requiring live orchestrator execution. All test methods are parameterized over `source ∈ {"plan-analyst", "codex-plan-review"}`:
    - Build four synthetic triage envelopes (one per verdict) per source; parse each via `parse-plan-review-triage-report --source <src> --findings-count 4`; assert the extracted `{verdict, load_bearing, dismissed, summary, source}` values. The `source` field on output MUST echo the input flag verbatim.
    - Build a synthetic `partial-agreement` envelope per source where `load_bearing=[0, 2]` and `dismissed=[1, 3]` for an evidence array of length 4; assert parser output round-trips.
    - Assert the four invariants of `partial-agreement` (empty bucket, duplicate index, out-of-range index, overlap) all surface as parser errors with the documented `code` values from TASK-001, for both sources.
    - Source-aware error messaging: the out-of-range test asserts the message contains "gap index" when `--source plan-analyst` and "finding index" when `--source codex-plan-review`.
    - Source-flag semantics: one test asserts omitting `--source` rejects with `triage-source-missing`; another asserts an unknown `--source` value (e.g. `--source foobar`) rejects with `triage-source-unknown`.
  - The harness uses the same fixture-factory pattern as the existing `_plan_review_envelope` helper at `test_plan_ops.py:6827` — build a helper `_plan_review_triage_envelope(verdict, load_bearing=None, dismissed=None, summary="...")` that is source-agnostic (the envelope body doesn't carry `source`; only the CLI flag does) and parameterize test methods over the verdict × source matrix.
  - No subprocess dispatch of a live triage agent; the tests validate the parser + schema contract only. Orchestrator routing (two insertion points) is validated at spec level via the verification items in this plan's `## Verification` section — the SKILL.md audit run (`plan_ops.py audit --json`) catches drift between the SKILL.md routing documentation and the parser contract for both sources.
- **Reversion guidance:** `git restore tests/scripts/test_plan_ops.py`

**Description:**
Defense-in-depth beyond TASK-001's unit tests. TASK-001 tests the parser in isolation (per-source); TASK-005 tests the parser's output against the routing contract the orchestrator consumes at BOTH insertion points. If TASK-004's SKILL.md routing ever disagrees with TASK-001's parser output shape — for either source — this harness is where the drift surfaces before a live run hits it.

**Implementation notes:**
The `_plan_review_envelope` helper at `test_plan_ops.py:6827` is the shape to mirror. Use the existing CLI-subprocess pattern (`self._run_parser(envelope)`) — live subprocess-invocations of `plan_ops.py` are how the existing plan-review parser tests are structured, and they catch CLI wiring regressions that pure-Python unit tests would miss. No triage subagent dispatch in the test — construct the markdown-report-with-embedded-JSON payload directly via a helper. Use `@pytest.mark.parametrize("source", ["plan-analyst", "codex-plan-review"])` to parameterize each test method over the two sources; the envelope body is shared (source-agnostic schema output), only the CLI flag differs.

### TASK-006: Integration harness for Phase 1-triage / Phase 1.5.5 orchestrator wiring

- **Status:** pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "plan_review_triage_integration" -x`
- **Acceptance criteria:**
  - A new test class `TestPlanReviewTriageIntegration` in `tests/scripts/test_plan_ops.py` exercises the end-to-end orchestrator wiring landed in TASK-004 for BOTH insertion points (Phase 1-triage on the analyst path, Phase 1.5.5 on the Codex-plan-review path) without requiring a live subagent dispatch.
  - For each source `∈ {plan-analyst, codex-plan-review}` and each verdict `∈ {ship, ship-with-fixes, partial-agreement, needs-rework}`, the harness drives a synthetic triage-report envelope through the parser and asserts the routing decision the orchestrator would take matches the SKILL.md prose from TASK-004 (proceed vs plan-author dispatch vs halt).
  - Exercise the CLI flag matrix: `--analyst-binding` short-circuits the analyst path with `run_end reason=plan_analyst_failed`; `--codex-plan-review-binding` short-circuits the Codex path with `run_end reason=plan_review_failed`; `--allow-gaps` pre-triage short-circuit on the analyst path preserved; `--no-auto-revise` preserved on the Codex path. Each flag assertion is a dedicated test method.
  - Run-log event ordering assertions: for each source, assert the documented `plan_review_triage_start` → `plan_review_triage_done` event pair is emitted with the correct `source` field and that the downstream event (`schedule_written` | `plan_author_start` for analyst; `batch_start` | `plan_author_start` for Codex) follows per-verdict.
  - Summary carryover assertions: `ship` → bare `[plan-review-disagreement]` (Codex) / `[analyst-triage-disagreement]` (analyst) banner; `ship-with-fixes` → items verbatim in "Plan review notes" (Codex) / "Analyst triage notes" (analyst); `partial-agreement` → dismissed indices listed.
  - No live subagent dispatch and no live orchestrator subprocess — stub the Agent dispatch at the same seam the existing integration tests use, and validate the parser → routing contract via the synthesized run-log + summary payloads.
- **Reversion guidance:** `git restore tests/scripts/test_plan_ops.py`

**Description:**
The deferred-testing target for TASK-002's agent spec, TASK-003's dispatch template, and TASK-004's SKILL.md orchestrator wiring. TASK-005 covers the parser contract in isolation (per source); TASK-006 covers the orchestrator's consumption of that contract at both insertion points end-to-end. Until this harness lands, the orchestrator wiring in TASK-004 is prose-only; TASK-006 is where the SKILL.md routing documentation is validated against actual orchestrator behavior for both sources.

**Implementation notes:**
Mirror the fixture-factory pattern from TASK-005's `_plan_review_triage_envelope` helper. The routing-decision assertion seam is wherever the existing plan-review integration tests (if any) stub the Agent dispatch today — follow that convention rather than inventing a new harness shape. Parameterize aggressively: source × verdict × flag combinations are a small Cartesian product and each cell is a one-liner assertion once the fixtures are in place.

## Expected outcome

After this plan is executed, both of today's pedantic-flag halts on plan-stage outcomes get adjudicated by a shared triage subagent:

- **Analyst `needs-enrichment` (Phase 1):** no longer automatically halts or demotes. A `plan-review-triage` subagent (source=`plan-analyst`) adjudicates the analyst's gaps against the plan. Run proceeds directly to Phase 1.5 when the triage judges the gaps non-blocking; `plan-author` fires with a filtered or full gaps payload on the `partial-agreement` / `needs-rework` paths (new enrichment capability); and `--allow-gaps` remains available as a pre-triage short-circuit for operators who want to bypass triage entirely.
- **Codex `needs-replan` (Phase 1.5):** no longer automatically dispatches a plan-prose rewrite. The same `plan-review-triage` subagent (source=`codex-plan-review`) adjudicates Codex's findings. Run proceeds to Phase 2 when the triage judges Codex wrong — the common case for the empirical ~39% `needs-replan` rate; a filtered `plan-author` dispatch handles the genuine partial-agreement case; the full plan-author dispatch fires only when the triage concurs with Codex across the board.

Both paths share one agent spec, one schema, one parser (parameterized by `--source`), and one dispatch template. Mirrors the Phase D.5 mechanism that task-level review already uses successfully, adapted for two plan-stage insertion points.

**Future work (v2, not in this plan):** `--triage-model {claude|gemini}` flag once `PLAN_GEMINI_INTEGRATION_2026-04-21.md` is shipped. The Gemini Investigator role's codebase-investigation tools would (a) widen the triage evidence gate beyond plan-text cross-reference into actual source-code verification, strengthening the dismissal-evidence rule, and (b) close the same-family caveat on the analyst-source path (Claude analyst / Gemini triage is cross-family; today's Claude analyst / Claude triage is not).
