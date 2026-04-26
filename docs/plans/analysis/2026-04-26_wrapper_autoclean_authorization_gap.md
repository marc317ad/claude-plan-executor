# Analysis — Wrapper-layer autoclean bypasses the Completed-Work Preservation Principle

**Date:** 2026-04-26
**Discovered during:** prohibit_silent_revert run `20260426T151634` TASK-009 implementer dispatch
**Severity:** P0 — work-destroying defect
**Status:** unaddressed; the principle prohibit_silent_revert was meant to enforce is partially breached
**Follow-up scope:** dedicated plan, ~4–6 tasks (parallel to TASK-001's `--authorization-source` work but for the wrapper layer)

---

## Summary

The `prohibit_silent_revert` plan landed seven tasks (TASK-001..007) that hardened the orchestrator surface against silent destruction of completed work: `cmd_fail_task` requires `--authorization-source`; `reconcile_batch` pauses instead of restoring on out-of-scope deltas; the awaiting-user pause subroutine is canonical; `paused` is a first-class plan status. **TASK-008 added path-traversal validation** for the pause-path's `plan_file` resolution — preventing the pause itself from being weaponized.

**The wrapper layer received none of this hardening.** `plan_codex_dispatch.py` and `plan_claude_dispatch.py` each carry their own `_restore_in_scope()` helper that calls `git restore` on observed file deltas it judges out-of-scope. The judgment is keyed off the agent's `declared_files_changed[]` array, which is silently zeroed to `[]` whenever the agent's `result` fails schema validation against the agent's output schema (`implementer_result.json`, `analyst_result.json`, etc.). When the schema check fails, the wrapper concludes "agent declared zero files but wrote to N files = N out-of-scope deltas to restore" — and silently destroys the work.

This is precisely the silent-revert pattern prohibit_silent_revert was designed to eliminate. The plan caught the orchestrator surface but not the wrapper surface. **Every bash-dispatch run has this gap.**

## Code-grounded evidence

### The orchestrator surface IS hardened (per prohibit_silent_revert)

| File | Function | Protection |
|---|---|---|
| `plugins/plan-executor/scripts/plan_ops.py` | `cmd_fail_task` | `--authorization-source` `required=True` with closed enum; missing → `errors[*].code = "authorization-source-required"` (TASK-001, commit `61fb67c`) |
| `plugins/plan-executor/scripts/plan_ops.py` | `reconcile_batch` (pause path ~line 3684) | `out_of_scope_policy == "pause"` route emits `awaiting_user` event with `dirty_files[]` preserved; basename-validates the schedule's `plan_file` value to prevent traversal (TASK-008, commit `6811dba`) |
| `plugins/plan-executor/scripts/plan_ops.py` | `_check_fail_task_authorization_source` audit | TASK-009 in-tree (preserved on disk by the protected-paths allowlist; +228 lines) |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | Awaiting-user pause subroutine (TASK-004) | Single source of truth; cross-referenced from D.2a.5, D.2a.6, D.4-rescue |

### The wrapper surface is NOT hardened

`plugins/plan-executor/scripts/plan_codex_dispatch.py:880` (and the `plan_claude_dispatch.py` mirror around the same logical site):

```python
def _restore_in_scope(
    tracked: list[str],
    untracked: list[str],
    repo_root: str,
) -> None:
    """Restore/delete changes that are inside allowed_files.

    Safe because caller has already filtered inputs to the task's own scope;
    out-of-scope paths must never be passed here. Sibling work on disjoint
    files is preserved by construction.
    """
    if tracked:
        _git(["restore", "--source=HEAD", "--"] + tracked, cwd=repo_root)
        _git(["restore", "--staged", "--"] + tracked, cwd=repo_root)
        _git(["restore", "--"] + tracked, cwd=repo_root)
```

Three things to notice:

1. **No `authorization_source` parameter.** No gate, no enum, no requirement. It's a raw `git restore`.
2. **The docstring's safety assumption** ("caller has already filtered inputs") is doing all the work. When the caller's filtering is wrong (because schema validation failed and `declared_files_changed` defaulted to `[]`), every observed delta becomes "in-scope for restore."
3. **No run-log audit event.** `git restore` happens silently — no `wrapper_autoclean_executed` event, no `dropped_diff_paths` field, no record. After the fact, the only evidence is the missing files.

The wrapper's `invoke_codex` (and the Claude mirror) returns the agent's stdout in the dispatch result, then a downstream caller in the same wrapper module decides whether the result is schema-valid. On schema-invalid:
- `result.declared_files_changed` is set to `[]` (empty default; not "absent" — silently zeroed)
- The full diff of what the agent actually wrote is computed via `git status`
- All paths in that diff that aren't in `declared_files_changed` (i.e., all of them) are routed to `_restore_in_scope`
- `git restore` zeros the work

## How TASK-009 hit this

Run `20260426T151634`, Phase 2, plan-implementer dispatch via `plan_claude_dispatch.py run --input <payload>`:

1. Wrapper's `claude_dispatch_input.json` declared `output_instructions.format: "json"` against `implementer_result.json`.
2. The `plan-implementer` agent's native output format is **markdown** (it has been since long before the bash-dispatch migration). The agent emitted a markdown success report with `Outcome: success`, all 5 files edited, all AC checked, 10/10 selected pytest passing.
3. Wrapper attempted to validate the markdown against `implementer_result.json` JSON schema — fail.
4. Wrapper set `declared_files_changed = []` (empty fallback).
5. Wrapper observed 5 file deltas.
6. Wrapper concluded scope violation; called `_restore_in_scope` on 4 of the 5 (the 5th, `plan_ops.py`, is in the wrapper's protected-paths allowlist and survived).
7. Returned `status: scope_violation` envelope to the orchestrator.

The implementer's actual work — verifiably correct, tests passing — was destroyed for 4 of 5 files. The orchestrator never had a chance to apply the prohibit_silent_revert pause protocol because the destruction happened *inside the wrapper*, before the orchestrator ever saw the result.

## Why the orchestrator-level fixes don't catch this

The Completed-Work Preservation Principle (`docs/analysis/2026-04-24_completed_work_preservation_principle.md`) was scoped to:

- Phase C (implementer failure)
- Phase D.4 (review-stage failure)
- D.2a binding-mode (Codex needs-rework under `--codex-review-binding`)
- D.2b retry failure
- `reconcile_batch` (Codex out-of-scope writes at the batch-join barrier)

All five paths are **orchestrator-side seams**. The principle's enforcement gate (`cmd_fail_task --authorization-source`) lives on `plan_ops.py`. The principle's pause subroutine lives in SKILL.md.

The wrapper's `_restore_in_scope` is a sixth silent-destruction seam that **nobody mapped**. It was likely treated as "internal wrapper plumbing safe by construction" — the docstring's "Safe because caller has already filtered inputs" matches that mental model. The plan-author of prohibit_silent_revert appears to have audited only the orchestrator-side seams (the run-log events `failed`, `reconcile_kept_*`, `awaiting_user` are all orchestrator-emitted; no `wrapper_autoclean_*` event exists to surface the wrapper's destruction in audit).

The mismatch between the agent's native output format (markdown) and the wrapper's input declaration (`format: "json"`) is the trigger. That mismatch was likely introduced when SKILL_bash_dispatch_migration TASK-004 ("Migrate Phase B plan-implementer dispatches") swapped Agent-tool dispatch for the bash wrapper but didn't update the agent manifest's emit-format. The agent has been emitting markdown all along — fine when the orchestrator was its consumer, broken now that the wrapper is.

## Blast radius

Every plan-implementer dispatch through the wrapper has the same trigger condition. Every plan-remediator dispatch through the wrapper has the same trigger condition (different agent, same wrapper, same `_restore_in_scope`). plan-analyst is structurally JSON-emitting so it's safer, but the same code path applies.

Specifically affected dispatch sites in the orchestrator (post-SKILL_bash_dispatch_migration):

- Phase B default plan-implementer (TASK-004 of SKILL_bash_dispatch_migration)
- Phase B-rework plan-implementer (D.2a.5)
- Phase D.2b role-swap plan-implementer
- Phase D.2a.6 plan-remediator (TASK-005 of SKILL_bash_dispatch_migration)

Four call sites. Each one routes through `plan_claude_dispatch.py run` and is subject to the same schema-validation-fails → `declared_files_changed = []` → silent restore.

The hazard fires whenever:
- Agent emits non-JSON content (markdown, malformed JSON, output truncated mid-stream by the wrapper, etc.)
- Schema validation produces *any* failure mode that defaults `declared_files_changed`

In other words: any agent that "succeeds in spirit but fails the schema." The exact case TASK-009 ran into.

## Recommended fix shape

Mirror prohibit_silent_revert TASK-001's design at the wrapper layer:

1. **`_restore_in_scope` requires an explicit authorization parameter.** Add a `restore_authorization` enum-typed argument; closed `choices=[]`; missing → raise. Initial enum: `wrapper_scope_violation_with_validated_decl`, `wrapper_scope_violation_with_empty_decl_explicit_user`. Missing or unknown value → no restore; emit `wrapper_autoclean_blocked` event instead.
2. **When `declared_files_changed == []` and schema validation has failed**, the wrapper MUST NOT call `_restore_in_scope`. Instead it should:
   - Emit `wrapper_autoclean_blocked` to the run log with diff snapshot, schema-failure reason, and the full agent stdout.
   - Return an envelope with `status: scope_violation`, `extra.wrapper_autoclean_blocked: true`, `extra.preserved_files[]: <observed delta paths>`.
   - Leave the work in the working tree.
   The orchestrator then routes through its existing awaiting-user pause subroutine.
3. **Add `wrapper_autoclean_executed` and `wrapper_autoclean_blocked` to `ALLOWED_LOG_EVENTS`.** Every wrapper-side restore (or refusal-to-restore) becomes auditable. Tests assert the events fire on the failure shapes.
4. **Mirror across both wrappers.** `plan_codex_dispatch.py:_restore_in_scope` and `plan_claude_dispatch.py:_restore_in_scope` are sibling implementations — both need the same gate.
5. **Audit drift check.** Add to `plan_ops.py audit` a check that every `_git(["restore", ...])` call inside `plan_*_dispatch.py` is gated by an authorization parameter — analogous to TASK-009's `_check_fail_task_authorization_source` but for the wrapper layer.

Estimated effort: ~4–6 tasks. Likely structure:

- TASK-A: `_restore_in_scope` argparse + enum + raise-on-missing in `plan_codex_dispatch.py`
- TASK-B: same for `plan_claude_dispatch.py`
- TASK-C: schema-fail short-circuit (no restore on `declared_files_changed = []` from schema-invalid result)
- TASK-D: run-log events + audit drift check
- TASK-E: orchestrator-side resume protocol for `wrapper_autoclean_blocked` envelopes
- TASK-F: tests across all four affected dispatch sites

## Until then — workarounds

**Per-run mitigation:** dispatch `plan-implementer` (and `plan-remediator`) via the in-process Agent tool instead of the wrapper for any plan whose tasks edit > 1 file. The Agent path doesn't go through `_restore_in_scope`. This is off-protocol for the bash-dispatch direction we just landed in SKILL_bash_dispatch_migration but it's correct until the wrapper is hardened.

**Per-task forensics:** if a wrapper run returns `status: scope_violation` with `declared_files_changed == []`, **assume work was destroyed**. Check `git status`, check the wrapper's stderr/trace for the agent's preserved report, and reconstruct manually. Do not trust the wrapper's pre-restore claim that everything was out-of-scope until the gate is fixed.

**Detection trigger:** any `commit_done` event with `disagreement_tag: false` AND the run preceded by a `claude_dispatch_done` event whose result was schema-invalid is a candidate for silent destruction. A quick `jq` query against `_run_log.jsonl` can surface historical incidents.

## Cross-reference

This analysis is the wrapper-layer counterpart to:
- `docs/analysis/2026-04-24_completed_work_preservation_principle.md` — the principle this plan was designed to enforce
- `docs/plans/analysis/2026-04-26_code_reviewer_subagent_missing.md` — sibling systemic gap discovered the same day, also in the dispatch layer
- `docs/plans/prohibit_silent_revert/prohibit_silent_revert/TASK-001_cmd_fail_task_requires.md` — the orchestrator-side `--authorization-source` gate that should be mirrored on the wrapper

The remediation plan, when authored, should explicitly state: *"This extends prohibit_silent_revert into the wrapper layer; without it, the principle is incompletely enforced and silent destruction is possible via every bash-dispatched plan-implementer or plan-remediator run."*
