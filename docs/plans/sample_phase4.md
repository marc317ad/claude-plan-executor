# Sample Phase 4 Plan — /implement-plan verification fixture

**Created:** 2026-04-13
**Status:** in-progress
**Base branch:** main

## Purpose

End-to-end fixture for the `/implement-plan` dual-agent executor. Exercises:

- Codex implementer path (TASK-001, TASK-003, TASK-004)
- Claude implementer path with `plan-implementer` (TASK-002)
- Per-batch interleaving — TASK-002 and TASK-004 both touch `docs/plans/sample_phase4_scratch/settings.py` but in different batches. Without interleaving, TASK-002's review diff would see TASK-004's edits. With interleaving, the diff cleanly scopes to TASK-002.
- Dependency graph — TASK-002 depends on TASK-001; TASK-004 depends on TASK-003
- Seeded failure on TASK-004 to exercise Phase C + block-dependents

All tasks operate under `docs/plans/sample_phase4_scratch/` (a fixture scratch directory). Running the skill against this plan creates throwaway files that can be deleted post-run. Do NOT point real source files at this fixture.

## Context

The skill under test is `/implement-plan`. Its orchestrator (`plugins/plan-executor/skills/implement-plan/SKILL.md`) runs a per-batch A→E loop and dispatches via `plugins/plan-executor/scripts/plan_ops.py` and `plugins/plan-executor/scripts/plan_codex_dispatch.py`. Verification focuses on: (1) analyst outcome `valid`, (2) four narrow commits landed on top of `starting_sha`, (3) TASK-004 failed in execute mode with a `failed stage=implement` event in `docs/plans/_run_log.jsonl`, (4) TASK-002's Codex review diff contains only TASK-002's edits.

## How to run

```
# dry-run (read-only)
/implement-plan docs/plans/sample_phase4.md --dry-run

# full execute
/implement-plan docs/plans/sample_phase4.md --parallel 2

# §8.4 escalation (force Codex critical on TASK-002)
/implement-plan docs/plans/sample_phase4.md --parallel 2

# role-swap retry (force Claude critical on TASK-001)
/implement-plan docs/plans/sample_phase4.md --parallel 2

# binding mode (§8.4 bypass)
/implement-plan docs/plans/sample_phase4.md --codex-review-binding
```

## Tasks

### TASK-001: Create rename helper

- **Status:** open
- **Agent:** codex
- **Files:**
  - docs/plans/sample_phase4_scratch/rename_helper.py
- **Dependencies:** none
- **Test command:** none (mechanical task — file contents checked by fixture validator)
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/rename_helper.py` exists
  - Contains a function `def rename(old, new): ...` with a trivial body (`return {"old": old, "new": new}`)
  - No other files touched

### TASK-002: Wire rename helper into settings

- **Status:** open
- **Agent:** claude
- **Files:**
  - docs/plans/sample_phase4_scratch/settings.py
- **Dependencies:** [001]
- **Test command:** none
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/settings.py` imports `rename` from `.rename_helper`
  - Defines a module-level `SETTINGS = {"version": "phase4-sample"}`
  - Module is syntactically valid (`venv/bin/python -c "import ast; ast.parse(open('docs/plans/sample_phase4_scratch/settings.py').read())"`)

### TASK-003: Create scratch constants module

- **Status:** open
- **Agent:** codex
- **Files:**
  - docs/plans/sample_phase4_scratch/constants.py
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - File `docs/plans/sample_phase4_scratch/constants.py` exists
  - Defines `VERSION = "phase4-sample"` at module level
  - No other files touched

### TASK-004: Seed failure — wire constants into settings (conflicting)

- **Status:** open
- **Agent:** codex
- **Files:**
  - docs/plans/sample_phase4_scratch/settings.py
- **Dependencies:** [003]
- **Test command:** none
- **Acceptance criteria:**
  - **SEEDED FAILURE:** the task definition intentionally requires Codex to open `settings.py` and add `from .constants import VERSION` at the top, but also asserts the resulting file MUST NOT contain the `SETTINGS` dict (which TASK-002 just added). This contradiction triggers a Codex scope violation (the wrapper will restore; outcome=scope_violation or outcome=failure) and the orchestrator falls back to Claude, which also cannot satisfy a contradiction → Phase C `fail-task stage=implement`.
  - Expected run-log entry: `{"event":"failed","task_id":"004","stage":"implement","reason":"..."}`
  - No successful commit for this task

## Expected outcome

- 3 feat commits: `feat(TASK-001)`, `feat(TASK-002)`, `feat(TASK-003)`.
- 1 chore commit: `chore(implement-plan): run <run_id> bookkeeping`.
- TASK-004: no commit, status `failed` in this file, `failed` event in run log, cascade-block propagation (no dependents here, so no `blocked` events expected from 004).
- Plan-level `**Status:**` flips to `partial` (not `complete`, because TASK-004 failed).
- TASK-002's Codex review diff should cleanly show only `settings.py` additions from TASK-002 — not from TASK-004. This is the per-batch interleaving regression test.
