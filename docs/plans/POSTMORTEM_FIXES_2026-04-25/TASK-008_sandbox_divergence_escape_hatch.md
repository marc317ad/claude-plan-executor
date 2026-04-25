# TASK-008 — Codex sandbox-divergence escape hatch

## Goal

Close the protocol gap that surfaced on TASK-027C: when the Codex wrapper reports `outcome: failure` because its sandbox test run failed, but the same test command passes in the target environment, the orchestrator today has no sanctioned recovery. The run had to commit TASK-027C with `--reviewer none --reviewer-verdict ""` (technically allowed but bypassing the cross-review contract).

This task ships two complementary mechanisms:

1. **Surface sandbox test output in the Codex envelope** so the orchestrator can distinguish "real test red" from "sandbox missing dep / permission / path divergence."
2. **Auto-validate in the target env on test-run failure** so the orchestrator can re-run the same test command before classifying the failure. If target-env tests pass, mark the work `[sandbox-divergence]` in the commit body and surface the divergence in the run summary for human review.

## Context

**The friction.** During run 20260425T041800, Codex's `implement` dispatch for TASK-027C wrote both files correctly (4-line `dispatch-templates.md` addition + 18-line test addition), but the wrapper reported `outcome: failure` with `error: "Independent test run failed after 2 attempt(s)"`. Running the same test command in the target env immediately afterward showed all 14 tests passing. The wrapper's sandbox test stdout/stderr are not surfaced in the envelope, so the failure was opaque from the orchestrator's perspective.

**The escape hatch design.**

- Codex wrapper `implement` envelope (failure-path): add `sandbox_test_stdout` (string, capped to N KB), `sandbox_test_stderr` (string, capped to N KB), `sandbox_test_command` (string), `sandbox_test_exit_code` (int), `sandbox_test_attempt_count` (int).
- Orchestrator (`plan_ops.py` or the implementer-fallback dispatcher): on `outcome: failure` with `cause: independent_test_run_failed` (or analogous), automatically re-run the test command in the target env. Behaviour:
  - **Target-env passes:** treat the work as success. Tag the commit body `[sandbox-divergence]` (alongside any existing `[disagreement]` / `[remediation]` tags). Append a structured `sandbox_divergence` block to the run-log event for the task containing the wrapper's sandbox stdout/stderr (truncated) and the target-env stdout/stderr (truncated). Surface the divergence in the end-of-run summary.
  - **Target-env fails:** treat as a real failure. Do not re-route. The orchestrator's existing failure-path applies (Codex→Claude fallback, or task-fail).
- New `commit-task` flag `--sandbox-divergence-tag` accepts the tag (string-only, no semantic effect on the verdict whitelist) and inscribes it into the commit body alongside `[disagreement]` etc.

**Dependency.** Sequenced after TASK-001 (the unified-normalizer verification task) so the `out_of_scope_observed` failure mode and the `independent_test_run_failed` failure mode don't interfere during testing.

**Cross-plan invariant.** CODEX_FRICTION_2026-04-25 TASK-001 lands a structural test that asserts every `codex_*_schema.json` file's nested objects with `additionalProperties: false` have `set(node['required']) == set(node['properties'].keys())`. The five new optional fields this task adds to `codex_implement_schema.json` MUST honour that invariant — declare each as `"type": ["string", "integer", "null"]` (per the optional-but-required convention) and include them in the `required` array of their parent object. The implementer must run the structural test from CODEX_FRICTION TASK-001 (or its successor) and confirm pass.

**Scope.** Wrapper envelope schema additions + orchestrator auto-validate branch + commit-tag plumbing + tests for both branches (target-passes, target-fails). NO changes to the `--reviewer none` escape — that remains as a final-resort override.

## Verification

- Codex wrapper `implement` failure envelope (when the failure is a test run) carries the five new fields above. Existing fields are unchanged.
- New unit tests for the wrapper assert: (a) success path envelope has no sandbox-test fields; (b) test-failure envelope carries all five fields; (c) stdout/stderr are truncated at the documented cap with a `truncated_to` marker.
- Orchestrator auto-validate branch:
  - On wrapper `outcome: failure` + `cause: independent_test_run_failed`, the orchestrator runs the same test command (resolved from the task's `Test command:` field) in the target env.
  - Target-passes: the run-log event for the task gains a `sandbox_divergence` block; the next `commit-task` invocation includes `--sandbox-divergence-tag`; the commit body shows `[sandbox-divergence]` (and the task's existing tags); cross-review proceeds as if the implementer succeeded.
  - Target-fails: the orchestrator records the divergence as `divergence_check_failed`; the existing failure path (Codex→Claude fallback or task-fail) runs unchanged.
- Run summary (end-of-run report): a "Sandbox divergences" subsection lists every task that hit the auto-validate branch, with file:line references to the wrapper stdout/stderr captures.
- Tests cover both auto-validate outcomes plus a no-divergence baseline (failure-path runs as before when the wrapper failure is NOT a test-run failure).
- `commit-task --sandbox-divergence-tag` accepts the tag and writes it to the commit body in the canonical position alongside `[disagreement]` / `[remediation]`. The tag does NOT relax the reviewer-verdict whitelist.

## Tasks

### TASK-008: Sandbox-divergence escape hatch

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit — `cmd_implement` failure-path envelope construction + sandbox stdout/stderr capture)
  - `plugins/plan-executor/scripts/codex_implement_schema.json` (edit — add the five new optional failure-path fields)
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — auto-validate branch in the orchestrator dispatch handler; `commit-task --sandbox-divergence-tag` plumbing; run-summary subsection)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — document the escape hatch in the failure-routing section)
  - `tests/scripts/test_plan_codex_dispatch.py` (regression tests for envelope schema)
  - `tests/scripts/test_plan_ops.py` (regression tests for auto-validate branch)
- **Dependencies:** ["001"]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py tests/scripts/test_plan_ops.py -k "sandbox_divergence or independent_test_run"`
- **Read targets:**
  - `plugins/plan-executor/scripts/codex_implement_schema.json`
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py::cmd_implement`
  - `plugins/plan-executor/scripts/plan_ops.py::cmd_commit_task`
- **Acceptance criteria:**
  - Codex wrapper `implement` envelope (failure path with `cause: independent_test_run_failed`) carries `sandbox_test_stdout`, `sandbox_test_stderr`, `sandbox_test_command`, `sandbox_test_exit_code`, `sandbox_test_attempt_count`. Each stdout/stderr is capped at 32 KB with a `truncated_to` marker if exceeded.
  - `codex_implement_schema.json` declares the five new fields as optional and structurally valid for OpenAI strict structured-output (paired with `required` exactly matching `properties.keys()` per the existing invariant).
  - Orchestrator auto-validate branch: on the matching failure cause, re-runs the task's `Test command:` in the target env (cwd = repo root, env = inherited). Target-passes → run-log event gains a `sandbox_divergence` block AND the next `commit-task` invocation passes `--sandbox-divergence-tag`. Target-fails → existing failure path (Codex→Claude fallback OR task-fail) runs unchanged.
  - `commit-task --sandbox-divergence-tag` writes `[sandbox-divergence]` in the commit body in the canonical tag position. The tag does NOT change the reviewer-verdict whitelist.
  - End-of-run summary (`gates --certify` or the equivalent run-summary path) surfaces a "Sandbox divergences" list when any task hit the auto-validate branch.
  - SKILL.md gains a "Sandbox divergence escape hatch" subsection explaining the auto-validate branch + commit tag + the constraint that `--reviewer none` remains the final-resort override (not the default for this case).
  - Tests: wrapper envelope schema (success/failure/cap), orchestrator auto-validate target-passes path, target-fails path, no-divergence baseline.
  - All four wrapper schemas (incl. the edited `codex_implement_schema.json`) pass the structural-fixture test from `tests/scripts/test_plan_codex_dispatch_schema.py` (the test landed by CODEX_FRICTION_2026-04-25 TASK-001 — `set(node['required']) == set(node['properties'].keys())` at every `additionalProperties: false` node). The five new fields are declared `"type": ["string", "integer", "null"]` as appropriate AND listed in `required`.
- **Reversion guidance:** revert envelope schema additions, auto-validate branch, commit-tag flag, SKILL section. The `--reviewer none` final-resort path stays unchanged.

**Description:**
Surface the Codex wrapper's sandbox test stdout/stderr in the failure envelope and add an orchestrator auto-validate branch so target-env-passing test runs are no longer mistaken for real failures. Tag affected commits `[sandbox-divergence]`.

**Implementation notes:**
- The 32 KB cap is a conservative default; tune downward if envelope size proves problematic.
- The auto-validate target-env re-run MUST use the task's declared `Test command:` field — do NOT infer from the wrapper's recorded sandbox command (which may have an environment-specific prefix).
- Resist scope creep: do NOT change Codex→Claude fallback behavior in this task. If target-env also fails, the existing fallback fires.
- The `[sandbox-divergence]` tag is informational; downstream consumers (audit, certify) should treat it as a soft signal for human review, not a verdict modifier.
