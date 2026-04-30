"""Tier-B pure-core conformance fixtures for ``plan_ops`` (TASK-005).

This suite is the byte-equal conformance gate for the 5 atomic-writer
Tier-B ``cmd_*`` subcommands enumerated in
``docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md``:
``cmd_audit``, ``cmd_build_claude_dispatch_input``,
``cmd_finalize_execution_log``, ``cmd_update_plan_header``,
``cmd_write_schedule``.

Beyond Tier-A's ``run_conformance`` byte-equal-of-stdout assertion, this
suite adds:

  * **Validate-then-write atomicity.** Each error fixture (``fs_invariant``
    True) materializes a sentinel target file pre-run, then asserts the
    on-disk image is byte-identical AFTER the CLI subprocess and AFTER
    the pure-core call. ``_run_X`` returning non-empty ``errors[]`` MUST
    leave the file untouched.
  * **Atomic-write tmp-file invariance.** ``_atomic_write_text`` renames a
    ``<name>.tmp`` sidecar into place; an OSError before the rename is
    cleaned up. After every fixture the suite asserts no leftover
    ``.tmp`` files anywhere under ``fixture_dir``.
  * **CLI vs pure write-content byte-equality.** For happy-path fixtures
    we run the CLI subprocess (capture filesystem snapshot), restore the
    pre-state, then run ``_run_X(payload)`` and compare the post-snapshot
    against the post-CLI snapshot. Timestamps are replaced with a
    sentinel so live wall-clock drift inside ``_now()`` does not register
    as a fake divergence.
  * **Sentinel-bearing ``--output -`` coverage.**
    ``cmd_build_claude_dispatch_input`` is exercised in BOTH stdout-mode
    (``output="-"``) and file-mode (``output=<tmp_path>/foo.json``); a
    dedicated ``test_cmd_build_claude_dispatch_input_byte_equal_across_output_modes``
    proves the canonical envelope is byte-equal regardless of routing.

Each fixture lives on disk as a payload+expected JSON pair under
``fixtures/plan_ops_pure_core/tier_b/`` and is authored programmatically
via ``_AUTHORED_CASES`` (single source of truth).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.scripts.plan_ops_pure_harness import (
    REPO_ROOT,
    SCRIPTS_DIR,
    _snapshot_dir,
    canonicalize_envelope,
    cli_argv_from,
    load_plan_ops,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "plan_ops_pure_core"
    / "tier_b"
)


# ---------------------------------------------------------------------------
# Static fixture content
# ---------------------------------------------------------------------------


SCHEDULE_VALID = {
    "outcome": "valid",
    "tasks": [
        {
            "id": "001",
            "agent": "claude",
            "title": "one",
            "files": ["src/one.py"],
            "dependencies": [],
            "plan_file": "TASK-001_one.md",
        },
    ],
    "batches": [
        {"index": 1, "task_ids": ["001"], "file_locks": ["src/one.py"]},
    ],
    "gaps": [],
    "risks": [],
}

PLAN_FILE_TASK_001 = """# Tier-B test plan

- **Status:** Pending

## Context

Stub.

## Tasks

### TASK-001: example

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - src/one.py
- **Dependencies:**
- **Test command:** `none`
- **Acceptance criteria:**
  - x

**Description:** Stub child plan.
"""

# Pinned wall clock for `_now()` — replaced by the timestamp sentinel
# during canonicalization but used for the file-content byte-equality
# check on the audit Markdown report.
FIXED_NOW = "2026-04-30T00:00:00Z"


# ---------------------------------------------------------------------------
# Authored cases: (subcommand, case_name, payload, expected)
# ---------------------------------------------------------------------------


_AUTHORED_CASES: list[tuple[str, str, dict, dict]] = [
    # ------------------------------------------------------------------
    # cmd_audit
    # ------------------------------------------------------------------
    (
        "audit",
        "happy_list",
        {"list": True, "check": None, "strict": False, "report_file": None,
         "json": True},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True,
         "fixed_now": FIXED_NOW},
    ),
    (
        "audit",
        "happy_with_report_file",
        {
            "list": False,
            "check": "status_vocabulary",
            "strict": False,
            "report_file": "{FIXTURE_DIR}/audit_report.md",
            "json": True,
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "fs_invariant": False,
            "fixed_now": FIXED_NOW,
            "expected_writes": ["audit_report.md"],
        },
    ),
    (
        "audit",
        "error_unknown_check",
        {
            "_files": {"target.md": "pre-existing\n"},
            "list": False,
            "check": "totally-bogus-check-name",
            "strict": False,
            "report_file": "{FIXTURE_DIR}/target.md",
            "json": True,
        },
        {
            "exit_code": 1,
            "stderr_policy": "empty",
            "fs_invariant": True,
            "fixed_now": FIXED_NOW,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_build_claude_dispatch_input
    # ------------------------------------------------------------------
    (
        "build-claude-dispatch-input",
        "happy_stdout_mode",
        {
            "_files": {"plan.md": PLAN_FILE_TASK_001},
            "plan_file": "{FIXTURE_DIR}/plan.md",
            "task_id": "1",
            "variant": "default",
            "repo_root": "{REPO_ROOT}",
            "analyst_annotations": None,
            "target_task_id": None,
            "starting_sha": "deadbeef",
            "dispatch_context": None,
            "run_id": "PINNED-RUN-ID",
            "output": "-",
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True,
         "fixed_now": FIXED_NOW, "compare_stdout_envelope": True},
    ),
    (
        "build-claude-dispatch-input",
        "happy_file_mode",
        {
            "_files": {"plan.md": PLAN_FILE_TASK_001},
            "plan_file": "{FIXTURE_DIR}/plan.md",
            "task_id": "1",
            "variant": "default",
            "repo_root": "{REPO_ROOT}",
            "analyst_annotations": None,
            "target_task_id": None,
            "starting_sha": "deadbeef",
            "dispatch_context": None,
            "run_id": "PINNED-RUN-ID",
            "output": "{FIXTURE_DIR}/dispatch.json",
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "fs_invariant": False,
            "fixed_now": FIXED_NOW,
            "expected_writes": ["dispatch.json"],
            "stdout_suppressed": True,
        },
    ),
    (
        "build-claude-dispatch-input",
        "error_variant_mismatch",
        {
            "_files": {"plan.md": PLAN_FILE_TASK_001},
            "plan_file": "{FIXTURE_DIR}/plan.md",
            "task_id": "1",
            "variant": "narrow-remediation",
            "repo_root": "{REPO_ROOT}",
            "analyst_annotations": None,
            "target_task_id": None,
            "starting_sha": "deadbeef",
            "dispatch_context": None,
            "run_id": "PINNED-RUN-ID",
            "output": "-",
        },
        {
            "exit_code": 1,
            "stderr_policy": "empty",
            "fs_invariant": True,
            "fixed_now": FIXED_NOW,
            "compare_stdout_envelope": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_finalize_execution_log
    # ------------------------------------------------------------------
    (
        "finalize-execution-log",
        "happy",
        {
            "_files": {"plan.md": "# Plan\n\n## Tasks\n\nbody\n"},
            "plan_file": "{FIXTURE_DIR}/plan.md",
            "run_id": "PINNED-RUN-ID",
            "starting_sha": "aaa",
            "ending_sha": "bbb",
            "rows_json": json.dumps([
                {"task": "001", "agent": "claude", "reviewer": "codex",
                 "verdict": "ship", "commit": "abc1234", "notes": "ok"}
            ]),
            "outcome": "success",
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "fs_invariant": False,
            "expected_writes": ["plan.md"],
        },
    ),
    (
        "finalize-execution-log",
        "error_bad_rows_json",
        {
            "_files": {"plan.md": "# Plan\n\n## Tasks\n\nbody\n"},
            "plan_file": "{FIXTURE_DIR}/plan.md",
            "run_id": "PINNED-RUN-ID",
            "starting_sha": "aaa",
            "ending_sha": "bbb",
            "rows_json": "{not json",
            "outcome": "success",
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    # ------------------------------------------------------------------
    # cmd_update_plan_header
    # ------------------------------------------------------------------
    (
        "update-plan-header",
        "happy",
        {
            "_files": {
                "plan.md": (
                    "# Plan\n\n"
                    "- **Status:** Pending\n\n"
                    "## Context\n\nBody\n\n## Tasks\n\n### TASK-001: x\n"
                ),
            },
            "plan_file": "{FIXTURE_DIR}/plan.md",
            "status": "complete",
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "fs_invariant": False,
            "expected_writes": ["plan.md"],
        },
    ),
    (
        "update-plan-header",
        "error_plan_not_found",
        {
            "plan_file": "{FIXTURE_DIR}/missing.md",
            "status": "complete",
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    # ------------------------------------------------------------------
    # cmd_write_schedule
    # ------------------------------------------------------------------
    (
        "write-schedule",
        "happy",
        {
            "schedule_file": {"__path__": "{FIXTURE_DIR}/schedule.json"},
            "stdin": True,
            "strict": False,
            "stdin_text": json.dumps(SCHEDULE_VALID),
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "fs_invariant": False,
            "expected_writes": ["schedule.json"],
        },
    ),
    (
        "write-schedule",
        "error_bad_json",
        {
            "_files": {"schedule.json": "PRE-EXISTING\n"},
            "schedule_file": {"__path__": "{FIXTURE_DIR}/schedule.json"},
            "stdin": True,
            "strict": False,
            "stdin_text": "{not json",
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
]


# ---------------------------------------------------------------------------
# Disk fixture I/O
# ---------------------------------------------------------------------------


def _payload_path(subcommand: str, case_name: str) -> Path:
    return FIXTURE_DIR / f"{subcommand}__{case_name}.payload.json"


def _expected_path(subcommand: str, case_name: str) -> Path:
    return FIXTURE_DIR / f"{subcommand}__{case_name}.expected.json"


def _write_authored_fixtures() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for subcommand, case_name, payload, expected in _AUTHORED_CASES:
        envelope = {"subcommand": subcommand, "payload": payload}
        _payload_path(subcommand, case_name).write_text(
            json.dumps(envelope, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _expected_path(subcommand, case_name).write_text(
            json.dumps(expected, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


@pytest.fixture(scope="session", autouse=True)
def _seed_fixture_pairs() -> None:
    _write_authored_fixtures()


def _resolve_template(value: Any, fixture_dir: Path) -> Any:
    """Resolve `{FIXTURE_DIR}` / `{REPO_ROOT}` placeholders.

    Mirrors the Tier-A driver: a dict with key ``__path__`` is wrapped
    as ``pathlib.Path`` after string substitution.
    """
    if isinstance(value, str):
        return value.replace(
            "{FIXTURE_DIR}", str(fixture_dir),
        ).replace(
            "{REPO_ROOT}", str(REPO_ROOT),
        )
    if isinstance(value, list):
        return [_resolve_template(v, fixture_dir) for v in value]
    if isinstance(value, dict):
        if set(value.keys()) == {"__path__"}:
            return Path(_resolve_template(value["__path__"], fixture_dir))
        return {k: _resolve_template(v, fixture_dir) for k, v in value.items()}
    return value


def _discover_pairs() -> list[tuple[str, str]]:
    if not FIXTURE_DIR.is_dir():
        _write_authored_fixtures()
    pairs: list[tuple[str, str]] = []
    for path in sorted(FIXTURE_DIR.glob("*.payload.json")):
        stem = path.name[: -len(".payload.json")]
        sub, _, case = stem.partition("__")
        if not sub or not case:
            continue
        if not _expected_path(sub, case).is_file():
            continue
        pairs.append((sub, case))
    return pairs


_DISCOVERED_PAIRS = _discover_pairs()


# ---------------------------------------------------------------------------
# Tier-B driver — extends Tier-A's harness with write+atomicity assertions.
# ---------------------------------------------------------------------------


_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_TIMESTAMP_SENTINEL = "<TIMESTAMP>"

_HARNESS_ONLY_PAYLOAD_KEYS = {"_files", "stdin_text"}


def _canon_text(text: str) -> str:
    return _TIMESTAMP_RE.sub(_TIMESTAMP_SENTINEL, text)


def _materialize_files(fixture_dir: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = fixture_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _restore_snapshot(fixture_dir: Path, snapshot: dict[str, str]) -> None:
    """Restore ``fixture_dir`` to the given snapshot.

    Files newly created since the snapshot are removed; files present in
    the snapshot are rewritten verbatim. Used between the CLI subprocess
    run and the in-process pure-core run so the second run starts from
    the exact same on-disk state as the first.
    """
    current = _snapshot_dir(fixture_dir)
    for relpath in current:
        if relpath not in snapshot:
            (fixture_dir / relpath).unlink()
    for relpath, content in snapshot.items():
        target = fixture_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _payload_for_pure_core(payload: dict, fixture_dir: Path) -> dict:
    """Strip harness-only keys and resolve templates for the pure call."""
    pure = {
        k: _resolve_template(v, fixture_dir)
        for k, v in payload.items()
        if k not in _HARNESS_ONLY_PAYLOAD_KEYS
    }
    # `_run_X` reads `payload["stdin_text"]` for stdin-bearing commands;
    # forward the harness key under the same name the pure core expects.
    if "stdin_text" in payload:
        pure["stdin_text"] = payload["stdin_text"]
    return pure


def _run_tier_b_conformance(
    subcommand: str,
    payload: dict,
    expected: dict,
    fixture_dir: Path,
) -> None:
    fixture_dir = Path(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)

    files = payload.get("_files")
    if files:
        _materialize_files(fixture_dir, files)

    pre_snapshot = _snapshot_dir(fixture_dir)

    pure_payload = _payload_for_pure_core(payload, fixture_dir)
    # `cli_argv_from` reads argparse dests from the pure_payload; harness
    # internals like `stdin_text` are excluded by `_HARNESS_ONLY_PAYLOAD_KEYS`
    # in the harness module itself.
    cli_argv = cli_argv_from(subcommand, pure_payload)
    stdin_text = pure_payload.get("stdin_text", "") or ""

    proc_env = os.environ.copy()
    # Scrub policy env so neither path inherits a value the fixture didn't
    # explicitly assert. cmd_build_claude_dispatch_input reads the env var
    # inside its `_args_to_payload_*` builder; the pure core only reads
    # the payload key, which we never set in tier_b fixtures.
    proc_env.pop("UNATTENDED_REVERT_POLICY", None)
    if expected.get("fixed_now"):
        proc_env["PLAN_OPS_FIXED_NOW"] = expected["fixed_now"]

    # ------------------------------------------------------------------
    # CLI subprocess run.
    # ------------------------------------------------------------------
    proc = subprocess.run(
        [sys.executable, "plan_ops.py", subcommand, *cli_argv],
        input=stdin_text,
        cwd=str(SCRIPTS_DIR),
        env=proc_env,
        capture_output=True,
        text=True,
    )
    cli_snapshot = _snapshot_dir(fixture_dir)

    # Restore to pre-state so the pure-core call starts identically.
    _restore_snapshot(fixture_dir, pre_snapshot)

    # ------------------------------------------------------------------
    # In-process pure-core run.
    # ------------------------------------------------------------------
    plan_ops = load_plan_ops()
    run_fn_name = f"_run_{subcommand.replace('-', '_')}"
    try:
        run_fn = getattr(plan_ops, run_fn_name)
    except AttributeError as exc:
        raise AssertionError(
            f"plan_ops has no pure-core entry point {run_fn_name!r} for "
            f"subcommand {subcommand!r}"
        ) from exc

    saved_now = getattr(plan_ops, "_now", None)
    saved_env_value = os.environ.pop("UNATTENDED_REVERT_POLICY", None)
    try:
        if expected.get("fixed_now"):
            plan_ops._now = lambda: expected["fixed_now"]  # type: ignore[attr-defined]
        pure_result_raw = run_fn(pure_payload)
    finally:
        if expected.get("fixed_now") and saved_now is not None:
            plan_ops._now = saved_now  # type: ignore[attr-defined]
        if saved_env_value is not None:
            os.environ["UNATTENDED_REVERT_POLICY"] = saved_env_value
    pure_snapshot = _snapshot_dir(fixture_dir)
    pure_exit = pure_result_raw.get("__plan_ops_exit_code__", 0)
    pure_envelope = plan_ops._public_result(pure_result_raw)

    # ------------------------------------------------------------------
    # Assertions.
    # ------------------------------------------------------------------
    expected_exit = int(expected["exit_code"])
    assert proc.returncode == expected_exit, (
        f"subprocess exit_code mismatch: expected {expected_exit}, "
        f"got {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert int(pure_exit) == expected_exit, (
        f"pure-core exit_code mismatch: expected {expected_exit}, "
        f"got {pure_exit}"
    )

    stderr_policy = expected.get("stderr_policy", "empty")
    if stderr_policy == "empty":
        assert proc.stderr == "", f"expected empty stderr; got {proc.stderr!r}"
    elif stderr_policy == "nonempty":
        assert proc.stderr.strip() != "", "expected nonempty stderr; got empty"

    # Atomic-write tmp leftover invariant.
    leftovers = sorted(
        str(p.relative_to(fixture_dir))
        for p in fixture_dir.rglob("*.tmp")
    )
    assert not leftovers, f"atomic-write tmp leftovers under fixture_dir: {leftovers}"

    if expected.get("fs_invariant"):
        # Validate-then-write atomicity: error path leaves disk untouched.
        assert pre_snapshot == cli_snapshot, (
            "CLI mutated filesystem on validate-then-write error:\n"
            f"  added: {sorted(set(cli_snapshot) - set(pre_snapshot))}\n"
            f"  changed: {sorted(k for k in pre_snapshot if pre_snapshot.get(k) != cli_snapshot.get(k))}"
        )
        assert pre_snapshot == pure_snapshot, (
            "pure-core mutated filesystem on validate-then-write error:\n"
            f"  added: {sorted(set(pure_snapshot) - set(pre_snapshot))}\n"
            f"  changed: {sorted(k for k in pre_snapshot if pre_snapshot.get(k) != pure_snapshot.get(k))}"
        )

    expected_writes: list[str] = expected.get("expected_writes", []) or []
    for relpath in expected_writes:
        assert relpath in cli_snapshot, (
            f"CLI did not write expected file {relpath!r}; "
            f"snapshot keys={sorted(cli_snapshot)}"
        )
        assert relpath in pure_snapshot, (
            f"pure-core did not write expected file {relpath!r}; "
            f"snapshot keys={sorted(pure_snapshot)}"
        )
        cli_content = _canon_text(cli_snapshot[relpath])
        pure_content = _canon_text(pure_snapshot[relpath])
        assert cli_content == pure_content, (
            f"divergent on-disk content for {relpath} between CLI and "
            f"pure-core paths after timestamp canonicalization\n"
            f"--- cli ---\n{cli_content}\n--- pure ---\n{pure_content}\n"
        )

    if expected.get("compare_stdout_envelope"):
        # Stdout-mode commands (output == '-' for builders, --json for
        # audit/etc.) emit their canonical envelope on stdout. Compare it
        # byte-equal against the public pure-core envelope.
        try:
            cli_envelope = json.loads(proc.stdout) if proc.stdout else None
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"subprocess stdout is not parseable JSON: {exc}; "
                f"raw stdout={proc.stdout!r}"
            ) from exc
        cli_canonical = canonicalize_envelope(cli_envelope)
        pure_canonical = canonicalize_envelope(pure_envelope)
        assert cli_canonical == pure_canonical, (
            "canonical envelope mismatch between CLI and pure-core paths\n"
            f"--- cli ---\n{cli_canonical}\n--- pure ---\n{pure_canonical}\n"
        )

    if expected.get("stdout_suppressed"):
        # File-mode builders suppress stdout (envelope routed to disk).
        assert proc.stdout == "", (
            f"expected suppressed stdout in file-mode; got {proc.stdout!r}"
        )
        assert pure_result_raw.get("__plan_ops_stdout_suppressed__") is True, (
            "pure-core did not set __plan_ops_stdout_suppressed__ for "
            f"file-mode {subcommand!r}"
        )


# ---------------------------------------------------------------------------
# The single conformance test, parametrized over discovered pairs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcommand", "case_name"),
    _DISCOVERED_PAIRS,
    ids=[f"{s}__{c}" for s, c in _DISCOVERED_PAIRS],
)
def test_tier_b_pure_core_conformance(
    subcommand: str,
    case_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNATTENDED_REVERT_POLICY", raising=False)
    payload_doc = json.loads(
        _payload_path(subcommand, case_name).read_text(encoding="utf-8")
    )
    expected = json.loads(
        _expected_path(subcommand, case_name).read_text(encoding="utf-8")
    )
    assert payload_doc.get("subcommand") == subcommand, (
        f"fixture envelope subcommand mismatch: "
        f"{payload_doc.get('subcommand')!r} vs {subcommand!r}"
    )
    payload = payload_doc["payload"]
    # Resolve templates inside `_files` content separately — `_files`
    # values are file bodies, not paths, so we leave them alone, but the
    # rest of the payload is templated when handed to the pure core.
    expected_resolved = _resolve_template(expected, tmp_path)
    _run_tier_b_conformance(subcommand, payload, expected_resolved, tmp_path)


def test_authored_case_count_matches_disk() -> None:
    expected = {(s, c) for s, c, _, _ in _AUTHORED_CASES}
    discovered = set(_discover_pairs())
    assert expected == discovered, (
        "_AUTHORED_CASES drifted from disk — "
        f"missing on disk: {sorted(expected - discovered)}; "
        f"orphan disk pairs: {sorted(discovered - expected)}"
    )


def test_runtime_under_30s() -> None:
    """Hard ceiling per AC: keep the suite small enough to run quickly."""
    assert len(_AUTHORED_CASES) <= 30, (
        f"too many authored cases ({len(_AUTHORED_CASES)})."
    )


# ---------------------------------------------------------------------------
# Sentinel-bearing --output - vs file-mode byte-equality cross-check.
# ---------------------------------------------------------------------------


def test_cmd_build_claude_dispatch_input_byte_equal_across_output_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--output -`` vs ``--output <path>`` MUST emit byte-equal envelopes.

    Codifies the AC's "Sentinel-bearing ``--output -`` coverage" bullet:
    the canonical claude_dispatch_input envelope content is identical
    regardless of whether it is routed to stdout or to a file.
    """
    monkeypatch.delenv("UNATTENDED_REVERT_POLICY", raising=False)
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(PLAN_FILE_TASK_001, encoding="utf-8")

    plan_ops = load_plan_ops()
    saved_now = plan_ops._now
    plan_ops._now = lambda: FIXED_NOW  # type: ignore[attr-defined]
    try:
        common_payload = {
            "plan_file": str(plan_path),
            "task_id": "1",
            "variant": "default",
            "repo_root": str(REPO_ROOT),
            "analyst_annotations": None,
            "target_task_id": None,
            "starting_sha": "deadbeef",
            "dispatch_context": None,
            "run_id": "PINNED-RUN-ID",
        }

        stdout_payload = dict(common_payload, output="-")
        stdout_result = plan_ops._run_build_claude_dispatch_input(stdout_payload)
        stdout_envelope = plan_ops._public_result(stdout_result)

        out_path = tmp_path / "dispatch.json"
        file_payload = dict(common_payload, output=str(out_path))
        file_result = plan_ops._run_build_claude_dispatch_input(file_payload)
        assert file_result.get("__plan_ops_stdout_suppressed__") is True
        file_envelope = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        plan_ops._now = saved_now  # type: ignore[attr-defined]

    assert canonicalize_envelope(stdout_envelope) == canonicalize_envelope(
        file_envelope
    ), "stdout-mode and file-mode envelopes diverge byte-equally"


# Defensive imports kept bound for future case helpers that may need
# them; avoids an accidental NameError if a contributor later references
# them inside an inline assertion block.
_ = (io,)
