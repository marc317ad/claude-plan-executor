# TASK-028 — Unwrap `claude --output-format json` envelope in live-CLI roundtrip tests

**Created:** 2026-04-20
**Status:** pending
**Base branch:** `main`
**Audit anchor commit:** `a65350e` (TASK-006 — the run during which the gap was re-confirmed as V9's failure mode)
**Chunk dependencies:** none (formerly TASK-002 — now archived/complete). No code-under-test changes — test-only fix.
**Motivating run:** `20260420T235312` and follow-up `20260421T002455` (TASK-006 execution). After TASK-006 landed the canonical `sample_phase4.md` fixture, V9 of TASK-006 — a clean full-file run of `test_plan_ops.py` — left exactly **one** residual failing test, `test_analyst_to_parse_schedule_roundtrip`. The failure is pre-existing, first flagged as out-of-scope by TASK-019 (line 394 of that plan: *"may continue to fail if the environment's `claude` binary returns diagnostic JSON; that is out of scope"*), and treated as an accepted residual by TASK-023's Regression sweep section. This chunk closes that residual.

---

## Goal

Fix two pre-existing live-CLI integration tests in `tests/scripts/test_plan_ops.py` so they pass against the current `claude` CLI (v2.1.118) when it is available:

1. `test_analyst_to_parse_schedule_roundtrip` (line 4346) — dispatches the `plan-analyst` subagent, extracts the schedule JSON from its output, and pipes that JSON into `plan_ops.py parse-schedule --stdin`.
2. `test_implementer_to_parse_implementer_report_roundtrip` (line 4392) — dispatches the `plan-implementer` subagent and pipes the raw output into `plan_ops.py parse-implementer-report --stdin`.

Both tests call `claude ... --output-format json ...`, which wraps the subagent's real output inside a top-level CLI envelope of roughly shape:

```json
{
  "type": "result",
  "subtype": "success",
  "session_id": "...",
  "result": "<the subagent's actual stdout as a single string>",
  "total_cost_usd": 0.0,
  "usage": { ... },
  "modelUsage": { ... },
  "uuid": "...",
  "permission_denials": [],
  "terminal_reason": "..."
}
```

The analyst test then calls `_extract_json_block(result.stdout)` (line 4373). That helper (line 4171) searches for a fenced ` ```json ` block first, then falls back to the first balanced `{...}` block. When `--output-format json` is on, the *entire* stdout IS the envelope object at the top level — so the balanced-braces fallback returns the envelope verbatim, with no `task_id` / `tasks` / schedule shape. `parse-schedule --stdin` then rejects it as schema-invalid.

The implementer test has the same `--output-format json` pattern (line 4409) and pipes `result.stdout` straight into `parse-implementer-report --stdin` (line 4427) — which expects the subagent's markdown report, not an envelope JSON object.

**Fix approach (decided in-plan, not left to the implementer):** keep `--output-format json` for both tests (it is deterministic and machine-readable — dropping it would mean parsing the CLI's streaming TTY-friendly default format, which is fragile). Unwrap `body["result"]` (the string field containing the subagent's real stdout) in both tests before handing it to `_extract_json_block` or to `parse-implementer-report`.

No change to `plan_ops.py`, no change to any subagent, no change to any non-test plumbing.

---

## Scoped Context

### Why unwrap instead of dropping `--output-format json`

Two alternatives were considered:

**(A) Drop `--output-format json` and parse the default streaming output.** Rejected because the default output is pretty-printed, interleaves tool-use traces with the final message, and is explicitly not a stable machine-readable surface. A future CLI upgrade could trivially break text parsing. Keeping `--output-format json` preserves the one stable contract we have.

**(B) Keep `--output-format json`, unwrap `body["result"]` before content extraction. CHOSEN.** The CLI envelope is documented and stable; `result` is a string field by contract. Unwrapping is two lines, localized to each test, and has no surface impact anywhere else. The existing `_extract_json_block` helper continues to do the right thing once it is fed the inner payload instead of the outer envelope.

### Why not generalize `_extract_json_block` to recognize the envelope

Tempting, but rejected:

1. `_extract_json_block` has exactly two callers today — the two tests this chunk fixes. Baking envelope-awareness into a helper named "extract JSON block" overloads its contract.
2. The envelope comes from a specific CLI flag (`--output-format json`). Making the generic helper detect a CLI-specific shape is a layering violation.
3. If future tests re-use the pattern, the right move is a new helper, e.g. `_unwrap_cli_envelope(stdout: str) -> str`, adjacent to `_extract_json_block`. For two callsites, inline unwrap is cleaner.

### Expected CLI envelope shape

Empirically observed in the motivating run. The envelope's `result` field is a string (not a nested JSON object) that contains whatever the subagent's final assistant turn would have printed to stdout in non-JSON mode. For the analyst, that includes a fenced ` ```json ... ``` ` block. For the implementer, that is the markdown task report. The envelope may contain other fields (`api_error_status`, `fast_mode_state`, etc.) that vary across CLI builds — tests must not pattern-match on those.

### Files this task edits

- `tests/scripts/test_plan_ops.py` — two test bodies (around lines 4346 and 4392). No new top-level helpers unless the implementer finds the duplication egregious; inline unwrap is acceptable.

### Non-goals

- **Any change to `plan_ops.py`.** The parsers' contracts are fine; the bug is in how the tests feed them input.
- **Any change to `_extract_json_block`.** See rationale above.
- **Any change to subagent behavior.** `plan-analyst` and `plan-implementer` emit the correct content; it was just being dropped on the floor by the outer envelope.
- **Removing the `@pytest.mark.slow` gating or the `_claude_cli_available()` skip.** Both remain — tests still skip in environments without the CLI, so CI on machines without `claude` installed keeps passing. This chunk just makes them *succeed* when the CLI IS available.
- **Backfilling a third-opinion / retry story around the wrapper dispatch.** If the CLI fails for environmental reasons (timeout, permission denial), the existing `pytest.skip(...)` path is preserved. This chunk does not change failure semantics — only success semantics.

---

## Verification

**V1 — `_extract_json_block` regression coverage: feeding it the CLI envelope must not silently return the envelope.**

Add a unit-scope test (no live CLI) that pre-builds a string matching the envelope shape and asserts that the new unwrap path recovers the inner fenced JSON, not the envelope object.

```python
def test_cli_envelope_unwrap_round_trips_analyst_fenced_json():
    # Build an envelope whose `result` string contains a fenced ```json block.
    inner_schedule = {"task_id": "001", "tasks": [{"task_id": "001", "route": "codex"}]}
    fenced = "```json\n" + json.dumps(inner_schedule) + "\n```"
    envelope = {
        "type": "result",
        "subtype": "success",
        "session_id": "abc",
        "result": "Some preamble.\n\n" + fenced + "\n\nTrailing notes.",
        "total_cost_usd": 0.0,
    }
    stdout = json.dumps(envelope)

    # Simulate the test's unwrap-then-extract chain. The helper used by the
    # two roundtrip tests (inline unwrap OR a new helper — whichever the
    # implementer chose) must recover `inner_schedule` exactly.
    recovered = _unwrap_and_extract(stdout)  # or inline equivalent
    assert recovered == inner_schedule
```

The implementer MAY implement `_unwrap_and_extract` as a small private helper alongside `_extract_json_block`, OR inline the unwrap in each test and name this V1 test against the inline logic (e.g., by factoring the unwrap into a tiny local function the test can import). Either shape is acceptable as long as V1 exists and guards the envelope case.

**V2 — `test_analyst_to_parse_schedule_roundtrip` passes against the real CLI.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::test_analyst_to_parse_schedule_roundtrip
```

Expected: exit 0. Either the test passes (CLI available, subagent dispatch succeeds, schedule parses clean with `errors == []`) or it *skips* for one of the existing gates (`_claude_cli_available() is False`, dispatch timeout, non-zero rc from the CLI). A silent skip in a CLI-less CI environment is the correct outcome for this test there; the change being verified is that in an environment where the CLI IS present AND the subagent DOES dispatch successfully, the roundtrip now succeeds instead of failing on the `assert cp.returncode == 0` line (line 4387).

**V3 — `test_implementer_to_parse_implementer_report_roundtrip` passes against the real CLI.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py::test_implementer_to_parse_implementer_report_roundtrip
```

Same semantics as V2: pass-or-skip, never fail.

**V4 — full-file regression: one less failure.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

Expected (in an environment with the `claude` CLI installed): the pytest summary line shows **zero** failures. The previous accepted-residual single-failure contract from TASK-023 (*"full-file run is expected to finish with exactly one failing test, `test_analyst_to_parse_schedule_roundtrip`"*) should no longer hold — callers of the post-TASK-028 expectation can drop that clause.

Expected (in an environment without the CLI): the summary shows the two roundtrip tests skipped, everything else passing. This is pre-TASK-028 behavior in CLI-less environments and must be preserved.

The implementer's report must include the pytest summary line.

**V5 — no unintended changes to `plan_ops.py` or `_extract_json_block`.**

```bash
git diff --stat HEAD~1 HEAD  # or equivalent against the task's starting SHA
```

Expected: only `tests/scripts/test_plan_ops.py` appears in the diff. No `plan_ops.py` lines touched. No `_extract_json_block` body change (additions adjacent to it are fine, e.g., a new `_unwrap_cli_envelope` helper).

---

## Tasks

### TASK-028: Unwrap `claude --output-format json` envelope in two live-CLI roundtrip tests

- **Status:** pending
- **Priority:** low
- **Files:**
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1-V5 pass.
  - `test_analyst_to_parse_schedule_roundtrip` (around line 4346) unwraps the `claude --output-format json` envelope before feeding the subagent output to `_extract_json_block`. The unwrap logic must:
    - Parse `result.stdout` as JSON; if that parse fails, fall through to the pre-TASK-028 `_extract_json_block(result.stdout)` path (a defensive no-regression path for CLI builds that don't wrap).
    - If the parse succeeds and the parsed object has a string `result` field, pass that string to `_extract_json_block` instead of `result.stdout`.
    - If the parse succeeds but there is no string `result` field (unexpected envelope shape), treat it as a skip-worthy environment issue and call `pytest.skip(...)` with a message naming the observed shape. Do NOT assert-fail — the test must be robust against CLI changes.
  - `test_implementer_to_parse_implementer_report_roundtrip` (around line 4392) applies the analogous unwrap before piping to `parse-implementer-report --stdin` (line 4427). The input to `subprocess.run([... "parse-implementer-report", "--stdin", ...], input=<unwrapped>, ...)` must be the inner `result` string, not the outer envelope JSON.
  - The `--output-format json` flag is retained in both `subprocess.run` argv lists. Do NOT drop it.
  - Existing skip paths (`_claude_cli_available() is False`, `FileNotFoundError`, `TimeoutExpired`, non-zero CLI rc) are preserved verbatim.
  - The `@pytest.mark.slow` decorator on both tests is preserved.
  - No change to `plan_ops.py`. No change to the body of `_extract_json_block`. A new small helper adjacent to `_extract_json_block` (e.g., `_unwrap_cli_envelope(stdout: str) -> str | None`) is permitted if the implementer prefers factoring over inline duplication.

- **Out of scope:**
  - Any edit to `plan_ops.py`, any subagent config file, or any non-test plumbing.
  - Rewriting `_extract_json_block` to be envelope-aware.
  - Dropping or changing the `--output-format json` flag.
  - Removing the `_claude_cli_available()` skip gate or the `@pytest.mark.slow` marker.
  - Landing a new CI job that requires the `claude` CLI (those tests remain gated-on-availability).
  - Renaming either test.
  - Backfilling V9 or any other TASK-006 verification criterion beyond the single failing-test fix.

**Description:**
Tests V2 and V3 are live-CLI integration checks that have been failing since they were ported from the algorithmic-trading-system repo (commit `f7aa069`). The root cause is mechanical: both tests pass `--output-format json` to the `claude` CLI, which wraps the subagent's real stdout (a fenced JSON block for the analyst, a markdown report for the implementer) inside a top-level envelope object with a `result` string field. The tests then hand `result.stdout` — the whole envelope — straight to downstream parsers, which correctly reject it. The fix is to parse the envelope, extract `result`, and hand *that* to the downstream parsers. Two lines per test, plus a unit-scope regression test (V1) that guards the unwrap logic against future CLI-envelope drift.

**Implementation notes:**

- **Unwrap shape:** the `claude --output-format json` envelope has no stable public schema guarantee beyond "there is a top-level `result` field of string type". Treat every other field as opaque. The unwrap should be tolerant:

  ```python
  def _unwrap_cli_envelope(stdout: str) -> str | None:
      """Return the inner `result` string from a claude --output-format json
      envelope, or None if stdout is not such an envelope."""
      try:
          body = json.loads(stdout)
      except json.JSONDecodeError:
          return None
      if not isinstance(body, dict):
          return None
      inner = body.get("result")
      if not isinstance(inner, str):
          return None
      return inner
  ```

  Place adjacent to `_extract_json_block` (around line 4171).

- **Analyst test patch (line 4373 region):**

  ```python
  inner = _unwrap_cli_envelope(result.stdout)
  source = inner if inner is not None else result.stdout
  analyst_json = _extract_json_block(source)
  if analyst_json is None:
      pytest.skip(
          f"no JSON block recovered from analyst output "
          f"(source[:400]={source[:400]!r})"
      )
  ```

  Note the `source` variable — the `pytest.skip` message anchors on *that* (whichever was actually searched), not on `result.stdout`, so the skip message stays informative.

- **Implementer test patch (line 4425 region):**

  ```python
  inner = _unwrap_cli_envelope(result.stdout)
  report_stdin = inner if inner is not None else result.stdout
  cp = subprocess.run(
      [str(PY), str(SCRIPT), "parse-implementer-report", "--stdin", "--json"],
      input=report_stdin,
      cwd=str(REPO_ROOT),
      capture_output=True,
      text=True,
  )
  ```

- **V1 placement:** adjacent to `_extract_json_block` and `_unwrap_cli_envelope`, before the live-CLI roundtrip tests. V1 does NOT require the `claude` CLI, so it MUST NOT be marked `@pytest.mark.slow` and MUST NOT be gated on `_claude_cli_available()`.

- **Envelope-shape oddities to skip (not fail):** if `_unwrap_cli_envelope` returns None but the downstream `_extract_json_block` on `result.stdout` also returns None (the existing fallback), the pre-TASK-028 `pytest.skip` still fires at line 4374. That behavior is preserved — no change needed beyond the `source` rename in the skip message.

**Reversion guidance:**

- All edits are localized to `tests/scripts/test_plan_ops.py`. Reverting (`git revert <commit>`) restores the pre-TASK-028 pass-with-one-accepted-failure behavior. No other test's behavior changes on revert.
- If `_unwrap_cli_envelope` is added as a module-level helper, the revert removes it cleanly; no downstream imports exist.
- Never revert in a way that silently switches either test back to feeding the envelope to downstream parsers without a compensating change — that would re-introduce the accepted-residual failure, which this chunk exists to eliminate.

---

## Implementation Playbook

### Step 1 — Add `_unwrap_cli_envelope` helper

Insert the helper body above `_extract_json_block` (around line 4170) or immediately below it. Either position is fine; keep them co-located so future maintainers see the pair together.

### Step 2 — Patch `test_analyst_to_parse_schedule_roundtrip`

Replace the single `analyst_json = _extract_json_block(result.stdout)` line at 4373 with the three-line unwrap-then-extract block shown in Implementation notes. Update the skip message to anchor on `source` instead of `result.stdout`.

### Step 3 — Patch `test_implementer_to_parse_implementer_report_roundtrip`

Add the two-line unwrap immediately above the `cp = subprocess.run(...parse-implementer-report...)` call at 4425. Change the `input=result.stdout` argument to `input=report_stdin`. Leave all other subprocess.run kwargs unchanged.

### Step 4 — Add V1 unit-scope regression test

Place `test_cli_envelope_unwrap_round_trips_analyst_fenced_json` in the same integration-tests section of the file, immediately before `test_analyst_to_parse_schedule_roundtrip`. No `@pytest.mark.slow`, no CLI gate, no `tmp_path` dependency.

### Step 5 — Regression sweep

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

**Accepted result (CLI available):** zero failures. The previous single-failure residual (`test_analyst_to_parse_schedule_roundtrip`) is now green, and its sibling roundtrip test passes as well.

**Accepted result (CLI unavailable):** zero failures, with both roundtrip tests reporting `skipped`. V1 must pass unconditionally — it is a pure-Python unit test and does not touch the CLI.

**How to report:** capture the pytest summary line (e.g. `N passed, M skipped`). If the environment has `claude` installed and any roundtrip test still fails, the failure is NOT an accepted residual and must block acceptance — re-diagnose.

### Step 6 — Out-of-date acceptance-residual language

Search for the literal substring `"test_analyst_to_parse_schedule_roundtrip"` across the plans and skill docs:

```bash
grep -rn 'test_analyst_to_parse_schedule_roundtrip' docs/plans plugins
```

For each hit that describes it as an accepted residual (TASK-023's Regression sweep section, TASK-019 line 394, TASK-006's V9 text), **do not edit those files in this chunk** — they are historical run records and should not be rewritten. The acceptance-residual language becomes stale after TASK-028 lands, but rewriting history is out of scope. A future plan that *refers forward* to residual-handling can cite this chunk as the resolution.

This step is verification-only — no edits expected.

---

## Out of Scope

- **`plan_ops.py` edits.** None needed; contracts are fine.
- **`_extract_json_block` rewrite.** Envelope-awareness belongs in a separate helper, not baked into the generic JSON-block extractor.
- **Dropping `--output-format json`.** See Scoped Context §(A) for the rejection rationale.
- **New CI job that runs live-CLI tests.** Gating-on-availability stays.
- **Rewriting historical plan language about the accepted residual.** Historical records stay intact.

## Reversion guidance

See per-step notes under Implementation notes. All edits are in `tests/scripts/test_plan_ops.py`; reverting is a single-file `git revert` with no downstream impact.
