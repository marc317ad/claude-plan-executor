# TASK-005 — `ALLOWED_LOG_EVENTS` extension + `wrapper_autoclean_authorization` AST audit drift check

## Goal

`ALLOWED_LOG_EVENTS` extension + `wrapper_autoclean_authorization` AST audit drift check

## Context

The `prohibit_silent_revert` plan (committed `8bb0b7b`) hardened the orchestrator surface against silent destruction of completed work and, in its TASK-009, added two audit drift checks (`fail_task_authorization_source`, `principle_referenced`) that catch CLI-level and SKILL.md-level drift. The wrapper-layer fix (TASK-002 / TASK-003 of this plan) puts authorization gates on every wrapper-side mutation site (`apply_cleanup`, `_restore_in_scope`); without a corresponding audit check, future contributors could add a new ungated `_git(["restore", ...])` / `Path.unlink()` call in a wrapper file and silently re-introduce the gap.

Codex's recommendation: AST walk, not regex grep. Grep over Python source produces noisy false positives around comments, docstrings, helper definitions, and test fixtures; AST gives a precise enclosing-function match for each Call node. The audit walks `plan_claude_dispatch.py`, `_claude_dispatch_cleanup.py`, `plan_codex_dispatch.py`, and conditionally `plan_gemini_dispatch.py` (if the file exists), flagging Call nodes that look like file-mutation primitives and asserting each is reachable only via an authorization-gated function or a name-allowlisted helper.

The orchestrator-side run-log events are also reserved here. Per Codex's guidance, the wrapper itself does NOT write to `_run_log.jsonl`; the orchestrator parses the wrapper envelope and emits the new events on the wrapper's behalf. `wrapper_autoclean_executed` (success path: cleanup ran with non-empty declared scope) and `wrapper_autoclean_blocked` (TASK-004 short-circuit fired) are reserved in `ALLOWED_LOG_EVENTS` so the orchestrator (TASK-006) and any future tooling can emit them without hitting the allowlist gate.

### Decisions folded in

1. **AST walk over regex grep.** Per Codex: precise enclosing-function lookup, no false positives on comments / test fixtures / helper definitions.
2. **Default audit tier (not strict) at v1.** Surfaces in `audit --json` output but does not break preflight. After the false-positive rate is known on the live tree, promotion to `strict` is a follow-up.
3. **Conditional `plan_gemini_dispatch.py` inclusion.** The file may not exist yet. The audit check uses `Path.exists()` to skip if absent so the audit doesn't fail in environments where the Gemini wrapper hasn't shipped. Future Gemini wrapper PRs would land their cleanup helpers and authorization gate together.
4. **Wrapper does NOT write run-log events.** Per Codex's recommendation: `wrapper_autoclean_executed` / `wrapper_autoclean_blocked` are orchestrator-emitted. The wrapper carries the signal in the envelope (`error.code` + `scope` sub-object); the orchestrator parses and emits.
5. **Helper-name allowlist for second-tier mutation primitives.** `_restore_path` (in `_claude_dispatch_cleanup.py`) is called only from `apply_cleanup`, which IS gated. Allowlisting `_restore_path` lets the audit accept `_git(["restore", ...])` inside it without manually verifying the call graph each time. The allowlist requires a second AST pass to confirm every direct caller of an allowlisted helper is itself authorized; this catches future regressions where someone adds a non-gated caller.

## Verification

- `python3 plugins/plan-executor/scripts/plan_ops.py audit --strict --json` passes (the new `wrapper_autoclean_authorization` check is at default tier; `--strict` mode still reports it but doesn't block on default-tier findings).
- `python3 plugins/plan-executor/scripts/plan_ops.py audit --list` includes the new `wrapper_autoclean_authorization` check.
- `ALLOWED_LOG_EVENTS` (in `plan_ops.py:232`) includes the literal strings `"wrapper_autoclean_executed"` and `"wrapper_autoclean_blocked"`.
- `python3 plugins/plan-executor/scripts/plan_ops.py log-event --event wrapper_autoclean_executed --fields-json '{"task_id":"001","agent":"plan-implementer","restored":[],"deleted":[],"failed_paths":[]}'` succeeds (new event passes the allowlist gate).
- An AST walk of `plan_claude_dispatch.py`, `_claude_dispatch_cleanup.py`, `plan_codex_dispatch.py` (after TASK-002/TASK-003 land) reports zero ungated mutation primitives.
- A test fixture that introduces a synthetic `_git(["restore", ...])` call in a non-authorized function reports a failure with the file/line/function name.
- A test fixture that introduces a `_git(["restore", ...])` call inside `_restore_path` (allowlisted) but adds a non-authorized direct caller of `_restore_path` reports a failure (the second-tier allowlist requires every direct caller to be authorized).

## Tasks

### TASK-005: `ALLOWED_LOG_EVENTS` extension + `wrapper_autoclean_authorization` AST audit drift check

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
    - `wrapper_autoclean_executed` — emitted by the orchestrator when the wrapper successfully ran cleanup against a non-empty declared scope. Documented fields: `{task_id, agent, restored: [...], deleted: [...], failed_paths: [...]}`. The wrapper itself does NOT emit this; the orchestrator parses the envelope's `scope` sub-object and emits the event after `claude_dispatch_done`.
    - `wrapper_autoclean_blocked` — emitted by the orchestrator when the wrapper returned `status: scope_violation` with `error.code: wrapper_autoclean_blocked`. Documented fields: `{task_id, agent, preserved_files: [...], reason: <error.message>}`. Surfaces alongside the existing `claude_dispatch_failed` event.
  - A docstring comment block above the two new entries explains they are orchestrator-emitted (not wrapper-emitted) and what each authorizes audit-wise.
  - New audit check `_check_wrapper_autoclean_authorization` registered alongside `principle_referenced` and `fail_task_authorization_source` in the `AUDIT_CHECKS` registry near line 10446. Tier: `default` (NOT `strict` — initial rollout favors visibility over hard-fail; promotion to `strict` is documented as a follow-up).
  - The audit check is an AST walk over the Python source files (NOT a regex grep). Files walked:
    - `plugins/plan-executor/scripts/plan_claude_dispatch.py`
    - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py`
    - `plugins/plan-executor/scripts/plan_codex_dispatch.py`
    - `plugins/plan-executor/scripts/plan_gemini_dispatch.py` (only if `Path.exists()` returns True at audit time; absent is fine).
  - The walk flags every `ast.Call` node whose function-receiver matches one of:
    - `_git` with first arg literal `ast.List` whose first element is the literal `"restore"`. Also flags `["reset", X]` where X is a string literal starting with `"--hard"`.
    - Attribute-access `.unlink()` (catches `Path(...).unlink()` / `pathlib.Path.unlink`). Detection: `Call(func=Attribute(attr="unlink", ...))`.
    - `os.unlink(...)` / `os.remove(...)`. Detection: `Call(func=Attribute(value=Name(id="os"), attr ∈ {"unlink", "remove"}))`.
  - For each flagged Call, the audit walks up the AST (using a parent-map computed once per file) to find the enclosing `FunctionDef`. The check passes for that Call iff one of:
    - The enclosing function's signature includes a parameter named `authorization_source`.
    - The enclosing function name is in the allowlist `ALLOWED_HELPER_NAMES = {"_restore_path", "_restore_in_scope"}` AND every direct caller of that function (a second AST pass: `Call(func=Name(id=helper_name))` walked against the same parent-map) is itself authorized via the previous rule (i.e., its enclosing function has `authorization_source`).
  - Failure surface: a list of `{file, line, function, reason}` dicts naming the offending call site(s). The `reason` field distinguishes "no authorization_source param on enclosing function" vs "allowlisted helper but unauthorized direct caller at <other-file:line>".
  - The check does NOT enforce on `tests/` paths (the `_walk` only inspects the four wrapper files; tests at `tests/scripts/*.py` may freely `Path.unlink()` temp files).
  - The check does NOT enforce on `os.unlink(...)` calls inside `try`/`except` blocks that catch `OSError` AND immediately re-raise OR return — these are best-effort cleanup that is not the primary mutation surface. Implementer's discretion: if the AST recognition is too complex for a tight v1, allow the false-positive and add the call site to the allowlist as a second iteration. The Codex `_restore_path` body at `_claude_dispatch_cleanup.py:496` is one such case.
  - New tests in `tests/scripts/test_plan_ops.py`:
    - `test_audit_wrapper_autoclean_authorization_passes_on_clean_wrappers` — uses the shipped wrapper sources after TASK-002/TASK-003 land; asserts the check passes.
    - `test_audit_wrapper_autoclean_authorization_fails_on_drifted_wrapper` — uses a `tmp_path`-cloned source where a `_git(["restore", "..."], cwd=...)` call is added inside a function without `authorization_source`; asserts the check produces a failure with the file/line.
    - `test_audit_wrapper_autoclean_authorization_passes_on_allowlisted_helper` — the same drift but the call is inside `_restore_path`, which is in the allowlist AND IS reachable only from `apply_cleanup` (which has the param); assert pass.
    - `test_audit_wrapper_autoclean_authorization_fails_when_allowlisted_helper_has_unauthorized_caller` — synthetic fixture where `_restore_path` exists with the mutation primitive and a NEW caller `_unauthorized_caller()` (no `authorization_source` param) directly calls `_restore_path`; assert fail.
    - `test_log_event_accepts_wrapper_autoclean_executed_and_blocked` — both events validate against `ALLOWED_LOG_EVENTS`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`

**Description:**
Drift protection for the wrapper layer. The orchestrator-side `_check_fail_task_authorization_source` (TASK-009 of prohibit_silent_revert) catches CLI-level drift; this audit catches Python-level drift in the wrapper layer. AST walk (Codex's recommendation over grep — fewer false positives around comments / helper definitions / test fixtures). The default tier (not strict) means the check surfaces in `audit --json` output but doesn't break preflight; it can be promoted to strict after the false-positive rate is known on the live tree.

**Implementation notes.** Python's `ast.parse(source).walk()` plus a `parent_map` (computed via `ast.walk` + a child-to-parent dict) gives constant-time enclosing-function lookup. The "every direct caller is authorized" rule for the helper-allowlist case is a second AST pass over the same files looking for `Call(func=Name(id="_restore_path"))` and asserting the enclosing function has `authorization_source`. Recursion is not needed in v1 because the call graph is two levels deep; if it ever grows, the audit can be promoted to a fixed-point traversal in a follow-up.

The conditional `plan_gemini_dispatch.py` inclusion uses a simple `if (REPO_ROOT / "plugins/plan-executor/scripts/plan_gemini_dispatch.py").exists():` guard. Gemini wrapper might never ship; the audit must not fail in that case. When the Gemini wrapper does ship, its own task lands the authorization gate AND extends the audit's `ALLOWED_HELPER_NAMES` if it adds new helper functions.

The `os.unlink` / `os.remove` recognition is conservative — pattern-matches are easy to evade; if a future contributor uses `import os as o; o.unlink(p)`, the audit misses it. v1 catches the canonical forms; v2 (follow-up) could extend to import-aliased forms via an import-alias resolver. Not load-bearing for this plan; the gate at `apply_cleanup` / `_restore_in_scope` is the primary defense.
