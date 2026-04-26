# Plan: Phase D State Machine + Injection Hardening

**Created:** 2026-04-20
**Status:** draft — Codex review 2026-04-20 (`needs-replan`, important finding on TASK-003 sanitizer contract addressed below)
**Base branch:** main
**Related:**
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `plugins/plan-executor/scripts/plan_ops.py`
- `plugins/plan-executor/scripts/plan_codex_dispatch.py`
- `docs/analysis/Gemini_Implement_Plan_SKILL_Analysis_20260418.md` (external critique seeding this plan)
- `docs/plans/PLAN_PHASE_D_STATE_MACHINE_2026-04-18.md` (earlier draft; this directory supersedes it)

---

## Goal

Move Phase D routing and orchestrator-tracked state out of the `implement-plan` SKILL prompt into `plan_ops.py`. Harden the Codex wrapper against prompt injection at the envelope boundary instead of asking the orchestrator LLM to triage untrusted content. Trim the SKILL to match the reduced orchestrator role and codify the three duties the LLM retains. No behavioral regressions on happy path or documented failure paths.

## Context

The SKILL (~500 lines) asks the orchestrator to (a) maintain state (`ready`, `done`, `failed`, `locked_files`, `committed`, `review_notes`, retry budgets) across long turns, and (b) navigate nested routing: D.2 → D.2a → D.5 → {D.2a.5, D.2a.6} → pause. LLMs drift under this load. The routing is deterministic given structured inputs — it belongs in Python.

The injection issue is narrower but load-bearing: the SKILL implicitly relies on the orchestrator to notice injections in subagent outputs. That puts untrusted payload in the highest-privilege context. Correct layering: (1) Codex wrapper enforces strict JSON envelope perimeter; (2) free-text fields are length-capped and format-stripped; (3) known injection shapes are **redacted** from free-text and flagged into `extra.sanitizer_flags[]` (pre-redaction payload logged to run-log for audit, never re-emitted to the orchestrator); (4) an optional unprivileged sanitizer subagent emits verdict-only without exposing the payload.

Post-work orchestrator responsibilities collapse to three: cross-task pattern-noticing, narrative synthesis, and pause-escalation on `unknown_state`.

## 3. Design

### 3.1 `plan_ops.py review-route` (new subcommand)

Stateless pure function. Input: parsed review envelope + per-task retry state + flags. Output: one directive the orchestrator executes.

**Input (stdin JSON):**
```json
{
  "task_id": "NNN",
  "implementer": "claude|codex",
  "reviewer_envelope": { "verdict": "...", "findings": [...], "summary": "..." },
  "d5_envelope": null | { "verdict": "ship|ship-with-fixes|partial-agreement|needs-rework",
                          "load_bearing": [0,2], "dismissed": [1,3], "summary": "..." },
  "retries_used": { "bounded_remediation": false, "narrow_remediation": false,
                    "role_swap": false, "codex_fallback": false },
  "flags": { "codex_review_binding": false, "skip_cross_review": false }
}
```

**Output (stdout JSON):**
```json
{
  "action": "commit | fail | dispatch_d5 | dispatch_bounded_remediation
           | dispatch_narrow_remediation | dispatch_role_swap
           | pause_awaiting_user | unknown_state",
  "args": {
    "commit_flags":     { "disagreement_tag": true, "remediation_tag": false,
                          "narrow_remediation_tag": false, "dismissed_finding_ids": [] },
    "fail_stage":       "implement|review|commit",
    "fail_reason":      "…",
    "pause_payload":    { "stage": "post_remediation_review|…", ... },
    "dispatch_context": { "template": "PhaseB-rework|PhaseB-narrow-remediation",
                          "findings_for_retry": [...], "dismissed_for_context": [...],
                          "d5_summary": "…" }
  }
}
```

Every current D.2 / D.2a / D.2a.5 / D.2a.6 row becomes a Python decision-tree line. `unknown_state` is the escape hatch — on that output the orchestrator pauses and returns to the user rather than guessing.

### 3.2 Persistent orchestrator state in `.schedule.json`

Extend the schedule schema with an optional `state` block:
```json
"state": {
  "done": ["001","002"], "failed": ["004"], "blocked": ["005"],
  "committed": [ {"task_id":"001","sha":"abc…"} ],
  "locked_files": [], "review_notes": { "002": [...] },
  "retries_used": { "003": { "bounded_remediation": true } }
}
```

`batch-next --from-schedule-state` reads `done`/`failed`/`locked_files`/`blocked` from the schedule. `commit-task`, `fail-task`, `block-dependents` each gain `--update-schedule-state <path>` to write transitions atomically via the existing atomic-write path. Old schedules without `state` continue to work; existing CLI args remain for back-compat.

### 3.3 Envelope sanitization perimeter (wrapper-side)

Three layers inside `plan_codex_dispatch.py`:

1. **Strict outer boundary.** Bytes outside the JSON envelope on Codex stdout are dropped; counter lands in `extra.dropped_bytes`.
2. **Content sanitization.** For `findings[].message`, `summary`, `diff_summary`, any freeform string: length-cap (default 8000 chars, marker + `extra.truncated_fields[]` on truncation), strip code fences, ATX headers, leading `>` blockquotes. Do NOT regex out general imperatives — real findings legitimately say "fix X".
3. **Known-shape redaction and flagging.** Scan `findings[].message`, `summary`, `diff_summary` for `<system>|<user>|<assistant>` tags, `<tool_calls>` / `<function_calls>` blocks, JSON-looking `"tool":"…"` payloads, literal `Ignore (previous|prior|above) instructions`. Each match is replaced in-place with a fixed marker `[redacted:<shape>]` and recorded in `extra.sanitizer_flags[]` as `{shape, field, count}`. The pre-redaction payload is emitted once to the run-log as a `sanitizer_redaction{shape, field, sha256}` event (payload never re-enters the envelope the orchestrator reads). Flags are surfaced in run summary but not routed on.

### 3.4 Content-sanitizer subagent (optional)

Gated by `--content-sanitizer-check`. Agent manifest `content-sanitizer.md` with `tools: ` (empty), cheap model tier, returns `{safe, category, summary}`. Wrapper pipes in suspect free-text only when `sanitizer_flags` is non-empty; verdict lands at `extra.content_sanitizer_verdict`. The orchestrator sees the verdict, never the payload. Dispatch failures degrade to `{status:"error", reason:"…"}` and routing continues.

### 3.5 SKILL.md diet

Target ≥30% character reduction. Changes:
- Collapse D.2 / D.2a / D.2a.5 / D.2a.6 tables into one paragraph describing the `review-route` call-and-comply loop + the `unknown_state` pause rule.
- Remove the "Cleanup policy (wrapper-enforced)" section entirely.
- Condense the `plan_ops.py` CLI reference to one line per subcommand; footer points at `--help` for flags. Drift guard (TASK-006) keeps this honest.
- Add an "Orchestrator LLM responsibilities" subsection enumerating the three preserved duties plus a counter-example list (orchestrator must NOT track `locked_files`, compose commit flags, recover from malformed envelopes, or evaluate untrusted text for intent).
- Remove the state-dict pseudocode at Phase D entry; replace with a one-liner referencing `batch-next --from-schedule-state`.

### 3.6 Preserved LLM-only responsibilities

Codified in SKILL and enforced by the absence of a Python path for them:

1. **Cross-task pattern-noticing at batch-join.** Python's `reconcile-batch` catches hard violations; the LLM flags soft patterns in the batch summary.
2. **Narrative synthesis at run-end / on pause.** Python supplies counts, findings, dirty-files, SHAs; the LLM composes the user-facing summary.
3. **Pause-escalation on `unknown_state`.** Orchestrator halts, emits `awaiting_user` with the envelope, returns control. No guessing.

## 4. Non-goals

- Full MCP tool-server migration of `plan_ops.py` subcommands (follow-up; orthogonal to this plan's correctness work).
- Changes to Phase 1.5 plan-review flow.
- `plan-implementer` / `plan-remediator` system-prompt changes beyond TASK-007's dispatch-templates.
- Run-resumption across crashes (schedule-state enables it but end-to-end resume is out of scope).
- Removing the `plan_ops.py` CLI surface. Bash dispatch stays until a future MCP migration.

## Verification

- Per-task unit tests cover new subcommands and module boundaries.
- TASK-006 drift guard fails CI if `plan_ops.py` argparse and SKILL.md's CLI table diverge.
- TASK-008 end-to-end smoke covers: clean commit, minor-findings commit, D.5 ship, bounded-remediation success, bounded second-failure pause, narrow success, narrow scope-violation pause, `unknown_state` pause, sanitizer-flag surfacing.
- Manual acceptance: one real plan through `--dry-run` and for-real; run-log deltas match the pre-change baseline plus new `review_route_called{action}`, `sanitizer_flag{shape}`, and `sanitizer_redaction{shape, field, sha256}` events only.

## 6. Execution — parallel batches

Eight tasks, five batches. TASKs 001 and 002 both edit `plan_ops.py` so they cannot batch together; likewise 003 and 004 both edit `plan_codex_dispatch.py`. Batching below obeys the no-shared-file-lock rule and is validated by `plan_ops.py parse-schedule`.

- **Batch 1 (parallel):** TASK-001, TASK-003 — independent; different files.
- **Batch 2 (parallel):** TASK-002 (no deps), TASK-004 (needs 003).
- **Batch 3:** TASK-005 (needs 001+002+003) — alone; SKILL.md edit.
- **Batch 4 (parallel):** TASK-006 (needs 005), TASK-007 (needs 001+005).
- **Batch 5:** TASK-008 (needs 001+002+003+005+007).

Single-session parallel execution within each batch is the target.

---

## Tasks

### TASK-001: Add `plan_ops.py review-route` subcommand

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - plugins/plan-executor/scripts/review_route_input_schema.json (create)
  - plugins/plan-executor/scripts/review_route_output_schema.json (create)
  - tests/scripts/test_plan_ops_review_route.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_review_route.py`
- **Acceptance criteria:**
  - `plan_ops.py review-route --stdin --json` consumes the §3.1 input and emits the §3.1 output.
  - Every current SKILL Phase D cell is reachable through one `action` value: `commit`, `fail`, `dispatch_d5`, `dispatch_bounded_remediation`, `dispatch_narrow_remediation`, `dispatch_role_swap`, `pause_awaiting_user`, `unknown_state`.
  - `args.commit_flags` composition respects `commit-task`'s existing XOR constraints (narrow-remediation-tag XOR remediation-tag; dismissed-finding-ids requires narrow-remediation-tag).
  - `flags.codex_review_binding=true` + Codex `needs-rework` on Claude work → `action: fail`, no `dispatch_d5`.
  - `retries_used.bounded_remediation=true` + Codex `needs-rework` → `pause_awaiting_user` with `pause_payload.stage="post_remediation_review"`.
  - Input schema violation → non-zero exit + structured `errors[*]`.
  - Any unrecognized input (e.g., verdict outside the enum) → `action: unknown_state` + human-readable `reason`.
  - Routing exposed as pure function `route(payload: dict) -> dict`; `cmd_review_route` is a thin stdin/`_emit` shim. Tests call `route()` directly — no subprocess, no stdin monkey-patching. Preserves the option of an in-process MCP wrap later.
  - Tests cover every routing cell plus ≥1 `unknown_state` branch.

**Description:** The core routing move. After this lands the orchestrator stops reading D.2 / D.2a / D.2a.5 / D.2a.6 tables and starts piping envelopes to Python. TASK-005's SKILL edits are only safe after this ships.

**Reversion guidance:** Delete the subcommand handler, the two schema files, and the test. Other `plan_ops.py` subcommands untouched.

---

### TASK-002: Persist orchestrator state in `.schedule.json`

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_schedule_state.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_schedule_state.py`
- **Acceptance criteria:**
  - Schedule schema adds an optional `state` object per §3.2; `write-schedule` accepts it and `parse-schedule` round-trips it.
  - `batch-next --from-schedule-state` reads `done`/`failed`/`locked_files`/`blocked` from the schedule; existing CLI-arg contract unchanged.
  - `commit-task --update-schedule-state <path>` atomically appends to `state.committed`, promotes the task into `state.done`, releases its `state.locked_files` entries.
  - `fail-task --update-schedule-state <path>` appends to `state.failed` and persists `state.retries_used[task_id]`.
  - `block-dependents --update-schedule-state <path>` populates `state.blocked`.
  - Old schedules without `state` load without error; state-write on an old schedule no-ops and warns.
  - Concurrent writers don't interleave (reuse the existing atomic-write path).
  - State helpers are pure functions: `read_schedule_state(path) -> dict`, `apply_commit_state_transition(state, task_id, sha, files) -> dict`, `apply_fail_state_transition(state, task_id, retries) -> dict`, `apply_blocked_state_transition(state, blocked_ids) -> dict`. The `--update-schedule-state` / `--from-schedule-state` flags are thin file-IO shims. Tests call the pure functions against in-memory dicts — no subprocess, no tempfile for routing logic.

**Description:** Moves the orchestrator's memory to disk. Required so `review-route` can read `retries_used` from the schedule rather than orchestrator context.

**Reversion guidance:** Revert the schema change, drop the two flags, remove the pure helpers. The run-log remains the audit source of truth.

---

### TASK-003: Wrapper envelope sanitizer perimeter

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_codex_dispatch.py (modify)
  - plugins/plan-executor/scripts/_codex_envelope_sanitizer.py (create)
  - tests/scripts/test_codex_envelope_sanitizer.py (create)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_codex_envelope_sanitizer.py`
- **Acceptance criteria:**
  - `_codex_envelope_sanitizer.sanitize(envelope: dict) -> (sanitized: dict, flags: list[str])` implements §3.3 layers 2 and 3.
  - Length cap (default 8000 chars per free-text field); truncation appends `" …[truncated]"` and increments `extra.truncated_fields[]`.
  - Code fences (triple backticks), ATX headers (`^#{1,6}\s`), leading `>` blockquotes stripped from `findings[].message`, `summary`, `diff_summary`.
  - Known-shape matches (`<system>|<user>|<assistant>` tags, `<tool_calls>` / `<function_calls>` blocks, JSON-looking `"tool":"…"` payloads, literal `Ignore (previous|prior|above) instructions`) are **replaced in-place** with the marker `[redacted:<shape>]` in the returned `sanitized` dict, and each match appends `{shape, field, count}` to `sanitizer_flags[]`.
  - For each redacted match, `sanitize()` emits one run-log event `sanitizer_redaction{shape, field, sha256}` capturing the sha256 of the pre-redaction payload. The raw payload is never returned in `sanitized` and never re-emitted downstream.
  - Bytes outside the envelope on Codex stdout are dropped with a counter in `extra.dropped_bytes`; never reach stdout.
  - `plan_codex_dispatch.py implement|review|plan-review` all route through `sanitize()` before emit.
  - Tests include a malicious fixture envelope (injected `<tool_calls>` + fake `<system>` instructions); sanitized output contains `[redacted:<shape>]` markers in place of the original shapes, `sanitizer_flags[]` lists each shape/field/count, and the pre-redaction sha256 appears in the captured run-log stream.

**Description:** Closes the injection gap the architectural review identified. Moves defense to the wrapper instead of trusting the orchestrator to self-police.

**Reversion guidance:** Remove the sanitizer module + its test; strip `sanitize()` call sites. Envelopes emit unfiltered as before.

---

### TASK-004: Content-sanitizer subagent (optional intent check)

- **Status:** pending
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/agents/content-sanitizer.md (create)
  - plugins/plan-executor/scripts/plan_codex_dispatch.py (modify)
  - tests/scripts/test_content_sanitizer_integration.py (create)
- **Dependencies:** TASK-003
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_content_sanitizer_integration.py`
- **Acceptance criteria:**
  - `content-sanitizer.md` declares `tools: ` (empty), cheap model tier, system prompt instructing `{clean|suspicious|malicious}` classification returning `{safe: bool, category: str, summary: str}` only.
  - `plan_codex_dispatch.py` gains `--content-sanitizer-check`; when set and `extra.sanitizer_flags` is non-empty the wrapper dispatches the sanitizer, gets a verdict, stamps `extra.content_sanitizer_verdict`.
  - The orchestrator is never passed the raw suspect text — verdict + category only.
  - Dispatch failures degrade: envelope still returns with `extra.content_sanitizer_verdict = {status:"error", reason:"…"}`; routing continues.
  - Integration test uses a stubbed sanitizer (not a real Claude call) and exercises `clean|suspicious|malicious|error` paths.

**Description:** Architecturally motivated stretch goal. Provides LLM-based intent classification on untrusted text without exposing it to the orchestrator. Safe to defer — TASK-003 already closes the main gap.

**Reversion guidance:** Delete the agent manifest + integration test; drop `--content-sanitizer-check` from argparse.

---

### TASK-005: SKILL.md rewrite — Phase D collapse, CLI diet, LLM roles

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (modify)
- **Dependencies:** TASK-001, TASK-002, TASK-003
- **Test command:** deferred (TASK-006)
- **Acceptance criteria:**
  - D.2, D.2a, D.2a.5, D.2a.6 tables replaced by one paragraph describing the `review-route` call-and-comply loop, with the `unknown_state` pause rule named explicitly.
  - The "Cleanup policy (wrapper-enforced, for reference)" section is removed entirely.
  - `plan_ops.py` CLI reference condensed to one line per subcommand (purpose only, no flags); footer points at `--help`.
  - New "Orchestrator LLM responsibilities" subsection enumerates the three preserved duties from §3.6 plus a counter-example list of things the orchestrator must NOT do.
  - Phase D state-dict pseudocode (`ready`, `done`, `failed`, `locked_files`, `committed`, `review_notes`) removed; replaced by a one-liner referencing `batch-next --from-schedule-state`.
  - Net reduction ≥30% characters (`wc -c` before/after).
  - Hard rules in the "Rules" section are preserved verbatim or strengthened; no behavior silently dropped.

**Description:** The context-window payoff. After 001/002/003 ship this removes the now-redundant orchestrator-side state-machine documentation.

**Reversion guidance:** Restore SKILL.md from the prior commit on the branch. No code to roll back.

---

### TASK-006: Drift guard — SKILL ↔ argparse parity test

- **Status:** pending
- **Priority:** medium
- **Files:**
  - tests/scripts/test_skill_cli_reference_drift.py (create)
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py`
- **Acceptance criteria:**
  - Test parses `plan_ops.py`'s argparse subcommand list (via introspection of `subparsers.add_parser(...)` or `--help` parse).
  - Test parses the condensed CLI reference table in SKILL.md.
  - Fails if an argparse subcommand is missing from the SKILL table.
  - Fails if the SKILL table lists a subcommand argparse does not register.
  - Explicitly excludes internal/hidden subcommands (define a `_cmd_internal_*` prefix convention if needed; none currently).

**Description:** Keeps the two documentation surfaces honest. Every future `plan_ops.py` subcommand addition mechanically requires a SKILL update.

**Reversion guidance:** Delete the test file.

---

### TASK-007: Dispatch-templates alignment

- **Status:** pending
- **Priority:** medium
- **Files:**
  - plugins/plan-executor/skills/implement-plan/dispatch-templates.md (modify)
- **Dependencies:** TASK-001, TASK-005
- **Test command:** deferred (TASK-008)
- **Acceptance criteria:**
  - PhaseB-rework and PhaseB-narrow-remediation templates accept the `dispatch_context` shape `review-route` emits (`findings_for_retry`, `dismissed_for_context`, `d5_summary`).
  - Template text references data received from `review-route`, not inline orchestrator composition.
  - Phase B default template unchanged (does not depend on Phase D routing).
  - Phase A analyst template unchanged.

**Description:** Mechanical alignment. Templates stay in the SKILL prompt library; only the expected variable names change.

**Reversion guidance:** Restore dispatch-templates.md from the prior commit.

---

### TASK-008: End-to-end smoke — full A→E loop with fakes

- **Status:** pending
- **Priority:** high
- **Files:**
  - tests/scripts/test_phase_d_e2e.py (create)
  - tests/scripts/fixtures/phase_d_plan.md (create)
- **Dependencies:** TASK-001, TASK-002, TASK-003, TASK-005, TASK-007
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_phase_d_e2e.py`
- **Acceptance criteria:**
  - Drives `plan_ops.py preflight → parse-schedule → write-schedule → batch-next → review-route → commit-task | fail-task` against a one-task fixture plan.
  - Subagent outputs injected via stub — no real Codex or Claude spawn.
  - Matrix covers: clean verdict → commit; minor-findings → commit with notes; Codex `needs-rework` + D.5 `ship` → commit with disagreement-tag; Codex `needs-rework` + D.5 `partial-agreement` → narrow remediation; narrow success → commit with narrow-remediation-tag + dismissed-finding-ids; narrow second failure → `pause_awaiting_user` correct payload; D.5 `needs-rework` + retry success → commit with remediation-tag; D.5 `needs-rework` + retry failure → `pause_awaiting_user`; envelope with sanitizer flags → commit proceeds, flags surface in run-log.
  - Run-log event order verified: `run_start`, `batch_start`, `implement_done`, `review_done`, `review_route_called`, one of `{commit_done, failed, awaiting_user}`, `batch_done`, `run_end`.
  - Runs in under 30s (no real subprocess spawn).

**Description:** The safety net. Locks in the orchestrator ↔ `review-route` contract so future SKILL edits cannot silently regress routing.

**Reversion guidance:** Delete the test file + fixture.

---
