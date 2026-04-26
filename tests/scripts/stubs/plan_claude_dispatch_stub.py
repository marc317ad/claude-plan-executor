#!/usr/bin/env python3
"""Stub replacement for ``plan_claude_dispatch.py`` (TASK-002).

Selected via env var ``PLAN_CLAUDE_DISPATCH_STUB_FIXTURE=<path>``: when set,
this script behaves as a drop-in CLI substitute for the real wrapper —
it reads the fixture envelope from the given path, optionally records
the invocation (argv + stdin payload) to
``$PLAN_CLAUDE_DISPATCH_STUB_RECORD_PATH`` as a single JSON line, and
emits the envelope on stdout. Exit code is mapped from
``envelope["status"]`` exactly the way the real wrapper does
(``ok`` -> 0, anything else -> 1).

If ``PLAN_CLAUDE_DISPATCH_STUB_FIXTURE`` is unset, the stub falls
through to the real wrapper at
``plugins/plan-executor/scripts/plan_claude_dispatch.py`` via
``os.execvp``, replacing the current process so the real wrapper sees
the original argv, stdin, and environment unchanged. The delegation
target can be overridden for testing via
``PLAN_CLAUDE_DISPATCH_STUB_REAL_WRAPPER=<path>``; when that env var is
set, the stub execs that path instead of the shipped wrapper.

Errors:
  * fixture path does not exist     -> exit 2, ``stub: fixture-not-found``
  * fixture is not valid JSON       -> exit 2, ``stub: malformed-fixture``
  * fixture missing ``status`` key  -> exit 2, ``stub: malformed-fixture``

The stub deliberately does NOT validate the fixture against the §7
output schema; the test harness in ``test_claude_dispatch_stub.py``
performs that validation. This keeps the stub minimal and lets the
tests assert on schema-validation failures explicitly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict


_FIXTURE_ENV = "PLAN_CLAUDE_DISPATCH_STUB_FIXTURE"
_RECORD_ENV = "PLAN_CLAUDE_DISPATCH_STUB_RECORD_PATH"
_REAL_WRAPPER_ENV = "PLAN_CLAUDE_DISPATCH_STUB_REAL_WRAPPER"
_DEFAULT_REAL_WRAPPER = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "plan-executor"
    / "scripts"
    / "plan_claude_dispatch.py"
)


def _record(argv: list, stdin_text: str) -> None:
    record_path = os.environ.get(_RECORD_ENV)
    if not record_path:
        return
    entry: Dict[str, Any] = {"argv": list(argv), "stdin": stdin_text}
    line = json.dumps(entry, sort_keys=True) + "\n"
    # Append-only; one JSON line per invocation.
    with open(record_path, "a", encoding="utf-8") as fh:
        fh.write(line)


def _read_stdin_if_piped() -> str:
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _delegate_to_real_wrapper(argv: list) -> int:
    """Replace the current process with the real wrapper.

    Honors ``PLAN_CLAUDE_DISPATCH_STUB_REAL_WRAPPER`` for test override;
    otherwise execs the shipped wrapper. argv[0] is replaced with the
    target path, but argv[1:] and the inherited stdin/env are preserved
    so the real wrapper sees the original invocation unchanged.
    """
    target = os.environ.get(_REAL_WRAPPER_ENV) or str(_DEFAULT_REAL_WRAPPER)
    target_path = Path(target)
    if not target_path.exists():
        sys.stderr.write(
            f"stub: real-wrapper-not-found: {target}\n"
        )
        return 2
    new_argv = [str(target_path), *list(argv[1:])]
    # If the target is a Python script, exec it via the current
    # interpreter so we don't rely on a shebang being executable.
    if target_path.suffix == ".py":
        os.execvp(sys.executable, [sys.executable, *new_argv])
    else:
        os.execvp(str(target_path), new_argv)
    # execvp does not return on success; this line is unreachable.
    return 127


def main(argv: list) -> int:
    fixture_path = os.environ.get(_FIXTURE_ENV)
    if not fixture_path:
        return _delegate_to_real_wrapper(argv)

    stdin_text = _read_stdin_if_piped()
    _record(argv, stdin_text)

    p = Path(fixture_path)
    if not p.exists():
        sys.stderr.write(f"stub: fixture-not-found: {fixture_path}\n")
        return 2

    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"stub: fixture-read-error: {exc}\n")
        return 2

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"stub: malformed-fixture: {exc}\n")
        return 2

    if not isinstance(envelope, dict) or "status" not in envelope:
        sys.stderr.write("stub: malformed-fixture: missing 'status' key\n")
        return 2

    sys.stdout.write(json.dumps(envelope, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()

    return 0 if envelope.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
