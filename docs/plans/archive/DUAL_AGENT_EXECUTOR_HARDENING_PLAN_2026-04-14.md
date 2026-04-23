# Plan: Dual Agent Executor Hardening and Contract-Governance

**Created:** 2026-04-14
**Status:** draft
**Base branch:** phase-7b5-bug-fixes

## Goal

Harden the Dual Agent Executor so it is resilient not only to implementation bugs, but also to contract drift, invalid fixtures, unsafe parallel state handling, weak phase gating, and protocol ambiguity during production use. The system should enforce its own operating contract, detect drift early, and refuse unsafe execution states before they can corrupt executor state or produce misleading run outcomes.

This plan is intentionally framed from the perspective of the executor's functionality. The aim is not merely to improve how we build the executor, but to make the executor itself a stronger, more self-policing system in production.

## Context

Recent review of `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`, the shipped Phase 1-4 artifacts, and `docs/analysis/DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md` surfaced a pattern:

- The high-level architecture is sound.
- The executor's concrete contracts drifted across documents, prompts, helpers, fixtures, and tests.
- Phase 5 failures are not purely "runtime bugs"; they also expose missing protocol enforcement and insufficient operational gating inside the executor itself.

The most important lesson is that the executor cannot rely on documents, prompts, or humans alone to keep the workflow coherent. It must actively validate:

- plan shape
- handoff artifact shape
- allowed vocabularies
- batch safety assumptions
- state-isolation guarantees
- promotion criteria between phases

The executor should behave less like a thin orchestrator and more like a protocol-governed workflow engine with explicit guardrails.

## Verification

The plan is complete when all of the following are true:

- A canonical machine-readable contract exists for the executor's key artifacts and is used by runtime helpers.
- The executor refuses to execute a plan, schedule, or report that violates the published contract.
- Parallel Codex execution and review no longer delete sibling outputs, schedule sidecars, lock files, or run-log state.
- The executor can enforce phase gates such that a plan/fixture/protocol mismatch halts before execution, not during or after it.
- End-to-end tests cover the actual runtime seams: analyst -> parser, implementer -> parser, batch scheduling, review isolation, failure cleanup, and run-log formation.
- The published design document and shipped artifacts are brought back into alignment, or explicit v2 deviations are documented and codified.

---

## Tasks

### TASK-001: Establish canonical executor protocol artifacts

- **Status:** pending
- **Priority:** critical
- **Files:**
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - docs/plans/DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14.md
  - scripts/plan_ops.py
  - scripts/plan_codex_dispatch.py
  - scripts/codex_implement_schema.json
  - scripts/codex_review_schema.json
  - .claude/agents/plan-analyst.md
  - .claude/agents/plan-implementer.md
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/run-log-schema.md
  - .claude/skills/implement-plan/dispatch-templates.md
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - One canonical contract is chosen for task status vocabulary, analyst JSON field names, batch field names, implementer report labels, and run-log event/vocabulary.
  - The chosen contract is documented in the design doc and reflected in every shipped agent/skill/helper/schema artifact.
  - Any intentional v2 divergence from the original design is explicitly documented as a deviation, not left implicit.
  - The executor no longer depends on duplicated free-text protocol definitions that can silently drift.

**Description:**
Unify the executor's protocol layer. Today, the design doc, prompts, helper code, tests, and sample fixtures do not all describe the same contract. This task chooses a single canonical contract and propagates it across the system.

This is the foundation for every other hardening change. Without a single authoritative contract, the executor cannot reliably validate or govern its own workflow.

**Implementation notes:**
Prefer a machine-readable contract artifact or a centralized constant/schema layer where practical. At minimum, normalize:

- `pending` vs `open`
- `id/index` vs `task_id/batch_index`
- implementer report section labels
- execution-log row schema
- review verdict vocabularies and event names

If backward compatibility is desired, it must be explicit and tested, not accidental.

**Reversion guidance:**
Restore the protocol-bearing files listed above to their previous state if the unified contract introduces incompatible breakage without migration handling.

---

### TASK-002: Add runtime contract validation as first-class executor behavior

- **Status:** pending
- **Priority:** critical
- **Files:**
  - scripts/plan_ops.py
  - scripts/plan_codex_dispatch.py
  - .claude/skills/implement-plan/SKILL.md
  - .claude/agents/plan-analyst.md
  - .claude/agents/plan-implementer.md
  - tests/scripts/test_plan_ops.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
- **Dependencies:** TASK-001
- **Test command:** none
- **Acceptance criteria:**
  - The executor validates plan documents, analyst schedules, implementer reports, and review outputs against the canonical contract before using them.
  - Invalid contract inputs halt execution with explicit reasons before code execution or commit steps begin.
  - Contract validation tests cover real producer -> consumer boundaries, not only helper-local synthetic payloads.
  - The executor can accept only documented backward-compatible variants, if such compatibility is intentionally supported.

**Description:**
Upgrade the executor from "trusting adjacent components" to "validating adjacent components." The executor should treat handoff artifacts as protocol messages and enforce them as such.

This task is about functional safety in production. If an agent emits an outdated schedule shape, or a fixture is no longer valid for the published plan schema, the executor should stop immediately and surface the mismatch.

**Implementation notes:**
Target the actual seams:

- plan document -> analyst
- analyst JSON -> `parse-schedule`
- implementer report -> `parse-implementer-report`
- reviewer output -> verdict routing
- final execution-log rows -> appended markdown/log output

Use the real emitted shapes wherever possible, not only hand-authored JSON in tests.

**Reversion guidance:**
Revert validation additions if they create false positives that block all execution, but keep any new tests for diagnosis if possible.

---

### TASK-003: Make working-tree isolation safe for parallel implement and serial review

- **Status:** pending
- **Priority:** critical
- **Files:**
  - scripts/plan_codex_dispatch.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/dispatch-templates.md
- **Dependencies:** TASK-001
- **Test command:** none
- **Acceptance criteria:**
  - Codex implement-path scope validation uses pre-run baselines and only classifies files newly created or modified by that dispatch.
  - Codex review-path cleanup also uses pre-review baselines and does not remove legitimate sibling state or orchestrator control files.
  - Timeout cleanup is bounded to the task delta and no longer performs repo-wide destructive cleanup.
  - Regression tests prove that adjacent parallel task outputs, schedule sidecars, run logs, and lock files survive sibling dispatches.

**Description:**
The executor's production safety depends on correct state isolation. Current scope cleanup can delete sibling outputs and orchestrator files, which makes parallel execution fundamentally unsafe and also risks corrupting operator-visible run history.

This task turns isolation into an enforced runtime guarantee.

**Implementation notes:**
The executor should maintain explicit notions of:

- baseline tracked state
- baseline untracked state
- always-ignored orchestrator artifacts
- task-owned file deltas

Review dispatches need the same rigor as implement dispatches. Serial review is not enough if cleanup logic still reasons from the full working tree rather than per-dispatch deltas.

**Reversion guidance:**
If the new isolation logic causes regressions, restore the prior wrapper behavior only behind a disabled-by-default compatibility flag; do not silently return to destructive cleanup.

---

### TASK-004: Strengthen scheduler and dependency enforcement

- **Status:** pending
- **Priority:** high
- **Files:**
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
  - .claude/skills/implement-plan/SKILL.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
- **Dependencies:** TASK-001
- **Test command:** none
- **Acceptance criteria:**
  - `batch-next` selects from the authoritative scheduled batch rather than greedily from all ready tasks.
  - Dependency cycles and orphaned filtered schedules are detected defensively downstream, not trusted solely to the analyst.
  - `--task-ids`, `--codex-only`, and `--claude-only` are fully backed by helper logic, including orphan-dependency detection.
  - Blocked dependents are represented in both run-log output and plan document status, keeping the plan as a true source of truth.

**Description:**
The executor's scheduling model is one of its defining features. It should not merely "usually work"; it should enforce the exact schedule chosen by the analyst and the exact dependency rules documented in the design.

This task closes the gap between declarative schedule intent and actual runtime selection.

**Implementation notes:**
Do not treat helper convenience as a substitute for schedule fidelity. If the analyst emits batches, batch semantics should matter operationally. Also ensure that downstream validators do not trust upstream success blindly when basic DAG verification is cheap to recheck.

**Reversion guidance:**
Restore previous selection behavior only if a new scheduler bug is introduced and execution is blocked, but keep defensive DAG validation if possible.

---

### TASK-005: Add executor phase gates and promotion criteria

- **Status:** pending
- **Priority:** high
- **Files:**
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - .claude/skills/implement-plan/SKILL.md
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
  - docs/plans/sample_phase4.md
- **Dependencies:** TASK-001, TASK-002, TASK-004
- **Test command:** none
- **Acceptance criteria:**
  - The executor has explicit phase-gate checks for schema validity, fixture validity, schedule validity, and operational safety before execute mode proceeds.
  - Dry-run and execute mode have distinct required pass conditions, documented and enforced.
  - Sample or verification plans are validated against the same schema and gates as production plans.
  - Phase completion criteria are defined so future work cannot mark a phase complete while contract-boundary checks are still missing.

**Description:**
The system needs promotion gates inside the executor lifecycle. Right now, the process allowed phases to be considered complete while critical runtime seams were still unverified or inconsistent.

This task formalizes the gates the executor itself should enforce before it accepts a plan as executable, before it allows a phase to be treated as validated, and before it reports a workflow as production-ready.

**Implementation notes:**
Think in terms of runtime states:

- schema-valid
- schedule-valid
- fixture-valid
- execution-safe
- review-safe
- commit-safe

The executor should halt on failed gates with clear reasons and remediation hints.

**Reversion guidance:**
If gating is too strict and blocks all usage, temporarily downgrade non-critical gates to warnings behind an explicit flag, but do not remove them without documenting the risk.

---

### TASK-006: Rebuild the sample fixture as a true production-like conformance plan

- **Status:** pending
- **Priority:** high
- **Files:**
  - docs/plans/sample_phase4.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - tests/scripts/test_plan_ops.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
- **Dependencies:** TASK-001, TASK-005
- **Test command:** none
- **Acceptance criteria:**
  - `sample_phase4.md` conforms to the canonical plan schema.
  - The sample fixture no longer hardcodes routing decisions that belong to the analyst/classifier unless the design is explicitly changed to allow that.
  - The fixture still exercises parallel execution, dependency handling, review cleanliness, and failure propagation.
  - The sample fixture is validated automatically as part of executor verification.

**Description:**
The executor's reference fixture should test the real system contract, not a nearby approximation. If the fixture drifts, it can create a false sense of phase completion while no longer verifying the published protocol.

This task makes the sample plan a first-class conformance artifact.

**Implementation notes:**
Keep the useful scenario structure:

- mixed Codex/Claude routing
- dependency chain
- seeded failure
- review-diff cleanliness

But express it using the real plan contract. If special test-only annotations are needed, add them deliberately to the contract rather than smuggling behavior through undocumented fields.

**Reversion guidance:**
If fixture refactoring makes tests unusable, preserve the old file as a legacy adversarial input under a clearly different name and purpose.

---

### TASK-007: Improve failure recovery semantics for created files, blocked tasks, and bookkeeping state

- **Status:** pending
- **Priority:** medium
- **Files:**
  - scripts/plan_ops.py
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/run-log-schema.md
  - tests/scripts/test_plan_ops.py
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
- **Dependencies:** TASK-001, TASK-003, TASK-004
- **Test command:** none
- **Acceptance criteria:**
  - Failed tasks that create untracked files are cleaned up safely and predictably.
  - Blocked tasks are represented consistently across in-memory state, plan-document status, and run-log events.
  - Run-lock, run-log, and schedule-sidecar lifecycle is documented and enforced as protected executor infrastructure.
  - Failure and recovery semantics are clear enough that operators can trust the plan document and run log after interrupted or partial runs.

**Description:**
The executor's recovery model should be explicit and reliable. A failed run should leave behind a coherent state story, not just "some files were restored and some were not."

This task closes recovery gaps that undermine resumability and auditability.

**Implementation notes:**
Treat executor infrastructure files as part of the runtime contract. They should have protected semantics and explicit handling rules, not merely be "some docs files that happen to exist."

**Reversion guidance:**
Restore previous failure-handling code only if new recovery logic causes worse data loss or leaves the repo in a less coherent state.

---

### TASK-008: Add production-oriented self-audit and protocol-drift detection

- **Status:** pending
- **Priority:** medium
- **Files:**
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - .claude/skills/implement-plan/SKILL.md
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
  - docs/analysis/DUAL_AGENT_EXECUTOR_DESIGN_AUDIT_2026-04-14.md
- **Dependencies:** TASK-001, TASK-002, TASK-005
- **Test command:** none
- **Acceptance criteria:**
  - The executor has a documented self-audit mode or equivalent verification path that checks contract alignment before production execution.
  - Drift between design-time protocol and runtime protocol can be surfaced proactively rather than only discovered during Phase 5-style incidents.
  - The system's verification plan includes explicit contract-drift checks, not only success-path execution checks.
  - Operators have a clear mechanism to validate executor readiness after substantive protocol changes.

**Description:**
The executor should be able to inspect its own protocol health. This is the functional answer to the root problem exposed here: drift accumulated until an end-to-end production-style run revealed it.

This task adds a capability for proactive protocol health checks, reducing dependence on manual forensic review.

**Implementation notes:**
Possible forms include:

- a `plan_ops.py` validation subcommand set
- a contract-conformance report mode
- a documented pre-production certification workflow

The important point is not the exact interface. The important point is that the executor gains a built-in way to detect protocol drift before use.

**Reversion guidance:**
If self-audit tooling proves too noisy, reduce scope or improve reporting, but keep at least the critical drift checks available.

---

## Cross-Cutting Design Revisions To Consider

These are not separate tasks yet, but they should be considered during implementation:

1. **Make the contract executable.**
   The executor should derive validation behavior from a canonical schema/contract source rather than duplicated prose.

2. **Differentiate "design contract" from "test fixture convenience".**
   If a fixture uses special affordances, those affordances should either be part of the formal contract or clearly marked as test-only extensions.

3. **Promote "production safety" to a top-level design principle.**
   The original design emphasizes routing, cross-review, and portability. It should also explicitly emphasize runtime state safety, protocol integrity, and phase-gate enforcement.

4. **Treat review isolation as a first-class state-management problem.**
   The system currently reasons clearly about implement parallelism but less rigorously about what the review wrapper can observe or clean up.

5. **Add a "contract drift" failure category.**
   Today many failures collapse into `invalid`, `failure`, or `parse_error`. It may be worth explicitly surfacing protocol-drift failures so operators understand they are dealing with a system-contract mismatch rather than a normal task failure.

6. **Document what the executor is allowed to trust.**
   For each phase, define whether the executor trusts:
   - plan text
   - analyst output
   - implementer report
   - review report
   - working tree state

   Wherever trust is not absolute, define the mandatory verification step.

---

## Risks

- If protocol alignment is attempted piecemeal, the repo may spend time in a worse mixed-contract state than it is now.
- If backward compatibility is added without discipline, the executor may become too permissive and fail to surface real drift.
- If gating is too strict without operator overrides, the executor may become impractical to use during migration.
- If the sample fixture is updated before the underlying contract is settled, it may need to be rewritten again.

## Execution Strategy

Recommended order:

1. TASK-001
2. TASK-002
3. TASK-003
4. TASK-004
5. TASK-005
6. TASK-006
7. TASK-007
8. TASK-008

Rationale:

- First settle the contract.
- Then enforce it.
- Then make runtime state handling safe.
- Then tighten scheduling and phase gating.
- Then rebuild the fixture and recovery story on top of the stabilized protocol.

## Expected Outcome

After this plan, the Dual Agent Executor should be meaningfully stronger in production:

- It should reject drift instead of absorbing it.
- It should preserve its own state correctly under parallel and partial execution.
- It should validate its own inputs and handoffs rather than trusting adjacent artifacts.
- It should make phase completion harder to claim prematurely.
- It should provide operators with a clearer, more auditable account of what happened during execution and why.
