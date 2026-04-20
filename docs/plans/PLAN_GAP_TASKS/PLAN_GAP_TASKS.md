# Plan: Analyst Gap Ergonomics — Post-TASK-025 Finish

**Created:** 2026-04-20
**Status:** pending
**Base branch:** main
**Target repo:** `/mnt/d/claude-plan-executor/`
**Anchor commit:** `a3a947d` (TASK-025 — `plan-author` subagent landed)
**Supersedes:** `docs/plans/PLAN_ANALYST_GAP_ERGONOMICS_2026-04-20.md`

---

## Context

`PLAN_ANALYST_GAP_ERGONOMICS_2026-04-20.md` identified three independent paper-cuts that turned `needs-enrichment` outcomes into hard halts even when the operator had explicitly opted into `--allow-gaps`:

1. **Template drift.** The ad-hoc TASK authoring template used across `docs/plans/DUAL_AGENT_Plans/TASK-*.md` and `docs/plans/decompose_plans_tasks/build-plan-decomposer-plugin/TASK-*.md` omitted `**Implementation notes:**` on ~60% of task blocks. `plan-analyst.md:165` treats missing Implementation notes on Claude-tier tasks as an `empty-implementation-notes` gap. No canonical template exists in the plugin to prevent new plans from reintroducing the drift.
2. **`--allow-gaps` did not propagate to plan review.** The flag is documented at `SKILL.md:74` as permitting `needs-enrichment` past Phase 1. Phase 1.5 (`plan-review` via Codex) reads `schedule_ok` from the persisted schedule and returns `needs-replan` whenever gaps exist, ignoring the flag. Result: the operator's explicit "I accept these gaps" stops mattering at the plan-review gate.
3. **Gap severity is flat.** All gap types ride in a single `gaps[]` list. The orchestrator, Codex plan-review, and the user all apply their own heuristics to decide which ones matter. Hard gaps (`stale-path`, `missing-test-command` on Claude-tier, `vague-ac`) are always ship-blockers; soft gaps (`unresolvable-test`, `empty-implementation-notes`) are advisory. Without the split, `--allow-gaps` has to mean "permit everything" rather than "permit advisory."

**TASK-025 (`plan-author` subagent, commit `a3a947d`) reshapes but does not eliminate these pains.** Phase 1.5's `needs-replan` branch now dispatches `plan-author` (Edit/Write-authorized sibling of `plan-analyst`) to apply Codex findings to the plan file in place, then re-validates via `plan-analyst`, then re-runs Codex `plan-review`. Second verdict binds. Auto-revise is default-on; `--no-auto-revise` opts out.

Residual impact on each original pain:

| Pain | Post-TASK-025 state |
|---|---|
| Template drift | `plan-author` repairs gaps at revision time, but token cost and round-trip latency are real. Upstream prevention (a canonical template) still pays for every gap avoided. |
| `--allow-gaps` not propagated | Operator's opt-in still stops mattering at Phase 1.5. Codex returns `needs-replan`, `plan-author` runs, re-review runs. The user explicitly said "I accept these gaps" but pays for the auto-revise round-trip anyway. |
| Flat gap severity | `plan-author` is invoked identically on any gap-driven `needs-replan` — soft gaps like `empty-implementation-notes` trigger the same dispatch as hard gaps like `stale-path`. The demotion added by TASK-003 below is only safe if severity is first classified. |

This plan finishes the three original tasks, adapted to the post-TASK-025 reality.

## Goals

- Ship a canonical TASK authoring template under `plugins/plan-executor/templates/` that satisfies every required field plus `Implementation notes:` by default — reducing the number of gap-driven `needs-replan`s that reach `plan-author` at all.
- Classify each gap entry as `hard` or `soft` inline via a new `gaps[i].severity` field. Source of truth lives in `plan-analyst.md` Step 7; `plan_ops.py` exposes a mirror constant (`GAP_SEVERITY`) with a regression test that guards drift between the two.
- Propagate `--allow-gaps` from the `/implement-plan` CLI into the Codex `plan-review` wrapper. When set AND the schedule carries soft-severity gaps only (no hard gaps, no structural schedule violations), Codex demotes `needs-replan` → `approved-with-notes`, bypassing the `plan-author` dispatch for the opted-in case. Hard gaps still trigger the full auto-revise path.

## Non-Goals

- **No new top-level schedule fields.** `ALLOWED_SCHEDULE_TOP_LEVEL` in `plan_ops.py:173` is the canonical whitelist (`{outcome, tasks, batches, gaps, risks}`) and is enforced as halt-on-unknown by `_validate_schedule`. Severity lives inline on each `gaps[i]` entry; this is schema-compatible because gap entries have no nested-field allowlist.
- **No change to `plan-author` behavior.** TASK-025 is out of scope. The demotion added here short-circuits the decision to dispatch `plan-author`; it does not modify `plan-author.md`, its rules, or its write scope.
- **No change to analyst classification rules.** Claude-vs-Codex tiering, batch computation, and risk detection stay as-is.
- **No retroactive enrichment of consumer-repo plans.** Backfilling `docs/plans/nit_fix_plans/TASK-*.md` in other repos, or enriching `docs/plans/DUAL_AGENT_Plans/TASK-*.md` in this repo, is a one-time operator action using the new template — not part of this plan.
- **No removal of hard gap types.** Hard-vs-soft is a classification layer on top of the existing list.

## Verification

After all three tasks land, the following must be true. Commands assume the plugin repo root as cwd.

**V1 — Template exists and satisfies analyst required-field check.**

```
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k template
```

A new test reads the template and asserts every required field (`Status`, `Priority`, `Files`, `Test command`, `Acceptance criteria`, `Description`, `Implementation notes`, `Reversion guidance`) is present as a section heading or label.

**V2 — Every gap entry emitted by the analyst carries a `severity` field.**

```
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k gap_severity
```

Tests feed a hand-built schedule with mixed gap types into `parse-schedule` and assert: `hard` severity is assigned to `stale-path`, `missing-test-command`, `vague-ac`; `soft` severity is assigned to `unresolvable-test`, `empty-implementation-notes`; unknown gap types default to `hard`. The classification module-level constant `GAP_SEVERITY` in `plan_ops.py` matches the table documented in `plan-analyst.md` Step 7 (a test reads both and asserts equality).

**V3 — `--allow-gaps` flag is recognized by the plan-review wrapper.**

```
venv/bin/python plugins/plan-executor/scripts/plan_codex_dispatch.py plan-review --help
```

`--allow-gaps` appears in the help text.

**V4 — `--allow-gaps` demotes soft-gap-only `needs-replan` to `approved-with-notes`.**

```
venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -k plan_review_allow_gaps
```

Integration test uses a dry-run wrapper invocation against a schedule whose `gaps[]` contain only `soft` severity entries; asserts the rendered prompt contains the demotion clause and that post-parse routing emits `approved-with-notes`.

**V5 — `--allow-gaps` halts on hard gaps, proceeds on soft-only.**

```
venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -k plan_review_allow_gaps_hard
```

Integration test asserts that a schedule carrying any `hard` gap still produces the full `plan-author` auto-revise path even with `--allow-gaps` applied (the demotion clause does not fire), while a soft-only schedule skips `plan-author` and proceeds directly to Phase 2.

**V6 — No regression in existing tests.**

```
venv/bin/pytest -q
```

All tests green.

---

## Tasks

### TASK-001: Ship canonical TASK authoring template

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/templates/TASK.md.template` (create) — canonical TASK template with every required field plus `Implementation notes:` by default
  - `plugins/plan-executor/templates/README.md` (create) — one-paragraph doc pointing authors at the template
  - `tests/scripts/test_plan_ops.py` — add `test_task_template_has_all_required_fields`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k template`
- **Acceptance criteria:**
  - `plugins/plan-executor/templates/TASK.md.template` exists and contains, as labeled sections or bulleted fields: `Status`, `Priority`, `Files`, `Dependencies`, `Test command`, `Acceptance criteria`, `Description`, `Implementation notes`, `Reversion guidance`.
  - The template includes a placeholder `TASK-NNN: <title>` header matching the `^TASK-\d{3}[A-Z]?:` regex used by `plan-analyst`.
  - `plugins/plan-executor/templates/README.md` names the template, states its purpose (prevent analyst enrichment gaps and reduce the frequency of `plan-author` auto-revise round-trips), and links to `plan-analyst.md` Step 7 for the canonical gap list.
  - A new test `test_task_template_has_all_required_fields` passes — reads the template file and asserts each required field appears as a section or labeled bullet.
  - No existing test regresses.

**Description:**
Author a reusable TASK template so downstream plan-authoring (manual or decomposer-driven) produces files that satisfy `plan-analyst.md` Step 2 and Step 7 without enrichment gaps. The template is authoritative documentation — other repos (e.g., `algorithmic_trading_system`'s `nit_fix_plans/`) copy from it. This is upstream prevention; `plan-author` handles downstream repair, but every gap avoided here saves a Codex plan-review → `plan-author` → re-analysis → re-review round-trip.

**Implementation notes:**
Concrete, not abstract. Use `<placeholder>` conventions the analyst already accepts (e.g., `<file_path>` for Files entries, literal `pending` for Status). Avoid shell-operator chains in the example `Test command:` — use a single `venv/bin/pytest <path>` invocation so the example aligns with `plan-analyst.md:79-82`. Include a three-bullet `Acceptance criteria:` with measurable assertions, and a one-sentence `Implementation notes:` block so authors see what "enough" looks like. Keep the template ≤80 lines — long templates get paraphrased away. Sibling `code-reviewer.md.template` already lives at `plugins/plan-executor/templates/`; match its directory convention. The README should explicitly call out that `plan-author` (TASK-025) edits plan files downstream but cannot fix what was never in the plan — template adherence reduces `plan-author` dispatch frequency.

**Reversion guidance:**
Delete `plugins/plan-executor/templates/TASK.md.template`, `plugins/plan-executor/templates/README.md`, and the new test. No other files modified.

---

### TASK-002: Classify gap severity via inline `gaps[i].severity`

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/agents/plan-analyst.md` — amend Step 7 to classify each gap type as `hard` or `soft` at emission time; amend the JSON contract (around line 280) to document the new required `severity` field on each `gaps[]` entry
  - `plugins/plan-executor/scripts/plan_ops.py` — add module-level `GAP_SEVERITY` constant and a `classify_gap_severity(gap_type: str) -> str` helper that returns `"hard"` or `"soft"`, defaulting to `"hard"` for unknown types; surface the classification in `parse-schedule`'s result by populating any missing `severity` fields on `gaps[i]` before emission (legacy schedules without severity get a `warnings` entry noting the fallback classification)
  - `tests/scripts/test_plan_ops.py` — add `test_gap_severity_classification_table_matches_analyst_spec`, `test_parse_schedule_gaps_severity_passthrough`, `test_parse_schedule_gaps_severity_legacy_fallback`
- **Dependencies:** 001
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -k gap_severity`
- **Acceptance criteria:**
  - `plan-analyst.md` Step 7 enumerates the hard set (`stale-path`, `missing-test-command`, `vague-ac`) and the soft set (`unresolvable-test`, `empty-implementation-notes`); explicitly notes unknown gap types are treated as `hard` by downstream consumers.
  - `plan-analyst.md` Report format → JSON contract (around line 280) documents the new required `severity: "hard" | "soft"` field on every `gaps[]` entry, alongside the existing `type`, `task_id`, `detail` fields.
  - `plan_ops.py` declares `GAP_SEVERITY = {"stale-path": "hard", "missing-test-command": "hard", "vague-ac": "hard", "unresolvable-test": "soft", "empty-implementation-notes": "soft"}` as a module-level constant, and `classify_gap_severity` exposes it with `"hard"` as the fail-safe default for unknown types.
  - `parse-schedule` preserves an analyst-emitted `severity` field unchanged; on a legacy schedule (entries without `severity`), it populates the field via `classify_gap_severity` and appends a `warnings` entry noting the backfill, matching the shape used by the `task_id` → `id` alias warning at `plan_ops.py:529-533`.
  - No change to `ALLOWED_SCHEDULE_TOP_LEVEL` — `gaps_hard[]`/`gaps_soft[]` are NOT added as top-level fields. Severity stays inline on each gap entry.
  - Three new tests pass. Existing `parse-schedule` / `compute-schedule` / `write-schedule` tests stay green.

**Description:**
Formalize the hard-vs-soft distinction that operators and reviewers already carry implicitly in their heads. After this task, `gaps[i].severity` is observable in every schedule, and TASK-003's `--allow-gaps` demotion has a sharp input to reason over ("permit soft gaps") rather than a blanket one ("permit all gaps").

**Implementation notes:**
Inline severity (not top-level arrays) is the one correct choice: `plan_ops.py:173` sets `ALLOWED_SCHEDULE_TOP_LEVEL = {"outcome", "tasks", "batches", "gaps", "risks"}` and `_validate_schedule` halts on unknown top-level fields (`plan_ops.py:474-483`). Adding top-level `gaps_hard[]` / `gaps_soft[]` would make every schedule that carries them fail write-schedule's strict validator. There is no `ALLOWED_GAP_FIELDS` allowlist today, so extending `gaps[i]` with a new field is schema-transparent.

Put `GAP_SEVERITY` and `classify_gap_severity` near the existing `ALLOWED_*` constants in `plan_ops.py` (around lines 170-180) so future readers find all schema constants together. The regression test (`test_gap_severity_classification_table_matches_analyst_spec`) should parse the Step 7 classification table out of `plan-analyst.md` and assert byte-for-byte equality with the `GAP_SEVERITY` constant — so drift between doc and code is impossible.

For the legacy-fallback test: feed `parse-schedule` a schedule with `gaps[]` entries missing the `severity` field and assert the parser fills them via `classify_gap_severity` AND emits a `warnings[*]` entry naming the backfilled entries. This mirrors the pattern used by the legacy `task_id` alias at `plan_ops.py:529-533` and keeps the forward-compat surface small.

TASK-003's demotion clause relies on this classification. If TASK-003 ships before TASK-002 (it should not, per this plan's dependency chain), the demotion clause must degrade to blanket "demote on any gap" to stay safe.

**Reversion guidance:**
Remove the `GAP_SEVERITY` constant, `classify_gap_severity` helper, and the `severity`-backfill branch in `parse-schedule`. Revert Step 7 and the JSON contract text in `plan-analyst.md`. Delete the three new tests. `gaps[]` consumers keep working because `severity` is additive — existing code that reads `type` + `detail` is unaffected. TASK-003's demotion degrades to blanket "no-op" (since it gates on severity).

---

### TASK-003: Propagate `--allow-gaps` into Codex plan-review with severity-aware demotion

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` — add `--allow-gaps` arg to the `plan-review` subparser (around line 1527-1553); thread the flag into `render_plan_review_prompt` (around line 270-325) as an optional demotion clause
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — update Phase 1.5 section (around lines 157-193) to forward `--allow-gaps` to the wrapper when the orchestrator was invoked with it; document the demotion semantics and the explicit short-circuit around `plan-author`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — update Phase 1.5 block (around lines 15-51) to document the new wrapper flag, the demotion clause text, and the "hard gaps still trigger plan-author" caveat
  - `tests/scripts/test_plan_codex_dispatch_integration.py` — add `test_plan_review_allow_gaps_demotes_soft_gap_verdict`, `test_plan_review_allow_gaps_hard_gaps_block`, `test_plan_review_without_allow_gaps_unchanged`
- **Dependencies:** 002
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_codex_dispatch_integration.py -k plan_review_allow_gaps`
- **Acceptance criteria:**
  - `plan_codex_dispatch.py plan-review --help` lists `--allow-gaps`.
  - When `--allow-gaps` is passed AND the persisted schedule's `gaps[]` contains only `severity: "soft"` entries (no hard gaps, no structural schedule violations), the rendered Codex prompt contains a literal demotion clause instructing the reviewer to treat `schedule_ok=false` as verdict `approved-with-notes` rather than `needs-replan`, and to surface the demotion in its `summary` string.
  - When `--allow-gaps` is passed but the schedule carries any `severity: "hard"` gap, the rendered prompt does NOT contain the demotion clause — the reviewer proceeds with standard verdict selection and the orchestrator dispatches `plan-author` on `needs-replan` per the existing Phase 1.5 routing.
  - When `--allow-gaps` is NOT passed, the rendered prompt is byte-identical to current output for a given schedule (no demotion clause, no other changes).
  - `SKILL.md` Phase 1.5 section shows the flag being forwarded (`plan-review ... [--allow-gaps]` in the command template) and explicitly documents that a demoted `approved-with-notes` verdict bypasses the `plan-author` dispatch — the auto-revise path is intentionally skipped when the operator has opted in to soft gaps.
  - `dispatch-templates.md` Phase 1.5 block documents the flag, the demotion clause text verbatim, and the "hard gaps still trigger plan-author auto-revise" caveat.
  - Three new integration tests pass. Existing plan-review tests stay green.

**Description:**
Thread `--allow-gaps` from the `/implement-plan` CLI through the Codex `plan-review` dispatch so a user's explicit "I accept these gaps" flows past Phase 1.5. The demotion is severity-aware (hard gaps still block) and short-circuits `plan-author` — the user who opted in does not pay for an auto-revise round-trip that would rewrite gaps they already accepted.

**Implementation notes:**
The demotion clause text should be precise: demote only when (a) `--allow-gaps` is set by the operator, (b) `schedule_ok=false` is caused solely by `gaps[]` entries whose every `severity` is `"soft"`, and (c) no structural schedule violations exist (duplicate IDs, unknown task IDs in batches, orphan deps, etc. — Codex already distinguishes structural from advisory in its verdict reasoning). Do not mutate the persisted schedule. Codex carries the demotion in its `summary` string so the run-log `plan_review_done` event has visible evidence of the verdict selection.

Inspecting gap severity from inside the prompt requires the wrapper to read the schedule's `gaps[]` when rendering — the schedule JSON is already passed to `render_plan_review_prompt` via `schedule_json` (string), so parsing it in-wrapper adds one `json.loads` call plus a helper that checks `all(g.get("severity") == "soft" for g in gaps) if gaps else False`. Place this check in `cmd_plan_review` (around `plan_codex_dispatch.py:1281-1461`) before rendering and pass the boolean result as a new kwarg to `render_plan_review_prompt`.

In `SKILL.md`, thread the flag next to `--skip-plan-review` in the argparse block at lines 69-76 and in the wrapper dispatch at lines 173-179. The Phase 1.5 routing table at lines 189-193 needs an explicit "Demotion note" paragraph right after the `needs-replan` row, stating: "When `--allow-gaps` was passed AND the first Codex `plan-review` returns `approved-with-notes` with a demotion summary, the orchestrator logs `plan_review_done` with a `demoted=true` marker and proceeds directly to Phase 2 — `plan-author` is NOT dispatched. Hard-gap-driven `needs-replan` still triggers the full Phase 1.5a auto-revise path."

Handle the case where plan-author ran (on a prior hard-gap-driven needs-replan that was fixed and re-reviewed): the second review may still carry soft gaps in the revised schedule. If `--allow-gaps` is set, the second review's demotion applies identically — there is no "one-shot" state to track. The demotion is a pure function of (`--allow-gaps`, schedule gap severities, no structural violations).

Cross-plan dependency halts at Phase 0 (`check-plan-deps`, `SKILL.md:107-113`) are unaffected: the flag does not propagate there, and SKILL.md already states that cross-plan deps are hard blockers.

**Reversion guidance:**
Drop the `--allow-gaps` arg from `plan_codex_dispatch.py`'s `plan-review` subparser, remove the conditional demotion clause in `render_plan_review_prompt`, remove the SKILL.md + `dispatch-templates.md` additions, and delete the three new integration tests. No schema changes to revert; the persisted schedule is untouched. TASK-025's `plan-author` path behaves exactly as it does today — the revert restores the pre-TASK-003 state where `--allow-gaps` only governs Phase 1 analyst outcome.

---

## Batching & execution order

The dependency chain forces strict sequential execution — one task per batch:

- **Batch 1:** TASK-001 (template). No deps. Pure additive.
- **Batch 2:** TASK-002 (gap severity). Depends on 001. Shared test file `tests/scripts/test_plan_ops.py` with 001 serializes batching even without the explicit dep; the dep is declared for clarity.
- **Batch 3:** TASK-003 (plan-review demotion). Depends on 002. The demotion clause needs `gaps[i].severity` to classify soft-vs-hard; without TASK-002 the demotion would have to use a blanket policy, which is unsafe.

File disjointness check:

- TASK-001: `plugins/plan-executor/templates/*` + `tests/scripts/test_plan_ops.py` (append-only)
- TASK-002: `plugins/plan-executor/agents/plan-analyst.md` + `plugins/plan-executor/scripts/plan_ops.py` + `tests/scripts/test_plan_ops.py`
- TASK-003: `plugins/plan-executor/scripts/plan_codex_dispatch.py` + `plugins/plan-executor/skills/implement-plan/SKILL.md` + `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` + `tests/scripts/test_plan_codex_dispatch_integration.py`

The analyst will compute file locks per batch; sequential execution via the declared dependency chain satisfies the user's "automatic sequential execution" requirement regardless of file overlap heuristics.

## Out of Scope

- **Retroactive enrichment of consumer-repo plans.** Backfilling `docs/plans/nit_fix_plans/TASK-*.md` in `algorithmic_trading_system` or enriching `docs/plans/DUAL_AGENT_Plans/TASK-*.md` in this repo is a one-time operator action using the new template — not part of this plan.
- **`plan-author` behavior changes.** TASK-025 is stable; this plan short-circuits the dispatch on opted-in soft-gap paths but does not touch the subagent spec, its rules, or its write scope.
- **Replacing `plan-enricher` as a concept.** The original plan mentioned a hypothetical `plan-enricher` agent for auto-synthesizing implementation notes; TASK-025 subsumes that idea, and no new agent is proposed here.
- **Analyst classification rules** (Claude vs Codex tiering, batch computation). Unchanged.
- **`fix-plan-decomposer` agent behavior.** Unchanged.
- **Adding top-level `gaps_hard[]` / `gaps_soft[]` to the schedule.** Rejected — violates `ALLOWED_SCHEDULE_TOP_LEVEL`. Consumer convenience arrays can be computed on-read from `gaps[i].severity` if needed, but are not part of this plan.

## Reversion guidance

Each TASK carries per-task reversion steps above. If all three need rolling back together, the order is: **TASK-003 first** (remove the `--allow-gaps` prompt-side demotion — keeps Phase 1.5 routing unchanged), then **TASK-002** (remove the severity classification — keeps `gaps[]` schema-compatible with pre-plan consumers because `severity` is additive), then **TASK-001** (remove template files — authoring-side only). No state outside git is modified; no data migrations; no schema version bumps. TASK-025's `plan-author` path continues to operate exactly as before.
