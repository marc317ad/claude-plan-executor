# claude-plan-executor

Portable dual-agent `/implement-plan` executor for Claude Code, extracted so it can be reused across projects.

The skill runs a plan file through a Claude-tier + Codex-tier pipeline: plan-analyst validates and schedules tasks, plan-implementer (or Codex) executes each task, code-reviewer cross-reviews, and `plan_ops.py` handles narrow commits, run-log appends, and failure handling.

## What lives here vs. per-project

**Shared (this repo):**
- `.claude/skills/implement-plan/` — skill contract, dispatch templates, run-log schema
- `.claude/agents/plan-analyst.md`, `plan-implementer.md` — generic subagents
- `scripts/plan_ops.py`, `scripts/plan_codex_dispatch.py` — plan orchestration + Codex wrapper
- `scripts/codex_{implement,review}_schema.json` — JSON envelope schemas
- `templates/code-reviewer.md.template` — generic reviewer stub

**Per-project (stays in each consuming project):**
- `.claude/agents/code-reviewer.md` — domain-specific; every project customizes this
- `.claude/plan-executor.json` — project config (plan_dir, etc.)
- `CLAUDE.md` — documents the Python invocation the skill should use (e.g. `venv/bin/python`)
- `docs/plans/` (or whatever `plan_dir` points to) — actual plan files + run logs

## Install into a project

```bash
/mnt/d/claude-plan-executor/install.sh /path/to/your/project
```

The installer creates **relative symlinks** from the target project's expected paths back into this repo, so updates pulled here are live everywhere. The installer is idempotent: existing symlinks are left alone, and existing non-symlink files at the target paths are preserved with a warning.

After install, in the target project:

1. Copy `templates/code-reviewer.md.template` to `.claude/agents/code-reviewer.md` and fill in the `{{…}}` placeholders.
2. Ensure `CLAUDE.md` documents the Python invocation the skill should call (the skill defers to this via `venv/bin/python` or whatever you specify).
3. Create `docs/plans/` (or override `plan_dir` in `.claude/plan-executor.json`).

## Configuration

`.claude/plan-executor.json` (at the target project root):

```json
{
  "plan_dir": "docs/plans"
}
```

If the file is missing, scripts fall back to `docs/plans` — so a clean install mirrors the original hardcoded behavior.

Fields:
- `plan_dir` (string, default `"docs/plans"`) — where plan files, `_run_log.jsonl`, `_run_lock.json`, and `*.schedule.json` sidecars live. All executor-infrastructure protected-path patterns are derived from this.

## Updating

Pull updates here:

```bash
cd /mnt/d/claude-plan-executor
git pull
```

Every consuming project picks up the change immediately because they symlink in. If a change breaks something, `git revert` here and every project is fixed in one shot.

To pin a project to a specific version instead of tracking `main`, replace the symlinks with copies of the files at a known commit.

## Manual install (no installer)

If you prefer not to run the script, the installer's logic is: for each of the nine paths listed in `install.sh` (`FILES=(...)`), create `ln -sr <shared>/<path> <target>/<path>`. Then write `.claude/plan-executor.json` with `{"plan_dir": "docs/plans"}`.

## What isn't portable yet

- **Plan file schema docs** are documented inside `.claude/agents/plan-analyst.md` (§ "Plan schema (reference)"). If you keep a larger design doc (e.g. `DUAL_AGENT_PLAN_EXECUTOR.md`), that stays in the consuming project — it's project-history, not harness logic.
- **The `code-reviewer` agent** is inherently domain-specific. The template here is a starting point; every project writes its own.
