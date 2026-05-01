#!/usr/bin/env bash
set -eu

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

if [ "${IMPLEMENT_PLAN_PYTHON:-}" ] && [ -x "$IMPLEMENT_PLAN_PYTHON" ]; then
    exec "$IMPLEMENT_PLAN_PYTHON" "$@"
fi

for p in "$PROJECT_ROOT/venv/bin/python" "$PROJECT_ROOT/.venv/bin/python"; do
    if [ -x "$p" ]; then
        exec "$p" "$@"
    fi
done

if command -v python3 >/dev/null 2>&1; then
    exec python3 "$@"
fi

echo "plan-ops MCP launcher could not find an executable interpreter; tried IMPLEMENT_PLAN_PYTHON, ./venv, ./.venv, and python3 on PATH" >&2
exit 127
