# TASK-001 — Roster-aware closure helper in `plan_ops.py`

**Base branch:** `main`
**Chunk dependencies:** none

---

## Goal

Add a pure-roster helper that walks `00_INDEX.json` `chunks[].depends_on` (which is already structured JSON, fully trustworthy) and returns the transitive-prereq closure of a requested ID set, plus a CLI surface (`index-closure`) so the orchestrator can compute the closure before invoking `build-tasks`.

## Scoped Context

The malformed `**Dependencies:**` prose problem lives in the per-child markdown body, not in the roster's structured `chunks[].depends_on`. The closure must therefore key on the roster — never on the child body — so it survives malformed siblings entirely. This helper is the foundation TASK-002 builds on.

### Existing surfaces we touch

- `plugins/plan-executor/scripts/plan_ops.py` — add helpers `_load_index_chunks(plans_dir) -> list[dict]` and `_compute_index_closure(chunks, requested_ids) -> tuple[set[str], list[dict]]` near the existing roster helpers (`_parse_index_roster` is at `plan_ops.py` ~line 1900; place new helpers adjacent). Add subcommand `cmd_index_closure` next to `cmd_filter_schedule` (~line 4093). Wire argparse + main dispatch (~lines 8322-8917).
- `tests/scripts/test_plan_ops.py` — add a `TestIndexClosure` class near the existing `TestBuildTasks` class.

### Non-goals

- Reading any `*.md` child file. The helper is pure-roster.
- Inventing a new error vocabulary. Reuse existing error codes where they fit (`unknown-requested-id`, `closure-malformed-dep`, `duplicate-roster-id`).
- Wrapping the helper in `build-tasks` yet — that's TASK-002.

## Verification

**V1.** `_compute_index_closure(chunks, {"009"})` against the live `docs/plans/DUAL_AGENT_Plans/00_INDEX.json` returns `{"009", "002", "001"}` (transitive walk via the roster's `depends_on`), with no malformed-deps complaints (the roster-side deps are clean even though the child markdown is not).

**V2.** `_compute_index_closure(chunks, {"017"})` returns `{"017", "001"}` (TASK-017 → 001).

**V3.** Unknown id surfaces `{"code": "unknown-requested-id", "task_id": "999"}` as a non-fatal entry in the returned errors list, AND the closure for the rest of the requested set is still computed.

**V4.** Malformed `depends_on` value (non-string, or string that doesn't normalize) on a chunk **inside** the closure surfaces `{"code": "closure-malformed-dep", "task_id": <chunk_id>, "dep_id": <bad value>}`.

**V5.** Same malformed value on a chunk **outside** the closure is silent — the helper neither walks it nor surfaces an error. (This is the load-bearing asymmetry the whole plan turns on.)

**V6.** Duplicate `task_id` across two chunks surfaces `duplicate-roster-id`. The helper returns the first occurrence's closure and surfaces the duplicate as an error.

**V7.** New CLI `python plan_ops.py index-closure --plans-dir <dir> --task-ids 009,017 --json` emits `{closure: ["001","002","009","017"], skipped_chunk_count: <N>, errors: []}` (closure sorted lexicographically; `skipped_chunk_count` is `len(chunks) - len(closure)`).

---

## Tasks

### TASK-001: Roster-aware closure helper in `plan_ops.py`

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "index_closure or IndexClosure"`
- **Acceptance criteria:**
  - V1–V7 pass.
  - `_compute_index_closure` is pure-roster: never reads any `*.md` child.
  - Closure is computed via iterative BFS starting from `requested_ids`, walking `chunks[].depends_on` only via structured JSON. No regex parsing.
  - Each `requested_id` is normalized via `_normalize_task_id` before lookup (so callers can pass `"9"`, `"009"`, or `"TASK-009"` interchangeably).
  - Errors are returned in source order (chunk-declaration order in `00_INDEX.json`); within a chunk, errors are returned in `depends_on` index order.
  - New `cmd_index_closure` registered in `build_parser()` and the `main()` dispatch table.
  - `--json` is the only output mode (no human-readable mode); the orchestrator is the only consumer.
  - At least 8 unit tests in `TestIndexClosure` covering: happy path, transitive 4-deep, unknown id (single + mixed-with-known), malformed dep inside closure, malformed dep outside closure (silent), duplicate `task_id`, empty `requested_ids` (returns empty closure + empty errors), self-referential `depends_on` (cycle of length 1 — error).

**Description:**

The helper signature is `_compute_index_closure(chunks: list[dict], requested_ids: set[str]) -> tuple[set[str], list[dict]]`. It returns `(closure_ids, errors)` where `closure_ids` is the transitive-closure ID set (including the requested IDs themselves) and `errors` is a list of `{code, ...}` dicts. Closure walking uses a BFS frontier seeded with normalized requested IDs; for each id, look up the chunk by normalized `task_id` (build a `{normalized_id: chunk}` map once), enqueue its `depends_on` entries (after normalizing each), and continue until the frontier drains. Cycles are naturally handled by the visited-set guard.

Errors are surfaced when (a) a `requested_id` doesn't resolve to a chunk (`unknown-requested-id`), (b) a `depends_on` entry inside the closure doesn't normalize or doesn't resolve (`closure-malformed-dep`), or (c) the roster has duplicate `task_id` values (`duplicate-roster-id`). Errors outside the closure are NEVER surfaced — if a sibling chunk has malformed `depends_on` but isn't reached, the helper doesn't even look at it.

The CLI subcommand `index-closure` is a thin wrapper: parse `--plans-dir` + `--task-ids`, load chunks, call the helper, emit JSON. Exit code 0 if `errors == []`, exit code 1 otherwise. The JSON shape is `{closure: [<sorted ids>], skipped_chunk_count: <int>, errors: [...]}`.

**Implementation notes:**

- `_load_index_chunks(plans_dir)` should reuse the existing `_parse_index_roster` if practical; otherwise extract just the `chunks` array.
- Normalization: every id (requested + roster-side `task_id` + `depends_on` entries) goes through `_normalize_task_id`. The closure set is canonical-cased (`"009"`, not `"9"`).
- Sort closure on output; do NOT sort the chunks themselves (callers handle ordering).
- `skipped_chunk_count` is informational telemetry for the run-log; keep it on the CLI envelope.

**Reversion guidance:**

`git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`. The new helper is additive; no existing callers depend on it until TASK-002 lands.
