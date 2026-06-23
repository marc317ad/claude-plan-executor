---
bug_id: 158
status: CLOSED
group: WRAP-TCMD
severity: important
source_fix_id: manual
source_plan: manual
source_date: '2026-06-23'
origin: agent
decomposed_at: '2026-06-23'
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
- plugins/plan-executor/scripts/_plan_paths.py
- plugins/plan-executor/scripts/plan_codex_dispatch.py
- plugins/plan-executor/scripts/plan_ops.py
- plugins/plan-executor/skills/implement-plan/SKILL.md
- tests/scripts/test_plan_codex_dispatch_parsing.py
- tests/scripts/test_plan_ops.py
- tests/scripts/test_plan_ops_lint_plans.py
content_fingerprint: sha256:manual-158
change_history: []
---

# BUG-158: Backtick+parenthetical Test command breaks wrapper dash run and is silently absorbed as [sandbox-divergence]

**Status:** CLOSED
**Severity:** important
**Group:** WRAP-TCMD
**Depends on:** none
**Test command:** `venv/bin/pytest tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_lint_plans.py -q`

## Acceptance criteria
- normalize_test_command in _plan_paths.py turns a backtick-wrapped command with a trailing parenthetical annotation into the bare command, and is the single shared normalizer used by plan_codex_dispatch.parse_task_block, plan_ops._parse_task_block, and run_test_command.
- The semantic marker deferred (TASK-NNN) and a leading subshell command are returned unchanged by normalize_test_command (their parentheses are not annotations).
- run_test_command never hands subprocess.run a string that still carries wrapping backticks or a dangling annotation parenthesis; a previously-divergent backtick+parenthetical command now runs to a real pass/fail under shell=True.
- auto_validate_divergence withholds the escape hatch when the sandbox failure is a shell syntax error (exit code 2 with Syntax error in stderr): it returns divergence=false, applicable=true, command_malformed=true with a reason and writes no sandbox_divergence event; the existing missing-dependency exit-1 divergence path is unchanged.
- lint-plans emits a warning-severity test_command_shell_hazard finding for a Test command whose normalized form still contains a surviving backtick or an unbalanced parenthesis, without changing the lint exit code.
- Existing auto-validate and parsing tests and the tier_c pure-core fixtures remain green (new result keys appear only on the new command_malformed path), and the full pytest suite passes.

## Problem
The Codex wrapper extracts a task Test command and runs it via subprocess.run(shell=True), i.e. /bin/sh = dash. Both the wrapper (plan_codex_dispatch.py parse_task_block, lines 434-439) and the canonical plan_ops._parse_task_block (lines 3143-3148) strip markdown backticks ONLY when the value BOTH starts and ends with a backtick. A trailing human annotation moves the closing backtick off the end, so a value like (backtick)pytest foo(backtick) (smoke subset) is NOT stripped: the surviving backticks trigger command substitution AND the literal open-paren reaches dash, which fails with: /bin/sh: 1: Syntax error: word unexpected. Reproduced exactly under /bin/sh. The wrapper then reports cause=independent_test_run_failed. The orchestrator runs auto_validate_divergence, which per SKILL.md line 491 re-runs the orchestrator-supplied Test command (NOT the wrapper sandbox_test_command) via shell=True with NO backtick handling (plan_ops.py:15601,15639). Because an LLM-supplied command is naturally the bare form, the re-run passes, divergence=true, and the commit ships tagged [sandbox-divergence]. The two sides ran DIFFERENT strings, so this is a deterministic parser/normalization bug, NOT an environment divergence; the TASK-008 escape hatch is laundering a parse failure. Reported elsewhere: 3 tasks (001, 002, 006) committed this way. Note: _plan_paths.normalize_files_entry already solves this exact backtick+trailing-annotation shape for Files entries; the test-command paths never adopted it.

## Recommended fix
Three layers plus a doc note. (1) Add a shared normalize_test_command() to _plan_paths.py modeled on normalize_files_entry: when the value is a leading backtick code span, return the inner command and drop any trailing annotation; else, if it is the semantic deferred (TASK-NNN) marker or a leading subshell, leave it intact; else strip a trailing parenthetical annotation (but not one that follows a shell operator) plus any legacy both-ends backticks. Route plan_codex_dispatch.parse_task_block, plan_ops._parse_task_block, and run_test_command defensive-unwrap through it so every path agrees byte-for-byte. (2) Make auto_validate_divergence fail loud on a malformed command: if the sandbox failure was a shell syntax error (exit code 2 with Syntax error in stderr) the command never executed and a malformed string fails identically in every environment, so withhold the escape hatch (divergence=false, applicable=true, command_malformed=true, reason set) instead of tagging [sandbox-divergence]. (3) Add a lint-plans warning test_command_shell_hazard that flags a Test command whose normalized form still contains a surviving backtick or an unbalanced parenthesis, catching it at authoring time. Then add bare-command authoring guidance to the docs as the human-facing complement.

## Verification
venv/bin/pytest tests/scripts/test_plan_codex_dispatch_parsing.py tests/scripts/test_plan_ops.py tests/scripts/test_plan_ops_lint_plans.py -q passes. New unit tests cover: backtick+trailing-parenthetical normalizes to the bare command on BOTH the wrapper and plan_ops paths; bare trailing parenthetical is stripped; deferred (TASK-NNN) preserved; leading subshell preserved; run_test_command never receives surviving wrapping backticks or a dangling paren; auto_validate_divergence returns divergence=false with command_malformed on the exit-2 Syntax-error envelope while the legacy missing-dep exit-1 envelope still yields divergence=true; lint flags a hazardous Test command. Full suite venv/bin/pytest tests/ -q stays green. Independent Codex review run on the diff.

## Reversion guidance
Revert the normalize_test_command helper in _plan_paths.py and re-point the three call sites back to their inline both-ends backtick strip; revert the auto_validate_divergence command_malformed guard and the lint test_command_shell_hazard check; drop the new tests and the SKILL doc note. No data or schema migration. Behavior returns to the prior over-permissive escape-hatch form.

## Run history

### Run 20260623 (manual hand-fix, Codex cross-review) — CLOSED
- **Branch:** fix-bug-158 (off main)
- **Fix applied:** all three layers + the SKILL doc note. (1) `normalize_test_command()` added to `_plan_paths.py` and routed into `plan_codex_dispatch.parse_task_block`, `plan_ops._parse_task_block`, and `plan_codex_dispatch.run_test_command`. (2) `auto_validate_divergence` withholds the escape hatch on a shell-emitted syntax error (`command_malformed`). (3) `lint-plans` `test_command_shell_hazard` warning. SKILL.md §Sandbox-divergence gained the `command_malformed` branch + bare-`Test command:` authoring guidance.
- **Codex cross-review:** first pass returned `NEEDS-REWORK` with 3 findings — (a) `echo $(date)` mangled to `echo $` (command substitution treated as annotation; caught independently by local probe and fixed via a `(?<![$<>])` lookbehind), (b) only one trailing annotation stripped (`pytest x (a) (b)` → `pytest x (a)`; fixed by looping the strip to fixpoint), (c) the auto-validate guard could withhold a real divergence when a *test* prints "syntax error" and exits 2 (narrowed to require a shell `<shell>:` prefix). After the fixes, Codex re-review returned **SHIP**, confirming all three resolved with no new mangling and a terminating loop.
- **Tests:** added normalizer parametrization (incl. `$()`/`<()` substitution, quoted parens, multi-annotation), a wrapper parse + `run_test_command` regression, the `auto_validate_divergence` `command_malformed` test and its false-positive counterpart, and two `lint-plans` `test_command_shell_hazard` tests. 89 targeted tests pass (parsing/lint/auto-validate/tier-c golden) + 94 decompose/build-tasks/wrapper round-trip tests pass.
- **Pre-existing failures (NOT introduced):** the full suite has 41 failures unrelated to this diff — stale hardcoded `cmd_` counts in `tests/tools/test_plan_ops_pure_core_extract.py` (HEAD already has 42 vs the test's 38), a missing `plan_ops_cheatsheet.md`, and MCP-registry/SKILL drift for `set-task-agent` / `build-agent-dispatch-prompt` / `parse_reviewer_envelope` (migration debt). `git diff` introduces none of those identifiers; verified pre-existing.
- **Files changed:** plugins/plan-executor/scripts/_plan_paths.py, plan_codex_dispatch.py, plan_ops.py; plugins/plan-executor/skills/implement-plan/SKILL.md; tests/scripts/test_plan_codex_dispatch_parsing.py, test_plan_ops.py, test_plan_ops_lint_plans.py.

---

## Provenance
- **Source plan:** manual
- **Source fix ID:** manual
- **Original review:** agent
- **Consolidation date:** 2026-06-23
- **First decomposed:** 2026-06-23
- **Group:** WRAP-TCMD
- **Absorbed from:** none
