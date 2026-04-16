# TASK-013 — Independent Plan Review Gate and Reviewer-Driven Remediation Loop

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md)
**Related chunks:** [`TASK-002_runtime_validation.md`](TASK-002_runtime_validation.md), [`TASK-004_scheduler_semantics.md`](TASK-004_scheduler_semantics.md), [`TASK-005_phase_gates.md`](TASK-005_phase_gates.md), [`TASK-012_reviewer_parser_spec.md`](TASK-012_reviewer_parser_spec.md)
**Status:** proposed follow-on design note; not assigned to an existing v3 chunk
**Reason this exists:** the current executor has no independent review pass on a newly-written plan before implementation starts, and review failures still terminate mostly as revert-and-fail outcomes. This note defines a bounded design for (1) a pre-implementation plan review gate and (2) one structured reviewer-driven remediation loop before terminal failure.

---

## Goal

Add one independent review pass on the plan before execution begins, and add one bounded remediation pass after implementation review when findings are actionable. The design should improve plan quality and reduce avoidable revert cycles without turning the executor into an unbounded self-editing loop.

The intended end state is:

1. Planner writes the plan.
2. Independent reviewer reviews the plan before batch execution.
3. Plan is either approved, revised once, or halted.
4. Implementation runs.
5. Cross-review runs as it does today.
6. If the implementation review finds actionable defects, the executor may generate a repair brief and re-run implementation once before reverting.

---

## Recommendation

### 1. Add the plan review as a new gate between Phase 1 and Phase 2

The clean insertion point is **after**:

- preflight,
- plan-analyst schedule generation,
- schedule parsing/validation,

and **before**:

- schedule persistence as executable truth,
- batch dispatch.

This is effectively **Phase 1.5 — Plan Review Gate**.

Rationale:

- The plan should be reviewed after it has a concrete schedule and task routing, not while it is still freeform prose.
- This keeps the review focused on executor-relevant quality: task sizing, file scope, dependencies, acceptance criteria, reviewability, and retry safety.
- It fits naturally into TASK-005's gate model as a new gate family rather than an ad-hoc prompt.

### 2. Do not send review failures straight into immediate revert when the findings are actionable

The current model is intentionally strict, but it is too lossy when the reviewer provides concrete, bounded corrections. The better policy is:

- minor findings -> commit
- non-actionable major findings -> fail and revert
- actionable major findings -> one remediation loop

That remediation loop should be **single-shot and bounded**. No open-ended retries.

### 3. Reuse the fix-planner pattern, but do not reuse the bug-file protocol directly

The existing fix-planner / replanner work is the right conceptual precedent: it takes failure context and produces a constrained corrective plan. But the current bug workflow is coupled to bug files, CLOSED/FAILED rename semantics, and bug-specific lifecycle assumptions.

Recommendation:

- reuse the **pattern** and possibly the agent prompt style,
- do **not** route executor task failures directly through the bug-file workflow,
- instead introduce a neutral `repair-planner` or generalize fix-planner into a non-bug-specific remediation planner.

The remediation unit here is a failed executor task, not a bug record.

---

## Why this belongs in the orchestrator

The plan review and remediation loop are orchestration concerns, not worker concerns.

- `plan-analyst` should not review its own plan.
- `plan-implementer` should not decide whether reviewer findings warrant a retry.
- reviewers should produce findings, not own the retry policy.

The orchestrator is the correct owner because it already owns:

- phase order,
- gate checks,
- retry caps,
- state transitions,
- fail/commit policy.

This should therefore live primarily in:

- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
- `plugins/plan-executor/scripts/plan_ops.py`

with prompt/template changes in:

- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`

and seam validation/tests in:

- `tests/scripts/test_plan_ops.py`
- `tests/scripts/test_plan_codex_dispatch_integration.py`

---

## Proposed Flow

### A. Plan review gate

**New Phase 1.5**

1. `plan-analyst` produces schedule JSON.
2. Orchestrator validates the schedule as usual.
3. Orchestrator dispatches an independent plan reviewer.
4. Reviewer returns one of:
   - `approved`
   - `approved-with-notes`
   - `needs-replan`
5. Routing:
   - `approved` -> proceed
   - `approved-with-notes` -> proceed, carry notes into summary
   - `needs-replan` -> dispatch planner/replanner once with findings
6. Re-reviewed revised plan:
   - `approved` or `approved-with-notes` -> proceed
   - `needs-replan` again -> halt before implementation

### B. Implementation review remediation loop

**New Phase D.2c**

When cross-review returns a blocking verdict:

1. Parse reviewer findings into a structured form.
2. Evaluate whether findings are actionable:
   - must contain concrete location,
   - must describe an implementable correction,
   - must stay within declared task scope or a documented allowed scope-expansion path.
3. If actionable:
   - dispatch remediation planner once,
   - generate a repair brief or mini-task,
   - re-run implementation once,
   - re-run the original opposite-side reviewer once.
4. If the re-review still blocks, fail and revert.
5. If the findings are not actionable, fail and revert immediately.

This loop is the task-executor equivalent of reviewer-guided retry in the fix-bugs pipeline, but generalized and explicitly bounded.

---

## Reviewer roles

### Plan review

Best default:

- Claude authors the plan
- Codex reviews the plan

Reason:

- this keeps planning and plan review on different model families,
- Codex is good at concrete repo-grounded criticism of file scope, task sizing, and acceptance criteria.

Fallback if Codex is unavailable:

- either degrade to warning-only and continue,
- or use a distinct Claude reviewer profile, but mark the run as lacking independent plan review.

Do not let the original planning agent review its own output.

### Post-implementation remediation planning

Best default:

- use a dedicated remediation planner role,
- not the original implementer,
- not the same reviewer.

If you want the smallest change set, the original planner may produce the repair brief, but only from structured reviewer findings and under strict scope limits.

---

## Suggested contracts

### 1. `parse-plan-review-report`

Add a helper analogous to `parse-implementer-report` and `parse-reviewer-report`:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py parse-plan-review-report --stdin --reviewer <codex|claude> --json
```

Canonical output:

```json
{
  "reviewer": "codex",
  "verdict": "approved|approved-with-notes|needs-replan",
  "summary": "short summary",
  "findings": [
    {
      "severity": "important|minor",
      "task_id": "001",
      "category": "scope|dependency|acceptance|test|routing|batching",
      "issue": "what is wrong",
      "suggested_fix": "concrete correction"
    }
  ],
  "warnings": [],
  "errors": []
}
```

### 2. `parse-remediation-plan`

If a remediation planner is introduced, give it a narrow contract:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py parse-remediation-plan --stdin --json
```

Canonical output:

```json
{
  "outcome": "repairable|not-repairable",
  "scope_ok": true,
  "task_id": "001",
  "files": ["src/foo.py"],
  "repair_brief": "bounded implementation brief",
  "reason": ""
}
```

The executor should refuse remediation plans that expand scope silently.

---

## Acceptance policy

### Plan review gate findings should focus on executor quality, not implementation semantics

The reviewer should check:

- task boundaries are small enough and internally coherent,
- file lists match the described work,
- dependency graph is plausible,
- batch parallelism assumptions are safe,
- acceptance criteria are measurable,
- test commands are real and specific,
- Codex-tier classification is justified.

This is not a duplicate of code review. It is a workflow-quality review.

### Remediation loop should trigger only for actionable findings

Use a strict actionability test:

- explicit file/path or task-local target,
- concrete correction, not vague concern,
- no hidden architecture expansion,
- no second retry beyond the one remediation pass.

If any of those are missing, halt and revert.

---

## Verification

**V1 — Plan review gate exists in the design and skill.**

`DUAL_AGENT_PLAN_EXECUTOR.md` and `SKILL.md` both describe a Phase 1.5 or equivalent named plan-review gate.

**V2 — Plan review is independently parseable.**

`plan_ops.py` exposes `parse-plan-review-report` with verdict validation and structured findings.

**V3 — Plan review can halt execution before implementation.**

A fixture with an underspecified task yields `needs-replan`, and the executor stops before Phase 2.

**V4 — One replan pass is allowed.**

A fixture with a repairable plan issue triggers one planner revision pass, then proceeds only if the revised plan is approved.

**V5 — Review remediation loop is bounded.**

A failing implementation review with actionable findings triggers one remediation plan and one implementation retry. A second failure terminates.

**V6 — Non-actionable review findings do not trigger repair planning.**

The executor fails and reverts immediately when findings are vague, out of scope, or structurally inconsistent.

---

## Tasks

### TASK-013: Add plan review gating and reviewer-driven remediation planning

- **Status:** proposed
- **Priority:** high
- **Files:**
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py`
  - `tests/scripts/test_plan_ops.py`
  - `tests/scripts/test_plan_codex_dispatch_integration.py`
- **Dependencies:** TASK-002, TASK-004, TASK-005, TASK-012
- **Test command:** none
- **Acceptance criteria:**
  - The executor has an explicit pre-implementation plan-review gate with structured verdicts `approved | approved-with-notes | needs-replan`.
  - The orchestrator can halt execution on plan-review failure before any batch implementation begins.
  - The orchestrator can perform at most one planner/replanner revision pass from structured plan-review findings.
  - The executor supports one bounded reviewer-driven remediation loop after blocking implementation review findings when those findings are actionable.
  - Non-actionable or scope-expanding review findings still terminate via fail-and-revert.
  - The design docs explicitly distinguish bug-fix replanning from executor-task remediation planning.

**Description:**
Introduce an independent plan review stage before execution and a bounded repair-planning stage after actionable review failure. The goal is to improve plan quality and recover from concrete reviewer findings without creating an unbounded autonomous loop.

**Implementation notes:**
Prefer a new neutral remediation planner over directly reusing the bug-file fix-planner protocol. Reuse its ideas, not its file lifecycle.

**Reversion guidance:**
If the remediation loop proves too complex, keep the plan-review gate and temporarily disable only the post-review remediation path behind a feature flag. Do not collapse back to having no independent plan review.

---

## Implementation sketch

### Step 1 — plan-review gate

Add a new gate in TASK-005's vocabulary:

- `plan-review-safe`

This gate is satisfied only after a valid plan-review report returns `approved` or `approved-with-notes`.

### Step 2 — reviewer prompt/template

Add a dedicated plan-review template. It should review:

- task decomposition,
- scope correctness,
- dependency correctness,
- acceptance/test quality,
- routing suitability,
- retry safety.

Do not let it drift into implementation design.

### Step 3 — one replan pass

On `needs-replan`:

- dispatch planner/replanner once with structured findings,
- regenerate the schedule if task structure changed,
- re-run plan review once,
- then either proceed or halt.

### Step 4 — remediation planner path

On blocking implementation review:

- pass the original task block,
- implementation report,
- structured reviewer findings,
- scope constraints,

to a dedicated remediation planner. Its output is a bounded repair brief, not a whole new freeform plan file.

### Step 5 — enforce retry ceiling

Track two separate counters:

- `plan_replan_attempts <= 1`
- `review_remediation_attempts <= 1`

Do not share them. Do not permit recursion.

---

## Out of Scope

- Full generalization of the fix-bugs pipeline.
- Multi-pass autonomous planning loops.
- Automatic task-scope expansion without human approval.
- Replacing asymmetric cross-review; this task adds a new gate and a new bounded repair path, it does not change reviewer ownership.

---

## Decision note

If this ships incrementally, land it in two slices:

1. **Plan review gate first.** This is the highest-value, lowest-risk addition.
2. **Reviewer-driven remediation second.** This is valuable, but it adds more state transitions and needs tighter contracts.

That split preserves momentum if the remediation design needs refinement.
