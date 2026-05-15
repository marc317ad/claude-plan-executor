# plan-executor templates

This directory ships canonical authoring templates for plan-executor artifacts.

## Which template to use

- **Hand-authoring a single `### TASK-NNN:` block** inside a whole-plan markdown file (or tuning a plan-decomposer that emits tasks): copy `TASK.md.template`. It is the H3-block-only authoring scaffold for hand-authored plans and remains intentionally narrow — it does not include the surrounding `## Goal` / `## Context` / `## Verification` wrapper. Adherence prevents `plan-analyst` from emitting enrichment gaps (see `plugins/plan-executor/agents/plan-analyst.md` Step 7 for the canonical gap list) and reduces the frequency of `plan-author` auto-revise round-trips. `plan-author` (TASK-025) edits plan files downstream to repair gaps, but it cannot fix what was never in the plan — upstream template adherence is the cheapest defense.
- **Authoring a decomposed-plan directory** (`docs/plans/<PLAN_SLUG>/` with `00_INDEX.json` + per-task `TASK-NNN_<slug>.md` children) consumed by `implement_plan.py`, `/implement-plan`, and `plan_ops.py build-tasks`:
  - Copy `decomposed_child.md.template` for each child file. It is the full child-file scaffold intended to mirror `_render_child_task_file` in `plugins/plan-executor/scripts/plan_ops.py` and to satisfy `_gate_schema_valid`.
  - Copy `00_INDEX.json.template` for the roster. It is the minimal scaffold matching `_parse_index_roster` (`schema_version`, `chunks`, and per-chunk `task_id`, `file`, `depends_on`, `status`, `superseded_by`).
- **Decomposer agents** authoring or generating a directory-mode plan should consult the full human guide at `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md` (root-level) for the directory shape, status vocabularies, validation commands, and notes; the plugin-local templates above are the reusable assets to copy.

## Ownership and runtime authority

`plan_ops.py` remains the runtime authority for the contract. Every template file in this directory is a **human authoring aid** — none of them are read at runtime by `_render_child_task_file` or by any other parser entry point. `_render_child_task_file` renders from the in-module constant `_DECOMPOSED_CHILD_SCAFFOLD` in `plugins/plan-executor/scripts/plan_ops.py`; `decomposed_child.md.template` mirrors that constant so humans (and external decomposer agents) can author child files that match what the renderer would emit.

| File | Role | Mirrored runtime authority |
| ---- | ---- | -------------------------- |
| `TASK.md.template` | Human authoring aid — `### TASK-NNN:` block scaffold for whole-plan markdown files. | `_parse_task_block` (parser side; renderer is not involved). |
| `decomposed_child.md.template` | Human authoring aid — full child-file scaffold for decomposed-directory plans. | `_DECOMPOSED_CHILD_SCAFFOLD` constant, used by `_render_child_task_file`. |
| `00_INDEX.json.template` | Human authoring aid — minimal roster scaffold. | `_parse_index_roster`. |
| `code-reviewer.md.template` | Human authoring aid — per-project reviewer subagent starter. | n/a (per-project asset). |

Drift between these templates, the root guide at `templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md`, and the parser constants is pinned by `TestDecomposedTemplateDrift` in `tests/scripts/test_plan_ops.py`. If you change a status vocabulary, required bullet, or required prose header in `plan_ops.py`, the drift suite will fail until the templates and the root guide are updated to match.

Sibling `code-reviewer.md.template` follows the same drop-in convention.
