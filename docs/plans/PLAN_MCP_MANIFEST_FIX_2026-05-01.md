# PLAN — MCP Manifest Bootstrap-Interpreter Fix

**Status:** Done — TASK-004 completed and focused manifest/scaffolding verification passed
**Created:** 2026-05-01
**Base branch:** main
**Depends on plans:** MCP_MIGRATION (complete and now archived at `docs/plans/archive/MCP_MIGRATION/`; this plan corrects a manifest defect that shipped at TASK-001 and was never exercised by the e2e test added at TASK-018)

## Current-state audit (2026-05-02)

This plan has already been mostly implemented in the working tree, but the plan metadata still said `Pending`. The remaining work is not to rebuild the manifest launcher; it is to finish verification and update the plan to match the current repo.

### Already present

- `plugins/plan-executor/.mcp.json` already points at `${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp_server.sh` with `plan_ops_mcp_server.py` in `args`.
- `plugins/plan-executor/scripts/run_mcp_server.sh` already exists, is executable, uses `set -eu`, resolves project root from `BASH_SOURCE[0]`, and tries `IMPLEMENT_PLAN_PYTHON` then `venv/bin/python` then `.venv/bin/python` then `python3`.
- `tests/scripts/test_plan_ops_mcp_manifest_spawn.py` already exists and passes all four spawn-level tests, including manifest command resolution, initialize response, project-venv interpreter selection, and the old undefined-variable negative control.
- `tests/scripts/test_plan_ops_mcp_scaffolding.py` already has the corrected manifest assertion for `run_mcp_server.sh`.
- The MCP migration plan has moved to `docs/plans/archive/MCP_MIGRATION/PLAN_MCP_MIGRATION.md`; that archived copy already contains the post-completion correction note and no longer references `${CLAUDE_PLUGIN_PYTHON}`.

### Completion

- `tests/scripts/test_plan_ops_mcp_scaffolding.py::test_uncaught_crash_emits_json_error_frame_and_exits_nonzero` now monkeypatches `mod._serve_stdio_jsonrpc` with a synchronous throwing function. This preserves the original crash-handler acceptance criterion without changing production code.
- `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_scaffolding.py tests/scripts/test_plan_ops_mcp_manifest_spawn.py` passed on 2026-05-02 (`13 passed`).
- The plan is complete in place. It was not renamed to `_done.md`; the functional completion marker is this status update plus green acceptance tests.

### Cross-plan compatibility

- **MCP TOOL PAYLOAD FIX:** no conflict. That plan changed `plan_ops.py`, MCP schemas, generated MCP registry code, and SKILL payload examples. This manifest plan only controls how Claude Code starts the already-registered MCP server.
- **Phase D:** no conflict. Phase D moved deterministic review routing into `plan_ops.py`; this launcher makes MCP startup reliable and does not alter Phase D state-machine logic.
- **Phase 1.5 state machine:** no conflict. Phase 1.5 is still in progress and its schedule currently lacks a `state` block until its TASK-002 lands. Finishing this manifest fix first is preferable because Phase 1.5 work depends on stable MCP availability during subsequent manual execution.
- **Archived MCP migration:** compatible. The original active path `docs/plans/MCP_MIGRATION/...` is now deleted/moved; all remaining references in this plan must target `docs/plans/archive/MCP_MIGRATION/...`.
- **Dirty work preservation:** do not revert the existing deleted `docs/plans/MCP_MIGRATION/...` entries or unrelated dirty files. Treat the archive move and prior MCP payload edits as existing user/worktree state.

## Problem

Historical defect: `plugins/plan-executor/.mcp.json` declared the MCP server's bootstrap interpreter via a Claude Code variable that does not exist:

```json
{
  "mcpServers": {
    "plan-ops": {
      "command": "${CLAUDE_PLUGIN_PYTHON}",
      "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"]
    }
  }
}
```

Claude Code's plugin reference (`code.claude.com/docs/en/plugins-reference.md`, "Environment variables" and "MCP servers" sections) documents only `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` as injected plugin variables. `${CLAUDE_PLUGIN_PYTHON}` was invented by the migration plan-author; see `docs/plans/archive/MCP_MIGRATION/PLAN_MCP_MIGRATION.md:31, 170` for the spec lines that formerly froze the wrong variable name before the archived correction. Claude Code substitutes the undefined variable to empty string and tries to spawn `""` — which fails with "MCP server failing to connect."

### Evidence chain

1. Before this plan's implementation, `plugins/plan-executor/.mcp.json:4` referenced `${CLAUDE_PLUGIN_PYTHON}`. The variable is unset in the Claude Code parent environment (verified: `printenv CLAUDE_PLUGIN_PYTHON` returns empty).
2. The MCP server itself works when launched with an explicit interpreter. Direct test: feeding `{"jsonrpc":"2.0","id":1,"method":"initialize",...}` over stdin to `venv/bin/python plugins/plan-executor/scripts/plan_ops_mcp_server.py` returns a valid `serverInfo` frame. The bug is in the launch contract, not the server.
3. The scaffolding test at `tests/scripts/test_plan_ops_mcp_scaffolding.py:78` formerly froze the literal string `${CLAUDE_PLUGIN_PYTHON}` as a manifest invariant — but the assertion verified the JSON content, not the spawn. The end-to-end test at `tests/scripts/test_implement_plan_mcp_e2e.py:256-258` bypassed the manifest entirely (`server._dispatch_registered_tool` was invoked in-process via Python imports). Together, both gaps explain how the defect shipped through TASK-001 → TASK-018.

### Why bare `"command": "python3"` is a workaround, not a fix

`plan_ops.py` has lazy imports of third-party packages reachable from MCP tool dispatch:

- `plan_ops.py:401` — `import yaml` inside a `cmd_*` function
- `plan_ops.py:3920` — `import yaml` inside another `cmd_*` function

The plugin's effective in-process dependency set at server runtime is `mcp` + `yaml` + (transitively) `jsonschema` and others, all installed in the project venv at `venv/lib/python3.13/site-packages/` (~30 third-party packages per `venv/bin/python -m pip list`). On a developer box where `python3` happens to be miniconda3 with those packages, the server boots; on a clean checkout / colleague's machine / CI, the manifest is parseable but the server crashes on first lazy import. Using bare `python3` would shift the failure from "fails to connect" to "boots, then crashes mid-run" — a worse failure mode masquerading as a fix.

## Decisions folded in

1. **Launcher shell script over plugin-data venv bootstrap.** The plugin already has a project venv at `venv/` that all bash-CLI codepaths use. Forking a parallel `${CLAUDE_PLUGIN_DATA}/venv` would (a) duplicate dep management, (b) diverge from the interpreter `plan_ops._resolve_python()` already prefers, and (c) require an install hook the plugin currently does not have. A launcher script that mirrors `_resolve_python()` precedence keeps the bootstrap interpreter and the subprocess-dispatch interpreter as a single resolved value end-to-end.

2. **Script-relative project-root resolution, not `$PWD` / `cwd`.** Claude Code's MCP launch cwd is not contractually pinned. Computing the project root from the script's own filesystem location (`scripts/run_mcp_server.sh` → `..` → plugin → `..` → `plugins` → `..` → repo root) is robust against any cwd Claude Code chooses, including invocations from worktrees or subdirectories. This avoids needing a `"cwd"` field in `.mcp.json` and avoids any runtime ambiguity. Codex independently flagged the cwd ambiguity in review; this is the more robust resolution than the `"cwd": "${CLAUDE_PLUGIN_ROOT}"` patch Codex proposed (since `CLAUDE_PLUGIN_ROOT` is the plugin dir, not the project dir, and the venv lives at the project root).

3. **Precedence parity with `plan_ops._resolve_python()` (`plan_ops.py:1983-2009`).** The launcher tries, in order: `$IMPLEMENT_PLAN_PYTHON` (if set and executable) → `$PROJECT_ROOT/venv/bin/python` → `$PROJECT_ROOT/.venv/bin/python` → `python3` from PATH. Final-fallback is omitted-by-design (`sys.executable` from `_resolve_python` has no shell equivalent without already running Python; the `python3`-on-PATH terminal step is the closest reasonable substitute). When no interpreter is found, the launcher writes a single explicit error frame to stderr and exits 127 — silent failure under `set -e` is the trap Codex flagged.

4. **POSIX-only, by design.** The plugin's existing `_resolve_python()` precedence already assumes POSIX paths (`venv/bin/python`, not `venv/Scripts/python.exe`). Native-Windows users would need a separate launcher and separate venv-layout assumptions; that's out of scope. The plugin's documented target is POSIX (Linux / macOS / WSL). Add a one-line note to the plan-doc correction in TASK-003 acknowledging this.

5. **Spawn-level test is mandatory.** The test gap that let `${CLAUDE_PLUGIN_PYTHON}` ship is the same gap that would let the next manifest defect ship. A regression test must exercise the actual subprocess spawn path: parse the manifest, expand `${CLAUDE_PLUGIN_ROOT}`, launch the resolved command, send a JSON-RPC `initialize` over stdin, and assert a valid `serverInfo` reply. This is TASK-002 below.

6. **Companion plan-doc correction (TASK-003).** `PLAN_MCP_MIGRATION.md:31, 168` documented the wrong manifest shape. The migration is Done so we don't reopen tasks, but leaving the wrong spec in the plan history would mislead any future reader trying to learn from the migration. A small in-place correction with a note that it was discovered post-completion is enough.

## Scope boundary

**In scope (current state):**

- `plugins/plan-executor/scripts/run_mcp_server.sh` — already present executable launcher; verify only unless a test proves a behavioral gap.
- `plugins/plan-executor/.mcp.json` — already updated to the launcher command; verify only.
- `tests/scripts/test_plan_ops_mcp_scaffolding.py` — finish the stale crash-path monkeypatch and keep the corrected manifest assertion.
- `tests/scripts/test_plan_ops_mcp_manifest_spawn.py` — already present spawn integration test; verify only.
- `docs/plans/archive/MCP_MIGRATION/PLAN_MCP_MIGRATION.md` — archived migration plan already corrected; verify only.
- `docs/plans/PLAN_MCP_MANIFEST_FIX_2026-05-01.md` — update plan status and current-state notes.

**Out of scope:**

- Plugin-data venv bootstrap (`${CLAUDE_PLUGIN_DATA}/venv` install hook). Tracked as a follow-up only if the project-venv-coupling becomes a portability blocker.
- Native-Windows launcher (`.cmd` / PowerShell). Plugin is POSIX-only by current contract.
- Any production change to `plan_ops_mcp_server.py`, `plan_ops.py`, `_plan_paths.py`, `_resolve_python()`, the `python_path` server capability, the `_dispatch_registered_tool` path, or any of the 30+ MCP tools registered via `_index.json`. The server behavior is already correct for this plan; the remaining code change is test-only.
- Any reopen of MCP_MIGRATION TASK-NNN entries. The plan stays Done; only the spec lines that documented the wrong shape are corrected in place.

## Verification

After the remaining task lands and already-present work is verified:

1. **Manual smoke** — restart Claude Code (or `/reload-plugins`), then invoke any `plan_ops__*` MCP tool from a session. The previous "failing to connect" error is gone, and the tool returns a valid response.
2. **Existing scaffolding test passes** with the updated assertion: `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_scaffolding.py` exits 0.
3. **New spawn-level test passes**: `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_manifest_spawn.py` exits 0. The test exercises the actual subprocess spawn that Claude Code performs.
4. **Full plan-executor test suite still passes**: `venv/bin/pytest -q tests/scripts/ --ignore=tests/scripts/test_plan_codex_dispatch_integration.py` exits 0 with no new failures.
5. **No regression in `_resolve_python()` parity** — the bootstrap interpreter selected by the launcher matches the interpreter `plan_ops.py preflight --json` reports as `python_path`. (Spot-check: launch the server via the launcher, capture `python_path` from the `initialize` response or from a `preflight` tool call, compare to `_resolve_python()` directly.)

## Tasks

## TASK-001: Add launcher script + update manifest + update scaffolding test

- **Status:** done
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/run_mcp_server.sh` (new, mode 0755)
  - `plugins/plan-executor/.mcp.json` (replace `command` and `args`)
  - `tests/scripts/test_plan_ops_mcp_scaffolding.py` (update lines 78–81 assertion to match the new manifest shape)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_scaffolding.py`
- **Acceptance criteria:**
  - `plugins/plan-executor/scripts/run_mcp_server.sh` is created with mode 0755 (executable), starts with `#!/usr/bin/env bash`, and contains exactly the precedence chain described in **Decisions #3** above. Specifically:
    - Uses `set -eu` (not `set -e` alone — the `-u` catches typos in env-var names).
    - Computes `PROJECT_ROOT` from `BASH_SOURCE[0]` via `cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd` so launch cwd does not matter.
    - First branch: `IMPLEMENT_PLAN_PYTHON` env var, but only if set non-empty AND `[ -x "$IMPLEMENT_PLAN_PYTHON" ]`.
    - Second branch: loops over `$PROJECT_ROOT/venv/bin/python` then `$PROJECT_ROOT/.venv/bin/python`; uses `[ -x "$p" ]` and `exec "$p" "$@"` on first match.
    - Third branch: `command -v python3 >/dev/null 2>&1 && exec python3 "$@"`.
    - Final fallthrough: prints a single line to stderr naming all four paths tried (`IMPLEMENT_PLAN_PYTHON`, `./venv`, `./.venv`, `python3` on PATH), then `exit 127`.
    - All branches use `exec` (no double-fork).
  - `plugins/plan-executor/.mcp.json` becomes:
    ```json
    {
      "mcpServers": {
        "plan-ops": {
          "command": "${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp_server.sh",
          "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"]
        }
      }
    }
    ```
    No `"cwd"` field. No reference to `${CLAUDE_PLUGIN_PYTHON}` anywhere.
  - `tests/scripts/test_plan_ops_mcp_scaffolding.py` lines 78–81 are updated. The assertion block must now read:
    ```python
    assert entry["command"] == "${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp_server.sh"
    assert entry["args"] == [
        "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops_mcp_server.py"
    ]
    ```
    Update the comment block at lines 74–77 to reference the launcher path instead of `${CLAUDE_PLUGIN_PYTHON}`. The other assertions in the file (`mcpServers` key, `plan-ops` server name) are unchanged.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_scaffolding.py` exits 0 after the changes.
  - Manual smoke (not automated, but confirm before marking task done): `bash plugins/plan-executor/scripts/run_mcp_server.sh -c "import sys; print(sys.executable)"` from any cwd prints `/mnt/d/claude-plan-executor/venv/bin/python`.

## TASK-002: Add spawn-level integration test

- **Status:** done
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `tests/scripts/test_plan_ops_mcp_manifest_spawn.py` (new)
- **Dependencies:** [TASK-001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_manifest_spawn.py`
- **Acceptance criteria:**
  - The test file is created at `tests/scripts/test_plan_ops_mcp_manifest_spawn.py` and contains at minimum these test cases:
    - `test_manifest_command_resolves_to_executable_launcher` — load `plugins/plan-executor/.mcp.json`, expand `${CLAUDE_PLUGIN_ROOT}` to the absolute plugin path, assert the resolved `command` path exists and is executable.
    - `test_manifest_spawn_returns_valid_initialize_response` — spawn the resolved command (with the resolved `args`) as a subprocess via `subprocess.Popen` with `stdin=PIPE`, `stdout=PIPE`, `stderr=PIPE`. Send a single JSON-RPC `initialize` request frame on stdin, close stdin, read one JSON-RPC frame from stdout. Assert the response has `result.serverInfo.name == "plan-ops"` and `result.protocolVersion` is a non-empty string. The subprocess must exit 0 within a 10-second timeout.
    - `test_manifest_spawn_uses_project_venv_interpreter` — spawn the resolved command as above. After receiving the `initialize` response, send a `tools/list` request, find any tool that returns `python_path` (e.g., the `preflight` tool wrapper) — or alternately read the `python_path` server capability from the `initialize` response if surfaced there. Assert the value resolves to `<repo>/venv/bin/python` (the project venv), confirming the launcher selected the right interpreter.
  - The test must be skipped (not failed) on environments where `mcp` SDK is not installed in the project venv — use the existing `requires_mcp` skip pattern from `test_plan_ops_mcp_scaffolding.py:42`.
  - The test must work with `pytest tests/scripts/test_plan_ops_mcp_manifest_spawn.py` from the repo root (no special env-var setup); it does its own `${CLAUDE_PLUGIN_ROOT}` expansion via `Path(__file__)` resolution.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_manifest_spawn.py` exits 0 after the test is added and TASK-001 is in place.
  - **Negative-control check (recommended but not strictly required):** before committing TASK-001's manifest change, the new spawn test must FAIL against the original `${CLAUDE_PLUGIN_PYTHON}` manifest. This proves the test would have caught the original defect. If implementing TASK-001 and TASK-002 in sequence, run the test against the pre-TASK-001 manifest first, observe failure, then commit TASK-001 and observe pass. (If you implement them as one atomic change, document this expected behavior in a comment in the test file.)

## TASK-003: Correct PLAN_MCP_MIGRATION.md manifest spec lines

- **Status:** done
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `docs/plans/archive/MCP_MIGRATION/PLAN_MCP_MIGRATION.md` (lines 31 and 170 — locate the two sentences that document the manifest command shape)
- **Dependencies:** []
- **Test command:** `! grep -n 'CLAUDE_PLUGIN_PYTHON' docs/plans/archive/MCP_MIGRATION/PLAN_MCP_MIGRATION.md`
- **Acceptance criteria:**
  - Both occurrences of `${CLAUDE_PLUGIN_PYTHON}` in `PLAN_MCP_MIGRATION.md` are replaced with the corrected manifest shape: `"${CLAUDE_PLUGIN_ROOT}/scripts/run_mcp_server.sh"` for the `command`, with the launcher's role explained in one sentence.
  - A short post-completion note is added near the corrected lines, in the form: `> Note (2026-05-01, post-completion): the original spec referenced \`${CLAUDE_PLUGIN_PYTHON}\` as a Claude Code-injected variable, but Claude Code does not inject that variable. Corrected by PLAN_MCP_MANIFEST_FIX_2026-05-01.md; manifest now points at \`run_mcp_server.sh\` which mirrors \`_resolve_python()\` precedence at bootstrap.`
  - The note acknowledges POSIX-only assumption per **Decisions #4**.
  - `grep -n 'CLAUDE_PLUGIN_PYTHON' docs/plans/archive/MCP_MIGRATION/PLAN_MCP_MIGRATION.md` returns zero lines (the test command above).
  - No other content in the archived `PLAN_MCP_MIGRATION.md` is changed unless required by the archive path correction. Tasks remain Done; this is a documentation correction only.

## TASK-004: Refresh stale crash-handler scaffolding test

- **Status:** done
- **Priority:** critical
- **Agent:** codex
- **Files:**
  - `tests/scripts/test_plan_ops_mcp_scaffolding.py`
- **Dependencies:** [TASK-001, TASK-002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_scaffolding.py tests/scripts/test_plan_ops_mcp_manifest_spawn.py`
- **Acceptance criteria:**
  - `test_uncaught_crash_emits_json_error_frame_and_exits_nonzero` still forces an exception inside `plan_ops_mcp_server.main()`.
  - The generated `crash_runner.py` monkeypatches `mod._serve_stdio_jsonrpc`, not `mod._serve`.
  - The injected `_boom` function is synchronous (`def _boom(): ...`), matching `_serve_stdio_jsonrpc()`.
  - The test continues to assert a non-zero return code and a single JSON-RPC error frame containing `synthetic crash`.
  - No production code is changed for this task.
  - `venv/bin/pytest -q tests/scripts/test_plan_ops_mcp_scaffolding.py tests/scripts/test_plan_ops_mcp_manifest_spawn.py` exits 0.

## Notes for the implementer

- **Codex review of this plan (2026-05-01):** the plan's direction was endorsed as SHIP-WITH-MODIFICATIONS in a pre-implementation second-opinion pass. The modifications Codex requested (script-relative path resolution, explicit stderr error on no-Python-found, `set -eu` instead of bare `set -e`, mandatory spawn-level test) are folded into the acceptance criteria above.
- **Completion note:** TASK-001, TASK-002, TASK-003, and TASK-004 are complete. The combined manifest/scaffolding tests passed on 2026-05-02.
- **Do not change production `plan_ops_mcp_server.py`, `plan_ops.py`, or any tool registration code for TASK-004.** The server is correct as it stands; the remaining defect is a stale test hook.
- **If `python_path` is not surfaced in the `initialize` response capabilities** (the migration plan promised it would be at `plan_ops_mcp_server.py:60`, but verify), the third test in TASK-002 may need to call `tools/list` and pick a tool that exposes interpreter info, or be split into a `preflight`-tool-call test. Adapt as needed — the underlying assertion is "the launcher resolved the project venv, not some other Python."
