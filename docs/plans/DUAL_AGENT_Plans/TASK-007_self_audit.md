# TASK-007 — Self-Audit and Protocol-Drift Detection

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-007
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md)
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract — the single source of truth self-audit compares against), TASK-002 (validation machinery self-audit reuses), TASK-005 (gate vocabulary).
**Issues absorbed:** none (new executor capability)

---

## Goal

The executor gains a self-audit path that proactively detects contract drift before it becomes another Phase 5-style incident. The core failure mode that produced Phase 5 was: three independent wire contracts drifted silently; the test suite validated the implementation against itself. Self-audit inverts that by comparing the shipped implementation (helpers, schemas, agent prompts, templates, design doc) against a small set of canonical decisions declared in one place, failing loudly when they diverge.

## Scoped Context

**What this is not.** This chunk is not a new test suite. It is not a linter. It is a small number of cross-cutting checks that run outside the normal execution path — typically invoked on demand, in CI, or before a rerun — and surface contract misalignment as a structured report.

**What this is.** A `plan_ops.py audit` subcommand (or equivalent) that:

1. Loads the "Canonical Contract" decision table authored in TASK-001 (`DUAL_AGENT_PLAN_EXECUTOR.md §9.7 or dedicated appendix`).
2. Grep/parse the shipped artifacts to extract their current shape.
3. Compare each extracted shape against the canonical decision. Report mismatches as structured findings.
4. Surface the report as JSON (machine-readable) and Markdown (operator-readable).

**Drift classes to detect.**

- **Status vocabulary drift:** does `ALLOWED_TASK_STATUSES` in `plan_ops.py` match the canonical set (`pending|in-progress|done|failed|blocked|skipped`)?
- **Schedule wire-format drift:** does `cmd_parse_schedule` read `id`/`index` (or the canonical alias window)?
- **Implementer report label drift:** does `cmd_parse_implementer_report` search for `**Concerns for reviewer:**` and `**Plan adaptations:**`?
- **Execution-log column drift:** does `finalize-execution-log` write the columns the design doc §224 prescribes?
- **Schema file drift:** do `codex_implement_schema.json` and `codex_review_schema.json` match the shapes documented in the design doc §7.2 and §7.3?
- **Portable tier drift:** do `SKILL.md` / `dispatch-templates.md` contain `venv/bin/python` literals (TASK-008 fixes; self-audit catches regressions)?
- **Wrapper isolation drift:** does `plan_codex_dispatch.py` still contain `ALWAYS_IGNORE`, `snapshot_baseline` calls at three seams, no `git clean -fd` at repo scope (TASK-003 delivers; self-audit asserts)?
- **Orphan doc drift:** does any design-doc section still reference a deprecated field or a missing subcommand?

**Relation to gates (TASK-005).** Gates check runtime conditions for a specific execution (this plan, this run). Self-audit checks the integrity of the executor itself across *all* plans. A gate failure means "this run cannot proceed"; a self-audit finding means "the executor has drifted — fix before the next rerun".

---

## Verification

**V1 — `audit` subcommand lists all checks.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --list --json
```

Returns a JSON array containing at least: `status_vocabulary`, `schedule_wire_format`, `implementer_report_labels`, `execution_log_columns`, `schemas`, `portable_tier`, `wrapper_isolation`, `design_doc_orphans`.

**V2 — Audit green on post-TASK-003 codebase.**

After TASK-001 through TASK-005 land (and TASK-006 rewrites the fixture), run:

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json
```

Exit 0, all checks report `pass`.

**V3 — Audit detects seeded drift.**

Manually introduce a single drift (e.g., rename `pending` back to `open` in `plan_ops.py:40`) and re-run. Exit 1; finding names `status_vocabulary` with a precise path-and-reason pair.

**V4 — Audit report shape.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json
```

Output shape:

```
{
  "overall": "pass" | "fail",
  "findings": [
    {
      "check": "status_vocabulary",
      "status": "pass" | "fail",
      "canonical": {"source": "...", "value": "..."},
      "actual": {"source": "...", "value": "..."},
      "reason": "..."
    }, ...
  ],
  "generated_at": "ISO-8601"
}
```

**V5 — Markdown report path.**

```bash
venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --report-file /tmp/audit.md
```

Produces a human-readable Markdown table at `/tmp/audit.md`.

**V6 — Verification plan integration.**

The verification plan (whatever is canonical post TASK-006 — likely `sample_phase4.md` itself or a companion `verification_phase5.md`) includes a "drift check" step that runs `audit`. Referenced explicitly in `DUAL_AGENT_PLAN_EXECUTOR.md §14`.

**V7 — No false positives on well-known alias windows.**

If TASK-001 retains an alias (e.g., accepting both `id` and `task_id` during a deprecation window), `audit` reports it as `pass_with_alias` and names the alias window explicitly so it does not masquerade as silent tolerance.

---

## Tasks

### TASK-007: Add production-oriented self-audit and protocol-drift detection

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-001, TASK-002, TASK-005
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k audit`
- **Acceptance criteria:**
  - `plan_ops.py audit` subcommand exists with `--list`, `--json`, `--report-file` modes.
  - Checks cover status vocabulary, schedule wire format, implementer report labels, execution-log columns, schemas, portable tier, wrapper isolation, design-doc orphans.
  - Audit exit code reflects overall pass/fail; structured findings surface path-and-reason pairs.
  - Verification plan (in `DUAL_AGENT_PLAN_EXECUTOR.md §14`) references the audit as part of any rerun.
  - Operators have a documented readiness check after substantive protocol changes.
  - Alias windows (if any from TASK-001) report as `pass_with_alias` without masking genuine drift.
  - Verification checks V1–V7 pass.

**Description:**
The executor needs a standing capability to detect protocol drift before it becomes another Phase 5-style incident.

**Implementation notes:**
Possible interfaces include dedicated validation subcommands, conformance reports, or a certification workflow. The important part is the capability, not the exact command name. Start with grep/parse-based checks; only promote to AST when grep yields false positives.

**Reversion guidance:**
If self-audit is noisy, narrow its scope or improve reporting. Keep the critical drift checks — they are the mechanism that prevents silent Phase-5-style regressions. Do not disable a check without adding an explicit `skip` entry in the decision table.

---

## Implementation Playbook

### Step 1 — canonical decision table as machine-readable

TASK-001 produces a human-readable decision table in `DUAL_AGENT_PLAN_EXECUTOR.md`. For `audit` to cross-check, promote the decisions into either:

(a) A YAML/JSON sidecar at `docs/plans/_canonical_contract.yaml`, or
(b) A Python constant table at the top of `plugins/plan-executor/scripts/plan_ops.py`, e.g.:

```
CANONICAL_CONTRACT = {
    "status_vocabulary": ["pending", "in-progress", "done", "failed", "blocked", "skipped"],
    "schedule_task_field": "id",
    "schedule_batch_field": "index",
    "implementer_concerns_label": "**Concerns for reviewer:**",
    "implementer_plan_adaptations_label": "**Plan adaptations:**",
    "execution_log_columns": ["Task", "Agent", "Reviewer", "Verdict", "Commit", "Notes"],
    "wrapper_always_ignore": ["docs/plans/_run_log.jsonl", "docs/plans/_run_lock.json"],
    "wrapper_always_ignore_globs": ["docs/plans/*.schedule.json"],
}
ALIAS_WINDOWS = {
    "schedule_task_field": ["task_id"],
    "schedule_batch_field": ["batch_index"],
    "implementer_concerns_label": ["**Concerns:**"],
    # If TASK-001 chose to retain any of these; otherwise the alias_windows dict is empty.
}
```

Recommendation: (b) Python constant. Simpler — the decision table and the code that reads it live in the same module.

### Step 2 — check registry

Each check is a function `def _check_<name>() -> dict`:

```
def _check_status_vocabulary():
    from scripts.plan_ops import ALLOWED_TASK_STATUSES, CANONICAL_CONTRACT
    canonical = set(CANONICAL_CONTRACT["status_vocabulary"])
    actual = set(ALLOWED_TASK_STATUSES)
    aliases = set(ALIAS_WINDOWS.get("status_vocabulary", []))
    ok = actual == canonical or actual == canonical | aliases
    return {
        "check": "status_vocabulary",
        "status": "pass" if ok else ("pass_with_alias" if actual == canonical | aliases else "fail"),
        "canonical": {"source": "CANONICAL_CONTRACT", "value": sorted(canonical)},
        "actual": {"source": "ALLOWED_TASK_STATUSES", "value": sorted(actual)},
        "reason": None if ok else "symbols differ",
    }
```

Repeat for each check. Checks that grep source files (e.g., `_check_wrapper_isolation`) return the matching line numbers when they fail.

### Step 3 — check list

Minimum viable check set for V1:

| Name | What it checks |
|---|---|
| `status_vocabulary` | `ALLOWED_TASK_STATUSES` constant vs canonical set |
| `schedule_wire_format` | `cmd_parse_schedule` required-field list contains `id` and `index` (or alias) |
| `implementer_report_labels` | `cmd_parse_implementer_report` searches canonical labels |
| `execution_log_columns` | `cmd_finalize_execution_log` header string matches canonical |
| `schemas` | `codex_implement_schema.json` blockers field matches canonical (array of strings, no object form) |
| `portable_tier` | No `venv/bin/python` literal in `SKILL.md` / `dispatch-templates.md` (TASK-008 fixes this; audit regressions) |
| `wrapper_isolation` | `plan_codex_dispatch.py` contains `ALWAYS_IGNORE`, `snapshot_baseline(` calls at three seams, no `git clean -fd` at repo scope (TASK-003 compliance) |
| `design_doc_orphans` | `DUAL_AGENT_PLAN_EXECUTOR.md` does not reference any `task_id`/`batch_index`/`--skip-analysis` stub that contradicts the canonical decision |

### Step 4 — subcommand wiring

```
parser_audit = subparsers.add_parser("audit", help="Self-audit the executor for contract drift")
parser_audit.add_argument("--list", action="store_true")
parser_audit.add_argument("--json", action="store_true")
parser_audit.add_argument("--report-file", default=None)
parser_audit.add_argument("--check", default=None, help="Comma-separated subset of checks to run")
parser_audit.set_defaults(func=cmd_audit)
```

`cmd_audit`:
- If `--list`: emit the registry names; exit 0.
- Run selected (or all) checks; collect findings.
- `overall = "fail"` iff any finding has `status == "fail"`; `pass_with_alias` is still pass.
- If `--report-file`: also write a Markdown table with columns `Check | Status | Canonical | Actual | Reason`.
- Emit JSON to stdout if `--json`, else human summary.
- Exit code 0 on overall pass, 1 on fail.

### Step 5 — SKILL.md integration

Add a "Readiness check" subsection at the top of `plugins/plan-executor/skills/implement-plan/SKILL.md`:

> Before running `/implement-plan` on a new plan, run `venv/bin/python plugins/plan-executor/scripts/plan_ops.py audit --json`. Any finding with `status: fail` must be resolved before proceeding. This catches protocol drift that would otherwise only surface during a run.

Reference the audit in Phase A preflight as an advisory (not a hard gate — gates are TASK-005's job and are runtime-scoped; audit is advisory-scoped).

### Step 6 — design doc integration

`DUAL_AGENT_PLAN_EXECUTOR.md`:
- Add §14 subsection "Drift detection and readiness".
- Describe the audit as a standing capability; reference `plan_ops.py audit`.
- The verification plan for Phase 5 rerun explicitly includes an audit step before scenarios 1-12.

### Step 7 — tests

`tests/scripts/test_plan_ops.py`:
- `test_audit_list_returns_expected_checks`
- `test_audit_passes_on_clean_codebase` — runs `audit --json`, asserts overall pass (assumes TASK-001 through TASK-006 landed).
- `test_audit_detects_status_vocabulary_drift` — monkeypatch `ALLOWED_TASK_STATUSES` to include `open`, assert `audit` reports `status_vocabulary: fail`.
- `test_audit_reports_alias_window_correctly` — if TASK-001 retains any alias, a test demonstrates `pass_with_alias` status and names the alias window.

### Step 8 — regression sweep

Full pytest; all tests green.

---

## Out of Scope

- **The canonical decisions themselves:** TASK-001.
- **Runtime validators:** TASK-002 — self-audit inspects them; does not duplicate them.
- **Scheduler semantics:** TASK-004.
- **Phase gates:** TASK-005 — gates are runtime; audit is standing.
- **Fixture rewrite:** TASK-006.
- **Portability / preflight:** TASK-008 — audit has a `portable_tier` check that TASK-008 flips from fail to pass.
- **Scale-aware reads / global locks / bounded logs:** TASK-009, 010, 011.

## Reversion guidance

- **Individual checks:** if a specific check is noisy, demote it to `--strict` only (off by default), but keep it registered. Noisy ≠ wrong; the check is the alarm system, not the dispatcher.
- **Decision table sidecar vs inline constant:** if the inline constant causes import cycles, move to a sidecar file. Do not delete the table.
- **Audit subcommand:** safe to revert; TASK-005 gates still catch most runtime issues. Losing audit means protocol drift lives longer before surfacing.
- **Alias windows:** if an alias is dropped unexpectedly, the audit will flag it as `fail` — do not silence by removing the check. Restore the alias or close the window explicitly in the design doc.
