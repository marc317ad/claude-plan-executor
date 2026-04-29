---
task_id: "007"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Recovery + Idempotency Protocol; CLI Surface item 8"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-007
depends_on: ["006"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-007 — Implement commit-swap subcommand (7-phase atomic protocol)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Recovery + Idempotency Protocol; CLI Surface item 8
**Base branch:** main
**Chunk dependencies:** 006

---

## Goal

Add `commit-swap`, the ONLY subcommand that mutates production paths. It encapsulates the full 7-phase cross-directory atomic protocol: pre-swap invariant check → journal phase 1 → parent rename → journal phase 2 → tasks backup → journal phase 3 → tasks swap → journal phase 4 → backup cleanup → journal phase 5 → final commit (+ cache manifest write).

## Scoped Context

Cross-directory ACID atomicity is not achievable with POSIX `rename` alone. The guarantee is **idempotent recovery**: every file is regenerable from `(source_plan, journal)`, so partial failure is recoverable. Journal lives OUTSIDE the swapped dir at `<tasks_root>/<slug>.journal.json` (a sibling of `<tasks_root>/<slug>/`) — this is critical because `<slug>/` itself is renamed during the tasks swap.

Cross-device rename check: compare `os.stat(...).st_dev` across `{staging_parent, production_parent, staging_tasks_dir, production_tasks_dir, journal_file}`. Mismatch → abort `cross-device-rename-unsafe` (exit code 4). Never attempt the rename.

Parent-copy drift policy (pre-swap invariant check): decide adopt / proceed / abort based on `(F_prod, F_journal, F_source)`. Error codes: `parent-copy-divergent` (prod hand-edited after prior run), `parent-copy-missing` (journal says committed but prod gone), `parent-copy-untracked` (no journal fingerprint AND prod exists AND F_prod != F_source). Only `--force-parent` bypasses — records `parent_override_at/reason/pre_override_fingerprint/source_fingerprint` in the `run_history[]` entry.

Phase-1 journal MUST include `prior_parent_fingerprint` (F_prod at run start) — this is what makes W0 disambiguation watertight.

Exit codes: `0 success`, `1 validation or invariant failure`, `2 recoverable-from-journal (re-run needed)`, `3 parent-copy-divergent / untracked / missing`, `4 cross-device-rename-unsafe`.

`--skip-parent-rename` (supersede mode) bypasses phases 1–4 parent-related steps but still routes tasks swap + all journal phases.

## Verification

- Clean first run: exits 0; production_parent and production_tasks_dir are NEW; journal `phase: "committed"`.
- Cross-device simulation (mock differing `st_dev`): exit 4, no rename attempted.
- Production parent hand-edited between runs: exit 3 (`parent-copy-divergent`) citing all three fingerprints.
- `--force-parent` on the hand-edited case: exit 0; journal records `parent_override_reason: "divergent"` + `pre_override_fingerprint`.

---

## Tasks

### TASK-007: Implement commit-swap subcommand

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add `commit-swap` handler + 7-phase state machine + journal writer (atomic `write-then-rename` on `.tmp`) + `st_dev` guard + parent-drift matrix + force-flag audit
- **Dependencies:** 006
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "commit_swap or cross_device or parent_divergent or parent_untracked or parent_missing" -x`
- **Acceptance criteria:**
  - All 7 protocol phases implemented in-order; each journal write occurs BEFORE the work the phase describes.
  - Journal writes use atomic `write-then-rename` (`os.replace`) of `.tmp` sibling; no bash `mv` anywhere.
  - `st_dev` mismatch on any of the five paths → exit 4 before any rename; error code `cross-device-rename-unsafe`.
  - Pre-swap invariant check implements the full matrix from the parent plan (F_prod vs F_journal vs F_source).
  - `--force-parent` bypass records `parent_override_at`, `parent_override_reason` ∈ `{divergent, missing, untracked}`, `pre_override_fingerprint`, `source_fingerprint` on the `run_history[]` entry (NOT inside `committed_state`).
  - `--skip-parent-rename` skips phases 1–4 parent steps; tasks swap + journal phases 3–5 still execute.
  - Phase-1 journal entry contains `run_id`, `phase: "pre-parent-rename"`, `source_fingerprint`, `prior_parent_fingerprint`, `planned_parent_fingerprint`, `planned_task_count`, `started_at`.
  - Each subsequent phase adds the required fields per the parent plan's phase list.
  - Exit codes match the contract (`0/1/2/3/4`); stdout prints final journal state on success.

**Description:**
The filesystem-authoritative step. Every production write is here. Every partial failure produces a journal state from which `recover` (TASK-008) can finish.

**Implementation notes:**
`backup_path` field: `null` when production tasks dir was absent at backup time. `os.rename` is used for production transitions; `shutil.rmtree` only on the BACKUP (never on production). When renaming same-content parent copies, compare fingerprints first and no-op the rename.

**Reversion guidance:**
Revert the `commit-swap` handler + helpers in `decomp_ops.py`.
