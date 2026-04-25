# Plan: Gemini CLI as Adversarial-Review Fallback for /implement-plan

**Created:** 2026-04-25
**Status:** Pending
**Base branch:** main
**Informed by:** [docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-21.md](PLAN_GEMINI_INTEGRATION_2026-04-21.md) (general-purpose Gemini wrapper, broader scope; this plan is the narrowly-scoped /implement-plan integration sibling)

## Goal

Stand up a `plan_gemini_dispatch.py` wrapper restricted to two adversarial-review subcommands (`review`, `plan-review`) and wire it into `/implement-plan` as a **fallback** for the existing Codex-tier review paths. Codex remains the primary reviewer; Gemini fires only when (a) `codex_available=false` at preflight, OR (b) a single Codex review/plan-review dispatch returns a transient failure (`timeout | parse_error | failure`) AND the operator opted in via `--allow-gemini-fallback`.

## Context

### Where Gemini fits in the dual-agent executor

Today the executor has three adversarial-review seams, all Codex-driven:

1. **Phase 1.5 — Codex `plan-review`** — independent pre-dispatch review of the analyst-authored schedule (`plan_codex_dispatch.py plan-review`).
2. **Phase D.1 — Codex `review` of Claude-implemented work** — cross-family review (`plan_codex_dispatch.py review`).
3. **Phase B-Codex — Codex `implement` of Codex-tier tasks** — adversarial only in cross-review (Phase D.1 swaps reviewer roles for Codex-implemented work, where Claude reviews).

Of these three, Gemini is only a viable fallback for (1) and (2) — the **review** seams. (3) is implementation, not review, and the user has explicitly scoped this effort to *"codex needs to be used in implementation and adversarial review of code changes; gemini acts as a backup for the adversarial tasks if codex becomes unavailable."* Gemini does not replace Codex for implementation under any condition.

Gemini also does NOT belong in:

- **Phase D.5 third-opinion** — the third opinion is structurally Claude-Sonnet adjudicating Codex findings against Claude's own implementation; the verdict lattice in §8.4 / D.2a.5 / D.2a.6 assumes that asymmetry. Adding Gemini here would create a 4-way verdict tangle the orchestrator was not designed for.
- **Phase 1-triage / Phase 1.5.5 triage** — same lattice argument. Triage is Claude-Sonnet by design.
- **Phase 1 analyst (`build-tasks` + per-child classifier)** — deterministic Python plus a narrow Sonnet classifier. Gemini latency / cost would buy nothing.

### The 2026-04-21 plan is broader; this plan is narrower

The earlier `PLAN_GEMINI_INTEGRATION_2026-04-21.md` (authored by Gemini) describes a **general-purpose** Gemini dispatch wrapper at `gemini_integration/` with `Researcher`, `Investigator`, and `Architect` roles. That broader plan is good and stays valid as future work; this plan does NOT supersede it. The two plans are complementary:

- The 2026-04-21 plan delivers a generic Gemini-as-research-tool surface usable by Claude/Codex agents and CI.
- This 2026-04-25 plan delivers a narrow `plan_gemini_dispatch.py` lookalike of `plan_codex_dispatch.py`, scoped to the two review subcommands the executor needs as a Codex fallback.

The wrappers can converge later if the broader plan lands. They live in different roots (`plugins/plan-executor/scripts/` vs. `gemini_integration/`) so neither blocks the other.

### Empirical findings from probing the installed Gemini CLI (v0.39.0)

Before authoring this plan, I consulted the installed Gemini CLI directly to ground the wrapper design in observed behavior. The findings adjust three load-bearing assumptions in the 2026-04-21 plan:

1. **No `--output-schema` equivalent.** Gemini does NOT have a flag mirroring Codex's `--output-schema PATH` that enforces the model output against a JSON schema. The recommended pattern (per Gemini's own documentation when asked) is to embed the schema in the prompt verbatim, instruct the model to return raw JSON in `.response`, and validate post-hoc in the wrapper. This implies the wrapper needs **bounded re-prompt retry on schema-validation failure** — a non-trivial control flow that has no analogue on the Codex side.
2. **`session_id` is `stream-json`-only.** The `-o json` envelope shape is `{response, stats, error?}`. The 2026-04-21 plan's verification matrix expected `session_id` to always be present; that expectation must be relaxed.
3. **Exit codes.** 0 success, 1 general/model error, 42 input error (empty prompt), 53 turn-limit-exceeded. The 2026-04-21 plan listed 0/1/42/53 — confirmed, with semantics now bound: 53 is reachable in long-context investigations and the wrapper must treat it as a discrete `turn_limit` failure, not a generic 1.

Two additional gotchas the broader plan understated:

- **Headless OAuth refresh can hang.** If `GEMINI_API_KEY` (or a Vertex AI service account) is not set, Gemini may attempt an interactive browser launch for token refresh and block the wrapper indefinitely. The wrapper MUST refuse to dispatch when `GEMINI_API_KEY` is absent.
- **Tool-approval default is `deny` in headless mode.** Good for review (no writes), but means the restrictive policy file is belt-and-braces, not the only enforcement.

### Injection-defense posture

Per the project memory `feedback_injection_defense_at_wrapper`: untrusted content (anything not authored by the orchestrator or the user) is sanitized in Python before any privileged LLM sees it. For the review wrapper this means:

- Plan text, task block, and schedule JSON are **trusted** (orchestrator-authored) and embedded verbatim in the prompt.
- `git diff -- <files>` content is **trusted** (locally generated) and embedded verbatim.
- Gemini's own tool reads via the `read_file` / `codebase_investigator` tools are constrained by the restrictive policy (`run_shell_command`, all edit tools, all browser tools `deny`-listed) AND post-hoc by the schema-validated envelope. Untrusted content does not reach Gemini through any orchestrator-controlled channel.

This is the same posture as `plan_codex_dispatch.py`. The Gemini wrapper imports the protected-paths constants from `_plan_paths.py` (single shared source) for delta-bounded cleanup of any post-dispatch working-tree noise.

## Verification

End-to-end acceptance criteria for the whole plan:

- `plan_gemini_dispatch.py review --plan-file <p> --task-id NNN --repo-root <r> --files <f> --timeout 180` emits a JSON envelope with the same outer shape as the Codex `review` envelope (`task_id, subcommand, outcome, codex_exit_code` → renamed `gemini_exit_code`, `parsed`), with `parsed` conforming to `gemini_review_schema.json` (a structural mirror of `codex_review_schema.json`).
- `plan_gemini_dispatch.py plan-review --schedule-file <s> --repo-root <r> --timeout 180` emits a JSON envelope with `parsed` conforming to `gemini_plan_review_schema.json` (mirror of `codex_plan_review_schema.json`).
- `plan_ops.py preflight --json` emits a new top-level field `gemini_available: bool` (sibling to `codex_available`) computed via `shutil.which("gemini") is not None` AND `GEMINI_API_KEY` (or `GOOGLE_APPLICATION_CREDENTIALS`) presence.
- `/implement-plan --allow-gemini-fallback <plan>` opt-in flag is parsed and forwarded.
- When the orchestrator is invoked with `--allow-gemini-fallback` AND preflight reports `codex_available=false` AND `gemini_available=true`, Phase 1.5 dispatches `plan_gemini_dispatch.py plan-review` instead of logging `plan_review_skipped {reason:"codex_unavailable"}`.
- When Codex `plan-review` returns `outcome ∈ {timeout, parse_error, failure}` AND `--allow-gemini-fallback` is set, the orchestrator re-dispatches the same review to Gemini ONCE before degrading to a summary warning. A new run-log event `plan_review_fallback_used {from:"codex", to:"gemini", reason:"<r>"}` records the swap.
- Same fallback semantics for Phase D.1 cross-review of Claude-implemented work.
- The schema audit fixture from `CODEX_FRICTION_2026-04-25/TASK-001` is extended to walk `gemini_*_schema.json` under the same `additionalProperties: false ⟹ required == properties.keys()` invariant.
- A pre-flight verification matrix (TASK-001 of this plan) validates that the installed Gemini CLI behaves as the wrapper assumes — empty-prompt exit 42, isolation via `GEMINI_CLI_HOME`, deny-policy enforcement, JSON envelope shape — BEFORE any wrapper code lands. The matrix is committed as a runnable script + report so future regressions are detectable.
- All new tests pass: `venv/bin/pytest -q tests/scripts/test_plan_gemini_dispatch_*.py tests/scripts/test_plan_ops.py tests/scripts/test_plan_codex_dispatch_schema.py`.

## Decomposition

The work is decomposed into 8 tasks under `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/` (see `00_INDEX.json` for the executable roster). Summary:

| Task | Title | Tier | Depends on |
|---|---|---|---|
| 001 | Empirical Gemini-CLI verification matrix (precondition) | codex | — |
| 002 | `gemini_*_schema.json` sidecars (review + plan-review) | codex | 001 |
| 003 | `plan_gemini_dispatch.py` skeleton + `review` subcommand | claude | 002 |
| 004 | `plan_gemini_dispatch.py plan-review` subcommand | codex | 003 |
| 005 | Preflight `gemini_available` field + `--allow-gemini-fallback` flag plumbing | codex | 001 |
| 006 | Phase 1.5 fallback wiring (orchestrator + SKILL.md / dispatch-templates.md) | claude | 004, 005 |
| 007 | Phase D.1 fallback wiring (orchestrator + SKILL.md / dispatch-templates.md) | claude | 003, 005, 006 |
| 008 | Schema audit extension (cover `gemini_*_schema.json`) | codex | 002 |

Children are individually self-contained; each carries its own `## Goal`, `## Context`, `## Verification`, `## Tasks` block per the executor's per-child `schema-valid` requirement.
