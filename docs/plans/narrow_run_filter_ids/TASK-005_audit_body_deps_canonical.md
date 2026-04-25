# TASK-005 — `body-deps-unparseable` warnings + `body_deps_canonical` audit check

**Base branch:** `main`
**Chunk dependencies:** TASK-002 (the warning is emitted from `_build_tasks` on the scoped path)

---

## Goal

Make the drift between malformed body `**Dependencies:**` prose and the trustworthy roster `chunks[].depends_on` visible in two places: per-task warnings on scoped runs, and a repo-wide audit check (advisory tier) that surfaces every plan whose body deps don't round-trip cleanly. This closes the loop so the deferred prose-cleanup work has a tracked surface.

## Scoped Context

TASK-002 added the `body-deps-unparseable` warning emission as an in-loop side effect of validating in-closure deps against the closure set. This task ensures (a) the warning shape is well-tested in isolation, and (b) a standing audit check at the executor's audit seam (~`plan_ops.py:7194`) flags the same condition across every plan in `<plan_dir>` so an operator can run a one-shot check before authoring a new plan or shipping a refactor.

The audit check is **advisory-tier** by default — it does not flip the audit verdict. Pass `--strict` to include it in the verdict. This matches the precedent set by `portable_tier` (TASK-008 in the original DUAL_AGENT plan).

### Existing surfaces we touch

- `plugins/plan-executor/scripts/plan_ops.py` — register `_check_body_deps_canonical` in `cmd_audit`'s check registry (advisory tier). The warning emission itself is part of TASK-002; this task only adds the audit-side complement.
- `tests/scripts/test_plan_ops.py` — extend `TestAudit` (or wherever audit checks are tested) with cases for the new check.

### Non-goals

- Rewriting the body-deps parser. The point is to surface the drift, not eliminate it.
- Auto-fixing the malformed prose. That's an authoring task.
- Promoting `body_deps_canonical` from advisory to default-tier. The audit's purpose is visibility, not enforcement.

## Verification

**V1.** `audit --json` against a clean plan dir lists `body_deps_canonical` in `findings[]` with `tier: "advisory"`, `status: "pass"`.

**V2.** `audit --json` against `docs/plans/DUAL_AGENT_Plans` (which has 20+ malformed body-deps lines) lists `body_deps_canonical` with `status: "fail"` and a list of offending `{task_id, plan_file, message}` entries. Default verdict is unaffected (advisory tier excluded by default).

**V3.** `audit --strict --json` against the same dir flips the verdict to fail.

**V4.** The check is pure-roster + per-child markdown — same files `_build_tasks` reads. No new disk surface.

**V5.** Running the check on a directory with no `00_INDEX.json` (e.g., a single-file plan that has not been auto-promoted) returns `status: "not_applicable"` with a reason, NOT `fail`.

**V6.** A scoped run on the broken-siblings fixture from TASK-003 emits `body-deps-unparseable` warnings in the `_build_tasks` result for every in-closure child whose body has malformed deps. The warning shape matches TASK-002's contract: `{code, task_id, plan_file, message, roster_deps}`.

---

## Tasks

### TASK-005: `body-deps-unparseable` warnings + `body_deps_canonical` audit check

- **Status:** pending
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-002
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "body_deps or BodyDepsCanonical"`
- **Acceptance criteria:**
  - V1–V6 pass.
  - The audit check is registered with `tier: "advisory"`. The default verdict (no `--strict`) does not include this check; `--strict` does.
  - The check iterates every plan dir under `<plan_dir>` (i.e., every directory containing `00_INDEX.json`). For each plan, it parses each child markdown's `**Dependencies:**` line and compares the parsed-and-normalized result against the chunk's roster `depends_on`. A mismatch (parse failure, missing entry, extra entry) surfaces as a finding.
  - Findings carry per-child detail: `{task_id, plan_file, body_message, roster_deps, mismatch_type}` where `mismatch_type ∈ {unparseable, drift, extra}`.
  - The check skips plans whose `00_INDEX.json` is missing or malformed (returns `status: "not_applicable"` for that plan with a per-plan reason; does not halt the audit).
  - The `body-deps-unparseable` warning shape is consumed by `_build_tasks` exactly as TASK-002 specifies: a per-task warning, not an error.
  - At least 3 new audit tests covering: clean roster + clean bodies (pass), clean roster + malformed body (advisory fail with `unparseable`), clean roster + body deps drifted from roster (advisory fail with `drift`).

**Description:**

The drift this task makes visible is real: `docs/plans/DUAL_AGENT_Plans/TASK-009_scale_aware_reads.md:176` says `**Dependencies:** TASK-001 (schema) and TASK-007 (audit can then verify new fields are documented, not required).` while the roster (`00_INDEX.json` chunk for `task_id: "009"`) declares `"depends_on": ["002"]`. The body says 001 + 007; the roster says 002. Today the executor parses the body and ignores the roster-side deps; under TASK-002's scoped path it does the inverse. Either way, the drift was invisible before this audit check.

The check is intentionally cheap — no schema changes, no rewrites, just a registered-with-the-audit standing finding so an operator running `python plan_ops.py audit --json` before a refactor sees a count of plans that need prose cleanup.

**Implementation notes:**

- The audit registry pattern in `plan_ops.py:7194` already supports advisory-tier checks. Mirror the registration shape used by `portable_tier` if present, or follow the standard `{name, tier, fn}` shape.
- The check function returns `{status, reason, findings}` per existing audit-check contract.
- Findings are sorted by `(plan_file, task_id)` for stable output.
- For drift detection, compare the body-parsed set against the roster `depends_on` set (both normalized). `mismatch_type`: `unparseable` if the body line failed to produce any normalizable IDs, `drift` if the sets differ in any direction (sub or super), `extra` if the body has IDs the roster lacks but no IDs are missing.
- Do NOT auto-correct. The check surfaces the drift; an operator decides whether to fix the body or the roster.

**Reversion guidance:**

`git restore plugins/plan-executor/scripts/plan_ops.py tests/scripts/test_plan_ops.py`. The check is additive; revert removes both the registration and the helper.
