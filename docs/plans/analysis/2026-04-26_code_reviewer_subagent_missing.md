# Analysis — `code-reviewer` subagent is referenced but not registered

**Date:** 2026-04-26
**Discovered during:** SKILL_bash_dispatch_migration run `20260426T125800` D.5 escalation on TASK-003
**Status:** unaddressed; orchestrator working around via "hand-D.5"
**Follow-up scope:** separate plan (~1 agent manifest + SKILL.md/templates retarget + tests)

---

## Summary

`SKILL.md` and `dispatch-templates.md` reference a `code-reviewer` subagent in multiple Phase D paths (D.1, D.5, D.4-rescue re-review, claude_only re-review). No agent of that name exists in this environment — neither in the plan-executor plugin's `agents/` directory nor in `~/.claude/agents/`, and it is not a Claude Code built-in.

Every Phase D path that calls `Agent(subagent_type="code-reviewer", …)` fails to dispatch. The `/implement-plan` orchestrator falls back to acting as the third opinion itself ("hand-D.5") — making the load-bearing-vs-dismissed call directly and writing the disposition rationale into the `commit_done` `findings[].disposition_reason` for audit. This works but bypasses the design's three-voice review (implementer + Codex + independent code-reviewer) and concentrates judgment in the orchestrator.

## Code-grounded evidence

References to `code-reviewer` in the plan-executor source:

| File | Line | Context |
|---|---|---|
| `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` | 588 | `## Phase D-Claude — code-reviewer on Codex work` (full template ~25 lines) |
| `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` | 614 | `## Phase D.5 — code-reviewer third opinion (§8.4 escalation)` (full template ~80 lines) |
| `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` | 716 | D.2a.5 callout: *"the third-opinion code-reviewer independently agreed"* |
| `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` | 776 | D.2a.6 callout: *"third-opinion code-reviewer returned partial-agreement"* |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | 655 | `claude_only=true` re-review variant: *"would use the code-reviewer Agent"* |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | 689 | D.4 rescue re-review: *"code-reviewer Agent under claude_only=true"* |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | 770 | `claude_only=true` D.2a collapse note |
| `plugins/plan-executor/skills/implement-plan/run-log-schema.md` | 17, 28, 31 | Run-log schema entries reference `code_reviewer_verdict` and `{ship | ship-with-fixes | needs-rework}` vocab |
| `plugins/plan-executor/agents/plan-remediator.md` | 8, 30, 36 | Inputs documented from "the third-opinion code-reviewer" |
| `plugins/plan-executor/agents/plan-review-triage.md` | 8 | *"plan-level sibling of the §8.4 / Phase D.5 task-level code-reviewer third opinion"* |

Available agents in this environment (source repo):

```
plugins/plan-executor/agents/
├── content-sanitizer.md
├── plan-analyst.md
├── plan-author.md
├── plan-implementer.md
├── plan-remediator.md
├── plan-review-triage.md
└── plan-reviewer.md       ← Sonnet, plan-stage review (CLAUDE_ONLY_FIX TASK-002)
```

`~/.claude/agents/` — does not exist (no user-defined agents).

Claude Code built-in agent set (per the system prompt's Agent tool description): `general-purpose`, `Plan`, `Explore`, `claude-code-guide`, `statusline-setup`, plus plugin-namespaced (`codex:codex-rescue`, `plan-executor:*`). **No `code-reviewer`.**

## Verdict vocabulary mismatch

`code-reviewer` is documented to emit `ship | ship-with-fixes | partial-agreement | needs-rework` (4 verdicts) per `dispatch-templates.md:614+`. The closest existing agent is `plan-executor:plan-reviewer`, which emits `approved | approved-with-notes | needs-replan` (3 verdicts) per the §Phase 1.5 binding-second-pass contract. The two are not drop-in compatible — `plan-reviewer` is plan-stage (reads schedule JSON), not code-stage (reads diff hunks against findings), and the verdict ladder differs.

## Why earlier runs appeared to succeed

- **TOPO_RESPECT_FIX** (run `20260425T231744`): had 3 disagreements, all dismissed as Codex over-flag. The orchestrator acted as hand-D.5 silently — `disagreement_tag: true` on commits, no `code_reviewer_verdict` field.
- **CLAUDE_ONLY_FIX** (run `20260426T010342`): 0 disagreements — TASK-001/002/003 all clean on first review, never hit D.5.
- **POSTMORTEM_FIXES** (run `20260426T032452`): 6 D.5 escalations all "dismissed/spec-deference" per the run summary. Same pattern — orchestrator dismissed inline; no real code-reviewer dispatch occurred.
- **PHASE_D_STATE_MACHINE resume** (run `20260426T112743`): 4 disagreements; orchestrator acted as hand-D.5; one D.5 dismissal explicitly cited *"D.5: stub OK per AC's stubbed sanitizer bullet"*.
- **prohibit_silent_revert** (run `20260426T060902`): 4 disagreements; orchestrator hand-D.5 throughout (e.g., TASK-001 `disposition_reason` cites *"D.5 dismissed: argparse required=True empirically short-circuits to exit-2 stderr usage"*).

In every case the orchestrator made the judgment without dispatching to a real code-reviewer agent — successfully, because (a) the disagreements were on relatively narrow points and (b) the orchestrator had the relevant context. **The current SKILL_bash_dispatch run is the first one where the orchestrator explicitly named the missing agent in its halt report rather than silently deciding** — this is a behavioral improvement (transparency), not a regression.

## Implication for current runs

**SKILL_bash_dispatch_migration (in flight as of writing):**
- TASK-005 was committed via hand-D.5 with both findings dismissed (Codex's review-flip-flop between rounds was correctly identified as not load-bearing). `[disagreement]` tag preserved.
- TASK-006 and TASK-007 are queued. If they hit Codex `needs-rework`, the orchestrator will continue using hand-D.5. The pattern is established.

**prohibit_silent_revert resume (TASK-009):**
- Single task remaining. If Codex needs-rework, hand-D.5 will adjudicate. Low-risk — TASK-009 is documentation-leaning per the plan.

**narrow_run_filter_ids resume (TASK-003, 004, 005):**
- Three small tasks. Same hand-D.5 fallback if needed. Low risk.

**PLAN_GEMINI_INTEGRATION resume (TASK-005..008):**
- Four tasks of substantive integration work. Higher likelihood of Codex disagreements requiring D.5. Hand-D.5 quality matters most here — the orchestrator will need to be careful on findings about gemini wrapper internals.

**Cross-cutting risks:**

1. **Hand-D.5 quality is bounded by the orchestrator's context.** A dedicated `code-reviewer` agent has its own focused system prompt for diff-vs-finding adjudication. The orchestrator pulls in the full /implement-plan skill context, plan markdown, run log, etc. — broader but shallower on the specific diff. For narrow/well-defined findings (most of what we've seen), this is fine. For findings that turn on subtle code semantics (e.g., race conditions, async ordering, security boundary violations), hand-D.5 may miss things.

2. **No three-voice quorum.** The design intent of D.5 is *independence* — implementer (one voice), Codex (second voice), code-reviewer (third, neutral voice). Hand-D.5 collapses voices 2 and 3 since the orchestrator already chose to dispatch Codex and is now adjudicating its own dispatch's output. The risk is amplified when Codex's finding shape is the orchestrator's blind spot too.

3. **`partial-agreement` verdict is unreachable.** The 4-verdict code-reviewer vocab includes `partial-agreement` (some findings load-bearing, others dismissed). Hand-D.5 has no structured way to emit this; in practice the orchestrator produces a flat dismissal/acceptance per finding, then commits with `[disagreement]` and a finding-level disposition. This works but the D.2a.6 narrow-remediation path (which is keyed on `partial-agreement` + `dismissed_findings[]` from D.5) becomes structurally unreachable.

4. **Run-log schema includes a `code_reviewer_verdict` field that nothing populates.** Audit consumers expecting this field (e.g., the postmortem analyzer or any downstream metrics) see `null` or absence. Not a correctness bug — just an audit-trail gap to know about.

## Recommended fix (for the follow-up plan)

**Author `plugins/plan-executor/agents/code-reviewer.md`** as a Sonnet-tier agent whose contract mirrors the Phase D.5 template at `dispatch-templates.md:614+` precisely:

- **Inputs:** task block, implementer diff (or pointer), Codex envelope (verdict + findings + summary), prior reviewer envelope on D.4-rescue re-review.
- **Output schema:** `{verdict ∈ {ship, ship-with-fixes, partial-agreement, needs-rework}, load_bearing[indices], dismissed[indices], summary}` — already declared in `scripts/codex_review_schema.json` reuse path or a sibling schema.
- **System prompt:** focused on diff-vs-finding adjudication; explicitly instructed not to introduce new findings beyond Codex's set; required to map each Codex finding to load-bearing or dismissed.
- **Tools:** read-only on the repo (Read, Grep), no Bash, no Edit.

Then update SKILL.md and dispatch-templates.md to use `subagent_type: "plan-executor:code-reviewer"` (namespaced) at the eight call sites listed in the table above. Add an integration test that drives a fixture-Codex-disagreement through the D.5 path end-to-end.

**Estimated effort:** 1 agent manifest (~8K chars) + 8 call-site edits in SKILL.md/templates + 1 integration test + plan-author pass to tighten the contract. Likely a 4–6 task plan.

**Until then,** hand-D.5 is the de-facto third-opinion mechanism. It has worked across 5+ runs without a wrong call so far, but the failure modes above (subtle code-semantic findings, partial-agreement unreachability) should be flagged when reviewing any commit carrying `[disagreement]` from a hand-D.5 disposition.
