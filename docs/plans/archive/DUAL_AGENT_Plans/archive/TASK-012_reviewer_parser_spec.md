# TASK-012 — Define `parse-reviewer-report` Contract and Integration

### TASK-012: Define `parse-reviewer-report` Contract and Integration

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md)
**Related chunks:** [`TASK-002_runtime_validation.md`](TASK-002_runtime_validation.md), [`TASK-003_state_isolation.md`](TASK-003_state_isolation.md), [`TASK-005_phase_gates.md`](TASK-005_phase_gates.md)
**Status:** proposed follow-on design note; not assigned to an existing v3 chunk
**Reason this exists:** TASK-002 names a reviewer validation seam ("reviewer -> parse-reviewer") but the current design pack does not define a concrete `parse-reviewer-report` wire contract, CLI, or test matrix. This file closes that design gap before implementation.

---

## Goal

Define a complete, implementable contract for a `plan_ops.py parse-reviewer-report` subcommand that consumes reviewer output at the real executor seam, emits a canonical structured payload, and halts on malformed reviewer input. The contract must preserve the existing asymmetric review vocabulary:

- Codex reviewer verdicts: `clean | minor-findings | needs-rework`
- Claude reviewer verdicts: `ship | ship-with-fixes | needs-rework`

The parser is a seam-normalizer and validator. It is not a policy engine. Routing decisions remain in the orchestrator.

---

## Why This Is Not Already Covered Elsewhere

This function is **not** implemented or fully specified in another chunk.

- [`TASK-002_runtime_validation.md`](TASK-002_runtime_validation.md) names the seam in principle but does not define the payload shape, CLI, or parser behavior.
- [`TASK-003_state_isolation.md`](TASK-003_state_isolation.md) is about review-path cleanup and baseline isolation, not parsing.
- [`TASK-005_phase_gates.md`](TASK-005_phase_gates.md) defines `review-safe` as a predicate family, but does not define a review parser.
- [`DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) says "Parse review report. Apply outcome matrix." It does not define a standalone helper contract for Claude reviewer output.

Therefore this is new design work, not unimplemented work from a prior chunk.

---

## Architectural Guidance

### 1. Validate at the real ingestion seam

Per TASK-002, validation should happen at the real handoff point, not only inside helper-local tests. For reviewer output, the architectural seam is:

1. Dispatch reviewer
2. Receive raw reviewer output
3. Parse and validate into canonical JSON
4. Route by verdict
5. Pass validated findings into `commit-task` or `fail-task`

If the orchestrator keeps consuming Claude reviewer prose directly and only later passes hand-normalized flags into `plan_ops.py`, then the seam remains under-specified and only partially validated.

### 2. Preserve asymmetric reviewer vocabularies

Per [`run-log-schema.md`](../../plugins/plan-executor/skills/implement-plan/run-log-schema.md), verdict vocabularies are intentionally asymmetric and must not be normalized:

- Codex reviewer: `clean | minor-findings | needs-rework`
- Claude reviewer: `ship | ship-with-fixes | needs-rework`

`parse-reviewer-report` must preserve the source verdict verbatim and include the reviewer type in its output.

### 3. Prefer structured producer output over brittle prose parsing

The current Claude `code-reviewer` agent emits human-readable markdown sections, not a hard machine contract. Parsing that format is possible, but brittle. The recommended architecture is:

- Keep `parse-reviewer-report` as the consumer seam.
- Tighten the producer prompt so Claude reviewer output has a fixed, parseable structure.
- Do not rely on fuzzy extraction from arbitrary prose if the prompt can instead require a narrow schema-like markdown shape.

This mirrors the executor rule already used for analyst schedules and implementer reports: canonical producer format first, parser second.

---

## Proposed Subcommand

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py parse-reviewer-report --stdin --reviewer <claude|codex> --json
```

### Arguments

- `--stdin`
  - Required. Reads the raw reviewer report from stdin.
- `--reviewer <claude|codex>`
  - Required. Declares which reviewer contract to validate against.
- `--json`
  - Optional standard `plan_ops.py` structured output flag.

No file-path input mode in v1. This remains a pipe helper, like `parse-schedule` and `parse-implementer-report`.

---

## Canonical Output Contract

On success, emit:

```json
{
  "reviewer": "claude|codex",
  "verdict": "ship|ship-with-fixes|needs-rework|clean|minor-findings",
  "summary": "short verdict summary",
  "findings": [
    {
      "severity": "critical|important|minor",
      "file": "path/to/file.py",
      "line": 123,
      "issue": "what is wrong",
      "suggested_fix": "concrete suggested fix"
    }
  ],
  "minor_findings": [
    {
      "severity": "minor",
      "file": "path/to/file.py",
      "line": 123,
      "issue": "what is wrong",
      "suggested_fix": "concrete suggested fix"
    }
  ],
  "findings_count": 1,
  "warnings": [],
  "errors": []
}
```

### Field semantics

- `reviewer`
  - Required. Echoes `claude` or `codex`.
- `verdict`
  - Required. Preserved verbatim from the reviewer contract.
- `summary`
  - Required. Reviewer’s high-level verdict text.
- `findings`
  - Required. Full normalized finding list.
- `minor_findings`
  - Required. Derived subset of `findings` where `severity == "minor"`.
- `findings_count`
  - Required. Integer equal to `len(findings)`.
- `warnings`
  - Required. For compatibility aliases only; should usually be empty.
- `errors`
  - Required. Empty on success.

### Exit behavior

- Exit `0` when the report is valid and `errors == []`.
- Exit `1` on any contract failure.
- On failure, emit structured `errors` with `path`, `code`, and `message`, matching TASK-002 validation style.

---

## Canonical Finding Shape

Every finding is an object:

```json
{
  "severity": "critical|important|minor",
  "file": "string",
  "line": 123,
  "issue": "string",
  "suggested_fix": "string"
}
```

### Rules

- `severity` must be one of `critical | important | minor`
- `file` must be a non-empty string
- `line` must be an integer > 0
- `issue` must be a non-empty string
- `suggested_fix` must be a non-empty string
- Additional fields are rejected in v1

This matches the minor-findings item shape already used in the executor run-log schema and Codex review schema.

---

## Reviewer-Specific Validation Rules

### Codex reviewer mode

Producer shape already exists via [`plugins/plan-executor/scripts/codex_review_schema.json`](../../plugins/plan-executor/scripts/codex_review_schema.json).

`parse-reviewer-report --reviewer codex` should:

- accept the existing schema-shaped JSON on stdin
- validate it again at the seam
- normalize output into the canonical parser output shape above

#### Allowed verdicts

- `clean`
- `minor-findings`
- `needs-rework`

#### Expected input fields

- `task_id`
- `verdict`
- `findings`
- `scope_ok`
- `acceptance_met`
- `summary`

`task_id`, `scope_ok`, and `acceptance_met` may be preserved in a future richer output shape, but are not required in the minimal parser result above. If retained, they should be additive and documented explicitly.

### Claude reviewer mode

Claude reviewer does not yet have an equivalent machine schema. This spec defines one.

#### Allowed verdicts

- `ship`
- `ship-with-fixes`
- `needs-rework`

#### Required semantic content

Claude reviewer output must provide:

- one verdict
- one short summary
- zero or more findings, each carrying severity and location

#### Recommended canonical markdown producer shape

To avoid brittle prose scraping, require the Claude reviewer prompt/template to emit:

```markdown
# Review: TASK-001

**Verdict:** ship | ship-with-fixes | needs-rework

**Summary:**
<2-3 sentences>

**Findings:**
- severity: critical | file: src/foo.py | line: 123 | issue: ... | suggested_fix: ...
- severity: minor | file: src/bar.py | line: 45 | issue: ... | suggested_fix: ...
```

Optional:

```markdown
**Not reviewed:**
- ...
```

This is intentionally stricter than the current freeform `code-reviewer` report. The parser should target this canonical shape rather than the reviewer’s current narrative sections.

#### Compatibility window for current reviewer prose

If a compatibility window is needed, allow a temporary alias parser for the current `code-reviewer` markdown:

- `## Summary`
- `## Critical (must fix)`
- `## Important (should fix)`
- `## Minor / nits`

But this alias mode should be explicitly temporary and warning-bearing, because it is less machine-stable than the canonical bullet form above.

Example warning:

```json
{
  "warnings": [
    "reviewer report used legacy sectioned markdown format; canonical shape is the fixed **Findings:** bullet list"
  ]
}
```

---

## Verdict Consistency Rules

The parser must enforce severity/verdict consistency.

### Codex reviewer

- Any `critical` or `important` finding is compatible only with `needs-rework`
- `minor-findings` implies all findings, if any, are `minor`
- `clean` implies `findings == []`

### Claude reviewer

Per [`code-reviewer.md`](../../plugins/plan-executor/agents/code-reviewer.md):

- Any `critical` finding forbids `ship`
- Any `important` finding forbids `ship`
- `ship` implies no `critical` and no `important` findings
- `ship-with-fixes` is allowed with `important` and `minor`, and may also be used for `critical` if the producer contract continues to permit it
- `needs-rework` is always allowed

Recommended stricter v1 parser rule:

- If any `critical` finding exists, require `needs-rework`
- If any `important` finding exists, allow `ship-with-fixes` or `needs-rework`
- `ship` implies only `minor` findings or none

This stricter rule is better aligned with executor safety than permitting `critical + ship-with-fixes`.

---

## Failure Cases

`parse-reviewer-report` must halt on:

- missing verdict
- invalid verdict enum for declared reviewer type
- malformed finding objects
- missing summary
- verdict inconsistent with severities
- unknown top-level fields in schema-driven inputs
- malformed JSON in Codex mode
- unparseable required sections in Claude mode

Example error object:

```json
{
  "path": "$.findings[0].line",
  "code": "invalid-reviewer-field",
  "message": "reviewer finding field 'line' must be a positive integer"
}
```

---

## Integration Points

### Orchestrator flow

Replace direct ad hoc reviewer consumption with:

1. Raw reviewer output
2. `parse-reviewer-report`
3. Route by `verdict`
4. Pass validated `minor_findings` to `commit-task`
5. Pass validated full `findings` payload to `fail-task --stage review`

### `commit-task`

`commit-task` should continue validating the already-structured payload defensively, but the main normalization should happen earlier at `parse-reviewer-report`.

### `fail-task`

`fail-task --stage review` should accept the canonical parsed review payload, not arbitrary JSON blobs.

### Run-log

`review_done` and `failed(stage=review)` should derive their reviewer fields from the canonical parsed payload, not ad hoc transformations.

---

## Tests Required

### Unit / helper tests

- `test_parse_reviewer_report_codex_clean`
- `test_parse_reviewer_report_codex_minor_findings`
- `test_parse_reviewer_report_codex_rejects_invalid_finding_shape`
- `test_parse_reviewer_report_codex_rejects_clean_with_findings`
- `test_parse_reviewer_report_claude_ship_no_findings`
- `test_parse_reviewer_report_claude_ship_with_only_minor_findings`
- `test_parse_reviewer_report_claude_rejects_ship_with_important`
- `test_parse_reviewer_report_claude_rejects_missing_verdict`
- `test_parse_reviewer_report_claude_rejects_unparseable_legacy_markdown`

### Real producer -> consumer seam tests

- Codex review wrapper output piped into `parse-reviewer-report --reviewer codex`
- Claude `code-reviewer` output, under the new fixed markdown template, piped into `parse-reviewer-report --reviewer claude`

The Claude test is the decisive one. Without it, the seam remains only synthetically specified.

---

## Easy-Add Assessment

This is **not** a trivial add if done correctly.

### Why it is not easy

- Codex review already has a machine schema; Claude review does not.
- The current Claude reviewer contract is optimized for human readability, not strict parsing.
- A parser added today without tightening the producer prompt would be fragile and likely violate TASK-002’s "validate canonical contract" intent by baking in heuristic prose scraping.

### When it becomes easy

It becomes an easy-add if we first tighten the Claude reviewer producer contract to emit a fixed markdown or JSON-like shape. Once that exists:

- `parse-reviewer-report` is straightforward
- tests are straightforward
- downstream routing gets simpler

### Recommendation

Treat this as a small design-and-implementation follow-on, not as an opportunistic helper tweak. The first step should be canonicalizing Claude reviewer output shape, then implementing the parser against that shape.

---

## Planning-Agent Prompt

Use this prompt if the work is handed to a planning/implementation agent.

```text
Implement the proposed reviewer-parser follow-on described in docs/plans/DUAL_AGENT_Plans/TASK-012_reviewer_parser_spec.md.

Constraints:
- Preserve the existing TASK-002 principle: validation must happen at the real reviewer ingestion seam.
- Do not normalize away asymmetric reviewer vocabularies.
- Do not add heuristic prose scraping if the producer contract can be tightened instead.
- Prefer the minimum change set that makes the seam real and testable.

Required deliverables:
1. Update the Claude reviewer producer contract so its output is parseable by a fixed canonical shape.
2. Add `plugins/plan-executor/scripts/plan_ops.py parse-reviewer-report --stdin --reviewer <claude|codex> --json`.
3. Route reviewer consumption through that parser instead of ad hoc field passing.
4. Keep defensive validation in `commit-task` / `fail-task`, but make `parse-reviewer-report` the main normalization seam.
5. Add focused tests in `tests/scripts/test_plan_ops.py` for both reviewer types and for verdict/severity consistency failures.
6. If real producer tests are feasible, add one Codex reviewer roundtrip and one Claude reviewer roundtrip.

Out of scope:
- TASK-003 cleanup/isolation refactors beyond what is strictly necessary for parser integration.
- TASK-005 gate work except where tests must be adjusted to reflect the new seam.

Before coding, confirm whether the best producer shape for Claude reviewer is:
- fixed markdown sections with structured finding bullets, or
- direct JSON.

Favor whichever fits the existing executor architecture with the least drift.
```

---

## Decision Summary

- `parse-reviewer-report` is a missing contract, not an already-assigned chunk.
- It should be implemented only after the Claude reviewer producer format is made canonical and parseable.
- With that producer change in place, the parser is a moderate but clean add.
- Without that producer change, implementation would be brittle and should not be treated as a quick helper patch.
