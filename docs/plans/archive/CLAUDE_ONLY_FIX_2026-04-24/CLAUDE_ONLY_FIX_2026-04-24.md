# CLAUDE-ONLY FIX — Strict no-Codex contract for plan and cross-review

**Status:** pending
**Base branch:** main
**Created:** 2026-04-24

## Goal

Make `--claude-only` mean "the orchestrator MUST NOT shell out to `plan_codex_dispatch.py` for ANY purpose during this run." Plan review (Phase 1.5) and cross-review (Phase D) still **run**, but route to Claude-side equivalents. The same routing fires automatically when preflight reports `codex_available=false` so a missing or unauthorized `codex` binary stops silently dropping plan review. Unblocks development when Codex tokens are exhausted while the binary is still installed.

## Scoped Context

### Today's behavior (the bug)

- `--claude-only` rewrites `tasks[].agent="claude"` (`SKILL.md:332`) so Codex is never an implementer.
- **Phase 1.5 plan review** still fires unless `--skip-plan-review` is set OR `codex_available=false` (`SKILL.md:404-407`). When the operator runs `--claude-only` with a healthy `codex` binary but exhausted tokens, the wrapper is invoked, returns `outcome=failure | parse_error`, and the orchestrator silently demotes that to `plan_review_skipped {reason:"codex_unavailable"}` per `SKILL.md:450`. One wasted wrapper roundtrip and a lost review.
- **Phase D.1 cross-review** unconditionally routes Claude-impl → Codex review per the asymmetric matrix at `SKILL.md:683`. With `--claude-only` every task is Claude-impl, so every D.1 hits the wrapper and fails. The user-reported symptom "task id is never passed to codex" is downstream rendering of that same wrapper failure; the contract bug is that we shouldn't be calling Codex at all in this mode.
- **D.2a / D.5 / D.2a.5 / D.2a.6** escalation and **D.2b** role-swap retry all assume Codex is on the other side. Under `--claude-only` the entire ladder is unreachable except via Codex calls that will fail.

### Why this is now urgent

Codex token exhaustion (or any sustained Codex unavailability that is not a missing binary) is currently a hard blocker on `/implement-plan` even when the user has explicitly opted into Claude-only execution.

### Why we're not fixing `plan_codex_dispatch.py` itself

The wrapper is correct: it shells to a real subprocess and returns a structured failure when the subprocess fails. The fix is at the orchestrator-routing layer, not the wrapper layer. A future refactor (renaming to `plan_reviewer_dispatch.py` and adding a unified prompt registry across Codex / Claude / Gemini) is out of scope for this plan; it lands as a follow-up after these three slices are stable.

### Design choices baked into this plan

1. **Plan reviewer is a new Sonnet-tier `plan-reviewer` agent**, not a re-purposed `plan-analyst`. Sonnet (vs the analyst/author Opus) means the reviewer is at least a different inference engine within the same family, even if the cross-family check Codex provides is genuinely lost.
2. **Cross-review under `--claude-only` reuses the existing `code-reviewer` agent** (model `sonnet`) via the existing Phase D-Claude template (`dispatch-templates.md:409`). No new agent file for the cross-review side.
3. **Verdict envelope shape stays Codex-shaped.** `parse-plan-review-report` and the run-log `review_done` events are reviewer-agnostic; the `reviewer` field is already a free string. Reuse them. Add a `--from-claude` flag on `parse-plan-review-report` to skip the wrapper-envelope unwrap (the agent returns the bare `parsed` payload, not an envelope).
4. **`codex_available=false` collapses to the same Claude path.** Today this case logs `plan_review_skipped {reason:"codex_unavailable"}` and warns; after this plan it routes through the same code as `--claude-only`. The `--skip-plan-review` flag remains the only explicit opt-out.
5. **D.5 third-opinion ladder dies under `--claude-only`** (no Codex verdict to escalate). `code-reviewer` `needs-rework` is terminal for the task — it goes straight to D.4 `fail-task`. This is documented in the run summary banner.
6. **`--allow-gaps` demotion** is ported from the wrapper prompt-injection path into the new `plan-reviewer` template prose. The clause is already text in the wrapper; copying it across is mechanical.
7. **The orchestrator's mutual-exclusion checks remain prose-driven**, matching the existing pattern at `SKILL.md:149` (`--codex-only` ⊕ `--claude-only`). No new structural checker in `plan_ops.py`.

### Loud banner contract

When the run uses the Claude-only review path (either via `--claude-only` or `codex_available=false`), the final run summary MUST include this banner verbatim:

> **--claude-only mode:** plan review and cross-review ran with Claude only (no cross-family check). D.5 third-opinion escalation was unavailable; `needs-rework` verdicts went straight to fail-task.

## Verification

After all three tasks land:

- `/implement-plan --claude-only --dry-run docs/plans/sample_phase4.md` does NOT shell out to `plan_codex_dispatch.py` at any phase. Verified by inspecting the run-log: zero `plan_review_start {reviewer:"codex"}` events, zero Codex `review_start` events.
- `/implement-plan --claude-only docs/plans/sample_phase4.md` completes with `plan_review_start {reviewer:"claude"}` and `review_done {reviewer:"claude", ...}` records in the run log. The run summary carries the loud banner described in §Scoped Context.
- `/implement-plan --claude-only --codex-review-binding docs/plans/sample_phase4.md` halts pre-dispatch with a mutual-exclusion error. Same for `--claude-only --codex-plan-review-binding`.
- A run on a system where preflight returns `codex_available=false` (e.g., `codex` binary not on `$PATH`) takes the same Claude-only review path automatically; it does NOT skip plan review.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes end-to-end.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "parse_plan_review_report and from_claude"` passes (new tests added in TASK-002).

## Tasks

## TASK-001: Plumbing — `claude_only` routing flag and mutual exclusions

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit)
- **Dependencies:** none
- **Test command:** venv/bin/pytest -q tests/scripts/test_plan_ops.py
- **Acceptance criteria:**
  - `SKILL.md` §Parse arguments documents that `--codex-review-binding` is mutually exclusive with `--claude-only` and that `--codex-plan-review-binding` is mutually exclusive with `--claude-only`. The mutex prose lives next to the existing `--codex-only` ⊕ `--claude-only` clause at `SKILL.md:149`.
  - Phase 0 prose binds a single boolean `claude_only` derived from `--claude-only OR (codex_available == false from preflight)`. The binding is documented in `SKILL.md` §Pre-flight (Phase 0) so every downstream phase reads one variable.
  - `SKILL.md` §Rules adds a hard rule: *"When `claude_only=true`, the orchestrator MUST NOT invoke `plan_codex_dispatch.py` for ANY subcommand (`plan-review`, `review`, `implement`). Codex shell-out under `claude_only` is a protocol violation."*
  - `run_start` event documents that it carries a `claude_only: <bool>` field in `fields` (the field is added in TASK-002 and TASK-003 as those phases are wired; TASK-001 only documents the binding).
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (regression check — no new tests are required; the change is prose-only).

**Description:**
Hoist the routing decision to one place so TASK-002 and TASK-003 each have a single boolean to consult. The flag is bound at Phase 0 from the union of `--claude-only` (operator opt-in) and `codex_available=false` (preflight signal). This task is prose-only — it documents the binding and the new mutual exclusions but does not change any review or cross-review behavior yet. Behavior changes land in TASK-002 (plan review) and TASK-003 (cross-review).

The mutual-exclusion checks follow the existing prose-driven pattern at `SKILL.md:149`. We do NOT add a structural checker in `plan_ops.py` because that would be inconsistent with how `--codex-only` ⊕ `--claude-only` is enforced today; the orchestrator LLM reads the prose and halts pre-dispatch. If empirically the prose-only mutex turns out to be unreliable, we add a `plan_ops.py validate-flags` subcommand as a separate follow-up.

**Implementation notes:**

Edit sites in `SKILL.md`:

1. **§Parse arguments mutual-exclusion line** (currently `SKILL.md:149`): extend the existing line to read approximately:

   > Mutual exclusions: `--codex-only` + `--claude-only` → error. `--codex-review-binding` + `--claude-only` → error. `--codex-plan-review-binding` + `--claude-only` → error. Normalize `--task-ids` values via `plan_ops.py normalize-task-id` before filtering.

2. **§Pre-flight (Phase 0)**: after the `preflight` invocation that pins `$PYTHON` and surfaces `codex_available`, add a paragraph binding `claude_only`:

   > **Bind `claude_only` (TASK-001).** After preflight, set `claude_only = (--claude-only flag is present) OR (preflight.codex_available == false)`. Every subsequent phase consults this single boolean — Phase 1.5 plan review, Phase D.1 cross-review, Phase D.2a/D.2a.5/D.2a.6 escalation, Phase D.2b role-swap retry. Include `claude_only: <bool>` in the `run_start` event's `fields`.

3. **§Rules**: append a new bullet near the top of the rule list:

   > - **`claude_only` is a strict no-Codex contract.** When `claude_only=true`, the orchestrator MUST NOT invoke `plan_codex_dispatch.py` for any subcommand (`plan-review`, `review`, `implement`). Routing in Phase 1.5 and Phase D consults this boolean and selects the Claude-side equivalent. Calling the wrapper while `claude_only=true` is a protocol violation.

Do NOT touch `dispatch-templates.md`, `plan_ops.py`, or any test files in this task. The plumbing is documentary; the behavior changes are TASK-002 and TASK-003.

**Reversion guidance:**
Single-file revert of the prose edits to `SKILL.md`. No code changes; no test changes; no agent files. The mutual-exclusion lines and the `claude_only` binding paragraph can be removed in one diff with no downstream impact since TASK-002/TASK-003 have not consumed the flag yet.

## TASK-002: Plan review Claude path

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/agents/plan-reviewer.md (create)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (edit)
  - plugins/plan-executor/scripts/plan_ops.py (edit)
  - tests/scripts/test_plan_ops.py (edit)
- **Dependencies:** TASK-001
- **Test command:** venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "parse_plan_review_report"
- **Acceptance criteria:**
  - File `plugins/plan-executor/agents/plan-reviewer.md` exists with frontmatter `model: sonnet`, `tools: Read, Grep, Glob, Bash`, and a system prompt that mirrors the Codex plan-review prompt structure. Output contract is one fenced ```json block conforming to `codex_plan_review_schema.json` (`{plan_file, verdict ∈ {approved, approved-with-notes, needs-replan}, findings[], notes[], schedule_ok, summary}`). The prompt explicitly states "You do NOT have the Agent tool" and forbids editing the plan or schedule.
  - `dispatch-templates.md` gains a new section "Phase 1.5-Claude — plan-reviewer dispatch (claude_only path)" that renders the Agent dispatch with `subagent_type: "plan-reviewer", model: "sonnet"`. The template embeds the schedule path, plan directory path, repo root, `findings_count` placeholder, and the `--allow-gaps` demotion clause when applicable.
  - `SKILL.md` §Phase 1.5 grows a route-switch at the top: when `claude_only=true`, dispatch via the Phase 1.5-Claude template; otherwise, dispatch via the existing wrapper path. Both branches feed the same `parse-plan-review-report` parser; the only difference is the dispatch mechanism and the `reviewer` field in the run-log events.
  - `SKILL.md` §Phase 1.5 retires the `codex_available=false → plan_review_skipped {reason:"codex_unavailable"}` skip clause. That case now flows through `claude_only=true` instead (per TASK-001's binding) and dispatches the Claude reviewer.
  - `plan_ops.py parse-plan-review-report` accepts a new `--from-claude` flag. When set, the parser treats stdin as the bare `parsed` payload (matching `codex_plan_review_schema.json`) instead of expecting the wrapper envelope `{task_id, subcommand, outcome, codex_exit_code, parsed}`. All existing callers continue to work without the flag.
  - Run-log events on the Claude path: `plan_review_start {reviewer:"claude", plan_file:"<basename>"}`, `plan_review_done {reviewer:"claude", verdict, findings_count, summary}`. The Codex path remains `reviewer:"codex"` unchanged.
  - Tests added to `tests/scripts/test_plan_ops.py`:
    - `test_parse_plan_review_report_from_claude_accepts_bare_parsed_payload` — feeds the bare schema-conforming payload through `--from-claude` and asserts the verdict, findings, and notes are extracted.
    - `test_parse_plan_review_report_from_claude_rejects_wrapper_envelope` — feeds the wrapper-envelope shape with `--from-claude` and asserts a structured `errors[*]` halt (the `--from-claude` flag is strict; passing the envelope shape with the flag set is an operator error).
    - `test_parse_plan_review_report_default_still_accepts_wrapper_envelope` — regression coverage that the wrapper-envelope path is unchanged.
  - The test command `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "parse_plan_review_report"` returns 0.

**Description:**
Replace the wrapper shell-out in Phase 1.5 with an Agent dispatch when `claude_only=true`. New `plan-reviewer` agent (Sonnet) produces the same envelope shape as the wrapper; `parse-plan-review-report` gains `--from-claude` to strip the envelope wrapping. Verdict-routing ladder (`approved | approved-with-notes | needs-replan`), `--codex-plan-review-binding` (now mutex with `--claude-only` per TASK-001), the auto-revise `plan-author` path, and the `--allow-gaps` demotion all keep working unchanged because they consume the parsed verdict, not the dispatch mechanism.

The `plan-reviewer` agent's prompt mirrors the Codex prompt body 1:1 except for: (a) replacing Codex-specific calibration ("Codex historically overuses `needs-rework`" guidance from `dispatch-templates.md:387` is dropped — Sonnet's calibration is different), (b) "You do NOT have the Agent tool" constraint, (c) inputs are passed as Agent prompt placeholders rather than CLI args. The output schema is identical to `codex_plan_review_schema.json`.

**Implementation notes:**

1. **`agents/plan-reviewer.md`**: Mirror the structure of `plan-review-triage.md` (frontmatter shape, "You do NOT have the Agent tool" constraint, output-shape rule). The body adapts the Codex plan-review prompt from `dispatch-templates.md:44-89` plus the `--allow-gaps` demotion clause from `SKILL.md:460`. Output is exactly `codex_plan_review_schema.json` shape, in a fenced ```json block.

2. **`dispatch-templates.md`**: Add a new top-level section after the existing Phase 1.5 (Codex) section. Title: `## Phase 1.5-Claude — plan-reviewer dispatch (claude_only path)`. The template renders an `Agent(subagent_type:"plan-reviewer", model:"sonnet", prompt: <rendered>)` invocation with placeholders for `<absolute schedule path>`, `<plan_dir>`, `<repo_root>`, and `<findings_count>` (where applicable). Include the `--allow-gaps` clause as a conditional block inside the prompt, matching the wrapper's pre-existing prompt-injection at `SKILL.md:460`.

3. **`SKILL.md`** §Phase 1.5: at the top of the section, add a route-switch:

   > **Route by `claude_only`** (bound in Phase 0 per TASK-001):
   > - `claude_only=true` → dispatch via the **Phase 1.5-Claude** template (`dispatch-templates.md`). The Agent's reply is the bare `parsed` payload; pipe it through `parse-plan-review-report --stdin --from-claude --json`. Run-log uses `reviewer:"claude"`.
   > - `claude_only=false` → existing wrapper path (`plan_codex_dispatch.py plan-review`). Run-log uses `reviewer:"codex"`.
   > Both branches share the same verdict-routing ladder, `--codex-plan-review-binding` short-circuit (mutex with `--claude-only` per TASK-001), Phase 1.5.5 plan-review-triage, and Phase 1.5a `plan-author` auto-revise sequence.

   In the §Skip conditions block, replace `codex_available=false (from preflight) → log plan_review_skipped {reason:"codex_unavailable"} ...` with a note that `codex_available=false` is now handled by the `claude_only` route-switch (per TASK-001's binding) and routes to the Claude reviewer rather than skipping.

4. **`plan_ops.py`** `parse-plan-review-report`: add `--from-claude` to the argparse subparser. When set, skip the `parsed = envelope["parsed"]` step and treat stdin directly as `parsed`. Validate against `codex_plan_review_schema.json` exactly the same way. When the flag is set AND stdin parses as the wrapper-envelope shape (top-level keys `task_id`/`subcommand`/`outcome`/`parsed`), halt with `errors[{"code":"unexpected-wrapper-envelope-with-from-claude", ...}]` — the flag is strict.

5. **Tests** in `tests/scripts/test_plan_ops.py`: add the three test cases listed in §Acceptance criteria. Use `subprocess.run` against the installed `plan_ops.py` and feed stdin via `input=`, matching the existing test patterns for `parse-implementer-report`.

**Reversion guidance:**

Per-decision revert table:

| Revert | Effect |
|---|---|
| Delete `agents/plan-reviewer.md` | Phase 1.5-Claude dispatch fails to find the agent; `claude_only` runs halt at Phase 1.5. |
| Remove `--from-claude` from `parse-plan-review-report` | Phase 1.5-Claude dispatch outputs a parser-error on the bare payload. |
| Restore `codex_available=false → plan_review_skipped` clause in §Skip conditions | Re-introduces the silent-drop behavior on missing `codex` binary; `--claude-only` still works because `claude_only=true` from the flag is independent of `codex_available`. |
| Revert `SKILL.md` §Phase 1.5 route-switch | Both branches collapse to the wrapper path; `--claude-only` runs fail at Phase 1.5 with wrapper-failure envelopes. |

Tests for `parse-plan-review-report --from-claude` are independently revertible — they cover only the parser flag, not the routing.

## TASK-003: Cross-review Claude path and D.2 ladder collapse

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (edit)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** venv/bin/pytest -q tests/scripts/test_plan_ops.py
- **Acceptance criteria:**
  - `SKILL.md` §Phase D.1 grows a route-switch identical in shape to TASK-002's: `claude_only=true` → `Agent(subagent_type:"code-reviewer", model:"sonnet", ...)` with the existing Phase D-Claude template (`dispatch-templates.md:409`); `claude_only=false` → existing wrapper path. Both feed the same `review_done` event shape with the appropriate `reviewer` field.
  - `SKILL.md` §Phase D.2a documents that under `claude_only=true`, D.5 escalation, D.2a.5 bounded remediation, and D.2a.6 narrow remediation are unreachable. `code-reviewer` `needs-rework` under `claude_only=true` goes straight to D.4 fail-task, with no D.5 third-opinion ladder. The §Rules section gains a corresponding bullet.
  - `SKILL.md` §Phase D.2b documents that under `claude_only=true`, the role-swap retry uses `code-reviewer` for the re-review (not the Codex wrapper). The retry implement step is unchanged (`plan-implementer` Opus).
  - `SKILL.md` §End of run final summary documents the loud banner contract from §Scoped Context. The banner appears whenever the run had `claude_only=true` for any reason (operator flag OR `codex_available=false`).
  - `dispatch-templates.md` §Phase D-Claude has a brief callout that this template is now the single Claude-cross-review template, used for both Codex-impl→Claude review (existing) AND Claude-impl→Claude review under `claude_only=true` (new).
  - Run-log events under `claude_only=true`: `review_start {reviewer:"claude", task_id, ...}`, `review_done {reviewer:"claude", task_id, verdict, findings_count, ...}`. Verdict vocabulary is `{ship, ship-with-fixes, needs-rework}` (matching the existing Codex-impl→Claude review path); the Codex-side `{clean, minor-findings, needs-rework}` vocab is not synthesized.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (regression check; no new tests are required because the change is prose-only routing).

**Description:**
Route Phase D.1 cross-review through the existing `code-reviewer` agent when `claude_only=true`, mirroring TASK-002's Phase 1.5 route-switch. The `code-reviewer` agent and Phase D-Claude template are reused as-is. The D.2a third-opinion ladder collapses because there is no Codex verdict to adjudicate; `needs-rework` goes straight to D.4 fail-task. The D.2b role-swap retry uses `code-reviewer` for the re-review.

This task is prose-only edits to `SKILL.md` and `dispatch-templates.md`. No new agent file. No code changes in `plan_ops.py`. No new tests. The run summary banner contract from TASK-001/TASK-002's `claude_only` plumbing fires here — the End-of-run summary section gains a one-paragraph rule that emits the banner when `claude_only=true`.

**Implementation notes:**

1. **`SKILL.md` §Phase D.1**: add a route-switch at the top:

   > **Route by `claude_only`** (bound in Phase 0 per TASK-001):
   > - `claude_only=true` → `Agent(subagent_type:"code-reviewer", model:"sonnet", prompt: render(templates.PhaseD_Claude, ...))` regardless of which side implemented (under `claude_only=true` every implementer is Claude, but the same template applies to any source — the asymmetric matrix collapses). Verdict vocab `{ship, ship-with-fixes, needs-rework}`. Run-log uses `reviewer:"claude"`.
   > - `claude_only=false` → existing asymmetric matrix at the table below. Claude-impl → Codex wrapper review, Codex-impl → `code-reviewer` Agent.

2. **`SKILL.md` §Phase D.2a**: add a halting clause at the top:

   > **Under `claude_only=true`, D.2a / D.5 / D.2a.5 / D.2a.6 are unreachable.** No Codex `needs-rework` verdict exists to escalate. The `code-reviewer` `needs-rework` outcome is terminal: route directly to D.4 `fail-task` with `--stage review`, then `block-dependents`. Skip the entire D.2a ladder. `--codex-review-binding` is mutex with `--claude-only` per TASK-001 so the binding-mode path also does not fire.

3. **`SKILL.md` §Phase D.2b**: add a one-line clause:

   > **Under `claude_only=true`**, the role-swap retry re-implements via `plan-implementer` (unchanged) and re-reviews via `code-reviewer` Agent (not the Codex wrapper). Binding behavior is the same: re-review `needs-rework` → D.4 fail-task.

4. **`SKILL.md` §Rules**: add a bullet:

   > - **`claude_only=true` collapses the D.2a ladder.** D.5 third-opinion escalation, D.2a.5 bounded remediation, and D.2a.6 narrow remediation are unreachable because they require a Codex `needs-rework` verdict to fire. `code-reviewer` `needs-rework` under `claude_only=true` is terminal and routes to D.4 fail-task. Operators trade the third-opinion ladder for the ability to run without Codex.

5. **`SKILL.md` §End of run**: add a paragraph in the final summary block:

   > **`claude_only` banner.** When the run had `claude_only=true` (either via `--claude-only` or `codex_available=false`), the run summary MUST include this banner verbatim before the per-task table:
   >
   > > **--claude-only mode:** plan review and cross-review ran with Claude only (no cross-family check). D.5 third-opinion escalation was unavailable; `needs-rework` verdicts went straight to fail-task.

6. **`dispatch-templates.md` §Phase D-Claude**: add a short callout near the top of the section:

   > **Reuse note (TASK-003).** Under `claude_only=true` (orchestrator-bound at Phase 0 from `--claude-only` or `codex_available=false`), this template is also the cross-review path for Claude-implemented work — the asymmetric matrix collapses and every D.1 dispatch uses this prompt. Verdict vocab `{ship, ship-with-fixes, needs-rework}` is unchanged.

**Reversion guidance:**

Single-file revert per file:

| Revert | Effect |
|---|---|
| Revert `SKILL.md` §Phase D.1 route-switch | `claude_only=true` runs fall through to the wrapper at D.1 and fail. |
| Revert §Phase D.2a halting clause | `code-reviewer` `needs-rework` under `claude_only=true` enters a non-existent D.5 ladder; orchestrator halts with internal-error. |
| Revert §End of run banner clause | `claude_only` runs succeed silently without the cross-family-check warning; no functional regression but trust signal is lost. |
| Revert `dispatch-templates.md` §Phase D-Claude callout | Cosmetic; no behavior change. |

The collapse of D.2a/D.5/D.2a.5/D.2a.6 under `claude_only=true` is a documentation contract, not a code change. Reverting it does NOT re-enable the ladder; that would require code-level dispatch logic that currently does not exist for the Claude-only path.

## Out of Scope

- **Renaming `plan_codex_dispatch.py` to `plan_reviewer_dispatch.py`** and adding Gemini support. This is a separate, larger refactor (~5–10 days) discussed in the parent conversation; ship after these three tasks land and stabilize.
- **Per-task reviewer assignment by analyst strength.** Discussed and deferred. If desired later, prefer letting plan authors declare `**Reviewer:**` in plan markdown over making the analyst pick.
- **Structural mutual-exclusion checker** (`plan_ops.py validate-flags`). The orchestrator's prose-driven mutex matches the existing `--codex-only` ⊕ `--claude-only` pattern; a structural checker is a follow-up only if the prose mutex empirically fails.
- **Per-provider prompt calibration registry.** Codex's `needs-rework` over-use ladder is dropped from the Sonnet `plan-reviewer` prompt (Sonnet's calibration is different). A formal calibration overlay system is part of the future Gemini integration.
- **Changes to `plan_codex_dispatch.py`.** The wrapper is correct; the bug is at the routing layer. No edits to the wrapper script.
