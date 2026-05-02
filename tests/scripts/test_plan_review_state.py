from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"
PY = REPO_ROOT / "venv" / "bin" / "python"
if not PY.exists():
    PY = Path(sys.executable)

sys.path.insert(0, str(SCRIPTS_DIR))

import plan_ops  # noqa: E402


def _schedule(**extra: object) -> dict:
    payload = {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "title": "Task",
                "agent": "codex",
                "files": ["plugins/plan-executor/scripts/plan_ops.py"],
                "dependencies": [],
                "plan_file": "PLAN.md",
            },
        ],
        "batches": [
            {
                "index": 1,
                "task_ids": ["001"],
                "file_locks": ["plugins/plan-executor/scripts/plan_ops.py"],
            },
        ],
    }
    payload.update(extra)
    return payload


def _run_plan_ops(*args: str, input_json: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(PY), str(SCRIPT), *args],
        input=json.dumps(input_json) if input_json is not None else None,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def test_plan_review_state_defaults_match_contract() -> None:
    state = plan_ops._empty_plan_review_state()

    assert state["attempt"] == 1
    assert state["triage_dispatched"] is False
    assert state["auto_revise_round_completed"] is False
    assert state["skipped_reason"] is None
    assert state["load_bearing_indices"] == []
    assert state["dismissed_indices"] == []
    assert state["author_dispatches_completed"] == []


def test_state_helpers_are_pure_and_record_expected_fields() -> None:
    original = {"attempt": 1, "load_bearing_indices": [99]}

    reviewed = plan_ops.record_plan_review_verdict(original, 1, "needs-replan", 3)
    triaged = plan_ops.record_triage_outcome(
        reviewed, "partial-agreement", [0, 2, "bad", True], [1],
    )
    authored = plan_ops.record_author_dispatch(
        triaged, 0, "002", ["docs/plans/child.md"],
    )

    assert original == {"attempt": 1, "load_bearing_indices": [99]}
    assert reviewed["first_verdict"] == "needs-replan"
    assert reviewed["first_findings_count"] == 3
    assert triaged["triage_dispatched"] is True
    assert triaged["triage_verdict"] == "partial-agreement"
    assert triaged["load_bearing_indices"] == [0, 2]
    assert triaged["dismissed_indices"] == [1]
    assert authored["author_dispatches_completed"] == [
        {
            "finding_index": 0,
            "target_task_id": "002",
            "files_edited": ["docs/plans/child.md"],
        },
    ]
    assert authored["auto_revise_round_completed"] is True


def test_apply_plan_review_state_transition_supports_route_output_shape() -> None:
    state = plan_ops.apply_plan_review_state_transition(
        {},
        {
            "record_plan_review_verdict": {
                "attempt": 1,
                "verdict": "needs-replan",
                "findings_count": 2,
            },
            "record_triage_outcome": {
                "verdict": "ship",
                "load_bearing": [],
                "dismissed": [0, 1],
            },
            "skipped_reason": "flag",
        },
    )

    assert state["attempt"] == 1
    assert state["first_verdict"] == "needs-replan"
    assert state["first_findings_count"] == 2
    assert state["triage_dispatched"] is True
    assert state["triage_verdict"] == "ship"
    assert state["dismissed_indices"] == [0, 1]
    assert state["skipped_reason"] == "flag"


def test_old_schedule_first_state_write_creates_defaulted_block(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(_schedule()), encoding="utf-8")

    written, warning = plan_ops._write_plan_review_state_transition(
        schedule_path,
        {"record_plan_review_verdict": {
            "attempt": 1,
            "verdict": "approved-with-notes",
            "findings_count": 4,
        }},
    )

    assert written is True
    assert warning is None
    data = json.loads(schedule_path.read_text(encoding="utf-8"))
    state = data["plan_review_state"]
    assert state["attempt"] == 1
    assert state["first_verdict"] == "approved-with-notes"
    assert state["first_findings_count"] == 4
    assert state["triage_dispatched"] is False
    assert state["auto_revise_round_completed"] is False
    assert state["skipped_reason"] is None


def test_read_plan_review_state_normalizes_missing_or_old_schedule(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(_schedule()), encoding="utf-8")

    state = plan_ops.read_plan_review_state(schedule_path)

    assert state == plan_ops._empty_plan_review_state()


def test_write_and_parse_schedule_round_trip_plan_review_state(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    payload = _schedule(
        plan_review_state={
            "attempt": 2,
            "first_verdict": "needs-replan",
            "first_findings_count": 2,
            "triage_dispatched": True,
            "triage_verdict": "partial-agreement",
            "load_bearing_indices": [0],
            "dismissed_indices": [1],
            "author_dispatches_completed": [
                {
                    "finding_index": 0,
                    "target_task_id": "001",
                    "files_edited": ["PLAN.md"],
                },
            ],
            "auto_revise_round_completed": True,
            "skipped_reason": None,
        },
    )

    write = _run_plan_ops(
        "write-schedule", "--schedule-file", str(schedule_path), "--stdin", "--json",
        input_json=payload,
    )
    assert write.returncode == 0, write.stderr or write.stdout

    parse = _run_plan_ops("parse-schedule", "--stdin", "--json", input_json=payload)
    assert parse.returncode == 0, parse.stderr or parse.stdout
    parsed = json.loads(parse.stdout)
    assert parsed["plan_review_state"] == payload["plan_review_state"]

    persisted = json.loads(schedule_path.read_text(encoding="utf-8"))
    assert persisted["plan_review_state"] == payload["plan_review_state"]


def test_plan_review_route_update_schedule_state_cli(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(_schedule()), encoding="utf-8")
    route_payload = {
        "stage": "post_review",
        "claude_only": False,
        "flags": {
            "skip_plan_review": False,
            "codex_plan_review_binding": False,
            "no_auto_revise": False,
            "allow_gaps": False,
        },
        "plan_review_envelope": {
            "outcome": "success",
            "reviewer": "codex",
            "verdict": "needs-replan",
            "findings": [{"target_task_id": "001", "concern": "fix"}],
            "notes": [],
            "summary": "summary",
            "schedule_ok": False,
        },
    }

    cp = _run_plan_ops(
        "plan-review-route",
        "--stdin",
        "--json",
        "--update-schedule-state",
        str(schedule_path),
        input_json=route_payload,
    )

    assert cp.returncode == 0, cp.stderr or cp.stdout
    directive = json.loads(cp.stdout)
    assert directive["action"] == "dispatch_triage"
    assert directive["schedule_state_updated"] is True
    state = json.loads(schedule_path.read_text(encoding="utf-8"))["plan_review_state"]
    assert state["first_verdict"] == "needs-replan"
    assert state["first_findings_count"] == 1
    assert state["triage_dispatched"] is True


def test_atomic_plan_review_state_writes_do_not_leave_torn_json(tmp_path: Path) -> None:
    schedule_path = tmp_path / "schedule.json"
    schedule_path.write_text(json.dumps(_schedule()), encoding="utf-8")

    def write(verdict: str) -> None:
        plan_ops._write_plan_review_state_transition(
            schedule_path,
            {
                "record_plan_review_verdict": {
                    "attempt": 1,
                    "verdict": verdict,
                    "findings_count": 1,
                },
            },
        )

    threads = [
        threading.Thread(target=write, args=("approved",)),
        threading.Thread(target=write, args=("needs-replan",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    data = json.loads(schedule_path.read_text(encoding="utf-8"))
    assert data["plan_review_state"]["first_verdict"] in {
        "approved",
        "needs-replan",
    }
