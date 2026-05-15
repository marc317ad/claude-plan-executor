"""Process-level drift guards for implement-plan run-log ordering.

These tests are deliberately cheap and mostly synthetic. They pin the
orchestrator-facing process contract in SKILL.md so a future prompt edit cannot
silently weaken review-before-commit, resumed-pause closeout, or TASK-008
deferred-test event shape.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SKILL_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md"
)

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402


DEFERRED_TEST_EVENT_KEYS = {"event", "run_id", "task_id", "deferred_to", "note"}


def _process_errors(events: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    event_names = [event.get("event") for event in events]
    paused_run_end_indexes = [
        index
        for index, event in enumerate(events)
        if event.get("event") == "run_end" and event.get("outcome") == "paused"
    ]
    task_cycles: dict[str, dict[str, Any]] = {}

    for index, event in enumerate(events):
        task_id = str(event.get("task_id") or "")
        name = event.get("event")
        if name == "test_deferred":
            keys = set(event)
            if keys != DEFERRED_TEST_EVENT_KEYS:
                errors.append(f"test_deferred keys drifted: {sorted(keys)!r}")
            continue

        if not task_id:
            continue

        cycle = task_cycles.setdefault(
            task_id,
            {
                "review_indexes": [],
                "last_review_done_verdict": None,
                "open_review_index": None,
                "last_commit_index": None,
            },
        )
        if name == "review_start":
            cycle["review_indexes"].append(index)
            cycle["open_review_index"] = index
            continue
        if name in {"review_done", "review_route_called"}:
            cycle["review_indexes"].append(index)
            if (
                name == "review_done"
                and cycle["open_review_index"] is not None
                and cycle["last_commit_index"] is not None
                and cycle["open_review_index"] < cycle["last_commit_index"]
            ):
                errors.append(f"review event follows commit_done for task {task_id}")
            if name == "review_done" and event.get("verdict"):
                cycle["last_review_done_verdict"] = str(event["verdict"])
            if name == "review_done":
                cycle["open_review_index"] = None
            continue

        if name != "commit_done" or not task_id:
            continue

        reviews = cycle["review_indexes"]
        expected_verdict = cycle["last_review_done_verdict"]
        if reviews and not event.get("reviewer_verdict"):
            errors.append(f"commit_done missing reviewer_verdict for reviewed task {task_id}")
        if expected_verdict is not None and expected_verdict != event.get("reviewer_verdict"):
            errors.append(f"commit_done reviewer_verdict drifted for task {task_id}")
        cycle["review_indexes"] = []
        cycle["last_review_done_verdict"] = None
        cycle["last_commit_index"] = index

    if paused_run_end_indexes:
        tail = events[-1]
        if tail.get("event") != "run_end" or tail.get("outcome") not in {"success", "paused"}:
            errors.append("resumed paused run is not closed by final success/paused run_end")

    assert all(name in plan_ops.ALLOWED_LOG_EVENTS for name in event_names)
    return errors


def test_resumed_pause_review_events_precede_commit_and_final_success_supersedes_pause() -> None:
    success_events = [
        {"event": "run_start", "run_id": "R"},
        {"event": "awaiting_user", "run_id": "R", "task_id": "010"},
        {"event": "run_end", "run_id": "R", "outcome": "paused"},
        {"event": "review_start", "run_id": "R", "task_id": "010"},
        {
            "event": "review_done",
            "run_id": "R",
            "task_id": "010",
            "reviewer": "codex",
            "verdict": "clean",
        },
        {
            "event": "review_route_called",
            "run_id": "R",
            "task_id": "010",
            "action": "commit",
        },
        {
            "event": "commit_done",
            "run_id": "R",
            "task_id": "010",
            "reviewer_verdict": "clean",
        },
        {"event": "run_end", "run_id": "R", "outcome": "success"},
    ]

    assert _process_errors(success_events) == []
    paused_again_events = [*success_events[:-1], {"event": "run_end", "run_id": "R", "outcome": "paused"}]
    assert _process_errors(paused_again_events) == []


def test_review_after_commit_or_missing_reviewer_verdict_is_process_drift() -> None:
    commit_before_review = [
        {"event": "run_start", "run_id": "R"},
        {"event": "review_start", "run_id": "R", "task_id": "010"},
        {"event": "commit_done", "run_id": "R", "task_id": "010"},
        {"event": "review_done", "run_id": "R", "task_id": "010", "verdict": "clean"},
        {"event": "run_end", "run_id": "R", "outcome": "success"},
    ]

    errors = _process_errors(commit_before_review)
    assert "commit_done missing reviewer_verdict for reviewed task 010" in errors
    assert "review event follows commit_done for task 010" in errors


def test_multi_cycle_review_commit_flow_is_valid() -> None:
    two_cycle_events = [
        {"event": "run_start", "run_id": "R"},
        {"event": "review_start", "run_id": "R", "task_id": "010"},
        {"event": "review_done", "run_id": "R", "task_id": "010", "verdict": "needs-rework"},
        {
            "event": "review_route_called",
            "run_id": "R",
            "task_id": "010",
            "action": "remediate",
        },
        {
            "event": "commit_done",
            "run_id": "R",
            "task_id": "010",
            "reviewer_verdict": "needs-rework",
        },
        {"event": "review_start", "run_id": "R", "task_id": "010"},
        {"event": "review_done", "run_id": "R", "task_id": "010", "verdict": "clean"},
        {
            "event": "review_route_called",
            "run_id": "R",
            "task_id": "010",
            "action": "commit",
        },
        {
            "event": "commit_done",
            "run_id": "R",
            "task_id": "010",
            "reviewer_verdict": "clean",
        },
        {"event": "run_end", "run_id": "R", "outcome": "success"},
    ]

    assert _process_errors(two_cycle_events) == []


def test_deferred_test_event_shape_is_frozen_and_does_not_count_as_review() -> None:
    deferred_event = {
        "event": "test_deferred",
        "run_id": "R",
        "task_id": "008",
        "deferred_to": "009",
        "note": "sibling owns e2e",
    }
    events = [
        {"event": "run_start", "run_id": "R"},
        deferred_event,
        {
            "event": "commit_done",
            "run_id": "R",
            "task_id": "008",
            "reviewer_verdict": "",
        },
        {"event": "run_end", "run_id": "R", "outcome": "success"},
    ]

    assert _process_errors(events) == []
    assert deferred_event["deferred_to"] == "009"
    assert deferred_event["note"] == "sibling owns e2e"

    drifted = [dict(deferred_event, command="deferred (TASK-009) sibling owns e2e")]
    assert _process_errors(drifted) == [
        "test_deferred keys drifted: "
        "['command', 'deferred_to', 'event', 'note', 'run_id', 'task_id']"
    ]


def test_deferred_test_event_does_not_weaken_review_before_commit() -> None:
    reviewed_deferred = [
        {"event": "run_start", "run_id": "R"},
        {"event": "review_start", "run_id": "R", "task_id": "008"},
        {
            "event": "test_deferred",
            "run_id": "R",
            "task_id": "008",
            "deferred_to": "009",
            "note": "sibling owns e2e",
        },
        {
            "event": "review_done",
            "run_id": "R",
            "task_id": "008",
            "reviewer": "codex",
            "verdict": "minor-findings",
        },
        {
            "event": "review_route_called",
            "run_id": "R",
            "task_id": "008",
            "action": "commit",
        },
        {
            "event": "commit_done",
            "run_id": "R",
            "task_id": "008",
            "reviewer_verdict": "minor-findings",
        },
        {"event": "run_end", "run_id": "R", "outcome": "success"},
    ]

    assert _process_errors(reviewed_deferred) == []

    review_after_commit = [
        reviewed_deferred[0],
        reviewed_deferred[1],
        reviewed_deferred[2],
        reviewed_deferred[5],
        reviewed_deferred[3],
        reviewed_deferred[6],
    ]
    assert _process_errors(review_after_commit) == [
        "review event follows commit_done for task 008",
    ]


def test_skill_documents_the_process_invariants() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    required_fragments = [
        "review_start` / `review_done` / `review_route_called` MUST precede",
        "`commit_done.reviewer_verdict` MUST be populated",
        "later terminal `run_end` whose `outcome` is `success` or `paused`",
        "`test_deferred` from TASK-008",
        "does not satisfy or weaken the review-before-commit ordering rule",
    ]
    missing = [fragment for fragment in required_fragments if fragment not in text]
    assert not missing
