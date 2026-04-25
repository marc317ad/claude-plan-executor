# Run friction notes — PLAN_GEMINI_INTEGRATION_2026-04-25

**Status:** seed for future plan-iteration; do not treat as a `PLAN_*.md` (no strict schema).

**Purpose:** capture the failure modes that surfaced while running `/implement-plan docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/ --task-ids 001,002,003,004 --parallel 2` so a follow-up planning pass can scope the upstream fixes. Generated from the orchestrator's in-context observations during runs `20260425T123302` and `20260425T131942`; deeper code-grounding (`grep`, archaeology) is left to the next planning iteration.

## Run trajectory (compressed)

| Phase | Run id | Outcome | Notes |
|---|---|---|---|
| Phase 0 preflight | `20260425T123302` | pass | All four pre-dispatch gates pass; codex_available=true; clean dirty-tree |
| Phase 1 build-tasks + classifier | `20260425T123302` | valid | 8 children classified; 6 claude / 2 codex |
| Phase 1.5 first Codex plan-review | `20260425T123302` | needs-replan (2 findings) | Finding 0: schedule batches collapse the 001→002→003 dep chain. Finding 1: TASK-004 `files[]` missing `plan_codex_dispatch.py` + `_plan_paths.py` |
| Phase 1.5.5 triage | `20260425T123302` | needs-rework (both load-bearing) | |
| Phase 1.5a plan-author x2 | `20260425T123302` | finding 0 → no-op (`00_INDEX.json` already correct), finding 1 → revised | TASK-004 child gained two file entries |
| Phase 1.5 second Codex plan-review | `20260425T123302` | needs-replan (1 finding) | TASK-004 finding cleared; the schedule-batch finding persists because `compute-schedule` regenerates wide batches every run |
| **HALT** | `20260425T123302` | failed (`plan_review_failed`) | Lock released, no batches dispatched |
| User intervention | — | — | Hand-edit schedule's `batches[]` to use `00_INDEX.json:parallel_batches` filtered to `--task-ids` (= 4 single-task batches) |
| Re-acquire lock + Phase 1.5 third Codex plan-review | `20260425T131942` | approved-with-notes | 0 findings; 3 informational notes |
| Phase B (TASK-001 plan-implementer, claude) | `20260425T131942` | success | 3 new files; pytest 4/4 passed |
| Phase D.1 Codex review (round 1) | `20260425T131942` | needs-rework | Finding 0: mode 100644 (not executable). Finding 1: Row 3 never exercised |
| Phase D.5 third-opinion (sonnet) | `20260425T131942` | partial-agreement | load_bearing=[1] (Row 3); dismissed=[0] (mode-100644 false-positive on `core.fileMode=false` WSL2 — `os.access(X_OK)` returns True regardless) |
| Phase D.2a.6 narrow remediation (plan-remediator) | `20260425T131942` | success | Row 3 now invokes `timeout 30 gemini ...`; report regenerated |
| Phase D.1 Codex re-review (round 2 — first attempt) | `20260425T131942` | wrapper crash | `UnicodeDecodeError` decoding `git diff` output (orphan `\xe2` at offset 5700 of regenerated report) |
| Hand-fix: re-encode report through `decode("utf-8", errors="replace")` | — | — | File now decodes cleanly |
| Phase D.1 Codex re-review (round 2 — second attempt) | `20260425T131942` | needs-rework | Finding 0: same mode-100644 (re-flagged despite D.5 dismissal). Finding 1 (NEW): three hardcoded `/home/mad/miniconda3/bin/python3` invocations at script lines 260, 530, 595 |
| **HALT** (D.2a.6 awaiting-user pause) | `20260425T131942` | paused | Pending edits in working tree; lock released |
| User intervention | — | — | Authorize hand-fix of the three hardcoded paths (replace with `python3` from PATH); produce these friction notes |

## Friction issues (numbered for cross-reference)

### F1 — `{{python_path}}` directive bleeds into artifacts the implementer ships

**Where the directive lives.** `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`:

- Phase B (claude implementer) line ~362: *"Run the test command if specified — use `{{python_path}} ...` (this repo requires the virtualenv)."*
- Phase B-Codex (codex implementer wrapper auto-resolves `{{python_path}}` similarly).
- Phase B-rework / Phase B-narrow-remediation: same wording.

**What the orchestrator substitutes.** `{{python_path}}` resolves to whatever `plan_ops.py preflight --json` returned in `python_path`. On this WSL2 box that resolves to `/home/mad/miniconda3/bin/python3` (no `venv/` in this worktree, so the resolver falls through to `python3` on PATH and pins the absolute path).

**Failure mode.** The directive is framed as "use this Python," not "use this Python *only when invoking the test command*." Implementers (and remediators) interpret it as a global rule and embed the absolute path inside artifacts they author. Specifically harmful when the artifact is a shell script that calls Python from bash — there is no implicit Python interpreter selection in bash, so the implementer reaches for the path the orchestrator handed them.

**Concrete instance (this run).** TASK-001's `scripts/gemini_verification_matrix.sh` had `/home/mad/miniconda3/bin/python3 -c '...'` at three sites (lines 260, 530, 595) for in-script JSON validation. The implementer's plan-adaptations section explicitly cited the directive as the reason: *"ran via `/home/mad/miniconda3/bin/python3 -m pytest` per the prompt's interpreter directive."* The directive was meant to apply to the pytest invocation but the implementer carried it into the script body.

**Why it didn't bite earlier.** Most prior tasks ship Python modules (interpreter is implicit) or test files (pytest's interpreter is set by the runner). TASK-001 is the first task in the project's history that ships a `bash` script that embeds Python invocations. That seam is what surfaces the directive's over-broad scope.

**Suggested upstream fix direction (for future plan iteration to scope).**

- Reword Phase B / Phase B-Codex / Phase B-rework / Phase B-narrow-remediation directives to scope `{{python_path}}` strictly to "invoking the declared Test command." Add an explicit caveat: *"For any Python you embed in shell scripts you produce, use a portable lookup (`command -v python3` or `python3` on PATH) — do NOT embed `{{python_path}}` in the artifact."*
- Optionally: tighten `plan_codex_dispatch.py`'s `render_implement_prompt` to inline the same caveat for the Codex tier (since wrapper auto-injects `{{python_path}}` into the prompt, the same bleed-through risk applies).
- Consider whether `plan_ops.py preflight` should resolve `{{python_path}}` to the **directory-relative** form (`./venv/bin/python3` when present; bare `python3` otherwise) so the directive is syntactically obviously a wrapper-side reference, not an absolute path the implementer copies verbatim.

### F2 — Codex re-review re-flags previously-dismissed findings (no dismissal forwarding)

**Where the gap lives.** `plugins/plan-executor/scripts/plan_codex_dispatch.py` `cmd_review`. The wrapper's prompt construction uses the diff + the task block + the review-focus flag. It does NOT consult the run-log for prior D.5 adjudications, and there is no flag to pass `--dismissed-finding-indices` (or equivalent) into the prompt.

**Failure mode.** Each Codex review dispatch is stateless. Reproducible-environment findings (e.g. `mode 100644` on a `core.fileMode=false` repo, or repo-wide gitignore patterns the reviewer doesn't recognize) reappear on every retry. D.5's `dismissed: [...]` decision is recorded in `_run_log.jsonl` and forwarded to the **remediator** as "DO NOT fix — context only" — but the **re-review** that follows the remediator gets none of that.

**Concrete instance (this run).** TASK-001 D.5 dismissed `mode 100644` (verified `os.access(SCRIPT_PATH, os.X_OK)` returns True via the test). The remediator did not touch the file mode. The second Codex review re-flagged the identical finding with the identical wording. D.2a.6's "binding after one retry" rule then halts because the second `needs-rework` is treated as terminal — even though one of its findings is a rerun of an already-adjudicated false-positive.

**Why it didn't bite earlier.** First time D.2a.6 fired in your runs. Most tasks land at D.5 verdicts of `ship` / `ship-with-fixes` (commit proceeds with `[disagreement]`) or `needs-rework` (D.2a.5 retry). `partial-agreement` is the only D.5 verdict that triggers D.2a.6, and partial-agreement requires both buckets non-empty.

**Suggested upstream fix direction (for future plan iteration to scope).**

- Add a `--prior-dismissed-findings <json-array>` flag to `plan_codex_dispatch.py review` that prepends a "do-not-flag" hint into the reviewer prompt: *"The following findings were independently adjudicated as non-load-bearing in a prior round; do not repeat them. If you nonetheless believe one is load-bearing, flag it with explicit new evidence."*
- Orchestrator-side: at the D.2a.6 step-4 re-review dispatch, populate `--prior-dismissed-findings` from the original Codex `parsed.findings[]` filtered to D.5's `dismissed[]` indices.
- Alternative (additive, not exclusive): post-filter the second-review's findings against the dismissal log and exclude exact re-flags from the verdict-routing decision. This is heuristic (text matching) and weaker than prompt forwarding, but a useful safety net.

### F3 — `plan_codex_dispatch.py review` crashes on non-UTF-8 bytes in `git diff`

**Where the crash lives.** Stack traces from the v2 review run:

- `plan_codex_dispatch.py:556` — `_git()` runs `subprocess.run(["git"] + args, ..., text=True, encoding="utf-8")` (inferred from the traceback chain through `_translate_newlines` → `data.decode("utf-8", errors)`).
- `plan_codex_dispatch.py:612` — `git_diff_for_files()` calls `_git(["diff", "HEAD", "--"] + files, cwd=repo_root)`.
- The traceback ends at `subprocess.py:1099` `data.decode(encoding, errors)` raising `UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe2 in position 6166: invalid continuation byte`.

**Failure mode.** `subprocess.run(text=True, encoding="utf-8")` decodes stdout strictly as UTF-8. When `git diff` output contains invalid UTF-8 bytes (because one of the diffed files contains them), the decode raises and the wrapper exits with a Python traceback on stderr and an empty stdout. The orchestrator's parse step then fails with `JSONDecodeError`. There is no graceful degradation.

**Concrete instance (this run).** Remediator regenerated `_verification_report.md` against live `gemini 0.39.0`. The shell script's `head -c 2000` truncation cut a multi-byte UTF-8 codepoint mid-sequence, leaving an orphan `\xe2` byte at offset 5700 of the report. `git diff HEAD -- docs/plans/.../_verification_report.md` then produced output containing that orphan byte; the wrapper crashed.

**Why it didn't bite earlier.** Source code is virtually always valid UTF-8 (Python, JSON, markdown, tests). The crash needs a diffed file with raw non-UTF-8 bytes. The first time the project ships a script that captures live external-CLI output and writes it to a committed file, this seam becomes reachable. (It's also reachable any time someone commits a binary file by accident, but the existing precommit hooks likely catch most of those.)

**Suggested upstream fix direction (for future plan iteration to scope).**

- Change `_git()` decoding to `errors="replace"` (or `"surrogateescape"`) so non-UTF-8 bytes round-trip as replacement chars without crashing the wrapper. The diff still goes to Codex; it just has `�` at the offending positions instead of the original bytes.
- Alternative: switch `_git()` to bytes mode (`text=False`) and do explicit `bytes.decode("utf-8", errors="replace")` on the wrapper side before passing to the prompt builder.
- Belt-and-braces: when the reviewer's diff contains replacement chars, prepend a note in the prompt: *"Some bytes in the diff were non-UTF-8 and have been replaced; flag them as a finding if review-relevant."* This makes the safety-net visible to Codex.

### F4 — `head -c` byte truncation in shell scripts splits multi-byte UTF-8 codepoints

**Where the bug lives.** `scripts/gemini_verification_matrix.sh` (the truncation helper used by `emit_row` for stdout/stderr captures). The script implements 2000-char truncation via `head -c 2000` — a byte-level, not codepoint-level, operation.

**Failure mode.** When the live `gemini` CLI emits output containing multi-byte UTF-8 (box-drawing chars, em-dashes, smart quotes, anything outside ASCII), `head -c 2000` can chop at any byte boundary, including mid-sequence. The truncated output written to the report is then invalid UTF-8.

**Concrete instance (this run).** Box-drawing char `│` (U+2502, 3 bytes `\xe2\x94\x82`) was at the 2000-byte boundary of one row's stdout capture. `head -c 2000` left only the leading `\xe2`. The report then carried an orphan `\xe2` byte, which cascaded into F3.

**Why it didn't bite earlier.** First task to commit a real-CLI report. Other tasks ship test fixtures (controlled UTF-8) or source code (controlled UTF-8).

**Suggested upstream fix direction (for future plan iteration to scope).**

- Make the matrix script's truncation codepoint-aware. Options:
  - `python3 -c 'import sys; sys.stdout.write(sys.stdin.read()[:2000])'` (codepoint-based; relies on F1 being fixed first).
  - `awk 'BEGIN{RS=""} {print substr($0, 1, 2000)}'` (byte-based but simpler; still incorrect for multi-byte).
  - `iconv -f utf-8 -t utf-8 -c | cut -c1-2000` (drops invalid bytes via `iconv -c` first, then `cut -c` is locale-aware in GNU coreutils when `LC_ALL=en_US.UTF-8`).
  - Sanitization layer: pipe through `iconv -c` (with `-c` to drop invalid bytes) before truncation regardless of approach.
- This is TASK-001-local; not a plan_ops / plan-executor framework issue. Could be addressed in TASK-001's eventual narrow-remediation or as a follow-up commit on the matrix script.

### F5 — D.2a.6's "binding after one retry" doesn't gracefully handle artifact-regeneration cases

**Where the contract lives.** `plugins/plan-executor/skills/implement-plan/SKILL.md` §D.2a.6 step 4: *"On retry success, re-run D.1 (Codex review). The re-review is binding — no further retry regardless of verdict."*

**Failure mode.** The contract assumes self-contained code: the remediator's diff is small, surgical, and shouldn't surface review concerns the first round didn't already see. When the remediator's work includes **regenerating an artifact** (a committed report, a live-CLI output capture, etc.), the new diff content can introduce findings the first-round reviewer never saw — independent of whether the original load-bearing finding was correctly addressed.

**Concrete instance (this run).** Round-1 review flagged Row 3 (load-bearing) and mode 100644 (D.5-dismissed). Remediator fixed Row 3 and regenerated the report. Round-2 review flagged the hardcoded paths — these were always present but Row-3-stub-distraction kept them out of the round-1 finding budget. The protocol's binding rule treats round-2 `needs-rework` as terminal even though the new finding is unrelated to the original load-bearing one.

**Why it didn't bite earlier.** First task in your runs whose remediation involved artifact regeneration vs. surgical code edits.

**Suggested upstream fix direction (for future plan iteration to scope).**

- Distinguish "round-2 finding is a re-flag of round-1 dismissed-or-fixed" from "round-2 finding is genuinely new." The former should be filterable (see F2's prompt-forwarding fix); the latter should optionally route to a fresh D.5+remediation cycle if the orchestrator can prove the new finding wasn't reviewable in round 1.
- Alternatively: introduce a `--max-narrow-remediation-rounds N` flag (default 1, configurable) so users running plans with regenerable artifacts can opt in to bounded retry loops.
- Conservative: don't change the binding rule, but add a lighter-weight halt-disposition path that lets the user accept "Codex's new finding is real but out-of-scope for this task; commit anyway with `[narrow-remediation]` + `[new-finding-deferred: ...]` trailer." Today's awaiting-user "keep as-is" branch is the analog but doesn't structurally distinguish the two outcomes.

### F6 — `compute-schedule` produces wide file-disjoint batches; Codex plan-review reads them as DAG violations (KNOWN)

**User has already flagged this for separate-session work.** Recording for completeness.

**Where the gap lives.** `plugins/plan-executor/scripts/plan_ops.py:_compute_schedule_batches` (around line 436). The algorithm sorts by `_task_order_key(id, priority)` and packs file-disjoint tasks into open batches — it does NOT respect the topological layering encoded in `tasks[].dependencies[]`. Runtime DAG enforcement is delegated to `batch-next`'s `_ready()` check (line ~4131), which filters tasks whose deps are not yet `done`.

**Failure mode (Phase 1.5).** Codex `plan-review` reads the persisted `<plan>.schedule.json` literally — "batch = concurrent dispatch unit." When `compute-schedule` collapses a strict dep chain (001→002→003) into a single batch (because the file_locks happen to be disjoint), Codex flags it as a DAG violation. The plan-author auto-revise can't fix it because the wide batches are recomputed every run from `tasks[].files[]` via `compute-schedule`; editing `00_INDEX.json:parallel_batches` (which already encodes the right topo) has no effect on the persisted schedule.

**Concrete instance (this run).** First plan-review pass returned `needs-replan` with this finding. Plan-author's no-op was correct (`00_INDEX.json:parallel_batches` was already `[["001"], ["002","005"], ["003","008"], ["004"], ["006"], ["007"]]`). Second pass re-flagged the identical finding. Run halted with `plan_review_failed`. User hand-edited the schedule's `batches[]` to use `parallel_batches` filtered to `--task-ids` (= 4 single-task batches), then re-ran plan-review which approved-with-notes.

**Suggested upstream fix direction (user is handling — do not duplicate).**

- Make `compute-schedule` consume `00_INDEX.json:parallel_batches` (filtered to the in-memory `tasks[]` set) as the topo source-of-truth, with file-disjointness applied within each topo wave.
- Or: have `compute-schedule` derive topo waves from `tasks[].dependencies[]` directly (Kahn's algorithm — already implemented in `_compute_decompose_batches`).
- Either way, the persisted `<plan>.schedule.json:batches[]` should match the user's intent and Codex's literal reading without per-run hand-edits.

## Cross-cutting observations

- **Stateless reviewer dispatches + reproducible-environment findings = thrash.** F2 + F5 + F6 are all instances of "the reviewer doesn't know what was already adjudicated." The solution shape is similar across all three: forward prior decisions into the next dispatch.
- **Directive scope discipline matters.** F1 is a wording bug. The fix is in the dispatch template, not in any code. Future template edits should explicitly call out which directives are "for the orchestrator's invocation" vs "for the implementer's artifact."
- **Bash + bytes is a UTF-8 hazard zone.** F3 + F4 are both UTF-8 issues at the bash↔python boundary. A blanket "scripts must be UTF-8 codepoint-safe" hygiene rule for plans that ship shell artifacts would prevent a class of these.

## Suggested follow-up planning shape

These notes seed two natural follow-up plans:

1. **`PLAN_DISPATCH_PROMPT_HARDENING_*`** — addresses F1 (template wording), F2 (dismissal forwarding), F5 (binding-rule refinement). One body of work; touches `dispatch-templates.md`, `plan_codex_dispatch.py`, `SKILL.md`, run-log schema. The user has experience scoping this kind of plan (per the existing `codex friction` work in the recent commit history).

2. **`PLAN_WRAPPER_UTF8_TOLERANCE_*`** — addresses F3 (wrapper crash on non-UTF-8 bytes). Scope is narrow: `_git()` decoding strategy + an integration test that diffs a file with deliberately-injected invalid UTF-8.

F4 (matrix-script truncation) and F6 (compute-schedule topo) are TASK-001-local and user-flagged, respectively. Neither needs a dedicated plan; each can ride along with whatever change touches the relevant module.

## Run-log pointers (for future code-grounding)

The two run logs are appended to `docs/plans/_run_log.jsonl`. Look for:

- `run_id="20260425T123302"`: Phase 1.5 first/second plan-review halt path.
- `run_id="20260425T131942"`: Phase 1.5 third plan-review approval, TASK-001 implement→D.5→narrow-remediation→D.2a.6 awaiting-user pause.

Each event carries the verdict/findings JSON inline so future code-grounding doesn't need to re-dispatch any agent.
