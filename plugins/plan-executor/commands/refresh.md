---
name: refresh
description: Rsync the plan-executor source repo into the installed plugin cache so recent edits take effect. Prompts the user to /reload-plugins afterward.
---

Refresh the installed plan-executor plugin cache with the latest edits from the source repo at `/mnt/d/claude-plan-executor`.

Run this exact bash block:

```bash
SRC=/mnt/d/claude-plan-executor/plugins/plan-executor
VER=$(python3 -c "import json; print(json.load(open('$SRC/.claude-plugin/plugin.json'))['version'])")
DST=~/.claude/plugins/cache/claude-plan-executor/plan-executor/$VER
test -d "$DST" || { echo "Cache not found: $DST — run '/plugin install plan-executor@claude-plan-executor --scope user' first."; exit 1; }
rsync -a --delete --exclude='__pycache__/' --exclude='*.pyc' "$SRC/" "$DST/"
echo "Refreshed $DST"
```

Then tell the user (verbatim): "Run `/reload-plugins` to apply the refresh."

If `rsync` prints nothing was transferred, say so — it means no changes since last refresh. If the cache-not-found branch fires, stop and relay the instruction; do not try to install the plugin yourself.
