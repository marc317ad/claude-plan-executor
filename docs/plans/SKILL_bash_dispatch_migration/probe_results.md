# v3 Claude wrapper envelope — key reference

Short reference tying each top-level key of the v3 Claude-dispatch envelope
emitted by `plugins/plan-executor/scripts/plan_claude_dispatch.py` (the
"wrapper") to its emission site in the wrapper source. Downstream tasks in
`PLAN_SKILL_bash_dispatch_migration` lock against this exact key set; the
canary test `tests/scripts/test_claude_dispatch_canary.py` asserts the live
wrapper still emits every key listed here on both dry-run and live paths.

The canonical key set is defined in
`tests/scripts/test_claude_dispatch_canary.py:V3_REQUIRED_TOP_LEVEL_KEYS`.
Read that constant as the source of truth — the table below is a
human-readable companion, not a redefinition.

| Key | Type | Emission site | Notes |
| --- | --- | --- | --- |
| `schema_version` | int (`1`) | `_dry_run_envelope` (≈L540) and the post-spawn envelope path in `cmd_run` (≈L820) | Protocol-version pin. Bumped on breaking envelope changes; consumers MUST reject unknown versions rather than coerce. |
| `status` | string enum (`ok`, `error`, `timeout`, `nonzero-exit`, …) | `_dry_run_envelope` (≈L553) and `cmd_run` post-spawn (≈L825 / L845) | Universal commit-forbidding sentinel: anything other than `ok` MUST block `commit-task` for the dispatched task. Drives `_exit_code_for_status` (L518). |
| `status_reason` | string \| null | Same emission sites as `status` | Free-form diagnostic. Dry-run carries the literal token `"dry_run: true"` (L553) so the canary can detect dry-run mode without inspecting CLI flags. Null on a clean `ok` live run. |
| `agent` | string | `_dry_run_envelope` (L542) and `cmd_run` post-spawn | Echoes `manifest["name"]` from the resolved agent manifest (`plan-analyst`, `plan-implementer`, `plan-remediator`, …). Used by the orchestrator to dispatch envelopes to the right parser. |
| `model` | string \| null | `_dry_run_envelope` (L543) and `cmd_run` post-spawn | Echoes `manifest["model"]` or the `overrides.model` override. May be `null` if the manifest omits it and no override is supplied. |
| `session_id` | string \| null | `cmd_run` (L827 / L846 / L901) | Forwarded from the inner Claude session. `null` on dry-run. Lets the orchestrator correlate envelopes with `_run_log.jsonl` spans. |
| `duration_ms` | int \| null | `cmd_run` (L828 / L847 / L902) | Wall-clock subprocess duration. `null` on dry-run. |
| `cost_usd` | number \| null | `cmd_run` (L829 / L848 / L903) | Reported by the underlying Claude session. **May be `null` on dry-run** and on any run where the CLI does not surface cost telemetry. Consumers MUST treat `null` as "unknown", not "zero". |
| `tokens` | object \| null | `cmd_run` post-spawn | Token-usage breakdown `{input, output, cache_read, cache_creation}`. Forwarded from the Claude session; `null` on dry-run. |
| `result` | object \| null | `cmd_run` post-spawn | The inlined-schema-validated agent output (the inner JSON the agent emitted). Schema validation occurs against the inline schema from `output_instructions.schema_inline` or `schema_path`. On dry-run, `result` is plan-only / synthetic and carries no `outcome`. Parsing/validation failure flips `status` away from `ok`. |
| `result_raw_truncated` | string \| null | `cmd_run` (L832 / L851 / L905) | Tail of the raw agent stdout, populated when `result` failed to parse so operators can diagnose without re-running. `null` when `result` parsed cleanly. |
| `stderr_tail` | string \| null | `cmd_run` (L833 / L852 / L906) | Last N bytes of the spawned `claude` process stderr. Always `null` on dry-run. |
| `permission_denials` | array | `cmd_run` (L834 / L853) | Records every Claude permission denial observed during the dispatched session. Empty array (`[]`) on a clean run; non-empty entries are surfaced to the orchestrator for review. |
| `scope` | object | `_dry_run_envelope` and `_merge_scope_with_cleanup` (L335) into `cmd_run` post-spawn | Carries `declared_files_changed`, `observed_delta_tracked`, `observed_delta_untracked`, `scope_violation_detected`, `scope_misreport_detected`. Authoritative source for scope-gate enforcement; mismatches set the booleans true. |
| `trace` | object | `_build_trace` (L219) and `_stamp_trace_end` (L423) | Carries `run_id`, `span_id`, `parent_span_id`, `depth`, `call_chain`, `started_at`, `ended_at`. Mirrored into `_run_log.jsonl` spans. |
| `error` | object \| null | `cmd_run` post-spawn / `_input_invalid_envelope` (L503) | Structured failure detail when `status != "ok"`. `null` on a successful run. Distinct from `status_reason` (free-form) — `error` is machine-readable. |

## Protocol invariants (cross-cutting)

- **Commit gate.** `status != "ok"` on any wrapper envelope forbids `commit-task` for the dispatched task. The orchestrator routes the envelope through `plan_ops__claude_envelope_extract` before any commit decision.
- **Dry-run signal.** Dry-run is conveyed *only* via `status_reason` containing the literal substring `dry_run` (case-insensitive). `status` remains `ok`; no `--dry-run` flag bleeds into the envelope shape.
- **Schema-version pin.** `schema_version` is currently `1`. A bump is a breaking change; downstream parsers MUST reject unknown versions outright rather than degrade.
- **64 KB size cap.** The full envelope JSON written to stdout is bounded at ≤64 KB (`ENVELOPE_SIZE_CAP_BYTES` in the canary). Oversized payloads truncate `result_raw_truncated`, never the structural keys.
- **Scope authority.** `scope.scope_violation_detected` / `scope_misreport_detected` are the sole authority for scope-gate enforcement; the orchestrator never re-derives scope from a diff.

## See also

- `tests/scripts/test_claude_dispatch_canary.py` — A/B parity probe and v3-key canary.
- `tests/scripts/fixtures/claude_dispatch/schemas/` — `analyst_result.json`, `implementer_result.json`, `remediator_result.json` inlined into `output_instructions.schema_inline`.
- `plugins/plan-executor/scripts/plan_claude_dispatch.py` — wrapper source; line numbers in the table are approximate and may drift; the canary is authoritative.
