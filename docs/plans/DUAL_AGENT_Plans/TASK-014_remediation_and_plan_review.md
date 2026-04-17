# TASK-014 — Bounded remediation retry, roster status auto-update, and Codex plan review gate

**Design contract:** [`TASK-013_plan_review_and_remediation.md`](TASK-013_plan_review_and_remediation.md)
**Base branch:** `main`
**Audit anchor commit:** `8a317a9`
**Chunk dependencies:** TASK-001 (canonical contracts), TASK-002 (runtime validation helpers).
**Motivating run:** `20260417T153553` (TASK-004A) — Codex flagged 4 findings, third-opinion confirmed only 1 as load-bearing (`SKILL.md:147`, a one-line doc fix), but the strict D.2a path reverted all 138 semantic lines of valid implementation. This plan removes that failure mode.

---

## Goal

Three tightly scoped changes so narrow review failures stop costing full reverts and the plan roster stays in sync with execution state:

1. **Phase D.2a.5 — one bounded remediation retry, then user-pause.** Mirror the existing D.2b shape for the retry itself. On a second `needs-rework`, halt the run and emit `awaiting_user` instead of auto-reverting — the user decides next action (revert, manual fix, mark done, ...) in the follow-up turn. No new flags.
2. **`commit-task` / `fail-task` update `00_INDEX.json`** — when a plan's terminal task transitions, flip the matching chunk entry's `status` to `Done` (on commit) or leave it as-is (on fail; run remains `partial`). Orchestrator never hand-edits the roster.
3. **Phase 1.5 — Codex plan review gate** between analyst validation and batch dispatch. Claude wrote the plan; Codex reads it and emits a pre-exec verdict. One re-plan retry before halt.

Each subtask ships in isolation; a partial run of (A)+(B) is still useful without (C).

---

## Scoped Context

### Why now

- Run `20260417T153553` demonstrated the cost of the revert-only policy on narrow findings. One-line doc contradiction → 25 min of work reverted.
- `00_INDEX.json` currently drifts: TASK-012 and TASK-013 are on disk as plan files but have no roster entries. `check-plan-deps` reads the roster as authoritative, so drift is load-bearing.
- No Codex review of plans before dispatch. The analyst (Claude/Opus) wrote the plan *and* validated the schedule, i.e. the same family double-checks itself.

### Existing surfaces we extend (not rebuild)

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — orchestrator protocol. Insert Phase 1.5 and D.2a.5 sections.
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — add Phase 1.5 Codex plan-review template, and a Phase B-rework template that embeds reviewer findings.
- `plugins/plan-executor/scripts/plan_ops.py` — `commit-task` and `fail-task` already touch plan-level status; extend them to also mutate `00_INDEX.json`. Add `parse-plan-review-report` helper per TASK-013 §Suggested contracts.
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` — add a `plan-review` subcommand alongside `implement`/`review`.

### Non-goals

- Not implementing a separate `repair-planner` / `remediation-planner` agent. The D.2a.5 retry re-dispatches `plan-implementer` with forwarded findings — no new agent role.
- Not rewriting `parse-reviewer-report` (TASK-012's scope). The rework retry uses the existing Codex-review envelope shape.
- Not changing the `minor-findings` → commit path. That already works correctly.

---

## Verification

### Subtask-A (remediation retry) — V1–V4

**V1 — Third-opinion `needs-rework` triggers retry, not immediate fail.**

A synthetic run where Codex returns `needs-rework` and the Phase D.5 code-reviewer agrees MUST dispatch `plan-implementer` once more with the Codex findings embedded, re-dispatch Codex review, and commit on the second verdict of `clean | minor-findings`. Assert via run-log events: `review_done verdict=needs-rework` (Codex) → `review_done verdict=needs-rework` (code-reviewer-d5) → `remediation_start {task_id, findings_count}` → `implement_done outcome=success` → `review_done verdict=clean` (Codex) → `commit_done`.

**V2 — Retry failure halts without auto-revert.**

If the post-remediation Codex re-review returns `needs-rework` a second time, the orchestrator MUST NOT call `fail-task` automatically. Instead: emit `log-event type=awaiting_user payload={task_id, stage:"post_remediation_review", codex_findings, d5_summary}`, call `finalize-execution-log --outcome paused`, print the failure envelope, and return control to the user with pending edits still in the working tree. No second retry; no silent revert. The user's next conversation turn decides disposition — typical options: "revert" → orchestrator runs `fail-task`; "mark done" → `commit-task` with override; "apply this fix" → manual edit + re-run review. Assert via run-log events: second `review_done verdict=needs-rework` → `awaiting_user` → `run_end outcome=paused`. No `fail_task_done` event. Working tree `git status` shows the implementer's edits unstaged/uncommitted.

**V3 — Retry is skipped under `--codex-review-binding`.**

When the flag is set, D.2a remains unchanged: Codex `needs-rework` → fail-task immediately. No D.5 third opinion, no D.2a.5 retry.

**V4 — Commit after retry tags `[remediation]`.**

The commit body for a remediation-retry success includes a `[remediation]` tag adjacent to any existing `[disagreement]` handling, and the run summary's per-task row shows `[remediation]`.

### Subtask-B (roster status auto-update) — V5–V7

**V5 — `commit-task` flips `00_INDEX.json` status to `Done`.**

Given a plan whose `00_INDEX.json` entry has `status=Pending`, a successful `commit-task` MUST set that entry's `status=Done` (capitalized; matches `ALLOWED_INDEX_STATUSES`). Assert via direct JSON diff on `00_INDEX.json` before and after. The plan-level `**Status:**` bullet flip (existing behavior) is unchanged.

**V6 — Roster write is atomic and idempotent.**

`commit-task` writes `00_INDEX.json` via `tempfile + os.replace` so a mid-write interrupt leaves the prior roster intact. Re-running `commit-task` for an already-Done chunk is a no-op write (same JSON bytes).

**V7 — Missing chunk entry is a structured error, not a silent skip.**

If the plan's `task_id` has no entry in `00_INDEX.json`, `commit-task` exits 1 with `errors[0].code == "task-not-in-index"` and the git commit is rolled back. (This is the existing `_parse_index_roster` error code from `check-plan-deps`; reuse it.)

### Subtask-C (Codex plan review gate) — V8–V11

**V8 — Phase 1.5 dispatches Codex plan review after schedule persist, before batch dispatch.**

Run-log events in order: `run_start` → `analyst_done` → `schedule_written` → `plan_review_start {reviewer=codex}` → `plan_review_done {verdict, findings_count}` → `batch_start` (only if verdict permits).

**V9 — Verdict routing.**

- `approved` → proceed.
- `approved-with-notes` → proceed; notes carried into final run summary.
- `needs-replan` → dispatch `plan-analyst` **once** with findings forwarded; re-review via Codex. A second `needs-replan` halts with `run_end reason=plan_review_failed` and no batches run.

**V10 — Codex unavailable → degrade to warning.**

If `codex_available=false` at preflight, Phase 1.5 logs `plan_review_skipped reason=codex_unavailable` and proceeds. Run summary flags "lacking independent plan review".

**V11 — `--skip-plan-review` bypass flag.**

An explicit `--skip-plan-review` CLI flag skips Phase 1.5 entirely, logs `plan_review_skipped reason=flag` and a loud banner in the final summary. Parallel-safe with `--skip-cross-review`.

---

## Tasks

### TASK-014A: Bounded remediation retry in Phase D.2a

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/scripts/plan_ops.py` (no new subcommand; add a `commit-task --remediation-tag` flag for V4; accept `awaiting_user` as a known `log-event` type; accept `paused` as a valid `finalize-execution-log --outcome` value)
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1–V4 pass.
  - SKILL.md Phase D.2a section grows a D.2a.5 step with unambiguous routing table (Codex `needs-rework` + D.5 `needs-rework` → D.2a.5; Codex `needs-rework` + D.5 `ship|ship-with-fixes` → existing D.3 `[disagreement]` path, unchanged).
  - `dispatch-templates.md` adds a "Phase B-rework" template that embeds `codex_findings_json` + `d5_summary` and explicitly tells the implementer to "fix narrowly, do not scope-inflate".
  - `commit-task` accepts `--remediation-tag` (boolean). When set, the commit body appends a `[remediation]` tag line.
  - Retry is strictly one attempt. A second Codex `needs-rework` emits `log-event type=awaiting_user` and `finalize-execution-log --outcome paused`; the orchestrator does NOT call `fail-task`. Pending edits remain in the working tree so the user's next turn can revert, keep, or hand-fix.
  - SKILL.md explicitly forbids `fail-task` on the post-remediation needs-rework path. The only way to `fail-task` after a paused run is an explicit user instruction in the next conversation turn.

**Description:**
After the Phase D.5 third-opinion code-reviewer agrees with Codex's `needs-rework`, re-dispatch `plan-implementer` once with the findings forwarded. Re-run Codex review on the rewrite. Commit on `clean|minor-findings`. On a second `needs-rework`, halt the run in a "paused — awaiting user" state: log the failure envelope, stop the orchestrator, and let the user decide in the next conversation turn whether to revert (`fail-task`), keep as-is (`commit-task` with override), or hand-fix. Mirrors D.2b's one-attempt shape for the retry; diverges from D.2b only on what happens *after* the second failure (D.2b goes straight to `fail-task`; D.2a.5 pauses for user judgment).

**Implementation notes:**
The feedback-forwarding risk ("implementer does exactly what reviewer said, even when reviewer is wrong") is the reason v1 of the skill deferred forwarding. It is mitigated here because D.2a.5 only fires after D.5 independently agreed the finding is load-bearing — we are not trusting Codex alone. Keep the forwarded prompt structured: `{findings:[...], d5_summary:"..."}`, not a free-form "do what Codex said".

The halt-on-second-failure path is deliberately minimal: no menu, no timer, no state machine, no new CLI flags. `log-event type=awaiting_user` + `finalize-execution-log --outcome paused` + a printed envelope is enough because the user's next conversation turn *is* the decision point. `finalize-execution-log` must accept `paused` as an outcome literal alongside the existing `success|partial|failed`. `log-event` must accept `awaiting_user` alongside existing event types. Pending edits are deliberately NOT reverted — the user needs them in the tree to choose keep/revert/hand-fix.

**Reversion guidance:**
Safe to revert; remove Phase D.2a.5 section from SKILL.md, drop the `--remediation-tag` arg, delete the "Phase B-rework" template, remove `awaiting_user` from the `log-event` allow-list, and remove `paused` from `finalize-execution-log --outcome` accepted values. Reverts restore the strict "Codex needs-rework → D.5 → fail-task" path.

---

### TASK-014B: `00_INDEX.json` roster status auto-update

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-001 (roster schema already validated by `_parse_index_roster`)
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V5–V7 pass.
  - `commit-task` locates `00_INDEX.json` via `<plan_file>.parent / "00_INDEX.json"` and mutates only the chunk entry whose `file` field matches the plan's basename.
  - Write is atomic: `tempfile.NamedTemporaryFile(dir=parent) + os.replace(tmp, index_path)`.
  - Missing-roster-entry → `errors[0].code == "task-not-in-index"` (reuse existing code); commit is rolled back via the existing `commit-task` rollback path.
  - Idempotent: running `commit-task` twice on the same task produces byte-identical `00_INDEX.json` on the second run.

**Description:**
When `commit-task` successfully commits a task, it must also flip the matching `00_INDEX.json` chunk's `status` from `Pending` (or anything else in `ALLOWED_INDEX_STATUSES`) to `Done`. `fail-task` leaves the roster status untouched — a failed task on an otherwise-green plan shouldn't mark the chunk Done, but also shouldn't retroactively mark it anything else (future retries need the original Pending).

**Implementation notes:**
`ALLOWED_INDEX_STATUSES = {"Done", "Pending", "Superseded"}` lives at `plan_ops.py:75`. The matcher is `chunk["file"] == plan_file.name` (basename-only; the roster stores unqualified filenames, see entries 001–011).

**Reversion guidance:**
Safe to revert; remove the `00_INDEX.json` mutation from `commit-task`. Roster drifts back to manual-update mode.

---

### TASK-014C: Phase 1.5 — Codex plan review gate

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py`
  - `plugins/plan-executor/scripts/plan_ops.py` (new subcommand: `parse-plan-review-report`)
  - `plugins/plan-executor/scripts/codex_plan_review_schema.json` (create) — new schema file, mirror `codex_review_schema.json`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-014A (prompt-template conventions), TASK-001 (canonical error shape)
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V8–V11 pass.
  - `plan_codex_dispatch.py plan-review --plan-file <abs> --schedule-file <abs> --timeout 180` emits one JSON envelope with `parsed.verdict ∈ {approved, approved-with-notes, needs-replan}`. Wrapper owns the sandbox baseline/cleanup same as `implement`/`review`.
  - `plan_ops.py parse-plan-review-report --stdin --json` validates the envelope against `codex_plan_review_schema.json`; halts with structured `errors[*]` on schema violations.
  - SKILL.md Phase 1.5 section inserts **after** schedule persist (currently line 149) and **before** Dry-run mode (currently line 151).
  - `--skip-plan-review` CLI flag mutually-exclusive with nothing; works alongside `--dry-run`, `--codex-only`, `--claude-only`, `--task-ids`.

**Description:**
Between analyst validation and batch dispatch, dispatch Codex to review the persisted schedule + plan file. Codex returns `approved | approved-with-notes | needs-replan`. `needs-replan` triggers one analyst re-dispatch with findings forwarded; a second `needs-replan` halts the run before any batches execute.

**Implementation notes:**
- Do NOT reuse the `review` subcommand of `plan_codex_dispatch.py` — a plan review has a different input shape (plan markdown + schedule JSON, not a diff) and a different verdict vocabulary. A sibling subcommand keeps the schemas clean.
- Codex is the reviewer because the plan was authored by Claude/Opus (analyst). If `codex_available=false` at preflight, Phase 1.5 degrades to a warning, not a halt.
- The plan-analyst re-dispatch on `needs-replan` uses the same `plan-analyst` agent with the Codex findings appended to the Phase A prompt. One attempt only.

**Reversion guidance:**
Safe to revert; remove Phase 1.5 from SKILL.md, drop the `plan-review` wrapper subcommand, delete `parse-plan-review-report` and the schema file. Preflight and analyst dispatch are unchanged.

---

## Out of Scope

- Tightening the Codex reviewer prompt to prefer `minor-findings` for non-load-bearing findings. Scheduled as [TASK-015](TASK-015_codex_reviewer_prompt_tuning.md); ships independently of TASK-014 (no dependency either way — the new D.2a.5 retry and the calibration tweak are orthogonal safety nets at different layers).
- Any behavior change when Codex review is binding on Claude work (`--codex-review-binding` orchestrator flag). Binding mode is an explicit opt-in to **Codex-authoritative review**: a `needs-rework` verdict in binding mode triggers immediate `fail-task` with NO Phase D.5 third-opinion, NO D.2a.5 remediation retry, and NO user-pause. The flag exists so users who want Codex to have veto authority over Claude's work can get that semantics unambiguously — it is meant to be the strict path, not the lenient one. TASK-014's new safety nets (D.2a.5 retry + user-pause on second failure) fire only on the default, non-binding path. Opting into binding mode means opting out of them. If you want the new safety nets, do not set `--codex-review-binding`. If you set it and hit `needs-rework`, the run will revert exactly as it did before TASK-014.

## Reversion guidance

All three subtasks are independently reversible. (A) and (C) are pure SKILL.md + dispatch-template + wrapper work; (B) is a ~30-line change inside `cmd_commit_task`. Revert order if needed: C → A → B (least-dependency first).
