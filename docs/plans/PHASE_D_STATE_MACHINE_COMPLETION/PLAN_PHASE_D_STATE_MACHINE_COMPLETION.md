# Plan: Phase D State Machine Completion

**Created:** 2026-04-30
**Status:** partial
**Base branch:** main
**Related:**
- `docs/plans/archive/PHASE_D_STATE_MACHINE/PLAN_PHASE_D_STATE_MACHINE.md`
- `plugins/plan-executor/scripts/plan_ops.py`
- `plugins/plan-executor/scripts/review_route_input_schema.json`
- `plugins/plan-executor/scripts/review_route_output_schema.json`
- `plugins/plan-executor/scripts/schemas/mcp/review_route.input.json`
- `plugins/plan-executor/scripts/schemas/mcp/review_route.output.json`
- `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
- `tests/scripts/test_plan_ops_review_route.py`
- `tests/scripts/test_phase_d_e2e.py`
- `tests/scripts/test_plan_ops_mcp_conformance.py`

---

## Goal

Finish the Phase D state-machine migration by closing the gaps between the landed `review-route` implementation, the MCP transport, and the current `implement-plan` SKILL contract. The end state is that every Phase D routing decision described in the SKILL is accepted by `plan_ops__review_route`, logged through ordinary allowlisted events, covered by CLI and MCP tests, and validated against the Gemini fallback seams.

## Context

The archived Phase D plan landed the core router, schedule-state helpers, wrapper sanitizer, SKILL diet, and an e2e smoke. The implementation is useful but not fully complete as the authoritative Phase D state machine:

- `review-route` routes primarily by `implementer`, so Claude-implemented work reviewed by Claude under `claude_only=true` still looks like an unknown Codex-verdict shape unless the orchestrator hand-adapts the payload.
- Gemini fallback exists for transient Codex review failure, but `review-route` has no first-class reviewer identity and no tests proving Gemini review envelopes are routed identically to Codex review envelopes on Claude-implemented work.
- `--codex-review-binding` prose says the route depends on `$UNATTENDED_REVERT_POLICY`, but the router currently returns a generic `fail` action without carrying the pause-vs-fail-fast policy decision.
- The e2e smoke asserts a `review_route_called` event, but that event is not in `ALLOWED_LOG_EVENTS`, so the test bypasses the public `log-event` path with `_append_run_log`.
- MCP registration for `plan_ops__review_route` exists, but the Phase D e2e still leans on direct Python calls and subprocess CLI instead of asserting the MCP tool is the orchestrator-facing contract.

This plan does not re-open the completed Phase D work. It adds the missing completion layer around the landed code.

## Verification

- `venv/bin/python -m pytest tests/scripts/test_plan_ops_review_route.py`
- `venv/bin/python -m pytest tests/scripts/test_phase_d_e2e.py`
- `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_plan_ops_mcp_schemas.py`
- `venv/bin/python -m pytest tests/scripts/test_plan_gemini_dispatch_review.py`
- `venv/bin/python plugins/plan-executor/scripts/plan_ops.py build-tasks --plans-dir docs/plans/PHASE_D_STATE_MACHINE_COMPLETION --json`
- Gemini validation of this plan text before execution; findings must be either incorporated or documented in the execution log with a concrete dismissal rationale.

## Non-goals

- Rewriting Phase 1.5 plan-review routing; that is tracked by `docs/plans/PHASE_1_5_STATE_MACHINE`.
- Replacing the CLI wrappers with MCP for wrapper-internal subprocess work.
- Reworking the already-landed MCP server architecture or registry generator.
- Removing the `plan_ops.py review-route` CLI shim.
- Changing reviewer verdict vocabularies emitted by Codex, Gemini, or Claude wrappers.

## Execution

Codex is the required implementer for every task in this plan.

Five tasks, five batches:

- **Batch 1:** TASK-001.
- **Batch 2:** TASK-002.
- **Batch 3:** TASK-003.
- **Batch 4:** TASK-004.
- **Batch 5:** TASK-005.

---

## Tasks

### TASK-001: Expand `review-route` input contract for reviewer identity and runtime mode

- **Status:** done
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/review_route_input_schema.json`
  - `plugins/plan-executor/scripts/review_route_output_schema.json`
  - `tests/scripts/test_plan_ops_review_route.py`
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_review_route.py`
- **Acceptance criteria:**
  - `review-route` accepts first-class `reviewer: "codex" | "gemini" | "claude" | "none"` and `claude_only: bool` fields while remaining backward-compatible with existing payloads for one release window.
  - Claude-implemented work reviewed by Claude under `claude_only=true` routes `ship | ship-with-fixes` to `commit` and `needs-rework` to the D.4 path without D.5, D.2a.5, or D.2a.6.
  - Claude-implemented work reviewed by Gemini under `claude_only=false` routes with the same verdict vocabulary and actions as Codex review.
  - Codex-implemented work reviewed by Claude keeps the existing D.2b role-swap behavior.
  - `reviewer:"none"` is accepted only for the explicit skip-review path and routes to `commit` with reviewer metadata suitable for `commit-task reviewer=none`.
  - Unknown reviewer/runtime combinations return `unknown_state`, not a Python exception and not an orchestrator-side branch.
  - Input and output schemas document the compatibility window and the final canonical fields.

**Description:** Makes the Python state machine match the actual Phase D dispatch matrix instead of relying on the orchestrator to infer reviewer semantics from the implementer alone.

**Reversion guidance:** Revert this task's schema and router changes. Existing narrower `implementer`-only routing remains intact.

---

### TASK-002: Encode binding and unattended-policy decisions in `review-route`

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/review_route_input_schema.json`
  - `plugins/plan-executor/scripts/review_route_output_schema.json`
  - `tests/scripts/test_plan_ops_review_route.py`
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_review_route.py`
- **Acceptance criteria:**
  - `review-route` accepts `unattended_revert_policy: "pause" | "fail-fast" | "preserve-only"` in the payload, defaulting to `"pause"` for backward compatibility.
  - `codex_review_binding=true` plus Codex or Gemini `needs-rework` on Claude work returns `pause_awaiting_user` with `stage:"post_binding_block"` when policy is `"pause"`.
  - The same binding branch returns `fail` only when policy is `"fail-fast"` or `"preserve-only"`, and the output carries an explicit `authorization_source` or equivalent machine-readable field for the follow-on `fail-task` path.
  - D.4 fail/rescue entries are distinguishable from binding-policy entries in the output payload so the SKILL does not have to inspect free-text `fail_reason`.
  - Tests cover all three policies and assert that no D.5/remediation actions are reachable from the binding branch.

**Description:** Moves the last policy-sensitive Phase D routing decision out of SKILL prose and into the deterministic router.

**Reversion guidance:** Drop the new payload field and restore the existing binding branch.

---

### TASK-003: Make `review_route_called` a first-class run-log event

- **Status:** Pending
- **Implementer:** codex
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_phase_d_e2e.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_phase_d_e2e.py tests/scripts/test_plan_ops.py -k 'log_event or phase_d or review_route_called'`
- **Acceptance criteria:**
  - `review_route_called` is added to `ALLOWED_LOG_EVENTS` and to the SKILL's post-compaction allowlist reference.
  - Phase D e2e emits `review_route_called` through the public `log-event` subcommand or pure-core equivalent, not by calling `_append_run_log` directly.
  - The event payload records `run_id`, `task_id`, `action`, `reviewer`, `implementer`, and a compact route reason without copying raw reviewer free text.
  - Existing event-order assertions remain strict and pass through the public logging path.
  - Run-log validation rejects malformed `review_route_called` payloads with structured errors if such validation exists for adjacent events.

**Description:** The original Phase D plan expected this event, but the current implementation only smokes it through a private helper. This task makes the audit event real.

**Reversion guidance:** Remove the event from the allowlist and revert the e2e test to its prior private append helper.

---

### TASK-004: Bring MCP conformance up to the completed route contract

- **Status:** Pending
- **Implementer:** codex
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/schemas/mcp/review_route.input.json`
  - `plugins/plan-executor/scripts/schemas/mcp/review_route.output.json`
  - `plugins/plan-executor/scripts/schemas/mcp/_index.json`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `tests/scripts/test_plan_ops_mcp_conformance.py`
  - `tests/scripts/test_plan_ops_mcp_registrations.py`
  - `tests/scripts/test_plan_ops_mcp_schemas.py`
- **Dependencies:** TASK-001, TASK-002, TASK-003
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_registrations.py tests/scripts/test_plan_ops_mcp_schemas.py`
- **Acceptance criteria:**
  - `plan_ops__review_route` MCP input/output schemas expose the TASK-001 and TASK-002 fields with the same defaults and enums as the CLI schema.
  - `_index.json` still maps `plan_ops__review_route` to subcommand `review-route`; regenerated server callables continue to dispatch `_run_review_route`.
  - MCP conformance fixtures cover Codex, Gemini, Claude-only, binding pause, binding fail-fast, skip-review, and unknown-state cases.
  - Schema-invalid MCP payloads return MCP tool errors; semantically unknown states return normal `unknown_state` results.
  - Argparse/MCP drift tests fail if the CLI and MCP contracts diverge on reviewer identity, policy fields, or output action shapes.

**Description:** Keeps the landed MCP migration aligned with the now-complete Phase D router.

**Reversion guidance:** Restore the previous MCP schema/index/server artifacts and remove the new conformance fixtures.

---

### TASK-005: Update SKILL and e2e smoke to use the completed MCP route contract

- **Status:** Pending
- **Implementer:** codex
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `tests/scripts/test_phase_d_e2e.py`
  - `tests/scripts/test_plan_gemini_dispatch_review.py`
- **Dependencies:** TASK-002, TASK-003, TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_phase_d_e2e.py tests/scripts/test_plan_gemini_dispatch_review.py`
- **Acceptance criteria:**
  - Phase D.2 SKILL prose names the canonical `review-route` payload fields: `implementer`, `reviewer`, `claude_only`, `unattended_revert_policy`, `flags`, `retries_used`, `reviewer_envelope`, and optional `d5_envelope`.
  - SKILL no longer relies on prose-only special cases for Claude-only, Gemini fallback, binding mode, or skip-review routing when those cases can be expressed in the route payload.
  - E2e coverage includes at least one MCP-backed `plan_ops__review_route` call path or the local MCP server harness equivalent.
  - Gemini fallback review is validated through the wrapper tests and through a route test proving Gemini `clean | minor-findings | needs-rework` maps like Codex on Claude-implemented work.
  - `dispatch-templates.md` remains aligned with the route output `dispatch_context` shape after any naming changes introduced by TASK-001/TASK-002.

**Description:** Makes the reduced SKILL depend on the Python/MCP state machine for the final edge cases, preserving the token-saving intent of the original Phase D migration.

**Reversion guidance:** Revert SKILL and test edits. Runtime router changes from prior tasks remain usable through direct callers.

## Execution log — 20260501T151343 (partial)

Starting SHA: `eaf18d3b4bb094798163b53241c0996101a6c8b4`  → Ending SHA: `5de62e8b0f6bc7990f5b79115c0c0e300361013b`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-001 | claude (codex-fallback) | codex | needs-rework [disagreement] | 5de62e8b | D.5 dismissed lone finding; reviewer=none gating filed as upstream contract follow-up |
