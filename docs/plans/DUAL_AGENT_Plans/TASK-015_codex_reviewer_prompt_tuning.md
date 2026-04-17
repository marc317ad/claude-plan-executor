# TASK-015 — Codex reviewer prompt tuning (calibrate `needs-rework` vs `minor-findings`)

**Base branch:** `main`
**Audit anchor commit:** `8a317a9`
**Chunk dependencies:** none (orthogonal to TASK-014).
**Motivating run:** `20260417T153553` — Codex issued 4 findings on the TASK-004A rewrite; the Phase D.5 third-opinion code-reviewer classified only 1 as load-bearing (`SKILL.md:147`, a one-line doc fix). The correct verdict split was `minor-findings` with a single elevated note, not blanket `needs-rework`.

---

## Goal

Tune the Phase D Codex review prompt so `needs-rework` is reserved for contract-violating or materially-wrong changes, and `minor-findings` covers style, nitpicks, doc drift, and other non-load-bearing issues a human would merge with follow-up notes. The verdict vocabulary itself is unchanged; only the calibration of when each fires.

---

## Scoped Context

### Why now

- The TASK-004A run is the clearest case: 1 of 4 findings was load-bearing, Codex still returned `needs-rework`. The strict revert policy then discarded 138 valid lines for a one-line doc fix. TASK-014A adds a remediation retry, but cheap upstream calibration is strictly better than spending an implementer turn on a narrow fix.
- The Claude `code-reviewer` agent shows the same bias less severely. Scope this task to Codex only to keep the diff narrow — if a Claude-side pattern emerges later, file a follow-up.

### Existing surfaces we edit

- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — the "Phase D-Codex review" template is the authoritative prompt source.
- `plugins/plan-executor/scripts/codex_review_schema.json` — verdict vocabulary `{clean, minor-findings, needs-rework}`; no schema changes.

### Non-goals

- Changing the verdict vocabulary. `minor-findings` already exists; we calibrate when it fires.
- Tuning the Claude `code-reviewer` agent. Separate concern.
- Tightening the Phase D.5 third-opinion prompt — that agent's strictness is appropriate; it is the tie-breaker.

---

## Verification

**V1 — Verdict decision ladder present in the Codex prompt.**

The updated template MUST include an explicit decision ladder, worded approximately as:
- `clean` — no issues, or only forward-looking suggestions.
- `minor-findings` — issues a human reviewer would merge with a follow-up note (style, typo, doc drift, non-load-bearing naming, comment inaccuracy, unused import).
- `needs-rework` — contract violation, acceptance-criterion miss, semantic bug, missing test for a declared verification criterion, scope inflation past the task's file allow-list.

**V2 — Two worked examples on the boundary.**

Prompt text MUST include at least two worked examples: (1) a nit (e.g. outdated comment) correctly resulting in `minor-findings`; (2) an acceptance-criterion miss correctly resulting in `needs-rework`. Examples are synthetic — NOT copies of recent real findings — to avoid cargo-culting.

**V3 — Prompt-text regression check.**

`tests/scripts/test_codex_review_prompt.py` (new, ~30 lines) asserts the "Phase D-Codex review" template contains the decision ladder headings and both worked examples. Pure string assertions; does not dispatch Codex.

**V4 — No schema regression.**

`plan_ops.py parse-reviewer-report` continues to parse a representative Codex envelope fixture (captured from a real run, checked into `tests/fixtures/`). Verdict vocabulary and envelope shape unchanged.

---

## Tasks

### TASK-015: Codex reviewer prompt tuning

- **Status:** done
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `tests/scripts/test_codex_review_prompt.py` (new)
  - `tests/fixtures/codex_review_envelope.json` (new, for V4)
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_codex_review_prompt.py tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1–V4 pass.
  - The "Phase D-Codex review" template in `dispatch-templates.md` gains a numbered verdict decision ladder and two worked examples before the JSON schema paste.
  - No changes to `codex_review_schema.json`, the Phase D.5 third-opinion prompt, or the Claude `code-reviewer` prompt.

**Description:**
Rewrite the verdict-guidance section of the Phase D-Codex review prompt to explicitly ladder `clean | minor-findings | needs-rework` with examples. The current prompt gives the verdict vocabulary but no calibration, so Codex overuses `needs-rework` on findings that are stylistic or advisory.

**Implementation notes:**
This is a prompt-engineering task, not a code task. Keep worked examples terse (one-line diff + one-line finding + verdict + one-line justification). Avoid examples that look like "fix my past TASK-004A failure" — they should be generic. If in doubt about where a finding falls, the heuristic is: "would a human reviewer block merge on this alone?" If no → `minor-findings`. If yes → `needs-rework`.

**Reversion guidance:**
Safe to revert; `git restore plugins/plan-executor/skills/implement-plan/dispatch-templates.md` restores the prior prompt. Drop the new test file and fixture.

---

## Out of Scope

- Tuning the Claude `code-reviewer` agent's prompt. Measure first; if a pattern emerges, file a follow-up.
- Adding a new verdict literal (e.g. `needs-rework-doc` vs `needs-rework-code`). The three-way vocabulary is expressive enough with correct calibration.
- Integration tests that dispatch Codex against fixtures live. Prompt tuning alone doesn't need live dispatch to verify — the fixture check in V4 is sufficient.

## Reversion guidance

Single-file template revert plus two test-only files. No cross-cutting state changes. Safe to roll back at any time.
