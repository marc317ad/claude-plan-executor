---
task_id: "005"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "CLI Surface item 4; Templates; Concision Rules"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-005
depends_on: ["004"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-005 — Implement render subcommand + 4 template files with concision elision

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Templates; Concision Rules; Agent workflow step 11
**Base branch:** main
**Chunk dependencies:** 004

---

## Goal

Write the four template files (task body, `00_INDEX.md`, `00_INDEX.json`, `plan-decomposer.json` already stubbed in TASK-003) and add the `render` subcommand that consumes a schedule object, applies Concision Rules elision, writes to a staging dir, and emits the list of files written.

## Scoped Context

`render` writes to staging only — `<tasks_dir>/.staging-<run_id>/` and `<plan_dir>/.staging-<run_id>/<slug>.md`. No production paths are touched; `commit-swap` (TASK-007) performs the atomic transition.

Concision elision (step 11a–e) uses the Section Conditionality table: evaluate each row, drop the section if its omit-condition holds, append the corresponding `prose_omitted_*` reason code to `_manifest.json.history[-1].prose_decisions[]` keyed by `task_id`. For `task_type: gate`, always slim-render (Goal + Tasks + Verification only) and record `prose_omitted_gate`.

Hard budget ceilings (400 standard / 60 gate tokens of implementer-facing body, excluding code fences + YAML) MUST be enforced during render — exceeding aborts with `prose-budget-exceeded` leaving staging intact for inspection (never calls `commit-swap`).

Overlap detection: when Description and Implementation Playbook paraphrase the same source-plan step, merge into Description, drop Playbook, record `prose_merged_overlap`.

Parent-plan inline link (`**Parent plan:** [...]`) is OMITTED by default; emitted only when the TASK depends on a cross-cutting invariant not inferable from its own files+acceptance — that decision comes from the schedule's `cross_cutting_dep` flag (set by `build-schedule` based on complexity scoring).

`00_INDEX.json` schema: `{schema_version:1, source:"plan-decomposer", chunks:[{task_id, v3_task, file, priority, issues_absorbed:[], depends_on, status, superseded_by}]}`. Topological order, byte-for-byte matches the DUAL_AGENT_Plans exemplar.

## Verification

- `render --schedule-file <s> --templates-dir <t> --output-dirs-json <j>` produces staging dirs containing all expected files; no file exists outside the staging dirs.
- Rendered TASK files match the template (all plan-analyst-required fields present).
- `00_INDEX.json` parses under the frozen schema; `source == "plan-decomposer"`; chunk count == TASK file count.
- Staging files for a schedule with `task_type: gate` contain ONLY Goal + Tasks + Verification sections.

---

## Tasks

### TASK-005: Implement render subcommand + 4 template files with concision elision

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/templates/task-template.md.template` — new; maximal shape with HTML-comment audience/conditional markers
  - `plugins/plan-decomposer/templates/00_INDEX.md.template` — new; frozen section order from DUAL_AGENT_Plans
  - `plugins/plan-decomposer/templates/00_INDEX.json.template` — new; frozen schema
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add `render` handler + Concision-elision pass + staging writer
- **Dependencies:** 004
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "render" -x`
- **Acceptance criteria:**
  - All four template files exist and contain their frozen structure (see `Templates` section of parent plan).
  - `render` writes exclusively under `.staging-<run_id>` subdirs of the configured `tasks_dir` and `plan_dir`; no writes elsewhere.
  - For each TASK emitted, the appropriate `prose_omitted_*` codes land in `_manifest.json.history[-1].prose_decisions[]`.
  - Gate tasks render only Goal + Tasks + Verification; every other section is suppressed with `prose_omitted_gate`.
  - A render whose implementer-facing token count > 400 (standard) or > 60 (gate) exits non-zero with `prose-budget-exceeded` — staging dir is retained, `commit-swap` is never invoked by this subcommand.
  - Parent-plan inline link appears only when schedule marks `cross_cutting_dep: true`; otherwise omitted and `prose_omitted_self_contained` recorded.
  - `00_INDEX.json` chunks in topological order; `source == "plan-decomposer"`.

**Description:**
Produces the user-visible output in staging: TASK files, both index files, and the parent-plan copy. Every concision decision is reason-coded so the validator and Phase B audits can verify the agent actually applied the rules.

**Implementation notes:**
Token counting excludes fenced code blocks and the YAML frontmatter block. Use the pinned `count_prose_tokens` helper from TASK-002 — never roll a second tokenizer here.

**Reversion guidance:**
Delete the four template files and revert the `render` hunk in `decomp_ops.py`.
