"""Regression tests for the /implement-plan plan-ops transport bootstrap."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md"
)


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = text.find(marker)
    assert start != -1, f"SKILL.md is missing section {heading!r}"
    next_heading = text.find("\n## ", start + len(marker))
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


def test_skill_documents_plan_ops_transport_bootstrap() -> None:
    text = _skill_text()
    section = _section(text, "Plan-ops transport bootstrap")

    required_fragments = [
        "ToolSearch",
        "plan_ops__preflight",
        "plan_ops__gates",
        "claude mcp list",
        "plugin:plan-executor:plan-ops",
        "PLAN_OPS_TRANSPORT=mcp",
        "PLAN_OPS_TRANSPORT=cli-fallback",
        '${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py',
        "<subcommand> ... --json",
        "mcp_unavailable",
        "write inline Python",
        "parse plan data ad hoc",
        "bypass schemas",
        "shell around plan operations",
    ]

    missing = [fragment for fragment in required_fragments if fragment not in section]
    assert not missing, (
        "SKILL.md transport bootstrap is missing required safety concepts: "
        f"{missing!r}"
    )


def test_skill_rules_use_active_transport_not_mcp_only_contract() -> None:
    text = _skill_text()
    command_idioms = _section(text, "Command idioms")
    rules = _section(text, "Rules")

    assert "active plan-ops transport" in command_idioms
    assert "active plan-ops transport" in rules
    assert "MCP `plan_ops__*` when visible" in rules
    assert "canonical `plan_ops.py` CLI fallback" in rules
    assert "Never write inline Python for plan ops." in rules
    assert "Use MCP `plan_ops__*` tools." not in rules
