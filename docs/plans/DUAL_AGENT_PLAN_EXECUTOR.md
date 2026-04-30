# Dual-Agent Plan Executor

Run-state source-of-truth rules live in `plugins/plan-executor/skills/implement-plan/SKILL.md#run-state-source-of-truth`; `_run_log.jsonl` is authoritative and the harness `TaskList` is a visibility mirror.

## Schedule Fields

Schedule tasks use canonical `id` fields and batches use canonical `index` fields. Legacy `task_id` and `batch_index` names are aliases only when a compatibility window explicitly says so.

## 14. Executor Self-Audit

The executor self-audit is exposed by `plan_ops.py audit --json` and `plan_ops.py audit --report-file <path>`. It verifies schema, schedule, wrapper, and documentation drift before plan execution.

Completed-Work Preservation Principle: implementation work must not be silently reverted or discarded; destructive cleanup requires an awaiting-user pause and explicit user instruction.
