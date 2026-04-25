# TASK-010 — Globally-Locked Dependency / Environment Paths

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-010
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §9.4 scheduler, §9.4 parallel execution
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** none (formerly TASK-004, TASK-003, TASK-007 — now archived/complete).
**Issues absorbed:** none (new executor capability; surfaced by Phase 5 scenario catalog's gap between parallel-sibling isolation and global-state mutation risk).

---

## Goal

Prevent interleaved mutation of globally-shared environment files across parallel batches. Today the scheduler treats two tasks as parallel-safe if their `allowed_files` sets are disjoint. But if TASK-A edits `requirements.txt` and TASK-B concurrently imports a package already being reinstalled, the test-run environment is unstable and results are not repeatable. The same applies to `pyproject.toml`, `poetry.lock`, `package.json`, `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `Cargo.toml`, `Cargo.lock`, `go.mod`, `go.sum`, `Gemfile`, `Gemfile.lock`, `composer.json`, `composer.lock`, and `Dockerfile` / `docker-compose.yml`. This chunk introduces a `GLOBAL_LOCK_PATHS` set plus a scheduler rule: if any task's `allowed_files` intersects the global-lock set, that task serializes against *all other tasks in the plan*, regardless of file overlap.

## Scoped Context

### The parallel-safety gap

TASK-004 makes the scheduler compute `batches[]` by greedy parallelization of tasks whose `allowed_files` sets are disjoint. This is correct for *code* files but wrong for *environment-affecting* files. Two concrete failure modes:

1. **Lock-file race.** Task A edits `requirements.txt` adding `foo==1.2`. Task B runs a test that imports `foo`. If A and B batch in parallel, B's test hits whichever version is resolved at test time — possibly a different version than the one A committed.
2. **Dockerfile race.** Task A edits `Dockerfile` changing a base image. Task B runs a test inside the image. Same race, larger blast radius.

Both are silent corruptions. They do not show up until a downstream developer tries to reproduce the run.

### Scope decision

The set of globally-locked paths is a canonical contract decision (a sibling of TASK-001's decisions). Default set at first landing:

```
GLOBAL_LOCK_PATHS = {
    # Python
    "requirements.txt", "requirements-dev.txt", "pyproject.toml",
    "poetry.lock", "Pipfile", "Pipfile.lock", "setup.cfg", "setup.py",
    # Node
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # Rust
    "Cargo.toml", "Cargo.lock",
    # Go
    "go.mod", "go.sum",
    # Ruby
    "Gemfile", "Gemfile.lock",
    # PHP
    "composer.json", "composer.lock",
    # Containers
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    # CI
    ".github/workflows/*.yml", ".github/workflows/*.yaml",
}
```

Globs are supported. The set is declared in one place (`plugins/plan-executor/scripts/plan_ops.py`) and documented in `DUAL_AGENT_PLAN_EXECUTOR.md` §9.4.

### Scheduler rule

Let `global_lock_tasks = {t for t in plan.tasks if any(f matches GLOBAL_LOCK_PATHS for f in t.files)}`.

Rule: every task in `global_lock_tasks` must occupy its own batch alone — no parallel siblings, no other global-lock task in the same batch. Ordering respects the existing dependency graph; only the parallel dimension is collapsed.

Equivalent statement: whenever a global-lock task T is placed into batch B, no other task is assigned to batch B, and all tasks topologically after T (even if unrelated) land in later batches. "Topologically after" here means "appears in any batch with index > B's index."

### Configurability

The set is a default, but operators running in a constrained repo may need to add paths (e.g., `.env.example`, `secrets-template.yaml`). The scheduler reads an optional YAML override at `docs/plans/_global_lock_paths.yaml`; if absent, uses the default.

### Relation to TASK-004 scheduler

TASK-004 already ships the batcher. This chunk adds:

1. A `GLOBAL_LOCK_PATHS` constant + default YAML template.
2. A pre-batch analysis step that tags tasks as `global_lock: true` in the schedule JSON.
3. A batch-assignment rule that enforces solitary placement.

The analyst (`plan-analyst`) can classify the tag during Step 3; or the scheduler helper can tag post-hoc during `parse-schedule` / `write-schedule`. Prefer the latter: one code path, no analyst-prompt dependency.

---

## Verification

**V1 — Constant is surfaced.**

```bash
$PYTHON plugins/plan-executor/scripts/plan_ops.py list-global-lock-paths --json
```

Emits the default set as a JSON array.

**V2 — Schedule tags global-lock tasks.**

Craft a plan fragment where TASK-001 edits `src/foo.py` and TASK-002 edits `requirements.txt`.

```bash
$PYTHON plugins/plan-executor/scripts/plan_ops.py parse-schedule --stdin < plan.json
```

Output contains `"global_lock": true` on TASK-002's record and `"global_lock": false` on TASK-001's.

**V3 — Batcher isolates global-lock tasks.**

Same plan, three independent tasks TASK-001 (`src/a.py`), TASK-002 (`requirements.txt`), TASK-003 (`src/b.py`). Without global lock rule, all three batch in parallel. With rule:

```
batches[0] = [001, 003]
batches[1] = [002]
```

or

```
batches[0] = [002]
batches[1] = [001, 003]
```

— either order respecting deps is acceptable; what is *not* acceptable is all three in one batch.

Assert via:

```bash
$PYTHON plugins/plan-executor/scripts/plan_ops.py batch-next --schedule-file <sched> --batch-index 0 --json
```

TASK-002 never shares a batch with TASK-001 or TASK-003.

**V4 — Dependency ordering still respected.**

Plan with TASK-001 (`requirements.txt`) → TASK-002 (`src/foo.py`, depends on 001). TASK-002 cannot run until TASK-001 completes. Existing DAG behavior unchanged.

**V5 — Two global-lock tasks serialize.**

TASK-001 (`requirements.txt`), TASK-002 (`package.json`), no dependency declared. Rule: they cannot co-batch. Each occupies its own batch.

**V6 — Override file supported.**

Create `docs/plans/_global_lock_paths.yaml`:

```yaml
additional:
  - ".env.example"
  - "config/global.toml"
```

Re-run `list-global-lock-paths`. Output includes both plus the default set. Re-run the batcher with a plan editing `.env.example`; rule applies.

**V7 — Audit check passes.**

```bash
$PYTHON plugins/plan-executor/scripts/plan_ops.py audit --check global_lock_paths --json
```

(TASK-007 audit has a new check added by this chunk; passes iff the constant matches the documented set in §9.4 of the design doc.)

**V8 — Tests green.**

```bash
$PYTHON -m pytest -q tests/scripts/test_plan_ops.py -k global_lock
```

---

## Tasks

### TASK-010: Globally-locked dependency / environment paths

- **Status:** pending
- **Priority:** medium
- **Files:**
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` (§9.4 Phase 2 batch execution — scheduler / parallel)
  - `plugins/plan-executor/scripts/plan_ops.py` (constant + override loader + subcommand + batch rule)
  - `tests/scripts/test_plan_ops.py`
  - `docs/plans/_global_lock_paths.yaml.example` (documented override template)
- **Dependencies:** none
- **Test command:** `$PYTHON -m pytest -q tests/scripts/test_plan_ops.py -k global_lock`
- **Acceptance criteria:**
  - `GLOBAL_LOCK_PATHS` constant declared in one place in `plugins/plan-executor/scripts/plan_ops.py` with the default set.
  - `list-global-lock-paths` subcommand emits the effective set (default + override).
  - `parse-schedule` / `write-schedule` tags each task with `global_lock: bool`.
  - Batcher rule: any task with `global_lock: true` occupies its own batch alone.
  - Override file `docs/plans/_global_lock_paths.yaml` is honored if present; default if absent.
  - `DUAL_AGENT_PLAN_EXECUTOR.md` §9.4 documents the rule and lists the default set.
  - Audit check `global_lock_paths` (TASK-007 registry extension) verifies constant-vs-doc alignment.
  - All verification checks V1–V8 pass.

**Description:**
Declare globally-locked paths and serialize any task touching them. Prevents parallel tasks from racing on shared environment state.

**Implementation notes:**
Keep the default set conservative — false positives (serializing an unrelated task) are harmless slowdowns; false negatives (allowing a race) are silent corruption. Globs should use `fnmatch` for consistency with `ALWAYS_IGNORE_GLOBS` (TASK-003).

**Reversion guidance:**
If the default set is too aggressive for a specific repo, override via YAML rather than patching the constant. If the scheduler rule interacts badly with a specific plan (e.g., two genuinely-independent config files that happen to match a glob), consider narrowing the glob rather than disabling the rule.

---

## Implementation Playbook

### Step 1 — constants

Add to `plugins/plan-executor/scripts/plan_ops.py` near the `ALWAYS_IGNORE` constant (from TASK-003):

```python
GLOBAL_LOCK_PATHS = frozenset({
    "requirements.txt", "requirements-dev.txt", "pyproject.toml",
    "poetry.lock", "Pipfile", "Pipfile.lock", "setup.cfg", "setup.py",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "Cargo.toml", "Cargo.lock",
    "go.mod", "go.sum",
    "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
})
GLOBAL_LOCK_GLOBS = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)
```

Helper:

```python
def _effective_global_lock_set() -> tuple[frozenset[str], tuple[str, ...]]:
    base_paths = set(GLOBAL_LOCK_PATHS)
    base_globs = list(GLOBAL_LOCK_GLOBS)
    override = Path("docs/plans/_global_lock_paths.yaml")
    if override.is_file():
        import yaml
        data = yaml.safe_load(override.read_text()) or {}
        for p in data.get("additional", []):
            if any(c in p for c in "*?["):
                base_globs.append(p)
            else:
                base_paths.add(p)
    return frozenset(base_paths), tuple(base_globs)


def _is_global_lock_path(path: str) -> bool:
    paths, globs = _effective_global_lock_set()
    if path in paths:
        return True
    return any(fnmatch.fnmatch(path, g) for g in globs)
```

### Step 2 — `list-global-lock-paths` subcommand

```python
def cmd_list_global_lock_paths(args):
    paths, globs = _effective_global_lock_set()
    out = {"paths": sorted(paths), "globs": list(globs)}
    print(json.dumps(out, indent=2))

parser_lglp = subparsers.add_parser("list-global-lock-paths",
    help="Emit the effective globally-locked path set")
parser_lglp.set_defaults(func=cmd_list_global_lock_paths)
```

### Step 3 — schedule tagging

In the schedule builder (produced by `parse-schedule` / `write-schedule` in TASK-002), add per-task:

```python
task["global_lock"] = any(_is_global_lock_path(f) for f in task["files"])
```

Schema note: document `global_lock: bool` as a required output field on every task record in `DUAL_AGENT_PLAN_EXECUTOR.md` §9.4.

### Step 4 — batcher rule

In the batch-assignment loop (in TASK-004's `batch-next` / in the schedule writer's batch layout), add the constraint:

```python
for ready_task in ready_set:
    if ready_task.global_lock:
        # Solitary batch; emit alone.
        batches.append([ready_task])
        ready_set.remove(ready_task)
        continue
    # Otherwise: normal disjoint-allowed_files packing.
    ...
```

When several global-lock tasks are ready simultaneously, each still gets its own batch; ordering within that subset is arbitrary (deterministic by task-id).

### Step 5 — override template

Create `docs/plans/_global_lock_paths.yaml.example`:

```yaml
# Additional paths to treat as globally-locked in the scheduler.
# Supported: plain paths or fnmatch globs.
additional:
  # - .env.example
  # - config/global.toml
  # - ci/pipelines/*.yml
```

Do *not* create `docs/plans/_global_lock_paths.yaml` itself; that is operator-created when needed.

### Step 6 — design doc updates

`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` §9.4 scheduler:

- Add a subsection "Globally-locked paths" listing the default set and globs.
- Document the override file (`docs/plans/_global_lock_paths.yaml`).
- State the serialization rule clearly: a global-lock task batches alone.

§9.4 parallel execution:

- Cross-reference §9.4; note that parallel-sibling isolation (TASK-003's state snapshots) remains the mechanism for non-global-lock tasks; the global-lock rule prevents parallel siblings from existing in the first place.

### Step 7 — TASK-007 audit check extension (if already landed)

Add to TASK-007's `CANONICAL_CONTRACT`:

```python
"global_lock_paths_default": sorted(GLOBAL_LOCK_PATHS),
"global_lock_globs_default": list(GLOBAL_LOCK_GLOBS),
```

Add a check `_check_global_lock_paths`:
- Greps `DUAL_AGENT_PLAN_EXECUTOR.md` §9.4 for the listed set; fails if docs drift from the constant.

If TASK-007 has not landed yet, this step is deferred to the TASK-007 implementation; no blocker.

### Step 8 — tests

`tests/scripts/test_plan_ops.py`:

- `test_list_global_lock_paths_default` — no override; emitted set matches constant.
- `test_list_global_lock_paths_override` — write a YAML; assert merged set.
- `test_is_global_lock_path_exact` — `requirements.txt` → True; `src/foo.py` → False.
- `test_is_global_lock_path_glob` — `.github/workflows/ci.yml` → True.
- `test_schedule_tags_global_lock` — craft a plan; assert tagged correctly.
- `test_batcher_serializes_global_lock_task` — 3 tasks, one global-lock; assert it batches alone.
- `test_batcher_serializes_two_global_lock_tasks` — 2 global-lock + 1 normal; assert no co-batch.
- `test_batcher_respects_existing_dependency_graph` — dep chain with a global-lock step; order preserved.

### Step 9 — regression sweep

Full suite green: `$PYTHON -m pytest -q`.

---

## Out of Scope

- **Runtime serialization** (e.g., OS-level locks across processes). This chunk is scheduler-only; once a task runs, the wrapper is already isolated per TASK-003.
- **Detecting transitive environment mutations** (e.g., a script that calls `pip install`). Scope stays on declared `allowed_files`.
- **Canonical contract:** TASK-001.
- **Validation:** TASK-002.
- **Wrapper isolation:** TASK-003.
- **Base scheduler:** TASK-004.
- **Phase gates:** TASK-005.
- **Fixture rewrite:** TASK-006.
- **Self-audit:** TASK-007 (TASK-010 extends the audit registry; full audit capability is TASK-007).
- **Portability / preflight:** TASK-008.
- **Scale-aware reads:** TASK-009.
- **Bounded log handling:** TASK-011.

## Reversion guidance

- **Default set too aggressive:** prefer narrowing a specific entry over disabling the serialization rule. For example, if `setup.py` proves noisy in a repo that treats it as a pure import target, remove it from the default and document; do not disable the rule for all entries.
- **Override YAML too invasive:** if operators prefer environment variables, add `IMPLEMENT_PLAN_GLOBAL_LOCK_ADDITIONAL=path1,path2` support as a secondary path; do not remove the YAML path.
- **Serialization too slow:** diagnose whether the plan has too many global-lock tasks (plan-level smell) before changing the rule. If the plan legitimately has many env edits, the serialization is correct — the plan should be chunked across multiple PRs.
