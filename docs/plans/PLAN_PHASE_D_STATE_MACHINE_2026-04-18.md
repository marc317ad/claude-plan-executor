# Plan: Phase D State Machine + Injection Hardening

**Created:** 2026-04-18
**Status:** draft — pending review
**Base branch:** main
**Related:**
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `plugins/plan-executor/scripts/plan_ops.py`
- `plugins/plan-executor/scripts/plan_codex_dispatch.py`
- `docs/analysis/Gemini_Implement_Plan_SKILL_Analysis_20260418.md` — the external critique that seeded this plan

---

## 1. Goal

Shift Phase D routing and orchestrator-tracked state out of the `implement-plan` SKILL prompt and into `plan_ops.py`. Harden the Codex wrapper against prompt injection at the envelope boundary rather than relying on the orchestrator LLM to triage untrusted content. Trim the SKILL to match the reduced orchestrator role and codify the three responsibilities the orchestrator LLM must retain. No behavioral regressions on happy-path or documented failure paths.

## 2. Context — Why

The current SKILL (≈500 lines) asks the orchestrator LLM to (a) maintain state variables `ready`, `done`, `failed`, `locked_files`, `committed`, `review_notes`, `retry budgets` across long conversation turns, and (b) navigate a nested routing state machine: D.2 → D.2a → D.5 adjudication → {D.2a.5 bounded remediation, D.2a.6 narrow remediation} → awaiting-user pause. LLMs are probabilistic text generators and drift under this kind of imperative load. The routing is nearly deterministic once the inputs are structured, so it belongs in Python.

A second issue is narrower but more consequential: the SKILL implicitly relies on the orchestrator LLM to notice prompt-injection attempts in subagent outputs. This is backwards — it puts the untrusted payload into the highest-privilege context in the system. The correct layering is (1) the Codex wrapper enforces a strict JSON envelope perimeter, (2) free-text fields inside the envelope are length-capped and format-stripped, (3) known injection shapes are flagged into `extra.sanitizer_flags[]`, and (4) an optional unprivileged sanitizer subagent emits a verdict-only boolean without exposing the payload to the orchestrator.

After this work, the orchestrator LLM's responsibilities collapse to three: (a) cross-task pattern-noticing at batch-join, (b) narrative synthesis at run-end and on pause, (c) pause-escalation when `plan_ops.py review-route` returns `unknown_state`. Everything else — verdict tables, commit-flag composition, retry bookkeeping, file-lock tracking, dependent cascades — is pure state transition driven by structured input.

## 3. Design

### 3.1 `plan_ops.py review-route` (new subcommand)

Stateless routing function. Input: the parsed review envelope + per-task retry state + flag state. Output: a single directive the orchestrator executes.

**Input (stdin JSON):**

```json
{
  "task_id": "NNN",
  "implementer": "claude|codex",
  "reviewer_envelope": { "verdict": "...", "findings": [...], "summary": "..." },
  "d5_envelope": null | { "verdict": "ship|ship-with-fixes|partial-agreement|needs-rework",
                          "load_bearing": [0,2], "dismissed": [1,3], "summary": "..." },
  "retries_used": {
    "bounded_remediation": false,
    "narrow_remediation":  false,
    "role_swap":           false,
    "codex_fallback":      false
  },
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
                          "narrow_remediation_tag": false,
                          "dismissed_finding_ids": [] },
    "fail_stage":       "implement|review|commit",
    "fail_reason":      "…",
    "pause_payload":    { "stage": "post_remediation_review|…", ... },
    "dispatch_context": { "template": "PhaseB-rework|PhaseB-narrow-remediation",
                          "findings_for_retry": [...], "dismissed_for_context": [...],
                          "d5_summary": "…" }
  }
}
```

The orchestrator calls `review-route` once per review turn and complies. Every D.2 table row becomes a line in the Python decision tree. The `unknown_state` output is the escape hatch for inputs the router does not recognize — it returns control to the orchestrator LLM, which then pauses for user adjudication rather than guessing.

### 3.2 Persistent orchestrator state in `.schedule.json`

Extend the existing schedule schema with a `state` block:

```json
{
  "tasks":   [ ... ],
  "batches": [ ... ],
  "state": {
    "done":         ["001","002"],
    "failed":       ["004"],
    "blocked":      ["005"],
    "committed":    [ {"task_id":"001","sha":"abc…"} ],
    "locked_files": [],
    "review_notes": { "002": [ ... ] },
    "retries_used": { "003": { "bounded_remediation": true } }
  }
}
```

`batch-next` gains optional `--from-schedule-state` mode: when set, it reads `done`/`failed`/`locked_files` from the schedule instead of CLI args. `commit-task`, `fail-task`, and `block-dependents` gain `--update-schedule-state <path>` to write the corresponding transitions atomically. The existing CLI args remain for back-compat; old schedules without a `state` block continue to work.

### 3.3 Envelope sanitization perimeter (wrapper-side)

Three layers, all inside `plan_codex_dispatch.py`:

1. **Strict outer boundary.** Anything on Codex stdout outside the JSON envelope is dropped before the envelope is emitted. Currently the wrapper already expects an envelope; make this a hard contract — stray bytes go to `extra.dropped_bytes` counter, never to stdout.
2. **Content sanitization of free-text fields.** For `findings[].message`, `summary`, `diff_summary`, and any other freeform string: length-cap to N chars, strip code fences and markdown headers, normalize whitespace. Do not regex out general imperatives — real findings legitimately contain "fix X."
3. **Known-shape flagging.** Scan the same fields for injection shapes: `<system>` / `<user>` / `<assistant>` tags, `<tool_calls>` and `<function_calls>` blocks, JSON-looking tool-call payloads, `Ignore (previous|prior) instructions` literal. Hits land in `extra.sanitizer_flags[]` — the orchestrator surfaces these in the run summary but does not route on them.

### 3.4 Content-sanitizer subagent (optional)

Gated by `--content-sanitizer-check`. A new agent manifest `content-sanitizer.md` with **no tools** (pure text-in, JSON-out). The wrapper pipes suspect free-text fields in; the agent returns `{safe: bool, category: "clean|suspicious|malicious", summary: "…"}`. The orchestrator never sees the payload — only the verdict.

This is the "LLM-as-sanitizer" pattern done correctly: the privileged agent is not evaluating untrusted content; an unprivileged one is.

### 3.5 SKILL.md diet

Net ≈35% shorter. Changes:

- **Collapse Phase D routing.** D.2 / D.2a / D.2a.5 / D.2a.6 tables → one paragraph: "call `plan_ops.py review-route` with the envelope + retry state, comply with the returned `action`; on `unknown_state` pause for user adjudication."
- **Drop the Cleanup policy section** (wrapper-enforced; orchestrator has no decision to make about it).
- **Condense the CLI reference table** to a one-line-per-command form; drop flag-by-flag documentation (argparse `--help` is authoritative; see TASK-006 drift guard).
- **Add an "LLM-only responsibilities" subsection** naming the three remaining duties (pattern-noticing, narrative, unknown-state escalation) so future edits don't re-expand the orchestrator's role by accident.

### 3.6 Preserved LLM-only responsibilities

Codified explicitly in SKILL and enforced by absence of a Python routing path:

1. **Cross-task pattern-noticing at batch-join.** Python's `reconcile-batch` already halts on hard violations; the LLM notices soft patterns (e.g., "three tasks failed with similar symptoms → probable plan-level issue") and surfaces them in the batch summary.
2. **Narrative synthesis at run-end and on pause.** Python supplies counts, findings, dirty-files, SHAs; the LLM composes the user-facing summary and next-step recommendations.
3. **Pause-escalation on `unknown_state`.** When `review-route` returns `unknown_state`, the orchestrator halts, emits `awaiting_user` with the full envelope, and returns control. No guessing.

## 4. Non-goals

- Full MCP tool-server migration (wrapping `plan_ops.py` subcommands as structured JSON tools). Called out as a follow-up in §3.5; context-window savings of that move are significant but orthogonal to this plan's correctness work.
- Changes to Phase 1.5 plan-review flow.
- Changes to `plan-implementer` / `plan-remediator` agent system prompts beyond the dispatch-templates adjustments needed for TASK-007.
- Run-resumption across crashes (the schedule-state persistence enables it, but end-to-end resume is out of scope for this plan).
- Removing the CLI surface of `plan_ops.py`. The bash-invocation model stays until a future MCP migration.

## 5. Verification

- Unit tests per task cover the new subcommands and module boundaries.
- TASK-006 drift guard test fails the build if a `plan_ops.py` argparse subcommand is missing from SKILL.md's reference table, or vice-versa.
- TASK-008 end-to-end smoke exercises the full A→E loop with faked subagent outputs covering: clean commit, minor-findings commit, D.5 adjudication ship, bounded-remediation success, bounded-remediation second-needs-rework pause, narrow-remediation success, narrow-remediation scope-violation pause, unknown_state pause.
- Manual acceptance: run one real plan through the new flow with `--dry-run` and then for-real; compare run-log events and commit trailers against a pre-change baseline. No new event types should appear except `review_route_called {action}` and `sanitizer_flag {shape}`.

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
  - `plan_ops.py review-route --stdin --json` accepts the §3.1 input schema and emits the §3.1 output envelope.
  - Every current SKILL Phase D path is reachable through one `action` value: `commit`, `fail`, `dispatch_d5`, `dispatch_bounded_remediation`, `dispatch_narrow_remediation`, `dispatch_role_swap`, `pause_awaiting_user`, `unknown_state`.
  - Commit-flag composition inside `args.commit_flags` honors the existing argparse constraints of `commit-task` (narrow-remediation-tag XOR remediation-tag; dismissed-finding-ids requires narrow-remediation-tag; etc.).
  - `--codex-review-binding=true` + Codex `needs-rework` on Claude work → `action: fail` with no `dispatch_d5`.
  - `retries_used.bounded_remediation=true` + Codex `needs-rework` → `action: pause_awaiting_user` with `pause_payload.stage="post_remediation_review"`.
  - Input envelope schema-violation → exit non-zero with structured `errors[*]`.
  - Any input the router cannot classify (e.g., reviewer verdict not in the enum) → `action: unknown_state` with a human-readable `reason`.
  - Tests cover every routing cell in the §3.1 state machine plus at least one `unknown_state` branch.
  - The routing logic is exposed as a pure function `route(payload: dict) -> dict` in `plan_ops.py` (no argparse, no stdin, no `_emit`); `cmd_review_route` is a thin shim that parses stdin, delegates to `route()`, and emits via `_emit`. Tests call `route()` directly — no subprocess, no stdin monkey-patching. This preserves the option of wrapping `review-route` as an in-process MCP tool in the follow-up migration (§4) without re-shaping the subcommand.

**Description:** The core routing move. After this task lands, the orchestrator LLM stops reading the D.2 / D.2a / D.2a.5 / D.2a.6 tables and starts piping review envelopes to Python. TASK-005's SKILL edits are only safe after this ships.

**Reversion guidance:** Delete the new subcommand handler, the two schema files, and the test file. `plan_ops.py`'s other subcommands are untouched.

---

### TASK-002: Persist orchestrator state in `.schedule.json`

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify)
  - tests/scripts/test_schedule_state.py (create)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_schedule_state.py`
- **Acceptance criteria:**
  - Schedule schema adds an optional `state` object per §3.2; `write-schedule` accepts it and round-trips via `parse-schedule`.
  - `batch-next --from-schedule-state` reads `done` / `failed` / `locked_files` / `blocked` from the schedule; the existing CLI-arg contract continues to work unchanged.
  - `commit-task --update-schedule-state <path>` atomically appends to `state.committed`, promotes the task into `state.done`, and releases its `state.locked_files` entries.
  - `fail-task --update-schedule-state <path>` appends to `state.failed` and persists `state.retries_used[task_id]` transitions.
  - `block-dependents --update-schedule-state <path>` populates `state.blocked`.
  - Old schedules lacking `state` load without error; commands that would mutate state on an old schedule no-op the state write and warn.
  - Atomicity: concurrent writers of `state` don't interleave (reuse the existing atomic-write path used by `write-schedule`).
  - State read/mutation helpers are exposed as pure functions in `plan_ops.py` (e.g., `read_schedule_state(schedule_path: Path) -> dict`, `apply_commit_state_transition(state: dict, task_id: str, sha: str, files: list[str]) -> dict`, `apply_fail_state_transition(state: dict, task_id: str, retries: dict) -> dict`, `apply_blocked_state_transition(state: dict, blocked_ids: list[str]) -> dict`); the `--update-schedule-state` / `--from-schedule-state` subcommand flags are thin shims that read/write the file and delegate to these functions. Tests call the pure functions directly against in-memory dicts — no subprocess, no tempfile. Same rationale as TASK-001: preserves the option of in-process MCP wrapping without reshaping the subcommands.

**Description:** Takes the orchestrator's memory burden and puts it on disk. Required for `review-route` to function stateless-ly (it reads `retries_used` from the schedule, not from the orchestrator's context).

**Reversion guidance:** Revert the schema change, drop the `--update-schedule-state` and `--from-schedule-state` flags, remove the state-mutation helpers. No data loss — the run-log remains the source of truth for audit.

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
  - Length cap (default 8000 chars per free-text field) is applied; truncation adds a trailing `" …[truncated]"` marker and increments `extra.truncated_fields[]`.
  - Code fences (<code>```</code>), markdown ATX headers (`^#{1,6}\s`), and leading `>` blockquotes are stripped from `findings[].message`, `summary`, `diff_summary`.
  - `sanitizer_flags[]` populates when any of these shapes appear: `<system>|<user>|<assistant>` tags, `<tool_calls>` / `<function_calls>` blocks, JSON-looking `"tool":"…"` payloads, the literal `Ignore (previous|prior|above) instructions`.
  - Stray bytes on Codex stdout outside the JSON envelope are dropped with a counter in `extra.dropped_bytes`; they never reach stdout.
  - `plan_codex_dispatch.py implement|review|plan-review` all route through `sanitize()` before envelope emit.
  - Tests include a malicious fixture envelope that injects `<tool_calls>` and fake `<system>` instructions; the sanitized output contains no such shapes and flags them.

**Description:** Closes the prompt-injection gap identified in the architectural review. Moves injection defense to the wrapper where it belongs instead of trusting the orchestrator LLM to self-police.

**Reversion guidance:** Remove the sanitizer module and its test; strip the `sanitize()` call sites in `plan_codex_dispatch.py`. Envelopes emit unfiltered as before.

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
  - `content-sanitizer.md` declares `tools: ` (empty), `model: haiku` (or equivalent cheap tier), and a system prompt instructing it to classify the input text as `{clean | suspicious | malicious}` and emit `{safe: bool, category: str, summary: str}` only.
  - `plan_codex_dispatch.py` gains `--content-sanitizer-check` flag; when set and `extra.sanitizer_flags` is non-empty, the wrapper dispatches the sanitizer, gets a verdict, and stamps it at `extra.content_sanitizer_verdict`.
  - The orchestrator is never passed the raw suspect text — only the verdict and category.
  - Sanitizer dispatch failures degrade gracefully: the envelope still returns, `extra.content_sanitizer_verdict = {status: "error", reason: "…"}`, and routing continues.
  - Integration test uses a stubbed sanitizer (not a real Claude call) and verifies the wrapper's handling of all three categories plus the error case.

**Description:** Stretch goal but architecturally motivated — gives the system a way to do LLM-based intent classification on untrusted text without exposing it to the privileged orchestrator. Safe to defer if timeline pressure forces prioritization; TASK-003 already closes the main gap.

**Reversion guidance:** Delete the agent manifest and integration test; remove the `--content-sanitizer-check` flag from the wrapper argparse.

---

### TASK-005: SKILL.md rewrite — Phase D collapse, CLI diet, LLM roles

- **Status:** pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/skills/implement-plan/SKILL.md (modify)
- **Dependencies:** TASK-001, TASK-002, TASK-003
- **Test command:** deferred (TASK-006)
- **Acceptance criteria:**
  - Phase D.2, D.2a, D.2a.5, D.2a.6 tables are replaced by a single paragraph describing the `review-route` call-and-comply loop, with the `unknown_state` escape hatch named explicitly.
  - The "Cleanup policy (wrapper-enforced, for reference)" section is removed in full.
  - The `plan_ops.py` CLI reference table is condensed to one line per subcommand (purpose only; no flag-by-flag documentation). A footer note points readers at `--help` for flag details.
  - A new "Orchestrator LLM responsibilities" subsection enumerates the three preserved duties from §3.6 and a counter-example list of things the orchestrator must NOT do (track `locked_files`, compose commit flags, recover from envelope-malformed outputs, evaluate untrusted content for intent).
  - The state-dict pseudocode at Phase D entry (`ready`, `done`, `failed`, `locked_files`, `committed`, `review_notes`) is removed; replaced by a one-liner referencing `batch-next --from-schedule-state`.
  - Net character count reduction ≥ 30% (measurable via `wc -c` before/after).
  - No behavioral rules are silently dropped — the hard rules in the "Rules" section are preserved verbatim or strengthened.

**Description:** The context-window payoff. After TASK-001/002/003 ship, this task removes the now-redundant orchestrator-side state-machine documentation and reframes the orchestrator's job around `review-route`.

**Reversion guidance:** Restore SKILL.md from the prior commit on the branch. No code changes to roll back.

---

### TASK-006: Drift guard — SKILL ↔ argparse parity test

- **Status:** pending
- **Priority:** medium
- **Files:**
  - tests/scripts/test_skill_cli_reference_drift.py (create)
- **Dependencies:** TASK-005
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_skill_cli_reference_drift.py`
- **Acceptance criteria:**
  - The test parses `plan_ops.py`'s argparse subcommand list (via introspection of the `subparsers.add_parser(...)` registrations, or by running `plan_ops.py --help` and parsing).
  - It also parses the condensed CLI reference table in SKILL.md.
  - Failure mode 1 (test fails): an argparse subcommand is missing from the SKILL table.
  - Failure mode 2 (test fails): the SKILL table lists a subcommand that argparse does not register.
  - The test explicitly excludes any subcommands marked internal/hidden (currently none; define a convention — `_cmd_internal_*` prefix — if the need arises).

**Description:** Keeps the two documentation surfaces honest. Every future `plan_ops.py` subcommand addition now mechanically requires a SKILL update.

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
  - The PhaseB-rework and PhaseB-narrow-remediation templates accept the `dispatch_context` shape that `review-route` emits (findings_for_retry, dismissed_for_context, d5_summary).
  - Template text is updated to reference the data it receives from `review-route` rather than from inline orchestrator composition.
  - No change to the Phase B default template (it does not depend on Phase D routing).
  - No change to the Phase A analyst template.

**Description:** Mechanical alignment. Templates still live in the SKILL's prompt library; only the expected variable names change to match `review-route`'s output contract.

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
  - The test drives `plan_ops.py preflight → parse-schedule → write-schedule → batch-next → review-route → commit-task | fail-task` against a one-task fixture plan.
  - Subagent outputs are injected via a stub — no real Codex or Claude subagents are spawned.
  - Test matrix covers: clean reviewer verdict → commit; minor-findings → commit with notes; Codex `needs-rework` + D.5 `ship` → commit with disagreement-tag; Codex `needs-rework` + D.5 `partial-agreement` → narrow remediation; narrow remediation success → commit with narrow-remediation-tag + dismissed-finding-ids; narrow remediation second failure → `pause_awaiting_user` with correct payload shape; D.5 `needs-rework` + retry success → commit with remediation-tag; D.5 `needs-rework` + retry failure → `pause_awaiting_user`; envelope with sanitizer flags set → commit proceeds but sanitizer_flags surface in run-log.
  - Run-log events are verified in order: `run_start`, `batch_start`, `implement_done`, `review_done`, `review_route_called`, one of {`commit_done`, `failed`, `awaiting_user`}, `batch_done`, `run_end`.
  - Test runs in under 30s (no real subprocess spawn).

**Description:** The safety net. Locks in the contract between the orchestrator and `review-route` so future SKILL edits cannot silently regress routing.

**Reversion guidance:** Delete the test file and fixture. Other tests remain.

---
