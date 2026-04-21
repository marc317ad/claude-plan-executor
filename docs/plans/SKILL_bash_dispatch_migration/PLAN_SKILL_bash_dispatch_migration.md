# Plan: Migrate SKILL.md Claude-subagent dispatches to `plan_claude_dispatch.py`

**Created:** 2026-04-20
**Status:** draft — pending Codex review
**Base branch:** main
**Related:**
- `plugins/plan-executor/skills/implement-plan/SKILL.md`
- `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`
- `plugins/plan-executor/scripts/plan_codex_dispatch.py` (shape we mirror)
- `docs/plans/PLAN_NESTED_DISPATCH_2026-04-18_v3.md` (builds the wrapper this plan consumes)
- `docs/analysis/Claude_Implement_Plan_context_bloat_response_20260420.md` (motivation — Tier-1 item #1)

---

## 1. Goal

Replace every `Agent` tool invocation of a `plan-executor:*` subagent in `SKILL.md` / `dispatch-templates.md` with a bounded-envelope Bash call to `plan_claude_dispatch.py`, mirroring the existing Codex dispatch pattern. The orchestrator stops ingesting full subagent markdown reports; it parses a size-capped JSON envelope like it already does for Codex. Net effect: orchestrator context per iteration drops materially and the two dispatch surfaces become symmetric.

## 2. Scope

**In scope:**
- Phase 1 plan-analyst dispatch.
- Phase B plan-implementer dispatches: default, `B-rework` (D.2a.5), and `D.2b` role-swap.
- Phase D.2a.6 plan-remediator dispatch.
- Envelope-parsing helpers, error-handling consolidation, run-log event alignment.
- End-to-end smoke + rollback documentation.

**Out of scope:**
- Phase D.1 / D.5 code-reviewer dispatches. `code-reviewer` is NOT in v3's dispatchable set (`{plan-analyst, plan-implementer, plan-remediator}`); migrating it requires expanding v3 scope and is a follow-on. These dispatches stay on the `Agent` tool. Acceptance §6 explicitly asserts no regression on them.
- Building `plan_claude_dispatch.py` — that is v3's deliverable, assumed shipped before TASK-003 starts.
- Dispatch-template text rewrites that change agent behavior. Template *transport* changes only.
- `plan_ops.py review-route` / schedule-state migration (PLAN_PHASE_D_STATE_MACHINE).
- MCP / JSON-schema tool conversion (long-term target, not v1).

## 3. Preconditions

- `plan_claude_dispatch.py` exists and passes its own tests (per v3 §TASKs).
- **Authoritative wrapper surface (per v3 §6–§10):** invocation is `plan_claude_dispatch.py run --input <payload.json>`. The envelope emitted on stdout is shaped `{schema_version, status, status_reason, agent, model, session_id, duration_ms, cost_usd, tokens, result, result_raw_truncated, stderr_tail, permission_denials, scope, trace, error}`. `status ∈ {ok, schema_invalid, timeout, denied, backend_error, budget_exhausted, depth_exceeded, manifest_invalid, input_invalid, scope_violation}`. The per-agent outcome vocabulary (analyst `valid|needs-enrichment|invalid`, implementer `success|partial|failed|plan-incorrect|blocked|malformed`, remediator adds `scope-violation`) lives inside `.result` and is validated against the agent's output schema — it is **not** the wrapper's transport `status`.
- **No existing orchestrator-side parser accepts this shape.** `plan_ops.py parse-schedule` consumes raw analyst JSON (not wrapped); `parse-implementer-report` consumes markdown; only `parse-plan-review-report` currently reads an envelope. TASK-003/004/005 each add a thin extraction step (TASK-006 consolidates them) that unwraps `.result` from the v3 envelope and feeds the existing parsers. No rewrite of existing parsers.
- `dispatch-templates.md` templates for analyst, implementer-default, B-rework, B-narrow-remediation, D.2b role-swap already exist. Each template has two parts: (i) a **transport header** (how to invoke the subagent, output-format instructions) and (ii) an **agent-behavior body** (what the subagent must do, its reasoning rules). TASK-003/004/005 edit (i) only; (ii) is invariant across this plan.

If any precondition is false, TASK-001 fails and the plan halts at Phase 1 analyst review.

## 4. Design sketch

**Before (current):**
```
Agent tool call  → plan-executor:<name> subagent  → full markdown report back to orchestrator
```

**After (this plan):**
```
Bash venv/bin/python plan_claude_dispatch.py run --input <payload.json>
   → payload.agent ∈ {plan-analyst, plan-implementer, plan-remediator}
   → spawns `claude -p --agent plan-executor:<name>` internally
   → validates inner result against agent's output schema
   → emits v3 envelope on stdout ({status, agent, result, scope, error, ...})
   → orchestrator extracts .result, feeds existing plan_ops.py parsers
```

The orchestrator's Phase-B/Phase-D decision logic does not change. Only the transport and the parsing shim change. Existing rules around commit-on-success, rework budgets, and scope-violation handling are preserved verbatim.

---

## 5. Verification commands (global)

- **V-GLOBAL-1:** `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch_*.py` — wrapper unit tests (from v3) continue to pass.
- **V-GLOBAL-2:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py` — orchestrator-side helpers unchanged (guard against accidental drift).
- **V-GLOBAL-3:** `rg -n "subagent_type.*plan-executor:(plan-analyst|plan-implementer|plan-remediator)" plugins/plan-executor/skills/implement-plan/` — MUST return zero hits after TASK-005. Any hit is a migration miss.
- **V-GLOBAL-4:** `rg -n "subagent_type.*plan-executor:code-reviewer" plugins/plan-executor/skills/implement-plan/` — MUST still return the pre-migration call-site count (D.1 + D.5). Asserts reviewer paths untouched.

---

## Tasks

### TASK-001: Canary A/B probe — behavioral parity

- **Status:** pending
- **Priority:** critical
- **Files:**
  - `tests/scripts/test_claude_dispatch_canary.py` (create)
  - `docs/plans/SKILL_bash_dispatch_migration/probe_results.md` (create)
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_canary.py -k canary`
- **Acceptance criteria:**
  1. Probe dispatches `plan-analyst` against a 2-task fixture plan via BOTH paths: (a) `Agent` tool (captured from an orchestrator transcript fixture) and (b) `plan_claude_dispatch.py run --input <payload with agent=plan-analyst>` directly from Bash.
  2. Parsed schedules are semantically equal (`tasks[*].id`, `batches[*].index`, outcome field). Byte equality not required.
  3. Envelope transport `status == "ok"`; `result.outcome ∈ {valid, needs-enrichment, invalid}`; `result.schedule` validates against the analyst output schema.
  4. Envelope size (full stdout JSON) ≤ 64 KB (sanity cap on context-recovery claim).
  5. `probe_results.md` records: timing delta, envelope size delta vs markdown-report size, any behavioral divergence (and whether it is load-bearing or a report-shape artifact). Also records the exact v3 envelope keys observed so downstream tasks lock against reality, not spec.
  6. Also probes implementer and remediator one-shot (same `run --input` path) with trivial payloads, asserting `status=="ok"` and the agent's schema validates. This front-loads "wrapper readiness" for TASK-004/005 so their dep on TASK-001 is meaningful.
- **Out of scope:** running the probe against implementer or remediator; those are covered by TASK-002 harness.

**Description:** De-risks the core claim. If `claude -p --agent` + wrapper doesn't reproduce analyst behavior, nothing downstream matters.

**Reversion guidance:** Delete the test file and probe_results.md. No code paths touched.

---

### TASK-002: Stubbed wrapper harness + envelope fixtures

- **Status:** pending
- **Priority:** critical
- **Files:**
  - `tests/scripts/stubs/plan_claude_dispatch_stub.py` (create) — env-var-configurable stub that records CLI args to a tempfile and returns a fixture envelope.
  - `tests/scripts/fixtures/claude_dispatch/analyst_valid.json` (create)
  - `tests/scripts/fixtures/claude_dispatch/implementer_success.json` (create)
  - `tests/scripts/fixtures/claude_dispatch/implementer_partial.json` (create)
  - `tests/scripts/fixtures/claude_dispatch/remediator_scope_violation.json` (create)
  - `tests/scripts/test_claude_dispatch_stub.py` (create)
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_stub.py`
- **Acceptance criteria:**
  1. Stub is selected by setting `PLAN_CLAUDE_DISPATCH_STUB_FIXTURE=<path>`; real wrapper is used when unset.
  2. Each fixture envelope validates against the v3 envelope keys (`schema_version, status, agent, result, scope, error, trace`) — proves contract parity with the shipped wrapper (not with Codex).
  3. Per-agent JSON Schemas are added under `tests/scripts/fixtures/claude_dispatch/schemas/` for `result` payloads of each dispatchable agent. Fixtures validate against their agent's schema. Analyst schema asserts `result.outcome ∈ {valid, needs-enrichment, invalid}`; implementer schema asserts `result.outcome ∈ {success, partial, failed, plan-incorrect, blocked, malformed}`; remediator schema asserts `result.outcome ∈ {success, partial, failed, plan-incorrect, blocked, malformed, scope-violation}`.
  4. Stub records argv + stdin payload to `$PLAN_CLAUDE_DISPATCH_STUB_RECORD_PATH` as one JSON line per invocation (used by downstream tasks' tests).
  5. Test covers: fixture-not-found error, malformed fixture error, schema-validation failure for each agent, happy path for each of 4 fixtures.
  6. A 5th fixture `implementer_oversized.json` contains a `result_raw_truncated` at exactly v3's 16 KB cap plus a structured `result.report` with all required sub-fields populated — used by TASK-004's envelope-size acceptance.
- **Out of scope:** integrating the stub with any SKILL.md call site.

**Description:** Enables TASK-003..005 to test migrated dispatches without spawning real Claude CLI. Mirrors the stub pattern already used for Codex wrapper integration tests.

**Reversion guidance:** Remove the stub, fixtures, and test file.

---

### TASK-003: Migrate Phase 1 plan-analyst dispatch

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify — Phase 1 dispatch paragraph only)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (modify — analyst template transport section only)
  - `tests/scripts/test_skill_dispatch_analyst.py` (create)
- **Dependencies:** TASK-001, TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_skill_dispatch_analyst.py`
- **Acceptance criteria:**
  1. Phase 1 in SKILL.md replaces its `Agent(subagent_type="plan-executor:plan-analyst", ...)` block with a Bash invocation of `plan_claude_dispatch.py run --input <payload.json>` where payload sets `agent=plan-analyst` and carries the plan path, run_id, and output schema reference.
  2. SKILL.md includes a one-line extraction pointer at the replacement site: *"Read stdout as JSON; assert `.status=="ok"`; extract `.result.schedule` and pipe through `plan_ops.py parse-schedule`; treat `.result.outcome in {valid, needs-enrichment, invalid}` per existing rules."* This extraction shim is inline — no new `plan_ops.py` subcommand.
  3. Analyst outcome vocabulary preserved verbatim (`valid | needs-enrichment | invalid`) and asserted against the analyst `result` schema fixture from TASK-002.
  4. Orchestrator no longer reads full analyst markdown bodies. Only `.result.schedule`, `.result.outcome`, `.result.gaps[]` are surfaced into the orchestrator's context. `.result_raw_truncated` and `.stderr_tail` are ignored on success; referenced only in error-handling (TASK-006).
  5. Fixture test drives the stub via envelope `analyst_valid.json` and asserts the extraction shim + existing `parse-schedule` together produce the same schedule object as the prior markdown path on a captured transcript.
  6. `dispatch-templates.md` analyst **transport header** is updated (how to invoke, output-format JSON instructions). The **agent-behavior body** (the analyst's reasoning rules, gap taxonomy, invalid-condition catalog) is byte-identical to pre-migration. Add a comment marker `<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->` above the behavior section to make the seam explicit for reviewers.
  7. `rg -n "subagent_type.*plan-executor:plan-analyst"` returns zero hits across `plugins/plan-executor/skills/implement-plan/`.
- **Out of scope:** `review-route` integration; changes to gap-type taxonomy; Phase 1.5 plan-review (already Codex-wrapped).

**Description:** First production call site. Plan-analyst is the cleanest target because its output is already heavily structured (schedule JSON), so the envelope transformation is mostly a wrapper-vs-tool swap.

**Reversion guidance:** Restore SKILL.md and dispatch-templates.md from the prior commit; delete the new test file.

---

### TASK-004: Migrate Phase B plan-implementer dispatches (default + B-rework + D.2b)

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify — Phase B default, Phase D.2a.5 rework, Phase D.2b role-swap dispatch paragraphs)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (modify — `PhaseB-default`, `PhaseB-rework`, `PhaseD.2b-role-swap` transport sections)
  - `tests/scripts/test_skill_dispatch_implementer.py` (create)
- **Dependencies:** TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_skill_dispatch_implementer.py`
- **Acceptance criteria:**
  1. All three implementer call sites invoke `plan_claude_dispatch.py run --input <payload.json>` where `payload.agent=plan-implementer` and `payload.variant ∈ {default, rework, role-swap}` carries the template selector. No new subcommand; variant is carried in the payload.
  2. Implementer outcome vocabulary preserved verbatim: `success | partial | failed | plan-incorrect | blocked | malformed`. Validated via TASK-002 implementer schema. `malformed` must round-trip (it is the existing markdown-parser output for un-parseable reports; the wrapper path emits it when `result` schema-validation fails but transport succeeded).
  3. Required report sections (`Plan adaptations`, `Concerns for reviewer`, `On-failure revert`) surface inside `.result.report` as structured arrays (`plan_adaptations[]`, `concerns_for_reviewer[]`, `on_failure_revert`). Orchestrator reads only those fields plus commit-scope metadata. It does NOT ingest `.result_raw_truncated` on success.
  4. `scope.scope_violation_detected` and `scope.scope_misreport_detected` from v3 §7 surface as top-level envelope fields and block commit per existing rules. Wrapper's delta-bounded cleanup is trusted — orchestrator does not re-compute file deltas.
  5. D.2b role-swap retains its `retries_used.role_swap` semantics — budget check happens orchestrator-side before dispatch, not inside the wrapper.
  6. Fixture tests cover: success commit, partial → reviewer notes, scope_violation → fail, plan-incorrect → halt, role-swap happy path, `malformed` outcome → fail-task with stage=`implement`.
  7. **Envelope-size guard (load-bearing):** Test using `implementer_oversized.json` fixture (16 KB `result_raw_truncated` + fully-populated `result.report`) proves all required structured fields are reachable from the envelope even when the raw-result cap is hit. Full envelope size ≤ 80 KB under this fixture (measured: `len(json.dumps(envelope).encode())`).
  8. `rg -n "subagent_type.*plan-executor:plan-implementer"` returns zero hits across `plugins/plan-executor/skills/implement-plan/`.
  9. Transport/behavior separator `<!-- TRANSPORT BOUNDARY -->` added to each of the three implementer templates; agent-behavior text below the marker is byte-identical to pre-migration.
- **Out of scope:** changing the implementer agent's own system prompt; adding new outcome codes; changing retry budgets.

**Description:** Highest-volume dispatch surface. Three call sites in SKILL.md consolidate to one wrapper subcommand with a template selector — the consolidation is itself a context-cost reduction.

**Reversion guidance:** Restore SKILL.md + dispatch-templates.md from prior commit; delete the test file.

---

### TASK-005: Migrate Phase D.2a.6 plan-remediator dispatch

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify — Phase D.2a.6 dispatch paragraph only)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (modify — `PhaseB-narrow-remediation` transport section)
  - `tests/scripts/test_skill_dispatch_remediator.py` (create)
- **Dependencies:** TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_skill_dispatch_remediator.py`
- **Acceptance criteria:**
  1. D.2a.6 dispatch invokes `plan_claude_dispatch.py run --input <payload.json>` where `payload.agent=plan-remediator` and payload carries `load_bearing_findings[]` and `dismissed_findings[]`.
  2. Touch-only-these-lines contract preserved: envelope's `scope.declared_files_changed[]` and `scope.observed_delta_tracked[]` must both be subsets of the `(file, line)` union derived from `load_bearing_findings[]`. Wrapper's delta-bounded cleanup enforces file-level scope; line-level enforcement stays orchestrator-side against the existing diff-hunks helper in `plan_ops.py`.
  3. Remediator outcome vocabulary preserved verbatim: `success | partial | failed | plan-incorrect | blocked | malformed | scope-violation`. The extra `scope-violation` outcome (not present in implementer) and `malformed` both round-trip and are validated by the TASK-002 remediator schema.
  4. Mandatory "Dismissed findings noted" report section surfaces as `.result.report.dismissed_findings_acknowledged[]`; orchestrator's D.5 gate reads from there.
  5. Fixture test uses `remediator_scope_violation.json` fixture from TASK-002 and asserts the orchestrator halts with `pause_awaiting_user` correctly.
  6. `rg -n "subagent_type.*plan-executor:plan-remediator"` returns zero hits across `plugins/plan-executor/skills/implement-plan/`.
  7. Transport/behavior separator `<!-- TRANSPORT BOUNDARY -->` added to the remediator template; agent-behavior text below the marker is byte-identical to pre-migration.
- **Out of scope:** changing touch-only semantics; changing D.5 evidence gate rules.

**Description:** Narrowest remaining call site. Remediator's scope contract is the strictest, so its envelope is the most stateful — this task proves the wrapper can carry that state faithfully.

**Reversion guidance:** Restore SKILL.md + dispatch-templates.md from prior commit; delete the test file.

---

### TASK-006: Error-handling consolidation + run-log event alignment

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (modify — collapse the per-dispatch error-handling sub-paragraphs into one shared section)
  - `plugins/plan-executor/scripts/plan_ops.py` (modify — `log-event` subcommand accepts new `claude_dispatch_*` event types if any were missing)
  - `docs/plans/SKILL_bash_dispatch_migration/run-log-events.md` (create — enumerate the event types)
  - `tests/scripts/test_claude_dispatch_run_log.py` (create)
- **Dependencies:** TASK-003, TASK-004, TASK-005
- **Test command:** `venv/bin/pytest -q tests/scripts/test_claude_dispatch_run_log.py`
- **Acceptance criteria:**
  1. SKILL.md has exactly one "Dispatch error handling (Claude wrapper)" paragraph shared by all three migrated call sites; per-site duplication from TASK-003..005 is removed.
  2. v3 transport `status` values (`schema_invalid | timeout | denied | backend_error | budget_exhausted | depth_exceeded | manifest_invalid | input_invalid | scope_violation`) each map to a deterministic orchestrator action — commit-blocked for any non-`ok`, and the mapping is stated once, not per-site. Mapping table is in the shared paragraph.
  3. An extraction shim is consolidated into a single `plan_ops.py claude-envelope-extract` subcommand (replaces the inline jq-style reads added in TASK-003/004/005). Input: envelope JSON on stdin; args: `--agent {plan-analyst|plan-implementer|plan-remediator}`. Output: `{status, outcome, result, scope_violation, scope_misreport, error}` — normalized across agents. Existing `parse-schedule` / markdown parsers are NOT modified; they continue to consume `result` sub-fields as before.
  4. Run-log event types covered: `claude_dispatch_start`, `claude_dispatch_done`, `claude_dispatch_failed`. `log-event` accepts these as first-class event types (existing `run_start`/`implement_done`/etc unchanged).
  5. Test replays a 3-dispatch sequence (analyst → implementer → remediator) and asserts the run-log has exactly the expected event ordering and payload shape.
  6. No regression on existing run-log events (`run_start, implement_done, review_done, commit_done, failed, awaiting_user`).
  7. After consolidation, the three migrated call sites in SKILL.md each shrink to ~6 lines (invoke + extract + outcome-switch); `wc -c` on SKILL.md shows a net reduction vs the pre-migration baseline (measured and recorded in run-log-events.md).
- **Out of scope:** changing the Codex wrapper's event names to match (symmetry can be a follow-on); rewriting `finalize-execution-log`.

**Description:** Where the real context-win lands. Three dispatch sites sharing one error paragraph is the reason this migration reduces SKILL.md size, not just re-routes bytes.

**Reversion guidance:** Restore SKILL.md, plan_ops.py, and delete the test + events doc.

---

### TASK-007: E2E smoke + rollback documentation

- **Status:** pending
- **Priority:** high
- **Files:**
  - `tests/scripts/test_skill_dispatch_e2e.py` (create)
  - `tests/scripts/fixtures/claude_dispatch/e2e_plan.md` (create — minimal 2-task plan)
  - `docs/plans/SKILL_bash_dispatch_migration/ROLLBACK.md` (create)
- **Dependencies:** TASK-006
- **Test command:** `venv/bin/pytest -q tests/scripts/test_skill_dispatch_e2e.py`
- **Acceptance criteria:**
  1. E2E test drives a full A→E loop (analyst → implementer → D.1 review → commit) on the fixture plan using the stub from TASK-002.
  2. Matrix covers: analyst `valid` → implementer `success` → Codex review `clean` → commit; analyst `needs-enrichment` → halt; implementer `scope_violation` → fail-task; implementer `success` → Codex `needs-rework` → D.5 `ship` → commit with disagreement-tag (verifies D.1/D.5 reviewer paths were NOT touched).
  3. Runs in under 30s with no real subprocess spawn to Claude CLI.
  4. `ROLLBACK.md` documents: (a) per-task revert order (7 → 6 → 5 → 4 → 3 → 2 → 1), (b) the single `git revert` range that undoes all migrated dispatches, (c) the flag to gate the migration behind if a partial rollout becomes necessary (env var `PLAN_EXEC_USE_CLAUDE_WRAPPER=0` → orchestrator falls back to `Agent` tool calls; implementation detail deferred if unused).
  5. Appendix in ROLLBACK.md lists the `Agent` tool call sites that intentionally remain (D.1 / D.5 reviewer) so future readers do not mistake them for migration misses.
- **Out of scope:** the env-var fallback's implementation (acceptance #4 documents it; wiring is only needed if a real rollout hits a snag).

**Description:** The safety net. Locks in the transport contract and documents the exit ramp. Also the only task that exercises the orchestrator-side contract end-to-end, so it's the last line of defense against silent regressions.

**Reversion guidance:** Delete the test + fixture + ROLLBACK.md. If the wider migration rolls back, this task reverts in the same changeset.

---

## Risks and open questions

- **Wrapper build slippage.** If v3's `plan_claude_dispatch.py` is not ready when this plan runs, TASK-001 fails and the plan halts before any SKILL.md edit. TASK-004 and TASK-005 both depend on TASK-001 (not just TASK-002) so a wrapper that works for analyst but not implementer/remediator still halts the plan correctly.
- **v3 envelope shape is the contract of record.** If v3 ships a different envelope shape than §3 documents here, TASK-001 probe_results.md records the actual keys and this plan must be revised before TASK-002 starts. Codex review caught this risk — the original draft assumed a Codex-shaped envelope that v3 does not emit.
- **`--allowedTools` enforcement (v3 Probe 2b).** If Probe 2b shows `acceptEdits` does not enforce the tool allowlist, delta-bounded cleanup carries the full weight of preventing scope creep. This plan assumes v3 handled that; if not, TASK-004 acceptance #4 and TASK-005 acceptance #2 need tightening.
- **Reviewer dispatches left behind.** Asymmetric surface (analyst/implementer/remediator via wrapper; reviewer via `Agent` tool) is confusing for future readers. Mitigation: TASK-007 ROLLBACK.md appendix explicitly enumerates the remaining `Agent` sites. Symmetric migration is a follow-on plan once v3 expands its dispatchable set to include `code-reviewer`.
- **Envelope size growth.** If implementer reports routinely exceed the wrapper's `result_raw_truncated` cap (16 KB per v3 §7), structured fields in `.result.report` must carry the load-bearing content. TASK-004 acceptance #7 tests this explicitly with an oversized fixture — not just by assertion.
