from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN_PATH = SCRIPT_DIR / "implement_plan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan_e2e", IMPLEMENT_PLAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MODULE = _load_module()


def _task(task_id: str, *, files: list[str]) -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "deps": [],
        "dependencies": [],
        "files": files,
        "plan_file": f"TASK-{task_id}_task.md",
        "description": f"Task {task_id} description",
        "acceptance_criteria": ["passes"],
    }


TASKS = [
    _task("001", files=["src/one.py"]),
    _task("002", files=["src/two.py"]),
    _task("003", files=["src/three.py"]),
]


class E2EFacade:
    def __init__(self, tmp_path: Path, *, pause_once: bool = False) -> None:
        self.tmp_path = tmp_path
        self.pause_once = pause_once
        self.reconcile_calls = 0
        self.calls: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.route_calls: list[dict[str, Any]] = []
        self.commit_calls: list[dict[str, Any]] = []
        self.fail_calls: list[dict[str, Any]] = []
        self.finalize_calls: list[dict[str, Any]] = []
        self.update_header_calls: list[dict[str, Any]] = []
        self.certify_calls: list[dict[str, Any]] = []
        self.acquire_calls: list[dict[str, Any]] = []
        self.release_calls: list[dict[str, Any]] = []

    def path_info(self) -> dict[str, Any]:
        self.calls.append("path_info")
        return {}

    def preflight(self, **payload: Any) -> dict[str, Any]:
        self.calls.append("preflight")
        return {"pass": True, "run_id": "RUN-E2E", "starting_sha": "START"}

    def check_plan_deps(self, **payload: Any) -> dict[str, Any]:
        self.calls.append("check_plan_deps")
        return {"ok": True, "errors": []}

    def gates(self, **payload: Any) -> dict[str, Any]:
        if payload.get("certify"):
            self.certify_calls.append(dict(payload))
            self.calls.append("certify")
            return {"certified": True, "gates": {}}
        self.calls.append("schedule_valid_gate" if payload.get("check") == "schedule-valid" else "pre_dispatch_gates")
        return {"ok": True, "gates": {}}

    def acquire_lock(self, **payload: Any) -> dict[str, Any]:
        self.acquire_calls.append(dict(payload))
        self.calls.append("acquire_lock")
        return {"acquired": True}

    def release_lock(self, **payload: Any) -> dict[str, Any]:
        self.release_calls.append(dict(payload))
        self.calls.append("release_lock")
        return {"released": True}

    def build_tasks(self, **payload: Any) -> dict[str, Any]:
        self.calls.append("build_tasks")
        return {
            "ok": True,
            "tasks": [dict(task) for task in TASKS],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"], "file_locks": ["src/one.py", "src/two.py"]},
                {"index": 2, "task_ids": ["003"], "file_locks": ["src/three.py"]},
            ],
            "warnings": [],
        }

    def write_schedule(self, **payload: Any) -> dict[str, Any]:
        self.calls.append("write_schedule")
        path = Path(payload["schedule_file"])
        path.write_text(json.dumps(payload["payload"], indent=2), encoding="utf-8")
        return {"ok": True, "written": str(path)}

    def batch_next(self, **payload: Any) -> dict[str, Any]:
        self.calls.append("batch_next")
        schedule = json.loads(Path(payload["schedule_file"]).read_text(encoding="utf-8"))
        state = schedule.get("state", {})
        done = {str(task_id) for task_id in state.get("done", [])}
        failed = {str(task_id) for task_id in state.get("failed", [])}
        picked: list[str] = []
        locked: set[str] = set()
        for task in TASKS:
            task_id = task["id"]
            if task_id in done or task_id in failed:
                continue
            files = set(task["files"])
            if files & locked:
                continue
            if len(picked) >= int(payload.get("parallel", 1)):
                break
            picked.append(task_id)
            locked |= files
        return {
            "batch_index": 1 if picked and "003" not in picked else 2 if picked else 0,
            "task_ids": picked,
            "file_locks": sorted(locked),
            "scheduler_stuck": False,
        }

    def plan_review_route(self, *, payload: dict[str, Any], update_schedule_state: str | None = None) -> dict[str, Any]:
        self.route_calls.append(dict(payload))
        self.calls.append(f"plan_review_route:{payload['stage']}")
        if payload["stage"] == "pre_dispatch":
            return {"action": "dispatch_stub_reviewer"}
        if payload["stage"] == "post_review":
            return {"action": "proceed_to_phase_2"}
        return {"action": "unknown_state", "reason": payload["stage"]}

    def review_route(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.route_calls.append(dict(payload))
        self.calls.append("review_route")
        task_id = payload["task_id"]
        if task_id == "002":
            return {
                "action": "fail",
                "args": {
                    "task_id": task_id,
                    "authorization_source": "phase-d4-review-failure",
                    "fail_stage": "review",
                    "fail_reason": "stub failure path",
                },
            }
        return {"action": "commit", "args": {"task_id": task_id}}

    def log_event(self, **payload: Any) -> dict[str, Any]:
        fields = payload["fields_json"]
        if isinstance(fields, str):
            fields = json.loads(fields)
        self.events.append({"event": payload["event"], **dict(fields)})
        self.calls.append(f"log:{payload['event']}")
        return {"ok": True}

    def commit_task(self, **payload: Any) -> dict[str, Any]:
        self.commit_calls.append(dict(payload))
        self._mark_state(Path(payload["update_schedule_state"]), payload["task_id"], "done")
        self.events.append({"event": "commit_done", "run_id": payload["run_id"], "task_id": payload["task_id"]})
        return {"ok": True, "committed": True}

    def fail_task(self, **payload: Any) -> dict[str, Any]:
        self.fail_calls.append(dict(payload))
        self._mark_state(Path(payload["update_schedule_state"]), payload["task_id"], "failed")
        self.events.append({"event": "failed", "run_id": payload["run_id"], "task_id": payload["task_id"]})
        return {"ok": True, "failed": True}

    def reconcile_batch(self, **payload: Any) -> dict[str, Any]:
        self.calls.append("reconcile_batch")
        self.reconcile_calls += 1
        if self.pause_once and self.reconcile_calls == 1:
            return {"ok": True, "paused": True, "results": [{"outcome": "scope_violation_paused"}]}
        return {"ok": True, "paused": False, "results": [{"outcome": "no_op"}]}

    def update_plan_header(self, **payload: Any) -> dict[str, Any]:
        self.update_header_calls.append(dict(payload))
        self.calls.append("update_plan_header")
        return {"ok": True}

    def finalize_execution_log(self, **payload: Any) -> dict[str, Any]:
        self.finalize_calls.append(dict(payload))
        self.calls.append("finalize_execution_log")
        return {"ok": True}

    def _mark_state(self, schedule_file: Path, task_id: str, key: str) -> None:
        schedule = json.loads(schedule_file.read_text(encoding="utf-8"))
        state = schedule.setdefault("state", {})
        state.setdefault(key, [])
        if task_id not in state[key]:
            state[key].append(task_id)
        schedule_file.write_text(json.dumps(schedule, indent=2), encoding="utf-8")


def _plan_dir(tmp_path: Path) -> Path:
    plan_dir = tmp_path / "DECOMPOSED"
    plan_dir.mkdir()
    (plan_dir / "00_INDEX.json").write_text(
        json.dumps({"chunks": [{"id": task["id"], "file": task["plan_file"], "depends_on": []} for task in TASKS]}),
        encoding="utf-8",
    )
    for task in TASKS:
        (plan_dir / task["plan_file"]).write_text(
            f"# Task {task['id']}\n\n**Status:** pending\n\n### TASK-{task['id']}: Task\n",
            encoding="utf-8",
        )
    return plan_dir


def _stub_registry() -> dict[str, Any]:
    provider = MODULE.StubProvider()
    return {"stub": provider}


def _config(plan_dir: Path) -> Any:
    return MODULE.RunnerConfig(
        plan=str(plan_dir),
        parallel=2,
        provider_preference=("stub", "codex", "claude", "gemini"),
        assignments=(MODULE.TaskAssignment("002", "stub"),),
        reviewers=("stub",),
        plan_reviewer="stub",
    )


def test_stub_provider_e2e_runs_plan_review_phase_d_pause_resume_and_finalization(tmp_path: Path) -> None:
    plan_dir = _plan_dir(tmp_path)
    facade = E2EFacade(tmp_path, pause_once=True)

    paused = MODULE.run(
        _config(plan_dir),
        facade=facade,
        providers=_stub_registry(),
        capabilities={"stub": MODULE.STUB_PROVIDER_CAPABILITY},
    )

    assert paused["status"] == "paused"
    assert paused["completed_phase"] == "reconcile_batch"
    assert [event["event"] for event in facade.events[:8]] == [
        "run_start",
        "schedule_written",
        "analyst_done",
        "plan_review_route_called",
        "plan_review_start",
        "plan_review_done",
        "plan_review_route_called",
        "batch_start",
    ]
    assert [call["task_id"] for call in facade.commit_calls] == ["001"]
    assert [call["task_id"] for call in facade.fail_calls] == ["002"]

    state = json.loads(MODULE._runner_state_path(MODULE._schedule_path_for_plan(plan_dir)).read_text(encoding="utf-8"))
    assert state["status"] == "paused"
    assert state["resume_options"] == ["fail-fast", "preserve-only", "revert", "keep-and-commit", "abort"]
    assert state["assignments"]["tasks"]["001"]["implementer"]["source"] == "provider-preference"
    assert state["assignments"]["tasks"]["002"]["implementer"]["source"] == "explicit"

    facade.pause_once = False
    resumed = MODULE.resume(plan=str(plan_dir), decision="fail-fast", facade=facade)

    assert resumed["status"] == "completed"
    assert resumed["resume_decision"] == "fail-fast"
    assert [call["task_id"] for call in facade.commit_calls] == ["001", "003"]
    assert [call["task_id"] for call in facade.fail_calls] == ["002"]
    assert facade.update_header_calls
    assert facade.finalize_calls
    assert facade.certify_calls[-1]["certify_mode"] == "execute"
    assert Path(facade.certify_calls[-1]["plan_file"]) == plan_dir / "TASK-001_task.md"
    assert facade.events[-1]["event"] == "run_end"
    assert facade.events[-1]["outcome"] == "success"

    event_names = [event["event"] for event in facade.events]
    assert event_names.index("plan_review_start") < event_names.index("batch_start")
    assert event_names.index("commit_done") < event_names.index("failed")
    assert event_names.index("failed") < event_names.index("run_end")
