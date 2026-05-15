# TASK-001 — Narrow wrapper `observed_delta_*` to intersection of whole-tree diff and (declared ∪ agent-reported writes)

## Goal

Narrow wrapper `observed_delta_*` to intersection of whole-tree diff and (declared ∪ agent-reported writes)

## Context

Auto-decomposed child for TASK-001. See the source plan for broader context.

## Verification

- In `plan_claude_dispatch.py`, the wrapper's published `envelope.scope.observed_delta_tracked` and `envelope.scope.observed_delta_untracked` (the lists set in `_merge_scope_with_cleanup` at `plan_claude_dispatch.py:380–384`) are filtered through `task_in_scope_paths = set(declared_files_changed) | set(_extract_observed_files_changed(envelope))` before publication. Paths not in `task_in_scope_paths` are dropped.
- The agent-reported `files_changed` extracted at `plan_claude_dispatch.py:720` already exists in the function-local `_observed` (currently `# noqa: F841` because unused); wire it into the narrowing — do NOT call `_extract_observed_files_changed` twice.
- The narrowing happens regardless of `scope_violation_detected` / `scope_misreport_detected` (those flags continue to be computed on the unfiltered cleanup result so they still warn loudly on real scope violations; only the published *observed delta lists* are narrowed).
- `scope.declared_files_changed` is untouched by this change (it is already the trusted top-level input, not a diff).
- Add a unit test `test_observed_delta_excludes_cross_batch_leakage`: construct a fake `_merge_scope_with_cleanup` input where `cleanup_result["restored"]` contains both a task-declared file and a sibling-task file (the cross-batch leak); assert the published `observed_delta_tracked` contains only the declared file. The sibling file is dropped from the envelope but the underlying cleanup result is unchanged (verify both via the same test).
- Add a second unit test `test_observed_delta_includes_agent_self_reported_writes`: construct an envelope whose inner `result.files_changed` lists a path that is NOT in `declared_files_changed` (a scope misreport). Assert the published `observed_delta_*` retains that path (so misreports still surface).
- The four-way `scope_violation_detected` / `scope_misreport_detected` / `failed_paths` / `baseline_captured` envelope fields are byte-identical pre- and post-fix for the existing fixtures in `tests/scripts/test_plan_claude_dispatch.py` (regression pin).

## Tasks

### TASK-001: Narrow wrapper `observed_delta_*` to intersection of whole-tree diff and (declared ∪ agent-reported writes)

- **Status:** done
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py`
  - `tests/scripts/test_plan_claude_dispatch.py`
  - `tests/scripts/test_plan_claude_dispatch_cli.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch.py -k "scope or observed_delta or intersection"`
- **Acceptance criteria:**
  - In `plan_claude_dispatch.py`, the wrapper's published `envelope.scope.observed_delta_tracked` and `envelope.scope.observed_delta_untracked` (the lists set in `_merge_scope_with_cleanup` at `plan_claude_dispatch.py:380–384`) are filtered through `task_in_scope_paths = set(declared_files_changed) | set(_extract_observed_files_changed(envelope))` before publication. Paths not in `task_in_scope_paths` are dropped.
  - The agent-reported `files_changed` extracted at `plan_claude_dispatch.py:720` already exists in the function-local `_observed` (currently `# noqa: F841` because unused); wire it into the narrowing — do NOT call `_extract_observed_files_changed` twice.
  - The narrowing happens regardless of `scope_violation_detected` / `scope_misreport_detected` (those flags continue to be computed on the unfiltered cleanup result so they still warn loudly on real scope violations; only the published *observed delta lists* are narrowed).
  - `scope.declared_files_changed` is untouched by this change (it is already the trusted top-level input, not a diff).
  - Add a unit test `test_observed_delta_excludes_cross_batch_leakage`: construct a fake `_merge_scope_with_cleanup` input where `cleanup_result["restored"]` contains both a task-declared file and a sibling-task file (the cross-batch leak); assert the published `observed_delta_tracked` contains only the declared file. The sibling file is dropped from the envelope but the underlying cleanup result is unchanged (verify both via the same test).
  - Add a second unit test `test_observed_delta_includes_agent_self_reported_writes`: construct an envelope whose inner `result.files_changed` lists a path that is NOT in `declared_files_changed` (a scope misreport). Assert the published `observed_delta_*` retains that path (so misreports still surface).
  - The four-way `scope_violation_detected` / `scope_misreport_detected` / `failed_paths` / `baseline_captured` envelope fields are byte-identical pre- and post-fix for the existing fixtures in `tests/scripts/test_plan_claude_dispatch.py` (regression pin).
- **Reversion guidance:** Remove the new kwarg + the filtering lines from `_merge_scope_with_cleanup`; revert the call site to the pre-fix signature; delete the two new test cases. The wrapper envelope returns to publishing the unfiltered whole-tree diff and cross-batch leakage reappears as misleading telemetry.

**Description:**
Narrow wrapper `observed_delta_*` to intersection of whole-tree diff and (declared ∪ agent-reported writes). (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-001. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
