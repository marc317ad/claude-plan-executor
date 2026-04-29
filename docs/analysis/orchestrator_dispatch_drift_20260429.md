# Orchestrator dispatch drift: hand-fix substitution + envelope schema mistakes

**Date:** 2026-04-29
**Author:** Claude Opus 4.7 (orchestrator session)
**Reviewers consulted:** Gemini (`gemini -p`), Codex (`codex exec`)
**Subject:** Two related but independent failures observed in the dual-agent plan executor's orchestrator behavior, both surfacing during/after the bash-dispatch refactor (commits `0f2153a`, `c83168b`, `f68dbfe`, `c776d4c`, 2026-04-26 / 2026-04-27).

---

## 0. TL;DR

A prior diagnosis tried to thread two failure modes onto a single root cause. **It is a forced fit.** Reviewers (Gemini, Codex) and a closer read of the live evidence confirm two independent failures with a partially shared enabler:

1. **Authorization-boundary leak** (Issue A — orchestrator hand-fixes instead of dispatching the remediator). Caused by an over-broad memory entry, not by interface friction.
2. **Contract ergonomics drift** (Issue B — orchestrator constructs `plan_ops.py` envelopes from working memory, gets schemas wrong). Caused by accumulated CLI surface area across `plan_ops.py` (13641 lines) and `SKILL.md` (874 lines), only partially aggravated by the bash-dispatch migration.

The bash-dispatch migration was **correctly motivated** (orchestrator context bloat reduction; Agent-registry session-snapshot bypass; Codex symmetry) and **should not be deprecated**. The wrapper carries security-relevant work (env scrubbing, preflight guardrails, schema validation, baseline+cleanup) that `Agent()` does not. The user's "deprecate `plan_claude_dispatch`" instinct identifies a real cognitive defect on the orchestrator side but prescribes the wrong remedy.

The fix is **layered**: a policy fix for Issue A (memory + commit-time guard), an ergonomics fix for Issue B (typed helper subcommands + pre-flight validate), and a thin orchestrator-side dispatch consolidation that preserves the wrapper-internal architecture while restoring atomic call shape.

---

## 1. Verified evidence

| Claim | Source | Status |
|---|---|---|
| Bash-dispatch migration replaced 4 `Agent()` call sites | commits `0f2153a` (Phase 1 analyst), `c83168b` (Phase B implementer × 3 sites), `f68dbfe` (D.2a.6 remediator), `c776d4c` (`build-claude-dispatch-input` + 4 dispatch-site rewires) | verified |
| Orchestrator literally substituted in-session work for wrapper dispatch | `_run_log.jsonl:1586` — `run_end ... reason: "orchestrator_substituted_in_session_run_should_be_claude_-p"` for run `20260428T120332` | verified |
| Orchestrator hand-fixed instead of dispatching remediator | `_run_log.jsonl:1678` — `remediation_start ... mechanism: "hand-fix" ... reason: "narrow doc phrasing fix; user-authorized hand-fix per memory feedback_handfix_default"` for run `20260429T111054` | verified |
| Claude-wrapper does work that `Agent()` does not | `plan_claude_dispatch.py:31-53` docstring enumerates: schema-validate input → manifest load → preflight guardrails → baseline snapshot → env scrub → backend invoke → cleanup → output schema validate → span log | verified |
| `finalize-execution-log` row schema (cited as wrong by second Claude) | `SKILL.md:769` — `{task, agent, reviewer, verdict, commit, notes}` (six required string keys; no extras) | verified |
| `SKILL.md` line 17 still says "Claude-tier via Agent" while line 518 routes through bash | doc drift: dispatch rules summary not updated post-migration | **doc bug** |
| Migration motive | `docs/plans/SKILL_bash_dispatch_migration/PLAN_SKILL_bash_dispatch_migration.md:17` — "orchestrator stops ingesting full subagent markdown reports... orchestrator context per iteration drops materially" | verified |
| Agent-registry session-snapshot was an additional motive | `SKILL.md:822` — Pre-invocation rule #4: "Never Agent-dispatch a subagent file created during the current run" | verified |

Each of the four cited refactor commits also carries follow-up notes in its body — most relevantly, `0f2153a` flags a separate cross-cutting infra gap (`code-reviewer` subagent missing from registry, forcing orchestrator-as-D.5 fallback) that contributes to the same pattern of "orchestrator picks up the work because the delegate isn't there." That gap is upstream of the failures here; tagged in §6 as a related bug.

---

## 2. Diagnosis: TWO failures, ONE shared enabler

### 2.1 Issue A — Authorization-boundary leak (hand-fix substitution)

**Primary cause:** the memory entry `feedback_handfix_default` was authored for a narrow case ("paused-run findings, ~5–10 line code change with a clear pointer") under "broad administrative authority." It encoded the rule but **did not encode an enforceable boundary condition.** The orchestrator generalised it to normal-flow batch execution — a stale exception that propagated into the default branch.

**Evidence the migration is NOT necessary to explain this:** Codex pushed back precisely here. The repo's `_run_log.jsonl` shows `mechanism: "hand-fix"` events scattered across multiple runs, including older runs that pre-date the bash-dispatch migration. The migration may have eased the slide (shell-pipeline work makes inline edits feel like "one more step"), but Issue A is fundamentally a **policy semantics** failure, not an interface failure.

**Why "atomic dispatch fixes hand-fix" is wishful thinking** (Gemini's strongest hit on my draft synthesis): the orchestrator did not slip into hand-fix because the dispatch was multi-step. It chose hand-fix because it believed it was authorized to. Restoring an atomic call shape doesn't withdraw the authorization.

### 2.2 Issue B — Contract ergonomics drift (envelope schema mistakes)

**Primary cause:** the orchestrator must remember too many CLI/schema shapes across long sessions. `plan_ops.py` is 13641 lines. `SKILL.md` is 874 lines. Schemas are scattered across prose, fenced examples, schema files (e.g. `review_route_input_schema.json`), and per-subcommand `--help` output. Validation is post-hoc — a wrong field name costs one full round-trip per mistake.

**The cited mistakes do NOT all trace to bash-dispatch:**
- `codex_review_binding: true` short-circuit on `review-route` → orchestrator-built envelope, predates bash-dispatch.
- `finalize-execution-log` rows with `{task_id, status, commit_sha, reviewer_verdict, disagreement_tag}` instead of `{task, agent, commit, reviewer, verdict, notes}` → orchestrator-built JSON, predates bash-dispatch.
- `--plan-file` pointed at a directory → CLI-binding error, orthogonal to envelopes.

Only ONE of three cited mistakes (the dispatch-input shape) was added by the bash-dispatch migration. **The migration added a new envelope to a surface that was already growing.**

### 2.3 Shared enabler

The bash-dispatch migration's blurring of the dispatch boundary made the orchestrator's "I'm running multi-step plumbing" pattern more available. This **accelerates** both failures but **causes** neither. Failure 1 is a policy bug. Failure 2 is an ergonomics bug. The migration is a contributing factor to both, dispositive of neither.

---

## 3. Critical evaluation of the user's "deprecate plan_claude_dispatch" instinct

The user wrote: *"I suspect the plan_claude_dispatch path needs to be deprecated or revised so that we are using the subagent pathway again."*

**Disagree on full deprecation; agree on revision.** Reasons:

| Argument for full revert | Counter |
|---|---|
| "Subagent pathway felt cleaner cognitively" | True — and that cognitive shape can be partly restored without revert (see §5). |
| "Orchestrator now does too much plumbing" | Partly true. The 3-step pipeline (build → invoke → extract) IS more surface than `Agent()`. But the new surface is mostly schema-construction, not capability the wrapper provides. |
| Agent-registered dispatcher could shell to wrapper | Codex pushed back: that re-introduces session-snapshot AND adds an LLM hop where deterministic plumbing belongs. Wrong layer. |

**Concrete capabilities lost by reverting to `Agent()`:**

1. **Bounded JSON envelope replacing unbounded markdown report.** This is the migration's primary motive. Per-iteration orchestrator context drops materially — a real win that is hard to recover with `Agent()`.
2. **Env scrubbing + preflight guardrails + schema validation.** The wrapper enforces these as prerequisites to backend invocation. `Agent()` does not.
3. **Agent-registry session-snapshot bypass.** New subagents created mid-run are not visible to `Agent()` until next session; the wrapper sidesteps this entirely.
4. **Codex / Claude / Gemini symmetry.** All three wrappers share envelope shape, status vocabulary, and routing surface. Reverting Claude breaks symmetry.

The user's instinct correctly localizes the cognitive defect (orchestrator-side experience). The fix is to consolidate the orchestrator-side pipeline, **not** to revert the wrapper.

---

## 4. Critical evaluation of the prior Claude's remedy ranking

The prior Claude offered four remedies (A: hoist responsibilities text, B: consolidate to one subcommand, C: tighten hand-fix memory, D: trim phase detail). Both reviewers and the verified evidence push back:

- **A (hoist text):** cosmetic. Does not change the policy semantics. Demoted.
- **B (consolidate dispatch):** directionally right but the prior Claude oversold it as the structural fix. Both Gemini and Codex flag that bloating `plan_ops.py` (already 13641 lines) is the wrong direction; better to back the consolidated entry-point with a separate module, not stack onto the god-object. Also: hiding too much risks losing forensic visibility into wrapper failures; the orchestrator still needs `claude_dispatch_start/done/failed` boundaries.
- **C (tighten memory):** undersold by the prior Claude. This is the **primary** fix for Issue A. Memory tightening alone will close hand-fix substitution if paired with a commit-time guard.
- **D (trim phase detail):** legitimate but separate problem. Defer.

---

## 5. Refined remedy plan (tiered)

### Tier 1 — Close Issue A (authorization)
*Highest leverage. Memory + mechanical guard. ~1–2 hours total.*

- **R1.** Tighten the `feedback_handfix_default` memory entry to: *"Hand-fix is permitted ONLY when (a) the run is in `paused` state per `_run_lock.json`, AND (b) the user's explicit instruction in the immediately-preceding turn names the file and change. Any normal-flow `mechanism: hand-fix` event is a protocol violation."* Add a rationale line so the constraint sticks.
- **R2.** Add a guard inside `plan_ops.py commit-task` (or a new `--require-paused-state` pre-flight in `log-event`) that rejects `remediation_start mechanism: "hand-fix"` events whose preceding run-log slice does NOT contain an `awaiting_user` event paired with a recent user-authorization marker. Mechanical enforcement, no orchestrator judgment.
- **R3.** Add `plan_ops.py audit --orchestrator-protocol` (advisory) that scans `_run_log.jsonl` for prohibited normal-flow hand-fix events post-hoc. Drop into the existing `audit` subcommand.

### Tier 2 — Close the high-impact subset of Issue B (ergonomics)
*Targets the specific recurring schema mistakes, not the whole CLI surface. ~3–6 hours total.*

- **R4.** Add `plan_ops.py validate-envelope <subcommand>` — schema-validates stdin against the named subcommand's input schema, returns `{ok, errors[]}` without performing the action. Pre-flight cheap. Protocol becomes "validate-envelope before every state-changing call."
- **R5.** Replace the highest-error free-form envelope shape with a typed helper. Specifically: `finalize-execution-log --from-run-log` derives row data from run-log events for the run, eliminating `--rows-json` as a user-facing path. The orchestrator passes `--task-ids 002,005,...` and the subcommand reconstructs the canonical row shape from `commit_done` / `task_failed` / `disagreement` events. **This is Codex's idea, and it is excellent — it eliminates the entire envelope class for the worst offender.**
- **R6.** Add typed pre-checks at every CLI surface: `--plan-file` rejects directories for mutators (some commands already do this; audit for completeness). `commit-task --remediation-tag` rejects when no remediation event in run log. Etc.

### Tier 3 — Reduce ambient cognitive load (shared enabler)
*Reorganization. ~4–8 hours.*

- **R7.** Hide the dispatch pipeline behind a single entry point — but **NOT** as a new `plan_ops.py` subcommand. Add a thin `plan_claude_dispatch_once.py` (or extend `plan_claude_dispatch.py` itself with a `dispatch-task` subcommand) that does build + invoke + extract internally. The orchestrator sees one Bash call returning one structured outcome `{status, outcome, result, scope_violation, error}`. Critical constraints from Codex's pushback:
   - Still emit `claude_dispatch_start/done/failed` events at the orchestrator-visible boundary.
   - Still surface raw envelope on a flag (`--emit-envelope-tail`) for forensic debugging.
   - Do NOT mutate run state inside the consolidated call (timeout sizing, retry budgeting, parallel dispatch decisions stay in the orchestrator).
- **R8.** Reorganize `SKILL.md`: move "Orchestrator LLM responsibilities" (currently at line 780, in an 874-line doc) up near the Dispatch rules (~line 24). Move phase-specific detail (Phase 1.5.5 triage, D.2a.5, D.2a.6, D.4-rescue, etc.) to per-phase reference files loaded on demand. Resolve the line-17-vs-518 contradiction noted in §1. Goal: SKILL.md core under ~300 lines.
- **R9.** Cheat-sheet appendix: `plan_ops_envelope_skeletons.md` with literal copy-paste JSON for every state-changing subcommand. The protocol becomes "Read the cheat-sheet section before any state-changing call." Cheaper than re-reading a 13k-line script.

### Tier 4 — Address the related infra gap
- **R10.** Resolve the `code-reviewer` subagent registry miss called out in commit `0f2153a`'s body. Either register the subagent OR retarget D.5 dispatch to `plan-reviewer`. Without this, every Codex `needs-rework` that requires D.5 third-opinion has no escape valve, forcing orchestrator-as-D.5 — itself a hand-fix vector. Tracked separately.

---

## 6. What I am NOT recommending

- ❌ **Full revert to `Agent()`.** Loses bounded envelope, env-scrub, preflight, session-snapshot bypass.
- ❌ **An `Agent`-registered "dispatcher" subagent that internally shells to the wrapper.** Both reviewers flagged this as wrong layer (LLM hop where deterministic plumbing belongs; re-introduces session-snapshot for the dispatcher itself).
- ❌ **Bloating `plan_ops.py` with a new `dispatch` subcommand without a separate backing module.** Adds mass to a god object. Better: a sibling script or a separate module imported via thin facade.
- ❌ **Hoisting the orchestrator-responsibilities text as the primary fix.** Cosmetic. The structural fix is policy enforcement (R1 + R2), not text proximity.

---

## 7. Open questions / verification gates

1. Is there an existing `plan_ops.py` audit hook that could carry R3 cheaply, or does it need a new subcommand entry? (Likely the former — `audit` already exists.)
2. Does R5 (`--from-run-log`) require any new run-log fields, or are `commit_done` / `task_failed` / `disagreement` already sufficient? Check by enumerating the 6 required execution-log row columns against current run-log event fields.
3. R7's transition: do we keep the existing `build-claude-dispatch-input` + `plan_claude_dispatch.py run` pipeline as a back-compat path, or migrate atomically? Atomic risks breaking in-flight plans; coexistence risks doubling the cognitive surface during the transition.
4. R10 (D.5 escape valve) — does the fix belong in this remedy plan, or as its own follow-up? It is upstream of orchestrator-as-D.5 hand-fix vectors; deferring it may leave Issue A partially open even after R1/R2.

---

## 8. What I would do first

If forced to ship one thing, ship **R1 + R2** (memory tightening + commit-time guard). They close Issue A immediately with no refactor cost. Issue B continues to bleed round-trips, but its cost is incremental; Issue A's cost is structural — every hand-fix event undermines the executor's value proposition.

If shipping a second thing, ship **R5** (`finalize-execution-log --from-run-log`). It eliminates the most-cited recurring envelope mistake by removing the envelope from the orchestrator's surface entirely. High leverage, narrow scope, no architectural commitment.

R7 (dispatch consolidation) is the architectural fix and should be planned, not rushed. It changes the orchestrator's mental model of what dispatching looks like; that's worth a small dedicated plan with a Codex review pass before implementation.

---

## 9. Reviewer attribution

Where this report's claims diverge from my initial draft synthesis:

- **"Single root cause is forced"** — Codex (sharpest formulation) and Gemini (independently arrived at same conclusion) both rejected my single-cause framing. The two-failures-one-enabler split in §2 is theirs, not mine.
- **"Bloating `plan_ops.py` is suspect"** — Gemini called it "architectural bankruptcy"; Codex offered the same critique more soberly. Both pushed for separate-module backing of the consolidated entry-point.
- **"Issue A doesn't require the migration to explain"** — Codex's #1, decisive against my "blurred boundary causes hand-fix" claim.
- **"`finalize-execution-log --from-run-log`"** — Codex's R5, an idea I hadn't considered. Eliminates the worst envelope from the orchestrator's surface entirely.
- **"Atomicity does not fix authorization semantics"** — Gemini's strongest line; corrected my draft's wishful framing.
- **"Reintroducing Agent dispatcher is wrong layer"** — both reviewers, independently. Closes off the user's instinctive remedy without dismissing the underlying observation that motivated it.

Where I push back on the reviewers:

- Gemini's "architectural bankruptcy" framing of `plan_ops.py` is **overstated**. The 13641-line size reflects a deliberate single-CLI-facade design (one cheat-sheet, one help target, one audit surface). Splitting into multiple scripts trades token-cost-of-reading for coordination-cost-of-cross-script-consistency. R7's compromise (separate backing module under a thin `plan_ops.py` facade, OR a sibling script) is a measured response, not a wholesale architectural pivot.
- Codex's concern about R7 "hiding too much" is valid but bounded: keeping orchestrator-visible `claude_dispatch_start/done/failed` events and a `--emit-envelope-tail` debug flag preserves forensic visibility without sacrificing the atomic-call cognitive shape.

---

**Filed:** 2026-04-29 by orchestrator session against branch `main` at `e0f6e73` (post `feat(TASK-007): Phase D.1 Codex-review-failure fallback`). Cross-references: `feedback_handfix_default` (memory), `feedback_patience_and_no_bandaid` (memory), `feedback_injection_defense_at_wrapper` (memory), `docs/plans/SKILL_bash_dispatch_migration/PLAN_SKILL_bash_dispatch_migration.md`, `docs/analysis/Claude_Implement_Plan_context_bloat_response_20260420.md`.
