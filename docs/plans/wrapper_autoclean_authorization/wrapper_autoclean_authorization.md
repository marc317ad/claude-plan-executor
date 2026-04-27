# Wrapper-Autoclean Authorization — Extend the Completed-Work Preservation Principle into the Dispatch Wrapper Layer

**Created:** 2026-04-27
**Status:** pending
**Base branch:** main
**Design reference:** `docs/plans/analysis/2026-04-26_wrapper_autoclean_authorization_gap.md`
**Related:**
- `docs/plans/prohibit_silent_revert/prohibit_silent_revert.md` (the just-shipped 8-task plan that hardened the orchestrator surface; this plan extends that work to the wrapper layer)
- `docs/analysis/2026-04-24_completed_work_preservation_principle.md` (the principle this plan extends)
- `docs/bugs/BUG_144_2026-04-26_OPEN_BASH-DISPATCH-MIGRATION.md` (sibling defect surfaced from the same bash-dispatch migration)
**Preconditions:** `prohibit_silent_revert` shipped through TASK-008 (committed `8bb0b7b`). TASK-009 of that plan is paused with the implementer's work partially destroyed (4 of 5 files reverted by the wrapper); the recovered work — chiefly the +228-line `_check_fail_task_authorization_source` audit + extended `_check_principle_referenced` in `plan_ops.py` — survives because `plan_ops.py` is in the wrapper's protected-paths allowlist.

## Goal

Extend the **Completed-Work Preservation Principle** from the orchestrator surface (where prohibit_silent_revert TASK-001..008 enforce it) into the **dispatch-wrapper layer** (`plan_claude_dispatch.py`, `_claude_dispatch_cleanup.py`, `plan_codex_dispatch.py`). Today, when the orchestrator dispatches a Claude-tier implementer/remediator via the bash wrapper without populating the top-level `declared_files_changed` field, the wrapper silently `git restore`s every observed write that isn't in its protected-paths allowlist — destroying verifiably-correct work without surfacing the situation to the user. This is the wrapper-layer counterpart to the gap that prohibit_silent_revert closed at the orchestrator surface; it must close the same way: a closed-enum `authorization_source` gate, a refuse-to-clean default when authorization is absent, and a `wrapper_autoclean_blocked` envelope status that the orchestrator routes through the existing `Awaiting-user pause` subroutine.

The plan has two layers:

- **Layer A — orchestrator-side declared-scope population (schema-required + canonical builder).** The Phase B / Phase B-rework / Phase D.2b / Phase B-narrow-remediation dispatch payloads in `dispatch-templates.md` and the SKILL.md wiring at `:502` build the wrapper input but never populate the top-level `declared_files_changed` field; the schema (`schemas/claude_dispatch_input.json`) declares it as optional with default `[]`. The fix has two complementary parts. **Schema-required (Gemini's recommendation):** move `declared_files_changed` to the schema's `required` array and remove the `default: []`, so any wrapper input that omits the field fails wrapper-level schema validation upfront and returns `status: input_invalid` — fail-fast beats silent destruction. **Canonical builder (Codex's recommendation):** factor a single `plan_ops.py build-claude-dispatch-input --task-id NNN --plan-file <path> --variant <default|rework|role-swap|narrow-remediation|analyst>` subcommand that emits the wrapper's input JSON with `declared_files_changed` derived from the task's `Files:` list via the existing `_extract_task_files_from_plan` helper. SKILL.md tells the orchestrator to invoke that subcommand and pipe its stdout into the wrapper, removing the four hand-built JSON snippets. The two changes reinforce each other: the schema is the structural backstop (any caller that omits the field fails); the builder is the ergonomic happy path (no caller needs to construct the JSON by hand). The Phase A-single (plan-analyst) inline JSON skeleton picks up one new line — `"declared_files_changed": []` — because once the field is required, even read-only agents must emit it explicitly.

- **Layer B — wrapper-side authorization gate.** `_claude_dispatch_cleanup.apply_cleanup` adds an `authorization_source` keyword-only parameter with a sentinel default that raises on omission. Initial enum: `wrapper-declared-scope` (orchestrator passed a non-empty `declared_files_changed` for a write-authorized agent), `wrapper-empty-scope-readonly` (read-only agent — `plan-analyst` — legitimately has empty declared scope; cleanup still proceeds). Mismatched authorization (e.g., `wrapper-empty-scope-readonly` paired with a write-authorized agent identity) → no cleanup; the wrapper returns an envelope with `status: scope_violation`, `error.code: wrapper_autoclean_blocked`, and the observed deltas surfaced in `scope.observed_delta_tracked/untracked` so the orchestrator can route through the existing `Awaiting-user pause` subroutine. The same gate is mirrored at `plan_codex_dispatch.py:_restore_in_scope` (currently called from `_handle_timeout_cleanup`) so the principle holds at every wrapper-layer mutation site. An AST-walk audit drift check in `plan_ops.py audit` enforces that every `_git(["restore", ...])` / `_git(["reset", "--hard", ...])` / `Path.unlink()` / `os.unlink()` call inside `plan_*_dispatch.py` and `_claude_dispatch_cleanup.py` is reachable only via an authorization-gated call site, so future contributors cannot reintroduce the gap accidentally.

This plan does NOT touch:
- `cmd_commit_task`'s metadata-only rollback (preserves work — already correct, not a wrapper-layer concern).
- The orchestrator-side `reconcile_batch` pause path (already hardened by prohibit_silent_revert TASK-008).
- The agent's own scope discipline (its `Files:` list is the authority — TASK-009 of prohibit_silent_revert reinforces that in the dispatch templates and the agent spec).
- The dispatch-template body text below the `<!-- TRANSPORT BOUNDARY -->` markers (TASK-009 of prohibit_silent_revert is the authoritative source for that prose; this plan only changes the transport header — the JSON skeleton above the boundary).
- Dry-run mode (`--dry-run` short-circuits before cleanup; no behavior change).

### Decisions folded in

1. **Authorization-source is a sentinel-default keyword param, not a positional argument.** `apply_cleanup(baseline, declared_files_changed, repo_root, *, authorization_source=_MISSING_AUTHORIZATION_SOURCE)`. Missing → `TypeError("apply_cleanup requires authorization_source; expected one of {...}")`. Unknown value → `ValueError`. This keeps the existing positional test seams readable, makes omissions fail loudly at the call site that introduced them, and surfaces the contract in a stack trace any reviewer can read.

2. **Wrapper status enum stays closed; we reuse `scope_violation` with a structured `error.code`.** Adding a new top-level wrapper status (`wrapper_autoclean_blocked`) would require coordinated edits across `claude_dispatch_output.json`, `_claude_dispatch_envelope.STATUS_VOCABULARY`, the SKILL.md routing table at `:74`, and every consumer of the §7 envelope. Instead, the new failure mode reuses `status: "scope_violation"` and is distinguished by `error.code: "wrapper_autoclean_blocked"`. `build_scope_violation()` is relaxed to accept an `error_code` parameter (default `"scope_violation"` preserves all existing call-sites). The orchestrator's routing ladder reads `error.code` to distinguish the new "preserve everything pending user" path from the existing "wrapper reverted out-of-scope writes" path.

3. **The orchestrator owns run-log mutation; the wrapper signals via the envelope.** The wrapper does not emit `wrapper_autoclean_executed` / `wrapper_autoclean_blocked` directly to `_run_log.jsonl` — wrapper code never writes orchestrator logs. Instead the wrapper carries the signal in the envelope (`error.code` + the scope sub-object), the orchestrator parses it at the dispatch site, and `claude_dispatch_failed` (existing event) carries the new `error.code` value forward. The two new event names are reserved in `ALLOWED_LOG_EVENTS` for orchestrator emission paths that surface the wrapper's signal to the run log explicitly (future-proofing; not load-bearing for this plan's behavior).

4. **Centralized payload builder over inline template edits.** `plan_ops.py build-claude-dispatch-input` is a new subcommand that emits the wrapper input JSON for a given `(plan-file, task-id, variant)` triple, populating `declared_files_changed` from `_extract_task_files_from_plan` (the canonical helper that `_gate_commit_safe` already uses at `plan_ops.py:8594`). SKILL.md and `dispatch-templates.md` are updated to invoke the subcommand and pipe its stdout into `plan_claude_dispatch.py run --input -`. The four hand-built JSON skeletons in `dispatch-templates.md` (Phase B default, Phase B-rework, Phase D.2b, Phase B-narrow-remediation) are replaced with a single template-literal that documents the canonical invocation.

5. **Read-only agent identity is the discriminator for `wrapper-empty-scope-readonly`.** The `plan-analyst` per-child classifier legitimately has empty declared scope (it never writes). The new authorization enum value `wrapper-empty-scope-readonly` is selected at the dispatch call site only when the agent identity is in the read-only set (currently `{plan-analyst}`). It is not a fallback that any caller can pass casually; the orchestrator's payload builder selects the value based on the agent name. Wrong choice → cleanup proceeds, which is safe-by-default for a read-only agent (it should have written nothing; if it did, we want to know). Right choice → empty declared scope is treated as authorized, cleanup runs, and any observed delta is reverted as out-of-scope (correct: a read-only agent that wrote anything violated its contract).

6. **Protected-paths allowlist stays as a second line of defense.** `PROTECTED_PATH_PREFIXES` (`_plan_paths.py:31`) protects executor infrastructure (`plan_ops.py`, `plan_codex_dispatch.py`, `.claude/`, `.codex/`, `_run_log.jsonl`, `*.schedule.json`) from all wrapper cleanup unconditionally. This survives TASK-009's destruction event — only `plan_ops.py` survived — and remains the right "even if every other gate fails" backstop. The asymmetry that `plan_claude_dispatch.py` and `_claude_dispatch_cleanup.py` are NOT in the prefix list is deliberate (they are the wrappers themselves, not consumers); TASK-002 documents that with a one-line comment but does not change the membership.

7. **No new wrapper status enum value, no new orchestrator state machine.** The orchestrator-side response to a `wrapper_autoclean_blocked` envelope reuses the existing `Awaiting-user pause` subroutine (prohibit_silent_revert TASK-004). New `stage` value: `post_wrapper_autoclean_blocked`. The existing per-stage payload contract extends with one row. No new enum value on `cmd_fail_task --authorization-source` is needed for the pause itself; user-instructed disposition in the next turn uses the existing `user-instruction` value.

## Verification

1. `schemas/claude_dispatch_input.json` lists `declared_files_changed` in the top-level `required` array. The field's `default: []` line is removed.
2. `plan_claude_dispatch.py run --input <input with no top-level declared_files_changed>` exits with `status: input_invalid` (the wrapper's existing schema-validation step catches the missing field BEFORE any backend dispatch).
3. `python3 plugins/plan-executor/scripts/plan_ops.py build-claude-dispatch-input --plan-file <abs> --task-id 001 --variant default --json` emits a JSON object that:
   - Validates against `schemas/claude_dispatch_input.json`.
   - Carries `agent: "plan-implementer"` (default variant) or `agent: "plan-remediator"` (narrow-remediation variant).
   - Carries top-level `declared_files_changed: [<task Files list, normalized>]` (non-empty for any task that declares files; explicitly empty for `--variant analyst`).
   - Carries `payload.task_id`, `payload.plan_path`, `payload.repo_root`, and the variant-specific keys per `dispatch-templates.md`.
4. `python3 -c "from _claude_dispatch_cleanup import apply_cleanup; apply_cleanup({}, [], '/tmp')"` exits with `TypeError` (missing `authorization_source`).
5. `apply_cleanup(baseline, [], repo, authorization_source='wrapper-declared-scope')` with a non-empty observed delta returns `cleanup_strategy: "skipped_authorization_blocked"` (TASK-004 short-circuit fires; all observed deltas in `out_of_scope_paths`, none reverted).
6. `apply_cleanup(baseline, [], repo, authorization_source='wrapper-empty-scope-readonly')` with a non-empty observed delta returns `cleanup_strategy: "delta_bounded"` and reverts the deltas (read-only agent that wrote anything violated its contract).
7. `apply_cleanup(baseline, ['x.py'], repo, authorization_source='wrapper-bogus-value')` exits with `ValueError`.
8. `plan_claude_dispatch.py run --input <input with declared_files_changed: [] AND agent: plan-implementer>` against an agent that wrote files emits an envelope with `status: "scope_violation"` and `error.code: "wrapper_autoclean_blocked"`. The observed deltas surface in `scope.observed_delta_tracked/untracked`. The working tree is unchanged (files preserved).
9. `plan_codex_dispatch.py implement` on a Codex timeout with non-empty observed delta calls `_restore_in_scope` only when authorized via the new enum. Missing authorization → `TypeError` raised at call site (caught by `cmd_implement` and surfaced as a `failure` envelope with structured error rather than a silent crash).
10. `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json` passes, including the new `wrapper_autoclean_authorization` AST-walk check that scans `plugins/plan-executor/scripts/plan_*_dispatch.py` and `_claude_dispatch_cleanup.py` for ungated `_git(["restore", ...])` / `_git(["reset", "--hard", ...])` / `Path.unlink()` / `os.unlink()` callsites.
11. `python3 -m pytest -q tests/scripts/test_wrapper_autoclean_authorization.py` passes; the suite exercises (a) `apply_cleanup` missing-auth raises, (b) read-only-agent path, (c) empty-declared-with-write-authorized path emits `wrapper_autoclean_blocked`, (d) one parametrized end-to-end test across the four affected dispatch sites (Phase B default, Phase B-rework, Phase D.2b role-swap, Phase B-narrow-remediation) verifying the implementer's work is preserved when the wrapper would otherwise revert it.
12. `python3 -m pytest -q tests/scripts/test_claude_dispatch_cleanup.py tests/scripts/test_plan_codex_dispatch_*.py tests/scripts/test_skill_dispatch_*.py` continues to pass (no regression in existing wrapper test suites; updated test calls thread `authorization_source` through).
13. `git grep -nE 'apply_cleanup\\(' plugins/plan-executor/scripts/ tests/scripts/` returns zero call sites that omit `authorization_source`. Same for `_restore_in_scope(` in `plan_codex_dispatch.py`.

## TASK-001: `declared_files_changed` schema-required + `plan_ops.py build-claude-dispatch-input` subcommand + SKILL.md / dispatch-templates.md wiring

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/schemas/claude_dispatch_input.json (move `declared_files_changed` to top-level `required` array; remove `default: []`)
  - plugins/plan-executor/scripts/plan_ops.py (NEW `cmd_build_claude_dispatch_input` + argparse subparser; reuses `_extract_task_files_from_plan` at `:8463`)
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Phase B default at `:502`, Phase B-rework / D.2a.5 routing at `:640`, Phase D.2b at `:650`, Phase B-narrow-remediation at `:640`'s `dispatch_narrow_remediation` branch — replace inline payload-build prose with one-liner that pipes `build-claude-dispatch-input` stdout into `plan_claude_dispatch.py run --input -`)
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (Phase B at `:426–530`, Phase B-rework at `:706`, Phase D.2b at `:694`, Phase B-narrow-remediation at `:766` — replace the four JSON skeletons with a single canonical template-literal; ALSO update Phase A-single inline skeleton at `:33–67` to add `"declared_files_changed": []`)
  - tests/scripts/test_plan_ops.py (new fixtures covering each variant of the builder)
  - tests/scripts/test_plan_claude_dispatch_cli.py (new fixture asserting `status: input_invalid` when the field is omitted)
- **Dependencies:** []
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "build_claude_dispatch_input or BuildClaudeDispatchInput" && python3 -m pytest tests/scripts/test_plan_claude_dispatch_cli.py -q -k "declared_files_changed_required or DeclaredFilesChangedRequired"`
- **Acceptance criteria:**
  - `schemas/claude_dispatch_input.json` lists `declared_files_changed` in the top-level `required` array. The field's `default: []` line is removed. Description updated to note the field is now required.
  - `plan_claude_dispatch.py run --input <input with no top-level declared_files_changed>` exits with `status: input_invalid` (the wrapper's existing schema-validation step at `:591` catches the missing field BEFORE any backend dispatch).
  - `cmd_build_claude_dispatch_input` argparse: `--plan-file <path>` (required), `--task-id <NNN>` (required), `--variant <default|rework|role-swap|narrow-remediation|analyst>` (required, closed enum), `--repo-root <path>` (default `Path.cwd()`), `--analyst-annotations <path>` (optional), `--target-task-id <NNN>` (optional), `--starting-sha <sha>` (optional), `--dispatch-context <path>` (optional, JSON file path; used only by `rework` and `narrow-remediation` variants), `--output <path-or-->` (default `-` → stdout), `--json` (always-on; reserved flag for parity with sibling subcommands).
  - The subcommand resolves the agent name by variant: `default | rework | role-swap → "plan-implementer"`, `narrow-remediation → "plan-remediator"`, `analyst → "plan-analyst"`.
  - The subcommand resolves `declared_files_changed` by variant: `analyst → []` (read-only — explicit empty); every other variant → `_extract_task_files_from_plan(plan_text, task_id)` (the canonical helper at `plan_ops.py:8463`). If the helper returns `None` (TASK-NNN not found), the subcommand exits non-zero with `errors[*].code = "task-not-found"`.
  - The emitted JSON validates against the updated `schemas/claude_dispatch_input.json` for every variant. Specifically: top-level `agent`, `payload`, `output_instructions`, `overrides`, `guardrails`, `trace`, and `declared_files_changed` are all present and well-formed.
  - The emitted `payload` carries the variant-specific keys per `dispatch-templates.md` (e.g., `default` carries `{plan_path, repo_root, task_id, target_task_id?, analyst_annotations, starting_sha}`; `rework` adds `dispatch_context`; `narrow-remediation` carries `{plan_path, repo_root, task_id, dispatch_context}` per `dispatch-templates.md:770`; `analyst` carries `{plan_path, repo_root}`).
  - The emitted `overrides.model` is selected by variant: `analyst → "sonnet"`, every other variant → `"opus"`. Mirrors `dispatch-templates.md`'s existing per-section model selectors.
  - SKILL.md Phase B (line ~502) replaces the inline payload-construction prose with: ``Build the wrapper input via `$PYTHON "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" build-claude-dispatch-input --plan-file <abs> --task-id NNN --variant default [--target-task-id NNN] [--analyst-annotations <path>] --starting-sha "$STARTING_SHA"` and pipe its stdout into `plan_claude_dispatch.py run --input -`. The subcommand emits the canonical `claude_dispatch_input.json` shape including the now-required top-level `declared_files_changed` derived from the task's `Files:` list — see §dispatch-templates §Phase B for the full skeleton.``
  - Same one-line replacement at the three other dispatch sites (Phase B-rework, Phase D.2b, Phase B-narrow-remediation). Each retains its variant-specific argument list (e.g., the rework site adds `--dispatch-context <findings_for_retry+d5_summary.json>`).
  - `dispatch-templates.md` Phase B (lines 436–477), Phase B-rework (around line 706–714), Phase D.2b (around 694–705), and Phase B-narrow-remediation (around 766–775) are each updated. The hand-built JSON skeleton is replaced with: a one-paragraph note that the canonical builder is `plan_ops.py build-claude-dispatch-input` (with the per-variant arg list), an explicit note that the **top-level `declared_files_changed` is populated from the task's `Files:` list via `_extract_task_files_from_plan`**, and a one-line invariant: ``MUST be threaded through this builder; do NOT hand-craft the input JSON. The wrapper's input schema makes `declared_files_changed` REQUIRED at the top level — omitting it returns `status: input_invalid` and refuses to spawn the agent.`` The agent-behavior body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical to pre-migration.
  - `dispatch-templates.md` Phase A-single (line 33–67, plan-analyst classifier inline JSON skeleton) gets one new line at the top level: `"declared_files_changed": []`. Insert after the `"trace": {...}` block. The body below the `<!-- TRANSPORT BOUNDARY -->` marker is byte-identical. A one-line cross-reference is added: ``Direct CLI callers can also use `plan_ops.py build-claude-dispatch-input --variant analyst` to construct this payload.``
  - New tests:
    - `test_build_claude_dispatch_input_default_variant_populates_declared_files` — invoke the subcommand with a fixture plan containing `**Files:** plugins/foo.py, plugins/bar.py` for TASK-001; assert the emitted JSON's `declared_files_changed == ["plugins/foo.py", "plugins/bar.py"]`.
    - `test_build_claude_dispatch_input_analyst_variant_emits_empty_declared` — `--variant analyst`; assert `declared_files_changed == []` AND `agent == "plan-analyst"`.
    - `test_build_claude_dispatch_input_unknown_task_id_errors` — `--task-id 999` with no such task; assert non-zero exit + `errors[*].code == "task-not-found"`.
    - `test_build_claude_dispatch_input_validates_against_input_schema` — every variant's emitted JSON validates against `schemas/claude_dispatch_input.json`.
    - `test_build_claude_dispatch_input_rework_variant_threads_dispatch_context` — `--variant rework --dispatch-context <path>`; assert `payload.dispatch_context` contains the contents of the file.
    - `test_build_claude_dispatch_input_narrow_remediation_variant_selects_remediator_agent` — `--variant narrow-remediation`; assert `agent == "plan-remediator"` AND `overrides.model == "opus"`.
    - `test_run_emits_input_invalid_when_declared_files_changed_omitted` — submit a wrapper input that omits `declared_files_changed` (otherwise valid); assert exit code `EXIT_CODE_WRAPPER_FAILURE` AND envelope `status == "input_invalid"` AND `error.message` mentions `declared_files_changed`.
    - `test_run_accepts_explicit_empty_declared_files_changed_for_plan_analyst` — submit `agent: "plan-analyst"` with `declared_files_changed: []` (explicit); assert the wrapper does NOT emit `input_invalid`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/schemas/claude_dispatch_input.json plugins/plan-executor/scripts/plan_ops.py plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/skills/implement-plan/dispatch-templates.md tests/scripts/test_plan_ops.py tests/scripts/test_plan_claude_dispatch_cli.py`

**Description:**
Foundation task. Closes Layer A with two complementary changes: schema-required (Gemini's recommendation; Layer A becomes structurally impossible at the wire-protocol layer) and canonical builder (Codex's recommendation; removes drift across four dispatch sites). The Phase A-single template picks up `"declared_files_changed": []` to satisfy the now-required schema for the read-only plan-analyst payload.

**Implementation notes.** The subcommand's variant-to-agent mapping is hard-coded; per-variant `output_instructions.schema_path` mirrors `dispatch-templates.md`'s existing references (e.g., `tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json` for `default | rework | role-swap`). The `payload.dispatch_context` value for `rework` and `narrow-remediation` is read from a JSON file (orchestrator-supplied; the orchestrator already builds these via `review-route`'s `dispatch_context` output). The `payload.target_task_id` injection rule (TASK-007 of POSTMORTEM_FIXES_2026-04-25) is pre-existing and continues to live in the wrapper's render path; this builder simply forwards the value as a payload field. No template-rendering logic moves into `plan_ops.py`.

**Migration risk.** Once the schema change lands, any in-flight dispatch from before this task that omits `declared_files_changed` will fail wrapper-level schema validation and return `status: input_invalid` instead of running the agent. This is the desired behavior — the alternative is the silent destruction we're trying to eliminate. Operators who hit this on the day this task lands will get an actionable error pointing at the field; their orchestrator (which is itself Claude reading SKILL.md) re-builds the payload via `build-claude-dispatch-input` and re-dispatches.

## TASK-002: `_claude_dispatch_cleanup.apply_cleanup` authorization gate + plan_claude_dispatch.py call-site update

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/_claude_dispatch_cleanup.py (`apply_cleanup` signature + early gate at line 564 — before the `_changed_paths()` call at line 589 and the classification loop at line 662)
  - plugins/plan-executor/scripts/plan_claude_dispatch.py (`cmd_run` step 9 at `:697` — pass `authorization_source="wrapper-declared-scope"` for write-authorized agents and `"wrapper-empty-scope-readonly"` for `plan-analyst`)
  - tests/scripts/test_claude_dispatch_cleanup.py (every `apply_cleanup(...)` call threads `authorization_source` — ~30 call sites)
- **Dependencies:** [001]
- **Test command:** `python3 -m pytest tests/scripts/test_claude_dispatch_cleanup.py -q && python3 -m pytest tests/scripts/test_plan_claude_dispatch_cli.py -q -k "authorization_source or AuthorizationSource"`
- **Acceptance criteria:**
  - `_claude_dispatch_cleanup.py` module-level constants:
    - `_MISSING_AUTHORIZATION_SOURCE = object()` (sentinel).
    - `ALLOWED_CLEANUP_AUTHORIZATION_SOURCES: frozenset[str] = frozenset({"wrapper-declared-scope", "wrapper-empty-scope-readonly"})`.
    - A docstring comment block above the constants enumerates each value and what it authorizes (mirrors `cmd_fail_task`'s `--authorization-source` enum docstring pattern from prohibit_silent_revert TASK-001).
  - `apply_cleanup` signature gains a keyword-only param: `*, authorization_source: object = _MISSING_AUTHORIZATION_SOURCE`. Body raises:
    - `TypeError("apply_cleanup requires authorization_source; expected one of " + sorted(ALLOWED_CLEANUP_AUTHORIZATION_SOURCES))` when `authorization_source is _MISSING_AUTHORIZATION_SOURCE`.
    - `ValueError("apply_cleanup: unknown authorization_source <value>; expected one of {sorted}")` when `authorization_source not in ALLOWED_CLEANUP_AUTHORIZATION_SOURCES`.
    - The check fires **before** `_changed_paths()` (line 589) and **before** the classification loop (line 662), so a missing/bad value cannot silently revert anything.
  - `plan_claude_dispatch.py:cmd_run` step 9 (around `:697`) constructs the `authorization_source` value explicitly: `authorization_source = "wrapper-empty-scope-readonly" if agent_name == "plan-analyst" else "wrapper-declared-scope"`. The value is passed to `apply_cleanup` as a keyword argument. A one-line comment documents the discriminator.
  - The wrapper additionally validates that `agent_name == "plan-analyst"` IFF `declared_files_changed` is empty (anti-aliasing guard): if a write-authorized agent's input arrives with empty `declared_files_changed`, the wrapper does NOT pass `wrapper-empty-scope-readonly` (that would be a Layer-A regression silently re-introducing the destruction) — instead it routes through TASK-004's schema-fail short-circuit (emit `error.code: wrapper_autoclean_blocked`).
  - All existing callers of `apply_cleanup` in tests are updated to thread `authorization_source` through. No silent-default fallback in test code.
  - `_claude_dispatch_cleanup.py` module docstring (lines 1–53) gains a new section under "Public API" titled "Authorization gate" that documents the new contract.
  - A one-line comment in `_plan_paths.py` near `PROTECTED_PATH_PREFIXES` (line 31) documents that `plan_claude_dispatch.py` and `_claude_dispatch_cleanup.py` are NOT in the prefix list because they ARE the wrapper layer (not consumers); the protected-paths set protects executor infrastructure that the wrapper might mutate, not the wrapper itself.
  - New tests:
    - `test_apply_cleanup_raises_typeerror_without_authorization_source` — call without the param; assert `TypeError` with "expected one of" in message.
    - `test_apply_cleanup_raises_valueerror_on_unknown_authorization_source` — bogus value; assert `ValueError`.
    - `test_apply_cleanup_wrapper_declared_scope_with_empty_declared_reverts_all` — `declared=[]` and authorized; observed delta gets reverted (legacy semantic preserved when authorization is explicit).
    - `test_apply_cleanup_wrapper_empty_scope_readonly_with_empty_declared_reverts_all` — same as above but via the readonly path (a read-only agent that wrote anything violated its contract).
    - `test_apply_cleanup_wrapper_declared_scope_with_nonempty_declared_preserves_in_scope` — `declared=["x.py"]`; observed delta on `x.py` is preserved, on `y.py` is reverted.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/_claude_dispatch_cleanup.py plugins/plan-executor/scripts/plan_claude_dispatch.py plugins/plan-executor/scripts/_plan_paths.py tests/scripts/test_claude_dispatch_cleanup.py`

**Description:**
Closes Layer B of the defect for the Claude wrapper. The sentinel-default keyword-only param keeps existing positional test seams readable while making omissions fail loudly. The agent-identity-based discriminator (`plan-analyst` ↔ `wrapper-empty-scope-readonly`; everyone else ↔ `wrapper-declared-scope`) anchors the authorization decision in the dispatch payload's agent field rather than allowing any caller to opt out by passing the readonly value casually. The anti-aliasing guard (write-authorized agent + empty declared scope → DON'T fall back to readonly) is the load-bearing fix for the TASK-009 destruction event: even after Layer A is in place, a buggy or rolled-back orchestrator that fails to populate `declared_files_changed` will trip Layer B and surface the situation rather than re-destroy work.

## TASK-003: `plan_codex_dispatch.py:_restore_in_scope` authorization gate + `_handle_timeout_cleanup` call-site update

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_codex_dispatch.py (`_restore_in_scope` at `:894`; `_handle_timeout_cleanup` at `:1003` — call-site at `:1084`)
  - tests/scripts/test_plan_codex_dispatch.py (or the cleanup-specific test file; whichever currently exercises `_restore_in_scope`) — every call threads `authorization_source` through
- **Dependencies:** [002]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_codex_dispatch*.py -q -k "restore_in_scope or RestoreInScope or timeout_cleanup or TimeoutCleanup"`
- **Acceptance criteria:**
  - `plan_codex_dispatch.py` module-level constants (mirroring TASK-002 in shape):
    - `_MISSING_AUTHORIZATION_SOURCE = object()` (separate sentinel from the Claude wrapper's; the two wrappers share no state).
    - `ALLOWED_RESTORE_AUTHORIZATION_SOURCES: frozenset[str] = frozenset({"wrapper-codex-timeout-cleanup"})`.
    - A docstring comment block above the constants enumerates the value and what it authorizes (Codex's `_restore_in_scope` is reachable only from `_handle_timeout_cleanup` today; the enum is single-valued because the other wrapper-layer cleanup paths in `plan_codex_dispatch.py` are observe-only by design — `validate_scope` does NOT mutate).
  - `_restore_in_scope` signature gains a keyword-only param: `*, authorization_source: object = _MISSING_AUTHORIZATION_SOURCE`. Body raises `TypeError` / `ValueError` with the same shape as TASK-002. The check fires **before** any `_git(["restore", ...])` or `Path.unlink()` call in the function body.
  - `_handle_timeout_cleanup` (`:1003`) call-site at `:1084` is updated to pass `authorization_source="wrapper-codex-timeout-cleanup"`.
  - The Codex wrapper's `validate_scope` (`:918`) is NOT changed — it is observe-only (the function docstring explicitly says "the wrapper NEVER mutates files outside `allowed_files`"). A one-line comment is added at the function's top documenting this and noting that `validate_scope` does not need an authorization gate because it does not mutate.
  - `plan_codex_dispatch.py` module docstring (or the section comment block above `_restore_in_scope`) gains a one-paragraph note documenting the new authorization contract and the asymmetry: the Codex wrapper has only one mutating cleanup call site (`_handle_timeout_cleanup`), so the authorization enum is a single value; `validate_scope` is observe-only and gateless. Future contributors who add a new mutating cleanup path MUST extend the enum.
  - New tests:
    - `test_restore_in_scope_raises_typeerror_without_authorization_source` — direct call without the param; assert `TypeError`.
    - `test_restore_in_scope_raises_valueerror_on_unknown_authorization_source` — bogus value; assert `ValueError`.
    - `test_handle_timeout_cleanup_passes_authorization_source` — invoke `_handle_timeout_cleanup` end-to-end with a fixture timeout; assert it succeeds AND the captured `_restore_in_scope` call carried `authorization_source="wrapper-codex-timeout-cleanup"` (use a `monkeypatch` on `_restore_in_scope` to capture kwargs).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_codex_dispatch.py tests/scripts/test_plan_codex_dispatch.py`

**Description:**
Mirrors TASK-002 at the Codex wrapper. The Codex wrapper has fewer mutation surfaces than the Claude wrapper (its `validate_scope` is observe-only; only `_handle_timeout_cleanup` actually calls `git restore`), so the authorization enum is single-valued. The asymmetry is documented so future contributors who add a Codex-side cleanup path know to extend the enum.

**Implementation notes.** The Codex wrapper's `_restore_in_scope` (`:894`) takes positional `tracked, untracked, repo_root` arguments. Adding a keyword-only param is a clean append; existing call sites at `:1084` get the kwarg added. The function is private (single underscore) and never called from tests directly except via `_handle_timeout_cleanup`, so the test-update surface is small.

## TASK-004: schema-fail short-circuit + `error.code: wrapper_autoclean_blocked` in `plan_claude_dispatch.py:cmd_run`

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_claude_dispatch.py (`cmd_run` step 9 at `:697`; the demote-status block at `:741`)
  - plugins/plan-executor/scripts/_claude_dispatch_envelope.py (`build_scope_violation` at `:366` — relax to accept `error_code` parameter)
  - tests/scripts/test_claude_dispatch_envelope.py (assert `build_scope_violation` accepts the new kwarg; default preserves the existing `"scope_violation"` code)
  - tests/scripts/test_plan_claude_dispatch_cli.py (new fixture covering the schema-fail short-circuit path)
- **Dependencies:** [002]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_claude_dispatch_cli.py -q -k "wrapper_autoclean_blocked or WrapperAutocleanBlocked or schema_fail_short_circuit"`
- **Acceptance criteria:**
  - `_claude_dispatch_envelope.build_scope_violation` (`:366`) gains a keyword-only param `error_code: str = "scope_violation"`. The internal `_error()` call at `:401` uses the supplied code instead of the hardcoded `"scope_violation"`. All existing callers (none today; the function is the only call site of the constant) continue to work without change because the default value preserves behavior.
  - `plan_claude_dispatch.py:cmd_run` step 9 (around `:697`) gains a new pre-cleanup branch that detects the "Layer-B catch" case: `agent_name != "plan-analyst"` AND `len(declared) == 0` AND `len(observed_delta) > 0` (where `observed_delta` is computed from the baseline + post-snapshot, the same input `apply_cleanup` would consume). When that condition fires, the wrapper:
    - Skips the `apply_cleanup` call entirely (no mutation).
    - Constructs an envelope via `build_scope_violation(message="wrapper autoclean blocked: declared_files_changed missing; preserving observed deltas pending user disposition", error_code="wrapper_autoclean_blocked", scope=<scope with observed_delta_tracked/untracked carrying the preserved paths>, ...)`.
    - Stamps `trace.ended_at`, validates the envelope against `claude_dispatch_output.json`, emits + spans + returns `EXIT_CODE_NON_OK`.
    - The `scope` sub-object's `observed_delta_tracked` and `observed_delta_untracked` carry the post-baseline observed deltas (the paths the orchestrator now needs to act on); `declared_files_changed` is empty (the input value, surfaced for traceability); `scope_violation_detected` is `False` (the wrapper did not detect a *violation*; it detected a *missing authorization*); `scope_misreport_detected` is `False`.
  - The schema-validation failure path (around `:784`) is NOT changed — `schema_invalid` continues to fire when the inner agent result fails its output-schema validation. The new schema-fail short-circuit is upstream of that, at the cleanup step, and is independent of inner-result schema validation.
  - The wrapper's status precedence is documented in a comment block above `cmd_run` step 9: `cleanup_failure > scope_violation(error_code=wrapper_autoclean_blocked) > scope_violation(error_code=scope_violation) > schema_invalid`. The comment notes that `cleanup_failure` continues to win because a non-empty `failed_paths` means the working tree is in an unknown state (TASK-004 hardening from PLAN_NESTED_DISPATCH §8.1).
  - New tests:
    - `test_build_scope_violation_default_error_code` — call without `error_code`; assert `envelope["error"]["code"] == "scope_violation"` (back-compat).
    - `test_build_scope_violation_custom_error_code` — call with `error_code="wrapper_autoclean_blocked"`; assert envelope's error code is the new value.
    - `test_cmd_run_emits_wrapper_autoclean_blocked_when_declared_empty_for_implementer` — fixture: `agent="plan-implementer"`, `declared_files_changed=[]` (or omitted), backend writes 3 files; assert envelope `status == "scope_violation"`, `error.code == "wrapper_autoclean_blocked"`, `scope.observed_delta_tracked` contains the 3 paths, working tree still has the 3 files.
    - `test_cmd_run_emits_normal_scope_violation_when_declared_nonempty_with_oos` — fixture: `declared_files_changed=["x.py"]`, backend writes `x.py` (in scope) and `y.py` (out of scope); assert envelope `status == "scope_violation"`, `error.code == "scope_violation"` (NOT autoclean-blocked), `y.py` is reverted, `x.py` is preserved.
    - `test_cmd_run_skips_short_circuit_for_plan_analyst` — fixture: `agent="plan-analyst"`, `declared_files_changed=[]`, backend writes 1 file (contract violation); assert envelope `status == "scope_violation"`, `error.code == "scope_violation"` (the readonly path runs `apply_cleanup`, which reverts the file).
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_claude_dispatch.py plugins/plan-executor/scripts/_claude_dispatch_envelope.py tests/scripts/test_claude_dispatch_envelope.py tests/scripts/test_plan_claude_dispatch_cli.py`

**Description:**
Implements the recoverable-failure path. When the orchestrator forgets to populate `declared_files_changed` for a write-authorized agent (Layer A regression OR an in-flight dispatch from before TASK-001 lands), the wrapper detects the situation pre-cleanup and surfaces a structured `error.code: wrapper_autoclean_blocked` envelope while preserving the working tree. The orchestrator's resume protocol (TASK-006) routes that envelope through the existing `Awaiting-user pause` subroutine. This is the load-bearing recovery path for the TASK-009 destruction event: with this in place, any future occurrence of "implementer wrote files, orchestrator didn't populate declared scope" pauses cleanly instead of destroying work.

**Implementation notes.** The "compute observed_delta pre-cleanup" step requires a `_changed_paths()` call against the post-dispatch state. Today `apply_cleanup` does that internally (`_claude_dispatch_cleanup.py:589`); the new short-circuit can either (a) factor `_changed_paths` into a public helper and call it from both `cmd_run` and `apply_cleanup`, or (b) move the short-circuit logic INTO `apply_cleanup` itself (returning a "blocked" sentinel that the wrapper translates to the envelope). Implementer's choice — recommend (b) because it keeps the gate logic colocated with the rest of the cleanup module's contract; (a) requires duplicating `_changed_paths` semantics across two call sites. If (b) is chosen, `apply_cleanup`'s return dict gains a new field `cleanup_strategy: "skipped_authorization_blocked"` (alongside the existing `"delta_bounded" | "skipped_no_baseline" | "skipped_git_failed"`).

## TASK-005: `ALLOWED_LOG_EVENTS` extension + `wrapper_autoclean_authorization` AST audit drift check

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (`ALLOWED_LOG_EVENTS` at `:232`; new `_check_wrapper_autoclean_authorization` audit function near `_check_fail_task_authorization_source` at `:10269`; registration entry near `:10446`)
  - tests/scripts/test_plan_ops.py (new fixtures for the audit check)
- **Dependencies:** [002, 003]
- **Test command:** `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json && python3 -m pytest tests/scripts/test_plan_ops.py -q -k "wrapper_autoclean_authorization or AuditWrapperAutoclean"`
- **Acceptance criteria:**
  - `ALLOWED_LOG_EVENTS` (`:232`) gains two new entries:
    - `wrapper_autoclean_executed` — emitted by the orchestrator when the wrapper successfully ran cleanup against a non-empty declared scope. Fields: `{task_id, agent, restored: [...], deleted: [...], failed_paths: [...]}`. The wrapper itself does NOT emit this; the orchestrator parses the envelope's `scope` sub-object and emits the event after `claude_dispatch_done`.
    - `wrapper_autoclean_blocked` — emitted by the orchestrator when the wrapper returned `status: scope_violation` with `error.code: wrapper_autoclean_blocked`. Fields: `{task_id, agent, preserved_files: [...], reason: <error.message>}`. Surfaces alongside the existing `claude_dispatch_failed` event.
  - A docstring comment block above the two new entries explains they are orchestrator-emitted (not wrapper-emitted) and what each authorizes audit-wise.
  - New audit check `wrapper_autoclean_authorization` registered alongside `principle_referenced` and `fail_task_authorization_source` in the `AUDIT_CHECKS` registry near line 10446. Tier: `default` (NOT `strict` — initial rollout favors visibility over hard-fail; can promote to `strict` in a follow-up after the audit's false-positive rate is known).
  - The audit check is an AST walk (NOT a regex grep) over `plugins/plan-executor/scripts/plan_claude_dispatch.py`, `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py`, `plugins/plan-executor/scripts/plan_codex_dispatch.py`. Optionally extends to `plugins/plan-executor/scripts/plan_gemini_dispatch.py` if that file exists at audit time (the wrapper inherits the same cleanup concerns per `_plan_paths.py:4`'s comment block; the audit check IS conditional on file existence so the audit doesn't fail in environments where Gemini wrapper hasn't shipped).
  - The walk flags every Call node whose function-receiver matches one of:
    - `_git` with first arg literal list whose first element is the literal `"restore"` or whose first two elements are `"reset"` then a string starting with `"--hard"`.
    - Attribute-access `.unlink()` (catches `Path(...).unlink()` and `pathlib.Path.unlink`).
    - `os.unlink(...)` / `os.remove(...)`.
  - For each flagged Call, the audit walks up the AST to find the enclosing `FunctionDef`. The check passes for that Call iff one of:
    - The enclosing function's signature includes a parameter named `authorization_source`.
    - The enclosing function is in a name-allowlist `{"_restore_path", "_restore_in_scope"}` AND every direct caller of that function (also walked from the AST) is itself authorized via the previous rule.
  - Failure surface: a list of `{file, line, function, reason}` dicts naming the offending call site(s).
  - The check does NOT enforce on `tests/` paths (a test fixture that `Path.unlink()`s a temp file is fine).
  - New tests:
    - `test_audit_wrapper_autoclean_authorization_passes_on_clean_wrappers` — uses the shipped wrapper sources after TASK-002/TASK-003 land; asserts the check passes.
    - `test_audit_wrapper_autoclean_authorization_fails_on_drifted_wrapper` — uses a `tmp_path`-cloned source where a `_git(["restore", ...])` call is added inside a function without `authorization_source`; asserts the check produces a failure with the file/line.
    - `test_audit_wrapper_autoclean_authorization_passes_on_allowlisted_helper` — the same drift but the call is inside `_restore_path`, which is in the allowlist and IS reachable only from `apply_cleanup` (which has the param); assert pass.
    - `test_log_event_accepts_wrapper_autoclean_executed_and_blocked` — both events validate against `ALLOWED_LOG_EVENTS`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Drift protection. The orchestrator-side `_check_fail_task_authorization_source` (TASK-009 of prohibit_silent_revert) catches CLI-level drift; this audit catches Python-level drift in the wrapper layer. AST walk (Codex's recommendation over grep — fewer false positives around comments / helper definitions / test fixtures). The default tier (not strict) means the check surfaces in `audit --json` output but doesn't break preflight; it can be promoted to strict after the false-positive rate is known on the live tree.

**Implementation notes.** Python's `ast.parse(source).walk()` plus a `parent_map` (computed via `ast.walk` + a child-to-parent dict) gives constant-time enclosing-function lookup. The "every direct caller is authorized" rule for the helper-allowlist case is a second AST pass over the same files looking for `Call(func=Name(id="_restore_path"))` and asserting the enclosing function has `authorization_source`. Recursion is not needed in v1 because the call graph is two levels deep; if it ever grows, the audit can be promoted to a fixed-point traversal.

## TASK-006: orchestrator-side resume protocol — `Awaiting-user pause` extension to `post_wrapper_autoclean_blocked`

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (Awaiting-user pause stage table; Phase B classify shim at `:512`; Phase B-rework / D.2a.5 routing at `:640`; Phase B-narrow-remediation routing at `:640`)
  - plugins/plan-executor/scripts/plan_ops.py (`cmd_fail_task` enum extends with `wrapper-autoclean-user-instruction`; review-route updates if needed)
  - tests/scripts/test_plan_ops.py
- **Dependencies:** [004]
- **Test command:** `python3 -m pytest tests/scripts/test_plan_ops.py -q -k "wrapper_autoclean_user_instruction or post_wrapper_autoclean_blocked"`
- **Acceptance criteria:**
  - SKILL.md `Awaiting-user pause (shared control flow)` subsection (added by prohibit_silent_revert TASK-004) — the per-stage payload contract table gains a new row:
    - **Stage:** `post_wrapper_autoclean_blocked`.
    - **Payload (in addition to `dirty_files`):** `{task_id, agent, preserved_files: [...], wrapper_error_code: "wrapper_autoclean_blocked", wrapper_error_message: <verbatim from envelope>}`.
    - **Trigger:** Phase B / B-rework / D.2b / B-narrow-remediation dispatch site receives a `claude_dispatch_failed` event with `error.code: wrapper_autoclean_blocked` from the wrapper.
    - **User options at next turn:** `(a) re-dispatch with declared_files_changed populated correctly` (orchestrator re-builds the wrapper input via `plan_ops.py build-claude-dispatch-input` from TASK-001); `(b) keep-and-commit` (user explicitly authorizes the preserved deltas); `(c) revert` (calls `cmd_fail_task --authorization-source wrapper-autoclean-user-instruction --stage <site>`).
  - SKILL.md Phase B classify shim (`:512`) — the existing extraction prose (`On status==ok route on outcome ∈ {success, partial, failed, plan-incorrect, blocked, malformed}`) gets a new sub-bullet under the `status != ok` row of the §Dispatch error handling (Claude wrapper) table at `:74`: ``status == "scope_violation" AND error.code == "wrapper_autoclean_blocked" → invoke the **Awaiting-user pause** subroutine with stage="post_wrapper_autoclean_blocked"; the existing scope_violation route (error.code == "scope_violation", file-level out-of-scope writes the wrapper reverted) routes the same as it does today (post_<site>_implement).``
  - The four call sites in SKILL.md (Phase B default `:502`, Phase B-rework `:640`'s `dispatch_bounded_remediation` branch, Phase D.2b `:650`, Phase B-narrow-remediation `:640`'s `dispatch_narrow_remediation` branch) each cross-reference the new stage with one line: ``Wrapper `error.code: "wrapper_autoclean_blocked"` → Awaiting-user pause stage=`post_wrapper_autoclean_blocked` (NOT `post_<site>_implement`); see §Awaiting-user pause table.``
  - `cmd_fail_task` `--authorization-source` enum extends with `wrapper-autoclean-user-instruction`. The docstring comment block above the argparse declaration documents the new value and what it authorizes (the user-instructed disposition of preserved files in a wrapper-autoclean-blocked pause).
  - `review-route` (the deterministic state machine in `plan_ops.py` documented at SKILL.md `:640`) does NOT need to change: the wrapper-autoclean-blocked path is at the dispatch-site level (Phase B classify shim), not inside `review-route`'s post-implement classification.
  - New tests:
    - `test_fail_task_authorization_source_accepts_wrapper_autoclean_user_instruction` — enum extension verified.
    - `test_skill_md_documents_post_wrapper_autoclean_blocked_stage` — SKILL.md doc-only assertion: the Awaiting-user pause subsection contains the new stage name.
- **Reversion guidance:** `git restore plugins/plan-executor/skills/implement-plan/SKILL.md plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Wires the wrapper's signal back into the orchestrator's existing pause infrastructure. The user gets four explicit options (re-dispatch with corrected scope, keep-and-commit, revert, abort) and the work stays in the working tree until disposition. Mirrors prohibit_silent_revert TASK-008's reconcile-batch four-options framing — same UX, different trigger.

**Implementation notes.** SKILL.md is the single source of truth for the orchestrator's runtime behavior. The Awaiting-user pause stage table is already maintained as a list per prohibit_silent_revert TASK-004; extending it is a one-row addition. The four cross-reference one-liners at the dispatch sites are pure prose; they don't change the parser's behavior on the existing `error.code: scope_violation` path (which routes through `post_<site>_implement` as documented today).

## TASK-007: end-to-end tests covering the four affected dispatch sites

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - tests/scripts/test_wrapper_autoclean_authorization.py (NEW — Codex's recommendation for a dedicated behavior test file)
  - tests/scripts/fixtures/wrapper_autoclean/ (NEW — minimal fixture plans + stub agent scripts that simulate the four dispatch shapes)
- **Dependencies:** [001, 002, 003, 004, 005, 006]
- **Test command:** `python3 -m pytest tests/scripts/test_wrapper_autoclean_authorization.py -q`
- **Acceptance criteria:**
  - New file `tests/scripts/test_wrapper_autoclean_authorization.py` (Codex's recommended location — keeps `test_skill_dispatch_implementer.py` for static migration invariants; the new file is the behavior-test home).
  - New fixture directory `tests/scripts/fixtures/wrapper_autoclean/` containing:
    - `plan_phase_b_default.md` — minimal plan with a single TASK-001 declaring `**Files:** plugins/foo.py, plugins/bar.py` and a trivial test command. The stub agent (next bullet) writes both files.
    - `plan_phase_b_rework.md` — same structure but the orchestrator dispatches `payload.variant="rework"` with a stub `dispatch_context.json`.
    - `plan_phase_d_2b_role_swap.md` — same structure with `payload.variant="role-swap"`.
    - `plan_phase_b_narrow_remediation.md` — minimal plan declaring `**Files:** plugins/foo.py:10-20` (line-bounded scope), with `dispatch_context` carrying `load_bearing_findings[]`.
    - `stub_agent_writes_files.sh` — writes the declared files and emits a markdown success report (mirrors the actual implementer's output shape; intentionally NOT JSON, so the inner schema-validation fails — same trigger TASK-009 hit).
  - Test class `TestEndToEndWrapperAutocleanAuthorization`:
    - `test_phase_b_default_preserves_implementer_work` — orchestrator builds payload via `plan_ops.py build-claude-dispatch-input --variant default`, dispatches via `plan_claude_dispatch.py run --input -` with the stub agent. Assert: working tree contains the two declared files post-dispatch; envelope `status == "ok"` (the wrapper's inner schema validation may fail and demote to `schema_invalid`, but the cleanup path didn't revert anything because `declared_files_changed` was populated).
    - `test_phase_b_default_blocks_when_declared_empty` — orchestrator builds payload but blanks `declared_files_changed` (simulates a Layer-A regression). Stub agent writes two files. Assert: envelope `status == "scope_violation"`, `error.code == "wrapper_autoclean_blocked"`, working tree still contains both files.
    - `test_phase_b_rework_preserves_implementer_work` — same as default but variant=rework + dispatch_context.
    - `test_phase_d_2b_role_swap_preserves_implementer_work` — variant=role-swap.
    - `test_phase_b_narrow_remediation_preserves_implementer_work` — variant=narrow-remediation, agent="plan-remediator".
    - One parametrized matrix test `test_all_four_sites_preserve_via_authorization_gate` — combines the four scenarios into one parametrize fixture so adding a fifth dispatch site in the future requires only adding a row.
  - All four end-to-end tests use a real `git init -q -b main` fixture per test (mirrors `test_claude_dispatch_cleanup.py`'s `_make_repo` pattern at `:70`); the stub agent is invoked via the existing `tests/scripts/stubs/plan_claude_dispatch_stub.py` machinery so no live `claude` binary is required.
  - `test_skill_dispatch_implementer.py` is updated minimally: any test that currently asserts the absence of `declared_files_changed` in the dispatch payload (legacy invariant) is updated to assert its presence post-TASK-001. No tests are deleted; doc-only invariants stay.
- **Reversion guidance:** `git restore tests/scripts/test_wrapper_autoclean_authorization.py tests/scripts/fixtures/wrapper_autoclean/ tests/scripts/test_skill_dispatch_implementer.py`

**Description:**
Behavior coverage for the four affected dispatch sites. The matrix test future-proofs against new sites being added without coverage. The "blocks when declared empty" test exercises the load-bearing recovery path (TASK-004) end-to-end, proving that even a buggy orchestrator that fails to populate `declared_files_changed` no longer destroys work.

**Implementation notes.** The fixture stub agent emits markdown (not JSON) so the inner result-schema validation fails — this is intentional, mirroring the actual TASK-009 failure mode. The test asserts that the *outer* envelope's `error.code` is `wrapper_autoclean_blocked` (not `schema_invalid`), proving that the new status-precedence rule from TASK-004 fires correctly. If a future test wants to exercise both the `wrapper_autoclean_blocked` path AND the `schema_invalid` path independently, the stub can be parametrized to emit JSON with deliberate schema violations.

## Expected outcome

- Seven `feat(TASK-NNN):` commits plus one `chore(implement-plan):` housekeeping commit if needed.
- `plan_ops.py build-claude-dispatch-input` is the canonical entry point for constructing wrapper inputs; the four dispatch sites in SKILL.md / dispatch-templates.md invoke it instead of hand-building JSON. Every Claude-tier dispatch carries a non-empty top-level `declared_files_changed` for write-authorized agents.
- `_claude_dispatch_cleanup.apply_cleanup` and `plan_codex_dispatch.py:_restore_in_scope` both require an explicit `authorization_source` keyword argument; missing/bogus values raise loudly. Closed enums document which call sites are authorized to mutate.
- The wrapper detects "write-authorized agent + empty declared scope" pre-cleanup and emits a structured `error.code: wrapper_autoclean_blocked` envelope instead of silently reverting. The orchestrator routes that envelope through the existing `Awaiting-user pause` subroutine with `stage: post_wrapper_autoclean_blocked`.
- `plan_ops.py audit --strict --json` passes (the new `wrapper_autoclean_authorization` AST drift check ships at default tier; can be promoted to strict in a follow-up).
- `plan_ops.py audit --list` includes the new `wrapper_autoclean_authorization` check.
- The four affected dispatch sites (Phase B default, Phase B-rework, Phase D.2b role-swap, Phase B-narrow-remediation) preserve implementer work even when the inner agent's result fails schema validation. The TASK-009 destruction event becomes structurally impossible.

## Follow-ups (out of scope)

- **Promote the `wrapper_autoclean_authorization` audit check to strict tier.** The default-tier rollout in TASK-005 surfaces the check in `audit --json` output but does not break preflight. After a few weeks of running on the live tree, the false-positive rate will be known; if zero, promote to `strict`. Not load-bearing for this plan.
- **Extend the AST audit to `plan_gemini_dispatch.py`.** TASK-005's audit conditionally walks the file IF it exists at audit time. If/when the Gemini wrapper ships its own cleanup, a one-line addition to its module's `_restore_*` helper signature wires it into the same gate. Documented in TASK-005 but not implemented here because the Gemini wrapper is out of this plan's scope.
- **Move the `target_task_id` auto-injection rule into `build-claude-dispatch-input`.** Today the rule is rendered inside `plan_codex_dispatch.py:render_implement_prompt` and (for Claude) the wrapper's render path. Now that `build-claude-dispatch-input` is the canonical input builder, the rule could move there too — but this is a separate refactor with its own dispatch-template implications, and is not load-bearing for the autoclean defect.
- **Wrap the wrapper's protected-paths allowlist in the same gate.** Today `PROTECTED_PATH_PREFIXES` is a hard allowlist that bypasses cleanup unconditionally. After TASK-002/003 land, the protected-paths allowlist becomes a "second line of defense" rather than the first; a future task could fold it into the authorization gate as a third enum value (`wrapper-protected-path-bypass`) so even the bypass is auditable. Not load-bearing for this plan because the allowlist is already conservative (executor infrastructure only).
- **Codify the read-only agent set.** TASK-002 hard-codes `{plan-analyst}` as the read-only agent set. As more agents are added (or `plan-analyst` evolves), this set may need to be a manifest-derived value (`agent.read_only: bool` in the agent frontmatter, exposed via `_claude_agent_manifest.load_agent`). Not load-bearing for this plan because the set is small and the discriminator is simple.
- **`build-claude-dispatch-input` for direct CLI callers.** TASK-001 documents the analyst-variant cross-reference for direct CLI users. A follow-up could ship a thin shell wrapper or a `--print-help` mode that walks operators through constructing a payload by hand, removing the inline JSON from the dispatch-templates entirely. Cosmetic, not load-bearing.
