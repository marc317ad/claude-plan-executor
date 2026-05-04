# Decomposed Plan Directory Template

Use this template when an agent or external decomposer needs to emit a plan
directory that `implement_plan.py`, `/implement-plan`, and `plan_ops.py
build-tasks` can consume.

The active workflow has two accepted inputs:

- A whole-plan markdown file with `## TASK-NNN:` blocks. `plan_ops.py
  decompose-plan` turns it into this directory shape.
- A decomposed directory containing `00_INDEX.json` plus one child markdown
  file per task. This is the native directory-mode input for
  `implement_plan.py` and the current `/implement-plan` workflow.

## Directory Shape

```text
docs/plans/<PLAN_SLUG>/
  00_INDEX.json
  TASK-001_<short_slug>.md
  TASK-002_<short_slug>.md
  TASK-003_<short_slug>.md
```

Only `00_INDEX.json` and the `TASK-*.md` children are required by the active
executor. A human `00_INDEX.md`, `_manifest.json`, or richer decomposer journal
may exist in other tooling, but the active plan-executor parser does not
require them.

Child filenames should be portable basenames. Do not put `/`, `\`, `..`, a
leading `.`, or NUL bytes in `chunks[].file`. The strict roster loader accepts
any non-empty string, but downstream routing and commit/fail/block operations
require a safe basename. The downstream schedule uses the basename as
`tasks[].plan_file`.

## `00_INDEX.json`

Minimum compatible roster:

```json
{
  "schema_version": 1,
  "source": "decompose-plan",
  "plan_title": "<Plan title>",
  "source_plan_file": "<original-plan.md>",
  "created": "YYYY-MM-DD",
  "base_branch": "main",
  "depends_on_plans": [],
  "supersedes": [],
  "chunks": [
    {
      "task_id": "001",
      "file": "TASK-001_<short_slug>.md",
      "priority": "high",
      "depends_on": [],
      "status": "Pending",
      "superseded_by": []
    },
    {
      "task_id": "002",
      "file": "TASK-002_<short_slug>.md",
      "priority": "medium",
      "depends_on": ["001"],
      "status": "Pending",
      "superseded_by": []
    }
  ]
}
```

Required roster fields for the active executor:

- Top level: `schema_version: 1`, `chunks: []`.
- Per chunk: `task_id`, `file`, `depends_on`, `status`, `superseded_by`.
- `task_id` and every `depends_on` / `superseded_by` value must be normalized:
  three digits with an optional single uppercase suffix, such as `001` or
  `004A`.
- `status` must be one of `Pending`, `Done`, or `Superseded`.
- `depends_on` and `superseded_by` must be arrays.
- `Superseded` chunks must have a non-empty `superseded_by`; non-superseded
  chunks must have an empty `superseded_by`.

`priority` and the descriptive top-level fields are emitted by the built-in
decomposer and are useful for humans, but the strict roster loader only needs
the required fields above.

## Child Task File

Each child file must contain exactly one intended `### TASK-NNN:` block matching
its roster `task_id`. Extra `### TASK-NNN:` blocks are tolerated with a warning
only when dispatch can target the intended task explicitly; avoid extras in
new decomposer output.

```markdown
# TASK-001 - <Task title>

## Goal

<One or two sentences describing the outcome of this child task.>

## Context

<Shared plan context and task-specific background. Use `## Scoped Context`
instead of `## Context` if the child needs a narrower context section.>

## Verification

- <How an operator or test can verify this child task after implementation.>

## Tasks

### TASK-001: <Task title>

- **Status:** pending
- **Priority:** high
- **Agent:** codex
- **Files:**
  - `path/to/file.py` (modify) - why this file is in scope
  - `tests/path/test_file.py` (create) - coverage for the behavior
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/path/test_file.py`
- **Read targets:**
  - `path/to/file.py:10-80` - existing behavior to preserve
- **Symbol targets:**
  - `path/to/file.py::ExistingClass.method`
- **Acceptance criteria:**
  - <Measurable behavioral assertion.>
  - <Specific test or command that passes.>
  - <Scope boundary, for example "no files outside the declared Files list are modified".>
- **Reversion guidance:** Revert `path/to/file.py` and delete `tests/path/test_file.py`.

**Description:**
<Two to five sentences explaining what this task changes and why. State the
system-visible or user-visible outcome. Do not merely repeat the file list.>

**Implementation notes:**
<Concrete guidance for the implementer: symbols to edit, constraints to
preserve, edge cases to cover, or known pitfalls. Keep this non-empty for
Claude-tier tasks, because plan review treats empty implementation notes as an
enrichment gap.>
```

Required child sections and fields:

- Top-level sections: `## Goal`, either `## Context` or `## Scoped Context`,
  and `## Verification`.
- Wrapper section: `## Tasks` is emitted by the built-in decomposer and keeps
  child files consistent, but the schema gate only requires the matching H3
  task block.
- Task heading: `### TASK-NNN: <title>`, where `NNN` matches the roster chunk.
- Required bullets: `Status`, `Priority`, `Files`, `Test command`, and
  `Acceptance criteria`.
- Required prose header: `**Description:**`.
- Strongly recommended fields: `Dependencies`, `Reversion guidance`, and
  `Implementation notes`.
- Optional targeting hints: `Read targets` and `Symbol targets`.
- Optional assignment hint: `Agent`. If omitted, the runner can classify the
  task.

Use lowercase body status values for executor-owned task state:

```text
pending, in-progress, done, failed, blocked, skipped, paused
```

This body status vocabulary is intentionally different from the title-case
`00_INDEX.json.chunks[].status` vocabulary.

## Whole-Plan Source For `decompose-plan`

When asking `plan_ops.py decompose-plan` to produce the directory, start from a
whole-plan file like this:

```markdown
# <Plan title>

**Created:** YYYY-MM-DD
**Status:** pending
**Base branch:** main

## Goal

<Overall outcome.>

## Context

<Shared background copied into generated child files.>

## Verification

<Plan-level verification notes.>

## Tasks

## TASK-001: <Task title>

- **Status:** pending
- **Priority:** high
- **Files:**
  - `path/to/file.py` (modify)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest tests/path/test_file.py`
- **Acceptance criteria:**
  - <Measurable assertion.>
- **Reversion guidance:** Revert `path/to/file.py`.

**Description:**
<Task intent and outcome.>

## TASK-002: <Task title>

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `path/to/other.py` (modify)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest tests/path/test_other.py`
- **Acceptance criteria:**
  - <Measurable assertion.>
- **Reversion guidance:** Revert `path/to/other.py`.

**Description:**
<Task intent and outcome.>
```

The built-in decomposer accepts `## TASK-NNN:` headings. If no H2 task headings
exist, it falls back to `### TASK-NNN:` headings. It requires `Priority` and
`Test command` on every source task, rejects duplicate ids, rejects short ids
such as `TASK-1`, rejects unresolved dependencies, and rejects dependency
cycles. Generated child files always include `Reversion guidance`; when the
source task omits it, the renderer emits the stable sentinel `none`.

## Validation Commands

Run these after generating or hand-authoring a decomposed directory:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py build-tasks \
  --plans-dir docs/plans/<PLAN_SLUG> \
  --json
```

```bash
for child in docs/plans/<PLAN_SLUG>/TASK-*.md; do
  venv/bin/python plugins/plan-executor/scripts/plan_ops.py gates \
    --check schema-valid \
    --plan-file "$child" \
    --json
done
```

`gates --check schema-valid` is a single-file check. Directory aggregation is
used by `gates --certify`, after a schedule file exists.

For scoped execution, verify the closure that `--task-ids` will select:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py index-closure \
  --plans-dir docs/plans/<PLAN_SLUG> \
  --task-ids 002,003 \
  --json
```

Then run the plan:

```bash
venv/bin/python plugins/plan-executor/scripts/implement_plan.py \
  docs/plans/<PLAN_SLUG> \
  --task-ids 002,003 \
  --provider-preference codex,claude,gemini \
  --reviewer gemini \
  --unattended-revert-policy pause
```

## Notes For Decomposer Agents

- Keep each task scoped to the smallest coherent file set.
- Put shared context in the child top-level `## Context` section, not only in
  the source parent plan.
- Keep `Files:` exhaustive. Commit safety and wrapper scope checks use this
  list.
- Prefer explicit test commands. Use `none` only when no meaningful command
  exists, and explain the manual verification in `Acceptance criteria`.
- Keep `00_INDEX.json.chunks[].depends_on` and the child `Dependencies` bullet
  identical. In directory mode, the roster is the topology source of truth.
- Use `Read targets` and `Symbol targets` for large files so implementers read
  the relevant code before editing.

## Current Source Of Truth And Refactor Guidance

The decomposer already has most of this information, but it is fragmented:

- `plugins/plan-executor/scripts/plan_ops.py` owns the executable grammar:
  `_task_header_re`, `_extract_metadata_field`, `_extract_bullet_list`,
  `_parse_task_block`, `_decompose_plan`, `_render_child_task_file`,
  `_parse_index_roster`, `_build_tasks`, and `_gate_schema_valid`.
- `plugins/plan-executor/templates/TASK.md.template` covers only the reusable
  `### TASK-NNN:` block.
- `docs/plans/build-plan-decomposer-plugin.md` documents a richer historical
  decomposer plugin shape, including `00_INDEX.md`, `_manifest.json`, and more
  status values. That plugin is not present in this checkout and its richer
  shape is not the active executor contract.

Recommended refactor:

- Keep `plan_ops.py` as the runtime authority.
- Move the rendering strings used by `_render_child_task_file` toward reusable
  template assets under `plugins/plan-executor/templates/`, or generate this
  document from the same constants used by the parser.
- Add a small drift test that validates this root template mentions the current
  required roster fields, task fields, body status vocabulary, index status
  vocabulary, and validation commands.
