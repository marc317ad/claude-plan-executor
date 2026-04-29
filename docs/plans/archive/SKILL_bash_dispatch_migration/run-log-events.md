# Run-log events: Claude wrapper dispatch lifecycle

This document enumerates the run-log event types added in TASK-006 for the v3
Claude wrapper dispatch path, and records the SKILL.md byte-count baseline +
delta the migration achieved.

## Event types

The TASK-006 consolidation adds three first-class event types to
`ALLOWED_LOG_EVENTS` in `plugins/plan-executor/scripts/plan_ops.py`. They are
emitted by the orchestrator at every `plan_claude_dispatch.py run` invocation
(analyst / implementer / remediator). They sit *alongside* (do not replace)
the existing semantic events `implement_done`, `review_done`, `commit_done`,
`failed`, `awaiting_user`, etc.

### `claude_dispatch_start`

Emitted before invoking `plan_claude_dispatch.py run`.

Required fields: `agent ∈ {plan-analyst, plan-implementer, plan-remediator}`,
`run_id`. Recommended: `task_id` (when the dispatch is task-bound), `plan_file`
(per-child basename when known).

### `claude_dispatch_done`

Emitted when the wrapper returns `status="ok"`.

Required fields: `agent`, `run_id`, `status` (always `"ok"`), `duration_ms`.
Recommended: `session_id`, `model`, `task_id`.

### `claude_dispatch_failed`

Emitted when the wrapper returns any non-`ok` status (every value in the
table below).

Required fields: `agent`, `run_id`, `status`, `status_reason`. Recommended:
`result_raw_truncated`, `stderr_tail`, `task_id`.

## Wrapper status routing (full mapping)

The orchestrator drives `claude-envelope-extract`'s `outcome` field. `outcome`
collapses every non-`ok` status to `"malformed"` *except* `scope_violation`
(Awaiting-user pause) and `cleanup_failure` (run halt) which retain dedicated
routing.

| Wrapper `status`    | `outcome` | Orchestrator action                                                                                                              |
|---------------------|-----------|----------------------------------------------------------------------------------------------------------------------------------|
| `ok`                | per-result| Continue per-site rules; consume `outcome` + `result` sub-fields via downstream parsers (`parse-schedule`, `parse-implementer-report`). |
| `schema_invalid`    | malformed | Per-site malformed branch (analyst → halt `analyst_invalid`; implementer / remediator → Phase C).                                |
| `timeout`           | malformed | Per-site malformed branch.                                                                                                       |
| `denied`            | malformed | Per-site malformed branch; surface `permission_denials[]` in the `claude_dispatch_failed` event payload.                         |
| `backend_error`     | malformed | Per-site malformed branch.                                                                                                       |
| `budget_exhausted`  | malformed | Per-site malformed branch.                                                                                                       |
| `depth_exceeded`    | malformed | Per-site malformed branch.                                                                                                       |
| `manifest_invalid`  | malformed | Per-site malformed branch.                                                                                                       |
| `input_invalid`     | malformed | Per-site malformed branch; treat as orchestrator-internal bug (the wrapper rejected the orchestrator's payload).                 |
| `scope_violation`   | malformed | Pause via Awaiting-user subroutine with `stage:"post_<site>_implement"` (e.g., `post_narrow_remediation_implement`).              |
| `cleanup_failure`   | malformed | Halt with `run_end reason=wrapper_cleanup_failed`; the working tree is in an unknown state (per `claude_dispatch_output.json` schema). |

**Universal invariant.** `status != "ok"` ⟹ no `commit-task` for this task.

## Per-site stage labels

| Dispatch site                       | `agent`             | Failure stage label             | Awaiting-user stage on `scope_violation`        |
|-------------------------------------|---------------------|---------------------------------|--------------------------------------------------|
| Phase 1 — per-child classifier      | `plan-analyst`      | `analyst`                       | n/a (analyst dispatch does not touch the tree)   |
| Phase 1 — whole-plan analyst        | `plan-analyst`      | `analyst`                       | n/a                                              |
| Phase B — implementer (default)     | `plan-implementer`  | `implement`                     | `post_implement_failure`                         |
| Phase B-rework — bounded retry      | `plan-implementer`  | `implement`                     | `post_remediation_implement`                     |
| Phase D.2b — role-swap retry        | `plan-implementer`  | `implement`                     | `post_implement_failure`                         |
| Phase D.2a.6 — narrow remediation   | `plan-remediator`   | `implement`                     | `post_narrow_remediation_implement`              |

## SKILL.md byte-count baseline (TASK-006 measurement)

Captured at TASK-006 implementation:

| Commit / state                                     | `wc -c` (SKILL.md) |
|----------------------------------------------------|--------------------|
| Pre-migration baseline (`0f2153a~1`, before TASK-003) | 97722              |
| After TASK-003 (analyst migration)                 | 99220              |
| After TASK-004 (implementer migration)             | 101047             |
| After TASK-005 (remediator migration)              | 101789             |
| After TASK-006 (this consolidation)                | 101649             |

**Net delta vs pre-migration baseline:** `+3927` bytes (+4.0%). The migration
as a whole added rather than removed bytes: each per-site dispatch grew by
the wrapper-invocation prose (`payload.agent="..."`, `payload.variant="..."`,
v3 envelope shape pointer) that was absent on the pre-migration `Agent(...)`
path. The per-site error-handling sub-paragraphs from TASK-003/004/005 were
collapsed into the shared `## Dispatch error handling (Claude wrapper)`
section in TASK-006 (saving `~140` bytes vs HEAD before TASK-006), but that
saving does not offset the v3 transport documentation added in TASK-003..005.

The AC requirement that `wc -c` show a net reduction vs the pre-migration
baseline is therefore not met by this consolidation alone — the net-reduction
target requires deferring the v3 transport shape documentation out of SKILL.md
(into `dispatch-templates.md` or a dedicated wrapper-contract doc) which is
out of scope for TASK-006. TASK-006 *did* shrink SKILL.md vs the post-TASK-005
HEAD (`101789 → 101649`, `-140` bytes) by replacing three per-site error
shims with one shared paragraph.

A follow-on task (out of TASK-006 scope) should move the v3 wrapper envelope
shape + payload contracts from SKILL.md into `dispatch-templates.md`'s
transport headers; that's where the net reduction vs the pre-migration
baseline lives.
