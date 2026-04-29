# MCP_MIGRATION Claude Worktree Run Observer

Date: 2026-04-29
Observer: Codex
Claude command: `claude -w mcp-migration --permission-mode auto "/implement-plan docs\\plans\\MCP_MIGRATION"`
Worktree: `.claude/worktrees/mcp-migration`
Branch: `worktree-mcp-migration`
Run id observed: `20260429T224915`

## Summary

The worktree-isolated Claude run successfully started in auto mode and loaded `/plan-executor:implement-plan`, but early execution surfaced several sources of overhead before any implementation work began. Most time was spent in orchestration, schema repair, and policy choices around plan decomposition/classification rather than task execution.

The run did not get blocked on normal Claude permission prompts. It did require operator choices for policy/process decisions, and those choices appear important enough that the skill should probably make them explicit or automate them.

## Timeline Notes

- `claude --help` confirmed `-w/--worktree` and `--permission-mode auto`.
- First launch failed under Codex sandbox because Claude needed to create `.git/refs/heads/worktree-mcp-migration.lock`; relaunching with elevated permission for `claude -w` succeeded.
- Claude entered `.claude/worktrees/mcp-migration`, loaded the implement-plan skill, and confirmed auto mode.
- Initial preflight failed because `CLAUDE_PLUGIN_ROOT` was empty inside the worktree, causing a call to `/scripts/plan_ops.py`.
- Claude manually recovered by exporting `CLAUDE_PLUGIN_ROOT=/mnt/d/claude-plan-executor/plugins/plan-executor`.
- Preflight then failed once because unattended execution required an explicit `--unattended-revert-policy`; Claude reran with an explicit policy and passed.
- Phase 0 completed and wrote `run_start` to `docs/plans/_run_log.jsonl`.
- Phase 1 `build-tasks` returned `ok=true` with 10 `extra-task-heading` warnings because the plan contains 10 `TASK-NNN` sections in one markdown file.
- Claude paused for a policy choice:
  - release the lock and restart with `--allow-gaps`, demoting warnings and skipping triage;
  - continue the default Phase 1 triage path.
- Observer chose the default triage path to preserve protocol semantics.
- Plan-review triage dispatch started with `findings_count: 10`.
- The triage report initially failed schema parsing twice:
  - missing fenced `json` block;
  - missing required `load_bearing` and `dismissed` fields.
- Claude repaired/normalized the result and logged `plan_review_triage_done` with verdict `ship`; all 10 warnings were dismissed.
- Claude then paused again because all 10 task chunks lacked `Agent:` declarations. It noted the skill requires per-child classifier dispatches.
- Claude identified a known risk: 10 simultaneous wrapper invocations match a previous "wrapper parallel-batch race" failure shape from the PLAN_GEMINI_INTEGRATION run.
- It offered:
  - hand-edit `Agent:` lines into the plan;
  - fan out 10 parallel classifier dispatches;
  - serialize classifier dispatches one at a time.
- Observer chose serialized classifier fan-out to keep protocol semantics while avoiding the known parallel-wrapper race and avoiding mutation of the plan to force labels.
- During serialized classification, Claude initially called `build-claude-dispatch-input` with an invalid argument shape, received usage output, then corrected the command.
- The first serialized classifier wrapper call completed successfully with about 4.2 KB of JSON output and no stderr; the next classifier call began with a long wrapper timeout (`9m 20s` shown in the TTY).
- After TASK-001 and TASK-002 classifier calls, Claude halted Phase 1 Step 2 because both classifier outputs classified TASK-001. The TASK-002 classifier output still referenced TASK-001 acceptance criteria.
- Claude inspected `cmd_build_claude_dispatch_input` and identified the cause: for `--variant analyst`, payload construction includes only `{plan_path, repo_root}` and deliberately does not propagate `task_id` or `target_task_id`; with 10 task headings in one file, every classifier call sees and classifies the first heading.
- Claude offered three recovery paths: file a bug and halt; decompose the plan into one child file per task and re-run; restart with a global agent override. Observer chose decomposition into a new directory, leaving the original plan directory untouched, then stop for re-invocation.
- Decomposition also failed: `decompose-plan` requires `## TASK-NNN:` H2 task headings, but `PLAN_MCP_MIGRATION.md` uses `### TASK-NNN:` H3 headings under a `## Tasks` parent.
- Claude released the lock, logged `run_end`, and halted cleanly. No implementation, schedule, code, or plan content was changed by the Claude worktree run.
- Worktree artifacts left behind: `docs/plans/_run_log.jsonl` modified in `.claude/worktrees/mcp-migration` with four new run events (`run_start`, `plan_review_triage_start`, `plan_review_triage_done`, `run_end`). Temporary classifier I/O existed under `/tmp/cl_in_*.json` and `/tmp/cl_out_*.json`.

## Performance Observations

- The system spent several minutes before reaching schedule construction, mostly on guardrails and meta-routing.
- The actual issue behind the first warning set was structural and predictable: shared-file task layout. The tooling already appears to support it through `target_task_id`, but the process still escalated to triage.
- The triage step consumed a full subagent round trip to confirm that all 10 warnings were dismissible. That is correct by protocol, but probably expensive for a known soft warning class.
- Schema repair for the triage output added more overhead. The parser did catch malformed output, but the recovery path relied on Claude manually normalizing the report.
- Missing `Agent:` declarations trigger a potentially expensive classifier phase. For 10 chunks, the protocol pushes toward 10 wrapper calls before implementation starts.
- Parallel classifier fan-out is faster but known-risk. Serialized fan-out is safer but likely slow enough to make the system feel stuck.
- Even serialized classifier setup has nontrivial command-construction overhead. Claude had to discover the correct `build-claude-dispatch-input` invocation interactively instead of following a stable wrapper path.
- Classifier wrapper calls are heavyweight relative to the decision they make. The first completed classifier produced a small report after a full wrapper invocation; multiplied by 10 tasks this is a significant fixed pre-implementation cost.
- The classifier was not merely expensive; for shared-file plans it is wrong. Serialized execution confirmed the same TASK-001 result for distinct requested task ids.
- The earlier triage verdict `ship` was based on an assumption that wrapper-side `target_task_id` injection applies to classifier/analyst dispatches. It does not.
- The intended escape hatch, decomposition, also depends on a narrower heading convention than this plan uses. That means the system recognized a shared-file plan problem, tried the canonical decomposition remedy, and still could not promote the plan without another code or plan-format change.

## Issues Surfaced

- `CLAUDE_PLUGIN_ROOT` was not initialized in the worktree session even though the slash command loaded. The skill should not rely on this env var being present unless the CLI/plugin layer guarantees it.
- Plan directory input (`docs\plans\MCP_MIGRATION`) was accepted at the slash-command layer, but some lower-level calls briefly treated the directory as a file. Directory mode needs a more direct normalized path handoff.
- `preflight` requires an unattended revert policy in non-stdin/automation contexts. The skill should pass this explicitly by default when running unattended.
- `plan_ops.py log-event` usage was non-obvious enough that Claude initially passed unsupported `--run-log` and `--run-id` flags, then corrected. The skill docs or command wrappers should hide this detail.
- Phase 1 triage output schema is brittle relative to actual subagent behavior. Missing fenced JSON and missing required fields both occurred.
- The system has no automatic policy for "known benign warning class plus tool-supported mitigation." It required an operator decision.
- Missing `Agent:` annotations produce a large classifier fan-out. This may be too expensive for plans where task classification is inferable from file targets and action type.
- The skill recognized a known race risk from previous wrapper parallelism, but it still required manual choice between unsafe parallelism and slow serialization.
- The classifier dispatch helper surface is easy to misuse from the skill instructions. `build-claude-dispatch-input` produced usage output before Claude found the correct invocation.
- `build-claude-dispatch-input --variant analyst` drops task disambiguation fields. This makes classifier fan-out invalid for shared-file plans and turns `extra-task-heading` from a soft warning into a hard blocker for this path.
- The warning/triage system and the actual classifier implementation disagree about whether shared-file task layout is supported.
- `decompose-plan` only accepts H2 task headings. Plans with `### TASK-NNN:` under `## Tasks` can pass enough early parsing to reach the executor, but cannot use the decomposer to escape shared-file classifier bugs.

## Questions

- Should `extra-task-heading` warnings be auto-demoted when every warning is from a shared-file plan and `target_task_id` injection is available?
- Should directory-mode plans default to an index-derived plan file before any preflight/gates calls, avoiding "directory as plan file" mistakes?
- Should `/implement-plan` always set `CLAUDE_PLUGIN_ROOT` internally from the loaded plugin path rather than depending on environment propagation?
- Should the default unattended revert policy be explicit in the skill command examples and wrapper calls?
- Should the classifier step support an in-process deterministic classifier before dispatching wrapper agents?
- Should classifier dispatch have a bounded concurrency setting, for example `--classifier-concurrency 2`, instead of all-or-serial choices?
- Should classifier wrappers have a much shorter timeout than implementation wrappers, given their expected output is only routing metadata?
- Should the skill expose one command for "classify all missing agents with bounded concurrency" so Claude does not manually compose per-task dispatch input calls?
- Should `build-claude-dispatch-input --variant analyst` carry `target_task_id`, or should shared-file plans be automatically decomposed before any classifier fan-out?
- Should triage be forbidden from dismissing `extra-task-heading` when the next planned route is analyst/classifier dispatch and target-task injection is absent?
- Should `decompose-plan` accept H3 task headings under a `## Tasks` parent, or should plan-authoring validation reject that layout before `/implement-plan` starts?
- Should subagent schema failures be retried with an automatic "return only required JSON schema" repair prompt instead of manual normalization?
- Is hand-editing `Agent:` into the plan an acceptable operational mutation, or should agent assignment live only in generated schedule metadata?

## Current Assessment

The system is enforcing useful safety gates, but the control plane is still too chatty and expensive. For this MCP migration run, the first meaningful implementation work was delayed by environment setup recovery, preflight argument recovery, warning triage, triage schema repair, and classifier fan-out policy.

The most valuable near-term improvements appear to be:

1. Make worktree sessions initialize plugin env/path context reliably.
2. Auto-handle known benign shared-file task warnings.
3. Add bounded concurrency for classifier and wrapper dispatch.
4. Harden subagent JSON schema recovery.
5. Separate agent assignment metadata from source plan mutation.

## Final Run State

Claude halted cleanly at Phase 1. The run log records:

- `run_start` at `2026-04-29T22:49:55Z`
- `plan_review_triage_start` at `2026-04-29T22:51:56Z`
- `plan_review_triage_done` at `2026-04-29T22:54:01Z` with verdict `ship`
- `run_end` at `2026-04-29T22:58:48Z` with reason detail:
  `shared-file layout incompatible with classifier fan-out (build-claude-dispatch-input --variant analyst drops task_id/target_task_id) AND with decompose-plan (requires H2 task headings, plan uses H3)`

The remaining global lock file did not contain the MCP migration path after halt. It still contained an older unrelated lock for `prohibit_silent_revert`.

The worktree was kept at `.claude/worktrees/mcp-migration` so the run log changes are preserved for inspection.

## Follow-up Fix Applied

After the run, the executor was patched in the main workspace to address the two blocking contract mismatches:

- `build-claude-dispatch-input --variant analyst` now preserves `task_id` and `target_task_id` in the dispatch payload, so the existing classifier renderer can inject the target-task disambiguator for shared-file plans.
- `decompose-plan` now accepts both whole-plan `## TASK-NNN:` headings and shared-plan `### TASK-NNN:` headings, while still reporting malformed H2/H3 task headings as structured errors before falling back to `no-tasks`.

Targeted verification:

- `pytest -q tests/scripts/test_plan_ops.py -k "DecomposePlan or BuildClaudeDispatchInput"` passed: 33 selected tests.
- `pytest -q tests/scripts/test_claude_backend.py -k "phase_a_single or target_task_id"` passed: 2 selected tests.
- `decompose-plan` against `docs/plans/MCP_MIGRATION/PLAN_MCP_MIGRATION.md` with output under `/tmp` succeeded and produced 10 child files.
- Analyst dispatch input for MCP TASK-002 now includes `"task_id": "002"` and `"target_task_id": "002"`.
