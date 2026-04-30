"""Raw CLI baseline fixtures for the plan_ops pure-core codemod.

The fixtures in ``fixtures/plan_ops_cli_baseline`` are byte images of the
pre-codemod CLI surface: stdin, stdout, stderr, and exit code. The subprocess
call intentionally uses ``[sys.executable, "plan_ops.py", *argv]`` so the
future codemod gate exercises the same entrypoint shape as direct CLI callers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "plan_ops_cli_baseline"


SCHEDULE_STDIN = """{
  "outcome": "valid",
  "tasks": [
    {
      "id": "001",
      "agent": "codex",
      "title": "one",
      "files": [
        "src/one.py"
      ],
      "dependencies": [],
      "plan_file": "TASK-001_one.md"
    },
    {
      "id": "002",
      "agent": "claude",
      "title": "two",
      "files": [
        "src/two.py"
      ],
      "dependencies": [
        "001"
      ],
      "plan_file": "TASK-002_two.md"
    }
  ],
  "batches": [
    {
      "index": 1,
      "task_ids": [
        "001"
      ],
      "file_locks": [
        "src/one.py"
      ]
    },
    {
      "index": 2,
      "task_ids": [
        "002"
      ],
      "file_locks": [
        "src/two.py"
      ]
    }
  ],
  "gaps": [],
  "risks": []
}
"""

BCDI_PLAN = """# Plan: baseline

### TASK-001: analyst fixture

- **Status:** pending
- **Priority:** high
- **Files:** []
- **Dependencies:** []
- **Test command:** `none`
- **Acceptance criteria:**
  - Fixture only.
"""

NON_APPLICABLE_ENVELOPE = """{
  "task_id": "027C",
  "subcommand": "implement",
  "outcome": "failure",
  "cause": "scope_misreport",
  "sandbox_test_stdout": "unused",
  "sandbox_test_stderr": "unused",
  "sandbox_test_command": "pytest -q tests/foo.py",
  "sandbox_test_exit_code": 1,
  "sandbox_test_attempt_count": 2
}
"""


@dataclass(frozen=True)
class Case:
    name: str
    argv: tuple[str, ...]
    stdin: str = ""
    setup: str = "default"


CASES: tuple[Case, ...] = (
    Case("normalize_task_id__happy_json", ("normalize-task-id", "--id", "1", "--json")),
    Case("normalize_task_id__happy_text", ("normalize-task-id", "--id", "1")),
    Case("normalize_task_id__bogus_json", ("normalize-task-id", "--id", "bogus", "--json")),
    Case("normalize_task_id__bogus_text", ("normalize-task-id", "--id", "bogus")),
    Case("release_lock__no_lock_file_json", ("release-lock", "--plan-file", "{PLAN}", "--run-id", "R1", "--json")),
    Case("release_lock__run_id_mismatch_json", ("release-lock", "--plan-file", "{PLAN}", "--run-id", "R2", "--json"), setup="lock_mismatch"),
    Case("acquire_lock__empty_run_id_json", ("acquire-lock", "--plan-file", "{PLAN}", "--run-id", "", "--json")),
    Case("gates__list_json", ("gates", "--list", "--json")),
    Case("gates__list_text", ("gates", "--list")),
    Case(
        "gates__certify_execute_missing_run_id_json",
        ("gates", "--certify", "--mode", "execute", "--plan-file", "{PLAN}", "--schedule-file", "{SCHEDULE}", "--json"),
    ),
    Case("parse_schedule__stdin_happy_json", ("parse-schedule", "--stdin", "--json"), stdin=SCHEDULE_STDIN),
    Case("parse_schedule__stdin_happy_text", ("parse-schedule", "--stdin"), stdin=SCHEDULE_STDIN),
    Case("filter_schedule__stdin_happy_json", ("filter-schedule", "--stdin", "--task-ids", "2", "--json"), stdin=SCHEDULE_STDIN),
    Case("filter_schedule__stdin_happy_text", ("filter-schedule", "--stdin", "--task-ids", "2"), stdin=SCHEDULE_STDIN),
    Case(
        "build_claude_dispatch_input__output_dash_json",
        (
            "build-claude-dispatch-input",
            "--plan-file",
            "{BCDI_PLAN}",
            "--task-id",
            "001",
            "--variant",
            "analyst",
            "--repo-root",
            "{REPO_ROOT}",
            "--output",
            "-",
        ),
        setup="bcdi",
    ),
    Case(
        "commit_task__dismissed_ids_non_integer_json",
        (
            "commit-task",
            "--plan-file",
            "{PLAN}",
            "--task-id",
            "001",
            "--run-id",
            "R1",
            "--files",
            "src/foo.py",
            "--title",
            "t",
            "--diff-summary",
            "d",
            "--reviewer",
            "none",
            "--reviewer-verdict",
            "",
            "--narrow-remediation-tag",
            "--dismissed-finding-ids",
            "1,x",
            "--json",
        ),
    ),
    Case(
        "auto_validate_divergence__stdin_non_applicable_json",
        (
            "auto-validate-divergence",
            "--test-command",
            "true",
            "--repo-root",
            "{SANDBOX}",
            "--run-id",
            "R-SKIP",
            "--json",
        ),
        stdin=NON_APPLICABLE_ENVELOPE,
    ),
)


def _fixture_path(case_name: str, suffix: str) -> Path:
    return FIXTURE_DIR / f"{case_name}.{suffix}.txt"


def _write_fixture(case_name: str, suffix: str, text: str) -> None:
    path = _fixture_path(case_name, suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_fixture(case_name: str, suffix: str) -> str:
    return _fixture_path(case_name, suffix).read_text(encoding="utf-8")


def _prepare_sandbox(tmp_path: Path, case: Case) -> dict[str, str]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "plan_ops.py").symlink_to(SCRIPT)
    (scripts / "_plan_paths.py").symlink_to(SCRIPTS_DIR / "_plan_paths.py")

    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "plan-executor.json").write_text(
        json.dumps({"plan_dir": "docs/plans"}) + "\n",
        encoding="utf-8",
    )

    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n", encoding="utf-8")
    schedule = tmp_path / "schedule.json"
    schedule.write_text(SCHEDULE_STDIN, encoding="utf-8")
    bcdi_plan = tmp_path / "TASK-001_analyst.md"
    if case.setup == "bcdi":
        bcdi_plan.write_text(BCDI_PLAN, encoding="utf-8")
    if case.setup == "lock_mismatch":
        lock = {
            str(plan.resolve()): {
                "run_id": "R1",
                "acquired_at": "2026-04-30T00:00:00+00:00",
            }
        }
        (plan_dir / "_run_lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")

    return {
        "SANDBOX": str(tmp_path.resolve()),
        "SCRIPTS": str(scripts.resolve()),
        "PLAN": str(plan.resolve()),
        "SCHEDULE": str(schedule.resolve()),
        "BCDI_PLAN": str(bcdi_plan.resolve()),
        "REPO_ROOT": str(REPO_ROOT.resolve()),
    }


def _expand(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _collapse(text: str, values: dict[str, str]) -> str:
    for key in ("SANDBOX", "SCRIPTS", "PLAN", "SCHEDULE", "BCDI_PLAN", "REPO_ROOT"):
        text = text.replace(values[key], "{" + key + "}")
    return text


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_plan_ops_cli_baseline(case: Case, tmp_path: Path) -> None:
    values = _prepare_sandbox(tmp_path, case)
    argv = tuple(_expand(part, values) for part in case.argv)
    stdin_text = _expand(case.stdin, values)

    proc = subprocess.run(
        [sys.executable, "plan_ops.py", *argv],
        input=stdin_text,
        cwd=values["SCRIPTS"],
        capture_output=True,
        text=True,
    )

    if os.environ.get("PLAN_OPS_UPDATE_CLI_BASELINE") == "1":
        _write_fixture(case.name, "stdin", _collapse(stdin_text, values))
        _write_fixture(case.name, "stdout", _collapse(proc.stdout, values))
        _write_fixture(case.name, "stderr", _collapse(proc.stderr, values))
        _write_fixture(case.name, "exit", f"{proc.returncode}\n")

    assert stdin_text == _expand(_read_fixture(case.name, "stdin"), values)
    assert proc.stdout == _expand(_read_fixture(case.name, "stdout"), values)
    assert proc.stderr == _expand(_read_fixture(case.name, "stderr"), values)
    assert f"{proc.returncode}\n" == _read_fixture(case.name, "exit")
