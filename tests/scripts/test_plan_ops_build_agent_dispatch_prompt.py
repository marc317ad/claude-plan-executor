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
    / "code-reviewer-d-claude"
)

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402


def _fixture_payload() -> dict:
    payload = json.loads((FIXTURE_DIR / "context.json").read_text(encoding="utf-8"))
    payload["context"]["plan_file"] = str((FIXTURE_DIR / "plan_file.md").resolve())
    return payload


def test_code_reviewer_d_claude_renders_to_golden() -> None:
    result = plan_ops._run_build_agent_dispatch_prompt(_fixture_payload())

    assert result["ok"] is True
    assert result["agent"] == "code-reviewer"
    assert result["model"] == "sonnet"
    assert result["prompt"] == (FIXTURE_DIR / "golden_prompt.txt").read_text(
        encoding="utf-8"
    )


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
