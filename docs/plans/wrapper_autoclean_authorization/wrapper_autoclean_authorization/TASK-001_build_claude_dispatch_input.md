# TASK-001 — `declared_files_changed` schema-required + `plan_ops.py build-claude-dispatch-input` subcommand + SKILL.md / dispatch-templates.md wiring

## Goal

Make `declared_files_changed` a REQUIRED top-level field in `schemas/claude_dispatch_input.json` (no default), factor wrapper-input construction into a canonical `plan_ops.py build-claude-dispatch-input` subcommand, and update SKILL.md / dispatch-templates.md so every dispatch site uses the canonical builder.

## Context

The `prohibit_silent_revert` plan (committed `8bb0b7b`) hardened the orchestrator surface against silent destruction of completed work: `cmd_fail_task` requires `--authorization-source`; `reconcile_batch` pauses on out-of-scope deltas; the awaiting-user pause subroutine is canonical; `paused` is a first-class plan-status. Its TASK-009 then ran via the bash-dispatched plan-implementer wrapper and **the wrapper destroyed 4 of the 5 implementer-written files** (only `plan_ops.py` survived because it's in the wrapper's protected-paths allowlist).

Two-layer root cause: (Layer A — orchestrator-side) the Phase B / Phase B-rework / Phase D.2b / Phase B-narrow-remediation dispatch payloads in `dispatch-templates.md` build the wrapper input but never populate the top-level `declared_files_changed` field; the schema (`schemas/claude_dispatch_input.json`) declares it as optional with default `[]`. (Layer B — wrapper-side) `plan_claude_dispatch.py:689-693` defaults `declared = []` on missing input; `_claude_dispatch_cleanup.apply_cleanup` then computes `(observed_delta - declared - protected)` and `_restore_path`s every observed delta that isn't protected. No authorization gate, no `awaiting_user` event, no audit-log entry.

This plan extends the **Completed-Work Preservation Principle** (SKILL.md `## Rules`, prohibit_silent_revert TASK-004) into the dispatch-wrapper layer. TASK-001 closes Layer A in two complementary ways:

1. **Schema-required.** Move `declared_files_changed` from optional-with-default to the schema's `required` array; remove `"default": []`. Any wrapper input that omits the field now fails wrapper-level schema validation up front, returning `status: input_invalid` (rather than silently defaulting to empty and proceeding to destructive cleanup). This is a hard wall: Layer A becomes structurally impossible at the wire-protocol level. The wrapper refuses to spawn the agent at all when the field is absent — fail-fast beats silent destruction. Per-Gemini's deep-dive recommendation.

2. **Canonical builder.** A new `plan_ops.py build-claude-dispatch-input` subcommand emits the canonical wrapper input JSON for a given `(plan-file, task-id, variant)` triple, populating `declared_files_changed` from the existing `_extract_task_files_from_plan` helper at `plan_ops.py:8463` (the same helper `_gate_commit_safe` uses at `plan_ops.py:8594`). SKILL.md and `dispatch-templates.md` are updated so the four dispatch sites invoke the subcommand and pipe its stdout into `plan_claude_dispatch.py run --input -`, removing the four hand-built JSON snippets that are the drift vector. The builder ensures every dispatch site gets the field correctly populated; the schema-required gate is the structural backstop if a direct CLI caller bypasses the builder.

### Decisions folded in

1. **Belt and suspenders: schema-required + canonical builder.** Per Gemini's deep-dive: making the field required in the schema is a structural fix that makes Layer A impossible at the wire-protocol layer. Per Codex's deep-dive: the canonical builder is the ergonomic fix that removes the drift vector across four dispatch sites. Both land in this task because they reinforce each other: the schema is the structural backstop, the builder is the ergonomic happy path.
2. **`_extract_task_files_from_plan` is the single source of truth for declared scope.** The same helper that `_gate_commit_safe` uses at `plan_ops.py:8594` for commit-time scope verification. No duplicate parsing logic.
3. **Variant-to-agent mapping is hard-coded.** `default | rework | role-swap → "plan-implementer"`; `narrow-remediation → "plan-remediator"`; `analyst → "plan-analyst"`. Mirrors the existing template-selector pattern in `dispatch-templates.md`.
4. **Per-variant `output_instructions.schema_path` is hard-coded** to the existing per-section references (e.g., `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` for implementer variants).
5. **Phase A-single (plan-analyst) MUST send `declared_files_changed: []` explicitly.** Now that the field is schema-required, every dispatch — including the read-only `plan-analyst` per-child classifier — must emit it. The Phase A-single inline JSON template gets one new line: `"declared_files_changed": []`. This is the orchestrator's explicit declaration that the analyst legitimately has empty scope; the wrapper's authorization gate (TASK-002) then resolves to `wrapper-empty-scope-readonly` based on the agent identity. The change is surgical (one new field in the JSON skeleton); the body below `<!-- TRANSPORT BOUNDARY -->` is unchanged.
6. **Migration is fail-fast.** Any in-flight dispatches that omit the field will immediately fail wrapper schema validation and return `status: input_invalid` rather than running the agent and destroying work. This is the desired behavior — fail-fast beats silent destruction. Operators who hit `input_invalid` on the day this lands are surfaced an actionable error pointing them at the schema-required field; their orchestrator (which is itself Claude reading SKILL.md) can re-build the payload via `build-claude-dispatch-input` and re-dispatch.

## Verification

- `schemas/claude_dispatch_input.json` lists `declared_files_changed` in the top-level `required` array. The field's `default: []` line is removed (the property still has `type: array, items: type: string` but no default; absence at runtime is now a schema violation, not a silent zero).
- `plan_claude_dispatch.py run --input <input with no top-level declared_files_changed>` exits with `status: input_invalid` (the wrapper's existing schema-validation step at `:591` catches the missing field and returns the structured error envelope BEFORE any backend dispatch).
- `python3 plugins/plan-executor/scripts/plan_ops.py build-claude-dispatch-input --plan-file <abs> --task-id 001 --variant default --json` emits a JSON object that:
  - Validates against `schemas/claude_dispatch_input.json`.
  - Carries `agent: "plan-implementer"` (default variant).
  - Carries top-level `declared_files_changed` populated from `_extract_task_files_from_plan(plan_text, task_id)` (non-empty for any task that declares files).
  - Carries `payload.task_id`, `payload.plan_path`, `payload.repo_root`, `payload.target_task_id` (when supplied), and the variant-specific keys per `dispatch-templates.md`.
- `--variant analyst` emits an envelope with `agent: "plan-analyst"`, `declared_files_changed: []` (explicit empty), `overrides.model: "sonnet"`.
- `--variant narrow-remediation --dispatch-context <path>` emits an envelope with `agent: "plan-remediator"`, `payload.dispatch_context` carrying the file's contents.
- `--task-id 999` (not present in plan) exits non-zero with `errors[*].code = "task-not-found"`.
- SKILL.md Phase B (line ~502) replaces inline payload-construction prose with a one-liner that pipes `build-claude-dispatch-input` into `plan_claude_dispatch.py run --input -`. Same change at the three other dispatch sites.
- `dispatch-templates.md` Phase B / Phase B-rework / Phase D.2b / Phase B-narrow-remediation each replace the hand-built JSON skeleton with a one-paragraph note about the canonical builder + a one-line invariant. The agent-behavior body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical.
- `dispatch-templates.md` Phase A-single (plan-analyst classifier, lines 33–67) — the inline payload skeleton gains one new line: `"declared_files_changed": []` (explicit empty for the read-only agent). The body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical.
- New tests in `tests/scripts/test_plan_ops.py` and `tests/scripts/test_plan_claude_dispatch_cli.py` (seven tests below).

## Tasks

### TASK-001: `declared_files_changed` schema-required + `plan_ops.py build-claude-dispatch-input` subcommand + SKILL.md / dispatch-templates.md wiring

- **Status:** done
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/schemas/claude_dispatch_input.json (move `declared_files_changed` to `required` array; remove `default: []`)
  - plugins/plan-executor/scripts/plan_ops.py (NEW `cmd_build_claude_dispatch_input` + argparse subparser; reuses `_extract_task_files_from_plan` at `:8463`)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase B default at `:502`; Phase B-rework / D.2a.5 routing at `:640`; Phase D.2b at `:650`; Phase B-narrow-remediation routing at `:640`'s `dispatch_narrow_remediation` branch — replace inline payload-build prose with one-liner that pipes `build-claude-dispatch-input` stdout into `plan_claude_dispatch.py run --input -`)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (Phase B at `:426–530`, Phase B-rework at `:706`, Phase D.2b at `:694`, Phase B-narrow-remediation at `:766` — replace four JSON skeletons with a single canonical template-literal documenting the subcommand invocation and the resulting `declared_files_changed` shape; ALSO update Phase A-single inline skeleton at `:33–67` to add `"declared_files_changed": []`)
  - tests/scripts/test_plan_ops.py (new fixtures covering each variant of the builder)
  - tests/scripts/test_plan_claude_dispatch_cli.py (new fixture asserting `status: input_invalid` when the field is omitted)
- **Dependencies:** []
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "build_claude_dispatch_input or BuildClaudeDispatchInput" && python3 -m pytest tests/scripts/test_plan_claude_dispatch_cli.py -q -k "declared_files_changed_required or DeclaredFilesChangedRequired"`
- **Acceptance criteria:**
  - `schemas/claude_dispatch_input.json`:
    - The top-level `required` array gains `"declared_files_changed"` (currently it lists `["schema_version", "agent", "payload", "output_instructions", "overrides", "guardrails", "trace"]`; new value: append `"declared_files_changed"`).
    - The `declared_files_changed` property keeps its `type: array, items: type: string` declaration but the `"default": []` line is REMOVED. Absence at runtime is now a schema violation.
    - The `description` field is updated: ``Trusted cleanup-scope authority populated by the orchestrator (TASK-001 sandbox-escape fix; TASK-001 of wrapper_autoclean_authorization made this required). Cleanup reverts any post-dispatch write outside this set. REQUIRED — every dispatch site MUST populate this. Read-only agents (e.g., plan-analyst) emit `[]` explicitly. Build via `plan_ops.py build-claude-dispatch-input` to ensure correct population per task variant.``
  - `cmd_build_claude_dispatch_input` argparse: `--plan-file <path>` (required), `--task-id <NNN>` (required), `--variant <default|rework|role-swap|narrow-remediation|analyst>` (required, closed enum), `--repo-root <path>` (default `Path.cwd()`), `--analyst-annotations <path>` (optional), `--target-task-id <NNN>` (optional), `--starting-sha <sha>` (optional), `--dispatch-context <path>` (optional, JSON file path; used by `rework` and `narrow-remediation` variants), `--output <path-or-->` (default `-` → stdout), `--json` (always-on; reserved flag for parity with sibling subcommands).
  - The subcommand resolves the agent name by variant: `default | rework | role-swap → "plan-implementer"`, `narrow-remediation → "plan-remediator"`, `analyst → "plan-analyst"`.
  - The subcommand resolves `declared_files_changed` by variant: `analyst → []` (read-only — explicit empty); every other variant → `_extract_task_files_from_plan(plan_text, task_id)` (the canonical helper at `plan_ops.py:8463`). If the helper returns `None` (TASK-NNN not found), the subcommand exits non-zero with `errors[*].code = "task-not-found"`. If the helper returns `[]` for a write-authorized variant (the task declares no files — atypical but legal), the subcommand emits an empty list and a stderr warning so operators know the dispatch will hit TASK-002's anti-aliasing guard / TASK-004's short-circuit.
  - The emitted JSON validates against the updated `schemas/claude_dispatch_input.json` for every variant. Specifically: top-level `agent`, `payload`, `output_instructions`, `overrides`, `guardrails`, `trace`, and `declared_files_changed` are all present and well-formed.
  - The emitted `payload` carries the variant-specific keys per `dispatch-templates.md` (e.g., `default` carries `{plan_path, repo_root, task_id, target_task_id?, analyst_annotations, starting_sha}`; `rework` adds `dispatch_context`; `narrow-remediation` carries `{plan_path, repo_root, task_id, dispatch_context}` per `dispatch-templates.md:770`; `analyst` carries `{plan_path, repo_root}` per Phase A-single at `dispatch-templates.md:39–42`).
  - The emitted `overrides.model` is selected by variant: `analyst → "sonnet"`, every other variant → `"opus"`. Mirrors `dispatch-templates.md`'s existing per-section model selectors.
  - SKILL.md Phase B (line ~502) replaces the inline payload-construction prose with: ``Build the wrapper input via `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" build-claude-dispatch-input --plan-file <abs> --task-id NNN --variant default [--target-task-id NNN] [--analyst-annotations <path>] --starting-sha "$STARTING_SHA"` and pipe its stdout into `plan_claude_dispatch.py run --input -`. The subcommand emits the canonical `claude_dispatch_input.json` shape including the now-required top-level `declared_files_changed` derived from the task's `Files:` list — see §dispatch-templates §Phase B for the full skeleton.``
  - Same one-line replacement at the three other dispatch sites (Phase B-rework, Phase D.2b, Phase B-narrow-remediation). Each retains its variant-specific argument list (e.g., the rework site adds `--dispatch-context <findings_for_retry+d5_summary.json>`).
  - `dispatch-templates.md` Phase B (lines 436–477), Phase B-rework (around line 706–714), Phase D.2b (around 694–705), and Phase B-narrow-remediation (around 766–775) are each updated. The hand-built JSON skeleton is replaced with: a one-paragraph note that the canonical builder is `plan_ops.py build-claude-dispatch-input` (with the per-variant arg list), an explicit note that the **top-level `declared_files_changed` is populated from the task's `Files:` list via `_extract_task_files_from_plan`**, and a one-line invariant: ``MUST be threaded through this builder; do NOT hand-craft the input JSON. The wrapper's input schema makes `declared_files_changed` REQUIRED at the top level — omitting it returns `status: input_invalid` and refuses to spawn the agent.`` The agent-behavior body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical to pre-migration.
  - `dispatch-templates.md` Phase A-single (line 33–67, plan-analyst classifier inline JSON skeleton) gets one new line at the top level: `"declared_files_changed": []`. Insert after the `"trace": {...}` block and before the closing `}`. The body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical. A one-line cross-reference is added: ``Direct CLI callers can also use `plan_ops.py build-claude-dispatch-input --variant analyst` to construct this payload.``
  - New tests in `tests/scripts/test_plan_ops.py`:
    - `test_build_claude_dispatch_input_default_variant_populates_declared_files` — invoke the subcommand with a fixture plan containing `**Files:** plugins/foo.py, plugins/bar.py` for TASK-001; assert the emitted JSON's `declared_files_changed == ["plugins/foo.py", "plugins/bar.py"]`.
    - `test_build_claude_dispatch_input_analyst_variant_emits_empty_declared` — `--variant analyst`; assert `declared_files_changed == []` AND `agent == "plan-analyst"`.
    - `test_build_claude_dispatch_input_unknown_task_id_errors` — `--task-id 999` with no such task; assert non-zero exit + `errors[*].code == "task-not-found"`.
    - `test_build_claude_dispatch_input_validates_against_input_schema` — every variant's emitted JSON validates against `schemas/claude_dispatch_input.json`.
    - `test_build_claude_dispatch_input_rework_variant_threads_dispatch_context` — `--variant rework --dispatch-context <path>`; assert `payload.dispatch_context` contains the contents of the file.
    - `test_build_claude_dispatch_input_narrow_remediation_variant_selects_remediator_agent` — `--variant narrow-remediation`; assert `agent == "plan-remediator"` AND `overrides.model == "opus"`.
  - New tests in `tests/scripts/test_plan_claude_dispatch_cli.py`:
    - `test_run_emits_input_invalid_when_declared_files_changed_omitted` — submit a wrapper input that omits `declared_files_changed` (otherwise valid); assert exit code `EXIT_CODE_WRAPPER_FAILURE` AND envelope `status == "input_invalid"` AND `error.code == "input_invalid"` AND `error.message` mentions `declared_files_changed`.
    - `test_run_accepts_explicit_empty_declared_files_changed_for_plan_analyst` — submit `agent: "plan-analyst"` with `declared_files_changed: []` (explicit); assert the wrapper does NOT emit `input_invalid` (the empty-but-present case is legal for read-only agents).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/schemas/claude_dispatch_input.json plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md tests/scripts/test_plan_ops.py tests/scripts/test_plan_claude_dispatch_cli.py`

**Description:**
Foundation task. Closes Layer A of the wrapper-autoclean defect with two complementary changes:

- **Schema-required (Gemini's recommendation).** Moving `declared_files_changed` to the schema's `required` array and removing the `default: []` makes Layer A structurally impossible at the wire-protocol layer. Any wrapper input that omits the field fails wrapper-level schema validation upfront and returns `status: input_invalid` — the wrapper refuses to spawn the agent at all. Fail-fast beats silent destruction.
- **Canonical builder (Codex's recommendation).** Centralizing the wrapper-input construction in one place — the `_extract_task_files_from_plan` helper that `_gate_commit_safe` already uses — so the four dispatch sites cannot drift, and every Claude-tier dispatch emits a non-empty `declared_files_changed` for write-authorized agents.

The two changes reinforce each other: the schema is the structural backstop (any caller that omits the field fails), and the builder is the ergonomic happy path (no caller needs to construct the JSON by hand).

**Implementation notes.** The subcommand's variant-to-agent mapping is hard-coded; per-variant `output_instructions.schema_path` mirrors `dispatch-templates.md`'s existing references (e.g., `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` for `default | rework | role-swap`). The `payload.dispatch_context` value for `rework` and `narrow-remediation` is read from a JSON file (orchestrator-supplied; the orchestrator already builds these via `review-route`'s `dispatch_context` output). The `target_task_id` injection rule (TASK-007 of POSTMORTEM_FIXES_2026-04-25) is pre-existing and continues to live in the wrapper's render path; this builder simply forwards the value as a payload field. No template-rendering logic moves into `plan_ops.py`.

The Phase A-single template gets a tiny update — one new line `"declared_files_changed": []` — because once the schema makes the field required, every dispatch must emit it (including the read-only `plan-analyst` per-child classifier). The empty list is the explicit declaration that the analyst legitimately has empty scope; TASK-002's authorization gate then maps the `agent: "plan-analyst"` identity to `wrapper-empty-scope-readonly` for the cleanup pathway.

**Migration risk.** Once the schema change lands, any in-flight dispatch from before this task that omits `declared_files_changed` will fail wrapper-level schema validation and return `status: input_invalid` instead of running the agent. This is the desired behavior — the alternative is the silent destruction we're trying to eliminate. Operators who hit this on the day this task lands will get an actionable error pointing at the field; their orchestrator (which is itself Claude reading SKILL.md) re-builds the payload via `build-claude-dispatch-input` and re-dispatches. There's no safe way to roll out this fix that preserves "old payload shape works" — that shape IS the silent-destruction vector.

## Execution log — 20260427T213500 (paused)

Starting SHA: `feba7001d79e5455860b46f0f9fe857286f3aa7f`  → Ending SHA: `feba7001d79e5455860b46f0f9fe857286f3aa7f`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude | none | paused (post_implement_failure) | - | backend timeout @ 900s; 6 declared files modified in working tree, preserved per Completed-Work Preservation Principle; awaiting user disposition |
