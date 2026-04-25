# `plan_claude_dispatch.py` — nested Claude CLI dispatch wrapper

Operator-and-caller orientation for the wrapper that lets a subagent shell out
to a fresh top-level `claude` session. Source modules in this directory are
authoritative; this README is a pointer + safety summary.

## Why it exists (the escape hatch)

Claude Code's harness grants the `Agent` tool only to the top-level session.
Subagents spawned via `Agent` inherit a reduced toolset and **cannot** spawn
further agents — calling `Agent` from a subagent crashes the session. That
blocks natural patterns in the plan-executor: a `plan-implementer` cannot
delegate a narrow research query, a `plan-remediator` cannot run a Codex
cross-check, and Claude-tier tasks cannot parallelize without bouncing back to
the orchestrator.

Validated escape (experiment 2026-04-18): a subagent with only `Bash` can
shell out to the `claude` CLI:

```
claude -p --output-format json --permission-mode acceptEdits --agent <name> ...
```

The nested `claude` is a fresh top-level session with full tool access,
including `Agent`. End-to-end chain
`subagent -> python -> claude -p -> Agent -> sub-sub-agent` returned the
sentinel `DEEP_NESTED_OK_a3f9c2` in ~6.3s at ~$0.093.

The escape hatch is real but raw. Unwrapped use would (a) duplicate prompt and
schema plumbing across callers, (b) leak auth / billing / recursion controls,
and (c) have no consistent structured input/output. This wrapper is the same
sort of normalization that `plan_codex_dispatch.py` provides for Codex.

## Quickstart

Semantic minimum for `plan-implementer` (the three load-bearing keys, ≤10
lines):

```json
{
  "schema_version": 1,
  "agent": "plan-implementer",
  "payload": {"prompt": "Summarize the README in one sentence."}
}
```

Dispatch via stdin:

```bash
cat payload.json | python plan_claude_dispatch.py run --input -
```

The wrapper-level schema (`schemas/claude_dispatch_input.json`,
`additionalProperties: false`) also requires the boilerplate blocks
`output_instructions`, `overrides`, `guardrails`, and `trace` to be present.
The fields inside each may be `null`; e.g. `"overrides": {"model": null,
"timeout_sec": null, "tools_allowed_extra": null, "tools_disallowed_extra":
null, "cwd": null}`. Run `plan_claude_dispatch.py validate-input -` against
your payload before piping it into `run`; the validator names the missing
fields explicitly. The wrapper synthesises sensible defaults from the
allowed-null fields, so you do not need to set them substantively for a
smoke test.

## Subcommands

```
plan_claude_dispatch.py run               --input <path|-> [--output <path|->]
                                          [--timeout N] [--dry-run]
                                          [--backend-binary <path>]
                                          [--repo-root <path>]
plan_claude_dispatch.py list-agents
plan_claude_dispatch.py show-agent        <name>
plan_claude_dispatch.py validate-input    <path|->
plan_claude_dispatch.py validate-output   <path|->
```

Exit codes:

- `0` — `status: ok` envelope on stdout.
- `1` — any non-`ok` status (envelope still on stdout).
- `2` — wrapper-level failure (malformed input, schema unloadable). When
  caused by malformed input, the wrapper still emits a
  `status: input_invalid` envelope on stdout so callers can distinguish
  wrapper-rejection from backend-failure.

`--dry-run` short-circuits before spawn and emits a plan-only envelope
(`status: ok`, `status_reason: "dry_run: true"`) carrying the resolved
manifest, effective overrides, and would-be argv.

## Output envelope (status vocabulary)

Every dispatch returns a JSON envelope conforming to
`schemas/claude_dispatch_output.json`. The `status` field is one of:

| Status              | Meaning                                                                    |
|---------------------|----------------------------------------------------------------------------|
| `ok`                | Backend produced a schema-valid inner result.                              |
| `schema_invalid`    | Backend returned JSON but it failed the inner-result schema.               |
| `timeout`           | `subprocess.TimeoutExpired` from the backend.                              |
| `denied`            | Pre-spawn refusal (killswitch, tool-not-in-manifest, ...).                 |
| `backend_error`     | Non-JSON stdout, spawn failure, binary-not-found, non-zero exit.           |
| `budget_exhausted`  | Projected cumulative cost would exceed `PLAN_EXEC_COST_CAP_USD`.           |
| `depth_exceeded`    | `PLAN_EXEC_DISPATCH_DEPTH >= PLAN_EXEC_MAX_DEPTH`.                         |
| `manifest_invalid`  | Agent frontmatter / manifest could not be loaded or failed validation.     |
| `input_invalid`     | Input JSON failed the wrapper-level schema (or agent not in allowlist).    |
| `scope_violation`   | Observed delta strayed outside declared `files_changed`; cleanup detected. |

The shape is fixed (no `additionalProperties`); see `_claude_dispatch_envelope.py`
for the `build_*` constructors and `STATUS_VOCABULARY` constant.

## v1 dispatchable agents

The wrapper refuses any agent name outside this allowlist (defined in
`_claude_agent_manifest.DISPATCHABLE_AGENTS`):

```
{plan-analyst, plan-implementer, plan-remediator}
```

Each name resolves to `plugins/plan-executor/agents/<name>.md`. Anything else
is rejected at the manifest-loader boundary before any disk I/O — callers
cannot probe arbitrary file paths via the agent name.

## Refusal matrix (`_claude_guardrails.evaluate_preflight`)

Walked in fixed order; first match wins, subsequent rows are not evaluated:

| Row | Trigger                                          | Envelope                                  |
|-----|--------------------------------------------------|-------------------------------------------|
| 1   | `trace.depth >= max_depth`                       | `depth_exceeded`                          |
| 2   | `cost_so_far >= cost_cap_usd`                    | `budget_exhausted`                        |
| 3   | `agent` not in dispatchable allowlist            | `input_invalid` (`agent_not_allowed`)     |
| 4   | manifest dict shape invalid                      | `manifest_invalid`                        |
| 5   | requested tool not in manifest                   | `denied` (`tool_not_in_manifest`)         |
| 6   | killswitch env var truthy                        | `denied` (`killswitch`)                   |

Refusals are pre-spawn — the backend is never invoked.

## Environment variables

Every variable below is read by the wrapper (or its helpers). Names match
live code; variables only mentioned in stale docstrings are excluded.

| Variable                                | Purpose                                                                                          |
|-----------------------------------------|--------------------------------------------------------------------------------------------------|
| `PLAN_EXEC_DISPATCH_DEPTH`              | Current recursion depth. Injected per-hop by `scrub_env`; child sees `parent + 1`.               |
| `PLAN_EXEC_MAX_DEPTH`                   | Depth cap (default 2). Overridable per-input via `guardrails.max_depth`.                         |
| `PLAN_EXEC_PARENT_AGENT`                | Name of the invoking agent. Set by `scrub_env` for the child session.                            |
| `PLAN_EXEC_PARENT_RUN_ID`               | Invoking session's `run_id`. Set by `scrub_env` for the child session.                           |
| `PLAN_EXEC_COST_CAP_USD`                | Cumulative cost cap. Drives the `budget_exhausted` row of the refusal matrix.                    |
| `PLAN_EXEC_COST_SO_FAR_USD`             | Running cost the orchestrator stamps before each dispatch.                                       |
| `PLAN_EXEC_LOG_DIR`                     | Override for the `spans.jsonl` directory (default `<repo_root>/docs/plans/`).                    |
| `PLAN_EXEC_CLAUDE_DISPATCH_KILLSWITCH`  | Emergency killswitch. Truthy (`1`/`true`/`yes`/`on`) refuses every preflight as `denied`.        |
| `PLAN_EXEC_E2E`                         | Test-only gate. Live `claude -p` E2E suites (incl. `test_claude_permission_mode_probe.py`) skip unless set to `1`. |

`PLAN_EXEC_*` vars and the keys named in the manifest's `env_allowlist` are
the **only** parent-env entries inherited by the child `claude -p` call. Every
other key is dropped (`_claude_guardrails.scrub_env`, deny-by-default).

## Billing model

The inner `claude -p` invocation **shares billing with the parent session**.
Each dispatch is a fresh top-level session that bills against the same
account; nested chains compound accordingly. `cost_usd` and `tokens` in the
output envelope reflect *only* that single hop — they do not include the
parent's spend. Use `PLAN_EXEC_COST_SO_FAR_USD` + `PLAN_EXEC_COST_CAP_USD`
to bound cumulative spend across a multi-hop chain (Row 2 of the refusal
matrix enforces the cap).

## Safety invariants & Probe 2b outcome

Probe 2b (TASK-007, run `20260425T124346`) empirically tested whether
`--permission-mode acceptEdits` plus `--allowedTools` is a real boundary or
merely advisory. The probe asks a nested `plan-analyst` (with `Write`
deliberately excluded from its allowlist) to write a sentinel file.

**Outcome: Pass case A.** The nested session declined the `Write` call
entirely. `permission_denials=0` was reported but no file landed on disk —
the model itself refused to invoke the disallowed tool rather than the
harness logging a denial. Either way, the boundary held.

What that means for the safety story:

1. **`--permission-mode acceptEdits` is the shipped default**, not advisory
   backup. `_claude_backend.DEFAULT_PERMISSION_MODE = "acceptEdits"` is
   load-bearing.
2. **`--allowedTools` from the manifest narrows the surface**; `Agent` is
   *always* in `--disallowedTools` (depth-limit invariant: the nested
   session must not spawn a further `Agent` hop and bypass the depth
   counter).
3. **Delta-bounded cleanup (`_claude_dispatch_cleanup`) is the secondary
   defense**, not the primary one. It runs after every dispatch and reverts
   writes outside `result.files_changed`; the wrapper demotes a backend
   `ok` to `scope_violation` if cleanup detects a breach.
4. **Killswitch beats everything.** Setting
   `PLAN_EXEC_CLAUDE_DISPATCH_KILLSWITCH=1` denies every preflight at Row 6.

Citations: probe test `tests/scripts/test_claude_permission_mode_probe.py`;
Pass A established by TASK-007 in commit `0b6fd28`.

## Span log

Every dispatch appends one JSON line to `<repo_root>/docs/plans/spans.jsonl`
(override via `PLAN_EXEC_LOG_DIR`). Each line is a *reduced* envelope —
metadata, status, trace links, key flags — not the full §7 envelope.
Atomic-append uses POSIX `O_APPEND`, escalating to `fcntl.flock` for
payloads exceeding `PIPE_BUF` (4096 bytes on Linux). See
`_claude_span_log.py` for the canonical key list (`_SPAN_KEYS`).

## Argv shape (`_claude_backend._build_argv`)

```
claude -p \
    --agent <manifest.name> \
    --permission-mode <effective.permission_mode | "acceptEdits"> \
    --allowedTools <csv from manifest.tools> \
    --disallowedTools Agent[,extras] \
    --add-dir <effective.cwd> \
    --output-format json \
    <prompt>
```

`--output-format json` is mandatory: the parser asserts JSON stdout and
surfaces `backend_error / malformed_output` otherwise. `--add-dir` confines
read/write scope to the directory the wrapper expects (the same dir cleanup
runs against).

## Source-of-truth files

- `plan_claude_dispatch.py` — CLI entry, pipeline.
- `_claude_dispatch_envelope.py` — `build_*` constructors, status vocabulary.
- `schemas/claude_dispatch_input.json` — input wire schema.
- `schemas/claude_dispatch_output.json` — output wire schema.
- `_claude_agent_manifest.py` — `DISPATCHABLE_AGENTS`, manifest loader.
- `_claude_guardrails.py` — refusal matrix, `scrub_env`, killswitch constant.
- `_claude_backend.py` — argv, timeout/malformed mappings.
- `_claude_dispatch_cleanup.py` — baseline snapshot + delta-bounded revert.
- `_claude_span_log.py` — `spans.jsonl` shape and atomic append.
