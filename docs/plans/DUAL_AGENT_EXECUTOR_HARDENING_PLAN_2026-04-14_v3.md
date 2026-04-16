# Plan: Dual Agent Executor Hardening and Contract-Governance v3

**Created:** 2026-04-14
**Status:** draft
**Base branch:** phase-7b5-bug-fixes

## Goal

Harden the Dual Agent Executor so it can:

- fix the currently known implementation defects,
- enforce its own runtime protocol boundaries,
- preserve repo and executor state safely under parallel and partial execution,
- scale more safely on large-file tasks,
- treat dependency/environment mutation as globally serialized work,
- and handle test/log output as untrusted passive data rather than implicit agent guidance.

This v3 plan extends v2 by incorporating valid operational concerns raised during external review, specifically around context scaling, dependency collisions, and log/test-output handling.

## Context

The current issue set shows two intertwined weaknesses:

1. **Concrete defects** in wrapper isolation, schedule contracts, fixture validity, and helper behavior.
2. **Missing runtime governance** in the executor itself, allowing contract drift, unsafe cleanup, weak phase-gating, and ambiguous state ownership to accumulate until exposed by end-to-end runs.

Additional review surfaced three production-facing risks that the earlier plans did not fully elevate:

- reading all scope files in full does not scale well for large-file tasks
- dependency-manifest and environment-mutating work should not be parallelized like ordinary file edits
- test output should be treated as passive, bounded, untrusted log data

The design principle for v3 is:

> The Dual Agent Executor should be a contract-enforcing workflow engine with explicit runtime safety rules, not merely a dispatcher for agents.

## Verification

This plan is complete when all of the following are true:

- All P0 issues from the consolidated remediation report are fixed and verified.
- The executor can pass a trustworthy Phase 5 rerun under the new gates.
- The executor validates canonical protocol artifacts before consuming them.
- The executor bounds cleanup to task-owned deltas and protects executor infrastructure.
- The executor enforces global locks for dependency/environment mutation paths.
- The executor uses scale-aware reading guidance for large-file tasks.
- The executor treats test/log output as bounded passive data in both prompts and parsing paths.
- Sample and verification plans act as conformance artifacts and are validated automatically.
- The executor has a self-audit/drift-detection path for production readiness.

---

## Workstreams

### Workstream A: Immediate Runtime Remediation

Fix the concrete issue inventory from the consolidated remediation report.

### Workstream B: Executor Governance and Production Hardening

Make protocol validation, phase-gating, recovery, and conformance first-class runtime behavior.

### Workstream C: Operational Safety Extensions

Add runtime rules for large-scope task handling, dependency/global-lock behavior, and untrusted log/test-output treatment.

---

## Tasks

### TASK-001: Canonicalize executor protocol contracts

- **Status:** pending
- **Priority:** critical
- **Files:**
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - docs/analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md
  - docs/plans/DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md
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
  - One canonical contract is chosen for task status vocabulary, analyst JSON, batch fields, implementer report labels, execution-log rows, and schedule persistence ownership.
  - Consolidated issues ISSUE-001, ISSUE-009, ISSUE-013, ISSUE-014, ISSUE-016, ISSUE-022, ISSUE-024, ISSUE-025, and ISSUE-026 are either resolved or explicitly absorbed into the canonical contract.
  - Backward-compatibility aliases, if any, are explicit and tested.
  - Design docs, prompts, helpers, and schemas no longer describe materially different protocol shapes.

**Description:**
Establish one authoritative executor protocol. All later validation, gating, and operational safety behavior depends on this contract being singular and enforceable.

**Implementation notes:**
This is the contract foundation. Do not allow “close enough” variants to survive undocumented.

**Reversion guidance:**
Restore prior protocol-bearing files only if the unified contract blocks all execution and no compatibility or migration path exists.

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
  - The executor validates plan documents, analyst schedules, implementer reports, reviewer outputs, and execution-log rows before using them.
  - Consolidated issues ISSUE-001, ISSUE-014, ISSUE-016, ISSUE-019, and ISSUE-020 are covered by runtime validation behavior.
  - Real producer -> consumer integration tests exist for analyst→parse-schedule and implementer→parse-implementer-report.
  - Invalid contract inputs halt before implementation or commit stages.

**Description:**
Upgrade the executor from trusting neighboring components to validating them as protocol participants.

**Implementation notes:**
Validation should happen at real handoff points, not only inside helper-local synthetic tests.

**Reversion guidance:**
If strict validation creates false positives, temporarily allow only documented compatibility variants. Do not remove seam validation.

---

### TASK-003: Make implement, review, and timeout state isolation safe

- **Status:** done
- **Priority:** critical
- **Files:**
  - scripts/plan_codex_dispatch.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - .claude/skills/implement-plan/SKILL.md
  - .claude/skills/implement-plan/dispatch-templates.md
  - docs/plans/DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md
- **Dependencies:** TASK-001
- **Test command:** none
- **Acceptance criteria:**
  - Consolidated issues ISSUE-003, ISSUE-004, ISSUE-005, ISSUE-015, ISSUE-017, and ISSUE-021 are resolved.
  - Implement-path scope validation uses pre-dispatch tracked and untracked baselines.
  - Review-path cleanup also uses pre-dispatch baselines and does not delete sibling task state or executor infrastructure.
  - Timeout cleanup is bounded to task-owned deltas and never performs repo-wide destructive cleanup.
  - Executor infrastructure files are explicitly protected.
  - Regression coverage exists for parallel sibling dispatches and dishonest `files_changed` behavior.

**Description:**
This is the state-safety cluster. It fixes the known wrapper isolation defects while establishing the executor’s rule that cleanup must always be delta-scoped, never repo-scoped.

**Implementation notes:**
Reject the tempting fallback of repo-wide `git clean -fd`. It solves the wrong problem and breaks executor safety.

**Reversion guidance:**
If the new cleanup logic regresses, fall back only behind an explicit compatibility flag. Never restore global destructive cleanup as the default.

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
  - `batch-next` honors authoritative batch semantics.
  - Schedule filtering and schedule persistence are handled by defined helper commands.
  - Failed `(create)` tasks clean up predictably.
  - Blocked tasks are coherent across plan state, in-memory state, and run-log events.
  - Lock-file format is validated strictly enough to reject malformed stale state.

**Description:**
Make scheduling and failure propagation actual executor invariants rather than best-effort helper behavior.

**Implementation notes:**
Defensive DAG validation should exist downstream even if the analyst already does it upstream.

**Reversion guidance:**
If new scheduler behavior causes blocking regressions, preserve defensive validation and downgrade only the new enforcement path behind a compatibility switch.

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
  - The executor has explicit gates for `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe`, and `commit-safe`.
  - Dry-run and execute mode have distinct pass conditions and are documented.
  - Phase 5 rerun is treated as a gateable certification event, not merely a retry attempt.
  - Minimum unblock work from the consolidated report is preserved, but phase gates are promoted into required executor behavior.

**Description:**
This task ensures “ready to rerun” and “phase complete” are explicit runtime states, not informal judgments.

**Implementation notes:**
These gates should be enforced or surfaced by the executor itself, not left as documentation-only process advice.

**Reversion guidance:**
If gates are too strict initially, temporarily downgrade non-critical gates to warnings behind explicit override flags. Keep the gate model itself.

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
  - `sample_phase4.md` conforms to the canonical plan schema and does not hardcode routing unless explicitly adopted into the contract.
  - The sample fixture still exercises mixed routing, dependency order, seeded failure, and review cleanliness.
  - Sample and verification plans are validated automatically as conformance artifacts before they are used to certify executor behavior.

**Description:**
Reference fixtures must test the real contract, not a nearby approximation.

**Implementation notes:**
Keep the scenario richness; remove undocumented shortcuts.

**Reversion guidance:**
If fixture migration causes disruption, preserve the old artifact as a legacy/adversarial case under a different name and purpose.

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
  - Contract drift is surfaced proactively rather than only discovered during end-to-end failure.
  - The verification plan includes drift checks, not just happy-path runs.
  - Operators have a documented readiness check after substantive protocol changes.

**Description:**
The executor needs a standing capability to detect protocol drift before it becomes another Phase 5-style incident.

**Implementation notes:**
Possible interfaces include dedicated validation subcommands, conformance reports, or a certification workflow. The important part is the capability, not the exact command name.

**Reversion guidance:**
If self-audit is noisy, narrow its scope or improve reporting. Keep the critical drift checks.

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
Scope-aware preflight is part of execution safety, and portability belongs in the executor’s functional model rather than as afterthought cleanup.

**Implementation notes:**
Preserve project-default invocation via variables or project instructions, not by hardcoding paths back into portable-tier files.

**Reversion guidance:**
If parameterization introduces temporary friction, keep a project-default variable rather than restoring hardcoded paths.

---

### TASK-009: Add scale-aware large-file and large-scope task handling

- **Status:** pending
- **Priority:** medium
- **Files:**
  - .claude/agents/plan-implementer.md
  - .claude/agents/plan-analyst.md
  - .claude/skills/implement-plan/SKILL.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001, TASK-002
- **Test command:** none
- **Acceptance criteria:**
  - The executor and/or plan-implementer contract distinguishes between small bounded tasks and large-scope tasks where full-file reads become unsafe or wasteful.
  - For large files or large-file-count tasks, the executor supports scale-aware reading guidance such as symbol-targeted reads, line-range targeting, or structured chunking.
  - The plan schema and/or task-authoring guidance encourages symbols/line ranges for large-file work.
  - The default remains conservative correctness for small bounded tasks; scaling behavior is an explicit extension rather than an undocumented shortcut.

**Description:**
The original plan-implementer instruction to read every scoped file in full is reasonable for small tasks but scales poorly on very large files or poorly scoped tasks. The executor should gain a formal notion of when targeted reading is allowed or required.

**Implementation notes:**
Do not overcorrect immediately into mandatory AST tooling. Start with explicit thresholds and controlled chunking/symbol targeting. Tooling can follow if needed.

**Reversion guidance:**
If scale-aware reading weakens correctness, tighten thresholds and fall back to full-file reads for borderline tasks.

---

### TASK-010: Treat dependency and environment mutation as globally locked work

- **Status:** pending
- **Priority:** medium
- **Files:**
  - .claude/skills/implement-plan/SKILL.md
  - .claude/agents/plan-analyst.md
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** TASK-001, TASK-004, TASK-005
- **Test command:** none
- **Acceptance criteria:**
  - The executor recognizes dependency and environment manifests as globally locked paths.
  - Tasks touching files such as `requirements.txt`, `pyproject.toml`, `poetry.lock`, `package.json`, `package-lock.json`, or equivalent env/build manifests cannot be parallelized with ordinary tasks unless explicitly allowed by policy.
  - Plans or tasks that imply package installation or environment mutation are either isolated into sequential execution or rejected from normal executor flow unless elevated handling is enabled.
  - Batch scheduling logic and/or analyst risk classification reflects these global-lock constraints.

**Description:**
File-disjoint parallelism is not sufficient when two tasks collide through dependency manifests or environment mutation. The executor should treat these as globally serialized resources.

**Implementation notes:**
This is an executor-level locking rule, not just a planning recommendation. It belongs in classification, batching, and policy enforcement.

**Reversion guidance:**
If the global-lock set is too broad, narrow the manifest list carefully. Do not revert to unconstrained parallel dependency mutation.

---

### TASK-011: Treat test and log output as bounded untrusted passive data

- **Status:** pending
- **Priority:** medium
- **Files:**
  - .claude/agents/plan-implementer.md
  - .claude/skills/implement-plan/dispatch-templates.md
  - scripts/plan_codex_dispatch.py
  - scripts/plan_ops.py
  - docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
  - tests/scripts/test_plan_ops.py
  - tests/scripts/test_plan_codex_dispatch_integration.py
- **Dependencies:** TASK-001, TASK-002
- **Test command:** none
- **Acceptance criteria:**
  - Test output passed through the executor is truncated to a bounded size or line count.
  - Prompts and reports that include test/log output wrap it in rigid delimiters and clearly label it as passive log data.
  - The executor prefers structured test-result summaries where possible:
    - command
    - exit code/result
    - failing test identifier if known
    - bounded tail
  - No executor behavior treats test output as instruction-bearing text.

**Description:**
Test output should be treated as untrusted operational evidence, not as freeform guidance. This reduces both context pollution and the risk of log text steering agent behavior.

**Implementation notes:**
This is less about theoretical prompt injection and more about good executor hygiene around untrusted log data. Keep it bounded, labeled, and structured.

**Reversion guidance:**
If truncation hides needed debugging detail, increase the bounded window modestly or expose a manual “full log” path outside the normal agent prompt flow.

---

## Consolidated Rerun Gate

The executor is ready for the next full Phase 5 rerun only when:

1. TASK-001 through TASK-006 are complete.
2. The consolidated report’s minimum unblock set is satisfied:
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

TASK-009, TASK-010, and TASK-011 do not have to block the first rerun if the immediate rerun scope does not involve large-file tasks, dependency manifest edits, or heavy test-log feedback loops. But they should be completed before calling the executor production-hardened.

## Merge / Implementation Strategy

Use the consolidated issue matrix as the defect map and this plan as the workstream map:

1. Contract foundation
2. Runtime validation
3. State isolation
4. Scheduler / status / recovery semantics
5. Phase gates and conformance fixtures
6. Self-audit and portability
7. Operational safety extensions
   - scale-aware reads
   - global dependency locks
   - bounded untrusted log handling

This preserves the actionability of the consolidated remediation report while lifting the executor into a safer runtime model.

## Expected Outcome

After this v3 plan:

- the known defect set should be fixed in a traceable way
- Phase 5 reruns should be safer and more trustworthy
- the executor should validate its own contracts and guard its own state
- large-file, dependency-mutation, and test-log risks should be explicitly managed
- future planning and production use should be less vulnerable to silent contractual drift, unsafe cleanup, or ambiguous phase completion
