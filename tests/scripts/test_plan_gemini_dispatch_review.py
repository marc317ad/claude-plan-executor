"""TASK-003 tests for plan_gemini_dispatch.py review subcommand.

Seven test cases (acceptance criteria):
  1. Happy path — shim returns valid envelope, wrapper emits success.
  2. Schema-retry success on attempt 3 — first two responses fail schema
     validation, third validates; envelope.attempts == 3.
  3. Schema-retry exhaustion — all three responses fail; envelope is
     ``outcome=parse_error`` with attempts == 3 and a non-empty
     ``last_validation_error``.
  4. CLI OAuth path — both ``GEMINI_API_KEY`` and
     ``GOOGLE_APPLICATION_CREDENTIALS`` may be unset; the wrapper still
     invokes the CLI so it can use the local Gemini OAuth session.
  5. ``implement`` subcommand rejection — exits nonzero with stderr
     containing the canonical "implement subcommand not supported"
     phrase.
  6. ``plan-review`` subcommand stub — exits nonzero with stderr
     containing the canonical "plan-review subcommand not yet
     implemented; see TASK-004" phrase.
  7. CLI auth preservation — wrapper invocations do not override
     ``GEMINI_CLI_HOME`` and invoke Gemini in headless ``-p ''`` mode
     with the restrictive policy passed via ``--policy``.

The fake ``gemini`` shim is a tiny Python script (≤80 lines) overlaid
onto PATH via a tmp-dir prefix. It reads ``GEMINI_SHIM_MODE`` from the
environment to choose which response to emit. This keeps the test
hermetic — no live Gemini binary is invoked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
WRAPPER_PATH = (
    SCRIPTS_DIR / "plan_gemini_dispatch.py"
)
SCHEMA_PATH = (
    SCRIPTS_DIR / "gemini_review_schema.json"
)

sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402


def _load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "plan_gemini_dispatch", WRAPPER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Helpers — fake gemini shim, repo + plan fixtures.
# ---------------------------------------------------------------------------


def _git(args, cwd, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        check=check,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    return repo


def _plan_text(task_id: str, files: list[str]) -> str:
    files_block = "\n".join(f"  - {f}" for f in files)
    return textwrap.dedent(f"""\
        # Plan: gemini review fixture

        **Created:** 2026-04-25
        **Status:** ready
        **Base branch:** main

        ## Goal
        Fixture plan for plan_gemini_dispatch review-subcommand tests.

        ## Context
        Minimal plan exercising the wrapper with a fake gemini shim.

        ---

        ## Tasks

        ### TASK-{task_id}: Fixture review task

        - **Status:** pending
        - **Priority:** low
        - **Files:**
        {files_block}
        - **Dependencies:** none
        - **Test command:** none
        - **Acceptance criteria:**
          - All declared files exist.

        **Description:**
        A minimal task whose only job is to give the dispatcher a parseable
        block to render a review prompt against.
        """)


def _write_plan(repo: Path, task_id: str, files: list[str]) -> Path:
    plan_dir = repo / "docs" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "fixture_plan.md"
    plan_path.write_text(_plan_text(task_id, files), encoding="utf-8")
    return plan_path


def _route_payload(*, reviewer: str, verdict: str) -> dict:
    return {
        "task_id": "001",
        "implementer": "claude",
        "reviewer": reviewer,
        "claude_only": False,
        "unattended_revert_policy": "pause",
        "reviewer_envelope": {
            "verdict": verdict,
            "findings": [{"i": 0}] if verdict == "needs-rework" else [],
            "summary": "",
        },
        "d5_envelope": None,
        "retries_used": {
            "bounded_remediation": False,
            "narrow_remediation": False,
            "role_swap": False,
            "codex_fallback": False,
        },
        "flags": {"codex_review_binding": False, "skip_cross_review": False},
    }


@pytest.mark.parametrize("verdict", ["clean", "minor-findings", "needs-rework"])
def test_gemini_review_route_matches_codex_for_claude_work(verdict: str) -> None:
    """Gemini fallback review uses the Codex verdict vocabulary and must route
    identically on Claude-implemented work."""
    gemini = plan_ops.route(_route_payload(reviewer="gemini", verdict=verdict))
    codex = plan_ops.route(_route_payload(reviewer="codex", verdict=verdict))
    assert gemini["action"] == codex["action"]
    if verdict in {"clean", "minor-findings"}:
        assert gemini["args"]["commit_flags"] == codex["args"]["commit_flags"]
    else:
        assert gemini["args"]["dispatch_context"] == codex["args"]["dispatch_context"]


# Fake shim source. Pure stdlib. The shim emits the FULL Gemini ``-o json``
# envelope shape ``{"response": "<inner-json-as-string>", "stats": {...}}``;
# the wrapper unwraps the envelope and validates the inner ``response``
# string against the review schema. The shim:
#   - Reads GEMINI_SHIM_MODE from environment.
#   - Records every invocation under <GEMINI_SHIM_LOG>/calls.jsonl.
#   - Emits one of: a valid envelope, an envelope whose response field is
#     malformed JSON, an envelope whose response is structurally invalid
#     against the schema, or non-zero exit.
_VALID_INNER = {
    "task_id": "001",
    "verdict": "clean",
    "findings": [],
    "notes": [],
    "scope_ok": True,
    "acceptance_met": True,
    "summary": "No substantiated issues found in the fixture diff.",
}

_INVALID_MISSING_FIELDS = {
    "task_id": "001",
    "verdict": "clean",
    # Missing required fields: findings, notes, scope_ok, acceptance_met,
    # summary. jsonschema.validate will reject this.
}

_GEMINI_STATS = {
    "models": {
        "gemini-2.5-flash-lite": {
            "tokens": {"input": 0, "prompt": 0, "total": 0, "cached": 0},
        },
    },
}

FAKE_SHIM_TEMPLATE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json
    import os
    import sys
    from pathlib import Path

    log_dir = os.environ.get("GEMINI_SHIM_LOG")
    home = os.environ.get("GEMINI_CLI_HOME", "")
    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        with (Path(log_dir) / "calls.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({{
                "argv": sys.argv,
                "gemini_cli_home": home,
                "pid": os.getpid(),
            }}) + "\\n")

    mode = os.environ.get("GEMINI_SHIM_MODE", "valid")
    counter_path = os.environ.get("GEMINI_SHIM_COUNTER")
    attempt = 1
    if counter_path:
        cp = Path(counter_path)
        try:
            attempt = int(cp.read_text(encoding="utf-8").strip()) + 1
        except (FileNotFoundError, ValueError):
            attempt = 1
        cp.write_text(str(attempt), encoding="utf-8")

    VALID_INNER = {valid_inner}
    INVALID_INNER = {invalid_inner}
    STATS = {stats}

    def _envelope(response_str):
        return json.dumps({{"response": response_str, "stats": STATS}})

    if mode == "valid":
        sys.stdout.write(_envelope(json.dumps(VALID_INNER)))
        sys.exit(0)
    if mode == "invalid_then_valid":
        # First two attempts: envelope's response field is malformed JSON
        # (truncated). Third attempt: envelope's response is valid inner.
        if attempt < 3:
            sys.stdout.write(_envelope('{{"task_id": "001",'))
            sys.exit(0)
        sys.stdout.write(_envelope(json.dumps(VALID_INNER)))
        sys.exit(0)
    if mode == "always_invalid":
        # Envelope's response field is non-JSON garbage on every attempt.
        sys.stdout.write(_envelope("not json at all -- retry me"))
        sys.exit(1)
    if mode == "always_invalid_schema":
        # Envelope's response field parses as JSON but is structurally
        # invalid (missing required schema fields).
        sys.stdout.write(_envelope(json.dumps(INVALID_INNER)))
        sys.exit(0)
    if mode == "valid_but_nonzero_exit":
        # Envelope is valid AND inner JSON is schema-conformant, but the
        # subprocess exits non-zero. The wrapper must not return success.
        sys.stdout.write(_envelope(json.dumps(VALID_INNER)))
        sys.exit(7)
    sys.stderr.write("unknown shim mode: " + mode + "\\n")
    sys.exit(2)
    """)


@pytest.fixture
def shim_path(tmp_path):
    """Create a temporary directory containing a fake ``gemini`` shim
    executable. Returns the directory path so callers can prepend it to
    PATH. The shim's response mode is chosen via the ``GEMINI_SHIM_MODE``
    env var inherited by the subprocess."""
    shim_dir = tmp_path / "shim_bin"
    shim_dir.mkdir()
    shim_src = FAKE_SHIM_TEMPLATE.format(
        valid_inner=repr(_VALID_INNER),
        invalid_inner=repr(_INVALID_MISSING_FIELDS),
        stats=repr(_GEMINI_STATS),
    )
    shim_py = shim_dir / "gemini.py"
    shim_py.write_text(shim_src, encoding="utf-8")

    # POSIX wrapper script that invokes the shim via the current
    # interpreter so we don't depend on the shebang resolving.
    shim_sh = shim_dir / "gemini"
    shim_sh.write_text(
        "#!/bin/sh\nexec " + sys.executable + " " + str(shim_py) + " \"$@\"\n",
        encoding="utf-8",
    )
    shim_sh.chmod(0o755)
    return shim_dir


def _run_review(
    *,
    repo: Path,
    plan_path: Path,
    task_id: str,
    shim_dir: Path | None,
    extra_env: dict | None = None,
    extra_argv: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Prepend the shim dir so the wrapper's `gemini` lookup finds the
    # fake binary first.
    if shim_dir is not None:
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    # Default: no API-key/ADC env is required; the real CLI can use its
    # local OAuth session and the shim is hermetic.
    env.pop("GEMINI_API_KEY", None)
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

    argv = [
        sys.executable, str(WRAPPER_PATH), "review",
        "--plan-file", str(plan_path),
        "--task-id", task_id,
        "--repo-root", str(repo),
        "--timeout", "20",
    ]
    if extra_argv:
        argv.extend(extra_argv)

    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_review_happy_path(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    plan_path = _write_plan(repo, "001", ["docs/plans/fixture_plan.md"])

    log_dir = tmp_path / "shim_log"
    proc = _run_review(
        repo=repo,
        plan_path=plan_path,
        task_id="001",
        shim_dir=shim_path,
        extra_env={
            "GEMINI_SHIM_MODE": "valid",
            "GEMINI_SHIM_LOG": str(log_dir),
        },
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["task_id"] == "001"
    assert envelope["subcommand"] == "review"
    assert envelope["reviewer"] == "gemini"
    assert envelope["outcome"] == "success"
    assert envelope["gemini_exit_code"] == 0
    assert envelope["parsed"]["verdict"] == "clean"
    assert envelope["attempts"] == 1
    # Schema-validate the parsed body — wrapper claims it did, sanity check.
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    import jsonschema
    jsonschema.validate(instance=envelope["parsed"], schema=schema)


# ---------------------------------------------------------------------------
# 2. Schema-retry success on attempt 3
# ---------------------------------------------------------------------------


def test_review_schema_retry_success_on_attempt_three(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    plan_path = _write_plan(repo, "001", ["docs/plans/fixture_plan.md"])

    counter = tmp_path / "shim_counter.txt"
    proc = _run_review(
        repo=repo,
        plan_path=plan_path,
        task_id="001",
        shim_dir=shim_path,
        extra_env={
            "GEMINI_SHIM_MODE": "invalid_then_valid",
            "GEMINI_SHIM_COUNTER": str(counter),
        },
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "success", envelope
    assert envelope["attempts"] == 3, envelope
    assert envelope["parsed"]["verdict"] == "clean"


# ---------------------------------------------------------------------------
# 3. Schema-retry exhaustion -> parse_error
# ---------------------------------------------------------------------------


def test_review_schema_retry_exhaustion(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    plan_path = _write_plan(repo, "001", ["docs/plans/fixture_plan.md"])

    proc = _run_review(
        repo=repo,
        plan_path=plan_path,
        task_id="001",
        shim_dir=shim_path,
        extra_env={
            "GEMINI_SHIM_MODE": "always_invalid_schema",
        },
    )

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "parse_error", envelope
    assert envelope["attempts"] == 3, envelope
    assert envelope["last_validation_error"], envelope
    assert envelope["reviewer"] == "gemini", envelope


# ---------------------------------------------------------------------------
# 3b. Valid envelope with non-zero exit code -> failure (not success)
# ---------------------------------------------------------------------------


def test_review_valid_envelope_with_nonzero_exit_is_failure(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    plan_path = _write_plan(repo, "001", ["docs/plans/fixture_plan.md"])

    proc = _run_review(
        repo=repo,
        plan_path=plan_path,
        task_id="001",
        shim_dir=shim_path,
        extra_env={"GEMINI_SHIM_MODE": "valid_but_nonzero_exit"},
    )

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "failure", envelope
    assert envelope["gemini_exit_code"] == 7, envelope
    assert "exited 7" in envelope["error"], envelope


# ---------------------------------------------------------------------------
# 4. CLI OAuth path does not require API-key env
# ---------------------------------------------------------------------------


def test_review_without_api_key_invokes_cli_oauth_path(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    plan_path = _write_plan(repo, "001", ["docs/plans/fixture_plan.md"])

    log_dir = tmp_path / "shim_log_missing_key"
    proc = _run_review(
        repo=repo,
        plan_path=plan_path,
        task_id="001",
        shim_dir=shim_path,
        extra_env={
            "GEMINI_API_KEY": None,
            "GOOGLE_APPLICATION_CREDENTIALS": None,
            "GEMINI_SHIM_MODE": "valid",
            "GEMINI_SHIM_LOG": str(log_dir),
        },
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "success", envelope
    calls_file = log_dir / "calls.jsonl"
    assert calls_file.exists(), "shim should be invoked so CLI OAuth can work"
    call = json.loads(calls_file.read_text(encoding="utf-8").splitlines()[0])
    assert call["gemini_cli_home"] == ""
    assert "--policy" in call["argv"]
    assert "--policy-file" not in call["argv"]
    assert "-p" in call["argv"]


# ---------------------------------------------------------------------------
# 5. implement subcommand rejection
# ---------------------------------------------------------------------------


def test_implement_subcommand_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    plan_path = _write_plan(repo, "001", ["docs/plans/fixture_plan.md"])

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = "test"
    proc = subprocess.run(
        [
            sys.executable, str(WRAPPER_PATH), "implement",
            "--plan-file", str(plan_path),
            "--task-id", "001",
            "--repo-root", str(repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0, proc.stdout
    assert "implement subcommand not supported" in proc.stderr, proc.stderr


# ---------------------------------------------------------------------------
# 6. plan-review subcommand argparse contract
#
# TASK-004 implemented the plan-review subcommand and removed the
# TASK-003 stub. The subcommand now requires --schedule-file; calling
# `plan-review` without it should be a clean argparse failure rather
# than the retired "not yet implemented" stub message.
# ---------------------------------------------------------------------------


def test_plan_review_requires_schedule_file(tmp_path):
    repo = _make_repo(tmp_path)

    env = os.environ.copy()
    env["GEMINI_API_KEY"] = "test"
    proc = subprocess.run(
        [
            sys.executable, str(WRAPPER_PATH), "plan-review",
            "--repo-root", str(repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0, proc.stdout
    # argparse surfaces missing-required-arg errors on stderr.
    assert "--schedule-file" in proc.stderr, proc.stderr
    # The retired TASK-003 stub error must NOT appear; the subcommand
    # is now implemented.
    assert (
        "not yet implemented" not in proc.stderr
        and "see TASK-004" not in proc.stderr
    ), proc.stderr


# ---------------------------------------------------------------------------
# 7. Gemini CLI auth preservation
# ---------------------------------------------------------------------------


def test_gemini_cli_home_is_not_overridden(tmp_path, shim_path):
    """The wrapper must not mask the operator's authenticated Gemini CLI
    home. Policy is supplied with --policy and the prompt is sent via
    headless -p mode."""
    repo_a = _make_repo(tmp_path / "a")
    repo_b = _make_repo(tmp_path / "b")
    plan_a = _write_plan(repo_a, "001", ["docs/plans/fixture_plan.md"])
    plan_b = _write_plan(repo_b, "001", ["docs/plans/fixture_plan.md"])

    log_a = tmp_path / "log_a"
    log_b = tmp_path / "log_b"

    results: dict[str, subprocess.CompletedProcess] = {}

    def _go(label: str, repo: Path, plan: Path, log: Path):
        proc = _run_review(
            repo=repo,
            plan_path=plan,
            task_id="001",
            shim_dir=shim_path,
            extra_env={
                "GEMINI_SHIM_MODE": "valid",
                "GEMINI_SHIM_LOG": str(log),
            },
        )
        results[label] = proc

    t_a = threading.Thread(target=_go, args=("a", repo_a, plan_a, log_a))
    t_b = threading.Thread(target=_go, args=("b", repo_b, plan_b, log_b))
    t_a.start()
    t_b.start()
    t_a.join(timeout=60)
    t_b.join(timeout=60)

    assert "a" in results and "b" in results
    assert results["a"].returncode == 0, results["a"].stderr
    assert results["b"].returncode == 0, results["b"].stderr

    def _read_calls(log_dir: Path) -> list[dict]:
        calls_file = log_dir / "calls.jsonl"
        assert calls_file.exists(), f"shim never logged a call under {log_dir}"
        return [
            json.loads(line)
            for line in calls_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    calls_a = _read_calls(log_a)
    calls_b = _read_calls(log_b)
    home_a = calls_a[0]["gemini_cli_home"]
    home_b = calls_b[0]["gemini_cli_home"]

    assert home_a == "", calls_a
    assert home_b == "", calls_b
    for call in (calls_a[0], calls_b[0]):
        assert "--policy" in call["argv"]
        assert "--policy-file" not in call["argv"]
        assert "-p" in call["argv"]


# ---------------------------------------------------------------------------
# PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002 — preserve-only smoke
#
# Direct unit-level smoke for the gemini wrapper's timeout-cleanup path
# under ``unattended_revert_policy="preserve-only"`` (AC #6). The gemini
# wrapper reuses ``_handle_timeout_cleanup`` from plan_codex_dispatch
# (imported at gemini-line-79); we exercise the function via the gemini
# module's import binding to prove the codepath is wired.
# ---------------------------------------------------------------------------


class TestUnattendedRevertPolicyPreserveOnlySmokeGemini:
    """Asserts that under ``unattended_revert_policy="preserve-only"``
    the gemini wrapper's timeout-cleanup path threads the policy through
    to its result envelope and that out-of-scope writes are observed-only
    — the gemini wrapper has no destructive path that can erase
    out-of-scope work, regardless of policy."""

    def _init_git_repo(self, repo_root: Path, file_name: str,
                       initial: str) -> None:
        subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=repo_root, check=True)
        subprocess.run(["git", "config", "user.name", "t"],
                       cwd=repo_root, check=True)
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=repo_root, check=True,
        )
        (repo_root / file_name).write_text(initial, encoding="utf-8")
        subprocess.run(["git", "add", file_name], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"],
            cwd=repo_root, check=True,
        )

    def test_preserve_only_threads_policy_and_observes_out_of_scope(
        self, tmp_path,
    ) -> None:
        scripts_dir = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
        sys.path.insert(0, str(scripts_dir))
        # Import the gemini module so the test exercises the gemini
        # wrapper's import binding (`_handle_timeout_cleanup` re-exported
        # at plan_gemini_dispatch.py:88).
        import plan_gemini_dispatch as pgd  # noqa: E402
        import plan_codex_dispatch as pcd  # noqa: E402

        # Sanity: the gemini wrapper resolves its policy helper to the
        # exact same callable as the codex wrapper (no shadowing).
        assert pgd._handle_timeout_cleanup is pcd._handle_timeout_cleanup

        self._init_git_repo(tmp_path, "in_scope.py", "ORIGINAL\n")
        baseline = pcd._snapshot_baseline(str(tmp_path))
        assert baseline["captured"] is True

        # Implementer wrote an out-of-scope untracked file before the
        # gemini CLI timed out.
        (tmp_path / "out_of_scope.txt").write_text("evil\n", encoding="utf-8")

        result = pgd._handle_timeout_cleanup(
            str(tmp_path), ["in_scope.py"], baseline,
            authorization_source="wrapper_internal_cleanup_explicit_declaration",
            unattended_revert_policy="preserve-only",
        )

        # AC #2 / AC #6: policy threaded through into result envelope.
        assert result.get("unattended_revert_policy") == "preserve-only"
        # The gemini timeout cleanup is in-scope-only (delegates to the
        # shared codex helper); out-of-scope writes are observed only.
        assert (tmp_path / "out_of_scope.txt").exists()
        assert "out_of_scope.txt" in result["out_of_scope_untracked"]
        assert result["out_of_scope_observed"] is True

    def test_resolve_unattended_revert_policy_helper_reads_env(
        self, monkeypatch,
    ) -> None:
        scripts_dir = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
        sys.path.insert(0, str(scripts_dir))
        import plan_gemini_dispatch as pgd  # noqa: E402

        class _NS:
            unattended_revert_policy = None

        monkeypatch.setenv("UNATTENDED_REVERT_POLICY", "preserve-only")
        assert pgd._resolve_unattended_revert_policy(_NS()) == "preserve-only"

        monkeypatch.delenv("UNATTENDED_REVERT_POLICY", raising=False)
        assert pgd._resolve_unattended_revert_policy(_NS()) is None
