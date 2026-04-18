"""Unit tests for scripts/plan_ops.py.

Exercises every subcommand plus the shared helpers. Tests start RED against the
scaffolding and go GREEN as subcommand bodies are implemented.

Focus areas:
  * Structural **Status:** bullet mutation across open/in-progress/done/failed
    (regression: prior `\\w+` regex broke on `in-progress`).
  * Run-log append with tail verification.
  * Lock file acquire/release semantics, including conflict detection.
  * Task-id normalization across `1`, `001`, `TASK-001`.
  * Batch scheduler file-lock + scheduler-stuck detection.
  * parse-schedule validation branches.
  * commit-task rollback path on simulated git failure.
"""

from __future__ import annotations

import json
import os
import re
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


def _run(*args: str, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    cmd = [str(PY), str(SCRIPT), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else os.getcwd(),
        capture_output=True,
        text=True,
        check=check,
    )


def _parse_json(cp: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(cp.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"stdout is not JSON:\nstdout={cp.stdout!r}\nstderr={cp.stderr!r}\nerr={e}"
        )


# ---------------------------------------------------------------------------
# Helpers: fixtures + sample plan body builder
# ---------------------------------------------------------------------------


SAMPLE_PLAN_BODY = """# Plan: sample

**Created:** 2026-04-13
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

### TASK-002: Second task with hyphen value

- **Status:** in-progress
- **Agent:** codex
- **Files:**
  - src/bar.py
- **Dependencies:** [001]

### TASK-003: Third task

- **Status:** done
- **Agent:** claude
- **Files:**
  - src/baz.py
- **Dependencies:** none
"""


SAMPLE_PLAN_BODY_MIXED = """# Plan: mixed

**Created:** 2026-04-14
**Status:** in-progress
**Base branch:** main

## Context

Prose goes here.

## Tasks

### TASK-001: Plain first task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/alpha.py
- **Dependencies:** none

Details for 001.

### TASK-004: Stem group task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/stem.py
- **Dependencies:** none
- **Test command:** `venv/bin/pytest tests/test_stem.py -q`

Body for the plain 004 block. It shares its numeric group stem with
the suffixed leaves 004A / 004B but is a wholly independent task.

### TASK-004A: Suffixed leaf task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/leaf_a.py
- **Dependencies:** [004]
- **Test command:** `venv/bin/pytest tests/test_leaf_a.py -q`

Body for the 004A suffixed leaf. It is not an alias of 004; the
orchestrator treats both as independent leaves that happen to share
a bookkeeping-only group stem.
"""


@pytest.fixture()
def isolated_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated workspace with plan file + docs/plans layout.

    Points plan_ops' module globals at the tmp dir so subcommands
    writing to `docs/plans/_run_log.jsonl` stay sandboxed.
    """
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    plan = plans_dir / "sample.md"
    plan.write_text(SAMPLE_PLAN_BODY, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_dir)
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans_dir / "_run_log.jsonl")
    monkeypatch.setattr(plan_ops, "RUN_LOCK_PATH", plans_dir / "_run_lock.json")
    return plan


# ---------------------------------------------------------------------------
# normalize-task-id — already implemented in the scaffolding
# ---------------------------------------------------------------------------


class TestNormalizeTaskId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1", "001"),
            ("001", "001"),
            ("042", "042"),
            ("TASK-007", "007"),
            ("TASK-123", "123"),
            ("001A", "001A"),
            ("TASK-004B", "004B"),
            ("4A", "004A"),
            ("017Z", "017Z"),
            ("TASK-000A", "000A"),
            ("4a", "004A"),
            ("TASK-4a", "004A"),
            ("task-001", "001"),
        ],
    )
    def test_valid(self, raw: str, expected: str) -> None:
        cp = _run("normalize-task-id", "--id", raw, "--json")
        assert cp.returncode == 0, cp.stderr
        assert _parse_json(cp) == {"normalized": expected}

    @pytest.mark.parametrize(
        "raw",
        ["", "abc", "1000", "TASK-1000", "001AB", "001A1", "A001", "001-A"],
    )
    def test_invalid(self, raw: str) -> None:
        cp = _run("normalize-task-id", "--id", raw, "--json")
        assert cp.returncode == 1
        assert "error" in _parse_json(cp)


# ---------------------------------------------------------------------------
# Structural status mutation — regression against the `\w+` regex bug
# ---------------------------------------------------------------------------


class TestMutateTaskStatus:
    def test_open_to_in_progress(self) -> None:
        updated, prior = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "001", "in-progress")
        assert prior == "open"
        assert "### TASK-001: First task\n\n- **Status:** in-progress" in updated
        assert "### TASK-002: Second task" in updated

    def test_in_progress_to_done(self) -> None:
        updated, prior = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "002", "done")
        assert prior == "in-progress"
        assert "### TASK-002: Second task with hyphen value\n\n- **Status:** done" in updated

    def test_done_to_failed(self) -> None:
        updated, prior = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "003", "failed")
        assert prior == "done"
        assert "### TASK-003: Third task\n\n- **Status:** failed" in updated

    def test_idempotent_chain(self) -> None:
        step1, _ = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "001", "in-progress")
        step2, p2 = plan_ops.mutate_task_status(step1, "001", "done")
        assert p2 == "in-progress"
        assert "### TASK-001: First task\n\n- **Status:** done" in step2

    def test_invalid_status_rejected(self) -> None:
        with pytest.raises(ValueError):
            plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "001", "bogus")

    def test_missing_task_rejected(self) -> None:
        with pytest.raises(ValueError):
            plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "999", "done")

    def test_missing_status_bullet_rejected(self) -> None:
        body = SAMPLE_PLAN_BODY.replace("- **Status:** open\n", "")
        with pytest.raises(ValueError):
            plan_ops.mutate_task_status(body, "001", "done")

    def test_other_blocks_untouched(self) -> None:
        updated, _ = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "001", "done")
        assert "### TASK-002: Second task with hyphen value\n\n- **Status:** in-progress" in updated
        assert "### TASK-003: Third task\n\n- **Status:** done" in updated


# ---------------------------------------------------------------------------
# log-event — JSONL append + tail verification
# ---------------------------------------------------------------------------


class TestLogEvent:
    def test_appends_one_line(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "run_start",
            "--fields-json", '{"run_id":"R1","plan_file":"sample.md","mode":"execute"}',
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["ok"] is True
        line = res["written_line"]
        log = plan_ops.RUN_LOG_PATH.read_text(encoding="utf-8").splitlines()
        assert len(log) == 1
        rec = json.loads(log[0])
        assert rec["event"] == "run_start"
        assert rec["run_id"] == "R1"
        assert "ts" in rec
        assert json.loads(line) == rec

    def test_appends_many(self, isolated_plan: Path) -> None:
        for i in range(3):
            cp = _run(
                "log-event",
                "--event", "implement_start",
                "--fields-json", f'{{"run_id":"R{i}","task_id":"00{i}"}}',
                "--json",
            )
            assert cp.returncode == 0

        lines = plan_ops.RUN_LOG_PATH.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        ids = [json.loads(ln)["task_id"] for ln in lines]
        assert ids == ["000", "001", "002"]

    def test_rejects_bad_json(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "run_start",
            "--fields-json", "{not json",
            "--json",
        )
        assert cp.returncode != 0


# ---------------------------------------------------------------------------
# acquire-lock / release-lock
# ---------------------------------------------------------------------------


class TestLock:
    def test_acquire_then_release(self, isolated_plan: Path) -> None:
        cp1 = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp1.returncode == 0, cp1.stderr
        assert _parse_json(cp1)["acquired"] is True
        assert plan_ops.RUN_LOCK_PATH.exists()

        cp2 = _run(
            "release-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp2.returncode == 0, cp2.stderr
        assert not plan_ops.RUN_LOCK_PATH.exists()

    def test_conflict_on_same_plan(self, isolated_plan: Path) -> None:
        cp1 = _run("acquire-lock", "--plan-file", str(isolated_plan), "--run-id", "R1", "--json")
        assert cp1.returncode == 0

        cp2 = _run("acquire-lock", "--plan-file", str(isolated_plan), "--run-id", "R2", "--json")
        assert cp2.returncode != 0
        body = _parse_json(cp2)
        assert body.get("acquired") is False
        assert body.get("conflict_run_id") == "R1"

    def test_release_unknown_run_is_noop(self, isolated_plan: Path) -> None:
        cp = _run(
            "release-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_missing",
            "--json",
        )
        assert cp.returncode == 0


# ---------------------------------------------------------------------------
# update-plan-header
# ---------------------------------------------------------------------------


class TestUpdatePlanHeader:
    def test_in_progress_to_complete(self, isolated_plan: Path) -> None:
        cp = _run(
            "update-plan-header",
            "--plan-file", str(isolated_plan),
            "--status", "complete",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        text = isolated_plan.read_text(encoding="utf-8")
        assert "**Status:** complete" in text
        assert "**Status:** in-progress" not in text.split("## Tasks")[0]

    def test_partial(self, isolated_plan: Path) -> None:
        cp = _run(
            "update-plan-header",
            "--plan-file", str(isolated_plan),
            "--status", "partial",
            "--json",
        )
        assert cp.returncode == 0
        assert "**Status:** partial" in isolated_plan.read_text(encoding="utf-8")

    def test_invalid_status_rejected(self, isolated_plan: Path) -> None:
        cp = _run(
            "update-plan-header",
            "--plan-file", str(isolated_plan),
            "--status", "bogus",
            "--json",
        )
        assert cp.returncode != 0


# ---------------------------------------------------------------------------
# parse-schedule
# ---------------------------------------------------------------------------


VALID_SCHEDULE = {
    "outcome": "valid",
    "tasks": [
        {
            "id": "001",
            "agent": "claude",
            "files": ["src/foo.py"],
            "dependencies": [],
            "acceptance_criteria": ["passes"],
        },
        {
            "id": "002",
            "agent": "codex",
            "files": ["src/bar.py"],
            "dependencies": ["001"],
            "acceptance_criteria": ["passes"],
        },
    ],
    "batches": [
        {"index": 0, "task_ids": ["001"], "file_locks": ["src/foo.py"]},
        {"index": 1, "task_ids": ["002"], "file_locks": ["src/bar.py"]},
    ],
    "gaps": [],
    "risks": [],
}


LEGACY_ALIAS_SCHEDULE = {
    "outcome": "valid",
    "tasks": [
        {
            "task_id": "001",
            "agent": "claude",
            "files": ["src/foo.py"],
            "dependencies": [],
            "acceptance_criteria": ["passes"],
        },
    ],
    "batches": [
        {"batch_index": 0, "task_ids": ["001"], "file_locks": ["src/foo.py"]},
    ],
    "gaps": [],
    "risks": [],
}


class TestParseSchedule:
    def test_valid(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(VALID_SCHEDULE),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["outcome"] == "valid"
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]
        assert body["batches"][0]["task_ids"] == ["001"]
        assert body.get("warnings") == []

    def test_needs_enrichment(self) -> None:
        data = dict(VALID_SCHEDULE)
        data = json.loads(json.dumps(VALID_SCHEDULE))
        data["outcome"] = "needs-enrichment"
        data["gaps"] = ["TASK-002 missing acceptance criteria"]
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(data),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["outcome"] == "needs-enrichment"
        assert body["gaps"]

    def test_invalid_shape(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input='{"outcome":"valid"}',
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("errors")

    def test_malformed_json(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input="not json",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0

    def test_accepts_canonical_id_index(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a.txt"], "dependencies": []}
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a.txt"]}
            ],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["errors"] == []
        assert body.get("warnings") == []

    def test_task_id_alias_emits_warning(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(LEGACY_ALIAS_SCHEDULE),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["errors"] == []
        warnings = body.get("warnings") or []
        assert any("task_id" in w for w in warnings), warnings

    def test_batch_index_alias_emits_warning(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(LEGACY_ALIAS_SCHEDULE),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        warnings = body.get("warnings") or []
        assert any("batch_index" in w for w in warnings), warnings

    def test_parse_schedule_rejects_needs_enrichment_with_empty_gaps(self) -> None:
        data = json.loads(json.dumps(VALID_SCHEDULE))
        data["outcome"] = "needs-enrichment"
        data["gaps"] = []
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(data),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors") or []
        assert any(e.get("code") == "outcome-gap-mismatch" for e in errors), errors

    def test_parse_schedule_rejects_valid_with_nonempty_gaps(self) -> None:
        data = json.loads(json.dumps(VALID_SCHEDULE))
        data["outcome"] = "valid"
        data["gaps"] = ["some gap entry"]
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(data),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors") or []
        assert any(e.get("code") == "outcome-gap-mismatch" for e in errors), errors

    def test_parse_schedule_accepts_invalid_with_any_gaps(self) -> None:
        data = json.loads(json.dumps(VALID_SCHEDULE))
        data["outcome"] = "invalid"
        data["gaps"] = ["structural failure reason"]
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(data),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["outcome"] == "invalid"
        assert not any(e.get("code") == "outcome-gap-mismatch" for e in (body.get("errors") or []))


# ---------------------------------------------------------------------------
# compute-schedule
# ---------------------------------------------------------------------------


class TestComputeSchedule:
    def _run_compute(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def test_disjoint_files_single_batch(self) -> None:
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": ["001"]},
                {"id": "003", "priority": "high", "files": ["c.py"], "dependencies": ["002"]},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["001", "002", "003"]
        assert body["batches"] == [
            {"index": 1, "task_ids": ["001", "002", "003"], "file_locks": ["a.py", "b.py", "c.py"]}
        ]

    def test_parallel_disjoint_files(self) -> None:
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
                {"id": "002", "priority": "medium", "files": ["b.py"], "dependencies": []},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["001", "002"]
        assert body["batches"] == [
            {"index": 1, "task_ids": ["001", "002"], "file_locks": ["a.py", "b.py"]}
        ]

    def test_file_conflict_forces_split(self) -> None:
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["shared.py"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["shared.py"], "dependencies": []},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert [batch["task_ids"] for batch in body["batches"]] == [["001"], ["002"]]

    def test_priority_ordering(self) -> None:
        payload = {
            "tasks": [
                {"id": "001", "priority": "medium", "files": ["a.py"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": []},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["002", "001"]

    def test_alnum_suffix_ordering(self) -> None:
        payload = {
            "tasks": [
                {"id": "004B", "priority": "high", "files": ["b.py"], "dependencies": []},
                {"id": "004A", "priority": "high", "files": ["a.py"], "dependencies": []},
                {"id": "004", "priority": "high", "files": ["base.py"], "dependencies": []},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["004", "004A", "004B"]

    def test_compute_batches_defaults_missing_priority_to_low(self) -> None:
        payload = {
            "tasks": [
                {"id": "001", "files": ["a.py"]},
                {"id": "002", "priority": "", "files": ["b.py"]},
                {"id": "003", "priority": "banana", "files": ["c.py"]},
                {"id": "004", "priority": "high", "files": ["d.py"]},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["004", "001", "002", "003"]

    def test_accepts_analyst_json_wrapper(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
            ],
            "batches": [],
            "gaps": [],
            "risks": [],
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["001"]
        assert body["batches"] == [
            {"index": 1, "task_ids": ["001"], "file_locks": ["a.py"]}
        ]


# ---------------------------------------------------------------------------
# Canonical-contract constants regression
# ---------------------------------------------------------------------------


class TestCanonicalContractConstants:
    def test_pending_in_allowed_statuses(self) -> None:
        assert "pending" in plan_ops.ALLOWED_TASK_STATUSES

    def test_open_alias_present_until_task_006(self) -> None:
        # Retained as an explicit alias while sample_phase4 still emits `open`.
        # Removal is TASK-006's responsibility; see DUAL_AGENT_PLAN_EXECUTOR.md §5.
        assert plan_ops.STATUS_ALIASES.get("open") == "pending"
        assert "open" in plan_ops.ALLOWED_TASK_STATUSES

    def test_schedule_field_aliases_declared(self) -> None:
        assert plan_ops.SCHEDULE_FIELD_ALIASES == {
            "task_id": "id",
            "batch_index": "index",
        }


# ---------------------------------------------------------------------------
# batch-next
# ---------------------------------------------------------------------------


class TestBatchNext:
    def _schedule_path(self, tmp_path: Path) -> Path:
        p = tmp_path / "schedule.json"
        p.write_text(json.dumps(VALID_SCHEDULE), encoding="utf-8")
        return p

    def test_first_batch(self, tmp_path: Path) -> None:
        # Under authoritative batch semantics (TASK-004B), `batch-next` returns
        # ONLY tasks from the first unresolved batch. VALID_SCHEDULE puts 001
        # in batch 0 and 002 in batch 1 — 002 must NOT be picked until batch
        # 0 resolves. Previously this test asserted [001, 002] globally,
        # which was documenting the ISSUE-010 bug.
        sched = self._schedule_path(tmp_path)
        cp = _run(
            "batch-next",
            "--schedule-file", str(sched),
            "--locked-files", "",
            "--done", "",
            "--failed", "",
            "--parallel", "2",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == ["001"]
        assert body["batch_index"] == 0
        assert body["scheduler_stuck"] is False

    def test_skip_locked_files(self, tmp_path: Path) -> None:
        # Locking batch 0's file now yields picked=[] with scheduler_stuck=True
        # because 002 is in batch 1 and ineligible until batch 0 finishes.
        # Previously this test returned [002]; that was the ISSUE-010 bug.
        sched = self._schedule_path(tmp_path)
        cp = _run(
            "batch-next",
            "--schedule-file", str(sched),
            "--locked-files", "src/foo.py",
            "--done", "",
            "--failed", "",
            "--parallel", "2",
            "--json",
        )
        assert cp.returncode == 0
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_batch_next_blocks_dependent_on_failed(self, tmp_path: Path) -> None:
        # 001 failed → batch 0 is resolved (done|failed) → advance to batch 1.
        # 002 depends on 001; dep in `failed` means _ready(002) is False, so
        # 002 is NOT picked. scheduler_stuck=True because batch 1 has an
        # unfinished task that cannot run. Previously the test (named
        # test_batch_next_ignores_upstream_failure) asserted "002" IS picked,
        # documenting the bug where failed deps were silently ignored. V14
        # locks in the correct semantics.
        sched = self._schedule_path(tmp_path)
        cp = _run(
            "batch-next",
            "--schedule-file", str(sched),
            "--failed", "001",
            "--parallel", "2",
            "--json",
        )
        assert cp.returncode == 0
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_all_done_returns_empty(self, tmp_path: Path) -> None:
        sched = self._schedule_path(tmp_path)
        cp = _run(
            "batch-next",
            "--schedule-file", str(sched),
            "--done", "001,002",
            "--parallel", "2",
            "--json",
        )
        assert cp.returncode == 0
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is False


# ---------------------------------------------------------------------------
# batch-next — TASK-004B batch fidelity + deadlock detection (V1-V14)
# ---------------------------------------------------------------------------


def _write_schedule(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "schedule.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _run_batch_next(
    sched: Path,
    *,
    locked: str = "",
    done: str = "",
    failed: str = "",
    parallel: int = 2,
) -> subprocess.CompletedProcess:
    return _run(
        "batch-next",
        "--schedule-file", str(sched),
        "--locked-files", locked,
        "--done", done,
        "--failed", failed,
        "--parallel", str(parallel),
        "--json",
    )


class TestBatchNextBatchFidelity:
    """V1-V14 regressions for TASK-004B: authoritative batches + deadlock.

    These tests lock in the `batch-next` contract:
      * Later-batch ready tasks are ineligible until the active batch
        resolves.
      * `active_batch` advances past `done OR failed` batches.
      * `scheduler_stuck` covers cross-batch deadlock.
      * Malformed batch structure surfaces `scheduler_stuck=True`, never
        silent success.
      * Failed deps block pick even inside the active batch.
    """

    def test_batch_next_honors_declared_batch(self, tmp_path: Path) -> None:
        # V1. batch 1 = [001, 003], batch 2 = [002]. All three are globally
        # ready (001, 002, 003 all have empty deps). batch-next MUST return
        # ONLY [001, 003] from batch 1. 002 is globally ready but in batch 2
        # and therefore ineligible.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "003"], "file_locks": ["a", "c"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        })
        cp = _run_batch_next(sched, parallel=3)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert sorted(body["task_ids"]) == ["001", "003"]
        assert "002" not in body["task_ids"]
        assert body["batch_index"] == 1
        assert body["scheduler_stuck"] is False

    def test_batch_next_waits_for_earlier_batch_completion(
        self, tmp_path: Path
    ) -> None:
        # V2. Same schedule as V1; 001 and 003 done. batch-next must now
        # advance to batch 2 and return [002].
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "003"], "file_locks": ["a", "c"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        })
        cp = _run_batch_next(sched, done="001,003", parallel=3)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == ["002"]
        assert body["batch_index"] == 2
        assert body["scheduler_stuck"] is False

    def test_batch_next_scheduler_stuck_on_cross_batch_deadlock(
        self, tmp_path: Path
    ) -> None:
        # V3 — hard-won regression from run 20260415T000811.
        # batch 1 = [001 dep 002], batch 2 = [002]. Nothing done/failed.
        # active_batch = batch 1. 001 is not ready (dep 002 not done).
        # ready_in_batch = []. scheduler_stuck MUST be True. The previous
        # formula `picked==[] and len(ready_in_batch)>0` returned False
        # here and let the orchestrator spin.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["002"]},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        })
        cp = _run_batch_next(sched)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_batch_next_scheduler_stuck_positive_control(
        self, tmp_path: Path
    ) -> None:
        # V3 inverse: same cross-batch dep structure but with 002 done.
        # batch 1's 001 is now ready; batch-next picks it and
        # scheduler_stuck=False.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["002"]},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        })
        cp = _run_batch_next(sched, done="002")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == ["001"]
        assert body["scheduler_stuck"] is False

    def test_batch_next_advances_past_done_or_failed_batch(
        self, tmp_path: Path
    ) -> None:
        # V4 — hard-won regression from run 20260415T022232.
        # batch 1 = [001, 002], batch 2 = [003]. 001 done, 002 failed.
        # batch-next MUST advance to batch 2 and return [003]. The previous
        # formula `all(tid in done for tid in bids)` returned False (002
        # not in done) and never advanced.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"], "file_locks": ["a", "b"]},
                {"index": 2, "task_ids": ["003"], "file_locks": ["c"]},
            ],
        })
        cp = _run_batch_next(sched, done="001", failed="002")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == ["003"]
        assert body["batch_index"] == 2
        assert body["scheduler_stuck"] is False

    def test_batch_next_advances_past_all_done_batch(self, tmp_path: Path) -> None:
        # V4 positive control: both tasks in batch 1 done. advance to
        # batch 2 — proves the `or failed` extension didn't break the
        # all-done case.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"], "file_locks": ["a", "b"]},
                {"index": 2, "task_ids": ["003"], "file_locks": ["c"]},
            ],
        })
        cp = _run_batch_next(sched, done="001,002")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == ["003"]
        assert body["batch_index"] == 2
        assert body["scheduler_stuck"] is False

    def test_batch_next_respects_parallel_cap(self, tmp_path: Path) -> None:
        # V5 — batch 1 has three ready, file-disjoint tasks; --parallel 2
        # returns exactly 2.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002", "003"],
                 "file_locks": ["a", "b", "c"]},
            ],
        })
        cp = _run_batch_next(sched, parallel=2)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert len(body["task_ids"]) == 2
        assert set(body["task_ids"]).issubset({"001", "002", "003"})
        assert body["scheduler_stuck"] is False

    def test_batch_next_respects_file_locks_partial(self, tmp_path: Path) -> None:
        # V6 case A: batch 1 = [001 files=[a], 002 files=[a]]. Both ready.
        # batch-next returns exactly one; the other is file-claimed by the
        # first pick. picked != [], so scheduler_stuck=False.
        # Note: we disable _validate_schedule_refs's batch-file-overlap
        # check by using two different files in the same batch... actually
        # overlap within a batch fails validation. So model V6A as an
        # external lock scenario instead — see V9 below. Here we only test
        # the file-lock "partial pick" via --parallel=1 on two disjoint
        # file tasks.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"], "file_locks": ["a", "b"]},
            ],
        })
        cp = _run_batch_next(sched, parallel=1)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert len(body["task_ids"]) == 1
        assert body["scheduler_stuck"] is False

    def test_batch_next_respects_file_locks_full(self, tmp_path: Path) -> None:
        # V6 case B: batch 1 has a single ready task whose file is
        # externally locked. batch-next returns picked=[] and
        # scheduler_stuck=True (only unfinished active-batch task is
        # blocked).
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
            ],
        })
        cp = _run_batch_next(sched, locked="a")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_batch_next_empty_when_all_batches_done(self, tmp_path: Path) -> None:
        # V7 — every task done. picked=[], scheduler_stuck=False.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        })
        cp = _run_batch_next(sched, done="001,002")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is False

    def test_batch_next_dag_cycle_rejected(self, tmp_path: Path) -> None:
        # V8 — feed a cycle (001 <-> 002) through batch-next. Must halt
        # with errors[*].code == "dependency-cycle".
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": ["002"]},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"]},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"],
                 "file_locks": ["a", "b"]},
            ],
        })
        cp = _run_batch_next(sched)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "dependency-cycle" in codes

    def test_batch_next_external_lock_blocks_pick(self, tmp_path: Path) -> None:
        # V9 — batch 1 = [001 files=[a], 002 files=[b]]. Both ready.
        # --locked-files=a → returns [002], scheduler_stuck=False.
        # --locked-files=a,b → returns [], scheduler_stuck=True.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"], "file_locks": ["a", "b"]},
            ],
        })
        cp = _run_batch_next(sched, locked="a")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == ["002"]
        assert body["scheduler_stuck"] is False

        cp2 = _run_batch_next(sched, locked="a,b")
        assert cp2.returncode == 0, cp2.stderr
        body2 = _parse_json(cp2)
        assert body2["task_ids"] == []
        assert body2["scheduler_stuck"] is True

    def test_batch_next_output_shape(self, tmp_path: Path) -> None:
        # V10 — output JSON has exactly {batch_index, task_ids, file_locks,
        # scheduler_stuck}. No extras, no omissions.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
            ],
        })
        cp = _run_batch_next(sched)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert set(body.keys()) == {
            "batch_index", "task_ids", "file_locks", "scheduler_stuck"
        }

    def test_batch_next_emits_stuck_when_batches_missing(
        self, tmp_path: Path
    ) -> None:
        # V11 — tasks=[{id=001}], batches=[]. Nothing done/failed.
        # scheduler_stuck=True required; silent scheduler_stuck=False
        # would let the orchestrator spin forever.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
            ],
            "batches": [],
        })
        cp = _run_batch_next(sched)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_batch_next_emits_stuck_when_active_batch_is_empty(
        self, tmp_path: Path
    ) -> None:
        # V12 — single task 001 pending, sole batch has empty task_ids=[].
        # MUST surface scheduler_stuck=True.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": [], "file_locks": []},
            ],
        })
        cp = _run_batch_next(sched)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_batch_next_emits_stuck_when_task_is_not_in_any_batch(
        self, tmp_path: Path
    ) -> None:
        # V13 — tasks=[001, 002], batches=[{1:[001]}]. 001 done, 002
        # pending but not in any batch. MUST surface scheduler_stuck=True
        # rather than silently returning []/False.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
            ],
        })
        cp = _run_batch_next(sched, done="001")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["scheduler_stuck"] is True

    def test_batch_next_skips_active_batch_task_with_failed_dep(
        self, tmp_path: Path
    ) -> None:
        # V14 — batch 1 = [001], batch 2 = [002 dep 001]. 001 failed.
        # batch-next advances past batch 1 (001 resolved as failed),
        # active_batch = batch 2. 002's dep is failed so _ready(002) is
        # False → not picked. scheduler_stuck=True.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"]},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        })
        cp = _run_batch_next(sched, failed="001")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["task_ids"] == []
        assert body["batch_index"] == 2
        assert body["scheduler_stuck"] is True


# ---------------------------------------------------------------------------
# parse-implementer-report
# ---------------------------------------------------------------------------


SAMPLE_REPORT = """# TASK-001 — report

**Outcome:** success

**Files changed:**
- src/foo.py

**Diff summary:**
Rename `old_fn` to `new_fn` in one module.

**Test command:** pytest tests/foo/
**Test outcome:** passed

**Concerns:** none
"""


class TestParseImplementerReport:
    def test_success(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input=SAMPLE_REPORT,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["outcome"] == "success"
        assert body["files_changed"] == ["src/foo.py"]
        assert body["test_outcome"] == "passed"

    def test_malformed_no_outcome(self) -> None:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input="# report\n\nNothing here.",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0

    def test_outcome_failed_is_parsed(self) -> None:
        body = SAMPLE_REPORT.replace("**Outcome:** success", "**Outcome:** failed")
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input=body,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        assert _parse_json(cp)["outcome"] == "failed"

    def test_concerns_for_reviewer_label(self) -> None:
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Concerns for reviewer:**\n- c1\n- c2\n"
            "**Plan adaptations:**\n- deviation1\n"
            "**Reversion guidance:** revert\n"
        )
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input=report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["concerns"] == ["c1", "c2"]
        assert body.get("warnings") == []

    def test_plan_adaptations_extracted(self) -> None:
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Concerns for reviewer:**\n- c1\n"
            "**Plan adaptations:**\n- deviation1\n- deviation2\n"
            "**Reversion guidance:** revert\n"
        )
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input=report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["plan_adaptations"] == ["deviation1", "deviation2"]

    def test_concerns_legacy_alias_emits_warning(self) -> None:
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Concerns:**\n- legacy_c1\n"
            "**Reversion guidance:** revert\n"
        )
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input=report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["concerns"] == ["legacy_c1"]
        warnings = body.get("warnings") or []
        assert any("Concerns" in w and "legacy" in w.lower() for w in warnings), warnings


# ---------------------------------------------------------------------------
# commit-task + fail-task — smoke tests against a fresh git repo
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "sample.md"
    plan.write_text(SAMPLE_PLAN_BODY, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestCommitTask:
    def test_commits_narrowly(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        (tmp_git_repo / "src" / "other.py").write_text("y = 1\n", encoding="utf-8")

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
        body = _parse_json(cp)
        assert body["commit_sha"]
        assert body["status_updated"] is True

        log = subprocess.run(
            ["git", "log", "-1", "--name-only", "--pretty=format:%s"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "feat(TASK-001)" in log
        committed_files = [ln for ln in log.splitlines()[1:] if ln]
        assert "src/foo.py" in committed_files or any("foo.py" in f for f in committed_files)
        assert plan.read_text(encoding="utf-8").count("### TASK-001: First task\n\n- **Status:** done") == 1

        other = subprocess.run(
            ["git", "status", "--short", "src/other.py"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "src/other.py" in other


class TestFailTask:
    def test_restores_and_marks_failed(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("BROKEN\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "fail-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--stage", "implement",
            "--reason", "malformed_report",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["restore_ok"] is True
        assert body["status_updated"] is True

        assert (tmp_git_repo / "src" / "foo.py").read_text(encoding="utf-8") == "x = 1\n"
        text = plan.read_text(encoding="utf-8")
        assert "### TASK-001: First task\n\n- **Status:** failed" in text


class TestPreflightDirtyCategorization:
    """Preflight splits `git status` entries into plan_doc / infra_ignored /
    source_blocking. The infra_ignored set must agree with the wrapper's
    `_is_reconcile_protected()` rule (plan_ops.py:924), so orchestrator
    infrastructure artifacts like a stray `.codex` file do not block runs.
    """

    def _preflight(self, repo: Path, plan: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(PY), str(SCRIPT), "preflight", "--plan-file", str(plan), "--json"],
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

    def test_codex_stray_file_is_infra_ignored(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / ".codex").write_text("", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = self._preflight(tmp_git_repo, plan)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert ".codex" in body["dirty_files"]["infra_ignored"]
        assert body["dirty_files"]["source_blocking"] == []
        assert body["pass"] is True

    def test_plan_dir_file_is_infra_ignored(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "docs" / "plans" / "_run_lock.json").write_text("{}", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = self._preflight(tmp_git_repo, plan)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert any("_run_lock.json" in p for p in body["dirty_files"]["infra_ignored"])
        assert body["pass"] is True

    def test_arbitrary_untracked_file_is_source_blocking(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "scratch.py").write_text("print('hi')\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = self._preflight(tmp_git_repo, plan)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "scratch.py" in body["dirty_files"]["source_blocking"]
        assert body["pass"] is False

    def test_untracked_tests_dir_is_source_blocking(self, tmp_git_repo: Path) -> None:
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "wip_test.py").write_text("def test_wip(): pass\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = self._preflight(tmp_git_repo, plan)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "tests/" in body["dirty_files"]["source_blocking"]

    def test_claude_dir_is_infra_ignored(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / ".claude").mkdir()
        (tmp_git_repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = self._preflight(tmp_git_repo, plan)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert ".claude/" in body["dirty_files"]["infra_ignored"]
        assert body["pass"] is True


# ---------------------------------------------------------------------------
# TASK-002: Runtime contract validation at every executor seam
# ---------------------------------------------------------------------------


def _parse_schedule_payload(payload: dict | str, *, strict: bool = False) -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    cmd = [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"]
    if strict:
        cmd.append("--strict")
    return subprocess.run(
        cmd,
        input=body,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


class TestParseScheduleContractValidation:
    def test_rejects_duplicate_id(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": []},
                {"id": "001", "agent": "claude", "files": [], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": []}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "duplicate-task-id" in codes
        dup = next(e for e in body["errors"] if e["code"] == "duplicate-task-id")
        assert dup["path"] == "$.tasks[1].id"

    def test_rejects_duplicate_batch_index(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": []},
                {"id": "002", "agent": "codex", "files": [], "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": []},
                {"index": 1, "task_ids": ["002"], "file_locks": []},
            ],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "duplicate-batch-index" in codes

    def test_rejects_batch_task_ref_unknown(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001", "999"], "file_locks": []}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        ref_err = next(
            (e for e in body["errors"] if e["code"] == "unknown-batch-task-ref"), None
        )
        assert ref_err is not None
        assert ref_err["path"] == "$.batches[0].task_ids[1]"

    def test_rejects_unknown_top_level_field(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "evil_key": 1,
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        bad = next(
            (e for e in body["errors"] if e["code"] == "unknown-top-level-field"), None
        )
        assert bad is not None
        assert bad["path"] == "$.evil_key"

    def test_top_level_halt_not_strict_gated(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "evil_key": 1,
        }
        cp_default = _parse_schedule_payload(payload)
        cp_strict = _parse_schedule_payload(payload, strict=True)
        for cp in (cp_default, cp_strict):
            assert cp.returncode == 1
            body = _parse_json(cp)
            codes = [e["code"] for e in body["errors"]]
            assert "unknown-top-level-field" in codes

    def test_rejects_non_canonical_id(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "1", "agent": "codex", "files": [], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["1"], "file_locks": []}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "non-canonical-id" in codes

    def test_rejects_batch_file_overlap(self) -> None:
        """Fix D: two tasks in the same batch whose files overlap must fail.

        The disjointness invariant keeps the observe-only dispatcher safe:
        under parallel execution, two sibling tasks with overlapping
        allowed_files could race into each other's scope. The schedule
        writer rejects this at persistence time."""
        payload = {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "codex",
                    "files": ["src/shared.py"],
                    "dependencies": [],
                },
                {
                    "id": "002",
                    "agent": "codex",
                    "files": ["src/shared.py", "src/b.py"],
                    "dependencies": [],
                },
            ],
            "batches": [
                {
                    "index": 1,
                    "task_ids": ["001", "002"],
                    "file_locks": ["src/shared.py", "src/b.py"],
                },
            ],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "batch-file-overlap" in codes
        overlap_err = next(
            e for e in body["errors"] if e["code"] == "batch-file-overlap"
        )
        assert "src/shared.py" in overlap_err["message"]

    def test_accepts_disjoint_batch_files(self) -> None:
        """Fix D sanity check: disjoint allowed_files across a batch are OK."""
        payload = {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "codex",
                    "files": ["src/a.py"],
                    "dependencies": [],
                },
                {
                    "id": "002",
                    "agent": "codex",
                    "files": ["src/b.py"],
                    "dependencies": [],
                },
            ],
            "batches": [
                {
                    "index": 1,
                    "task_ids": ["001", "002"],
                    "file_locks": ["src/a.py", "src/b.py"],
                },
            ],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "batch-file-overlap" not in codes

    def test_warns_unknown_nested_field(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "codex",
                    "files": [],
                    "dependencies": [],
                    "confidence": 0.9,
                },
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": []}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["errors"] == []
        warnings = body.get("warnings") or []
        assert any("confidence" in w for w in warnings), warnings

    def test_strict_flag_promotes_nested_unknown(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "codex",
                    "files": [],
                    "dependencies": [],
                    "confidence": 0.9,
                },
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": []}],
        }
        cp = _parse_schedule_payload(payload, strict=True)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "unknown-nested-field" in codes

    def test_rejects_top_level_warnings_key(self) -> None:
        # Consumer-output fields MUST NOT be accepted as producer input.
        payload = {
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "warnings": [],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        bad = next(
            (e for e in body["errors"]
             if e["code"] == "unknown-top-level-field" and e["path"] == "$.warnings"),
            None,
        )
        assert bad is not None


# ---------------------------------------------------------------------------
# write-schedule
# ---------------------------------------------------------------------------


def _run_write_schedule(payload: dict | str, dest: Path, *, strict: bool = False) -> subprocess.CompletedProcess:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    cmd = [
        str(PY), str(SCRIPT), "write-schedule",
        "--schedule-file", str(dest),
        "--stdin", "--json",
    ]
    if strict:
        cmd.append("--strict")
    return subprocess.run(
        cmd,
        input=body,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


class TestWriteSchedule:
    def test_roundtrip_valid_json(self, tmp_path: Path) -> None:
        dest = tmp_path / "schedule.json"
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a.txt"], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["a.txt"]}],
        }
        cp = _run_write_schedule(payload, dest)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["written"] == str(dest)
        assert body["bytes"] > 0
        assert body.get("warnings") == []
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert written["tasks"][0]["id"] == "001"

    def test_does_not_write_on_unknown_batch_task_ref(self, tmp_path: Path) -> None:
        dest = tmp_path / "schedule.json"
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001", "999"], "file_locks": []}],
        }
        cp = _run_write_schedule(payload, dest)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "unknown-batch-task-ref" in codes
        assert not dest.exists()
        assert not (dest.parent / (dest.name + ".tmp")).exists()

    def test_parse_and_write_schedule_tolerate_orphan_dependencies_field(
        self, tmp_path: Path,
    ) -> None:
        dest = tmp_path / "schedule.json"
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["999"]},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["a"]}],
        }
        cp = _run_write_schedule(payload, dest)
        assert cp.returncode == 0, cp.stderr
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert written["tasks"][0]["dependencies"] == ["999"]

        parse_cp = _parse_schedule_payload(payload)
        assert parse_cp.returncode == 0, parse_cp.stderr
        body = _parse_json(parse_cp)
        codes = [e.get("code", "") for e in body.get("errors") or []]
        assert not any(
            "orphan" in code or "unknown-dependency" in code or "dependency" in code
            for code in codes
        )

    def test_atomic_replaces_existing_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "schedule.json"
        dest.write_text("{\"existing\": true}\n", encoding="utf-8")
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": []}],
        }
        cp = _run_write_schedule(payload, dest)
        assert cp.returncode == 0, cp.stderr
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert written["outcome"] == "valid"
        assert "existing" not in written
        assert not (dest.parent / (dest.name + ".tmp")).exists()

    def test_malformed_json_halts(self, tmp_path: Path) -> None:
        dest = tmp_path / "schedule.json"
        cp = _run_write_schedule("not json at all", dest)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "json-decode" in codes
        assert not dest.exists()


# ---------------------------------------------------------------------------
# parse-implementer-report: diagnostics[] for missing mandatory section headers
# ---------------------------------------------------------------------------


class TestParseImplementerReportDiagnostics:
    def _run(self, report: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
            input=report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def test_missing_plan_adaptations_diagnostic(self) -> None:
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Concerns for reviewer:**\n- none\n"
            "**Reversion guidance:** revert\n"
        )
        cp = self._run(report)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        codes = [d["code"] for d in body.get("diagnostics") or []]
        assert "missing-plan-adaptations" in codes
        assert body.get("warnings") == []

    def test_missing_concerns_for_reviewer_diagnostic(self) -> None:
        # No canonical `**Concerns for reviewer:**` header; legacy `**Concerns:**`
        # as a single-line field (no bullet list) is also absent in bullet form.
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Plan adaptations:**\n- none\n"
            "**Reversion guidance:** revert\n"
        )
        cp = self._run(report)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        codes = [d["code"] for d in body.get("diagnostics") or []]
        assert "missing-concerns-for-reviewer" in codes

    @pytest.mark.parametrize("outcome", ["failed", "blocked"])
    def test_missing_concerns_not_diagnostic_for_failed_or_blocked(self, outcome: str) -> None:
        report = (
            f"**Outcome:** {outcome}\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Plan adaptations:**\n- none\n"
            "**Reversion guidance:** revert\n"
        )
        cp = self._run(report)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        codes = [d["code"] for d in body.get("diagnostics") or []]
        assert "missing-concerns-for-reviewer" not in codes

    def test_clean_report_has_empty_diagnostics(self) -> None:
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Concerns for reviewer:**\n- none\n"
            "**Plan adaptations:**\n- none\n"
            "**Reversion guidance:** revert\n"
        )
        cp = self._run(report)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("diagnostics") == []
        assert body.get("warnings") == []


# ---------------------------------------------------------------------------
# batch-next defensive DAG validation (reads schedule from disk)
# ---------------------------------------------------------------------------


class TestBatchNextDagDefense:
    def test_rejects_duplicate_task_id_in_schedule_file(self, tmp_path: Path) -> None:
        sched = tmp_path / "schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": []},
                {"id": "001", "agent": "claude", "files": [], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": []}],
        }), encoding="utf-8")
        cp = _run(
            "batch-next",
            "--schedule-file", str(sched),
            "--json",
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "duplicate-task-id" in codes

    def test_rejects_non_dag_schedule_contract_error(self, tmp_path: Path) -> None:
        sched = tmp_path / "schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": []}],
        }), encoding="utf-8")
        cp = _run(
            "batch-next",
            "--schedule-file", str(sched),
            "--json",
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "missing-field" in codes
        assert any(e["path"] == "$.tasks[0].files" for e in body["errors"])


# ---------------------------------------------------------------------------
# finalize-execution-log row validation
# ---------------------------------------------------------------------------


class TestFinalizeExecutionLog:
    def test_appends_markdown_on_valid_rows(self, isolated_plan: Path) -> None:
        rows = [{
            "task": "TASK-001",
            "agent": "codex",
            "reviewer": "claude",
            "verdict": "ship",
            "commit": "abc1234",
            "notes": "done",
        }]
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--starting-sha", "1111111",
            "--ending-sha", "2222222",
            "--rows-json", json.dumps(rows),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        text = isolated_plan.read_text(encoding="utf-8")
        assert "## Execution log — R1" in text
        assert "| TASK-001 | codex | claude | ship | abc1234 | done |" in text

    def test_rejects_invalid_row_payload(self, isolated_plan: Path) -> None:
        before = isolated_plan.read_text(encoding="utf-8")
        rows = [{
            "task": "TASK-001",
            "agent": "codex",
            "reviewer": "claude",
            "verdict": "ship",
            "commit": 123,
            "notes": "done",
        }]
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--starting-sha", "1111111",
            "--ending-sha", "2222222",
            "--rows-json", json.dumps(rows),
            "--json",
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-execution-log-field" in codes
        assert isolated_plan.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# reviewer seam validation at actual ingestion points
# ---------------------------------------------------------------------------


class TestReviewerSeamValidation:
    def test_commit_task_rejects_invalid_minor_findings_shape(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            "--reviewer", "codex",
            "--reviewer-verdict", "minor-findings",
            "--reviewer-minor-findings", '[{"severity":"minor","file":"src/foo.py","line":"7","issue":"x","suggested_fix":"y"}]',
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-reviewer-finding-field" in codes

    def test_commit_task_rejects_needs_rework_verdict(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x",
            "--reviewer", "codex",
            "--reviewer-verdict", "needs-rework",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "uncommittable-reviewer-verdict" in codes

    def test_fail_task_review_rejects_invalid_reviewer_payload(self, tmp_git_repo: Path) -> None:
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "fail-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--stage", "review",
            "--reason", "needs_rework",
            "--reviewer-findings", '{"task_id":"001","verdict":"needs-rework","findings":[],"scope_ok":"yes","acceptance_met":false,"summary":"nope"}',
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-reviewer-field" in codes


# ---------------------------------------------------------------------------
# Real-producer integration tests (slow, gated on claude CLI availability)
# ---------------------------------------------------------------------------


# Embedded spec-compliant plan (copied from
# tests/scripts/test_plan_codex_dispatch_integration.py:27-71 to avoid
# cross-test-file coupling). Uses the canonical `pending` status.
MINIMAL_SPEC_COMPLIANT_PLAN = """# Plan: Scratch integration test

**Created:** 2026-04-13
**Status:** ready
**Base branch:** main

## Goal
Verify that the plan-analyst can classify a trivial single-task plan.

## Context
This is an isolated test repository with no real code. The only task
is to create a single file with fixed content. Follow the instructions
verbatim.

## Verification
Check that SCRATCH.txt exists and contains the expected content.

---

## Tasks

### TASK-001: Create SCRATCH.txt

- **Status:** pending
- **Priority:** low
- **Files:**
  - SCRATCH.txt (create)
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - SCRATCH.txt exists in the repository root
  - SCRATCH.txt contents equal the string "hello" (no trailing newline required)

**Description:**
Create a new file named `SCRATCH.txt` in the repository root containing
the text "hello". Do not create any other files. Do not modify any
other files.

**Implementation notes:**
Use a simple file write. The file does not need a trailing newline.

**Reversion guidance:**
Delete SCRATCH.txt from the repository root.
"""


def _claude_cli_available() -> bool:
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _extract_json_block(text: str) -> dict | None:
    """Best-effort extraction of a fenced ```json or bare {...} block."""
    m = re.search(r"```json\s*\n(.+?)\n```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# reconcile-batch (Fix E: orchestrator batch reconciliation)
# ---------------------------------------------------------------------------


def _reconcile_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _run_reconcile(repo: Path, envelopes: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PY), str(SCRIPT), "reconcile-batch",
         "--repo-root", str(repo), "--json"],
        input=json.dumps(envelopes),
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _scope_envelope(task_id: str, *, tracked=(), untracked=(), observed=True) -> dict:
    """Build an envelope mirroring how make_envelope flattens `extra` keys."""
    return {
        "task_id": task_id,
        "subcommand": "implement",
        "outcome": "scope_violation" if observed else "success",
        "out_of_scope_tracked": list(tracked),
        "out_of_scope_untracked": list(untracked),
        "out_of_scope_observed": observed,
    }


class TestReconcileBatch:
    def test_no_op_when_no_out_of_scope_writes(self, tmp_path: Path) -> None:
        """An envelope with no observed out-of-scope writes must result in no-op."""
        repo = _reconcile_git_repo(tmp_path)
        envelope = _scope_envelope("001", observed=False)
        cp = _run_reconcile(repo, [envelope])
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        assert body["reconciliation_failed"] is False
        results = body["results"]
        assert len(results) == 1
        assert results[0]["outcome"] == "no_op"

    def test_reconciles_untracked_out_of_scope_by_unlinking(
        self, tmp_path: Path,
    ) -> None:
        """Untracked out-of-scope files are deleted by the orchestrator."""
        repo = _reconcile_git_repo(tmp_path)
        stray = repo / "stray.txt"
        stray.write_text("observed but out of scope\n")

        envelope = _scope_envelope("001", untracked=["stray.txt"])
        cp = _run_reconcile(repo, [envelope])
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["outcome"] == "scope_violation_reconciled"
        assert "stray.txt" in results[0]["reconciled_untracked"]
        assert not stray.exists()

    def test_reconciles_tracked_out_of_scope_by_restoring(
        self, tmp_path: Path,
    ) -> None:
        """Tracked out-of-scope modifications are reverted via git restore."""
        repo = _reconcile_git_repo(tmp_path)
        other = repo / "other.py"
        other.write_text("v1\n")
        subprocess.run(["git", "add", "other.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add other"],
                       cwd=repo, check=True)
        other.write_text("codex-touched\n")

        envelope = _scope_envelope("002", tracked=["other.py"])
        cp = _run_reconcile(repo, [envelope])
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["outcome"] == "scope_violation_reconciled"
        assert "other.py" in results[0]["reconciled_tracked"]
        assert other.read_text() == "v1\n"

    def test_reconciles_multiple_envelopes_in_batch(self, tmp_path: Path) -> None:
        """Reconciliation must iterate through all envelopes in the batch."""
        repo = _reconcile_git_repo(tmp_path)
        (repo / "stray_a.txt").write_text("a\n")
        (repo / "stray_b.txt").write_text("b\n")

        envelopes = [
            _scope_envelope("001", untracked=["stray_a.txt"]),
            _scope_envelope("002", untracked=["stray_b.txt"]),
        ]
        cp = _run_reconcile(repo, envelopes)
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        results = body["results"]
        assert len(results) == 2
        assert all(r["outcome"] == "scope_violation_reconciled" for r in results)
        assert not (repo / "stray_a.txt").exists()
        assert not (repo / "stray_b.txt").exists()

    def test_skips_protected_infrastructure_paths(self, tmp_path: Path) -> None:
        """Executor-infrastructure paths must never be restored/deleted by
        reconciliation; they are classified as skipped_protected."""
        repo = _reconcile_git_repo(tmp_path)
        (repo / "docs" / "plans").mkdir(parents=True)
        log = repo / "docs" / "plans" / "_run_log.jsonl"
        log.write_text('{"event":"baseline"}\n')

        envelope = _scope_envelope(
            "001", untracked=["docs/plans/_run_log.jsonl"],
        )
        cp = _run_reconcile(repo, [envelope])
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        results = body["results"]
        assert "docs/plans/_run_log.jsonl" in results[0]["skipped_protected"]
        assert log.exists()
        assert log.read_text() == '{"event":"baseline"}\n'

    def test_reports_failure_exit_code_on_unlink_error(
        self, tmp_path: Path,
    ) -> None:
        """Reconciliation of a non-existent untracked path is a no-op for
        unlink but must not emit a residual — overall exit code is 0 because
        the `unlink` raises FileNotFoundError which IS recorded as an error,
        producing reconciliation_failed and exit 1."""
        repo = _reconcile_git_repo(tmp_path)
        # File does not exist — unlink raises FileNotFoundError
        envelope = _scope_envelope("001", untracked=["ghost.txt"])
        cp = _run_reconcile(repo, [envelope])
        assert cp.returncode == 1, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        assert body["reconciliation_failed"] is True
        results = body["results"]
        assert results[0]["outcome"] == "reconciliation_failed"
        assert "ghost.txt" in results[0]["error"]


@pytest.mark.slow
def test_analyst_to_parse_schedule_roundtrip(tmp_path: Path) -> None:
    """Dispatch the real plan-analyst agent and round-trip into parse-schedule."""
    if not _claude_cli_available():
        pytest.skip("claude CLI not available")

    try:
        result = subprocess.run(
            [
                "claude",
                "-p", MINIMAL_SPEC_COMPLIANT_PLAN,
                "--agents", "plan-analyst",
                "--output-format", "json",
                "--permission-mode", "plan",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"claude CLI dispatch failed: {e}")

    if result.returncode != 0:
        pytest.skip(
            f"claude CLI subagent dispatch not supported "
            f"(rc={result.returncode}, stderr={result.stderr[:400]!r})"
        )

    analyst_json = _extract_json_block(result.stdout)
    if analyst_json is None:
        pytest.skip(
            f"no JSON block recovered from analyst output "
            f"(stdout[:400]={result.stdout[:400]!r})"
        )

    cp = subprocess.run(
        [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
        input=json.dumps(analyst_json),
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stdout + cp.stderr
    body = _parse_json(cp)
    assert body["errors"] == []


@pytest.mark.slow
def test_implementer_to_parse_implementer_report_roundtrip(tmp_path: Path) -> None:
    """Dispatch the real plan-implementer and round-trip into parse-implementer-report."""
    if not _claude_cli_available():
        pytest.skip("claude CLI not available")

    task_prompt = (
        "Implement TASK-001 of the following plan. Do not write any files; "
        "report as if you had written SCRATCH.txt = 'hello'.\n\n"
        + MINIMAL_SPEC_COMPLIANT_PLAN
    )
    try:
        result = subprocess.run(
            [
                "claude",
                "-p", task_prompt,
                "--agents", "plan-implementer",
                "--output-format", "json",
                "--permission-mode", "plan",
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"claude CLI dispatch failed: {e}")

    if result.returncode != 0:
        pytest.skip(
            f"claude CLI subagent dispatch not supported "
            f"(rc={result.returncode}, stderr={result.stderr[:400]!r})"
        )

    cp = subprocess.run(
        [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
        input=result.stdout,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    if cp.returncode != 0:
        pytest.skip(
            f"implementer markdown did not parse (rc={cp.returncode}, "
            f"stdout={cp.stdout[:400]!r})"
        )
    body = _parse_json(cp)
    assert body["outcome"] in {"success", "partial"}
    assert body.get("warnings") == []
    assert body.get("diagnostics") == []


# ---------------------------------------------------------------------------
# TASK-004A: Suffixed task-id contract (^\d{3}[A-Z]?$)
# ---------------------------------------------------------------------------


class TestSuffixedTaskIdRegexes:
    def test_canonical_id_re_accepts_suffix(self) -> None:
        assert plan_ops.CANONICAL_ID_RE.match("004A") is not None
        assert plan_ops.CANONICAL_ID_RE.match("004") is not None
        assert plan_ops.CANONICAL_ID_RE.match("000Z") is not None

    def test_canonical_id_re_rejects_non_canonical(self) -> None:
        assert plan_ops.CANONICAL_ID_RE.match("004a") is None
        assert plan_ops.CANONICAL_ID_RE.match("004AB") is None
        assert plan_ops.CANONICAL_ID_RE.match("004A1") is None
        assert plan_ops.CANONICAL_ID_RE.match("4A") is None

    def test_task_header_re_captures_suffix(self) -> None:
        m = plan_ops.TASK_HEADER_RE.search("### TASK-004A: something\n")
        assert m is not None
        assert m.group(1) == "004A"

    def test_task_header_re_rejects_lowercase(self) -> None:
        assert plan_ops.TASK_HEADER_RE.search("### TASK-004a: x\n") is None

    def test_task_header_re_rejects_multiletter(self) -> None:
        assert plan_ops.TASK_HEADER_RE.search("### TASK-004AB: x\n") is None


class TestSplitTaskBlocksMixed:
    def test_split_task_blocks_mixed_stem_and_suffixed(self) -> None:
        preamble, blocks = plan_ops._split_task_blocks(SAMPLE_PLAN_BODY_MIXED)
        ids = [tid for tid, _ in blocks]
        assert ids == ["001", "004", "004A"]
        body_004 = next(body for tid, body in blocks if tid == "004")
        body_004A = next(body for tid, body in blocks if tid == "004A")
        assert "Stem group task" in body_004
        assert "Suffixed leaf task" in body_004A
        # Independence: the 004 block must not spill into 004A or vice versa.
        assert "Suffixed leaf task" not in body_004
        assert "Stem group task" not in body_004A


class TestMutateTaskStatusSuffixed:
    def test_mutate_task_status_suffixed_id(self) -> None:
        updated, prior = plan_ops.mutate_task_status(
            SAMPLE_PLAN_BODY_MIXED, "004A", "done"
        )
        assert prior == "open"
        assert "### TASK-004A: Suffixed leaf task\n\n- **Status:** done" in updated
        # The 004 stem-sibling must be untouched — no aliasing.
        assert "### TASK-004: Stem group task\n\n- **Status:** open" in updated

    def test_mutate_task_status_only_suffixed_changes(self) -> None:
        updated, _ = plan_ops.mutate_task_status(
            SAMPLE_PLAN_BODY_MIXED, "004", "done"
        )
        assert "### TASK-004: Stem group task\n\n- **Status:** done" in updated
        assert "### TASK-004A: Suffixed leaf task\n\n- **Status:** open" in updated

    def test_mutate_task_status_missing_suffixed_raises(self) -> None:
        with pytest.raises(ValueError):
            plan_ops.mutate_task_status(SAMPLE_PLAN_BODY_MIXED, "004Z", "done")


class TestValidateScheduleSuffixed:
    def test_accepts_canonical_suffixed_id(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "004A", "agent": "claude", "files": ["x"], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["004A"], "file_locks": ["x"]}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr

    def test_validate_schedule_accepts_mixed_ids(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "004", "agent": "claude", "files": ["a.py"], "dependencies": []},
                {
                    "id": "004A",
                    "agent": "claude",
                    "files": ["b.py"],
                    "dependencies": [],
                },
            ],
            "batches": [
                {"index": 1, "task_ids": ["004", "004A"], "file_locks": ["a.py", "b.py"]},
            ],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("errors", []) == []

    def test_validate_schedule_rejects_lowercase_suffix(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "004a", "agent": "claude", "files": ["a.py"], "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["004a"], "file_locks": ["a.py"]}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "non-canonical-id" in codes

    def test_batch_task_ids_accept_suffix(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "004", "agent": "claude", "files": ["a.py"], "dependencies": []},
                {
                    "id": "004A",
                    "agent": "claude",
                    "files": ["b.py"],
                    "dependencies": [],
                },
            ],
            "batches": [
                {
                    "index": 1,
                    "task_ids": ["004", "004A"],
                    "file_locks": ["a.py", "b.py"],
                },
            ],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr

@pytest.fixture()
def tmp_git_repo_mixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "sample_mixed.md"
    plan.write_text(SAMPLE_PLAN_BODY_MIXED, encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.py").write_text("x = 1\n", encoding="utf-8")
    (src / "stem.py").write_text("x = 1\n", encoding="utf-8")
    (src / "leaf_a.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestCommitTaskSuffixed:
    def test_commit_task_suffixed_id(self, tmp_git_repo_mixed: Path) -> None:
        (tmp_git_repo_mixed / "src" / "leaf_a.py").write_text("x = 2\n", encoding="utf-8")
        (tmp_git_repo_mixed / "src" / "stem.py").write_text("UNTOUCHED\n", encoding="utf-8")

        plan = tmp_git_repo_mixed / "docs" / "plans" / "sample_mixed.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "004A",
            "--run-id", "R_004A",
            "--files", "src/leaf_a.py",
            "--title", "Suffixed leaf task",
            "--diff-summary", "bump leaf",
            "--reviewer", "none",
            "--reviewer-verdict", "",
            "--json",
            cwd=tmp_git_repo_mixed,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["commit_sha"]
        assert body["status_updated"] is True

        log = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%s"],
            cwd=tmp_git_repo_mixed, capture_output=True, text=True, check=True,
        ).stdout
        assert "feat(TASK-004A)" in log

        text = plan.read_text(encoding="utf-8")
        # 004A flipped to done.
        assert "### TASK-004A: Suffixed leaf task\n\n- **Status:** done" in text
        # 004 stem-sibling remains open — no aliasing.
        assert "### TASK-004: Stem group task\n\n- **Status:** open" in text

        # stem.py was not committed by the narrow --only.
        stem_status = subprocess.run(
            ["git", "status", "--short", "src/stem.py"],
            cwd=tmp_git_repo_mixed, capture_output=True, text=True, check=True,
        ).stdout
        assert "src/stem.py" in stem_status


class TestNoAliasBetweenStemAndSuffixed:
    def test_mutate_preserves_independence(self) -> None:
        """Flipping 004 Status must not affect 004A, and vice versa."""
        after_004, _ = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY_MIXED, "004", "done")
        assert "### TASK-004A: Suffixed leaf task\n\n- **Status:** open" in after_004

        after_004a, _ = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY_MIXED, "004A", "failed")
        assert "### TASK-004: Stem group task\n\n- **Status:** open" in after_004a

    def test_validator_treats_stem_and_suffix_as_independent_ids(self) -> None:
        """Schedule with both 004 and 004A must not raise duplicate-task-id."""
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "004", "agent": "claude", "files": ["a.py"], "dependencies": []},
                {
                    "id": "004A",
                    "agent": "claude",
                    "files": ["b.py"],
                    "dependencies": [],
                },
            ],
            "batches": [
                {
                    "index": 1,
                    "task_ids": ["004", "004A"],
                    "file_locks": ["a.py", "b.py"],
                },
            ],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "duplicate-task-id" not in codes


class TestSuffixedBackcompat:
    def test_backcompat_plain_ids_unchanged(self) -> None:
        """Parse/mutate round-trip on a 3-digit-only plan produces the
        same structural results after the regex extension."""
        preamble, blocks = plan_ops._split_task_blocks(SAMPLE_PLAN_BODY)
        assert [tid for tid, _ in blocks] == ["001", "002", "003"]

        updated, prior = plan_ops.mutate_task_status(SAMPLE_PLAN_BODY, "001", "done")
        assert prior == "open"
        assert "### TASK-001: First task\n\n- **Status:** done" in updated
        # All other tasks untouched.
        assert "### TASK-002: Second task with hyphen value\n\n- **Status:** in-progress" in updated
        assert "### TASK-003: Third task\n\n- **Status:** done" in updated

    def test_plain_schedule_still_validates(self) -> None:
        cp = _parse_schedule_payload(VALID_SCHEDULE)
        assert cp.returncode == 0, cp.stderr


class TestSuffixedPlanFilenameInvariance:
    def test_plan_filenames_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Task-id parsing/mutation is a function of plan BODY, not file name.
        Rename a suffixed-id plan to an arbitrary filename and confirm
        parse/mutate produce identical results."""
        plans = tmp_path / "docs" / "plans"
        plans.mkdir(parents=True)
        weird = plans / "arbitrary-name-no-task-id-reference.md"
        weird.write_text(SAMPLE_PLAN_BODY_MIXED, encoding="utf-8")

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(plan_ops, "PLAN_DIR", plans)
        monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans / "_run_log.jsonl")
        monkeypatch.setattr(plan_ops, "RUN_LOCK_PATH", plans / "_run_lock.json")

        preamble, blocks = plan_ops._split_task_blocks(weird.read_text(encoding="utf-8"))
        assert [tid for tid, _ in blocks] == ["001", "004", "004A"]

        updated, prior = plan_ops.mutate_task_status(
            weird.read_text(encoding="utf-8"), "004A", "done"
        )
        assert prior == "open"
        assert "### TASK-004A: Suffixed leaf task\n\n- **Status:** done" in updated


# ---------------------------------------------------------------------------
# check-plan-deps — cross-plan dependency resolution via 00_INDEX.json
# ---------------------------------------------------------------------------


def _sibling_plan(task_id: str, status: str, title: str = "Sibling task") -> str:
    return (
        f"# Plan: {task_id}\n\n"
        "**Created:** 2026-04-15\n"
        "**Status:** in-progress\n"
        "**Base branch:** main\n\n"
        "## Context\n\nPretend context.\n\n"
        "## Tasks\n\n"
        f"### TASK-{task_id}: {title}\n\n"
        f"- **Status:** {status}\n"
        "- **Priority:** medium\n"
        "- **Files:**\n  - src/x.py\n"
        "- **Dependencies:** none\n"
        "- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`\n"
        "- **Acceptance criteria:**\n  - Ships.\n"
        "- **Description:** Placeholder.\n"
        "- **Reversion guidance:** `git restore src/x.py`\n"
    )


def _target_plan(task_id: str, deps_raw: str, title: str = "Target task") -> str:
    return (
        f"# Plan: {task_id}\n\n"
        "**Created:** 2026-04-15\n"
        "**Status:** in-progress\n"
        "**Base branch:** main\n\n"
        "## Context\n\nPretend context.\n\n"
        "## Tasks\n\n"
        f"### TASK-{task_id}: {title}\n\n"
        "- **Status:** pending\n"
        "- **Priority:** medium\n"
        "- **Files:**\n  - src/y.py\n"
        f"- **Dependencies:** {deps_raw}\n"
        "- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`\n"
        "- **Acceptance criteria:**\n  - Ships.\n"
        "- **Description:** Placeholder.\n"
        "- **Reversion guidance:** `git restore src/y.py`\n"
    )


def _write_roster(
    plans_dir: Path,
    rows: list[dict],
) -> Path:
    """Write `00_INDEX.json` with chunk rows."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for row in rows:
        task_id = row["task_id"]
        chunks.append({
            "task_id": task_id,
            "v3_task": f"TASK-{task_id}",
            "file": row.get("file", f"TASK-{task_id}.md"),
            "priority": row.get("priority", "medium"),
            "issues_absorbed": row.get("issues_absorbed", []),
            "depends_on": row.get("depends_on", []),
            "status": row.get("status", "Done"),
            "superseded_by": row.get("superseded_by", []),
        })
    path = plans_dir / "00_INDEX.json"
    path.write_text(
        json.dumps({"schema_version": 1, "source": "test", "chunks": chunks}, indent=2),
        encoding="utf-8",
    )
    return path


class TestCheckPlanDeps_check_plan_deps:
    def _run_cpd(
        self, plan_file: Path, plans_dir: Path
    ) -> subprocess.CompletedProcess:
        return _run(
            "check-plan-deps",
            "--plan-file", str(plan_file),
            "--plans-dir", str(plans_dir),
            "--json",
        )

    def test_parse_index_roster_returns_public_shape(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        path = _write_roster(plans_dir, [
            {"task_id": "001", "file": "TASK-001_canonical_contracts.md", "depends_on": [], "status": "Done"},
            {"task_id": "004", "file": "TASK-004_scheduler_semantics.md", "depends_on": ["001"], "status": "Superseded", "superseded_by": ["004A"]},
            {"task_id": "004A", "file": "TASK-004A_filter_schedule.md", "depends_on": ["001"], "status": "Pending"},
        ])

        roster = plan_ops._parse_index_roster(path)

        assert roster["001"] == {
            "file": "TASK-001_canonical_contracts.md",
            "depends_on": [],
            "status": "Done",
        }
        assert "superseded_by" not in roster["004"]
        assert plan_ops._INDEX_SUPERSEDED_BY == {"004": ["004A"]}

    def test_004a_passes_direct_done_deps(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "001", "status": "Done"},
            {"task_id": "002", "depends_on": ["001"], "status": "Done"},
            {"task_id": "003", "depends_on": ["001"], "status": "Done"},
            {"task_id": "004A", "depends_on": ["001", "002", "003"], "status": "Pending"},
        ])
        target = plans_dir / "TASK-004A_target.md"
        target.write_text(_target_plan("004A", "ignored"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is True
        assert body["unresolved"] == []
        assert body["errors"] == []
        ids = sorted(d["task_id"] for d in body["deps"])
        assert ids == ["001", "002", "003"]
        for entry in body["deps"]:
            assert entry["status"] == "Done"

    def test_004b_blocks_on_direct_004a(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "004A", "status": "Pending"},
            {"task_id": "004B", "depends_on": ["004A"], "status": "Pending"},
        ])
        target = plans_dir / "TASK-004B_target.md"
        target.write_text(_target_plan("004B", "none"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is False
        assert body["errors"] == []
        assert [u["task_id"] for u in body["unresolved"]] == ["004A"]
        assert body["unresolved"][0]["reason"] == "dep-not-done"
        assert body["unresolved"][0]["status"] == "Pending"

    def test_004c_checks_only_direct_004b(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "004A", "status": "Pending"},
            {"task_id": "004B", "depends_on": ["004A"], "status": "Done"},
            {"task_id": "004C", "depends_on": ["004B"], "status": "Pending"},
        ])
        target = plans_dir / "TASK-004C_target.md"
        target.write_text(_target_plan("004C", "TASK-004A"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is True
        assert body["errors"] == []
        assert body["unresolved"] == []
        assert [d["task_id"] for d in body["deps"]] == ["004B"]

    def test_005_expands_superseded_004_and_reports_replacements(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "004", "status": "Superseded", "superseded_by": ["004A", "004B", "004C", "004D", "004E"]},
            {"task_id": "004A", "status": "Done"},
            {"task_id": "004B", "status": "Pending"},
            {"task_id": "004C", "status": "Done"},
            {"task_id": "004D", "status": "Pending"},
            {"task_id": "004E", "status": "Pending"},
            {"task_id": "005", "depends_on": ["004"], "status": "Pending"},
        ])
        target = plans_dir / "TASK-005_target.md"
        target.write_text(_target_plan("005", "none"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is False
        assert body["errors"] == []
        assert [d["task_id"] for d in body["deps"]] == ["004A", "004C"]
        assert [u["task_id"] for u in body["unresolved"]] == ["004B", "004D", "004E"]
        assert {u["parent_id"] for u in body["unresolved"]} == {"004"}
        assert {u["reason"] for u in body["unresolved"]} == {"dep-not-done"}

    def test_005_passes_when_all_004_replacements_done(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "004", "status": "Superseded", "superseded_by": ["004A", "004B", "004C", "004D", "004E"]},
            {"task_id": "004A", "status": "Done"},
            {"task_id": "004B", "status": "Done"},
            {"task_id": "004C", "status": "Done"},
            {"task_id": "004D", "status": "Done"},
            {"task_id": "004E", "status": "Done"},
            {"task_id": "005", "depends_on": ["004"], "status": "Pending"},
        ])
        target = plans_dir / "TASK-005_target.md"
        target.write_text(_target_plan("005", "none"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is True
        assert body["unresolved"] == []
        assert body["errors"] == []
        assert [d["task_id"] for d in body["deps"]] == ["004A", "004B", "004C", "004D", "004E"]

    def test_superseded_missing_replacement_reported(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "004", "status": "Superseded", "superseded_by": ["004A", "004B"]},
            {"task_id": "004A", "status": "Done"},
            {"task_id": "005", "depends_on": ["004"], "status": "Pending"},
        ])
        target = plans_dir / "TASK-005_target.md"
        target.write_text(_target_plan("005", "none"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is False
        assert body["unresolved"] == [{
            "task_id": "004B",
            "reason": "superseded-target-missing",
            "detail": "task 004B is not declared in 00_INDEX.json roster",
            "parent_id": "004",
        }]

    def test_malformed_index_rejected(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({"source": "test"}), encoding="utf-8",
        )
        target = plans_dir / "TASK-004A_target.md"
        target.write_text(_target_plan("004A", "TASK-001"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "index-not-found" in codes

    @pytest.mark.parametrize("status", ["Superseded", "done", "pending", "failed", "Deferred"])
    def test_invalid_status_values_rejected(self, tmp_path: Path, status: str) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [{"task_id": "001", "status": status}])

        with pytest.raises(ValueError):
            plan_ops._parse_index_roster(plans_dir / "00_INDEX.json")

    def test_duplicate_task_ids_rejected(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "001", "status": "Done"},
            {"task_id": "001", "status": "Pending"},
        ])

        with pytest.raises(ValueError, match="duplicate"):
            plan_ops._parse_index_roster(plans_dir / "00_INDEX.json")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("depends_on", "001"),
            ("depends_on", ["TASK-001"]),
            ("depends_on", ["1"]),
            ("superseded_by", "004A"),
            ("superseded_by", ["TASK-004A"]),
            ("superseded_by", ["4A"]),
        ],
    )
    def test_malformed_dep_lists_rejected(self, tmp_path: Path, field: str, value: object) -> None:
        plans_dir = tmp_path / "plans"
        row = {"task_id": "004", "status": "Superseded", "superseded_by": ["004A"]}
        row[field] = value
        _write_roster(plans_dir, [row, {"task_id": "004A", "status": "Pending"}])

        with pytest.raises(ValueError):
            plan_ops._parse_index_roster(plans_dir / "00_INDEX.json")

    def test_supersession_cycle_rejected(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [
            {"task_id": "004", "status": "Superseded", "superseded_by": ["004A"]},
            {"task_id": "004A", "status": "Superseded", "superseded_by": ["004"]},
        ])

        with pytest.raises(ValueError, match="cycle"):
            plan_ops._parse_index_roster(plans_dir / "00_INDEX.json")

    def test_missing_plan_file_arg(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        _write_roster(plans_dir, [{"task_id": "001", "status": "Done"}])
        missing = plans_dir / "does_not_exist.md"

        cp = self._run_cpd(missing, plans_dir)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "file-not-found" in codes

    def test_missing_index_file(self, tmp_path: Path) -> None:
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir(parents=True)
        target = plans_dir / "TASK-004A_target.md"
        target.write_text(_target_plan("004A", "TASK-001"), encoding="utf-8")

        cp = self._run_cpd(target, plans_dir)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "index-not-found" in codes


# ---------------------------------------------------------------------------
# filter-schedule (TASK-004A) — subcommand for `--task-ids` orchestrator path
# ---------------------------------------------------------------------------


def _run_filter_schedule(
    sched_path: Path, task_ids: str
) -> subprocess.CompletedProcess:
    return _run(
        "filter-schedule",
        "--schedule-file", str(sched_path),
        "--task-ids", task_ids,
        "--json",
    )


def _full_schedule_fixture() -> dict:
    return {
        "outcome": "valid",
        "tasks": [
            {"id": "001", "agent": "codex", "files": ["a"], "dependencies": []},
            {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"]},
            {"id": "003", "agent": "codex", "files": ["c"], "dependencies": []},
        ],
        "batches": [
            {"index": 1, "task_ids": ["001", "003"], "file_locks": ["a", "c"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
        ],
    }


class TestFilterSchedule:
    def test_filter_schedule_happy_path(self, tmp_path: Path) -> None:
        # V1 — request 002, transitive closure pulls in 001 (002's dep).
        sched = tmp_path / "full.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["outcome"] == "valid"
        # Source order preserved: 001 appears first in fixture, 002 second.
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]
        # 002's dep is kept.
        ids_to_deps = {t["id"]: t.get("dependencies", []) for t in body["tasks"]}
        assert ids_to_deps["002"] == ["001"]
        # batch 1 was [001, 003] → retained as [001]; batch 2 = [002].
        assert body["batches"] == [
            {"index": 1, "task_ids": ["001"], "file_locks": ["a", "c"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
        ]
        assert body["gaps"] == []
        assert body["risks"] == []

    def test_filter_schedule_transitive_prereqs(self, tmp_path: Path) -> None:
        # V1 explicit: requesting 002 pulls in 001; source order preserved.
        sched = tmp_path / "trans.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]

    def test_filter_schedule_unknown_id_rejected(self, tmp_path: Path) -> None:
        sched = tmp_path / "full.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "999")
        assert cp.returncode == 1
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "unknown-task-id"
        assert "999" in body["errors"][0]["message"]

    def test_filter_schedule_missing_dep_rejected(
        self, tmp_path: Path,
    ) -> None:
        # V3 — source declares 002 with dep 999, 999 is absent from tasks[].
        # filter-schedule MUST halt with structured `missing-dependency`, NOT
        # a Python KeyError. The source schedule validates only because the
        # batch reference-integrity check is scoped to known ids; the dep
        # reference is what's broken here — filter-schedule is the consumer
        # that guards against the dangling reference.
        #
        # NOTE: the source must pass _validate_schedule upfront. The batch
        # here references only 001 (a known id) because the dep on 999 is
        # inside a task's dependencies array — not in batch.task_ids — and
        # _validate_schedule_refs only cross-checks batch.task_ids against
        # known tasks.
        sched = tmp_path / "broken.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": ["999"]},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["a"]}],
        }), encoding="utf-8")
        cp = _run_filter_schedule(sched, "1")
        assert cp.returncode == 1
        assert "KeyError" not in cp.stderr
        assert "Traceback" not in cp.stderr
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "missing-dependency" in codes
        # Message names both ids per V3 contract.
        msg = body["errors"][0]["message"]
        assert "001" in msg and "999" in msg

    def test_filter_schedule_cycle_rejected(self, tmp_path: Path) -> None:
        # V6 — cycle in the filtered subgraph: 001 → 002 → 001.
        # parse-schedule should also catch this upstream, but filter-schedule
        # has its own defensive check so a cycle cannot escape into
        # write-schedule downstream.
        sched = tmp_path / "cycle.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": ["002"]},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"]},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"],
                 "file_locks": ["a", "b"]},
            ],
        }), encoding="utf-8")
        cp = _run_filter_schedule(sched, "1")
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "dependency-cycle" in codes

    def test_filter_schedule_source_not_valid_rejected(self, tmp_path: Path) -> None:
        sched = tmp_path / "needs_enr.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "needs-enrichment",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["a"]}],
            "gaps": [{"id": "G1", "description": "x"}],
        }), encoding="utf-8")
        cp = _run_filter_schedule(sched, "1")
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "source-not-valid" in codes

    def test_filter_schedule_invalid_source_rejected(self, tmp_path: Path) -> None:
        # Source fails _validate_schedule (duplicate ids).
        sched = tmp_path / "dup.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": []},
                {"id": "001", "agent": "claude", "files": ["b"],
                 "dependencies": []},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["a"]}],
        }), encoding="utf-8")
        cp = _run_filter_schedule(sched, "1")
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "duplicate-task-id" in codes

    def test_filter_schedule_missing_file_rejected(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.json"
        cp = _run_filter_schedule(missing, "1")
        assert cp.returncode == 1
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "file-not-found"

    def test_filter_schedule_malformed_json_rejected(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        cp = _run_filter_schedule(bad, "1")
        assert cp.returncode == 1
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "json-decode"

    def test_filter_schedule_top_level_not_object_rejected(self, tmp_path: Path) -> None:
        lst = tmp_path / "list.json"
        lst.write_text("[]", encoding="utf-8")
        cp = _run_filter_schedule(lst, "1")
        assert cp.returncode == 1
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "top-level-not-object"

    def test_filter_schedule_alias_task_id_field_supported(self, tmp_path: Path) -> None:
        # Legacy alias `task_id` must be processed without KeyError — the
        # validator warns but does not normalize. Transitive closure over
        # 002 must still pull in 001 even through the alias.
        sched = tmp_path / "alias.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"task_id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": []},
                {"task_id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"]},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
        }), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        assert "KeyError" not in cp.stderr
        body = _parse_json(cp)
        # Task objects are copied verbatim — `task_id` is preserved.
        ids = [
            (t.get("id") if "id" in t else t.get("task_id")) for t in body["tasks"]
        ]
        # Transitive closure: requesting 002 pulls in 001 via the alias.
        assert ids == ["001", "002"]

    def test_filter_schedule_id_form_normalization(self, tmp_path: Path) -> None:
        # Accepts 001, 1, and TASK-001 forms; empty fragments skipped.
        sched = tmp_path / "norm.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "1,,002,TASK-003")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert [t["id"] for t in body["tasks"]] == ["001", "002", "003"]

    def test_filter_schedule_all_empty_task_ids_rejected(self, tmp_path: Path) -> None:
        sched = tmp_path / "full.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, ",,,")
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-task-ids" in codes

    def test_filter_schedule_drops_empty_batches(self, tmp_path: Path) -> None:
        sched = tmp_path / "drop.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": []},
                {"id": "003", "agent": "codex", "files": ["c"],
                 "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["003"], "file_locks": ["c"]},
                {"index": 2, "task_ids": ["001", "002"], "file_locks": ["a", "b"]},
            ],
        }), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        indices = [b["index"] for b in body["batches"]]
        assert indices == [2]
        assert body["batches"][0]["task_ids"] == ["002"]

    def test_filter_schedule_preserves_batch_index(self, tmp_path: Path) -> None:
        # V9 — after transitive closure of {002} the kept set is {001, 002}.
        # Source batches [1: (001,003), 2: (002)] survive filtering as
        # [1: (001), 2: (002)] — original `index` values preserved.
        sched = tmp_path / "preserve.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        indices = [b["index"] for b in body["batches"]]
        assert indices == [1, 2]

    def test_filter_schedule_filters_batch_task_ids(self, tmp_path: Path) -> None:
        # V10 — batch 1 had [001, 003]; filtering on 002 (transitive: 001)
        # keeps 001 only; 003 is filtered out of that batch's task_ids.
        sched = tmp_path / "filter_bids.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "002")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert [b["index"] for b in body["batches"]] == [1, 2]
        assert body["batches"][0]["task_ids"] == ["001"]
        assert body["batches"][1]["task_ids"] == ["002"]

    def test_filter_schedule_success_stdout_is_canonical_only(
        self, tmp_path: Path,
    ) -> None:
        """V11 — Hard-won regression from run 20260415T000811.

        Success stdout MUST have exactly the canonical five keys; no
        warnings/errors/other metadata. Otherwise write-schedule --stdin will
        reject the piped payload on unknown top-level fields.
        """
        sched = tmp_path / "canon.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert set(body.keys()) == {"outcome", "tasks", "batches", "gaps", "risks"}

    def test_filter_schedule_pipes_to_write_schedule_via_shell(
        self, tmp_path: Path,
    ) -> None:
        """V12 — Literal shell pipe, NO Python-side reshaping.

        Hard-won regression from run 20260415T000811: a prior implementation
        emitted `warnings`/`errors` keys on success and the test masked the
        bug by Python-popping them before piping. This test uses
        subprocess.run(..., shell=True) to prove byte-for-byte pipeability.
        """
        src = tmp_path / "src.schedule.json"
        dest = tmp_path / "filtered.schedule.json"
        src.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        pipeline = (
            f'{PY} {SCRIPT} filter-schedule '
            f'--schedule-file {src} --task-ids 2 --json '
            f'| {PY} {SCRIPT} write-schedule '
            f'--schedule-file {dest} --stdin --json'
        )
        cp = subprocess.run(
            pipeline, shell=True, capture_output=True, text=True,
            cwd=str(REPO_ROOT),
        )
        assert cp.returncode == 0, (
            f"pipeline failed:\nstdout={cp.stdout}\nstderr={cp.stderr}"
        )
        assert dest.is_file()
        # Verify write succeeded and file parses back cleanly. Transitive
        # closure pulls 001 in so the persisted file has both tasks.
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert written["outcome"] == "valid"
        assert [t["id"] for t in written["tasks"]] == ["001", "002"]
        ids_to_deps = {
            t["id"]: t.get("dependencies", []) for t in written["tasks"]
        }
        assert ids_to_deps["002"] == ["001"]

    def test_filter_schedule_full_round_trip_drops_source_risks(
        self, tmp_path: Path,
    ) -> None:
        """V13 — Source schedule has non-empty `risks`; filter-schedule +
        write-schedule + parse-schedule round-trip MUST exit 0 and the
        persisted file MUST have `risks=[]` regardless of source.

        Source-side `gaps` cannot be tested under outcome='valid' (validator
        forbids non-empty gaps with that outcome); `risks` is the only
        source-side metadata that could survive if the implementer
        mistakenly copied from source.
        """
        src_payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"]},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["a"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
            "gaps": [],
            "risks": [{"id": "R1", "description": "example risk"}],
        }
        src = tmp_path / "src_risks.schedule.json"
        dest = tmp_path / "filtered_risks.schedule.json"
        src.write_text(json.dumps(src_payload), encoding="utf-8")
        # Literal shell pipe: filter-schedule | write-schedule.
        pipeline = (
            f'{PY} {SCRIPT} filter-schedule '
            f'--schedule-file {src} --task-ids 2 --json '
            f'| {PY} {SCRIPT} write-schedule '
            f'--schedule-file {dest} --stdin --json'
        )
        cp = subprocess.run(
            pipeline, shell=True, capture_output=True, text=True,
            cwd=str(REPO_ROOT),
        )
        assert cp.returncode == 0, (
            f"pipeline failed:\nstdout={cp.stdout}\nstderr={cp.stderr}"
        )
        # Now parse the persisted file back through parse-schedule.
        parse_cmd = [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"]
        parse_cp = subprocess.run(
            parse_cmd, input=dest.read_text(encoding="utf-8"),
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert parse_cp.returncode == 0, parse_cp.stderr
        # Persisted file MUST have risks=[] regardless of source. Transitive
        # closure includes 001 (002's dep).
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert written.get("risks") == []
        assert written.get("gaps") == []
        assert written["outcome"] == "valid"
        assert [t["id"] for t in written["tasks"]] == ["001", "002"]


# ---------------------------------------------------------------------------
# TASK-014A — D.2a.5 bounded remediation retry surface
# ---------------------------------------------------------------------------


class TestRemediationTag:
    """V4 — `commit-task --remediation-tag` appends a `[remediation]` line to
    the commit body and records `remediation_tag=true` in the `commit_done`
    run-log event.
    """

    def test_remediation_tag_appears_in_commit_body(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "bump x post-remediation",
            "--reviewer", "codex",
            "--reviewer-verdict", "clean",
            "--remediation-tag",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        body_json = _parse_json(cp)
        assert body_json["commit_sha"]

        # Commit body (git log -1 --pretty=%B) must contain the [remediation] tag.
        body = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%B"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "[remediation]" in body, (
            f"expected [remediation] tag in commit body, got:\n{body}"
        )

    def test_remediation_tag_absent_by_default(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
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

        body = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%B"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "[remediation]" not in body

    def test_remediation_tag_flag_logged_in_commit_done(
        self, tmp_git_repo: Path
    ) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "retry fix",
            "--reviewer", "codex",
            "--reviewer-verdict", "clean",
            "--remediation-tag",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr

        log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        commit_events = [json.loads(ln) for ln in lines if '"commit_done"' in ln]
        assert commit_events, f"no commit_done event in log: {lines}"
        assert commit_events[-1]["remediation_tag"] is True


class TestLogEventAllowlist:
    """`log-event` MUST accept `awaiting_user` (per TASK-014A V2) and reject
    typos to prevent silent vocabulary drift.
    """

    def test_accepts_awaiting_user(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "awaiting_user",
            "--fields-json", (
                '{"run_id":"R1","task_id":"001",'
                '"stage":"post_remediation_review",'
                '"codex_findings":[],"d5_summary":"agreed"}'
            ),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["ok"] is True
        rec = json.loads(body["written_line"])
        assert rec["event"] == "awaiting_user"
        assert rec["stage"] == "post_remediation_review"

    def test_accepts_remediation_start(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "remediation_start",
            "--fields-json",
            '{"run_id":"R1","task_id":"001","findings_count":2}',
            "--json",
        )
        assert cp.returncode == 0, cp.stderr

    def test_rejects_unknown_event(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "bogus_typo_event",
            "--fields-json", '{"run_id":"R1"}',
            "--json",
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "unknown-event-type" in codes


class TestFinalizeExecutionLogOutcome:
    """`finalize-execution-log --outcome paused` must be accepted per
    TASK-014A; legacy callers that omit `--outcome` must still work.
    """

    def test_accepts_paused_outcome(self, isolated_plan: Path) -> None:
        rows = [{
            "task": "TASK-001",
            "agent": "claude",
            "reviewer": "codex",
            "verdict": "needs-rework",
            "commit": "-",
            "notes": "paused after retry",
        }]
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--starting-sha", "1111111",
            "--ending-sha", "1111111",
            "--outcome", "paused",
            "--rows-json", json.dumps(rows),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        text = isolated_plan.read_text(encoding="utf-8")
        assert "## Execution log — R1 (paused)" in text

    @pytest.mark.parametrize("outcome", ["success", "partial", "failed", "paused"])
    def test_accepts_all_allowed_outcomes(
        self, isolated_plan: Path, outcome: str,
    ) -> None:
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(isolated_plan),
            "--run-id", f"R-{outcome}",
            "--starting-sha", "aaa",
            "--ending-sha", "bbb",
            "--outcome", outcome,
            "--rows-json", "[]",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr

    def test_rejects_unknown_outcome(self, isolated_plan: Path) -> None:
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--starting-sha", "aaa",
            "--ending-sha", "bbb",
            "--outcome", "bogus",
            "--rows-json", "[]",
            "--json",
        )
        # argparse rejects invalid choices with exit-code 2 and writes to stderr.
        assert cp.returncode != 0

    def test_outcome_is_optional(self, isolated_plan: Path) -> None:
        """Callers that haven't migrated still work — heading omits the outcome
        suffix when --outcome is not passed."""
        cp = _run(
            "finalize-execution-log",
            "--plan-file", str(isolated_plan),
            "--run-id", "R-legacy",
            "--starting-sha", "aaa",
            "--ending-sha", "bbb",
            "--rows-json", "[]",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        text = isolated_plan.read_text(encoding="utf-8")
        assert "## Execution log — R-legacy\n" in text
        assert "## Execution log — R-legacy (" not in text


# ---------------------------------------------------------------------------
# TASK-014B — roster auto-update in commit-task (V5–V7)
# ---------------------------------------------------------------------------


def _write_index(plans_dir: Path, chunks: list[dict]) -> Path:
    """Write a `00_INDEX.json` with the given chunk rows into `plans_dir`.

    Mirrors the on-disk schema (`schema_version=1`, `chunks=[...]`) that
    `_parse_index_roster` validates. Returns the path.
    """
    path = plans_dir / "00_INDEX.json"
    path.write_text(
        json.dumps({"schema_version": 1, "source": "test", "chunks": chunks}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return path


class TestRosterAutoUpdate:
    """V5–V7 — `commit-task` flips the matching `00_INDEX.json` chunk's
    `status` to `Done`, writes atomically via tempfile+os.replace, is
    idempotent on re-run, and rolls back the commit if the chunk is
    missing from the roster.
    """

    def _chunk(self, task_id: str, file_: str, status: str = "Pending") -> dict:
        return {
            "task_id": task_id,
            "v3_task": f"TASK-{task_id}",
            "file": file_,
            "priority": "medium",
            "issues_absorbed": [],
            "depends_on": [],
            "status": status,
            "superseded_by": [],
        }

    def test_v5_commit_task_flips_pending_to_done(
        self, tmp_git_repo: Path
    ) -> None:
        """V5 — a successful commit-task flips the chunk's status to Done."""
        plans_dir = tmp_git_repo / "docs" / "plans"
        index_path = _write_index(plans_dir, [
            self._chunk("001", "sample.md", status="Pending"),
        ])
        before = json.loads(index_path.read_text(encoding="utf-8"))
        assert before["chunks"][0]["status"] == "Pending"

        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
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

        after = json.loads(index_path.read_text(encoding="utf-8"))
        assert after["chunks"][0]["status"] == "Done"
        # Every other field (task_id, file, priority, depends_on, ...) is
        # byte-preserved — the mutation is narrow.
        for field in ("task_id", "file", "priority", "depends_on", "superseded_by"):
            assert after["chunks"][0][field] == before["chunks"][0][field]
        # Plan-level status bullet flip is unchanged (existing behavior).
        assert "### TASK-001: First task\n\n- **Status:** done" in plan.read_text(
            encoding="utf-8"
        )
        # The roster update MUST be part of the commit, not an uncommitted
        # working-tree mutation — drift is the whole reason TASK-014B exists.
        head_index = subprocess.run(
            ["git", "show", "HEAD:docs/plans/00_INDEX.json"],
            capture_output=True, text=True, cwd=tmp_git_repo, check=True,
        )
        head_roster = json.loads(head_index.stdout)
        assert head_roster["chunks"][0]["status"] == "Done"
        # And no uncommitted changes to the roster are left behind.
        porcelain = subprocess.run(
            ["git", "status", "--porcelain", "--", "docs/plans/00_INDEX.json"],
            capture_output=True, text=True, cwd=tmp_git_repo, check=True,
        )
        assert porcelain.stdout.strip() == ""

    def test_v5_only_matching_chunk_mutated(
        self, tmp_git_repo: Path
    ) -> None:
        """`commit-task` MUST only mutate the chunk whose `file` matches the
        plan's basename; sibling chunks are byte-preserved.
        """
        plans_dir = tmp_git_repo / "docs" / "plans"
        index_path = _write_index(plans_dir, [
            self._chunk("001", "sample.md", status="Pending"),
            self._chunk("002", "other.md", status="Pending"),
            self._chunk("003", "third.md", status="Done"),
        ])
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
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

        after = json.loads(index_path.read_text(encoding="utf-8"))
        # 001: flipped to Done.
        assert after["chunks"][0]["task_id"] == "001"
        assert after["chunks"][0]["status"] == "Done"
        # 002: untouched — a stale Pending that a future run will flip.
        assert after["chunks"][1]["task_id"] == "002"
        assert after["chunks"][1]["status"] == "Pending"
        # 003: untouched Done.
        assert after["chunks"][2]["task_id"] == "003"
        assert after["chunks"][2]["status"] == "Done"

    def test_v6_write_is_atomic_no_tmpfile_leftover(
        self, tmp_git_repo: Path
    ) -> None:
        """V6 — after a successful commit-task, no `.tmp`/`.00_INDEX.*`
        temporary file is left behind in the plans directory.
        """
        plans_dir = tmp_git_repo / "docs" / "plans"
        _write_index(plans_dir, [
            self._chunk("001", "sample.md", status="Pending"),
        ])

        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
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

        leftovers = [
            p.name for p in plans_dir.iterdir()
            if p.name.startswith(".00_INDEX.") or p.suffix == ".tmp"
        ]
        assert leftovers == [], f"tempfile leaked: {leftovers}"

    def test_v6_idempotent_second_commit_byte_identical(
        self, tmp_git_repo: Path
    ) -> None:
        """V6 — re-running commit-task for an already-Done chunk produces
        byte-identical `00_INDEX.json` on the second run.
        """
        plans_dir = tmp_git_repo / "docs" / "plans"
        index_path = _write_index(plans_dir, [
            self._chunk("001", "sample.md", status="Pending"),
        ])

        # First run: Pending → Done.
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
        cp1 = _run(
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
        assert cp1.returncode == 0, cp1.stderr
        bytes_after_first = index_path.read_bytes()
        assert json.loads(bytes_after_first)["chunks"][0]["status"] == "Done"

        # Second run: already-Done → no-op write, byte-identical.
        (tmp_git_repo / "src" / "foo.py").write_text("x = 3\n", encoding="utf-8")
        cp2 = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R2",
            "--files", "src/foo.py",
            "--title", "First task retry",
            "--diff-summary", "bump x again",
            "--reviewer", "none",
            "--reviewer-verdict", "",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp2.returncode == 0, cp2.stderr
        bytes_after_second = index_path.read_bytes()
        assert bytes_after_second == bytes_after_first, (
            "idempotent re-run must be byte-identical"
        )

    def test_v7_missing_roster_entry_rolls_back_commit(
        self, tmp_git_repo: Path
    ) -> None:
        """V7 — if the plan's basename has no matching chunk in
        `00_INDEX.json`, commit-task exits 1 with
        `errors[0].code == 'task-not-in-index'` and the git commit is rolled
        back (HEAD unchanged; working-tree edits remain for the user).
        """
        plans_dir = tmp_git_repo / "docs" / "plans"
        # Roster is present but sample.md has no entry — only an unrelated
        # chunk exists. This is the drift scenario the plan calls out.
        index_path = _write_index(plans_dir, [
            self._chunk("999", "unrelated.md", status="Pending"),
        ])
        original_index_bytes = index_path.read_bytes()

        initial_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
        original_plan_text = plan.read_text(encoding="utf-8")

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
        assert cp.returncode == 1, cp.stdout + cp.stderr
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "task-not-in-index" in codes
        assert body["errors"][0]["code"] == "task-not-in-index"

        # Git HEAD is unchanged — the commit was rolled back.
        final_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert final_sha == initial_sha, (
            "rollback must leave HEAD at the pre-commit-task SHA"
        )

        # Plan text is restored — the status bullet flip is undone.
        assert plan.read_text(encoding="utf-8") == original_plan_text
        # Roster bytes are also byte-identical — the rollback introduced
        # by TASK-014B must preserve the sidecar on the missing-entry path
        # (the roster mutation is skipped entirely before git state changes).
        assert index_path.read_bytes() == original_index_bytes

    def test_fail_task_leaves_roster_untouched(
        self, tmp_git_repo: Path
    ) -> None:
        """`fail-task` MUST NOT mutate the roster. Future retries need the
        original Pending status intact.
        """
        plans_dir = tmp_git_repo / "docs" / "plans"
        index_path = _write_index(plans_dir, [
            self._chunk("001", "sample.md", status="Pending"),
        ])
        before_bytes = index_path.read_bytes()

        (tmp_git_repo / "src" / "foo.py").write_text("BROKEN\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
        cp = _run(
            "fail-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--stage", "implement",
            "--reason", "malformed_report",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        assert index_path.read_bytes() == before_bytes, (
            "fail-task must not touch 00_INDEX.json"
        )

    def test_commit_task_with_no_roster_still_commits(
        self, tmp_git_repo: Path
    ) -> None:
        """Backward compatibility — plans that predate the roster (no
        `00_INDEX.json` next to the plan file) MUST still commit cleanly.
        Only drift in an EXISTING roster is load-bearing.
        """
        # No _write_index call — tmp_git_repo has no 00_INDEX.json.
        plans_dir = tmp_git_repo / "docs" / "plans"
        assert not (plans_dir / "00_INDEX.json").exists()

        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
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
        body = _parse_json(cp)
        assert body["commit_sha"]

    def test_commit_task_malformed_roster_is_hard_error(
        self, tmp_git_repo: Path
    ) -> None:
        """A present-but-invalid `00_INDEX.json` (bad JSON or missing
        `chunks`) is a hard error — commit-task must NOT silently treat it
        as "no roster" and commit anyway. Otherwise a corrupted sidecar
        would let drift sneak through. The plan text is restored and git
        HEAD is unchanged.
        """
        plans_dir = tmp_git_repo / "docs" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        index_path = plans_dir / "00_INDEX.json"
        index_path.write_text("{ not valid json", encoding="utf-8")

        initial_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()

        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = plans_dir / "sample.md"
        original_plan_text = plan.read_text(encoding="utf-8")

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
        assert cp.returncode == 1, cp.stdout + cp.stderr
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "index-malformed" in codes

        final_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert final_sha == initial_sha
        assert plan.read_text(encoding="utf-8") == original_plan_text
        # The malformed sidecar is left exactly as-is — we never rewrote it.
        assert index_path.read_text(encoding="utf-8") == "{ not valid json"


# ---------------------------------------------------------------------------
# TASK-014C — Phase 1.5 Codex plan review gate
# ---------------------------------------------------------------------------
#
# V8  — event ordering (documented in SKILL.md; asserted here by prose
#        assertion + the presence of the new event vocab in
#        ALLOWED_LOG_EVENTS, which is the tripwire the orchestrator depends
#        on).
# V9  — verdict routing (validated via parse-plan-review-report on
#        synthetic envelopes; each verdict lands in the output).
# V10 — codex unavailable → degrade (wrapper's `codex_not_found` /
#        `failure` outcomes surface as terminal-outcome envelopes that
#        parse-plan-review-report accepts without raising schema errors).
# V11 — --skip-plan-review bypass (documented in SKILL.md §Phase 1.5 and
#        in the CLI flag list; prose-asserted here).


_PLAN_REVIEW_SCHEMA = (
    SCRIPTS_DIR / "codex_plan_review_schema.json"
)


def _plan_review_envelope(
    *,
    verdict: str | None = "approved",
    outcome: str = "success",
    findings: list | None = None,
    plan_file: str = "sample.md",
    schedule_ok: bool = True,
    dependencies_ok: bool = True,
    summary: str = "ok",
    error: str | None = None,
    drop_parsed: bool = False,
) -> dict:
    """Build a synthetic plan-review wrapper envelope for the parser tests."""
    envelope: dict = {
        "task_id": "plan",
        "plan_file": plan_file,
        "subcommand": "plan-review",
        "outcome": outcome,
        "codex_exit_code": 0,
        "codex_output_raw": None,
        "error": error,
    }
    if drop_parsed:
        envelope["parsed"] = None
        return envelope
    parsed = {
        "plan_file": plan_file,
        "verdict": verdict,
        "findings": findings if findings is not None else [],
        "schedule_ok": schedule_ok,
        "dependencies_ok": dependencies_ok,
        "summary": summary,
    }
    envelope["parsed"] = parsed
    return envelope


class TestPlanReviewSchemaFile:
    """V10 scaffold — the schema file exists and is valid JSON with the
    expected verdict vocabulary. Codex unavailability is handled at the
    parser level (terminal outcomes) but the schema must be on disk so
    the wrapper can hand it to `codex exec --output-schema`."""

    def test_schema_file_exists(self) -> None:
        assert _PLAN_REVIEW_SCHEMA.is_file(), (
            f"expected schema file at {_PLAN_REVIEW_SCHEMA}"
        )

    def test_schema_declares_verdict_enum(self) -> None:
        schema = json.loads(_PLAN_REVIEW_SCHEMA.read_text(encoding="utf-8"))
        verdict = schema["properties"]["verdict"]
        assert set(verdict["enum"]) == {
            "approved", "approved-with-notes", "needs-replan",
        }
        required = set(schema["required"])
        assert required == {
            "plan_file", "verdict", "findings",
            "schedule_ok", "dependencies_ok", "summary",
        }


class TestParsePlanReviewReport:
    """V9 — verdict routing. parse-plan-review-report extracts the verdict
    from a valid envelope so the orchestrator can route on it.
    """

    def _run_parser(self, envelope: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report", "--stdin", "--json",
            ],
            input=json.dumps(envelope),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize(
        "verdict",
        ["approved", "approved-with-notes", "needs-replan"],
    )
    def test_accepts_each_verdict(self, verdict: str) -> None:
        cp = self._run_parser(_plan_review_envelope(verdict=verdict))
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == verdict
        assert body["plan_file"] == "sample.md"
        assert body["findings_count"] == 0
        assert body["errors"] == []

    def test_findings_count_reflects_findings_len(self) -> None:
        findings = [
            {
                "severity": "important",
                "section": "TASK-014C Files",
                "concern": "schema file missing",
                "suggested_change": "add codex_plan_review_schema.json",
            },
            {
                "severity": "minor",
                "section": "Context",
                "concern": "typo in §Scoped Context",
                "suggested_change": "re-read paragraph and fix",
            },
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["findings_count"] == 2
        assert body["findings"] == findings

    def test_rejects_invalid_verdict(self) -> None:
        cp = self._run_parser(_plan_review_envelope(verdict="clean"))
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-plan-review-verdict" in codes

    def test_rejects_missing_parsed(self) -> None:
        cp = self._run_parser(_plan_review_envelope(drop_parsed=True))
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        # `parsed: null` surfaces as an invalid-type / missing-field error;
        # either is acceptable for contract purposes as long as the run halts.
        assert any(
            code in codes
            for code in ("invalid-type", "missing-field")
        ), codes

    def test_rejects_unknown_parsed_field(self) -> None:
        env = _plan_review_envelope()
        env["parsed"]["unexpected_top_field"] = "nope"
        cp = self._run_parser(env)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "unknown-parsed-field" in codes

    def test_rejects_missing_required_parsed_field(self) -> None:
        env = _plan_review_envelope()
        env["parsed"].pop("schedule_ok")
        cp = self._run_parser(env)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "missing-field" in codes

    def test_rejects_malformed_finding(self) -> None:
        env = _plan_review_envelope(
            verdict="needs-replan",
            findings=[{"severity": "info", "section": "x"}],
        )
        cp = self._run_parser(env)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        # Missing required fields + bad severity both land in errors[*].
        assert "missing-plan-review-finding-field" in codes
        assert "invalid-plan-review-finding-severity" in codes

    def test_rejects_non_plan_review_subcommand(self) -> None:
        env = _plan_review_envelope()
        env["subcommand"] = "review"
        cp = self._run_parser(env)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-subcommand" in codes

    def test_wrong_subcommand_with_terminal_outcome_still_rejected(self) -> None:
        """Regression: envelope-level validation MUST run before the
        terminal-outcome shortcut. Otherwise a malformed envelope like
        `{"subcommand":"review","outcome":"failure"}` would slip through as
        a 'codex unavailable' degradation signal, masking a real
        contract violation.
        """
        env = {"subcommand": "review", "outcome": "failure", "error": "nope"}
        cp = self._run_parser(env)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-subcommand" in codes

    def test_rejects_empty_stdin(self) -> None:
        cp = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report", "--stdin", "--json",
            ],
            input="",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "empty-stdin" in codes

    def test_rejects_non_json_stdin(self) -> None:
        cp = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report", "--stdin", "--json",
            ],
            input="not json at all",
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "json-decode" in codes

    @pytest.mark.parametrize(
        "outcome",
        ["failure", "timeout", "parse_error", "scope_violation"],
    )
    def test_terminal_outcomes_surface_without_schema_violation(
        self, outcome: str,
    ) -> None:
        """V10 — wrapper timeout / failure / parse_error envelopes don't
        have a schema-compliant `parsed` body. parse-plan-review-report
        must surface the outcome for orchestrator routing without raising
        a schema violation, so the orchestrator can degrade Phase 1.5
        to a warning (codex unavailable / plan review skipped) rather
        than halt."""
        env = _plan_review_envelope(outcome=outcome, error="codex not found")
        # Clear parsed to simulate a non-success envelope.
        env["parsed"] = None
        cp = self._run_parser(env)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["outcome"] == outcome
        assert body["verdict"] is None
        assert body["envelope_error"] == "codex not found"


class TestPlanReviewRunLogEvents:
    """V8 — the new plan-review events must be in the ALLOWED_LOG_EVENTS
    tripwire so typo'd events surface immediately and the orchestrator's
    event-order documentation stays enforceable."""

    def test_plan_review_events_allowed(self) -> None:
        allowed = plan_ops.ALLOWED_LOG_EVENTS
        assert "plan_review_start" in allowed
        assert "plan_review_done" in allowed
        assert "plan_review_skipped" in allowed


class TestPlanReviewConstants:
    """V9 — verdict vocabulary lives in a single constant so wrapper,
    parser, and orchestrator routing can't drift."""

    def test_verdict_vocab(self) -> None:
        assert plan_ops.ALLOWED_PLAN_REVIEW_VERDICTS == {
            "approved", "approved-with-notes", "needs-replan",
        }

    def test_severity_vocab(self) -> None:
        assert plan_ops.ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES == {
            "critical", "important", "minor",
        }


class TestPlanReviewDocumentation:
    """V8 + V11 — SKILL.md and dispatch-templates.md document the Phase 1.5
    protocol. The orchestrator is a markdown reader; if the prose goes
    missing, the gate silently stops running. Assert the load-bearing
    anchor strings exist verbatim."""

    SKILL = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "SKILL.md"
    )
    TEMPLATES = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "dispatch-templates.md"
    )

    def test_skill_md_has_phase_1_5_section(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        assert "### Phase 1.5 — Codex plan review" in text
        # V8 event order must be documented for the orchestrator to follow.
        assert "plan_review_start" in text
        assert "plan_review_done" in text
        # V9 verdict routing.
        assert "approved" in text
        assert "approved-with-notes" in text
        assert "needs-replan" in text
        # V10 degradation.
        assert "codex_unavailable" in text

    def test_skill_md_documents_skip_plan_review_flag(self) -> None:
        # V11 — --skip-plan-review must appear in the CLI surface docs and
        # be parallel-safe with --skip-cross-review.
        text = self.SKILL.read_text(encoding="utf-8")
        assert "--skip-plan-review" in text
        # Parallel-safe caveat must be documented so the orchestrator
        # doesn't invent a spurious conflict check.
        assert "--skip-cross-review" in text

    def test_phase_1_5_inserted_before_dry_run_mode(self) -> None:
        """Acceptance criterion: Phase 1.5 inserts after schedule persist
        and before Dry-run mode."""
        text = self.SKILL.read_text(encoding="utf-8")
        phase_1_5_idx = text.find("### Phase 1.5")
        dry_run_idx = text.find("### Dry-run mode")
        write_schedule_idx = text.find("write-schedule --schedule-file")
        assert phase_1_5_idx >= 0
        assert dry_run_idx >= 0
        assert write_schedule_idx >= 0
        assert write_schedule_idx < phase_1_5_idx < dry_run_idx

    def test_dispatch_templates_has_phase_1_5_block(self) -> None:
        text = self.TEMPLATES.read_text(encoding="utf-8")
        assert "Phase 1.5" in text
        assert "plan_codex_dispatch.py" in text
        assert "plan-review" in text
        assert "--plan-file" in text
        assert "--schedule-file" in text


class TestPlanCodexDispatchPlanReviewSubcommand:
    """V9 — the wrapper exposes a plan-review subcommand with the expected
    CLI shape. Use --dry-run so we don't need Codex on the test runner."""

    WRAPPER = SCRIPTS_DIR / "plan_codex_dispatch.py"

    def test_dry_run_emits_envelope(self, tmp_path: Path) -> None:
        plan = tmp_path / "sample.md"
        plan.write_text("# plan\n\n## Context\n\nprose\n", encoding="utf-8")
        schedule = tmp_path / "sample.schedule.json"
        schedule.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
        }), encoding="utf-8")

        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--plan-file", str(plan),
                "--schedule-file", str(schedule),
                "--plans-dir", str(tmp_path),
                "--repo-root", str(tmp_path),
                "--dry-run",
                "--timeout", "180",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        assert body["subcommand"] == "plan-review"
        assert body["outcome"] == "dry_run"
        assert body["plan_file"] == "sample.md"
        assert "prompt_preview" in body
        # The prompt must carry the verdict vocab so Codex knows what to
        # return; if this drifts, the wrapper silently corrupts the
        # gating contract.
        assert "approved" in body["prompt_preview"]
        assert "needs-replan" in body["prompt_preview"]
        # The prompt must direct Codex to compute dependencies_ok via
        # check-plan-deps rather than parsing it from the plan; otherwise
        # Codex falls back to hunting for the legacy tasks[*].dependencies
        # field and blocks the run on its absence.
        assert "check-plan-deps" in body["prompt_preview"]

    def test_missing_plan_file_fails(self, tmp_path: Path) -> None:
        schedule = tmp_path / "sample.schedule.json"
        schedule.write_text("{}", encoding="utf-8")
        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--plan-file", str(tmp_path / "nope.md"),
                "--schedule-file", str(schedule),
                "--plans-dir", str(tmp_path),
                "--repo-root", str(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert cp.returncode == 1
        body = json.loads(cp.stdout)
        assert body["outcome"] == "failure"
        assert "Plan file not found" in (body.get("error") or "")

    def test_missing_schedule_file_fails(self, tmp_path: Path) -> None:
        plan = tmp_path / "sample.md"
        plan.write_text("# plan\n", encoding="utf-8")
        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--plan-file", str(plan),
                "--schedule-file", str(tmp_path / "nope.json"),
                "--plans-dir", str(tmp_path),
                "--repo-root", str(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert cp.returncode == 1
        body = json.loads(cp.stdout)
        assert body["outcome"] == "failure"
        assert "Schedule file not found" in (body.get("error") or "")

    def test_malformed_schedule_json_fails(self, tmp_path: Path) -> None:
        plan = tmp_path / "sample.md"
        plan.write_text("# plan\n", encoding="utf-8")
        schedule = tmp_path / "sample.schedule.json"
        schedule.write_text("{ not valid json", encoding="utf-8")
        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--plan-file", str(plan),
                "--schedule-file", str(schedule),
                "--plans-dir", str(tmp_path),
                "--repo-root", str(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        assert cp.returncode == 1
        body = json.loads(cp.stdout)
        assert body["outcome"] == "failure"
        assert "not valid JSON" in (body.get("error") or "")


# ---------------------------------------------------------------------------
# TASK-016A — D.5 partial-agreement verdict + adjudication payload parser
# ---------------------------------------------------------------------------
#
# V1 — parse-d5-adjudication accepts a partial-agreement payload whose
#      buckets are non-empty, disjoint, and in-range; the structured
#      dispatch fields are surfaced to the orchestrator.
# V2 — empty or overlapping buckets emit partial-agreement-invalid-split.
# V3 — out-of-range indices emit partial-agreement-unknown-index.


class TestD5AdjudicationConstants:
    """The verdict vocab MUST contain `partial-agreement` alongside the
    three classical D.5 verdicts. Everything else in the routing chain
    keys off this set; drift would silently break D.2a.6 routing.
    """

    def test_partial_agreement_in_allowed_claude_verdicts(self) -> None:
        assert "partial-agreement" in plan_ops.ALLOWED_CLAUDE_REVIEW_VERDICTS
        assert plan_ops.ALLOWED_CLAUDE_REVIEW_VERDICTS == {
            "ship", "ship-with-fixes", "partial-agreement", "needs-rework",
        }


class TestParseD5Adjudication:
    """V1–V3 — parse-d5-adjudication validates the adjudication payload
    shape D.5 emits and surfaces the structured dispatch fields the
    orchestrator forwards to D.2a.6.
    """

    def _run_parser(
        self,
        payload: dict | str,
        *,
        codex_findings_count: int,
    ) -> subprocess.CompletedProcess:
        stdin = (
            payload if isinstance(payload, str) else json.dumps(payload)
        )
        return subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-d5-adjudication", "--stdin",
                "--codex-findings-count", str(codex_findings_count),
                "--json",
            ],
            input=stdin,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    # -- V1 -----------------------------------------------------------------

    def test_accepts_partial_agreement_with_clean_split(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0, 2],
                "dismissed": [1, 3],
                "summary": "0 and 2 block ship; 1 and 3 are nits",
            },
            codex_findings_count=4,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == "partial-agreement"
        assert body["load_bearing"] == [0, 2]
        assert body["dismissed"] == [1, 3]
        assert body["summary"].startswith("0 and 2 block")
        assert body["errors"] == []

    @pytest.mark.parametrize(
        "verdict", ["ship", "ship-with-fixes", "needs-rework"],
    )
    def test_accepts_non_split_verdicts_without_buckets(
        self, verdict: str,
    ) -> None:
        """The split fields are partial-agreement-only; other verdicts
        pass through with load_bearing/dismissed left as null."""
        cp = self._run_parser(
            {"verdict": verdict, "summary": "justification"},
            codex_findings_count=3,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == verdict
        assert body["load_bearing"] is None
        assert body["dismissed"] is None

    def test_accepts_single_load_bearing_single_dismissed(self) -> None:
        """Minimum non-empty split: one on each side."""
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [1],
                "dismissed": [0],
                "summary": "one each side",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["load_bearing"] == [1]
        assert body["dismissed"] == [0]

    # -- V2 -----------------------------------------------------------------

    def test_rejects_empty_load_bearing(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [],
                "dismissed": [0, 1],
                "summary": "should have been ship-with-fixes",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-invalid-split" in codes

    def test_rejects_empty_dismissed(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0, 1],
                "dismissed": [],
                "summary": "should have been needs-rework",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-invalid-split" in codes

    def test_rejects_overlapping_buckets(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0, 2],
                "dismissed": [1, 2],
                "summary": "2 in both buckets is a contradiction",
            },
            codex_findings_count=3,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-invalid-split" in codes
        msgs = " ".join(e["message"] for e in body["errors"])
        assert "[2]" in msgs or "2" in msgs

    # -- V3 -----------------------------------------------------------------

    def test_rejects_out_of_range_index(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [4],
                "dismissed": [0],
                "summary": "idx 4 hallucinated for a length-2 array",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-unknown-index" in codes

    def test_rejects_negative_index(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0],
                "dismissed": [-1],
                "summary": "negative indices are nonsense",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-unknown-index" in codes

    # -- misc surface checks -----------------------------------------------

    def test_rejects_invalid_verdict(self) -> None:
        cp = self._run_parser(
            {"verdict": "clean", "summary": "wrong vocab"},
            codex_findings_count=1,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-reviewer-verdict" in codes

    def test_rejects_non_integer_bucket_entries(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": ["0"],
                "dismissed": [1],
                "summary": "indices must be integers",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-type" in codes

    def test_rejects_empty_stdin(self) -> None:
        cp = self._run_parser("", codex_findings_count=1)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "empty-stdin" in codes

    def test_rejects_non_json_stdin(self) -> None:
        cp = self._run_parser("not valid json", codex_findings_count=1)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "json-decode" in codes


class TestD5TemplateDocumentation:
    """Acceptance-criterion anchor: the Phase D.5 template in
    dispatch-templates.md must advertise `partial-agreement` in the
    verdict enum AND include the output-shape example with both
    `load_bearing` and `dismissed` arrays. These anchors are the
    orchestrator's only contact with the reviewer's output schema;
    prose drift would silently break D.2a.6 routing.
    """

    TEMPLATES = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "dispatch-templates.md"
    )

    def test_d5_template_lists_partial_agreement_in_verdict_enum(
        self,
    ) -> None:
        text = self.TEMPLATES.read_text(encoding="utf-8")
        # D.5 section heading exists.
        assert "## Phase D.5" in text
        # Verdict enum includes partial-agreement alongside the three
        # classical verdicts.
        d5_start = text.find("## Phase D.5")
        d5_body = text[d5_start:]
        # Next top-level section boundary.
        next_section = d5_body.find("\n## ", 1)
        if next_section >= 0:
            d5_body = d5_body[:next_section]
        assert "partial-agreement" in d5_body
        assert "ship" in d5_body
        assert "ship-with-fixes" in d5_body
        assert "needs-rework" in d5_body
        # Output-shape example MUST include both bucket fields.
        assert "load_bearing" in d5_body
        assert "dismissed" in d5_body

    def test_d5_template_includes_decision_rubric(self) -> None:
        text = self.TEMPLATES.read_text(encoding="utf-8")
        d5_start = text.find("## Phase D.5")
        d5_body = text[d5_start:]
        next_section = d5_body.find("\n## ", 1)
        if next_section >= 0:
            d5_body = d5_body[:next_section]
        # Rubric instructs the reviewer on when to pick partial-agreement.
        assert "load-bearing" in d5_body.lower()
        assert "dismissed" in d5_body
        # The hard rule against a unanimous split must be named.
        assert (
            "non-empty" in d5_body.lower()
            or "both" in d5_body.lower()
        )


class TestD5RouteTableDocumentation:
    """The D.2a route table in SKILL.md is the structural switch that
    actually routes partial-agreement to D.2a.6. Missing row = the
    verdict becomes dead text.
    """

    SKILL = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "SKILL.md"
    )

    def test_route_table_has_partial_agreement_row(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        # The D.2a section and its route table must both exist.
        assert "#### D.2a — Escalation" in text
        # Row text (forward-reference to D.2a.6 is OK at this chunk;
        # body lands in TASK-016C).
        assert "partial-agreement" in text
        # The route column must mention D.2a.6 (forward-referenced).
        assert "D.2a.6" in text


# ---------------------------------------------------------------------------
# TASK-016C — D.2a.6 narrow-remediation retry path + commit trailers
# ---------------------------------------------------------------------------
#
# V6  — narrow_remediation_start / narrow_remediation_done are in the
#       run-log allow-list and round-trip through log-event.
# V7  — (verified by TestD5RouteTableDocumentation above — TASK-016A owns
#       the route-table row; this task only confirms presence.)
# V8  — Phase B-narrow-remediation template in dispatch-templates.md
#       advertises plan-remediator + the three JSON slots.
# V9  — commit-task --narrow-remediation-tag --dismissed-finding-ids
#       emits the [narrow-remediation]\n[disagreement: I,J,K] trailers.
# V10 — argparse constraints (a)-(d) are enforced with argparse errors.
# V11 — awaiting_user event accepts the two new stage labels with the
#       D.2a.5-parallel payload shape, and SKILL.md §D.2a.6 documents
#       the retry protocol end-to-end.


class TestNarrowRemediationLogEvents:
    """V6 — `narrow_remediation_start` / `narrow_remediation_done` MUST be
    in the `log-event` allow-list so the orchestrator can signal D.2a.6
    entry/exit without the allow-list tripwire firing. Distinct from
    `remediation_start` (which belongs to D.2a.5 full rework) so the run
    log is the audit source of truth for which retry path fired.
    """

    def test_narrow_remediation_start_accepted(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "narrow_remediation_start",
            "--fields-json",
            (
                '{"run_id":"R1","task_id":"001",'
                '"load_bearing_count":2,"dismissed_count":2,'
                '"d5_summary":"0 and 2 are ship-blockers"}'
            ),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        rec = json.loads(body["written_line"])
        assert rec["event"] == "narrow_remediation_start"
        assert rec["load_bearing_count"] == 2
        assert rec["dismissed_count"] == 2

    def test_narrow_remediation_done_accepted(self, isolated_plan: Path) -> None:
        cp = _run(
            "log-event",
            "--event", "narrow_remediation_done",
            "--fields-json",
            '{"run_id":"R1","task_id":"001","outcome":"success"}',
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        rec = json.loads(body["written_line"])
        assert rec["event"] == "narrow_remediation_done"
        assert rec["outcome"] == "success"

    def test_narrow_events_distinct_from_d2a5_events(self) -> None:
        """The narrow-remediation events are sibling entries to the
        D.2a.5 events, not replacements. Both pairs MUST coexist in the
        allow-list so the orchestrator can still emit the full-rework
        pair on the `needs-rework` route."""
        assert "remediation_start" in plan_ops.ALLOWED_LOG_EVENTS
        assert "narrow_remediation_start" in plan_ops.ALLOWED_LOG_EVENTS
        assert "narrow_remediation_done" in plan_ops.ALLOWED_LOG_EVENTS


class TestAwaitingUserNarrowStages:
    """V11 — `awaiting_user` event accepts the two D.2a.6 stage labels
    with the D.2a.5-parallel payload shape. Tests the structural
    round-trip; stage-label validation lives in the orchestrator
    template, not in `log-event` itself.
    """

    def test_post_narrow_remediation_review_accepted(
        self, isolated_plan: Path
    ) -> None:
        cp = _run(
            "log-event",
            "--event", "awaiting_user",
            "--fields-json",
            (
                '{"run_id":"R1","task_id":"001",'
                '"stage":"post_narrow_remediation_review",'
                '"codex_findings":[],"d5_summary":"split stable",'
                '"dismissed_finding_indices":[1,3],'
                '"dirty_files":["src/foo.py"]}'
            ),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        rec = json.loads(body["written_line"])
        assert rec["event"] == "awaiting_user"
        assert rec["stage"] == "post_narrow_remediation_review"
        assert rec["dismissed_finding_indices"] == [1, 3]

    def test_post_narrow_remediation_implement_accepted(
        self, isolated_plan: Path
    ) -> None:
        cp = _run(
            "log-event",
            "--event", "awaiting_user",
            "--fields-json",
            (
                '{"run_id":"R1","task_id":"001",'
                '"stage":"post_narrow_remediation_implement",'
                '"retry_outcome":"scope-violation",'
                '"diagnostics":[],'
                '"reversion_guidance":"see report",'
                '"dirty_files":["src/foo.py"]}'
            ),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        rec = json.loads(body["written_line"])
        assert rec["event"] == "awaiting_user"
        assert rec["stage"] == "post_narrow_remediation_implement"
        assert rec["retry_outcome"] == "scope-violation"


class TestNarrowRemediationCommitTag:
    """V9 — `commit-task --narrow-remediation-tag --dismissed-finding-ids
    I,J,K` emits the `[narrow-remediation]\\n[disagreement: I,J,K]` trailers
    on their own adjacent lines, narrow-remediation first. Run-log
    `commit_done` event surfaces the flags so the run summary can
    distinguish narrow retries from full D.2a.5 retries without
    re-parsing the commit body.
    """

    def test_narrow_remediation_trailers_in_commit_body(
        self, tmp_git_repo: Path
    ) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "narrow fix",
            "--reviewer", "codex",
            "--reviewer-verdict", "clean",
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "1,3",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        body = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%B"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        # Narrow-remediation first, disagreement-with-ids immediately
        # after on the next line. The exact adjacency is the auditable
        # shape the run summary keys off.
        assert "[narrow-remediation]\n[disagreement: 1,3]" in body, (
            f"expected adjacent trailers in commit body, got:\n{body}"
        )
        # Absence checks — neither the bare [disagreement] form nor the
        # D.2a.5 [remediation] tag should appear on a D.2a.6 commit.
        assert "[remediation]\n" not in body.replace(
            "[narrow-remediation]\n", ""
        ), f"unexpected bare [remediation] tag: {body}"

    def test_commit_done_records_narrow_flags(
        self, tmp_git_repo: Path
    ) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "narrow fix",
            "--reviewer", "codex",
            "--reviewer-verdict", "clean",
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "0,2,4",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr

        log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        commit_events = [json.loads(ln) for ln in lines if '"commit_done"' in ln]
        assert commit_events, f"no commit_done event: {lines}"
        rec = commit_events[-1]
        assert rec["narrow_remediation_tag"] is True
        assert rec["dismissed_finding_ids"] == [0, 2, 4]
        # The D.2a.5 fields MUST still be present and false — the flags
        # are parallel, not overloaded.
        assert rec["remediation_tag"] is False
        assert rec["disagreement_tag"] is False

    def test_narrow_flags_absent_by_default(
        self, tmp_git_repo: Path
    ) -> None:
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "First task",
            "--diff-summary", "plain first pass",
            "--reviewer", "none",
            "--reviewer-verdict", "",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr
        body = subprocess.run(
            ["git", "log", "-1", "--pretty=format:%B"],
            cwd=tmp_git_repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "[narrow-remediation]" not in body
        assert "[disagreement:" not in body

        log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        rec = json.loads(
            [ln for ln in log_path.read_text(encoding="utf-8").splitlines()
             if '"commit_done"' in ln][-1]
        )
        assert rec["narrow_remediation_tag"] is False
        assert rec["dismissed_finding_ids"] == []


class TestNarrowRemediationArgparseConstraints:
    """V10 — commit-task argparse enforces all four constraints with
    argparse errors (exit code 2, usage banner on stderr):
        (a) --narrow-remediation-tag XOR --remediation-tag
        (b) --dismissed-finding-ids XOR --disagreement-tag
        (c) --dismissed-finding-ids requires --narrow-remediation-tag
        (d) --narrow-remediation-tag requires non-empty
            --dismissed-finding-ids
    Catches operator error on the CLI before any commit is written.
    """

    COMMON = [
        "commit-task",
        "--plan-file", "docs/plans/sample.md",
        "--task-id", "001",
        "--run-id", "R1",
        "--files", "src/foo.py",
        "--title", "t",
        "--diff-summary", "d",
        "--reviewer", "none",
        "--reviewer-verdict", "",
    ]

    def test_a_narrow_and_full_remediation_tags_mutually_exclusive(
        self, tmp_git_repo: Path
    ) -> None:
        cp = _run(
            *self.COMMON,
            "--remediation-tag",
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "1",
            "--json",
            cwd=tmp_git_repo,
        )
        # argparse mutually-exclusive violation exits with code 2.
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "not allowed with" in cp.stderr or "mutually exclusive" in cp.stderr

    def test_b_dismissed_ids_and_disagreement_tag_mutually_exclusive(
        self, tmp_git_repo: Path
    ) -> None:
        cp = _run(
            *self.COMMON,
            "--disagreement-tag",
            "--dismissed-finding-ids", "1,2",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "not allowed with" in cp.stderr or "mutually exclusive" in cp.stderr

    def test_c_dismissed_ids_requires_narrow_remediation_tag(
        self, tmp_git_repo: Path
    ) -> None:
        cp = _run(
            *self.COMMON,
            "--dismissed-finding-ids", "1,2",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        # parser.error() prefaces with `error:` on stderr; both the
        # clause and the flag name appear verbatim.
        assert "--narrow-remediation-tag" in cp.stderr
        assert "--dismissed-finding-ids" in cp.stderr

    def test_d_narrow_remediation_requires_non_empty_dismissed_ids(
        self, tmp_git_repo: Path
    ) -> None:
        cp = _run(
            *self.COMMON,
            "--narrow-remediation-tag",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "--narrow-remediation-tag" in cp.stderr
        assert "--dismissed-finding-ids" in cp.stderr

    def test_d_narrow_remediation_rejects_empty_dismissed_ids(
        self, tmp_git_repo: Path
    ) -> None:
        """Whitespace-only --dismissed-finding-ids is equivalent to
        omitting it; argparse constraint (d) still fires."""
        cp = _run(
            *self.COMMON,
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "   ",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "--narrow-remediation-tag" in cp.stderr

    def test_dismissed_ids_rejects_bare_comma(
        self, tmp_git_repo: Path
    ) -> None:
        """A single comma parses to two empty tokens; argparse MUST
        reject rather than silently produce an empty trailer. The plan
        file must remain untouched on argparse failure."""
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan_before = plan.read_text(encoding="utf-8")
        cp = _run(
            *self.COMMON,
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", ",",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "--dismissed-finding-ids" in cp.stderr
        assert "empty" in cp.stderr
        # No plan mutation — argparse exits before cmd_commit_task runs.
        assert plan.read_text(encoding="utf-8") == plan_before

    def test_dismissed_ids_rejects_internal_empty_token(
        self, tmp_git_repo: Path
    ) -> None:
        """`1,,3` is a malformed operator entry; silently dropping the
        empty middle token would emit `[disagreement: 1,3]` and hide
        the mistake. argparse MUST reject with exit 2."""
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan_before = plan.read_text(encoding="utf-8")
        cp = _run(
            *self.COMMON,
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "1,,3",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "--dismissed-finding-ids" in cp.stderr
        assert "empty" in cp.stderr
        assert plan.read_text(encoding="utf-8") == plan_before

    def test_dismissed_ids_rejects_non_integer_token(
        self, tmp_git_repo: Path
    ) -> None:
        """Non-integer tokens (`1,x`) also fail at argparse, not in the
        commit handler, so the plan file stays untouched."""
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan_before = plan.read_text(encoding="utf-8")
        cp = _run(
            *self.COMMON,
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "1,x",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 2, (cp.stdout, cp.stderr)
        assert "--dismissed-finding-ids" in cp.stderr
        assert "'x'" in cp.stderr
        assert plan.read_text(encoding="utf-8") == plan_before

    def test_valid_combination_passes(self, tmp_git_repo: Path) -> None:
        """Sanity check — the happy path must still work after all the
        mutual-exclusion groups and post-parse checks are in place."""
        (tmp_git_repo / "src" / "foo.py").write_text("x = 2\n", encoding="utf-8")
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        cp = _run(
            "commit-task",
            "--plan-file", str(plan),
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "t",
            "--diff-summary", "d",
            "--reviewer", "codex",
            "--reviewer-verdict", "clean",
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "1,3",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)


class TestPhaseBNarrowRemediationTemplate:
    """V8 — dispatch-templates.md gains a Phase B-narrow-remediation
    template. The orchestrator's D.2a.6 dispatch reads the template
    anchors here; drift would silently break the retry wiring.
    """

    TEMPLATES = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "dispatch-templates.md"
    )

    def _narrow_section(self) -> str:
        text = self.TEMPLATES.read_text(encoding="utf-8")
        start = text.find("## Phase B-narrow-remediation")
        assert start >= 0, (
            "Phase B-narrow-remediation heading missing from "
            "dispatch-templates.md"
        )
        body = text[start:]
        next_section = body.find("\n## ", 1)
        if next_section >= 0:
            body = body[:next_section]
        return body

    def test_template_heading_and_plan_remediator_dispatch(self) -> None:
        body = self._narrow_section()
        # Subagent type is the new plan-remediator role, not plan-implementer.
        assert "plan-remediator" in body
        # Model stays opus per TASK-016B frontmatter.
        assert "opus" in body

    def test_template_embeds_the_three_json_slots(self) -> None:
        body = self._narrow_section()
        assert "load_bearing_findings_json" in body
        assert "dismissed_findings_json" in body
        assert "d5_summary" in body

    def test_template_labels_dismissed_as_do_not_fix(self) -> None:
        body = self._narrow_section()
        lowered = body.lower()
        # The dismissed block MUST carry the "DO NOT fix" marker so the
        # remediator cannot silently act on the dismissed subset.
        assert "do not fix" in lowered
        # The "context only" phrasing is the canonical complement.
        assert "context only" in lowered or "context-only" in lowered

    def test_template_retains_fix_narrowly_guidance(self) -> None:
        body = self._narrow_section()
        # The file:line scope rule is the structural enforcement, but
        # the prompt-level hint is preserved for the same reasons
        # dispatch-templates.md:209 keeps it on D.2a.5.
        assert "Fix narrowly" in body or "fix narrowly" in body

    def test_template_has_no_agent_tool_constraint(self) -> None:
        body = self._narrow_section()
        assert "You do NOT have the Agent tool" in body


class TestD2a6SkillMdSection:
    """V11 — SKILL.md grows a §D.2a.6 section parallel to §D.2a.5 with
    the one-attempt bounding, route-on-retry-success, the two
    awaiting-user branches with distinct stage labels, and the
    binding-mode exemption.
    """

    SKILL = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "SKILL.md"
    )

    def _d2a6_section(self) -> str:
        text = self.SKILL.read_text(encoding="utf-8")
        start = text.find("#### D.2a.6")
        assert start >= 0, "SKILL.md missing #### D.2a.6 heading"
        body = text[start:]
        next_section = body.find("\n#### ", 1)
        if next_section >= 0:
            body = body[:next_section]
        return body

    def test_section_exists_and_names_plan_remediator(self) -> None:
        body = self._d2a6_section()
        assert "plan-remediator" in body

    def test_section_bounds_one_attempt(self) -> None:
        body = self._d2a6_section()
        # The "one attempt" constraint must be explicit so the
        # orchestrator does not loop the retry.
        assert "one attempt" in body.lower() or "One attempt" in body

    def test_section_re_runs_d1_binding(self) -> None:
        body = self._d2a6_section()
        # On retry success, the re-review is binding.
        assert "D.1" in body
        assert "binding" in body.lower()

    def test_section_has_both_awaiting_user_stage_labels(self) -> None:
        body = self._d2a6_section()
        assert "post_narrow_remediation_review" in body
        assert "post_narrow_remediation_implement" in body

    def test_section_lists_narrow_remediation_events(self) -> None:
        body = self._d2a6_section()
        assert "narrow_remediation_start" in body
        assert "narrow_remediation_done" in body

    def test_section_documents_binding_mode_exemption(self) -> None:
        body = self._d2a6_section()
        # --codex-review-binding skips D.2a.6 entirely per the
        # non-goals in the plan's Scoped Context.
        assert "codex-review-binding" in body
        # The exemption must be unambiguous — either explicit "skip" or
        # "NO D.2a.6".
        assert (
            "skip" in body.lower()
            or "NO D.2a.6" in body
            or "not entered" in body.lower()
        )

    def test_section_commit_uses_narrow_flags(self) -> None:
        body = self._d2a6_section()
        assert "--narrow-remediation-tag" in body
        assert "--dismissed-finding-ids" in body


class TestD5AdjudicationFollowups:
    """Non-blocking follow-ups from D.5 on TASK-016A, addressed within
    TASK-016C because the same file is already in scope. These are
    refinements to the partial-agreement validator, not new surface.
    """

    def _run_parser(
        self, payload: dict | str, *, codex_findings_count: int,
    ) -> subprocess.CompletedProcess:
        stdin = (
            payload if isinstance(payload, str) else json.dumps(payload)
        )
        return subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-d5-adjudication", "--stdin",
                "--codex-findings-count", str(codex_findings_count),
                "--json",
            ],
            input=stdin,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def test_rejects_missing_summary_on_partial_agreement(self) -> None:
        """Follow-up #1 — partial-agreement MUST carry a non-empty
        summary; the remediator template forwards it as the D.5
        justification."""
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0],
                "dismissed": [1],
                # summary deliberately omitted
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-missing-summary" in codes

    def test_rejects_empty_summary_on_partial_agreement(self) -> None:
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0],
                "dismissed": [1],
                "summary": "   ",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-missing-summary" in codes

    def test_non_partial_agreement_tolerates_missing_summary(self) -> None:
        """The follow-up applies only to partial-agreement. Other
        verdicts were already passing through without a summary
        requirement and MUST continue to do so."""
        cp = self._run_parser(
            {"verdict": "ship"},
            codex_findings_count=2,
        )
        assert cp.returncode == 0, cp.stderr

    def test_bucket_type_error_short_circuits_split_validation(self) -> None:
        """Follow-up #2 — a bucket with a non-integer element MUST NOT
        stack a spurious `partial-agreement-invalid-split` on top of
        the underlying `invalid-type` code."""
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": ["0"],  # non-integer — type error
                "dismissed": [1],
                "summary": "indices must be integers",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-type" in codes
        # The split-validation code MUST NOT fire when the bucket is
        # structurally broken — it would be a spurious stacking error.
        assert "partial-agreement-invalid-split" not in codes

    def test_rejects_intra_bucket_duplicate_indices(self) -> None:
        """Follow-up #2 — a bucket with a repeated index (e.g., [0, 0])
        is a contract violation; the remediator would see the same
        finding twice."""
        cp = self._run_parser(
            {
                "verdict": "partial-agreement",
                "load_bearing": [0, 0],
                "dismissed": [1],
                "summary": "duplicate index 0",
            },
            codex_findings_count=2,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "partial-agreement-invalid-split" in codes
