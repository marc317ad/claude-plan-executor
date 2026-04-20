# TASK-025 — Auto-revise on `needs-replan` via new `plan-author` subagent

**Base branch:** `main`
**Audit anchor commit:** `5036837` (TASK-022 commit — last stable state before this task)
**Design contract:** [`TASK-013_plan_review_and_remediation.md`](TASK-013_plan_review_and_remediation.md) — this task delivers item 3 of TASK-013's Goal ("Plan is either approved, revised once, or halted"). TASK-014A shipped the plan-review gate + approved/halted paths + the D.2a.5 repair loop; this task completes the "revised once" path that TASK-014A stubbed as a read-only-analyst re-dispatch.
**Chunk dependencies:** TASK-014A (shipped the plan-review gate this task extends), TASK-022 (added `--findings-json` persistence — the payload this task forwards to the new author).
**Motivating run:** `20260420T031537` — `TASK-020_plan_status_integrity.md` halted with `run_end reason=plan_review_failed` after two identical `needs-replan` verdicts from Codex. Root cause: the `needs-replan` retry re-dispatched `plan-analyst`, which is read-only (`plan-analyst.md:288-289`) and cannot edit the plan file. The findings were passed to an agent with no tools to act on them; the second review saw the same unchanged plan text and reached the same verdict.

---

## Goal

Make the `needs-replan` retry loop actually revise the plan before the second review. Introduce a new `plan-author` subagent with `Edit`/`Write` tools that consumes Codex's findings + the current plan text and emits a revised plan on disk. Default behavior is auto-revise on; `--no-auto-revise` preserves the pre-TASK-025 halt-on-second-`needs-replan` path for users who want to apply revisions by hand.

The retry path becomes:

1. `plan-analyst` → `valid`
2. Codex `plan-review` → `needs-replan` + findings
3. **`plan-author`** (new) — receives plan + findings, writes revised plan file in place
4. `plan-analyst` re-validate — cheap structural sanity check on the revised text
5. Codex `plan-review` — binding. Second `needs-replan` halts as today.

---

## Scoped Context

### Why a new subagent, not reusing `plan-analyst`

`plan-analyst.md:288-289` explicitly bans mutation (`"Forbidden commands: git add, git commit, git stash, git restore, git checkout <path>, mv, rm, cp, touch, output redirection"`). That constraint is load-bearing for the analyst's role as a *validator* — an agent that writes the file it validates cannot be trusted to report on it. Relaxing those bans in-place would erode the analyst's contract; the cleaner move is a sibling agent with `Edit`/`Write` whose sole job is to apply findings to the plan text.

### Why between the first and second review, not before or after

- **Not before the first review:** the user just authored the plan. Most plans land `approved` on the first pass (see run log 2026-04-18/19 — ~80% approved-or-approved-with-notes). Pre-emptive rewriting is waste.
- **Not after the second review:** that chases an asymptote. One authored revision per run is sufficient; if Codex objects a second time, the user should see the halt and decide.
- **Between:** targets the exact failure mode. The findings describe what's wrong; the author rewrites to fix them; the analyst re-validates structure; Codex gets a binding second look.

### Why default-on

The halt-and-wait alternative preserves human control but loses the automation's value for a class of errors (undefined schema codes, prose-vs-AC drift) that are mechanical to fix from the findings text. Default-on keeps runs moving; `--no-auto-revise` exists for users who want to apply revisions by hand or who are iterating on the author prompt itself.

### Files this task edits

- `plugins/plan-executor/agents/plan-author.md` **(create)** — new subagent spec, frontmatter declares `tools: Read, Grep, Glob, Edit, Write, Bash`, `model: opus`. Mirrors `plan-analyst.md` structure where applicable; body describes the input contract (plan file + Codex findings JSON), the edit rules (minimum change per finding, no scope inflation, preserve untouched sections verbatim), and the report format (markdown summary of edits applied + list of findings not actioned with rationale).
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` **(modify)** — new `## Phase 1.5a — plan-author dispatch (needs-replan auto-revise)` section after the existing Phase 1.5 block. Template embeds the plan path, the current plan text (optional — author reads the file directly), the Codex findings verbatim, and the `--no-auto-revise` opt-out reminder.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` **(modify)** — (1) CLI surface: add `--no-auto-revise` to the flags table; (2) §Phase 1.5 `needs-replan` branch rewrite to dispatch `plan-author` (when auto-revise on), then re-dispatch `plan-analyst` for structural re-validation, then re-run `plan-review`; (3) the existing "re-dispatch `plan-analyst` once with findings appended" text is replaced — do not keep the obsolete path.
- `plugins/plan-executor/scripts/plan_ops.py` **(modify)** — add `plan_author_start` and `plan_author_done` to `ALLOWED_LOG_EVENTS` (line 133-159). No schema changes, no new subcommand.
- `plugins/plan-executor/skills/implement-plan/run-log-schema.md` **(modify)** — document the two new events: required fields (`run_id`, `plan_file`, `findings_count`) and optional (`files_edited[]`, `findings_actioned[]`, `findings_skipped[]`).
- `tests/scripts/test_plan_ops.py` **(modify)** — V2-V4 below.

---

## Verification

**V1 — `plan-author.md` exists with the required frontmatter and sections.**

```bash
test -f plugins/plan-executor/agents/plan-author.md
grep -n '^name: plan-author$' plugins/plan-executor/agents/plan-author.md
grep -n '^tools:.*Edit' plugins/plan-executor/agents/plan-author.md
grep -n '^tools:.*Write' plugins/plan-executor/agents/plan-author.md
grep -n '^model: opus$' plugins/plan-executor/agents/plan-author.md
```

All four greps must hit.

**V2 — `ALLOWED_LOG_EVENTS` accepts the two new event types.**

```python
def test_allowed_log_events_includes_plan_author_events():
    from plugins.plan_executor.scripts.plan_ops import ALLOWED_LOG_EVENTS
    assert "plan_author_start" in ALLOWED_LOG_EVENTS
    assert "plan_author_done" in ALLOWED_LOG_EVENTS
```

**V3 — `log-event --event plan_author_start` and `plan_author_done` succeed end-to-end.**

```python
def test_log_event_plan_author_events_roundtrip(tmp_path):
    # invoke plan_ops.py log-event --event plan_author_start
    #   --fields-json '{"run_id":"X","plan_file":"p.md","findings_count":2}'
    # expect exit 0; tail line parses as dict with event=="plan_author_start"
    # repeat for plan_author_done with files_edited/findings_actioned/findings_skipped
```

**V4 — `dispatch-templates.md` contains a Phase 1.5a plan-author section.**

```bash
grep -n '^## Phase 1.5a — plan-author dispatch' plugins/plan-executor/skills/implement-plan/dispatch-templates.md
grep -n 'needs-replan auto-revise' plugins/plan-executor/skills/implement-plan/dispatch-templates.md
```

Both greps must hit.

**V5 — SKILL.md documents `--no-auto-revise` and the revised `needs-replan` branch.**

```bash
grep -n '^  --no-auto-revise' plugins/plan-executor/skills/implement-plan/SKILL.md
grep -n 'plan-author' plugins/plan-executor/skills/implement-plan/SKILL.md
```

Both greps must hit. A follow-up content check verifies the old "re-dispatch `plan-analyst` **once**" line in §Phase 1.5 no longer appears (or has been replaced with the new author-first path):

```bash
! grep -n 'Re-dispatch `plan-analyst` \*\*once\*\*' plugins/plan-executor/skills/implement-plan/SKILL.md
```

**V6 — `run-log-schema.md` documents the new events.**

```bash
grep -n 'plan_author_start' plugins/plan-executor/skills/implement-plan/run-log-schema.md
grep -n 'plan_author_done' plugins/plan-executor/skills/implement-plan/run-log-schema.md
```

Both greps must hit.

---

## Tasks

### TASK-025: Auto-revise on `needs-replan` via new `plan-author` subagent

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/agents/plan-author.md` (create)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/run-log-schema.md`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-014A, TASK-022
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1-V6 pass.
  - `plan-author.md` frontmatter declares `tools: Read, Grep, Glob, Edit, Write, Bash` and `model: opus`. Body specifies: (a) inputs are the plan path + Codex findings JSON + optional analyst annotations; (b) the author edits the plan file in place, minimum change per finding, preserving untouched sections verbatim; (c) the author emits a markdown report with three sections — `**Findings actioned:**`, `**Findings skipped:**` (with rationale per skip, e.g., "dismissed — finding contradicts project norm"), `**Files edited:**`; (d) the author's write scope is **the single plan file passed in as input** — it MUST NOT edit any other file, whether under `docs/plans/` or elsewhere. The restriction is keyed on the input path, not a directory glob, so the constraint holds regardless of where the plan lives on disk. (Codex plan-review finding #1, approved.)
  - SKILL.md §Phase 1.5 `needs-replan` branch dispatches `plan-author` (Agent, `subagent_type: "plan-author"`, `model: "opus"`) when auto-revise is on; on `--no-auto-revise`, falls through to the pre-TASK-025 path (halt with `run_end reason=plan_review_failed` — no silent retry without revision).
  - After `plan-author` returns, the orchestrator re-runs `plan-analyst` for structural re-validation of the revised plan file. If the re-validation returns `invalid`, halt with `run_end reason=plan_review_failed reason_detail=author_introduced_structural_defect`. If `valid` or `needs-enrichment` (same allow-gaps routing as the first pass), proceed to the binding second Codex `plan-review`.
  - The second Codex `plan-review` is binding exactly as today: `approved | approved-with-notes` → proceed to batch dispatch; `needs-replan` → halt.
  - `ALLOWED_LOG_EVENTS` includes `plan_author_start` and `plan_author_done`. Orchestrator emits `plan_author_start {run_id, plan_file, findings_count}` before dispatch and `plan_author_done {run_id, plan_file, files_edited[], findings_actioned[], findings_skipped[]}` after. V3 covers the log-event surface; the orchestrator-side emission is verified by reading SKILL.md's §Phase 1.5 content (the emit calls are documented inline as the author dispatch wraps the template).
  - `--no-auto-revise` appears in the SKILL.md CLI flags table with a one-line description: *"Disable auto-revise on needs-replan — halt instead of dispatching plan-author."* Defaults to off (auto-revise on).
  - `run-log-schema.md` documents `plan_author_start` and `plan_author_done` with the required/optional field lists above.
- **Out of scope:**
  - Retroactive reprocessing of historical `plan_review_failed` runs.
  - Extending `plan-author` to author plans from scratch (first-pass authoring). This task only handles the revision path.
  - Adding a `--auto-revise-max-attempts` knob. One revision pass per run is sufficient; more attempts are a future task if needed.
  - Forwarding `plan-author`'s edit report to the second `plan-analyst` or Codex `plan-review` pass — those agents re-read the revised plan file directly; context forwarding would invite ping-pong.
  - Schema changes to `codex_plan_review_schema.json`. Codex's output shape is unchanged; this task only consumes it.
  - A new `plan_author_skipped` event (mirroring `plan_review_skipped`). When `--no-auto-revise` is set, the orchestrator emits the existing `run_end reason=plan_review_failed` path; no separate skipped event is needed until a user-invocable skip path is added.

**Description:**
Introduce a new `plan-author` subagent with `Edit`/`Write` tools that applies Codex plan-review findings to the plan file on `needs-replan`, replacing the current read-only-analyst re-dispatch that cannot actually modify the plan. Default the behavior on; add `--no-auto-revise` for opt-out. The retry path gains a real writer between the first and second reviews; the second review remains binding.

**Implementation notes:**

- `plan-author.md` should mirror `plan-analyst.md`'s structure (frontmatter, Inputs, Process, Rules) but with the allowed/forbidden command lists inverted: `Edit`/`Write` on **the single plan file passed as input only** (bind the input path at dispatch time; no directory globs), explicit ban on editing any other file, explicit ban on `git` mutation commands. The "Process" section has three steps: (1) read the plan + parse the findings JSON, (2) apply a minimum-change edit per finding — if a finding is vague, contradictory, or contradicts the plan's existing acceptance criteria, skip it with rationale rather than invent intent, (3) emit the report.
- SKILL.md §Phase 1.5 edit window is roughly lines 186-204 of the current file (the `needs-replan` branch + the second-`needs-replan` halt). The rewrite: the `needs-replan` row in the routing table now reads *"Dispatch `plan-author` (if auto-revise on), then re-validate via `plan-analyst`, then re-run Codex `plan-review`. Second `needs-replan` halts."* Below the table, replace the "Re-dispatch `plan-analyst` **once**" paragraph with a new paragraph describing the three-step author→analyst→review sequence. Keep the second-`needs-replan` halt block intact (the `run_end reason=plan_review_failed` wiring).
- `plan_ops.py` edit is trivial: add the two string literals to the `ALLOWED_LOG_EVENTS` set at line 133-159. Place them adjacent to `plan_review_start` / `plan_review_done` for readability. No other plan_ops.py changes.
- `dispatch-templates.md` new section goes immediately after the Phase 1.5 block (after the current closing prose near line 51) and before Phase B. Template shape:

  > Apply Codex plan-review findings to the plan at `<absolute plan path>`. The first plan-review pass returned `needs-replan`; your job is to revise the plan text so a second review can proceed.
  >
  > Plan path: `<absolute plan>` (edit this file in place)
  >
  > Codex findings (verbatim from `parsed.findings` of the wrapper envelope):
  >
  > ```json
  > <codex_findings_json>
  > ```
  >
  > Codex summary (verbatim from `parsed.summary`):
  >
  > ```
  > <codex_summary>
  > ```
  >
  > Apply a minimum-change edit per finding. Preserve untouched sections verbatim — do not re-flow or re-format text the findings do not reference. If a finding is vague, contradictory, or contradicts the plan's existing acceptance criteria, skip it with a written rationale in your report rather than invent intent. Your write scope is **exactly the plan path above** — do NOT edit any other file, including other plan documents. Do NOT edit source code, tests, or configuration.
  >
  > Emit a markdown report with three sections: `**Findings actioned:**` (one bullet per finding applied, with the file:line anchor), `**Findings skipped:**` (one bullet per finding not applied, with rationale), `**Files edited:**` (the list of plan paths you touched — typically just the one input plan).
  >
  > **You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.

- CLI flag placement in SKILL.md: under the `Optional:` block of the `## Parse arguments` section (near line 67-76). One line: `  --no-auto-revise        Disable auto-revise on needs-replan; halt instead of dispatching plan-author`. Ordering is stylistic — place near `--skip-plan-review` since both flags are plan-review-adjacent.
- `run-log-schema.md` entries mirror the style of existing events: event name, required-fields block, optional-fields block, one-sentence when-emitted prose.
- Tests V2/V3 are pure unit tests on `plan_ops.py`; V1/V4/V5/V6 are grep-based content assertions. Place all six in a new `class TestTask025PlanAuthorSubagent:` adjacent to the existing TASK-022 test class.

**Reversion guidance:**

- `plan_ops.py` allowlist extension is additive. Safe to revert; post-TASK-025 log lines with `plan_author_start`/`plan_author_done` would fail re-validation against the pre-TASK-025 allowlist, but those are historical records and not re-validated.
- `plan-author.md` is a new file. Safe to delete on revert; the orchestrator falls back to the pre-TASK-025 path (re-dispatch read-only analyst — the bug this task fixes).
- `dispatch-templates.md` Phase 1.5a section is additive. Safe to remove on revert.
- SKILL.md §Phase 1.5 rewrite is not purely additive — it replaces the old "re-dispatch `plan-analyst` once" paragraph. On revert, restore the old paragraph verbatim from git history (commit `5036837` is a clean anchor).
- `run-log-schema.md` entries are additive. Safe to revert.
- **Never revert without addressing the underlying gap.** The TASK-020 halt this task fixes is structural — reverting without replacement re-creates the dead-loop retry. If the author-subagent approach itself turns out to be wrong, replace it with an explicit awaiting-user pause on the first `needs-replan` rather than restoring the read-only-analyst retry.

---

## Implementation Playbook

### Step 1 — Create `plan-author.md`

Author the new subagent spec at `plugins/plan-executor/agents/plan-author.md`. Frontmatter: `name: plan-author`, `description: ...`, `tools: Read, Grep, Glob, Edit, Write, Bash`, `model: opus`. Body sections: Inputs, Process (three steps per Implementation notes), Report format, Rules (allowed/forbidden commands; ban on editing files outside `docs/plans/**/*.md`; ban on `git` mutation commands; no Agent tool). Mirror the tone of `plan-analyst.md`.

### Step 2 — `plan_ops.py` allowlist extension

In `plan_ops.py:133-159`, add `"plan_author_start",` and `"plan_author_done",` to the `ALLOWED_LOG_EVENTS` set. Place them adjacent to `plan_review_start` / `plan_review_done` for readability.

### Step 3 — `dispatch-templates.md` Phase 1.5a section

Insert the Phase 1.5a block per the template in Implementation notes. Position: after the Phase 1.5 closing prose (current line ~51) and before `## Phase B — plan-implementer dispatch` (current line ~53).

### Step 4 — SKILL.md `needs-replan` branch rewrite + CLI flag

Two edits:

1. Add `--no-auto-revise` to the CLI flags block (around line 67-76).
2. Rewrite the `needs-replan` branch in §Phase 1.5 (current lines ~186-204): the routing-table row, the paragraph describing re-dispatch, and the second-`needs-replan` halt block. Preserve the halt block's `run_end reason=plan_review_failed` wiring verbatim.

### Step 5 — `run-log-schema.md` entries

Document `plan_author_start` and `plan_author_done` with required/optional field lists and one-sentence when-emitted prose per existing style.

### Step 6 — Tests

Add `class TestTask025PlanAuthorSubagent` with V1-V6 per the Verification section. Place adjacent to the existing TASK-022 test class.

### Step 7 — Regression sweep

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

V1-V6 green; existing TASK-013 through TASK-023 coverage unchanged. The pre-existing `test_analyst_to_parse_schedule_roundtrip` failure remains out of scope per TASK-019's Playbook.

---

## Out of Scope

- **First-pass plan authoring.** The `plan-author` subagent handles revision only; authoring from scratch stays with the user + Claude in a prior chat.
- **Multiple revision attempts per run.** One pass; second `needs-replan` halts as today.
- **Forwarding author reports to downstream agents.** Analyst and Codex re-read the revised plan file; context forwarding is out.
- **Schema changes to `codex_plan_review_schema.json`.** Codex output shape unchanged.
- **Retroactive reprocessing of historical `plan_review_failed` runs.**

## Reversion guidance

See per-step notes in Implementation notes. `plan-author.md`, the allowlist extension, the dispatch template section, and the run-log-schema entries are additive. The SKILL.md §Phase 1.5 rewrite replaces the old read-only-analyst retry paragraph; restore verbatim from commit `5036837` on revert. Do not revert without replacement — the underlying retry dead-loop is the bug.
