# TASK-001 — Plumbing — `claude_only` routing flag and mutual exclusions

## Goal

Plumbing — `claude_only` routing flag and mutual exclusions

## Context

Hoist the routing decision to one place so TASK-002 and TASK-003 each have a single boolean to consult. The flag is bound at Phase 0 from the union of `--claude-only` (operator opt-in) and `codex_available=false` (preflight signal). This task is prose-only — it documents the binding and the new mutual exclusions but does not change any review or cross-review behavior yet. Behavior changes land in TASK-002 (plan review) and TASK-003 (cross-review).

The mutual-exclusion checks follow the existing prose-driven pattern at `SKILL.md:149`. We do NOT add a structural checker in `plan_ops.py` because that would be inconsistent with how `--codex-only` ⊕ `--claude-only` is enforced today; the orchestrator LLM reads the prose and halts pre-dispatch. If empirically the prose-only mutex turns out to be unreliable, we add a `plan_ops.py validate-flags` subcommand as a separate follow-up.

## Verification

- `SKILL.md` §Parse arguments documents that `--codex-review-binding` is mutually exclusive with `--claude-only` and that `--codex-plan-review-binding` is mutually exclusive with `--claude-only`. The mutex prose lives next to the existing `--codex-only` ⊕ `--claude-only` clause at `SKILL.md:149`.
- Phase 0 prose binds a single boolean `claude_only` derived from `--claude-only OR (codex_available == false from preflight)`. The binding is documented in `SKILL.md` §Pre-flight (Phase 0) so every downstream phase reads one variable.
- `SKILL.md` §Rules adds a hard rule: *"When `claude_only=true`, the orchestrator MUST NOT invoke `plan_codex_dispatch.py` for ANY subcommand (`plan-review`, `review`, `implement`). Codex shell-out under `claude_only` is a protocol violation."*
- `run_start` event documents that it carries a `claude_only: <bool>` field in `fields` (the field is added in TASK-002 and TASK-003 as those phases are wired; TASK-001 only documents the binding).
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (regression check — no new tests are required; the change is prose-only).

## Tasks

### TASK-001: Plumbing — `claude_only` routing flag and mutual exclusions

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (edit)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - `SKILL.md` §Parse arguments documents that `--codex-review-binding` is mutually exclusive with `--claude-only` and that `--codex-plan-review-binding` is mutually exclusive with `--claude-only`. The mutex prose lives next to the existing `--codex-only` ⊕ `--claude-only` clause at `SKILL.md:149`.
  - Phase 0 prose binds a single boolean `claude_only` derived from `--claude-only OR (codex_available == false from preflight)`. The binding is documented in `SKILL.md` §Pre-flight (Phase 0) so every downstream phase reads one variable.
  - `SKILL.md` §Rules adds a hard rule: *"When `claude_only=true`, the orchestrator MUST NOT invoke `plan_codex_dispatch.py` for ANY subcommand (`plan-review`, `review`, `implement`). Codex shell-out under `claude_only` is a protocol violation."*
  - `run_start` event documents that it carries a `claude_only: <bool>` field in `fields` (the field is added in TASK-002 and TASK-003 as those phases are wired; TASK-001 only documents the binding).
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` passes (regression check — no new tests are required; the change is prose-only).
- **Reversion guidance:** none

**Description:**
Hoist the routing decision to one place so TASK-002 and TASK-003 each have a single boolean to consult. The flag is bound at Phase 0 from the union of `--claude-only` (operator opt-in) and `codex_available=false` (preflight signal). This task is prose-only — it documents the binding and the new mutual exclusions but does not change any review or cross-review behavior yet. Behavior changes land in TASK-002 (plan review) and TASK-003 (cross-review).

The mutual-exclusion checks follow the existing prose-driven pattern at `SKILL.md:149`. We do NOT add a structural checker in `plan_ops.py` because that would be inconsistent with how `--codex-only` ⊕ `--claude-only` is enforced today; the orchestrator LLM reads the prose and halts pre-dispatch. If empirically the prose-only mutex turns out to be unreliable, we add a `plan_ops.py validate-flags` subcommand as a separate follow-up.

## Execution log — 20260426T010342 (success)

Starting SHA: `70103df4c2f5aa70d5233ecf4ec6cf1d0133a0bd`  → Ending SHA: `4817f6f8652704fe22a1e6da5cd912f09a6f313d`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude_fallback | codex | clean | 281c6fb7 | codex implement timed out 300s; claude fallback succeeded |
