# TASK-022 — Persist full reviewer findings in run log + D.5 dismissal-evidence gate

**Base branch:** `main`
**Audit anchor commit:** `df60801` (TASK-019 commit that surfaced the audit gap)
**Chunk dependencies:** TASK-019 (introduced the `disposition` field on minor findings), TASK-021 (introduced the Codex evidence gate — D.5 gate here is the parallel pattern on the dismissal side).
**Motivating run:** `20260419T200147` — TASK-019's D.5 third-opinion reviewer dismissed 3 Codex findings on "plan says so" reasoning, and the full Codex finding payloads were never persisted past the wrapper's stdout.

---

## Goal

Close two related audit/quality gaps surfaced in run `20260419T200147`:

1. **Persistence.** Events `review_done`, `disagreement`, and `commit_done` in `_run_log.jsonl` record only finding counts. Extend each to optionally carry the full Codex findings payload (plus D.5 dispositions on `commit_done`) so audits weeks or months later can answer "what was flagged, and if it was dismissed, why?"
2. **D.5 depth.** Mirror TASK-021's Codex `needs-rework` evidence gate on the D.5 dismissal side: before labeling a Codex finding `dismissed`, D.5 MUST cite a concrete verification move (trace the cited code, run the test, reproduce the failure) OR label the dismissal as `spec-deference` — a new disposition value meaning "the critique has merit but contradicts the current spec; flagged for future plan-review rather than silently buried."

---

## Scoped Context

### Persistence gap — concrete evidence from run `20260419T200147`

Three events were written for task 019's review cycle:

- `review_done`: `{reviewer:"codex", verdict:"needs-rework", findings_count:3}` — counts only, no payload.
- `disagreement`: `{codex_findings_count:3}` — count only, no payload.
- `commit_done`: `{minor_findings_count:3, disagreement_tag:true, dismissed_finding_ids:[]}` — counts + tags, no finding text.

Codex emits findings with schema `{severity, file, line, issue, suggested_fix}` (per `codex_review_schema.json`). That payload survives ~20s in the wrapper's stdout, gets passed to `commit-task --reviewer-minor-findings`, which writes `len(minor)` to the log and drops the rest. Three months from now, an auditor asking "did Codex ever flag `plan_ops.py:686` on TASK-019?" has no retrieval path short of re-dispatching Codex against the same commit — expensive, non-deterministic, and it doesn't recover D.5's rationale.

### Depth gap — why D.5 over-dismisses

TASK-021 added an evidence gate for Codex's `needs-rework` verdict: Codex must cite a concrete observation (reproduced failure, traced path, cited diff invariant) before escalating. D.5 (the third-opinion reviewer dispatched on Codex `needs-rework` against Claude-implemented work) has NO equivalent gate on its *dismissal* path.

In practice, D.5 falls back to "plan says so" when a Codex finding contradicts an explicit spec line. That move is shallow because:

1. **The spec may be wrong or arbitrary.** TASK-019's `disposition-reason-without-disposition` error was a design choice, not a correctness requirement — Codex's objection that the two optional fields shouldn't be coupled has legitimate design merit.
2. **"Spec says so" skips Codex's verification premise.** Finding 2 in TASK-019 made a concrete claim about `cmd_parse_schedule`'s error-propagation path that D.5 never verified — D.5 just observed that the implementation matched the Playbook text.

The fix is prompt-side, mirroring the TASK-021 shape on the dismissal half: require D.5 to cite one of {traced code, ran the task's test command, reproduced the failure, or explicit-spec-contradiction-with-named-followup} before each dismissal. The new `spec-deference` disposition lets D.5 record "the critique has merit but contradicts the spec" so design-level concerns queue for future plan-review passes rather than vanishing.

### Files this task edits

- `plugins/plan-executor/scripts/plan_ops.py` — (1) `cmd_log_event` gains `--findings-json` argument, validated via existing `_validate_minor_findings_payload` and embedded verbatim under key `findings`; (2) `cmd_commit_task` includes the parsed `--reviewer-minor-findings` payload under key `findings` in the `commit_done` event, alongside the existing `minor_findings_count`; (3) `OPTIONAL_DISPOSITIONS` gains `"spec-deference"` as a fourth accepted value.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — §D.1 shows the orchestrator passes `--findings-json` to the `review_done` `log-event` call; §D.2a shows the same for the `disagreement` event.
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` — Phase D.5 gains a "Dismissal-evidence gate" paragraph mirroring the Codex evidence gate's structure (dispatch-templates.md:125). The gate names the new `spec-deference` disposition and the required verification moves.
- `tests/scripts/test_plan_ops.py` — new V1-V6 tests below.

---

## Verification

**V1 — `log-event` accepts `--findings-json` and embeds verbatim.**

```python
def test_log_event_findings_json_embedded(tmp_path):
    # invoke: plan_ops.py log-event --event review_done
    #         --fields-json '{"run_id":"X","task_id":"001","reviewer":"codex","verdict":"minor-findings","findings_count":1}'
    #         --findings-json '[{"severity":"minor","file":"a.py","line":1,"issue":"x","suggested_fix":"y"}]'
    # expect the JSONL tail line parses as a dict with key "findings" == the array, alongside the other fields
```

**V2 — `log-event --findings-json` rejects malformed findings.**

```python
def test_log_event_findings_json_rejects_malformed():
    # invoke log-event with --findings-json '[{"severity":"bogus","file":"a.py","line":1,"issue":"x","suggested_fix":"y"}]'
    # expect exit 1; stderr errors[*].code includes a known minor-findings validation code
```

**V3 — `commit_done` event embeds `findings` verbatim when `--reviewer-minor-findings` is non-empty.**

```python
def test_commit_done_event_embeds_findings(tmp_path):
    # seed a plan, stage one file, run commit-task with a 1-finding --reviewer-minor-findings payload
    # read the last _run_log.jsonl line; expect event=="commit_done" AND "findings" key equals the parsed 1-finding list
    # AND the existing "minor_findings_count" field still present and equals 1
```

**V4 — `spec-deference` is an accepted `disposition` value.**

```python
def test_reviewer_finding_disposition_accepts_spec_deference():
    finding = {
        "severity": "minor", "file": "a.py", "line": 1,
        "issue": "x", "suggested_fix": "y",
        "disposition": "spec-deference",
        "disposition_reason": "plan mandates this but critique has design merit",
    }
    # expect _validate_reviewer_finding_item returns []
```

**V5 — dispatch-templates.md Phase D.5 contains the dismissal-evidence gate + `spec-deference` wording.**

```bash
grep -n 'Dismissal-evidence gate' plugins/plan-executor/skills/implement-plan/dispatch-templates.md
grep -n 'spec-deference' plugins/plan-executor/skills/implement-plan/dispatch-templates.md
```

Both greps must hit.

**V6 — backward compat: `log-event` and `commit-task` without the new findings payloads behave unchanged.**

```python
def test_log_event_without_findings_json_unchanged(tmp_path):
    # existing log-event calls without --findings-json still succeed
    # JSONL line contains no "findings" key
def test_commit_done_without_minor_findings_is_empty_list(tmp_path):
    # commit-task with empty --reviewer-minor-findings '[]' still succeeds
    # commit_done event has "findings": [] EXPLICITLY (key always present)
    # minor_findings_count: 0
```

---

## Tasks

### TASK-022: Persist full reviewer findings in run log + D.5 dismissal-evidence gate

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-019, TASK-021
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1-V6 pass.
  - `plan_ops.py log-event` accepts `--findings-json <json>`. When present, the JSON is parsed and validated via `_validate_minor_findings_payload`; on validation failure, `_die` fires with the structured error list. On success, the parsed list is embedded verbatim under key `findings` in the JSONL line (alongside the existing `fields-json` keys). Absence is unchanged (no `findings` key in the line).
  - `plan_ops.py commit-task` includes the parsed `--reviewer-minor-findings` payload under key `findings` in the `commit_done` event, IN ADDITION TO the existing `minor_findings_count` integer. An empty `--reviewer-minor-findings '[]'` MUST yield `findings: []` explicitly — the key is always present on `commit_done` events. (Codex plan-review finding #1, approved.)
  - `OPTIONAL_DISPOSITIONS` accepts `"spec-deference"` alongside the existing `{"dismissed", "accepted", "deferred"}`. Validator rejections for invalid disposition values continue to use the `invalid-reviewer-finding-disposition` error code.
  - `dispatch-templates.md` Phase D.5 section contains a "Dismissal-evidence gate" paragraph parallel to the Codex evidence gate at dispatch-templates.md:125. The paragraph names: (a) the required verification moves (trace cited code, run the task's declared test command, reproduce the failure) and (b) the `spec-deference` disposition for critiques that have merit but contradict the current spec.
  - SKILL.md §D.1 shows the orchestrator passes `--findings-json` to the `review_done` `log-event` call; §D.2a shows the same for the `disagreement` event. A one-line note under each is sufficient — do not rewrite either section.
- **Out of scope:**
  - Retroactive backfill of prior `_run_log.jsonl` entries. The new fields are forward-only.
  - Auto-opening follow-up plan stubs from `spec-deference` dispositions — a future TASK-023-class concern.
  - Changes to Codex's wire schema (`codex_review_schema.json`). Reviewer output stays unchanged; only orchestrator-side persistence + D.5 prompt change.
  - A new `adjudication_done` event type. The existing `disagreement` event with the new `findings` field is sufficient for the current audit need.
  - Schema extension to D.5's own JSON output (e.g., a `findings_dispositions` array). The orchestrator continues to populate per-finding `disposition` manually based on D.5's verdict and summary text.

**Description:**
Extend three run-log events (`review_done`, `disagreement`, `commit_done`) to optionally persist the full Codex finding payload and D.5's per-finding dispositions. Mirror TASK-021's Codex `needs-rework` evidence gate on D.5's dismissal path so "plan says so" cannot silently bury a real design concern. Add a fourth disposition value `spec-deference` that distinguishes "the critique has merit but contradicts the spec — queue for follow-up" from "the finding is not real."

**Implementation notes:**

- `_validate_minor_findings_payload` (plan_ops.py around line 695) already validates the finding schema (severity/file/line/issue/suggested_fix + optional disposition/disposition_reason). Reuse it for `--findings-json`. Do NOT duplicate the validation logic.
- `OPTIONAL_DISPOSITIONS` lives near `_validate_reviewer_finding_item` (around line 504-553 after TASK-019's edits). Add `"spec-deference"` to the set literal. Also update the test `test_reviewer_minor_finding_disposition_accepted` (or the adjacent V6 test from TASK-019) so the test vector covers at least one `spec-deference` case.
- The `commit_done` event is emitted from `cmd_commit_task` around line 2800-2815. The parsed `minor` list already exists at line 2663. Add `"findings": minor` to the event dict, directly adjacent to the existing `"minor_findings_count": len(minor)`. Preserve the count field unchanged for backward-compat.
- `cmd_log_event` is `plan_ops.py`'s `log-event` handler. Add `--findings-json` as an argparse argument (string). Parse + validate via `_validate_minor_findings_payload`; on non-empty errors, `_die` before any write. On success, merge `{"findings": parsed_list}` into the event dict built from `--fields-json`. Key collision (`findings` already present in `--fields-json`) should error out — use a dedicated error code like `findings-json-collision`.
- Phase D.5 dismissal-evidence gate text (propose, tune to match surrounding style):

  > **Dismissal-evidence gate (required before labeling any finding `dismissed`).** The decision rubric above answers *what verdict fits the split*; the dismissal-evidence gate answers *do you know the finding is wrong*. Before labeling a Codex finding `dismissed`, you MUST cite one of:
  >
  > 1. **A concrete verification move:** trace the cited control-flow path by hand, run the task's declared test command (`pytest -k <name>`, etc.), read the cited symbol in the repo, or reproduce the failure Codex describes. If the verification refutes the finding, `dismissed` is appropriate.
  > 2. **An explicit spec contradiction:** the plan's acceptance criteria or Implementation Playbook mandates the behavior Codex objects to. In this case, label the disposition `spec-deference` (not `dismissed`) and include a one-line note on why the critique has merit despite the spec conflict. `spec-deference` surfaces the finding for a future plan-review pass rather than silently burying it.
  >
  > "Plan says so" without a `spec-deference` label is a protocol violation — it pretends the spec is unimpeachable. If you cannot verify the finding AND there is no explicit spec conflict, downgrade to a `minor-findings`-style note and let the commit proceed with the finding recorded rather than dismissed.

- SKILL.md §D.1 update: after the existing `review_done` `log-event` example, add a note: *"When findings are non-empty, also pass `--findings-json "$(<json-array>)"` so the review_done line carries the full payload. The audit trail depends on this."* Parallel note under §D.2a for the `disagreement` event (log before D.5 dispatch; include the Codex findings verbatim).

**Reversion guidance:**

- `--findings-json` on `log-event` is additive. Safe to revert; log lines lose the new field but existing consumers unaffected.
- `commit_done.findings` is additive. Safe to revert; the `minor_findings_count` fallback remains.
- `spec-deference` disposition value is additive. Safe to revert (existing three values remain valid); any `spec-deference`-labeled finding in a post-TASK-022 log becomes a validation error in the pre-TASK-022 validator, but those are historical records, not re-validated.
- The D.5 prompt text is additive. Safe to revert; D.5 falls back to its pre-TASK-022 behavior (which is what ran in TASK-019 — over-dismissal on "plan says so").
- **Never revert without replacement.** If the audit trail requirement moves (e.g., to an external database), keep at least the `--findings-json` seam in place — reverting both paths simultaneously re-creates the TASK-019-era blind spot.

---

## Implementation Playbook

### Step 1 — `cmd_log_event` gains `--findings-json`

In `plan_ops.py`'s `log-event` argparse subparser, add:

```python
log_event_parser.add_argument("--findings-json", dest="findings_json", default=None)
```

In `cmd_log_event`, after the existing `--fields-json` parse:

```python
findings: list | None = None
if args.findings_json is not None:
    try:
        parsed = json.loads(args.findings_json)
    except json.JSONDecodeError as e:
        _die(args, {"errors": [{"path":"$.findings_json","code":"invalid-json","message":str(e)}]})
    errs = _validate_minor_findings_payload(parsed, path="$.findings_json")
    if errs:
        _die(args, {"errors": errs})
    findings = parsed
    if "findings" in fields:
        _die(args, {"errors": [{"path":"$.findings","code":"findings-json-collision","message":"findings key is present in both --fields-json and --findings-json"}]})

event = {"ts": ..., "event": args.event, **fields}
if findings is not None:
    event["findings"] = findings
```

### Step 2 — `cmd_commit_task` embeds findings in `commit_done`

In `cmd_commit_task`, at the `commit_done` event assembly (around line 2800):

```python
commit_done_event = {
    ...
    "minor_findings_count": len(minor),
    "findings": minor,    # <-- new
    ...
}
```

### Step 3 — `OPTIONAL_DISPOSITIONS` extension

Add `"spec-deference"` to the set literal:

```python
OPTIONAL_DISPOSITIONS = {"dismissed", "accepted", "deferred", "spec-deference"}
```

Update V4 test coverage. Ensure the V7 test from TASK-019 (invalid-disposition-value rejected) still passes — it checks that `"bogus"` is rejected, which is unaffected by adding `spec-deference`.

### Step 4 — D.5 dismissal-evidence gate in dispatch-templates.md

Insert the gate paragraph (full text above in Implementation notes) directly after the D.5 **"Verdict decision rubric"** block and before the **"Hard rules for `partial-agreement`"** block in the `## Phase D.5 — code-reviewer third opinion (§8.4 escalation)` section. The V5 grep tests for `Dismissal-evidence gate` and `spec-deference` are the canonical anchors: both must hit after the edit. Preserve surrounding tone and formatting. (Codex plan-review finding #2, approved.)

### Step 5 — SKILL.md one-line notes

Under §D.1, after the `review_done` log-event example, add the `--findings-json` note. Under §D.2a step 1, add the same-shape note for the `disagreement` event. Keep both under two lines each.

### Step 6 — Tests

Add V1-V6 as individual test functions in `tests/scripts/test_plan_ops.py`. V5 is grep-based and belongs next to the V11 SKILL.md-content test from TASK-019.

### Step 7 — Regression sweep

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
```

Expect V1-V6 green; existing TASK-019 / TASK-021 tests unchanged. The pre-existing `test_analyst_to_parse_schedule_roundtrip` may continue to fail against the live `claude` binary — that is out of scope per TASK-019's Implementation Playbook Step 9.

---

## Out of Scope

- **Retroactive backfill of prior run logs.** Forward-only.
- **`adjudication_done` as a distinct event.** The existing `disagreement` event with the new `findings` field covers the audit need.
- **D.5 output schema extension** (e.g., a `findings_dispositions` array). Orchestrator continues to populate `disposition` on each finding based on D.5's verdict and summary text; a future task can wire per-finding D.5 output if needed.
- **Auto-opening plan stubs from `spec-deference`.** TASK-023-class follow-up.

## Reversion guidance

See per-step notes in Implementation notes. The `--findings-json` seam and the D.5 evidence gate are both additive and backward-compatible. The `spec-deference` disposition is additive; downgrading it from the validator's OPTIONAL set would make post-TASK-022 log entries fail re-validation, but those are historical artifacts, not live schema checks.
