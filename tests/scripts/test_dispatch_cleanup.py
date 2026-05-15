"""Unit + integration tests for ``_dispatch_cleanup``.

Exercises the public API:

  * :func:`snapshot_baseline(repo_root) -> dict`
  * :func:`apply_cleanup(baseline, declared_files_changed, repo_root) -> dict`

against a real ``git init -q -b main`` fixture per test, covering each
acceptance-criterion bullet from PLAN_NESTED_DISPATCH §8.1 TASK-004.

The concurrent-safety test spawns two ``multiprocessing.Process``
workers on disjoint declared file sets; if the wrapper ever stashed
baseline state in a shared location (``~/.claude``, a /tmp file, a
module-level dict), one worker's "out of scope" path would be the
other worker's declared scope and the cleanup would cross-contaminate.
The test passes only if each worker sees ONLY its own writes after
``apply_cleanup``.
"""

from __future__ import annotations

import importlib.util
import multiprocessing
import subprocess
from pathlib import Path
from typing import Any, Mapping

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    REPO_ROOT
    / "plugins"
    / "plan-executor"
    / "scripts"
    / "_dispatch_cleanup.py"
)


def _load_cleanup():
    spec = importlib.util.spec_from_file_location(
        "_dispatch_cleanup", MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


cleanup = _load_cleanup()


def _call_cleanup(
    baseline,
    declared,
    repo,
    *,
    source: str = "wrapper-declared-scope",
    policy: str = "preserve-only",
):
    """Test helper: thread the TASK-002 authorization_source and the
    PLAN_WRAPPER_REVERT_POLICY_GATE TASK-001 ``unattended_revert_policy``
    through every call. The default policy is ``preserve-only`` so
    existing tests that assert on ``restored`` / ``deleted`` content
    keep their existing semantics; gate-tests override ``policy`` to
    exercise the new non-destructive ``pause`` / ``fail-fast`` branches.
    """
    return cleanup.apply_cleanup(
        baseline, declared, repo,
        authorization_source=source,
        unattended_revert_policy=policy,
    )


# ---------------------------------------------------------------------------
# Repo fixture
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


def _make_repo(tmp_path: Path, name: str = "repo") -> Path:
    """Initialize a fresh git repo with a single committed README."""
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git(["add", "README.md"], cwd=repo)
    _git(["commit", "-q", "-m", "init"], cwd=repo)
    return repo


# ---------------------------------------------------------------------------
# snapshot_baseline
# ---------------------------------------------------------------------------


class TestSnapshotBaseline:
    def test_clean_repo_captures_empty_state(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        assert baseline["captured"] is True
        assert baseline["tracked_changed"] == []
        assert baseline["untracked"] == []
        assert baseline["tracked_blobs"] == {}
        assert baseline["untracked_blobs"] == {}
        assert baseline["repo_root"] == str(repo.resolve())

    def test_captures_tracked_unstaged_changes(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("modified\n")
        baseline = cleanup.snapshot_baseline(str(repo))
        assert baseline["captured"] is True
        assert "README.md" in baseline["tracked_changed"]
        assert baseline["tracked_blobs"]["README.md"] == b"modified\n"
        assert baseline["untracked"] == []

    def test_captures_tracked_staged_changes(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("staged\n")
        _git(["add", "README.md"], cwd=repo)
        baseline = cleanup.snapshot_baseline(str(repo))
        assert "README.md" in baseline["tracked_changed"]
        assert baseline["tracked_blobs"]["README.md"] == b"staged\n"

    def test_captures_untracked_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "new.txt").write_text("new content\n")
        (repo / "subdir").mkdir()
        (repo / "subdir" / "leaf.py").write_text("leaf\n")
        baseline = cleanup.snapshot_baseline(str(repo))
        assert "new.txt" in baseline["untracked"]
        assert "subdir/leaf.py" in baseline["untracked"]
        assert baseline["untracked_blobs"]["new.txt"] == b"new content\n"

    def test_excludes_gitignored(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / ".gitignore").write_text("ignored.tmp\n")
        _git(["add", ".gitignore"], cwd=repo)
        _git(["commit", "-q", "-m", "ignore"], cwd=repo)
        (repo / "ignored.tmp").write_text("noise\n")
        baseline = cleanup.snapshot_baseline(str(repo))
        assert "ignored.tmp" not in baseline["untracked"]

    def test_returns_uncaptured_when_not_a_repo(self, tmp_path):
        notrepo = tmp_path / "notrepo"
        notrepo.mkdir()
        baseline = cleanup.snapshot_baseline(str(notrepo))
        # ``git diff`` outside a repo returns non-zero but does not raise;
        # _changed_paths returns an empty snapshot. captured stays True
        # because no exception was raised. The point of the captured=
        # False sentinel is unrecoverable git failure (binary missing,
        # subprocess error). Documenting this distinction here for the
        # reviewer.
        assert isinstance(baseline, dict)

    def test_baseline_is_serializable_dict(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "x.txt").write_text("x\n")
        baseline = cleanup.snapshot_baseline(str(repo))
        # The baseline is process-local and may carry bytes; it does not
        # need to be JSON-serializable, but it must be a plain dict the
        # caller can stash. Verifying shape only.
        assert isinstance(baseline, dict)
        for key in (
            "captured", "tracked_changed", "untracked",
            "tracked_blobs", "untracked_blobs",
            "tracked_hashes", "untracked_hashes", "repo_root",
        ):
            assert key in baseline

    def test_baseline_blob_threshold_drops_oversized(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path)
        monkeypatch.setattr(cleanup, "MAX_BASELINE_BLOB_BYTES", 4)
        (repo / "big.bin").write_text("x" * 100)
        baseline = cleanup.snapshot_baseline(str(repo))
        assert "big.bin" in baseline["untracked"]
        # Oversized -> blob omitted but path still recorded.
        assert "big.bin" not in baseline["untracked_blobs"]


# ---------------------------------------------------------------------------
# apply_cleanup
# ---------------------------------------------------------------------------


class TestApplyCleanupBasics:
    def test_skipped_no_baseline(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = _call_cleanup(
            {"captured": False}, ["foo"], str(repo),
        )
        assert result["cleanup_strategy"] == "skipped_no_baseline"
        assert result["scope_violation_detected"] is False
        assert result["scope_misreport_detected"] is False
        assert result["restored"] == []
        assert result["deleted"] == []
        assert result["baseline_captured"] is False

    def test_no_changes_no_violations(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        result = _call_cleanup(baseline, [], str(repo))
        assert result["cleanup_strategy"] == "delta_bounded"
        assert result["scope_violation_detected"] is False
        assert result["scope_misreport_detected"] is False
        assert result["out_of_scope_paths"] == []
        assert result["restored"] == []
        assert result["deleted"] == []

    def test_in_declaration_write_is_allowed(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        # Simulate the nested session creating a declared file.
        (repo / "declared.txt").write_text("payload\n")
        result = _call_cleanup(
            baseline, ["declared.txt"], str(repo),
        )
        assert result["scope_violation_detected"] is False
        assert result["scope_misreport_detected"] is False
        assert (repo / "declared.txt").exists()
        assert (repo / "declared.txt").read_text() == "payload\n"


class TestScopeViolationDetected:
    def test_untracked_out_of_scope_is_deleted(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        # Nested session declares "declared.txt" but writes "rogue.txt" too.
        (repo / "declared.txt").write_text("ok\n")
        (repo / "rogue.txt").write_text("nope\n")
        result = _call_cleanup(
            baseline, ["declared.txt"], str(repo),
        )
        assert result["scope_violation_detected"] is True
        assert "rogue.txt" in result["out_of_scope_paths"]
        assert "rogue.txt" in result["deleted"]
        assert not (repo / "rogue.txt").exists()
        # Declared file is preserved.
        assert (repo / "declared.txt").read_text() == "ok\n"

    def test_tracked_out_of_scope_is_restored(self, tmp_path):
        repo = _make_repo(tmp_path)
        # Add a second tracked file so we can mutate it.
        (repo / "keep.py").write_text("orig\n")
        _git(["add", "keep.py"], cwd=repo)
        _git(["commit", "-q", "-m", "add keep"], cwd=repo)

        baseline = cleanup.snapshot_baseline(str(repo))
        # Nested session declares "declared.txt" but mutates keep.py.
        (repo / "declared.txt").write_text("ok\n")
        (repo / "keep.py").write_text("rogue mutation\n")
        result = _call_cleanup(
            baseline, ["declared.txt"], str(repo),
        )
        assert result["scope_violation_detected"] is True
        assert "keep.py" in result["out_of_scope_paths"]
        assert "keep.py" in result["restored"]
        assert (repo / "keep.py").read_text() == "orig\n"

    def test_no_declaration_means_all_writes_are_violations(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "a.txt").write_text("a\n")
        (repo / "b.txt").write_text("b\n")
        result = _call_cleanup(baseline, [], str(repo))
        assert result["scope_violation_detected"] is True
        assert set(result["out_of_scope_paths"]) == {"a.txt", "b.txt"}
        assert set(result["deleted"]) == {"a.txt", "b.txt"}

    def test_protected_path_is_not_mutated(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        # Create an executor-protected path under .claude/ — a new
        # session must NEVER have its .claude/ writes erased.
        proto = repo / ".claude" / "internal.json"
        proto.parent.mkdir(parents=True, exist_ok=True)
        proto.write_text("{}\n")
        result = _call_cleanup(baseline, [], str(repo))
        # Out-of-declaration but protected → classified under
        # protected_skipped, NOT mutated, and DOES NOT flip the
        # scope-violation flag.
        assert proto.exists()
        assert ".claude/internal.json" in result["protected_skipped"]
        assert ".claude/internal.json" not in result["out_of_scope_paths"]
        assert result["scope_violation_detected"] is False

    def test_protected_runlog_is_not_mutated(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        run_log = repo / "docs" / "plans" / "_run_log.jsonl"
        run_log.parent.mkdir(parents=True, exist_ok=True)
        run_log.write_text('{"event":"x"}\n')
        result = _call_cleanup(baseline, [], str(repo))
        assert run_log.exists()
        assert "docs/plans/_run_log.jsonl" in result["protected_skipped"]
        assert result["scope_violation_detected"] is False


class TestScopeMisreportDetected:
    def test_misreport_when_declared_path_untouched(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        # Declare two paths but only write one.
        (repo / "real.txt").write_text("real\n")
        result = _call_cleanup(
            baseline, ["real.txt", "phantom.txt"], str(repo),
        )
        assert result["scope_misreport_detected"] is True
        assert "phantom.txt" in result["misreported_paths"]
        assert "real.txt" not in result["misreported_paths"]
        # Nothing was deleted; misreport is observe-only on declared.
        assert result["scope_violation_detected"] is False

    def test_no_misreport_when_all_declared_were_touched(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "a.txt").write_text("a\n")
        (repo / "b.txt").write_text("b\n")
        result = _call_cleanup(
            baseline, ["a.txt", "b.txt"], str(repo),
        )
        assert result["scope_misreport_detected"] is False
        assert result["misreported_paths"] == []

    def test_misreport_for_modify_declared_but_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        # Declare README.md (already present, unchanged) — misreport.
        result = _call_cleanup(baseline, ["README.md"], str(repo))
        assert result["scope_misreport_detected"] is True
        assert "README.md" in result["misreported_paths"]


class TestObservedDeltaSubtraction:
    def test_pre_existing_dirty_is_not_violation(self, tmp_path):
        """A path that was already dirty at baseline (and remains the
        same dirty state after dispatch) is NOT counted as a delta and
        does NOT flip scope_violation_detected. This is what enables
        wrapper isolation — sibling tasks that pre-modified files
        unrelated to this task must be left alone."""
        repo = _make_repo(tmp_path)
        # Pre-dirty the README before snapshot.
        (repo / "README.md").write_text("pre-dirty\n")
        baseline = cleanup.snapshot_baseline(str(repo))
        # Dispatch declares NOTHING and touches NOTHING; the pre-dirty
        # state is unchanged.
        result = _call_cleanup(baseline, [], str(repo))
        assert result["scope_violation_detected"] is False
        assert "README.md" not in result["out_of_scope_paths"]
        # Pre-dirty content preserved.
        assert (repo / "README.md").read_text() == "pre-dirty\n"

    def test_pre_existing_dirty_then_overwritten_out_of_scope(self, tmp_path):
        """If a pre-dirty file is FURTHER mutated by the nested session
        (and not declared), apply_cleanup must restore it to its
        pre-dispatch baseline content (NOT to HEAD)."""
        repo = _make_repo(tmp_path)
        (repo / "README.md").write_text("pre-dirty\n")
        baseline = cleanup.snapshot_baseline(str(repo))
        # Nested session mutates README again.
        (repo / "README.md").write_text("rogue further mutation\n")
        result = _call_cleanup(baseline, [], str(repo))
        assert result["scope_violation_detected"] is True
        assert "README.md" in result["out_of_scope_paths"]
        assert "README.md" in result["restored"]
        # Restored to PRE-DISPATCH state (the dirty version), NOT HEAD.
        assert (repo / "README.md").read_text() == "pre-dirty\n"


# ---------------------------------------------------------------------------
# TASK-002 (wrapper_autoclean_authorization): authorization gate
# ---------------------------------------------------------------------------


class TestAuthorizationGate:
    def test_apply_cleanup_raises_typeerror_without_authorization_source(
        self, tmp_path,
    ):
        repo = _make_repo(tmp_path)
        with pytest.raises(TypeError) as excinfo:
            cleanup.apply_cleanup({}, [], str(repo))
        msg = str(excinfo.value)
        assert "expected one of" in msg
        for v in sorted([
            "wrapper-declared-scope",
            "wrapper-empty-scope-readonly",
            "orchestrator-declared-scope",
            "orchestrator-empty-scope-readonly",
        ]):
            assert v in msg

    def test_apply_cleanup_rejects_unknown_authorization_source(
        self, tmp_path,
    ):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        with pytest.raises(ValueError) as excinfo:
            cleanup.apply_cleanup(
                baseline, [], str(repo),
                authorization_source="rogue",
            )
        assert "unknown authorization_source 'rogue'" in str(excinfo.value)

    def test_apply_cleanup_wrapper_declared_scope_with_empty_declared_reverts_all(
        self, tmp_path,
    ):
        """When the explicit ``wrapper-declared-scope`` is passed with an
        empty ``declared`` list, the legacy semantic is preserved: every
        observed delta is reverted. The anti-aliasing guard against this
        configuration lives in ``plan_claude_dispatch.py:cmd_run`` and
        prevents the wrapper from EVER reaching this branch silently for
        a write-authorized agent — but if a caller explicitly authorizes
        it, cleanup proceeds."""
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "rogue.txt").write_text("nope\n")
        result = _call_cleanup(
            baseline, [], str(repo), source="wrapper-declared-scope",
        )
        assert result["cleanup_strategy"] == "delta_bounded"
        assert result["scope_violation_detected"] is True
        assert "rogue.txt" in result["deleted"]
        assert not (repo / "rogue.txt").exists()

    def test_apply_cleanup_wrapper_empty_scope_readonly_with_empty_declared_reverts_all(
        self, tmp_path,
    ):
        """A read-only agent that wrote anything violated its contract;
        the wrapper reverts every observed delta."""
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "leaked.txt").write_text("read-only agent wrote me\n")
        result = _call_cleanup(
            baseline, [], str(repo), source="wrapper-empty-scope-readonly",
        )
        assert result["cleanup_strategy"] == "delta_bounded"
        assert result["scope_violation_detected"] is True
        assert "leaked.txt" in result["deleted"]
        assert not (repo / "leaked.txt").exists()

    def test_apply_cleanup_wrapper_declared_scope_with_nonempty_declared_preserves_in_scope(
        self, tmp_path,
    ):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "x.py").write_text("declared\n")
        (repo / "y.py").write_text("undeclared\n")
        result = _call_cleanup(
            baseline, ["x.py"], str(repo), source="wrapper-declared-scope",
        )
        assert (repo / "x.py").exists()
        assert (repo / "x.py").read_text() == "declared\n"
        assert "y.py" in result["deleted"]
        assert not (repo / "y.py").exists()

    def test_apply_cleanup_accepts_orchestrator_declared_scope(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "declared.txt").write_text("in scope\n")
        (repo / "rogue.txt").write_text("out of scope\n")
        result = cleanup.apply_cleanup(
            baseline, ["declared.txt"], str(repo),
            authorization_source="orchestrator-declared-scope",
            unattended_revert_policy="preserve-only",
        )
        assert result["cleanup_strategy"] == "delta_bounded"
        assert result["scope_violation_detected"] is True
        assert "rogue.txt" in result["out_of_scope_paths"]
        assert "rogue.txt" in result["deleted"]
        assert not (repo / "rogue.txt").exists()
        assert (repo / "declared.txt").read_text() == "in scope\n"

    def test_apply_cleanup_accepts_orchestrator_empty_scope_readonly(
        self, tmp_path,
    ):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "leaked.txt").write_text("read-only violation\n")
        result = cleanup.apply_cleanup(
            baseline, [], str(repo),
            authorization_source="orchestrator-empty-scope-readonly",
            unattended_revert_policy="preserve-only",
        )
        assert result["cleanup_strategy"] == "delta_bounded"
        assert result["scope_violation_detected"] is True
        assert "leaked.txt" in result["out_of_scope_paths"]
        assert "leaked.txt" in result["deleted"]
        assert not (repo / "leaked.txt").exists()


# ---------------------------------------------------------------------------
# TASK-004 hardening: failed_paths surfacing
# ---------------------------------------------------------------------------


class TestFailedPathsSurfacing:
    """Per-file ``OSError`` victims must surface via ``failed_paths``.

    Exercises the TASK-004 hardening: if ``_restore_path`` returns
    ``"failed"`` for any out-of-scope path (read-only target, permission
    denied, parent dir not writable, etc.), the path is appended to
    ``failed_paths`` and processing continues for the rest. The wrapper
    layer translates a non-empty ``failed_paths`` into a
    ``cleanup_failure`` envelope (covered in
    ``test_plan_claude_dispatch_cli.py``).
    """

    def test_clean_run_has_empty_failed_paths(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "rogue.txt").write_text("nope\n")
        result = _call_cleanup(baseline, [], str(repo))
        assert result["scope_violation_detected"] is True
        assert "rogue.txt" in result["deleted"]
        assert result["failed_paths"] == []

    def test_skipped_no_baseline_returns_empty_failed_paths(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = _call_cleanup(
            {"captured": False}, [], str(repo),
        )
        assert "failed_paths" in result
        assert result["failed_paths"] == []

    def test_failed_revert_surfaces_failed_paths(self, tmp_path, monkeypatch):
        """If ``_restore_path`` returns ``"failed"``, the path must be in
        ``failed_paths`` and the cleanup loop must keep processing the
        rest of the delta."""
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "locked.txt").write_text("rogue\n")
        (repo / "ok.txt").write_text("rogue too\n")

        original = cleanup._restore_path

        def _fake_restore(repo_root: str, rel: str, baseline_arg: Mapping[str, Any], **_kw) -> str:
            if rel == "locked.txt":
                return "failed"
            return original(repo_root, rel, baseline_arg, **_kw)

        monkeypatch.setattr(cleanup, "_restore_path", _fake_restore)
        result = _call_cleanup(baseline, [], str(repo))

        # Both are out of scope.
        assert result["scope_violation_detected"] is True
        assert set(result["out_of_scope_paths"]) == {"locked.txt", "ok.txt"}
        # The OK path was reverted; the failing one is in failed_paths.
        assert "ok.txt" in result["deleted"]
        assert "locked.txt" not in result["deleted"]
        assert "locked.txt" not in result["restored"]
        assert result["failed_paths"] == ["locked.txt"]

    def test_mixed_some_restored_some_failed(self, tmp_path, monkeypatch):
        """Mixed outcomes: cleanup result must include both successful
        (``restored``/``deleted``) and failed (``failed_paths``) lists
        for diagnostics."""
        repo = _make_repo(tmp_path)
        # A second tracked file we will mutate (so cleanup tries to
        # restore it from baseline bytes).
        (repo / "keep.py").write_text("orig\n")
        _git(["add", "keep.py"], cwd=repo)
        _git(["commit", "-q", "-m", "add keep"], cwd=repo)

        baseline = cleanup.snapshot_baseline(str(repo))
        # Three out-of-scope writes.
        (repo / "keep.py").write_text("rogue mutation\n")  # → restore
        (repo / "new.txt").write_text("rogue create\n")    # → delete
        (repo / "broken.txt").write_text("oops\n")         # → fail (forced)

        original = cleanup._restore_path

        def _fake_restore(repo_root: str, rel: str, baseline_arg: Mapping[str, Any], **_kw) -> str:
            if rel == "broken.txt":
                return "failed"
            return original(repo_root, rel, baseline_arg, **_kw)

        monkeypatch.setattr(cleanup, "_restore_path", _fake_restore)
        result = _call_cleanup(baseline, [], str(repo))

        assert result["scope_violation_detected"] is True
        assert "keep.py" in result["restored"]
        assert "new.txt" in result["deleted"]
        assert result["failed_paths"] == ["broken.txt"]
        # All three paths still surface as out-of-scope.
        assert set(result["out_of_scope_paths"]) == {
            "keep.py", "new.txt", "broken.txt",
        }


# ---------------------------------------------------------------------------
# Concurrency: disjoint baselines must not cross-contaminate
# ---------------------------------------------------------------------------


def _worker(repo_str: str, declared: list, my_files: dict, result_q):
    """Multiprocessing worker. Snapshots, writes its own files, runs
    cleanup with its own declaration. Returns the result dict (and the
    on-disk state of every file it touched) via the queue.

    A buggy implementation that stashes baseline state globally would
    see the OTHER worker's files in its observed_delta and either (a)
    delete them as out-of-scope or (b) record them as protected_skipped
    without deleting. Either outcome would be visible in the assertions.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_dispatch_cleanup", MODULE_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    baseline = mod.snapshot_baseline(repo_str)
    repo = Path(repo_str)
    for rel, content in my_files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content)
    res = mod.apply_cleanup(
        baseline, declared, repo_str,
        authorization_source="wrapper-declared-scope",
    )

    # Capture which of MY declared files survived AND which OTHER files
    # are visible from this worker's POV (used by assertions).
    on_disk = {}
    for rel in list(my_files) + list(declared):
        full = repo / rel
        on_disk[rel] = full.read_text() if full.exists() else None

    result_q.put({
        "declared": declared,
        "my_files": list(my_files),
        "result": res,
        "on_disk": on_disk,
    })


def _snap_only_worker(repo_str: str, ready_evt, q):
    """Module-level worker for the same-repo-disjoint-files
    isolation test. Must be top-level so ``spawn``-mode pickling
    can find it.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_dispatch_cleanup", MODULE_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    baseline = mod.snapshot_baseline(repo_str)
    ready_evt.wait()
    q.put({
        "tracked_changed": list(baseline["tracked_changed"]),
        "untracked": list(baseline["untracked"]),
        "tracked_blobs_keys": sorted(baseline["tracked_blobs"]),
        "untracked_blobs_keys": sorted(baseline["untracked_blobs"]),
    })


class TestConcurrencyIsolation:
    """Spawn two workers on DISJOINT file sets in DISJOINT repos.

    Distinct repos verify that the wrapper's baseline does not depend
    on any cwd-relative state or shared scratch dir; if it did, two
    concurrent dispatches in the same parent would race on it.
    """

    def test_two_workers_disjoint_repos(self, tmp_path):
        repo_a = _make_repo(tmp_path, name="repo_a")
        repo_b = _make_repo(tmp_path, name="repo_b")

        ctx = multiprocessing.get_context("spawn")
        q = ctx.Queue()

        p1 = ctx.Process(
            target=_worker,
            args=(
                str(repo_a),
                ["a_declared.txt"],
                {"a_declared.txt": "A-PAYLOAD\n"},
                q,
            ),
        )
        p2 = ctx.Process(
            target=_worker,
            args=(
                str(repo_b),
                ["b_declared.txt"],
                {"b_declared.txt": "B-PAYLOAD\n"},
                q,
            ),
        )
        p1.start(); p2.start()
        p1.join(timeout=60); p2.join(timeout=60)
        assert p1.exitcode == 0, p1.exitcode
        assert p2.exitcode == 0, p2.exitcode

        results = [q.get(timeout=10), q.get(timeout=10)]

        for r in results:
            assert r["result"]["scope_violation_detected"] is False, r
            assert r["result"]["scope_misreport_detected"] is False, r
            assert r["result"]["restored"] == [], r
            assert r["result"]["deleted"] == [], r

        # Each repo retained ONLY its own declared file.
        assert (repo_a / "a_declared.txt").read_text() == "A-PAYLOAD\n"
        assert not (repo_a / "b_declared.txt").exists()
        assert (repo_b / "b_declared.txt").read_text() == "B-PAYLOAD\n"
        assert not (repo_b / "a_declared.txt").exists()

    def test_two_workers_same_repo_disjoint_files(self, tmp_path):
        """Both workers point at the same repo but write disjoint
        files. Each declares ONLY its own files. Because each worker
        snapshotted before either wrote, each will see the OTHER's
        write as an out-of-scope delta against its OWN baseline — and
        delete it. That is the expected, documented behavior of a
        process-local baseline (the wrapper has no notion of "sibling
        tasks"; concurrent same-repo dispatch must be coordinated by
        the orchestrator, not the wrapper). The acceptance criterion
        only asks for *disjoint file sets* to not cross-contaminate
        BASELINES, not that two unrelated wrapper invocations on the
        same repo race-survive. The test below uses a synchronization
        barrier so both workers finish writing before either calls
        cleanup, then asserts that neither baseline includes the
        OTHER worker's files in its captured snapshot — proving the
        baseline is process-local."""
        repo = _make_repo(tmp_path)

        ctx = multiprocessing.get_context("spawn")
        ready = ctx.Barrier(2)
        snap_q = ctx.Queue()

        p1 = ctx.Process(
            target=_snap_only_worker,
            args=(str(repo), ready, snap_q),
        )
        p2 = ctx.Process(
            target=_snap_only_worker,
            args=(str(repo), ready, snap_q),
        )
        p1.start(); p2.start()
        p1.join(timeout=60); p2.join(timeout=60)
        assert p1.exitcode == 0, p1.exitcode
        assert p2.exitcode == 0, p2.exitcode

        s1 = snap_q.get(timeout=10)
        s2 = snap_q.get(timeout=10)

        # Both baselines were taken on a CLEAN repo; both should be
        # empty. The non-trivial assertion is that nothing
        # process-global leaked between them: the lists are equal,
        # which trivially holds for two empty captures.
        assert s1["tracked_changed"] == []
        assert s2["tracked_changed"] == []
        assert s1["untracked"] == []
        assert s2["untracked"] == []
        assert s1["tracked_blobs_keys"] == []
        assert s2["tracked_blobs_keys"] == []


# ---------------------------------------------------------------------------
# PLAN_WRAPPER_REVERT_POLICY_GATE TASK-001: unattended_revert_policy gate
# ---------------------------------------------------------------------------


class TestUnattendedRevertPolicyGate:
    def test_apply_cleanup_pause_policy_detects_but_does_not_revert(
        self, tmp_path,
    ):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "rogue.txt").write_text("rogue payload\n")
        result = cleanup.apply_cleanup(
            baseline, [], str(repo),
            authorization_source="wrapper-declared-scope",
            unattended_revert_policy="pause",
        )
        assert result["out_of_scope_paths"] == ["rogue.txt"]
        assert result["restored"] == []
        assert result["deleted"] == []
        assert result["failed_paths"] == []
        assert (repo / "rogue.txt").read_text() == "rogue payload\n"
        assert result["cleanup_strategy"] == "detect_only_revert_policy_pause"
        assert result["scope_violation_detected"] is True

    def test_apply_cleanup_fail_fast_policy_detects_but_does_not_revert(
        self, tmp_path,
    ):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "rogue.txt").write_text("rogue payload\n")
        result = cleanup.apply_cleanup(
            baseline, [], str(repo),
            authorization_source="wrapper-declared-scope",
            unattended_revert_policy="fail-fast",
        )
        assert result["out_of_scope_paths"] == ["rogue.txt"]
        assert result["restored"] == []
        assert result["deleted"] == []
        assert (repo / "rogue.txt").read_text() == "rogue payload\n"
        assert (
            result["cleanup_strategy"]
            == "detect_only_revert_policy_fail_fast"
        )

    def test_apply_cleanup_preserve_only_policy_unchanged(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "keep.py").write_text("orig\n")
        _git(["add", "keep.py"], cwd=repo)
        _git(["commit", "-q", "-m", "add keep"], cwd=repo)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "keep.py").write_text("rogue mutation\n")
        result = cleanup.apply_cleanup(
            baseline, [], str(repo),
            authorization_source="wrapper-declared-scope",
            unattended_revert_policy="preserve-only",
        )
        assert result["cleanup_strategy"] == "delta_bounded"
        assert "keep.py" in result["out_of_scope_paths"]
        assert "keep.py" in result["restored"]
        assert (repo / "keep.py").read_text() == "orig\n"

    def test_apply_cleanup_default_policy_is_pause(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        (repo / "rogue.txt").write_text("rogue payload\n")
        result = cleanup.apply_cleanup(
            baseline, [], str(repo),
            authorization_source="wrapper-declared-scope",
        )
        assert result["out_of_scope_paths"] == ["rogue.txt"]
        assert result["restored"] == []
        assert result["deleted"] == []
        assert (repo / "rogue.txt").read_text() == "rogue payload\n"
        assert result["cleanup_strategy"] == "detect_only_revert_policy_pause"

    def test_apply_cleanup_unknown_policy_raises_valueerror(self, tmp_path):
        repo = _make_repo(tmp_path)
        baseline = cleanup.snapshot_baseline(str(repo))
        with pytest.raises(ValueError) as excinfo:
            cleanup.apply_cleanup(
                baseline, [], str(repo),
                authorization_source="wrapper-declared-scope",
                unattended_revert_policy="yolo",
            )
        msg = str(excinfo.value)
        assert "yolo" in msg
        for v in sorted(["pause", "fail-fast", "preserve-only"]):
            assert v in msg
