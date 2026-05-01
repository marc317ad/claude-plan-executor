#!/usr/bin/env python3
"""One-shot SKILL.md plan_ops Bash-to-MCP migration helper.

The helper intentionally handles only the simple orchestrator-facing shape:

    $PYTHON ".../plan_ops.py" <subcommand> [--flag value ...] [--json]

It rewrites those lines to:

    Tool: plan_ops__<subcommand_with_underscores> with input {...}

Everything multiline, bootstrap-python, stdin-piped, or wrapper-internal is
reported for the manual Phase pass instead of being silently left on the old
path.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_SKILL = Path("plugins/plan-executor/skills/implement-plan/SKILL.md")

# Protected wrapper-internal subprocess walkthroughs. These anchors name ranges
# where Bash remains the transport because the wrapper subprocess, not the
# orchestrator, owns the shell command.
PROTECTED_ANCHOR_RANGES = [
    (
        "### Step 2 — Per-child classifier fan-out (conditional)",
        "### Step 3 — Apply filters and per-task overrides",
        "Phase 1 classifier wrapper fan-out pseudo-syntax",
    ),
    (
        "### Phase B — Implement (parallel, one message)",
        "*Codex envelope (JSON from wrapper):*",
        "Phase B wrapper dispatch walkthrough",
    ),
    (
        "*Phase D-Codex path (`claude_only=false`, Claude-implemented → Codex review):*",
        "Parse `parsed.verdict ∈ {clean, minor-findings, needs-rework}`.",
        "Phase D Codex wrapper review walkthrough",
    ),
]

BOOTSTRAP_PLAN_OPS_RE = re.compile(r"\bpython3\b[^\n`]*plan_ops\.py[^\n`]*")
SIMPLE_PLAN_OPS_RE = re.compile(
    r"(?P<prefix>^|\s|`)[$]PYTHON\s+"
    r"(?P<script>(?:\"[^\"]*plan_ops\.py\"|'[^']*plan_ops\.py'|\S*plan_ops\.py))\s+"
    r"(?P<rest>[^\n`|&;<>]*?)(?P<suffix>`?\s*$)"
    ,
    re.MULTILINE,
)
PLAN_OPS_INLINE_RE = re.compile(r"[$]PYTHON\s+plan_ops\.py\s+([a-z0-9-]+)\s+([^`.\n]+)")


@dataclass(frozen=True)
class ProtectedRange:
    start: int
    end: int
    label: str


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for idx, char in enumerate(text):
        if char == "\n":
            starts.append(idx + 1)
    return starts


def _line_number(starts: list[int], offset: int) -> int:
    lo = 0
    hi = len(starts)
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= offset:
            lo = mid
        else:
            hi = mid
    return lo + 1


def _protected_ranges(text: str) -> list[ProtectedRange]:
    ranges: list[ProtectedRange] = []
    for start_anchor, end_anchor, label in PROTECTED_ANCHOR_RANGES:
        start = text.find(start_anchor)
        if start == -1:
            continue
        end = text.find(end_anchor, start + len(start_anchor))
        if end == -1:
            end = len(text)
        ranges.append(ProtectedRange(start=start, end=end, label=label))
    return ranges


def _is_protected(offset: int, ranges: Iterable[ProtectedRange]) -> str | None:
    for protected in ranges:
        if protected.start <= offset < protected.end:
            return protected.label
    return None


def _coerce_value(raw: str) -> object:
    value = raw.strip()
    if value in {"true", "false"}:
        return value == "true"
    if value.startswith("{") or value.startswith("["):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _flag_key(flag: str) -> str:
    return flag.removeprefix("--").replace("-", "_")


def _parse_input(rest: str) -> tuple[str, dict[str, object]]:
    tokens = shlex.split(rest)
    if not tokens:
        raise ValueError("missing subcommand")
    subcommand = tokens[0]
    input_obj: dict[str, object] = {}
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token == "--json":
            idx += 1
            continue
        if not token.startswith("--"):
            input_obj.setdefault("_args", []).append(token)
            idx += 1
            continue
        key = _flag_key(token)
        if idx + 1 >= len(tokens) or tokens[idx + 1].startswith("--"):
            input_obj[key] = True
            idx += 1
            continue
        input_obj[key] = _coerce_value(tokens[idx + 1])
        idx += 2
    return subcommand, input_obj


def _tool_line(subcommand: str, input_obj: dict[str, object]) -> str:
    tool = f"plan_ops__{subcommand.replace('-', '_')}"
    return f"Tool: {tool} with input {json.dumps(input_obj, sort_keys=True)}"


def migrate(text: str) -> tuple[str, list[str]]:
    protected_ranges = _protected_ranges(text)
    starts = _line_starts(text)
    reports: list[str] = []

    def replace_simple(match: re.Match[str]) -> str:
        protected = _is_protected(match.start(), protected_ranges)
        line = _line_number(starts, match.start())
        original = match.group(0)
        if protected:
            reports.append(f"manual_review line {line}: protected {protected}: {original.strip()}")
            return original
        rest = match.group("rest").strip()
        if "\\" in rest or not rest:
            reports.append(f"manual_review line {line}: multiline/simple-regex-skip: {original.strip()}")
            return original
        try:
            subcommand, input_obj = _parse_input(rest)
        except ValueError as exc:
            reports.append(f"manual_review line {line}: parse_error {exc}: {original.strip()}")
            return original
        replacement = _tool_line(subcommand, input_obj)
        reports.append(f"rewrote line {line}: {subcommand} -> plan_ops__{subcommand.replace('-', '_')}")
        prefix = match.group("prefix")
        if prefix == "`":
            return f"`{replacement}`"
        return f"{prefix}{replacement}"

    migrated = SIMPLE_PLAN_OPS_RE.sub(replace_simple, text)

    # Report known non-simple old-path forms for the manual Phase pass.
    starts_after = _line_starts(migrated)
    protected_after = _protected_ranges(migrated)
    for regex, label in (
        (BOOTSTRAP_PLAN_OPS_RE, "bootstrap python3"),
        (PLAN_OPS_INLINE_RE, "inline shorthand"),
    ):
        for match in regex.finditer(migrated):
            protected = _is_protected(match.start(), protected_after)
            line = _line_number(starts_after, match.start())
            reports.append(
                f"manual_review line {line}: {label}"
                + (f" protected {protected}" if protected else "")
                + f": {match.group(0).strip()}"
            )

    return migrated, reports


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skill", nargs="?", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--apply", action="store_true", help="write the migrated SKILL.md")
    args = parser.parse_args()

    text = args.skill.read_text(encoding="utf-8")
    migrated, reports = migrate(text)
    for report in reports:
        print(report)
    if args.apply and migrated != text:
        args.skill.write_text(migrated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
