# TASK-001 — Empirical Gemini-CLI verification matrix (precondition)

## Goal

Validate, with a runnable script + a committed report, that the installed `gemini` CLI behaves as the rest of this plan assumes. No wrapper code, no orchestrator wiring, no schema mirrors land before this matrix passes — every downstream task depends on its findings.

## Context

The 2026-04-21 sibling plan (`docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-21.md`) listed an "Empirical Verification Matrix" as its TASK-005 — last in line. That ordering is wrong for our scope: the matrix's results gate the wrapper's design (envelope shape, schema-validation retry semantics, isolation-policy enforcement), so it has to land first.

Three findings from a one-shot Gemini consultation during this plan's authoring phase already adjusted the assumed contract:

1. `-o json` envelope shape is `{response, stats, error?}`. `session_id` is `stream-json`-only — NOT present on `-o json`. The 2026-04-21 plan assumed it was always present; the matrix must verify the current shape and pin it.
2. Gemini has NO `--output-schema PATH` flag. Schema enforcement is in-prompt + post-hoc validation. The matrix must confirm there is no hidden flag that would let us skip the post-hoc pattern.
3. Exit codes documented at first contact: `0` success, `1` error, `42` empty/input, `53` turn-limit. The matrix must reproduce each.

Three other claims from the 2026-04-21 plan need verification before they are load-bearing in `plan_gemini_dispatch.py`:

- `GEMINI_CLI_HOME` fully isolates session state, settings, and credentials cache from `$HOME/.gemini`. Concurrent invocations against different homes must not leak state.
- `policies/restrictive.toml` with `[[rule]] toolName = ["run_shell_command", "edit_file"] decision = "deny"` is honored by Gemini's Policy Engine in headless mode.
- Headless mode does NOT attempt interactive OAuth refresh when `GEMINI_API_KEY` is set, AND does fail loudly (not hang) when the key is absent.

The matrix is a `bash` script that exercises each row, captures stdout / stderr / exit code, and writes a markdown report. It does NOT depend on any wrapper code. It runs against the CLI on PATH so future regressions on `gemini` upgrades are detectable.

**Out of scope.** Authoring the wrapper module; designing schemas; touching `plan_ops.py`. All three are downstream tasks gated on the matrix's outcomes.

## Verification

- A new script at `scripts/gemini_verification_matrix.sh` exercises every row of the matrix below, captures `stdout`, `stderr`, exit code, and per-row pass/fail into a markdown report. The script honors a `--report` flag pointing to the output path.
- A committed report at `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/_verification_report.md` records the most recent run's results — every row marked `pass` or `fail` with the captured exit code, a one-line stdout/stderr summary, and a verdict line.
- Every row in the matrix below is `pass` on the report committed with this task. Any `fail` is a hard halt — downstream wrapper / preflight / orchestrator tasks DO NOT proceed until the failing row is reconciled (either the CLI's behavior matches the assumption, or the assumption is amended in this plan and the dependent tasks are revised).
- The matrix script is idempotent: re-running it against the same `gemini` build produces the same report (modulo timestamps + latency-ms fields).
- `venv/bin/pytest -q tests/scripts/test_gemini_verification_matrix.py` (a thin sanity test that exec's the script with a fake `gemini` shim and asserts the report shape) returns 0.

| # | Row | Command | Expected |
|---|---|---|---|
| 1 | JSON envelope shape | `gemini -p "hi" -o json` | exit 0; stdout parses as JSON; top-level keys ⊆ `{response, stats, error, session_id}`; `response` is string; `session_id` MAY be absent. |
| 2 | Empty-prompt exit code | `gemini -p "" -o json` | exit 42. |
| 3 | Turn-limit exit code | `gemini -p "<long pathological prompt that should hit turn limit>"  -o json` (best-effort; if the row cannot be reliably reproduced, mark `unverified` not `fail` and document the reason) | exit 53 OR `unverified` with reason. |
| 4 | Stdin context piping | `printf 'FILE_CONTEXT\n' \| gemini -p "repeat the literal context above verbatim" -o json` | exit 0; `response` contains `FILE_CONTEXT`. |
| 5 | `GEMINI_CLI_HOME` isolation | `GEMINI_CLI_HOME=$(mktemp -d) gemini -p "hi" -o json` | exit 0; `<home>/.gemini/` directory is created; `$HOME/.gemini` is NOT touched (verify by mtime check before/after). |
| 6 | Policy `deny` enforcement | Set up `<home>/.gemini/policies/deny.toml` with `[[rule]] toolName = ["run_shell_command"] decision = "deny" priority = 999`; run `GEMINI_CLI_HOME=<home> gemini -p "run 'ls' via run_shell_command and tell me what you see" -o json --approval-mode plan` | exit 0; `response` does NOT contain output that could only come from running `ls`; if Gemini reports the deny, the `response` text references the policy. |
| 7 | `--approval-mode plan` read-only | `gemini -p "summarize README.md" -o json --approval-mode plan` from a repo with a README | exit 0; no files modified post-run (verify via `git status` before/after). |
| 8 | `GEMINI_API_KEY` absent failure mode | `env -u GEMINI_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS gemini -p "hi" -o json --approval-mode plan` (run in a `GEMINI_CLI_HOME=$(mktemp -d)` so no cached credentials are available) | Either exit ≠ 0 within ≤ 10 seconds with a structured stderr that names "auth" or "credentials", OR an `error` envelope with the same. NEVER an interactive prompt or hang. |
| 9 | `stats.tokens` shape | inspect row 1's `stats` object | top-level keys include at minimum a model-named sub-object whose `.tokens` carries `input`, `prompt`, `total`, `cached`. The exact key path is recorded in the report so the wrapper's stats parser binds to a verified shape. |
| 10 | Markdown-fenced JSON in `response` | `gemini -p "Return ONLY a JSON object with field 'ok' = true. No prose." -o json` | exit 0; `response` may or may not be wrapped in ```json ... ``` fences. Record which form Gemini emits today; the wrapper's strip-fence logic is keyed on this. |

## Tasks

### TASK-001: Empirical Gemini-CLI verification matrix (precondition)

- **Status:** done
- **Priority:** high
- **Files:**
  - `scripts/gemini_verification_matrix.sh` (new)
  - `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/_verification_report.md` (new — committed report)
  - `tests/scripts/test_gemini_verification_matrix.py` (new — shim-based sanity test)
- **Dependencies:** []
- **Test command:** `venv/bin/pytest -q tests/scripts/test_gemini_verification_matrix.py`
- **Acceptance criteria:**
  - `scripts/gemini_verification_matrix.sh` exists, is executable, and exercises every row in the matrix above. The script accepts `--report <path>` and writes a markdown report; default path is the committed report under this plan's directory.
  - Every row's section in the report carries: command, exit code, captured stdout (truncated to 2000 chars), captured stderr (truncated to 2000 chars), per-row verdict (`pass` / `fail` / `unverified`), and a one-line rationale.
  - Every row in the committed report is `pass` OR `unverified` (with a documented reason). NO row is `fail` — a `fail` row halts this plan's downstream tasks per the rule above.
  - The shim test in `test_gemini_verification_matrix.py` invokes the script with a `gemini` stub on `$PATH` (a tiny Python or shell script that emits a hard-coded JSON envelope and the expected exit codes per row) and asserts the report's row count, the row labels, and that every row's verdict is structurally one of `{pass, fail, unverified}`. The shim test does NOT require a real Gemini CLI.
  - `venv/bin/pytest -q tests/scripts/test_gemini_verification_matrix.py` returns 0.
- **Reversion guidance:** revert the new files; downstream tasks halt naturally because they depend on TASK-001.

**Description:**
Run the empirical matrix, commit the report, and pin the assumptions every other task depends on. The matrix is the contract surface between "what we believe Gemini does" and "what `plan_gemini_dispatch.py` is built against". Authoring the wrapper before this lands gives us a wrapper built on guesses; pinning the matrix first means every downstream design decision references a verified row.

**Implementation notes:**
- Keep the script POSIX-bash; do not assume `bashism` extensions. `mktemp -d`, `printf`, `command -v` are all the script needs.
- Capture exit codes with `set +e; gemini ...; rc=$?; set -e` style — never let `set -e` kill the matrix mid-row.
- For row 8, set a 10-second `timeout` wrapper so a hang (failure of the test's premise) is itself recorded as a row failure, not a stuck CI.
- Truncation to 2000 chars per stdout/stderr field mirrors `RAW_TRUNCATE_CHARS` in `plan_codex_dispatch.py` — use the same constant to keep the wrapper's eventual error-envelope shapes aligned.
- The shim test's fake `gemini` is a 30-line Python script invoked via PATH-overlay (`PATH=<test-dir>:$PATH`) — it emits the canonical envelope shape from row 1 plus the documented exit codes for each row's matching prompt. Keeps the test hermetic.
- Do NOT validate Gemini's actual model output quality in this matrix — that is the wrapper's job once schemas land. The matrix tests CLI-shape contract only.

## Execution log — 20260425T131942 (paused)

Starting SHA: `8a62fc279f1b70368fa5050b2de7938c0aa888ac`  → Ending SHA: `8a62fc279f1b70368fa5050b2de7938c0aa888ac`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude | codex | needs-rework [narrow-remediation second pass] |  | awaiting user; D.2a.6 second needs-rework: dismissed finding 0 re-flagged + NEW hardcoded miniconda3 path |
