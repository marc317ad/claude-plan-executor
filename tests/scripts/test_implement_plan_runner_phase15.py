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
    spec = importlib.util.spec_from_file_location("implement_plan_phase15", IMPLEMENT_PLAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeFacade:
    def __init__(self, root: Path, *, route_overrides: dict[str, list[dict]] | None = None) -> None:
        self.root = root
        self.calls: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.route_calls: list[dict[str, Any]] = []
        self.released = False
        self.release_count = 0
        self.fail_build = False
        self.fail_batch_next = False
        self.route_overrides = route_overrides or {}

    def path_info(self) -> dict:
        self.calls.append("path_info")
        return {}

    def preflight(self, **payload: Any) -> dict:
        self.calls.append("preflight")
        return {"pass": True, "run_id": "RUN-005"}

    def decompose_plan(self, **payload: Any) -> dict:
        self.calls.append("decompose_plan")
        out_dir = Path(payload["plan_file"]).with_suffix("")
        out_dir.mkdir(exist_ok=True)
        return {"ok": True, "out_dir": str(out_dir)}

    def check_plan_deps(self, **payload: Any) -> dict:
        self.calls.append("check_plan_deps")
        return {"ok": True, "errors": []}

    def gates(self, **payload: Any) -> dict:
        name = "schedule_valid_gate" if payload.get("check") == "schedule-valid" else "pre_dispatch_gates"
        self.calls.append(name)
        return {"gates": {}}

    def acquire_lock(self, **payload: Any) -> dict:
        self.calls.append("acquire_lock")
        return {"acquired": True}

    def release_lock(self, **payload: Any) -> dict:
        self.calls.append("release_lock")
        self.released = True
        self.release_count += 1
        return {"released": True}

    def build_tasks(self, **payload: Any) -> dict:
        self.calls.append("build_tasks")
        if self.fail_build:
            return {"ok": False, "errors": [{"code": "build-failed"}]}
        return {
            "ok": True,
            "tasks": [
                {
                    "id": "001",
                    "title": "Task",
                    "deps": [],
                    "files": ["src/app.py"],
                    "plan_file": "TASK-001_task.md",
                    "description": "task",
                    "acceptance_criteria": ["works"],
                }
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["src/app.py"]}],
            "warnings": [],
        }

    def write_schedule(self, **payload: Any) -> dict:
        self.calls.append("write_schedule")
        path = Path(payload["schedule_file"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload["payload"]), encoding="utf-8")
        return {"written": str(path)}

    def batch_next(self, **payload: Any) -> dict:
        self.calls.append("batch_next")
        if self.fail_batch_next:
            return {"ok": False, "errors": [{"code": "batch-next-failed"}]}
        return {"task_ids": ["001"]}

    def log_event(self, **payload: Any) -> dict:
        fields = payload["fields_json"]
        if isinstance(fields, str):
            fields = json.loads(fields)
        event = {"event": payload["event"], **dict(fields)}
        self.events.append(event)
        self.calls.append(f"log:{payload['event']}")
        return {"ok": True}

    def plan_review_route(self, *, payload: dict, update_schedule_state: str | None = None) -> dict:
        self.calls.append(f"route:{payload['stage']}")
        self.route_calls.append(dict(payload))
        queue = self.route_overrides.get(payload["stage"], [])
        if queue:
            directive = dict(queue.pop(0))
        else:
            directive = self._default_route(payload)
        if update_schedule_state:
            self._apply_state(Path(update_schedule_state), payload, directive)
        return directive

    def _default_route(self, payload: dict) -> dict:
        stage = payload["stage"]
        if stage == "pre_dispatch":
            if payload["flags"].get("skip_plan_review"):
                return {"action": "skip_plan_review", "reason": "flag"}
            reviewer = payload.get("reviewer", "codex")
            if reviewer == "gemini":
                return {"action": "dispatch_gemini_reviewer"}
            if reviewer == "claude":
                return {"action": "dispatch_claude_reviewer"}
            return {"action": "dispatch_codex_reviewer"}
        if stage == "post_review":
            env = payload["plan_review_envelope"]
            if env.get("outcome") in {"timeout", "parse_error", "failure"}:
                return {"action": "skip_plan_review", "reason": "codex_unavailable"}
            if env.get("verdict") == "approved":
                return {"action": "proceed_to_phase_2"}
            if env.get("verdict") == "needs-replan":
                attempt = payload.get("attempt", 1)
                if attempt == 1:
                    return {"action": "dispatch_triage", "dispatch_context": {"findings_for_payload": env.get("findings", [])}}
                return {"action": "halt_plan_review_failed", "args": {"reason_detail": "second_needs_replan"}}
            return {"action": "unknown_state", "reason": f"unknown {env.get('verdict')}"}
        if stage == "post_triage":
            verdict = payload["triage_envelope"]["verdict"]
            if verdict == "ship":
                return {"action": "proceed_to_phase_2"}
            if verdict == "needs-rework":
                return {
                    "action": "dispatch_plan_author_per_finding",
                    "dispatch_context": {"per_finding_dispatches": [{"target_task_id": "001", "variant": "A"}]},
                }
            return {"action": "unknown_state", "reason": f"unknown triage {verdict}"}
        if stage == "post_plan_author":
            return {"action": "rerun_analyst_then_review", "args": {"next_attempt": 2}}
        if stage == "manual_pause":
            return {"action": "pause_awaiting_user", "reason": payload.get("reason", "pause")}
        return {"action": "unknown_state", "reason": "bad stage"}

    def _apply_state(self, path: Path, payload: dict, directive: dict) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = data.setdefault("plan_review_state", {})
        if payload["stage"] == "post_plan_author":
            state["attempt"] = 2
            state["auto_revise_round_completed"] = True
        if payload["stage"] == "post_review":
            attempt = payload.get("attempt", state.get("attempt", 1))
            state["attempt"] = attempt
            verdict = payload["plan_review_envelope"].get("verdict")
            if attempt == 1:
                state["first_verdict"] = verdict
            elif attempt == 2:
                state["second_verdict"] = verdict
        if directive.get("action") == "skip_plan_review":
            state["skipped_reason"] = directive.get("reason")
        path.write_text(json.dumps(data), encoding="utf-8")


class FakeProvider:
    def __init__(self, module: Any, name: str, *, reviews: list[dict] | None = None, triage: dict | None = None) -> None:
        self.capability = module.DEFAULT_PROVIDER_CAPABILITIES[name]
        self.reviews = reviews or [{"verdict": "approved", "findings": [], "outcome": "success"}]
        self.triage_payload = triage or {"verdict": "ship", "summary": ""}
        self.plan_review_calls: list[dict[str, Any]] = []
        self.triage_calls = 0
        self.author_calls = 0

    def plan_review(self, **payload: Any):
        self.plan_review_calls.append(payload)
        parsed = dict(self.reviews.pop(0))
        parsed.setdefault("outcome", "success")
        parsed.setdefault("reviewer", self.capability.name)
        return _MODULE.DispatchResult(provider=self.capability.name, role="plan_review", status="ok", parsed=parsed)

    def triage(self, **payload: Any):
        self.triage_calls += 1
        return _MODULE.DispatchResult(provider=self.capability.name, role="triage", status="ok", parsed=dict(self.triage_payload))

    def author(self, **payload: Any):
        self.author_calls += 1
        return _MODULE.DispatchResult(provider=self.capability.name, role="author", status="ok", parsed={"ok": True})


_MODULE = _load_module()


def _plan(tmp_path: Path) -> Path:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\n## TASK-001: Task\n", encoding="utf-8")
    return path


def _providers(module: Any, *, codex_reviews: list[dict] | None = None, triage: dict | None = None) -> dict[str, FakeProvider]:
    claude = FakeProvider(module, "claude", triage=triage)
    return {
        "codex": FakeProvider(module, "codex", reviews=codex_reviews),
        "gemini": FakeProvider(module, "gemini", reviews=codex_reviews),
        "claude": claude,
    }


def test_approved_path_promotes_single_file_and_runs_front_half_in_order(tmp_path: Path, monkeypatch) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    order: list[str] = []
    original = module.resolve_assignments

    def wrapped(tasks, config, capabilities):
        order.append("assignment_policy")
        return original(tasks, config, capabilities)

    monkeypatch.setattr(module, "resolve_assignments", wrapped)
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=_providers(module),
    )

    assert result["completed_phase"] == "phase_2_ready"
    expected = [
        "preflight",
        "decompose_plan",
        "check_plan_deps",
        "pre_dispatch_gates",
        "acquire_lock",
        "build_tasks",
        "write_schedule",
        "schedule_valid_gate",
    ]
    assert [call for call in facade.calls if call in expected] == expected
    assert order == ["assignment_policy"]
    assert "route:pre_dispatch" in facade.calls
    assert "route:post_review" in facade.calls
    assert facade.release_count == 1
    assert [e["event"] for e in facade.events[:3]] == ["run_start", "schedule_written", "analyst_done"]
    assert {"plan_review_start", "plan_review_done", "plan_review_route_called"} <= {e["event"] for e in facade.events}


def test_skip_plan_review_reaches_phase_2_without_dispatching_reviewer(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(module)
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path)), skip_plan_review=True, plan_reviewer=None),
        facade=facade,
        providers=providers,
    )

    assert result["completed_phase"] == "phase_2_ready"
    assert providers["codex"].plan_review_calls == []
    assert any(e["event"] == "plan_review_skipped" for e in facade.events)
    assert any(e["event"] == "plan_review_route_called" and e["stage"] == "pre_dispatch" for e in facade.events)


def test_plan_reviewer_selection_uses_assignment_policy_capabilities(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(module)
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path)), plan_reviewer="codex"),
        facade=facade,
        providers=providers,
        capabilities={"claude": module.DEFAULT_PROVIDER_CAPABILITIES["claude"], "gemini": module.DEFAULT_PROVIDER_CAPABILITIES["gemini"]},
    )

    assert result["completed_phase"] == "phase_2_ready"
    assert providers["gemini"].plan_review_calls
    assert providers["codex"].plan_review_calls == []
    assert facade.route_calls[0]["reviewer"] == "gemini"


def test_reviewer_unavailable_degrades_to_skip(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(module, codex_reviews=[{"outcome": "timeout", "verdict": None, "findings": []}])
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=providers,
    )

    assert result["completed_phase"] == "phase_2_ready"
    assert any(e["event"] == "plan_review_skipped" and e["reason"] == "codex_unavailable" for e in facade.events)


def test_needs_replan_triage_ship_reaches_phase_2(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(
        module,
        codex_reviews=[{"verdict": "needs-replan", "findings": [{"concern": "x"}], "outcome": "success"}],
        triage={"verdict": "ship", "summary": "ship"},
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=providers,
    )

    assert result["completed_phase"] == "phase_2_ready"
    assert providers["claude"].triage_calls == 1
    assert "route:post_triage" in facade.calls


def test_author_retry_second_approved_uses_post_review_attempt_2(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(
        module,
        codex_reviews=[
            {"verdict": "needs-replan", "findings": [{"concern": "x"}], "outcome": "success"},
            {"verdict": "approved", "findings": [], "outcome": "success"},
        ],
        triage={"verdict": "needs-rework", "summary": "fix"},
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=providers,
    )

    assert result["completed_phase"] == "phase_2_ready"
    assert providers["claude"].author_calls == 1
    post_review_calls = [call for call in facade.route_calls if call["stage"] == "post_review"]
    assert [call["attempt"] for call in post_review_calls] == [1, 2]
    assert "post_second_review" not in [call["stage"] for call in facade.route_calls]


def test_second_needs_replan_halts_and_releases_lock(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(
        module,
        codex_reviews=[
            {"verdict": "needs-replan", "findings": [{"concern": "x"}], "outcome": "success"},
            {"verdict": "needs-replan", "findings": [{"concern": "x"}], "outcome": "success"},
        ],
        triage={"verdict": "needs-rework", "summary": "fix"},
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=providers,
    )

    assert result["status"] == "failed"
    assert facade.released is True
    assert facade.release_count == 1
    assert any(e["event"] == "run_end" and e["reason"] == "plan_review_failed" for e in facade.events)


def test_unknown_state_pauses_and_writes_runner_sidecar_only(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(
        module,
        codex_reviews=[{"verdict": "surprising", "findings": [], "outcome": "success"}],
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=providers,
    )

    assert result["status"] == "paused"
    schedule = tmp_path / "plan.schedule.json"
    sidecar = tmp_path / "plan.schedule.json.runner_state.json"
    assert sidecar.exists()
    sidecar_payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_payload["current_phase"] == "paused"
    assert set(sidecar_payload) == {"current_phase", "pause"}
    assert "plan_review_state" in json.loads(schedule.read_text(encoding="utf-8"))
    assert facade.release_count == 1


def test_stop_after_preflight_returns_before_lock_for_cli_or_config(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path)), stop_after="preflight"),
        facade=facade,
        providers=_providers(module),
    )

    assert result["completed_phase"] == "preflight"
    assert result["status"] == "completed"
    assert "acquire_lock" not in facade.calls


def test_dry_run_without_stop_after_runs_phase15(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(module)
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path)), dry_run=True),
        facade=facade,
        providers=providers,
    )

    assert result["completed_phase"] == "phase_2_ready"
    assert result["dry_run"] is True
    assert providers["codex"].plan_review_calls
    assert providers["codex"].plan_review_calls[0]["dry_run"] is True


def test_runner_config_fields_used_by_route_and_phase2_are_present(tmp_path: Path) -> None:
    module = _MODULE
    config = module.RunnerConfig(plan=str(_plan(tmp_path)), parallel=3, plan_reviewer="codex")
    assignment_plan = module.resolve_assignments(
        [{"id": "001", "files": ["src/app.py"]}],
        config,
        module.DEFAULT_PROVIDER_CAPABILITIES,
    )

    assert module._route_flags(config, assignment_plan)["skip_plan_review"] is False
    assert config.parallel == 3


def test_batch_next_failure_is_reported(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    facade.fail_batch_next = True
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=_providers(module),
    )

    assert result["status"] == "failed"
    assert result["completed_phase"] == "batch_next"
    assert result["errors"] == [{"code": "batch-next-failed"}]


def test_initial_schedule_write_preserves_existing_plan_review_state(tmp_path: Path) -> None:
    module = _MODULE
    plan = _plan(tmp_path)
    schedule = tmp_path / "plan.schedule.json"
    schedule.write_text(
        json.dumps(
            {
                "plan_review_state": {
                    "attempt": 1,
                    "source_marker": "preserved",
                    "task_plan_file_map": {"005": "existing.md"},
                }
            }
        ),
        encoding="utf-8",
    )
    facade = FakeFacade(tmp_path)

    result = module.run(
        module.RunnerConfig(plan=str(plan)),
        facade=facade,
        providers=_providers(module),
    )

    assert result["completed_phase"] == "phase_2_ready"
    state = json.loads(schedule.read_text(encoding="utf-8"))["plan_review_state"]
    assert state["source_marker"] == "preserved"
    assert state["task_plan_file_map"] == {"005": "existing.md"}


def test_paused_pre_dispatch_releases_lock(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(
        tmp_path,
        route_overrides={
            "pre_dispatch": [{"action": "unknown_state", "reason": "router could not classify"}],
        },
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=_providers(module),
    )

    assert result["status"] == "paused"
    assert facade.release_count == 1


def test_failed_after_lock_releases_lock(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    facade.fail_build = True
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=_providers(module),
    )

    assert result["status"] == "failed"
    assert result["completed_phase"] == "build_tasks"
    assert facade.release_count == 1


def test_post_plan_author_pause_is_terminal_before_second_review(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(
        tmp_path,
        route_overrides={
            "post_plan_author": [{"action": "pause_awaiting_user", "reason": "manual check"}],
        },
    )
    providers = _providers(
        module,
        codex_reviews=[
            {"verdict": "needs-replan", "findings": [{"concern": "x"}], "outcome": "success"},
            {"verdict": "approved", "findings": [], "outcome": "success"},
        ],
        triage={"verdict": "needs-rework", "summary": "fix"},
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path))),
        facade=facade,
        providers=providers,
    )

    assert result["status"] == "paused"
    assert len(providers["codex"].plan_review_calls) == 1
    assert facade.release_count == 1


def test_triage_log_source_uses_selected_review_route(tmp_path: Path) -> None:
    module = _MODULE
    facade = FakeFacade(tmp_path)
    providers = _providers(
        module,
        codex_reviews=[{"verdict": "needs-replan", "findings": [{"concern": "x"}], "outcome": "success"}],
        triage={"verdict": "ship", "summary": "ship"},
    )
    result = module.run(
        module.RunnerConfig(plan=str(_plan(tmp_path)), plan_reviewer="gemini"),
        facade=facade,
        providers=providers,
    )

    assert result["completed_phase"] == "phase_2_ready"
    triage_events = [event for event in facade.events if event["event"] == "plan_review_triage_start"]
    assert triage_events[0]["source"] == "gemini-plan-review"
