---
task_id: "009"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Cross-Plugin Status Contract; CLI Surface item 10"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-009
depends_on: ["004"]
superseded_by: []
change_history: []
priority: medium
---
<!-- prose_decisions: [prose_omitted_trivial_scope] -->

# TASK-009 — Implement sync-status subcommand

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Cross-Plugin Status Contract; CLI Surface item 10
**Base branch:** main
**Chunk dependencies:** 004

---

## Goal

Add `sync-status` to reconcile executor-owned body `- **Status:**` bullets (lowercase) with decomposer-owned frontmatter `status:` + `00_INDEX.json.chunks[].status` (title-case), using the contract's mapping table. Non-destructive by default: only `Pending → Done/Failed`; `--force` required for any demotion.

## Scoped Context

The two plugins co-own state on the same files but on different fields — this subcommand is the opt-in reconciliation path. Emits a manifest `history[]` entry: `{action: "sync-status", promoted: [...], demoted: [...], run_id, at}`. Writes use `manifest --action commit`'s atomic path (reuse, don't re-implement).

## Verification

- TASK with frontmatter `status: Pending` + body `- **Status:** done` → `sync-status` promotes frontmatter to `Done`, `00_INDEX.json` chunk to `Done`, records history entry.
- Re-running `sync-status` on an already-synced dir is a no-op (empty `promoted`/`demoted`).
- Frontmatter `status: Done` + body `- **Status:** open` → default run leaves frontmatter, reports inconsistency; `--force` run demotes.

---

## Tasks

### TASK-009: Implement sync-status subcommand

- **Status:** open
- **Priority:** medium
- **Files:**
  - `plugins/plan-decomposer/scripts/decomp_ops.py` — add `sync-status` handler; reuses `STATUS_MAP` + `manifest commit`
- **Dependencies:** 004
- **Test command:** `python -m pytest tests/scripts/test_decomp_ops.py -k "sync_status" -x`
- **Acceptance criteria:**
  - Default (no `--force`): only promotes `Pending → Done` or `Pending → Failed`.
  - `--force`: allows any lowercase-body-driven demotion; records demotion in history.
  - Idempotent: second run on the same inputs produces empty `promoted/demoted` and no writes.
  - `--json` emits `{promoted: [...], demoted: [...], inconsistent: [...], run_id}`.
  - Manifest `history[]` entry appended atomically via `manifest commit`.

**Description:**
Closes the cross-plugin status loop: after `/implement-plan` runs, this promotes the plan-lifecycle view without demoting by accident. The mapping lives in a single constant imported from TASK-002.

**Reversion guidance:**
Revert the `sync-status` handler hunk in `decomp_ops.py`.
