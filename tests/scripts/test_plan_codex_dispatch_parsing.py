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

    def test_wrapper_parse_task_block_strips_wrapping_backticks(self) -> None:
        """`test_command` must round-trip unwrapped. Plan markdown writes
        `- **Test command:** \\`cmd\\``; the wrapper hands the value to
        `subprocess.run(..., shell=True)`, where surviving backticks
        trigger shell command-substitution and exit 127. Mirrors the
        canonical unwrap in `plan_ops._parse_task_block`."""
        block = plan_codex_dispatch.parse_task_block(
            PLAN_WITH_SUFFIXED_TASKS, "004A"
        )
        assert block["test_command"] == (
            "venv/bin/pytest tests/scripts/test_plan_ops.py -q"
        )
        assert not block["test_command"].startswith("`")
        assert not block["test_command"].endswith("`")


# ---------------------------------------------------------------------------
# Files: bullet normalization (TASK-002 parser unification)
#
# The orchestrator (`plan_ops._normalize_files_entry`) and the wrapper
# (`plan_codex_dispatch.normalize_file_path`) have to agree byte-for-byte
# on what a `Files:` bullet reduces to, otherwise commit-safe scope
# checks reject commits that the wrapper allowed (or vice versa).
# Both helpers now delegate to `_plan_paths.normalize_files_entry`; this
# test class pins the wrapper-side surface.
# ---------------------------------------------------------------------------


_PROSE_LADEN_FILES_PLAN = """# Plan: prose-laden

## Context

Plan with prose-continuation Files: bullets to verify the wrapper's
unified normaliser drops the prose tail.

## Tasks

### TASK-001: Prose-laden bullets

- **Status:** pending
- **Priority:** P1
- **Files:**
  - `Makefile` -- add `audit` target wired to `plan_ops.py audit`
- **Dependencies:** []
- **Test command:** none

**Description:**
Backticked-prose-laden bullet must reduce to the bare path.
"""


class TestWrapperNormalizeFilePath:
    """Pin the wrapper's `normalize_file_path` (alias of the canonical
    `_plan_paths.normalize_files_entry`). >= 5 cases covering bare path,
    backticked path, em-dash-prose, annotation, and `:N-M` suffix forms.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Bare path (no markdown wrapping, no annotation).
            ("scripts/foo.py", "scripts/foo.py"),
            # Backticked path with no prose continuation.
            ("`scripts/foo.py`", "scripts/foo.py"),
            # Backticked path + em-dash + prose continuation. The leading
            # backtick capture must win so prose does not weld to the path.
            (
                "`Makefile` -- add `audit` target wired to `plan_ops.py audit`",
                "Makefile",
            ),
            # Annotation in trailing parens (single token).
            ("`scripts/foo.py` (modify)", "scripts/foo.py"),
            # Annotation plus prose continuation after an em dash.
            (
                "plugins/foo.py (create) — generated helper",
                "plugins/foo.py",
            ),
            # Annotation in trailing parens with embedded em-dash inside.
            (
                "tests/fixtures/decomposer_inputs/ "
                "(create — canonical + malformed markdown fixtures)",
                "tests/fixtures/decomposer_inputs/",
            ),
            # `:N-M` line range suffix.
            ("scripts/foo.py:10-20", "scripts/foo.py"),
            # `:N` single-line reference suffix.
            ("scripts/foo.py:42", "scripts/foo.py"),
        ],
    )
    def test_wrapper_normalize_file_path_pins_canonical_helper(
        self, raw: str, expected: str
    ) -> None:
        assert plan_codex_dispatch.normalize_file_path(raw) == expected

    def test_wrapper_parse_task_block_prose_laden_bullet_reduces_to_bare_path(
        self,
    ) -> None:
        """`parse_task_block` returns raw bullet items; the call sites in
        `render_implement_prompt` / `cmd_implement` / `cmd_review` then
        feed each item through `normalize_file_path`. Asserting the
        normalised form here pins the wrapper's contract end-to-end for a
        backticked-prose-laden bullet."""
        block = plan_codex_dispatch.parse_task_block(
            _PROSE_LADEN_FILES_PLAN, "001"
        )
        normalised = [
            plan_codex_dispatch.normalize_file_path(f) for f in block["files"]
        ]
        assert normalised == ["Makefile"], normalised


# ---------------------------------------------------------------------------
# Fenced multi-line test commands + nested acceptance criteria.
#
# The wrapper reads the child plan file directly (`cmd_implement` /
# `cmd_review`) and hands `test_command` to `run_test_command`, which runs it
# under `subprocess.run(..., shell=True)`. Before the parser-unification fix,
# the `\s*` after `:**` in `_extract_inline_field` spanned the newline, so a
# *standalone* `- **Test command:**` marker captured the next line — the
# ```` ```bash ```` fence opener — and the wrapper then exec'd ``` ```bash ```
# under the shell (nonzero exit, a misleading "test failure"). And
# `_extract_bullet_list` emitted every indented bullet as its own item, so a
# nested acceptance criterion's sub-conditions split into separate entries.
# These pin the wrapper to the canonical `plan_ops` behavior byte-for-byte.
# ---------------------------------------------------------------------------


_PLAN_FENCED_CMD_AND_NESTED_AC = """# Plan: fenced

## Context

Wrapper must read a fenced multi-line test command and nested acceptance
criteria identically to the orchestrator's plan_ops parser.

## Tasks

### TASK-010: Fenced command and nested criteria

- **Status:** pending
- **Priority:** high
- **Agent:** codex
- **Files:**
  - scripts/gen.py
- **Dependencies:** []
- **Test command:**
  ```bash
  venv/bin/python scripts/gen.py
  venv/bin/pytest tests/scripts/test_gen.py -q
  ```
- **Acceptance criteria:**
  - First criterion with sub-conditions:
    - sub-condition a
    - sub-condition b
  - Second top-level criterion

**Description:**
Body for the fenced-command task.
"""


class TestWrapperFencedCommandAndNestedAC:
    def test_extract_inline_field_standalone_marker_returns_empty(self) -> None:
        """A standalone `- **Test command:**` marker must NOT swallow the
        following ```` ```bash ```` line. With the horizontal-only whitespace
        fix the inline extractor returns "" so the caller falls through to the
        fenced-block extractor."""
        block = (
            "- **Test command:**\n"
            "  ```bash\n"
            "  venv/bin/pytest -q\n"
            "  ```\n"
        )
        assert plan_codex_dispatch._extract_inline_field(block, "Test command") == ""

    def test_extract_inline_field_still_reads_inline_value(self) -> None:
        """The fix must not regress the ordinary single-line form."""
        block = "- **Status:** open\n- **Priority:** high\n"
        assert plan_codex_dispatch._extract_inline_field(block, "Status") == "open"
        assert plan_codex_dispatch._extract_inline_field(block, "Priority") == "high"

    def test_extract_fenced_block_field_joins_with_ampersand(self) -> None:
        block = (
            "- **Test command:**\n"
            "  ```bash\n"
            "  venv/bin/python scripts/gen.py\n"
            "  venv/bin/pytest tests/scripts/test_gen.py -q\n"
            "  ```\n"
        )
        assert plan_codex_dispatch._extract_fenced_block_field(
            block, "Test command"
        ) == (
            "venv/bin/python scripts/gen.py && "
            "venv/bin/pytest tests/scripts/test_gen.py -q"
        )

    def test_extract_fenced_block_field_absent_for_inline_form(self) -> None:
        """Inline form has no fenced block beneath the marker → ""."""
        block = "- **Test command:** `venv/bin/pytest -q`\n"
        assert (
            plan_codex_dispatch._extract_fenced_block_field(block, "Test command")
            == ""
        )

    def test_extract_fenced_block_field_unterminated_returns_empty(self) -> None:
        block = "- **Test command:**\n  ```bash\n  venv/bin/pytest -q\n"
        assert (
            plan_codex_dispatch._extract_fenced_block_field(block, "Test command")
            == ""
        )

    def test_extract_bullet_list_folds_nested_subbullets(self) -> None:
        block = (
            "- **Acceptance criteria:**\n"
            "  - First criterion with sub-conditions:\n"
            "    - sub-condition a\n"
            "    - sub-condition b\n"
            "  - Second top-level criterion\n"
        )
        assert plan_codex_dispatch._extract_bullet_list(
            block, "Acceptance criteria"
        ) == [
            "First criterion with sub-conditions: sub-condition a sub-condition b",
            "Second top-level criterion",
        ]

    def test_parse_task_block_captures_fenced_test_command(self) -> None:
        block = plan_codex_dispatch.parse_task_block(
            _PLAN_FENCED_CMD_AND_NESTED_AC, "010"
        )
        assert block["test_command"] == (
            "venv/bin/python scripts/gen.py && "
            "venv/bin/pytest tests/scripts/test_gen.py -q"
        )
        # The original bug symptom: a captured ```` ```bash ```` opener that
        # would have exited nonzero under `shell=True`. Guard against it.
        assert not block["test_command"].startswith("`"), block["test_command"]

    def test_parse_task_block_folds_nested_acceptance_criteria(self) -> None:
        block = plan_codex_dispatch.parse_task_block(
            _PLAN_FENCED_CMD_AND_NESTED_AC, "010"
        )
        assert block["acceptance_criteria"] == [
            "First criterion with sub-conditions: sub-condition a sub-condition b",
            "Second top-level criterion",
        ]
        # The fenced command block must not leak into Files.
        assert block["files"] == ["scripts/gen.py"], block["files"]
