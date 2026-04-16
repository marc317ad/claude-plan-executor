# Plan: Dual Agent Executor Hardening and Contract-Governance v2

**Created:** 2026-04-14
**Status:** draft
**Base branch:** phase-7b5-bug-fixes

## Goal

Harden the Dual Agent Executor so it can recover from current implementation defects and also prevent the same class of failures in production. This means fixing the concrete issue set already identified in the consolidated remediation report while also upgrading the executor into a protocol-governed system that validates its own contracts, enforces safer runtime states, and gates unsafe execution paths before they mutate the repo.

This v2 plan consolidates:

- the executor-level hardening perspective from `docs/plans/DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14.md`
- the concrete defect inventory and sequencing from `docs/analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`

## Context

The consolidated remediation report is strong on issue normalization, fix bundling, and immediate unblock sequencing. Its weakness is that it still frames several system-level controls as follow-up cleanup rather than core executor functionality.

The hardening work should therefore be split into two synchronized tracks:

1. **Remediation track:** fix the specific known defects and rerun Phase 5 successfully.
2. **Governance track:** make the executor itself enforce protocol integrity, state isolation, conformance, and production gates so future drift is detected before end-to-end failure.

The governing design principle for this v2 is:

> The Dual Agent Executor must not merely orchestrate agents. It must validate the protocol it runs, preserve the integrity of its own state, and refuse execution when required guarantees are missing.

## Verification

This plan is complete when all of the following are true:

- All P0 issues from the consolidated remediation report are fixed and verified.
- The minimum Phase 5 rerun path passes.
- The executor validates canonical contract artifacts before consuming plan, schedule, implementer, and reviewer outputs.
- The executor enforces safe batch, review, timeout, and recovery behavior under parallel or partial execution.
- Sample and verification plans are treated as conformance artifacts and validated automatically.
- The executor has explicit phase gates and a self-audit/drift-detection path for production readiness.

---

## Workstreams

### Workstream A: Immediate Runtime Remediation

Derived primarily from the consolidated issue report. These tasks restore end-to-end operability.

### Workstream B: Executor Governance and Production Hardening

Derived from the original hardening plan. These tasks make the executor self-policing and reduce the chance of similar failures recurring.

The intent is not to run these sequentially as “fixes first, hardening later.” Some governance items are prerequisites for a trustworthy rerun.

---

## Tasks

### TASK-001: Canonicalize executor protocol contracts

- **Status:** pending
- **Priority:** critical
- **Files:**
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - docs/analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md
  - docs/plans/DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v2.md
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
  - One canonical contract is chosen for:
    - task status vocabulary
    - analyst JSON field names
    - batch field names
    - implementer report labels
    - execution-log schema
    - schedule persistence ownership
  - Consolidated issues ISSUE-001, ISSUE-009, ISSUE-013, ISSUE-014, ISSUE-016, ISSUE-022, ISSUE-024, ISSUE-025, and ISSUE-026 are either directly resolved or explicitly absorbed into the chosen canonical contract.
  - Any supported backward-compatibility aliases are documented and tested.
  - The design doc and shipped artifacts no longer describe materially different protocol shapes.

**Description:**
The executor currently has multiple overlapping protocol definitions. This task establishes a single canonical contract and propagates it through docs, prompts, helper code, and schemas.

This task absorbs the contract-alignment core of Claude’s consolidation and makes it the formal basis for subsequent runtime validation and gating.

**Implementation notes:**
Use Claude’s issue IDs as the traceability map, but do not stop at doc edits. The executor’s live consumers and producers must speak the same protocol after this task.

**Reversion guidance:**
Restore prior protocol-bearing files only if the unified contract blocks all execution and no migration path is available.

---

### TASK-002: Add runtime contract validation at every executor seam

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
  - The executor validates:
    - plan documents before analysis/execution
    - analyst schedules before scheduling
    - implementer reports before commit routing
    - reviewer outputs before verdict routing
    - execution-log rows before append/finalization
  - Consolidated issues ISSUE-001, ISSUE-019, ISSUE-020, ISSUE-014, and ISSUE-016 are covered by runtime validation behavior rather than only by convention.
  - Real producer -> consumer integration tests exist for analyst→parse-schedule and implementer→parse-implementer-report.
  - Invalid protocol inputs halt before implementation or commit stages.

**Description:**
The executor should treat plan/schedule/report/review artifacts as protocol messages, not trusted prose. This task makes protocol enforcement part of runtime behavior.

**Implementation notes:**
Claude’s acceptance-criteria style is useful here; keep that concreteness, but widen the scope from helper-local validation to executor seam validation.

**Reversion guidance:**
If strict validation causes widespread false positives, temporarily permit only documented compatibility variants. Do not remove seam validation entirely.

---

### TASK-003: Make implement, review, and timeout state isolation safe

- **Status:** pending
- **Priority:** critical
- **Files:**
  - scripts/plan_codex_dispatch.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR_HARDENING_PLAN_2026-04-14_v2.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/dispatch-templates.md
- **Dependencies:** TASK-001
- **Test command:** none
- **Acceptance criteria:**
  - Consolidated issues ISSUE-003, ISSUE-004, ISSUE-005, ISSUE-015, ISSUE-017, and ISSUE-021 are resolved.
  - Implement-path scope validation uses pre-dispatch tracked and untracked baselines.
  - Review-path isolation also uses pre-dispatch baselines and does not delete sibling task state or executor infrastructure.
  - Timeout cleanup is bounded to task-owned deltas and never performs repo-wide destructive cleanup.
  - Orchestrator-owned artifacts (`_run_log.jsonl`, `_run_lock.json`, `*.schedule.json`, and any equivalent canonical infra files) are protected explicitly.
  - Regression coverage exists for parallel sibling dispatches and dishonest `files_changed` reports.

**Description:**
This is the executor’s state-safety task. It merges Claude’s wrapper isolation cluster with the broader hardening requirement that the executor preserve its own operational state under concurrency, review, timeout, and partial failure.

**Implementation notes:**
Serial review is not sufficient protection. The wrapper must reason from per-dispatch deltas, not from the full working tree.

**Reversion guidance:**
If new cleanup logic regresses, fall back only behind an explicit disabled-by-default compatibility flag. Do not restore repo-wide cleanup behavior.

---

### TASK-004: Enforce scheduler, dependency, and blocked-state semantics

- **Status:** pending
- **Priority:** high
- **Files:**
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/run-log-schema.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
- **Dependencies:** TASK-001, TASK-002
- **Test command:** none
- **Acceptance criteria:**
  - Consolidated issues ISSUE-006, ISSUE-010, ISSUE-011, ISSUE-012, ISSUE-018, ISSUE-019, and ISSUE-020 are resolved.
  - `batch-next` honors the authoritative batch rather than a global-ready shortcut.
  - Schedule filtering and persistence are handled by defined helper commands, not brittle orchestrator-side ad hoc writes.
  - Failed `(create)` tasks clean up predictably.
  - Blocked tasks are reflected consistently across in-memory state, plan-document status, and run-log events.
  - Lock-file format is validated strictly enough to reject malformed stale state.

**Description:**
This task turns scheduling and failure propagation into stronger executor invariants. It absorbs the orchestrator-semantics cluster from the consolidated plan and aligns it with the “plan as source of truth” requirement from the hardening plan.

**Implementation notes:**
Downstream verification should not trust upstream analyst success blindly when cheap defensive checks are available.

**Reversion guidance:**
If scheduler enforcement introduces a blocking bug, temporarily preserve defensive validation while downgrading only the new selection behavior behind a compatibility flag.

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
  - docs/analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md
- **Dependencies:** TASK-001, TASK-002, TASK-004
- **Test command:** none
- **Acceptance criteria:**
  - The executor has explicit gates for:
    - schema-valid
    - schedule-valid
    - fixture-valid
    - execution-safe
    - review-safe
    - commit-safe
  - Dry-run and execute mode have different pass conditions and are documented.
  - Phase 5 rerun is treated as a gateable certification event, not just “the next thing to try.”
  - The minimum viable rerun set from the consolidated plan is preserved, but phase-gate requirements are promoted from follow-up cleanup into required executor behavior.

**Description:**
This is the main place where the earlier hardening plan extends Claude’s consolidation. The executor should not allow “phase complete” or “safe to rerun” status unless the relevant gates pass.

**Implementation notes:**
Do not dilute this into a process memo. The executor should expose or enforce these states directly in its operational workflow.

**Reversion guidance:**
If gates are initially too strict, temporarily downgrade non-critical gates to warnings behind an explicit override. Keep the gate model intact.

---

### TASK-006: Rebuild sample and verification plans as conformance artifacts

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
  - Consolidated issue ISSUE-002 is fully resolved.
  - `sample_phase4.md` conforms to the canonical plan schema and no longer hardcodes routing unless that is explicitly adopted into the contract.
  - The sample fixture still exercises mixed routing, dependency order, seeded failure, and review cleanliness.
  - Verification/sample plans are validated automatically as conformance artifacts before they are relied upon for phase certification.

**Description:**
The sample plan must test the real system contract. If it drifts, it undermines phase validation and creates false confidence.

**Implementation notes:**
Keep the scenario value of the current fixture, but remove undocumented shortcuts like `**Agent:**`.

**Reversion guidance:**
If fixture migration is disruptive, preserve the old fixture as a legacy/adversarial case under a different name and purpose.

---

### TASK-007: Add production-oriented self-audit and protocol-drift detection

- **Status:** pending
- **Priority:** medium
- **Files:**
  - scripts/plan_ops.py
  - .claude/skills/implement-plan/SKILL.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - tests/scripts/test_plan_ops.py
  - docs/analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md
  - docs/analysis/DUAL_AGENT_EXECUTOR_DESIGN_AUDIT_2026-04-14.md
- **Dependencies:** TASK-001, TASK-002, TASK-005
- **Test command:** none
- **Acceptance criteria:**
  - The executor has a self-audit mode or equivalent verification path that checks protocol alignment before production execution.
  - Contract drift is surfaced proactively rather than only discovered during end-to-end failures.
  - The verification plan includes drift checks, not only happy-path runs.
  - Operators have a documented readiness check after substantive protocol changes.

**Description:**
This task fills the main gap in Claude’s consolidation. The issue matrix is good at enumerating current defects, but the executor also needs a standing capability to detect similar drift in the future.

**Implementation notes:**
Possible implementations include dedicated `plan_ops.py` validation subcommands, a conformance report mode, or a documented certification workflow. The key requirement is executor-visible drift detection.

**Reversion guidance:**
If self-audit is too noisy, reduce scope or improve reporting. Keep at least the critical protocol drift checks.

---

### TASK-008: Parameterize portability and scope-aware preflight behavior

- **Status:** pending
- **Priority:** medium
- **Files:**
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/dispatch-templates.md
  - scripts/plan_ops.py
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001, TASK-005
- **Test command:** none
- **Acceptance criteria:**
  - Consolidated issues ISSUE-007 and ISSUE-008 are resolved.
  - Portable-tier artifacts no longer hardcode repo-specific Python paths.
  - Preflight classifies dirty files using actual plan/task scope rather than broad directory heuristics alone.
  - Portability and preflight semantics are documented consistently with the canonical contract.

**Description:**
These were lower in Claude’s ordering, but they still affect executor correctness. Scope-aware preflight is part of safe execution; portability belongs in the executor’s declared operating model.

**Implementation notes:**
Treat scope-aware preflight as an execution-safety behavior, not mere cleanup.

**Reversion guidance:**
If parameterization causes temporary friction, preserve a project-default path variable rather than hardcoding paths back into portable files.

---

## Consolidated Rerun Gate

The executor is ready for the next full Phase 5 rerun only when:

1. TASK-001, TASK-002, TASK-003, TASK-004, TASK-005, and TASK-006 are complete.
2. The consolidated report’s minimum unblock path is satisfied:
   - wrapper state isolation
   - contract alignment
   - fixture rewrite
   - parallel sibling regression coverage
3. The executor can positively assert:
   - schedule contract valid
   - fixture valid
   - timeout cleanup bounded
   - review cleanup bounded
   - batch selection faithful
   - blocked/dependency state coherent

This intentionally tightens the gate relative to the consolidated plan. The goal is not only to get a rerun to start, but to make the rerun worth trusting.

## Merge / Implementation Strategy

Use Claude’s issue clusters as the implementation map, but organize work under the hardening workstreams:

1. Contract foundation
   - maps mainly to ISSUE-001, 009, 013, 014, 016, 020, 022, 024, 025, 026

2. Runtime validation
   - maps mainly to ISSUE-001, 014, 016, 019, 020

3. State isolation
   - maps mainly to ISSUE-003, 004, 005, 015, 017, 021

4. Scheduler / status / recovery semantics
   - maps mainly to ISSUE-006, 010, 011, 012, 018, 019, 020

5. Phase gates and conformance fixtures
   - maps mainly to ISSUE-002, 017 and the minimum rerun set

6. Self-audit and portability polish
   - maps mainly to ISSUE-007, 008 and future drift prevention

This allows the team to preserve Claude’s actionable issue structure without losing the executor-level functional hardening perspective.

## Expected Outcome

After this v2 plan:

- the currently known defects should be fixed in a reviewable, traceable way
- the executor should be able to rerun Phase 5 with a materially safer runtime model
- the executor should validate its own protocol boundaries rather than trusting drift-prone text
- future planning and production use should be less vulnerable to silent contractual drift
- “phase complete” should mean the relevant gates actually passed, not just that the components exist
