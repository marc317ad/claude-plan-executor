# TASK-021 — Codex reviewer evidence gate for `needs-rework`

**Base branch:** `main`
**Audit anchor commit:** `f783c95`
**Chunk dependencies:** TASK-015 (established the verdict decision ladder this task extends).
**Motivating run:** `20260418T010801` — Codex issued 3 findings on TASK-004A (2 `important`, 1 `minor`) and returned `needs-rework`. The Phase D.5 third-opinion code-reviewer independently dismissed all three in ~2 minutes after actually tracing the control flow and running `pytest -k cycle`. All three findings were unverified hypotheses: Finding 0 ("cycle-closure may hang") missed the `closed`-set guard at `plan_ops.py:1920`; Finding 1 conditionally hedged on `_validate_schedule`'s behavior without reading it; Finding 2 was conditioned on "only safe if upfront validation enforces dependency item types" — a premise Codex never checked.

---

## Goal

Add an **evidence gate** to the Phase D-Codex reviewer prompt: before assigning `needs-rework` for a claimed semantic bug or contract violation, Codex must cite a concrete observation (a reproduced failure, a traced control-flow step, a failing test run, or a cited invariant violation). A finding phrased as *"if X is true, then..."* or *"this is only safe if..."* is a hypothesis, not an observation — such findings belong at `minor-findings` phrased as a question, not at `needs-rework`.

TASK-015 calibrated *which rung a finding belongs on*. TASK-021 calibrates *what Codex must do before picking the top rung*.

The verdict vocabulary itself is unchanged.

---

## Scoped Context

### Why now

Run `20260418T010801` is a clean repeat of the failure mode TASK-015 was supposed to address. The ladder helped (Codex didn't blanket-escalate stylistic nits), but the calibration didn't require evidence for the findings that did get escalated. D.5 dismissed 3/3 findings by doing the verification Codex skipped — reading ~10 lines of `_validate_schedule_refs`, tracing a 3-pop cycle, running one `pytest -k` invocation.

Without this gate, the bounded-remediation retry (D.2a.5) and narrow-remediation retry (D.2a.6) will keep burning implementer turns on speculative findings that D.5 would have dismissed anyway. The evidence gate is strictly upstream of those retries and costs nothing at steady state.

### Existing surfaces we edit

- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — the "Phase D-Codex review" template (lines 114-131) is the authoritative prompt source. Add a new "Evidence gate" subsection between the heuristic (line 124) and the worked examples (line 126-129).
- `tests/scripts/test_codex_review_prompt.py` — extend with one new test function asserting the evidence-gate wording is present. Do NOT modify the existing `test_decision_ladder_present` / `test_worked_examples_present` / `test_fixture_matches_review_schema` tests.

### Non-goals

- Changing the verdict vocabulary (`clean | minor-findings | needs-rework`).
- Tuning the D.5 third-opinion prompt — its strictness is appropriate; it's the tie-breaker.
- Tuning the Claude `code-reviewer` (Phase D-Claude) prompt — separate concern; pattern hasn't emerged from Claude-side reviews.
- Adding runtime verification enforcement in `plan_codex_dispatch.py` (e.g., a linter that rejects envelopes with hypothesis-shaped finding text). Prompt-level guidance is the cheapest first intervention; harder enforcement can be a follow-up if this doesn't take.

### Design constraints

The evidence gate must:

1. Be **prompt-level only** — no schema changes, no wrapper changes, no runtime parser changes.
2. Be **action-guiding, not advisory** — tell Codex what to do (cite an observation; if you can't, downgrade), not just what to avoid.
3. Name specific verification moves that are cheap in the dispatch sandbox: running `pytest -k <name>`, reading the cited symbol in the repo, tracing a short control-flow path by hand.
4. Give a **downgrade path** — "if you can't verify, file at `minor-findings` as a question." Hypotheses still get surfaced; they just don't gate the commit.
5. Be terse. The Phase D-Codex section is shared context budget; aim for ≤150 words of added text.

---

## Verification

**V1 — Evidence gate present in the Codex prompt.**

The updated template MUST include an explicit "Evidence gate" subsection between the existing verdict ladder/heuristic and the worked examples. The subsection names:

- the requirement (observation cited before `needs-rework`);
- at least two specific verification moves (e.g., "run the task's test command", "read the cited symbol", "trace the control-flow path");
- the downgrade rule (hypothesis without verification → `minor-findings` phrased as a question).

**V2 — Worked example illustrating the gate.**

Add a **third** worked example to the existing "Worked examples" block, showing a finding that reads as `needs-rework` on surface (e.g., "X may cause a hang") but is correctly downgraded to `minor-findings` because the reviewer could not reproduce or verify it. Keep the other two TASK-015 examples (outdated-comment → `minor-findings`; acceptance-criterion miss → `needs-rework`) intact.

**V3 — Prompt-text regression check.**

Extend `tests/scripts/test_codex_review_prompt.py` with a new `test_evidence_gate_present` function that asserts:

- the Phase D-Codex section contains the string `Evidence gate` (or equivalent heading);
- the section contains at least one of the specific verification moves (e.g., `pytest` or `trace` or `read the cited`);
- the section contains the downgrade phrasing (e.g., `if you cannot verify` → downgrade / `minor-findings`);
- the existing `test_decision_ladder_present` and `test_worked_examples_present` still pass (no regression).

**V4 — No schema or envelope regression.**

The `test_fixture_matches_review_schema` test must still pass. No changes to `codex_review_schema.json` or the checked-in fixture.

---

## Tasks

### TASK-021: Codex reviewer evidence gate

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `tests/scripts/test_codex_review_prompt.py`
- **Dependencies:** TASK-015 (decision ladder — this task extends it).
- **Test command:** `venv/bin/pytest -q tests/scripts/test_codex_review_prompt.py`
- **Acceptance criteria:**
  - V1–V4 pass.
  - The Phase D-Codex section gains one new "Evidence gate" subsection between the heuristic paragraph and the worked-examples block.
  - One new worked example added; the original two TASK-015 examples are preserved verbatim.
  - One new test function added; existing test functions unchanged and still passing.
  - No changes to `codex_review_schema.json`, the checked-in fixture, the Phase D.5 prompt, the Phase D-Claude prompt, or any other section of `dispatch-templates.md`.

**Description:**
Extend the Phase D-Codex reviewer prompt with an evidence gate: `needs-rework` requires a cited observation, not a hypothesis. Finding phrased as *"if X is true..."* or *"this is only safe if..."* that the reviewer did not verify must be downgraded to `minor-findings` phrased as a question. Add one worked example illustrating the downgrade path so Codex has a pattern to match.

**Implementation notes:**

- This is a prompt-engineering task. Keep the new subsection ≤150 words.
- The existing heuristic ("would a human reviewer block merge on this alone?") complements the evidence gate — keep both, don't replace one with the other. The heuristic answers *how severe is this*; the evidence gate answers *do I actually know it's real*.
- Worked-example shape (mirror TASK-015's worked-example format):

  > Finding: "Transitive closure may loop forever on cycles; cycle check only runs after." Verdict without verification: **`minor-findings`** phrased as a question. Justification: hypothesis — tracing `001→002→001` by hand or running the cycle test would have confirmed or refuted it. When unverified, downgrade and ask.

  Keep the worked example generic; do NOT reference TASK-004A or any specific file path — the cargo-cult risk TASK-015 called out still applies.
- The evidence-gate wording must be unambiguous about which verification moves are in bounds for the Codex sandbox. Running the task's declared test command and reading the repo are both fine; spinning up external services is not.

**Reversion guidance:**
Safe to revert; `git restore plugins/plan-executor/skills/implement-plan/dispatch-templates.md tests/scripts/test_codex_review_prompt.py` restores the prior prompt and test set. No cross-cutting state changes.

---

## Out of Scope

- Tuning the Claude `code-reviewer` (Phase D-Claude) prompt. Measure first; if Claude-side reviewers show the same hypothesize-without-verify pattern in a future run, file a follow-up.
- Runtime enforcement (wrapper-level lint of finding text for hypothesis markers, schema-level `evidence_cited` field). Prompt-level guidance is the cheapest first intervention; escalate to wrapper enforcement only if this doesn't take.
- Retrofitting the evidence gate onto the Phase 1.5 Codex plan-review prompt. Plan-review findings are inherently more speculative (Codex is reading a plan, not a diff); different calibration, different task.
- Changing the `--codex-review-binding` semantics. Independent concern.

## Reversion guidance

Two-file revert (one template section, one new test function). No schema, wrapper, or state changes. Safe to roll back at any time.
