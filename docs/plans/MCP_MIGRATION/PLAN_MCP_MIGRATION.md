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

## 6. Execution — parallel batches

Ten tasks, six batches. The hot file is `plan_ops_mcp_server.py` (TASKs 004/005/006 all mutate it), so those serialize. `plan_ops.py` is touched only by TASK-002 (refactor); subsequent tasks register against pure functions instead of editing it. Schema sidecars (TASK-003) live in their own directory. SKILL.md (TASK-007) and the drift-guard test (TASK-009) are independent files.

- **Batch 1 (parallel):** TASK-001 (server scaffolding, new file), TASK-002 (`plan_ops.py` pure-function audit), TASK-003 (schema sidecars, new files).
- **Batch 2:** TASK-004 (Tier-1 tools) — needs 001+002+003.
- **Batch 3:** TASK-005 (Tier-2 tools, same file as 004 → must follow 004).
- **Batch 4:** TASK-006 (Tier-3 tools, same file as 005 → must follow 005).
- **Batch 5 (parallel):** TASK-007 (SKILL.md rewrite, depends on 004+006 — full MCP surface must be registered before SKILL.md rewrites every orchestrator-facing `plan_ops.py` invocation as a `Tool: plan_ops__<name>` call), TASK-008 (conformance test, depends on 006 for full-surface coverage). Different files; parallel.
- **Batch 6 (parallel):** TASK-009 (drift guard, depends on 006+007), TASK-010 (end-to-end smoke, depends on 007+008). Different files; parallel.

Single-session parallel execution within each batch is the target.

---

## Tasks

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

- **Status:** Pending
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

**Run note (2026-04-30, run 20260429T232737):** Implementer halted with `outcome: plan-incorrect`, no edits applied. Empirical scope contradicts the Description's "5–15 line shape change" estimate: `plan_ops.py` is 13,796 lines with 38 `cmd_*` functions spanning ~4,000 LoC and ~200 `_die`/`_emit` callsites interleaved through conditional branches. AC requires every `cmd_<sub>` to be the literal shim `cmd = _emit(_run_<sub>(payload))` with no `_emit`/`print`/`sys.exit` (and by extension no `_die`, since `_die = _emit(..., exit_code=1)`) and no `sys.stdin.read()` inside the pure core; even the AC's named already-pure example `cmd_review_route` is not literally `cmd = _emit(_run(payload))` today (it reads stdin, validates, calls `route(payload)`, then `_emit`). So the AC's literal shim shape is not present anywhere, requiring a full refactor of all 38 `cmd_*` sites including the supposedly-exempt ones. Behavior preservation is non-trivial: atomic writes, run-log JSONL appends, file-lock acquire/release, git operations, subprocess re-execution (`auto-validate-divergence`) all live inside `cmd_*` bodies and must move into `_run_*` without semantic drift; TASK-008's byte-equal conformance gate would surface any drift later as a hard-to-bisect failure across 38 simultaneous refactors. **Status remains Pending.** This task will be re-scoped (likely split into per-region or per-risk-tier sub-tasks, or have its AC narrowed) and re-attempted in a separate session.

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

- **Status:** Pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops_mcp_server.py (modify)
  - tests/scripts/test_plan_ops_mcp_tier1.py (create)
- **Dependencies:** TASK-001, TASK-002, TASK-003
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

- **Status:** Pending
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

- **Status:** Pending
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

- **Status:** Pending
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

- **Status:** Pending
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

- **Status:** Pending
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

- **Status:** Pending
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
