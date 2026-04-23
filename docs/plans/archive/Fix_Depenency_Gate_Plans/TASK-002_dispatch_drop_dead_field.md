# TASK-002 — Drop dead `dependencies` field from `plan_codex_dispatch.py`

**Base branch:** `main`
**Source plan:** `~/.claude/plans/polymorphic-foraging-scone.md` (section B)

---

## Goal

Remove the one-line dead write of `dependencies` inside `parse_task_block` in `plugins/plan-executor/scripts/plan_codex_dispatch.py`. The field is written to the task dict but no downstream consumer reads it.

## Verification

1. `grep -n '"Dependencies"' plugins/plan-executor/scripts/plan_codex_dispatch.py` — zero matches (case-sensitive; the only consumer was `parse_task_block`).
2. `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_codex_dispatch_integration.py` — both pass.

---

## Tasks

### TASK-002: Delete `dependencies` key from `parse_task_block` and its lone test assertion

- **Status:** done
  > Landed 2026-04-17 in commit `68954dd`. `plan_codex_dispatch.parse_task_block` no longer returns a `dependencies` key — verified by inspection. Worker-layer correctness fix; remains in effect under the two-layer model documented in `docs/analysis/TASK_DEPENDENCY_DAG_Architecture_Inconsistency.md`.
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py`
  - `tests/scripts/test_plan_codex_dispatch_parsing.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_codex_dispatch_integration.py`
- **Acceptance criteria:**
  - In `plan_codex_dispatch.py`, line 203 (`"dependencies": _extract_inline_field(block, "Dependencies"),`) is deleted. No other line in `parse_task_block` is modified; the returned dict loses exactly one key.
  - In `test_plan_codex_dispatch_parsing.py`, line 127 (`assert block["dependencies"] == "[004]"`) is deleted. No other assertion in `test_wrapper_parse_task_block_suffixed` is modified.
  - The `PLAN_WITH_SUFFIXED_TASKS` fixture (lines 29–65) is left intact — the `- **Dependencies:** [004]` / `[004A]` lines stay, exercising silent ignore of unknown inline fields for legacy plan compatibility.
  - No changes to `tests/scripts/test_plan_ops.py`, `test_plan_codex_dispatch_integration.py`, `test_plan_codex_dispatch_schema.py`, or `test_plan_codex_dispatch_state_isolation.py`.
  - Both pytest files in the Test command pass.

**Description:**
Remove one dead production line and its one matching test assertion. `parse_task_block` writes a `dependencies` key into the returned dict that no downstream consumer reads; the only observer is an assertion in the parsing test suite. Cleaning both in the same commit prevents a KeyError regression and keeps the deletion atomic. The inline `- **Dependencies:**` lines in the fixture stay so the parser continues to be tested against real-world plans that still carry the header during migration.

**Reversion guidance:**
Re-add the single deleted line in `plan_codex_dispatch.py` (line 203) and the single deleted assertion in `test_plan_codex_dispatch_parsing.py` (line 127). No other code path depends on the absence of either.
