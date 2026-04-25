"""Regression checks for the Phase D-Codex reviewer prompt calibration.

V3 — asserts the dispatch-templates.md Phase D-Codex section contains the
verdict decision ladder and both worked examples. V4 — asserts the checked-in
Codex review envelope fixture conforms structurally to the reviewer schema
(required keys, verdict enum, finding shape), so prompt tuning does not drift
from the wrapper's envelope contract.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "dispatch-templates.md"
SCHEMA = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "codex_review_schema.json"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "codex_review_envelope.json"
SKILL_MD = REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" / "SKILL.md"
DISPATCH_PY = REPO_ROOT / "plugins" / "plan-executor" / "scripts" / "plan_codex_dispatch.py"


def _load_dispatch_module():
    spec = importlib.util.spec_from_file_location(
        "plan_codex_dispatch_under_test", DISPATCH_PY
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _phase_d5_section() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("## Phase D.5")
    end = text.index("\n## ", start + 1)
    return text[start:end]


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


def test_review_prompt_forwards_description_and_impl_notes():
    mod = _load_dispatch_module()
    task = {
        "task_id": "999",
        "title": "Synthetic test task",
        "files": ["plugins/plan-executor/scripts/plan_codex_dispatch.py"],
        "acceptance_criteria": ["AC sentinel bullet"],
        "description": "DESCSENTINEL_lorem_ipsum_unique",
        "implementation_notes": "IMPLNOTESSENTINEL_dolor_sit",
        "raw_block": "",
    }
    prompt = mod.render_review_prompt(task, "diff text", "bugs", ["foo.py"])
    assert "Description:" in prompt
    assert "Implementation notes:" in prompt
    assert "DESCSENTINEL_lorem_ipsum_unique" in prompt
    assert "IMPLNOTESSENTINEL_dolor_sit" in prompt


def test_review_prompt_fallback_when_description_missing():
    mod = _load_dispatch_module()
    task = {
        "task_id": "999",
        "title": "Synthetic",
        "files": ["foo.py"],
        "acceptance_criteria": ["x"],
        "description": "",
        "implementation_notes": None,
        "raw_block": "",
    }
    prompt = mod.render_review_prompt(task, "", "bugs", ["foo.py"])
    assert "Description:\n(none provided)" in prompt
    assert "Implementation notes:\n(none provided)" in prompt


def test_symbol_verification_flags_missing_citation():
    mod = _load_dispatch_module()
    parsed = {
        "findings": [
            {
                "file": "plugins/plan-executor/scripts/plan_codex_dispatch.py",
                "issue": "This calls _definitely_not_real_xyz(foo) internally and breaks the invariant.",
                "severity": "high",
            }
        ]
    }
    out = mod._verify_cited_symbols(parsed, str(REPO_ROOT))
    not_found = [w for w in out if w["status"] == "not-found"]
    assert any(
        w["cited_symbol"] == "_definitely_not_real_xyz" for w in not_found
    ), out


def test_symbol_verification_passes_real_citation():
    mod = _load_dispatch_module()
    parsed = {
        "findings": [
            {
                "file": "plugins/plan-executor/scripts/plan_ops.py",
                "issue": "Look at how _append_run_log( handles the cleanup branch.",
                "severity": "low",
            }
        ]
    }
    out = mod._verify_cited_symbols(parsed, str(REPO_ROOT))
    assert out == []


def test_symbol_verification_no_candidate_produces_no_entry():
    mod = _load_dispatch_module()
    parsed = {
        "findings": [
            {
                "file": "plugins/plan-executor/scripts/plan_codex_dispatch.py",
                "issue": "the cleanup control-flow path is unreachable in this branch",
                "severity": "low",
            }
        ]
    }
    out = mod._verify_cited_symbols(parsed, str(REPO_ROOT))
    assert out == []


def test_symbol_verification_unchecked_when_file_missing():
    mod = _load_dispatch_module()
    parsed = {
        "findings": [
            {
                "file": "tmp/does_not_exist_xyz.py",
                "issue": "calls _some_symbol(arg) here and explodes",
                "severity": "low",
            }
        ]
    }
    out = mod._verify_cited_symbols(parsed, str(REPO_ROOT))
    assert any(
        w["status"] == "unchecked" and w["cited_symbol"] == "_some_symbol"
        for w in out
    ), out


def test_symbol_verification_skips_dotted_identifiers():
    mod = _load_dispatch_module()
    parsed = {
        "findings": [
            {
                "file": "plugins/plan-executor/scripts/plan_codex_dispatch.py",
                "issue": (
                    "Calls self.foo(bar) and obj.method(arg) and "
                    "obj._helper(arg) internally."
                ),
                "severity": "low",
            }
        ]
    }
    out = mod._verify_cited_symbols(parsed, str(REPO_ROOT))
    assert out == []


def test_review_envelope_includes_wrapper_checks():
    mod = _load_dispatch_module()
    env = mod.make_envelope(
        "999", "review", "success", parsed={"findings": []},
        extra={"wrapper_checks": {"symbol_warnings": []}},
    )
    assert "wrapper_checks" in env
    assert env["wrapper_checks"]["symbol_warnings"] == []


def test_d5_dispatch_template_and_skill_forward_wrapper_checks():
    section = _phase_d5_section()
    assert "Wrapper checks" in section
    assert "<wrapper_checks_json>" in section
    findings_idx = section.index("Codex findings")
    wrapper_idx = section.index("Wrapper checks")
    rubric_idx = section.index("Verdict decision rubric")
    assert findings_idx < wrapper_idx < rubric_idx, (
        "Wrapper checks block must sit between Codex findings and verdict rubric"
    )
    skill_text = SKILL_MD.read_text(encoding="utf-8")
    assert "render(templates.PhaseD5" in skill_text
    # Find the render(templates.PhaseD5 ...) call line and assert it
    # carries wrapper_checks as an argument.
    call_idx = skill_text.index("render(templates.PhaseD5")
    call_end = skill_text.index(")", call_idx)
    assert "wrapper_checks" in skill_text[call_idx:call_end]


def test_d5_spec_deference_rubric_scoped():
    section = _phase_d5_section()
    gate_start = section.index("Dismissal-evidence gate")
    gate_end = section.index('"Plan says so"', gate_start)
    gate = section[gate_start:gate_end]
    lowered = gate.lower()

    assert "implementer" in lowered
    assert "followed" in lowered
    assert "literal wording" in lowered or "literal-wording" in lowered

    example_idx = gate.index("Worked example")
    example = gate[example_idx:]
    dismissed_idx = example.index("dismissed")
    spec_idx = example.index("spec-deference")
    assert abs(dismissed_idx - spec_idx) <= 200


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
