"""Regression checks for the Phase D-Codex reviewer prompt calibration.

V3 — asserts the dispatch-templates.md Phase D-Codex section contains the
verdict decision ladder and both worked examples. V4 — asserts the checked-in
Codex review envelope fixture conforms structurally to the reviewer schema
(required keys, verdict enum, finding shape), so prompt tuning does not drift
from the wrapper's envelope contract.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "dispatch-templates.md"
SCHEMA = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "codex_review_schema.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "codex_review_envelope.json"


def _phase_d_codex_section() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("## Phase D-Codex")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_decision_ladder_present():
    section = _phase_d_codex_section()
    assert "Verdict decision ladder" in section
    assert "`clean`" in section and "`minor-findings`" in section and "`needs-rework`" in section
    # Numbered rungs 1/2/3 ensure the ladder is explicit, not just vocabulary.
    assert "1. **`clean`**" in section
    assert "2. **`minor-findings`**" in section
    assert "3. **`needs-rework`**" in section


def test_worked_examples_present():
    section = _phase_d_codex_section()
    assert "Worked examples" in section
    # (1) nit → minor-findings
    assert "outdated comment" in section.lower() or "stale" in section.lower()
    assert "Verdict: **`minor-findings`**" in section
    # (2) acceptance-criterion miss → needs-rework
    assert "Acceptance criterion" in section
    assert "Verdict: **`needs-rework`**" in section


def test_evidence_gate_present():
    section = _phase_d_codex_section()
    # Heading is present between heuristic and worked examples.
    assert "Evidence gate" in section
    heuristic_idx = section.index("Heuristic when unsure")
    gate_idx = section.index("Evidence gate")
    examples_idx = section.index("Worked examples")
    assert heuristic_idx < gate_idx < examples_idx, (
        "Evidence gate subsection must sit between heuristic and worked examples"
    )
    # At least one named verification move the Codex sandbox can execute.
    assert (
        "pytest" in section
        or "trace" in section.lower()
        or "read the cited" in section.lower()
    )
    # Downgrade rule wording — hypothesis without verification becomes minor-findings.
    lowered = section.lower()
    assert "if you cannot verify" in lowered or "downgrade" in lowered
    assert "minor-findings" in section
    # The third worked example illustrates the downgrade path.
    assert "unverified" in lowered or "without verification" in lowered


def test_fixture_matches_review_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    env = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in schema["required"]:
        assert key in env, f"fixture missing required key {key!r}"
    assert env["verdict"] in schema["properties"]["verdict"]["enum"]
    finding_props = schema["properties"]["findings"]["items"]["properties"]
    severity_enum = finding_props["severity"]["enum"]
    for f in env["findings"]:
        for key in schema["properties"]["findings"]["items"]["required"]:
            assert key in f, f"finding missing required key {key!r}"
        assert f["severity"] in severity_enum
