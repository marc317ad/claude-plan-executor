"""Unit tests for schedule-state persistence (TASK-002 PHASE_D_STATE_MACHINE).

The state helpers are pure functions on in-memory dicts:
    read_schedule_state(path) -> dict
    apply_commit_state_transition(state, task_id, sha, files) -> dict
    apply_fail_state_transition(state, task_id, retries) -> dict
    apply_blocked_state_transition(state, blocked_ids) -> dict

The CLI flags --update-schedule-state / --from-schedule-state are thin
file-IO shims around `_write_schedule_state` + `read_schedule_state`.
These tests exercise the helpers directly + a small set of file-shim
tests that hit the atomic-write path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import plan_ops  # noqa: E402


# ---------------------------------------------------------------------------
# Pure-function tests — no tempfile, no subprocess.
# ---------------------------------------------------------------------------


def test_empty_state_has_every_subfield():
    s = plan_ops._empty_schedule_state()
    assert set(s.keys()) == {
        "done", "failed", "blocked", "committed",
        "locked_files", "review_notes", "retries_used",
    }


def test_normalize_state_defaults_missing_subfields():
    s = plan_ops._normalize_state({"done": ["001"]})
    assert s["done"] == ["001"]
    assert s["failed"] == []
    assert s["committed"] == []
    assert s["review_notes"] == {}
    assert s["retries_used"] == {}


def test_normalize_state_handles_none_or_garbage():
    assert plan_ops._normalize_state(None) == plan_ops._empty_schedule_state()
    assert plan_ops._normalize_state("not a dict") == plan_ops._empty_schedule_state()


def test_apply_commit_transition_promotes_done_and_appends_committed():
    state = plan_ops._empty_schedule_state()
    state["locked_files"] = ["a.py", "b.py"]
    new = plan_ops.apply_commit_state_transition(state, "001", "abc123", ["a.py"])
    assert new["done"] == ["001"]
    assert new["committed"] == [{"task_id": "001", "sha": "abc123"}]
    assert new["locked_files"] == ["b.py"]
    # Input is unchanged.
    assert state["done"] == []
    assert state["locked_files"] == ["a.py", "b.py"]


def test_apply_commit_transition_idempotent_on_done():
    state = plan_ops._normalize_state({"done": ["001"]})
    new = plan_ops.apply_commit_state_transition(state, "001", "sha", [])
    # `done` not duplicated.
    assert new["done"] == ["001"]


def test_apply_fail_transition_appends_failed_and_persists_retries():
    state = plan_ops._empty_schedule_state()
    new = plan_ops.apply_fail_state_transition(
        state, "003", {"bounded_remediation": True},
    )
    assert new["failed"] == ["003"]
    assert new["retries_used"] == {"003": {"bounded_remediation": True}}


def test_apply_fail_transition_retries_none_leaves_map_untouched():
    state = plan_ops._normalize_state({"retries_used": {"003": {"bounded_remediation": True}}})
    new = plan_ops.apply_fail_state_transition(state, "004", None)
    # 004 added to failed; retries_used dict unchanged for 003 and absent for 004.
    assert new["failed"] == ["004"]
    assert new["retries_used"] == {"003": {"bounded_remediation": True}}


def test_apply_fail_transition_idempotent_on_failed():
    state = plan_ops._normalize_state({"failed": ["004"]})
    new = plan_ops.apply_fail_state_transition(state, "004", None)
    assert new["failed"] == ["004"]


def test_apply_blocked_transition_appends_dedup():
    state = plan_ops._normalize_state({"blocked": ["005"]})
    new = plan_ops.apply_blocked_state_transition(state, ["005", "006", "007", "006"])
    assert new["blocked"] == ["005", "006", "007"]


# ---------------------------------------------------------------------------
# Schedule schema acceptance — write-schedule + parse-schedule round-trip.
# ---------------------------------------------------------------------------


def _minimal_valid_schedule(state: dict | None = None) -> dict:
    s = {
        "outcome": "valid",
        "tasks": [{
            "id": "001",
            "files": ["a.py"],
            "agent": "claude",
            "plan_file": "PLAN.md",
        }],
        "batches": [{"index": 0, "task_ids": ["001"], "file_locks": ["a.py"]}],
        "gaps": [],
        "risks": [],
    }
    if state is not None:
        s["state"] = state
    return s


def test_validate_schedule_accepts_optional_state():
    sched = _minimal_valid_schedule(state={
        "done": ["000"], "failed": [], "blocked": [],
        "committed": [{"task_id": "000", "sha": "abc"}],
        "locked_files": [], "review_notes": {}, "retries_used": {},
    })
    errors, warnings = plan_ops._validate_schedule(sched)
    assert errors == [], errors


def test_validate_schedule_accepts_partial_state():
    # Every subfield optional.
    sched = _minimal_valid_schedule(state={"done": ["001"]})
    errors, _ = plan_ops._validate_schedule(sched)
    assert errors == []


def test_validate_schedule_rejects_state_not_dict():
    sched = _minimal_valid_schedule()
    sched["state"] = "not a dict"
    errors, _ = plan_ops._validate_schedule(sched)
    assert any(e["code"] == "invalid-type" and e["path"] == "$.state" for e in errors)


def test_validate_schedule_rejects_state_done_not_list():
    sched = _minimal_valid_schedule(state={"done": "001"})
    errors, _ = plan_ops._validate_schedule(sched)
    assert any(e["path"] == "$.state.done" for e in errors)


def test_validate_schedule_rejects_state_retries_used_not_dict():
    sched = _minimal_valid_schedule(state={"retries_used": []})
    errors, _ = plan_ops._validate_schedule(sched)
    assert any(e["path"] == "$.state.retries_used" for e in errors)


def test_validate_schedule_strict_rejects_unknown_state_field():
    sched = _minimal_valid_schedule(state={"made_up": []})
    errors, _ = plan_ops._validate_schedule(sched, strict_nested=True)
    assert any(e["code"] == "unknown-nested-field" for e in errors)


def test_validate_schedule_lenient_warns_unknown_state_field():
    sched = _minimal_valid_schedule(state={"made_up": []})
    errors, warnings = plan_ops._validate_schedule(sched)
    assert errors == []
    assert any("made_up" in w for w in warnings)


def test_old_schedule_without_state_loads_clean():
    # No `state` key at all — backward compatibility.
    sched = _minimal_valid_schedule()
    assert "state" not in sched
    errors, _ = plan_ops._validate_schedule(sched)
    assert errors == []


# ---------------------------------------------------------------------------
# File-IO shim tests — minimal tempfile coverage of read/write paths.
# ---------------------------------------------------------------------------


def test_read_schedule_state_missing_file_returns_empty(tmp_path: Path):
    s = plan_ops.read_schedule_state(tmp_path / "does-not-exist.json")
    assert s == plan_ops._empty_schedule_state()


def test_read_schedule_state_round_trips_via_write(tmp_path: Path):
    sched_path = tmp_path / "schedule.json"
    sched = _minimal_valid_schedule(state=plan_ops._empty_schedule_state())
    sched_path.write_text(json.dumps(sched), encoding="utf-8")

    new_state = plan_ops.apply_commit_state_transition(
        plan_ops._empty_schedule_state(), "001", "sha-abc", ["a.py"],
    )
    written, warning = plan_ops._write_schedule_state(sched_path, new_state)
    assert written is True
    assert warning is None

    reread = plan_ops.read_schedule_state(sched_path)
    assert reread["done"] == ["001"]
    assert reread["committed"] == [{"task_id": "001", "sha": "sha-abc"}]
    # The non-state fields of the schedule are preserved.
    on_disk = json.loads(sched_path.read_text(encoding="utf-8"))
    assert on_disk["outcome"] == "valid"
    assert on_disk["tasks"][0]["id"] == "001"


def test_write_schedule_state_on_missing_schedule_no_ops_with_warning(tmp_path: Path):
    written, warning = plan_ops._write_schedule_state(
        tmp_path / "no-such.json", plan_ops._empty_schedule_state(),
    )
    assert written is False
    assert warning is not None
    assert "no-such.json" in warning or "not found" in warning


def test_write_schedule_state_on_garbage_schedule_no_ops_with_warning(tmp_path: Path):
    p = tmp_path / "bad.json"
    p.write_text("not json", encoding="utf-8")
    written, warning = plan_ops._write_schedule_state(
        p, plan_ops._empty_schedule_state(),
    )
    assert written is False
    assert warning is not None


def test_read_schedule_state_old_schedule_returns_empty(tmp_path: Path):
    p = tmp_path / "schedule.json"
    p.write_text(json.dumps(_minimal_valid_schedule()), encoding="utf-8")
    s = plan_ops.read_schedule_state(p)
    # Old schedule with no `state` key → empty (every subfield present).
    assert s == plan_ops._empty_schedule_state()


def test_write_schedule_state_on_legacy_schedule_no_ops_with_warning(tmp_path: Path):
    # AC: state-write on an old schedule (no `state` key) must no-op + warn,
    # and must not modify the on-disk file.
    p = tmp_path / "schedule.json"
    p.write_text(json.dumps(_minimal_valid_schedule()), encoding="utf-8")
    before = p.read_bytes()

    new_state = plan_ops.apply_commit_state_transition(
        plan_ops._empty_schedule_state(), "001", "sha-abc", ["a.py"],
    )
    written, warning = plan_ops._write_schedule_state(p, new_state)
    assert written is False
    assert warning is not None and warning != ""
    assert p.read_bytes() == before
