# claude-plan-executor

Portable dual-agent `/implement-plan` executor packaged as a **Claude Code plugin**. Install once at user scope and `/implement-plan <plan-path>` is available from every project you open.

The skill runs a plan file through a Claude-tier + Codex-tier pipeline: `plan-analyst` validates and schedules tasks, `plan-implementer` (or Codex via `plan_codex_dispatch.py`) executes each task, `code-reviewer` cross-reviews, and `plan_ops.py` handles narrow commits, run-log appends, and failure handling.

## Install

**Dev mode** (live-reload from this checkout — recommended while iterating):

```bash
claude --plugin-dir /mnt/d/claude-plan-executor
```

**Permanent install** (user scope, available from every project):

Inside Claude Code:

```
/plugin marketplace add /mnt/d/claude-plan-executor
/plugin install plan-executor@claude-plan-executor --scope user
```

Restart Claude Code afterwards. `/implement-plan` should appear in the slash-command list.

## Authoring plans

`/implement-plan` and `implement_plan.py` consume **decomposed-plan
directories** (`docs/plans/<PLAN_SLUG>/` containing `00_INDEX.json` plus one
`TASK-NNN_<slug>.md` child per task). When authoring a plan by hand, by an
external decomposer agent, or via `plan_ops.py decompose-plan`, follow the
canonical guide at
[`templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md`](templates/DECOMPOSED_PLAN_DIRECTORY_TEMPLATE.md).
That guide documents the directory shape, the `00_INDEX.json` roster contract,
the required child-file sections, status vocabularies, and the validation
commands. Reusable scaffolds live alongside the parser at
[`plugins/plan-executor/templates/`](plugins/plan-executor/templates/) — see its
[`README.md`](plugins/plan-executor/templates/README.md) for which file to
copy. `plugins/plan-executor/scripts/plan_ops.py` remains the runtime authority
for the contract; drift between templates, the root guide, and the parser
constants is pinned by `TestDecomposedTemplateDrift` in
`tests/scripts/test_plan_ops.py`.

## What this plugin provides

- **Slash command**: `/implement-plan <plan-path>` (dispatches the skill)
- **Skill**: `skills/implement-plan/SKILL.md` (orchestrator protocol)
- **Subagents**: `plan-analyst`, `plan-implementer`
- **Scripts**: `implement_plan.py` (portable runner), `plan_ops.py` (commit ceremony, run-log, reconcile), `plan_codex_dispatch.py` (Codex wrapper)
- **JSON envelope schemas**: `codex_implement_schema.json`, `codex_review_schema.json`
- **Reviewer template**: `templates/code-reviewer.md.template` (per-project starter)

Scripts execute with `cwd = consuming project` and read project-local `.claude/plan-executor.json`. Plugin-internal references resolve through `${CLAUDE_PLUGIN_ROOT}` at runtime.

### Nested Claude dispatch (`plan_claude_dispatch.py`)

Sibling to `plan_codex_dispatch.py`: a structured wrapper around `claude -p` that lets a subagent (which lacks the `Agent` tool) shell out to a fresh top-level Claude session. The wrapper enforces an agent allowlist (`{plan-analyst, plan-implementer, plan-remediator}`), strips parent env down to a deny-by-default surface, applies depth/budget/killswitch guardrails, and runs delta-bounded cleanup so writes outside `result.files_changed` are reverted. This is what enables Claude-tier tasks to parallelize and what lets remediators delegate cross-checks without bouncing back to the orchestrator. See `plugins/plan-executor/scripts/README_claude_dispatch.md` for the input/output JSON shapes, env vars, refusal matrix, and the Probe 2b safety story.

## Per-project requirements

Each project that uses `/implement-plan` must provide:

1. **`.claude/agents/code-reviewer.md`** — domain-specific reviewer. Copy `templates/code-reviewer.md.template` from the plugin and fill in `{{…}}` placeholders.
2. **`.claude/plan-executor.json`** — project config (see Configuration below).
3. **`CLAUDE.md`** — documents the Python invocation the skill should use (e.g. `venv/bin/python`). The skill reads this convention from your project — the plugin assumes nothing about your interpreter.
4. **Plan directory** — defaults to `docs/plans/`. Override via `plan_dir` in the config.

## Configuration

`.claude/plan-executor.json` (at the consuming project root):

```json
{
  "plan_dir": "docs/plans"
}
```

Fields:
- `plan_dir` (string, default `"docs/plans"`) — where plan files, `_run_log.jsonl`, `_run_lock.json`, and `*.schedule.json` sidecars live. All executor-infrastructure protected-path patterns are derived from this.

### Script runner

`implement_plan.py` is a scriptable entry point for agents or CI contexts that need one durable command instead of manually stepping through the SKILL:

```bash
venv/bin/python plugins/plan-executor/scripts/implement_plan.py docs/plans/example \
  --parallel 2 \
  --provider-preference codex,claude,gemini \
  --assign TASK-001=claude \
  --dry-run
```

Structured callers can use:

```bash
venv/bin/python plugins/plan-executor/scripts/implement_plan.py run \
  --plan docs/plans/example \
  --config docs/plans/example.runner.json
```

Runner config is JSON. It accepts the same core routing fields as the CLI:

```json
{
  "plan": "docs/plans/example",
  "parallel": 2,
  "provider_preference": ["codex", "claude", "gemini"],
  "assignments": ["TASK-001=claude"],
  "reviewers": ["codex", "gemini", "claude"],
  "plan_reviewer": "codex",
  "dry_run": true,
  "unattended_revert_policy": "pause"
}
```

Provider selection is capability-based. Built-in providers advertise which roles they can satisfy:
- `claude`: classify, implement, review, triage, author; routes as Claude for implementation/review.
- `codex`: implement, review, plan review; routes as Codex.
- `gemini`: review and plan review only; it is not an implementation provider.
- `stub`: test-only provider for dry-run/e2e state-machine coverage without live model calls.

Use MCP directly in interactive Claude/Codex sessions where `plan_ops__*` tools are available; those schemas remain the source of truth. Use the SKILL through `/implement-plan` for the established Claude Code workflow. Use `implement_plan.py` when MCP is unavailable, when another agent needs a script entry point, or when CI/non-interactive automation should own the whole run.

**Behavior on bad config (F-4):**
- Missing file → silent fallback to defaults.
- Malformed JSON → exit 2, stderr names the file.
- Unreadable file (e.g. `chmod 000`) → exit 2, stderr names the file.

Both scripts call `_load_plan_config()` at **import time**, so a malformed config file makes even `--help` exit 2. This is intentional: if the config is broken, no operation should appear to work.

## Path discovery from the orchestrator

The orchestrator queries dynamic paths once at preflight:

```bash
venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" path-info --json
```

Returns:

```json
{
  "plan_dir": "docs/plans",
  "run_log": "docs/plans/_run_log.jsonl",
  "run_lock": "docs/plans/_run_lock.json",
  "schedule_glob": "docs/plans/*.schedule.json"
}
```

These bind to placeholders (`<plan_dir>`, `<run_log>`, `<run_lock>`, `<schedule_file>`) that SKILL.md and dispatch-templates.md reference everywhere. No hardcoded `docs/plans` outside default-value documentation.

## Updating

```bash
cd /mnt/d/claude-plan-executor
git pull
```

Dev-mode users see changes on next Claude Code restart. Permanently-installed users run `/plugin update plan-executor@claude-plan-executor`.

## Remote WSL test bed

Use the PowerShell sync scripts when this checkout is the development machine
and `marcd@100.112.76.122` is the Windows test bed. The scripts copy the
current tracked, modified, and untracked non-ignored files with `scp`, unpack
them into WSL on the remote `D:` drive, create `venv`, and install
`requirements-dev.txt`.

Prerequisites: SSH access to the Windows host, WSL installed on that host, and
`python3`, `python3-venv`, and `python3-pip` available inside the target WSL
distro.

First bootstrap, from PowerShell in this repo:

```powershell
.\scripts\bootstrap_remote_wsl.ps1
```

Repeat updates after local development changes:

```powershell
.\scripts\update_remote_wsl.ps1
```

Run a focused smoke test remotely after the sync:

```powershell
.\scripts\update_remote_wsl.ps1 -RunTests
```

If the remote has multiple WSL distros, pass the target distro name:

```powershell
.\scripts\update_remote_wsl.ps1 -Distro Ubuntu -RunTests
```

If the SSH key is not one of OpenSSH's default filenames, pass it explicitly:

```powershell
.\scripts\update_remote_wsl.ps1 -IdentityFile ~/.ssh/id_ed25519_windows -RunTests
```

By default the remote tree is mirrored and stale files are removed, while
`venv/` and `.remote-sync/` are preserved. Use `-NoPrune` if you need to keep
extra scratch files in `D:\claude-plan-executor`.

## Architecture notes

- Scripts run with `cwd = consuming project`, not the plugin install directory.
- `${CLAUDE_PLUGIN_ROOT}` resolves at runtime to the plugin install dir (dev mode: this checkout; permanent install: `~/.claude/plugins/cache/claude-plan-executor/plan-executor/<version>/`).
- `Path(__file__).resolve()` inside scripts works in both modes — schema JSONs colocated next to scripts are found via the resolved script directory.
- The plugin commits no per-project state. The trading system (or any consuming project) commits only its own `.claude/plan-executor.json` and `.claude/agents/code-reviewer.md`.

## Outside Claude Code

The scripts are usable from CI or shell scripts that don't run inside Claude Code. With `CLAUDE_PLUGIN_ROOT` unset, invoke via the absolute install-cache path:

```bash
~/.claude/plugins/cache/claude-plan-executor/plan-executor/<version>/scripts/plan_ops.py path-info --json
```

## What isn't portable

- **Plan file schema docs** live inside `agents/plan-analyst.md` (§ "Plan schema (reference)"). If you keep a larger design doc, that stays in the consuming project — it's project history, not harness logic.
- **`code-reviewer`** is inherently domain-specific. The template is a starting point; every project writes its own.
