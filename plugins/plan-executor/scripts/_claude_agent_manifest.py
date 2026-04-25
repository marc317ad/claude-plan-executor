"""Agent-manifest loader for ``plan_claude_dispatch.py`` (PLAN_NESTED_DISPATCH §9).

Reads the YAML frontmatter of an existing
``plugins/plan-executor/agents/<name>.md`` subagent spec and returns a
plain-dict manifest with the keys the rest of the wrapper relies on:

  - ``name``           : str
  - ``description``    : str
  - ``model``          : str
  - ``tools``          : list[str]
  - ``env_allowlist``  : list[str]   (optional in frontmatter; defaults to [])

Only the agents in :data:`DISPATCHABLE_AGENTS` are valid v1 dispatch
targets.  Any other identifier raises :class:`AgentNotDispatchable` —
this is enforced before any disk I/O so callers cannot probe arbitrary
file paths via the agent name.

If the frontmatter is missing, malformed, or violates the manifest
schema, :class:`ManifestSchemaInvalid` is raised.  Both exceptions
inherit from :class:`ManifestError` so call sites can catch the family
in one ``except`` clause.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Repo-root-relative directory holding subagent specs.
AGENTS_DIRNAME = "agents"

#: v1 dispatch allowlist — anything outside this set must be rejected at
#: the manifest-loader boundary so we never attempt to read arbitrary
#: ``plugins/plan-executor/agents/<x>.md`` files based on caller input.
DISPATCHABLE_AGENTS: FrozenSet[str] = frozenset(
    {"plan-analyst", "plan-implementer", "plan-remediator"}
)

#: Agent-name shape (defensive — only relevant for ad-hoc callers; the
#: real allowlist is :data:`DISPATCHABLE_AGENTS`).  Letters, digits, and
#: hyphens only; no path separators or dots.
_AGENT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9\-]*$")

#: Required frontmatter keys for a usable manifest.
_REQUIRED_KEYS = ("name", "description", "model", "tools")

#: Resolved at load time relative to this module's location.  Layout is
#: ``plugins/plan-executor/scripts/_claude_agent_manifest.py`` →
#: ``plugins/plan-executor/agents/<name>.md``.
_DEFAULT_AGENTS_DIR = Path(__file__).resolve().parent.parent / AGENTS_DIRNAME


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ManifestError(Exception):
    """Base class for manifest-loader errors."""


class AgentNotDispatchable(ManifestError):
    """Raised when ``name`` is not in :data:`DISPATCHABLE_AGENTS`."""


class ManifestSchemaInvalid(ManifestError):
    """Raised when the frontmatter is missing, malformed, or fails schema
    validation."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _split_frontmatter(text: str) -> str:
    """Extract the YAML frontmatter block from a Claude Code subagent spec.

    The expected layout is::

        ---
        key: value
        ...
        ---
        <body>

    Raises :class:`ManifestSchemaInvalid` when the leading or trailing
    fence is absent.
    """
    if not text.startswith("---"):
        raise ManifestSchemaInvalid(
            "agent spec is missing the leading '---' frontmatter fence"
        )
    # Allow either ``\n`` or ``\r\n`` after the opening fence.
    rest = text[3:]
    if rest.startswith("\r\n"):
        rest = rest[2:]
    elif rest.startswith("\n"):
        rest = rest[1:]
    else:
        raise ManifestSchemaInvalid(
            "agent spec leading '---' must be followed by a newline"
        )

    # The closing fence is a line containing only '---'.
    closing_re = re.compile(r"(?m)^---\s*$")
    m = closing_re.search(rest)
    if m is None:
        raise ManifestSchemaInvalid(
            "agent spec is missing the closing '---' frontmatter fence"
        )
    return rest[: m.start()]


def _normalize_tools(value: Any) -> List[str]:
    """Accept either a comma-separated string or a YAML list of strings.

    Empty / whitespace-only tokens are dropped.  Whitespace is stripped
    around each entry.  Returns ``[]`` for ``None``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        out: List[str] = []
        for entry in value:
            if not isinstance(entry, str):
                raise ManifestSchemaInvalid(
                    f"frontmatter 'tools' entries must be strings, got {entry!r}"
                )
            stripped = entry.strip()
            if stripped:
                out.append(stripped)
        return out
    raise ManifestSchemaInvalid(
        f"frontmatter 'tools' must be a string or list, got {type(value).__name__}"
    )


def _normalize_env_allowlist(value: Any) -> List[str]:
    """Same shape as ``_normalize_tools`` — comma-string or YAML list.

    Returns ``[]`` for ``None`` (deny-by-default).
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, list):
        out: List[str] = []
        for entry in value:
            if not isinstance(entry, str):
                raise ManifestSchemaInvalid(
                    f"frontmatter 'env_allowlist' entries must be strings, "
                    f"got {entry!r}"
                )
            stripped = entry.strip()
            if stripped:
                out.append(stripped)
        return out
    raise ManifestSchemaInvalid(
        f"frontmatter 'env_allowlist' must be a string or list, "
        f"got {type(value).__name__}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_agent(name: str, *, agents_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load and validate the manifest for subagent ``name``.

    Parameters
    ----------
    name
        Subagent identifier (e.g., ``"plan-implementer"``).  Must be in
        :data:`DISPATCHABLE_AGENTS`.
    agents_dir
        Override the directory that holds ``<name>.md`` specs.  Tests
        use this to stage fixtures; production callers should not pass
        it.

    Returns
    -------
    dict
        ``{"name", "description", "model", "tools", "env_allowlist"}``.
        ``tools`` is always a ``list[str]``; ``env_allowlist`` defaults
        to ``[]`` when the frontmatter does not declare it.

    Raises
    ------
    AgentNotDispatchable
        ``name`` is not in :data:`DISPATCHABLE_AGENTS`, or fails the
        defensive shape check.
    ManifestSchemaInvalid
        Frontmatter is missing, malformed, or the resulting dict is
        missing a required key / has the wrong type.
    """
    if not isinstance(name, str) or not _AGENT_NAME_RE.match(name):
        raise AgentNotDispatchable(
            f"agent name {name!r} is not a valid identifier"
        )
    if name not in DISPATCHABLE_AGENTS:
        raise AgentNotDispatchable(
            f"agent {name!r} is not in the v1 dispatch allowlist "
            f"{sorted(DISPATCHABLE_AGENTS)}"
        )

    base_dir = Path(agents_dir) if agents_dir is not None else _DEFAULT_AGENTS_DIR
    spec_path = base_dir / f"{name}.md"
    if not spec_path.is_file():
        raise ManifestSchemaInvalid(
            f"agent spec not found at {spec_path}"
        )

    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestSchemaInvalid(
            f"could not read agent spec {spec_path}: {exc}"
        ) from exc

    yaml_block = _split_frontmatter(text)
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        raise ManifestSchemaInvalid(
            f"frontmatter YAML parse error in {spec_path}: {exc}"
        ) from exc

    if parsed is None:
        raise ManifestSchemaInvalid(
            f"frontmatter is empty in {spec_path}"
        )
    if not isinstance(parsed, dict):
        raise ManifestSchemaInvalid(
            f"frontmatter must be a YAML mapping in {spec_path}, "
            f"got {type(parsed).__name__}"
        )

    for key in _REQUIRED_KEYS:
        if key not in parsed:
            raise ManifestSchemaInvalid(
                f"frontmatter in {spec_path} is missing required key {key!r}"
            )

    parsed_name = parsed["name"]
    if not isinstance(parsed_name, str) or not parsed_name:
        raise ManifestSchemaInvalid(
            f"frontmatter 'name' in {spec_path} must be a non-empty string"
        )
    if parsed_name != name:
        raise ManifestSchemaInvalid(
            f"frontmatter 'name' {parsed_name!r} in {spec_path} does not match "
            f"requested agent {name!r}"
        )

    description = parsed["description"]
    if not isinstance(description, str) or not description.strip():
        raise ManifestSchemaInvalid(
            f"frontmatter 'description' in {spec_path} must be a non-empty string"
        )

    model = parsed["model"]
    if not isinstance(model, str) or not model.strip():
        raise ManifestSchemaInvalid(
            f"frontmatter 'model' in {spec_path} must be a non-empty string"
        )

    tools = _normalize_tools(parsed["tools"])
    env_allowlist = _normalize_env_allowlist(parsed.get("env_allowlist"))

    return {
        "name": parsed_name,
        "description": description,
        "model": model,
        "tools": tools,
        "env_allowlist": env_allowlist,
    }


__all__ = [
    "AGENTS_DIRNAME",
    "DISPATCHABLE_AGENTS",
    "ManifestError",
    "AgentNotDispatchable",
    "ManifestSchemaInvalid",
    "load_agent",
]
