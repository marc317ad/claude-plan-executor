# BUG-146 — Implementer agent emits legacy result envelope shape

**Status:** OPEN
**Date observed:** 2026-04-27
**Severity:** medium (procedural — every implementer dispatch hits this; orchestrator must hand-finish each report)
**Related:** wrapper_autoclean_authorization plan TASK-002 (paused 22:28:14Z run 20260427T231248)

## Summary

When dispatched via `plan_claude_dispatch.py run` with `output_instructions.format: "json"` and `schema_path: tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json`, the plan-implementer agent emits markdown-fenced JSON in the **legacy shape** with top-level `{status, task_id, summary, files_changed, test_result, test_command, test_output_tail, notes}`, instead of the canonical schema's `{outcome, files_changed, report: {...}}` shape that requires `outcome` (string enum: success | partial | failed | plan-incorrect | blocked | malformed).

The orchestrator's `claude-envelope-extract` parser sees no `outcome` key, returns `outcome=None`, and the SKILL classifies the result as `malformed` — pausing the run via `Awaiting-user pause stage=post_implement_failure` even though the on-disk implementation is correct.

## Reproduction

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

## Root cause hypotheses (need investigation)

1. **Implementer agent spec** (`plugins/plan-executor/agents/plan-implementer.md` lines 105–160) describes the report in **markdown** with `**Outcome:**` heading. When `output_instructions.format: "json"` is set, the implementer treats it as a directive to emit JSON but doesn't follow the schema_path file (the schema isn't inlined in its prompt). Falls back to a guessed legacy shape.

2. **`build-claude-dispatch-input` doesn't inline the result schema.** The dispatch payload references `schema_path` by string path; the implementer doesn't read that file proactively. Should either:
   - Inline the schema in the prompt, OR
   - Inline the canonical example in the prompt, OR
   - Change agent spec to describe the JSON shape directly.

3. **Mismatch between agent-spec format (markdown) and dispatch-payload format (json).** The agent spec describes markdown; the wrapper passes "format: json". Pick one.

## Suggested fix (for a follow-up plan)

Either:
1. Update `plan-implementer.md` agent spec to describe the canonical JSON shape directly (≤30-line addition), OR
2. Update `cmd_build_claude_dispatch_input` in `plan_ops.py` to inline the schema content (not just the path) into `payload.prompt` (~10-line addition to the structured-fallback prompt rendered in `_claude_backend._resolve_prompt`).

## Workaround (current run)

Per the SKILL's documented `awaiting_user` resume options, the orchestrator hand-finishes the report into the canonical schema, then dispatches Phase D code-review on the on-disk diff. This works per-task but requires manual intervention every time.

## Impact

- TASK-002 paused; on-disk work intact (+371 lines correct implementation across 5 files).
- TASK-003+ will hit the same wall under the current dispatch path.
- Hand-finishing per task is cheap (~30 sec orchestrator time per task) but pessimizes the autonomous-run promise.
