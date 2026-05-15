from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = (
    REPO_ROOT
    / "plugins"
    / "plan-executor"
    / "skills"
    / "implement-plan"
    / "CODEX_GEMINI_MCP.md"
)


def test_codex_gemini_bridge_is_mcp_first_and_no_claude_dispatch() -> None:
    text = BRIDGE.read_text(encoding="utf-8")

    for marker in [
        "plan_ops__preflight",
        "plan_ops__gates",
        "plan_ops__build_tasks",
        "plan_ops__review_route",
        "plan_ops__plan_review_route",
        "plan_codex_dispatch.py implement",
        "plan_gemini_dispatch.py review",
        "plan_gemini_dispatch.py plan-review",
        'implementer: "codex"',
        'reviewer: "gemini"',
    ]:
        assert marker in text

    assert "MCP tools directly" in text
    assert "stop and report the MCP registration issue" in text
    assert "plan_ops.py` CLI is only acceptable for diagnostics" in text

    for primitive in [
        "ToolSearch",
        "claude mcp list",
        "Claude `Agent`",
        "/reload-plugins",
        "plan_claude_dispatch.py",
    ]:
        lines = [line for line in text.splitlines() if primitive in line]
        assert lines, f"bridge must explicitly forbid {primitive}"
        assert all(
            "Do not use" in line or "Forbidden Claude Primitives" in line
            for line in lines
        ), f"{primitive} appears outside the forbidden-primitives guard: {lines!r}"
