# TASK-016C binding re-review override — analysis report

- **Date:** 2026-04-17
- **Run ID:** `20260417T214309`
- **Plan:** `docs/plans/DUAL_AGENT_Plans/TASK-016_partial_agreement_and_narrow_remediation.md`
- **Task:** TASK-016C — D.2a.6 narrow-remediation retry path + commit trailers
- **Disposition:** **keep as-is** (committed against binding re-review verdict)
- **Override commit:** `12ad2c5` (trailer: `[remediation]`)
- **Path taken:** Claude-implement → Codex `needs-rework`/3 → D.5 `partial-agreement` split `load_bearing=[2]` / `dismissed=[0,1]` → narrow remediation (plan-implementer fallback, see §1 below) `success` → binding Codex re-review `needs-rework`/2 → awaiting-user → **user keep-as-is** → `commit-task --remediation-tag` (override).

---

## 1. Session-level plan adaptation (non-blocking, already documented in-line)

The D.2a.6 spec dispatches the narrow-remediation retry to `Agent(subagent_type: "plan-remediator", model: "opus")`. TASK-016B created that agent spec at `plugins/plan-executor/agents/plan-remediator.md` earlier in the same run (commit `7839d5d`), but Claude Code's Agent registry snapshotted at session start does not include newly-created agents — the runtime returned `Agent type 'plan-executor:plan-remediator' not found`.

Fallback applied: dispatched `plan-executor:plan-implementer` with a Phase B-narrow-remediation-equivalent prompt embedding `load_bearing_findings_json`, `dismissed_findings_json` (labeled "DO NOT fix — context only"), and `d5_summary`. This stand-in is functionally equivalent for the narrow-remediation attempt itself — the structural scope rule (file:line touch-only) was enforced via the prompt, not the subagent's frontmatter. The next fresh session will have `plan-remediator` registered and D.2a.6 will dispatch to it natively without fallback.

---

## 2. D.5 partial-agreement split (first review round)

Codex's initial review of the TASK-016C implementation raised 3 findings. D.5 (third-opinion reviewer, claude) split them:

| Idx | Status | Finding summary | D.5 rationale |
|---|---|---|---|
| 0 | dismissed | "XOR/requires-partnership checks are post-parse, not argparse" | False premise — the XOR constraints **are** in `add_mutually_exclusive_group()` at argparse layer. The "requires" checks live in post-parse `parser.error()` because argparse groups cannot express "X requires Y" dependencies. |
| 1 | dismissed | "content-validation lives in `cmd_commit_task` rather than argparse" | Duplicate of finding 2's narrow intent; subsumed. |
| 2 | **load-bearing** | Empty/non-integer tokens silently skipped in `--dismissed-finding-ids` parsing; fails contract `"X,Y,Z must be integers"`. | Real bug: silent skipping lets `',,1'` and `'1,x'` corrupt a partial-agreement commit without surfacing the operator error. |

D.5 summary (verbatim from run log):

> Finding 2 (silent empty-token skipping + missing argparse-level content validation) is load-bearing contract failure; findings 0-1 dismissed because the XOR/requires-partnership checks ARE at argparse-level, just content parsing lives in cmd_commit_task. Fix narrowly: move dismissed-ids content parsing into main() post-parse block using parser.error().

## 3. Narrow remediation applied

The plan-implementer fallback moved `--dismissed-finding-ids` content parsing from `cmd_commit_task` into the `main()` post-parse block so empty tokens (`','`, `'1,,3'`) and non-integer tokens (`'1,x'`) fail via `parser.error()` (exit 2) **before** any plan mutation or git I/O. Added three tests covering the previously-silent holes.

Diff scope: `plugins/plan-executor/scripts/plan_ops.py` (+18/−10), `tests/scripts/test_plan_ops.py` (+61). Test outcome: 271 passed, 1 pre-existing failure (unrelated baseline).

## 4. Binding re-review (Codex) — 2 findings, both disputed

Per SKILL.md §D.2a.6 step 4, the re-review is binding. Codex returned `needs-rework`:

### Finding 1 — `plan_ops.py:2860` (IMPORTANT) — **empirically wrong**

Claim:

> `--dismissed-finding-ids` is added to an argparse mutually-exclusive group with `default=""`. In argparse, an option with a non-`None` default in a mutually exclusive group is treated as present even when the user does not pass it, so a normal commit using only `--disagreement-tag` will now fail as conflicting with `--dismissed-finding-ids`.

**Why the claim is wrong:** argparse's mutex-group violation check fires only when both actions are **explicitly consumed from `argv`**. The `default` value is written into the namespace regardless, but the group's "seen" set tracks invocations, not namespace values. Verified empirically in this session:

```python
import argparse
p = argparse.ArgumentParser()
grp = p.add_mutually_exclusive_group()
grp.add_argument('--disagreement-tag', action='store_true')
grp.add_argument('--dismissed-finding-ids', default='')

p.parse_args(['--disagreement-tag'])
# → Namespace(disagreement_tag=True, dismissed_finding_ids='') ✓ OK

p.parse_args(['--dismissed-finding-ids', '0,1'])
# → Namespace(disagreement_tag=False, dismissed_finding_ids='0,1') ✓ OK

p.parse_args([])
# → Namespace(disagreement_tag=False, dismissed_finding_ids='') ✓ OK

p.parse_args(['--disagreement-tag', '--dismissed-finding-ids', '0,1'])
# → SystemExit 2: not allowed with argument --disagreement-tag ✓ expected fail
```

All four cases behave as expected. The bare `--disagreement-tag` path (used by D.5 `ship` / `ship-with-fixes`) is not regressed. `default="None"` is not required for correctness; the suggested fix would be a no-op refactor.

**If you want to double-check:** re-run the snippet above, or grep `tests/scripts/test_plan_ops.py` for `TestD3CommitArgparseMutualExclusion` (the committed tests exercise both the explicit-both-fail case and each single-flag-succeeds case).

### Finding 2 — `test_plan_ops.py:4500` (IMPORTANT) — real but minor

Claim: the new argparse tests don't explicitly cover "bare `--disagreement-tag` without `--dismissed-finding-ids`" as a regression-guarded happy path.

**Assessment:** the gap is real but non-regressive. Given that finding 1 is wrong, there is no live regression to guard against. The value of the suggested test is "defense in depth" — cheap to add, no harm, but not blocking.

**Optional follow-up (if you want to close the gap later):** add a test to `TestD3CommitArgparseMutualExclusion` that invokes `commit-task --disagreement-tag` (no `--dismissed-finding-ids`), asserts exit 0, and verifies the commit body contains a bare `[disagreement]` trailer with no `[narrow-remediation]` or `[disagreement: I,J,K]` line. Already implicitly exercised by the commit of TASK-016A (`d461e4b`, trailer `[disagreement]`) and TASK-016B (`7839d5d`, same).

## 5. Why override was the right call

1. **Binding rule cannot be relitigated by the orchestrator.** SKILL.md §D.2a.6 step 4 forbids a further retry regardless of the orchestrator's disagreement with the findings.
2. **User disposition is the only unblock path** under §D.2a.6 step 7's awaiting-user pause.
3. **Both findings are non-blocking:** finding 1 is a false positive (verified by direct `parse_args` probe); finding 2 is a test-coverage nice-to-have with no live failure mode.
4. **The implementation is correct and the narrow remediation addressed the real bug (finding 2 of the first-round review).** The behavior of the committed code matches the plan's acceptance criteria V6–V11.

## 6. Things to verify / fix later (if the user wants belt-and-suspenders)

- **Add the "bare `--disagreement-tag` happy path" regression test** described in finding 2's optional follow-up above. Cheap.
- **Consider tightening the D.5 reviewer prompt** to emit the D.5 summary with a verified-premise guard ("before dismissing, paste the exact code lines you think conflict with Codex's claim"). This would have caught Codex finding 0/1's false premise in the first round automatically. Not required; current logic works.
- **Re-verify `plan-remediator` is registered on the next fresh session.** A restart of Claude Code should pick up the agent file created by TASK-016B. If not, investigate why the registry snapshot excludes session-new agents.

## 7. Run-log anchors

All of the following are in `docs/plans/_run_log.jsonl` under `run_id: 20260417T214309`:

- `narrow_remediation_start` with full D.5 summary and load-bearing/dismissed counts
- `narrow_remediation_done` with `outcome: success`
- `review_start` / `review_done` for the binding re-review (`binding: true`)
- `awaiting_user` with `stage: post_narrow_remediation_review`, `codex_findings: [...]` (both re-review findings preserved verbatim), `d5_summary`, `dismissed_finding_indices: [0, 1]`, and `orchestrator_note` documenting the dispute.
- `run_end` with `outcome: paused` (note: the run ended paused before this override commit; the override is an out-of-run-envelope commit appended in the user's next turn).

## 8. Commit trailer semantics

The override commit `12ad2c5` carries `[remediation]` per D.2a.6 step 7's "keep as-is" spec. This **loses the `[narrow-remediation]` + `[disagreement: 0,1]` trailers** that a clean D.2a.6 success path would have produced. Rationale: `--narrow-remediation-tag` is mutex with `--remediation-tag` at the argparse layer, and the spec directs `--remediation-tag` for override commits. The D.2a.6 context is preserved in the run log (see §7) and in the commit-body diff summary, which names both the path and the override reason.

If you ever want `git log --oneline` to distinguish narrow-remediation overrides from full-rework overrides without reading the body, we could add a fourth tag (`[narrow-remediation-override]`) — but that's scope for a future plan, not a bug in what we just committed.
