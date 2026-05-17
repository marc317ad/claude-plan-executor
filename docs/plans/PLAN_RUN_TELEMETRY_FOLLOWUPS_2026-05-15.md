# PLAN — Follow-ups surfaced by PLAN_DEPRECATE_CLAUDE_CLI_FOLLOWUPS_2026-05-15 (run 20260515T122941)

**Status:** Pending
**Created:** 2026-05-15
**Base branch:** main
**Parent run:** `20260515T122941` against `docs/plans/PLAN_DEPRECATE_CLAUDE_CLI_FOLLOWUPS_2026-05-15/`

## Goal

Close three operational follow-ups surfaced during the parent run. None of them blocked task commit (reconcile-batch's per-task partition contained #1, D.5 adjudication contained #2, post-commit normalization contained #3), but each leaves misleading telemetry, an avoidable plan-author footgun, or recurring repo churn that costs review attention on every run.

1. **Wrapper scope-diff is unreliable under parallel batches.** When two sibling tasks dispatch in the same batch and share the working tree, each wrapper's `snapshot_baseline` → post-dispatch diff sees the other sibling's writes as either cross-batch leakage (false positive `observed_delta_*`) or as missing observations on its own write (false negative `declared_files_changed: []`). The downstream `plan_ops__reconcile_batch` partition still routes commits correctly, but the per-wrapper scope envelope is wrong, which corrupts `run_log.jsonl` post-mortems and makes the `wrapper_scope_diff_mismatch` warning channel noisy/useless.
2. **Plans can name symbols that don't exist in the codebase.** Parent run TASK-003's AC named `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY`; the canonical Python-side map is `_AGENT_DISPATCH_CONTEXT_DEF_BY_TEMPLATE`. The reviewer correctly flagged a load-bearing finding, the D.5 adjudicator dismissed it as spec-deference, and the run shipped — but at the cost of one full D.5 round-trip plus a hand-fix. A pre-dispatch invariant in `lint-plans` (or in `plan-author`'s output validation) would catch this at plan-authorship time.
3. **`_run_log.jsonl` keeps churning CRLF in the working tree.** Every parent-run commit carried a single-byte normalization diff on `_run_log.jsonl` despite repo `.gitattributes` declaring `* text=auto eol=lf`. The wrapper's appender writes with the platform default newline translation, and the `auto` detection is fragile for a file that mixes long single-line JSON records. Result: every `git status` between runs shows a phantom dirty file, and every `commit-task` carries an unrelated normalization delta.

## Findings

### 1. Wrapper scope-diff under parallel batches

**Observed in parent run telemetry** (`docs/plans/PLAN_DEPRECATE_CLAUDE_CLI_FOLLOWUPS_2026-05-15/_run_log.jsonl`, run `20260515T122941`):

- TASK-001 wrapper envelope: `scope.declared_files_changed: []`, `scope.observed_delta_tracked` containing TASK-002's `docs/plans/SKILL_bash_dispatch_migration/probe_results.md`.
- TASK-002 wrapper envelope: `scope.declared_files_changed: []`, `scope.observed_delta_*` empty despite the wrapper having actually written `probe_results.md`.

**Root cause** (`plan_claude_dispatch.py:672` + `_dispatch_cleanup.py:303`): `snapshot_baseline(repo_root)` captures the *whole* working tree (tracked + untracked) at wrapper entry. When TASK-001's wrapper enters slightly later than TASK-002's, TASK-002's already-written file is part of TASK-001's baseline as untracked and gets attributed to TASK-001's `observed_delta_*` post-dispatch diff. Symmetrically, when TASK-002's wrapper exits while TASK-001 is mid-flight, the post-dispatch snapshot can omit the just-written file from the diff because the baseline already included it.

`plan_ops.py:14431` and the orchestrator-side `reconcile-batch` step already paper over this with per-task partition (each commit only stages paths in the task's declared `Files:`), so commits land correctly. The damage is confined to the wrapper's per-task scope envelope — the field the wrapper publishes is the post-snapshot whole-tree diff, not the task's actual contribution.

**Fix shape (TASK-001):** narrow the wrapper's reported `observed_delta_*` to the intersection of the post-dispatch whole-tree diff and the task's declared `Files:` ∪ `_extract_observed_files_changed(envelope)` (the agent's self-reported writes, already parsed for diff metadata at `plan_claude_dispatch.py:720`). Cross-batch leakage drops out of the envelope; the agent's actual writes still appear; reconcile-batch's downstream partition is unchanged.

Out of scope for this fix: introducing per-task working-tree isolation (worktrees / overlays). That is a much larger refactor with its own performance trade-offs; the narrowed-diff fix removes the misleading telemetry without changing the parallelism model.

### 2. Plan-author writes ACs naming symbols that don't exist

**Observed in parent run:** TASK-003's AC named `_AGENT_DISPATCH_TEMPLATE_CONTEXT_KEY` (a constant that has never existed in `plan_ops.py`). The canonical map is `_AGENT_DISPATCH_CONTEXT_DEF_BY_TEMPLATE`. The implementer wrote the test against the real map; the reviewer flagged AC-divergence (`needs-rework`, severity `important`); D.5 adjudicator dismissed as spec-deference (`partial-agreement`, `dismissed_finding_ids=[0]`); commit landed with `narrow_remediation_tag`.

The full round-trip cost: one Codex review (24s), one D.5 Agent adjudication (~30s), one orchestrator-side hand-fix (alias removal), one re-review. Plan-author should have caught the symbol-doesn't-exist at authorship time.

**Authoring context:** `plan_ops__lint_plans` already scans the markdown for shape violations (missing sections, missing fields). It does not cross-reference AC-named symbols against the source tree. The check is cheap: extract every backtick-wrapped identifier from each AC bullet, ripgrep each one against the task's `Files:` set, surface "AC names symbol `X` not found in any declared file" as a `warning` finding.

**Fix shape (TASK-002):** extend `lint-plans` with a new check `ac_symbol_groundedness` that:

- Parses backtick-wrapped tokens out of each AC bullet (regex `` `([A-Za-z_][A-Za-z0-9_]*)` ``).
- Filters to tokens that look like Python identifiers ≥ 4 chars (avoid false positives on `git`, `cp`, English words).
- For each candidate, runs `git grep -lF "<token>"` restricted to the task's declared `Files:` list (plus any `Implementation notes` file references the parser already extracts).
- Emits a `warning`-level finding when zero hits AND the token is not in an allowlist of intentional placeholders (e.g. `NEW_CONSTANT_NAME`, `<symbol>`).
- Warning, not error: the AC might legitimately name a *new* symbol to be created. The plan-author / human reviewer disposition decides whether to dismiss.

The check fires in `plan_ops__lint_plans` (already called by the parent decomposer and reachable as a standing audit). It does NOT block Phase 0 preflight or Phase 1.5 plan-review unless the operator opts in via a `--strict-ac-symbols` flag; the default mode surfaces the warning in the lint report.

### 3. `_run_log.jsonl` CRLF churn

**Observed across parent run commits:** every `commit-task` carried a diff on `_run_log.jsonl` containing only `\r` byte removals on lines untouched by the current event. The `.gitattributes` declaration `* text=auto eol=lf` plus the explicit `*.py text eol=lf` / `*.sh text eol=lf` rules normalize on commit but do not prevent the working-tree CRLF from re-appearing between runs. The `auto` detection is statistical — for a file that grows by single multi-kilobyte JSON records with no `\r` in payload, the heuristic flickers.

**Root cause:** `plan_ops.py:4544` opens the run log in text mode (`"a", encoding="utf-8"`) and writes `line + "\n"`. On Python 3, text-mode `open()` translates `\n` to `os.linesep` on write. On Linux/WSL2 `os.linesep` is `\n` so this should be a no-op. But the file lives on a `/mnt/d/` DrvFs mount; the underlying NTFS layer plus any Windows-side process that touches it (editors, the WSL2 file watcher, Git for Windows) can re-introduce CRLF, and once the file has CRLF anywhere, `git status` reports the whole file as modified after the next LF-only append.

**Fix shape (TASK-003):** belt + suspenders.

- **A. Bind newline explicitly in the appender.** Change `RUN_LOG_PATH.open("a", encoding="utf-8")` at `plan_ops.py:4544` (and the read+verify pair at `:4547` / `:4552`) to `open("a", encoding="utf-8", newline="\n")`. This makes the Python side deterministic regardless of platform / mount.
- **B. Pin `.gitattributes` for JSONL.** Add explicit `*.jsonl text eol=lf` after the existing `*.py text eol=lf` line in `.gitattributes`. The `auto` mode is removed from the JSONL detection path; LF is forced on every checkout / normalize-on-commit.
- **C. One-shot normalization.** Run `git add --renormalize "*.jsonl"` once on the fix commit so any historical CRLF in tracked JSONL is flattened in a single audit-able commit.

The fix is independent of #1 and #2 and lives entirely in two files (`plan_ops.py:4544` + `.gitattributes`).

## Decisions

1. **Three independent tasks.** No shared file scope:
   - TASK-001: `plugins/plan-executor/scripts/plan_claude_dispatch.py` (+ a unit test).
   - TASK-002: `plugins/plan-executor/scripts/plan_ops.py` (the `lint_plans` subcommand body) (+ a unit test).
   - TASK-003: `plugins/plan-executor/scripts/plan_ops.py` (`_append_run_log` only) + `.gitattributes` + a one-shot renormalize commit (no test — `.gitattributes` is its own self-test).
   - TASK-001 and TASK-003 both touch `plan_ops.py` only if TASK-003 lands the appender fix there — yes, they overlap on `plan_ops.py`. To keep batches parallel-safe, TASK-001's *Files:* must NOT include `plan_ops.py`; the wrapper-scope-diff fix lives entirely in `plan_claude_dispatch.py`. TASK-003 owns the `plan_ops.py` edit. Schedule will run TASK-001 + TASK-002 in batch 1 (disjoint), TASK-003 in batch 2.
2. **TASK-001 picks intersection-narrowing, not working-tree isolation.** Isolation (per-task git worktrees) is a much larger refactor and has its own cost. The narrower fix (intersect observed-delta with declared + agent-reported writes) removes the misleading telemetry without changing the parallelism model.
3. **TASK-002 is a `lint_plans` extension, not a Phase 0 gate.** The AC-symbol check is a warning, not an error: ACs legitimately name *new* symbols. Wiring it into `schema-valid` would block correct plans; surfacing it in the lint report gives plan-authors a fast feedback loop without changing the preflight contract.
4. **TASK-003 uses both fixes (newline= and .gitattributes).** The Python-side bind makes the appender deterministic; the `.gitattributes` pin makes the repo-side normalization deterministic. Either alone would probably suffice in steady state; both together remove the recurring noise without leaving a single point of fragility.

## Scope

In scope:

- `plugins/plan-executor/scripts/plan_claude_dispatch.py` — narrow `_merge_scope_with_cleanup` (or its caller) so the published `observed_delta_*` lists are the intersection of the whole-tree diff with `declared ∪ agent_reported_files_changed`.
- `plugins/plan-executor/scripts/plan_ops.py` — extend `lint_plans` with `ac_symbol_groundedness` check; bind `newline="\n"` in `_append_run_log`'s three `RUN_LOG_PATH.open(...)` sites.
- `.gitattributes` — add `*.jsonl text eol=lf`.
- `tests/scripts/test_plan_claude_dispatch.py` (or the existing wrapper-scope test, whichever owns the diff path) — pin the intersection narrowing.
- `tests/scripts/test_plan_ops_lint_plans.py` (or the closest existing lint test) — pin the new AC-groundedness check.

Out of scope:

- Per-task working-tree isolation / git-worktree-per-dispatch.
- Wiring `ac_symbol_groundedness` into Phase 0 `schema-valid` or Phase 1.5 plan-review verdicts.
- A migration of `_run_log.jsonl` to a binary format (length-prefixed framing, sqlite). The append + verify-on-read shape is fine.
- Backfilling `narrow_remediation_tag` retroactively on past commits.
- Changes to `_extract_observed_files_changed`'s parsing (it already does the right thing; TASK-001 just consumes it differently).

## Tasks

### TASK-001: Narrow wrapper `observed_delta_*` to intersection of whole-tree diff and (declared ∪ agent-reported writes)

- **Status:** Pending
- **Priority:** high
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_claude_dispatch.py`
  - `tests/scripts/test_plan_claude_dispatch.py`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch.py -k "scope or observed_delta or intersection"`
- **Acceptance criteria:**
  - In `plan_claude_dispatch.py`, the wrapper's published `envelope.scope.observed_delta_tracked` and `envelope.scope.observed_delta_untracked` (the lists set in `_merge_scope_with_cleanup` at `plan_claude_dispatch.py:380–384`) are filtered through `task_in_scope_paths = set(declared_files_changed) | set(_extract_observed_files_changed(envelope))` before publication. Paths not in `task_in_scope_paths` are dropped.
  - The agent-reported `files_changed` extracted at `plan_claude_dispatch.py:720` already exists in the function-local `_observed` (currently `# noqa: F841` because unused); wire it into the narrowing — do NOT call `_extract_observed_files_changed` twice.
  - The narrowing happens regardless of `scope_violation_detected` / `scope_misreport_detected` (those flags continue to be computed on the unfiltered cleanup result so they still warn loudly on real scope violations; only the published *observed delta lists* are narrowed).
  - `scope.declared_files_changed` is untouched by this change (it is already the trusted top-level input, not a diff).
  - Add a unit test `test_observed_delta_excludes_cross_batch_leakage`: construct a fake `_merge_scope_with_cleanup` input where `cleanup_result["restored"]` contains both a task-declared file and a sibling-task file (the cross-batch leak); assert the published `observed_delta_tracked` contains only the declared file. The sibling file is dropped from the envelope but the underlying cleanup result is unchanged (verify both via the same test).
  - Add a second unit test `test_observed_delta_includes_agent_self_reported_writes`: construct an envelope whose inner `result.files_changed` lists a path that is NOT in `declared_files_changed` (a scope misreport). Assert the published `observed_delta_*` retains that path (so misreports still surface).
  - The four-way `scope_violation_detected` / `scope_misreport_detected` / `failed_paths` / `baseline_captured` envelope fields are byte-identical pre- and post-fix for the existing fixtures in `tests/scripts/test_plan_claude_dispatch.py` (regression pin).
- **Implementation notes:** Read `plan_claude_dispatch.py:335–395` (`_merge_scope_with_cleanup`) and `:680–760` (the cleanup wiring) end-to-end before editing. `_extract_observed_files_changed` is already defined at `:398`; reuse it. The narrowing should be a 5–10 line addition inside `_merge_scope_with_cleanup` — accept a new kwarg `task_in_scope_paths: set[str]`, filter `observed_tracked` / `observed_untracked` against it, default `None` means "no narrowing" for back-compat in any direct unit-test caller. The call site at `:760` passes `set(declared) | set(_observed)`.
- **Reversion guidance:** Remove the new kwarg + the filtering lines from `_merge_scope_with_cleanup`; revert the call site to the pre-fix signature; delete the two new test cases. The wrapper envelope returns to publishing the unfiltered whole-tree diff and cross-batch leakage reappears as misleading telemetry.

### TASK-002: Add `ac_symbol_groundedness` check to `lint_plans`

- **Status:** Pending
- **Priority:** medium
- **Agent:** claude
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `tests/scripts/test_plan_ops_lint_plans.py`
  - `tests/scripts/fixtures/lint_plans/ac_names_missing_symbol.md`
- **Dependencies:** none
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_ops_lint_plans.py -k "ac_symbol or groundedness"`
- **Acceptance criteria:**
  - Extend the `lint_plans` subcommand body in `plan_ops.py` (search for `def cmd_lint_plans` or the `lint-plans` argparse branch — read the existing implementation end-to-end before editing) with a new check named `ac_symbol_groundedness`.
  - The check, for each TASK-NNN block in the plan markdown:
    - Parses every backtick-wrapped token out of the AC bullet list (regex `` `([A-Za-z_][A-Za-z0-9_]+)` `` — note the `+` to require ≥ 2 chars beyond the leading char, total length ≥ 3; tune the threshold to ≥ 4 chars if needed to suppress noise from words like `git`).
    - Filters out tokens that match common English-word / shell-builtin allowlist (start with: `git`, `pytest`, `python`, `bash`, `sh`, `cp`, `mv`, `rm`, `ls`, `grep`, `find`, `awk`, `sed` — extend as needed). Also filters out tokens that contain only lowercase letters and are < 6 chars (likely English).
    - For each surviving candidate token, runs `git -C <repo_root> grep -lF "<token>"` restricted to the task's `Files:` list. If `Files:` is empty or the grep finds zero hits, the token is flagged.
    - Surfaces a `warning`-severity finding `{"severity":"warning", "check":"ac_symbol_groundedness", "task_id":"NNN", "token":"<token>", "ac_bullet_index":<int>, "message":"AC names backtick-wrapped symbol `<token>` but no declared file in Files: contains it"}`.
  - The check is opt-in via a new `--check ac_symbol_groundedness` flag OR runs always at `warning` severity (decision: runs always, since warnings do not block lint exit code).
  - The check returns no findings when the AC's named symbol IS present in one of the declared files (positive case fixture).
  - Add three test cases:
    - `test_ac_symbol_groundedness_flags_missing_token`: a fixture plan whose AC names a token not present in `Files:`; assert exactly one finding with the expected shape.
    - `test_ac_symbol_groundedness_passes_when_symbol_present`: a fixture plan whose AC names a token that IS in one of the declared files; assert zero findings.
    - `test_ac_symbol_groundedness_skips_english_allowlist`: a fixture whose AC bullets contain `` `git` `` and `` `pytest` `` (real backticked tokens but in the allowlist); assert zero findings for those tokens.
  - The new check appears in the `lint-plans --json` output under the existing `checks[]` (or equivalent) array; do NOT introduce a new top-level key.
- **Implementation notes:** Find the lint-plans implementation by `grep -n "def cmd_lint_plans\|\"lint-plans\"" plugins/plan-executor/scripts/plan_ops.py`. The TASK-NNN parser is already used elsewhere; reuse `_parse_task_blocks` (or whatever the symbol is — confirm by reading `plan_ops.py:2901`–end of the parser). For the `git grep` invocation, use `subprocess.run(["git", "-C", repo_root, "grep", "-lF", token, "--", *files], capture_output=True, text=True, check=False)` and treat `returncode == 1` (no hits) as "missing"; `returncode == 0` (hits) as "present". The token-extraction regex must match across the AC bullet list AFTER the existing AC-bullet parser already split bullets — re-extracting from raw markdown risks pulling backticks from other sections. Pull the parsed AC list, then regex over each bullet string. Read the existing `lint-plans` `checks[]` envelope shape before adding the new check so the output schema doesn't drift.
- **Reversion guidance:** Remove the `ac_symbol_groundedness` check function and its registration in the lint-checks dispatch table; delete the three test cases and the two fixtures. Plan-authors lose the warning; AC-divergence findings return to surfacing only at Phase D cross-review time.

### TASK-003: Stop `_run_log.jsonl` from accruing CRLF churn

- **Status:** Pending
- **Priority:** medium
- **Agent:** codex
- **Files:**
  - `plugins/plan-executor/scripts/plan_ops.py`
  - `.gitattributes`
- **Dependencies:** none
- **Test command:** `deferred (TASK-003) — one-shot renormalize commit + a manual reproduction is the verification; no automated regression test is added (the .gitattributes pin is self-verifying via git's normalization-on-commit, and the newline= bind is a one-line correctness fix that does not benefit from a Python-side test on a non-Windows CI host).`
- **Acceptance criteria:**
  - In `plan_ops.py:_append_run_log` (currently `plan_ops.py:4539`), change all three `RUN_LOG_PATH.open(...)` calls (lines 4544, 4547, 4552) to pass `newline="\n"` in addition to the existing `encoding="utf-8"`. The write call's `fh.write(line + "\n")` is unchanged.
  - In `.gitattributes`, add a new line `*.jsonl text eol=lf` immediately after the existing `*.sh   text eol=lf` line (keep alphabetical / file-extension grouping consistent with the surrounding lines). Do NOT change any other line in `.gitattributes`.
  - The implementer runs `git add --renormalize "*.jsonl"` once on the same commit so any historical CRLF in tracked `.jsonl` files (including past `_run_log.jsonl` entries) is flattened in this commit's diff. Document the renormalize step in the commit body.
  - The implementer manually verifies the fix in the same dispatch: append a one-line test event via the canonical path (`plan_ops__log_event` or `plan_ops.py log-event`), then `git status --short`; the `_run_log.jsonl` line MUST be the only change AND the diff hunk MUST contain no `\r` markers (run `git diff -- '*.jsonl' | grep -c $'\r'` and assert zero).
  - No other behavior changes: the run-log append + verify-on-read shape is unchanged, the event schema is unchanged, the audit trail is unchanged.
- **Implementation notes:** This is intentionally a Codex task because the change is mechanical (three `open()` call sites + one `.gitattributes` line + one `git` command), and Codex's tighter scope diff is well suited to a two-file patch. Read `plan_ops.py:4539–4556` end-to-end first; the read+verify pair at `:4547` / `:4552` must also pass `newline="\n"` — otherwise the post-write tail-verification could re-translate and mask the fix. The `git add --renormalize` step uses `git`, not the `plan_ops__commit_task` staging path, but stays inside the canonical `Files:` set because the renormalization touches only `*.jsonl` files which are tracked under `docs/plans/**`. The commit message body documents the renormalize step so the audit trail is clear.
- **Reversion guidance:** Remove the `newline="\n"` kwarg from the three `open()` calls; remove the `*.jsonl text eol=lf` line from `.gitattributes`; run `git checkout HEAD~1 -- '*.jsonl'` only if the operator explicitly authorizes restoring the pre-renormalized blobs (otherwise the renormalized state stays as the new baseline).

## Verification

Before marking this plan done:

1. `venv/bin/pytest -q tests/scripts/test_plan_claude_dispatch.py` — TASK-001.
2. `venv/bin/pytest -q tests/scripts/test_plan_ops_lint_plans.py` — TASK-002.
3. `venv/bin/pytest -q tests/scripts/test_plan_ops.py` — broad regression on the parent run's invariants (run-log append, plan-ops state machine).
4. `git status --short` after a clean checkout and a single `plan_ops__log_event` call: only `_run_log.jsonl` should be dirty, and `git diff -- '*.jsonl' | grep -c $'\r'` should return `0`. TASK-003 verification.
5. `$PYTHON plugins/plan-executor/scripts/plan_ops.py audit --json` — standing audit must still pass.

Manual smoke for TASK-001:

1. Run `/implement-plan` against a 2-task plan whose tasks both write to disjoint files in the same batch.
2. Inspect both wrappers' published envelopes in `_run_log.jsonl`'s `claude_dispatch_done` events.
3. For each task, the envelope's `scope.observed_delta_tracked` must contain ONLY paths from that task's declared `Files:` ∪ the agent's self-reported `files_changed`. Cross-batch leakage must be absent.

Manual smoke for TASK-002:

1. Construct a plan markdown whose TASK-NNN AC names `` `_DEFINITELY_DOES_NOT_EXIST_SYMBOL` `` inside the AC bullet.
2. Run `plan_ops__lint_plans` against it.
3. The output must contain one `ac_symbol_groundedness` finding naming the missing token.
4. Add a real token from one of the declared files and re-run; the finding must disappear.
