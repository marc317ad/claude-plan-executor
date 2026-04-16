# TASK-008 — Portability Parameterization and Scope-Aware Preflight

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-008
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md) ISSUE-007, ISSUE-008
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9.1 Preflight, §11.1-11.2 Environment, §13 Assumptions
**Base branch:** `phase-7b5-bug-fixes`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** TASK-001 (canonical contract — affects the preflight result wire shape only if changed), TASK-007 (`portable_tier` audit check flips from fail to pass once this lands).
**Issues absorbed:** ISSUE-007 (P1 portability), ISSUE-008 (P1 preflight classification).

---

## Goal

Remove two usability/portability defects that block adoption outside this exact checkout:

1. Replace hardcoded `venv/bin/python` literals with a portable `$PYTHON` indirection so the executor runs on systems whose Python binary lives elsewhere (`.venv/bin/python`, system `python3`, conda).
2. Replace the preflight dirty-file classifier's broad directory-prefix rule (`.claude/`, `docs/`, `tests/`) with a plan-scoped rule that consults the actual plan's declared `allowed_files` and always-ignore set.

Neither issue was a Phase 5 blocker in our environment, but both are documented gaps between the design intent ("generic executor") and the shipped implementation ("our repo only").

## Scoped Context

### ISSUE-007 (P1) — Hardcoded `venv/bin/python` breaks portability

Per the audits: the executor is written as a generic dual-agent orchestrator, but both the skill body and helper docstrings assume a `venv/bin/python` interpreter at the repo root. Counts verified at `d0f9740`:

- `.claude/skills/implement-plan/SKILL.md`: **14 occurrences**.
- `.claude/skills/implement-plan/dispatch-templates.md`: **3 occurrences**.
- `scripts/plan_ops.py` usage docstring (lines 8-20): **13 occurrences** in the module header.
- Additional command-lines may appear inside `plan_codex_dispatch.py` for test runs. Sweep during implementation.

Consequence: on any machine without `./venv/bin/python` at that exact path (a conda checkout; a `.venv` convention; a system `python3`; Windows), the instructions are wrong and the user must hand-edit. Design §11.1-11.2 describes environment assumptions per-repo — this is supposed to be carried by per-repo overrides (e.g., `CLAUDE.md`), not baked into the skill.

**Decision vocabulary.** Introduce `$PYTHON` as the canonical interpreter reference in the skill and templates. Resolution precedence (highest first):

1. Explicit environment variable `IMPLEMENT_PLAN_PYTHON` if set.
2. Project-local override in `CLAUDE.md` or `.implement-plan.env` (if established).
3. `venv/bin/python` if it exists in the current working directory.
4. `.venv/bin/python` if it exists.
5. `python3` on `$PATH`.

The orchestrator resolves `$PYTHON` once at phase A preflight and passes the resolved absolute path into the skill environment via `plan_ops.py preflight --json` output (`python_path` field). Templates then interpolate `{{python_path}}` at dispatch time.

### ISSUE-008 (P1) — Preflight classifies dirty files by blunt directory prefix

`scripts/plan_ops.py:182-193` (`cmd_preflight`) partitions `git status --porcelain` output into three buckets:

```python
dirty: dict[str, list[str]] = {"source_blocking": [], "infra_ignored": [], "plan_doc": []}
...
elif path.startswith((".claude/", "docs/", "tests/")):
    dirty["infra_ignored"].append(path)
else:
    dirty["source_blocking"].append(path)
```

Problems:

- Any dirty file under `.claude/`, `docs/`, or `tests/` is silently ignored — even if the active plan doesn't touch those paths. A plan editing `src/example/module.py` with a stray uncommitted file in `docs/analysis/` passes preflight; the reviewer has no idea that `docs/` surface was already dirty pre-run.
- Conversely, a plan whose tasks legitimately touch `src/` will flag correctly, but a plan whose tasks *do* touch `.claude/skills/*` would *also* pass regardless of the pre-run state of those files — masking real interference.
- There is no consultation of the plan's own scope. Preflight should know what this plan intends to touch.

**Decision vocabulary.** Replace the blunt prefix rule with a three-tier classifier:

1. **`plan_doc`** (always-ignore) — the plan file itself; identical to current behavior.
2. **`orchestrator_state`** (always-ignore) — `ALWAYS_IGNORE` / `ALWAYS_IGNORE_GLOBS` from TASK-003 (`docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `docs/plans/*.schedule.json`).
3. **`plan_scope_dirty`** (reportable, *not* auto-blocking unless `--strict-scope`) — path is present in the union of all tasks' `allowed_files` in the plan. Report with task attribution: "file X is dirty and TASK-NNN will write to it".
4. **`source_blocking`** (blocking) — all other uncommitted paths.

Crucially, category 4 is the default blocker. Category 3 is a *scoped* warning the operator must resolve — not a silent pass. The `.claude/` / `docs/` / `tests/` prefix heuristic is retired.

### Relation to TASK-007 (self-audit)

TASK-007 adds a `portable_tier` audit check that greps for `venv/bin/python` literals in SKILL.md and dispatch-templates.md. That check is expected to fail until TASK-008 lands, then pass. After TASK-008, any regression reintroducing a `venv/bin/python` literal in the skill surface is caught by audit, not by a Phase 5 scenario.

---

## Verification

**V1 — No `venv/bin/python` literal in skill surface.**

```bash
grep -nE 'venv/bin/python' .claude/skills/implement-plan/SKILL.md \
  .claude/skills/implement-plan/dispatch-templates.md
```

Zero matches. (Literals in `scripts/plan_ops.py` header docstring are acceptable as example commands *if* they also show the `$PYTHON` alternative on the adjacent line; preferred is to convert the header to `$PYTHON scripts/plan_ops.py ...`.)

**V2 — `$PYTHON` placeholder present.**

```bash
grep -nE '\$PYTHON|\{\{python_path\}\}' .claude/skills/implement-plan/SKILL.md \
  .claude/skills/implement-plan/dispatch-templates.md
```

≥10 matches in SKILL.md, ≥3 in dispatch-templates.md (one per former literal site).

**V3 — Preflight surfaces resolved `python_path`.**

```bash
venv/bin/python scripts/plan_ops.py preflight --plan-file docs/plans/sample_phase4.md --json
```

Output JSON includes `"python_path": "<absolute path>"`. Path exists and is executable.

**V4 — `$PYTHON` resolution precedence respected.**

Integration test: with `IMPLEMENT_PLAN_PYTHON=/tmp/fake-python` set (file must exist + be executable), `preflight --json` returns `python_path: "/tmp/fake-python"`. Unset → falls back to `venv/bin/python` if present. Unset + `venv/bin` removed → falls back to `.venv/bin/python` or `python3`.

**V5 — Preflight scope-aware classifier.**

Set up scratch state:
- Plan `docs/plans/sample_phase4.md` declares `TASK-001` touches `docs/plans/sample_phase4_scratch/rename_helper.py`.
- Leave `src/example/module.py` dirty (uncommitted change unrelated to plan).
- Leave `docs/plans/sample_phase4_scratch/rename_helper.py` dirty too.
- Leave `docs/analysis/X.md` dirty (outside plan scope).

Run preflight. Expected output:

```json
{
  "pass": false,
  "dirty_files": {
    "plan_doc": [],
    "orchestrator_state": [],
    "plan_scope_dirty": [
      {"path": "docs/plans/sample_phase4_scratch/rename_helper.py",
       "task_id": "001"}
    ],
    "source_blocking": [
      "src/example/module.py",
      "docs/analysis/X.md"
    ]
  },
  "scope_warnings": [
    "docs/plans/sample_phase4_scratch/rename_helper.py is dirty and TASK-001 will write to it"
  ]
}
```

Key invariants:
- `src/example/module.py` is `source_blocking` (not ignored by old `.claude/` rule).
- `docs/analysis/X.md` is `source_blocking` (retired `docs/` prefix ignored category).
- `docs/plans/sample_phase4_scratch/rename_helper.py` is `plan_scope_dirty` with task attribution.
- `pass == false` because either category 3 or category 4 is non-empty.

**V6 — `--strict-scope` upgrades warnings to blocks.**

```bash
venv/bin/python scripts/plan_ops.py preflight --plan-file docs/plans/sample_phase4.md \
  --strict-scope --json
```

Returns `pass: false` if `plan_scope_dirty` is non-empty. Without `--strict-scope`, `plan_scope_dirty` is reported but does not by itself fail the gate — `source_blocking` does.

**V7 — Always-ignore set is respected.**

Dirty-file list includes `docs/plans/_run_log.jsonl` (always-ignore). Classifier bins it into `orchestrator_state`, not `source_blocking` or `plan_scope_dirty`.

**V8 — Tests pass.**

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "preflight or python_path"
```

All green.

**V9 — TASK-007 `portable_tier` audit flips to pass.**

```bash
venv/bin/python scripts/plan_ops.py audit --check portable_tier --json
```

Returns `"status": "pass"` (after TASK-007 + TASK-008 both land).

---

## Tasks

### TASK-008: Portability parameterization + scope-aware preflight

- **Status:** pending
- **Priority:** high
- **Files:**
  - `.claude/skills/implement-plan/SKILL.md`
  - `.claude/skills/implement-plan/dispatch-templates.md`
  - `scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` (§11.1 environment notes)
- **Dependencies:** TASK-001 (plan schema for allowed_files extraction).
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py -k "preflight or python_path"`
- **Acceptance criteria:**
  - No `venv/bin/python` literal in `SKILL.md` or `dispatch-templates.md`; `$PYTHON` / `{{python_path}}` used instead.
  - `plan_ops.py preflight` resolves `$PYTHON` per the documented precedence and emits `python_path` in its JSON.
  - Preflight classifier consults the plan's `allowed_files` union instead of the `.claude/`/`docs/`/`tests/` prefix rule.
  - `orchestrator_state` category correctly bins always-ignore paths.
  - `plan_scope_dirty` entries include `{path, task_id}` attribution; operator sees exactly which task is at risk.
  - `--strict-scope` flag upgrades `plan_scope_dirty` to a blocking condition.
  - All verification checks V1–V9 pass.

**Description:**
Adopt a $PYTHON abstraction for the skill and dispatch templates, and upgrade the preflight dirty-file classifier to consult the active plan's declared scope. Both changes decouple the executor from the specific layout of this checkout and make preflight's "pass" meaningful.

**Implementation notes:**
Python resolution precedence stays simple; the interpreter lookup happens in one place (`_resolve_python()` helper in `plan_ops.py`) and the result is echoed back to the orchestrator through preflight JSON. Do not scatter resolution logic across templates. For the classifier, build the `allowed_files` union once per preflight call and reuse it.

**Reversion guidance:**
If the `$PYTHON` abstraction confuses users, retain `venv/bin/python` in example lines but keep `$PYTHON` as the canonical form in the numbered phase steps. Do not revert the preflight classifier upgrade; if the scope rule is too noisy on a specific plan, use `--strict-scope` as an opt-in and keep the non-strict default as an advisory warning.

---

## Implementation Playbook

### Step 1 — Python resolution helper

Add to `scripts/plan_ops.py` near the other helpers:

```python
def _resolve_python() -> str:
    """Resolve the interpreter path per documented precedence.

    Precedence (first match wins):
      1. $IMPLEMENT_PLAN_PYTHON if the path exists and is executable.
      2. <cwd>/venv/bin/python if present.
      3. <cwd>/.venv/bin/python if present.
      4. shutil.which('python3').
    Returns absolute path.
    """
    env = os.environ.get("IMPLEMENT_PLAN_PYTHON")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return str(Path(env).resolve())
    for rel in ("venv/bin/python", ".venv/bin/python"):
        p = Path.cwd() / rel
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    which = shutil.which("python3")
    if which:
        return which
    return sys.executable
```

Echo the resolved value into preflight output:

```python
result = {
    "pass": pass_flag,
    "starting_sha": starting_sha,
    ...
    "python_path": _resolve_python(),
}
```

### Step 2 — scope-aware classifier

Replace `scripts/plan_ops.py:182-193` with a classifier that consumes the plan's tasks.

Pseudocode:

```python
ALWAYS_IGNORE = {"docs/plans/_run_log.jsonl", "docs/plans/_run_lock.json"}
ALWAYS_IGNORE_GLOBS = ("docs/plans/*.schedule.json",)

def _allowed_files_union(plan_text: str) -> dict[str, str]:
    """Return {path: task_id} for every file listed under any task's Files: bullet."""
    result = {}
    for task_id, task_block in _iter_task_blocks(plan_text):
        for path in _extract_allowed_files(task_block):
            result[path] = task_id  # last wins; duplicates are already a plan smell
    return result

def _is_always_ignored(path: str) -> bool:
    if path in ALWAYS_IGNORE:
        return True
    return any(fnmatch.fnmatch(path, g) for g in ALWAYS_IGNORE_GLOBS)

scope = _allowed_files_union(plan_text)
dirty = {"plan_doc": [], "orchestrator_state": [], "plan_scope_dirty": [], "source_blocking": []}
warnings = []

for path in changed_paths:
    if path == str(plan) or path.endswith(plan.name):
        dirty["plan_doc"].append(path)
    elif _is_always_ignored(path):
        dirty["orchestrator_state"].append(path)
    elif path in scope:
        tid = scope[path]
        dirty["plan_scope_dirty"].append({"path": path, "task_id": tid})
        warnings.append(f"{path} is dirty and TASK-{tid} will write to it")
    else:
        dirty["source_blocking"].append(path)

pass_flag = len(dirty["source_blocking"]) == 0
if getattr(args, "strict_scope", False) and dirty["plan_scope_dirty"]:
    pass_flag = False
if args.strict_branch and not base_branch_match:
    pass_flag = False
```

Helpers `_iter_task_blocks` and `_extract_allowed_files` should reuse the plan-parsing logic already present for `parse-implementer-report` and `commit-task`; factor if needed.

Add the `--strict-scope` CLI flag:

```python
parser_preflight.add_argument("--strict-scope", action="store_true")
```

### Step 3 — SKILL.md $PYTHON sweep

`.claude/skills/implement-plan/SKILL.md`:

- At the top of the skill body, add a "Python interpreter resolution" paragraph:
  > The orchestrator resolves `$PYTHON` once at phase A preflight via `plan_ops.py preflight --json`'s `python_path` field. All subsequent commands must use the resolved path. Do not hardcode `venv/bin/python`.
- Replace all 14 occurrences of `venv/bin/python` with `$PYTHON`. After each replacement, verify the surrounding phrasing still reads correctly (example: "run `$PYTHON scripts/plan_ops.py ...`" is idiomatic bash).
- In Phase A, add a step: "After preflight, set `PYTHON=<python_path>` from its JSON output."

### Step 4 — dispatch-templates.md $PYTHON sweep

`.claude/skills/implement-plan/dispatch-templates.md`:

- Replace all 3 occurrences of `venv/bin/python` with `{{python_path}}`.
- Document near the template header: templates are interpolated by the orchestrator; `{{python_path}}` is substituted from preflight's `python_path`.

### Step 5 — plan_ops.py header docstring

Header docstring at lines 8-20 enumerates example invocations with `venv/bin/python`. Convert to `$PYTHON`:

```python
"""
Executor helper utilities.

Example invocations (resolve $PYTHON via `preflight --json`'s python_path):

    $PYTHON scripts/plan_ops.py preflight --plan-file <abs> [--strict-branch] [--strict-scope]
    $PYTHON scripts/plan_ops.py parse-schedule --stdin
    ...
"""
```

### Step 6 — design doc integration

`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` §11.1-11.2:

- Add a "Portable interpreter" subsection describing `$PYTHON` resolution precedence.
- Reference `preflight --json`'s `python_path` as the authoritative value once resolved.
- Update §9.1 preflight description: classifier uses plan scope; `--strict-scope` is available for operators who want strict exit codes.

### Step 7 — tests

`tests/scripts/test_plan_ops.py`:

- `test_preflight_resolves_python_from_env` — monkeypatch `IMPLEMENT_PLAN_PYTHON` to a scratch executable; assert `python_path` matches.
- `test_preflight_resolves_python_fallback_venv` — unset env; stub `Path.is_file` to return True for `venv/bin/python`; assert that path is returned.
- `test_preflight_resolves_python_fallback_which` — unset env; both venvs absent; mock `shutil.which('python3')`; assert that path is returned.
- `test_preflight_classifier_plan_scope` — scaffolded dirty tree with the four categories above; assert the exact partition.
- `test_preflight_strict_scope_blocks` — same scaffold; pass `--strict-scope`; assert `pass: false`.
- `test_preflight_classifier_respects_always_ignore` — dirty `docs/plans/_run_log.jsonl`; assert it lands in `orchestrator_state`, not `source_blocking`.

### Step 8 — SKILL surface audit

After the sweep, run `scripts/plan_ops.py audit --check portable_tier --json` (from TASK-007). It must pass. If it does not, locate the missed literal with the grep in V1.

### Step 9 — regression sweep

Full test suite:

```bash
venv/bin/pytest -q tests/scripts/
```

All tests green.

---

## Out of Scope

- **Canonical contract (status vocabulary, wire shape):** TASK-001 owns those decisions; this chunk uses the existing shape.
- **Wrapper isolation / always-ignore constant definition:** TASK-003 owns those; this chunk imports them.
- **Self-audit check for portability:** TASK-007 owns `portable_tier`; this chunk just flips it from fail to pass.
- **Runtime validation:** TASK-002.
- **Scheduler semantics:** TASK-004.
- **Phase gates:** TASK-005.
- **Fixture rewrite:** TASK-006.
- **Scale-aware reads / global locks / bounded logs:** TASK-009, 010, 011.

## Reversion guidance

- **`$PYTHON` rollout:** if external users report confusion, keep the precedence and the preflight `python_path` echo, but restore literal `venv/bin/python` in example comments only. Do not revert the resolver helper — it is the single source of truth.
- **Scope-aware classifier:** if a particular plan has so many pre-dirty scoped files that `--strict-scope` becomes unusable, triage the plan's scope rather than widening the classifier. The old `.claude/`/`docs/`/`tests/` rule is a footgun; do not restore it.
- **`--strict-scope` flag:** safe to default-off; tighter policy can ride on a follow-up. Never default-on without operator buy-in — it will block reruns whose scope overlaps mid-edit files.
