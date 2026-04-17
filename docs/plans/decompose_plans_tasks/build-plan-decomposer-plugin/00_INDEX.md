# build-plan-decomposer-plugin — Decomposition Index

**Created:** 2026-04-17
**Base branch:** main
**Parent plan:** [`../build-plan-decomposer-plugin.md`](../build-plan-decomposer-plugin.md)

## Why this directory exists

The parent plan specs out a `plan-decomposer` plugin that will eventually mechanically decompose freeform plans into executor-consumable TASK files. Until that plugin exists, this first decomposition is hand-built (a "manual sidecar") so `/implement-plan` can execute the build steps in the right order. Each chunk below is a self-contained mini-plan that plan-analyst will accept and plan-implementer can execute without reading any sibling TASK.

## Chunk roster

| File | Task | Priority | Depends on |
|---|---|---|---|
| [TASK-001_plugin_skeleton.md](TASK-001_plugin_skeleton.md) | Create plan-decomposer plugin skeleton + manifest | critical | — |
| [TASK-002_canonical_contract_constants.md](TASK-002_canonical_contract_constants.md) | Encode Canonical Contract as module-level constants in decomp_ops.py | critical | 001 |
| [TASK-003_inspect_parse_fingerprint_subcommands.md](TASK-003_inspect_parse_fingerprint_subcommands.md) | Implement inspect + parse-source-plan + compute-fingerprint subcommands | high | 002 |
| [TASK-004_build_schedule_and_manifest_subcommands.md](TASK-004_build_schedule_and_manifest_subcommands.md) | Implement build-schedule + manifest subcommands | high | 003 |
| [TASK-005_render_and_templates.md](TASK-005_render_and_templates.md) | Implement render subcommand + 4 template files with concision elision | high | 004 |
| [TASK-006_validate_output_subcommand.md](TASK-006_validate_output_subcommand.md) | Implement validate-output subcommand (prose-budget + coverage gates) | high | 005 |
| [TASK-007_commit_swap_subcommand.md](TASK-007_commit_swap_subcommand.md) | Implement commit-swap subcommand (7-phase atomic protocol) | high | 006 |
| [TASK-008_recover_subcommand.md](TASK-008_recover_subcommand.md) | Implement recover subcommand (W0–W5 crash windows) | high | 007 |
| [TASK-009_sync_status_subcommand.md](TASK-009_sync_status_subcommand.md) | Implement sync-status subcommand | medium | 004 |
| [TASK-010_plan_decomposer_agent.md](TASK-010_plan_decomposer_agent.md) | Write plan-decomposer agent (14-step workflow) | high | 003, 004, 005, 006, 007, 008, 009 |
| [TASK-011_skill_and_commands.md](TASK-011_skill_and_commands.md) | Write decompose-plan skill + bridge command + refresh command | high | 010 |
| [TASK-012_plan_analyst_gate_suppression.md](TASK-012_plan_analyst_gate_suppression.md) | Surgical plan-analyst rule — suppress missing-test-command when task_type:gate | medium | — |
| [TASK-013_tests_units_inputmode_e2e.md](TASK-013_tests_units_inputmode_e2e.md) | Tests — subcommand units + input mode + e2e (cases 1–29, 44–46) | high | 003, 004, 005, 006 |
| [TASK-014_tests_recovery_parentdrift.md](TASK-014_tests_recovery_parentdrift.md) | Tests — recovery + cross-device + parent-drift (cases 30–43) | high | 007, 008 |
| [TASK-015_tests_status_supersede_concision.md](TASK-015_tests_status_supersede_concision.md) | Tests — cross-plugin status + supersede-after-exec + concision (cases 47–61) | high | 005, 006, 009 |
| [TASK-016_readme_update.md](TASK-016_readme_update.md) | Update README with plan-decomposer section + /decompose vs /implement note | medium | 011 |
| [TASK-017_phase_b_shadow_run_gate.md](TASK-017_phase_b_shadow_run_gate.md) | GATE — Phase B shadow run on pinwheel plan | high | 011, 012, 013, 014, 015, 016 |
| [TASK-018_phase_c_permanent_install_gate.md](TASK-018_phase_c_permanent_install_gate.md) | GATE — Phase C permanent install via /plugin install | medium | 017 |

## Execution order

```
                  ┌──► 012 ─────────────────────────────────┐
                  │                                         ▼
001 ──► 002 ──► 003 ──► 004 ──┬──► 005 ──► 006 ──► 007 ──► 008 ──┐
                              │                                  │
                              ├──► 009 ──────────────────────────┤
                              │                                  │
                              └──► 013 (needs 003,004,005,006)   │
                                                                 ▼
                                                                010
                                                                 │
                                                                 ▼
                                                                011
                                                                 │
                              014 (needs 007,008) ──────────────►│
                              015 (needs 005,006,009) ──────────►│
                              016 (needs 011) ───────────────────┤
                                                                 ▼
                                                                017 (gate)
                                                                 │
                                                                 ▼
                                                                018 (gate)
```

Layer summary:

- Layer 0: `001`, `012` (no deps)
- Layer 1: `002`
- Layer 2: `003`
- Layer 3: `004`
- Layer 4: `005`, `009`, `013` (share `003`/`004` prereqs)
- Layer 5: `006`
- Layer 6: `007`
- Layer 7: `008`
- Layer 8: `010` (fans in `003`–`009`)
- Layer 9: `011`, `014`, `015` (share earlier prereqs; independent of each other)
- Layer 10: `016`
- Layer 11: `017` (gate)
- Layer 12: `018` (gate)

## How each chunk is structured

Every TASK file carries YAML frontmatter (stable `task_id`, `task_type`, `status`, `source_plan`, `source_section`, `decomposed_at`, `content_fingerprint`, `depends_on`, `superseded_by`, `change_history`, `priority`), a header block pointing back to this index and the parent plan, an implementer-facing `## Goal` / conditional `## Scoped Context`, a reviewer-facing `## Verification` block with runnable commands, a `## Tasks` section containing every plan-analyst-required field (`Status`, `Priority`, `Files`, `Test command`, `Acceptance criteria`, `Description`, `Reversion guidance`), and conditional `## Implementation Playbook` / `## Out of Scope` blocks elided per the Concision Rules recorded in the `<!-- prose_decisions: [...] -->` comment at the top of the file. Gate TASKs (`017`, `018`) carry a slim body (no Scoped Context / Playbook / Out of Scope) and have `Test command: none`.

## Reference pointers

- [TASK-001_plugin_skeleton.md](TASK-001_plugin_skeleton.md)
- [TASK-002_canonical_contract_constants.md](TASK-002_canonical_contract_constants.md)
- [TASK-003_inspect_parse_fingerprint_subcommands.md](TASK-003_inspect_parse_fingerprint_subcommands.md)
- [TASK-004_build_schedule_and_manifest_subcommands.md](TASK-004_build_schedule_and_manifest_subcommands.md)
- [TASK-005_render_and_templates.md](TASK-005_render_and_templates.md)
- [TASK-006_validate_output_subcommand.md](TASK-006_validate_output_subcommand.md)
- [TASK-007_commit_swap_subcommand.md](TASK-007_commit_swap_subcommand.md)
- [TASK-008_recover_subcommand.md](TASK-008_recover_subcommand.md)
- [TASK-009_sync_status_subcommand.md](TASK-009_sync_status_subcommand.md)
- [TASK-010_plan_decomposer_agent.md](TASK-010_plan_decomposer_agent.md)
- [TASK-011_skill_and_commands.md](TASK-011_skill_and_commands.md)
- [TASK-012_plan_analyst_gate_suppression.md](TASK-012_plan_analyst_gate_suppression.md)
- [TASK-013_tests_units_inputmode_e2e.md](TASK-013_tests_units_inputmode_e2e.md)
- [TASK-014_tests_recovery_parentdrift.md](TASK-014_tests_recovery_parentdrift.md)
- [TASK-015_tests_status_supersede_concision.md](TASK-015_tests_status_supersede_concision.md)
- [TASK-016_readme_update.md](TASK-016_readme_update.md)
- [TASK-017_phase_b_shadow_run_gate.md](TASK-017_phase_b_shadow_run_gate.md)
- [TASK-018_phase_c_permanent_install_gate.md](TASK-018_phase_c_permanent_install_gate.md)
