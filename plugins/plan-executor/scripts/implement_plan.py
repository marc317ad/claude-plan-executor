#!/usr/bin/env python3
"""Import-safe runner contracts for the /implement-plan workflow.

This module intentionally defines data contracts and validation helpers only.
It must not import dispatch wrappers or start subprocesses at import time.
"""

from dataclasses import dataclass, field
import argparse
import json
import subprocess
import sys
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


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
        parse_task_assignment(value)
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
    if args.assign is not None:
        raw["assignments"] = args.assign
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
