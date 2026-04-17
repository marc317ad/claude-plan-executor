---
task_id: "010"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "Agent Spec: plan-decomposer.md"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-010
depends_on: ["003", "004", "005", "006", "007", "008", "009"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [] -->

# TASK-010 — Write plan-decomposer agent (14-step workflow)

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** Agent Spec: plan-decomposer.md
**Base branch:** main
**Chunk dependencies:** 003, 004, 005, 006, 007, 008, 009

---

## Goal

Author `agents/plan-decomposer.md` encoding the 14-step fresh-decomposition workflow, the supersede-mode divergence, and the subcommand-to-workflow mapping. The agent MUST never hand-roll `mv` / `shutil.move` — all production transitions route through `commit-swap`.

## Scoped Context

Modeled on `fix-plan-decomposer.md` structure but adapted to plan-to-task decomposition. Required tools: `Read, Write, Glob, Grep, Bash`. Model: `opus`.

Load-existing-state FIRST (step 2, Codex ordering fix): read manifest + scan existing TASK-*.md files BEFORE fingerprint computation, so the authoritative state model exists before reconciliation.

Render-step sub-bullets (step 11 a–e): section elision, gate slim render, overlap merge, parent-plan inline decision, token-budget check. A `prose-budget-exceeded` at step 11e MUST leave staging intact and skip `commit-swap`.

Step 13 is the exclusive production-write step: the agent invokes `decomp_ops.py commit-swap` in-process. On exit code 2 (recoverable), the agent calls `recover`. On exit code 3 (parent-copy divergence), the agent surfaces the error and HALTS — it never silently applies `--force-parent`.

Supersede mode (input_mode=="supersede"): steps 1–8 treat the parent TASK's body as a mini source plan; children get single-letter suffix IDs; parent becomes `Superceeded` with `superseded_by: [child_ids]`; `commit-swap` is invoked with `--skip-parent-rename`; abort with `supersede-illegal-state` on parents in `Superceeded / Done / Cancelled`; abort `supersede-after-execution` when body `- **Status:** done` (override with `--force-supersede`).

## Verification

- `plugins/plan-decomposer/agents/plan-decomposer.md` exists and its frontmatter specifies `model: opus` and the required tool list.
- Agent body contains sections: Inputs, Input mode detection, Workflow (14 numbered steps), Workflow supersede mode, Rules, Subcommand-to-Workflow mapping.
- Grep confirms NO mention of `mv ` / `shutil.move` / `shutil.copy` as production-write verbs.
- Dry-run smoke (manual): pointing the agent at the pinwheel plan with `--dry-run` writes only to staging dirs (deferred to TASK-017 gate).

---

## Tasks

### TASK-010: Write plan-decomposer agent

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/agents/plan-decomposer.md` — new agent definition (frontmatter + prose)
- **Dependencies:** 003, 004, 005, 006, 007, 008, 009
- **Test command:** `grep -n "commit-swap" plugins/plan-decomposer/agents/plan-decomposer.md && ! grep -nE '\bmv |shutil\.move|shutil\.copy' plugins/plan-decomposer/agents/plan-decomposer.md`
- **Acceptance criteria:**
  - Agent frontmatter: `name: plan-decomposer`, `model: opus`, `tools: Read, Write, Glob, Grep, Bash`.
  - Exactly 14 numbered workflow steps in the fresh-decomposition section, in the order defined by the parent plan.
  - Step 2 appears BEFORE step 4 (load existing state before fingerprinting).
  - Step 11 contains sub-steps (a)–(e) listed individually.
  - Step 13 explicitly calls out `decomp_ops.py commit-swap`, the exit-code handling (2 → recover; 3 → halt + surface), and the no-hand-rolled-`mv` rule.
  - Supersede-mode section documents: input-path trigger, single-letter suffix cap, `--skip-parent-rename` on `commit-swap`, three abort codes (`supersede-illegal-state`, `supersede-depth-exceeded`, `supersede-after-execution`).
  - Subcommand-to-Workflow mapping table present, maps all 10 `decomp_ops.py` subcommands + the `recover` + `sync-status` call sites.

**Description:**
The agent is the only component that reads all subcommands together and encodes the end-to-end workflow including supersede detection, dry-run short-circuit, and report composition.

**Reversion guidance:**
Delete the new agent file.

---

## Implementation Playbook

1. Port structure from `plan-executor`'s existing agent (frontmatter shape, tool list, model).
2. Write the Inputs + Input mode detection blocks verbatim from parent plan's Agent Spec.
3. Enumerate the 14 workflow steps; number Step 11 sub-bullets (a)–(e).
4. Add the supersede-mode section with its three abort codes.
5. Add Rules bullets (never-execute, never-modify-source, etc.).
6. Add the Subcommand-to-Workflow mapping table.
