#!/usr/bin/env python3
"""Bounded capture of subprocess stdout+stderr with framework-aware summary.

Usage:
    $PYTHON scripts/log_capture.py [--head-lines N] [--tail-lines N]
        [--max-bytes N]
        [--framework auto|pytest|unittest|go|jest|none]
        [--run-id RID] [--task-id TID]
        [--log-dir-root PATH]
        -- <cmd> [args...]

Writes a bounded, UUID-delimited summary to stdout; writes the full
unbounded log to ``<log-dir-root>/<run_id>/<task_id>.log`` when both
run_id and task_id are provided; exits with the subprocess's exit code.

The summary is bracketed by ``<<<LOG-BLOCK {uuid}>>>`` /
``<<<END LOG-BLOCK {uuid}>>>`` fences so downstream parsers
(``plan_ops.py parse-implementer-report``) can sandbox the content
against prompt-injection by embedded test output.

See ``docs/plans/DUAL_AGENT_Plans/TASK-011_bounded_log_handling.md``
for the full design contract.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Sequence

DEFAULT_LOG_DIR_ROOT = Path("docs/plans/_run_logs")

LOG_BLOCK_OPEN = "<<<LOG-BLOCK {uuid}>>>"
LOG_BLOCK_CLOSE = "<<<END LOG-BLOCK {uuid}>>>"


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="log_capture.py",
        description=(
            "Run a command and emit a bounded, UUID-delimited summary of "
            "its combined stdout+stderr to this process's stdout. The full "
            "unbounded log is written to a side file when --run-id and "
            "--task-id are supplied."
        ),
    )
    parser.add_argument("--head-lines", type=int, default=20)
    parser.add_argument("--tail-lines", type=int, default=100)
    parser.add_argument("--max-bytes", type=int, default=32000)
    parser.add_argument(
        "--framework",
        default="auto",
        choices=["auto", "pytest", "unittest", "go", "jest", "none"],
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--task-id", default=None)
    parser.add_argument(
        "--log-dir-root",
        default=str(DEFAULT_LOG_DIR_ROOT),
        help=(
            "Root directory for full-log side files. Defaults to "
            "``docs/plans/_run_logs``."
        ),
    )
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args(list(argv))
    if not args.cmd or args.cmd[0] != "--":
        parser.error("expected '--' before the command")
    args.cmd = args.cmd[1:]
    if not args.cmd:
        parser.error("no command supplied after '--'")
    return args


def _detect_framework(cmd: Sequence[str], text: str) -> str:
    joined = " ".join(cmd)
    if "pytest" in joined:
        return "pytest"
    if "unittest" in joined:
        return "unittest"
    if re.search(r"(^|\s)go\s+test(\s|$)", joined):
        return "go"
    if "jest" in joined:
        return "jest"
    return "none"


_PYTEST_FAILURES_RE = re.compile(
    r"=+\s+FAILURES\s+=+.*?(?=\n=+\s+\d+ (?:failed|passed|error))",
    re.DOTALL,
)
_PYTEST_SHORT_SUMMARY_RE = re.compile(
    r"=+\s+short test summary info\s+=+.*?(?=\n=+|\Z)",
    re.DOTALL,
)


def _extract_framework_block(framework: str, text: str) -> str | None:
    """Return a best-effort framework summary, capped to ~8 KiB.

    pytest is the P0 path; unittest/go/jest are best-effort and return
    None when no obvious marker is found (callers fall back to plain
    head+tail).
    """
    if framework == "pytest":
        m = _PYTEST_FAILURES_RE.search(text)
        short = _PYTEST_SHORT_SUMMARY_RE.search(text)
        chunks: list[str] = []
        if m:
            chunks.append(m.group(0))
        if short:
            chunks.append(short.group(0))
        if chunks:
            return ("\n".join(chunks))[:8000]
        # Still surface terse summary line (`3 failed, 2 passed in ...`) if present.
        tail_summary = re.search(
            r"=+\s+(?:\d+ failed[^\n]*|\d+ error[^\n]*)\s+=+",
            text,
        )
        if tail_summary:
            return tail_summary.group(0)
        return None
    if framework == "unittest":
        m = re.search(r"^FAIL:.*?(?=\n----+\n|\Z)", text, re.DOTALL | re.MULTILINE)
        if m:
            return m.group(0)[:8000]
        return None
    if framework == "go":
        m = re.search(r"--- FAIL: .*?(?=\nFAIL\s|\nok\s|\Z)", text, re.DOTALL)
        if m:
            return m.group(0)[:8000]
        return None
    if framework == "jest":
        m = re.search(r"FAIL\s+.*?(?=\nTest Suites:|\Z)", text, re.DOTALL)
        if m:
            return m.group(0)[:8000]
        return None
    return None


def _render(
    head: list[str],
    tail: list[str],
    framework_block: str | None,
    *,
    exit_code: int,
    elapsed: float,
    byte_count: int,
    line_count: int,
    full_path: str | None,
    summary_uuid: str,
    truncated: bool,
    complete: bool,
) -> str:
    header_flags: list[str] = []
    if truncated:
        header_flags.append("truncated=true")
    if complete:
        header_flags.append("complete=true")
    header = (
        f"exit_code={exit_code} "
        f"elapsed_s={elapsed:.2f} "
        f"bytes={byte_count} "
        f"lines={line_count} "
        f"full_log={full_path or 'not_persisted'} "
        f"uuid={summary_uuid}"
    )
    if header_flags:
        header = header + " " + " ".join(header_flags)
    parts: list[str] = [LOG_BLOCK_OPEN.format(uuid=summary_uuid), header]
    if complete:
        parts.append("--- full ---")
        parts.extend(head)  # head in this path contains the whole log
    else:
        parts.append("--- head ---")
        parts.extend(head)
        parts.append("--- tail ---" if not truncated else "--- tail (truncated) ---")
        parts.extend(tail)
    if framework_block:
        parts.append("--- framework summary ---")
        parts.append(framework_block)
    parts.append(LOG_BLOCK_CLOSE.format(uuid=summary_uuid))
    return "\n".join(parts) + "\n"


def _compose_bounded(
    head: list[str],
    tail: list[str],
    framework_block: str | None,
    *,
    exit_code: int,
    elapsed: float,
    byte_count: int,
    line_count: int,
    full_path: str | None,
    max_bytes: int,
    summary_uuid: str,
    complete: bool,
) -> str:
    payload = _render(
        head, tail, framework_block,
        exit_code=exit_code, elapsed=elapsed,
        byte_count=byte_count, line_count=line_count,
        full_path=full_path, summary_uuid=summary_uuid,
        truncated=False, complete=complete,
    )
    if len(payload.encode("utf-8")) <= max_bytes:
        return payload

    # Over cap: tail is more important than head for failures, but we
    # still prefer a non-empty head. Trim tail first, then head, then
    # drop the framework block, each time re-rendering and re-checking.
    trimmed_tail = list(tail)
    while trimmed_tail:
        trimmed_tail = trimmed_tail[1:]
        payload = _render(
            head, trimmed_tail, framework_block,
            exit_code=exit_code, elapsed=elapsed,
            byte_count=byte_count, line_count=line_count,
            full_path=full_path, summary_uuid=summary_uuid,
            truncated=True, complete=False,
        )
        if len(payload.encode("utf-8")) <= max_bytes:
            return payload

    trimmed_head = list(head)
    while trimmed_head:
        trimmed_head = trimmed_head[1:]
        payload = _render(
            trimmed_head, [], framework_block,
            exit_code=exit_code, elapsed=elapsed,
            byte_count=byte_count, line_count=line_count,
            full_path=full_path, summary_uuid=summary_uuid,
            truncated=True, complete=False,
        )
        if len(payload.encode("utf-8")) <= max_bytes:
            return payload

    # Last resort: drop framework block + head/tail. Header alone.
    payload = _render(
        [], [], None,
        exit_code=exit_code, elapsed=elapsed,
        byte_count=byte_count, line_count=line_count,
        full_path=full_path, summary_uuid=summary_uuid,
        truncated=True, complete=False,
    )
    return payload


def run_and_summarize(
    cmd: Sequence[str],
    *,
    head_lines: int = 20,
    tail_lines: int = 100,
    max_bytes: int = 32000,
    framework: str = "auto",
    run_id: str | None = None,
    task_id: str | None = None,
    log_dir_root: Path | str = DEFAULT_LOG_DIR_ROOT,
) -> tuple[int, str, str | None]:
    """Execute ``cmd`` and return ``(exit_code, summary_text, full_log_path)``.

    ``full_log_path`` is the str path of the persisted full log or
    ``None`` if persistence was skipped (missing run_id / task_id).
    """
    start = time.time()
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    chunks: list[bytes] = []
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(4096)
        if not chunk:
            break
        chunks.append(chunk)
    exit_code = proc.wait()
    elapsed = time.time() - start
    full_bytes = b"".join(chunks)
    full_text = full_bytes.decode("utf-8", errors="replace")
    lines = full_text.splitlines()

    full_path: str | None = None
    if run_id and task_id:
        root = Path(log_dir_root)
        dest = root / run_id / f"{task_id}.log"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(full_text, encoding="utf-8")
        full_path = str(dest)

    if framework == "auto":
        framework = _detect_framework(cmd, full_text)

    # If the log is shorter than head+tail, emit the whole thing with a
    # ``complete=true`` annotation.
    if len(lines) <= head_lines + tail_lines:
        head = lines
        tail: list[str] = []
        complete = True
    else:
        head = lines[:head_lines]
        tail = lines[-tail_lines:] if tail_lines > 0 else []
        complete = False

    framework_block = _extract_framework_block(framework, full_text)
    summary_uuid = uuid.uuid4().hex[:8]
    payload = _compose_bounded(
        head, tail, framework_block,
        exit_code=exit_code, elapsed=elapsed,
        byte_count=len(full_bytes), line_count=len(lines),
        full_path=full_path, max_bytes=max_bytes,
        summary_uuid=summary_uuid, complete=complete,
    )
    return exit_code, payload, full_path


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    args = _parse_args(argv)
    exit_code, payload, _ = run_and_summarize(
        args.cmd,
        head_lines=args.head_lines,
        tail_lines=args.tail_lines,
        max_bytes=args.max_bytes,
        framework=args.framework,
        run_id=args.run_id,
        task_id=args.task_id,
        log_dir_root=args.log_dir_root,
    )
    sys.stdout.write(payload)
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
