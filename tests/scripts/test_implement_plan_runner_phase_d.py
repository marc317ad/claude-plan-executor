from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN_PATH = SCRIPT_DIR / "implement_plan.py"
sys.path.insert(0, str(SCRIPT_DIR))
import plan_ops  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan_phase_d", IMPLEMENT_PLAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_MODULE = _load_module()


def _plan(tmp_path: Path) -> Path:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n## TASK-001: Task\n", encoding="utf-8")
    return path


def _task(task_id: str, files: list[str] | None = None, agent: str = "claude") -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "dependencies": [],
        "deps": [],
        "files": files or [f"src/{task_id}.py"],
        "plan_file": f"TASK-{task_id}_task.md",
        "description": "task",
        "acceptance_criteria": ["works"],
        "agent": agent,
    }


class FakeFacade:
    def __init__(
        self,
        tmp_path: Path,
        *,
        tasks: list[dict[str, Any]] | None = None,
        reconcile_paused: bool = False,
    ) -> None:
        self.tmp_path = tmp_path
        self.tasks = tasks or [_task("001")]
        self.reconcile_paused = reconcile_paused
        self.events: list[dict[str, Any]] = []
        self.route_calls: list[dict[str, Any]] = []
        self.commit_calls: list[dict[str, Any]] = []
        self.fail_calls: list[dict[str, Any]] = []
        self.batch_next_calls: list[dict[str, Any]] = []
        self.batch_results: list[list[str]] = []
        self.reconcile_calls: list[dict[str, Any]] = []

    def path_info(self) -> dict[str, Any]:
        return {}

    def preflight(self, **payload: Any) -> dict[str, Any]:
        return {"pass": True, "run_id": "RUN-006"}

    def decompose_plan(self, **payload: Any) -> dict[str, Any]:
        out_dir = Path(payload["plan_file"]).with_suffix("")
        out_dir.mkdir(exist_ok=True)
        for task in self.tasks:
            (out_dir / task["plan_file"]).write_text(
                f"### TASK-{task['id']}: Task\n\n**Status:** pending\n",
                encoding="utf-8",
            )
        (out_dir / "00_INDEX.json").write_text(json.dumps({"chunks": []}), encoding="utf-8")
        return {"ok": True, "out_dir": str(out_dir)}

    def check_plan_deps(self, **payload: Any) -> dict[str, Any]:
        return {"ok": True, "errors": []}

    def gates(self, **payload: Any) -> dict[str, Any]:
        return {"ok": True}

    def acquire_lock(self, **payload: Any) -> dict[str, Any]:
        return {"acquired": True}

    def release_lock(self, **payload: Any) -> dict[str, Any]:
        return {"released": True}

    def build_tasks(self, **payload: Any) -> dict[str, Any]:
        batches = [{"index": 1, "task_ids": [t["id"] for t in self.tasks], "file_locks": []}]
        return {"ok": True, "tasks": self.tasks, "batches": batches, "warnings": []}

    def write_schedule(self, **payload: Any) -> dict[str, Any]:
        path = Path(payload["schedule_file"])
        path.write_text(json.dumps(payload["payload"]), encoding="utf-8")
        return {"written": str(path)}

    def batch_next(self, **payload: Any) -> dict[str, Any]:
        self.batch_next_calls.append(dict(payload))
        schedule = json.loads(Path(payload["schedule_file"]).read_text(encoding="utf-8"))
        done = set(schedule.get("state", {}).get("done", []))
        failed = set(schedule.get("state", {}).get("failed", []))
        picked: list[str] = []
        locked: set[str] = set()
        for task in self.tasks:
            tid = task["id"]
            if tid in done or tid in failed:
                continue
            files = set(task.get("files") or [])
            if files & locked:
                continue
            if len(picked) >= int(payload.get("parallel", 1)):
                break
            picked.append(tid)
            locked |= files
        self.batch_results.append(list(picked))
        return {"batch_index": 1 if picked else 0, "task_ids": picked, "file_locks": sorted(locked), "scheduler_stuck": False}

    def plan_review_route(self, *, payload: dict, update_schedule_state: str | None = None) -> dict[str, Any]:
        if payload["stage"] == "pre_dispatch":
            return {"action": "skip_plan_review", "reason": "test"}
        return {"action": "proceed_to_phase_2"}

    def review_route(self, payload: dict) -> dict[str, Any]:
        self.route_calls.append(dict(payload))
        return plan_ops._route_review_route(dict(payload))

    def log_event(self, **payload: Any) -> dict[str, Any]:
        fields = payload["fields_json"]
        if isinstance(fields, str):
            fields = json.loads(fields)
        self.events.append({"event": payload["event"], **fields})
        return {"ok": True}

    def commit_task(self, **payload: Any) -> dict[str, Any]:
        self.commit_calls.append(dict(payload))
        self._mark_state(payload["update_schedule_state"], payload["task_id"], "done")
        return {"ok": True, "committed": True}

    def fail_task(self, **payload: Any) -> dict[str, Any]:
        self.fail_calls.append(dict(payload))
        self._mark_state(payload["update_schedule_state"], payload["task_id"], "failed")
        return {"ok": True, "failed": True}

    def reconcile_batch(self, **payload: Any) -> dict[str, Any]:
        self.reconcile_calls.append(dict(payload))
        if self.reconcile_paused:
            return {"ok": True, "paused": True, "results": [{"outcome": "scope_violation_paused"}]}
        return {"ok": True, "paused": False, "results": [{"outcome": "no_op"}]}

    def _mark_state(self, schedule_file: str, task_id: str, key: str) -> None:
        schedule = json.loads(Path(schedule_file).read_text(encoding="utf-8"))
        state = schedule.setdefault("state", {})
        state.setdefault(key, [])
        if task_id not in state[key]:
            state[key].append(task_id)
        Path(schedule_file).write_text(json.dumps(schedule), encoding="utf-8")


class FakeProvider:
    def __init__(
        self,
        module: Any,
        name: str,
        *,
        implements: list[dict[str, Any]] | None = None,
        reviews: list[dict[str, Any]] | None = None,
    ) -> None:
        self.capability = module.DEFAULT_PROVIDER_CAPABILITIES[name]
        self.implements = implements or [{"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "done"}]
        self.reviews = reviews or [{"verdict": "clean", "findings": [], "summary": ""}]
        self.implement_calls: list[dict[str, Any]] = []
        self.review_calls: list[dict[str, Any]] = []
        self.plan_review_calls = 0

    def implement(self, **payload: Any):
        self.implement_calls.append(dict(payload))
        parsed = dict(
            self.implements.pop(0)
            if self.implements
            else {"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "done"}
        )
        return _MODULE.DispatchResult(provider=self.capability.name, role="implement", status="ok", parsed=parsed)

    def review(self, **payload: Any):
        self.review_calls.append(dict(payload))
        parsed = dict(self.reviews.pop(0) if self.reviews else {"verdict": "clean", "findings": [], "summary": ""})
        return _MODULE.DispatchResult(provider=self.capability.name, role="review", status="ok", parsed=parsed)

    def plan_review(self, **payload: Any):
        self.plan_review_calls += 1
        return _MODULE.DispatchResult(
            provider=self.capability.name,
            role="plan_review",
            status="ok",
            parsed={"verdict": "approved", "findings": [], "outcome": "success"},
        )

    def triage(self, **payload: Any):
        return _MODULE.DispatchResult(provider=self.capability.name, role="triage", status="ok", parsed={"verdict": "ship"})

    def author(self, **payload: Any):
        return _MODULE.DispatchResult(provider=self.capability.name, role="author", status="ok", parsed={"ok": True})


def _providers(
    *,
    codex_reviews: list[dict[str, Any]] | None = None,
    claude_reviews: list[dict[str, Any]] | None = None,
    gemini_reviews: list[dict[str, Any]] | None = None,
    codex_implements: list[dict[str, Any]] | None = None,
    claude_implements: list[dict[str, Any]] | None = None,
) -> dict[str, FakeProvider]:
    return {
        "claude": FakeProvider(_MODULE, "claude", implements=claude_implements, reviews=claude_reviews),
        "codex": FakeProvider(_MODULE, "codex", implements=codex_implements, reviews=codex_reviews),
        "gemini": FakeProvider(_MODULE, "gemini", reviews=gemini_reviews),
    }


def _run(
    tmp_path: Path,
    facade: FakeFacade,
    providers: dict[str, FakeProvider],
    config: Any | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return _MODULE.run(
        config or _MODULE.RunnerConfig(plan=str(_plan(tmp_path)), skip_plan_review=True, plan_reviewer=None),
        facade=facade,
        providers=providers,
        **kwargs,
    )


def test_clean_commit_uses_schedule_state_and_logs_phase_d_events(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(codex_reviews=[{"verdict": "clean", "findings": [], "summary": ""}])
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "completed"
    assert facade.batch_next_calls[0]["from_schedule_state"] is True
    assert facade.commit_calls[0]["update_schedule_state"]
    event_names = [event["event"] for event in facade.events]
    for name in ("batch_start", "implement_start", "implement_done", "review_start", "review_done", "review_route_called", "run_end"):
        assert name in event_names
    assert "batch_done" not in event_names


def test_minor_findings_commit_passes_reviewer_findings(tmp_path: Path) -> None:
    finding = {"severity": "low", "category": "style", "message": "minor", "path": "src/001.py", "line": 1}
    facade = FakeFacade(tmp_path)
    providers = _providers(codex_reviews=[{"verdict": "minor-findings", "findings": [finding], "summary": ""}])
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "completed"
    assert json.loads(facade.commit_calls[0]["reviewer_minor_findings"]) == [finding]


def test_reviewer_none_skips_review_and_commits_with_none_identity(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers()
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        skip_cross_review=True,
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "completed"
    assert providers["codex"].review_calls == []
    assert facade.commit_calls[0]["reviewer"] == "none"
    assert facade.commit_calls[0]["reviewer_verdict"] == ""
    assert facade.route_calls[0]["reviewer"] == "none"
    assert "review_skipped" in [event["event"] for event in facade.events]


def test_gemini_review_identity_routes_to_commit(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(gemini_reviews=[{"verdict": "clean", "findings": [], "summary": ""}])
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        reviewers=("gemini",),
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "completed"
    assert providers["gemini"].review_calls
    assert facade.route_calls[0]["reviewer"] == "gemini"


def test_claude_only_review_routes_to_commit(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(claude_reviews=[{"verdict": "ship", "findings": [], "summary": ""}])
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        reviewers=("claude",),
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "completed"
    assert facade.route_calls[0]["claude_only"] is True
    assert facade.commit_calls[0]["reviewer"] == "claude"


def test_binding_pause_stops_without_fail_task(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(codex_reviews=[{"verdict": "needs-rework", "findings": [], "summary": ""}])
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        agent_args={"codex_review_binding": True},
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "paused"
    assert facade.fail_calls == []
    assert facade.route_calls[0]["flags"]["codex_review_binding"] is True


def test_binding_fail_fast_calls_fail_task_with_authorization(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(codex_reviews=[{"verdict": "needs-rework", "findings": [], "summary": ""}])
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        unattended_revert_policy="fail-fast",
        agent_args={"codex_review_binding": True},
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "completed"
    assert facade.fail_calls[0]["authorization_source"] == "unattended-fail-fast"


def test_d5_disagreement_commits_with_route_flags(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(
        codex_reviews=[{"verdict": "needs-rework", "findings": [{"id": 1}], "summary": ""}],
        claude_reviews=[{"verdict": "ship", "load_bearing": [], "dismissed": [], "summary": ""}],
    )
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "completed"
    assert facade.commit_calls[0]["disagreement_tag"] is True
    assert [call["action"] for call in facade.route_calls if "action" in call] == []
    assert [event["action"] for event in facade.events if event["event"] == "review_route_called"][-1] == "commit"


def test_bounded_remediation_dispatches_provider_with_context(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(
        codex_reviews=[
            {"verdict": "needs-rework", "findings": [{"id": 1}], "summary": ""},
            {"verdict": "clean", "findings": [], "summary": ""},
        ],
        claude_reviews=[{"verdict": "needs-rework", "load_bearing": [0], "dismissed": [], "summary": "d5"}],
        claude_implements=[
            {"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "first"},
            {"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "retry"},
        ],
    )
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "completed"
    assert len(providers["claude"].implement_calls) == 2
    assert providers["claude"].implement_calls[1]["dispatch_context"]["template"] == "PhaseB-rework"
    assert facade.route_calls[-1]["d5_envelope"] is None
    reconcile_payload = facade.reconcile_calls[0]["payload"]
    assert len(reconcile_payload) == 1
    assert reconcile_payload[0]["provider"] == "claude"
    assert reconcile_payload[0]["envelope"]["diff_summary"] == "retry"


def test_narrow_remediation_dispatches_provider_with_context(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(
        codex_reviews=[
            {"verdict": "needs-rework", "findings": [{"id": 1}, {"id": 2}], "summary": ""},
            {"verdict": "clean", "findings": [], "summary": ""},
        ],
        claude_reviews=[{"verdict": "partial-agreement", "load_bearing": [0], "dismissed": [1], "summary": "d5"}],
        claude_implements=[
            {"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "first"},
            {"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "retry"},
        ],
    )
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "completed"
    assert providers["claude"].implement_calls[1]["dispatch_context"]["template"] == "PhaseB-narrow-remediation"
    assert facade.route_calls[-1]["d5_envelope"] is None


def test_role_swap_dispatches_claude_provider(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path, tasks=[_task("001", agent="codex")])
    providers = _providers(
        claude_reviews=[
            {"verdict": "needs-rework", "findings": [{"id": 1}], "summary": "rework"},
            {"verdict": "ship", "findings": [], "summary": ""},
        ],
        codex_implements=[{"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "codex"}],
        claude_implements=[{"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "swap"}],
    )
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        provider_preference=("codex", "claude"),
        reviewers=("claude",),
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "completed"
    assert providers["codex"].implement_calls
    assert providers["claude"].implement_calls[0]["dispatch_context"]["template"] == "PhaseB-rework"
    assert facade.route_calls[-1]["implementer"] == "claude"
    assert facade.route_calls[-1]["claude_only"] is True
    reconcile_payload = facade.reconcile_calls[0]["payload"]
    assert reconcile_payload[0]["provider"] == "claude"
    assert reconcile_payload[0]["envelope"]["diff_summary"] == "swap"


def test_retry_implementation_failure_pauses_before_review(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(
        codex_reviews=[{"verdict": "needs-rework", "findings": [{"id": 1}], "summary": ""}],
        claude_reviews=[{"verdict": "needs-rework", "load_bearing": [0], "dismissed": [], "summary": "d5"}],
        claude_implements=[
            {"status": "completed", "files_changed": ["src/001.py"], "diff_summary": "first"},
            {"status": "failed", "files_changed": ["src/001.py"], "diff_summary": "retry failed"},
        ],
    )
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "paused"
    assert result["completed_phase"] == "implement"
    assert result["errors"][0]["status"] == "failed"
    assert len(providers["codex"].review_calls) == 1
    assert facade.commit_calls == []
    assert facade.reconcile_calls == []


def test_unknown_state_pauses(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(codex_reviews=[{"verdict": "surprise", "findings": [], "summary": ""}])
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "paused"
    assert result["errors"][0]["action"] == "unknown_state"


def test_supplied_route_decision_is_consumed_instead_of_rerouting_unknown_state(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path)
    providers = _providers(codex_reviews=[{"verdict": "surprise", "findings": [], "summary": ""}])

    result = _run(
        tmp_path,
        facade,
        providers,
        route_decision_payload={"action": "commit", "args": {}},
        route_decision_stage="phase_d",
    )

    assert result["status"] == "completed"
    assert facade.route_calls == []
    assert facade.commit_calls
    assert [event["action"] for event in facade.events if event["event"] == "review_route_called"] == ["commit"]


def test_parallel_batch_does_not_dispatch_conflicting_file_locks(tmp_path: Path) -> None:
    tasks = [_task("001", files=["src/shared.py"]), _task("002", files=["src/shared.py"])]
    facade = FakeFacade(tmp_path, tasks=tasks)
    providers = _providers(codex_reviews=[
        {"verdict": "clean", "findings": [], "summary": ""},
        {"verdict": "clean", "findings": [], "summary": ""},
    ])
    config = _MODULE.RunnerConfig(
        plan=str(_plan(tmp_path)),
        skip_plan_review=True,
        plan_reviewer=None,
        parallel=2,
    )
    result = _run(tmp_path, facade, providers, config)

    assert result["status"] == "completed"
    assert facade.batch_results[1:3] == [["001"], ["002"]]


def test_reconcile_batch_can_pause_run(tmp_path: Path) -> None:
    facade = FakeFacade(tmp_path, reconcile_paused=True)
    providers = _providers(codex_reviews=[{"verdict": "clean", "findings": [], "summary": ""}])
    result = _run(tmp_path, facade, providers)

    assert result["status"] == "paused"
    assert result["completed_phase"] == "reconcile_batch"
    assert facade.reconcile_calls
