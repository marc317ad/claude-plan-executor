from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
IMPLEMENT_SCHEMA = SCRIPT_DIR / "codex_implement_schema.json"
WRAPPER_SCHEMA_FILES = sorted(
    list(SCRIPT_DIR.glob("codex_*_schema.json"))
    + list(SCRIPT_DIR.glob("gemini_*_schema.json"))
)


def _walk_schema_objects(node: dict, pointer: str = "$"):
    yield pointer, node

    properties = node.get("properties")
    if isinstance(properties, dict):
        for key, child in properties.items():
            if isinstance(child, dict):
                yield from _walk_schema_objects(child, f"{pointer}.properties.{key}" if pointer != "$" else f"properties.{key}")

    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_schema_objects(items, f"{pointer}.items" if pointer != "$" else "items")


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


@pytest.mark.parametrize("schema_path", WRAPPER_SCHEMA_FILES, ids=lambda path: path.name)
def test_wrapper_schemas_required_matches_properties(schema_path: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    for pointer, node in _walk_schema_objects(schema):
        if node.get("type") == "object" and node.get("additionalProperties") is False:
            required = set(node["required"])
            properties = set(node["properties"].keys())
            assert required == properties, (
                f"{schema_path}: {pointer}: required={required} "
                f"!= properties keys={properties}"
            )
