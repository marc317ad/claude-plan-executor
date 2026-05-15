# PLAN - Wire `implement_plan.py` Phase 1 to classify + persist `**Agent:**`

**Status:** Pending
**Created:** 2026-05-12
**Base branch:** main

## Goal

Bring the script-runner entrypoint (`plugins/plan-executor/scripts/implement_plan.py`) to parity with the LLM-orchestrator's Phase 1 Step 2 `**Agent:**` persistence (shipped in `PLAN_AGENT_PERSISTENCE_2026-05-11.md`, commits `30b9979` + `36dcb5b`). After `build_tasks` returns, for each task whose markdown lacks `**Agent:**`, invoke the configured `classifier` provider's `classify()` method, then persist the result to the child plan file via `plan_ops__set_task_agent`. This closes the loop the Codex Phase D cross-reviewer flagged on TASK-002 of the parent plan ("tests bypass orchestrator path") — the runner-side `classify()` adapter methods exist (`implement_plan.py:989, 1214, 1281`) but no call site invokes them, and the runner has no Step 2 analogue.

## Findings

### 1. Runner reads `**Agent:**`; never writes it

`resolve_assignments` (`implement_plan.py:534`) consumes `**Agent:**` as routing precedence #2 via `_task_agent(task)` (line 583). The runner's `ProviderAdapter.classify()` methods (`implement_plan.py:989, 1214, 1281`) are defined on every provider but **unreferenced** — `grep -n "\.classify\(" implement_plan.py` returns only the definitions, no call sites. The runner thus treats `**Agent:**` as input-only and falls through to `provider_preference` when absent.

### 2. LLM-orchestrator and runner share the persistence surface

`mutate_task_agent` (`plan_ops.py`) and `plan_ops__set_task_agent` are pure, transport-agnostic, and already shipped. The runner reaches them via the same `PlanOpsFacade` it uses for `build_tasks` / `write_schedule` (`implement_plan.py:789`). No new tool, no new helper, no new CLI subcommand needed — just a new call site inside the runner's Phase 1 sequence.

### 3. Insertion point is unambiguous

The runner's Phase 1 sequence (`implement_plan.py:3290-3323`) is:

1. `run_start` event.
2. `facade.build_tasks(...)` → `build`.
3. `tasks = list(build.get("tasks", []))`.
4. `resolve_assignments(tasks, config, resolved_capabilities)`.
5. `_schedule_from_build(build, ...)` → `write_schedule`.

The classify-and-persist step inserts between (3) and (4): identify tasks where `_task_agent(task) is None`, dispatch the classifier per task, persist each result to the child file, then refresh the in-memory `tasks[]` so `resolve_assignments` sees the new `agent` field. Re-running `build_tasks` after persistence is the simplest refresh — `build_tasks` is the canonical reader and the post-persist read is the source of truth.

### 4. Classifier dispatch shape

The `classify()` adapter signature is `**payload: Any -> DispatchResult`. The Claude adapter at `implement_plan.py:1214` already routes to `self._claude("classify", "analyst", **payload)` — the wrapper carries the `plan-analyst` agent name. Payload contract is whatever the existing analyst dispatch input expects (see `plan_ops__build_claude_dispatch_input` with `variant="analyst"`). Expected `DispatchResult.result` JSON: `{"agent": "<claude|codex>", "classification_reason": "<str>"}` — identical to the LLM-orchestrator path (verified against `/tmp/classifier_001_envelope.json` and `/tmp/classifier_002_envelope.json` from run `20260512T022637`).

### 5. Skip-classifier short-circuit must hold

After persistence, a second runner invocation on the same plan must dispatch the classifier **zero** times. This mirrors `SKILL.md:290`'s short-circuit ("If `missing_agent_children` is empty, the classifier step is skipped entirely"). The runner achieves this implicitly: the missing-agent filter sees no tasks, the classify-and-persist loop is a no-op, and `resolve_assignments` consumes the persisted `**Agent:**` directly.

### 6. Dry-run exemption already in policy

SKILL.md §Dry-run mode exemption #3 (shipped in parent plan TASK-002) declares `**Agent:**` persistence fires under `--dry-run`. The runner inherits the same exemption — when `config.dry_run` is true, classify + persist still run; only dispatch / commit are suppressed downstream. No new policy decision.

### 7. Provider-preference precedence is unchanged

When the classifier provider itself is unavailable or returns `status != ok`, the runner falls back to the existing `_task_agent` → `provider_preference` chain in `resolve_assignments`. The new step is **additive**: it tries to fill `**Agent:**` when missing, but failure to do so is not fatal — the runner proceeds with whatever routing the existing precedence yields. This matches the LLM-orchestrator's "on `status!=ok` for the underlying classifier dispatch, DO NOT call `set_task_agent` for that child" rule (parent plan TASK-002 AC).

## Decisions

1. **Single task.** Both the wiring and the test live in one task; this is a narrow, self-contained loop closure. Splitting would add ceremony without bounded-context benefit.
2. **Insert between `build_tasks` and `resolve_assignments`.** This is the natural seam; no other phase touches `tasks[]` between those two calls.
3. **Re-call `build_tasks` after persistence.** Cheap, correct, single source of truth. Avoids hand-patching the in-memory `tasks[]` and risking drift between disk and memory.
4. **Serial classifier dispatch, not parallel.** Simpler to test, no concurrency primitive needed in the runner today, and Phase 1 latency is not the optimization target for the script-runner path (it's primarily a CI / non-interactive surface). The LLM orchestrator parallelizes via the Agent tool's parallel-call ergonomics; the runner has no equivalent.
5. **`status != ok` is non-fatal.** Per Finding #7. Log via `classifier_dispatch_failed` (or a new `classifier_skipped` event if no allowed value matches — check `ALLOWED_LOG_EVENTS` before invention) and continue. The runner already tolerates missing-agent tasks via provider-preference.
6. **No retro-fill, no flag.** Same as parent plan decision #5 / #6. The next runner invocation does its one classifier pass per task and is done.

## Scope

In scope:

- `plugins/plan-executor/scripts/implement_plan.py` — new classify-and-persist helper function, call site between `build_tasks` and `resolve_assignments` in `run()` (~line 3310).
- `tests/scripts/test_implement_plan_runner_phase1.py` — new test file (or add to existing runner-side test file if one exists; locate via `grep -rln "implement_plan.run\b\|from .* import run\b" tests/`).

Out of scope:

- Parallelizing the classifier fan-out in the runner.
- Caching classifier output in `00_INDEX.json`.
- New CLI flags (`--no-classify`, etc.).
- LLM-orchestrator changes (already shipped in parent plan).
- Changes to `mutate_task_agent`, `plan_ops__set_task_agent`, or the analyst dispatch contract.
- Retroactive edits to existing decomposed plans on disk.

## Tasks

### TASK-001: Wire runner Phase 1 to classify missing-agent tasks and persist via `set_task_agent`

- **Status:** Pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/implement_plan.py`
  - `tests/scripts/test_implement_plan_runner_phase1.py`
- **Dependencies:** none (parent plan's TASK-001 + TASK-002 already shipped at `30b9979` + `36dcb5b`)
- **Acceptance criteria:**
  - In `implement_plan.py`, add a module-level helper `_classify_and_persist_missing_agents(facade, registry, classifier_capability, build, effective_plan_path, dry_run) -> tuple[Mapping[str, Any], list[dict]]`. Returns `(refreshed_build_result, classifier_events)`. The events list is an append-only log of `{task_id, status, agent|error}` entries for the caller to forward to `_log(...)`. Read `mutate_task_agent` first to mirror its `ValueError` semantics for the persistence-failure case (treat as non-fatal: log and continue).
  - Helper behavior:
    1. Compute `missing = [t for t in build["tasks"] if _task_agent(t) is None]`.
    2. If `missing` is empty: return `(build, [])` immediately — short-circuit fires, zero classifier calls.
    3. For each task in `missing`: resolve `classifier_adapter = registry[classifier_capability.name]`. Build the classifier payload by mirroring the existing `build_claude_dispatch_input` "analyst" variant payload — read `_phase1_classify_payload` or equivalent helper if present; otherwise inline a single dict with `plan_file=<child path>`, `task_id=<task["id"]>`, `variant="analyst"`.
    4. Invoke `classifier_adapter.classify(**payload)`. On `result.status != "ok"`: append `{task_id, status: "failed", error: result.error}` to events and continue (do NOT call `set_task_agent`).
    5. On `result.status == "ok"`: parse `result.raw_envelope` (or `result.result`) for `{"agent": "<claude|codex>"}`. Reject any other shape as a classifier failure (same non-fatal path).
    6. Call `facade.set_task_agent(plan_file=<absolute child path resolved from task["plan_file"]>, task_id=<task["id"]>, agent=<parsed agent>)`. On any error result (envelope `errors` non-empty): treat as non-fatal, log, continue.
    7. After the loop, if at least one persistence succeeded: re-call `facade.build_tasks(plans_dir=effective_plan_path, filter_ids=...)` and return its result; otherwise return the original `build`.
  - Call site: in `run()` immediately after the existing `tasks = list(build.get("tasks", []))` at `implement_plan.py:3310`, but before `assignment_plan = resolve_assignments(...)`. Replace those two lines (the `tasks =` assignment + the `resolve_assignments` call's `tasks` argument) so they read the refreshed build. Wire the returned events through `_log(facade, "<event-name>", ...)` per event; check `ALLOWED_LOG_EVENTS` in `plan_ops.py` before choosing event names — if no allowed value matches `classifier_dispatch_done` / `classifier_dispatch_failed`, fold both into the closest existing event (e.g. `analyst_dispatched` / `analyst_dispatch_failed`) rather than inventing new ones.
  - The helper must respect `dry_run`: under `--dry-run`, both `classify()` and `set_task_agent` still execute (per parent plan's Dry-run exemption #3 in SKILL.md). The helper itself does not branch on `dry_run` — it is the caller's responsibility to suppress downstream dispatch/commit. Verify by reading `implement_plan.py:3290-3325` for how dry-run gates today; if classify is already gated elsewhere, document that the new helper sits *upstream* of the gate.
  - Reuse existing helpers: `_task_agent` (line 387), `_resolve_child_plan_path` if it exists (otherwise compute via `effective_plan_path / task["plan_file"]`), and the facade's `set_task_agent` / `build_tasks` methods. Do NOT add inline plan-markdown parsing — that is a Cardinal forbidden action.
  - Tests in `tests/scripts/test_implement_plan_runner_phase1.py` (create the file if absent; otherwise add to the runner-side test module that already exercises `implement_plan.run`):
    - `test_runner_phase1_classifies_and_persists_missing_agents`: build a 2-task decomposed plan fixture where neither child declares `**Agent:**`. Stub `ProviderAdapter.classify` (on the test registry) to return `DispatchResult(status="ok", raw_envelope={...}, result={"agent": "codex", "classification_reason": "stub"})` for both children. Invoke `run(...)` through Phase 1 only (dry-run is fine for hermetic). Assert: (a) both child files on disk now contain `- **Agent:** codex` exactly once in the canonical slot; (b) the in-memory schedule's `tasks[*].agent == "codex"`; (c) `classify` was invoked exactly 2 times.
    - `test_runner_phase1_skips_classifier_when_all_agents_present`: same fixture, but pre-populate `**Agent:** claude` in both children before invoking `run`. Stub `classify` to raise if called. Assert: `run` completes without error, `classify` was invoked 0 times, schedule `tasks[*].agent == "claude"`.
    - `test_runner_phase1_classifier_failure_is_non_fatal`: same fixture as the first test, but stub `classify` to return `DispatchResult(status="failed", error="stub")` for one task and `status="ok"` for the other. Assert: `run` proceeds past Phase 1, the successful task's child file is persisted, the failed task's child file is **not** modified, and the schedule still routes the failed task via `provider_preference` (i.e. carries the runner's default).
- **Test command:** `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_phase1.py -k "runner_phase1"`
- **Implementation notes:** Read `implement_plan.py:3087-3330` (the `run()` function) end-to-end before editing — the classify-and-persist seam is short, but Phase 1's downstream consumers (write_schedule, resolve_assignments) are tightly coupled to the shape of `build["tasks"]`. The re-call to `build_tasks` after persistence is the safest refresh; do not patch `build` in place. Locate the existing runner test module via `ls tests/scripts/test_implement_plan_*.py` and check whether `test_implement_plan_directory_smoke.py` or another file is the right home — only create a new test file if no runner-side Phase 1 test file already exists.
- **Reversion guidance:** Delete the new `_classify_and_persist_missing_agents` helper, revert the two-line call-site change in `run()` (restore the original `tasks = list(build.get("tasks", []))` + `resolve_assignments(tasks, ...)`), and delete the new test functions / test file. The runner reverts to its pre-existing behavior of consuming `**Agent:**` when present and falling through to `provider_preference` when absent — no breaking change to existing callers.

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_implement_plan_runner_phase1.py -k "runner_phase1"`
2. `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "mutate_task_agent or set_task_agent"` (regression — parent plan's tests must still pass)
3. `venv/bin/pytest -q tests/scripts/test_implement_plan_mcp_e2e.py -k "phase1_step2"` (regression — LLM-orchestrator path untouched)
4. `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`

Manual smoke:

1. Take a small decomposed plan with one child lacking `**Agent:**`.
2. Run `$PYTHON plugins/plan-executor/scripts/implement_plan.py <plan> --dry-run`. Confirm classifier dispatches once for the missing child.
3. Inspect the child file on disk; confirm `**Agent:**` bullet is now present in the canonical slot.
4. Re-run the same command. Confirm classifier dispatches **zero** times (skip-classifier short-circuit fires).
5. Confirm the printed schedule's `tasks[*].agent` matches the persisted child-file value.
