# TASK-002 — Create the missing `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` to unblock the canary test

## Goal

Create the missing `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` to unblock the canary test

## Context

Auto-decomposed child for TASK-002. See the source plan for broader context.

## Verification

- The file `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` exists.
- The file contains every string in `tests/scripts/test_claude_dispatch_canary.py:V3_REQUIRED_TOP_LEVEL_KEYS` (the canonical v3 wrapper envelope key set: `schema_version`, `status`, `status_reason`, `agent`, `model`, `session_id`, `duration_ms`, `cost_usd`, `tokens`, `result`, `result_raw_truncated`, `stderr_tail`, `permission_denials`, `scope`, and any other entries in the constant at test-time — read the source of truth, do not transcribe from memory). Each key appears at least once as plain text in the markdown body.
- The doc is a short reference (≤2 pages) tying each v3 envelope key to its wrapper-side emission site in `plan_claude_dispatch.py`. Format: a markdown table or definition list with columns/labels "Key", "Type", "Emission site", "Notes". The notes column documents protocol invariants (e.g., `status` is the universal commit-forbidding sentinel when not `ok`; `cost_usd` may be `null` on dry-run; `result` carries the inlined-schema-validated agent output).
- The doc's title line is `# v3 Claude wrapper envelope — key reference` (matches the SKILL bash-dispatch migration's vocabulary).
- The canary test (named in the test command) passes.

## Tasks

### TASK-002: Create the missing `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` to unblock the canary test

- **Status:** Pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `docs/plans/SKILL_bash_dispatch_migration/probe_results.md`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_canary.py::test_canary_probe_results_md_present_and_records_v3_keys`
- **Acceptance criteria:**
  - The file `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` exists.
  - The file contains every string in `tests/scripts/test_claude_dispatch_canary.py:V3_REQUIRED_TOP_LEVEL_KEYS` (the canonical v3 wrapper envelope key set: `schema_version`, `status`, `status_reason`, `agent`, `model`, `session_id`, `duration_ms`, `cost_usd`, `tokens`, `result`, `result_raw_truncated`, `stderr_tail`, `permission_denials`, `scope`, and any other entries in the constant at test-time — read the source of truth, do not transcribe from memory). Each key appears at least once as plain text in the markdown body.
  - The doc is a short reference (≤2 pages) tying each v3 envelope key to its wrapper-side emission site in `plan_claude_dispatch.py`. Format: a markdown table or definition list with columns/labels "Key", "Type", "Emission site", "Notes". The notes column documents protocol invariants (e.g., `status` is the universal commit-forbidding sentinel when not `ok`; `cost_usd` may be `null` on dry-run; `result` carries the inlined-schema-validated agent output).
  - The doc's title line is `# v3 Claude wrapper envelope — key reference` (matches the SKILL bash-dispatch migration's vocabulary).
  - The canary test (named in the test command) passes.
- **Reversion guidance:** Delete the file. The canary test goes back to failing; the parent issue resurfaces for a future plan.

**Description:**
Create the missing `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` to unblock the canary test. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-002. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
