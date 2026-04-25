from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"


def load_schema(name: str) -> dict:
    with (SCRIPTS_DIR / name).open(encoding="utf-8") as schema_file:
        return json.load(schema_file)


def assert_openai_strict(schema: dict) -> None:
    if schema.get("additionalProperties") is False:
        assert set(schema["required"]) == set(schema["properties"].keys())

    for value in schema.values():
        if isinstance(value, dict):
            assert_openai_strict(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    assert_openai_strict(item)


@pytest.mark.parametrize(
    ("codex_schema_name", "gemini_schema_name"),
    [
        ("codex_review_schema.json", "gemini_review_schema.json"),
        ("codex_plan_review_schema.json", "gemini_plan_review_schema.json"),
    ],
)
def test_gemini_schema_mirrors_codex_contract(
    codex_schema_name: str,
    gemini_schema_name: str,
) -> None:
    codex_schema = load_schema(codex_schema_name)
    gemini_schema = load_schema(gemini_schema_name)

    assert_openai_strict(gemini_schema)

    assert set(gemini_schema["required"]) == set(codex_schema["required"])
    assert set(gemini_schema["properties"].keys()) == set(codex_schema["properties"].keys())
    assert set(gemini_schema["properties"]["verdict"]["enum"]) == set(
        codex_schema["properties"]["verdict"]["enum"]
    )

    codex_findings = codex_schema["properties"]["findings"]["items"]
    gemini_findings = gemini_schema["properties"]["findings"]["items"]
    assert set(gemini_findings["properties"].keys()) == set(codex_findings["properties"].keys())
    assert set(gemini_findings["required"]) == set(codex_findings["required"])
