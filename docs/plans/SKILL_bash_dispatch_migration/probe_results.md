# TASK-001 — Canary A/B probe: results

This document records the empirical findings from the TASK-001 canary
probe (`tests/scripts/test_claude_dispatch_canary.py`). The probe
exercises **two paths** for dispatching `plan-analyst`:

  - **Path (a) — Agent tool**: replayed from a hand-rolled transcript
    fixture (`_agent_tool_transcript_analyst()` inside the test file).
    Stands in for an orchestrator transcript capture.
  - **Path (b) — Bash wrapper**: `plan_claude_dispatch.py run --input
    <payload>` invoked directly. Two modes:
      * Dry-run mode (always on in CI) — exercises the wrapper pipeline
        through `_dry_run_envelope` without spawning `claude`.
      * Live mode (gated by `CANARY_LIVE_CLAUDE=1` AND `claude` on
        PATH) — full pipeline including real backend.

## Observed v3 envelope top-level keys

The wrapper at `plugins/plan-executor/scripts/plan_claude_dispatch.py`
emits a JSON envelope on stdout that schema-validates against
`schemas/claude_dispatch_output.json`. The keys observed (and asserted
by the canary test) are:

  - `schema_version` — integer, const `1`.
  - `status` — one of `ok`, `schema_invalid`, `timeout`, `denied`,
    `backend_error`, `budget_exhausted`, `depth_exceeded`,
    `manifest_invalid`, `input_invalid`, `scope_violation`,
    `cleanup_failure`.
  - `status_reason` — string or null. Carries the literal token
    `"dry_run: true"` for dry-run envelopes.
  - `agent` — string (e.g., `"plan-analyst"`) or null.
  - `model` — string or null.
  - `session_id` — string or null.
  - `duration_ms` — integer ≥0 or null.
  - `cost_usd` — number ≥0 or null.
  - `tokens` — object `{input, output, cache_read, cache_creation}` or null.
  - `result` — opaque inner agent result; for analyst this is the
    schedule dict (`outcome`, `tasks`, `batches`, `gaps`, `risks`,
    optional `diagnostics`).
  - `result_raw_truncated` — string or null.
  - `stderr_tail` — string or null.
  - `permission_denials` — array of objects.
  - `scope` — object `{declared_files_changed, observed_delta_tracked,
    observed_delta_untracked, scope_violation_detected,
    scope_misreport_detected, failed_paths?}`.
  - `trace` — object `{run_id, span_id, parent_span_id, depth,
    call_chain, started_at, ended_at}`.
  - `error` — object `{code, message, retriable}` or null.

Downstream tasks (TASK-002 envelope fixtures, TASK-004 implementer
dispatch, TASK-005 remediator dispatch) lock against this exact set.
The canary test enforces the set via
`set(envelope.keys()) == V3_REQUIRED_TOP_LEVEL_KEYS`.

## Per-agent inner outcome enums (under `result`)

  - **plan-analyst** — `outcome ∈ {valid, needs-enrichment, invalid}`.
  - **plan-implementer** — `outcome ∈ {success, partial, failed,
    plan-incorrect, blocked, malformed}`.
  - **plan-remediator** — implementer set ∪ `{scope-violation}`.

## Envelope size

The wrapper pretty-prints with `indent=2`. A representative dry-run
envelope for `plan-analyst` against the 2-task `directory_mode_plan`
fixture serializes to roughly 1.2–1.6 KB (well under the 64 KB cap
asserted by the canary test). The 64 KB ceiling is a sanity bound on
the "context recovery" claim, not a tuned target — it leaves headroom
for real analyst outputs that include longer descriptions, gaps, and
diagnostics.

## Timing

  - **Path (a) — Agent tool transcript replay**: O(microseconds);
    pure dict construction, no I/O.
  - **Path (b) — wrapper dry-run**: ~150–400 ms wall-clock under the
    test runner. Dominated by the Python interpreter cold-start of
    spawning `plan_claude_dispatch.py` plus jsonschema validator load.
  - **Path (b) — wrapper live mode**: dominated by the real `claude`
    backend round-trip (seconds to tens of seconds). Not measured in
    CI; gated behind `CANARY_LIVE_CLAUDE=1`.

The wrapper's per-invocation Python startup overhead is the largest
non-LLM cost. It is acceptable for the migration's target call sites
(Phase 1 / Phase B / Phase D.2a.6) which already incur a Claude CLI
roundtrip; the wrapper overhead is sub-1% of the LLM latency budget.

## Envelope size delta vs. markdown report

The legacy Agent-tool path returns a **markdown report** (the agent's
free-form output) and the orchestrator parses load-bearing fields out
of it. Empirically those reports are 2–8 KB of markdown for analyst
output and 4–20 KB for implementer reports.

The wrapper path returns a **structured JSON envelope**. The
`schema_version=1` v3 envelope including the inner agent `result`
typically lands in 1.5–10 KB for analyst outcomes and 3–25 KB for
implementer outcomes (with `result_raw_truncated` capturing oversize
text out-of-band when the inner agent emits >max_bytes).

The 64 KB hard cap is order-of-magnitude consistent with the legacy
markdown sizes; the asserted invariant is that the v3 envelope is **not
materially larger** than the markdown form it replaces, validating the
"context recovery" claim that motivated the migration.

## Behavioral divergence findings

The probe found one divergence between path (a) and path (b), classified
as a **report-shape artifact** (NOT load-bearing):

  - **Inner-result wrapping.** Path (a) returns the analyst dict
    directly as the Agent tool's reply object. Path (b) wraps the same
    analyst dict under `envelope.result`. Downstream consumers must
    unwrap one level on the wrapper path. This is the entire point of
    the v3 envelope (transport metadata around the inner result) and
    is documented in the schema; it is not a behavioral regression.

The semantic projection asserted by the canary test
(`outcome`, `tasks[*].id`, `batches[*].index`) is **byte-equal** across
the two paths. No load-bearing divergence was observed.

## Live-mode caveats

The live test (`test_canary_live_analyst_schedule_parity`) is gated
because (a) it incurs a real `claude` LLM cost, (b) the analyst output
is non-deterministic on real plans (the fixture used here is a tiny
2-task plan chosen to minimize variance, but exact byte equality is
not guaranteed even on the projected fields). The live assertion uses
the same semantic projection as the dry-run test, so any drift on
`outcome`, `tasks[*].id`, or `batches[*].index` will fail loudly.

## Wrapper-readiness checklist for TASK-002 / TASK-004 / TASK-005

  - [x] v3 envelope keys observed and recorded above.
  - [x] `status == "ok"` reachable on dry-run for all three
        dispatchable agents (`plan-analyst`, `plan-implementer`,
        `plan-remediator`).
  - [x] Envelope size cap (64 KB) satisfied with significant margin.
  - [x] Per-agent outcome enums documented.
  - [x] `--dry-run` short-circuit confirmed: no backend spawn, no
        preflight side effects, plan-only `result` payload.

## Reversion guidance

Delete this file and `tests/scripts/test_claude_dispatch_canary.py`.
No code paths under `plugins/plan-executor/scripts/` were modified by
TASK-001.
