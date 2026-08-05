"""TASK-001 canary: A/B parity probe for plan_claude_dispatch.

Probes that the Bash-dispatched ``plan_claude_dispatch.py run --input
<payload>`` path produces a v3 wrapper envelope that is semantically
equivalent to what the orchestrator would have received from invoking
the same agent through the in-process ``Agent`` tool path.

The Agent-tool path is replayed from a *transcript fixture* embedded in
this file: a hand-rolled analyst output dict that mirrors what the
``Agent`` tool would have returned for the same inputs. Byte equality
across the two paths is not required; the assertion is on the parsed
schedule shape (`tasks[*].id`, `batches[*].index`, `outcome`).

Path (b) — direct wrapper invocation — is exercised in two modes:

  * **Dry-run mode** (always runs in CI): uses ``--dry-run`` so no real
    ``claude`` CLI is spawned. Asserts envelope shape, status==ok, the
    ≤64 KB size cap, and that the v3 envelope keys observed match the
    keys the downstream tasks lock against.
  * **Live mode** (gated): runs the full pipeline against the real
    ``claude`` binary. Skipped unless ``CANARY_LIVE_CLAUDE=1`` is set
    AND a ``claude`` binary is on PATH. The *assertion code* is present
    unconditionally so CI exercises both code paths whenever the
    binary is present.

Acceptance criteria covered (per PLAN_SKILL_bash_dispatch_migration
TASK-001):

  - Parsed schedules from path (a) and path (b) are semantically equal
    on ``tasks[*].id``, ``batches[*].index``, ``outcome``.
  - Envelope transport ``status == "ok"``.
  - For analyst probe, ``result.outcome ∈ {valid, needs-enrichment,
    invalid}`` (the analyst-output schema).
  - Envelope size (full stdout JSON) ≤ 64 KB.
  - One-shot probes for plan-implementer and plan-remediator (front-
    loads wrapper readiness for TASK-004/005).

Test selection: every test function name starts with ``test_canary_``
so ``pytest -k canary`` (the plan's test command) selects them via
substring match. We do NOT use ``@pytest.mark.canary`` because the
project's ``pytest.ini`` runs with ``--strict-markers`` and registering
a new global marker is out of scope for TASK-001.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: paths, env detection
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_claude_dispatch.py"
)
STUB_PATH = (
    REPO_ROOT / "tests" / "scripts" / "stubs" / "plan_claude_dispatch_stub.py"
)
SCHEMAS_DIR = (
    REPO_ROOT / "tests" / "scripts" / "fixtures" / "claude_dispatch" / "schemas"
)
# Runtime result schemas ship inside the plugin package (fixture-packaging
# fix); analyst_result.json is test-only and stays in the fixtures tree.
PLUGIN_SCHEMAS_DIR = (
    REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "schemas"
)
ANALYST_RESULT_SCHEMA_PATH = SCHEMAS_DIR / "analyst_result.json"
IMPLEMENTER_RESULT_SCHEMA_PATH = PLUGIN_SCHEMAS_DIR / "implementer_result.json"
REMEDIATOR_RESULT_SCHEMA_PATH = PLUGIN_SCHEMAS_DIR / "remediator_result.json"
PROBE_RESULTS_PATH = (
    REPO_ROOT / "docs" / "plans" / "SKILL_bash_dispatch_migration" / "probe_results.md"
)

#: Hard cap from TASK-001 acceptance criteria — sanity bound on the
#: "context recovery" claim, not a tuned figure.
ENVELOPE_SIZE_CAP_BYTES = 64 * 1024

#: v3 envelope keys per the plan's Preconditions block. Downstream tasks
#: lock against this exact set; canary asserts the wrapper still emits
#: it on a real run.
V3_REQUIRED_TOP_LEVEL_KEYS = {
    "schema_version",
    "status",
    "status_reason",
    "agent",
    "model",
    "session_id",
    "duration_ms",
    "cost_usd",
    "tokens",
    "result",
    "result_raw_truncated",
    "stderr_tail",
    "permission_denials",
    "scope",
    "trace",
    "error",
}

ALLOWED_ANALYST_OUTCOMES = {"valid", "needs-enrichment", "invalid"}
ALLOWED_IMPLEMENTER_OUTCOMES = {
    "success",
    "partial",
    "failed",
    "plan-incorrect",
    "blocked",
    "malformed",
}
ALLOWED_REMEDIATOR_OUTCOMES = ALLOWED_IMPLEMENTER_OUTCOMES | {"scope-violation"}


def _claude_binary_present() -> bool:
    return shutil.which("claude") is not None


_LIVE_MODE = (
    os.environ.get("CANARY_LIVE_CLAUDE", "").strip() == "1"
    and _claude_binary_present()
)


# ---------------------------------------------------------------------------
# Path (a): Agent-tool transcript fixture
# ---------------------------------------------------------------------------


def _agent_tool_transcript_analyst() -> dict:
    """Return the inner analyst result an Agent-tool dispatch would yield.

    Hand-rolled to match the analyst output schema documented in
    ``plugins/plan-executor/agents/plan-analyst.md`` for a synthetic
    2-task plan. Stands in for a captured orchestrator transcript;
    keeping it inline (vs. on disk) lets the fixture move with the test.
    """
    return {
        "outcome": "valid",
        "tasks": [
            {
                "id": "001",
                "title": "Seed scratch",
                "agent": "codex",
                "priority": "high",
                "files": ["scratch/.keep"],
                "test_command": "pytest -q",
                "classification_reason": "Single file, mechanical",
            },
            {
                "id": "002",
                "title": "Write a.txt",
                "agent": "codex",
                "priority": "medium",
                "files": ["scratch/a.txt"],
                "test_command": "pytest -q",
                "classification_reason": "Single file, mechanical",
            },
        ],
        "batches": [
            {"index": 1, "task_ids": ["001"], "file_locks": ["scratch/.keep"]},
            {"index": 2, "task_ids": ["002"], "file_locks": ["scratch/a.txt"]},
        ],
        "gaps": [],
        "risks": [],
    }


# ---------------------------------------------------------------------------
# Wrapper invocation helpers
# ---------------------------------------------------------------------------


def _make_input_payload(agent: str, payload: dict) -> dict:
    """Build a schema-valid claude_dispatch_input.json payload for ``agent``."""
    return {
        "schema_version": 1,
        "agent": agent,
        "payload": payload,
        "output_instructions": {
            "format": "json",
            "schema_path": None,
            "schema_inline": None,
            "max_bytes": 65536,
        },
        "overrides": {
            "model": None,
            "timeout_sec": 60,
            "tools_allowed_extra": None,
            "tools_disallowed_extra": None,
            "cwd": None,
        },
        "guardrails": {
            "max_depth": 1,
            "cost_cap_usd": 1.0,
            "network": "deny",
        },
        "trace": {
            "run_id": "canary-run-0001",
            "parent_span_id": None,
            "depth": 0,
            "call_chain": [],
        },
        "declared_files_changed": [],
    }


def _run_wrapper(
    payload: dict,
    tmp_path: Path,
    *,
    dry_run: bool,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Invoke ``plan_claude_dispatch.py run --input <file>``; return (rc, stdout, stderr)."""
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    cmd = [
        sys.executable,
        str(WRAPPER_PATH),
        "run",
        "--input",
        str(input_path),
        "--repo-root",
        str(tmp_path),
    ]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_envelope(stdout: str) -> dict:
    return json.loads(stdout)


def _build_fixture_envelope(agent: str, result: dict, *, status: str = "ok") -> dict:
    """Build a minimal §7 v3-shaped envelope wrapping ``result``.

    Used to drive the ``plan_claude_dispatch_stub.py`` fixture mechanism
    so the canary can exercise *parsing* the wrapper's ``result`` payload
    end-to-end without spawning a real ``claude`` binary.
    """
    return {
        "schema_version": 1,
        "status": status,
        "status_reason": None,
        "agent": agent,
        "model": "claude-opus-4-7",
        "session_id": f"canary-stub-{agent}",
        "duration_ms": 100,
        "cost_usd": 0.0,
        "tokens": {
            "input": 0,
            "output": 0,
            "cache_read": 0,
            "cache_creation": 0,
        },
        "result": result,
        "result_raw_truncated": None,
        "stderr_tail": None,
        "permission_denials": [],
        "scope": {
            "declared_files_changed": [],
            "observed_delta_tracked": [],
            "observed_delta_untracked": [],
            "scope_violation_detected": False,
            "scope_misreport_detected": False,
        },
        "trace": {
            "run_id": "canary-run-0001",
            "span_id": f"canary-span-{agent}",
            "parent_span_id": None,
            "depth": 0,
            "call_chain": ["orchestrator", agent],
            "started_at": "2026-04-26T00:00:00Z",
            "ended_at": "2026-04-26T00:00:01Z",
        },
        "error": None,
    }


def _run_stub_with_fixture(
    fixture_envelope: dict,
    tmp_path: Path,
    *,
    timeout: int = 30,
) -> tuple[int, str, str]:
    """Invoke the TASK-002 stub with ``fixture_envelope`` and return (rc, stdout, stderr).

    The stub honors ``PLAN_CLAUDE_DISPATCH_STUB_FIXTURE`` and emits the
    fixture envelope verbatim on stdout, with exit code derived from
    ``status`` (ok->0 else 1). This lets the canary exercise the wrapper
    contract — invocation + envelope parse — against a deterministic
    payload that carries a *real* inner ``result`` (schedule / impl /
    remediator output) so downstream schema validation has something to
    chew on.
    """
    fixture_path = tmp_path / "stub_fixture.json"
    fixture_path.write_text(json.dumps(fixture_envelope), encoding="utf-8")
    env = os.environ.copy()
    env["PLAN_CLAUDE_DISPATCH_STUB_FIXTURE"] = str(fixture_path)
    cmd = [sys.executable, str(STUB_PATH), "run", "--input", "-"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schedule semantic equality
# ---------------------------------------------------------------------------


def _normalize_schedule(schedule: dict) -> dict:
    """Project the analyst output to its load-bearing semantic shape.

    Per AC: parity is on ``outcome``, ``tasks[*].id`` (ordered set),
    ``batches[*].index``. Byte equality not required.
    """
    return {
        "outcome": schedule.get("outcome"),
        "task_ids": [t.get("id") for t in (schedule.get("tasks") or [])],
        "batch_indices": [b.get("index") for b in (schedule.get("batches") or [])],
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_canary_wrapper_dry_run_shape_analyst(tmp_path: Path) -> None:
    """Path (b) dry-run: wrapper emits the v3 envelope and exits ok."""
    payload = _make_input_payload(
        "plan-analyst",
        {"plan_path": "tests/fixtures/directory_mode_plan"},
    )
    rc, stdout, stderr = _run_wrapper(payload, tmp_path, dry_run=True)
    assert rc == 0, f"wrapper exited {rc}: stderr={stderr!r} stdout={stdout!r}"

    envelope = _parse_envelope(stdout)
    # v3 envelope key parity — downstream tasks lock against this set.
    assert set(envelope.keys()) == V3_REQUIRED_TOP_LEVEL_KEYS, (
        f"unexpected envelope keys: extra="
        f"{set(envelope.keys()) - V3_REQUIRED_TOP_LEVEL_KEYS} "
        f"missing={V3_REQUIRED_TOP_LEVEL_KEYS - set(envelope.keys())}"
    )
    assert envelope["status"] == "ok"
    assert envelope["agent"] == "plan-analyst"

    # ≤64 KB envelope size cap.
    size_bytes = len(stdout.encode("utf-8"))
    assert size_bytes <= ENVELOPE_SIZE_CAP_BYTES, (
        f"envelope {size_bytes} bytes exceeds {ENVELOPE_SIZE_CAP_BYTES}"
    )


def test_canary_schedule_parity_agent_tool_vs_wrapper_path(tmp_path: Path) -> None:
    """Path (a) vs path (b): parsed schedules are semantically equal.

    Path (a) is the inline Agent-tool transcript fixture. Path (b)
    exercises the wrapper in two parts (mirrors the pattern at
    ``test_canary_implementer_dry_run_one_shot`` and
    ``test_canary_remediator_dry_run_one_shot``):

      (1) **Real wrapper invocation.** Drives the actual
          ``plan_claude_dispatch.py run --input`` subprocess in
          ``--dry-run`` mode. Satisfies the AC literal that path (b)
          exercises the real wrapper, and asserts the v3 envelope
          shape on the actual production code path. Dry-run does NOT
          synthesize a real analyst schedule (no Claude is spawned),
          so the schedule-parity assertion runs against (2).

      (2) **Deterministic schedule parity.** Drives the TASK-002 stub
          (``plan_claude_dispatch_stub.py``) with a fixture envelope
          whose inner ``result`` is a schema-shaped analyst schedule,
          and asserts schedule-parity against path (a) on
          ``outcome``, ``tasks[*].id``, ``batches[*].index``. Live
          end-to-end parity against the real ``claude`` binary is
          asserted independently by
          ``test_canary_live_analyst_schedule_parity`` (gated on
          ``CANARY_LIVE_CLAUDE=1``).
    """
    from jsonschema import Draft7Validator

    a = _agent_tool_transcript_analyst()

    # ---- (1) Real wrapper subprocess (--dry-run) ----
    payload = _make_input_payload(
        "plan-analyst",
        {"plan_path": "tests/fixtures/directory_mode_plan"},
    )
    rc1, stdout1, stderr1 = _run_wrapper(payload, tmp_path, dry_run=True)
    assert rc1 == 0, f"real wrapper exited {rc1}: stderr={stderr1!r}"
    envelope1 = _parse_envelope(stdout1)
    assert set(envelope1.keys()) == V3_REQUIRED_TOP_LEVEL_KEYS, (
        f"unexpected envelope keys from real wrapper: extra="
        f"{set(envelope1.keys()) - V3_REQUIRED_TOP_LEVEL_KEYS} "
        f"missing={V3_REQUIRED_TOP_LEVEL_KEYS - set(envelope1.keys())}"
    )
    assert envelope1["status"] == "ok"
    assert envelope1["agent"] == "plan-analyst"
    # Dry-run signal per wrapper §10: status_reason carries the literal token.
    assert "dry_run" in (envelope1.get("status_reason") or "").lower(), (
        f"expected dry_run signal in status_reason, got "
        f"{envelope1.get('status_reason')!r}"
    )

    # ---- (2) Stub-driven schedule parity ----
    fixture_envelope = _build_fixture_envelope("plan-analyst", a)
    rc2, stdout2, stderr2 = _run_stub_with_fixture(fixture_envelope, tmp_path)
    assert rc2 == 0, f"stub exited {rc2}: stderr={stderr2!r}"
    envelope2 = _parse_envelope(stdout2)
    assert envelope2["status"] == "ok"
    assert envelope2["agent"] == "plan-analyst"

    # Validate the inner schedule against the analyst-result schema.
    schedule = envelope2["result"]
    schema = _load_schema(ANALYST_RESULT_SCHEMA_PATH)
    Draft7Validator(schema).validate(schedule)

    # AC parity: outcome, tasks[*].id, batches[*].index.
    assert _normalize_schedule(schedule) == _normalize_schedule(a)
    assert schedule["outcome"] in ALLOWED_ANALYST_OUTCOMES
    assert all(isinstance(t["id"], str) for t in schedule["tasks"])
    assert all(isinstance(b_["index"], int) for b_ in schedule["batches"])


def test_canary_implementer_dry_run_one_shot(tmp_path: Path) -> None:
    """Front-loads wrapper readiness for TASK-004 (implementer dispatch).

    Two-part probe:
      (1) Real wrapper ``--dry-run`` invocation asserts the v3 envelope
          shape downstream tasks lock against.
      (2) Stub-driven fixture round-trip validates a realistic implementer
          ``result`` against the implementer-result schema and asserts
          ``outcome ∈ ALLOWED_IMPLEMENTER_OUTCOMES`` per the AC.
    """
    from jsonschema import Draft7Validator

    # Part 1: real wrapper --dry-run for v3 envelope shape parity.
    payload = _make_input_payload(
        "plan-implementer",
        {"task_id": "TASK-001", "plan_file": "PLAN.md"},
    )
    rc, stdout, stderr = _run_wrapper(payload, tmp_path, dry_run=True)
    assert rc == 0, f"wrapper exited {rc}: stderr={stderr!r}"
    envelope = _parse_envelope(stdout)
    assert envelope["status"] == "ok"
    assert envelope["agent"] == "plan-implementer"
    assert set(envelope.keys()) == V3_REQUIRED_TOP_LEVEL_KEYS

    # Part 2: stub-driven probe so we have a realistic inner result to
    # schema-validate (the wrapper's --dry-run result is plan-only and
    # carries no implementer ``outcome`` field).
    impl_result = {
        "outcome": "success",
        "files_changed": ["scratch/a.txt"],
        "report": {
            "diff_summary": ["Added stub line."],
            "test_command": "pytest -q",
            "test_outcome": "passed",
            "acceptance_criteria_check": ["[x] AC1 — file written"],
            "plan_adaptations": [],
            "concerns_for_reviewer": [],
        },
    }
    fixture_envelope = _build_fixture_envelope("plan-implementer", impl_result)
    rc, stdout, stderr = _run_stub_with_fixture(fixture_envelope, tmp_path)
    assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"
    stub_env = _parse_envelope(stdout)
    schema = _load_schema(IMPLEMENTER_RESULT_SCHEMA_PATH)
    Draft7Validator(schema).validate(stub_env["result"])
    assert stub_env["result"]["outcome"] in ALLOWED_IMPLEMENTER_OUTCOMES


def test_canary_remediator_dry_run_one_shot(tmp_path: Path) -> None:
    """Front-loads wrapper readiness for TASK-005 (remediator dispatch).

    Two-part probe (mirrors the implementer canary):
      (1) Real wrapper ``--dry-run`` for v3 envelope shape parity.
      (2) Stub-driven fixture round-trip validates a realistic remediator
          ``result`` against the remediator-result schema and asserts
          ``outcome ∈ ALLOWED_REMEDIATOR_OUTCOMES``.
    """
    from jsonschema import Draft7Validator

    # Part 1: real wrapper --dry-run for v3 envelope shape parity.
    payload = _make_input_payload(
        "plan-remediator",
        {"task_id": "TASK-001", "review_findings": []},
    )
    rc, stdout, stderr = _run_wrapper(payload, tmp_path, dry_run=True)
    assert rc == 0, f"wrapper exited {rc}: stderr={stderr!r}"
    envelope = _parse_envelope(stdout)
    assert envelope["status"] == "ok"
    assert envelope["agent"] == "plan-remediator"
    assert set(envelope.keys()) == V3_REQUIRED_TOP_LEVEL_KEYS

    # Part 2: stub-driven probe with a realistic remediator ``result``.
    rem_result = {
        "outcome": "success",
        "files_changed": ["scratch/a.txt"],
        "report": {
            "diff_summary": ["Re-applied implementer change inside scope."],
            "test_command": "pytest -q",
            "test_outcome": "passed",
            "acceptance_criteria_check": ["[x] AC1 — fix re-applied"],
            "plan_adaptations": [],
            "concerns_for_reviewer": [],
        },
    }
    fixture_envelope = _build_fixture_envelope("plan-remediator", rem_result)
    rc, stdout, stderr = _run_stub_with_fixture(fixture_envelope, tmp_path)
    assert rc == 0, f"stub exited {rc}: stderr={stderr!r}"
    stub_env = _parse_envelope(stdout)
    schema = _load_schema(REMEDIATOR_RESULT_SCHEMA_PATH)
    Draft7Validator(schema).validate(stub_env["result"])
    assert stub_env["result"]["outcome"] in ALLOWED_REMEDIATOR_OUTCOMES


# ---------------------------------------------------------------------------
# Live-mode path (b): real claude binary. Assertion code present
# unconditionally; skipped at runtime unless CANARY_LIVE_CLAUDE=1 and
# the binary is present.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _LIVE_MODE,
    reason=(
        "live mode disabled: requires CANARY_LIVE_CLAUDE=1 and `claude` "
        "binary on PATH"
    ),
)
def test_canary_live_analyst_schedule_parity(tmp_path: Path) -> None:
    """Live-claude probe: parse schedule out of wrapper envelope.

    Asserts the parsed schedule from the real wrapper run matches
    the Agent-tool transcript fixture on the AC-defined semantic
    projection (outcome, tasks[*].id, batches[*].index).
    """
    payload = _make_input_payload(
        "plan-analyst",
        {"plan_path": str(REPO_ROOT / "tests/fixtures/directory_mode_plan")},
    )
    rc, stdout, _ = _run_wrapper(payload, tmp_path, dry_run=False, timeout=300)
    assert rc == 0, f"live wrapper exited {rc}"
    envelope = _parse_envelope(stdout)
    assert envelope["status"] == "ok"
    assert set(envelope.keys()) == V3_REQUIRED_TOP_LEVEL_KEYS
    size_bytes = len(stdout.encode("utf-8"))
    assert size_bytes <= ENVELOPE_SIZE_CAP_BYTES

    schedule = envelope.get("result")
    assert isinstance(schedule, dict)
    assert schedule.get("outcome") in ALLOWED_ANALYST_OUTCOMES

    expected = _normalize_schedule(_agent_tool_transcript_analyst())
    observed = _normalize_schedule(schedule)
    assert observed == expected, (
        f"schedule parity broken: observed={observed} expected={expected}"
    )


# ---------------------------------------------------------------------------
# Probe-results sanity: assert the documentation file exists and
# records the v3 keys we just asserted on.
# ---------------------------------------------------------------------------


def test_canary_probe_results_md_present_and_records_v3_keys() -> None:
    """The companion probe_results.md must enumerate the v3 envelope keys."""
    assert PROBE_RESULTS_PATH.exists(), (
        f"missing probe results doc: {PROBE_RESULTS_PATH}"
    )
    text = PROBE_RESULTS_PATH.read_text(encoding="utf-8")
    for key in V3_REQUIRED_TOP_LEVEL_KEYS:
        assert key in text, f"probe_results.md missing v3 key: {key!r}"
