# TASK-002 — Wrapper Files-block parser unification (Issue 4)

## Goal

Eliminate the divergence between `plan_ops._normalize_files_entry` (orchestrator side, prose-aware) and `plan_codex_dispatch.normalize_file_path` (wrapper side, prose-broken) so a `Files:` bullet of the form `` - `Makefile` — add `audit` to the `.PHONY` list (line 1) `` extracts the path `Makefile` on both sides. This is the root cause of Issue 4 and the upstream trigger for Issue 5 (false `out_of_scope_tracked` → reconcile-batch wipes correct edit).

## Scoped Context

**The two parsers today.**

- Orchestrator (`plan_ops.py:6906-6934`, `_normalize_files_entry`):
  1. Strip trailing `(annotation)` parenthetical (em-dash-tolerant).
  2. **If the cleaned string starts with a backticked path, capture just the contents of the first backticked region** (`re.match(r'`([^`]+)`', cleaned)`).
  3. Otherwise split on `\s+[-–—]\s+` (space-dash-space) and take the head.
  4. Strip wrapping backticks.
  5. Strip `:N-M` and `:N` suffixes.
- Wrapper (`plan_codex_dispatch.py:237-248`, `normalize_file_path`):
  1. Strip trailing `(annotation)`.
  2. Strip `:N-M` and `:N` suffixes.
  3. `cleaned.strip().strip("`").strip()` — strips leading/trailing backticks ONLY when the entire string ends with a backtick. Bullets like `` `Makefile` — add `audit`... `` do NOT end with a backtick (the trailing chars are `...` or prose), so the leading backtick is stripped while the prose continuation stays welded to the path.

The wrapper's broken normaliser is invoked in three places: `render_implement_prompt` (line 257) for the `Allowed files:` line in the Codex prompt, `cmd_implement` (line 1108) and `cmd_review` (line 1380) for the `allowed_files` set used by `validate_scope` / `_handle_timeout_cleanup`. All three have to see the post-normalisation string.

**Why "share, don't dual-maintain."** The two parsers have already drifted twice (the original divergence, and the test fixture additions in `tests/scripts/test_plan_codex_dispatch_parsing.py` which never exercised a prose-laden bullet). Keeping a single helper in `_plan_paths.py` (already imported by both modules — `plan_codex_dispatch.py:55` re-exports `is_protected_path` from there) eliminates the drift surface.

**Out of scope.** Touching `_extract_bullet_list` (the function that gathers raw bullet items before normalisation) — both copies of that helper already handle multi-line bullet bodies correctly; the bug is downstream. Likewise, changing the wrapper's `parse_task_block` signature; only its call to `normalize_file_path` needs to land on the unified helper (or the unified helper needs to be called from `cmd_implement` / `cmd_review` after `parse_task_block`).

## Verification

- `plugins/plan-executor/scripts/_plan_paths.py` exports a `normalize_files_entry(raw: str) -> str` function with the orchestrator's existing semantics: strip trailing `(annotation)` (em-dash-tolerant); if a leading backticked path exists, capture only that path; else split on space-dash-space and take the head; strip wrapping backticks; strip `:N-M` and `:N` suffixes.
- `plan_ops._normalize_files_entry` is rewritten to delegate to `_plan_paths.normalize_files_entry` (one-line wrapper to preserve module-private name; or call sites updated, both acceptable). All existing `plan_ops` callers and tests continue to pass.
- `plan_codex_dispatch.normalize_file_path` is rewritten to delegate to `_plan_paths.normalize_files_entry` (or aliased: `normalize_file_path = normalize_files_entry`). All three wrapper call sites (`render_implement_prompt`, `cmd_implement`, `cmd_review`) consume the unified helper.
- New parameterised parsing tests in `tests/scripts/test_plan_codex_dispatch_parsing.py`:
  - Bare path bullet: `- Makefile — add audit to .PHONY` → `Makefile`.
  - Backticked path bullet: `` - `Makefile` — add `audit` to the `.PHONY` list (line 1) `` → `Makefile`.
  - Backticked path with em-dash: `` - `scripts/foo.py` — prose `` → `scripts/foo.py`.
  - Backticked path with annotation: `` - `scripts/foo.py` (modify) `` → `scripts/foo.py`.
  - Bare path with line range: `- scripts/foo.py:42-58` → `scripts/foo.py`.
  - Absolute path normalisation is NOT in this task's scope (the analysis lists it as a follow-up item; defer to a future task) — tests should not assert absolute-path stripping.
- Equivalent parameterised tests added (or existing tests extended) in `tests/scripts/test_plan_ops.py` for `_plan_paths.normalize_files_entry` covering the same cases, so the helper is regression-tested through both modules.
- `parse_task_block(plan_text, "001")["files"]` for a task whose markdown contains `` - `Makefile` — add `audit` to `.PHONY` `` returns `["Makefile"]` (after the wrapper applies the unified helper to each entry), not the prose-laden raw string.
- The existing `test_wrapper_parse_task_block_*` tests pass unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_ops.py` returns 0.

## Tasks

### TASK-002: Wrapper Files-block parser unification

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_plan_paths.py` (edit)
  - `plugins/plan-executor/scripts/plan_ops.py` (edit)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit)
  - `tests/scripts/test_plan_codex_dispatch_parsing.py` (edit)
  - `tests/scripts/test_plan_ops.py` (edit)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_ops.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/_plan_paths.py` — full file
  - `plugins/plan-executor/scripts/plan_ops.py:6906-6934` (the existing `_normalize_files_entry`)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:237-292` (`normalize_file_path` plus first call site `render_implement_prompt`)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1085-1120` (`cmd_implement` second call site)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1356-1396` (`cmd_review` third call site)
- **Symbol targets:**
  - `_normalize_files_entry` in `plan_ops.py`
  - `normalize_file_path` in `plan_codex_dispatch.py`
- **Acceptance criteria:**
  - `_plan_paths.normalize_files_entry(raw: str) -> str` is the single canonical implementation. Semantics: strip trailing `(annotation)` parenthetical (em-dash and en-dash tolerant inside the parens); if a leading backticked region exists, capture its contents; otherwise split on space-dash-space (`\s+[-–—]\s+`) and take the head; strip wrapping backticks; strip `:N-M` / `:N–M` ranges; strip single `:N` reference; return the stripped result.
  - `plan_ops._normalize_files_entry` and `plan_codex_dispatch.normalize_file_path` both delegate to (or alias) the canonical helper. No duplicated regex bodies remain.
  - Wrapper `parse_task_block(plan_text, task_id)["files"]` returns the un-normalised raw bullet items (current contract preserved); the THREE call sites (`render_implement_prompt`, `cmd_implement`, `cmd_review`) call the unified helper before consuming the file list. Equivalently, if the implementer chooses to normalise inside `parse_task_block`, all THREE call sites must see normalised paths and existing tests must keep passing.
  - New parameterised test cases added to `tests/scripts/test_plan_codex_dispatch_parsing.py` covering bare path, backticked path, em-dash-prose, annotation, and `:N-M` suffix forms (one assertion per case; >= 5 cases total). At least one case asserts `parse_task_block` produces `["Makefile"]` for a backticked-prose-laden bullet.
  - Equivalent / extended parameterised tests in `tests/scripts/test_plan_ops.py` exercise `_plan_paths.normalize_files_entry` directly and confirm the existing `_extract_task_files_from_plan` continues to work end-to-end.
  - All existing tests in both files pass unchanged.
  - `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_ops.py` returns 0.
- **Reversion guidance:** revert the parser-unification edits in `_plan_paths.py`, restore the per-module duplicated implementations in `plan_ops.py` and `plan_codex_dispatch.py`, and drop the new test cases. The pre-fix behaviour is buggy but well-understood; reverting does not introduce a new failure mode.

**Description:**
Lift the orchestrator's prose-aware `_normalize_files_entry` into `_plan_paths.py` (the existing shared module) as `normalize_files_entry`, then have both call sites (orchestrator's `_normalize_files_entry` and the wrapper's `normalize_file_path`) delegate to it. Add parameterised parsing tests in both `test_plan_codex_dispatch_parsing.py` and `test_plan_ops.py` that pin the prose-laden, backticked, annotated, and line-range forms. The parser drift was the upstream cause of Issue 4 and the trigger for Issue 5; sharing one helper eliminates the surface entirely.

**Implementation notes:**
- The simplest landing pattern is to add `normalize_files_entry` to `_plan_paths.py` (with the same body that `plan_ops._normalize_files_entry` already has), then in `plan_ops.py` either reduce the existing function to `def _normalize_files_entry(raw): return normalize_files_entry(raw)` (preserve the module-private name to avoid touching ~40 call sites) or update call sites; both are acceptable.
- In `plan_codex_dispatch.py`, the cleanest landing is `from _plan_paths import normalize_files_entry as normalize_file_path` (preserving the wrapper-public name for back-compat with any direct importers — there are none in the test suite, but the alias is cheap).
- The trailing-parenthetical strip must run BEFORE the dash-split, so a parenthetical containing an em-dash like `(create — canonical)` is removed as a unit and does not get truncated by the dash-split.
- Do NOT introduce absolute-path normalisation in this task; the analysis flags it as a follow-up. Resist the urge to "while I'm here…" — the test cases for this task assert relative-path inputs only.
- The `_extract_bullet_list` helpers in both modules already produce raw item strings; this task only changes how those items are normalised after extraction.
