# `plan_ops.py` cheat sheet

Purpose: one-page CLI summary of every `plan_ops.py` subcommand the orchestrator uses, in lifecycle order. Read this after a context compaction or as a first lookup instead of `--help` — it rehydrates the CLI vocabulary in a single tool call.

Each block lists the subcommand, the critical flags, and one copy-pastable example. Placeholder substitution: `<plan-file>`, `<plan-dir>`, `<run-id>`, `<task-id>`, `<sha>`, `<run-log>`, `<run-lock>`, `<schedule-file>`. For full payloads + JSON shapes see SKILL.md `## Command reference`.

For every flag-shape lookup post-compaction, **prefer this file over `--help`** — it's faster and there's no argparse cost.

`$PYTHON` is bound by Phase 0 preflight; pre-bind use `python3`.

---

## Lifecycle order

### preflight — dirty-tree probe + `$PYTHON` bind + `run_id`
```
$PYTHON plan_ops.py preflight --plan-file <plan-file> --json [--strict-branch]
```

### path-info — bind `<plan-dir>` / `<run-log>` / `<run-lock>` placeholders
```
$PYTHON plan_ops.py path-info --json
```

### parse-schedule — validate analyst JSON shape (pipe analyst stdout)
```
echo "$ANALYST_JSON" | $PYTHON plan_ops.py parse-schedule --stdin --strict
```

### compute-schedule — recompute disjoint batches from `tasks[]`
Standalone helper for direct callers; not part of `/implement-plan` Phase 1 anymore.
```
echo "$TASKS_JSON" | $PYTHON plan_ops.py compute-schedule --stdin --strict
```

### write-schedule — atomic persist; refuses on validation error
```
echo "$SCHED_JSON" | $PYTHON plan_ops.py write-schedule --schedule-file <schedule-file> --stdin --strict
```

### acquire-lock / release-lock — per-plan-file run lock
```
$PYTHON plan_ops.py acquire-lock --plan-file <plan-file> --run-id <run-id>
$PYTHON plan_ops.py release-lock --plan-file <plan-file> --run-id <run-id>
```

### batch-next — pick next batch respecting file locks
```
$PYTHON plan_ops.py batch-next --schedule-file <schedule-file> --locked-files "" --done "" --failed "" --parallel 2
```

### parse-implementer-report — extract structured fields from markdown
```
cat report.md | $PYTHON plan_ops.py parse-implementer-report --stdin
```

### parse-plan-review-report — validate full envelope (NOT bare parsed)
```
echo '{"subcommand":"plan-review","outcome":"success","parsed":{...}}' | $PYTHON plan_ops.py parse-plan-review-report --stdin
```

### parse-d5-adjudication — adjudicate D.5 verdict; pass codex findings count
```
echo '{"verdict":"ship-with-fixes","summary":"...","load_bearing":[],"dismissed":[]}' \
  | $PYTHON plan_ops.py parse-d5-adjudication --stdin --codex-findings-count 3
```

### commit-task — guard + status flip + narrow `git commit --only`
```
$PYTHON plan_ops.py commit-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --files "a.py,b.py" --title "feat(TASK-NNN): <title>" --diff-summary "<one-liner>" \
  --reviewer codex --reviewer-verdict minor-findings \
  --reviewer-minor-findings '[{"severity":"minor","confidence":"high","file":"a.py","line":12,"issue":"...","suggested_fix":"..."}]'
```
D.5-disagreement variant: `--reviewer claude --reviewer-verdict <ship|ship-with-fixes|partial-agreement> --disagreement-tag` (D.5's verdict, NOT Codex's `needs-rework`).

### fail-task — restore + status → failed + run-log `failed`
```
$PYTHON plan_ops.py fail-task --plan-file <plan-file> --task-id <task-id> --run-id <run-id> \
  --stage implement --files "a.py" --reason "<short>"
```

### block-dependents — cascade `blocked` onto dependents
```
$PYTHON plan_ops.py block-dependents --schedule-file <schedule-file> --plan-file <plan-file> --failed <task-id> --run-id <run-id>
```

### log-event — append JSONL with tail re-verify (uses `--fields-json`, not `--payload`)
```
$PYTHON plan_ops.py log-event --event implement_start --fields-json '{"task_id":"<task-id>","run_id":"<run-id>","plan_file":"<basename>"}'
```

### update-plan-header — flip top-level `**Status:**`
```
$PYTHON plan_ops.py update-plan-header --plan-file <plan-file> --status complete
```

### finalize-execution-log — append §5 markdown table
```
$PYTHON plan_ops.py finalize-execution-log --plan-file <plan-file> --run-id <run-id> \
  --starting-sha <sha> --ending-sha <sha> \
  --rows-json '[{"task":"001","agent":"claude","reviewer":"codex","verdict":"clean","commit":"<sha>","notes":""}]' \
  --outcome success
```

---

## Helpers

### normalize-task-id — canonicalize to `^\d{3}[A-Z]?$`
```
$PYTHON plan_ops.py normalize-task-id --id TASK-001
```

### filter-schedule — restrict by `--task-ids`
```
$PYTHON plan_ops.py filter-schedule --schedule-file <schedule-file> --task-ids 001,002
```

### reconcile-batch — reconcile observed out-of-scope writes
```
$PYTHON plan_ops.py reconcile-batch --schedule-file <schedule-file> --batch-index 0 --observed "a.py,b.py"
```

### check-plan-deps — resolve cross-plan prerequisites
```
$PYTHON plan_ops.py check-plan-deps --plan-file <plan-file>
```

### gates / audit — phase-gate predicates / self-audit
```
$PYTHON plan_ops.py gates --check schema-valid,schedule-valid
$PYTHON plan_ops.py audit --json
```

---

For full JSON payload shapes, `ALLOWED_LOG_EVENTS` enumeration, and the D.5-disagreement verdict-flattening explanation, see `SKILL.md` `## Command reference`.
