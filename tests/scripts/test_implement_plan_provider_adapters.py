from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN = SCRIPT_DIR / "implement_plan.py"
FIXTURES_DIR = (
    REPO_ROOT
    / "tests"
    / "scripts"
    / "fixtures"
    / "implement_plan_runner"
    / "providers"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan", IMPLEMENT_PLAN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    def __init__(self, envelope: dict[str, Any] | None = None) -> None:
        self.envelope = envelope or {
            "task_id": "001",
            "outcome": "success",
            "parsed": {"verdict": "clean", "findings": [], "summary": "ok"},
        }
        self.calls: list[dict[str, Any]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append({"command": command, **kwargs})
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(self.envelope),
            stderr="",
        )


class FakePlanOps:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def build_claude_dispatch_input(self, **payload: Any) -> dict[str, Any]:
        self.calls.append(payload)
        declared_files = payload.get("declared_files_changed", ["src/example.py"])
        return {
            "ok": True,
            "envelope": {
                "schema_version": 3,
                "agent": "plan-implementer",
                "declared_files_changed": declared_files,
                "payload": {"task_id": payload["task_id"]},
            },
        }


class FakePlanOpsError:
    def __init__(self, error: Any) -> None:
        self.error = error

    def build_claude_dispatch_input(self, **payload: Any) -> dict[str, Any]:
        return {"ok": False, "error": self.error}


def test_codex_implement_command_includes_target_task_id() -> None:
    module = _load_module()
    runner = FakeRunner()
    provider = module.CodexProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=runner,
    )

    result = provider.implement(
        plan_file="/plan.md",
        task_id="001",
        repo_root="/repo",
        target_task_id="003",
    )

    command = runner.calls[0]["command"]
    assert command[:3] == [
        "/py",
        str(SCRIPT_DIR / "plan_codex_dispatch.py"),
        "implement",
    ]
    assert command[command.index("--target-task-id") + 1] == "003"
    assert result.provider == "codex"
    assert result.role == "implement"
    assert result.raw_envelope == runner.envelope


def test_codex_and_gemini_plan_review_forward_allow_gaps() -> None:
    module = _load_module()
    codex_runner = FakeRunner()
    gemini_runner = FakeRunner()

    module.CodexProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=codex_runner,
    ).plan_review(schedule_file="/schedule.json", repo_root="/repo", allow_gaps=True)
    module.GeminiProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=gemini_runner,
    ).plan_review(schedule_file="/schedule.json", repo_root="/repo", allow_gaps=True)

    assert codex_runner.calls[0]["command"][:3] == [
        "/py",
        str(SCRIPT_DIR / "plan_codex_dispatch.py"),
        "plan-review",
    ]
    assert "--allow-gaps" in codex_runner.calls[0]["command"]
    assert gemini_runner.calls[0]["command"][:3] == [
        "/py",
        str(SCRIPT_DIR / "plan_gemini_dispatch.py"),
        "plan-review",
    ]
    assert "--allow-gaps" in gemini_runner.calls[0]["command"]


def test_gemini_implementation_is_adapter_unsupported() -> None:
    module = _load_module()
    runner = FakeRunner()
    result = module.GeminiProvider(runner=runner).implement(
        plan_file="/plan.md",
        task_id="001",
        repo_root="/repo",
    )

    assert result.status == "unsupported"
    assert result.raw_envelope == {
        "provider": "gemini",
        "role": "implement",
        "status": "unsupported",
    }
    assert runner.calls == []


def test_claude_uses_build_input_and_pipes_envelope_to_wrapper() -> None:
    module = _load_module()
    runner = FakeRunner(
        {
            "status": "ok",
            "result": {"status": "completed", "files_changed": ["src/example.py"]},
        },
    )
    plan_ops = FakePlanOps()
    provider = module.ClaudeProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=runner,
        plan_ops=plan_ops,
    )

    result = provider.implement(
        plan_file="/plan.md",
        task_id="001",
        repo_root="/repo",
        target_task_id="003",
        files=["src/example.py"],
    )

    assert plan_ops.calls[0]["variant"] == "default"
    assert plan_ops.calls[0]["target_task_id"] == "003"
    assert plan_ops.calls[0]["declared_files_changed"] == ["src/example.py"]
    assert runner.calls[0]["command"][:3] == [
        "/py",
        str(SCRIPT_DIR / "plan_claude_dispatch.py"),
        "run",
    ]
    stdin_payload = json.loads(runner.calls[0]["input"])
    assert stdin_payload["declared_files_changed"] == ["src/example.py"]
    assert result.parsed == {
        "status": "completed",
        "files_changed": ["src/example.py"],
    }


def test_completed_to_dispatch_result_preserves_stderr_without_json() -> None:
    module = _load_module()
    completed = subprocess.CompletedProcess(
        ["/wrapper"],
        1,
        stdout="",
        stderr="Traceback: boom",
    )

    result = module._completed_to_dispatch_result(
        provider="codex",
        role="review",
        completed=completed,
    )

    assert result.status == "error"
    assert result.error == "Traceback: boom"


def test_claude_build_input_string_error_returns_dispatch_error() -> None:
    module = _load_module()
    runner = FakeRunner()
    provider = module.ClaudeProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=runner,
        plan_ops=FakePlanOpsError("plan ops failed"),
    )

    result = provider.implement(
        plan_file="/plan.md",
        task_id="001",
        repo_root="/repo",
    )

    assert result.status == "error"
    assert result.error == "plan ops failed"
    assert runner.calls == []


def test_claude_plan_review_is_schedule_based_and_does_not_require_task_keys() -> None:
    module = _load_module()
    runner = FakeRunner()
    plan_ops = FakePlanOps()
    provider = module.ClaudeProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=runner,
        plan_ops=plan_ops,
    )

    result = provider.plan_review(schedule_file="/schedule.json", repo_root="/repo")

    assert result.status == "unsupported"
    assert result.role == "plan_review"
    assert plan_ops.calls == []
    assert runner.calls == []


def test_probe_reports_capabilities_without_dispatching() -> None:
    module = _load_module()
    runner = FakeRunner()
    provider = module.CodexProvider(runner=runner)

    probe = provider.probe()

    assert probe["provider"] == "codex"
    assert probe["capability"]["route_implementer"] == "codex"
    assert probe["capability"]["route_reviewer"] == "codex"
    assert runner.calls == []


def test_stub_provider_supports_all_roles_from_fixtures() -> None:
    module = _load_module()
    provider = module.StubProvider(fixtures_dir=FIXTURES_DIR)

    assert provider.probe()["available"] is True
    assert provider.implement(task_id="001").parsed["summary"] == "stub implement"
    assert provider.review(task_id="001").parsed["verdict"] == "clean"
    assert provider.plan_review(task_id="001").parsed["verdict"] == "approved"
    assert provider.classify(task_id="001").status == "ok"
    assert provider.triage(task_id="001").status == "ok"
    assert provider.author(task_id="001").status == "ok"
