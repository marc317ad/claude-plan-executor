# Plan: Nested Subagent Dispatch (`plan_nested_dispatch.py`) — v2

**Created:** 2026-04-18 (v2 revised same day)
**Status:** draft — specification + implementation plan
**Base branch:** main
**Related:** `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`, `plugins/plan-executor/scripts/plan_codex_dispatch.py`, v1 `PLAN_NESTED_DISPATCH_2026-04-18.md`

**Changes from v1:**
- §2.1 added: "fresh minds by default" promoted from a v1 risk into a load-bearing design principle.
- §3 reframed: "Observed risks" → "Observed properties". Billing bleed-through demoted to an operational note; context amnesia removed (now a feature per §2.1); recursion reframed as a permissions problem, not a runtime check.
- §3.1 added: "Cache economics in practice" — TTL-aware cost table and mitigations.
- §3.2 added: "Prefix-stability discipline and per-agent TTL selection" — mandatory prompt ordering, new `dispatch.cache_ttl` manifest field, per-agent defaults, measurement-driven revision protocol.
- §3.3 added: "Backend responsibility for cache_ttl" — CLI-vs-SDK fallback for TTL control.
- §6.2 updated: manifest schema gains `dispatch.cache_ttl`.
- §7.1 updated: claude-cli backend spec routes `cache_ttl` via `--settings` or SDK fallback; prompt assembly must honor §3.2 segment ordering.
- §9.3 updated: explicit two-layer recursion killswitch (env flag + narrow Bash allowlist) for agents with `max_spawn_depth: 0`.

---

## 1. Goal

Build a single, configurable dispatch script — `plugins/plan-executor/scripts/plan_nested_dispatch.py` — that lets any Claude agent (including a subagent that lacks the `Agent` tool) spawn a fully-featured worker agent. Backends: Claude Code CLI, Codex CLI, and a pluggable local-LLM adapter. The script is the one sanctioned chokepoint for nested agent dispatch; every hop traverses its guardrails, its structured input contract, and its structured output envelope.

## 2. Context — Why

The Claude Code harness grants the `Agent` tool only to the top-level session. Subagents spawned via that tool inherit a reduced toolset and cannot spawn further agents — calling `Agent` from a subagent crashes the session. This blocks natural patterns in the plan-executor: a `plan-implementer` cannot delegate a narrow research query, a `plan-remediator` cannot run a Codex cross-check without the orchestrator's involvement, and Claude-tier tasks cannot parallelize without returning control to the top level.

**Validated escape hatch (experiment, 2026-04-18):** a subagent with only `Bash` can shell out to the `claude` CLI (`claude -p --output-format json --permission-mode bypassPermissions …`). That nested `claude` is a fresh top-level session with full tool access, including `Agent`. End-to-end chain `subagent → python → claude -p → Agent tool → sub-sub-agent` returned the sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093. See §3 for measurements and §4 for the prototype.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and schema plumbing across callers, (b) leak auth/billing/recursion controls, and (c) have no consistent structured input/output — exactly the problems `plan_codex_dispatch.py` solved for Codex. This plan specifies the equivalent wrapper for nested Claude and, as a pleasant side effect, unifies Codex and future local-LLM dispatch behind one contract.

### 2.1 Design principle — fresh minds by default

Children start with no conversation history, no inherited memory, and no residue from the parent's reasoning trail. This is **load-bearing**, not incidental. A parent that has accumulated 50k+ tokens of state — plan-wide discussion, earlier tool outputs, branches considered and rejected — can hand a child only the 1–3k tokens strictly needed to do its job. The child reasons on a clean slate and produces more accurate output than a sibling working at the bottom of a long transcript would.

The implication is an obligation, not a hazard: callers must pack every fact the child needs into the input payload (`task_block`, `base_sha`, `analyst_annotations`, relevant diffs, any non-obvious constraints). Anything assumed but unstated will be invisible to the child. The manifest body + payload pair is the total world the child sees; design payloads accordingly. The wrapper never leaks parent conversation state — that's the feature, not a bug.

Corollary: do not "pass context for safety." Aggressive payload minimization is the point. If the child gets too much, you've recreated the parent's blurry context inside the child and lost the benefit.

## 3. Experiment summary

Two hops were validated:

| Hop | What | Outcome | Duration | Cost |
|-----|------|---------|----------|------|
| 1 | Top-level Claude → `claude -p` → Agent tool → sub-sub | `NESTED_AGENT_OK` returned | 6.50 s | $0.2447 |
| 2 | Top-level Claude → `Agent` subagent (no Agent tool) → `python3 /tmp/spawn_nested.py` → `claude -p` → Agent tool → sub-sub | `DEEP_NESTED_OK_a3f9c2` returned | 6.34 s (outer) | $0.0933 |

**Confirmed:**
- Inner `claude -p` has the `Agent` tool.
- `--output-format json` is `json.load()`-able; `result`/`duration_ms`/`total_cost_usd`/`session_id`/`usage` fields are stable.
- `--permission-mode bypassPermissions` avoids interactive prompts in non-TTY spawn.
- Warm-cache hops are ~6 s and <$0.10. Cold cache is the ~24 k token system-prompt read (see §9).

**Observed properties:**
- **Auth scope (risk).** `bypassPermissions` is inherited. Sandbox must come from `--add-dir`/cwd, not interactive prompts.
- **Recursion control (risk, mitigated).** The CLI does not self-limit; a nested agent could, in principle, invoke the wrapper endlessly. The correct framing is *permissions*, not runtime: agents whose manifest sets `max_spawn_depth: 0` are spawned with `PLAN_EXEC_DISABLE_NESTED=1` in their env **and** a narrow `Bash` allowlist that excludes `plan_nested_dispatch.py`. Two independently sufficient layers. See §9.3.
- **Cache economics (cost).** First spawn of each distinct agent system-prompt in each TTL window pays cache-creation cost (~1.25× base input on the cached prefix). Warm-chain hops read the cache at ~0.1× base. Full model in §3.1.
- **Shared billing (operational note).** Inner CLI bills the same account as the parent. Accept as-is for v1; the `PLAN_EXEC_COST_CAP_USD` guardrail is an advisory tree-wide budget, not a per-subtree enforceable limit.

*Context amnesia was listed as a risk in v1. It is the opposite — see §2.1.*

### 3.1 Cache economics in practice

Anthropic's prompt cache is keyed by content-hash + API key + organization. A `claude -p` spawn sends the Claude Code system prompt (~24k tokens in the cached prefix) as the first input segment. That segment is cache-creation on first use and cache-read on hit. Pricing on the cached portion:

| Event | Rate | ~24k prefix cost | Notes |
|---|---|---|---|
| First spawn of a given prefix (cold) | 1.25× base input | ~30k token-equiv billed | cache-creation tokens |
| Spawn within same TTL window, same prefix | 0.10× base input | ~2.4k token-equiv | cache-read tokens |
| Spawn after TTL expiry | 1.25× base input | ~30k token-equiv | re-creation |

TTL: **5 min default** (`ephemeral_5m`). A 1-hour tier (`ephemeral_1h`) costs ~2× on creation; opt-in via settings.

**What actually drives cost in a run tree:**
- **Distinct prefixes = distinct cache slots.** Each agent's `.md` body becomes part of the system prompt. Ten different agents = ten slots, each warmed independently. Repeated dispatch of the *same* agent is cheap; round-robining across many distinct agents in a short window is expensive.
- **TTL default is 5 min.** `ephemeral_1h` pays off only when the run tree will re-hit the same prefixes repeatedly over >15 min.
- **Parallel fanout can race.** N simultaneous spawns of the same cold agent may each pay creation cost if their requests arrive before the first write completes. Serial chains amortize; wide fanouts of identical agents should be sized against this.
- **Per-subscription vs per-API-key.** On Claude Pro/Max the `total_cost_usd` from `claude -p --output-format json` is telemetry, not a dollar bill; rate-limit throughput is still shaped by cache hits. On raw API keys, the numbers are real money.

**Mitigations baked into the spec:**
- `--bare` (§7.1) drops dynamic system-prompt sections (cwd, git status, env info), stabilizing the cached prefix across machines and invocations.
- System prompt = manifest body, used verbatim. No per-call interpolation into the cached segment — anything caller-specific goes into the user prompt or an appended tail that sits *outside* the cached prefix.
- `--exclude-dynamic-system-prompt-sections` as a belt-and-braces option for CI-style runs.
- Prefer serial ordering for identical-agent chains when latency allows; reserve parallel fanout for *different* agents (where caches don't overlap anyway).

**TL;DR:** cold first hop ≈ $0.45 + output; warm subsequent hop ≈ $0.04 + output on the prefix (experiment observed ~$0.09 total, dominated by output tokens and a partially warm state).

### 3.2 Prefix-stability discipline and per-agent TTL selection

The cache is prefix-matched on content-hash. A prompt is cacheable up to the first byte of variation; every byte after that is recomputed. Maximizing reuse requires treating each agent's prompt as a **strictly ordered, stably composed artifact** — not an ad-hoc assembly built at dispatch time.

**Mandatory prompt layout for every `claude-cli` dispatch:**

```
[1] system_prompt     = manifest .md body, verbatim
[2] tool_definitions  = derived from manifest.dispatch.tools_override (stable per agent)
[3] output_contract   = stable block derived from manifest.output_schema
[4] append_tail       = manifest-declared --append-system-prompt text (stable per agent)
───────── cache boundary ─────────
[5] user_prompt       = caller-supplied payload; varies every call
```

Segments [1]–[4] are **byte-identical across every dispatch of the same agent** and form the cached prefix. Segment [5] carries per-call data and is never cached; it must come last.

**Violations that silently invalidate the cache:**

- Interpolating `task_id`, `base_sha`, timestamps, session ids, or cwd into segments [1]–[4].
- Letting the CLI's default system-prompt include dynamic sections (cwd, git status, env info). Mitigated by `--bare` and `--exclude-dynamic-system-prompt-sections`.
- Per-call edits to `tools_override.allowed` / `.disallowed` (changes segment [2]).
- Inlining a varying `output_schema` instead of referencing a stable path.
- Caller-supplied `overrides.system_prompt_append` touching segments [1]–[4]. The wrapper MUST reject overrides that would mutate the cached prefix; caller content lands in segment [5] exclusively.

**Cost matrix under prefix-stability discipline:**

| Run shape | Default (`5m`) | Premium (`1h`) | Winner |
|---|---|---|---|
| Tight batch, all dispatches <5 min, same agent | 1× creation + N reads | 1× creation (2×) + N reads | **5m** (saves 0.75× creation premium) |
| 45-min run, same agent, ~7 min between calls | 6–7× creations + reads | 1× creation + reads | **1h** decisively |
| 45-min run, 10 distinct agents round-robin | 60+ creations across slots | ~10 creations total | **1h** decisively |
| Multi-hour run exceeding 1 hr idle gaps | periodic re-creation | periodic re-creation | ~wash; pick `1h` for intra-hour density |

Row 1 is the parallel implementer fanout. Row 3 is the realistic plan-executor workload: batches of implementers interleaved with reviewers, remediators, and analysts across 30–60 min. A single global TTL is the wrong choice; **set TTL per agent**.

**New manifest field:** `dispatch.cache_ttl: "5m" | "1h"` (default `"5m"`).

**Recommended defaults per agent role:**

| Agent | `cache_ttl` | Rationale |
|---|---|---|
| `plan-implementer` | `5m` | fanout-tight; calls cluster within a batch |
| `codex-implementer` | `5m` | same fanout pattern |
| `plan-remediator` | `1h` | sparse, post-review; >5 min gaps typical |
| `plan-reviewer` | `1h` | interleaved, long-lived across a batch |
| `codex-reviewer` | `1h` | sparse cross-review cadence |
| `plan-analyst` | `1h` | once per plan; occasional re-runs |
| `codex-plan-reviewer` | `1h` | infrequent; long gaps |
| `local-llm-reviewer` | N/A | local backend owns its own cache semantics |

**Measurement and revision protocol:**

Each `spans.jsonl` record carries `tokens.cache_creation` and `tokens.cache_read`. After the first real plan run, compute per-agent **creation ratio** = `sum(cache_creation) / sum(cache_creation + cache_read)`. Rules:

- Ratio >20% on `5m` → **promote** that agent to `1h`.
- Ratio <5% on `1h` → **demote** that agent back to `5m` (the 0.75× creation premium is waste at that density).
- Ratio 5–20% → leave as-is; noise-dominated.

Review quarterly or after any change to agent system prompts, since prompt edits invalidate previously warmed cache slots.

### 3.3 Backend responsibility for `cache_ttl`

The `claude-cli` backend maps `manifest.dispatch.cache_ttl` onto the Anthropic API's `cache_control.ttl`. The `claude` CLI (v2.1.114 at time of writing) does not expose a direct flag for this in `-p` mode, so the backend picks one of three paths, in preference order:

1. **`--settings` injection.** Write a session-scoped JSON settings file setting `cacheControl.defaultTtl` (or the contemporary field name), pass via `--settings <path>`. Preferred when the setting exists in the deployed CLI version.
2. **SDK fallback.** Bypass the CLI and call the `anthropic` Python SDK directly, placing explicit `cache_control: {type: "ephemeral", ttl: "1h"}` on each cached segment. The manifest body remains the source of truth for the system prompt; the SDK call reproduces the CLI's tool/agent wiring under our control.
3. **Degrade.** If neither path is available, emit a warning in the envelope's `trace` and proceed at the CLI default TTL. Do not fail the dispatch — cache tier is an optimization, not a correctness requirement.

The backend adapter detects CLI version at startup and chooses a path. TASK-004 covers the implementation; a `--cache-backend-mode {cli,sdk,auto}` flag on `plan_nested_dispatch.py run` exists as a test/override seam.

## 4. Validated prototype (verbatim)

From the experiment; `/tmp/spawn_nested.py`:

```python
#!/usr/bin/env python3
import json
import subprocess
import sys

CMD = [
    "claude", "-p",
    "--output-format", "json",
    "--permission-mode", "bypassPermissions",
    (
        "Call the Agent tool with subagent_type='general-purpose', "
        "description='nested echo', "
        "prompt='Reply with exactly: DEEP_NESTED_OK_<random 6 hex chars>'. "
        "Report what the agent returned."
    ),
]

def main() -> int:
    try:
        proc = subprocess.run(CMD, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired as e:
        print(f"TIMEOUT after 180s: {e}", file=sys.stderr); return 2
    except FileNotFoundError as e:
        print(f"claude CLI not found: {e}", file=sys.stderr); return 3
    if proc.returncode != 0:
        print(f"claude exited {proc.returncode}\nSTDERR: {proc.stderr}", file=sys.stderr)
        return proc.returncode
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"JSON parse failed: {e}", file=sys.stderr); return 4
    print("=== result ===");         print(data.get("result"))
    print("=== duration_ms ===");    print(data.get("duration_ms"))
    print("=== total_cost_usd ==="); print(data.get("total_cost_usd"))
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

This is the minimum-viable kernel. The production script expands it along five axes: backend selection, agent-manifest resolution, structured I/O, guardrails, and observability.

## 5. Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│ Caller (orchestrator, subagent, another nested dispatch)           │
│   writes JSON payload → invokes plan_nested_dispatch.py run        │
└───────────────────────────────────┬────────────────────────────────┘
                                    │
                  ┌─────────────────▼───────────────────┐
                  │ plan_nested_dispatch.py             │
                  │  1. parse & validate input          │
                  │  2. resolve agent → manifest        │
                  │  3. enforce guardrails (§9)         │
                  │  4. build backend invocation        │
                  │  5. spawn backend (subprocess)      │
                  │  6. parse & validate inner output   │
                  │  7. emit dispatch envelope (§8)     │
                  │  8. append span to run log          │
                  └─────┬─────────────┬─────────────┬───┘
                        │             │             │
               ┌────────▼───┐  ┌──────▼──────┐  ┌───▼────────────┐
               │claude-cli  │  │ codex-cli   │  │ local-llm      │
               │ backend    │  │ backend     │  │ backend (stub) │
               │ (§7.1)     │  │ (§7.2)      │  │ (§7.3)         │
               └────────────┘  └─────────────┘  └────────────────┘
```

Single script, multiple backends. The agent manifest (§6) decides which backend is selected; callers specify the **agent name**, not the backend.

## 6. Agent registry & manifest

### 6.1 Registry layout

```
plugins/plan-executor/agents/
  plan-analyst.md          # existing
  plan-implementer.md      # existing
  plan-remediator.md       # existing
  plan-reviewer.md         # NEW — Claude-side review counterpart
  codex-implementer.md     # NEW — wraps plan_codex_dispatch implement
  codex-reviewer.md        # NEW — wraps plan_codex_dispatch review
  codex-plan-reviewer.md   # NEW — wraps plan_codex_dispatch plan-review
  local-llm-reviewer.md    # NEW — stub manifest for local adapter
  agents.index.json        # NEW — name → manifest path registry
```

Agents are addressed by **name**; the index resolves name → file. Ad-hoc agents can be passed via `manifest_path` (absolute path) bypassing the index.

### 6.2 Manifest frontmatter schema (extends existing)

Existing agents already carry `name`, `description`, `tools`, `model`. The dispatch wrapper adds a namespaced `dispatch:` block. **The body of the .md (below the frontmatter) is the system prompt**, reused verbatim.

```yaml
---
name: plan-implementer
description: …
tools: Read, Grep, Glob, Edit, Write, Bash
model: opus
dispatch:
  backend: claude-cli           # claude-cli | codex-cli | local-llm
  output_schema: plugins/plan-executor/scripts/schemas/implementer_report.json
  default_timeout_sec: 600
  default_effort: high          # low|medium|high|xhigh|max (Claude only)
  max_output_tokens: 16000
  max_spawn_depth: 1            # this agent may nest-dispatch at most 1 deeper
  network: deny                 # allow | deny  → --disallowedTools WebFetch,WebSearch
  cwd_scope:
    - "${REPO_ROOT}"
  write_allowlist:              # enforcement lives in orchestrator, declared here
    - "**/*"
  env_allowlist:                # everything else stripped before spawn
    - PATH
    - HOME
    - LANG
    - PLAN_EXEC_*
  required_env: []
  cost_cap_usd: 1.50            # per-call hard cap
  permission_mode: bypassPermissions
  tools_override:               # optional; else derived from frontmatter `tools`
    allowed: [Read, Grep, Glob, Edit, Write, Bash]
    disallowed: [Agent, WebFetch, WebSearch]
  settings_profile: null        # optional path to a --settings file
  codex:                        # only meaningful when backend: codex-cli
    subcommand: implement       # implement | review | plan-review
  local_llm:                    # only meaningful when backend: local-llm
    endpoint: "${PLAN_EXEC_LOCAL_LLM_URL}"
    model: "qwen2.5-coder:32b"
    api_kind: openai-compatible # openai-compatible | ollama | tgi
    max_context: 32768
---
```

**Why in the manifest and not in the caller:** the manifest is the agent's *constitution*. Callers override specific fields per-call (§7.4), but they cannot elevate an agent beyond its manifest caps — the wrapper enforces `min(caller_override, manifest_limit)` for timeouts, cost caps, tool sets, and spawn depth.

### 6.3 Index file

`agents.index.json`:

```json
{
  "schema_version": 1,
  "agents": {
    "plan-implementer":     "plugins/plan-executor/agents/plan-implementer.md",
    "plan-remediator":      "plugins/plan-executor/agents/plan-remediator.md",
    "plan-reviewer":        "plugins/plan-executor/agents/plan-reviewer.md",
    "plan-analyst":         "plugins/plan-executor/agents/plan-analyst.md",
    "codex-implementer":    "plugins/plan-executor/agents/codex-implementer.md",
    "codex-reviewer":       "plugins/plan-executor/agents/codex-reviewer.md",
    "codex-plan-reviewer":  "plugins/plan-executor/agents/codex-plan-reviewer.md",
    "local-llm-reviewer":   "plugins/plan-executor/agents/local-llm-reviewer.md"
  }
}
```

## 7. Backends

### 7.1 `claude-cli` backend

Spawn arguments (derived from manifest + overrides):

```
claude -p
  --output-format json
  --permission-mode <manifest.permission_mode>
  --model <effective_model>
  --effort <effective_effort>
  --add-dir <cwd>                        # per entry in cwd_scope
  --tools <allowed_tools_csv>            # from manifest.tools_override.allowed
  --disallowedTools <disallowed_csv>     # always includes Agent unless max_spawn_depth>0
  --system-prompt-file <manifest_body_path>
  --append-system-prompt "<output_instructions block>"
  --agents <json>                        # optional: register nested-allowed agents
  --settings <profile_path>              # if settings_profile set
  [--session-id <uuid>]                  # top-level only; children re-mint
  [--max-budget-usd <N>]                 # propagate effective budget
  --no-session-persistence               # default on; children are throwaway
  --bare                                 # strongly recommended for determinism
  "<user_prompt>"                        # compact prompt; the real payload is the appended system prompt
```

The caller-facing **user prompt** is kept thin ("Perform the task per your system prompt and the attached payload. Emit output per the output_instructions. Payload: <inline JSON>"). The heavy lifting — role, constraints, schema — goes in the system prompt so the API prompt cache can reuse it across hops.

### 7.2 `codex-cli` backend

Thin adapter around the existing `plan_codex_dispatch.py`:

```
python plugins/plan-executor/scripts/plan_codex_dispatch.py <subcommand> \
    --plan-file <payload.plan_path> \
    --task-id <payload.task_id> \
    --repo-root <cwd> \
    [--dry-run] [--timeout <N>]
```

The wrapper normalizes the existing Codex output into the shared dispatch envelope (§8). No changes to `plan_codex_dispatch.py` required beyond a thin `--envelope` output mode (TASK-004 below).

### 7.3 `local-llm` backend (stub, pluggable)

Defines an abstract `LocalLLMBackend` interface; the initial reference implementation targets OpenAI-compatible HTTP endpoints (covers Ollama, vLLM, LM Studio, text-generation-inference).

```python
class LocalLLMBackend:
    def invoke(self, *, system_prompt: str, user_prompt: str,
               output_schema: dict | None, timeout_sec: int,
               model: str, extra: dict) -> LocalLLMResult: ...
```

Config via `dispatch.local_llm` block on the manifest, plus `PLAN_EXEC_LOCAL_LLM_URL` env. Structured output via JSON-schema constrained decoding where the backend supports it (Ollama `format=json`, vLLM guided decoding); fallback to prompt-engineered JSON with post-hoc schema validation. Tool use for local models is **out of scope for v1** — local-llm agents are review/analysis roles, not implementers.

### 7.4 Backend-agnostic invocation

The script selects a backend purely from `manifest.dispatch.backend`. Adding a new backend is: one class implementing the interface + one registry entry. Callers never name a backend.

## 8. Contracts (input / output)

### 8.1 Input schema (`nested_dispatch_input_schema.json`)

```json
{
  "schema_version": 1,
  "agent": "plan-implementer",
  "manifest_path": null,
  "payload": {
    "task_id": "004E",
    "task_block": "### TASK-004E: …\n…",
    "plan_path": "/abs/path/to/plan.md",
    "base_sha": "abc123…",
    "analyst_annotations": { "…": "opaque pass-through" },
    "extra": { "…": "agent-specific additional fields" }
  },
  "output_instructions": {
    "format": "json",
    "schema_path": "plugins/plan-executor/scripts/schemas/implementer_report.json",
    "schema_inline": null,
    "top_level_key": null,
    "max_bytes": 65536
  },
  "overrides": {
    "model": null,
    "effort": null,
    "timeout_sec": null,
    "tools_allowed": null,
    "tools_disallowed": null,
    "cwd": null,
    "system_prompt_append": null,
    "user_prompt_prefix": null
  },
  "guardrails": {
    "max_depth": null,
    "cost_cap_usd": null,
    "network": null,
    "env_allowlist_extra": [],
    "allowed_agents": null,
    "disable_if_depth_at_least": null
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
- `agent` resolves via `agents.index.json`; `manifest_path` overrides.
- `payload` is opaque to the wrapper; it is serialized into the prompt.
- `output_instructions.schema_*` — at least one must be supplied; wrapper validates inner output.
- `overrides` fields are clamped by manifest caps (see §9).
- `trace` is typically filled by the caller. If absent at depth 0, the wrapper mints a new `run_id`.

### 8.2 Output envelope (`nested_dispatch_output_schema.json`)

```json
{
  "schema_version": 1,
  "status": "ok",
  "status_reason": null,
  "agent": "plan-implementer",
  "backend": "claude-cli",
  "model": "claude-opus-4-7",
  "session_id": "uuid",
  "depth": 1,
  "duration_ms": 12345,
  "cost_usd": 0.0933,
  "tokens": {
    "input": 13, "output": 235,
    "cache_read": 24145, "cache_creation": 36268
  },
  "result": { "…": "inner agent's structured result, schema-validated" },
  "result_raw_truncated": "… first N bytes of stdout …",
  "stderr_tail": "… last ~2KB of stderr …",
  "permission_denials": [],
  "trace": {
    "run_id": "uuid", "span_id": "uuid", "parent_span_id": "uuid",
    "depth": 1, "call_chain": ["orchestrator", "plan-implementer"],
    "started_at": "2026-04-18T…Z", "ended_at": "2026-04-18T…Z"
  },
  "error": null
}
```

**`status` vocabulary:** `ok | schema_invalid | timeout | denied | backend_error | budget_exhausted | depth_exceeded | manifest_invalid | input_invalid`.

**`denied`** is reserved for guardrail refusals (§9). When `status != ok`, `result` may be null; `error` carries a structured diagnosis `{ code, message, retriable: bool }`.

### 8.3 Output-instructions flow

The parent dictates the return shape by passing `output_instructions.schema_path` (or inline). The wrapper:

1. Reads the schema.
2. Appends a deterministic block to the inner agent's system prompt: *"Return ONLY a JSON document conforming to the schema below. No prose. Schema: `{schema}`."*
3. On receipt, `json.loads(inner.result)` and validate against the schema.
4. If invalid, retry **once** with an explicit correction prompt quoting the validation error. Second failure → `status: schema_invalid`.

This is the mechanism the user asked for: *"The output of these agents should either be a configurable json or other efficient object it can pass to the agent (output_instructions from parent)."*

## 9. Guardrails

### 9.1 Environment-variable protocol

Every variable is namespaced `PLAN_EXEC_*`. The wrapper both **reads** them from the caller's environment and **sets** them for the child.

| Var | Set by | Meaning | Child behavior |
|-----|--------|---------|----------------|
| `PLAN_EXEC_RUN_ID` | top-level caller | UUID of the whole tree | inherited |
| `PLAN_EXEC_SPAN_ID` | wrapper per hop | UUID of this hop | overwritten each hop |
| `PLAN_EXEC_PARENT_SPAN` | wrapper per hop | parent hop's `SPAN_ID` | inherited as span parent |
| `PLAN_EXEC_DEPTH` | wrapper | int, 0 at top | wrapper increments before spawn; refuses if `>= MAX_DEPTH` |
| `PLAN_EXEC_MAX_DEPTH` | top-level config | hard cap | inherited; child cannot raise |
| `PLAN_EXEC_TRACE` | wrapper | `>`-joined agent names | appended each hop |
| `PLAN_EXEC_COST_USD` | wrapper | cumulative cost to date | updated post-hop |
| `PLAN_EXEC_COST_CAP_USD` | top-level config | hard cap for whole tree | inherited |
| `PLAN_EXEC_DISABLE_NESTED` | operator | killswitch (`"1"` denies) | wrapper refuses spawn |
| `PLAN_EXEC_LOG_DIR` | top-level config | dir for JSONL spans | inherited |
| `PLAN_EXEC_PARENT_PID` | wrapper | parent PID | loop detection |
| `PLAN_EXEC_ALLOWED_AGENTS` | optional | CSV whitelist | wrapper refuses unlisted agents |
| `PLAN_EXEC_LOCAL_LLM_URL` | operator | local endpoint | read by local-llm backend |
| `PLAN_EXEC_DRY_RUN` | operator | `"1"` short-circuits spawn | wrapper emits a plan-only envelope |

### 9.2 Pre-spawn refusal matrix

| Condition | Action | Envelope |
|-----------|--------|----------|
| `PLAN_EXEC_DISABLE_NESTED=1` | refuse | `status: denied`, `code: killswitch` |
| `PLAN_EXEC_DEPTH >= PLAN_EXEC_MAX_DEPTH` | refuse | `status: depth_exceeded` |
| manifest `max_spawn_depth` exceeded | refuse | `status: depth_exceeded` |
| `PLAN_EXEC_COST_USD + manifest.cost_cap_usd > PLAN_EXEC_COST_CAP_USD` | refuse | `status: budget_exhausted` |
| agent not in `PLAN_EXEC_ALLOWED_AGENTS` (if set) | refuse | `status: denied`, `code: agent_not_allowed` |
| manifest/input schema invalid | refuse | `status: manifest_invalid`/`input_invalid` |
| cwd not within `manifest.cwd_scope` | refuse | `status: denied`, `code: cwd_out_of_scope` |
| call chain contains this agent twice | warn; allow | noted in trace (loop-prone roles should set `max_spawn_depth: 0`) |

### 9.3 Spawn-time hardening

- **Env scrubbing.** Start from `{k: v for k, v in os.environ.items() if k in manifest.env_allowlist or k.startswith("PLAN_EXEC_")}`. Add the per-hop `PLAN_EXEC_*` vars. Prevents secrets from leaking into children whose tool allowlist might exfiltrate them (e.g., an agent with Bash + network).
- **Tool scrubbing.** `--disallowedTools Agent` unless manifest `max_spawn_depth > 0` and current depth allows.
- **Recursion killswitch (defense-in-depth).** For any agent whose manifest sets `max_spawn_depth: 0`, or whose effective remaining depth is 0 after clamping against `PLAN_EXEC_MAX_DEPTH`, apply **both** of the following independently:
  1. **Env flag.** Spawn the child with `PLAN_EXEC_DISABLE_NESTED=1`. The wrapper refuses on that flag alone, regardless of any other state.
  2. **Bash allowlist.** If the child's manifest includes `Bash`, narrow it via `--tools 'Bash(git *) Bash(ls *) …'` (project-specific) that explicitly excludes `plan_nested_dispatch.py`. The wrapper's `show-agent` subcommand emits the recommended allowlist as an audit aid.

  Rationale: env-only enforcement fails if the env is stripped; tool-only enforcement fails if the Bash allowlist is too broad. Two layers, each sufficient alone, close the gap without coupling them. Do not rely on runtime depth arithmetic for this class of agent — don't hand them the key.
- **Network scoping.** `network: deny` → add `WebFetch,WebSearch` to disallowed tools.
- **Cwd sandbox.** Spawn with cwd = first entry of `cwd_scope`; pass `--add-dir` for each additional entry. Anything outside is invisible.
- **Permission mode.** `bypassPermissions` only when cwd sandbox and tool allowlist are both set. Otherwise fall back to `acceptEdits` and expect prompts to fail the spawn (surface as `permission_denials` in the envelope).
- **Settings profile.** Optional `--settings` path to a frozen JSON profile with hooks disabled — children should not trigger parent hooks.
- **Timeout.** `subprocess.run(..., timeout=effective_timeout_sec)`. On `TimeoutExpired`, SIGKILL the PG, drain buffers, return `status: timeout` with the partial stderr tail.

### 9.4 Post-spawn accounting

- Parse `total_cost_usd` from Claude JSON output; sum into `PLAN_EXEC_COST_USD` for downstream hops and the top-level aggregator.
- Append one JSONL record to `$PLAN_EXEC_LOG_DIR/spans.jsonl` per hop: input summary, envelope, tokens, timings. Mirror format of the existing `docs/plans/_run_log.jsonl`.
- Bubble permission denials into the envelope (from Claude's `permission_denials` array) so callers can inspect without re-parsing stdout.

## 10. CLI surface

```
plan_nested_dispatch.py run  --input <path|-> [--output <path|->]
                             [--timeout <N>] [--dry-run] [--verbose]
                             [--backend-binary <path>]     # test seam
plan_nested_dispatch.py list-agents
plan_nested_dispatch.py show-agent <name>                 # dumps resolved manifest JSON
plan_nested_dispatch.py validate-input  <path|->          # schema-check, resolve agent, no spawn
plan_nested_dispatch.py validate-output <path|->          # schema-check a prior envelope
```

`run` is the workhorse. Exit codes: `0` on `status: ok`; `1` on any non-ok status (with envelope on stdout); `2` on wrapper-level failure (malformed input, I/O errors). Non-zero exit + valid envelope is the expected shape for recoverable problems — callers read the envelope, not the exit code, for diagnosis.

## 11. Security model

- **Auth.** Nested Claude inherits the parent's OAuth session or `ANTHROPIC_API_KEY`. There is no in-process way to sandbox billing. Operators wanting a hard budget must set `PLAN_EXEC_COST_CAP_USD` at the top of the tree.
- **Secrets.** Env allowlist (§9.3) is the chokepoint. `forbidden_env` on the manifest adds named-deny entries over the allowlist baseline.
- **Filesystem.** `--add-dir` is the only filesystem boundary — children can read/write everything under any passed dir. Narrow `cwd_scope` in manifests for review agents that have no reason to write.
- **Network.** `network: deny` → `--disallowedTools WebFetch,WebSearch`. Local-llm backend still speaks HTTP to the configured endpoint regardless; route that through a locked-down bridge if the endpoint is not trusted.
- **Recursion.** Depth, cost, and agent-whitelist guardrails compose. A misbehaving child cannot raise its own caps.
- **Hook isolation.** `--bare` and/or a frozen settings profile disables child-side hooks, preventing the child's execution from firing parent's auto-memory, status-line, etc.
- **Trust boundary with local-llm.** Local models may be pre-prompted by an attacker who controls the server. Treat local-llm output as untrusted: schema-validate and bound size; never exec output.

## 12. Verification

This plan is complete when all of the following are true:

1. `plugins/plan-executor/scripts/plan_nested_dispatch.py run --input …` dispatches a `plan-implementer` agent and returns a schema-valid envelope with `status: ok` and a valid inner `result`.
2. Same command with `agent: codex-implementer` dispatches via `plan_codex_dispatch.py` and returns an envelope with identical shape.
3. Same command with `agent: local-llm-reviewer` against a local Ollama endpoint returns an envelope; `result` conforms to `output_instructions.schema_path`.
4. Each of the refusal rows in §9.2 is exercised by an integration test.
5. A subagent with only `Bash` successfully invokes the script and receives an envelope back — the escape-hatch pattern is end-to-end.
6. `spans.jsonl` contains one entry per hop for a 3-hop test, and depth, call_chain, and cumulative cost are correct.
7. README entry documents the security model, env-var protocol, and the fact that inner CLIs share billing with the parent.
8. All existing plan-executor tests pass; new tests cover ≥85 % of `plan_nested_dispatch.py` lines.

## 13. Primary input arguments — assessment

Ordered by how much callers actually vary them:

1. **`agent`** — the identity of the worker. Highest-variance field. Drives backend, tools, schema, model. Must be a bare name (not a file path) for the common case.
2. **`payload`** — task-specific content. Opaque to the wrapper; agent-specific shape. The plan-executor already has a de facto shape here (`task_id`, `task_block`, `plan_path`, `base_sha`, `analyst_annotations`) that should be the default; extra fields live under `payload.extra`.
3. **`output_instructions.schema_path`** — what the parent wants back. Nearly always set. Inline schema is a rare escape hatch.
4. **`overrides.timeout_sec`** — per-task; short tasks want tight timeouts to fail fast.
5. **`overrides.model`** — controlling cost vs. capability per task. Clamped to models the manifest permits.
6. **`guardrails.max_depth`** and **`guardrails.cost_cap_usd`** — operator-supplied, not per-task; usually set at the top of the run tree and inherited via env.
7. **`trace.*`** — bookkeeping; callers at depth ≥ 1 must pass the inherited values through.

Everything else (`effort`, tool overrides, `system_prompt_append`, settings profile) is available but ordinary callers shouldn't touch it. The manifest is where per-agent defaults live; callers should reach for overrides only when a specific task demands it.

## 14. Open questions / future work

- **Streaming output.** v1 is batch JSON. If a plan-implementer wants incremental progress, we'd add `--output-format stream-json` and relay it through. Defer.
- **Worktree isolation.** Agents doing destructive edits could be spawned in a git worktree. Not in v1 — orchestrator already has this at a higher level.
- **Cross-provider routing inside local-llm.** v1 is one endpoint per manifest; a roster (e.g., Qwen for review, DeepSeek-Coder for implement) is a second-order enhancement.
- **Cost estimation up-front.** `Claude -p` doesn't quote cost pre-hoc. We can add a heuristic based on model + input tokens for better budget rejection.
- **Runtime prompt-cache hints.** The `-bare` + stable system prompts should maximize reuse across hops; verify empirically.

---

## Tasks

### TASK-001: Add dispatch frontmatter spec + new agent manifests

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/agents/plan-implementer.md (modify)
  - plugins/plan-executor/agents/plan-remediator.md (modify)
  - plugins/plan-executor/agents/plan-analyst.md (modify)
  - plugins/plan-executor/agents/plan-reviewer.md (create)
  - plugins/plan-executor/agents/codex-implementer.md (create)
  - plugins/plan-executor/agents/codex-reviewer.md (create)
  - plugins/plan-executor/agents/codex-plan-reviewer.md (create)
  - plugins/plan-executor/agents/local-llm-reviewer.md (create)
  - plugins/plan-executor/agents/agents.index.json (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_agent_manifest_loader.py`
- **Acceptance criteria:**
  - Every existing agent has a `dispatch:` frontmatter block per §6.2.
  - Three Codex wrapper manifests exist and point at the right `plan_codex_dispatch.py` subcommand.
  - `plan-reviewer.md` has a Claude-side reviewer system prompt derived from the existing `plan-remediator` review checklist.
  - `local-llm-reviewer.md` is a valid stub with `backend: local-llm` and an `endpoint` placeholder.
  - `agents.index.json` maps each name to its manifest path and passes JSON-schema validation.

**Description:** Establish the registry and frontmatter conventions that everything else in this plan depends on.

**Reversion guidance:** Delete the new manifests; revert dispatch-block additions in the existing three agent files.

---

### TASK-002: Input/output JSON schemas + shared envelope module

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/schemas/nested_dispatch_input.json (create)
  - plugins/plan-executor/scripts/schemas/nested_dispatch_output.json (create)
  - plugins/plan-executor/scripts/schemas/implementer_report.json (create)
  - plugins/plan-executor/scripts/schemas/reviewer_report.json (create)
  - plugins/plan-executor/scripts/_dispatch_envelope.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_dispatch_envelope.py`
- **Acceptance criteria:**
  - Input/output schemas validate the examples in §8.1 and §8.2.
  - `_dispatch_envelope.py` exposes `build_ok()`, `build_denied()`, `build_timeout()`, `build_schema_invalid()`, `build_backend_error()` with the status vocabulary from §8.2.
  - Unknown top-level keys on envelope construction raise (`additionalProperties: false` in the schema).

**Description:** Pure-data layer so backend code can be written against stable shapes.

**Reversion guidance:** Remove the schema dir and envelope module; revert any consumer imports.

---

### TASK-003: Manifest loader + guardrail engine

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/_agent_manifest.py (create)
  - plugins/plan-executor/scripts/_guardrails.py (create)
  - tests/scripts/test_agent_manifest_loader.py (create)
  - tests/scripts/test_guardrails.py (create)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_agent_manifest_loader.py tests/scripts/test_guardrails.py`
- **Acceptance criteria:**
  - `load_agent(name_or_path)` returns a validated manifest dict (frontmatter + body).
  - `clamp_overrides(manifest, overrides)` returns effective values, enforcing `min(override, manifest_cap)` for timeout/cost/effort/tool sets.
  - `evaluate_preflight(manifest, input, env)` returns `(allow: bool, envelope_if_denied | None)` covering every row in §9.2.
  - `scrub_env(manifest, parent_env)` applies `env_allowlist` + `forbidden_env` + injects `PLAN_EXEC_*`.

**Description:** All reasoning about "can I spawn this?" concentrated in one module, testable in isolation without subprocess.

**Reversion guidance:** Delete both modules and their tests; no other callers yet.

---

### TASK-004: Backend adapters — claude-cli, codex-cli, local-llm

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/backends/__init__.py (create)
  - plugins/plan-executor/scripts/backends/_base.py (create)
  - plugins/plan-executor/scripts/backends/claude_cli.py (create)
  - plugins/plan-executor/scripts/backends/codex_cli.py (create)
  - plugins/plan-executor/scripts/backends/local_llm.py (create)
  - plugins/plan-executor/scripts/plan_codex_dispatch.py (modify — add `--envelope` output mode)
  - tests/scripts/test_backend_claude_cli.py (create)
  - tests/scripts/test_backend_codex_cli.py (create)
  - tests/scripts/test_backend_local_llm.py (create)
- **Dependencies:** TASK-002, TASK-003
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_backend_claude_cli.py tests/scripts/test_backend_codex_cli.py tests/scripts/test_backend_local_llm.py`
- **Acceptance criteria:**
  - `Backend.invoke(manifest, payload, effective, trace)` returns an envelope.
  - `claude_cli` builds the argv per §7.1, captures stdout, parses JSON, maps fields into envelope; TimeoutExpired → `status: timeout`; non-JSON stdout → `status: backend_error`.
  - `codex_cli` shells to `plan_codex_dispatch.py <subcommand>` in `--envelope` mode and passes through.
  - `local_llm` OpenAI-compatible reference impl hits `POST /v1/chat/completions`, schema-validates the content, returns envelope; missing endpoint → `status: backend_error`.
  - Tests use a `--backend-binary` test seam (a tiny shim script) rather than real CLIs; real-CLI paths are gated on an env flag.

**Description:** The actual spawn layer, one adapter per backend kind. Adapters are the only code that shells out.

**Reversion guidance:** Remove `backends/` package; revert the `--envelope` mode in `plan_codex_dispatch.py`.

---

### TASK-005: CLI entrypoint `plan_nested_dispatch.py`

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_nested_dispatch.py (create)
  - tests/scripts/test_plan_nested_dispatch_cli.py (create)
- **Dependencies:** TASK-003, TASK-004
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_nested_dispatch_cli.py`
- **Acceptance criteria:**
  - Subcommands `run`, `list-agents`, `show-agent`, `validate-input`, `validate-output` per §10.
  - `run` wires preflight → backend dispatch → schema-validate inner result → envelope emit → log append.
  - Stdin/stdout `-` syntax works for `--input` and `--output`.
  - Exit codes match §10 (`0` ok, `1` non-ok envelope emitted, `2` wrapper failure).
  - `--dry-run` returns a plan-only envelope with `backend: dry-run` and no spawn.
  - A deliberately-malformed input on stdin returns exit `2` and a `status: input_invalid` envelope on stdout.

**Description:** The user-facing binary. Wires everything together; contains no backend or guardrail logic itself.

**Reversion guidance:** Delete the script and its test; nothing else imports it yet.

---

### TASK-006: Run-log integration (`spans.jsonl`)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/scripts/_span_log.py (create)
  - plugins/plan-executor/scripts/plan_nested_dispatch.py (modify)
  - tests/scripts/test_span_log.py (create)
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_span_log.py`
- **Acceptance criteria:**
  - `append_span(log_dir, envelope)` atomically writes one JSON line to `$log_dir/spans.jsonl`.
  - Each span carries `run_id`, `span_id`, `parent_span_id`, `depth`, `call_chain`, `agent`, `backend`, `status`, `cost_usd`, `duration_ms`, timestamps.
  - A 3-hop integration test (wrapper in test mode calling wrapper calling wrapper) produces 3 spans with the expected parent links.

**Description:** Observability. Mirrors the existing `_run_log.jsonl` idiom so operators can read both logs with the same tooling.

**Reversion guidance:** Remove `_span_log.py`; drop the write call in the CLI.

---

### TASK-007: End-to-end escape-hatch test (subagent → wrapper → claude-cli)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - tests/scripts/test_nested_dispatch_e2e.py (create)
  - tests/fixtures/dispatch/minimal_payload.json (create)
- **Dependencies:** TASK-005
- **Test command:** `PLAN_EXEC_E2E=1 venv/bin/python -m pytest tests/scripts/test_nested_dispatch_e2e.py`
- **Acceptance criteria:**
  - Test is skipped by default; runs only when `PLAN_EXEC_E2E=1` AND `claude` on PATH.
  - Exercises the real CLI with `agent: plan-analyst` against a trivial stub plan and asserts `status: ok`.
  - Second case uses `agent` with `max_spawn_depth: 1` and confirms a nested call through the wrapper produces two spans in `spans.jsonl`.

**Description:** Proves the escape hatch is live end-to-end, matching the §3 experiment but through the sanctioned chokepoint.

**Reversion guidance:** Delete the test and fixture.

---

### TASK-008: Skill wiring — optional nested route in `implement-plan`

- **Status:** pending
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (modify)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (modify)
- **Dependencies:** TASK-005, TASK-007
- **Test command:** none
- **Acceptance criteria:**
  - Skill documents when to reach for nested dispatch (subagent needs to delegate; orchestrator wants a uniform envelope across Claude/Codex/local).
  - A dispatch-template example shows the minimum input JSON for `plan-implementer` and for `codex-implementer` via the wrapper.
  - No change to default orchestration — nested dispatch is an *available* route, not the *mandatory* one for v1.

**Description:** Makes the new mechanism discoverable without breaking the existing flow.

**Reversion guidance:** Revert the two skill files.

---

### TASK-009: README + security doc

- **Status:** pending
- **Priority:** medium
- **Files:**
  - README.md (modify)
  - plugins/plan-executor/scripts/README_nested_dispatch.md (create)
- **Dependencies:** TASK-005
- **Test command:** none
- **Acceptance criteria:**
  - README gains a "Nested subagent dispatch" section pointing at the new wrapper and this plan.
  - `README_nested_dispatch.md` documents every `PLAN_EXEC_*` env var, the refusal matrix (§9.2), and the explicit statement that inner CLIs share billing with the parent.
  - Quickstart example shows the three-line JSON for a typical `plan-implementer` call.

**Description:** Operator-facing documentation. Without it, the safety envelope may be misused.

**Reversion guidance:** Revert both files.

---

## Appendix A — Example invocations

**A.1 Claude-side implementer from a subagent (Bash-only context):**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_nested_dispatch.py run \
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

**A.2 Unified Codex call (same envelope as Claude):**

```bash
… --input - <<'EOF'
{ "schema_version": 1, "agent": "codex-implementer",
  "payload": { "task_id": "010", "plan_path": "/abs/path/plan.md",
               "task_block": "…", "base_sha": "abc123" },
  "output_instructions": {
    "schema_path": "plugins/plan-executor/scripts/codex_implement_schema.json"
  } }
EOF
```

**A.3 Local-LLM reviewer:**

```bash
PLAN_EXEC_LOCAL_LLM_URL="http://localhost:11434/v1" \
… --input - <<'EOF'
{ "schema_version": 1, "agent": "local-llm-reviewer",
  "payload": { "diff": "…", "task_block": "…" },
  "output_instructions": {
    "schema_path": "plugins/plan-executor/scripts/schemas/reviewer_report.json"
  },
  "overrides": { "timeout_sec": 120 } }
EOF
```

## Appendix B — Effective-value resolution (pseudocode)

```python
def resolve_effective(manifest, overrides, env):
    eff = {}
    eff["model"]       = overrides.get("model") or manifest["model"]
    eff["effort"]      = overrides.get("effort") or manifest["dispatch"].get("default_effort")
    eff["timeout_sec"] = min(
        overrides.get("timeout_sec", sys.maxsize),
        manifest["dispatch"]["default_timeout_sec"],
    )
    eff["tools_allowed"]    = intersect(
        overrides.get("tools_allowed", manifest["dispatch"]["tools_override"]["allowed"]),
        manifest["dispatch"]["tools_override"]["allowed"],
    )
    eff["tools_disallowed"] = union(
        overrides.get("tools_disallowed", []),
        manifest["dispatch"]["tools_override"]["disallowed"],
    )
    eff["cost_cap_usd"] = min(
        overrides.get("cost_cap_usd", float("inf")),
        manifest["dispatch"]["cost_cap_usd"],
    )
    eff["cwd"] = overrides.get("cwd") or manifest["dispatch"]["cwd_scope"][0]
    return eff
```

## Appendix C — Agent-identity cheat sheet

| Agent | Backend | When a caller picks it |
|-------|---------|------------------------|
| `plan-analyst` | claude-cli | read-only plan validation / scheduling |
| `plan-implementer` | claude-cli | multi-file or design-sensitive implementation |
| `plan-remediator` | claude-cli | narrow patch on a load-bearing finding |
| `plan-reviewer` | claude-cli | Claude-side cross-review of Codex-implemented diffs |
| `codex-implementer` | codex-cli | ≤3 files, ≤30 LOC, bounded spec |
| `codex-reviewer` | codex-cli | independent review of Claude-implemented diffs |
| `codex-plan-reviewer` | codex-cli | pre-flight plan audit |
| `local-llm-reviewer` | local-llm | bulk review on cheap hardware |
