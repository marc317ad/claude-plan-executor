# Plan: `plan_ops.py` MCP Tool-Server Migration

**Created:** 2026-04-28
**Status:** Pending
**Base branch:** main
**Related:**
- `plugins/plan-executor/scripts/plan_ops.py` (~13.3K lines, 36 subcommands)
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md`
- `plugins/plan-executor/scripts/plan_codex_dispatch.py`
- `plugins/plan-executor/scripts/plan_claude_dispatch.py`
- `plugins/plan-executor/scripts/_codex_envelope_sanitizer.py`
- `docs/plans/PHASE_D_STATE_MACHINE/PLAN_PHASE_D_STATE_MACHINE.md` (Non-goal #1 named this work as the orthogonal follow-up)

---

## Goal

Expose every orchestrator-facing `plan_ops.py` subcommand as a typed MCP (Model Context Protocol) tool, reachable from the orchestrator LLM as a structured tool call instead of a bash-dispatched CLI. The wins: orchestrator stops constructing `$PYTHON … plan_ops.py …` command lines and parsing `--json` envelopes out of stdout; tool input/output is schema-validated at the MCP layer; SKILL.md sheds the ~120-line CLI reference + most worked-example boilerplate. Out of scope: any change to routing logic, schedule/run-log/commit semantics, or the bash CLI's continued availability for subprocess-internal callers (`plan_codex_dispatch.py`, `plan_claude_dispatch.py`, the test suite). Removal of the bash CLI is a separate deprecation plan.

## Context

The orchestrator currently runs ~25 distinct `plan_ops.py` subcommand invocations per task (preflight, decompose, build-tasks, parse-schedule, write-schedule, batch-next, claude-envelope-extract, parse-implementer-report, parse-plan-review-report, parse-d5-adjudication, review-route, build-claude-dispatch-input, log-event, commit-task / fail-task, gates, reconcile-batch, etc.). Each call is a `Bash` tool round-trip that costs (a) the orchestrator constructing a command line correctly (HEREDOCs for stdin payloads, `--json` placement, flag-name recall after compaction); (b) ~2–4 KB of "CLI reference" prose retained in the SKILL prompt so the orchestrator can compose the call; (c) parsing the JSON envelope back out of bash stdout. The user's standing memory ("Structured tools over bash CLI") names MCP/JSON-schema tools as the long-term target and the bash dispatch + `plan_ops_cheatsheet.md` as a v1 compromise; PHASE_D_STATE_MACHINE explicitly named this migration as orthogonal follow-up #1.

The PHASE_D refactor already paid down the prerequisite debt: every routing-relevant subcommand has a pure-function entry point (`route(payload) -> dict`, `apply_*_state_transition(...)`, `_emit` is a thin shim). The MCP migration is therefore mostly a registration + transport layer over existing pure functions. The Codex envelope sanitizer (`_codex_envelope_sanitizer.py`) and the JSON-envelope discipline carry over verbatim — MCP I/O contracts are at least as strict (every tool has a JSON Schema for input *and* output, validated by the MCP runtime, no free-text passthrough).

## 3. Design

### 3.1 MCP server scaffolding

A new module `plugins/plan-executor/scripts/plan_ops_mcp_server.py` is the MCP entry point. Library: the official Anthropic-maintained Python `mcp` SDK (already a transitive dependency of any modern Claude Code install; pin `mcp >= 1.0` in `requirements.txt` or the plugin's existing `pyproject.toml` if present, otherwise document the pin in the plugin README). Transport: stdio (matches Claude Code's MCP server convention; no network surface). Launch: a `claude_mcp_servers` block in the plugin's `.claude/settings.json` (or per-plugin equivalent) registers the server with command `["${CLAUDE_PLUGIN_PYTHON}", "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"]`. The server resolves `$PYTHON` at startup via the existing `_resolve_python()` helper — same precedence as the bash path — and exposes that as a server-side capability rather than a per-tool argument.

Server lifecycle: stateless. Each tool call is a pure function call against the existing `cmd_*` pure cores. No long-lived objects, no in-memory caches; `.schedule.json` and the run-log remain the durable state. This keeps MCP-server crashes / restarts indistinguishable from a fresh CLI subprocess.

### 3.2 Tool surface

One MCP tool per `plan_ops.py` subcommand, named `plan_ops__<subcommand_with_underscores>` (MCP tool names disallow hyphens; the bash subcommand string is preserved as a tool annotation for the drift guard). The 36-subcommand surface partitions into three tiers by orchestrator usage:

**Tier 1 — high-frequency orchestrator-facing (10 tools, lead the migration):**
`review_route`, `batch_next`, `commit_task`, `fail_task`, `parse_schedule`, `log_event`, `claude_envelope_extract`, `parse_implementer_report`, `parse_plan_review_report`, `parse_d5_adjudication`. These dominate the per-run token budget; migrating them first delivers most of the SKILL.md slimming.

**Tier 2 — medium-frequency (9 tools):**
`gates`, `audit`, `reconcile_batch`, `build_claude_dispatch_input`, `order_triage_findings`, `parse_plan_review_triage_report`, `filter_schedule`, `lint_plans`, `finalize_execution_log`.

**Tier 3 — low-frequency / one-shot / lifecycle (17 tools):**
`preflight`, `decompose_plan`, `build_tasks`, `compute_schedule`, `write_schedule`, `update_plan_header`, `normalize_task_id`, `acquire_lock`, `release_lock`, `path_info`, `list_global_lock_paths`, `resolve_read_targets`, `index_closure`, `check_plan_deps`, `block_dependents`, `auto_validate_divergence`, `run_summary`.

Each tool ships:
- `inputSchema`: JSON Schema 2020-12, mirroring the subcommand's argparse contract (required flags → required properties, enums → `enum`, choices → `enum`, JSON-string flags like `--reviewer-minor-findings` → typed arrays/objects, `--stdin` is **removed** — its payload becomes a `payload` property on the tool input).
- `outputSchema`: JSON Schema for the subcommand's `--json` envelope. Existing sidecars (`review_route_input_schema.json`, `review_route_output_schema.json`, `codex_*_schema.json`) are reused verbatim where present; new sidecars added under `plugins/plan-executor/scripts/schemas/mcp/<tool>.input.json` and `<tool>.output.json` for subcommands that lack them today.
- Description: copied from the argparse `help=` string verbatim — single source of truth.

Tools never accept free-form text fields outside their declared schemas; the MCP runtime rejects out-of-schema inputs before the handler runs. Outputs are `dict[str, Any]` and pass through the same `_codex_envelope_sanitizer.sanitize()` perimeter when they originate from a Codex envelope (e.g., `parse_implementer_report` after a Codex dispatch).

### 3.3 Backward compatibility

The bash CLI stays. Three categories of caller:

1. **Orchestrator LLM (token-cost relevant).** Migrates to MCP tools (TASK-007). Bash invocations in SKILL.md are removed in lockstep with each tier's MCP wiring.
2. **Subprocess-internal callers** — `plan_codex_dispatch.py`, `plan_claude_dispatch.py`, helper Bash sequences inside the Codex wrapper, dispatch templates that exec `plan_ops.py …`. These are token-irrelevant (they run inside subprocesses the orchestrator never sees mid-stream) and stay on the bash CLI for this plan. A future plan can convert them to in-process imports of `plan_ops` pure functions.
3. **The test suite.** `tests/scripts/test_plan_ops*.py` already drive pure-function entry points directly (post-PHASE_D); the bash CLI tests remain unchanged. Conformance tests (TASK-008) drive both surfaces against shared fixtures and assert byte-equality on the JSON envelope, locking the contract.

The MCP server and the argparse `main()` both call the same `cmd_<subcommand>(args_dict) -> envelope_dict` pure cores. No subcommand has duplicated logic: the bash entrypoint marshals argparse Namespace → dict; the MCP entrypoint marshals MCP tool input → dict; both return the canonical envelope.

### 3.4 SKILL.md changes

The orchestrator-facing `plan_ops.py` CLI reference (currently §"plan_ops.py CLI reference" in SKILL.md, ~120 lines) collapses to: a single paragraph stating "all plan operations are reachable as MCP tools `plan_ops__<name>`; tool input/output schemas are the source of truth; consult `mcp/list-tools` after a context compaction". Worked examples that exist solely to remind the orchestrator of CLI grammar (HEREDOC patterns, `--json` placement, stdin pipe shapes) are deleted. Every Phase 0–E walkthrough that currently shows `Bash: $PYTHON … plan_ops.py …` is rewritten as `Tool: plan_ops__<name> with input { … }`.

Subprocess-internal `plan_ops.py` invocations (the wrapper scripts, dispatch templates) are not orchestrator-facing and remain documented in the Bash form where they appear; the SKILL no longer needs to teach them.

**Quantified token savings (back-of-envelope):**
- CLI reference + cheatsheet redundancy removal: ~3.5–4.5 KB of SKILL prose (≈ 1 K tokens).
- Per-call: each Bash round-trip costs ~150–400 tokens of command-construction prose in the orchestrator's response stream; an MCP tool call is ~40–80 tokens of structured JSON. With ~25 tool calls per task and a typical 8-task run, savings are **~15–40 K tokens per run** in orchestrator output, before counting the input-side win from the slimmer SKILL prompt that is paid once per run.
- Compaction resilience: today's "re-grep `--help` after compaction" rule (SKILL §"Cache hygiene rules") is obsolete — MCP tool schemas are always present in tool-definition context.

### 3.5 Drift guard (TASK-009 in this plan; analog of PHASE_D TASK-006)

A test asserts: every `plan_ops.py` argparse subcommand has a corresponding registered MCP tool, and every registered MCP tool has a corresponding argparse subcommand. The PHASE_D drift guard (`tests/scripts/test_skill_cli_reference_drift.py`) is extended (not replaced) to cover the MCP registration side, so deletions on either surface fail CI. Tool-name canonicalization (`-` ↔ `_`) is via a single mapping function `_subcommand_to_mcp_tool_name()` shared by the server registration and the drift guard.

### 3.6 Migration phasing

Tier 1 ships first (TASK-004) and unlocks the SKILL.md edit (TASK-007). Tier 2 (TASK-005) and Tier 3 (TASK-006) follow and are mostly mechanical — the schema sidecars exist, the pure-function cores exist, the registration is templated. The conformance test (TASK-008) runs against whatever tier is wired at the time it executes; it is dependency-gated on Tier 3 so a single CI invocation covers the full surface. The end-to-end smoke (TASK-010) drives a one-task fixture plan **without touching `Bash plan_ops.py …`** — every plan operation flows through MCP — proving the orchestrator no longer needs the CLI.

## 4. Non-goals

- No behavioral changes to any subcommand: routing logic, commit-tag composition, schedule-state transitions, run-log event vocabulary, gate predicates, audit checks, sanitizer perimeter. Pure transport-layer migration.
- No schema-breaking output changes. MCP `outputSchema` mirrors today's `--json` envelope shape one-to-one. If a current envelope has a quirk (e.g., string-encoded JSON inside a string field), the quirk is preserved — fixing it is a separate plan.
- No removal of the bash CLI. Subprocess-internal callers, the test suite, and any operator who runs `plan_ops.py` from a terminal continue to work unchanged. Deprecation is a follow-up.
- No conversion of `plan_codex_dispatch.py` / `plan_claude_dispatch.py` to MCP tools. They are wrappers around external dispatch (Codex CLI / Claude SDK), not plan-state operations; their migration is orthogonal.
- No change to Phase 1.5 (plan-review) flow, the Codex envelope sanitizer perimeter, or the dispatch templates beyond the SKILL-side wording shift from "Bash" to "Tool".
- No new MCP servers beyond the `plan_ops` one in this plan.
- No support for stateful MCP capabilities (subscriptions, resources, prompts) — tools only. A future plan can layer stateful MCP capabilities on top.

## Verification

- Per-task unit tests (see each TASK block) cover the new server scaffolding, schema sidecars, registration, and tier-by-tier tool wiring.
- TASK-008 conformance test drives every MCP tool against a shared fixture set and asserts the MCP-emitted envelope is byte-equal to the bash-CLI `--json` envelope on the same input. This is the contract lock.
- TASK-009 drift guard fails CI on any argparse↔MCP registration mismatch, and on any SKILL.md reference to a `plan_ops__<name>` tool that does not exist.
- TASK-010 end-to-end smoke runs a one-task fixture plan with the orchestrator path mocked to use MCP tools only (no `Bash plan_ops.py` invocations); asserts the run-log under `_run_log.jsonl` matches the byte-baseline produced by an equivalent bash-CLI run.
- Manual acceptance: one real plan run with `--dry-run` and one for-real, both via MCP. The execution-log table and run-log deltas must match the pre-migration baseline; new server-startup events (`mcp_server_start`, `mcp_tool_called{tool}`) are the only allowed additions.

## 6. Execution — sequential and parallel batches (v3, post-2026-04-30 consolidation)

The original 7-task tail (TASK-004 through TASK-010) and the 3-task `plan_ops.py` refactor split (TASK-011/012/013) are absorbed into a 5-task post-codemod sequence (TASK-014 through TASK-018). The consolidation is enabled by `PURE_CORE_CODEMOD` (sub-plan at `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md`) which ships 38 `_run_<sub>(payload) -> dict` pure cores + 38 `_args_to_payload_<sub>` shim helpers + 47-49 conformance fixtures via deterministic AST rewrite — eliminating the per-tier complexity gradient that motivated the original split. See the v3 consolidation note at the top of §Tasks for the rationale and per-task mapping.

Five dispatchable tasks (`00_INDEX.json` chunks: TASK-001 / 003 / 014–018); TASK-001 and TASK-003 already shipped 2026-04-29 in commits `890b09b` / `9f174d5`. TASK-002, TASK-004 through TASK-010, and TASK-011 through TASK-013 are retained in this markdown for audit but absent from `chunks[]`. The codemod sub-plan is wired via `depends_on_plans` in the parent `00_INDEX.json` so the orchestrator's `check-plan-deps` blocks parent-plan dispatch until the codemod completes.

- **Batch 1 (already shipped 2026-04-29):** TASK-001 (server scaffolding) and TASK-003 (schema sidecars).
- **Cross-plan precondition:** PURE_CORE_CODEMOD must complete (delivers 38 `_run_*` cores + Tier-A/B/C fixture pack). Wired via `depends_on_plans` in this plan's `00_INDEX.json`.
- **Batch 2:** TASK-014 (MCP tool registration via `_index.json`-driven codegen). Single dispatchable task replacing TASK-004/005/006.
- **Batch 3 (parallel):** TASK-015 (SKILL.md migration, depends on TASK-014) and TASK-016 (MCP↔CLI conformance test, depends on TASK-014). Different files (SKILL.md vs new test file); parallel-safe.
- **Batch 4:** TASK-017 (drift guard, depends on TASK-014 + TASK-015).
- **Batch 5:** TASK-018 (end-to-end smoke, depends on TASK-015 + TASK-016).

Total: 5 dispatchable tasks across 5 batches (1 already shipped + 4 post-codemod). The original 12-task / 8-batch structure is reduced by ~58% on dispatch count; ~50% on LLM-implementer dispatch budget (the 36 tool-registration LLM passes become a single codegen script; the parent's "errors[]-producing input per tool" requirement is reframed to reuse the codemod's existing fixtures plus per-tool semantic-invalid additions only where `_run_*` has body-level error paths).

The serialization risk that motivated the TASK-011→012→013 chain (wrapper's parallel-batch baseline race against the same `plan_ops.py`) is moved into the codemod sub-plan and applies there only; the parent-plan tail no longer mutates `plan_ops.py` so parallel-batch dispatch is safe within the constraints noted per-batch above.

---

## Tasks

### v3 consolidation note (2026-04-30) — TASK-004..010 + TASK-011..013 superseded by TASK-014..018

The original 7-task tail (TASK-004 through TASK-010) and the 3-task `plan_ops.py` pure-core split (TASK-011 / 012 / 013) are absorbed into 5 post-codemod tasks (TASK-014 through TASK-018). Two consecutive scope-bloat halts during /implement-plan dispatch (TASK-002 run `20260429T232737`, TASK-011 run `20260430T033005`) surfaced that the original task structure under-estimated implementer scope on the `_run_*` extraction surface. The user pass on 2026-04-30 produced the `PURE_CORE_CODEMOD` sub-plan (`docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md`) which replaces the LLM-implemented refactor with a deterministic AST-rewrite codemod, and a parent-plan consolidation that reduces the post-refactor surface from 7 LLM-implemented tasks to 5 (with one being a codegen script and two being parametrized harnesses).

**Mapping:**

| Old | Status | New | Rationale |
|---|---|---|---|
| TASK-002 | Superseded (was 2026-04-29) | PURE_CORE_CODEMOD/TASK-001..006 | Original LLM refactor → deterministic codemod. |
| TASK-011 | Superseded | PURE_CORE_CODEMOD/TASK-001..006 | Tier-A read-only pure-core extraction → folded into codemod's full-surface AST rewrite. |
| TASK-012 | Superseded | PURE_CORE_CODEMOD/TASK-001..006 | Tier-B atomic-writer extraction → folded into codemod. |
| TASK-013 | Superseded | PURE_CORE_CODEMOD/TASK-001..006 | Tier-C state-mutating extraction → folded into codemod. |
| TASK-004 + TASK-005 + TASK-006 | Superseded | TASK-014 | 36 tier-partitioned MCP tool registrations → single `_index.json`-driven codegen pass (data-table emit + single dispatcher). |
| TASK-007 | Superseded | TASK-015 | SKILL.md prose rewrite → mechanical regex + per-Phase manual pass. |
| TASK-008 | Superseded | TASK-016 | 36-tool conformance test with hand-authored fixtures → parametrized over `_index.json` × codemod fixtures + conditional semantic-invalid coverage. |
| TASK-009 | Superseded | TASK-017 | Drift guard extension → drift guard + codegen-as-truth check. |
| TASK-010 | Superseded | TASK-018 | E2E smoke (unchanged in spirit; description note about parametrizable inner loop). |

**Why the consolidation works:**

- The on-disk `_index.json` registry at `plugins/plan-executor/scripts/schemas/mcp/_index.json` (38 tools, schema paths) makes MCP tool registration table-driven. The original tier partition (10 + 9 + 17) was a risk-management artifact for the now-superseded `_run_*` extraction; with the codemod establishing every `_run_<sub>` uniformly, every registration is identical boilerplate.
- The codemod plan delivers Tier-A/B/C conformance fixtures (47-49 pairs) covering happy-path round-tripping and per-tier error fixtures. TASK-016's parametrized harness reuses these directly; new fixtures are required only for tools with body-level error paths that schema validation can't reach (a small named subset, NOT every Tier-A tool — see TASK-016 AC for the full conditional).
- The MCP server pattern uses a single `@server.list_tools()` and single `@server.call_tool()` dispatcher driven by `_index.json`, NOT decorator-per-tool — making the codegen a data-table emit rather than 38 individual decorated functions.

**Codex double-check (2026-04-30):** Two independent codex-rescue passes (codegen feasibility + design review) signed off on the consolidation with three structural fixes folded into the v3 ACs: (a) schema-validation failure surfaces as `isError=True` `CallToolResult`, NOT a JSON-RPC error frame; (b) the conditional reinstatement of "errors[]-producing input per tool" for tools with body-level error paths; (c) explicit lock-semantics + subprocess-timeout preservation ACs.

The original task bodies for TASK-004 through TASK-013 are retained below for audit, mirroring the TASK-002 retained-for-audit pattern. The `00_INDEX.json` chunks[] array contains only TASK-001 / 003 / 014 / 015 / 016 / 017 / 018.

---

### TASK-001: MCP server scaffolding

- **Status:** Done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (create)
  - plugins/plan-executor/scripts/schemas/mcp/.keep (create)
  - plugins/plan-executor/.mcp.json (create) — server registration manifest, mirrors the bash `claude_mcp_servers` shape
  - tests/scripts/test_plan_ops_mcp_scaffolding.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_scaffolding.py`
- **Acceptance criteria:**
  - `plan_ops_mcp_server.py` instantiates a `mcp.Server` over stdio with no registered tools yet (registration arrives in TASK-004/005/006); `--list-tools` over the stdio transport returns an empty array on the freshly scaffolded server, and the process exits 0 on EOF.
  - The server resolves `$PYTHON` via `_resolve_python()` from `plan_ops` and exposes the resolved path under a server capability `python_path` (parity with `preflight --json`'s field).
  - Server crashes (uncaught exceptions in handlers) emit a JSON error frame on stdout and exit non-zero; never partial-write a tool response.
  - The plugin manifest registers the server under name `plan-ops` with command `["${CLAUDE_PLUGIN_PYTHON}", "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"]`.
  - Test exercises: server starts, replies to `tools/list` with `[]`, replies to `tools/call` with `MethodNotFound` for any tool name, exits cleanly on stdin EOF.
  - Pure-function helper `_subcommand_to_mcp_tool_name(name: str) -> str` (e.g. `review-route` → `review_route`, `claude-envelope-extract` → `claude_envelope_extract`) lives in this file and is unit-tested.

**Description:** Introduces the transport layer and naming convention. No tools are exposed yet; this task is the chassis later tasks bolt onto. Keeping the scaffolding alone in TASK-001 lets the next-batch work proceed without waiting on `plan_ops.py` refactor in TASK-002.

**Reversion guidance:** Delete `plan_ops_mcp_server.py`, the `.mcp.json` manifest, the `schemas/mcp/` directory, and the test file. No other surface affected.

---

### TASK-002: `plan_ops.py` pure-function entrypoint audit

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** This block is preserved as a historical record of the original task and the rationale for the split. The successor work is in TASK-011 / TASK-012 / TASK-013, which is what `/implement-plan` actually dispatches. The plan-executor's `_build_tasks` does not currently filter `chunks[].status == "Superseded"`, so leaving a Superseded chunk in the index would put TASK-002 in batch 1 of the schedule. The audit trail therefore lives in this markdown body, not in the structured index.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-011 (Tier A — read-only / no-mutation, 23 fns), TASK-012 (Tier B — single-file atomic writers, 5 fns), TASK-013 (Tier C — state-mutating: locks, JSONL, git, subprocess, 10 fns)
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_plan_ops_pure_entrypoints.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints.py`
- **Acceptance criteria:**
  - Every `cmd_<subcommand>(args)` in `plan_ops.py` has a thin shape `cmd_<sub>(args) = _emit(_run_<sub>(payload_dict))` where `_run_<sub>: dict -> dict` is callable without an `argparse.Namespace`. Subcommands that already meet this shape (post-PHASE_D: `review-route`, `batch-next` state helpers, sanitizer) are untouched.
  - For the audit-test: introspect every entry in the argparse subparser map, locate the matching `cmd_*` symbol, and assert it dispatches through a `_run_*` (or already-pure-function) that accepts a single dict argument and returns a single dict.
  - No subcommand reads `sys.stdin` directly inside the pure core; `--stdin` payloads are pre-read in the argparse shim and passed in via the dict.
  - No subcommand calls `_emit`, `print`, or `sys.exit` from inside the pure core. (Argparse-validation errors that today exit early are reshaped into `{"errors": [...]}` returns the shim translates to non-zero exit.)
  - The 36 known subcommands enumerated in §3.2 are all covered.
  - Run-log appends, file writes (atomic), git operations are PRESERVED in the pure cores — refactor is purely about the I/O perimeter, not the side-effect semantics.

**Description:** Removes the remaining `_emit` / stdin / sys.exit entanglement so the MCP server can call the same cores the argparse shim does, with no behavior drift. Without this, TASK-004/005/006 would have to either duplicate logic or reach into private state. Most subcommands need only a 5–15 line shape change; a handful (`commit-task`, `fail-task`, `reconcile-batch`) carry larger I/O perimeters and are the riskiest sites.

**Run note (2026-04-30, run 20260429T232737):** Implementer halted with `outcome: plan-incorrect`, no edits applied. Empirical scope contradicted the Description's "5–15 line shape change" estimate: `plan_ops.py` is 13,796 lines with 38 `cmd_*` functions spanning ~4,000 LoC and ~200 `_die`/`_emit` callsites interleaved through conditional branches. AC required every `cmd_<sub>` to be the literal shim `cmd = _emit(_run_<sub>(payload))` with no `_emit`/`print`/`sys.exit` (and by extension no `_die`, since `_die = _emit(..., exit_code=1)`) and no `sys.stdin.read()` inside the pure core. Behavior preservation was non-trivial: atomic writes, run-log JSONL appends, file-lock acquire/release, git operations, subprocess re-execution (`auto-validate-divergence`) all live inside `cmd_*` bodies and must move into `_run_*` without semantic drift; TASK-008's byte-equal conformance gate would surface any drift later as a hard-to-bisect failure across 38 simultaneous refactors.

**Resolution (2026-04-29 plan refactor):** Status moved to **Superseded** with explicit `superseded_by` chain to TASK-011 → TASK-012 → TASK-013. The 38 `cmd_*` were classified by mutation/concurrency profile (verified line-by-line against the source, cross-validated by Gemini per-task review): Tier A (23 fns, no file mutation, no lock, no subprocess), Tier B (5 fns, single-file atomic writers), Tier C (10 fns, multi-file mutation / file-locks / JSONL hash chain / git / subprocess). The original Tier classification missed five mutations (`cmd_block_dependents`, `cmd_update_plan_header`, `cmd_finalize_execution_log`, `cmd_decompose_plan`, `cmd_reconcile_batch`); the new task split incorporates the corrected mapping. The successor tasks serialize (TASK-011 → TASK-012 → TASK-013) because `plan_ops.py` is a hot file and the wrapper's parallel-batch baseline races on shared working-tree deltas (a separate plan covers that wrapper bug).

**Reversion guidance:** Per-subcommand revert: restore `cmd_*` to its prior shape and drop the `_run_*` indirection. The argparse map and CLI surface are unchanged regardless of revert depth.

---

### TASK-003: MCP input/output schema sidecars

- **Status:** Done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/schemas/mcp/<tool>.input.json × 36 (create — minus the ~6 that already have a sidecar reused)
  - plugins/plan-executor/scripts/schemas/mcp/<tool>.output.json × 36 (create)
  - plugins/plan-executor/scripts/schemas/mcp/_index.json (create) — registry mapping tool name → (input_schema_path, output_schema_path)
  - tests/scripts/test_plan_ops_mcp_schemas.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_schemas.py`
- **Acceptance criteria:**
  - Each schema is JSON Schema 2020-12, valid against a meta-schema check.
  - Input schemas mirror argparse: required flags → required properties; `choices` → `enum`; JSON-string flags (`--reviewer-minor-findings`, `--rows-json`, `--retries-used`, etc.) → typed arrays/objects rather than strings.
  - `--stdin` payloads are modeled as a top-level `payload` property; no `--stdin` boolean appears anywhere.
  - Output schemas describe the `--json` envelope today including `errors[]`, `warnings[]`, `outcome`, and any subcommand-specific fields.
  - Existing sidecars (`review_route_input_schema.json`, `review_route_output_schema.json`, `codex_implement_schema.json`, etc.) are referenced via `$ref` rather than duplicated.
  - `_index.json` enumerates all 36 tools in a stable order; the drift guard (TASK-009) consumes it.
  - Test asserts: every argparse subcommand has an entry in `_index.json`; every entry's referenced schema files exist and parse; every output schema includes `errors` and `warnings` array fields.

**Description:** The contract layer. JSON-schema-backed I/O is the entire reason MCP is a token win — the orchestrator never has to hand-construct a `--reviewer-minor-findings` string or reverse-engineer the `parse-d5-adjudication` envelope.

**Reversion guidance:** Delete the entire `schemas/mcp/` subdirectory and the test file. No code path in `plan_ops.py` references these sidecars.

---

### TASK-004: Register Tier-1 tools (high-frequency orchestrator surface)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-014 (single `_index.json`-driven codegen pass). See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-014 (MCP tool registration via codegen)
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (modify)
  - tests/scripts/test_plan_ops_mcp_tier1.py (create)
- **Dependencies:** TASK-001, TASK-003, TASK-011, TASK-012, TASK-013
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_tier1.py`
- **Acceptance criteria:**
  - The following 10 tools are registered with input/output schemas from TASK-003 and dispatch to the matching `_run_*` pure core: `plan_ops__review_route`, `plan_ops__batch_next`, `plan_ops__commit_task`, `plan_ops__fail_task`, `plan_ops__parse_schedule`, `plan_ops__log_event`, `plan_ops__claude_envelope_extract`, `plan_ops__parse_implementer_report`, `plan_ops__parse_plan_review_report`, `plan_ops__parse_d5_adjudication`.
  - `tools/list` over the stdio transport returns these 10 tools with full metadata.
  - For each tool, the test invokes it via the MCP client harness against a fixture input and asserts the response's `result` content matches the bash CLI's `--json` envelope on the same input (byte-equal after JSON-canonicalize).
  - Tools that today accept JSON-string flags accept structured objects/arrays; the server marshals to the legacy string form on the way into `_run_*` only where the pure core still expects it (transitional; flagged TODO for follow-up cleanup).
  - Run-log appends emitted by `commit-task` / `fail-task` / `log-event` arrive identically when the call originates from MCP vs bash (same fields, same order, same hashes).
  - On `_run_*` returning `{"errors": […]}`, the MCP tool response is an MCP tool error with `errors` carried verbatim in the structured content; never a plain-text error.

**Description:** The token-savings payload. After this, the orchestrator can in principle stop calling Bash for routing, batch selection, commit/fail, and the four parse-* validators that dominate per-task call volume. TASK-007's SKILL.md edit is only safe after this ships.

**Reversion guidance:** Delete the registrations and the test file; the server falls back to the empty tool surface from TASK-001. SKILL.md remains on the bash path until TASK-007 lands.

---

### TASK-005: Register Tier-2 tools (medium-frequency)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-014. See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-014 (MCP tool registration via codegen)
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (modify)
  - tests/scripts/test_plan_ops_mcp_tier2.py (create)
- **Dependencies:** TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_tier2.py`
- **Acceptance criteria:**
  - The following 9 tools are registered: `plan_ops__gates`, `plan_ops__audit`, `plan_ops__reconcile_batch`, `plan_ops__build_claude_dispatch_input`, `plan_ops__order_triage_findings`, `plan_ops__parse_plan_review_triage_report`, `plan_ops__filter_schedule`, `plan_ops__lint_plans`, `plan_ops__finalize_execution_log`.
  - `gates --certify --mode {dry-run,execute}` shape carries through MCP unchanged; mutex argparse groups (`--list` / `--check` / `--certify`) are encoded as a `oneOf` in the input schema.
  - `audit --check <csv>` and `audit --strict` flags survive the schema translation; `audit --report-file` (a write side-effect) is supported and writes occur server-side under the same path semantics as the CLI.
  - `reconcile_batch` accepts envelope arrays as a structured input rather than a stdin JSON blob; preserves the partition semantics when `--schedule-file` is supplied.
  - Per-tool conformance test mirrors TASK-004's pattern (byte-equal MCP envelope vs CLI `--json`).

**Description:** Mostly mechanical after Tier 1. The interesting surface is the `gates` and `reconcile_batch` schemas — both have non-trivial argparse mutex groups and stdin payloads.

**Reversion guidance:** Delete these registrations and the test; Tier 1 remains live.

---

### TASK-006: Register Tier-3 tools (low-frequency / lifecycle)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-014. See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-014 (MCP tool registration via codegen)
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (modify)
  - tests/scripts/test_plan_ops_mcp_tier3.py (create)
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_tier3.py`
- **Acceptance criteria:**
  - The remaining 17 tools are registered: `plan_ops__preflight`, `plan_ops__decompose_plan`, `plan_ops__build_tasks`, `plan_ops__compute_schedule`, `plan_ops__write_schedule`, `plan_ops__update_plan_header`, `plan_ops__normalize_task_id`, `plan_ops__acquire_lock`, `plan_ops__release_lock`, `plan_ops__path_info`, `plan_ops__list_global_lock_paths`, `plan_ops__resolve_read_targets`, `plan_ops__index_closure`, `plan_ops__check_plan_deps`, `plan_ops__block_dependents`, `plan_ops__auto_validate_divergence`, `plan_ops__run_summary`.
  - `tools/list` returns the full 36-tool set after this task; matches the argparse subcommand list one-to-one.
  - `acquire_lock` / `release_lock` retain their on-disk file-lock semantics; the MCP path does not inadvertently treat them as in-memory operations.
  - `auto_validate_divergence` retains its subprocess re-execution of the task's `Test command:` (server-side); timeout enforcement carries through.
  - Per-tool conformance test mirrors prior tiers.

**Description:** Long tail. None individually drives meaningful token savings, but completing the surface unlocks the conformance test (TASK-008) and the e2e smoke (TASK-010), and removes the "is this subcommand on MCP yet?" lookup the orchestrator would otherwise need during transition.

**Reversion guidance:** Delete these registrations and the test. Tier 1+2 remain live.

---

### TASK-007: SKILL.md migration to MCP tool calls

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-015 (mechanical regex + per-Phase manual pass). See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-015 (SKILL.md migration via mechanical script + per-Phase manual pass)
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (modify)
  - plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md (modify or delete — see acceptance)
- **Dependencies:** TASK-004, TASK-006
- **Test command:** deferred (TASK-009 drift guard + TASK-010 e2e)
- **Acceptance criteria:**
  - Every orchestrator-facing `Bash: $PYTHON … plan_ops.py …` invocation in SKILL.md is rewritten as a `Tool: plan_ops__<name>` call with structured input.
  - The "plan_ops.py CLI reference" section (currently ~120 lines, one line per subcommand) is replaced by a single paragraph: "All plan operations are MCP tools named `plan_ops__<subcommand-with-underscores>`. Tool input/output schemas are the source of truth; consult `tools/list` after a context compaction." No per-subcommand prose remains.
  - The "Cache hygiene rules" subsection's rule about re-running `--help` after compaction is removed; the schema-in-tool-context replaces it.
  - `plan_ops_cheatsheet.md` is either deleted (preferred) or reduced to a one-line pointer at the MCP tool list.
  - HEREDOC patterns, `--json` placement reminders, and stdin-pipe worked examples are removed.
  - Subprocess-internal `plan_ops.py` invocations (those running inside `plan_codex_dispatch.py`, dispatch templates, the wrapper) remain documented in Bash form; the SKILL clearly demarcates "orchestrator path: MCP" vs "wrapper-internal: bash".
  - Net SKILL.md character reduction ≥25% (`wc -c` before/after).
  - Hard rules in the "Rules" / "Universal invariants" sections are preserved verbatim or strengthened.

**Description:** The user-visible payoff. After this lands, the orchestrator stops constructing bash command lines for plan operations; SKILL prompt cost drops by roughly the size of the deleted CLI reference plus per-call construction prose.

**Reversion guidance:** Restore SKILL.md and `plan_ops_cheatsheet.md` from the prior commit on the branch. No code rollback needed; the MCP tools remain registered and can be re-adopted in a future SKILL edit.

---

### TASK-008: MCP↔CLI conformance test (full surface)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-016 (parametrized over `_index.json` × codemod fixtures + conditional semantic-invalid coverage). See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-016 (parametrized MCP↔CLI conformance test)
- **Priority:** high
- **Files:**
  - tests/scripts/test_plan_ops_mcp_conformance.py (create)
  - tests/scripts/fixtures/mcp_conformance/<tool>.json × 36 (create — input fixture per tool)
- **Dependencies:** TASK-006
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_conformance.py`
- **Acceptance criteria:**
  - For each of the 36 tools, the test runs the bash CLI (`$PYTHON plan_ops.py <subcommand> … --json`) and the MCP tool against the same fixture input, and asserts the JSON envelopes are byte-equal after canonicalization (sorted keys, normalized whitespace, deterministic timestamps stubbed).
  - Side-effect-bearing tools (`commit-task`, `fail-task`, `log-event`, `block-dependents`, `update-plan-header`, `acquire-lock`, `release-lock`, `write-schedule`, `finalize-execution-log`, `auto-validate-divergence`) run inside an isolated tempdir per fixture; the test asserts identical filesystem deltas + run-log JSONL between the two paths.
  - Tools that resolve `$PYTHON` resolve to the same path under both surfaces.
  - Test runtime under 60s (no real Codex / Claude dispatch; subprocess spawn allowed for the bash side, in-process for the MCP side).
  - The fixture set includes: a clean happy-path input per tool, an `errors[]`-producing input per tool, and a non-trivial input for the four highest-volume tools (`review_route`, `batch_next`, `commit_task`, `parse_implementer_report`).

**Description:** The contract lock. Without this, drift between the two surfaces creeps in silently and the bash CLI quietly stops being a valid fallback. This is the test that lets us deprecate the CLI in a future plan with confidence.

**Reversion guidance:** Delete the test file and the fixture directory.

---

### TASK-009: Drift guard — argparse ↔ MCP registration ↔ SKILL parity

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-017 (drift guard + codegen-as-truth check). See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-017 (drift guard + codegen-as-truth)
- **Priority:** medium
- **Files:**
  - tests/scripts/test_skill_cli_reference_drift.py (modify — extend existing PHASE_D drift guard)
- **Dependencies:** TASK-006, TASK-007
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py`
- **Acceptance criteria:**
  - Existing PHASE_D-era assertions (argparse subcommand vs SKILL.md table parity) are kept or deprecated cleanly if the SKILL table no longer exists.
  - New assertion: every argparse subcommand has a matching MCP tool registration in `plan_ops_mcp_server.py` (introspect the server's tool registry).
  - New assertion: every MCP tool registered has a matching argparse subcommand (no orphans on the MCP side).
  - New assertion: every `plan_ops__<name>` reference in SKILL.md corresponds to a registered MCP tool name.
  - New assertion: the `_index.json` from TASK-003 enumerates exactly the registered tools.
  - Failure messages name the offending subcommand/tool/SKILL line for fast diagnosis.

**Description:** Keeps three documentation surfaces (argparse, MCP registry, SKILL prose) in sync mechanically. Every future `plan_ops.py` subcommand addition mechanically requires an MCP registration and a schema sidecar.

**Reversion guidance:** Restore the prior version of the drift guard test from the branch; the new assertions disappear.

---

### TASK-010: End-to-end smoke — orchestrator runs through MCP only

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by TASK-018 (unchanged in spirit; description note about parametrizable inner loop). See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** TASK-018 (e2e smoke)
- **Priority:** high
- **Files:**
  - tests/scripts/test_implement_plan_mcp_e2e.py (create)
  - tests/scripts/fixtures/mcp_e2e_plan.md (create)
- **Dependencies:** TASK-007, TASK-008
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_mcp_e2e.py`
- **Acceptance criteria:**
  - Drives a one-task fixture plan through Phase 0 → Phase E with all plan operations going through MCP tools, no `Bash $PYTHON … plan_ops.py …` invocations on the orchestrator path. Subagent dispatches (Codex/Claude) are stubbed, identical to TASK-008 of PHASE_D.
  - Asserts the `_run_log.jsonl` byte-baseline matches a parallel run that uses the bash CLI throughout, modulo the new server-startup events `mcp_server_start{pid,python_path}` and per-tool `mcp_tool_called{tool}` events emitted by the MCP server.
  - Covers: clean-commit path, `review_route` returning every action value, `commit-safe` gate post-commit, `reconcile_batch` partition under a stub envelope with `out_of_scope_observed=true`, `fail_task` with the correct authorization-source.
  - Run completes in under 30 s.
  - On a deliberate MCP server crash injected mid-run, the orchestrator path receives an error frame and pauses cleanly (no half-committed state, run-log integrity preserved).

**Description:** The migration's smoke test. Proves the orchestrator no longer needs the bash CLI for a routine run, and proves the MCP server's failure modes are non-corrupting.

**Reversion guidance:** Delete the test file and fixture. Bash-CLI e2e (PHASE_D TASK-008) remains the canonical regression smoke until this stabilizes.

---

### TASK-011: `plan_ops.py` pure-core extraction — Tier A (read-only / no-mutation)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by `PURE_CORE_CODEMOD/TASK-001..006` (deterministic AST-rewrite codemod covering all 38 `cmd_*`). See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md` (TASK-001..006)
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_plan_ops_pure_entrypoints_tier_a.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_a.py`
- **Acceptance criteria:**
  - Each of these 23 `cmd_<sub>` becomes the literal shim `cmd_<sub>(args) = _emit(args, _run_<sub>(_args_to_payload_<sub>(args)))` (or `_die(args, result)` if `result["errors"]` is non-empty): `cmd_batch_next`, `cmd_build_codex_dispatch_input`, `cmd_build_gemini_dispatch_input`, `cmd_build_tasks`, `cmd_check_plan_deps`, `cmd_claude_envelope_extract`, `cmd_compute_schedule`, `cmd_filter_schedule`, `cmd_gates`, `cmd_index_closure`, `cmd_lint_plans`, `cmd_list_global_lock_paths`, `cmd_normalize_task_id`, `cmd_order_triage_findings`, `cmd_parse_d5_adjudication`, `cmd_parse_implementer_report`, `cmd_parse_plan_review_report`, `cmd_parse_plan_review_triage_report`, `cmd_parse_schedule`, `cmd_path_info`, `cmd_resolve_read_targets`, `cmd_review_route`, `cmd_run_summary`.
  - Each `_run_<sub>(payload: dict) -> dict` accepts a single dict (the union of argparse-derived flags + any stdin-JSON payload) and returns a single envelope dict containing at minimum `errors: list` and `warnings: list`. No `sys.stdin`, `_emit`, `_die`, `print`, or `sys.exit` inside any `_run_*`.
  - Stdin-consuming subcommands read stdin once in the argparse shim via a small `_read_stdin_json()` helper; the parsed JSON is passed in the payload under a subcommand-appropriate key (`envelope`, `report`, `findings`, `payload`, `data`, etc.).
  - The shim helper `_args_to_payload_<sub>(args: argparse.Namespace) -> dict` converts string paths to `pathlib.Path` objects in the payload (uniform contract — pure cores always receive `Path` objects, never `str`).
  - `cmd_gates`'s mutex argparse group (`--list` / `--check` / `--certify`) collapses into a single `payload["mode"]`; `_run_gates` switches on the mode and rejects other-mode flags via `errors[]` entries rather than argparse-style halts. Required-flag combinations for `--certify` (`--plan-file` + `--schedule-file`) are enforced in `_run_gates`.
  - `cmd_filter_schedule`'s conditional `data.get("outcome")` validation (relaxed when stdin-mode, near line ~5710) is preserved in `_run_filter_schedule`; the shim records the input source via `payload["input_source"] in {"stdin","file"}` so the pure core can branch identically. `cmd_parse_schedule` carries the same convention.
  - Conformance test: per-function fixture set (`<sub>__<case>.payload.json`, `<sub>__<case>.expected.json`) drives `_run_<sub>(payload)` and the bash CLI (`python plan_ops.py <sub> --json …`) and asserts byte-equal envelopes after JSON canonicalization (sorted keys, normalized whitespace, deterministic timestamps stubbed).
  - Multi-fixture coverage required: `cmd_filter_schedule` ≥ 4 cases (valid filter, dependency-cycle output triggering `_validate_schedule_dag`, stdin-vs-file `outcome=needs-enrichment` branch, unknown-task-id error); `cmd_gates` ≥ 4 cases (each of `--list` / `--check` / `--certify` modes plus a `--certify` missing-required-flag error); each `cmd_parse_*` ≥ 2 cases (happy path + structured-error path).
  - Implementation order within this task: (1) `cmd_review_route` and `cmd_normalize_task_id` (smallest, simplest — establish the shim pattern and `_args_to_payload_*` helper); (2) the five `cmd_parse_*` (`cmd_parse_schedule`, `cmd_parse_d5_adjudication`, `cmd_parse_implementer_report`, `cmd_parse_plan_review_report`, `cmd_parse_plan_review_triage_report` — validates `_read_stdin_json` helper and the stdin payload pattern); (3) `cmd_gates` (validate mutex-group → `payload["mode"]` collapse pattern); (4) `cmd_filter_schedule` and `cmd_compute_schedule` (largest pure-logic bodies, refactored last when the harness is stable).
  - Argparse map and CLI surface (subcommand names, flag names, flag types, `--json` placement) are untouched.
  - No subcommand in this task writes files, acquires locks, runs MUTATING subprocesses, or appends to JSONL. Any function that does is in TASK-012 (Tier B) or TASK-013 (Tier C); the per-function test fixture asserts the absence of any filesystem mutation as a side effect. Read-only git introspection (`git log`, `git rev-parse`) via the existing `_load_feat_commit_ids` helper that `cmd_lint_plans` calls (and `cmd_audit` reuses in Tier B) is permitted because it cannot mutate state and therefore cannot drift the conformance comparison.

**Description:** The low-risk bulk of the original TASK-002 split — 23 of 38 `cmd_*` are read-only pure transformations or near-pure parsers. `cmd_review_route` and `cmd_batch_next` are already very close to the target shape; the rest just need stdin parsing lifted to the shim and `_emit`/`_die` callsites moved to the perimeter. This task ships the shim infrastructure (`_args_to_payload_*`, `_read_stdin_json`, the `tmp_path`-rooted byte-equal conformance harness) that TASK-012 and TASK-013 reuse. Landing this first delivers ~60% of the cmd_* count at low risk and validates the conformance test pattern before the harder tiers.

**Reversion guidance:** Per-function revert: restore `cmd_<sub>` to its prior shape and drop the matching `_run_<sub>` plus `_args_to_payload_<sub>`. The conformance fixtures are per-function so the test stays green at any partial-refactor point. Argparse map and CLI surface unaffected.

---

### TASK-012: `plan_ops.py` pure-core extraction — Tier B (single-file atomic writers)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by `PURE_CORE_CODEMOD/TASK-001..006`. See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md` (TASK-001..006)
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_plan_ops_pure_entrypoints_tier_b.py (create)
- **Dependencies:** TASK-011
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_b.py`
- **Acceptance criteria:**
  - Each of these 5 `cmd_<sub>` becomes the literal shim `cmd_<sub>(args) = _emit(args, _run_<sub>(_args_to_payload_<sub>(args)))`: `cmd_audit` (86 LoC), `cmd_build_claude_dispatch_input` (292 LoC), `cmd_finalize_execution_log` (43 LoC), `cmd_update_plan_header` (28 LoC), `cmd_write_schedule` (35 LoC).
  - The atomic-write step (`_atomic_write_text` for `cmd_write_schedule` and `cmd_audit`'s `--report-file`; `_write_text` for `cmd_update_plan_header`, `cmd_finalize_execution_log`, and `cmd_build_claude_dispatch_input`) lives inside `_run_<sub>` so a single tool invocation completes the full subcommand semantics. Output dict includes `wrote_path` and `bytes_written` for the write so MCP and CLI callers observe the side effect deterministically.
  - Strict validate-then-write atomicity: when `_run_<sub>` returns a non-empty `errors[]`, no write occurs and the on-disk file is byte-identical to its pre-call state. Implementations must complete all schema/path/value validation before opening the destination file for write.
  - `cmd_audit --report-file`'s path-validation guards (parent-directory existence, target-not-a-directory, no path-traversal) run inside `_run_audit` before any write; the path is `payload["report_file"]` as a `pathlib.Path` (shim converts).
  - `cmd_audit`'s read-only git introspection helper `_load_feat_commit_ids` (which subprocess-calls `git log --all --pretty=%s` and is non-mutating) is permitted to remain; the AC's "no subprocess" wording is intentionally relaxed for read-only `git log` / `git rev-parse` calls because they cannot drift the conformance comparison.
  - `cmd_build_claude_dispatch_input`'s mutex variant flags (`--variant analyst|default|rework|narrow-remediation|role-swap|rework-variant-b`) collapse into a single `payload["variant"]` field; the variant-routing logic continues through the existing pure helpers — refactor is shape-only.
  - Conformance test: per-function fixture set runs `_run_<sub>(payload)` in an isolated `tmp_path`-rooted directory and runs the bash CLI in a sibling tmp dir; asserts (a) byte-equal `--json` envelopes after canonicalization, (b) byte-equal file contents at `wrote_path`, (c) identical filesystem snapshot after the call (no extra writes, no `.tmp.<random>` leftovers from `_atomic_write_text`'s rename dance).
  - The test sets a fixed clock via existing test fixtures so `cmd_audit` report timestamps and `cmd_write_schedule` schedule timestamps are deterministic.
  - Implementation order within this task: (1) `cmd_write_schedule` (smallest, validates the side-effect-bearing pure-core pattern); (2) `cmd_update_plan_header` and `cmd_finalize_execution_log` (small plan-mutators using the shared `mutate_task_status` / append-table pattern); (3) `cmd_audit` (adds `--report-file` conditional write + read-only git subprocess); (4) `cmd_build_claude_dispatch_input` (largest, six variant branches, refactored last when the harness and pattern are mature).
  - Conformance harness from TASK-011 is reused; only the file-content + filesystem-snapshot assertions are added.

**Description:** Five functions, single-file atomic writers, no locking, no JSONL hash chain, no mutating subprocess. The risk over Tier A is preserving file-write semantics (validate-then-write atomicity, `_atomic_write_text` rename-dance cleanup) and the variant-collapse pattern in `cmd_build_claude_dispatch_input`. Sequencing AFTER TASK-011 ensures the conformance harness exists and the `_args_to_payload_*` / `_read_stdin_json` shim helpers are stable; sequencing BEFORE TASK-013 means write-side gotchas surface here under simpler semantics rather than entangled with locks/git/JSONL.

**Reversion guidance:** Per-function revert: restore `cmd_<sub>` to the pre-TASK-012 shape and drop the matching `_run_<sub>`. Tier-A pure cores from TASK-011 remain intact. CLI surface is unaffected.

---

### TASK-013: `plan_ops.py` pure-core extraction — Tier C (state-mutating: locks, JSONL, git, subprocess)

> **Retained for audit only — NOT in `00_INDEX.json` chunks[].** Superseded by `PURE_CORE_CODEMOD/TASK-001..006`. See v3 consolidation note above.

- **Status:** Superseded (markdown-body only; absent from index)
- **Superseded by:** `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md` (TASK-001..006)
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py (create)
- **Dependencies:** TASK-012
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py`
- **Acceptance criteria:**
  - Each of these 10 `cmd_<sub>` becomes the literal shim `cmd_<sub>(args) = _emit(args, _run_<sub>(_args_to_payload_<sub>(args)))`: `cmd_acquire_lock` (52 LoC), `cmd_auto_validate_divergence` (161 LoC), `cmd_block_dependents` (374 LoC, multi-file plan write + per-id JSONL append), `cmd_commit_task` (308 LoC, git ops + V-check subprocess + JSONL), `cmd_decompose_plan` (23 LoC body; mutates via `_decompose_plan` helper that writes a sibling directory of `00_INDEX.json` + per-task TASK files), `cmd_fail_task` (182 LoC, git revert + plan mutation + JSONL), `cmd_log_event` (112 LoC, hash-chained JSONL append), `cmd_preflight` (179 LoC, stdin + subprocess), `cmd_reconcile_batch` (41 LoC body; mutates via `reconcile_batch` helper at line ~3991 which calls `_atomic_write_text(plan_path, mutated)` per envelope), `cmd_release_lock` (19 LoC).
  - Side-effect ORDERING is preserved: `cmd_commit_task` and `cmd_fail_task` run their git operations (status check, add, commit, optional V-check subprocess, `git restore` / `git clean` revert paths) inside `_run_*`; the JSONL run-log append happens AFTER the git commit succeeds and BEFORE the envelope is returned, identically to today.
  - `cmd_log_event`'s JSONL append uses the existing `_append_run_log` helper inside `_run_log_event`; the per-event hash chain is byte-identical between bash and MCP paths because the helper computes the hash from canonicalized JSON of the prior event. ALLOWED_LOG_EVENTS validation runs inside the pure core; rejected events return `errors[]` rather than `_die`.
  - `cmd_acquire_lock` / `cmd_release_lock` retain on-disk file-lock semantics: the lock-file create/delete and the metadata write happen inside `_run_*` via the existing `FileLock` / `_atomic_write_json` helpers. Lock-already-held / stale-lock takeover paths return the same `errors[]` codes the bash CLI emits today; stale-lock detection logic at lines ~8096–8118 moves verbatim into `_run_acquire_lock`.
  - `cmd_preflight` and `cmd_auto_validate_divergence` retain their subprocess re-execution paths (`subprocess.run` / `subprocess.Popen`) inside `_run_*`; timeout, return-code, stdout/stderr capture, and the `--auto-fail-on-divergence` semantics carry through unchanged. Subprocess invocation continues to use `_resolve_python()` and the same env propagation.
  - **Subprocess re-entrancy guard:** `_run_auto_validate_divergence` sets `IN_PLAN_OPS_SUBPROCESS=1` in the child env before invoking the test command; `plan_ops.py main()` reads this var and refuses to recursively dispatch a subcommand that itself spawns a `plan_ops.py` child (returns `errors[{"code":"reentrant-plan-ops"}]`). Prevents infinite recursion if a fixture-supplied test command invokes `plan_ops.py auto-validate-divergence`.
  - `cmd_decompose_plan` and `cmd_reconcile_batch` retain their multi-file write semantics: `_run_decompose_plan` writes the entire output directory (`00_INDEX.json` + N per-task markdowns) one-file-at-a-time via `_atomic_write_text`; `_run_reconcile_batch` writes per-envelope plan-status mutations and any schedule update via the same atomic helper.
  - `cmd_block_dependents` retains its mutate-all-in-memory → single plan write per file → log-each-applied-id ordering (per the docstring at line ~7244 / TASK-004D mutation contract); the run-log JSONL appends still occur post-write, one event per applied id; `missing-plan-file` short-circuit error path is preserved verbatim.
  - Conformance test: per-function fixture runs `_run_<sub>(payload)` in an isolated tmp git repo (`tmp_path / "repo"`) with a stub run-log and lock-state file, and runs the bash CLI in a parallel tmp git repo (`tmp_path / "repo_ref"`); asserts (a) byte-equal `--json` envelopes, (b) byte-equal `_run_log.jsonl`, (c) byte-equal git history (`git log --oneline --all` plus tree-hashes of every commit), (d) byte-equal lock-state file contents, (e) identical exit codes on every error path, (f) byte-equal directory snapshot for `cmd_decompose_plan` outputs.
  - Required fixtures: clean-commit happy path, `cmd_fail_task` paused-run-with-authorization-source, `cmd_log_event` rejected-event, `cmd_acquire_lock` stale-lock-takeover, `cmd_auto_validate_divergence` test-command-fails / test-command-times-out, `cmd_commit_task` `out_of_scope_observed=true` reconciler-partition path, `cmd_decompose_plan` malformed-plan error path, `cmd_block_dependents` empty-cascade short-circuit + missing-plan-file error path.
  - **Crash-recovery test:** at least one fixture per multi-step state mutator (`cmd_commit_task`, `cmd_block_dependents`) verifies that a `SIGTERM` between the git commit and the post-commit run-log append leaves the repo in a state from which the orchestrator can recover (commit present, run-log missing the trailing event); the test asserts the same observable state across bash and `_run_*` paths so the orchestrator's existing reconciliation logic continues to work.
  - **Process-tree assertion:** subprocess fixtures (`cmd_preflight`, `cmd_auto_validate_divergence`) assert no orphan / zombie processes remain after the call, particularly on the timeout path. Use `psutil` (preferred) or `os.waitpid(-1, os.WNOHANG)` to verify.
  - **Hermetic env:** the test harness scrubs `PATH`, `PYTHONPATH`, and `PYTHONHASHSEED` to a known fixed set before invoking either path so subprocess-driven divergence cannot mask drift in the byte-equal comparison.
  - Implementation order within this task: (1) `cmd_release_lock` and `cmd_acquire_lock` (simplest; FileLock is already a contained helper); (2) `cmd_log_event` (validates the hash-chain invariant under the new shim path); (3) `cmd_auto_validate_divergence` (validates the subprocess re-entrancy guard); (4) `cmd_preflight` (similar subprocess pattern, plus stdin); (5) `cmd_decompose_plan` and `cmd_reconcile_batch` (multi-file atomic writers, orthogonal to git/lock semantics); (6) `cmd_block_dependents` (multi-file plan mutation + JSONL); (7) `cmd_fail_task` (git revert + plan mutation); (8) `cmd_commit_task` (largest, most state, refactored last with mature harness).
  - Conformance harness from TASK-011/012 is reused; the per-task tmp-git-repo fixture and the SIGTERM/process-tree extensions are the new additions.

**Description:** The high-risk core of the original TASK-002. These 10 functions hold the load-bearing side-effect semantics of the entire plan-executor: git atomicity, JSONL hash-chain ordering, file-lock invariants, subprocess re-execution and re-entrancy. Drift here is the failure mode TASK-008's byte-equal conformance gate is designed to catch — surfacing drift later as cross-tier regressions instead of inside this task is the worst-case outcome. Sequencing AFTER TASK-012 means the harness, helpers, and shim pattern are battle-tested on 28 lower-risk functions before being applied to these 10. The crash-recovery test, process-tree assertion, and subprocess re-entrancy guard are direct outputs of the per-task adversarial review and are mandatory; without them the conformance test only proves equivalence under the happy path.

**Reversion guidance:** Per-function revert: restore `cmd_<sub>` to the pre-TASK-013 shape and drop the matching `_run_<sub>`. Tier-A and Tier-B pure cores from TASK-011/012 remain intact. CLI surface is unaffected. The byte-equal conformance fixtures are the audit trail for any post-revert investigation; the per-function structure means a single function's revert leaves the rest of the test suite green.

---

### TASK-014: MCP tool registration via `_index.json`-driven codegen

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py (create) — the generator
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (modify) — add the AUTOGENERATED registration block
  - tests/scripts/test_plan_ops_mcp_registrations.py (create)
- **Dependencies:** TASK-001, TASK-003
- **Cross-plan precondition:** PURE_CORE_CODEMOD complete (wired via `00_INDEX.json` `depends_on_plans`)
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_registrations.py`
- **Acceptance criteria:**
  - Generator script reads `plugins/plan-executor/scripts/schemas/mcp/_index.json`. The registry's `tool_names_ordered` array holds bare subcommand names (e.g., `acquire_lock`, `audit`, `auto_validate_divergence`); the codegen derives the prefixed MCP tool key as `plan_ops__<name>` for each entry. The `tools` map in `_index.json` already uses the prefixed-key form and points to `<name>.input.json` / `<name>.output.json` schema sidecars + the original argparse `subcommand` string.
  - For each tool, the generator emits an entry in a single `TOOL_REGISTRY` Python data structure: `{tool_key, input_schema, output_schema, subcommand, run_callable_name, args_to_payload_callable_name, description_help}`. Schema bodies are loaded from disk (the JSON file contents). `description_help` is the matching argparse subparser's `help=` string (the generator imports `plan_ops` and introspects its argparse subparser map).
  - **Single-dispatcher pattern:** the MCP server uses one `@server.list_tools()` handler returning a `list[mcp.types.Tool]` built from the registry, and one `@server.call_tool(name, arguments)` handler that looks up the entry by `name`, marshals `arguments` via `_args_to_payload_<sub>` (path strings → `pathlib.Path`, mutex/variant collapse, stdin-payload injection), invokes `_run_<sub>(payload)`, and returns the result. NOT one decorator per tool; the SDK supports loop-based registration (`mcp.server.lowlevel.Server` accepts a function returning `list[types.Tool]` and refreshes a tool cache).
  - The registration block in `plan_ops_mcp_server.py` is fenced as:
    ```
    # BEGIN AUTOGENERATED — do not edit by hand. Run plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py to regenerate.
    ...
    # END AUTOGENERATED
    ```
    `build_server()` calls into the generated registration helper; everything outside the fence is hand-maintained. Generator output is deterministic: tools emitted in `tool_names_ordered` order; whitespace, import ordering, and intra-block formatting are byte-stable across re-runs.
  - On `_run_<sub>` returning a dict with non-empty `errors[]` (a body-level error from in-schema input), the MCP tool response is an `isError=True` `CallToolResult` with the structured error payload carried verbatim in the content list. **Note:** SDK schema-validation failures (out-of-schema input) ALSO surface as `isError=True` `CallToolResult` with plain text content, NOT as a JSON-RPC error frame; tests in TASK-016/017 must assert the `CallToolResult` shape, not a JSON-RPC envelope.
  - Tools accepting JSON-string flags (`--reviewer-minor-findings`, `--rows-json`, `--retries-used`, etc.) accept structured objects/arrays at the MCP edge — the input schema sidecar from TASK-003 already encodes this; the generator surfaces it without re-translation.
  - `--stdin` payloads are surfaced as a top-level `payload` property on the tool input (consistent with the schema sidecars).
  - Special-case argparse mutex groups (`gates --list/--check/--certify`, `build_claude_dispatch_input --variant ...`) are encoded as `oneOf` / `enum` in the schema sidecars; `_args_to_payload_<sub>` (from PURE_CORE_CODEMOD) collapses them into a single `payload["mode"]` / `payload["variant"]` key. The MCP layer does not re-handle them.
  - **Lock semantics + subprocess timeouts preserved (codex#2 fix):** `acquire_lock` / `release_lock` retain their on-disk `FileLock` semantics through the MCP path (lock-file create/delete and metadata write happen inside `_run_*` via the existing `FileLock` / `_atomic_write_json` helpers — verified by the conformance test in TASK-016). `auto_validate_divergence` retains its subprocess re-execution path including timeout enforcement; the MCP server's call-tool handler does not re-implement timeout policy — `_run_auto_validate_divergence` owns it.
  - **Sanitizer perimeter note (codex#2 confirmed):** `parse_implementer_report` is NOT fed raw Codex stdout via the MCP path. The Codex envelope sanitizer (`plan_codex_dispatch.py:1429-1437`) runs wrapper-side, before the parsed envelope reaches any MCP-tool-callable surface. The MCP `parse_implementer_report` tool consumes already-sanitized input from the orchestrator's parsed-envelope flow.
  - Test asserts: `tools/list` over the in-process stdio transport (driven via `mcp.client.session.ClientSession + stdio_client`, which spawns the server as a subprocess — matching production transport, per the existing pattern at `tests/scripts/test_plan_ops_mcp_scaffolding.py:137-156`) returns exactly 38 tools; each one's input schema validates against JSON Schema 2020-12; each handler dispatches to a real `_run_<sub>` (no orphan registrations); calling each tool with the corresponding Tier-A/B/C happy-path fixture from PURE_CORE_CODEMOD returns a non-error `CallToolResult`.
  - **Codegen-as-truth check:** re-running `mcp_tool_registrations.py` produces output byte-identical to the AUTOGENERATED block in `plan_ops_mcp_server.py`. The test fails CI if the in-repo file is stale relative to `_index.json` or `plan_ops.py`'s argparse map. The same check is enforced from a different angle by TASK-017's drift guard.
  - Run-log appends emitted by `commit_task` / `fail_task` / `log_event` arrive identically when the call originates from MCP vs bash (same fields, same order, same hashes). Verified by re-using PURE_CORE_CODEMOD's Tier-C fixtures.

**Description:** Replaces TASK-004 + TASK-005 + TASK-006. The original tier partition (10 + 9 + 17) was a risk-management artifact for the now-superseded `_run_*` extraction; with PURE_CORE_CODEMOD's deterministic AST rewrite establishing every `_run_<sub>` uniformly, every MCP tool registration is identical boilerplate. One codegen pass replaces three LLM-implemented tasks. The single-dispatcher pattern (one `list_tools` + one `call_tool` handler driven by a `TOOL_REGISTRY` data table) avoids decorator-per-tool boilerplate entirely — the registration plumbing is hand-written once, and the generator's job reduces to emitting a data table. SKILL.md migration (TASK-015) is only safe after this lands, exactly as in the original plan.

**Reversion guidance:** Delete the generator, the test, and the AUTOGENERATED block from `plan_ops_mcp_server.py`. Server falls back to TASK-001's empty-tool-set scaffolding. SKILL.md remains on bash path until TASK-015 lands.

---

### TASK-015: SKILL.md migration to MCP tool calls (mechanical script + per-Phase manual pass)

- **Status:** Pending
- **Priority:** high
- **Files:**
  - tools/skill_md_mcp_migration.py (create) — one-shot mechanical regex pass
  - plugins/plan-executor/skills/implement-plan/SKILL.md (modify) — per-Phase manual surgery
  - plugins/plan-executor/skills/implement-plan/plan_ops_cheatsheet.md (delete or reduce to 1-line pointer)
- **Dependencies:** TASK-014
- **Test command:** deferred to TASK-017 drift guard + TASK-018 e2e smoke
- **Acceptance criteria:**
  - Step 1 — mechanical pass via `tools/skill_md_mcp_migration.py`: every orchestrator-facing single-line `Bash: $PYTHON … plan_ops.py <sub> [--flags] --json` invocation in SKILL.md is rewritten to `Tool: plan_ops__<sub> with input { … }` with the input dict reconstructed from the flag set. Multiline commands and bootstrap `python3 …` calls (which don't match the simple pattern) are flagged in the script's stdout for manual review during Step 2; the script does NOT silently leave them on the bash path.
  - Wrapper-internal invocations (`plan_codex_dispatch.py`, `plan_claude_dispatch.py`, dispatch templates) are explicitly NOT touched — they stay on bash because they run inside subprocesses the orchestrator never sees mid-stream. The mechanical script's regex restricts substitutions to sections that are not inside the wrapper-internal subprocess walkthroughs (a path-and-anchor list at the top of the script names the protected sections).
  - Step 2 — per-Phase manual pass (default approach, NOT a fallback): the prose surgery is staged per-Phase walkthrough, allowing the LLM-implementer to dispatch each Phase independently if scope-bloat surfaces. Phase boundaries follow SKILL.md's existing `## Phase X` / `### Step Y` headers (Phase 0 preflight at lines ~187-279, Phase 1 analysis at ~290+, Phase 1.5 plan-review, Phase 1-triage, Phase A-E loop at ~500+, Phase D dispatch at ~607+). The implementer is authorized to spawn one re-dispatch per remaining Phase if a single dispatch hits scope-bloat indicators (≥2K tokens of un-applied edits, partial AC coverage on prior dispatches).
  - The "## plan_ops.py CLI reference" section (currently SKILL.md:76-117, ~120 lines) is replaced by a single paragraph: "All plan operations are reachable as MCP tools `plan_ops__<subcommand-with-underscores>`. Tool input/output schemas are the source of truth; consult `tools/list` after a context compaction." No per-subcommand prose remains.
  - The "Cache hygiene rules" subsection's rule about re-running `--help` after compaction is removed; schema-in-tool-context replaces it.
  - `plan_ops_cheatsheet.md` is either deleted (preferred) or reduced to a one-line pointer at the MCP tool list.
  - HEREDOC patterns, `--json` placement reminders, and stdin-pipe worked examples are removed from orchestrator-facing prose.
  - SKILL clearly demarcates "orchestrator path: MCP tools" vs "wrapper-internal: bash" (one explicit subsection or a single bold callout).
  - Net SKILL.md character reduction ≥25% (`wc -c` before vs after).
  - Hard rules in the "Rules" / "Universal invariants" / "Universal post-D-state" sections are preserved verbatim or strengthened (never weakened). The mechanical script's regex MUST NOT touch these sections — the script restricts substitutions to the parts of the doc that contain `$PYTHON` shell calls.
  - The mechanical script itself is committed alongside the result so the diff is fully auditable; it is a one-shot tool, not a long-lived codegen step.

**Description:** Replaces TASK-007. The originally-flagged scope-bloat risk (Gemini called this the highest-risk task; codex#1 confirmed 60/40 mech/prose, with the bulk of the prose work inside Phase 0→E walkthroughs at SKILL.md:187-279, :363, :577-700) is mitigated by two layers: (a) the mechanical-script split — ~17 fenced bash invocations + 2 inline pipelines are regex-rewritable, eliminating the rote 60% of the work; (b) the per-Phase manual pass as default — each Phase walkthrough is independently rewritable, so partial implementation is verifiable and a single dispatch's scope-bloat doesn't block the whole task. Worst-case is ~40% of the doc rewritten across 6-7 Phase dispatches; best-case is one dispatch finishing the manual pass after the script lands.

**Reversion guidance:** Restore SKILL.md and `plan_ops_cheatsheet.md` from the prior commit on the branch. Mechanical script can be deleted. No code rollback needed; MCP tools remain registered and re-adoptable in a future SKILL edit.

---

### TASK-016: MCP↔CLI conformance test — parametrized over `_index.json` × codemod fixtures + conditional semantic-invalid coverage

- **Status:** Pending
- **Priority:** high
- **Files:**
  - tests/scripts/test_plan_ops_mcp_conformance.py (create)
  - tests/scripts/fixtures/mcp_conformance/semantic_invalid/<sub>.json (create — only for tools with body-level error paths; small named subset, NOT 36)
- **Dependencies:** TASK-014
- **Cross-plan fixture source:** PURE_CORE_CODEMOD's Tier-A/B/C fixtures
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_mcp_conformance.py`
- **Acceptance criteria:**
  - Parametrized over `_index.json["tool_names_ordered"]`. For each tool: pick the corresponding Tier-A/B/C fixture set (path convention: `tests/scripts/fixtures/plan_ops_pure_core/tier_<a|b|c>/<sub>__<case>.{payload,expected}.json` per PURE_CORE_CODEMOD's TASK-004/005/006); for each fixture, run the bash CLI (`$PYTHON plan_ops.py <sub> ... --json`, with stdin-payload tools fed via stdin) AND the MCP path (subprocess stdio transport, driven via `mcp.client.session.ClientSession + stdio_client`, matching the existing pattern at `tests/scripts/test_plan_ops_mcp_scaffolding.py:137-156`) on the same payload; assert byte-equal `--json` envelopes after canonicalization (sorted keys, normalized whitespace, deterministic timestamps stubbed).
  - Side-effect-bearing tools — `commit_task`, `fail_task`, `log_event`, `block_dependents`, `update_plan_header`, `acquire_lock`, `release_lock`, `write_schedule`, `finalize_execution_log`, `auto_validate_divergence`, `decompose_plan`, `reconcile_batch` — run inside an isolated tmp-git-repo per fixture; assert (a) byte-equal `--json` envelopes, (b) byte-equal filesystem deltas (file content + directory snapshot), (c) byte-equal `_run_log.jsonl` between bash and MCP paths, (d) byte-equal lock-state file contents for `acquire_lock` / `release_lock`, (e) `auto_validate_divergence` subprocess timeout enforcement matches between paths (timeout-path test asserts non-zero exit + bounded wall-clock under both surfaces).
  - Tools that resolve `$PYTHON` resolve to the same path under both surfaces.
  - Test runtime under 90s.
  - **errors[]-producing-input coverage (codex#2 fix — STRUCTURAL):** the parent's original AC ("`errors[]`-producing input per tool") is reinstated as a CONDITIONAL requirement, NOT collapsed into schema-rejection alone. Schema-rejection at the MCP edge is sufficient ONLY for tools whose `_run_*` has no body-level error path on in-schema input. For tools where `_run_*` emits structured `errors[]` or `{"error":...}` from in-schema input — confirmed by code-grounded audit on `cmd_normalize_task_id` (`plan_ops.py:7983-7987`, emits `{"error":...}` for unnormalizable strings that pass schema), `cmd_review_route` (`plan_ops.py:13333-13383, 13660-13665`, emits structured `errors[]` for missing/wrong fields), and similar — a semantic-invalid fixture is REQUIRED in this task's parametrized suite. The fixture set lives under `tests/scripts/fixtures/mcp_conformance/semantic_invalid/<sub>.json` and is reviewed for completeness during implementation.
  - The named tools requiring semantic-invalid fixtures (initial set, audited from `plan_ops.py`): `cmd_normalize_task_id`, `cmd_review_route`. Implementation MUST audit each Tier-A function during the implementer pass and add fixtures for any function emitting body-level errors[] from in-schema input. The audit is a small grep (`grep -n '\\"errors\\":\\s*\\[' plan_ops.py | head -200`) cross-referenced against the schema's input constraint set. Implementation must not collapse this audit step.
  - For schema-rejection cases (out-of-schema input on tools with no body-level error path): the test asserts the bash CLI's argparse layer returns non-zero exit with argparse error text on stderr, AND the MCP path returns an `isError=True` `CallToolResult` with plain-text content describing the schema mismatch (per the SDK's behavior at `mcp/server/lowlevel/server.py:467-473, 527-532` — NOT a JSON-RPC error frame). The two failure shapes are not byte-equal (bash returns argparse stderr; MCP returns CallToolResult with plain text); the test asserts both surfaces reject and that the rejection messages name the offending field.
  - On a deliberate MCP server crash injected mid-call (e.g., `_run_*` raises an unexpected exception), the test asserts: bash side gets non-zero exit with the exception text on stderr; MCP path receives an MCP-protocol-level error (the SDK's crash handling, distinct from `isError=True` CallToolResult); never half-executes a tool.
  - The fixture-discovery loop fails with a clear message if a `_index.json` tool has zero matching fixtures across both Tier-A/B/C (codemod) and semantic_invalid (this task) — surfaces missing coverage early.

**Description:** Replaces TASK-008. The contract lock — without this, drift between MCP and bash creeps in silently and the bash CLI quietly stops being a valid fallback. The original AC's "create 36 input fixtures" is split: codemod fixtures cover the structural happy + per-tier error surface; this task adds semantic-invalid fixtures only where `_run_*` has a body-level error path that schema validation cannot reach. The conditional structure was a structural rework imposed by codex#2's review — the prior schema-rejection-as-sufficient argument was unsound for tools like `cmd_normalize_task_id` that accept any string in schema but emit `{"error":...}` for unnormalizable inputs.

**Reversion guidance:** Delete this test file and the semantic_invalid fixture directory. Codemod fixtures remain in place for the codemod plan's own smoke gate.

---

### TASK-017: Drift guard — argparse ↔ MCP registry ↔ SKILL parity ↔ codegen-up-to-date

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - tests/scripts/test_skill_cli_reference_drift.py (modify — extend the existing PHASE_D drift guard)
- **Dependencies:** TASK-014, TASK-015
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py`
- **Acceptance criteria:**
  - Existing PHASE_D-era assertion (argparse subcommand list vs SKILL.md CLI-reference table parity) is updated: when TASK-015 has deleted the CLI-reference section, the assertion switches from "table parity" to "every argparse subcommand has a matching MCP tool registered". A clean cutover with no transitional gap.
  - New assertion: every argparse subcommand in `plan_ops.py` has a matching MCP tool registration in `plan_ops_mcp_server.py` (introspect via `mcp.types.Tool` extraction from a freshly-built server, OR via static AST parse of the AUTOGENERATED block; either is acceptable as long as the assertion is sound).
  - New assertion: every MCP tool registered has a matching argparse subcommand (no orphans on the MCP side).
  - New assertion: every `plan_ops__<name>` reference in SKILL.md corresponds to a registered MCP tool name.
  - New assertion: `_index.json` enumerates exactly the registered tools (intersection equality on `tool_names_ordered` array vs derived registry keys after stripping the `plan_ops__` prefix).
  - **New assertion (codegen-as-truth):** running `python plugins/plan-executor/scripts/_codegen/mcp_tool_registrations.py` (TASK-014's generator) in a fresh process produces output that is byte-identical to the AUTOGENERATED block currently in `plan_ops_mcp_server.py`. The test fails CI if the in-repo file is stale relative to `_index.json` or `plan_ops.py`'s argparse map.
  - Failure messages name the offending subcommand/tool/SKILL line for fast diagnosis.

**Description:** Replaces TASK-009. Keeps four documentation surfaces (argparse, MCP registry, SKILL prose, codegen output) in sync mechanically. Every future `plan_ops.py` subcommand addition mechanically requires running the generator and updating the corresponding schema sidecar — the test fails until both happen. Modeled on the existing 90-LoC PHASE_D drift guard at `tests/scripts/test_skill_cli_reference_drift.py:25-90`; extended by ~50-80 LoC.

**Reversion guidance:** Restore the prior version of this test file from the branch. The new assertions disappear; the existing PHASE_D-era assertions remain valid as long as TASK-015 has not run.

---

### TASK-018: End-to-end smoke — orchestrator runs through MCP only

- **Status:** Pending
- **Priority:** high
- **Files:**
  - tests/scripts/test_implement_plan_mcp_e2e.py (create)
  - tests/scripts/fixtures/mcp_e2e_plan.md (create)
- **Dependencies:** TASK-015, TASK-016
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_implement_plan_mcp_e2e.py`
- **Acceptance criteria:**
  - Drives a one-task fixture plan through Phase 0 → Phase E with all plan operations going through MCP tools, no `Bash $PYTHON … plan_ops.py …` invocations on the orchestrator path. Subagent dispatches (Codex/Claude) are stubbed, identical to PHASE_D TASK-008's pattern.
  - Asserts the `_run_log.jsonl` byte-baseline matches a parallel run that uses the bash CLI throughout, modulo new server-startup events `mcp_server_start{pid,python_path}` and per-tool `mcp_tool_called{tool}` events emitted by the MCP server.
  - Covers: clean-commit path; `review_route` returning every action value; `commit-safe` gate post-commit; `reconcile_batch` partition under a stub envelope with `out_of_scope_observed=true`; `fail_task` with the correct authorization-source.
  - Run completes in under 30 s.
  - On a deliberate MCP server crash injected mid-run, the orchestrator path receives an error frame and pauses cleanly (no half-committed state, run-log integrity preserved).
  - **Inner-loop split (codex#1 finding D):** the per-tool run-log event sequence comparison is parametrized over `_index.json` (codegen-friendly inner loop). The outer Phase 0→E orchestration wrapper that drives phase transitions, stubs Codex/Claude, and compares `_run_log.jsonl` event sequences is hand-coded narrative. The split keeps the test tractable; the inner loop is data-driven over the registry rather than per-tool hand-coded.

**Description:** Replaces TASK-010, unchanged in spirit. The migration's smoke test — proves the orchestrator no longer needs the bash CLI for a routine run, and proves the MCP server's failure modes are non-corrupting. This is the only remaining narrative-style integration test in the consolidated plan; everything else is codegen or unit-scoped.

**Reversion guidance:** Delete the test file and fixture. Bash-CLI e2e (PHASE_D TASK-008) remains the canonical regression smoke until this stabilizes.

---

## Execution log — 20260429T232737 (paused)

Starting SHA: `fc9d80b246124fbd91719a36c11dfd6739e15eea`  → Ending SHA: `fc9d80b246124fbd91719a36c11dfd6739e15eea`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude | none | n/a | n/a | Implementer reported success; wrote 4 files; scaffolding tests passed in-session. Wrapper flagged scope_violation (false positive: parallel execution leaked sibling deltas + .mcp.json (create) prose suffix prevented declared-list match). Work preserved on disk under pause policy. |
| 002 | claude | none | n/a | n/a | Implementer halted with plan-incorrect outcome (no edits). Genuine plan defect: Description estimates 5-15 line shape change but plan_ops.py is 13.8K LoC with 38 cmd_* fns and ~200 _emit/_die/print callsites; AC requires whole-surface refactor. |
| 003 | claude | none | n/a | n/a | Implementer reported success; wrote 73 files in schemas/mcp/ + 1 test file. Wrapper flagged scope_violation (false positive: declared list had pseudo-paths <tool>.input.json × 36 + sibling parallel writes leaked into observed_delta). Work preserved on disk. |
| 004 | claude | none | n/a | n/a | Not started; depends on TASK-001/002/003. |
| 005 | claude | none | n/a | n/a | Not started; depends on TASK-004. |
| 006 | claude | none | n/a | n/a | Not started; depends on TASK-005. |
| 007 | claude | none | n/a | n/a | Not started; depends on TASK-004+TASK-006 (post plan revision). |
| 008 | claude | none | n/a | n/a | Not started; depends on TASK-006. |
| 009 | claude | none | n/a | n/a | Not started; depends on TASK-006+TASK-007. |
| 010 | claude | none | n/a | n/a | Not started; depends on TASK-007+TASK-008. |

## Execution log — 20260501T005802 (paused)

Starting SHA: `c8b276e9f77d81ef9d174bcbe4d6fe28c7122839`  → Ending SHA: `c8b276e9f77d81ef9d174bcbe4d6fe28c7122839`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 014 | codex | none |  |  | paused: wrapper crashed during timeout-cleanup (plan_codex_dispatch.py:1135 TypeError missing authorization_source). Preservable in-scope diff in working tree. |

## Execution log — 20260501T010959 (paused)

Starting SHA: `468347e4f1a10b38345753cc05ab1094633998c0`  → Ending SHA: `468347e4f1a10b38345753cc05ab1094633998c0`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 014 | codex | none | paused | - | scope_violation_paused (wrapper false-positive on declared Files: with " — description" suffix); 3 declared files written; awaiting user disposition (widen-plan|in-place-fix|keep-and-commit|revert) |
