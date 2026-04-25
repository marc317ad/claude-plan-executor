# TASK-005 — Remove the dead `parallel_batches` roster field from `_decompose_plan` + update fixtures + tests

## Goal

Delete the write-only `parallel_batches` field from `00_INDEX.json` rosters. Today `_decompose_plan` emits `parallel_batches` (a topo-only batch list, no file-disjoint partitioning) into both the roster manifest (`plan_ops.py:2425`) and the function's return value (`plan_ops.py:2457`), but no code anywhere in the plugin reads it back. It is documentation theater: confusingly different in shape from the schedule's `batches[]` (no file-disjoint partition, no `index`, no `file_locks`), and a footgun for operators reading the roster trying to predict batch boundaries. Removing it eliminates the conceptual ambiguity between roster `parallel_batches` and schedule `batches[]` and keeps the schedule wire format the single batch source-of-truth.

## Context

**Where `parallel_batches` is emitted.** `_decompose_plan` (`plan_ops.py:2169-2458`) writes the roster manifest at line 2425 with `"parallel_batches": batches` and returns the same value at line 2457. The `batches` value is the output of `_compute_decompose_batches(task_ids, deps_map)` — pure topo layers, no file-disjoint refinement. So the field carries the topo skeleton without any of the file-lock partitioning that the actual scheduler applies.

**Where it is read.** Nowhere. A repo-wide grep (`grep -rn "parallel_batches" plugins/ scripts/ tests/ docs/`) finds matches only in:
- `plan_ops.py` itself (the two emit sites)
- Existing 00_INDEX.json files in `docs/plans/*` (a write-side consequence)
- Two tests in `tests/scripts/test_plan_ops.py` that assert the field's value at line 16270 and its presence at line 16531 (both are tests of the emit, not of any reader)
- Two fixture comments in `tests/fixtures/decomposer_inputs/` that mention the field by name in their narrative

There is no reader in `scripts/`, `skills/`, or `agents/`. Nothing in `_build_tasks`, `_compute_schedule_batches`, `cmd_filter_schedule`, `cmd_batch_next`, or `_validate_schedule` consumes it. It is dead.

**Why the field exists at all.** It predates the schedule wire format. Earlier `_decompose_plan` was the canonical batch authority; the orchestrator read the roster and treated `parallel_batches` as the schedule. Once `build-tasks` + `compute-schedule` + `write-schedule` became the canonical flow (per the `per_task_dispatch_refactor_v2` plan), the roster's `parallel_batches` became advisory. Once the schedule's `batches[]` became the wire format with both topo and file-disjoint information, the roster field became redundant. It has been "kept for human-readability" without that being formally documented anywhere — every roster carries it but nothing actually consults it.

**Why remove rather than canonicalize.** Three options were considered (per Gemini cross-check + the prompt):
- **(a) Make canonical:** have `_build_tasks` honor it, OR cross-check that recomputed batches match it. This requires teaching `_build_tasks` to read the roster's `parallel_batches`, OR adding a validator that complains when they differ. Either way: more code, more drift surface, no actual operator benefit (humans don't edit `00_INDEX.json` to influence batching).
- **(b) Remove entirely:** delete the field from `_decompose_plan` emission and from existing roster JSONs. Smallest long-term surface; fixes the conceptual ambiguity at the cost of a one-time mass edit of in-flight rosters.
- **(c) Keep as documentation:** no code change. Future drift inevitable; the field's shape remains different from `batches[]`, perpetuating the confusion.

Option (b) is correct. The field has no consumer; preserving it for "human-readability" is theater that misleads readers (since it doesn't reflect the actual batch shape). Option (a) is over-engineering for a value nobody reads. Option (c) is do-nothing.

**Why this is a separate task from TASK-001/002/003.** It's a pure cleanup with no dep on the topo fix. Could land independently. Bundled here because it's adjacent — operators reading `00_INDEX.json` will less often confuse it with `batches[]` once the field is gone, and the decomposed-plan ergonomics improve. Sequencing: this task can run in parallel with TASK-001 (the helper extraction). It does NOT block any other task in the plan.

**Existing in-flight rosters.** Several `docs/plans/*/00_INDEX.json` files carry `parallel_batches` today (`POSTMORTEM_FIXES_2026-04-25`, `CODEX_FRICTION_2026-04-25`, `PLAN_NESTED_DISPATCH`, `PLAN_GEMINI_INTEGRATION_2026-04-25`, `PHASE_D_STATE_MACHINE`, `CLAUDE_ONLY_FIX_2026-04-24`, `prohibit_silent_revert/prohibit_silent_revert`, this plan's own roster after TASK-005 of THIS plan emits, and the archive subdirectory). The implementer should NOT mass-edit `archive/`. For the live (non-archived) rosters, the implementer either (i) re-runs `decompose-plan --force` against each whole-plan source markdown to regenerate the roster without `parallel_batches`, OR (ii) hand-deletes the field from each live roster JSON. Option (i) is preferable when a `*.md` whole-plan source exists; option (ii) for hand-crafted rosters where no whole-plan source survives.

**Fixture file `tests/fixtures/directory_mode_plan/00_INDEX.json`** also carries `parallel_batches`. Update it to drop the field. Test fixtures in `tests/fixtures/decomposer_inputs/cyclic_deps.md` and `unresolvable_deps.md` mention the field name in narrative comments — leave the markdown narrative alone; the comments are descriptive, not assertions.

**Tolerated-extra-keys consideration.** If the roster parser (`_parse_index_roster`) rejects unknown keys, then existing rosters in the wild would suddenly fail validation after the manifest schema changes. Verify before deleting: read `_parse_index_roster` and confirm it ignores extra keys. (Spoiler from inspection: it reads only the keys it needs and tolerates extras silently. Removing the field from emission therefore does not break parsing of older rosters that still carry it.)

## Verification

- `_decompose_plan` no longer emits `parallel_batches` in the roster manifest (`plan_ops.py:2416-2427`).
- `_decompose_plan`'s return value no longer carries `parallel_batches` (`plan_ops.py:2451-2458`).
- `cmd_decompose_plan` (the CLI envelope for the function) returns a JSON envelope without `parallel_batches`.
- All live (non-archived) `docs/plans/*/00_INDEX.json` rosters have `parallel_batches` removed. (Implementer chooses regenerate-via-`decompose-plan` or hand-edit, per task block guidance.)
- `tests/fixtures/directory_mode_plan/00_INDEX.json` no longer carries `parallel_batches`.
- `tests/scripts/test_plan_ops.py:16270` (assertion `res["parallel_batches"] == [["001"], ["002", "003"]]`) is removed.
- `tests/scripts/test_plan_ops.py:16531` (assertion `isinstance(manifest["parallel_batches"], list)`) is removed.
- A new test asserts that the emitted manifest does NOT contain `parallel_batches` (negative pin to prevent re-introduction).
- `_parse_index_roster` continues to parse old rosters that still carry `parallel_batches` (back-compat — it tolerates the extra key silently). A new test confirms this back-compat: parse a roster with `parallel_batches` as if it were a stale manifest, assert no error, assert the parsed structure is correct.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "decompose"` returns 0.
- The schema-valid + fixture-valid gate runs against this plan (after TASK-005 of THIS plan deletes the field from its OWN roster, which it will not yet have because TASK-005 hasn't run when the user authored this plan): assertion follows the in-flight rosters' shape — should still pass per `gates --check` because `parallel_batches` is not a required key in the roster validator.
- `archive/` rosters are NOT modified by this task.

## Tasks

### TASK-005: Remove `parallel_batches` from `_decompose_plan` (manifest + return), update live rosters + fixtures + tests, add back-compat parse test

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
  - `tests/fixtures/directory_mode_plan/00_INDEX.json`
  - `docs/plans/POSTMORTEM_FIXES_2026-04-25/00_INDEX.json`
  - `docs/plans/CODEX_FRICTION_2026-04-25/00_INDEX.json`
  - `docs/plans/PLAN_NESTED_DISPATCH/00_INDEX.json`
  - `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/00_INDEX.json`
  - `docs/plans/PHASE_D_STATE_MACHINE/00_INDEX.json`
  - `docs/plans/CLAUDE_ONLY_FIX_2026-04-24/00_INDEX.json`
  - `docs/plans/prohibit_silent_revert/prohibit_silent_revert/00_INDEX.json`
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "decompose"`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:2169-2458` — `_decompose_plan` (the emit sites)
  - `plugins/plan-executor/scripts/plan_ops.py::_parse_index_roster` — confirm tolerates-extras
  - `tests/scripts/test_plan_ops.py:16260-16310` — the assertion at 16270 + neighborhood
  - `tests/scripts/test_plan_ops.py:16520-16560` — the assertion at 16531 + neighborhood
  - `tests/fixtures/directory_mode_plan/00_INDEX.json` — fixture to update
- **Symbol targets:**
  - `plugins/plan-executor/scripts/plan_ops.py::_decompose_plan`
  - `plugins/plan-executor/scripts/plan_ops.py::cmd_decompose_plan`
  - `plugins/plan-executor/scripts/plan_ops.py::_parse_index_roster`
- **Acceptance criteria:**
  - `_decompose_plan` no longer carries the `"parallel_batches": batches` line in either the manifest dict (line ~2425) or the return dict (line ~2457).
  - The variable `batches` inside `_decompose_plan` is also removed if it has no other consumer after the emit deletion. Verify by reading the function: cycle-detection at lines 2302-2316 still requires the variable for `batch_errors`, so the variable stays — it's just no longer emitted. Don't get clever and chop more than necessary.
  - All eight 00_INDEX.json files listed under §Files lose their `parallel_batches` key. The remaining JSON shape stays valid (no trailing comma, no key ordering disruption).
  - Existing test assertions at `test_plan_ops.py:16270` and `:16531` are removed (not commented out — actually deleted).
  - A new test (recommended location: in the same `TestDecomposePlan` class containing the existing tests at lines ~16260+) asserts `"parallel_batches" not in manifest` after a fresh `decompose-plan` run. This is the negative pin against re-introduction.
  - A new test asserts back-compat: read a synthetic roster JSON that still carries `parallel_batches`, run it through `_parse_index_roster`, assert no error and assert the parsed roster's task ids match expectations. (This is the cheap belt-and-braces fence — old rosters in the wild remain parseable.)
  - `archive/` rosters are NOT modified. The implementer must explicitly avoid touching `docs/plans/archive/**/00_INDEX.json` files.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "decompose"` returns 0; full file (`venv/bin/pytest -q tests/scripts/test_plan_ops.py`) returns 0.
- **Reversion guidance:** restore `"parallel_batches": batches` to both emit sites in `_decompose_plan`; restore the field in each modified 00_INDEX.json; restore the deleted test assertions; delete the new tests. (Reverting requires re-deriving the topo `batches` value for each manually-edited roster — re-run `decompose-plan --force` against the whole-plan source to regenerate, or accept that hand-edited rosters lose the field permanently.)

**Description:**
Delete the dead `parallel_batches` emission from `_decompose_plan` and strip the field from all live (non-archive) roster `00_INDEX.json` files plus the `directory_mode_plan` fixture. Remove the two existing test assertions that pin the field's value/shape. Add a negative pin (`"parallel_batches" not in manifest` after a fresh decompose) and a back-compat parse test (old rosters in the wild that still carry the field must continue to parse without error). Archive rosters are left untouched — they're historical records and the field's removal there would be needless churn. After this task, the schedule's `batches[]` (emitted by `compute-schedule` / `build-tasks` and persisted by `write-schedule`) is the single batch source-of-truth, with no shadow advisory roster field to confuse operators.

**Implementation notes:**
- Verify `_parse_index_roster` tolerates extra keys before deleting any existing roster's `parallel_batches`. If it does NOT (it should, but verify), this task expands to also relax the parser's strictness — that should be a quick `grep` and is the implementer's first action under "Pre-flight" in their report.
- For the live roster JSON edits, prefer using `python -c 'import json; ...'` to load + drop the key + dump with `json.dumps(d, indent=2, sort_keys=False) + "\n"` to preserve the canonical formatting — manually-rewritten JSON tends to drift on indent levels.
- The `_batching_rationale` key in `POSTMORTEM_FIXES_2026-04-25/00_INDEX.json` is unrelated and stays. Do not delete it.
- The plan ALSO emits `_batching_rationale` in `POSTMORTEM_FIXES_2026-04-25` — that's an operator-facing note pinned by the plan's author. Don't disturb it.
- The new back-compat parse test should assert that `_parse_index_roster` returns the expected `roster` dict shape (mapping of task_id → entry) when the input JSON carries `parallel_batches`. Pick a small test case (2-3 chunks) so the test stays readable.
- Skip the archive/ directory entirely — `archive/per_task_dispatch_refactor_v2/00_INDEX.json`, `archive/plan_review_triage_2026-04-23/00_INDEX.json`, `archive/directory_mode_hotfix_2026-04-24/00_INDEX.json`, `archive/implement_plan_directory_mode/00_INDEX.json` all retain their original (with-`parallel_batches`) shape.
- This plan's OWN roster (`docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25/00_INDEX.json`) must also not carry `parallel_batches` — but the plan author handles that at plan-creation time (this is the plan you're reading); the implementer has nothing to do at this task block for that file.

**Reversion guidance:**
Restore the two `"parallel_batches": batches` lines in `_decompose_plan`; restore the deleted assertions in `test_plan_ops.py`; delete the two new tests; restore the `parallel_batches` key in each modified 00_INDEX.json (re-derive the value via `decompose-plan --force` against the matching whole-plan source markdown, or accept that hand-edited rosters lose it permanently — the field has no consumer, so the loss is purely cosmetic).
