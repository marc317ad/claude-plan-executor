# TASK-002 — Coupling-detector step in plan-implementer prompt

## Goal

When an AC names a regex, header, or symbol pattern that appears as part of a sibling family (e.g., three coupled regexes guarding the same parser section), the plan-implementer must perform a single mandatory grep for the pattern family before declaring the task complete. This closes the friction class behind the post-mortem's Issue 2 (TASK-009 D.2a.5 sibling miss) and Issue 9 (the awaiting-user halt that fired on what was effectively "you missed one").

## Context

**The friction.** During run 20260425T041800, TASK-009's first Codex review correctly flagged that `_READ_TARGETS_HEADER_RE` and `_SYMBOL_TARGETS_HEADER_RE` required whitespace-only after the bold field label but the documented template form was `- **Read targets:** (optional, TASK-009)`. The D.2a.5 remediator fixed those two regexes but missed a third sibling site: `_iter_target_bullets`'s section-boundary detector at `plan_ops.py:8420`, which used the same restrictive pattern. The second Codex review caught the new failure path; per protocol the run hit the D.2a.5 awaiting-user pause and the user had to override the protocol with "fix it and proceed."

**Why a prompt step, not a new retry tier.** The post-mortem proposes two alternatives: (1) a coupling-detector grep step in the implementer prompt, or (2) a new `D.2a.7` narrow-coupled-retry tier. The post-mortem expresses a preference for (1) — "a tighter prompt is cheaper than a new retry tier and doesn't expand the verdict matrix." This task implements (1).

**The mechanic.** The plan-implementer prompt gains a "Coupling check" section that fires when the AC text or `Files:` block contains identifier-shaped strings (regex names like `_FOO_RE`, constant names like `ALLOWED_FOO_VERDICTS`, header strings like `**Read targets:**`). The implementer must run a grep for the symbol's pattern family across the touched files and confirm the change is applied uniformly OR justify in their report why the sibling site was excluded.

**Scope.** Spec-only — agent prompt edit. No `plan_ops.py` change. No protocol verdict-matrix change.

## Verification

- `plugins/plan-executor/agents/plan-implementer.md` gains a "Coupling check" subsection (probably under "Before declaring complete") that:
  1. Names when the check fires (AC mentions a regex/constant/header pattern).
  2. Names the grep to run (the family pattern, e.g., `\\*\\*[^*]+:\\*\\*\\s*\\$` for the TASK-009 case).
  3. Names the report obligation (mention each sibling hit + its disposition).
- A worked example in the prompt cites the TASK-009 case (or an analogous one) so the implementer recognizes the trigger shape.
- The implementer's report schema (per `dispatch-templates.md`) includes a `coupling_check` field listing siblings checked + disposition (`uniformly_applied | excluded_with_reason | not_applicable`).
- Plan-implementer dispatched against a fixture task whose AC names a regex AND whose touched file contains a sibling regex emits a `coupling_check` block in the report. (If exercising end-to-end is too heavy, an integration-level smoke check is enough.)

## Tasks

### TASK-002: Coupling-detector prompt step

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/agents/plan-implementer.md` (edit — add Coupling check subsection + report schema field)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit — surface the new report field in §Phase B)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k coupling_check`
- **Read targets:**
  - `plugins/plan-executor/agents/plan-implementer.md` — full file
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — §Phase B section
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_iter_target_bullets`
- **Acceptance criteria:**
  - `plan-implementer.md` gains a "Coupling check" step. The step is mandatory when the AC mentions any of: a regex name (suffix `_RE`), an allowlist constant (prefix `ALLOWED_`), a markdown header pattern (text inside `**...**`), or any symbol that appears ≥2 times in the touched files.
  - The step names the grep command shape and how to interpret hits ("each hit must be either uniformly modified OR excluded with a one-line reason in the report").
  - The implementer report schema (in `dispatch-templates.md` §Phase B and in `plan-implementer.md`) gains a `coupling_check` block with fields `pattern_family`, `siblings_checked` (list of `file:line`), `disposition` per sibling (`uniformly_applied | excluded_with_reason | not_applicable`).
  - A worked example in the prompt cites the TASK-009 sibling-regex case so the trigger shape is concrete.
  - A unit test (or fixture-driven smoke test) under `tests/scripts/test_plan_ops.py` parses the implementer report and asserts the `coupling_check` block round-trips through the report parser without error.
  - The new prompt step does NOT change the existing implementer verdict allowlist or D.2a routing.
- **Reversion guidance:** revert the prompt section + report-schema field; routing rules are untouched.

**Description:**
Add a mandatory "Coupling check" step to the plan-implementer prompt that fires when an AC names a regex/constant/header pattern. The implementer greps for the pattern family across touched files and reports each sibling hit's disposition. Surface a `coupling_check` block in the implementer report so the cross-reviewer (Codex) can verify uniform application.

**Implementation notes:**
- Prefer concrete trigger heuristics (suffix `_RE`, prefix `ALLOWED_`, `**...**` headers) over open-ended judgement — the implementer must KNOW when to fire.
- The report-schema field is additive; no need to bump any version constant.
- Resist scope creep: do NOT add a coupling check on the cross-reviewer side. The cross-reviewer already greps independently; doubling up adds latency without information.
