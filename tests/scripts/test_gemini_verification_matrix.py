"""TASK-001 shim test: validate `scripts/gemini_verification_matrix.sh`
report shape against a hermetic `gemini` stub injected on `$PATH`.

The matrix script's correctness against a real Gemini CLI is covered by the
committed `_verification_report.md` artifact; this test guards the report
*shape* — row count, row labels, and the verdict vocabulary. We do NOT
assert specific verdicts, since those depend on the stub's behavior across
rows and on the script's internal verification heuristics. The contract
this test pins is: "every row in the matrix is emitted, every row carries
a structural verdict, and the script exits 0 even when downstream rows are
unverified".

The fake `gemini` stub is a tiny Python script written into a tempdir. The
test prepends that tempdir to `$PATH` so the matrix script picks it up
ahead of any real `gemini` binary the host may have installed.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gemini_verification_matrix.sh"

# Row labels mirror the matrix table in
# `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/TASK-001_verification_matrix.md`.
EXPECTED_ROWS = [
    (1, "JSON envelope shape"),
    (2, "Empty-prompt exit code"),
    (3, "Turn-limit exit code"),
    (4, "Stdin context piping"),
    (5, "GEMINI_CLI_HOME isolation"),
    (6, "Policy deny enforcement"),
    (7, "--approval-mode plan read-only"),
    (8, "GEMINI_API_KEY absent failure mode"),
    (9, "stats.tokens shape"),
    (10, "Markdown-fenced JSON in response"),
]

ALLOWED_VERDICTS = {"pass", "fail", "unverified"}


def _write_gemini_stub(stub_dir: Path) -> Path:
    """Drop a minimal gemini stub at `<stub_dir>/gemini` and return path.

    The stub recognizes the matrix's argv shapes:

      * `--version` → echoes a fixed version string and exits 0.
      * `-p "" -o json` → exits 42 (empty-prompt contract).
      * Any `-p <non-empty> -o json` → emits a canonical JSON envelope on
        stdout matching row 1's shape, with a `stats` key whose shape
        mirrors the documented ``stats[<model>].tokens.{input,prompt,total,cached}``.

    The stub also reads stdin when present and includes the first line in
    the synthesized `response` so row 4 (stdin piping) verifies its
    contract.
    """
    stub = stub_dir / "gemini"
    interpreter = sys.executable
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!{interpreter}
            import json
            import select
            import sys

            argv = sys.argv[1:]
            if "--version" in argv or "-v" in argv:
                print("0.0.0-stub")
                sys.exit(0)

            # Extract -p <prompt> and -o json
            prompt = None
            i = 0
            while i < len(argv):
                a = argv[i]
                if a in ("-p", "--prompt") and i + 1 < len(argv):
                    prompt = argv[i + 1]
                    i += 2
                    continue
                i += 1

            # Read non-blocking stdin if any has been piped.
            stdin_data = ""
            try:
                if not sys.stdin.isatty():
                    stdin_data = sys.stdin.read()
            except Exception:
                stdin_data = ""

            if prompt == "":
                # Empty-prompt exit code per Gemini docs.
                sys.exit(42)

            # Build a canonical envelope. Include any piped stdin so the
            # row 4 oracle (substring search for "FILE_CONTEXT") passes.
            response_text = (prompt or "")
            if stdin_data:
                response_text = stdin_data.strip() + " " + response_text

            envelope = {{
                "response": response_text,
                "stats": {{
                    "gemini-2.5-pro": {{
                        "tokens": {{
                            "input": 1,
                            "prompt": 1,
                            "total": 2,
                            "cached": 0,
                        }}
                    }}
                }},
            }}
            print(json.dumps(envelope))
            sys.exit(0)
            """
        )
    )
    stub.chmod(0o755)
    return stub


def _parse_rows(report_text: str) -> list[dict]:
    """Parse the matrix report into per-row dicts: number, title, verdict."""
    rows: list[dict] = []
    # Row header pattern: `## Row 1 — JSON envelope shape`
    header_re = re.compile(r"^## Row (\d+)\s*[—-]\s*(.+?)\s*$", re.MULTILINE)
    verdict_re = re.compile(r"^- \*\*Verdict:\*\* `([^`]+)`", re.MULTILINE)

    headers = [(m.start(), m) for m in header_re.finditer(report_text)]
    for idx, (start, m) in enumerate(headers):
        end = headers[idx + 1][0] if idx + 1 < len(headers) else len(report_text)
        section = report_text[start:end]
        v = verdict_re.search(section)
        rows.append(
            {
                "number": int(m.group(1)),
                "title": m.group(2).strip(),
                "verdict": v.group(1).strip() if v else None,
            }
        )
    return rows


def test_script_exists_and_executable():
    assert SCRIPT_PATH.is_file(), f"matrix script missing at {SCRIPT_PATH}"
    assert os.access(SCRIPT_PATH, os.X_OK), (
        f"matrix script must be executable; chmod +x {SCRIPT_PATH}"
    )


def test_matrix_emits_all_rows_with_structural_verdicts(tmp_path):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _write_gemini_stub(stub_dir)
    report_path = tmp_path / "report.md"

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [str(SCRIPT_PATH), "--report", str(report_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, (
        "matrix script must exit 0 even when individual rows are unverified; "
        f"rc={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert report_path.is_file(), "report not written at requested path"

    text = report_path.read_text()
    rows = _parse_rows(text)

    # Row count: exactly 10 rows in the matrix.
    assert len(rows) == len(EXPECTED_ROWS), (
        f"expected {len(EXPECTED_ROWS)} rows, got {len(rows)}: "
        f"{[(r['number'], r['title']) for r in rows]}"
    )

    # Row labels: numbers and titles match the matrix table.
    for actual, expected in zip(rows, EXPECTED_ROWS):
        exp_num, exp_title = expected
        assert actual["number"] == exp_num, (
            f"row order drift: expected #{exp_num}, got #{actual['number']}"
        )
        assert exp_title in actual["title"], (
            f"row {exp_num} title mismatch: expected substring "
            f"'{exp_title}', got '{actual['title']}'"
        )

    # Verdict vocabulary: every row carries one of {pass, fail, unverified}.
    for row in rows:
        assert row["verdict"] in ALLOWED_VERDICTS, (
            f"row {row['number']} '{row['title']}' has non-structural "
            f"verdict {row['verdict']!r}; expected one of {sorted(ALLOWED_VERDICTS)}"
        )


def test_report_contains_required_sections_per_row(tmp_path):
    """Every row carries: command, exit code, stdout, stderr, verdict, rationale."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    _write_gemini_stub(stub_dir)
    report_path = tmp_path / "report.md"

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"

    result = subprocess.run(
        [str(SCRIPT_PATH), "--report", str(report_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    text = report_path.read_text()
    # Per-row presence checks rely on the literal labels emitted by
    # `emit_row` in the script.
    for row_num, _ in EXPECTED_ROWS:
        header = f"## Row {row_num} —"
        assert header in text, f"missing section header for row {row_num}"
        # Section spans from this header until the next or EOF.
        start = text.index(header)
        next_match = re.search(r"^## Row \d+", text[start + 1 :], re.MULTILINE)
        end = (start + 1 + next_match.start()) if next_match else len(text)
        section = text[start:end]
        for required in (
            "**Command:**",
            "**Exit code:**",
            "**Verdict:**",
            "**Rationale:**",
            "### stdout",
            "### stderr",
        ):
            assert required in section, (
                f"row {row_num} missing required label '{required}'"
            )


def test_matrix_default_report_path_documented(tmp_path):
    """The script's `--help` (or error on bad args) names the script.

    Secondary check: passing `--report` without a value must exit non-zero
    so callers can't silently drop the flag.
    """
    result = subprocess.run(
        [str(SCRIPT_PATH), "--report"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0, "missing --report value should error"
    assert "--report" in result.stderr or "--report" in result.stdout
