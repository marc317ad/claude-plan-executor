---
task_id: "006"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "CLI Surface item 5; Concision Rules prose-budget enforcement"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-006
depends_on: ["005"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-006 — Implement validate-output subcommand (prose-budget + coverage gates)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** CLI Surface item 5
**Base branch:** main
**Chunk dependencies:** 005

---

## Goal

Add `validate-output` as the hard-gate linter invoked on the staging dir before `commit-swap`. It enforces plan-analyst required fields, ID/status/priority vocabularies, `task_type` vocabulary, dependency resolution, no cycles, INDEX schema, parent-plan reference resolution, AND the two concision gates (hard prose-budget → error; soft → warning; prose-decision coverage).

## Scoped Context

`validate-output` is the last line of defence before production paths mutate. It MUST detect any render-side drift: e.g., a bug that emits the maximal template without elision is caught by the prose-decision-coverage check (every emitted TASK needs ≥1 `prose_*` entry in manifest history). Re-running the hard prose-budget check here is deliberate — it catches bugs where render-side budget accounting disagrees with what actually lands on disk.

Cross-plugin status rule: frontmatter uses title-case (`Pending/…`), body `- **Status:**` bullet uses lowercase (`open/…`). Both must validate against their separate vocabularies.

`--json` flag emits `{valid: bool, errors: [...], warnings: [...]}` where `prose-budget-soft` lands in `warnings` and never fails validation.

## Verification

- `validate-output --tasks-dir <dir> --plan-dir <dir>` exits 0 on a clean staging produced by TASK-005.
- Hand-corrupting a TASK (remove `- **Test command:**` line) causes non-zero exit naming the missing field.
- Hand-creating a TASK with 500 tokens implementer-facing body → exit non-zero with `prose-budget-exceeded`.
- Hand-creating a TASK with 250 tokens implementer-facing body → exit 0 with one `prose-budget-soft` entry in `warnings[]`.
- `_manifest.json` with any emitted `task_id` missing a `prose_*` reason code → exit non-zero with `prose-decisions-missing`.

---

## Tasks

### TASK-006: Implement validate-output subcommand

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add `validate-output` handler + every gate check
- **Dependencies:** 005
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "validate_output or prose_budget" -x`
- **Acceptance criteria:**
  - Required-field check passes only when every TASK body includes all 7 plan-analyst fields (`Status`, `Priority`, `Files`, `Test command`, `Acceptance criteria`, `Description`, `Reversion guidance`).
  - `task_id` regex `^\d{3}[A-Z]?$` enforced per file and per `00_INDEX.json.chunks[]`.
  - Frontmatter `status` validated against title-case vocabulary; body `- **Status:**` bullet validated against lowercase vocabulary; mismatch is an error.
  - `priority` validated against `critical|high|medium|low`.
  - `task_type` validated against `{standard, gate}`; absent field treated as `standard` (backwards-compat).
  - `00_INDEX.json` schema validated; `chunks` length == TASK files on disk; all referenced `task_id` values resolve.
  - Dependency resolution: every `depends_on` entry refers to a known task_id; no cycles.
  - Parent-plan reference (`../<slug>.md`) resolves to an existing file when the inline link is present.
  - Hard prose-budget: implementer-facing tokens > 400 (standard) / > 60 (gate) → error `prose-budget-exceeded` naming TASK id + count + ceiling.
  - Soft prose-budget: 200–400 (standard) / 40–60 (gate) → warning `prose-budget-soft`; exit 0.
  - Coverage: every emitted `task_id` has ≥1 `prose_*` reason code in `_manifest.json.history[].prose_decisions[]`; missing → error `prose-decisions-missing`.
  - `--json` emits `{valid, errors, warnings}`.

**Description:**
Final pre-commit gate. Exits non-zero on any structural or concision violation; staging survives so the operator can inspect. This is what prevents any render drift from reaching production paths.

**Reversion guidance:**
Revert the `validate-output` handler hunk in `decomp_ops.py`.

---

## Implementation Playbook

1. Iterate `<tasks_dir>/TASK-*.md` → parse frontmatter + `## Tasks` body block.
2. Check each plan-analyst-required field (present + non-empty).
3. Validate both status vocabularies on their respective fields.
4. Load `00_INDEX.json`; cross-check `chunks[]` against file list.
5. Build DAG from `depends_on`; run cycle detector (reuse helper from TASK-004).
6. Resolve parent-plan path when inline link present.
7. Count implementer-facing tokens per TASK (reuse `count_prose_tokens`).
8. Load `_manifest.json`; check every emitted `task_id` has a prose reason code in history.
9. Emit `{valid, errors, warnings}`; exit non-zero iff `errors` non-empty.
