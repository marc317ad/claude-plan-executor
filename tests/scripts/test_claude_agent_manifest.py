"""Tests for ``_claude_agent_manifest`` (TASK-002).

Coverage:
  * Each real subagent spec under
    ``plugins/plan-executor/agents/<name>.md`` parses and round-trips
    through :func:`load_agent`.
  * ``AgentNotDispatchable`` covers names outside the v1 allowlist
    (``plan-author``, ``plan-review-triage``, garbage strings, path
    traversal attempts).
  * ``ManifestSchemaInvalid`` covers missing fences, malformed YAML,
    missing required keys, name mismatches, and bad ``tools`` shapes.
  * ``tools`` is normalized from both comma-string and YAML-list
    frontmatter.
  * ``env_allowlist`` defaults to ``[]`` when absent.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
AGENTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "agents"

sys.path.insert(0, str(SCRIPTS_DIR))

import _claude_agent_manifest as manifest_mod  # noqa: E402
from _claude_agent_manifest import (  # noqa: E402
    DISPATCHABLE_AGENTS,
    AgentNotDispatchable,
    ManifestError,
    ManifestSchemaInvalid,
    load_agent,
)


# ---------------------------------------------------------------------------
# Real-spec round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent_name", sorted(DISPATCHABLE_AGENTS))
def test_load_agent_returns_required_keys_for_real_specs(agent_name: str) -> None:
    m = load_agent(agent_name)

    assert m["name"] == agent_name
    assert isinstance(m["description"], str) and m["description"].strip()
    assert isinstance(m["model"], str) and m["model"].strip()
    assert isinstance(m["tools"], list) and all(isinstance(t, str) for t in m["tools"])
    assert isinstance(m["env_allowlist"], list)
    assert all(isinstance(t, str) for t in m["env_allowlist"])


def test_dispatchable_agents_is_exactly_the_v1_set() -> None:
    assert DISPATCHABLE_AGENTS == frozenset(
        {"plan-analyst", "plan-implementer", "plan-remediator"}
    )


# ---------------------------------------------------------------------------
# AgentNotDispatchable surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "plan-author",          # exists on disk but not v1-dispatchable
        "plan-review-triage",   # exists on disk but not v1-dispatchable
        "plan-orchestrator",    # nonexistent
        "claude-code",          # nonexistent
        "",                     # empty
        "PLAN-IMPLEMENTER",     # case-sensitive: not in allowlist
    ],
)
def test_load_agent_rejects_non_v1_names(bad_name: str) -> None:
    with pytest.raises(AgentNotDispatchable):
        load_agent(bad_name)


@pytest.mark.parametrize(
    "bad_name",
    [
        "../etc/passwd",
        "plan/implementer",
        "plan-implementer.md",
        "plan implementer",
        "plan-implementer\x00",
    ],
)
def test_load_agent_rejects_path_traversal_and_bad_identifiers(
    bad_name: str,
) -> None:
    with pytest.raises(AgentNotDispatchable):
        load_agent(bad_name)


def test_agent_not_dispatchable_inherits_manifest_error() -> None:
    assert issubclass(AgentNotDispatchable, ManifestError)
    assert issubclass(ManifestSchemaInvalid, ManifestError)


# ---------------------------------------------------------------------------
# ManifestSchemaInvalid surface — uses tmp_path-staged fixtures
# ---------------------------------------------------------------------------


def _write_spec(tmp_path: Path, name: str, body: str) -> Path:
    """Write a synthetic spec file at ``tmp_path/<name>.md``."""
    p = tmp_path / f"{name}.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_agent_missing_file_raises_schema_invalid(tmp_path: Path) -> None:
    # No spec written.
    with pytest.raises(ManifestSchemaInvalid, match="not found"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_no_leading_fence_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        name: plan-implementer
        description: x
        model: opus
        tools: Read
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="leading"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_no_closing_fence_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: Read
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="closing"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_bad_yaml_raises(tmp_path: Path) -> None:
    body = "---\nname: [unterminated\n---\n"
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="YAML"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_empty_frontmatter_raises(tmp_path: Path) -> None:
    body = "---\n---\n\nbody\n"
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="empty"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_non_mapping_frontmatter_raises(tmp_path: Path) -> None:
    body = "---\n- foo\n- bar\n---\n\nbody\n"
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="mapping"):
        load_agent("plan-implementer", agents_dir=tmp_path)


@pytest.mark.parametrize(
    "missing_key",
    ["name", "description", "model", "tools"],
)
def test_load_agent_missing_required_key_raises(
    tmp_path: Path, missing_key: str
) -> None:
    keys = {
        "name": "plan-implementer",
        "description": "x",
        "model": "opus",
        "tools": "Read",
    }
    keys.pop(missing_key)
    body = "---\n" + "\n".join(f"{k}: {v}" for k, v in keys.items()) + "\n---\n"
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match=missing_key):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_name_mismatch_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-analyst
        description: x
        model: opus
        tools: Read
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="does not match"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_blank_description_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: "   "
        model: opus
        tools: Read
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="description"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_blank_model_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: ""
        tools: Read
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="model"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_bad_tools_type_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: 42
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="tools"):
        load_agent("plan-implementer", agents_dir=tmp_path)


def test_load_agent_tools_list_with_non_string_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools:
          - Read
          - 42
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="tools"):
        load_agent("plan-implementer", agents_dir=tmp_path)


# ---------------------------------------------------------------------------
# tools / env_allowlist normalization
# ---------------------------------------------------------------------------


def test_load_agent_normalizes_comma_string_tools(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: Read, Grep,  Glob ,Edit
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    m = load_agent("plan-implementer", agents_dir=tmp_path)
    assert m["tools"] == ["Read", "Grep", "Glob", "Edit"]


def test_load_agent_normalizes_yaml_list_tools(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools:
          - Read
          - Grep
          - Bash
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    m = load_agent("plan-implementer", agents_dir=tmp_path)
    assert m["tools"] == ["Read", "Grep", "Bash"]


def test_load_agent_env_allowlist_defaults_to_empty(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: Read
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    m = load_agent("plan-implementer", agents_dir=tmp_path)
    assert m["env_allowlist"] == []


def test_load_agent_env_allowlist_comma_string(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: Read
        env_allowlist: HOME, PATH ,CLAUDE_CODE_OAUTH_TOKEN
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    m = load_agent("plan-implementer", agents_dir=tmp_path)
    assert m["env_allowlist"] == ["HOME", "PATH", "CLAUDE_CODE_OAUTH_TOKEN"]


def test_load_agent_env_allowlist_yaml_list(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: Read
        env_allowlist:
          - HOME
          - PATH
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    m = load_agent("plan-implementer", agents_dir=tmp_path)
    assert m["env_allowlist"] == ["HOME", "PATH"]


def test_load_agent_env_allowlist_bad_type_raises(tmp_path: Path) -> None:
    body = dedent(
        """\
        ---
        name: plan-implementer
        description: x
        model: opus
        tools: Read
        env_allowlist: 99
        ---
        """
    )
    _write_spec(tmp_path, "plan-implementer", body)
    with pytest.raises(ManifestSchemaInvalid, match="env_allowlist"):
        load_agent("plan-implementer", agents_dir=tmp_path)


# ---------------------------------------------------------------------------
# Module surface sanity
# ---------------------------------------------------------------------------


def test_module_exports_expected_public_surface() -> None:
    expected = {
        "AGENTS_DIRNAME",
        "DISPATCHABLE_AGENTS",
        "ManifestError",
        "AgentNotDispatchable",
        "ManifestSchemaInvalid",
        "load_agent",
    }
    assert expected.issubset(set(manifest_mod.__all__))


def test_default_agents_dir_resolves_to_repo_layout() -> None:
    # When ``agents_dir`` is not passed, the module should resolve to the
    # repository's ``plugins/plan-executor/agents`` directory.
    m = load_agent("plan-analyst")
    # Sanity: the spec we just loaded matches the repo's spec on disk.
    text = (AGENTS_DIR / "plan-analyst.md").read_text(encoding="utf-8")
    assert m["name"] == "plan-analyst"
    assert m["description"] in text
