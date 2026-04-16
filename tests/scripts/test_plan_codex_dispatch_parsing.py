"""Runtime parsing tests for scripts/plan_codex_dispatch.py.

Covers the wrapper's in-process helpers that the orchestrator relies on
to dispatch Codex-tier tasks:

  * ``normalize_task_id`` — CLI-input canonicalization with lenient lowercase
    handling and strict uppercase output (``^\\d{3}[A-Z]?$``).
  * ``parse_task_block`` — locates ``### TASK-NNN[A-Z]?:`` blocks in a plan
    and extracts metadata.

These supplement ``test_plan_codex_dispatch_schema.py`` (JSON schema
shape only) with real behavioral coverage of the suffixed task-id
contract decided in the plan-ops extension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "plan-executor" / "scripts"))

import plan_codex_dispatch  # noqa: E402


PLAN_WITH_SUFFIXED_TASKS = """# Plan: suffixed

**Status:** in-progress
**Base branch:** main

## Context

Some context prose.

## Tasks

### TASK-004A: Filter schedule by agent tier

- **Status:** open
- **Agent:** codex
- **Files:**
  - scripts/plan_ops.py
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [004]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -q`

**Description:**
Add a filter-schedule subcommand that returns only tasks matching the
requested agent tier. The leaf sits under the 004 stem group.

### TASK-004B: Batch next selection

- **Status:** open
- **Agent:** claude
- **Files:**
  - scripts/plan_ops.py
- **Dependencies:** [004A]
- **Test command:** `venv/bin/pytest tests/scripts/test_plan_ops.py -q`

**Description:**
Body for the 004B block — distinct from 004A, immediately adjacent.
"""


class TestWrapperNormalizeTaskId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", "001"),
            ("001", "001"),
            ("17", "017"),
            ("TASK-17", "017"),
            ("TASK-001", "001"),
            ("042", "042"),
        ],
    )
    def test_wrapper_normalize_task_id_plain(self, raw: str, expected: str) -> None:
        assert plan_codex_dispatch.normalize_task_id(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("004A", "004A"),
            ("TASK-004B", "004B"),
            ("4A", "004A"),
            ("017Z", "017Z"),
            ("TASK-000A", "000A"),
        ],
    )
    def test_wrapper_normalize_task_id_suffixed(self, raw: str, expected: str) -> None:
        assert plan_codex_dispatch.normalize_task_id(raw) == expected

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("4a", "004A"),
            ("task-4a", "004A"),
            ("task-004b", "004B"),
            ("004a", "004A"),
        ],
    )
    def test_wrapper_normalize_task_id_lowercase_cli(
        self, raw: str, expected: str
    ) -> None:
        """Lowercase letter-suffix on CLI input is accepted and uppercased."""
        assert plan_codex_dispatch.normalize_task_id(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["004AB", "004A1", "A001", "001-A", "1000A", "TASK-", ""],
    )
    def test_wrapper_normalize_task_id_rejects_multiletter(self, raw: str) -> None:
        with pytest.raises(ValueError):
            plan_codex_dispatch.normalize_task_id(raw)


class TestWrapperParseTaskBlock:
    def test_wrapper_parse_task_block_suffixed(self) -> None:
        block = plan_codex_dispatch.parse_task_block(PLAN_WITH_SUFFIXED_TASKS, "004A")
        assert block["task_id"] == "004A"
        assert block["title"] == "Filter schedule by agent tier"
        assert "scripts/plan_ops.py" in block["files"]
        assert "tests/scripts/test_plan_ops.py" in block["files"]
        assert block["dependencies"] == "[004]"
        assert "pytest tests/scripts/test_plan_ops.py" in block["test_command"]
        assert "filter-schedule subcommand" in block["description"]

    def test_wrapper_parse_task_block_suffixed_from_lowercase_cli(self) -> None:
        """Lowercase CLI input resolves to the uppercase-canonical header."""
        block = plan_codex_dispatch.parse_task_block(PLAN_WITH_SUFFIXED_TASKS, "4a")
        assert block["task_id"] == "004A"
        assert block["title"] == "Filter schedule by agent tier"

    def test_wrapper_parse_task_block_end_boundary_stops_at_next_task(self) -> None:
        """Parsing 004A must stop at `### TASK-004B:` and not absorb its body."""
        block = plan_codex_dispatch.parse_task_block(PLAN_WITH_SUFFIXED_TASKS, "004A")
        # 004B's description must not leak into 004A.
        assert "Body for the 004B block" not in block["description"]
        # 004A's description is still present.
        assert "filter-schedule subcommand" in block["description"]
        # 004A's files must not include 004B's.
        assert block["files"] == [
            "scripts/plan_ops.py",
            "tests/scripts/test_plan_ops.py",
        ]

    def test_wrapper_parse_task_block_missing_suffixed_raises(self) -> None:
        with pytest.raises(ValueError, match=r"TASK-004C not found in plan"):
            plan_codex_dispatch.parse_task_block(PLAN_WITH_SUFFIXED_TASKS, "004C")
