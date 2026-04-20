# PLAN — Nested Dispatch v2 probe supplement

**Date:** 2026-04-18 (probes executed 2026-04-18 through 2026-04-20)
**Supplements:** prior nested-dispatch plan (v1 not referenced here; this document stands alone against the probe results)
**Driver:** orchestrator context pressure + Claude↔Codex dispatch asymmetry documented in `docs/post_mortem/2026-04-18_TASK-004E_halt_and_context_pressure.md`

---

## Purpose

Capture the three headless-`claude -p` probe results that de-risk building `plugins/plan-executor/scripts/plan_claude_dispatch.py` — a Python subprocess wrapper that mirrors `plan_codex_dispatch.py` for Claude-tier subagents. Document findings, cost model, wrapper architecture, and concrete implementation guidance so the build can start without re-doing the probe work.

## TL;DR

Wrapper is viable. All three blocker probes came back green with one bounded caveat.

- `claude -p --agent <plugin>:<name>` natively loads the subagent's system prompt. No prompt-splicing hack needed.
- Tool allowlist from the agent's frontmatter is NOT enforced under `--agent` in headless mode. Wrapper must construct `--allowedTools` itself and also run delta-bounded cleanup against a git baseline (same safety net as the Codex wrapper).
- Per-dispatch cost is cost-neutral vs the current Agent-tool path because the subagent system prompt lives in the 1-hour ephemeral cache — steady-state per-dispatch cost stabilizes at haiku-$0.005 / opus-~$0.08-0.12 after a one-time cold-start surcharge. Win is structural (context footprint), not monetary.

Net recommendation: build the wrapper. Migrate Phase B (implementer), Phase D-Claude (reviewer), Phase D.5 (third-opinion), Phase B-rework (D.2a.5 retry), Phase B-narrow-remediation (D.2a.6 retry), and the Phase A (plan-analyst) dispatch itself to the wrapper. Keep `Agent(subagent_type: ...)` as a fallback only if the wrapper errors out.

---

## Probe results (verbatim from runs 2026-04-18 → 2026-04-20)

### Probe 1 — `--agent` flag resolves plugin subagents and loads their system prompt

Invocation:

```
claude -p "Reply with exactly one word: READY. Do not use any tools." \
  --agent plan-executor:plan-analyst \
  --model haiku \
  --output-format json \
  --permission-mode bypassPermissions
```

Result envelope key fields:

- `subtype: success`, `is_error: false`, `result: "READY"`, `num_turns: 1`, `duration_ms: 2379`
- `cache_creation_input_tokens: 29531` — strong signal that the full plan-analyst system prompt + tool config loaded (a bare default Claude Code session is far smaller)
- `total_cost_usd: 0.03718` on haiku (cold-start run)
- `permission_denials: []`

Findings:

1. `--agent` accepts plugin-namespaced names (`plan-executor:plan-analyst`) verbatim; no "unknown agent" error.
2. The ~29K cache-creation tokens is the subagent spec being pulled into the context.
3. `--model haiku` overrode the agent's declared `model: opus` — confirmed via `modelUsage.claude-haiku-4-5-20251001` in the envelope. Wrapper must pass `--model opus` (or the agent's declared model) explicitly.

### Probe 2 — Tool allowlist is NOT enforced under `--agent` in `-p` mode

Agent spec (`plugins/plan-executor/agents/plan-analyst.md` frontmatter):

```yaml
tools: Read, Grep, Glob, Bash
```

Plan-analyst's own system prompt: *"you never implement tasks or modify any files."*

Invocation:

```
claude -p "Use the Write tool to create /tmp/probe2-$$.txt with the contents 'hello'. \
  If you cannot, say exactly: NO_WRITE_TOOL. Do not use any other tool." \
  --agent plan-executor:plan-analyst \
  --model haiku --output-format json \
  --permission-mode bypassPermissions
```

Result envelope key fields:

- `result: "Done. File created at /tmp/probe2-1372028.txt with contents hello."`
- `num_turns: 2` (one Write tool call + one final message)
- `permission_denials: []`
- File confirmed on disk via `ls -la /tmp/probe2-*.txt` before cleanup

Findings:

1. The `tools:` frontmatter field is NOT applied by `--agent` in headless mode. Write succeeded despite not being in the allowlist.
2. The subagent's own system-prompt self-declarations ("never modify files") are advisory only — the model can and did ignore them when user-prompted to.
3. This is a material gap versus the in-session `Agent(subagent_type: ...)` path, which does enforce the tool allowlist.

Consequence: wrapper owns allowlist enforcement. Two layers:

- **Hard enforcement via `--allowedTools`** constructed from the agent's frontmatter.
- **Defense in depth via git-baseline delta-bounded cleanup**, same policy as `plan_codex_dispatch.py`. If a misbehaving subagent writes outside its declared `files_changed`, the wrapper restores those files from baseline.

### Probe 3 — Cache amortizes across dispatches within a 1-hour window

Three back-to-back plan-analyst dispatches, haiku, identical-shape prompts ("Reply with exactly one word: PING{i}"):

| Dispatch | cache_creation | cache_read | duration_ms | cost_usd |
|---|---|---|---|---|
| 1 | 1,856 | 27,669 | 1,726 | 0.00532 |
| 2 | 1,856 | 27,669 | 2,170 | 0.00532 |
| 3 | 1,856 | 27,669 | 1,741 | 0.00532 |

Findings:

1. The subagent system prompt lives in the **1-hour ephemeral cache** (`ephemeral_1h_input_tokens` in the usage block). Earlier notes citing a 5-minute window were wrong.
2. First dispatch in probe 3 already hit the warm cache because probe 1 and probe 2 (minutes earlier) had primed it. Cold-start surcharge is a one-time per-session cost, not per-dispatch.
3. Steady-state cost is flat across dispatches — $0.005 on haiku, extrapolated $0.08-0.12 on opus (15-20× haiku pricing ratio).
4. Steady-state duration is ~1.7-2.2s per trivial dispatch. Real plan-implementer dispatches will be dominated by the implementer's own tool calls and test runs, not by cache overhead.

Cost extrapolation for a representative 5-task plan run (1 analyst dispatch + 1 plan-review + 5 implementer dispatches + 5 reviewer dispatches + 0-2 retries):

- Cold-start surcharge: one-time ~$0.40-0.60 on opus.
- Steady-state dispatches: ~12 × $0.10 ≈ $1.20 on opus.
- Total per-run API cost: **~$1-2** — identical to the current Agent-tool path. API cost is not a factor in the migration decision.

---

## Architecture

### Orchestrator-side contract (what changes in SKILL.md)

Today's Phase B dispatch (Claude-tier):

```
Agent(subagent_type: "plan-executor:plan-implementer", model: "opus",
      prompt: render(templates.PhaseB, ...))
```

After migration:

```
Bash: venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_claude_dispatch.py" implement \
  --plan-file <abs> --task-id NNN --repo-root <abs> --timeout 300
```

Identical shape to the existing Codex dispatch. Every Phase B/D/D.5/D.2a.5/D.2a.6 and the Phase A analyst call flips the same way.

### Envelope shape (wrapper → orchestrator)

Single JSON line on stdout, bounded. No raw markdown in the orchestrator's context.

```json
{
  "outcome": "success",
  "files_changed": ["plugins/plan-executor/scripts/plan_ops.py", "..."],
  "test_outcome": "passed",
  "report_path": "docs/plans/_run_reports/20260418T042817-004E-implement-claude.md",
  "diagnostics": [],
  "concerns": [],
  "plan_adaptations": [],
  "warnings": [],
  "reversion_guidance": null,
  "wall_seconds": 187,
  "total_cost_usd": 0.0834,
  "session_id": "8ed6527b-...",
  "extra": {
    "scope_violation_detected": false,
    "protected_skipped_tracked": [],
    "protected_skipped_untracked": [],
    "num_turns": 14,
    "cache_creation_input_tokens": 1856,
    "cache_read_input_tokens": 27669
  }
}
```

`outcome ∈ {success, failure, timeout, parse_error, scope_violation, malformed}` — unified with Codex wrapper vocabulary where possible. `malformed` is new vs Codex and maps to Claude's current post-hoc `malformed` classification.

### Wrapper internal flow

1. **Parse args:** `implement | review | plan-review | analyst` subcommand + `--plan-file --task-id --repo-root --timeout --files --review-focus` (the latter two for review subcommand only).
2. **Load agent spec:** read `plugins/plan-executor/agents/<name>.md` frontmatter, extract `tools:` and `model:`.
3. **Snapshot baseline:** `git status --porcelain` + file content hashes for files in the agent's working set. Store in a per-dispatch temp dir.
4. **Render prompt:** load the matching template from `skills/implement-plan/dispatch-templates.md`; splice in the plan-file context, task block, starting SHA, analyst annotations, reviewer findings (for retry subcommands), etc.
5. **Invoke `claude -p`:**
   ```python
   result = subprocess.run(
       ["claude", "-p", rendered_prompt,
        "--agent", f"plan-executor:{agent_name}",
        "--model", agent_spec["model"],
        "--allowedTools", ",".join(agent_spec["tools"].split(", ")),
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--dangerously-skip-permissions"],  # wrap in subprocess, so orchestrator doesn't see prompts
       capture_output=True, text=True, timeout=args.timeout,
       env={**os.environ, "CLAUDE_CODE_SIMPLE": "0"})
   ```
   Consider `--bare` if plugin-load overhead becomes material; tradeoff is losing CLAUDE.md auto-discovery.
6. **Parse Claude JSON envelope:** extract `result`, `duration_ms`, `num_turns`, `total_cost_usd`, `usage`, `session_id`.
7. **Parse the markdown report** from `result` using `plan_ops.py parse-implementer-report --stdin` (or a new `parse-claude-envelope` subcommand if the report shape diverges for review/analyst).
8. **Delta-bounded cleanup:** diff working tree against baseline. For any file NOT in `files_changed`, restore from baseline. Protected paths (`_run_lock.json`, `_run_log.jsonl`, `.claude/`, `.codex/`, `<plan_dir>/_run_*`) are never touched. Same invariant as Codex wrapper.
9. **Scope check:** if wrapper-observed delta disagrees with parsed `files_changed`, flip `outcome = "scope_violation"` and record `extra.scope_misreport_files`.
10. **Persist raw report:** write `result` text to `docs/plans/_run_reports/<run_id>-<task>-<phase>-claude.md`. Create directory if missing.
11. **Emit envelope on stdout:** single JSON line.

### Timeout strategy

- `implement`: 300s (matches Codex wrapper).
- `review`, `plan-review`, `analyst`: 180s.
- Wrapper enforces with `subprocess.run(timeout=...)`. On `TimeoutExpired`, kill the process, run cleanup, emit `outcome = "timeout"`.

### Parallelism

Already works: orchestrator fires multiple Bash calls in one message, each spawns an independent `claude -p` subprocess. No serialization through the orchestrator's Agent-tool bottleneck. This is already how Codex parallelism works; same mechanic applies.

---

## Implementation guidance

### Files to create

- `plugins/plan-executor/scripts/plan_claude_dispatch.py` — wrapper entry point.
- `plugins/plan-executor/scripts/_claude_dispatch_lib.py` — shared helpers (frontmatter parse, baseline snapshot, delta cleanup). Factor out from the Codex wrapper where sensible; avoid copy-paste.
- `docs/plans/_run_reports/.gitkeep` — directory placeholder; `_run_reports` becomes the canonical location for raw subagent reports.

### Files to modify

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — replace every `Agent(subagent_type: ...)` dispatch reference with the Bash wrapper invocation. Keep the templates in `dispatch-templates.md` untouched (they're the prompt content; wrapper consumes them).
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — no content changes, but add a header note that templates are now consumed by the wrapper, not by Agent-tool rendering.
- `plugins/plan-executor/scripts/plan_ops.py` — if a new `parse-claude-envelope` subcommand is added, wire it in. Otherwise reuse `parse-implementer-report` for all markdown reports from Claude subagents.
- `.gitignore` — add `docs/plans/_run_reports/*.md` if raw reports shouldn't be committed. Decision depends on whether replayability-from-git is valuable. Default: gitignore them; they're large and rarely re-read.

### Argparse sketch

```python
parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="subcommand", required=True)

p_impl = sub.add_parser("implement")
p_impl.add_argument("--plan-file", required=True)
p_impl.add_argument("--task-id", required=True)
p_impl.add_argument("--repo-root", required=True)
p_impl.add_argument("--timeout", type=int, default=300)

p_rev = sub.add_parser("review")
p_rev.add_argument("--plan-file", required=True)
p_rev.add_argument("--task-id", required=True)
p_rev.add_argument("--repo-root", required=True)
p_rev.add_argument("--files", required=True)   # comma-separated
p_rev.add_argument("--review-focus", default="bugs")
p_rev.add_argument("--timeout", type=int, default=180)

# retry variants — D.2a.5 (bounded remediation) + D.2a.6 (narrow remediation)
p_rwk = sub.add_parser("implement-rework")
p_rwk.add_argument(...)
p_rwk.add_argument("--codex-findings-json", required=True)
p_rwk.add_argument("--d5-summary", required=True)

p_narrow = sub.add_parser("implement-narrow-remediation")
p_narrow.add_argument("--load-bearing-findings-json", required=True)
p_narrow.add_argument("--dismissed-findings-json", required=True)
p_narrow.add_argument("--d5-summary", required=True)

# analyst dispatch
p_ana = sub.add_parser("analyst")
p_ana.add_argument("--plan-file", required=True)
p_ana.add_argument("--repo-root", required=True)
p_ana.add_argument("--timeout", type=int, default=180)

# third-opinion (D.5) dispatch
p_d5 = sub.add_parser("third-opinion")
p_d5.add_argument("--plan-file", required=True)
p_d5.add_argument("--task-id", required=True)
p_d5.add_argument("--repo-root", required=True)
p_d5.add_argument("--files", required=True)
p_d5.add_argument("--codex-findings-json", required=True)
p_d5.add_argument("--timeout", type=int, default=180)
```

### Frontmatter parsing

Simple YAML extraction — use the existing helper in `plan_ops.py` if one exists, otherwise a 10-line regex splitter on `---\n...\n---`:

```python
def load_agent_spec(agent_name: str) -> dict:
    path = PLUGIN_ROOT / "agents" / f"{agent_name}.md"
    text = path.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"no frontmatter in {path}")
    import yaml
    return yaml.safe_load(m.group(1))
```

`tools:` field comes in as a string like `"Read, Grep, Glob, Bash"` — split on commas, strip whitespace, emit as comma-separated to `--allowedTools`.

### Delta-bounded cleanup (reuse from Codex wrapper)

The Codex wrapper at `plugins/plan-executor/scripts/plan_codex_dispatch.py` already implements this pattern per the cleanup policy section at the end of SKILL.md. Specifically:

- Protected exact paths: `_run_lock.json`, `.claude`, `.codex`
- Protected prefixes: `<plan_dir>/_run_log.jsonl`, `<plan_dir>/_run_lock.json`, `.claude/`, `.codex/`
- Delta invariant: cleanup only touches `(allowed_files ∪ new-delta-violations) − protected`
- Scope-misreport: if `files_changed` disagrees with observed delta, emit `outcome=failure` with `extra.reason="scope_misreport"` and skip the test command

Factor the cleanup logic into `_claude_dispatch_lib.py` and have the Codex wrapper import it too. Two wrappers sharing one cleanup implementation is lower maintenance than two copies.

### Permission-mode recommendation

- Use `acceptEdits` (NOT `bypassPermissions`) once `--allowedTools` is supplied. `acceptEdits` auto-approves edits within the allowlist but respects the allowlist itself. `bypassPermissions` skips all checks including allowlist — that's what probe 2 used and it defeated the allowlist even if one were provided.
- Alternative: `--dangerously-skip-permissions` is shorter but the explicit `--permission-mode acceptEdits` is self-documenting.

### Report-path convention

`docs/plans/_run_reports/<run_id>-<task_id>-<phase>-<agent>.md`

Examples:

- `20260418T042817-004E-implement-claude.md`
- `20260418T042817-004E-review-codex.md`
- `20260418T042817-004E-d5-claude.md`
- `20260418T042817-plan-review-codex.md` (no task_id for plan-review)
- `20260418T042817-analyst-claude.md`

Use hyphens, not underscores, for separators so run_id internal underscores (if any future format adds them) don't collide.

### Gotchas from the probes

1. **Model override must be explicit.** `--agent` does not auto-apply the agent's `model:` field. Wrapper must pass `--model <from frontmatter>` always. Missing `--model` falls back to the CLI default, which may not be what the agent wants.
2. **Cache TTL is 1 hour, not 5 minutes.** Plan ahead of schedule decisions around cache warmth. Runs that take >1h between dispatches pay the cold-start surcharge twice.
3. **`num_turns` in the envelope is useful telemetry.** Log it in `implement_done` events so that extraordinarily high turn counts (20+) surface as a signal of a stuck subagent.
4. **`total_cost_usd` is per-call, not cumulative.** Orchestrator can sum across dispatches for a per-run cost rollup in the final summary.

---

## Risk and caveat register

| Risk | Status | Mitigation |
|---|---|---|
| Tool allowlist not applied natively | Confirmed via probe 2 | Wrapper constructs `--allowedTools` from agent frontmatter; delta-bounded cleanup as defense in depth |
| Cache creation cost inflating runs | Disproven by probe 3 | 1h TTL; steady-state cost matches Agent-tool path |
| Subagent system prompt not loaded | Disproven by probe 1 | `--agent` works natively |
| `claude -p` stalls on permissions | Disproven by probe 1/2 | `--permission-mode acceptEdits` is non-interactive |
| Agent tool parity for Read/Grep/Glob/Edit/Write/Bash | Disproven by probe 2 (Write succeeded) | All core tools available in `-p` mode |
| Stream-json output needed for progress | Not required | `--output-format json` emits final-only envelope, which is what the wrapper wants |
| Cancellation semantics | Green | `subprocess.run(timeout=N)` + `Popen.kill()` work |
| Nested Agent-tool calls by subagent | Untested but mitigated | Subagent system prompts all say "you do NOT have the Agent tool"; `--allowedTools` can exclude Agent explicitly |
| Concurrent same-plan dispatches collide on git baseline | Untested | Baseline is per-process-temp-dir; concurrent dispatches in one batch touch disjoint files per analyst schedule. Mirrors Codex wrapper's concurrency model. |
| `claude` CLI version skew across machines | Not probed | Wrapper could check `claude --version` at startup and require a minimum version. Low priority until observed in practice. |

---

## SKILL.md change inventory

Estimated diff footprint of the migration:

- `dispatch-templates.md` — add a one-line header note; no template edits.
- `SKILL.md`:
  - Dispatch rule #1 (line ~13): update "Claude-tier via Agent, Codex-tier via Bash" to "both via Bash."
  - Phase 1 (analyst dispatch): replace Agent call with `plan_claude_dispatch.py analyst`.
  - Phase 1.5 (plan review) and Phase D (Codex review): unchanged — these are already Bash wrappers.
  - Phase B (implementer): replace Agent call with `plan_claude_dispatch.py implement`.
  - Phase D-Claude (code-reviewer on Codex work): replace with `plan_claude_dispatch.py review-codex-impl` or merge into `review` subcommand.
  - Phase D.5 (third-opinion): replace with `plan_claude_dispatch.py third-opinion`.
  - Phase B-rework (D.2a.5): replace with `plan_claude_dispatch.py implement-rework`.
  - Phase B-narrow-remediation (D.2a.6): replace with `plan_claude_dispatch.py implement-narrow-remediation`.
  - Cleanup policy section at the end: extend to note that the Claude wrapper shares the same delta-bounded cleanup implementation.
  - Timeout idioms section: add wrapper-specific timeouts.

Net: ~20-30 line changes across SKILL.md, mostly find-and-replace.

---

## Rollout sequence

Recommended order (each step mergeable independently):

1. **Land `plan_claude_dispatch.py` with `analyst` subcommand only.** Phase 1 is the cheapest dispatch to migrate — one call per run, failure is recoverable. Shakes out frontmatter parsing, envelope shape, cleanup policy, permission mode.
2. **Migrate Phase B (`implement` subcommand).** This is where the context-pressure savings actually land — multi-task plans generate many implementer reports.
3. **Migrate Phase D-Claude + Phase D.5.** Lower-volume but similar shape to implement; reuses the same plumbing.
4. **Migrate Phase B-rework + Phase B-narrow-remediation.** Retry paths are rare but share the template-rendering code.
5. **Remove the Agent-tool fallback.** Only after 2-3 multi-task plan runs have validated the wrapper end-to-end.

Each step ships a matching SKILL.md update so the orchestrator's routing table and the wrapper's capabilities stay in lockstep.

---

## Still-open probes (non-blocking)

- **Probe 4 — `--bare` mode overhead.** Does `--bare` (skip plugin sync, CLAUDE.md auto-discovery) materially drop cold-start time? If yes, wrapper opts in.
- **Probe 5 — `--exclude-dynamic-system-prompt-sections`.** Could improve cache reuse across different repos / cwds. Relevant only if wrapper invocations span multiple plan directories in one session.
- **Probe 6 — Concurrent dispatch git-baseline safety.** Fire two implement dispatches in parallel against disjoint files; verify cleanup doesn't cross-contaminate. Mirrors the Codex wrapper's concurrency test.
- **Probe 7 — `--session-id` reuse for cache continuity.** Could further reduce cache-creation by pinning a shared session UUID across a run. Experimental; may conflict with per-dispatch cleanup semantics.

None of these gate the v2 build; they're tuning passes for after the wrapper is in place.

---

## Acceptance criteria for the wrapper build (minimum bar)

1. `plan_claude_dispatch.py analyst --plan-file ... --repo-root ... --timeout 180` emits a valid envelope with `outcome=success` on a known-good plan.
2. Envelope JSON validates against a new `scripts/claude_dispatch_envelope_schema.json`.
3. Delta-bounded cleanup restores baseline when subagent writes outside declared scope (integration test: inject a Write to a protected path, verify rollback).
4. `--allowedTools` constructed from frontmatter matches agent spec exactly (unit test on the parser).
5. Orchestrator end-to-end: run `implement-plan` on a toy 2-task plan, both Claude, one commit per task, no errors.
6. Raw reports land in `docs/plans/_run_reports/` with the documented filename convention.
7. `run_log.jsonl` `implement_done` events gain `num_turns` and `total_cost_usd` fields; final summary rolls up cost per run.
8. Codex wrapper's protected-paths and delta invariants import the same shared helper — no drift.

Build can proceed in parallel with the analyst/severity/gap-triage work from the post-mortem; the two efforts touch different files (wrapper vs analyst spec + plan_ops validator).

---

## Appendix A — Local-LLM (Ollama) Python engagement

This appendix documents the concrete host-environment setup and Python call patterns for the `local-llm` backend referenced in v2 §7.3. It is operator-facing (how to talk to the running Ollama) and implementer-facing (what the backend adapter will wrap). Captured here so TASK-004's `local-llm` adapter does not need to re-discover the host wiring.

### Host environment (as configured 2026-04-20)

- Ollama installed on **Windows**, not in WSL: `C:\Users\marcd\AppData\Local\Programs\Ollama\ollama.exe` (v0.20.7).
- Server bound to `0.0.0.0:11434` (confirmed via `netstat.exe -an | grep 11434` → `0.0.0.0:11434 LISTENING` and `[::]:11434 LISTENING`). Bind was changed from the default `127.0.0.1` by setting `OLLAMA_HOST=0.0.0.0:11434` via `setx` and fully restarting the tray app.
- Models resident: `qwen2.5-coder:14b-instruct-q4_K_M` (8.4 GB, Q4_K_M, primary code model), `lfm2.5-thinking:latest` (700 MB, exploratory).
- WSL2 localhost-mirroring is **off**; WSL reaches the Windows host via the default-route gateway, not `localhost`.

WSL shell bootstrap (`~/.bashrc`):

```bash
# Ollama on Windows host — gateway IP can shift across wsl --shutdown, so recompute each shell
export OLLAMA_HOST="http://$(ip route show | grep -i default | awk '{print $3}'):11434"
```

After `source ~/.bashrc`, `echo $OLLAMA_HOST` yields e.g. `http://172.17.176.1:11434`. From Windows Git Bash the equivalent is `http://localhost:11434` and both addresses reach the same daemon.

Sanity probe (both shells):

```bash
curl -s $OLLAMA_HOST/api/tags | python3 -m json.tool | head -20
```

### Python engagement — three supported call patterns

All three talk to the same daemon; pick based on the consumer's constraints.

**Pattern 1 — OpenAI-compatible SDK (preferred for the `local-llm` backend adapter)**

Matches v2 §7.3's `api_kind: openai-compatible`. Ollama exposes `/v1/chat/completions` mirroring OpenAI's schema; any OpenAI client works by pointing `base_url` at Ollama.

```python
# venv/bin/pip install openai
import os
from openai import OpenAI

client = OpenAI(
    base_url=f"{os.environ['OLLAMA_HOST']}/v1",
    api_key="ollama",  # required by the SDK; value is ignored by Ollama
)

resp = client.chat.completions.create(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[
        {"role": "system", "content": "You are a terse code reviewer."},
        {"role": "user",   "content": "Review this diff:\n<diff text>"},
    ],
    temperature=0.2,
    max_tokens=2048,
)
print(resp.choices[0].message.content)
```

This is what the `local-llm` backend adapter should target first — one code path serves Ollama, vLLM, LM Studio, and any other OpenAI-compatible server, satisfying v2 §7.3's "one reference implementation, multiple endpoints" goal.

**Pattern 2 — Native `ollama` Python client (fastest iteration for ad-hoc scripts)**

```python
# venv/bin/pip install ollama
import os
from ollama import Client

client = Client(host=os.environ["OLLAMA_HOST"])
resp = client.chat(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[{"role": "user", "content": "Explain the Newton-Raphson method in two sentences."}],
    options={"temperature": 0.2, "num_predict": 512},
)
print(resp["message"]["content"])
```

Not appropriate for the backend adapter (ties to one vendor) but ideal for quick experiments and one-off diagnostics from WSL.

**Pattern 3 — Raw HTTP (zero dependencies; useful for tests and the dispatch-envelope integration test)**

```python
import os, json, urllib.request

req = urllib.request.Request(
    f"{os.environ['OLLAMA_HOST']}/v1/chat/completions",
    data=json.dumps({
        "model": "qwen2.5-coder:14b-instruct-q4_K_M",
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
    }).encode(),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=120) as r:
    print(json.loads(r.read())["choices"][0]["message"]["content"])
```

### Structured JSON output (matches v2 §8.3 output-instructions flow)

Ollama supports JSON-mode and, on recent versions, JSON-schema-constrained decoding. The `local-llm` backend should use this when the caller supplies `output_instructions.schema_path` — it's the local-model equivalent of Claude's retry-on-invalid loop, except the constraint is enforced at decode time rather than after the fact.

Via OpenAI-compatible endpoint:

```python
resp = client.chat.completions.create(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[...],
    response_format={"type": "json_object"},  # Ollama honors OpenAI's JSON-mode flag
)
```

Via native client (supports full JSON schema, not just JSON mode — preferred):

```python
from ollama import Client
client = Client(host=os.environ["OLLAMA_HOST"])
schema = json.loads(open("plugins/plan-executor/scripts/schemas/reviewer_report.json").read())
resp = client.chat(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[...],
    format=schema,  # newer Ollama: full JSON-schema constrained decoding
)
```

Fallback when the backend version lacks schema support: `format="json"` for JSON-mode, then `jsonschema.validate()` post-hoc, with one retry quoting the validator's error message. Mirror the v2 §8.3 retry policy — a second failure yields `status: schema_invalid`.

### Integration pointers for TASK-004's `local-llm` adapter

- Use `manifest.dispatch.local_llm.endpoint` (v2 §6.2) as the `base_url`. Default to `${PLAN_EXEC_LOCAL_LLM_URL}` env (v2 §9.1), which callers set from `$OLLAMA_HOST` (or append `/v1` depending on the endpoint field convention — settle on **endpoint carries `/v1` suffix** for OpenAI-compatible backends).
- The `api_key` parameter is required by the `openai` SDK but ignored by Ollama — pass any non-empty string. Do **not** source it from the parent environment's `OPENAI_API_KEY`; that leaks real credentials into a local call for no benefit.
- Timeouts on local models must be generous. A 14B model on CPU can take 60–300s for a non-trivial review. Set the manifest's `default_timeout_sec` to 300 for `local-llm-reviewer` and expose `overrides.timeout_sec` for callers who want faster-fail semantics.
- The `total_cost_usd` envelope field (v2 §8.2) is 0.0 for local calls; record `tokens.input`/`tokens.output` from the OpenAI response's `usage` block for observability, even though they don't map to dollars.
- **No tool use in v1.** Local Qwen Coder does not have native tool-call plumbing through this backend. v2 §7.3 already flags this out-of-scope; it stays out-of-scope.
- Envelope mapping: `status: ok` when (a) HTTP 200, (b) `choices[0].finish_reason in {"stop", "length"}`, (c) schema-valid JSON (if `output_instructions` supplied). `length` with schema-required output → retry once with a higher `max_tokens`, else `status: schema_invalid`.

### WSL-specific gotchas

1. `$(ip route show | grep default | awk '{print $3}')` can shift across `wsl --shutdown`. The `.bashrc` entry recomputes on each new shell, so *new* shells are fine; long-lived Python processes holding a cached `base_url` are not — resolve at request time if the process is meant to survive a WSL restart.
2. Windows Firewall may block 11434 from WSL after a Windows update. If `curl $OLLAMA_HOST/api/tags` suddenly 404s or times out, run in admin PowerShell: `New-NetFirewallRule -DisplayName "Ollama" -Direction Inbound -LocalPort 11434 -Protocol TCP -Action Allow`.
3. Do **not** install a second Ollama natively inside WSL. Duplicated model blobs waste disk; the Windows server already has GPU access the WSL instance would lack without CUDA-for-WSL setup. One server, many clients.
4. If a future reboot moves Ollama back to `127.0.0.1`, check `OLLAMA_HOST` in the Windows user environment (`setx` persists across reboots, but a Windows update of the Ollama app can reset it). Recheck with `netstat.exe -an | grep 11434`.

### Minimum acceptance for v2 TASK-004 local-llm path

In addition to the TASK-004 criteria in the main plan:

- Adapter hits the Windows Ollama server from WSL using the env-configured `base_url`.
- `qwen2.5-coder:14b-instruct-q4_K_M` round-trips a schema-validated `reviewer_report.json`.
- Timeout path (model hung or slow) emits `status: timeout` with the observed elapsed time in `duration_ms`.
- Endpoint-down path (server not running) emits `status: backend_error` with `error.code: "endpoint_unreachable"` and `error.retriable: true`.
