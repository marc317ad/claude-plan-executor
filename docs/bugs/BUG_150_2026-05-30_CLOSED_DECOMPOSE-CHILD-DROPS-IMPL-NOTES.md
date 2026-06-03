---
bug_id: 150
status: CLOSED
group: DECOMPOSE-CHILD-DROPS-IMPL-NOTES
severity: minor
source_fix_id: null
source_plan: null
source_date: '2026-05-30'
origin: BUG-149 reviewer nit (run 20260530T210941)
decomposed_at: '2026-05-30'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/scripts/plan_codex_dispatch.py
content_fingerprint: null
change_history: []
---

# BUG-150: Codex implement and review prompts diverge on absent implementation-notes handling

**Status:** CLOSED
**Severity:** minor
**Group:** DECOMPOSE-CHILD-DROPS-IMPL-NOTES
**Depends on:** none
**Test command:** `none`

## Acceptance criteria
  - Fix described in ## Problem is applied and verified.

## Problem

render_implement_prompt (plan_codex_dispatch.py ~L485-490) now normalizes absent/sentinel implementation_notes (none/n-a/empty) to the verbose fallback 'None provided -- follow existing patterns in the target files.', but render_review_prompt (~L691) uses a different fallback '(none provided)' and does NOT apply the sentinel normalization. The two dispatch surfaces diverge in both fallback wording and sentinel handling; a child rendered with the literal none sentinel shows the reviewer a bare none. Harmless for review correctness and pre-exists BUG-149, but surfaced during its re-review.

## Recommended fix

Factor the sentinel-normalization into a small shared helper (e.g. _normalize_impl_notes(task) -> str) used by both render_implement_prompt and render_review_prompt so implement and review prompts agree on absent-notes handling.

## Reversion guidance

Revert the changes described in Recommended fix.

## Run history

### Run 20260603T020528 — CLOSED
- **Stage:** D.3 commit
- **Files changed:** plugins/plan-executor/scripts/plan_codex_dispatch.py, tests/scripts/test_codex_review_prompt.py
- **Reviewer verdict:** ship
- **Reviewer advisories:** Nits filed as follow-ups: description fallback at plan_codex_dispatch.py L692 still bare (none provided); sentinel set lacks n-a hyphen variant; plan_gemini_dispatch.py has the same divergence. Pre-existing unrelated test failure test_d5_dispatch_template_and_skill_forward_wrapper_checks noted for separate filing.
