# TASK-020 — Plan-status integrity: lint + runtime V-check enforcement

**Base branch:** `main`
**Audit anchor commit:** `4b96f89`
**Chunk dependencies:** TASK-019 (establishes the TASK-002 V3/V4 backfill that motivates this; no code-level dependency, only conceptual).
**Motivating incident:** TASK-002 shipped with `**Status:** done` while acceptance-criteria V3 (cycle rejection in `parse-schedule`) and V4 (orphan-dep rejection) were empirically unmet. Root cause: (a) the status line was hand-edited rather than set by `plan_ops.py commit-task`, bypassing the reviewer gate entirely; (b) nothing re-runs a plan's own V-check snippets at commit time, so the implementer's self-report ("all V checks pass") was trusted without independent verification. TASK-020 closes both gaps.

---

## Goal

Make it structurally impossible to ship a plan marked `**Status:** done` without a matching reviewer-gated `commit-task` invocation AND (optionally, per plan) without the plan's declared acceptance V-checks passing against the committed code.

**Two mechanisms, landing as two subtasks:**

- **TASK-020A — `lint-plans` subcommand.** A read-only scan that cross-references every `**Status:** done` task in any plan under `docs/plans/**/*.md` against `_run_log.jsonl` (must have a `commit_done` event) and `git log` (must have a matching `feat(TASK-NNN)` commit). Hand-edited `done` markers flunk the lint. Available for manual use and for the follow-up CI gate in TASK-020C.
- **TASK-020B — `acceptance_v_check` runtime enforcement in `commit-task`.** Plans may optionally declare an executable `acceptance_v_check:` command in a YAML front-matter block. `commit-task` runs it immediately before the `git commit --only` call; non-zero exit halts with `acceptance-v-check-failed` before any plan mutation or git I/O. Backward-compat: plans without the field behave exactly as today.

TASK-020A is strictly additive (new subcommand, no behavior change to existing plans). It ships first. TASK-020B is a behavior change on `commit-task` but opt-in per plan; it ships independently after 020A is landed. The CI gate that wires `lint-plans` into PR checks is deferred to TASK-020C — which is gated on first triaging the baseline findings the subcommand will surface (running the subcommand against the current tree shows `done`/`partial` markers on several older plans that predate `commit-task`; a green gate requires either cleaning those up, scoping the gate to changed files only, or an allowlist — all three are TASK-020C's scope, not this plan's).

---

## Scoped Context

### Why two mechanisms, not one

The two mechanisms catch different failure modes:

| Failure mode | TASK-020A (lint) | TASK-020B (runtime V-check) |
|---|---|---|
| Status hand-edited to `done` without a `commit-task` run | **Catches** — no matching `commit_done` / `feat(...)` exists | Does not catch — lint gate is upstream of any V-check |
| `commit-task` ran, reviewer voted `ship`, but the V-check snippet fails against the committed code (i.e. the reviewer didn't run the V-checks) | Does not catch — lint only checks that a commit exists, not whether its behavior meets the plan | **Catches** — V-check runs pre-commit and halts `commit-task` before the status flip |
| Plan has no `acceptance_v_check:` field at all | N/A — both mechanisms pass | N/A — V-check enforcement is opt-in |

TASK-020A handles the **process** slip (hand-edited markers bypassing the reviewer gate). TASK-020B handles the **trust** slip (reviewer approving work that doesn't actually meet the plan's own bash-level V-check snippets). TASK-002 exhibited the first failure mode; any future plan where the implementer self-reports "all V checks pass" without actually running them would exhibit the second.

### Existing surfaces

- `plugins/plan-executor/scripts/plan_ops.py` — gains `cmd_lint_plans` (020A) and a pre-commit V-check hook inside `cmd_commit_task` (020B).
- `docs/plans/_run_log.jsonl` — authoritative source for `commit_done` events. Lint reads it; does not mutate it.
- `plugins/plan-executor/skills/implement-plan/SKILL.md` — §D.3 gets a one-line note that `commit-task` may run the plan's V-check pre-commit (020B); no behavior change to the orchestrator's dispatch path.
- `tests/scripts/test_plan_ops.py` — new test classes for both subcommands.

### Out-of-band state we must NOT touch

- `_run_log.jsonl` — read-only in lint. Mutating it to synthesize missing `commit_done` events would paper over real slips. The whole point is to fail loudly on the gap.
- Existing plan files marked `done` that may or may not have valid commit pairings — the lint will surface them as findings. The intent is to see the list, not to auto-fix. A separate housekeeping task can triage the findings once the lint exists.
- `.claude/settings.json` hooks — if the lint becomes a pre-commit hook, that belongs in a follow-up (TASK-021) — CI-only is enough for v1.

---

## Verification

### TASK-020A verification

**V1 — `lint-plans` passes on a clean plan tree.**

```bash
# Create a fixture plan tree with one task marked done, a matching commit_done
# event in a fixture _run_log.jsonl, and a matching feat(TASK-NNN) commit.
venv/bin/python plugins/plan-executor/scripts/plan_ops.py lint-plans \
  --plans-dir <fixture_dir> --run-log <fixture_log> --git-dir <fixture_repo> --json
```

Exit 0; `findings: []`; `scanned: 1`; `done_tasks: 1`.

**V2 — `lint-plans` flags hand-edited `done` without `commit_done` event.**

```python
def test_lint_plans_flags_missing_commit_done_event(tmp_path):
    # Plan: TASK-999, status=done. Run log: empty. Git: no matching commit.
    # Expect: exit 1, findings[*].code == "missing-commit-done-event"
    # Finding names task_id and plan_file.
```

**V3 — `lint-plans` flags `done` with `commit_done` event but no matching git commit.**

```python
def test_lint_plans_flags_missing_git_commit(tmp_path):
    # Plan: TASK-999, status=done. Run log: has commit_done for 999.
    # Git: no feat(TASK-999) commit.
    # Expect: exit 1, findings[*].code == "missing-feat-commit"
```

**V4 — `lint-plans` honors `superseded` / `failed` / pending statuses without flagging.**

```python
def test_lint_plans_ignores_non_done_statuses(tmp_path):
    # Plan: TASK-999, status=superseded. No commit, no event.
    # Expect: exit 0, findings: []
```

`done` and `partial` both trigger the commit-pairing check (`partial` implies some V-checks passed, some didn't — the same commit-pairing requirement applies to whatever DID land). `superseded`, `failed`, and any pending/in-progress status do NOT trigger the check.

**V5 — `lint-plans --json` emits structured findings.**

Schema:

```json
{
  "scanned": <int>,
  "done_tasks": <int>,
  "findings": [
    {
      "plan_file": "<relative path>",
      "task_id": "<NNN[A-Z]?>",
      "code": "missing-commit-done-event | missing-feat-commit",
      "message": "<human readable>"
    }
  ]
}
```

**V6 — (deferred to TASK-020C.)** The CI workflow that wires `lint-plans` into PR checks is TASK-020C's scope; see "Out of Scope" below. TASK-020A ships the subcommand for manual use.

### TASK-020B verification

**V7 — `acceptance_v_check` in YAML frontmatter is parsed.**

```python
def test_commit_task_parses_acceptance_v_check_frontmatter(tmp_path):
    # Plan has YAML frontmatter with acceptance_v_check: "venv/bin/pytest -q tests/..."
    # commit-task reads it without error.
```

**V8 — `acceptance_v_check` runs pre-commit and passing exits normally.**

```python
def test_commit_task_runs_v_check_success(tmp_path):
    # Plan declares acceptance_v_check that exits 0.
    # commit-task proceeds to commit; log-event "v_check_passed" appended.
```

**V9 — `acceptance_v_check` failure halts `commit-task` before git mutation.**

```python
def test_commit_task_halts_on_v_check_failure(tmp_path):
    # Plan declares acceptance_v_check that exits 1.
    # Expect: exit 1, errors[*].code == "acceptance-v-check-failed",
    # git log unchanged, plan status line NOT updated, no commit_done event.
```

This is the core invariant — failure MUST halt before any side-effect.

**V10 — Plan without `acceptance_v_check` frontmatter commits normally (backward-compat).**

```python
def test_commit_task_without_v_check_unchanged(tmp_path):
    # Existing plan layout with no YAML frontmatter. commit-task behaves
    # identically to pre-TASK-020B — no new log events, no new errors,
    # no new exit codes.
```

Critical: every existing plan must continue to work unchanged. Absence of frontmatter = no enforcement.

**V11 — V-check timeout enforced.**

```python
def test_commit_task_v_check_timeout(tmp_path):
    # Plan's acceptance_v_check is `sleep 600`. commit-task enforces a
    # default 300s timeout (configurable via --v-check-timeout SECONDS).
    # Expect: exit 1, errors[*].code == "v-check-timeout".
```

**V12 — V-check stdout/stderr captured in the error envelope.**

```python
def test_commit_task_v_check_captures_output(tmp_path):
    # Failing v-check outputs "FAIL: V3 cycle check"
    # Expect: errors[*].stdout_tail contains "FAIL: V3" so the orchestrator
    # can surface it to the user without re-running.
```

Tail is bounded (last 2KB of stdout + 2KB of stderr) to keep envelopes small.

**V13 — SKILL.md documents the opt-in field.**

```bash
grep -n 'acceptance_v_check' plugins/plan-executor/skills/implement-plan/SKILL.md
```

Must hit at least once in the §D.3 commit-task section.

---

## Tasks

### TASK-020A: `lint-plans` subcommand

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (one-line reference)
- **Dependencies:** none (additive)
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V1-V5 pass. (V6 is deferred to TASK-020C.)
  - `cmd_lint_plans` exists as a new subcommand in `plan_ops.py`. It is read-only (no file writes, no git mutation). Accepts `--plans-dir <abs>`, `--run-log <path>`, `--git-dir <path>`, `--json`.
  - Lint scans all `*.md` files under `--plans-dir` recursively. For each file, enumerates tasks via the existing `_split_task_blocks` helper. For each task block whose `**Status:**` line reads `done` or `partial`:
    - Asserts at least one `commit_done` event in `--run-log` has `task_id` equal to the canonical normalized task id.
    - Asserts at least one commit in `git log --oneline --all` matches `^[0-9a-f]+ feat\(TASK-<NNN[A-Z]?>\): ` (grep-level check; exact match not required).
    - Emits a finding per violation; codes: `missing-commit-done-event`, `missing-feat-commit`. (No `status-commit-mismatch` in v1 — the two codes above cover the described checks; if a future check needs a third code, add it in the task that introduces the check.)
  - Exit 1 iff `findings` is non-empty; exit 0 otherwise. `--json` always emits the structured envelope.
  - SKILL.md has a one-line pointer to the subcommand in §D.3 (informational; not required for any dispatch path).
- **Out of scope:**
  - CI workflow wiring (`.github/workflows/plan-status-lint.yml`) — deferred to TASK-020C. That task must first decide the baseline-handling policy: pre-clean existing findings (a historical sweep in its own right), scope the CI command to changed plan files only via a new `--files` flag, add a deletable allowlist, or accept that the gate only activates once the baseline is green. TASK-020A's job is to ship the detection primitive so the triage can begin.
  - Pre-commit hook integration (future TASK-021; CI-only is enough for v1 once TASK-020C lands).
  - Auto-fixing findings — the lint reports, humans triage.
  - Historical sweep / retroactive flagging of existing done markers — that's a one-time housekeeping task, not TASK-020A's job. The lint will surface the list; a follow-up can decide per-entry disposition.

**Description:**
Add a read-only `lint-plans` subcommand that flags any `**Status:** done` (or `partial`) task without a matching `commit_done` run-log event AND matching `feat(TASK-NNN)` git commit. No CI wiring in this task — CI is TASK-020C's scope.

- **Implementation notes:**
  - Re-use `_split_task_blocks` (`plan_ops.py:1008`) for per-task iteration and `_find_status_bullet` (`plan_ops.py:1025`) for status extraction. Re-use `_normalize_task_id` for id canonicalization.
  - The `feat(TASK-NNN)` grep should use `git log --all --grep "^feat(TASK-<id>)"` (or `--pretty=%s | grep`), not just `git log --grep TASK-NNN` (TASK-019's concern prose would falsely match).
  - Handle the `parent plan superseded` case gracefully: if a plan's top-level `**Status:** superseded` is present, skip the per-task done-pairing checks for that file. The parent plan's subtask decomposition is tracked in its own children; duplicating the gate at the parent level produces noise.
- **Reversion guidance:**
  Safe to revert. `lint-plans` is a new subcommand; removing it restores the pre-TASK-020A state (no lint, hand-edited markers can still slip). No schema changes, no runtime behavior changes.

### TASK-020B: `acceptance_v_check` runtime enforcement in `commit-task`

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `plugins/plan-executor/skills/implement-plan/SKILL.md`
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** TASK-020A (land first so the lint can observe the new `v_check_passed` / `v_check_failed` event kinds if needed; not a hard dependency).
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops.py`
- **Acceptance criteria:**
  - V7-V13 pass.
  - `cmd_commit_task` reads a YAML frontmatter block at the top of the plan file (delimited by `---` lines). If present AND it contains a key `acceptance_v_check: <command>`, the command is executed via `subprocess.run(..., shell=True, timeout=<default 300s or --v-check-timeout>)` with `cwd=<git-dir>` immediately before the `git commit --only` call.
  - Non-zero exit, timeout, or subprocess error halts `commit-task` with exit 1. Error envelope includes `errors[*].code ∈ {"acceptance-v-check-failed", "v-check-timeout", "v-check-subprocess-error"}`, `stdout_tail` (last 2048 bytes), `stderr_tail` (last 2048 bytes).
  - On V-check failure, NO plan mutation, NO git commit, NO run-log `commit_done` event. The V-check runs AFTER the `--files` staging guard but BEFORE the plan-status flip.
  - On V-check success, `plan_ops.py log-event --event v_check_passed --fields-json '{"task_id":"NNN","command":"..."}'` is appended, then the existing `commit-task` flow proceeds.
  - Plans without frontmatter, or with frontmatter that omits `acceptance_v_check`, behave IDENTICALLY to pre-TASK-020B. No new log events, no new fields in error envelopes, no new exit codes.
  - `ALLOWED_LOG_EVENTS` gains `v_check_passed` and `v_check_failed`. `cmd_commit_task` emits `v_check_passed` on success and `_die`s silently on failure (preserving V9's "no run-log mutation on failure" invariant). `v_check_failed` is reserved in `ALLOWED_LOG_EVENTS` for direct `plan_ops.py log-event --event v_check_failed` calls by external tooling (e.g., a future CI job that wants to record a V-check miss without going through `commit-task`); it is NOT emitted by `cmd_commit_task` in v1.
  - SKILL.md §D.3 gains a subsection describing the opt-in field, its semantics (halts pre-commit on failure), and a short YAML example.
- **Out of scope:**
  - Parallelizing or caching V-checks across tasks — run once per commit-task, no memoization.
  - Custom runner integration (Nix/Bazel/pytest-xdist) — the V-check is a shell command; if the plan needs more, it writes a wrapper script. Don't over-engineer.
  - Mandatory V-check for all plans — opt-in only. A future TASK-022 could flip the default to mandatory once enough plans carry the field to make it practical.

**Description:**
Let plans declare an `acceptance_v_check: <command>` field in YAML frontmatter. When present, `commit-task` runs it pre-commit and halts on non-zero exit, preventing the `done` flip from landing against code that doesn't meet the plan's acceptance criteria. Backward-compat: absence of the field = no enforcement.

- **Implementation notes:**
  - Use `re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)` to extract the frontmatter block. Parse via stdlib `yaml` if available OR use a minimal key-value regex — but we have `yaml` available (confirmed via TASK-013); prefer it.
  - The V-check runs with `shell=True`. This is intentional — plan authors declare shell commands like `venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestBatchNextBatchFidelity`. Document the shell-injection surface: plans must not be edited by untrusted parties without review (which is already the case).
  - Bounded tail: `stdout_tail = proc.stdout[-2048:]` likewise for stderr. Prevents runaway envelopes when a V-check produces many MB of output.
  - Timeout: default 300s, overridable via `--v-check-timeout SECONDS`. Hit the same wrapper pattern used by `plan_codex_dispatch.py`.
  - The V-check runs AFTER the `--files` staging guard (line ~2600 in `cmd_commit_task`, before the plan-status flip) and BEFORE any plan-file mutation. Ordering matters: staging guard catches unexpected writes first; V-check catches behavior misses second; only after both pass do we flip status and commit.
- **Reversion guidance:**
  Safe to revert. Removing the V-check branch from `cmd_commit_task` restores pre-TASK-020B behavior. The `ALLOWED_LOG_EVENTS` additions are additive; leaving them in after revert causes no harm. No plan file should be authored to DEPEND on the V-check running — it is a gate, not a runtime contract.

---

## Implementation Playbook

### Step 1 (TASK-020A) — `cmd_lint_plans` skeleton

Add to `plan_ops.py`:

```python
def cmd_lint_plans(args: argparse.Namespace) -> None:
    plans_dir = Path(args.plans_dir).resolve()
    run_log_path = Path(args.run_log).resolve() if args.run_log else None
    git_dir = Path(args.git_dir or ".").resolve()

    findings: list[dict] = []
    scanned = 0
    done_tasks = 0

    commit_done_ids = _load_commit_done_ids(run_log_path) if run_log_path else set()
    feat_commit_ids = _load_feat_commit_ids(git_dir)

    for md in sorted(plans_dir.rglob("*.md")):
        scanned += 1
        text = _load_text(md)
        # Skip parent plans marked superseded
        if re.search(r"^\*\*Status:\*\*\s*superseded\b", text, flags=re.MULTILINE | re.IGNORECASE):
            continue
        _, task_blocks = _split_task_blocks(text)
        for raw_id, block in task_blocks:
            status_m = _find_status_bullet(block)
            if not status_m:
                continue
            status = status_m.group("status").strip().lower()
            if status not in {"done", "partial"}:
                continue
            done_tasks += 1
            tid = _normalize_task_id(raw_id)
            if tid is None:
                continue
            rel_path = str(md.relative_to(plans_dir.parent))
            if tid not in commit_done_ids:
                findings.append({
                    "plan_file": rel_path,
                    "task_id": tid,
                    "code": "missing-commit-done-event",
                    "message": f"plan marks {tid!r} as {status!r} but no commit_done event found in run log",
                })
            if tid not in feat_commit_ids:
                findings.append({
                    "plan_file": rel_path,
                    "task_id": tid,
                    "code": "missing-feat-commit",
                    "message": f"plan marks {tid!r} as {status!r} but no 'feat(TASK-{tid}):' commit found",
                })

    result = {"scanned": scanned, "done_tasks": done_tasks, "findings": findings}
    _emit(args, result, exit_code=1 if findings else 0)
```

Helpers `_load_commit_done_ids` and `_load_feat_commit_ids`:

```python
def _load_commit_done_ids(run_log: Path) -> set[str]:
    ids: set[str] = set()
    if not run_log.exists():
        return ids
    for line in run_log.read_text().splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") == "commit_done":
            tid = _normalize_task_id(str(ev.get("task_id", "")))
            if tid:
                ids.add(tid)
    return ids

def _load_feat_commit_ids(git_dir: Path) -> set[str]:
    result = subprocess.run(
        ["git", "log", "--all", "--pretty=%s"],
        cwd=git_dir, capture_output=True, text=True, check=False,
    )
    ids: set[str] = set()
    for line in result.stdout.splitlines():
        m = re.match(r"^feat\(TASK-(\d{3}[A-Z]?)\)\b", line)
        if m:
            tid = _normalize_task_id(m.group(1))
            if tid:
                ids.add(tid)
    return ids
```

### Step 2 (TASK-020A) — Tests

Add a `TestLintPlans` class in `test_plan_ops.py`:

- `test_lint_plans_clean_tree` (V1)
- `test_lint_plans_flags_missing_commit_done_event` (V2)
- `test_lint_plans_flags_missing_git_commit` (V3)
- `test_lint_plans_ignores_non_done_statuses` (V4)
- `test_lint_plans_json_shape` (V5)
- `test_lint_plans_skips_superseded_parents`

Use `tmp_path` fixtures with synthetic plan text, a local git init + commit fixture, and a fixture `_run_log.jsonl`. Do NOT point tests at the real repo's `_run_log.jsonl` — that couples tests to live state.

### Step 3 (TASK-020A) — CI workflow (deferred)

The CI workflow that wires `lint-plans` into PR checks is **deferred to TASK-020C**. TASK-020A ships only the subcommand; running it is manual for now. TASK-020C must first decide how to handle the baseline findings (pre-clean, `--files` diff-only mode, allowlist, or hold the gate) before landing `.github/workflows/plan-status-lint.yml`.

### Step 4 (TASK-020A) — SKILL.md pointer

Add to §D.3 (end-of-section paragraph):

> **Lint reference:** `plan_ops.py lint-plans --plans-dir docs/plans --run-log <run_log> --git-dir . --json` cross-references every `**Status:** done` (or `partial`) task against the run log and git history. A `done`/`partial` marker without a matching `commit_done` event AND `feat(TASK-NNN)` commit flags the task as a hand-edit. Run manually during review or before shipping a plan; the PR-gate wiring is a follow-up (TASK-020C).

### Step 5 (TASK-020B) — YAML frontmatter parsing

In `cmd_commit_task`, after loading plan text and before the first mutation:

```python
def _parse_frontmatter(text: str) -> dict:
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not m:
        return {}
    try:
        import yaml
        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}
```

### Step 6 (TASK-020B) — V-check runner

```python
def _run_v_check(cmd: str, git_dir: Path, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=git_dir,
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "code": "v-check-timeout",
            "message": f"acceptance V-check exceeded {timeout}s",
            "stdout_tail": (e.stdout or "")[-2048:] if isinstance(e.stdout, str) else "",
            "stderr_tail": (e.stderr or "")[-2048:] if isinstance(e.stderr, str) else "",
        }
    except Exception as e:
        return {
            "code": "v-check-subprocess-error",
            "message": f"acceptance V-check failed to launch: {e}",
            "stdout_tail": "",
            "stderr_tail": "",
        }
    if proc.returncode != 0:
        return {
            "code": "acceptance-v-check-failed",
            "message": f"acceptance V-check exited with code {proc.returncode}",
            "stdout_tail": proc.stdout[-2048:],
            "stderr_tail": proc.stderr[-2048:],
        }
    return {"code": "v-check-passed", "stdout_tail": proc.stdout[-2048:]}
```

### Step 7 (TASK-020B) — Wire V-check into `cmd_commit_task`

Insert after the staging-guard block and before the plan-status flip:

```python
fm = _parse_frontmatter(plan_text)
v_check_cmd = fm.get("acceptance_v_check")
if v_check_cmd:
    timeout = getattr(args, "v_check_timeout", None) or 300
    v_result = _run_v_check(str(v_check_cmd), Path(args.git_dir or "."), timeout)
    if v_result.get("code") != "v-check-passed":
        _die(args, {
            "errors": [{
                "code": v_result["code"],
                "message": v_result["message"],
                "stdout_tail": v_result["stdout_tail"],
                "stderr_tail": v_result["stderr_tail"],
            }],
        })
    # Log pass event via the same append helper commit-task already uses.
    _append_run_log("v_check_passed", {
        "task_id": args.task_id,
        "run_id": args.run_id,
        "command": str(v_check_cmd),
    })
```

Add `--v-check-timeout SECONDS` to the subcommand's argparse (default: 300).

Add `v_check_passed` and `v_check_failed` to `ALLOWED_LOG_EVENTS`.

### Step 8 (TASK-020B) — Tests

Add `TestCommitTaskVCheck` class:

- `test_commit_task_parses_acceptance_v_check_frontmatter` (V7)
- `test_commit_task_runs_v_check_success` (V8)
- `test_commit_task_halts_on_v_check_failure` (V9)
- `test_commit_task_without_v_check_unchanged` (V10)
- `test_commit_task_v_check_timeout` (V11)
- `test_commit_task_v_check_captures_output` (V12)
- `test_commit_task_v_check_no_mutation_on_failure` — additional: after a failing V-check, assert plan text, run log, and git HEAD are all unchanged.

Use `tmp_path` + local git init + synthetic plan fixtures. Use a shell command like `exit 1` or `false` for the failing branch and `true` for the passing branch.

### Step 9 (TASK-020B) — SKILL.md update

Add to §D.3 after the existing `commit-task` documentation:

> **Opt-in V-check gate:** plans may declare `acceptance_v_check: <shell command>` in YAML frontmatter (delimited by `---` / `---` at the very top of the file). When present, `commit-task` runs the command with `cwd=<repo-root>` immediately before the git commit, bounded by `--v-check-timeout SECONDS` (default 300). Non-zero exit halts with `acceptance-v-check-failed` and leaves the plan, run log, and git HEAD unchanged. Example:
>
> ```yaml
> ---
> acceptance_v_check: venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestMyTask
> ---
> # TASK-NNN — ...
> ```
>
> Plans without frontmatter behave exactly as before.

### Step 10 — Regression sweep

After both 020A and 020B land, run:

```bash
venv/bin/pytest -q tests/scripts/test_plan_ops.py
venv/bin/python plugins/plan-executor/scripts/plan_ops.py lint-plans --plans-dir docs/plans --run-log docs/plans/_run_log.jsonl --git-dir . --json
```

Expected: test suite green; lint output lists whatever plan slips currently exist in the repo (TASK-002 being the known one; may be others). That findings list is the input to the follow-up housekeeping task — TASK-020 itself does not fix the findings, it only exposes them.

---

## Out of Scope

- **CI workflow wiring** (`.github/workflows/plan-status-lint.yml`) — deferred to a future **TASK-020C** that first picks a baseline-handling policy (pre-clean, `--files` diff-only scoping, allowlist, or hold the gate until baseline is green). Landing the workflow in TASK-020A would create a red CI on the very PR that lands it, because running `lint-plans` against the current tree surfaces pre-existing `done`/`partial` markers on older plans that predate `commit-task`.
- **Pre-commit hook integration** — a future TASK-021 can add the hook for local developer workflow once TASK-020C has landed the CI gate.
- **Auto-remediation of flagged tasks** — lint reports, humans triage. Different problems need different fixes (some are process slips requiring backfill plans; others may be legitimate `partial` states that need status correction).
- **Making `acceptance_v_check` mandatory** — opt-in for v1. Once enough plans carry the field to validate the pattern, a future TASK-022 can flip the default.
- **Reviewer-prompt updates** (prompt Codex/Claude to run V-checks during review) — separate concern, better handled inside TASK-018 or a dedicated reviewer-prompt task. The runtime V-check gate is upstream of the review and doesn't depend on reviewer diligence.
- **Retroactive status correction sweep** — TASK-019 flips TASK-002's status to `partial`; any additional slips surfaced by 020A's lint should be handled in a housekeeping task, not here.
- **Linter for design-contract drift** — that's TASK-007's scope (canonical contract vs. shipped artifacts). Orthogonal to plan-status integrity.

## Reversion guidance

- TASK-020A is strictly additive. Reverting removes the `lint-plans` subcommand; hand-edited markers become silently tolerated again. No CI workflow file exists in this task (deferred to TASK-020C), so revert is subcommand-only. Before reverting, consider whether the lint findings have been addressed — reverting while findings exist re-hides them.
- TASK-020B's V-check enforcement is opt-in per plan; reverting removes the enforcement branch from `cmd_commit_task` and the `ALLOWED_LOG_EVENTS` additions. Plans that carry `acceptance_v_check:` continue to be valid markdown; the field simply becomes a no-op. Safe to revert without touching plan files.
- **Never revert the status-partial fix on TASK-002** (that's TASK-019's change, not TASK-020's). These are separate concerns; reverting TASK-020 does not undo TASK-019.

## Execution log — 20260421T031650 (success)

Starting SHA: `459b6a8c9403d6bea0e2a86df93fc8c0c843f8ae`  → Ending SHA: `489995d2720a1c1efd10f675e7e121f48414b724`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 020A | claude | codex | disagreement-ship | bc9ea17 | Pre-existing impl on base commit 459b6a8; D.5=ship, backfill commit with [disagreement] tag. |
| 020B | claude | codex | disagreement-ship-with-fixes | 489995d | D.5 dismissed Finding 0 (no --git-dir arg); spec-deference on Finding 1 (matches commit_done pattern); Finding 2 surfaced as minor. |
