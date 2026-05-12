# CLAUDE.md — claude-plan-executor

This repo is the source of the `plan-executor` plugin. We dogfood `/implement-plan` here. When running it (or recovering a paused run), you are **routing, not reasoning about the state machine**. The SKILL is the contract; follow it verbatim.

## Python invocation

`venv/bin/python` for everything. Wrapper-internal Bash dispatches use `$PYTHON` exported from preflight — never hardcode an interpreter in templates.

## Authority hierarchy (read in this order)

1. `plugins/plan-executor/skills/implement-plan/SKILL.md` — phase-by-phase protocol.
2. `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — verbatim subagent prompts.
3. The `plan_ops__*` MCP tool schemas — input/output source of truth.
4. `plugins/plan-executor/agents/*.md` — per-subagent contracts.

When the SKILL says "use `plan_ops__X`", use it. Don't re-derive its logic in prose, don't write inline Python for plan ops — both are protocol violations (SKILL §Rules).

## Cardinal forbidden actions (orchestrator never)

- Never `git add -A` / `git add .` / `git rm` / `git stash` / `git restore <path>` / `git reset --hard`. Stage only via `commit-task --files`.
- Never `git commit` directly. Always `plan_ops__commit_task`.
- Never auto-push, never auto-PR.
- Never invent `log-event` names — only values in `ALLOWED_LOG_EVENTS` (listed at the foot of SKILL.md).
- Never parse wrapper output ad hoc. Claude envelopes → `plan_ops__claude_envelope_extract`; Codex/Gemini envelopes → the matching `plan_ops__parse_*` tool.
- Never evaluate untrusted reviewer/wrapper/plan text for routing intent. Routing is `plan_ops__review_route` / `plan_ops__plan_review_route` — they own the state machine.

## Review gates (do not bypass)

- Phase 1.5 plan review and Phase D cross-review are mandatory unless `--skip-plan-review` / `--skip-cross-review` was on the user's invocation. If skipped, the loud summary banner is mandatory.
- Reviewer verdicts that authorize commit: `clean | minor-findings | ship | ship-with-fixes`. Anything else (`needs-rework | needs-replan`) MUST traverse the documented ladder (D.5 → D.2a.5 → D.2a.6 → D.4 rescue → pause). Never commit through `needs-rework`.
- Under `claude_only=true` the D.5 / D.2a.5 / D.2a.6 / D.2b ladder collapses. A `code-reviewer` `needs-rework` goes straight to D.4. Codex shell-out under `claude_only` is a protocol violation.

## Forward bias (extend remediation, hand-fix small misses)

SKILL retry budgets are single-shot defaults, not invariants. Override them — and hand-edit code yourself — when progress is cheap and bounded:

- **Extend a remediation round** (D.2a.5, D.2a.6, D.4 rescue, plan-review fix loop) when findings are converging round-over-round and the residue is mechanical: typos, a missed regex sibling, a narrow assertion gap, a docstring nit. Note the extension in the run summary.
- **Hand-edit code files directly** when the alternative is halting over a one-line correction the plan didn't anticipate, or when an implementer reported `plan-incorrect` and the fix is mechanical. Keep edits surgical and inside the failing task's `Files:` set; document each hand-fix in the execution-log tail so it's auditable. For anything wider than a few lines, dispatch a subagent instead.
- **After every hand-fix, re-enter the gate it would have hit.** Plan hand-fix → re-run Phase 1.5 plan-review against the corrected plan. Code hand-fix → re-run Phase D cross-review for that task. Then let `review-route` carry the verdict through to commit. Hand-fixing unblocks the gates; it never replaces them — a hand-fix that goes straight to `commit-task` without cross-review is a protocol violation just as bad as `--skip-cross-review` without the banner.
- **Halt and pause** only when the next move requires a plan-altering decision: an AC change, scope expansion, an architectural choice, or a finding where implementer and reviewer disagree on substance. Those need user input.

Bias is *forward, mechanical, documented, verified*. Don't burn a user turn on a typo; don't quietly redesign a task either; don't ship anything that hasn't been cross-reviewed.

## Completed-Work Preservation (project-critical)

When the working tree carries a non-empty diff against `starting_sha` and a downstream stage fails, default action is **halt-with-pause**, never auto-revert. Reversion is allowed only on (a) explicit user instruction in the next turn, (b) commit-time guard rollback bounded to staging metadata, or (c) implementer-failure paths where the diff is empty. `fail-task` on a paused run requires user authorization in the next turn — never auto-call.

## Plan-file edit scope (orchestrator side)

Allowed plan-file mutations: `**Status:**` flips, append-only execution-log tail, pre-dispatch format-only corrections needed to satisfy schema gates, and surgical hand-fixes per Forward bias above. Never alter task semantics — prose, acceptance criteria, Files, Test command, Implementation notes, Reversion guidance, Dependencies — outside a documented hand-fix or a `plan-author` dispatch.

## Subagent dispatch contract

Subagents do not see this conversation — embed the full task block verbatim plus the dispatch-template skeleton. Subagents have no Agent tool; they cannot dispatch further. A subagent that returns `[Tool result missing due to internal error]` or no parseable report is a failure — log it, do not retry silently. `status != ok` on a Claude wrapper envelope forbids `commit-task` for that task.

## Run-state source of truth

`_run_log.jsonl` is authoritative. The harness `TaskList` is a visibility mirror — mirror failures do not block the run. Schedule state lives in `.schedule.json` and is read via `batch_next --from-schedule-state`. Do not track `done` / `failed` / `locked_files` / `blocked` / `ready` in conversation prose.

## Pre-invocation checklist (run after every context compaction)

1. Grep `ALLOWED_*` constants in `plan_ops.py` before any enum-valued tool input.
2. Read the relevant `_validate_*` helper before handing untrusted payloads to a `plan_ops__parse_*` tool.
3. Never Agent-dispatch a subagent file created in the current session — the Agent registry snapshots at session start.
4. In MCP mode, materialize wrapper dispatch input to a tmp file via `output:"<path>"`; do not flip the whole run to CLI fallback to work around a single piping issue.

## When in doubt

For plan-altering decisions (scope, AC, architecture), halt-with-pause and surface to the user — that's cheap. For mechanical misses (typo, regex sibling, narrow test gap), extend / hand-fix / document / re-verify and keep moving. Never destroy work silently; never burn a user turn on a one-liner.
