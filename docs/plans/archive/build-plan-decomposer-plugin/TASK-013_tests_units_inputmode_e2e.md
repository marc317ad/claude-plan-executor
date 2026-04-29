---
task_id: "013"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Tests cases 1–29, 44–46"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-013
depends_on: ["003", "004", "005", "006"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-013 — Tests: subcommand units + input mode + e2e (cases 1–29, 44–46)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Tests cases 1–29 and 44–46
**Base branch:** main
**Chunk dependencies:** 003, 004, 005, 006

---

## Goal

Author `tests/scripts/test_decomp_ops.py` with test cases 1–29 (config, subcommand happy paths, supersede mode via `build-schedule`, render dry-run, validate-output, input mode detection including all symlink + filename regex + frontmatter edge cases, pinwheel-plan e2e) plus cases 44–46 (hard-abort tests for TASK-shaped files and directory inputs under `<tasks_root>`).

## Scoped Context

Includes the pinwheel plan fixture: copy the source to a tests fixture dir — DO NOT parse the actual `~/.claude/plans/` copy; tests must be hermetic. Build a minimal fixture under `tests/fixtures/plan-decomposer/` with a stripped-down pinwheel structure sufficient to validate phase detection, file-migration matrix, and test-matrix parsing.

Test case 29 is NORMATIVE: a hand-placed file under `<tasks_root>/<slug>/` with valid filename but missing `content_fingerprint` in frontmatter MUST hard-abort `invalid-task-frontmatter`. It MUST NOT fall back to fresh mode.

Tests 44–46: all three hard-abort paths — TASK-shaped file with empty/malformed frontmatter (44), directory input under `<tasks_root>` (45), TASK-shaped file whose `task_id` is absent from `_manifest.json` (46).

Use `pytest` style, `tmp_path` fixtures, subprocess invocation of `decomp_ops.py` (not import — CLI contract is what matters), `--json` output parsing.

## Verification

- `python -m pytest tests/scripts/test_decomp_ops.py -x` green with all test cases 1–29 + 44–46 present.
- `pytest --collect-only tests/scripts/test_decomp_ops.py | wc -l` shows ≥ 32 test functions.

---

## Tasks

### TASK-013: Tests — subcommand units, input mode, e2e

- **Status:** open
- **Priority:** high
- **Files:**
  - `tests/scripts/test_decomp_ops.py` — new test module (cases 1–29, 44–46)
  - `tests/fixtures/plan-decomposer/pinwheel_minimal.md` — stripped-down pinwheel fixture
  - `tests/fixtures/plan-decomposer/task_sample_*.md` — fixtures for supersede mode and input-mode edge cases
- **Dependencies:** 003, 004, 005, 006
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -x`
- **Acceptance criteria:**
  - All 32 test cases (1–29 + 44–46) present as named test functions.
  - Case 1 (config) covers env / cwd / plugin-default / malformed.
  - Cases 2–3 (inspect paths) emit 10-key payload; cases 4–5 (parse-source-plan) distinguish plan vs TASK input.
  - Case 6 (fingerprint determinism) — same input produces same hash across two invocations.
  - Cases 7–8 (manifest) — init shape + atomic commit + corrupt-stdin safety.
  - Cases 9–13 (build-schedule) — happy path, cycle detection, supersede happy, illegal-state, depth-exceeded.
  - Cases 14–17 (render + validate-output + set-status — or their replacements `manifest commit` / `sync-status` stubs invoked from later TASKs) cover the render-dry-run + render happy + validate-output + status-rewrite paths.
  - Case 18 (e2e pinwheel) runs parse → build-schedule → render (staging) → validate-output; exits 0; emits N TASK files.
  - Cases 20–23 (idempotency, merge on edit, context-budget split hard/soft).
  - Case 24 (gate annotation): gate candidate renders with `task_type: gate`; non-gate with `standard`; `validate-output` rejects unknown values.
  - Cases 25–29 (input mode detection): realpath symlink into / out of tasks_root, filename regex strictness, TASK-shaped source filename abort, frontmatter-missing-fields HARD-ABORT (not fresh fallback).
  - Cases 44–46 (hard aborts): invalid-frontmatter TASK in tasks_root, directory input under tasks_root, TASK-shaped with missing manifest record.
  - Every test asserts on `--json` output schema (no brittle stdout grep).

**Description:**
Core regression suite proving the subcommands 003–006 honor their contracts. Hard-abort semantics are what keep the decomposer from silently overwriting its own output.

**Reversion guidance:**
Delete `test_decomp_ops.py` and the new fixture files.
