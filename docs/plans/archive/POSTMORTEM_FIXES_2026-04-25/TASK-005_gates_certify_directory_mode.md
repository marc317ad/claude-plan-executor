# TASK-005 — `gates --certify` accepts directory-mode `--plan-file`

## Goal

End-of-run `gates --certify --plan-file <plan-dir>` should succeed when the plan is a directory (with `00_INDEX.json` + `TASK-NNN_*.md` children) — same resolution rule used by `commit-task` / `fail-task` / `block-dependents` per `SKILL.md:99–106`. Today, `--certify` halts on `schema-valid` and `commit-safe` with `plan file not found: <dir>` because both subchecks expect a single markdown file.

## Context

**The friction.** End of run 20260425T041800: `gates --certify --plan-file docs/plans/DUAL_AGENT_Plans` returned `certified: false`. Both `schema-valid` and `commit-safe` reported `plan file not found: docs/plans/DUAL_AGENT_Plans`. Per-commit `commit-safe` had passed at every individual commit, so this is purely a cosmetic aggregate failure — but it gives the operator a misleading "false" verdict on an otherwise clean run, and the lock-release decision had to be made on a manual judgement call.

**The resolution rule.** For directory-mode `<plan-file>`:

- **`schema-valid`:** resolve to the first chunk in `00_INDEX.json` (or iterate every chunk; either is acceptable as long as the result is the AND of the per-chunk schema validity).
- **`commit-safe` (re-check):** events in `_run_log.jsonl` already carry `plan_file` per child task. Iterate every committed task's child file when the certify request is directory-mode.

**Scope.** `cmd_gates --certify` branch in `plan_ops.py`; tests covering both single-file (existing) and directory-mode (new) certification, plus the cross-mode invariant ("a directory whose every child certifies should itself certify").

## Verification

- `gates --certify --plan-file <plan-dir>` returns `certified: true` when every child plan in `00_INDEX.json` certifies under the existing single-file rule.
- The certify report's per-check JSON includes a `mode` key (`single-file | directory`) so the result shape is self-describing.
- Per-check `schema-valid` aggregates: every child's `schema-valid` result is included; the aggregate is `pass` iff all children pass.
- Per-check `commit-safe` aggregates: every committed task's `commit-safe` re-check is run against its child plan file (resolved via `_run_log.jsonl` events).
- A new test fixture (or reuse of `sample_phase4` if it can be made directory-shaped) exercises a 2-chunk directory plan with at least one committed task; certify returns `true`.
- A negative test: a directory plan whose one child has a schema violation certifies as `false` and the per-check output names the offending child.
- Existing single-file `--certify` tests pass unchanged.

## Tasks

### TASK-005: Directory-mode certify resolution

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — `cmd_gates --certify` directory-mode resolution; helpers shared with existing `_resolve_directory_plan_file`-style sites if present)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (small edit — anchor 99–106 mention `gates --certify` alongside `commit-task` etc.)
  - `tests/scripts/test_plan_ops.py` (regression tests for directory-mode certify)
- **Dependencies:** [004]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k certify`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:90-120`
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::cmd_gates`
- **Acceptance criteria:**
  - `cmd_gates --certify` detects when `--plan-file` is a directory containing `00_INDEX.json` and applies the directory-mode resolution rule documented in SKILL.md §99–106.
  - For `schema-valid`: every chunk in `00_INDEX.json` is schema-validated; the aggregate result is `pass` iff every chunk passes; per-chunk results are emitted in the report.
  - For `commit-safe`: every event of type `commit-task` (or equivalent) in `_run_log.jsonl` for the run is re-validated against its child plan file (per-event `plan_file` field). Aggregate result is `pass` iff every per-event re-check passes.
  - The certify report JSON shape gains a top-level `mode: "single-file" | "directory"` and per-check `subresults` array when in directory mode (single-file mode is unchanged).
  - SKILL.md §99–106 is amended to list `gates --certify` alongside `commit-task` / `fail-task` / `block-dependents` as commands honouring directory-mode `<plan-file>` resolution.
  - Existing single-file certify tests pass unmodified.
  - New tests cover the success path AND the per-child-failure attribution path.
- **Reversion guidance:** revert the directory-mode branch + SKILL anchor; single-file behavior is unchanged.

**Description:**
Make `gates --certify` directory-aware. Aggregate per-chunk `schema-valid` and per-event `commit-safe` re-checks under one self-describing report shape. Update the SKILL.md directory-mode resolution rule to include certify.

**Implementation notes:**
- If `_resolve_directory_plan_file` (or a similarly-named helper) already exists for `commit-task`, reuse it — don't fork a parallel resolver.
- The per-event `plan_file` lookup is the source of truth for directory-mode `commit-safe`; do NOT scan the directory tree for `TASK-NNN_*.md` files independently.
- The aggregate "AND of subchecks" rule is the simple correct choice; resist the temptation to weight or partially credit.

## Execution log — 20260426T032452 (success)

Starting SHA: `4220c0aa16b60579f826d4d8a8ed3150cc7d39a4`  → Ending SHA: `5d5a0637e945998fe546965c0f7764ebf3b53b71`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 005 | claude | codex | ship-with-fixes [disagreement] | b8b67d3f | D.5 spec-deference: existing mode field carries dry-run|execute; plan_mode is the new directory|single-file discriminator. |
