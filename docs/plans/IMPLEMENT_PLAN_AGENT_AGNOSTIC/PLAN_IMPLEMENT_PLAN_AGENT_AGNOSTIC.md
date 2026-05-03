# Plan: Agent-Agnostic `implement_plan.py` Runner

**Created:** 2026-05-02
**Last reviewed against codebase:** 2026-05-03
**Status:** Pending
**Base branch:** main
**Depends on plans:**
- `docs/plans/PHASE_D_STATE_MACHINE_COMPLETION/PLAN_PHASE_D_STATE_MACHINE_COMPLETION.md`
- `docs/plans/PHASE_1_5_STATE_MACHINE/PLAN_PHASE_1_5_STATE_MACHINE.md`
- `docs/plans/MCP_MIGRATION/PLAN_MCP_MIGRATION.md`

## Goal

Create an executable `implement_plan.py` runner that can drive the existing `/implement-plan` workflow without relying on Claude-specific SKILL prose as the orchestrator. The runner must be agent-agnostic: it accepts explicit provider/task assignments, can infer safe assignments from provider capabilities and task metadata, and routes all Phase 1.5 and Phase D decisions through the completed deterministic `plan-review-route` and `review-route` contracts.

The end state is a scriptable entry point:

```bash
venv/bin/python plugins/plan-executor/scripts/implement_plan.py docs/plans/example \
  --parallel 2 \
  --provider-preference codex,claude,gemini \
  --assign TASK-001=claude,TASK-002=codex \
  --dry-run
```

and, for structured callers:

```bash
venv/bin/python plugins/plan-executor/scripts/implement_plan.py run \
  --plan docs/plans/example \
  --config docs/plans/example.runner.json
```

## Context

The system has already paid down the largest sources of Claude-specific orchestration risk:

- Phase D routing is now owned by `plan_ops.py review-route`, with reviewer identity, Gemini fallback semantics, `claude_only`, binding mode, unattended revert policy, public `review_route_called` run-log events, and MCP conformance.
- Phase 1.5 routing is now owned by `plan_ops.py plan-review-route`, with persistent `plan_review_state`, public `plan_review_route_called` events, and MCP registration. The Phase 1.5 sidecar still has TASK-009/TASK-010 hardening work pending in the current worktree; this runner should consume the existing route contract, not assume those hardening tasks have landed unless their tests are present.
- `plan_ops_mcp_server.py` exposes `plan_ops__*` tools over structured schemas, while `plan_ops.py` keeps pure `_run_*` entry points and CLI compatibility.
- `plan_codex_dispatch.py`, `plan_claude_dispatch.py`, and `plan_gemini_dispatch.py` already normalize external model/agent execution into structured wrapper envelopes with timeout, schema, and cleanup behavior.

The remaining Claude-specific piece is the outer orchestration loop in `SKILL.md`: parsing command flags, selecting agents, dispatching wrappers in batches, calling route tools, logging events, committing/failing/pausing, and summarizing. This plan moves that loop into `implement_plan.py` while preserving the SKILL as a thin bridge and operational reference.

## Design

### Runner boundaries

`implement_plan.py` owns orchestration only. It must not duplicate parser, scheduler, route, commit, fail, lock, or gate logic that already lives in `plan_ops.py`. It calls the existing pure cores or CLI/MCP-equivalent wrappers through a narrow `PlanOpsFacade`.

The runner dispatches implementation/review work through provider adapters. Provider adapters have a public provider id and, where needed, a route identity used by today's route schemas:

- `codex`: wraps `plan_codex_dispatch.py implement|review|plan-review`.
- `claude`: wraps `plan_claude_dispatch.py run` with inputs built by `plan_ops.py build-claude-dispatch-input`.
- `gemini`: wraps `plan_gemini_dispatch.py review|plan-review`; implementation remains unsupported until a future provider capability explicitly says otherwise.
- `stub`: test-only provider for e2e state-machine coverage without live model calls.

Current `review-route` accepts implementers `claude|codex` and reviewers `codex|gemini|claude|none`. Initial runner support must fail closed for any implementation provider that cannot map to a route-supported implementer identity. Supporting a genuinely new implementing provider requires extending `review_route_input_schema.json`, the route tests, and the Phase D route table first.

### Provider-agnostic contracts

Provider selection is driven by capabilities rather than provider names hardcoded into the runner:

```json
{
  "provider": "codex",
  "available": true,
  "route_implementer": "codex",
  "route_reviewer": "codex",
  "roles": ["implement", "review", "plan_review"],
  "edits_files": true,
  "supports_structured_output": true,
  "supports_plan_review": true,
  "supports_review_verdicts": ["clean", "minor-findings", "needs-rework"],
  "supports_implementation": true
}
```

The runner maps provider outputs into the existing route payloads. It does not invent verdict vocabularies or route identities.

### Agent arguments

The user-facing argument surface must support both explicit and automatic routing:

- Explicit: `--assign TASK-001=claude,TASK-002=codex`, `--reviewer TASK-001=gemini`, `--plan-reviewer codex`.
- Policy: `--provider-preference codex,claude,gemini`, `--allow-provider-fallback`, `--codex-only`, `--claude-only`.
- Config file: JSON form for CI and higher-level agents.

Explicit assignments win over task `**Agent:**`; task `**Agent:**` wins over auto-policy; auto-policy uses capabilities and safe defaults. Unsupported combinations fail before dispatch.

### State and resume

The runner persists state through existing schedule/run-log files wherever possible. Any runner-only state must be stored in a documented sidecar adjacent to the persisted schedule, preferably `<schedule_file>.runner-state.json`, and contain only orchestration metadata: run id, current phase, active batch, provider assignments, pause payload, and resume token. It must not duplicate authoritative task status, commit status, or route state already represented in schedule/run-log.

## Non-goals

- Rewriting `plan_ops.py` state machines.
- Replacing MCP with a new transport.
- Removing the SKILL or slash command.
- Adding Gemini implementation support.
- Adding arbitrary new implementation-provider route identities before Phase D schemas support them.
- Changing wrapper output schemas or reviewer verdict vocabularies.
- Supporting arbitrary remote agent APIs before the local CLI provider adapter contract is stable.

## Execution

Eight tasks, eight scheduler batches:

- **Batch 1:** TASK-001.
- **Batch 2:** TASK-002.
- **Batch 3:** TASK-003.
- **Batch 4:** TASK-004.
- **Batch 5:** TASK-005.
- **Batch 6:** TASK-006.
- **Batch 7:** TASK-007.
- **Batch 8:** TASK-008.

TASK-003 and TASK-004 are conceptually separable, but both touch `implement_plan.py`; the current scheduler correctly serializes them.

## Verification

Minimum final verification:

```bash
venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_contracts.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_cli.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_provider_adapters.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_assignment_policy.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_phase15.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_phase_d.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_resume.py
venv/bin/python -m pytest tests/scripts/test_implement_plan_runner_e2e.py
venv/bin/python plugins/plan-executor/scripts/plan_ops.py build-tasks --plans-dir docs/plans/IMPLEMENT_PLAN_AGENT_AGNOSTIC --json
```

Manual smoke after TASK-008:

```bash
venv/bin/python plugins/plan-executor/scripts/implement_plan.py \
  docs/plans/IMPLEMENT_PLAN_AGENT_AGNOSTIC \
  --dry-run \
  --provider-preference stub,codex,claude,gemini
```

## Review Notes For Gemini And Claude

Review should focus on whether the task split prevents reimplementing `plan_ops.py`, whether provider capability negotiation is sufficiently narrow, whether pause/resume introduces a second source of truth, and whether the CLI/config contract is ergonomic enough for other agents to call.

## Codebase Re-review Notes

The 2026-05-03 re-review found these current-code constraints and folded them into the child tasks:

- There is no `plugins/plan-executor/commands/implement-plan.md` file in this plugin; TASK-008 should update the SKILL and plugin docs only unless a separate slash-command scaffold exists by then.
- `plan-review-route` currently supports `pre_dispatch`, `post_review`, `post_triage`, `post_plan_author`, and `manual_pause`; second review is represented by `post_review` with `attempt=2`, not by a `post_second_review` stage.
- `batch_done` is not a public `log-event` event in `plan_ops.ALLOWED_LOG_EVENTS`; the runner must use the current allowlist or explicitly add a separate event-contract task before emitting it publicly.
