# Plan: `plan_ops.py` pure-core extraction via codemod

**Status:** partial
**Author of draft:** orchestrator session under `/implement-plan docs/plans/MCP_MIGRATION` run `20260430T033005`; refined via paired `gemini` (code-search) and `codex exec` (coding-implications) deep dives plus first-party AST sampling of `plan_ops.py`.
**Supersedes (proposed):** `docs/plans/MCP_MIGRATION/PLAN_MCP_MIGRATION.md` TASK-011, TASK-012, TASK-013
**Does not supersede:** TASK-001, TASK-003, TASK-004, TASK-005, TASK-006, TASK-007, TASK-008, TASK-009, TASK-010 of the parent plan; this draft only re-shapes the pure-core extraction trio.

## Goal

Extract a deterministic AST-codemod-driven pure-core (`_run_X(payload) -> dict`) layer for all 38 `cmd_*` functions in `plugins/plan-executor/scripts/plan_ops.py`, replacing the failed monolithic TASK-011/012/013 split with a reproducible pipeline: stabilize the terminator contract → capture a CLI byte-equal baseline → build + dry-run a libcst codemod → apply it in one commit → hand-fix the 9 skip-list functions → ship per-tier conformance fixtures. End state: every `cmd_X` is a 3-line shim over `_run_X(payload)`, unblocking parent-plan TASK-004/005/006 (MCP tool registration) and TASK-007/008/009/010 (SKILL migration / conformance / drift guard / E2E).

## Verification

- The full existing `tests/scripts/test_plan_ops*.py` suite passes after every individual task and at end-of-plan (no semantic drift).
- The `tests/scripts/test_plan_ops_pure_core_baseline.py` byte-equal CLI baseline (created in TASK-000B) passes against the post-codemod `plan_ops.py` (TASK-002) and again after every TASK-003A–003F hand-fix commit.
- `python plugins/plan-executor/scripts/plan_ops.py --help` lists 38 subcommands; `python plugins/plan-executor/scripts/plan_ops.py <sub> --help` returns the same `option_strings` list per-subcommand as the pre-codemod baseline.
- `dry_run_report.json` (TASK-001B) shows `summary.total_cmd_x == 38`, the 9-function skip-list (or its expansion if the dry-run discovers more), and zero `ambiguous_arg_specs_count`.
- Tier-A / Tier-B / Tier-C conformance harness fixtures (TASK-003H / TASK-004 / TASK-005 / TASK-006) all pass: byte-equal canonical envelopes between CLI subprocess and in-process `_run_*` paths for each fixture pair.
- `git log` shows: one TASK-000A commit, one TASK-000B commit, one TASK-001 commit, one TASK-001B commit (plus the report artifact), one TASK-002 commit (codemod-applied), one commit each for TASK-003A–003G, plus per-task fixture commits — no rebases, no force-pushes.

**Refinement summary (v1 → v2):**
- Added `TASK-000A` (stabilize `_emit/_die` terminator contract) + `TASK-000B` (capture pre-codemod CLI baseline) as preconditions to the codemod itself, because the v1 substitution rule "`_emit(args, r)` → `return r`" is not semantics-preserving (`_emit` calls `sys.exit`).
- Added `TASK-001B` (dry-run codemod + finalize skip-list and `ARG_SPECS` type table) so the single large `TASK-002` diff lands against a reviewed plan, not a discovery one.
- Added `TASK-004-HARNESS` (shared pure-core conformance harness) so per-tier fixture tasks can run independently against one canonical driver.
- **Skip list grew from 3 → 9 functions.** Ground-truth audit surfaced six additional `cmd_*` that bypass `_emit/_die`: `cmd_commit_task` (mutex groups + cross-flag-mutated args), `cmd_fail_task` (direct `sys.stdout.write+sys.exit` at lines 7627-7652 mixed with `_die`), `cmd_audit` (custom text renderer at lines 11060-11062 mixed with `_emit`), `cmd_resolve_read_targets` (custom success-emit at lines 11673-11674), `cmd_build_claude_dispatch_input` (reads `UNATTENDED_REVERT_POLICY` env + uses private `_bcdi_emit_error` terminator at 12 call sites + sentinel-bearing `--output -` flag), `cmd_build_codex_dispatch_input` and `cmd_build_gemini_dispatch_input` (use `_bcdi_emit_envelope` and `_bcdi_resolve_policy_or_die` private terminators). `cmd_parse_schedule` is intentionally NOT skip-listed in v3: it is a required-`--stdin` JSON command, not a stdin-vs-file branch.
- Replaced the path-flag suffix heuristic with an explicit `ARG_SPECS` table built from argparse introspection. Codex enumerated specific flags the suffix heuristic would mis-classify (e.g. `--output` is a path that takes sentinel `"-"`, `--update-schedule-state` is a path with no recognizable suffix, `--repo-root`/`--plans-dir`/`--task-file` are paths, while `--task-id`/`--run-id`/`--fields-json`/`--rows-json` are NOT paths despite their structured suffixes).

---

## Context

### Why this plan exists

Run `20260430T033005` of `/implement-plan docs/plans/MCP_MIGRATION` halted at Phase 2 batch 1 with TASK-011 returning `outcome: plan-incorrect` (no edits applied). Run-log entry: `docs/plans/_run_log.jsonl` — search for `run_id: "20260430T033005"`. Implementer envelope is preserved in conversation; prior summary captured under memory.

The original PLAN_MCP_MIGRATION.md split the deferred TASK-002 (38 `cmd_*` refactor) into three sub-tasks by mutation profile (Tier A read-only / Tier B atomic-writers / Tier C state-mutating). That split addressed parallel-batch concurrency safety but **did not reduce per-task implementer scope** — TASK-011 alone still carries ~60% of the original TASK-002 surface AND a stricter byte-equal CLI conformance gate the original task did not require. The implementer immediately re-diagnosed the same scope-mismatch failure mode that halted TASK-002.

### Diagnosis — what failed about the previous shape

The failure mode is not "too much code." It's the **product of four multipliers** that compound across N functions in a single dispatch:

1. **Uniform refactor pattern across 23 functions.** Every `cmd_X` becomes the literal shim `cmd_X(args) = _emit(args, _run_X(_args_to_payload_X(args)))` with the body lifted into `_run_X(payload: dict) -> dict`. Four mechanical steps × 23 functions.

2. **23 new helpers introduced** (`_args_to_payload_X` × 23 + a single `_read_stdin_text`) — roughly +500 LoC of new code on top of the moved body LoC.

3. **Per-function byte-equal conformance fixtures.** AC requires ≥34 paired payload+expected JSON files driving both the in-process pure core AND the bash CLI subprocess; envelopes must be byte-equal after JSON canonicalization (sorted keys, normalized whitespace, deterministic timestamps stubbed).

4. **All four mutations land atomically in one dispatch** before the test command can even execute — implementer cannot ship partial work without leaving `plan_ops.py` half-refactored.

### Verified body LoC (measured 2026-04-30 against the live `plan_ops.py`)

`plan_ops.py` carries 38 `cmd_*` functions today. Body LoC measured as the gap to the next top-level `def`/`class` (so private helpers between `cmd_*` are excluded from the cmd_X count itself):

**Tier A (TASK-011, 23 fns, total 1,901 LoC):**

| LoC | Function |
|---:|---|
| 237 | cmd_filter_schedule |
| 209 | cmd_parse_plan_review_report |
| 206 | cmd_batch_next |
| 142 | cmd_gates |
| 118 | cmd_lint_plans |
| 99 | cmd_run_summary |
| 94 | cmd_parse_implementer_report |
| 84 | cmd_parse_schedule |
| 81 | cmd_parse_plan_review_triage_report |
| 80 | cmd_compute_schedule |
| 77 | cmd_parse_d5_adjudication |
| 67 | cmd_path_info |
| 66 | cmd_claude_envelope_extract |
| 62 | cmd_resolve_read_targets |
| 62 | cmd_order_triage_findings |
| 59 | cmd_build_tasks |
| 52 | cmd_index_closure |
| 32 | cmd_review_route |
| 19 | cmd_build_gemini_dispatch_input |
| 18 | cmd_check_plan_deps |
| 14 | cmd_build_codex_dispatch_input |
| 13 | cmd_list_global_lock_paths |
| 10 | cmd_normalize_task_id |

**Tier B (TASK-012, 5 fns, total 484 LoC):**

| LoC | Function |
|---:|---|
| 292 | cmd_build_claude_dispatch_input |
| 86 | cmd_audit |
| 43 | cmd_finalize_execution_log |
| 35 | cmd_write_schedule |
| 28 | cmd_update_plan_header |

**Tier C (TASK-013, 10 fns, total 1,451 LoC):**

| LoC | Function |
|---:|---|
| 374 | cmd_block_dependents |
| 308 | cmd_commit_task |
| 182 | cmd_fail_task |
| 179 | cmd_preflight |
| 161 | cmd_auto_validate_divergence |
| 112 | cmd_log_event |
| 52 | cmd_acquire_lock |
| 41 | cmd_reconcile_batch |
| 23 | cmd_decompose_plan |
| 19 | cmd_release_lock |

**Grand total: 3,836 LoC across 38 cmd_* bodies** (matches the 38 fns enumerated by `grep -c "^def cmd_" plan_ops.py`).

**TASK-011 mutation surface (estimated):** ~1,900 LoC moved + ~500 LoC new helpers + ~150 LoC harness scaffold + ~34 fixture pairs (≥68 JSON files of fixture content, ~1,700 LoC) ≈ **3,300+ LoC of edits across two files in one dispatch under a byte-equal conformance gate.** That's a 1–3 day human refactor compressed into one implementer turn.

### Codemod insight

The refactor itself is ~80% deterministic AST rewrite. Every cmd_X transforms identically:

| Sub-step | Mechanizable? | Notes |
|---|---|---|
| Identify `cmd_X` functions | yes | grep `^def cmd_` (38 today) |
| Read the matching argparse subparser | yes | parse `add_argument(...)` calls in CLI registration block in `main()` |
| Generate `_args_to_payload_X(args)` | yes | one entry per `add_argument` flag |
| Rewrite body: `args.X` → `payload["X"]` | yes | identifier substitution via `libcst` |
| Move stdin to payload via raw `_read_stdin_text()` | yes | grep `sys.stdin`; preserve raw text so existing `json.loads(raw)` and markdown parsers keep their own error envelopes |
| Replace `_emit(args, r)` / `_die(args, r)` with `return r` | yes | one-line substitution |
| Wrap as `def _run_X(payload):`; reduce cmd_X to 3-line shim | yes | mechanical |
| String-vs-Path flag detection | partial | suffix heuristic (`_file`, `_path`, `_dir` → `pathlib.Path`); maintain a known-flag → type override table |
| `cmd_gates` mutex-group → `payload["mode"]` | no | hand-coded special case |
| `cmd_filter_schedule` stdin-vs-file branch | no | hand-coded special case (`payload["input_source"] in {"stdin","file"}`); `cmd_parse_schedule` has no file branch |
| Required-flag combinations → `errors[]` inside `_run_*` | no | small refactor in ~3 functions (e.g. cmd_gates `--certify` requires plan-file + schedule-file) |

The conformance fixtures themselves are NOT mechanizable — they require knowing what each `cmd_X` does to construct happy-path + error-path + branch-coverage inputs. That's where LLM dispatches stay.

### Why this beats five implementer dispatches (TASK-011a/b/c/d/e)

The implementer that returned `plan-incorrect` recommended a 5-way function-cluster split: TASK-011a (shim infra + cmd_review_route + cmd_normalize_task_id), TASK-011b (5× cmd_parse_* + cmd_claude_envelope_extract + cmd_order_triage_findings), TASK-011c (cmd_gates alone), TASK-011d (cmd_filter_schedule + cmd_compute_schedule + cmd_batch_next + cmd_parse_schedule), TASK-011e (residual long tail). Each sub-task is single-session-tractable in isolation.

This codemod approach is strictly better because:

- **Codemod runs in seconds and produces a reviewable single diff.** The "halfway-refactored plan_ops.py" failure mode is impossible by construction.
- **Semantic drift is structurally prevented.** An AST rewrite that doesn't change identifiers or call order can't introduce divergence. The byte-equal conformance gate becomes belt-and-suspenders rather than load-bearing — meaning the fixture density requirement can be relaxed (existing `plan_ops` test suite passing + a smoke set of fixtures is a fully sufficient gate).
- **Fixture authoring decouples from refactor.** Per-tier fixture-authoring dispatches run against an already-stable refactored codebase, so each is independent and tractable.
- **Lower aggregate cost.** Five implementer dispatches × ~$0.78 each ≈ $4 vs one codemod-script implementer ($0.50–$1) + one codemod-application review ($0.20) + three fixture-authoring dispatches ($0.50 each) ≈ $2.20.
- **Same end state.** `_run_X(payload) -> dict` cores exist for all 38 functions — unblocking parent-plan TASK-004/005/006 (MCP tool registration), TASK-007 (SKILL.md migration), TASK-008 (full conformance), TASK-009 (drift guard), TASK-010 (E2E smoke) exactly as the parent plan intends.

### Risk assessment for sibling tasks (parent plan)

I read every TASK block in `PLAN_MCP_MIGRATION.md` and verified scope. Same-shape failure risk under the original AC:

| Task | Surface | LoC | Fixtures | Risk verdict |
|---|---|---:|---:|---|
| 011 (Tier A) | 23 cmd_* | 1,901 | ≥34 pairs | **REPRODUCED** failure |
| 012 (Tier B) | 5 cmd_* | 484 | ~10 pairs | **TRACTABLE** as-written |
| 013 (Tier C) | 10 cmd_* | 1,451 | ≥10 named + crash-recovery + tmp-git-repo + JSONL hash chain + lock-state + SIGTERM + process-tree + hermetic env | **HIGH — same shape as 011** |
| 008 (full conformance) | 36 tools | n/a | 108 fixtures + harness | **HIGH — same shape as 011** |
| 004/005/006 (MCP register) | 10/9/17 tools | small | byte-equal MCP↔CLI | **BLOCKED then tractable** (depends on `_run_*` existing) |
| 007 (SKILL.md) | ~6000 line edit | n/a | none (deferred) | **MEDIUM**, splittable |
| 009 (drift guard) | 1 test, 4 assertions | small | n/a | **SMALL** |
| 010 (E2E smoke) | 1 test + fixture plan | medium | n/a | **MEDIUM-LARGE**, bounded |

This plan addresses TASK-011/012/013 (and indirectly de-risks 008 by providing a reusable codemod for any future similar refactor).

---

## Tasks

### TASK-000A: Stabilize the `_emit` / `_die` terminator contract

- **Status:** done
- **Priority:** high (precondition for the codemod itself)
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify — add `_result()` and `_emit_or_die()` helpers near the existing `_emit/_die` definitions at lines 2011-2026)
  - tests/scripts/test_plan_ops.py (modify — add unit tests for new helpers)
- **Dependencies:** none
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py -k "emit or release_lock or acquire_lock or gates"`
- **Acceptance criteria:**
  - `_emit(args, result, *, exit_code=0)` and `_die(args, result, *, exit_code=1)` retain their current behavior byte-identically (JSON path and non-JSON path both unchanged for callers that pass through them today).
  - New helper `_result(payload: dict, *, exit_code: int = 0) -> dict` returns a copy of `payload` with `__plan_ops_exit_code__` set. The marker is internal-only and deliberately uses a collision-resistant name instead of `_exit_code`.
  - New helper `_emit_or_die(args, result: dict) -> None` reads and pops the `__plan_ops_exit_code__` marker, then delegates to `_emit(args, result, exit_code=...)`. Default exit code if marker is absent: `1` if `result.get("errors")` is non-empty OR `result.get("error")` is set, else `0`.
  - New helper `_public_result(result: dict) -> dict` returns a shallow copy with every `__plan_ops_*__` internal marker removed. MCP/SKILL callers and the pure-core conformance harness consume `_public_result(_run_X(payload))`, never the raw marker-bearing dict.
  - The popped `__plan_ops_exit_code__` key NEVER appears in stdout/stderr output (test asserts both JSON and non-JSON paths).
  - **`__plan_ops_text_output__` convention** (closed-vocabulary internal-only envelope key, documented in `_emit_or_die`'s docstring): if `result.pop("__plan_ops_text_output__", None)` returns a non-empty string, `_emit_or_die` writes that string verbatim to stdout (no `json.dumps` wrapping) before exiting. `_public_result()` strips this marker so MCP / structured callers consume only the public result fields. Used by `cmd_audit` (TASK-003 hand-fix); other commands are forbidden from using it without an explicit allowlist update.
  - **`__plan_ops_stdout_suppressed__` convention** (closed-vocabulary internal-only envelope key, documented in `_emit_or_die`'s docstring): if true, `_emit_or_die` exits with the resolved exit code without writing stdout. This is reserved for commands whose current CLI contract writes to `--output <file>` instead of stdout, notably the `cmd_build_*_dispatch_input` family in TASK-003.
  - Unit tests cover: `_emit_or_die` happy path (`__plan_ops_exit_code__` 0), top-level `"error"` (non-zero exit, ERROR text on stderr), `errors[]` (non-zero exit, JSON envelope unchanged), explicit non-zero `__plan_ops_exit_code__` override, a regression test for the `--json` vs non-`--json` divergence at line 2012-2020, a `__plan_ops_text_output__` shape test asserting (a) stdout is the verbatim text, no JSON wrapping, (b) the internal key never appears in stdout/stderr, (c) `_public_result()` stripping is safe for downstream MCP callers, a `__plan_ops_stdout_suppressed__` shape test asserting file-output commands can exit 0 without stdout, and `_public_result()` removes all current `__plan_ops_*__` keys without mutating the original result dict.
  - `cmd_X` bodies are NOT modified in this task — this only stabilizes the contract.

**Description:** The codemod plan's v1 substitution rule "replace `_emit(args, r)` with `return r`" is not semantics-preserving because `_emit` calls `sys.exit` at line 2021 (and `_die` delegates to `_emit` at 2025). Rewriting mid-body `_emit/_die` calls to `return r` would silently let subsequent body code execute (e.g. `cmd_release_lock` at line 8132 — its `no-lock-file` branch must terminate, not fall through into `RUN_LOCK_PATH.read_text(...)` at line 8134). Establishing the result-envelope-with-exit-marker contract before the codemod runs makes the rewrite a pure mechanical operation: every `_emit(args, expr, exit_code=N)` becomes `return _result(expr, exit_code=N)`, and the only caller in the shim is `_emit_or_die(args, result)`.

**Reversion guidance:** Revert this commit; `_emit/_die` semantics are unchanged today, and the new helpers + tests are additive.

---

### TASK-000B: Capture pre-codemod CLI baseline

- **Status:** done
- **Priority:** high (regression gate for TASK-002)
- **Files:**
  - tests/scripts/test_plan_ops_pure_core_baseline.py (create — captures the baseline once and asserts byte-equality on subsequent runs)
  - tests/scripts/fixtures/plan_ops_cli_baseline/ (create — directory of ~12-15 fixture sets, each a quad of `<sub>__<case>.{stdin,stdout,stderr,exit}.txt` files)
- **Dependencies:** TASK-000A
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_core_baseline.py`
- **Acceptance criteria:**
  - For each fixture, captures pre-codemod stdout, stderr, AND exit code via `subprocess.run([sys.executable, "plan_ops.py", *argv], input=stdin_text, capture_output=True)`.
  - Baseline fixture set covers terminator-sensitive branches (one per non-trivial pattern):
  - `normalize-task-id 1` (happy) and `normalize-task-id bogus` (validation error path)
  - `release-lock` no-lock-file branch (terminator at line 8132)
  - `release-lock` run-id-mismatch branch (terminator at line 8139)
  - `acquire-lock` empty-`--run-id` branch (terminator at line 8079)
  - `gates --list` (terminator)
  - `gates --certify --mode execute` missing `--run-id` (cross-flag argparse error → exit 2)
  - `parse-schedule --stdin` happy path
  - `filter-schedule --stdin` happy path (stdin-vs-file branch)
  - `build-claude-dispatch-input --output -` (sentinel-bearing path flag)
  - `commit-task --narrow-remediation-tag --dismissed-finding-ids 1,x` (cross-flag argparse error mid-`main()` at line 13750)
  - `auto-validate-divergence --stdin` non-applicable envelope path
  - `--json` and non-`--json` shapes for at least three of the above (stresses the `args.json` formatting branch in `_emit`)
  - All fixtures isolated under `tmp_path`; no test depends on the developer's live `_run_log.jsonl` or lock file.
  - Baseline test runs in <30 s.
  - File format: one fixture per case, plain text on disk, no canonicalization (this is the raw byte-image — TASK-002 must reproduce it byte-equally after codemod application).

**Description:** The existing `plan_ops` test suite asserts behavior at the helper / function level but does NOT lock down the exact stdout/stderr/exit triple at the CLI surface. Without that snapshot, TASK-002's codemod application could pass all unit tests while introducing silent stderr divergence (e.g. an `args.X` → `payload["X"]` substitution inside an f-string with `!r`) or formatting drift. This task creates the before-image that TASK-002 must match.

**Reversion guidance:** Delete the test and fixture directory. Production code is untouched.

---

### TASK-001: Build the AST-rewrite codemod script

- **Status:** done
- **Priority:** high
- **Files:**
  - tools/codemods/plan_ops_pure_core_extract.py (create)
  - tests/tools/test_plan_ops_pure_core_extract.py (create)
- **Dependencies:** TASK-000A
- **Test command:** `venv/bin/python -m pytest tests/tools/test_plan_ops_pure_core_extract.py`
- **Acceptance criteria:**
  - Script accepts `--in <plan_ops.py>` and `--out <plan_ops.py>` (may be the same path; in-place rewrite is supported); also accepts `--dry-run` (writes nothing, prints the diff and the per-function classification) and `--report <path>` (writes a machine-readable JSON report consumed by TASK-001B).
  - Uses `libcst` (preferred — round-trippable formatting) or `ast`+`astor` (fallback) to:
  - Find every `def cmd_X(args: argparse.Namespace) -> None:` definition.
  - Locate `build_parser()` (currently around line 12077; argparse registration lives there, NOT in `main()`). Enumerate each `subparsers.add_parser(...)` block and its `add_argument(...)` calls. This is the source of truth for the flag list, types, and `required=` flags.
  - Build a unified `ARG_SPECS` table (in-memory, dumped into the report) of `{subcommand: [{flag, dest, type, default, required, value_kind}]}`. `value_kind` is one of `"path" | "string" | "int" | "bool" | "json-string" | "csv-string"` and is determined by argparse `type=`/`action=` first, then by an explicit override list, then by suffix-heuristic as a last resort.
  - Generate `def _args_to_payload_X(args: argparse.Namespace) -> dict:` mapping each flag to a dict entry per `value_kind`. Path values are wrapped `pathlib.Path(args.X) if args.X else None` ONLY for `value_kind == "path"`.
  - Generate `def _run_X(payload: dict) -> dict:` with the original cmd_X body, identifier-substituted (`args.X` → `payload["X"]` via CST node replacement, NOT textual substitution — preserves `!r`, `%s`, `repr`, and f-string conversion specifiers byte-equally) and with terminator calls rewritten:
  - `_emit(args, expr)` → `return _result(expr, exit_code=0)`
  - `_emit(args, expr, exit_code=N)` → `return _result(expr, exit_code=N)`
  - `_die(args, expr)` → `return _result(expr, exit_code=1)` (default `_die` exit is 1)
  - `_die(args, expr, exit_code=N)` → `return _result(expr, exit_code=N)`
  - These rewrites apply to mid-body terminators AS WELL AS terminal terminators (no special-casing for "last statement").
  - Replace stdin reads (`sys.stdin.read()`) with `payload["stdin_text"]`; the shim populates `stdin_text` from `_read_stdin_text()` for subcommands whose argparse parser declares `--stdin`. The codemod MUST NOT parse JSON in the shim. Existing command bodies keep their own `json.loads(raw)` calls and `JSONDecodeError` handling after `raw = payload["stdin_text"]`, and raw-markdown commands such as `cmd_parse_implementer_report` keep receiving text.
  - Replace cmd_X body with the literal shim, **preserving the original `cmd_X` docstring on the shim** (operator/help-text contract):
    ```python
    def cmd_X(args: argparse.Namespace) -> None:
    """<original cmd_X docstring, preserved verbatim>"""
    payload = _args_to_payload_X(args)
    result = _run_X(payload)
    _emit_or_die(args, result)
    ```
  - Insert `_read_stdin_text()` helper at module scope (one definition) if not already present.
  - **Skip-list (functions left untouched in `--out`, surfaced to stderr and to the JSON report; hand-fixed in TASK-003A–003F):**
  - `cmd_gates` — argparse mutex group (`mx_gates` at line 12938) requires mode collapse to `payload["mode"]` rather than three boolean flags.
  - `cmd_filter_schedule` — stdin-vs-file branch encoded by argparse default `--schedule-file` + `--stdin`; payload needs `input_source` discriminator.
  - `cmd_commit_task` — TWO mutex groups (`p_commit_rem_grp` at 12383, `p_commit_dis_grp` at 12425) PLUS cross-flag arg mutation in `main()` at line 13750 that converts `--dismissed-finding-ids` from str to `list[int]` before dispatch.
  - `cmd_fail_task` — direct `sys.stdout.write+sys.exit` at lines 7627-7652 (the `--authorization-source` validation gate writes JSON to stdout and exits without going through `_emit/_die`); only the rest of the body uses `_die`.
  - `cmd_audit` — direct `sys.stdout.write(_render_audit_text(report))` + `sys.exit(exit_code)` at lines 11060-11062 for the non-`--json` path; `_emit` is used only for the `--json` path. Mixed pattern.
  - `cmd_resolve_read_targets` — custom success-emit `sys.stdout.write("\n")` + `sys.exit(0)` at lines 11673-11674 (no `_emit/_die` call for the success path).
  - `cmd_build_claude_dispatch_input` — reads `os.environ["UNATTENDED_REVERT_POLICY"]` at lines 11734 + 12021, uses private `_bcdi_emit_error(code, message)` terminator at 12 call sites (not `_emit/_die`), AND has a sentinel-bearing path flag `--output -`.
  - `cmd_build_codex_dispatch_input` — uses `_bcdi_emit_envelope(envelope, output)` private terminator (lines 12039-12042); same `--output -` sentinel.
  - `cmd_build_gemini_dispatch_input` — uses `_bcdi_resolve_policy_or_die()` and `_bcdi_emit_envelope(envelope, output)` private terminators (entire 13-line body); same `--output -` sentinel.
  - The codemod's dry-run report (TASK-001B) MUST surface any additional skip candidates discovered during AST inspection.
  - **`ARG_SPECS` value_kind override table (initial; TASK-001B refines):**
  - Path-typed flags that DO NOT match the `_file/_path/_dir` suffix heuristic — must be declared `path`:
    `update_schedule_state`, `from_schedule_state`, `repo_root`, `plans_dir`, `task_file`, `analyst_annotations`, `dispatch_context`, `report_file`, `baseline_file`, `out`, `baseline`.
  - String-typed flags that DO match the suffix heuristic but are NOT paths — must be declared `string`:
    `output` (sentinel `"-"` for stdout; body converts non-sentinel values to `Path` only when writing), `task_id`, `target_task_id`, `run_id`, `parent_run_id`, `fields_json` (JSON literal), `rows_json` (JSON literal), `reviewer_minor_findings` (JSON literal), `dismissed_finding_ids` (post-`main()` becomes `list[int]`; pre-`main()` it's a CSV string).
  - All other flags: codemod uses argparse's declared `type=` if present, else suffix heuristic, else `string`.
  - Idempotent: re-running on already-refactored code is a no-op (detect already-shimmed cmd_X by AST shape match — body is exactly the 4-line shim shape).
  - Unit tests:
  - Golden-file test: feed a small synthetic `cmd_X` body covering each rewrite shape (mid-body `_emit`, mid-body `_die`, f-string with `!r` on `args.X`, `%s` interpolation, raw stdin read, `json.loads(raw)` after stdin read, raw markdown/text stdin read, `args.json` access in shim), assert byte-equal rewritten output.
  - Real-file dry-run regression test: run codemod on the real `plan_ops.py` (read-only), assert (a) `ast.parse` succeeds on output, (b) all 38 cmd_X enumerated in the report (rewritten OR skipped — no silent omissions), (c) every original `_emit`/`_die` call site in non-skipped functions is accounted for as a `return _result(...)` in the rewritten body (count match), (d) no `args.<attr>` access remains in `_run_*` bodies (sanity check on identifier substitution), (e) `python -m py_compile <out>` succeeds, (f) `python <out> --help` lists the same 38 subcommand names as pre-codemod, (g) `python <out> <subcmd> --help` for each subcommand returns the same `option_strings` list as pre-codemod (argparse contract preserved).
  - Targeted regression: assert `cmd_release_lock`'s `no-lock-file` and `run-id-mismatch` branches do NOT fall through after rewrite (mock `_atomic_write_json` to raise — if the rewritten body falls through, the mock fires; otherwise the early-return `return _result(...)` short-circuits).
  - F-string preservation regression: assert that `cmd_commit_task`'s `f"bad --task-id: {args.task_id!r}"` (line 6887) and `cmd_fail_task`'s `f"bad --task-id: {args.task_id!r}"` (line 7656) stay byte-equal after rewrite (these are intentionally NOT rewritten in this task because both functions are in the skip list, but the codemod must demonstrate the rewrite shape works on a synthetic cmd_X body with the same construct).

**Description:** This is the load-bearing investment. Once this script exists, every non-skipped `cmd_X` refactor becomes a deterministic operation with a reviewable diff. The implementer that writes this script is doing one focused job (AST rewriting) — no fixture authoring, no conformance harness, no pattern across N functions. Single-session tractable. Estimated codemod size: ~400–700 LoC of Python (libcst + argparse-introspection logic) + ~200–300 LoC of tests.

**Reversion guidance:** Delete `tools/codemods/plan_ops_pure_core_extract.py` and the matching test. `plan_ops.py` is unaffected.

---

### TASK-001B: Dry-run codemod and finalize skip-list + ARG_SPECS table

- **Status:** done
- **Priority:** high (gate before TASK-002 fires)
- **Files:**
  - tools/codemods/plan_ops_pure_core_extract.py (modify if dry-run surfaces ambiguous flags or new skip candidates — operator updates the override table and re-runs)
  - docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/PLAN_PURE_CORE_CODEMOD.md (update — sync the skip list to the dry-run output)
  - docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/dry_run_report.json (create — committed artifact)
- **Dependencies:** TASK-001
- **Test command:** `venv/bin/python tools/codemods/plan_ops_pure_core_extract.py --in plugins/plan-executor/scripts/plan_ops.py --out /tmp/plan_ops.codemod.py --dry-run --report docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/dry_run_report.json && venv/bin/python -c "import json,sys; r=json.load(open('docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/dry_run_report.json')); assert r['skipped'] and r['rewritten'], r; print('OK')"`
- **Acceptance criteria:**
  - Dry-run produces `dry_run_report.json` with the schema:
    ```json
    {
    "schema_version": 1,
    "rewritten": [{"function": "cmd_X", "lines": [start, end], "emit_die_count": N, "stdin_read": true|false, "fstring_args_uses": M, "argparse_flags": [...]}],
    "skipped": [{"function": "cmd_X", "reason": "<one-of: mutex-group | direct-stdout-exit | private-terminator | env-var-read | cross-flag-arg-mutation | mixed-pattern>", "evidence": "<file:line>"}],
    "arg_specs": {"subcommand": [{"flag": "--X", "dest": "X", "value_kind": "path|string|int|bool|json-string|csv-string", "source": "argparse-type|override|suffix-heuristic", "required": true|false}]},
    "warnings": ["<text>"],
    "summary": {"total_cmd_x": 38, "rewritten_count": N, "skipped_count": M, "ambiguous_arg_specs_count": K}
    }
    ```
  - Skip list in `dry_run_report.json` matches (or extends) the v3 plan's 9 candidates: `cmd_gates`, `cmd_filter_schedule`, `cmd_commit_task`, `cmd_fail_task`, `cmd_audit`, `cmd_resolve_read_targets`, `cmd_build_claude_dispatch_input`, `cmd_build_codex_dispatch_input`, `cmd_build_gemini_dispatch_input`. Any newly-discovered skip candidate is logged with reason + evidence and the plan's TASK-003 description is updated by hand to cover it. `cmd_parse_schedule` should be rewritten by the codemod unless dry-run evidence discovers a new non-mechanical blocker.
  - Every entry in `arg_specs` has `value_kind` resolved (no `"unknown"` values). Ambiguous flags are listed in `warnings` with proposed override; operator hand-edits the codemod's override table per warning, then re-runs.
  - `dry_run_report.json` is committed as a plan artifact so the schedule of TASK-002's diff is reviewable in advance.
  - The dry run does NOT mutate `plugins/plan-executor/scripts/plan_ops.py`. The output file `/tmp/plan_ops.codemod.py` is purely advisory.
  - `git status` after this task shows only `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/dry_run_report.json` as new; production code is untouched.

**Description:** Separates "codemod exists" from "codemod is safe to apply at scale." This task converts the unknown set of additional skip candidates and the unresolved `value_kind` overrides into a reviewed, machine-readable artifact before the single large `TASK-002` diff fires. Without this gate, `TASK-002` would either generate a half-shimmed `plan_ops.py` (silent skip) or apply the wrong path-wrapping to a sentinel-bearing flag.

**Reversion guidance:** Delete `dry_run_report.json` and revert any plan-doc edits made in response to its findings. Production code is unaffected.

---

### TASK-002: Apply the codemod, review the diff, commit

- **Status:** Pending
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify, codemod-generated diff — single commit)
- **Dependencies:** TASK-000A, TASK-000B, TASK-001, TASK-001B
- **Test command:** `venv/bin/python -m pytest tests/scripts/ -k "plan_ops" && venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_core_baseline.py`
- **Acceptance criteria:**
  - Run codemod with the finalized skip list from `dry_run_report.json` (TASK-001B): `python tools/codemods/plan_ops_pure_core_extract.py --in plugins/plan-executor/scripts/plan_ops.py --out plugins/plan-executor/scripts/plan_ops.py --skip-from-report docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/dry_run_report.json`. The codemod refuses to run if `dry_run_report.json` is older than the codemod script's git sha (forces a re-dry-run if codemod source changed).
  - The full existing `plan_ops` test suite continues to pass without modification (semantic-drift gate).
  - The TASK-000B baseline test (`test_plan_ops_pure_core_baseline.py`) passes — every captured stdout/stderr/exit-code triple is byte-equal post-codemod.
  - `python plan_ops.py --help` lists the same 38 subcommand names; `python plan_ops.py <subcommand> --help` for each subcommand returns the same `option_strings` list (argparse contract preserved per-subcommand). Help-text wording may differ ONLY where the codemod's docstring-preservation logic intentionally normalizes whitespace; any such difference is documented in the commit message.
  - Smoke matrix (extends TASK-000B's baseline; runs in CI):
  - Read-only fast path: `review-route`, `batch-next`, `parse-schedule`, `compute-schedule`, `log-event`
  - Terminator-sensitive: `release-lock` no-lock-file, `release-lock` run-id-mismatch, `acquire-lock` empty-`--run-id`
  - Argparse cross-flag: `commit-task --narrow-remediation-tag --dismissed-finding-ids 1,x` (must continue to exit 2 with parser.error banner — `cmd_commit_task` is in skip list precisely so this is preserved)
  - Sentinel path flag: `build-claude-dispatch-input --output -` (preserved-as-skipped)
  - File-output sentinel family: `build-codex-dispatch-input --output <tmp_path>/codex.json` and `build-gemini-dispatch-input --output <tmp_path>/gemini.json` must preserve the existing contract of writing the file and emitting no stdout.
  - Stdin path: `filter-schedule --stdin` (preserved-as-skipped); `parse-schedule --stdin` (codemod-rewritten raw-stdin JSON path)
  - `--json` and non-`--json` for at least three of the above
  - **Codemod-diff review by `code-reviewer` subagent**: dispatch one-shot review of the entire diff before commit. Reviewer checks: (a) no `args.<attr>` references remain in `_run_*` bodies, (b) every original `_emit/_die` call site is accounted for as a `return _result(...)`, (c) docstrings preserved on `cmd_X` shims, (d) `ARG_SPECS`-derived `_args_to_payload_X` helpers correctly Path-wrap only declared-`path` flags. Reviewer report captured in commit message body.
  - Commit message lists the rewritten count (target ≈29 of 38) and explicitly enumerates the skip-list (target 9 of 38) referencing TASK-001B's `dry_run_report.json` sha.
  - **Reverter clearance**: at the time of commit, `git status` shows ONLY `plugins/plan-executor/scripts/plan_ops.py` modified (codemod must not introduce stray changes — fixture files, formatting drift in unrelated functions, etc.).

**Description:** This is the surgical strike. One commit, one diff, one CI run. Either the codemod produced a working refactor (validated against TASK-000B's pre-image) or it didn't — there is no halfway state. The byte-equal CLI baseline replaces the parent plan's per-function fixture-density gate as the load-bearing semantic-drift detector.

**Reversion guidance:** `git revert` the single commit. `plan_ops.py` is back to pre-refactor state. Codemod script (TASK-001), dry-run report (TASK-001B), and baseline test (TASK-000B) remain for re-application after fixes. If the parent plan's TASK-007 (SKILL.md migration) has already merged before this revert, do NOT also revert TASK-007 — the SKILL.md change is independent of the `_run_*` core layer's existence.

---

### TASK-003A: Hand-fix `cmd_gates`

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify only `cmd_gates`, `_args_to_payload_gates`, `_run_gates`, and directly required helper tests)
  - tests/scripts/test_plan_ops.py (modify focused gates tests only if needed)
- **Dependencies:** TASK-002
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -k "gates or pure_core_baseline" -q`
- **Acceptance criteria:**
  - `cmd_gates` is a shim over `_args_to_payload_gates(args)` → `_run_gates(payload)` → `_emit_or_die(args, result)`.
  - Argparse mutex group `--list` / `--check` / `--certify` remains intact for CLI ergonomics.
  - `_args_to_payload_gates` collapses the mutex group into `payload["mode"] in {"list","check","certify"}`.
  - `_run_gates` validates invalid mode and mismatched mode flags via structured `errors[]`, not argparse exits.
  - `certify` required-flag combinations are enforced inside `_run_gates` for pure/MCP callers.
  - `certify` failing gates preserve non-zero exit via `_result(envelope, exit_code=1)` rather than inferred `errors[]`.
  - Existing gates tests and the TASK-000B baseline stay green.
  - Commit only this task's files; no other skip-list function is changed.

**Description:** First special-case hand-fix, scoped to one mutex-group command.

**Reversion guidance:** Revert the single TASK-003A commit; TASK-002 remains intact.

---

### TASK-003B: Hand-fix `cmd_filter_schedule`

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify only `cmd_filter_schedule`, `_args_to_payload_filter_schedule`, `_run_filter_schedule`, and directly required helper tests)
  - tests/scripts/test_plan_ops.py (modify focused filter-schedule tests only if needed)
- **Dependencies:** TASK-003A
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -k "filter_schedule or pure_core_baseline" -q`
- **Acceptance criteria:**
  - `cmd_filter_schedule` is a shim over `_args_to_payload_filter_schedule(args)` → `_run_filter_schedule(payload)` → `_emit_or_die(args, result)`.
  - Shim sets `payload["input_source"] in {"stdin","file"}` from the resolved input path.
  - Stdin mode reads raw text once into `payload["stdin_text"]`; file mode reads `payload["schedule_text"]`.
  - JSON parsing remains inside `_run_filter_schedule` so decode-error envelopes stay byte-equal.
  - The stdin-mode `data.get("outcome")` relaxation is preserved.
  - Existing filter-schedule tests and the TASK-000B baseline stay green.
  - Commit only this task's files; no other skip-list function is changed.

**Description:** Isolates the stdin-vs-file branch so it can be reviewed independently.

**Reversion guidance:** Revert the single TASK-003B commit.

---

### TASK-003C: Hand-fix `cmd_commit_task`

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify only `cmd_commit_task`, `_args_to_payload_commit_task`, `_run_commit_task`, and commit-task validation helpers)
  - tests/scripts/test_plan_ops.py (modify focused commit-task tests only if needed)
- **Dependencies:** TASK-003B
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -k "commit_task or pure_core_baseline" -q`
- **Acceptance criteria:**
  - `main()`'s cross-flag `parser.error` validation remains intact for CLI misuse banners.
  - `cmd_commit_task` is a shim over `_args_to_payload_commit_task(args)` → `_run_commit_task(payload)` → `_emit_or_die(args, result)`.
  - Introduce `_validate_commit_task_payload(payload: dict) -> list[dict]` for pure/MCP callers; it mirrors CLI cross-flag invariants without calling `parser.error`.
  - `_args_to_payload_commit_task` consumes already-normalized `args.dismissed_finding_ids` from `main()`.
  - Mutex group behavior remains in argparse; pure callers get equivalent structured `errors[]`.
  - The invalid task-id f-string output remains byte-equivalent after `args.task_id` becomes `payload["task_id"]`.
  - Existing commit-task tests and the TASK-000B baseline stay green.
  - Commit only this task's files; no other skip-list function is changed.

**Description:** Isolates the highest-risk state-mutator special case.

**Reversion guidance:** Revert the single TASK-003C commit.

---

### TASK-003D: Hand-fix direct-output commands

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify only `cmd_fail_task`, `cmd_audit`, `cmd_resolve_read_targets`, and their payload/run helpers)
  - tests/scripts/test_plan_ops.py (modify focused tests only if needed)
- **Dependencies:** TASK-003C
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -k "fail_task or audit or resolve_read_targets or pure_core_baseline" -q`
- **Acceptance criteria:**
  - `cmd_fail_task`, `cmd_audit`, and `cmd_resolve_read_targets` become shims over `_run_*` payload cores.
  - `cmd_fail_task` authorization-source validation returns structured `errors[]` and preserves JSON/non-JSON exit behavior through `_emit_or_die`.
  - `cmd_fail_task` invalid task-id f-string output remains byte-equivalent.
  - `cmd_audit` returns `__plan_ops_text_output__` for the custom non-JSON renderer and strips that key through `_public_result()`.
  - `cmd_audit --json` flows through `_emit_or_die` without custom stdout writes.
  - `cmd_resolve_read_targets` success and error paths use `_emit_or_die`; trailing-newline behavior remains byte-equivalent.
  - Existing focused tests and the TASK-000B baseline stay green.
  - Commit only this task's files; dispatch-builder commands are not changed here.

**Description:** Groups the three direct-stdout/sys.exit commands because they share the `_emit_or_die` text/suppression boundary.

**Reversion guidance:** Revert the single TASK-003D commit.

---

### TASK-003E: Hand-fix `cmd_build_claude_dispatch_input`

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify `_bcdi_*` helpers only as required by Claude dispatch input and `cmd_build_claude_dispatch_input`)
  - tests/scripts/test_plan_ops.py (modify focused build-claude-dispatch tests only if needed)
- **Dependencies:** TASK-003D
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -k "build_claude_dispatch_input or pure_core_baseline" -q`
- **Acceptance criteria:**
  - `cmd_build_claude_dispatch_input` becomes a shim over `_args_to_payload_build_claude_dispatch_input(args)` → `_run_build_claude_dispatch_input(payload)` → `_emit_or_die(args, result)`.
  - Env reads for `UNATTENDED_REVERT_POLICY` are lifted into the shim payload; pure callers supply the policy explicitly.
  - Private `_bcdi_*` terminators used by this command return `_result(...)` dictionaries instead of calling `sys.exit`.
  - `--output -` remains a string sentinel, not a `Path`.
  - `--output <path>` writes only the file and emits no stdout by using `__plan_ops_stdout_suppressed__`.
  - Existing build-claude-dispatch tests and the TASK-000B baseline stay green.
  - Commit only this task's files; Codex/Gemini builders are not migrated here unless required helper signature changes make a no-op compatibility edit unavoidable.

**Description:** Splits the largest private-terminator builder away from the two small sibling builders.

**Reversion guidance:** Revert the single TASK-003E commit.

---

### TASK-003F: Hand-fix Codex/Gemini dispatch builders

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify only `cmd_build_codex_dispatch_input`, `cmd_build_gemini_dispatch_input`, and directly shared `_bcdi_*` helper call sites)
  - tests/scripts/test_plan_ops.py (modify focused build-codex/build-gemini tests only if needed)
- **Dependencies:** TASK-003E
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -k "build_codex_dispatch_input or build_gemini_dispatch_input or pure_core_baseline" -q`
- **Acceptance criteria:**
  - Both commands become shims over `_args_to_payload_*` → `_run_*` → `_emit_or_die`.
  - Both preserve `--output -` stdout-mode and `--output <path>` no-stdout file-output mode.
  - Both use the pure `_bcdi_to_error_result` / `_bcdi_to_envelope_result` helpers from TASK-003E.
  - Existing focused tests and the TASK-000B baseline stay green.
  - Commit only this task's files.

**Description:** Completes the private-terminator family after the larger Claude builder is stable.

**Reversion guidance:** Revert the single TASK-003F commit.

---

### TASK-003G: Special-case completion audit

- **Status:** done
- **Priority:** high
- **Files:**
  - plugins/plan-executor/scripts/plan_ops.py (modify only if audit finds a small missed shim issue)
  - docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/00_INDEX.json (mark 003A-003F done only if not already updated by orchestrator)
- **Dependencies:** TASK-003F
- **Test command:** `venv/bin/python -m pytest tests/scripts/ -k "plan_ops" -q`
- **Acceptance criteria:**
  - All 9 TASK-001B skip-list functions now have `_args_to_payload_*`, `_run_*`, and `cmd_*` shim structure.
  - No `sys.exit`, `sys.stdout.write`, `_emit`, or `_die` call remains inside those 9 `_run_*` bodies, except through documented helper boundaries returning `_result(...)`.
  - The exact broad gate `venv/bin/python -m pytest tests/scripts/ -k "plan_ops" -q` is green.
  - The TASK-000B baseline remains green.
  - No unrelated files are changed beyond status metadata.

**Description:** Explicitly prevents the next phase from starting on a partially migrated skip-list.

**Reversion guidance:** Revert only the audit/status commit; individual 003A-003F commits remain independently revertable.

---

### TASK-003H: Shared pure-core conformance harness

- **Status:** done
- **Priority:** medium
- **Files:**
  - tests/scripts/plan_ops_pure_harness.py (create — driver/utility, not a test file)
  - tests/scripts/test_plan_ops_pure_harness.py (create — meta-tests over the driver itself)
- **Dependencies:** TASK-003G
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_harness.py`
- **Acceptance criteria:**
  - Provides one reusable driver `run_conformance(subcommand: str, payload: dict, *, expected: dict, fixture_dir: Path) -> None` that:
  - Invokes both `subprocess.run([sys.executable, "plan_ops.py", subcommand, *cli_argv_from(payload)], input=stdin_text, capture_output=True)` (CLI subprocess path)
  - AND `_public_result(_run_<subcommand>(payload))` (in-process pure-core path, with CLI-only internal markers stripped)
  - Asserts byte-equal exit code, stderr policy (text-vs-empty), and stdout JSON parseability AS SEPARATE assertions BEFORE any canonicalization comparison.
  - After parseability passes, canonicalizes both envelopes (sorted keys, normalized whitespace, deterministic-timestamp stubs) and asserts byte-equal canonical form.
  - Supports per-fixture: stdin payloads (text), temp file inputs (writes payload's `_files` map under `tmp_path`), expected filesystem snapshots (asserts pre-call vs post-call directory snapshot equality for Tier-A read-only invariant), fixed clock injection (monkeypatches `_now()`), and command-specific payload builders (defers to `_args_to_payload_<subcommand>`).
  - Self-tests of the harness cover: byte-equal happy path, deliberate stdin-text divergence detection, exit-code mismatch detection, JSON canonicalization equivalence (re-ordered keys equal), filesystem-snapshot diff detection.
  - The Tier-A/B/C fixture test files (TASK-004/005/006) import this driver and add ONLY fixture cases — no subprocess logic duplicated.

**Description:** TASK-004/005/006 share substantial infrastructure (subprocess driver, JSON canonicalization, side-effect snapshotting). Without this shared harness, three divergent implementations would drift on canonicalization rules and assertion details — exactly the silent-divergence failure mode the conformance gate exists to catch. This now depends on TASK-003G so the harness is written against the final skip-list pure-core shape, not a moving target.

**Reversion guidance:** Delete the harness and self-test. Tier fixture tasks can be rescheduled to include their own local harness if needed.

---

### TASK-004: Tier-A conformance fixtures (smoke gate, recommended)

- **Status:** done
- **Priority:** medium
- **Files:**
  - tests/scripts/test_plan_ops_pure_entrypoints_tier_a.py (create)
  - tests/scripts/fixtures/plan_ops_pure_core/tier_a/<sub>__<case>.{payload,expected}.json (create, ~30 pairs)
- **Dependencies:** TASK-003H
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_a.py`
- **Acceptance criteria:**
  - Conformance harness with subprocess vs in-process driver: one fixture file pair per case, asserts byte-equal envelopes after JSON canonicalization (sorted keys, normalized whitespace, deterministic timestamps stubbed).
  - One happy-path fixture per Tier-A function (23 fns).
  - Special branch coverage:
  - `cmd_filter_schedule`: 4 cases (valid filter, dependency-cycle triggering `_validate_schedule_dag`, stdin-vs-file `outcome=needs-enrichment` branch, unknown-task-id error).
  - `cmd_gates`: 4 cases (each of `list` / `check` / `certify` modes plus a `certify`-missing-required-flag error).
  - Each `cmd_parse_*` (5 fns): 2 cases (happy path + structured-error path).
  - Asserts no filesystem mutation as side effect (Tier-A invariant) — fixture runs in `tmp_path`, post-call directory snapshot must equal pre-call.
  - Test runtime under 30s.
  - Fixture authoring may be split into 2–3 sub-dispatches per function-cluster if budget pressure surfaces; each cluster (the 5 cmd_parse_*, the 4 schedule-graph fns, the long tail) is independently tractable.

**Description:** With the refactor already landed and existing tests passing, this task is purely belt-and-suspenders coverage. Each fixture is independent — if implementer budget runs short, partial fixture coverage still leaves the test suite green and the refactor in place. Fixture density relaxed from the parent plan's ≥34 hard requirement to "smoke set + per-function happy + branch coverage on the 3 special-case fns."

**Reversion guidance:** Delete the test file and fixture directory. Refactor remains live and TASK-001/002/003 work is preserved.

---

### TASK-005: Tier-B conformance fixtures + atomic-write assertions

- **Status:** done
- **Priority:** medium
- **Files:**
  - tests/scripts/test_plan_ops_pure_entrypoints_tier_b.py (create)
  - tests/scripts/fixtures/plan_ops_pure_core/tier_b/<sub>__<case>.{payload,expected}.json (create, ~10 pairs)
- **Dependencies:** TASK-003H
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_b.py`
- **Acceptance criteria:**
  - Reuses Tier-A harness, adds: byte-equal `wrote_path` file content assertion + filesystem-snapshot diff (no `.tmp.<random>` leftovers from `_atomic_write_text` rename dance).
  - Validate-then-write atomicity: when `_run_X` returns non-empty `errors[]`, on-disk file is byte-identical to pre-call state.
  - Per-function happy + error fixture pair for: cmd_audit, cmd_build_claude_dispatch_input (variant happy + variant-mismatch error), cmd_finalize_execution_log, cmd_update_plan_header, cmd_write_schedule.
  - Output dict for each `_run_X` includes `wrote_path` and `bytes_written` so MCP and CLI callers observe the side effect deterministically.
  - Fixed clock for deterministic timestamps (audit reports, schedule timestamps).
  - `cmd_audit`'s read-only `_load_feat_commit_ids` git subprocess is permitted to remain (read-only `git log` calls do not drift the conformance comparison).
  - **Sentinel-bearing `--output -` coverage:** at least one fixture per `cmd_build_*_dispatch_input` exercising both `--output -` (stdout-mode) and `--output <tmp_path>/foo.json` (file-mode) — assert byte-equal across both paths.
  - **Path-like non-suffix flag coverage:** fixtures covering `--report-file <path>` (cmd_audit) and `--output <path>` (cmd_build_*) prove the `ARG_SPECS` override table held.

**Description:** Five small functions, mostly mechanical at this point because the refactor is already done. Tier-B body LoC is 484 — small enough that a single dispatch is comfortably tractable.

**Reversion guidance:** Delete test + fixtures. Refactor and Tier-A fixtures remain.

---

### TASK-006: Tier-C conformance fixtures (happy-path + codemod-correctness gates only)

- **Status:** Done
- **Priority:** medium
- **Files:**
  - tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py (create)
  - tests/scripts/fixtures/plan_ops_pure_core/tier_c/<sub>__<case>.{payload,expected}.json (create, ~7-9 pairs)
- **Dependencies:** TASK-003H
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_plan_ops_pure_entrypoints_tier_c.py`
- **Acceptance criteria:**
  - Per-function tmp-git-repo fixture: byte-equal git history (`git log --oneline --all` plus tree-hashes of every commit), byte-equal `_run_log.jsonl` (validates hash-chain), byte-equal lock-state file, identical exit codes per error path, byte-equal directory snapshot for `cmd_decompose_plan` outputs — all on the **happy path only**.
  - Required fixtures (named, happy-path-shaped):
  - cmd_commit_task: clean-commit happy path; `out_of_scope_observed=true` reconciler-partition path
  - cmd_fail_task: paused-run-with-authorization-source (codifies the hand-fix from TASK-003)
  - cmd_log_event: rejected-event (not in `ALLOWED_LOG_EVENTS`) — exercises `errors[]` round-trip
  - cmd_acquire_lock: stale-lock-takeover (codifies TASK-003's `_run_acquire_lock` shape)
  - cmd_auto_validate_divergence: applicable-envelope happy path (no test-command-times-out — that's underlying behavior, not codemod equivalence)
  - cmd_decompose_plan: malformed-plan error path (exercises `_run_*` error-envelope shape)
  - cmd_block_dependents: empty-cascade short-circuit (exercises early-return `_run_*` shape)
  - **`cmd_commit_task` dismissed-finding-ids fixture:** at least one happy-path fixture for the `--narrow-remediation-tag` + `--dismissed-finding-ids 1,2,3` combo proving that `_validate_commit_task_payload` (introduced by TASK-003) returns `errors[]` for invalid combos and the CLI's argparse layer keeps producing exit-code-2 banners for the same combos. Asserts both paths reject `--dismissed-finding-ids 1,x` (CLI: exit 2 + banner; pure: `errors[{"code":"dismissed-finding-ids-not-int"}]`).
  - **commit-task body f-string preservation:** byte-equal stderr verification on `bad --task-id: <invalid>` error path (line 6887 / its hand-fixed equivalent post-TASK-003) — directly load-bearing for codemod correctness.

**Out of scope (intentionally dropped from v2 — see Q5 resolution):**
- Crash-recovery (`SIGTERM` between commit and run-log append): tests underlying state-mutator robustness, not codemod equivalence. The codemod is a deterministic AST rewrite — no LLM in the loop — so cross-path drift under SIGTERM is impossible by construction.
- Process-tree / orphan-zombie assertions: same reasoning. Belongs in a separate state-mutator hardening plan if the parent plan still wants it.
- Hermetic env scrubbing: was a defense against subprocess-driven divergence between bash and `_run_*`; codemod determinism makes this redundant.
- Subprocess re-entrancy guard (`IN_PLAN_OPS_SUBPROCESS=1`): "may already exist; if not, add it" was a discovery-shaped acceptance criterion; out of scope for a codemod plan.

**Description:** Tier-C is now a regression net for the **codemod's correctness** (f-string preservation, dismissed-finding-ids validation shape, error-envelope round-trip on the named state mutators) plus structural happy-path round-tripping. The richer adversarial behavior tests that motivated v1's TASK-006 are deferred — they protect against drift in the underlying `cmd_*` logic, which is unchanged by an AST rewrite. If a future hardening plan wants them, they slot in cleanly against the same `_run_*` surface.

**Reversion guidance:** Delete test + fixtures. Refactor, Tier-A, Tier-B fixtures all remain.

---

## Open questions for refinement (post v2 status)

This section tracks what the v2 refinement pass resolved and what still needs human decision. Items moved to **Resolved** carry brief rationale; items still open have a recommendation but await the user's call.

### Resolved during v2 refinement (2026-04-30)

- **Codemod permanence (was v1 Q1).** Checked in under `tools/codemods/plan_ops_pure_core_extract.py` per Codex review (small file, audit value). TASK-001 + TASK-001B hold this.
- **Byte-equal conformance gate relaxation (was v1 Q2).** Replaced hand-fixture density gate with TASK-000B baseline (12-15 fixtures covering terminator-sensitive branches) + Tier A/B/C smoke matrices. The deterministic codemod makes per-function fixture count non-load-bearing; baseline is the regression gate.
- **Special-case list completeness (was v1 Q4).** Skip list expanded from 3 → 9 functions during v2/v3 ground-truth audit (cmd_gates, cmd_filter_schedule, cmd_commit_task, cmd_fail_task, cmd_audit, cmd_resolve_read_targets, cmd_build_claude_dispatch_input, cmd_build_codex_dispatch_input, cmd_build_gemini_dispatch_input). `cmd_parse_schedule` was removed from the skip list in v3 because it is a required-`--stdin` JSON command, not a stdin-vs-file branch; TASK-001 now preserves raw stdin text so the codemod can rewrite it without moving JSON parsing into the shim. TASK-001B's dry-run report is the gate that surfaces any further candidates before TASK-002.
- **Path-flag heuristic robustness (was v1 Q5).** Replaced suffix-only heuristic with `ARG_SPECS` table built from argparse introspection + explicit override list (codified in TASK-001 AC). TASK-001B's dry-run validates every `value_kind` resolves to a non-`unknown` value before TASK-002 fires.
- **Codemod review by code-reviewer subagent (was v1 Q6).** Now an explicit AC in TASK-002.
- **`_emit/_die` terminator semantics.** TASK-000A introduces `_result()` and `_emit_or_die()` helpers so the codemod's per-call rewrite shape is `_emit(args, expr, exit_code=N)` → `return _result(expr, exit_code=N)` (semantics-preserving for both mid-body and terminal calls).

### Resolved during v2 user pass (2026-04-30)

- **TASK-011/012/013 disposition in parent plan (v2 Q1).** PURE_CORE_CODEMOD will execute as a standalone sub-plan in isolation. Cross-plan task references (e.g. `PURE_CORE_CODEMOD/TASK-003` in parent's `depends_on`) are NOT introduced — they would confuse the orchestrator's schedule walker and Codex plan-review. Post-completion, edit `docs/plans/MCP_MIGRATION/00_INDEX.json` to: (a) mark TASK-011/012/013 chunks as `status: "Superseded"`; (b) trim TASK-004's `depends_on` from `["001","003","011","012","013"]` to `["001","003"]`. Follow-on tasks (TASK-004 onward) treat the pure-core layer as already-done by virtue of PURE_CORE_CODEMOD's completion. Apply this edit once PURE_CORE_CODEMOD's TASK-006 lands, not before.
- **Schedule wiring for the new sub-plan (v2 Q2).** Sidecar `docs/plans/MCP_MIGRATION/PURE_CORE_CODEMOD/00_INDEX.json` is non-negotiable. Created alongside this v2 pass (see Cross-references). Lets the orchestrator decompose / schedule / gate via the standard `/implement-plan` protocol.
- **Tier-C fixture authoring cost vs. value (v2 Q5).** With the codemod's determinism (AST rewrite, no LLM in the rewrite loop), cross-path drift between bash CLI and `_run_*` is impossible by construction. The v1 adversarial fixtures (SIGTERM crash-recovery, process-tree assertions, hermetic env scrubbing, subprocess re-entrancy guard) were defending against rewrite drift — that defense is now redundant. TASK-006 trimmed to **happy-path round-tripping + codemod-correctness gates only** (f-string preservation, dismissed-finding-ids shape). Fixture count drops from ~12 to ~7-9. The dropped adversarial coverage targets the underlying state-mutator behavior; if a future hardening plan wants it, it slots cleanly against the same `_run_*` surface this plan delivers.
- **Two wrapper/reviewer follow-on bugs (v2 Q4).** Filed separately as `docs/bugs/BUG_WRAPPER_PLAN_REVIEW_LATENT_2026-04-30.md` to be addressed after PURE_CORE_CODEMOD completes; this plan continues unaffected.
- **Cost re-estimate (v2 Q3).** Removed — user is on subscription, the estimate was informational only.
- **`__plan_ops_text_output__` convention placement (v2 Q6, revised in v3).** Documented as a closed-vocabulary internal-only envelope key inside `_emit_or_die` (TASK-000A AC updated). `cmd_audit` remains skip-listed and hand-fixed in TASK-003 because the current non-JSON CLI path is a custom renderer plus `sys.exit`; the hand-fix returns structured data for MCP/SKILL callers while preserving rendered CLI text through the text-output marker. If a second `cmd_X` ever needs the same human-readable-render-on-CLI shape, the convention is already in place.

### Still open

(none — all v2 user-pass questions resolved 2026-04-30)

---

## Cross-references

- Parent plan: `docs/plans/MCP_MIGRATION/PLAN_MCP_MIGRATION.md` — TASK-011/012/013 are the sub-tasks this plan supersedes; TASK-004/005/006/007/008/009/010 remain unchanged and consume the `_run_*` layer this plan delivers.
- Failure run-log: `docs/plans/_run_log.jsonl`, search `run_id: "20260430T033005"` — captures the dispatch envelope, the implementer's `plan-incorrect` diagnosis, and the run_end pause event.
- Original TASK-002 halt note: `PLAN_MCP_MIGRATION.md` execution-log line 446 (run `20260429T232737`, paused) — the prior plan-incorrect diagnosis whose root cause this plan now addresses.
- Implementer's recommended 5-way split (TASK-011a/b/c/d/e): captured in the failed dispatch's `report.summary[4]` for run `20260430T033005`. Useful as an alternative shape for comparison; this plan's codemod approach is strictly preferable per the cost/confidence analysis above.
- v2 refinement evidence:
  - Codex coding-implications review: `/tmp/codex_codemod_review.md` (20152 bytes, 2026-04-30T07:58Z) — produced via `codex exec --full-auto` against the v1 draft + `plan_ops.py` samples. The review introduced TASK-000A, TASK-000B, TASK-001B, TASK-004-HARNESS (renumbered to TASK-003B in v2), and the `_result()`/`_emit_or_die()` contract. Source prompt at `/tmp/codex_review_prompt.txt`.
  - Gemini code-search audit: dispatched twice (`/tmp/gemini_audit_prompt.txt`, `/tmp/gemini_audit_prompt2.txt`) — both runs hit a 429 rate limit on the Google API and produced no output. The orchestrator instead performed the equivalent ground-truth scan first-party (using grep + sample reads of `cmd_fail_task`, `cmd_audit`, `cmd_resolve_read_targets`, `cmd_build_*_dispatch_input`) which is what surfaced the +6 skip-list candidates beyond Codex's enumeration.
  - Ground-truth scan output (informal): captured in this conversation's working-memory; key citations baked into TASK-001/003 ACs (line numbers 7627-7652, 11060-11062, 11673-11674, 11734, 11741+, 12039-12042). Not persisted to a separate artifact because the plan body now carries the load-bearing references.

## Task graph (v4)

```
TASK-000A (contract) ─┬─→ TASK-001 (codemod) ──→ TASK-001B (dry-run) ──→ TASK-002 (apply)
                      │                                                          │
TASK-000B (baseline) ─┘                                                          └─→ TASK-003A (gates)
                                                                                       → TASK-003B (filter-schedule)
                                                                                       → TASK-003C (commit-task)
                                                                                       → TASK-003D (direct-output commands)
                                                                                       → TASK-003E (Claude dispatch builder)
                                                                                       → TASK-003F (Codex/Gemini builders)
                                                                                       → TASK-003G (completion audit)
                                                                                       → TASK-003H (harness)
                                                                                         ├─→ TASK-004 (Tier-A fixtures)
                                                                                         ├─→ TASK-005 (Tier-B fixtures)
                                                                                         └─→ TASK-006 (Tier-C fixtures)
```

13 tasks in v4 vs 8 in v2. TASK-004/005/006 can run in parallel after TASK-003H lands. Critical path: 000A → 001 → 001B → 002 → 003A → 003B → 003C → 003D → 003E → 003F → 003G → 003H → 004 (or 005, or 006). 000B runs in parallel with 001/001B.

## Execution log — 20260430T145307 (paused)

Starting SHA: `e88d674f9d6e46d763c67ee96d596d39e4ca62ef`  → Ending SHA: `37b9b036f559dd9299abe189b27ddd37262d3b05`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 000A | codex | claude | ship [sandbox-divergence] | 2dde16003fc4 | _emit/_die terminator contract |
| 001 | codex | claude | ship-with-fixes | d56aa412aec8 | libcst codemod |
| 000B | codex | claude | ship-with-fixes | ccc102ef5a9e | CLI baseline 17 fixtures |
| 001B | codex | claude | ship | 37b9b036f559 | dry-run report 29/9/0 |
| 002 | codex | none | PAUSED | - | codemod applied + partial patches; 37 tests fail; awaiting user |

## Operator follow-up — TASK-002 completion

Claude/Codex paused on TASK-002 after applying the codemod with 37 remaining test failures and a missing codemod `--skip-from-report` implementation. Per user guidance, the operator completed the task by hand rather than reverting: added `--skip-from-report` support to `tools/codemods/plan_ops_pure_core_extract.py`, fixed generated `plan_ops.py` regressions, corrected review-schema/doc drift that blocked the existing suite, normalized `plan_ops.py` line endings/whitespace, and reran the gates.

Completion commits: `78c3a9319969` (TASK-002 code/docs/tests) and `0ca67d2` (mark TASK-002 done in `00_INDEX.json`).

Verification:

- `venv/bin/python -m pytest tests/tools/test_plan_ops_pure_core_extract.py -q` → 7 passed.
- `venv/bin/python -m pytest tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_pure_core_baseline.py -q` → 1051 passed, 2 skipped.

## Execution log — 20260430T192352 (partial)

Starting SHA: `a1cf4760f65b53fe7055028a979631d243201e28`  → Ending SHA: `7d5007cb4ffe257aa1f430065fccbe0a74ed9ae4`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003A | codex | claude | ship | 7d5007cb | 3 minor nits recorded |

## Execution log — 20260430T195058 (partial)

Starting SHA: `d74bd63cec874a775b252ca17c08082c49695f5f`  → Ending SHA: `3caecb4e1e3ea5344c533f82c3cfc203842dcc6b`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003C | claude | codex | clean | 3caecb4e |  |

## Execution log — 20260430T202130 (partial)

Starting SHA: `e7c870820265dae908e933d0591288fb09f60526`  → Ending SHA: `cbb87b660eca44e6628dfa0560a4b08efe86ec73`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003E | claude | codex | clean | cbb87b6 |  |

## Execution log — 20260430T220154 (paused)

Starting SHA: `67036d6afaa8f6a0b0cf439a6caeade9b7448ee1`  → Ending SHA: `67036d6afaa8f6a0b0cf439a6caeade9b7448ee1`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003H | claude | codex | needs-rework [narrow-remediation] | - | D.5 partial-agreement; narrow-remediation succeeded; binding re-review still needs-rework; paused for user instruction |

## Execution log — 20260430T222710 (paused)

Starting SHA: `b02045c8002c99dbf14574807ac2ce733a2b9466`  → Ending SHA: `b02045c8002c99dbf14574807ac2ce733a2b9466`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| TASK-004 | claude | none | paused (wrapper scope_violation; tests pass; work preserved) | - | Implementer mis-reported files_changed as directory path; 68 in-scope files preserved on disk; awaiting user instruction |
