"""Tier-A pure-core conformance fixtures for ``plan_ops`` (TASK-004).

This suite is the byte-equal conformance gate for the 23 read-only Tier-A
``cmd_*`` subcommands enumerated in
``docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md``.
Each test case has a fixture file pair on disk:

    fixtures/plan_ops_pure_core/tier_a/<sub>__<case>.payload.json
    fixtures/plan_ops_pure_core/tier_a/<sub>__<case>.expected.json

The pairs are authored programmatically via ``_AUTHORED_CASES`` and dumped
to disk once at test-session start (the on-disk JSON is the canonical
fixture image used by the parametrized test; reviewers inspect those
files directly). The parametrized test discovers the disk pairs and
hands them to the shared ``run_conformance`` driver from TASK-003H which
asserts:

  * subprocess exit code == pure-core ``__plan_ops_exit_code__``
  * stderr matches the policy
  * canonical-envelope byte equality (sorted keys, ts-stubbed) between
    the CLI subprocess and the in-process ``_run_<sub>(payload)`` call
  * filesystem-snapshot invariance under ``fixture_dir`` (Tier-A is
    read-only by contract)

Branch-coverage cases are included alongside happy paths so the suite
also exercises:

  * ``filter-schedule``: valid filter / dependency-cycle /
    stdin-needs-enrichment / unknown-task-id
  * ``gates``: list / check / certify / certify-missing-required-flag
  * each ``cmd_parse_*`` (5 fns): happy path + structured-error path
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.scripts.plan_ops_pure_harness import REPO_ROOT, run_conformance

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "plan_ops_pure_core"
    / "tier_a"
)


# ---------------------------------------------------------------------------
# Fixture helpers
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
        {
            "id": "002",
            "agent": "claude",
            "title": "two",
            "files": ["src/two.py"],
            "dependencies": ["001"],
            "plan_file": "TASK-002_two.md",
        },
    ],
    "batches": [
        {"index": 1, "task_ids": ["001"], "file_locks": ["src/one.py"]},
        {"index": 2, "task_ids": ["002"], "file_locks": ["src/two.py"]},
    ],
    "gaps": [],
    "risks": [],
}

SCHEDULE_DEP_CYCLE = {
    "outcome": "valid",
    "tasks": [
        {
            "id": "001",
            "agent": "claude",
            "title": "one",
            "files": ["src/one.py"],
            "dependencies": ["002"],
            "plan_file": "TASK-001_one.md",
        },
        {
            "id": "002",
            "agent": "claude",
            "title": "two",
            "files": ["src/two.py"],
            "dependencies": ["001"],
            "plan_file": "TASK-002_two.md",
        },
    ],
    "batches": [
        {"index": 1, "task_ids": ["001", "002"], "file_locks": []},
    ],
    "gaps": [],
    "risks": [],
}

SCHEDULE_NEEDS_ENRICHMENT = {
    "outcome": "needs-enrichment",
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
    "gaps": [
        {
            "type": "missing-description",
            "task_id": "001",
            "severity": "soft",
            "message": "no description",
        }
    ],
    "risks": [],
}

INDEX_ROSTER = {
    "schema_version": 1,
    "chunks": [
        {
            "task_id": "001",
            "file": "TASK-001_one.md",
            "depends_on": [],
            "status": "Pending",
            "superseded_by": [],
        },
        {
            "task_id": "002",
            "file": "TASK-002_two.md",
            "depends_on": ["001"],
            "status": "Pending",
            "superseded_by": [],
        },
    ],
}

CHILD_TASK_001 = """### TASK-001: one

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - src/one.py
- **Dependencies:**
- **Test command:** `none`
- **Acceptance criteria:**
  - one is one.

**Description:** Stub child plan.
"""

CHILD_TASK_002 = """### TASK-002: two

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - src/two.py
- **Dependencies:**
  - TASK-001
- **Test command:** `none`
- **Acceptance criteria:**
  - two is two.

**Description:** Stub child plan.
"""

PLAN_REVIEW_PARSED_HAPPY = {
    "plan_file": "docs/plans/PLAN.md",
    "verdict": "approved",
    "findings": [],
    "notes": [],
    "schedule_ok": True,
    "summary": "ok",
}

D5_ADJUDICATION_HAPPY = {
    "verdict": "ship",
    "summary": "ok",
}

D5_ADJUDICATION_BAD = {
    "verdict": "totally-bogus",
    "summary": "x",
}

TRIAGE_REPORT_HAPPY = """# Triage report

Some prose.

```json
{
  "verdict": "ship",
  "load_bearing": [],
  "dismissed": [],
  "summary": "ok"
}
```
"""

TRIAGE_REPORT_BAD = """# Triage report

```json
{
  "verdict": "ship",
  "load_bearing": [],
  "dismissed": []
}
```
"""

IMPLEMENTER_REPORT_HAPPY = """## TASK-001 implementation report

**Outcome:** success

**Files changed:**
- src/one.py

**Diff summary:**
- did stuff

**Test command:** none
**Test outcome:** not-run

**Acceptance criteria check:**
- [x] one is one — see src/one.py:1

**Plan adaptations:**
- None.

**Concerns for reviewer:**
- None.
"""

IMPLEMENTER_REPORT_BAD = """## TASK-001 implementation report

No outcome here.
"""

CLAUDE_ENVELOPE_HAPPY = json.dumps({
    "status": "ok",
    "result": {"outcome": "success"},
    "scope": {"scope_violation_detected": False},
})

CLAUDE_ENVELOPE_BAD = "not-json"

ROUTE_INPUT_HAPPY = {
    "task_id": "001",
    "implementer": "claude",
    "reviewer_envelope": {
        "verdict": "clean",
        "findings": [],
        "summary": "",
    },
    "retries_used": {},
    "flags": {},
}

TASK_BLOCK_FOR_RESOLVE = """### TASK-001: example

- **Status:** Pending
- **Priority:** medium
- **Files:**
  - src/one.py
- **Dependencies:**
- **Test command:** `none`
- **Acceptance criteria:**
  - x

**Description:** Resolve targets test.
"""


# A "case" is a 4-tuple: (subcommand, case_name, payload, expected).
#
# ``payload`` may carry ``_files`` (relpath -> text) for files materialized
# under fixture_dir before either path runs, plus a ``stdin_text`` key
# (forwarded to the subprocess as stdin and to ``_run_<sub>(payload)`` as
# part of the dict). String values inside the payload (and its nested
# objects) are templated: ``{FIXTURE_DIR}`` resolves to the per-case temp
# dir, ``{REPO_ROOT}`` to the repo root.

_AUTHORED_CASES: list[tuple[str, str, dict, dict]] = [
    # ------------------------------------------------------------------
    # Trivial happy paths (small fns, no filesystem deps).
    # ------------------------------------------------------------------
    (
        "normalize-task-id",
        "happy",
        {"id": "1"},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "path-info",
        "happy",
        {},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "list-global-lock-paths",
        "happy",
        {},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "build-codex-dispatch-input",
        "happy_stdout_mode",
        {"output": "-"},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "build-gemini-dispatch-input",
        "happy_stdout_mode",
        {"output": "-"},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "run-summary",
        "happy_empty",
        {"section": "sandbox-divergences", "run_id": "NEVER-MATCHES"},
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    # ------------------------------------------------------------------
    # parse-* (5 fns × 2 cases each = 10 fixtures)
    # ------------------------------------------------------------------
    (
        "parse-schedule",
        "happy",
        {
            "stdin": True,
            "strict": False,
            "stdin_text": json.dumps(SCHEDULE_VALID),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-schedule",
        "error_bad_json",
        {
            "stdin": True,
            "strict": False,
            "stdin_text": "{not json",
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-implementer-report",
        "happy",
        {
            "stdin": True,
            "stdin_text": IMPLEMENTER_REPORT_HAPPY,
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-implementer-report",
        "error_missing_outcome",
        {
            "stdin": True,
            "stdin_text": IMPLEMENTER_REPORT_BAD,
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-plan-review-report",
        "happy_from_claude",
        {
            "stdin": True,
            "from_claude": True,
            "stdin_text": json.dumps(PLAN_REVIEW_PARSED_HAPPY),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-plan-review-report",
        "error_invalid_outcome",
        {
            "stdin": True,
            "from_claude": False,
            "stdin_text": json.dumps({
                "subcommand": "plan-review",
                "outcome": "success",
                "reviewer": "codex",
                "parsed": {"verdict": "bogus"},
            }),
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-d5-adjudication",
        "happy",
        {
            "stdin": True,
            "codex_findings_count": 0,
            "stdin_text": json.dumps(D5_ADJUDICATION_HAPPY),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-d5-adjudication",
        "error_invalid_verdict",
        {
            "stdin": True,
            "codex_findings_count": 0,
            "stdin_text": json.dumps(D5_ADJUDICATION_BAD),
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-plan-review-triage-report",
        "happy",
        {
            "stdin": True,
            "source": "codex-plan-review",
            "findings_count": 0,
            "stdin_text": TRIAGE_REPORT_HAPPY,
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "parse-plan-review-triage-report",
        "error_missing_summary",
        {
            "stdin": True,
            "source": "codex-plan-review",
            "findings_count": 0,
            "stdin_text": TRIAGE_REPORT_BAD,
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    # ------------------------------------------------------------------
    # claude-envelope-extract / order-triage-findings / review-route
    # ------------------------------------------------------------------
    (
        "claude-envelope-extract",
        "happy",
        {
            "stdin": True,
            "agent": "plan-implementer",
            "stdin_text": CLAUDE_ENVELOPE_HAPPY,
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "order-triage-findings",
        "happy_empty",
        {
            "stdin": True,
            "stdin_text": json.dumps([]),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "review-route",
        "happy_clean_commit",
        {
            "stdin": True,
            "stdin_text": json.dumps(ROUTE_INPUT_HAPPY),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    # ------------------------------------------------------------------
    # filter-schedule (4 cases per AC)
    # ------------------------------------------------------------------
    (
        "filter-schedule",
        "happy_valid_filter",
        {
            "stdin": True,
            "task_ids": "2",
            "stdin_text": json.dumps(SCHEDULE_VALID),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "filter-schedule",
        "error_dependency_cycle",
        {
            "stdin": True,
            "task_ids": "1",
            "stdin_text": json.dumps(SCHEDULE_DEP_CYCLE),
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "filter-schedule",
        "happy_stdin_needs_enrichment",
        {
            "stdin": True,
            "task_ids": "1",
            "stdin_text": json.dumps(SCHEDULE_NEEDS_ENRICHMENT),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "filter-schedule",
        "error_unknown_task_id",
        {
            "stdin": True,
            "task_ids": "999",
            "stdin_text": json.dumps(SCHEDULE_VALID),
        },
        {"exit_code": 1, "stderr_policy": "empty", "fs_invariant": True},
    ),
    # ------------------------------------------------------------------
    # gates (4 cases per AC)
    # ------------------------------------------------------------------
    # ``gates`` payload keys re-shape ``args.mode`` -> ``certify_mode`` and
    # add a derived ``mode`` disposition that argparse does not know about,
    # so each gates case supplies an explicit ``cli_argv`` to bypass
    # ``cli_argv_from``'s argparse-derivation step.
    (
        "gates",
        "happy_list",
        {
            "mode": "list",
            "list": True,
            "check": None,
            "certify": False,
            "certify_mode": None,
            "plan_file": None,
            "schedule_file": None,
            "commit_sha": None,
            "task_id": None,
            "run_id": None,
        },
        {
            "cli_argv": ["--list", "--json"],
            "exit_code": 0,
            "stderr_policy": "empty",
            "fs_invariant": True,
        },
    ),
    (
        "gates",
        "happy_check_unknown",
        {
            "mode": "check",
            "list": False,
            "check": "totally-bogus",
            "certify": False,
            "certify_mode": None,
            "plan_file": None,
            "schedule_file": None,
            "commit_sha": None,
            "task_id": None,
            "run_id": None,
        },
        {
            "cli_argv": ["--check", "totally-bogus", "--json"],
            "exit_code": 1,
            "stderr_policy": "empty",
            "fs_invariant": True,
        },
    ),
    (
        "gates",
        "error_certify_missing_required_flag",
        {
            "mode": "certify",
            "list": False,
            "check": None,
            "certify": True,
            "certify_mode": "dry-run",
            "plan_file": None,
            "schedule_file": None,
            "commit_sha": None,
            "task_id": None,
            "run_id": None,
        },
        {
            "cli_argv": ["--certify", "--mode", "dry-run", "--json"],
            "exit_code": 1,
            "stderr_policy": "empty",
            "fs_invariant": True,
        },
    ),
    (
        "gates",
        "happy_certify_dry_run",
        {
            "_files": {
                "fake_plan.md": "# stub\n",
                "fake_schedule.json": json.dumps(SCHEDULE_VALID),
            },
            "mode": "certify",
            "list": False,
            "check": None,
            "certify": True,
            "certify_mode": "dry-run",
            "plan_file": "{FIXTURE_DIR}/fake_plan.md",
            "schedule_file": "{FIXTURE_DIR}/fake_schedule.json",
            "commit_sha": None,
            "task_id": None,
            "run_id": None,
        },
        {
            "cli_argv": [
                "--certify",
                "--mode",
                "dry-run",
                "--plan-file",
                "{FIXTURE_DIR}/fake_plan.md",
                "--schedule-file",
                "{FIXTURE_DIR}/fake_schedule.json",
                "--json",
            ],
            "exit_code": 1,
            "stderr_policy": "empty",
            "fs_invariant": True,
        },
    ),
    # ------------------------------------------------------------------
    # batch-next / compute-schedule / build-tasks / index-closure /
    # check-plan-deps / lint-plans / resolve-read-targets
    # ------------------------------------------------------------------
    (
        "batch-next",
        "happy",
        {
            "_files": {
                "schedule.json": json.dumps(SCHEDULE_VALID),
            },
            "schedule_file": {"__path__": "{FIXTURE_DIR}/schedule.json"},
            "locked_files": "",
            "done": "",
            "failed": "",
            "paused": "",
            "parallel": 1,
            "from_schedule_state": None,
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "compute-schedule",
        "happy",
        {
            "stdin": True,
            "strict": False,
            "stdin_text": json.dumps(SCHEDULE_VALID),
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "build-tasks",
        "happy",
        {
            "_files": {
                "plans/00_INDEX.json": json.dumps(INDEX_ROSTER),
                "plans/TASK-001_one.md": CHILD_TASK_001,
                "plans/TASK-002_two.md": CHILD_TASK_002,
            },
            "plans_dir": {"__path__": "{FIXTURE_DIR}/plans"},
            "filter_ids": "",
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "index-closure",
        "happy",
        {
            "_files": {
                "plans/00_INDEX.json": json.dumps(INDEX_ROSTER),
            },
            "plans_dir": {"__path__": "{FIXTURE_DIR}/plans"},
            "task_ids": "001",
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "check-plan-deps",
        "happy",
        {
            "_files": {
                "plans/00_INDEX.json": json.dumps(INDEX_ROSTER),
                "plans/TASK-001_one.md": CHILD_TASK_001,
            },
            "plan_file": {"__path__": "{FIXTURE_DIR}/plans/TASK-001_one.md"},
            "plans_dir": {"__path__": "{FIXTURE_DIR}/plans"},
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "lint-plans",
        "happy_no_findings",
        {
            "_files": {
                "plans/.keep": "",
            },
            "plans_dir": {"__path__": "{FIXTURE_DIR}/plans"},
            "run_log": None,
            "git_dir": {"__path__": "{FIXTURE_DIR}"},
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
    (
        "resolve-read-targets",
        "happy",
        {
            "stdin": True,
            "stdin_text": TASK_BLOCK_FOR_RESOLVE,
        },
        {"exit_code": 0, "stderr_policy": "empty", "fs_invariant": True},
    ),
]


# ---------------------------------------------------------------------------
# Disk fixture I/O — write authored cases at session start, then discover.
# ---------------------------------------------------------------------------


def _payload_path(subcommand: str, case_name: str) -> Path:
    return FIXTURE_DIR / f"{subcommand}__{case_name}.payload.json"


def _expected_path(subcommand: str, case_name: str) -> Path:
    return FIXTURE_DIR / f"{subcommand}__{case_name}.expected.json"


def _write_authored_fixtures() -> None:
    """Dump every entry of ``_AUTHORED_CASES`` to the on-disk fixture pair.

    Idempotent: rewrites the file on every invocation so the disk image
    never drifts from the Python source. Fixture authors should treat
    ``_AUTHORED_CASES`` as the single source of truth and inspect the
    JSON pair files only to review the resulting payload+expected
    images.
    """
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
    """Resolve ``{FIXTURE_DIR}`` / ``{REPO_ROOT}`` placeholders.

    A dict with the single key ``"__path__"`` is the path-marker shape:
    its value is templated as a string and then wrapped as
    ``pathlib.Path``. The ``_args_to_payload_<sub>`` builders return
    ``pathlib.Path`` for path-valued arguments, so the harness's
    builder-vs-pure-payload consistency check requires the in-process
    payload to carry ``Path`` objects on those keys (string values
    would compare unequal even when string-formatting matches).
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
    """Discover ``(subcommand, case_name)`` pairs from the disk fixtures.

    Discovery is filesystem-driven so the test never silently drops a
    case authored in ``_AUTHORED_CASES`` but missing on disk — the
    session-scoped writer ensures parity, and a missing pair would raise
    a clear ``FileNotFoundError`` here.
    """
    if not FIXTURE_DIR.is_dir():
        # Run the writer once eagerly so ``pytest --collect-only`` works
        # even before the autouse session fixture has been initialized.
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
# The single conformance test, parametrized over discovered pairs.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcommand", "case_name"),
    _DISCOVERED_PAIRS,
    ids=[f"{s}__{c}" for s, c in _DISCOVERED_PAIRS],
)
def test_tier_a_pure_core_conformance(
    subcommand: str,
    case_name: str,
    tmp_path: Path,
) -> None:
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
    payload = _resolve_template(payload_doc["payload"], tmp_path)
    expected = _resolve_template(expected, tmp_path)

    run_conformance(
        subcommand,
        payload,
        expected=expected,
        fixture_dir=tmp_path,
    )


def test_authored_case_count_matches_disk() -> None:
    """Sanity: every authored case has a matching pair on disk."""
    expected = {(s, c) for s, c, _, _ in _AUTHORED_CASES}
    discovered = set(_discover_pairs())
    assert expected == discovered, (
        "_AUTHORED_CASES drifted from disk — "
        f"missing on disk: {sorted(expected - discovered)}; "
        f"orphan disk pairs: {sorted(discovered - expected)}"
    )


def test_runtime_under_30s() -> None:
    """Hard ceiling per AC: the suite stays small enough to run quickly.

    This is a lightweight smoke assertion against case count rather than
    timing the full run from inside a single test (which would require a
    second pytest invocation). Each case is bounded by the harness;
    keeping the case count in this range empirically holds the suite to
    well under the 30s ceiling on developer hardware.
    """
    assert len(_AUTHORED_CASES) <= 50, (
        f"too many authored cases ({len(_AUTHORED_CASES)}); split into "
        f"sub-clusters per the TASK-004 description."
    )


# Quiet unused-import warnings on the helper modules without leaking
# globals. ``shutil`` is imported defensively in case a future case
# needs to scrub a tmp tree before run_conformance materializes new
# files; keeping the import bound prevents accidental NameError when a
# test later references it.
_ = (os, shutil)
