# TASK-005 — Preflight `gemini_available` field + `--allow-gemini-fallback` flag plumbing

## Goal

Extend `plan_ops.py preflight` to report `gemini_available: bool` alongside the existing `codex_available`, and add the `--allow-gemini-fallback` orchestrator flag (off by default) to `/implement-plan`'s argument-parsing surface so downstream tasks can route on it.

## Context

The Phase 0 preflight currently emits `codex_available = shutil.which("codex") is not None` (`plan_ops.py:3877`). The Gemini availability check is structurally similar but with an additional auth-key requirement: `gemini` is "available" only when

1. `shutil.which("gemini") is not None`, AND
2. `os.environ.get("GEMINI_API_KEY")` OR `os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")` is non-empty.

The auth-key check matches the wrapper's missing-API-key short-circuit (TASK-003) — the orchestrator should not advertise Gemini as available if the wrapper would refuse to dispatch. The two checks living in different modules MUST agree; the cleanest hygiene is a shared helper `gemini_available()` in a shared module that both `plan_ops.py preflight` and `plan_gemini_dispatch.py` import.

The `--allow-gemini-fallback` flag is opt-in, off by default. Rationale: introducing automatic cross-vendor fallback should be an explicit operator decision per run, not a silent change in semantics for runs that previously degraded gracefully. Per SKILL.md §Phase 1.5, today the unavailable-Codex path logs `plan_review_skipped {reason:"codex_unavailable"}` and proceeds with a summary warning. That degraded-but-proceed behavior is preserved when the flag is off; when the flag is on AND `gemini_available=true`, TASK-006 substitutes Gemini as the reviewer instead of skipping.

The flag is also forwarded to the wrapper (when invoked) so the wrapper knows the orchestrator-level intent and can log accordingly. In v1 the wrapper does not actually do anything different on the flag's presence — it always tries when invoked — but plumbing it through keeps the run-log fields consistent.

**Mutual exclusion.** `--allow-gemini-fallback` is parallel-safe with `--codex-only`, `--claude-only`, `--skip-cross-review`, `--skip-plan-review`, `--codex-review-binding`, and the various binding flags. The only meaningful interaction is with `--codex-only`: that flag drops Claude tasks entirely; if the orchestrator's only adversarial review is Codex's `--codex-only` path AND Codex review fails AND `--allow-gemini-fallback` is on, Gemini fallback fires (the implementation tier and the review tier are independent).

**Out of scope.** Wrapper code (TASK-003 / TASK-004); orchestrator routing (TASK-006 / TASK-007); SKILL.md / dispatch-templates.md prose updates (TASK-006 / TASK-007 carry the prose touching their respective phases — TASK-005's only doc change is the SKILL.md `## Parse arguments` flag table).

## Verification

- `plan_ops.py preflight --json --plan-file <p>` JSON output contains a top-level `gemini_available: bool` field. Type and presence are stable; the field is always present even when both checks fail.
- A new helper `_resolve_gemini_available() -> bool` in `plan_ops.py` (or in a shared module imported by both `plan_ops` and `plan_gemini_dispatch`) is the SOLE source of truth for the check. Both `preflight` and the wrapper's missing-API-key short-circuit (TASK-003) reference it.
- Helper signature respects the auth-key contract: returns `False` when `gemini` binary is absent OR when both `GEMINI_API_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` env vars are unset/empty.
- Helper is testable via pytest's `monkeypatch.setenv` / `monkeypatch.delenv` — no global state.
- New tests in `tests/scripts/test_plan_ops.py` cover the helper's truth table:
  - binary present + key present → True
  - binary present + key absent → False
  - binary absent + key present → False
  - binary absent + key absent → False
- The `--allow-gemini-fallback` flag is added to the argument-parsing section of `SKILL.md` (the table under `## Parse arguments`). NO behavior change yet — this task is plumbing-only.
- `SKILL.md`'s argument-parsing description for `--allow-gemini-fallback` reads exactly: *"Enable Gemini-CLI fallback for adversarial review when Codex is unavailable or transiently fails. Implementation tasks remain Codex-only."* The wording is load-bearing — TASK-006 / TASK-007 reference it.
- The audit (`plan_ops.py audit --json`) gains an entry under `gemini-available` (advisory tier) that surfaces the helper's truth-table state at audit time, useful for executor-self-checks. Auditors that don't run with `--strict` see no behavior change.
- `venv/bin/pytest -q tests/scripts/test_plan_ops.py` returns 0.

## Tasks

### TASK-005: Preflight `gemini_available` field + `--allow-gemini-fallback` flag plumbing

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (edit — preflight + audit + helper)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (edit — `## Parse arguments` table only; do NOT touch Phase 1.5 / Phase D.1 prose in this task — those are TASK-006 / TASK-007 scope)
  - `tests/scripts/test_plan_ops.py` (edit — append helper tests)
- **Dependencies:** [001]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_ops.py:3870-3920` (preflight return-shape construction; the field-add seam)
  - `plugins/plan-executor/scripts/plan_ops.py:1580-1610` (`_resolve_python` — example of a similar resolver helper to mirror style for `_resolve_gemini_available`)
- **Symbol targets:**
  - `_resolve_python` in `plan_ops.py`
  - the `audit` subcommand's check registration block (search `CANONICAL_CONTRACT` and the `_audit_*` helpers)
- **Acceptance criteria:**
  - The four-row truth-table tests pass.
  - `preflight --json` emits `gemini_available: bool` on every invocation.
  - The `--allow-gemini-fallback` flag is documented in SKILL.md's argument-parsing table; the wording is exactly the load-bearing string above.
  - The `audit` subcommand registers the new check at advisory tier (does NOT flip default-tier audit verdicts).
  - The new helper has docstring + module-level constants for the env-var names (`GEMINI_API_KEY_ENV = "GEMINI_API_KEY"`, `GOOGLE_APP_CRED_ENV = "GOOGLE_APPLICATION_CREDENTIALS"`) so the test can assert against the same names the helper reads.
  - No behavior change in any other code path (Phase 1.5 / Phase D.1 prose remains untouched in this task).
- **Reversion guidance:** revert the three files; the new `gemini_available` field is additive — its absence does not break existing JSON consumers because every consumer in the repo uses dict `.get` with a default (verify at write time).

**Description:**
Plumbing-only task: add the helper, add the preflight field, add the SKILL.md flag entry, add the audit check. No phase-routing prose changes. The helper lives in `plan_ops.py` so both the orchestrator's preflight AND the wrapper (TASK-003 imports it for its missing-API-key short-circuit) reference one source of truth.

**Implementation notes:**
- Place the helper near `_resolve_python` so future readers find similar resolvers together.
- Audit-check naming convention: `gemini-available` mirrors the `codex-available` analogue (which doesn't exist today but is implied by the preflight field). If you add a `codex-available` audit check at the same time as belt-and-braces symmetry, do it in a separate commit so this task stays scope-tight.
- The flag is added to argparse in the orchestrator's command-parsing layer. Today /implement-plan's argument-parsing is documented in SKILL.md's `## Parse arguments` table — the orchestrator (Claude-Opus running the skill) reads the table at run-time. There is no Python argparse module to edit for the orchestrator-level flag; the flag is "real" because it is documented, normalized, and routed in the skill's prose. TASK-005's job is to add the row to the table; TASK-006 / TASK-007 wire the routing.
- The `parse-preflight-report` subcommand (if it exists; otherwise the JSON consumers in SKILL.md) MUST gracefully accept the new field. Verify by running `plan_ops.py preflight --json` against the existing `sample_phase4.md` fixture and confirming downstream consumers do not error on the additional key.
