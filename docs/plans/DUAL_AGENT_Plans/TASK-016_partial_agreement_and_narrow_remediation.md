# TASK-016 — Partial-agreement verdict and narrow remediation retry

**Design contract:** [`TASK-014_remediation_and_plan_review.md`](TASK-014_remediation_and_plan_review.md) §TASK-014A — this plan extends the D.2a.5 shape with a parallel, narrower path.
**Base branch:** `main`
**Audit anchor commit:** `9eca996`
**Chunk dependencies:** TASK-001 (canonical contracts), TASK-002 (runtime validation helpers), TASK-014A (D.2a.5 bounded remediation — this plan parallels its shape).
**Motivation:** The D.5 verdict set `{ship, ship-with-fixes, needs-rework}` has no name for the "Codex is right about 1-of-4 findings" split. Today the orchestrator must promote that case to full `needs-rework`, which re-dispatches `plan-implementer` with all Codex findings forwarded via Phase B-rework — over-forwarding findings D.5 already deemed irrelevant. This plan gives the split case a first-class verdict (`partial-agreement`), a narrower retry path (D.2a.6), and a dedicated subagent (`plan-remediator`) so the narrow fix is auditable and scope-bounded by construction.

---

## Goal

Three tightly scoped changes so narrow D.5 adjudications stop forcing an all-or-nothing retry:

1. **D.5 `partial-agreement` verdict** with structured payload `{load_bearing:[idx...], dismissed:[idx...]}` referring to 0-based indices of the Codex `parsed.findings[]` array.
2. **Phase D.2a.6 narrow-remediation retry** parallel to D.2a.5. Fires when D.5 returns `partial-agreement`; dispatches the new `plan-remediator` subagent with **only** the load-bearing finding subset; captures dismissed indices in a `[disagreement: i,j]` commit trailer; re-runs D.1 (Codex review) as binding. One attempt. Second-failure pause matches D.2a.5.
3. **`plan-remediator` subagent** (new file `plugins/plan-executor/agents/plan-remediator.md`) — dedicated role with strict "touch only these `file:line` locations" scope, a mandatory **Dismissed findings noted** report section, and a `scope-violation` outcome for edits outside the load-bearing finding scope.

Subtasks A and B may land in the same batch; C depends on both.

---

## Scoped Context

### Why now

- Run `20260417T153553` (cited in TASK-014A motivation, TASK-014 line 7) showed the cost of the all-or-nothing retry even after D.2a.5 landed: when 1 of 4 Codex findings is load-bearing, the implementer still receives all 4 findings in the Phase B-rework payload (dispatch-templates.md:191-201), and "fix narrowly, do not scope-inflate" (dispatch-templates.md:209) is a prompt-time hint rather than a structural bound.
- TASK-015 is tuning the Codex reviewer prompt so fewer findings escalate to `needs-rework`; TASK-016 is the complementary downstream fix — when escalation does happen and D.5 disagrees with only some findings, the retry is scoped to the real problems only.
- A dedicated subagent role (not "plan-implementer wearing a narrower hat") makes the retry visible in the run log as a distinct dispatch and enforces the scope rule via its system prompt rather than a single prompt line.

### Existing surfaces we extend (not rebuild)

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — orchestrator protocol. Extend the D.2a route table (SKILL.md:328-331); insert §D.2a.6 immediately after §D.2a.5 (SKILL.md:335-369).
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — update the D.5 template's verdict enum and output shape (dispatch-templates.md:155); add a new "Phase B-narrow-remediation" template parallel to "Phase B-rework" (dispatch-templates.md:169-216).
- `plugins/plan-executor/scripts/plan_ops.py` — add `partial-agreement` to `ALLOWED_CLAUDE_REVIEW_VERDICTS` (plan_ops.py:84); extend the D.5 adjudication parser to accept `{load_bearing, dismissed}`; add `commit-task --narrow-remediation-tag` and `--dismissed-finding-ids=i,j` flags mirroring the existing `--remediation-tag` shape (plan_ops.py:2087-2092, 2479-2484); extend the log-event allow-list (plan_ops.py:~114) with `narrow_remediation_start` and `narrow_remediation_done`; accept two new `awaiting_user` stage labels (`post_narrow_remediation_review`, `post_narrow_remediation_implement`).
- `plugins/plan-executor/agents/plan-remediator.md` — NEW file. Mirror plan-implementer.md's frontmatter and prompt skeleton but add the touch-only-these-lines scope rule, the `Scope violations` report section, the `Dismissed findings noted` section, and the `scope-violation` outcome.

### Non-goals

- Not modifying D.2a.5 or its Phase B-rework template. Full-rework retry remains the path for D.5 verdict `needs-rework`.
- Not introducing a second retry after a failed D.2a.6. One attempt, same bounding as D.2a.5 (SKILL.md:337).
- Not changing `--codex-review-binding` semantics. Binding mode continues to skip all of D.2a — no D.5, no D.2a.5, no D.2a.6 (SKILL.md:333).
- Not extending the `codex_review_schema.json` findings shape. D.5 references Codex findings by **0-based array index** into `parsed.findings[]`; no new `id` field on Codex's envelope. This keeps the Codex wrapper unchanged.
- Not implementing per-finding severity tiering in the split decision. D.5 emits `partial-agreement` whenever the split is non-empty on both sides; the orchestrator doesn't ask it to rank further.
- Not auto-filing dismissed findings into a tracker. They appear in the `[disagreement: ids]` trailer and the run log; any downstream triage is out of scope.

---

## Verification

### Subtask-A (partial-agreement verdict) — V1–V3

**V1 — D.5 envelope with `partial-agreement` is accepted and routed.**
D.5 emits:
```json
{"verdict":"partial-agreement","load_bearing":[0,2],"dismissed":[1,3],"summary":"..."}
```
where indices are 0-based into Codex's `parsed.findings[]` (length 4). Parser MUST accept, validate that `load_bearing` and `dismissed` are disjoint non-empty integer arrays whose union is a subset of `range(len(codex_findings))`, and return a structured dispatch payload to the orchestrator. Assert via synthetic envelope fixtures in `tests/scripts/test_plan_ops.py`.

**V2 — Empty or overlapping split is a structured error.**
`partial-agreement` with `load_bearing=[]` OR `dismissed=[]` OR `set(load_bearing) & set(dismissed) != set()` MUST exit with `errors[0].code == "partial-agreement-invalid-split"` and a human-readable message that names the failing condition. Empty lists collapse to `needs-rework` / `ship-with-fixes`; the reviewer should have chosen those verdicts instead.

**V3 — Out-of-range index is a structured error.**
Any index in `load_bearing ∪ dismissed` that is `< 0` or `>= len(codex_findings)` MUST exit with `errors[0].code == "partial-agreement-unknown-index"`. Prevents D.5 from hallucinating findings.

### Subtask-B (plan-remediator subagent) — V4–V5

**V4 — Agent file is registered and discoverable.**
`plugins/plan-executor/agents/plan-remediator.md` exists with frontmatter `name: plan-remediator`, `tools: Read, Grep, Glob, Edit, Write, Bash` (no Agent — matches plan-implementer.md), `model: opus`. Plugin manifest loads without error; `Agent(subagent_type: "plan-remediator")` resolves.

**V5 — System prompt enforces the touch-only-these-lines scope.**
The agent spec MUST include, at minimum:
- The "You do NOT have the Agent tool" constraint (mirrors plan-implementer.md line 8 and plan-analyst.md).
- An **Inputs** contract listing `load_bearing_findings[]` (each entry has `index`, `file`, `line`, `issue`, `suggested_fix`), `dismissed_findings[]` (context-only, explicitly labeled "do NOT fix"), `d5_summary`, the full `TASK-NNN` block, and the `## Context` section — all of which the Phase B-narrow-remediation dispatch template provides.
- A **Process** rule: the union of `(file, line)` scopes across `load_bearing_findings[]` defines the permitted edit region. Edits outside that region MUST appear in a `**Scope violations:**` report section with a justification. Unjustified scope violations → outcome `scope-violation`.
- A **Dismissed findings noted** report section echoing each dismissed finding's index and a one-line ack that the remediator read but did NOT act on it. Prevents silent drift from the D.5 split.
- Outcome vocabulary `success | partial | failed | plan-incorrect | blocked | scope-violation` (adds `scope-violation` relative to plan-implementer's set).
- Parallel-tree caveat (mirrors dispatch-templates.md:128-129 / :157).

### Subtask-C (D.2a.6 narrow-remediation path) — V6–V11

**V6 — Dispatch happens via the Agent tool, logged as `role=plan-remediator`.**
On D.2a.6 entry, the run-log records `narrow_remediation_start {task_id, load_bearing_count, dismissed_count, d5_summary}` followed by the standard `implement_start {role:"plan-remediator", ...}` event emitted by the subagent-dispatch instrumentation, then `narrow_remediation_done {task_id, outcome}`. Asserted via a synthetic orchestrator trace in `test_plan_ops.py`.

**V7 — Route table extension is unambiguous.**

| Codex verdict | D.5 verdict | Route |
|---|---|---|
| `needs-rework` | `ship` \| `ship-with-fixes` | D.3 with `--disagreement-tag` (unchanged) |
| `needs-rework` | `needs-rework` | D.2a.5 (unchanged) |
| `needs-rework` | `partial-agreement` | **D.2a.6** narrow-remediation |

**V8 — D.2a.6 dispatches plan-remediator with the load-bearing subset only.**
The Phase B-narrow-remediation template embeds `load_bearing_findings_json` (filtered subset of `codex_findings` where array index ∈ `load_bearing`) and `dismissed_findings_json` (the complement, labeled "DO NOT fix — context only"). `d5_summary` is forwarded. One attempt.

**V9 — Re-review after successful remediation is binding.**
On retry success, re-run D.1 (Codex review) on the remediator's changes. `clean | minor-findings` → D.3 commit with `--narrow-remediation-tag --dismissed-finding-ids <comma-separated-indices>`. `needs-rework` (second failure) → awaiting-user pause with `stage:"post_narrow_remediation_review"`.

**V10 — Commit trailers.**
Commit body for a successful narrow-remediation retry MUST include, on their own lines, in this order:
```
[narrow-remediation]
[disagreement: 1,3]
```
where `1,3` are the dismissed indices from D.5. Run summary row shows both tags. The following argparse-level constraints MUST all hold: (a) `--narrow-remediation-tag` and `--remediation-tag` are mutually exclusive (a commit cannot be both a full and a narrow remediation); (b) `--dismissed-finding-ids` and `--disagreement-tag` are mutually exclusive (a commit cannot be both "D.5 disagrees with all Codex findings" and "D.5 disagrees with a subset"); (c) `--dismissed-finding-ids` requires `--narrow-remediation-tag`; (d) `--narrow-remediation-tag` requires non-empty `--dismissed-finding-ids`. The pre-existing bare `[disagreement]` trailer (no indices) remains the form used for D.5 verdict `ship | ship-with-fixes`; the bracketed-with-ids form only appears when `dismissed` is a proper subset of Codex findings (i.e., D.5 verdict `partial-agreement`).

**V11 — Second-failure and retry-implement failure paths.**
- Second Codex `needs-rework` on the re-review → `awaiting_user {stage:"post_narrow_remediation_review", codex_findings:[...], d5_summary:"...", dismissed_finding_indices:[...], dirty_files:[...]}` then `finalize-execution-log --outcome paused`. No `fail-task`.
- plan-remediator returns outcome ≠ success (failed, plan-incorrect, blocked, scope-violation) → `awaiting_user {stage:"post_narrow_remediation_implement", retry_outcome, diagnostics, reversion_guidance, dirty_files:[...]}` then `finalize-execution-log --outcome paused`. No `fail-task`. Mirrors D.2a.5 step 6 retry-implement branch (SKILL.md:346-369).
- Binding mode (`--codex-review-binding`) continues to skip D.2a entirely — D.2a.6 is not entered (SKILL.md:333).

---

## Tasks

### TASK-016A: D.5 partial-agreement verdict + payload schema

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-001, TASK-002, TASK-014A
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1–V3 pass.
  - `ALLOWED_CLAUDE_REVIEW_VERDICTS` (plan_ops.py:84) contains `partial-agreement` alongside the existing three.
  - D.5 envelope parser extension accepts `{load_bearing:[int...], dismissed:[int...], summary:str}` payload; emits structured `errors[*]` with codes `partial-agreement-invalid-split` and `partial-agreement-unknown-index` for malformed splits.
  - dispatch-templates.md Phase D.5 template (line 155) verdict enum is updated to `ship | ship-with-fixes | partial-agreement | needs-rework`, with a short decision rubric telling the reviewer to choose `partial-agreement` only when the findings split cleanly (≥1 load-bearing AND ≥1 dismissed) and the output shape example shows the `load_bearing` / `dismissed` arrays.
  - SKILL.md §D.2a route table (line 328-331) gains the `partial-agreement` row pointing to §D.2a.6 (forward-reference OK; §D.2a.6 body lands in TASK-016C). **This is the sole owner of the route-table row; TASK-016C verifies its presence but does NOT re-add it.**

**Description:**
Extend the D.5 adjudication layer with a `partial-agreement` verdict that carries a disjoint split of Codex finding indices into load-bearing vs dismissed buckets. Parser validation rejects empty buckets, overlapping buckets, and out-of-range indices with distinct structured error codes. Template and route-table text are updated so downstream work (TASK-016C) has a named hook to attach to.

**Implementation notes:**
- Finding references use 0-based integer indices into `codex_findings = envelope.parsed.findings`, not string IDs. Codex's review schema has no `id` field (codex_review_schema.json has severity/file/line/issue/suggested_fix); adding one would be a wrapper-layer breaking change and is out of scope.
- The D.5 prompt rubric should explicitly instruct the reviewer: "Emit `partial-agreement` only when at least one finding is load-bearing AND at least one finding can be safely dismissed. If all are load-bearing → `needs-rework`. If none are → `ship-with-fixes`. Never both emit the verdict AND a unanimous split."
- Keep the forwarded payload structured (indices, not free-form text) so TASK-016C's template can filter Codex findings deterministically.

**Reversion guidance:**
Safe to revert in isolation; remove `partial-agreement` from `ALLOWED_CLAUDE_REVIEW_VERDICTS`, drop the parser extension, revert the D.5 template enum and the SKILL.md route-table row. The forward-referenced §D.2a.6 row becomes dead text until TASK-016C lands (or gets reverted alongside).

---

### TASK-016B: plan-remediator subagent

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/agents/plan-remediator.md` (create)
- **Dependencies:** TASK-001
- **Test command:** `none` (pure agent spec; end-to-end exercise lands in TASK-016C)
- **Acceptance criteria:**
  - V4–V5 pass.
  - Frontmatter: `name: plan-remediator`; `description` names the strict touch-only-these-lines contract; `tools: Read, Grep, Glob, Edit, Write, Bash` (no Agent); `model: opus`.
  - System prompt sections match the plan-implementer.md template structure (Inputs / Process / Report format / Status vocabulary / Rules) with the additions required by V5: Scope rule, Dismissed findings noted section, `scope-violation` outcome.
  - Report format specifies a `**Scope violations:**` section (default `None`) and a `**Dismissed findings noted:**` section (default echoes every dismissed index). Both are parsed as bulleted lists, matching plan-implementer.md's conventions.
  - Word cap ≤500 words across narrative sections (same as plan-implementer.md).

**Description:**
Add a dedicated subagent for the D.2a.6 narrow-remediation retry. The agent receives the `TASK-NNN` block, plan context, load-bearing finding subset, dismissed finding subset (context-only), and D.5 summary. It edits only within the `(file, line)` union of the load-bearing findings, writes the two mandatory report sections, and emits a standard plan-implementer-shaped report plus the new `scope-violation` outcome when it exceeds scope without justification.

**Implementation notes:**
- Mirror plan-implementer.md's existing Step 1–5 process (Read context / Apply change / Run test / Self-check / Report) but tighten Step 2 with the file:line scope rule and add a Step 2.5 "Acknowledge dismissed findings".
- Test command handling mirrors plan-implementer.md verbatim — re-run once on flake, classify pre-existing vs caused failures, record `Test outcome: passed|failed|not-run|pre-existing-failure`.
- The agent MUST NOT modify the plan file (same constraint as plan-implementer.md and plan-analyst.md).

**Reversion guidance:**
Safe to revert; delete the file. Nothing else references `plan-remediator` until TASK-016C lands.

---

### TASK-016C: D.2a.6 narrow-remediation retry path + commit trailers

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-016A, TASK-016B, TASK-014A
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V6–V11 pass.
  - SKILL.md grows a new §D.2a.6 section parallel to §D.2a.5 (insert after SKILL.md:369) with: one-attempt bounding, route on retry success (re-run D.1, binding), awaiting-user pauses for both the retry-implement-failure and second-review-failure branches with distinct `stage` labels, and explicit binding-mode exemption.
  - SKILL.md D.2a route table `partial-agreement → D.2a.6` row is present (added by TASK-016A — this task verifies presence only and does NOT re-edit that row).
  - dispatch-templates.md gains a "Phase B-narrow-remediation" template parallel to "Phase B-rework" (after line 216). Template dispatches `Agent(subagent_type: "plan-remediator", model: "opus")` and embeds `load_bearing_findings_json`, `dismissed_findings_json` (labeled "DO NOT fix — context only"), and `d5_summary`. "Fix narrowly, do not scope-inflate" guidance is retained; the file:line touch-only scope is the structural enforcement.
  - `commit-task` accepts `--narrow-remediation-tag` (boolean) and `--dismissed-finding-ids I,J,K` (comma-separated integers). When both are set, the commit body appends `[narrow-remediation]\n[disagreement: I,J,K]` on their own lines, adjacent, narrow-remediation tag first. Argparse-level constraints (all enforced with argparse errors, tested): (a) `--narrow-remediation-tag` XOR `--remediation-tag`; (b) `--dismissed-finding-ids` XOR `--disagreement-tag`; (c) `--dismissed-finding-ids` requires `--narrow-remediation-tag`; (d) `--narrow-remediation-tag` requires non-empty `--dismissed-finding-ids`.
  - `log-event` allow-list extended with `narrow_remediation_start` and `narrow_remediation_done`.
  - `awaiting_user` event accepts `stage:"post_narrow_remediation_review"` and `stage:"post_narrow_remediation_implement"` payloads (same shape validation as D.2a.5's two stages).
  - `--codex-review-binding` continues to skip D.2a in its entirety — D.2a.6 is NOT entered; no test regressions against the existing binding-mode fixtures.

**Description:**
Wire the D.2a.6 path end-to-end: orchestrator receives a `partial-agreement` verdict from D.5, emits `narrow_remediation_start`, dispatches `plan-remediator` with the filtered finding subset via the new Phase B-narrow-remediation template, classifies the retry with the standard plan-implementer rules plus the new `scope-violation` outcome, re-runs D.1 (Codex review) as binding on success, commits with `[narrow-remediation]` + `[disagreement: indices]` trailers on `clean|minor-findings`, and pauses for user decision on any failure branch. Mirrors D.2a.5's shape one-to-one with the scope-narrowing tweak.

**Implementation notes:**
- `narrow_remediation_start` and `narrow_remediation_done` are distinct from D.2a.5's `remediation_start` / `remediation_done`. The run log thereby becomes the audit source of truth for which retry path fired.
- Mutual exclusion between `--narrow-remediation-tag` and `--remediation-tag` is enforced in argparse (an `add_mutually_exclusive_group`), not at the log-event layer — catches operator error on the CLI rather than after a commit is written.
- The `[disagreement: i,j]` trailer form extends the bare `[disagreement]` used by D.5 `ship | ship-with-fixes`. The bare form stays as-is; the with-indices form only appears when `dismissed` is a proper subset of Codex findings (i.e., the `partial-agreement` path). `commit-task`'s existing `--disagreement-tag` flag (plan_ops.py:2180) emits the bare form; the new `--dismissed-finding-ids` flag emits the with-indices form. They are mutually exclusive (another argparse group) — a commit cannot be both "D.5 disagrees with all Codex findings" and "D.5 disagrees with a subset".
- plan-remediator runs under the same parallel-tree caveat as Phase D.5 (dispatch-templates.md:157) — working-tree contamination from batch-mates is suppressed by the file:line scope rule.
- The awaiting-user pause payload reuses the D.2a.5 shape (SKILL.md:346-369), adding `dismissed_finding_indices` to the `post_narrow_remediation_review` stage for round-tripping and flipping the `stage` label on both branches.

**Reversion guidance:**
Safe to revert; remove §D.2a.6 from SKILL.md, delete the Phase B-narrow-remediation template, drop `--narrow-remediation-tag` and `--dismissed-finding-ids` from `commit-task`, remove `narrow_remediation_start` / `narrow_remediation_done` from the log-event allow-list, remove the two new `awaiting_user` stage labels, remove the `scope-violation` outcome handler. Reverts restore the post-014A routing: D.5 `ship | ship-with-fixes` → D.3, D.5 `needs-rework` → D.2a.5, D.5 `partial-agreement` becomes unreachable dead code (cleaned up in TASK-016A's reversion).

---

## Out of Scope

- **D.2b narrow remediation.** The role-swap retry (Codex-implements + Claude-reviewer-`needs-rework`) keeps its v1 "no feedback forwarding" stance (SKILL.md:371-378, dispatch-templates.md:163-167). A narrow-D.2b variant can be a follow-up if warranted.
- **Per-finding severity tiering in D.5's split decision.** D.5 emits `partial-agreement` whenever `load_bearing` and `dismissed` are both non-empty; the orchestrator doesn't ask D.5 to rank findings further.
- **A CLI flag to disable the partial-agreement route.** A user who wants all non-unanimous D.5 verdicts to take the full-rework path can calibrate the D.5 reviewer via prompt tuning (TASK-015's domain). No new flag.
- **Automated dismissed-finding triage.** Dismissed indices land in the commit trailer and the run log; any tracker integration is out of scope.
- **Extending the `codex_review_schema.json` envelope with `id` fields.** Finding references use 0-based array indices; no change to the Codex wrapper.

## Reversion guidance

Three subtasks are independently reversible. B has no runtime surface until C wires it up (safe to ship alone; no-op until dispatched). A adds a verdict the router doesn't route (until C) — so A + B can land without C, and C depends on both. Revert order if needed: C → A → B (reverse of dependency order; least-dependency last).
