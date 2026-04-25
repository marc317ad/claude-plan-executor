# TASK-003 — `plan_gemini_dispatch.py` skeleton + `review` subcommand

## Goal

Stand up `plugins/plan-executor/scripts/plan_gemini_dispatch.py` with a single working subcommand: `review`. The wrapper mirrors the surface of `plan_codex_dispatch.py review` so the orchestrator can substitute it with a one-line dispatch swap. Implementation (`implement` subcommand) is **deliberately not added** — this wrapper exists for adversarial review only.

## Context

### Why this task is Claude-tier, not Codex-tier

The wrapper has three load-bearing concerns that go beyond a small-scope fix-up:

1. **Headless OAuth contract.** Per TASK-001 row 8, the wrapper MUST refuse to run when `GEMINI_API_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` are both absent — otherwise Gemini may attempt an interactive browser launch and hang the orchestrator. This is structurally different from `plan_codex_dispatch.py`'s "Codex binary not found on PATH" branch and needs a fresh design pass, not a copy-paste.
2. **Schema-validation retry loop.** Per TASK-001 row 1 + the consultation findings, Gemini has NO `--output-schema PATH` flag. The wrapper embeds the schema in the prompt + validates the parsed `response` field post-hoc with `jsonschema`. On validation failure the wrapper re-prompts up to 2 additional times (3 total attempts) before returning `outcome=parse_error`. This control flow has no analogue in `plan_codex_dispatch.py` and is the wrapper's most non-obvious section.
3. **Restrictive policy + isolation.** The wrapper creates an ephemeral `GEMINI_CLI_HOME` per invocation, writes a `policies/restrictive.toml` denying `run_shell_command` and edit tools, AND tears the home down on completion. Concurrent invocations must not collide on the home directory.

Each of these is a small-but-careful design decision; together they exceed the codex-tier scope envelope (≤30 lines, ≤3 files, concrete test command, simple control flow).

### Scope is `review` only — `implement` is explicitly excluded

The user's directive: *"codex needs to be used in implementation and adversarial review of code changes; gemini acts as a backup for the adversarial tasks if codex becomes unavailable."* Implementation is Codex-only under all conditions. The wrapper's argparse must reject any attempt to invoke an `implement` subcommand at the CLI surface — not silently no-op, but exit nonzero with a structured stderr explaining the policy. This makes the scope visible at the wrapper layer (the wrapper IS the contract), not just in the orchestrator's routing tables.

### Surface mirror of `plan_codex_dispatch.py review`

The Codex `review` subcommand's argspec (per `plan_codex_dispatch.py` and SKILL.md §Phase D-Codex):

```
plan_codex_dispatch.py review \
  --plan-file <abs> --task-id NNN --repo-root <abs> \
  --files f1,f2 [--review-focus bugs] [--dry-run] [--timeout SECS]
```

The Gemini wrapper's `review` argspec is byte-for-byte the same except for the executable name:

```
plan_gemini_dispatch.py review \
  --plan-file <abs> --task-id NNN --repo-root <abs> \
  --files f1,f2 [--review-focus bugs] [--dry-run] [--timeout SECS]
```

The envelope shape is the same, with two field renames so the reviewer family is visible:

- `codex_exit_code` → `gemini_exit_code`.
- The `parsed` object validates against `gemini_review_schema.json` (TASK-002), structurally identical to `codex_review_schema.json`.

A sibling renamed key `reviewer: "gemini"` is added to the top-level envelope so log-event consumers can distinguish without parsing the binary path. Codex envelopes implicitly have `reviewer: "codex"` — TASK-007 of this plan adds the field to the Codex wrapper too for symmetry.

### Injection-defense posture

Per `feedback_injection_defense_at_wrapper`: orchestrator-authored content (plan text, task block, files-changed list, schedule JSON) is trusted and embedded verbatim. Diff content from `git diff -- <files>` is locally generated and trusted. Gemini's tool reads via `read_file` are constrained by the restrictive policy denying `run_shell_command` + all edit tools. Untrusted content does not reach Gemini through any orchestrator-controlled channel.

The wrapper's working-tree cleanup uses `_plan_paths.is_protected_path` (the same shared module `plan_codex_dispatch.py` imports), so executor-infrastructure protection is automatic.

### Out of scope

- `plan-review` subcommand (TASK-004).
- Preflight detection of `gemini_available` (TASK-005).
- Orchestrator routing for fallback (TASK-006 / TASK-007).
- Schema-audit fixture extension (TASK-008).
- Any `implement` subcommand — explicitly excluded by the user's directive.

## Verification

- New file `plugins/plan-executor/scripts/plan_gemini_dispatch.py` is executable and exposes a single working subcommand `review`.
- Invoking `python plan_gemini_dispatch.py implement ...` with any arguments exits with a non-zero code and stderr containing `"implement subcommand not supported"` (the wrapper is review-only by policy).
- Invoking `python plan_gemini_dispatch.py plan-review ...` exits with a non-zero code and stderr containing `"plan-review subcommand not yet implemented; see TASK-004"` (until TASK-004 lands the wrapper does not silently no-op the missing subcommand).
- Invoking `python plan_gemini_dispatch.py review --plan-file <p> --task-id 001 --repo-root <r> --files a.py --timeout 180` with a fake `gemini` shim on PATH (the same shim used in TASK-001's matrix test) emits a JSON envelope on stdout with shape `{task_id, subcommand: "review", reviewer: "gemini", outcome: "success", gemini_exit_code: 0, parsed: {...}}` where `parsed` validates against `gemini_review_schema.json`.
- When the shim returns a `response` field whose JSON does NOT validate against `gemini_review_schema.json`, the wrapper retries up to 2 additional times (3 total attempts). After the third failure the envelope is `{outcome: "parse_error", gemini_exit_code: <last>, attempts: 3, last_validation_error: "<jsonschema message>"}`.
- When `GEMINI_API_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` are both unset, the wrapper exits with `outcome: "failure", error: "missing GEMINI_API_KEY or GOOGLE_APPLICATION_CREDENTIALS"` BEFORE invoking the binary. No subprocess is spawned.
- The wrapper creates a per-invocation `GEMINI_CLI_HOME` under `tempfile.mkdtemp(prefix="gemini_dispatch_")`, writes `<home>/.gemini/policies/restrictive.toml` with `[[rule]] toolName = ["run_shell_command","edit_file","write_file","replace","glob","shell"] decision = "deny" priority = 999`, and deletes the home in a `finally` block. Two concurrent invocations get distinct homes.
- Working-tree cleanup uses a baseline snapshot (`_snapshot_baseline`) + delta-bounded restore + `is_protected_path` allowlist — same idiom as `plan_codex_dispatch.py cmd_review`.
- New test `tests/scripts/test_plan_gemini_dispatch_review.py` exercises:
  - happy path (shim returns valid envelope on first attempt → wrapper returns success);
  - schema-retry path (shim returns invalid JSON twice then valid → wrapper returns success with `attempts: 3`);
  - schema-retry exhaustion (shim returns invalid 3 times → wrapper returns `parse_error`);
  - missing API key short-circuit (no subprocess spawned);
  - implement subcommand rejection;
  - plan-review subcommand "not yet implemented" stub;
  - `GEMINI_CLI_HOME` isolation (the home is created and deleted; no `~/.gemini` writes).
- `venv/bin/pytest -q tests/scripts/test_plan_gemini_dispatch_review.py` returns 0.

## Tasks

### TASK-003: `plan_gemini_dispatch.py` skeleton + `review` subcommand

- **Status:** done
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/plan_gemini_dispatch.py` (new)
  - `tests/scripts/test_plan_gemini_dispatch_review.py` (new)
- **Dependencies:** [002]
- **Test command:** `venv/bin/pytest -q tests/scripts/test_plan_gemini_dispatch_review.py`
- **Read targets:**
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py:1-220` (header + constants + plan-parsing helpers — the lifecycle and constants the Gemini wrapper imports / mirrors)
  - `plugins/plan-executor/scripts/_plan_paths.py` — full file (protected-path constants and `is_protected_path` predicate)
  - `plugins/plan-executor/scripts/codex_review_schema.json` — full file (the structural model the Gemini envelope's `parsed` field mirrors)
  - `plugins/plan-executor/scripts/gemini_review_schema.json` — full file (TASK-002 output; the post-hoc validation target)
- **Acceptance criteria:**
  - Wrapper module exists, is importable as `plan_gemini_dispatch`, and exposes a `main()` callable that the test invokes via `subprocess` with `sys.executable`.
  - All seven test cases in `test_plan_gemini_dispatch_review.py` pass.
  - Wrapper imports `is_protected_path`, `PROTECTED_EXACT_PATHS`, `PROTECTED_PATH_PREFIXES`, `PROTECTED_PATH_SUFFIXES`, `PROTECTED_PATH_GLOBS` from `_plan_paths` — does NOT redefine its own copies (consistent with the post-TASK-004C consolidation in `plan_codex_dispatch.py`).
  - Wrapper imports `plan_ops` for prompt construction / pre-read excerpts (`plan_ops.render_pre_read_excerpts`) and task-block parsing (re-uses `parse_task_block` semantics from `plan_codex_dispatch.py` either by importing the helper directly or by mirroring its implementation byte-for-byte; do NOT diverge — drift here will silently change reviewer behavior across families).
  - The schema-retry loop is bounded at 3 attempts total. Hard-coded; no `--max-attempts` flag in v1.
  - The `restrictive.toml` content is a module-level constant `RESTRICTIVE_POLICY_TOML` so a future broadening (e.g. allowing `read_file` explicitly) is one edit at one location.
  - The `--review-focus` and `--dry-run` flags exist on argparse but are passed through to the prompt-construction layer untouched (mirror `plan_codex_dispatch.py`'s handling).
  - No `git stash` anywhere in the module (mirror SKILL.md §Bash command idioms).
- **Reversion guidance:** revert both new files; nothing else in the tree imports them yet (TASK-005 and onward will).

**Description:**
Build the Gemini wrapper module with the `review` subcommand wired end-to-end. This is the wrapper that the orchestrator's Phase D.1 fallback path (TASK-007) will dispatch. The retry loop and the missing-API-key short-circuit are the two non-obvious sections; the rest is mirror-from-Codex. Tests use a fake `gemini` shim on PATH so they are hermetic.

**Implementation notes:**
- Layout the module to mirror `plan_codex_dispatch.py`'s sectioning: constants → plan-parsing helpers (or imports thereof) → `cmd_review` function → `main()` argparse dispatcher. Keep `cmd_implement` and `cmd_plan_review` as stubs that exit nonzero with a structured stderr — they are present so a missing subcommand produces a clear error, not a `usage:` dump.
- The schema-retry loop:
  ```python
  for attempt in range(1, 4):  # 3 attempts max
      stdout, stderr, rc = _invoke_gemini(prompt, home, timeout)
      try:
          envelope = json.loads(stdout)
          inner = _strip_fence(envelope["response"])
          parsed = json.loads(inner)
          jsonschema.validate(parsed, schema)
          return _success_envelope(parsed, rc, attempt)
      except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
          last_err = exc
          if attempt < 3:
              prompt = _add_retry_clause(prompt, exc)
              continue
          return _parse_error_envelope(rc, attempt, last_err)
  ```
  The `_add_retry_clause` helper appends a short suffix telling Gemini "the previous attempt's JSON did not validate; here is the schema again, try again." This is the bounded escalation — it does NOT pass the previous response back to Gemini (avoids self-reinforcement of malformed output).
- The missing-API-key check fires BEFORE `tempfile.mkdtemp` so a misconfigured invocation does not litter `/tmp`.
- The `GEMINI_CLI_HOME` cleanup is in a `try/finally` block. If the wrapper crashes between `mkdtemp` and `finally`, the next operator-run cleanup (advisory; not in v1 scope) sweeps `gemini_dispatch_*` prefixes older than 1 hour.
- The fake `gemini` shim used by the test is a tiny Python script (≤80 lines) that reads its own `--shim-mode` flag (set via the test's `GEMINI_SHIM_MODE` env var) and emits one of: a valid envelope, an envelope with malformed JSON in `.response`, or a non-zero exit. The test's pytest fixture overlays the shim onto `PATH`. Keep the shim dependency-free — pure stdlib.
- Restrictive policy TOML uses `priority = 999` as recommended by Gemini's policy-engine docs (highest precedence so user-level allow rules can't override it during a review). Belt-and-braces: the `--approval-mode plan` flag also makes the session read-only, but the policy is the structural enforcement.
- Do NOT pass `--yolo` anywhere. Do NOT pass `--accept-raw-output-risk` anywhere. Both would weaken the review's trust posture.

## Execution log — 20260425T162535 (paused)

Starting SHA: `8a62fc279f1b70368fa5050b2de7938c0aa888ac`  → Ending SHA: `cd799fc65a30d8a85e9a09fb11815dbc9ffcd48c`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 001 | claude | codex | ship-with-fixes [narrow-remediation, disagreement: 0] | ecb91db | D.5 dismissed mode-100644 false-positive; user authorized hand-fix of hardcoded miniconda3 paths + override commit |
| 002 | codex->claude (fallback) | codex | clean | cd799fc | Codex wrapper test-cmd env mismatch (no venv/) -> Claude verification fallback; review clean |
| 003 | claude | codex | needs-rework [post-remediation, awaiting user] |  | D.2a.5 round-2 review surfaced 2 NEW production-contract findings (missing -o json flag, exit-code ignored) |

## Execution log — 20260425T172244 (success)

Starting SHA: `8a62fc279f1b70368fa5050b2de7938c0aa888ac`  → Ending SHA: `d7bd9dd620561810d897e8e7af051153b8a3ff5e`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 003 | claude | codex | clean [remediation] | 837d941 | D.2a.5 envelope-unwrap remediation + 2 user-authorized hand-fixes (-o json + --approval-mode plan + non-zero-exit-as-failure); round-4 clean |
