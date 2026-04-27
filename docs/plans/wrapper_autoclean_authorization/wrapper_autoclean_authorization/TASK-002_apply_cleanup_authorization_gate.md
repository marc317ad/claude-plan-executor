# TASK-002 — `_claude_dispatch_cleanup.apply_cleanup` authorization gate + `plan_claude_dispatch.py` call-site update

## Goal

`_claude_dispatch_cleanup.apply_cleanup` authorization gate + `plan_claude_dispatch.py` call-site update

## Context

The `prohibit_silent_revert` plan (committed `8bb0b7b`) hardened the orchestrator surface against silent destruction of completed work. Its TASK-009 then ran via the bash-dispatched plan-implementer wrapper and **the wrapper destroyed 4 of the 5 implementer-written files** (only `plan_ops.py` survived because it's in the wrapper's protected-paths allowlist).

Two-layer root cause; this task closes Layer B for the Claude wrapper. `_claude_dispatch_cleanup.apply_cleanup` (`:500`) currently has no authorization gate: any caller passing `declared_files_changed=[]` triggers `_restore_path` on every observed delta that isn't protected. The fix mirrors prohibit_silent_revert TASK-001's `cmd_fail_task --authorization-source` design at the wrapper layer — a sentinel-default keyword-only parameter, a closed enum, and a hard-fail on omission/unknown values — so future contributors who add a new caller cannot silently re-introduce the gap.

The discriminator for which authorization value to pass is **agent identity**, anchored at the dispatch-payload's top-level `agent` field (one of `{plan-analyst, plan-implementer, plan-remediator}`). `plan-analyst` is the only read-only agent today and legitimately has empty declared scope; it gets `wrapper-empty-scope-readonly`. Everything else is write-authorized and gets `wrapper-declared-scope` regardless of whether `declared_files_changed` is populated. The wrapper's anti-aliasing guard (write-authorized agent + empty declared scope ≠ readonly path) is the load-bearing fix: it short-circuits to TASK-004's `wrapper_autoclean_blocked` envelope rather than silently reverting, so a Layer-A regression (TASK-001 not landed yet OR a buggy orchestrator that fails to populate the field) cannot destroy work.

### Decisions folded in

1. **Sentinel-default keyword-only param.** Not `required=True` at argparse-style positional level; not a string default with a runtime check. The sentinel default keeps existing positional test seams readable (`apply_cleanup(baseline, [], repo)` is still legal at parse time) but raises a `TypeError` at call time with a message naming the expected enum values. This matches Codex's recommended shape and makes omissions fail loudly at the call site that introduced them.
2. **Closed two-value enum at v1.** `wrapper-declared-scope` (write-authorized agents) and `wrapper-empty-scope-readonly` (read-only agents). The set is intentionally minimal — every additional value is one more code path that future audit checks (TASK-005) need to track. New dispatch shapes that need new authorization values extend the enum in their own task.
3. **Anti-aliasing guard.** A write-authorized agent (`plan-implementer | plan-remediator`) with empty `declared_files_changed` does NOT take the readonly path. Instead, `plan_claude_dispatch.py:cmd_run` short-circuits to TASK-004's `wrapper_autoclean_blocked` envelope. Without this guard, the discriminator could be defeated by Layer-A regressions: an orchestrator that fails to populate `declared_files_changed` would also be the orchestrator likely to mis-pass `wrapper-empty-scope-readonly` for `plan-implementer`, re-introducing silent destruction. The guard is structural — it doesn't trust the caller.
4. **Protected-paths allowlist stays as second line of defense.** `PROTECTED_PATH_PREFIXES` (`_plan_paths.py:31`) protects executor infrastructure (`plan_ops.py`, `plan_codex_dispatch.py`, `.claude/`, `.codex/`, `_run_log.jsonl`, `*.schedule.json`) from all wrapper cleanup unconditionally. Codex's recommendation: keep the allowlist as a backstop "even if every other gate fails". TASK-002 documents the asymmetry (the wrappers themselves are NOT in the prefix list) with a one-line comment but does not change the membership.
5. **Failure mode is `TypeError` (missing) / `ValueError` (unknown).** Per Codex's recommended diagnosis-friendly behavior: omitted param produces a `TypeError` (Python's native "wrong call shape" signal that any reviewer can read in a stack trace); bogus value produces a `ValueError` (Python's native "right shape, wrong content" signal). Both exception types are unambiguously distinct from any backend / cleanup runtime error.

## Verification

- `python3 -c "from _claude_dispatch_cleanup import apply_cleanup; apply_cleanup({}, [], '/tmp')"` exits with `TypeError` whose message contains the literal "expected one of" and the sorted list `['wrapper-declared-scope', 'wrapper-empty-scope-readonly']`.
- `apply_cleanup(baseline, [], repo, authorization_source="bogus")` exits with `ValueError` whose message contains "unknown authorization_source 'bogus'".
- `apply_cleanup(baseline, [], repo, authorization_source="wrapper-declared-scope")` with a non-empty observed delta returns `cleanup_strategy: "delta_bounded"` and reverts the deltas (legacy semantic preserved when authorization is explicit).
- `apply_cleanup(baseline, [], repo, authorization_source="wrapper-empty-scope-readonly")` with a non-empty observed delta reverts the deltas (a read-only agent that wrote anything violated its contract).
- `apply_cleanup(baseline, ["x.py"], repo, authorization_source="wrapper-declared-scope")` with observed delta on `x.py` and `y.py` preserves `x.py` and reverts `y.py`.
- `plan_claude_dispatch.py:cmd_run` step 9 passes `authorization_source="wrapper-declared-scope"` for `agent ∈ {plan-implementer, plan-remediator}` and `"wrapper-empty-scope-readonly"` for `agent == "plan-analyst"`. Verified by integration test that captures the kwargs at `apply_cleanup` via `monkeypatch`.
- Anti-aliasing guard: `plan_claude_dispatch.py:cmd_run` with `agent="plan-implementer"`, `declared_files_changed=[]`, and a non-empty observed delta does NOT call `apply_cleanup` with the readonly value; instead it routes through TASK-004's short-circuit (verified in TASK-004's test).
- All existing callers of `apply_cleanup` in tests are updated; no silent-default fallback in test code.
- `_claude_dispatch_cleanup.py` module docstring (lines 1–53) gains a new "Authorization gate" section under "Public API" documenting the new contract.
- A one-line comment in `_plan_paths.py` near `PROTECTED_PATH_PREFIXES` (line 31) documents the asymmetry: `plan_claude_dispatch.py` and `_claude_dispatch_cleanup.py` are NOT in the prefix list because they ARE the wrapper layer, not consumers.

## Tasks

### TASK-002: `_claude_dispatch_cleanup.apply_cleanup` authorization gate + `plan_claude_dispatch.py` call-site update

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - plugins/plan-executor/scripts/_claude_dispatch_cleanup.py (`apply_cleanup` signature + early gate at line 564 — before the `_changed_paths()` call at line 589 and the classification loop at line 662)
  - plugins/plan-executor/scripts/plan_claude_dispatch.py (`cmd_run` step 9 at `:697` — pass `authorization_source="wrapper-declared-scope"` for write-authorized agents and `"wrapper-empty-scope-readonly"` for `plan-analyst`)
  - plugins/plan-executor/scripts/_plan_paths.py (one-line comment near `PROTECTED_PATH_PREFIXES` at line 31)
  - tests/scripts/test_claude_dispatch_cleanup.py (every `apply_cleanup(...)` call threads `authorization_source` — ~30 call sites across `TestApplyCleanup` classes at lines 173–490)
- **Dependencies:** [001]
- **Test command:** `python3 -m pytest tests/scripts/test_claude_dispatch_cleanup.py -q && python3 -m pytest tests/scripts/test_plan_claude_dispatch_cli.py -q -k "authorization_source or AuthorizationSource"`
- **Acceptance criteria:**
  - `_claude_dispatch_cleanup.py` module-level constants:
    - `_MISSING_AUTHORIZATION_SOURCE = object()` (sentinel).
    - `ALLOWED_CLEANUP_AUTHORIZATION_SOURCES: frozenset[str] = frozenset({"wrapper-declared-scope", "wrapper-empty-scope-readonly"})`.
    - A docstring comment block above the constants enumerates each value and what it authorizes (mirrors `cmd_fail_task`'s `--authorization-source` enum docstring pattern from prohibit_silent_revert TASK-001).
  - `apply_cleanup` signature gains a keyword-only param: `*, authorization_source: object = _MISSING_AUTHORIZATION_SOURCE`. Body raises:
    - `TypeError("apply_cleanup requires authorization_source; expected one of " + sorted(ALLOWED_CLEANUP_AUTHORIZATION_SOURCES))` when `authorization_source is _MISSING_AUTHORIZATION_SOURCE`.
    - `ValueError(f"apply_cleanup: unknown authorization_source {authorization_source!r}; expected one of {sorted(ALLOWED_CLEANUP_AUTHORIZATION_SOURCES)}")` when `authorization_source not in ALLOWED_CLEANUP_AUTHORIZATION_SOURCES`.
    - The check fires **before** `_changed_paths()` (line 589) and **before** the classification loop (line 662), so a missing/bad value cannot silently revert anything.
  - `plan_claude_dispatch.py:cmd_run` step 9 (around `:697`) constructs the `authorization_source` value explicitly:
    ```python
    if agent_name == "plan-analyst":
        authorization_source = "wrapper-empty-scope-readonly"
    else:
        authorization_source = "wrapper-declared-scope"
    ```
    A one-line comment documents the discriminator. The value is passed to `apply_cleanup` as a keyword argument.
  - Anti-aliasing guard: at the same call site, when `agent_name in {"plan-implementer", "plan-remediator"}` AND `len(declared) == 0`, the wrapper does NOT call `apply_cleanup` at all — it routes through TASK-004's short-circuit (this is the load-bearing recovery path). This task lands the structural decision; TASK-004 implements the actual envelope-construction logic.
  - All existing callers of `apply_cleanup` in tests are updated to thread `authorization_source` through. No silent-default fallback in test code. The most ergonomic path is to add a small fixture helper `_call_cleanup(baseline, declared, repo, *, source="wrapper-declared-scope")` near the top of `test_claude_dispatch_cleanup.py` and route every existing call through it; that keeps the diff narrow.
  - `_claude_dispatch_cleanup.py` module docstring (lines 1–53) gains a new section under "Public API" titled "Authorization gate" that documents the new contract (one paragraph + the enum values).
  - A one-line comment in `_plan_paths.py` near `PROTECTED_PATH_PREFIXES` (line 31) documents the asymmetry: ``# Note: plan_claude_dispatch.py and _claude_dispatch_cleanup.py are intentionally NOT in this prefix list — they are the wrapper layer itself, not consumers. Wrapper-self-protection is provided by the authorization-source gate in apply_cleanup, not by this allowlist.``
  - New tests:
    - `test_apply_cleanup_raises_typeerror_without_authorization_source` — call without the param; assert `TypeError` with "expected one of" in message.
    - `test_apply_cleanup_raises_valueerror_on_unknown_authorization_source` — bogus value; assert `ValueError`.
    - `test_apply_cleanup_wrapper_declared_scope_with_empty_declared_reverts_all` — `declared=[]` and authorized; observed delta gets reverted (legacy semantic preserved when authorization is explicit).
    - `test_apply_cleanup_wrapper_empty_scope_readonly_with_empty_declared_reverts_all` — same as above but via the readonly path.
    - `test_apply_cleanup_wrapper_declared_scope_with_nonempty_declared_preserves_in_scope` — `declared=["x.py"]`; observed delta on `x.py` is preserved, on `y.py` is reverted.
    - `test_cmd_run_passes_wrapper_declared_scope_for_plan_implementer` — integration: monkeypatch `_claude_dispatch_cleanup.apply_cleanup`, dispatch with `agent="plan-implementer"`, assert captured kwargs include `authorization_source="wrapper-declared-scope"`.
    - `test_cmd_run_passes_wrapper_empty_scope_readonly_for_plan_analyst` — same but `agent="plan-analyst"`.
- **Reversion guidance:** `git restore plugins/plan-executor/scripts/_claude_dispatch_cleanup.py plugins/plan-executor/scripts/plan_claude_dispatch.py plugins/plan-executor/scripts/_plan_paths.py tests/scripts/test_claude_dispatch_cleanup.py`

**Description:**
Closes Layer B of the defect for the Claude wrapper. The sentinel-default keyword-only param keeps existing positional test seams readable while making omissions fail loudly. The agent-identity-based discriminator (`plan-analyst` ↔ `wrapper-empty-scope-readonly`; everyone else ↔ `wrapper-declared-scope`) anchors the authorization decision in the dispatch payload's agent field rather than allowing any caller to opt out by passing the readonly value casually. The anti-aliasing guard (write-authorized agent + empty declared scope → DON'T fall back to readonly, route to TASK-004 short-circuit) is the load-bearing fix for the TASK-009 destruction event.

**Implementation notes.** The check at the top of `apply_cleanup` is two lines (sentinel comparison + enum membership); the existing function body needs no other changes. The wrapper's `cmd_run` change is similarly small — three lines (one for the `if agent_name == "plan-analyst"` branch, one for the explicit `authorization_source` keyword on the `apply_cleanup` call). The anti-aliasing guard is one extra `if` in `cmd_run` that early-returns on the empty-declared write-authorized case; the actual envelope construction lands in TASK-004.

Test-fixture update strategy: prefer the `_call_cleanup` helper approach over rewriting every assertion. The new helper signature mirrors `apply_cleanup`'s: `_call_cleanup(baseline, declared, repo, *, source="wrapper-declared-scope")`. Most tests use the default; the four new tests above explicitly pass non-default values to exercise the gate.
