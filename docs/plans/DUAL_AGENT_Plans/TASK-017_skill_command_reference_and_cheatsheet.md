# TASK-017 — Skill command reference + CLI cheat sheet + invocation discipline

**Base branch:** `main`
**Audit anchor commit:** `1456687`
**Chunk dependencies:** none (formerly TASK-001 — now archived/complete).
**Motivating run:** `20260417T214309` — executing TASK-016 produced five distinct CLI / runtime errors: (1) `parse-plan-review-report` fed bare `parsed` object instead of the full envelope; (2) invented `log-event` type `d5_review_done` not in the allowlist; (3) `commit-task` rejected on `--reviewer codex --reviewer-verdict needs-rework` + wrong `minor-findings` schema; (4) Agent-registry miss on the newly-created `plan-remediator` subagent (same-session create-then-invoke); (5) post-compaction flag amnesia (`--payload` vs `--fields-json` on `log-event`). Full post-mortem in `docs/analysis/2026-04-17_TASK-016C_binding_review_override.md` and in the TASK-016 execution log.

---

## Goal

Prevent the class of CLI / runtime errors seen in run `20260417T214309` with pure documentation + process rules:

1. **"## Command reference" appendix** in `SKILL.md` — correctly-shaped invocation examples for every non-obvious `plan_ops.py` subcommand the orchestrator uses. Targets errors 1, 2, 3.
2. **`plan_ops_cheatsheet.md`** (new file next to `dispatch-templates.md`) — one-page CLI summary designed to be Read'd after context compaction to rehydrate the CLI vocabulary in one shot. Targets error 5.
3. **"### Pre-invocation checklist"** under `SKILL.md`'s existing `## Rules` — three process rules that would have caught errors 1/2/3/5 at source, plus one rule codifying error 4 as "never Agent-invoke a subagent file created in the current run."

No `plan_ops.py` code changes, no schema changes, no template changes, no agent-spec changes. Append-only, fully revertable.

---

## Scoped Context

### Why now

Five errors in one run; four preventable with docs; one is a runtime-registry note. The cheapest high-leverage prevention is documentation co-located with the skill that makes the calls. Doing this before the next dual-agent run amortizes instantly.

### Existing surfaces we edit

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — append two sections (command reference appendix + pre-invocation-checklist rule block under the existing `## Rules`). Do NOT rewrite existing phase sections.
- `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md` — NEW file, sibling to `dispatch-templates.md`.

### Non-goals

- Changing `plan_ops.py` argparse or error messages (e.g., suggesting the D.5-disagreement pattern when `--reviewer codex --reviewer-verdict needs-rework` is passed). Nicer, but out of scope; revisit if the doc fix proves insufficient.
- Changing `dispatch-templates.md`. Templates are fine after TASK-015/016.
- Auto-generating the cheat sheet from `plan_ops.py --help` output. Static handwritten text is more readable and this CLI is stable.
- Preflight check that warns on same-session subagent create-then-invoke. A process rule is cheaper and equally reliable.

### Error 4 is intentionally not a code fix

The Agent registry snapshots at session start. A subagent file created by commit `7839d5d` during run `20260417T214309` was not live for dispatch later in that same run. This is expected Claude Code runtime behavior, not a bug — the plan-remediator IS available now in a fresh session. The prevention is a process rule: don't invoke what didn't exist when your session hatched.

---

## Verification

**V1 — SKILL.md gains a "## Command reference" appendix** (appended before the final `## Rules` section, OR at end of file if `## Rules` is currently last). Covers at minimum:

- `parse-plan-review-report`: full envelope wrapper shape `{subcommand:"plan-review", outcome:"success", parsed:{...}}` — NOT just the inner `parsed`. Example rejects the bare form with a pointer to the validator.
- `parse-d5-adjudication`: stdin payload shape `{verdict, summary, load_bearing, dismissed}` + `--codex-findings-count N` example for each of the four verdicts (`ship | ship-with-fixes | needs-rework | partial-agreement`).
- `log-event`: `ALLOWED_LOG_EVENTS` rendered inline as an alphabetized code block, plus one `--fields-json '{...}'` example for each event class (lifecycle, phase, outcome).
- `commit-task`: one example per committable pattern — (a) standard `--reviewer codex --reviewer-verdict minor-findings` with a correctly-shaped `--reviewer-minor-findings` JSON array; (b) D.5-disagreement `--reviewer claude --reviewer-verdict ship-with-fixes --disagreement-tag` with an explicit callout that D.5's verdict becomes the winning verdict, NOT Codex's `needs-rework`; (c) D.2a.5 remediation `--remediation-tag`; (d) D.2a.6 narrow-remediation `--narrow-remediation-tag --dismissed-finding-ids I,J,K`; (e) user-override `--reviewer none --reviewer-verdict "" --remediation-tag` pattern for post-pause keep-as-is commits. `minor-findings` schema rendered once with allowed severity enum `{critical, important, minor}`.
- `finalize-execution-log`: `--rows-json` row shape `{task, agent, reviewer, verdict, commit, notes}` example plus the `--outcome {success,partial,failed,paused}` semantics table.

Each example is a real, copy-pastable invocation against `plan_ops.py` at audit anchor `1456687`, using `--dry-run` where the subcommand supports it.

**V2 — SKILL.md gains a "### Pre-invocation checklist" block under the existing `## Rules` section** with four numbered rules:

1. Run `--help` on any `plan_ops.py` subcommand before re-invoking it after a context compaction. The cache is cheap; flag-name drift isn't.
2. Grep `ALLOWED_*` constants in `plan_ops.py` before a `log-event` or `commit-task` call that uses enum-valued flags (event names, reviewer verdicts, severity values, outcome values).
3. Read the relevant `_validate_*` function in `plan_ops.py` before piping into a `parse-*` subcommand — the validator checks the envelope shape, not what downstream code consumes.
4. **Never Agent-dispatch a subagent file created during the current run.** The Agent registry snapshots at session start; newly-created subagents become available in the next fresh session. If you must retry within the current session, fall back to the closest existing subagent with an inlined prompt matching the new subagent's contract.

Each rule has a one-line **Why:** tagline referencing the error it prevents, matching the `## Rules` style already used in SKILL.md.

**V3 — New file `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md`** (≤150 lines total). Structure:

- Header: purpose + "Read this after compaction or as a first lookup instead of `--help`".
- One block per subcommand, ordered by typical-run lifecycle (preflight → parse-schedule → compute/write-schedule → acquire-lock → batch-next → parse-implementer-report → parse-plan-review-report → parse-d5-adjudication → commit-task → fail-task → log-event → update-plan-header → finalize-execution-log → release-lock, plus helpers `normalize-task-id`, `filter-schedule`, `reconcile-batch`, `check-plan-deps`, `path-info`).
- Each block: subcommand name, critical flags only, one-line example. No prose beyond the one-line-what-it-does intro per subcommand.
- Footer: cross-link to `SKILL.md` "## Command reference" for deeper examples.

**V4 — SKILL.md's "## Command reference" cross-links to the cheat sheet** (first line of the appendix). One sentence naming the cheat-sheet path and the scenario in which to Read it (post-compaction CLI rehydration).

---

## Tasks

### TASK-017: Skill command reference + CLI cheat sheet + invocation discipline

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md` (create)
- **Dependencies:** none
- **Test command:** `none` (pure docs; each example is hand-verified at author time against the current `plan_ops.py --help` output and the run-log of `20260417T214309`. No Python dispatch tests — static string assertions would add brittle coupling for trivial upside.)
- **Acceptance criteria:**
  - V1–V4 pass.
  - No edits to `plan_ops.py`, `dispatch-templates.md`, agent specs, `codex_*_schema.json`, or any schema JSON.
  - Cheat sheet file ≤150 lines total.
  - Command reference section growth in SKILL.md ≤250 lines.
  - Every code-block example uses real subcommand names and real flag names verified against `venv/bin/python plugins/plan-executor/scripts/plan_ops.py <sub> --help` at author time. Placeholder substitution is allowed only for task IDs, run IDs, SHAs, and file paths.
  - Pre-invocation checklist lives under the existing `## Rules` section, not as a new top-level section. Rules 1–4 each carry a one-line **Why:** tagline referencing the error number from run `20260417T214309` (1–5 as numbered in the Motivating-run paragraph above).
  - Rule 4 (subagent-dispatch rule) explicitly names the Agent registry snapshot behavior so future orchestrators understand the cause, not just the rule.
  - The D.5-disagreement commit-task example (V1 item c in the bullet list above) explicitly states: "D.5's verdict (`ship | ship-with-fixes | partial-agreement`) is passed as `--reviewer-verdict`; Codex's `needs-rework` is NOT — that was the confusion behind error 3 in run `20260417T214309`."
  - `ALLOWED_LOG_EVENTS` in the command reference matches the current set in `plan_ops.py` exactly (grep the constant at author time). Missing or stale entries fail the acceptance bar.

**Description:**
Append-only documentation work. All additions live at the end of `SKILL.md` (new `## Command reference` appendix) or under existing `## Rules` (new `### Pre-invocation checklist` sub-block); a new sibling file `plan_ops_cheatsheet.md` is created next to `dispatch-templates.md`. No runtime behavior changes. The command reference exists to make three recurring CLI-shape errors zero-shot-resolvable; the cheat sheet exists as a compaction-resilient single-Read lookup; the checklist codifies the four process rules.

**Implementation notes:**
- Source every example from the run-log of `20260417T214309` (or from the current `plan_ops.py --help` output) — match validator shapes exactly, do not invent.
- For the `commit-task` D.5-disagreement example: include a short prose paragraph explaining the verdict-flattening rule ("D.5 is the third-opinion tie-breaker; its verdict becomes the committed verdict, `--disagreement-tag` records that Codex disagreed"). This was the exact point-of-confusion in run `20260417T214309`.
- For `log-event`, render `ALLOWED_LOG_EVENTS` as an alphabetized code block inside a single fenced block, not a prose list. Grep-visibility matters for the post-compaction recovery case.
- Cheat-sheet subcommand order is LIFECYCLE order (preflight → ... → release-lock, then helpers), not alphabetical. The reader is usually asking "what comes next in the run?" — alphabetical defeats that.
- Do NOT duplicate content between SKILL.md's command reference and the cheat sheet. Cheat sheet is the one-page summary; SKILL.md is the deep reference with full JSON payloads. Cross-link in both directions.
- Keep the cheat sheet ≤150 lines — if it grows past that, split examples into the SKILL.md command reference instead of bloating the cheat sheet.

**Reversion guidance:**
Pure additive. `git restore plugins/plan-executor/skills/implement-plan/SKILL.md` + `rm plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md`. Nothing else references these.

---

## Out of Scope

- Improving `plan_ops.py` argparse error messages (e.g., suggesting the D.5-disagreement pattern when `--reviewer codex --reviewer-verdict needs-rework` is passed). Follow-up plan candidate; revisit after TASK-017 ships and measures whether the docs alone close the error rate.
- Auto-generating either doc from `plan_ops.py --help` output. Maintenance cost exceeds freshness gain at current CLI stability.
- Preflight check at Phase 0 that cross-references subagent filenames in the plan against the live Agent registry to warn on same-session create-then-invoke. The process rule (V2 rule 4) is cheaper and equally reliable.
- Changes to Claude `code-reviewer`, Codex reviewer, or D.5 third-opinion prompts. These are fine after TASK-015.
- Test-harness work (no pytest dependency). Hand-verification against `--help` is the acceptance mechanism.

## Reversion guidance

Single SKILL.md edit + single new file. Clean revert path; no downstream references.
