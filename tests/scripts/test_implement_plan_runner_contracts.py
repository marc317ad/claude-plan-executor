from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN_PATH = SCRIPT_DIR / "implement_plan.py"
SCHEMA_DIR = SCRIPT_DIR / "schemas"


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan", IMPLEMENT_PLAN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _errors(schema: dict, payload: dict) -> list:
    Draft7Validator.check_schema(schema)
    return sorted(Draft7Validator(schema).iter_errors(payload), key=str)


def test_implement_plan_imports_without_live_backends() -> None:
    module = _load_module()

    for name in [
        "RunnerConfig",
        "RunnerState",
        "ProviderCapability",
        "TaskAssignment",
        "DispatchResult",
    ]:
        assert hasattr(module, name)


def test_runner_config_schema_accepts_happy_config() -> None:
    schema = _schema("implement_plan_runner_config.json")
    payload = {
        "plan": "docs/plans/example.md",
        "parallel": 2,
        "provider_preference": ["claude", "codex", "gemini"],
        "assignments": ["TASK-001=claude", "002=codex", "TASK-003A=gemini"],
        "reviewers": ["codex", "gemini", "claude"],
        "plan_reviewer": "codex",
        "allow_provider_fallback": True,
        "dry_run": False,
        "skip_cross_review": False,
        "skip_plan_review": False,
        "unattended_revert_policy": "pause",
        "agent_args": {
            "claude": ["--permission-mode", "acceptEdits"],
            "codex": ["--model", "gpt-5.3-codex"],
            "gemini": ["--json"],
        },
    }

    assert _errors(schema, payload) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"plan": "p.md", "provider_preference": ["openai"]},
        {"plan": "p.md", "reviewers": ["openai"]},
        {"plan": "p.md", "plan_reviewer": "openai"},
    ],
)
def test_runner_config_rejects_unsupported_provider_names(payload: dict) -> None:
    schema = _schema("implement_plan_runner_config.json")

    assert _errors(schema, payload)


@pytest.mark.parametrize(
    "assignment",
    ["TASK-001:claude", "TASK-ABC=claude", "TASK-001=openai", "001="],
)
def test_runner_config_rejects_invalid_task_assignment_syntax(
    assignment: str,
) -> None:
    schema = _schema("implement_plan_runner_config.json")

    assert _errors(schema, {"plan": "p.md", "assignments": [assignment]})


def test_provider_capability_schema_declares_runner_roles_and_route_identity() -> None:
    schema = _schema("implement_plan_provider_capability.json")
    roles = schema["properties"]["roles"]["properties"]

    assert set(roles) == {
        "classify",
        "implement",
        "review",
        "plan_review",
        "triage",
        "author",
    }
    assert schema["properties"]["route_implementer"]["enum"] == [
        "claude",
        "codex",
        None,
    ]
    assert schema["properties"]["route_reviewer"]["enum"] == [
        "codex",
        "gemini",
        "claude",
        "none",
        None,
    ]


def test_provider_capability_rejects_implement_without_route_implementer() -> None:
    schema = _schema("implement_plan_provider_capability.json")
    payload = {
        "name": "claude",
        "roles": {
            "classify": True,
            "implement": True,
            "review": True,
            "plan_review": True,
            "triage": True,
            "author": True,
        },
        "dispatch_command": "plan_claude_dispatch.py",
        "route_implementer": None,
        "route_reviewer": "claude",
    }

    assert _errors(schema, payload)


def test_default_provider_capability_fixtures_match_contracts() -> None:
    module = _load_module()
    schema = _schema("implement_plan_provider_capability.json")
    payloads = {
        payload["name"]: payload for payload in module.provider_capability_payloads()
    }

    assert set(payloads) == {"claude", "codex", "gemini"}
    assert all(_errors(schema, payload) == [] for payload in payloads.values())

    gemini = payloads["gemini"]
    assert gemini["roles"]["implement"] is False
    assert gemini["roles"]["review"] is True
    assert gemini["roles"]["plan_review"] is True
    assert gemini["route_implementer"] is None
    assert gemini["route_reviewer"] == "gemini"

    claude = payloads["claude"]
    assert claude["roles"]["implement"] is True
    assert claude["roles"]["classify"] is True
    assert claude["roles"]["author"] is True
    assert claude["dispatch_command"] == "plan_claude_dispatch.py"

    codex = payloads["codex"]
    assert codex["roles"]["implement"] is True
    assert codex["roles"]["review"] is True
    assert codex["roles"]["plan_review"] is True
    assert codex["dispatch_command"] == "plan_codex_dispatch.py"


def test_python_helpers_validate_config_and_assignments() -> None:
    module = _load_module()

    assert module.parse_task_assignment("TASK-001=claude") == module.TaskAssignment(
        task_id="001",
        provider="claude",
    )
    with pytest.raises(module.RunnerContractError):
        module.parse_task_assignment("TASK-001=openai")
    with pytest.raises(module.RunnerContractError):
        module.validate_runner_config(
            module.RunnerConfig(plan="p.md", provider_preference=("openai",))
        )
