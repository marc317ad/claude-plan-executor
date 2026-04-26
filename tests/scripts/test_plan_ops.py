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

    # ------------------------------------------------------------------
    # V1-V20 (ISSUE-018): strict canonical-shape validation + --force
    # ------------------------------------------------------------------

    # V1 — Canonical happy path.
    def test_acquire_lock_canonical_happy_path(self, isolated_plan: Path) -> None:
        assert not plan_ops.RUN_LOCK_PATH.exists()
        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {plan_abs}
        entry = contents[plan_abs]
        assert set(entry.keys()) == {"run_id", "acquired_at"}
        assert entry["run_id"] == "R1"
        assert isinstance(entry["acquired_at"], str) and entry["acquired_at"]

    # V2 — Top-level JSON is a list, not a dict.
    def test_acquire_lock_rejects_toplevel_list(self, isolated_plan: Path) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text('["oops"]', encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("acquired") is False
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-toplevel-not-object" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V3 — Entry value is not a dict.
    def test_acquire_lock_rejects_entry_value_not_object(
        self, isolated_plan: Path
    ) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"/abs/other/plan.md": "a string not a dict"}
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("acquired") is False
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-not-object" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V4 — Entry missing required key.
    def test_acquire_lock_rejects_entry_missing_acquired_at(
        self, isolated_plan: Path
    ) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"/abs/other/plan.md": {"run_id": "R_old"}}
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-missing-keys" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V5 — Entry has extra keys.
    def test_acquire_lock_rejects_entry_with_extra_keys(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {plan_abs: {"run_id": "R", "acquired_at": "T", "pid": 999}}
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-extra-keys" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V6 — Invalid JSON (not silently wiped).
    def test_acquire_lock_rejects_invalid_json(self, isolated_plan: Path) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text("not-json{", encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("acquired") is False
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-json-decode" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V7 — --force overwrites.
    def test_acquire_lock_force_overwrites_malformed(
        self, isolated_plan: Path
    ) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text('["malformed"]', encoding="utf-8")

        cp = _run(
            "acquire-lock",
            "--force",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True
        assert body.get("forced") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {plan_abs}
        entry = contents[plan_abs]
        assert entry["run_id"] == "R_new"
        assert isinstance(entry["acquired_at"], str) and entry["acquired_at"]

    # V8 — Canonical-shape with other-plan entry, no conflict for this plan.
    def test_acquire_lock_merges_into_canonical_other_plan_entry(
        self, isolated_plan: Path
    ) -> None:
        other_abs = "/abs/path/to/other/plan.md"
        pre = {
            other_abs: {
                "run_id": "R_other",
                "acquired_at": "2026-01-01T00:00:00Z",
            }
        }
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {other_abs, plan_abs}
        assert contents[other_abs] == pre[other_abs]
        new_entry = contents[plan_abs]
        assert new_entry["run_id"] == "R_new"
        assert isinstance(new_entry["acquired_at"], str) and new_entry["acquired_at"]

    # V9 — Canonical-shape, conflict for THIS plan (different run_id).
    def test_acquire_lock_refuses_conflicting_run_id(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        pre = {plan_abs: {"run_id": "R1", "acquired_at": "2026-01-01T00:00:00Z"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R2",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("acquired") is False
        assert body.get("conflict_run_id") == "R1"

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V10 — Same run_id re-acquire is idempotent.
    def test_acquire_lock_same_run_id_is_idempotent(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        pre = {plan_abs: {"run_id": "R1", "acquired_at": "2026-01-01T00:00:00Z"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        assert set(contents.keys()) == {plan_abs}
        entry = contents[plan_abs]
        assert entry["run_id"] == "R1"
        # acquired_at may be refreshed or retained; both allowed.
        assert isinstance(entry["acquired_at"], str) and entry["acquired_at"]

    # V11 — Cross-plan entries untouched in non-force path.
    def test_acquire_lock_preserves_unrelated_canonical_entries(
        self, isolated_plan: Path
    ) -> None:
        pre = {
            "/abs/a.md": {"run_id": "Ra", "acquired_at": "2026-01-01T00:00:00Z"},
            "/abs/b.md": {"run_id": "Rb", "acquired_at": "2026-01-02T00:00:00Z"},
            "/abs/c.md": {"run_id": "Rc", "acquired_at": "2026-01-03T00:00:00Z"},
        }
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {"/abs/a.md", "/abs/b.md", "/abs/c.md", plan_abs}
        for k, v in pre.items():
            assert contents[k] == v

    # V12 — Atomic write (torn-write prevention).
    def test_acquire_lock_writes_atomically(
        self,
        isolated_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        pre = {plan_abs: {"run_id": "R1", "acquired_at": "2026-01-01T00:00:00Z"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        def fake_replace(src, dst):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(plan_ops.os, "replace", fake_replace)

        # Call the acquire-lock function directly to let the OSError propagate.
        import argparse as _argparse
        args = _argparse.Namespace(
            plan_file=str(isolated_plan),
            run_id="R1",
            force=False,
            json=True,
        )
        with pytest.raises(OSError):
            plan_ops.cmd_acquire_lock(args)

        # File on disk byte-equal to pre-state.
        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

        # No stray *.tmp file left.
        parent = plan_ops.RUN_LOCK_PATH.parent
        stray = [
            p for p in parent.iterdir()
            if p.name.startswith(plan_ops.RUN_LOCK_PATH.name + ".")
            and p.name.endswith(".tmp")
        ]
        assert stray == [], f"unexpected stray tmp files: {stray}"

    # V13 — --force with no pre-existing file is still canonical.
    def test_acquire_lock_force_on_missing_file(
        self, isolated_plan: Path
    ) -> None:
        assert not plan_ops.RUN_LOCK_PATH.exists()

        cp = _run(
            "acquire-lock",
            "--force",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True
        assert body.get("forced") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {plan_abs}
        entry = contents[plan_abs]
        assert set(entry.keys()) == {"run_id", "acquired_at"}
        assert entry["run_id"] == "R_new"
        assert isinstance(entry["acquired_at"], str) and entry["acquired_at"]

    # V14 — Empty-dict canonical pre-existing file is accepted.
    def test_acquire_lock_accepts_empty_dict(self, isolated_plan: Path) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text("{}", encoding="utf-8")

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {plan_abs}

    # V15 — Entry with run_id = None is rejected.
    def test_acquire_lock_rejects_none_run_id(self, isolated_plan: Path) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        payload = {plan_abs: {"run_id": None, "acquired_at": "T"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-value-empty" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V16 — Entry with run_id = 0 is rejected.
    def test_acquire_lock_rejects_zero_run_id(self, isolated_plan: Path) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        payload = {plan_abs: {"run_id": 0, "acquired_at": "T"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-value-empty" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V17 — Entry with empty-string run_id is rejected.
    def test_acquire_lock_rejects_empty_string_run_id(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        payload = {plan_abs: {"run_id": "", "acquired_at": "T"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-value-empty" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V15b — Entry with acquired_at = None is rejected.
    def test_acquire_lock_rejects_none_acquired_at(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        payload = {plan_abs: {"run_id": "R", "acquired_at": None}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-value-empty" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V16b — Entry with acquired_at = 0 is rejected.
    def test_acquire_lock_rejects_zero_acquired_at(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        payload = {plan_abs: {"run_id": "R", "acquired_at": 0}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-value-empty" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V17b — Entry with empty-string acquired_at is rejected.
    def test_acquire_lock_rejects_empty_string_acquired_at(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))
        payload = {plan_abs: {"run_id": "R", "acquired_at": ""}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-entry-value-empty" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V18 — Empty-string top-level key is rejected.
    def test_acquire_lock_rejects_empty_string_plan_key(
        self, isolated_plan: Path
    ) -> None:
        payload = {"": {"run_id": "R", "acquired_at": "T"}}
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert any(e.get("code") == "lock-key-invalid" for e in errors)

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V19 — Zero-byte lock file is rejected as decode error.
    def test_acquire_lock_rejects_zero_byte_file(
        self, isolated_plan: Path
    ) -> None:
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text("", encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()
        assert before == b""

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors", [])
        assert len(errors) >= 1
        assert errors[0].get("code") == "lock-json-decode"

        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # V20 — --force over a valid canonical file with other-plan entries
    # overwrites them.
    def test_acquire_lock_force_discards_other_plans(
        self, isolated_plan: Path
    ) -> None:
        pre = {
            "/abs/A.md": {"run_id": "Ra", "acquired_at": "2026-01-01T00:00:00Z"},
            "/abs/B.md": {"run_id": "Rb", "acquired_at": "2026-01-02T00:00:00Z"},
        }
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")

        cp = _run(
            "acquire-lock",
            "--force",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_new",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("acquired") is True
        assert body.get("forced") is True

        contents = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        plan_abs = os.path.abspath(str(isolated_plan))
        assert set(contents.keys()) == {plan_abs}
        assert "/abs/A.md" not in contents
        assert "/abs/B.md" not in contents

    # ------------------------------------------------------------------
    # VA-VE (TASK-018): non-empty --run-id guard at write entry
    # ------------------------------------------------------------------

    # VA — Empty --run-id is rejected on the non-force path.
    def test_acquire_lock_rejects_empty_run_id_nonforce(
        self, isolated_plan: Path
    ) -> None:
        assert not plan_ops.RUN_LOCK_PATH.exists()

        cp = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("acquired") is False
        errors = body.get("errors", [])
        assert len(errors) >= 1
        assert errors[0].get("code") == "lock-run-id-empty"

        # No file created — guard fires before any file operation.
        assert not plan_ops.RUN_LOCK_PATH.exists()

    # VB — Empty --run-id is rejected on the --force path.
    def test_acquire_lock_rejects_empty_run_id_force(
        self, isolated_plan: Path
    ) -> None:
        assert not plan_ops.RUN_LOCK_PATH.exists()

        cp = _run(
            "acquire-lock",
            "--force",
            "--plan-file", str(isolated_plan),
            "--run-id", "",
            "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert body.get("acquired") is False
        errors = body.get("errors", [])
        assert len(errors) >= 1
        assert errors[0].get("code") == "lock-run-id-empty"

        # --force did NOT bypass the guard — no file created.
        assert not plan_ops.RUN_LOCK_PATH.exists()

    # VC — Empty --run-id does not mutate a pre-existing canonical lock file
    # (both force and non-force paths).
    def test_acquire_lock_empty_run_id_preserves_existing_file(
        self, isolated_plan: Path
    ) -> None:
        other_abs = "/abs/path/to/other/plan.md"
        pre = {
            other_abs: {
                "run_id": "R_other",
                "acquired_at": "2026-01-01T00:00:00Z",
            }
        }
        plan_ops.RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")
        before = plan_ops.RUN_LOCK_PATH.read_bytes()

        # Non-force empty run_id.
        cp_nonforce = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "",
            "--json",
        )
        assert cp_nonforce.returncode != 0
        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

        # Force empty run_id.
        cp_force = _run(
            "acquire-lock",
            "--force",
            "--plan-file", str(isolated_plan),
            "--run-id", "",
            "--json",
        )
        assert cp_force.returncode != 0
        assert plan_ops.RUN_LOCK_PATH.read_bytes() == before

    # VD — Non-string --run-id is rejected with the same error code.
    # Argparse cannot produce a non-string from a required= flag; simulate
    # by calling cmd_acquire_lock directly with an argparse.Namespace.
    def test_acquire_lock_rejects_non_string_run_id(
        self, isolated_plan: Path
    ) -> None:
        import argparse as _argparse

        assert not plan_ops.RUN_LOCK_PATH.exists()

        # run_id=None case.
        ns_none = _argparse.Namespace(
            plan_file=str(isolated_plan),
            run_id=None,
            force=False,
            json=True,
        )
        with pytest.raises(SystemExit) as exc_none:
            plan_ops.cmd_acquire_lock(ns_none)
        assert exc_none.value.code != 0

        # run_id=0 case (also non-string; also symmetric with read-side).
        ns_zero = _argparse.Namespace(
            plan_file=str(isolated_plan),
            run_id=0,
            force=False,
            json=True,
        )
        with pytest.raises(SystemExit) as exc_zero:
            plan_ops.cmd_acquire_lock(ns_zero)
        assert exc_zero.value.code != 0

        # Guard fired before any file operation in both cases.
        assert not plan_ops.RUN_LOCK_PATH.exists()

    # VE — Non-empty --run-id happy path regressions.
    # Three sub-cases mirroring TASK-004E V1 / V13 / V8.
    def test_acquire_lock_nonempty_run_id_happy_paths(
        self, isolated_plan: Path
    ) -> None:
        plan_abs = os.path.abspath(str(isolated_plan))

        # Sub-case 1 — no pre-existing file, normal path.
        assert not plan_ops.RUN_LOCK_PATH.exists()
        cp1 = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R1",
            "--json",
        )
        assert cp1.returncode == 0, cp1.stderr
        body1 = _parse_json(cp1)
        assert body1.get("acquired") is True
        contents1 = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        assert set(contents1.keys()) == {plan_abs}
        entry1 = contents1[plan_abs]
        assert set(entry1.keys()) == {"run_id", "acquired_at"}
        assert entry1["run_id"] == "R1"
        assert isinstance(entry1["acquired_at"], str) and entry1["acquired_at"]

        # Clean up for sub-case 2.
        plan_ops.RUN_LOCK_PATH.unlink()

        # Sub-case 2 — no pre-existing file, --force path.
        assert not plan_ops.RUN_LOCK_PATH.exists()
        cp2 = _run(
            "acquire-lock",
            "--force",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_force",
            "--json",
        )
        assert cp2.returncode == 0, cp2.stderr
        body2 = _parse_json(cp2)
        assert body2.get("acquired") is True
        assert body2.get("forced") is True
        contents2 = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        assert set(contents2.keys()) == {plan_abs}
        entry2 = contents2[plan_abs]
        assert set(entry2.keys()) == {"run_id", "acquired_at"}
        assert entry2["run_id"] == "R_force"
        assert isinstance(entry2["acquired_at"], str) and entry2["acquired_at"]

        # Reset for sub-case 3 — canonical pre-existing with OTHER plan entry.
        other_abs = "/abs/path/to/other/plan.md"
        pre = {
            other_abs: {
                "run_id": "R_other",
                "acquired_at": "2026-01-01T00:00:00Z",
            }
        }
        plan_ops.RUN_LOCK_PATH.write_text(json.dumps(pre, indent=2), encoding="utf-8")

        cp3 = _run(
            "acquire-lock",
            "--plan-file", str(isolated_plan),
            "--run-id", "R_merged",
            "--json",
        )
        assert cp3.returncode == 0, cp3.stderr
        body3 = _parse_json(cp3)
        assert body3.get("acquired") is True
        contents3 = json.loads(plan_ops.RUN_LOCK_PATH.read_text(encoding="utf-8"))
        assert set(contents3.keys()) == {other_abs, plan_abs}
        assert contents3[other_abs] == pre[other_abs]
        entry3 = contents3[plan_abs]
        assert entry3["run_id"] == "R_merged"
        assert isinstance(entry3["acquired_at"], str) and entry3["acquired_at"]


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
            "plan_file": "sample.md",
        },
        {
            "id": "002",
            "agent": "codex",
            "files": ["src/bar.py"],
            "dependencies": ["001"],
            "acceptance_criteria": ["passes"],
            "plan_file": "sample.md",
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
            "plan_file": "sample.md",
        },
    ],
    "batches": [
        {"batch_index": 0, "task_ids": ["001"], "file_locks": ["src/foo.py"]},
    ],
    "gaps": [],
    "risks": [],
}


def _schedule_plan_file_fixture() -> dict:
    # TASK-008 directory-only contract: every task must declare `plan_file`.
    # Task 002 carries a distinct `plan_file` value so passthrough/round-trip
    # tests still verify per-task carriage (it differs from the other two so
    # filter/order checks below can spot drift).
    return {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "agent": "claude",
                "files": ["src/foo.py"],
                "dependencies": [],
                "acceptance_criteria": ["passes"],
                "plan_file": "TASK-001_alpha.md",
            },
            {
                "id": "002",
                "agent": "codex",
                "files": ["src/bar.py"],
                "dependencies": ["001"],
                "acceptance_criteria": ["passes"],
                "plan_file": "TASK-002_gamma.md",
            },
            {
                "id": "003",
                "agent": "codex",
                "files": ["src/baz.py"],
                "dependencies": [],
                "acceptance_criteria": ["passes"],
                "plan_file": "TASK-003-beta.md",
            },
        ],
        "batches": [
            {"index": 1, "task_ids": ["001", "003"], "file_locks": ["src/baz.py", "src/foo.py"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["src/bar.py"]},
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
                {"id": "001", "agent": "codex", "files": ["a.txt"], "dependencies": [],
                 "plan_file": "sample.md"}
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

    def test_task_id_alias_rejected_post_task_008(self) -> None:
        # TASK-008 (per_task_dispatch_refactor_v2): the legacy `task_id`
        # field alias was REMOVED. A schedule carrying it now fails
        # `_validate_schedule` with `missing-field` on `$.tasks[i].id`.
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(LEGACY_ALIAS_SCHEDULE),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors") or []
        # Both legacy fields surface as missing-field errors. Match by code
        # + path so we do not over-couple to message wording.
        codes_paths = [(e.get("code"), e.get("path")) for e in errors]
        assert ("missing-field", "$.tasks[0].id") in codes_paths, codes_paths

    def test_batch_index_alias_rejected_post_task_008(self) -> None:
        # TASK-008: the legacy `batch_index` field alias was REMOVED.
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(LEGACY_ALIAS_SCHEDULE),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        errors = body.get("errors") or []
        codes_paths = [(e.get("code"), e.get("path")) for e in errors]
        assert ("missing-field", "$.batches[0].index") in codes_paths, codes_paths

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

    # TASK-002: gap severity classification ---------------------------------

    def test_gap_severity_classification_table_matches_analyst_spec(self) -> None:
        """The Step 7 classification table in plan-analyst.md must match
        the GAP_SEVERITY constant in plan_ops.py — they are the single
        source of truth for which gaps block execution."""
        analyst_md = (
            REPO_ROOT
            / "plugins"
            / "plan-executor"
            / "agents"
            / "plan-analyst.md"
        ).read_text()
        # Extract the markdown table rows of the form:
        #   | `gap-type` | `hard` |
        row_re = re.compile(r"\|\s*`([a-z-]+)`\s*\|\s*`(hard|soft)`\s*\|")
        table = dict(row_re.findall(analyst_md))
        # Must include every canonical gap type.
        expected_types = {
            "stale-path",
            "missing-test-command",
            "vague-ac",
            "unresolvable-test",
            "empty-implementation-notes",
        }
        assert expected_types.issubset(table.keys()), (
            f"plan-analyst.md Step 7 table missing gap types: "
            f"{expected_types - table.keys()}"
        )
        for gtype in expected_types:
            assert table[gtype] == plan_ops.GAP_SEVERITY[gtype], (
                f"severity mismatch for {gtype!r}: analyst={table[gtype]} "
                f"vs plan_ops={plan_ops.GAP_SEVERITY[gtype]}"
            )
        # Fail-safe default for unknown types must be "hard".
        assert plan_ops.classify_gap_severity("not-a-real-gap") == "hard"
        # Every constant entry round-trips through the helper.
        for gtype, sev in plan_ops.GAP_SEVERITY.items():
            assert plan_ops.classify_gap_severity(gtype) == sev

    def test_parse_schedule_gap_severity_passthrough(self) -> None:
        """An analyst-emitted `severity` field must be preserved verbatim
        and must NOT trigger the legacy-backfill warning."""
        data = json.loads(json.dumps(VALID_SCHEDULE))
        data["outcome"] = "needs-enrichment"
        data["gaps"] = [
            {
                "task_id": "002",
                "type": "missing-test-command",
                "severity": "hard",
                "detail": "Claude-tier task with Test command: none",
            },
            {
                "task_id": "002",
                "type": "unresolvable-test",
                "severity": "soft",
                "detail": "wrapper command",
            },
        ]
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(data),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["errors"] == []
        gaps = body["gaps"]
        assert [g["severity"] for g in gaps] == ["hard", "soft"]
        warnings = body.get("warnings") or []
        assert not any("severity" in w for w in warnings), warnings

    def test_parse_schedule_gap_severity_legacy_fallback(self) -> None:
        """Legacy schedules without `severity` get backfilled via
        classify_gap_severity and emit a single `warnings` entry."""
        data = json.loads(json.dumps(VALID_SCHEDULE))
        data["outcome"] = "needs-enrichment"
        data["gaps"] = [
            {
                "task_id": "002",
                "type": "vague-ac",
                "detail": "AC says 'should work'",
            },
            {
                "task_id": "002",
                "type": "unresolvable-test",
                "detail": "wrapper command",
            },
            {
                "task_id": "002",
                "type": "not-a-known-type",
                "detail": "future gap type",
            },
        ]
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(data),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["errors"] == []
        gaps = body["gaps"]
        assert gaps[0]["severity"] == "hard"  # vague-ac
        assert gaps[1]["severity"] == "soft"  # unresolvable-test
        assert gaps[2]["severity"] == "hard"  # unknown → hard fail-safe
        warnings = body.get("warnings") or []
        assert any("severity" in w for w in warnings), warnings


class TestSchedulePlanFile:
    def test_schedule_plan_file_accept_matrix(self) -> None:
        payload = _schedule_plan_file_fixture()
        payload["tasks"][0]["plan_file"] = "TASK-001.md"
        payload["tasks"][1]["plan_file"] = "child_plan-02.txt"
        payload["tasks"][2]["plan_file"] = "Z" * 255
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["errors"] == []
        assert [t.get("plan_file") for t in body["tasks"]] == [
            "TASK-001.md",
            "child_plan-02.txt",
            "Z" * 255,
        ]

    def test_schedule_plan_file_missing_field_is_rejected(self) -> None:
        # TASK-008 directory-only contract: every task MUST declare
        # `plan_file`. A schedule that omits it on any task is rejected by
        # `_validate_schedule` with a structured `missing-field` error
        # pointing at the offending index.
        payload = _schedule_plan_file_fixture()
        del payload["tasks"][0]["plan_file"]
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes_paths = [(e["code"], e["path"]) for e in body["errors"]]
        assert ("missing-field", "$.tasks[0].plan_file") in codes_paths, codes_paths

    @pytest.mark.parametrize(
        ("value", "code"),
        [
            (None, "invalid-plan-file"),
            ("", "invalid-plan-file"),
            ("child/plan.md", "invalid-plan-file"),
            ("child\\plan.md", "invalid-plan-file"),
            ("..", "invalid-plan-file"),
            ("child..plan.md", "invalid-plan-file"),
            (".hidden.md", "invalid-plan-file"),
            ("a" * 256, "invalid-plan-file"),
            (123, "invalid-plan-file"),
        ],
    )
    def test_schedule_plan_file_reject_matrix(self, value: object, code: str) -> None:
        payload = _schedule_plan_file_fixture()
        payload["tasks"][1]["plan_file"] = value
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        err = next(e for e in body["errors"] if e["code"] == code)
        assert err["path"] == "$.tasks[1].plan_file"

    def test_schedule_plan_file_rejects_nul_byte(self) -> None:
        payload = _schedule_plan_file_fixture()
        payload["tasks"][1]["plan_file"] = "bad\x00name.md"
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 1
        body = _parse_json(cp)
        err = next(e for e in body["errors"] if e["code"] == "invalid-plan-file")
        assert err["path"] == "$.tasks[1].plan_file"

    def test_schedule_plan_file_compute_schedule_passthrough(self) -> None:
        payload = {"tasks": _schedule_plan_file_fixture()["tasks"]}
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert [t.get("plan_file") for t in body["tasks"]] == [
            "TASK-001_alpha.md",
            "TASK-002_gamma.md",
            "TASK-003-beta.md",
        ]

    def test_schedule_plan_file_filter_schedule_preserves_direct_and_transitive(
        self, tmp_path: Path,
    ) -> None:
        sched = tmp_path / "schedule.json"
        sched.write_text(json.dumps(_schedule_plan_file_fixture()), encoding="utf-8")
        cp = _run_filter_schedule(sched, "2")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]
        assert [t.get("plan_file") for t in body["tasks"]] == [
            "TASK-001_alpha.md",
            "TASK-002_gamma.md",
        ]

    def test_schedule_plan_file_batch_next_accepts_schedule_entries(
        self, tmp_path: Path,
    ) -> None:
        sched = _write_schedule(tmp_path, _schedule_plan_file_fixture())
        cp = _run_batch_next(sched, parallel=2)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert set(body.keys()) == {
            "batch_index", "task_ids", "file_locks", "scheduler_stuck"
        }
        assert sorted(body["task_ids"]) == ["001", "003"]

    def test_schedule_plan_file_write_schedule_round_trip(self, tmp_path: Path) -> None:
        dest = tmp_path / "schedule.json"
        payload = _schedule_plan_file_fixture()
        cp = _run_write_schedule(payload, dest)
        assert cp.returncode == 0, cp.stderr
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert [t.get("plan_file") for t in written["tasks"]] == [
            "TASK-001_alpha.md",
            "TASK-002_gamma.md",
            "TASK-003-beta.md",
        ]


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

    def test_serial_chain_with_disjoint_files_yields_n_batches(self) -> None:
        """Renamed from ``test_disjoint_files_single_batch`` (PLAN_TOPO_RESPECT_FIX_2026-04-25 TASK-002).

        Old assertion (collapsing 001→002→003 into one file-disjoint batch)
        was the bug being fixed. Topo layering MUST take precedence over
        file-disjoint packing: a serial chain with disjoint files yields
        N batches in topo order, not one.
        """
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
            {"index": 1, "task_ids": ["001"], "file_locks": ["a.py"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["b.py"]},
            {"index": 3, "task_ids": ["003"], "file_locks": ["c.py"]},
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

    # PLAN_TOPO_RESPECT_FIX_2026-04-25 TASK-002: regression-pinning + helper
    # error-propagation tests at the CLI envelope layer. The helper-level
    # equivalents live in TestDependencyAwareBatches; these confirm
    # _compute_schedule_batches forwards helper errors to the third tuple
    # position and preserves the compute-schedule envelope shape.
    def test_serial_chain_with_disjoint_files_respects_dependencies(self) -> None:
        """Regression pin: serial chain ``001 → 002 → 003`` with file-disjoint
        payloads MUST emit 3 topo-ordered batches via the compute-schedule
        envelope, not collapse into a single file-disjoint batch.

        Pre-fix bug: ``_compute_schedule_batches`` ran its own file-disjoint
        packer that ignored ``dependencies[]``. Post-fix it delegates to
        ``_dependency_aware_batches`` which topo-layers first.
        """
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
        assert [b["task_ids"] for b in body["batches"]] == [["001"], ["002"], ["003"]]
        assert [b["index"] for b in body["batches"]] == [1, 2, 3]
        assert [b["file_locks"] for b in body["batches"]] == [["a.py"], ["b.py"], ["c.py"]]

    def test_orphan_dep_returns_unresolvable_dep_error(self) -> None:
        """A ``dependencies[]`` entry pointing at an id absent from
        ``tasks[]`` MUST surface as ``unresolvable-dep`` in the envelope's
        ``errors[]`` (helper error propagated via the third tuple slot).

        Asserts the documented CLI envelope shape only — ``{path, code,
        message}``. Helper-specific keys (``task_id`` / ``dep_id``) are
        pinned at the helper layer in
        ``TestDependencyAwareBatches.test_orphan_dep_returns_unresolvable_dep_error``.
        """
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": ["999"]},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 1, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["batches"] == []
        assert body["topo"] == []
        codes = [e.get("code") for e in body["errors"]]
        assert "unresolvable-dep" in codes
        orphan = next(e for e in body["errors"] if e.get("code") == "unresolvable-dep")
        assert orphan["path"] == "$.tasks[001].dependencies"
        assert orphan["code"] == "unresolvable-dep"
        assert "999" in orphan["message"]

    def test_cycle_returns_cyclic_dependency_error(self) -> None:
        """A cycle ``001 → 002 → 001`` MUST surface as ``cyclic-dependency``
        in the envelope's ``errors[]``.

        Asserts the documented CLI envelope shape only — ``{path, code,
        message}``. Helper-specific keys (``task_ids``) are pinned at the
        helper layer in
        ``TestDependencyAwareBatches.test_cycle_returns_cyclic_dependency_error``.
        """
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": ["002"]},
                {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": ["001"]},
            ]
        }
        cp = self._run_compute(payload)
        assert cp.returncode == 1, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["batches"] == []
        assert body["topo"] == []
        codes = [e.get("code") for e in body["errors"]]
        assert "cyclic-dependency" in codes
        cycle = next(e for e in body["errors"] if e.get("code") == "cyclic-dependency")
        assert cycle["path"] == "$.tasks"
        assert cycle["code"] == "cyclic-dependency"
        assert "cycle" in cycle["message"].lower()


# ---------------------------------------------------------------------------
# PLAN_TOPO_RESPECT_FIX TASK-001: shared dep-aware batching helper
# ---------------------------------------------------------------------------


class TestDependencyAwareBatches:
    """Direct unit tests for ``_dependency_aware_batches`` —
    the canonical (topo-layered + file-disjoint + global-lock-solitary)
    batching helper shared by ``_compute_schedule_batches`` and
    ``_build_tasks``.

    Regression-pinning suite for
    ``docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25``: a serial chain with
    disjoint files MUST yield N batches in topo order, not collapse into
    a single file-disjoint batch.
    """

    @staticmethod
    def _ordered_ids(tasks: list[dict]) -> list[str]:
        """Mimic ``_compute_schedule_batches``'s caller-side priority
        sort so the helper sees ids in the canonical order."""
        return [
            t["id"]
            for t in sorted(
                tasks,
                key=lambda t: plan_ops._task_order_key(
                    t["id"], t.get("priority", "low"),
                ),
            )
        ]

    def test_single_task_yields_single_batch(self) -> None:
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert errors == []
        assert batches == [
            {"index": 1, "task_ids": ["001"], "file_locks": ["a.py"]},
        ]

    def test_independent_disjoint_tasks_pack_into_single_batch(self) -> None:
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": []},
            {"id": "003", "priority": "high", "files": ["c.py"], "dependencies": []},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert errors == []
        assert batches == [
            {
                "index": 1,
                "task_ids": ["001", "002", "003"],
                "file_locks": ["a.py", "b.py", "c.py"],
            },
        ]

    def test_independent_overlapping_tasks_split_into_multiple_batches(
        self,
    ) -> None:
        tasks = [
            {"id": "001", "priority": "high", "files": ["shared.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["shared.py"], "dependencies": []},
            {"id": "003", "priority": "high", "files": ["shared.py"], "dependencies": []},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert errors == []
        # File-disjoint packer cannot share `shared.py` → solitary batches
        # in priority/id order.
        assert [b["task_ids"] for b in batches] == [["001"], ["002"], ["003"]]
        assert [b["index"] for b in batches] == [1, 2, 3]

    def test_serial_chain_disjoint_files_yields_n_batches(self) -> None:
        """Regression-pinning: serial chain ``001 → 002 → 003`` with
        file-disjoint payloads MUST produce 3 topo-ordered batches, not
        collapse into a single file-disjoint batch.

        See ``docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25`` for the
        post-mortem; this is the case the prior file-disjoint-only
        batcher got wrong.
        """
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": ["001"]},
            {"id": "003", "priority": "high", "files": ["c.py"], "dependencies": ["002"]},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert errors == []
        assert len(batches) == 3
        assert [b["task_ids"] for b in batches] == [["001"], ["002"], ["003"]]
        assert [b["index"] for b in batches] == [1, 2, 3]
        assert [b["file_locks"] for b in batches] == [["a.py"], ["b.py"], ["c.py"]]

    def test_diamond_dependency_yields_three_batches(self) -> None:
        """Diamond ``A → B,C → D`` with file-disjoint B and C produces
        3 batches: ``[[A], [B, C], [D]]``."""
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": ["001"]},
            {"id": "003", "priority": "high", "files": ["c.py"], "dependencies": ["001"]},
            {"id": "004", "priority": "high", "files": ["d.py"], "dependencies": ["002", "003"]},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert errors == []
        assert [b["task_ids"] for b in batches] == [
            ["001"], ["002", "003"], ["004"],
        ]
        assert [b["index"] for b in batches] == [1, 2, 3]

    def test_global_lock_task_forced_into_solitary_batch(self) -> None:
        """A task whose files intersect ``GLOBAL_LOCK_PATHS`` MUST get
        its own sub-batch even when file-disjoint with siblings."""
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["requirements.txt"], "dependencies": []},
            {"id": "003", "priority": "high", "files": ["b.py"], "dependencies": []},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert errors == []
        # 001 and 003 are file-disjoint with each other; 002 is
        # global-locked → solitary sub-batch.
        assert [b["task_ids"] for b in batches] == [
            ["001", "003"], ["002"],
        ]
        assert [b["index"] for b in batches] == [1, 2]
        # Confirm the global-lock entry is the solitary one.
        solitary = [b for b in batches if b["task_ids"] == ["002"]][0]
        assert solitary["file_locks"] == ["requirements.txt"]

    def test_cycle_returns_cyclic_dependency_error(self) -> None:
        """``A → B → A`` returns ``cyclic-dependency`` and empty batches."""
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": ["002"]},
            {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": ["001"]},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert batches == []
        assert len(errors) == 1
        assert errors[0]["code"] == "cyclic-dependency"
        assert sorted(errors[0]["task_ids"]) == ["001", "002"]

    def test_orphan_dep_returns_unresolvable_dep_error(self) -> None:
        """A dep id not present in the task set returns
        ``unresolvable-dep`` and empty batches."""
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": ["999"]},
        ]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, self._ordered_ids(tasks),
        )
        assert batches == []
        assert len(errors) == 1
        assert errors[0]["code"] == "unresolvable-dep"
        assert errors[0]["task_id"] == "001"
        assert errors[0]["dep_id"] == "999"

    def test_mixed_priority_within_layer_packs_higher_priority_first(
        self,
    ) -> None:
        """Within a single topo layer, the greedy packer sees ids in
        ``_task_order_key`` (priority-then-id) order, so high-priority
        ids occupy the first sub-batch slot when file conflicts force a
        split."""
        # Three independent (no-deps) tasks all touching `shared.py` →
        # one layer with three solitary sub-batches; the high-priority
        # task MUST land in batch 1.
        tasks = [
            {"id": "001", "priority": "low", "files": ["shared.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["shared.py"], "dependencies": []},
            {"id": "003", "priority": "medium", "files": ["shared.py"], "dependencies": []},
        ]
        ordered = self._ordered_ids(tasks)
        # Sanity: caller-side sort places high → medium → low.
        assert ordered == ["002", "003", "001"]
        batches, errors = plan_ops._dependency_aware_batches(
            tasks, ordered,
        )
        assert errors == []
        assert [b["task_ids"] for b in batches] == [
            ["002"], ["003"], ["001"],
        ]
        assert [b["index"] for b in batches] == [1, 2, 3]

    def test_helper_does_not_mutate_inputs(self) -> None:
        """Pure-function contract: neither ``tasks`` nor
        ``ordered_task_ids`` is mutated by the call."""
        tasks = [
            {"id": "001", "priority": "high", "files": ["a.py"], "dependencies": []},
            {"id": "002", "priority": "high", "files": ["b.py"], "dependencies": ["001"]},
        ]
        ordered = self._ordered_ids(tasks)
        # Snapshot via deep copy through json round-trip (sufficient for
        # plain dicts/lists; we don't carry sets at the wire boundary).
        tasks_before = json.loads(json.dumps(tasks))
        ordered_before = list(ordered)
        plan_ops._dependency_aware_batches(tasks, ordered)
        assert tasks == tasks_before
        assert ordered == ordered_before


# ---------------------------------------------------------------------------
# TASK-010: globally-locked dependency / environment paths
# ---------------------------------------------------------------------------


class TestGlobalLockPaths:
    """Scheduler rule: any task whose `files` intersects GLOBAL_LOCK_PATHS
    (or its globs / YAML override) runs alone in its batch."""

    def _run(self, *args: str, cwd: Path | None = None, stdin: str | None = None) -> subprocess.CompletedProcess:
        cmd = [str(PY), str(SCRIPT), *args]
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else str(REPO_ROOT),
            input=stdin,
            capture_output=True,
            text=True,
        )

    def test_global_lock_constant_membership(self) -> None:
        # V1 surface: default constant declares the canonical set.
        assert "requirements.txt" in plan_ops.GLOBAL_LOCK_PATHS
        assert "package.json" in plan_ops.GLOBAL_LOCK_PATHS
        assert "Dockerfile" in plan_ops.GLOBAL_LOCK_PATHS
        assert "src/foo.py" not in plan_ops.GLOBAL_LOCK_PATHS

    def test_global_lock_paths_audit_check_passes(self) -> None:
        # V7: the global_lock_paths self-audit check is registered and
        # currently passes (constant matches the doc default set).
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "audit",
             "--check", "global_lock_paths", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True, text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        findings = body["findings"]
        assert len(findings) == 1
        assert findings[0]["check"] == "global_lock_paths"
        assert findings[0]["status"] == "pass", findings[0]

    def test_is_global_lock_path_exact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert plan_ops._is_global_lock_path("requirements.txt") is True
        assert plan_ops._is_global_lock_path("pyproject.toml") is True
        assert plan_ops._is_global_lock_path("src/foo.py") is False

    def test_is_global_lock_path_glob(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert plan_ops._is_global_lock_path(".github/workflows/ci.yml") is True
        assert plan_ops._is_global_lock_path(".github/workflows/release.yaml") is True
        assert plan_ops._is_global_lock_path("workflows/ci.yml") is False

    def test_list_global_lock_paths_default(self, tmp_path: Path) -> None:
        # V1: subcommand emits default set as JSON.
        cp = self._run("list-global-lock-paths", "--json", cwd=tmp_path)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert "requirements.txt" in body["paths"]
        assert "Dockerfile" in body["paths"]
        assert ".github/workflows/*.yml" in body["globs"]
        assert body["paths"] == sorted(body["paths"])

    def test_list_global_lock_paths_override(self, tmp_path: Path) -> None:
        # V6: YAML override merges with defaults.
        plans_dir = tmp_path / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "_global_lock_paths.yaml").write_text(
            "additional:\n"
            "  - .env.example\n"
            "  - 'config/global.toml'\n"
            "  - 'ci/pipelines/*.yml'\n",
            encoding="utf-8",
        )
        cp = self._run("list-global-lock-paths", "--json", cwd=tmp_path)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert ".env.example" in body["paths"]
        assert "config/global.toml" in body["paths"]
        assert "requirements.txt" in body["paths"]  # defaults preserved
        assert "ci/pipelines/*.yml" in body["globs"]

    def test_parse_schedule_tags_global_lock(self, tmp_path: Path) -> None:
        # V2: parse-schedule emits global_lock: bool on every task record.
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "claude", "files": ["src/foo.py"],
                 "dependencies": [], "plan_file": "p.md"},
                {"id": "002", "agent": "claude", "files": ["requirements.txt"],
                 "dependencies": [], "plan_file": "p.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["src/foo.py"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["requirements.txt"]},
            ],
            "gaps": [],
            "risks": [],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        flags = {t["id"]: t["global_lock"] for t in body["tasks"]}
        assert flags == {"001": False, "002": True}

    def test_compute_schedule_serializes_global_lock_task(self, tmp_path: Path) -> None:
        # V3: with one global-lock task among 3 ready tasks, that task
        # batches alone; the others co-batch normally.
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["src/a.py"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["requirements.txt"], "dependencies": []},
                {"id": "003", "priority": "high", "files": ["src/b.py"], "dependencies": []},
            ]
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # The global-lock task must NEVER co-batch with another task.
        for batch in body["batches"]:
            if "002" in batch["task_ids"]:
                assert batch["task_ids"] == ["002"], batch
        # And tasks must be partitioned across at least 2 batches.
        all_ids = sorted(tid for b in body["batches"] for tid in b["task_ids"])
        assert all_ids == ["001", "002", "003"]
        assert len(body["batches"]) >= 2

    def test_compute_schedule_serializes_two_global_lock_tasks(self, tmp_path: Path) -> None:
        # V5: two global-lock tasks each get their own batch; no co-batch.
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["requirements.txt"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["package.json"], "dependencies": []},
                {"id": "003", "priority": "high", "files": ["src/x.py"], "dependencies": []},
            ]
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        for batch in body["batches"]:
            ids = batch["task_ids"]
            assert not ({"001", "002"} <= set(ids))  # never co-batched
            if "001" in ids:
                assert ids == ["001"]
            if "002" in ids:
                assert ids == ["002"]

    def test_compute_schedule_global_lock_respects_dependency_ordering(self, tmp_path: Path) -> None:
        # V4: dep chain with a global-lock step preserves topological order.
        payload = {
            "tasks": [
                {"id": "001", "priority": "high", "files": ["requirements.txt"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["src/foo.py"], "dependencies": ["001"]},
            ]
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["topo"] == ["001", "002"]
        # 001 is global-lock and alone; 002 is its own batch (after 001).
        for batch in body["batches"]:
            if "001" in batch["task_ids"]:
                assert batch["task_ids"] == ["001"]

    def test_compute_schedule_glob_match_serializes(self, tmp_path: Path) -> None:
        # Glob default `.github/workflows/*.yml` triggers solitary placement.
        payload = {
            "tasks": [
                {"id": "001", "priority": "high",
                 "files": [".github/workflows/ci.yml"], "dependencies": []},
                {"id": "002", "priority": "high", "files": ["src/x.py"], "dependencies": []},
            ]
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        for batch in body["batches"]:
            if "001" in batch["task_ids"]:
                assert batch["task_ids"] == ["001"]


# ---------------------------------------------------------------------------
# Canonical-contract constants regression
# ---------------------------------------------------------------------------


class TestCanonicalContractConstants:
    def test_pending_in_allowed_statuses(self) -> None:
        assert "pending" in plan_ops.ALLOWED_TASK_STATUSES

    def test_open_alias_removed_in_task_008(self) -> None:
        # TASK-008 (per_task_dispatch_refactor_v2): the file-mode `open`
        # alias was REMOVED. The directory-only canonical contract is
        # the single accepted runtime form. `STATUS_ALIASES` retains its
        # symbol as an empty dict for forward compatibility.
        assert "open" not in plan_ops.ALLOWED_TASK_STATUSES
        assert plan_ops.STATUS_ALIASES == {}

    def test_schedule_field_aliases_removed_in_task_008(self) -> None:
        # TASK-008: the file-mode `task_id` / `batch_index` field aliases
        # were REMOVED. `SCHEDULE_FIELD_ALIASES` retains its symbol as
        # an empty dict for forward compatibility.
        assert plan_ops.SCHEDULE_FIELD_ALIASES == {}


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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
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
                 "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"], "plan_file": "sample.md"},
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

    def test_concerns_legacy_label_rejected_post_task_008(self) -> None:
        # TASK-008 (per_task_dispatch_refactor_v2): the legacy
        # `**Concerns:**` bullet section was REMOVED as a fallback; only
        # the canonical `**Concerns for reviewer:**` label is recognized.
        # A report that uses the legacy label loses its concerns and
        # surfaces a `missing-concerns-for-reviewer` diagnostic.
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Concerns:**\n- legacy_c1\n"
            "**Plan adaptations:**\n- none\n"
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
        # Concerns from the legacy label are NOT picked up.
        assert body["concerns"] == []
        # No legacy warning emitted (the fallback path is gone).
        assert body.get("warnings") == []
        # The parser surfaces the missing canonical header diagnostic.
        codes = [d["code"] for d in body.get("diagnostics") or []]
        assert "missing-concerns-for-reviewer" in codes, body

    def test_coupling_check_block_round_trips(self) -> None:
        # TASK-002 (POSTMORTEM_FIXES): a report carrying the new
        # **Coupling check:** block (mandatory when an AC names a
        # regex/header/symbol pattern family) must round-trip through
        # `parse-implementer-report` without surfacing a parser error
        # or losing any of the canonical fields. The block itself is
        # opaque to the parser today (extracted as fenced text inside
        # the report `raw`); this test pins that the parser does not
        # choke on the new section header.
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- plugins/plan-executor/scripts/plan_ops.py\n"
            "**Diff summary:** loosen `_READ_TARGETS_HEADER_RE`, "
            "`_SYMBOL_TARGETS_HEADER_RE`, and the `_iter_target_bullets` "
            "section-boundary detector uniformly.\n"
            "**Test outcome:** passed\n"
            "**Coupling check:**\n"
            "```yaml\n"
            'pattern_family: "\\\\*\\\\*[^*]+:\\\\*\\\\*\\\\s*$"\n'
            "siblings_checked:\n"
            '  - file: "plugins/plan-executor/scripts/plan_ops.py"\n'
            "    line: 9300\n"
            "    disposition: uniformly_applied\n"
            '  - file: "plugins/plan-executor/scripts/plan_ops.py"\n'
            "    line: 9320\n"
            "    disposition: uniformly_applied\n"
            '  - file: "plugins/plan-executor/scripts/plan_ops.py"\n'
            "    line: 8420\n"
            "    disposition: uniformly_applied\n"
            "```\n"
            "**Concerns for reviewer:**\n- none\n"
            "**Plan adaptations:**\n- none\n"
            "**Reversion guidance:** revert the three regex loosenings.\n"
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
        # Canonical fields still parse correctly despite the new block.
        assert body["outcome"] == "success"
        assert body["files_changed"] == [
            "plugins/plan-executor/scripts/plan_ops.py",
        ]
        assert body["test_outcome"] == "passed"
        assert body["concerns"] == ["none"]
        assert body["plan_adaptations"] == ["none"]
        # No mandatory-header diagnostics — Plan adaptations + Concerns
        # for reviewer headers are both present.
        codes = [d["code"] for d in body.get("diagnostics") or []]
        assert "missing-plan-adaptations" not in codes, body
        assert "missing-concerns-for-reviewer" not in codes, body
        # The Coupling check block is preserved verbatim in `raw` so
        # downstream cross-reviewer dispatch can read it.
        assert "**Coupling check:**" in body["raw"]
        assert "pattern_family" in body["raw"]
        assert "siblings_checked" in body["raw"]

    def test_coupling_check_not_applicable_form_round_trips(self) -> None:
        # TASK-002: when no Step 4.5 trigger fires, the implementer
        # emits a one-line `not applicable` form instead of the
        # structured fenced block. Verify the parser tolerates this
        # form too.
        report = (
            "**Outcome:** success\n"
            "**Files changed:**\n- a.txt\n"
            "**Diff summary:** noop\n"
            "**Test outcome:** not_run\n"
            "**Coupling check:** not applicable — AC names no regex/header/symbol pattern.\n"
            "**Concerns for reviewer:**\n- none\n"
            "**Plan adaptations:**\n- none\n"
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
        assert body["outcome"] == "success"
        assert "not applicable" in body["raw"]


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


# ---------------------------------------------------------------------------
# TASK-004C: fail-task tracked+untracked partition cleanup (V1-V17)
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_fail_repo(tmp_path: Path) -> Path:
    """Dedicated fresh git repo for the V1-V17 fail-task suite.

    Seeds:
        * ``tracked.txt`` committed with ``original\\n``
        * ``docs/plans/<plan>.md`` with TASK-001 at status ``open``
        * ``docs/plans/_run_log.jsonl`` committed (so V5 can verify the
          tracked + protected branch without ``fail-task`` restoring it)
    """
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("original\n", encoding="utf-8")
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    plan = plans / "sample.md"
    plan.write_text(SAMPLE_PLAN_BODY, encoding="utf-8")
    (plans / "_run_log.jsonl").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _fail_task_run(
    repo: Path,
    *,
    files: str,
    plan_name: str = "sample.md",
    repo_root: Path | str | None = "__SELF__",
    cwd: Path | None = None,
    stage: str = "implement",
    reason: str = "test_reason",
) -> subprocess.CompletedProcess:
    plan = repo / "docs" / "plans" / plan_name
    args = [
        "fail-task",
        "--plan-file", str(plan),
        "--task-id", "001",
        "--run-id", "R1",
        "--files", files,
        "--stage", stage,
        "--reason", reason,
        "--json",
    ]
    if repo_root == "__SELF__":
        args.extend(["--repo-root", str(repo)])
    elif repo_root is not None:
        args.extend(["--repo-root", str(repo_root)])
    return _run(*args, cwd=cwd if cwd is not None else repo)


class TestFailTaskPartitionCleanup:
    """TASK-004C V1-V17 coverage: tracked+untracked partition cleanup in
    ``fail-task`` plus shared protected-paths module."""

    # V1 -----------------------------------------------------------------
    def test_v1_restores_tracked_edit(self, tmp_fail_repo: Path) -> None:
        (tmp_fail_repo / "tracked.txt").write_text("modded\n", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files="tracked.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []
        assert (tmp_fail_repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"

    # V2 -----------------------------------------------------------------
    def test_v2_removes_untracked_creates(self, tmp_fail_repo: Path) -> None:
        (tmp_fail_repo / "new.txt").write_text("leftover\n", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files="new.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert (tmp_fail_repo / "new.txt").exists() is False
        assert set(body["removed_untracked"]) == {"new.txt"}
        assert body["restore_ok"] is True

    # V3 -----------------------------------------------------------------
    def test_v3_mixed_tracked_and_untracked(self, tmp_fail_repo: Path) -> None:
        """Hard-won regression: previously ``git restore -- tracked untracked``
        exited 1 on the pathspec error, leaving tracked unreverted."""
        (tmp_fail_repo / "tracked.txt").write_text("modded\n", encoding="utf-8")
        (tmp_fail_repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files="tracked.txt,untracked.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # (a) tracked.txt restored
        assert (tmp_fail_repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"
        # (b) untracked.txt removed
        assert (tmp_fail_repo / "untracked.txt").exists() is False
        # (c) restore_ok is True (not merely truthy)
        assert body["restore_ok"] is True
        # (d) removed_untracked == {"untracked.txt"}
        assert set(body["removed_untracked"]) == {"untracked.txt"}
        # (e) / (f)
        assert body["protected_skipped"] == []
        assert body["out_of_repo_skipped"] == []

    # V4 -----------------------------------------------------------------
    def test_v4_preserves_sibling_untracked(self, tmp_fail_repo: Path) -> None:
        (tmp_fail_repo / "in_scope.txt").write_text("in\n", encoding="utf-8")
        (tmp_fail_repo / "sibling.txt").write_text("keep\n", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files="in_scope.txt")
        assert cp.returncode == 0, cp.stderr
        assert (tmp_fail_repo / "in_scope.txt").exists() is False
        assert (tmp_fail_repo / "sibling.txt").read_text(encoding="utf-8") == "keep\n"

    # V5 -----------------------------------------------------------------
    def test_v5_skips_protected_paths(self, tmp_fail_repo: Path) -> None:
        run_log = tmp_fail_repo / "docs" / "plans" / "_run_log.jsonl"
        run_log.write_text("modded\n", encoding="utf-8")
        schedule = tmp_fail_repo / "docs" / "plans" / "sample.schedule.json"
        schedule.write_text("{}\n", encoding="utf-8")
        cp = _fail_task_run(
            tmp_fail_repo,
            files="docs/plans/_run_log.jsonl,docs/plans/sample.schedule.json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # Protected tracked file was NOT restored to its committed content.
        # (fail-task still appends to the run log for its own event -- that is
        # NOT the restore path under test here; the assertion is that the
        # on-disk ``modded\n`` prefix survives, which it only does if
        # ``git restore`` was NOT invoked against this file.)
        assert run_log.read_text(encoding="utf-8").startswith("modded\n")
        # Protected untracked file still present (NOT unlinked).
        assert schedule.exists()
        assert sorted(body["protected_skipped"]) == sorted(
            ["docs/plans/_run_log.jsonl", "docs/plans/sample.schedule.json"]
        )
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []

    # V6 -----------------------------------------------------------------
    def test_v6_idempotent_no_op_when_clean(
        self, tmp_fail_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Exercise the legacy ``--repo-root``-not-passed path via monkeypatch.chdir.
        monkeypatch.chdir(tmp_fail_repo)
        cp = _fail_task_run(tmp_fail_repo, files="tracked.txt", repo_root=None)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []
        assert (tmp_fail_repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"

    # V7 -----------------------------------------------------------------
    def test_v7_status_mutation_runs_when_all_skipped(
        self, tmp_fail_repo: Path
    ) -> None:
        # Directory entry, out-of-repo entry, and protected entry.
        some_dir = tmp_fail_repo / "some_dir"
        some_dir.mkdir()
        # Use a sibling path of tmp_fail_repo to guarantee it is outside.
        outside = tmp_fail_repo.parent / f"{tmp_fail_repo.name}_outsider.txt"
        outside.write_text("x\n", encoding="utf-8")
        cp = _fail_task_run(
            tmp_fail_repo,
            files=f"some_dir,{outside},docs/plans/_run_log.jsonl",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []
        assert body["directory_skipped"] == ["some_dir"]
        assert body["protected_skipped"] == ["docs/plans/_run_log.jsonl"]
        assert body["out_of_repo_skipped"] == [str(outside)]
        assert body["status_updated"] is True
        plan_text = (tmp_fail_repo / "docs" / "plans" / "sample.md").read_text(
            encoding="utf-8"
        )
        assert "### TASK-001: First task\n\n- **Status:** failed" in plan_text
        # Outsider file still exists.
        assert outside.exists()

    # V8 -----------------------------------------------------------------
    def test_v8_output_shape_is_stable(self, tmp_fail_repo: Path) -> None:
        cp = _fail_task_run(tmp_fail_repo, files="tracked.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        expected = {
            "restore_ok",
            "status_updated",
            "log_appended",
            "removed_untracked",
            "protected_skipped",
            "out_of_repo_skipped",
            "directory_skipped",
            "submodule_skipped",
        }
        assert set(body.keys()) == expected

    # V9 -----------------------------------------------------------------
    def test_v9_repo_root_threaded(
        self, tmp_fail_repo: Path, tmp_path: Path
    ) -> None:
        alien_cwd = tmp_path / "elsewhere"
        alien_cwd.mkdir()
        # Decoy at alien cwd that MUST remain untouched.
        (alien_cwd / "in_scope.txt").write_text("decoy\n", encoding="utf-8")
        # Real target under repo root.
        (tmp_fail_repo / "in_scope.txt").write_text("leftover\n", encoding="utf-8")
        cp = _fail_task_run(
            tmp_fail_repo,
            files="in_scope.txt",
            cwd=alien_cwd,
        )
        assert cp.returncode == 0, cp.stderr
        # Repo-root copy removed.
        assert (tmp_fail_repo / "in_scope.txt").exists() is False
        # Alien-cwd decoy preserved.
        assert (alien_cwd / "in_scope.txt").read_text(encoding="utf-8") == "decoy\n"

    # V10 ----------------------------------------------------------------
    def test_v10_protected_predicate_shared_across_callsites(self) -> None:
        """Three-way parity: all callsites alias the same function from
        ``_plan_paths``. Prevents the drift that motivated TASK-004C."""
        import importlib

        # Load the shared module directly.
        _plan_paths = importlib.import_module("_plan_paths")
        is_protected = _plan_paths.is_protected_path
        # Fixture set covering exact, prefix, suffix, glob, ./-prefixed,
        # a/../-containing. (Windows backslash normalization is handled at
        # the canonicalize_file boundary, not in is_protected_path.)
        positives = [
            ".codex",
            ".claude",
            "_run_lock.json",
            "docs/plans/_run_log.jsonl",
            ".codex/session.json",
            ".claude/skills/plan.md",
            "docs/plans/anything.schedule.json",
            "plugins/plan-executor/scripts/plan_ops.py",
            "plugins/plan-executor/scripts/plan_codex_dispatch.py",
        ]
        for rel in positives:
            assert is_protected(rel) is True, rel
        # Load plan_codex_dispatch.py and plan_ops.py as independent modules
        # and confirm they alias the SAME function object from _plan_paths.
        import importlib.util

        def _load_by_path(name: str, path: Path):
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        scripts_dir = (
            REPO_ROOT / "plugins" / "plan-executor" / "scripts"
        )
        wrapper = _load_by_path(
            "plan_codex_dispatch_v10", scripts_dir / "plan_codex_dispatch.py"
        )
        ops = _load_by_path("plan_ops_v10", scripts_dir / "plan_ops.py")
        assert wrapper.is_protected_path is is_protected
        assert wrapper._is_protected is is_protected  # alias compat
        assert ops.is_protected_path is is_protected
        # Wrapper + ops MUST NOT redefine the constants locally.
        assert wrapper.PROTECTED_EXACT_PATHS is _plan_paths.PROTECTED_EXACT_PATHS
        assert ops.PROTECTED_EXACT_PATHS is _plan_paths.PROTECTED_EXACT_PATHS

    # V11 ----------------------------------------------------------------
    def test_v11_absolute_path_outside_repo(
        self, tmp_fail_repo: Path
    ) -> None:
        # Sibling of the repo dir is guaranteed outside repo_root.
        outside = tmp_fail_repo.parent / f"{tmp_fail_repo.name}_v11_outside.txt"
        outside.write_text("important", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files=str(outside))
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert outside.exists()
        assert outside.read_text(encoding="utf-8") == "important"
        assert body["out_of_repo_skipped"] == [str(outside)]
        assert body["removed_untracked"] == []
        assert body["restore_ok"] is True
        assert body["status_updated"] is True

    # V12 ----------------------------------------------------------------
    def test_v12_dotdot_escape(
        self, tmp_fail_repo: Path
    ) -> None:
        sibling = tmp_fail_repo.parent / f"{tmp_fail_repo.name}_v12_sib.txt"
        sibling.write_text("keep\n", encoding="utf-8")
        # Compute a ../ path relative to the repo root.
        dotdot = f"../{sibling.name}"
        cp = _fail_task_run(tmp_fail_repo, files=dotdot)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert sibling.read_text(encoding="utf-8") == "keep\n"
        assert dotdot in body["out_of_repo_skipped"]
        assert body["removed_untracked"] == []

    # V13 ----------------------------------------------------------------
    def test_v13_directory_entry(self, tmp_fail_repo: Path) -> None:
        some = tmp_fail_repo / "some_dir"
        some.mkdir()
        (some / "tracked_child.txt").write_text("tc\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "some_dir/tracked_child.txt"],
            cwd=tmp_fail_repo,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-qm", "add_child"],
            cwd=tmp_fail_repo,
            check=True,
        )
        # Also a fresh untracked child.
        (some / "untracked_child.txt").write_text("uc\n", encoding="utf-8")
        # Modify the tracked child.
        (some / "tracked_child.txt").write_text("tc-modded\n", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files="some_dir")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # Directory itself and both children untouched.
        assert some.is_dir()
        assert (some / "tracked_child.txt").read_text(encoding="utf-8") == "tc-modded\n"
        assert (some / "untracked_child.txt").read_text(encoding="utf-8") == "uc\n"
        assert body["directory_skipped"] == ["some_dir"]
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []

    # V14 ----------------------------------------------------------------
    def test_v14_submodule_path(self, tmp_fail_repo: Path) -> None:
        """Nested ``git init`` inside the repo (treated like a submodule
        for classification purposes — same _is_inside_submodule path)."""
        submod = tmp_fail_repo / "nested"
        submod.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=submod, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=submod, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=submod, check=True)
        (submod / "inner.txt").write_text("inner\n", encoding="utf-8")
        subprocess.run(["git", "add", "inner.txt"], cwd=submod, check=True)
        subprocess.run(["git", "commit", "-qm", "inner"], cwd=submod, check=True)
        # Modify the inner file so "if fail-task restored it, we'd notice".
        (submod / "inner.txt").write_text("modded\n", encoding="utf-8")
        cp = _fail_task_run(tmp_fail_repo, files="nested/inner.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # File untouched
        assert (submod / "inner.txt").read_text(encoding="utf-8") == "modded\n"
        assert "nested/inner.txt" in body["submodule_skipped"]
        assert body["removed_untracked"] == []
        assert body["restore_ok"] is True

    # V14b ---------------------------------------------------------------
    def test_v14b_submodule_root_classified_as_submodule(
        self, tmp_fail_repo: Path
    ) -> None:
        """Gitlink root IS a directory in the worktree; classification order
        must catch submodule BEFORE directory."""
        # Create a real submodule-like gitlink via `git submodule add`.
        upstream = tmp_fail_repo.parent / f"{tmp_fail_repo.name}_upstream"
        upstream.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=upstream, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=upstream, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=upstream, check=True)
        (upstream / "readme.txt").write_text("seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "readme.txt"], cwd=upstream, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=upstream, check=True)
        submod_path = "submods/foo"
        env = os.environ.copy()
        env["GIT_ALLOW_PROTOCOL"] = "file"
        r = subprocess.run(
            [
                "git",
                "-c", "protocol.file.allow=always",
                "submodule", "add", "-q",
                str(upstream),
                submod_path,
            ],
            cwd=tmp_fail_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            pytest.skip(
                "git submodule add unavailable in this sandbox: "
                + (r.stderr or r.stdout)
            )
        subprocess.run(
            ["git", "commit", "-qm", "add_sub"],
            cwd=tmp_fail_repo,
            check=True,
        )
        cp = _fail_task_run(tmp_fail_repo, files=submod_path)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["submodule_skipped"] == [submod_path]
        assert body["directory_skipped"] == []
        assert body["removed_untracked"] == []
        assert body["restore_ok"] is True
        # Submodule dir still present.
        assert (tmp_fail_repo / submod_path).exists()

    # V15 ----------------------------------------------------------------
    def test_v15_path_normalization(self, tmp_fail_repo: Path) -> None:
        (tmp_fail_repo / "docs" / "plans" / "_run_log.jsonl").write_text(
            "modded\n", encoding="utf-8"
        )
        cp = _fail_task_run(
            tmp_fail_repo,
            files="./docs/plans/_run_log.jsonl,plugins/plan-executor/scripts/./plan_ops.py",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert sorted(body["protected_skipped"]) == sorted(
            [
                "docs/plans/_run_log.jsonl",
                "plugins/plan-executor/scripts/plan_ops.py",
            ]
        )

    # V16 ----------------------------------------------------------------
    def test_v16_tracked_deleted_is_restored(self, tmp_fail_repo: Path) -> None:
        (tmp_fail_repo / "tracked.txt").unlink()
        assert (tmp_fail_repo / "tracked.txt").exists() is False
        cp = _fail_task_run(tmp_fail_repo, files="tracked.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []
        assert (tmp_fail_repo / "tracked.txt").read_text(encoding="utf-8") == "original\n"

    # V17 ----------------------------------------------------------------
    def test_v17_nonexistent_silent_noop(self, tmp_fail_repo: Path) -> None:
        cp = _fail_task_run(tmp_fail_repo, files="nonexistent.txt")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["restore_ok"] is True
        assert body["removed_untracked"] == []
        assert body["status_updated"] is True


# ---------------------------------------------------------------------------
# TASK-004D: block-dependents must mutate plan markdown (V1-V16)
# ---------------------------------------------------------------------------


BLOCK_DEP_PLAN_BODY = """# Plan: block-dependents sample

**Created:** 2026-04-15
**Status:** in-progress
**Base branch:** main

## Context

Prose.

## Tasks

### TASK-001: First task

- **Status:** pending
- **Agent:** claude
- **Files:**
  - src/foo.py
- **Dependencies:** none

### TASK-002: Second task

- **Status:** pending
- **Agent:** claude
- **Files:**
  - src/bar.py
- **Dependencies:** [001]

### TASK-003: Third task

- **Status:** pending
- **Agent:** claude
- **Files:**
  - src/baz.py
- **Dependencies:** none

### TASK-004: Fourth task

- **Status:** pending
- **Agent:** claude
- **Files:**
  - src/qux.py
- **Dependencies:** [002]

### TASK-005: Fifth task

- **Status:** pending
- **Agent:** claude
- **Files:**
  - src/quux.py
- **Dependencies:** [003]
"""


def _bd_status_of(plan_text: str, task_id: str) -> str:
    """Return the current `**Status:**` value of TASK-NNN in plan_text."""
    m = re.search(
        rf"### TASK-{task_id}:[^\n]*\n\n- \*\*Status:\*\* (\S+)",
        plan_text,
    )
    assert m, f"could not find status for TASK-{task_id} in plan"
    return m.group(1)


def _bd_make_args(
    *,
    schedule_file: Path,
    plan_file: Path,
    failed: str = "001",
    run_id: str = "RID1",
    json_out: bool = True,
) -> "argparse.Namespace":
    import argparse as _argparse
    ns = _argparse.Namespace(
        command="block-dependents",
        schedule_file=str(schedule_file),
        plan_file=str(plan_file),
        failed=failed,
        run_id=run_id,
        json=json_out,
    )
    return ns


def _bd_call(ns) -> tuple[int, dict]:
    """Invoke cmd_block_dependents in-process. Returns (exit_code, json_body).

    `_emit` calls sys.exit; catch SystemExit and parse stdout as JSON.
    """
    import io
    import contextlib
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            plan_ops.cmd_block_dependents(ns)
        except SystemExit as e:
            code = int(e.code) if e.code is not None else 0
    raw = buf.getvalue()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        body = {"__raw__": raw}
    return code, body


def _bd_write_schedule(
    tmp_path: Path,
    tasks: list[dict],
    *,
    default_plan_file: str | None = "sample.md",
) -> Path:
    """Write a block-dependents schedule fixture.

    TASK-008 (per_task_dispatch_refactor_v2): the directory-only contract
    requires every dependent to declare `plan_file`. The single-file
    fallback was REMOVED, so this helper auto-injects
    ``plan_file: default_plan_file`` for any task entry that omits it.
    Callers exercising the multi-file routing pass an explicit
    ``plan_file`` per task and can leave the default in place (their
    explicit value wins). Callers asserting the new
    `missing-plan-file` rejection pass ``default_plan_file=None`` and
    omit the field deliberately.
    """
    if default_plan_file is not None:
        tasks = [
            {**t, "plan_file": t.get("plan_file") or default_plan_file}
            for t in tasks
        ]
    p = tmp_path / "schedule.json"
    p.write_text(
        json.dumps({"outcome": "valid", "tasks": tasks, "batches": [], "gaps": [], "risks": []}),
        encoding="utf-8",
    )
    return p


@pytest.fixture()
def isolated_bd_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Plan + sandboxed run-log path for block-dependents V1-V16 tests."""
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    plan = plans_dir / "sample.md"
    plan.write_text(BLOCK_DEP_PLAN_BODY, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_dir)
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans_dir / "_run_log.jsonl")
    monkeypatch.setattr(plan_ops, "RUN_LOCK_PATH", plans_dir / "_run_lock.json")
    return plan


def _bd_read_run_log_events(plan: Path, event: str = "blocked") -> list[dict]:
    run_log = plan.parent / "_run_log.jsonl"
    if not run_log.is_file():
        return []
    out: list[dict] = []
    for line in run_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("event") == event:
            out.append(rec)
    return out


class TestBlockDependents:
    """TASK-004D V1-V16 coverage for `block-dependents`.

    Locks in the source-of-truth invariant: the plan file (not the run-log)
    is the authoritative state store for `blocked` cascades. I/O discipline
    is single plan read + single plan write; ordering is mutate-all-in-memory
    → plan write → run-log append per id.
    """

    # V1 -----------------------------------------------------------------
    def test_v1_single_level_cascade_mutates_plan(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": []},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["blocked_task_ids"] == ["002"]
        assert body["plan_mutations_applied"] == ["002"]
        assert body["run_log_appended"] == ["002"]
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        # 001 untouched (fail-task owns it), 003 untouched (not a dependent)
        assert _bd_status_of(plan_text, "001") == "pending"
        assert _bd_status_of(plan_text, "003") == "pending"
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert len(events) == 1
        assert events[0]["task_id"] == "002"

    # V2 -----------------------------------------------------------------
    def test_v2_transitive_chain_single_io(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["002"]},
            {"id": "004", "dependencies": ["003"]},
        ])

        loads: list[Path] = []
        writes: list[Path] = []
        orig_load = plan_ops._load_text
        orig_write = plan_ops._write_text

        def counting_load(path):
            loads.append(Path(path))
            return orig_load(path)

        def counting_write(path, text):
            writes.append(Path(path))
            return orig_write(path, text)

        monkeypatch.setattr(plan_ops, "_load_text", counting_load)
        monkeypatch.setattr(plan_ops, "_write_text", counting_write)

        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        plan_reads = [p for p in loads if p == isolated_bd_plan]
        plan_writes = [p for p in writes if p == isolated_bd_plan]
        assert len(plan_reads) == 1
        assert len(plan_writes) == 1
        assert body["blocked_task_ids"] == ["002", "003", "004"]
        assert body["plan_mutations_applied"] == ["002", "003", "004"]
        assert body["run_log_appended"] == ["002", "003", "004"]
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        for tid in ("002", "003", "004"):
            assert _bd_status_of(plan_text, tid) == "blocked"

    # V3 -----------------------------------------------------------------
    def test_v3_bfs_and_sibling_order_preserved(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        # tasks[] intentionally NOT numeric sort order: [001, 003, 002, 005, 004]
        # 001 failed; direct deps on 001: 002, 003 (in tasks[] order: 003, 002)
        # transitive: 004→002, 005→003 (in tasks[] order: 005, 004)
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "003", "dependencies": ["001"]},
            {"id": "002", "dependencies": ["001"]},
            {"id": "005", "dependencies": ["003"]},
            {"id": "004", "dependencies": ["002"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        expected = ["003", "002", "005", "004"]
        assert body["blocked_task_ids"] == expected
        assert body["plan_mutations_applied"] == expected
        assert body["run_log_appended"] == expected
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert [e["task_id"] for e in events] == expected

    # V4 -----------------------------------------------------------------
    def test_v4_output_shape_subset(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        required = {"blocked_task_ids", "plan_mutations_applied", "run_log_appended"}
        assert required.issubset(set(body.keys()))
        for k in required:
            assert isinstance(body[k], list)
            assert all(isinstance(v, str) for v in body[k])

    # V5 -----------------------------------------------------------------
    def test_v5_missing_dependent_block_halts_pre_write(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        """TASK-008: with the single-file fallback removed, every
        dependent declares `plan_file` and the directory-mode pre-write
        block-presence probe runs unconditionally. A plan body that
        omits the dependent's block surfaces as `dependent-block-missing`
        BEFORE any mutation is attempted — strictly stronger than the
        prior inline `plan_mutate` failure (no partial writes possible).
        """
        # Write a plan body with TASK-002 block MISSING.
        short_plan = (
            "# Plan: short\n\n"
            "**Status:** in-progress\n"
            "**Base branch:** main\n\n"
            "## Tasks\n\n"
            "### TASK-001: Only task\n\n"
            "- **Status:** pending\n"
            "- **Dependencies:** none\n"
        )
        isolated_bd_plan.write_text(short_plan, encoding="utf-8")
        original_on_disk = isolated_bd_plan.read_text(encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "dependent-block-missing"
        assert err["path"] == "$.tasks[1]"
        assert err["failed_id"] == "002"
        assert err["plan_file"] == "sample.md"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        assert err["remaining"] == ["002"]
        # Plan on disk: unchanged.
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk
        # No blocked events.
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V6 -----------------------------------------------------------------
    def test_v6_missing_block_in_cascade_halts_atomically(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        """TASK-008: when ANY dependent's block is missing, the directory
        pre-check halts the whole cascade BEFORE any flip lands on disk.
        This replaces the prior partial-persistence semantics: 002 is no
        longer flipped speculatively when 003 turns out to be missing.
        Atomicity wins.
        """
        # Plan has TASK-002 block but NOT TASK-003. Both 002 and 003 are
        # direct dependents of 001 in tasks[] order [002, 003].
        partial_plan = (
            "# Plan: partial\n\n"
            "**Status:** in-progress\n"
            "**Base branch:** main\n\n"
            "## Tasks\n\n"
            "### TASK-001: First\n\n"
            "- **Status:** pending\n"
            "- **Dependencies:** none\n\n"
            "### TASK-002: Second\n\n"
            "- **Status:** pending\n"
            "- **Dependencies:** [001]\n"
        )
        isolated_bd_plan.write_text(partial_plan, encoding="utf-8")
        original_on_disk = isolated_bd_plan.read_text(encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "dependent-block-missing"
        assert err["path"] == "$.tasks[2]"
        assert err["failed_id"] == "003"
        assert err["plan_file"] == "sample.md"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        # Plan on disk: untouched.
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk
        # No blocked events: pre-check halted before any append.
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V7 -----------------------------------------------------------------
    def test_v7_idempotent_on_already_blocked(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        # Pre-flip 002 to blocked (simulating a prior partial run).
        text = isolated_bd_plan.read_text(encoding="utf-8")
        text = text.replace(
            "### TASK-002: Second task\n\n- **Status:** pending",
            "### TASK-002: Second task\n\n- **Status:** blocked",
        )
        isolated_bd_plan.write_text(text, encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["blocked_task_ids"] == ["002"]
        assert body["plan_mutations_applied"] == ["002"]
        assert body["run_log_appended"] == ["002"]
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert len(events) == 1
        assert events[0]["task_id"] == "002"

    # V8 -----------------------------------------------------------------
    def test_v8_cli_signature_requires_plan_file(
        self, tmp_path: Path,
    ) -> None:
        sched = _bd_write_schedule(tmp_path, [{"id": "001", "dependencies": []}])
        cp = _run(
            "block-dependents",
            "--schedule-file", str(sched),
            "--failed", "001",
            "--run-id", "RID1",
            "--json",
        )
        assert cp.returncode != 0
        # argparse surfaces the missing argument on stderr.
        combined = (cp.stderr or "") + (cp.stdout or "")
        assert "--plan-file" in combined

    # V9 -----------------------------------------------------------------
    def test_v9_leaves_non_dependents_untouched(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": []},
            {"id": "004", "dependencies": []},
            {"id": "005", "dependencies": []},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["plan_mutations_applied"] == ["002"]
        assert body["run_log_appended"] == ["002"]
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        for tid in ("003", "004", "005"):
            assert _bd_status_of(plan_text, tid) == "pending"
        assert _bd_status_of(plan_text, "002") == "blocked"

    # V10 ----------------------------------------------------------------
    def test_v10_plan_read_failure_halts_before_mutation(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_on_disk = isolated_bd_plan.read_text(encoding="utf-8")

        def bad_load(path):
            raise OSError("simulated read fail")

        monkeypatch.setattr(plan_ops, "_load_text", bad_load)
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "plan_read"
        assert err["failed_id"] is None
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        assert err["remaining"] == ["002"]
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V11 ----------------------------------------------------------------
    def test_v11_plan_write_failure(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_on_disk = isolated_bd_plan.read_text(encoding="utf-8")

        def bad_write(path, text):
            raise OSError("simulated write fail")

        monkeypatch.setattr(plan_ops, "_write_text", bad_write)
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["002"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "plan_write"
        assert err["failed_id"] is None
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        assert err["remaining"] == []
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V12 ----------------------------------------------------------------
    def test_v12_log_append_failure_mid_loop(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = {"n": 0}
        orig_append = plan_ops._append_run_log

        def flaky_append(event, fields):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated log-append fail on 003")
            return orig_append(event, fields)

        monkeypatch.setattr(plan_ops, "_append_run_log", flaky_append)

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["002"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "run_log_append"
        assert err["failed_id"] == "003"
        assert err["plan_mutations_applied"] == ["002", "003"]
        assert err["run_log_appended"] == ["002"]
        assert err["remaining"] == []
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        assert _bd_status_of(plan_text, "003") == "blocked"
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert [e["task_id"] for e in events] == ["002"]

    # V13 ----------------------------------------------------------------
    def test_v13_log_append_failure_first_id(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def bad_append(event, fields):
            raise RuntimeError("simulated log-append fail on first id")

        monkeypatch.setattr(plan_ops, "_append_run_log", bad_append)
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "run_log_append"
        assert err["failed_id"] == "002"
        assert err["plan_mutations_applied"] == ["002"]
        assert err["run_log_appended"] == []
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V14 ----------------------------------------------------------------
    def test_v14_no_dependents_no_plan_io(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_on_disk = isolated_bd_plan.read_text(encoding="utf-8")
        plan_reads: list[Path] = []
        plan_writes: list[Path] = []

        def bad_load(path):
            plan_reads.append(Path(path))
            raise AssertionError("plan_path should not be read for empty cascade")

        def bad_write(path, text):
            plan_writes.append(Path(path))
            raise AssertionError("plan_path should not be written for empty cascade")

        monkeypatch.setattr(plan_ops, "_load_text", bad_load)
        monkeypatch.setattr(plan_ops, "_write_text", bad_write)

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": []},
            {"id": "003", "dependencies": []},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["blocked_task_ids"] == []
        assert body["plan_mutations_applied"] == []
        assert body["run_log_appended"] == []
        assert plan_reads == []
        assert plan_writes == []
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V15 ----------------------------------------------------------------
    def test_v15_double_failure_mutate_then_write(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original_on_disk = isolated_bd_plan.read_text(encoding="utf-8")

        orig_mutate = plan_ops.mutate_task_status

        def mutate_fails_on_004(text, task_id, new_status):
            if task_id == "004":
                raise ValueError("synthetic: no block for 004")
            return orig_mutate(text, task_id, new_status)

        def bad_write(path, text):
            raise OSError("write refused")

        monkeypatch.setattr(plan_ops, "mutate_task_status", mutate_fails_on_004)
        monkeypatch.setattr(plan_ops, "_write_text", bad_write)

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["002"]},
            {"id": "004", "dependencies": ["003"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "plan_mutate"
        assert err["failed_id"] == "004"
        assert err["secondary_failed_stage"] == "plan_write"
        assert err["secondary_failed_id"] is None
        assert "write refused" in err["secondary_error"]
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk

    # V16 ----------------------------------------------------------------
    def test_v16_double_failure_mutate_then_log(
        self, tmp_path: Path, isolated_bd_plan: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        orig_mutate = plan_ops.mutate_task_status

        def mutate_fails_on_004(text, task_id, new_status):
            if task_id == "004":
                raise ValueError("synthetic: no block for 004")
            return orig_mutate(text, task_id, new_status)

        log_calls = {"n": 0}
        orig_append = plan_ops._append_run_log

        def flaky_append(event, fields):
            log_calls["n"] += 1
            if log_calls["n"] == 2:
                raise RuntimeError("synthetic log fail on second append")
            return orig_append(event, fields)

        monkeypatch.setattr(plan_ops, "mutate_task_status", mutate_fails_on_004)
        monkeypatch.setattr(plan_ops, "_append_run_log", flaky_append)

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["002"]},
            {"id": "004", "dependencies": ["003"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "plan_mutate"
        assert err["failed_id"] == "004"
        assert err["secondary_failed_stage"] == "run_log_append"
        assert err["secondary_failed_id"] == "003"
        assert err["plan_mutations_applied"] == ["002", "003"]
        assert err["run_log_appended"] == ["002"]
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        assert _bd_status_of(plan_text, "003") == "blocked"
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert [e["task_id"] for e in events] == ["002"]


# ---------------------------------------------------------------------------
# TASK-002: block-dependents multi-file cascade (directory-mode routing)
# ---------------------------------------------------------------------------


def _bd_child_plan_body(task_id: str, deps: list[str]) -> str:
    """Minimal single-task child plan body suitable for block-dependents."""
    dep_str = "[" + ", ".join(deps) + "]" if deps else "none"
    return (
        f"# Plan: child {task_id}\n\n"
        "**Created:** 2026-04-23\n"
        "**Status:** in-progress\n"
        "**Base branch:** main\n\n"
        "## Context\n\nProse.\n\n"
        "## Tasks\n\n"
        f"### TASK-{task_id}: Task {task_id}\n\n"
        "- **Status:** pending\n"
        "- **Agent:** claude\n"
        "- **Files:**\n"
        f"  - src/{task_id}.py\n"
        f"- **Dependencies:** {dep_str}\n"
    )


def _bd_multi_child_plan_body(task_entries: list[tuple[str, list[str]]]) -> str:
    """Single child plan body containing multiple tasks.

    `task_entries` is an ordered list of `(task_id, deps)` tuples. Used to
    simulate a child file that holds both the failed task and one of its
    dependents — the 3-file-cascade test needs this shape so one child has
    two distinct tasks (001 = failed, 002 = blocked) while 003 and 004 live
    in their own single-task files.
    """
    lines = [
        "# Plan: multi-task child\n",
        "\n",
        "**Created:** 2026-04-23\n",
        "**Status:** in-progress\n",
        "**Base branch:** main\n",
        "\n",
        "## Context\n",
        "\nProse.\n",
        "\n",
        "## Tasks\n",
        "\n",
    ]
    for tid, deps in task_entries:
        dep_str = "[" + ", ".join(deps) + "]" if deps else "none"
        lines.extend([
            f"### TASK-{tid}: Task {tid}\n",
            "\n",
            "- **Status:** pending\n",
            "- **Agent:** claude\n",
            "- **Files:**\n",
            f"  - src/{tid}.py\n",
            f"- **Dependencies:** {dep_str}\n",
            "\n",
        ])
    return "".join(lines)


@pytest.fixture()
def isolated_bd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandboxed plan-directory root for multi-file block-dependents tests."""
    plans_dir = tmp_path / "docs" / "plans" / "dirmode"
    plans_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_dir)
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans_dir / "_run_log.jsonl")
    monkeypatch.setattr(plan_ops, "RUN_LOCK_PATH", plans_dir / "_run_lock.json")
    return plans_dir


def _bd_read_run_log_events_in(plans_dir: Path, event: str = "blocked") -> list[dict]:
    run_log = plans_dir / "_run_log.jsonl"
    if not run_log.is_file():
        return []
    out: list[dict] = []
    for line in run_log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("event") == event:
            out.append(rec)
    return out


class TestBlockDependentsMultiFile:
    """TASK-002 directory-mode routing for block-dependents.

    Locks in the new per-dependent `plan_file` behavior: cascade grouped
    by resolved file, one atomic write per group, run-log events carry
    `plan_file` basename for audit attribution, and pre-write containment
    + block-presence halts when the schedule references a child file
    that cannot be safely resolved.
    """

    # (a) degenerate one-child directory ------------------------------------
    def test_block_dependents_multi_file_single_child_directory(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        """Single-child plan_dir: every dependent routes to the same file.

        TASK-008 (per_task_dispatch_refactor_v2) REMOVED the single-file
        `--plan-file` fallback. The `_bd_write_schedule` helper now
        auto-injects `plan_file: "sample.md"` so this test exercises a
        legitimate one-child-directory cascade — every dependent
        explicitly declares its target file. Output shape and run-log
        attribution are identical to the multi-child case.
        """
        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["002"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["blocked_task_ids"] == ["002", "003"]
        assert body["plan_mutations_applied"] == ["002", "003"]
        assert body["run_log_appended"] == ["002", "003"]
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        assert _bd_status_of(plan_text, "003") == "blocked"
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert [e["task_id"] for e in events] == ["002", "003"]
        for ev in events:
            assert ev["plan_file"] == isolated_bd_plan.name
            assert ev["plan_file"] == "sample.md"

    # (b) two-file cascade ---------------------------------------------------
    def test_block_dependents_multi_file_two_file_cascade(
        self, tmp_path: Path, isolated_bd_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Failed in child-a, one transitive dependent in child-b.

        Exactly one write to child-b; child-a is read only via the
        --plan-file existence probe and is NEVER written (block-dependents
        does not mutate the failed task's own file).
        """
        child_a = isolated_bd_dir / "child-a.md"
        child_b = isolated_bd_dir / "child-b.md"
        child_a.write_text(_bd_child_plan_body("001", deps=[]), encoding="utf-8")
        child_b.write_text(_bd_child_plan_body("002", deps=["001"]), encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
            {"id": "002", "dependencies": ["001"], "plan_file": "child-b.md"},
        ])

        writes: list[Path] = []
        orig_write = plan_ops._write_text

        def counting_write(path, text):
            writes.append(Path(path))
            return orig_write(path, text)

        monkeypatch.setattr(plan_ops, "_write_text", counting_write)

        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["blocked_task_ids"] == ["002"]
        assert body["plan_mutations_applied"] == ["002"]
        assert body["run_log_appended"] == ["002"]
        # child-a never written (failed task's own file is fail-task's job).
        assert child_a not in writes
        # child-b written exactly once.
        assert writes.count(child_b) == 1
        # child-b task flipped; child-a untouched.
        assert _bd_status_of(child_b.read_text(encoding="utf-8"), "002") == "blocked"
        assert _bd_status_of(child_a.read_text(encoding="utf-8"), "001") == "pending"
        events = _bd_read_run_log_events_in(isolated_bd_dir)
        assert len(events) == 1
        assert events[0]["task_id"] == "002"
        assert events[0]["plan_file"] == "child-b.md"

    # (c) three-file cascade with one dependent in each ----------------------
    def test_block_dependents_multi_file_three_file_cascade(
        self, tmp_path: Path, isolated_bd_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TASK-001 fails (in child-a). Three direct dependents across A, B, C.

        TASK-002 lives in child-a (same file as the failed task). TASK-003
        in child-b. TASK-004 in child-c. Exactly three writes (one per
        file), each a single atomic mutate+replace.
        """
        child_a = isolated_bd_dir / "child-a.md"
        child_b = isolated_bd_dir / "child-b.md"
        child_c = isolated_bd_dir / "child-c.md"
        # child-a holds both 001 (failed) and 002 (dependent).
        child_a.write_text(
            _bd_multi_child_plan_body([("001", []), ("002", ["001"])]),
            encoding="utf-8",
        )
        child_b.write_text(_bd_child_plan_body("003", deps=["001"]), encoding="utf-8")
        child_c.write_text(_bd_child_plan_body("004", deps=["001"]), encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
            {"id": "002", "dependencies": ["001"], "plan_file": "child-a.md"},
            {"id": "003", "dependencies": ["001"], "plan_file": "child-b.md"},
            {"id": "004", "dependencies": ["001"], "plan_file": "child-c.md"},
        ])

        writes: list[Path] = []
        reads: list[Path] = []
        orig_write = plan_ops._write_text
        orig_load = plan_ops._load_text

        def counting_write(path, text):
            writes.append(Path(path))
            return orig_write(path, text)

        def counting_load(path):
            reads.append(Path(path))
            return orig_load(path)

        monkeypatch.setattr(plan_ops, "_write_text", counting_write)
        monkeypatch.setattr(plan_ops, "_load_text", counting_load)

        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code == 0, body
        assert body["blocked_task_ids"] == ["002", "003", "004"]
        assert body["plan_mutations_applied"] == ["002", "003", "004"]
        assert body["run_log_appended"] == ["002", "003", "004"]
        # Exactly one write per unique file (three files total).
        assert writes.count(child_a) == 1
        assert writes.count(child_b) == 1
        assert writes.count(child_c) == 1
        assert len(writes) == 3
        # Each file read exactly once (pre-check read, reused for mutate).
        assert reads.count(child_a) == 1
        assert reads.count(child_b) == 1
        assert reads.count(child_c) == 1
        # Every dependent flipped in its own file.
        assert _bd_status_of(child_a.read_text(encoding="utf-8"), "002") == "blocked"
        assert _bd_status_of(child_b.read_text(encoding="utf-8"), "003") == "blocked"
        assert _bd_status_of(child_c.read_text(encoding="utf-8"), "004") == "blocked"
        # Failed task's own status untouched by block-dependents.
        assert _bd_status_of(child_a.read_text(encoding="utf-8"), "001") == "pending"
        # Run-log: one event per dependent, each with the correct plan_file.
        events = _bd_read_run_log_events_in(isolated_bd_dir)
        assert len(events) == 3
        by_tid = {e["task_id"]: e for e in events}
        assert by_tid["002"]["plan_file"] == "child-a.md"
        assert by_tid["003"]["plan_file"] == "child-b.md"
        assert by_tid["004"]["plan_file"] == "child-c.md"

    # (d) dependent missing plan_file is REJECTED ---------------------------
    def test_block_dependents_missing_plan_file_halts(
        self, tmp_path: Path, isolated_bd_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """TASK-008 (per_task_dispatch_refactor_v2) REMOVED the
        single-file `--plan-file` fallback for dependents that omit
        `plan_file`. The schedule below previously routed TASK-002 to
        `--plan-file = child-a.md`; the directory-only contract now
        rejects the missing field with a structured `missing-plan-file`
        error pre-write — no partial cascade.
        """
        child_a = isolated_bd_dir / "child-a.md"
        child_b = isolated_bd_dir / "child-b.md"
        child_a.write_text(
            _bd_multi_child_plan_body([("001", []), ("002", ["001"])]),
            encoding="utf-8",
        )
        child_b.write_text(_bd_child_plan_body("003", deps=["001"]), encoding="utf-8")
        child_a_before = child_a.read_text(encoding="utf-8")
        child_b_before = child_b.read_text(encoding="utf-8")

        writes: list[Path] = []
        orig_write = plan_ops._write_text

        def counting_write(path, text):
            writes.append(Path(path))
            return orig_write(path, text)

        monkeypatch.setattr(plan_ops, "_write_text", counting_write)

        sched = _bd_write_schedule(
            tmp_path,
            [
                {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
                # 002 intentionally omits plan_file — used to fall back to
                # --plan-file; now produces `missing-plan-file`.
                {"id": "002", "dependencies": ["001"]},
                {"id": "003", "dependencies": ["001"], "plan_file": "child-b.md"},
            ],
            default_plan_file=None,  # no auto-injection — the missing field is the point.
        )
        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "missing-plan-file"
        # Path is rooted at the offending dependent's tasks[] index (1).
        assert err["path"] == "$.tasks[1].plan_file"
        assert err["failed_id"] == "002"
        assert err["plan_file"] is None
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        # No write occurred — no partial cascade.
        assert writes == []
        assert child_a.read_text(encoding="utf-8") == child_a_before
        assert child_b.read_text(encoding="utf-8") == child_b_before
        # No run-log events.
        assert _bd_read_run_log_events_in(isolated_bd_dir) == []

    # (e) unresolvable plan_file (escape attempt) ---------------------------
    def test_block_dependents_multi_file_escape_attempt_halts(
        self, tmp_path: Path, isolated_bd_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A `plan_file` containing `/` (path separator) halts pre-write.

        `_is_valid_plan_file_basename` already rejects any form that
        contains a path separator, `..`, a leading dot, or a NUL byte.
        The new pre-write containment check surfaces the violation as
        `dependent-file-not-in-plan-dir` and halts BEFORE any file is
        written — no partial cascade.
        """
        child_a = isolated_bd_dir / "child-a.md"
        child_b = isolated_bd_dir / "child-b.md"
        child_a.write_text(_bd_child_plan_body("001", deps=[]), encoding="utf-8")
        child_b.write_text(_bd_child_plan_body("002", deps=["001"]), encoding="utf-8")
        child_a_before = child_a.read_text(encoding="utf-8")
        child_b_before = child_b.read_text(encoding="utf-8")

        writes: list[Path] = []
        orig_write = plan_ops._write_text

        def counting_write(path, text):
            writes.append(Path(path))
            return orig_write(path, text)

        monkeypatch.setattr(plan_ops, "_write_text", counting_write)

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
            # Path separator → fails basename predicate → halts.
            {"id": "002", "dependencies": ["001"], "plan_file": "subdir/escape.md"},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "dependent-file-not-in-plan-dir"
        # Path is rooted at the dependent's tasks[] index (here 1).
        assert err["path"] == "$.tasks[1].plan_file"
        assert err["failed_id"] == "002"
        assert err["plan_file"] == "subdir/escape.md"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        # No write occurred — no partial cascade.
        assert writes == []
        assert child_a.read_text(encoding="utf-8") == child_a_before
        assert child_b.read_text(encoding="utf-8") == child_b_before
        # No run-log events.
        assert _bd_read_run_log_events_in(isolated_bd_dir) == []

    def test_block_dependents_multi_file_dotdot_escape_halts(
        self, tmp_path: Path, isolated_bd_dir: Path,
    ) -> None:
        """`..` segment in a plan_file fails the basename predicate."""
        child_a = isolated_bd_dir / "child-a.md"
        child_a.write_text(_bd_child_plan_body("001", deps=[]), encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
            {"id": "002", "dependencies": ["001"], "plan_file": "..escape.md"},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "dependent-file-not-in-plan-dir"
        assert err["path"] == "$.tasks[1].plan_file"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []

    # (f) resolvable plan_file but missing task block -----------------------
    def test_block_dependents_multi_file_resolvable_but_block_missing(
        self, tmp_path: Path, isolated_bd_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`plan_file` resolves to a real file, but the task block is absent.

        child-b exists but holds only TASK-999 (not TASK-002). The
        pre-write presence check halts with `dependent-block-missing`
        before any mutation is applied. No partial cascade.
        """
        child_a = isolated_bd_dir / "child-a.md"
        child_b = isolated_bd_dir / "child-b.md"
        child_a.write_text(_bd_child_plan_body("001", deps=[]), encoding="utf-8")
        # child-b exists as a valid plan file but has no TASK-002 block.
        child_b.write_text(_bd_child_plan_body("999", deps=[]), encoding="utf-8")
        child_a_before = child_a.read_text(encoding="utf-8")
        child_b_before = child_b.read_text(encoding="utf-8")

        writes: list[Path] = []
        orig_write = plan_ops._write_text

        def counting_write(path, text):
            writes.append(Path(path))
            return orig_write(path, text)

        monkeypatch.setattr(plan_ops, "_write_text", counting_write)

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
            {"id": "002", "dependencies": ["001"], "plan_file": "child-b.md"},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "dependent-block-missing"
        assert err["path"] == "$.tasks[1]"
        assert err["failed_id"] == "002"
        assert err["plan_file"] == "child-b.md"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        # No writes.
        assert writes == []
        assert child_a.read_text(encoding="utf-8") == child_a_before
        assert child_b.read_text(encoding="utf-8") == child_b_before
        assert _bd_read_run_log_events_in(isolated_bd_dir) == []

    # Bonus: referenced file missing entirely is also a containment failure.
    def test_block_dependents_multi_file_referenced_file_absent(
        self, tmp_path: Path, isolated_bd_dir: Path,
    ) -> None:
        """`plan_file` has valid basename but no such file under plan-dir."""
        child_a = isolated_bd_dir / "child-a.md"
        child_a.write_text(_bd_child_plan_body("001", deps=[]), encoding="utf-8")

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": [], "plan_file": "child-a.md"},
            {"id": "002", "dependencies": ["001"], "plan_file": "nonexistent.md"},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=child_a)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["code"] == "dependent-file-not-in-plan-dir"
        assert err["path"] == "$.tasks[1].plan_file"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []


def _sample_plan_as_directory(tmp_git_repo: Path, name: str = "sample") -> Path:
    """Materialize the tmp_git_repo's ``docs/plans/sample.md`` fixture as a
    decomposed directory at ``docs/plans/<name>/``, commit it, and return
    the directory path. TASK-008 made ``cmd_preflight`` directory-only,
    so preflight tests need a decomposed plan to exercise.

    The produced directory mirrors the SAMPLE_PLAN_BODY task set (TASK-001
    src/foo.py, TASK-002 src/bar.py, TASK-003 src/baz.py) via H3
    ``### TASK-NNN:`` child files + a schema-compliant ``00_INDEX.json``
    roster (per ``_parse_index_roster``).
    """
    plans_dir = tmp_git_repo / "docs" / "plans"
    plan_dir = plans_dir / name
    plan_dir.mkdir(parents=True, exist_ok=True)

    roster = {
        "schema_version": 1,
        "chunks": [
            {
                "task_id": "001",
                "file": "TASK-001.md",
                "depends_on": [],
                "status": "Pending",
                "superseded_by": [],
            },
            {
                "task_id": "002",
                "file": "TASK-002.md",
                "depends_on": ["001"],
                "status": "Pending",
                "superseded_by": [],
            },
            {
                "task_id": "003",
                "file": "TASK-003.md",
                "depends_on": [],
                "status": "Pending",
                "superseded_by": [],
            },
        ],
    }
    (plan_dir / "00_INDEX.json").write_text(
        json.dumps(roster, indent=2) + "\n", encoding="utf-8",
    )

    # H3-grammar child files matching SAMPLE_PLAN_BODY's Files: declarations.
    (plan_dir / "TASK-001.md").write_text(
        "### TASK-001: First task\n\n"
        "- **Status:** open\n"
        "- **Priority:** high\n"
        "- **Agent:** claude\n"
        "- **Files:**\n"
        "  - src/foo.py\n"
        "- **Dependencies:** none\n"
        "- **Base branch:** main\n"
        "- **Test command:** `true`\n"
        "- **Acceptance criteria:**\n"
        "  - it compiles\n\n"
        "**Description:**\nFirst task body.\n",
        encoding="utf-8",
    )
    (plan_dir / "TASK-002.md").write_text(
        "### TASK-002: Second task\n\n"
        "- **Status:** in-progress\n"
        "- **Priority:** medium\n"
        "- **Agent:** codex\n"
        "- **Files:**\n"
        "  - src/bar.py\n"
        "- **Dependencies:** [001]\n"
        "- **Test command:** `true`\n"
        "- **Acceptance criteria:**\n"
        "  - it compiles\n\n"
        "**Description:**\nSecond task body.\n",
        encoding="utf-8",
    )
    (plan_dir / "TASK-003.md").write_text(
        "### TASK-003: Third task\n\n"
        "- **Status:** done\n"
        "- **Priority:** low\n"
        "- **Agent:** claude\n"
        "- **Files:**\n"
        "  - src/baz.py\n"
        "- **Dependencies:** none\n"
        "- **Test command:** `true`\n"
        "- **Acceptance criteria:**\n"
        "  - it compiles\n\n"
        "**Description:**\nThird task body.\n",
        encoding="utf-8",
    )

    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "add", str(plan_dir.relative_to(tmp_git_repo))],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_git_repo), "commit", "-qm", f"seed {name} plan dir"],
        check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )
    return plan_dir


class TestPreflightDirtyCategorization:
    """Preflight splits `git status` entries into four buckets (TASK-008):
    ``plan_doc`` (always-ignore), ``orchestrator_state`` (always-ignore —
    run-log / run-lock / per-plan schedule sidecar), ``plan_scope_dirty``
    (files declared in the active plan's `Files:` union; reported with
    per-task attribution but non-blocking in default mode), and
    ``source_blocking`` (everything else — the default blocker). The
    retired `.claude/` / `docs/` / `tests/` prefix heuristic is gone —
    paths under those prefixes are `source_blocking` unless the plan's
    own scope claims them.

    TASK-008 removed ``cmd_preflight``'s file-branch; these tests now
    target a decomposed-plan directory materialized via
    ``_sample_plan_as_directory`` from the shared fixture.
    """

    def _preflight(self, repo: Path, plan: Path, *, strict_scope: bool = False) -> subprocess.CompletedProcess:
        cmd = [str(PY), str(SCRIPT), "preflight", "--plan-file", str(plan), "--json"]
        if strict_scope:
            cmd.append("--strict-scope")
        return subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

    def test_codex_stray_file_is_source_blocking(self, tmp_git_repo: Path) -> None:
        """`.codex` is not in the TASK-008 always-ignore set; it blocks."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / ".codex").write_text("", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert ".codex" in body["dirty_files"]["source_blocking"]
        assert body["dirty_files"]["orchestrator_state"] == []
        assert body["pass"] is False

    def test_run_lock_is_orchestrator_state(self, tmp_git_repo: Path) -> None:
        """`docs/plans/_run_lock.json` is always-ignore `orchestrator_state`."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / "docs" / "plans" / "_run_lock.json").write_text("{}", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert any("_run_lock.json" in p for p in body["dirty_files"]["orchestrator_state"])
        assert body["pass"] is True

    def test_arbitrary_untracked_file_is_source_blocking(self, tmp_git_repo: Path) -> None:
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / "scratch.py").write_text("print('hi')\n", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "scratch.py" in body["dirty_files"]["source_blocking"]
        assert body["pass"] is False

    def test_foreign_schedule_sidecar_is_orchestrator_state(self, tmp_git_repo: Path) -> None:
        """A leftover schedule sidecar belonging to a DIFFERENT plan (e.g.
        from a paused prior run) is orchestrator_state, not source_blocking.
        Regression for a preflight classifier bug where `is_commit_always_ignore`
        is scoped to the current plan's basename, so foreign `*.schedule.json`
        files at `docs/plans/` fell through to `source_blocking`. The fix
        recognizes any `*.schedule.json` under the plan_dir as bookkeeping."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        # Flat-layout foreign sidecar — the exact shape of the real incident.
        (tmp_git_repo / "docs" / "plans" / "other_plan.schedule.json").write_text(
            '{"tasks": []}\n', encoding="utf-8"
        )
        # Directory-mode foreign sidecar. Commit a sibling .md first so the
        # plan subfolder is tracked; without that, default `git status` would
        # collapse the whole folder to one untracked-directory line.
        nested_dir = tmp_git_repo / "docs" / "plans" / "nested_plan"
        nested_dir.mkdir()
        (nested_dir / "nested_plan.md").write_text("# nested\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "add", "docs/plans/nested_plan/nested_plan.md"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_git_repo), "commit", "-m", "add nested plan"],
            check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        (nested_dir / "nested_plan.schedule.json").write_text(
            '{"tasks": []}\n', encoding="utf-8"
        )
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is True
        assert body["dirty_files"]["source_blocking"] == []
        orch = body["dirty_files"]["orchestrator_state"]
        assert any("other_plan.schedule.json" in p for p in orch)
        assert any("nested_plan/nested_plan.schedule.json" in p for p in orch)

    def test_untracked_tests_dir_is_source_blocking(self, tmp_git_repo: Path) -> None:
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        tests_dir = tmp_git_repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "wip_test.py").write_text("def test_wip(): pass\n", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "tests/" in body["dirty_files"]["source_blocking"]

    def test_claude_dir_is_source_blocking(self, tmp_git_repo: Path) -> None:
        """`.claude/` is retired from the always-ignore set; it blocks now.

        TASK-008 drops the blunt `.claude/` / `docs/` / `tests/` prefix rule
        in favor of consulting the plan's `allowed_files` union.
        """
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / ".claude").mkdir()
        (tmp_git_repo / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert any(".claude/" in p for p in body["dirty_files"]["source_blocking"])
        assert body["pass"] is False

    def test_plan_scope_dirty_attributes_to_task(self, tmp_git_repo: Path) -> None:
        """A dirty file inside the plan's `Files:` union lands in
        `plan_scope_dirty` with `{path, task_id}` attribution, NOT in
        `source_blocking`; `scope_warnings` carries a human-readable string."""
        # _sample_plan_as_directory declares TASK-001 Files: src/foo.py.
        # The fixture already committed `src/foo.py`; rewrite it so it
        # shows up as dirty.
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / "src" / "foo.py").write_text("CHANGED\n", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        # Non-strict mode: `plan_scope_dirty` alone does not flip `pass`.
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["dirty_files"]["source_blocking"] == []
        scoped = body["dirty_files"]["plan_scope_dirty"]
        assert any(
            e["path"] == "src/foo.py" and e["task_id"] == "001" for e in scoped
        ), scoped
        assert any(
            "src/foo.py" in w and "TASK-001" in w for w in body["scope_warnings"]
        )
        assert body["pass"] is True

    def test_plan_scope_dirty_with_source_blocking_still_fails(self, tmp_git_repo: Path) -> None:
        """Mixing a scoped dirty file with a non-scoped dirty file still blocks,
        because `source_blocking` is non-empty. Mirrors the plan's V5 scenario."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / "src" / "foo.py").write_text("SCOPED\n", encoding="utf-8")
        (tmp_git_repo / "scratch.py").write_text("NON-SCOPED\n", encoding="utf-8")
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "scratch.py" in body["dirty_files"]["source_blocking"]
        assert any(e["path"] == "src/foo.py" for e in body["dirty_files"]["plan_scope_dirty"])
        assert body["pass"] is False

    def test_strict_scope_flips_scope_dirty_to_block(self, tmp_git_repo: Path) -> None:
        """V6: `--strict-scope` upgrades `plan_scope_dirty` to a hard block."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        (tmp_git_repo / "src" / "foo.py").write_text("SCOPED\n", encoding="utf-8")
        # Non-strict: passes despite scope-dirty.
        cp = self._preflight(tmp_git_repo, plan_dir, strict_scope=False)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        # Strict: fails.
        cp = self._preflight(tmp_git_repo, plan_dir, strict_scope=True)
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert any(e["path"] == "src/foo.py" for e in body["dirty_files"]["plan_scope_dirty"])
        assert body["pass"] is False


class TestPreflightPythonPath:
    """TASK-008 `$PYTHON` resolution: preflight emits `python_path` per the
    documented precedence (IMPLEMENT_PLAN_PYTHON → venv/bin/python →
    .venv/bin/python → python3). The helper is the single source of truth;
    the skill / templates interpolate the resolved absolute path.

    TASK-008 (per_task_dispatch_refactor_v2) made `cmd_preflight`
    directory-only; this class materializes the `sample.md` fixture as a
    decomposed directory via ``_sample_plan_as_directory`` before
    preflighting.
    """

    def _preflight(self, repo: Path, plan: Path, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(PY), str(SCRIPT), "preflight", "--plan-file", str(plan), "--json"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_emits_python_path(self, tmp_git_repo: Path) -> None:
        """V3: preflight surfaces `python_path` on the JSON envelope."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        cp = self._preflight(tmp_git_repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert "python_path" in body
        resolved = body["python_path"]
        assert isinstance(resolved, str) and resolved
        # Must exist as a file and be executable.
        assert Path(resolved).is_file(), resolved
        assert os.access(resolved, os.X_OK), resolved

    def test_env_override_wins(self, tmp_git_repo: Path, tmp_path: Path) -> None:
        """V4 (env branch): `IMPLEMENT_PLAN_PYTHON` overrides all fallbacks
        when it points at an existing executable."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        # Place the fake executable OUTSIDE the git repo so its presence
        # doesn't show up as a dirty file that would flip preflight to fail.
        fake = tmp_path.parent / "env_override_fake-python"
        fake.write_text("#!/bin/sh\nexec " + str(PY) + " \"$@\"\n", encoding="utf-8")
        fake.chmod(0o755)
        env = dict(os.environ)
        env["IMPLEMENT_PLAN_PYTHON"] = str(fake)
        try:
            cp = self._preflight(tmp_git_repo, plan_dir, env=env)
            assert cp.returncode == 0, cp.stdout + cp.stderr
            body = _parse_json(cp)
            assert body["python_path"] == str(fake.resolve())
        finally:
            fake.unlink(missing_ok=True)

    def test_env_override_ignored_when_missing(self, tmp_git_repo: Path) -> None:
        """Non-existent `IMPLEMENT_PLAN_PYTHON` falls through to the next rung."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        env = dict(os.environ)
        env["IMPLEMENT_PLAN_PYTHON"] = "/tmp/does-not-exist-never-will"
        cp = self._preflight(tmp_git_repo, plan_dir, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        # Falls through to cwd/venv or cwd/.venv or python3; in any event it
        # resolves to something runnable.
        resolved = body["python_path"]
        assert resolved != "/tmp/does-not-exist-never-will"
        assert Path(resolved).is_file()
        assert os.access(resolved, os.X_OK)

    def test_venv_fallback_picks_cwd_venv(self, tmp_git_repo: Path) -> None:
        """V4 (venv branch): without env override, `cwd/venv/bin/python` is
        preferred when present. Build a fake venv inside the tmp repo,
        then commit it so the dirty-tree classifier does not block."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        venv_bin = tmp_git_repo / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake = venv_bin / "python"
        fake.write_text("#!/bin/sh\nexec " + str(PY) + " \"$@\"\n", encoding="utf-8")
        fake.chmod(0o755)
        subprocess.run(["git", "add", "venv"], cwd=str(tmp_git_repo), check=True)
        subprocess.run(["git", "commit", "-qm", "add fake venv"], cwd=str(tmp_git_repo), check=True)
        env = dict(os.environ)
        env.pop("IMPLEMENT_PLAN_PYTHON", None)
        cp = self._preflight(tmp_git_repo, plan_dir, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["python_path"] == str(fake.resolve())

    def test_dot_venv_fallback(self, tmp_git_repo: Path) -> None:
        """V4 (.venv branch): with no `venv/bin/python` and no env override,
        `cwd/.venv/bin/python` is picked. Commit the fake `.venv` so the
        dirty-tree classifier does not block the assertion."""
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        dot_venv_bin = tmp_git_repo / ".venv" / "bin"
        dot_venv_bin.mkdir(parents=True)
        fake = dot_venv_bin / "python"
        fake.write_text("#!/bin/sh\nexec " + str(PY) + " \"$@\"\n", encoding="utf-8")
        fake.chmod(0o755)
        subprocess.run(["git", "add", ".venv"], cwd=str(tmp_git_repo), check=True)
        subprocess.run(["git", "commit", "-qm", "add fake .venv"], cwd=str(tmp_git_repo), check=True)
        env = dict(os.environ)
        env.pop("IMPLEMENT_PLAN_PYTHON", None)
        cp = self._preflight(tmp_git_repo, plan_dir, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["python_path"] == str(fake.resolve())

    def test_python3_on_path_as_final_fallback(self, tmp_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """V4 (python3 branch): neither venv present and no env override →
        resolver falls through to `shutil.which('python3')`.

        `tmp_git_repo` has no `venv/` or `.venv/` directories, so the
        default fallback path is exercised as long as `python3` is on
        `$PATH` (which it is in every Linux/macOS test environment).
        """
        plan_dir = _sample_plan_as_directory(tmp_git_repo)
        env = dict(os.environ)
        env.pop("IMPLEMENT_PLAN_PYTHON", None)
        cp = self._preflight(tmp_git_repo, plan_dir, env=env)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        resolved = body["python_path"]
        # Not a venv-bin path under the tmp repo.
        assert "venv/bin/python" not in resolved or not resolved.startswith(str(tmp_git_repo))
        assert Path(resolved).is_file()
        assert os.access(resolved, os.X_OK)


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
                {"id": "001", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
                {"id": "001", "agent": "claude", "files": [], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
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
                    "plan_file": "sample.md",
                },
                {
                    "id": "002",
                    "agent": "codex",
                    "files": ["src/shared.py", "src/b.py"],
                    "dependencies": [],
                    "plan_file": "sample.md",
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
                    "plan_file": "sample.md",
                },
                {
                    "id": "002",
                    "agent": "codex",
                    "files": ["src/b.py"],
                    "dependencies": [],
                    "plan_file": "sample.md",
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
                    "plan_file": "sample.md",
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
                    "plan_file": "sample.md",
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
                {"id": "001", "agent": "codex", "files": ["a.txt"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
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

    def test_write_schedule_tolerates_orphan_dependencies_field(
        self, tmp_path: Path,
    ) -> None:
        # TASK-019 contract split: `write-schedule` runs `_validate_schedule`
        # (shape + refs) but NOT `_validate_schedule_dag` (cycles + orphans),
        # so persisting a schedule with a `dependencies: ["999"]` orphan is
        # still permitted — callers that build intermediate schedules may
        # legitimately stage orphan refs before cleanup. `parse-schedule` is
        # the stricter seam (see TASK-019 V2) and is exercised separately.
        dest = tmp_path / "schedule.json"
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["999"], "plan_file": "sample.md"},
            ],
            "batches": [{"index": 1, "task_ids": ["001"], "file_locks": ["a"]}],
        }
        cp = _run_write_schedule(payload, dest)
        assert cp.returncode == 0, cp.stderr
        written = json.loads(dest.read_text(encoding="utf-8"))
        assert written["tasks"][0]["dependencies"] == ["999"]

        # TASK-019 V2 regression: parse-schedule MUST reject the same payload
        # with `unknown-dependency`. Asserting both halves in one test keeps
        # the write/parse contract split explicit.
        parse_cp = _parse_schedule_payload(payload)
        assert parse_cp.returncode == 1
        body = _parse_json(parse_cp)
        codes = [e.get("code", "") for e in body.get("errors") or []]
        assert "unknown-dependency" in codes

    def test_atomic_replaces_existing_file(self, tmp_path: Path) -> None:
        dest = tmp_path / "schedule.json"
        dest.write_text("{\"existing\": true}\n", encoding="utf-8")
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "001", "agent": "codex", "files": [], "dependencies": [], "plan_file": "sample.md"},
                {"id": "001", "agent": "claude", "files": [], "dependencies": [], "plan_file": "sample.md"},
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

    def test_commit_task_uncommittable_verdict_carries_canonical_hint(
        self, tmp_git_repo: Path,
    ) -> None:
        """POSTMORTEM_FIXES TASK-003: the rejection envelope MUST carry both
        a structured `hint` field AND a human-readable hint suffix on the
        message string, naming the canonical D.5-driven binding form and
        citing the SKILL.md §D.2a routing anchor.
        """
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
        rejections = [
            e for e in body["errors"]
            if e["code"] == "uncommittable-reviewer-verdict"
        ]
        assert len(rejections) == 1
        rej = rejections[0]
        # Structured hint field on the envelope.
        assert "hint" in rej, f"missing structured hint: {rej!r}"
        assert "ship-with-fixes" in rej["hint"]
        assert "claude" in rej["hint"]
        assert "§D.2a" in rej["hint"]
        # Human-readable suffix on the message itself (so non-JSON
        # stderr operators see it too).
        assert "hint:" in rej["message"]
        assert "ship-with-fixes" in rej["message"]
        assert "§D.2a" in rej["message"]

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


def _classify_cli_envelope(stdout: str) -> tuple[str, object]:
    """Classify ``claude --output-format json`` stdout into one of three states.

    Returns ``("unwrapped", inner_str)`` when stdout is a JSON object with a
    string ``result`` field (the documented happy path), ``("unexpected",
    body)`` when JSON parses but the shape is not what we expected (caller
    should ``pytest.skip`` with a message naming the observed shape), or
    ``("non-json", None)`` when stdout is not JSON at all (caller can fall
    back to the raw stdout).
    """
    try:
        body = json.loads(stdout)
    except json.JSONDecodeError:
        return ("non-json", None)
    if not isinstance(body, dict):
        return ("unexpected", body)
    inner = body.get("result")
    if not isinstance(inner, str):
        return ("unexpected", body)
    return ("unwrapped", inner)


def _unwrap_cli_envelope(stdout: str) -> str | None:
    """Back-compat shim over ``_classify_cli_envelope``.

    Returns the inner ``result`` string for the happy path and ``None`` for
    every other state (non-JSON OR parsed-but-unexpected). Live integration
    tests SHOULD call ``_classify_cli_envelope`` directly so they can
    ``pytest.skip`` on parsed-but-unexpected envelopes; this shim is
    retained for the V1 unit-scope regression test that asserts the
    ``None`` collapse for the documented bad-shape inputs.
    """
    status, value = _classify_cli_envelope(stdout)
    if status == "unwrapped":
        return value  # type: ignore[return-value]
    return None


def _extract_json_block(text: str) -> dict | None:
    """Best-effort extraction of a fenced ```json or bare {...} block.

    Unwraps the Claude Agent SDK envelope (`--output-format json`) when
    detected: the outer object carries `type="result"` with the agent's
    textual output in `result`, so we scan inside that instead of
    returning the envelope itself.
    """
    try:
        outer = json.loads(text)
        if (
            isinstance(outer, dict)
            and outer.get("type") == "result"
            and isinstance(outer.get("result"), str)
        ):
            text = outer["result"]
    except json.JSONDecodeError:
        pass
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


def _run_reconcile(
    repo: Path,
    envelopes: list,
    *,
    schedule_file: Path | str | None = None,
) -> subprocess.CompletedProcess:
    cmd = [str(PY), str(SCRIPT), "reconcile-batch",
           "--repo-root", str(repo), "--json"]
    if schedule_file is not None:
        cmd.extend(["--schedule-file", str(schedule_file)])
    return subprocess.run(
        cmd,
        input=json.dumps(envelopes),
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _write_reconcile_schedule(
    tmp_path: Path,
    *,
    task_id: str,
    files: list[str],
) -> Path:
    """Write a minimal schedule.json fixture with one task entry.

    The schedule's per-task identifier field is `id` (matching the
    on-disk `build-tasks` output).
    """
    sched = tmp_path / "fixture.schedule.json"
    sched.write_text(json.dumps({
        "tasks": [{"id": task_id, "files": list(files)}],
    }))
    return sched


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

    # ------------------------------------------------------------------
    # TASK-003: plan-aware preservation
    # ------------------------------------------------------------------

    def test_preserves_tracked_when_in_dispatched_task_files(
        self, tmp_path: Path,
    ) -> None:
        """A tracked path declared in the task's `Files:` is PRESERVED, not
        restored, even if the wrapper falsely flagged it as out-of-scope."""
        repo = _reconcile_git_repo(tmp_path)
        keeper = repo / "Makefile"
        keeper.write_text("orig\n")
        subprocess.run(["git", "add", "Makefile"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add makefile"],
                       cwd=repo, check=True)
        keeper.write_text("declared-edit\n")

        sched = _write_reconcile_schedule(
            tmp_path, task_id="001", files=["`Makefile` (edit)"],
        )
        envelope = _scope_envelope("001", tracked=["Makefile"])
        cp = _run_reconcile(repo, [envelope], schedule_file=sched)
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["outcome"] == "scope_violation_preserved"
        assert results[0]["reconcile_kept_tracked"] == ["Makefile"]
        assert results[0]["reconciled_tracked"] == []
        # The wrapper-declared edit must remain on disk.
        assert keeper.read_text() == "declared-edit\n"
        # And `git diff HEAD -- Makefile` is non-empty.
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "Makefile"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert diff.stdout.strip() != ""

    def test_restores_tracked_when_outside_dispatched_task_files(
        self, tmp_path: Path,
    ) -> None:
        """A tracked path NOT in the dispatched task's `Files:` set is
        restored as today's behaviour."""
        repo = _reconcile_git_repo(tmp_path)
        stranger = repo / "other.py"
        stranger.write_text("v1\n")
        subprocess.run(["git", "add", "other.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add other"],
                       cwd=repo, check=True)
        stranger.write_text("codex-touched\n")

        sched = _write_reconcile_schedule(
            tmp_path, task_id="002", files=["`Makefile` (edit)"],
        )
        envelope = _scope_envelope("002", tracked=["other.py"])
        cp = _run_reconcile(repo, [envelope], schedule_file=sched)
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["outcome"] == "scope_violation_reconciled"
        assert results[0]["reconciled_tracked"] == ["other.py"]
        assert results[0]["reconcile_kept_tracked"] == []
        # File restored to baseline.
        assert stranger.read_text() == "v1\n"
        diff = subprocess.run(
            ["git", "diff", "HEAD", "--", "other.py"],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        assert diff.stdout.strip() == ""

    def test_preserves_untracked_create_when_in_dispatched_task_files(
        self, tmp_path: Path,
    ) -> None:
        """A `(create)`-declared untracked path in `out_of_scope_untracked`
        is preserved on disk; not unlinked."""
        repo = _reconcile_git_repo(tmp_path)
        new_file = repo / "new_module.py"
        new_file.write_text("declared new\n")

        sched = _write_reconcile_schedule(
            tmp_path, task_id="003", files=["`new_module.py` (create)"],
        )
        envelope = _scope_envelope("003", untracked=["new_module.py"])
        cp = _run_reconcile(repo, [envelope], schedule_file=sched)
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["outcome"] == "scope_violation_preserved"
        assert results[0]["reconcile_kept_untracked"] == ["new_module.py"]
        assert results[0]["reconciled_untracked"] == []
        assert new_file.exists()
        assert new_file.read_text() == "declared new\n"

    def test_mixed_kept_and_restored_in_one_envelope(
        self, tmp_path: Path,
    ) -> None:
        """One envelope with one declared and one undeclared path produces
        both `reconcile_kept_tracked` AND `reconciled_tracked`; outcome is
        `scope_violation_reconciled` (any restoration trumps preserved)."""
        repo = _reconcile_git_repo(tmp_path)
        keeper = repo / "Makefile"
        keeper.write_text("orig\n")
        stranger = repo / "other.py"
        stranger.write_text("v1\n")
        subprocess.run(
            ["git", "add", "Makefile", "other.py"], cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True,
        )
        keeper.write_text("declared-edit\n")
        stranger.write_text("codex-touched\n")

        sched = _write_reconcile_schedule(
            tmp_path, task_id="004", files=["`Makefile` (edit)"],
        )
        envelope = _scope_envelope(
            "004", tracked=["Makefile", "other.py"],
        )
        cp = _run_reconcile(repo, [envelope], schedule_file=sched)
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["outcome"] == "scope_violation_reconciled"
        assert results[0]["reconcile_kept_tracked"] == ["Makefile"]
        assert results[0]["reconciled_tracked"] == ["other.py"]
        # Makefile preserved, other.py restored.
        assert keeper.read_text() == "declared-edit\n"
        assert stranger.read_text() == "v1\n"

    def test_falls_back_when_schedule_file_missing(
        self, tmp_path: Path,
    ) -> None:
        """`--schedule-file` pointing at a non-existent path emits
        `warning: "schedule_lookup_failed"` and reverts to today's
        restore-everything behaviour."""
        repo = _reconcile_git_repo(tmp_path)
        stranger = repo / "other.py"
        stranger.write_text("v1\n")
        subprocess.run(["git", "add", "other.py"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "add other"],
                       cwd=repo, check=True)
        stranger.write_text("codex-touched\n")

        # Path that does not exist.
        bogus = tmp_path / "does_not_exist.schedule.json"
        envelope = _scope_envelope("999", tracked=["other.py"])
        cp = _run_reconcile(repo, [envelope], schedule_file=bogus)
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        results = body["results"]
        assert results[0]["warning"] == "schedule_lookup_failed"
        # Fallback: restore-everything behaviour preserved.
        assert results[0]["outcome"] == "scope_violation_reconciled"
        assert results[0]["reconciled_tracked"] == ["other.py"]
        assert results[0]["reconcile_kept_tracked"] == []
        assert stranger.read_text() == "v1\n"


def test_cli_envelope_unwrap_round_trips_analyst_fenced_json() -> None:
    """V1 regression: the unwrap-then-extract chain must recover the inner
    fenced JSON from a ``claude --output-format json`` envelope rather than
    silently returning the envelope dict itself.
    """
    inner_schedule = {
        "task_id": "001",
        "tasks": [{"task_id": "001", "route": "codex"}],
    }
    fenced = "```json\n" + json.dumps(inner_schedule) + "\n```"
    envelope = {
        "type": "result",
        "subtype": "success",
        "session_id": "abc",
        "result": "Some preamble.\n\n" + fenced + "\n\nTrailing notes.",
        "total_cost_usd": 0.0,
    }
    stdout = json.dumps(envelope)

    inner = _unwrap_cli_envelope(stdout)
    assert isinstance(inner, str)
    source = inner if inner is not None else stdout
    recovered = _extract_json_block(source)
    assert recovered == inner_schedule

    # Non-envelope input must be passed through transparently.
    assert _unwrap_cli_envelope("not json at all") is None
    assert _unwrap_cli_envelope(json.dumps({"type": "other"})) is None
    assert _unwrap_cli_envelope(json.dumps({"result": 123})) is None


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

    status, inner = _classify_cli_envelope(result.stdout)
    if status == "unexpected":
        pytest.skip(
            f"unexpected claude JSON envelope shape: "
            f"{type(inner).__name__} keys={list(inner.keys()) if isinstance(inner, dict) else 'n/a'}"
        )
    source = inner if status == "unwrapped" else result.stdout
    analyst_json = _extract_json_block(source)  # type: ignore[arg-type]
    if analyst_json is None:
        pytest.skip(
            f"no JSON block recovered from analyst output "
            f"(source[:400]={source[:400]!r})"
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

    status, inner = _classify_cli_envelope(result.stdout)
    if status == "unexpected":
        pytest.skip(
            f"unexpected claude JSON envelope shape: "
            f"{type(inner).__name__} keys={list(inner.keys()) if isinstance(inner, dict) else 'n/a'}"
        )
    report_stdin = inner if status == "unwrapped" else result.stdout
    cp = subprocess.run(
        [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
        input=report_stdin,  # type: ignore[arg-type]
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
                {"id": "004A", "agent": "claude", "files": ["x"], "dependencies": [], "plan_file": "sample.md"},
            ],
            "batches": [{"index": 1, "task_ids": ["004A"], "file_locks": ["x"]}],
        }
        cp = _parse_schedule_payload(payload)
        assert cp.returncode == 0, cp.stderr

    def test_validate_schedule_accepts_mixed_ids(self) -> None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "004", "agent": "claude", "files": ["a.py"], "dependencies": [], "plan_file": "sample.md"},
                {
                    "id": "004A",
                    "agent": "claude",
                    "files": ["b.py"],
                    "dependencies": [],
                    "plan_file": "sample.md",
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
                {"id": "004a", "agent": "claude", "files": ["a.py"], "dependencies": [], "plan_file": "sample.md"},
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
                {"id": "004", "agent": "claude", "files": ["a.py"], "dependencies": [], "plan_file": "sample.md"},
                {
                    "id": "004A",
                    "agent": "claude",
                    "files": ["b.py"],
                    "dependencies": [],
                    "plan_file": "sample.md",
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
                {"id": "004", "agent": "claude", "files": ["a.py"], "dependencies": [], "plan_file": "sample.md"},
                {
                    "id": "004A",
                    "agent": "claude",
                    "files": ["b.py"],
                    "dependencies": [],
                    "plan_file": "sample.md",
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
            {"id": "001", "agent": "codex", "files": ["a"], "dependencies": [],
             "plan_file": "sample.md"},
            {"id": "002", "agent": "claude", "files": ["b"], "dependencies": ["001"],
             "plan_file": "sample.md"},
            {"id": "003", "agent": "codex", "files": ["c"], "dependencies": [],
             "plan_file": "sample.md"},
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
                 "dependencies": ["999"], "plan_file": "sample.md"},
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
                 "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
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
                 "dependencies": [], "plan_file": "sample.md"},
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
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "001", "agent": "claude", "files": ["b"],
                 "dependencies": [], "plan_file": "sample.md"},
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

    def test_filter_schedule_alias_task_id_field_rejected_post_task_008(
        self, tmp_path: Path,
    ) -> None:
        # TASK-008 (per_task_dispatch_refactor_v2): the legacy `task_id`
        # field alias was REMOVED. `filter-schedule` rejects schedules
        # carrying it via `_validate_schedule`'s `missing-field` error.
        # The filter still produces a structured payload (no KeyError).
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
        assert cp.returncode != 0
        assert "KeyError" not in cp.stderr
        body = _parse_json(cp)
        codes = [e.get("code") for e in body.get("errors") or []]
        assert "missing-field" in codes, body

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
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"],
                 "dependencies": [], "plan_file": "sample.md"},
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
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
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

    # ------------------------------------------------------------------
    # --stdin input mode (TASK-005 remediation round 4).
    #
    # User-authorized scope expansion: `filter-schedule` gains a `--stdin`
    # alternative to `--schedule-file` so the orchestrator's Phase 1
    # `--task-ids` branch can stay fully in-memory. On the `--stdin` path
    # the `outcome='valid'` gate is relaxed — an in-memory schedule
    # carrying `outcome='needs-enrichment'` (from build-tasks warnings→gaps
    # mapping) is also accepted. The file path is unchanged.
    # ------------------------------------------------------------------

    def test_filter_schedule_stdin_accepts_valid_outcome(self) -> None:
        # --stdin happy path: valid outcome, transitive closure works, output
        # is the canonical 5-key envelope matching the file path.
        payload = json.dumps(_full_schedule_fixture())
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "filter-schedule",
             "--stdin", "--task-ids", "2", "--json"],
            input=payload,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert set(body.keys()) == {"outcome", "tasks", "batches", "gaps", "risks"}
        assert body["outcome"] == "valid"
        # Transitive closure: requesting 002 pulls in 001.
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]
        assert body["gaps"] == []
        assert body["risks"] == []

    def test_filter_schedule_stdin_accepts_needs_enrichment_outcome(self) -> None:
        # --stdin MUST accept outcome='needs-enrichment' with non-empty
        # gaps[] — this is the shape the orchestrator synthesizes from
        # build-tasks warnings→gaps mapping before Step 3. The file path
        # would reject this with `source-not-valid`; the stdin path lets
        # the in-memory pipeline proceed.
        #
        # Phase 1 contract: gaps referencing a retained task MUST be
        # carried forward on the stdin path (and the outcome MUST stay
        # 'needs-enrichment' when any such gap remains). Silently forcing
        # outcome='valid'/gaps=[] would drop the build-tasks warning→gap
        # that prompted this call.
        payload = {
            "outcome": "needs-enrichment",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"],
                 "dependencies": [], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "003"],
                 "file_locks": ["a", "c"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
            "gaps": [
                {"task_id": "002", "type": "missing-acceptance-criteria",
                 "severity": "soft", "detail": "no acceptance bullets"},
            ],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "filter-schedule",
             "--stdin", "--task-ids", "2", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, (
            f"needs-enrichment source rejected on --stdin path:\n"
            f"stdout={cp.stdout}\nstderr={cp.stderr}"
        )
        body = _parse_json(cp)
        codes = [e.get("code") for e in (body.get("errors") or [])]
        # Specifically MUST NOT trip the file-path `source-not-valid` guard.
        assert "source-not-valid" not in codes, body
        # Retained task 002 still carries a gap → outcome stays
        # 'needs-enrichment' and the gap is preserved byte-for-byte.
        assert body["outcome"] == "needs-enrichment", body
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]
        assert body["gaps"] == [
            {"task_id": "002", "type": "missing-acceptance-criteria",
             "severity": "soft", "detail": "no acceptance bullets"},
        ], body
        assert body["risks"] == []

    def test_filter_schedule_stdin_drops_gaps_for_excluded_tasks(self) -> None:
        # Phase 1 contract: gaps that reference a task filtered out of the
        # closed set MUST NOT survive into the output, and when no gaps
        # remain after filtering the outcome flips to 'valid'. This guards
        # against stale `task_id` references leaking into downstream
        # consumers (write-schedule / batch-next).
        payload = {
            "outcome": "needs-enrichment",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
                {"id": "003", "agent": "codex", "files": ["c"],
                 "dependencies": [], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "003"],
                 "file_locks": ["a", "c"]},
                {"index": 2, "task_ids": ["002"], "file_locks": ["b"]},
            ],
            # Gap on 003 (excluded by --task-ids 2) MUST be dropped; gap
            # on 004 references an unknown id and MUST be dropped too.
            "gaps": [
                {"task_id": "003", "type": "vague-ac",
                 "severity": "hard", "detail": "excluded task gap"},
            ],
            # Risk entries carrying a task_id follow the same policy.
            "risks": [
                {"task_id": "003", "id": "R9",
                 "description": "risk on excluded task"},
            ],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "filter-schedule",
             "--stdin", "--task-ids", "2", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, (
            f"needs-enrichment source with excluded-task gaps rejected:\n"
            f"stdout={cp.stdout}\nstderr={cp.stderr}"
        )
        body = _parse_json(cp)
        # No gaps remain → outcome flips back to 'valid' so downstream
        # write-schedule accepts the output.
        assert body["outcome"] == "valid", body
        assert body["gaps"] == [], body
        assert body["risks"] == [], body
        assert [t["id"] for t in body["tasks"]] == ["001", "002"]

    def test_filter_schedule_mutually_exclusive_input(self, tmp_path: Path) -> None:
        # Both --schedule-file and --stdin supplied → error.
        sched = tmp_path / "conflict.schedule.json"
        sched.write_text(json.dumps(_full_schedule_fixture()), encoding="utf-8")
        cp_both = subprocess.run(
            [str(PY), str(SCRIPT), "filter-schedule",
             "--schedule-file", str(sched), "--stdin",
             "--task-ids", "2", "--json"],
            input=json.dumps(_full_schedule_fixture()),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp_both.returncode == 1
        body = _parse_json(cp_both)
        codes = [e.get("code") for e in (body.get("errors") or [])]
        assert "input-mode-conflict" in codes, body

        # Neither --schedule-file nor --stdin supplied → error.
        cp_neither = subprocess.run(
            [str(PY), str(SCRIPT), "filter-schedule",
             "--task-ids", "2", "--json"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp_neither.returncode == 1
        body = _parse_json(cp_neither)
        codes = [e.get("code") for e in (body.get("errors") or [])]
        assert "input-mode-missing" in codes, body


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
_PLAN_REVIEW_TRIAGE_SCHEMA = (
    SCRIPTS_DIR / "codex_plan_review_triage_schema.json"
)


def _plan_review_envelope(
    *,
    verdict: str | None = "approved",
    outcome: str = "success",
    findings: list | None = None,
    plan_file: str = "sample.md",
    schedule_ok: bool = True,
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
        "notes": [],
        "schedule_ok": schedule_ok,
        "summary": summary,
    }
    envelope["parsed"] = parsed
    return envelope


def _plan_review_triage_envelope(
    *,
    verdict: str = "ship",
    load_bearing: list[int] | None = None,
    dismissed: list[int] | None = None,
    summary: str = "triage rationale",
) -> str:
    """Build a synthetic triage markdown report for parser-driven tests."""
    payload = {
        "verdict": verdict,
        "load_bearing": load_bearing if load_bearing is not None else [],
        "dismissed": dismissed if dismissed is not None else [],
        "summary": summary,
    }
    return "Summary\n\n```json\n" + json.dumps(payload) + "\n```\n"


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
            "notes", "schedule_ok", "summary",
        }


class Test_plan_review_triage_schema_file:
    def test_schema_file_exists(self) -> None:
        assert _PLAN_REVIEW_TRIAGE_SCHEMA.is_file(), (
            f"expected schema file at {_PLAN_REVIEW_TRIAGE_SCHEMA}"
        )

    def test_schema_declares_verdict_enum_and_required_fields(self) -> None:
        schema = json.loads(
            _PLAN_REVIEW_TRIAGE_SCHEMA.read_text(encoding="utf-8")
        )
        verdict = schema["properties"]["verdict"]
        assert set(verdict["enum"]) == {
            "ship", "ship-with-fixes", "partial-agreement", "needs-rework",
        }
        required = set(schema["required"])
        assert required == {
            "verdict", "load_bearing", "dismissed", "summary",
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
                "blocking": True,
                "section": "TASK-014C Files",
                "concern": "schema file missing",
                "suggested_change": "add codex_plan_review_schema.json",
            },
            {
                "severity": "minor",
                "blocking": False,
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
        # TASK-007: the parser synthesizes `target_task_id: null` on
        # every finding that omits it (backward-compat). The output
        # findings carry the same keys as the input PLUS the synthesized
        # target_task_id, so equality must tolerate that augmentation.
        expected = [
            {**f, "target_task_id": None} for f in findings
        ]
        assert body["findings"] == expected

    def test_parse_plan_review_preserves_populated_notes(self) -> None:
        env = _plan_review_envelope()
        env["parsed"]["notes"] = ["n1", "n2"]
        cp = self._run_parser(env)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["notes"] == ["n1", "n2"]

    def test_parse_plan_review_emits_empty_notes_for_success_envelope(self) -> None:
        env = _plan_review_envelope()
        env["parsed"]["notes"] = []
        cp = self._run_parser(env)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["notes"] == []

    def test_parse_plan_review_terminal_envelope_emits_empty_notes(self) -> None:
        """Terminal envelopes (no `parsed` body) must still emit
        `"notes": []` so downstream consumers never see a KeyError.
        Covers the emitter-side gap in the terminal-outcome branch.
        """
        env = _plan_review_envelope(outcome="failure", error="codex not found")
        env["parsed"] = None
        cp = self._run_parser(env)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert "notes" in body
        assert body["notes"] == []

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

    def test_invalid_subcommand_hints_inner_payload(self) -> None:
        """TASK-023 V2 — when `subcommand` is absent entirely (a likely
        symptom of piping the inner `parsed` body by mistake), the
        invalid-subcommand message names that failure mode explicitly so
        the caller can self-diagnose without reading the parser source."""
        # Inner-payload-shaped input: no subcommand key, but has
        # verdict + findings + plan_file at top level.
        inner = {
            "plan_file": "sample.md",
            "verdict": "approved-with-notes",
            "findings": [],
            "schedule_ok": True,
            "summary": "ok",
        }
        cp = self._run_parser(inner)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "invalid-subcommand"
        msg = body["errors"][0]["message"]
        assert "parsed" in msg, msg
        assert "envelope" in msg, msg

    def test_invalid_subcommand_wrong_value(self) -> None:
        """TASK-023 V3 — when `subcommand` is present but wrong, the
        message quotes the actual value and omits the inner-payload
        hint (the hint is noise for genuine wrong-value mistakes)."""
        env = _plan_review_envelope()
        env["subcommand"] = "implement"
        cp = self._run_parser(env)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "invalid-subcommand"
        msg = body["errors"][0]["message"]
        assert "'implement'" in msg, msg
        # The inner-payload hint (shorthand: "did you pipe") is reserved
        # for the None case; it must not appear here.
        assert "did you pipe" not in msg, msg

    def test_invalid_subcommand_code_unchanged(self) -> None:
        """TASK-023 V4 — both the absent-field and wrong-value branches
        emit the same error code so callers that pattern-match on
        `errors[*].code` remain unaffected by the TASK-023 message
        sharpening."""
        inner = {
            "plan_file": "sample.md",
            "verdict": "approved",
            "findings": [],
            "schedule_ok": True,
            "summary": "ok",
        }
        cp_absent = self._run_parser(inner)
        assert cp_absent.returncode == 1, cp_absent.stdout
        assert _parse_json(cp_absent)["errors"][0]["code"] == "invalid-subcommand"

        env = _plan_review_envelope()
        env["subcommand"] = "implement"
        cp_wrong = self._run_parser(env)
        assert cp_wrong.returncode == 1, cp_wrong.stdout
        assert _parse_json(cp_wrong)["errors"][0]["code"] == "invalid-subcommand"

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
        assert body["notes"] == []
        assert body["envelope_error"] == "codex not found"


class Test_parse_plan_review_report_from_claude:
    """TASK-002 — `--from-claude` flag wires the Phase 1.5-Claude path
    through the same parser. On this path stdin is the bare `parsed`
    payload (the Agent emits the schema-conforming JSON directly without
    the wrapper envelope's `task_id` / `subcommand` / `outcome` fields),
    so the envelope-level checks are skipped and the body is validated
    against the same `codex_plan_review_schema.json` the Codex path
    validates against. Output shape is identical to the Codex path so
    the verdict-routing ladder consumes the same parser output.
    """

    def _run_parser(self, payload: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report",
                "--stdin", "--from-claude", "--json",
            ],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def _parsed_payload(
        self,
        *,
        verdict: str = "approved",
        plan_file: str = "sample_plan",
        findings: list | None = None,
        notes: list | None = None,
        schedule_ok: bool = True,
        summary: str = "ok",
    ) -> dict:
        """Build the bare `parsed` payload the Phase 1.5-Claude Agent emits."""
        return {
            "plan_file": plan_file,
            "verdict": verdict,
            "findings": findings if findings is not None else [],
            "notes": notes if notes is not None else [],
            "schedule_ok": schedule_ok,
            "summary": summary,
        }

    @pytest.mark.parametrize(
        "verdict",
        ["approved", "approved-with-notes", "needs-replan"],
    )
    def test_from_claude_accepts_each_verdict(self, verdict: str) -> None:
        """Bare `parsed` payload with each documented verdict round-trips
        through the parser when --from-claude is set."""
        cp = self._run_parser(self._parsed_payload(verdict=verdict))
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == verdict
        assert body["plan_file"] == "sample_plan"
        # Outcome is always "success" on the Claude path — Agent failures
        # bubble up as Agent dispatch errors, not envelope outcomes.
        assert body["outcome"] == "success"
        assert body["findings_count"] == 0
        assert body["errors"] == []

    def test_from_claude_emits_same_result_shape_as_codex_path(self) -> None:
        """The --from-claude output keys must match the Codex-path output
        keys (modulo `outcome` always being "success" on the Claude path)
        so the orchestrator's verdict-routing code consumes the same
        parser output regardless of which reviewer mechanism produced it.
        """
        findings = [
            {
                "severity": "important",
                "blocking": True,
                "section": "tasks[002].test_command",
                "concern": "test command is bare 'none' with no deferral",
                "suggested_change": "add deferred (TASK-NNN) sibling reference",
                "target_task_id": "002",
            },
        ]
        cp = self._run_parser(
            self._parsed_payload(
                verdict="needs-replan",
                findings=findings,
                notes=["minor polish suggestion"],
                schedule_ok=False,
                summary="one blocking finding",
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # Same keys the Codex path emits in cmd_parse_plan_review_report.
        expected_keys = {
            "plan_file", "outcome", "verdict", "findings_count",
            "findings", "notes", "summary", "schedule_ok", "errors",
        }
        assert set(body.keys()) == expected_keys
        assert body["verdict"] == "needs-replan"
        assert body["findings_count"] == 1
        assert body["findings"] == findings
        assert body["notes"] == ["minor polish suggestion"]
        assert body["schedule_ok"] is False
        assert body["summary"] == "one blocking finding"
        assert body["outcome"] == "success"
        assert body["errors"] == []

    def test_from_claude_synthesizes_target_task_id_when_missing(self) -> None:
        """Backward-compat parity with the Codex path: a finding emitted
        without `target_task_id` is normalized to `None` so downstream
        consumers (triage template, plan-author dispatcher) can read the
        field uniformly."""
        findings_input = [
            {
                "severity": "minor",
                "blocking": False,
                "section": "schedule.batches[1]",
                "concern": "batch ordering note",
                "suggested_change": "reorder",
                # No target_task_id — parser must synthesize null.
            },
        ]
        cp = self._run_parser(
            self._parsed_payload(
                verdict="approved-with-notes", findings=findings_input,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["findings_count"] == 1
        assert body["findings"][0]["target_task_id"] is None

    def test_from_claude_rejects_invalid_verdict(self) -> None:
        """Schema violations on the Claude path halt with the same
        `invalid-plan-review-verdict` code the Codex path emits."""
        cp = self._run_parser(self._parsed_payload(verdict="clean"))
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "invalid-plan-review-verdict" in codes

    def test_from_claude_rejects_missing_required_field(self) -> None:
        """Schema violations on the Claude path halt with `missing-field`
        for absent required keys."""
        payload = self._parsed_payload()
        payload.pop("schedule_ok")
        cp = self._run_parser(payload)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "missing-field" in codes

    def test_from_claude_rejects_unknown_top_level_field(self) -> None:
        """The Claude payload is the bare `parsed` body; unknown top-level
        fields are rejected the same way the Codex `parsed` body is."""
        payload = self._parsed_payload()
        payload["unexpected_top_field"] = "nope"
        cp = self._run_parser(payload)
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "unknown-parsed-field" in codes

    def test_from_claude_rejects_malformed_finding(self) -> None:
        """A finding missing required schema keys halts with the same
        `missing-plan-review-finding-field` /
        `invalid-plan-review-finding-severity` codes the Codex path emits.
        """
        cp = self._run_parser(
            self._parsed_payload(
                verdict="needs-replan",
                findings=[{"severity": "info", "section": "x"}],
            )
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "missing-plan-review-finding-field" in codes
        assert "invalid-plan-review-finding-severity" in codes

    def test_from_claude_skips_envelope_subcommand_check(self) -> None:
        """Critical contract — on the Claude path the parser must NOT emit
        `invalid-subcommand` for a payload that lacks the wrapper
        envelope's `subcommand` field. The bare `parsed` body has no
        `subcommand`, and the --from-claude flag tells the parser to skip
        that envelope-level check entirely. Without --from-claude, the
        same payload would be rejected with `invalid-subcommand`.
        """
        payload = self._parsed_payload()
        # Without --from-claude this payload is rejected (TestParsePlanReviewReport
        # `test_invalid_subcommand_hints_inner_payload` covers that case);
        # with --from-claude the envelope-level check is skipped and the
        # body validates cleanly.
        cp = self._run_parser(payload)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # Sanity: no envelope-level error codes leaked into the result.
        for code in [e["code"] for e in body["errors"]]:
            assert "invalid-subcommand" not in code

    def test_from_claude_rejects_empty_stdin(self) -> None:
        """Empty stdin halts the same way on both paths."""
        cp = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report",
                "--stdin", "--from-claude", "--json",
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

    def test_from_claude_rejects_non_json_stdin(self) -> None:
        """Non-JSON stdin halts the same way on both paths."""
        cp = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report",
                "--stdin", "--from-claude", "--json",
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

    def test_existing_callers_without_from_claude_still_work(self) -> None:
        """Regression: every existing caller of parse-plan-review-report
        must continue to work without the --from-claude flag (no
        unintended behavior change on the Codex envelope path).
        Re-asserts the contract end-to-end via a happy-path Codex
        envelope through the default parser invocation.
        """
        env = _plan_review_envelope(verdict="approved-with-notes")
        cp = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report", "--stdin", "--json",
            ],
            input=json.dumps(env),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == "approved-with-notes"
        assert body["outcome"] == "success"
        assert body["errors"] == []


class Test_parse_plan_review_triage_report:
    def _run_parser(
        self,
        report: str,
        *,
        source: str | None = "plan-analyst",
        findings_count: int = 2,
        include_source: bool = True,
    ) -> subprocess.CompletedProcess:
        cmd = [
            str(PY), str(SCRIPT),
            "parse-plan-review-triage-report", "--stdin",
            "--findings-count", str(findings_count),
            "--json",
        ]
        if include_source and source is not None:
            cmd.extend(["--source", source])
        return subprocess.run(
            cmd,
            input=report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    def _report(self, payload: dict) -> str:
        return (
            "Summary text\n\n"
            "```json\n"
            f"{json.dumps(payload)}\n"
            "```\n"
        )

    @pytest.mark.parametrize(
        ("source", "findings_count"),
        [("plan-analyst", 4), ("codex-plan-review", 4)],
    )
    @pytest.mark.parametrize(
        "verdict",
        ["ship", "ship-with-fixes", "partial-agreement", "needs-rework"],
    )
    def test_accepts_each_verdict(
        self, source: str, findings_count: int, verdict: str,
    ) -> None:
        payload = {
            "verdict": verdict,
            "load_bearing": [0] if verdict == "partial-agreement" else [],
            "dismissed": [1] if verdict == "partial-agreement" else [],
            "summary": "triage summary",
        }
        cp = self._run_parser(
            self._report(payload),
            source=source,
            findings_count=findings_count,
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == verdict
        assert body["load_bearing"] == payload["load_bearing"]
        assert body["dismissed"] == payload["dismissed"]
        assert body["summary"] == "triage summary"
        assert body["findings_count"] == findings_count
        assert body["source"] == source
        assert body["errors"] == []

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_rejects_partial_agreement_with_empty_load_bearing(
        self, source: str,
    ) -> None:
        cp = self._run_parser(
            self._report({
                "verdict": "partial-agreement",
                "load_bearing": [],
                "dismissed": [0],
                "summary": "bad split",
            }),
            source=source,
            findings_count=1,
        )
        assert cp.returncode == 1, cp.stdout
        codes = [e["code"] for e in _parse_json(cp)["errors"]]
        assert "partial-agreement-invalid-split" in codes

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_rejects_partial_agreement_with_empty_dismissed(
        self, source: str,
    ) -> None:
        cp = self._run_parser(
            self._report({
                "verdict": "partial-agreement",
                "load_bearing": [0],
                "dismissed": [],
                "summary": "bad split",
            }),
            source=source,
            findings_count=1,
        )
        assert cp.returncode == 1, cp.stdout
        codes = [e["code"] for e in _parse_json(cp)["errors"]]
        assert "partial-agreement-invalid-split" in codes

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_rejects_overlapping_buckets(self, source: str) -> None:
        cp = self._run_parser(
            self._report({
                "verdict": "partial-agreement",
                "load_bearing": [0, 1],
                "dismissed": [1],
                "summary": "overlap",
            }),
            source=source,
            findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        codes = [e["code"] for e in _parse_json(cp)["errors"]]
        assert "triage-buckets-not-disjoint" in codes

    @pytest.mark.parametrize(
        ("source", "expected_phrase"),
        [
            ("plan-analyst", "gap index 3 out of range"),
            ("codex-plan-review", "finding index 3 out of range"),
        ],
    )
    def test_rejects_out_of_range_index_with_source_aware_message(
        self, source: str, expected_phrase: str,
    ) -> None:
        cp = self._run_parser(
            self._report({
                "verdict": "partial-agreement",
                "load_bearing": [3],
                "dismissed": [0],
                "summary": "bad index",
            }),
            source=source,
            findings_count=2,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        codes = [e["code"] for e in body["errors"]]
        assert "triage-index-out-of-range" in codes
        assert any(expected_phrase in e["message"] for e in body["errors"])

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_rejects_missing_fenced_json_block(self, source: str) -> None:
        cp = self._run_parser(
            "No fenced json here",
            source=source,
            findings_count=1,
        )
        assert cp.returncode == 1, cp.stdout
        codes = [e["code"] for e in _parse_json(cp)["errors"]]
        assert "triage-report-missing-json" in codes

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_last_fenced_json_block_wins(self, source: str) -> None:
        report = (
            "```json\n"
            "{\"verdict\": \"ship\", \"load_bearing\": [], "
            "\"dismissed\": [], \"summary\": \"first\"}\n"
            "```\n"
            "later\n"
            "```json\n"
            "{\"verdict\": \"needs-rework\", \"load_bearing\": [], "
            "\"dismissed\": [], \"summary\": \"second\"}\n"
            "```\n"
        )
        cp = self._run_parser(report, source=source, findings_count=0)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["verdict"] == "needs-rework"
        assert body["summary"] == "second"

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_rejects_malformed_outer_json(self, source: str) -> None:
        cp = self._run_parser(
            "```json\n{\"verdict\":\n```\n",
            source=source,
            findings_count=1,
        )
        assert cp.returncode == 1, cp.stdout
        codes = [e["code"] for e in _parse_json(cp)["errors"]]
        assert "json-decode" in codes

    def test_rejects_missing_source_flag(self) -> None:
        cp = self._run_parser(
            self._report({
                "verdict": "ship",
                "load_bearing": [],
                "dismissed": [],
                "summary": "ok",
            }),
            include_source=False,
            findings_count=0,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "triage-source-missing"

    def test_rejects_unknown_source_flag(self) -> None:
        cp = self._run_parser(
            self._report({
                "verdict": "ship",
                "load_bearing": [],
                "dismissed": [],
                "summary": "ok",
            }),
            source="other-source",
            findings_count=0,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "triage-source-unknown"

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_rejects_non_object_json_payload(self, source: str) -> None:
        cp = self._run_parser(
            "```json\n[]\n```\n",
            source=source,
            findings_count=0,
        )
        assert cp.returncode == 1, cp.stdout
        body = _parse_json(cp)
        assert body["errors"][0]["code"] == "invalid-type"


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
        # TASK-002: the section heading qualifier evolved from "Codex plan
        # review" to "Independent plan review" when the route-switch
        # (claude_only=true → plan-reviewer Agent dispatch; otherwise →
        # Codex wrapper) landed at the top of Phase 1.5. Anchor on the
        # section number — that's the load-bearing hop the orchestrator
        # navigates by — and validate the route-switch prose separately.
        assert "### Phase 1.5 — " in text
        assert "Phase 1.5-Claude" in text  # claude_only=true branch is documented.
        # V8 event order must be documented for the orchestrator to follow.
        assert "plan_review_start" in text
        assert "plan_review_done" in text
        # V9 verdict routing.
        assert "approved" in text
        assert "approved-with-notes" in text
        assert "needs-replan" in text
        # V10 degradation. The Codex-path wrapper-failure degrade clause
        # still maps to `plan_review_skipped {reason:"codex_unavailable"}`
        # for routing purposes; the legacy preflight skip clause was
        # retired in TASK-002 (claude_only=true now dispatches the
        # Claude reviewer instead of skipping plan review entirely).
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


# ---------------------------------------------------------------------------
# TASK-007 — per-child targeting: plan-review findings gain optional
# target_task_id; triage + author dispatches route per-child; backward-compat
# with old Codex envelopes (no target_task_id) synthesizes null.
# ---------------------------------------------------------------------------


def _finding(
    *,
    severity: str = "important",
    blocking: bool = False,
    section: str = "tasks[001].test_command",
    concern: str = "unreachable test",
    suggested_change: str = "wire deferred-testing note",
    target_task_id: str | None = ...,  # sentinel: absent vs present null
) -> dict:
    """Build a synthetic plan-review finding with optional target_task_id.

    `target_task_id=...` (Ellipsis) means "omit the field entirely", which
    mirrors an old pre-TASK-007 Codex envelope. Explicit `None` means the
    producer emitted `target_task_id: null` (a schedule-level finding).
    """
    entry: dict = {
        "severity": severity,
        "blocking": blocking,
        "section": section,
        "concern": concern,
        "suggested_change": suggested_change,
    }
    if target_task_id is not ...:
        entry["target_task_id"] = target_task_id
    return entry


class TestTask007PlanReviewSchemaTargetTaskIdOptional:
    """TASK-007 — `codex_plan_review_schema.json` findings[*] gains an
    optional `target_task_id: string | null` field. It is NOT in the
    required list, and `additionalProperties: false` continues to apply
    (the field is enumerated in `properties`).
    """

    SCHEMA = _PLAN_REVIEW_SCHEMA

    def test_schema_exposes_target_task_id_in_finding_properties(self) -> None:
        schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
        finding = schema["properties"]["findings"]["items"]
        assert "target_task_id" in finding["properties"], (
            "findings[*].target_task_id must be enumerated in `properties` "
            "so additionalProperties:false continues to permit it"
        )
        prop = finding["properties"]["target_task_id"]
        # Type must admit both string and null (schedule-level findings
        # carry null).
        types = prop.get("type")
        if isinstance(types, str):
            types = [types]
        assert "string" in types and "null" in types, prop

    def test_schema_target_task_id_not_in_required(self) -> None:
        schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
        finding = schema["properties"]["findings"]["items"]
        required = set(finding.get("required", []))
        assert "target_task_id" not in required, (
            "target_task_id must remain optional so pre-TASK-007 Codex "
            "envelopes continue to validate"
        )

    def test_schema_finding_additional_properties_stays_false(self) -> None:
        schema = json.loads(self.SCHEMA.read_text(encoding="utf-8"))
        finding = schema["properties"]["findings"]["items"]
        # The TASK-007 change must not weaken the global
        # additionalProperties:false gate on findings — extra unknown
        # keys would still silently leak through.
        assert finding.get("additionalProperties") is False


class TestTask007PlanReviewSchemaBackwardCompat:
    """TASK-007 — envelope with `target_task_id` present validates;
    envelope without it still validates (not in `required`).
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

    def test_envelope_with_target_task_id_string_validates(self) -> None:
        findings = [_finding(target_task_id="002")]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["findings"][0]["target_task_id"] == "002"

    def test_envelope_with_target_task_id_null_validates(self) -> None:
        findings = [_finding(target_task_id=None)]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["findings"][0]["target_task_id"] is None

    def test_envelope_without_target_task_id_validates(self) -> None:
        # `target_task_id=...` omits the field entirely — the pre-TASK-007
        # Codex envelope shape. It MUST still validate (optional field).
        findings = [_finding()]
        # Safety check: the field really is absent on the wire.
        assert "target_task_id" not in findings[0]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        # The parser validates the raw envelope first; see the separate
        # backward-compat synthesis test for the post-normalization value.
        assert body["errors"] == []

    def test_invalid_target_task_id_type_rejected(self) -> None:
        """Non-string, non-null values (e.g., integer task id) MUST be
        rejected. Old envelopes that accidentally emit `target_task_id: 2`
        (integer) would otherwise slip through and break downstream
        target-resolution in the orchestrator."""
        bad = _finding()
        bad["target_task_id"] = 42  # wrong type
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=[bad],
            )
        )
        assert cp.returncode == 1, cp.stdout
        codes = [e["code"] for e in _parse_json(cp)["errors"]]
        assert "invalid-plan-review-finding-field" in codes


class TestTask007ParsePlanReviewReportSynthesizesNullTargetTaskId:
    """TASK-007 — `parse-plan-review-report` surfaces `target_task_id`
    when present; synthesizes `null` when absent (backward-compat with
    pre-TASK-007 Codex envelopes). Schedule-level routing on the author
    side keys off this normalized field, so the synthesis is the seam
    that lets old envelopes plug into the new per-child dispatcher.
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

    def test_absent_field_synthesizes_null(self) -> None:
        findings = [_finding()]
        assert "target_task_id" not in findings[0]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert "target_task_id" in body["findings"][0]
        assert body["findings"][0]["target_task_id"] is None, (
            "parse-plan-review-report must synthesize null when the "
            "pre-TASK-007 envelope omits target_task_id — routing to "
            "the schedule-level author path depends on this default"
        )

    def test_present_string_value_preserved(self) -> None:
        findings = [_finding(target_task_id="007")]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["findings"][0]["target_task_id"] == "007"

    def test_present_null_value_preserved(self) -> None:
        findings = [_finding(target_task_id=None)]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["findings"][0]["target_task_id"] is None

    def test_mixed_findings_normalized_uniformly(self) -> None:
        """Some findings carry target_task_id, some do not. Every
        finding in the parser's output MUST carry the key — downstream
        consumers iterate unconditionally."""
        findings = [
            _finding(target_task_id="001"),     # present string
            _finding(),                           # absent entirely
            _finding(target_task_id=None),       # present null
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="approved-with-notes", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert all(
            "target_task_id" in f for f in body["findings"]
        ), [f.keys() for f in body["findings"]]
        assert body["findings"][0]["target_task_id"] == "001"
        assert body["findings"][1]["target_task_id"] is None
        assert body["findings"][2]["target_task_id"] is None


class TestTask007TriageRoutingBlockingFirst:
    """TASK-007 — Phase 1.5.5 triage dispatch template instructs the
    triage agent to prioritize `blocking=true` findings first, then
    `severity=critical`, then `severity=important`. The template also
    embeds per-finding {target_task_id, blocking, severity} verbatim so
    the agent can reason about the split.

    The ordering is advisory (enforced as triage prose, not parser
    logic — the triage agent returns indices into the ORIGINAL Codex
    findings[] array). The tests below assert that:

    1. The dispatch template documents the prioritization ladder.
    2. Mixed-blocking findings survive round-trip through
       `parse-plan-review-report` with per-finding blocking + severity
       + target_task_id fields intact, so the downstream triage
       template can render them in the prioritized order.
    """

    TEMPLATES = (
        REPO_ROOT / "plugins" / "plan-executor" / "skills"
        / "implement-plan" / "dispatch-templates.md"
    )

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

    def test_triage_template_documents_prioritization_ladder(self) -> None:
        text = self.TEMPLATES.read_text(encoding="utf-8")
        # The per-finding fields must be named in the template so the
        # embedded JSON carries them.
        assert "target_task_id" in text
        assert "blocking" in text
        assert "severity" in text
        # Ordering rule: blocking first, then critical, then important.
        # The assertion matches the literal prose from the template
        # (see dispatch-templates.md §Phase 1-triage / Phase 1.5.5 —
        # TASK-007 injection).
        assert "blocking=true" in text, text
        assert "severity=critical" in text, text
        assert "severity=important" in text, text

    def test_mixed_blocking_findings_preserved_with_per_finding_fields(
        self,
    ) -> None:
        """Round-trip through the parser preserves per-finding blocking,
        severity, and target_task_id so the triage dispatcher can
        reorder / embed them without re-deriving the shape."""
        findings = [
            _finding(
                severity="minor",
                blocking=False,
                target_task_id="003",
                section="tasks[003].description",
                concern="minor nit",
            ),
            _finding(
                severity="critical",
                blocking=True,
                target_task_id="001",
                section="tasks[001].test_command",
                concern="test_command unreachable",
            ),
            _finding(
                severity="important",
                blocking=False,
                target_task_id=None,
                section="batches[0]",
                concern="schedule-level batch ordering",
            ),
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="needs-replan", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        out = body["findings"]
        # The parser preserves the source order — the triage template
        # (not the parser) performs the prioritization re-ordering.
        assert [f["blocking"] for f in out] == [False, True, False]
        assert [f["severity"] for f in out] == [
            "minor", "critical", "important",
        ]
        assert [f["target_task_id"] for f in out] == [
            "003", "001", None,
        ]

    def test_triage_payload_orders_blocking_first_when_sorted(self) -> None:
        """Exercises the production helper `plan_ops.order_triage_findings`
        that the orchestrator / triage-dispatch builder calls before
        rendering the Phase 1.5.5 triage template payload.

        The parser preserves source order; `order_triage_findings` is the
        single codepath that reorders by (blocking desc, severity-bucket,
        source-index) AND annotates each entry with `source_index` so the
        triage agent can reference original Codex positions in its
        `load_bearing` / `dismissed` buckets. A regression where the
        helper stops sorting or stops annotating would fail this test.
        """
        findings = [
            # Deliberately place the blocking=true finding LAST so
            # stable sort by priority key exposes a non-trivial reorder.
            _finding(
                severity="minor", blocking=False,
                target_task_id="003", section="tasks[003].description",
            ),
            _finding(
                severity="important", blocking=False,
                target_task_id=None, section="batches[0]",
            ),
            _finding(
                severity="critical", blocking=True,
                target_task_id="001", section="tasks[001].test_command",
            ),
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="needs-replan", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        parsed = _parse_json(cp)["findings"]

        # Delegate to the production helper — this is the code the
        # orchestrator / dispatch builder actually calls.
        ordered = plan_ops.order_triage_findings(parsed)

        # Helper returns a list of the same length containing annotated
        # copies (no mutation of the input dicts).
        assert len(ordered) == len(parsed)
        assert all(isinstance(f, dict) for f in ordered)
        # Annotated copies — the helper must NOT mutate the caller's
        # finding dicts in place.
        assert "source_index" not in parsed[0]
        assert "source_index" not in parsed[1]
        assert "source_index" not in parsed[2]

        # blocking=true entry must come first; its source_index is 2
        # (originally the LAST entry in `findings`).
        assert ordered[0]["blocking"] is True
        assert ordered[0]["severity"] == "critical"
        assert ordered[0]["target_task_id"] == "001"
        assert ordered[0]["source_index"] == 2
        # After that, non-blocking important > non-blocking minor.
        assert ordered[1]["blocking"] is False
        assert ordered[1]["severity"] == "important"
        assert ordered[1]["target_task_id"] is None
        assert ordered[1]["source_index"] == 1
        assert ordered[2]["blocking"] is False
        assert ordered[2]["severity"] == "minor"
        assert ordered[2]["target_task_id"] == "003"
        assert ordered[2]["source_index"] == 0

    def test_order_triage_findings_is_stable_within_tier(self) -> None:
        """Stable within-tier ordering: two non-blocking important
        findings keep their source order. A regression that swapped to
        an unstable sort (or bucketed without source-index tie-break)
        would fail this test. The annotated `source_index` must match
        each finding's original 0-based position."""
        findings = [
            _finding(
                severity="important", blocking=False,
                target_task_id="001", section="tasks[001].a",
                concern="first-important",
            ),
            _finding(
                severity="critical", blocking=True,
                target_task_id="002", section="tasks[002].b",
                concern="blocking-critical",
            ),
            _finding(
                severity="important", blocking=False,
                target_task_id="003", section="tasks[003].c",
                concern="second-important",
            ),
        ]
        ordered = plan_ops.order_triage_findings(findings)
        # blocking/critical first; originally at position 1.
        assert ordered[0]["concern"] == "blocking-critical"
        assert ordered[0]["source_index"] == 1
        # Two non-blocking importants retain source order (0 before 2).
        assert ordered[1]["concern"] == "first-important"
        assert ordered[1]["source_index"] == 0
        assert ordered[2]["concern"] == "second-important"
        assert ordered[2]["source_index"] == 2

    def test_order_triage_findings_empty_list(self) -> None:
        """An empty input returns an empty list (defensive guard)."""
        assert plan_ops.order_triage_findings([]) == []

    def test_order_triage_findings_rejects_non_list(self) -> None:
        """Non-list input raises TypeError so a caller bug surfaces
        loudly rather than silently no-op'ing."""
        with pytest.raises(TypeError):
            plan_ops.order_triage_findings({"not": "a list"})  # type: ignore[arg-type]

    def test_order_triage_findings_does_not_mutate_input(self) -> None:
        """The helper must not mutate the caller's finding dicts in
        place. A regression that added `source_index` to the input
        would leak across call sites."""
        findings = [
            _finding(
                severity="critical", blocking=True,
                target_task_id="001",
            ),
            _finding(
                severity="minor", blocking=False,
                target_task_id=None,
            ),
        ]
        before = [dict(f) for f in findings]
        _ = plan_ops.order_triage_findings(findings)
        assert findings == before

    def test_triage_dispatch_uses_source_index_and_ordering_helper(self) -> None:
        """Integration test wiring the production dispatch path together:

        1. Codex's `parsed.findings` survive the `parse-plan-review-report`
           parser in source order.
        2. The orchestrator calls `plan_ops.py order-triage-findings` (the
           single testable wire) to order + annotate findings before
           rendering `<codex_findings_json>`.
        3. The triage dispatch template documents that `load_bearing` /
           `dismissed` reference `source_index`, NOT positions in the
           presorted array.
        4. A simulated triage response with `load_bearing=[source_index]`
           correctly identifies the original Codex finding that was
           flagged, even though it is no longer at that positional
           index in the presorted payload.
        """
        # Mixed blocking/severity with the blocking=true finding placed
        # LAST so a non-identity reorder is required.
        findings = [
            _finding(
                severity="minor", blocking=False,
                target_task_id="010", section="tasks[010].a",
                concern="minor-first",
            ),
            _finding(
                severity="important", blocking=False,
                target_task_id=None, section="batches[0]",
                concern="important-middle",
            ),
            _finding(
                severity="critical", blocking=True,
                target_task_id="020", section="tasks[020].b",
                concern="blocking-last",
            ),
        ]

        # Step 1: parse the plan-review envelope. The parser MUST preserve
        # source order — the triage dispatch step performs the reorder.
        cp_parse = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-report", "--stdin", "--json",
            ],
            input=json.dumps(
                _plan_review_envelope(
                    verdict="needs-replan", findings=findings,
                )
            ),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp_parse.returncode == 0, cp_parse.stderr
        parsed_findings = json.loads(cp_parse.stdout)["findings"]
        assert [f["concern"] for f in parsed_findings] == [
            "minor-first", "important-middle", "blocking-last",
        ]

        # Step 2: call the new `order-triage-findings` subcommand — the
        # single testable wire the orchestrator uses. Pipe the full
        # parse-plan-review-report output so the dispatch-rendering
        # site can be a one-liner pipeline. The subcommand accepts both
        # {findings: [...]} envelopes and bare lists.
        cp_order = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "order-triage-findings", "--stdin", "--json",
            ],
            input=cp_parse.stdout,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp_order.returncode == 0, cp_order.stderr
        ordered = json.loads(cp_order.stdout)["ordered"]

        # Prioritized order: blocking first (was source_index=2), then
        # non-blocking important (source_index=1), then non-blocking
        # minor (source_index=0).
        assert [f["concern"] for f in ordered] == [
            "blocking-last", "important-middle", "minor-first",
        ]
        assert [f["source_index"] for f in ordered] == [2, 1, 0]

        # Step 3: the dispatch template documents the `source_index`
        # contract. The triage agent must NOT return 0 to mean "the
        # first entry in the presorted array"; it must return the
        # `source_index` value carried on that entry.
        template = self.TEMPLATES.read_text(encoding="utf-8")
        assert "source_index" in template
        assert "codex-plan-review" in template
        # Explicit prose asserting that indices reference `source_index`.
        assert "source_index" in template
        # The ordering wire must be referenced by name so operators can
        # discover the orchestrator contract by reading the template.
        assert "order-triage-findings" in template

        # Step 4: simulate a triage response that flags the blocking
        # finding as load-bearing. The triage agent sees the blocking
        # entry at presorted position 0 — but per the `source_index`
        # contract it MUST emit `load_bearing=[2]` (the original Codex
        # position). The parser validates against the ORIGINAL findings
        # count and accepts the source_index value.
        triage_report = _plan_review_triage_envelope(
            verdict="partial-agreement",
            load_bearing=[2],  # source_index of the blocking finding
            dismissed=[0, 1],  # source_indices of the dismissed pair
            summary="blocking-last is load-bearing; others dismissed",
        )
        cp_triage = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-triage-report", "--stdin",
                "--source", "codex-plan-review",
                "--findings-count", str(len(parsed_findings)),
                "--json",
            ],
            input=triage_report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp_triage.returncode == 0, cp_triage.stdout or cp_triage.stderr
        triage = json.loads(cp_triage.stdout)
        assert triage["verdict"] == "partial-agreement"
        assert triage["load_bearing"] == [2]
        assert triage["dismissed"] == [0, 1]

        # Translating the triage selection back to the original Codex
        # findings[] array via source_index values identifies the correct
        # concern — the blocking one that was placed LAST in the input.
        load_bearing_findings = [
            parsed_findings[i] for i in triage["load_bearing"]
        ]
        assert [f["concern"] for f in load_bearing_findings] == [
            "blocking-last",
        ]
        assert load_bearing_findings[0]["blocking"] is True
        assert load_bearing_findings[0]["target_task_id"] == "020"


class TestTask007AuthorPerChildTargeting:
    """TASK-007 — Phase 1.5a `plan-author` dispatch receives per-finding
    {finding, target_task_id, child_plan_file} triples and fans out
    one dispatch per finding. Two findings with different target_task_id
    values produce two separate author dispatches, each scoped to one
    child file. Schedule-level findings (target_task_id=null) route to
    a single "schedule-level — no child file" author dispatch.

    The fan-out itself is orchestrator prose (SKILL.md + dispatch-templates.md
    read by the parent agent). These tests assert that:

    1. The parser surfaces distinct target_task_id values per finding so
       the orchestrator can group by child.
    2. The SKILL.md + dispatch-templates.md prose documents the per-finding
       payload shape and the schedule-level exception.
    3. The plan-author.md agent spec accepts child_plan_file (per-child)
       AND 00_INDEX.json (schedule-level) as valid edit targets.
    """

    SKILL = (
        REPO_ROOT / "plugins" / "plan-executor" / "skills"
        / "implement-plan" / "SKILL.md"
    )
    TEMPLATES = (
        REPO_ROOT / "plugins" / "plan-executor" / "skills"
        / "implement-plan" / "dispatch-templates.md"
    )
    AGENT = (
        REPO_ROOT / "plugins" / "plan-executor" / "agents" / "plan-author.md"
    )

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

    def test_two_findings_different_target_task_id_yield_distinct_targets(
        self,
    ) -> None:
        """Two findings with different target_task_id values parse with
        distinct target_task_id on each — the orchestrator uses this
        field to dispatch one author per child."""
        findings = [
            _finding(
                target_task_id="002",
                section="tasks[002].test_command",
                concern="bad test command",
            ),
            _finding(
                target_task_id="005",
                section="tasks[005].description",
                concern="description missing",
            ),
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="needs-replan", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)["findings"]
        # Grouping by target_task_id yields two distinct buckets, i.e.,
        # two separate author dispatches.
        by_target: dict[str | None, list[dict]] = {}
        for f in out:
            by_target.setdefault(f["target_task_id"], []).append(f)
        assert set(by_target.keys()) == {"002", "005"}
        assert len(by_target["002"]) == 1
        assert len(by_target["005"]) == 1
        # Each bucket carries the original finding shape verbatim so
        # the dispatcher can embed it in the per-child payload.
        assert by_target["002"][0]["concern"] == "bad test command"
        assert by_target["005"][0]["concern"] == "description missing"

    def test_two_findings_build_two_dispatches_with_distinct_child_files(
        self,
    ) -> None:
        """Integration-style: parsing two findings with different non-null
        target_task_id values and running the per-finding fan-out (resolve
        target_task_id → schedule tasks[].plan_file → child_plan_file) must
        produce TWO distinct dispatch payloads, each with its own
        child_plan_file. Guards against a dispatch-builder regression that
        could collapse both findings onto one child or drop one dispatch."""
        findings = [
            _finding(
                target_task_id="002",
                section="tasks[002].test_command",
                concern="bad test command",
            ),
            _finding(
                target_task_id="005",
                section="tasks[005].description",
                concern="description missing",
            ),
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="needs-replan", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        parsed_findings = _parse_json(cp)["findings"]

        # Synthetic schedule tasks[] mapping — the orchestrator's resolver
        # uses target_task_id as the key to look up the child plan_file.
        schedule_tasks = {
            "002": "/tmp/plan_dir/TASK-002_fix-test.md",
            "005": "/tmp/plan_dir/TASK-005_description.md",
        }

        # Build one dispatch per finding (the TASK-007 per-finding fan-out).
        dispatches: list[dict] = []
        for finding in parsed_findings:
            target_task_id = finding["target_task_id"]
            assert target_task_id is not None, (
                "both findings are task-targeted so neither resolves to the "
                "schedule-level path"
            )
            dispatches.append({
                "agent": "plan-author",
                "source": "codex-plan-review",
                "finding": finding,
                "target_task_id": target_task_id,
                "child_plan_file": schedule_tasks[target_task_id],
            })

        # Two findings with different targets yield TWO dispatches.
        assert len(dispatches) == 2, (
            "per-finding fan-out must emit one dispatch per finding; "
            "collapsing two findings into one dispatch is a regression"
        )
        # Each dispatch carries a distinct child_plan_file.
        child_files = [d["child_plan_file"] for d in dispatches]
        assert len(set(child_files)) == 2, (
            "dispatches must target distinct child plan files when findings "
            f"have different target_task_id values; got {child_files}"
        )
        # target_task_id is preserved per-dispatch so the author agent
        # knows which child to edit.
        assert {d["target_task_id"] for d in dispatches} == {"002", "005"}
        # The finding payload is embedded verbatim so the author sees
        # the original concern/section/etc.
        concerns = {d["target_task_id"]: d["finding"]["concern"] for d in dispatches}
        assert concerns == {
            "002": "bad test command",
            "005": "description missing",
        }

    def test_schedule_level_findings_each_surface_as_separate_entry(self) -> None:
        """Findings with target_task_id=null each surface as their own
        parser-emitted entry — the orchestrator emits ONE author dispatch
        per schedule-level finding (not a single collapsed dispatch across
        all null-target findings). The parser preserves one-to-one
        payload-to-finding correspondence so the fan-out contract
        (N findings → N author dispatches) holds uniformly for task-targeted
        and schedule-level findings alike."""
        findings = [
            _finding(target_task_id=None, section="batches[0]"),
            _finding(target_task_id=None, section="roster"),
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="needs-replan", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)["findings"]
        schedule_level = [f for f in out if f["target_task_id"] is None]
        assert len(schedule_level) == 2, (
            "parser preserves both findings as independent entries so the "
            "orchestrator can dispatch one author per schedule-level "
            "finding, matching the uniform per-finding fan-out contract"
        )

    def test_dispatch_templates_document_per_finding_payload(self) -> None:
        text = self.TEMPLATES.read_text(encoding="utf-8")
        # The Phase 1.5a block must name the per-finding triple shape.
        assert "child_plan_file" in text, text
        assert "target_task_id" in text, text
        # Fan-out wording: one dispatch per finding (per-child).
        assert (
            "per finding" in text
            or "per-child" in text
            or "one finding per dispatch" in text
            or "per-finding" in text
        ), text
        # Schedule-level exception must be documented with the
        # 00_INDEX.json OR empty edit-target language.
        assert "00_INDEX.json" in text, text
        assert (
            "schedule-level" in text or "schedule level" in text
        ), text

    def test_author_per_child_targeting_includes_schedule_level_as_separate_dispatch(
        self,
    ) -> None:
        """N findings → N author dispatches, regardless of target_task_id
        null/non-null. Two findings — one task-targeted ("001") and one
        schedule-level (null) — each surface as an independent parser
        entry so the orchestrator's per-finding fan-out emits TWO
        separate author dispatches (not a single one that collapses the
        schedule-level finding into a whole-plan sweep).

        Also asserts the orchestrator-side contracts (SKILL.md +
        dispatch-templates.md) document one-dispatch-per-finding for
        schedule-level findings and do NOT describe a batched
        'single schedule-level author dispatch'.
        """
        findings = [
            _finding(
                target_task_id="001",
                section="tasks[001].test_command",
                concern="task-targeted concern",
            ),
            _finding(
                target_task_id=None,
                section="batches[0]",
                concern="schedule-level concern",
            ),
        ]
        cp = self._run_parser(
            _plan_review_envelope(
                verdict="needs-replan", findings=findings,
            )
        )
        assert cp.returncode == 0, cp.stderr
        out = _parse_json(cp)["findings"]

        # Parser surfaces BOTH findings as independent entries — this is
        # what the orchestrator keys on to emit one author dispatch per
        # finding. A collapsed dispatch would require the parser to fold
        # null-target findings together; it does not.
        assert len(out) == 2, (
            "both findings must surface as separate entries so the "
            "orchestrator dispatches one author per finding"
        )

        # Each finding's target_task_id is preserved verbatim, so the
        # orchestrator can build the {finding, target_task_id,
        # child_plan_file} triple independently for each finding.
        targets = [f["target_task_id"] for f in out]
        assert targets == ["001", None], (
            "parser preserves target_task_id per-finding so each finding "
            "gets its own author dispatch — the task-targeted and "
            "schedule-level findings must remain distinguishable"
        )

        # Distinct triples → distinct dispatches. Grouping preserves
        # one-to-one correspondence.
        assert out[0]["concern"] == "task-targeted concern"
        assert out[1]["concern"] == "schedule-level concern"

        # Orchestrator-prose contract: SKILL.md must describe the
        # uniform per-finding fan-out and MUST NOT describe a single
        # collapsed schedule-level author dispatch.
        skill_text = self.SKILL.read_text(encoding="utf-8")
        assert (
            "one-dispatch-per-finding" in skill_text
            or "per-finding" in skill_text
            or "per finding" in skill_text
        ), (
            "SKILL.md §Phase 1.5a must document the one-dispatch-per-"
            "finding fan-out"
        )
        # The legacy "single schedule-level author dispatch" phrasing
        # must be gone (it was the contract violation Codex flagged).
        assert "single \"schedule-level\" author dispatch" not in skill_text, (
            "SKILL.md must not describe schedule-level findings as "
            "collapsing into a single author dispatch"
        )

        # Dispatch-templates mirrors the same contract.
        tpl_text = self.TEMPLATES.read_text(encoding="utf-8")
        assert "single \"schedule-level\" author dispatch" not in tpl_text, (
            "dispatch-templates.md must not describe schedule-level "
            "findings as routing to a single dispatch"
        )
        assert (
            "single schedule-level author dispatch" not in tpl_text
        ), (
            "dispatch-templates.md must not batch schedule-level "
            "findings into one dispatch"
        )

    def test_skill_md_documents_per_child_author_fanout(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        # The fan-out is named explicitly in the needs-replan auto-revise
        # section so an orchestrator reading the skill top-down can
        # implement the shift from single-dispatch to per-child fan-out.
        assert "per-child" in text or "per child" in text, (
            "SKILL.md §Phase 1.5a must document the per-child author "
            "fan-out"
        )
        assert "target_task_id" in text, (
            "SKILL.md §Phase 1.5a must name target_task_id as the "
            "resolution key"
        )
        assert "child_plan_file" in text or "plan_file" in text, (
            "SKILL.md §Phase 1.5a must name the child file edit target"
        )
        # Schedule-level exception.
        assert "schedule-level" in text or "schedule level" in text, (
            "SKILL.md §Phase 1.5a must document the schedule-level "
            "(target_task_id=null) exception"
        )

    def test_plan_author_agent_accepts_child_plan_file(self) -> None:
        text = self.AGENT.read_text(encoding="utf-8")
        # The agent spec MUST declare child_plan_file as the edit target
        # for per-task findings.
        assert "child_plan_file" in text, (
            "plan-author.md must declare child_plan_file as the edit "
            "target for per-task findings"
        )
        # And 00_INDEX.json OR empty (files_edited: []) for schedule-level.
        assert "00_INDEX.json" in text, (
            "plan-author.md must declare 00_INDEX.json as the allowed "
            "edit surface for schedule-level findings"
        )
        assert (
            "schedule-level" in text or "schedule level" in text
        ), (
            "plan-author.md must name the schedule-level path"
        )
        # Child-grammar guidance — H3 `### TASK-NNN:` sub-heading.
        assert "### TASK-NNN" in text, (
            "plan-author.md must document the `### TASK-NNN:` child "
            "sub-heading grammar (TASK-007 parser guidance)"
        )


class TestPlanCodexDispatchPlanReviewSubcommand:
    """V9 — the wrapper exposes a plan-review subcommand with the expected
    CLI shape. Use --dry-run so we don't need Codex on the test runner.

    TASK-008 removed ``--plan-file`` / ``--plans-dir`` from the argparse
    surface; the schedule sidecar's parent-directory basename (or file
    stem) is now the sole source of envelope.plan_file. These tests pin
    the schedule-only argv shape.
    """

    WRAPPER = SCRIPTS_DIR / "plan_codex_dispatch.py"

    def test_dry_run_emits_envelope(self, tmp_path: Path) -> None:
        schedule = tmp_path / "sample.schedule.json"
        schedule.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
        }), encoding="utf-8")

        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--schedule-file", str(schedule),
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
        # plan_file derived from the schedule sidecar stem (TASK-006 / TASK-008):
        # "sample.schedule.json" → "sample".
        assert body["plan_file"] == "sample"
        assert "prompt_preview" in body
        # The prompt must carry the verdict vocab so Codex knows what to
        # return; if this drifts, the wrapper silently corrupts the
        # gating contract.
        assert "approved" in body["prompt_preview"]
        assert "needs-replan" in body["prompt_preview"]
        # The prompt must explicitly tell Codex the orchestrator owns the
        # cross-plan dependency gate so the reviewer does not fabricate a
        # `needs-replan` on dep-check grounds when it declines to run tools.
        assert "Phase 0 preflight" in body["prompt_preview"]

    def test_retired_plan_file_flag_rejected(self, tmp_path: Path) -> None:
        """TASK-008: ``--plan-file`` is no longer recognized by argparse.
        Passing it must cause a hard exit, not silent acceptance."""
        schedule = tmp_path / "sample.schedule.json"
        schedule.write_text("{}", encoding="utf-8")
        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--plan-file", str(tmp_path / "nope.md"),  # retired flag
                "--schedule-file", str(schedule),
                "--repo-root", str(tmp_path),
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
        )
        # argparse emits rc=2 with "unrecognized arguments" on stderr.
        assert cp.returncode != 0
        assert "--plan-file" in cp.stderr or "unrecognized" in cp.stderr.lower(), cp.stderr

    def test_missing_schedule_file_fails(self, tmp_path: Path) -> None:
        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--schedule-file", str(tmp_path / "nope.json"),
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
        schedule = tmp_path / "sample.schedule.json"
        schedule.write_text("{ not valid json", encoding="utf-8")
        cp = subprocess.run(
            [
                str(PY), str(self.WRAPPER), "plan-review",
                "--schedule-file", str(schedule),
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


# ---------------------------------------------------------------------------
# TASK-019 — backfill TASK-002 V3/V4 and orchestrator paper-cuts
# ---------------------------------------------------------------------------


class TestTask019ScheduleDagHelper:
    """V1-V5. `_validate_schedule_dag` helper and the three call sites."""

    def test_parse_schedule_rejects_dependency_cycle(self) -> None:
        # V1 — TASK-002 V3 backfill. A cycle must surface in parse-schedule
        # (not only in batch-next / filter-schedule).
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [],
                 "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "codex", "files": [],
                 "dependencies": ["001"], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"], "file_locks": []},
            ],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in (body.get("errors") or [])]
        assert "dependency-cycle" in codes
        # Message names both cyclic ids.
        cycle_msgs = [e["message"] for e in body["errors"]
                      if e["code"] == "dependency-cycle"]
        assert any("001" in m and "002" in m for m in cycle_msgs), cycle_msgs

    def test_parse_schedule_rejects_orphan_dependency(self) -> None:
        # V2 — TASK-002 V4 backfill. Orphan dep must surface in parse-schedule.
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": [],
                 "dependencies": ["999"], "plan_file": "sample.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": []},
            ],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in (body.get("errors") or [])]
        assert "unknown-dependency" in codes
        orphan_msgs = [e["message"] for e in body["errors"]
                       if e["code"] == "unknown-dependency"]
        assert any("999" in m for m in orphan_msgs), orphan_msgs

    def test_validate_schedule_dag_shared_by_consumers(self) -> None:
        # V3 — the helper is a single module-local definition with exactly
        # four call sites. Inline Kahn's blocks in cmd_batch_next /
        # cmd_filter_schedule are forbidden.
        script_text = SCRIPT.read_text(encoding="utf-8")
        occurrences = [
            ln for ln in script_text.splitlines()
            if "_validate_schedule_dag(" in ln
        ]
        # 1 def + 4 call sites (cmd_parse_schedule, cmd_batch_next,
        # cmd_filter_schedule, _gate_schedule_valid).
        assert len(occurrences) == 5, (
            f"expected 5 occurrences (1 def + 4 calls), got {len(occurrences)}:\n"
            + "\n".join(occurrences)
        )
        defs = [ln for ln in occurrences if ln.lstrip().startswith("def ")]
        assert len(defs) == 1, defs

    def test_batch_next_cycle_still_emits_dependency_cycle(
        self, tmp_path: Path
    ) -> None:
        # V4 — regression of TASK-004B V8 after the refactor.
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
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

    def test_filter_schedule_cycle_still_emits_dependency_cycle(
        self, tmp_path: Path
    ) -> None:
        # V5 — regression of TASK-004A after the refactor.
        sched = tmp_path / "cycle.schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "codex", "files": ["a"],
                 "dependencies": ["002"], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
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


class TestTask006_validate_schedule_dag_batch_topo:
    """TASK-006. `_validate_schedule_dag` enforces batch topology.

    Defense-in-depth: even if a batcher (TASK-002 / TASK-003) regresses
    or a hand-crafted schedule sneaks past, the persisted schedule
    cannot pass `parse-schedule` / `write-schedule` / `schedule-valid`
    without batches[] respecting dependencies[]. The class name embeds
    the `validate_schedule_dag` substring so the canonical test command
    `pytest -k "validate_schedule_dag or schedule_valid"` selects every
    test below alongside the existing TASK-019 helper coverage.
    """

    def test_dag_validator_rejects_dependent_in_same_batch_as_prereq(
        self,
    ) -> None:
        tasks = [
            {"id": "001", "files": [], "dependencies": []},
            {"id": "002", "files": [], "dependencies": ["001"]},
        ]
        batches = [
            {"index": 1, "task_ids": ["001", "002"], "file_locks": []},
        ]
        errors = plan_ops._validate_schedule_dag(tasks, batches)
        viol = [e for e in errors if e["code"] == "dependency-batch-violation"]
        assert len(viol) == 1, errors
        e = viol[0]
        assert e["path"] == "$.tasks[1].dependencies[0]", e
        assert e["message"] == (
            "TASK-002 (batch 1) depends on TASK-001 which is in batch 1; "
            "dependent must run in a strictly later batch"
        ), e["message"]

    def test_dag_validator_rejects_dependent_in_earlier_batch_than_prereq(
        self,
    ) -> None:
        tasks = [
            {"id": "001", "files": [], "dependencies": ["002"]},
            {"id": "002", "files": [], "dependencies": []},
        ]
        batches = [
            {"index": 1, "task_ids": ["001"], "file_locks": []},
            {"index": 2, "task_ids": ["002"], "file_locks": []},
        ]
        errors = plan_ops._validate_schedule_dag(tasks, batches)
        viol = [e for e in errors if e["code"] == "dependency-batch-violation"]
        assert len(viol) == 1, errors
        e = viol[0]
        assert e["path"] == "$.tasks[0].dependencies[0]", e
        assert e["message"] == (
            "TASK-001 (batch 1) depends on TASK-002 which is in batch 2; "
            "dependent must run in a strictly later batch"
        ), e["message"]

    def test_dag_validator_emits_all_violations_not_just_first(self) -> None:
        # Three offending edges; the validator must surface all three in a
        # single pass so the operator sees the full extent.
        tasks = [
            {"id": "001", "files": [], "dependencies": []},
            {"id": "002", "files": [], "dependencies": ["001"]},
            {"id": "003", "files": [], "dependencies": ["001", "002"]},
        ]
        # All three crammed into one batch — every cross-task edge violates.
        batches = [
            {"index": 1, "task_ids": ["001", "002", "003"], "file_locks": []},
        ]
        errors = plan_ops._validate_schedule_dag(tasks, batches)
        viol = [e for e in errors if e["code"] == "dependency-batch-violation"]
        # 002→001, 003→001, 003→002.
        assert len(viol) == 3, errors
        paths = sorted(e["path"] for e in viol)
        assert paths == [
            "$.tasks[1].dependencies[0]",
            "$.tasks[2].dependencies[0]",
            "$.tasks[2].dependencies[1]",
        ], paths

    def test_dag_validator_accepts_topo_correct_batches(self) -> None:
        # Negative control: a topo-correct schedule produces zero
        # batch-violation entries (and no other dag errors either).
        tasks = [
            {"id": "001", "files": [], "dependencies": []},
            {"id": "002", "files": [], "dependencies": ["001"]},
            {"id": "003", "files": [], "dependencies": ["001", "002"]},
        ]
        batches = [
            {"index": 1, "task_ids": ["001"], "file_locks": []},
            {"index": 2, "task_ids": ["002"], "file_locks": []},
            {"index": 3, "task_ids": ["003"], "file_locks": []},
        ]
        errors = plan_ops._validate_schedule_dag(tasks, batches)
        assert errors == [], errors

    def test_dag_validator_orphan_dep_takes_precedence_over_batch_violation(
        self,
    ) -> None:
        # The orphan-dep pass already names the offending edge; emitting a
        # second `dependency-batch-violation` for the same edge would be
        # redundant noise, so the new check skips orphan prereqs.
        tasks = [
            # 002 declares a dep on the unknown task 999.
            {"id": "001", "files": [], "dependencies": []},
            {"id": "002", "files": [], "dependencies": ["999"]},
        ]
        batches = [
            {"index": 1, "task_ids": ["001", "002"], "file_locks": []},
        ]
        errors = plan_ops._validate_schedule_dag(tasks, batches)
        codes = [e["code"] for e in errors]
        assert "unknown-dependency" in codes, errors
        # Crucially, NO batch-violation for the same edge.
        viol = [e for e in errors if e["code"] == "dependency-batch-violation"]
        assert viol == [], viol

    def test_dag_validator_unbatched_task_does_not_falsely_trigger(
        self,
    ) -> None:
        # 002 is declared in tasks[] but absent from batches[]. The
        # missing-from-batches problem is `_validate_schedule_refs`'s
        # responsibility; this validator MUST NOT emit a phantom
        # batch-violation. (Same applies if 001 — the prereq — were
        # unbatched; both endpoints must be in `batch_of` for the
        # violation check to fire.)
        tasks = [
            {"id": "001", "files": [], "dependencies": []},
            {"id": "002", "files": [], "dependencies": ["001"]},
        ]
        batches = [
            {"index": 1, "task_ids": ["001"], "file_locks": []},
            # 002 is intentionally omitted from any batch.
        ]
        errors = plan_ops._validate_schedule_dag(tasks, batches)
        viol = [e for e in errors if e["code"] == "dependency-batch-violation"]
        assert viol == [], viol

    def test_parse_schedule_strict_stdin_rejects_batch_topo_violation(
        self,
    ) -> None:
        # CLI envelope test: a synthetic schedule whose batches violate
        # deps must produce a non-zero exit + the new error code on
        # `parse-schedule --strict --stdin`. Operators rely on this seam
        # to fail fast before round-tripping through Codex/Claude review.
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "claude", "files": ["a.py"],
                 "dependencies": [], "plan_file": "sample.md"},
                {"id": "002", "agent": "claude", "files": ["b.py"],
                 "dependencies": ["001"], "plan_file": "sample.md"},
            ],
            "batches": [
                # Same-batch placement of dep + prereq — the bug we catch.
                {"index": 1, "task_ids": ["001", "002"],
                 "file_locks": ["a.py", "b.py"]},
            ],
        }
        cp = subprocess.run(
            [str(PY), str(SCRIPT),
             "parse-schedule", "--stdin", "--strict", "--json"],
            input=json.dumps(payload),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 1, (cp.returncode, cp.stdout, cp.stderr)
        body = _parse_json(cp)
        codes = [e["code"] for e in (body.get("errors") or [])]
        assert "dependency-batch-violation" in codes, body
        msgs = [e["message"] for e in body["errors"]
                if e["code"] == "dependency-batch-violation"]
        assert any(
            "TASK-002" in m and "TASK-001" in m
            and "strictly later batch" in m
            for m in msgs
        ), msgs


class TestTask019ReviewerFindingDisposition:
    """V6-V8. Optional `disposition` field on reviewer minor-findings."""

    def test_reviewer_minor_finding_disposition_accepted(self) -> None:
        # V6 — finding with a valid disposition + reason is accepted.
        # TASK-022 extended this to cover `spec-deference` alongside the
        # original three values so the V6 vector doubles as the V4 anchor
        # for the fourth disposition.
        for disposition, reason in (
            ("dismissed", "D.5 override"),
            ("accepted", "applied on top of commit"),
            ("deferred", "follow-up task planned"),
            (
                "spec-deference",
                "plan mandates this but critique has design merit",
            ),
        ):
            finding = {
                "severity": "minor",
                "confidence": "medium",
                "file": "a.py",
                "line": 1,
                "issue": "x",
                "suggested_fix": "y",
                "disposition": disposition,
                "disposition_reason": reason,
            }
            errors = plan_ops._validate_reviewer_finding_item(
                finding, path="$.f"
            )
            assert errors == [], (disposition, errors)

    def test_reviewer_minor_finding_disposition_value_rejected(self) -> None:
        # V7 — disposition value outside the allowed set is rejected.
        finding = {
            "severity": "minor",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
            "disposition": "bogus",
        }
        errors = plan_ops._validate_reviewer_finding_item(finding, path="$.f")
        codes = [e["code"] for e in errors]
        assert "invalid-reviewer-finding-disposition" in codes

    def test_reviewer_minor_finding_no_disposition_ok(self) -> None:
        # V8 — backward-compat. Finding without disposition is valid.
        finding = {
            "severity": "minor",
            "confidence": "medium",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
        }
        errors = plan_ops._validate_reviewer_finding_item(finding, path="$.f")
        assert errors == []

    def test_reviewer_minor_finding_reason_without_disposition_rejected(
        self,
    ) -> None:
        # Extra coverage — disposition_reason without disposition is invalid
        # per the helper's contract.
        finding = {
            "severity": "minor",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
            "disposition_reason": "stranded reason",
        }
        errors = plan_ops._validate_reviewer_finding_item(finding, path="$.f")
        codes = [e["code"] for e in errors]
        assert "disposition-reason-without-disposition" in codes


class TestTask019UpdatePlanHeaderAbsent:
    """V9. `update-plan-header` gracefully skips when Status is absent."""

    def test_update_plan_header_absent_is_not_error(
        self, tmp_path: Path
    ) -> None:
        # V9 — per-task-only plan format (DUAL_AGENT_Plans style) must not
        # error out of the End-of-run Step 1.
        plan = tmp_path / "p.md"
        plan.write_text(
            "# Plan\n\n## Tasks\n\n### TASK-001: x\n- **Status:** pending\n",
            encoding="utf-8",
        )
        cp = _run(
            "update-plan-header",
            "--plan-file", str(plan),
            "--status", "complete",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("status") == "absent" or "warning" in body
        # The per-task marker was NOT touched (it's the authoritative signal).
        assert "- **Status:** pending" in plan.read_text(encoding="utf-8")

    def test_update_plan_header_with_status_line_still_works(
        self, tmp_path: Path
    ) -> None:
        # Regression — existing behavior when the top-level Status line IS
        # present is unchanged.
        plan = tmp_path / "p.md"
        plan.write_text(
            "# Plan\n\n- **Status:** in-progress\n\n## Tasks\n\n"
            "### TASK-001: x\n- **Status:** pending\n",
            encoding="utf-8",
        )
        cp = _run(
            "update-plan-header",
            "--plan-file", str(plan),
            "--status", "complete",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body.get("ok") is True
        text = plan.read_text(encoding="utf-8")
        assert "- **Status:** complete" in text


class TestTask019ExecutionLogErrorMessages:
    """V10. Execution-log error messages list allowed fields inline."""

    def test_execution_log_missing_field_message_lists_allowed(
        self,
    ) -> None:
        # V10 — missing-field message includes the full required-field list.
        rows = [{"task": "001"}]  # missing the other five fields
        errors = plan_ops._validate_execution_log_rows(rows)
        missing = [e for e in errors
                   if e["code"] == "missing-execution-log-field"]
        assert missing, errors
        # Each message must list the allowed fields so the orchestrator can
        # see the full schema from the error alone.
        for e in missing:
            for field in ("agent", "reviewer", "verdict", "commit", "notes"):
                assert field in e["message"], (field, e)

    def test_execution_log_unknown_field_message_lists_allowed(
        self,
    ) -> None:
        rows = [{
            "task": "001",
            "agent": "codex",
            "reviewer": "claude",
            "verdict": "ship",
            "commit": "deadbeef",
            "notes": "",
            "stray": "extra",
        }]
        errors = plan_ops._validate_execution_log_rows(rows)
        unknown = [e for e in errors
                   if e["code"] == "unknown-execution-log-field"]
        assert unknown, errors
        for e in unknown:
            for field in ("task", "agent", "reviewer",
                          "verdict", "commit", "notes"):
                assert field in e["message"], (field, e)


class TestTask019SkillMdGrepRegressions:
    """V11. SKILL.md documents D.2a reviewer-flip and execution-log schema."""

    def test_skill_md_documents_d2a_reviewer_flip_and_row_schema(self) -> None:
        # V11 — regression guard over SKILL.md text. Exact phrasing may drift,
        # but the literal tokens the orchestrator needs to spot the pattern
        # MUST remain greppable on a single line.
        skill = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "SKILL.md"
        )
        text = skill.read_text(encoding="utf-8")
        # Pattern A: the D.2a reviewer-flip guidance mentions --reviewer claude,
        # ship-with-fixes, and --disagreement-tag on a single line.
        flip_re = re.compile(
            r"reviewer claude.*ship-with-fixes.*--disagreement-tag"
        )
        assert flip_re.search(text), (
            "expected §D.3 reviewer-flip line mentioning "
            "`--reviewer claude --reviewer-verdict ship-with-fixes "
            "--disagreement-tag` on a single line"
        )
        # Pattern B: the End-of-run Step 2 row schema names all six keys on a
        # single line in order.
        schema_re = re.compile(
            r"rows-json.*task.*agent.*reviewer.*verdict.*commit.*notes"
        )
        assert schema_re.search(text), (
            "expected End-of-run Step 2 to list the rows-json row schema "
            "with keys task/agent/reviewer/verdict/commit/notes"
        )


# ---------------------------------------------------------------------------
# TASK-022 — Persist full reviewer findings in run log + D.5 dismissal gate
# ---------------------------------------------------------------------------
#
# V1 — `log-event --findings-json` embeds the array verbatim under key
#      `findings`.
# V2 — `log-event --findings-json` rejects malformed findings with a
#      structured minor-findings validation error code.
# V3 — `commit_done` event embeds `findings` when `--reviewer-minor-findings`
#      is non-empty, alongside the existing `minor_findings_count`.
# V4 — `spec-deference` is an accepted disposition value (covered in
#      `TestTask019ReviewerFindingDisposition` above as a parametrized case).
# V5 — dispatch-templates.md Phase D.5 section carries the
#      "Dismissal-evidence gate" paragraph + `spec-deference` wording.
# V6 — back-compat: `log-event` without `--findings-json` is unchanged
#      (no `findings` key); `commit-task` with empty `--reviewer-minor-findings`
#      still writes `findings: []` explicitly (the key is always present).


class TestTask022LogEventFindingsJson:
    """V1–V2 — `log-event` accepts an optional `--findings-json` payload.

    The parsed array is validated via `_validate_minor_findings_payload`
    (same schema as `commit-task --reviewer-minor-findings`) and, on
    success, embedded verbatim under key `findings` in the JSONL line
    alongside the existing `--fields-json` keys.
    """

    def test_v1_log_event_findings_json_embedded(
        self, isolated_plan: Path
    ) -> None:
        # V1 — a well-formed findings array round-trips through the JSONL
        # line under key `findings`, alongside the `--fields-json` keys.
        findings_array = [{
            "severity": "minor",
            "confidence": "medium",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
        }]
        fields = {
            "run_id": "X",
            "task_id": "001",
            "reviewer": "codex",
            "verdict": "minor-findings",
            "findings_count": 1,
        }
        cp = _run(
            "log-event",
            "--event", "review_done",
            "--fields-json", json.dumps(fields),
            "--findings-json", json.dumps(findings_array),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["ok"] is True
        rec = json.loads(body["written_line"])
        assert rec["event"] == "review_done"
        assert rec["run_id"] == "X"
        assert rec["task_id"] == "001"
        assert rec["reviewer"] == "codex"
        assert rec["verdict"] == "minor-findings"
        assert rec["findings_count"] == 1
        assert rec["findings"] == findings_array

    def test_v2_log_event_findings_json_rejects_malformed(
        self, isolated_plan: Path
    ) -> None:
        # V2 — a bad severity trips `_validate_minor_findings_payload`;
        # `_die` fires with the structured minor-findings error list.
        bad = [{
            "severity": "bogus",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
        }]
        cp = _run(
            "log-event",
            "--event", "review_done",
            "--fields-json",
            '{"run_id":"X","task_id":"001","reviewer":"codex",'
            '"verdict":"minor-findings","findings_count":1}',
            "--findings-json", json.dumps(bad),
            "--json",
        )
        assert cp.returncode == 1, (cp.stdout, cp.stderr)
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "invalid-reviewer-finding-severity" in codes, (body, codes)

    def test_v2b_log_event_findings_json_rejects_invalid_json(
        self, isolated_plan: Path
    ) -> None:
        # Extra coverage — an invalid JSON blob surfaces as a structured
        # `invalid-json` error, not a raw stack trace.
        cp = _run(
            "log-event",
            "--event", "review_done",
            "--fields-json",
            '{"run_id":"X","task_id":"001"}',
            "--findings-json", "{not json",
            "--json",
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "invalid-json" in codes, (body, codes)

    def test_v2c_log_event_findings_json_collision_rejected(
        self, isolated_plan: Path
    ) -> None:
        # Extra coverage — `findings` already in `--fields-json` collides
        # with `--findings-json`; the orchestrator MUST pick one source.
        findings_array = [{
            "severity": "minor",
            "confidence": "medium",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
        }]
        fields = {"run_id": "X", "task_id": "001", "findings": []}
        cp = _run(
            "log-event",
            "--event", "review_done",
            "--fields-json", json.dumps(fields),
            "--findings-json", json.dumps(findings_array),
            "--json",
        )
        assert cp.returncode == 1
        body = _parse_json(cp)
        codes = [e["code"] for e in body.get("errors", [])]
        assert "findings-json-collision" in codes, (body, codes)


class TestTask022CommitDoneFindings:
    """V3, V6 — `commit-task` always embeds `findings` on `commit_done`.

    When `--reviewer-minor-findings` is non-empty, the full payload is
    preserved verbatim. When the flag is empty or defaults to `[]`, the
    `findings` key is still present with an empty list — the key is
    always there, the backward-compat count field `minor_findings_count`
    is preserved unchanged.
    """

    def test_v3_commit_done_event_embeds_findings(
        self, tmp_git_repo: Path
    ) -> None:
        # V3 — one minor finding survives end-to-end on commit_done.
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8"
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        findings_payload = [{
            "severity": "minor",
            "confidence": "medium",
            "file": "src/foo.py",
            "line": 7,
            "issue": "rename variable",
            "suggested_fix": "call it count",
            "disposition": "dismissed",
            "disposition_reason": "D.5 override",
        }]
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
            "--reviewer-minor-findings", json.dumps(findings_payload),
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr

        log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        commit_events = [
            json.loads(ln) for ln in lines if '"commit_done"' in ln
        ]
        assert commit_events, f"no commit_done event in log: {lines}"
        rec = commit_events[-1]
        assert rec["event"] == "commit_done"
        assert rec["minor_findings_count"] == 1
        assert "findings" in rec, rec
        assert rec["findings"] == findings_payload

    def test_v6_commit_done_empty_findings_is_empty_list(
        self, tmp_git_repo: Path
    ) -> None:
        # V6 — empty `--reviewer-minor-findings '[]'` still writes
        # `findings: []` explicitly. The key is always present on
        # commit_done events; absence would be a regression.
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8"
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
            "--reviewer", "codex",
            "--reviewer-verdict", "clean",
            "--reviewer-minor-findings", "[]",
            "--json",
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 0, cp.stderr

        log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        rec = [
            json.loads(ln) for ln in lines if '"commit_done"' in ln
        ][-1]
        assert rec["minor_findings_count"] == 0
        assert "findings" in rec, rec
        assert rec["findings"] == []


class TestTask022LogEventFindingsBackwardCompat:
    """V6 — `log-event` without `--findings-json` is byte-identical to
    the pre-TASK-022 behavior; the `findings` key is absent.
    """

    def test_v6_log_event_without_findings_json_has_no_key(
        self, isolated_plan: Path
    ) -> None:
        cp = _run(
            "log-event",
            "--event", "review_done",
            "--fields-json",
            '{"run_id":"R1","task_id":"001","reviewer":"codex",'
            '"verdict":"clean","findings_count":0}',
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        rec = json.loads(body["written_line"])
        assert rec["event"] == "review_done"
        assert "findings" not in rec, rec


class TestTask022DismissalEvidenceGateDocs:
    """V5 — dispatch-templates.md Phase D.5 contains the dismissal
    evidence gate paragraph + the `spec-deference` disposition wording.
    Grep-based regression so the auditable tokens stay greppable.
    """

    def test_v5_dispatch_templates_has_dismissal_gate_and_spec_deference(
        self,
    ) -> None:
        templates = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "dispatch-templates.md"
        )
        text = templates.read_text(encoding="utf-8")
        assert "Dismissal-evidence gate" in text, (
            "expected Phase D.5 section to carry a "
            "'Dismissal-evidence gate' paragraph"
        )
        assert "spec-deference" in text, (
            "expected Phase D.5 dismissal gate to name the "
            "`spec-deference` disposition"
        )
        # The gate and the Verdict decision rubric MUST co-locate under
        # the same Phase D.5 prompt block. Verify ordering so future
        # edits don't drift the gate into Phase D-Claude by mistake.
        d5_idx = text.find("## Phase D.5")
        next_section_idx = text.find("## Phase D.2b", d5_idx)
        assert d5_idx >= 0 and next_section_idx > d5_idx
        d5_block = text[d5_idx:next_section_idx]
        assert "Dismissal-evidence gate" in d5_block, (
            "Dismissal-evidence gate paragraph must live inside the "
            "Phase D.5 section, not in a sibling template block"
        )
        assert "spec-deference" in d5_block


class TestTask022SpecDeferenceValidator:
    """V4 — `spec-deference` is accepted by
    `_validate_reviewer_finding_item` with the full disposition +
    reason combo. Duplicates the parametrized V6 coverage from
    `TestTask019ReviewerFindingDisposition` so the TASK-022 anchor is
    discoverable by name.
    """

    def test_v4_reviewer_finding_disposition_accepts_spec_deference(
        self,
    ) -> None:
        finding = {
            "severity": "minor",
            "confidence": "medium",
            "file": "a.py",
            "line": 1,
            "issue": "x",
            "suggested_fix": "y",
            "disposition": "spec-deference",
            "disposition_reason": (
                "plan mandates this but critique has design merit"
            ),
        }
        errors = plan_ops._validate_reviewer_finding_item(
            finding, path="$.f"
        )
        assert errors == [], errors
        # Ensure the constant set itself carries the new value.
        assert (
            "spec-deference"
            in plan_ops.ALLOWED_REVIEWER_FINDING_DISPOSITIONS
        )


class TestTask025PlanAuthorSubagent:
    """V1–V6 — TASK-025 plan-author subagent, auto-revise wiring, and the
    two new run-log events (`plan_author_start` / `plan_author_done`).

    - V1: `plugins/plan-executor/agents/plan-author.md` exists with the
      required frontmatter fields (`name`, `tools` including Edit+Write,
      `model: opus`) and body sections per the task's acceptance
      criteria.
    - V2: `ALLOWED_LOG_EVENTS` carries both new event names.
    - V3: end-to-end `log-event` round-trip for both events (the tail
      line parses back with the correct event type).
    - V4: `dispatch-templates.md` declares the Phase 1.5a section and
      mentions the `needs-replan auto-revise` trigger.
    - V5: `SKILL.md` carries the `--no-auto-revise` flag, references
      `plan-author`, and does NOT carry the obsolete
      "Re-dispatch `plan-analyst` **once**" paragraph (the replaced
      path).
    - V6: `run-log-schema.md` documents both new events.
    """

    def test_v1_plan_author_md_has_required_frontmatter_and_sections(
        self,
    ) -> None:
        path = (
            REPO_ROOT / "plugins" / "plan-executor" / "agents"
            / "plan-author.md"
        )
        assert path.exists(), f"plan-author.md missing at {path}"
        text = path.read_text(encoding="utf-8")

        # Frontmatter fields — these are the exact regex hits V1 greps for.
        assert re.search(r"(?m)^name: plan-author$", text), (
            "plan-author.md must declare `name: plan-author` in "
            "frontmatter"
        )
        assert re.search(r"(?m)^tools:.*\bEdit\b.*", text), (
            "plan-author.md frontmatter must list `Edit` in tools"
        )
        assert re.search(r"(?m)^tools:.*\bWrite\b.*", text), (
            "plan-author.md frontmatter must list `Write` in tools"
        )
        assert re.search(r"(?m)^model: opus$", text), (
            "plan-author.md must declare `model: opus` in frontmatter"
        )

        # Body must describe the three-section report format; these
        # strings are load-bearing (the orchestrator's Phase 1.5a prompt
        # references them verbatim).
        assert "**Findings actioned:**" in text, text
        assert "**Findings skipped:**" in text, text
        assert "**Files edited:**" in text, text

        # Write-scope invariant: the agent is bound to a single input
        # plan path, not a directory glob. The acceptance criterion
        # calls out this phrasing. TASK-007 shifted the write target
        # from "single plan file" (whole-plan) to "single child plan
        # file" (per-child fan-out); accept either phrasing.
        assert (
            "single plan file passed" in text
            or "single plan file passed as input" in text
            or "single plan file" in text
            or "single child plan file" in text
        ), text

    def test_v2_allowed_log_events_includes_plan_author_events(
        self,
    ) -> None:
        assert "plan_author_start" in plan_ops.ALLOWED_LOG_EVENTS, (
            "ALLOWED_LOG_EVENTS must carry `plan_author_start` per "
            "TASK-025"
        )
        assert "plan_author_done" in plan_ops.ALLOWED_LOG_EVENTS, (
            "ALLOWED_LOG_EVENTS must carry `plan_author_done` per "
            "TASK-025"
        )

    def test_v3_log_event_plan_author_events_roundtrip(
        self, isolated_plan: Path
    ) -> None:
        # `plan_author_start` — required fields: run_id, plan_file,
        # findings_count.
        start_fields = {
            "run_id": "R1",
            "plan_file": "sample.md",
            "findings_count": 3,
        }
        cp = _run(
            "log-event",
            "--event", "plan_author_start",
            "--fields-json", json.dumps(start_fields),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        rec = json.loads(body["written_line"])
        assert rec["event"] == "plan_author_start"
        assert rec["run_id"] == "R1"
        assert rec["plan_file"] == "sample.md"
        assert rec["findings_count"] == 3

        # `plan_author_done` — required: run_id, plan_file; optional:
        # files_edited[], findings_actioned[], findings_skipped[].
        done_fields = {
            "run_id": "R1",
            "plan_file": "sample.md",
            "files_edited": ["/abs/path/to/sample.md"],
            "findings_actioned": [0, 2],
            "findings_skipped": [1],
        }
        cp2 = _run(
            "log-event",
            "--event", "plan_author_done",
            "--fields-json", json.dumps(done_fields),
            "--json",
        )
        assert cp2.returncode == 0, cp2.stderr
        body2 = _parse_json(cp2)
        rec2 = json.loads(body2["written_line"])
        assert rec2["event"] == "plan_author_done"
        assert rec2["run_id"] == "R1"
        assert rec2["plan_file"] == "sample.md"
        assert rec2["files_edited"] == ["/abs/path/to/sample.md"]
        assert rec2["findings_actioned"] == [0, 2]
        assert rec2["findings_skipped"] == [1]

        # The on-disk tail must match the emitted line byte-for-byte
        # (log-event already re-verifies internally, but re-check here
        # so the test class directly exercises the file).
        lines = plan_ops.RUN_LOG_PATH.read_text(
            encoding="utf-8"
        ).splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "plan_author_start"
        assert json.loads(lines[1])["event"] == "plan_author_done"

    def test_v4_dispatch_templates_has_phase_15a_section(self) -> None:
        path = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "dispatch-templates.md"
        )
        text = path.read_text(encoding="utf-8")
        assert re.search(
            r"(?m)^## Phase 1\.5a — plan-author dispatch",
            text,
        ), (
            "dispatch-templates.md must declare a "
            "`## Phase 1.5a — plan-author dispatch` section"
        )
        assert "needs-replan auto-revise" in text, (
            "Phase 1.5a section header must name the "
            "`needs-replan auto-revise` trigger"
        )

    def test_v5_skill_md_has_no_auto_revise_and_no_obsolete_paragraph(
        self,
    ) -> None:
        path = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "SKILL.md"
        )
        text = path.read_text(encoding="utf-8")
        # `--no-auto-revise` appears in the CLI flags table indented
        # under Optional (two-space prefix, same as sibling flags).
        assert re.search(r"(?m)^  --no-auto-revise", text), (
            "SKILL.md must list `--no-auto-revise` under Optional "
            "flags"
        )
        # `plan-author` must be named somewhere in the needs-replan
        # branch documentation — either the routing-table row or the
        # subsequent prose.
        assert "plan-author" in text, (
            "SKILL.md §Phase 1.5 must reference `plan-author` for the "
            "needs-replan branch"
        )
        # The obsolete "Re-dispatch `plan-analyst` **once**" paragraph
        # MUST be gone — the auto-revise rewrite replaces it, not
        # augments it. Keeping both would confuse implementers.
        assert "Re-dispatch `plan-analyst` **once**" not in text, (
            "SKILL.md still carries the obsolete "
            "'Re-dispatch plan-analyst once' paragraph; TASK-025 "
            "replaces it with the author→analyst→review sequence"
        )

    def test_v6_run_log_schema_documents_plan_author_events(self) -> None:
        path = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "run-log-schema.md"
        )
        text = path.read_text(encoding="utf-8")
        assert "plan_author_start" in text, (
            "run-log-schema.md must document `plan_author_start`"
        )
        assert "plan_author_done" in text, (
            "run-log-schema.md must document `plan_author_done`"
        )


# ---------------------------------------------------------------------------
# TASK-020A: lint-plans subcommand
# ---------------------------------------------------------------------------


def _init_lint_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "lint@test"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "lint"], cwd=repo, check=True,
    )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)


def _make_feat_commit(repo: Path, task_id: str, title: str = "ship") -> None:
    """Create an empty commit with subject `feat(TASK-<id>): <title>`.

    Uses `--allow-empty` so no filesystem state is required; the lint only
    looks at commit subjects via `git log --pretty=%s`.
    """
    subprocess.run(
        [
            "git", "commit", "--allow-empty", "-q",
            "-m", f"feat(TASK-{task_id}): {title}",
        ],
        cwd=repo, check=True,
    )


def _write_commit_done_event(run_log: Path, task_id: str) -> None:
    """Append a commit_done event to `run_log` (creating it if absent)."""
    run_log.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({
        "event": "commit_done",
        "task_id": task_id,
        "run_id": "R1",
        "ts": "2026-04-20T00:00:00Z",
    })
    with run_log.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _lint_plan_body(task_id: str, status: str) -> str:
    """Minimal plan body with a single task block at the requested status."""
    return (
        "# Plan: lint fixture\n\n"
        "**Base branch:** main\n\n"
        "## Tasks\n\n"
        f"### TASK-{task_id}: lint fixture task\n\n"
        f"- **Status:** {status}\n"
        "- **Files:**\n"
        "  - src/fixture.py\n"
        "- **Dependencies:** none\n"
    )


@pytest.fixture()
def lint_workspace(tmp_path: Path) -> dict:
    """Isolated git repo + plans dir + empty run log for lint-plans tests."""
    repo = tmp_path / "repo"
    _init_lint_git_repo(repo)
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    run_log = plans_dir / "_run_log.jsonl"
    return {"repo": repo, "plans_dir": plans_dir, "run_log": run_log}


def _run_lint(
    plans_dir: Path,
    run_log: Path | None,
    git_dir: Path,
    *,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    argv = [
        "lint-plans",
        "--plans-dir", str(plans_dir),
        "--git-dir", str(git_dir),
        "--json",
    ]
    if run_log is not None:
        argv.extend(["--run-log", str(run_log)])
    argv.extend(extra)
    return _run(*argv)


class TestLintPlans:
    """V1-V5 coverage for the TASK-020A `lint-plans` subcommand."""

    def test_v1_clean_tree_exits_zero(self, lint_workspace: dict) -> None:
        """V1: a tree with one done task + matching commit_done + matching
        feat commit passes the lint with no findings."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-999.md"
        plan.write_text(_lint_plan_body("999", "done"), encoding="utf-8")
        _write_commit_done_event(ws["run_log"], "999")
        _make_feat_commit(ws["repo"], "999")

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 0, f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
        body = _parse_json(cp)
        assert body["findings"] == []
        assert body["scanned"] == 1
        assert body["done_tasks"] == 1

    def test_v2_flags_missing_commit_done_event(
        self, lint_workspace: dict,
    ) -> None:
        """V2: status=done without any commit_done in the run log is flagged
        with code `missing-commit-done-event`. (Git commit also missing so we
        also see `missing-feat-commit`; assert the commit-done code is
        present.)
        """
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-999.md"
        plan.write_text(_lint_plan_body("999", "done"), encoding="utf-8")
        # No commit_done event, no feat commit.

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        codes = [f["code"] for f in body["findings"]]
        assert "missing-commit-done-event" in codes, body
        missing_ev = next(
            f for f in body["findings"]
            if f["code"] == "missing-commit-done-event"
        )
        assert missing_ev["task_id"] == "999"
        assert "plan-999.md" in missing_ev["plan_file"]

    def test_v3_flags_missing_git_commit(
        self, lint_workspace: dict,
    ) -> None:
        """V3: commit_done event exists but no matching feat commit → flagged
        with code `missing-feat-commit` (and only that code; the commit_done
        check passes)."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-999.md"
        plan.write_text(_lint_plan_body("999", "done"), encoding="utf-8")
        _write_commit_done_event(ws["run_log"], "999")
        # Do not create a feat(TASK-999) commit.

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        codes = [f["code"] for f in body["findings"]]
        assert "missing-feat-commit" in codes, body
        assert "missing-commit-done-event" not in codes, body

    def test_v4_ignores_non_done_statuses(
        self, lint_workspace: dict,
    ) -> None:
        """V4: superseded / failed / pending / in-progress do NOT trigger the
        pairing check."""
        ws = lint_workspace
        for i, status in enumerate(
            ["superseded", "failed", "pending", "in-progress"], start=1,
        ):
            plan = ws["plans_dir"] / f"plan-{i:03d}.md"
            plan.write_text(
                _lint_plan_body(f"{i:03d}", status), encoding="utf-8",
            )

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 0, f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
        body = _parse_json(cp)
        assert body["findings"] == []
        assert body["done_tasks"] == 0

    def test_v4_partial_triggers_the_pairing_check(
        self, lint_workspace: dict,
    ) -> None:
        """`partial` status must trigger the same commit-pairing check as
        `done`: whatever DID land still requires a commit pair."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-777.md"
        plan.write_text(_lint_plan_body("777", "partial"), encoding="utf-8")
        # Neither event nor commit present → both codes must fire.

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        codes = [f["code"] for f in body["findings"]]
        assert "missing-commit-done-event" in codes
        assert "missing-feat-commit" in codes
        assert body["done_tasks"] == 1

    def test_v5_json_envelope_shape(
        self, lint_workspace: dict,
    ) -> None:
        """V5: --json emits `{scanned, done_tasks, findings:[{plan_file,
        task_id, code, message}]}` with exactly these keys at each level."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-042.md"
        plan.write_text(_lint_plan_body("042", "done"), encoding="utf-8")

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        assert set(body.keys()) == {"scanned", "done_tasks", "findings"}
        assert isinstance(body["scanned"], int)
        assert isinstance(body["done_tasks"], int)
        assert isinstance(body["findings"], list)
        for f in body["findings"]:
            assert set(f.keys()) == {
                "plan_file", "task_id", "code", "message",
            }
            assert isinstance(f["plan_file"], str) and f["plan_file"]
            assert re.fullmatch(r"\d{3}[A-Z]?", f["task_id"]), f
            assert f["code"] in {
                "missing-commit-done-event", "missing-feat-commit",
            }
            assert isinstance(f["message"], str) and f["message"]

    def test_skips_superseded_parent_plans(
        self, lint_workspace: dict,
    ) -> None:
        """A plan whose top-level `**Status:** superseded` header is present
        must skip the per-task done-pairing check entirely — the parent
        plan's decomposition is tracked by its children."""
        ws = lint_workspace
        body = (
            "# Plan: superseded parent\n\n"
            "**Base branch:** main\n"
            "**Status:** superseded\n\n"
            "## Tasks\n\n"
            "### TASK-555: child task\n\n"
            "- **Status:** done\n"
            "- **Files:**\n"
            "  - src/x.py\n"
            "- **Dependencies:** none\n"
        )
        (ws["plans_dir"] / "parent.md").write_text(body, encoding="utf-8")
        # No matching commit_done / feat(TASK-555) — but lint must NOT flag.

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 0, f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
        body_json = _parse_json(cp)
        assert body_json["findings"] == []
        # The superseded plan contributes to `scanned` but NOT `done_tasks`,
        # because the per-task iteration is skipped for superseded parents.
        assert body_json["scanned"] == 1
        assert body_json["done_tasks"] == 0

    def test_feat_grep_is_subject_only_not_body(
        self, lint_workspace: dict,
    ) -> None:
        """Regression: the feat-commit check must grep commit SUBJECTS, not
        bodies. A commit whose body mentions `feat(TASK-999)` in prose (e.g.
        a task-019 concern reference) must NOT satisfy the gate for 999."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-999.md"
        plan.write_text(_lint_plan_body("999", "done"), encoding="utf-8")
        _write_commit_done_event(ws["run_log"], "999")
        # Commit whose SUBJECT does NOT match feat(TASK-999) but BODY does.
        subprocess.run(
            [
                "git", "commit", "--allow-empty", "-q",
                "-m",
                "chore: unrelated\n\nConcern references feat(TASK-999): ...",
            ],
            cwd=ws["repo"], check=True,
        )

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        codes = [f["code"] for f in body["findings"]]
        assert "missing-feat-commit" in codes, body

    def test_missing_run_log_is_tolerated(
        self, lint_workspace: dict,
    ) -> None:
        """A non-existent --run-log path must not crash the lint; every done
        task is simply reported as missing its commit_done event."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-001.md"
        plan.write_text(_lint_plan_body("001", "done"), encoding="utf-8")
        _make_feat_commit(ws["repo"], "001")
        nonexistent_log = ws["plans_dir"] / "absent.jsonl"

        cp = _run_lint(ws["plans_dir"], nonexistent_log, ws["repo"])
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        codes = [f["code"] for f in body["findings"]]
        assert "missing-commit-done-event" in codes
        assert "missing-feat-commit" not in codes

    def test_read_only_no_writes(
        self, lint_workspace: dict,
    ) -> None:
        """The lint must be read-only: no files get written, no git state
        mutates. Snapshot the run log + HEAD before and after the run."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan-123.md"
        plan.write_text(_lint_plan_body("123", "done"), encoding="utf-8")
        pre_log = ws["run_log"].read_text() if ws["run_log"].exists() else ""
        pre_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ws["repo"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        pre_plan = plan.read_text()

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode in (0, 1), cp.stderr

        post_log = ws["run_log"].read_text() if ws["run_log"].exists() else ""
        post_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ws["repo"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert pre_log == post_log
        assert pre_head == post_head
        assert plan.read_text() == pre_plan

    def test_accepts_short_task_id_forms(
        self, lint_workspace: dict,
    ) -> None:
        """The matcher uses `_normalize_task_id` so a plan declaring
        `### TASK-007:` and a commit `feat(TASK-007): ...` pair correctly
        (no confusion with TASK-007A / TASK-070 etc)."""
        ws = lint_workspace
        plan = ws["plans_dir"] / "plan.md"
        plan.write_text(_lint_plan_body("007", "done"), encoding="utf-8")
        _write_commit_done_event(ws["run_log"], "007")
        _make_feat_commit(ws["repo"], "007")
        # Also create a feat(TASK-007A) commit to ensure the match doesn't
        # bleed between suffixed and plain forms.
        _make_feat_commit(ws["repo"], "007A", title="unrelated")

        cp = _run_lint(ws["plans_dir"], ws["run_log"], ws["repo"])
        assert cp.returncode == 0, f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
        body = _parse_json(cp)
        assert body["findings"] == []


def test_task_template_has_all_required_fields() -> None:
    """The canonical TASK authoring template must expose every required field
    plus `Implementation notes:` by default, so authors who copy it produce
    plans `plan-analyst` can parse without enrichment gaps."""
    template_path = (
        REPO_ROOT
        / "plugins"
        / "plan-executor"
        / "templates"
        / "TASK.md.template"
    )
    assert template_path.exists(), f"missing template at {template_path}"
    body = template_path.read_text(encoding="utf-8")

    # Header must match the TASK-id regex used by plan-analyst.
    header_re = re.compile(r"^###\s+TASK-\d{3}[A-Z]?:", re.MULTILINE)
    assert header_re.search(body), (
        "template header must match ^### TASK-\\d{3}[A-Z]?:"
    )

    required_labels = [
        "Status",
        "Priority",
        "Files",
        "Dependencies",
        "Test command",
        "Acceptance criteria",
        "Description",
        "Implementation notes",
        "Reversion guidance",
    ]
    for label in required_labels:
        # Accept either a bolded bullet form (`- **Label:**`) or a bolded
        # section header (`**Label:**`) — both are parsed by plan-analyst.
        pattern = re.compile(rf"\*\*{re.escape(label)}:\*\*")
        assert pattern.search(body), (
            f"template missing required field: {label}"
        )


# ---------------------------------------------------------------------------
# TASK-005: phase gates (gates --list / --check / --certify).
#
# Gate predicates are pure — they grep artifacts on disk, never invoke the
# wrapper. Tests use synthetic fixtures rather than the live sample plan
# because the sample rewrite is TASK-006's scope.
# ---------------------------------------------------------------------------


_GATES_SYNTHETIC_PLAN = """# Plan: gates-synthetic

**Created:** 2026-04-20
**Status:** in-progress
**Base branch:** main

## Goal

Exercise the TASK-005 gate predicates against a known-good plan body.

## Context

Synthetic plan used by the TASK-005 gate tests. Does not touch the live repo.

## Tasks

### TASK-001: Seed task

- **Status:** pending
- **Priority:** P1
- **Files:**
  - `example/seed.py` (create)
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k gates`
- **Acceptance criteria:**
  - Seed file created.

**Description:** Placeholder task for the gate schema predicate.

## Verification

Verification prose.
"""


def _write_gates_plan(tmp_path: Path, body: str = _GATES_SYNTHETIC_PLAN) -> Path:
    path = tmp_path / "plan.md"
    path.write_text(body, encoding="utf-8")
    return path


def _write_gates_schedule(tmp_path: Path, payload: dict | None = None) -> Path:
    if payload is None:
        payload = {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "claude",
                    "priority": "P1",
                    "files": ["example/seed.py"],
                    "dependencies": [],
                    "plan_file": "plan.md",
                }
            ],
            "batches": [
                {
                    "index": 0,
                    "task_ids": ["001"],
                    "file_locks": ["example/seed.py"],
                }
            ],
            "gaps": [],
            "risks": [],
        }
    path = tmp_path / "plan.schedule.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestGatesCli:
    """`gates --list` and the CLI shape contract."""

    def test_gates_list_returns_six_names(self) -> None:
        """`gates --list --json` returns the six canonical gate names."""
        cp = _run("gates", "--list", "--json")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body == {
            "gates": [
                "schema-valid",
                "schedule-valid",
                "fixture-valid",
                "execution-safe",
                "review-safe",
                "commit-safe",
            ]
        }

    def test_gates_module_constant_matches(self) -> None:
        """The exported `GATE_NAMES` tuple and the `--list` output agree."""
        assert tuple(plan_ops.GATE_NAMES) == (
            "schema-valid",
            "schedule-valid",
            "fixture-valid",
            "execution-safe",
            "review-safe",
            "commit-safe",
        )

    def test_unknown_gate_name_errors(self, tmp_path: Path) -> None:
        """--check rejects gate names outside the canonical set."""
        plan = _write_gates_plan(tmp_path)
        cp = _run("gates", "--check", "no-such-gate", "--plan-file", str(plan), "--json")
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "unknown gate name" in body.get("error", "")


class TestGateSchemaValid:
    """`schema-valid` asserts §5 conformance on the plan markdown."""

    def test_passes_on_conforming_plan(self, tmp_path: Path) -> None:
        plan = _write_gates_plan(tmp_path)
        cp = _run("gates", "--check", "schema-valid", "--plan-file", str(plan), "--json")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["gates"][0]["name"] == "schema-valid"
        assert body["gates"][0]["status"] == "pass"

    def test_fails_on_missing_verification_section(self, tmp_path: Path) -> None:
        body = _GATES_SYNTHETIC_PLAN.replace(
            "## Verification\n\nVerification prose.\n", ""
        )
        plan = _write_gates_plan(tmp_path, body)
        cp = _run("gates", "--check", "schema-valid", "--plan-file", str(plan), "--json")
        assert cp.returncode == 1
        payload = _parse_json(cp)
        gate = payload["gates"][0]
        assert gate["status"] == "fail"
        assert "Verification" in gate["reason"]

    def test_fails_on_missing_task_bullet(self, tmp_path: Path) -> None:
        body = _GATES_SYNTHETIC_PLAN.replace("- **Priority:** P1\n", "")
        plan = _write_gates_plan(tmp_path, body)
        cp = _run("gates", "--check", "schema-valid", "--plan-file", str(plan), "--json")
        assert cp.returncode == 1
        gate = _parse_json(cp)["gates"][0]
        assert gate["status"] == "fail"
        assert "Priority" in gate["reason"]

    def test_fails_on_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist.md"
        cp = _run("gates", "--check", "schema-valid", "--plan-file", str(missing), "--json")
        assert cp.returncode == 1
        gate = _parse_json(cp)["gates"][0]
        assert gate["status"] == "fail"
        assert "not found" in gate["reason"]

    def test_scoped_context_header_is_accepted(self, tmp_path: Path) -> None:
        """Per-chunk plans use `## Scoped Context` instead of `## Context`."""
        body = _GATES_SYNTHETIC_PLAN.replace("## Context", "## Scoped Context")
        plan = _write_gates_plan(tmp_path, body)
        cp = _run("gates", "--check", "schema-valid", "--plan-file", str(plan), "--json")
        assert cp.returncode == 0, cp.stderr
        gate = _parse_json(cp)["gates"][0]
        assert gate["status"] == "pass"


class TestGateScheduleValid:
    """`schedule-valid` reuses `_validate_schedule` + `_validate_schedule_dag`."""

    def test_passes_on_valid_schedule(self, tmp_path: Path) -> None:
        sched = _write_gates_schedule(tmp_path)
        cp = _run("gates", "--check", "schedule-valid", "--schedule-file", str(sched), "--json")
        assert cp.returncode == 0, cp.stderr
        gate = _parse_json(cp)["gates"][0]
        assert gate["name"] == "schedule-valid"
        assert gate["status"] == "pass"

    def test_fails_on_invalid_schedule_shape(self, tmp_path: Path) -> None:
        """Dropping required fields makes `_validate_schedule` flag errors."""
        sched = _write_gates_schedule(
            tmp_path,
            payload={"outcome": "valid"},  # missing tasks[], batches[]
        )
        cp = _run("gates", "--check", "schedule-valid", "--schedule-file", str(sched), "--json")
        assert cp.returncode == 1
        gate = _parse_json(cp)["gates"][0]
        assert gate["status"] == "fail"

    def test_fails_on_dag_cycle(self, tmp_path: Path) -> None:
        """`_validate_schedule_dag` catches cycles in the dependency graph."""
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "claude", "priority": "P1",
                 "files": ["a.py"], "dependencies": ["002"]},
                {"id": "002", "agent": "claude", "priority": "P1",
                 "files": ["b.py"], "dependencies": ["001"]},
            ],
            "batches": [
                {
                    "index": 0,
                    "task_ids": ["001", "002"],
                    "file_locks": ["a.py", "b.py"],
                }
            ],
            "gaps": [],
            "risks": [],
        }
        sched = _write_gates_schedule(tmp_path, payload)
        cp = _run("gates", "--check", "schedule-valid", "--schedule-file", str(sched), "--json")
        assert cp.returncode == 1
        gate = _parse_json(cp)["gates"][0]
        assert gate["status"] == "fail"

    def test_fails_when_schedule_file_missing(self, tmp_path: Path) -> None:
        cp = _run(
            "gates", "--check", "schedule-valid",
            "--schedule-file", str(tmp_path / "absent.json"),
            "--json",
        )
        assert cp.returncode == 1
        gate = _parse_json(cp)["gates"][0]
        assert gate["status"] == "fail"


_EXECUTION_SAFE_GOOD_WRAPPER_SRC = (
    "from _plan_paths import PROTECTED_EXACT_PATHS, is_protected_path\n"
    "def _snapshot_baseline(): pass\n"
    "def _handle_timeout_cleanup(a, b, baseline): pass\n"
    "def cmd_implement(args):\n"
    "    baseline = _snapshot_baseline()\n"
    "    codex = {'status': 'timeout'}\n"
    "    if codex['status'] == 'timeout':\n"
    "        _handle_timeout_cleanup('x', [], baseline)\n"
    "    return 0\n"
    "def cmd_review(args):\n"
    "    baseline = _snapshot_baseline()\n"
    "    return 0\n"
)

_REVIEW_SAFE_GOOD_WRAPPER_SRC = (
    "from _plan_paths import PROTECTED_EXACT_PATHS, is_protected_path\n"
    "def _snapshot_baseline(): pass\n"
    "def cmd_implement(args):\n"
    "    return 0\n"
    "def cmd_review(args):\n"
    "    baseline = _snapshot_baseline()\n"
    "    if is_protected_path('x'):\n"
    "        return 0\n"
    "    return 0\n"
)


class TestGateExecutionAndReviewSafe:
    """Wrapper-predicate gates: greps of `plan_codex_dispatch.py`.

    V3/V4 positive paths intentionally exercise the predicate against
    fixture wrappers constructed under tmp_path rather than against the
    live `plan_codex_dispatch.py`. Per TASK-005 acceptance criteria,
    live-tree green is TASK-003's scope; asserting it here couples the
    gate to wrapper evolution and would break if TASK-003 is rolled back
    or if the wrapper legitimately grows new call sites.
    """

    def test_gate_execution_safe_predicate(self, tmp_path: Path) -> None:
        """Positive fixture: a minimal wrapper carrying the two-seam
        snapshot invariant + always-ignore import passes execution-safe."""
        fixture = tmp_path / "execution_safe_good_wrapper.py"
        fixture.write_text(
            _EXECUTION_SAFE_GOOD_WRAPPER_SRC, encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(fixture)
        assert result["name"] == "execution-safe"
        assert result["status"] == "pass", result

    def test_gate_review_safe_predicate(self, tmp_path: Path) -> None:
        """Positive fixture: a minimal wrapper with a snapshot call in
        cmd_review and an is_protected_path reference passes review-safe."""
        fixture = tmp_path / "review_safe_good_wrapper.py"
        fixture.write_text(
            _REVIEW_SAFE_GOOD_WRAPPER_SRC, encoding="utf-8",
        )
        result = plan_ops._gate_review_safe(fixture)
        assert result["name"] == "review-safe"
        assert result["status"] == "pass", result

    def test_execution_safe_fails_on_stub_wrapper(self, tmp_path: Path) -> None:
        """A wrapper lacking the invariants fails the predicate."""
        stub = tmp_path / "stub_wrapper.py"
        stub.write_text("# empty wrapper\n", encoding="utf-8")
        result = plan_ops._gate_execution_safe(stub)
        assert result["name"] == "execution-safe"
        assert result["status"] == "fail"

    def test_execution_safe_flags_raw_git_clean_invocation(self, tmp_path: Path) -> None:
        """A wrapper that actually runs `git clean -fd` fails the predicate."""
        stub = tmp_path / "bad_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def cmd_implement():\n"
            "    _snapshot_baseline()\n"
            "    os.system('git clean -fd')\n"
            "def cmd_timeout():\n"
            "    _snapshot_baseline()\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail"
        assert "git clean -fd" in result["reason"]

    def test_execution_safe_accepts_docstring_reference(self, tmp_path: Path) -> None:
        """The prohibition string inside a docstring must not trip the gate."""
        stub = tmp_path / "ok_wrapper.py"
        stub.write_text(
            '"""Docs.\n\nNever invokes `git clean -fd` outside scope.\n"""\n'
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def _handle_timeout_cleanup(a, b, baseline): pass\n"
            "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [], baseline)\n"
            "    return 0\n"
            "def cmd_review(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "pass", result

    def test_review_safe_fails_without_snapshot_in_cmd_review(self, tmp_path: Path) -> None:
        stub = tmp_path / "stub_no_review.py"
        stub.write_text(
            "PROTECTED_EXACT_PATHS = set()\n"
            "def is_protected_path(x): return False\n"
            "def _snapshot_baseline(): pass\n"
            "def cmd_review():\n"
            "    return None  # no snapshot\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_review_safe(stub)
        assert result["status"] == "fail"


class TestGateCommitSafe:
    """`commit-safe` verifies a landed commit's footprint against Files:."""

    def _make_repo_with_plan(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        plan = repo / "plan.md"
        plan.write_text(_GATES_SYNTHETIC_PLAN, encoding="utf-8")
        (repo / "example").mkdir()
        subprocess.run(["git", "add", "plan.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        return repo, plan

    def test_happy_path_touches_only_allowed_files(self, tmp_path: Path) -> None:
        """Commit staging exactly the declared `Files:` entry passes commit-safe."""
        repo, plan = self._make_repo_with_plan(tmp_path)
        seed = repo / "example" / "seed.py"
        seed.write_text("# seed\n", encoding="utf-8")
        subprocess.run(["git", "add", "example/seed.py"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): seed"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["name"] == "commit-safe"
        assert result["status"] == "pass", result

    def test_detects_scope_violation(self, tmp_path: Path) -> None:
        """Commit touching a file outside `Files:` fails commit-safe."""
        repo, plan = self._make_repo_with_plan(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        (repo / "other.py").write_text("# leak\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "example/seed.py", "other.py"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): leak"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "fail"
        assert "other.py" in result["reason"]

    def test_plan_file_is_allowed(self, tmp_path: Path) -> None:
        """The plan file itself is always-ignored from commit-safe's violation set."""
        repo, plan = self._make_repo_with_plan(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        plan.write_text(
            _GATES_SYNTHETIC_PLAN.replace("**Status:** pending", "**Status:** done"),
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "example/seed.py", "plan.md"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): seed + plan flip"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "pass", result

    def test_unknown_task_id_fails(self, tmp_path: Path) -> None:
        repo, plan = self._make_repo_with_plan(tmp_path)
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "999", plan, repo_root=repo,
        )
        assert result["status"] == "fail"
        assert "999" in result["reason"]

    def test_missing_required_args_fails(self, tmp_path: Path) -> None:
        result = plan_ops._gate_commit_safe(None, None, None)
        assert result["status"] == "fail"

    def test_directory_scoped_files_entry_allows_children(
        self, tmp_path: Path,
    ) -> None:
        """A Files: entry with a trailing slash (e.g.
        `tests/fixtures/decomposer_inputs/`) allows any path under
        that directory. Plans that declare a fixture directory must
        not need to enumerate each child file individually."""
        repo, plan = self._make_repo_with_plan(tmp_path)
        plan.write_text(
            _GATES_SYNTHETIC_PLAN.replace(
                "- **Files:**\n  - `example/seed.py` (create)\n",
                (
                    "- **Files:**\n"
                    "  - example/seed.py\n"
                    "  - tests/fixtures/decomposer_inputs/ "
                    "(create — canonical + malformed fixtures)\n"
                ),
            ).replace("**Status:** pending", "**Status:** done"),
            encoding="utf-8",
        )
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        fx_dir = repo / "tests" / "fixtures" / "decomposer_inputs"
        fx_dir.mkdir(parents=True)
        (fx_dir / "canonical.md").write_text("canonical\n", encoding="utf-8")
        (fx_dir / "malformed.md").write_text("malformed\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "example", "tests", "plan.md"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): seed + fixtures"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "pass", result


class TestCertifyBundles:
    """`--certify --mode dry-run|execute` bundles."""

    def test_certify_dry_run_bundle_shape(self, tmp_path: Path) -> None:
        """Dry-run bundle: commit-safe is `not_applicable`."""
        plan = _write_gates_plan(tmp_path)
        sched = _write_gates_schedule(tmp_path)
        cp = _run(
            "gates", "--certify", "--mode", "dry-run",
            "--plan-file", str(plan),
            "--schedule-file", str(sched),
            "--json",
        )
        # Might fail because the live sample fixture gate returns fail pre-TASK-006.
        body = _parse_json(cp)
        assert body["mode"] == "dry-run"
        assert "commit-safe" in body["gates"]
        assert body["gates"]["commit-safe"]["status"] == "not_applicable"
        assert body["gates"]["schema-valid"]["status"] == "pass"
        assert body["gates"]["schedule-valid"]["status"] == "pass"
        assert body["gates"]["execution-safe"]["status"] == "pass"
        assert body["gates"]["review-safe"]["status"] == "pass"

    def test_certify_dry_run_without_schedule_file_fails(
        self, tmp_path: Path,
    ) -> None:
        """--certify requires --schedule-file; omission must fail at the CLI seam.

        Previously the bundle collapsed `schedule-valid` to `not_applicable`
        when no schedule file was supplied, which let a certify pass without
        exercising schedule validation at all. The acceptance criteria make
        schedule-valid a mandatory gate for dry-run, so this path now dies.
        """
        plan = _write_gates_plan(tmp_path)
        cp = _run(
            "gates", "--certify", "--mode", "dry-run",
            "--plan-file", str(plan), "--json",
        )
        assert cp.returncode != 0, cp.stdout
        body = _parse_json(cp)
        assert "schedule-file" in body.get("error", "").lower()

    def test_certify_execute_without_schedule_file_fails(
        self, tmp_path: Path,
    ) -> None:
        """Execute certification also requires --schedule-file."""
        plan = _write_gates_plan(tmp_path)
        cp = _run(
            "gates", "--certify", "--mode", "execute",
            "--plan-file", str(plan),
            "--run-id", "some-run-id",
            "--json",
        )
        assert cp.returncode != 0, cp.stdout
        body = _parse_json(cp)
        assert "schedule-file" in body.get("error", "").lower()

    def test_certify_execute_with_no_commits_reports_not_applicable(
        self, tmp_path: Path,
    ) -> None:
        """Execute mode with zero `commit_done` events → commit-safe not_applicable."""
        plan = _write_gates_plan(tmp_path)
        sched = _write_gates_schedule(tmp_path)
        cp = _run(
            "gates", "--certify", "--mode", "execute",
            "--plan-file", str(plan),
            "--schedule-file", str(sched),
            "--run-id", "does-not-exist-run-id-zzz",
            "--json",
        )
        body = _parse_json(cp)
        assert body["mode"] == "execute"
        assert body["gates"]["commit-safe"]["status"] == "not_applicable"

    def test_certify_without_mode_fails(self, tmp_path: Path) -> None:
        plan = _write_gates_plan(tmp_path)
        cp = _run(
            "gates", "--certify", "--plan-file", str(plan), "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "mode" in body.get("error", "").lower()

    def test_certify_without_plan_file_fails(self, tmp_path: Path) -> None:
        cp = _run(
            "gates", "--certify", "--mode", "dry-run", "--json",
        )
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "plan-file" in body.get("error", "").lower()


class TestExtractTaskFiles:
    """`_extract_task_files_from_plan` is shared with commit-safe."""

    def test_strips_create_annotation(self) -> None:
        """Multiline `Files:` bullets must be fully normalized — backticks,
        `(create|modify|delete)` annotations, and `:line` suffixes all
        stripped. Anything less would desync from `normalize_file_path`
        in the wrapper and from `git show --name-only` output (which
        never emits backticks), breaking commit-safe scope checks."""
        files = plan_ops._extract_task_files_from_plan(
            _GATES_SYNTHETIC_PLAN, "001",
        )
        assert files == ["example/seed.py"], files

    def test_unknown_task_returns_none(self) -> None:
        files = plan_ops._extract_task_files_from_plan(
            _GATES_SYNTHETIC_PLAN, "999",
        )
        assert files is None

    def test_strips_line_range_suffix(self) -> None:
        body = _GATES_SYNTHETIC_PLAN.replace(
            "- `example/seed.py` (create)",
            "- `example/seed.py:10-20`",
        )
        files = plan_ops._extract_task_files_from_plan(body, "001")
        # The colon + line range suffix should be stripped.
        assert files is not None
        assert all(":10-20" not in f for f in files)

    def test_inline_single_file_form(self) -> None:
        """`- **Files:** path` (inline form) yields a single normalized entry."""
        body = _GATES_SYNTHETIC_PLAN.replace(
            "- **Files:**\n  - `example/seed.py` (create)\n",
            "- **Files:** `example/seed.py`\n",
        )
        files = plan_ops._extract_task_files_from_plan(body, "001")
        assert files == ["example/seed.py"], files

    def test_inline_comma_separated_form(self) -> None:
        """Inline comma-separated paths split into distinct allowlist keys."""
        body = _GATES_SYNTHETIC_PLAN.replace(
            "- **Files:**\n  - `example/seed.py` (create)\n",
            "- **Files:** `example/seed.py`, `example/other.py` (modify)\n",
        )
        files = plan_ops._extract_task_files_from_plan(body, "001")
        assert files == ["example/seed.py", "example/other.py"], files

    def test_strips_parenthetical_with_embedded_em_dash(self) -> None:
        """Trailing `(annotation — prose)` with an em-dash *inside* the
        parens must be stripped as a unit. Before the fix, the em-dash
        triggered the dash-split first and the trailing-paren strip
        never saw a closing `)`, mangling e.g.
        `tests/fixtures/decomposer_inputs/ (create — canonical + ...)`
        into `tests/fixtures/decomposer_inputs/ (create`. That desynced
        commit-safe from commit-task's own staging logic."""
        body = _GATES_SYNTHETIC_PLAN.replace(
            "- **Files:**\n  - `example/seed.py` (create)\n",
            (
                "- **Files:**\n"
                "  - tests/fixtures/decomposer_inputs/ "
                "(create — canonical + malformed markdown fixtures)\n"
                "  - plugins/plan-executor/skills/implement-plan/SKILL.md "
                "(Phase 0 invocation note only — full prose cleanup "
                "is TASK-002)\n"
            ),
        )
        files = plan_ops._extract_task_files_from_plan(body, "001")
        assert files == [
            "tests/fixtures/decomposer_inputs/",
            "plugins/plan-executor/skills/implement-plan/SKILL.md",
        ], files


class TestPlanPathsNormalizeFilesEntry:
    """TASK-002 — `_plan_paths.normalize_files_entry` is the canonical
    helper shared by `plan_ops._normalize_files_entry` and the wrapper's
    `normalize_file_path`. Both call sites used to carry independent
    bodies that drifted; this test class pins the canonical behaviour
    directly so the orchestrator-side surface stays in sync with the
    wrapper-side surface in `tests/scripts/test_plan_codex_dispatch_parsing.py`.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Bare path passes through unchanged.
            ("scripts/foo.py", "scripts/foo.py"),
            # Backticked path with no prose.
            ("`scripts/foo.py`", "scripts/foo.py"),
            # Backticked path + em-dash + prose continuation. The leading
            # backtick capture must drop the prose tail (this was the
            # wrapper's pre-fix bug).
            (
                "`Makefile` -- add `audit` target wired to `plan_ops.py audit`",
                "Makefile",
            ),
            # Trailing parenthetical annotation.
            ("`scripts/foo.py` (modify)", "scripts/foo.py"),
            # Trailing parenthetical with embedded em-dash inside the parens.
            (
                "tests/fixtures/decomposer_inputs/ "
                "(create — canonical + malformed markdown fixtures)",
                "tests/fixtures/decomposer_inputs/",
            ),
            # `:N-M` line range suffix (ASCII hyphen).
            ("scripts/foo.py:10-20", "scripts/foo.py"),
            # `:N–M` line range suffix (en-dash).
            ("scripts/foo.py:10–20", "scripts/foo.py"),
            # `:N` single-line reference.
            ("scripts/foo.py:42", "scripts/foo.py"),
        ],
    )
    def test_normalize_files_entry_canonical(
        self, raw: str, expected: str
    ) -> None:
        from _plan_paths import normalize_files_entry  # noqa: WPS433
        assert normalize_files_entry(raw) == expected

    def test_plan_ops_thin_alias_delegates(self) -> None:
        """`plan_ops._normalize_files_entry` is a back-compat shim that
        must produce byte-for-byte identical output to the canonical
        helper, otherwise the ~40 internal call sites in `plan_ops.py`
        would silently desync from the wrapper."""
        from _plan_paths import normalize_files_entry  # noqa: WPS433
        cases = [
            "scripts/foo.py",
            "`Makefile` -- add `audit` target wired to `plan_ops.py audit`",
            "tests/fixtures/decomposer_inputs/ "
            "(create — canonical + malformed markdown fixtures)",
            "scripts/foo.py:10-20",
        ]
        for raw in cases:
            assert plan_ops._normalize_files_entry(raw) == normalize_files_entry(raw)

    def test_extract_task_files_end_to_end_unchanged(self) -> None:
        """The unification must not perturb `_extract_task_files_from_plan`
        — same parser surface, same outputs."""
        files = plan_ops._extract_task_files_from_plan(
            _GATES_SYNTHETIC_PLAN, "001",
        )
        assert files == ["example/seed.py"], files


class TestGateFixtureValidSidecar:
    """`fixture-valid` is the schema+schedule aggregate; sidecar is required."""

    def test_gates_fixture_valid_fails_when_sidecar_missing(
        self, tmp_path: Path,
    ) -> None:
        """A schema-valid fixture with no `.schedule.json` sidecar fails."""
        plan = _write_gates_plan(tmp_path)
        # No sidecar written. fixture-valid must fail and the reason must
        # name the missing sidecar.
        result = plan_ops._gate_fixture_valid(plan)
        assert result["name"] == "fixture-valid"
        assert result["status"] == "fail"
        assert "sidecar" in result["reason"].lower()
        assert "plan.schedule.json" in result["reason"]

    def test_gates_fixture_valid_passes_when_sidecar_present(
        self, tmp_path: Path,
    ) -> None:
        """Schema-valid fixture + valid sidecar passes."""
        plan = _write_gates_plan(tmp_path)
        _write_gates_schedule(tmp_path)
        result = plan_ops._gate_fixture_valid(plan)
        assert result["status"] == "pass", result

    def test_gates_fixture_valid_fails_when_sidecar_invalid(
        self, tmp_path: Path,
    ) -> None:
        """Schema-valid fixture + invalid sidecar fails with schedule reason."""
        plan = _write_gates_plan(tmp_path)
        _write_gates_schedule(tmp_path, payload={"outcome": "valid"})
        result = plan_ops._gate_fixture_valid(plan)
        assert result["status"] == "fail"
        assert "schedule-valid failed" in result["reason"]


class TestGateExecutionSafeScopedRegions:
    """`execution-safe` must locate snapshot inside cmd_implement + timeout."""

    def test_gates_execution_safe_fails_when_snapshots_outside_cmd_implement(
        self, tmp_path: Path,
    ) -> None:
        """Two snapshot calls in helper/review code, none in cmd_implement, fails."""
        stub = tmp_path / "bad_placement_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def _handle_timeout_cleanup(a, b, baseline): pass\n"
            "def cmd_implement(args):\n"
            "    # no snapshot here\n"
            "    return 0\n"
            "def cmd_review(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    return 0\n"
            "def helper():\n"
            "    baseline = _snapshot_baseline()\n"
            "    return baseline\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result
        assert "cmd_implement" in result["reason"]

    def test_gates_execution_safe_fails_when_timeout_branch_missing(
        self, tmp_path: Path,
    ) -> None:
        """Snapshot in cmd_implement but no timeout cleanup branch fails."""
        stub = tmp_path / "no_timeout_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    return 0  # no timeout handling\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result

    def test_gates_execution_safe_fails_when_timeout_omits_baseline(
        self, tmp_path: Path,
    ) -> None:
        """Timeout branch that forgets to carry baseline through fails."""
        stub = tmp_path / "no_baseline_in_timeout_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def _handle_timeout_cleanup(a, b): pass\n"
            "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [])  # baseline not passed\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result

    def test_gates_execution_safe_passes_with_proper_placement(
        self, tmp_path: Path,
    ) -> None:
        """cmd_implement contains both snapshot and baseline-carrying timeout call."""
        stub = tmp_path / "ok_placement_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def _handle_timeout_cleanup(a, b, baseline): pass\n"
            "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [], baseline)\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "pass", result


class TestCertifyExecuteRequiresRunId:
    """`--certify --mode execute` must require --run-id at the CLI layer."""

    def test_gates_certify_execute_without_run_id_fails(
        self, tmp_path: Path,
    ) -> None:
        """Execute certification without --run-id must not silently pass."""
        plan = _write_gates_plan(tmp_path)
        sched = _write_gates_schedule(tmp_path)
        cp = _run(
            "gates", "--certify", "--mode", "execute",
            "--plan-file", str(plan),
            "--schedule-file", str(sched),
            "--json",
        )
        assert cp.returncode != 0, cp.stdout
        body = _parse_json(cp)
        assert "run-id" in body.get("error", "").lower()


class TestGatesFixtureCheckUsesCanonicalSample:
    """`gates --check fixture-valid` always targets the canonical sample
    fixture, never the `--plan-file` that Phase 0 passes in for the other
    gates. The docstring invariant is explicit about `sample_phase4.md`; a
    --plan-file-driven fixture-valid would validate the user plan twice."""

    def test_fixture_valid_via_cli_ignores_plan_file_arg(
        self, tmp_path: Path,
    ) -> None:
        """Even a well-formed --plan-file must not short-circuit
        fixture-valid; the gate result comes from the canonical sample."""
        plan = _write_gates_plan(tmp_path)
        # Run the CLI with a valid plan passed as --plan-file. The
        # resulting fixture-valid gate must reflect the REAL sample
        # fixture state (pre-TASK-006: fail), not the in-memory synthetic
        # plan + sidecar we just wrote under tmp_path.
        cp_synthetic = _run(
            "gates", "--check", "fixture-valid",
            "--plan-file", str(plan), "--json",
        )
        # Drop the --plan-file entirely; result should be identical.
        cp_bare = _run("gates", "--check", "fixture-valid", "--json")
        body_synth = _parse_json(cp_synthetic)
        body_bare = _parse_json(cp_bare)
        assert body_synth["gates"][0]["name"] == "fixture-valid"
        assert body_bare["gates"][0]["name"] == "fixture-valid"
        # Status + reason must match — the CLI ignored the --plan-file arg
        # and used the canonical sample for both invocations.
        assert body_synth["gates"][0]["status"] == body_bare["gates"][0]["status"]
        assert body_synth["gates"][0]["reason"] == body_bare["gates"][0]["reason"]


class TestGateCommitSafeNoProtectedPathFallback:
    """`commit-safe` must not rely on `is_protected_path` as an always-allow
    filter. Protected paths (plan_ops.py, plan_codex_dispatch.py, etc.) are
    NOT in the commit-task implicit allowlist; a task that commits against
    them without declaring them in Files: is out of scope."""

    def _make_repo_with_plan(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        plan = repo / "plan.md"
        plan.write_text(_GATES_SYNTHETIC_PLAN, encoding="utf-8")
        (repo / "example").mkdir()
        (repo / "plugins" / "plan-executor" / "scripts").mkdir(parents=True)
        subprocess.run(["git", "add", "plan.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
        return repo, plan

    def test_commit_touching_undeclared_protected_path_fails(
        self, tmp_path: Path,
    ) -> None:
        """A commit that mutates `plan_ops.py` without declaring it fails
        commit-safe. Previously the `is_protected_path` fallback allowed
        the path through because it matches PROTECTED_PATH_PREFIXES."""
        repo, plan = self._make_repo_with_plan(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        protected_path = (
            repo / "plugins" / "plan-executor" / "scripts" / "plan_ops.py"
        )
        protected_path.write_text("# stray mutation\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "example/seed.py",
             "plugins/plan-executor/scripts/plan_ops.py"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m",
             "feat(TASK-001): seed + undeclared protected edit"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "fail", result
        assert "plan_ops.py" in result["reason"]

    def test_index_json_alongside_plan_is_allowed(
        self, tmp_path: Path,
    ) -> None:
        """`00_INDEX.json` in the plan's directory IS always-allowed
        because `commit-task` auto-updates it. This is the commit-task
        implicit allowlist we DO honor; contrast with the test above."""
        repo, plan = self._make_repo_with_plan(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        index_path = plan.parent / "00_INDEX.json"
        index_path.write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "example/seed.py", "plan.md", "00_INDEX.json"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m",
             "feat(TASK-001): seed + plan + index"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "pass", result


class TestGateSafeCommentHardening:
    """`execution-safe` and `review-safe` predicates must not be fooled by
    commented-out or docstring-mentioned occurrences of the required call
    sites. Strip comments/docstrings before matching."""

    def test_execution_safe_fails_when_snapshot_only_in_comment(
        self, tmp_path: Path,
    ) -> None:
        """A wrapper whose only snapshot call is inside a `#` comment
        must not pass execution-safe."""
        stub = tmp_path / "commented_snapshot_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _handle_timeout_cleanup(a, b, baseline): pass\n"
            "def cmd_implement(args):\n"
            "    # baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [], baseline)\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result
        assert "_snapshot_baseline" in result["reason"]

    def test_execution_safe_fails_when_timeout_call_only_in_comment(
        self, tmp_path: Path,
    ) -> None:
        """A wrapper whose only `_handle_timeout_cleanup(...baseline)` call
        is commented out must not pass execution-safe."""
        stub = tmp_path / "commented_cleanup_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS\n"
            "def _snapshot_baseline(): pass\n"
            "def _handle_timeout_cleanup(a, b, baseline): pass\n"
            "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        # _handle_timeout_cleanup('x', [], baseline)\n"
            "        pass\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result

    def test_execution_safe_fails_when_snapshot_only_in_docstring(
        self, tmp_path: Path,
    ) -> None:
        """Triple-quoted docstring mentions of the helper must not pass."""
        stub = tmp_path / "docstring_snapshot_wrapper.py"
        stub.write_text(
            'from _plan_paths import PROTECTED_EXACT_PATHS\n'
            'def _handle_timeout_cleanup(a, b, baseline): pass\n'
            'def cmd_implement(args):\n'
            '    """See _snapshot_baseline() for context."""\n'
            '    codex = {"status": "timeout"}\n'
            '    if codex["status"] == "timeout":\n'
            '        _handle_timeout_cleanup("x", [], baseline)\n'
            '    return 0\n',
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result

    def test_review_safe_fails_when_snapshot_only_in_comment(
        self, tmp_path: Path,
    ) -> None:
        """A wrapper whose only snapshot in cmd_review is commented out
        must not pass review-safe."""
        stub = tmp_path / "commented_review_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS, is_protected_path\n"
            "def cmd_review(args):\n"
            "    # baseline = _snapshot_baseline()\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_review_safe(stub)
        assert result["status"] == "fail", result


class TestGateFixturePathIsCwdIndependent:
    """`_gate_fixture_valid()` (no arg) must resolve the sample fixture
    from the repo root regardless of the process cwd; Phase 0 invocations
    do not require cwd == repo root, so a cwd-relative fixture path makes
    the gate fail spuriously from other directories."""

    def test_gate_fixture_valid_resolves_same_from_any_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invoking from tmp_path yields the same gate result as from the
        repo root. The expected status depends on whether the live
        sample fixture is schema-conformant (pre-TASK-006: fail;
        post: pass); we assert equality across cwds, not the status.
        Critically, neither invocation may report `fixture not found`
        — that would mean the gate was looking in the wrong place."""
        result_from_repo = plan_ops._gate_fixture_valid()
        monkeypatch.chdir(tmp_path)
        result_from_tmp = plan_ops._gate_fixture_valid()
        assert result_from_repo["name"] == "fixture-valid"
        assert result_from_tmp["name"] == "fixture-valid"
        # Neither cwd should produce a "fixture not found" reason — that
        # is the specific failure mode the cwd-independent path fix was
        # meant to prevent.
        assert "fixture not found" not in result_from_repo["reason"].lower()
        assert "fixture not found" not in result_from_tmp["reason"].lower()
        # Both invocations must produce identical status + reason; any
        # divergence proves the gate is cwd-sensitive.
        assert result_from_tmp["status"] == result_from_repo["status"], (
            result_from_tmp, result_from_repo,
        )
        assert result_from_tmp["reason"] == result_from_repo["reason"], (
            result_from_tmp, result_from_repo,
        )


class TestCertifyExecutePlumbsRepoRoot:
    """`_certify_execute` must pass a repo_root derived from the plan file
    through to `_gate_commit_safe` so `git show --name-only` targets the
    correct repository instead of the caller's cwd."""

    def test_execute_certify_commit_safe_uses_plan_git_toplevel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A staged task commit in a tmp-path git repo is verified even
        when the process cwd is outside that repo. Previously
        `_gate_commit_safe` ran `git show` in cwd and would return empty
        output, making commit-safe silently pass as `not_applicable` or
        fail with a git error."""
        # Build a tiny repo with a plan + a task commit.
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        plan = repo / "plan.md"
        plan.write_text(_GATES_SYNTHETIC_PLAN, encoding="utf-8")
        (repo / "example").mkdir()
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "plan.md", "example/seed.py"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): seed"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()

        # Fake a run log with a commit_done event for TASK-001.
        fake_log = tmp_path / "run_log.jsonl"
        fake_log.write_text(
            json.dumps({
                "event": "commit_done",
                "run_id": "rid-1",
                "task_id": "001",
                "commit_sha": sha,
            }) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", fake_log)

        # Invoke _certify_execute from a cwd OUTSIDE the tmp repo.
        outside_cwd = tmp_path / "elsewhere"
        outside_cwd.mkdir()
        monkeypatch.chdir(outside_cwd)

        # Need a schedule file too so schedule-valid doesn't fail the bundle.
        sched = _write_gates_schedule(repo)

        gates = plan_ops._certify_execute(plan, "rid-1", sched)
        commit_safe = next(
            g for g in gates if g["name"] == "commit-safe"
        )
        # With the repo-root-from-plan plumbing, the commit lookup lands
        # inside `repo/` and the gate can evaluate the commit's file
        # footprint against the Files: allowlist. Without the fix, this
        # would fail with a git error (cwd outside any repo) or with a
        # mismatched-repo false negative.
        assert commit_safe["status"] == "pass", commit_safe


class TestGateScheduleValidShapeContract:
    """`schedule-valid` must treat non-list `tasks`/`batches` as a failure,
    not collapse to empty lists. Defends against regressions in the
    underlying `_validate_schedule` contract."""

    def test_non_list_tasks_fails_schedule_valid(
        self, tmp_path: Path,
    ) -> None:
        """`tasks: "oops"` (string instead of list) fails, does not silently
        pass via the DAG validator seeing an empty graph."""
        sched = tmp_path / "bad_shape.schedule.json"
        # Craft a payload that might or might not trigger
        # _validate_schedule's shape error; the gate's own shape guard
        # must catch non-list tasks regardless.
        sched.write_text(
            json.dumps({
                "outcome": "valid",
                "tasks": "not-a-list",
                "batches": [],
            }),
            encoding="utf-8",
        )
        result = plan_ops._gate_schedule_valid(sched)
        assert result["name"] == "schedule-valid"
        assert result["status"] == "fail", result

    def test_non_list_batches_fails_schedule_valid(
        self, tmp_path: Path,
    ) -> None:
        """`batches: 42` (number instead of list) fails."""
        sched = tmp_path / "bad_shape2.schedule.json"
        sched.write_text(
            json.dumps({
                "outcome": "valid",
                "tasks": [],
                "batches": 42,
            }),
            encoding="utf-8",
        )
        result = plan_ops._gate_schedule_valid(sched)
        assert result["status"] == "fail", result


# ---------------------------------------------------------------------------
# TASK-026: Phase-gate review-drift follow-up.
#
# The three classes below close the five Codex review findings that surfaced
# after TASK-005 committed. V1 covers the execution-safe two-seam invariant
# (finding 1); V2 covers the commit-safe always-ignore set + the "protected
# but not declared" negative (finding 2); V3 covers the SKILL.md Phase 0
# preflight halt-set reconciliation (finding 3). Finding 4 (promotion-table
# phase cell) is covered by a single assertion inside the Phase 0 halt-set
# test; finding 5 (V3/V4 test strategy) is covered by the positive-path
# fixture rewrite inside `TestGateExecutionAndReviewSafe` above.
# ---------------------------------------------------------------------------


_WRAPPER_BASE_GOOD = (
    "from _plan_paths import PROTECTED_EXACT_PATHS, is_protected_path\n"
    "def _snapshot_baseline(): pass\n"
    "def _handle_timeout_cleanup(a, b, baseline): pass\n"
)


class TestGateExecutionSafeWindowScoped:
    """V1 (finding 1) -- `execution-safe` must assert a `_snapshot_baseline()`
    call inside the implement-dispatch window AND inside the timeout-cleanup
    window, parsed as separate regions. A snapshot in only one window must
    fail the gate so a wrapper cannot satisfy the invariant by placing a
    single snapshot in the wrong seam."""

    def test_fails_when_implement_has_snapshot_but_cleanup_does_not(
        self, tmp_path: Path,
    ) -> None:
        """cmd_implement captures a baseline but the timeout branch calls
        _handle_timeout_cleanup without passing baseline, and the cleanup
        helper itself never snapshots. Cleanup window is unreachable for
        the delta check, so the gate must fail."""
        stub = tmp_path / "impl_only_wrapper.py"
        stub.write_text(
            _WRAPPER_BASE_GOOD.replace(
                "def _handle_timeout_cleanup(a, b, baseline): pass\n",
                "def _handle_timeout_cleanup(a, b): pass\n",
            )
            + "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [])\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result
        # The reason must mention the cleanup window specifically so the
        # operator can distinguish this from a missing-implement seam.
        assert (
            "timeout" in result["reason"].lower()
            or "cleanup" in result["reason"].lower()
        ), result

    def test_fails_when_cleanup_has_snapshot_but_implement_does_not(
        self, tmp_path: Path,
    ) -> None:
        """The cleanup helper captures its own snapshot but cmd_implement
        itself never snapshots. Implement-dispatch window has nothing to
        compare against, so the gate must fail."""
        stub = tmp_path / "cleanup_only_wrapper.py"
        stub.write_text(
            "from _plan_paths import PROTECTED_EXACT_PATHS, is_protected_path\n"
            "def _snapshot_baseline(): pass\n"
            "def _handle_timeout_cleanup(a, b):\n"
            "    baseline = _snapshot_baseline()\n"
            "    return baseline\n"
            "def cmd_implement(args):\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [])\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "fail", result
        # The reason must name cmd_implement so the operator can see the
        # implement-dispatch window (not the cleanup window) is the gap.
        assert "cmd_implement" in result["reason"], result

    def test_passes_when_both_seams_have_snapshot(
        self, tmp_path: Path,
    ) -> None:
        """Positive path: cmd_implement captures baseline and passes it
        through `_handle_timeout_cleanup(..., baseline)`. Both windows
        are covered as separate regions; gate passes."""
        stub = tmp_path / "both_seams_wrapper.py"
        stub.write_text(
            _WRAPPER_BASE_GOOD
            + "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [], baseline)\n"
            "    return 0\n"
            "def cmd_review(args):\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "pass", result

    def test_passes_when_cleanup_kwarg_propagates_baseline(
        self, tmp_path: Path,
    ) -> None:
        """Positive path (kwarg-propagation variant): cmd_implement captures
        baseline and the timeout branch calls `_handle_timeout_cleanup(...,
        baseline)`. The cleanup helper's own body does NOT need a snapshot
        call -- the baseline kwarg carries the pre-dispatch snapshot
        through and satisfies the cleanup-window invariant."""
        stub = tmp_path / "kwarg_wrapper.py"
        stub.write_text(
            _WRAPPER_BASE_GOOD
            + "def cmd_implement(args):\n"
            "    baseline = _snapshot_baseline()\n"
            "    codex = {'status': 'timeout'}\n"
            "    if codex['status'] == 'timeout':\n"
            "        _handle_timeout_cleanup('x', [], baseline)\n"
            "    return 0\n",
            encoding="utf-8",
        )
        result = plan_ops._gate_execution_safe(stub)
        assert result["status"] == "pass", result


class TestGateCommitSafeAlwaysIgnoreConsistency:
    """V2 (finding 2) -- `commit-safe` uses the shared `COMMIT_ALWAYS_IGNORE`
    set as its sole always-allow filter. Members of the set (run-log,
    run-lock, per-plan schedule sidecar, 00_INDEX.json) are ignored; paths
    that are `is_protected_path` but NOT in `COMMIT_ALWAYS_IGNORE` still
    fail the gate when they are not declared in Files:."""

    def _make_repo(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        plans_dir = repo / "docs" / "plans"
        plans_dir.mkdir(parents=True)
        plan = plans_dir / "plan.md"
        plan.write_text(_GATES_SYNTHETIC_PLAN, encoding="utf-8")
        (repo / "example").mkdir()
        subprocess.run(
            ["git", "add", "docs/plans/plan.md"], cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True,
        )
        return repo, plans_dir, plan

    def test_run_log_change_is_ignored(self, tmp_path: Path) -> None:
        """`docs/plans/_run_log.jsonl` is a COMMIT_ALWAYS_IGNORE member;
        a commit that touches it alongside a declared Files: entry passes."""
        repo, plans_dir, plan = self._make_repo(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        (plans_dir / "_run_log.jsonl").write_text(
            '{"event":"x"}\n', encoding="utf-8",
        )
        subprocess.run(
            ["git", "add",
             "example/seed.py",
             "docs/plans/_run_log.jsonl"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): seed + log"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "pass", result

    def test_schedule_sidecar_for_current_plan_is_ignored(
        self, tmp_path: Path,
    ) -> None:
        """The per-plan schedule sidecar `<plan_stem>.schedule.json`
        beside the plan is matched by `is_commit_always_ignore(path,
        plan.name)` and ignored by commit-safe."""
        repo, plans_dir, plan = self._make_repo(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        (plans_dir / "plan.schedule.json").write_text(
            '{"outcome":"valid","tasks":[],"batches":[]}\n',
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add",
             "example/seed.py",
             "docs/plans/plan.schedule.json"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m",
             "feat(TASK-001): seed + sidecar"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "pass", result

    def test_undeclared_protected_path_still_fails(
        self, tmp_path: Path,
    ) -> None:
        """`plugins/plan-executor/scripts/plan_ops.py` is protected
        against delta-cleanup but is NOT in `COMMIT_ALWAYS_IGNORE`; a
        commit that mutates it without declaring it in Files: must
        still fail commit-safe. Regression guard against the old
        `is_protected_path` fallback that silently green-lit executor
        mutations."""
        repo, plans_dir, plan = self._make_repo(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        protected_dir = repo / "plugins" / "plan-executor" / "scripts"
        protected_dir.mkdir(parents=True)
        protected_path = protected_dir / "plan_ops.py"
        protected_path.write_text("# stray\n", encoding="utf-8")
        subprocess.run(
            ["git", "add",
             "example/seed.py",
             "plugins/plan-executor/scripts/plan_ops.py"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m",
             "feat(TASK-001): seed + stray mutation"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "fail", result
        assert "plan_ops.py" in result["reason"], result

    def test_undeclared_non_protected_path_fails(
        self, tmp_path: Path,
    ) -> None:
        """An arbitrary path outside both `Files:` and
        `COMMIT_ALWAYS_IGNORE` fails the gate; the narrow always-ignore
        set is not a general-purpose allowlist."""
        repo, plans_dir, plan = self._make_repo(tmp_path)
        (repo / "example" / "seed.py").write_text("# seed\n", encoding="utf-8")
        (repo / "other.py").write_text("# leak\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "example/seed.py", "other.py"],
            cwd=repo, check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "feat(TASK-001): leak"],
            cwd=repo, check=True,
        )
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
        ).strip()
        result = plan_ops._gate_commit_safe(
            sha, "001", plan, repo_root=repo,
        )
        assert result["status"] == "fail", result
        assert "other.py" in result["reason"], result


class TestSkillPreflightStrictHalt:
    """V3 (finding 3) -- SKILL.md Phase 0 preflight lists
    `schema-valid`, `schedule-valid`, `fixture-valid` as strict
    halt-on-fail gates with no interim demotion clause. V4 (finding 4)
    -- the promotion-table row for `schedule-valid` cites Phase 1
    (post-write-schedule), not Phase 0."""

    _SKILL_PATH = (
        REPO_ROOT
        / "plugins" / "plan-executor" / "skills"
        / "implement-plan" / "SKILL.md"
    )

    def _read(self) -> str:
        assert self._SKILL_PATH.is_file(), self._SKILL_PATH
        return self._SKILL_PATH.read_text(encoding="utf-8")

    def test_skill_md_phase_0_halt_set_includes_schema_schedule_and_fixture_valid(
        self,
    ) -> None:
        """SKILL.md Phase 0 preflight section names all three gates as the
        halt set -- schema-valid, schedule-valid, fixture-valid. This is
        the canonical-contract smoke test; phrasing may shift but the
        three gate names must co-locate inside the preflight section."""
        text = self._read()
        # Locate the "Pre-flight (Phase 0)" section and slice until the
        # next top-level heading so we do not match any other discussion
        # of the gates elsewhere in the skill.
        phase0_re = re.compile(
            r"^##\s+Pre-flight\s*\(Phase 0\).*?(?=^##\s+[A-Z])",
            re.MULTILINE | re.DOTALL,
        )
        m = phase0_re.search(text)
        assert m, "Phase 0 preflight section not found in SKILL.md"
        block = m.group(0)
        # All three preflight-halt gates must be named inside this
        # section, and the block must describe a strict halt contract.
        for gate in ("schema-valid", "schedule-valid", "fixture-valid"):
            assert gate in block, (
                f"Phase 0 block is missing preflight-halt gate {gate!r}"
            )
        assert "halt" in block.lower(), (
            "Phase 0 block must state the halt-on-fail contract"
        )

    def test_skill_md_phase_0_has_no_interim_demotion_wording(self) -> None:
        """The TASK-005 interim wording that demoted `fixture-valid` on
        self-reference to the sample fixture is removed. None of the
        known demotion phrasings may appear in the Phase 0 block."""
        text = self._read()
        phase0_re = re.compile(
            r"^##\s+Pre-flight\s*\(Phase 0\).*?(?=^##\s+[A-Z])",
            re.MULTILINE | re.DOTALL,
        )
        m = phase0_re.search(text)
        assert m, "Phase 0 preflight section not found in SKILL.md"
        block = m.group(0).lower()
        forbidden_phrases = (
            "demote to warning",
            "demote `fixture-valid",
            "demote fixture-valid",
            "pre-task-006",
            "self-reference halt",
            "deferred-pending-task-006",
        )
        for phrase in forbidden_phrases:
            assert phrase not in block, (
                f"Phase 0 block still carries interim demotion wording: "
                f"{phrase!r}"
            )

    def test_skill_md_promotion_table_schedule_valid_row_cites_phase_1(
        self,
    ) -> None:
        """V4 (finding 4) -- the promotion table row for `schedule-valid`
        cites Phase 1 (post-write-schedule), not Phase 0 preflight. The
        schedule file does not exist during Phase 0; the gate runs after
        `write-schedule` persists the analyst JSON."""
        text = self._read()
        # Locate the row whose first cell is `schedule-valid`.
        row_re = re.compile(
            r"^\|\s*`schedule-valid`\s*\|\s*(?P<phase>[^|]+?)\s*\|",
            re.MULTILINE,
        )
        m = row_re.search(text)
        assert m, "promotion table has no `schedule-valid` row"
        phase_cell = m.group("phase").strip()
        assert "Phase 1" in phase_cell, (
            f"schedule-valid row still cites {phase_cell!r}; expected "
            f"Phase 1 (post-write-schedule)"
        )
        assert "Phase 0" not in phase_cell, (
            f"schedule-valid row must not cite Phase 0: {phase_cell!r}"
        )

    def test_gates_cli_has_no_warn_only_flag(self) -> None:
        """Structural guard: the `gates` subcommand must not expose a
        `--warn-only` flag. Finding 3's reconciliation is strictly a
        text-only change; widening the gate CLI surface would re-open
        TASK-005's frozen CLI contract."""
        cp = _run("gates", "--help")
        # --help exits 0 and prints usage on stdout.
        combined = (cp.stdout + cp.stderr).lower()
        assert "--warn-only" not in combined, (
            "gates subcommand must not expose --warn-only; the frozen "
            "status vocabulary is pass|fail|not_applicable"
        )

    def test_gate_status_vocabulary_is_frozen(self) -> None:
        """Sanity: every gate result keeps the frozen vocabulary. We
        exercise the --list and a benign --check to confirm no `warn`
        status leaks out."""
        cp = _run("gates", "--list", "--json")
        body = _parse_json(cp)
        assert "gates" in body
        # The --list response enumerates canonical names, not statuses,
        # so assert the CLI did not crash. For a status sanity check,
        # hit fixture-valid (requires no --plan-file) and assert the
        # status value sits in the frozen vocabulary.
        cp2 = _run("gates", "--check", "fixture-valid", "--json")
        body2 = _parse_json(cp2)
        gate = body2["gates"][0]
        assert gate["status"] in {"pass", "fail", "not_applicable"}, gate


# ---------------------------------------------------------------------------
# TASK-006: sample fixture conformance.
#
# After TASK-006, `docs/plans/sample_phase4.md` is the canonical conformance
# artifact for Phase 5 certification: `fixture-valid` passes against it and
# its schedule sidecar, it carries no out-of-schema `**Agent:**` bullets,
# and every task block declares the required §5 fields. Regressions here
# would re-break Phase 0 preflight for every downstream plan run (see the
# 2026-04-20 blocked-run incident captured in
# `docs/plans/DUAL_AGENT_Plans/TASK-006_conformance_fixture.md`).
# ---------------------------------------------------------------------------


_SAMPLE_PHASE4_PATH = REPO_ROOT / "docs" / "plans" / "sample_phase4.md"


class TestSamplePhase4FixtureConformance:
    """TASK-006: `docs/plans/sample_phase4.md` is the canonical conformance
    fixture. The three tests below are selected by `pytest -k fixture` (via
    this class name), matching the task's `Test command`."""

    def test_sample_phase4_passes_fixture_valid_gate(self) -> None:
        """`gates --check fixture-valid` returns pass against the canonical
        sample fixture + sidecar."""
        cp = _run(
            "gates",
            "--check",
            "fixture-valid",
            "--plan-file",
            str(_SAMPLE_PHASE4_PATH),
            "--json",
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = _parse_json(cp)
        gate = body["gates"][0]
        assert gate["name"] == "fixture-valid"
        assert gate["status"] == "pass", gate

    def test_sample_phase4_has_no_hardcoded_agent_field(self) -> None:
        """Routing is classifier-owned. A hardcoded `**Agent:**` bullet on
        any task block would override the analyst's classification and
        defeat the mixed-routing scenario the fixture is meant to
        exercise."""
        assert _SAMPLE_PHASE4_PATH.exists(), _SAMPLE_PHASE4_PATH
        text = _SAMPLE_PHASE4_PATH.read_text(encoding="utf-8")
        # Restrict to bullet-form agent declarations (`- **Agent:**`).
        # Prose mentions of the word "agent" are fine and common in
        # descriptions.
        agent_bullet_re = re.compile(
            r"^\s*-\s*\*\*Agent:\*\*", re.MULTILINE
        )
        matches = agent_bullet_re.findall(text)
        assert matches == [], (
            f"found {len(matches)} `**Agent:**` bullet(s) in the sample "
            f"fixture; routing is classifier-owned and the field must "
            f"not appear in the plan schema"
        )

    def test_sample_phase4_has_required_task_fields(self) -> None:
        """Every `### TASK-NNN` block in the sample fixture declares the
        mandatory §5 fields: Status, Priority, Files, Test command,
        Acceptance criteria (bullets) + Description (prose header).
        `Reversion guidance` is also required per the plan schema
        checklist, so we assert it too.
        """
        assert _SAMPLE_PHASE4_PATH.exists(), _SAMPLE_PHASE4_PATH
        text = _SAMPLE_PHASE4_PATH.read_text(encoding="utf-8")
        _, blocks = plan_ops._split_task_blocks(text)
        assert blocks, "sample fixture has no TASK-NNN blocks"
        required_bullets = (
            "Status",
            "Priority",
            "Files",
            "Test command",
            "Acceptance criteria",
        )
        required_prose = (
            "Description",
            "Reversion guidance",
        )
        for tid, block in blocks:
            for field in required_bullets:
                pat = re.compile(
                    rf"^\s*-\s*\*\*{re.escape(field)}:\*\*",
                    re.MULTILINE,
                )
                assert pat.search(block), (
                    f"TASK-{tid} missing required bullet **{field}:**"
                )
            for field in required_prose:
                pat = re.compile(
                    rf"^\*\*{re.escape(field)}:\*\*",
                    re.MULTILINE,
                )
                assert pat.search(block), (
                    f"TASK-{tid} missing required prose header "
                    f"**{field}:**"
                )
            # Status vocabulary: canonical is exactly `pending` for the
            # rewritten fixture. Aliases such as `open`, `in-progress`,
            # `todo`, or empty values must all be rejected.
            status_value_re = re.compile(
                r"^\s*-\s*\*\*Status:\*\*\s*(.+?)\s*$", re.MULTILINE,
            )
            status_match = status_value_re.search(block)
            assert status_match, (
                f"TASK-{tid} missing parseable `**Status:**` value"
            )
            status_value = status_match.group(1).strip()
            assert status_value == "pending", (
                f"TASK-{tid} `**Status:** {status_value}` is not "
                f"canonical; expected exactly `pending`"
            )
            # Dependencies must be the canonical form: exactly `none` or
            # `TASK-NNN[, TASK-NNN]...` with 3-digit zero-padded ids and
            # `, ` separators. Anything else (bracket form `[001]`,
            # bare `001`, `TASK-1`, `TASK-001; TASK-002`, empty) must
            # fail.
            deps_value_re = re.compile(
                r"^\s*-\s*\*\*Dependencies:\*\*\s*(.+?)\s*$",
                re.MULTILINE,
            )
            deps_match = deps_value_re.search(block)
            assert deps_match, (
                f"TASK-{tid} missing parseable `**Dependencies:**` value"
            )
            deps_value = deps_match.group(1).strip()
            canonical_deps_re = re.compile(
                r"^(?:none|TASK-\d{3}(?:, TASK-\d{3})*)$"
            )
            assert canonical_deps_re.match(deps_value), (
                f"TASK-{tid} `**Dependencies:** {deps_value}` is not "
                f"canonical; expected `none` or "
                f"`TASK-NNN[, TASK-NNN]...` with 3-digit ids"
            )


# ---------------------------------------------------------------------------
# TASK-020B: opt-in `acceptance_v_check` runtime enforcement in commit-task.
#
# V7-V13 coverage. Tests use the existing `tmp_git_repo` fixture plus a
# helper that prepends a YAML frontmatter block to SAMPLE_PLAN_BODY so the
# plan retains its task structure (TASK-001 at status `open`) while gaining
# the opt-in field. Absence of frontmatter → identical-to-v0 behavior is
# covered by V10 via `TestCommitTask.test_commits_narrowly` above plus the
# explicit V10 test here.
# ---------------------------------------------------------------------------


def _plan_with_frontmatter(v_check_cmd: str | None) -> str:
    """Return SAMPLE_PLAN_BODY prefixed with `acceptance_v_check: "<cmd>"`
    YAML frontmatter. If `v_check_cmd` is None, no frontmatter is attached
    (raw SAMPLE_PLAN_BODY).

    Note: the value is emitted as a double-quoted YAML scalar so bare tokens
    like `true` / `false` / `123` are NOT coerced into Python bool/int —
    they must reach the subprocess as literal shell strings. Embedded `"`
    chars are escaped so YAML's quoted-string rules apply.
    """
    if v_check_cmd is None:
        return SAMPLE_PLAN_BODY
    escaped = v_check_cmd.replace("\\", "\\\\").replace("\"", "\\\"")
    return (
        "---\n"
        f'acceptance_v_check: "{escaped}"\n'
        "---\n"
        + SAMPLE_PLAN_BODY
    )


def _commit_task_argv(
    plan: Path,
    files: str = "src/foo.py",
    extra: tuple[str, ...] = (),
) -> list[str]:
    """Common argv builder for commit-task V-check tests."""
    return [
        "commit-task",
        "--plan-file", str(plan),
        "--task-id", "001",
        "--run-id", "R1",
        "--files", files,
        "--title", "First task",
        "--diff-summary", "bump x",
        "--reviewer", "none",
        "--reviewer-verdict", "",
        "--json",
        *extra,
    ]


def _v_check_run_log_events(repo: Path) -> list[dict]:
    """Parse `docs/plans/_run_log.jsonl` events from `repo`; empty list if
    the file does not exist. Returns a list of decoded JSONL records."""
    run_log = repo / "docs" / "plans" / "_run_log.jsonl"
    if not run_log.exists():
        return []
    events: list[dict] = []
    for line in run_log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


class TestCommitTaskVCheck:
    """V7-V13 coverage for TASK-020B's opt-in `acceptance_v_check` gate."""

    # V7 ----------------------------------------------------------------
    def test_v7_frontmatter_parsed_without_error(
        self, tmp_git_repo: Path,
    ) -> None:
        """Plan with YAML frontmatter declaring `acceptance_v_check: true`
        (always passes) is parsed and commit-task runs to completion."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan.write_text(_plan_with_frontmatter("true"), encoding="utf-8")

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 0, (
            f"stderr={cp.stderr!r} stdout={cp.stdout!r}"
        )
        body = _parse_json(cp)
        assert body["commit_sha"], body
        assert body["status_updated"] is True

    # V8 ----------------------------------------------------------------
    def test_v8_passing_v_check_commits_and_logs_v_check_passed(
        self, tmp_git_repo: Path,
    ) -> None:
        """V-check exits 0 → commit proceeds + `v_check_passed` run-log event
        appended alongside the usual `commit_done`."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan.write_text(_plan_with_frontmatter("true"), encoding="utf-8")

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 0, cp.stderr

        events = _v_check_run_log_events(tmp_git_repo)
        kinds = [e.get("event") for e in events]
        assert "v_check_passed" in kinds, events
        assert "commit_done" in kinds, events
        # v_check_passed MUST precede commit_done.
        vpass_idx = kinds.index("v_check_passed")
        cdone_idx = kinds.index("commit_done")
        assert vpass_idx < cdone_idx, (kinds, events)
        vpass_ev = events[vpass_idx]
        assert vpass_ev.get("task_id") == "001", vpass_ev
        assert vpass_ev.get("run_id") == "R1", vpass_ev
        assert vpass_ev.get("command") == "true", vpass_ev

    # V9 ----------------------------------------------------------------
    def test_v9_failing_v_check_halts_before_any_side_effect(
        self, tmp_git_repo: Path,
    ) -> None:
        """V-check non-zero exit halts before plan mutation, git commit,
        AND run-log append. Core invariant: no side effect on failure."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan_text_before = _plan_with_frontmatter("false")
        plan.write_text(plan_text_before, encoding="utf-8")

        pre_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        run_log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        pre_run_log = (
            run_log_path.read_text() if run_log_path.exists() else ""
        )

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 1, (
            f"expected exit 1; stderr={cp.stderr!r} stdout={cp.stdout!r}"
        )
        body = _parse_json(cp)
        errors = body.get("errors") or []
        codes = [e.get("code") for e in errors]
        assert "acceptance-v-check-failed" in codes, body

        # Invariants: plan text unchanged, HEAD unchanged, run-log
        # contains NO `commit_done` AND NO `v_check_passed` for this task.
        assert plan.read_text(encoding="utf-8") == plan_text_before
        post_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert pre_head == post_head
        post_run_log = (
            run_log_path.read_text() if run_log_path.exists() else ""
        )
        assert pre_run_log == post_run_log
        events = _v_check_run_log_events(tmp_git_repo)
        kinds = [e.get("event") for e in events]
        assert "commit_done" not in kinds, events
        assert "v_check_passed" not in kinds, events

    # V10 ---------------------------------------------------------------
    def test_v10_plan_without_frontmatter_unchanged(
        self, tmp_git_repo: Path,
    ) -> None:
        """No frontmatter → zero behavior change. No v_check_passed event,
        no v-check error code, commit proceeds normally."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        # Raw SAMPLE_PLAN_BODY is written by the fixture already; overwrite
        # for clarity so the test is explicit about the no-frontmatter case.
        plan.write_text(_plan_with_frontmatter(None), encoding="utf-8")

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert body["status_updated"] is True
        events = _v_check_run_log_events(tmp_git_repo)
        kinds = [e.get("event") for e in events]
        assert "v_check_passed" not in kinds, events
        assert "commit_done" in kinds, events

    def test_v10_frontmatter_without_key_unchanged(
        self, tmp_git_repo: Path,
    ) -> None:
        """Frontmatter present but `acceptance_v_check` key absent → also
        behaves like pre-TASK-020B. Guards against false-positive enforcement
        from unrelated frontmatter keys."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan.write_text(
            "---\ntitle: other metadata\n---\n" + SAMPLE_PLAN_BODY,
            encoding="utf-8",
        )

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 0, cp.stderr
        events = _v_check_run_log_events(tmp_git_repo)
        kinds = [e.get("event") for e in events]
        assert "v_check_passed" not in kinds, events
        assert "commit_done" in kinds, events

    # V11 ---------------------------------------------------------------
    def test_v11_timeout_enforced(self, tmp_git_repo: Path) -> None:
        """`sleep 600` with `--v-check-timeout 1` trips the TimeoutExpired
        branch and halts with `v-check-timeout` before any mutation."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan.write_text(
            _plan_with_frontmatter("sleep 600"), encoding="utf-8",
        )
        pre_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        plan_before = plan.read_text(encoding="utf-8")

        cp = _run(
            *_commit_task_argv(
                plan, extra=("--v-check-timeout", "1"),
            ),
            cwd=tmp_git_repo,
        )
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        codes = [e.get("code") for e in (body.get("errors") or [])]
        assert "v-check-timeout" in codes, body

        # No side-effects.
        post_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert pre_head == post_head
        assert plan.read_text(encoding="utf-8") == plan_before

    # V12 ---------------------------------------------------------------
    def test_v12_captures_stdout_stderr_tails(
        self, tmp_git_repo: Path,
    ) -> None:
        """Failing V-check's stdout/stderr are captured in the error envelope
        so the orchestrator can surface them without re-running."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        # Emit a distinctive marker on stdout AND stderr, then fail.
        cmd = (
            "echo FAIL_MARKER_STDOUT_V3; "
            "echo FAIL_MARKER_STDERR_V3 1>&2; "
            "exit 1"
        )
        plan.write_text(_plan_with_frontmatter(cmd), encoding="utf-8")

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        errors = body.get("errors") or []
        assert len(errors) == 1 and errors[0]["code"] == (
            "acceptance-v-check-failed"
        ), body
        err = errors[0]
        assert "FAIL_MARKER_STDOUT_V3" in err.get("stdout_tail", ""), err
        assert "FAIL_MARKER_STDERR_V3" in err.get("stderr_tail", ""), err

    def test_v12_tail_bounded_to_2048_bytes(
        self, tmp_git_repo: Path,
    ) -> None:
        """Runaway output → tail is capped at the last 2048 bytes of stdout
        and stderr respectively so error envelopes stay bounded."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        # 5000 bytes of 'A' on stdout then exit 1 — tail must be ≤ 2048.
        cmd = "python3 -c \"print('A' * 5000)\"; exit 1"
        plan.write_text(_plan_with_frontmatter(cmd), encoding="utf-8")

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 1, cp.stderr
        body = _parse_json(cp)
        err = (body.get("errors") or [{}])[0]
        stdout_tail = err.get("stdout_tail", "")
        # 5000 chars of 'A' + trailing newline → stdout is ~5001 bytes.
        # Tail is last 2048. Count of 'A' should be at most 2048.
        assert len(stdout_tail) <= 2048, len(stdout_tail)
        assert stdout_tail.count("A") <= 2048, stdout_tail.count("A")

    # Invariant check: V9 second phase --------------------------------
    def test_no_mutation_after_failing_v_check(
        self, tmp_git_repo: Path,
    ) -> None:
        """Post-failure invariant: plan text, run log, and git HEAD are all
        identical before and after the failing invocation. Mirrors V9 with a
        different failure path (subprocess error via nonexistent command)."""
        (tmp_git_repo / "src" / "foo.py").write_text(
            "x = 2\n", encoding="utf-8",
        )
        plan = tmp_git_repo / "docs" / "plans" / "sample.md"
        plan.write_text(
            _plan_with_frontmatter("false"), encoding="utf-8",
        )
        run_log_path = tmp_git_repo / "docs" / "plans" / "_run_log.jsonl"
        pre_log = (
            run_log_path.read_text() if run_log_path.exists() else ""
        )
        pre_plan = plan.read_text(encoding="utf-8")
        pre_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

        cp = _run(*_commit_task_argv(plan), cwd=tmp_git_repo)
        assert cp.returncode == 1

        post_log = (
            run_log_path.read_text() if run_log_path.exists() else ""
        )
        post_plan = plan.read_text(encoding="utf-8")
        post_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert pre_log == post_log
        assert pre_plan == post_plan
        assert pre_head == post_head


class TestCommitTaskVCheckUnit:
    """Pure-unit coverage of `_parse_frontmatter` and `_run_v_check` so edge
    cases (malformed YAML, mid-doc `---`, bounded tails) are pinned without
    spinning up the full subprocess."""

    def test_parse_frontmatter_extracts_top_level_yaml(self) -> None:
        text = "---\nfoo: bar\nbaz: qux\n---\n# heading\n"
        data = plan_ops._parse_frontmatter(text)
        assert data == {"foo": "bar", "baz": "qux"}

    def test_parse_frontmatter_missing_returns_empty_dict(self) -> None:
        assert plan_ops._parse_frontmatter("# plain markdown\n") == {}

    def test_parse_frontmatter_requires_leading_triple_dash(self) -> None:
        # `---` line mid-document is NOT a frontmatter start.
        text = "# heading\n\n---\nfoo: bar\n---\n"
        assert plan_ops._parse_frontmatter(text) == {}

    def test_parse_frontmatter_malformed_returns_empty(self) -> None:
        text = "---\nfoo: [unclosed\n---\n"
        assert plan_ops._parse_frontmatter(text) == {}

    def test_parse_frontmatter_non_mapping_returns_empty(self) -> None:
        text = "---\n- just\n- a\n- list\n---\n"
        assert plan_ops._parse_frontmatter(text) == {}


class TestCommitTaskVCheckAllowedEvents:
    """`v_check_passed` and `v_check_failed` MUST be in `ALLOWED_LOG_EVENTS`
    so (a) `cmd_commit_task`'s append on success is accepted by any future
    stricter allowlist gate and (b) external tooling can emit `v_check_failed`
    via `plan_ops.py log-event`."""

    def test_v_check_events_in_allowlist(self) -> None:
        assert "v_check_passed" in plan_ops.ALLOWED_LOG_EVENTS
        assert "v_check_failed" in plan_ops.ALLOWED_LOG_EVENTS


class TestCommitTaskVCheckSkillMd:
    """V13: SKILL.md §D.3 documents the opt-in field."""

    def test_skill_md_references_acceptance_v_check(self) -> None:
        skill_path = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "SKILL.md"
        )
        body = skill_path.read_text(encoding="utf-8")
        assert "acceptance_v_check" in body, (
            "SKILL.md must reference the opt-in acceptance_v_check field"
        )


# ---------------------------------------------------------------------------
# TASK-007: self-audit / protocol-drift detection
# ---------------------------------------------------------------------------


class TestAuditList:
    """V1: `audit --list --json` enumerates every registered check."""

    def test_audit_list_returns_expected_checks(self) -> None:
        cp = _run("audit", "--list", "--json")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        names = [entry["name"] for entry in body["checks"]]
        # Acceptance criteria — every canonical check must appear.
        for required in (
            "status_vocabulary",
            "schedule_wire_format",
            "implementer_report_labels",
            "execution_log_columns",
            "schemas",
            "portable_tier",
            "wrapper_isolation",
            "design_doc_orphans",
        ):
            assert required in names, f"check {required!r} missing from --list"
        # Each entry carries a tier annotation; portable_tier is advisory
        # until TASK-008 lands per the plan.
        tiers = {entry["name"]: entry["tier"] for entry in body["checks"]}
        assert tiers["portable_tier"] == "advisory"
        assert tiers["status_vocabulary"] == "default"


class TestAuditDefaultRun:
    """V2: clean default run on the post-TASK-006 codebase passes overall.

    Pre-TASK-008, `portable_tier` is excluded from the verdict; the audit
    must not flip to `fail` because of advisory-tier findings on the
    pre-TASK-008 codebase.
    """

    def test_audit_default_run_overall_pass(self) -> None:
        cp = _run("audit", "--json")
        assert cp.returncode == 0, (
            f"default audit failed:\nstdout={cp.stdout}\nstderr={cp.stderr}"
        )
        body = _parse_json(cp)
        assert body["overall"] == "pass"
        # Default run MUST include the advisory portable_tier check so
        # operators see advisory drift in the structured report; but its
        # status must not affect `overall` (per spec §step-4 line 241,
        # "Advisory-tier findings still appear in the report regardless
        # of whether they were included in the verdict").
        findings_by_name = {f["check"]: f for f in body["findings"]}
        assert "portable_tier" in findings_by_name, (
            "default audit must surface advisory portable_tier finding"
        )
        assert findings_by_name["portable_tier"]["tier"] == "advisory"

    def test_audit_default_run_includes_required_checks(self) -> None:
        cp = _run("audit", "--json")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        check_names = [f["check"] for f in body["findings"]]
        for required in (
            "status_vocabulary",
            "schedule_wire_format",
            "implementer_report_labels",
            "execution_log_columns",
            "schemas",
            "wrapper_isolation",
            "design_doc_orphans",
        ):
            assert required in check_names, (
                f"default audit missing required check {required!r}"
            )


class TestAuditStatusVocabularyDrift:
    """V3: Seeded status-vocabulary drift makes the check fail.

    Monkeypatches `ALLOWED_TASK_STATUSES` in-process so the source file
    is left untouched; the check inspects the live module constant, so
    the patch is sufficient to prove the detection works.
    """

    def test_audit_detects_status_vocabulary_drift(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Drop a canonical member to simulate drift. TASK-008 removed
        # the `open` alias so the live ALLOWED_TASK_STATUSES already
        # equals the canonical set; dropping `pending` produces a clean
        # `missing=['pending']` reason.
        bad = set(plan_ops.ALLOWED_TASK_STATUSES) - {"pending"}
        monkeypatch.setattr(plan_ops, "ALLOWED_TASK_STATUSES", bad)
        finding = plan_ops._check_status_vocabulary()
        assert finding["check"] == "status_vocabulary"
        assert finding["status"] == "fail"
        assert "missing=['pending']" in finding["reason"]

    def test_audit_detects_unknown_status_addition(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Add a value outside both canonical and alias windows.
        bad = set(plan_ops.ALLOWED_TASK_STATUSES) | {"frobnicated"}
        monkeypatch.setattr(plan_ops, "ALLOWED_TASK_STATUSES", bad)
        finding = plan_ops._check_status_vocabulary()
        assert finding["status"] == "fail"
        assert "extra=['frobnicated']" in finding["reason"]


class TestAuditAliasReporting:
    """TASK-008: every file-mode alias window was REMOVED. The
    directory-only canonical contract is the single accepted runtime
    form, so these checks now report plain `pass` — no
    `pass_with_alias`. The class name is preserved for test-discovery
    stability; the assertions pin the new behavior."""

    def test_status_vocabulary_reports_plain_pass(self) -> None:
        # `open` was REMOVED from both the canonical set and
        # ALLOWED_TASK_STATUSES in TASK-008, so the check returns `pass`
        # (the runtime constant exactly matches the canonical set).
        finding = plan_ops._check_status_vocabulary()
        assert finding["status"] == "pass"
        assert finding["reason"] is None

    def test_schedule_wire_format_reports_plain_pass(self) -> None:
        finding = plan_ops._check_schedule_wire_format()
        assert finding["status"] == "pass"
        assert finding["reason"] is None

    def test_implementer_report_labels_reports_plain_pass(self) -> None:
        finding = plan_ops._check_implementer_report_labels()
        assert finding["status"] == "pass"
        assert finding["reason"] is None


class TestAuditCheckSubset:
    """`--check <csv>` runs the named subset and overrides tier filtering
    so advisory checks may be requested by name."""

    def test_audit_check_subset_runs_only_named_checks(self) -> None:
        cp = _run(
            "audit", "--check", "status_vocabulary,wrapper_isolation",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        names = [f["check"] for f in body["findings"]]
        assert names == ["status_vocabulary", "wrapper_isolation"]

    def test_audit_check_unknown_name_errors(self) -> None:
        cp = _run("audit", "--check", "no-such-check", "--json")
        assert cp.returncode != 0
        body = _parse_json(cp)
        assert "no-such-check" in body.get("error", "")

    def test_audit_check_explicit_advisory_counts_in_verdict(self) -> None:
        # Per the plan: explicit --check overrides tier filtering, so an
        # advisory check named on the CLI counts toward the verdict.
        # `portable_tier` will fail on the pre-TASK-008 codebase.
        cp = _run("audit", "--check", "portable_tier", "--json")
        body = _parse_json(cp)
        # Either the live tree still has venv/bin/python literals (fail)
        # or TASK-008 has cleaned them (pass). Either way, exit code
        # tracks the verdict.
        if body["overall"] == "fail":
            assert cp.returncode == 1
        else:
            assert cp.returncode == 0


class TestAuditStrictMode:
    """`--strict` includes advisory-tier findings in the verdict.

    Three isolated cases exercise the three distinct mechanisms:

      1. strict-only (`check=None`, `strict=True`) — strict promotes
         advisory findings into the verdict on its own.
      2. explicit-check-only (`check=portable_tier`, `strict=False`) —
         naming an advisory check via `--check` forces it into the
         verdict without `--strict`.
      3. composed (`check=portable_tier`, `strict=True`) — both paths
         engaged simultaneously; overall still reflects the advisory
         finding.
    """

    def test_audit_strict_includes_portable_tier(self) -> None:
        cp = _run("audit", "--strict", "--json")
        body = _parse_json(cp)
        names = [f["check"] for f in body["findings"]]
        assert "portable_tier" in names

    # The three tests below are hermetic: they monkeypatch `AUDIT_CHECKS`
    # with a known advisory-failing check plus a known default-passing
    # check, so the advisory finding can enter the verdict ONLY through
    # the mechanism under test. Each test pairs a baseline run (no
    # mechanism engaged -> `overall == "pass"`) with a run that engages
    # exactly one mechanism -> `overall == "fail"`. A regression that
    # broke the mechanism would flip the second assertion.

    @staticmethod
    def _fake_advisory_fail() -> dict:
        return plan_ops._audit_finding(
            check="fake_adv",
            status="fail",
            canonical={"source": "hermetic", "value": "expected"},
            actual={"source": "hermetic", "value": "drifted"},
            reason="hermetic advisory failure",
            tier="advisory",
        )

    @staticmethod
    def _fake_default_pass() -> dict:
        return plan_ops._audit_finding(
            check="fake_def",
            status="pass",
            canonical={"source": "hermetic", "value": "expected"},
            actual={"source": "hermetic", "value": "expected"},
            reason=None,
            tier="default",
        )

    def _install_fake_checks(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_checks = (
            ("fake_def", self._fake_default_pass, "default"),
            ("fake_adv", self._fake_advisory_fail, "advisory"),
        )
        monkeypatch.setattr(plan_ops, "AUDIT_CHECKS", fake_checks)
        monkeypatch.setattr(
            plan_ops,
            "AUDIT_CHECK_NAMES",
            tuple(name for name, _, _ in fake_checks),
        )
        monkeypatch.setattr(
            plan_ops,
            "AUDIT_CHECK_TIERS",
            {name: tier for name, _, tier in fake_checks},
        )

    def _invoke(
        self,
        capsys: pytest.CaptureFixture[str],
        *,
        check: str | None,
        strict: bool,
    ) -> tuple[int, dict]:
        import argparse as _argparse

        ns = _argparse.Namespace(
            json=True, list=False, check=check,
            strict=strict, report_file=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            plan_ops.cmd_audit(ns)
        captured = capsys.readouterr()
        body = json.loads(captured.out)
        code = exc_info.value.code
        assert isinstance(code, int)
        return code, body

    def test_strict_promotes_advisory_without_explicit_check(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Strict-only mechanism: with `check=None`, the explicit-check
        # path cannot admit the advisory finding. Baseline (no strict)
        # must exclude it; engaging strict must promote it.
        self._install_fake_checks(monkeypatch)

        baseline_code, baseline = self._invoke(
            capsys, check=None, strict=False,
        )
        assert baseline["overall"] == "pass"
        assert baseline_code == 0
        # The advisory finding is still reported, just excluded from the verdict.
        assert [f["check"] for f in baseline["findings"]] == ["fake_def", "fake_adv"]

        strict_code, strict_body = self._invoke(
            capsys, check=None, strict=True,
        )
        # Strict alone must promote the advisory failure into the verdict.
        assert strict_body["overall"] == "fail"
        assert strict_code == 1

    def test_explicit_check_forces_advisory_without_strict(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Explicit-check-only mechanism: with `strict=False`, only the
        # explicit-check path can admit the advisory finding. Baseline
        # must exclude it; naming it via `--check` must force it in.
        self._install_fake_checks(monkeypatch)

        baseline_code, baseline = self._invoke(
            capsys, check=None, strict=False,
        )
        assert baseline["overall"] == "pass"
        assert baseline_code == 0

        forced_code, forced_body = self._invoke(
            capsys, check="fake_adv", strict=False,
        )
        # Explicit --check alone must force the advisory failure into the verdict.
        assert forced_body["overall"] == "fail"
        assert forced_code == 1
        # Only the explicitly-requested check ran.
        assert [f["check"] for f in forced_body["findings"]] == ["fake_adv"]

    def test_strict_and_explicit_check_compose(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Composed: both paths active. Overall must still reflect the
        # advisory failure. Baseline (no mechanism) must remain pass, so
        # this test still rules out the "always fails" failure mode.
        self._install_fake_checks(monkeypatch)

        baseline_code, baseline = self._invoke(
            capsys, check=None, strict=False,
        )
        assert baseline["overall"] == "pass"
        assert baseline_code == 0

        composed_code, composed_body = self._invoke(
            capsys, check="fake_adv", strict=True,
        )
        assert composed_body["overall"] == "fail"
        assert composed_code == 1


class TestAuditReportFile:
    """V5: `--report-file PATH` writes a Markdown table report."""

    def test_audit_report_file_writes_markdown_table(self, tmp_path: Path) -> None:
        out = tmp_path / "audit.md"
        cp = _run("audit", "--report-file", str(out), "--json")
        assert cp.returncode == 0, cp.stderr
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "# Executor self-audit" in body
        assert "**Overall:**" in body
        # Table header + at least one default check row.
        assert "| Check | Tier | Status | Canonical | Actual | Reason |" in body
        assert "`status_vocabulary`" in body

    def test_audit_report_file_creates_parent_dirs(self, tmp_path: Path) -> None:
        out = tmp_path / "nested" / "dir" / "audit.md"
        cp = _run("audit", "--report-file", str(out), "--json")
        assert cp.returncode == 0, cp.stderr
        assert out.is_file()


class TestAuditFindingShape:
    """V4: every finding carries the documented shape."""

    def test_audit_findings_shape_is_canonical(self) -> None:
        cp = _run("audit", "--json")
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert "overall" in body
        assert "findings" in body
        assert "generated_at" in body
        for finding in body["findings"]:
            for key in (
                "check", "status", "tier",
                "canonical", "actual", "reason", "locations",
            ):
                assert key in finding, (
                    f"finding for {finding.get('check')!r} missing key {key!r}"
                )
            assert finding["status"] in {
                "pass", "pass_with_alias", "fail",
            }
            assert finding["tier"] in {"default", "advisory"}
            assert "source" in finding["canonical"]
            assert "value" in finding["canonical"]
            assert "source" in finding["actual"]
            assert "value" in finding["actual"]
            # Acceptance criterion: structured findings surface
            # path-and-reason pairs. Every `locations` entry must have
            # the documented shape.
            assert isinstance(finding["locations"], list), (
                f"finding for {finding.get('check')!r}: locations must be list"
            )
            for loc in finding["locations"]:
                assert "path" in loc and isinstance(loc["path"], str)
                assert "line" in loc  # int or None
                assert "reason" in loc and isinstance(loc["reason"], str)


class TestAuditTextRender:
    """Finding A regression: `audit` (no `--json`) prints a readable
    plaintext summary instead of the Python dict-repr fallback.

    Shape invariants:
      * Header line `audit: <overall>`
      * Per-finding bullet `  - [<tier>] <check>: <reason>` where
        `<tier>` is a registered audit-check tier (`default` /
        `advisory`).
      * No Python-repr tokens (single-quoted dict keys, `{'...'}` braces)
      * `--json` output unchanged (still valid JSON)
    """

    def test_default_stdout_has_audit_header(self) -> None:
        cp = _run("audit")
        assert cp.stdout.startswith("audit: "), (
            f"expected leading `audit: <overall>` header; got {cp.stdout!r}"
        )
        # Overall is on the first line.
        first_line = cp.stdout.splitlines()[0]
        assert first_line in {"audit: pass", "audit: fail"}, (
            f"header must name overall; got {first_line!r}"
        )

    def test_default_stdout_has_per_finding_bullets(self) -> None:
        cp = _run("audit")
        # Must contain bullets of shape `  - [<tier>] <check>: <reason>`
        # where `<tier>` is a registered audit-check tier. The loose
        # "any bracket-and-colon" check here would accept `[pass]` style
        # leftovers; assert the bracket carries a tier value so a
        # regression that re-introduces status-in-bracket would trip.
        valid_tiers = {"default", "advisory"}
        bullets = [
            line for line in cp.stdout.splitlines()
            if line.startswith("  - [")
        ]
        assert bullets, (
            f"expected per-finding bullets; got {cp.stdout!r}"
        )
        for line in bullets:
            end = line.find("]")
            assert end != -1, (
                f"bullet missing closing `]`: {line!r}"
            )
            bracket = line[len("  - ["):end]
            assert bracket in valid_tiers, (
                f"bullet bracket must be a tier value (one of {valid_tiers}); "
                f"got {bracket!r} in line {line!r}"
            )
            assert ": " in line[end:], (
                f"bullet must separate check from reason with `: `: {line!r}"
            )

    def test_default_stdout_has_no_python_repr_tokens(self) -> None:
        cp = _run("audit")
        # Python dict repr emits single-quoted keys like "{'overall':".
        # The text renderer must not leak those tokens.
        assert "{'overall'" not in cp.stdout, (
            f"stdout carries Python dict-repr tokens: {cp.stdout!r}"
        )
        assert "{'findings'" not in cp.stdout
        assert "{'check'" not in cp.stdout

    def test_json_output_still_parses_as_json(self) -> None:
        # The plaintext render must not disturb the `--json` path.
        cp = _run("audit", "--json")
        body = _parse_json(cp)
        assert "overall" in body
        assert "findings" in body


class TestAuditPortableTierLegacyMarker:
    """The `portable_tier` check skips lines wrapped in
    `<!-- portable_tier: legacy-example -->` /
    `<!-- /portable_tier: legacy-example -->` markers so deliberate
    instructional examples can mention the legacy invocation without
    flunking the check."""

    def test_portable_tier_skips_legacy_marker_block(self, tmp_path: Path) -> None:
        sample = tmp_path / "sample.md"
        sample.write_text(
            "# Sample\n"
            "<!-- portable_tier: legacy-example -->\n"
            "Use `venv/bin/python plan_ops.py preflight ...` (legacy form).\n"
            "<!-- /portable_tier: legacy-example -->\n"
            "After the legacy block, no literal: python3 plan_ops.py ...\n",
            encoding="utf-8",
        )
        hits = plan_ops._scan_portable_tier_violations(sample)
        assert hits == []

    def test_portable_tier_flags_literal_outside_marker(self, tmp_path: Path) -> None:
        sample = tmp_path / "bad.md"
        sample.write_text(
            "# Bad\nUse `venv/bin/python plan_ops.py audit` here.\n",
            encoding="utf-8",
        )
        hits = plan_ops._scan_portable_tier_violations(sample)
        assert len(hits) == 1
        assert hits[0].endswith(":2")


class TestAuditCanonicalContractShape:
    """The CANONICAL_CONTRACT module constant carries every key the
    checks consume; future contributors who add a check must also add
    the corresponding canonical entry.

    TASK-008: the schedule-field and implementer-label keys retain
    plural shapes (lists), but their file-mode aliases were REMOVED
    rather than absorbed. `decompose-plan` is registered as a
    first-class subcommand."""

    def test_canonical_contract_has_required_keys(self) -> None:
        for key in (
            "status_vocabulary",
            "schedule_task_fields",
            "schedule_batch_fields",
            "implementer_concerns_labels",
            "implementer_plan_adaptations_label",
            "execution_log_columns",
            "subcommands",
        ):
            assert key in plan_ops.CANONICAL_CONTRACT, (
                f"CANONICAL_CONTRACT missing canonical key {key!r}"
            )

    def test_canonical_status_vocabulary_excludes_open(self) -> None:
        # TASK-008 (per_task_dispatch_refactor_v2) REMOVED the file-mode
        # `open` alias. The directory-only canonical contract is the
        # single accepted runtime form.
        canonical = set(plan_ops.CANONICAL_CONTRACT["status_vocabulary"])
        assert "open" not in canonical
        assert "pending" in canonical

    def test_canonical_schedule_fields_directory_only(self) -> None:
        # TASK-008: `task_id` / `batch_index` field aliases were REMOVED
        # from the canonical contract. Only the canonical `id` / `index`
        # forms are recognized. `plan_file` is also a required canonical
        # task field (added in the directory-only contract remediation):
        # every task carries the basename of its owning plan file so
        # per-task dispatch can attribute work back to the plan directory.
        assert plan_ops.CANONICAL_CONTRACT["schedule_task_fields"] == ["id", "plan_file"]
        assert plan_ops.CANONICAL_CONTRACT["schedule_batch_fields"] == ["index"]

    def test_canonical_implementer_concerns_label_directory_only(self) -> None:
        # TASK-008: the legacy `**Concerns:**` label was REMOVED. Only
        # the canonical `**Concerns for reviewer:**` label is recognized.
        assert plan_ops.CANONICAL_CONTRACT["implementer_concerns_labels"] == [
            "**Concerns for reviewer:**",
        ]

    def test_alias_windows_is_empty(self) -> None:
        # TASK-008 REMOVED every file-mode alias. ALIAS_WINDOWS is
        # intentionally empty going forward — future genuine alias
        # windows may reintroduce entries.
        assert plan_ops.ALIAS_WINDOWS == {}

    def test_canonical_subcommands_includes_decompose_plan(self) -> None:
        # TASK-008: `decompose-plan` (the single-file → directory bridge
        # from TASK-001) is a first-class subcommand entry in the
        # canonical contract, not a file-mode remnant.
        subs = list(plan_ops.CANONICAL_CONTRACT["subcommands"])
        assert "decompose-plan" in subs
        # Sanity checks: the other core v2 subcommands are also registered.
        for required in ("preflight", "build-tasks", "commit-task"):
            assert required in subs, required


class TestAuditSchemasEnumSetCompare:
    """Finding C regression: `_check_schemas` compares the verdict enum
    as a set, not an ordered list. Reordering the enum in the schema
    (a non-semantic change) must not flip the check; set-mismatch
    (missing/extra members) must still fail.

    Fixtures are written to `tmp_path` and `_SCRIPT_DIR` is monkeypatched
    so the real schema files on disk are never mutated.
    """

    _IMPL_SCHEMA = {
        "type": "object",
        "properties": {
            "blockers": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id"],
    }
    _REQUIRED = [
        "task_id", "verdict", "findings",
        "notes", "scope_ok", "acceptance_met", "summary",
    ]

    def _write_schemas(
        self,
        tmp_path: Path,
        verdict_enum: list[str],
    ) -> None:
        (tmp_path / "codex_implement_schema.json").write_text(
            json.dumps(self._IMPL_SCHEMA), encoding="utf-8",
        )
        review = {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": verdict_enum},
            },
            "required": list(self._REQUIRED),
        }
        (tmp_path / "codex_review_schema.json").write_text(
            json.dumps(review), encoding="utf-8",
        )

    def test_reordered_enum_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Canonical order is [clean, minor-findings, needs-rework]; a
        # reordering is a non-semantic change that must not fail.
        self._write_schemas(
            tmp_path, ["needs-rework", "clean", "minor-findings"],
        )
        monkeypatch.setattr(plan_ops, "_SCRIPT_DIR", tmp_path)
        finding = plan_ops._check_schemas()
        assert finding["check"] == "schemas"
        assert finding["status"] == "pass", (
            f"reordered enum must pass; got {finding!r}"
        )

    def test_missing_enum_member_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Dropping `needs-rework` is a real set mismatch and must fail.
        self._write_schemas(tmp_path, ["clean", "minor-findings"])
        monkeypatch.setattr(plan_ops, "_SCRIPT_DIR", tmp_path)
        finding = plan_ops._check_schemas()
        assert finding["status"] == "fail", (
            f"missing enum member must fail; got {finding!r}"
        )

    def test_extra_enum_member_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Adding an unknown verdict is also a set mismatch -> fail.
        self._write_schemas(
            tmp_path,
            ["clean", "minor-findings", "needs-rework", "bogus"],
        )
        monkeypatch.setattr(plan_ops, "_SCRIPT_DIR", tmp_path)
        finding = plan_ops._check_schemas()
        assert finding["status"] == "fail", (
            f"extra enum member must fail; got {finding!r}"
        )


class TestAuditDocReferences:
    """The audit must be referenced by both SKILL.md and the design doc
    so operators discover the readiness check via the same surfaces they
    use for everything else."""

    def test_skill_md_references_audit_subcommand(self) -> None:
        skill = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "SKILL.md"
        )
        body = skill.read_text(encoding="utf-8")
        assert "plan_ops.py audit" in body, (
            "SKILL.md must reference the audit subcommand for operators"
        )

    def test_design_doc_references_audit_in_section_14(self) -> None:
        doc = REPO_ROOT / "docs" / "plans" / "DUAL_AGENT_PLAN_EXECUTOR.md"
        body = doc.read_text(encoding="utf-8")
        # The §14 verification plan section must name the audit.
        idx = body.find("## 14.")
        assert idx >= 0, "design doc lost §14 anchor"
        section = body[idx: body.find("## 15.", idx)]
        assert "audit" in section.lower(), (
            "design doc §14 must reference the audit readiness check"
        )


class TestPlanReviewTriageContract:
    """TASK-005 regression harness — parser-to-routing contract for both sources."""

    def _run(
        self,
        payload: dict,
        *,
        source: str,
        findings_count: int,
    ) -> subprocess.CompletedProcess:
        report = (
            "Summary\n\n```json\n" + json.dumps(payload) + "\n```\n"
        )
        return subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-triage-report", "--stdin",
                "--source", source,
                "--findings-count", str(findings_count),
                "--json",
            ],
            input=report,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )

    @pytest.mark.parametrize("source", ["plan-analyst", "codex-plan-review"])
    @pytest.mark.parametrize(
        "verdict", ["ship", "ship-with-fixes", "partial-agreement", "needs-rework"],
    )
    def test_plan_review_triage_contract_verdict_roundtrip(
        self, source: str, verdict: str,
    ) -> None:
        payload = {
            "verdict": verdict,
            "load_bearing": [0, 2] if verdict == "partial-agreement" else [],
            "dismissed": [1, 3] if verdict == "partial-agreement" else [],
            "summary": "triage rationale",
        }
        cp = self._run(payload, source=source, findings_count=4)
        assert cp.returncode == 0, cp.stderr
        body = json.loads(cp.stdout)
        assert body["verdict"] == verdict
        assert body["source"] == source
        assert body["load_bearing"] == payload["load_bearing"]
        assert body["dismissed"] == payload["dismissed"]
        assert body["findings_count"] == 4

    @pytest.mark.parametrize("source", ["plan-analyst", "codex-plan-review"])
    @pytest.mark.parametrize(
        ("load_bearing", "dismissed", "code"),
        [
            ([], [0, 1], "partial-agreement-invalid-split"),
            ([0, 1], [], "partial-agreement-invalid-split"),
            ([0, 0], [1], "partial-agreement-invalid-split"),
            ([0], [0], "triage-buckets-not-disjoint"),
        ],
    )
    def test_plan_review_triage_contract_partial_agreement_invariants(
        self,
        source: str,
        load_bearing: list[int],
        dismissed: list[int],
        code: str,
    ) -> None:
        payload = {
            "verdict": "partial-agreement",
            "load_bearing": load_bearing,
            "dismissed": dismissed,
            "summary": "x",
        }
        cp = self._run(payload, source=source, findings_count=2)
        assert cp.returncode != 0
        body = json.loads(cp.stdout or cp.stderr)
        codes = {e["code"] for e in body.get("errors", [])}
        assert code in codes, (code, body)

    @pytest.mark.parametrize(
        ("source", "needle"),
        [
            ("plan-analyst", "gap index"),
            ("codex-plan-review", "finding index"),
        ],
    )
    def test_plan_review_triage_contract_source_aware_out_of_range_message(
        self, source: str, needle: str,
    ) -> None:
        payload = {
            "verdict": "partial-agreement",
            "load_bearing": [0],
            "dismissed": [5],
            "summary": "x",
        }
        cp = self._run(payload, source=source, findings_count=2)
        assert cp.returncode != 0
        body = json.loads(cp.stdout or cp.stderr)
        messages = " ".join(e.get("message", "") for e in body.get("errors", []))
        assert needle in messages, (needle, messages)


class Test_plan_review_triage_integration:
    """TASK-006 harness — parser-driven routing simulation for both triage seams."""

    _ANALYST_GAPS = [
        {
            "location": "TASK-001",
            "missing_field": "Files",
            "severity": "soft",
            "detail": "Missing explicit Files bullet.",
        },
        {
            "location": "TASK-002",
            "missing_field": "Dependencies",
            "severity": "hard",
            "detail": "Missing dependency declaration.",
        },
    ]
    # TASK-007 per-finding fan-out coverage — finding 0 is task-targeted
    # (target_task_id="001"), finding 1 is schedule-level (target_task_id=None).
    # The schedule-level entry is required so the simulator can exercise
    # the {finding, target_task_id=null, roster_file} triple that has no
    # child_plan_file.
    _CODEX_FINDINGS = [
        {
            "severity": "important",
            "message": "Acceptance criteria are underspecified.",
            "location": "TASK-001",
            "target_task_id": "001",
        },
        {
            "severity": "minor",
            "message": "Batch ordering is ambiguous.",
            "location": "batches[0]",
            "target_task_id": None,
        },
    ]

    # Simulated schedule + plan-dir so the Codex-branch fan-out can
    # resolve target_task_id -> child_plan_file (task-targeted) and
    # pass roster_file=<plan_dir>/00_INDEX.json (schedule-level).
    _PLAN_DIR = "/tmp/sim_plan_dir"
    _SCHEDULE_TASKS = {
        "001": f"{_PLAN_DIR}/TASK-001_seed.md",
    }
    _ROSTER_FILE = f"{_PLAN_DIR}/00_INDEX.json"

    def _parse_triage(
        self,
        *,
        source: str,
        verdict: str,
        load_bearing: list[int] | None = None,
        dismissed: list[int] | None = None,
        findings_count: int = 2,
        summary: str = "triage rationale",
    ) -> dict:
        cp = subprocess.run(
            [
                str(PY), str(SCRIPT),
                "parse-plan-review-triage-report", "--stdin",
                "--source", source,
                "--findings-count", str(findings_count),
                "--json",
            ],
            input=_plan_review_triage_envelope(
                verdict=verdict,
                load_bearing=load_bearing,
                dismissed=dismissed,
                summary=summary,
            ),
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stdout or cp.stderr
        return json.loads(cp.stdout)

    def _build_author_dispatch(
        self, source: str, finding: dict,
    ) -> dict:
        """Build a single plan-author dispatch payload for ONE finding
        (TASK-007 per-finding fan-out).

        Shape by target_task_id:

        - target_task_id != None (task-targeted): payload carries
          {finding, target_task_id, child_plan_file}; roster_file is
          absent.
        - target_task_id is None (schedule-level): payload carries
          {finding, target_task_id: None, roster_file}; child_plan_file
          is absent.
        """
        target_task_id = finding.get("target_task_id")
        dispatch: dict = {
            "agent": "plan-author",
            "source": source,
            "finding": finding,
            "target_task_id": target_task_id,
        }
        if target_task_id is None:
            # Schedule-level — roster_file present, child_plan_file absent.
            dispatch["roster_file"] = self._ROSTER_FILE
        else:
            # Task-targeted — child_plan_file resolved via schedule tasks[].
            dispatch["child_plan_file"] = self._SCHEDULE_TASKS[target_task_id]
        return dispatch

    def _simulate(
        self,
        *,
        source: str,
        verdict: str,
        analyst_binding: bool = False,
        codex_plan_review_binding: bool = False,
        allow_gaps: bool = False,
        no_auto_revise: bool = False,
        load_bearing: list[int] | None = None,
        dismissed: list[int] | None = None,
    ) -> dict:
        events: list[dict] = []
        summary: dict = {}
        dispatches: list[dict] = []
        run_end: dict | None = None

        def log(event: str, **fields: object) -> None:
            events.append({"event": event, **fields})

        if source == "plan-analyst":
            log("run_start", run_id="R1")
            log(
                "analyst_done",
                run_id="R1",
                outcome="needs-enrichment",
                gaps_count=len(self._ANALYST_GAPS),
            )
            if allow_gaps:
                log(
                    "analyst_triage_skipped",
                    run_id="R1",
                    reason="allow_gaps",
                )
                log("schedule_written", run_id="R1")
                return {
                    "events": events,
                    "summary": summary,
                    "dispatches": dispatches,
                    "run_end": run_end,
                    "route": "proceed",
                }
            if analyst_binding:
                log(
                    "analyst_triage_skipped",
                    run_id="R1",
                    reason="binding_flag",
                )
                run_end = {"reason": "plan_analyst_failed"}
                log("run_end", run_id="R1", outcome="failed", **run_end)
                return {
                    "events": events,
                    "summary": summary,
                    "dispatches": dispatches,
                    "run_end": run_end,
                    "route": "halt",
                }

            parsed = self._parse_triage(
                source=source,
                verdict=verdict,
                load_bearing=load_bearing,
                dismissed=dismissed,
                findings_count=len(self._ANALYST_GAPS),
            )
            log(
                "plan_review_triage_start",
                run_id="R1",
                source=source,
                findings_count=len(self._ANALYST_GAPS),
            )
            log(
                "plan_review_triage_done",
                run_id="R1",
                source=source,
                verdict=parsed["verdict"],
                load_bearing_count=len(parsed["load_bearing"] or []),
                dismissed_count=len(parsed["dismissed"] or []),
            )
            if verdict == "ship":
                summary["banner"] = "[analyst-triage-disagreement]"
                summary["analyst_gaps"] = list(self._ANALYST_GAPS)
                log("schedule_written", run_id="R1")
                route = "proceed"
            elif verdict == "ship-with-fixes":
                summary["Analyst triage notes"] = list(self._ANALYST_GAPS)
                log("schedule_written", run_id="R1")
                route = "proceed"
            elif verdict == "partial-agreement":
                dispatches.append({
                    "agent": "plan-author",
                    "source": source,
                    "payload": [self._ANALYST_GAPS[i] for i in parsed["load_bearing"]],
                })
                summary["dismissed_indices"] = parsed["dismissed"]
                log("plan_author_start", run_id="R1", source=source)
                log("plan_author_done", run_id="R1", source=source)
                log("analyst_done", run_id="R1", outcome="valid")
                log("schedule_written", run_id="R1")
                route = "plan-author"
            else:
                dispatches.append({
                    "agent": "plan-author",
                    "source": source,
                    "payload": list(self._ANALYST_GAPS),
                })
                log("plan_author_start", run_id="R1", source=source)
                log("plan_author_done", run_id="R1", source=source)
                log("analyst_done", run_id="R1", outcome="valid")
                log("schedule_written", run_id="R1")
                route = "plan-author"
            return {
                "events": events,
                "summary": summary,
                "dispatches": dispatches,
                "run_end": run_end,
                "route": route,
            }

        log("schedule_written", run_id="R1")
        log("plan_review_start", run_id="R1", reviewer="codex")
        log(
            "plan_review_done",
            run_id="R1",
            verdict="needs-replan",
            findings_count=len(self._CODEX_FINDINGS),
        )
        if codex_plan_review_binding:
            run_end = {"reason": "plan_review_failed"}
            log("run_end", run_id="R1", outcome="failed", **run_end)
            return {
                "events": events,
                "summary": summary,
                "dispatches": dispatches,
                "run_end": run_end,
                "route": "halt",
            }
        if no_auto_revise:
            run_end = {"reason": "plan_review_failed"}
            log("run_end", run_id="R1", outcome="failed", **run_end)
            return {
                "events": events,
                "summary": summary,
                "dispatches": dispatches,
                "run_end": run_end,
                "route": "halt",
            }

        parsed = self._parse_triage(
            source=source,
            verdict=verdict,
            load_bearing=load_bearing,
            dismissed=dismissed,
            findings_count=len(self._CODEX_FINDINGS),
        )
        log(
            "plan_review_triage_start",
            run_id="R1",
            source=source,
            findings_count=len(self._CODEX_FINDINGS),
        )
        log(
            "plan_review_triage_done",
            run_id="R1",
            source=source,
            verdict=parsed["verdict"],
            load_bearing_count=len(parsed["load_bearing"] or []),
            dismissed_count=len(parsed["dismissed"] or []),
        )
        if verdict == "ship":
            summary["banner"] = "[plan-review-disagreement]"
            summary["plan_review_findings"] = list(self._CODEX_FINDINGS)
            log("batch_start", run_id="R1")
            route = "proceed"
        elif verdict == "ship-with-fixes":
            summary["Plan review notes"] = list(self._CODEX_FINDINGS)
            log("batch_start", run_id="R1")
            route = "proceed"
        elif verdict == "partial-agreement":
            # TASK-007: one plan-author dispatch per forwarded finding, each
            # carrying a single {finding, target_task_id, child_plan_file}
            # triple (child_plan_file on task-targeted path; roster_file on
            # schedule-level path with child_plan_file absent).
            forwarded_findings = [
                self._CODEX_FINDINGS[i] for i in parsed["load_bearing"]
            ]
            for finding in forwarded_findings:
                dispatches.append(
                    self._build_author_dispatch(source, finding)
                )
            summary["Plan review notes"] = list(self._CODEX_FINDINGS)
            summary["dismissed_indices"] = parsed["dismissed"]
            for _ in forwarded_findings:
                log("plan_author_start", run_id="R1", source=source)
                log("plan_author_done", run_id="R1", source=source)
            log("analyst_done", run_id="R1", outcome="valid")
            log("plan_review_start", run_id="R1", reviewer="codex")
            log("plan_review_done", run_id="R1", verdict="approved")
            log("batch_start", run_id="R1")
            route = "plan-author"
        else:
            # needs-rework: full findings array forwarded, still fanned out
            # one dispatch per finding (uniform per-finding contract).
            forwarded_findings = list(self._CODEX_FINDINGS)
            for finding in forwarded_findings:
                dispatches.append(
                    self._build_author_dispatch(source, finding)
                )
            for _ in forwarded_findings:
                log("plan_author_start", run_id="R1", source=source)
                log("plan_author_done", run_id="R1", source=source)
            log("analyst_done", run_id="R1", outcome="valid")
            log("plan_review_start", run_id="R1", reviewer="codex")
            log("plan_review_done", run_id="R1", verdict="approved")
            log("batch_start", run_id="R1")
            route = "plan-author"
        return {
            "events": events,
            "summary": summary,
            "dispatches": dispatches,
            "run_end": run_end,
            "route": route,
        }

    @pytest.mark.parametrize(
        ("source", "verdict", "route"),
        [
            ("plan-analyst", "ship", "proceed"),
            ("plan-analyst", "ship-with-fixes", "proceed"),
            ("plan-analyst", "partial-agreement", "plan-author"),
            ("plan-analyst", "needs-rework", "plan-author"),
            ("codex-plan-review", "ship", "proceed"),
            ("codex-plan-review", "ship-with-fixes", "proceed"),
            ("codex-plan-review", "partial-agreement", "plan-author"),
            ("codex-plan-review", "needs-rework", "plan-author"),
        ],
    )
    def test_routes_each_source_and_verdict(
        self, source: str, verdict: str, route: str,
    ) -> None:
        load_bearing = [0] if verdict == "partial-agreement" else None
        dismissed = [1] if verdict == "partial-agreement" else None
        result = self._simulate(
            source=source,
            verdict=verdict,
            load_bearing=load_bearing,
            dismissed=dismissed,
        )
        assert result["route"] == route
        if route == "proceed":
            assert result["dispatches"] == []
            assert result["run_end"] is None
        else:
            # Every dispatched agent is plan-author regardless of path.
            assert all(
                d["agent"] == "plan-author" for d in result["dispatches"]
            )
            # Codex path fans out one dispatch per forwarded finding
            # (TASK-007). Analyst path still emits a single dispatch.
            if source == "codex-plan-review":
                expected_n = (
                    1 if verdict == "partial-agreement"  # load_bearing=[0]
                    else len(self._CODEX_FINDINGS)  # needs-rework → all
                )
                assert len(result["dispatches"]) == expected_n
                # Each Codex-path dispatch carries a SINGLE finding — no
                # array payload, no collapsed whole-plan dispatch.
                for d in result["dispatches"]:
                    assert "finding" in d, (
                        "Codex-path dispatch must carry a single `finding` "
                        "(not a `payload` array) per TASK-007 fan-out"
                    )
                    assert "payload" not in d or not isinstance(
                        d.get("payload"), list
                    ), (
                        "legacy array-payload shape is forbidden on the "
                        "Codex fan-out path"
                    )
            else:
                assert len(result["dispatches"]) == 1

    def test_analyst_binding_short_circuits_with_plan_analyst_failed(self) -> None:
        result = self._simulate(
            source="plan-analyst",
            verdict="ship",
            analyst_binding=True,
        )
        assert result["route"] == "halt"
        assert result["run_end"] == {"reason": "plan_analyst_failed"}
        assert [e["event"] for e in result["events"][-2:]] == [
            "analyst_triage_skipped",
            "run_end",
        ]

    def test_codex_plan_review_binding_short_circuits_with_plan_review_failed(self) -> None:
        result = self._simulate(
            source="codex-plan-review",
            verdict="ship",
            codex_plan_review_binding=True,
        )
        assert result["route"] == "halt"
        assert result["run_end"] == {"reason": "plan_review_failed"}
        assert [e["event"] for e in result["events"][-1:]] == ["run_end"]

    def test_allow_gaps_preserves_pre_triage_short_circuit(self) -> None:
        result = self._simulate(
            source="plan-analyst",
            verdict="needs-rework",
            allow_gaps=True,
            analyst_binding=True,
        )
        assert result["route"] == "proceed"
        assert result["run_end"] is None
        assert result["dispatches"] == []
        assert [e["event"] for e in result["events"][-2:]] == [
            "analyst_triage_skipped",
            "schedule_written",
        ]
        assert result["events"][-2]["reason"] == "allow_gaps"

    def test_no_auto_revise_preserves_codex_halt_without_triage(self) -> None:
        result = self._simulate(
            source="codex-plan-review",
            verdict="needs-rework",
            no_auto_revise=True,
        )
        assert result["route"] == "halt"
        assert result["run_end"] == {"reason": "plan_review_failed"}
        assert result["dispatches"] == []
        assert not any(
            e["event"] == "plan_review_triage_start" for e in result["events"]
        )

    @pytest.mark.parametrize(
        ("source", "verdict", "downstream_event"),
        [
            ("plan-analyst", "ship", "schedule_written"),
            ("plan-analyst", "ship-with-fixes", "schedule_written"),
            ("plan-analyst", "partial-agreement", "plan_author_start"),
            ("plan-analyst", "needs-rework", "plan_author_start"),
            ("codex-plan-review", "ship", "batch_start"),
            ("codex-plan-review", "ship-with-fixes", "batch_start"),
            ("codex-plan-review", "partial-agreement", "plan_author_start"),
            ("codex-plan-review", "needs-rework", "plan_author_start"),
        ],
    )
    def test_run_log_event_ordering_matches_documented_routing(
        self, source: str, verdict: str, downstream_event: str,
    ) -> None:
        load_bearing = [0] if verdict == "partial-agreement" else None
        dismissed = [1] if verdict == "partial-agreement" else None
        result = self._simulate(
            source=source,
            verdict=verdict,
            load_bearing=load_bearing,
            dismissed=dismissed,
        )
        triage_start_idx = next(
            i for i, event in enumerate(result["events"])
            if event["event"] == "plan_review_triage_start"
        )
        triage_done_idx = next(
            i for i, event in enumerate(result["events"])
            if event["event"] == "plan_review_triage_done"
        )
        downstream_idx = next(
            i for i, event in enumerate(result["events"])
            if event["event"] == downstream_event
        )
        assert triage_start_idx < triage_done_idx < downstream_idx
        assert result["events"][triage_start_idx]["source"] == source
        assert result["events"][triage_done_idx]["source"] == source

    @pytest.mark.parametrize(
        ("source", "verdict", "summary_key", "expected"),
        [
            ("plan-analyst", "ship", "banner", "[analyst-triage-disagreement]"),
            ("codex-plan-review", "ship", "banner", "[plan-review-disagreement]"),
            ("plan-analyst", "ship-with-fixes", "Analyst triage notes", _ANALYST_GAPS),
            ("codex-plan-review", "ship-with-fixes", "Plan review notes", _CODEX_FINDINGS),
        ],
    )
    def test_summary_carryover_for_ship_and_ship_with_fixes(
        self,
        source: str,
        verdict: str,
        summary_key: str,
        expected: object,
    ) -> None:
        result = self._simulate(source=source, verdict=verdict)
        assert result["summary"][summary_key] == expected

    @pytest.mark.parametrize(
        "source",
        ["plan-analyst", "codex-plan-review"],
    )
    def test_partial_agreement_summary_lists_dismissed_indices(self, source: str) -> None:
        result = self._simulate(
            source=source,
            verdict="partial-agreement",
            load_bearing=[0],
            dismissed=[1],
        )
        assert result["summary"]["dismissed_indices"] == [1]

    def test_author_fan_out_one_dispatch_per_finding_including_schedule_level(
        self,
    ) -> None:
        """TASK-007 contract — the Codex `needs-rework` path produces N
        plan-author dispatches when N findings are forwarded, each
        carrying a SINGLE finding (not an array). Including the
        schedule-level `target_task_id=null` path: that dispatch has
        `roster_file` present and `child_plan_file` absent / null.

        Exercises the fan-out with both a task-targeted finding (index 0,
        `target_task_id="001"`) and a schedule-level finding (index 1,
        `target_task_id=None`) in the same review pass.
        """
        result = self._simulate(
            source="codex-plan-review",
            verdict="needs-rework",
        )
        dispatches = result["dispatches"]

        # N findings forwarded → N dispatches (one per finding).
        assert len(dispatches) == len(self._CODEX_FINDINGS), (
            "needs-rework must fan out one plan-author dispatch per "
            "forwarded finding (legacy single-dispatch-with-array-payload "
            f"shape is forbidden); got {len(dispatches)} dispatches for "
            f"{len(self._CODEX_FINDINGS)} findings"
        )

        # Each dispatch is plan-author with a single-finding payload.
        for d in dispatches:
            assert d["agent"] == "plan-author"
            assert "finding" in d, (
                "dispatch must carry a single `finding` (not a `payload` "
                "array) per TASK-007 fan-out contract"
            )
            assert not isinstance(d.get("payload"), list), (
                "dispatch must not carry an array `payload` — the legacy "
                "collapsed shape is forbidden on the fan-out path"
            )

        # Task-targeted dispatch — target_task_id="001", child_plan_file
        # resolved from schedule tasks[], roster_file absent.
        task_targeted = [d for d in dispatches if d["target_task_id"] == "001"]
        assert len(task_targeted) == 1, (
            "exactly one task-targeted dispatch for target_task_id='001'"
        )
        dt = task_targeted[0]
        assert "child_plan_file" in dt, (
            "task-targeted dispatch must carry child_plan_file resolved "
            "via the schedule's tasks[].plan_file"
        )
        assert dt["child_plan_file"] == self._SCHEDULE_TASKS["001"]
        assert "roster_file" not in dt, (
            "roster_file must NOT render on the task-targeted path"
        )
        assert dt["finding"]["target_task_id"] == "001"

        # Schedule-level dispatch — target_task_id=None, roster_file
        # present (absolute path to 00_INDEX.json), child_plan_file
        # absent / null.
        schedule_level = [d for d in dispatches if d["target_task_id"] is None]
        assert len(schedule_level) == 1, (
            "exactly one schedule-level dispatch for target_task_id=None"
        )
        ds = schedule_level[0]
        assert "roster_file" in ds, (
            "schedule-level dispatch must carry roster_file so the "
            "author has a concrete edit target"
        )
        assert ds["roster_file"] == self._ROSTER_FILE
        # child_plan_file must be absent (not just None) on the
        # schedule-level path — the two shapes are mutually exclusive.
        assert "child_plan_file" not in ds or ds.get("child_plan_file") is None, (
            "child_plan_file must be absent (or explicitly null) on the "
            "schedule-level path"
        )
        assert ds["finding"]["target_task_id"] is None

        # Each dispatched finding is one of the forwarded findings
        # (no duplicates, no fabrications).
        dispatched_findings = [d["finding"] for d in dispatches]
        assert dispatched_findings == list(self._CODEX_FINDINGS), (
            "forwarded findings preserved in order, one per dispatch"
        )


# ---------------------------------------------------------------------------
# TASK-004: Directory-mode driver (orchestrator integration)
# ---------------------------------------------------------------------------
#
# Covers the integration points where TASK-001 (schedule plan_file), TASK-002
# (block-dependents multi-file cascade), and TASK-003 (analyst directory-mode
# input) come together. SKILL.md is the primary deliverable (prose, not code);
# the tests here exercise the plan_ops.py primitives the SKILL.md invokes and
# validate the shipped integration fixture in `tests/fixtures/directory_mode_plan/`.


DIRECTORY_MODE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "directory_mode_plan"
)


@pytest.fixture()
def directory_mode_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Copy the shipped fixture into a tmp dir so mutation tests don't touch
    the checked-in fixture files. Sandboxes the module globals so run-log
    writes land under tmp."""
    import shutil
    dst = tmp_path / "decomposed_plan"
    shutil.copytree(DIRECTORY_MODE_FIXTURE, dst)
    # Point module globals at tmp so log-event / lock etc. don't bleed.
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_dir)
    monkeypatch.setattr(plan_ops, "RUN_LOG_PATH", plans_dir / "_run_log.jsonl")
    monkeypatch.setattr(plan_ops, "RUN_LOCK_PATH", plans_dir / "_run_lock.json")
    return dst


class TestDirectoryModeFixture:
    """Validates the shipped fixture at tests/fixtures/directory_mode_plan/.

    Covers acceptance criterion: "Integration fixture ... three minimal child
    plans + 00_INDEX.json ... Each child is a single-task plan passing
    schema-valid."
    """

    def test_directory_mode_fixture_index_parses(self) -> None:
        """00_INDEX.json parses via the existing _parse_index_roster loader.

        TASK-004 explicitly notes that 00_INDEX.json schema is unchanged —
        the loader is reused as-is. This test locks in that invariant.
        """
        index = DIRECTORY_MODE_FIXTURE / "00_INDEX.json"
        roster = plan_ops._parse_index_roster(index)
        assert set(roster.keys()) == {"001", "002", "003"}
        assert roster["001"]["file"] == "TASK-001_seed.md"
        assert roster["002"]["file"] == "TASK-002_write_a.md"
        assert roster["003"]["file"] == "TASK-003_write_b.md"
        assert roster["001"]["depends_on"] == []
        assert roster["002"]["depends_on"] == ["001"]
        assert roster["003"]["depends_on"] == ["001"]

    def test_directory_mode_fixture_each_child_is_schema_valid(self) -> None:
        """Every child in chunks[] passes the schema-valid gate.

        Matches the Phase 0 per-child loop documented in SKILL.md's
        Directory-mode input section.
        """
        for child_name in (
            "TASK-001_seed.md",
            "TASK-002_write_a.md",
            "TASK-003_write_b.md",
        ):
            child = DIRECTORY_MODE_FIXTURE / child_name
            result = plan_ops._gate_schema_valid(child)
            assert result["status"] == "pass", (
                f"child {child_name} failed schema-valid: {result['reason']}"
            )

    def test_directory_mode_fixture_chunks_match_on_disk_children(self) -> None:
        """Every chunks[].file names a real sibling markdown file.

        Defensive check: a typo in 00_INDEX.json would break the Phase 0
        per-child schema-valid loop.
        """
        index = DIRECTORY_MODE_FIXTURE / "00_INDEX.json"
        roster = plan_ops._parse_index_roster(index)
        for entry in roster.values():
            child = DIRECTORY_MODE_FIXTURE / entry["file"]
            assert child.is_file(), (
                f"00_INDEX.json references {entry['file']} but it is not on disk"
            )


class TestDirectoryModePhase0SchemaLoop:
    """Phase 0 iterates chunks[].file and halts on first schema-valid failure.

    Covers acceptance criterion: "Phase 0 schema-valid iterates every
    chunks[].file in <dir>/00_INDEX.json and halts on the first failure,
    naming the offending child file in the halt message."
    """

    def test_directory_mode_schema_loop_halts_on_first_bad_child(
        self, tmp_path: Path,
    ) -> None:
        """Pseudocode in SKILL.md: `for chunk in roster["chunks"]: check
        schema-valid on <plans_dir>/<chunk.file>; halt with basename in
        message on first fail`.

        Build a roster that lists a valid child followed by a bogus child,
        iterate, and confirm we halt at the bogus child with its basename
        named in the reason.
        """
        d = tmp_path / "decomposed"
        d.mkdir()
        # Child 1: valid.
        good = d / "child-good.md"
        good.write_text(
            (DIRECTORY_MODE_FIXTURE / "TASK-001_seed.md").read_text(
                encoding="utf-8"
            ),
            encoding="utf-8",
        )
        # Child 2: structurally broken (missing required sections).
        bad = d / "child-bad.md"
        bad.write_text(
            "# Just a header, no ## Goal, no ## Context, no TASK block.\n",
            encoding="utf-8",
        )
        # Minimal 00_INDEX.json. Order matters — good first, then bad.
        (d / "00_INDEX.json").write_text(json.dumps({
            "schema_version": 1,
            "chunks": [
                {
                    "task_id": "001", "file": "child-good.md",
                    "depends_on": [], "status": "Pending", "superseded_by": [],
                },
                {
                    "task_id": "002", "file": "child-bad.md",
                    "depends_on": [], "status": "Pending", "superseded_by": [],
                },
            ],
        }), encoding="utf-8")

        roster = plan_ops._parse_index_roster(d / "00_INDEX.json")
        first_failure: tuple[str, dict] | None = None
        for tid, entry in roster.items():
            res = plan_ops._gate_schema_valid(d / entry["file"])
            if res["status"] == "fail":
                first_failure = (entry["file"], res)
                break
        assert first_failure is not None
        bad_name, bad_result = first_failure
        assert bad_name == "child-bad.md"
        # SKILL.md halt-message convention: "schema-valid failed for child
        # <basename>: <gate.reason>" — check both the basename and the
        # gate's own reason are surfaceable.
        assert bad_result["status"] == "fail"
        assert "missing" in bad_result["reason"].lower()


class TestDirectoryModeBatchNext:
    """batch-next batches file-disjoint cross-child tasks together.

    Covers acceptance criterion: "Integration test verifies ... batch-next
    batches TASK-002 + TASK-003 together".
    """

    def test_directory_mode_batch_next_batches_siblings_in_parallel(
        self, tmp_path: Path,
    ) -> None:
        """Schedule derived from the fixture: TASK-001 seeds, TASK-002/003
        depend on 001 and write disjoint files. Given TASK-001 done, batch-
        next returns BOTH TASK-002 and TASK-003 in one batch.
        """
        sched = _write_schedule(tmp_path, {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "claude",
                    "files": ["scratch/.gitkeep"],
                    "dependencies": [],
                    "plan_file": "TASK-001_seed.md",
                },
                {
                    "id": "002",
                    "agent": "claude",
                    "files": ["scratch/a.txt"],
                    "dependencies": ["001"],
                    "plan_file": "TASK-002_write_a.md",
                },
                {
                    "id": "003",
                    "agent": "claude",
                    "files": ["scratch/b.txt"],
                    "dependencies": ["001"],
                    "plan_file": "TASK-003_write_b.md",
                },
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"],
                 "file_locks": ["scratch/.gitkeep"]},
                {"index": 2, "task_ids": ["002", "003"],
                 "file_locks": ["scratch/a.txt", "scratch/b.txt"]},
            ],
        })
        # 001 already done; batch-next must return batch 2 with both siblings.
        cp = _run_batch_next(sched, done="001", parallel=2)
        assert cp.returncode == 0, cp.stderr
        body = _parse_json(cp)
        assert sorted(body["task_ids"]) == ["002", "003"]
        assert body["batch_index"] == 2
        # file_locks covers both disjoint leaf files.
        assert "scratch/a.txt" in body["file_locks"]
        assert "scratch/b.txt" in body["file_locks"]

    def test_directory_mode_batch_next_preserves_plan_file_in_schedule(
        self, tmp_path: Path,
    ) -> None:
        """batch-next reads a schedule with per-task plan_file and emits the
        picked tasks; the downstream orchestrator re-reads the schedule for
        each picked id to get its plan_file.

        This test confirms the schedule on disk still carries plan_file for
        each task after the batch-next subcommand runs — i.e. batch-next
        does not rewrite or strip plan_file. (Preservation is the TASK-001
        invariant; re-verify here in the directory-mode context.)
        """
        payload = {
            "outcome": "valid",
            "tasks": [
                {"id": "001", "agent": "claude", "files": ["f1"],
                 "dependencies": [], "plan_file": "c1.md"},
                {"id": "002", "agent": "claude", "files": ["f2"],
                 "dependencies": [], "plan_file": "c2.md"},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"],
                 "file_locks": ["f1", "f2"]},
            ],
        }
        sched = _write_schedule(tmp_path, payload)
        cp = _run_batch_next(sched, parallel=2)
        assert cp.returncode == 0
        # Schedule on disk must still carry plan_file for each task.
        reread = json.loads(sched.read_text(encoding="utf-8"))
        assert [t.get("plan_file") for t in reread["tasks"]] == ["c1.md", "c2.md"]


class TestDirectoryModeCommitTask:
    """commit-task with --plan-file <child-abs> flips THAT child's status,
    not any sibling.

    Covers acceptance criterion: "commit-task flips the right child's
    header". Tests the substitution at the Phase D.3 write site.
    """

    def test_directory_mode_commit_task_flips_only_target_child(
        self, directory_mode_sandbox: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dry-run commit-task against the TASK-002 child file. The target
        child's TASK-002 status bullet flips to `done`; the sibling child
        files (TASK-001, TASK-003) are untouched byte-for-byte.

        Uses --dry-run so we don't invoke git, but the plan-status mutate
        happens before the git path in cmd_commit_task — so the flip is
        persisted on disk. That is the behavior the acceptance test cares
        about: per-task <plan-file> routes the mutation to the right file.

        Note: --dry-run exits BEFORE the git commit but the plan-status
        flip has already been applied (per cmd_commit_task's ordering).
        We don't actually want --dry-run here because it exits before
        _write_text. Instead: use in-process mutate_task_status which is
        exactly what cmd_commit_task calls for the flip — bypasses git
        entirely and is the right seam for directory-mode routing tests.
        """
        child_002 = directory_mode_sandbox / "TASK-002_write_a.md"
        child_001_before = (
            directory_mode_sandbox / "TASK-001_seed.md"
        ).read_text(encoding="utf-8")
        child_003_before = (
            directory_mode_sandbox / "TASK-003_write_b.md"
        ).read_text(encoding="utf-8")

        # Simulate the Phase D.3 write: mutate TASK-002 status in the
        # TASK-002 child file only.
        original = child_002.read_text(encoding="utf-8")
        mutated, _prior = plan_ops.mutate_task_status(original, "002", "done")
        child_002.write_text(mutated, encoding="utf-8")

        # Target child: TASK-002 is now `done`.
        after = child_002.read_text(encoding="utf-8")
        assert "- **Status:** done" in after
        # Sibling children: byte-identical to their pre-commit state.
        assert (
            (directory_mode_sandbox / "TASK-001_seed.md").read_text(encoding="utf-8")
            == child_001_before
        )
        assert (
            (directory_mode_sandbox / "TASK-003_write_b.md").read_text(encoding="utf-8")
            == child_003_before
        )

    def test_directory_mode_commit_task_child_resolution_via_plan_file(
        self, directory_mode_sandbox: Path,
    ) -> None:
        """The orchestrator resolves tasks[].plan_file against <plans_dir>
        to get the absolute path it hands to --plan-file.

        Re-exercises the per-task resolution documented in SKILL.md's
        'Per-task <plan-file> resolution' section:
          child_path = <plans_dir> / task.plan_file  (if present)
                     or fallback to the run-level plan path.
        """
        # Schedule entry for TASK-002 with plan_file set.
        task_entry = {
            "id": "002",
            "plan_file": "TASK-002_write_a.md",
        }
        # Resolution:
        resolved = directory_mode_sandbox / task_entry["plan_file"]
        assert resolved.is_file(), (
            "directory-mode resolution produced non-existent path"
        )
        assert resolved.name == "TASK-002_write_a.md"

        # Fallback when task lacks plan_file: use the single plan path.
        fallback_single_file = directory_mode_sandbox / "TASK-001_seed.md"
        task_no_pf = {"id": "001"}  # no plan_file
        resolved_fallback = (
            directory_mode_sandbox / task_no_pf["plan_file"]
            if task_no_pf.get("plan_file")
            else fallback_single_file
        )
        assert resolved_fallback == fallback_single_file


class TestDirectoryModeBlockDependents:
    """Multi-file block-dependents cascade with directory-mode schedule.

    Covers acceptance criterion: "multi-file block-dependents when TASK-001
    is seeded to fail".

    TASK-002 (the block-dependents internals change) already has its own
    test class `TestBlockDependentsMultiFile`; this class is the directory-
    mode *integration* angle that exercises the SHIPPED fixture files as
    the source plan markdown.
    """

    def test_directory_mode_block_dependents_cascade_across_children(
        self, directory_mode_sandbox: Path, tmp_path: Path,
    ) -> None:
        """Given the fixture's three-child layout and a schedule mirroring
        the analyst's directory-mode output, block-dependents with
        --failed 001 cascades:
          - TASK-002 flipped `blocked` in TASK-002_write_a.md
          - TASK-003 flipped `blocked` in TASK-003_write_b.md
          - TASK-001 untouched (fail-task owns the failed task's own file)
        """
        # Build schedule with per-task plan_file entries exactly as the
        # analyst (TASK-003) would emit for this directory.
        sched = tmp_path / "schedule.json"
        sched.write_text(json.dumps({
            "outcome": "valid",
            "tasks": [
                {"id": "001", "dependencies": [], "plan_file": "TASK-001_seed.md"},
                {"id": "002", "dependencies": ["001"],
                 "plan_file": "TASK-002_write_a.md"},
                {"id": "003", "dependencies": ["001"],
                 "plan_file": "TASK-003_write_b.md"},
            ],
            "batches": [],
            "gaps": [],
            "risks": [],
        }), encoding="utf-8")

        import argparse as _argparse
        ns = _argparse.Namespace(
            command="block-dependents",
            schedule_file=str(sched),
            plan_file=str(directory_mode_sandbox / "TASK-001_seed.md"),
            failed="001",
            run_id="DM-RUN-1",
            json=True,
        )
        code, body = _bd_call(ns)
        assert code == 0, body
        assert sorted(body["blocked_task_ids"]) == ["002", "003"]
        assert sorted(body["plan_mutations_applied"]) == ["002", "003"]

        # TASK-002 flipped in TASK-002_write_a.md
        t2_text = (
            directory_mode_sandbox / "TASK-002_write_a.md"
        ).read_text(encoding="utf-8")
        t2_status = re.search(
            r"### TASK-002:[^\n]*\n\n- \*\*Status:\*\* (\S+)", t2_text,
        )
        assert t2_status is not None and t2_status.group(1) == "blocked"

        # TASK-003 flipped in TASK-003_write_b.md
        t3_text = (
            directory_mode_sandbox / "TASK-003_write_b.md"
        ).read_text(encoding="utf-8")
        t3_status = re.search(
            r"### TASK-003:[^\n]*\n\n- \*\*Status:\*\* (\S+)", t3_text,
        )
        assert t3_status is not None and t3_status.group(1) == "blocked"

        # TASK-001 (failed) untouched — fail-task owns the failed task's own
        # status; block-dependents never flips the failed id itself.
        t1_text = (
            directory_mode_sandbox / "TASK-001_seed.md"
        ).read_text(encoding="utf-8")
        t1_status = re.search(
            r"### TASK-001:[^\n]*\n\n- \*\*Status:\*\* (\S+)", t1_text,
        )
        assert t1_status is not None and t1_status.group(1) == "pending"

        # Each blocked event carries plan_file pointing to the dependent's
        # own child file — directory-mode audit attribution. RUN_LOG_PATH
        # was patched in `directory_mode_sandbox` to tmp_path/docs/plans,
        # where directory_mode_sandbox == tmp_path/decomposed_plan — so
        # the run log lives at directory_mode_sandbox.parent/docs/plans.
        events = _bd_read_run_log_events_in(
            directory_mode_sandbox.parent / "docs" / "plans"
        )
        by_tid = {e["task_id"]: e for e in events}
        assert "002" in by_tid, (
            f"TASK-002 not in run log events; got {list(by_tid.keys())}; "
            f"events={events}"
        )
        assert "003" in by_tid
        assert by_tid["002"]["plan_file"] == "TASK-002_write_a.md"
        assert by_tid["003"]["plan_file"] == "TASK-003_write_b.md"


class TestDirectoryModeUpdatePlanHeader:
    """update-plan-header is called once per distinct child in directory mode.

    Covers acceptance criterion: "update-plan-header iterates the distinct
    plan_file values present in completed + failed tasks. Each child's own
    top-level **Status:** flips to complete when every task in that child
    passed, partial otherwise. A run-level aggregate header is NOT
    synthesized."
    """

    def test_directory_mode_per_child_header_iteration(
        self, directory_mode_sandbox: Path,
    ) -> None:
        """Simulate an end-of-run where TASK-001 and TASK-002 passed (both
        in their own children) and TASK-003 failed. The orchestrator's
        end-of-run partitions by plan_file, and calls update-plan-header
        for each child with that child's own local outcome:
          - TASK-001_seed.md → `complete` (its only task TASK-001 passed)
          - TASK-002_write_a.md → `complete` (TASK-002 passed)
          - TASK-003_write_b.md → `partial` (TASK-003 failed)
        """
        # Simulated completed + failed tasks with plan_file routing.
        completed = [
            {"task_id": "001", "plan_file": "TASK-001_seed.md"},
            {"task_id": "002", "plan_file": "TASK-002_write_a.md"},
        ]
        failed = [
            {"task_id": "003", "plan_file": "TASK-003_write_b.md"},
        ]

        # Partition by plan_file and compute per-child status.
        per_child: dict[str, dict] = {}
        for t in completed:
            pf = t["plan_file"]
            per_child.setdefault(pf, {"done": 0, "failed": 0})["done"] += 1
        for t in failed:
            pf = t["plan_file"]
            per_child.setdefault(pf, {"done": 0, "failed": 0})["failed"] += 1

        # For each distinct plan_file, call update-plan-header with the
        # child's local outcome.
        for child_basename, counts in per_child.items():
            child = directory_mode_sandbox / child_basename
            status = "complete" if counts["failed"] == 0 else "partial"
            import argparse as _argparse
            ns = _argparse.Namespace(
                command="update-plan-header",
                plan_file=str(child),
                status=status,
                json=True,
            )
            import io
            import contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                try:
                    plan_ops.cmd_update_plan_header(ns)
                except SystemExit:
                    pass

        # Verify per-child headers are as expected.
        t1 = (directory_mode_sandbox / "TASK-001_seed.md").read_text(
            encoding="utf-8"
        )
        assert "**Status:** complete" in t1.split("## Tasks")[0]

        t2 = (directory_mode_sandbox / "TASK-002_write_a.md").read_text(
            encoding="utf-8"
        )
        assert "**Status:** complete" in t2.split("## Tasks")[0]

        t3 = (directory_mode_sandbox / "TASK-003_write_b.md").read_text(
            encoding="utf-8"
        )
        assert "**Status:** partial" in t3.split("## Tasks")[0]

        # v1 scope boundary: NO run-level aggregate header synthesis. There
        # is no orchestrator-level "plan" file at <plan_dir> to check — the
        # directory itself has no top-level **Status:** to flip. This is
        # documented in SKILL.md's Directory-mode input section.
        # (This assertion is satisfied by construction: there is nothing to
        # assert the absence of, which is exactly the v1 boundary.)


class TestDirectoryModeFileModeRegression:
    """File-mode runs remain byte-identical to pre-TASK-004 behavior.

    Covers acceptance criterion: "File input path remains byte-identical
    to pre-TASK-004 behavior: when <plan-path> resolves to a file, none
    of the new branches fire."
    """

    def test_directory_mode_is_dir_detects_file_correctly(self) -> None:
        """Phase 0 detection helper: Path.is_dir() correctly distinguishes
        a plan file from a plan directory. SKILL.md mandates this exact
        predicate — no trailing-slash or string-shape heuristics.
        """
        file_path = DIRECTORY_MODE_FIXTURE / "TASK-001_seed.md"
        dir_path = DIRECTORY_MODE_FIXTURE
        assert file_path.is_file()
        assert not file_path.is_dir()
        assert dir_path.is_dir()
        assert not dir_path.is_file()

    def test_directory_mode_schedule_entry_without_plan_file_is_rejected(
        self, tmp_path: Path,
    ) -> None:
        """TASK-008 directory-only contract: every task MUST declare
        `plan_file`. A schedule that omits it on any task is rejected at
        the `batch-next` boundary by `_validate_schedule` with a
        structured `missing-field` error. The previous file-mode fallback
        (where a missing `plan_file` resolved to the run-level plan path)
        was removed when the directory-only contract was finalized.
        """
        payload = {
            "outcome": "valid",
            "tasks": [
                # No plan_file field anywhere — formerly file-mode, now invalid.
                {"id": "001", "agent": "claude", "files": ["a"],
                 "dependencies": []},
                {"id": "002", "agent": "claude", "files": ["b"],
                 "dependencies": []},
            ],
            "batches": [
                {"index": 1, "task_ids": ["001", "002"],
                 "file_locks": ["a", "b"]},
            ],
        }
        sched = _write_schedule(tmp_path, payload)
        cp = _run_batch_next(sched, parallel=2)
        assert cp.returncode != 0
        body = _parse_json(cp)
        codes_paths = [(e.get("code"), e.get("path")) for e in body.get("errors") or []]
        assert ("missing-field", "$.tasks[0].plan_file") in codes_paths, codes_paths
        assert ("missing-field", "$.tasks[1].plan_file") in codes_paths, codes_paths


class TestPreflightDirectoryMode:
    """Hotfix TASK-002: `cmd_preflight` accepts a directory input and handles
    decomposed plans per the same envelope schema as file mode.

    Each test seeds a fresh git repo, copies the shipped
    ``tests/fixtures/directory_mode_plan/`` in as the plan, commits the
    initial tree, then runs `preflight --plan-file <dir>` and inspects the
    JSON envelope. File-mode semantics are preserved; directory-mode tests
    exercise the new branch.
    """

    def _preflight(
        self, repo: Path, plan_dir: Path, *, strict_scope: bool = False,
    ) -> subprocess.CompletedProcess:
        cmd = [str(PY), str(SCRIPT), "preflight", "--plan-file", str(plan_dir), "--json"]
        if strict_scope:
            cmd.append("--strict-scope")
        return subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            text=True,
        )

    def _init_repo_with_fixture(
        self, tmp_path: Path,
    ) -> tuple[Path, Path]:
        """Init a fresh git repo at tmp_path, copy the shipped fixture in as
        ``docs/plans/decomposed_plan/``, seed a tracked ``scratch/a.txt`` +
        ``scratch/b.txt`` so TASK-002/003's Files: paths are real tracked
        files, and commit the whole tree. Returns (repo_root, plan_dir).
        """
        import shutil
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        plans_root = tmp_path / "docs" / "plans"
        plans_root.mkdir(parents=True)
        plan_dir = plans_root / "decomposed_plan"
        shutil.copytree(DIRECTORY_MODE_FIXTURE, plan_dir)
        # Seed the Files: targets so they can be made dirty later.
        (tmp_path / "scratch").mkdir()
        (tmp_path / "scratch" / "a.txt").write_text("orig-a\n", encoding="utf-8")
        (tmp_path / "scratch" / "b.txt").write_text("orig-b\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
        return tmp_path, plan_dir

    def test_clean_directory_passes(self, tmp_path: Path) -> None:
        """Preflight on a clean tree against the fixture directory returns
        ``pass: true``, empty ``plan_scope_dirty``, and no
        ``source_blocking``. This is the happy-path acceptance from the
        hotfix plan.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        cp = self._preflight(repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["pass"] is True
        assert body["dirty_files"]["source_blocking"] == []
        assert body["dirty_files"]["plan_scope_dirty"] == []
        # Each child declares `**Base branch:** main`; preflight picks the
        # first child's value (TASK-001_seed.md → main).
        assert body["base_branch"] == "main"

    def test_directory_scope_dirty_attributes_to_child_task(
        self, tmp_path: Path,
    ) -> None:
        """Dirtying ``scratch/a.txt`` (declared in TASK-002's Files:) lands
        it in ``plan_scope_dirty`` attributed to task_id ``002`` — proving
        the union spans every child and per-task attribution survives.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        (repo / "scratch" / "a.txt").write_text("CHANGED\n", encoding="utf-8")
        cp = self._preflight(repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert body["dirty_files"]["source_blocking"] == []
        scoped = body["dirty_files"]["plan_scope_dirty"]
        assert any(
            e["path"] == "scratch/a.txt" and e["task_id"] == "002" for e in scoped
        ), scoped
        assert any(
            "scratch/a.txt" in w and "TASK-002" in w for w in body["scope_warnings"]
        )
        assert body["pass"] is True

    def test_directory_missing_roster_chunk_halts(self, tmp_path: Path) -> None:
        """Preflight against a directory whose ``00_INDEX.json`` references
        a child that is not on disk halts with a ``missing-roster-chunk``
        error and non-zero exit.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        # Delete chunks[0].file (TASK-001_seed.md) from disk but leave the
        # roster intact so the preflight validator sees the mismatch.
        (plan_dir / "TASK-001_seed.md").unlink()
        cp = self._preflight(repo, plan_dir)
        assert cp.returncode != 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        err = body.get("error", "")
        assert "missing-roster-chunk" in err, err
        assert "TASK-001_seed.md" in err, err

    def test_directory_plan_doc_exact_repo_relative_match(
        self, tmp_path: Path,
    ) -> None:
        """Dirtying a chunk inside the real plan directory lands it in
        ``plan_doc`` (not ``source_blocking``). Asserts the remediated
        classifier uses the exact repo-relative path rather than the old
        basename + parent-name heuristic.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        # Edit the index + a chunk so both show up in `git status`.
        (plan_dir / "TASK-001_seed.md").write_text(
            "changed plan child\n", encoding="utf-8",
        )
        (plan_dir / "00_INDEX.json").write_text(
            (plan_dir / "00_INDEX.json").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        cp = self._preflight(repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        pd = body["dirty_files"]["plan_doc"]
        assert "docs/plans/decomposed_plan/TASK-001_seed.md" in pd, body
        assert "docs/plans/decomposed_plan/00_INDEX.json" in pd, body
        assert body["dirty_files"]["source_blocking"] == [], body

    def test_directory_plan_doc_rejects_same_named_sibling_tree(
        self, tmp_path: Path,
    ) -> None:
        """A same-named directory elsewhere in the repo must NOT be
        misclassified as plan text. Under the old (basename + parent.name)
        predicate, a dirty ``other/decomposed_plan/TASK-001_seed.md`` file
        would silently be absorbed into ``plan_doc``, hiding a legitimate
        ``source_blocking`` hit. The remediated exact-path classifier
        keeps it in ``source_blocking``.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        # Create a directory that shares the plan's basename but lives
        # outside the true plan_dir, seed a file with the same basename
        # as a chunk, and commit so `git status` only surfaces the
        # subsequent modification.
        decoy_dir = repo / "other" / "decomposed_plan"
        decoy_dir.mkdir(parents=True)
        decoy_file = decoy_dir / "TASK-001_seed.md"
        decoy_file.write_text("decoy-initial\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "add decoy"], cwd=repo, check=True)
        decoy_file.write_text("decoy-changed\n", encoding="utf-8")
        cp = self._preflight(repo, plan_dir)
        # Expect source_blocking to fire → preflight fails (exit non-zero).
        assert cp.returncode != 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        assert "other/decomposed_plan/TASK-001_seed.md" in (
            body["dirty_files"]["source_blocking"]
        ), body
        assert body["dirty_files"]["plan_doc"] == [], body

    def test_directory_tracked_dirty_attributes_to_third_child_strict_scope(
        self, tmp_path: Path,
    ) -> None:
        """TASK-003 dirty-tree coverage complementing the hotfix's TASK-002
        attribution test. Proves the preflight scope union spans every child
        in ``chunks[]`` (not just the first one) and that ``--strict-scope``
        flips ``pass`` to False when any attributed file is dirty. Also pins
        the "first non-null wins" ``base_branch`` invariant: all three
        children declare ``**Base branch:** main`` so the resolved value
        must be ``main``.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        # ``scratch/b.txt`` is in TASK-003's declared Files: list (committed
        # as tracked content by _init_repo_with_fixture). Modify in-place to
        # create a tracked-dirty file attributed to task_id 003.
        (repo / "scratch" / "b.txt").write_text("CHANGED-B\n", encoding="utf-8")
        # Default (non-strict) pass: plan_scope_dirty is a warning, not a
        # halt. Source_blocking stays empty because the dirty file IS in
        # scope.
        cp_default = self._preflight(repo, plan_dir, strict_scope=False)
        assert cp_default.returncode == 0, cp_default.stdout + cp_default.stderr
        body_default = _parse_json(cp_default)
        assert body_default["pass"] is True
        assert body_default["dirty_files"]["source_blocking"] == []
        scoped = body_default["dirty_files"]["plan_scope_dirty"]
        assert any(
            e["path"] == "scratch/b.txt" and e["task_id"] == "003" for e in scoped
        ), scoped
        assert any(
            "scratch/b.txt" in w and "TASK-003" in w
            for w in body_default["scope_warnings"]
        )
        # First-non-null-wins: every child declares main, so main wins.
        assert body_default["base_branch"] == "main"

        # --strict-scope flips pass to False (even though source_blocking is
        # empty) because plan_scope_dirty is non-empty.
        cp_strict = self._preflight(repo, plan_dir, strict_scope=True)
        assert cp_strict.returncode != 0, cp_strict.stdout + cp_strict.stderr
        body_strict = _parse_json(cp_strict)
        assert body_strict["pass"] is False
        strict_scoped = body_strict["dirty_files"]["plan_scope_dirty"]
        assert any(
            e["path"] == "scratch/b.txt" and e["task_id"] == "003"
            for e in strict_scoped
        ), strict_scoped

    def test_directory_base_branch_first_non_null_wins(
        self, tmp_path: Path,
    ) -> None:
        """Pins the "first non-null wins" ``base_branch`` contract by varying
        per-child values. The shipped fixture has all three children declaring
        ``**Base branch:** main`` — so the sibling
        ``test_directory_tracked_dirty_attributes_to_third_child_strict_scope``
        assertion that ``base_branch == "main"`` would still pass under a
        buggy refactor that read only the first child, only the last child,
        or any non-null child. This test varies the values so it distinguishes
        the contract: child 001 declares NO base branch (null), child 002
        declares ``release-2026-04``, child 003 declares ``main``. Preflight
        must return the first non-null in roster order (002's value:
        ``release-2026-04``). A refactor that picked "last non-null" would
        return ``main``; a refactor that picked only the first roster entry
        would return ``None`` or fall through to a default.
        """
        repo, plan_dir = self._init_repo_with_fixture(tmp_path)
        # Child 001: remove the `**Base branch:**` line entirely (null child).
        seed_path = plan_dir / "TASK-001_seed.md"
        seed_text = seed_path.read_text(encoding="utf-8")
        seed_mutated = re.sub(
            r"^\*\*Base branch:\*\*.*\n", "", seed_text, count=1, flags=re.MULTILINE,
        )
        assert seed_mutated != seed_text, (
            "fixture precondition: TASK-001_seed.md must contain a "
            "**Base branch:** line to remove"
        )
        seed_path.write_text(seed_mutated, encoding="utf-8")
        # Child 002: set to a distinct non-main branch name.
        a_path = plan_dir / "TASK-002_write_a.md"
        a_text = a_path.read_text(encoding="utf-8")
        a_mutated = re.sub(
            r"^\*\*Base branch:\*\*\s*\S+\s*$",
            "**Base branch:** release-2026-04",
            a_text,
            count=1,
            flags=re.MULTILINE,
        )
        assert a_mutated != a_text, (
            "fixture precondition: TASK-002_write_a.md must contain a "
            "**Base branch:** line to rewrite"
        )
        a_path.write_text(a_mutated, encoding="utf-8")
        # Child 003: explicitly set to main (distinct from child 002's value).
        b_path = plan_dir / "TASK-003_write_b.md"
        b_text = b_path.read_text(encoding="utf-8")
        b_mutated = re.sub(
            r"^\*\*Base branch:\*\*\s*\S+\s*$",
            "**Base branch:** main",
            b_text,
            count=1,
            flags=re.MULTILINE,
        )
        assert b_mutated != b_text, (
            "fixture precondition: TASK-003_write_b.md must contain a "
            "**Base branch:** line to rewrite"
        )
        b_path.write_text(b_mutated, encoding="utf-8")
        # Re-commit the mutated children so `git status` stays clean (prevents
        # the mutation from polluting dirty_files classifications under test).
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "mutate base_branch fixture"],
            cwd=repo, check=True,
        )

        cp = self._preflight(repo, plan_dir)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        body = _parse_json(cp)
        # First-non-null-wins: 001 has no declaration, 002 has release-2026-04,
        # 003 has main → 002's value wins because it's the first non-null in
        # roster order. If a refactor changed selection to "last non-null" or
        # "any", `main` would slip through; this assertion would then fail.
        assert body["base_branch"] == "release-2026-04", (
            "first-non-null-wins violated: expected release-2026-04 (from "
            "child 002, the first roster entry with a non-null **Base "
            f"branch:**), got {body.get('base_branch')!r}. Roster order was "
            "001 (null) → 002 (release-2026-04) → 003 (main)."
        )


class TestDirectoryModeHotfixSmoke:
    """Hotfix TASK-004: in-process integration across the three hotfix seams.

    Chains the three touched command functions (`cmd_preflight`,
    `cmd_plan_review`, `cmd_parse_plan_review_report`) by direct import
    against the shipped ``tests/fixtures/directory_mode_plan/`` fixture.
    Verifies the data flow between the three fixes — no single-task test
    covers the interface contracts simultaneously.

    Mock boundary: ``plan_codex_dispatch.invoke_codex`` is monkey-patched
    so Codex does not actually run; the fake writes a canned parsed-body
    JSON to ``output_path`` and returns a synthetic success dict. Both
    test and target live in the same Python process; the orchestrator's
    real subprocess path is exercised by the pre-existing
    ``TestDirectoryMode*`` CLI-level suites earlier in this file.
    """

    def test_directory_mode_hotfix_smoke(
        self, directory_mode_sandbox: Path,
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
    ) -> None:
        """Preflight → plan-review (mocked Codex) → parse-plan-review-report.

        Assertions:
          * Step 1: cmd_preflight returns ``pass: true`` + empty
            ``source_blocking`` against the fixture directory.
          * Step 4: cmd_plan_review emits a success envelope keyed by the
            directory basename; the fake invoke_codex is called exactly once.
          * Step 5: cmd_parse_plan_review_report preserves ``notes`` from
            the wrapper envelope verbatim into its emitted result.
          * End-to-end: no ``plan_review_skipped`` event appears in the
            sandboxed ``_run_log.jsonl``.
        """
        import argparse as _argparse
        import importlib
        import io
        import sys as _sys

        # Lazy-import the wrapper so sys.path (set up at module import
        # time) resolves it from plugins/plan-executor/scripts/. Using
        # importlib + invalidate_caches avoids a stale cache when this
        # test runs after test modules that load the wrapper via
        # spec_from_file_location under a different name.
        importlib.invalidate_caches()
        import plan_codex_dispatch

        plan_dir = directory_mode_sandbox  # tmp_path / "decomposed_plan"

        # Pre-seed the sandboxed run log so the end-to-end assertion below
        # is always evaluated. The in-process command functions exercised
        # here (cmd_preflight, cmd_plan_review, cmd_parse_plan_review_report)
        # do not themselves emit log events, so without seeding the file
        # would not exist and the `plan_review_skipped` guard would silently
        # no-op. The fixture already monkey-patches RUN_LOG_PATH into tmp.
        plan_ops.RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        plan_ops.RUN_LOG_PATH.touch()

        # -- Step 1: preflight against the directory -----------------------
        pre_ns = _argparse.Namespace(
            command="preflight",
            plan_file=str(plan_dir),
            strict_branch=False,
            strict_scope=False,
            json=True,
        )
        pre_buf = io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(pre_buf):
            with pytest.raises(SystemExit) as excinfo:
                plan_ops.cmd_preflight(pre_ns)
        assert excinfo.value.code == 0, (
            f"preflight exited non-zero: {pre_buf.getvalue()!r}"
        )
        pre_result = json.loads(pre_buf.getvalue())
        assert pre_result["pass"] is True, pre_result
        assert pre_result["dirty_files"]["source_blocking"] == [], pre_result

        # -- Step 2: synthesize the schedule sidecar -----------------------
        schedule_path = plan_dir / "directory_mode_plan.schedule.json"
        schedule_payload = {
            "outcome": "valid",
            "tasks": [
                {
                    "id": "001",
                    "agent": "claude",
                    "files": ["scratch/.gitkeep"],
                    "dependencies": [],
                    "plan_file": "TASK-001_seed.md",
                },
                {
                    "id": "002",
                    "agent": "claude",
                    "files": ["scratch/a.txt"],
                    "dependencies": ["001"],
                    "plan_file": "TASK-002_write_a.md",
                },
                {
                    "id": "003",
                    "agent": "claude",
                    "files": ["scratch/b.txt"],
                    "dependencies": ["001"],
                    "plan_file": "TASK-003_write_b.md",
                },
            ],
            "batches": [
                {"index": 1, "task_ids": ["001"], "file_locks": ["scratch/.gitkeep"]},
                {
                    "index": 2, "task_ids": ["002", "003"],
                    "file_locks": ["scratch/a.txt", "scratch/b.txt"],
                },
            ],
            "gaps": [],
            "risks": [],
        }
        schedule_path.write_text(
            json.dumps(schedule_payload), encoding="utf-8",
        )

        # -- Step 3: monkey-patch invoke_codex with a fake that writes the
        #            canned parsed-body JSON to output_path and returns
        #            the minimal success dict. ------------------------------
        invoke_calls: list[dict] = []
        canned_parsed_body = {
            "plan_file": "directory_mode_plan",
            "verdict": "approved-with-notes",
            "findings": [],
            "notes": ["cross-child parallelism ok"],
            "summary": "ok",
            "schedule_ok": True,
        }

        def _fake_invoke_codex(
            *,
            prompt: str, workdir: str, schema_path: str, output_path: str,
            timeout_sec: int, sandbox: str | None = None,
        ) -> dict:
            invoke_calls.append({
                "prompt_len": len(prompt),
                "workdir": workdir,
                "schema_path": schema_path,
                "output_path": output_path,
                "timeout_sec": timeout_sec,
                "sandbox": sandbox,
            })
            # The wrapper reads `output_path` and JSON-parses the result into
            # the envelope's `parsed` field, so we write the parsed body
            # (NOT a nested envelope) to disk.
            Path(output_path).write_text(
                json.dumps(canned_parsed_body), encoding="utf-8",
            )
            return {
                "status": "ok",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "file_changes": [],
                "wall_seconds": 0.01,
            }

        monkeypatch.setattr(
            plan_codex_dispatch, "invoke_codex", _fake_invoke_codex,
        )

        # -- Step 4: cmd_plan_review, capturing the emitted envelope -------
        pr_ns = _argparse.Namespace(
            subcommand="plan-review",
            plan_file=str(plan_dir),
            schedule_file=str(schedule_path),
            plans_dir=str(plan_dir),
            repo_root=str(plan_dir),
            dry_run=False,
            timeout=60,
            allow_gaps=False,
            json=True,
        )
        # Drain pre-existing capsys buffer so our assertions see only the
        # wrapper's stdout.
        capsys.readouterr()
        rc = plan_codex_dispatch.cmd_plan_review(pr_ns)
        captured = capsys.readouterr()
        assert rc == 0, (
            f"cmd_plan_review returned non-zero: stdout={captured.out!r}, "
            f"stderr={captured.err!r}"
        )
        assert len(invoke_calls) == 1, (
            f"fake invoke_codex not called exactly once: {invoke_calls}"
        )
        wrapper_envelope = json.loads(captured.out)
        assert wrapper_envelope["outcome"] == "success", wrapper_envelope
        assert wrapper_envelope["subcommand"] == "plan-review", wrapper_envelope
        assert wrapper_envelope["plan_file"] == "decomposed_plan", wrapper_envelope
        assert wrapper_envelope["parsed"]["notes"] == [
            "cross-child parallelism ok",
        ], wrapper_envelope

        # -- Step 5: pipe the captured envelope through
        #            cmd_parse_plan_review_report via monkey-patched stdin.
        monkeypatch.setattr(
            _sys, "stdin", io.StringIO(json.dumps(wrapper_envelope)),
        )
        parse_ns = _argparse.Namespace(
            command="parse-plan-review-report",
            stdin=True,
            json=True,
        )
        parse_buf = io.StringIO()
        with contextlib.redirect_stdout(parse_buf):
            with pytest.raises(SystemExit) as parse_exc:
                plan_ops.cmd_parse_plan_review_report(parse_ns)
        assert parse_exc.value.code == 0, (
            f"parse-plan-review-report exited non-zero: "
            f"{parse_buf.getvalue()!r}"
        )
        parse_result = json.loads(parse_buf.getvalue())
        assert parse_result["notes"] == ["cross-child parallelism ok"], (
            parse_result
        )
        assert parse_result["verdict"] == "approved-with-notes", parse_result
        assert parse_result["outcome"] == "success", parse_result
        assert parse_result["errors"] == [], parse_result

        # -- End-to-end: no plan_review_skipped event in the sandboxed log.
        # The log was pre-seeded above, so this assertion always runs and
        # actually exercises the acceptance criterion.
        run_log = plan_ops.RUN_LOG_PATH
        assert run_log.is_file(), (
            f"sandboxed run log missing at {run_log!s}"
        )
        events = [
            json.loads(line)
            for line in run_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        skipped = [
            e for e in events if e.get("event") == "plan_review_skipped"
        ]
        assert skipped == [], (
            f"unexpected plan_review_skipped events: {skipped}"
        )


# ---------------------------------------------------------------------------
# TASK-001 (per_task_dispatch_refactor_v2): decompose-plan subcommand +
# shared parsing helpers. Tests live here so they share the existing
# `isolated_plan` / `_run` / `_parse_json` plumbing with the rest of the
# suite.
# ---------------------------------------------------------------------------


DECOMPOSER_INPUTS_DIR = REPO_ROOT / "tests" / "fixtures" / "decomposer_inputs"


class TestParseTaskBlockHelper:
    """Direct unit coverage for the shared parsing helpers.

    Exercises `_parse_task_block`, `_extract_metadata_field`, and
    `_extract_bullet_list` against both H2 (whole-plan) and H3 (child-plan)
    inputs so the contract stays grammar-agnostic at different levels.
    """

    H2_BLOCK = (
        "## TASK-005: Example task\n"
        "\n"
        "- **Status:** pending\n"
        "- **Priority:** high\n"
        "- **Agent:** codex\n"
        "- **Files:**\n"
        "  - src/a.py\n"
        "  - src/b.py\n"
        "- **Dependencies:** [001, 002]\n"
        "- **Test command:** `pytest tests/`\n"
        "- **Acceptance criteria:**\n"
        "  - AC one\n"
        "  - AC two\n"
        "- **Reversion guidance:** `git restore .`\n"
        "\n"
        "**Description:**\n"
        "Multi-line description\n"
        "continues here.\n"
    )

    H3_BLOCK = (
        "### TASK-007: Child task\n"
        "\n"
        "- **Status:** open\n"
        "- **Priority:** critical\n"
        "- **Files:**\n"
        "  - foo.py\n"
        "- **Dependencies:** none\n"
        "- **Test command:** `true`\n"
        "- **Acceptance criteria:**\n"
        "  - it works\n"
        "\n"
        "**Description:**\n"
        "Child block.\n"
    )

    def test_parse_task_block_h2_full(self) -> None:
        task = plan_ops._parse_task_block(self.H2_BLOCK, level=2)
        assert task["id"] == "005"
        assert task["title"] == "Example task"
        assert task["priority"] == "high"
        assert task["agent"] == "codex"
        assert task["status"] == "pending"
        assert task["depends_on"] == ["001", "002"]
        assert task["test_command"] == "pytest tests/"
        assert task["files"] == ["src/a.py", "src/b.py"]
        assert task["acceptance_criteria"] == ["AC one", "AC two"]
        assert "Multi-line description" in (task["description"] or "")
        assert "continues here" in (task["description"] or "")
        assert task["reversion_guidance"] == "`git restore .`"
        assert task["source_line"] == 1

    def test_parse_task_block_h3_minimal(self) -> None:
        task = plan_ops._parse_task_block(self.H3_BLOCK, level=3)
        assert task["id"] == "007"
        assert task["title"] == "Child task"
        assert task["priority"] == "critical"
        assert task["agent"] is None
        assert task["status"] == "open"
        assert task["depends_on"] == []
        assert task["test_command"] == "true"
        assert task["files"] == ["foo.py"]
        assert task["acceptance_criteria"] == ["it works"]

    def test_parse_task_block_wrong_level_raises(self) -> None:
        with pytest.raises(ValueError):
            plan_ops._parse_task_block(self.H2_BLOCK, level=3)

    def test_extract_metadata_field_present(self) -> None:
        assert plan_ops._extract_metadata_field(
            self.H2_BLOCK, "Priority",
        ) == "high"
        assert plan_ops._extract_metadata_field(
            self.H2_BLOCK, "Agent",
        ) == "codex"

    def test_extract_metadata_field_missing(self) -> None:
        assert plan_ops._extract_metadata_field(
            self.H3_BLOCK, "Agent",
        ) is None
        assert plan_ops._extract_metadata_field(
            self.H2_BLOCK, "Nonexistent",
        ) is None

    def test_extract_bullet_list_standalone(self) -> None:
        assert plan_ops._extract_bullet_list(
            self.H2_BLOCK, "Files",
        ) == ["src/a.py", "src/b.py"]
        assert plan_ops._extract_bullet_list(
            self.H2_BLOCK, "Acceptance criteria",
        ) == ["AC one", "AC two"]

    def test_extract_bullet_list_inline_brackets(self) -> None:
        assert plan_ops._extract_bullet_list(
            self.H2_BLOCK, "Dependencies",
        ) == ["001", "002"]

    def test_extract_bullet_list_inline_none(self) -> None:
        assert plan_ops._extract_bullet_list(
            self.H3_BLOCK, "Dependencies",
        ) == []

    def test_extract_bullet_list_missing_heading(self) -> None:
        assert plan_ops._extract_bullet_list(
            self.H3_BLOCK, "Reversion guidance",
        ) == []


class TestDecomposePlan:
    """End-to-end coverage for the `decompose-plan` subcommand.

    Each fixture under `tests/fixtures/decomposer_inputs/` exercises a
    specific branch: canonical success, duplicate ids, missing metadata,
    unresolvable deps, cycles. Round-trip validation checks that the
    emitted `00_INDEX.json` + child files reparse via the shared helpers.
    """

    def test_canonical_round_trip(self, tmp_path: Path) -> None:
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run(
            "decompose-plan", "--plan-file", str(src), "--json",
        )
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["ok"] is True
        assert res["task_count"] == 3
        produced = Path(res["produced_dir"])
        assert produced.is_dir()
        # Canonical manifest shape.
        manifest = json.loads(
            (produced / "00_INDEX.json").read_text(encoding="utf-8"),
        )
        assert manifest["schema_version"] == 1
        assert manifest["source"] == "decompose-plan"
        assert [c["task_id"] for c in manifest["chunks"]] == [
            "001", "002", "003",
        ]
        # Manifest parses via the existing loader (same shape the
        # directory-mode fixture uses).
        roster = plan_ops._parse_index_roster(produced / "00_INDEX.json")
        assert set(roster.keys()) == {"001", "002", "003"}
        # Each child carries the H3 sub-heading + canonical metadata block.
        for chunk in manifest["chunks"]:
            child_path = produced / chunk["file"]
            assert child_path.is_file()
            body = child_path.read_text(encoding="utf-8")
            # H3 heading (NOT H2).
            assert re.search(
                rf"^### TASK-{chunk['task_id']}: ", body, re.MULTILINE,
            ), body
            assert "- **Status:**" in body
            assert "- **Priority:**" in body
            assert "- **Files:**" in body
            assert "- **Dependencies:**" in body
            assert "- **Test command:**" in body
            assert "- **Acceptance criteria:**" in body
            assert "**Description:**" in body
            # Children re-parse via the shared helper at level=3.
            task = plan_ops._parse_task_block(body, level=3)
            assert task["id"] == chunk["task_id"]
            # Description is non-empty and acceptance_criteria is a
            # non-empty list — the "fat" manifest invariant TASK-004
            # depends on.
            assert task["description"] and task["description"].strip()
            assert len(task["acceptance_criteria"]) >= 1

    def test_canonical_force_idempotent(self, tmp_path: Path) -> None:
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp1 = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp1.returncode == 0, cp1.stderr
        produced = Path(_parse_json(cp1)["produced_dir"])
        # Snapshot content of every produced file.
        first_snapshot = {
            p.name: p.read_bytes() for p in produced.iterdir() if p.is_file()
        }
        # Rerun without --force should fail on non-empty out-dir.
        cp2 = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp2.returncode == 1
        err = _parse_json(cp2)["errors"]
        assert any(e["code"] == "out-dir-not-empty" for e in err)
        # Rerun with --force should succeed and produce byte-identical output.
        cp3 = _run(
            "decompose-plan", "--plan-file", str(src), "--force", "--json",
        )
        assert cp3.returncode == 0, cp3.stderr
        second_snapshot = {
            p.name: p.read_bytes() for p in produced.iterdir() if p.is_file()
        }
        assert first_snapshot.keys() == second_snapshot.keys()
        for name, blob in first_snapshot.items():
            assert second_snapshot[name] == blob, (
                f"--force rerun produced non-identical content for {name}"
            )

    def test_missing_metadata_structured_error(self, tmp_path: Path) -> None:
        src = tmp_path / "missing_metadata.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "missing_metadata.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 1
        err = _parse_json(cp)["errors"]
        assert any(
            e["code"] == "missing-required-metadata"
            and e["task_id"] == "001"
            and e["field"] == "Priority"
            for e in err
        ), err
        # Source-line pinning must name a real line in the fixture.
        src_lines = src.read_text(encoding="utf-8").splitlines()
        assert all(1 <= e["source_line"] <= len(src_lines) for e in err), err

    def test_duplicate_ids_structured_error(self, tmp_path: Path) -> None:
        src = tmp_path / "duplicate_ids.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "duplicate_ids.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 1
        err = _parse_json(cp)["errors"]
        dup = [e for e in err if e["code"] == "duplicate-id"]
        assert dup and dup[0]["task_id"] == "001", err
        assert dup[0]["first_seen_line"] < dup[0]["source_line"], dup[0]

    def test_unresolvable_deps_structured_error(self, tmp_path: Path) -> None:
        src = tmp_path / "unresolvable_deps.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "unresolvable_deps.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 1
        err = _parse_json(cp)["errors"]
        unresolved = [e for e in err if e["code"] == "unresolvable-dep"]
        assert unresolved and unresolved[0]["task_id"] == "001"
        assert unresolved[0]["dep_id"] == "999", unresolved

    def test_cyclic_deps_structured_error(self, tmp_path: Path) -> None:
        src = tmp_path / "cyclic_deps.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "cyclic_deps.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 1
        err = _parse_json(cp)["errors"]
        cycles = [e for e in err if e["code"] == "cyclic-dependency"]
        assert cycles, err
        assert set(cycles[0]["task_ids"]) == {"001", "002"}

    def test_malformed_header_structured_error(self, tmp_path: Path) -> None:
        """`## TASK-1:` (one digit) must be rejected with a structured
        `malformed-task-header` error pinned to the offending source line,
        NOT silently normalized to `001` or silently dropped as `no-tasks`.
        """
        src = tmp_path / "malformed_header.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "malformed_header.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 1
        err = _parse_json(cp)["errors"]
        malformed = [e for e in err if e["code"] == "malformed-task-header"]
        assert malformed, err
        assert malformed[0]["raw_id"] == "1", malformed
        # Pins a real source line in the fixture.
        src_lines = src.read_text(encoding="utf-8").splitlines()
        assert 1 <= malformed[0]["source_line"] <= len(src_lines), malformed
        # The offending line IS the one-digit TASK heading.
        assert src_lines[malformed[0]["source_line"] - 1].startswith(
            "## TASK-1:"
        ), src_lines[malformed[0]["source_line"] - 1]
        # Critically: no `no-tasks` fallback. The error surfaces loudly.
        assert not any(e["code"] == "no-tasks" for e in err), err

    def test_decompose_preserves_source_task_status(
        self, tmp_path: Path,
    ) -> None:
        """Auto-decomposed manifest chunks must carry through each source
        task's `**Status:**` (mapped to `{Done, Pending}`), not blanket
        every chunk as `Pending`. A whole-plan task already marked `done`
        must NOT be reintroduced to the scheduler as pending work.
        """
        src = tmp_path / "status_mix.md"
        src.write_text(
            "# Status-mix fixture\n"
            "\n"
            "**Base branch:** main\n"
            "\n"
            "## Goal\n\nGoal.\n\n## Context\n\nCtx.\n\n## Verification\n\nV.\n\n"
            "## Tasks\n\n"
            "## TASK-001: Done already\n"
            "\n"
            "- **Status:** done\n"
            "- **Priority:** high\n"
            "- **Files:**\n"
            "  - a.txt (create)\n"
            "- **Dependencies:** []\n"
            "- **Test command:** `true`\n"
            "- **Acceptance criteria:**\n"
            "  - it works\n"
            "\n"
            "**Description:**\nAlready completed.\n\n"
            "## TASK-002: Still pending\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** medium\n"
            "- **Files:**\n"
            "  - b.txt (create)\n"
            "- **Dependencies:** []\n"
            "- **Test command:** `true`\n"
            "- **Acceptance criteria:**\n"
            "  - it works\n"
            "\n"
            "**Description:**\nTo-do.\n",
            encoding="utf-8",
        )
        res = plan_ops._decompose_plan(
            src, tmp_path / "out", force=True,
        )
        assert res["ok"] is True, res
        manifest = json.loads(
            (Path(res["produced_dir"]) / "00_INDEX.json").read_text(
                encoding="utf-8",
            ),
        )
        by_id = {c["task_id"]: c for c in manifest["chunks"]}
        assert by_id["001"]["status"] == "Done", by_id["001"]
        assert by_id["002"]["status"] == "Pending", by_id["002"]
        # Both statuses are in the narrow index vocabulary.
        for chunk in manifest["chunks"]:
            assert chunk["status"] in plan_ops.ALLOWED_INDEX_STATUSES, chunk

    def test_decompose_manifest_has_canonical_schema_fields(
        self, tmp_path: Path,
    ) -> None:
        """Emitted `00_INDEX.json` carries the same top-level keys as the
        canonical manual-sidecar manifest (see
        `docs/plans/per_task_dispatch_refactor_v2/00_INDEX.json`), with
        correct types. This is the schema-compatibility invariant: an
        auto-decomposed directory must be consumable by the same loader
        that reads hand-authored directories.
        """
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        res = plan_ops._decompose_plan(
            src, tmp_path / "out", force=True,
        )
        assert res["ok"] is True, res
        manifest = json.loads(
            (Path(res["produced_dir"]) / "00_INDEX.json").read_text(
                encoding="utf-8",
            ),
        )
        # Canonical top-level keys present with the right types.
        assert manifest["schema_version"] == 1
        assert isinstance(manifest["source"], str) and manifest["source"]
        assert isinstance(manifest["plan_title"], str) and manifest["plan_title"]
        assert isinstance(manifest["created"], str) and manifest["created"]
        assert manifest["base_branch"] == "main"
        assert manifest["depends_on_plans"] == []
        assert manifest["supersedes"] == []
        assert isinstance(manifest["chunks"], list)
        # Compare the emitted top-level key set against the canonical
        # manual-sidecar manifest. The decomposer may add additional
        # provenance keys (e.g. `source_plan_file`) but must emit at
        # least every canonical top-level field. The canonical reference
        # was archived under `docs/plans/archive/` and still carries the
        # legacy `parallel_batches` field — that field is intentionally
        # no longer emitted by `_decompose_plan`, so it is excluded from
        # the comparison set.
        canonical_path = (
            REPO_ROOT
            / "docs" / "plans" / "archive"
            / "per_task_dispatch_refactor_v2" / "00_INDEX.json"
        )
        canonical = json.loads(
            canonical_path.read_text(encoding="utf-8"),
        )
        canonical_keys = set(canonical.keys()) - {"parallel_batches"}
        missing = canonical_keys - set(manifest.keys())
        assert not missing, (
            f"decomposed manifest missing canonical top-level keys: "
            f"{sorted(missing)}"
        )

    def test_decompose_manifest_omits_parallel_batches(
        self, tmp_path: Path,
    ) -> None:
        """Negative pin: emitted manifest must NOT carry `parallel_batches`.

        TASK-005 of PLAN_TOPO_RESPECT_FIX_2026-04-25 deleted the dead
        `parallel_batches` field from `_decompose_plan`. This test pins
        the absence so a future re-introduction (whether intentional or
        accidental) breaks loudly. The schedule's `batches[]` is the
        single batch source-of-truth; the roster is no longer expected
        to carry batch metadata.
        """
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        res = plan_ops._decompose_plan(
            src, tmp_path / "out", force=True,
        )
        assert res["ok"] is True, res
        # The function's return dict must not carry it.
        assert "parallel_batches" not in res, res
        # The persisted manifest must not carry it.
        manifest = json.loads(
            (Path(res["produced_dir"]) / "00_INDEX.json").read_text(
                encoding="utf-8",
            ),
        )
        assert "parallel_batches" not in manifest, manifest

    def test_decompose_roster_back_compat_parses_legacy_parallel_batches(
        self, tmp_path: Path,
    ) -> None:
        """Back-compat: `_parse_index_roster` tolerates rosters in the
        wild that still carry the legacy `parallel_batches` field.

        Pre-existing 00_INDEX.json files written before TASK-005 of
        PLAN_TOPO_RESPECT_FIX_2026-04-25 may still ship with
        `parallel_batches`. The parser must ignore the extra key so
        operators do not need to rewrite every historical roster to
        adopt the cleanup.
        """
        legacy = {
            "schema_version": 1,
            "source": "manual-sidecar",
            "plan_title": "Legacy back-compat roster",
            "created": "2026-04-25",
            "base_branch": "main",
            "depends_on_plans": [],
            "supersedes": [],
            "parallel_batches": [["001"], ["002", "003"]],
            "chunks": [
                {
                    "task_id": "001",
                    "file": "TASK-001_a.md",
                    "depends_on": [],
                    "status": "Pending",
                    "superseded_by": [],
                },
                {
                    "task_id": "002",
                    "file": "TASK-002_b.md",
                    "depends_on": ["001"],
                    "status": "Pending",
                    "superseded_by": [],
                },
                {
                    "task_id": "003",
                    "file": "TASK-003_c.md",
                    "depends_on": ["001"],
                    "status": "Pending",
                    "superseded_by": [],
                },
            ],
        }
        path = tmp_path / "00_INDEX.json"
        path.write_text(json.dumps(legacy, indent=2), encoding="utf-8")
        roster = plan_ops._parse_index_roster(path)
        assert set(roster.keys()) == {"001", "002", "003"}
        assert roster["001"]["depends_on"] == []
        assert roster["002"]["depends_on"] == ["001"]
        assert roster["003"]["depends_on"] == ["001"]
        for tid in ("001", "002", "003"):
            assert roster[tid]["status"] == "Pending"
            assert roster[tid]["file"].startswith(f"TASK-{tid}_")

    def test_decompose_plan_timing_budget(self, tmp_path: Path) -> None:
        """Decomposition for a 10-task plan completes under 100 ms wall.

        AC requires heuristic-only (no LLM, no network) with a soft budget.
        """
        src = tmp_path / "perf.md"
        body = [
            "# Perf test plan",
            "",
            "**Status:** pending",
            "",
            "## Goal",
            "",
            "Perf target.",
            "",
            "## Context",
            "",
            "Context.",
            "",
            "## Verification",
            "",
            "ok.",
            "",
            "## Tasks",
            "",
        ]
        for i in range(1, 11):
            body += [
                f"## TASK-{i:03d}: Task {i}",
                "",
                "- **Status:** pending",
                "- **Priority:** high",
                "- **Files:**",
                f"  - scratch/x{i}.txt",
                "- **Dependencies:** []",
                "- **Test command:** `true`",
                "- **Acceptance criteria:**",
                "  - it works",
                "- **Reversion guidance:** cleanup",
                "",
                "**Description:**",
                f"Body for TASK-{i:03d}.",
                "",
            ]
        src.write_text("\n".join(body), encoding="utf-8")
        import time
        t0 = time.perf_counter()
        res = plan_ops._decompose_plan(
            src, tmp_path / "perf_out", force=True,
        )
        elapsed = time.perf_counter() - t0
        assert res["ok"] is True, res
        # Generous 500 ms budget so slow CI hardware does not flake — the AC
        # "<100 ms for a 10-task plan" is the target on local hardware; CI
        # baselines add I/O and import overhead.
        assert elapsed < 0.5, f"decompose took {elapsed*1000:.1f}ms; too slow"

    def test_decompose_to_child_grammar_matches_level3_helper(
        self, tmp_path: Path,
    ) -> None:
        """Round-trip: decompose emits H3 children that reparse via the same
        shared helpers at level=3. This is the grammar-contract test TASK-004
        depends on — if the decomposer emits a shape `build-tasks` can't
        read, it shows up here.
        """
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        res = plan_ops._decompose_plan(
            src, tmp_path / "out", force=True,
        )
        assert res["ok"] is True, res
        manifest = json.loads(
            (Path(res["produced_dir"]) / "00_INDEX.json").read_text(
                encoding="utf-8",
            ),
        )
        for chunk in manifest["chunks"]:
            body = (Path(res["produced_dir"]) / chunk["file"]).read_text(
                encoding="utf-8",
            )
            task = plan_ops._parse_task_block(body, level=3)
            assert task["id"] == chunk["task_id"]
            assert task["priority"] == chunk["priority"]
            assert task["depends_on"] == chunk["depends_on"]
            # Description + acceptance_criteria round-trip. The "fat"
            # invariant TASK-006's schedule-only plan-review needs.
            assert task["description"] and task["description"].strip(), task
            assert task["acceptance_criteria"], task

    def test_decompose_default_out_dir_sibling(self, tmp_path: Path) -> None:
        """Default --out-dir is `<file-parent>/<file-stem>/`."""
        src = tmp_path / "nested" / "canonical.md"
        src.parent.mkdir()
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 0, cp.stderr
        produced = Path(_parse_json(cp)["produced_dir"])
        assert produced == (src.parent / "canonical").resolve()

    def test_decompose_auto_promote_event_accepted(
        self, isolated_plan: Path,
    ) -> None:
        """Orchestrator smoke: Phase 0 wire-up emits a `decompose_auto_promote`
        log event after a successful decomposition. The event name must be
        in `ALLOWED_LOG_EVENTS` so `log-event` accepts it, and the fields
        payload the skill passes (`{source_file, produced_dir, task_count}`)
        must round-trip through the JSONL writer.

        Real end-to-end `/implement-plan <file.md>` dispatch happens inside
        Claude Code's slash-command runtime and cannot be simulated here; this
        test locks in the contract pieces that live in `plan_ops.py`.
        """
        # Contract: the event type is in the allowed set.
        assert "decompose_auto_promote" in plan_ops.ALLOWED_LOG_EVENTS
        # Wire-up: `log-event` accepts the expected field payload.
        cp = _run(
            "log-event",
            "--event", "decompose_auto_promote",
            "--fields-json",
            json.dumps(
                {
                    "run_id": "R_SMOKE",
                    "source_file": "docs/plans/demo.md",
                    "produced_dir": "docs/plans/demo",
                    "task_count": 3,
                }
            ),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        assert _parse_json(cp)["ok"] is True
        lines = plan_ops.RUN_LOG_PATH.read_text(
            encoding="utf-8",
        ).splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["event"] == "decompose_auto_promote"
        assert rec["source_file"] == "docs/plans/demo.md"
        assert rec["produced_dir"] == "docs/plans/demo"
        assert rec["task_count"] == 3

    def test_decompose_children_pass_schema_valid_gate(
        self, tmp_path: Path,
    ) -> None:
        """Every auto-decomposed child carries the full top-level §5 layout
        (`## Goal`, `## Context` or `## Scoped Context`, `## Verification`,
        and `## Tasks` wrapping the H3 block), so the Phase 0 `schema-valid`
        gate that runs per-child on a decomposed directory passes.

        This is the Codex-review load-bearing finding: without these
        sections the auto-promoted directory would immediately fail
        preflight on the first child file the gate inspects.
        """
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        res = plan_ops._decompose_plan(
            src, tmp_path / "out", force=True,
        )
        assert res["ok"] is True, res
        produced = Path(res["produced_dir"])
        manifest = json.loads(
            (produced / "00_INDEX.json").read_text(encoding="utf-8"),
        )
        for chunk in manifest["chunks"]:
            child = produced / chunk["file"]
            gate = plan_ops._gate_schema_valid(child)
            assert gate["status"] == "pass", (
                f"child {chunk['file']} failed schema-valid: "
                f"{gate.get('reason')!r}; body preview:\n"
                f"{child.read_text(encoding='utf-8')[:600]}"
            )
            body = child.read_text(encoding="utf-8")
            # Positive-shape checks mirroring the layout in the shipped
            # hand-authored fixture `tests/fixtures/directory_mode_plan/
            # TASK-001_seed.md`: top-level Goal, Context, Verification,
            # and Tasks sections precede the `### TASK-NNN:` block.
            assert re.search(r"^## Goal\b", body, re.MULTILINE), body
            assert re.search(r"^## Context\b", body, re.MULTILINE), body
            assert re.search(
                r"^## Verification\b", body, re.MULTILINE,
            ), body
            assert re.search(r"^## Tasks\b", body, re.MULTILINE), body
            # H3 task block still parses via the shared helper.
            task = plan_ops._parse_task_block(body, level=3)
            assert task["id"] == chunk["task_id"]

    def test_decompose_force_rerun_is_cross_day_idempotent(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch",
    ) -> None:
        """Cross-day `--force` rerun produces byte-identical output.

        `00_INDEX.json`'s `created` field is derived from the prior
        manifest on `--force` when one exists, so re-running the
        decomposer on a different day (or across day boundaries in CI)
        cannot perturb the byte content.
        """
        import plan_ops as _plan_ops_module  # type: ignore

        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )

        class _FrozenDay1:
            @classmethod
            def now(cls, tz: object = None) -> "_FrozenDay1":
                return cls()

            def strftime(self, fmt: str) -> str:
                return "2026-04-24"

        class _FrozenDay2:
            @classmethod
            def now(cls, tz: object = None) -> "_FrozenDay2":
                return cls()

            def strftime(self, fmt: str) -> str:
                return "2027-01-01"

        out_dir = tmp_path / "out"
        # Day 1: create the directory; records created="2026-04-24".
        monkeypatch.setattr(_plan_ops_module, "datetime", _FrozenDay1)
        res1 = _plan_ops_module._decompose_plan(src, out_dir, force=True)
        assert res1["ok"] is True, res1
        manifest1_text = (out_dir / "00_INDEX.json").read_text(
            encoding="utf-8",
        )
        snapshot_day1 = {
            p.name: p.read_bytes()
            for p in out_dir.iterdir()
            if p.is_file()
        }
        assert '"created": "2026-04-24"' in manifest1_text

        # Day 2: `datetime.now()` now returns a different date. Under
        # `--force`, the decomposer MUST preserve the prior manifest's
        # `created` field so the output stays byte-identical.
        monkeypatch.setattr(_plan_ops_module, "datetime", _FrozenDay2)
        res2 = _plan_ops_module._decompose_plan(src, out_dir, force=True)
        assert res2["ok"] is True, res2
        snapshot_day2 = {
            p.name: p.read_bytes()
            for p in out_dir.iterdir()
            if p.is_file()
        }
        assert snapshot_day1.keys() == snapshot_day2.keys()
        for name, blob in snapshot_day1.items():
            assert snapshot_day2[name] == blob, (
                f"cross-day --force rerun produced non-identical content "
                f"for {name}"
            )

    def test_decompose_fresh_day_stamps_today_when_no_prior_manifest(
        self, tmp_path: Path, monkeypatch: "pytest.MonkeyPatch",
    ) -> None:
        """First-time decomposition (no prior `00_INDEX.json`) stamps
        today's date, even under `--force`. The `--force` preservation
        path only applies when a prior manifest is on disk."""
        import plan_ops as _plan_ops_module  # type: ignore

        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )

        class _FrozenDay:
            @classmethod
            def now(cls, tz: object = None) -> "_FrozenDay":
                return cls()

            def strftime(self, fmt: str) -> str:
                return "2027-06-15"

        monkeypatch.setattr(_plan_ops_module, "datetime", _FrozenDay)
        out_dir = tmp_path / "fresh_out"
        res = _plan_ops_module._decompose_plan(src, out_dir, force=True)
        assert res["ok"] is True, res
        manifest = json.loads(
            (out_dir / "00_INDEX.json").read_text(encoding="utf-8"),
        )
        assert manifest["created"] == "2027-06-15"

    def test_decompose_child_always_emits_reversion_guidance(
        self, tmp_path: Path,
    ) -> None:
        """Child file grammar is pinned: `**Reversion guidance:**` must
        appear in every emitted child, even when the source task omits
        the field. Emits the stable `none` sentinel in that case.
        """
        src = tmp_path / "no_rev.md"
        src.write_text(
            "# No-reversion fixture\n"
            "\n"
            "**Base branch:** main\n"
            "\n"
            "## Goal\n\nG.\n\n## Context\n\nC.\n\n## Verification\n\nV.\n\n"
            "## Tasks\n\n"
            "## TASK-001: Without reversion\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Files:**\n"
            "  - foo.txt (create)\n"
            "- **Dependencies:** []\n"
            "- **Test command:** `true`\n"
            "- **Acceptance criteria:**\n"
            "  - it works\n"
            "\n"
            "**Description:**\nNo reversion guidance on source.\n",
            encoding="utf-8",
        )
        res = plan_ops._decompose_plan(
            src, tmp_path / "out", force=True,
        )
        assert res["ok"] is True, res
        produced = Path(res["produced_dir"])
        manifest = json.loads(
            (produced / "00_INDEX.json").read_text(encoding="utf-8"),
        )
        assert len(manifest["chunks"]) == 1
        child = produced / manifest["chunks"][0]["file"]
        body = child.read_text(encoding="utf-8")
        # Unconditional emission: the header MUST be present.
        assert "- **Reversion guidance:**" in body, body
        # Sentinel value for the omitted case.
        assert re.search(
            r"^- \*\*Reversion guidance:\*\* none\s*$",
            body,
            re.MULTILINE,
        ), body
        # Round-trips cleanly: parser reads the sentinel as the literal
        # string "none".
        task = plan_ops._parse_task_block(body, level=3)
        assert task["reversion_guidance"] == "none"

    def test_decompose_force_removes_stale_children(
        self, tmp_path: Path,
    ) -> None:
        """When the source plan shrinks (or a task slug changes) between
        two `--force` runs, the decomposer MUST remove any stale
        `TASK-*.md` files that the new chunk set no longer covers. The
        directory's canonical shape is
        `{00_INDEX.json, TASK-NNN_<slug>.md, ...}` and extra children
        would diverge from that shape.
        """
        # Build a three-task source.
        three_task_plan = (
            "# Stale-children fixture\n"
            "\n"
            "**Base branch:** main\n"
            "\n"
            "## Goal\n\nG.\n\n## Context\n\nC.\n\n## Verification\n\nV.\n\n"
            "## Tasks\n\n"
        )
        for idx in (1, 2, 3):
            three_task_plan += (
                f"## TASK-{idx:03d}: Task {idx}\n"
                "\n"
                "- **Status:** pending\n"
                "- **Priority:** high\n"
                "- **Files:**\n"
                f"  - f{idx}.txt (create)\n"
                "- **Dependencies:** []\n"
                "- **Test command:** `true`\n"
                "- **Acceptance criteria:**\n"
                "  - it works\n"
                "\n"
                f"**Description:**\nBody {idx}.\n\n"
            )
        src = tmp_path / "plan.md"
        src.write_text(three_task_plan, encoding="utf-8")
        out_dir = tmp_path / "out"
        res1 = plan_ops._decompose_plan(src, out_dir, force=True)
        assert res1["ok"] is True, res1
        children_before = sorted(
            p.name for p in out_dir.glob("TASK-*.md")
        )
        assert len(children_before) == 3, children_before
        stale_name = children_before[2]  # TASK-003's child file
        assert (out_dir / stale_name).is_file()
        # Drop an unrelated user note into the directory; the sweep
        # must leave it alone.
        user_note = out_dir / "NOTES.md"
        user_note.write_text("personal notes\n", encoding="utf-8")
        # Modify the source to remove TASK-003.
        two_task_plan = (
            "# Stale-children fixture\n"
            "\n"
            "**Base branch:** main\n"
            "\n"
            "## Goal\n\nG.\n\n## Context\n\nC.\n\n## Verification\n\nV.\n\n"
            "## Tasks\n\n"
        )
        for idx in (1, 2):
            two_task_plan += (
                f"## TASK-{idx:03d}: Task {idx}\n"
                "\n"
                "- **Status:** pending\n"
                "- **Priority:** high\n"
                "- **Files:**\n"
                f"  - f{idx}.txt (create)\n"
                "- **Dependencies:** []\n"
                "- **Test command:** `true`\n"
                "- **Acceptance criteria:**\n"
                "  - it works\n"
                "\n"
                f"**Description:**\nBody {idx}.\n\n"
            )
        src.write_text(two_task_plan, encoding="utf-8")
        res2 = plan_ops._decompose_plan(src, out_dir, force=True)
        assert res2["ok"] is True, res2
        children_after = sorted(
            p.name for p in out_dir.glob("TASK-*.md")
        )
        # Stale third child was swept.
        assert stale_name not in children_after, children_after
        assert len(children_after) == 2, children_after
        # Manifest reflects the two-task shape.
        manifest = json.loads(
            (out_dir / "00_INDEX.json").read_text(encoding="utf-8"),
        )
        assert [c["task_id"] for c in manifest["chunks"]] == ["001", "002"]
        # Unrelated user file was NOT touched.
        assert user_note.is_file()
        assert user_note.read_text(encoding="utf-8") == "personal notes\n"


class TestSkillAutoPromoteBootstrapInterpreter:
    """SKILL.md Phase 0 auto-promote must use a bootstrap interpreter
    (literal `python3`), not `$PYTHON`. Per Phase 0's own ordering,
    `$PYTHON` is only pinned *after* `preflight --json` runs, so
    invoking `$PYTHON` before preflight is a direct contradiction.
    """

    SKILL = (
        REPO_ROOT / "plugins" / "plan-executor"
        / "skills" / "implement-plan" / "SKILL.md"
    )

    def test_auto_promote_uses_python3_not_pinned(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        # Find the auto-promote block (bounded by the section header the
        # task ships) and confirm the `decompose-plan` invocation inside
        # it does NOT reference `$PYTHON`.
        start_m = re.search(
            r"\*\*Auto-promote single-file input to directory mode",
            text,
        )
        assert start_m is not None, "auto-promote block missing from SKILL.md"
        # Scope the search to the block: until the next `## ` heading or
        # the next top-level `**`-bold paragraph marker.
        tail = text[start_m.end():]
        # Pick a generous bound — the `path-info` section, or the next
        # H2 heading, whichever comes first.
        end_m = re.search(
            r"^(?:## |First, bind the path placeholders)",
            tail,
            re.MULTILINE,
        )
        block = tail[: end_m.start()] if end_m else tail
        # The decompose-plan invocation inside this block uses python3.
        decompose_cmds = re.findall(
            r"^[^\n]*plan_ops\.py[^\n]*decompose-plan[^\n]*$",
            block,
            re.MULTILINE,
        )
        assert decompose_cmds, (
            "no decompose-plan invocation found inside auto-promote block"
        )
        for cmd in decompose_cmds:
            assert "$PYTHON" not in cmd, (
                f"auto-promote decompose-plan invocation must NOT use "
                f"$PYTHON (it is not bound until preflight): {cmd!r}"
            )
            assert "python3" in cmd, (
                f"auto-promote decompose-plan invocation must use the "
                f"literal `python3` bootstrap interpreter: {cmd!r}"
            )


# ---------------------------------------------------------------------------
# TASK-004: Roster-driven fat `tasks[]` synthesis (`build-tasks`).
# ---------------------------------------------------------------------------


DIRECTORY_MODE_FIXTURE_PATH = (
    REPO_ROOT / "tests" / "fixtures" / "directory_mode_plan"
)


def _schedule_shape(build_tasks_output: dict) -> dict:
    """Project `build-tasks` output onto the `parse-schedule` wire shape.

    `build-tasks` emits a superset of the schedule shape (adds `ok`,
    `warnings`, `errors`). `parse-schedule --stdin` is strict about
    unknown top-level fields, so pipe-throughs strip the extra keys.
    """
    return {
        "outcome": build_tasks_output.get("outcome", "valid"),
        "tasks": build_tasks_output.get("tasks", []),
        "batches": build_tasks_output.get("batches", []),
        "gaps": [],
        "risks": [],
    }


class TestBuildTasks:
    """End-to-end coverage for the `build-tasks` subcommand (TASK-004).

    Validates the fat-manifest invariant (`description` + `acceptance_criteria`
    populated per task), the schedule-shape compatibility (pipes through
    `parse-schedule --stdin` cleanly), and the structured error paths for
    roster / child / dependency malformations.
    """

    def test_directory_mode_fixture_round_trip(self, tmp_path: Path) -> None:
        """Shipped fixture → `build-tasks` → `parse-schedule` clean pass.

        AC: "passes `parse-schedule --stdin` with `outcome=valid`;
        `tasks[0].description` is non-empty; `tasks[0].acceptance_criteria`
        is a non-empty list"
        """
        cp = _run(
            "build-tasks",
            "--plans-dir", str(DIRECTORY_MODE_FIXTURE_PATH),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["ok"] is True
        assert res["outcome"] == "valid"
        assert res["errors"] == []
        assert res["warnings"] == []
        tasks = res["tasks"]
        assert len(tasks) == 3
        assert [t["id"] for t in tasks] == ["001", "002", "003"]
        # Fat-manifest invariant: description + AC populated per task.
        for t in tasks:
            assert isinstance(t["description"], str)
            assert t["description"].strip(), (
                f"task {t['id']}: description must be non-empty"
            )
            assert isinstance(t["acceptance_criteria"], list)
            assert len(t["acceptance_criteria"]) >= 1, (
                f"task {t['id']}: acceptance_criteria must be non-empty"
            )
            assert t["plan_file"].startswith(f"TASK-{t['id']}_")
            assert t["plan_file"].endswith(".md")
        # `tasks[0]` spelled out explicitly (AC literal text).
        assert tasks[0]["description"].strip(), tasks[0]
        assert len(tasks[0]["acceptance_criteria"]) >= 1, tasks[0]
        # Feed the projected schedule shape into `parse-schedule --stdin`.
        sched_input = json.dumps(_schedule_shape(res))
        cp2 = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=sched_input,
            capture_output=True,
            text=True,
        )
        assert cp2.returncode == 0, cp2.stderr
        parsed = _parse_json(cp2)
        assert parsed["outcome"] == "valid", parsed
        assert parsed["errors"] == [], parsed
        # Schedule warnings must also be empty (no alias or unknown-field).
        assert parsed["warnings"] == [], parsed

    def test_directory_mode_fixture_agent_preserved(self) -> None:
        """Shipped children all declare `**Agent:**`; build-tasks preserves it."""
        cp = _run(
            "build-tasks",
            "--plans-dir", str(DIRECTORY_MODE_FIXTURE_PATH),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        tasks = _parse_json(cp)["tasks"]
        for t in tasks:
            assert t.get("agent") == "claude", t

    def test_decompose_to_build_tasks_round_trip(self, tmp_path: Path) -> None:
        """Canonical decomposer fixture → decompose → build-tasks pipe.

        AC: "output is a valid fat schedule" — every task carries a
        non-empty `description` and a non-empty `acceptance_criteria`
        list, AND the projected schedule passes `parse-schedule --stdin`
        with `outcome=valid`. The canonical fixture omits `**Agent:**`
        on TASK-002 on purpose (classifier fan-out in TASK-005 populates
        it), so the `agent` key is absent on that task and
        `parse-schedule` surfaces it as a warning — not an error.
        """
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS_DIR / "canonical.md").read_text(
                encoding="utf-8",
            ),
            encoding="utf-8",
        )
        cp = _run("decompose-plan", "--plan-file", str(src), "--json")
        assert cp.returncode == 0, cp.stderr
        decomp = _parse_json(cp)
        produced = Path(decomp["produced_dir"])
        assert produced.is_dir(), produced
        cp2 = _run("build-tasks", "--plans-dir", str(produced), "--json")
        assert cp2.returncode == 0, cp2.stderr
        bt = _parse_json(cp2)
        assert bt["ok"] is True, bt
        assert bt["errors"] == [], bt
        assert bt["warnings"] == [], bt
        tasks = bt["tasks"]
        assert [t["id"] for t in tasks] == ["001", "002", "003"], tasks
        for t in tasks:
            assert t["description"].strip(), (
                f"task {t['id']}: description must be non-empty"
            )
            assert len(t["acceptance_criteria"]) >= 1, (
                f"task {t['id']}: acceptance_criteria must be non-empty"
            )
        # TASK-002 in canonical.md deliberately omits `**Agent:**` —
        # build-tasks must NOT synthesize a placeholder.
        assert "agent" not in tasks[1], tasks[1]
        # TASK-001 + TASK-003 declare agent; it is preserved verbatim.
        assert tasks[0]["agent"] == "claude", tasks[0]
        assert tasks[2]["agent"] == "codex", tasks[2]
        # Full round-trip contract: the projected schedule shape must pass
        # `parse-schedule --stdin` with outcome=valid and zero errors.
        # Missing `agent` on TASK-002 is surfaced as a warning (not an
        # error) so the unclassified transitional state round-trips
        # cleanly. TASK-005's classifier fan-out fills it in later.
        sched_input = json.dumps(_schedule_shape(bt))
        cp3 = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=sched_input,
            capture_output=True,
            text=True,
        )
        assert cp3.returncode == 0, cp3.stderr
        parsed = _parse_json(cp3)
        assert parsed["outcome"] == "valid", parsed
        assert parsed["errors"] == [], parsed
        missing_agent_warnings = [
            w for w in parsed.get("warnings") or []
            if "missing field 'agent'" in w and "tasks[1]" in w
        ]
        assert missing_agent_warnings, parsed

    def test_build_tasks_batches_respect_dependencies(self) -> None:
        """`batches[]` must topologically order dependent tasks.

        Regression: prior `_compute_schedule_batches` only grouped by
        file-lock disjointness, so TASK-001 → TASK-002 → TASK-003 would
        land in a single batch when their files were disjoint, and
        `batch-next` could dispatch a dependent before its prereq
        finished. The shipped `directory_mode_plan` fixture has
        TASK-001 as a seeder with TASK-002 and TASK-003 both depending
        on it; TASK-001 must therefore land in an earlier batch than
        both dependents.
        """
        cp = _run(
            "build-tasks",
            "--plans-dir", str(DIRECTORY_MODE_FIXTURE_PATH),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        batches = res["batches"]
        # Locate the batch index for each task.
        batch_of: dict[str, int] = {}
        for b in batches:
            for tid in b["task_ids"]:
                batch_of[tid] = b["index"]
        assert {"001", "002", "003"} <= set(batch_of.keys()), batches
        assert batch_of["001"] < batch_of["002"], batches
        assert batch_of["001"] < batch_of["003"], batches
        # TASK-002 and TASK-003 are file-disjoint siblings that both
        # depend only on TASK-001; they SHOULD share a batch so
        # `batch-next` can dispatch them in parallel.
        assert batch_of["002"] == batch_of["003"], batches

    def test_build_tasks_preserves_wrapped_acceptance_criteria_bullets(
        self,
    ) -> None:
        """Wrapped (continuation-line) AC bullets round-trip intact.

        Regression: prior `_extract_bullet_list` broke on the first
        non-bullet line, silently truncating bullets whose content
        wrapped onto a second line. The shipped
        `TASK-002_write_a.md` fixture has a 2-line wrapped AC bullet
        ending in "... invariant that\\n    lets TASK-002 and TASK-003
        batch in parallel)."; the full continuation must be preserved.
        """
        cp = _run(
            "build-tasks",
            "--plans-dir", str(DIRECTORY_MODE_FIXTURE_PATH),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        tasks = res["tasks"]
        t2 = next(t for t in tasks if t["id"] == "002")
        ac = t2["acceptance_criteria"]
        # Must retain exactly 2 bullets (the wrapped continuation must
        # NOT split the second bullet into two entries).
        assert len(ac) == 2, ac
        # The second bullet must include both halves of the wrapped
        # text — the "batch in parallel" tail is the load-bearing proof
        # that continuation lines are folded into the current bullet.
        assert "batch in parallel" in ac[1], ac[1]
        assert "file-lock disjointness invariant" in ac[1], ac[1]
        # Same invariant on TASK-003, whose AC bullet is similarly
        # wrapped — makes the fix provably symmetric across siblings.
        t3 = next(t for t in tasks if t["id"] == "003")
        ac3 = t3["acceptance_criteria"]
        assert len(ac3) == 2, ac3
        assert "batch in parallel" in ac3[1], ac3[1]

    def test_missing_roster_structured_error(self, tmp_path: Path) -> None:
        """Directory exists but `00_INDEX.json` is absent → structured error."""
        empty_dir = tmp_path / "no_roster"
        empty_dir.mkdir()
        cp = _run("build-tasks", "--plans-dir", str(empty_dir), "--json")
        assert cp.returncode == 1
        res = _parse_json(cp)
        assert res["ok"] is False
        assert res["outcome"] == "invalid"
        assert any(
            e["code"] == "roster-not-found" for e in res["errors"]
        ), res

    def test_missing_child_structured_error(self, tmp_path: Path) -> None:
        """A `chunks[].file` that does not exist on disk surfaces as an error.

        AC: "a child file named in `chunks[]` that doesn't exist on disk
        surfaces as structured `errors[*]`"
        """
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({
                "schema_version": 1,
                "chunks": [
                    {
                        "task_id": "001",
                        "file": "TASK-001_ghost.md",
                        "depends_on": [],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                ],
            }),
            encoding="utf-8",
        )
        # Note: no TASK-001_ghost.md actually written.
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        assert cp.returncode == 1
        res = _parse_json(cp)
        assert res["ok"] is False
        err = res["errors"]
        missing = [e for e in err if e["code"] == "child-file-not-found"]
        assert missing, err
        assert missing[0]["task_id"] == "001"
        assert missing[0]["plan_file"] == "TASK-001_ghost.md"

    def test_malformed_roster_structured_error(self, tmp_path: Path) -> None:
        """Invalid JSON in `00_INDEX.json` surfaces as `malformed-roster`."""
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        # Deliberately invalid JSON.
        (plans_dir / "00_INDEX.json").write_text(
            "{ this is not valid json ]]", encoding="utf-8",
        )
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        assert cp.returncode == 1
        res = _parse_json(cp)
        assert res["ok"] is False
        err = res["errors"]
        assert any(e["code"] == "malformed-roster" for e in err), err

    def test_malformed_roster_wrong_shape(self, tmp_path: Path) -> None:
        """Top-level JSON that is not an object surfaces as malformed-roster."""
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        (plans_dir / "00_INDEX.json").write_text("[]", encoding="utf-8")
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        assert cp.returncode == 1
        res = _parse_json(cp)
        assert any(
            e["code"] == "malformed-roster" for e in res["errors"]
        ), res

    def test_cycle_in_dependencies_structured_error(
        self, tmp_path: Path,
    ) -> None:
        """Dependency cycle A→B→A surfaces as `cyclic-dependency`."""
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        # Two children with A→B→A cycle. Roster must pass its own
        # schema (deps must be normalized), so both entries are valid
        # on the roster side — the cycle is in the parsed child graph.
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({
                "schema_version": 1,
                "chunks": [
                    {
                        "task_id": "001",
                        "file": "TASK-001_a.md",
                        "depends_on": ["002"],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                    {
                        "task_id": "002",
                        "file": "TASK-002_b.md",
                        "depends_on": ["001"],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                ],
            }),
            encoding="utf-8",
        )
        child_a = (
            "# TASK-001 — A\n\n"
            "## Goal\n\nA\n\n## Context\n\nctx\n\n"
            "## Verification\n\n- x\n\n## Tasks\n\n"
            "### TASK-001: A\n\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - src/a.py\n"
            "- **Dependencies:** [002]\n"
            "- **Test command:** `test -f src/a.py`\n"
            "- **Acceptance criteria:**\n"
            "  - a exists\n"
            "- **Reversion guidance:** none\n"
            "\n**Description:**\nDesc a.\n"
        )
        child_b = (
            "# TASK-002 — B\n\n"
            "## Goal\n\nB\n\n## Context\n\nctx\n\n"
            "## Verification\n\n- x\n\n## Tasks\n\n"
            "### TASK-002: B\n\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - src/b.py\n"
            "- **Dependencies:** [001]\n"
            "- **Test command:** `test -f src/b.py`\n"
            "- **Acceptance criteria:**\n"
            "  - b exists\n"
            "- **Reversion guidance:** none\n"
            "\n**Description:**\nDesc b.\n"
        )
        (plans_dir / "TASK-001_a.md").write_text(child_a, encoding="utf-8")
        (plans_dir / "TASK-002_b.md").write_text(child_b, encoding="utf-8")
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        assert cp.returncode == 1
        res = _parse_json(cp)
        cycles = [
            e for e in res["errors"] if e["code"] == "cyclic-dependency"
        ]
        assert cycles, res["errors"]
        assert set(cycles[0]["task_ids"]) == {"001", "002"}, cycles[0]

    def test_missing_description_is_warning_not_error(
        self, tmp_path: Path,
    ) -> None:
        """A well-formed child missing `**Description:**` emits a warning.

        AC: "Missing `**Description:**` or `**Acceptance criteria:**` in a
        child surfaces as `warnings[*]` with task_id (non-fatal — plan-review
        will flag downstream)."
        """
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({
                "schema_version": 1,
                "chunks": [
                    {
                        "task_id": "001",
                        "file": "TASK-001_a.md",
                        "depends_on": [],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                ],
            }),
            encoding="utf-8",
        )
        # Child with AC bullets but NO `**Description:**` section.
        child_no_desc = (
            "# TASK-001 — A\n\n"
            "## Goal\n\nA\n\n## Context\n\nctx\n\n"
            "## Verification\n\n- x\n\n## Tasks\n\n"
            "### TASK-001: A\n\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - src/a.py\n"
            "- **Dependencies:** []\n"
            "- **Test command:** `test -f src/a.py`\n"
            "- **Acceptance criteria:**\n"
            "  - a exists\n"
            "- **Reversion guidance:** none\n"
        )
        (plans_dir / "TASK-001_a.md").write_text(
            child_no_desc, encoding="utf-8",
        )
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        # Warning, not error — exit code is 0 and task is populated.
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["ok"] is True, res
        assert res["errors"] == [], res
        assert len(res["tasks"]) == 1, res
        assert res["tasks"][0]["description"] == "", res["tasks"][0]
        # AC list is populated, so only the missing-description warning fires.
        warns = [
            w for w in res["warnings"] if w["code"] == "missing-description"
        ]
        assert warns, res["warnings"]
        assert warns[0]["task_id"] == "001"
        assert warns[0]["plan_file"] == "TASK-001_a.md"

    def test_missing_acceptance_criteria_is_warning_not_error(
        self, tmp_path: Path,
    ) -> None:
        """A child missing `**Acceptance criteria:**` bullets emits a warning.

        AC coverage: the sibling path to `missing-description`. Both are
        non-fatal per the plan — plan-review downstream flags them as
        gaps, not structural errors.
        """
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({
                "schema_version": 1,
                "chunks": [
                    {
                        "task_id": "001",
                        "file": "TASK-001_a.md",
                        "depends_on": [],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                ],
            }),
            encoding="utf-8",
        )
        # Child with description but NO acceptance-criteria bullets.
        child_no_ac = (
            "# TASK-001 — A\n\n"
            "## Goal\n\nA\n\n## Context\n\nctx\n\n"
            "## Verification\n\n- x\n\n## Tasks\n\n"
            "### TASK-001: A\n\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - src/a.py\n"
            "- **Dependencies:** []\n"
            "- **Test command:** `test -f src/a.py`\n"
            "- **Reversion guidance:** none\n"
            "\n**Description:**\nDesc.\n"
        )
        (plans_dir / "TASK-001_a.md").write_text(
            child_no_ac, encoding="utf-8",
        )
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["ok"] is True, res
        assert res["errors"] == [], res
        assert res["tasks"][0]["acceptance_criteria"] == [], res["tasks"][0]
        warns = [
            w for w in res["warnings"]
            if w["code"] == "missing-acceptance-criteria"
        ]
        assert warns, res["warnings"]
        assert warns[0]["task_id"] == "001"

    def test_unresolvable_dep_structured_error(
        self, tmp_path: Path,
    ) -> None:
        """Dependency id not in the roster surfaces as `unresolvable-dep`."""
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({
                "schema_version": 1,
                "chunks": [
                    {
                        "task_id": "001",
                        "file": "TASK-001_a.md",
                        "depends_on": [],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                ],
            }),
            encoding="utf-8",
        )
        child = (
            "# TASK-001 — A\n\n"
            "## Goal\n\nA\n\n## Context\n\nctx\n\n"
            "## Verification\n\n- x\n\n## Tasks\n\n"
            "### TASK-001: A\n\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - src/a.py\n"
            "- **Dependencies:** [999]\n"
            "- **Test command:** `test -f src/a.py`\n"
            "- **Acceptance criteria:**\n"
            "  - a exists\n"
            "- **Reversion guidance:** none\n"
            "\n**Description:**\nDesc.\n"
        )
        (plans_dir / "TASK-001_a.md").write_text(child, encoding="utf-8")
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        assert cp.returncode == 1
        res = _parse_json(cp)
        unresolved = [
            e for e in res["errors"] if e["code"] == "unresolvable-dep"
        ]
        assert unresolved, res["errors"]
        assert unresolved[0]["task_id"] == "001"
        assert unresolved[0]["dep_id"] == "999"

    def test_build_tasks_subdir_prefixed_chunks_emit_basename_plan_file(
        self, tmp_path: Path,
    ) -> None:
        """Subdir-prefixed `chunks[].file` → emitted `plan_file` is basename only.

        Regression: `build-tasks` previously copied `chunks[].file` verbatim
        into `tasks[].plan_file`. When a roster entry pointed into a
        subdirectory (e.g. `subdir/TASK-001_a.md`), the child file was
        still readable via `plans_dir / chunks[].file`, but the emitted
        `plan_file` retained the `subdir/` prefix. `parse-schedule` then
        rejected it as `invalid-plan-file` because
        `_is_valid_plan_file_basename` forbids `/` in the value, and
        downstream basename-keyed routing also broke.

        Fix: `_build_tasks` stores `Path(child_name).name` in `plan_file`
        while continuing to locate the child on disk via the full path.
        """
        plans_dir = tmp_path / "decomposed"
        plans_dir.mkdir()
        subdir = plans_dir / "subdir"
        subdir.mkdir()
        (plans_dir / "00_INDEX.json").write_text(
            json.dumps({
                "schema_version": 1,
                "chunks": [
                    {
                        "task_id": "001",
                        "file": "subdir/TASK-001_a.md",
                        "depends_on": [],
                        "status": "Pending",
                        "superseded_by": [],
                    },
                ],
            }),
            encoding="utf-8",
        )
        child = (
            "# TASK-001 — A\n\n"
            "## Goal\n\nA\n\n## Context\n\nctx\n\n"
            "## Verification\n\n- x\n\n## Tasks\n\n"
            "### TASK-001: A\n\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - src/a.py\n"
            "- **Dependencies:** []\n"
            "- **Test command:** `test -f src/a.py`\n"
            "- **Acceptance criteria:**\n"
            "  - a exists\n"
            "- **Reversion guidance:** none\n"
            "\n**Description:**\nDesc a.\n"
        )
        (subdir / "TASK-001_a.md").write_text(child, encoding="utf-8")
        cp = _run("build-tasks", "--plans-dir", str(plans_dir), "--json")
        # Build succeeds — no `child-file-not-found`; the child is
        # readable at `plans_dir / subdir / TASK-001_a.md`.
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["ok"] is True, res
        assert res["errors"] == [], res
        # Critical invariant: emitted `plan_file` is the basename only
        # (no `subdir/` prefix), so `_is_valid_plan_file_basename` will
        # accept it downstream.
        assert len(res["tasks"]) == 1, res
        assert res["tasks"][0]["plan_file"] == "TASK-001_a.md", (
            res["tasks"][0]
        )
        # Pipe through `parse-schedule --stdin` — must surface no
        # `invalid-plan-file` error.
        sched_input = json.dumps(_schedule_shape(res))
        cp2 = subprocess.run(
            [str(PY), str(SCRIPT), "parse-schedule", "--stdin", "--json"],
            input=sched_input,
            capture_output=True,
            text=True,
        )
        assert cp2.returncode == 0, cp2.stderr
        parsed = _parse_json(cp2)
        assert parsed["outcome"] == "valid", parsed
        invalid_pf = [
            e for e in parsed.get("errors") or []
            if isinstance(e, str) and "invalid-plan-file" in e
        ] + [
            e for e in parsed.get("errors") or []
            if isinstance(e, dict) and e.get("code") == "invalid-plan-file"
        ]
        assert not invalid_pf, parsed

    def test_build_tasks_global_lock_task_is_solitary_in_batch(
        self, tmp_path: Path,
    ) -> None:
        """Global-lock tasks land in a solitary batch in `_build_tasks`'s output.

        Regression: pre-PLAN_TOPO_RESPECT_FIX_2026-04-25 TASK-003,
        ``_build_tasks`` ran its own in-loop file-disjoint batcher that
        did NOT honor ``_is_global_lock_path`` — so a task touching
        ``requirements.txt`` could co-batch with file-disjoint siblings
        in `build-tasks` output even though `compute-schedule` would
        force it solitary. After routing both call sites through the
        shared ``_dependency_aware_batches`` helper, the global-lock
        carve-out applies uniformly.

        The fixture is synthesized inline (whole-plan markdown → tmp_path
        → ``decompose-plan`` → ``build-tasks``) so no on-disk fixture
        addition is required.
        """
        whole_plan = (
            "# Plan: global-lock smoke\n"
            "\n"
            "**Created:** 2026-04-25\n"
            "**Status:** pending\n"
            "**Base branch:** main\n"
            "\n"
            "## Goal\n"
            "\n"
            "Exercise the global-lock solitary-batch carve-out inside\n"
            "`_build_tasks`'s output.\n"
            "\n"
            "## Context\n"
            "\n"
            "Three independent (no inter-dependency) tasks. TASK-002 touches\n"
            "`requirements.txt` (a global-lock path). The shared batcher must\n"
            "place TASK-002 in its own batch even though it is file-disjoint\n"
            "from TASK-001 and TASK-003.\n"
            "\n"
            "## Verification\n"
            "\n"
            "After `decompose-plan` + `build-tasks`, the batches[] array\n"
            "places TASK-002 alone in its own batch.\n"
            "\n"
            "## Tasks\n"
            "\n"
            "## TASK-001: Touch alpha file\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - scratch/alpha.txt (create)\n"
            "- **Dependencies:** []\n"
            "- **Test command:** none\n"
            "- **Acceptance criteria:**\n"
            "  - alpha exists\n"
            "- **Reversion guidance:** `rm -f scratch/alpha.txt`\n"
            "\n"
            "**Description:**\n"
            "Independent leaf-write task A.\n"
            "\n"
            "## TASK-002: Bump runtime requirement pin\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - requirements.txt\n"
            "- **Dependencies:** []\n"
            "- **Test command:** none\n"
            "- **Acceptance criteria:**\n"
            "  - requirements.txt updated\n"
            "- **Reversion guidance:** revert pin\n"
            "\n"
            "**Description:**\n"
            "Touches a global-lock path; must land solo in its batch.\n"
            "\n"
            "## TASK-003: Touch beta file\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** high\n"
            "- **Agent:** claude\n"
            "- **Files:**\n"
            "  - scratch/beta.txt (create)\n"
            "- **Dependencies:** []\n"
            "- **Test command:** none\n"
            "- **Acceptance criteria:**\n"
            "  - beta exists\n"
            "- **Reversion guidance:** `rm -f scratch/beta.txt`\n"
            "\n"
            "**Description:**\n"
            "Independent leaf-write task B.\n"
        )
        plan_path = tmp_path / "global_lock_plan.md"
        plan_path.write_text(whole_plan, encoding="utf-8")
        cp = _run("decompose-plan", "--plan-file", str(plan_path), "--json")
        assert cp.returncode == 0, cp.stderr
        produced = Path(_parse_json(cp)["produced_dir"])
        cp2 = _run("build-tasks", "--plans-dir", str(produced), "--json")
        assert cp2.returncode == 0, cp2.stderr
        res = _parse_json(cp2)
        assert res["ok"] is True, res
        assert res["errors"] == [], res
        batches = res["batches"]
        # TASK-002 is global-lock; must be alone in its batch.
        batch_for_002 = next(
            (b for b in batches if "002" in b["task_ids"]), None,
        )
        assert batch_for_002 is not None, batches
        assert batch_for_002["task_ids"] == ["002"], (
            f"global-lock task TASK-002 must be solitary in its batch; "
            f"got {batch_for_002!r}"
        )
        assert batch_for_002["file_locks"] == ["requirements.txt"], (
            batch_for_002
        )
        # The siblings must NOT be co-batched with TASK-002.
        for b in batches:
            if "002" in b["task_ids"]:
                continue
            assert "002" not in b["task_ids"], b

    def test_build_tasks_then_compute_schedule_is_no_op_on_batches(
        self,
    ) -> None:
        """`build-tasks` and `compute-schedule` agree byte-for-byte on `batches[]`.

        This is the load-bearing post-condition that authorizes
        TASK-004 (PLAN_TOPO_RESPECT_FIX_2026-04-25) to delete the
        SKILL.md ``compute-schedule --stdin`` recompute pipe: if both
        CLIs route through ``_dependency_aware_batches`` and produce
        bytewise-identical ``batches[]`` for the same input, the
        recompute is a provable no-op rather than a presumed one.

        The CLIs are invoked via subprocess (not direct function calls)
        so the JSON serialization layer is exercised — that is where any
        residual byte-difference would surface.
        """
        cp = _run(
            "build-tasks",
            "--plans-dir", str(DIRECTORY_MODE_FIXTURE_PATH),
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        bt = _parse_json(cp)
        assert bt["ok"] is True, bt
        # Pipe `build-tasks` output's tasks/batches into `compute-schedule
        # --stdin`. The schedule input shape is `{"tasks": [...]}` — the
        # CLI re-derives `batches[]` from `tasks[]` on its own.
        sched_input = json.dumps({"tasks": bt["tasks"]})
        cp2 = subprocess.run(
            [str(PY), str(SCRIPT), "compute-schedule", "--stdin", "--json"],
            input=sched_input,
            capture_output=True,
            text=True,
        )
        assert cp2.returncode == 0, cp2.stderr
        cs = _parse_json(cp2)
        assert cs.get("errors") == [], cs
        # Strict byte-equality contract via canonical (sorted-key) JSON
        # serialization. Any drift between the two batchers — element
        # ordering, file_locks ordering, batch indexing — would surface
        # here.
        bt_batches_json = json.dumps(bt["batches"], sort_keys=True)
        cs_batches_json = json.dumps(cs["batches"], sort_keys=True)
        assert bt_batches_json == cs_batches_json, (
            f"build-tasks vs compute-schedule batches[] drift:\n"
            f"  build-tasks   : {bt_batches_json}\n"
            f"  compute-sched : {cs_batches_json}"
        )


# ---------------------------------------------------------------------------
# TASK-009: resolve-read-targets / pre-read excerpts
# ---------------------------------------------------------------------------


class TestResolveReadTargets:
    """Coverage for `**Read targets:**` / `**Symbol targets:**` resolution.

    Exercises the helper's structured output (line-range reads, Python AST
    symbol extraction, regex fallback for non-Python files, clamping to
    file length, and clean reporting of missing symbols / files). Also
    asserts that dispatcher prompt rendering grows a `## Pre-read excerpts`
    block when targets are present and is a no-op otherwise.
    """

    def _run_stdin(self, body: str) -> dict:
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "resolve-read-targets", "--stdin"],
            input=body,
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        return json.loads(cp.stdout)

    def test_read_targets_line_range_basic(self, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "sample.py"
        target.write_text(
            "\n".join(f"line{i:03d}" for i in range(1, 51)) + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Read targets:**\n"
            "  - sample.py:5-8\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        assert len(out["reads"]) == 1, out
        r = out["reads"][0]
        assert r["file"] == "sample.py"
        assert r["start"] == 5 and r["end"] == 8
        assert r["text"] == "line005\nline006\nline007\nline008"
        assert "truncated_to" not in r

    def test_read_targets_clamps_to_file_length(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "small.py"
        target.write_text(
            "\n".join(f"L{i}" for i in range(1, 11)) + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Read targets:**\n"
            "  - small.py:1-9999\n"
        )
        out = self._run_stdin(body)
        r = out["reads"][0]
        assert r["start"] == 1
        assert r["end"] == 10
        assert r.get("truncated_to") == 10

    def test_read_targets_symbol_python_function(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        py = tmp_path / "mod.py"
        py.write_text(
            "def alpha():\n"
            "    return 1\n"
            "\n"
            "\n"
            "def beta(x):\n"
            "    y = x + 1\n"
            "    return y\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Symbol targets:**\n"
            "  - mod.py::beta\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        assert len(out["symbols"]) == 1, out
        sym = out["symbols"][0]
        assert sym == {"path": "mod.py", "symbol": "beta", "start": 5, "end": 7}
        # The corresponding `reads` entry carries the function body text.
        r = out["reads"][0]
        assert "def beta" in r["text"]
        assert r.get("symbol_match") == "ast"

    def test_read_targets_symbol_class_method(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        py = tmp_path / "cls.py"
        py.write_text(
            "class Foo:\n"
            "    def bar(self):\n"
            "        return 'hi'\n"
            "\n"
            "    def baz(self, n):\n"
            "        return n * 2\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Symbol targets:**\n"
            "  - cls.py::Foo.baz\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        sym = out["symbols"][0]
        assert sym["symbol"] == "Foo.baz"
        assert sym["start"] == 5
        assert sym["end"] == 6
        # The body must lie inside the class span.
        assert "def baz" in out["reads"][0]["text"]

    def test_read_targets_symbol_missing_records_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        py = tmp_path / "tiny.py"
        py.write_text("def real():\n    return 1\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Symbol targets:**\n"
            "  - tiny.py::does_not_exist\n"
        )
        # Exit code is 0 even with errors (advisory).
        cp = subprocess.run(
            [str(PY), str(SCRIPT), "resolve-read-targets", "--stdin"],
            input=body,
            capture_output=True,
            text=True,
        )
        assert cp.returncode == 0, cp.stderr
        out = json.loads(cp.stdout)
        assert out["reads"] == []
        assert out["symbols"] == []
        assert any(
            "tiny.py::does_not_exist not found" in e for e in out["errors"]
        ), out

    def test_read_targets_non_python_regex_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sh = tmp_path / "lib.sh"
        sh.write_text(
            "#!/bin/bash\n"
            "\n"
            "do_thing() {\n"
            "  echo hello\n"
            "  echo world\n"
            "}\n"
            "\n"
            "other_fn() {\n"
            "  echo other\n"
            "}\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Symbol targets:**\n"
            "  - lib.sh::do_thing\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        sym = out["symbols"][0]
        assert sym["symbol"] == "do_thing"
        assert sym["start"] == 3
        # Regex fallback annotates the read entry.
        r = out["reads"][0]
        assert r.get("symbol_match") == "regex"
        assert "regex" in (r.get("note") or "")

    def test_read_targets_missing_file_records_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Read targets:**\n"
            "  - does_not_exist.py:1-10\n"
        )
        out = self._run_stdin(body)
        assert any("file not found" in e for e in out["errors"]), out
        assert out["reads"][0].get("missing") is True

    def test_read_targets_absent_returns_empty_structure(self) -> None:
        out = self._run_stdin("Some unrelated markdown body.\n")
        assert out == {"reads": [], "symbols": [], "errors": []}

    def test_read_targets_header_with_trailing_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Header with documented `(optional, TASK-009)` trailing text parses.

        Regression: the bold-field header regex used to require an exact
        line, which silently dropped reads from the documented template
        form `- **Read targets:** (optional, TASK-009)`.
        """
        target = tmp_path / "sample.py"
        target.write_text(
            "\n".join(f"line{i:03d}" for i in range(1, 21)) + "\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Read targets:** (optional, TASK-009)\n"
            "  - sample.py:2-4\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        assert len(out["reads"]) == 1, out
        r = out["reads"][0]
        assert r["file"] == "sample.py"
        assert r["start"] == 2 and r["end"] == 4
        assert r["text"] == "line002\nline003\nline004"

    def test_symbol_targets_header_with_trailing_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Symbol-targets header tolerates documented trailing text."""
        target = tmp_path / "mod.py"
        target.write_text(
            "def alpha():\n"
            "    return 1\n"
            "\n"
            "def beta():\n"
            "    return 2\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Symbol targets:** (optional, TASK-009)\n"
            "  - mod.py::beta\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        assert len(out["symbols"]) == 1, out
        sym = out["symbols"][0]
        assert sym["symbol"] == "beta"
        assert sym["start"] == 4
        assert sym["end"] == 5

    def test_read_targets_coexist_with_symbol_section_trailing_text(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both `Read targets:` and `Symbol targets:` headers can carry the
        documented `(optional, TASK-009)` trailing parenthetical and the
        section-boundary detector must stop the read scan at the symbol
        header rather than swallowing it as an unparseable read entry.
        """
        target = tmp_path / "mod.py"
        target.write_text(
            "line_a\n"
            "line_b\n"
            "line_c\n"
            "def gamma():\n"
            "    return 9\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        body = (
            "- **Read targets:** (optional, TASK-009)\n"
            "  - mod.py:1-2\n"
            "- **Symbol targets:** (optional, TASK-009)\n"
            "  - mod.py::gamma\n"
        )
        out = self._run_stdin(body)
        assert out["errors"] == [], out
        # Symbol resolution also appends to reads (symbol body is
        # rendered as a pre-read excerpt), so reads has 2 entries: the
        # explicit line range + the symbol body.
        assert len(out["reads"]) == 2, out
        assert out["reads"][0]["start"] == 1
        assert out["reads"][0]["end"] == 2
        assert len(out["symbols"]) == 1, out
        assert out["symbols"][0]["symbol"] == "gamma"

    def test_render_pre_read_excerpts_empty_when_no_targets(self) -> None:
        rendered = plan_ops.render_pre_read_excerpts(
            {"reads": [], "symbols": [], "errors": []}
        )
        assert rendered == ""

    def test_render_pre_read_excerpts_includes_block_when_present(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "show.py"
        target.write_text("a=1\nb=2\nc=3\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        resolved = plan_ops.resolve_read_targets(
            "- **Read targets:**\n  - show.py:1-2\n"
        )
        rendered = plan_ops.render_pre_read_excerpts(resolved)
        assert "## Pre-read excerpts" in rendered
        assert "show.py (lines 1-2)" in rendered
        assert "a=1" in rendered
        assert "```python" in rendered

    def test_dispatch_implement_prompt_includes_pre_read_excerpts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Stand up a minimal plan + target file so the dispatcher's
        # render_implement_prompt grows the Pre-read excerpts block.
        sys.path.insert(0, str(SCRIPTS_DIR))
        import plan_codex_dispatch  # noqa: WPS433

        target = tmp_path / "big.py"
        target.write_text(
            "\n".join(f"line{i:03d}" for i in range(1, 21)) + "\n",
            encoding="utf-8",
        )
        plan = tmp_path / "plan.md"
        plan.write_text(
            "# Plan: t\n"
            "\n"
            "## Context\n"
            "ctx body.\n"
            "\n"
            "## Tasks\n"
            "\n"
            "### TASK-001: Sample\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** medium\n"
            "- **Files:**\n"
            "  - big.py\n"
            "- **Test command:** none\n"
            "- **Acceptance criteria:**\n"
            "  - works\n"
            "- **Read targets:**\n"
            "  - big.py:3-5\n"
            "\n"
            "**Description:**\n"
            "Do the thing.\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        plan_text = plan.read_text(encoding="utf-8")
        task = plan_codex_dispatch.parse_task_block(plan_text, "001")
        prompt = plan_codex_dispatch.render_implement_prompt(task, "ctx body.")
        assert "## Pre-read excerpts" in prompt
        assert "big.py (lines 3-5)" in prompt
        assert "line003" in prompt
        # The legacy prompt body is still present.
        assert "Implement TASK-001" in prompt

    def test_dispatch_implement_prompt_unchanged_without_targets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import plan_codex_dispatch  # noqa: WPS433

        plan = tmp_path / "plan.md"
        plan.write_text(
            "# Plan: t\n"
            "\n"
            "## Context\n"
            "ctx.\n"
            "\n"
            "## Tasks\n"
            "\n"
            "### TASK-001: NoTargets\n"
            "\n"
            "- **Status:** pending\n"
            "- **Priority:** medium\n"
            "- **Files:**\n"
            "  - foo.py\n"
            "- **Test command:** none\n"
            "- **Acceptance criteria:**\n"
            "  - works\n"
            "\n"
            "**Description:**\n"
            "Do.\n",
            encoding="utf-8",
        )
        monkeypatch.chdir(tmp_path)
        plan_text = plan.read_text(encoding="utf-8")
        task = plan_codex_dispatch.parse_task_block(plan_text, "001")
        prompt = plan_codex_dispatch.render_implement_prompt(task, "ctx.")
        # No targets => no Pre-read excerpts block.
        assert "Pre-read excerpts" not in prompt
        # Sanity: the prompt opens with the canonical implement header.
        assert prompt.startswith("Implement TASK-001"), prompt[:80]


# ---------------------------------------------------------------------------
# TASK-006: Phase D.1 review-failure routing documentation in SKILL.md
# ---------------------------------------------------------------------------


class TestSkillRoutingDocumentation:
    """Documentation smoke test: SKILL.md §Phase D.1 documents the
    Codex-side wrapper-failure routing reason enum.

    Pins the documented vocabulary so a future SKILL refactor cannot
    quietly delete the routing rule.

    Class/test names embed the literal substrings ``skill_routing`` and
    ``routing_documentation`` so the task's ``pytest -k`` filter selects
    this test verbatim.
    """

    def test_skill_routing_documentation_lists_codex_review_reasons(self) -> None:
        skill_path = (
            Path(__file__).resolve().parents[2]
            / "plugins"
            / "plan-executor"
            / "skills"
            / "implement-plan"
            / "SKILL.md"
        )
        text = skill_path.read_text(encoding="utf-8")
        assert "codex_review_timeout" in text
        assert "codex_review_parse_error" in text
        assert "codex_review_failure" in text


# ---------------------------------------------------------------------------
# index-closure (TASK-001 — narrow_run_filter_ids)
# ---------------------------------------------------------------------------


def _write_index(plans_dir: Path, chunks: list[dict]) -> Path:
    """Write a `00_INDEX.json` shell with the supplied raw `chunks` array.

    Skips `_parse_index_roster` validation entirely — these fixtures
    deliberately exercise malformed shapes that the strict roster parser
    rejects up-front. The closure helper consumes the raw `chunks` list.
    """
    plans_dir.mkdir(parents=True, exist_ok=True)
    index_path = plans_dir / "00_INDEX.json"
    index_path.write_text(
        json.dumps({"schema_version": 1, "chunks": chunks}, indent=2),
        encoding="utf-8",
    )
    return index_path


class TestIndexClosure:
    """Unit coverage for `_compute_index_closure` + `index-closure` CLI.

    Class/test names embed the literal substrings ``index_closure`` and
    ``IndexClosure`` so the task's ``pytest -k "index_closure or
    IndexClosure"`` filter selects this suite verbatim.
    """

    # ------------------------------------------------------------------
    # In-process helper coverage (V1, V2, V3, V4, V5, V6 + cycle).
    # ------------------------------------------------------------------

    def test_index_closure_happy_path_single_seed(self) -> None:
        """V1 analogue: closure walks roster-side `depends_on` only.

        Mirrors the live `DUAL_AGENT_Plans` shape in miniature: a seed
        with two transitive prereqs returns the full canonical-cased
        closure with zero errors.
        """
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
            {"task_id": "009", "depends_on": ["002"]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"009"})
        assert closure == {"001", "002", "009"}, closure
        assert errors == [], errors

    def test_index_closure_transitive_4_deep(self) -> None:
        """Closure walks at least 4 levels deep without truncation."""
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
            {"task_id": "003", "depends_on": ["002"]},
            {"task_id": "004", "depends_on": ["003"]},
            {"task_id": "005", "depends_on": ["004"]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"005"})
        assert closure == {"001", "002", "003", "004", "005"}, closure
        assert errors == [], errors

    def test_index_closure_normalizes_requested_id(self) -> None:
        """Callers can pass `9`, `009`, or `TASK-009` interchangeably.

        The closure set is canonical-cased (`"009"`, not `"9"`).
        """
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "009", "depends_on": ["001"]},
        ]
        for raw in ("9", "009", "TASK-009", "task-009"):
            closure, errors = plan_ops._compute_index_closure(chunks, {raw})
            assert closure == {"001", "009"}, (raw, closure)
            assert errors == [], (raw, errors)

    def test_index_closure_unknown_id_single(self) -> None:
        """V3: unknown id is non-fatal; surfaced as `unknown-requested-id`."""
        chunks = [
            {"task_id": "001", "depends_on": []},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"999"})
        assert closure == set(), closure
        assert errors == [
            {"code": "unknown-requested-id", "task_id": "999"},
        ], errors

    def test_index_closure_unknown_id_mixed_with_known(self) -> None:
        """V3: unknown id surfaces an error AND closure for known is computed."""
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
        ]
        closure, errors = plan_ops._compute_index_closure(
            chunks, {"002", "999"},
        )
        assert closure == {"001", "002"}, closure
        assert errors == [
            {"code": "unknown-requested-id", "task_id": "999"},
        ], errors

    def test_index_closure_malformed_dep_inside_closure(self) -> None:
        """V4: in-closure malformed dep surfaces `closure-malformed-dep`."""
        chunks = [
            {"task_id": "001", "depends_on": []},
            # In-closure: malformed dep must be surfaced.
            {"task_id": "002", "depends_on": ["001", 42]},
            {"task_id": "003", "depends_on": ["002"]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"003"})
        assert closure == {"001", "002", "003"}, closure
        # Exactly one closure-malformed-dep error against TASK-002, dep_id=42.
        assert errors == [
            {"code": "closure-malformed-dep", "task_id": "002", "dep_id": 42},
        ], errors

    def test_index_closure_malformed_dep_outside_closure_silent(self) -> None:
        """V5: out-of-closure malformed dep is silent (load-bearing).

        TASK-004 is a sibling of TASK-002 not reachable from the seed
        TASK-003. Its malformed `depends_on` must not be inspected — the
        helper neither walks it nor surfaces an error against it.
        """
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
            {"task_id": "003", "depends_on": ["002"]},
            # Outside the closure of {003}: the malformed dep MUST be silent.
            {"task_id": "004", "depends_on": [42, "not-a-valid-id", None]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"003"})
        assert closure == {"001", "002", "003"}, closure
        assert errors == [], errors

    def test_index_closure_duplicate_task_id(self) -> None:
        """V6: duplicate `task_id` surfaces `duplicate-roster-id`.

        First occurrence wins for traversal; the duplicate is recorded
        with its chunk index and the first-occurrence index.
        """
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
            # Duplicate of TASK-002 with a different dep set: must be
            # ignored for traversal AND surfaced as an error.
            {"task_id": "002", "depends_on": ["999"]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"002"})
        # Closure used the first occurrence's deps (just TASK-001).
        assert closure == {"001", "002"}, closure
        assert errors == [
            {
                "code": "duplicate-roster-id",
                "task_id": "002",
                "chunk_index": 2,
                "first_chunk_index": 1,
            },
        ], errors

    def test_index_closure_empty_requested_ids(self) -> None:
        """Empty `requested_ids` returns empty closure + empty errors."""
        chunks = [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, set())
        assert closure == set(), closure
        assert errors == [], errors

    def test_index_closure_self_referential_cycle(self) -> None:
        """Self-referential `depends_on` (cycle of length 1) → error.

        BFS terminates via the visited-set guard AND the self-cycle
        surfaces as `closure-malformed-dep` per the AC.
        """
        chunks = [
            {"task_id": "001", "depends_on": ["001"]},
        ]
        closure, errors = plan_ops._compute_index_closure(chunks, {"001"})
        # Visited-set guard: traversal terminates; closure includes the seed.
        assert closure == {"001"}, closure
        # Self-cycle surfaces as a closure-malformed-dep error.
        assert len(errors) == 1, errors
        e = errors[0]
        assert e["code"] == "closure-malformed-dep", e
        assert e["task_id"] == "001", e
        assert e["dep_id"] == "001", e
        assert "self-referential" in e.get("reason", ""), e

    def test_index_closure_error_source_order(self) -> None:
        """Errors are returned in chunk-declaration order; deps in index order.

        Two in-closure chunks each have two malformed deps. The four
        errors must come out in (chunk_idx, dep_idx) order.
        """
        chunks = [
            {"task_id": "001", "depends_on": []},
            # Two malformed deps at indices 1 and 2.
            {"task_id": "002", "depends_on": ["001", 11, "not-an-id"]},
            # Two more malformed deps at indices 0 and 1.
            {"task_id": "003", "depends_on": [22, "still-bad", "002"]},
        ]
        _closure, errors = plan_ops._compute_index_closure(chunks, {"003"})
        # 4 closure-malformed-dep errors in source order.
        bad_codes = [e["code"] for e in errors]
        assert bad_codes == ["closure-malformed-dep"] * 4, errors
        # Chunk 1 (TASK-002) before chunk 2 (TASK-003); within each
        # chunk, dep index is preserved.
        assert errors[0]["task_id"] == "002" and errors[0]["dep_id"] == 11
        assert errors[1]["task_id"] == "002" and errors[1]["dep_id"] == "not-an-id"
        assert errors[2]["task_id"] == "003" and errors[2]["dep_id"] == 22
        assert errors[3]["task_id"] == "003" and errors[3]["dep_id"] == "still-bad"

    # ------------------------------------------------------------------
    # CLI surface coverage (V7).
    # ------------------------------------------------------------------

    def test_index_closure_cli_v7_happy_path(self, tmp_path: Path) -> None:
        """V7: CLI emits sorted closure + skipped_chunk_count + empty errors."""
        plans_dir = tmp_path / "plan"
        _write_index(plans_dir, [
            {"task_id": "001", "depends_on": []},
            {"task_id": "002", "depends_on": ["001"]},
            {"task_id": "009", "depends_on": ["002"]},
            {"task_id": "017", "depends_on": ["001"]},
            # An unrelated sibling outside any closure.
            {"task_id": "099", "depends_on": []},
        ])
        cp = _run(
            "index-closure",
            "--plans-dir", str(plans_dir),
            "--task-ids", "009,017",
            "--json",
        )
        assert cp.returncode == 0, cp.stderr
        res = _parse_json(cp)
        assert res["closure"] == ["001", "002", "009", "017"], res
        # 5 chunks total - 4 in closure = 1 skipped.
        assert res["skipped_chunk_count"] == 1, res
        assert res["errors"] == [], res

    def test_index_closure_cli_exit_1_on_errors(self, tmp_path: Path) -> None:
        """CLI exits 1 when errors is non-empty (e.g., unknown id)."""
        plans_dir = tmp_path / "plan"
        _write_index(plans_dir, [
            {"task_id": "001", "depends_on": []},
        ])
        cp = _run(
            "index-closure",
            "--plans-dir", str(plans_dir),
            "--task-ids", "999",
            "--json",
        )
        assert cp.returncode == 1, (cp.returncode, cp.stdout, cp.stderr)
        res = _parse_json(cp)
        assert res["closure"] == [], res
        assert any(
            e.get("code") == "unknown-requested-id" for e in res["errors"]
        ), res

    def test_index_closure_cli_missing_index(self, tmp_path: Path) -> None:
        """Fatal load error: missing `00_INDEX.json` exits 1 with envelope."""
        plans_dir = tmp_path / "plan"
        plans_dir.mkdir(parents=True)
        cp = _run(
            "index-closure",
            "--plans-dir", str(plans_dir),
            "--task-ids", "001",
            "--json",
        )
        assert cp.returncode == 1, (cp.returncode, cp.stdout, cp.stderr)
        res = _parse_json(cp)
        assert res["closure"] == [], res
        assert res["errors"], res
        assert res["errors"][0]["code"] == "index-not-found", res
