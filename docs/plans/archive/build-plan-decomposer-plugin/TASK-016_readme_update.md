---
task_id: "016"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Phase C step 19; Critical Files to Touch"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-016
depends_on: ["011"]
superseded_by: []
change_history: []
priority: medium
---
<!-- prose_decisions: [prose_omitted_self_evident, prose_omitted_trivial_scope, prose_omitted_bounded_by_files] -->

# TASK-016 — Update README with plan-decomposer section + /decompose vs /implement note

**Source section:** Phase C step 19
**Base branch:** main
**Chunk dependencies:** 011

---

## Goal

Add a top-level `plan-decomposer` section to the repo `README.md` and a short disambiguation paragraph explaining when to use `/decompose-plan` (freeform plan → per-TASK files) vs `/implement-plan` (run existing TASK files). Also replace the placeholder in `plugins/plan-decomposer/docs/README.md` with a substantive plugin-local README.

## Verification

- `grep -n "plan-decomposer" README.md` returns ≥ 1 line in the new section.
- `grep -nE "/decompose-plan|/implement-plan" README.md` returns lines from the disambiguation paragraph.
- `plugins/plan-decomposer/docs/README.md` is ≥ 20 lines and describes install, invocation, and expected output layout.

---

## Tasks

### TASK-016: README updates

- **Status:** open
- **Priority:** medium
- **Files:**
  - `README.md` — add plan-decomposer section + disambiguation paragraph
  - `plugins/plan-decomposer/docs/README.md` — replace placeholder with real plugin README
- **Dependencies:** 011
- **Test command:** `grep -q "^## plan-decomposer" README.md && grep -q "/decompose-plan" README.md && grep -q "/implement-plan" README.md`
- **Acceptance criteria:**
  - `README.md` contains a `## plan-decomposer` (or equivalent H2) section covering: what the plugin does, install command, typical invocation, output location.
  - Disambiguation paragraph (anywhere in README.md) explicitly contrasts `/decompose-plan` (creates TASK files) vs `/implement-plan` (executes existing TASK files).
  - `plugins/plan-decomposer/docs/README.md` covers: install, `.claude/plan-decomposer.json` shape, command surface, supersede usage.

**Description:**
Documents how downstream users discover and invoke the plugin. Locked behind TASK-011 because command names / skill paths must already exist before being documented.

**Reversion guidance:**
`git checkout HEAD -- README.md plugins/plan-decomposer/docs/README.md`.
