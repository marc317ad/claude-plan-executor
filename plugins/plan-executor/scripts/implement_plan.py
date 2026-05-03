#!/usr/bin/env python3
"""Import-safe runner contracts for the /implement-plan workflow.

This module intentionally defines data contracts and validation helpers only.
It must not import dispatch wrappers or start subprocesses at import time.
"""

from dataclasses import dataclass, field
import argparse
import json
import shutil
import subprocess
import sys
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = SCRIPT_DIR / "schemas"
PLAN_OPS_SCRIPT = SCRIPT_DIR / "plan_ops.py"

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
class ProviderSelection:
    """Resolved provider choice for a runner role."""

    provider: str
    route: str | None
    source: str
    fallback_from: str | None = None
    fallback_to: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class TaskAssignmentResolution:
    """Resolved implementation provider choice for one task."""

    task_id: str
    implementer: ProviderSelection


@dataclass(frozen=True)
class AssignmentPlan:
    """Complete provider routing plan computed before model dispatch."""

    tasks: tuple[TaskAssignmentResolution, ...]
    reviewer: ProviderSelection
    plan_reviewer: ProviderSelection | None
    classifier: ProviderSelection
    author: ProviderSelection
    triage: ProviderSelection

    @property
    def implementers(self) -> Mapping[str, ProviderSelection]:
        return {task.task_id: task.implementer for task in self.tasks}

    @property
    def route_implementers(self) -> Mapping[str, str | None]:
        return {
            task.task_id: task.implementer.route
            for task in self.tasks
        }

    @property
    def route_reviewer(self) -> str | None:
        return self.reviewer.route

    @property
    def route_plan_reviewer(self) -> str | None:
        return self.plan_reviewer.route if self.plan_reviewer else None

    def as_dict(self) -> dict[str, Any]:
        def selection_payload(selection: ProviderSelection) -> dict[str, Any]:
            return {
                "provider": selection.provider,
                "route": selection.route,
                "source": selection.source,
                "fallback_from": selection.fallback_from,
                "fallback_to": selection.fallback_to,
                "reason": selection.reason,
            }

        return {
            "tasks": {
                task.task_id: {
                    "implementer": selection_payload(task.implementer),
                    "route_implementer": task.implementer.route,
                }
                for task in self.tasks
            },
            "implementers": {
                task.task_id: task.implementer.provider
                for task in self.tasks
            },
            "reviewer": selection_payload(self.reviewer),
            "plan_reviewer": (
                selection_payload(self.plan_reviewer)
                if self.plan_reviewer is not None
                else None
            ),
            "classifier": selection_payload(self.classifier),
            "author": selection_payload(self.author),
            "triage": selection_payload(self.triage),
            "route_identities": {
                "implementers": dict(self.route_implementers),
                "reviewer": self.route_reviewer,
                "plan_reviewer": self.route_plan_reviewer,
            },
        }


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
    """Normalized provider transport result."""

    provider: str
    role: str
    status: str
    task_id: str | None = None
    parsed: Mapping[str, Any] | None = None
    raw_envelope: Mapping[str, Any] | None = None
    error: str | None = None

    @property
    def outcome(self) -> str:
        """Backward-compatible alias for early runner-contract tests."""

        return self.status

    @property
    def envelope(self) -> Mapping[str, Any]:
        """Backward-compatible alias for early runner-contract tests."""

        return self.raw_envelope or {}


DEFAULT_PROVIDER_CAPABILITIES: Mapping[str, ProviderCapability] = {
    "claude": ProviderCapability(
        name="claude",
        roles={
            "classify": True,
            "implement": True,
            "review": True,
            "plan_review": False,
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

STUB_PROVIDER_CAPABILITY = ProviderCapability(
    name="stub",
    roles={role: True for role in RUNNER_ROLES},
    dispatch_command="fixture",
    route_implementer="claude",
    route_reviewer="none",
)


class SubprocessRunner(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        ...


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


def _coerce_task_assignment(value: str | TaskAssignment) -> TaskAssignment:
    if isinstance(value, TaskAssignment):
        return value
    return parse_task_assignment(value)


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


def _task_id_from_payload(task: Mapping[str, Any]) -> str:
    raw_task_id = task.get("id", task.get("task_id"))
    if raw_task_id is None:
        raise RunnerContractError("task is missing id")
    return normalize_task_id(str(raw_task_id))


def _task_agent(task: Mapping[str, Any]) -> str | None:
    agent = task.get("agent")
    if agent is None:
        return None
    normalized = str(agent).strip().lower()
    if not normalized:
        return None
    if normalized not in SUPPORTED_PROVIDERS:
        raise RunnerContractError(f"unsupported task agent: {agent!r}")
    return normalized


def _validate_capability_map(
    capabilities: Mapping[str, ProviderCapability],
) -> dict[str, ProviderCapability]:
    normalized: dict[str, ProviderCapability] = {}
    for name, capability in capabilities.items():
        validate_provider_capability(capability)
        provider = str(name).strip().lower()
        if provider != capability.name:
            raise RunnerContractError(
                f"capability key {name!r} does not match provider {capability.name!r}"
            )
        normalized[provider] = capability
    return normalized


def _role_route(capability: ProviderCapability, role: str) -> str | None:
    if role == "implement":
        return capability.route_implementer
    if role in {"review", "plan_review"}:
        return capability.route_reviewer
    return capability.name


def _provider_supports_role(capability: ProviderCapability, role: str) -> bool:
    if not capability.roles.get(role, False):
        return False
    if role == "implement":
        return capability.route_implementer in ROUTE_IMPLEMENTERS
    if role in {"review", "plan_review"}:
        return capability.route_reviewer in ROUTE_REVIEWERS
    return True


def _role_failure_reason(
    provider: str,
    role: str,
    capabilities: Mapping[str, ProviderCapability],
) -> str:
    capability = capabilities.get(provider)
    if capability is None:
        return f"provider {provider!r} is unavailable"
    if not capability.roles.get(role, False):
        return f"provider {provider!r} does not support {role!r}"
    if role == "implement" and capability.route_implementer not in ROUTE_IMPLEMENTERS:
        return (
            f"provider {provider!r} cannot implement because route_implementer "
            "is missing or unsupported"
        )
    if role in {"review", "plan_review"} and capability.route_reviewer not in ROUTE_REVIEWERS:
        return (
            f"provider {provider!r} cannot route {role!r} because route_reviewer "
            "is missing or unsupported"
        )
    return f"provider {provider!r} cannot satisfy role {role!r}"


def _auto_candidates(
    *,
    role: str,
    config: RunnerConfig,
    explicit_candidates: Sequence[str] = (),
) -> tuple[str, ...]:
    if explicit_candidates:
        return tuple(explicit_candidates)
    if role == "review":
        return config.reviewers
    return config.provider_preference


def _select_provider(
    *,
    role: str,
    config: RunnerConfig,
    capabilities: Mapping[str, ProviderCapability],
    source: str,
    explicit_provider: str | None = None,
    candidates: Sequence[str] = (),
) -> ProviderSelection:
    """Resolve one role provider.

    Precedence is explicit provider, then caller-supplied candidates, then the
    role's configured auto-policy. Explicit providers are binding for role
    support: an explicit Gemini implementation request fails instead of silently
    becoming a different implementer. Provider fallback applies only when the
    assigned provider is unavailable and ``allow_provider_fallback`` is true.
    """

    if explicit_provider is not None:
        requested = explicit_provider
        capability = capabilities.get(requested)
        if capability is not None and _provider_supports_role(capability, role):
            return ProviderSelection(
                provider=requested,
                route=_role_route(capability, role),
                source=source,
            )
        reason = _role_failure_reason(requested, role, capabilities)
        if capability is not None or not config.allow_provider_fallback:
            raise RunnerContractError(reason)
        for fallback in _auto_candidates(role=role, config=config, explicit_candidates=candidates):
            fallback_capability = capabilities.get(fallback)
            if fallback == requested or fallback_capability is None:
                continue
            if _provider_supports_role(fallback_capability, role):
                return ProviderSelection(
                    provider=fallback,
                    route=_role_route(fallback_capability, role),
                    source=source,
                    fallback_from=requested,
                    fallback_to=fallback,
                    reason=reason,
                )
        raise RunnerContractError(
            f"{reason}; no fallback provider supports {role!r}"
        )

    skipped: list[str] = []
    for provider in _auto_candidates(role=role, config=config, explicit_candidates=candidates):
        capability = capabilities.get(provider)
        if capability is None:
            skipped.append(_role_failure_reason(provider, role, capabilities))
            continue
        if _provider_supports_role(capability, role):
            return ProviderSelection(
                provider=provider,
                route=_role_route(capability, role),
                source=source,
            )
        skipped.append(_role_failure_reason(provider, role, capabilities))
    detail = "; ".join(skipped) if skipped else "no providers configured"
    raise RunnerContractError(
        f"no provider supports {role!r} for {source}; {detail}"
    )


def resolve_assignments(
    tasks: Sequence[Mapping[str, Any]],
    config: RunnerConfig,
    capabilities: Mapping[str, ProviderCapability],
) -> AssignmentPlan:
    """Resolve all provider routing before dispatch.

    Implementation precedence is:
    1. CLI/config ``assignments`` entries such as ``TASK-001=codex``.
    2. Task metadata ``agent`` / ``**Agent:**``.
    3. ``provider_preference`` auto-policy, skipping unavailable or
       role-unsupported providers.
    4. Fail with a message that names the missing capability.

    ``--codex-only`` and ``--claude-only`` are normalized into config
    preferences before this function runs, so they behave as assignment
    constraints without a separate code path.
    """

    validate_runner_config(config)
    available_capabilities = _validate_capability_map(capabilities)
    tasks_by_id: dict[str, Mapping[str, Any]] = {}
    ordered_task_ids: list[str] = []
    for task in tasks:
        task_id = _task_id_from_payload(task)
        if task_id in tasks_by_id:
            raise RunnerContractError(f"duplicate task id: {task_id}")
        tasks_by_id[task_id] = task
        ordered_task_ids.append(task_id)

    explicit_assignments: dict[str, str] = {}
    for assignment in config.assignments:
        if assignment.task_id not in tasks_by_id:
            raise RunnerContractError(
                f"assignment references unknown task id: TASK-{assignment.task_id}"
            )
        if assignment.task_id in explicit_assignments:
            raise RunnerContractError(
                f"duplicate assignment for task id: TASK-{assignment.task_id}"
            )
        explicit_assignments[assignment.task_id] = assignment.provider

    resolved_tasks: list[TaskAssignmentResolution] = []
    for task_id in ordered_task_ids:
        task = tasks_by_id[task_id]
        explicit_provider = explicit_assignments.get(task_id)
        if explicit_provider is not None:
            source = "explicit"
        else:
            explicit_provider = _task_agent(task)
            source = "task-agent" if explicit_provider is not None else "provider-preference"
        resolved_tasks.append(
            TaskAssignmentResolution(
                task_id=task_id,
                implementer=_select_provider(
                    role="implement",
                    config=config,
                    capabilities=available_capabilities,
                    source=source,
                    explicit_provider=explicit_provider,
                ),
            )
        )

    reviewer = _select_provider(
        role="review",
        config=config,
        capabilities=available_capabilities,
        source="reviewers",
        candidates=config.reviewers,
    )
    plan_reviewer = (
        None
        if config.plan_reviewer is None
        else _select_provider(
            role="plan_review",
            config=config,
            capabilities=available_capabilities,
            source="plan_reviewer",
            explicit_provider=config.plan_reviewer,
        )
    )
    classifier = _select_provider(
        role="classify",
        config=config,
        capabilities=available_capabilities,
        source="provider-preference",
    )
    author = _select_provider(
        role="author",
        config=config,
        capabilities=available_capabilities,
        source="provider-preference",
    )
    triage = _select_provider(
        role="triage",
        config=config,
        capabilities=available_capabilities,
        source="provider-preference",
    )
    return AssignmentPlan(
        tasks=tuple(resolved_tasks),
        reviewer=reviewer,
        plan_reviewer=plan_reviewer,
        classifier=classifier,
        author=author,
        triage=triage,
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


def _load_plan_ops_module() -> Any:
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    import plan_ops

    return plan_ops


def _public_plan_ops_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(result).items()
        if not key.startswith("__plan_ops_")
    }


class PlanOpsFacade:
    """Narrow runner facade over the canonical ``plan_ops`` entry points."""

    def __init__(
        self,
        *,
        python: str | None = None,
        plan_ops_script: Path | None = None,
        timeout: int = 300,
        module_loader: Callable[[], Any] = _load_plan_ops_module,
    ) -> None:
        self.python = python or sys.executable
        self.plan_ops_script = plan_ops_script or PLAN_OPS_SCRIPT
        self.timeout = timeout
        self._module_loader = module_loader
        self._module: Any | None = None

    @property
    def module(self) -> Any:
        if self._module is None:
            self._module = self._module_loader()
        return self._module

    def _run_direct(self, name: str, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        func = getattr(self.module, f"_run_{name}", None)
        if not callable(func):
            raise RunnerContractError(f"plan_ops._run_{name} is not available")
        result = func(dict(payload or {}))
        if not isinstance(result, Mapping):
            raise RunnerContractError(f"plan_ops._run_{name} returned non-object result")
        return _public_plan_ops_result(result)

    def _run_subprocess_json(
        self,
        subcommand: str,
        *,
        argv: Sequence[str] = (),
        stdin_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        command = [
            self.python,
            str(self.plan_ops_script),
            subcommand,
            *argv,
            "--json",
        ]
        stdin_text = (
            json.dumps(stdin_payload) if stdin_payload is not None else None
        )
        try:
            completed = subprocess.run(
                command,
                input=stdin_text,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerContractError(
                f"plan_ops subprocess timed out after {self.timeout}s: {subcommand}"
            ) from exc

        if not completed.stdout.strip():
            payload: dict[str, Any] = {}
        else:
            try:
                parsed = json.loads(completed.stdout)
            except json.JSONDecodeError as exc:
                raise RunnerContractError(
                    f"plan_ops subprocess emitted invalid JSON for {subcommand}: "
                    f"{exc}; stderr={completed.stderr.strip()!r}"
                ) from exc
            if not isinstance(parsed, dict):
                raise RunnerContractError(
                    f"plan_ops subprocess emitted non-object JSON for {subcommand}"
                )
            payload = parsed
        if completed.returncode != 0:
            payload.setdefault("errors", [])
            payload["subprocess_returncode"] = completed.returncode
            if completed.stderr.strip():
                payload["stderr"] = completed.stderr.strip()
        return payload

    def path_info(self) -> dict[str, Any]:
        return self._run_direct("path_info")

    def preflight(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("preflight", payload)

    def decompose_plan(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("decompose_plan", payload)

    def check_plan_deps(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("check_plan_deps", payload)

    def gates(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("gates", payload)

    def acquire_lock(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("acquire_lock", payload)

    def release_lock(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("release_lock", payload)

    def build_tasks(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("build_tasks", payload)

    def write_schedule(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("write_schedule", payload)

    def batch_next(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("batch_next", payload)

    def review_route(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._run_subprocess_json(
            "review-route",
            argv=["--stdin"],
            stdin_payload=payload,
        )

    def plan_review_route(
        self,
        payload: Mapping[str, Any],
        *,
        update_schedule_state: str | None = None,
    ) -> dict[str, Any]:
        argv = ["--stdin"]
        if update_schedule_state:
            argv.extend(["--update-schedule-state", update_schedule_state])
        return self._run_subprocess_json(
            "plan-review-route",
            argv=argv,
            stdin_payload=payload,
        )

    def log_event(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("log_event", payload)

    def commit_task(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("commit_task", payload)

    def fail_task(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("fail_task", payload)

    def block_dependents(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("block_dependents", payload)

    def reconcile_batch(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("reconcile_batch", payload)

    def update_plan_header(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("update_plan_header", payload)

    def finalize_execution_log(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("finalize_execution_log", payload)

    def parse_implementer_report(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("parse_implementer_report", payload)

    def parse_plan_review_report(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("parse_plan_review_report", payload)

    def parse_plan_review_triage_report(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("parse_plan_review_triage_report", payload)

    def parse_d5_adjudication(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("parse_d5_adjudication", payload)

    def claude_envelope_extract(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("claude_envelope_extract", payload)

    def order_triage_findings(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("order_triage_findings", payload)

    def resolve_read_targets(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("resolve_read_targets", payload)

    def build_claude_dispatch_input(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("build_claude_dispatch_input", payload)

    def build_codex_dispatch_input(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("build_codex_dispatch_input", payload)

    def build_gemini_dispatch_input(self, **payload: Any) -> dict[str, Any]:
        return self._run_direct("build_gemini_dispatch_input", payload)


def _completed_to_dispatch_result(
    *,
    provider: str,
    role: str,
    completed: subprocess.CompletedProcess[str],
) -> DispatchResult:
    raw_text = completed.stdout.strip()
    raw_envelope: dict[str, Any] | None
    parsed: Mapping[str, Any] | None = None
    error: str | None = None
    if raw_text:
        try:
            loaded = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raw_envelope = None
            error = f"invalid JSON envelope: {exc}"
        else:
            if isinstance(loaded, dict):
                raw_envelope = loaded
                raw_parsed = loaded.get("parsed", loaded.get("result"))
                if isinstance(raw_parsed, Mapping):
                    parsed = raw_parsed
            else:
                raw_envelope = None
                error = "wrapper emitted non-object JSON envelope"
    else:
        raw_envelope = None
        error = "wrapper emitted no JSON envelope"

    if completed.returncode != 0:
        if completed.stderr.strip():
            error = completed.stderr.strip()
        elif raw_envelope and isinstance(raw_envelope.get("error"), str):
            error = str(raw_envelope["error"])
        elif error is None:
            error = f"wrapper exited with {completed.returncode}"

    if raw_envelope and isinstance(raw_envelope.get("status"), str):
        status = str(raw_envelope["status"])
    elif raw_envelope and isinstance(raw_envelope.get("outcome"), str):
        status = str(raw_envelope["outcome"])
    elif completed.returncode == 0 and error is None:
        status = "ok"
    else:
        status = "error"

    return DispatchResult(
        provider=provider,
        role=role,
        status=status,
        task_id=(
            str(raw_envelope.get("task_id"))
            if raw_envelope and raw_envelope.get("task_id") is not None
            else None
        ),
        parsed=parsed,
        raw_envelope=raw_envelope,
        error=error,
    )


def _run_provider_subprocess(
    command: Sequence[str],
    *,
    provider: str,
    role: str,
    timeout: int,
    runner: SubprocessRunner = subprocess.run,
    stdin_payload: Mapping[str, Any] | None = None,
) -> DispatchResult:
    """Run one wrapper command and preserve its JSON envelope."""

    stdin_text = json.dumps(stdin_payload) if stdin_payload is not None else None
    try:
        completed = runner(
            list(command),
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return DispatchResult(
            provider=provider,
            role=role,
            status="timeout",
            error=f"provider subprocess timed out after {timeout}s",
            raw_envelope={
                "provider": provider,
                "role": role,
                "timeout": timeout,
                "stdout": exc.stdout,
                "stderr": exc.stderr,
            },
        )
    return _completed_to_dispatch_result(
        provider=provider,
        role=role,
        completed=completed,
    )


class ProviderAdapter:
    """Common adapter interface over dispatch wrappers."""

    capability: ProviderCapability

    def probe(self) -> dict[str, Any]:
        raise NotImplementedError

    def classify(self, **payload: Any) -> DispatchResult:
        return self._unsupported("classify")

    def implement(self, **payload: Any) -> DispatchResult:
        return self._unsupported("implement")

    def review(self, **payload: Any) -> DispatchResult:
        return self._unsupported("review")

    def plan_review(self, **payload: Any) -> DispatchResult:
        return self._unsupported("plan_review")

    def triage(self, **payload: Any) -> DispatchResult:
        return self._unsupported("triage")

    def author(self, **payload: Any) -> DispatchResult:
        return self._unsupported("author")

    def _unsupported(self, role: str) -> DispatchResult:
        return DispatchResult(
            provider=self.capability.name,
            role=role,
            status="unsupported",
            error=f"{self.capability.name} does not support {role}",
            raw_envelope={
                "provider": self.capability.name,
                "role": role,
                "status": "unsupported",
            },
        )


class WrapperProvider(ProviderAdapter):
    """Base class for subprocess-backed provider adapters."""

    def __init__(
        self,
        *,
        python: str | None = None,
        script_dir: Path | None = None,
        timeout: int = 300,
        runner: SubprocessRunner = subprocess.run,
    ) -> None:
        self.python = python or sys.executable
        self.script_dir = script_dir or SCRIPT_DIR
        self.timeout = timeout
        self.runner = runner

    @property
    def script_path(self) -> Path:
        return self.script_dir / self.capability.dispatch_command

    def probe(self) -> dict[str, Any]:
        return {
            "provider": self.capability.name,
            "available": self.script_path.exists() and shutil.which(self.python) is not None,
            "capability": {
                "name": self.capability.name,
                "roles": dict(self.capability.roles),
                "dispatch_command": self.capability.dispatch_command,
                "route_implementer": self.capability.route_implementer,
                "route_reviewer": self.capability.route_reviewer,
            },
        }

    def _run(
        self,
        role: str,
        argv: Sequence[str],
        *,
        stdin_payload: Mapping[str, Any] | None = None,
    ) -> DispatchResult:
        return _run_provider_subprocess(
            [self.python, str(self.script_path), *argv],
            provider=self.capability.name,
            role=role,
            timeout=self.timeout,
            runner=self.runner,
            stdin_payload=stdin_payload,
        )


def _common_task_args(payload: Mapping[str, Any]) -> list[str]:
    argv = [
        "--plan-file",
        str(payload["plan_file"]),
        "--task-id",
        str(payload["task_id"]),
        "--repo-root",
        str(payload["repo_root"]),
        "--json",
    ]
    if payload.get("dry_run"):
        argv.append("--dry-run")
    if payload.get("timeout") is not None:
        argv.extend(["--timeout", str(payload["timeout"])])
    if payload.get("target_task_id") is not None:
        argv.extend(["--target-task-id", str(payload["target_task_id"])])
    if payload.get("unattended_revert_policy") is not None:
        argv.extend(["--unattended-revert-policy", str(payload["unattended_revert_policy"])])
    return argv


def _review_args(payload: Mapping[str, Any]) -> list[str]:
    argv = _common_task_args(payload)
    if payload.get("files"):
        files = payload["files"]
        if isinstance(files, str):
            files_arg = files
        else:
            files_arg = ",".join(str(path) for path in files)
        argv.extend(["--files", files_arg])
    if payload.get("review_focus"):
        argv.extend(["--review-focus", str(payload["review_focus"])])
    return argv


def _declared_files_changed(files: Any) -> list[str] | None:
    if files is None:
        return None
    if isinstance(files, str):
        declared = [part.strip() for part in files.split(",")]
    else:
        declared = [str(path).strip() for path in files]
    return [path for path in declared if path]


def _plan_review_args(payload: Mapping[str, Any]) -> list[str]:
    argv = [
        "--schedule-file",
        str(payload["schedule_file"]),
        "--repo-root",
        str(payload["repo_root"]),
        "--json",
    ]
    if payload.get("dry_run"):
        argv.append("--dry-run")
    if payload.get("timeout") is not None:
        argv.extend(["--timeout", str(payload["timeout"])])
    if payload.get("allow_gaps"):
        argv.append("--allow-gaps")
    if payload.get("unattended_revert_policy") is not None:
        argv.extend(["--unattended-revert-policy", str(payload["unattended_revert_policy"])])
    return argv


class CodexProvider(WrapperProvider):
    capability = DEFAULT_PROVIDER_CAPABILITIES["codex"]

    def implement(self, **payload: Any) -> DispatchResult:
        return self._run("implement", ["implement", *_common_task_args(payload)])

    def review(self, **payload: Any) -> DispatchResult:
        return self._run("review", ["review", *_review_args(payload)])

    def plan_review(self, **payload: Any) -> DispatchResult:
        return self._run("plan_review", ["plan-review", *_plan_review_args(payload)])


class GeminiProvider(WrapperProvider):
    capability = DEFAULT_PROVIDER_CAPABILITIES["gemini"]

    def implement(self, **payload: Any) -> DispatchResult:
        return self._unsupported("implement")

    def review(self, **payload: Any) -> DispatchResult:
        return self._run("review", ["review", *_review_args(payload)])

    def plan_review(self, **payload: Any) -> DispatchResult:
        return self._run("plan_review", ["plan-review", *_plan_review_args(payload)])


class ClaudeProvider(WrapperProvider):
    capability = DEFAULT_PROVIDER_CAPABILITIES["claude"]

    def __init__(
        self,
        *,
        plan_ops: PlanOpsFacade | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.plan_ops = plan_ops or PlanOpsFacade(python=self.python, timeout=self.timeout)

    def _claude(self, role: str, variant: str, **payload: Any) -> DispatchResult:
        build_payload: dict[str, Any] = {
            "plan_file": payload["plan_file"],
            "task_id": payload["task_id"],
            "variant": variant,
            "repo_root": payload.get("repo_root"),
            "analyst_annotations": payload.get("analyst_annotations"),
            "target_task_id": payload.get("target_task_id"),
            "starting_sha": payload.get("starting_sha"),
            "dispatch_context": payload.get("dispatch_context"),
            "run_id": payload.get("run_id"),
            "output": "-",
        }
        declared_files = _declared_files_changed(payload.get("files"))
        if declared_files is not None:
            build_payload["declared_files_changed"] = declared_files
        dispatch_input = self.plan_ops.build_claude_dispatch_input(**build_payload)
        if dispatch_input.get("ok") is False:
            error_value = dispatch_input.get("error")
            if isinstance(error_value, Mapping):
                error_message = str(error_value.get("message", error_value))
            else:
                error_message = str(error_value)
            return DispatchResult(
                provider=self.capability.name,
                role=role,
                status="error",
                raw_envelope=dispatch_input,
                error=error_message,
            )
        envelope = dispatch_input.get("envelope", dispatch_input)
        if declared_files is not None and isinstance(envelope, Mapping):
            envelope = dict(envelope)
            envelope["declared_files_changed"] = declared_files
        argv = ["run", "--input", "-", "--output", "-"]
        if payload.get("timeout") is not None:
            argv.extend(["--timeout", str(payload["timeout"])])
        if payload.get("dry_run"):
            argv.append("--dry-run")
        return self._run(role, argv, stdin_payload=envelope)

    def classify(self, **payload: Any) -> DispatchResult:
        return self._claude("classify", "analyst", **payload)

    def implement(self, **payload: Any) -> DispatchResult:
        return self._claude("implement", "default", **payload)

    def review(self, **payload: Any) -> DispatchResult:
        return self._claude("review", "role-swap", **payload)

    def plan_review(self, **payload: Any) -> DispatchResult:
        return self._unsupported("plan_review")

    def triage(self, **payload: Any) -> DispatchResult:
        return self._claude("triage", "analyst", **payload)

    def author(self, **payload: Any) -> DispatchResult:
        return self._claude("author", "rework", **payload)


class StubProvider(ProviderAdapter):
    capability = STUB_PROVIDER_CAPABILITY

    def __init__(self, *, fixtures_dir: Path | None = None) -> None:
        self.fixtures_dir = fixtures_dir or (
            SCRIPT_DIR.parents[2]
            / "tests"
            / "scripts"
            / "fixtures"
            / "implement_plan_runner"
            / "providers"
        )

    def probe(self) -> dict[str, Any]:
        return {
            "provider": "stub",
            "available": True,
            "capability": {
                "name": self.capability.name,
                "roles": dict(self.capability.roles),
                "dispatch_command": self.capability.dispatch_command,
                "route_implementer": self.capability.route_implementer,
                "route_reviewer": self.capability.route_reviewer,
            },
        }

    def _fixture(self, role: str, **payload: Any) -> DispatchResult:
        path = self.fixtures_dir / f"{role}.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw = {
                "provider": "stub",
                "role": role,
                "status": "ok",
                "task_id": payload.get("task_id"),
                "parsed": {"verdict": "stub", "findings": [], "summary": "stub fixture"},
            }
        return DispatchResult(
            provider="stub",
            role=role,
            status=str(raw.get("status", "ok")),
            task_id=str(raw.get("task_id")) if raw.get("task_id") is not None else None,
            parsed=raw.get("parsed") if isinstance(raw.get("parsed"), Mapping) else None,
            raw_envelope=raw,
            error=raw.get("error") if isinstance(raw.get("error"), str) else None,
        )

    def classify(self, **payload: Any) -> DispatchResult:
        return self._fixture("classify", **payload)

    def implement(self, **payload: Any) -> DispatchResult:
        return self._fixture("implement", **payload)

    def review(self, **payload: Any) -> DispatchResult:
        return self._fixture("review", **payload)

    def plan_review(self, **payload: Any) -> DispatchResult:
        return self._fixture("plan_review", **payload)

    def triage(self, **payload: Any) -> DispatchResult:
        return self._fixture("triage", **payload)

    def author(self, **payload: Any) -> DispatchResult:
        return self._fixture("author", **payload)


def provider_registry(
    *,
    runner: SubprocessRunner = subprocess.run,
    plan_ops: PlanOpsFacade | None = None,
    timeout: int = 300,
    include_stub: bool = True,
) -> dict[str, ProviderAdapter]:
    registry: dict[str, ProviderAdapter] = {
        "codex": CodexProvider(runner=runner, timeout=timeout),
        "gemini": GeminiProvider(runner=runner, timeout=timeout),
        "claude": ClaudeProvider(runner=runner, plan_ops=plan_ops, timeout=timeout),
    }
    if include_stub:
        registry["stub"] = StubProvider()
    return registry


def _split_csv(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _read_config_file(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunnerContractError(f"invalid config JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RunnerContractError("config file must contain a JSON object")
    return data


def _normalize_config_payload(raw: Mapping[str, Any]) -> RunnerConfig:
    payload = dict(raw)
    assignments = tuple(
        _coerce_task_assignment(value)
        for value in payload.get("assignments", payload.get("assign", []))
    )
    provider_preference = tuple(
        payload.get("provider_preference", ("claude", "codex", "gemini"))
    )
    reviewers = tuple(payload.get("reviewers", payload.get("reviewer", ("codex", "gemini", "claude"))))
    plan_reviewer = payload.get("plan_reviewer", "codex")
    if plan_reviewer == "none":
        plan_reviewer = None
    config = RunnerConfig(
        plan=str(payload["plan"]),
        parallel=int(payload.get("parallel", 1)),
        provider_preference=provider_preference,
        assignments=assignments,
        reviewers=reviewers,
        plan_reviewer=plan_reviewer,
        allow_provider_fallback=bool(payload.get("allow_provider_fallback", True)),
        dry_run=bool(payload.get("dry_run", False)),
        skip_cross_review=bool(payload.get("skip_cross_review", False)),
        skip_plan_review=bool(payload.get("skip_plan_review", False)),
        unattended_revert_policy=str(payload.get("unattended_revert_policy", "pause")),
        agent_args=payload.get("agent_args", {}),
    )
    validate_runner_config(config)
    return config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Portable CLI runner for the /implement-plan workflow.",
    )
    parser.add_argument("command_or_plan", nargs="?")
    parser.add_argument("legacy_plan", nargs="?")
    parser.add_argument("--plan")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--parallel", type=int)
    parser.add_argument("--provider-preference")
    parser.add_argument("--codex-only", action="store_true", default=False)
    parser.add_argument("--claude-only", action="store_true", default=False)
    parser.add_argument("--assign", action="append")
    parser.add_argument("--reviewer")
    parser.add_argument("--plan-reviewer")
    parser.add_argument("--allow-provider-fallback", action="store_true", default=None)
    parser.add_argument("--no-allow-provider-fallback", dest="allow_provider_fallback", action="store_false")
    parser.add_argument("--skip-cross-review", action="store_true", default=None)
    parser.add_argument("--skip-plan-review", action="store_true", default=None)
    parser.add_argument("--task-ids")
    parser.add_argument(
        "--unattended-revert-policy",
        choices=sorted(UNATTENDED_REVERT_POLICIES),
    )
    parser.add_argument(
        "--stop-after",
        choices=("preflight",),
        help=argparse.SUPPRESS,
    )
    return parser


def _merge_cli_config(args: argparse.Namespace) -> tuple[RunnerConfig, tuple[str, ...]]:
    raw: dict[str, Any] = {
        "provider_preference": ["claude", "codex", "gemini"],
        "reviewers": ["codex", "gemini", "claude"],
        "plan_reviewer": "codex",
        "allow_provider_fallback": True,
        "dry_run": False,
        "skip_cross_review": False,
        "skip_plan_review": False,
        "unattended_revert_policy": "pause",
        "parallel": 1,
    }
    raw.update(_read_config_file(args.config))

    plan = args.plan
    if args.command_or_plan == "run":
        if args.legacy_plan:
            raise RunnerContractError("run form requires --plan, not a positional plan")
    elif args.command_or_plan:
        if args.legacy_plan:
            raise RunnerContractError("unexpected extra positional argument")
        plan = plan or args.command_or_plan
    if plan:
        raw["plan"] = plan
    if "plan" not in raw:
        raise RunnerContractError("plan is required")

    if args.parallel is not None:
        raw["parallel"] = args.parallel
    if args.provider_preference is not None:
        raw["provider_preference"] = list(_split_csv(args.provider_preference) or ())
    codex_only = bool(getattr(args, "codex_only", False))
    claude_only = bool(getattr(args, "claude_only", False))
    if codex_only and claude_only:
        raise RunnerContractError("--codex-only cannot be combined with --claude-only")
    if codex_only:
        raw["provider_preference"] = ["codex"]
        raw["reviewers"] = ["codex"]
        raw["plan_reviewer"] = "codex"
    if claude_only:
        raw["provider_preference"] = ["claude"]
        raw["reviewers"] = ["claude"]
        raw["plan_reviewer"] = None
        raw["skip_plan_review"] = True
    if args.assign is not None:
        raw["assignments"] = [parse_task_assignment(value) for value in args.assign]
    if args.reviewer is not None:
        raw["reviewers"] = list(_split_csv(args.reviewer) or ())
    if args.plan_reviewer is not None:
        raw["plan_reviewer"] = args.plan_reviewer
    for key in (
        "allow_provider_fallback",
        "dry_run",
        "skip_cross_review",
        "skip_plan_review",
    ):
        value = getattr(args, key)
        if value is not None:
            raw[key] = value
    if args.unattended_revert_policy is not None:
        raw["unattended_revert_policy"] = args.unattended_revert_policy

    explicit_skip_review = args.skip_cross_review is True
    explicit_reviewer = args.reviewer is not None
    if explicit_skip_review and explicit_reviewer:
        raise RunnerContractError("--reviewer cannot be combined with --skip-cross-review")
    if args.skip_plan_review is True and args.plan_reviewer is not None:
        raise RunnerContractError("--plan-reviewer cannot be combined with --skip-plan-review")

    return _normalize_config_payload(raw), (_split_csv(args.task_ids) or ())


def _summary(
    *,
    status: str,
    run_id: str | None,
    plan_path: Path | None,
    dry_run: bool,
    completed_phase: str,
    warnings: Sequence[str] = (),
    errors: Sequence[Any] = (),
) -> dict[str, Any]:
    return {
        "status": status,
        "run_id": run_id,
        "plan_path": str(plan_path) if plan_path else None,
        "dry_run": dry_run,
        "completed_phase": completed_phase,
        "warnings": list(warnings),
        "errors": list(errors),
    }


def run(config: RunnerConfig, *, task_ids: Sequence[str] = (), stop_after: str | None = None) -> dict[str, Any]:
    facade = PlanOpsFacade()
    plan_path = Path(config.plan).expanduser().resolve()
    warnings: list[str] = []

    facade.path_info()
    preflight = facade.preflight(
        plan_file=plan_path,
        strict_branch=False,
        strict_scope=False,
        unattended_revert_policy=config.unattended_revert_policy,
    )
    warnings.extend(preflight.get("scope_warnings", []))
    run_id = preflight.get("run_id")
    if preflight.get("error"):
        return _summary(
            status="failed",
            run_id=run_id,
            plan_path=plan_path,
            dry_run=config.dry_run,
            completed_phase="preflight",
            warnings=warnings,
            errors=[preflight["error"]],
        )
    if preflight.get("errors"):
        return _summary(
            status="failed",
            run_id=run_id,
            plan_path=plan_path,
            dry_run=config.dry_run,
            completed_phase="preflight",
            warnings=warnings,
            errors=preflight["errors"],
        )
    if preflight.get("pass") is False:
        dirty_files = preflight.get("dirty_files", {})
        source_blocking = list(dirty_files.get("source_blocking", []))
        try:
            plan_rel = plan_path.relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            plan_rel = plan_path.as_posix()
        source_outside_plan = [
            path
            for path in source_blocking
            if path != plan_rel
            and path != f"{plan_rel}/"
            and not path.startswith(f"{plan_rel}/")
        ]
        if config.dry_run and not source_outside_plan:
            if source_blocking:
                warnings.append(
                    "preflight reported the fixture plan directory as dirty; "
                    "continuing because dry-run stops before lock or dispatch"
                )
        else:
            return _summary(
                status="failed",
                run_id=run_id,
                plan_path=plan_path,
                dry_run=config.dry_run,
                completed_phase="preflight",
                warnings=warnings,
                errors=[{"code": "preflight-failed", "dirty_files": dirty_files}],
            )
    if preflight.get("pass") is False and not config.dry_run:
        return _summary(
            status="failed",
            run_id=run_id,
            plan_path=plan_path,
            dry_run=config.dry_run,
            completed_phase="preflight",
            warnings=warnings,
            errors=[{"code": "preflight-failed", "dirty_files": preflight.get("dirty_files", {})}],
        )
    if config.dry_run and (stop_after == "preflight" or stop_after is None):
        return _summary(
            status="completed",
            run_id=run_id,
            plan_path=plan_path,
            dry_run=True,
            completed_phase="preflight",
            warnings=warnings,
        )
    if task_ids:
        warnings.append("task id filtering is parsed but full dispatch is not implemented in this skeleton")
    return _summary(
        status="partial",
        run_id=run_id,
        plan_path=plan_path,
        dry_run=config.dry_run,
        completed_phase="preflight",
        warnings=[*warnings, "provider dispatch loop is not implemented in this skeleton"],
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config, task_ids = _merge_cli_config(args)
        result = run(config, task_ids=task_ids, stop_after=args.stop_after)
    except RunnerContractError as exc:
        parser.exit(2, f"{parser.prog}: error: {exc}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
