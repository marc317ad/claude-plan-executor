"""Tests for the hard review-evidence gate.

Covers `_review_evidence_gate` (pure helper) plus its three runtime hook
points — `commit-task`, `finalize-execution-log`, and `log-event run_end
outcome=success` — and the `audit-review-evidence` CLI exposed for parent
orchestrators. Trigger semantics: `implement_done` for the
`(run_id, task_id)` pair. Without it the gate is N/A so legacy / raw
callers (no run log written) keep working. With it, the gate enforces
the SKILL.md §Phase D contract that `review_start + review_done` precede
`commit_done` and the verdict is in the union of the existing commit-
allowed sets (`clean | minor-findings | ship | ship-with-fixes`).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

sys.path.insert(0, str(SCRIPTS_DIR))

import plan_ops  # noqa: E402


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [str(PY), str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else os.getcwd(),
        capture_output=True,
        text=True,
        check=False,
    )


def _parse_json(cp: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"stdout is not JSON:\nstdout={cp.stdout!r}\nstderr={cp.stderr!r}\nerr={e}"
        )


def _write_run_log(log_path: Path, events: list[dict]) -> None:
    """Write a JSONL run log from a list of event dicts."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


SAMPLE_PLAN_BODY = """# Plan: sample

**Created:** 2026-05-21
**Status:** in-progress
**Base branch:** main

## Context

Prose goes here.

## Tasks

### TASK-001: First task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/foo.py
- **Dependencies:** none

Details body.

### TASK-002: Second task

- **Status:** open
- **Agent:** codex
- **Files:**
  - src/bar.py
- **Dependencies:** none
"""


# ---------------------------------------------------------------------------
# Pure helper — `_review_evidence_gate` semantics
# ---------------------------------------------------------------------------


@pytest.fixture()
def log_path(tmp_path: Path) -> Path:
    return tmp_path / "_run_log.jsonl"


class TestReviewEvidenceGatePure:
    """Pure semantics of the in-process helper."""

    def test_pass_implement_review_start_review_done_accepted_verdict(
        self, log_path: Path
    ) -> None:
        _write_run_log(log_path, [
            {"event": "run_start", "run_id": "R1"},
            {"event": "implement_start", "run_id": "R1", "task_id": "001"},
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "clean"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is True
        row = report["per_task"]["001"]
        assert row["ok"] is True
        assert row["triggered"] is True
        assert row["failed"] is False
        assert row["evidence"]["last_review_verdict"] == "clean"
        assert row["reasons"] == []

    def test_fail_missing_review_start(self, log_path: Path) -> None:
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            # No review_start.
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is False
        codes = {r["code"] for r in report["per_task"]["001"]["reasons"]}
        assert "review-evidence-missing-review-start" in codes

    def test_fail_missing_review_done(self, log_path: Path) -> None:
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            # No review_done.
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is False
        codes = {r["code"] for r in report["per_task"]["001"]["reasons"]}
        assert "review-evidence-missing-review-done" in codes

    def test_fail_review_skipped_is_hard_failure(
        self, log_path: Path
    ) -> None:
        """`--skip-cross-review` writes `review_skipped` then goes to
        commit. The gate makes that mechanically impossible: review_skipped
        is a hard failure for the (run_id, task_id) pair even when
        review_start / review_done are also present (defense in depth).
        """
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_skipped", "run_id": "R1", "task_id": "001",
             "reason": "flag"},
            # Even an accepted review_done shouldn't rescue a task whose
            # log shows the operator opted into skip-cross-review.
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is False
        codes = {r["code"] for r in report["per_task"]["001"]["reasons"]}
        assert "review-evidence-review-skipped" in codes

    def test_fail_only_plan_review_done_no_task_review(
        self, log_path: Path
    ) -> None:
        """Plan-level review (`plan_review_done`) is unrelated to per-task
        review and must NOT satisfy the gate."""
        _write_run_log(log_path, [
            {"event": "plan_review_start", "run_id": "R1"},
            {"event": "plan_review_done", "run_id": "R1",
             "verdict": "approved"},
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            # Note: no `review_start` / `review_done` for the task.
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is False
        codes = {r["code"] for r in report["per_task"]["001"]["reasons"]}
        assert "review-evidence-missing-review-start" in codes
        assert "review-evidence-missing-review-done" in codes

    def test_fail_unacceptable_verdict(self, log_path: Path) -> None:
        """`needs-rework` is a valid review verdict but does not authorize
        commit; the gate must reject it just like
        `_validate_review_success_payload` does on the commit-task seam.
        """
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "needs-rework"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is False
        codes = {r["code"] for r in report["per_task"]["001"]["reasons"]}
        assert "review-evidence-unacceptable-verdict" in codes

    @pytest.mark.parametrize(
        "verdict",
        sorted(plan_ops.REVIEW_EVIDENCE_ACCEPTED_VERDICTS),
    )
    def test_pass_each_accepted_verdict(
        self, log_path: Path, verdict: str
    ) -> None:
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": verdict},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is True, report

    def test_remediation_path_uses_last_review_verdict(
        self, log_path: Path
    ) -> None:
        """D.2a.5 bounded remediation: first review returns `needs-rework`,
        remediator re-implements, second review returns `clean`. The gate
        must look at the LAST review_done, not the first.
        """
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "needs-rework"},
            {"event": "remediation_start", "run_id": "R1", "task_id": "001"},
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "clean"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is True

    def test_role_swap_path_with_ship_with_fixes(
        self, log_path: Path
    ) -> None:
        """D.2b role-swap on Codex-implemented work: Claude reviewer
        verdict `ship-with-fixes` is acceptable. Confirms the gate uses
        the SAME accepted-verdict set across reviewer identities (no
        per-reviewer carve-outs).
        """
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "needs-rework"},
            # Role-swap re-implementation, Claude reviewer this time.
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship-with-fixes"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is True

    def test_failed_task_exempt(self, log_path: Path) -> None:
        """A task that hit `fail-task` (review or commit stage failure) is
        exempt from the gate. Lifecycle authorization for `fail-task` is
        enforced separately via ALLOWED_FAIL_AUTHORIZATION_SOURCES.
        """
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "needs-rework"},
            {"event": "failed", "run_id": "R1", "task_id": "001",
             "stage": "review"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is True
        row = report["per_task"]["001"]
        assert row["failed"] is True
        assert row["reasons"] == []

    def test_no_implement_done_is_na(self, log_path: Path) -> None:
        """Legacy / raw callers that never wrote `implement_done` for a
        task get a pass — this is the backward-compat carve-out the user
        requested for historical logs."""
        _write_run_log(log_path, [
            {"event": "run_start", "run_id": "R1"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is True
        row = report["per_task"]["001"]
        assert row["triggered"] is False
        assert row["reasons"] == []

    def test_require_commit_done_fails_without_commit_event(
        self, log_path: Path
    ) -> None:
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
            # No commit_done yet — finalize would fail.
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
            require_commit_done=True,
        )
        assert report["ok"] is False
        codes = {r["code"] for r in report["per_task"]["001"]["reasons"]}
        assert "review-evidence-missing-commit-done" in codes

    def test_require_commit_done_passes_with_commit_event(
        self, log_path: Path
    ) -> None:
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
            {"event": "commit_done", "run_id": "R1", "task_id": "001",
             "commit_sha": "deadbeef"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
            require_commit_done=True,
        )
        assert report["ok"] is True

    def test_scans_all_tasks_when_task_ids_omitted(
        self, log_path: Path
    ) -> None:
        """Without explicit task_ids the gate enumerates every
        (run_id, task_id) seen in the log."""
        _write_run_log(log_path, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "clean"},
            {"event": "implement_done", "run_id": "R1", "task_id": "002"},
            # No review for 002.
        ])
        report = plan_ops._review_evidence_gate(log_path, "R1")
        assert report["ok"] is False
        assert report["per_task"]["001"]["ok"] is True
        assert report["per_task"]["002"]["ok"] is False

    def test_filters_other_run_ids(self, log_path: Path) -> None:
        """Events for a different run_id MUST NOT count toward this run's
        evidence — that was exactly the bypass the user is preventing."""
        _write_run_log(log_path, [
            # Different run: it HAS evidence.
            {"event": "implement_done", "run_id": "OTHER", "task_id": "001"},
            {"event": "review_start", "run_id": "OTHER", "task_id": "001"},
            {"event": "review_done", "run_id": "OTHER", "task_id": "001",
             "verdict": "clean"},
            # Our run: only implement_done.
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
        ])
        report = plan_ops._review_evidence_gate(
            log_path, "R1", task_ids=["001"],
        )
        assert report["ok"] is False

    def test_missing_log_file_is_na(self, tmp_path: Path) -> None:
        """Missing log file → no events known → every queried task is
        N/A (triggered=False)."""
        report = plan_ops._review_evidence_gate(
            tmp_path / "_run_log.jsonl", "R1", task_ids=["001"],
        )
        assert report["ok"] is True
        assert report["per_task"]["001"]["triggered"] is False


# ---------------------------------------------------------------------------
# commit-task hook
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh git repo + plan file + src tree. Mirrors the existing fixture
    in test_plan_ops.py but inlined here so the gate suite is independently
    runnable.
    """
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@test"], cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=tmp_path, check=True,
    )
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "sample.md"
    plan.write_text(SAMPLE_PLAN_BODY, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (src / "bar.py").write_text("y = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=tmp_path, check=True,
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_run_log(repo: Path, events: list[dict]) -> Path:
    """Seed `docs/plans/_run_log.jsonl` so the gate has events to scan."""
    log = repo / "docs" / "plans" / "_run_log.jsonl"
    _write_run_log(log, events)
    return log


class TestCommitTaskGate:
    """Hook 1: `commit-task` MUST refuse to flip a task to done without
    review evidence in the same `run_id`."""

    def test_commit_blocked_without_review_evidence(
        self, tmp_git_repo: Path
    ) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        _seed_run_log(tmp_git_repo, [
            # Implementer reached implement_done; orchestrator then tried
            # to commit without firing review — exactly the bypass the
            # user is fixing.
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
        ])
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            "--reviewer", "claude",
            "--reviewer-verdict", "ship",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        codes = {e["code"] for e in body["errors"]}
        assert "review-evidence-missing-review-start" in codes
        assert "review-evidence-missing-review-done" in codes
        # Plan status was NOT flipped to done.
        text = plan.read_text(encoding="utf-8")
        assert "### TASK-001: First task\n\n- **Status:** open" in text

    def test_commit_blocked_when_review_skipped(
        self, tmp_git_repo: Path
    ) -> None:
        """The `--skip-cross-review` pathway — historically the loud-banner
        bypass — is now mechanically blocked from completing a commit."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        _seed_run_log(tmp_git_repo, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_skipped", "run_id": "R1", "task_id": "001",
             "reason": "flag"},
        ])
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            # Skip-review path uses `--reviewer none`. Verdict whitelist
            # validation accepts the empty string here.
            "--reviewer", "none",
            "--reviewer-verdict", "",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        codes = {e["code"] for e in body["errors"]}
        assert "review-evidence-review-skipped" in codes

    def test_commit_passes_with_full_review_evidence(
        self, tmp_git_repo: Path
    ) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        _seed_run_log(tmp_git_repo, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
        ])
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            "--reviewer", "claude",
            "--reviewer-verdict", "ship",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["commit_sha"]
        assert body["status_updated"] is True

    def test_dry_run_bypasses_gate(self, tmp_git_repo: Path) -> None:
        """`--dry-run` returns early BEFORE the gate (and before any git
        state mutation) so plan-review-only / preview flows stay green
        even when the run log is empty."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            "--reviewer", "claude",
            "--reviewer-verdict", "ship",
            "--dry-run",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["dry_run"] is True
        assert body["would_commit"] is True

    def test_legacy_no_run_log_still_commits(
        self, tmp_git_repo: Path
    ) -> None:
        """Backward-compat: a raw commit-task call with NO prior
        `_run_log.jsonl` (no `implement_done` for the task) trips the
        N/A path and commits as before. Preserves existing test fixtures
        that don't bother to seed the log."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            "--reviewer", "none",
            "--reviewer-verdict", "",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr


# ---------------------------------------------------------------------------
# finalize-execution-log + log-event run_end backstop
# ---------------------------------------------------------------------------


class TestFinalizeAndRunEndGate:
    """Hook 2 + 3: `finalize-execution-log --outcome success` and
    `log-event --event run_end ... outcome:"success"` are the run-level
    success markers and must refuse to write when any implemented task
    lacks review evidence."""

    def test_finalize_success_blocks_when_implemented_task_missing_review(
        self, tmp_git_repo: Path
    ) -> None:
        _seed_run_log(tmp_git_repo, [
            # TASK-001 fully reviewed + committed.
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
            {"event": "commit_done", "run_id": "R1", "task_id": "001"},
            # TASK-002 implemented but NEVER reviewed (the bypass).
            {"event": "implement_done", "run_id": "R1", "task_id": "002"},
            {"event": "commit_done", "run_id": "R1", "task_id": "002"},
        ])
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        rows = json.dumps([
            {"task": "001", "agent": "claude", "reviewer": "claude",
             "verdict": "ship", "commit": "deadbeef", "notes": ""},
            {"task": "002", "agent": "codex", "reviewer": "",
             "verdict": "", "commit": "cafebabe", "notes": ""},
        ])
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(plan),
            "--run-id", "R1",
            "--starting-sha", "AAA",
            "--ending-sha", "BBB",
            "--rows-json", rows,
            "--outcome", "success",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        codes = {e["code"] for e in body["errors"]}
        assert "review-evidence-gate-failed" in codes
        # Per-task reasons are propagated with `task_id` annotation so
        # the operator sees which task tripped the gate.
        per_task_failures = [
            e for e in body["errors"] if e.get("task_id") == "002"
        ]
        assert per_task_failures, body
        # Plan markdown was NOT mutated — finalize is fail-closed.
        assert "## Execution log — R1" not in plan.read_text(encoding="utf-8")

    def test_finalize_partial_outcome_does_not_invoke_gate(
        self, tmp_git_repo: Path
    ) -> None:
        """A `partial` finalize is the orchestrator saying "some tasks
        failed"; we don't claim every implemented task shipped, so the
        gate stays off. Only `success` is enforced."""
        _seed_run_log(tmp_git_repo, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            # No review evidence — this would have failed `outcome=success`.
        ])
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        rows = json.dumps([
            {"task": "001", "agent": "claude", "reviewer": "",
             "verdict": "", "commit": "", "notes": "failed"},
        ])
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(plan),
            "--run-id", "R1",
            "--starting-sha", "AAA",
            "--ending-sha", "BBB",
            "--rows-json", rows,
            "--outcome", "partial",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr

    def test_finalize_success_passes_when_every_task_has_evidence(
        self, tmp_git_repo: Path
    ) -> None:
        _seed_run_log(tmp_git_repo, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "ship"},
            {"event": "commit_done", "run_id": "R1", "task_id": "001"},
        ])
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        rows = json.dumps([
            {"task": "001", "agent": "claude", "reviewer": "claude",
             "verdict": "ship", "commit": "deadbeef", "notes": ""},
        ])
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(plan),
            "--run-id", "R1",
            "--starting-sha", "AAA",
            "--ending-sha", "BBB",
            "--rows-json", rows,
            "--outcome", "success",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr

    def test_log_event_run_end_success_blocks_on_missing_evidence(
        self, tmp_git_repo: Path
    ) -> None:
        """The terminal `run_end outcome=success` write is the strongest
        backstop — even if a buggy orchestrator skipped finalize, the
        log_event hook refuses to durably mark the run as successful
        when review evidence is missing."""
        _seed_run_log(tmp_git_repo, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            # No review.
            {"event": "commit_done", "run_id": "R1", "task_id": "001"},
        ])
        fields = json.dumps({
            "run_id": "R1",
            "plan_file": "sample.md",
            "outcome": "success",
            "done": 1, "failed": 0,
        })
        cp = _run(
            "log-event",
            "--event", "run_end",
            "--fields-json", fields,
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        codes = {e["code"] for e in body["errors"]}
        assert "review-evidence-gate-failed" in codes

    def test_log_event_run_end_paused_does_not_invoke_gate(
        self, tmp_git_repo: Path
    ) -> None:
        """`outcome=paused` records an awaiting-user pause and does not
        claim shipping — gate stays off so the pause/resume contract
        keeps working."""
        _seed_run_log(tmp_git_repo, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
        ])
        fields = json.dumps({
            "run_id": "R1",
            "plan_file": "sample.md",
            "outcome": "paused",
        })
        cp = _run(
            "log-event",
            "--event", "run_end",
            "--fields-json", fields,
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr


# ---------------------------------------------------------------------------
# audit-review-evidence CLI
# ---------------------------------------------------------------------------


class TestAuditReviewEvidenceCli:
    """End-to-end checks for the new parent-orchestrator audit
    subcommand."""

    def test_cli_passes_on_clean_run(
        self, tmp_path: Path
    ) -> None:
        log = tmp_path / "_run_log.jsonl"
        _write_run_log(log, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "clean"},
            {"event": "commit_done", "run_id": "R1", "task_id": "001"},
        ])
        cp = _run(
            "audit-review-evidence",
            "--run-id", "R1",
            "--run-log", str(log),
            "--require-commit-done",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["ok"] is True
        assert body["per_task"]["001"]["ok"] is True

    def test_cli_fails_on_skipped_run(self, tmp_path: Path) -> None:
        log = tmp_path / "_run_log.jsonl"
        _write_run_log(log, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_skipped", "run_id": "R1", "task_id": "001"},
        ])
        cp = _run(
            "audit-review-evidence",
            "--run-id", "R1",
            "--run-log", str(log),
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body["ok"] is False
        codes = {
            r["code"]
            for r in body["per_task"]["001"]["reasons"]
        }
        assert "review-evidence-review-skipped" in codes

    def test_cli_scopes_to_explicit_task_ids(self, tmp_path: Path) -> None:
        log = tmp_path / "_run_log.jsonl"
        _write_run_log(log, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "clean"},
            {"event": "implement_done", "run_id": "R1", "task_id": "002"},
            # 002 has no review evidence.
        ])
        # Audit only 001: expect pass.
        cp = _run(
            "audit-review-evidence",
            "--run-id", "R1",
            "--task-ids", "001",
            "--run-log", str(log),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert set(body["per_task"]) == {"001"}, body
        # Audit both: expect fail, and 002 surfaces missing review.
        cp = _run(
            "audit-review-evidence",
            "--run-id", "R1",
            "--task-ids", "001,002",
            "--run-log", str(log),
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body["per_task"]["001"]["ok"] is True
        assert body["per_task"]["002"]["ok"] is False

    def test_cli_normalizes_task_id_input(self, tmp_path: Path) -> None:
        """`--task-ids 1` / `TASK-001` / `001` all canonicalize the same
        way the rest of plan_ops does."""
        log = tmp_path / "_run_log.jsonl"
        _write_run_log(log, [
            {"event": "implement_done", "run_id": "R1", "task_id": "001"},
            {"event": "review_start", "run_id": "R1", "task_id": "001"},
            {"event": "review_done", "run_id": "R1", "task_id": "001",
             "verdict": "clean"},
        ])
        for raw in ("1", "001", "TASK-001"):
            cp = _run(
                "audit-review-evidence",
                "--run-id", "R1",
                "--task-ids", raw,
                "--run-log", str(log),
                "--json",
            )
            assert cp.returncode == 0, (raw, cp.stderr)
            body = _parse_json(cp)
            assert "001" in body["per_task"], (raw, body)

    def test_cli_rejects_missing_run_id(self, tmp_path: Path) -> None:
        cp = _run("audit-review-evidence", "--json")
        # argparse handles --run-id as required.
        assert cp.returncode != 0
