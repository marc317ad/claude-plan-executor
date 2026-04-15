#!/usr/bin/env bash
#
# Install claude-plan-executor into a target project via relative symlinks.
#
# Usage: ./install.sh /path/to/target/project
#
# What it does:
#   1. Creates relative symlinks under the target's .claude/ and scripts/ dirs
#      pointing back into this shared repo.
#   2. Writes a default .claude/plan-executor.json if none exists.
#   3. Notes what the target still needs to provide (code-reviewer.md, a
#      CLAUDE.md with a python invocation line).
#
# The installer is idempotent: existing symlinks are left in place, and
# existing non-symlink files at the same paths are preserved (with a warning).

set -euo pipefail

SHARED_REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
TARGET="${1:-}"

if [[ -z "$TARGET" ]]; then
  echo "usage: $0 /path/to/project" >&2
  exit 1
fi

if [[ ! -d "$TARGET" ]]; then
  echo "error: target is not a directory: $TARGET" >&2
  exit 1
fi

TARGET="$(cd "$TARGET" && pwd)"

FILES=(
  ".claude/skills/implement-plan/SKILL.md"
  ".claude/skills/implement-plan/dispatch-templates.md"
  ".claude/skills/implement-plan/run-log-schema.md"
  ".claude/agents/plan-analyst.md"
  ".claude/agents/plan-implementer.md"
  "scripts/plan_ops.py"
  "scripts/plan_codex_dispatch.py"
  "scripts/codex_implement_schema.json"
  "scripts/codex_review_schema.json"
)

echo "Installing from: $SHARED_REPO"
echo "              →: $TARGET"
echo ""

for f in "${FILES[@]}"; do
  src="$SHARED_REPO/$f"
  dst="$TARGET/$f"

  if [[ ! -e "$src" ]]; then
    echo "ERROR: source missing in shared repo: $src" >&2
    exit 1
  fi

  mkdir -p "$(dirname "$dst")"

  if [[ -L "$dst" ]]; then
    echo "  skip (symlink exists): $f"
    continue
  fi
  if [[ -e "$dst" ]]; then
    echo "  WARN (non-symlink file exists, preserving): $f" >&2
    continue
  fi

  ln -sr "$src" "$dst"
  echo "  link: $f"
done

CFG="$TARGET/.claude/plan-executor.json"
if [[ ! -f "$CFG" ]]; then
  mkdir -p "$(dirname "$CFG")"
  cat > "$CFG" <<'JSON'
{
  "plan_dir": "docs/plans"
}
JSON
  echo "  wrote default config: .claude/plan-executor.json"
else
  echo "  skip (config exists): .claude/plan-executor.json"
fi

echo ""
echo "Install complete."
echo ""

if [[ ! -f "$TARGET/.claude/agents/code-reviewer.md" ]]; then
  echo "Next steps:"
  echo "  1. Copy $SHARED_REPO/templates/code-reviewer.md.template"
  echo "     to $TARGET/.claude/agents/code-reviewer.md and customize."
  echo "  2. Ensure $TARGET/CLAUDE.md documents the Python invocation"
  echo "     (e.g. 'venv/bin/python' or 'python3') — the skill defers to this."
fi
