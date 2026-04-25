# TASK-004 — Codex wrapper file-count-aware timeout scaling (Issues 2 + 3)

## Goal

Stop killing legitimate Codex deliberation on multi-file mechanical work. Today's flat 300 s implement / 180 s review caps are tight for tasks that touch 4+ files because Codex's planning loop scales roughly linearly with the number of files it has to inspect. This task introduces a file-count-aware `--timeout` default in the wrapper, plumbs the override through SKILL.md and `dispatch-templates.md`, and keeps the existing back-compat floors so a single-file task does not get a longer leash than it needs.

## Scoped Context

**Today's defaults (`plan_codex_dispatch.py:77-79`).**

```
DEFAULT_TIMEOUT_IMPLEMENT = 300
DEFAULT_TIMEOUT_REVIEW = 180
DEFAULT_TIMEOUT_PLAN_REVIEW = 180
```

The wrapper's `--timeout N` arg has been pluggable since the wrapper was written (`add_common(p, default_timeout)` at `plan_codex_dispatch.py:1782`). What is missing is the *default-derivation* layer: today the wrapper picks a constant default; SKILL.md prescribes the constant verbatim; the dispatch templates echo it again.

**The proposed scaling.**

- `implement_timeout(num_files) = max(300, 60 * num_files)` — adds 60 s per declared file. A 5-file task gets 300 s; a 6-file task gets 360 s. This matches the empirical observation that the friction run's 3-file mechanical task ran out of time in Codex's planning step (~100 s/file budget).
- `review_timeout(num_files) = max(180, 30 * num_files)` — adds 30 s per file under review. The friction run's 8-file diff hit 180 s mid-verification; the new scaling yields 240 s.
- `plan_review_timeout` stays a flat 180 s (the schedule is bounded; per-task scaling does not apply).

**Where the scaling lives.**

- The wrapper computes the derived default itself when `--timeout` is NOT explicitly passed. `len(task["files"])` (after `_extract_bullet_list`) is the file count. For `cmd_review`, `len(review_files)` (after `--files` parsing) is the file count.
- The `--timeout N` flag continues to be an explicit override. When the operator passes `--timeout`, no scaling is applied (operator is in charge).
- `add_common` no longer hard-codes a constant; it adds the flag with `default=None` and the cmd handler computes the per-call default.

**Operator surface (SKILL.md + dispatch-templates.md).**

- `SKILL.md:594` (Phase B-Codex implement) — drop the hard-coded `--timeout 300`; document that the wrapper now scales with file count and takes an optional `--timeout` override.
- `SKILL.md:665` (Phase D.1 review) — drop the hard-coded `--timeout 180`; same documentation update.
- `SKILL.md:419` (Phase 1.5 plan-review) — keeps `--timeout 180` (per the rule above).
- `dispatch-templates.md` Phase B-Codex (line ~370 and the bash template at line ~378) and Phase 1.5 / Phase D.1 mirror SKILL.md. Drop the inline `--timeout 300|180`; document the scaling.
- `SKILL.md:21` (the Bash-call timeout idiom rule) currently asserts `300000ms (300s) for plan_codex_dispatch.py implement` — update to a file-count-aware ceiling note: the orchestrator's `Bash` tool timeout MUST be set to at least the wrapper's effective timeout (compute as `max(300_000, 60_000 * num_files)` for implement; `max(180_000, 30_000 * num_files)` for review). The wrapper enforces its own internal timeout; the Bash call's outer ceiling has to cover that plus a small buffer.

**The implement-side spillover safety net (the open piece of Issue 2).**

The friction run also documented Codex touching `task008_sweep_coordinator.py` outside scope without it appearing in `out_of_scope_tracked`. Re-reading the wrapper's `_handle_timeout_cleanup` (lines 890-982) shows the existing logic already classifies non-allowed deltas as `out_of_scope_tracked` correctly when baseline is captured. The likeliest explanation for the missing entry is that `_snapshot_baseline` returned `captured=False` (the `try/except subprocess.SubprocessError, OSError`) and the cleanup short-circuited.

This task adds an instrumentation-only enhancement: when `_snapshot_baseline` returns `captured=False`, the timeout envelope's `cleanup_strategy` MUST be `"skipped_no_baseline"` (it already is) AND the envelope MUST surface the underlying error reason in a new `baseline_error: str | null` field so the orchestrator can route differently. No code-restoration heuristic is added; doing so without a baseline would risk wiping unrelated work.

**Out of scope.**

- Changing the way the orchestrator escalates timeouts (Phase B's "any outcome ≠ success → fallback to Claude" rule stays). TASK-006 owns the Phase D.1 review-timeout routing rule explicitly.
- Per-file dispatch splitting for large diffs (analysis lists this as an alternative for Issue 3; defer as a follow-up — file-count scaling is the cheaper first cut).
- Stripping prose from the `Files:` block in the rendered prompt — that surface-area drops to zero once TASK-002 lands the unified parser, since the wrapper consumes paths only after normalisation.

## Verification

- `plan_codex_dispatch.py` defines `compute_implement_timeout(num_files: int) -> int` returning `max(300, 60 * num_files)` and `compute_review_timeout(num_files: int) -> int` returning `max(180, 30 * num_files)`.
- `add_common` sets `--timeout` default to `None`. `cmd_implement` derives the effective timeout as `args.timeout if args.timeout is not None else compute_implement_timeout(len(task["files"]))`. `cmd_review` derives it from `len(review_files)`. `cmd_plan_review` keeps the flat default.
- The wrapper envelope's existing `wall_seconds` field is unchanged. A new `effective_timeout: int` field is added to all three subcommand envelopes (implement, review, plan-review) so the run log records what cap actually applied. (Plan-review reports the flat default.)
- The implement timeout envelope additionally carries `baseline_error: str | null` populated from the captured `subprocess.SubprocessError` / `OSError` message (truncated to 200 chars). `null` when baseline capture succeeded.
- `SKILL.md` updates:
  - Line ~594 (Phase B-Codex bash template): drop `--timeout 300`; replace the inline `--timeout` line with prose documenting the file-count scaling and the operator override.
  - Line ~665 (Phase D.1 review bash template): drop `--timeout 180`; same prose.
  - Line ~21 (the timeout-idiom bullet): describe the new outer-ceiling formula `max(300_000ms, 60_000 * len(files))` for implement / `max(180_000ms, 30_000 * len(files))` for review. Plan-review remains 180_000ms.
- `dispatch-templates.md` updates: Phase B-Codex (~line 370+) and Phase 1.5 / Phase D.1 (~line 50+, ~line 660+) drop the hard-coded `--timeout` value and reference SKILL.md's scaling rule.
- New unit tests in `tests/scripts/test_plan_codex_dispatch.py` (or `test_plan_codex_dispatch_parsing.py` — wherever the closest existing class lives):
  - `test_compute_implement_timeout_floor` (1 file → 300, 2 → 300, 5 → 300, 6 → 360, 10 → 600).
  - `test_compute_review_timeout_floor` (1 file → 180, 6 → 180, 7 → 210, 10 → 300).
  - `test_implement_envelope_carries_effective_timeout_and_baseline_error` (mock `_snapshot_baseline` to return `captured=False` with a synthetic error; assert envelope keys present).
  - `test_explicit_timeout_override_skips_scaling` (pass `--timeout 100`; effective timeout is 100 regardless of file count).
- All existing wrapper tests pass unchanged.
- `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py tests/scripts/test_plan_codex_dispatch_parsing.py` returns 0.

## Tasks

### TASK-004: Codex wrapper file-count-aware timeout scaling

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (edit)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (edit)
  - `tests/scripts/test_plan_codex_dispatch.py` (edit)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py tests/scripts/test_plan_codex_dispatch_parsing.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:75-82` (existing default constants)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1085-1180` (`cmd_implement` head — baseline capture + invoke + timeout envelope)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1356-1450` (`cmd_review` head)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1761-1840` (`_build_parser` / `add_common` / per-subcommand parser construction)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:17-25` (timeout-idiom bullet)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md:585-670` (Phase B-Codex + Phase D.1 paragraphs and bash templates)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md:44-60` (Phase 1.5 bash template) and `dispatch-templates.md:370-460` (Phase B-Codex / D.1 bodies)
- **Symbol targets:**
  - `_handle_timeout_cleanup` in `plan_codex_dispatch.py`
  - `cmd_implement` in `plan_codex_dispatch.py`
  - `cmd_review` in `plan_codex_dispatch.py`
  - `_build_parser` in `plan_codex_dispatch.py`
- **Acceptance criteria:**
  - `compute_implement_timeout` and `compute_review_timeout` helpers exist in the wrapper module with the formulas above and are exercised by parameterised unit tests.
  - `--timeout` defaults to `None` for `implement` and `review` subparsers; `cmd_implement` / `cmd_review` derive the effective default from the file count when the flag is unset, otherwise honour the operator's value.
  - The implement, review, and plan-review wrapper envelopes carry `effective_timeout: int` reporting the cap actually used.
  - The implement envelope on `outcome: "timeout"` carries `baseline_error: str | null` capturing the `_snapshot_baseline` failure message (truncated to 200 chars) when baseline capture failed; `null` when it succeeded.
  - `SKILL.md` line ~21 (Bash-call timeout idioms bullet) reads (verbatim or near-verbatim): *"Bash-call outer timeout MUST cover the wrapper's effective internal timeout plus a small buffer. Implement: `max(300_000ms, 60_000 * len(files))`. Review: `max(180_000ms, 30_000 * len(files))`. Plan-review: `180_000ms` (flat). The wrapper enforces its own internal timeout; pass `--timeout N` only to override."*
  - `SKILL.md` §Phase B-Codex (line ~594) bash template REMOVES the literal `--timeout 300` from the multi-line invocation and adds a one-line note immediately above the bash block: *"Wrapper computes the timeout default from `len(task["files"])` per the formula in §Bash-call idioms; pass `--timeout N` to override."*
  - `SKILL.md` §Phase D.1 (line ~665) bash template REMOVES the literal `--timeout 180` and adds the same one-line note referencing the review formula.
  - `SKILL.md` §Phase 1.5 (line ~419) bash template KEEPS `--timeout 180` (per the rule that plan-review is bounded and does not need scaling).
  - `dispatch-templates.md` Phase B-Codex (line ~370) bash template REMOVES the literal `--timeout 300` and references SKILL.md §Bash-call idioms by name.
  - `dispatch-templates.md` Phase 1.5 (line ~50) bash template KEEPS `--timeout 180` (mirrors SKILL.md).
  - `dispatch-templates.md` Phase D.1 — search for any inline `--timeout` literal in the file (currently none expected, but verify with `grep -n "timeout" plugins/plan-executor/skills/implement-plan/dispatch-templates.md`); if present, align with the SKILL update.
  - New unit tests in `tests/scripts/test_plan_codex_dispatch.py` cover the scaling helpers, the explicit override, and the timeout envelope's new fields. The existing tests (parser scope, JSON envelope shape, normalize_task_id, parse_task_block, etc.) keep passing.
  - `venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch.py tests/scripts/test_plan_codex_dispatch_parsing.py` returns 0.
- **Reversion guidance:** restore the three `DEFAULT_TIMEOUT_*` constants, revert `add_common` to the constant default, drop the new helpers, drop the new envelope fields, and revert the SKILL / dispatch-templates updates. The pre-fix flat-cap behaviour is suboptimal but well-understood.

**Description:**
Replace the wrapper's three flat timeout constants (`DEFAULT_TIMEOUT_IMPLEMENT = 300`, `DEFAULT_TIMEOUT_REVIEW = 180`, `DEFAULT_TIMEOUT_PLAN_REVIEW = 180`) with file-count-aware helpers for implement and review, keeping the plan-review default flat. Plumb the change through SKILL.md and dispatch-templates.md so the documented bash invocations no longer hard-code a `--timeout N` value (the wrapper picks the right floor; operators override with `--timeout` when they have a reason). Also surface `effective_timeout` in every envelope so the run-log records the cap used, and capture the underlying `_snapshot_baseline` error in the implement timeout envelope so the orchestrator can route differently when baseline capture itself failed.

**Implementation notes:**
- Keep the three `DEFAULT_TIMEOUT_*` module-level constants as named floors used by the helpers — that preserves the operator-readable contract and avoids magic numbers in the formulas.
- The `effective_timeout` field is purely informational; the orchestrator does NOT consume it for routing in this task. (TASK-006 wires the timeout-routing rule for review.)
- The `baseline_error` field is OPTIONAL in the schema sense — `null` is the common case. Existing tests that assert envelope shape need to either accept the new optional key or be updated to assert it explicitly.
- `_snapshot_baseline` already wraps git calls in `try/except (subprocess.SubprocessError, OSError)`. Capture the exception message into a thread-local-style buffer (or return it alongside `captured: bool`) and forward into the envelope from `cmd_implement`.
- Do NOT change the wrapper's internal `subprocess.run(timeout=timeout_sec)` call site — `invoke_codex` keeps using the resolved value.
- The Bash-call outer ceiling rule in SKILL.md §timeout idioms is informational; orchestrators reading SKILL.md choose their `Bash` tool `timeout=` value based on the formula. Keep the rule terse — one bullet per timeout class.
