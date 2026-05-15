# TASK-003 — Add a smoke test pinning the JSON-schema template_id enum to the Python-side renderer maps

## Goal

Add a smoke test pinning the JSON-schema template_id enum to the Python-side renderer maps

## Context

Auto-decomposed child for TASK-003. See the source plan for broader context.

## Verification

- Add a new test `test_template_id_enum_matches_renderer_registry` to `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`.
- The test loads `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` and extracts the `template_id` field's enum (the JSON path is `properties.template_id.enum` — verify the exact path by reading the schema file before writing the test).
- The test imports `_AGENT_DISPATCH_TEMPLATE_HEADING`, `_AGENT_DISPATCH_TEMPLATE_MODEL`, and `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` from `plugins.plan_executor.scripts.plan_ops` (adjust import path if `plan_ops.py` is imported a different way in this test file — read existing imports first).
- The test asserts that the JSON enum, the keys of `_AGENT_DISPATCH_TEMPLATE_HEADING`, the keys of `_AGENT_DISPATCH_TEMPLATE_MODEL`, and the keys of `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` are all the same set (use `set(...) == set(...)` four-way equality with a clear assertion message that names the diff on failure).
- The test passes against `main` as of TASK-001 merge (the rework-context extension does not add new template_ids, so the invariant holds).
- Per-template renderer branches: optionally extend the test to assert that every template_id in the JSON enum has a corresponding `if template_id == "...":` branch in `plan_ops.py`'s render function. If detection is fragile (regex over Python source), the four-way set equality is sufficient — the per-branch assertion is a stretch goal.

## Tasks

### TASK-003: Add a smoke test pinning the JSON-schema template_id enum to the Python-side renderer maps

- **Status:** Pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py -k "template_id_registry_invariant"`
- **Acceptance criteria:**
  - Add a new test `test_template_id_enum_matches_renderer_registry` to `tests/scripts/test_plan_ops_build_agent_dispatch_prompt.py`.
  - The test loads `plugins/plan-executor/scripts/schemas/mcp/build_agent_dispatch_prompt.input.json` and extracts the `template_id` field's enum (the JSON path is `properties.template_id.enum` — verify the exact path by reading the schema file before writing the test).
  - The test imports `_AGENT_DISPATCH_TEMPLATE_HEADING`, `_AGENT_DISPATCH_TEMPLATE_MODEL`, and `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` from `plugins.plan_executor.scripts.plan_ops` (adjust import path if `plan_ops.py` is imported a different way in this test file — read existing imports first).
  - The test asserts that the JSON enum, the keys of `_AGENT_DISPATCH_TEMPLATE_HEADING`, the keys of `_AGENT_DISPATCH_TEMPLATE_MODEL`, and the keys of `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` are all the same set (use `set(...) == set(...)` four-way equality with a clear assertion message that names the diff on failure).
  - The test passes against `main` as of TASK-001 merge (the rework-context extension does not add new template_ids, so the invariant holds).
  - Per-template renderer branches: optionally extend the test to assert that every template_id in the JSON enum has a corresponding `if template_id == "...":` branch in `plan_ops.py`'s render function. If detection is fragile (regex over Python source), the four-way set equality is sufficient — the per-branch assertion is a stretch goal.
- **Reversion guidance:** Delete the new test function. The defensive guard goes away; runtime failures resurface (loud but late).

**Description:**
Add a smoke test pinning the JSON-schema template_id enum to the Python-side renderer maps. (Auto-filled by decompose-plan; the source plan omitted a `**Description:**` body for TASK-003. See the parent plan's `## Context` and `## Verification` sections for the full intent.)
