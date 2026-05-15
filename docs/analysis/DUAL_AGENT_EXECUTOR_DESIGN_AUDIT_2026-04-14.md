# Dual-Agent Plan Executor Design Audit

**Date:** 2026-04-14  
**Scope:** Review `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` against the shipped Phase 1-4 artifacts, with Phase 5 failures interpreted through `docs/analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md`.

## Executive Summary

Phases 1-4 are **not yet aligned cleanly with the design contract**. The broad architecture is in place: there is a plan analyst, a plan implementer, a Codex wrapper, a plan-ops helper, a run-log schema, dispatch templates, and a sample fixture. However, several parts of the implementation and fixture were built against a different contract than the design doc.

The result is that Phase 5 is failing for two reasons:

1. **True Phase 5 runtime bugs** exist, especially the Codex implement-path scope cleanup bug already captured in the postmortem.
2. **Earlier contract drift from Phases 1-4** means the system cannot reliably execute the documented protocol even before the runtime bug is hit.

The most important conclusion is: **do not treat this as a Phase 5-only stabilization problem**. The current repo state still needs a Phase 1-4 contract alignment pass.

## What Matches the Design

The following design elements are materially present:

- The core artifact set from the plan exists: `.claude/agents/plan-analyst.md`, `.claude/agents/plan-implementer.md`, `.claude/skills/implement-plan/SKILL.md`, `scripts/plan_codex_dispatch.py`, `scripts/codex_implement_schema.json`, and `scripts/codex_review_schema.json`.
- The overall workflow shape matches the intended system: preflight, analyst-produced schedule, batch execution, asymmetric cross-review, task-level commit path, and run-log/event concepts.
- The plan-implementer agent is close to the intended contract and is one of the more internally consistent pieces. Its rules around bounded scope, no git mutation, and explicit failure reporting match the design well.
- The Codex wrapper uses structured schemas for both implement and review, which is faithful to the “machine-readable handoff” principle in the design.
- `plan_ops.py` covers many of the expected subcommands and gives the orchestrator a stdlib-only state-management surface.

## Confirmed Design Gaps

### P0: Analyst JSON contract and `plan_ops.py parse-schedule` still disagree

The design doc defines analyst JSON with `tasks[*].id` and `batches[*].index` ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:312](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:312), [docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:327](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:327)). The shipped `plan-analyst` agent emits exactly that shape ([.claude/agents/plan-analyst.md:275](.claude/agents/plan-analyst.md:275), [.claude/agents/plan-analyst.md:296](.claude/agents/plan-analyst.md:296)).

But `scripts/plan_ops.py parse-schedule` requires `task_id` and `batch_index` instead ([scripts/plan_ops.py:250](scripts/plan_ops.py:250), [scripts/plan_ops.py:263](scripts/plan_ops.py:263)).

This is the central contract break. It means the documented analyst handoff cannot be consumed by the documented orchestrator helper.

### P0: The sample fixture is built against a different plan schema than the design

The design requires `## Goal`, `## Context`, `## Verification`, plus per-task `Priority`, `Description`, and `Reversion guidance` ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:148](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:148), [docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:165](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:165), [docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:203](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:203)).

`docs/plans/sample_phase4.md` uses `## Purpose`, `## How to run`, and `## Expected outcome`; it omits `## Verification`, and each task omits `Priority`, `Description`, and `Reversion guidance` ([docs/plans/sample_phase4.md:7](docs/plans/sample_phase4.md:7), [docs/plans/sample_phase4.md:23](docs/plans/sample_phase4.md:23), [docs/plans/sample_phase4.md:44](docs/plans/sample_phase4.md:44)).

It also includes `- **Agent:** ...`, which contradicts the design principle that routing is classifier-owned, not plan-authored ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:20](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:20), [docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:742](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:742), [docs/plans/sample_phase4.md:47](docs/plans/sample_phase4.md:47)).

This means the main end-to-end fixture is not actually exercising the published schema.

### P0: Codex implement-path scope validation is structurally unsafe in parallel mode

This is correctly identified in the postmortem, and the code confirms it. `validate_scope()` compares the full current working tree against `allowed_files` with no pre-run baseline on the implement path ([scripts/plan_codex_dispatch.py:414](scripts/plan_codex_dispatch.py:414), [scripts/plan_codex_dispatch.py:652](scripts/plan_codex_dispatch.py:652)).

That makes parallel Codex tasks delete each other’s untracked outputs and orchestrator state. The postmortem demonstrates the failure in practice.

### P0: Review-path scope cleanup is also unsafe for the same class of reason

The review path has a similar design flaw. After Codex review runs, it snapshots changed files and treats everything outside `review_files` as unexpected, then restores/deletes it ([scripts/plan_codex_dispatch.py:875](scripts/plan_codex_dispatch.py:875), [scripts/plan_codex_dispatch.py:887](scripts/plan_codex_dispatch.py:887)).

There is no pre-review baseline. In the intended workflow, reviews occur while other uncommitted task outputs, plan mutations, schedule sidecars, and run-log files may still exist. That means the review wrapper can also delete legitimate sibling state.

This is not called out in the postmortem only because Scenario 7 died earlier on implement-path cleanup. It is still a real blocker for Phase 5.

### P0: Timeout recovery in the Codex wrapper is too destructive

On implement timeout, the wrapper runs `git checkout -- .` and `git clean -fd` across the repo ([scripts/plan_codex_dispatch.py:608](scripts/plan_codex_dispatch.py:608)).

That violates the design’s bounded-recovery model. It can destroy unrelated local work, orchestrator state files, and sibling task outputs. Even if Phase 5 passes after the baseline fix, this remains a ship blocker for real use.

### P1: Task status vocabulary is inconsistent across the design, helpers, and fixture

The design schema uses `pending | in-progress | done | failed | blocked | skipped` ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:165](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:165)).

`plan_ops.py` accepts `open`, not `pending` ([scripts/plan_ops.py:40](scripts/plan_ops.py:40)). The sample fixture also uses `open` ([docs/plans/sample_phase4.md:46](docs/plans/sample_phase4.md:46)).

This is more than wording drift: any plan that follows the published schema exactly is not using the same status vocabulary as the helper code and tests.

### P1: `batch-next` does not actually “take the next batch from the schedule”

The design says to take the next group of tasks from the analyst’s schedule ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:796](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:796), [.claude/skills/implement-plan/SKILL.md:133](.claude/skills/implement-plan/SKILL.md:133)).

But `cmd_batch_next()` computes all ready tasks across the schedule and greedily picks up to `--parallel`, independent of whether they belong to the selected batch ([scripts/plan_ops.py:301](scripts/plan_ops.py:301), [scripts/plan_ops.py:334](scripts/plan_ops.py:334)).

It reports a `batch_index`, but selection is not actually constrained to that batch. That weakens the schedule as the source of truth and can undermine interleaving assumptions.

### P1: `fail-task` cannot fully revert failed Claude tasks that created new files

The design assumes failed tasks can be reverted cleanly. But `cmd_fail_task()` only does `git restore` on listed files ([scripts/plan_ops.py:497](scripts/plan_ops.py:497)).

If a Claude task creates an untracked file and then fails, `fail-task` will leave that file behind. This is especially problematic because the plan schema explicitly allows `(create)` tasks.

### P1: Blocked dependents are logged, but not written back into the plan document

The design says failed tasks cascade-block dependents and status is tracked in the plan ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:815](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:815), [.claude/skills/implement-plan/SKILL.md:195](.claude/skills/implement-plan/SKILL.md:195)).

`cmd_block_dependents()` only appends `blocked` events; it does not mutate task statuses to `blocked` in the plan file ([scripts/plan_ops.py:533](scripts/plan_ops.py:533)).

So the plan document is not actually the fully updated source of truth after dependency failure.

### P1: The published `--task-ids` feature is still not backed by helper support

The design and skill both expose `--task-ids` ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:744](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:744), [.claude/skills/implement-plan/SKILL.md:64](.claude/skills/implement-plan/SKILL.md:64)).

But `plan_ops.py` has no schedule-filter subcommand or equivalent helper. The postmortem already caught this; code inspection confirms it.

### P1: Portability has been compromised in multiple “portable” artifacts

The design is explicit that the portable tier must not embed repo-specific environment details ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:915](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:915), [docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:924](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:924)).

But the skill, dispatch templates, and `plan_ops.py` usage header all hardcode `venv/bin/python` ([.claude/skills/implement-plan/SKILL.md:25](.claude/skills/implement-plan/SKILL.md:25), [.claude/skills/implement-plan/dispatch-templates.md:39](.claude/skills/implement-plan/dispatch-templates.md:39), [scripts/plan_ops.py:8](scripts/plan_ops.py:8)).

This matches the postmortem’s portability defect and should be treated as a genuine design violation, not just polish.

### P1: The sample fixture no longer tests capability-based routing

Because the sample plan hardcodes `Agent`, it is effectively a fixture for “agent selection already decided in the plan,” not for the design’s intended capability-based routing model. That means Phase 4 did not truly validate one of the system’s core claims.

### P2: Execution-log format drift exists between the design and implementation

The design’s execution-log table uses columns `Task | Agent | Outcome | Reviewer | Commit` ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:224](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:224)).

`finalize_execution_log()` writes `Task | Agent | Reviewer | Verdict | Commit | Notes` under a different section header ([scripts/plan_ops.py:601](scripts/plan_ops.py:601)).

This is not load-bearing by itself, but it is more evidence that the shipped helpers and the design document have diverged.

### P2: `parse-implementer-report` does not fully match the plan-implementer report shape

The plan-implementer emits `**Concerns for reviewer:**` ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:406](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:406), [.claude/agents/plan-implementer.md:126](.claude/agents/plan-implementer.md:126)).

`parse-implementer-report` looks for `**Concerns:**` instead ([scripts/plan_ops.py:391](scripts/plan_ops.py:391)). The parser will therefore drop reviewer concerns from well-formed implementer reports.

### P2: Preflight dirty-tree logic is not task-scope-aware

The design says dirty-tree handling should allow infrastructure churn but block source files in task scope ([docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:757](docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md:757)).

`cmd_preflight()` uses broad path-prefix heuristics: `.claude/`, `docs/`, and `tests/` are always infra-ignored ([scripts/plan_ops.py:182](scripts/plan_ops.py:182)).

That is not portable and can be wrong for plans whose actual task scope is inside `docs/`, `tests/`, or `.claude/`.

## Phase Assessment

### Phase 1: Wrapper Script

**Partially complete, not design-aligned.**

Good:
- Structured output envelope is present.
- Implement and review subcommands exist.
- Independent test rerun exists.

Not aligned:
- No safe baseline-aware scope check on implement.
- No safe baseline-aware scope check on review.
- Timeout recovery is repo-wide and destructive.
- Wrapper behavior is not safe under the exact parallel/interleaved execution model the design requires.

### Phase 2: Plan-Analyst Agent

**Conceptually complete, contract-misaligned with downstream consumer.**

Good:
- Strong schema specification.
- Explicit classification heuristic.
- Deterministic DAG/batch construction.

Not aligned:
- Emits JSON the downstream parser does not accept.
- Is stricter than the shipped fixture and therefore exposes that the fixture was never upgraded to the published schema.

### Phase 3: Plan-Implementer Agent

**Mostly aligned.**

This is the cleanest phase. The largest issue is downstream: `parse-implementer-report` does not quite match the report labels, and failure cleanup in `fail-task` is insufficient for created files.

### Phase 4: Orchestrator Skill + Helpers

**Broadly implemented, but materially incomplete.**

Good:
- The workflow reflects the intended architecture.
- Cross-review asymmetry, third-opinion escalation, and role-swap retry are specified in the skill/templates.

Not aligned:
- Several required helper semantics are missing (`--task-ids`, blocked-status mutation, true batch selection).
- The helper/test layer validates a different schedule schema than the analyst.
- The sample fixture is testing a different plan contract and partially bypasses the routing design.

## Additional Observations

- The dispatch templates intentionally diverge from the original design around the “parallel-tree caveat” for Claude reviewers ([.claude/skills/implement-plan/dispatch-templates.md:91](.claude/skills/implement-plan/dispatch-templates.md:91)). That may be acceptable, but it should be reflected back into the main design doc if retained.
- `tests/scripts/test_plan_ops.py` encodes the helper-side contract, not the published plan/analyst contract. That is why 42 tests can pass while the real analyst→parser handoff is broken.
- Verification note: `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (`42 passed in 5.10s`). `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -m 'not slow'` selects no tests because that file is entirely marked `slow`.

## Recommended Fix Order

1. Unify the plan/schema contract.
   Choose one canonical shape for:
   - task status vocabulary (`pending` vs `open`)
   - analyst JSON (`id/index` vs `task_id/batch_index`)
   - sample fixture plan schema
   - execution-log markdown shape

2. Fix wrapper state isolation before any further Phase 5 reruns.
   - Add baseline-aware scope validation to implement.
   - Add baseline-aware scope validation to review.
   - Replace repo-wide timeout cleanup with bounded cleanup against the task baseline.
   - Explicitly ignore orchestrator-owned state files.

3. Fix orchestrator helper gaps.
   - Implement schedule filtering for `--task-ids`.
   - Make `batch-next` honor the selected analyst batch exactly.
   - Make `block-dependents` mutate plan task statuses to `blocked`.
   - Make `fail-task` handle untracked created files.

4. Rebuild the sample fixture so it validates the real design.
   - Remove `Agent` from plan tasks.
   - Add `Priority`, `Description`, `Reversion guidance`, and `## Verification`.
   - Keep it capability-routed and schema-valid.

5. Add integration coverage at the actual contract boundaries.
   - Real analyst output piped into `parse-schedule`.
   - Parallel Codex implement dispatches with adjacent untracked outputs.
   - Review dispatch with sibling uncommitted task outputs present.
   - Failed Claude `(create)` task cleanup.

## Bottom Line

The system design has been implemented far enough to prove the architecture is viable, but not far enough to say “phases 1-4 are complete according to plan.” The repo currently contains:

- one set of docs describing the intended protocol,
- one set of helpers/tests enforcing a slightly different protocol,
- and a Phase 5 runtime failure that exposed the mismatch under load.

The correct next move is a **contract-alignment pass plus wrapper isolation fixes**, then a fresh Phase 5 rerun. Without that, further end-to-end attempts will keep failing for reasons that are upstream of Phase 5 itself.
