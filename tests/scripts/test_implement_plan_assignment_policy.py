from __future__ import annotations

import importlib.util
import argparse
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
IMPLEMENT_PLAN = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "implement_plan.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan", IMPLEMENT_PLAN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _caps(module, *providers: str):
    return {
        provider: module.DEFAULT_PROVIDER_CAPABILITIES[provider]
        for provider in providers
    }


def _config(module, **overrides):
    values = {
        "plan": "plan.md",
        "provider_preference": ("claude", "codex", "gemini"),
        "reviewers": ("codex", "gemini", "claude"),
        "plan_reviewer": "codex",
    }
    values.update(overrides)
    return module.RunnerConfig(**values)


def _tasks():
    return [
        {"id": "001", "title": "one"},
        {"id": "002", "title": "two", "agent": "codex"},
        {"id": "003", "title": "three"},
    ]


def test_explicit_assignment_overrides_task_agent_and_auto_policy() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001", "agent": "claude"}],
        _config(
            module,
            assignments=(module.TaskAssignment("001", "codex"),),
        ),
        _caps(module, "claude", "codex", "gemini"),
    )

    assert plan.implementers["001"].provider == "codex"
    assert plan.implementers["001"].source == "explicit"
    assert plan.route_implementers["001"] == "codex"


def test_task_declared_agent_beats_provider_preference() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001", "agent": "codex"}],
        _config(module, provider_preference=("claude", "codex")),
        _caps(module, "claude", "codex", "gemini"),
    )

    assert plan.implementers["001"].provider == "codex"
    assert plan.implementers["001"].source == "task-agent"


def test_auto_assignment_skips_role_unsupported_providers() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001"}],
        _config(module, provider_preference=("gemini", "codex", "claude")),
        _caps(module, "claude", "codex", "gemini"),
    )

    assert plan.implementers["001"].provider == "codex"
    assert plan.implementers["001"].source == "provider-preference"


def test_explicit_gemini_implementation_assignment_fails() -> None:
    module = _load_module()

    with pytest.raises(module.RunnerContractError, match="does not support 'implement'"):
        module.resolve_assignments(
            [{"id": "001"}],
            _config(
                module,
                assignments=(module.TaskAssignment("001", "gemini"),),
            ),
            _caps(module, "claude", "codex", "gemini"),
        )


def test_implementation_requires_route_supported_implementer() -> None:
    module = _load_module()
    broken = module.ProviderCapability(
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
        route_implementer=None,
        route_reviewer="claude",
    )

    with pytest.raises(module.RunnerContractError, match="route_implementer"):
        module.resolve_assignments(
            [{"id": "001"}],
            _config(module),
            {"claude": broken, "codex": module.DEFAULT_PROVIDER_CAPABILITIES["codex"]},
        )


def test_review_assignment_can_select_gemini_for_review_roles() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001"}],
        _config(
            module,
            reviewers=("gemini",),
            plan_reviewer="gemini",
        ),
        _caps(module, "claude", "codex", "gemini"),
    )

    assert plan.reviewer.provider == "gemini"
    assert plan.reviewer.route == "gemini"
    assert plan.plan_reviewer is not None
    assert plan.plan_reviewer.provider == "gemini"
    assert plan.plan_reviewer.route == "gemini"


def test_unavailable_auto_provider_is_skipped() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001"}],
        _config(
            module,
            provider_preference=("codex", "claude"),
            reviewers=("claude",),
            plan_reviewer=None,
        ),
        _caps(module, "claude"),
    )

    assert plan.implementers["001"].provider == "claude"


def test_fallback_disabled_fails_when_assigned_provider_is_lost() -> None:
    module = _load_module()

    with pytest.raises(module.RunnerContractError, match="unavailable"):
        module.resolve_assignments(
            [{"id": "001", "agent": "codex"}],
            _config(
                module,
                provider_preference=("codex", "claude"),
                reviewers=("claude",),
                plan_reviewer=None,
                allow_provider_fallback=False,
            ),
            _caps(module, "claude"),
        )


def test_fallback_enabled_records_provider_loss() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001", "agent": "codex"}],
        _config(
            module,
            provider_preference=("codex", "claude"),
            reviewers=("claude",),
            plan_reviewer=None,
            allow_provider_fallback=True,
        ),
        _caps(module, "claude"),
    )

    selection = plan.implementers["001"]
    assert selection.provider == "claude"
    assert selection.fallback_from == "codex"
    assert selection.fallback_to == "claude"
    assert "unavailable" in str(selection.reason)


def test_task_ids_subset_is_resolved_from_supplied_tasks_only() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [_tasks()[1]],
        _config(module),
        _caps(module, "claude", "codex", "gemini"),
    )

    assert list(plan.implementers) == ["002"]
    assert plan.implementers["002"].provider == "codex"


def test_unknown_task_id_assignment_fails() -> None:
    module = _load_module()

    with pytest.raises(module.RunnerContractError, match="unknown task id"):
        module.resolve_assignments(
            [{"id": "001"}],
            _config(
                module,
                assignments=(module.TaskAssignment("999", "claude"),),
            ),
            _caps(module, "claude", "codex", "gemini"),
        )


def test_merge_cli_config_parses_assign_values() -> None:
    module = _load_module()
    args = argparse.Namespace(
        command_or_plan=None,
        legacy_plan=None,
        plan="plan.md",
        config=None,
        dry_run=None,
        parallel=None,
        provider_preference=None,
        codex_only=False,
        claude_only=False,
        assign=["TASK-001=codex"],
        reviewer=None,
        plan_reviewer=None,
        allow_provider_fallback=None,
        skip_cross_review=None,
        skip_plan_review=None,
        task_ids=None,
        unattended_revert_policy=None,
    )

    config, _ = module._merge_cli_config(args)

    assert config.assignments == (module.TaskAssignment("001", "codex"),)


def test_codex_only_alias_normalizes_assignment_constraints() -> None:
    module = _load_module()
    args = argparse.Namespace(
        command_or_plan=None,
        legacy_plan=None,
        plan="plan.md",
        config=None,
        dry_run=None,
        parallel=None,
        provider_preference=None,
        codex_only=True,
        claude_only=False,
        assign=None,
        reviewer=None,
        plan_reviewer=None,
        allow_provider_fallback=None,
        skip_cross_review=None,
        skip_plan_review=None,
        task_ids=None,
        unattended_revert_policy=None,
    )

    config, _ = module._merge_cli_config(args)

    assert config.provider_preference == ("codex",)
    assert config.reviewers == ("codex",)
    assert config.plan_reviewer == "codex"


def test_claude_only_alias_normalizes_assignment_constraints() -> None:
    module = _load_module()
    args = argparse.Namespace(
        command_or_plan=None,
        legacy_plan=None,
        plan="plan.md",
        config=None,
        dry_run=None,
        parallel=None,
        provider_preference=None,
        codex_only=False,
        claude_only=True,
        assign=None,
        reviewer=None,
        plan_reviewer=None,
        allow_provider_fallback=None,
        skip_cross_review=None,
        skip_plan_review=None,
        task_ids=None,
        unattended_revert_policy=None,
    )

    config, _ = module._merge_cli_config(args)

    assert config.provider_preference == ("claude",)
    assert config.reviewers == ("claude",)
    assert config.plan_reviewer is None
    assert config.skip_plan_review is True


def test_assignment_output_includes_dispatch_and_route_identities() -> None:
    module = _load_module()

    plan = module.resolve_assignments(
        [{"id": "001"}],
        _config(module),
        _caps(module, "claude", "codex", "gemini"),
    )
    payload = plan.as_dict()

    assert payload["implementers"] == {"001": "claude"}
    assert payload["reviewer"]["provider"] == "codex"
    assert payload["plan_reviewer"]["provider"] == "codex"
    assert payload["classifier"]["provider"] == "claude"
    assert payload["author"]["provider"] == "claude"
    assert payload["triage"]["provider"] == "claude"
    assert payload["route_identities"] == {
        "implementers": {"001": "claude"},
        "reviewer": "codex",
        "plan_reviewer": "codex",
    }
