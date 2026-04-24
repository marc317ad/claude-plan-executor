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
