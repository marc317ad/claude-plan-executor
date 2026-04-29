---
task_id: "011"
task_type: standard
status: Pending
source_plan: build-plan-decomposer-plugin.md
source_section: "SKILL.md; Bridge Command"
decomposed_at: 2026-04-17
content_fingerprint: sha256:bootstrap-011
depends_on: ["010"]
superseded_by: []
change_history: []
priority: high
---
<!-- prose_decisions: [prose_omitted_trivial_scope] -->

# TASK-011 — Write decompose-plan skill + bridge command + refresh command

**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)
**Source section:** SKILL.md; Bridge Command
**Base branch:** main
**Chunk dependencies:** 010

---

## Goal

Author the skill orchestrator (`skills/decompose-plan/SKILL.md`), the user-facing bridge command (`commands/decompose-plan.md` — 2-line pass-through to the skill), and a `commands/refresh.md` command cloned from `plan-executor`'s equivalent (rsyncs the plan-decomposer source repo into the installed plugin cache).

## Scoped Context

Skill steps: parse args (`input_path`, optional `output_slug`, optional `tasks_root`, flags) → invoke `decomp_ops.py inspect --mode paths` to resolve all derived paths + `input_mode` in one call → delegate to the `plan-decomposer` agent with a structured prompt including every resolved path, today's date, flags, and the path-info output → pass the agent's report through to the user.

Bridge command frontmatter: `name: decompose-plan`, `description:` covering both modes (freeform plan and too-large TASK). Body = 2 lines invoking the skill via `${CLAUDE_PLUGIN_ROOT}/skills/decompose-plan/SKILL.md` and passing args verbatim.

`refresh.md` mirrors `plan-executor`'s: rsync the plugin source into `~/.claude/plugins/claude-plan-executor/plan-decomposer/`, then prompt the user to `/reload-plugins`.

## Verification

- `plugins/plan-decomposer/skills/decompose-plan/SKILL.md` exists and references `${CLAUDE_PLUGIN_ROOT}/scripts/decomp_ops.py`.
- `plugins/plan-decomposer/commands/decompose-plan.md` frontmatter has `name: decompose-plan` and its body is ≤ 10 lines.
- `plugins/plan-decomposer/commands/refresh.md` exists and references an `rsync` invocation.

---

## Tasks

### TASK-011: Write skill + bridge command + refresh command

- **Status:** open
- **Priority:** high
- **Files:**
  - `plugins/plan-decomposer/skills/decompose-plan/SKILL.md` — new
  - `plugins/plan-decomposer/commands/decompose-plan.md` — new bridge
  - `plugins/plan-decomposer/commands/refresh.md` — new, cloned from plan-executor variant
- **Dependencies:** 010
- **Test command:** `test -f plugins/plan-decomposer/skills/decompose-plan/SKILL.md && test -f plugins/plan-decomposer/commands/decompose-plan.md && test -f plugins/plan-decomposer/commands/refresh.md`
- **Acceptance criteria:**
  - SKILL.md documents the 4-step orchestration protocol (parse args → `inspect --mode paths` → delegate to agent → pass-through report).
  - Bridge command body is ≤ 10 lines and invokes the skill via `${CLAUDE_PLUGIN_ROOT}` reference.
  - `refresh.md` rsync target path is `~/.claude/plugins/claude-plan-executor/plan-decomposer/` (or matches installed plugin cache convention); prompts `/reload-plugins` after success.
  - All three files have valid frontmatter (at minimum `name` + `description` on commands; SKILL.md per existing convention).

**Description:**
Closes the user-facing surface so `/decompose-plan <path>` dispatches end-to-end. Bridge is intentionally thin — all behavior lives in the skill + agent + script.

**Reversion guidance:**
Delete all three new files.
