# plan-executor templates

This directory ships canonical authoring templates for plan-executor artifacts.

## Which template to use

- **Hand-authoring a single `### TASK-NNN:` block** inside a whole-plan markdown file (or tuning a plan-decomposer that emits tasks): copy `TASK.md.template`. It is the H3-block-only authoring scaffold for hand-authored plans and remains intentionally narrow — it does not include the surrounding `## Goal` / `## Context` / `## Verification` wrapper. Adherence prevents `plan-analyst` from emitting enrichment gaps (see `plugins/plan-executor/agents/plan-analyst.md` Step 7 for the canonical gap list) and reduces the frequency of `plan-author` auto-revise round-trips. `plan-author` (TASK-025) edits plan files downstream to repair gaps, but it cannot fix what was never in the plan — upstream template adherence is the cheapest defense.
- **Authoring a decomposed-plan directory** (`docs/plans/<PLAN_SLUG>/` with `00_INDEX.json` + per-task `TASK-NNN_<slug>.md` children) consumed by `implement_plan.py`, `/implement-plan`, and `plan_ops.py build-tasks`:
  - Copy `decomposed_child.md.template` for each child file. It is the full child-file scaffold intended to mirror `_render_child_task_file` in `plugins/plan-executor/scripts/plan_ops.py` and to satisfy `_gate_schema_valid`.
  - Copy `00_INDEX.json.template` for the roster. It is the minimal scaffold matching `_parse_index_roster` (`schema_version`, `chunks`, and per-chunk `task_id`, `file`, `depends_on`, `status`, `superseded_by`).
- **Decomposer agents** authoring or generating a directory-mode plan should consult the full human guide at `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` (root-level) for the directory shape, status vocabularies, validation commands, and notes; the plugin-local templates above are the reusable assets to copy.

`plan_ops.py` remains the runtime authority for the contract. The plugin-local templates and the root directory guide must stay aligned with the parser constants — drift tests live alongside `plan_ops.py` tests.

Sibling `code-reviewer.md.template` follows the same drop-in convention.
