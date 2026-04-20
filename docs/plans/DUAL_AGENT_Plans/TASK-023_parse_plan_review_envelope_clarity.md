# TASK-023 — Sharpen `parse-plan-review-report` envelope guidance (docs + error message)

**Base branch:** `main`
**Audit anchor commit:** `5036837` (TASK-022 commit — the run during which the gap surfaced)
**Chunk dependencies:** TASK-013 (introduced the plan-review gate), TASK-022 (added `--findings-json` and the D.5 dismissal-evidence gate; this task is a usability follow-up, not a functional extension).
**Motivating run:** `20260420T010733` — the orchestrator (Claude) piped only the inner `parsed` object into `parse-plan-review-report --stdin`, tripping `invalid-subcommand`. The skill docs use a `<envelope>` placeholder and the inline output-description phrase "validates the envelope against `codex_plan_review_schema.json`" — but that schema describes the *inner* payload, so "envelope" and "schema-conforming payload" read as interchangeable. Mistake was caught and corrected on the retry; no state impact.

---

## Goal

Close a narrow usability gap between the `parse-plan-review-report` contract and the skill instructions that describe how to call it. Two surface edits, no contract change:

1. **Skill docs:** replace the opaque `<envelope>` placeholder in SKILL.md §Phase 1.5 with a literal shape example so the expected wrapper structure (`{task_id, subcommand, outcome, codex_exit_code, parsed: {...}}`) is un-guessable.
2. **Error message:** when `parse-plan-review-report` rejects an input for `invalid-subcommand`, add a one-line hint distinguishing "missing subcommand" from "wrong subcommand". The most likely cause of `subcommand=None` is the caller piping only the inner `parsed` body — the message should say so.

The contract shape is unchanged: `parse-plan-review-report` continues to strictly require the full wrapper envelope. No auto-detection, no shape-lenient fallback.

---

## Scoped Context

### Why not auto-detect the inner payload

A tempting fix is to make `parse-plan-review-report` accept either the full envelope or the inner `parsed` body and unwrap on the fly. Rejected because:

1. **Inner-payload-only inputs lose envelope metadata** — `outcome`, `codex_exit_code`, `error` live at the envelope level. Auto-detection would silently accept a shape that skips the terminal-outcome shortcut (`failure | timeout | parse_error | scope_violation`), so a caller who piped `parsed` alone after a wrapper timeout would get a bogus "verdict" out of what should be an error path.
2. **Shape leniency hides real bugs** — the `invalid-subcommand` that tripped the TASK-022 orchestrator was exactly the kind of mistake the validator should catch loudly. Papering over it with auto-unwrap defeats the gate.
3. **Doc + message fix is cheaper and complete** — a literal example in the skill docs and a pointed error string together make the correct shape obvious at both read-time and run-time.

### What changed in the motivating run

Orchestrator called:

```bash
printf '%s' '{"plan_file":"...","verdict":"approved-with-notes","findings":[...], ...}' | \
  venv/bin/python "${CLAUDE_PLUGIN_ROOT}/scripts/plan_ops.py" parse-plan-review-report --stdin --json
```

Got:

```json
{"errors": [{"path": "$.subcommand", "code": "invalid-subcommand", "message": "envelope subcommand must be 'plan-review', got None"}]}
```

Two observations:

- The message reads as "subcommand exists but is wrong", when the actual cause was "subcommand field absent because the caller piped the inner body".
- SKILL.md §Phase 1.5 (line ~180) shows `printf '%s' "<envelope>"` — placeholder, not a literal shape. The output-description sentence ("validates the envelope against `codex_plan_review_schema.json`") reinforced the wrong mental model because that schema describes the *inner* payload.

### Files this task edits

- `plugins/plan-executor/scripts/plan_ops.py` — `cmd_parse_plan_review_report` error branch around the `invalid-subcommand` check (current anchor line 2511-2519). Message is lengthened to distinguish None vs other-value cases and name the likely caller mistake.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — §Phase 1.5 around line 180. The `printf '%s' "<envelope>"` line is preceded by a literal JSON shape example showing the required wrapper fields.
- `tests/scripts/test_plan_ops.py` — two new tests covering the improved error string.

### Non-goals

- **Auto-detect / shape-lenient acceptance.** Rejected per the reasoning above.
- **Changing the error code `invalid-subcommand`.** The code stays; only the human-readable message changes. Callers that pattern-match on `errors[*].code` are unaffected.
- **Changing `codex_plan_review_schema.json` or the envelope shape.** Neither is broken.
- **Updating dispatch-templates.md Phase 1.5 block.** That file already shows the envelope structure in prose (lines 32-49 of dispatch-templates.md); the usability gap is in SKILL.md's bash example.

---

## Verification

**V1 — SKILL.md §Phase 1.5 shows the literal envelope shape.**

```bash
grep -n '"task_id"' plugins/plan-executor/skills/implement-plan/SKILL.md
grep -n '"subcommand": "plan-review"' plugins/plan-executor/skills/implement-plan/SKILL.md
grep -n '"parsed"' plugins/plan-executor/skills/implement-plan/SKILL.md
```

All three greps must hit at least once inside the §Phase 1.5 block (line range 157-207 at commit `5036837`; adjust anchor window if surrounding text moves).

**V2 — `invalid-subcommand` error message names the "inner-payload piped alone" failure mode when subcommand is absent.**

```python
def test_parse_plan_review_report_invalid_subcommand_hints_inner_payload(tmp_path):
    # invoke parse-plan-review-report --stdin --json on an inner-payload-shaped input
    # (no subcommand key, but has verdict + findings + plan_file at top level)
    # expect exit 1; stderr errors[0].code == "invalid-subcommand"
    # AND stderr errors[0].message contains substring "parsed" AND "envelope"
    # (one possible phrasing: "envelope subcommand missing; did you pipe only the `parsed` object?")
```

**V3 — `invalid-subcommand` error message distinguishes wrong-value from absent-field.**

```python
def test_parse_plan_review_report_invalid_subcommand_wrong_value(tmp_path):
    # invoke parse-plan-review-report --stdin --json on an envelope with subcommand="implement"
    # expect exit 1; stderr errors[0].code == "invalid-subcommand"
    # AND stderr errors[0].message contains "'implement'" (the actual wrong value)
    # AND stderr errors[0].message does NOT contain the inner-payload hint substring
    # (the hint is reserved for the None case to avoid noise on genuine wrong-value mistakes)
```

**V4 — no change in error code surface.**

```python
def test_parse_plan_review_report_invalid_subcommand_code_unchanged():
    # both V2 and V3 inputs emit errors[0].code == "invalid-subcommand"
    # (guards against accidental code churn that would break callers)
```

---

## Tasks

### TASK-023: Sharpen `parse-plan-review-report` envelope guidance

- **Status:** done
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-013, TASK-022
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1-V4 pass.
  - The `invalid-subcommand` check in `cmd_parse_plan_review_report` (plan_ops.py around line 2511) branches on `subcommand is None` vs any other non-matching value:
    - When `subcommand is None`: the message includes both the literal phrase "envelope subcommand missing" (or equivalent — the V2 test anchors on "parsed" AND "envelope" substrings) AND a hint naming the likely cause, e.g. *"did you pipe only the `parsed` object?"*. No backtick-quoting style rules — match surrounding code.
    - When `subcommand` is any other value (non-None, non-"plan-review"): the message matches the pre-TASK-023 pattern closely — "envelope subcommand must be 'plan-review', got `<value>`" — with the actual value quoted. No inner-payload hint in this branch (the hint is only useful when the field is missing entirely).
  - The error code `invalid-subcommand` is preserved in both branches.
  - SKILL.md §Phase 1.5 contains a literal envelope JSON example next to (or immediately before) the `printf '%s' "<envelope>" | ... parse-plan-review-report --stdin --json` line. The example shows at minimum: `task_id`, `subcommand: "plan-review"`, `outcome`, `codex_exit_code`, and a nested `parsed` object. Fields that are not load-bearing for the example (e.g., `error: null`, `wall_seconds`, `scope`) may be omitted. Keep the example under ~10 lines to avoid bloating the section.
- **Out of scope:**
  - Auto-detection / shape-lenient acceptance of inner-payload inputs.
  - Renaming or removing the `invalid-subcommand` error code.
  - Editing `dispatch-templates.md` Phase 1.5 prose.
  - Adding analogous hints to other subcommand parsers (`parse-implementer-report`, etc.) — if the pattern generalizes, a follow-up task can sweep them together.
  - Changing the `codex_plan_review_schema.json` file or its inner-payload contract.

**Description:**
Make the `parse-plan-review-report` "full wrapper envelope required" contract visible from both ends: show the literal shape in the skill docs (so reading tells you the answer) and sharpen the error message when subcommand is absent (so running tells you the answer). No contract change — the parser remains strict.

**Implementation notes:**

- The `invalid-subcommand` branch is at `plan_ops.py:2511-2519` on commit `5036837`. Current code:

  ```python
  subcommand = envelope.get("subcommand")
  if subcommand != "plan-review":
      _die(args, {"errors": [{
          "path": "$.subcommand",
          "code": "invalid-subcommand",
          "message": (
              f"envelope subcommand must be 'plan-review', got "
              f"{subcommand!r}"
          ),
      }]})
  ```

  Proposed replacement (tune phrasing to match file style):

  ```python
  subcommand = envelope.get("subcommand")
  if subcommand != "plan-review":
      if subcommand is None:
          msg = (
              "envelope subcommand missing — did you pipe only the inner "
              "`parsed` object? Expected full wrapper with top-level "
              "`task_id`, `subcommand`, `outcome`, and `parsed`."
          )
      else:
          msg = f"envelope subcommand must be 'plan-review', got {subcommand!r}"
      _die(args, {"errors": [{
          "path": "$.subcommand",
          "code": "invalid-subcommand",
          "message": msg,
      }]})
  ```

- SKILL.md §Phase 1.5 insertion point is the `printf '%s' "<envelope>"` line at roughly line 180 on commit `5036837`. Example block to add immediately before it:

  ```markdown
  The full wrapper envelope (produced by `plan_codex_dispatch.py plan-review`) has shape:

  ```json
  {
    "task_id": "plan",
    "subcommand": "plan-review",
    "outcome": "success",
    "codex_exit_code": 0,
    "parsed": { "plan_file": "...", "verdict": "...", "findings": [...], "schedule_ok": true, "summary": "..." }
  }
  ```

  Pipe the entire envelope (not just `parsed`) into `parse-plan-review-report`.
  ```

- V1 is grep-anchored on the new example block. V2/V3/V4 are new pytest functions in `tests/scripts/test_plan_ops.py` — place them adjacent to the existing `test_parse_plan_review_report_*` suite if one exists; otherwise create the suite. Run the full `test_plan_ops.py` file to confirm no regressions in adjacent TASK-013/TASK-022 coverage.

**Reversion guidance:**

- Error-message change is additive — reverting restores the terser pre-TASK-023 string without affecting the error code or the contract. Callers that pattern-match on `errors[0].code` are unaffected; callers that pattern-match on message text should not exist (undocumented surface).
- SKILL.md doc change is additive prose. Safe to revert; usability regresses but function is unchanged.
- **Never revert the contract tightness.** Adopting auto-detection later would require a separate design discussion (see "Non-goals" above).

---

## Implementation Playbook

### Step 1 — Branch the `invalid-subcommand` message on `subcommand is None`

Edit `plan_ops.py:2511-2519` per the proposed replacement in Implementation notes. Preserve the `code: "invalid-subcommand"` identifier and the `path: "$.subcommand"` structure.

### Step 2 — Add the literal envelope example to SKILL.md §Phase 1.5

Insert the JSON example block immediately before the `printf '%s' "<envelope>" | ...` line. Keep total added length under ~15 lines (including backtick fences and the one-line explanatory prose) so the section stays scannable.

### Step 3 — Tests V2-V4 + V1 grep anchor

Add `test_parse_plan_review_report_invalid_subcommand_hints_inner_payload`, `test_parse_plan_review_report_invalid_subcommand_wrong_value`, and `test_parse_plan_review_report_invalid_subcommand_code_unchanged` in `tests/scripts/test_plan_ops.py`. If V1 is covered by an existing SKILL.md-content test family (there's a `TestTask022DismissalEvidenceGateDocs`-style pattern from TASK-022), add a parallel test class or function; otherwise inline a pytest function that does three greps on the SKILL.md file.

### Step 4 — Regression sweep

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

**Accepted result:** the full-file run is expected to finish with exactly **one** failing test, `test_analyst_to_parse_schedule_roundtrip` (a pre-existing failure owned by TASK-019, unrelated to this task's surfaces). All other tests — including V1-V4 newly added here and the existing TASK-013/TASK-021/TASK-022 coverage — must pass.

**How to report:** the implementer must capture the pytest summary line (e.g. `1 failed, N passed`), verify that the single failure is exactly `test_analyst_to_parse_schedule_roundtrip`, and record that verification in the task's run notes. Any additional failure — or any change in the failing test's name or file — is a regression and must block acceptance.

If the implementer prefers a clean-green signal instead, they may additionally run the scoped command below and attach its output; it is informational, not a substitute for the full-file run above:

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py \
  -k "parse_plan_review_report or Task023 or invalid_subcommand"
```

Expect this scoped run to finish with zero failures.

---

## Out of Scope

- **Auto-detect / shape-lenient acceptance.** Argued against in Scoped Context.
- **Error-code rename.** `invalid-subcommand` stays.
- **Sibling parser sweeps.** `parse-implementer-report`, `parse-analyst-report`, etc. are out of scope until the pattern is observed to recur.
- **dispatch-templates.md edits.** Already carries the envelope shape in prose.

## Reversion guidance

See per-step notes in Implementation notes. All three edits are additive and backward-compatible; reverting restores pre-TASK-023 behavior without schema or contract impact.

## Execution log — 20260420T204152 (success)

Starting SHA: `c1875fe8878a0e0f37cd1914a55e80fc2ecb558e`  → Ending SHA: `8e19cb3b40f6b63cccc448d8f007d5705357409a`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 023 | claude | codex | clean | 8e19cb3 | Test outcome: pre-existing-failure (accepted: only test_analyst_to_parse_schedule_roundtrip failed, owned by TASK-019). V1-V4 all pass. |
