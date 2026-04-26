"""TASK-006: run-log event coverage for the Claude wrapper dispatch path.

Replays a 3-dispatch sequence (plan-analyst → plan-implementer →
plan-remediator) by piping wrapper envelopes through
``plan_ops.py claude-envelope-extract`` and emitting the
``claude_dispatch_{start,done,failed}`` lifecycle events via
``plan_ops.py log-event``. Asserts:

  * The new event names are first-class in ``ALLOWED_LOG_EVENTS``.
  * The replay produces exactly the expected event ordering and the
    payloads carry the wrapper status / result fields.
  * The pre-existing event vocabulary (``run_start``, ``implement_done``,
    ``review_done``, ``commit_done``, ``failed``, ``awaiting_user``)
    still appends without regression.
  * ``claude-envelope-extract`` normalizes ``ok`` and non-``ok``
    envelopes correctly across all three agents.
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
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

sys.path.insert(0, str(SCRIPTS_DIR))

import plan_ops  # noqa: E402


def _run(
    *args: str,
    cwd: Path | None = None,
    stdin: str | None = None,
) -> subprocess.CompletedProcess:
    cmd = [str(PY), str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else os.getcwd(),
        capture_output=True,
        text=True,
        input=stdin,
    )


def _parse_json(cp: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"stdout is not JSON:\nstdout={cp.stdout!r}\nstderr={cp.stderr!r}\nerr={e}"
        )


@pytest.fixture
def isolated_run_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox `_run_log.jsonl` under tmp_path; subprocess `_run` calls
    inherit cwd via `cwd=tmp_path` so plan_ops resolves the same path."""
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_dir)
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans_dir / "_run_log.jsonl")
    return plans_dir / "_run_log.jsonl"


# ---------------------------------------------------------------------------
# ALLOWED_LOG_EVENTS membership
# ---------------------------------------------------------------------------


class TestAllowedLogEvents:
    def test_claude_dispatch_events_are_first_class(self) -> None:
        for name in ("claude_dispatch_start", "claude_dispatch_done",
                     "claude_dispatch_failed"):
            assert name in plan_ops.ALLOWED_LOG_EVENTS, (
                f"{name!r} must be in ALLOWED_LOG_EVENTS"
            )

    def test_existing_events_unchanged(self) -> None:
        # Regression: TASK-006 must not drop pre-existing event names.
        for name in ("run_start", "implement_done", "review_done",
                     "commit_done", "failed", "awaiting_user"):
            assert name in plan_ops.ALLOWED_LOG_EVENTS


# ---------------------------------------------------------------------------
# claude-envelope-extract — normalization across the three agents
# ---------------------------------------------------------------------------


def _ok_analyst_envelope() -> dict:
    """Per-child classifier envelope: result is {agent, classification_reason}."""
    return {
        "schema_version": 1,
        "status": "ok",
        "status_reason": None,
        "agent": "plan-analyst",
        "model": "claude-sonnet",
        "session_id": "S-A1",
        "duration_ms": 1234,
        "cost_usd": 0.01,
        "tokens": {"input": 1000, "output": 200},
        "result": {"agent": "claude", "classification_reason": "trivial python edit"},
        "result_raw_truncated": None,
        "stderr_tail": None,
        "permission_denials": [],
        "scope": {},
        "trace": {"run_id": "R-1"},
        "error": None,
    }


def _ok_implementer_envelope() -> dict:
    return {
        "schema_version": 1,
        "status": "ok",
        "status_reason": None,
        "agent": "plan-implementer",
        "model": "claude-opus",
        "session_id": "S-I1",
        "duration_ms": 4242,
        "cost_usd": 0.05,
        "tokens": {"input": 5000, "output": 800},
        "result": {
            "outcome": "success",
            "report": {
                "files_changed": ["src/foo.py"],
                "plan_adaptations": [],
                "concerns_for_reviewer": [],
                "on_failure_revert": "n/a",
            },
        },
        "result_raw_truncated": None,
        "stderr_tail": None,
        "permission_denials": [],
        "scope": {
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
        },
        "trace": {"run_id": "R-1"},
        "error": None,
    }


def _failed_remediator_envelope() -> dict:
    """Wrapper transport failure: status=schema_invalid."""
    return {
        "schema_version": 1,
        "status": "schema_invalid",
        "status_reason": "result.outcome missing",
        "agent": "plan-remediator",
        "model": "claude-opus",
        "session_id": "S-R1",
        "duration_ms": 999,
        "cost_usd": 0.02,
        "tokens": {"input": 3000, "output": 50},
        "result": None,
        "result_raw_truncated": "{partial json...",
        "stderr_tail": "warning: schema mismatch",
        "permission_denials": [],
        "scope": {},
        "trace": {"run_id": "R-1"},
        "error": None,
    }


class TestClaudeEnvelopeExtract:
    def test_ok_analyst_classifier(self) -> None:
        cp = _run(
            "claude-envelope-extract", "--stdin",
            "--agent", "plan-analyst", "--json",
            stdin=json.dumps(_ok_analyst_envelope()),
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)
        assert out["status"] == "ok"
        # Per-child classifier seam: result has no `outcome`, so extract
        # returns None.
        assert out["outcome"] is None
        assert out["result"]["agent"] == "claude"
        assert out["scope_violation"] is False
        assert out["scope_misreport"] is False
        assert out["error"] is None

    def test_ok_implementer(self) -> None:
        cp = _run(
            "claude-envelope-extract", "--stdin",
            "--agent", "plan-implementer", "--json",
            stdin=json.dumps(_ok_implementer_envelope()),
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)
        assert out["status"] == "ok"
        assert out["outcome"] == "success"
        assert out["result"]["report"]["files_changed"] == ["src/foo.py"]
        assert out["scope_violation"] is False
        assert out["error"] is None

    def test_failed_remediator_collapses_to_malformed(self) -> None:
        cp = _run(
            "claude-envelope-extract", "--stdin",
            "--agent", "plan-remediator", "--json",
            stdin=json.dumps(_failed_remediator_envelope()),
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)
        assert out["status"] == "schema_invalid"
        # Universal collapse: any non-ok wrapper status -> outcome=malformed.
        assert out["outcome"] == "malformed"
        assert out["error"] is not None
        assert "status_reason=result.outcome missing" in out["error"]
        assert "result_raw_truncated={partial json..." in out["error"]
        assert "stderr_tail=warning: schema mismatch" in out["error"]

    def test_scope_violation_envelope(self) -> None:
        env = _ok_implementer_envelope()
        env["status"] = "scope_violation"
        env["status_reason"] = "wrote outside Files: list"
        env["result"] = None
        env["scope"] = {
            "scope_violation_detected": True,
            "scope_misreport_detected": True,
        }
        cp = _run(
            "claude-envelope-extract", "--stdin",
            "--agent", "plan-implementer", "--json",
            stdin=json.dumps(env),
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)
        assert out["status"] == "scope_violation"
        assert out["outcome"] == "malformed"
        assert out["scope_violation"] is True
        assert out["scope_misreport"] is True

    def test_rejects_unknown_agent(self) -> None:
        cp = _run(
            "claude-envelope-extract", "--stdin",
            "--agent", "code-reviewer", "--json",
            stdin=json.dumps(_ok_implementer_envelope()),
        )
        # argparse `choices=` rejects pre-handler.
        assert cp.returncode != 0

    def test_rejects_non_json_stdin(self) -> None:
        cp = _run(
            "claude-envelope-extract", "--stdin",
            "--agent", "plan-implementer", "--json",
            stdin="{not json",
        )
        assert cp.returncode != 0


# ---------------------------------------------------------------------------
# 3-dispatch replay — analyst -> implementer -> remediator
# ---------------------------------------------------------------------------


def _log_event(
    event: str,
    fields: dict,
    *,
    cwd: Path,
) -> subprocess.CompletedProcess:
    return _run(
        "log-event",
        "--event", event,
        "--fields-json", json.dumps(fields),
        "--json",
        cwd=cwd,
    )


class TestThreeDispatchReplay:
    def test_replay_event_ordering_and_payloads(
        self, isolated_run_log: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cwd = isolated_run_log.parent.parent.parent  # tmp_path
        # Open the run with run_start (existing event) so we exercise
        # the no-regression invariant.
        cp = _log_event(
            "run_start",
            {"run_id": "R-1", "plan_file": "sample.md", "mode": "execute"},
            cwd=cwd,
        )
        assert cp.returncode == 0, cp.stderr

        # 1. plan-analyst dispatch (ok).
        cp = _log_event(
            "claude_dispatch_start",
            {"agent": "plan-analyst", "run_id": "R-1", "plan_file": "child_001.md"},
            cwd=cwd,
        )
        assert cp.returncode == 0, cp.stderr
        cp = _log_event(
            "claude_dispatch_done",
            {
                "agent": "plan-analyst", "run_id": "R-1", "status": "ok",
                "duration_ms": 1234, "session_id": "S-A1",
            },
            cwd=cwd,
        )
        assert cp.returncode == 0

        # 2. plan-implementer dispatch (ok).
        cp = _log_event(
            "claude_dispatch_start",
            {"agent": "plan-implementer", "run_id": "R-1", "task_id": "001"},
            cwd=cwd,
        )
        assert cp.returncode == 0
        cp = _log_event(
            "claude_dispatch_done",
            {
                "agent": "plan-implementer", "run_id": "R-1", "status": "ok",
                "duration_ms": 4242, "session_id": "S-I1", "task_id": "001",
            },
            cwd=cwd,
        )
        assert cp.returncode == 0
        # The semantic event still fires alongside the lifecycle event.
        cp = _log_event(
            "implement_done",
            {"task_id": "001", "outcome": "success", "files_changed": ["src/foo.py"]},
            cwd=cwd,
        )
        assert cp.returncode == 0

        # 3. plan-remediator dispatch (transport failure).
        cp = _log_event(
            "claude_dispatch_start",
            {"agent": "plan-remediator", "run_id": "R-1", "task_id": "002"},
            cwd=cwd,
        )
        assert cp.returncode == 0
        cp = _log_event(
            "claude_dispatch_failed",
            {
                "agent": "plan-remediator", "run_id": "R-1",
                "status": "schema_invalid", "status_reason": "result.outcome missing",
                "task_id": "002", "stderr_tail": "warning: schema mismatch",
            },
            cwd=cwd,
        )
        assert cp.returncode == 0

        # Read back the run-log and assert ordering + payload shapes.
        lines = isolated_run_log.read_text(encoding="utf-8").splitlines()
        events = [json.loads(ln) for ln in lines]
        names = [ev["event"] for ev in events]
        assert names == [
            "run_start",
            "claude_dispatch_start",
            "claude_dispatch_done",
            "claude_dispatch_start",
            "claude_dispatch_done",
            "implement_done",
            "claude_dispatch_start",
            "claude_dispatch_failed",
        ]

        # Per-event payload assertions.
        analyst_start = events[1]
        assert analyst_start["agent"] == "plan-analyst"
        assert analyst_start["plan_file"] == "child_001.md"
        analyst_done = events[2]
        assert analyst_done["status"] == "ok"
        assert analyst_done["duration_ms"] == 1234

        implementer_done = events[4]
        assert implementer_done["agent"] == "plan-implementer"
        assert implementer_done["task_id"] == "001"
        assert implementer_done["status"] == "ok"

        remediator_failed = events[7]
        assert remediator_failed["status"] == "schema_invalid"
        assert remediator_failed["status_reason"] == "result.outcome missing"
        assert remediator_failed["agent"] == "plan-remediator"
        assert remediator_failed["task_id"] == "002"

        # Every event line carries a timestamp (existing invariant).
        for ev in events:
            assert "ts" in ev


# ---------------------------------------------------------------------------
# No-regression: existing event types still append after TASK-006.
# ---------------------------------------------------------------------------


class TestExistingEventsNoRegression:
    @pytest.mark.parametrize("event,fields", [
        ("run_start", {"run_id": "R", "plan_file": "x.md", "mode": "execute"}),
        ("implement_done", {"task_id": "001", "outcome": "success"}),
        ("review_done", {"task_id": "001", "reviewer": "claude", "verdict": "ship"}),
        ("commit_done", {"task_id": "001", "commit_sha": "abc"}),
        ("failed", {"task_id": "001", "stage": "implement", "reason": "x"}),
        ("awaiting_user", {"task_id": "001", "stage": "post_implement_failure"}),
    ])
    def test_event_appends(
        self,
        event: str,
        fields: dict,
        isolated_run_log: Path,
    ) -> None:
        cwd = isolated_run_log.parent.parent.parent
        cp = _log_event(event, fields, cwd=cwd)
        assert cp.returncode == 0, f"{event} rejected:\n{cp.stderr}"
        lines = isolated_run_log.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == event
