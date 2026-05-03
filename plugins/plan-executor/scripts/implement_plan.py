#!/usr/bin/env python3
"""Import-safe runner contracts for the /implement-plan workflow.

This module intentionally defines data contracts and validation helpers only.
It must not import dispatch wrappers or start subprocesses at import time.
"""

from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = SCRIPT_DIR / "schemas"

SUPPORTED_PROVIDERS = frozenset({"claude", "codex", "gemini"})
RUNNER_ROLES = frozenset(
    {"classify", "implement", "review", "plan_review", "triage", "author"}
)
ROUTE_IMPLEMENTERS = frozenset({"claude", "codex"})
ROUTE_REVIEWERS = frozenset({"codex", "gemini", "claude", "none"})
UNATTENDED_REVERT_POLICIES = frozenset({"pause", "fail-fast", "preserve-only"})

TASK_ID_RE = re.compile(r"^(?:TASK-)?(?P<task_id>\d{1,3}[A-Z]?)$")
ASSIGNMENT_RE = re.compile(
    r"^(?:TASK-)?(?P<task_id>\d{1,3}[A-Z]?)=(?P<provider>claude|codex|gemini)$"
)


class RunnerContractError(ValueError):
    """Raised when a runner contract payload is internally inconsistent."""


@dataclass(frozen=True)
class TaskAssignment:
    """Provider assignment for one task id."""

    task_id: str
    provider: str


@dataclass(frozen=True)
class ProviderCapability:
    """Static capabilities advertised by one dispatch provider."""

    name: str
    roles: Mapping[str, bool]
    dispatch_command: str
    route_implementer: str | None = None
    route_reviewer: str | None = None


@dataclass(frozen=True)
class RunnerConfig:
    """Normalized runner configuration before orchestration starts."""

    plan: str
    parallel: int = 1
    provider_preference: tuple[str, ...] = ("claude", "codex", "gemini")
    assignments: tuple[TaskAssignment, ...] = ()
    reviewers: tuple[str, ...] = ("codex", "gemini", "claude")
    plan_reviewer: str | None = "codex"
    allow_provider_fallback: bool = True
    dry_run: bool = False
    skip_cross_review: bool = False
    skip_plan_review: bool = False
    unattended_revert_policy: str = "pause"
    agent_args: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerState:
    """Serializable runner state checkpoint."""

    run_id: str
    plan: str
    status: str = "pending"
    active: tuple[str, ...] = ()
    done: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()
    paused: tuple[str, ...] = ()
    committed: tuple[str, ...] = ()
    retries_used: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class DispatchResult:
    """Provider dispatch result captured by the future runner loop."""

    provider: str
    role: str
    outcome: str
    task_id: str | None = None
    envelope: Mapping[str, Any] = field(default_factory=dict)


DEFAULT_PROVIDER_CAPABILITIES: Mapping[str, ProviderCapability] = {
    "claude": ProviderCapability(
        name="claude",
        roles={
            "classify": True,
            "implement": True,
            "review": True,
            "plan_review": True,
            "triage": True,
            "author": True,
        },
        dispatch_command="plan_claude_dispatch.py",
        route_implementer="claude",
        route_reviewer="claude",
    ),
    "codex": ProviderCapability(
        name="codex",
        roles={
            "classify": False,
            "implement": True,
            "review": True,
            "plan_review": True,
            "triage": False,
            "author": False,
        },
        dispatch_command="plan_codex_dispatch.py",
        route_implementer="codex",
        route_reviewer="codex",
    ),
    "gemini": ProviderCapability(
        name="gemini",
        roles={
            "classify": False,
            "implement": False,
            "review": True,
            "plan_review": True,
            "triage": False,
            "author": False,
        },
        dispatch_command="plan_gemini_dispatch.py",
        route_implementer=None,
        route_reviewer="gemini",
    ),
}


def _schema_path(schema_name: str) -> Path:
    path = SCHEMA_DIR / schema_name
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def load_schema(schema_name: str) -> dict[str, Any]:
    """Load one runner JSON schema by filename."""

    return json.loads(_schema_path(schema_name).read_text(encoding="utf-8"))


def validate_json_schema(instance: Mapping[str, Any], schema_name: str) -> None:
    """Validate an instance with jsonschema, imported only on demand."""

    from jsonschema import Draft7Validator

    schema = load_schema(schema_name)
    Draft7Validator.check_schema(schema)
    errors = sorted(Draft7Validator(schema).iter_errors(instance), key=str)
    if errors:
        raise RunnerContractError(errors[0].message)


def normalize_task_id(task_id: str) -> str:
    match = TASK_ID_RE.match(task_id)
    if not match:
        raise RunnerContractError(f"invalid task id: {task_id!r}")
    raw = match.group("task_id")
    suffix = ""
    if raw[-1:].isalpha():
        suffix = raw[-1]
        raw = raw[:-1]
    return f"{int(raw):03d}{suffix}"


def parse_task_assignment(value: str) -> TaskAssignment:
    """Parse `TASK-001=claude` / `001=codex` assignment syntax."""

    match = ASSIGNMENT_RE.match(value)
    if not match:
        raise RunnerContractError(f"invalid task assignment: {value!r}")
    return TaskAssignment(
        task_id=normalize_task_id(match.group("task_id")),
        provider=match.group("provider"),
    )


def validate_provider_capability(capability: ProviderCapability) -> None:
    if capability.name not in SUPPORTED_PROVIDERS:
        raise RunnerContractError(f"unsupported provider: {capability.name!r}")
    unknown_roles = set(capability.roles) - RUNNER_ROLES
    if unknown_roles:
        raise RunnerContractError(f"unknown provider roles: {sorted(unknown_roles)!r}")
    if capability.route_implementer not in ROUTE_IMPLEMENTERS | {None}:
        raise RunnerContractError(
            f"unsupported route_implementer: {capability.route_implementer!r}"
        )
    if capability.route_reviewer not in ROUTE_REVIEWERS | {None}:
        raise RunnerContractError(
            f"unsupported route_reviewer: {capability.route_reviewer!r}"
        )
    if capability.roles.get("implement") and capability.route_implementer is None:
        raise RunnerContractError(
            "providers that support implement must declare route_implementer"
        )


def validate_runner_config(config: RunnerConfig) -> None:
    if config.parallel < 1:
        raise RunnerContractError("parallel must be >= 1")
    provider_fields = [
        *config.provider_preference,
        *(assignment.provider for assignment in config.assignments),
        *config.reviewers,
    ]
    if config.plan_reviewer is not None:
        provider_fields.append(config.plan_reviewer)
    unsupported = sorted(set(provider_fields) - SUPPORTED_PROVIDERS)
    if unsupported:
        raise RunnerContractError(f"unsupported providers: {unsupported!r}")
    if config.unattended_revert_policy not in UNATTENDED_REVERT_POLICIES:
        raise RunnerContractError(
            f"unsupported unattended_revert_policy: {config.unattended_revert_policy!r}"
        )


def provider_capability_payloads() -> list[dict[str, Any]]:
    """Return serializable built-in capability fixtures."""

    return [
        {
            "name": capability.name,
            "roles": dict(capability.roles),
            "dispatch_command": capability.dispatch_command,
            "route_implementer": capability.route_implementer,
            "route_reviewer": capability.route_reviewer,
        }
        for capability in DEFAULT_PROVIDER_CAPABILITIES.values()
    ]
