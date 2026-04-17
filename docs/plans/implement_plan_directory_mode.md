# Directory-mode execution for `/implement-plan`

**Created:** 2026-04-17
**Status:** pending
**Base branch:** main

## Purpose

Let `/implement-plan <dir>` walk a decomposed-plan directory (e.g., `docs/plans/decompose_plans_tasks/build-plan-decomposer-plugin/`) and run each child plan sequentially in `00_INDEX.json` topological order, so an 18-child decomposition is a single invocation instead of 18. Single-file invocations are unchanged. No cross-child parallelism in v1.

## Tasks

### TASK-001: `walk-directory` subcommand + Kahn sort

- **Status:** open
- **Agent:** codex
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k walk_directory -x`
- **Acceptance criteria:**
  - `plan_ops.py walk-directory --plans-dir <dir> --json` reads `<dir>/00_INDEX.json`, Kahn-sorts the plan entries by `depends_on`, and emits `{"pass": bool, "ordered": ["<abs plan path>", ...], "errors": [{"code","message"}]}` on stdout.
  - Errors surfaced with `pass: false` and non-zero exit: missing `00_INDEX.json`, malformed JSON, cycle, unknown dep reference, index entry whose `file` does not resolve inside `--plans-dir`.
  - Ordered paths are absolute; order is stable — Kahn with tiebreaker on `task_id` lexicographic.
  - Reuses the existing `_parse_index_roster` loader used by `check-plan-deps` (plan_ops.py ~line 1107). No new filesystem writes, no git calls.
  - Unit tests cover: happy path, cycle, missing index, unknown dep, single-entry index, empty roster, out-of-dir `file` reference.
- **Description:** Pure library subcommand registered alongside `check-plan-deps` in `build_parser()`; handler returns JSON + exit code mirroring other `plan_ops.py` commands.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

### TASK-002: SKILL.md directory-mode loop + flag propagation + aggregate summary

- **Status:** open
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md
- **Dependencies:** [001]
- **Test command:** none
- **Acceptance criteria:**
  - A new "Directory-mode driver" section precedes "Pre-flight (Phase 0)". If `<plan-path>` resolves to a directory, the orchestrator calls `plan_ops.py walk-directory --plans-dir <dir> --json`, halts on `pass: false`, and otherwise iterates the returned `ordered[]` — running the existing Phase 0 → End-of-run flow per child.
  - **Inside each child run under directory-mode, the `check-plan-deps` step is skipped.** Topology is already enforced by `walk-directory`, and `00_INDEX.json` entry statuses are not synced during the outer loop, so the in-child gate would falsely halt every child after the first. This skip MUST be explicitly documented in the new section and scoped strictly to directory-mode (single-file invocations still run the gate).
  - Flags propagated to every child run: `--dry-run`, `--parallel N`, `--codex-only`, `--claude-only`, `--skip-cross-review`, `--codex-review-binding`, `--allow-gaps`, `--strict-branch`.
  - `--task-ids` combined with a directory path halts before the walk with a clear error (ambiguous across children).
  - The outer loop halts if any child ends with `**Status:** partial` or any of its phases halts; no continue-on-failure path in v1. Remaining un-run children are listed as `skipped` in the aggregate summary.
  - End-of-run aggregate summary lists each child's outcome (`complete` / `partial` / `halted` / `skipped`), any failed `task_id`s per child, and aggregate disagreement / minor-findings counts.
  - A noted follow-up: after a successful directory run the children's `00_INDEX.json` status entries remain `Pending`, so re-running a single child through `/implement-plan <child.md>` will halt on `dep-not-done` until a future index-sync step lands. Call this out in the new section.
  - When `<plan-path>` resolves to a file, behavior is byte-identical to today.
- **Description:** Protocol-documentation edit only. No script changes. Preserve the existing Phase 0 → End-of-run text verbatim and reference `walk-directory` from TASK-001.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md`

## Expected outcome

- 2 feat commits (`feat(TASK-001)`, `feat(TASK-002)`) plus one bookkeeping `chore(implement-plan)` commit. Tasks run sequentially (002 depends on 001).
- `/implement-plan docs/plans/decompose_plans_tasks/build-plan-decomposer-plugin/ --dry-run` prints an 18-child schedule in topological order without edits.
- Plan-level `**Status:**` flips to `complete`.
