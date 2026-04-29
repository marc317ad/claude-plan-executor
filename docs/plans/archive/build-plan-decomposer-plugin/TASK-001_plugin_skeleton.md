---
task_id: "001"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Target Plugin Layout; Marketplace Entry + Plugin Manifest"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-001
depends_on: []
superseded_by: []
change_history: []
priority: critical
---
<!-- prose_decisions: [prose_omitted_trivial_scope, prose_omitted_bounded_by_files, prose_omitted_self_contained] -->

# TASK-001 — Create plan-decomposer plugin skeleton + manifest

**Source section:** Target Plugin Layout; Marketplace Entry + Plugin Manifest
**Base branch:** main
**Chunk dependencies:** none

---

## Goal

Stand up the empty `plan-decomposer` plugin directory tree sibling to `plan-executor`, wire its manifest, and register it in the repo marketplace so `/plugin list` can see it.

## Scoped Context

The plugin lives at `plugins/plan-decomposer/` and must mirror `plan-executor`'s layout (`.claude-plugin/`, `agents/`, `commands/`, `skills/`, `scripts/`, `templates/`, `docs/`). Only `plugin.json`, `marketplace.json` entry, and a placeholder `docs/README.md` land in this TASK — every other file is filled by later TASKs.

## Verification

- `ls plugins/plan-decomposer/.claude-plugin/plugin.json` succeeds.
- `jq '.plugins[] | select(.name=="plan-decomposer")' .claude-plugin/marketplace.json` returns the new entry.

---

## Tasks

### TASK-001: Create plan-decomposer plugin skeleton + manifest

- **Status:** open
- **Priority:** critical
- **Files:**
  - `plugins/plan-decomposer/.claude-plugin/plugin.json` — new manifest (`name`, `version: 0.1.0`, `description`, `author.name: marc317ad`)
  - `plugins/plan-decomposer/docs/README.md` — one-paragraph placeholder (replaced in TASK-016)
  - `.claude-plugin/marketplace.json` — append plan-decomposer entry to `plugins[]`
  - `plugins/plan-decomposer/{agents,commands,skills/decompose-plan,scripts,templates}/` — empty dirs committed via `.gitkeep`
- **Dependencies:** none
- **Test command:** `jq '.plugins[] | select(.name=="plan-decomposer").source' .claude-plugin/marketplace.json`
- **Acceptance criteria:**
  - `plugins/plan-decomposer/.claude-plugin/plugin.json` parses as JSON and contains `"name": "plan-decomposer"` and `"version": "0.1.0"`.
  - `.claude-plugin/marketplace.json` `plugins[]` contains an entry with `"name": "plan-decomposer"` and `"source": "./plugins/plan-decomposer"`.
  - Directories `agents/`, `commands/`, `skills/decompose-plan/`, `scripts/`, `templates/`, `docs/` exist under `plugins/plan-decomposer/`.
  - `jq` on both JSON files exits 0.

**Description:**
Add the plugin scaffold required before any subcommand, agent, or template can be written. Mirrors the `plan-executor` sibling and registers via marketplace so subsequent TASKs can drop files into resolved plugin paths.

**Reversion guidance:**
`git rm -r plugins/plan-decomposer/` and revert the `marketplace.json` hunk.
