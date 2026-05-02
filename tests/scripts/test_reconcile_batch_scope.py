from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from test_plan_ops import (
    _parse_json,
    _reconcile_git_repo,
    _run_reconcile,
    _scope_envelope,
    _write_reconcile_schedule,
)


def test_declared_directory_scope_covers_nested_tracked_and_untracked(
    tmp_path: Path,
) -> None:
    repo = _reconcile_git_repo(tmp_path)
    tracked = repo / "pkg" / "existing.py"
    tracked.parent.mkdir()
    tracked.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "pkg/existing.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add pkg"], cwd=repo, check=True)
    tracked.write_text("v2\n", encoding="utf-8")
    untracked = repo / "pkg" / "new.py"
    untracked.write_text("new\n", encoding="utf-8")

    sched = _write_reconcile_schedule(
        tmp_path, task_id="008", files=["pkg/ (create)"],
    )
    envelope = _scope_envelope(
        "008", tracked=["pkg/existing.py"], untracked=["pkg/new.py"],
    )

    cp = _run_reconcile(
        repo,
        [envelope],
        schedule_file=sched,
        out_of_scope_policy="reconcile-and-revert",
    )

    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    result = _parse_json(cp)["results"][0]
    assert result["outcome"] == "scope_violation_preserved"
    assert result["reconcile_kept_tracked"] == ["pkg/existing.py"]
    assert result["reconcile_kept_untracked"] == ["pkg/new.py"]
    assert tracked.read_text(encoding="utf-8") == "v2\n"
    assert untracked.exists()


def test_declared_directory_scope_strips_known_annotations_case_insensitively(
    tmp_path: Path,
) -> None:
    repo = _reconcile_git_repo(tmp_path)
    nested = repo / "src" / "generated" / "file.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("new\n", encoding="utf-8")
    sched = _write_reconcile_schedule(
        tmp_path, task_id="008", files=["`src/generated` ( MoDiFy )"],
    )

    cp = _run_reconcile(
        repo,
        [_scope_envelope("008", untracked=["src/generated/file.py"])],
        schedule_file=sched,
        out_of_scope_policy="reconcile-and-revert",
    )

    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    result = _parse_json(cp)["results"][0]
    assert result["outcome"] == "scope_violation_preserved"
    assert result["reconcile_kept_untracked"] == ["src/generated/file.py"]
    assert nested.exists()


def test_unknown_scope_annotation_does_not_widen_directory_scope(
    tmp_path: Path,
) -> None:
    repo = _reconcile_git_repo(tmp_path)
    nested = repo / "src" / "generated" / "file.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("new\n", encoding="utf-8")
    sched = _write_reconcile_schedule(
        tmp_path, task_id="008", files=["src/generated (invented)"],
    )

    cp = _run_reconcile(
        repo,
        [_scope_envelope("008", untracked=["src/generated/file.py"])],
        schedule_file=sched,
        out_of_scope_policy="reconcile-and-revert",
    )

    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    result = _parse_json(cp)["results"][0]
    assert result["outcome"] == "scope_violation_reconciled"
    assert result["reconciled_untracked"] == ["src/generated/file.py"]
    assert not nested.exists()


def test_real_out_of_scope_file_still_pauses_under_default_policy(
    tmp_path: Path,
) -> None:
    repo = _reconcile_git_repo(tmp_path)
    stray = repo / "outside.py"
    stray.write_text("new\n", encoding="utf-8")
    sched = _write_reconcile_schedule(
        tmp_path, task_id="008", files=["src/ (create)"],
    )

    cp = _run_reconcile(
        repo,
        [_scope_envelope("008", untracked=["outside.py"])],
        schedule_file=sched,
    )

    assert cp.returncode == 0, (cp.stdout, cp.stderr)
    body = json.loads(cp.stdout)
    assert body["paused"] is True
    assert body["results"][0]["outcome"] == "scope_violation_paused"
    assert stray.exists()
