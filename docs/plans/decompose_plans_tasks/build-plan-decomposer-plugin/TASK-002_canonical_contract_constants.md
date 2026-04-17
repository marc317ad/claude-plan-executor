---
task_id: "002"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Canonical Contract (v1) — Freeze These First"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-002
depends_on: ["001"]
superseded_by: []
change_history: []
priority: critical
---
<!-- prose_decisions: [prose_omitted_bounded_by_files] -->

# TASK-002 — Encode Canonical Contract as module-level constants in decomp_ops.py

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Canonical Contract (v1)
**Base branch:** main
**Chunk dependencies:** 001

---

## Goal

Create `decomp_ops.py` as a stdlib-only module whose only content (this TASK) is the frozen Canonical Contract: regexes, vocabularies, fingerprint normalization function, filename pattern, calibration ceilings, token budgets, and section-conditionality reason codes — each encoded in exactly one place so every later subcommand imports from here.

## Scoped Context

The Canonical Contract table in the parent plan is load-bearing: fingerprint normalization (lowercase + strip `:line_range` + collapse whitespace), status vocabularies (title-case frontmatter vs lowercase body), task-id regex `^\d{3}[A-Z]?$`, filename regex `^TASK-\d{3}[A-Z]?_[a-z0-9-]+\.md$`, and the Cross-Plugin Status Mapping. Drift between these and later renderer/validator code is the failure class the plan is designed to prevent.

Also pin the stdlib tokenizer used for prose-budget counting (whitespace + punctuation approximation) — future concision validators depend on its exact behavior.

## Verification

- `python plugins/plan-decomposer/scripts/decomp_ops.py --help` lists no subcommands yet but exits 0.
- `python -c "import importlib.util,sys; s=importlib.util.spec_from_file_location('d','plugins/plan-decomposer/scripts/decomp_ops.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); assert m.TASK_ID_RE.match('004A'); assert m.STATUS_MAP['Blocked']=='skipped'"` exits 0.

---

## Tasks

### TASK-002: Encode Canonical Contract as module-level constants in decomp_ops.py

- **Status:** open
- **Priority:** critical
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — new; stdlib-only; argparse scaffold + contract constants + helper functions (`normalize_fingerprint_input`, `count_prose_tokens`)
- **Dependencies:** 001
- **Test command:** `python plugins/plan-decomposer/scripts/decomp_ops.py --help`
- **Acceptance criteria:**
  - Module defines: `TASK_ID_RE`, `TASK_FILENAME_RE`, `DECOMPOSER_STATUSES` (7 title-case values including `Superseded`), `EXECUTOR_STATUSES` (5 lowercase), `STATUS_MAP` (cross-plugin), `PRIORITIES`, `TASK_TYPES = {"standard","gate"}`, `CEILING_FILES=5`, `CEILING_LOC=250`, `CEILING_PLAN_FILES=3`, `CEILING_NEW_MODULES=1`, `PROSE_BUDGET_STANDARD_HARD=400`, `PROSE_BUDGET_STANDARD_SOFT=200`, `PROSE_BUDGET_GATE_HARD=60`, `PROSE_BUDGET_GATE_SOFT=40`, `PROSE_REASON_CODES` (7 codes).
  - Function `normalize_fingerprint_input(title: str, first_file: str, problem_prefix: str) -> str` lowercases all three inputs, strips `:<line_range>` from path, collapses whitespace, joins with `|`.
  - Function `sha256_hex(normalized: str) -> str` returns `sha256:<hex>`.
  - Function `count_prose_tokens(body: str) -> int` excludes code-fence blocks and YAML frontmatter; approximates tiktoken via whitespace+punctuation split; algorithm pinned via docstring.
  - `argparse` top-level parser present with `--json` global flag; no subcommands yet (subparsers added in TASK-003+).
  - `python decomp_ops.py --help` exits 0.

**Description:**
Establishes the single source of truth for every schema decision downstream TASKs must respect. If any later TASK re-declares one of these constants, that TASK is wrong — it must import from this module.

**Implementation notes:**
Status title-case vocabulary matches `DUAL_AGENT_Plans/00_INDEX.json` byte-for-byte (correct spelling `Superseded`; the prior `Superceeded` misspelling was corrected across the plan-executor plugin and DUAL_AGENT_Plans roster).

**Reversion guidance:**
Delete the new file; this TASK creates `decomp_ops.py` from scratch.
