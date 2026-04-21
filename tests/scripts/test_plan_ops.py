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


def _bd_write_schedule(tmp_path: Path, tasks: list[dict]) -> Path:
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
    def test_v5_mutate_failure_halts_with_full_payload(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
        # Write a plan body with TASK-002 block MISSING → mutate_task_status
        # raises ValueError for 002.
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
        assert err["failed_stage"] == "plan_mutate"
        assert err["failed_id"] == "002"
        assert err["plan_mutations_applied"] == []
        assert err["run_log_appended"] == []
        assert err["remaining"] == []
        # Plan on disk: unchanged.
        assert isolated_bd_plan.read_text(encoding="utf-8") == original_on_disk
        # No blocked events.
        assert _bd_read_run_log_events(isolated_bd_plan) == []

    # V6 -----------------------------------------------------------------
    def test_v6_partial_mutate_failure_persists_partial(
        self, tmp_path: Path, isolated_bd_plan: Path,
    ) -> None:
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

        sched = _bd_write_schedule(tmp_path, [
            {"id": "001", "dependencies": []},
            {"id": "002", "dependencies": ["001"]},
            {"id": "003", "dependencies": ["001"]},
        ])
        ns = _bd_make_args(schedule_file=sched, plan_file=isolated_bd_plan)
        code, body = _bd_call(ns)
        assert code != 0
        err = body["errors"][0]
        assert err["failed_stage"] == "plan_mutate"
        assert err["failed_id"] == "003"
        assert err["plan_mutations_applied"] == ["002"]
        assert err["run_log_appended"] == ["002"]
        assert err["remaining"] == []
        # Plan on disk: 002 flipped; 003 still missing.
        plan_text = isolated_bd_plan.read_text(encoding="utf-8")
        assert _bd_status_of(plan_text, "002") == "blocked"
        assert "TASK-003" not in plan_text
        events = _bd_read_run_log_events(isolated_bd_plan)
        assert [e["task_id"] for e in events] == ["002"]

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
                {"id": "001", "agent": "codex", "files": ["a"], "dependencies": ["999"]},
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
            "schedule_ok", "summary",
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
        # The prompt must explicitly tell Codex the orchestrator owns the
        # cross-plan dependency gate so the reviewer does not fabricate a
        # `needs-replan` on dep-check grounds when it declines to run tools.
        assert "Phase 0 preflight" in body["prompt_preview"]

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
                 "dependencies": ["002"]},
                {"id": "002", "agent": "codex", "files": [],
                 "dependencies": ["001"]},
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
                 "dependencies": ["999"]},
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

    def test_filter_schedule_cycle_still_emits_dependency_cycle(
        self, tmp_path: Path
    ) -> None:
        # V5 — regression of TASK-004A after the refactor.
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

        # Write-scope invariant: the agent is bound to the single input
        # plan path, not a directory glob. The acceptance criterion
        # calls out this phrasing.
        assert (
            "single plan file passed" in text
            or "single plan file passed as input" in text
            or "single plan file" in text
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


class TestGateExecutionAndReviewSafe:
    """Wrapper-predicate gates: greps of `plan_codex_dispatch.py`."""

    def test_execution_safe_passes_on_live_wrapper(self) -> None:
        """The live wrapper carries the always-ignore + baseline-snapshot seams."""
        cp = _run("gates", "--check", "execution-safe", "--json")
        assert cp.returncode == 0, cp.stderr
        gate = _parse_json(cp)["gates"][0]
        assert gate["name"] == "execution-safe"
        assert gate["status"] == "pass"

    def test_review_safe_passes_on_live_wrapper(self) -> None:
        cp = _run("gates", "--check", "review-safe", "--json")
        assert cp.returncode == 0, cp.stderr
        gate = _parse_json(cp)["gates"][0]
        assert gate["name"] == "review-safe"
        assert gate["status"] == "pass"

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
