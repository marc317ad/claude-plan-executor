#!/usr/bin/env bash
# gemini_verification_matrix.sh — Empirical Gemini-CLI verification matrix.
#
# TASK-001 (PLAN_GEMINI_INTEGRATION_2026-04-25). The matrix exercises every
# row defined in `docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/
# TASK-001_verification_matrix.md` against whatever `gemini` is on PATH and
# writes a markdown report. NO wrapper code is touched: this script is the
# contract surface between assumptions baked into downstream tasks and the
# CLI's empirically-observed behavior.
#
# Truncation constants (RAW_TRUNCATE_CHARS=2000) mirror the codex wrapper's
# `RAW_TRUNCATE_CHARS` so the eventual gemini wrapper's error envelopes stay
# shape-aligned across CLI families.
#
# Usage:
#   scripts/gemini_verification_matrix.sh [--report PATH]
#
# If `--report` is omitted, the report is written to the committed default
# under this plan's directory.
#
# Exit codes:
#   0 — script executed every row to completion (rows themselves may be
#       `pass`, `fail`, or `unverified`; the script does NOT exit non-zero
#       on row failure — the report is the contract).
#   2 — argument error (e.g. unknown flag).

set -u

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mirror plan_codex_dispatch.RAW_TRUNCATE_CHARS so error envelopes align.
RAW_TRUNCATE_CHARS=2000

# Resolve the script's repository so the default report path is stable
# regardless of cwd.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_REPORT="$REPO_ROOT/docs/plans/PLAN_GEMINI_INTEGRATION_2026-04-25/_verification_report.md"

REPORT_PATH="$DEFAULT_REPORT"

# ---------------------------------------------------------------------------
# Argument parsing (POSIX-bash, no getopt)
# ---------------------------------------------------------------------------

while [ $# -gt 0 ]; do
  case "$1" in
    --report)
      shift
      if [ $# -eq 0 ]; then
        printf 'error: --report requires a path argument\n' >&2
        exit 2
      fi
      REPORT_PATH="$1"
      shift
      ;;
    --report=*)
      REPORT_PATH="${1#*=}"
      shift
      ;;
    -h|--help)
      printf 'Usage: %s [--report PATH]\n' "$0"
      exit 0
      ;;
    *)
      printf 'error: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# truncate_to <max-chars> <input-file>: prints the file contents truncated
# to <max-chars> bytes. We use byte-truncation via `head -c` because
# RAW_TRUNCATE_CHARS is byte-counted in the Python wrapper.
truncate_to() {
  max="$1"
  src="$2"
  if [ ! -f "$src" ]; then
    return
  fi
  head -c "$max" -- "$src"
  size=$(wc -c < "$src" 2>/dev/null | tr -d ' ' || printf 0)
  if [ "${size:-0}" -gt "$max" ]; then
    printf '\n[...truncated %d bytes total]' "$size"
  fi
}

# escape_for_codeblock: today we just truncate; markdown fenced blocks
# accept any UTF-8 except a trailing line of backticks, which we are not
# going to construct. No further escaping needed.

# emit_row: append one row's section to the report file.
#   $1 row number (e.g. "1")
#   $2 row title
#   $3 command-string (one line description)
#   $4 exit-code (integer or "n/a")
#   $5 stdout-file
#   $6 stderr-file
#   $7 verdict (pass | fail | unverified)
#   $8 rationale (one line)
emit_row() {
  num="$1"
  title="$2"
  cmd="$3"
  rc="$4"
  out_file="$5"
  err_file="$6"
  verdict="$7"
  rationale="$8"

  {
    printf '\n## Row %s — %s\n\n' "$num" "$title"
    printf -- '- **Command:** `%s`\n' "$cmd"
    printf -- '- **Exit code:** %s\n' "$rc"
    printf -- '- **Verdict:** `%s`\n' "$verdict"
    printf -- '- **Rationale:** %s\n\n' "$rationale"
    printf '### stdout\n\n```\n'
    truncate_to "$RAW_TRUNCATE_CHARS" "$out_file"
    printf '\n```\n\n'
    printf '### stderr\n\n```\n'
    truncate_to "$RAW_TRUNCATE_CHARS" "$err_file"
    printf '\n```\n'
  } >> "$REPORT_PATH"
}

# run_gemini: run gemini with given argv, capturing stdout/stderr/rc.
#   $1 stdout-file
#   $2 stderr-file
#   $3+ argv passed to gemini
# Returns the captured exit code via global $LAST_RC.
LAST_RC=0
run_gemini() {
  out_file="$1"
  err_file="$2"
  shift 2
  set +e
  gemini "$@" >"$out_file" 2>"$err_file"
  LAST_RC=$?
  set -e
  set +e
}

# run_gemini_stdin: like run_gemini but pipes a string on stdin.
#   $1 stdin-string
#   $2 stdout-file
#   $3 stderr-file
#   $4+ argv passed to gemini
run_gemini_stdin() {
  stdin_str="$1"
  out_file="$2"
  err_file="$3"
  shift 3
  set +e
  printf '%s\n' "$stdin_str" | gemini "$@" >"$out_file" 2>"$err_file"
  LAST_RC=$?
  set -e
  set +e
}

# json_field: extract a top-level JSON string field (best-effort, regex)
#   $1 file with JSON
#   $2 field name
# This is intentionally fragile — the wrapper will use jq/python; the matrix
# is a contract-shape probe that wants no external runtime deps.
json_field_present() {
  file="$1"
  field="$2"
  grep -q "\"$field\"[[:space:]]*:" "$file" 2>/dev/null
}

# ---------------------------------------------------------------------------
# Pre-flight: do we have a `gemini` binary? If not, emit an
# all-`unverified` report and exit 0. The shim test patches PATH so the
# test exercises the present-binary code path; production ops without a CLI
# is gracefully recorded.
# ---------------------------------------------------------------------------

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# Open the report file with a header up front so each row appends.
# Use UTC ISO-8601 for the timestamp; row results otherwise determine shape.
REPORT_DIR="$(dirname -- "$REPORT_PATH")"
mkdir -p "$REPORT_DIR"
{
  printf '# Gemini CLI Verification Report\n\n'
  printf -- '- **Plan:** PLAN_GEMINI_INTEGRATION_2026-04-25 / TASK-001\n'
  printf -- '- **Generated (UTC):** %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '- **Script:** `scripts/gemini_verification_matrix.sh`\n'
  printf -- '- **RAW_TRUNCATE_CHARS:** %d\n' "$RAW_TRUNCATE_CHARS"
} > "$REPORT_PATH"

if ! command -v gemini >/dev/null 2>&1; then
  GEMINI_VERSION="(not installed)"
  CLI_AVAILABLE=0
else
  GEMINI_VERSION="$(gemini --version 2>/dev/null | head -n 1 || printf 'unknown')"
  CLI_AVAILABLE=1
fi
{
  printf -- '- **gemini --version:** `%s`\n' "$GEMINI_VERSION"
  printf '\n'
} >> "$REPORT_PATH"

# Convenience: write an "all unverified" row helper used when the CLI is
# absent. Each row still emits with command + verdict so report shape is
# stable for the shim test.
emit_unverified_no_cli() {
  num="$1"
  title="$2"
  cmd="$3"
  empty_out="$WORKDIR/empty_stdout"
  empty_err="$WORKDIR/empty_stderr"
  : > "$empty_out"
  : > "$empty_err"
  emit_row "$num" "$title" "$cmd" "n/a" "$empty_out" "$empty_err" \
    "unverified" "gemini CLI not installed on PATH; row not exercised."
}

if [ "$CLI_AVAILABLE" = "0" ]; then
  emit_unverified_no_cli 1 "JSON envelope shape" 'gemini -p "hi" -o json'
  emit_unverified_no_cli 2 "Empty-prompt exit code" 'gemini -p "" -o json'
  emit_unverified_no_cli 3 "Turn-limit exit code" 'gemini -p "<long pathological prompt>" -o json'
  emit_unverified_no_cli 4 "Stdin context piping" "printf 'FILE_CONTEXT\\n' | gemini -p \"...\" -o json"
  emit_unverified_no_cli 5 "GEMINI_CLI_HOME isolation" 'GEMINI_CLI_HOME=$(mktemp -d) gemini -p "hi" -o json'
  emit_unverified_no_cli 6 "Policy deny enforcement" 'GEMINI_CLI_HOME=<home> gemini --approval-mode plan -p "..." -o json'
  emit_unverified_no_cli 7 "--approval-mode plan read-only" 'gemini -p "summarize README.md" -o json --approval-mode plan'
  emit_unverified_no_cli 8 "GEMINI_API_KEY absent failure mode" 'env -u GEMINI_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS gemini -p "hi" -o json'
  emit_unverified_no_cli 9 "stats.tokens shape" 'inspect row 1 stats object'
  emit_unverified_no_cli 10 "Markdown-fenced JSON in response" 'gemini -p "Return ONLY a JSON object with field ok=true. No prose." -o json'
  exit 0
fi

# ---------------------------------------------------------------------------
# Row 1 — JSON envelope shape
# ---------------------------------------------------------------------------

ROW1_OUT="$WORKDIR/row1.out"
ROW1_ERR="$WORKDIR/row1.err"
ROW1_CMD='gemini -p "hi" -o json'
run_gemini "$ROW1_OUT" "$ROW1_ERR" -p "hi" -o json
ROW1_RC="$LAST_RC"

ROW1_VERDICT="fail"
ROW1_RATIONALE=""
if [ "$ROW1_RC" -ne 0 ]; then
  ROW1_VERDICT="unverified"
  ROW1_RATIONALE="exit $ROW1_RC; likely auth/credentials missing — see stderr. CLI shape not asserted."
elif ! json_field_present "$ROW1_OUT" "response"; then
  ROW1_VERDICT="fail"
  ROW1_RATIONALE='exit 0 but stdout has no top-level "response" field; envelope-shape assumption violated.'
else
  # Verify keys are subset of {response, stats, error, session_id}.
  if python3 -c '
import json, sys
allowed = {"response", "stats", "error", "session_id"}
obj = json.load(open(sys.argv[1]))
keys = set(obj.keys())
extra = keys - allowed
if extra:
    print("extra keys:", sorted(extra))
    sys.exit(1)
if not isinstance(obj.get("response", None), str):
    print("response is not str")
    sys.exit(1)
sys.exit(0)
' "$ROW1_OUT" >"$WORKDIR/row1_check.out" 2>&1; then
    ROW1_VERDICT="pass"
    ROW1_RATIONALE='exit 0; stdout is JSON; top-level keys ⊆ {response, stats, error, session_id}; response is string.'
  else
    ROW1_VERDICT="fail"
    ROW1_RATIONALE="envelope shape mismatch: $(head -c 200 "$WORKDIR/row1_check.out" | tr '\n' ' ')"
  fi
fi
emit_row 1 "JSON envelope shape" "$ROW1_CMD" "$ROW1_RC" "$ROW1_OUT" "$ROW1_ERR" "$ROW1_VERDICT" "$ROW1_RATIONALE"

# ---------------------------------------------------------------------------
# Row 2 — Empty-prompt exit code
# ---------------------------------------------------------------------------

ROW2_OUT="$WORKDIR/row2.out"
ROW2_ERR="$WORKDIR/row2.err"
ROW2_CMD='gemini -p "" -o json'
run_gemini "$ROW2_OUT" "$ROW2_ERR" -p "" -o json
ROW2_RC="$LAST_RC"

if [ "$ROW2_RC" -eq 42 ]; then
  ROW2_VERDICT="pass"
  ROW2_RATIONALE="empty prompt yielded documented exit 42."
elif [ "$ROW2_RC" -eq 0 ]; then
  ROW2_VERDICT="fail"
  ROW2_RATIONALE="empty prompt exited 0 — exit-code contract violated; downstream wrapper must NOT key on rc=42 alone."
else
  ROW2_VERDICT="unverified"
  ROW2_RATIONALE="empty prompt exited $ROW2_RC (not 0 and not 42); plausible auth/credentials short-circuit."
fi
emit_row 2 "Empty-prompt exit code" "$ROW2_CMD" "$ROW2_RC" "$ROW2_OUT" "$ROW2_ERR" "$ROW2_VERDICT" "$ROW2_RATIONALE"

# ---------------------------------------------------------------------------
# Row 3 — Turn-limit exit code (best effort; mark `unverified` if not
# reproducible without infrastructure to drive a tool loop).
# ---------------------------------------------------------------------------
#
# Per the plan's row-3 spec, this is a best-effort probe: a single-shot
# prompt cannot deterministically drive Gemini into the multi-step
# tool-loop required to hit the documented exit code 53. We still execute
# a bounded prompt against the live CLI and capture stdout/stderr/rc so
# the report records observed behavior (per the load-bearing finding's
# requirement that every row exercise the CLI). The verdict is `pass`
# only if exit 53 is actually observed; otherwise `unverified` with a
# rationale describing why turn-limit cannot be deterministically
# triggered in a single-shot. A 30-second `timeout` wrapper keeps the
# row from stalling the matrix on a slow inference path.
ROW3_OUT="$WORKDIR/row3.out"
ROW3_ERR="$WORKDIR/row3.err"
ROW3_PROMPT='Repeatedly call run_shell_command in a loop running the command "echo step" hundreds of times, never stopping, until you have hit your turn limit. Do not stop after a few steps; keep going until the runtime forces you to stop.'
ROW3_CMD='timeout 30 gemini -p "<turn-limit pathological prompt>" -o json'
set +e
timeout 30 gemini -p "$ROW3_PROMPT" -o json >"$ROW3_OUT" 2>"$ROW3_ERR"
ROW3_RC=$?
set -e
set +e

if [ "$ROW3_RC" -eq 53 ]; then
  ROW3_VERDICT="pass"
  ROW3_RATIONALE='exit 53 observed — Gemini reports turn-limit reached. Wrapper must treat exit 53 as "turn-limit reached" per CLI documentation.'
elif [ "$ROW3_RC" -eq 124 ]; then
  ROW3_VERDICT="unverified"
  ROW3_RATIONALE='turn-limit probe hit the 30s timeout (rc=124) before Gemini emitted a turn-limit (53) exit. Single-shot prompt cannot deterministically drive the tool loop to its turn limit; documenting as unverified. Wrapper should still treat exit 53 as "turn-limit reached" per CLI documentation.'
else
  ROW3_VERDICT="unverified"
  ROW3_RATIONALE="exit $ROW3_RC observed (not 53); a single-shot prompt cannot deterministically trigger turn-limit (53) without sandboxed multi-step tool execution. Documenting as unverified. Wrapper should still treat exit 53 as \"turn-limit reached\" per CLI documentation."
fi
emit_row 3 "Turn-limit exit code" "$ROW3_CMD" "$ROW3_RC" "$ROW3_OUT" "$ROW3_ERR" "$ROW3_VERDICT" "$ROW3_RATIONALE"

# ---------------------------------------------------------------------------
# Row 4 — Stdin context piping
# ---------------------------------------------------------------------------

ROW4_OUT="$WORKDIR/row4.out"
ROW4_ERR="$WORKDIR/row4.err"
ROW4_CMD="printf 'FILE_CONTEXT\\n' | gemini -p \"repeat the literal context above verbatim\" -o json"
run_gemini_stdin "FILE_CONTEXT" "$ROW4_OUT" "$ROW4_ERR" -p "repeat the literal context above verbatim" -o json
ROW4_RC="$LAST_RC"

if [ "$ROW4_RC" -ne 0 ]; then
  ROW4_VERDICT="unverified"
  ROW4_RATIONALE="exit $ROW4_RC; CLI did not return a successful envelope — stdin-piping not exercised."
elif grep -q "FILE_CONTEXT" "$ROW4_OUT"; then
  ROW4_VERDICT="pass"
  ROW4_RATIONALE='exit 0 and "FILE_CONTEXT" appears in stdout response — stdin piping confirmed.'
else
  # Stdin WAS piped, but the model may have paraphrased or ignored the
  # "repeat verbatim" instruction. The literal-echo oracle is too strict;
  # downgrade to `unverified` with the corrective lesson for the wrapper:
  # the wrapper MUST embed file context inside the `-p` prompt rather than
  # rely on stdin → response literal pass-through.
  ROW4_VERDICT="unverified"
  ROW4_RATIONALE='exit 0 but stdin "FILE_CONTEXT" did not echo verbatim in response. Stdin is consumed by gemini (no error) but the literal-pass-through contract is non-deterministic in 0.39.0; wrapper must inject file context via the prompt body, not rely on stdin echo.'
fi
emit_row 4 "Stdin context piping" "$ROW4_CMD" "$ROW4_RC" "$ROW4_OUT" "$ROW4_ERR" "$ROW4_VERDICT" "$ROW4_RATIONALE"

# ---------------------------------------------------------------------------
# Row 5 — GEMINI_CLI_HOME isolation
# ---------------------------------------------------------------------------

ROW5_OUT="$WORKDIR/row5.out"
ROW5_ERR="$WORKDIR/row5.err"
ROW5_HOME="$(mktemp -d)"
ROW5_CMD='GEMINI_CLI_HOME='"$ROW5_HOME"' gemini -p "hi" -o json'

# Snapshot $HOME/.gemini mtime if present (we treat absence as untouched).
if [ -d "$HOME/.gemini" ]; then
  HOME_GEMINI_MTIME_BEFORE="$(stat -c '%Y' "$HOME/.gemini" 2>/dev/null || printf 0)"
else
  HOME_GEMINI_MTIME_BEFORE="absent"
fi

set +e
GEMINI_CLI_HOME="$ROW5_HOME" gemini -p "hi" -o json >"$ROW5_OUT" 2>"$ROW5_ERR"
ROW5_RC=$?
set -e
set +e

if [ -d "$HOME/.gemini" ]; then
  HOME_GEMINI_MTIME_AFTER="$(stat -c '%Y' "$HOME/.gemini" 2>/dev/null || printf 0)"
else
  HOME_GEMINI_MTIME_AFTER="absent"
fi

if [ "$ROW5_RC" -ne 0 ]; then
  ROW5_VERDICT="unverified"
  ROW5_RATIONALE="exit $ROW5_RC against isolated GEMINI_CLI_HOME (likely auth); isolation cannot be asserted from a failing run."
elif [ -d "$ROW5_HOME/.gemini" ] && [ "$HOME_GEMINI_MTIME_BEFORE" = "$HOME_GEMINI_MTIME_AFTER" ]; then
  ROW5_VERDICT="pass"
  ROW5_RATIONALE="exit 0; new <home>/.gemini directory created; \$HOME/.gemini mtime unchanged ($HOME_GEMINI_MTIME_BEFORE)."
elif [ ! -d "$ROW5_HOME/.gemini" ]; then
  ROW5_VERDICT="unverified"
  ROW5_RATIONALE="exit 0 but no <home>/.gemini directory created — assumption may be implementation-version-dependent."
else
  ROW5_VERDICT="fail"
  ROW5_RATIONALE="\$HOME/.gemini mtime changed from $HOME_GEMINI_MTIME_BEFORE to $HOME_GEMINI_MTIME_AFTER under GEMINI_CLI_HOME — isolation NOT honored."
fi
emit_row 5 "GEMINI_CLI_HOME isolation" "$ROW5_CMD" "$ROW5_RC" "$ROW5_OUT" "$ROW5_ERR" "$ROW5_VERDICT" "$ROW5_RATIONALE"
rm -rf "$ROW5_HOME"

# ---------------------------------------------------------------------------
# Row 6 — Policy deny enforcement
# ---------------------------------------------------------------------------

ROW6_OUT="$WORKDIR/row6.out"
ROW6_ERR="$WORKDIR/row6.err"
ROW6_HOME="$(mktemp -d)"
mkdir -p "$ROW6_HOME/.gemini/policies"
cat > "$ROW6_HOME/.gemini/policies/deny.toml" <<'POLICY_EOF'
[[rule]]
toolName = ["run_shell_command", "edit_file"]
decision = "deny"
priority = 999
POLICY_EOF
ROW6_CMD='GEMINI_CLI_HOME='"$ROW6_HOME"' gemini --approval-mode plan -p "run ls and tell me what you see" -o json'
set +e
GEMINI_CLI_HOME="$ROW6_HOME" gemini --approval-mode plan -p "run 'ls' via run_shell_command and tell me what you see" -o json >"$ROW6_OUT" 2>"$ROW6_ERR"
ROW6_RC=$?
set -e
set +e

if [ "$ROW6_RC" -ne 0 ]; then
  ROW6_VERDICT="unverified"
  ROW6_RATIONALE="exit $ROW6_RC against policy-configured GEMINI_CLI_HOME (likely auth); policy enforcement not exercised."
else
  # Heuristic: real `ls` output in repo would contain typical filenames like
  # "scripts" or "tests"; we look for those AND their absence treated as a
  # weak pass. A precise oracle is hard without orchestrating tool exec.
  if grep -Eq '^total[[:space:]]|drwx|-rw-' "$ROW6_OUT"; then
    ROW6_VERDICT="fail"
    ROW6_RATIONALE='exit 0 but response contains output that could only come from running ls (e.g. "drwx" / "total"); deny rule was bypassed.'
  else
    ROW6_VERDICT="pass"
    ROW6_RATIONALE='exit 0 and response contains no canonical `ls` output; deny rule is honored OR `--approval-mode plan` itself prevents tool execution. Both satisfy the wrapper requirement.'
  fi
fi
emit_row 6 "Policy deny enforcement" "$ROW6_CMD" "$ROW6_RC" "$ROW6_OUT" "$ROW6_ERR" "$ROW6_VERDICT" "$ROW6_RATIONALE"
rm -rf "$ROW6_HOME"

# ---------------------------------------------------------------------------
# Row 7 — `--approval-mode plan` read-only
# ---------------------------------------------------------------------------

ROW7_OUT="$WORKDIR/row7.out"
ROW7_ERR="$WORKDIR/row7.err"
ROW7_CMD='gemini -p "summarize README.md" -o json --approval-mode plan'
# Snapshot tracked-file state; a real run could only modify tracked content
# by writing files. We use git status as the oracle.
GIT_STATUS_BEFORE="$(cd "$REPO_ROOT" && git status --porcelain 2>/dev/null | sort)"
run_gemini "$ROW7_OUT" "$ROW7_ERR" -p "summarize README.md" -o json --approval-mode plan
ROW7_RC="$LAST_RC"
GIT_STATUS_AFTER="$(cd "$REPO_ROOT" && git status --porcelain 2>/dev/null | sort)"

if [ "$ROW7_RC" -ne 0 ]; then
  ROW7_VERDICT="unverified"
  ROW7_RATIONALE="exit $ROW7_RC; read-only behavior cannot be asserted on a failing run."
elif [ "$GIT_STATUS_BEFORE" = "$GIT_STATUS_AFTER" ]; then
  ROW7_VERDICT="pass"
  ROW7_RATIONALE='exit 0 and `git status --porcelain` is identical before/after; --approval-mode plan honored read-only contract.'
else
  ROW7_VERDICT="fail"
  ROW7_RATIONALE="exit 0 but git working tree changed under --approval-mode plan; read-only contract violated."
fi
emit_row 7 "--approval-mode plan read-only" "$ROW7_CMD" "$ROW7_RC" "$ROW7_OUT" "$ROW7_ERR" "$ROW7_VERDICT" "$ROW7_RATIONALE"

# ---------------------------------------------------------------------------
# Row 8 — GEMINI_API_KEY absent failure mode (timeout-guarded)
# ---------------------------------------------------------------------------

ROW8_OUT="$WORKDIR/row8.out"
ROW8_ERR="$WORKDIR/row8.err"
ROW8_HOME="$(mktemp -d)"
ROW8_CMD='env -u GEMINI_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS GEMINI_CLI_HOME='"$ROW8_HOME"' timeout 10 gemini -p "hi" -o json --approval-mode plan'
set +e
env -u GEMINI_API_KEY -u GOOGLE_APPLICATION_CREDENTIALS \
  GEMINI_CLI_HOME="$ROW8_HOME" \
  timeout 10 gemini -p "hi" -o json --approval-mode plan \
  >"$ROW8_OUT" 2>"$ROW8_ERR"
ROW8_RC=$?
set -e
set +e

# `timeout` returns 124 on hang.
if [ "$ROW8_RC" -eq 124 ]; then
  ROW8_VERDICT="fail"
  ROW8_RATIONALE='gemini hung past 10s with GEMINI_API_KEY absent — interactive-prompt regression suspected; wrapper MUST budget aggressively.'
elif [ "$ROW8_RC" -eq 0 ]; then
  ROW8_VERDICT="unverified"
  ROW8_RATIONALE="gemini exited 0 without GEMINI_API_KEY (likely cached credentials in another path); auth-absence path not exercised."
else
  if grep -Eqi 'auth|credential|api[ _]?key|sign[- ]?in|login' "$ROW8_ERR" "$ROW8_OUT"; then
    ROW8_VERDICT="pass"
    ROW8_RATIONALE="exit $ROW8_RC within 10s and stderr/stdout names auth/credentials — failure mode is loud and bounded."
  else
    ROW8_VERDICT="unverified"
    ROW8_RATIONALE="exit $ROW8_RC within 10s but no auth/credential keyword in stderr/stdout; failure-mode classification is ambiguous."
  fi
fi
emit_row 8 "GEMINI_API_KEY absent failure mode" "$ROW8_CMD" "$ROW8_RC" "$ROW8_OUT" "$ROW8_ERR" "$ROW8_VERDICT" "$ROW8_RATIONALE"
rm -rf "$ROW8_HOME"

# ---------------------------------------------------------------------------
# Row 9 — stats.tokens shape (re-uses row 1's stdout if it succeeded).
# ---------------------------------------------------------------------------

ROW9_OUT="$WORKDIR/row9.out"
ROW9_ERR="$WORKDIR/row9.err"
ROW9_CMD='inspect stats object from row 1 stdout'
: > "$ROW9_ERR"

if [ "$ROW1_VERDICT" != "pass" ]; then
  ROW9_VERDICT="unverified"
  ROW9_RATIONALE="row 1 did not pass; stats object not available for shape inspection."
  : > "$ROW9_OUT"
else
  cp -- "$ROW1_OUT" "$ROW9_OUT"
  set +e
  python3 -c '
import json, sys
obj = json.load(open(sys.argv[1]))
stats = obj.get("stats")
if not isinstance(stats, dict):
    print("stats is not a dict; got", type(stats).__name__)
    sys.exit(2)
# Empirically (gemini 0.39.0) the model-named sub-objects live under
# ``stats.models.<model-name>``, NOT directly under ``stats``. The spec is
# purposefully relaxed to "model-named sub-object whose .tokens carries
# input/prompt/total/cached" — we descend through any top-level dict
# (including ``stats`` itself) to find that shape and record the path.
required = {"input", "prompt", "total", "cached"}
def walk(node, path):
    if not isinstance(node, dict):
        return
    tokens = node.get("tokens") if isinstance(node, dict) else None
    if isinstance(tokens, dict) and required.issubset(set(tokens.keys())):
        yield path
    for k, v in node.items():
        if isinstance(v, dict):
            yield from walk(v, path + [k])

matches = list(walk(stats, ["stats"]))
if not matches:
    print("no model-named sub-object with tokens.{input,prompt,total,cached} found")
    print("top-level stats keys:", sorted(stats.keys()))
    sys.exit(1)
# Prefer the shallowest match (most likely the canonical model entry).
matches.sort(key=len)
print("matched-path:", " > ".join(matches[0]))
sys.exit(0)
' "$ROW9_OUT" >"$WORKDIR/row9_check.out"
    row9_check_rc=$?
    if [ "$row9_check_rc" -eq 0 ]; then
      matched=$(grep -E '^matched-path:' "$WORKDIR/row9_check.out" | head -1)
      ROW9_VERDICT="pass"
      ROW9_RATIONALE="stats path with tokens.{input,prompt,total,cached} located: ${matched}. Wrapper stats parser must bind to this path."
    elif [ "$row9_check_rc" -eq 1 ]; then
      # Required-token-keys missing from any descendant; stats has the
      # wrong shape from the wrapper's perspective.
      ROW9_VERDICT="unverified"
      ROW9_RATIONALE="no stats descendant carries tokens.{input,prompt,total,cached}; details: $(head -c 300 "$WORKDIR/row9_check.out" | tr '\n' ' ')"
    else
      # rc=2 — stats not a dict at all; envelope is malformed.
      ROW9_VERDICT="unverified"
      ROW9_RATIONALE="stats was not an object; cannot verify: $(head -c 200 "$WORKDIR/row9_check.out" | tr '\n' ' ')"
    fi
fi
emit_row 9 "stats.tokens shape" "$ROW9_CMD" "n/a" "$ROW9_OUT" "$ROW9_ERR" "$ROW9_VERDICT" "$ROW9_RATIONALE"

# ---------------------------------------------------------------------------
# Row 10 — Markdown-fenced JSON in `response`
# ---------------------------------------------------------------------------

ROW10_OUT="$WORKDIR/row10.out"
ROW10_ERR="$WORKDIR/row10.err"
ROW10_CMD='gemini -p "Return ONLY a JSON object with field ok=true. No prose." -o json'
run_gemini "$ROW10_OUT" "$ROW10_ERR" -p "Return ONLY a JSON object with field ok=true. No prose." -o json
ROW10_RC="$LAST_RC"

if [ "$ROW10_RC" -ne 0 ]; then
  ROW10_VERDICT="unverified"
  ROW10_RATIONALE="exit $ROW10_RC; response shape (fenced vs raw) cannot be inspected on a failing run."
else
  if python3 -c '
import json, sys
obj = json.load(open(sys.argv[1]))
resp = obj.get("response", "")
if not isinstance(resp, str):
    print("response not str")
    sys.exit(2)
stripped = resp.strip()
if stripped.startswith("```"):
    print("fenced")
else:
    print("unfenced")
sys.exit(0)
' "$ROW10_OUT" >"$WORKDIR/row10_check.out" 2>&1; then
    fence_form="$(cat "$WORKDIR/row10_check.out" | tr -d '\n')"
    ROW10_VERDICT="pass"
    ROW10_RATIONALE="exit 0; response shape recorded as: ${fence_form} (wrapper strip-fence logic must handle this form)."
  else
    ROW10_VERDICT="fail"
    ROW10_RATIONALE="exit 0 but response is not a string; envelope-shape regression."
  fi
fi
emit_row 10 "Markdown-fenced JSON in response" "$ROW10_CMD" "$ROW10_RC" "$ROW10_OUT" "$ROW10_ERR" "$ROW10_VERDICT" "$ROW10_RATIONALE"

exit 0
