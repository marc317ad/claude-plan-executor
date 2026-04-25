"""End-to-end escape-hatch integration test (PLAN_NESTED_DISPATCH TASK-008).

Exercises ``plan_claude_dispatch.py`` against a real ``claude`` binary,
validating the full nested-dispatch pipeline end to end:

  * Case 1 — Happy path: dispatch ``agent: plan-analyst`` against a
    stub plan via the fixture payload. Assert the wrapper exits 0,
    the envelope is ``status: ok`` with a non-empty ``result``, and at
    least one new line was appended to ``spans.jsonl``.
  * Case 2 — Malformed input: feed invalid JSON. The wrapper must
    exit 2 with a ``status: input_invalid`` envelope on stdout
    (covers TASK-005's fast-fail path).
  * Case 3 — Parallel disjoint: two ``subprocess.Popen`` instances
    of the wrapper run concurrently with disjoint per-worker tmp
    dirs (per-worker cwd, per-worker span-log dir, per-worker
    trace.run_id). Both must succeed and neither baseline may
    cross-contaminate the other (TASK-004 invariant).

Skipped by default. Runs only when ``PLAN_EXEC_E2E=1`` is set in the
environment AND ``claude`` is on PATH. Cost per full run: three real
``claude -p`` invocations (~10-30s each, a few cents).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_claude_dispatch.py"
FIXTURE_PAYLOAD = (
    REPO_ROOT / "tests" / "fixtures" / "claude_dispatch" / "minimal_payload.json"
)


# ---------------------------------------------------------------------------
# Skip gates (PLAN_EXEC_E2E=1 + claude on PATH)
# ---------------------------------------------------------------------------


def _e2e_enabled() -> bool:
    return os.environ.get("PLAN_EXEC_E2E") == "1"


def _claude_available() -> bool:
    return shutil.which("claude") is not None


pytestmark = [
    pytest.mark.skipif(
        not _e2e_enabled(),
        reason="set PLAN_EXEC_E2E=1 to run live claude e2e tests",
    ),
    pytest.mark.skipif(
        not _claude_available(),
        reason="claude CLI not on PATH",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_payload() -> Dict[str, Any]:
    """Read the §6 fixture payload from disk."""
    return json.loads(FIXTURE_PAYLOAD.read_text(encoding="utf-8"))


def _wrapper_env(span_log_dir: Path) -> Dict[str, str]:
    """Build a sanitised env that pins ``PLAN_EXEC_LOG_DIR`` to ``span_log_dir``.

    The wrapper's :func:`_resolve_span_log_path` consumes ``PLAN_EXEC_LOG_DIR``
    first; redirecting it keeps the test from polluting the repo's actual
    ``docs/plans/spans.jsonl``.
    """
    env = os.environ.copy()
    env["PLAN_EXEC_LOG_DIR"] = str(span_log_dir)
    return env


def _resolve_claude_binary() -> str:
    """Return the absolute path to the ``claude`` binary.

    The wrapper's :func:`_claude_guardrails.scrub_env` deny-by-defaults the
    parent env down to ``manifest['env_allowlist'] + PLAN_EXEC_*``. The
    plan-analyst manifest declares no ``env_allowlist``, so PATH is stripped
    from the nested-claude subprocess env. To keep the wrapper's argv
    resolvable in that scrubbed env, we pass ``--backend-binary`` with the
    absolute path to ``claude`` so subprocess.Popen does not need PATH.
    """
    binary = shutil.which("claude")
    assert binary is not None, "skip gate should have caught missing claude"
    return binary


def _run_wrapper(
    payload_path: Path,
    *,
    repo_root: Path,
    span_log_dir: Path,
    timeout_sec: int = 240,
) -> tuple[int, str, str]:
    """Spawn the wrapper synchronously; return ``(rc, stdout, stderr)``."""
    proc = subprocess.run(
        [
            sys.executable,
            str(WRAPPER),
            "run",
            "--input", str(payload_path),
            "--output", "-",
            "--repo-root", str(repo_root),
            "--backend-binary", _resolve_claude_binary(),
        ],
        env=_wrapper_env(span_log_dir),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _launch_wrapper(
    payload_path: Path,
    *,
    repo_root: Path,
    span_log_dir: Path,
) -> subprocess.Popen:
    """Spawn the wrapper asynchronously for parallel execution."""
    return subprocess.Popen(
        [
            sys.executable,
            str(WRAPPER),
            "run",
            "--input", str(payload_path),
            "--output", "-",
            "--repo-root", str(repo_root),
            "--backend-binary", _resolve_claude_binary(),
        ],
        env=_wrapper_env(span_log_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _parse_envelope(stdout: str) -> Dict[str, Any]:
    """Best-effort JSON parse of wrapper stdout into the §7 envelope."""
    return json.loads(stdout.strip())


def _read_spans(span_log_path: Path) -> list[Dict[str, Any]]:
    """Read every JSONL line from ``span_log_path`` (returns ``[]`` if absent)."""
    if not span_log_path.exists():
        return []
    lines = [
        ln for ln in span_log_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    return [json.loads(ln) for ln in lines]


def _span_run_id(span: Dict[str, Any]) -> Any:
    """Extract a span entry's ``run_id``.

    Tolerates both the legacy nested-trace shape (``span["trace"]["run_id"]``,
    pre-TASK-006 placeholder) and the TASK-006 structured shape
    (``span["run_id"]`` flat at the top level).
    """
    if "run_id" in span:
        return span["run_id"]
    trace = span.get("trace") or {}
    return trace.get("run_id")


def _span_span_id(span: Dict[str, Any]) -> Any:
    """Extract a span entry's ``span_id`` (see :func:`_span_run_id`)."""
    if "span_id" in span:
        return span["span_id"]
    trace = span.get("trace") or {}
    return trace.get("span_id")


def _make_isolated_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo under ``tmp_path`` that the wrapper can use as
    its ``--repo-root`` for baseline snapshot + cleanup. Returns the repo
    path. Each call returns a fresh, fully-isolated repo so two parallel
    wrapper instances cannot cross-contaminate baselines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(repo), check=True, capture_output=True,
    )
    # An initial commit so HEAD exists; otherwise some git diff invocations
    # in delta-bounded cleanup will treat the entire workdir as untracked.
    seed = repo / "README.md"
    seed.write_text("# scratch\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"],
        cwd=str(repo), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True,
    )
    return repo


def _write_payload_with_run_id(
    base_payload: Dict[str, Any],
    target: Path,
    run_id: str,
) -> Path:
    """Materialize a copy of ``base_payload`` with ``trace.run_id`` set to
    ``run_id`` at ``target``. Returns ``target``."""
    payload = json.loads(json.dumps(base_payload))
    payload["trace"] = dict(payload.get("trace") or {})
    payload["trace"]["run_id"] = run_id
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Case 1 — Happy path (plan-analyst against a stub plan)
# ---------------------------------------------------------------------------


def test_e2e_plan_analyst_happy_path(tmp_path: Path) -> None:
    """End-to-end: ``agent: plan-analyst`` returns ``status: ok`` and a
    matching ``spans.jsonl`` line is appended."""
    repo = _make_isolated_repo(tmp_path)
    span_log_dir = tmp_path / "spans"
    span_log_dir.mkdir()
    payload_path = tmp_path / "input.json"
    payload_path.write_text(FIXTURE_PAYLOAD.read_text(encoding="utf-8"), encoding="utf-8")

    rc, stdout, stderr = _run_wrapper(
        payload_path,
        repo_root=repo,
        span_log_dir=span_log_dir,
    )

    # Wrapper must always emit a JSON envelope on stdout, even on
    # non-ok statuses; assert that first so the diagnostic is readable.
    try:
        envelope = _parse_envelope(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"wrapper stdout not valid JSON: {exc}\n"
            f"rc={rc}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )

    assert rc == 0, (
        f"wrapper rc={rc}, status={envelope.get('status')!r}, "
        f"error={envelope.get('error')!r}\n"
        f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    )
    assert envelope.get("status") == "ok", (
        f"expected status=ok, got {envelope.get('status')!r}\n"
        f"envelope:\n{json.dumps(envelope, indent=2)[:2000]}"
    )
    assert envelope.get("result") is not None, (
        "ok envelope must carry a non-null result\n"
        f"envelope:\n{json.dumps(envelope, indent=2)[:2000]}"
    )
    # The trace echoes the fixture's run_id.
    fixture = _load_fixture_payload()
    expected_run_id = fixture["trace"]["run_id"]
    assert envelope["trace"]["run_id"] == expected_run_id, envelope["trace"]

    # spans.jsonl was written with at least one line whose run_id /
    # span_id matches the wrapper's envelope. The span shape is
    # tolerated by ``_span_run_id`` / ``_span_span_id`` so the test
    # passes against both the v1 placeholder (envelope-as-jsonl) and
    # the TASK-006 structured shape.
    span_log = span_log_dir / "spans.jsonl"
    spans = _read_spans(span_log)
    assert spans, f"expected at least one span line at {span_log}"
    matching = [
        s for s in spans
        if _span_run_id(s) == envelope["trace"]["run_id"]
        and _span_span_id(s) == envelope["trace"]["span_id"]
    ]
    assert matching, (
        f"no spans.jsonl line matched the envelope's trace identity\n"
        f"envelope.trace={envelope['trace']}\n"
        f"spans={spans}"
    )


# ---------------------------------------------------------------------------
# Case 2 — Malformed input (TASK-005 fast-fail path)
# ---------------------------------------------------------------------------


def test_e2e_malformed_input_exits_two_with_input_invalid(tmp_path: Path) -> None:
    """Garbage on the input path → exit 2 + ``status: input_invalid``."""
    repo = _make_isolated_repo(tmp_path)
    span_log_dir = tmp_path / "spans"
    span_log_dir.mkdir()
    bad_input = tmp_path / "bad.json"
    bad_input.write_text("this is not json {", encoding="utf-8")

    rc, stdout, stderr = _run_wrapper(
        bad_input,
        repo_root=repo,
        span_log_dir=span_log_dir,
        timeout_sec=30,
    )

    assert rc == 2, (
        f"expected exit 2 for malformed input, got rc={rc}\n"
        f"STDOUT:\n{stdout}\nSTDERR:\n{stderr}"
    )
    try:
        envelope = _parse_envelope(stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"wrapper stdout not valid JSON: {exc}\n"
            f"rc={rc}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        )
    assert envelope.get("status") == "input_invalid", (
        f"expected status=input_invalid, got {envelope.get('status')!r}\n"
        f"envelope:\n{json.dumps(envelope, indent=2)[:2000]}"
    )


# ---------------------------------------------------------------------------
# Case 3 — Parallel disjoint (TASK-004 invariant)
# ---------------------------------------------------------------------------


def test_e2e_parallel_disjoint_baselines_no_cross_contamination(
    tmp_path: Path,
) -> None:
    """Two parallel wrapper invocations on disjoint repos + span dirs both
    succeed; their span-log writes do not cross-contaminate."""
    base_payload = _load_fixture_payload()

    # Worker A: own repo, own span dir, own trace.run_id, own payload file.
    workdir_a = tmp_path / "worker_a"
    workdir_a.mkdir()
    repo_a = _make_isolated_repo(workdir_a)
    span_dir_a = workdir_a / "spans"
    span_dir_a.mkdir()
    payload_a = _write_payload_with_run_id(
        base_payload,
        workdir_a / "input.json",
        run_id="e2e-parallel-A",
    )

    # Worker B: own repo, own span dir, own trace.run_id, own payload file.
    workdir_b = tmp_path / "worker_b"
    workdir_b.mkdir()
    repo_b = _make_isolated_repo(workdir_b)
    span_dir_b = workdir_b / "spans"
    span_dir_b.mkdir()
    payload_b = _write_payload_with_run_id(
        base_payload,
        workdir_b / "input.json",
        run_id="e2e-parallel-B",
    )

    proc_a = _launch_wrapper(
        payload_a, repo_root=repo_a, span_log_dir=span_dir_a,
    )
    proc_b = _launch_wrapper(
        payload_b, repo_root=repo_b, span_log_dir=span_dir_b,
    )

    out_a, err_a = proc_a.communicate(timeout=240)
    out_b, err_b = proc_b.communicate(timeout=240)

    # Both wrappers must emit valid envelopes with status ok.
    try:
        env_a = _parse_envelope(out_a)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"worker A stdout not JSON: {exc}\n"
            f"rc={proc_a.returncode}\nSTDOUT:\n{out_a}\nSTDERR:\n{err_a}"
        )
    try:
        env_b = _parse_envelope(out_b)
    except json.JSONDecodeError as exc:
        pytest.fail(
            f"worker B stdout not JSON: {exc}\n"
            f"rc={proc_b.returncode}\nSTDOUT:\n{out_b}\nSTDERR:\n{err_b}"
        )

    assert proc_a.returncode == 0, (
        f"worker A failed rc={proc_a.returncode}, status={env_a.get('status')!r}\n"
        f"STDOUT:\n{out_a}\nSTDERR:\n{err_a}"
    )
    assert proc_b.returncode == 0, (
        f"worker B failed rc={proc_b.returncode}, status={env_b.get('status')!r}\n"
        f"STDOUT:\n{out_b}\nSTDERR:\n{err_b}"
    )
    assert env_a.get("status") == "ok", env_a
    assert env_b.get("status") == "ok", env_b

    # Each worker's run_id round-trips through its own envelope.
    assert env_a["trace"]["run_id"] == "e2e-parallel-A", env_a["trace"]
    assert env_b["trace"]["run_id"] == "e2e-parallel-B", env_b["trace"]

    # Each worker's spans.jsonl carries its own run_id and ONLY its own
    # run_id — no leakage across the disjoint span dirs. ``_span_run_id``
    # tolerates both the v1 placeholder shape and the TASK-006
    # structured shape.
    spans_a = _read_spans(span_dir_a / "spans.jsonl")
    spans_b = _read_spans(span_dir_b / "spans.jsonl")
    assert spans_a, f"worker A spans.jsonl is empty at {span_dir_a}"
    assert spans_b, f"worker B spans.jsonl is empty at {span_dir_b}"
    run_ids_a = {_span_run_id(s) for s in spans_a}
    run_ids_b = {_span_run_id(s) for s in spans_b}
    assert run_ids_a == {"e2e-parallel-A"}, (
        f"worker A span dir contains foreign run_ids: {run_ids_a}"
    )
    assert run_ids_b == {"e2e-parallel-B"}, (
        f"worker B span dir contains foreign run_ids: {run_ids_b}"
    )

    # Repo-level scope cleanliness: neither parallel wrapper may have
    # touched the *other* worker's repo. (Each repo's working tree
    # should be clean; the analyst is read-only by manifest.)
    for label, repo in (("A", repo_a), ("B", repo_b)):
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout.strip() == "", (
            f"worker {label}'s repo is dirty after dispatch:\n"
            f"{status.stdout}\n(expected analyst dispatch to be read-only)"
        )
