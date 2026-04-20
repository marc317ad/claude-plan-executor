# Plan: Analyst Gap Ergonomics — Template + Allow-Gaps Propagation + Soft/Hard Split

**Created:** 2026-04-20
**Status:** pending
**Base branch:** main
**Target repo:** `/mnt/d/claude-plan-executor/`

---

## Context

Three independent gaps in the `/implement-plan` pre-dispatch pipeline make `needs-enrichment` paper-cuts into hard halts, even when the user has explicitly opted into `--allow-gaps`:

1. **TASK authoring template drift.** The template used to hand-author `docs/plans/decompose_plans_tasks/build-plan-decomposer-plugin/TASK-*.md` and `docs/plans/DUAL_AGENT_Plans/TASK-*.md` omits `**Implementation notes:**` on ~60% of tasks (11 of 18 in build-plan-decomposer-plugin). `plan-analyst.md:165` treats missing Implementation notes on Claude-tier tasks as an `empty-implementation-notes` gap. There is no canonical template file in the plugin to prevent new plans from reintroducing the drift.

2. **`--allow-gaps` does not propagate to plan review.** The flag is documented at `skills/implement-plan/SKILL.md:73` as permitting `needs-enrichment` past Phase 1. Phase 1.5 (`plan-review` via Codex) reads `schedule_ok` from the persisted schedule and returns `needs-replan` whenever gaps exist, ignoring the flag. Result: the user's explicit "I accept these gaps" stops mattering at the plan-review gate. This was observed on `2026-04-20T03:26:17Z` against `docs/plans/nit_fix_plans/TASK-001_standardize_reviewer_output_schema.md` in the algorithmic_trading_system repo.

3. **Gap severity is flat.** All gap types ride in a single `gaps[]` list. The orchestrator, Codex plan-review, and the user all apply their own heuristics to decide which ones matter. Hard gaps (`stale-path`, `missing-test-command` on Claude-tier, `vague-ac`) are always ship-blockers; soft gaps (`unresolvable-test`, `empty-implementation-notes`) are advisory. Without the split, `--allow-gaps` has to mean "permit everything" rather than "permit advisory".

These are additive, backwards-compatible changes. They reduce operator friction without relaxing any structural contract in the skill.

## Goals

- Ship a canonical TASK authoring template under `plugins/plan-executor/templates/` that includes every required field plus `Implementation notes:` by default.
- Propagate `--allow-gaps` from the `/implement-plan` CLI into the Codex plan-review dispatch so the reviewer treats gap-induced `schedule_ok=false` as `approved-with-notes` instead of `needs-replan`.
- Split gap severity at analyst emission time into `gaps_soft[]` and `gaps_hard[]`; keep legacy `gaps[]` as the union for backwards compatibility. Teach `--allow-gaps` to permit soft gaps while still halting on hard gaps.

## Non-Goals

- No changes to the orchestrator state machine beyond flag propagation.
- No changes to the analyst's classification rules (codex vs claude) or batch computation.
- No changes to `fix-plan-decomposer` or `plan-decomposer`; the template is authoring-side only.
- No retroactive enrichment of existing TASK files (e.g. `docs/plans/DUAL_AGENT_Plans/TASK-00*.md` or consumer-repo plans). Template ships; backfill is out of scope.
- No removal of hard gap types. Hard-vs-soft is a classification layer on top of the existing list.

## Verification

After all three chunks land, the following must be true. Commands assume the plugin repo root as cwd.

**V1 — Template exists and satisfies analyst required-field check.**

```
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k template
```

A new test reads the template and asserts every required field (Status, Priority, Files, Test command, Acceptance criteria, Description, Implementation notes, Reversion guidance) is present as a section heading or label.

**V2 — `--allow-gaps` flag is recognized by the plan-review wrapper.**

```
venv/bin/python plugins/plan-executor/scripts/plan_codex_dispatch.py plan-review --help
```

`--allow-gaps` appears in the help text.

**V3 — `--allow-gaps` demotes soft-gap-induced needs-replan to approved-with-notes.**

```
venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -k plan_review_allow_gaps
```

Integration test uses a dry-run wrapper invocation with a schedule that carries only soft gaps; asserts the rendered prompt contains the demotion clause and that post-parse routing emits `approved-with-notes`.

**V4 — Analyst emits `gaps_hard` and `gaps_soft` alongside `gaps`.**

```
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k gaps_split
```

New tests feed schedules with mixed gap types into `parse-schedule` and assert:
- `gaps_hard[]` contains only `stale-path`, `missing-test-command`, `vague-ac`
- `gaps_soft[]` contains only `unresolvable-test`, `empty-implementation-notes`
- `gaps[]` equals `gaps_hard + gaps_soft` (order preserved from analyst input)

**V5 — `--allow-gaps` halts on hard gaps, proceeds on soft-only.**

```
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k allow_gaps_hard
```

New test asserts that a schedule with a `stale-path` gap still produces the halt signal even with `--allow-gaps` semantics applied, while a soft-only schedule proceeds.

**V6 — No regression in existing tests.**

```
venv/bin/pytest -q
```

All tests green.

---

## Tasks

### TASK-001: Ship canonical TASK authoring template

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/templates/TASK.md.template` (create) — canonical TASK template with all required + recommended fields
  - `plugins/plan-executor/templates/README.md` (create) — one-paragraph doc pointing authors at the template
  - `tests/scripts/test_plan_ops.py` — add `test_task_template_has_all_required_fields`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k template`
- **Acceptance criteria:**
  - `plugins/plan-executor/templates/TASK.md.template` exists and contains, as labeled sections or bulleted fields: `Status`, `Priority`, `Files`, `Dependencies`, `Test command`, `Acceptance criteria`, `Description`, `Implementation notes`, `Reversion guidance`.
  - The template includes a placeholder `TASK-NNN: <title>` header matching the `^TASK-\d{3}[A-Z]?:` analyst regex.
  - `plugins/plan-executor/templates/README.md` names the template, states its purpose (prevent analyst enrichment gaps), and links to `agents/plan-analyst.md` Step 7 for the canonical gap list.
  - A new test `test_task_template_has_all_required_fields` passes — reads the template file and asserts each required field appears as a section or labeled bullet.
  - No existing test regresses.

**Description:**
Author a reusable TASK template so downstream plan-authoring (manual or decomposer-driven) produces files that satisfy `plan-analyst.md` Step 2 and Step 7 without enrichment gaps. The template is authoritative documentation — other repos (e.g. algorithmic_trading_system's `nit_fix_plans/`) copy from it.

**Implementation notes:**
The template should be concrete, not abstract. Use `<placeholder>` conventions the analyst already accepts (e.g. `<file_path>` for Files entries, literal `pending` for Status). Avoid shell-operator chains in the example `Test command:` — use a single `venv/bin/pytest <path>` invocation so the example aligns with `plan-analyst.md:79-82`. Include a three-bullet `Acceptance criteria:` with measurable assertions, and a one-sentence `Implementation notes:` block so authors see what "enough" looks like. Keep the template ≤80 lines — long templates get paraphrased away.

**Reversion guidance:**
Delete `plugins/plan-executor/templates/TASK.md.template`, `plugins/plan-executor/templates/README.md`, and the new test. No other files modified.

---

### TASK-002: Propagate `--allow-gaps` into Codex plan-review

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` — add `--allow-gaps` arg to the `plan-review` subparser; thread into the rendered prompt
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — update Phase 1.5 section to pass `--allow-gaps` through when the orchestrator was invoked with it
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — update Phase 1.5 plan-review template block to document the new flag + demotion semantics
  - `tests/scripts/test_plan_codex_dispatch_integration.py` — add `test_plan_review_allow_gaps_demotes_soft_gap_verdict`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_codex_dispatch_integration.py -k plan_review_allow_gaps`
- **Acceptance criteria:**
  - `plan_codex_dispatch.py plan-review --help` lists `--allow-gaps`.
  - When `--allow-gaps` is passed, the rendered Codex prompt contains a literal demotion clause instructing the reviewer to treat `schedule_ok=false` caused solely by `gaps[]` non-empty (with no structural schedule violations) as verdict `approved-with-notes` rather than `needs-replan`.
  - When `--allow-gaps` is NOT passed, the prompt is unchanged (byte-identical to current output for a given schedule).
  - SKILL.md Phase 1.5 section shows the flag being forwarded: `plan-review ... [--allow-gaps]` in the command template, with a sentence explaining the demotion semantics and a note that hard-gap-driven `needs-replan` is still honored.
  - `dispatch-templates.md` Phase 1.5 section documents the flag, the demotion clause text, and the "structural violations still block" caveat.
  - New integration test passes: dry-run a plan-review dispatch with `--allow-gaps` against a schedule containing only `empty-implementation-notes`; assert the rendered prompt carries the demotion clause and the parsed verdict routes as `approved-with-notes`.
  - Existing plan-review tests all green.

**Description:**
Make `--allow-gaps` a first-class signal that flows from the orchestrator CLI through Phase 1.5. Today it stops at Phase 1 (analyst outcome) and Codex re-raises the same gap findings at Phase 1.5, creating a circular halt. After this task, a user explicitly accepting gaps sees the full pipeline proceed to batch dispatch.

**Implementation notes:**
The prompt demotion clause should be precise: demote only when `schedule_ok=false`'s sole cause is `gaps[]` non-empty with no structural schedule violations (e.g. unknown task IDs in batches, duplicate IDs, missing required top-level fields). Codex already distinguishes structural from advisory in its summary; the clause just reorders the verdict mapping. Do not mutate the persisted schedule. Flag the demotion in the returned `summary` so the run-log `plan_review_done` event carries visible evidence. In SKILL.md, add the flag next to `--skip-plan-review` in the argparse block at line 70-76 and update the forwarding step around line 173 to conditionally append `--allow-gaps` to the wrapper command.

**Reversion guidance:**
Drop the `--allow-gaps` arg from `plan_codex_dispatch.py`'s `plan-review` subparser, remove the conditional prompt clause, remove the SKILL.md + dispatch-templates.md additions, and delete the new integration test. No schema changes to revert.

---

### TASK-003: Split gap severity into hard and soft

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/agents/plan-analyst.md` — amend Step 7 to classify each gap type as hard or soft at emission time; document the two new JSON fields
  - `plugins/plan-executor/scripts/plan_ops.py` — in `parse-schedule` (and `compute-schedule`, `write-schedule`), populate `gaps_hard[]` and `gaps_soft[]` from the input `gaps[]`; preserve `gaps[]` as the union; add `--allow-gaps` semantics check that only permits soft-only
  - `tests/scripts/test_plan_ops.py` — add `test_parse_schedule_gaps_split`, `test_parse_schedule_allow_gaps_hard_still_halts`, `test_parse_schedule_allow_gaps_soft_only_proceeds`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k gaps`
- **Acceptance criteria:**
  - `plan-analyst.md` Step 7 enumerates the hard set (`stale-path`, `missing-test-command`, `vague-ac`) and the soft set (`unresolvable-test`, `empty-implementation-notes`) under the existing Gap types list; explicitly notes unknown gap types are treated as hard by downstream consumers.
  - `parse-schedule --json` output includes both `gaps_hard[]` and `gaps_soft[]` arrays, with `gaps[]` retained as the source-of-truth union for backwards compatibility.
  - When `--allow-gaps` semantics are applied (via a new `allow_gaps_mode` field in the parser result or equivalent signal), a schedule with any hard gap still produces the halt signal; a soft-only schedule proceeds.
  - `parse-schedule` accepts both input shapes: bare `gaps[]` (legacy) and a pre-split `{gaps_hard, gaps_soft, gaps}` trio. Emits a `warnings` entry when the legacy shape is re-classified.
  - Three new tests pass: the split test, the hard-halt test, the soft-proceed test.
  - Existing `parse-schedule` / `compute-schedule` / `write-schedule` tests all green.

**Description:**
Formalize the hard-vs-soft distinction that operators and reviewers already carry implicitly in their heads. After this task, `--allow-gaps` has a sharp semantics ("permit soft gaps") rather than a blanket one ("permit all gaps"), and Codex plan-review can route by severity.

**Implementation notes:**
Put the classification table in a single module-level dict in `plan_ops.py` (e.g. `GAP_SEVERITY = {"stale-path": "hard", "missing-test-command": "hard", "vague-ac": "hard", "unresolvable-test": "soft", "empty-implementation-notes": "soft"}`) so analyst text and helper code agree on one source. Unknown gap types default to `hard` — fail-safe toward blocking. Do not break TASK-002: when TASK-002's `--allow-gaps` flag is live, its demotion clause should reference the hard/soft split from this task if both have landed, else fall back to the blanket demotion. To keep the tasks independent in either order, TASK-002's clause should be phrased as "demote soft-gap-only cases" — which is a no-op (blanket) until this task populates the distinction.

**Reversion guidance:**
Remove `gaps_hard`/`gaps_soft` emission from `parse-schedule`, revert the Step 7 analyst text, delete the classification dict, and delete the three new tests. `gaps[]` consumers keep working. TASK-002's demotion clause degrades to blanket behavior.

---

## Batching & execution order

All three tasks are independent and operate on disjoint files:

- TASK-001: `plugins/plan-executor/templates/` + `tests/scripts/test_plan_ops.py` (new test only — disjoint from TASK-003's new tests)
- TASK-002: `plugins/plan-executor/scripts/plan_codex_dispatch.py` + two skill/dispatch-template files + `tests/scripts/test_plan_codex_dispatch_integration.py`
- TASK-003: `plugins/plan-executor/agents/plan-analyst.md` + `plugins/plan-executor/scripts/plan_ops.py` + `tests/scripts/test_plan_ops.py`

Conflict note: TASK-001 and TASK-003 both append new tests to `tests/scripts/test_plan_ops.py`. Different test functions, zero line-level overlap, but the file is a shared lock — so batch the two sequentially, not in parallel. Analyst should place TASK-001 and TASK-003 in different batches; TASK-002 can run in either batch.

Recommended batch layout:
- Batch 1: TASK-001 + TASK-002 (parallel, disjoint files)
- Batch 2: TASK-003 (serial after TASK-001 due to shared test file)

## Out of Scope

- **Retroactive enrichment of consumer-repo plans.** Backfilling `docs/plans/nit_fix_plans/TASK-*.md` in the algorithmic_trading_system repo, or enriching `DUAL_AGENT_Plans/TASK-*.md` in this repo, is a one-time operator action using the new template — not part of this plan.
- **New `plan-enricher` agent.** Recommendation 5 from the design discussion (give an agent plan-file write permission to auto-synthesize Implementation notes) is tracked separately and will be executed at a later date.
- **Analyst classification rules** (claude vs codex, batch computation). Untouched.
- **`fix-plan-decomposer`** agent behavior. Untouched.

## Reversion guidance

Each TASK carries per-task reversion steps above. If all three need rolling back together, the order is: TASK-003 first (remove the split — keeps `gaps[]` compatible), then TASK-002 (remove flag propagation), then TASK-001 (remove template files). No state outside git is modified; no data migrations; no schema version bumps.
