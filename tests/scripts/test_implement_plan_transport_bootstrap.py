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


def test_skill_documents_canonical_claude_wrapper_dispatch_recipe() -> None:
    text = _skill_text()
    section = _section(text, "Claude wrapper dispatch recipe (canonical)")

    # MCP-mode recipe must build to a tmp path and route through claude_envelope_extract
    # with native payload — never piping the MCP builder response into Bash.
    required_fragments = [
        "PLAN_OPS_TRANSPORT=mcp",
        "plan_ops__build_claude_dispatch_input",
        '"output"',
        "output_written",
        "plan_claude_dispatch.py",
        "--input <tmp dispatch input path>",
        "plan_ops__claude_envelope_extract",
        '"payload"',
        "Never pipe MCP tool output directly into Bash stdin",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in section]
    assert not missing, (
        "SKILL.md canonical Claude-wrapper recipe is missing required fragments: "
        f"{missing!r}"
    )


def test_skill_qualifies_input_dash_pipes_as_cli_fallback_only() -> None:
    text = _skill_text()
    # Every literal mention of `plan_claude_dispatch.py run --input -` (the legacy
    # stdin-pipe shape) MUST sit inside a CLI-fallback qualified context. Drift
    # would re-introduce ad hoc shell pipes under MCP mode.
    needle = "plan_claude_dispatch.py run --input -"
    cursor = 0
    while True:
        idx = text.find(needle, cursor)
        if idx == -1:
            break
        # 200-char window before the hit must mention CLI fallback / cli-fallback.
        window = text[max(0, idx - 200) : idx + len(needle)]
        assert (
            "CLI fallback" in window
            or "CLI-fallback" in window
            or "cli-fallback" in window
        ), (
            "Unqualified `plan_claude_dispatch.py run --input -` mention at offset "
            f"{idx}; must be inside a CLI-fallback qualified context. Window: "
            f"{window!r}"
        )
        cursor = idx + len(needle)


def test_skill_routes_claude_wrapper_envelopes_through_claude_envelope_extract() -> None:
    text = _skill_text()
    # Canonical recipe must explicitly hand the wrapper envelope to
    # plan_ops__claude_envelope_extract; the §Dispatch error handling section
    # must keep the same routing contract.
    recipe = _section(text, "Claude wrapper dispatch recipe (canonical)")
    assert "plan_ops__claude_envelope_extract" in recipe
    dispatch_err = _section(text, "Dispatch error handling (Claude wrapper)")
    assert "plan_ops__claude_envelope_extract" in dispatch_err

    # Phase D bounded-remediation and D.2b role-swap rows must point at the
    # canonical recipe rather than ad hoc wrapper invocations.
    assert "Claude wrapper dispatch recipe (canonical)" in text
    # Locate the bounded-remediation row and assert it references the recipe.
    br_idx = text.find("`dispatch_bounded_remediation`")
    assert br_idx != -1
    br_row = text[br_idx : text.find("\n", br_idx + 1) if "\n" in text[br_idx:] else len(text)]
    assert "Claude wrapper dispatch recipe (canonical)" in br_row, (
        f"dispatch_bounded_remediation row no longer points at the recipe: {br_row!r}"
    )

    # D.2b role-swap prose must reference the recipe.
    role_swap_idx = text.find("Role-swap retry")
    assert role_swap_idx != -1
    role_swap_section = text[role_swap_idx : role_swap_idx + 800]
    assert "Claude wrapper dispatch recipe (canonical)" in role_swap_section


def test_skill_has_no_ad_hoc_claude_wrapper_dispatch_sites() -> None:
    text = _skill_text()
    invocation = "plan_claude_dispatch.py"
    cursor = 0
    violations: list[str] = []

    while True:
        idx = text.find(invocation, cursor)
        if idx == -1:
            break
        paragraph_start = text.rfind("\n\n", 0, idx)
        paragraph_end = text.find("\n\n", idx)
        paragraph = text[
            0 if paragraph_start == -1 else paragraph_start + 2:
            len(text) if paragraph_end == -1 else paragraph_end
        ]

        # Generic references to the wrapper binary are not dispatch recipes.
        if " run" not in paragraph and "run --input" not in paragraph:
            cursor = idx + len(invocation)
            continue

        routes_through_extractor = (
            "plan_ops__claude_envelope_extract" in paragraph
            or "claude-envelope-extract" in paragraph
        )
        points_at_recipe = "Claude wrapper dispatch recipe (canonical)" in paragraph
        cli_fallback_only = (
            "CLI fallback" in paragraph
            or "CLI-fallback" in paragraph
            or "cli-fallback" in paragraph
        )

        if not (routes_through_extractor or points_at_recipe or cli_fallback_only):
            violations.append(paragraph.strip())
        cursor = idx + len(invocation)

    assert not violations, (
        "SKILL.md has Claude-wrapper dispatch prose that does not route through "
        "claude_envelope_extract, point at the canonical recipe, or qualify "
        f"itself as CLI fallback: {violations!r}"
    )


def test_skill_does_not_pipe_mcp_builder_output_into_bash() -> None:
    text = _skill_text()
    # Forbid any MCP-mode prose that pipes plan_ops__build_claude_dispatch_input
    # output directly into Bash. Acceptable form: write to file via `output:"..."`,
    # then Bash reads the file.
    forbidden = [
        "plan_ops__build_claude_dispatch_input | ",
        "plan_ops__build_claude_dispatch_input ... | ",
        # JSON-RPC framing means MCP responses must never appear on a shell pipe.
        "MCP response into `plan_claude_dispatch.py",
    ]
    hits = [fragment for fragment in forbidden if fragment in text]
    assert not hits, (
        "SKILL.md still pipes MCP builder output directly into Bash: "
        f"{hits!r}"
    )
