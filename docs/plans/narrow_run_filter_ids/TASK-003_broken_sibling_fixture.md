# TASK-003 — Broken-sibling fixture and regression tests

**Base branch:** `main`
**Chunk dependencies:** TASK-002 (the new `--filter-ids` semantics drive the assertions)

---

## Goal

Land a fixture under `tests/fixtures/directory_mode_plan_broken_siblings/` that captures the canonical bug (free-form prose in `**Dependencies:**` defeating the parser) and pin the new scoped-validation behavior with end-to-end tests.

## Scoped Context

The shipped canonical fixture (`tests/fixtures/directory_mode_plan/`) must stay clean — `_gate_fixture_valid` is a Phase 0 preflight gate that asserts the canonical sample passes `schema-valid` + `schedule-valid`. The new fixture lives beside it and exists specifically to demonstrate that scoped runs survive sibling rot.

### Fixture shape

`tests/fixtures/directory_mode_plan_broken_siblings/`:

- `00_INDEX.json` — 4 chunks with structured `depends_on`:
  - `001 → []`, `002 → [001]`, `003 → [001]`, `004 → [001, 002]`.
- `TASK-001_*.md` — well-formed task block.
- `TASK-002_*.md` — body has `**Dependencies:** TASK-001 (foundation — see prior notes), TASK-007 (audit pass not strictly required).` This mirrors the `TASK-009_scale_aware_reads.md:176` defect verbatim in style.
- `TASK-003_*.md` — well-formed task block; depends on 001 only.
- `TASK-004_*.md` — body has `**Dependencies:** TASK-001 (schema lock) and TASK-002 (the partially-landed runtime validation).` Different prose pattern from TASK-002's defect to exercise both the comma-split and the `and`-conjunction failure modes.

### Existing surfaces we touch

- `tests/fixtures/directory_mode_plan_broken_siblings/` — new directory.
- `tests/scripts/test_plan_ops.py` — new test class `TestBuildTasksBrokenSiblings` near `TestBuildTasks`.

### Non-goals

- Modifying the canonical `tests/fixtures/directory_mode_plan/`.
- Adding any `*.schedule.json` sidecar (the fixture is for `build-tasks` + `index-closure`; schedule comes later in the orchestrator path).
- Wiring `_gate_fixture_valid` to also check the broken-siblings fixture. That gate stays anchored on the canonical sample.

## Verification

**V1.** Default `build-tasks --plans-dir <broken-fixture>` exits 1 with `unresolvable-dep` errors against TASK-002 and TASK-004's body-deps prose. Default-mode regression: the bug is preserved exactly when no filter is set.

**V2.** Scoped `build-tasks --plans-dir <broken-fixture> --filter-ids 003 --json` exits 0 with `tasks=[001, 003]`, `scope.skipped_chunk_count=2` (TASK-002 + TASK-004 outside closure).

**V3.** Scoped `build-tasks --plans-dir <broken-fixture> --filter-ids 002 --json` exits 1 with `unresolvable-dep` against TASK-002's body. Filter-set-internal breakage halts loudly even on the scoped path.

**V4.** Scoped `build-tasks --plans-dir <broken-fixture> --filter-ids 003,004 --json` (mixed clean + broken in closure) exits 1 with errors only against TASK-004's body. TASK-002 stays silent because it's outside this closure.

**V5.** `build-tasks --plans-dir <broken-fixture> --filter-ids 001` (leaf) exits 0 with `tasks=[001]` and `scope.closure=["001"]`. No body-parse runs against any other child.

**V6.** `index-closure --plans-dir <broken-fixture> --task-ids 003,004 --json` returns `{closure: ["001", "002", "003", "004"], skipped_chunk_count: 0, errors: []}`. Demonstrates the closure walks `004 → 002` even though TASK-002's body is malformed (the helper is pure-roster).

---

## Tasks

### TASK-003: Broken-sibling fixture and regression tests

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `tests/fixtures/directory_mode_plan_broken_siblings/00_INDEX.json` (create)
  - `tests/fixtures/directory_mode_plan_broken_siblings/TASK-001_<slug>.md` (create)
  - `tests/fixtures/directory_mode_plan_broken_siblings/TASK-002_<slug>.md` (create)
  - `tests/fixtures/directory_mode_plan_broken_siblings/TASK-003_<slug>.md` (create)
  - `tests/fixtures/directory_mode_plan_broken_siblings/TASK-004_<slug>.md` (create)
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "BrokenSiblings or broken_sibling"`
- **Acceptance criteria:**
  - V1–V6 pass.
  - Each child file in the fixture is a complete, parseable task block aside from the deliberate `**Dependencies:**` defect on TASK-002 + TASK-004 (i.e., they have valid `### TASK-NNN:` H3, `**Status:**`, `**Priority:**`, `**Files:**`, `**Test command:**`, `**Acceptance criteria:**`, `**Description:**`). The defect is isolated to the deps line; everything else parses.
  - Body-deps prose for TASK-002 + TASK-004 is copied in style from real defects in `docs/plans/DUAL_AGENT_Plans/TASK-009_scale_aware_reads.md:176` and similar — no synthetic strings, the fixture documents the real bug.
  - The shipped `tests/fixtures/directory_mode_plan/` is unchanged.
  - `_gate_fixture_valid` continues to pass against the canonical sample.

**Description:**

This task ships test infrastructure, not behavior. The unit tests in TASK-002 use inline string fixtures to exercise `_build_tasks` and `_compute_index_closure` directly; the end-to-end tests here exercise the CLI subcommands against an on-disk directory shaped like a real plan. Together they bracket the change at both the function and the subcommand boundaries.

**Implementation notes:**

- Pick file slugs that are short and don't collide with anything in the canonical fixture: e.g., `TASK-001_alpha.md`, `TASK-002_beta.md`, `TASK-003_gamma.md`, `TASK-004_delta.md`.
- The fixture's `00_INDEX.json` should NOT carry `decompose_meta` or other auto-promote fields — manual-sidecar shape only, mirroring `tests/fixtures/directory_mode_plan/00_INDEX.json`.
- Test class `TestBuildTasksBrokenSiblings` should subclass nothing (use the same plain-pytest style as `TestBuildTasks`). Use a `pytest.fixture` to materialize the fixture path.
- Each test runs the CLI via `subprocess.run([sys.executable, "plan_ops.py", ...], capture_output=True)` so the assertions cover argparse + dispatch wiring, not just the helper.

**Reversion guidance:**

`rm -rf tests/fixtures/directory_mode_plan_broken_siblings/` plus `git restore tests/scripts/test_plan_ops.py`. The fixture is referenced only by the new test class; no production code points at it.
