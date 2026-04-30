"""Shared pure-core conformance harness for ``plan_ops`` CLI subcommands.

TASK-003H: this driver is the single source of truth for the byte-equal
comparison the Tier-A/B/C fixture suites (TASK-004/005/006) run between
each subcommand's CLI subprocess output and its in-process pure-core
``_run_<subcommand>(payload)`` result. Without this shared driver, the
three fixture suites would each carry their own copy of the subprocess
plumbing, JSON canonicalization, and side-effect snapshotting — which is
exactly the silent-divergence failure mode the conformance gate exists
to catch.

Contract (per ``run_conformance``)
==================================

The harness asserts, in this order, AS SEPARATE assertions, BEFORE any
canonicalization step:

1. Subprocess exit code matches ``expected["exit_code"]``.
2. Pure-core ``__plan_ops_exit_code__`` marker matches ``expected["exit_code"]``.
3. Subprocess stderr satisfies ``expected["stderr_policy"]``
   (``"empty"`` | ``"nonempty"`` | ``"exact"``).
4. Subprocess stdout is parseable JSON (or matches ``expected["stdout_text"]``
   verbatim when ``expected["text_output"]`` is set, for ``cmd_audit`` and the
   ``cmd_build_*_dispatch_input`` family that bypass the JSON envelope).

Only after all four assertions pass does the harness canonicalize both
envelopes (sorted keys, ``json.dumps`` indent=2 normalized whitespace,
ISO-8601 timestamp values replaced with a sentinel) and assert byte-equal
canonical form. If ``expected["stdout_envelope"]`` is supplied, it too is
canonicalized and compared.

Per-fixture knobs
=================

``payload`` (in-process input to ``_run_<subcommand>``):
    ``payload["stdin_text"]`` is forwarded as subprocess stdin (Tier-A's
    common stdin contract). ``payload["_files"]`` is a relpath -> text map
    of temp files materialized under ``fixture_dir`` before either path
    runs; the key is harness-internal and is popped before
    ``_run_<subcommand>`` is invoked.

``expected``:
    ``cli_argv`` (list[str], optional) — argv after the subcommand name.
        OPTIONAL override. When omitted, the harness derives subprocess argv
        from the payload via ``cli_argv_from(subcommand, payload)`` so fixture
        cases do not have to duplicate state already encoded in the payload.
        Supply explicitly only when a fixture deliberately needs the CLI and
        in-process payloads to diverge (e.g., divergence-detection self-tests).
    ``exit_code`` (int) — required.
    ``stderr_policy`` ("empty"|"nonempty"|"exact") — default "empty".
    ``stderr_text`` (str) — required when stderr_policy == "exact".
    ``stdout_envelope`` (dict | None) — optional canonicalized comparison.
    ``text_output`` (bool) — when True, treat stdout as opaque text and
        compare against ``expected["stdout_text"]`` instead of JSON-decoding.
    ``stdout_text`` (str) — text to match when ``text_output`` is True.
    ``fs_invariant`` (bool) — when True, snapshot ``fixture_dir`` before
        and after both paths and assert no diff (Tier-A read-only invariant).
    ``fixed_now`` (str | None) — pinned timestamp injected via
        ``PLAN_OPS_FIXED_NOW`` env var (subprocess) and a monkey-patch of
        ``plan_ops._now`` (in-process).

Self-tests in ``test_plan_ops_pure_harness.py`` exercise the byte-equal
happy path, deliberate stdin-text divergence detection, exit-code mismatch
detection, JSON canonicalization equivalence (re-ordered keys equal), and
filesystem-snapshot diff detection.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "plugins" / "plan-executor" / "scripts"
SCRIPT = SCRIPTS_DIR / "plan_ops.py"

# Match ISO-8601 timestamps with optional fractional seconds and a Z or
# +HH:MM offset. Used to replace embedded clock readings before canonical
# comparison so `_now()` drift between the subprocess and in-process paths
# does not register as a fake divergence.
_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)
_TIMESTAMP_SENTINEL = "<TIMESTAMP>"


def load_plan_ops():
    """Import ``plan_ops`` once and cache the module on ``sys.modules``.

    The script lives outside any installable package, so we extend
    ``sys.path`` to include its directory and let the standard import
    machinery handle caching. Repeated calls return the same module
    object; that means a monkey-patch of ``plan_ops._now`` made by one
    fixture is observable across the test session unless reverted.
    ``run_conformance`` always reverts.
    """
    scripts_dir = str(SCRIPTS_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    return importlib.import_module("plan_ops")


def _subcommand_to_ident(subcommand: str) -> str:
    return subcommand.replace("-", "_")


def _materialize_files(fixture_dir: Path, files: dict[str, str]) -> None:
    for relpath, content in files.items():
        target = fixture_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _snapshot_dir(root: Path) -> dict[str, str]:
    """Capture every file under ``root`` keyed by relative path.

    Binary files are recorded as the sentinel ``"<binary>"`` so the diff
    assertion stays human-readable. Used for the Tier-A read-only
    invariant: callers snapshot before both subprocess + in-process runs
    and assert post-snapshot equality.
    """
    snap: dict[str, str] = {}
    if not root.exists():
        return snap
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                snap[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                snap[str(path.relative_to(root))] = "<binary>"
    return snap


_HARNESS_ONLY_PAYLOAD_KEYS = {
    "_files",
    "stdin_text",
    "schedule_text",
    "report_text",
}


def _preferred_option(action: Any) -> str | None:
    options = [opt for opt in action.option_strings if opt.startswith("--")]
    if options:
        return max(options, key=len)
    return action.option_strings[0] if action.option_strings else None


def cli_argv_from(subcommand: str, payload: dict) -> list[str]:
    """Derive subprocess argv after the subcommand from the pure-core payload.

    The derivation is argparse-driven: the real ``plan_ops`` parser is used
    as the command-specific source of truth, and the resulting argv is later
    validated through ``_args_to_payload_<subcommand>`` when that builder
    exists. Fixture suites therefore encode payload state once and inherit
    each command's CLI contract from the production parser.
    """
    plan_ops = load_plan_ops()
    parser = plan_ops.build_parser()
    subparser = None
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if choices and subcommand in choices:
            subparser = action.choices[subcommand]
            break
    if subparser is None:
        raise KeyError(f"no argparse subcommand registered for {subcommand!r}")

    consumed: set[str] = set()
    argv: list[str] = []
    for action in subparser._actions:
        dest = getattr(action, "dest", None)
        if not dest or dest == "help" or dest in _HARNESS_ONLY_PAYLOAD_KEYS:
            continue

        if not action.option_strings:
            if dest in payload:
                value = payload[dest]
                consumed.add(dest)
                if isinstance(value, (list, tuple)):
                    argv.extend(str(item) for item in value)
                elif value is not None:
                    argv.append(str(value))
            continue

        option = _preferred_option(action)
        if option is None:
            continue

        if dest == "json":
            if payload.get("json", True):
                argv.append(option)
            consumed.add(dest)
            continue

        if dest not in payload:
            continue

        value = payload[dest]
        consumed.add(dest)
        if action.__class__.__name__ == "_StoreTrueAction":
            if value:
                argv.append(option)
        elif action.__class__.__name__ == "_StoreFalseAction":
            if value is False:
                argv.append(option)
        elif isinstance(value, (list, tuple)):
            for item in value:
                argv.extend([option, str(item)])
        elif value is not None:
            argv.extend([option, str(value)])

    unknown = sorted(
        key for key in payload
        if key not in consumed and key not in _HARNESS_ONLY_PAYLOAD_KEYS
    )
    if unknown:
        raise KeyError(
            f"payload keys cannot be derived for {subcommand!r}: {unknown}"
        )
    return argv


def _payload_from_cli_argv(
    subcommand: str,
    cli_argv: list[str],
    *,
    stdin_text: str,
) -> dict | None:
    plan_ops = load_plan_ops()
    parser = plan_ops.build_parser()
    args = parser.parse_args([subcommand, *cli_argv])
    builder = getattr(
        plan_ops,
        f"_args_to_payload_{_subcommand_to_ident(subcommand)}",
        None,
    )
    if builder is None:
        return None
    saved_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(stdin_text)
        return builder(args)
    finally:
        sys.stdin = saved_stdin


def _assert_builder_payload_consistent(
    subcommand: str,
    cli_argv: list[str],
    pure_payload: dict,
    *,
    stdin_text: str,
) -> None:
    built_payload = _payload_from_cli_argv(
        subcommand, cli_argv, stdin_text=stdin_text,
    )
    if built_payload is None:
        return
    comparable_keys = (
        set(built_payload)
        & set(pure_payload)
        - _HARNESS_ONLY_PAYLOAD_KEYS
    )
    mismatches = {
        key: (built_payload[key], pure_payload[key])
        for key in sorted(comparable_keys)
        if built_payload[key] != pure_payload[key]
    }
    assert not mismatches, (
        f"_args_to_payload builder mismatch for {subcommand!r}: {mismatches}"
    )


def canonicalize_envelope(envelope: Any) -> str:
    """Canonical form for byte-equal comparison: sorted keys + ts stub."""
    serialized = json.dumps(envelope, sort_keys=True, indent=2)
    return _TIMESTAMP_RE.sub(_TIMESTAMP_SENTINEL, serialized)


def _pure_text_stdout(result: dict, public_envelope: dict) -> str:
    marker = result.get("__plan_ops_text_output__")
    if marker is not None:
        assert isinstance(marker, str), (
            "pure-core text output marker '__plan_ops_text_output__' "
            "must be a string"
        )
        return marker
    if "error" in public_envelope:
        return ""
    return "".join(f"{key}: {value}\n" for key, value in public_envelope.items())


def run_conformance(
    subcommand: str,
    payload: dict,
    *,
    expected: dict,
    fixture_dir: Path,
) -> None:
    """Execute the byte-equal CLI vs pure-core comparison for ``subcommand``.

    See module docstring for the contract and supported ``expected`` keys.
    Raises ``AssertionError`` on the first divergence; the message names
    which assertion failed (exit-code, stderr, JSON parseability,
    canonical envelope, filesystem snapshot, or expected-envelope match).
    """
    fixture_dir = Path(fixture_dir)
    fixture_dir.mkdir(parents=True, exist_ok=True)

    pure_payload = dict(payload)
    files = pure_payload.pop("_files", None)
    if files:
        _materialize_files(fixture_dir, files)

    explicit_cli_argv = "cli_argv" in expected
    if explicit_cli_argv:
        cli_argv = list(expected["cli_argv"])
    else:
        cli_argv = cli_argv_from(subcommand, pure_payload)
    stdin_text = pure_payload.get("stdin_text", "") or ""
    if not explicit_cli_argv:
        _assert_builder_payload_consistent(
            subcommand, cli_argv, pure_payload, stdin_text=stdin_text,
        )

    fs_invariant = bool(expected.get("fs_invariant", False))
    pre_snapshot = _snapshot_dir(fixture_dir) if fs_invariant else None

    proc_env = os.environ.copy()
    fixed_now = expected.get("fixed_now")
    if fixed_now:
        proc_env["PLAN_OPS_FIXED_NOW"] = fixed_now

    proc = subprocess.run(
        [sys.executable, "plan_ops.py", subcommand, *cli_argv],
        input=stdin_text,
        cwd=str(SCRIPTS_DIR),
        env=proc_env,
        capture_output=True,
        text=True,
    )

    plan_ops = load_plan_ops()
    run_fn_name = f"_run_{_subcommand_to_ident(subcommand)}"
    try:
        run_fn = getattr(plan_ops, run_fn_name)
    except AttributeError as exc:
        raise AssertionError(
            f"plan_ops has no pure-core entry point {run_fn_name!r} for "
            f"subcommand {subcommand!r}"
        ) from exc

    saved_now = getattr(plan_ops, "_now", None)
    try:
        if fixed_now:
            plan_ops._now = lambda: fixed_now  # type: ignore[attr-defined]
        pure_result_raw = run_fn(pure_payload)
    finally:
        if fixed_now and saved_now is not None:
            plan_ops._now = saved_now  # type: ignore[attr-defined]

    pure_exit = pure_result_raw.get("__plan_ops_exit_code__", 0)
    pure_envelope = plan_ops._public_result(pure_result_raw)

    if fs_invariant:
        post_snapshot = _snapshot_dir(fixture_dir)
        assert pre_snapshot == post_snapshot, (
            f"filesystem snapshot mismatch under {fixture_dir!s}\n"
            f"  pre keys:  {sorted((pre_snapshot or {}).keys())}\n"
            f"  post keys: {sorted(post_snapshot.keys())}"
        )

    expected_exit = int(expected["exit_code"])
    assert proc.returncode == expected_exit, (
        f"subprocess exit_code mismatch: expected {expected_exit}, "
        f"got {proc.returncode}; stderr={proc.stderr!r}"
    )
    assert int(pure_exit) == expected_exit, (
        f"pure-core exit_code mismatch: expected {expected_exit}, "
        f"got {pure_exit}"
    )

    stderr_policy = expected.get("stderr_policy", "empty")
    if stderr_policy == "empty":
        assert proc.stderr == "", (
            f"expected empty stderr; got {proc.stderr!r}"
        )
    elif stderr_policy == "nonempty":
        assert proc.stderr.strip() != "", (
            "expected non-empty stderr; got empty"
        )
    elif stderr_policy == "exact":
        assert proc.stderr == expected.get("stderr_text", ""), (
            f"stderr text mismatch: expected "
            f"{expected.get('stderr_text', '')!r}, got {proc.stderr!r}"
        )
    else:
        raise ValueError(f"unknown stderr_policy {stderr_policy!r}")

    if expected.get("text_output"):
        expected_text = expected.get("stdout_text", "")
        assert proc.stdout == expected_text, (
            f"stdout text mismatch: expected {expected_text!r}, "
            f"got {proc.stdout!r}"
        )
        pure_text = _pure_text_stdout(pure_result_raw, pure_envelope)
        assert pure_text == proc.stdout, (
            "text output mismatch between CLI and pure-core paths\n"
            f"--- cli ---\n{proc.stdout!r}\n--- pure ---\n{pure_text!r}\n"
        )
        return

    try:
        cli_envelope = json.loads(proc.stdout) if proc.stdout else None
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"subprocess stdout is not parseable JSON: {exc}; "
            f"raw stdout={proc.stdout!r}"
        ) from exc

    cli_canonical = canonicalize_envelope(cli_envelope)
    pure_canonical = canonicalize_envelope(pure_envelope)
    assert cli_canonical == pure_canonical, (
        "canonical envelope mismatch between CLI and pure-core paths\n"
        f"--- cli ---\n{cli_canonical}\n--- pure ---\n{pure_canonical}\n"
    )

    expected_envelope = expected.get("stdout_envelope")
    if expected_envelope is not None:
        expected_canonical = canonicalize_envelope(expected_envelope)
        assert cli_canonical == expected_canonical, (
            "canonical envelope mismatch against expected\n"
            f"--- got ---\n{cli_canonical}\n"
            f"--- want ---\n{expected_canonical}\n"
        )
