---
bug_id: 148
status: OPEN
group: DECOMPOSE-TESTCMD-CONTINUATION
severity: major
source_fix_id: null
source_plan: null
source_date: '2026-05-30'
origin: Surfaced while dry-running the trading-system pre-earnings pipeline plans (PHASE_5_shadow_live, /implement-plan run 20260530T193741, directory mode). A conformed plan's `**Test command:**` fenced block used backslash line-continuations for a single multi-line shell command; the decomposer's fenced-block joiner corrupted it into a non-runnable `\ &&` form, which Codex plan-review then correctly flagged as a terminal finding.
decomposed_at: '2026-05-30'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
  - plugins/plan-executor/scripts/plan_ops.py
content_fingerprint: null
change_history: []
---

# BUG-148: `_extract_fenced_block_field` ` && `-joins fenced `Test command` lines without collapsing backslash (`\`) line-continuations, corrupting a single multi-line shell command

**Status:** OPEN
**Severity:** major (silently emits a non-runnable test/run command into the child + schedule; only caught downstream if a reviewer happens to inspect the command string)
**Group:** DECOMPOSE-TESTCMD-CONTINUATION
**Depends on:** none
**Test command:** `venv/bin/pytest tests/ -k "fenced or extract_fenced or test_command" -v`

## Acceptance criteria
- `_extract_fenced_block_field` (plugins/plan-executor/scripts/plan_ops.py) MUST correctly handle a fenced `**Test command:**` body whose lines use trailing backslash (`\`) line-continuations to express a SINGLE logical shell command: continuation lines are reconstructed into one logical line (trailing `\` + newline → single space) before any ` && ` join.
- A unit test MUST feed a fenced block of the form:
  ```
  - **Test command:**
    ```bash
    FOO=1 \
    BAR=2 \
    venv/bin/python script.py --flag
    ```
  ```
  and assert the extracted command is `FOO=1 BAR=2 venv/bin/python script.py --flag` (one runnable command, env vars exported) — NOT `FOO=1 \ && BAR=2 \ && venv/bin/python script.py --flag`.
- An existing-behaviour test MUST confirm a genuine multi-step fenced body (no backslashes) is still ` && `-joined (fail-fast semantics under `shell=True` preserved).
- A mixed test (one `\`-continued logical command, then a separate command line) MUST join the reconstructed logical commands with ` && `.

## Problem
`_extract_fenced_block_field` (plan_ops.py:2783-2838) supports the multi-line `**Test command:**` form:
```
- **Test command:**
  ```bash
  cmd1
  cmd2
  ```
```
It reads the fenced body, `.strip()`s each line (line 2832), drops blanks (line 2835), and `return " && ".join(commands)` (line 2838). The documented intent (lines 2795-2799) is fail-fast multi-step semantics, because `run_test_command` runs the string under `shell=True` and a bare newline-join would only surface the last line's exit code.

The gap: it assumes every physical fenced line is an INDEPENDENT command. When the author instead writes a SINGLE shell command spanning multiple lines via trailing `\` continuations — the standard way to write a long env-prefixed invocation — `.strip()` does not remove the trailing `\`, so each `VAR=value \` line becomes its own "command" and they are ` && `-joined into:
```
VAR1=v1 \ && VAR2=v2 \ && … \ && venv/bin/python …
```
Under the shell this does NOT export VAR1/VAR2 to the final program (the `\ &&` breaks the assignment-prefix chain) — the command is non-runnable / semantically wrong.

## Reproduction (forensic)
- Trading-system repo, plan `docs/plans/20260527_pre_earnings_pipeline/PHASE_5_shadow_live.md`. The conformed task (decomposes to TASK-005) declares its `**Test command:**` as a fenced block (lines ~185-201) in the valid single-command form:
  ```
  SCHWAB_STREAMER_ENABLED=true \
  NASDAQ_CALENDAR_ENABLED=true \
  …
  ACTIVE_STRATEGIES=late_trend_quality \
  venv/bin/python scripts/run_unified_trader.py paper --capital 10000 --strategies late_trend_quality
  ```
- After `/implement-plan … --dry-run` (run `20260530T193741`) decomposed it, the child `PHASE_5_shadow_live/TASK-005_*.md` `**Test command:**` and the entry in `PHASE_5_shadow_live.schedule.json` became:
  ```
  SCHWAB_STREAMER_ENABLED=true \ && NASDAQ_CALENDAR_ENABLED=true \ && … \ && venv/bin/python scripts/run_unified_trader.py paper …
  ```
- Codex plan-review (pass 2) flagged this as a terminal `needs-replan` (env vars not exported), producing a binding plan-review halt for PHASE_5.

## Recommended fix
In `_extract_fenced_block_field`, before the ` && ` join, collapse backslash continuations:
- Walk the non-blank body lines; if a line ends with `\` (after rstrip), drop the trailing `\` and concatenate the next line with a single space, continuing until a line without a trailing `\`. The accumulated text is one logical command.
- ` && `-join the resulting logical commands (so genuine multi-step recipes keep fail-fast semantics; single long commands are reconstructed intact).

## Notes
- The plan authoring used a documented, supported form (the docstring at line 2789 shows exactly this `- **Test command:**` + fenced shape), so the defect is in the joiner, not the plan.
- Author-side workaround until fixed: write `**Test command:**` as a single physical line (space-separated env assignments, no backslashes), or as genuinely independent ` && `-able steps.
- Companion observation (NOT filed here — already tracked): the MCP-server registry drift (`set_task_agent` / the `plan-analyst-per-child` template not exposed over MCP, so the orchestrator falls back to the `plan_ops.py` CLI) is covered by `PLAN_SET_TASK_AGENT_MCP_EXPOSURE_2026-05-23` and `PLAN_DEPRECATE_CLAUDE_CLI_2026-05-14`.

## Run history
(none yet — bug filed 2026-05-30)
