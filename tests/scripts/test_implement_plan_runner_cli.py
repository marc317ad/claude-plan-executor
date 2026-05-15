from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN = SCRIPT_DIR / "implement_plan.py"
FIXTURE_PLAN = REPO_ROOT / "tests" / "scripts" / "fixtures" / "implement_plan_runner"


def _load_module():
    spec = importlib.util.spec_from_file_location("implement_plan", IMPLEMENT_PLAN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(IMPLEMENT_PLAN), *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_supports_positional_plan_dry_run_preflight() -> None:
    completed = _run_cli(
        str(FIXTURE_PLAN),
        "--dry-run",
        "--stop-after",
        "preflight",
        "--unattended-revert-policy",
        "pause",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload) == {
        "status",
        "run_id",
        "plan_path",
        "dry_run",
        "completed_phase",
        "warnings",
        "errors",
    }
    assert payload["status"] == "completed"
    assert payload["plan_path"] == str(FIXTURE_PLAN.resolve())
    assert payload["dry_run"] is True
    assert payload["completed_phase"] == "preflight"
    assert payload["errors"] == []
    assert payload["run_id"]


def test_cli_supports_run_subcommand_plan_form() -> None:
    completed = _run_cli(
        "run",
        "--plan",
        str(FIXTURE_PLAN),
        "--dry-run",
        "--stop-after",
        "preflight",
        "--unattended-revert-policy",
        "pause",
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["status"] == "completed"
    assert payload["completed_phase"] == "preflight"


def test_facade_exposes_required_plan_ops_methods() -> None:
    module = _load_module()
    required = {
        "path_info",
        "preflight",
        "decompose_plan",
        "check_plan_deps",
        "gates",
        "acquire_lock",
        "release_lock",
        "build_tasks",
        "write_schedule",
        "batch_next",
        "review_route",
        "plan_review_route",
        "log_event",
        "commit_task",
        "fail_task",
        "block_dependents",
        "reconcile_batch",
        "update_plan_header",
        "finalize_execution_log",
        "parse_implementer_report",
        "parse_plan_review_report",
        "parse_plan_review_triage_report",
        "parse_d5_adjudication",
        "claude_envelope_extract",
        "order_triage_findings",
        "resolve_read_targets",
        "build_claude_dispatch_input",
        "build_codex_dispatch_input",
        "build_gemini_dispatch_input",
    }

    facade = module.PlanOpsFacade()

    assert required <= {name for name in dir(facade) if not name.startswith("_")}


def test_facade_uses_direct_pure_core_for_builder_command() -> None:
    module = _load_module()
    facade = module.PlanOpsFacade()

    result = facade.build_tasks(plans_dir=FIXTURE_PLAN)

    assert result["ok"] is True
    assert [task["id"] for task in result["tasks"]] == ["001"]


def test_facade_uses_subprocess_fallback_for_stdin_route_command() -> None:
    module = _load_module()
    facade = module.PlanOpsFacade(timeout=30)
    payload = {
        "task_id": "001",
        "implementer": "claude",
        "reviewer_envelope": {
            "verdict": "clean",
            "findings": [],
            "summary": "",
        },
        "d5_envelope": None,
        "retries_used": {
            "bounded_remediation": False,
            "narrow_remediation": False,
            "role_swap": False,
            "codex_fallback": False,
        },
        "flags": {
            "codex_review_binding": False,
            "skip_cross_review": False,
        },
    }

    result = facade.review_route(payload)

    assert result["action"] == "commit"


def test_invalid_flag_combinations_fail_before_lock_acquisition(monkeypatch) -> None:
    module = _load_module()

    def fail_if_called(*args, **kwargs):
        raise AssertionError("lock should not be acquired during CLI validation")

    monkeypatch.setattr(module.PlanOpsFacade, "acquire_lock", fail_if_called)
    completed = _run_cli(
        str(FIXTURE_PLAN),
        "--reviewer",
        "codex",
        "--skip-cross-review",
    )

    assert completed.returncode == 2
    assert "--reviewer cannot be combined with --skip-cross-review" in completed.stderr


def test_config_file_merge_precedence_defaults_config_then_cli(tmp_path: Path) -> None:
    module = _load_module()
    config_path = tmp_path / "runner.json"
    config_path.write_text(
        json.dumps(
            {
                "plan": "from-config",
                "parallel": 2,
                "provider_preference": ["codex"],
                "reviewers": ["gemini"],
                "dry_run": False,
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        command_or_plan=None,
        legacy_plan=None,
        plan=str(FIXTURE_PLAN),
        config=str(config_path),
        dry_run=True,
        parallel=4,
        provider_preference="gemini,codex",
        assign=["TASK-001=claude"],
        reviewer=None,
        plan_reviewer=None,
        allow_provider_fallback=None,
        skip_cross_review=None,
        skip_plan_review=None,
        task_ids="001",
        unattended_revert_policy=None,
    )

    config, task_ids = module._merge_cli_config(args)

    assert config.plan == str(FIXTURE_PLAN)
    assert config.parallel == 4
    assert config.provider_preference == ("gemini", "codex")
    assert config.reviewers == ("gemini",)
    assert config.assignments == (
        module.TaskAssignment(task_id="001", provider="claude"),
    )
    assert config.dry_run is True
    assert config.plan_reviewer == "codex"
    assert config.unattended_revert_policy == "pause"
    assert task_ids == ("001",)
