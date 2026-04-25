"""Regression checks for the plan-implementer adaptation-flagging contract."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC = REPO_ROOT / "plugins" / "plan-executor" / "agents" / "plan-implementer.md"
SCRIPT = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_ops.py"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)


def _step_1_section() -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("### Step 1")
    end = text.index("\n### Step 2", start)
    return text[start:end]


def _plan_adaptations_report_section() -> str:
    text = SPEC.read_text(encoding="utf-8")
    start = text.index("**Plan adaptations:**")
    end = text.index("\n\n**Concerns for reviewer:**", start)
    return text[start:end]


def test_literal_wording_substitution_trigger_present():
    step_1 = _step_1_section()
    report_section = _plan_adaptations_report_section()

    assert "specific mechanism" in step_1
    assert "flag it under" in step_1
    assert "literal-wording substitutions" in report_section


def test_plan_adaptations_worked_example_present():
    step_1 = _step_1_section()

    assert "internal helper" in step_1
    assert "identical" in step_1


def test_parse_implementer_report_contract_preserved():
    report = (
        "## TASK-027B implementation report\n\n"
        "**Outcome:** success\n\n"
        "**Files changed:**\n"
        "- plugins/plan-executor/agents/plan-implementer.md\n\n"
        "**Diff summary:**\n"
        "- Updated plan adaptation wording.\n\n"
        "**Test command:** venv/bin/pytest -q tests/scripts/test_plan_implementer_spec.py\n"
        "**Test outcome:** passed\n\n"
        "**Plan adaptations:**\n"
        "- None\n\n"
        "**Concerns for reviewer:**\n"
        "- None\n"
    )
    cp = subprocess.run(
        [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
        input=report,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )

    assert cp.returncode == 0, cp.stderr
    body = json.loads(cp.stdout)
    assert body["plan_adaptations"] in (["None"], [])
