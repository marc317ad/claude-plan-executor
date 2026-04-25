"""TASK-006 / TASK-008 tests: schedule-only plan-review contract.

Covers the acceptance criteria added by TASK-006 and finalized by
TASK-008 (deprecation shim removal):

  1. ``plan-review`` dispatched against the fixture's fat manifest schedule
     (produced by TASK-004 `build-tasks`) returns a reviewer verdict of
     ``approved`` / ``approved-with-notes`` with no ``critical`` findings.
  2. ``plan-review`` dispatched against a schedule whose fat-manifest
     ``description`` is deliberately blank for one task surfaces a finding
     pointing at ``tasks[<id>].description`` with a non-empty
     ``suggested_change``.
  3. The invoking argv never carries ``--plan-file``; the internal
     ``render_plan_review_prompt`` helper accepts the new signature with
     no ``plan_text`` / ``plan_basename`` / ``plan_path`` actuals and
     does not raise ``TypeError``.
  4. TASK-008: argparse no longer recognizes ``--plan-file`` or
     ``--plans-dir`` on ``plan-review``; render helper no longer accepts
     the legacy ``plan_text`` / ``plan_abs_path`` / ``plans_dir`` kwargs.

These tests monkeypatch ``invoke_codex`` to avoid the real Codex CLI
(mirrors the pattern in ``test_plan_codex_dispatch_state_isolation.py``).
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_codex_dispatch.py"
)
DIRECTORY_MODE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "directory_mode_plan"
)
# Shipped fat-manifest schedule sidecar for the directory_mode_plan fixture.
# Lives at the documented in-directory convention path
# (``<plan_dir>/<plan_dir>.schedule.json``, see ``_plan_paths.py:69``) so
# the wrapper's plan_basename derivation from the sidecar's parent folder
# round-trips back to ``directory_mode_plan``. Committed to git; tests that
# mutate the schedule must copy it into ``tmp_path`` first to avoid
# touching the committed artifact.
FIXTURE_SCHEDULE = DIRECTORY_MODE_FIXTURE / "directory_mode_plan.schedule.json"


def _load_wrapper():
    spec = importlib.util.spec_from_file_location(
        "plan_codex_dispatch", WRAPPER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Schedule helpers — dispatch against the shipped fat-manifest schedule
# sidecar. Read-only tests point directly at the committed fixture path;
# mutation tests copy the fixture directory (including the sidecar) into
# tmp_path first so the committed artifact is never touched.
# ---------------------------------------------------------------------------


def _copy_fixture_into(plan_dir: Path) -> None:
    """Copy the shipped fixture (including the sidecar) into ``plan_dir``
    so tests can mutate a per-test copy without touching the committed
    fixture. The sidecar file is copied alongside the chunk markdown so the
    copy retains the in-directory sidecar convention
    (``<plan_dir>/<plan_dir>.schedule.json``)."""
    plan_dir.mkdir(parents=True, exist_ok=True)
    for src in DIRECTORY_MODE_FIXTURE.iterdir():
        dst = plan_dir / src.name
        dst.write_bytes(src.read_bytes())


def _fixture_sidecar_in(plan_dir: Path) -> Path:
    """Return the expected sidecar path for a tmp plan_dir copy. The
    sidecar filename follows the committed fixture's own basename so the
    wrapper's plan_basename-derivation assertions in the tests keep
    targeting ``directory_mode_plan``."""
    return plan_dir / FIXTURE_SCHEDULE.name


# ---------------------------------------------------------------------------
# Fake invoke_codex — returns a canned envelope body into ``output_path``.
# The wrapper's cmd_plan_review path reads the file back, decodes it, and
# puts the body under envelope.parsed, so the fake simulates Codex's
# behaviour without any live CLI.
# ---------------------------------------------------------------------------


def _fake_codex_returning(parsed_body: dict):
    """Return a fake invoke_codex that writes `parsed_body` as the Codex
    structured-output JSON file. Mirrors the real dispatcher's
    `--output-schema + -o` contract."""

    captured: dict = {"prompt": None}

    def fake(prompt, workdir, schema_path, output_path, timeout_sec,
             sandbox=None):
        captured["prompt"] = prompt
        Path(output_path).write_text(
            json.dumps(parsed_body), encoding="utf-8",
        )
        return {
            "status": "ok",
            "exit_code": 0,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    fake.captured = captured  # type: ignore[attr-defined]
    return fake


def _plan_review_args(
    schedule_file: Path,
    repo_root: Path,
    *,
    allow_gaps: bool = False,
) -> argparse.Namespace:
    """Build a Namespace matching the `plan-review` argparse contract
    (schedule-only; TASK-006, finalized in TASK-008). The retired
    ``plan_file`` / ``plans_dir`` kwargs are no longer accepted."""
    return argparse.Namespace(
        schedule_file=str(schedule_file),
        repo_root=str(repo_root),
        json=True,
        dry_run=False,
        timeout=180,
        allow_gaps=allow_gaps,
    )


def _review_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _git_stdout(repo: Path, args: list[str]) -> str:
    cp = subprocess.run(
        ["git"] + args,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout


def _git_index_snapshot(repo: Path) -> tuple[str, str]:
    return (
        _git_stdout(repo, ["status", "--porcelain=v1"]),
        _git_stdout(repo, ["ls-files", "--others", "--exclude-standard"]),
    )


def _review_plan(path: Path, file_path: str) -> None:
    path.write_text(
        "\n".join([
            "### TASK-005: Review untracked file",
            "- **Status:** pending",
            "- **Priority:** P1",
            "- **Files:**",
            f"  - {file_path}",
            "",
            "**Description:**",
            "Review the changed file.",
            "",
        ]),
        encoding="utf-8",
    )


def _cmd_review_args(
    plan_file: Path,
    repo: Path,
    *,
    files: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_file=str(plan_file),
        repo_root=str(repo),
        task_id="005",
        files=files,
        review_focus="",
        dry_run=True,
        timeout=180,
    )


# ---------------------------------------------------------------------------
# TASK-005: review diffs include untracked files via intent-to-add.
# ---------------------------------------------------------------------------


def test_git_diff_for_files_default_omits_untracked_file(tmp_path):
    sig = inspect.signature(wrapper.git_diff_for_files)
    include = sig.parameters["include_untracked"]
    assert include.kind is inspect.Parameter.KEYWORD_ONLY
    assert include.default is False

    repo = _review_git_repo(tmp_path)
    (repo / "new.txt").write_text("new content\n", encoding="utf-8")

    diff = wrapper.git_diff_for_files(str(repo), ["new.txt"])

    assert diff == ""


def test_git_diff_for_files_include_untracked_preserves_index(tmp_path):
    repo = _review_git_repo(tmp_path)
    (repo / "new.txt").write_text("new content\n", encoding="utf-8")
    before = _git_index_snapshot(repo)

    diff = wrapper.git_diff_for_files(
        str(repo), ["new.txt"], include_untracked=True,
    )
    after = _git_index_snapshot(repo)

    assert "diff --git a/new.txt b/new.txt" in diff
    assert "+new content" in diff
    assert after == before


def test_git_diff_for_files_empty_file_list_returns_empty_without_git(
    monkeypatch,
):
    calls = []

    def fake_git(args, cwd, timeout=wrapper.GIT_TIMEOUT):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(wrapper, "_git", fake_git)

    assert wrapper.git_diff_for_files(
        "/unused", [], include_untracked=True,
    ) == ""
    assert calls == []


def test_git_diff_for_files_add_failure_does_not_raise_and_resets(
    monkeypatch,
):
    calls = []

    def fake_git(args, cwd, timeout=wrapper.GIT_TIMEOUT):
        calls.append(args)
        if args[:2] == ["add", "-N"]:
            return subprocess.CompletedProcess(args, 1, "", "missing\n")
        if args[:2] == ["diff", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "diff body\n", "")
        if args[:2] == ["reset", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(wrapper, "_git", fake_git)

    diff = wrapper.git_diff_for_files(
        "/repo", ["missing.txt"], include_untracked=True,
    )

    assert diff == "diff body\n"
    assert calls == [
        ["add", "-N", "--", "missing.txt"],
        ["diff", "HEAD", "--", "missing.txt"],
        ["reset", "HEAD", "--", "missing.txt"],
    ]


def test_git_diff_for_files_resets_when_diff_raises(monkeypatch):
    import pytest as _pytest

    calls = []

    def fake_git(args, cwd, timeout=wrapper.GIT_TIMEOUT):
        calls.append(args)
        if args[:2] == ["add", "-N"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["diff", "HEAD"]:
            raise RuntimeError("diff failed")
        if args[:2] == ["reset", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(wrapper, "_git", fake_git)

    with _pytest.raises(RuntimeError):
        wrapper.git_diff_for_files(
            "/repo", ["new.txt"], include_untracked=True,
        )

    assert calls == [
        ["add", "-N", "--", "new.txt"],
        ["diff", "HEAD", "--", "new.txt"],
        ["reset", "HEAD", "--", "new.txt"],
    ]


def test_cmd_review_dry_run_prompt_includes_untracked_file_diff(
    tmp_path, capsys,
):
    repo = _review_git_repo(tmp_path)
    (repo / "new.txt").write_text("new content\n", encoding="utf-8")
    before = _git_index_snapshot(repo)
    plan = tmp_path / "plan.md"
    _review_plan(plan, "new.txt")

    rc = wrapper.cmd_review(_cmd_review_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    after = _git_index_snapshot(repo)

    assert rc == 0, envelope
    assert envelope["outcome"] == "dry_run"
    assert envelope["diff_size_bytes"] > 0
    assert "diff --git a/new.txt b/new.txt" in envelope["prompt_preview"]
    assert "+new content" in envelope["prompt_preview"]
    assert after == before


# ---------------------------------------------------------------------------
# 1. Fixture schedule → approved verdict, no critical findings.
# ---------------------------------------------------------------------------


def test_plan_review_fixture_schedule_approved(tmp_path, monkeypatch, capsys):
    """Acceptance: dispatch against the shipped fat-manifest schedule
    (``tests/fixtures/directory_mode_plan/directory_mode_plan.schedule.json``)
    and assert the envelope carries `verdict in {approved, approved-with-notes}`
    with no critical findings. Codex is mocked; no live CLI. The shipped
    sidecar is read-only for this test — we point ``--schedule-file`` at
    the committed path directly."""
    sidecar = FIXTURE_SCHEDULE
    assert sidecar.is_file(), (
        f"fixture sidecar missing at {sidecar}; TASK-006 ships this file"
    )

    # Canned envelope body: reviewer approves the fixture schedule outright.
    canned = {
        "plan_file": DIRECTORY_MODE_FIXTURE.name,
        "verdict": "approved",
        "findings": [],
        "notes": [],
        "schedule_ok": True,
        "summary": "Fat manifest schedule parses cleanly; DAG + file-lock "
                   "batches are consistent; every task carries description + "
                   "acceptance_criteria.",
    }
    fake = _fake_codex_returning(canned)
    monkeypatch.setattr(wrapper, "invoke_codex", fake)

    args = _plan_review_args(sidecar, tmp_path)
    rc = wrapper.cmd_plan_review(args)
    out = capsys.readouterr().out
    envelope = json.loads(out)

    assert rc == 0, envelope
    assert envelope["outcome"] == "success", envelope
    parsed = envelope["parsed"]
    assert parsed["verdict"] in {"approved", "approved-with-notes"}, parsed
    # No critical findings allowed (AC6 contract).
    critical = [
        f for f in parsed.get("findings", [])
        if f.get("severity") == "critical"
    ]
    assert critical == [], (
        f"fixture schedule should not elicit critical findings, got: "
        f"{critical}"
    )
    # envelope.plan_file identifies the plan directory (directory-mode
    # sidecar convention puts the sidecar inside the plan dir).
    assert envelope["plan_file"] == DIRECTORY_MODE_FIXTURE.name, envelope


# ---------------------------------------------------------------------------
# 2. Schedule with a missing description → reviewer surfaces a finding
#    whose `section` points at that task and `suggested_change` is non-empty.
# ---------------------------------------------------------------------------


def test_plan_review_schedule_with_missing_description_surfaces_finding(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance: when a task's `description` is deliberately blank, the
    reviewer surfaces a finding whose `section` points at that task and
    `suggested_change` is non-empty. The finding section should follow the
    new `tasks[i].description` path convention added to the prompt.

    Copies the shipped fixture (including the committed sidecar) into
    ``tmp_path`` so the mutation never touches the on-disk artifact."""
    plan_dir = tmp_path / DIRECTORY_MODE_FIXTURE.name
    _copy_fixture_into(plan_dir)
    sidecar = _fixture_sidecar_in(plan_dir)
    assert sidecar.is_file(), (
        f"copied sidecar missing at {sidecar}; the shipped fixture must "
        f"include {FIXTURE_SCHEDULE.name}"
    )

    # Mutate the persisted schedule: blank description on TASK-002.
    schedule_doc = json.loads(sidecar.read_text(encoding="utf-8"))
    target_id = None
    for task in schedule_doc.get("tasks", []):
        if task.get("id") == "002":
            task["description"] = ""
            target_id = task["id"]
            break
    assert target_id == "002", schedule_doc
    sidecar.write_text(
        json.dumps(schedule_doc, indent=2), encoding="utf-8",
    )

    # Canned envelope: reviewer flags the missing-description task.
    canned = {
        "plan_file": plan_dir.name,
        "verdict": "needs-replan",
        "findings": [
            {
                "severity": "important",
                "blocking": True,
                "section": f"tasks[{target_id}].description",
                "concern": (
                    "Task description is empty; implementer has no "
                    "statement of intent to work against."
                ),
                "suggested_change": (
                    "Populate tasks[002].description with a 2-3 sentence "
                    "statement of what TASK-002 is supposed to accomplish "
                    "and why."
                ),
            }
        ],
        "notes": [],
        "schedule_ok": False,
        "summary": (
            "Schedule is well-shaped structurally but TASK-002 has a blank "
            "description; implementer cannot work without intent."
        ),
    }
    fake = _fake_codex_returning(canned)
    monkeypatch.setattr(wrapper, "invoke_codex", fake)

    args = _plan_review_args(sidecar, tmp_path)
    rc = wrapper.cmd_plan_review(args)
    out = capsys.readouterr().out
    envelope = json.loads(out)

    assert rc == 0, envelope
    parsed = envelope["parsed"]
    findings = parsed.get("findings", [])
    assert findings, "expected at least one finding"
    target_finding = next(
        (
            f for f in findings
            if f.get("section", "").startswith(f"tasks[{target_id}]")
        ),
        None,
    )
    assert target_finding is not None, (
        f"no finding points at tasks[{target_id}]; got sections: "
        f"{[f.get('section') for f in findings]}"
    )
    assert target_finding.get("section") == (
        f"tasks[{target_id}].description"
    ), target_finding
    assert target_finding.get("suggested_change", "").strip(), (
        "suggested_change must be non-empty"
    )


# ---------------------------------------------------------------------------
# 3. Argv + helper-signature invariants — no --plan-file; no TypeError
#    when the render helper is called without plan_text/plan_basename/
#    plan_path actuals.
# ---------------------------------------------------------------------------


def test_plan_review_argv_no_plan_file_and_render_prompt_no_plan_text(
    tmp_path, monkeypatch, capsys,
):
    """Acceptance: the argv that the orchestrator constructs for
    `plan-review` dispatch MUST NOT carry `--plan-file`. Additionally, the
    internal `render_plan_review_prompt` helper accepts the new signature
    with no `plan_text` / `plan_basename` / `plan_path` actuals without
    raising `TypeError`."""
    # Part A — argv composition (constructed the same way the orchestrator
    # would assemble it): schedule-only, no --plan-file.
    schedule = tmp_path / "some_plan.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "gaps": [],
        }),
        encoding="utf-8",
    )
    argv_under_test = [
        "plan-review",
        "--schedule-file", str(schedule),
        "--repo-root", str(tmp_path),
    ]
    # Explicit positive and negative assertions on the argv shape.
    assert "--plan-file" not in argv_under_test, (
        "argv must not carry --plan-file (TASK-006 schedule-only contract)"
    )
    assert "--schedule-file" in argv_under_test
    assert "--repo-root" in argv_under_test

    # The wrapper's argparse must accept this argv — no required-argument
    # error on the missing --plan-file / --plans-dir.
    parser = wrapper._build_parser()
    args = parser.parse_args(argv_under_test)
    assert args.subcommand == "plan-review"
    assert args.schedule_file == str(schedule)
    assert getattr(args, "plan_file", None) is None
    assert getattr(args, "plans_dir", None) is None

    # Part B — render helper signature: calling with only schedule_json
    # (positional), and optionally plan_basename, must succeed.
    # Failing this would be the TypeError the AC warns against.
    schedule_json = schedule.read_text(encoding="utf-8")
    # No plan_text / plan_basename / plan_path actuals — the helper must
    # accept this call shape without raising.
    prompt_only_schedule = wrapper.render_plan_review_prompt(schedule_json)
    assert isinstance(prompt_only_schedule, str)
    assert "Persisted schedule JSON:" in prompt_only_schedule
    # Plan-markdown block must NOT appear in the rendered prompt (the
    # schedule-only contract). Historical pre-TASK-006 prompts carried a
    # "Plan document (verbatim):" section.
    assert "Plan document (verbatim):" not in prompt_only_schedule, (
        "schedule-only prompt must not carry the plan-markdown block"
    )

    # Signature introspection: the required positional is schedule_json;
    # the plan-markdown actuals are optional (kwargs with None defaults).
    sig = inspect.signature(wrapper.render_plan_review_prompt)
    params = sig.parameters
    assert "schedule_json" in params, params
    # Per AC, any legacy plan_* params must default to None (or be absent).
    for legacy in ("plan_text", "plan_basename", "plan_path", "plan_abs_path"):
        if legacy in params:
            p = params[legacy]
            assert p.default is None, (
                f"legacy param {legacy!r} must default to None "
                f"(got {p.default!r})"
            )

    # Part C — dispatch the wrapper with the argv-under-test and assert the
    # prompt the fake Codex received does NOT embed any plan-markdown block
    # and that envelope.plan_file round-trips from the sidecar stem.
    canned = {
        "plan_file": schedule.stem.replace(".schedule", ""),
        "verdict": "approved",
        "findings": [],
        "notes": [],
        "schedule_ok": True,
        "summary": "empty fixture",
    }
    fake = _fake_codex_returning(canned)
    monkeypatch.setattr(wrapper, "invoke_codex", fake)

    rc = wrapper.cmd_plan_review(args)
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["outcome"] == "success", envelope
    # The fake recorded the prompt: it must not carry the legacy
    # plan-markdown block.
    assert fake.captured["prompt"] is not None
    assert "Plan document (verbatim):" not in fake.captured["prompt"], (
        "cmd_plan_review should render schedule-only prompt"
    )
    # The prompt must still carry the schedule JSON block and the task
    # section-reference guidance (new to TASK-006).
    assert "Persisted schedule JSON:" in fake.captured["prompt"]
    assert "tasks[i]" in fake.captured["prompt"], (
        "prompt should instruct reviewer to use tasks[i] section paths"
    )


# ---------------------------------------------------------------------------
# 4. TASK-008 — --plan-file / --plans-dir removed from argparse; the legacy
#    positional / kwarg signature of render_plan_review_prompt is gone too.
#    The transition tests (pre-TASK-008) are retired.
# ---------------------------------------------------------------------------


def test_plan_review_argv_rejects_plan_file_flag():
    """TASK-008: `--plan-file` is no longer a recognized argparse flag on
    the `plan-review` subcommand. argparse must reject any argv that tries
    to pass it, making accidental usage by stale callers a loud failure
    rather than a silent no-op.
    """
    import pytest as _pytest  # local import: keep top of module lean

    parser = wrapper._build_parser()
    argv = [
        "plan-review",
        "--plan-file", "/tmp/stale.md",  # retired flag
        "--schedule-file", "/tmp/x.json",
        "--repo-root", "/tmp",
    ]
    with _pytest.raises(SystemExit):
        parser.parse_args(argv)


def test_render_plan_review_prompt_rejects_legacy_kwargs():
    """TASK-008: the render helper no longer accepts the legacy `plan_text`
    / `plan_abs_path` / `plans_dir` keyword arguments. Stale callers get a
    `TypeError` rather than a silent no-op, making migration obvious."""
    import pytest as _pytest

    schedule_json = json.dumps({
        "outcome": "valid",
        "tasks": [],
        "batches": [],
        "gaps": [],
    })
    for legacy_kwarg in ("plan_text", "plan_abs_path", "plans_dir"):
        with _pytest.raises(TypeError):
            wrapper.render_plan_review_prompt(
                schedule_json, **{legacy_kwarg: "stale"},
            )


# ---------------------------------------------------------------------------
# TASK-004: file-count-aware timeout scaling helpers + envelope plumbing.
# ---------------------------------------------------------------------------


import pytest  # noqa: E402  (intentional late import — keeps top of module lean)


@pytest.mark.parametrize(
    "num_files, expected",
    [
        (0, 300),
        (1, 300),
        (2, 300),
        (5, 300),
        (6, 360),
        (7, 420),
        (10, 600),
    ],
)
def test_compute_implement_timeout_floor(num_files, expected):
    """``compute_implement_timeout(N)`` returns ``max(300, 60*N)``.
    Floor pins single- and small-file tasks at 300 s; per-file growth
    kicks in at N >= 6."""
    assert wrapper.compute_implement_timeout(num_files) == expected


@pytest.mark.parametrize(
    "num_files, expected",
    [
        (0, 180),
        (1, 180),
        (2, 180),
        (6, 180),
        (7, 210),
        (8, 240),
        (10, 300),
    ],
)
def test_compute_review_timeout_floor(num_files, expected):
    """``compute_review_timeout(N)`` returns ``max(180, 30*N)``. The
    friction run's 8-file diff hit 180 s mid-verification; the new
    scaling yields 240 s."""
    assert wrapper.compute_review_timeout(num_files) == expected


def test_compute_timeouts_clamp_negative_inputs():
    """Defensive: a negative/garbage ``num_files`` should clamp to 0
    and return the floor rather than yielding a sub-floor timeout."""
    assert wrapper.compute_implement_timeout(-3) == 300
    assert wrapper.compute_review_timeout(-3) == 180


def test_add_common_timeout_default_is_none():
    """``--timeout`` on implement/review parsers defaults to ``None``;
    the cmd handler derives the effective default from the file count.
    The plan-review subparser also defaults to ``None`` (resolved to the
    flat ``DEFAULT_TIMEOUT_PLAN_REVIEW`` inside ``cmd_plan_review``)."""
    parser = wrapper._build_parser()

    impl_args = parser.parse_args([
        "implement",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
    ])
    assert impl_args.timeout is None, (
        f"--timeout default must be None for cmd_implement to derive "
        f"file-count-aware default; got {impl_args.timeout!r}"
    )

    rev_args = parser.parse_args([
        "review",
        "--plan-file", "/tmp/p.md",
        "--task-id", "001",
        "--repo-root", "/tmp",
    ])
    assert rev_args.timeout is None, rev_args.timeout

    pr_args = parser.parse_args([
        "plan-review",
        "--schedule-file", "/tmp/s.json",
        "--repo-root", "/tmp",
    ])
    assert pr_args.timeout is None, pr_args.timeout


def _impl_plan(plan: Path, task_id: str, files: list[str]) -> None:
    """Build a minimal plan markdown for cmd_implement to parse. The
    plan only needs to satisfy ``parse_task_block`` — the test fakes
    Codex so the fields beyond Files / Test command don't influence
    the timeout-resolution path under test."""
    bullets = "\n".join(f"  - {f}" for f in files)
    plan.write_text(
        "\n".join([
            "# Plan",
            "",
            "## Context",
            "",
            "(unused).",
            "",
            "## Tasks",
            "",
            f"### TASK-{task_id}: scaling test",
            "",
            "- **Status:** pending",
            "- **Priority:** medium",
            "- **Files:**",
            bullets,
            "- **Test command:** `none`",
            "- **Acceptance criteria:**",
            "  - It works.",
            "",
            "**Description:**",
            "Test fixture.",
            "",
        ]),
        encoding="utf-8",
    )


def _impl_args(
    plan: Path,
    repo: Path,
    task_id: str = "001",
    *,
    timeout=None,
    dry_run: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        plan_file=str(plan),
        task_id=task_id,
        repo_root=str(repo),
        json=True,
        dry_run=dry_run,
        timeout=timeout,
    )


def _impl_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def test_implement_envelope_carries_effective_timeout_and_baseline_error(
    tmp_path, monkeypatch, capsys,
):
    """On a Codex timeout, the implement envelope MUST carry both:
      * ``effective_timeout`` — the file-count-aware cap actually used.
      * ``baseline_error`` — the truncated ``_snapshot_baseline`` error
        message when baseline capture failed (synthetic git boom here).
    """
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # 7 files → compute_implement_timeout(7) == 420.
    seven_files = [f"file_{i}.py" for i in range(7)]
    _impl_plan(plan, "001", seven_files)

    # Synthetic baseline failure: forces _snapshot_baseline to capture
    # the underlying error message and forward it through the envelope.
    boom_msg = "git broken: fake snapshot failure"

    def boom(repo_root):
        raise subprocess.SubprocessError(boom_msg)

    monkeypatch.setattr(wrapper, "git_changed_files", boom)

    # Fake Codex returns timeout immediately; nothing else needs to be
    # exercised — the envelope shape under test is the timeout branch.
    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.5,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)

    assert rc == 1, envelope
    assert envelope["outcome"] == "timeout", envelope
    # effective_timeout matches compute_implement_timeout(len(files)).
    assert envelope["effective_timeout"] == wrapper.compute_implement_timeout(
        len(seven_files)
    ), envelope
    assert envelope["effective_timeout"] == 420, envelope
    # baseline_error carries the truncated message; baseline_captured False.
    assert envelope["baseline_captured"] is False, envelope
    assert envelope["baseline_error"] == boom_msg, envelope
    # Truncation cap: 200 chars max.
    assert envelope["baseline_error"] is not None
    assert len(envelope["baseline_error"]) <= 200


def test_implement_envelope_baseline_error_truncated_to_200_chars(
    tmp_path, monkeypatch, capsys,
):
    """``baseline_error`` is truncated at 200 chars to keep envelopes
    bounded even on pathological git error messages."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _impl_plan(plan, "001", ["a.py"])

    long_msg = "X" * 500

    def boom(repo_root):
        raise subprocess.SubprocessError(long_msg)

    monkeypatch.setattr(wrapper, "git_changed_files", boom)

    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["outcome"] == "timeout", envelope
    assert envelope["baseline_error"] is not None
    assert len(envelope["baseline_error"]) == 200, len(envelope["baseline_error"])


def test_implement_envelope_baseline_error_null_when_capture_succeeds(
    tmp_path, monkeypatch, capsys,
):
    """When ``_snapshot_baseline`` succeeds, ``baseline_error`` is
    ``None`` — the field must still be present in the timeout envelope."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _impl_plan(plan, "001", ["a.py"])

    # Baseline capture succeeds (real git_changed_files runs against a
    # fresh repo). Codex still times out.
    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["outcome"] == "timeout", envelope
    assert envelope["baseline_captured"] is True, envelope
    assert envelope["baseline_error"] is None, envelope
    # effective_timeout still recorded.
    assert "effective_timeout" in envelope, envelope


def test_explicit_timeout_override_skips_scaling(
    tmp_path, monkeypatch, capsys,
):
    """When the operator passes ``--timeout N``, the scaling is bypassed:
    ``effective_timeout`` MUST be exactly N regardless of len(files), and
    the resolved value MUST be the timeout actually passed to
    ``invoke_codex``."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    # 10 files would scale to 600 s under the default formula; the
    # operator override of 100 s must win.
    ten_files = [f"file_{i}.py" for i in range(10)]
    _impl_plan(plan, "001", ten_files)

    seen = {"timeout_sec": None}

    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        seen["timeout_sec"] = timeout_sec
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    rc = wrapper.cmd_implement(
        _impl_args(plan, repo, timeout=100),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    assert envelope["effective_timeout"] == 100, envelope
    # The wrapper-derived default for 10 files would be 600 — assert
    # the override is genuinely shorter so we know scaling was skipped.
    assert envelope["effective_timeout"] != wrapper.compute_implement_timeout(10)
    # And the same value flowed into the subprocess timeout.
    assert seen["timeout_sec"] == 100


def test_implement_default_timeout_derives_from_file_count(
    tmp_path, monkeypatch, capsys,
):
    """When ``--timeout`` is not passed (``args.timeout is None``),
    ``cmd_implement`` resolves the effective timeout from
    ``compute_implement_timeout(len(task['files']))`` and forwards that
    same value into ``invoke_codex``."""
    repo = _impl_repo(tmp_path)
    plan = tmp_path / "plan.md"
    eight_files = [f"file_{i}.py" for i in range(8)]
    _impl_plan(plan, "001", eight_files)

    seen = {"timeout_sec": None}

    def fake_codex(prompt, workdir, schema_path, output_path, timeout_sec,
                  sandbox=None):
        seen["timeout_sec"] = timeout_sec
        return {
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": "",
            "file_changes": [],
            "wall_seconds": 0.1,
        }

    monkeypatch.setattr(wrapper, "invoke_codex", fake_codex)

    # No timeout override → wrapper-derived default.
    rc = wrapper.cmd_implement(_impl_args(plan, repo))
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 1, envelope
    expected = wrapper.compute_implement_timeout(8)
    assert expected == 480, expected  # sanity: 60 * 8 = 480 > 300 floor.
    assert envelope["effective_timeout"] == expected, envelope
    assert seen["timeout_sec"] == expected


def _review_args_for_timeout(
    plan: Path,
    repo: Path,
    *,
    timeout=None,
    files: str = "",
    dry_run: bool = True,
) -> argparse.Namespace:
    """Args helper for cmd_review timeout-scaling tests. Defaults to
    ``dry_run=True`` so the test never hits ``invoke_codex``."""
    return argparse.Namespace(
        plan_file=str(plan),
        repo_root=str(repo),
        task_id="005",
        files=files,
        review_focus="bugs",
        dry_run=dry_run,
        timeout=timeout,
    )


def test_review_default_timeout_derives_from_files_count(
    tmp_path, capsys,
):
    """``cmd_review`` derives ``effective_timeout`` from
    ``len(review_files)`` when ``--timeout`` is unset. The dry-run
    envelope surfaces the resolved value so the operator can validate
    the scaling without dispatching Codex."""
    repo = _review_git_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _review_plan(plan, "new.txt")
    # Pass --files with 7 entries → compute_review_timeout(7) == 210.
    seven = ",".join(f"f{i}.py" for i in range(7))

    rc = wrapper.cmd_review(
        _review_args_for_timeout(plan, repo, files=seven, dry_run=True),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    assert envelope["outcome"] == "dry_run", envelope
    assert envelope["effective_timeout"] == 210, envelope


def test_review_explicit_timeout_override_wins(tmp_path, capsys):
    """Operator override of ``--timeout 50`` short-circuits the
    file-count scaling for ``cmd_review`` too."""
    repo = _review_git_repo(tmp_path)
    plan = tmp_path / "plan.md"
    _review_plan(plan, "new.txt")
    seven = ",".join(f"f{i}.py" for i in range(7))

    rc = wrapper.cmd_review(
        _review_args_for_timeout(
            plan, repo, files=seven, timeout=50, dry_run=True,
        ),
    )
    envelope = json.loads(capsys.readouterr().out)
    assert rc == 0, envelope
    assert envelope["effective_timeout"] == 50, envelope


def test_plan_review_dry_run_envelope_carries_effective_timeout(
    tmp_path, capsys,
):
    """``cmd_plan_review`` surfaces ``effective_timeout`` even though
    plan-review keeps a flat default — the run-log records what cap
    actually applied. Default is ``DEFAULT_TIMEOUT_PLAN_REVIEW``;
    operator override still wins."""
    schedule = tmp_path / "x.schedule.json"
    schedule.write_text(
        json.dumps({
            "outcome": "valid",
            "tasks": [],
            "batches": [],
            "gaps": [],
        }),
        encoding="utf-8",
    )

    # No --timeout override → flat default.
    args_default = argparse.Namespace(
        schedule_file=str(schedule),
        repo_root=str(tmp_path),
        json=True,
        dry_run=True,
        timeout=None,
        allow_gaps=False,
    )
    rc = wrapper.cmd_plan_review(args_default)
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["effective_timeout"] == wrapper.DEFAULT_TIMEOUT_PLAN_REVIEW
    assert envelope["effective_timeout"] == 180

    # Explicit --timeout override still wins.
    args_override = argparse.Namespace(
        schedule_file=str(schedule),
        repo_root=str(tmp_path),
        json=True,
        dry_run=True,
        timeout=42,
        allow_gaps=False,
    )
    rc = wrapper.cmd_plan_review(args_override)
    out = capsys.readouterr().out
    envelope = json.loads(out)
    assert rc == 0, envelope
    assert envelope["effective_timeout"] == 42, envelope
