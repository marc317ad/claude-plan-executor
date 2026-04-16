from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
IMPLEMENT_SCHEMA = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "codex_implement_schema.json"


def test_implement_schema_matches_task_001_report_contract() -> None:
    schema = json.loads(IMPLEMENT_SCHEMA.read_text(encoding="utf-8"))
    properties = schema["properties"]

    assert properties["concerns"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert properties["plan_adaptations"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert "concerns" in schema["required"]
    assert "plan_adaptations" in schema["required"]
