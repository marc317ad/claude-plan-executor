from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_PLAN = SCRIPT_DIR / "implement_plan.py"

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import plan_ops  # noqa: E402
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


def _public_extractor_result(env: dict[str, Any], *, agent: str = "plan-implementer") -> dict[str, Any]:
    """Run the canonical extractor on a wrapper envelope and strip internal markers."""
    extracted = plan_ops._run_claude_envelope_extract(
        {"stdin_text": json.dumps(env), "agent": agent}
    )
    return {key: value for key, value in extracted.items() if not key.startswith("__plan_ops_")}


def test_canonical_claude_envelope_extract_normalizes_implementer_dispatch_for_route() -> None:
    """TASK-004: canonical implementer dispatch sequence
    `build_claude_dispatch_input -> plan_claude_dispatch.py run ->
    claude_envelope_extract -> route` must normalize routing inputs through
    the extractor rather than reading raw wrapper `.status` / `.result`.
    """
    module = _load_module()
    wrapper_envelope = {
        "status": "ok",
        "result": {"outcome": "success", "report": "**Outcome:** success\n"},
        "scope": {
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
        },
    }
    runner = FakeRunner(wrapper_envelope)
    plan_ops_facade = FakePlanOps()
    provider = module.ClaudeProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=runner,
        plan_ops=plan_ops_facade,
    )

    dispatch = provider.implement(
        plan_file="/plan.md",
        task_id="001",
        repo_root="/repo",
        files=["src/example.py"],
    )

    # Step 1 — build_claude_dispatch_input is invoked before the wrapper run.
    assert plan_ops_facade.calls
    # Step 2 — plan_claude_dispatch.py run is the wrapper subprocess.
    assert runner.calls[0]["command"][1].endswith("plan_claude_dispatch.py")
    # Step 3 — wrapper envelope is normalized through claude_envelope_extract.
    extracted = _public_extractor_result(dispatch.raw_envelope or {})
    assert extracted["status"] == "ok"
    assert extracted["outcome"] == "success"
    assert extracted["scope_violation"] is False
    assert extracted["scope_misreport"] is False
    assert extracted["error"] is None
    # Step 4 — route consumes normalized fields (extractor `outcome=success`
    # is the contract that lets routing reach `commit`).
    route_payload = {
        "task_id": "001",
        "implementer": "claude",
        "implementer_envelope": {
            "status": extracted["status"],
            "outcome": extracted["outcome"],
            "scope_violation": extracted["scope_violation"],
            "scope_misreport": extracted["scope_misreport"],
            "error": extracted["error"],
        },
        "reviewer": "codex",
        "reviewer_envelope": {"verdict": "clean", "findings": [], "summary": ""},
        "d5_envelope": None,
        "retries_used": {
            "bounded_remediation": False,
            "narrow_remediation": False,
            "role_swap": False,
            "codex_fallback": False,
        },
        "flags": {"codex_review_binding": False, "skip_cross_review": False},
    }
    assert route_payload["implementer_envelope"]["outcome"] == "success"
    assert route_payload["implementer_envelope"]["scope_violation"] is False
    route = plan_ops._route_review_route(route_payload)
    assert route["action"] == "commit"


def test_canonical_claude_envelope_extract_normalizes_remediation_dispatch() -> None:
    """TASK-004: a remediation (Phase B rework) dispatch must run through the
    same `build -> wrapper -> claude_envelope_extract -> route` sequence."""
    module = _load_module()
    rework_envelope = {
        "status": "ok",
        "result": {"outcome": "partial", "report": "rework"},
        "scope": {"scope_violation_detected": False},
    }
    runner = FakeRunner(rework_envelope)
    plan_ops_facade = FakePlanOps()
    provider = module.ClaudeProvider(
        python="/py",
        script_dir=SCRIPT_DIR,
        runner=runner,
        plan_ops=plan_ops_facade,
    )

    dispatch = provider.implement(
        plan_file="/plan.md",
        task_id="001",
        repo_root="/repo",
        files=["src/example.py"],
        dispatch_context={"template": "PhaseB-rework"},
    )

    # The build call carried the remediation dispatch_context (Phase D
    # role-swap / bounded / narrow remediation paths reuse this same path).
    assert plan_ops_facade.calls[0].get("dispatch_context") == {"template": "PhaseB-rework"}
    extracted = _public_extractor_result(
        dispatch.raw_envelope or {}, agent="plan-remediator"
    )
    assert extracted["status"] == "ok"
    assert extracted["outcome"] == "partial"
    assert extracted["scope_violation"] is False
    assert extracted["error"] is None
    # Step 4 — exercise the remediation route step: a `partial` remediation
    # outcome with a still-needs-rework reviewer envelope MUST NOT route to
    # `commit`. The route consumes the normalized extractor `outcome` (here
    # `partial`) plus the canonical reviewer/d5 envelopes; on the canonical
    # remediation path (bounded_remediation already used) this lands on a
    # non-commit branch.
    route_payload = {
        "task_id": "001",
        "implementer": "claude",
        "implementer_envelope": {
            "status": extracted["status"],
            "outcome": extracted["outcome"],
            "scope_violation": extracted["scope_violation"],
            "scope_misreport": extracted["scope_misreport"],
            "error": extracted["error"],
        },
        "reviewer": "codex",
        "reviewer_envelope": {
            "verdict": "needs-rework",
            "findings": [{"id": 1}],
            "summary": "still broken",
        },
        "d5_envelope": None,
        "retries_used": {
            "bounded_remediation": True,
            "narrow_remediation": False,
            "role_swap": False,
            "codex_fallback": False,
        },
        "flags": {"codex_review_binding": False, "skip_cross_review": False},
    }
    assert route_payload["implementer_envelope"]["outcome"] == "partial"
    assert route_payload["implementer_envelope"]["scope_violation"] is False
    route = plan_ops._route_review_route(route_payload)
    assert route["action"] != "commit"


def test_claude_envelope_extract_marks_non_ok_wrapper_status_malformed_to_block_commit() -> None:
    """TASK-004: a non-`ok` wrapper status is normalized to `outcome=malformed`
    so no Phase D route can mark the task committable."""
    bad_envelope = {
        "status": "schema_invalid",
        "status_reason": "result missing schema_version",
        "result": None,
    }
    extracted = _public_extractor_result(bad_envelope)
    assert extracted["status"] == "schema_invalid"
    assert extracted["outcome"] == "malformed"
    # `success` is the only outcome a Phase D commit branch will accept; the
    # extractor returns `malformed` for any non-`ok` wrapper status, which
    # blocks commit at the route boundary.
    assert extracted["outcome"] != "success"
    assert isinstance(extracted["error"], str) and "status_reason" in extracted["error"]


def test_claude_envelope_extract_scope_violation_drives_reconcile_pause_at_dispatch_site() -> None:
    """TASK-004: scope_violation surfaces on the normalized extractor field
    (not on raw `.scope.scope_violation_detected`), and that normalized field
    is what the runner's reconcile_batch consumes to pause the run."""
    scope_envelope = {
        "status": "ok",
        "result": {"outcome": "success", "report": "x"},
        "scope": {
            "scope_violation_detected": True,
            "scope_misreport_detected": False,
            "out_of_scope_observed": True,
            "out_of_scope_untracked": ["leak.txt"],
        },
    }
    extracted = _public_extractor_result(scope_envelope)
    assert extracted["status"] == "ok"
    assert extracted["outcome"] == "success"
    assert extracted["scope_violation"] is True
    assert extracted["scope_misreport"] is False


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
