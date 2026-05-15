"""TASK-009 (per_task_dispatch_refactor_v2): end-to-end directory-mode smoke.

Locks in the directory-only `/implement-plan` flow against the shipped
`tests/fixtures/directory_mode_plan/` fixture and any single-file → directory
auto-promotion path. The harness is pure: no git writes, no network, no live
Codex invocation. The Codex wrapper boundary is mocked at
`plan_codex_dispatch.invoke_codex` so the schedule-only plan-review path
exercises argv composition + envelope shape without dispatching to Codex.

The /implement-plan slash command itself is prose-driven (SKILL.md), so
"end-to-end" here means: walk the same chain of `plan_ops.py` /
`plan_codex_dispatch.py` calls the orchestrator would emit, in the same order,
against the shipped directory fixture, and assert each contract from the task
acceptance list (a)–(f).

The six acceptance bullets map to test classes below:
  (a) `TestPreflight` — preflight passes against the directory.
  (b) `TestBuildTasksFatManifest` — every task carries non-empty
      `description` and `acceptance_criteria`.
  (c) `TestClassifierFanOut` — when a child omits `**Agent:**`,
      `build-tasks` returns a task entry without `agent` key, and the
      orchestrator's prose contract names N discrete dispatches per
      missing-agent child (prose-pinned against SKILL.md Phase 1 step 2).
  (d) `TestPlanReviewArgvScheduleOnly` — schedule-only plan-review
      dispatch receives only `--schedule-file` (no `--plan-file` in
      argv); `render_plan_review_prompt` accepts no `plan_text` actuals.
  (e) `TestRunLogPlanFileMetadata` — run-log events carry per-task
      `plan_file`: directory basename for run_start/run_end, child
      basename for everything else (prose-pinned + JSONL roundtrip).
  (f) `TestSingleFileAutoPromote` — single-file input triggers
      `decompose_auto_promote` event and produces a sibling directory.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import inspect
import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
PLAN_OPS_PATH = SCRIPTS_DIR / "plan_ops.py"
WRAPPER_PATH = SCRIPTS_DIR / "plan_codex_dispatch.py"
DIRECTORY_MODE_FIXTURE = (
    REPO_ROOT / "tests" / "fixtures" / "directory_mode_plan"
)
FIXTURE_SCHEDULE = (
    DIRECTORY_MODE_FIXTURE / "directory_mode_plan.schedule.json"
)
SKILL_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" /
    "SKILL.md"
)
DECOMPOSER_INPUTS = (
    REPO_ROOT / "tests" / "fixtures" / "decomposer_inputs"
)


sys.path.insert(0, str(SCRIPTS_DIR))
import plan_ops  # noqa: E402


def _load_wrapper():
    """Re-import the wrapper from its file path so legacy module aliasing
    doesn't collide with sibling tests that load it under a different name.
    """
    spec = importlib.util.spec_from_file_location(
        "plan_codex_dispatch", WRAPPER_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wrapper = _load_wrapper()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_plan_ops(*args: str, cwd: Path | None = None,
                  input_text: str | None = None,
                  ) -> subprocess.CompletedProcess:
    """Invoke `plan_ops.py` as a subprocess. The interpreter follows
    `sys.executable` so the test runs cleanly under any pytest interpreter.
    """
    cmd = [sys.executable, str(PLAN_OPS_PATH), *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        capture_output=True,
        text=True,
    )


def _copy_fixture(dst: Path) -> Path:
    """Copy the shipped directory_mode fixture (including the schedule
    sidecar) into ``dst``. Mutation tests must work on a tmp copy."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(DIRECTORY_MODE_FIXTURE, dst)
    return dst


@pytest.fixture()
def fixture_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the shipped directory_mode fixture into tmp_path/decomposed_plan
    and sandbox the run-log / run-lock module globals so JSONL writes do
    not touch the repo's docs/plans tree.

    Returns the absolute path to the copied plan directory.
    """
    plan_dir = tmp_path / "decomposed_plan"
    _copy_fixture(plan_dir)

    plans_root = tmp_path / "docs" / "plans"
    plans_root.mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plan_ops, "PLAN_DIR", plans_root)
    monkeypatch.setattr(
        plan_ops, "RUN_LOG_PATH", plans_root / "_run_log.jsonl",
    )
    monkeypatch.setattr(
        plan_ops, "RUN_LOCK_PATH", plans_root / "_run_lock.json",
    )
    return plan_dir


def _git_init(repo: Path) -> None:
    """Init a fresh git repo + commit the seed tree so `git status` is clean.

    Returns nothing; raises if any subcommand exits non-zero.
    """
    subprocess.run(
        ["git", "init", "-q", "-b", "main"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=repo, check=True,
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo, check=True,
    )


# ---------------------------------------------------------------------------
# (a) Preflight passes against the directory
# ---------------------------------------------------------------------------


class TestPreflight:
    """Acceptance (a): preflight passes against the directory.

    Mirrors `TestDirectoryModeHotfixSmoke` Step 1 in `test_plan_ops.py`:
    in-process `cmd_preflight` against the fixture-copy with a real git
    repo + seed commit so `git status --porcelain` returns a clean tree.
    """

    def test_preflight_passes_on_clean_directory_copy(
        self, tmp_path: Path,
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        plan_dir = repo / "decomposed_plan"
        _copy_fixture(plan_dir)
        _git_init(repo)

        cp = _run_plan_ops(
            "preflight", "--plan-file", str(plan_dir), "--json",
            "--unattended-revert-policy", "fail-fast",
            cwd=repo,
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        # AC: preflight passes (pass=true; no source_blocking churn).
        assert body["pass"] is True, body
        assert body["dirty_files"]["source_blocking"] == [], body
        assert body["dirty_files"]["plan_doc"] == [], body
        # `python_path` is the orchestrator's pin source for the rest of the
        # run; it must round-trip as an absolute path.
        assert body["python_path"], body
        assert Path(body["python_path"]).is_absolute(), body

    def test_preflight_passes_when_decompose_just_created_dir(
        self, tmp_path: Path,
    ) -> None:
        """Regression: Phase 0 auto-promote runs `decompose-plan` against a
        single-file plan, producing a sibling directory whose contents are
        all brand-new untracked. `git status --porcelain` collapses such a
        directory to a single entry `?? <plan_dir_rel>/`. Preflight must
        recognize that bare directory entry as the just-created plan dir
        and classify its contents as `plan_doc`, not `source_blocking` —
        otherwise the orchestrator halts on its own decompose output.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        # Commit only the single-file plan; the produced directory will be
        # untracked at preflight time, just like the live auto-promote path.
        single_file = repo / "test_plan.md"
        single_file.write_text(
            (DECOMPOSER_INPUTS / "canonical.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        _git_init(repo)

        cp_d = _run_plan_ops(
            "decompose-plan", "--plan-file", str(single_file), "--json",
            cwd=repo,
        )
        assert cp_d.returncode == 0, (cp_d.stdout, cp_d.stderr)
        produced = Path(json.loads(cp_d.stdout)["produced_dir"])
        assert produced.is_dir(), produced

        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
        # Sanity: git really does collapse the fresh dir to a single entry.
        # If git's defaults change and individual files start showing up
        # without `-uall`, the bug also goes away — but the assertion below
        # still validates the classifier; this just documents the trigger.
        assert any(
            line.endswith("test_plan/") for line in porcelain.splitlines()
        ), porcelain

        cp = _run_plan_ops(
            "preflight", "--plan-file", str(produced), "--json",
            "--unattended-revert-policy", "fail-fast",
            cwd=repo,
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        body = json.loads(cp.stdout)
        assert body["pass"] is True, body
        assert body["dirty_files"]["source_blocking"] == [], body
        # The collapsed `test_plan/` entry must have been expanded into
        # individual files, all classified as plan_doc.
        plan_doc = set(body["dirty_files"]["plan_doc"])
        assert "test_plan/00_INDEX.json" in plan_doc, body
        assert any(
            p.startswith("test_plan/TASK-") and p.endswith(".md")
            for p in plan_doc
        ), body


# ---------------------------------------------------------------------------
# (b) build-tasks produces a fat manifest matching the roster
# ---------------------------------------------------------------------------


class TestBuildTasksFatManifest:
    """Acceptance (b): `build-tasks` output matches the roster, and every
    task carries a non-empty `description` plus a non-empty
    `acceptance_criteria` list. The fixture's three children all declare
    full Description + Acceptance criteria sections, so warnings[] must
    be empty too.
    """

    def test_build_tasks_emits_fat_manifest_for_fixture(
        self, fixture_copy: Path,
    ) -> None:
        cp = _run_plan_ops(
            "build-tasks", "--plans-dir", str(fixture_copy), "--json",
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        res = json.loads(cp.stdout)

        # Roster shape — 3 tasks, ids 001/002/003, each with the expected
        # plan_file basename matching the shipped roster.
        assert res["ok"] is True, res
        assert res["errors"] == [], res
        assert res["warnings"] == [], res
        tasks = res["tasks"]
        assert [t["id"] for t in tasks] == ["001", "002", "003"], tasks
        assert [t["plan_file"] for t in tasks] == [
            "TASK-001_seed.md",
            "TASK-002_write_a.md",
            "TASK-003_write_b.md",
        ], tasks

        # Fat-manifest invariant — description + acceptance_criteria are
        # populated for every task.
        for t in tasks:
            assert isinstance(t["description"], str), t
            assert t["description"].strip(), (
                f"task {t['id']}: description must be non-empty"
            )
            assert isinstance(t["acceptance_criteria"], list), t
            assert len(t["acceptance_criteria"]) >= 1, (
                f"task {t['id']}: acceptance_criteria must be non-empty"
            )
            # plan_file must be a basename per
            # `_is_valid_plan_file_basename` rules; TASK-009 fixture
            # children carry no subdir prefix.
            assert "/" not in t["plan_file"], t

        # Batches respect the DAG: TASK-001 (seeder) lands strictly before
        # TASK-002/003. TASK-002 + TASK-003 are file-disjoint siblings and
        # share the same batch index.
        batches = res["batches"]
        batch_of = {
            tid: b["index"] for b in batches for tid in b["task_ids"]
        }
        assert batch_of["001"] < batch_of["002"], batches
        assert batch_of["001"] < batch_of["003"], batches
        assert batch_of["002"] == batch_of["003"], batches


# ---------------------------------------------------------------------------
# (c) Per-child classifier fan-out — N discrete Agent dispatches when
#     N children omit **Agent:**.
# ---------------------------------------------------------------------------


class TestClassifierFanOut:
    """Acceptance (c): the orchestrator emits N discrete `Agent` tool-use
    blocks in a single assistant turn when N children omit `**Agent:**`.

    The orchestrator is prose-driven (SKILL.md / dispatch-templates.md)
    rather than Python code; we therefore split the assertion into two
    halves:

      1. Behavioral half (Python): `build-tasks` emits the missing-agent
         signal correctly — tasks whose source declares `**Agent:**` carry
         an `agent` key, tasks that omit it do NOT (no synthesized
         placeholder). The orchestrator computes `missing_agent_children`
         by reading this signal, so the per-child Python contract is the
         load-bearing piece a regression would miss.

      2. Prose-pin half: SKILL.md Phase 1 step 2 (Per-child classifier
         fan-out) names "N discrete `Agent` tool calls in a single
         assistant turn" verbatim. We grep the section to lock that
         language in — if the prose drifts away from "N discrete" the
         skill no longer instructs the orchestrator to fan out, so the
         Python signal is moot.
    """

    def test_build_tasks_signals_missing_agent_per_child(
        self, tmp_path: Path,
    ) -> None:
        """Build a directory variant where the SECOND child drops
        `**Agent:**` from its source. `build-tasks` must emit two tasks
        with `agent` declared (TASK-001, TASK-003) and one without
        (TASK-002). The orchestrator then fans out N=1 dispatch.

        Stretches to the multi-missing case in the canonical decomposer
        fixture (two children with no Agent on TASK-002) so the
        N≥2 fan-out is also covered without re-deriving the fixture.
        """
        # Variant 1: surgically strip `**Agent:**` from TASK-002 in the
        # shipped fixture copy.
        plan_dir = tmp_path / "decomposed_plan"
        _copy_fixture(plan_dir)
        child_002 = plan_dir / "TASK-002_write_a.md"
        original = child_002.read_text(encoding="utf-8")
        modified = re.sub(
            r"^- \*\*Agent:\*\*.*\n", "", original, count=1, flags=re.MULTILINE,
        )
        assert modified != original, (
            "fixture sanity: TASK-002 should declare **Agent:** before "
            "the variant strip"
        )
        child_002.write_text(modified, encoding="utf-8")

        cp = _run_plan_ops(
            "build-tasks", "--plans-dir", str(plan_dir), "--json",
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        res = json.loads(cp.stdout)
        tasks = res["tasks"]

        missing_agent = [t for t in tasks if "agent" not in t]
        declared_agent = [t for t in tasks if "agent" in t]
        # Variant 1: exactly one child (TASK-002) is missing **Agent:**.
        assert [t["id"] for t in missing_agent] == ["002"], tasks
        assert {t["id"] for t in declared_agent} == {"001", "003"}, tasks
        # Per the prose contract, `tasks[i].agent` must NOT carry a
        # synthesized placeholder for the missing-agent child — it's
        # absent, so the orchestrator's `"agent" not in t` predicate
        # picks it up cleanly.
        for t in missing_agent:
            assert t.get("agent") in (None, ""), t
            assert "agent" not in t, t

    def test_build_tasks_handles_n_geq_two_missing_agents(
        self, tmp_path: Path,
    ) -> None:
        """N≥2 fan-out shape: the canonical decomposer fixture deliberately
        omits `**Agent:**` on TASK-002 only; we drop it on TASK-001 too so
        the missing-agent set has size 2 and the orchestrator would emit
        two parallel Agent dispatches inside one turn.
        """
        src = tmp_path / "canonical.md"
        src.write_text(
            (DECOMPOSER_INPUTS / "canonical.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        # Strip the first `- **Agent:**` bullet — that's TASK-001 in this
        # fixture's task order. TASK-002 already has none.
        original = src.read_text(encoding="utf-8")
        modified = original.replace(
            "- **Agent:** claude\n", "", 1,
        )
        assert modified != original, (
            "canonical fixture sanity: at least one **Agent:** bullet"
        )
        src.write_text(modified, encoding="utf-8")

        # Decompose to get a directory we can hand to build-tasks.
        cp_d = _run_plan_ops(
            "decompose-plan", "--plan-file", str(src), "--json",
        )
        assert cp_d.returncode == 0, (cp_d.stdout, cp_d.stderr)
        produced = Path(json.loads(cp_d.stdout)["produced_dir"])
        cp = _run_plan_ops(
            "build-tasks", "--plans-dir", str(produced), "--json",
        )
        assert cp.returncode == 0, (cp.stdout, cp.stderr)
        tasks = json.loads(cp.stdout)["tasks"]

        missing_agent = [t for t in tasks if "agent" not in t]
        # TASK-001 (we stripped it) and TASK-002 (canonical fixture omits
        # it) both end up missing-agent. TASK-003 declares codex.
        assert {t["id"] for t in missing_agent} == {"001", "002"}, tasks
        # The orchestrator's pseudo-syntax in SKILL.md Phase 1 step 2
        # would render N=2 discrete Agent blocks — see the prose-pin
        # assertion below for the language guarantee.

    def test_skill_md_pins_n_discrete_fan_out_language(self) -> None:
        """Prose-pin: SKILL.md Phase 1 step 2 must keep the "N discrete
        `Agent` tool calls in a single assistant turn" language. This is
        the load-bearing instruction that turns the missing-agent signal
        from `build-tasks` into N parallel dispatches; a prose edit that
        drops the "N discrete" / "single assistant turn" phrasing breaks
        the orchestrator's contract for fan-out.
        """
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        # Grep for the verbatim contract phrasing. Keep the assertions
        # narrow so cosmetic doc edits are not blocked, but the load-
        # bearing phrases are pinned.
        assert (
            "N discrete `Agent` tool calls in a single assistant turn"
            in skill_text
        ), "SKILL.md Phase 1 step 2 lost the N discrete fan-out phrase"
        assert "Per-child classifier fan-out" in skill_text, (
            "SKILL.md Phase 1 step 2 heading drifted"
        )
        # The pseudo-syntax block illustrating N tool-use blocks must
        # still reference the Phase A-single template.
        assert "templates.PhaseASingle" in skill_text, (
            "SKILL.md Phase 1 step 2 lost the Phase A-single template ref"
        )


# ---------------------------------------------------------------------------
# (d) Schedule-only plan-review: only --schedule-file in argv;
#     render_plan_review_prompt accepts no plan_text.
# ---------------------------------------------------------------------------


class TestPlanReviewArgvScheduleOnly:
    """Acceptance (d): the plan-review dispatch composes argv with
    `--schedule-file <path>` and `--repo-root <path>` only; `--plan-file`
    is NOT a recognized argparse flag (TASK-008 removed it). The
    `render_plan_review_prompt` helper must accept a call shape with no
    `plan_text` actuals (positional or kwarg). Pipes the captured
    envelope through the wrapper end-to-end with `invoke_codex` mocked.
    """

    def test_argv_carries_only_schedule_flag(self, tmp_path: Path) -> None:
        """Argv composition + argparse acceptance check. Mirrors the
        argv the orchestrator would assemble per SKILL.md Phase 1.5.
        """
        plan_dir = tmp_path / "decomposed_plan"
        _copy_fixture(plan_dir)
        sidecar = plan_dir / FIXTURE_SCHEDULE.name
        assert sidecar.is_file(), sidecar  # ships with TASK-006

        argv_under_test = [
            "plan-review",
            "--schedule-file", str(sidecar),
            "--repo-root", str(tmp_path),
        ]
        # Negative: --plan-file is forbidden. Positive: --schedule-file
        # and --repo-root are present.
        assert "--plan-file" not in argv_under_test, argv_under_test
        assert "--schedule-file" in argv_under_test
        assert "--repo-root" in argv_under_test

        parser = wrapper._build_parser()
        args = parser.parse_args(argv_under_test)
        assert args.subcommand == "plan-review"
        assert args.schedule_file == str(sidecar)
        # TASK-008 removed the legacy --plan-file / --plans-dir flags;
        # parsed Namespace must not carry them.
        assert getattr(args, "plan_file", None) is None
        assert getattr(args, "plans_dir", None) is None

    def test_argparse_rejects_legacy_plan_file_flag(
        self, tmp_path: Path,
    ) -> None:
        """Stale callers passing `--plan-file` must fail loudly via
        argparse, not silently no-op."""
        sidecar = tmp_path / "stub.schedule.json"
        sidecar.write_text(
            json.dumps({
                "outcome": "valid", "tasks": [], "batches": [], "gaps": [],
            }),
            encoding="utf-8",
        )
        parser = wrapper._build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([
                "plan-review",
                "--plan-file", str(tmp_path / "phantom.md"),
                "--schedule-file", str(sidecar),
                "--repo-root", str(tmp_path),
            ])

    def test_render_helper_accepts_no_plan_text_actuals(self) -> None:
        """`render_plan_review_prompt(schedule_json)` must succeed without
        any `plan_text` / `plan_basename` / `plan_path` positional or
        keyword actual. Signature introspection backs this up so a future
        helper edit that re-introduces a required `plan_text` parameter
        fails this test loudly.
        """
        schedule_json = json.dumps({
            "outcome": "valid", "tasks": [], "batches": [], "gaps": [],
        })
        prompt = wrapper.render_plan_review_prompt(schedule_json)
        assert isinstance(prompt, str)
        # The prompt must not embed a verbatim plan-markdown block (the
        # retired TASK-006 contract). The Persisted schedule JSON header
        # is the schedule-only marker.
        assert "Plan document (verbatim):" not in prompt, prompt
        assert "Persisted schedule JSON:" in prompt, prompt

        sig = inspect.signature(wrapper.render_plan_review_prompt)
        params = sig.parameters
        # Positional / required parameter is the schedule JSON.
        assert "schedule_json" in params, params
        # Legacy plan_text / plan_abs_path / plans_dir kwargs must not
        # exist anymore (TASK-008 removed them) OR must default to None
        # if they reappear (forward-compat hardening).
        for legacy in ("plan_text", "plan_abs_path", "plans_dir"):
            if legacy in params:
                p = params[legacy]
                assert p.default is None, (
                    f"legacy param {legacy!r} must default to None"
                )

    def test_wrapper_round_trip_with_mocked_codex(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """End-to-end wrapper call against the shipped sidecar with
        `invoke_codex` mocked: the dispatcher must succeed, the fake must
        observe a schedule-only prompt (no plan-markdown block), and the
        captured envelope's `plan_file` must round-trip from the plan
        directory basename.
        """
        plan_dir = tmp_path / "decomposed_plan"
        _copy_fixture(plan_dir)
        sidecar = plan_dir / FIXTURE_SCHEDULE.name

        captured: dict = {"prompt": None, "calls": 0}

        def _fake_invoke_codex(
            prompt: str, workdir: str, schema_path: str, output_path: str,
            timeout_sec: int, sandbox: str | None = None,
        ) -> dict:
            captured["prompt"] = prompt
            captured["calls"] += 1
            Path(output_path).write_text(
                json.dumps({
                    "plan_file": plan_dir.name,
                    "verdict": "approved",
                    "findings": [],
                    "notes": [],
                    "schedule_ok": True,
                    "summary": "stub",
                }),
                encoding="utf-8",
            )
            return {
                "status": "ok",
                "exit_code": 0,
                "stdout": "",
                "stderr": "",
                "file_changes": [],
                "wall_seconds": 0.0,
            }

        monkeypatch.setattr(wrapper, "invoke_codex", _fake_invoke_codex)

        ns = argparse.Namespace(
            subcommand="plan-review",
            schedule_file=str(sidecar),
            repo_root=str(tmp_path),
            dry_run=False,
            timeout=180,
            allow_gaps=False,
            json=True,
        )
        capsys.readouterr()  # drain
        rc = wrapper.cmd_plan_review(ns)
        out = capsys.readouterr().out
        envelope = json.loads(out)

        assert rc == 0, envelope
        assert captured["calls"] == 1, captured
        # Prompt: schedule-only — no plan-markdown block.
        assert captured["prompt"] is not None
        assert (
            "Plan document (verbatim):" not in captured["prompt"]
        ), captured["prompt"]
        assert "Persisted schedule JSON:" in captured["prompt"]
        # Envelope: plan_file derives from the plan directory basename
        # (per `_plan_paths.py` directory-mode convention).
        assert envelope["plan_file"] == plan_dir.name, envelope
        assert envelope["subcommand"] == "plan-review", envelope


# ---------------------------------------------------------------------------
# (e) Run-log per-task plan_file metadata: dir basename for run_start /
#     run_end, child basename for everything in between.
# ---------------------------------------------------------------------------


class TestRunLogPlanFileMetadata:
    """Acceptance (e): run-log events log the right `plan_file` per the
    SKILL.md table:
       run_start / run_end       → directory basename
       batch_start / implement_* → child basename
       commit_done               → child basename

    Two-pronged assertion:
      1. JSONL roundtrip: feed the orchestrator's would-be sequence
         through `log-event` and read `_run_log.jsonl` back; assert each
         event row carries the expected `plan_file` value.
      2. Prose-pin: SKILL.md §"Per-task `<plan-file>` resolution" lists
         every event with the right basename rule. We grep those bullets
         to lock the contract — a doc edit that swaps the rule
         (e.g. promoting commit_done to dir basename) is caught loudly.
    """

    def test_run_log_events_carry_correct_plan_file(
        self, fixture_copy: Path,
    ) -> None:
        plan_dir = fixture_copy
        dir_basename = plan_dir.name  # "decomposed_plan"
        child_seed = "TASK-001_seed.md"
        child_a = "TASK-002_write_a.md"

        # Replay the orchestrator's intended order of log events. Each
        # call uses the same JSONL appender path (`_append_run_log`)
        # the real orchestrator drives via `log-event`.
        events_to_log = [
            ("run_start", {"run_id": "R1", "plan_file": dir_basename}),
            ("batch_start", {
                "run_id": "R1", "batch_index": 1,
                "plan_file": child_seed,
            }),
            ("implement_start", {
                "run_id": "R1", "task_id": "001",
                "plan_file": child_seed,
            }),
            ("commit_done", {
                "run_id": "R1", "task_id": "001",
                "plan_file": child_seed,
                "sha": "deadbeef",
            }),
            ("implement_start", {
                "run_id": "R1", "task_id": "002",
                "plan_file": child_a,
            }),
            ("run_end", {"run_id": "R1", "plan_file": dir_basename}),
        ]
        for event, fields in events_to_log:
            cp = _run_plan_ops(
                "log-event",
                "--event", event,
                "--fields-json", json.dumps(fields),
                "--json",
            )
            assert cp.returncode == 0, (event, cp.stdout, cp.stderr)

        log = plan_ops.RUN_LOG_PATH
        assert log.is_file(), log
        rows = [json.loads(l) for l in log.read_text(
            encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == len(events_to_log), rows

        # AC: run_start / run_end carry the directory basename; every
        # other event carries the child basename.
        for row, (event, _) in zip(rows, events_to_log):
            assert row["event"] == event, row
            if event in {"run_start", "run_end"}:
                assert row["plan_file"] == dir_basename, row
            else:
                # batch_start / implement_start / commit_done all use the
                # child basename.
                assert row["plan_file"] in {child_seed, child_a}, row
                assert row["plan_file"] != dir_basename, (
                    "non-bracket events must carry child basename, "
                    f"not directory basename, got {row}"
                )

    def test_skill_md_pins_per_event_basename_rule(self) -> None:
        """SKILL.md §Per-task `<plan-file>` resolution must keep the
        per-event `plan_file` rule table verbatim. Drift here desyncs
        the orchestrator's prose from the JSONL contract this test just
        proved."""
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        # Bracket events use directory basename.
        assert (
            "`run_start` — `plan_file: \"<dir-basename>\"`" in skill_text
        ), "SKILL.md run_start basename rule drifted"
        assert (
            "`run_end` — `plan_file: \"<dir-basename>\"`" in skill_text
        ), "SKILL.md run_end basename rule drifted"
        # Per-task events use child basename.
        assert (
            "`implement_start` — `plan_file: \"<child-basename>\"`"
            in skill_text
        ), "SKILL.md implement_start basename rule drifted"
        assert (
            "`commit_done` — `plan_file: \"<child-basename>\"`" in skill_text
        ), "SKILL.md commit_done basename rule drifted"


# ---------------------------------------------------------------------------
# (f) Auto-promote: single-file input → decompose_auto_promote event
#     and a sibling directory.
# ---------------------------------------------------------------------------


class TestSingleFileAutoPromote:
    """Acceptance (f): when the user passes a single-file plan (a
    whole-plan markdown), Phase 0 invokes `decompose-plan`, the resulting
    sibling directory becomes the new `<plan-path>`, and a
    `decompose_auto_promote` run-log event is appended with
    `{source_file, produced_dir, task_count}`.

    Assertions:
      1. `decompose-plan` against a single-file fixture exits 0 and
         produces the sibling directory.
      2. `log-event --event decompose_auto_promote` accepts the
         orchestrator's payload shape.
      3. The produced directory matches the directory-mode contract:
         `00_INDEX.json` parses, every chunk file exists, every child
         re-parses via `_parse_task_block(level=3)` (i.e., directory-mode
         dispatch can proceed).
    """

    def test_single_file_input_decomposes_and_emits_event(
        self, fixture_copy: Path, tmp_path: Path,
    ) -> None:
        # Use the canonical decomposer fixture as the single-file input
        # (well-formed, three tasks, sibling-directory expected).
        src = tmp_path / "single_file_plan.md"
        src.write_text(
            (DECOMPOSER_INPUTS / "canonical.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        # Phase 0 step 1: decompose-plan.
        cp_d = _run_plan_ops(
            "decompose-plan", "--plan-file", str(src), "--json",
        )
        assert cp_d.returncode == 0, (cp_d.stdout, cp_d.stderr)
        decomp = json.loads(cp_d.stdout)
        assert decomp["ok"] is True, decomp
        produced = Path(decomp["produced_dir"])
        assert produced.is_dir(), produced
        # Sibling directory at <file-parent>/<file-stem>/ per SKILL.md.
        assert produced.parent == src.parent, (produced, src.parent)
        assert produced.name == src.stem, (produced, src.stem)

        # Phase 0 step 2: emit decompose_auto_promote event.
        cp_l = _run_plan_ops(
            "log-event",
            "--event", "decompose_auto_promote",
            "--fields-json", json.dumps({
                "run_id": "R_AUTO",
                "source_file": str(src),
                "produced_dir": str(produced),
                "task_count": decomp["task_count"],
            }),
            "--json",
        )
        assert cp_l.returncode == 0, (cp_l.stdout, cp_l.stderr)

        # Event must be in the allowed vocabulary AND must land in the
        # JSONL log with the expected fields.
        assert (
            "decompose_auto_promote" in plan_ops.ALLOWED_LOG_EVENTS
        ), plan_ops.ALLOWED_LOG_EVENTS

        log = plan_ops.RUN_LOG_PATH
        assert log.is_file(), log
        rows = [
            json.loads(l)
            for l in log.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
        promote_rows = [r for r in rows if r["event"] == "decompose_auto_promote"]
        assert len(promote_rows) == 1, rows
        promote = promote_rows[0]
        assert promote["source_file"] == str(src), promote
        assert promote["produced_dir"] == str(produced), promote
        assert promote["task_count"] == decomp["task_count"], promote

        # Phase 0 step 3: the produced directory satisfies the directory-
        # mode contract — every subsequent phase assumes plan_path.is_dir()
        # AND that `build-tasks` against that directory succeeds.
        index = produced / "00_INDEX.json"
        assert index.is_file(), index
        roster = plan_ops._parse_index_roster(index)
        assert {"001", "002", "003"} <= set(roster.keys()), roster
        for chunk in roster.values():
            child_path = produced / chunk["file"]
            assert child_path.is_file(), child_path
            # H3 sub-heading is the directory-mode grammar `build-tasks`
            # parses; a directory mode dispatch would fail here if the
            # auto-promoter wrote H2 by accident.
            text = child_path.read_text(encoding="utf-8")
            assert re.search(
                r"^### TASK-\d{3}[A-Z]?:", text, re.MULTILINE,
            ), text[:200]

        cp_b = _run_plan_ops(
            "build-tasks", "--plans-dir", str(produced), "--json",
        )
        assert cp_b.returncode == 0, (cp_b.stdout, cp_b.stderr)
        bt = json.loads(cp_b.stdout)
        assert bt["ok"] is True, bt
        assert bt["errors"] == [], bt
        # Directory-mode dispatch can proceed: tasks[] non-empty.
        assert len(bt["tasks"]) == decomp["task_count"], bt

    def test_skill_md_pins_auto_promote_phase_0_step(self) -> None:
        """SKILL.md must keep the Phase 0 auto-promote step + the
        `decompose_auto_promote` event documented. The orchestrator
        relies on this prose to know it must run `decompose-plan` before
        preflight when the input is a single file."""
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        assert "decompose_auto_promote" in skill_text, (
            "SKILL.md lost the decompose_auto_promote event reference"
        )
        assert "Auto-promote single-file input to directory mode" in skill_text, (
            "SKILL.md Phase 0 auto-promote step heading drifted"
        )
        # TASK-015 migrated SKILL.md's Phase 0 invocation from the CLI
        # form `plan_ops.py decompose-plan --plan-file <abs>` to the
        # canonical MCP tool form `plan_ops__decompose_plan` with input
        # `{"plan_file": "<abs>"}`. The `plan_file` input is pinned in
        # the tool's input schema; grep there for the canonical shape.
        from pathlib import Path
        repo_root = Path(__file__).resolve().parents[2]
        decompose_schema = (
            repo_root / "plugins" / "plan-executor" / "scripts"
            / "schemas" / "mcp" / "decompose_plan.input.json"
        )
        decompose_schema_text = decompose_schema.read_text(encoding="utf-8")
        assert '"plan_file"' in decompose_schema_text, (
            "decompose_plan.input.json must pin the `plan_file` input "
            "(the MCP-tool form replaces the legacy "
            "`decompose-plan --plan-file` CLI invocation example)"
        )


# ---------------------------------------------------------------------------
# Phase D-Claude MCP render-path prose-pin (PLAN_AGENT_DISPATCH_MCP TASK-002)
# ---------------------------------------------------------------------------


class TestPhaseDClaudeUsesMcpRenderPath:
    """Phase D-Claude dispatch must go through
    `plan_ops__build_agent_dispatch_prompt` with
    `template_id="code-reviewer-d-claude"`, NOT through an
    `awk`/`Read` against `dispatch-templates.md`.

    The orchestrator is prose-driven (SKILL.md), so this is enforced
    as a prose-pin pair on SKILL.md + dispatch-templates.md plus a
    schema/registry check that the named `template_id` is honored by
    the MCP tool. A live `/implement-plan` dry-run trace is not
    available in this harness (Step (c) above documents the same
    prose-driven nature); the prose pin is the load-bearing
    regression backstop.
    """

    def test_skill_md_pins_canonical_agent_dispatch_recipe(self) -> None:
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        # New recipe section exists.
        assert "## Canonical Agent dispatch recipe" in skill_text, (
            "SKILL.md must declare the canonical Agent dispatch recipe"
        )
        # The MCP tool name is pinned in the recipe.
        assert "plan_ops__build_agent_dispatch_prompt" in skill_text, (
            "SKILL.md recipe must invoke plan_ops__build_agent_dispatch_prompt"
        )
        # The explicit "do NOT read dispatch-templates.md at dispatch time"
        # rule is pinned (this is the structural protection against the
        # legacy markdown-read path sneaking back in).
        assert "Do NOT read `dispatch-templates.md`" in skill_text, (
            "SKILL.md recipe must forbid reading dispatch-templates.md "
            "at dispatch time"
        )

    def test_skill_md_phase_d1_routes_via_agent_dispatch_recipe(
        self,
    ) -> None:
        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        # Both Phase D.1 rows that dispatch the `code-reviewer` Agent
        # MUST reference the canonical recipe + the named template_id.
        assert "code-reviewer-d-claude" in skill_text, (
            "SKILL.md Phase D.1 must name template_id "
            "`code-reviewer-d-claude` so the MCP tool can render the "
            "Phase D-Claude template"
        )
        assert "§Canonical Agent dispatch recipe" in skill_text, (
            "SKILL.md Phase D.1 must reference §Canonical Agent "
            "dispatch recipe (the runtime render path)"
        )

    def test_skill_md_all_eight_phases_use_canonical_agent_recipe(
        self,
    ) -> None:
        """TASK-004 cutover assertion: across the eight in-process Agent
        dispatch phases — Phase 1.5-Claude, Phase 1.5a (three variants),
        Phase 1-triage, Phase 1.5.5, Phase D-Claude, Phase D.5,
        Phase D.2a.6, and Phase D.4-rescue — SKILL.md names the
        corresponding template_id and there are zero `awk` / `Read`
        invocations against `dispatch-templates.md` outside the
        §Canonical Agent dispatch recipe documentation block.
        """
        import re

        skill_text = SKILL_PATH.read_text(encoding="utf-8")
        required_template_ids = [
            "code-reviewer-d-claude",   # Phase D-Claude (TASK-002)
            "code-reviewer-d5",          # Phase D.5
            "plan-reviewer",             # Phase 1.5-Claude
            "plan-author-task-targeted", # Phase 1.5a variant A
            "plan-author-schedule-level",# Phase 1.5a variant B
            "plan-author-legacy-whole-plan", # Phase 1.5a legacy variant C
            "plan-review-triage",        # Phase 1-triage + Phase 1.5.5
            "plan-remediator-narrow",    # Phase D.2a.6
            "plan-remediator-rescue",    # Phase D.4-rescue
        ]
        for tid in required_template_ids:
            assert tid in skill_text, (
                f"SKILL.md must name template_id `{tid}` so the "
                "MCP tool can render the corresponding template at "
                "dispatch time"
            )

        # Zero `awk`/`Read` invocations against `dispatch-templates.md`
        # outside the §Canonical Agent dispatch recipe documentation
        # block. The recipe section itself names the file in a "do NOT
        # read" rule; everything else must avoid it entirely.
        offenders: list[str] = []
        for match in re.finditer(
            r"(awk[^\n]*dispatch-templates|Read[^\n]*dispatch-templates\.md)",
            skill_text,
        ):
            # Skip matches inside the canonical recipe block (the only
            # site that names `dispatch-templates.md` legitimately).
            start = match.start()
            preceding = skill_text[:start]
            heading = preceding.rfind("## ")
            if heading != -1:
                next_nl = skill_text.find("\n", heading)
                section_heading = skill_text[heading:next_nl]
                if "Canonical Agent dispatch recipe" in section_heading:
                    continue
            offenders.append(match.group(0))
        assert offenders == [], (
            "SKILL.md must not contain `awk`/`Read` invocations "
            "against dispatch-templates.md outside the §Canonical "
            f"Agent dispatch recipe block — found: {offenders!r}"
        )

    def test_dispatch_templates_md_seven_sections_carry_header_note(
        self,
    ) -> None:
        """TASK-004 cutover assertion: each of the seven non-D-Claude
        dispatch-template sections gains the same header-note pattern
        TASK-002 added to Phase D-Claude.
        """
        templates_path = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "dispatch-templates.md"
        )
        text = templates_path.read_text(encoding="utf-8")
        for tid in [
            "plan-reviewer",
            "plan-author-task-targeted",
            "plan-author-schedule-level",
            "plan-author-legacy-whole-plan",
            "plan-review-triage",
            "code-reviewer-d5",
            "plan-remediator-narrow",
            "plan-remediator-rescue",
        ]:
            needle = (
                "Rendered by plan_ops__build_agent_dispatch_prompt("
                f'template_id="{tid}"'
            )
            assert needle in text, (
                "dispatch-templates.md section for template_id "
                f"`{tid}` must carry the MCP-render header note"
            )

    def test_dispatch_templates_md_carries_header_note(self) -> None:
        templates_path = (
            REPO_ROOT / "plugins" / "plan-executor" / "skills"
            / "implement-plan" / "dispatch-templates.md"
        )
        text = templates_path.read_text(encoding="utf-8")
        # The Phase D-Claude section gains a header note pointing
        # readers at the SKILL recipe and disclaiming runtime read.
        assert (
            "Rendered by plan_ops__build_agent_dispatch_prompt(template_id="
            "\"code-reviewer-d-claude\""
        ) in text, (
            "dispatch-templates.md Phase D-Claude section must carry "
            "the MCP-render header note"
        )
        assert (
            "The orchestrator does not read this section at runtime"
            in text
        ), (
            "dispatch-templates.md Phase D-Claude header note must "
            "disclaim runtime read"
        )

    def test_mcp_tool_registers_code_reviewer_d_claude_template(
        self,
    ) -> None:
        """Registry-level assertion (in lieu of a live tool-use trace):
        the `plan_ops__build_agent_dispatch_prompt` tool's input schema
        + implementation honor `template_id="code-reviewer-d-claude"`.
        """
        schema_path = (
            REPO_ROOT / "plugins" / "plan-executor" / "scripts"
            / "schemas" / "mcp"
            / "build_agent_dispatch_prompt.input.json"
        )
        assert schema_path.is_file(), schema_path
        schema_text = schema_path.read_text(encoding="utf-8")
        # The named template_id is enumerated as a valid input.
        assert "code-reviewer-d-claude" in schema_text, (
            "build_agent_dispatch_prompt input schema must enumerate "
            "template_id `code-reviewer-d-claude`"
        )
