# TASK-004E — `acquire-lock` strict-shape rejection + `--force`

**Parent plan:** TASK-004_scheduler_semantics.md (decomposed into A/B/C/D/E sub-plans after the bundled TASK-004 failed D.2a review twice due to scope/blast-radius issues).

**Status:** pending
**Base branch:** main
**Scope:** Single-issue sub-plan. Addresses **ISSUE-018** ONLY.

**Siblings (do not touch here):**
- TASK-004A: `filter-schedule` subcommand + DAG defensive check (ISSUE-006/020).
- TASK-004B: `batch-next` batch-fidelity + cross-batch deadlock (ISSUE-010).
- TASK-004C: `fail-task` tracked + untracked cleanup partition (ISSUE-011).
- TASK-004D: `block-dependents` plan-markdown mutation (ISSUE-012).

---

## Issue

**ISSUE-018 (P2) — `acquire-lock` tolerates orphan-shape JSON.**

- **Location:** `plugins/plan-executor/scripts/plan_ops.py:1453-1469` (`cmd_acquire_lock`).
- **Current behavior:**
  ```python
  current: dict = {}
  if RUN_LOCK_PATH.exists():
      try:
          current = json.loads(RUN_LOCK_PATH.read_text(encoding="utf-8"))
      except json.JSONDecodeError:
          current = {}                                                  # silently wipes
  if plan_abs in current and current[plan_abs].get("run_id") != args.run_id:
      _die(args, {"acquired": False, "conflict_run_id": current[plan_abs].get("run_id")})
  current[plan_abs] = {"run_id": args.run_id, "acquired_at": _now()}
  RUN_LOCK_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
  ```
  Only rejects when `plan_abs in current` has a mismatching `run_id`. Orphan-shape JSON (e.g., `{"pid": 99999}`, `{"lock_owner": "stale"}`, an integer, a list) passes through because the `plan_abs in current` check silently coerces to `False` on wrong-typed values or merges a dict with unrelated keys.
- **Defect:** a crashed prior run leaving a malformed lock does not block a new run. No liveness check, no age TTL, and — critically — no structural validation. Preflight (`ISSUE-008`) classifies `_run_lock.json` as `infra_ignored`, so that path does not backstop this.
- **Fix (spec):** reject any pre-existing JSON that does not conform to the canonical shape:
  ```json
  {
    "<absolute-plan-path>": {"run_id": "<string>", "acquired_at": "<opaque-non-empty-string>"},
    ...
  }
  ```
  Add `--force` flag for explicit override — overwrites the lock file with a single-entry dict for this plan/run. Document the lock-file shape in `plugins/plan-executor/skills/implement-plan/SKILL.md`.

### `acquired_at` validation level (decision)

**Decision: shape-only validation.** `acquired_at` is treated as an opaque non-empty string. `acquire-lock` does NOT parse, normalize, or verify ISO-8601 formatting. The writer emits `_now()` (already ISO-8601 by construction), so well-formed lock files will in practice contain ISO-8601 strings, but the validator rejects only non-string or empty-string values. This keeps the validation contract simple and matches the `_validate_lock_shape()` sketch. If a future change wants to require parseable timestamps, it is an additive refinement — not in scope here.

Unicode in `run_id` / `acquired_at` is ACCEPTED. `isinstance(x, str)` passes regardless of encoding, and there is no semantic reason to restrict code points. Do not add Unicode filters.

### Interaction with existing integration test (scope clarification)

The integration test at `tests/scripts/test_plan_codex_dispatch_integration.py:406-424` (`test_parallel_preserves_run_lock_json`) pre-populates `docs/plans/_run_lock.json` with an orphan-shape body and asserts that it survives byte-equal after two parallel `plan_codex_dispatch.py implement` runs. That test exercises the **wrapper's** delta-bounded cleanup + protected-paths policy — it never invokes `acquire-lock`. The wrapper has no dependency on `plan_ops.py` at runtime (verified: the only reference is the protected-path string `"plugins/plan-executor/scripts/plan_ops.py"` at line 62). Tightening `acquire-lock` therefore does NOT change this test's behavior, and this test must continue to pass unchanged.

The broader operational concern — "what happens when the orchestrator encounters a pre-existing orphan-shape lock?" — is covered by the new `--force` escape hatch (acceptance #4) and by the SKILL.md note (acceptance #11). No in-the-wild orphan-shape locks can exist from legitimate callers because no legitimate writer emits anything other than the canonical shape; they would only exist as the result of manual file edits or a prior-version release that has since been replaced. `--force` is the documented recovery path.

---

## Background — why this landed in its own sub-plan

The parent TASK-004 bundled 7 ISSUE-### (006, 010, 011, 012, 018 + 019/020 secondary) into one 600+-line diff. Two D.2a review rounds flagged cousin-bug regressions that only became visible because the reviewer had to hold all 7 fixes in context simultaneously. `acquire-lock` is P2 (not P1 like the others) and strictly concerns lock-file I/O; it belongs in its own sub-plan so the validation contract and the `--force` escape hatch can be reviewed in isolation.

---

## Acceptance criteria

1. **Canonical shape is the ONLY valid shape.** The lock JSON at `docs/plans/_run_lock.json` MUST be a JSON object (top-level dict). Empty object `{}` IS canonical (zero entries is valid). Every key MUST be a non-empty string (treated as an absolute plan path by convention; no path-existence check — locks are opaque handles). Every value MUST be a dict with EXACTLY the keys `{"run_id": str, "acquired_at": str}` — no extra keys, no missing keys. Both values non-empty strings; Unicode accepted. `acquired_at` is validated as an opaque non-empty string (no ISO-8601 parse). **(See V1, V14.)**
2. **Malformed pre-existing JSON blocks acquisition (no `--force`).** If `_run_lock.json` exists but violates the canonical shape (top-level not a dict; any entry value not a dict; any entry missing `run_id` or `acquired_at`; any entry containing extra keys; any entry with non-string or empty `run_id` / `acquired_at`; empty-string top-level key), `acquire-lock` MUST exit non-zero with a structured error that (a) identifies the specific shape violation and (b) does NOT truncate or modify the lock file on disk. Value-level rejection is symmetric across `run_id` and `acquired_at`: both use `isinstance(x, str) and x != ""`. **(See V2, V3, V4, V5, V15, V15b, V16, V16b, V17, V17b, V18.)**
3. **JSON-decode error blocks acquisition (no `--force`).** If `_run_lock.json` exists but is not valid JSON (invalid tokens, truncated) OR is a zero-byte file (which `json.loads("")` raises `JSONDecodeError` for), `acquire-lock` MUST exit non-zero with a structured error. It MUST NOT silently treat the file as `{}` and overwrite it — that is the current behavior at `plugins/plan-executor/scripts/plan_ops.py:1460-1461`. **(See V6, V19.)**
4. **`--force` overwrites atomically.** When `--force` is passed, `acquire-lock` MUST overwrite `_run_lock.json` with a single-entry dict `{<plan_abs>: {"run_id": <args.run_id>, "acquired_at": <now>}}` — explicitly NOT merged with prior (malformed or valid) content. **Prior entries for other plans are LOST under `--force`**; this is the documented behavior because `--force` exists precisely to recover from corruption, and merging with corrupt data is the hazard it is meant to escape. **Concurrency consequence (explicit):** two concurrent `--force` acquires for the same plan are last-writer-wins and can obliterate each other's entry. This is acceptable because `--force` is a manual recovery tool, not a normal code path; concurrent forced recovery is operator error. **(See V7, V20.)**
5. **Standard happy-path preserved.** When the file does not exist, OR when the file is canonical-shape AND has no entry for this `plan_abs`, acquire the lock by merging (canonical add) into the existing canonical dict. No new keys outside `run_id` / `acquired_at` are written. Output `{"acquired": true}` on stdout. **(See V1, V8, V14.)**
6. **Same-run re-acquire is idempotent.** When the file is canonical-shape AND `plan_abs` is present AND `run_id` matches `args.run_id`, re-acquisition is a no-op (rewrite the same entry with a fresh `acquired_at` OK; `acquired=True`; exit 0). This matches the current behavior — do not regress. **(See V10.)**
7. **Cross-plan entries untouched in non-force path.** When a valid canonical dict already exists with entries for OTHER plans, acquiring a lock for THIS plan must not drop the other entries. **Semantic equality** on the other-plan sub-object (NOT byte-equal — the file is fully re-serialized, so JSON whitespace/key-order may differ). **(See V11.)**
8. **Atomic write.** `_run_lock.json` MUST be written atomically (unique temp file in the same directory + `os.replace`) so a crash mid-write cannot leave a truncated or partially-updated file visible at the canonical path. Use `tempfile.mkstemp(dir=parent, prefix=path.name + ".", suffix=".tmp")` (or `NamedTemporaryFile(delete=False)` with the same dir/prefix) to avoid two concurrent acquires from sharing a fixed `.tmp` name. V12 scope is torn-write prevention **only** — no `fsync`/directory-`fsync` durability guarantees are required. **(See V12.)**
9. **`release-lock` unchanged.** This sub-plan does NOT modify `cmd_release_lock`. The asymmetry (strict acquire, tolerant release) is intentional: release-lock's only job is to best-effort remove this run's own entry. The existing `cmd_release_lock` at `plugins/plan-executor/scripts/plan_ops.py:1476-1479` coerces `JSONDecodeError` to `{}`, but it is NOT fully tolerant of valid-JSON-of-wrong-shape (e.g., a top-level list will raise `AttributeError` on `.get()`). Leaving it alone here is deliberate: a stricter release path could strand legitimate locks when the orchestrator tries to clean up, and the release-side crash modes will be addressed separately if they become load-bearing. Do not add `--force` to release-lock.
10. **CLI surface.** Add `--force` to the `acquire-lock` subparser. No changes to `--plan-file`, `--run-id`, `--json`. **(See V7, V13.)**
11. **`SKILL.md` update.** Document the canonical lock-file shape and the `--force` escape hatch in `plugins/plan-executor/skills/implement-plan/SKILL.md` (Pre-flight Phase 0 paragraph, which already mentions the lock). One paragraph, not a new section. The paragraph MUST label `--force` as a manual recovery tool, not a normal flag.
12. **Concurrent-writer exclusion is out of scope.** Atomic replace prevents torn writes (partial file content visible at the canonical path). It does NOT prevent a TOCTOU race where two concurrent `acquire-lock` processes both read "no entry for this plan," both write, and the second wins. Adding a file lock (`fcntl.flock` / `msvcrt.locking`) is a deliberate non-goal of this sub-plan — the higher-level orchestrator acquires the lock once per run, not concurrently. If concurrent-writer exclusion becomes necessary later, it is an additive change.
13. **Garbage collection of stale entries out of scope.** Canonical entries for plans whose files have been deleted still block acquisition until manual `--force`. No TTL, no path-existence probe, no auto-release.
14. **Duplicate JSON keys out of scope.** Python `json.loads()` last-key-wins duplicate top-level keys before this plan's validator runs. Duplicate-key detection at the raw-text layer is NOT a requirement. If this becomes load-bearing it is an additive change.
15. All verification checks V1–V20 pass.

---

## Out of scope — do NOT touch

- `cmd_fail_task` (TASK-004C).
- `cmd_batch_next` (TASK-004B).
- `cmd_block_dependents` (TASK-004D).
- `cmd_filter_schedule` (TASK-004A).
- `cmd_release_lock` — intentional asymmetry; see acceptance #9.
- Any PID liveness check (`os.kill(pid, 0)`, `/proc/<pid>` probes). Locks are plan-scoped, not process-scoped, and the orchestrator runs across machines/containers.
- Any age-based TTL auto-release. The parent plan's ISSUE-018 explicitly considered and rejected this — `--force` is the escape hatch, not TTL expiry.
- Changing the file path of `_run_lock.json`.
- Changing the lock-file format (keep JSON, do not switch to flock / sqlite / etc.).

---

## Verification

**V1 — Canonical happy path.**

```python
def test_acquire_lock_canonical_happy_path(tmp_path, isolated_plan, monkeypatch):
    # RUN_LOCK_PATH does not exist initially.
    # acquire-lock --plan-file ABS --run-id R1 --json -> exits 0, {"acquired": True}.
    # After: RUN_LOCK_PATH contains exactly:
    #   {"<ABS>": {"run_id": "R1", "acquired_at": "<ISO string>"}}
    # No other keys at top level. No other keys inside entry dict.
```

**V2 — Top-level JSON is a list, not a dict.**

```python
def test_acquire_lock_rejects_toplevel_list(isolated_plan, monkeypatch):
    # Pre-write RUN_LOCK_PATH with contents: `["oops"]`
    # acquire-lock without --force MUST exit non-zero.
    # stderr / stdout JSON MUST indicate "malformed lock file" with a shape-violation
    # code (e.g., errors[0].code == "lock-toplevel-not-object" or similar).
    # Assert: file on disk is UNCHANGED (still `["oops"]` verbatim).
```

**V3 — Entry value is not a dict.**

```python
def test_acquire_lock_rejects_entry_value_not_object(isolated_plan, monkeypatch):
    # Pre-write: {"<OTHER_PLAN_ABS>": "a string not a dict"}
    # acquire-lock (this plan) MUST exit non-zero, file unchanged.
    # Rationale: even entries for OTHER plans violating the shape must block
    # acquisition, because the file is malformed and merging would preserve
    # the corruption.
```

**V4 — Entry missing required key.**

```python
def test_acquire_lock_rejects_entry_missing_acquired_at(isolated_plan, monkeypatch):
    # Pre-write: {"<OTHER_PLAN_ABS>": {"run_id": "R_old"}}  # missing acquired_at
    # acquire-lock MUST exit non-zero, file unchanged.
```

**V5 — Entry has extra keys.**

```python
def test_acquire_lock_rejects_entry_with_extra_keys(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": "R", "acquired_at": "T", "pid": 999}}
    # acquire-lock MUST exit non-zero with shape-violation, file unchanged.
    # Rationale: extra keys are the "orphan-shape" class of bug from ISSUE-018.
```

**V6 — Invalid JSON (not silently wiped).**

```python
def test_acquire_lock_rejects_invalid_json(isolated_plan, monkeypatch):
    # Pre-write raw text: "not-json{"  (invalid JSON)
    # acquire-lock WITHOUT --force MUST exit non-zero.
    # File on disk unchanged.
    # This locks in the REGRESSION: current implementation at plan_ops.py:1460-1461
    # catches JSONDecodeError and silently treats as {}, which loses visibility
    # into corruption.
```

**V7 — `--force` overwrites.**

```python
def test_acquire_lock_force_overwrites_malformed(isolated_plan, monkeypatch):
    # Pre-write: `["malformed"]` (list, not dict).
    # acquire-lock --force --plan-file ABS --run-id R_new exits 0.
    # After: RUN_LOCK_PATH contains exactly:
    #   {"<ABS>": {"run_id": "R_new", "acquired_at": "<ISO>"}}
    # The prior list content is GONE. Assert output JSON has
    # `{"acquired": True, "forced": True}` (new key `forced` surfaces the fact).
```

**V8 — Canonical-shape with other-plan entry, no conflict for this plan.**

```python
def test_acquire_lock_merges_into_canonical_other_plan_entry(
        tmp_path, isolated_plan, monkeypatch):
    other_abs = "/abs/path/to/other/plan.md"
    pre = {other_abs: {"run_id": "R_other", "acquired_at": "2026-01-01T00:00:00Z"}}
    # Pre-write pre as RUN_LOCK_PATH content (canonical shape, just a different plan).
    # acquire-lock --plan-file <isolated_plan> --run-id R_new exits 0.
    # After: RUN_LOCK_PATH contains BOTH entries (PARSED-JSON equality on each entry).
    #   - other_abs entry == pre[other_abs]   (dict equality on parsed JSON,
    #                                          NOT byte-level — the file is
    #                                          fully re-serialized)
    #   - isolated_plan entry == {"run_id": "R_new", "acquired_at": <non-empty str>}
```

**V9 — Canonical-shape, conflict for THIS plan (different run_id).**

```python
def test_acquire_lock_refuses_conflicting_run_id(isolated_plan, monkeypatch):
    # Pre-write canonical-shape with this plan already owned by R1.
    # acquire-lock --run-id R2 MUST exit non-zero with conflict_run_id=R1.
    # (Preserves current behavior at plan_ops.py:1462-1466.)
    # File on disk unchanged.
```

**V10 — Same run_id re-acquire is idempotent.**

```python
def test_acquire_lock_same_run_id_is_idempotent(isolated_plan, monkeypatch):
    # Pre-write canonical-shape: this plan owned by R1.
    # acquire-lock --run-id R1 exits 0, acquired=True.
    # File still contains only this plan's entry with run_id=R1.
    # acquired_at MAY be refreshed (current impl does); allowed either way.
```

**V11 — Cross-plan entries untouched in non-force path.**

```python
def test_acquire_lock_preserves_unrelated_canonical_entries(
        isolated_plan, monkeypatch):
    # Pre-write canonical with three other-plan entries.
    # acquire-lock --run-id R_new for isolated_plan.
    # Assert: exactly FOUR top-level keys after (the three pre-existing + this one).
    # None of the pre-existing entries mutated.
```

**V12 — Atomic write (torn-write prevention).**

```python
def test_acquire_lock_writes_atomically(isolated_plan, monkeypatch):
    # Patch os.replace to raise OSError mid-rename.
    # Pre-condition: canonical lock with one entry (capture bytes up front).
    # acquire-lock --run-id R_new raises the OSError (let it propagate or wrap).
    # After assertions:
    #   1. RUN_LOCK_PATH byte-equal to the pre-state (no torn write visible at the
    #      canonical path).
    #   2. No stray *.tmp file left in the parent directory matching the
    #      mkstemp-style prefix `_run_lock.json.*.tmp`. Implementation must clean
    #      up the temp file when replace fails.
    # Scope note: this test only asserts torn-write prevention. Durability after
    # power loss (fsync of the file or parent directory) is explicitly NOT tested
    # and NOT required — see acceptance #8.
```

**V13 — `--force` with no pre-existing file is still canonical.**

```python
def test_acquire_lock_force_on_missing_file(isolated_plan, monkeypatch):
    # RUN_LOCK_PATH does not exist.
    # acquire-lock --force --run-id R_new exits 0. `forced=True`, `acquired=True`.
    # File content same as V1 — canonical single-entry dict.
```

**V14 — Empty-dict canonical pre-existing file is accepted.**

```python
def test_acquire_lock_accepts_empty_dict(isolated_plan, monkeypatch):
    # Pre-write: "{}"  (zero-entry canonical dict; isinstance(raw, dict) true,
    # no entries to violate shape).
    # acquire-lock (no --force) exits 0, acquired=True.
    # After: RUN_LOCK_PATH has ONE entry for this plan.
```

**V15 — Entry with `run_id = None` is rejected.**

```python
def test_acquire_lock_rejects_none_run_id(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": None, "acquired_at": "T"}}
    # acquire-lock (no --force) MUST exit non-zero with
    # errors containing code="lock-entry-value-empty" (or equivalent).
    # File on disk unchanged.
```

**V16 — Entry with `run_id = 0` is rejected.**

```python
def test_acquire_lock_rejects_zero_run_id(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": 0, "acquired_at": "T"}}
    # Integer (not str) must be rejected under the non-empty-string contract.
    # acquire-lock (no --force) MUST exit non-zero; file unchanged.
```

**V17 — Entry with empty-string `run_id` is rejected.**

```python
def test_acquire_lock_rejects_empty_string_run_id(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": "", "acquired_at": "T"}}
    # Empty string fails the "non-empty" validator.
    # acquire-lock (no --force) MUST exit non-zero; file unchanged.
```

**V15b — Entry with `acquired_at = None` is rejected.**

```python
def test_acquire_lock_rejects_none_acquired_at(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": "R", "acquired_at": None}}
    # Symmetric with V15 — acquired_at is validated with the same
    # non-empty-string rule as run_id.
    # acquire-lock (no --force) MUST exit non-zero; file unchanged.
```

**V16b — Entry with `acquired_at = 0` is rejected.**

```python
def test_acquire_lock_rejects_zero_acquired_at(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": "R", "acquired_at": 0}}
    # Integer fails isinstance(x, str); rejection required.
    # acquire-lock (no --force) MUST exit non-zero; file unchanged.
```

**V17b — Entry with empty-string `acquired_at` is rejected.**

```python
def test_acquire_lock_rejects_empty_string_acquired_at(isolated_plan, monkeypatch):
    # Pre-write: {"<PLAN_ABS>": {"run_id": "R", "acquired_at": ""}}
    # Empty string fails the "non-empty" validator.
    # acquire-lock (no --force) MUST exit non-zero; file unchanged.
```

**V18 — Empty-string top-level key is rejected.**

```python
def test_acquire_lock_rejects_empty_string_plan_key(isolated_plan, monkeypatch):
    # Pre-write: {"": {"run_id": "R", "acquired_at": "T"}}
    # Top-level key "" fails the "non-empty string" validator at the key level
    # (code="lock-key-invalid").
    # acquire-lock (no --force) MUST exit non-zero; file unchanged.
```

**V19 — Zero-byte lock file is rejected as decode error.**

```python
def test_acquire_lock_rejects_zero_byte_file(isolated_plan, monkeypatch):
    # Pre-write: "" (zero-byte file; json.loads("") raises JSONDecodeError).
    # acquire-lock (no --force) MUST exit non-zero with
    # errors[0].code == "lock-json-decode".
    # File on disk unchanged (still zero bytes).
```

**V20 — `--force` over a valid canonical file with other-plan entries overwrites them.**

```python
def test_acquire_lock_force_discards_other_plans(isolated_plan, monkeypatch):
    # Pre-write canonical with two OTHER plan entries (A.md, B.md).
    # acquire-lock --force --plan-file <isolated_plan> --run-id R_new exits 0.
    # After: RUN_LOCK_PATH contains EXACTLY ONE entry keyed by <isolated_plan>.
    # Both A.md and B.md entries are GONE. This is the explicit concurrency
    # consequence documented in acceptance #4.
    # Output JSON: {"acquired": true, "forced": true}.
```

---

## Tasks

### TASK-004E: Enforce canonical lock-file shape + add `--force` escape hatch

- **Status:** done
- **Priority:** medium
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py` (rewrite `cmd_acquire_lock`; add `--force` arg; add `_validate_lock_shape`, `_atomic_write_json` helpers)
  - `tests/scripts/test_plan_ops.py` (extend `TestLock` class with V1–V20)
  - `tests/scripts/test_plan_codex_dispatch_integration.py` — NOT MODIFIED. This sub-plan explicitly preserves the existing `test_parallel_preserves_run_lock_json` contract. That test never invokes `acquire-lock`; it tests the wrapper's protected-paths policy. No change needed. If the implementer finds themselves editing this file, stop and re-read the scope clarification section.
  - `plugins/plan-executor/skills/implement-plan/SKILL.md` (one-paragraph lock-file shape + `--force` note in Pre-flight section)
- **Dependencies:** TASK-001
- **Test command:**
  ```bash
  venv/bin/pytest -q tests/scripts/test_plan_ops.py::TestLock \
    && venv/bin/pytest -q tests/scripts/test_plan_codex_dispatch_integration.py -k "preserves_run_lock_json" --no-header
  ```
  The integration-test leg is a regression guard: if the wrapper's lock-file handling has been accidentally coupled to `acquire-lock`, the wrapper test will fail and the implementer MUST halt before claiming completion. (The integration test is `@pytest.mark.slow` and skips when the Codex CLI is absent; if skipped, the implementer must state that in the report so the reviewer can request a retry on a machine with Codex.)
- **Acceptance criteria:** all 15 above (V1–V20, inclusive of V15b/V16b/V17b symmetric `acquired_at` checks).
- **Out of scope:** see top-of-plan list.

**Description:**
Tighten `acquire-lock` to reject any lock-file shape that is not canonical. Add a `--force` flag as the explicit escape hatch for corruption recovery. Keep `release-lock` as-is (asymmetric by design).

**Implementation sketch:**

```python
LOCK_ENTRY_KEYS = frozenset({"run_id", "acquired_at"})


def _validate_lock_shape(raw: object) -> list[dict]:
    """Return [] if canonical; else a list of {code, message} violations."""
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return [{"code": "lock-toplevel-not-object",
                 "message": f"lock file top-level is {type(raw).__name__}, expected object"}]
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            errors.append({"code": "lock-key-invalid",
                           "message": f"entry key {key!r} must be non-empty string"})
            continue
        if not isinstance(value, dict):
            errors.append({"code": "lock-entry-not-object",
                           "message": f"entry {key!r} value is {type(value).__name__}, expected object"})
            continue
        actual_keys = set(value.keys())
        missing = LOCK_ENTRY_KEYS - actual_keys
        extra = actual_keys - LOCK_ENTRY_KEYS
        if missing:
            errors.append({"code": "lock-entry-missing-keys",
                           "message": f"entry {key!r} missing keys: {sorted(missing)}"})
        if extra:
            errors.append({"code": "lock-entry-extra-keys",
                           "message": f"entry {key!r} has extra keys: {sorted(extra)}"})
        for req in ("run_id", "acquired_at"):
            if req in value and (not isinstance(value[req], str) or not value[req]):
                errors.append({"code": "lock-entry-value-empty",
                               "message": f"entry {key!r} field {req!r} must be non-empty string"})
    return errors


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Torn-write-safe write. Uses mkstemp in the same directory (so os.replace
    is a same-filesystem atomic rename) and a unique suffix (so two concurrent
    acquires cannot collide on a fixed .tmp name). Does NOT fsync — durability
    after power loss is explicitly not a requirement here; the guarantee is
    that the canonical path never holds a partial JSON body visible to a
    concurrent reader."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    )
    tmp = Path(tmp_name)
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)
        replaced = True
    finally:
        if not replaced and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def cmd_acquire_lock(args: argparse.Namespace) -> None:
    plan_abs = os.path.abspath(args.plan_file)
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)

    pre_raw: object | None = None
    decode_err: str | None = None
    if RUN_LOCK_PATH.exists():
        raw_text = RUN_LOCK_PATH.read_text(encoding="utf-8")
        try:
            pre_raw = json.loads(raw_text)
        except json.JSONDecodeError as e:
            decode_err = str(e)

    if args.force:
        new_state = {plan_abs: {"run_id": args.run_id, "acquired_at": _now()}}
        _atomic_write_json(RUN_LOCK_PATH, new_state)
        _emit(args, {"acquired": True, "forced": True})
        return

    if decode_err is not None:
        _die(args, {
            "acquired": False,
            "errors": [{"code": "lock-json-decode", "message": decode_err}],
        })

    if pre_raw is not None:
        shape_errors = _validate_lock_shape(pre_raw)
        if shape_errors:
            _die(args, {"acquired": False, "errors": shape_errors})
        current = pre_raw
    else:
        current = {}

    if plan_abs in current and current[plan_abs].get("run_id") != args.run_id:
        _die(args, {
            "acquired": False,
            "conflict_run_id": current[plan_abs].get("run_id"),
        })

    current[plan_abs] = {"run_id": args.run_id, "acquired_at": _now()}
    _atomic_write_json(RUN_LOCK_PATH, current)
    _emit(args, {"acquired": True})
```

Argparse update:

```python
p_acq = sub.add_parser("acquire-lock", help="...")
p_acq.add_argument("--plan-file", required=True)
p_acq.add_argument("--run-id", required=True)
p_acq.add_argument("--force", action="store_true",
                   help="Overwrite lock file even if malformed / owned by other plans")
_add_json(p_acq)
```

**Forbidden patterns:**

- `except json.JSONDecodeError: current = {}` — silent coercion is the exact bug being fixed.
- Auto-merging extra keys into the canonical entry (e.g., preserving a legacy `pid` field "for compatibility"). There is no legacy client of this shape; extra keys are corruption.
- Deleting the lock file in the non-force path. The non-force path is READ-OR-REJECT only.
- TTL-based auto-release (e.g., "if acquired_at > 1 hour ago, wipe"). Out of scope per parent plan.
- PID liveness checks (`os.kill(pid, 0)`). Out of scope per parent plan.
- Modifying `cmd_release_lock` to add `--force` or a similar flag. Asymmetry is intentional.
- Changing the write path (e.g., writing to `_run_lock.json.new` then later renaming via a second subcommand). Use atomic write-tmp + os.replace in a single subcommand call.

**Test scaffolding notes:**

- `TestLock` class at `tests/scripts/test_plan_ops.py:247` already has the happy-path + conflict tests. Extend that class; do NOT create a parallel test class.
- `RUN_LOCK_PATH` is redirected per-test via the `isolated_plan` fixture (verify pattern at top of `test_plan_ops.py`). Use the same fixture.
- For V12 atomic-write test: `monkeypatch.setattr(os, "replace", ...)` raises; assert original file content preserved.
- For "file on disk unchanged" assertions: read byte-level (`RUN_LOCK_PATH.read_bytes()`) before and after, compare exact equality.

**SKILL.md update (surgical):**

Phase 0 Pre-flight section currently mentions:

> Then acquire the run-lock:
>
> ```bash
> venv/bin/python plugins/plan-executor/scripts/plan_ops.py acquire-lock --plan-file <absolute plan> --run-id <id>
> ```
>
> Overlap on the same plan → halt with the conflicting run_id.

Append one paragraph (NOT a new section):

> The lock file at `docs/plans/_run_lock.json` follows a strict canonical shape: a JSON object keyed by absolute plan path, with each entry containing exactly `{"run_id": "<id>", "acquired_at": "<opaque non-empty string>"}`. Any other shape (invalid JSON, extra keys, missing keys, top-level not an object, non-string or empty values) is rejected by `acquire-lock`. `--force` is a manual recovery tool — use it only to recover from a corrupted or stuck lock file. `--force` discards all existing entries (including entries for other plans), so do not run it while a legitimate run is in progress.

**Rollout:**

- No schema migration needed — the canonical shape matches what `cmd_acquire_lock` already writes (line 1467). Only the READ path gets stricter.
- Any in-the-wild `_run_lock.json` that diverges from canonical MUST have been written by a non-canonical writer, which doesn't exist in this repo. The backstop is `--force`.
- The `forced` key in the JSON emit is additive. Existing tests at `tests/scripts/test_plan_ops.py:255-276` only assert subsets of fields; no caller in `plugins/plan-executor/skills/implement-plan/SKILL.md` parses a strict keyset. Adding `forced` is backward-compatible.
- No `.gitignore` entry or git hook mutates `docs/plans/_run_lock.json` today (the only ignored lock path is `docs/bugs/_run_lock.json` at `.gitignore:128`). Tightening `acquire-lock` therefore cannot trip CI or hook workflows.
- `tempfile` must be imported at the top of `plugins/plan-executor/scripts/plan_ops.py`. Verify the existing import block includes it; add if absent.
