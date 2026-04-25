# Plan: Nested Subagent Dispatch (`plan_claude_dispatch.py`) — v3

**Created:** 2026-04-20
**Status:** draft — specification + implementation plan + critical analysis
**Base branch:** main
**Supersedes:** v1 (`PLAN_NESTED_DISPATCH_2026-04-18.md`) and v2 (`PLAN_NESTED_DISPATCH_2026-04-18_v2.md`). The v2 probe supplement is folded in; the qwen probe supplement is deferred to a follow-on local-llm plan and preserved in Appendix B for that plan's starting context.
**Related:** `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`; `plugins/plan-executor/scripts/plan_codex_dispatch.py`; `plugins/plan-executor/scripts/_plan_paths.py`.

---

## Changes from v2 (headline)

| Area | v2 | v3 |
|---|---|---|
| Script name | `plan_nested_dispatch.py` | `plan_claude_dispatch.py` (mirrors existing `plan_codex_dispatch.py`) |
| Backends | claude-cli + codex-cli + local-llm, multi-backend abstraction | claude-cli only; no abstraction layer |
| Prompt assembly | `--system-prompt-file` + ordered segments + `--append-system-prompt` | Native `--agent plan-executor:<name>`; caller payload goes in user-prompt argument |
| Cache TTL | Per-agent `dispatch.cache_ttl` (5m vs 1h) selection + `--settings` injection | Dropped. Default is 1h (probe-confirmed); no operator knob in v1 |
| Agent registry | `agents.index.json` | Dropped. CLI resolves plugin-namespaced names natively |
| Dispatchable agents | Existing 3 Claude agents + 5 new (plan-reviewer, codex-*, local-llm-*) | Existing 3 only: `plan-analyst`, `plan-implementer`, `plan-remediator` |
| Framing | Escape-hatch AND future orchestrator migration | **Escape-hatch only.** SKILL.md and dispatch-templates.md unchanged in v1 |
| Recursion killswitch | Env flag + narrow Bash allowlist (two layers) | Single layer: env flag + `Agent` in `--disallowedTools` |
| Manifest `dispatch:` block | Mandatory on every agent manifest | Not added in v1; existing frontmatter (`tools`, `model`) is sufficient |
| Tool allowlist enforcement | Assumed enforced under `--agent` | **Probe 2 proved it is NOT** — wrapper constructs `--allowedTools` explicitly; delta-bounded cleanup as defense in depth |
| `--permission-mode` | `bypassPermissions` | `acceptEdits` — pending Probe 2b confirmation (§12) |
| Local-LLM | In-scope as backend #3 | Deferred to separate plan; qwen probe findings in Appendix B |

---

## Critical analysis — why v3 differs from v2

This section is the "opinion" deliverable. It records which v2 and probe-supplement assertions are load-bearing, which were wrong, and which are open.

### A. v2 assertions the probes invalidate (dropped in v3)

| v2 section | Assertion | Probe finding | v3 action |
|---|---|---|---|
| §3.1 | "TTL: 5 min default (`ephemeral_5m`)" with cost matrix around 5m vs 1h | Probe 3: subagent system prompt lives in the **1-hour** ephemeral cache by default | Drop. No per-agent TTL machinery. |
| §3.2 | Mandatory 5-segment prompt layout; manifest gains `dispatch.cache_ttl` | `--agent plan-executor:<name>` loads the subagent's frontmatter + body natively (Probe 1) | Drop. Payload goes in user prompt; rest is native. |
| §3.3 | Backend maps `dispatch.cache_ttl` via `--settings` injection or SDK fallback | Unnecessary once 1h is the default | Drop. |
| §6.2 | Manifest gains `dispatch.cache_ttl`, `settings_profile`, `tools_override`, `codex:`, `local_llm:` | Most fields are premature optimization or out-of-scope | Drop the mandatory `dispatch:` block. Read only existing `tools:` and `model:` |
| §6.3 | `agents.index.json` registry | CLI already resolves `<plugin>:<name>` | Drop. |
| §7.1 | `--system-prompt-file <manifest_body_path>` + `--append-system-prompt <output_instructions>` | `--agent` loads the body; output instructions go in user prompt | Drop file-based prompt assembly. |
| §7.2 | codex-cli backend adapter | Out of scope per user direction | Drop. |
| §7.3 | local-llm backend | Out of scope per user direction; separate plan | Drop from v1. Findings in Appendix B. |
| §9.3 | Two-layer recursion killswitch (env flag + narrow Bash allowlist excluding `plan_*_dispatch.py`) | Subagents invoking the wrapper via Bash don't have `Agent` in their tool set; putting `Agent` in `--disallowedTools` on the inner `claude -p` closes the spawn vector. The Bash-allowlist layer is both paranoid (for honest-mistake threat model) and insufficient (against hostile-subagent, which is outside the model). | Simplify to single layer. |
| TASK-001 | Create 5 new agent manifests + `agents.index.json` | Dispatchable set is the existing three agents | Drop the task. |

### B. v2 assertions that stand and are preserved

- §2 **Escape-hatch framing.** `subagent → python → claude -p → Agent → sub-sub` is real and validated.
- §2.1 **Fresh minds by default.** Children inherit no parent state; payload must carry everything. Load-bearing, inverts the v1 "context amnesia risk" framing correctly.
- §8 **Input/output envelope structure.** Structured input (`agent`, `payload`, `output_instructions`, `overrides`, `guardrails`, `trace`) + status-coded output envelope is the right shape. Trimmed for single-backend v1 but skeleton preserved.
- §9.1 **`PLAN_EXEC_*` env-variable protocol.** Clean namespacing; parent→child propagation of RUN_ID, DEPTH, COST_USD, cost cap. Preserved intact.
- §9.2 **Pre-spawn refusal matrix.** Each row is a real guardrail. Preserved (minus the `PLAN_EXEC_ALLOWED_AGENTS` row, which becomes optional).
- §11 **Security model.** Auth / secrets / filesystem / network / recursion / hook-isolation decomposition is sound.

### C. Probe supplement assertions that stand and are preserved

- **`--agent plan-executor:<name>` loads the subagent natively** (Probe 1). Single biggest simplification over v2.
- **Tool allowlist is NOT enforced under `--agent` in `-p` mode** (Probe 2). Material safety gap. Wrapper MUST construct `--allowedTools` explicitly; delta-bounded cleanup is defense in depth.
- **Delta-bounded cleanup reuses `plan_codex_dispatch.py` pattern.** `_plan_paths.py` already factors out protected paths; new wrapper imports it.
- **Cache amortizes within the 1-hour window** (Probe 3). Steady-state cost flat; cold-start is a one-time surcharge.
- **Subcommand timeouts** (300s implement, 180s review/analyst) are sensible defaults.

### D. Probe supplement assertions I question or flag

- **"`acceptEdits` enforces `--allowedTools`"** — **untested.** Probe 2 proved `bypassPermissions` ignores the allowlist; the supplement asserts `acceptEdits` respects it without running the equivalent probe. v3 adds **Probe 2b** as a mandatory pre-build step (§12, TASK-007). If Probe 2b fails, v3 falls back to strict cwd sandbox + mandatory cleanup.
- **Concurrent dispatches on git baseline — "untested" per probe supplement** — elevated from a tuning-pass to a v3 acceptance criterion. The orchestrator already fires parallel dispatches; concurrent escape-hatch dispatches must not cross-contaminate baselines.
- **`docs/plans/_run_reports/` persistence convention** — the probe supplement proposes a new filesystem location for raw subagent markdown reports. v3 defers this: `spans.jsonl` carries observability; raw content is bounded and inlined in the envelope (`result_raw_truncated` ≤ 16 KB). No new filesystem artifacts in v1.
- **"API cost is not a factor in the migration decision"** — true for serial dispatches of a single agent; wide fanout across distinct agents pays creation per prefix. Not dominant at v1 scale but instrumented via `spans.jsonl`.

### E. Qwen probe supplement (deferred, Appendix B)

All eight probes are empirically solid and directly applicable to the future local-llm backend. One policy concern worth elevating for the follow-on plan: Probe 5+6 showed qwen-coder:14b misclassified a real `hmac.compare_digest → ==` timing-attack regression as `severity: low, category: style` across 4 deterministic runs. "Bulk review triage" is correct positioning; the follow-on plan must spell out which cases local-llm is safe for, not just note the caveat.

---

## 1. Goal

Build `plugins/plan-executor/scripts/plan_claude_dispatch.py` — a Bash-callable chokepoint that lets a subagent (running in a tool-restricted context that lacks the `Agent` tool) spawn a fully-featured nested Claude agent via `claude -p --agent plan-executor:<name>`. The wrapper provides: a structured input contract, a schema-validated output envelope, delta-bounded cleanup, guardrails (depth, cost cap, tool allowlist, cwd sandbox, network gating, recursion killswitch), and observability (a per-dispatch span log). It is the one sanctioned chokepoint for subagent escape-hatch dispatch; every hop traverses its guardrails and its contract.

## Context

(Preserved from v2 §2.)

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). The nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth / billing / recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude.

## 2.1 Design principle — fresh minds by default

(Preserved from v2 §2.1.)

Children start with no conversation history, no inherited memory, and no residue from the parent's reasoning trail. This is **load-bearing**, not incidental. A parent that has accumulated 50k+ tokens of state — plan-wide discussion, earlier tool outputs, branches considered and rejected — can hand a child only the 1–3k tokens strictly needed to do its job. The child reasons on a clean slate and produces more accurate output than a sibling working at the bottom of a long transcript would.

The implication is an obligation, not a hazard: callers must pack every fact the child needs into the input payload. Anything assumed but unstated will be invisible to the child. The manifest body + payload pair is the total world the child sees; design payloads accordingly. The wrapper never leaks parent conversation state — that's the feature, not a bug.

Corollary: do not "pass context for safety." Aggressive payload minimization is the point. If the child gets too much, you've recreated the parent's blurry context inside the child and lost the benefit.

## 3. Empirical basis

Consolidated from v2 §3 (two-hop experiment) and the probe supplement (Probes 1–3). Probe 2b is added by v3 and is a build gate, not a retrospective data point.

| Hop / probe | What was tested | Result | Cost / latency |
|---|---|---|---|
| v2 §3 hop 1 | Top-level Claude → `claude -p` → Agent tool → sub-sub | Sentinel `NESTED_AGENT_OK` returned | 6.50 s / $0.2447 |
| v2 §3 hop 2 | Subagent (Bash-only) → python → `claude -p` → Agent → sub-sub | Sentinel `DEEP_NESTED_OK_a3f9c2` returned | 6.34 s / $0.0933 (warm) |
| Probe 1 | `--agent plan-executor:plan-analyst` resolves + loads the subagent | 29,531 cache-creation tokens — full system prompt body loaded | 2.4 s / $0.037 (haiku, cold) |
| Probe 2 | `tools:` frontmatter enforcement under `--agent` in `-p` | **Not enforced.** `Write` succeeded despite absence from the agent's `tools:` list | — |
| Probe 3 | Cache TTL for subagent system prompt | 1-hour ephemeral cache; 3 back-to-back dispatches all hit the warm cache; steady-state cost flat at $0.00532 | 1.7–2.2 s / $0.00532 (haiku, warm) |
| **Probe 2b (v3 adds — build gate)** | `acceptEdits` + explicit `--allowedTools` — does the allowlist get enforced? | TBD before build starts | — |

## 4. Scope

**In scope:**
- `plan_claude_dispatch.py` wrapper
- `claude_dispatch_input.json` / `claude_dispatch_output.json` schemas
- Manifest loader + guardrail engine
- claude-cli invocation adapter
- Delta-bounded cleanup (shared with Codex wrapper where practical)
- `spans.jsonl` observability log
- End-to-end test proving subagent → wrapper → `claude -p` round-trip
- Probe 2b acceptance test
- README documenting escape-hatch usage and env vars

**Out of scope (deferred or rejected):**
- Codex-cli backend / adapter (callers invoke `plan_codex_dispatch.py` directly)
- Local-llm backend (follow-on plan; qwen findings preserved in Appendix B)
- New agent manifests (`plan-reviewer`, `codex-*`, `local-llm-*`)
- `agents.index.json` registry
- Orchestrator migration — SKILL.md and dispatch-templates.md are **unchanged** in v1
- Mandatory `dispatch:` frontmatter block

## 5. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ Caller (subagent with Bash-only tool set, or any Python/Bash       │
│ context). Writes JSON payload → invokes plan_claude_dispatch.py    │
└───────────────────────────────────┬────────────────────────────────┘
                                    │
                  ┌─────────────────▼───────────────────┐
                  │ plan_claude_dispatch.py run         │
                  │  1. parse & validate input          │
                  │  2. resolve agent → manifest        │
                  │  3. enforce guardrails (§9)         │
                  │  4. snapshot git baseline           │
                  │  5. spawn claude -p --agent ...     │
                  │  6. parse & validate inner result   │
                  │  7. delta-bounded cleanup           │
                  │  8. emit envelope (stdout)          │
                  │  9. append span (spans.jsonl)       │
                  └─────────────────┬───────────────────┘
                                    │
                            ┌───────▼────────┐
                            │ claude -p      │
                            │ --agent <name> │
                            └────────────────┘
```

Single backend. `plan_claude_dispatch.py` talks directly to `claude -p`. If a future plan adds local-llm, it gets its own wrapper (e.g., `plan_local_llm_dispatch.py`) — we do not build a generic multi-backend abstraction now.

## 6. Input contract

Input is supplied as JSON via `--input <path>` or stdin (`--input -`).

```json
{
  "schema_version": 1,
  "agent": "plan-implementer",
  "payload": {
    "…": "opaque to wrapper; serialized into user prompt; agent-specific shape"
  },
  "output_instructions": {
    "format": "json",
    "schema_path": "plugins/plan-executor/scripts/schemas/implementer_report.json",
    "schema_inline": null,
    "max_bytes": 65536
  },
  "overrides": {
    "model": null,
    "timeout_sec": null,
    "tools_allowed_extra": null,
    "tools_disallowed_extra": null,
    "cwd": null
  },
  "guardrails": {
    "max_depth": null,
    "cost_cap_usd": null,
    "network": null
  },
  "trace": {
    "run_id": "uuid-of-top-level-run",
    "parent_span_id": "uuid-of-caller-span",
    "depth": 0,
    "call_chain": ["orchestrator"]
  }
}
```

**Rules:**
- `agent` must resolve to one of `{plan-analyst, plan-implementer, plan-remediator}` in v1. Wrapper rejects otherwise with `status: input_invalid, code: agent_not_dispatchable`.
- `payload` is opaque to the wrapper; it is serialized into the `claude -p` user prompt. The agent's own system prompt (loaded by `--agent`) interprets it.
- `output_instructions.schema_*`: at least one of `schema_path` / `schema_inline` must be supplied. The wrapper validates inner output against it.
- `overrides.tools_allowed_extra` intersects with manifest `tools:`; `tools_disallowed_extra` adds to the disallowed set. Neither can expand beyond the manifest's `tools:` set.
- `trace` is filled by the caller if nested. If absent at depth 0, the wrapper mints a new `run_id` and an initial `span_id`.

## 7. Output envelope

Emitted as JSON on stdout. Shape aligns with `plan_codex_dispatch.py` where semantically equivalent and extends it with Claude-specific fields.

```json
{
  "schema_version": 1,
  "status": "ok",
  "status_reason": null,
  "agent": "plan-implementer",
  "model": "claude-opus-4-7",
  "session_id": "uuid",
  "duration_ms": 12345,
  "cost_usd": 0.0933,
  "tokens": {
    "input": 13, "output": 235,
    "cache_read": 24145, "cache_creation": 36268
  },
  "result": { "…": "inner agent's structured result, schema-validated" },
  "result_raw_truncated": "… first 16 KB of raw result text …",
  "stderr_tail": "… last 2 KB of stderr …",
  "permission_denials": [],
  "scope": {
    "declared_files_changed": ["plugins/…"],
    "observed_delta_tracked": ["plugins/…"],
    "observed_delta_untracked": [],
    "scope_violation_detected": false,
    "scope_misreport_detected": false
  },
  "trace": {
    "run_id": "uuid", "span_id": "uuid", "parent_span_id": "uuid",
    "depth": 1, "call_chain": ["orchestrator", "plan-implementer"],
    "started_at": "2026-04-20T…Z", "ended_at": "2026-04-20T…Z"
  },
  "error": null
}
```

**Status vocabulary:** `ok | schema_invalid | timeout | denied | backend_error | budget_exhausted | depth_exceeded | manifest_invalid | input_invalid | scope_violation`.

When `status != ok`, `result` may be null and `error` carries `{code, message, retriable: bool}`.

## 8. Backend — claude-cli

### 8.1 Invocation

```
claude -p
  --agent plan-executor:<agent_name>
  --model <manifest.model or overrides.model>
  --allowedTools <csv from manifest.tools>
  --disallowedTools <csv — always includes Agent in v1>
  --add-dir <cwd>
  --output-format json
  --permission-mode acceptEdits
  [--settings <profile_path>]       # reserved, no-op in v1
  <user_prompt>
```

The user prompt is compact: *"Perform the task per your system prompt. Return ONLY a JSON document conforming to the schema below; no prose. Schema: `<schema>`. Payload: `<payload>`."* The agent's system prompt — its full body, its `tools:` declaration, its description — is loaded natively by `--agent` (Probe 1). No `--system-prompt-file`, no `--append-system-prompt` in v1.

### 8.2 Allowlist construction (closes Probe 2 gap)

Read the agent's frontmatter `tools:` field (comma-separated string), split and strip, pass as `--allowedTools`. Always append `Agent` to `--disallowedTools` (no nested dispatch from children in v1). Probe 2 proved this is mandatory — the CLI does not enforce `tools:` under `--agent` by itself.

### 8.3 Delta-bounded cleanup

Import `is_protected_path()` and the constants from `plugins/plan-executor/scripts/_plan_paths.py`. Before spawn, snapshot baseline via `git diff --name-only HEAD` + `git ls-files --others --exclude-standard`. After spawn, compute delta. For files in `observed_delta` but NOT in `declared_files_changed` AND NOT in the protected-paths set: restore from baseline (`git checkout -- <file>` for tracked modifications; delete for untracked additions). Matches the invariants enforced in `plan_codex_dispatch.py`.

**Scope reconciliation:**
- `scope_violation_detected` flips when an `observed_delta` file is not in `declared_files_changed` and not protected.
- `scope_misreport_detected` flips when a `declared_files_changed` entry is not in `observed_delta` (phantom declaration).

### 8.4 Subprocess management

`subprocess.run(cmd, capture_output=True, text=True, timeout=effective_timeout_sec, env=scrubbed_env, cwd=effective_cwd)`. On `TimeoutExpired`: SIGKILL the process group, drain buffers, run cleanup, emit `status: timeout` with the observed `duration_ms`.

## 9. Guardrails

### 9.1 Environment-variable protocol

(Preserved from v2 §9.1.)

Every variable is namespaced `PLAN_EXEC_*`. The wrapper both **reads** them from the caller's environment and **sets** them for the child.

| Var | Set by | Meaning | Child behavior |
|---|---|---|---|
| `PLAN_EXEC_RUN_ID` | top-level caller | UUID of the whole tree | inherited |
| `PLAN_EXEC_SPAN_ID` | wrapper per hop | UUID of this hop | overwritten each hop |
| `PLAN_EXEC_PARENT_SPAN` | wrapper per hop | parent hop's `SPAN_ID` | inherited as span parent |
| `PLAN_EXEC_DEPTH` | wrapper | int, 0 at top | incremented before spawn; refuses if `>= MAX_DEPTH` |
| `PLAN_EXEC_MAX_DEPTH` | top-level config | hard cap | inherited; child cannot raise |
| `PLAN_EXEC_TRACE` | wrapper | `>`-joined agent names | appended each hop |
| `PLAN_EXEC_COST_USD` | wrapper | cumulative cost to date | updated post-hop |
| `PLAN_EXEC_COST_CAP_USD` | top-level config | hard cap for whole tree | inherited |
| `PLAN_EXEC_DISABLE_NESTED` | operator / wrapper | killswitch (`"1"` denies) | wrapper refuses spawn |
| `PLAN_EXEC_LOG_DIR` | top-level config | dir for JSONL spans | inherited |
| `PLAN_EXEC_PARENT_PID` | wrapper | parent PID | loop detection |
| `PLAN_EXEC_ALLOWED_AGENTS` | optional | CSV whitelist | wrapper refuses unlisted agents |
| `PLAN_EXEC_DRY_RUN` | operator | `"1"` short-circuits spawn | emits plan-only envelope |

### 9.2 Pre-spawn refusal matrix

(Preserved from v2 §9.2, with one v3 row added.)

| Condition | Action | Envelope |
|---|---|---|
| `PLAN_EXEC_DISABLE_NESTED=1` | refuse | `status: denied`, `code: killswitch` |
| `PLAN_EXEC_DEPTH >= PLAN_EXEC_MAX_DEPTH` | refuse | `status: depth_exceeded` |
| `PLAN_EXEC_COST_USD + manifest.cost_cap_usd > PLAN_EXEC_COST_CAP_USD` | refuse | `status: budget_exhausted` |
| agent not in `PLAN_EXEC_ALLOWED_AGENTS` (if set) | refuse | `status: denied`, `code: agent_not_allowed` |
| manifest / input schema invalid | refuse | `status: manifest_invalid` / `input_invalid` |
| cwd not within allowed scope | refuse | `status: denied`, `code: cwd_out_of_scope` |
| call chain contains this agent twice | warn; allow | noted in trace |
| **(v3)** `agent` not in `{plan-analyst, plan-implementer, plan-remediator}` | refuse | `status: input_invalid`, `code: agent_not_dispatchable` |

### 9.3 Spawn-time hardening

- **Env scrubbing.** Start from `{k: v for k, v in os.environ.items() if k in env_allowlist or k.startswith("PLAN_EXEC_")}`. Default allowlist: `PATH`, `HOME`, `LANG`. Callers can extend via `guardrails.env_allowlist_extra`. Per-hop `PLAN_EXEC_*` vars are injected after scrub.
- **Tool scrubbing.** `--disallowedTools Agent` always, unless a future version explicitly supports multi-hop (not in v1).
- **Recursion killswitch (single layer).** Child's env has `PLAN_EXEC_DISABLE_NESTED=1` when wrapper-level max_spawn_depth is zero or exhausted; combined with `--disallowedTools Agent`, the child has neither the env capability nor the CLI capability to re-spawn. v2's Bash-allowlist layer is dropped as (a) paranoid for the honest-mistake threat model, and (b) insufficient for the hostile-subagent model (which is out-of-scope for v1).
- **Network scoping.** `guardrails.network: "deny"` → add `WebFetch,WebSearch` to `--disallowedTools`.
- **Cwd sandbox.** Spawn cwd = `overrides.cwd` or repo root. Pass `--add-dir` for each additional explicit path.
- **Permission mode.** `acceptEdits` is the default — conditional on Probe 2b confirming allowlist enforcement. If Probe 2b fails, the wrapper falls back to a tighter scheme (see §11).
- **Timeout.** `subprocess.run(..., timeout=effective_timeout_sec)`. On `TimeoutExpired`, SIGKILL, drain, cleanup, envelope.

### 9.4 Post-spawn accounting

- Parse `total_cost_usd` from Claude's JSON output; update `PLAN_EXEC_COST_USD` for downstream hops.
- Append one JSONL record to `$PLAN_EXEC_LOG_DIR/spans.jsonl` (default: `docs/plans/spans.jsonl`) per hop. Fields: `run_id`, `span_id`, `parent_span_id`, `depth`, `call_chain`, `agent`, `status`, `cost_usd`, `duration_ms`, `tokens`, `started_at`, `ended_at`.
- This log is **distinct** from the orchestrator's `docs/plans/_run_log.jsonl`. Escape-hatch dispatches are subagent-initiated; they do not belong in the orchestrator's event stream.
- Bubble `permission_denials` into the envelope (from Claude's JSON) for caller inspection.

## 10. CLI surface

```
plan_claude_dispatch.py run  --input <path|-> [--output <path|->]
                             [--timeout <N>] [--dry-run] [--verbose]
                             [--backend-binary <path>]    # test seam
plan_claude_dispatch.py list-agents
plan_claude_dispatch.py show-agent <name>                 # resolved manifest JSON
plan_claude_dispatch.py validate-input  <path|->
plan_claude_dispatch.py validate-output <path|->
```

Exit codes: `0` on `status: ok`; `1` on any non-ok status (with envelope on stdout); `2` on wrapper-level failure (malformed input, I/O error). A non-zero exit with a valid envelope on stdout is the expected shape for recoverable problems — callers read the envelope, not the exit code, for diagnosis.

## 11. Security model

(Preserved from v2 §11; one paragraph added for permission-mode.)

- **Auth.** Nested Claude inherits the parent's OAuth session or `ANTHROPIC_API_KEY`. There is no in-process way to sandbox billing. Operators wanting a hard budget must set `PLAN_EXEC_COST_CAP_USD` at the top of the tree.
- **Secrets.** Env allowlist (§9.3) is the chokepoint. Callers can extend but not remove the default allowlist baseline.
- **Filesystem.** `--add-dir` is the only filesystem boundary — children can read/write everything under any passed dir. Narrow cwd and delta-bounded cleanup catch out-of-scope writes.
- **Network.** `guardrails.network: "deny"` → `--disallowedTools WebFetch,WebSearch`.
- **Recursion.** Depth, cost, and agent-allowlist guardrails compose. A misbehaving child cannot raise its own caps. `Agent` is in `--disallowedTools` for the child, and `PLAN_EXEC_DISABLE_NESTED=1` in its env.
- **Hook isolation.** `--settings` (reserved; no-op in v1) will later point to a frozen profile that disables child-side hooks, preventing the child's execution from firing the parent's auto-memory, status-line, etc.

**Permission mode (v3 addition).** `acceptEdits` is the default. Probe 2 proved that `bypassPermissions` defeats `--allowedTools`. The probe supplement asserts — but did not test — that `acceptEdits` respects `--allowedTools`. v3 makes **Probe 2b** (TASK-007) a build gate: if `acceptEdits` also ignores the allowlist, v3 falls back to `bypassPermissions` plus strict cwd sandbox plus mandatory delta-bounded cleanup, and the README documents that tool-level enforcement is advisory only while cleanup is authoritative.

## 12. Verification

The plan is complete when all of the following are true:

1. `plan_claude_dispatch.py run --input <payload>` dispatches `plan-implementer` against a trivial stub task and returns a schema-valid envelope with `status: ok` and a valid inner `result`.
2. **Probe 2b passes** in one of two forms:
   - **Pass A:** `acceptEdits` + `--allowedTools Read,Grep,Glob,Bash` causes `Write` to populate `permission_denials` and produce no file on disk.
   - **Pass B:** `Write` produces a file but delta-bounded cleanup reverts it; envelope carries `scope_violation_detected: true`.
   - **Fail case** (neither A nor B holds) is a ship-blocker; the plan returns for revision.
3. Each refusal row in §9.2 is exercised by an integration test.
4. A subagent with only `Bash` successfully invokes the script and receives an envelope back — escape-hatch is end-to-end.
5. `spans.jsonl` contains one entry per hop for a 2-hop test; `depth`, `call_chain`, and cumulative cost are correct.
6. Concurrent dispatches on disjoint file sets do not cross-contaminate baseline snapshots or cleanup (narrow integration test; full conflict matrix deferred).
7. `README_claude_dispatch.md` documents env vars, refusal matrix, dispatchable-agent set, and the "inner CLI shares billing with parent" caveat.
8. Existing plan-executor test suite passes; new tests cover ≥85% of `plan_claude_dispatch.py` lines.

## 13. Open questions

- **`--settings` profile** is exposed in the CLI but a no-op in v1. Concrete hook-isolation or settings-override use case needed before it becomes load-bearing. Left as reserved for now.
- **Span-log location.** Default `docs/plans/spans.jsonl` keeps observability close to `_run_log.jsonl`. Alternative: `plugins/plan-executor/logs/spans.jsonl`. Pick at build time; not a design-breaking choice.
- **Baseline-snapshot factoring with Codex wrapper.** `plan_codex_dispatch.py` has inline baseline + delta cleanup. TASK-004 includes a conditional extraction rule: extract into a shared helper if the diff is ≤100 lines, else duplicate in the Claude wrapper and file an extraction follow-up.
- **Existing agent frontmatter unchanged in v1.** If wrapper later needs per-agent `cost_cap_usd` or `max_spawn_depth`, we add an additive `dispatch:` block. Not needed now because env-var guardrails operate at the call-tree level.

## 14. Tasks

## TASK-001: Input / output JSON schemas + envelope builder module

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json` (create)
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_output.json` (create)
  - `plugins/plan-executor/scripts/_claude_dispatch_envelope.py` (create)
  - `tests/scripts/test_claude_dispatch_envelope.py` (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_dispatch_envelope.py`
- **Acceptance criteria:**
  - Schemas validate the examples in §6 / §7; `additionalProperties: false`.
  - `_claude_dispatch_envelope.py` exposes `build_ok()`, `build_denied()`, `build_timeout()`, `build_schema_invalid()`, `build_backend_error()`, `build_scope_violation()`, `build_input_invalid()`, `build_manifest_invalid()`, `build_depth_exceeded()`, `build_budget_exhausted()` matching the §7 status vocabulary.
  - Constructor rejects unknown top-level keys.

## TASK-002: Manifest loader + guardrail engine

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_agent_manifest.py` (create)
  - `plugins/plan-executor/scripts/_claude_guardrails.py` (create)
  - `tests/scripts/test_claude_agent_manifest.py` (create)
  - `tests/scripts/test_claude_guardrails.py` (create)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_agent_manifest.py tests/scripts/test_claude_guardrails.py`
- **Acceptance criteria:**
  - `load_agent(name)` reads existing `plugins/plan-executor/agents/<name>.md` frontmatter, returns dict with `tools` (list[str]), `model` (str), `description` (str).
  - Rejects names outside `{plan-analyst, plan-implementer, plan-remediator}` with `AgentNotDispatchable`.
  - `evaluate_preflight(manifest, input, env)` returns `(allow: bool, envelope_if_denied: dict | None)` covering every row in §9.2.
  - `scrub_env(manifest, parent_env)` keeps `env_allowlist + PLAN_EXEC_*`, injects per-hop vars.

## TASK-003: claude-cli backend adapter

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_backend.py` (create)
  - `tests/scripts/test_claude_backend.py` (create)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_backend.py`
- **Acceptance criteria:**
  - `invoke(manifest, effective, payload, trace)` returns an envelope.
  - Argv built per §8.1; `Agent` always in `--disallowedTools`; `--add-dir` from effective cwd; `--permission-mode acceptEdits`.
  - Captures stdout, parses JSON, maps `result` / `duration_ms` / `total_cost_usd` / `session_id` / `usage` into envelope.
  - `TimeoutExpired` → `status: timeout`; non-JSON stdout → `status: backend_error, code: malformed_output`.
  - `--backend-binary` test seam (tiny shim script); real-CLI path gated on `PLAN_EXEC_E2E=1`.

## TASK-004: Delta-bounded cleanup

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` (create)
  - `tests/scripts/test_claude_dispatch_cleanup.py` (create)
- **Dependencies:** TASK-003
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_dispatch_cleanup.py`
- **Acceptance criteria:**
  - `snapshot_baseline(repo_root)` captures tracked + untracked state via git plumbing, mirroring the Codex wrapper.
  - `apply_cleanup(baseline, declared_files_changed, repo_root)` restores / deletes files in `(observed_delta − declared_files_changed − protected_paths)`. Imports `_plan_paths.is_protected_path`.
  - `scope_violation_detected` flips on out-of-declaration writes; `scope_misreport_detected` flips on phantom declarations.
  - Concurrent dispatches on disjoint file sets do not cross-contaminate (integration test with two wrapper processes).
  - **Factoring rule:** if sharing logic with `plan_codex_dispatch.py` is a ≤100-line extraction, do it now and update both wrappers in one PR; otherwise duplicate in the Claude wrapper and file a follow-up task.

## TASK-005: CLI entrypoint `plan_claude_dispatch.py`

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (create)
  - `tests/scripts/test_plan_claude_dispatch_cli.py` (create)
- **Dependencies:** TASK-002, TASK-003, TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_claude_dispatch_cli.py`
- **Acceptance criteria:**
  - Subcommands per §10.
  - `run` wires: preflight → baseline → backend dispatch → cleanup → schema-validate inner result → envelope emit → span append.
  - Stdin/stdout `-` syntax works for `--input` and `--output`.
  - Exit codes per §10.
  - `--dry-run` returns a plan-only envelope with `status: ok, dry_run: true` and no spawn.
  - Malformed input → exit 2 + `status: input_invalid` envelope on stdout.

## TASK-006: Span log (`spans.jsonl`)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/_claude_span_log.py` (create)
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (modify — add `append_span` call)
  - `tests/scripts/test_claude_span_log.py` (create)
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_span_log.py`
- **Acceptance criteria:**
  - `append_span(log_dir, envelope)` atomically writes one JSON line to `$log_dir/spans.jsonl`.
  - Default log dir: `docs/plans/` (sibling of `_run_log.jsonl`).
  - Multi-hop test (wrapper calling wrapper) produces N spans with correct parent links.

## TASK-007: Probe 2b — `acceptEdits` allowlist enforcement (build gate)

- **Status:** pending (blocks TASK-003 final sign-off)
- **Priority:** high
- **Files:**
  - `tests/scripts/test_claude_permission_mode_probe.py` (create)
- **Dependencies:** none
- **Test command:** `PLAN_EXEC_E2E=1 venv/bin/python -m pytest tests/scripts/test_claude_permission_mode_probe.py`
- **Acceptance criteria:**
  - Test is skipped by default; runs only when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
  - Invokes `claude -p --agent plan-executor:plan-analyst --permission-mode acceptEdits --allowedTools Read,Grep,Glob,Bash --output-format json` with a prompt to `Write` a file in a tmp dir.
  - **Pass case A:** `permission_denials` non-empty AND no file on disk → proceed with `acceptEdits` as default permission mode.
  - **Pass case B:** file created but delta-bounded cleanup reverts it → proceed; README elevates cleanup as the primary safety mechanism and notes allowlist is advisory only.
  - **Fail case:** file created and not reverted → ship-blocker; revise v3 to use strict cwd sandbox + cleanup as sole mechanism.

## TASK-008: End-to-end escape-hatch integration test

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `tests/scripts/test_claude_dispatch_e2e.py` (create)
  - `tests/fixtures/claude_dispatch/minimal_payload.json` (create)
- **Dependencies:** TASK-005, TASK-007
- **Test command:** `PLAN_EXEC_E2E=1 venv/bin/python -m pytest tests/scripts/test_claude_dispatch_e2e.py`
- **Acceptance criteria:**
  - Skipped by default; runs when `PLAN_EXEC_E2E=1` AND `claude` is on PATH.
  - Case 1: `agent: plan-analyst` against a stub plan → `status: ok`, valid inner result, a `spans.jsonl` entry.
  - Case 2: malformed input → exit 2 + `status: input_invalid`.
  - Case 3: two parallel wrapper invocations on disjoint file sets succeed; baselines do not cross-contaminate.

## TASK-009: README + security doc

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/README_claude_dispatch.md` (create)
  - `README.md` (modify)
- **Dependencies:** TASK-005, TASK-007
- **Test command:** none
- **Acceptance criteria:**
  - `README_claude_dispatch.md` documents: escape-hatch use case; input / output shape; every `PLAN_EXEC_*` env var; the refusal matrix (§9.2); inner CLI shares billing with parent; the v1 dispatchable-agents set; the outcome of Probe 2b (Pass A or B) and what it means for safety invariants.
  - Quickstart example: ≤10-line JSON payload for `plan-implementer`.
  - Top-level `README.md` gains a one-paragraph pointer to the new wrapper.

---

## Appendix A — Example invocations

### A.1 Claude-side implementer from a subagent (Bash-only context)

```bash
venv/bin/python plugins/plan-executor/scripts/plan_claude_dispatch.py run \
    --input - --output - <<'EOF'
{
  "schema_version": 1,
  "agent": "plan-implementer",
  "payload": {
    "task_id": "010",
    "task_block": "### TASK-010: …",
    "plan_path": "/abs/path/plan.md",
    "base_sha": "abc123"
  },
  "output_instructions": {
    "schema_path": "plugins/plan-executor/scripts/schemas/implementer_report.json"
  },
  "overrides": { "timeout_sec": 900 },
  "trace": { "run_id": "…", "parent_span_id": "…", "depth": 1,
             "call_chain": ["orchestrator", "subagent-A"] }
}
EOF
```

### A.2 Dry-run (no spawn)

```bash
PLAN_EXEC_DRY_RUN=1 plan_claude_dispatch.py run --input payload.json
# emits envelope with status: ok, trace.depth, planned argv; does not spawn claude -p
```

### A.3 Manifest inspection

```bash
plan_claude_dispatch.py show-agent plan-implementer
# prints resolved manifest JSON (tools, model, description)
```

---

## Appendix B — Local-LLM probe findings (deferred to follow-on plan)

Full preservation of the qwen probe supplement's load-bearing findings, so the follow-on local-llm plan starts warm.

### B.1 Host environment

- Ollama on Windows (v0.20.7), bound to `0.0.0.0:11434`.
- Models resident: `qwen2.5-coder:14b-instruct-q4_K_M` (8.4 GB), `lfm2.5-thinking:latest` (700 MB).
- WSL clients compute gateway IP per shell via `.bashrc`:
  ```bash
  export OLLAMA_HOST="http://$(ip route show | grep -i default | awk '{print $3}'):11434"
  ```

### B.2 Transport decision

- **Primary:** native `ollama.Client` with `format=<schema>` for schema-constrained decoding. 4/4 schema-valid outputs on a realistic reviewer report across back-to-back runs.
- **Secondary:** `openai.OpenAI(base_url=.../v1, api_key="ollama-stub", max_retries=0)` with `response_format={"type":"json_object"}` + post-hoc `jsonschema.validate`. Reserved for vLLM, TGI, LM Studio (non-Ollama endpoints).
- `api_key` required by the OpenAI SDK but ignored by Ollama. Pass literal `"ollama-stub"` — do **not** source from caller `OPENAI_API_KEY`.

### B.3 Gotchas (load-bearing for the follow-on adapter)

1. **`max_retries=0` mandatory on `OpenAI()`.** Default is 2 retries, silent. Without it, a declared `timeout=5` on an unreachable endpoint takes ~16s wall. Unit test must assert the property on the constructed client.
2. **Context overflow is silent.** A 67.5k-token blob with `num_ctx=32768` returns HTTP 200 with `prompt_eval_count=32768` — Ollama drops the overflow. Adapter must pre-count input tokens and refuse with `status: input_invalid, code: context_overflow` when `estimated_input + max_output_tokens > max_context`.
3. **Tool-use is broken on qwen-coder:14b.** The model emits raw JSON (`{"name": ..., "arguments": {...}}`) without the `<tool_call>` tags Ollama's post-processor looks for. Both OpenAI-compat and native transports return `tool_calls: []`. Adapter must refuse tool-enabled manifests with `status: manifest_invalid, code: tools_unsupported`.
4. **Server-side prompt-eval cache is real but not exposed.** Prompt-eval latency drops ~47% on the second call with the same system prompt. No `cache_creation` / `cache_read` counters — only `prompt_eval_duration`.
5. **No modelfile parameter defaults.** `/api/show` returns `parameters: None`. Adapter must set `num_predict`, `num_ctx`, `temperature` explicitly on every call — no fall-through.
6. **Generation rate ≈ 28 tokens/s** on the deployed host. `default_timeout_sec: 300` is comfortable for a 144-token reviewer report (~10s wall); `120` is a reasonable tight default once measured.

### B.4 Semantic-quality concern (policy for the follow-on plan)

qwen-coder:14b misclassified a real `hmac.compare_digest → ==` timing-attack regression as `severity: low, category: style` across 4 deterministic runs. The model is reliably deterministic under schema-constrained decode, but determinism ≠ correctness. Local-llm review is **triage, not final sign-off.** The follow-on plan must define which case classes local-llm is safe for (likely: first-pass bulk filtering of obvious issues; NOT security-sensitive diffs, NOT final approval, NOT diffs touching auth / crypto / session handling).

### B.5 Error-to-envelope mapping (for future adapter)

| Exception / condition | Envelope |
|---|---|
| `APIStatusError` 404 with `not_found_error` | `status: backend_error, code: model_not_found, retriable: false` |
| `APIStatusError` 400 / 422 | `status: backend_error, code: bad_request, retriable: false` |
| `APIStatusError` 5xx | `status: backend_error, code: server_error, retriable: true` |
| `APIConnectionError` | `status: backend_error, code: endpoint_unreachable, retriable: true` |
| `APITimeoutError` | `status: timeout, retriable: true` |
| Post-hoc schema validation fails twice | `status: schema_invalid, retriable: false` |
| Preflight token-count overflow | `status: input_invalid, code: context_overflow` |
| Preflight tool-use declared | `status: manifest_invalid, code: tools_unsupported` |

---

## Appendix C — Why v2's cache-TTL machinery was unnecessary

v2 §3.1 / §3.2 / §3.3 designed a per-agent TTL-selection system against the assumption that the default cache TTL is 5 minutes (`ephemeral_5m`). The elaborate cost matrix, the `dispatch.cache_ttl` manifest field, the `--settings` injection fallback, the quarterly measurement-and-revision protocol — all of it addresses the question *"which agents should pay the 2× cache-creation premium for a 1-hour cache?"*

Probe 3 showed the question is moot: subagent system prompts land in the `ephemeral_1h` cache by default. Three back-to-back plan-analyst dispatches with identical-shape prompts each hit the same warmed cache (1,856 cache-creation tokens + 27,669 cache-read tokens per call, flat). The probe was executed across runs separated by several minutes; all three read the cache, confirming a >5-minute TTL.

v3's cache story is one line: *"The first dispatch of each distinct subagent in a run tree pays cache-creation; subsequent dispatches within ~1 hour read the cache. The wrapper does not need to manage TTL."*

If a future operator measures a real cost-per-run problem driven by cache misses, re-introducing `dispatch.cache_ttl` is an additive change — but it is not a v1 requirement and the v2 design was solving a non-problem.

---

## Appendix D — Probe 2b procedure

Run before any wrapper code is committed. The outcome determines whether `acceptEdits` is the wrapper's default permission mode (Pass A), whether delta-bounded cleanup is the sole enforcement mechanism with `acceptEdits` (Pass B), or whether v3 itself must be revised (Fail).

### D.1 Script

```bash
#!/usr/bin/env bash
set -e
cd "$(mktemp -d)"

claude -p \
    "Use the Write tool to create ./probe2b.txt with the contents 'hello'. \
     If you cannot use Write, say exactly: NO_WRITE_TOOL. Do not use any other tool." \
    --agent plan-executor:plan-analyst \
    --model haiku \
    --allowedTools "Read,Grep,Glob,Bash" \
    --output-format json \
    --permission-mode acceptEdits \
    > output.json

echo "--- envelope ---"
python -c "import json; d=json.load(open('output.json')); print('result:', d.get('result')); print('permission_denials:', d.get('permission_denials'))"
echo "--- filesystem ---"
ls -la probe2b.txt 2>/dev/null && echo "FILE CREATED" || echo "no file"
```

### D.2 Acceptance matrix

| `permission_denials` | `probe2b.txt` | Verdict | Action |
|---|---|---|---|
| non-empty (contains `Write`) | absent | **Pass A** | `acceptEdits` enforces `--allowedTools`. Proceed with v3 as written. |
| empty | present | **Pass B** | `acceptEdits` does NOT enforce `--allowedTools`. v3 still safe: delta-bounded cleanup reverts the file in a real dispatch. Document in README that tool-level enforcement is advisory; cleanup is authoritative. |
| empty | present, and cleanup (simulated) does not revert | **Fail** | Ship-blocker. Revise v3 to default to `bypassPermissions` + strict cwd sandbox + mandatory cleanup; or escalate with a Claude Code CLI issue. |

### D.3 Outcome recording

Append the probe result to `docs/plans/_run_log.jsonl` as a one-off event:

```json
{"ts": "2026-04-20T…Z", "event": "probe_2b_result", "verdict": "pass_a", "permission_denials": [...], "file_created": false}
```

And to the TASK-007 acceptance-criteria checklist. No dedicated markdown report artifact is required; the one-line log event is the durable record.
