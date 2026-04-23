# TASK-007A — Self-Audit Follow-ups (post-TASK-007)

**Parent plan:** [`./TASK-007_self_audit.md`](./TASK-007_self_audit.md)
**Originating run:** `20260423T015255` (Codex binding pass-2 review on the D.2a.6 narrow-remediation retry)
**Base branch:** `main`
**Audit anchor commit:** `de6e64b`
**Chunk dependencies:** TASK-007 (self-audit subcommand shipped; this chunk closes follow-up findings deferred under the user-override "keep as-is" disposition).
**Issues absorbed:** none (new follow-up).
**Origin note:** TASK-007 committed as `de6e64b` with `[narrow-remediation]` + `[disagreement: 0,1,2,3,4]` trailers after orchestrator-user review of the binding pass-2 needs-rework verdict. Three findings are real and deferred here; two findings (the original critical claim and the design_doc_orphans coarseness) were dismissed on contract/spec grounds and are not addressed in this chunk.

---

## Goal

Close the three legitimate quality gaps in TASK-007's self-audit subcommand that were dismissed (with reasons) at commit time but warrant a small follow-up: (a) the default text rendering of `audit` is Python-repr style instead of pretty JSON, which works but reads poorly; (b) the strict-mode test does not isolate strict semantics from explicit `--check` semantics; (c) `_check_schemas` compares the verdict enum as an ordered list, making enum reordering a false-positive fail.

Each fix is local to one of two files and does not change the audit subcommand's CLI surface, finding shape, or canonical-contract structure.

---

## Scoped Context

### What this chunk is and isn't

This is a quality-polish chunk, not a redesign. Specifically:

- It does NOT add new `_check_*` functions.
- It does NOT change `CANONICAL_CONTRACT` or `ALIAS_WINDOWS`.
- It does NOT promote the `portable_tier` check from advisory to default — that remains TASK-008's scope.
- It does NOT change the `_audit_finding` shape (`{check, severity, summary, locations, suggestion}`).
- It does NOT change the `--list / --json / --report-file / --check / --strict` CLI surface.
- It does NOT change the verdict-tier rule (advisory excluded unless `--strict` or explicit `--check`).
- It does NOT replace any grep-based check with AST parsing — the plan's stated approach is to start with grep and only promote to AST when grep yields false positives, and no such false positives have been observed.

### Findings dismissed at TASK-007 commit (NOT addressed here)

For audit-trail completeness, these two findings from the binding pass-2 review were dismissed at commit time and remain dismissed:

- **Pass-2 finding 0 (critical, "audit produces empty stdout"):** Factually wrong. Reproduced `audit`, `audit --list`, and `audit --report-file …` and all three produced visible stdout. The `_emit` function falls through to a Python-repr-style text rendering when `--json` is omitted. The legitimate residual is that the repr-style output is ugly — captured as Finding A below — not the alleged null-output bug.
- **Pass-2 finding 1 (important, "design_doc_orphans alias check too coarse"):** Partially valid as a quality observation, but the plan's acceptance criterion only requires detecting deprecated-field/missing-subcommand references in design docs, which the current implementation satisfies. Alias-pairing refinement is beyond the contract and out of scope.

### The three findings, restated with scope calls

#### Finding A — Default `audit` stdout uses Python-repr formatting (UX polish)

When `audit` is invoked without `--json`, `_emit` currently falls back to printing the result dict via Python's default repr (e.g. `{'overall': 'pass', 'findings': [...]}` with single-quoted keys). This is observably visible and machine-parseable, but humans reading the stdout get an ugly wall of Python syntax instead of a readable summary. The fix is a small text-renderer for the non-JSON path.

**Fix scope:** In `cmd_audit` (or the local `_emit` helper it uses), branch on `args.json` and, when false, print a structured plaintext summary: header line `audit: <overall>`, one blank line, then per-finding lines `  - [<severity>] <check>: <summary>` followed by an indented `locations:` block when present. `--report-file` continues to emit the markdown report (unchanged). Do NOT add any new flag; the existing `--json` flag is the toggle.

#### Finding B — Strict-mode test does not isolate strict semantics

`tests/scripts/test_plan_ops.py::TestAuditStrictMode` currently builds an args namespace with both `check=portable_tier` (explicit `--check`) AND `strict=True`. Because explicit `--check` already forces an advisory check into the verdict tier, the test cannot distinguish "strict mode promoted advisory to verdict" from "explicit check forced it in". A regression that breaks `strict=True` while keeping the explicit-check path working would slip through.

**Fix scope:** Split the existing test into two cases:
1. `test_strict_promotes_advisory_without_explicit_check` — args namespace has `check=None` (or unset) and `strict=True`; assert that an advisory finding contributes to the verdict.
2. `test_explicit_check_forces_advisory_without_strict` — args namespace has `check=portable_tier` and `strict=False`; assert that the advisory finding contributes to the verdict (existing behavior).

The combined-flags case can stay as a third test (`test_strict_and_explicit_check_compose`) but is no longer the only test of strict semantics.

#### Finding C — `_check_schemas` compares verdict enum as ordered list

`_check_schemas` reads `codex_review_schema.json[properties.verdict.enum]` and compares it to the canonical enum from `CANONICAL_CONTRACT` using Python list equality. Reordering the enum in the schema (a non-semantic change) currently fails the check.

**Fix scope:** Compare as sets, not as ordered lists. The check should fail iff the two sets differ; ordering is not part of the contract. Update the assertion in `_check_schemas` (one line) and update the expected behavior in any test that exercises the check.

### Files this task edits

- `plugins/plan-executor/scripts/plan_ops.py` — text-render branch in `cmd_audit`/`_emit` for non-JSON path (Finding A); set-equality compare in `_check_schemas` (Finding C).
- `tests/scripts/test_plan_ops.py` — split `TestAuditStrictMode` into isolated cases (Finding B); add a regression test for the text-render output shape (Finding A); adjust schema-enum test if it asserts ordered equality (Finding C).

### Files this task does NOT edit

- `plugins/plan-executor/skills/implement-plan/SKILL.md` — no CLI-surface changes, no new flags, no doc impact.
- `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` — no contract changes; §14.1 stays as-is.
- `docs/plans/DUAL_AGENT_Plans/TASK-007_self_audit.md` — closed; do not re-edit.
- `plugins/plan-executor/scripts/_plan_paths.py` — out of scope.

---

## Verification

**V1 — `audit` (no flags) produces a readable plaintext summary.**

```bash
CLAUDE_PLUGIN_ROOT=plugins/plan-executor python3 plugins/plan-executor/scripts/plan_ops.py audit
```

Stdout begins with `audit: <pass|fail>` on the first line, followed by a blank line and per-finding bullet lines `  - [<severity>] <check>: <summary>`. Stdout MUST NOT contain Python-repr tokens (`{'`, `':`, `}`); a smoke test greps for those tokens and fails if present.

**V2 — `audit --json` is unchanged.**

```bash
CLAUDE_PLUGIN_ROOT=plugins/plan-executor python3 plugins/plan-executor/scripts/plan_ops.py audit --json | python3 -c "import sys, json; json.load(sys.stdin)"
```

Exits 0 (valid JSON) and the parsed object has the same top-level keys as before this chunk (`overall`, `findings`, optional `checks_run`, etc.).

**V3 — Strict-mode test isolates strict semantics from explicit-check semantics.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestAuditStrictMode
```

The class contains at least three tests: one that exercises `strict=True` with `check=None`, one that exercises `strict=False` with explicit `--check`, and one that exercises the composed case. Each independently asserts that the advisory finding affects the verdict in the expected way.

**V4 — `_check_schemas` accepts enum reordering.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "schemas and (enum or order)"
```

A new test reorders `properties.verdict.enum` in a fixture copy of `codex_review_schema.json` and asserts the check still passes. A second test mutates the set (adds/removes an entry) and asserts the check fails.

**V5 — Existing audit suite still green.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "audit or canonical_contract"
```

All pre-existing tests pass; new tests added under V1/V3/V4 also pass.

---

## Tasks

### TASK-007A: Self-audit follow-ups (text rendering, strict-mode isolation, schema enum order)

- **Status:** done
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Read-only context (not edited by TASK-007A):**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` — CLI surface unchanged; no doc edit needed.
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` — contract unchanged.
  - `docs/plans/DUAL_AGENT_Plans/TASK-007_self_audit.md` — closed.
- **Dependencies:** TASK-007
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "audit or canonical_contract or schemas"`
- **Acceptance criteria:**
  - Finding A (default text render): `audit` (no `--json`) prints a header line `audit: <overall>` followed by per-finding bullets `  - [<severity>] <check>: <summary>`. No Python-repr tokens in stdout. `--json` output is unchanged.
  - Finding B (strict-mode test isolation): `TestAuditStrictMode` contains separate tests for the strict-only path, the explicit-check-only path, and the composed path; each assertion targets one mechanism.
  - Finding C (enum order): `_check_schemas` compares `codex_review_schema.json[properties.verdict.enum]` to the canonical set as a set, not a list. Reordering passes; mutation fails.
  - All verification checks V1–V5 pass.
- **Deferred to downstream chunks (tracked, not required for TASK-007A merge):**
  - portable_tier promotion to default (currently advisory) — TASK-008's scope.
  - design_doc_orphans alias-pairing refinement — out of contract; not scheduled.
  - AST promotion of grep-based checks — only if a false positive is observed in practice.

**Description:**
Three small quality gaps in the TASK-007 audit subcommand: an ugly default text render, an under-isolated strict-mode test, and an order-sensitive enum compare. None affect the contract; each fix is local to one or two functions.

**Implementation notes:**
Keep the text renderer simple — a header, a blank line, and one bullet per finding with the existing severity/check/summary fields. Do not introduce a new templating layer or new color codes. The `locations` block should be indented under each finding when present and omitted when empty.

For the strict-mode test split, prefer parametrized pytest cases over copy-paste classes if the test file already uses `pytest.mark.parametrize`; otherwise three plain test methods on the same class is fine.

For the enum-set compare, the change is one line. Be careful that the test fixture does not mutate the real schema file in-place — copy or use `tmp_path` per the existing test conventions.

**Reversion guidance:**
If the text-render change causes downstream tooling to break (unlikely — `--json` was the documented machine path), revert just the `cmd_audit`/`_emit` branch and keep the test/enum changes. The three fixes are independent.

## Execution log — 20260423T094734 (paused)

Starting SHA: `469e941734658ea301d3457844142e78fa72d684`  → Ending SHA: `469e941734658ea301d3457844142e78fa72d684`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 007A | claude | codex | needs-rework (pass-2, paused) [narrow-remediation] | (none — paused) | D.2a.6 pass-2 re-litigates pass-1 spec-deference (finding 0 status-vs-severity). Awaiting user disposition: revert / keep-as-is / hand-fix. |

## Execution log — 20260423T094734 (success)

Starting SHA: `469e941734658ea301d3457844142e78fa72d684`  → Ending SHA: `207f80a025c1a2b02bb61c547408885a7a235047`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 007A | claude | claude | ship-with-fixes [narrow-remediation] [disagreement: 0,2] [pass-2-user-override] | 207f80a | D.2a.6: pass-1 codex needs-rework (3 findings); D.5 adjudication split 1 load-bearing / 0 spec-deference / 2 dismissed. Narrow remediation applied and tested green. Pass-2 codex returned needs-rework with 3 findings (1 re-litigated spec-deference; 2 new nits: em-dash, test-tightening). User overrode the D.2a.6 pause and directed hand-edits addressing all three pass-2 findings (bullet bracket now reads severity->tier, em-dash replaced with ASCII, test bullet contract tightened). Re-review intentionally bypassed per user direction. |
