# PLAN - Persist `**Agent:**` to child plan files after classifier fan-out

**Status:** Pending
**Created:** 2026-05-11
**Base branch:** main

## Goal

After the Phase 1 Step 2 per-child classifier fan-out resolves each task's `agent` (`claude | codex`), write a `**Agent:** <value>` bullet back into each child plan file's metadata block. The classifier becomes a one-shot per task: subsequent runs (dry-run or real) read `**Agent:**` from the child file in `build-tasks`, find no missing-agent children, and Phase 1 Step 2 short-circuits to the existing "Skip-classifier case" path documented at `plugins/plan-executor/skills/implement-plan/SKILL.md:290`. Saves N Claude-wrapper dispatches per re-run (one per task that lacked `**Agent:**`).

The specific symptom this prevents:

> When we run `--dry-run`, the agent doesn't persist to the index. This requires the actual run to classify all over again.

## Findings

### 1. Classifier output flows to schedule, never to child plan files

Phase 1 today (SKILL.md §266-321):

1. `build-tasks` reads each child file; emits `tasks[i].agent` iff `**Agent:**` declared, else omits the field (`plan_ops.py:4039-4045`).
2. Step 2 dispatches one `plan-analyst` per missing-agent child in parallel; each returns `{agent, classification_reason}`.
3. Orchestrator merges classifier output into `tasks[]` in memory (SKILL.md §321).
4. Step 4 `write-schedule` persists `tasks[*].agent` to `.schedule.json`.

The child markdown file is **never updated**. The `**Agent:**` bullet that `_emit_child` knows how to write (`plan_ops.py:3193-3195`) is only ever populated at decompose time, and only when the *source* plan declared `**Agent:**` per task — which is rare.

### 2. Short-circuit already exists; it just never fires on first re-run

`SKILL.md:290`:

> **Skip-classifier case.** If `missing_agent_children` is empty (every child already declares `**Agent:**`), the classifier step is skipped entirely. No Agent dispatches are issued in Phase 1, no network roundtrip, no model latency — Phase 1 collapses to `build-tasks + write-schedule`. Proceed directly to step 3.

This branch is unreachable on a typical re-run because no upstream step writes `**Agent:**` after classification. Closing the loop turns every run after the first into a zero-Agent Phase 1.

### 3. Surgical-mutation prototype exists

`mutate_task_status` (`plan_ops.py:4173-4201`) is the canonical pattern for surgical task-bullet mutation: locate the task block, find the bullet, validate the new value, splice. Build `mutate_task_agent` exactly like it — same signature, same error semantics, same enum gate (`ALLOWED_AGENTS = {"claude", "codex"}`). Bullet ordering already canonicalized by `_emit_child` (`plan_ops.py:3187-3197`): `**Status:** → **Priority:** → **Agent:** → **Files:**`. The mutator must insert the bullet at that exact slot when absent (idempotent replace when present).

### 4. Plan-file mutation policy needs one entry

CLAUDE.md:50:

> Allowed plan-file mutations: `**Status:**` flips, append-only execution-log tail, pre-dispatch format-only corrections needed to satisfy schema gates, and surgical hand-fixes per Forward bias above.

`**Agent:**` persistence is a new sibling of `**Status:**` flips — surgical, single-bullet, enum-valued, idempotent. The mutation policy enumeration extends by one entry. This is the only authority-document change required.

### 5. Dry-run already exempts schedule writes; this is a sibling exemption

SKILL.md's Dry-run mode section (recently amended) lists two exemptions: Phase 0 auto-promote commit and the Phase 1.5 plan-revision cycle. `**Agent:**` persistence becomes a third — same shape (mutation on disk that future runs depend on), same justification (dry-run becomes more useful, not less).

## Decisions

1. **Pure-core helper `mutate_task_agent`.** Modeled byte-for-byte on `mutate_task_status`. Enum gate `{"claude", "codex"}`. Idempotent replace when bullet present; insert at canonical slot (between `**Priority:**` and `**Files:**`) when absent. Same `ValueError` semantics on drift.
2. **MCP tool `plan_ops__set_task_agent`.** Mirrors `plan_ops__update_plan_header`'s registration shape. Inputs: `plan_file`, `task_id`, `agent`. Outputs: standard envelope + `prior_agent` field for traceability. The orchestrator calls this once per classified child between Step 2 (classifier returns) and Step 4 (`write-schedule`).
3. **Orchestrator wiring lives in SKILL.md, not in `plan_ops.py`.** Phase 1 Step 2's fan-out post-processing gains one line: for each child whose classifier returned an `agent`, dispatch `plan_ops__set_task_agent`. The schedule synthesis (Step 3) and write-schedule (Step 4) remain unchanged.
4. **Dry-run is exempt.** Add a third entry to the Dry-run mode exemption enumeration. Without this, dry-run leaves the plan amnesic — defeating the whole purpose.
5. **No retro-fill.** This plan does not edit existing decomposed plans on disk to add `**Agent:**` bullets. The next run of any such plan does one classifier fan-out, persists, and is done — the migration is implicit, not eager.
6. **`build-tasks` is already correct.** It already reads `**Agent:**` when present and short-circuits Step 2 when all children declare one. No change to `build-tasks` semantics or output shape.

## Scope

In scope:

- `plugins/plan-executor/scripts/plan_ops.py` (new `mutate_task_agent` helper, new `cmd_set_task_agent` / `_run_set_task_agent` / `_args_to_payload_set_task_agent`, argparse registration)
- `plugins/plan-executor/scripts/plan_ops_mcp_server.py` (new tool registration block, mirrored on `plan_ops__update_plan_header`)
- `plugins/plan-executor/skills/implement-plan/SKILL.md` (Phase 1 Step 2 post-processing instruction; Dry-run mode exemption; command idioms table entry)
- `CLAUDE.md` (Allowed plan-file mutations enumeration adds `**Agent:**` flips)
- `tests/scripts/test_plan_ops.py` (unit tests for `mutate_task_agent` + integration test for the MCP tool)

Out of scope:

- Retroactive editing of existing decomposed plans on disk.
- Caching `agent` in `00_INDEX.json` (the child-file bullet is the canonical surface; index stays a roster, not a cache).
- Changing `build-tasks` short-circuit logic (already correct).
- Changing the classifier prompt or `plan-analyst` agent.
- Persisting `classification_reason` to the plan file (today it lives only in the run log; promoting it to disk is a separate question — keep this plan narrow).
- A new `--no-persist-agent` flag (no operational need yet; defer unless a concrete use case appears).
- Reconcile / schedule files-prose work (separate plan: `PLAN_RECONCILE_BATCH_AWARE_2026-05-11.md`).

## Tasks

### TASK-001: Add `mutate_task_agent` pure helper + `plan_ops__set_task_agent` MCP tool

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/scripts/plan_ops_mcp_server.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** none
- **Acceptance criteria:**
  - In `plan_ops.py`, define `ALLOWED_AGENTS = ("claude", "codex")` near the existing `ALLOWED_TASK_STATUSES` constant.
  - In `plan_ops.py`, add `mutate_task_agent(plan_text: str, task_id: str, new_agent: str) -> tuple[str, str]` modeled byte-for-byte on `mutate_task_status` (`plan_ops.py:4173`). Returns `(updated_plan_text, prior_agent)` — `prior_agent` is empty string when no `**Agent:**` bullet existed.
  - Idempotent replace when `**Agent:**` is present; insert at the canonical slot (between `**Priority:**` and `**Files:**`, matching `_emit_child`'s order at `plan_ops.py:3187-3197`) when absent. If `**Priority:**` is absent, insert before `**Files:**`; if `**Files:**` is also absent, insert at the end of the metadata-bullet run (before the first non-bullet line).
  - Raises `ValueError` on: unknown `new_agent`, task block not found, malformed task block (no metadata bullets at all).
  - Add `cmd_set_task_agent`, `_run_set_task_agent`, `_args_to_payload_set_task_agent` mirroring the `update-plan-header` trio (`plan_ops.py:7997-8024`). Subcommand name: `set-task-agent`. Required args: `--plan-file`, `--task-id`, `--agent`.
  - Register `plan_ops__set_task_agent` in `plan_ops_mcp_server.py` mirroring the `plan_ops__update_plan_header` block at line 1958. Input schema: `{plan_file: string, task_id: string, agent: enum["claude","codex"]}`, all required, `additionalProperties: false`. Output schema: same envelope shape as `update_plan_header.output` plus an optional `prior_agent: string` field.
  - Unit tests in `tests/scripts/test_plan_ops.py` (place next to existing `test_mutate_task_status_*` block if present; otherwise immediately after the `mutate_task_status` test block — locate via `grep -n "mutate_task_status" tests/scripts/test_plan_ops.py` before implementing):
    - `test_mutate_task_agent_inserts_when_missing`: child markdown with `**Status:**` + `**Priority:**` + `**Files:**`, no `**Agent:**`. Assert result contains `- **Agent:** codex` between Priority and Files; `prior_agent == ""`.
    - `test_mutate_task_agent_replaces_when_present`: child markdown already declaring `- **Agent:** claude`. Mutate to `codex`. Assert single `**Agent:**` bullet remains, value is `codex`; `prior_agent == "claude"`.
    - `test_mutate_task_agent_idempotent`: mutate twice to the same value. Assert second call returns identical text, `prior_agent == new_agent`.
    - `test_mutate_task_agent_rejects_unknown_agent`: `new_agent="gpt5"`. Assert `ValueError`.
    - `test_mutate_task_agent_rejects_missing_task`: task id absent from plan. Assert `ValueError`.
    - `test_set_task_agent_cli_emits_envelope`: invoke `plan_ops.py set-task-agent --plan-file <tmp> --task-id 001 --agent codex --json`; assert exit 0, stdout JSON contains `{"errors": [], "prior_agent": "..."}`, and the tmp file on disk now contains the bullet.
    - `test_set_task_agent_mcp_dispatch`: dispatch the MCP tool via the conformance harness (model on existing `test_plan_ops_mcp_conformance.py` patterns) with the same inputs; assert envelope shape matches the registered output schema and the file is written.
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "mutate_task_agent or set_task_agent"`
- **Implementation notes:** Read `mutate_task_status` and its tests first; the implementation should be a near-mechanical clone. The bullet-insertion case is the only new logic — model it on `_emit_child`'s ordering. Reuse `_split_task_blocks` and `_find_status_bullet`-style helpers if a `_find_priority_bullet` or `_find_files_bullet` already exists; otherwise inline a single regex with the same idiom (`re.compile(r"^-\s*\*\*Files:\*\*", re.MULTILINE)`).
- **Reversion guidance:** Delete the helper, the three new CLI functions, the argparse subcommand registration, the MCP tool registration block, and the test functions added in `tests/scripts/test_plan_ops.py`. No call sites elsewhere depend on this surface until TASK-002 lands.

### TASK-002: Wire orchestrator Phase 1 Step 2 to persist `**Agent:**`; document policy

- **Status:** Pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `CLAUDE.md`
  - `tests/scripts/test_implement_plan_mcp_e2e.py`
- **Dependencies:** 001
- **Acceptance criteria:**
  - In `SKILL.md` §Step 2 (around line 286-321), add a new sub-step **after** the per-child classifier extraction and **before** the merged-tasks shape-shift in Step 3. Exact text (copy verbatim; this is the contract):
    > After every per-child classifier dispatch returns `status==ok` with `result.agent ∈ {claude, codex}`, dispatch `Tool: plan_ops__set_task_agent with input {"plan_file": "<absolute child path>", "task_id": "<canonical NNN>", "agent": "<result.agent>"}` to persist `**Agent:**` into the child plan file. This is fan-out: one call per classified child, in parallel with sibling sets. On `status!=ok` for the underlying classifier dispatch, DO NOT call `set_task_agent` for that child — let Phase 1 fail through the existing analyst-invalid path.
  - In `SKILL.md` §Dry-run mode (the recently-amended section around line 402+), add a third numbered exemption immediately after the Phase 1.5 plan-revision cycle entry:
    > 3. **Phase 1 Step 2 `**Agent:**` persistence** — `plan_ops__set_task_agent` fires under `--dry-run` so the classifier output reaches disk. Without this, dry-run leaves the plan amnesic and the next real run re-classifies from scratch, defeating the rehearsal value.
  - In `SKILL.md` §Command idioms table (around line 119), add `set-task-agent` to the list of commands that take `--plan-file` (it mutates plan markdown, same family as `commit-task` / `update-plan-header`).
  - In `CLAUDE.md` line 50, extend the "Allowed plan-file mutations" enumeration. Current text: ``Allowed plan-file mutations: `**Status:**` flips, append-only execution-log tail, ...``. New text: ``Allowed plan-file mutations: `**Status:**` flips, `**Agent:**` flips (post-classifier persistence; idempotent, enum-valued), append-only execution-log tail, ...``.
  - Add `test_phase1_step2_persists_agent_to_child_file` in `tests/scripts/test_implement_plan_mcp_e2e.py`:
    - Build a decomposed plan fixture with two tasks, neither declaring `**Agent:**`.
    - Stub the classifier dispatch to return `{"agent": "codex", "classification_reason": "..."}` for both children.
    - Run the harness through Phase 1 Step 2.
    - After Phase 1 completes, read both child files from disk. Assert each contains `- **Agent:** codex` exactly once, in the canonical slot.
    - Assert `.schedule.json` also carries `tasks[*].agent == "codex"` (unchanged behavior).
  - Add `test_phase1_step2_skips_classifier_on_second_invocation`:
    - Same fixture as above. Run Phase 1 once; assert the classifier was called N times (once per missing-agent child).
    - Run Phase 1 a second time on the same plan directory (now with `**Agent:**` populated by the first run).
    - Assert the classifier was called 0 times in the second invocation, AND `tasks[*].agent` in the second-run schedule matches the first-run value.
  - Add `test_phase1_step2_persists_under_dry_run`:
    - Same fixture. Invoke with `--dry-run`. Assert child files on disk DO contain the `**Agent:**` bullet after the dry-run exits. Assert no `.git` commits were made (dry-run still otherwise hermetic).
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2 and (agent or skip_classifier or dry_run)"`
- **Implementation notes:** The orchestrator wiring lives entirely in SKILL.md prose — no `plan_ops.py` change for this task. The e2e test harness already has fixtures for decomposed plans (see existing tests near `test_implement_plan_mcp_e2e.py`'s top for the fixture builder). Model the new tests on the closest existing Phase 1 e2e fixture; do not invent a parallel harness.
- **Reversion guidance:** Remove the three SKILL.md additions (Step 2 sub-step, Dry-run exemption #3, command-idioms table row), revert the CLAUDE.md mutation enumeration, and delete the three new test functions. TASK-001's helper + tool remain installed but uncalled by the orchestrator; no operational impact, just dead surface.

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "mutate_task_agent or set_task_agent"`
2. `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2"`
3. `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_conformance.py tests/scripts/test_plan_ops_mcp_schemas.py -k "set_task_agent"`
4. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`

Manual smoke:

1. Take a small decomposed plan with one child lacking `**Agent:**`.
2. Run `/implement-plan <plan> --dry-run`. Confirm classifier dispatches once.
3. Inspect the child file on disk; confirm `**Agent:**` bullet is now present.
4. Re-run `/implement-plan <plan> --dry-run`. Confirm classifier dispatches **zero** times (skip-classifier short-circuit fires).
5. Confirm `.schedule.json` `tasks[*].agent` matches the persisted child-file value.
