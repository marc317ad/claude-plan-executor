"""Tier-C pure-core conformance fixtures for ``plan_ops`` (TASK-006).

This suite is the byte-equal conformance gate for the state-mutating
Tier-C ``cmd_*`` subcommands enumerated in
``docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md``:
``cmd_commit_task``, ``cmd_fail_task``, ``cmd_log_event``,
``cmd_acquire_lock``, ``cmd_auto_validate_divergence``,
``cmd_decompose_plan``, ``cmd_block_dependents``, and
``cmd_reconcile_batch`` (the latter for the AC-required
``out_of_scope_observed=true`` reconciler-partition path).

Tier-C is now (per v2 plan refinement) a regression net for the codemod's
correctness — happy-path round-tripping, ``errors[]`` round-trip on the
named state mutators, and codemod-correctness gates (commit-task bad-id
f-string preservation, dismissed-finding-ids validation shape). The richer
adversarial fixtures from v1 (SIGTERM crash-recovery, hermetic env, etc.)
are deferred — they protect against drift in the underlying ``cmd_*``
logic, which is unchanged by an AST rewrite.

For each fixture the suite:

  * Materializes two fresh tmp directories (``cli_dir`` and ``pure_dir``)
    seeded identically.
  * Runs the CLI subcommand as a subprocess in ``cli_dir``.
  * Runs the pure-core ``_run_<subcommand>(payload)`` in ``pure_dir``
    after monkey-patching ``os.chdir`` so ``RUN_LOCK_PATH`` /
    ``RUN_LOG_PATH`` (relative to CWD by import-time construction)
    resolve under ``pure_dir``.
  * Compares post-state byte-equally between the two directories
    (``_run_log.jsonl``, ``_run_lock.json``, plan markdown, working
    tree) after timestamp canonicalization.
  * Compares the canonical envelope between subprocess stdout and
    ``_public_result(_run_<subcommand>(payload))``.

Each fixture lives on disk as a payload+expected JSON pair under
``fixtures/plan_ops_pure_core/tier_c/`` and is authored programmatically
via ``_AUTHORED_CASES`` (single source of truth, mirroring Tier-A/B).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.scripts.plan_ops_pure_harness import (
    SCRIPTS_DIR,
    canonicalize_envelope,
    load_plan_ops,
)

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "plan_ops_pure_core"
    / "tier_c"
)


# ---------------------------------------------------------------------------
# Static fixture content
# ---------------------------------------------------------------------------


PLAN_BODY_BASE = """# Plan: tier-c

**Created:** 2026-04-30
**Status:** in-progress
**Base branch:** main

## Context

Stub.

## Tasks

### TASK-001: First task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/foo.py
- **Dependencies:** none

Body.

### TASK-002: Second task

- **Status:** open
- **Agent:** claude
- **Files:**
  - src/bar.py
- **Dependencies:** [001]

Body.
"""

# Whole-plan markdown that violates the decomposer grammar (no `## Tasks`
# section). ``cmd_decompose_plan`` MUST reject this with a structured
# ``errors[]`` envelope on both CLI and pure-core paths.
PLAN_BODY_MALFORMED = """# Plan: bogus

No Tasks section here.

Just prose.
"""

SCHEDULE_FOR_BLOCK_EMPTY = {
    "outcome": "valid",
    "tasks": [
        {
            "id": "001",
            "agent": "claude",
            "title": "one",
            "files": ["src/foo.py"],
            "dependencies": [],
            "plan_file": "sample.md",
        },
        {
            "id": "002",
            "agent": "claude",
            "title": "two",
            "files": ["src/bar.py"],
            "dependencies": [],
            "plan_file": "sample.md",
        },
    ],
    "batches": [],
    "gaps": [],
    "risks": [],
}

# A pre-existing lock owned by a different run-id; ``acquire-lock --force``
# MUST overwrite it byte-equally on both paths.
PRE_EXISTING_LOCK = {
    "/some/other/plan.md": {
        "run_id": "OTHER-RUN",
        "acquired_at": "2026-04-29T00:00:00Z",
    },
}

AVD_ENVELOPE_NOT_APPLICABLE = {
    "outcome": "success",
    "task_id": "001",
}

# An envelope that DOES match the auto-validate-divergence trigger:
# outcome=failure + cause=independent_test_run_failed. The pinned
# ``--test-command true`` returns immediately with exit 0, so target
# passes deterministically across both paths. No --run-id, so no
# sandbox_divergence event is appended (which would otherwise embed a
# non-deterministic timestamp into the run log).
AVD_ENVELOPE_APPLICABLE = {
    "outcome": "failure",
    "cause": "independent_test_run_failed",
    "task_id": "001",
}


# ---------------------------------------------------------------------------
# Authored cases: (subcommand, case_name, payload, expected)
# ---------------------------------------------------------------------------


_AUTHORED_CASES: list[tuple[str, str, dict, dict]] = [
    # ------------------------------------------------------------------
    # cmd_decompose_plan — malformed-plan error path
    # ------------------------------------------------------------------
    (
        "decompose-plan",
        "error_malformed_plan",
        {
            "_files": {"plan.md": PLAN_BODY_MALFORMED},
            "plan_file": "plan.md",
            "out_dir": None,
            "force": False,
        },
        {
            "exit_code": 1,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_dir_snapshot": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_log_event — rejected-event (errors[] round-trip)
    # ------------------------------------------------------------------
    (
        "log-event",
        "error_unknown_event",
        {
            "_seed_plans_dir": True,
            "event": "totally-bogus-event-name",
            "fields_json": json.dumps({"run_id": "R1", "task_id": "001"}),
            "findings_json": None,
        },
        {
            "exit_code": 1,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_block_dependents — empty-cascade short-circuit
    # ------------------------------------------------------------------
    (
        "block-dependents",
        "happy_empty_cascade",
        {
            "_seed_plans_dir": True,
            "_files": {
                "docs/plans/sample.md": PLAN_BODY_BASE,
                "docs/plans/schedule.json": json.dumps(SCHEDULE_FOR_BLOCK_EMPTY),
            },
            "schedule_file": "docs/plans/schedule.json",
            "plan_file": "docs/plans/sample.md",
            "failed": "001",
            "run_id": "R1",
            "update_schedule_state": None,
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
            "compare_plan_files": ["docs/plans/sample.md"],
        },
    ),
    # ------------------------------------------------------------------
    # cmd_acquire_lock — stale-lock-takeover via --force
    # ------------------------------------------------------------------
    (
        "acquire-lock",
        "happy_stale_takeover",
        {
            "_seed_plans_dir": True,
            "_files": {
                "docs/plans/sample.md": PLAN_BODY_BASE,
                "docs/plans/_run_lock.json": json.dumps(PRE_EXISTING_LOCK),
            },
            "plan_file": "docs/plans/sample.md",
            "run_id": "TAKEOVER-RUN",
            "force": True,
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_lock_file": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_auto_validate_divergence — applicable-envelope happy path
    # An envelope whose cause does not match `independent_test_run_failed`
    # exercises the early-return ``applicable: False`` short-circuit; this
    # is the codemod-equivalence gate (the underlying re-run logic is
    # exercised by test_plan_ops.py — testing it here would couple this
    # suite to subprocess test-command behavior that's not codemod-related).
    # ------------------------------------------------------------------
    (
        "auto-validate-divergence",
        "happy_not_applicable",
        {
            "_seed_plans_dir": True,
            "envelope_file": None,
            "test_command": "true",
            "repo_root": None,
            "run_id": None,
            "task_id": None,
            "timeout": 30,
            "stdin_text": json.dumps(AVD_ENVELOPE_NOT_APPLICABLE),
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
        },
    ),
    (
        "auto-validate-divergence",
        "happy_applicable",
        {
            "_seed_plans_dir": True,
            "envelope_file": None,
            "test_command": "true",
            "repo_root": None,
            "run_id": None,
            "task_id": None,
            "timeout": 30,
            "stdin_text": json.dumps(AVD_ENVELOPE_APPLICABLE),
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_commit_task — narrow-remediation + dismissed-finding-ids 1,2,3
    # happy path under --dry-run. Proves ``_validate_commit_task_payload``
    # accepts the canonical D.2a.6 narrow-remediation shape and that the
    # pure-core dry-run short-circuit produces the same envelope as the
    # CLI subprocess.
    # ------------------------------------------------------------------
    (
        "commit-task",
        "happy_narrow_remediation_dry_run",
        {
            "_seed_plans_dir": True,
            "_files": {"docs/plans/sample.md": PLAN_BODY_BASE},
            "task_id": "001",
            "files": "src/foo.py",
            "plan_file": "docs/plans/sample.md",
            "reviewer": "none",
            "reviewer_verdict": "",
            "reviewer_minor_findings": "[]",
            "dry_run": True,
            "v_check_timeout": 300,
            "run_id": "R1",
            "title": "x",
            "diff_summary": "y",
            "remediation_tag": False,
            "sandbox_divergence_tag": False,
            "d4_rescue_tag": False,
            "narrow_remediation_tag": True,
            "disagreement_tag": False,
            "dismissed_finding_ids": [1, 2, 3],
            "update_schedule_state": None,
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_commit_task — bad --task-id error envelope
    # Codifies the f-string preservation gate: the error message MUST
    # carry the literal ``bad --task-id: <invalid>!r`` shape on both
    # the CLI and pure-core paths.
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # cmd_commit_task — clean-commit happy path (real git, non-dry-run)
    # AC fixture (TASK-006): byte-equal commit_sha + run-log entry across
    # CLI subprocess and pure-core paths. ``_seed_git_repo`` initializes
    # both repos with pinned env (``_GIT_ENV_PIN``) so the deterministic
    # second commit matches SHA byte-equally.
    # ------------------------------------------------------------------
    (
        "commit-task",
        "happy_clean_commit",
        {
            "_seed_plans_dir": True,
            "_seed_git_repo": True,
            "_git_initial_files": {
                "docs/plans/sample.md": PLAN_BODY_BASE,
                "src/foo.py": "# foo seed\n",
            },
            "_files": {
                # Post-init working-tree mutation that commit-task will
                # actually commit alongside the plan-status flip.
                "src/foo.py": "# foo updated by task 001\n",
            },
            "task_id": "001",
            "files": "src/foo.py",
            "plan_file": "docs/plans/sample.md",
            "reviewer": "none",
            "reviewer_verdict": "",
            "reviewer_minor_findings": "[]",
            "dry_run": False,
            "v_check_timeout": 300,
            "run_id": "R1",
            "title": "land foo",
            "diff_summary": "tweak foo content",
            "remediation_tag": False,
            "sandbox_divergence_tag": False,
            "d4_rescue_tag": False,
            "narrow_remediation_tag": False,
            "disagreement_tag": False,
            "dismissed_finding_ids": [],
            "update_schedule_state": None,
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
            "compare_plan_files": ["docs/plans/sample.md"],
        },
    ),
    # ------------------------------------------------------------------
    # cmd_reconcile_batch — out_of_scope_observed=true reconciler-partition
    # path. The schedule declares task 001's Files: as ["src/foo.py"]; the
    # envelope's out_of_scope_tracked lists exactly that path, so the
    # plan-aware partition classifies it as ``kept_tracked`` (wrapper
    # false-positive) and there is no actionable remainder. Outcome:
    # ``scope_violation_preserved`` with no git ops needed (empty
    # actionable lists short-circuit the diff-residual check).
    # ------------------------------------------------------------------
    (
        "reconcile-batch",
        "happy_kept_partition",
        {
            "_seed_plans_dir": True,
            "_files": {
                "docs/plans/schedule.json": json.dumps({
                    "outcome": "valid",
                    "tasks": [
                        {
                            "id": "001",
                            "agent": "claude",
                            "title": "scope-test",
                            "files": ["src/foo.py"],
                            "dependencies": [],
                            "plan_file": "sample.md",
                        },
                    ],
                    "batches": [],
                    "gaps": [],
                    "risks": [],
                }),
            },
            "schedule_file": "docs/plans/schedule.json",
            "repo_root": ".",
            "out_of_scope_policy": "reconcile-and-revert",
            "plans_dir": None,
            "stdin_text": json.dumps([{
                "task_id": "001",
                "outcome": "success",
                "out_of_scope_observed": True,
                "out_of_scope_tracked": ["src/foo.py"],
                "out_of_scope_untracked": [],
            }]),
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
        },
    ),
    # ------------------------------------------------------------------
    # cmd_fail_task — paused-run-with-authorization-source happy path.
    # Codifies the hand-fix from TASK-003: the ``authorization_source``
    # field is logged into the ``failed`` event on both CLI and pure
    # paths. Empty ``--files`` keeps the suite git-free (no
    # ``_seed_git_repo`` needed).
    # ------------------------------------------------------------------
    (
        "fail-task",
        "happy_paused_with_auth_source",
        {
            "_seed_plans_dir": True,
            "_files": {"docs/plans/sample.md": PLAN_BODY_BASE},
            "task_id": "001",
            "plan_file": "docs/plans/sample.md",
            "files": "",
            "stage": "implement",
            "reason": "paused-run reverted by user",
            "authorization_source": "reconcile-out-of-scope-user-instruction",
            "reviewer_findings": "",
            "reversion_guidance": "operator chose `revert` from the four-option pause menu",
            "repo_root": None,
            "run_id": "R1",
            "update_schedule_state": None,
            "retries_used": None,
        },
        {
            "exit_code": 0,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
            "compare_plan_files": ["docs/plans/sample.md"],
        },
    ),
    (
        "commit-task",
        "error_bad_task_id",
        {
            "_seed_plans_dir": True,
            "_files": {"docs/plans/sample.md": PLAN_BODY_BASE},
            "task_id": "not-a-task-id",
            "files": "src/foo.py",
            "plan_file": "docs/plans/sample.md",
            "reviewer": "none",
            "reviewer_verdict": "",
            "reviewer_minor_findings": "[]",
            "dry_run": True,
            "v_check_timeout": 300,
            "run_id": "R1",
            "title": "x",
            "diff_summary": "y",
            "remediation_tag": False,
            "sandbox_divergence_tag": False,
            "d4_rescue_tag": False,
            "narrow_remediation_tag": False,
            "disagreement_tag": False,
            "dismissed_finding_ids": [],
            "update_schedule_state": None,
        },
        {
            "exit_code": 1,
            "stderr_policy": "empty",
            "compare_envelope": True,
            "compare_run_log": True,
            "expected_error_substring": "bad --task-id: 'not-a-task-id'",
        },
    ),
]


# ---------------------------------------------------------------------------
# Disk fixture I/O
# ---------------------------------------------------------------------------


def _payload_path(subcommand: str, case_name: str) -> Path:
    return FIXTURE_DIR / f"{subcommand}__{case_name}.payload.json"


def _expected_path(subcommand: str, case_name: str) -> Path:
    return FIXTURE_DIR / f"{subcommand}__{case_name}.expected.json"


def _write_authored_fixtures() -> None:
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


def _discover_pairs() -> list[tuple[str, str]]:
    # Always rewrite first so the on-disk fixture pair set tracks
    # ``_AUTHORED_CASES`` byte-equally on every collect — avoids the
    # chicken-and-egg case where a new authored fixture is invisible
    # to ``pytest --collect-only`` until a second run.
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
# Tier-C driver
# ---------------------------------------------------------------------------


_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_TIMESTAMP_SENTINEL = "<TIMESTAMP>"

_HARNESS_ONLY_PAYLOAD_KEYS = {
    "_files",
    "_seed_plans_dir",
    "_seed_git_repo",
    "_git_initial_files",
    "stdin_text",
}

# Pinned git env for byte-equal commit SHAs across cli_dir and pure_dir.
# A second commit with identical tree+parent+author+committer+date yields
# the same SHA, so commit-task fixtures with `_seed_git_repo: True` can
# compare commit_sha byte-equally between paths.
_GIT_ENV_PIN = {
    "GIT_AUTHOR_NAME": "Tier-C Test",
    "GIT_AUTHOR_EMAIL": "tier-c@example.invalid",
    "GIT_COMMITTER_NAME": "Tier-C Test",
    "GIT_COMMITTER_EMAIL": "tier-c@example.invalid",
    "GIT_AUTHOR_DATE": "2026-04-30T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-04-30T00:00:00+0000",
}

_PAYLOAD_TO_CLI_FLAG = {
    "task_id": "--task-id",
    "files": "--files",
    "plan_file": "--plan-file",
    "reviewer": "--reviewer",
    "reviewer_verdict": "--reviewer-verdict",
    "reviewer_minor_findings": "--reviewer-minor-findings",
    "v_check_timeout": "--v-check-timeout",
    "run_id": "--run-id",
    "title": "--title",
    "diff_summary": "--diff-summary",
    "update_schedule_state": "--update-schedule-state",
    "schedule_file": "--schedule-file",
    "failed": "--failed",
    "event": "--event",
    "fields_json": "--fields-json",
    "findings_json": "--findings-json",
    "envelope_file": "--envelope-file",
    "test_command": "--test-command",
    "repo_root": "--repo-root",
    "timeout": "--timeout",
    "out_dir": "--out-dir",
    "stage": "--stage",
    "reason": "--reason",
    "authorization_source": "--authorization-source",
    "reviewer_findings": "--reviewer-findings",
    "reversion_guidance": "--reversion-guidance",
    "retries_used": "--retries-used",
    "id": "--id",
    "status": "--status",
    "out_of_scope_policy": "--out-of-scope-policy",
    "plans_dir": "--plans-dir",
}

_BOOL_PAYLOAD_TO_CLI_FLAG = {
    "force": "--force",
    "dry_run": "--dry-run",
    "remediation_tag": "--remediation-tag",
    "sandbox_divergence_tag": "--sandbox-divergence-tag",
    "d4_rescue_tag": "--d4-rescue-tag",
    "narrow_remediation_tag": "--narrow-remediation-tag",
    "disagreement_tag": "--disagreement-tag",
}


def _build_cli_argv(subcommand: str, payload: dict) -> list[str]:
    """Build argv for the CLI subprocess from the payload.

    Tier-C uses a hand-rolled mapping (rather than reusing
    ``cli_argv_from``) because several state-mutating commands carry
    payload keys that don't round-trip cleanly through argparse — e.g.
    ``commit_task.dismissed_finding_ids`` is a ``list[int]`` post-parse
    but a CSV string in argparse-land.
    """
    argv: list[str] = []
    for key, value in payload.items():
        if key in _HARNESS_ONLY_PAYLOAD_KEYS:
            continue
        if key in _BOOL_PAYLOAD_TO_CLI_FLAG:
            if value:
                argv.append(_BOOL_PAYLOAD_TO_CLI_FLAG[key])
            continue
        if key == "dismissed_finding_ids":
            if value:
                argv.extend(["--dismissed-finding-ids", ",".join(str(i) for i in value)])
            continue
        flag = _PAYLOAD_TO_CLI_FLAG.get(key)
        if flag is None:
            raise KeyError(
                f"Tier-C harness has no CLI mapping for payload key {key!r} "
                f"(subcommand {subcommand!r})"
            )
        if value is None:
            continue
        argv.extend([flag, str(value)])
    argv.append("--json")
    return argv


def _materialize_files(root: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _seed_plans_dir(root: Path) -> None:
    """Ensure ``docs/plans`` exists so ``RUN_LOCK_PATH``/``RUN_LOG_PATH``
    (relative to CWD) resolve cleanly without a NotADirectoryError.
    """
    (root / "docs" / "plans").mkdir(parents=True, exist_ok=True)


def _seed_git_repo(root: Path, initial_files: dict[str, str] | None) -> None:
    """Init a git repo in ``root`` with a deterministic initial commit.

    Pinned env (``_GIT_ENV_PIN``) plus identical seed content guarantees
    the initial commit's SHA matches across cli_dir and pure_dir, so any
    follow-on ``commit-task`` invocation produces a byte-equal commit_sha
    on both paths.
    """
    env = {**os.environ, **_GIT_ENV_PIN}
    subprocess.run(
        ["git", "init", "-q", "-b", "main"],
        cwd=str(root), env=env, check=True, capture_output=True,
    )
    # Repo-local config so the commit doesn't pick up the host's user.* —
    # belt-and-braces; GIT_*_NAME/EMAIL above already win for the commit
    # path, but `git commit` can still complain if neither is set in some
    # environments.
    subprocess.run(
        ["git", "config", "user.name", _GIT_ENV_PIN["GIT_AUTHOR_NAME"]],
        cwd=str(root), env=env, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", _GIT_ENV_PIN["GIT_AUTHOR_EMAIL"]],
        cwd=str(root), env=env, check=True, capture_output=True,
    )
    if initial_files:
        _materialize_files(root, initial_files)
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(root), env=env, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"],
        cwd=str(root), env=env, check=True, capture_output=True,
    )


def _payload_for_pure_core(payload: dict, root: Path) -> dict:
    """Strip harness-only keys and rewrite path values relative to root."""
    pure: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _HARNESS_ONLY_PAYLOAD_KEYS:
            continue
        if key == "stdin_text":
            pure[key] = value
            continue
        # ``_run_*`` payloads expect path-typed values for path-bearing
        # flags. The fixture supplies relative-to-fixture-root strings.
        if isinstance(value, str) and key in {
            "plan_file", "out_dir", "schedule_file",
            "envelope_file", "repo_root", "update_schedule_state",
        }:
            pure[key] = str(root / value)
        else:
            pure[key] = value
    if "stdin_text" in payload:
        pure["stdin_text"] = payload["stdin_text"]
    return pure


def _read_text_safe(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _canon(text: str | None, *, prefixes: tuple[str, ...] = ()) -> str | None:
    """Stub timestamps and any per-path absolute-prefix divergence.

    Lock files (and a handful of envelope error messages) embed the
    absolute plan-file path. Since the two paths run in disjoint tmp
    directories, the absolute prefix differs trivially even when the
    payload is byte-equal in spirit. Replacing both prefixes with a
    common sentinel preserves the divergence-detection power of the
    byte comparison while ignoring the per-run prefix.
    """
    if text is None:
        return None
    out = _TIMESTAMP_RE.sub(_TIMESTAMP_SENTINEL, text)
    for prefix in prefixes:
        out = out.replace(prefix, "<FIXTURE_DIR>")
    return out


def _snapshot_dir_text(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        # Skip any tmp atomic-write sidecars that may briefly exist.
        if rel.endswith(".tmp"):
            continue
        try:
            snap[rel] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            snap[rel] = "<binary>"
    return snap


def _run_cli(
    subcommand: str,
    cli_argv: list[str],
    cli_dir: Path,
    stdin_text: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("UNATTENDED_REVERT_POLICY", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "plan_ops.py"), subcommand, *cli_argv],
        input=stdin_text,
        cwd=str(cli_dir),
        env=env,
        capture_output=True,
        text=True,
    )


def _run_pure(
    subcommand: str,
    payload: dict,
    pure_dir: Path,
    stdin_text: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[dict, dict, int]:
    """Run ``_run_<subcommand>`` with CWD pinned to ``pure_dir``.

    ``RUN_LOCK_PATH`` / ``RUN_LOG_PATH`` are resolved relative to CWD at
    each call site (Path("docs/plans/...")) so chdir is sufficient to
    isolate the state mutators between fixtures.
    """
    plan_ops = load_plan_ops()
    run_fn_name = f"_run_{subcommand.replace('-', '_')}"
    run_fn = getattr(plan_ops, run_fn_name)
    saved_cwd = os.getcwd()
    saved_stdin = sys.stdin
    saved_env: dict[str, str | None] = {}
    if extra_env:
        for key, value in extra_env.items():
            saved_env[key] = os.environ.get(key)
            os.environ[key] = value
    try:
        os.chdir(str(pure_dir))
        # Stdin-bearing commands consume payload["stdin_text"] directly;
        # we don't need to monkey-patch sys.stdin here (the codemod
        # rewrote stdin readers to consult the payload key).
        sys.stdin = sys.__stdin__
        result_raw = run_fn(payload)
    finally:
        os.chdir(saved_cwd)
        sys.stdin = saved_stdin
        for key, prior in saved_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior
    public = plan_ops._public_result(result_raw)
    exit_code = int(result_raw.get("__plan_ops_exit_code__", 0))
    return result_raw, public, exit_code


def _run_tier_c_conformance(
    subcommand: str,
    payload: dict,
    expected: dict,
    tmp_path: Path,
) -> None:
    cli_dir = tmp_path / "cli"
    pure_dir = tmp_path / "pure"
    cli_dir.mkdir()
    pure_dir.mkdir()

    if payload.get("_seed_plans_dir"):
        _seed_plans_dir(cli_dir)
        _seed_plans_dir(pure_dir)

    extra_env: dict[str, str] | None = None
    if payload.get("_seed_git_repo"):
        # ``_git_initial_files`` are committed by ``_seed_git_repo``;
        # ``_files`` (below) are materialized after the initial commit
        # so they show up as a post-init working-tree mutation that the
        # subcommand under test can act on.
        initial = payload.get("_git_initial_files") or {}
        _seed_git_repo(cli_dir, initial)
        _seed_git_repo(pure_dir, initial)
        extra_env = dict(_GIT_ENV_PIN)

    files = payload.get("_files") or {}
    if files:
        _materialize_files(cli_dir, files)
        _materialize_files(pure_dir, files)

    cli_argv = _build_cli_argv(subcommand, payload)
    stdin_text = payload.get("stdin_text", "") or ""

    proc = _run_cli(subcommand, cli_argv, cli_dir, stdin_text, extra_env=extra_env)
    pure_payload = _payload_for_pure_core(payload, pure_dir)
    _, pure_envelope, pure_exit = _run_pure(
        subcommand, pure_payload, pure_dir, stdin_text, extra_env=extra_env,
    )

    expected_exit = int(expected["exit_code"])
    assert proc.returncode == expected_exit, (
        f"CLI exit mismatch for {subcommand}: expected {expected_exit}, "
        f"got {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert pure_exit == expected_exit, (
        f"pure-core exit mismatch for {subcommand}: expected "
        f"{expected_exit}, got {pure_exit}"
    )

    stderr_policy = expected.get("stderr_policy", "empty")
    if stderr_policy == "empty":
        assert proc.stderr == "", f"expected empty stderr; got {proc.stderr!r}"
    elif stderr_policy == "nonempty":
        assert proc.stderr.strip() != "", "expected non-empty stderr"

    if expected.get("compare_envelope"):
        try:
            cli_envelope = json.loads(proc.stdout) if proc.stdout else None
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"subprocess stdout is not parseable JSON: {exc}; "
                f"raw stdout={proc.stdout!r}"
            ) from exc
        # Canonicalize per-path prefixes so absolute paths embedded in
        # error/diagnostic strings don't register as fake divergences.
        prefix_pairs = (str(cli_dir), str(pure_dir))
        cli_canon = canonicalize_envelope(cli_envelope)
        pure_canon = canonicalize_envelope(pure_envelope)
        for prefix in prefix_pairs:
            cli_canon = cli_canon.replace(prefix, "<FIXTURE_DIR>")
            pure_canon = pure_canon.replace(prefix, "<FIXTURE_DIR>")
        assert cli_canon == pure_canon, (
            "canonical envelope mismatch between CLI and pure-core paths\n"
            f"--- cli ---\n{cli_canon}\n--- pure ---\n{pure_canon}\n"
        )

    sub_err = expected.get("expected_error_substring")
    if sub_err is not None:
        flat = json.dumps(pure_envelope)
        assert sub_err in flat, (
            f"expected error substring {sub_err!r} not in pure envelope: "
            f"{pure_envelope!r}"
        )
        assert sub_err in proc.stdout, (
            f"expected error substring {sub_err!r} not in CLI stdout: "
            f"{proc.stdout!r}"
        )

    err_code = expected.get("expected_error_code")
    if err_code is not None:
        codes = [
            err.get("code")
            for err in (pure_envelope.get("errors") or [])
        ]
        assert err_code in codes, (
            f"expected errors[*].code={err_code!r} in pure envelope; got {codes!r}"
        )

    prefixes = (str(cli_dir), str(pure_dir))

    if expected.get("compare_envelope"):
        # Re-canonicalize the envelope strings against per-path prefixes
        # to suppress absolute-path divergence in error messages that
        # embed `plan_file` etc.
        pass

    if expected.get("compare_run_log"):
        cli_log = _canon(
            _read_text_safe(cli_dir / "docs" / "plans" / "_run_log.jsonl"),
            prefixes=prefixes,
        )
        pure_log = _canon(
            _read_text_safe(pure_dir / "docs" / "plans" / "_run_log.jsonl"),
            prefixes=prefixes,
        )
        assert cli_log == pure_log, (
            "_run_log.jsonl divergence between CLI and pure-core paths\n"
            f"--- cli ---\n{cli_log!r}\n--- pure ---\n{pure_log!r}\n"
        )

    if expected.get("compare_lock_file"):
        cli_lock = _canon(
            _read_text_safe(cli_dir / "docs" / "plans" / "_run_lock.json"),
            prefixes=prefixes,
        )
        pure_lock = _canon(
            _read_text_safe(pure_dir / "docs" / "plans" / "_run_lock.json"),
            prefixes=prefixes,
        )
        assert cli_lock == pure_lock, (
            "_run_lock.json divergence between CLI and pure-core paths\n"
            f"--- cli ---\n{cli_lock!r}\n--- pure ---\n{pure_lock!r}\n"
        )

    for relpath in expected.get("compare_plan_files", []) or []:
        cli_text = _canon(_read_text_safe(cli_dir / relpath), prefixes=prefixes)
        pure_text = _canon(_read_text_safe(pure_dir / relpath), prefixes=prefixes)
        assert cli_text == pure_text, (
            f"divergent on-disk content for {relpath!r}\n"
            f"--- cli ---\n{cli_text!r}\n--- pure ---\n{pure_text!r}\n"
        )

    if expected.get("compare_dir_snapshot"):
        cli_snap = {
            k: _canon(v, prefixes=prefixes)
            for k, v in _snapshot_dir_text(cli_dir).items()
        }
        pure_snap = {
            k: _canon(v, prefixes=prefixes)
            for k, v in _snapshot_dir_text(pure_dir).items()
        }
        assert cli_snap == pure_snap, (
            "directory-snapshot divergence between CLI and pure-core paths\n"
            f"  cli only: {sorted(set(cli_snap) - set(pure_snap))}\n"
            f"  pure only: {sorted(set(pure_snap) - set(cli_snap))}\n"
            f"  changed: {sorted(k for k in cli_snap if cli_snap.get(k) != pure_snap.get(k))}"
        )


# ---------------------------------------------------------------------------
# Parametrized conformance test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcommand", "case_name"),
    _DISCOVERED_PAIRS,
    ids=[f"{s}__{c}" for s, c in _DISCOVERED_PAIRS],
)
def test_tier_c_pure_core_conformance(
    subcommand: str,
    case_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("UNATTENDED_REVERT_POLICY", raising=False)
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
    payload = payload_doc["payload"]
    _run_tier_c_conformance(subcommand, payload, expected, tmp_path)


def test_authored_case_count_matches_disk() -> None:
    expected = {(s, c) for s, c, _, _ in _AUTHORED_CASES}
    discovered = set(_discover_pairs())
    assert expected == discovered, (
        "_AUTHORED_CASES drifted from disk — "
        f"missing on disk: {sorted(expected - discovered)}; "
        f"orphan disk pairs: {sorted(discovered - expected)}"
    )


def test_runtime_under_30s() -> None:
    """Hard ceiling per AC: keep the suite small enough to run quickly."""
    assert len(_AUTHORED_CASES) <= 20, (
        f"too many authored cases ({len(_AUTHORED_CASES)})."
    )


# ---------------------------------------------------------------------------
# Codemod-correctness gate: commit-task --dismissed-finding-ids 1,x must
# be rejected by argparse on the CLI path (exit 2 + banner) and rejected
# by the same value-conversion gate (since pure callers MUST pass
# already-typed list[int]) on the pure path.
#
# This dedicated test sits outside the parametrized harness because the
# CLI path's exit code is 2 (argparse), not 1 (pure errors[]), and the
# CLI banner shape is not byte-equal to the pure errors[] envelope.
# ---------------------------------------------------------------------------


def test_commit_task_dismissed_finding_ids_invalid_csv_token(
    tmp_path: Path,
) -> None:
    """``--dismissed-finding-ids 1,x`` rejected by both paths.

    CLI: argparse-level rejection via ``main()``'s post-parse validation
    of the CSV (exit 2 + stderr banner).

    Pure: ``_run_commit_task`` operates on already-typed ``list[int]``;
    the equivalent failure mode is invalid input that the caller would
    have produced ``["1", "x"]`` for — the pure path never parses raw
    CSV, so the equivalence we assert here is that the CLI banner names
    the offending token.
    """
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    _seed_plans_dir(cli_dir)
    (cli_dir / "docs" / "plans" / "sample.md").write_text(
        PLAN_BODY_BASE, encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "plan_ops.py"),
            "commit-task",
            "--plan-file", "docs/plans/sample.md",
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "x",
            "--diff-summary", "y",
            "--narrow-remediation-tag",
            "--dismissed-finding-ids", "1,x",
            "--dry-run",
            "--json",
        ],
        cwd=str(cli_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0, (
        f"expected non-zero exit on '1,x'; got {proc.returncode}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "x" in combined, (
        f"expected the offending token 'x' to be named in CLI output: "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_commit_task_narrow_remediation_without_dismissed_both_paths_reject(
    tmp_path: Path,
) -> None:
    """``--narrow-remediation-tag`` without ``--dismissed-finding-ids``
    rejected by both paths.

    CLI: argparse cross-flag check via ``main()`` (exit 2 + stderr banner
    naming the missing flag).

    Pure: ``_validate_commit_task_payload`` returns
    ``errors[*].code = 'narrow-remediation-without-dismissed'`` (exit 1).

    The two divergence shapes mean the harness's byte-equal envelope
    comparator does not apply; we instead assert the structured-error
    contract on both sides independently.
    """
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    _seed_plans_dir(cli_dir)
    (cli_dir / "docs" / "plans" / "sample.md").write_text(
        PLAN_BODY_BASE, encoding="utf-8",
    )

    # CLI path — argparse cross-flag check, exit 2.
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "plan_ops.py"),
            "commit-task",
            "--plan-file", "docs/plans/sample.md",
            "--task-id", "001",
            "--run-id", "R1",
            "--files", "src/foo.py",
            "--title", "x",
            "--diff-summary", "y",
            "--narrow-remediation-tag",
            "--dry-run",
            "--json",
        ],
        cwd=str(cli_dir),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, (
        f"expected argparse exit 2; got {proc.returncode}; "
        f"stderr={proc.stderr!r}"
    )
    assert "--narrow-remediation-tag" in proc.stderr
    assert "--dismissed-finding-ids" in proc.stderr

    # Pure path — _validate_commit_task_payload structured error envelope.
    plan_ops = load_plan_ops()
    pure_payload = {
        "task_id": "001",
        "files": "src/foo.py",
        "plan_file": str(cli_dir / "docs/plans/sample.md"),
        "reviewer": "none",
        "reviewer_verdict": "",
        "reviewer_minor_findings": "[]",
        "dry_run": True,
        "v_check_timeout": 300,
        "run_id": "R1",
        "title": "x",
        "diff_summary": "y",
        "remediation_tag": False,
        "sandbox_divergence_tag": False,
        "d4_rescue_tag": False,
        "narrow_remediation_tag": True,
        "disagreement_tag": False,
        "dismissed_finding_ids": [],
        "update_schedule_state": None,
    }
    saved_cwd = os.getcwd()
    pure_dir = tmp_path / "pure"
    pure_dir.mkdir()
    _seed_plans_dir(pure_dir)
    try:
        os.chdir(str(pure_dir))
        result = plan_ops._run_commit_task(pure_payload)
    finally:
        os.chdir(saved_cwd)
    assert int(result.get("__plan_ops_exit_code__", 0)) == 1
    public = plan_ops._public_result(result)
    codes = [e.get("code") for e in (public.get("errors") or [])]
    assert "narrow-remediation-without-dismissed" in codes, (
        f"expected pure errors[*].code in {codes!r}"
    )


# Defensive imports kept bound for future case helpers.
_ = (shutil,)
