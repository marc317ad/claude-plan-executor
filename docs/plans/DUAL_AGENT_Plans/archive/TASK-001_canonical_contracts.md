# TASK-001 — Canonicalize Executor Protocol Contracts

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-001
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md)
**Base branch:** `phase-7b5-bug-fixes`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** none — this is the foundation every other chunk assumes.
**Issues absorbed:** ISSUE-001, 009, 013, 014, 016, 022, 024, 025, 026

---

## Goal

Pick one authoritative version of every executor wire contract (task status, schedule JSON, implementer report labels, execution-log shape, schedule persistence ownership), apply it across every seam (helpers, tests, design doc, dispatch templates), and delete or explicitly alias the drift. After this chunk lands there is exactly one protocol per concept, and every downstream chunk can assume a single canonical shape.

## Scoped Context

The audits (Codex + Claude) agree that three independent wire contracts drifted across Phases 1–4 and that the Phase 4 test suite pinned the drift by asserting the implementation against itself. The canonical choice in every case is **update helper code and tests to match the design doc / analyst / implementer emissions**, because those already ship the contract the design prescribes.

### ISSUE-001 (P0) — analyst JSON field names

- **Design:** `tasks[*].id`, `batches[*].index` (`DUAL_AGENT_PLAN_EXECUTOR.md:312, 327`).
- **Analyst emits:** exactly that shape (`.claude/agents/plan-analyst.md:270-306`).
- **Helper requires:** `task_id`, `batch_index` in three sites:
  - `scripts/plan_ops.py:250` — `parse-schedule` required-field check
  - `scripts/plan_ops.py:263` — `parse-schedule` batch required-field check
  - `scripts/plan_ops.py:301, 322-328` — `batch-next` lookups
- **Tests pin the drift:** `tests/scripts/test_plan_ops.py:334, 341, 349, 350, 369, 370, 436, 437` all hand-build schedules with `task_id`/`batch_index`.
- **Fix direction:** update helper + tests to read `id`/`index`. Any compatibility alias is explicit and covered by a test.

### ISSUE-009 (P1) — task status vocabulary

- **Design:** `pending | in-progress | done | failed | blocked | skipped` (`DUAL_AGENT_PLAN_EXECUTOR.md:165`).
- **Helper accepts:** `{"open", "in-progress", "done", "failed", "blocked", "skipped"}` at `scripts/plan_ops.py:40` (`ALLOWED_TASK_STATUSES`).
- **Fixture uses:** `open` (`docs/plans/sample_phase4.md:46` etc. — fixed by TASK-006).
- **Fix direction:** replace `open` → `pending` in `ALLOWED_TASK_STATUSES`. If an alias is retained, document it and cover it with a regression test.

### ISSUE-013 (P2) — execution-log column shape

- **Design:** `Task | Agent | Outcome | Reviewer | Commit` (`DUAL_AGENT_PLAN_EXECUTOR.md:224`).
- **Writer emits:** `Task | Agent | Reviewer | Verdict | Commit | Notes` at `scripts/plan_ops.py:601` (`cmd_finalize_execution_log` / helpers nearby).
- **Fix direction:** pick one and apply it in both places. Recommendation: keep writer's current richer shape (`Agent | Reviewer | Verdict | Commit | Notes`) and promote it into the design doc §224, because `Outcome` and `Verdict` are the same axis and the writer already distinguishes reviewer from implementer agent. State the choice explicitly in the design doc once picked.

### ISSUE-014 (P2) — implementer concerns label

- **Design / implementer contract:** `**Concerns for reviewer:**` (`DUAL_AGENT_PLAN_EXECUTOR.md:406`, `.claude/agents/plan-implementer.md:126`).
- **Parser searches for:** `**Concerns:**` at `scripts/plan_ops.py:391`.
- **Effect:** reviewer concerns are silently dropped from all well-formed implementer reports.
- **Fix direction:** parser searches `**Concerns for reviewer:**`; optionally retain `**Concerns:**` as an alias and flag with a warning field in the output.

### ISSUE-016 (P1) — implementer plan-adaptations label

- **Implementer contract:** `**Plan adaptations:**` is mandatory (`.claude/agents/plan-implementer.md:98-99`).
- **Parser behavior:** `cmd_parse_implementer_report` (`scripts/plan_ops.py:354-403`) extracts outcome / files_changed / diff_summary / test_outcome / concerns / reversion_guidance. **Plan adaptations is not extracted.**
- **Effect:** the orchestrator has no visibility into plan deviations the implementer declared; reviewers cannot be told about them. The rule at `.claude/skills/implement-plan/SKILL.md:316` ("Never write inline Python for plan ops") means the orchestrator cannot work around this by parsing the raw markdown itself.
- **Fix direction:** extend parser to extract `plan_adaptations` as a list (one bullet per adaptation), same pattern as `concerns`.

### ISSUE-022 (P3) — design-doc `blockers` shape

- **Design §7.2 line 508:** `"blockers": []` (implicitly array of strings).
- **Design §15.2 lines 1134-1136:** `blockers` as array of objects `{type, details, needs_from_dispatcher}`.
- **Schema implements simpler form:** `scripts/codex_implement_schema.json:30-33` = array of strings.
- **Fix direction:** docs-only. Pick §7.2's array-of-strings form (matches schema and is simpler). Delete or update §15.2.

### ISSUE-024 (P3) — `--skip-analysis` orphan note

- **Design §9.1 line 751** notes `--skip-analysis` was dropped from v1 but does not reconcile with the schedule-sidecar gap.
- **Fix direction:** update §9.1 to reference ISSUE-020 / TASK-004's `write-schedule` as the prerequisite that unblocks a future `--skip-analysis` flag.

### ISSUE-025 (P3) — retry forwards reviewer findings?

- **Design §8.3 Codex-implements row:** silent on whether retry forwards reviewer findings.
- **SKILL.md:** "NOT forwarded in v1" (`.claude/skills/implement-plan/SKILL.md:249-256`).
- **Fix direction:** promote the SKILL.md choice into §8.3 with rationale (retry may repeat the same failure; v1 accepts this tradeoff to keep retry scope narrow).

### ISSUE-026 (P3) — schedule persistence path + subcommand

- **Design §6.1 (306-346):** specifies the schedule JSON contract but does not name the persistence path or the subcommand that owns writing it.
- **SKILL.md:113:** says to persist to `docs/plans/<basename>.schedule.json`.
- **Fix direction:** add a §9.3 bullet naming `docs/plans/<basename>.schedule.json` and attributing the write path to the `plan_ops.py write-schedule` subcommand that TASK-004 delivers. This chunk reserves the bullet; TASK-004 implements the subcommand.

---

## Verification

After this chunk lands, all of the following must be true. Commands assume the repo root as cwd; Python is `venv/bin/python` (per `CLAUDE.md`).

**V1 — Status vocabulary single-source.**

```bash
venv/bin/python -c "from scripts import plan_ops; assert 'pending' in plan_ops.ALLOWED_TASK_STATUSES; assert 'open' not in plan_ops.ALLOWED_TASK_STATUSES or getattr(plan_ops, 'STATUS_ALIAS_OPEN_ACCEPTED', False) is True"
```

**V2 — Analyst field round-trip.**

Run `parse-schedule` against a minimal analyst-shaped JSON and assert exit 0 with no errors:

```bash
echo '{"outcome":"valid","tasks":[{"id":"001","agent":"codex","files":["a.txt"],"dependencies":[]}],"batches":[{"index":1,"task_ids":["001"],"file_locks":["a.txt"]}]}' \
  | venv/bin/python scripts/plan_ops.py parse-schedule --stdin --json
```

Exit code must be 0; `errors` must be `[]`.

**V3 — `batch-next` uses `id`/`index`.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k batch_next
```

All tests green after test migration.

**V4 — Implementer report parser reads both labels.**

```bash
printf '**Outcome:** success\n**Files changed:**\n- a.txt\n**Diff summary:** noop\n**Test outcome:** not_run\n**Concerns for reviewer:**\n- c1\n- c2\n**Plan adaptations:**\n- deviation1\n**Reversion guidance:** revert\n' \
  | venv/bin/python scripts/plan_ops.py parse-implementer-report --json
```

Output must include `"concerns": ["c1", "c2"]` **and** `"plan_adaptations": ["deviation1"]`.

**V5 — Execution-log column shape.**

`finalize-execution-log` emits the shape that matches the design doc after this chunk. If the writer's richer shape wins (recommendation), §224 of `DUAL_AGENT_PLAN_EXECUTOR.md` reflects `Task | Agent | Reviewer | Verdict | Commit | Notes`. If the design's original shape wins, `plan_ops.py:601` region emits `Task | Agent | Outcome | Reviewer | Commit`. Either way, `grep -E '^\| Task \| Agent' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` and the rows written by `finalize-execution-log` must agree.

**V6 — Design-doc self-consistency.**

```bash
# blockers field shape reconciled
grep -c '"blockers"' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
# should match one form; no mixed examples
```

Inspect §7.2 and §15.2 manually; both describe the same shape.

**V7 — Design-doc §9.1, §8.3, §9.3 are filled in.**

```bash
grep -n 'skip-analysis' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md   # §9.1 note reconciled with ISSUE-020 / schedule write
grep -n 'retry forwards' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md  # §8.3 addition
grep -n '.schedule.json' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md  # §9.3 adds the persistence bullet
```

**V8 — No lingering drift in SKILL.md / templates.**

```bash
# No references to task_id / batch_index as field names in skill/templates prose
grep -nE 'task_id|batch_index' .claude/skills/implement-plan/*.md || true
```

If matches remain, they must be in helper CLI examples where `--task-id` is the flag name (that's fine), not JSON field references.

---

## Tasks

### TASK-001: Canonicalize executor protocol contracts

- **Status:** done
- **Priority:** critical
- **Files:**
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `scripts/plan_ops.py`
  - `scripts/plan_codex_dispatch.py`
  - `scripts/codex_implement_schema.json`
  - `scripts/codex_review_schema.json`
  - `.claude/agents/plan-analyst.md`
  - `.claude/agents/plan-implementer.md`
  - `.claude/skills/implement-plan/SKILL.md`
  - `.claude/skills/implement-plan/run-log-schema.md`
  - `.claude/skills/implement-plan/dispatch-templates.md`
  - `tests/scripts/test_plan_ops.py`
  - `tests/scripts/test_plan_codex_dispatch_integration.py` (only if implementer report helpers are exercised)
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - Canonical vocabulary documented in exactly one place; every other file references that place.
  - ISSUE-001, 009, 013, 014, 016, 022, 024, 025, 026 are either resolved or explicitly absorbed into the canonical contract with a compatibility alias + test.
  - Backward-compatibility aliases, if any, are called out in `DUAL_AGENT_PLAN_EXECUTOR.md` and covered by a test in `tests/scripts/test_plan_ops.py`.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops.py` is green after the test-suite migration to the canonical field names.
  - Verification checks V1–V8 above all pass.

**Description:**
Establish one authoritative executor protocol. All later validation, gating, and operational safety behavior depends on this contract being singular and enforceable.

**Implementation notes:**
This is the contract foundation. Do not allow "close enough" variants to survive undocumented. If an alias is kept (e.g. to avoid churning 33 tests in one pass), declare it explicitly at the top of `plan_ops.py` as a named constant and cover it with a dedicated regression test. Do not silently accept two shapes.

**Reversion guidance:**
Restore prior protocol-bearing files only if the unified contract blocks all execution and no compatibility or migration path exists. Keep the decision record (see "Contract decisions" section below) so a revert can be scoped to one line per decision rather than a big-bang rollback.

---

## Implementation Playbook

Work top-down so later edits can cite the canonical decisions:

### Step 1 — write down the decisions

Append a "Canonical Contract (v1)" subsection to `DUAL_AGENT_PLAN_EXECUTOR.md` §5 (plan schema) or §9 (execution protocol). It should list, as a decision table:

| Concept | Canonical | Compatibility alias |
|---|---|---|
| Task status | `pending` | none — migrate fixture + tests to `pending` |
| Analyst task field | `id` | `task_id` accepted with warning; scheduled for removal |
| Analyst batch field | `index` | `batch_index` accepted with warning; scheduled for removal |
| Implementer concerns | `**Concerns for reviewer:**` | `**Concerns:**` accepted; scheduled for removal |
| Implementer plan-adaptations | `**Plan adaptations:**` | none |
| Execution-log columns | `Task \| Agent \| Reviewer \| Verdict \| Commit \| Notes` | none (promote into design) |
| Schedule persistence path | `docs/plans/<basename>.schedule.json` | none |
| Schedule persistence owner | `plan_ops.py write-schedule` (TASK-004) | orchestrator direct-write forbidden post-TASK-004 |
| `blockers` field shape | array of strings (§7.2) | delete §15.2 object form |

The decision table is where every other chunk checks the canonical shape. Do not put decisions in helper code comments.

### Step 2 — `scripts/plan_ops.py` edits

- Line 40 `ALLOWED_TASK_STATUSES`: replace `"open"` with `"pending"`. If keeping `"open"` as an alias, add a `STATUS_ALIASES = {"open": "pending"}` dict and apply it wherever status is read/written from the plan markdown.
- Line 250 `parse-schedule` required-field check: accept `id` as primary; accept `task_id` as alias only if the alias decision is kept. Emit a `warnings` list in the result when alias is used.
- Line 263 batch required-field check: same — accept `index` primary, optional `batch_index` alias.
- Line 301, 322-328 `batch-next`: read `id` / `index` primary. Touch every `_normalize_task_id(str(t.get("task_id")))` call site; the replacement is `t.get("id") or t.get("task_id")` when alias is active, else `t.get("id")`.
- Line 391 `parse-implementer-report`: change the `Concerns` label literal to `"Concerns for reviewer"`. If alias retained, try the new label first then fall back; emit a `warnings` entry on fallback.
- Line 354-403 region: add a `plan_adaptations` extraction block. Mirror the bullet-list pattern already used for `concerns` (each `-` bullet becomes one string).
- Line 601 region `finalize-execution-log`: keep current richer shape; do NOT modify the writer. The design doc gets updated in Step 5 to match.

### Step 3 — `scripts/plan_codex_dispatch.py` edits

- No field-name changes here — this file does not consume the analyst schedule, only the dispatch envelope. Confirm the envelope shape in `make_envelope` still matches `codex_implement_schema.json`. No action required beyond confirmation grep.

### Step 4 — schemas

- `scripts/codex_implement_schema.json:30-33` (blockers): already `array[string]`. No change.
- `scripts/codex_review_schema.json`: confirm unchanged; no field relates to this chunk.

### Step 5 — `DUAL_AGENT_PLAN_EXECUTOR.md` edits

Apply in this order so cross-references resolve:

1. Add the "Canonical Contract (v1)" subsection from Step 1.
2. §224 (execution-log table): change columns to `Task | Agent | Reviewer | Verdict | Commit | Notes` matching the writer.
3. §7.2 line 508: keep `"blockers": []` (no change).
4. §15.2 lines 1134-1136: delete the object-form example; reference §7.2.
5. §9.1 line 751 `--skip-analysis` note: rewrite to say "deferred; blocked by `plan_ops.py write-schedule` (TASK-004)".
6. §8.3 Codex-implements retry row: add "reviewer findings are not forwarded to the retry; v1 accepts the tradeoff of identical retries failing identically, in exchange for bounded retry scope".
7. Add §9.3 bullet: "Analyst schedules persist at `docs/plans/<basename>.schedule.json`. `plan_ops.py write-schedule --schedule-file <path> --stdin` is the sole writer; SKILL.md must never write this file directly (delivered in TASK-004)."

### Step 6 — `.claude/` prose edits

- `.claude/agents/plan-analyst.md`: already emits canonical shape. Verify examples at lines 270-306 still use `id`/`index`; no edit expected. Add a one-line callout near the JSON schema that the orchestrator pipeline (`plan_ops.py`) accepts both `id` and `task_id` during the v1 alias window (if alias retained).
- `.claude/agents/plan-implementer.md`: already emits `**Concerns for reviewer:**` and `**Plan adaptations:**`. No change. Add a one-line note that `plan_ops.py parse-implementer-report` now extracts plan adaptations.
- `.claude/skills/implement-plan/SKILL.md`: no field-name changes; update any example JSON blobs to use `id`/`index`. Grep `task_id` / `batch_index` in the file; the only matches should be helper CLI flag names (`--task-id`), not JSON field names.
- `.claude/skills/implement-plan/dispatch-templates.md`: grep same. No expected changes.
- `.claude/skills/implement-plan/run-log-schema.md`: confirm execution-log row shape matches the design-doc change from Step 5.

### Step 7 — test migration

`tests/scripts/test_plan_ops.py`: 8 sites enumerate schedule JSON with `task_id`/`batch_index`. Migrate all to `id`/`index`:

- lines 334, 341, 349, 350 — task fields
- lines 369, 370, 436, 437 — batch fields

If alias is retained, add two dedicated tests (`test_schedule_id_alias_accepted`, `test_schedule_index_alias_accepted`) that feed the legacy shape through and assert warnings surface in the result.

Add test cases:
- `test_parse_implementer_report_concerns_for_reviewer_label` — new label is extracted.
- `test_parse_implementer_report_plan_adaptations_extracted` — new field returned.
- If alias retained: `test_parse_implementer_report_concerns_legacy_alias` — old label still works with warning.

### Step 8 — CI sanity

Run `venv/bin/pytest -q tests/scripts/test_plan_ops.py` end-to-end. Expected: all tests green. If any unrelated test breaks, it is surfacing drift, not a regression — fix it inside this chunk.

---

## Out of Scope (to prevent overreach)

- **Wrapper state isolation** (baseline-scoped cleanup): TASK-003. Do not touch `validate_scope` / timeout cleanup / review post-check in this chunk even though they live in `plan_codex_dispatch.py`.
- **Schedule persistence subcommand** `write-schedule`: TASK-004. This chunk adds the design-doc bullet reserving the subcommand's path and owner; the implementation is TASK-004.
- **Phase gates** and **self-audit**: TASK-005, TASK-007.
- **Sample fixture rewrite**: TASK-006. This chunk changes neither `sample_phase4.md` nor the analyst's required-field heuristic.
- **Preflight / portability**: TASK-008.
- **Scale-aware reads / global locks / bounded log handling**: TASK-009, 010, 011.
- **Runtime validation** at each seam (halt-on-invalid behavior): TASK-002. This chunk establishes *what* the shape is; TASK-002 enforces the shape.

## Reversion guidance

Per-decision revert table:

| Revert | Effect |
|---|---|
| Restore `ALLOWED_TASK_STATUSES = {"open", ...}` | Re-breaks fixture alignment; TASK-006 must also revert. |
| Restore `task_id` / `batch_index` in `plan_ops.py` | Re-breaks analyst→parse-schedule handoff; scenario 2 and 7 fail again. |
| Restore `**Concerns:**` parser literal | Silently drops implementer concerns. |
| Restore design §15.2 object `blockers` example | Doc becomes self-contradictory again. |

Do not restore the pre-chunk state as a whole; scope the revert to the single decision that broke. The decision table in Step 1 exists so the revert is auditable line-by-line.
