# TASK-018 — `acquire-lock` non-empty `--run-id` guard

**Parent plan:** TASK-004E_acquire_lock_strict.md (follow-up to a minor-but-important reviewer finding on ISSUE-018).

**Status:** complete
**Base branch:** main
**Scope:** Single-issue follow-up. Closes the write-path invariant gap left open by TASK-004E.

---

## Issue

**ISSUE-018-F1 (minor, severity=important) — `acquire-lock` writes a shape it would reject on next read when `--run-id` is empty.**

- **Location:** `plugins/plan-executor/scripts/plan_ops.py:3270-3311` (`cmd_acquire_lock`).
- **Flagged by:** Codex reviewer on commit `bb51c47` (TASK-004E). Verdict was `minor-findings` — one finding, severity `important`, at line 3284 (force path) and line 3309 (normal path).
- **Current behavior:** TASK-004E added `_validate_lock_shape()` which rejects entries whose `run_id` is `""` (`lock-entry-value-empty`). But the **write paths** in `cmd_acquire_lock` — both the `--force` branch at line 3284 and the merge-and-write branch at line 3309 — consume `args.run_id` verbatim. `argparse` with `required=True` accepts `--run-id ''` (the empty string is "present"), so the following invocation produces a non-canonical `_run_lock.json` that the next `acquire-lock` call would reject as malformed:
  ```bash
  venv/bin/python plan_ops.py acquire-lock --plan-file ABS --run-id '' --force
  # writes: {"<ABS>": {"run_id": "", "acquired_at": "<iso>"}}
  # next acquire-lock for any plan returns errors[0].code = "lock-entry-value-empty"
  ```
- **Defect:** the tool that enforces the canonical shape can produce non-canonical state. Self-inflicted corruption. Recovery requires `--force`, which `--force` itself triggered — ugly operator experience, and the only current path forward is manual deletion of the lock file.
- **Fix (spec):** reject `args.run_id` that is not a non-empty string at the top of `cmd_acquire_lock` (before any read / write), emitting the same structured `errors[]` envelope used elsewhere. Apply **before** the `--force` branch so force cannot bypass the check. `argparse` is not the right enforcement layer: changing the arg type to a custom validator would spread the validation logic across two files (argparse setup + function body), whereas the function-body guard co-locates it with the shape validator it complements.

### Symmetry note — why `release-lock` is NOT in scope

`cmd_release_lock` (line 3314) also takes `--run-id`, but it never **writes** a new entry — it only deletes the entry whose key-plus-run_id matches. An empty `--run-id` to `release-lock` falls through to `"run-id-mismatch"` and emits `released=False` with no mutation. No corruption is possible. TASK-004E deliberately left `release-lock` alone (asymmetric by design); this follow-up keeps that posture.

### Symmetry note — why `--plan-file` is NOT in scope

The Codex finding is specific to `--run-id`. `os.path.abspath("")` returns the cwd, which is a legitimate path — not a shape violation — and this follow-up stays narrowly scoped to the flagged invariant. If an empty-`--plan-file` guard is judged load-bearing later, it is an additive change.

---

## Acceptance criteria

1. **Empty `--run-id` is rejected at the top of `cmd_acquire_lock`, before any file read or write.** The check MUST fire **before** the `--force` branch so force cannot bypass it. Rejection uses `_die(args, {"acquired": False, "errors": [{"code": "lock-run-id-empty", "message": "..."}]})`. **(See VA, VB.)**
2. **Rejection criterion is symmetric with the shape validator.** `not isinstance(args.run_id, str) or args.run_id == ""`. This matches `_validate_lock_shape()`'s check for existing `run_id` values (`lock-entry-value-empty`). Using the SAME criterion keeps the invariant consistent between write-side and read-side. Error `code` differs (`lock-run-id-empty` vs `lock-entry-value-empty`) so the call site is unambiguous in the structured output. **(See VA, VB, VD.)**
3. **No lock-file mutation on rejection.** When `--run-id` is empty, the lock file on disk MUST be byte-equal before and after the rejected call — both on the force path and the non-force path. **(See VC.)**
4. **Happy path unchanged.** Non-empty `--run-id` produces exactly the behavior inherited from TASK-004E — canonical merge on the normal path, single-entry overwrite on `--force`. The new guard does NOT change any output envelope for a valid call. Existing V1–V20 tests continue to pass unchanged. **(See VE.)**
5. **CLI surface unchanged.** No new flags. No changes to argparse declarations for `--plan-file`, `--run-id`, `--force`, `--json`. The guard is a function-body check.
6. **`release-lock` untouched.** Preserve the asymmetry from TASK-004E acceptance #9. Do NOT add a non-empty guard to `cmd_release_lock`. **(See §Out of scope.)**
7. **SKILL.md update is additive, not restructuring.** Extend the existing Phase 0 paragraph added by TASK-004E with one sentence noting that `--run-id` must be non-empty — do NOT add a new section, do NOT rewrite the paragraph. One sentence, in-line.
8. All verification checks VA–VE pass.

---

## Out of scope — do NOT touch

- `cmd_release_lock` — intentional asymmetry; see acceptance #6.
- `--plan-file` non-empty enforcement — not the flagged invariant; see Symmetry note above.
- Any change to `_validate_lock_shape` — the read-side validator is correct; this is a write-side gap.
- Any change to `_atomic_write_json` — unaffected by the guard.
- Argparse-level `type=` validators — we enforce at the function body to keep validation co-located with the rest of the shape logic.
- Whitespace-only `--run-id` rejection (e.g., `'   '`). The canonical shape accepts any non-empty string; adding whitespace-specific rules is scope creep. If a future change wants to require ISO-8601-like run IDs, it is an additive refinement.

---

## Verification

**VA — Empty `--run-id` is rejected on the non-force path.**

```python
def test_acquire_lock_rejects_empty_run_id_nonforce(isolated_plan, monkeypatch):
    # RUN_LOCK_PATH does not exist initially.
    # acquire-lock --plan-file ABS --run-id '' --json (no --force) MUST exit non-zero.
    # Output JSON: {"acquired": False, "errors": [{"code": "lock-run-id-empty", ...}]}
    # After: RUN_LOCK_PATH still does NOT exist (no file created).
```

**VB — Empty `--run-id` is rejected on the force path.**

```python
def test_acquire_lock_rejects_empty_run_id_force(isolated_plan, monkeypatch):
    # RUN_LOCK_PATH does not exist initially.
    # acquire-lock --plan-file ABS --run-id '' --force --json MUST exit non-zero.
    # Output JSON: {"acquired": False, "errors": [{"code": "lock-run-id-empty", ...}]}
    # After: RUN_LOCK_PATH still does NOT exist. --force did NOT bypass the guard.
```

**VC — Empty `--run-id` does not mutate a pre-existing canonical lock file.**

```python
def test_acquire_lock_empty_run_id_preserves_existing_file(isolated_plan, monkeypatch):
    # Pre-write canonical: {"<OTHER_PLAN>": {"run_id": "R_other", "acquired_at": "T"}}
    # Snapshot RUN_LOCK_PATH.read_bytes() before.
    # acquire-lock --plan-file ABS --run-id '' (force AND non-force) both exit non-zero.
    # After: RUN_LOCK_PATH.read_bytes() is byte-equal to the snapshot in BOTH cases.
```

**VD — Non-string `--run-id` is rejected with the same error code.**

```python
def test_acquire_lock_rejects_non_string_run_id(isolated_plan, monkeypatch):
    # Simulate argparse passing a non-string (e.g., direct cmd_acquire_lock call with
    # args.run_id=None or 0). MUST exit non-zero with errors[0].code == "lock-run-id-empty".
    # Rationale: the guard uses `not isinstance(x, str) or x == ""` symmetric with
    # the read-side validator, so non-string values collapse to the same error code.
    # In practice argparse cannot produce non-string from a required= flag; this test
    # exists to lock in the isinstance check and prevent accidental regression to
    # a naked `if not args.run_id:` (which would mis-classify None/0 vs "").
```

**VE — Non-empty `--run-id` happy path regressions.**

```python
def test_acquire_lock_nonempty_run_id_happy_paths(isolated_plan, monkeypatch):
    # Three sub-cases, each with a non-empty run_id:
    #  1. No pre-existing file, normal path: {"acquired": True} and canonical entry written.
    #  2. No pre-existing file, --force path: {"acquired": True, "forced": True} and
    #     canonical entry written.
    #  3. Canonical pre-existing with OTHER plan entry, normal path: merges as before.
    # All three MUST exit 0 and match the exact behavior TASK-004E tested at V1/V7/V8.
    # This is the regression-guard for acceptance #4.
```

---

## Tasks

### TASK-018: Guard non-empty `--run-id` at acquire-lock write entry

- **Status:** done
- **Priority:** low
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (add guard at the top of `cmd_acquire_lock`, before the `--force` branch)
  - `tests/scripts/test_plan_ops.py` (extend `TestLock` class with VA–VE)
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (add one sentence to the TASK-004E paragraph about `--run-id` non-emptiness)
- **Dependencies:** TASK-004E
- **Test command:**
  ```bash
  venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestLock
  ```
  Single-leg — no integration regression guard required here because the wrapper never invokes `acquire-lock` (verified under TASK-004E).
- **Acceptance criteria:** all 8 above (VA–VE).
- **Out of scope:** see top-of-plan list.

**Description:**
Close the write-path invariant gap left open by TASK-004E: reject empty / non-string `--run-id` at the top of `cmd_acquire_lock` with the same `_die({errors[]})` envelope used elsewhere, before the `--force` branch. Keep the criterion symmetric with `_validate_lock_shape()`'s existing read-side check.

**Implementation sketch:**

```python
def cmd_acquire_lock(args: argparse.Namespace) -> None:
    if not isinstance(args.run_id, str) or args.run_id == "":
        _die(args, {
            "acquired": False,
            "errors": [{
                "code": "lock-run-id-empty",
                "message": "--run-id must be a non-empty string",
            }],
        })
    plan_abs = os.path.abspath(args.plan_file)
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    # ... rest unchanged (decode / force / shape-validate / merge / write)
```

Note the guard fires **before** `RUN_LOCK_PATH.parent.mkdir`. A rejected call must not create the lock directory as a side effect — it is a pure input-validation reject.

**Forbidden patterns:**

- `if not args.run_id:` — truthy check would mis-classify `None` / `0` / `False` with the same code as `""`. The plan mandates isinstance+equality explicitly (see VD rationale).
- Adding a custom `type=` validator in argparse — the invariant lives in the function body next to `_validate_lock_shape()` so both sides of the contract stay visible in one file region.
- Putting the guard inside or after the `--force` branch — force MUST NOT bypass the guard.
- Adding a matching guard to `cmd_release_lock` — asymmetric by design; see acceptance #6.

**Test scaffolding notes:**

- Extend the existing `TestLock` class in `tests/scripts/test_plan_ops.py` (pattern established by TASK-004E). Do NOT create a parallel test class.
- For VC "byte-equal before/after" assertions: use `RUN_LOCK_PATH.read_bytes()`, compare exact equality, same pattern as TASK-004E V2/V3/V6.
- For VD "non-string" simulation: construct an `argparse.Namespace` manually and call `cmd_acquire_lock(ns)` directly, same pattern as TASK-004E V12's in-process `os.replace` test. Subprocess invocation cannot reach the non-string branch because argparse coerces.

**SKILL.md update (surgical):**

The TASK-004E paragraph in Phase 0 currently ends:

> ... `--force` discards all existing entries (including entries for other plans), so do not run it while a legitimate run is in progress.

Append ONE sentence to the same paragraph (not a new paragraph, not a new section):

> `--run-id` must be a non-empty string; empty or non-string values are rejected before any file operation so a botched invocation cannot corrupt the lock file.

**Rollout:**

- Additive guard only — the happy path (non-empty `--run-id`) is unchanged, so no caller in the orchestrator (`SKILL.md` Phase 0, `commit-task`, `fail-task` — none of which invoke `acquire-lock` except via `SKILL.md`'s documented command) is affected.
- The orchestrator already produces run IDs from `_now()`-derived strings that are non-empty by construction, so the guard is a safety net against manual / out-of-orchestrator invocation, not a behavior change for normal runs.
- No schema change. No migration. The guard is input validation, not lock-file format.

## Execution log — 20260419T104003 (success)

Starting SHA: `29ae2cebb09d6b6304d778a271af6f50ce7e6e5a`  → Ending SHA: `e8ed74518c83c3f3642f3ebbda833fc2e22c42be`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-018 | claude | codex | clean | e8ed7451 | acceptance_met=true |
