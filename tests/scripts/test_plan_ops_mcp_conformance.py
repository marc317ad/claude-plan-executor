"""MCP <-> CLI conformance tests for registered ``plan_ops`` tools.

TASK-016 covers the transport migration layer: every tool advertised by
``schemas/mcp/_index.json`` must have at least one fixture and the MCP
stdio path must emit the same public JSON envelope as the bash CLI path.
State-mutating fixtures run in paired temporary repositories/directories
so the filesystem deltas, run log, and lock file can be compared too.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.scripts.plan_ops_pure_harness import (
    SCRIPTS_DIR,
    _TIMESTAMP_RE,
    _TIMESTAMP_SENTINEL,
    canonicalize_envelope,
    cli_argv_from,
)
from tests.scripts.test_plan_ops_pure_entrypoints_tier_c import (
    _GIT_ENV_PIN,
    _build_cli_argv as _build_tier_c_cli_argv,
    _seed_git_repo,
    _seed_plans_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "plan-executor"
SERVER_PATH = PLUGIN_ROOT / "scripts" / "plan_ops_mcp_server.py"
MCP_INDEX_PATH = PLUGIN_ROOT / "scripts" / "schemas" / "mcp" / "_index.json"
PURE_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "plan_ops_pure_core"
SEMANTIC_INVALID_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_conformance" / "semantic_invalid"
)

_mcp_available = importlib.util.find_spec("mcp") is not None
requires_mcp = pytest.mark.skipif(not _mcp_available, reason="mcp SDK not installed")
_SERVER_MODULE: Any | None = None

SIDE_EFFECT_SUBCOMMANDS = {
    "commit-task",
    "fail-task",
    "log-event",
    "block-dependents",
    "update-plan-header",
    "acquire-lock",
    "release-lock",
    "write-schedule",
    "finalize-execution-log",
    "build-claude-dispatch-input",
    "build-codex-dispatch-input",
    "build-gemini-dispatch-input",
    "auto-validate-divergence",
    "decompose-plan",
    "reconcile-batch",
}

TIER_C_SUBCOMMANDS = {
    "acquire-lock",
    "auto-validate-divergence",
    "block-dependents",
    "commit-task",
    "decompose-plan",
    "fail-task",
    "log-event",
    "reconcile-batch",
    "release-lock",
}

PATH_KEYS = {
    "plan_file",
    "out_dir",
    "schedule_file",
    "envelope_file",
    "repo_root",
    "update_schedule_state",
    "plans_dir",
    "report_file",
    "output",
}

HARNESS_ONLY_KEYS = {
    "_files",
    "_seed_plans_dir",
    "_seed_git_repo",
    "_git_initial_files",
}


def _load_index() -> dict[str, Any]:
    return json.loads(MCP_INDEX_PATH.read_text(encoding="utf-8"))


def _subcommand_to_tool_name(subcommand: str) -> str:
    return subcommand.replace("-", "_")


def _load_server_module():
    global _SERVER_MODULE
    if _SERVER_MODULE is not None:
        return _SERVER_MODULE
    spec = importlib.util.spec_from_file_location("plan_ops_mcp_server", SERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _SERVER_MODULE = module
    return module


def _tool_name_to_subcommand(tool_name: str) -> str:
    tools = _load_index()["tools"]
    return tools[f"plan_ops__{tool_name}"]["subcommand"]


def _resolve_template(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace("{FIXTURE_DIR}", str(root)).replace(
            "{REPO_ROOT}", str(REPO_ROOT)
        )
    if isinstance(value, list):
        return [_resolve_template(v, root) for v in value]
    if isinstance(value, dict):
        if set(value) == {"__path__"}:
            return Path(_resolve_template(value["__path__"], root))
        return {k: _resolve_template(v, root) for k, v in value.items()}
    return value


def _fixture_docs_for(subcommand: str) -> list[tuple[str, Path]]:
    docs: list[tuple[str, Path]] = []
    for tier in ("tier_a", "tier_b", "tier_c"):
        for path in sorted((PURE_FIXTURE_ROOT / tier).glob(f"{subcommand}__*.payload.json")):
            docs.append((tier, path))
    semantic = SEMANTIC_INVALID_DIR / f"{subcommand}.json"
    if semantic.is_file():
        docs.append(("semantic_invalid", semantic))
    return docs


def _all_fixture_params() -> list[tuple[str, str, Path]]:
    params: list[tuple[str, str, Path]] = []
    missing: list[str] = []
    for tool_name in _load_index()["tool_names_ordered"]:
        subcommand = _tool_name_to_subcommand(tool_name)
        docs = _fixture_docs_for(subcommand)
        if not docs:
            missing.append(subcommand)
            continue
        params.extend((subcommand, group, path) for group, path in docs)
    assert not missing, (
        "MCP conformance fixture coverage missing for _index.json tools: "
        + ", ".join(missing)
    )
    return params


FIXTURE_PARAMS = _all_fixture_params()


def _materialize_files(root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _prepare_root(root: Path, payload: dict[str, Any]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if payload.get("_seed_plans_dir"):
        _seed_plans_dir(root)
    if payload.get("_seed_git_repo"):
        _seed_git_repo(root, payload.get("_git_initial_files") or {})
    if payload.get("_files"):
        _materialize_files(root, payload["_files"])


def _payload_for_root(payload: dict[str, Any], root: Path, *, tier: str) -> dict[str, Any]:
    resolved = _resolve_template(payload, root)
    out: dict[str, Any] = {}
    for key, value in resolved.items():
        if key in HARNESS_ONLY_KEYS:
            continue
        if tier == "tier_c" and isinstance(value, str) and key in PATH_KEYS:
            out[key] = str(root / value)
        else:
            out[key] = value
    return out


def _expected_path_for_payload(fixture_path: Path) -> Path | None:
    if not fixture_path.name.endswith(".payload.json"):
        return None
    expected = fixture_path.with_name(
        fixture_path.name[: -len(".payload.json")] + ".expected.json"
    )
    return expected if expected.is_file() else None


def _cli_argv(
    subcommand: str,
    payload: dict[str, Any],
    *,
    tier: str,
    expected: dict[str, Any],
    root: Path,
) -> list[str]:
    if "cli_argv" in expected:
        return [str(_resolve_template(item, root)) for item in expected["cli_argv"]]
    if tier == "tier_c":
        return _build_tier_c_cli_argv(subcommand, payload)
    return cli_argv_from(subcommand, payload)


def _run_cli(
    subcommand: str,
    payload: dict[str, Any],
    *,
    tier: str,
    expected: dict[str, Any],
    cwd: Path,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = _cli_argv(subcommand, payload, tier=tier, expected=expected, root=cwd)
    stdin_text = payload.get("stdin_text", "") or ""
    env = os.environ.copy()
    env.pop("UNATTENDED_REVERT_POLICY", None)
    if expected.get("fixed_now"):
        env["PLAN_OPS_FIXED_NOW"] = expected["fixed_now"]
    if env_extra:
        env.update(env_extra)
    run_cwd = cwd if subcommand in SIDE_EFFECT_SUBCOMMANDS else SCRIPTS_DIR
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "plan_ops.py"), subcommand, *argv],
        input=stdin_text,
        cwd=str(run_cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _mcp_arguments(subcommand: str, payload: dict[str, Any]) -> dict[str, Any]:
    if subcommand == "review-route":
        if "stdin_text" in payload:
            text = payload.get("stdin_text", "") or ""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"payload": text}
        return {
            key: value
            for key, value in payload.items()
            if key not in {"stdin", "json"}
        }
    args = {
        key: value
        for key, value in payload.items()
        if key not in {"stdin", "stdin_text", "json"}
    }
    if subcommand == "gates" and args.get("certify") and args.get("certify_mode"):
        args["mode"] = args.pop("certify_mode")
    if payload.get("stdin") or "stdin_text" in payload:
        text = payload.get("stdin_text", "") or ""
        try:
            args["payload"] = json.loads(text)
        except json.JSONDecodeError:
            args["payload"] = text
    return args


async def _call_mcp_async(
    subcommand: str,
    arguments: dict[str, Any],
    *,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
) -> Any:
    """Drive the MCP dispatch path with the same server registry.

    The stdio client is covered by the scaffolding suite; in this
    repository's sandbox that client can block during teardown, so the
    conformance matrix calls the registered server dispatcher directly.
    This still exercises the MCP registry, JSON-schema-shaped argument
    mapping, and ``CallToolResult`` error shape without paying one
    subprocess handshake per fixture.
    """
    server = _load_server_module()
    target_cwd = cwd if subcommand in SIDE_EFFECT_SUBCOMMANDS else SCRIPTS_DIR
    saved_cwd = os.getcwd()
    saved_env: dict[str, str | None] = {}
    if env_extra:
        for key, value in env_extra.items():
            saved_env[key] = os.environ.get(key)
            os.environ[key] = value
    try:
        os.chdir(str(target_cwd))
        return await server._dispatch_registered_tool(
            f"plan_ops__{_subcommand_to_tool_name(subcommand)}",
            arguments,
        )
    finally:
        os.chdir(saved_cwd)
        for key, prior in saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


def _run_mcp(
    subcommand: str,
    arguments: dict[str, Any],
    *,
    cwd: Path,
    env_extra: dict[str, str] | None = None,
) -> Any:
    return asyncio.run(_call_mcp_async(subcommand, arguments, cwd=cwd, env_extra=env_extra))


def _structured_from_call_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return _public_result(structured)
    if isinstance(result, dict):
        return _public_result(result)
    content = getattr(result, "content", None) or []
    if content and hasattr(content[0], "text"):
        return _public_result(json.loads(content[0].text))
    raise AssertionError(f"cannot extract structured MCP result from {result!r}")


def _public_result(envelope: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in envelope.items() if not k.startswith("__plan_ops_")}


def _canon_text(text: str | None, prefixes: tuple[str, ...]) -> str | None:
    if text is None:
        return None
    out = text
    out = _TIMESTAMP_RE.sub(_TIMESTAMP_SENTINEL, out)
    for prefix in prefixes:
        out = out.replace(prefix, "<FIXTURE_DIR>")
    return out


def _canon_envelope(envelope: Any, prefixes: tuple[str, ...]) -> str:
    out = canonicalize_envelope(envelope)
    for prefix in prefixes:
        out = out.replace(prefix, "<FIXTURE_DIR>")
    return out


def _snapshot(root: Path, prefixes: tuple[str, ...]) -> dict[str, str]:
    snap: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.name.endswith(".tmp"):
            continue
        rel = str(path.relative_to(root))
        try:
            snap[rel] = _canon_text(path.read_text(encoding="utf-8"), prefixes) or ""
        except UnicodeDecodeError:
            snap[rel] = "<binary>"
    return snap


@requires_mcp
@pytest.mark.parametrize(
    ("subcommand", "tier", "fixture_path"),
    FIXTURE_PARAMS,
    ids=[f"{s}__{p.stem}__{t}" for s, t, p in FIXTURE_PARAMS],
)
def test_mcp_cli_conformance_for_indexed_fixtures(
    subcommand: str,
    tier: str,
    fixture_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNATTENDED_REVERT_POLICY", raising=False)
    doc = json.loads(fixture_path.read_text(encoding="utf-8"))
    expected_path = _expected_path_for_payload(fixture_path)
    expected = (
        json.loads(expected_path.read_text(encoding="utf-8"))
        if expected_path is not None
        else {}
    )
    assert doc["subcommand"] == subcommand

    cli_root = tmp_path / "cli"
    mcp_root = tmp_path / "mcp"
    raw_payload = doc["payload"]
    _prepare_root(cli_root, raw_payload)
    _prepare_root(mcp_root, raw_payload)

    cli_payload = _payload_for_root(raw_payload, cli_root, tier=tier)
    mcp_payload = _payload_for_root(raw_payload, mcp_root, tier=tier)
    env_extra = dict(_GIT_ENV_PIN) if raw_payload.get("_seed_git_repo") else None
    if expected.get("fixed_now"):
        env_extra = {**(env_extra or {}), "PLAN_OPS_FIXED_NOW": expected["fixed_now"]}

    cli_proc = _run_cli(
        subcommand,
        cli_payload,
        tier=tier,
        expected=expected,
        cwd=cli_root,
        env_extra=env_extra,
    )
    mcp_result = _run_mcp(
        subcommand,
        _mcp_arguments(subcommand, mcp_payload),
        cwd=mcp_root,
        env_extra=env_extra,
    )
    mcp_envelope = _structured_from_call_result(mcp_result)

    assert cli_proc.stderr == "", cli_proc.stderr
    prefixes = (str(cli_root), str(mcp_root))
    if cli_proc.stdout.strip():
        cli_envelope = json.loads(cli_proc.stdout)
        assert _canon_envelope(cli_envelope, prefixes) == _canon_envelope(
            mcp_envelope, prefixes
        )
    else:
        assert expected.get("stdout_suppressed"), "CLI --json path emitted no stdout"

    if cli_proc.returncode != 0 and not isinstance(mcp_result, dict):
        assert getattr(mcp_result, "isError", False), (
            "MCP result must be isError=True when the CLI envelope exits non-zero"
        )
    else:
        assert not getattr(mcp_result, "isError", False)

    if subcommand in SIDE_EFFECT_SUBCOMMANDS:
        cli_snapshot = _snapshot(cli_root, prefixes)
        mcp_snapshot = _snapshot(mcp_root, prefixes)
        assert cli_snapshot == mcp_snapshot, (
            f"filesystem delta mismatch for {subcommand}\n"
            f"cli only: {sorted(set(cli_snapshot) - set(mcp_snapshot))}\n"
            f"mcp only: {sorted(set(mcp_snapshot) - set(cli_snapshot))}\n"
            f"changed: {sorted(k for k in cli_snapshot if cli_snapshot.get(k) != mcp_snapshot.get(k))}"
        )
        for relpath in ("docs/plans/_run_log.jsonl", "docs/plans/_run_lock.json"):
            assert cli_snapshot.get(relpath) == mcp_snapshot.get(relpath)


@requires_mcp
def test_schema_rejection_shapes_name_offending_field() -> None:
    bad_args = {"id": "001", "unexpected_field": True}
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS_DIR / "plan_ops.py"),
            "normalize-task-id",
            "--id",
            "001",
            "--unexpected-field",
            "true",
            "--json",
        ],
        cwd=str(SCRIPTS_DIR),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode != 0
    assert "unexpected" in proc.stderr

    import jsonschema

    server = _load_server_module()
    entry = server._tool_registry_by_name()["plan_ops__normalize_task_id"]
    validator = jsonschema.Draft202012Validator(entry["input_schema"])
    errors = sorted(validator.iter_errors(bad_args), key=lambda e: e.path)
    assert errors
    text = "\n".join(error.message for error in errors)
    assert "unexpected_field" in text or "unexpected" in text


@requires_mcp
def test_mcp_protocol_error_on_unexpected_server_exception(tmp_path: Path) -> None:
    cli_runner = tmp_path / "crash_cli.py"
    cli_runner.write_text(
        "\n".join(
            [
                "import sys",
                f"sys.path.insert(0, {str(SERVER_PATH.parent)!r})",
                "import plan_ops",
                "def boom(payload):",
                "    raise RuntimeError('synthetic normalize crash')",
                "plan_ops._run_normalize_task_id = boom",
                "plan_ops.cmd_normalize_task_id(plan_ops.build_parser().parse_args(['normalize-task-id', '--id', '1', '--json']))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cli_proc = subprocess.run(
        [sys.executable, str(cli_runner)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert cli_proc.returncode != 0
    assert "synthetic normalize crash" in cli_proc.stderr

    server = _load_server_module()

    def boom(payload: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("synthetic normalize crash")

    original = server.plan_ops._run_normalize_task_id
    try:
        server.plan_ops._run_normalize_task_id = boom
        with pytest.raises(RuntimeError, match="synthetic normalize crash"):
            _run_mcp("normalize-task-id", {"id": "1"}, cwd=SCRIPTS_DIR)
    finally:
        server.plan_ops._run_normalize_task_id = original


@requires_mcp
@pytest.mark.parametrize(
    ("case_name", "arguments", "expected_action", "expected_args"),
    [
        (
            "codex_clean",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "codex",
                "claude_only": False,
                "reviewer_envelope": {"verdict": "clean", "findings": [], "summary": ""},
                "d5_envelope": None,
                "retries_used": {},
                "flags": {},
            },
            "commit",
            {},
        ),
        (
            "gemini_clean",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "gemini",
                "claude_only": False,
                "reviewer_envelope": {"verdict": "clean", "findings": [], "summary": ""},
                "d5_envelope": None,
                "retries_used": {},
                "flags": {},
            },
            "commit",
            {},
        ),
        (
            "claude_only_ship",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "claude",
                "claude_only": True,
                "reviewer_envelope": {"verdict": "ship", "findings": [], "summary": ""},
                "d5_envelope": None,
                "retries_used": {},
                "flags": {},
            },
            "commit",
            {},
        ),
        (
            "binding_pause",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "codex",
                "claude_only": False,
                "unattended_revert_policy": "pause",
                "reviewer_envelope": {
                    "verdict": "needs-rework",
                    "findings": ["fix me"],
                    "summary": "blocked",
                },
                "d5_envelope": None,
                "retries_used": {},
                "flags": {"codex_review_binding": True},
            },
            "pause_awaiting_user",
            {"policy_kind": "binding_policy", "unattended_revert_policy": "pause"},
        ),
        (
            "binding_fail_fast",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "codex",
                "claude_only": False,
                "unattended_revert_policy": "fail-fast",
                "reviewer_envelope": {
                    "verdict": "needs-rework",
                    "findings": ["fix me"],
                    "summary": "blocked",
                },
                "d5_envelope": None,
                "retries_used": {},
                "flags": {"codex_review_binding": True},
            },
            "fail",
            {
                "policy_kind": "binding_policy",
                "unattended_revert_policy": "fail-fast",
                "authorization_source": "unattended-fail-fast",
            },
        ),
        (
            "skip_review",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "none",
                "claude_only": False,
                "reviewer_envelope": {"verdict": "clean", "findings": [], "summary": ""},
                "d5_envelope": None,
                "retries_used": {},
                "flags": {},
            },
            "commit",
            {"reviewer": "none"},
        ),
        (
            "unknown_verdict",
            {
                "task_id": "001",
                "implementer": "claude",
                "reviewer": "codex",
                "claude_only": False,
                "reviewer_envelope": {"verdict": "surprising", "findings": [], "summary": ""},
                "d5_envelope": None,
                "retries_used": {},
                "flags": {},
            },
            "unknown_state",
            {},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_review_route_mcp_contract_cases(
    case_name: str,
    arguments: dict[str, Any],
    expected_action: str,
    expected_args: dict[str, Any],
) -> None:
    result = _run_mcp("review-route", arguments, cwd=SCRIPTS_DIR)
    assert not getattr(result, "isError", False), case_name
    envelope = _structured_from_call_result(result)
    assert envelope["action"] == expected_action
    for key, value in expected_args.items():
        assert envelope["args"][key] == value


@requires_mcp
def test_review_route_schema_invalid_mcp_payload_returns_tool_error() -> None:
    result = _run_mcp(
        "review-route",
        {
            "task_id": "001",
            "implementer": "claude",
            "reviewer": "codex",
            "claude_only": "false",
            "reviewer_envelope": {"verdict": "clean"},
            "retries_used": {},
            "flags": {},
        },
        cwd=SCRIPTS_DIR,
    )

    assert getattr(result, "isError", False) is True
    envelope = _structured_from_call_result(result)
    assert envelope["error"] == "review-route input schema violation"
    assert any(error["path"] == "$.claude_only" for error in envelope["errors"])


def test_semantic_invalid_fixture_audit_is_complete() -> None:
    """Document the TASK-016 body-level error audit result.

    The required grep was run against ``plan_ops.py`` and cross-checked
    against Tier-A schemas. The two audited in-schema body-error cases
    named in TASK-016 must remain fixture-backed.
    """
    required = {"normalize-task-id", "review-route"}
    present = {path.stem for path in SEMANTIC_INVALID_DIR.glob("*.json")}
    assert required <= present
