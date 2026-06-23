"""Tests for the ``ac_symbol_groundedness`` check in ``lint-plans``.

TASK-002 (PLAN_RUN_TELEMETRY_FOLLOWUPS_2026-05-15): a warning-severity check
that flags acceptance criteria referencing a code symbol which does not
literally appear in any of the task's declared Files. Catches AC drift at
plan-author time rather than at Phase D cross-review.

Three scenarios are covered:

  * negative case (from fixture) — symbol absent → finding present
  * positive case — symbol present in a declared file → no finding
  * warning severity — finding does not raise the lint exit code

The check shares the existing ``findings[]`` envelope (no new top-level
key); warning entries carry ``severity: "warning"`` so the existing
blocking-finding tests in ``test_plan_ops.py`` remain unaffected.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"
FIXTURES = REPO_ROOT / "tests" / "scripts" / "fixtures" / "lint_plans"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [str(PY), str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else os.getcwd(),
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_json(cp: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:  # pragma: no cover - debug aid
        raise AssertionError(
            f"stdout is not JSON:\nstdout={cp.stdout!r}\n"
            f"stderr={cp.stderr!r}\nerr={e}"
        )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "lint@test"], cwd=repo, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "lint"], cwd=repo, check=True
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)


@pytest.fixture()
def lint_ws(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    return {"repo": repo, "plans_dir": plans_dir}


def _lint(plans_dir: Path, repo: Path) -> subprocess.CompletedProcess:
    return _run(
        "lint-plans",
        "--plans-dir", str(plans_dir),
        "--git-dir", str(repo),
        "--json",
    )


def test_ac_symbol_groundedness_negative_case_flags_missing_symbol(
    lint_ws: dict,
) -> None:
    """Fixture plan names two symbols in AC; neither appears in declared file."""
    ws = lint_ws
    repo, plans_dir = ws["repo"], ws["plans_dir"]
    # Copy the fixture plan into the temp plans dir.
    fixture_text = (
        FIXTURES / "ac_names_missing_symbol.md"
    ).read_text(encoding="utf-8")
    (plans_dir / "plan.md").write_text(fixture_text, encoding="utf-8")
    # Create the declared file with unrelated content (no matching symbols).
    src = repo / "src"
    src.mkdir()
    (src / "widget.py").write_text(
        "def unrelated_helper():\n    return 1\n", encoding="utf-8"
    )

    cp = _lint(plans_dir, repo)
    body = _parse_json(cp)
    codes = [f["code"] for f in body["findings"]]
    assert codes.count("ac_symbol_groundedness") == 2, body
    flagged_syms = {
        f["message"].split("`")[1]
        for f in body["findings"]
        if f["code"] == "ac_symbol_groundedness"
    }
    assert flagged_syms == {
        "compute_total_orphan_value",
        "MISSING_SENTINEL_CONST",
    }, body
    for f in body["findings"]:
        if f["code"] == "ac_symbol_groundedness":
            assert f["severity"] == "warning", f
            assert f["task_id"] == "101"


def test_ac_symbol_groundedness_positive_case_no_finding(
    lint_ws: dict,
) -> None:
    """When the AC's named symbol IS present in a declared file → no finding."""
    ws = lint_ws
    repo, plans_dir = ws["repo"], ws["plans_dir"]
    plan_body = (
        "# Plan: positive case\n\n"
        "**Base branch:** main\n\n"
        "## Tasks\n\n"
        "### TASK-200: helper exists\n\n"
        "- **Status:** pending\n"
        "- **Files:**\n"
        "  - src/grounded.py\n"
        "- **Dependencies:** none\n"
        "- **Acceptance criteria:**\n"
        "  - The helper `compute_grounded_value` returns 0 for empty inputs.\n"
        "  - The constant `GROUNDED_SENTINEL` is exposed at module level.\n"
    )
    (plans_dir / "plan.md").write_text(plan_body, encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    (src / "grounded.py").write_text(
        "GROUNDED_SENTINEL = object()\n\n"
        "def compute_grounded_value(items):\n"
        "    return 0 if not items else sum(items)\n",
        encoding="utf-8",
    )

    cp = _lint(plans_dir, repo)
    body = _parse_json(cp)
    codes = [f["code"] for f in body["findings"]]
    assert "ac_symbol_groundedness" not in codes, body


def test_ac_symbol_groundedness_warning_does_not_block_exit_code(
    lint_ws: dict,
) -> None:
    """Warning-severity findings must keep the lint exit code at 0."""
    ws = lint_ws
    repo, plans_dir = ws["repo"], ws["plans_dir"]
    fixture_text = (
        FIXTURES / "ac_names_missing_symbol.md"
    ).read_text(encoding="utf-8")
    (plans_dir / "plan.md").write_text(fixture_text, encoding="utf-8")
    src = repo / "src"
    src.mkdir()
    (src / "widget.py").write_text("# empty\n", encoding="utf-8")

    cp = _lint(plans_dir, repo)
    body = _parse_json(cp)
    # The fixture's task status is `pending`, so no blocking findings are
    # produced — only the two ac_symbol_groundedness warnings.
    assert all(
        f.get("severity") == "warning" for f in body["findings"]
    ), body
    assert cp.returncode == 0, (
        f"warning-only run must exit 0; got {cp.returncode!r} "
        f"stderr={cp.stderr!r}"
    )
    # And the new check appears in the existing `findings[]` array — not
    # under any new top-level key.
    assert set(body.keys()) == {"scanned", "done_tasks", "findings"}, body


def test_test_command_shell_hazard_flags_unbalanced_paren(lint_ws: dict) -> None:
    """BUG-158: a Test command whose normalized form still carries a shell
    hazard (here an unbalanced paren that normalization cannot strip) is
    flagged warning-severity, without raising the lint exit code."""
    ws = lint_ws
    repo, plans_dir = ws["repo"], ws["plans_dir"]
    plan_body = (
        "# Plan: hazard\n\n"
        "**Base branch:** main\n\n"
        "## Tasks\n\n"
        "### TASK-300: hazardous test command\n\n"
        "- **Status:** pending\n"
        "- **Files:**\n"
        "  - src/x.py\n"
        "- **Dependencies:** none\n"
        "- **Test command:** pytest tests/x.py (smoke\n"
        "- **Acceptance criteria:**\n"
        "  - works\n"
    )
    (plans_dir / "plan.md").write_text(plan_body, encoding="utf-8")
    cp = _lint(plans_dir, repo)
    body = _parse_json(cp)
    haz = [
        f for f in body["findings"]
        if f["code"] == "test_command_shell_hazard"
    ]
    assert len(haz) == 1, body
    assert haz[0]["severity"] == "warning", haz
    assert haz[0]["task_id"] == "300", haz
    assert cp.returncode == 0, (cp.returncode, cp.stderr)


def test_test_command_shell_hazard_not_flagged_when_normalizable(
    lint_ws: dict,
) -> None:
    """The common `cmd` (annotation) shape normalizes cleanly to a bare
    command, so it must NOT be flagged — only genuinely unfixable shapes are."""
    ws = lint_ws
    repo, plans_dir = ws["repo"], ws["plans_dir"]
    plan_body = (
        "# Plan: clean\n\n"
        "**Base branch:** main\n\n"
        "## Tasks\n\n"
        "### TASK-301: normalizable test command\n\n"
        "- **Status:** pending\n"
        "- **Files:**\n"
        "  - src/x.py\n"
        "- **Dependencies:** none\n"
        "- **Test command:** `venv/bin/pytest tests/x.py -q` (smoke subset)\n"
        "- **Acceptance criteria:**\n"
        "  - works\n"
    )
    (plans_dir / "plan.md").write_text(plan_body, encoding="utf-8")
    cp = _lint(plans_dir, repo)
    body = _parse_json(cp)
    codes = [f["code"] for f in body["findings"]]
    assert "test_command_shell_hazard" not in codes, body
