# TASK-002 — Orphan-child prevention on parent SIGTERM/SIGKILL

## Goal

Orphan-child prevention on parent SIGTERM/SIGKILL for `_claude_backend.invoke`.

## Context

Run 20260425T124346 shipped v1 of `plan_claude_dispatch.py`. The backend adapter `plugins/plan-executor/scripts/_claude_backend.py` uses `subprocess.run(...)` (around line 290) to invoke the nested `claude -p` session. `subprocess.run` does not propagate parent-termination signals to the child: when the wrapper receives SIGTERM (orchestrator timeout), SIGKILL (operator force-kill), or an uncaught exception that bypasses normal cleanup, the nested `claude` process becomes an orphan, reparented to PID 1, and continues running with full repo write access until it completes naturally — by which time the orchestrator may already have moved on.

The fix is to switch to `subprocess.Popen` with `start_new_session=True` (so the child gets its own POSIX process group), wrap the call in a try/finally that registers signal handlers for SIGTERM + SIGINT, and on receipt of those signals call `os.killpg(child_pgid, SIGTERM)` followed by `SIGKILL` after a 5s grace period to reap the entire subprocess tree (the inner `claude` may itself spawn children — its own subagents). An `atexit` hook adds a defense-in-depth path for normal/exception exits.

Signal handlers must be installed only for the duration of `invoke` (try/finally to restore prior handlers), so the wrapper does not clobber the orchestrator's signal handling globally.

## Verification

- `subprocess.run` is replaced with `Popen` + `start_new_session=True` + try/finally that installs/restores SIGTERM and SIGINT handlers.
- Handlers terminate the child process group (`os.killpg`), with a SIGKILL escalation after 5s.
- An `atexit` hook reaps the child if the parent exits via normal return or uncaught exception.
- A test spawns the wrapper with a long-running shim, sends SIGTERM, and confirms the child is gone within 5s.
- A test does the same with SIGINT.
- A test confirms normal-exit reaps the child (no zombie).
- Existing happy-path tests continue to pass.

## Tasks

### TASK-002: Orphan-child prevention on parent SIGTERM/SIGKILL

- **Status:** pending
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/scripts/_claude_backend.py` (modify)
  - `tests/scripts/test_claude_backend.py` (modify)
- **Dependencies:** []
- **Test command:** `venv/bin/python -m pytest tests/scripts/test_claude_backend.py`
- **Acceptance criteria:**
  - Replace `subprocess.run` in `_claude_backend.invoke` with `Popen` + try/finally + `signal.signal` handlers (SIGTERM, SIGINT) that terminate the child claude process before re-raising.
  - Use `start_new_session=True` (POSIX `setsid`) so the child runs in its own process group; on parent termination, `os.killpg(child_pgid, SIGTERM)` followed by `SIGKILL` after a 5s grace period reaps the entire subprocess tree (the inner claude may itself spawn children).
  - `atexit` hook: register a defense-in-depth cleanup that terminates the child process group if the parent exits via uncaught exception or normal return without explicit cleanup.
  - Signal handlers are installed only for the duration of the `invoke` call (try/finally restores the prior handler). Do NOT clobber the orchestrator's handlers globally.
  - Test: spawn the wrapper with a long-running shim (the shim sleeps 60s before printing JSON); send SIGTERM to the wrapper PID; assert the child PID is gone within 5 seconds (poll `/proc/<pid>` existence — present then absent).
  - Test: same flow with SIGINT (the wrapper should propagate equally).
  - Test: shim exits normally → child is reaped, no zombie process.
  - Existing happy-path tests (argv assembly, stdout parsing, malformed JSON, etc.) continue to pass.
- **Reversion guidance:** none

**Description:**

Replaces `subprocess.run` in `_claude_backend.invoke` with a `Popen`-based pattern that propagates parent termination to the nested `claude` process and its subprocess tree. The current implementation orphans the child on parent SIGTERM/SIGKILL because `subprocess.run` provides no signal-forwarding plumbing; the orphaned child continues to hold full repo write access until it exits naturally. The new implementation: (1) spawns the child with `start_new_session=True` so it occupies its own POSIX process group, (2) wraps the wait in try/finally that registers SIGTERM/SIGINT handlers for the duration of the call, (3) on signal, sends SIGTERM to the child's process group and waits up to 5s, then escalates to SIGKILL, (4) restores the prior signal handlers in the finally block (so the orchestrator's signal handling is not clobbered globally), and (5) registers an `atexit` defense-in-depth hook that reaps any still-running child on parent exit. Three new tests exercise the SIGTERM, SIGINT, and normal-exit paths; existing argv/stdout/timeout/malformed-JSON tests must continue to pass unchanged.
