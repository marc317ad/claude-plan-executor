---
task_id: "003"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "decomp_ops.py CLI Surface items 1, 2, 7; Input Mode Detection Rules"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-003
depends_on: ["002"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-003 — Implement inspect + parse-source-plan + compute-fingerprint subcommands

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** CLI Surface items 1, 2, 7
**Base branch:** main
**Chunk dependencies:** 002

---

## Goal

Add the three read-only subcommands that prepare inputs for the rest of the pipeline: `inspect` (path/manifest/journal/tasks enumeration with Input Mode Detection), `parse-source-plan` (freeform plan OR TASK file → intermediate JSON tree), and `compute-fingerprint` (pure canonical fingerprint).

## Scoped Context

Input Mode Detection has 7 normative checks. The hardest requirement: any realpath landing INSIDE `<tasks_root>` but failing any of checks 3–6 MUST hard-abort — the decomposer MUST NEVER fall back to fresh mode for such paths. Error codes are part of the public contract (`refuse-to-decompose-into-own-output`, `invalid-task-frontmatter`, `task-id-filename-drift`, `task-not-in-manifest`, `refuse-task-shaped-slug`).

Config resolution order: env var `CLAUDE_PLAN_DECOMPOSER_CONFIG` → `.claude/plan-decomposer.json` in cwd → plugin default template. Malformed JSON aborts loud.

`parse-source-plan` must accept BOTH a freeform plan (extract H1, headers, phases, tables) AND a rendered TASK file (extract `## Scoped Context` + `## Implementation Playbook` + user-added deviations) — supersede mode reuses the same parser.

## Verification

- `python decomp_ops.py inspect --mode paths --input-path <some.md> --json` emits keys: `python_bin, plan_dir, tasks_root, tasks_dir, journal_file, manifest, index_md, index_json, parent_copy_path, staging_templates, input_mode` (plus `abort_code` when applicable).
- `python decomp_ops.py compute-fingerprint` with `title|file|problem` on stdin emits `sha256:<hex>` deterministically across runs.
- `python decomp_ops.py parse-source-plan --plan-file <task-file.md> --json` emits `{"mode":"task",...}` and the same command on a freeform plan emits `{"mode":"plan",...}`.

---

## Tasks

### TASK-003: Implement inspect + parse-source-plan + compute-fingerprint subcommands

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add argparse subparsers + handlers for the three commands + config loader + input-mode detector
  - `plugins/plan-decomposer/templates/plan-decomposer.json.template` — consumer config default (`plan_dir`, `tasks_root`, `base_branch`, `python_bin`)
- **Dependencies:** 002
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "inspect or parse_source or compute_fingerprint" -x` (tests land in TASK-013; short-term use `--help` sanity)
- **Acceptance criteria:**
  - `inspect --mode paths` correctly classifies input_mode per Input Mode Detection Rules (7 checks); symlinks resolved via `os.path.realpath`.
  - Any realpath inside `<tasks_root>` that fails checks 3–6 returns a non-zero exit and emits the matching `abort_code` in JSON — never `input_mode: "fresh"`.
  - `inspect --mode manifest|journal|tasks` validates schema and reports errors with non-zero exit on invalid JSON.
  - `parse-source-plan` detects input kind (plan vs TASK-file) automatically; emits intermediate tree including `{headings[], phases[], step_candidates[], file_tables[], test_matrices[]}` for plans and `{scoped_context, implementation_playbook, deviations[]}` for TASK files.
  - `compute-fingerprint` reads `title|first_file|problem_prefix` from stdin, applies `normalize_fingerprint_input`, outputs `sha256:<hex>`; identical input produces identical output across invocations.
  - Malformed `.claude/plan-decomposer.json` causes every subcommand to exit non-zero with a clear error message; missing config falls back to plugin template default.

**Description:**
These three subcommands are the strict prerequisites for schedule building: `inspect` resolves all derived paths + mode, `parse-source-plan` converts markdown to a structured tree the agent can reason over, `compute-fingerprint` provides the canonical identity hash used by reconciliation.

**Implementation notes:**
Realpath canonicalization (`os.path.realpath`) MUST run before any containment check — a symlink whose literal name is outside `<tasks_root>` but whose target is inside MUST be classified as supersede-mode candidate (per test case 25).

**Reversion guidance:**
Revert the hunk that adds the three subparsers + their handlers from `decomp_ops.py`; the file reverts to the constants-only state from TASK-002.

---

## Implementation Playbook

1. Add `load_config(cwd)` honoring env → cwd file → plugin-template fallback.
2. Add `detect_input_mode(input_path, tasks_root) -> (mode, abort_code | None)` executing checks 1–7 in order.
3. Add `inspect` subparser with `--mode {paths,manifest,journal,tasks}` dispatch.
4. Add `parse-source-plan` subparser; branch on frontmatter presence (TASK file vs plan).
5. Add `compute-fingerprint` subparser reading `|`-delimited stdin.
6. Write the `plan-decomposer.json.template` config default.
