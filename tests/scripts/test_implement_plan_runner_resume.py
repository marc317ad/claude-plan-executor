from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN_PATH = SCRIPT_DIR / "implement_plan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan_resume", IMPLEMENT_PLAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


class FakeFacade:
    def __init__(self, *, acquired: bool = True, run_id: str = "RUN-007") -> None:
        self.acquired = acquired
        self.run_id = run_id
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []
        self.log_calls: list[dict[str, Any]] = []

    def acquire_lock(self, **payload: Any) -> dict[str, Any]:
        self.acquire_calls.append(dict(payload))
        if payload["run_id"] != self.run_id:
            return {"acquired": False, "conflict_run_id": self.run_id}
        return {"acquired": self.acquired}

    def release_lock(self, **payload: Any) -> dict[str, Any]:
        self.release_calls.append(dict(payload))
        return {"released": True}

    def log_event(self, **payload: Any) -> dict[str, Any]:
        self.log_calls.append(dict(payload))
        return {"ok": True}


def _plan(tmp_path: Path) -> Path:
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n## TASK-001: Task\n", encoding="utf-8")
    return plan


def _write_schedule(tmp_path: Path) -> Path:
    schedule = tmp_path / "plan.schedule.json"
    schedule.write_text(
        json.dumps(
            {
                "tasks": [{"id": "001", "files": ["src/app.py"], "dependencies": []}],
                "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["src/app.py"]}],
                "state": {
                    "done": [],
                    "failed": [],
                    "blocked": [],
                    "locked_files": [],
                    "committed": [],
                    "review_notes": {},
                    "retries_used": {},
                },
            }
        ),
        encoding="utf-8",
    )
    return schedule


def _assignment_payload() -> dict[str, Any]:
    config = MODULE.RunnerConfig(
        plan="unused",
        assignments=(MODULE.TaskAssignment(task_id="001", provider="codex"),),
        reviewers=("gemini",),
        plan_reviewer=None,
    )
    return MODULE.resolve_assignments(
        [{"id": "001", "files": ["src/app.py"]}],
        config,
        MODULE.DEFAULT_PROVIDER_CAPABILITIES,
    ).as_dict()


def _write_state(
    tmp_path: Path,
    *,
    status: str = "paused",
    run_id: str = "RUN-007",
    resume_options: list[str] | None = None,
    pause_payload: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    plan = _plan(tmp_path)
    schedule = _write_schedule(tmp_path)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "plan_path": str(plan.resolve()),
        "schedule_file": str(schedule),
        "phase": "paused" if status == "paused" else status,
        "status": status,
        "assignments": _assignment_payload(),
        "active_task_id": "001",
        "active_batch": {"index": 1, "task_ids": ["001"], "file_locks": ["src/app.py"]},
        "pause_reason": "test",
        "pause_payload": pause_payload
        or {"stage": "phase_d", "directive": {"action": "pause_awaiting_user"}},
        "resume_options": resume_options or ["retry", "fail-fast", "abort"],
    }
    path = MODULE._runner_state_path(schedule)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return plan, path


def test_status_outputs_runner_state_without_schedule_task_state(tmp_path: Path) -> None:
    plan, _state_path = _write_state(tmp_path)

    result = MODULE.status(str(plan))

    assert result["status"] == "paused"
    assert result["run_id"] == "RUN-007"
    assert result["active_task_id"] == "001"
    assert result["resume_options"] == ["retry", "fail-fast", "abort"]
    assert {"done", "failed", "blocked", "committed"}.isdisjoint(result)


def test_valid_abort_resume_marks_schedule_and_run_log(tmp_path: Path) -> None:
    plan, state_path = _write_state(tmp_path, resume_options=["abort"])
    facade = FakeFacade()

    result = MODULE.resume(plan=str(plan), decision="abort", facade=facade)

    assert result["status"] == "aborted"
    assert facade.acquire_calls == []
    assert [call["event"] for call in facade.log_calls] == ["resume_abort", "run_end"]
    schedule = json.loads((tmp_path / "plan.schedule.json").read_text(encoding="utf-8"))
    assert schedule["state"]["failed"] == ["001"]
    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "aborted"


def test_invalid_resume_decision_fails_closed(tmp_path: Path) -> None:
    plan, _state_path = _write_state(tmp_path, resume_options=["abort"])
    schedule_before = (tmp_path / "plan.schedule.json").read_text(encoding="utf-8")
    facade = FakeFacade()

    with pytest.raises(MODULE.RunnerContractError, match="not authorized"):
        MODULE.resume(plan=str(plan), decision="retry", facade=facade)

    assert facade.acquire_calls == []
    assert facade.log_calls == []
    assert (tmp_path / "plan.schedule.json").read_text(encoding="utf-8") == schedule_before


def test_stale_run_id_fails_before_resume_work(tmp_path: Path) -> None:
    plan, _state_path = _write_state(tmp_path, run_id="STALE", resume_options=["retry"])
    facade = FakeFacade(run_id="RUN-007")

    with pytest.raises(MODULE.RunnerContractError, match="lock acquisition failed"):
        MODULE.resume(plan=str(plan), decision="retry", facade=facade)

    assert facade.acquire_calls[0]["run_id"] == "STALE"
    assert facade.log_calls == []


def test_missing_lock_fails_before_resume_work(tmp_path: Path) -> None:
    plan, _state_path = _write_state(tmp_path, resume_options=["retry"])
    facade = FakeFacade(acquired=False)

    with pytest.raises(MODULE.RunnerContractError, match="lock acquisition failed"):
        MODULE.resume(plan=str(plan), decision="retry", facade=facade)

    assert facade.acquire_calls
    assert facade.log_calls == []


def test_corrupted_runner_state_json_fails_closed(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    schedule = _write_schedule(tmp_path)
    MODULE._runner_state_path(schedule).write_text("{", encoding="utf-8")

    with pytest.raises(MODULE.RunnerContractError, match="corrupted"):
        MODULE.status(str(plan))


def test_runner_state_rejects_duplicate_schedule_truth(tmp_path: Path) -> None:
    plan, state_path = _write_state(tmp_path)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["done"] = ["001"]
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MODULE.RunnerContractError, match="duplicates schedule task state"):
        MODULE.status(str(plan))


def test_unknown_state_resume_requires_abort_or_route_payload(tmp_path: Path) -> None:
    plan, _state_path = _write_state(
        tmp_path,
        resume_options=["retry", "abort"],
        pause_payload={"stage": "phase_d", "directive": {"action": "unknown_state"}},
    )

    with pytest.raises(MODULE.RunnerContractError, match="unknown_state resume requires"):
        MODULE.resume(plan=str(plan), decision="retry", facade=FakeFacade())


def test_unknown_state_pause_authorizes_retry_with_route_payload(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    schedule = _write_schedule(tmp_path)

    MODULE._write_runner_state(
        schedule,
        phase="paused",
        pause={"stage": "phase_d", "directive": {"action": "unknown_state"}},
        run_id="RUN-007",
        plan_path=plan,
        assignment_plan=_assignment_payload(),
        active_task_id="001",
        active_batch={"index": 1, "task_ids": ["001"], "file_locks": ["src/app.py"]},
    )

    result = MODULE.status(str(plan))

    assert result["resume_options"] == ["retry", "abort"]


def test_resume_rehydrates_assignments_from_runner_state(tmp_path: Path, monkeypatch) -> None:
    plan, _state_path = _write_state(tmp_path, resume_options=["retry"])
    facade = FakeFacade()
    captured: dict[str, Any] = {}

    def fake_run(config: Any, **kwargs: Any) -> dict[str, Any]:
        captured["assignments"] = config.assignments
        captured["reviewers"] = config.reviewers
        captured["plan_reviewer"] = config.plan_reviewer
        captured["resume_run_id"] = kwargs["resume_run_id"]
        captured["lock_already_acquired"] = kwargs["lock_already_acquired"]
        captured["route_decision_payload"] = kwargs["route_decision_payload"]
        captured["route_decision_stage"] = kwargs["route_decision_stage"]
        return {"status": "completed", "run_id": "RUN-007", "dry_run": False}

    monkeypatch.setattr(MODULE, "run", fake_run)
    route_decision = {"action": "commit", "args": {}}
    result = MODULE.resume(
        plan=str(plan),
        decision="retry",
        route_decision_payload=route_decision,
        facade=facade,
    )

    assert result["status"] == "completed"
    assert captured["assignments"] == (MODULE.TaskAssignment(task_id="001", provider="codex"),)
    assert captured["reviewers"] == ("gemini",)
    assert captured["plan_reviewer"] is None
    assert captured["resume_run_id"] == "RUN-007"
    assert captured["lock_already_acquired"] is True
    assert captured["route_decision_payload"] == route_decision
    assert captured["route_decision_stage"] == "phase_d"
    assert facade.acquire_calls == [{"plan_file": plan.resolve(), "run_id": "RUN-007", "force": False}]
    assert facade.release_calls == [{"plan_file": plan.resolve(), "run_id": "RUN-007"}]


def test_legacy_runner_state_migrates_before_schema_validation(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    schedule = _write_schedule(tmp_path)
    legacy_path = MODULE._legacy_runner_state_path(schedule)
    legacy_path.write_text(
        json.dumps(
            {
                "run_id": "RUN-007",
                "plan": str(plan.resolve()),
                "status": "paused",
                "active": ["001"],
                "done": [],
                "failed": [],
                "blocked": [],
            }
        ),
        encoding="utf-8",
    )

    result = MODULE.status(str(plan))

    assert result["status"] == "paused"
    assert result["run_id"] == "RUN-007"
    assert result["active_task_id"] == "001"
