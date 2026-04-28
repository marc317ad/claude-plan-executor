---
bug_id: 146
status: OPEN
group: IMPLEMENTER-RESULT-SCHEMA-MISMATCH
severity: minor
source_fix_id: null
source_plan: null
source_date: 2026-04-27
origin: surfaced during /implement-plan TASK-002 of wrapper_autoclean_authorization plan, paused 22:28:14Z run 20260427T231248
decomposed_at: 2026-04-27
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
  - plugins/plan-executor/agents/plan-implementer.md
  - plugins/plan-executor/scripts/plan_ops.py
  - tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json
content_fingerprint: null
change_history: []
---

# BUG-146: Implementer agent emits legacy result envelope shape (top-level `status`/`task_id` instead of canonical `outcome`/`report`)

**Status:** OPEN
**Severity:** minor (procedural — every implementer dispatch hits this; orchestrator must hand-finish each report)
**Group:** IMPLEMENTER-RESULT-SCHEMA-MISMATCH
**Depends on:** none
**Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -v -k "build_claude_dispatch_input or implementer_schema"`

## Acceptance criteria

- The plan-implementer agent dispatched via `plan_claude_dispatch.py run` with `output_instructions.format: "json"` and `schema_path: tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` MUST emit JSON conforming to the canonical schema: top-level `{outcome, files_changed, report: {...}}` with `outcome` ∈ `{success, partial, failed, plan-incorrect, blocked, malformed}`.
- The fix MUST eliminate the orchestrator-side hand-finish loop. After the fix, on a clean implementer run the orchestrator's `claude-envelope-extract` parser MUST resolve a non-`None` `outcome` and the SKILL MUST NOT classify the result as `malformed`.
- A unit test in `tests/scripts/test_plan_ops.py` MUST assert that the prompt rendered by `cmd_build_claude_dispatch_input` (or the implementer agent spec, depending on which side carries the contract) contains the canonical JSON shape — either inlined schema content or a worked example with `"outcome"`, `"files_changed"`, and `"report"` keys.
- An integration smoke test (or a fixture-based wrapper test) MUST stub the `claude` binary returning a canonical-shape payload and confirm the orchestrator parses `outcome` correctly end-to-end.

## Problem

### Symptom

When dispatched via `plan_claude_dispatch.py run` with `output_instructions.format: "json"` and `schema_path: tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json`, the plan-implementer agent emits markdown-fenced JSON in the **legacy shape** with top-level `{status, task_id, summary, files_changed, test_result, test_command, test_output_tail, notes}`, instead of the canonical schema's `{outcome, files_changed, report: {...}}` shape that requires `outcome` (string enum: success | partial | failed | plan-incorrect | blocked | malformed).

The orchestrator's `claude-envelope-extract` parser sees no `outcome` key, returns `outcome=None`, and the SKILL classifies the result as `malformed` — pausing the run via `Awaiting-user pause stage=post_implement_failure` even though the on-disk implementation is correct.

### Reproduction

`run_id=20260427T231248`, `task_id=002`, agent emitted (verbatim shape):

```json
{
  "status": "success",
  "task_id": "002",
  "summary": "...",
  "files_changed": ["..."],
  "test_result": "passed",
  "test_command": "...",
  "test_output_tail": "...",
  "notes": "..."
}
```

Expected per `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json`:

```json
{
  "outcome": "success",
  "files_changed": ["..."],
  "report": { ... }
}
```

### Root cause hypotheses (need investigation)

1. **Implementer agent spec** (`plugins/plan-executor/agents/plan-implementer.md` lines 105–160) describes the report in **markdown** with `**Outcome:**` heading. When `output_instructions.format: "json"` is set, the implementer treats it as a directive to emit JSON but doesn't follow the schema_path file (the schema isn't inlined in its prompt). Falls back to a guessed legacy shape.

2. **`build-claude-dispatch-input` doesn't inline the result schema.** The dispatch payload references `schema_path` by string path; the implementer doesn't read that file proactively. Should either:
   - Inline the schema in the prompt, OR
   - Inline the canonical example in the prompt, OR
   - Change agent spec to describe the JSON shape directly.

3. **Mismatch between agent-spec format (markdown) and dispatch-payload format (json).** The agent spec describes markdown; the wrapper passes "format: json". Pick one.

## Recommended fix

Either:
1. Update `plan-implementer.md` agent spec to describe the canonical JSON shape directly (≤30-line addition), OR
2. Update `cmd_build_claude_dispatch_input` in `plan_ops.py` to inline the schema content (not just the path) into `payload.prompt` (~10-line addition to the structured-fallback prompt rendered in `_claude_backend._resolve_prompt`).

Preferred: option 2, because it keeps the contract co-located with the wrapper-side rendering path being introduced by BUG-144's fix and avoids agent-spec drift. The schema file becomes the single source of truth; the wrapper inlines its content; the agent has no choice but to follow.

## Workaround (current run)

Per the SKILL's documented `awaiting_user` resume options, the orchestrator hand-finishes the report into the canonical schema, then dispatches Phase D code-review on the on-disk diff. This works per-task but requires manual intervention every time.

## Impact

- TASK-002 paused; on-disk work intact (+371 lines correct implementation across 5 files).
- TASK-003+ will hit the same wall under the current dispatch path.
- Hand-finishing per task is cheap (~30 sec orchestrator time per task) but pessimizes the autonomous-run promise.

## Run history

(none yet — bug filed 2026-04-27)
