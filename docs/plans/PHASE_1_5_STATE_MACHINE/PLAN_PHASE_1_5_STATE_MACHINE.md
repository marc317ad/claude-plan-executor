# Plan: Phase 1.5 State Machine + Sanitizer Extension

**Created:** 2026-04-28
**Status:** Pending
**Base branch:** main
**Related:**
- `plugins/plan-executor/skills/implement-plan/SKILL.md` (Phase 1.5 / Phase 1.5.5 sections)
- `plugins/plan-executor/scripts/plan_ops.py` (`parse-plan-review-report`, `parse-plan-review-triage-report`, `review-route`)
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` (`plan-review` subcommand)
- `plugins/plan-executor/scripts/_codex_envelope_sanitizer.py` (FINDING_FIELDS / SCALAR_FIELDS coverage)
- `plugins/plan-executor/agents/plan-reviewer.md`, `plan-review-triage.md`, `plan-author.md`
- `plugins/plan-executor/scripts/codex_plan_review_schema.json`, `codex_plan_review_triage_schema.json`
- `docs/plans/PHASE_D_STATE_MACHINE/PLAN_PHASE_D_STATE_MACHINE.md` (sister plan; this picks up its non-goal #2)

---

## Goal

Move Phase 1.5 (plan-review) and Phase 1.5.5 (plan-review-triage) routing out of the `implement-plan` SKILL prompt into `plan_ops.py`, mirroring the move PHASE_D_STATE_MACHINE applied to Phase D. Persist Phase 1.5 cross-turn state on disk in `.schedule.json`. Extend the existing wrapper sanitizer perimeter to cover the plan-review envelope's free-text fields. Trim the Phase 1.5 / Phase 1.5.5 SKILL sections to match the reduced orchestrator role and codify the responsibilities the LLM retains. No behavioral regressions on happy path, on the `--allow-gaps` demotion path, on the `--codex-plan-review-binding` halt path, or on the second-`needs-replan` halt path.

## Context

The Phase 1.5 routing tables in SKILL.md (lines 392–472) ask the orchestrator LLM to maintain three pieces of cross-turn state and traverse a four-layer ladder:

1. **Pre-dispatch route-switch** by `claude_only` (Claude `plan-reviewer` Agent vs Codex `plan_codex_dispatch.py plan-review` shell-out).
2. **Post-verdict routing** on `approved | approved-with-notes | needs-replan`, with three skip/binding gates that demote or halt: `--skip-plan-review`, `--codex-plan-review-binding`, `--no-auto-revise`.
3. **Phase 1.5.5 triage routing** on `ship | ship-with-fixes | partial-agreement | needs-rework`, with the per-finding `plan-author` fan-out shape (`target_task_id`-aware Variant A vs Variant B) selected by triage payload.
4. **Second-pass binding** — the re-run Codex `plan-review` verdict after `plan-author → re-analyst` is binding; no second triage is dispatched.

The orchestrator currently tracks `auto_revise_round_completed`, the filtered-vs-full findings payload chosen by triage, the dismissed-indices list (for the run summary), and the `skipped_reason` choice between `flag | codex_unavailable | claude_review_failure` — all in conversation context. LLMs drift across long turns, especially on second-pass boolean state. The routing is deterministic given the parsed envelopes the existing `parse-plan-review-report` and `parse-plan-review-triage-report` subcommands already emit; it belongs in Python.

The wrapper sanitizer landed in PHASE_D TASK-003 covers `parsed.summary`, `parsed.diff_summary`, and `parsed.findings[].{message,issue,suggested_fix}`. The plan-review schema's free-text fields are `parsed.findings[].{concern,suggested_change,section}` plus the top-level `parsed.notes[]` string array — none of these flow through the existing layer-2/3 redactors today. Closing that gap is part of this plan and lands at the wrapper, never via the orchestrator.

Post-work the orchestrator's Phase 1.5 responsibilities collapse to three: composing the user-facing plan-review summary banner from Python-supplied counts, narrating dismissed-finding context in the "Plan review notes" section, and pausing on `unknown_state` from the new router.

## 3. Design

### 3.1 `plan_ops.py plan-review-route` (new subcommand)

Stateless pure function. Input: a stage discriminator + whichever envelopes / flags are relevant at that stage. Output: one directive the orchestrator executes. Mirrors `review-route` (PHASE_D TASK-001) cell-for-cell.

**Input (stdin JSON):**
```json
{
  "stage": "pre_dispatch | post_review | post_triage | post_second_review",
  "claude_only": true,
  "plan_review_state": {
    "attempt": 1,
    "first_verdict": null,
    "triage_dispatched": false,
    "triage_verdict": null,
    "load_bearing_indices": [],
    "dismissed_indices": [],
    "auto_revise_round_completed": false,
    "skipped_reason": null
  },
  "plan_review_envelope": null | { "verdict":"approved|approved-with-notes|needs-replan",
                                   "findings":[...], "notes":[...], "summary":"...",
                                   "schedule_ok": true, "outcome":"success|failure|timeout|parse_error",
                                   "reviewer":"claude|codex" },
  "triage_envelope": null | { "verdict":"ship|ship-with-fixes|partial-agreement|needs-rework",
                              "load_bearing":[0,2], "dismissed":[1,3], "summary":"..." },
  "flags": {
    "skip_plan_review": false,
    "codex_plan_review_binding": false,
    "no_auto_revise": false,
    "allow_gaps": false
  }
}
```

**Output (stdout JSON):**
```json
{
  "action": "skip_plan_review | dispatch_claude_reviewer | dispatch_codex_reviewer
           | proceed_to_phase_2 | dispatch_triage | dispatch_plan_author_per_finding
           | rerun_analyst_then_review | halt_plan_review_failed
           | pause_awaiting_user | unknown_state",
  "args": {
    "skip_payload":      { "reason": "flag|codex_unavailable|claude_review_failure",
                           "summary_banner": "..." },
    "dispatch_context":  { "template": "PhaseB-1.5-claude | PhaseB-1.5-codex
                                       | Phase1-triage | Phase1.5a-author-task-targeted
                                       | Phase1.5a-author-schedule-level",
                           "findings_for_payload":   [...],
                           "dismissed_for_context":  [...],
                           "per_finding_dispatches": [
                             { "finding_index": 0, "target_task_id": "002",
                               "child_plan_file": "...", "variant": "A" }
                           ],
                           "allow_gaps_demotion": false },
    "summary_section":   { "banner": "[plan-review-disagreement]" | null,
                           "notes_section": "Plan review notes" | null,
                           "findings": [...], "dismissed_indices": [] },
    "halt_payload":      { "reason": "plan_review_failed",
                           "reason_detail": "second_needs_replan
                                            | binding_flag
                                            | no_auto_revise
                                            | author_retry_failure
                                            | author_introduced_structural_defect",
                           "findings": [...] },
    "state_transitions": { "set_attempt": 2, "record_triage_verdict": "ship",
                           "record_load_bearing": [0,2], "record_dismissed": [1] }
  }
}
```

Every cell from §Phase 1.5 routing table (lines 418–422) and §Phase 1.5.5 routing table (lines 439–444) becomes one Python decision. The four ladder layers fold into the four `stage` values:

- **`pre_dispatch`** — applies `--skip-plan-review`, then route-switches by `claude_only`. Emits `skip_plan_review`, `dispatch_claude_reviewer`, or `dispatch_codex_reviewer` (the latter carrying `allow_gaps_demotion` when `--allow-gaps` is set).
- **`post_review`** — applies wrapper-degradation (`outcome ∈ {timeout, parse_error, failure}` → `skip_plan_review {reason:"codex_unavailable"}`; reviewer-side error on the Claude path → `skip_plan_review {reason:"claude_review_failure"}`). Then routes the verdict ladder: `approved`/`approved-with-notes` → `proceed_to_phase_2`; `needs-replan` + binding-flag → `halt_plan_review_failed{reason_detail:"binding_flag"}`; `needs-replan` + `--no-auto-revise` → `halt_plan_review_failed{reason_detail:"no_auto_revise"}`; otherwise `dispatch_triage`. **Second pass** (`attempt == 2`): `approved`/`approved-with-notes` → `proceed_to_phase_2`; `needs-replan` → `halt_plan_review_failed{reason_detail:"second_needs_replan"}` (binding rule, no second triage).
- **`post_triage`** — routes the triage verdict: `ship` → `proceed_to_phase_2` with `summary_section.banner = "[plan-review-disagreement]"`; `ship-with-fixes` → `proceed_to_phase_2` with `notes_section = "Plan review notes"`; `partial-agreement` → `dispatch_plan_author_per_finding` with `findings_for_payload` filtered to triage `load_bearing` indices and `dismissed_for_context` carrying the rest; `needs-rework` → `dispatch_plan_author_per_finding` with the full findings array.
- **`post_second_review`** — present so callers can distinguish the second-pass binding state explicitly when surfacing run-log events; behaviorally equivalent to `post_review` with `attempt == 2`.

`unknown_state` is the escape hatch on any unrecognized verdict, missing required envelope, or non-enumerated `(stage, flag)` combination — the orchestrator pauses and returns to the user rather than guessing.

Routing exposed as pure function `route(payload: dict) -> dict`; `cmd_plan_review_route` is the thin stdin / `_emit` shim. Tests call `route()` directly. Schemas live in `plugins/plan-executor/scripts/plan_review_route_input_schema.json` + `plan_review_route_output_schema.json`.

### 3.2 Persistent Phase 1.5 state in `.schedule.json`

Extend the schedule schema's `state` block (TASK-002 of PHASE_D) with a sibling `plan_review_state` object. The block is optional — old schedules without it continue to work.

```json
"plan_review_state": {
  "attempt": 1,
  "first_verdict": "needs-replan",
  "first_findings_count": 4,
  "triage_dispatched": true,
  "triage_verdict": "partial-agreement",
  "load_bearing_indices": [0, 2],
  "dismissed_indices": [1, 3],
  "author_dispatches_completed": [
    {"finding_index": 0, "target_task_id": "002", "files_edited": ["..."]}
  ],
  "auto_revise_round_completed": false,
  "skipped_reason": null
}
```

Pure helpers in `plan_ops.py`:
- `read_plan_review_state(schedule_path) -> dict`
- `apply_plan_review_state_transition(state, transitions) -> dict`
- `record_plan_review_verdict(state, attempt, verdict, findings_count) -> dict`
- `record_triage_outcome(state, verdict, load_bearing, dismissed) -> dict`
- `record_author_dispatch(state, finding_index, target_task_id, files_edited) -> dict`

`plan-review-route` accepts `--update-schedule-state <path>` to atomically apply the `state_transitions` block from its output via the existing `_atomic_write_json` path (reused from `commit-task`). The CLI flag is a thin file-IO shim around the pure helpers; tests call the helpers against in-memory dicts.

### 3.3 Wrapper sanitizer coverage extension

The plan-review envelope's free-text fields are not covered by `_codex_envelope_sanitizer.py` today. Three additions:

1. Add `"concern"`, `"suggested_change"`, `"section"` to `FINDING_FIELDS` so layer-2 stripping (code fences, ATX headers, `>` blockquotes) and layer-3 known-shape redaction (role tags, tool-call blocks, `Ignore previous instructions`) cover them inside `parsed.findings[].*`.
2. Extend `sanitize()` to walk `parsed.notes[]` (array of strings) — each string runs through `_process_field` with field path `parsed.notes[i]`.
3. Add a regression-fixture envelope to `test_codex_envelope_sanitizer.py` with injected role-tags inside `findings[].concern`, `<tool_calls>` inside `findings[].suggested_change`, and `Ignore prior instructions` inside `notes[2]`. Sanitized output must contain `[redacted:<shape>]` markers, `extra.sanitizer_flags[]` must list each shape/field/count, and the pre-redaction sha256 must appear in the captured run-log stream.

Wrapper-side coverage is sufficient — the same `sanitize()` call already runs in `plan_codex_dispatch.py plan-review`'s emit path, so once the field lists expand the new fields are covered without touching the dispatcher.

### 3.4 SKILL.md diet — Phase 1.5 / Phase 1.5.5 collapse

Combined Phase 1.5 + Phase 1.5.5 sections are ~12,977 chars today. Target ≥30% character reduction (`wc -c` before/after on the section delimiters `^### Phase 1\.5 — ` through end of `^### Phase 1\.5\.5` block). Expected: ~9,000 chars or less.

Changes:
- Collapse the `claude_only=true` / `claude_only=false` route-switch prose into one paragraph: "call `plan-review-route stage=pre_dispatch` and dispatch whichever reviewer mechanism it names". The verdict-routing table (lines 418–422) becomes one paragraph: "call `plan-review-route stage=post_review` with the parsed envelope and execute the named action".
- Remove the verbatim `--allow-gaps` severity-aware demotion paragraph (line 424); replace with a one-liner stating the orchestrator forwards `--allow-gaps` into the `flags` block of the route input and `plan-review-route` decides whether to set `dispatch_context.allow_gaps_demotion`.
- Collapse the Phase 1.5.5 triage verdict table (lines 439–444) into one paragraph naming `plan-review-route stage=post_triage` and the action set it returns.
- Remove the verbatim "Per-finding embeds in the triage dispatch" prose (line 448); replace with a sentence stating that `plan-review-route` enriches the per-finding payload before emitting `dispatch_plan_author_per_finding`.
- Remove the "Second `needs-replan`" prose block + accompanying `log-event` example (lines 460–468); replace with a sentence stating that `plan-review-route stage=post_second_review` returns `halt_plan_review_failed` directly.
- Add a new "Orchestrator Phase 1.5 responsibilities" subsection enumerating the three preserved duties from §3.6 plus a counter-example list.
- Update the `plan_ops.py` CLI reference (the table that drift-guard TASK-006 of PHASE_D enforces) with the new `plan-review-route` row.

Hard rules in the "Rules" section — including the `claude_only=true` Codex-shellout prohibition (line 769) and the re-source-verdict-is-binding rule (line 774) — are preserved verbatim.

### 3.5 Drift-guard expansion

Extend `tests/scripts/test_skill_cli_reference_drift.py` (created in PHASE_D TASK-006) so it picks up the new `plan-review-route` argparse subparser registration and asserts the SKILL CLI reference table contains exactly one matching row. No new test file — reuse the existing introspection logic; adding the subcommand to `plan_ops.py`'s argparse automatically widens the assertion set.

### 3.6 Preserved LLM-only Phase 1.5 responsibilities

Codified in SKILL and enforced by the absence of a Python path for them:

1. **Plan-review summary banner composition.** Python supplies counts, dismissed indices, and the canonical banner string identifier; the LLM composes the prose paragraph the user reads.
2. **Dismissed-finding narration.** Python supplies the dismissed indices and verbatim finding bodies; the LLM weaves the "Plan review notes" / "Analyst triage notes" subsection into the run summary.
3. **Pause-escalation on `unknown_state`.** Orchestrator halts, emits `awaiting_user` with the route-input + envelopes, returns control. No guessing.

Counter-examples (orchestrator MUST NOT): track `auto_revise_round_completed` across turns; pick filtered-vs-full findings payload by re-reading the triage envelope; re-derive the second-pass binding rule from prose; evaluate untrusted reviewer text for injection intent.

## 4. Non-goals

- Changes to the `plan-analyst` per-child classifier contract (Phase 1, distinct seam).
- Changes to `codex_plan_review_schema.json` or `codex_plan_review_triage_schema.json` beyond what routing requires (no field additions; existing fields are sufficient inputs to `route()`).
- Full MCP tool-server migration of `plan_ops.py` subcommands (separate sibling plan; bash dispatch stays).
- Changes to the `plan-author` system prompt beyond the dispatch-template variable-name alignment in TASK-006.
- Cross-plan dependency surfacing in plan-review (Phase 0 owns this).
- Run-resumption across crashes (the persisted `plan_review_state` enables it but end-to-end resume is out of scope).
- Removing the `--codex-plan-review-binding` / `--no-auto-revise` / `--allow-gaps` / `--skip-plan-review` flag surface; they remain user-facing on the SKILL command line.

## Verification

- Per-task unit tests cover the new subcommand and pure helpers.
- Drift guard (TASK-005) fails CI if `plan_ops.py` argparse and SKILL.md's CLI table diverge on `plan-review-route`.
- TASK-007 end-to-end smoke covers, with stubbed reviewer + triage + author Agents:
  - `--skip-plan-review` short-circuit.
  - Wrapper outcome timeout/parse_error/failure → `skip_plan_review {reason:"codex_unavailable"}`.
  - Claude reviewer dispatch failure → `skip_plan_review {reason:"claude_review_failure"}`.
  - `approved` happy path → `proceed_to_phase_2`.
  - `approved-with-notes` → `proceed_to_phase_2` with notes section.
  - `--allow-gaps` + soft-only `gaps[]` → `dispatch_codex_reviewer` with `allow_gaps_demotion=true` → `approved-with-notes` → `proceed_to_phase_2` (no plan-author).
  - `needs-replan` + `--codex-plan-review-binding` → `halt_plan_review_failed{reason_detail:"binding_flag"}`.
  - `needs-replan` + `--no-auto-revise` → `halt_plan_review_failed{reason_detail:"no_auto_revise"}`.
  - `needs-replan` → triage `ship` → `proceed_to_phase_2` with `[plan-review-disagreement]` banner.
  - `needs-replan` → triage `ship-with-fixes` → `proceed_to_phase_2` with notes.
  - `needs-replan` → triage `partial-agreement` → `dispatch_plan_author_per_finding` (filtered) → `rerun_analyst_then_review` → second `approved` → `proceed_to_phase_2`.
  - `needs-replan` → triage `needs-rework` → `dispatch_plan_author_per_finding` (full) → second `needs-replan` → `halt_plan_review_failed{reason_detail:"second_needs_replan"}`.
  - Malformed reviewer envelope (unknown verdict) → `unknown_state`.
  - Sanitizer-flag surfacing on a plan-review envelope with injected `<tool_calls>` inside `findings[].suggested_change`.
- Run-log event order verified: `run_start`, `schedule_written`, `analyst_done`, `plan_review_start`, `plan_review_done`, `plan_review_route_called {stage, action}`, optional `plan_review_triage_{start,done}`, optional `plan_author_{start,done}`, `batch_start` (or `run_end reason=plan_review_failed`).
- Manual acceptance: one real plan through `--dry-run` and for-real; run-log deltas match the pre-change baseline plus new `plan_review_route_called {stage, action}` events.

## 6. Execution — parallel batches

Codex is the required implementer for every task in this plan.

Seven tasks, four batches. TASK-001 and TASK-002 both edit `plan_ops.py` so they cannot batch together; TASK-003 edits `_codex_envelope_sanitizer.py` and is independent of the rest. Batching obeys the no-shared-file-lock rule and is validated by `plan_ops.py parse-schedule`.

- **Batch 1 (parallel):** TASK-001, TASK-003 — independent; different files.
- **Batch 2:** TASK-002 — alone; `plan_ops.py` lock.
- **Batch 3:** TASK-004 — alone; SKILL.md edit (depends on 001+002).
- **Batch 4 (parallel):** TASK-005 (depends on 004), TASK-006 (depends on 001+004).
- **Batch 5:** TASK-007 (depends on 001+002+004+006).

Single-session parallel execution within each batch is the target.

---

## Tasks

### TASK-001: Add `plan_ops.py plan-review-route` subcommand

- **Status:** done
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - plugins/plan-executor/scripts/plan_review_route_input_schema.json (create)
  - plugins/plan-executor/scripts/plan_review_route_output_schema.json (create)
  - tests/scripts/test_plan_ops_plan_review_route.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_plan_review_route.py`
- **Acceptance criteria:**
  - `plan_ops.py plan-review-route --stdin --json` consumes the §3.1 input and emits the §3.1 output.
  - Every Phase 1.5 / Phase 1.5.5 routing cell from SKILL.md (lines 418–422 and 439–444) is reachable through one `action` value: `skip_plan_review`, `dispatch_claude_reviewer`, `dispatch_codex_reviewer`, `proceed_to_phase_2`, `dispatch_triage`, `dispatch_plan_author_per_finding`, `rerun_analyst_then_review`, `halt_plan_review_failed`, `pause_awaiting_user`, `unknown_state`.
  - `stage="pre_dispatch"` + `flags.skip_plan_review=true` → `skip_plan_review {reason:"flag"}`.
  - `stage="pre_dispatch"` + `claude_only=true` → `dispatch_claude_reviewer`; `claude_only=false` → `dispatch_codex_reviewer` with `dispatch_context.allow_gaps_demotion = flags.allow_gaps`.
  - `stage="post_review"` + `plan_review_envelope.outcome ∈ {"timeout","parse_error","failure"}` → `skip_plan_review {reason:"codex_unavailable"}`; Claude reviewer + outcome=failure → `skip_plan_review {reason:"claude_review_failure"}`.
  - `stage="post_review"` + verdict `approved|approved-with-notes` → `proceed_to_phase_2`; `needs-replan` + `flags.codex_plan_review_binding=true` → `halt_plan_review_failed{reason_detail:"binding_flag"}`; `needs-replan` + `flags.no_auto_revise=true` → `halt_plan_review_failed{reason_detail:"no_auto_revise"}`; `needs-replan` + `attempt==1` → `dispatch_triage`; `needs-replan` + `attempt==2` → `halt_plan_review_failed{reason_detail:"second_needs_replan"}` (no second triage).
  - `stage="post_triage"` + verdict `ship` → `proceed_to_phase_2 summary_section.banner="[plan-review-disagreement]"`; `ship-with-fixes` → `proceed_to_phase_2 summary_section.notes_section="Plan review notes"`; `partial-agreement` → `dispatch_plan_author_per_finding` with `findings_for_payload` filtered to triage `load_bearing` indices and `dismissed_for_context` carrying the complement; `needs-rework` → `dispatch_plan_author_per_finding` with full findings.
  - `dispatch_plan_author_per_finding` produces one entry in `dispatch_context.per_finding_dispatches` per forwarded finding, with `target_task_id` resolved from the finding and `variant: "A"` for task-targeted (`target_task_id != null`) or `variant: "B"` for schedule-level (`target_task_id == null`); the per-entry `child_plan_file` is supplied by the caller via `plan_review_state.task_plan_file_map`.
  - Input schema violation → non-zero exit + structured `errors[*]`.
  - Any unrecognized verdict (outside the schemas' enums) → `action: unknown_state` with human-readable `reason`.
  - Routing exposed as pure function `route(payload: dict) -> dict`; `cmd_plan_review_route` is a thin stdin / `_emit` shim. Tests call `route()` directly — no subprocess, no stdin monkey-patching.
  - Tests cover every cell from §3.1's `stage` matrix plus ≥1 `unknown_state` branch and the second-`needs-replan` halt path.

**Description:** The core routing move. After this lands the orchestrator stops reading the Phase 1.5 / Phase 1.5.5 routing tables and starts piping envelopes to Python. TASK-004's SKILL edits are only safe after this ships.

**Reversion guidance:** Delete the subcommand handler, the two schema files, and the test. Other `plan_ops.py` subcommands are untouched.

---

### TASK-002: Persist Phase 1.5 state in `.schedule.json`

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_plan_review_state.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_review_state.py`
- **Acceptance criteria:**
  - Schedule schema accepts an optional `plan_review_state` object per §3.2; `write-schedule` and `parse-schedule` round-trip it without loss.
  - `plan-review-route --update-schedule-state <path>` atomically applies the `state_transitions` block returned by `route()` to the schedule's `plan_review_state`, reusing the existing `_atomic_write_json` path used by `commit-task` / `fail-task`.
  - Old schedules without `plan_review_state` load without error; first state-write on an old schedule creates the block with `attempt: 1` defaults.
  - State helpers are pure functions: `read_plan_review_state(path) -> dict`, `apply_plan_review_state_transition(state, transitions) -> dict`, `record_plan_review_verdict(state, attempt, verdict, findings_count) -> dict`, `record_triage_outcome(state, verdict, load_bearing, dismissed) -> dict`, `record_author_dispatch(state, finding_index, target_task_id, files_edited) -> dict`. CLI flag is a thin file-IO shim. Tests call the pure functions against in-memory dicts — no subprocess, no tempfile for routing logic.
  - Concurrent writers don't interleave (atomic-write path verified by an explicit test).
  - State defaults match §3.2: `attempt=1`, `triage_dispatched=false`, `auto_revise_round_completed=false`, `skipped_reason=null`, all index lists empty.

**Description:** Moves the orchestrator's Phase 1.5 memory to disk. Required so `plan-review-route` can read `attempt`, `triage_verdict`, `load_bearing_indices`, etc. from the schedule rather than orchestrator context across the multi-turn auto-revise loop.

**Reversion guidance:** Revert the schema additions, drop the `--update-schedule-state` flag from `plan-review-route`, remove the pure helpers. The run-log remains the audit source of truth.

---

### TASK-003: Wrapper sanitizer perimeter — plan-review field expansion

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/_codex_envelope_sanitizer.py (modify)
  - tests/scripts/test_codex_envelope_sanitizer.py (modify)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_codex_envelope_sanitizer.py`
- **Acceptance criteria:**
  - `FINDING_FIELDS` extended to `("message", "issue", "suggested_fix", "concern", "suggested_change", "section")` — covering both the cross-task review schema and the plan-review schema in one tuple.
  - `sanitize()` walks `parsed.notes` when it is a list, running each string element through `_process_field` with field path `parsed.notes[i]`.
  - Existing test cases continue to pass (no regression on `summary` / `diff_summary` / `findings[].message` coverage).
  - New regression fixture: a plan-review envelope with `parsed.findings[0].concern` containing `<system>...</system>`, `parsed.findings[1].suggested_change` containing `<tool_calls>...</tool_calls>`, `parsed.findings[2].section` containing `Ignore prior instructions`, and `parsed.notes[2]` containing `<function_calls>...</function_calls>`. Sanitized output contains `[redacted:<shape>]` markers in place of the originals; `extra.sanitizer_flags[]` lists each shape/field/count; the pre-redaction sha256 for each match appears in the captured run-log stream.
  - Layer-2 strips (code fences, ATX headers, leading `>`) apply uniformly to the new fields and to `notes[]` strings.
  - Length-cap behavior unchanged; `extra.truncated_fields[]` accumulates per-field truncation flags for the new fields.

**Description:** Closes the layer-2/3 gap in the wrapper. Today the plan-review envelope's `concern`, `suggested_change`, `section`, and `notes[]` strings flow through the orchestrator unfiltered — the same shape PHASE_D TASK-003 closed for the cross-task review path, applied to the plan-review path. Wrapper-side defense, not orchestrator-side.

**Reversion guidance:** Revert `FINDING_FIELDS` to `("message", "issue", "suggested_fix")`, remove the `parsed.notes[]` walk, remove the new fixture. Behavior reverts to pre-change envelope flow.

---

### TASK-004: SKILL.md rewrite — Phase 1.5 / Phase 1.5.5 collapse, LLM roles

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (modify)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** deferred (TASK-005)
- **Acceptance criteria:**
  - The Phase 1.5 verdict-routing table (lines 418–422) is replaced by one paragraph describing the `plan-review-route stage=post_review` call-and-comply loop, naming the `unknown_state` pause rule explicitly.
  - The Phase 1.5.5 triage verdict table (lines 439–444) is replaced by one paragraph describing the `plan-review-route stage=post_triage` call-and-comply loop.
  - The route-switch prose (lines 396–401) is collapsed to a one-paragraph statement that the orchestrator calls `plan-review-route stage=pre_dispatch` and dispatches whichever reviewer the action names.
  - The `--allow-gaps` severity-aware demotion paragraph (line 424) is collapsed to a one-liner stating that `--allow-gaps` flows into `flags.allow_gaps` of the route input.
  - The "Per-finding embeds in the triage dispatch" prose (line 448) is collapsed to a sentence stating `plan-review-route` enriches the per-finding payload before emitting `dispatch_plan_author_per_finding`.
  - The "Second `needs-replan`" prose block + `log-event` example (lines 460–468) is replaced by a sentence stating `plan-review-route stage=post_second_review` returns `halt_plan_review_failed`.
  - A new "Orchestrator Phase 1.5 responsibilities" subsection enumerates the three preserved duties from §3.6 plus a counter-example list of things the orchestrator must NOT do (track `auto_revise_round_completed`, pick filtered-vs-full payload, evaluate untrusted reviewer text for injection intent).
  - The `plan_ops.py` CLI reference table gets one new row for `plan-review-route` (purpose only, no flags); footer points at `--help`.
  - Net reduction ≥30% characters across the combined Phase 1.5 + Phase 1.5.5 sections (`wc -c` before/after on the section delimiters; baseline ≈12,977, target ≤9,000).
  - Hard rules in the "Rules" section are preserved verbatim — including the `claude_only=true` Codex-shellout prohibition and the re-source-verdict-is-binding rule.
  - The run-log event order paragraph (line 472) is updated to insert `plan_review_route_called {stage, action}` between `plan_review_done` and the optional triage events.

**Description:** The context-window payoff. After 001/002/003 ship, this removes the now-redundant orchestrator-side state-machine documentation for Phase 1.5.

**Reversion guidance:** Restore SKILL.md from the prior commit on the branch. No code to roll back.

---

### TASK-005: Drift guard — extend SKILL ↔ argparse parity test for `plan-review-route`

- **Status:** Pending
- **Implementer:** codex
- **Priority:** medium
- **Files:**
  - tests/scripts/test_skill_cli_reference_drift.py (modify)
- **Dependencies:** TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py`
- **Acceptance criteria:**
  - Existing introspection logic from PHASE_D TASK-006 picks up the new `plan-review-route` argparse subparser without code changes; the test passes after TASK-001 + TASK-004 land.
  - Test fails fast and loudly if a future change adds `plan-review-route` flags to argparse but not to the SKILL CLI table, or vice versa.
  - Add an explicit assertion that the SKILL table contains a row whose first column equals `plan-review-route`, distinct from the existing `parse-plan-review-report` and `parse-plan-review-triage-report` rows (no name collision).

**Description:** Keeps the two documentation surfaces honest across this plan's edits. Mostly a no-op extension — the existing TASK-006 logic is generic over argparse subcommands.

**Reversion guidance:** Revert the explicit-row assertion; the rest of the test is shared with PHASE_D and stays.

---

### TASK-006: Dispatch-templates alignment for Phase 1.5 / Phase 1.5.5

- **Status:** Pending
- **Implementer:** codex
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (modify)
- **Dependencies:** TASK-001, TASK-004
- **Test command:** deferred (TASK-007)
- **Acceptance criteria:**
  - Phase 1.5-Claude template accepts the `dispatch_context.template = "PhaseB-1.5-claude"` shape `plan-review-route` emits (`schedule_path`, `plan_basename`, `allow_gaps_demotion`).
  - Phase 1.5-Codex template accepts the `dispatch_context.template = "PhaseB-1.5-codex"` shape (`schedule_path`, `repo_root`, `timeout`, `allow_gaps`).
  - Phase 1-triage / Phase 1.5.5 template accepts the `dispatch_context.template = "Phase1-triage"` shape (`source`, `findings_for_payload`, `findings_count`, optional `schedule_path`).
  - Phase 1.5a author templates (Variant A task-targeted, Variant B schedule-level) accept the `per_finding_dispatches[i]` entry shape (`finding`, `target_task_id`, `child_plan_file` for A; `finding`, `target_task_id: null`, `roster_file` for B) — one dispatch per finding, not one whole-plan dispatch.
  - Template text references data received from `plan-review-route`, not inline orchestrator composition; no hand-curated examples reintroduce orchestrator-side state.
  - Phase B default template (Phase D code review) is unchanged.
  - Phase A analyst template is unchanged.

**Description:** Mechanical alignment. Templates stay in the SKILL prompt library; only the expected variable names change to match the new route output shape.

**Reversion guidance:** Restore dispatch-templates.md from the prior commit.

---

### TASK-007: End-to-end smoke — full Phase 1.5 → Phase 1.5.5 → Phase 2 loop with stubs

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - tests/scripts/test_phase_1_5_e2e.py (create)
  - tests/scripts/fixtures/phase_1_5_plan/ (create) — minimal plan dir + `00_INDEX.json` + one child task
- **Dependencies:** TASK-001, TASK-002, TASK-004, TASK-006
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_phase_1_5_e2e.py`
- **Acceptance criteria:**
  - Drives `plan_ops.py preflight → parse-schedule → write-schedule → plan-review-route stage=pre_dispatch → ... → batch-next` against the fixture plan directory.
  - Reviewer + triage + plan-author Agent outputs injected via stub — no real Codex or Claude Agent spawn.
  - Matrix covers every entry in §Verification: skip-flag, wrapper degradation (timeout / parse_error / failure), Claude reviewer dispatch failure, `approved` happy path, `approved-with-notes`, `--allow-gaps` demotion → `approved-with-notes`, `--codex-plan-review-binding` halt, `--no-auto-revise` halt, triage `ship` / `ship-with-fixes` / `partial-agreement` (with author + re-analyst + re-review → second `approved`) / `needs-rework` (with second `needs-replan` halt), unknown-verdict → `unknown_state`, sanitizer-flag surfacing on a plan-review envelope with injected `<tool_calls>` inside `findings[].suggested_change`.
  - Run-log event order verified: `run_start`, `schedule_written`, `analyst_done`, `plan_review_start`, `plan_review_done`, `plan_review_route_called {stage, action}`, optional `plan_review_triage_{start,done}`, optional `plan_author_{start,done}`, `batch_start` or `run_end reason=plan_review_failed`.
  - Test runs in under 30s (no real subprocess spawn beyond `plan_ops.py` CLI calls).
  - `plan_review_state` in the schedule round-trips correctly across multi-turn cases — second-pass tests assert `attempt=2` is observed by `route()` from the persisted state, not from in-memory carry.

**Description:** The safety net. Locks in the orchestrator ↔ `plan-review-route` contract so future SKILL edits cannot silently regress Phase 1.5 / Phase 1.5.5 routing.

**Reversion guidance:** Delete the test file + fixture directory.
