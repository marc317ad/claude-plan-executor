from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
FIXTURE_DIR = (
    REPO_ROOT
    / "tests"
    / "fixtures"
    / "agent_dispatch_prompt"
)

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402


def _fixture_payload() -> dict:
    fixture_dir = FIXTURE_DIR / "code-reviewer-d-claude"
    payload = json.loads((fixture_dir / "context.json").read_text(encoding="utf-8"))
    payload["context"]["plan_file"] = str((fixture_dir / "plan_file.md").resolve())
    return payload


def _payload_from_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name / "context.json").read_text(encoding="utf-8"))


def _golden_from_fixture(name: str) -> str:
    return (FIXTURE_DIR / name / "golden_prompt.txt").read_text(encoding="utf-8")


def test_code_reviewer_d_claude_renders_to_golden() -> None:
    result = plan_ops._run_build_agent_dispatch_prompt(_fixture_payload())

    assert result["ok"] is True
    assert result["agent"] == "code-reviewer"
    assert result["model"] == "sonnet"
    assert result["prompt"] == _golden_from_fixture("code-reviewer-d-claude")


def test_code_reviewer_d_claude_prepends_target_task_id_for_multi_heading(
    tmp_path: Path,
) -> None:
    plan_file = tmp_path / "multi.md"
    plan_file.write_text(
        """### TASK-001: First

- **Status:** pending
- **Priority:** high
- **Files:** a.py
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - First task criterion.

**Description:**
First task description.

### TASK-002: Second

- **Status:** pending
- **Priority:** high
- **Files:** b.py
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - Second task criterion.

**Description:**
Second task description.
""",
        encoding="utf-8",
    )
    payload = {
        "template_id": "code-reviewer-d-claude",
        "context": {
            "plan_file": str(plan_file.resolve()),
            "task_id": "002",
            "target_task_id": "002",
            "files_changed": ["b.py"],
        },
    }

    result = plan_ops._run_build_agent_dispatch_prompt(payload)

    assert result["ok"] is True
    assert result["prompt"].startswith(
        "Reviewing specifically `### TASK-002:` (this child plan file declares "
        "2 `### TASK-NNN:` H3 headings; read only the matching block).\n\n"
    )
    assert "**Description:**\nSecond task description." in result["prompt"]
    assert "**Acceptance criteria:**\n- Second task criterion." in result["prompt"]


def test_code_reviewer_d_claude_missing_files_changed_schema_error() -> None:
    payload = _fixture_payload()
    del payload["context"]["files_changed"]

    result = plan_ops._run_build_agent_dispatch_prompt(payload)

    assert result["ok"] is False
    assert result["errors"]


def test_schema_exposes_all_agent_dispatch_template_ids() -> None:
    schema = json.loads(
        (
            SCRIPTS_DIR
            / "schemas"
            / "mcp"
            / "build_agent_dispatch_prompt.input.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["properties"]["template_id"]["enum"] == [
        "code-reviewer-d-claude",
        "code-reviewer-d5",
        "plan-reviewer",
        "plan-author-task-targeted",
        "plan-author-schedule-level",
        "plan-author-legacy-whole-plan",
        "plan-review-triage",
        "plan-remediator-narrow",
        "plan-remediator-rescue",
    ]
    assert "oneOf" not in schema
    assert len(schema["properties"]["context"]["oneOf"]) == 9


def test_new_agent_dispatch_variants_render_to_golden() -> None:
    expected = {
        "code-reviewer-d5": ("code-reviewer", "sonnet"),
        "code-reviewer-d5/multi": ("code-reviewer", "sonnet"),
        "plan-reviewer": ("plan-reviewer", "sonnet"),
        "plan-author-task-targeted": ("plan-author", "opus"),
        "plan-author-task-targeted/multi": ("plan-author", "opus"),
        "plan-author-schedule-level": ("plan-author", "opus"),
        "plan-author-legacy-whole-plan": ("plan-author", "opus"),
        "plan-review-triage/analyst": ("plan-review-triage", "sonnet"),
        "plan-review-triage/codex": ("plan-review-triage", "sonnet"),
        "plan-remediator-narrow": ("plan-remediator", "opus"),
        "plan-remediator-narrow/multi": ("plan-remediator", "opus"),
        "plan-remediator-rescue": ("plan-remediator", "opus"),
        "plan-remediator-rescue/multi": ("plan-remediator", "opus"),
    }

    for fixture_name, (agent, model) in expected.items():
        result = plan_ops._run_build_agent_dispatch_prompt(
            _payload_from_fixture(fixture_name)
        )

        assert result["ok"] is True, fixture_name
        assert result["agent"] == agent
        assert result["model"] == model
        assert result["prompt"] == _golden_from_fixture(fixture_name)


def test_target_task_id_variants_prepend_multi_heading_injection() -> None:
    cases = {
        "code-reviewer-d5/multi": "Adjudicate specifically `### TASK-002:`",
        "plan-author-task-targeted/multi": (
            "Apply the plan-review finding specifically `### TASK-002:`"
        ),
        "plan-remediator-narrow/multi": (
            "Apply the narrow-remediation patch specifically `### TASK-002:`"
        ),
        "plan-remediator-rescue/multi": (
            "Apply the D.4 rescue specifically `### TASK-002:`"
        ),
    }

    for fixture_name, prefix in cases.items():
        result = plan_ops._run_build_agent_dispatch_prompt(
            _payload_from_fixture(fixture_name)
        )

        assert result["ok"] is True
        assert result["prompt"].startswith(prefix)


def test_plan_review_triage_source_discrimination() -> None:
    analyst = plan_ops._run_build_agent_dispatch_prompt(
        _payload_from_fixture("plan-review-triage/analyst")
    )
    codex = plan_ops._run_build_agent_dispatch_prompt(
        _payload_from_fixture("plan-review-triage/codex")
    )

    assert "Analyst gaps from `findings_for_payload`" in analyst["prompt"]
    assert "**Same-family caveat.**" in analyst["prompt"]
    assert "Plan-review findings from `findings_for_payload`" in codex["prompt"]
    assert "**Same-family caveat.**" not in codex["prompt"]


def test_new_agent_dispatch_variants_missing_required_fields_schema_errors() -> None:
    cases = {
        "code-reviewer-d5": ("wrapper_checks_json", "/context/wrapper_checks_json"),
        "plan-reviewer": ("schedule_path", "/context/schedule_path"),
        "plan-author-task-targeted": ("finding", "/context/finding"),
        "plan-author-schedule-level": ("finding", "/context/finding"),
        "plan-author-legacy-whole-plan": ("finding", "/context/finding"),
        "plan-review-triage/analyst": ("source", "/context/source"),
        "plan-remediator-narrow": ("findings_for_retry", "/context/findings_for_retry"),
        "plan-remediator-rescue": ("reviewer_source", "/context/reviewer_source"),
    }

    for fixture_name, (field, path) in cases.items():
        payload = _payload_from_fixture(fixture_name)
        del payload["context"][field]

        result = plan_ops._run_build_agent_dispatch_prompt(payload)

        assert result["ok"] is False, fixture_name
        assert {
            "code": "required-field-missing",
            "path": path,
        }.items() <= result["errors"][0].items()


def test_code_reviewer_d_claude_missing_target_task_id_for_multi_heading(
    tmp_path: Path,
) -> None:
    plan_file = tmp_path / "multi.md"
    plan_file.write_text(
        """### TASK-001: First

- **Acceptance criteria:**
  - First criterion.

**Description:**
First description.

### TASK-002: Second

- **Acceptance criteria:**
  - Second criterion.

**Description:**
Second description.
""",
        encoding="utf-8",
    )
    payload = {
        "template_id": "code-reviewer-d-claude",
        "context": {
            "plan_file": str(plan_file.resolve()),
            "task_id": "001",
            "files_changed": ["a.py"],
        },
    }

    result = plan_ops._run_build_agent_dispatch_prompt(payload)

    assert result["ok"] is False
    assert result["errors"][0]["code"] == "target-task-id-required"
