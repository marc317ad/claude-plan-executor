# TASK-004 — Delta-bounded cleanup

## Goal

Delta-bounded cleanup

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## Verification

- `snapshot_baseline(repo_root)` captures tracked + untracked state via git plumbing, mirroring the Codex wrapper.
- `apply_cleanup(baseline, declared_files_changed, repo_root)` restores / deletes files in `(observed_delta − declared_files_changed − protected_paths)`. Imports `_plan_paths.is_protected_path`.
- `scope_violation_detected` flips on out-of-declaration writes; `scope_misreport_detected` flips on phantom declarations.
- Concurrent dispatches on disjoint file sets do not cross-contaminate (integration test with two wrapper processes).
- **Factoring rule:** if sharing logic with `plan_codex_dispatch.py` is a ≤100-line extraction, do it now and update both wrappers in one PR; otherwise duplicate in the Claude wrapper and file a follow-up task.

## Tasks

### TASK-004: Delta-bounded cleanup

- **Status:** complete
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` (create)
  - `tests/scripts/test_claude_dispatch_cleanup.py` (create)
- **Dependencies:** [003]
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_dispatch_cleanup.py`
- **Acceptance criteria:**
  - `snapshot_baseline(repo_root)` captures tracked + untracked state via git plumbing, mirroring the Codex wrapper.
  - `apply_cleanup(baseline, declared_files_changed, repo_root)` restores / deletes files in `(observed_delta − declared_files_changed − protected_paths)`. Imports `_plan_paths.is_protected_path`.
  - `scope_violation_detected` flips on out-of-declaration writes; `scope_misreport_detected` flips on phantom declarations.
  - Concurrent dispatches on disjoint file sets do not cross-contaminate (integration test with two wrapper processes).
  - **Factoring rule:** if sharing logic with `plan_codex_dispatch.py` is a ≤100-line extraction, do it now and update both wrappers in one PR; otherwise duplicate in the Claude wrapper and file a follow-up task.
- **Reversion guidance:** none

**Description:**

Implements delta-bounded cleanup for the Claude dispatch wrapper, mirroring the proven Codex wrapper. `snapshot_baseline(repo_root)` captures the tracked + untracked state via git plumbing before the nested session runs. `apply_cleanup(baseline, declared_files_changed, repo_root)` then computes the post-dispatch delta and restores/deletes any files in `(observed_delta − declared_files_changed − protected_paths)` — the same `_plan_paths.is_protected_path` predicate as the Codex wrapper. Two flags surface for envelope reporting: `scope_violation_detected` flips when the nested session wrote outside its declared `files_changed`; `scope_misreport_detected` flips when declared files were not actually touched. Concurrent dispatches on disjoint file sets must not cross-contaminate baselines (integration test with two wrapper processes). If sharing logic with the Codex wrapper is a ≤100-line extraction, refactor in this PR; otherwise duplicate and file a follow-up.
