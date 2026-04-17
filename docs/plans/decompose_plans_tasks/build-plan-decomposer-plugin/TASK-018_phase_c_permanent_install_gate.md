---
task_id: "018"
task_type: gate
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Phase C — Permanent install"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-018
depends_on: ["017"]
superseded_by: []
change_history: []
priority: medium
---
<!-- prose_decisions: [prose_omitted_gate] -->

# TASK-018 — GATE: Phase C permanent install via /plugin install

**Source section:** Phase C — Permanent install
**Base branch:** main
**Chunk dependencies:** 017

---

## Goal

Install the plan-decomposer plugin permanently at user scope once Phase B parity holds, and land the downstream-consumer documentation.

## Tasks

### TASK-018: Phase C permanent install

- **Status:** open
- **Priority:** medium
- **Files:**
  - `README.md` — confirm Phase C section is present and points at the install command
- **Dependencies:** 017
- **Test command:** none
- **Acceptance criteria:**
  - `/plugin install plan-decomposer@claude-plan-executor --scope user` succeeds; `/plugin list` lists `plan-decomposer` at user scope.
  - `/decompose-plan <some_plan.md> --dry-run` works in a fresh Claude Code session without `--plugin-dir` override.
  - README.md Phase C note links to install command and `.claude/plan-decomposer.json` shape for downstream consumers.
  - Downstream consumer `CLAUDE.md` template / note documented in README.md for opt-in install.

**Description:**
Manual install gate. Operator runs `/plugin install` at user scope and confirms the plugin works in a fresh session without `--plugin-dir` override.

**Reversion guidance:**
`/plugin uninstall plan-decomposer` to remove the user-scope install; revert the Phase C README note if it was committed separately.

## Verification

- `/plugin list` in a fresh Claude Code session shows `plan-decomposer` at user scope.
- `claude` session (no `--plugin-dir` flag) can invoke `/decompose-plan`.
- README.md references the user-scope install command AND the consumer-config file shape.
