"""Regression tests for the /implement-plan plan-ops transport bootstrap."""

from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md"
)
MCP_SCHEMA_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "schemas" / "mcp"


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


def test_skill_fallback_discipline_returns_to_mcp_after_single_tool_failure() -> None:
    text = _skill_text()
    bootstrap = _section(text, "Plan-ops transport bootstrap")

    assert "CLI fallback for that call only" in bootstrap
    assert "return to MCP for the next plan operation" in bootstrap
    assert "keep the same `PLAN_OPS_TRANSPORT` for the run unless" not in bootstrap


def test_skill_bans_inline_python_for_envelope_parsing() -> None:
    text = _skill_text()
    command_idioms = _section(text, "Command idioms")

    assert "Never write inline Python for envelope parsing" in command_idioms
    assert "plan_ops__claude_envelope_extract" in command_idioms
    assert "claude-envelope-extract" in command_idioms


def test_skill_mcp_payload_examples_do_not_use_stdin() -> None:
    text = _skill_text()

    expected_examples = {
        "plan_ops__claude_envelope_extract": '"payload": <envelope>',
        "plan_ops__write_schedule": '"payload": <schedule_json>',
        "plan_ops__parse_plan_review_triage_report": '"payload": <report>',
        "plan_ops__reconcile_batch": '"payload": <envelopes_json>',
    }
    for tool_name, payload_fragment in expected_examples.items():
        assert tool_name in text
        assert payload_fragment in text

    forbidden_examples = [
        '"stdin": <envelope>',
        '"stdin": <schedule_json>',
        '"stdin": <report>',
        '"stdin": <envelopes_json>',
    ]
    present = [fragment for fragment in forbidden_examples if fragment in text]
    assert not present, f"SKILL.md MCP examples still use stdin: {present!r}"

    index = json.loads((MCP_SCHEMA_DIR / "_index.json").read_text(encoding="utf-8"))
    stale_examples = []
    for tool_name, entry in index["tools"].items():
        schema = json.loads((MCP_SCHEMA_DIR / entry["input_schema"]).read_text(encoding="utf-8"))
        if "payload" not in schema.get("properties", {}):
            continue
        if tool_name not in text:
            continue
        pattern = re.compile(
            rf"{re.escape(tool_name)}\s+with input \{{[^}}]*\"stdin\"",
            re.DOTALL,
        )
        if pattern.search(text):
            stale_examples.append(tool_name)

    assert not stale_examples, (
        "SKILL.md MCP examples for payload-bearing tools still use stdin: "
        f"{stale_examples!r}"
    )
