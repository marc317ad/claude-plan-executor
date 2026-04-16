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

## What this plugin provides

- **Slash command**: `/implement-plan <plan-path>` (dispatches the skill)
- **Skill**: `skills/implement-plan/SKILL.md` (orchestrator protocol)
- **Subagents**: `plan-analyst`, `plan-implementer`
- **Scripts**: `plan_ops.py` (commit ceremony, run-log, reconcile), `plan_codex_dispatch.py` (Codex wrapper)
- **JSON envelope schemas**: `codex_implement_schema.json`, `codex_review_schema.json`
- **Reviewer template**: `templates/code-reviewer.md.template` (per-project starter)

Scripts execute with `cwd = consuming project` and read project-local `.claude/plan-executor.json`. Plugin-internal references resolve through `${CLAUDE_PLUGIN_ROOT}` at runtime.

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
