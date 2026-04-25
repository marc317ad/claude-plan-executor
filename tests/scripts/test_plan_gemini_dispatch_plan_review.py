"""TASK-004 tests for plan_gemini_dispatch.py plan-review subcommand.

Acceptance scenarios exercised here:
  1. Happy path — shim returns valid plan-review envelope, wrapper emits
     success with parsed payload validating against
     gemini_plan_review_schema.json.
  2. --allow-gaps demotion clause is present in the rendered prompt iff
     the persisted schedule has only soft gaps (and outcome is
     ``needs-enrichment``). Verified via the GEMINI_DISPATCH_DEBUG=1
     prompt-dump file.
  3. Hard-gap schedule does NOT inject the demotion clause even with
     --allow-gaps. Same prompt-dump inspection.
  4. Schema-retry exhaustion → outcome=parse_error.
  5. Missing API key short-circuits before subprocess spawn (shim is
     never invoked).
  6. Valid envelope + non-zero exit code → outcome=failure (the wrapper
     does NOT silently treat parseable JSON as success when the
     subprocess itself exited non-zero).
  7. The shared ALLOW_GAPS_DEMOTION_CLAUSE constant + the shared
     _should_inject_allow_gaps_demotion helper are imported from
     _plan_paths.py by both wrappers (sanity-check the lockstep contract).

The fake ``gemini`` shim is reused conceptually from TASK-003: a small
Python script overlaid onto PATH via a tmp-dir prefix that emits the
full Gemini ``-o json`` envelope shape.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
WRAPPER_PATH = SCRIPTS_DIR / "plan_gemini_dispatch.py"
PLAN_REVIEW_SCHEMA = SCRIPTS_DIR / "gemini_plan_review_schema.json"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "plan_gemini_dispatch", WRAPPER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Helpers — fake gemini shim, repo + schedule fixtures.
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


def _write_schedule(
    tmp_path: Path,
    *,
    gaps: list[dict] | None = None,
    outcome: str = "valid",
    stem: str = "fixture_plan",
) -> Path:
    """Persist a minimal schedule JSON sidecar at ``<tmp>/<stem>.schedule.json``."""
    schedule = tmp_path / f"{stem}.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": outcome,
            "tasks": [],
            "batches": [],
            "gaps": gaps or [],
            "risks": [],
        }, indent=2),
        encoding="utf-8",
    )
    return schedule


# Canned plan-review envelope body (validates against
# gemini_plan_review_schema.json).
_VALID_INNER = {
    "plan_file": "fixture_plan",
    "verdict": "approved",
    "findings": [],
    "notes": [],
    "schedule_ok": True,
    "summary": "Fixture schedule reviewed cleanly under the test shim.",
}

_INVALID_INNER_MISSING_FIELDS = {
    "plan_file": "fixture_plan",
    # Missing required fields: verdict, findings, notes, schedule_ok, summary.
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
    executable. The shim's response mode is chosen via the
    ``GEMINI_SHIM_MODE`` env var inherited by the subprocess."""
    shim_dir = tmp_path / "shim_bin"
    shim_dir.mkdir()
    shim_src = FAKE_SHIM_TEMPLATE.format(
        valid_inner=repr(_VALID_INNER),
        invalid_inner=repr(_INVALID_INNER_MISSING_FIELDS),
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


def _run_plan_review(
    *,
    schedule: Path,
    repo: Path,
    shim_dir: Path | None,
    extra_env: dict | None = None,
    extra_argv: list[str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Prepend the shim dir so the wrapper's `gemini` lookup finds the
    # fake binary first.
    if shim_dir is not None:
        env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    # Default: provide an API key so the short-circuit doesn't fire.
    env.setdefault("GEMINI_API_KEY", "test-key-not-real")
    env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v

    argv = [
        sys.executable, str(WRAPPER_PATH), "plan-review",
        "--schedule-file", str(schedule),
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


def _read_debug_prompt(debug_dir: Path) -> str:
    """Pull the rendered prompt out of GEMINI_DISPATCH_DEBUG=1 dumps."""
    if not debug_dir.is_dir():
        pytest.fail(f"debug dir {debug_dir} was not created")
    files = list(debug_dir.glob("plan_review_*.txt"))
    assert files, f"no debug prompt files under {debug_dir}"
    # One subprocess per test → one file is expected; tolerate >1 in
    # case of retries by joining them.
    return "\n".join(f.read_text(encoding="utf-8") for f in sorted(files))


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_plan_review_happy_path(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    schedule = _write_schedule(tmp_path)

    log_dir = tmp_path / "shim_log"
    proc = _run_plan_review(
        schedule=schedule,
        repo=repo,
        shim_dir=shim_path,
        extra_env={
            "GEMINI_SHIM_MODE": "valid",
            "GEMINI_SHIM_LOG": str(log_dir),
        },
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["task_id"] == "plan", envelope
    assert envelope["subcommand"] == "plan-review", envelope
    assert envelope["reviewer"] == "gemini", envelope
    assert envelope["outcome"] == "success", envelope
    assert envelope["gemini_exit_code"] == 0, envelope
    # plan_file mirrors the Codex wrapper's identifier (the schedule's
    # sidecar stem in this fixture).
    assert envelope["plan_file"] == "fixture_plan", envelope
    parsed = envelope["parsed"]
    assert parsed["verdict"] == "approved", parsed
    # Schema-validate the parsed body — wrapper claims it did, sanity check.
    schema = json.loads(PLAN_REVIEW_SCHEMA.read_text(encoding="utf-8"))
    import jsonschema
    jsonschema.validate(instance=parsed, schema=schema)


# ---------------------------------------------------------------------------
# 2. --allow-gaps demotion clause is present iff schedule has only soft gaps.
# ---------------------------------------------------------------------------


_DEMOTION_CLAUSE_NEEDLE = "Operator override (--allow-gaps)"


def test_plan_review_allow_gaps_soft_only_injects_clause(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    soft_gaps = [
        {"type": "unresolvable-test", "detail": "foo", "severity": "soft"},
        {"type": "empty-implementation-notes", "detail": "bar", "severity": "soft"},
    ]
    schedule = _write_schedule(
        tmp_path, gaps=soft_gaps, outcome="needs-enrichment",
    )

    debug_dir = Path(tempfile.gettempdir()) / "plan_gemini_dispatch_debug"
    # Clean any stale debug dumps before the run.
    if debug_dir.is_dir():
        shutil.rmtree(debug_dir, ignore_errors=True)

    proc = _run_plan_review(
        schedule=schedule,
        repo=repo,
        shim_dir=shim_path,
        extra_env={
            "GEMINI_SHIM_MODE": "valid",
            "GEMINI_DISPATCH_DEBUG": "1",
        },
        extra_argv=["--allow-gaps"],
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    prompt = _read_debug_prompt(debug_dir)
    assert _DEMOTION_CLAUSE_NEEDLE in prompt, (
        "demotion clause missing from prompt when --allow-gaps + soft gaps"
    )
    assert "approved-with-notes" in prompt
    assert "mention that demotion in the `summary`" in prompt


# ---------------------------------------------------------------------------
# 3. Hard-gap schedule does NOT inject the demotion clause even with --allow-gaps.
# ---------------------------------------------------------------------------


def test_plan_review_allow_gaps_hard_gaps_block_clause(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    mixed_gaps = [
        {"type": "unresolvable-test", "detail": "foo", "severity": "soft"},
        {"type": "stale-path", "detail": "baz", "severity": "hard"},
    ]
    schedule = _write_schedule(
        tmp_path, gaps=mixed_gaps, outcome="needs-enrichment",
    )

    debug_dir = Path(tempfile.gettempdir()) / "plan_gemini_dispatch_debug"
    if debug_dir.is_dir():
        shutil.rmtree(debug_dir, ignore_errors=True)

    proc = _run_plan_review(
        schedule=schedule,
        repo=repo,
        shim_dir=shim_path,
        extra_env={
            "GEMINI_SHIM_MODE": "valid",
            "GEMINI_DISPATCH_DEBUG": "1",
        },
        extra_argv=["--allow-gaps"],
    )

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    prompt = _read_debug_prompt(debug_dir)
    assert _DEMOTION_CLAUSE_NEEDLE not in prompt, (
        "demotion clause must be suppressed when any gap is hard-severity"
    )
    # Verdict vocab remains intact regardless.
    assert "needs-replan" in prompt


# ---------------------------------------------------------------------------
# 4. Schema-retry exhaustion → parse_error.
# ---------------------------------------------------------------------------


def test_plan_review_schema_retry_exhaustion(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    schedule = _write_schedule(tmp_path)

    proc = _run_plan_review(
        schedule=schedule,
        repo=repo,
        shim_dir=shim_path,
        extra_env={"GEMINI_SHIM_MODE": "always_invalid_schema"},
    )

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "parse_error", envelope
    assert envelope["attempts"] == 3, envelope
    assert envelope["last_validation_error"], envelope
    assert envelope["reviewer"] == "gemini", envelope
    assert envelope["task_id"] == "plan", envelope
    assert envelope["plan_file"] == "fixture_plan", envelope


# ---------------------------------------------------------------------------
# 5. Missing API key short-circuits before subprocess spawn.
# ---------------------------------------------------------------------------


def test_plan_review_missing_api_key_short_circuits(tmp_path, shim_path):
    repo = _make_repo(tmp_path)
    schedule = _write_schedule(tmp_path)

    log_dir = tmp_path / "shim_log_missing_key"
    proc = _run_plan_review(
        schedule=schedule,
        repo=repo,
        shim_dir=shim_path,
        extra_env={
            "GEMINI_API_KEY": None,
            "GOOGLE_APPLICATION_CREDENTIALS": None,
            "GEMINI_SHIM_MODE": "valid",
            "GEMINI_SHIM_LOG": str(log_dir),
        },
    )

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "failure", envelope
    assert envelope["task_id"] == "plan", envelope
    assert envelope["error"] == (
        "missing GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS"
    ), envelope
    # The shim must NEVER have been invoked — short-circuit fires
    # BEFORE subprocess spawn (headless-OAuth contract).
    assert not (log_dir / "calls.jsonl").exists(), (
        "shim was invoked despite missing-API-key short-circuit; "
        "this defeats the headless-OAuth contract"
    )


# ---------------------------------------------------------------------------
# 6. Valid envelope + non-zero exit code → outcome=failure.
# ---------------------------------------------------------------------------


def test_plan_review_valid_envelope_with_nonzero_exit_is_failure(
    tmp_path, shim_path,
):
    repo = _make_repo(tmp_path)
    schedule = _write_schedule(tmp_path)

    proc = _run_plan_review(
        schedule=schedule,
        repo=repo,
        shim_dir=shim_path,
        extra_env={"GEMINI_SHIM_MODE": "valid_but_nonzero_exit"},
    )

    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    envelope = json.loads(proc.stdout)
    assert envelope["outcome"] == "failure", envelope
    assert envelope["gemini_exit_code"] == 7, envelope
    assert envelope["task_id"] == "plan", envelope
    assert envelope["plan_file"] == "fixture_plan", envelope
    assert "exited 7" in envelope["error"], envelope


# ---------------------------------------------------------------------------
# 7. Shared constant + helper come from _plan_paths.py for both wrappers.
# ---------------------------------------------------------------------------


def test_demotion_clause_constant_and_helper_are_shared():
    """Both wrappers must import ``ALLOW_GAPS_DEMOTION_CLAUSE`` and
    ``_should_inject_allow_gaps_demotion`` from ``_plan_paths.py``.
    Drift between the two wrappers' demotion paths is the failure mode
    this test exists to prevent.
    """
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    import _plan_paths  # noqa: F401  — imported for identity comparison
    import importlib

    # The Gemini wrapper is already loaded as `wrapper`. Load the Codex
    # wrapper too and confirm both modules see the same constant +
    # helper objects (identity, not just equality).
    spec = importlib.util.spec_from_file_location(
        "plan_codex_dispatch", SCRIPTS_DIR / "plan_codex_dispatch.py",
    )
    codex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(codex)

    assert wrapper.ALLOW_GAPS_DEMOTION_CLAUSE is _plan_paths.ALLOW_GAPS_DEMOTION_CLAUSE
    assert codex.ALLOW_GAPS_DEMOTION_CLAUSE is _plan_paths.ALLOW_GAPS_DEMOTION_CLAUSE
    assert (
        wrapper._should_inject_allow_gaps_demotion
        is _plan_paths._should_inject_allow_gaps_demotion
    )
    assert (
        codex._should_inject_allow_gaps_demotion
        is _plan_paths._should_inject_allow_gaps_demotion
    )
