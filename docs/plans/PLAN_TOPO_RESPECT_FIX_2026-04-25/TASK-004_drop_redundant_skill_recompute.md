# TASK-004 — Drop the redundant `compute-schedule --stdin` calls from `SKILL.md` Phase 1 step 3

## Goal

After TASK-002 and TASK-003 land, the `compute-schedule --stdin` recompute in `SKILL.md` Phase 1 step 3 is a provable no-op (TASK-003's idempotence test is the machine-checked contract). This task deletes the no-op pipe from BOTH the default branch (line ~317-328) and the `--task-ids` filter branch (line ~341-343), updates the surrounding prose so the orchestrator instructions reflect what actually happens, refreshes `plan_ops_cheatsheet.md`'s `compute-schedule` entry, and adds a fence test that fails if the deletion ever regresses. Clarification: `compute-schedule` itself is NOT deprecated — it remains a useful standalone CLI for direct callers (e.g. an operator hand-validating a `tasks[]` JSON), so the cheatsheet entry stays. What's removed is the orchestrator's automatic post-`build-tasks` re-pipe.

## Context

**Where the recompute lives.** `plugins/plan-executor/skills/implement-plan/SKILL.md` Phase 1 step 3 ("Recompute file-disjoint batches") at lines 317-343. Two distinct invocations:

1. **Default branch (lines 319-328).** After `build-tasks` runs in Phase 1 step 1 and the per-child classifier fan-out optionally runs in step 2, the orchestrator pipes the merged in-memory schedule's `tasks[]` through `compute-schedule --stdin --json` and replaces `batches[]` with the returned value. The justification text says: "so the classifier-populated `agent` assignments are reflected in batch boundaries." This justification is wrong — `agent` is not an input to either batcher. The recompute exists for no useful reason.

2. **`--task-ids` filter branch (lines 334-343).** After `filter-schedule --stdin --task-ids <csv>` trims the in-memory schedule to a requested subset, the orchestrator re-pipes through `compute-schedule --stdin` to "recompute file-disjoint batches." But `cmd_filter_schedule` already preserves the source schedule's batches (drops empty batches, trims `task_ids` within surviving batches). The recompute clobbers the topo ordering with the file-disjoint-only output of the broken `_compute_schedule_batches`.

**Why the recompute is provably a no-op after TASK-002 and TASK-003.** TASK-001's helper is the single source of truth. TASK-003's idempotence test pins `build-tasks` and `compute-schedule` to byte-equal `batches[]`. Therefore the default-branch recompute reads `tasks[]`, runs the same logic, returns the same `batches[]`. Same for the filter-branch recompute, modulo the different input shape — `cmd_filter_schedule`'s output already carries topo-correct batches (it never re-batches; it preserves the input's `batches[]` with task_ids trimmed). Re-piping through `compute-schedule` reads the same `tasks[]` and produces the same canonical batching. No-op confirmed.

**A latent bug fixed for free.** `cmd_filter_schedule`'s output preserves the source's `file_locks[]` verbatim, even when some tasks are filtered out — meaning a filtered batch can carry stale file_locks (paths from removed tasks). The current SKILL `compute-schedule` re-pipe accidentally cleaned this up (because `_compute_schedule_batches` regenerates `file_locks` from scratch). After this task, the cleanup no longer happens and the stale-file_locks bug is exposed. **Mitigation: this task deletes the recompute regardless; cleaning up `cmd_filter_schedule`'s stale file_locks is tracked as a follow-up note in §Verification, NOT as part of this plan.** Operator visibility: `_validate_schedule` does not reject stale file_locks — they are tolerated by the validator — so the bug is ergonomic, not blocking.

**Why a fence test.** The recompute was added to SKILL.md by an earlier plan (`docs/plans/archive/Fix_Depenency_Gate_Plans/TASK-004_skill_add_preflight_gate.md`) for ostensibly correct reasons. Without an automated check, any future skill rewrite could re-introduce the pipe — and the plan-author edit path (Phase 1.5) is exactly the kind of place where an LLM could "helpfully" add the recompute back. A text-match test in `tests/scripts/test_skill_md_invariants.py` (or extension of an existing skill-invariant test file) reads `SKILL.md` and fails if `compute-schedule --stdin` appears in the Phase 1 step 3 region.

**Cross-references that also need to be fixed.** `dispatch-templates.md` mentions `compute-schedule` as part of the prose describing Phase 1 re-runs (lines 89, 197). That prose is descriptive rather than a literal pipe instruction — it should be edited for accuracy after this task lands (the Phase 1 re-run no longer requires `compute-schedule`), but the change is small and surface-level; this task includes those edits to keep the orchestrator narrative consistent.

**`plan_ops_cheatsheet.md` is unchanged in spirit.** The cheatsheet's `compute-schedule` entry (lines 30-33) describes the standalone CLI, which still exists for direct callers. This task tweaks the description to clarify "use directly to recompute batches from a `tasks[]` JSON; not invoked by `/implement-plan`'s Phase 1 anymore" — keeping the entry but pinning the new contract.

## Verification

- `plugins/plan-executor/skills/implement-plan/SKILL.md` Phase 1 step 3 contains NO `compute-schedule --stdin` invocation (neither in the default branch nor in the `--task-ids` filter branch).
- The Phase 1 step 3 prose is rewritten so the section header and the body describe the actual flow: filter rewrites are applied in-memory, agent overrides applied in-memory, then the schedule flows directly to step 4 `write-schedule`. The "Shape-shift between `build-tasks` and `compute-schedule` / `write-schedule`" explainer (lines 326-328 in current SKILL.md) is preserved BUT trimmed to remove the `compute-schedule` reference — the shape-shift is now `build-tasks` → `write-schedule` directly, with the warnings→gaps mapping unchanged.
- `dispatch-templates.md` lines 89 and 197 are edited to drop the `compute-schedule` step from the Phase 1 re-run sequence prose — the new sequence reads `build-tasks → classifier → write-schedule + schedule-valid gate`. (Each occurrence of `build-tasks → classifier → compute-schedule → write-schedule` becomes `build-tasks → classifier → write-schedule`. There are several such occurrences in SKILL.md too — every one is updated.)
- `plan_ops_cheatsheet.md`'s `compute-schedule` entry stays in place but the description gains a one-line note: "Standalone helper for direct callers; not part of `/implement-plan` Phase 1 anymore."
- A new fence test `tests/scripts/test_skill_md_invariants.py::test_skill_md_no_compute_schedule_pipe_in_phase1_step3` reads `SKILL.md` and asserts that no line in the Phase 1 step 3 region (between the `### Step 3 ` header and the next `###` header) contains the substring `compute-schedule --stdin`. The test must scope its search to step 3 specifically, not to the whole file (so unrelated mentions of `compute-schedule` elsewhere — e.g. in §Phase 1.5 prose or the CLI table — do not trip it).
- A complementary fence test asserts that no line in the same region contains the literal `compute-schedule` invocation pattern (defensive: future drift could re-introduce via a different flag combination).
- The `plan_ops_cheatsheet.md` `compute-schedule` block is checked for the new descriptor line by another lightweight assertion in the same test file.
- `venv/bin/pytest -q tests/scripts/test_skill_md_invariants.py` returns 0.
- Manual inspection: a fresh `/implement-plan` dry run on the existing `directory_mode_plan` fixture produces the same final schedule shape as before (the recompute really was a no-op). This is exercised by the idempotence test in TASK-003.
- **Out-of-scope follow-up note (recorded under §Context but NOT addressed in this plan):** `cmd_filter_schedule` preserves stale `file_locks` after task_id trimming. Tracked as a separate ergonomic bug; address in a follow-up plan.

## Tasks

### TASK-004: Delete `compute-schedule --stdin` from SKILL.md Phase 1 step 3 (default + filter branch) + add a regression fence test + update related prose in `dispatch-templates.md` and `plan_ops_cheatsheet.md`

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md`
  - `tests/scripts/test_skill_md_invariants.py`
- **Dependencies:** [002, 003]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_skill_md_invariants.py`
- **Read targets:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:317-343` — Phase 1 step 3 (the recompute call sites)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:80` — CLI table entry for `compute-schedule`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:264-291` — Phase 1 protocol overview prose (mentions `compute-schedule`)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:391-398` — Phase 1-triage routing table (mentions `compute-schedule` in the re-run prose)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:529` — Phase 1.5.5 re-run prose
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:885` — Re-source-verdict rule prose
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md:89` — needs-replan re-run prose
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md:197` — author re-run prose
  - `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md:30-33` — `compute-schedule` cheatsheet entry
- **Symbol targets:**
  - n/a (markdown changes only; no Python symbol edits)
- **Acceptance criteria:**
  - `SKILL.md` Phase 1 step 3 contains no `compute-schedule --stdin` invocation. The two code blocks at lines ~321-324 and ~343 are deleted; the prose around them is rewritten to describe `build-tasks` → filter (in-memory) → step 4 directly. The "Shape-shift" paragraph is preserved with `compute-schedule` references removed.
  - The Phase 1 protocol overview at SKILL.md:264-291 is updated where it lists "build-tasks + compute-schedule + write-schedule" / "build-tasks + classifier + compute-schedule + write-schedule" — those phrases become "build-tasks + write-schedule" / "build-tasks + classifier + write-schedule" respectively.
  - Phase 1-triage routing table (SKILL.md:391-398), Phase 1.5.5 re-run prose (SKILL.md:529), re-source-verdict rule (SKILL.md:885), and `dispatch-templates.md:89,197` re-run prose are updated to drop `compute-schedule` from the `build-tasks → classifier → compute-schedule → write-schedule` sequence (which becomes `build-tasks → classifier → write-schedule`).
  - The CLI table entry for `compute-schedule` at SKILL.md:80 is updated to: "Recompute file-disjoint + topo-respecting batches from `tasks[]`; standalone helper, not invoked by `/implement-plan` Phase 1." (Or similarly accurate phrasing.)
  - `plan_ops_cheatsheet.md:30-33` gains a one-line note that `compute-schedule` is a standalone helper, not part of `/implement-plan` Phase 1.
  - A new test file `tests/scripts/test_skill_md_invariants.py` is created with at least three tests:
    1. `test_skill_md_no_compute_schedule_pipe_in_phase1_step3` — reads SKILL.md, slices the region between the `### Step 3 ` header and the next `###` header, asserts no occurrence of `compute-schedule --stdin` (or `compute-schedule\s*\\` to catch line-continuation variants).
    2. `test_skill_md_phase1_overview_does_not_chain_compute_schedule` — asserts that no line in SKILL.md describes the Phase 1 sequence as `build-tasks → ... → compute-schedule → ... → write-schedule` (regex match on the chain).
    3. `test_dispatch_templates_no_compute_schedule_in_phase1_rerun` — asserts the same for `dispatch-templates.md`.
  - The new test file imports `pathlib.Path` and reads SKILL.md via `Path(__file__).resolve().parents[2] / "plugins/plan-executor/skills/implement-plan/SKILL.md"` (or the convention used by neighbor tests in the file — match the dominant pattern in the directory).
  - `venv/bin/pytest -q tests/scripts/test_skill_md_invariants.py` returns 0.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` (full file) returns 0 — no skill-text-related test breaks.
- **Reversion guidance:** restore the deleted `compute-schedule --stdin` blocks in SKILL.md Phase 1 step 3; revert dispatch-templates and cheatsheet prose to pre-task state; delete `tests/scripts/test_skill_md_invariants.py`. The behavior reverts to the pre-task no-op-recompute state.

**Description:**
Remove the `compute-schedule --stdin` recompute pipe from SKILL.md Phase 1 step 3 (both default + `--task-ids` filter branches), update every other SKILL.md and `dispatch-templates.md` mention of the `build-tasks → ... → compute-schedule → ... → write-schedule` chain to reflect the new (no-recompute) sequence, refresh the cheatsheet entry to clarify `compute-schedule` is now a standalone-only helper, and add a fence test file (`tests/scripts/test_skill_md_invariants.py`) that fails if a future edit re-introduces the recompute pipe in Phase 1 step 3. The fence test is cheap insurance: this exact recompute regressed before in the `Fix_Depenency_Gate_Plans` plan, and the plan-author auto-revise path is structurally invitation-prone for re-introduction. Note: `compute-schedule` itself stays as a CLI subcommand for direct callers — TASK-002 leaves the function dep-aware regardless of caller, so it's safe.

**Implementation notes:**
- The "Shape-shift" paragraph at SKILL.md:326-328 is dense and load-bearing — it explains the `tasks/batches/gaps/risks/outcome` mapping from `build-tasks` output to canonical schedule shape. Don't gut it. The narrow surgery: every sentence that mentions `compute-schedule` is rewritten so `build-tasks` flows directly to `write-schedule`. The `warnings → gaps` mapping is unchanged.
- The Phase 1-triage routing table and Phase 1.5.5 re-run prose mention `compute-schedule` in the sequence list. These are not load-bearing instructions — just prose accuracy. Replace the chain string `build-tasks → classifier → compute-schedule → write-schedule` with `build-tasks → classifier → write-schedule` everywhere it appears.
- The fence test should slice SKILL.md by `^### ` headers to find Phase 1 step 3's region — don't grep the whole file (the CLI table at line 80 legitimately mentions `compute-schedule`, and the cheatsheet header lives in its own file but is referenced by the test). Use `re.split(r"^###\s", text, flags=re.MULTILINE)` and locate the chunk starting with "Step 3 ".
- Keep the deletion surgical. Don't rewrite the prose in §Phase 1 step 3 beyond what's needed to remove the recompute pipe. (TASK-005 and TASK-006 are separate.)
- The dispatch-templates updates are two single-line edits — don't expand them into a broader rewrite.
- After the deletion, the section heading "### Step 3 — Recompute file-disjoint batches" is misleading. Rename to "### Step 3 — Apply filters and per-task overrides" (or similar — the implementer chooses, but the new heading must accurately reflect the post-deletion content).

**Reversion guidance:**
Restore the two deleted code blocks (`compute-schedule --stdin` invocations) at their original SKILL.md positions; restore the original prose that surrounded them; revert dispatch-templates and cheatsheet edits; delete the fence test file. The behavior reverts to "recompute pipe runs as a no-op" — which is harmless under TASK-002 and TASK-003 (the recompute is provably idempotent), so reversion does NOT regress topo correctness.
