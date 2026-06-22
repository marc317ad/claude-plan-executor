---
bug_id: 153
status: OPEN
group: CODEX
severity: important
source_fix_id: manual
source_plan: manual
source_date: '2026-06-22'
origin: surfaced 2026-06-22 during parent-orchestrated /implement-plan campaigns (codex
  headless dispatch recurrence)
decomposed_at: '2026-06-22'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/skills/implement-plan/SKILL.md
- plugins/plan-executor/skills/implement-plan/dispatch-templates.md
- plugins/plan-executor/scripts/plan_ops.py
- plugins/plan-executor/scripts/plan_codex_dispatch.py
content_fingerprint: sha256:manual-153
change_history: []
---

# BUG-153: Codex implementer envelope lost when the orchestrator yields the turn

**Status:** OPEN
**Severity:** important
**Group:** CODEX
**Depends on:** none
**Test command:** `none`

## Acceptance criteria
- SKILL.md Phase B documents the run_in_background + TaskOutput{block:true} synchronous-await idiom for codex (and any Bash-shell-out) implementer and reviewer dispatch, with an explicit rule never to end the turn before the envelope is in hand and never to yield mid-batch.
- A claude -w worktree run driven as essentially headless over a plan with a codex-tier task either completes the task with a non-empty envelope and committed code, or fails loudly with a clear error -- it never silently produces an empty envelope.
- A Phase 0 preflight signal detects a non-interactive/automated drive and enforces the guard (await idiom, auto-claude, or explicit-opt-in refusal).
- Plan-review (Phase 1.5) and any Phase D codex/gemini shell-outs follow the same synchronous-await contract.

## Problem
Symptom: in a claude -w worktree run driven as an essentially-headless automated session (the autonomous worktree-run drive; note claude -p print/one-shot mode is API-limited and is NOT used here), a task whose implementer Agent is codex produces an empty / 0-byte envelope -- no source change, no commit, nothing corrupted, nothing recoverable. The whole-plan --claude-only dispatch avoids it.

Root cause -- an orchestration-contract gap in SKILL.md, NOT a wrapper concurrency bug. Phase B routes codex tasks to a Bash shell-out (plugins/plan-executor/skills/implement-plan/SKILL.md:447-448) that can run up to 1800s (plugins/plan-executor/skills/implement-plan/dispatch-templates.md:541-555), whereas claude tasks use the in-process Agent tool (SKILL.md:446) that the harness resolves synchronously within the turn. The codex wrapper itself blocks correctly and only writes its envelope to stdout after the subprocess completes (plugins/plan-executor/scripts/plan_codex_dispatch.py:1071-1076 subprocess.run; emit after :1099-1130) -- there is no backgrounding in the code. SKILL.md:450 says "Await all" but documents no synchronous-await idiom safe for an essentially-headless drive (no run_in_background + TaskOutput{block:true} recipe) and no guard for non-interactive drives. Because the Bash dispatch exceeds the ~10-min foreground cap, the orchestrator yields the turn intending to read the result later. When the session is then continued by a fresh process (e.g. the claude --resume used by an automated worktree drive), that process cannot reattach to the in-flight codex subprocess stdout bound to the prior turn's tool call, so the envelope reads empty (0 bytes). The run is stateful (run-lock + run-log + per-batch A->E loop, SKILL.md:429-433), so yielding mid-batch is unsafe.

Codex-specific: codex has no in-process Agent equivalent (external CLI) so it must be a Bash shell-out; the claude path gained the in-process plan-implementer-default Agent and is therefore immune (this is why --claude-only works). Plan-review (Phase 1.5) uses the same shell-out mechanism (dispatch-templates.md:125-133) and is protected only in practice by its 180s default timeout (under the foreground cap), not by a different code path.

Reproduction: run /implement-plan via a claude -w worktree session driven as essentially headless over a plan with at least one codex-tier task; the codex task's envelope returns empty (0 bytes) with no source change and no commit. Run the same plan with --claude-only and it completes.

## Recommended fix
(a) PRIMARY -- document a synchronous-await idiom (safe for an essentially-headless drive) in SKILL.md Phase B (and Phase 1.5 plan-review + any Phase D codex/gemini shell-outs): issue the dispatch with run_in_background:true, block in the SAME turn via TaskOutput{block:true,timeout:600000} (re-block if it exceeds 10 min), read the envelope only after status=completed, and add a hard rule "never end the turn before the envelope is in hand / never yield mid-batch awaiting a dispatch". Touch SKILL.md:447-450 and the Bash command-idiom note at SKILL.md:88.

(b) non-interactive / automated-drive detection + guard in plan_ops.py preflight (reuse the TTY vs non-TTY stdin signal already used for $UNATTENDED_REVERT_POLICY at SKILL.md:246): under a non-interactive drive, enforce the await idiom, OR auto-prefer the in-process claude implementer, OR refuse codex dispatch absent an explicit opt-in -- fail loud, not silent.

(c) optional --codex-sync flag (largely redundant if (a) is unconditional, since the run is stateful and yielding mid-batch is never safe).

(optional, defensive) make the codex wrapper persist the final envelope to a deterministic run/task-scoped path (not only stdout + the ephemeral temp at plan_codex_dispatch.py:1855-1859) so a lost stdout leaves a recoverable artifact for a resumed run.

## Verification
Run /implement-plan via a claude -w worktree session driven as essentially headless (the autonomous worktree-run drive; claude -p is API-limited and not used) over a plan containing at least one codex-tier task. Confirm the codex task yields a non-empty envelope and a committed code change (or, if the guard disallows codex dispatch under a non-interactive drive, a loud clear error) -- never a silent empty envelope with no code. Cross-check that a run_in_background + TaskOutput{block:true} dispatch reads the envelope within the same turn, and that the per-batch A->E state machine completes without a mid-batch yield. Compare against the --claude-only whole-plan dispatch (which already completes today).

## Reversion guidance
Revert the SKILL.md and dispatch-templates.md wording (the synchronous-await idiom + headless guard) and any plan_ops.py preflight / plan_codex_dispatch.py changes. The whole-plan --claude-only dispatch remains the manual workaround.

---

## Provenance
- **Source plan:** manual
- **Source fix ID:** manual
- **Original review:** surfaced 2026-06-22 during parent-orchestrated /implement-plan campaigns (codex headless dispatch recurrence)
- **Consolidation date:** 2026-06-22
- **First decomposed:** 2026-06-22
- **Group:** CODEX
- **Absorbed from:** none
