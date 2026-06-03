# Forensic: the `universe_snapshot` full-universe persistence was specified, marked done, and never built

- **Date:** 2026-06-02
- **Type:** Read-only forensic investigation (no code/DB/git mutated).
- **Status:** Findings for operator review. Feeds the persistence task group of
  `docs/plans/20260602_signal_ranker_persistence_and_integrity_code.md` (TASK-001).
- **Why this is its own document:** the *mechanism* of the loss (a P0 requirement
  evaporating between a parent plan and its auto-decomposed child, with every
  verification gate de-scoped in lockstep) is a process failure worth investigating
  independently of the code fix.

## BLUF

The system is supposed to write **one `trade_decision_features` row per universe symbol
per trading day** (`observation_kind='universe_snapshot'`), carrying that symbol's
full per-generator feature snapshot plus its ranker score/rank/components. This is what
makes "analyze every symbol every day → persist → rank → pass winners to strategy"
auditable and researchable.

**It has never written a single such row, in any run.** Across **411 backtest runs**,
`trade_decision_features` holds **45,884 rows, 100% `observation_kind='trade'`, zero
`universe_snapshot`.** The behavior was fully specified in an archived plan, the schema
and writer support it, but the actual per-symbol persistence loop was dropped when the
owning task was auto-decomposed, and the implementation, review, and tests all validated
against the de-scoped child rather than the parent's intent.

## 1. What was intended (archived Plan C — fully specified)

Source: `docs/plans/archive/20260522-signal-pipeline-consolidation-plan.md`.

- **Scope (line 6):** "run every generator on the full universe exactly once and
  **persist one snapshot row per symbol** to `trade_decision_features` via Plan A's
  observation path."
- **Design Decision 1 (line 71):** "The morning pass writes **exactly one row per
  universe symbol per trading day** … independent of whether any actionable signal was
  produced." It explicitly forbids reusing `evaluated_no_signal` for this purpose.
- **TASK-003 step (lines 188–190):** read the post-seat universe via
  `signal_engine.get_universe()`, then "**For each symbol in `universe_symbols`**, call
  `record_observation(observation_kind='universe_snapshot', symbol=symbol,
  features=…, ranker_score=…, ranker_rank=…, ranker_components=…,
  ranker_backend_version=…)`" sourced from `signal_engine.signal_ranked_details[symbol]`.
- **Acceptance/tests:** TASK-004 (line 229) required a test asserting a held non-top-K
  symbol "appears in the morning snapshot rows (`observation_kind='universe_snapshot'`)";
  TASK-005 (lines 252–260) required "**one `universe_snapshot` row per universe symbol
  per day**" and explicit **backtest-mode parity** plus a 500-symbol insert-latency test;
  Validation SQL (lines 276–322) counts `universe_snapshot` rows per day and expects
  ≈ universe size.

The authoritative schema doc encodes the same contract:
`docs/ref/DATABASE_SCHEMA.md` describes `universe_snapshot` as "written once per universe
symbol per trading day … capturing the per-symbol feature snapshot."

## 2. What actually shipped (live code)

The morning-pass *compute* exists; the *per-symbol persistence* does not.

- `src/core/trading_session.py:299` calls `self._run_morning_pass(period)`.
- `_run_morning_pass` (`:502`) runs `generate_all_signals(return_indicators=True)` over
  the full universe (`:535`) — so the work is done — then **loops only the actionable
  signals**: `for sig in morning_signals` (`:556`), building features and emitting a
  `SIGNAL_GENERATED` event per signal (`:563`). Its own docstring says "Emits one
  `SIGNAL_GENERATED` event **per morning-pass signal**" (`:505`) — per *signal*, not per
  *universe symbol*.
- Those `SIGNAL_GENERATED` events flow to `_process_signal`, whose only persistence is
  `capture()` in the execute/reject branches (`trading_session.py:1162/1190/1249/1274`),
  which hard-codes `observation_kind='trade'` (`src/analysis/decision_snapshot.py:457`).
- **There is no loop over `get_universe()` and no `record_observation(
  observation_kind='universe_snapshot', …)` call in `src/`.** Repo grep finds
  `universe_snapshot` only in `decision_snapshot.py` — the enum allow-list (`:61`) and a
  side-map (`:963`). It is a **defined-but-never-emitted** value; **no `_emit_universe_snapshot`
  method exists.**
- The `_indicators_to_features_dict` helper the task promised **does** exist (`:465`) and
  is used (`:562`) — but only for the signal rows, confirming the scaffolding was built
  while the actual deliverable was not.
- The one full-universe-ish observation path that *does* exist,
  `_emit_evaluated_no_signal_observations` (`:833`), is a **structural no-op**: it sources
  symbols from SignalEngine attributes (`_last_computed_feature_symbols`,
  `_last_computed_features`, …) that **the SignalEngine never populates** (grep: those
  names appear only as *reads* in `trading_session.py:745–768`), so it early-returns every
  cycle. It is also gated off by default (`OBSERVATION_LOG_ENABLED=false`,
  `settings.py:2333`; `emit_evaluated_no_signal=false`, `:2341`).
- The full ranked universe is computed in memory (`signal_engine.py:2300`
  `self.signal_ranked_universe = …`) but `src/universe/signal_ranker.py` contains **zero**
  persistence; the scored universe is never handed to the writer.

## 3. Database proof

```sql
SELECT observation_kind, COUNT(*), COUNT(DISTINCT run_id) AS runs
FROM trade_decision_features GROUP BY 1;
-- trade | 45884 | 411      (the only row; zero universe_snapshot, ever)
```

Consistent across the two Phase-2 gate runs as well: `e29829d9` (ranked) and `c1548f2e`
(random) each persisted only `observation_kind='trade'` rows (~4/day = traded
entry+exit snapshots + a few `rejected_gate`), never the ~115–199 universe rows/day the
design requires.

## 4. Root cause — a P0 requirement dropped in decomposition, with verification gates de-scoped in lockstep

The owning task was auto-decomposed, and the persistence requirement did not survive the
render into the child task file:

`docs/plans/archive/20260522-signal-pipeline-consolidation/TASK-003_tradingsession_morning_pass_and.md`:

- Marked **`Status: done`**, **Agent: claude**, commit **`9e1ee5fe`** (execution log line 40–44).
- Its **Description** is auto-fill boilerplate that *admits the loss*: "(Auto-filled by
  decompose-plan; **the source plan omitted a `**Description:**` body for TASK-003**…)".
  The parent in fact had a full TASK-003 body (parent lines 173–216) — the decompose
  carried a one-line goal, not the per-symbol `record_observation` step.
- Its **acceptance criteria** mention only "emits a morning-pass signal event,"
  "the `_indicators_to_features_dict` helper," the empty-top-K fallback, and the "toggle
  matrix." **`universe_snapshot` and "one row per universe symbol per day" are absent.**
- The **review** (execution log) shipped via **D.5 over a Codex `needs-rework`** that was
  about an unrelated *cache-reuse intraday-emission* claim — not the missing persistence.
- Commit **`9e1ee5fe`** is titled "TradingSession morning-pass **emission**, intraday-scope
  recompute, and cached-reuse" — the scope reflects the watered-down child, not the parent.

The verification gates that should have caught it were de-scoped the same way:

- **TASK-004's test exists but doesn't test the dropped requirement.**
  `tests/test_signal_pipeline_held_position.py` is present (13 KB) yet contains **zero**
  `universe_snapshot` references (repo grep). The parent required it to assert the held
  symbol "appears in the morning snapshot rows (`observation_kind='universe_snapshot'`)."
- **TASK-005's validation SQL was evidently never run** against a session — it would have
  returned 0 `universe_snapshot` rows immediately. No `universe_snapshot` row has ever
  existed.
- A later "GAP-01" patch (commit **`e72cec9c`**, "feed ranker score/components into
  `trade_decision_features.signal_ranker_*`") noticed ranker fields were missing and
  back-filled them onto the **existing `'trade'` rows** — a symptom fix that made the
  ranker columns look populated while the full-universe rows stayed absent, further
  masking the gap.

This is the same failure family as the recorded decompose child-render defects
(impl-notes dropped / fenced-block corruption on auto-decompose). Here the casualty was a
load-bearing P0 deliverable.

## 5. Impact

- **Research/audit blocked.** The "analyze every symbol every day" data the ranker is
  meant to be studied and tuned on does not exist in the DB. The full-universe
  feature→forward-return study that motivates ranker re-weighting had to be reconstructed
  from raw `intraday_bar_features` price bars instead of queried from persisted features.
- **The ranker has run unaudited.** Ranking happens in memory and the top-K narrowing
  reaches the strategy, but because the per-symbol analysis was never persisted, no one
  could see that the ranker scores carry no relationship to outcome (separate finding).
- **Backtest history is not backfillable for the missing rows** without re-running with
  the fix in place.

## 6. Recommended follow-up (process — beyond the code fix)

The code fix is TASK-001 of the companion CODE plan. Independently, investigate:

1. **Decompose fidelity:** why did `decompose-plan` render TASK-003's child without the
   per-symbol `record_observation` step and without a Description body, when the parent
   had both? Is this the known child-render drop defect, and which other "done" tasks in
   the `20260522` family (or elsewhere) lost requirements the same way?
2. **AC provenance:** the implementer and reviewer validated against child acceptance
   criteria derived from the lossy decompose. Acceptance criteria that are themselves a
   product of the decompose cannot catch a decompose drop — flag this circularity.
3. **False-done detection:** `9e1ee5fe` was marked done without its core deliverable.
   Is there (should there be) a gate that cross-checks a "done" task's acceptance against
   the parent plan's validation SQL / tests before close?
4. **Audit the sibling tasks** (`20260522-signal-observation-log` Plan A, `signal_ranker_v1`
   Plan B): confirm their delivered surface matches their parent specs, since Plan C
   consumed both.

## Provenance

Three read-only subagents (2026-06-02): plan-intent, writer-trace, git-history+wiring —
all converged. Cross-checked first-hand: `src/core/trading_session.py:299,465,502,556,563`;
`src/analysis/decision_snapshot.py:61,457,963`; `src/config/settings.py:2333,2341`;
the archived parent plan and TASK-003 child file; and the live DB
(`observation_kind` distribution across 411 runs).
