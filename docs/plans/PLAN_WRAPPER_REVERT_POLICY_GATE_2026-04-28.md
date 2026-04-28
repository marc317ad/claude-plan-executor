# PLAN — Wrapper Revert-Policy Gate (no destruction without operator authorization)

**Status:** Pending
**Created:** 2026-04-28
**Base branch:** main
**Depends on plans:** wrapper_autoclean_authorization (committed), prohibit_silent_revert (committed)

## Problem

`/implement-plan` exposes `--unattended-revert-policy=pause|fail-fast|preserve-only` as the operator's authorization for revert decisions in unattended runs. The flag is consumed by the orchestrator (`plan_ops.py preflight` and `plan_ops.py review-route`) — it correctly governs orchestrator-level revert/pause behavior in `review-route` and the awaiting-user pause subroutine.

The flag does NOT reach the dispatch-wrapper layer. The three wrappers (`plan_claude_dispatch.py`, `plan_codex_dispatch.py`, `plan_gemini_dispatch.py`) do not receive `unattended_revert_policy` in their input payloads. The shared cleanup module `_claude_dispatch_cleanup.apply_cleanup()` is gated only by `authorization_source` (anti-aliasing guard for empty declared scope; otherwise revert is unconditional). For a write-authorized agent (`plan-implementer` / `plan-remediator`) with a non-empty `declared_files_changed`, `apply_cleanup` reverts every observed-delta path that is not in the local declared scope — by design.

Concrete failure mode (run `20260428T121041`): TASK-005's wrapper called `apply_cleanup` ~14 minutes after TASK-008 (parallel sibling) had finished, observed TASK-008's correct edit to `tests/scripts/test_plan_codex_dispatch_schema.py` as "out of TASK-005's declared scope," and reverted it. The orchestrator's pause was a downstream consequence; the destructive act was upstream of the operator's policy gate.

The cleanup module's docstring at `_claude_dispatch_cleanup.py:646–652` makes the design intent explicit:

> Two concurrent callers operating on disjoint declared scopes will see each other's writes as "out of scope" relative to their own baseline — which is the correct safe behavior.

That stated invariant is what this plan overrides. The operator-stated rule is: **no process in this workflow is allowed to revert without express permission via the documented policy flag.** Even in single-task runs (no sibling-task confound), the wrapper-level `apply_cleanup` is destroying state that the operator's policy choice should govern.

## Decisions folded in

1. **Minimum change is in the cleanup module's gate** — the fix is to thread the orchestrator's pinned `unattended_revert_policy` into the wrapper input payload, then have `apply_cleanup` consult the policy before performing any restore / delete. No change to the cleanup module's classification logic (the `out_of_scope` set, the `_restore_path` translation, the misreport detection); only the destructive-action decision changes.
2. **Policy → action mapping mirrors the orchestrator's existing semantics:**
   - `pause` and `fail-fast` (the two non-destructive policies): `apply_cleanup` MUST detect the violation, populate `out_of_scope_paths` and `scope_violation_detected`, and return WITHOUT calling `_restore_path` on any path. The working tree is left as the inner agent left it. The wrapper still emits the `scope_violation` envelope (TASK-002 of `wrapper_autoclean_authorization` already wires this) so the orchestrator's `review-route` and reconcile-batch can decide what to do.
   - `preserve-only` (the operator-explicit destructive policy): existing `apply_cleanup` behavior — the operator opted into salvage-then-revert. (Note: `preserve-only`'s side-ref salvage is a separate concern not addressed here; this plan preserves whatever the existing behavior is for that policy. A follow-up plan can add salvage-to-side-ref if it isn't already implemented.)
3. **Default when policy field is absent stays "pause" (non-destructive)** — for any caller that emits a v1 wrapper input without the new field, the wrapper SHALL behave as `pause`. This is the safer-by-default choice and avoids a backwards-incompatible flag-day. The orchestrator always populates the field, so legacy-default behavior is reachable only by direct CLI callers (testing, manual recovery), where non-destructive-by-default is the right call.
4. **No `apply_cleanup` signature break** — add the policy as a keyword-only argument with a documented default. Existing callers continue to compile; only behavior under non-`preserve-only` policies changes.
5. **Out of scope (deferred):**
   - Authorship-aware cleanup (restricting `observed_delta` to writes the inner agent itself caused via stream-parse or worktree isolation). This is the deeper architectural fix; the policy gate is a sufficient minimum. The deferred concern goes into `docs/analysis/` as a follow-up. Under the policy gate, the wrapper never destroys without the operator's explicit `preserve-only` choice, which subsumes the user-facing harm of authorship-blind cleanup.
   - Sibling-task scope partitioning in reconcile-batch (the parallel-batch race fix Gemini suggested in `(B)`). Not needed once the wrapper stops destroying — the orchestrator's reconcile-batch already has the cross-task picture and will decide correctly.
   - Cron / non-TTY default behavior (the `unattended-revert-policy-required` enforcement at `plan_ops.py:4962`) is unchanged. The wrapper just respects whatever the orchestrator pinned.

## Scope boundary

- **In scope (3 files of code, 1 schema, 3 wrappers, 2 test files):** `_claude_dispatch_cleanup.py` (gate logic + signature), three wrapper scripts (`plan_claude_dispatch.py`, `plan_codex_dispatch.py`, `plan_gemini_dispatch.py`) (thread the field through their input → cleanup call), the shared input schemas under `plugins/plan-executor/scripts/schemas/` (add the optional field), `plan_ops.py build-claude-dispatch-input` and `build-codex-dispatch-input` and `build-gemini-dispatch-input` if present (populate the field from `$UNATTENDED_REVERT_POLICY`), `tests/scripts/test_claude_dispatch_cleanup.py` (gate-under-each-policy tests), `tests/scripts/test_plan_claude_dispatch_cli.py` (wire-level integration), `tests/scripts/test_plan_codex_dispatch_*.py`, `tests/scripts/test_plan_gemini_dispatch_*.py` if extant.
- **Out of scope:** any change to `_restore_path`, `snapshot_baseline`, the per-file gate translation, the misreport detection, or the orchestrator-level `review-route` consumer of the policy. Those code paths already work; this plan only changes WHAT `apply_cleanup` does in the destructive vs. non-destructive branch.

## Verification

- After the plan lands: re-running `/implement-plan docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25 --claude-only --unattended-revert-policy=pause` (with parallelism enabled) reproduces the same race-detection signal but `tests/scripts/test_plan_codex_dispatch_schema.py` is NOT reverted. The orchestrator pauses with both implementers' edits intact in the working tree, and reconcile-batch can decide whether to commit, partition, or fail.
- Direct unit test: `cleanup.apply_cleanup(baseline, declared, repo_root, authorization_source=..., unattended_revert_policy="pause")` returns `scope_violation_detected=True`, `out_of_scope_paths=[...]`, but `restored=[]`, `deleted=[]`, `failed_paths=[]`, `cleanup_strategy="detect_only_revert_policy_pause"`. The on-disk file content for the out-of-scope path is unchanged from the inner agent's write.
- Integration smoke: `python3 plan_claude_dispatch.py run --input <payload-with-policy-pause-and-out-of-scope-write>` exits with `status=scope_violation`, `extra.wrapper_events[0].event="wrapper_autoclean_blocked_by_policy"`, and the working tree retains the out-of-scope write.

## Tasks

### TASK-001: `apply_cleanup` policy gate in `_claude_dispatch_cleanup.py`

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/_claude_dispatch_cleanup.py` (add keyword-only `unattended_revert_policy` arg to `apply_cleanup`; add the gate; update return-shape docs and `cleanup_strategy` enum)
  - `tests/scripts/test_claude_dispatch_cleanup.py` (3 new tests: detect-only under `pause`, detect-only under `fail-fast`, salvage-then-revert under `preserve-only` continues to behave as today)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_cleanup.py`
- **Acceptance criteria:**
  - `apply_cleanup` signature gains a keyword-only argument `unattended_revert_policy: Optional[str] = None`. When `None`, the function behaves as `pause` (non-destructive). When the value is not in the closed set `{"pause", "fail-fast", "preserve-only"}` the function raises `ValueError` with a message naming the unknown value (parallel to the existing `authorization_source` validation at line 662).
  - When `unattended_revert_policy in {"pause", "fail-fast", None}`: the function MUST detect the out-of-scope set exactly as today (same `observed_delta` math, same protected-skip handling, same misreport logic, same `out_of_scope_paths` population) but MUST NOT call `_restore_path` on any out-of-scope path. `restored=[]`, `deleted=[]`, `failed_paths=[]`. `scope_violation_detected` is computed from `out_of_scope_paths` (unchanged). `cleanup_strategy` is `"detect_only_revert_policy_pause"` when policy is `pause` (and `None`-default), `"detect_only_revert_policy_fail_fast"` when `fail-fast`.
  - When `unattended_revert_policy == "preserve-only"`: the function behaves identically to today (existing `_restore_path` loop + `restored`/`deleted`/`failed_paths` population; `cleanup_strategy="delta_bounded"`).
  - The return-dict shape grows by ZERO keys; only the values of `restored` / `deleted` / `failed_paths` / `cleanup_strategy` differ across policies. Downstream consumers (`plan_claude_dispatch.py:771–781`, the wrapper-events emission) require no changes.
  - Module docstring at `_claude_dispatch_cleanup.py:646–652` is rewritten to reflect the new contract: under `pause`/`fail-fast`, two concurrent callers' writes are NOT mutated; the orchestrator's reconcile-batch is authoritative for cross-task scope partitioning.
  - Tests:
    - `test_apply_cleanup_pause_policy_detects_but_does_not_revert` — write a file outside declared scope, call `apply_cleanup(..., unattended_revert_policy="pause")`, assert `out_of_scope_paths == [path]`, `restored == []`, `deleted == []`, file content on disk is unchanged from the test-induced write.
    - `test_apply_cleanup_fail_fast_policy_detects_but_does_not_revert` — same as above but with `fail-fast`; `cleanup_strategy == "detect_only_revert_policy_fail_fast"`.
    - `test_apply_cleanup_preserve_only_policy_unchanged` — `unattended_revert_policy="preserve-only"`; behaves identically to today's no-policy-arg call (existing test must still pass; add an explicit assertion that `restored` is non-empty when an out-of-scope file is written).
    - `test_apply_cleanup_default_policy_is_pause` — call without the policy kwarg; assert non-destructive behavior. Documents the safer-by-default contract.
    - `test_apply_cleanup_unknown_policy_raises_valueerror` — `unattended_revert_policy="yolo"`; assert `ValueError` containing the literal `"yolo"`.

### TASK-002: Thread `unattended_revert_policy` from orchestrator → wrapper payload → `apply_cleanup` for all three wrappers

- **Status:** pending
- **Priority:** critical
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/schemas/claude_dispatch_input.json` (add `unattended_revert_policy` as optional top-level field; closed enum `["pause", "fail-fast", "preserve-only"]`; default unset; description references `--unattended-revert-policy`)
  - `plugins/plan-executor/scripts/schemas/codex_dispatch_input.json` (same field, same shape)
  - `plugins/plan-executor/scripts/schemas/gemini_dispatch_input.json` (same field, same shape) — only if this schema file exists; if Gemini wrapper inlines its schema, update the inline version
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py` (extract `unattended_revert_policy` from validated input at the same level as `declared_files_changed`; pass it to `cleanup.apply_cleanup` at line 748)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (analogous edit; if codex has its own cleanup call, mirror the threading)
  - `plugins/plan-executor/scripts/plan_gemini_dispatch.py` (analogous edit; the four `_handle_timeout_cleanup` sites I patched on commit a7be6c5 also feed into this — verify they propagate the policy)
  - `plugins/plan-executor/scripts/plan_ops.py` (the `build-claude-dispatch-input`, `build-codex-dispatch-input`, and `build-gemini-dispatch-input` subcommands populate the field from `$UNATTENDED_REVERT_POLICY` env var, falling back to the value pinned by `cmd_preflight`; if no value is resolvable, omit the field — let the wrapper default kick in)
  - `tests/scripts/test_plan_claude_dispatch_cli.py` (1 new test: end-to-end policy threading — payload with `unattended_revert_policy=pause` + an out-of-scope write yields a `scope_violation` envelope with the working tree retaining the write)
  - `tests/scripts/test_plan_codex_dispatch_cli.py` if extant, or whichever fixture file covers the Codex wrapper CLI (1 analogous test)
  - `tests/scripts/test_plan_gemini_dispatch_cli.py` if extant (1 analogous test)
  - `tests/scripts/test_plan_ops.py` (1 new test per builder subcommand: emitted JSON's top-level `unattended_revert_policy` matches the env-var input)
- **Dependencies:** ["001"]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch_cli.py tests/scripts/test_plan_codex_dispatch_cli.py tests/scripts/test_plan_gemini_dispatch_cli.py tests/scripts/test_plan_ops.py -k "unattended_revert_policy or revert_policy_gate or RevertPolicy"`
- **Acceptance criteria:**
  - All three wrapper-input schema files (or inlined schemas) declare `unattended_revert_policy` as an optional top-level property with the closed enum `{"pause", "fail-fast", "preserve-only"}`. `description` cross-references the orchestrator-level flag.
  - All three wrapper scripts read the field from the validated input and pass it to `cleanup.apply_cleanup(...)` (or analogous Codex/Gemini cleanup call) as the `unattended_revert_policy` keyword argument.
  - All three `build-*-dispatch-input` subcommands populate the field from `os.environ.get("UNATTENDED_REVERT_POLICY")`. If unset, the field is omitted (wrapper defaults to non-destructive). If set to a value not in the enum, the subcommand exits non-zero with `errors[*].code = "unattended-revert-policy-invalid"` (parallel to `cmd_preflight`'s validation at line 4960).
  - End-to-end test: `python3 plan_claude_dispatch.py run --input <payload-with-pause-and-an-out-of-scope-write>` returns a `scope_violation` envelope; `git status --porcelain` confirms the out-of-scope file's edit is still on disk; `envelope.extra.wrapper_events` carries an event documenting the policy-gated non-destruction.
  - The schema-audit fixture (per TASK-008 of PLAN_GEMINI_INTEGRATION) covers the new field across all three wrapper-input schemas.
  - Direct CLI smoke (one per wrapper): a payload with `unattended_revert_policy: "preserve-only"` + an out-of-scope write reverts the file (existing destructive behavior is preserved when the operator explicitly opts in).
