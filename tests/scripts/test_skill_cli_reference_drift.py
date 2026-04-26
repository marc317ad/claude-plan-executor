"""Drift guard for the condensed SKILL.md plan_ops.py CLI reference.

The skill intentionally keeps one terse row per plan_ops.py subcommand and
delegates flag details to ``<subcommand> --help``. This test keeps that
condensed table honest by comparing it to the argparse registrations in
``plan_ops.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_OPS_PATH = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_ops.py"
SKILL_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md"
)

INTERNAL_SUBCOMMAND_PREFIX = "_cmd_internal_"


def _registered_argparse_subcommands() -> set[str]:
    """Return literal ``subparsers.add_parser("name", ...)`` registrations."""
    tree = ast.parse(PLAN_OPS_PATH.read_text(encoding="utf-8"))
    commands: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue

        command = node.args[0].value
        if not command.startswith(INTERNAL_SUBCOMMAND_PREFIX):
            commands.add(command)

    assert commands, "No argparse subcommands found in plan_ops.py"
    return commands


def _skill_cli_reference_subcommands() -> set[str]:
    text = SKILL_PATH.read_text(encoding="utf-8")
    match = re.search(
        r"^## plan_ops\.py CLI reference\n(?P<section>.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match, "SKILL.md is missing `## plan_ops.py CLI reference`"

    commands: set[str] = set()
    for line in match.group("section").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or cells[0] in {"Command", "---"}:
            continue

        commands.update(
            command
            for command in re.findall(r"`([^`]+)`", cells[0])
            if not command.startswith(INTERNAL_SUBCOMMAND_PREFIX)
        )

    assert commands, "No subcommands found in SKILL.md CLI reference table"
    return commands


def test_skill_cli_reference_matches_plan_ops_argparse_subcommands() -> None:
    argparse_commands = _registered_argparse_subcommands()
    skill_commands = _skill_cli_reference_subcommands()

    missing_from_skill = sorted(argparse_commands - skill_commands)
    extra_in_skill = sorted(skill_commands - argparse_commands)

    assert not missing_from_skill and not extra_in_skill, (
        "SKILL.md `## plan_ops.py CLI reference` drifted from "
        "plan_ops.py argparse registrations. "
        f"Missing from SKILL.md: {missing_from_skill!r}. "
        f"Extra in SKILL.md: {extra_in_skill!r}."
    )
