from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_plan_ops import _parse_json, _run

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
DISPATCH = SCRIPTS_DIR / "plan_codex_dispatch.py"

spec = importlib.util.spec_from_file_location("plan_codex_dispatch", DISPATCH)
assert spec is not None and spec.loader is not None
plan_codex_dispatch = importlib.util.module_from_spec(spec)
sys.modules["plan_codex_dispatch"] = plan_codex_dispatch
spec.loader.exec_module(plan_codex_dispatch)


def test_deferred_marker_is_not_invoked_by_shell(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(*args, **kwargs):
        calls.append(args[0])
        raise AssertionError("deferred marker must not invoke subprocess.run")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = plan_codex_dispatch.run_test_command(
        "deferred (TASK-009) exercised by sibling", str(tmp_path),
    )

    assert result["result"] == "deferred"
    assert result["deferred_to"] == "009"
    assert result["note"] == "exercised by sibling"
    assert calls == []


def test_malformed_deferred_marker_fails_closed_without_shell(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("malformed deferred marker must not run shell"),
        ),
    )

    result = plan_codex_dispatch.run_test_command("deferred", str(tmp_path))

    assert result["result"] == "failed"
    assert result["attempts"] == 0
    assert "expected `deferred (TASK-NNN)`" in result["stderr"]


def test_real_shell_test_failure_still_fails(tmp_path: Path) -> None:
    result = plan_codex_dispatch.run_test_command("false", str(tmp_path), max_attempts=1)

    assert result["result"] == "failed"
    assert result["exit_code"] == 1


def test_auto_validate_deferred_records_run_log_event(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "docs" / "plans").mkdir(parents=True)
    envelope = {
        "task_id": "008",
        "subcommand": "implement",
        "outcome": "failure",
        "cause": "independent_test_run_failed",
    }
    env_file = repo / "envelope.json"
    env_file.write_text(json.dumps(envelope), encoding="utf-8")

    cp = _run(
        "auto-validate-divergence",
        "--envelope-file", str(env_file),
        "--test-command", "deferred (TASK-009) sibling owns e2e",
        "--repo-root", str(repo),
        "--run-id", "R-DEFER",
        "--task-id", "008",
        "--json",
        cwd=repo,
    )

    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    body = _parse_json(cp)
    assert body["target_test"]["result"] == "deferred"
    assert body["target_test"]["deferred_to"] == "009"
    log_path = repo / "docs" / "plans" / "_run_log.jsonl"
    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[-1]["event"] == "test_deferred"
    assert events[-1]["task_id"] == "008"
    assert events[-1]["deferred_to"] == "009"
    assert events[-1]["note"] == "sibling owns e2e"


def test_auto_validate_malformed_deferred_marker_errors(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    envelope = {
        "task_id": "008",
        "subcommand": "implement",
        "outcome": "failure",
        "cause": "independent_test_run_failed",
    }
    env_file = repo / "envelope.json"
    env_file.write_text(json.dumps(envelope), encoding="utf-8")

    cp = _run(
        "auto-validate-divergence",
        "--envelope-file", str(env_file),
        "--test-command", "deferred",
        "--repo-root", str(repo),
        "--run-id", "R-DEFER",
        "--task-id", "008",
        "--json",
        cwd=repo,
    )

    assert cp.returncode == 1
    body = _parse_json(cp)
    assert "expected `deferred (TASK-NNN)`" in body["error"]
