# TASK-007 — End-to-end tests covering the four affected dispatch sites

## Goal

End-to-end tests covering the four affected dispatch sites

## Context

After TASK-001..006 land, the wrapper-autoclean defect is structurally closed: the orchestrator populates `declared_files_changed` via the canonical builder; the wrapper requires explicit `authorization_source`; a Layer-A regression triggers `wrapper_autoclean_blocked` instead of silent revert; the orchestrator routes the blocked envelope through `Awaiting-user pause`. This task adds end-to-end behavior coverage for all four affected dispatch sites (Phase B default, Phase B-rework, Phase D.2b role-swap, Phase B-narrow-remediation) so future regressions are caught at CI rather than at a real /implement-plan run.

Per Codex's recommendation:
- New file `tests/scripts/test_wrapper_autoclean_authorization.py` for behavior coverage. Keep `tests/scripts/test_skill_dispatch_implementer.py` (from SKILL_bash_dispatch_migration TASK-004) for static migration invariants — that file is already broad and prose-heavy.
- Four scenarios bundled into one parametrized matrix test so adding a fifth dispatch site in the future requires only adding a row.
- The fixture stub agent emits markdown (NOT JSON), mirroring the actual TASK-009 failure mode — the inner result-schema validation fails, and we assert the *outer* envelope's `error.code` is `wrapper_autoclean_blocked` (proving TASK-004's status precedence fires correctly).

### Decisions folded in

1. **Behavior tests live in a new file.** Codex's call: keeping `test_skill_dispatch_implementer.py` for static migration invariants and centralizing the new behavior tests makes the diff readable and the test purpose obvious.
2. **One parametrized matrix test future-proofs against new dispatch sites.** Adding a fifth site needs only a parametrize row, not a new test function.
3. **Stub agent emits markdown.** Mirrors TASK-009's actual failure mode (the implementer's native output format is markdown; the wrapper's inner schema validation fails). Critically, this exercises the status precedence: with the stub's output the wrapper would naturally produce `status: schema_invalid`, but TASK-004's short-circuit fires first and the envelope ships `status: scope_violation` with `error.code: wrapper_autoclean_blocked`.
4. **`test_skill_dispatch_implementer.py` updated minimally.** Any test that asserts the absence of `declared_files_changed` in the dispatch payload (legacy invariant) is updated to assert its presence post-TASK-001. No tests are deleted; doc-only invariants stay.
5. **Real `git init` per test, not mocked.** Mirrors `test_claude_dispatch_cleanup.py`'s `_make_repo` pattern at `:70`. The tests exercise the actual `git diff --name-only` and `git restore` commands so behavior matches production.

## Verification

- `python3 -m pytest tests/scripts/test_wrapper_autoclean_authorization.py -q` passes.
- The matrix test exercises all four dispatch sites (`default | rework | role-swap | narrow-remediation`) and asserts:
  - Working tree contains the implementer-written files post-dispatch.
  - Envelope `status == "ok"` (or `"schema_invalid"` if the inner result fails — but cleanup did not run).
- The "blocks when declared empty" test (per dispatch site) asserts:
  - Envelope `status == "scope_violation"`.
  - Envelope `error.code == "wrapper_autoclean_blocked"`.
  - Working tree still contains the implementer-written files (preservation).
- The `tests/scripts/fixtures/wrapper_autoclean/` directory contains all per-variant fixture plans + the markdown-emitting stub agent.
- `tests/scripts/test_skill_dispatch_implementer.py` continues to pass (with any pre-TASK-001 negative assertions about `declared_files_changed` updated to positive).

## Tasks

### TASK-007: End-to-end tests covering the four affected dispatch sites

- **Status:** pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - tests/scripts/test_wrapper_autoclean_authorization.py (NEW — Codex's recommended location)
  - tests/scripts/fixtures/wrapper_autoclean/ (NEW — fixture plans + stub agent)
  - tests/scripts/test_skill_dispatch_implementer.py (minimal update if any pre-TASK-001 negative assertion exists about `declared_files_changed`)
- **Dependencies:** [001, 002, 003, 004, 005, 006]
- **Test command:** `python3 -m pytest tests/scripts/test_wrapper_autoclean_authorization.py -q`
- **Acceptance criteria:**
  - New file `tests/scripts/test_wrapper_autoclean_authorization.py`:
    - Loads `_claude_dispatch_cleanup` and `plan_claude_dispatch` via `importlib.util.spec_from_file_location` (mirrors `test_claude_dispatch_cleanup.py:_load_cleanup` at line 41 — keeps the test self-contained without requiring `pytest` to import the wrapper modules at collection time).
    - Per-test fixture: `_make_repo(tmp_path, name="repo")` creates a fresh `git init -q -b main` repo with a single committed seed file. Mirrors the existing `test_claude_dispatch_cleanup.py:_make_repo` at line 70.
    - Per-test fixture: `_make_plan(repo, task_id, files)` creates a minimal plan markdown declaring TASK-NNN with the supplied `**Files:**` list and a trivial test command.
    - Stub agent: a small Python or bash script `tests/scripts/fixtures/wrapper_autoclean/stub_agent_writes_files.py` (or `.sh`) that takes `--write <path>...` arguments and `Write`s the listed files with deterministic content, then emits a markdown success report on stdout (intentionally NOT JSON, so the inner schema validation fails).
    - Stub binary discovery: the test passes `--backend-binary <path-to-stub>` to `plan_claude_dispatch.py run`. Mirrors the existing `tests/scripts/stubs/plan_claude_dispatch_stub.py` machinery from SKILL_bash_dispatch_migration TASK-002.
  - Test class `TestEndToEndWrapperAutocleanAuthorization`:
    - `test_phase_b_default_preserves_implementer_work` — orchestrator builds payload via `plan_ops.py build-claude-dispatch-input --variant default --task-id 001 --plan-file <plan>`; pipes stdout into `plan_claude_dispatch.py run --input -`. Stub agent writes the two declared files. Assert: envelope `error.code != "wrapper_autoclean_blocked"` (the cleanup path was authorized; either `status: ok` or `status: schema_invalid` is acceptable depending on whether the markdown-emitting stub trips the inner schema gate). Assert: working tree contains both declared files post-dispatch.
    - `test_phase_b_default_blocks_when_declared_empty` — orchestrator builds payload then BLANKS top-level `declared_files_changed` to `[]` (simulates a Layer-A regression). Stub agent writes two files. Assert: envelope `status == "scope_violation"`, `error.code == "wrapper_autoclean_blocked"`, working tree still contains both files.
    - `test_phase_b_rework_preserves_implementer_work` — same as default but `--variant rework --dispatch-context <stub.json>`.
    - `test_phase_d_2b_role_swap_preserves_implementer_work` — `--variant role-swap`.
    - `test_phase_b_narrow_remediation_preserves_implementer_work` — `--variant narrow-remediation`, `agent="plan-remediator"`, line-bounded scope in the plan.
    - `test_all_four_sites_preserve_via_authorization_gate` — `@pytest.mark.parametrize("variant,agent_name", [...4 rows...])`; combines the four scenarios into one matrix so adding a fifth dispatch site in the future requires only adding a row.
  - The "blocks when declared empty" test variant runs for all four sites (parametrized similarly):
    - `test_all_four_sites_block_on_empty_declared_via_authorization_gate` — same 4-row parametrize matrix; asserts `error.code == "wrapper_autoclean_blocked"` for each.
  - The fixture directory `tests/scripts/fixtures/wrapper_autoclean/`:
    - `plan_phase_b_default.md` — minimal plan with TASK-001 declaring `**Files:** plugins/foo.py, plugins/bar.py` and a trivial test command.
    - `plan_phase_b_rework.md` — same but TASK-001 also declares a test.
    - `plan_phase_d_2b_role_swap.md` — same.
    - `plan_phase_b_narrow_remediation.md` — declares `**Files:** plugins/foo.py:10-20` (line-bounded scope) — note: the line-bounded form is parsed by `_extract_task_files_from_plan` to the file-level entry; line-level enforcement is orchestrator-side and not exercised by this test.
    - `dispatch_context_rework.json` — minimal `{findings_for_retry: [], d5_summary: ""}` JSON.
    - `dispatch_context_narrow_remediation.json` — minimal `{load_bearing_findings: [], dismissed_findings: [], d5_summary: ""}` JSON.
    - `stub_agent_writes_files.py` (or `.sh`) — the markdown-emitting stub.
  - `tests/scripts/test_skill_dispatch_implementer.py` is updated minimally: any test that currently asserts the absence of `declared_files_changed` in the dispatch payload (legacy invariant from before TASK-001) is updated to assert its presence. No tests are deleted; existing doc-only invariants stay.
- **Reversion guidance:** `git restore tests/scripts/test_wrapper_autoclean_authorization.py tests/scripts/fixtures/wrapper_autoclean/ tests/scripts/test_skill_dispatch_implementer.py`

**Description:**
Behavior coverage for the four affected dispatch sites. The matrix test future-proofs against new sites being added without coverage. The "blocks when declared empty" tests exercise the load-bearing recovery path (TASK-004) end-to-end, proving that even a buggy orchestrator that fails to populate `declared_files_changed` no longer destroys work.

**Implementation notes.** The fixture stub agent emits markdown (not JSON) so the inner result-schema validation fails — this is intentional, mirroring the actual TASK-009 failure mode. The test asserts that the *outer* envelope's `error.code` is `wrapper_autoclean_blocked` (not `schema_invalid`), proving that the new status-precedence rule from TASK-004 fires correctly. If a future test wants to exercise both the `wrapper_autoclean_blocked` path AND the `schema_invalid` path independently, the stub can be parametrized to emit JSON with deliberate schema violations.

Stub agent: prefer Python over bash for cross-platform compatibility (the existing `tests/scripts/stubs/plan_claude_dispatch_stub.py` is Python). The stub's `argv[0]` will be the executable path, `argv[1:]` will be the args the wrapper passes (`-p`, `--agent`, `--permission-mode`, ...). The stub parses `--add-dir <repo>` to find the repo and writes the files there, then emits the markdown success report on stdout. Stderr is ignored; exit code 0.

Cross-platform note: on Windows / WSL, `subprocess.run([stub_path, ...])` works for both `.py` and `.sh` files as long as the `.sh` has a shebang and is executable. The Python stub is more portable; recommend that.

The `--backend-binary` flag was added in SKILL_bash_dispatch_migration TASK-002 specifically for test stubs (per `plan_claude_dispatch.py:933-938`). Tests use that flag instead of relying on `claude` being on PATH.

The matrix tests use `pytest.mark.parametrize` with `ids=` so failures point to the specific variant: `default | rework | role-swap | narrow-remediation`. The matrix's "blocks when declared empty" version asserts `error.code == "wrapper_autoclean_blocked"` for each variant.
