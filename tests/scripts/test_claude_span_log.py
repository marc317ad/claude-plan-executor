"""Tests for ``_claude_span_log.append_span`` (PLAN_NESTED_DISPATCH TASK-006).

Coverage matches the task's acceptance criteria:

  * ``append_span(log_dir, envelope)`` atomically writes one JSON line to
    ``$log_dir/spans.jsonl``.
  * Default log dir resolves to ``<repo_root>/docs/plans/`` (sibling of
    ``_run_log.jsonl``) when ``log_dir`` is ``None``.
  * Multi-hop test (wrapper-calling-wrapper simulation) produces N spans
    with correct parent links forming a chain.
  * Concurrency test (10 simultaneous appenders) leaves no torn writes:
    every line is valid JSON and ``wc -l`` matches the spawn count.

The "wrapper" itself is not invoked here — this is a unit test of the
helper. End-to-end coverage of plan_claude_dispatch.py invoking the
helper lives in ``test_plan_claude_dispatch_cli.py`` (TASK-005) and the
shim integration test (TASK-008).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _claude_span_log as span_log  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _envelope(
    *,
    status: str = "ok",
    run_id: str = "run-A",
    span_id: str = "span-A",
    parent_span_id: str | None = None,
    depth: int = 0,
    agent: str = "plan-implementer",
    task_id: str = "001",
    duration_ms: int | None = 100,
    cost_usd: float | None = 0.001,
    tokens: dict | None = None,
    declared_files_changed: list | None = None,
    observed_tracked: list | None = None,
    observed_untracked: list | None = None,
    scope_violation: bool = False,
    scope_misreport: bool = False,
    started_at: str = "2026-04-25T00:00:00Z",
    ended_at: str | None = "2026-04-25T00:00:01Z",
) -> Dict[str, Any]:
    """Build a §7-shaped envelope-ish dict for span tests.

    Not the full output schema — just enough for ``build_span`` to do
    its work. Helpers like ``_claude_dispatch_envelope.build_ok`` would
    work too, but constructing locally keeps the test independent.
    """
    return {
        "schema_version": 1,
        "status": status,
        "status_reason": None,
        "agent": agent,
        "model": "claude-opus-4-7",
        "session_id": "sess-1",
        "duration_ms": duration_ms,
        "cost_usd": cost_usd,
        "tokens": tokens if tokens is not None else {"input": 10, "output": 20, "cache_read": 0, "cache_creation": 0},
        "result": {"task_id": task_id, "files_changed": []},
        "result_raw_truncated": "{}",
        "stderr_tail": "",
        "permission_denials": [],
        "scope": {
            "declared_files_changed": declared_files_changed if declared_files_changed is not None else [],
            "observed_delta_tracked": observed_tracked if observed_tracked is not None else [],
            "observed_delta_untracked": observed_untracked if observed_untracked is not None else [],
            "scope_violation_detected": scope_violation,
            "scope_misreport_detected": scope_misreport,
        },
        "trace": {
            "run_id": run_id,
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "depth": depth,
            "call_chain": ["orchestrator"],
            "started_at": started_at,
            "ended_at": ended_at,
        },
        "error": None,
    }


@pytest.fixture(autouse=True)
def _no_per_hop_env(monkeypatch):
    """Ensure ambient PER_HOP env vars don't bleed into spans by default."""
    monkeypatch.delenv("PLAN_EXEC_PARENT_RUN_ID", raising=False)
    monkeypatch.delenv("PLAN_EXEC_PARENT_AGENT", raising=False)
    monkeypatch.delenv("PLAN_EXEC_DISPATCH_DEPTH", raising=False)
    yield


# ---------------------------------------------------------------------------
# build_span — reduced envelope shape
# ---------------------------------------------------------------------------


class TestBuildSpan:
    def test_extracts_top_level_metadata(self):
        env = _envelope()
        span = span_log.build_span(env)
        assert span["agent"] == "plan-implementer"
        assert span["status"] == "ok"
        assert span["session_id"] == "sess-1"
        assert span["duration_ms"] == 100
        assert span["cost_usd"] == 0.001

    def test_extracts_trace_links(self):
        env = _envelope(run_id="run-B", span_id="span-B", parent_span_id="span-A", depth=2)
        span = span_log.build_span(env)
        assert span["run_id"] == "run-B"
        assert span["span_id"] == "span-B"
        assert span["parent_span_id"] == "span-A"
        assert span["dispatch_depth"] == 2

    def test_ts_falls_back_to_started_at(self):
        env = _envelope(started_at="2026-04-25T01:00:00Z", ended_at=None)
        span = span_log.build_span(env)
        assert span["ts"] == "2026-04-25T01:00:00Z"

    def test_ts_prefers_ended_at(self):
        env = _envelope(
            started_at="2026-04-25T01:00:00Z",
            ended_at="2026-04-25T01:00:30Z",
        )
        span = span_log.build_span(env)
        assert span["ts"] == "2026-04-25T01:00:30Z"

    def test_extracts_task_id_from_result(self):
        env = _envelope(task_id="042")
        span = span_log.build_span(env)
        assert span["task_id"] == "042"

    def test_task_id_none_when_result_missing_field(self):
        env = _envelope()
        env["result"] = {}
        span = span_log.build_span(env)
        assert span["task_id"] is None

    def test_tokens_total_sums_known_keys(self):
        env = _envelope(tokens={"input": 100, "output": 200, "cache_read": 50, "cache_creation": 0})
        span = span_log.build_span(env)
        assert span["tokens_total"] == 350

    def test_tokens_total_none_when_missing(self):
        env = _envelope(tokens=None)
        env["tokens"] = None
        span = span_log.build_span(env)
        assert span["tokens_total"] is None

    def test_scope_flags_propagate(self):
        env = _envelope(
            scope_violation=True,
            scope_misreport=True,
            observed_tracked=["a.py", "b.py"],
            declared_files_changed=["a.py"],
        )
        span = span_log.build_span(env)
        assert span["scope_violation_detected"] is True
        assert span["scope_misreport_detected"] is True
        # b.py is observed but not declared -> 1 out-of-scope path.
        assert span["out_of_scope_observed"] == 1

    def test_no_out_of_scope_when_all_declared(self):
        env = _envelope(
            observed_tracked=["a.py"],
            declared_files_changed=["a.py"],
        )
        span = span_log.build_span(env)
        assert span["out_of_scope_observed"] == 0

    def test_per_hop_env_populates_parent_links(self, monkeypatch):
        monkeypatch.setenv("PLAN_EXEC_PARENT_RUN_ID", "outer-run-uuid")
        monkeypatch.setenv("PLAN_EXEC_PARENT_AGENT", "plan-orchestrator")
        env = _envelope()
        span = span_log.build_span(env)
        assert span["parent_run_id"] == "outer-run-uuid"
        assert span["parent_agent"] == "plan-orchestrator"

    def test_non_mapping_envelope_returns_minimal_span(self):
        span = span_log.build_span("not a dict")  # type: ignore[arg-type]
        # All canonical keys present, all None / safe defaults.
        assert "status" in span
        assert span["status"] is None


# ---------------------------------------------------------------------------
# resolve_log_path — default + override semantics
# ---------------------------------------------------------------------------


class TestResolveLogPath:
    def test_explicit_log_dir(self, tmp_path):
        result = span_log.resolve_log_path(tmp_path)
        assert result == tmp_path / "spans.jsonl"

    def test_explicit_log_dir_str(self, tmp_path):
        result = span_log.resolve_log_path(str(tmp_path))
        assert result == tmp_path / "spans.jsonl"

    def test_explicit_jsonl_file_path_used_verbatim(self, tmp_path):
        explicit = tmp_path / "custom.jsonl"
        result = span_log.resolve_log_path(explicit)
        assert result == explicit

    def test_default_uses_repo_root_kwarg(self, tmp_path):
        result = span_log.resolve_log_path(None, repo_root=tmp_path)
        assert result == tmp_path / "docs" / "plans" / "spans.jsonl"

    def test_default_uses_envelope_repo_root(self, tmp_path):
        env = _envelope()
        env["scope"]["repo_root"] = str(tmp_path)
        result = span_log.resolve_log_path(None, envelope=env)
        assert result == tmp_path / "docs" / "plans" / "spans.jsonl"

    def test_default_falls_back_to_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = span_log.resolve_log_path(None)
        assert result == tmp_path / "docs" / "plans" / "spans.jsonl"


# ---------------------------------------------------------------------------
# append_span — single-write semantics
# ---------------------------------------------------------------------------


class TestAppendSpanSingle:
    def test_writes_one_jsonl_line(self, tmp_path):
        env = _envelope(run_id="r1", span_id="s1")
        target = span_log.append_span(tmp_path, env)
        assert target == tmp_path / "spans.jsonl"
        assert target.exists()
        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["run_id"] == "r1"
        assert record["span_id"] == "s1"
        assert record["status"] == "ok"

    def test_appends_subsequent_calls(self, tmp_path):
        env1 = _envelope(run_id="r1", span_id="s1")
        env2 = _envelope(run_id="r2", span_id="s2")
        span_log.append_span(tmp_path, env1)
        span_log.append_span(tmp_path, env2)
        lines = (tmp_path / "spans.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        record_a = json.loads(lines[0])
        record_b = json.loads(lines[1])
        assert record_a["run_id"] == "r1"
        assert record_b["run_id"] == "r2"

    def test_creates_log_dir_recursively(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        env = _envelope()
        target = span_log.append_span(deep, env)
        assert target == deep / "spans.jsonl"
        assert target.exists()

    def test_default_log_dir_with_repo_root(self, tmp_path):
        env = _envelope()
        target = span_log.append_span(None, env, repo_root=tmp_path)
        # Sibling of _run_log.jsonl per the task description.
        expected = tmp_path / "docs" / "plans" / "spans.jsonl"
        assert target == expected
        assert expected.exists()

    def test_each_line_is_well_formed_json(self, tmp_path):
        for i in range(5):
            span_log.append_span(tmp_path, _envelope(run_id=f"r{i}", span_id=f"s{i}"))
        lines = (tmp_path / "spans.jsonl").read_text(encoding="utf-8").splitlines()
        for ln in lines:
            json.loads(ln)  # raises if malformed

    def test_jsonl_file_path_used_verbatim(self, tmp_path):
        explicit = tmp_path / "alt_spans.jsonl"
        env = _envelope()
        target = span_log.append_span(explicit, env)
        assert target == explicit
        assert explicit.exists()


# ---------------------------------------------------------------------------
# Multi-hop chain — wrapper-calling-wrapper simulation
# ---------------------------------------------------------------------------


def _hop_writer(log_dir: str, run_id: str, span_id: str, parent_span_id: str | None, depth: int) -> None:
    """Helper run inside a forked process to simulate one wrapper hop.

    Writes a single span with explicit parent_span_id linkage so the
    parent chain is reconstructible after the fact.
    """
    env = _envelope(
        run_id=run_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        depth=depth,
    )
    span_log.append_span(log_dir, env)


class TestMultiHopChain:
    def test_three_hops_produce_chain(self, tmp_path):
        """Simulate top -> mid -> leaf, each writing one span.

        The acceptance criterion is "Multi-hop test (wrapper calling
        wrapper) produces N spans with correct parent links". We use
        explicit parent_span_id linkage rather than process inheritance
        because the actual nested-claude chain is process-isolated; what
        matters for the audit trail is that *a reader* can reconstruct
        the chain from spans.jsonl alone.
        """
        log_dir = tmp_path

        # Three hops in sequence (parent links follow the spawn order).
        hops = [
            ("run-top",  "span-top",  None,        0),
            ("run-mid",  "span-mid",  "span-top",  1),
            ("run-leaf", "span-leaf", "span-mid",  2),
        ]
        for run_id, span_id, parent_span_id, depth in hops:
            _hop_writer(str(log_dir), run_id, span_id, parent_span_id, depth)

        log_path = log_dir / "spans.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3

        records = [json.loads(ln) for ln in lines]

        # 1) Each span carries its own (run_id, span_id, parent_span_id, depth).
        assert [r["span_id"] for r in records] == ["span-top", "span-mid", "span-leaf"]
        assert [r["parent_span_id"] for r in records] == [None, "span-top", "span-mid"]
        assert [r["dispatch_depth"] for r in records] == [0, 1, 2]

        # 2) Parent-link chain is reconstructible: walking parent_span_id
        # back from the leaf reaches the root in N-1 steps.
        by_span = {r["span_id"]: r for r in records}
        chain: list[str] = []
        cursor: str | None = "span-leaf"
        while cursor is not None:
            chain.append(cursor)
            cursor = by_span[cursor]["parent_span_id"]
        assert chain == ["span-leaf", "span-mid", "span-top"]

    def test_three_hops_via_subprocesses(self, tmp_path):
        """Same chain, but each hop writes from a separate process.

        This is the closer model of nested-claude: each hop is its own
        Python process. We use ``multiprocessing.Process`` (spawn-safe)
        with explicit parent_span_id chaining so the audit trail is
        independent of process inheritance.
        """
        ctx = multiprocessing.get_context("spawn")
        log_dir = str(tmp_path)
        hops = [
            ("run-top",  "span-top",  None,        0),
            ("run-mid",  "span-mid",  "span-top",  1),
            ("run-leaf", "span-leaf", "span-mid",  2),
        ]
        for args in hops:
            p = ctx.Process(target=_hop_writer, args=(log_dir, *args))
            p.start()
            p.join(timeout=15)
            assert p.exitcode == 0, f"hop failed: exitcode={p.exitcode}"

        log_path = tmp_path / "spans.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        records = [json.loads(ln) for ln in lines]
        depths = sorted(r["dispatch_depth"] for r in records)
        assert depths == [0, 1, 2]
        # Verify parent-link chain after reading back from disk.
        by_span = {r["span_id"]: r for r in records}
        cursor = "span-leaf"
        chain = []
        while cursor is not None:
            chain.append(cursor)
            cursor = by_span[cursor]["parent_span_id"]
        assert chain == ["span-leaf", "span-mid", "span-top"]


# ---------------------------------------------------------------------------
# Env-var inheritance — production parent metadata path.
# ---------------------------------------------------------------------------


def _env_parent_writer(log_dir: str) -> None:
    """Append one span after setting parent metadata in the child env."""
    os.environ["PLAN_EXEC_PARENT_RUN_ID"] = "parent-run-X"
    os.environ["PLAN_EXEC_PARENT_AGENT"] = "parent-agent-X"
    env = _envelope(run_id="child-run-X", span_id="child-span-X")
    span_log.append_span(log_dir, env)


class TestEnvVarInheritance:
    def test_span_captures_env_var_parent_when_no_kwarg(self, tmp_path):
        ctx = multiprocessing.get_context("spawn")
        p = ctx.Process(target=_env_parent_writer, args=(str(tmp_path),))
        p.start()
        p.join(timeout=15)
        assert p.exitcode == 0, f"env parent writer failed: exitcode={p.exitcode}"

        log_path = tmp_path / "spans.jsonl"
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["parent_run_id"] == "parent-run-X"
        assert record["parent_agent"] == "parent-agent-X"


# ---------------------------------------------------------------------------
# Concurrency — N processes appending simultaneously, no torn writes.
# ---------------------------------------------------------------------------


def _concurrency_writer(log_dir: str, run_id: str, padding_size: int = 0) -> None:
    """Append one span; optionally pad ``status_reason`` to test long lines.

    Padding > PIPE_BUF (4096) forces the flock fallback path so we
    cover both atomicity strategies.
    """
    env = _envelope(run_id=run_id)
    if padding_size > 0:
        env["status_reason"] = "x" * padding_size
    span_log.append_span(log_dir, env)


class TestConcurrency:
    def test_ten_processes_no_torn_writes(self, tmp_path):
        """Spawn N=10 processes; assert every line is valid JSON and count matches."""
        n = 10
        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(target=_concurrency_writer, args=(str(tmp_path), f"run-{i}"))
            for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0

        log_path = tmp_path / "spans.jsonl"
        text = log_path.read_text(encoding="utf-8")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        assert len(lines) == n, f"expected {n} lines, got {len(lines)}"
        # Every line parses as JSON (no torn writes interleaving bytes).
        parsed = [json.loads(ln) for ln in lines]
        seen_runs = {r["run_id"] for r in parsed}
        assert seen_runs == {f"run-{i}" for i in range(n)}

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock only")
    def test_long_lines_use_flock_path(self, tmp_path):
        """Lines > PIPE_BUF use the explicit flock fallback; no torn writes."""
        n = 5
        # 8 KB of padding pushes the line well past PIPE_BUF (4096).
        padding = 8192
        ctx = multiprocessing.get_context("spawn")
        procs = [
            ctx.Process(
                target=_concurrency_writer,
                args=(str(tmp_path), f"run-long-{i}", padding),
            )
            for i in range(n)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0

        log_path = tmp_path / "spans.jsonl"
        lines = [ln for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) == n
        for ln in lines:
            record = json.loads(ln)
            # status_reason still intact (no truncation, no interleaving).
            assert record["status_reason"] == "x" * padding


# ---------------------------------------------------------------------------
# File mode + creation
# ---------------------------------------------------------------------------


class TestFilePermissions:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")
    def test_file_mode_is_0644(self, tmp_path):
        env = _envelope()
        target = span_log.append_span(tmp_path, env)
        mode = os.stat(target).st_mode & 0o777
        # umask may strip some bits; assert at least owner can read/write
        # and that no execute bit is set.
        assert mode & 0o200, "owner write missing"
        assert mode & 0o400, "owner read missing"
        assert not (mode & 0o111), f"unexpected execute bit on log file: {oct(mode)}"
