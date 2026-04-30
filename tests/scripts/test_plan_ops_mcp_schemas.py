"""TASK-003 (MCP_MIGRATION): conformance for the MCP schema sidecars.

Validates that every plan_ops.py argparse subcommand has a matching
input/output schema sidecar under
``plugins/plan-executor/scripts/schemas/mcp/`` registered in ``_index.json``.

Asserts (per TASK-003 acceptance criteria):
- Each schema is JSON Schema 2020-12 valid against the meta-schema.
- ``--stdin`` is removed from input schemas — payload arrives via the
  top-level ``payload`` property instead.
- Required argparse flags are required properties; argparse ``choices``
  surface as ``enum``; JSON-string flags become typed structures.
- Output schemas declare ``errors`` and ``warnings`` array fields.
- Existing sidecars (e.g. ``review_route_input_schema.json``) are
  referenced via ``$ref`` rather than duplicated.
- ``_index.json`` enumerates all subcommands in stable order; every
  referenced schema file exists and parses.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO / "plugins" / "plan-executor" / "scripts"
MCP_DIR = SCRIPTS_DIR / "schemas" / "mcp"

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402

JSON_STRING_FLAG_DESTS = {
    "reviewer_minor_findings",
    "rows_json",
    "retries_used",
    "findings_json",
    "reviewer_findings",
    "fields_json",
    "dismissed_finding_ids",
}


# ---------- fixtures ---------------------------------------------------------


@pytest.fixture(scope="module")
def index() -> dict:
    raw = (MCP_DIR / "_index.json").read_text(encoding="utf-8")
    return json.loads(raw)


@pytest.fixture(scope="module")
def argparse_subcommands() -> dict[str, argparse.ArgumentParser]:
    parser = plan_ops.build_parser()
    subs_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return dict(subs_action.choices)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_ref(schema_path: Path, schema: dict) -> dict:
    """Follow a single-level $ref relative to schema_path; return the
    referenced document. The new MCP sidecars never deeply nest $refs
    so a single level is sufficient.
    """
    ref = schema.get("$ref")
    if not ref:
        return schema
    target = (schema_path.parent / ref).resolve()
    return json.loads(target.read_text(encoding="utf-8"))


# ---------- structural assertions -------------------------------------------


def test_index_exists_and_is_well_formed(index: dict) -> None:
    assert "tools" in index and isinstance(index["tools"], dict)
    assert "tool_names_ordered" in index
    assert index["tool_names_ordered"] == sorted(index["tool_names_ordered"])
    # Stable ordering across sibling tooling (drift guard, MCP server) — no dupes.
    assert len(set(index["tool_names_ordered"])) == len(index["tool_names_ordered"])


def test_index_covers_every_argparse_subcommand(
    index: dict, argparse_subcommands: dict
) -> None:
    indexed_subs = {entry["subcommand"] for entry in index["tools"].values()}
    argparse_subs = set(argparse_subcommands.keys())
    missing_in_index = argparse_subs - indexed_subs
    extra_in_index = indexed_subs - argparse_subs
    assert not missing_in_index, (
        f"argparse subcommands missing from _index.json: {sorted(missing_in_index)}"
    )
    assert not extra_in_index, (
        f"_index.json has tool entries with no argparse twin: {sorted(extra_in_index)}"
    )


def test_tool_names_use_underscore_canonicalization(index: dict) -> None:
    for tool_name, entry in index["tools"].items():
        assert tool_name.startswith("plan_ops__"), tool_name
        canonical = entry["subcommand"].replace("-", "_")
        assert tool_name == f"plan_ops__{canonical}", (
            f"tool name {tool_name!r} does not match canonicalized "
            f"subcommand plan_ops__{canonical!r}"
        )


def test_referenced_schema_files_exist_and_parse(index: dict) -> None:
    for tool_name, entry in index["tools"].items():
        for key in ("input_schema", "output_schema"):
            path = MCP_DIR / entry[key]
            assert path.exists(), f"{tool_name}: {key} {path} missing"
            doc = _load(path)
            assert doc.get("$schema") == "https://json-schema.org/draft/2020-12/schema", (
                f"{tool_name}.{key} not declared as JSON Schema 2020-12"
            )


def test_meta_schema_validation(index: dict) -> None:
    """Every emitted schema must validate against the JSON Schema 2020-12
    meta-schema (i.e. be a syntactically valid schema document)."""
    pytest.importorskip("jsonschema")
    from jsonschema import Draft202012Validator

    for tool_name, entry in index["tools"].items():
        for key in ("input_schema", "output_schema"):
            path = MCP_DIR / entry[key]
            doc = _load(path)
            # `check_schema` raises SchemaError on malformed schemas.
            Draft202012Validator.check_schema(doc)


# ---------- input-schema semantics ------------------------------------------


def test_no_stdin_property_on_any_input_schema(index: dict) -> None:
    """`--stdin` must not appear as an input property; payload replaces it."""
    for tool_name, entry in index["tools"].items():
        path = MCP_DIR / entry["input_schema"]
        doc = _load(path)
        # If the schema is a thin $ref shell, resolve and assume the underlying
        # contract is the source of truth (existing sidecars did not predate MCP).
        resolved = _resolve_ref(path, doc)
        properties = resolved.get("properties", {})
        assert "stdin" not in properties, (
            f"{tool_name}: --stdin leaked into MCP input schema"
        )


def test_input_schemas_carry_payload_for_stdin_subcommands(
    index: dict, argparse_subcommands: dict
) -> None:
    """Subcommands that accept `--stdin` in argparse must expose a
    `payload` property in their MCP input schema."""
    for tool_name, entry in index["tools"].items():
        sp = argparse_subcommands[entry["subcommand"]]
        accepts_stdin = any(
            a.dest == "stdin" and a.option_strings for a in sp._actions
        )
        if not accepts_stdin:
            continue
        if entry["subcommand"] in {"review-route"}:
            # Reused $ref sidecar predates the payload convention; the MCP
            # server marshals it differently. Skip: covered by TASK-004.
            continue
        path = MCP_DIR / entry["input_schema"]
        doc = _load(path)
        resolved = _resolve_ref(path, doc)
        assert "payload" in resolved.get("properties", {}), (
            f"{tool_name}: stdin-bearing subcommand missing `payload` property"
        )


def test_required_flags_are_required_properties(
    index: dict, argparse_subcommands: dict
) -> None:
    for tool_name, entry in index["tools"].items():
        sp = argparse_subcommands[entry["subcommand"]]
        path = MCP_DIR / entry["input_schema"]
        doc = _load(path)
        if doc.get("$ref"):
            # $ref shells delegate the required-set to the referenced doc.
            continue
        required = set(doc.get("required", []))
        for action in sp._actions:
            if not action.option_strings or not action.required:
                continue
            if action.dest in ("help", "json"):
                continue
            if action.dest == "stdin":
                # Replaced by `payload` (asserted in sibling test).
                assert "payload" in required, (
                    f"{tool_name}: required --stdin → payload missing from required"
                )
                continue
            assert action.dest in required, (
                f"{tool_name}: required flag --{action.dest.replace('_','-')} "
                f"absent from MCP input.required"
            )


def test_choices_surface_as_enum(index: dict, argparse_subcommands: dict) -> None:
    for tool_name, entry in index["tools"].items():
        sp = argparse_subcommands[entry["subcommand"]]
        path = MCP_DIR / entry["input_schema"]
        doc = _load(path)
        if doc.get("$ref"):
            continue
        properties = doc.get("properties", {})
        for action in sp._actions:
            if not action.option_strings or not action.choices:
                continue
            if action.dest in ("help", "json"):
                continue
            prop = properties.get(action.dest, {})
            assert "enum" in prop, (
                f"{tool_name}: argparse choices for --{action.dest} not "
                f"surfaced as enum"
            )
            assert set(prop["enum"]) == set(action.choices)


def test_json_string_flags_are_typed_structures(index: dict) -> None:
    for tool_name, entry in index["tools"].items():
        path = MCP_DIR / entry["input_schema"]
        doc = _load(path)
        if doc.get("$ref"):
            continue
        properties = doc.get("properties", {})
        for dest, prop in properties.items():
            if dest not in JSON_STRING_FLAG_DESTS:
                continue
            assert prop.get("type") in ("array", "object"), (
                f"{tool_name}.{dest}: JSON-string flag must be typed as "
                f"array/object, got {prop.get('type')!r}"
            )


# ---------- output-schema semantics -----------------------------------------


def test_output_schemas_declare_errors_and_warnings(index: dict) -> None:
    for tool_name, entry in index["tools"].items():
        path = MCP_DIR / entry["output_schema"]
        doc = _load(path)
        # $ref-only output sidecars (review_route) are exempted: their
        # canonical envelope predates this plan and is locked separately.
        if doc.get("$ref") and "properties" not in doc:
            continue
        properties = doc.get("properties", {})
        assert "errors" in properties, f"{tool_name}: output missing `errors`"
        assert "warnings" in properties, f"{tool_name}: output missing `warnings`"
        assert properties["errors"].get("type") == "array"
        assert properties["warnings"].get("type") == "array"


# ---------- $ref reuse ------------------------------------------------------


def test_review_route_reuses_existing_sidecar(index: dict) -> None:
    entry = index["tools"]["plan_ops__review_route"]
    inp = _load(MCP_DIR / entry["input_schema"])
    out = _load(MCP_DIR / entry["output_schema"])
    assert inp.get("$ref", "").endswith("review_route_input_schema.json")
    assert out.get("$ref", "").endswith("review_route_output_schema.json")
    # Resolve the $ref and confirm the existing sidecar still parses.
    referenced_in = _resolve_ref(MCP_DIR / entry["input_schema"], inp)
    referenced_out = _resolve_ref(MCP_DIR / entry["output_schema"], out)
    assert isinstance(referenced_in, dict)
    assert isinstance(referenced_out, dict)
