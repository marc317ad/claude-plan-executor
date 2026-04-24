# Completed-Work Preservation Principle for `/implement-plan`

**Status:** DRAFT v2 — revised 2026-04-24 after Codex (`needs-rework`) + Gemini (`ship-with-fixes`) cross-review. v1 in git history (uncommitted).
**Date:** 2026-04-24
**Author:** Claude (Opus 4.7) on behalf of @marc317ad

---

## 1. Goal

Prohibit any agent, subagent, wrapper, or orchestrator step from **silently reverting, restoring, discarding, or rolling back work that an implementer produced** without first surfacing the situation to the user and receiving an explicit instruction. When a downstream obstacle (review failure, commit failure, hook failure, scope violation, binding-review fail-fast, **post-batch out-of-scope reconciliation**) lands on already-implemented work, the default response must be to **halt with an awaiting-user pause** and prefer an in-place fix over a destructive revert — regardless of how "out of scope" the necessary follow-up appears.

Today the orchestrator already does this correctly on the D.2a.5 / D.2a.6 retry-failure paths (SKILL.md hard rule line 811). The gaps below are where the same principle must be extended.

---

## 2. The Principle (drop-in text for SKILL.md `## Rules`)

> **Completed-Work Preservation Principle.** No agent, subagent, wrapper, or orchestrator step may silently revert, `git restore`, `git reset --hard`, delete, `unlink`, or otherwise discard implementer work product — defined as **any non-empty diff against the run's `starting_sha` present in the working tree (tracked or untracked) inside the task's declared `Files:` set OR the wrapper-observed `out_of_scope_*` sets** — without first (a) surfacing the situation to the user via an `awaiting_user` event + paused `run_end` and (b) receiving an explicit user instruction in the next conversation turn. The default response to a downstream obstacle on such work is **halt-with-pause**, not auto-`fail-task`. When a follow-up fix is required, the orchestrator MUST first attempt an in-place patch via `plan-remediator` (touch-only) before considering reversion, even when the required fix appears "out of scope" of the original task — preserving implementer effort takes precedence over scope tidiness. Reversion is allowed only on (i) explicit user instruction in the next turn, (ii) commit-time guard failures whose rollback is bounded to staging metadata (`git reset HEAD` + plan/roster restore, see §G7 below), or (iii) implementer-failure paths where the working tree contains **no** non-empty diff (nothing to preserve). Auto-revert in any other path is a protocol violation.

Wording notes (Gemini loophole pass):
- "implementer work product" is defined by **diff against `starting_sha`**, not by implementer outcome string. A `failure` outcome that left a non-empty diff is still preserved.
- "silently" is operationalized: a single log line does NOT satisfy "surface to user". The user-surfacing requires both an `awaiting_user` event AND a paused `run_end` AND release of the run-lock so the next turn can act.
- "out of scope" is explicitly named: the `out_of_scope_tracked` / `out_of_scope_untracked` sets from the Codex wrapper envelope ARE work product under this principle.

---

## 3. Gap analysis — where the current protocol violates the principle

Verified against `/mnt/d/claude-plan-executor/plugins/plan-executor/`.

| # | Path | File:line | Current behavior | Violation? |
|---|---|---|---|---|
| **G10** | **`reconcile_batch` out-of-scope handler** (NEW — Codex Q1) | `plan_ops.py:1882–1903, 1942–1958` | At batch-join barrier, observed out-of-scope tracked paths get `git restore --staged --` + `git restore --`, untracked paths get `unlink()`. Task marked `scope_violation_reconciled` and made ineligible for review/commit. | **Yes — flagship case.** This IS the "out-of-scope writes get destroyed" path the user explicitly called out. Highest priority. |
| G2 | **Phase D.4** (review-stage failure) | `SKILL.md:755–771` | Auto `fail-task --stage review` → `git restore` of files + status flip + cascade | **Yes — clearest pure-G2 violation.** Implementation succeeded; only the downstream review/commit seam failed. |
| G1 | **Phase C** (implementer failure) | `SKILL.md:551–575` | Auto `fail-task --stage implement` → `git restore` of touched files + status flip + cascade `blocked` | **Yes if implementer left non-empty diff** (per §2 wording). Empty diff → fail-task is fine. |
| G3 | **D.2a binding-fail-fast** | `SKILL.md:626, 137–138, 666`; tests `test_plan_ops.py:8513–8524` | `--codex-review-binding` set → immediate `fail-task` on Codex `needs-rework`, no D.5, no retry. CLI help, D.2a.6 exemption phrasing, AND a test all assert this contract. | **Yes.** Reinterpretation requires same-PR updates to all four sites. |
| G4/G5 | **D.2b retry paths** | `SKILL.md:698–705` | Retry implement failure / re-review needs-rework → D.4 (`fail-task`) | **Yes** (inherits G2). Auto-fixed when G2 lands. |
| G6 | **`cmd_fail_task`** call-site auditing | `plan_ops.py:4185–4301` | No call-site audit field; any caller can trigger destructive `git restore` + untracked `unlink` | **Indirectly.** Drift risk for future contributors. Hardening, not a violation today. |
| G7 | **`cmd_commit_task`** rollback path | `plan_ops.py:3739–3744` | On `git commit` failure: `git reset HEAD --` + restore plan text + restore roster | **No** — work preserved in working tree; only metadata rolls back. Correct as-is. |
| G8 | **Codex dispatch wrapper** internal cleanup | `plan_codex_dispatch.py` | Restores its OWN pre-dispatch delta on failure within a single dispatch | **No** — bounded by design. Principle applies to orchestrator, not single-dispatch self-cleanup. |
| G9 | **Plan-stage halts** | `SKILL.md:264–302, 393–436` | Plan-author / re-analyst / re-Codex-review second failure → halt | **No** — plan markdown only; code never touched. |

**Net:** six real gaps (G1, G2, G3, G4, G5, G10) plus one hardening (G6). G7, G8, G9 left alone.

---

## 4. Edits — file by file

### 4.1 `plugins/plan-executor/skills/implement-plan/SKILL.md`

#### 4.1.a Add the principle to `## Rules` (around line 811)

Insert §2 text as a new top bullet of `## Rules`. Reword the existing line-811 D.2a.5 rule into a sub-bullet: "specific instance of the Completed-Work Preservation Principle…".

#### 4.1.b Define a shared **Awaiting-user pause control-flow** subroutine

Insert as a new subsection between Phase D.4 and Phase E.

**Important (Codex Q2):** the subroutine factors out **control flow only**. Per-stage payload contracts are intentionally distinct and stay declared at each call site. The subroutine spec lists the call-site contracts as a table:

| Call site | `stage` value | Required payload fields (additional to dirty_files) |
|---|---|---|
| D.2a.5 second-review fail | `post_remediation_review` | `codex_findings[]`, `d5_summary` |
| D.2a.5 retry-implement fail | `post_remediation_implement` | `retry_outcome`, `diagnostics`, `reversion_guidance` |
| D.2a.6 second-review fail | `post_narrow_remediation_review` | `codex_findings[]`, `d5_summary`, `dismissed_finding_indices[]` |
| D.2a.6 retry-implement fail | `post_narrow_remediation_implement` | `retry_outcome`, `diagnostics`, `reversion_guidance` |
| **G1 (Phase C, non-empty diff)** (NEW) | `post_implement_failure` | `implementer_outcome`, `diagnostics`, `reversion_guidance`, `nonempty_diff_files[]` |
| **G2 (Phase D.4 after rescue)** (NEW) | `post_d4_rescue_failed` | `reviewer_findings[]`, `rescue_attempt_outcome`, `rescue_diagnostics` |
| **G2 (Phase D.4 commit-seam)** (NEW) | `post_commit_seam_failure` | `commit_seam_reason`, `seam_diagnostics` |
| **G3 (binding-mode block)** (NEW) | `post_binding_block` | `codex_findings[]`, `binding_flag` |
| **G10 (reconcile out-of-scope)** (NEW) | `post_reconcile_out_of_scope` | `out_of_scope_tracked[]`, `out_of_scope_untracked[]`, `task_id`, `wrapper_envelope_summary` |

The subroutine itself does the same five things at every call site:
1. `log-event awaiting_user --fields-json '{"task_id":"NNN","stage":"<see table>",...,"dirty_files":[...]}'` (per-site payload merged in)
2. `finalize-execution-log --outcome paused --ending-sha "$(git rev-parse HEAD)"`
3. `log-event run_end --fields-json '{"outcome":"paused","paused_on_task":"NNN",...}'`
4. Skip End-of-run housekeeping; print failure envelope; release the run-lock
5. **Never** call `fail-task`; **never** `git restore`; **never** mutate plan-status to `failed`

#### 4.1.c Modify Phase C (lines 551–575) — gap G1

```
Phase B implementer outcome ≠ success
├─ Probe: git diff --quiet --no-ext-diff <starting_sha> -- <touched ∪ Files:>
│  ├─ exit 0 (no diff) → fail-task (current behavior — nothing to preserve)
│  └─ exit ≠ 0 (non-empty diff) → mark task **Status: paused** (NEW status, see §4.1.h);
│     call Awaiting-user pause with stage="post_implement_failure" and
│     payload as per §4.1.b table.
└─ Cascade-blocked on dependents only fires on the fail-task branch
   (i.e., when user instructs revert in next turn, OR empty-diff auto-fail).
```

#### 4.1.d Modify Phase D.4 (lines 755–771) — gap G2

D.4 stops being a destructive seam. New flow:

```
D.4 trigger (review-stage failure on completed implementation)
├─ If trigger = commit-seam failure (commit_safe_gate_failed,
│  v_check failure, post-commit gate mismatch):
│  └─ mark task **Status: paused**; Awaiting-user pause stage=post_commit_seam_failure.
│     (No remediator attempt — failure isn't in code, it's at the staging seam.)
└─ If trigger = reviewer-findings (D.2a.5/D.2a.6 didn't apply, but
   Phase D detected an issue before commit):
   └─ Single-shot D.4 rescue (see §4.1.i).
```

#### 4.1.e Modify D.2a binding-fail-fast (line 626) — gap G3

Reinterpret `--codex-review-binding`: the flag is binding for the **commit decision** (cannot be overridden by D.5), but does NOT authorize silent destruction of the implementation.

- Replace immediate `fail-task` with: mark task **Status: paused**; Awaiting-user pause stage=`post_binding_block`.
- Add new escape-hatch flag `--codex-review-binding-destructive` (back-compat) that preserves the legacy fail-fast destroy. Default: pause.

**Same-PR doc/test updates required (Codex Q4):**
- `SKILL.md:137–138` — CLI help string
- `SKILL.md:626` — D.2a routing prose
- `SKILL.md:666` — D.2a.6 exemption phrasing
- `tests/scripts/test_plan_ops.py:8513–8524` — `test_section_documents_binding_mode_exemption`. The test asserts presence of `codex-review-binding` plus one of `skip|NO D.2a.6|not entered`. Replace the assertion to reflect new wording: "binding mode pauses for user instruction; D.2a.6 is not entered".

#### 4.1.f Modify D.2b retry paths (lines 698–705) — gaps G4, G5

D.2b currently routes both `retry_implement_failed` and re-review `needs-rework` to D.4. Once §4.1.d lands, D.2b is automatically fixed; explicitly cross-reference §4.1.d so the route is unambiguous.

#### 4.1.g Cross-reference cleanup

In every phase that previously documented destructive auto-revert, add: "See **Completed-Work Preservation Principle** in §Rules — destructive paths require explicit user instruction in the next turn."

#### 4.1.h NEW: `paused` plan-status (state-machine)

This addresses the Codex Q5/Q6 + Gemini #1 scheduler-paralysis finding.

Add `paused` as a first-class plan-status alongside `pending | in-progress | done | failed | blocked`.

**Transitions:**
- `in-progress → paused` — set by orchestrator when entering Awaiting-user pause for any G1/G2/G3/G10 path.
- `paused → done` — when user instructs `commit-task` in next turn.
- `paused → failed` — when user instructs `fail-task` (`--stage <pause-origin> --reason "user-instructed"`).
- `paused → in-progress` — when user instructs hand-fix retry (re-dispatch).

**Effects on subsystems:**
- **`batch-next`** (`plan_ops.py:2738–2754, 2805–2812`): a `paused` task is **not** in `done|failed`, so under today's logic it would be `remaining` and possibly `ready`. Add a third filter: tasks with status `paused` are excluded from `remaining`. Same for `blocked`. (Today `blocked` is also missing from this filter — separate audit-fix; bundle in same PR.)
- **`block-dependents`**: paused tasks NOT cascaded. Only `failed` tasks cascade.
- **`update-plan-header`**: a run that ends with any task in `paused` reports outcome `paused` (already supported via `--outcome paused` on `finalize-execution-log`); per-child header becomes `partial` if any task is paused or failed.
- **`lint-plans`**: a `paused` status without a matching `awaiting_user` run-log event for the same task in the same run is a hand-edit drift; flag it.
- **`gates`**: no change.
- **`mutate_task_status`** in `plan_ops.py`: extend the allowed-status enum (search `_PLAN_STATUS_VALUES` or equivalent).

**Resume protocol:** When a user re-runs `/implement-plan` on a plan with paused tasks, Phase 0 preflight surfaces them (new `paused_tasks[]` field in preflight output). The orchestrator does NOT auto-resume — the user must explicitly instruct what to do with each paused task in their conversation turn.

#### 4.1.i NEW: Phase D.4 rescue (single-shot, terminal) — Codex Q3 + Gemini #2

D.4 rescue is **separate machinery** from D.2a.5/D.2a.6. It does NOT reuse D.5 adjudication, narrow-remediation tags, or `[remediation]` commit trailers.

**Spec:**

```
D.4 rescue trigger: reviewer findings on a Phase D path that didn't already
go through D.2a.5/D.2a.6 (i.e., a fresh post-commit-attempt review failure
or a review-stage seam that surfaced new findings).
```

1. Log `d4_rescue_start {task_id, reviewer_findings_count, trigger_reason}`.
2. Dispatch `plan-remediator` with the **NEW Phase D.4-rescue dispatch template** (§4.2). Findings are passed under a NEW key `rescue_findings[]` (NOT `load_bearing_findings[]` — those imply a D.5 split that didn't happen). `dismissed_findings: []` is passed as empty literal. `d5_summary` is OMITTED. The remediator's scope rule applies to `(file, line)` of `rescue_findings[]`.
3. Classify retry: `success` → step 4. `scope-violation | failure` → step 5b.
4. Re-dispatch the original reviewer (binding — no further retry, no D.5). Route:
   - `clean | minor-findings | ship | ship-with-fixes` → Phase D.3 commit with **NEW `--d4-rescue-tag`** (commit body line `[d4-rescue]`). Argparse XOR rules: `--d4-rescue-tag` is mutually exclusive with `--remediation-tag` and `--narrow-remediation-tag`; `--disagreement-tag` may co-occur.
   - `needs-rework` → step 5a (TERMINAL — no second rescue).
5. Halt branches:
   - **5a (re-review still fails after rescue):** mark task **Status: paused**; Awaiting-user pause stage=`post_d4_rescue_failed`.
   - **5b (rescue dispatch failed or scope-violation):** mark task **Status: paused**; Awaiting-user pause stage=`post_d4_rescue_failed`, payload includes `rescue_attempt_outcome` distinguishing the two sub-reasons.
6. Log `d4_rescue_done {task_id, outcome, halt_reason?}` before entering the pause.

**Hard rule (drop into §Rules):** "D.4 rescue is single-shot. If the post-rescue re-review fails, halt for user — do NOT dispatch a second rescue, do NOT chain into D.2a.5/D.2a.6 (those are pre-D.4 paths)."

#### 4.1.j NEW: Unattended-revert policy (Gemini #3)

Add a Phase 0 preflight argument `--unattended-revert-policy <pause|fail-fast|preserve-only>`:

- `pause` (default in interactive mode) — full new behavior; pauses on G1/G2/G3/G10.
- `fail-fast` — legacy behavior; auto-fail-task on G1/G2/G3/G10. For cron/CI that prefer fast-fail.
- `preserve-only` — pauses on G2/G3/G10, but on G1 with non-empty diff, LOG the diff to a salvage-branch ref and still fail-task. Compromise mode for unattended runs that want to keep diagnostic data without stalling.

**TTY auto-detection:** If `stdin.isatty()` is False AND the flag is absent, `preflight` halts with `errors[*].code = "unattended-revert-policy-required"`. Forces explicit decision for cron/CI.

`preflight --json` adds field `unattended_revert_policy`; orchestrator pins for the run.

---

### 4.2 `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`

Three additions:

- **Phase B-rework template** (~line 416): keep the existing "prior attempt is still in the working tree — it was NOT reverted" line. Add: "Per the Completed-Work Preservation Principle, you MUST NOT `git restore`, `git reset --hard`, delete files, or otherwise discard the prior implementation. If you cannot complete the rework without doing so, return `outcome=blocked` with rationale; the orchestrator will halt for user instruction."
- **Phase B-narrow-remediation template** (~line 466): same addition, adapted to touch-only contract.
- **NEW Phase D.4-rescue template:** explicitly distinct from B-rework and B-narrow. Inputs: `rescue_findings[]` (note key name — NOT `load_bearing_findings[]`), `dismissed_findings: []` (literal empty), reviewer-source, task-block. Output contract: same as plan-remediator's report, with the `**Dismissed findings noted:**` section literally `(none — D.4 rescue does not carry dismissed findings)`.

---

### 4.3 `plugins/plan-executor/agents/plan-remediator.md`

Three additions:

- **Why this agent exists** preamble: cite the Completed-Work Preservation Principle.
- **Scope rule (line 37):** if the load-bearing findings request a fix that genuinely cannot be made within the touch-only region, return `scope-violation` with a NEW `**Proposed-fix-scope:**` report section listing files/lines that *would* need broader edits. The orchestrator surfaces this in the awaiting-user pause; do NOT silently widen scope.
- **NEW input mode**: the agent now accepts EITHER `load_bearing_findings[] + dismissed_findings[] + d5_summary` (D.2a.6 path) OR `rescue_findings[] + dismissed_findings: [] + (no d5_summary)` (D.4 rescue path). Document the difference. The `**Dismissed findings noted:**` section is required in the D.2a.6 mode; in D.4-rescue mode it is the literal "(none)" line.

---

### 4.4 `plugins/plan-executor/agents/plan-implementer.md`

Add a clause near the report-shape section: "Per the Completed-Work Preservation Principle, on Phase B-rework you MUST NOT `git restore`, `git reset --hard`, delete files, or otherwise discard the prior implementation. If your rework cannot succeed without doing so, return `outcome=blocked` with `reversion_guidance:` describing what you would need; the orchestrator will halt for user instruction. Phase B (initial implementation) is unconstrained — the principle applies to rework where prior work exists."

---

### 4.5 `plugins/plan-executor/scripts/plan_ops.py` — gap G6 (REQUIRED, not audit-only)

Per Gemini's recommendation (and on reflection this is right), make `--authorization-source` **required** for `cmd_fail_task` (lines 4185–4301).

- Allowed values: `phase-c-empty-diff`, `phase-d4-commit-seam`, `user-instruction`, `unattended-fail-fast`.
- argparse: `required=True`. Missing → exit 1 with `errors[*].code = "authorization-source-required"`. This fail-fasts any future contributor who adds a `fail-task` call without thinking through the principle.
- The recorded value lands in the `failed` run-log event under key `authorization_source` for postmortem audits.
- **Migration:** every existing call site in SKILL.md is updated in the same PR to pass `--authorization-source`. There is no `legacy` default — the CLI is a hard wall.
- **Test:** `tests/scripts/test_plan_ops.py` gets a new test asserting the field round-trips and that missing-flag fails.

---

### 4.6 `DUAL_AGENT_PLAN_EXECUTOR.md` (design doc)

Propagate the principle into the design doc. Cross-reference SKILL.md.

---

### 4.7 NEW: `reconcile_batch` (`plan_ops.py:1882–1958`) — gap G10

Replace the silent destroy with halt-with-pause.

**New behavior:**

For each batch envelope with `out_of_scope_observed == true`:

1. Do NOT `git restore` tracked out-of-scope paths.
2. Do NOT `unlink` untracked out-of-scope paths.
3. Mark the task **Status: paused** in its plan file.
4. Emit a per-task entry in the result with `outcome: "out_of_scope_paused"` (NEW outcome value, alongside `no_op | scope_violation_reconciled | reconciliation_failed`).
5. The orchestrator, on receiving any `out_of_scope_paused` result, calls Awaiting-user pause stage=`post_reconcile_out_of_scope` for that task. The other tasks in the batch proceed to review/commit independently (the pause is per-task, not per-batch).
6. The user's next-turn options for an out-of-scope-paused task:
   - **"widen plan"** → user edits the task's `Files:` to include the out-of-scope paths, then re-runs `/implement-plan` on the paused task (resume path); the writes are preserved and now in-scope.
   - **"in-place fix"** → user dispatches `plan-remediator` (touch-only) to revise the task's intent within the original `Files:` and revert the out-of-scope writes.
   - **"keep & commit"** → user instructs `commit-task` with an `--out-of-scope-override-rationale`.
   - **"revert"** → user instructs `fail-task --authorization-source user-instruction --stage reconcile`.

**Backward compatibility:**
- Add a new flag to the orchestrator's `reconcile-batch` invocation: `--out-of-scope-policy <pause|reconcile-and-revert>`. Default: `pause` (interactive). Under `reconcile-and-revert` (legacy), behavior is the current destroy. The Phase 0 `--unattended-revert-policy` controls the default at preflight time.
- `reconciliation_failed` outcome (residual dirt after a reconcile-and-revert attempt) still hard-halts the run, same as today.

---

## 5. Out of scope (explicit non-goals)

- No change to the Codex dispatch wrapper's delta-bounded cleanup (G8). That is correctly bounded to a single dispatch.
- No change to `cmd_commit_task`'s `git reset HEAD` + plan/roster restore (G7). That preserves work product.
- No change to plan-stage halts (G9). Plan markdown is metadata; code never touched.
- No retroactive change to already-completed runs.
- No change to dry-run mode (never commits or reverts).
- The cascade-`blocked` mechanism itself is unchanged — only the *trigger condition* shifts (now post-user-instruction on the pause paths instead of automatic).

---

## 6. Decision points / open questions for user (REVISED after reviews)

| # | Question | Author recommendation | Reviewer signal |
|---|---|---|---|
| 1 | G1 empty-diff probe: auto-fail-task allowed when working tree is clean after impl failure? | **Yes** — nothing to preserve. | Both reviewers OK. |
| 2 | G3 binding-mode: ship `--codex-review-binding-destructive` back-compat flag? | **Yes** — preserves cron/CI workflows. | Codex flagged the test/doc co-update; addressed in §4.1.e. |
| 3 | G6 `--authorization-source`: required at CLI or audit-only? | **Required (Gemini's call)** — fail-fast on missing. Closes future-regression vector. | Gemini explicit. Codex didn't position. **User decision needed if you disagree.** |
| 4 | G10 reconcile-batch out-of-scope: which user options to expose? | All four (widen-plan / in-place-fix / keep-and-commit / revert). | Neither reviewer critiqued; ship as proposed. |
| 5 | `paused` status: introduce as first-class plan-status, OR make `batch-next` run-log-aware? | **First-class status** — simpler, lower-impact, self-documenting. | Both reviewers signaled the gap; first-class status is the cleaner fix. |
| 6 | D.4 rescue tag: dedicated `[d4-rescue]` commit trailer? | **Yes** — Codex Q3 directly. Distinct event types + trailer. | Codex required. |
| 7 | `--unattended-revert-policy` default for non-TTY: refuse-without-flag, or default `fail-fast`? | **Refuse-without-flag** — forces explicit decision per environment. Loud failure beats silent destruction. | Gemini wanted "TTY detection vs required flag formalized"; refuse-without-flag does both. |
| 8 | Where does the principle live, canonically? | SKILL.md `## Rules`. Design doc + agent specs cross-reference. | Both reviewers OK. |
| 9 | Implementation order: G6 (audit flag) first, paused-status next? | **Yes** — Gemini's recommendation. Telemetry before refactor. | See §9. |

**Decisions still requiring your sign-off:** #3 (required vs audit-only), #7 (refuse vs fail-fast default). Everything else has a reviewer-supported recommendation.

---

## 7. Risks / what could break

- **Stalled runs.** Every previously-auto-failing path now halts and waits for user. Mitigated by §4.1.j `--unattended-revert-policy`.
- **Run-lock held across pauses.** Already the model for D.2a.5/D.2a.6. The lock IS released; the user re-runs `/implement-plan` to resume. Confirm the new G1/G2/G3/G10 pauses release the lock identically (they do — they call the shared subroutine).
- **Test surface expansion.** `test_plan_ops.py` plus integration tests need new assertions for: pause path on G1/G2/G3/G10, `paused` status transitions, `batch-next` filter on `paused`, `--authorization-source` required, D.4-rescue commit-tag XOR rules. Existing destructive-path tests get migrated to use the legacy back-compat flags or removed.
- **Audit-trail compatibility.** New `failed` run-log event field `authorization_source`; new event types `d4_rescue_start` / `d4_rescue_done`; new `awaiting_user.stage` values; new `commit_done` field for `d4_rescue_tag`. All additive — consumers tolerate unknown fields per existing convention. ALLOWED_LOG_EVENTS in `plan_ops.py` extends accordingly.
- **`paused` status surface.** Every consumer of plan-status enums (`mutate_task_status`, `lint-plans`, `update-plan-header`, `_validate_schedule`, parser) gets the new value. Bundle as one tight commit with grep-driven coverage.
- **Doc drift.** Add a `plan_ops.py audit` check that scans SKILL.md for `fail-task` usage and asserts each call site references the principle AND passes `--authorization-source`.

---

## 8. Cross-review summary (CODEX + GEMINI)

| Finding | Source | Status in v2 |
|---|---|---|
| `reconcile-batch` is a missed destructive path | Codex Q1 | Added as G10 (flagship case) — §4.7 |
| `batch-next` lacks paused-state filter (`plan_ops.py:2738, 2805`) | Codex Q5/Q6 + Gemini #1 | Introduced first-class `paused` status — §4.1.h |
| Binding-mode reinterpretation contradicts shipped tests/docs | Codex Q4 | Same-PR co-update plan + test rewrite spec — §4.1.e |
| D.4 rescue overloads D.5 machinery; no commit-tag exists | Codex Q3 | Defined as separate machinery: new template, events, tag — §4.1.i, §4.2 |
| Shared pause helper: payload mismatch risk | Codex Q2 | Clarified: control-flow only; per-stage payload table — §4.1.b |
| Infinite-rescue loop risk in D.4 | Gemini #2 | Explicit "single-shot terminal" rule — §4.1.i hard rule |
| Unattended-run deadlock | Gemini #3 | Spec'd `--unattended-revert-policy` + TTY-refuse — §4.1.j |
| Principle wording loophole ("success" too narrow) | Gemini #4 | Reworded to "non-empty diff against starting_sha" — §2 |
| `--authorization-source` should be required, not audit-only | Gemini | Adopted; surfaced as decision point #3 — §4.5, §6 |
| G6 implementation order — first | Gemini | Adopted — §9 |

**Net verdict from reviewers post-revision (predicted):** the v2 plan addresses every load-bearing finding with concrete file:line edits and explicit user-decision surfacing. Two items (decision points #3 and #7) genuinely need user input; everything else is reviewer-supported.

---

## 9. Implementation order (after user approval)

1. **`plan_ops.py` — `--authorization-source` required on `cmd_fail_task`** (G6). Telemetry first per Gemini.
2. **`plan_ops.py` — `paused` plan-status enum + `batch-next` filter + `mutate_task_status` allowed-set** (§4.1.h foundation).
3. **`plan_ops.py` — `--unattended-revert-policy` on preflight + TTY refuse** (§4.1.j).
4. **SKILL.md — principle + shared awaiting-user pause subroutine** (§4.1.a, §4.1.b).
5. **SKILL.md — Phase D.4 rescue (G2)** + new commit-tag (§4.1.d, §4.1.i). Highest-value behavior change.
6. **SKILL.md — Phase C empty-diff probe (G1)** (§4.1.c).
7. **SKILL.md — D.2a binding-mode reinterpretation (G3)** + same-PR test/doc updates (§4.1.e).
8. **`plan_ops.py` — `reconcile_batch` out-of-scope pause (G10)** + `--out-of-scope-policy` (§4.7).
9. **`commit-task` — `--d4-rescue-tag` argparse XOR rules** (§4.1.i).
10. **dispatch-templates.md — D.4-rescue template + B-rework / B-narrow reinforcement** (§4.2).
11. **`plan-remediator.md` — `Proposed-fix-scope:` output + dual input modes** (§4.3).
12. **`plan-implementer.md` — Phase B-rework clause** (§4.4).
13. **Tests — pause-path coverage, paused-status transitions, audit-flag round-trip, D.4-rescue commit-tag XOR, reconcile-batch pause** (§7).
14. **`DUAL_AGENT_PLAN_EXECUTOR.md` — propagate principle** (§4.6).
15. **`plan_ops.py audit` — drift check on `fail-task` call sites** (§7 mitigation).

Each step is one commit. The PR groups them under a single banner. No `/implement-plan` dogfooding for this PR — applied manually per the user's stated preference for plan-first review.

---

## 10. Items still requiring user input before code changes

- **Decision #3:** Make `--authorization-source` required (recommended) or audit-only?
- **Decision #7:** When `stdin` is non-TTY and `--unattended-revert-policy` is absent: refuse-with-error (recommended) or default to `fail-fast`?
- Confirm: same-PR vs split for the binding-mode reinterpretation (§4.1.e) — would you rather ship G3 in a follow-up so it doesn't conflict with the test rewrite?
- Confirm: ship the legacy `--codex-review-binding-destructive` back-compat flag, or break the contract cleanly?
