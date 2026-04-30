"""Meta-tests for the TASK-003H pure-core conformance harness.

These tests exercise the harness ITSELF, not any individual subcommand's
fixture suite. The Tier-A/B/C fixture tasks (TASK-004/005/006) import
``run_conformance`` and add subcommand cases — but the harness contract
needs to be locked down independently so a divergence-detection bug in
the harness can never silently mask a real CLI/pure-core drift.

Coverage (per the TASK-003H acceptance criteria):

  * Byte-equal happy path on a real subcommand (``normalize-task-id``).
  * Deliberate stdin-text divergence detection.
  * Exit-code mismatch detection.
  * JSON canonicalization equivalence (re-ordered keys compare equal).
  * Filesystem-snapshot diff detection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.scripts.plan_ops_pure_harness import (
    _snapshot_dir,
    canonicalize_envelope,
    cli_argv_from,
    run_conformance,
)

AUDIT_LIST_TEXT = (
    "checks: [{'name': 'status_vocabulary', 'tier': 'default'}, "
    "{'name': 'schedule_wire_format', 'tier': 'default'}, "
    "{'name': 'implementer_report_labels', 'tier': 'default'}, "
    "{'name': 'execution_log_columns', 'tier': 'default'}, "
    "{'name': 'schemas', 'tier': 'default'}, "
    "{'name': 'portable_tier', 'tier': 'advisory'}, "
    "{'name': 'wrapper_isolation', 'tier': 'default'}, "
    "{'name': 'design_doc_orphans', 'tier': 'default'}, "
    "{'name': 'global_lock_paths', 'tier': 'default'}, "
    "{'name': 'canonical_fixture_not_archived', 'tier': 'default'}, "
    "{'name': 'principle_referenced', 'tier': 'default'}, "
    "{'name': 'fail_task_authorization_source', 'tier': 'default'}, "
    "{'name': 'wrapper_restore_authorization', 'tier': 'default'}, "
    "{'name': 'gemini-available', 'tier': 'advisory'}]\n"
)


# ---------------------------------------------------------------------------
# Byte-equal happy path
# ---------------------------------------------------------------------------


def test_run_conformance_byte_equal_happy_path(tmp_path: Path) -> None:
    """``normalize-task-id 1`` is identical via CLI and pure-core paths."""
    run_conformance(
        "normalize-task-id",
        {"id": "1"},
        expected={
            "cli_argv": ["--id", "1", "--json"],
            "exit_code": 0,
            "stderr_policy": "empty",
            "stdout_envelope": {"normalized": "001"},
            "fs_invariant": True,
        },
        fixture_dir=tmp_path,
    )


def test_run_conformance_parse_schedule_stdin(tmp_path: Path) -> None:
    """Stdin-driven ``parse-schedule`` round-trips byte-equal."""
    schedule = {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "agent": "claude",
                "title": "one",
                "files": ["src/one.py"],
                "dependencies": [],
                "plan_file": "TASK-001_one.md",
            }
        ],
        "batches": [
            {"index": 1, "task_ids": ["001"], "file_locks": ["src/one.py"]}
        ],
        "gaps": [],
        "risks": [],
    }
    stdin_text = json.dumps(schedule)
    run_conformance(
        "parse-schedule",
        {"stdin": True, "strict": False, "stdin_text": stdin_text},
        expected={
            "cli_argv": ["--stdin", "--json"],
            "exit_code": 0,
            "stderr_policy": "empty",
        },
        fixture_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# cli_argv_from derivation (no fixture-side duplication)
# ---------------------------------------------------------------------------


def test_cli_argv_from_normalize_task_id() -> None:
    assert cli_argv_from("normalize-task-id", {"id": "1"}) == [
        "--id",
        "1",
        "--json",
    ]


def test_cli_argv_from_parse_schedule_flags() -> None:
    assert cli_argv_from(
        "parse-schedule",
        {"stdin": True, "strict": True, "stdin_text": "{}"},
    ) == ["--stdin", "--strict", "--json"]


def test_cli_argv_from_unregistered_subcommand_raises() -> None:
    with pytest.raises(KeyError, match="no argparse subcommand registered"):
        cli_argv_from("totally-bogus-subcommand", {})


def test_run_conformance_text_output_compares_pure_marker(
    tmp_path: Path,
) -> None:
    """Text-output commands compare CLI stdout to the pure marker too."""
    run_conformance(
        "audit",
        {"list": True, "json": False},
        expected={
            "exit_code": 0,
            "stderr_policy": "empty",
            "text_output": True,
            "stdout_text": AUDIT_LIST_TEXT,
        },
        fixture_dir=tmp_path,
    )


def test_run_conformance_detects_text_output_divergence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pure-core text marker drift must fail even when CLI stdout matches."""
    from tests.scripts.plan_ops_pure_harness import load_plan_ops

    plan_ops = load_plan_ops()
    original = plan_ops._run_audit

    def wrong_text(payload: dict) -> dict:
        result = dict(original(payload))
        result["__plan_ops_text_output__"] = "wrong\n"
        return result

    monkeypatch.setattr(plan_ops, "_run_audit", wrong_text)

    with pytest.raises(AssertionError, match="text output mismatch"):
        run_conformance(
            "audit",
            {"list": True, "json": False},
            expected={
                "exit_code": 0,
                "stderr_policy": "empty",
                "text_output": True,
                "stdout_text": AUDIT_LIST_TEXT,
            },
            fixture_dir=tmp_path,
        )


def test_run_conformance_derives_argv_when_omitted(tmp_path: Path) -> None:
    """Fixture cases need not pass ``cli_argv`` — harness derives from payload."""
    run_conformance(
        "normalize-task-id",
        {"id": "1"},
        expected={
            "exit_code": 0,
            "stderr_policy": "empty",
            "stdout_envelope": {"normalized": "001"},
            "fs_invariant": True,
        },
        fixture_dir=tmp_path,
    )


# ---------------------------------------------------------------------------
# Divergence detection
# ---------------------------------------------------------------------------


def test_run_conformance_detects_exit_code_mismatch(tmp_path: Path) -> None:
    """Wrong ``expected.exit_code`` raises AssertionError naming exit_code."""
    with pytest.raises(AssertionError, match="exit_code mismatch"):
        run_conformance(
            "normalize-task-id",
            {"id": "1"},
            expected={
                "cli_argv": ["--id", "1", "--json"],
                "exit_code": 99,  # the real exit is 0
                "stderr_policy": "empty",
            },
            fixture_dir=tmp_path,
        )


def test_run_conformance_detects_stdin_divergence(tmp_path: Path) -> None:
    """If pure-core sees a different stdin payload than the subprocess, the
    canonical-envelope assertion fires.

    Here the CLI argv normalizes id ``"1"`` (success), while the in-process
    pure-core call receives ``id="bogus"`` and produces an error envelope —
    same shape as a stdin-text drift between the two paths.
    """
    with pytest.raises(AssertionError) as excinfo:
        run_conformance(
            "normalize-task-id",
            {"id": "bogus"},
            expected={
                "cli_argv": ["--id", "1", "--json"],
                "exit_code": 0,
                "stderr_policy": "empty",
            },
            fixture_dir=tmp_path,
        )
    msg = str(excinfo.value)
    assert "exit_code mismatch" in msg or "canonical envelope mismatch" in msg


def test_run_conformance_detects_stderr_policy_violation(tmp_path: Path) -> None:
    """An ``stderr_policy="nonempty"`` expectation against a quiet command
    fails — the harness MUST distinguish text-vs-empty stderr."""
    with pytest.raises(AssertionError, match="non-empty stderr"):
        run_conformance(
            "normalize-task-id",
            {"id": "1"},
            expected={
                "cli_argv": ["--id", "1", "--json"],
                "exit_code": 0,
                "stderr_policy": "nonempty",
            },
            fixture_dir=tmp_path,
        )


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_canonicalize_envelope_reordered_keys_equal() -> None:
    a = {"b": 1, "a": [3, {"y": 2, "x": 1}]}
    b = {"a": [3, {"x": 1, "y": 2}], "b": 1}
    assert canonicalize_envelope(a) == canonicalize_envelope(b)


def test_canonicalize_envelope_stubs_iso_timestamps() -> None:
    a = {"ts": "2026-04-30T12:34:56Z", "extra": 1}
    b = {"ts": "2025-01-01T00:00:00+00:00", "extra": 1}
    canon_a = canonicalize_envelope(a)
    canon_b = canonicalize_envelope(b)
    assert canon_a == canon_b
    assert "<TIMESTAMP>" in canon_a


def test_canonicalize_envelope_distinct_envelopes_diverge() -> None:
    """Sanity: distinct payloads must NOT canonicalize to the same string."""
    a = {"normalized": "001"}
    b = {"error": "cannot normalize task id: 'bogus'"}
    assert canonicalize_envelope(a) != canonicalize_envelope(b)


# ---------------------------------------------------------------------------
# Filesystem snapshot
# ---------------------------------------------------------------------------


def test_snapshot_dir_detects_added_file(tmp_path: Path) -> None:
    pre = _snapshot_dir(tmp_path)
    (tmp_path / "new.txt").write_text("hello", encoding="utf-8")
    post = _snapshot_dir(tmp_path)
    assert pre != post
    assert "new.txt" in post and "new.txt" not in pre


def test_snapshot_dir_detects_modified_file(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("a", encoding="utf-8")
    pre = _snapshot_dir(tmp_path)
    (tmp_path / "x.txt").write_text("b", encoding="utf-8")
    post = _snapshot_dir(tmp_path)
    assert pre != post
    assert pre["x.txt"] == "a"
    assert post["x.txt"] == "b"


def test_run_conformance_detects_fs_invariant_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the in-process ``_run_*`` mutates the snapshotted directory, the
    fs_invariant assertion fires.

    We patch ``plan_ops._run_normalize_task_id`` to write a file under
    ``fixture_dir`` as a side effect, simulating a Tier-A regression where a
    nominally read-only command starts mutating the filesystem.
    """
    from tests.scripts.plan_ops_pure_harness import load_plan_ops

    plan_ops = load_plan_ops()
    original = plan_ops._run_normalize_task_id

    side_effect_path = tmp_path / "side_effect.txt"

    def mutating_run(payload: dict) -> dict:
        side_effect_path.write_text("oops", encoding="utf-8")
        return original(payload)

    monkeypatch.setattr(plan_ops, "_run_normalize_task_id", mutating_run)

    with pytest.raises(AssertionError, match="filesystem snapshot mismatch"):
        run_conformance(
            "normalize-task-id",
            {"id": "1"},
            expected={
                "cli_argv": ["--id", "1", "--json"],
                "exit_code": 0,
                "stderr_policy": "empty",
                "fs_invariant": True,
            },
            fixture_dir=tmp_path,
        )
