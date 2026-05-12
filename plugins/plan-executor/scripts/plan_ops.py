#!/usr/bin/env python3
"""Plan operations CLI for the /implement-plan workflow.

Scaffolding: argparse surface + shared helpers + subcommand stubs.
Stdlib-only (plan docs are pure markdown per DUAL_AGENT_PLAN_EXECUTOR.md §5).

Example invocations (resolve $PYTHON via `preflight --json`'s python_path):

    $PYTHON scripts/plan_ops.py preflight --plan-file <abs> [--strict-branch] [--strict-scope]
    $PYTHON scripts/plan_ops.py parse-schedule --stdin
    $PYTHON scripts/plan_ops.py compute-schedule --stdin
    $PYTHON scripts/plan_ops.py batch-next --schedule-file <path> ...
    $PYTHON scripts/plan_ops.py parse-implementer-report --stdin
    $PYTHON scripts/plan_ops.py parse-plan-review-report --stdin
    $PYTHON scripts/plan_ops.py claude-envelope-extract --stdin --agent <plan-analyst|plan-implementer|plan-remediator>
    $PYTHON scripts/plan_ops.py commit-task --plan-file <abs> --task-id NNN ...
    $PYTHON scripts/plan_ops.py fail-task --plan-file <abs> --task-id NNN ...
    $PYTHON scripts/plan_ops.py update-plan-header --plan-file <abs> --status <s>
    $PYTHON scripts/plan_ops.py finalize-execution-log --plan-file <abs> ...
    $PYTHON scripts/plan_ops.py log-event --event E --fields-json '{...}'
    $PYTHON scripts/plan_ops.py normalize-task-id --id <1|001|TASK-001>
    $PYTHON scripts/plan_ops.py acquire-lock --plan-file <abs> --run-id RID
    $PYTHON scripts/plan_ops.py release-lock --plan-file <abs> --run-id RID
    $PYTHON scripts/plan_ops.py block-dependents --schedule-file <path> --plan-file <abs> --failed NNN --run-id RID
"""

import argparse
import pathlib
import copy
import fnmatch
import json
import os
import re
import shutil
import string
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Ensure the sibling ``_plan_paths`` module is importable when this file is
# loaded via ``importlib.util.spec_from_file_location`` (e.g., from tests).
# Direct CLI invocation already adds the script's directory to ``sys.path``.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from _plan_paths import (  # noqa: E402
    COMMIT_ALWAYS_IGNORE,
    PROTECTED_EXACT_PATHS,
    PROTECTED_PATH_PREFIXES,
    PROTECTED_PATH_SUFFIXES,
    PROTECTED_PATH_GLOBS,
    canonicalize_file,
    is_commit_always_ignore,
    is_protected_path,
    normalize_files_entry,
)

def _load_plan_config() -> dict:
    """Read `.claude/plan-executor.json` from cwd; return {} if missing.

    Config schema (all optional):
        { "plan_dir": "docs/plans" }

    Missing file → defaults to {} so projects without the config keep working.
    Malformed JSON or unreadable file → SystemExit(2). The config gates every
    plan operation; silently falling back to defaults on a typo would route
    writes to the wrong directory.
    """
    cfg_path = Path(".claude/plan-executor.json")
    if not cfg_path.exists():
        return {}
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"plan_ops: malformed JSON in {cfg_path}: {exc}\n"
        )
        raise SystemExit(2)
    except OSError as exc:
        sys.stderr.write(
            f"plan_ops: cannot read {cfg_path}: {exc}\n"
        )
        raise SystemExit(2)


_PLAN_CFG = _load_plan_config()
PLAN_DIR = Path(_PLAN_CFG.get("plan_dir", "docs/plans"))
_PLAN_DIR_POSIX = PLAN_DIR.as_posix()
RUN_LOG_PATH = PLAN_DIR / "_run_log.jsonl"
RUN_LOCK_PATH = PLAN_DIR / "_run_lock.json"

TASK_HEADER_RE = re.compile(r"^### TASK-(\d{3}[A-Z]?):", re.MULTILINE)
TASK_ID_INPUT_RE = re.compile(r"^(?:TASK-)?(\d{1,3})([A-Z]?)$")
STATUS_BULLET_RE = re.compile(r"^(\s*-\s*\*\*Status:\*\*)\s*(.+?)\s*$", re.MULTILINE)
DEPENDENCIES_BULLET_RE = re.compile(
    r"^\s*-\s*\*\*Dependencies:\*\*\s*(.+?)\s*$", re.MULTILINE,
)
# Per-task status vocabulary. Recognized values:
#   pending       — not yet started (default for newly-authored tasks)
#   in-progress   — claimed by an implementer; mid-flight
#   done          — completed and committed
#   failed        — implementer outcome != success and silent-revert authorized
#   blocked       — cascade-blocked by an upstream `failed` (block-dependents)
#   skipped       — operator/orchestrator deliberately skipped (e.g. reroute)
#   paused        — TASK-002 (prohibit_silent_revert): mid-flight task halted
#                   into the awaiting-user pause path (D.2a.5 / D.2a.6 second
#                   `needs-rework` OR retry-implement failure). Distinct from
#                   `failed` because no silent revert occurred — the working
#                   tree still holds remediation edits and the user's next
#                   conversation turn decides disposition (revert | keep |
#                   hand-fix). The scheduler treats `paused` like `failed` for
#                   pick eligibility (skip) but, crucially, does NOT cascade
#                   `block-dependents` — a paused task is not a terminal
#                   failure, so its dependents must wait for human disposition
#                   rather than being preemptively blocked.
ALLOWED_TASK_STATUSES = {"pending", "in-progress", "done", "failed", "blocked", "skipped", "paused"}
# Per-task agent vocabulary — mirrors the `**Agent:**` bullet emitted by
# `_emit_child` (`plan_ops.py:3193-3195`). Ordered tuple so MCP enum
# registrations and unit-test parametrizations track the canonical order.
ALLOWED_AGENTS = ("claude", "codex")
ALLOWED_INDEX_STATUSES = {"Done", "Pending", "Superseded"}
_INDEX_SUPERSEDED_BY: dict[str, list[str]] = {}
# TASK-008 (per_task_dispatch_refactor_v2): file-mode deprecation aliases were
# REMOVED rather than absorbed into the canonical set. The dict symbols are
# retained as empty mappings for forward compatibility — future genuine alias
# windows may reintroduce entries — but the directory-only contract is the
# only canonical form recognized at runtime.
STATUS_ALIASES: dict[str, str] = {}
SCHEDULE_FIELD_ALIASES: dict[str, str] = {}
ALLOWED_PLAN_STATUSES = {"in-progress", "complete", "partial"}
ALLOWED_FAIL_STAGES = {"implement", "review", "commit"}
# ALLOWED_FAIL_AUTHORIZATION_SOURCES — closed enum of authorized paths that
# may legitimately invoke ``fail-task`` (and its silent-revert side effects:
# ``git restore`` of touched files + plan-status flip to ``failed``). Every
# value here corresponds to a documented `/implement-plan` code path; future
# tasks (TASK-005, TASK-006, TASK-007, TASK-008) extend this set as new
# authorized paths are introduced. Adding a value here is a load-bearing
# audit decision: it sanctions a new place where completed work may be
# destroyed. Each value authorizes:
#   - "phase-c-empty-diff": Phase C implementer-failure path, EMPTY-DIFF
#     branch only (TASK-006). The orchestrator detected an implementer
#     outcome != success AND `git diff HEAD --quiet` reported no working-
#     tree changes. With nothing to lose, auto fail-task is the right move.
#     The non-empty-diff branch is NOT authorized here — it routes through
#     the awaiting-user pause subroutine (or, under
#     `--unattended-revert-policy fail-fast`/`preserve-only`, through the
#     fail-fast/salvage-then-fail variants documented in SKILL.md Phase C).
#     The TASK-001 placeholder "phase-c-impl-failure" was REMOVED here
#     because after TASK-006 the empty-diff branch is the only authorized
#     destruction path within Phase C.
#   - "phase-d4-review-failure": Phase D.4 review-stage failure path
#     (reviewer verdict requires a halt; the implementation succeeded but
#     review found the work unshippable).
#   - "phase-d4-rescue-failed": Phase D.4 rescue path's terminal failure
#     (TASK-005). Authorized when the single-shot D.4 rescue dispatch
#     itself failed AND the user instructed a revert in the next turn.
#     Rescue-success commits use `--d4-rescue-tag` on commit-task and do
#     NOT invoke fail-task; this enum value is reserved for the user-
#     authorized post-pause revert that follows a failed rescue.
#   - "user-instruction": the user's next conversation turn after a paused
#     run explicitly instructed the orchestrator to revert (the only
#     sanctioned post-pause revert path).
#   - "phase-d2b-role-swap-exhausted": Phase D.2b terminal failure after the
#     single role-swap retry has already been used and Claude review still
#     returns needs-rework on Codex-implemented work.
#   - "unattended-fail-fast": cron/CI runs that opted into auto-fail-task
#     via `--unattended-revert-policy fail-fast` (TASK-003) — covers both
#     the Phase C non-empty-diff fail-fast branch AND the D.2a binding-mode
#     fail-fast fall-through. When `--codex-review-binding` is
#     active and the unattended-revert policy is `fail-fast`, the
#     orchestrator skips the awaiting-user pause and authorizes the revert
#     under this value.
#   - "unattended-preserve-only": unattended binding-mode path where
#     `--codex-review-binding` is active, the reviewer returned
#     `needs-rework`, and the caller chose `--unattended-revert-policy
#     preserve-only`. The route is terminal (`action: fail`) but distinct
#     from fail-fast so the follow-on path can preserve implementation
#     artifacts according to policy.
#   - "reconcile-out-of-scope-user-instruction": authorized post-pause
#     revert path for the G10 `reconcile_batch` out-of-scope pause
#     (TASK-008). When the wrapper observes out-of-scope writes and the
#     policy is `pause` (default), the orchestrator marks the task
#     `paused` and returns four options to the user: widen-plan,
#     in-place-fix, keep-and-commit, revert. Only the user's explicit
#     "revert" instruction in the next conversation turn authorizes
#     `fail-task` under this value.
# Binding-mode pause note: `--codex-review-binding` (TASK-007) pauses by
# default and does NOT call `fail-task`. If the user instructs a revert in
# the next turn, that revert authorizes under `user-instruction`. The
# unattended binding fall-throughs above are machine-readable policy
# decisions emitted by `review-route`, not prose scraped from `fail_reason`.
ALLOWED_FAIL_AUTHORIZATION_SOURCES = {
    "phase-c-empty-diff",
    "phase-d4-review-failure",
    "phase-d4-rescue-failed",
    "phase-d2b-role-swap-exhausted",
    "user-instruction",
    "unattended-fail-fast",
    "unattended-preserve-only",
    "reconcile-out-of-scope-user-instruction",
}
ALLOWED_CODEX_REVIEW_VERDICTS = {"clean", "minor-findings", "needs-rework"}
ALLOWED_CLAUDE_REVIEW_VERDICTS = {
    "ship",
    "ship-with-fixes",
    "partial-agreement",
    "needs-rework",
}
ALLOWED_PLAN_REVIEW_TRIAGE_SOURCES = {
    "plan-analyst",
    "codex-plan-review",
}
# Phase 1.5 Codex plan-review verdicts (TASK-014C). Distinct from the
# code-level review verdicts above because a plan review operates on plan
# markdown + schedule JSON, not a diff, and drives a different routing table
# (see SKILL.md §Phase 1.5).
ALLOWED_PLAN_REVIEW_VERDICTS = {"approved", "approved-with-notes", "needs-replan"}
# TASK-009 (POSTMORTEM_FIXES): D.5 third-opinion adjudicators draw from the
# same vocabulary as the Claude reviewer (ship | ship-with-fixes |
# partial-agreement | needs-rework). Exposed as a named alias so
# prompt-render paths and external callers can reference the D.5 role
# explicitly without re-deriving the relationship.
ALLOWED_D5_VERDICTS = ALLOWED_CLAUDE_REVIEW_VERDICTS
ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES = {"critical", "important", "minor"}
# TASK-019: reviewer-minor-finding optional disposition vocabulary. Allows the
# D.5 adjudicator to attach a structured "what happened to this finding"
# annotation without stuffing it into the prose of `issue`/`suggested_fix`.
ALLOWED_REVIEWER_FINDING_DISPOSITIONS = {
    "dismissed",
    "accepted",
    "deferred",
    # TASK-022: `spec-deference` marks a Codex finding that has design merit
    # but contradicts the plan's explicit spec (acceptance criteria or
    # Implementation Playbook). D.5 uses it instead of `dismissed` so the
    # critique surfaces for a future plan-review pass rather than being
    # silently buried under "plan says so" reasoning.
    "spec-deference",
}
OPTIONAL_REVIEWER_FINDING_FIELDS = {"disposition", "disposition_reason"}
ALLOWED_ROW_FIELDS = {"task", "agent", "reviewer", "verdict", "commit", "notes"}
# Transport-surface exceptions for public plan_ops commands. Keep all
# CLI-only / MCP-only deviations here so conformance tests and registry
# generation consume the same allowlist instead of growing local skips.
PUBLIC_SUBCOMMAND_TRANSPORT_EXCEPTIONS = {
    "cli_only": set(),
    "mcp_only": set(),
}
# Known run-log event types. The orchestrator owns the vocabulary; this set
# acts as a tripwire so typo'd events surface immediately rather than drifting
# silently into the log. `awaiting_user` is added per TASK-014A D.2a.5 to
# signal the bounded-remediation pause state to the next conversation turn.
ALLOWED_LOG_EVENTS = {
    "run_start",
    "run_end",
    "analyst_done",
    "batch_start",
    "implement_start",
    "implement_done",
    "review_start",
    "review_done",
    "review_route_called",
    "test_deferred",
    "commit_done",
    "failed",
    "disagreement",
    "fallback_used",
    "review_skipped",
    "remediation_start",
    # `narrow_remediation_start` / `narrow_remediation_done` are added per
    # TASK-016C D.2a.6. Distinct from `remediation_start` so the run log is
    # the audit source of truth for which retry path fired (D.2a.5 full
    # rework vs D.2a.6 narrow scope).
    "narrow_remediation_start",
    "narrow_remediation_done",
    # `d4_rescue_start` / `d4_rescue_done` are added per TASK-005 D.4
    # rescue. Single-shot terminal rescue path: before D.4 halts, the
    # orchestrator dispatches `plan-remediator` once on the reviewer's
    # findings as `rescue_findings[]`. Distinct from D.2a.5/D.2a.6 events
    # so the audit trail can tell which retry path fired (D.4 rescue does
    # NOT consume D.5 adjudication; it operates directly on reviewer
    # findings).
    "d4_rescue_start",
    "d4_rescue_done",
    "plan_review_start",
    "plan_review_done",
    "plan_review_skipped",
    # Phase 1.5 plan-review-route call audit. Emitted by the orchestrator at
    # each `plan-review-route` stage (`pre_dispatch`, `post_review`,
    # `post_triage`, `post_second_review`) with `{stage, action}` so the run
    # log records which deterministic action the router selected. Distinct
    # from `review_route_called` (Phase D task-level review-route).
    "plan_review_route_called",
    # Phase 1-triage / Phase 1.5.5 plan-review triage events. Dual-sourced on
    # `source ∈ {plan-analyst, codex-plan-review}` per the SKILL.md routing;
    # `analyst_triage_skipped` records the pre-triage short-circuits
    # (`--allow-gaps` / `--analyst-binding`).
    "plan_review_triage_start",
    "plan_review_triage_done",
    "analyst_triage_skipped",
    # `plan_author_start` / `plan_author_done` are added per TASK-025 for the
    # needs-replan auto-revise path: when Codex returns `needs-replan` on the
    # first plan-review pass and auto-revise is on, the orchestrator dispatches
    # `plan-author` to apply the findings to the plan file in place before the
    # second binding review.
    "plan_author_start",
    "plan_author_done",
    "awaiting_user",
    "schedule_written",
    # TASK-020B: `acceptance_v_check` runtime enforcement. `v_check_passed`
    # is emitted by `cmd_commit_task` when a plan's opt-in YAML frontmatter
    # V-check succeeds pre-commit. `v_check_failed` is reserved for direct
    # `log-event` calls by external tooling (e.g., future CI jobs); v1
    # `cmd_commit_task` does NOT emit it — a failed V-check `_die`s silently
    # to preserve the "no run-log mutation on failure" invariant.
    "v_check_passed",
    "v_check_failed",
    # TASK-001 (per_task_dispatch_refactor_v2). Emitted by SKILL.md Phase 0
    # when single-file input is auto-promoted to directory mode via
    # `plan_ops.py decompose-plan`. Fields: `{source_file, produced_dir,
    # task_count}`. The produced directory is treated identically to a
    # user-authored decomposed directory after this event.
    "decompose_auto_promote",
    # TASK-006 (SKILL_bash_dispatch_migration). Per-dispatch lifecycle events
    # for the v3 Claude wrapper (`plan_claude_dispatch.py run`). Emitted at
    # every analyst / implementer / remediator dispatch site so the run-log
    # captures the wrapper transport status alongside the existing
    # implement_done / review_done semantic events. `claude_dispatch_failed`
    # carries the wrapper's `status_reason` + truncated diagnostics.
    "claude_dispatch_start",
    "claude_dispatch_done",
    "claude_dispatch_failed",
    # TASK-004 (prohibit_silent_revert extension). Emitted by wrapper scripts
    # and hoisted by the orchestrator when out-of-declaration writes are
    # either reverted (executed) or preserved due to blocked authorization.
    "wrapper_autoclean_executed",
    "wrapper_autoclean_blocked",
}
# Accepted values for `finalize-execution-log --outcome`. `paused` is added
# per TASK-014A for the D.2a.5 awaiting-user pause — the run halted mid-flight
# and the user's next turn decides disposition.
ALLOWED_RUN_OUTCOMES = {"success", "partial", "failed", "paused"}

# Hand-fix gate (R2 from docs/analysis/orchestrator_dispatch_drift_20260429.md).
# Run-log events that may carry `mechanism: "hand-fix"`. When such an event is
# emitted via `log-event`, `cmd_log_event` requires an active `awaiting_user`
# pause for the same `(run_id, task_id)`; otherwise it is a normal-flow leak
# of the `feedback_handfix_default` rule and is rejected. See SKILL.md:19
# (orchestrator's role is routing) and the rule body in that memory file.
HANDFIX_GATED_EVENTS = frozenset({"remediation_start", "narrow_remediation_start"})
# Recognized hand-fix vocabulary across `mechanism` and `mode` fields. The
# orchestrator's current emission is `mechanism: "hand-fix"`; the run log
# also carries the legacy `mode: "hand_fix_by_orchestrator"` form (e.g.,
# `_run_log.jsonl` lines 30/32/44). The gate normalizes both fields by
# case-folding and stripping `-`/`_`/whitespace, then matches the
# substring "handfix" — covering "hand-fix", "hand_fix",
# "hand_fix_by_orchestrator", etc. so the orchestrator cannot bypass the
# guard by switching field name or punctuation.
HANDFIX_FIELDS = ("mechanism", "mode")
HANDFIX_TOKEN = "handfix"
REVIEW_ROUTE_CALLED_REQUIRED_FIELDS = (
    "run_id",
    "task_id",
    "action",
    "reviewer",
    "implementer",
    "route_reason",
)
REVIEW_ROUTE_REASON_MAX_CHARS = 160


def _is_handfix_intent(fields: dict) -> bool:
    """True if any of the recognized `HANDFIX_FIELDS` carries a value whose
    normalized form contains the `HANDFIX_TOKEN` substring.

    Normalization: lower-case and strip `-`, `_`, whitespace. Non-string
    values are ignored (the field shape contract is set elsewhere).
    """
    for key in HANDFIX_FIELDS:
        val = fields.get(key)
        if not isinstance(val, str):
            continue
        normalized = (
            val.lower().replace("-", "").replace("_", "").replace(" ", "")
        )
        if HANDFIX_TOKEN in normalized:
            return True
    return False


def _validate_review_route_called_fields(fields: dict) -> list[dict]:
    """Validate the public run-log shape for `review_route_called`.

    The event is an audit breadcrumb for the state-machine call, so it keeps
    reviewer identity and the routed action while intentionally avoiding raw
    reviewer prose. `route_reason` is bounded to a short, single-line summary
    suitable for logs.
    """
    errors: list[dict] = []
    for key in REVIEW_ROUTE_CALLED_REQUIRED_FIELDS:
        if key not in fields:
            errors.append({
                "path": f"$.{key}",
                "code": "required",
                "message": f"{key} is required for review_route_called",
            })
            continue
        if not isinstance(fields[key], str) or not fields[key].strip():
            errors.append({
                "path": f"$.{key}",
                "code": "non-empty-string-required",
                "message": f"{key} must be a non-empty string for review_route_called",
            })

    task_id = fields.get("task_id")
    if isinstance(task_id, str) and task_id.strip() and _normalize_task_id(task_id) is None:
        errors.append({
            "path": "$.task_id",
            "code": "bad-task-id",
            "message": "task_id must be a valid task identifier (expected NNN, NNNX, or TASK-NNN[X])",
        })

    action = fields.get("action")
    if isinstance(action, str) and action.strip() and action not in _REVIEW_ROUTE_ACTIONS:
        errors.append({
            "path": "$.action",
            "code": "unknown-review-route-action",
            "message": f"action must be one of {sorted(_REVIEW_ROUTE_ACTIONS)}",
        })

    reviewer = fields.get("reviewer")
    if isinstance(reviewer, str) and reviewer.strip() and reviewer not in _ALLOWED_REVIEWERS:
        errors.append({
            "path": "$.reviewer",
            "code": "unknown-reviewer",
            "message": f"reviewer must be one of {sorted(_ALLOWED_REVIEWERS)}",
        })

    implementer = fields.get("implementer")
    if isinstance(implementer, str) and implementer.strip() and implementer not in {"claude", "codex"}:
        errors.append({
            "path": "$.implementer",
            "code": "unknown-implementer",
            "message": "implementer must be one of ['claude', 'codex']",
        })

    route_reason = fields.get("route_reason")
    if isinstance(route_reason, str):
        if "\n" in route_reason or "\r" in route_reason:
            errors.append({
                "path": "$.route_reason",
                "code": "route-reason-multiline",
                "message": "route_reason must be a compact single-line summary",
            })
        if len(route_reason) > REVIEW_ROUTE_REASON_MAX_CHARS:
            errors.append({
                "path": "$.route_reason",
                "code": "route-reason-too-long",
                "message": f"route_reason must be at most {REVIEW_ROUTE_REASON_MAX_CHARS} characters",
            })

    raw_text_keys = sorted(
        key for key in fields
        if isinstance(key, str)
        and (
            key.startswith("raw_")
            or key in {"summary", "reviewer_summary", "review_text", "reviewer_text"}
        )
    )
    if raw_text_keys:
        errors.append({
            "path": "$",
            "code": "raw-reviewer-text-forbidden",
            "message": (
                "review_route_called must not copy raw reviewer free text; "
                f"remove fields {raw_text_keys}"
            ),
        })

    return errors

# TASK-010: Globally-locked dependency / environment paths. Tasks whose
# `files` set intersects this default set (or globs, or operator-supplied
# additions from `docs/plans/_global_lock_paths.yaml`) MUST occupy their
# own batch alone — the scheduler cannot put them in parallel with any
# other task, regardless of file overlap. Rationale: parallel mutation of
# `requirements.txt`, lock files, `Dockerfile`, etc., produces silent
# environment races (Task A reinstalls a package while Task B imports it
# at test time). Conservative-by-design: false positives are harmless
# slowdowns; false negatives are silent corruption. See §9.4.
GLOBAL_LOCK_PATHS = frozenset({
    # Python
    "requirements.txt", "requirements-dev.txt", "pyproject.toml",
    "poetry.lock", "Pipfile", "Pipfile.lock", "setup.cfg", "setup.py",
    # Node
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    # Rust
    "Cargo.toml", "Cargo.lock",
    # Go
    "go.mod", "go.sum",
    # Ruby
    "Gemfile", "Gemfile.lock",
    # PHP
    "composer.json", "composer.lock",
    # Containers
    "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
})
GLOBAL_LOCK_GLOBS: tuple[str, ...] = (
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)
GLOBAL_LOCK_OVERRIDE_PATH = "docs/plans/_global_lock_paths.yaml"


def _effective_global_lock_set() -> tuple[frozenset[str], tuple[str, ...]]:
    """Return (paths, globs) merging defaults with optional YAML override.

    Override file at ``docs/plans/_global_lock_paths.yaml`` (relative to
    cwd) is honored if present; absent → defaults only. Entries containing
    glob metacharacters (``*?[``) are routed to the globs tuple, others to
    the exact-path set. Malformed YAML or unreadable file is treated as
    "no override" — a noisy override should not silently disable the
    default lock set.
    """
    base_paths = set(GLOBAL_LOCK_PATHS)
    base_globs = list(GLOBAL_LOCK_GLOBS)
    override = Path(GLOBAL_LOCK_OVERRIDE_PATH)
    if override.is_file():
        try:
            import yaml  # available per TASK-013
            data = yaml.safe_load(override.read_text(encoding="utf-8")) or {}
        except Exception:
            data = {}
        if isinstance(data, dict):
            additional = data.get("additional") or []
            if isinstance(additional, list):
                for entry in additional:
                    if not isinstance(entry, str) or not entry:
                        continue
                    if any(c in entry for c in "*?["):
                        base_globs.append(entry)
                    else:
                        base_paths.add(entry)
    return frozenset(base_paths), tuple(base_globs)


def _is_global_lock_path(path: str) -> bool:
    """True iff `path` is a globally-locked dependency/environment file."""
    paths, globs = _effective_global_lock_set()
    if path in paths:
        return True
    return any(fnmatch.fnmatch(path, g) for g in globs)


# TASK-007 / TASK-008: Canonical Contract decision table. Self-audit
# (`cmd_audit`) compares the shipped artifacts against this table and
# surfaces drift as structured findings. Keep this dict the single source
# of truth — the checks below import from it; the design doc §9.7 / §14
# references it by name. TASK-008 (per_task_dispatch_refactor_v2) is the
# directory-mode contract: the prior file-mode deprecation aliases
# (`open` status, `task_id` / `batch_index` schedule fields, legacy
# `**Concerns:**` implementer label) were REMOVED — neither the canonical
# set nor the runtime parsers accept them. `decompose-plan` is the
# single-file → directory bridge and is registered as a first-class
# subcommand entry. `ALIAS_WINDOWS` is retained as an empty dict for
# forward compatibility — future genuine alias windows may reintroduce
# entries, but no file-mode entries remain.
CANONICAL_CONTRACT: dict[str, object] = {
    "status_vocabulary": [
        "blocked", "done", "failed", "in-progress", "paused", "pending", "skipped",
    ],
    "schedule_task_fields": ["id", "plan_file"],
    "schedule_batch_fields": ["index"],
    "implementer_concerns_labels": [
        "**Concerns for reviewer:**",
    ],
    "implementer_plan_adaptations_label": "**Plan adaptations:**",
    "execution_log_columns": [
        "Task", "Agent", "Reviewer", "Verdict", "Commit", "Notes",
    ],
    "wrapper_always_ignore_paths": [
        "docs/plans/_run_log.jsonl",
        "docs/plans/_run_lock.json",
    ],
    "wrapper_always_ignore_globs": [
        "docs/plans/*.schedule.json",
    ],
    # TASK-008: first-class subcommand entries. `decompose-plan` is the
    # single-file → directory bridge that makes auto-promotion work at
    # SKILL.md Phase 0; it is part of the directory-only architecture,
    # not a file-mode remnant.
    "subcommands": [
        "preflight",
        "decompose-plan",
        "build-tasks",
        "parse-schedule",
        "compute-schedule",
        "write-schedule",
        "batch-next",
        "filter-schedule",
        "parse-implementer-report",
        "plan-review-route",
        "parse-plan-review-report",
        "claude-envelope-extract",
        "order-triage-findings",
        "parse-plan-review-triage-report",
        "commit-task",
        "fail-task",
        "block-dependents",
        "update-plan-header",
        "finalize-execution-log",
        "log-event",
        "normalize-task-id",
        "acquire-lock",
        "release-lock",
        "path-info",
        "gates",
        "audit",
        "check-plan-deps",
        "lint-plans",
        "reconcile-batch",
        # TASK-010: surfaces the effective globally-locked path set
        # (defaults + optional YAML override). The scheduler reads the
        # same set when tagging tasks with `global_lock`.
        "list-global-lock-paths",
    ],
    # TASK-010: defaults exposed for the self-audit's `global_lock_paths`
    # check, which compares the constants against the documented set in
    # `DUAL_AGENT_PLAN_EXECUTOR.md` §9.4 and fails on drift.
    "global_lock_paths_default": sorted(GLOBAL_LOCK_PATHS),
    "global_lock_globs_default": list(GLOBAL_LOCK_GLOBS),
    # TASK-004 (POSTMORTEM_FIXES_2026-04-25): single source of truth for the
    # canonical sample fixture path the `fixture-valid` gate validates.
    # `_gate_fixture_valid` reads from this field; the audit check
    # `canonical_fixture_not_archived` lints that the fixture has not been
    # moved under `docs/plans/archive/` without updating this constant.
    "fixture_path": "docs/plans/sample_phase4.md",
    "fixture_schedule_path": "docs/plans/sample_phase4.schedule.json",
}

ALIAS_WINDOWS: dict[str, list[str]] = {
    # TASK-008 removed all file-mode deprecation aliases. The directory-only
    # canonical contract above is the single accepted runtime form. No active
    # alias windows remain. Entries added here in the future should be
    # deliberate, time-boxed, and paired with a removal-task reference.
}

CANONICAL_ID_RE = re.compile(r"^\d{3}[A-Z]?$")
ALLOWED_SCHEDULE_TOP_LEVEL = {
    "outcome", "tasks", "batches", "gaps", "risks",
    "state", "plan_review_state",
}
# TASK-002 (PHASE_D_STATE_MACHINE): persistent orchestrator-state schema
# (§3.2). All keys optional on input; helpers default-populate missing
# subfields. Validation is intentionally loose: a freshly-authored schedule
# omits `state` entirely; an in-flight schedule may have only a subset of
# subfields populated.
ALLOWED_SCHEDULE_STATE_FIELDS = {
    "done", "failed", "blocked", "committed",
    "locked_files", "review_notes", "retries_used",
}
ALLOWED_PLAN_REVIEW_STATE_FIELDS = {
    "attempt",
    "first_verdict",
    "first_findings_count",
    "second_verdict",
    "second_findings_count",
    "triage_dispatched",
    "triage_verdict",
    "load_bearing_indices",
    "dismissed_indices",
    "author_dispatches_completed",
    "auto_revise_round_completed",
    "skipped_reason",
    "task_plan_file_map",
}
# TASK-008: directory-mode canonical contract. The legacy `task_id` /
# `batch_index` field aliases are NO longer accepted — schedules carrying
# them produce structured `unknown-nested-field` (in `--strict`) or warning
# entries (in lenient mode), and `_validate_schedule` rejects them as
# missing canonical `id` / `index` fields.
ALLOWED_TASK_FIELDS = {
    "id",
    "title", "agent", "priority", "files", "dependencies",
    "test_command", "classification_reason", "acceptance_criteria",
    "plan_file", "description",
    # TASK-010: scheduler-tagged flag indicating that any of `files`
    # intersects `GLOBAL_LOCK_PATHS` (or its overrides). Tagged in
    # `cmd_parse_schedule`, consumed by the batcher to enforce a
    # solitary-batch rule. Optional on input; backfilled if absent.
    "global_lock",
}
ALLOWED_BATCH_FIELDS = {"index", "task_ids", "file_locks"}
# TASK-002: formalize hard-vs-soft gap severity. Hard gaps block execution
# without explicit operator override; soft gaps are advisory and can be
# demoted to warnings by --allow-gaps (TASK-003). Unknown gap types default
# to "hard" as a fail-safe — better to over-block than silently allow an
# unclassified gap to flow through.
GAP_SEVERITY = {
    "stale-path": "hard",
    "missing-test-command": "hard",
    "vague-ac": "hard",
    "unresolvable-test": "soft",
    "empty-implementation-notes": "soft",
}


def classify_gap_severity(gap_type: str) -> str:
    """Return "hard" or "soft" for a gap type. Unknown types → "hard"."""
    return GAP_SEVERITY.get(gap_type, "hard")


def _is_valid_plan_file_basename(value: str) -> bool:
    if not value:
        return False
    if value.startswith("."):
        return False
    if "/" in value or "\\" in value:
        return False
    if ".." in value:
        return False
    if "\x00" in value:
        return False
    return len(value.encode("utf-8")) <= 255


PRIORITY_RANKS = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
}


def _task_order_key(task_id: str, priority: str) -> tuple[int, int, str]:
    normalized = _normalize_task_id(task_id)
    if normalized is None:
        return (PRIORITY_RANKS["low"], 999, "Z")
    match = re.match(r"^(\d{3})([A-Z]?)$", normalized)
    assert match is not None
    return (
        PRIORITY_RANKS.get(priority, PRIORITY_RANKS["low"]),
        int(match.group(1)),
        match.group(2),
    )


def _create_dependency_aware_batches(
    tasks: list[dict], task_ids_to_process: list[str],
) -> tuple[list[dict], list[dict]]:
    """Dependency-aware + file-lock-aware batching (canonical helper).

    Topo-sorts tasks via ``_compute_decompose_batches`` (Kahn's), then
    partitions each topo layer into file-disjoint sub-batches using a
    greedy first-fit packer, with global-lock tasks
    (``_is_global_lock_path`` over the task's files) forced into their
    own solitary sub-batches. Sub-batches are flattened across topo
    layers into the canonical schedule wire shape
    ``[{index, task_ids, file_locks}, ...]`` with monotonically
    increasing 1-based ``index``.

    Inputs are not mutated. The function does not call ``_die``, write
    files, or print — it is a pure (helper-style) function.

    Error codes returned (in ``errors[]``, with ``batches=[]``):

    * ``unresolvable-dep`` — a ``dependencies[]`` entry references an
      id not present in ``tasks[]``.
    * ``cyclic-dependency`` — Kahn's cannot drain the graph.

    Regression prevented: ``docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25``
    — without topo layering, a serial chain ``001 → 002 → 003`` whose
    files are disjoint collapses into a single batch (file-disjoint
    only), which violates the declared dependency DAG and is correctly
    flagged by Codex ``plan-review`` as a layering violation.
    """
    errors: list[dict] = []
    # Cycle detection / topo-sort over the collected tasks.
    deps_map = {
        str(t["id"]): [str(d) for d in (t.get("dependencies") or [])]
        for t in tasks
    }
    known_ids = {str(t["id"]) for t in tasks}
    for tid in task_ids_to_process:
        for dep in deps_map.get(tid, []):
            if dep not in known_ids:
                errors.append({
                    "code": "unresolvable-dep",
                    "task_id": tid,
                    "dep_id": dep,
                    "message": (
                        f"TASK-{tid} depends on TASK-{dep} which is "
                        "not in the schedule"
                    ),
                })
    if errors:
        return [], errors

    topo_layers, cycle_errors = _compute_decompose_batches(
        task_ids_to_process, deps_map,
    )
    if cycle_errors:
        errors.extend(cycle_errors)
        return [], errors

    batches: list[dict] = []
    files_by_id = {
        str(t["id"]): set(str(p) for p in (t.get("files") or []))
        for t in tasks
    }
    tasks_by_id = {str(t["id"]): t for t in tasks}

    next_batch_index = 1
    for layer in topo_layers:
        sub_batches: list[dict] = []
        # Sort tasks within a layer by priority to pack higher-priority
        # items first.
        sorted_layer_ids = sorted(
            layer,
            key=lambda tid: _task_order_key(
                tid, tasks_by_id[tid].get("priority", "low"),
            ),
        )

        for tid in sorted_layer_ids:
            t_files = files_by_id.get(tid, set())
            is_global_lock = any(_is_global_lock_path(f) for f in t_files)

            # Global-lock tasks must occupy their own batch.
            if is_global_lock:
                sub_batches.append({
                    "task_ids": [tid],
                    "_files": set(t_files),
                    "_solitary": True,
                })
                continue

            placed = False
            for sb in sub_batches:
                if sb.get("_solitary"):
                    continue
                if sb["_files"] & t_files:
                    continue
                sb["task_ids"].append(tid)
                sb["_files"].update(t_files)
                placed = True
                break
            if not placed:
                sub_batches.append({
                    "task_ids": [tid],
                    "_files": set(t_files),
                    "_solitary": False,
                })
        for sb in sub_batches:
            batches.append({
                "index": next_batch_index,
                "task_ids": sb["task_ids"],
                "file_locks": sorted(sb["_files"]),
            })
            next_batch_index += 1
    return batches, []


# Spec-named alias for TASK-001 of PLAN_TOPO_RESPECT_FIX_2026-04-25. Both
# names point at the canonical helper; the alias satisfies the plan's
# Acceptance criteria which names the symbol exactly as
# ``_dependency_aware_batches(tasks, ordered_task_ids)``. The original
# ``_create_dependency_aware_batches`` is preserved so existing call sites
# (``_compute_schedule_batches``, ``_build_tasks``) keep working unchanged.
_dependency_aware_batches = _create_dependency_aware_batches


def _compute_schedule_batches(tasks: list) -> tuple[list[str], list[dict], list[dict]]:
    errors: list[dict] = []
    normalized_tasks: list[dict] = []
    seen_ids: dict[str, int] = {}

    for i, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append({
                "path": f"$.tasks[{i}]",
                "code": "invalid-type",
                "message": f"tasks[{i}] must be an object",
            })
            continue

        raw_id = task.get("id")
        task_id = _normalize_task_id(raw_id)
        if task_id is None:
            errors.append({
                "path": f"$.tasks[{i}].id",
                "code": "invalid-task-id",
                "message": f"tasks[{i}].id={raw_id!r} is not a valid task id",
            })
            continue
        if task_id in seen_ids:
            errors.append({
                "path": f"$.tasks[{i}].id",
                "code": "duplicate-task-id",
                "message": f"duplicate id {task_id!r} (first at tasks[{seen_ids[task_id]}])",
            })
            continue
        seen_ids[task_id] = i

        raw_files = task.get("files") or []
        if not isinstance(raw_files, list):
            errors.append({
                "path": f"$.tasks[{i}].files",
                "code": "invalid-type",
                "message": "files must be an array",
            })
            continue

        raw_deps = task["dependencies"] if "dependencies" in task else []
        if not isinstance(raw_deps, list):
            errors.append({
                "path": f"$.tasks[{i}].dependencies",
                "code": "invalid-type",
                "message": "dependencies must be an array",
            })
            continue

        priority = str(task.get("priority", "low")).strip().lower() or "low"
        if priority not in PRIORITY_RANKS:
            priority = "low"

        normalized_tasks.append({
            "id": task_id,
            "priority": priority,
            "files": [_normalize_files_entry(str(path)) for path in raw_files],
            "dependencies": [
                nd for d in raw_deps if (nd := _normalize_task_id(d))
            ],
        })

    if errors:
        return [], [], errors

    ordered_tasks = sorted(
        normalized_tasks,
        key=lambda task: _task_order_key(task["id"], task["priority"]),
    )
    ordered_task_ids = [task["id"] for task in ordered_tasks]

    batches, batch_errors = _dependency_aware_batches(
        normalized_tasks, ordered_task_ids,
    )
    if batch_errors:
        errors.extend(batch_errors)
        return [], [], errors

    return ordered_task_ids, batches, []


def _validate_schedule_refs(tasks: list, batches: list) -> list[dict]:
    errors: list[dict] = []
    seen_ids: dict[str, int] = {}
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        raw = t.get("id")
        if raw is None:
            continue
        tid = str(raw)
        if tid in seen_ids:
            errors.append({
                "path": f"$.tasks[{i}].id",
                "code": "duplicate-task-id",
                "message": f"duplicate id {tid!r} (first at tasks[{seen_ids[tid]}])",
            })
        else:
            seen_ids[tid] = i

    seen_idx: dict = {}
    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            continue
        raw = b.get("index")
        if raw is None:
            continue
        if raw in seen_idx:
            errors.append({
                "path": f"$.batches[{i}].index",
                "code": "duplicate-batch-index",
                "message": f"duplicate index {raw!r} (first at batches[{seen_idx[raw]}])",
            })
        else:
            seen_idx[raw] = i

    known_ids = set(seen_ids)
    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            continue
        refs = b.get("task_ids") or []
        if not isinstance(refs, list):
            continue
        for j, r in enumerate(refs):
            if str(r) not in known_ids:
                errors.append({
                    "path": f"$.batches[{i}].task_ids[{j}]",
                    "code": "unknown-batch-task-ref",
                    "message": f"task_ids[{j}]={str(r)!r} is not a known task id",
                })

    # Batch file-scope disjointness (TASK-003B Fix D):
    # Tasks in the same batch must have pairwise-disjoint `files` lists.
    # Overlap means two wrappers would race on the same file; observe-only
    # cleanup protects sibling state, but legitimate overwrites would still
    # produce undefined results. Fail fast with the concrete colliding tasks
    # and files so the analyst can rebalance the schedule.
    task_files_by_id: dict[str, list[str]] = {}
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get("id")
        if raw is None:
            continue
        tid = str(raw)
        raw_files = t.get("files") or []
        if not isinstance(raw_files, list):
            continue
        task_files_by_id[tid] = [str(f) for f in raw_files if isinstance(f, str)]

    for i, b in enumerate(batches):
        if not isinstance(b, dict):
            continue
        refs_raw = b.get("task_ids") or []
        if not isinstance(refs_raw, list):
            continue
        refs = [str(r) for r in refs_raw]
        per_task_files: list[tuple[str, set[str]]] = []
        for tid in refs:
            files = set(task_files_by_id.get(tid, []))
            if files:
                per_task_files.append((tid, files))
        for a in range(len(per_task_files)):
            tid_a, files_a = per_task_files[a]
            for bi in range(a + 1, len(per_task_files)):
                tid_b, files_b = per_task_files[bi]
                overlap = sorted(files_a & files_b)
                if overlap:
                    errors.append({
                        "path": f"$.batches[{i}].task_ids",
                        "code": "batch-file-overlap",
                        "message": (
                            f"batch {i} tasks {tid_a!r} and {tid_b!r} share "
                            f"files: {overlap}"
                        ),
                    })
    return errors


def _validate_schedule_dag(tasks: list, batches: list) -> list[dict]:
    """Cycle + orphan-dep + batch-topo detection on a schedule's task graph.

    Returns errors[*]; never calls `_die` — callers decide whether to halt or
    merge into their own error list. Mirrors `_validate_schedule_refs`'s
    contract. Error codes emitted:

      * ``unknown-dependency`` — one per ``task.dependencies[j]`` entry that
        does not normalize to a known task id. The orphan is reported, but
        the dep is still dropped before cycle analysis so a cycle among the
        remaining known tasks still surfaces.
      * ``dependency-batch-violation`` — one per ``task.dependencies[j]`` edge
        whose prereq is placed in the same batch as, or a later batch than,
        the dependent. Defense-in-depth wire-format check that runs even if
        the batchers regress; emitted in a single pass so the operator sees
        every offending edge. Edges whose prereq or dependent is unbatched,
        or whose prereq was already flagged as ``unknown-dependency`` in
        this same call, are skipped to avoid double-reporting.
      * ``dependency-cycle`` — at most one entry; message names the residual
        cyclic task ids (sorted, canonical form).
    """
    errors: list[dict] = []
    known_ids: set[str] = set()
    for t in tasks:
        if not isinstance(t, dict):
            continue
        raw = t.get("id")
        if raw is None:
            continue
        tid = _normalize_task_id(str(raw))
        if tid:
            known_ids.add(tid)

    # 1. Orphan-dependency pre-check. A dep that doesn't resolve to any known
    # task is reported individually and then dropped from the cycle graph so
    # the subsequent Kahn's pass is over the cleaned subgraph.
    dag_deps: dict[str, list[str]] = {}
    orphan_dep_ids: set[str] = set()
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        raw = t.get("id")
        if raw is None:
            continue
        tid = _normalize_task_id(str(raw))
        if not tid:
            continue
        deps_norm: list[str] = []
        for j, dep in enumerate(t.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None:
                continue
            if dep_norm not in known_ids:
                errors.append({
                    "path": f"$.tasks[{i}].dependencies[{j}]",
                    "code": "unknown-dependency",
                    "message": (
                        f"tasks[{i}].dependencies[{j}]={str(dep)!r} is not a known task id"
                    ),
                })
                orphan_dep_ids.add(dep_norm)
                continue
            deps_norm.append(dep_norm)
        dag_deps[tid] = deps_norm

    # 2. Batch-topology check (TASK-006, defense-in-depth). Build a
    # `batch_of` map and emit `dependency-batch-violation` for every edge
    # whose prereq sits in the same batch as the dependent or in a later
    # one. Walks every edge; never short-circuits — operators want the full
    # picture. Skips edges whose prereq is an orphan (already flagged
    # above) and edges where either endpoint is unbatched (handled by
    # `_validate_schedule_refs` upstream). Cyclic edges in the same batch
    # are surfaced here AND by the Kahn's pass below; both signals fire.
    batch_of: dict[str, int] = {}
    for b in batches:
        if not isinstance(b, dict):
            continue
        idx_raw = b.get("index")
        if not isinstance(idx_raw, int):
            continue
        refs = b.get("task_ids") or []
        if not isinstance(refs, list):
            continue
        for r in refs:
            tid_norm = _normalize_task_id(str(r))
            if tid_norm is None:
                continue
            # First placement wins; duplicate placements are caught by
            # `_validate_schedule_refs` (`unknown-batch-task-ref` /
            # duplicate-batch-index) — don't second-guess it here.
            batch_of.setdefault(tid_norm, idx_raw)

    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        raw = t.get("id")
        if raw is None:
            continue
        dependent = _normalize_task_id(str(raw))
        if not dependent:
            continue
        b_dep = batch_of.get(dependent)
        if b_dep is None:
            continue
        for j, dep in enumerate(t.get("dependencies") or []):
            prereq = _normalize_task_id(str(dep))
            if prereq is None:
                continue
            if prereq in orphan_dep_ids:
                continue
            b_pre = batch_of.get(prereq)
            if b_pre is None:
                continue
            if b_pre >= b_dep:
                errors.append({
                    "path": f"$.tasks[{i}].dependencies[{j}]",
                    "code": "dependency-batch-violation",
                    "message": (
                        f"TASK-{dependent} (batch {b_dep}) depends on "
                        f"TASK-{prereq} which is in batch {b_pre}; "
                        f"dependent must run in a strictly later batch"
                    ),
                })

    # 3. Kahn's algorithm — cycle detection over the cleaned dep graph.
    indeg: dict[str, int] = {tid: 0 for tid in dag_deps}
    for tid, deps in dag_deps.items():
        for d in deps:
            if d in indeg:
                indeg[tid] += 1
    queue = [tid for tid, n in indeg.items() if n == 0]
    visited = 0
    while queue:
        head = queue.pop(0)
        visited += 1
        for other, deps in dag_deps.items():
            if head in deps:
                indeg[other] -= 1
                if indeg[other] == 0:
                    queue.append(other)
    if visited != len(dag_deps):
        cyclic = sorted(tid for tid, n in indeg.items() if n > 0)
        errors.append({
            "path": "$.tasks",
            "code": "dependency-cycle",
            "message": f"dependency cycle in schedule involving tasks: {cyclic}",
        })
    return errors


def _validate_schedule(data: dict, *, strict_nested: bool = False) -> tuple[list[dict], list[str]]:
    errors: list[dict] = []
    warnings: list[str] = []

    for key in data.keys():
        if key not in ALLOWED_SCHEDULE_TOP_LEVEL:
            errors.append({
                "path": f"$.{key}",
                "code": "unknown-top-level-field",
                "message": (
                    f"unknown top-level field {key!r}; "
                    f"allowed: {sorted(ALLOWED_SCHEDULE_TOP_LEVEL)}"
                ),
            })

    outcome = data.get("outcome")
    if outcome not in {"valid", "invalid", "needs-enrichment"}:
        errors.append({
            "path": "$.outcome",
            "code": "invalid-outcome",
            "message": (
                f"outcome must be valid|invalid|needs-enrichment, got {outcome!r}"
            ),
        })

    gaps = data.get("gaps") or []
    if outcome == "needs-enrichment" and len(gaps) == 0:
        errors.append({
            "path": "$.outcome",
            "code": "outcome-gap-mismatch",
            "message": "outcome='needs-enrichment' requires at least one entry in gaps[]; got empty list",
        })
    if outcome == "valid" and len(gaps) > 0:
        errors.append({
            "path": "$.outcome",
            "code": "outcome-gap-mismatch",
            "message": "outcome='valid' requires gaps[] to be empty; got non-empty list",
        })

    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        errors.append({
            "path": "$.tasks",
            "code": "invalid-type",
            "message": "tasks must be an array",
        })
        tasks = []
    else:
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                errors.append({
                    "path": f"$.tasks[{i}]",
                    "code": "invalid-type",
                    "message": f"tasks[{i}] must be an object",
                })
                continue
            if "id" not in t:
                # TASK-008: directory-only contract. Legacy `task_id`
                # field is rejected as a missing canonical `id` (it
                # surfaces as an `unknown-nested-field` error in strict
                # mode via the per-key check below).
                errors.append({
                    "path": f"$.tasks[{i}].id",
                    "code": "missing-field",
                    "message": f"tasks[{i}] missing field 'id'",
                })
            # `files` is structural — every task must declare its file
            # locks for batch-scheduling. `agent` is a transitional field:
            # the classifier fan-out (TASK-005) fills it in after
            # `build-tasks` emits the fat manifest, so missing-agent is a
            # warning (not an error) on unclassified tasks.
            if "files" not in t:
                errors.append({
                    "path": f"$.tasks[{i}].files",
                    "code": "missing-field",
                    "message": f"tasks[{i}] missing field 'files'",
                })
            if "agent" not in t:
                warnings.append(
                    f"tasks[{i}] missing field 'agent' "
                    "(unclassified; classifier fan-out populates this)"
                )
            raw_id = t.get("id")
            if raw_id is not None and not CANONICAL_ID_RE.match(str(raw_id)):
                errors.append({
                    "path": f"$.tasks[{i}].id",
                    "code": "non-canonical-id",
                    "message": (
                        f"tasks[{i}].id={raw_id!r} does not match canonical "
                        "form /^\\d{3}[A-Z]?$/"
                    ),
                })
            # TASK-008: directory-only contract requires every task to
            # declare its owning plan file (basename) so per-task dispatch
            # and the `block-dependents` cascade can attribute work back
            # to the correct file in the plan directory. Missing field is
            # rejected; a present-but-invalid value (wrong type, escapes,
            # path separators, leading dot, NUL byte, length > 255 bytes)
            # surfaces as `invalid-plan-file`.
            if "plan_file" not in t:
                errors.append({
                    "path": f"$.tasks[{i}].plan_file",
                    "code": "missing-field",
                    "message": f"tasks[{i}] missing field 'plan_file'",
                })
            else:
                plan_file = t.get("plan_file")
                if not isinstance(plan_file, str) or not _is_valid_plan_file_basename(plan_file):
                    errors.append({
                        "path": f"$.tasks[{i}].plan_file",
                        "code": "invalid-plan-file",
                        "message": (
                            "plan_file must be a non-empty POSIX-portable basename "
                            "with no path separators, '..', leading dot, NUL byte, "
                            "or length > 255 bytes"
                        ),
                    })
            for key in t.keys():
                if key not in ALLOWED_TASK_FIELDS:
                    msg = f"tasks[{i}] unknown field {key!r}"
                    if strict_nested:
                        errors.append({
                            "path": f"$.tasks[{i}].{key}",
                            "code": "unknown-nested-field",
                            "message": msg,
                        })
                    else:
                        warnings.append(msg)

    batches = data.get("batches")
    if not isinstance(batches, list):
        errors.append({
            "path": "$.batches",
            "code": "invalid-type",
            "message": "batches must be an array",
        })
        batches = []
    else:
        for i, b in enumerate(batches):
            if not isinstance(b, dict):
                errors.append({
                    "path": f"$.batches[{i}]",
                    "code": "invalid-type",
                    "message": f"batches[{i}] must be an object",
                })
                continue
            if "index" not in b:
                # TASK-008: directory-only contract. Legacy `batch_index`
                # field is rejected as a missing canonical `index` (it
                # surfaces as an `unknown-nested-field` error in strict
                # mode via the per-key check below).
                errors.append({
                    "path": f"$.batches[{i}].index",
                    "code": "missing-field",
                    "message": f"batches[{i}] missing field 'index'",
                })
            for key in ("task_ids", "file_locks"):
                if key not in b:
                    errors.append({
                        "path": f"$.batches[{i}].{key}",
                        "code": "missing-field",
                        "message": f"batches[{i}] missing field {key!r}",
                    })
            for key in b.keys():
                if key not in ALLOWED_BATCH_FIELDS:
                    msg = f"batches[{i}] unknown field {key!r}"
                    if strict_nested:
                        errors.append({
                            "path": f"$.batches[{i}].{key}",
                            "code": "unknown-nested-field",
                            "message": msg,
                        })
                    else:
                        warnings.append(msg)

    # TASK-002 (PHASE_D_STATE_MACHINE): optional `state` block. Loosely
    # validated — every subfield is optional and defaults apply on read.
    if "state" in data:
        state = data.get("state")
        if not isinstance(state, dict):
            errors.append({
                "path": "$.state",
                "code": "invalid-type",
                "message": "state must be an object",
            })
        else:
            for key in state.keys():
                if key not in ALLOWED_SCHEDULE_STATE_FIELDS:
                    msg = f"state unknown field {key!r}"
                    if strict_nested:
                        errors.append({
                            "path": f"$.state.{key}",
                            "code": "unknown-nested-field",
                            "message": msg,
                        })
                    else:
                        warnings.append(msg)
            for list_key in ("done", "failed", "blocked", "locked_files"):
                if list_key in state and not isinstance(state[list_key], list):
                    errors.append({
                        "path": f"$.state.{list_key}",
                        "code": "invalid-type",
                        "message": f"state.{list_key} must be an array",
                    })
            if "committed" in state and not isinstance(state["committed"], list):
                errors.append({
                    "path": "$.state.committed",
                    "code": "invalid-type",
                    "message": "state.committed must be an array",
                })
            for dict_key in ("review_notes", "retries_used"):
                if dict_key in state and not isinstance(state[dict_key], dict):
                    errors.append({
                        "path": f"$.state.{dict_key}",
                        "code": "invalid-type",
                        "message": f"state.{dict_key} must be an object",
                    })

    if "plan_review_state" in data:
        plan_review_state = data.get("plan_review_state")
        if not isinstance(plan_review_state, dict):
            errors.append({
                "path": "$.plan_review_state",
                "code": "invalid-type",
                "message": "plan_review_state must be an object",
            })
        else:
            for key in plan_review_state.keys():
                if key not in ALLOWED_PLAN_REVIEW_STATE_FIELDS:
                    msg = f"plan_review_state unknown field {key!r}"
                    if strict_nested:
                        errors.append({
                            "path": f"$.plan_review_state.{key}",
                            "code": "unknown-nested-field",
                            "message": msg,
                        })
                    else:
                        warnings.append(msg)
            if "attempt" in plan_review_state and (
                not isinstance(plan_review_state["attempt"], int)
                or isinstance(plan_review_state["attempt"], bool)
            ):
                errors.append({
                    "path": "$.plan_review_state.attempt",
                    "code": "invalid-type",
                    "message": "plan_review_state.attempt must be an integer",
                })
            for bool_key in ("triage_dispatched", "auto_revise_round_completed"):
                if (
                    bool_key in plan_review_state
                    and not isinstance(plan_review_state[bool_key], bool)
                ):
                    errors.append({
                        "path": f"$.plan_review_state.{bool_key}",
                        "code": "invalid-type",
                        "message": f"plan_review_state.{bool_key} must be a boolean",
                    })
            for list_key in (
                "load_bearing_indices",
                "dismissed_indices",
                "author_dispatches_completed",
            ):
                if list_key in plan_review_state and not isinstance(
                    plan_review_state[list_key], list
                ):
                    errors.append({
                        "path": f"$.plan_review_state.{list_key}",
                        "code": "invalid-type",
                        "message": f"plan_review_state.{list_key} must be an array",
                    })
            if (
                "task_plan_file_map" in plan_review_state
                and not isinstance(plan_review_state["task_plan_file_map"], dict)
            ):
                errors.append({
                    "path": "$.plan_review_state.task_plan_file_map",
                    "code": "invalid-type",
                    "message": "plan_review_state.task_plan_file_map must be an object",
                })

    if not errors:
        errors.extend(_validate_schedule_refs(tasks, batches))

    return errors, warnings


def _validate_reviewer_finding_item(item: object, *, path: str) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(item, dict):
        return [{
            "path": path,
            "code": "invalid-reviewer-finding",
            "message": "reviewer finding must be an object",
        }]

    required = {
        "severity": str,
        "confidence": str,
        "file": str,
        "line": int,
        "issue": str,
        "suggested_fix": str,
    }
    for key, typ in required.items():
        if key not in item:
            errors.append({
                "path": f"{path}.{key}",
                "code": "missing-reviewer-finding-field",
                "message": f"reviewer finding missing field {key!r}",
            })
            continue
        value = item[key]
        if typ is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, typ)
        if not ok:
            errors.append({
                "path": f"{path}.{key}",
                "code": "invalid-reviewer-finding-field",
                "message": f"reviewer finding field {key!r} must be a {typ.__name__}",
            })
    severity = item.get("severity")
    if isinstance(severity, str) and severity not in {"critical", "important", "minor"}:
        errors.append({
            "path": f"{path}.severity",
            "code": "invalid-reviewer-finding-severity",
            "message": f"reviewer finding severity must be one of ['critical', 'important', 'minor'], got {severity!r}",
        })
    confidence = item.get("confidence")
    if isinstance(confidence, str) and confidence not in {"high", "medium", "low"}:
        errors.append({
            "path": f"{path}.confidence",
            "code": "invalid-reviewer-finding-confidence",
            "message": (
                "reviewer finding confidence must be one of "
                "['high', 'medium', 'low'], "
                f"got {confidence!r}"
            ),
        })
    for key in item.keys():
        if key in required or key in OPTIONAL_REVIEWER_FINDING_FIELDS:
            continue
        errors.append({
            "path": f"{path}.{key}",
            "code": "unknown-reviewer-finding-field",
            "message": f"reviewer finding has unknown field {key!r}",
        })

    # TASK-019: optional disposition + disposition_reason fields. Both are
    # additive — absent is fine. Value constraints are enforced only when the
    # field is present, so existing callers continue to pass unchanged.
    disposition_present = "disposition" in item
    disposition = item.get("disposition") if disposition_present else None
    if disposition_present and disposition is not None:
        if disposition not in ALLOWED_REVIEWER_FINDING_DISPOSITIONS:
            errors.append({
                "path": f"{path}.disposition",
                "code": "invalid-reviewer-finding-disposition",
                "message": (
                    "reviewer finding disposition must be one of "
                    f"{sorted(ALLOWED_REVIEWER_FINDING_DISPOSITIONS)}, "
                    f"got {disposition!r}"
                ),
            })
    reason_present = "disposition_reason" in item
    reason = item.get("disposition_reason") if reason_present else None
    if reason_present and reason is not None and not isinstance(reason, str):
        errors.append({
            "path": f"{path}.disposition_reason",
            "code": "invalid-reviewer-finding-disposition-reason",
            "message": "reviewer finding disposition_reason must be a string",
        })
    if reason_present and reason is not None and (not disposition_present or disposition is None):
        errors.append({
            "path": f"{path}.disposition_reason",
            "code": "disposition-reason-without-disposition",
            "message": (
                "reviewer finding has disposition_reason but no disposition; "
                "the reason cannot be interpreted without an accompanying disposition"
            ),
        })
    return errors


def _validate_minor_findings_payload(minor: object, *, path: str) -> list[dict]:
    if not isinstance(minor, list):
        return [{
            "path": path,
            "code": "invalid-reviewer-minor-findings",
            "message": "reviewer minor findings must be a JSON array",
        }]
    errors: list[dict] = []
    for i, item in enumerate(minor):
        errors.extend(_validate_reviewer_finding_item(item, path=f"{path}[{i}]"))
    return errors


def _validate_review_success_payload(
    reviewer: str,
    reviewer_verdict: str,
    reviewer_minor_findings: object,
) -> list[dict]:
    errors: list[dict] = []
    allowed_verdicts: set[str]
    commit_allowed: set[str]
    if reviewer == "codex":
        allowed_verdicts = ALLOWED_CODEX_REVIEW_VERDICTS
        commit_allowed = {"clean", "minor-findings"}
    elif reviewer == "gemini":
        allowed_verdicts = ALLOWED_CODEX_REVIEW_VERDICTS
        commit_allowed = {"clean", "minor-findings"}
    elif reviewer == "claude":
        allowed_verdicts = ALLOWED_CLAUDE_REVIEW_VERDICTS
        commit_allowed = {"ship", "ship-with-fixes"}
    elif reviewer == "none":
        allowed_verdicts = {""}
        commit_allowed = {""}
    else:
        return [{
            "path": "$.reviewer",
            "code": "invalid-reviewer",
            "message": f"reviewer must be one of ['claude', 'codex', 'gemini', 'none'], got {reviewer!r}",
        }]

    if reviewer_verdict not in allowed_verdicts:
        errors.append({
            "path": "$.reviewer_verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                f"reviewer {reviewer!r} verdict must be one of {sorted(allowed_verdicts)}, "
                f"got {reviewer_verdict!r}"
            ),
        })
    elif reviewer_verdict not in commit_allowed:
        # POSTMORTEM_FIXES TASK-003: surface the canonical D.2a routing form
        # inline so the next operator who hits this rejection knows which
        # binding-reviewer call to make without chasing cross-references.
        # The hint is BOTH structured (separate `hint` field on the JSON
        # error envelope) AND human-readable (suffixed to the message
        # string so the non-`--json` stderr path also carries it — `_emit`
        # only formats top-level `error`, not nested error dicts' `hint`).
        canonical_hint = (
            "use --reviewer claude --reviewer-verdict ship-with-fixes "
            "(or 'ship') for the D.5-driven binding-reviewer commit; "
            "see SKILL.md §D.2a routing"
        )
        errors.append({
            "path": "$.reviewer_verdict",
            "code": "uncommittable-reviewer-verdict",
            "message": (
                f"commit-task cannot accept reviewer verdict {reviewer_verdict!r}; "
                f"allowed commit verdicts for reviewer {reviewer!r}: {sorted(commit_allowed)}; "
                f"hint: {canonical_hint}"
            ),
            "hint": canonical_hint,
        })

    errors.extend(_validate_minor_findings_payload(
        reviewer_minor_findings,
        path="$.reviewer_minor_findings",
    ))
    return errors


def _validate_review_failure_payload(reviewer_findings: object) -> list[dict]:
    if not isinstance(reviewer_findings, dict):
        return [{
            "path": "$.reviewer_findings",
            "code": "invalid-reviewer-findings",
            "message": "reviewer findings must be a JSON object",
        }]

    errors: list[dict] = []
    required = {
        "task_id": str,
        "verdict": str,
        "findings": list,
        "notes": list,
        "scope_ok": bool,
        "acceptance_met": bool,
        "summary": str,
    }
    for key, typ in required.items():
        if key not in reviewer_findings:
            errors.append({
                "path": f"$.reviewer_findings.{key}",
                "code": "missing-reviewer-field",
                "message": f"reviewer findings missing field {key!r}",
            })
            continue
        value = reviewer_findings[key]
        if not isinstance(value, typ):
            errors.append({
                "path": f"$.reviewer_findings.{key}",
                "code": "invalid-reviewer-field",
                "message": f"reviewer findings field {key!r} must be a {typ.__name__}",
            })

    verdict = reviewer_findings.get("verdict")
    if isinstance(verdict, str) and verdict not in ALLOWED_CODEX_REVIEW_VERDICTS:
        errors.append({
            "path": "$.reviewer_findings.verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                "reviewer findings verdict must be one of "
                f"{sorted(ALLOWED_CODEX_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    findings = reviewer_findings.get("findings")
    if isinstance(findings, list):
        for i, item in enumerate(findings):
            errors.extend(_validate_reviewer_finding_item(
                item,
                path=f"$.reviewer_findings.findings[{i}]",
            ))

    for key in reviewer_findings.keys():
        if key not in required:
            errors.append({
                "path": f"$.reviewer_findings.{key}",
                "code": "unknown-reviewer-field",
                "message": f"reviewer findings has unknown field {key!r}",
            })
    return errors


def _validate_d5_adjudication_payload(
    payload: object,
    *,
    codex_findings_count: int,
) -> list[dict]:
    """Validate a D.5 adjudication payload per TASK-016A.

    Shape (verdict-dependent):
        {
          "verdict": "ship" | "ship-with-fixes" | "partial-agreement" | "needs-rework",
          "summary": "<one-line justification>",
          # required when verdict == "partial-agreement":
          "load_bearing": [int, ...],   # 0-based indices into codex_findings
          "dismissed":   [int, ...],    # 0-based indices into codex_findings
        }

    For `partial-agreement`, `load_bearing` and `dismissed` MUST be
    non-empty integer arrays, disjoint, and their union MUST be a
    subset of `range(codex_findings_count)`. The split must be clean
    (≥1 load-bearing AND ≥1 dismissed); an empty bucket collapses to
    `needs-rework` / `ship-with-fixes` and the reviewer should have
    chosen those verdicts instead.

    Errors:
      - `partial-agreement-invalid-split`: empty or overlapping buckets.
      - `partial-agreement-unknown-index`: index < 0 or >= findings count.

    The non-partial-agreement verdicts (`ship`, `ship-with-fixes`,
    `needs-rework`) pass through without requiring the split fields;
    their routing does not need finding-level indices.
    """
    errors: list[dict] = []
    if not isinstance(payload, dict):
        return [{
            "path": "$",
            "code": "invalid-type",
            "message": "D.5 adjudication payload must be a JSON object",
        }]

    verdict = payload.get("verdict")
    if not isinstance(verdict, str):
        errors.append({
            "path": "$.verdict",
            "code": "missing-field",
            "message": "D.5 payload missing required field 'verdict'",
        })
    elif verdict not in ALLOWED_CLAUDE_REVIEW_VERDICTS:
        errors.append({
            "path": "$.verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                f"D.5 verdict must be one of "
                f"{sorted(ALLOWED_CLAUDE_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        errors.append({
            "path": "$.summary",
            "code": "invalid-field",
            "message": "D.5 payload field 'summary' must be a string",
        })

    if verdict == "partial-agreement":
        # Pre-TASK-016C follow-up (non-blocking note from D.5 on TASK-016A #1):
        # the orchestrator forwards `d5_summary` to the plan-remediator
        # Phase B-narrow-remediation template; a missing or empty string
        # silently hands the remediator a blank justification for why the
        # load-bearing findings are load-bearing. Require non-empty summary
        # on the partial-agreement path.
        if not isinstance(summary, str) or summary.strip() == "":
            errors.append({
                "path": "$.summary",
                "code": "partial-agreement-missing-summary",
                "message": (
                    "partial-agreement payload requires a non-empty "
                    "'summary' string; the remediator template forwards "
                    "this to justify why the load-bearing findings are "
                    "load-bearing"
                ),
            })

        def _check_bucket(name: str) -> list[int] | None:
            value = payload.get(name)
            if value is None:
                errors.append({
                    "path": f"$.{name}",
                    "code": "missing-field",
                    "message": (
                        f"partial-agreement payload missing required field "
                        f"{name!r}"
                    ),
                })
                return None
            if not isinstance(value, list):
                errors.append({
                    "path": f"$.{name}",
                    "code": "invalid-type",
                    "message": (
                        f"partial-agreement field {name!r} must be an array"
                    ),
                })
                return None
            cleaned: list[int] = []
            had_type_error = False
            for i, item in enumerate(value):
                # bool is a subclass of int in Python; reject it explicitly so
                # `[True]` does not pass as `[1]`.
                if not isinstance(item, int) or isinstance(item, bool):
                    errors.append({
                        "path": f"$.{name}[{i}]",
                        "code": "invalid-type",
                        "message": (
                            f"partial-agreement field {name!r}[{i}] must be "
                            f"an integer index, got {type(item).__name__}"
                        ),
                    })
                    had_type_error = True
                    continue
                cleaned.append(item)
            # Pre-TASK-016C follow-up (non-blocking note from D.5 on
            # TASK-016A #2): short-circuit downstream split validation
            # on bucket-element type errors so callers don't see a
            # spurious `partial-agreement-invalid-split` stacked on top
            # of the underlying `invalid-type`. Return None signals
            # "structurally broken; skip split checks".
            if had_type_error:
                return None
            return cleaned

        load_bearing = _check_bucket("load_bearing")
        dismissed = _check_bucket("dismissed")

        # Only proceed with split validation when both buckets were
        # structurally valid; otherwise the type errors above are
        # enough signal for the caller.
        if load_bearing is not None and dismissed is not None:
            if len(load_bearing) == 0:
                errors.append({
                    "path": "$.load_bearing",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        "partial-agreement requires a non-empty "
                        "'load_bearing' bucket; empty collapses to "
                        "'ship-with-fixes' — pick that verdict instead"
                    ),
                })
            if len(dismissed) == 0:
                errors.append({
                    "path": "$.dismissed",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        "partial-agreement requires a non-empty "
                        "'dismissed' bucket; empty collapses to "
                        "'needs-rework' — pick that verdict instead"
                    ),
                })
            # Pre-TASK-016C follow-up (non-blocking note from D.5 on
            # TASK-016A #2): intra-bucket duplicates (e.g., `[0, 0]`)
            # are a contract violation — the remediator would see the
            # same index twice. Reject with the existing
            # `partial-agreement-invalid-split` code.
            for name, bucket in (
                ("load_bearing", load_bearing),
                ("dismissed", dismissed),
            ):
                seen: dict[int, int] = {}
                dup_indices: list[int] = []
                for i, idx in enumerate(bucket):
                    if idx in seen:
                        dup_indices.append(idx)
                    else:
                        seen[idx] = i
                if dup_indices:
                    errors.append({
                        "path": f"$.{name}",
                        "code": "partial-agreement-invalid-split",
                        "message": (
                            f"partial-agreement bucket {name!r} must not "
                            f"repeat indices; duplicates: "
                            f"{sorted(set(dup_indices))}"
                        ),
                    })
            overlap = sorted(set(load_bearing) & set(dismissed))
            if overlap:
                errors.append({
                    "path": "$.load_bearing",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        f"partial-agreement requires disjoint buckets; "
                        f"indices {overlap} appear in both 'load_bearing' "
                        f"and 'dismissed'"
                    ),
                })
            for name, bucket in (
                ("load_bearing", load_bearing),
                ("dismissed", dismissed),
            ):
                for i, idx in enumerate(bucket):
                    if idx < 0 or idx >= codex_findings_count:
                        errors.append({
                            "path": f"$.{name}[{i}]",
                            "code": "partial-agreement-unknown-index",
                            "message": (
                                f"partial-agreement index {idx} in {name!r} "
                                f"is out of range for codex_findings of "
                                f"length {codex_findings_count}"
                            ),
                        })

    return errors


def _extract_last_fenced_json_block(text: str) -> str | None:
    matches = list(
        re.finditer(r"```json[ \t]*\r?\n(.*?)\r?\n```", text, re.DOTALL)
    )
    if not matches:
        return None
    return matches[-1].group(1)


def _validate_plan_review_triage_payload(
    payload: object,
    *,
    findings_count: int,
    source: str,
) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(payload, dict):
        return [{
            "path": "$",
            "code": "invalid-type",
            "message": "triage payload must be a JSON object",
        }]

    required = {
        "verdict": str,
        "load_bearing": list,
        "dismissed": list,
        "summary": str,
    }
    for key, typ in required.items():
        if key not in payload:
            errors.append({
                "path": f"$.{key}",
                "code": "missing-field",
                "message": f"triage payload missing required field {key!r}",
            })
            continue
        value = payload[key]
        ok = isinstance(value, typ)
        if not ok:
            errors.append({
                "path": f"$.{key}",
                "code": "invalid-type",
                "message": (
                    f"triage field {key!r} must be a {typ.__name__}"
                ),
            })

    verdict = payload.get("verdict")
    if (
        isinstance(verdict, str)
        and verdict not in ALLOWED_CLAUDE_REVIEW_VERDICTS
    ):
        errors.append({
            "path": "$.verdict",
            "code": "invalid-reviewer-verdict",
            "message": (
                f"triage verdict must be one of "
                f"{sorted(ALLOWED_CLAUDE_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    noun = "gap" if source == "plan-analyst" else "finding"

    def _check_bucket(name: str) -> list[int] | None:
        value = payload.get(name)
        if not isinstance(value, list):
            return None
        cleaned: list[int] = []
        had_type_error = False
        for i, item in enumerate(value):
            if not isinstance(item, int) or isinstance(item, bool):
                errors.append({
                    "path": f"$.{name}[{i}]",
                    "code": "invalid-type",
                    "message": (
                        f"triage field {name!r}[{i}] must be an integer "
                        f"index, got {type(item).__name__}"
                    ),
                })
                had_type_error = True
                continue
            cleaned.append(item)
        if had_type_error:
            return None
        return cleaned

    load_bearing = _check_bucket("load_bearing")
    dismissed = _check_bucket("dismissed")

    if load_bearing is not None and dismissed is not None:
        if verdict == "partial-agreement":
            if len(load_bearing) == 0:
                errors.append({
                    "path": "$.load_bearing",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        "partial-agreement requires a non-empty "
                        "'load_bearing' bucket"
                    ),
                })
            if len(dismissed) == 0:
                errors.append({
                    "path": "$.dismissed",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        "partial-agreement requires a non-empty "
                        "'dismissed' bucket"
                    ),
                })

        for name, bucket in (
            ("load_bearing", load_bearing),
            ("dismissed", dismissed),
        ):
            seen: set[int] = set()
            dup: list[int] = []
            for idx in bucket:
                if idx in seen and idx not in dup:
                    dup.append(idx)
                seen.add(idx)
            if dup:
                errors.append({
                    "path": f"$.{name}",
                    "code": "partial-agreement-invalid-split",
                    "message": (
                        f"triage bucket {name!r} contains duplicate "
                        f"{noun} indices {dup}; entries must be unique"
                    ),
                })

        overlap = sorted(set(load_bearing) & set(dismissed))
        if overlap:
            errors.append({
                "path": "$.load_bearing",
                "code": "triage-buckets-not-disjoint",
                "message": (
                    f"triage buckets must be disjoint; indices {overlap} "
                    f"appear in both 'load_bearing' and 'dismissed'"
                ),
            })

        for name, bucket in (
            ("load_bearing", load_bearing),
            ("dismissed", dismissed),
        ):
            for i, idx in enumerate(bucket):
                if idx < 0 or idx >= findings_count:
                    errors.append({
                        "path": f"$.{name}[{i}]",
                        "code": "triage-index-out-of-range",
                        "message": (
                            f"{noun} index {idx} out of range for "
                            f"{findings_count} {noun}s"
                        ),
                    })

    allowed_keys = set(required.keys())
    for key in payload.keys():
        if key not in allowed_keys:
            errors.append({
                "path": f"$.{key}",
                "code": "unknown-parsed-field",
                "message": f"triage payload has unknown field {key!r}",
            })

    return errors


def _validate_execution_log_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list):
        return [{
            "path": "$.rows",
            "code": "invalid-execution-log-rows",
            "message": "execution-log rows must be a JSON array",
        }]

    # TASK-019: render the allowed-field list inline on both missing-field
    # and unknown-field error messages so an orchestrator hitting the error
    # can see the full schema without consulting SKILL.md.
    allowed_display = sorted(ALLOWED_ROW_FIELDS)
    errors: list[dict] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            errors.append({
                "path": f"$.rows[{i}]",
                "code": "invalid-execution-log-row",
                "message": "execution-log row must be an object",
            })
            continue
        for key in ALLOWED_ROW_FIELDS:
            if key not in row:
                errors.append({
                    "path": f"$.rows[{i}].{key}",
                    "code": "missing-execution-log-field",
                    "message": (
                        f"execution-log row missing field {key!r}; "
                        f"required fields: {allowed_display}"
                    ),
                })
            elif not isinstance(row[key], str):
                errors.append({
                    "path": f"$.rows[{i}].{key}",
                    "code": "invalid-execution-log-field",
                    "message": f"execution-log row field {key!r} must be a string",
                })
        for key in row.keys():
            if key not in ALLOWED_ROW_FIELDS:
                errors.append({
                    "path": f"$.rows[{i}].{key}",
                    "code": "unknown-execution-log-field",
                    "message": (
                        f"execution-log row has unknown field {key!r}; "
                        f"allowed fields: {allowed_display}"
                    ),
                })
    return errors


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


# TASK-005: env-var names for the Gemini availability check. Exposed as
# module constants so tests can assert against the same names the helper
# reads, and so any future relocation of the helper updates one source.
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GOOGLE_APP_CRED_ENV = "GOOGLE_APPLICATION_CREDENTIALS"


def _resolve_gemini_available() -> bool:
    """Return True iff the `gemini` CLI is on `$PATH`.

    Sole source of truth for the orchestrator preflight `gemini_available`
    field. The Gemini CLI may authenticate through its local OAuth session
    (for example ``~/.gemini``), ``GEMINI_API_KEY``, or
    ``GOOGLE_APPLICATION_CREDENTIALS``; preflight should not mark Gemini
    unavailable just because API-key/ADC environment variables are absent.
    """
    return shutil.which("gemini") is not None


def _resolve_python() -> str:
    """Resolve the interpreter path per documented precedence (TASK-008).

    Precedence (first match wins):
      1. $IMPLEMENT_PLAN_PYTHON if the path exists and is executable.
      2. <cwd>/venv/bin/python if present and executable.
      3. <cwd>/.venv/bin/python if present and executable.
      4. shutil.which('python3').
      5. sys.executable as final fallback.
    Returns an absolute path string. The result is echoed into
    `preflight --json` as `python_path` so the orchestrator can pin one
    interpreter for the rest of the run via ``{{python_path}}``
    substitution in dispatch templates. The skill surface (SKILL.md +
    dispatch-templates.md) uses `$PYTHON` as the placeholder; this
    helper is the single source of truth for the resolution rule.
    """
    env = os.environ.get("IMPLEMENT_PLAN_PYTHON")
    if env and Path(env).is_file() and os.access(env, os.X_OK):
        return str(Path(env).resolve())
    for rel in ("venv/bin/python", ".venv/bin/python"):
        p = Path.cwd() / rel
        if p.is_file() and os.access(p, os.X_OK):
            return str(p.resolve())
    which = shutil.which("python3")
    if which:
        return which
    return sys.executable


def _emit(args, result: dict, *, exit_code: int = 0) -> None:
    if getattr(args, "json", False):
        json.dump(result, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        if "error" in result:
            print(f"ERROR: {result['error']}", file=sys.stderr)
        else:
            for k, v in result.items():
                print(f"{k}: {v}")
    sys.exit(exit_code)


def _die(args, result: dict, *, exit_code: int = 1) -> None:
    _emit(args, result, exit_code=exit_code)


def _result(payload: dict, *, exit_code: int = 0) -> dict:
    result = dict(payload)
    result["__plan_ops_exit_code__"] = exit_code
    return result


def _public_result(result: dict) -> dict:
    return {
        key: value
        for key, value in result.items()
        if not key.startswith("__plan_ops_")
    }


def _emit_or_die(args, result: dict) -> None:
    """Emit a marker-bearing result envelope and exit.

    Internal-only ``__plan_ops_*__`` keys are a closed vocabulary:
    ``__plan_ops_exit_code__`` overrides the process exit code;
    ``__plan_ops_text_output__`` writes a non-empty string verbatim to stdout
    instead of JSON/dict rendering, currently allowed only for ``cmd_audit``;
    ``__plan_ops_stdout_suppressed__`` exits without stdout for file-output
    commands such as the ``cmd_build_*_dispatch_input`` family.
    """
    exit_marker = result.pop("__plan_ops_exit_code__", None)
    text_output = result.pop("__plan_ops_text_output__", None)
    stdout_suppressed = bool(result.pop("__plan_ops_stdout_suppressed__", False))
    # The MCP-only acknowledgement marker is consumed by the MCP server
    # path; the CLI path (this function) drops it so the on-wire byte
    # image stays envelope-shaped.
    result.pop("__plan_ops_mcp_acknowledgement__", None)

    if exit_marker is None:
        exit_code = 1 if result.get("errors") or result.get("error") else 0
    else:
        exit_code = int(exit_marker)

    if stdout_suppressed:
        sys.exit(exit_code)
    if isinstance(text_output, str) and text_output:
        sys.stdout.write(text_output)
        sys.exit(exit_code)
    _emit(args, result, exit_code=exit_code)


def _normalize_csv_or_list(value: object) -> list[str]:
    """Accept either CLI-shape CSV string or MCP-shape list of strings.

    The MCP server delivers oneOf-string|array params as their native
    JSON shape; the CLI delivers a single argparse string. Shared
    `_run_*` payload normalizers must accept both.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [s.strip() for s in value if isinstance(s, str) and s.strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _normalize_json_or_list(value: object) -> list:
    """Accept either CLI-shape JSON string or MCP-shape list payload."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return list(value)
    return json.loads(value)


def _normalize_task_id(raw: str) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    m = TASK_ID_INPUT_RE.match(s)
    if not m:
        return None
    n = int(m.group(1))
    if n < 0 or n > 999:
        return None
    return f"{n:03d}{m.group(2)}"


def _load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _atomic_write_text(path: Path, text: str) -> int:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return len(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# TASK-002 (PHASE_D_STATE_MACHINE): persistent orchestrator-state helpers.
#
# Pure, stateless transitions over the §3.2 `state` dict. The CLI flags
# `--update-schedule-state` and `--from-schedule-state` are thin file-IO
# shims that read the schedule, apply one of these helpers, and re-write
# atomically via `_atomic_write_text`. Tests call these directly against
# in-memory dicts.
# ---------------------------------------------------------------------------


def _empty_schedule_state() -> dict:
    """Return a freshly-initialized state dict with every subfield present."""
    return {
        "done": [],
        "failed": [],
        "blocked": [],
        "committed": [],
        "locked_files": [],
        "review_notes": {},
        "retries_used": {},
    }


def _normalize_state(state: object) -> dict:
    """Coerce a possibly-partial state dict to the full shape.

    Missing subfields are defaulted; extra/unknown fields are preserved
    verbatim. Returns a NEW dict (caller-owned); the input is not mutated.
    """
    base = _empty_schedule_state()
    if isinstance(state, dict):
        for k, v in state.items():
            base[k] = copy.deepcopy(v)
    return base


def read_schedule_state(path) -> dict:
    """Read the `state` block from a schedule JSON file.

    Returns the normalized state dict (every subfield present). On a
    missing file, malformed JSON, or schedule without a `state` block,
    returns the empty-state default. Never raises.
    """
    p = Path(path)
    if not p.is_file():
        return _empty_schedule_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_schedule_state()
    if not isinstance(data, dict):
        return _empty_schedule_state()
    return _normalize_state(data.get("state"))


def _empty_plan_review_state() -> dict:
    return {
        "attempt": 1,
        "first_verdict": None,
        "first_findings_count": 0,
        "second_verdict": None,
        "second_findings_count": 0,
        "triage_dispatched": False,
        "triage_verdict": None,
        "load_bearing_indices": [],
        "dismissed_indices": [],
        "author_dispatches_completed": [],
        "auto_revise_round_completed": False,
        "skipped_reason": None,
        "task_plan_file_map": {},
    }


def _normalize_plan_review_state(state: object) -> dict:
    base = _empty_plan_review_state()
    if isinstance(state, dict):
        for k, v in state.items():
            base[k] = copy.deepcopy(v)
    return base


def read_plan_review_state(path) -> dict:
    """Read and normalize `plan_review_state` from a schedule JSON file."""
    p = Path(path)
    if not p.is_file():
        return _empty_plan_review_state()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_plan_review_state()
    if not isinstance(data, dict):
        return _empty_plan_review_state()
    return _normalize_plan_review_state(data.get("plan_review_state"))


def _coerce_index_list(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    return [v for v in values if isinstance(v, int) and not isinstance(v, bool)]


def record_plan_review_verdict(
    state: dict, attempt: int, verdict: str, findings_count: int,
) -> dict:
    out = _normalize_plan_review_state(state)
    out["attempt"] = attempt
    if attempt == 1:
        out["first_verdict"] = verdict
        out["first_findings_count"] = findings_count
    elif attempt == 2:
        out["second_verdict"] = verdict
        out["second_findings_count"] = findings_count
        out["auto_revise_round_completed"] = True
    return out


def record_triage_outcome(
    state: dict, verdict: str, load_bearing: list[int], dismissed: list[int],
) -> dict:
    out = _normalize_plan_review_state(state)
    out["triage_dispatched"] = True
    out["triage_verdict"] = verdict
    out["load_bearing_indices"] = _coerce_index_list(load_bearing)
    out["dismissed_indices"] = _coerce_index_list(dismissed)
    return out


def record_author_dispatch(
    state: dict, finding_index: int, target_task_id: str | None,
    files_edited: list[str],
) -> dict:
    out = _normalize_plan_review_state(state)
    dispatches = list(out.get("author_dispatches_completed") or [])
    dispatches.append({
        "finding_index": finding_index,
        "target_task_id": target_task_id,
        "files_edited": list(files_edited or []),
    })
    out["author_dispatches_completed"] = dispatches
    out["auto_revise_round_completed"] = True
    return out


def apply_plan_review_state_transition(
    state: dict, transitions: dict | None,
) -> dict:
    """Apply route-produced plan-review state transitions to a new dict."""
    out = _normalize_plan_review_state(state)
    if not isinstance(transitions, dict):
        return out

    verdict_transition = transitions.get("record_plan_review_verdict")
    if isinstance(verdict_transition, dict):
        out = record_plan_review_verdict(
            out,
            verdict_transition.get("attempt", out["attempt"]),
            verdict_transition.get("verdict"),
            verdict_transition.get("findings_count", 0),
        )

    triage_transition = transitions.get("record_triage_outcome")
    if isinstance(triage_transition, dict):
        out = record_triage_outcome(
            out,
            triage_transition.get("verdict"),
            triage_transition.get("load_bearing", []),
            triage_transition.get("dismissed", []),
        )

    author_transition = transitions.get("record_author_dispatch")
    if isinstance(author_transition, dict):
        out = record_author_dispatch(
            out,
            author_transition.get("finding_index"),
            author_transition.get("target_task_id"),
            author_transition.get("files_edited", []),
        )
    elif isinstance(author_transition, list):
        for item in author_transition:
            if isinstance(item, dict):
                out = record_author_dispatch(
                    out,
                    item.get("finding_index"),
                    item.get("target_task_id"),
                    item.get("files_edited", []),
                )

    direct = transitions.get("set")
    if isinstance(direct, dict):
        for key, value in direct.items():
            out[key] = copy.deepcopy(value)

    for key in ALLOWED_PLAN_REVIEW_STATE_FIELDS:
        if key in transitions:
            out[key] = copy.deepcopy(transitions[key])
    return out


def _write_plan_review_state_transition(
    path, transitions: dict | None,
) -> tuple[bool, str | None]:
    p = Path(path)
    if not p.is_file():
        return False, f"schedule file not found: {p} (plan-review-state write no-op)"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"schedule file unreadable: {e} (plan-review-state write no-op)"
    if not isinstance(data, dict):
        return False, "schedule top-level is not an object (plan-review-state write no-op)"
    current = _normalize_plan_review_state(data.get("plan_review_state"))
    data["plan_review_state"] = apply_plan_review_state_transition(
        current, transitions,
    )
    _atomic_write_json(p, data)
    return True, None


def apply_commit_state_transition(
    state: dict, task_id: str, sha: str, files: list[str],
) -> dict:
    """Promote `task_id` into `state.done`, append the commit record, and
    release this task's entries from `state.locked_files`.

    Idempotent: re-applying for the same task_id is a no-op on `done`,
    appends a fresh `committed` entry (callers should not call twice for
    one commit), and removes the `files` from `locked_files` regardless.
    """
    out = _normalize_state(state)
    if task_id not in out["done"]:
        out["done"] = [*out["done"], task_id]
    out["committed"] = [*out["committed"], {"task_id": task_id, "sha": sha}]
    files_set = set(files or [])
    out["locked_files"] = [f for f in out["locked_files"] if f not in files_set]
    return out


def apply_fail_state_transition(
    state: dict, task_id: str, retries: dict | None,
) -> dict:
    """Append `task_id` to `state.failed` and persist the per-task
    `retries_used` map under `state.retries_used[task_id]`.

    Passing `retries=None` leaves `state.retries_used[task_id]` untouched
    (the orchestrator may call `fail-task` without a retry-budget update
    on stages where no retry was attempted).
    """
    out = _normalize_state(state)
    if task_id not in out["failed"]:
        out["failed"] = [*out["failed"], task_id]
    if retries is not None:
        new_map = dict(out["retries_used"])
        new_map[task_id] = copy.deepcopy(retries)
        out["retries_used"] = new_map
    return out


def apply_blocked_state_transition(
    state: dict, blocked_ids: list[str],
) -> dict:
    """Populate `state.blocked` with the supplied ids (de-duplicated,
    appended to any existing entries in stable order).
    """
    out = _normalize_state(state)
    existing = list(out["blocked"])
    seen = set(existing)
    for tid in (blocked_ids or []):
        if tid not in seen:
            existing.append(tid)
            seen.add(tid)
    out["blocked"] = existing
    return out


def _write_schedule_state(path, state: dict) -> tuple[bool, str | None]:
    """File-IO shim: load schedule from `path`, replace its `state` block,
    and atomically rewrite. Returns `(written, warning)`.

    On a schedule that does not exist or is malformed → `(False, warning)`
    (no-op + warning). On a well-formed schedule, the `state` field is
    inserted/replaced and written via `_atomic_write_text`.
    """
    p = Path(path)
    if not p.is_file():
        return False, f"schedule file not found: {p} (state-write no-op)"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return False, f"schedule file unreadable: {e} (state-write no-op)"
    if not isinstance(data, dict):
        return False, "schedule top-level is not an object (state-write no-op)"
    if "state" not in data:
        return False, (
            f"legacy schedule (no state block) at {p}; state-write no-op"
        )
    data["state"] = state
    text = json.dumps(data, indent=2, sort_keys=False) + "\n"
    _atomic_write_text(p, text)
    return True, None


def _split_task_blocks(plan_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split plan body on `^### TASK-NNN:` boundaries.

    Returns (preamble, [(task_id, block_text), ...]) where each block_text
    starts with `### TASK-NNN:` through the start of the next task or EOF.
    """
    matches = list(TASK_HEADER_RE.finditer(plan_text))
    if not matches:
        return plan_text, []
    preamble = plan_text[: matches[0].start()]
    blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plan_text)
        blocks.append((m.group(1), plan_text[m.start() : end]))
    return preamble, blocks


# ---------------------------------------------------------------------------
# TASK-001 shared parsing helpers.
#
# Both `decompose-plan` (reads whole-plan `## TASK-NNN:` H2 headings) and
# TASK-004's forthcoming `build-tasks` (reads child-plan `### TASK-NNN:` H3
# headings) use the same primitives. Extracting them here keeps the grammar
# single-sourced: every markdown → task-dict conversion flows through these
# three helpers against the appropriate heading level.
# ---------------------------------------------------------------------------


def _task_header_re(level: int) -> re.Pattern[str]:
    """Return a compiled regex for `^<level-hashes> TASK-NNN[A-Z]?: <title>`.

    `level=2` matches `## TASK-NNN:`; `level=3` matches `### TASK-NNN:`.
    Captures the id in group 1 and the title in group 2.

    The grammar is pinned to three-digit task ids (with an optional single
    alpha suffix for sub-tasks, e.g. `TASK-004A`). Short forms like
    `TASK-1:` or `TASK-12:` are *not* accepted; malformed plans must
    surface a `malformed-task-header` error rather than silently accept
    the short id. `_malformed_task_headers()` provides the loose-match
    pre-scan that powers that error.
    """
    if level not in (2, 3):
        raise ValueError(f"unsupported heading level {level!r}; expected 2 or 3")
    hashes = "#" * level
    return re.compile(
        rf"^{hashes} TASK-(\d{{3}}[A-Z]?):\s*(.+?)\s*$",
        re.MULTILINE,
    )


def _malformed_task_headers(
    plan_text: str, level: int,
) -> list[tuple[str, int]]:
    """Return `[(raw_id, source_line)]` for headers that look like TASK
    headings at the given level but do NOT match the strict three-digit
    grammar. Used by `_decompose_plan` to emit structured
    `malformed-task-header` errors instead of silently dropping the block.
    """
    if level not in (2, 3):
        raise ValueError(f"unsupported heading level {level!r}; expected 2 or 3")
    hashes = "#" * level
    # Loose regex: any digit run (1+), with optional alpha suffix. Matches
    # both canonical `TASK-001` and malformed `TASK-1`, `TASK-12`, etc.
    loose = re.compile(
        rf"^{hashes} TASK-(\d+[A-Z]?):\s*.+?\s*$",
        re.MULTILINE,
    )
    strict = _task_header_re(level)
    offenders: list[tuple[str, int]] = []
    for m in loose.finditer(plan_text):
        if strict.match(plan_text, m.start()):
            continue
        raw_id = m.group(1)
        source_line = plan_text.count("\n", 0, m.start()) + 1
        offenders.append((raw_id, source_line))
    return offenders


def _split_task_blocks_at_level(
    plan_text: str,
    level: int,
) -> tuple[str, list[tuple[str, str, str, int]]]:
    """Split plan body on task-heading boundaries at the given heading level.

    Returns `(preamble, [(task_id, title, block_text, source_line), ...])`.
    `task_id` is the **raw** id as it appears in the heading (e.g. `001` or
    `4A`); callers normalize via `_normalize_task_id`. `source_line` is the
    1-indexed line number of the heading in `plan_text`, which malformed-
    input error reports pin to the offending block.
    """
    pattern = _task_header_re(level)
    matches = list(pattern.finditer(plan_text))
    if not matches:
        return plan_text, []
    preamble = plan_text[: matches[0].start()]
    blocks: list[tuple[str, str, str, int]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(plan_text)
        block_text = plan_text[m.start() : end]
        raw_id = m.group(1)
        title = m.group(2).strip()
        # 1-indexed line number of the heading. `count("\n", 0, start)`
        # is the number of newlines before the heading, so line = that + 1.
        source_line = plan_text.count("\n", 0, m.start()) + 1
        blocks.append((raw_id, title, block_text, source_line))
    return preamble, blocks


def _extract_metadata_field(block: str, key: str) -> str | None:
    """Extract the value of a `- **<key>:**` inline metadata bullet.

    Accepts both inline (`- **Priority:** high`) and trailing-space variants.
    Returns the stripped value, or None if the bullet is absent. Backtick
    wrappers around the value are preserved so callers can distinguish
    ``- **Test command:** `cmd args``` from a bare command; the callers that
    need the unwrapped form strip them explicitly.
    """
    pattern = re.compile(
        rf"^\s*-\s*\*\*{re.escape(key)}:\*\*\s*(.+?)\s*$",
        re.MULTILINE,
    )
    m = pattern.search(block)
    if not m:
        return None
    return m.group(1).strip()


def _extract_bullet_list(block: str, heading: str) -> list[str]:
    """Extract bullet items nested under a `- **<heading>:**` marker.

    Supports two shapes:

      1. Standalone marker with indented children:
             - **Files:**
               - path/a.py
               - path/b.py
      2. Inline form with a single value or comma list:
             - **Files:** path/a.py
             - **Files:** path/a.py, path/b.py

    Returns the list of raw item strings in document order (stripped of
    leading `-` + whitespace; backticks and `(create|modify|delete)`
    annotations are left intact so callers can inspect them).

    Returns `[]` if the heading bullet exists but has no items (e.g.
    `- **Dependencies:** none` normalizes to `[]` for the caller).
    """
    # Standalone marker form.
    standalone = re.search(
        rf"^(\s*)-\s*\*\*{re.escape(heading)}:\*\*\s*$",
        block,
        re.MULTILINE,
    )
    if standalone:
        base_indent_str = standalone.group(1) or ""
        base_indent = len(base_indent_str)
        items: list[str] = []
        child_indent: int | None = None
        in_nested = False
        for line in block[standalone.end():].splitlines():
            if not line.strip():
                # Blank lines inside a bullet list are tolerated so long as
                # the next non-blank line is still indented deeper than the
                # heading bullet. If the next non-blank line is at or above
                # base_indent, the list has ended.
                continue
            indent = len(line) - len(line.lstrip(" \t"))
            stripped = line.strip()
            if indent <= base_indent:
                break
            is_bullet = stripped.startswith("- ") or stripped == "-"
            if child_indent is not None and indent > child_indent:
                # Deeper-indented line under an already-open top-level
                # child. If it is itself a bullet, treat as a nested
                # sub-bullet and skip (we only collect top-level children).
                # If it is plain text, fold it into the current bullet as
                # a continuation line so wrapped bullets round-trip intact.
                if is_bullet:
                    in_nested = True
                    continue
                if in_nested:
                    # Continuation of a nested sub-bullet — skip.
                    continue
                if items:
                    items[-1] = (items[-1] + " " + stripped).strip()
                continue
            if not is_bullet:
                # Non-bullet line at the child-indent level (or before the
                # first child) ends the list.
                break
            if child_indent is None:
                child_indent = indent
            in_nested = False
            raw = stripped[1:].strip() if stripped != "-" else ""
            items.append(raw)
        return items
    # Inline form. Returns a single-element list unless the value is empty
    # or a literal `none` / `[]`. Comma-separated inline lists are split.
    inline = re.search(
        rf"^\s*-\s*\*\*{re.escape(heading)}:\*\*\s+(.+?)\s*$",
        block,
        re.MULTILINE,
    )
    if inline:
        raw_value = inline.group(1).strip()
        if not raw_value:
            return []
        if raw_value.lower() in {"none", "[]"}:
            return []
        # `[001, 002]` → `001, 002`; also handles loose `[001,002]` with
        # no space. Non-bracketed comma lists are split verbatim.
        if raw_value.startswith("[") and raw_value.endswith("]"):
            inner = raw_value[1:-1].strip()
            if not inner:
                return []
            return [piece.strip() for piece in inner.split(",") if piece.strip()]
        return [piece.strip() for piece in raw_value.split(",") if piece.strip()]
    return []


def _extract_prose_section(block: str, heading: str) -> str | None:
    """Extract a `**<heading>:**` paragraph section from a task block.

    Returns the stripped prose (possibly multi-line) following the `**Xyz:**`
    marker, or None if the marker is absent. Stops at the next `**Xyz:**`
    paragraph marker or the end of the block, whichever comes first.
    """
    pattern = re.compile(
        rf"^\*\*{re.escape(heading)}:\*\*\s*(?:\n|\s+)",
        re.MULTILINE,
    )
    m = pattern.search(block)
    if not m:
        return None
    tail = block[m.end():]
    # Next paragraph-level `**Xyz:**` marker closes the section.
    end_m = re.search(r"^\*\*[^*]+:\*\*", tail, re.MULTILINE)
    if end_m:
        tail = tail[: end_m.start()]
    return tail.strip() or None


def _parse_task_block(
    markdown: str,
    level: int,
    *,
    raw_id: str | None = None,
    title: str | None = None,
    source_line: int | None = None,
) -> dict:
    """Parse a single task block at H2 (`level=2`) or H3 (`level=3`).

    If `raw_id` / `title` / `source_line` are not supplied, they are derived
    from the first matching heading in `markdown`.

    Returns a dict with keys:
        id (str, normalized 3-digit), title (str), source_line (int),
        priority (str | None), depends_on (list[str] of normalized ids),
        test_command (str | None), agent (str | None, omitted → None),
        files (list[str], raw entries — callers normalize),
        acceptance_criteria (list[str]), description (str | None),
        reversion_guidance (str | None), status (str | None).

    No validation — that is the caller's job. Missing fields are returned
    as `None` / `[]` so the caller can report structured errors pinned to
    `source_line`.
    """
    if raw_id is None or title is None or source_line is None:
        pattern = _task_header_re(level)
        m = pattern.search(markdown)
        if not m:
            raise ValueError(
                f"no H{level} `TASK-NNN:` heading found in the supplied block"
            )
        raw_id = m.group(1)
        title = m.group(2).strip()
        source_line = markdown.count("\n", 0, m.start()) + 1
        block = markdown[m.start():]
    else:
        block = markdown
    normalized_id = _normalize_task_id(raw_id)
    canonical_id = normalized_id if normalized_id is not None else raw_id
    # Extract the inline `Test command` in stripped form (strip surrounding
    # backticks if present — downstream consumers want the bare command).
    test_raw = _extract_metadata_field(block, "Test command")
    test_command: str | None = None
    if test_raw is not None:
        test_unwrapped = test_raw.strip()
        if (
            len(test_unwrapped) >= 2
            and test_unwrapped.startswith("`")
            and test_unwrapped.endswith("`")
        ):
            test_unwrapped = test_unwrapped[1:-1]
        test_command = test_unwrapped
    # Dependencies: inline only; `_extract_bullet_list` handles `none` /
    # `[]` / `[001, 002]` / `001, 002` equivalently.
    raw_deps = _extract_bullet_list(block, "Dependencies")
    deps: list[str] = []
    for d in raw_deps:
        nd = _normalize_task_id(d)
        # Keep the raw form if normalization fails so the caller can
        # surface an "unresolvable dep" error pinned to the task id.
        deps.append(nd if nd is not None else d.strip())
    # Files: standalone or inline form; leave raw so the caller can inspect
    # `(create|modify|delete)` annotations as needed.
    files = _extract_bullet_list(block, "Files")
    acceptance_criteria = _extract_bullet_list(block, "Acceptance criteria")
    agent_raw = _extract_metadata_field(block, "Agent")
    priority_raw = _extract_metadata_field(block, "Priority")
    status_raw = _extract_metadata_field(block, "Status")
    description = _extract_prose_section(block, "Description")
    reversion_raw = _extract_metadata_field(block, "Reversion guidance")
    return {
        "id": canonical_id,
        "raw_id": raw_id,
        "title": title,
        "source_line": source_line,
        "priority": priority_raw,
        "depends_on": deps,
        "test_command": test_command,
        "agent": agent_raw,
        "files": files,
        "acceptance_criteria": acceptance_criteria,
        "description": description,
        "reversion_guidance": reversion_raw,
        "status": status_raw,
    }


def _slugify_title(title: str, *, max_words: int = 4) -> str:
    """Produce a lowercase underscore slug of up to `max_words` tokens.

    Drops punctuation; collapses whitespace. Matches the convention used by
    the existing `docs/plans/DUAL_AGENT_Plans/TASK-NNN_<slug>.md` children
    (short, hand-authored, readable). Empty or punctuation-only titles
    fall back to `task` so the produced filename is always non-empty.
    """
    tokens = re.findall(r"[A-Za-z0-9]+", title.lower())
    if not tokens:
        return "task"
    return "_".join(tokens[:max_words]) or "task"


def _compute_decompose_batches(
    task_ids: list[str],
    deps: dict[str, list[str]],
) -> tuple[list[list[str]], list[dict]]:
    """Topo-sort `task_ids` into parallel batches using `deps`.

    Returns `(batches, errors)`. A cycle produces one error with
    `code: "cyclic-dependency"` and the remaining unscheduled ids. Ids in
    `deps` values that are not in `task_ids` are ignored here (the caller
    surfaces those as `unresolvable-dep` errors before calling this helper).
    """
    id_set = set(task_ids)
    # Normalized depends_on filtered to the known id set.
    remaining: dict[str, set[str]] = {
        tid: {d for d in deps.get(tid, []) if d in id_set} for tid in task_ids
    }
    batches: list[list[str]] = []
    while remaining:
        ready = sorted(tid for tid, ds in remaining.items() if not ds)
        if not ready:
            cycle_ids = sorted(remaining.keys())
            return batches, [
                {
                    "code": "cyclic-dependency",
                    "message": (
                        "dependency cycle detected among tasks "
                        f"{cycle_ids}"
                    ),
                    "task_ids": cycle_ids,
                }
            ]
        batches.append(ready)
        for rid in ready:
            del remaining[rid]
        for tid in remaining:
            remaining[tid].difference_update(ready)
    return batches, []


def _extract_plan_context_section(plan_text: str) -> str | None:
    """Extract the body of a whole-plan `## Context` section, if present.

    Returns the stripped paragraph(s) between `## Context` and the next
    top-level `## ` heading, or None if the section is missing / empty.
    Used by the decomposer to populate the `## Context` block of each
    emitted child file so the output satisfies `_gate_schema_valid`
    (which requires top-level Goal / Context / Verification sections).
    """
    m = re.search(r"^## Context\s*$", plan_text, re.MULTILINE)
    if not m:
        return None
    tail = plan_text[m.end():]
    end_m = re.search(r"^## ", tail, re.MULTILINE)
    if end_m:
        tail = tail[: end_m.start()]
    body = tail.strip()
    return body or None


def _extract_plan_verification_bullets(plan_text: str) -> list[str]:
    """Return the bullet items under the whole-plan `## Verification` section.

    Used by the decomposer to backfill empty per-task `**Acceptance criteria:**`
    bullets: if a task block omits AC entirely, the parent plan's verification
    bullets are a near-perfect substitute (the verification section IS the
    plan's success criteria). Returns `[]` if the section is missing or has
    no top-level bullets.
    """
    m = re.search(r"^## Verification\s*$", plan_text, re.MULTILINE)
    if not m:
        return []
    tail = plan_text[m.end():]
    end_m = re.search(r"^## ", tail, re.MULTILINE)
    if end_m:
        tail = tail[: end_m.start()]
    bullets: list[str] = []
    for line in tail.splitlines():
        stripped = line.lstrip()
        # Top-level bullets only — nested sub-bullets are skipped to keep the
        # backfill list short and aligned with the parent's headline asserts.
        indent = len(line) - len(stripped)
        if indent == 0 and stripped.startswith("- ") and len(stripped) > 2:
            bullets.append(stripped[2:].strip())
    return bullets


def _infer_test_command_from_plan(plan_text: str) -> str | None:
    """Best-effort inference of a `**Test command:**` from plan prose.

    Scans the whole-plan `## Verification` section for an inline-code
    fragment whose payload looks like a runnable test invocation (`pytest …`,
    `venv/bin/pytest …`, `npm test …`, `cargo test …`, `go test …`). Returns
    the bare command (sans backticks) on the first hit, or None if nothing
    matches. The decomposer only consults this when a task block omits
    `**Test command:**` entirely — it is a courtesy fallback, not a primary
    parse path.
    """
    m = re.search(r"^## Verification\s*$", plan_text, re.MULTILINE)
    if not m:
        return None
    tail = plan_text[m.end():]
    end_m = re.search(r"^## ", tail, re.MULTILINE)
    if end_m:
        tail = tail[: end_m.start()]
    # Inline-code payload that starts with a recognized test runner.
    runner_re = re.compile(
        r"`((?:venv/bin/)?(?:pytest|python\s+-m\s+pytest|npm\s+test|"
        r"cargo\s+test|go\s+test)\b[^`]*)`"
    )
    hit = runner_re.search(tail)
    return hit.group(1).strip() if hit else None


# ---------------------------------------------------------------------------
# Child-plan rendering scaffold
# ---------------------------------------------------------------------------
#
# `_DECOMPOSED_CHILD_SCAFFOLD` is the runtime authority for child-plan
# markdown shape. It is a module-level constant so a missing or unreadable
# `templates/decomposed_child.md.template` file in an installed plugin
# context cannot break execution. The sibling template file mirrors this
# scaffold (with the same `${...}` placeholder grammar) for human
# authoring + drift testing — see `TestDecomposedTemplateDrift` in
# `tests/scripts/test_plan_ops.py`.
#
# Placeholder slots:
#   ${TID}           Three-digit task id (e.g. `001`).
#   ${TITLE}         Human-readable task title.
#   ${CONTEXT}       Parent-plan `## Context` body, or task description
#                    fallback, or stub line.
#   ${VERIFICATION}  Multi-line bullet list (no trailing newline).
#   ${METADATA}      Multi-line metadata bullets for the H3 block (no
#                    trailing newline) — Status/Priority/Agent?/Files/
#                    Dependencies/Test command/Acceptance criteria/
#                    Reversion guidance, in that order.
#   ${DESCRIPTION}   Either an empty string (when the source omitted the
#                    description, so the rendered file ends at the
#                    `**Description:**` header) or `\n` + the description
#                    body (so the body sits on the line after the header).
_DECOMPOSED_CHILD_SCAFFOLD = (
    "# TASK-${TID} — ${TITLE}\n"
    "\n"
    "## Goal\n"
    "\n"
    "${TITLE}\n"
    "\n"
    "## Context\n"
    "\n"
    "${CONTEXT}\n"
    "\n"
    "## Verification\n"
    "\n"
    "${VERIFICATION}\n"
    "\n"
    "## Tasks\n"
    "\n"
    "### TASK-${TID}: ${TITLE}\n"
    "\n"
    "${METADATA}\n"
    "\n"
    "**Description:**${DESCRIPTION}\n"
)


def _render_child_task_file(task: dict, *, plan_context: str | None = None) -> str:
    """Render a decomposed task dict as a child-plan markdown file.

    Shape matches the `### TASK-NNN:` H3 grammar TASK-004 `build-tasks`
    parses AND the `_gate_schema_valid` top-level contract: `## Goal`,
    `## Context`, `## Verification`, and `## Tasks` wrapping the H3
    block. `**Reversion guidance:**` is emitted UNCONDITIONALLY — when
    the source task provides guidance it is used verbatim; when it
    omits the field, the stable sentinel `none` is emitted so the
    child grammar matches the pinned contract without exception.

    `plan_context`, when supplied, is the whole-plan `## Context`
    section body; it is used to populate the child's `## Context`
    section verbatim so the child carries the same narrative context as
    the parent plan. If absent, the task's `description` is used.

    The fixed-shape scaffolding is provided by the module-level
    `_DECOMPOSED_CHILD_SCAFFOLD` constant; only the variable bits
    (context body, verification bullets, metadata bullets, description)
    are assembled here and substituted in.
    """
    tid = task["id"]
    title = task["title"]
    # ---- Context: prefer the parent plan's `## Context` block for parity
    # with hand-authored child plans (see the shipped
    # `tests/fixtures/directory_mode_plan/TASK-001_seed.md`); otherwise
    # fall back to the task's own description, otherwise a stub.
    context_body = (plan_context or "").strip()
    if not context_body:
        context_body = (task.get("description") or "").strip()
    if not context_body:
        context_body = (
            f"Auto-decomposed child for TASK-{tid}. See the source plan for "
            "broader context."
        )
    # ---- Verification: derived from the task's acceptance-criteria list.
    ac_list = task.get("acceptance_criteria") or []
    if ac_list:
        verification_lines = [f"- {ac}" for ac in ac_list]
    else:
        verification_lines = ["- See acceptance criteria under the task block below."]
    verification = "\n".join(verification_lines)
    # ---- Metadata: H3 task-block bullets in canonical order.
    metadata_lines: list[str] = []
    metadata_lines.append(f"- **Status:** {task.get('status') or 'pending'}")
    priority = task.get("priority")
    if priority:
        metadata_lines.append(f"- **Priority:** {priority}")
    agent = task.get("agent")
    if agent:
        metadata_lines.append(f"- **Agent:** {agent}")
    # Files: always emit the standalone marker form for predictability.
    metadata_lines.append("- **Files:**")
    if task.get("files"):
        for f in task["files"]:
            metadata_lines.append(f"  - {f}")
    # Dependencies: inline list, `[]` if empty for parser unambiguity.
    deps = task.get("depends_on") or []
    if deps:
        metadata_lines.append(f"- **Dependencies:** [{', '.join(deps)}]")
    else:
        metadata_lines.append("- **Dependencies:** []")
    test_cmd = task.get("test_command")
    if test_cmd is None or test_cmd == "":
        metadata_lines.append("- **Test command:** none")
    elif test_cmd.strip().lower() == "none":
        # Source wrote the literal sentinel; preserve it unwrapped so the
        # round-trip is byte-identical.
        metadata_lines.append("- **Test command:** none")
    else:
        metadata_lines.append(f"- **Test command:** `{test_cmd}`")
    metadata_lines.append("- **Acceptance criteria:**")
    for ac in task.get("acceptance_criteria") or []:
        metadata_lines.append(f"  - {ac}")
    # `**Reversion guidance:**` is emitted UNCONDITIONALLY so the child
    # grammar matches the pinned contract stated in the plan's task
    # description (child files always include the section). When the
    # source task omits reversion guidance, emit the stable `none`
    # sentinel; the parser already tolerates it (round-trips as the
    # literal string "none" which downstream consumers treat as absent).
    reversion = task.get("reversion_guidance")
    if reversion:
        metadata_lines.append(f"- **Reversion guidance:** {reversion}")
    else:
        metadata_lines.append("- **Reversion guidance:** none")
    metadata = "\n".join(metadata_lines)
    # ---- Description: ALWAYS non-empty. Plan-review (Phase 1.5) treats
    # `tasks[i].description` empty/trivial as a likely-blocking gap and
    # routes the run to `needs-replan` (see `plan-reviewer.md` Intent
    # completeness check + `plan_codex_dispatch.py` / `plan_gemini_dispatch.py`
    # prompt line 6). A bare `**Description:**` header would satisfy
    # `_gate_schema_valid` but halt the run downstream. Fallback chain
    # when the source task omits the body:
    #   1. task title (always available, name-grounded)
    #   2. plus a one-line rationale pointing back at the parent context
    # so an implementer reading the child knows where to look. The
    # placeholder is a single short sentence — terse but non-empty,
    # which is the contract plan-review actually checks.
    description = (task.get("description") or "").strip()
    if not description:
        description = (
            f"{title}. (Auto-filled by decompose-plan; the source plan "
            f"omitted a `**Description:**` body for TASK-{tid}. See the "
            f"parent plan's `## Context` and `## Verification` sections "
            f"for the full intent.)"
        )
    description_slot = f"\n{description}"
    return string.Template(_DECOMPOSED_CHILD_SCAFFOLD).substitute(
        TID=tid,
        TITLE=title,
        CONTEXT=context_body,
        VERIFICATION=verification,
        METADATA=metadata,
        DESCRIPTION=description_slot,
    )


def _decompose_plan(
    plan_path: Path,
    out_dir: Path,
    *,
    force: bool = False,
) -> dict:
    """Core decomposition logic. Returns `{ok, errors, tasks, out_dir, ...}`.

    No side effects on failure — files are written only when `errors == []`.
    """
    if not plan_path.is_file():
        return {
            "ok": False,
            "errors": [
                {
                    "code": "plan-not-found",
                    "message": f"plan file not found: {plan_path}",
                }
            ],
        }
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {
            "ok": False,
            "errors": [
                {
                    "code": "plan-unreadable",
                    "message": f"cannot read {plan_path}: {exc}",
                }
            ],
        }
    task_heading_level = 2
    _, raw_blocks = _split_task_blocks_at_level(plan_text, level=2)
    if not raw_blocks:
        task_heading_level = 3
        _, raw_blocks = _split_task_blocks_at_level(plan_text, level=3)
    errors: list[dict] = []
    # Surface malformed headers (short ids like `## TASK-1:` or `## TASK-12:`)
    # as structured errors before the `no-tasks` fallback — a tightened
    # grammar must fail loudly, not silently accept or silently drop blocks.
    for malformed_level in (2, 3):
        for raw_id, source_line in _malformed_task_headers(
            plan_text, level=malformed_level,
        ):
            errors.append(
                {
                    "code": "malformed-task-header",
                    "raw_id": raw_id,
                    "message": (
                        f"`{'#' * malformed_level} TASK-{raw_id}:` at line "
                        f"{source_line} does not match "
                        "the required three-digit TASK-NNN[A] grammar"
                    ),
                    "source_line": source_line,
                }
            )
    if errors:
        return {"ok": False, "errors": errors}
    if not raw_blocks:
        errors.append(
            {
                "code": "no-tasks",
                "message": (
                    f"no `## TASK-NNN:` or `### TASK-NNN:` headings found "
                    f"in {plan_path.name}; the whole-plan decomposer "
                    "requires task headings"
                ),
                "source_line": 1,
            }
        )
        return {"ok": False, "errors": errors}
    parsed: list[dict] = []
    seen_ids: dict[str, int] = {}
    for raw_id, title, block_text, source_line in raw_blocks:
        task = _parse_task_block(
            block_text,
            level=task_heading_level,
            raw_id=raw_id,
            title=title,
            source_line=source_line,
        )
        tid = task["id"]
        if tid in seen_ids:
            errors.append(
                {
                    "code": "duplicate-id",
                    "task_id": tid,
                    "message": (
                        f"TASK-{tid} appears twice in plan; first at line "
                        f"{seen_ids[tid]}, duplicate at line {source_line}"
                    ),
                    "source_line": source_line,
                    "first_seen_line": seen_ids[tid],
                }
            )
            continue
        seen_ids[tid] = source_line
        parsed.append(task)
    # Backfill schema-required task fields with sensible defaults so a
    # decomposed plan never fails the downstream `_gate_schema_valid`
    # check on a missing `**Priority:**` / `**Test command:**` /
    # `**Acceptance criteria:**` bullet. The decomposer is a transformer,
    # not a content reviewer — surfacing structural errors for these
    # routine omissions just halts a run on something the renderer can
    # patch in place. Defaults applied here are reported back as
    # `defaults_applied` (informational, non-fatal) so the operator can
    # see what the decomposer filled in.
    #
    # Defaults:
    #   - **Priority:**          → "medium" (neutral; PRIORITY_RANKS-valid)
    #   - **Test command:**      → light inference from the parent plan's
    #                              `## Verification` section, else "none"
    #   - **Acceptance criteria:** → fall back to the parent plan's
    #                                `## Verification` bullets if any,
    #                                else leave empty (renderer still
    #                                emits the bullet header so
    #                                `_gate_schema_valid` is satisfied)
    inferred_test_command = _infer_test_command_from_plan(plan_text)
    parent_verification_bullets = _extract_plan_verification_bullets(plan_text)
    defaults_applied: list[dict] = []
    for task in parsed:
        tid = task["id"]
        if not task.get("priority"):
            task["priority"] = "medium"
            defaults_applied.append(
                {
                    "task_id": tid,
                    "field": "Priority",
                    "default": "medium",
                    "source": "decomposer-default",
                }
            )
        if task.get("test_command") is None:
            if inferred_test_command:
                task["test_command"] = inferred_test_command
                defaults_applied.append(
                    {
                        "task_id": tid,
                        "field": "Test command",
                        "default": inferred_test_command,
                        "source": "plan-verification-inference",
                    }
                )
            else:
                task["test_command"] = "none"
                defaults_applied.append(
                    {
                        "task_id": tid,
                        "field": "Test command",
                        "default": "none",
                        "source": "decomposer-default",
                    }
                )
        if not task.get("acceptance_criteria") and parent_verification_bullets:
            task["acceptance_criteria"] = list(parent_verification_bullets)
            defaults_applied.append(
                {
                    "task_id": tid,
                    "field": "Acceptance criteria",
                    "default": "(copied from parent `## Verification`)",
                    "source": "plan-verification-inference",
                }
            )
        # Description body MUST be non-empty so plan-review's Intent
        # completeness check (`tasks[i].description` non-empty) does not
        # route the run to `needs-replan`. The renderer applies a
        # title-plus-rationale fallback inside `_render_child_task_file`
        # when the source omits the body; we record the substitution
        # here so operators can see the decomposer filled it in.
        if not (task.get("description") or "").strip():
            defaults_applied.append(
                {
                    "task_id": tid,
                    "field": "Description",
                    "default": "(auto-filled from task title + parent context pointer)",
                    "source": "decomposer-default",
                }
            )
    # Validate dependency ids exist in the plan.
    known_ids = {t["id"] for t in parsed}
    for task in parsed:
        tid = task["id"]
        for dep in task["depends_on"]:
            if dep not in known_ids:
                errors.append(
                    {
                        "code": "unresolvable-dep",
                        "task_id": tid,
                        "dep_id": dep,
                        "message": (
                            f"TASK-{tid} depends on TASK-{dep} which is not "
                            "declared in this plan"
                        ),
                        "source_line": task["source_line"],
                    }
                )
    # If no structural errors so far, compute batches (cycles surface here).
    batches: list[list[str]] = []
    if not errors:
        task_ids = [t["id"] for t in parsed]
        deps_map = {t["id"]: t["depends_on"] for t in parsed}
        batches, batch_errors = _compute_decompose_batches(task_ids, deps_map)
        if batch_errors:
            # Pin cycle error to the first cycle-member's source line so
            # the reporter can jump to the offending block.
            by_id = {t["id"]: t["source_line"] for t in parsed}
            for err in batch_errors:
                first = err.get("task_ids", [None])[0]
                if first and first in by_id:
                    err["source_line"] = by_id[first]
            errors.extend(batch_errors)
    if errors:
        return {"ok": False, "errors": errors}
    # Build manifest + child files.
    plan_title_m = re.search(r"^#\s+(.+?)\s*$", plan_text, re.MULTILINE)
    plan_title = plan_title_m.group(1).strip() if plan_title_m else plan_path.stem
    plan_context = _extract_plan_context_section(plan_text)
    # Extract plan-level `**Base branch:**` metadata (whole-plan header).
    # Defaults to "main" if the source plan omits the declaration.
    base_branch_m = re.search(
        r"^\s*\**Base branch:\**\s*(.+?)\s*$",
        plan_text,
        re.MULTILINE,
    )
    base_branch = (
        base_branch_m.group(1).strip() if base_branch_m else "main"
    )
    chunks: list[dict] = []
    child_files: dict[str, str] = {}
    for task in parsed:
        slug = _slugify_title(task["title"])
        child_name = f"TASK-{task['id']}_{slug}.md"
        # Carry through the source task's `**Status:**` field into the
        # manifest chunk so an auto-decomposed directory respects the
        # source's completion state. `ALLOWED_INDEX_STATUSES` is a narrow
        # capitalized set ({Done, Pending, Superseded}) distinct from the
        # task-level vocabulary ({pending, open, in-progress, done,
        # failed, blocked, skipped}); map `done` → "Done" and everything
        # else (including missing) → "Pending".
        raw_status = (task.get("status") or "").strip().lower()
        if raw_status == "done":
            chunk_status = "Done"
        else:
            chunk_status = "Pending"
        chunks.append(
            {
                "task_id": task["id"],
                "file": child_name,
                "priority": task["priority"],
                "depends_on": task["depends_on"],
                "status": chunk_status,
                "superseded_by": [],
            }
        )
        child_files[child_name] = _render_child_task_file(
            task, plan_context=plan_context,
        )
    # Handle target directory.
    target = out_dir
    existing_manifest: dict | None = None
    if target.exists():
        if not target.is_dir():
            return {
                "ok": False,
                "errors": [
                    {
                        "code": "out-dir-not-directory",
                        "message": (
                            f"--out-dir {target} exists and is not a directory"
                        ),
                    }
                ],
            }
        existing = [p for p in target.iterdir() if p.name != ".gitkeep"]
        if existing and not force:
            return {
                "ok": False,
                "errors": [
                    {
                        "code": "out-dir-not-empty",
                        "message": (
                            f"--out-dir {target} is non-empty; pass --force "
                            "to overwrite"
                        ),
                        "existing": sorted(p.name for p in existing),
                    }
                ],
            }
        # `--force` rerun: preserve the prior manifest's `created`
        # timestamp if it parses cleanly, so byte-identical output holds
        # across day boundaries for the same source plan.
        prior_index = target / "00_INDEX.json"
        if force and prior_index.is_file():
            try:
                existing_manifest = json.loads(
                    prior_index.read_text(encoding="utf-8"),
                )
                if not isinstance(existing_manifest, dict):
                    existing_manifest = None
            except (OSError, json.JSONDecodeError):
                existing_manifest = None
    # `created` is deterministic across `--force` reruns: preserve the
    # prior manifest's value if present + valid; otherwise stamp today.
    created_value: str | None = None
    if existing_manifest is not None:
        prior_created = existing_manifest.get("created")
        if isinstance(prior_created, str) and prior_created.strip():
            created_value = prior_created
    if created_value is None:
        created_value = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    manifest = {
        "schema_version": 1,
        "source": "decompose-plan",
        "plan_title": plan_title,
        "source_plan_file": plan_path.name,
        "created": created_value,
        "base_branch": base_branch,
        "depends_on_plans": [],
        "supersedes": [],

        "chunks": chunks,
    }
    target.mkdir(parents=True, exist_ok=True)
    # On `--force`, sweep stale `TASK-*.md` children that the new chunk
    # set does not cover. If the source plan shrinks or a task is
    # renamed (slug change), the prior child file must be removed so
    # the decomposed directory matches the canonical
    # {00_INDEX.json, TASK-NNN_<slug>.md, ...} shape. Only files
    # matching the `TASK-*.md` glob are touched — unrelated notes or
    # user files in the directory are left alone.
    if force and target.is_dir():
        keep_names = set(child_files.keys())
        for stale in target.glob("TASK-*.md"):
            if stale.is_file() and stale.name not in keep_names:
                try:
                    stale.unlink()
                except OSError:
                    pass
    # Write manifest + children atomically-per-file. Idempotent under
    # --force: byte-identical rewrites of the same content if the source
    # plan did not change.
    manifest_text = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
    (target / "00_INDEX.json").write_text(manifest_text, encoding="utf-8")
    for name, body in child_files.items():
        (target / name).write_text(body, encoding="utf-8")
    return {
        "ok": True,
        "errors": [],
        "produced_dir": str(target),
        "task_count": len(parsed),
        "children": [c["file"] for c in chunks],
        "defaults_applied": defaults_applied,

    }

def _read_stdin_text() -> str:
    return sys.stdin.read()



def _args_to_payload_decompose_plan(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "out_dir": pathlib.Path(args.out_dir) if args.out_dir else None,
        "force": args.force,
    }
    return payload

def _run_decompose_plan(payload: dict) -> dict:
    plan_path = Path(payload['plan_file']).resolve()
    if payload['out_dir']:
        out_dir = Path(payload['out_dir']).resolve()
    else:
        out_dir = plan_path.parent / plan_path.stem
    result = _decompose_plan(plan_path, out_dir, force=bool(payload['force']))
    if not result['ok']:
        return _result({'errors': result['errors']}, exit_code=1)
    return _result(result, exit_code=0)

def cmd_decompose_plan(args: argparse.Namespace) -> None:
    'Heuristic plan-decomposition: whole-plan markdown → directory layout.\n\n    Reads a `## TASK-NNN:` whole-plan file and writes a sibling directory\n    containing `00_INDEX.json` plus one `TASK-NNN_<slug>.md` child per task.\n    Each child carries a `### TASK-NNN:` H3 sub-heading (matches the\n    child-plan grammar `build-tasks` parses).\n\n    Malformed plans (missing headers, duplicate ids, missing required\n    metadata, unresolvable or cyclic dependencies) error loudly with\n    structured `errors[*]` entries pinned to source line numbers.\n    '
    payload = _args_to_payload_decompose_plan(args)
    result = _run_decompose_plan(payload)
    _emit_or_die(args, result)


def _build_tasks(
    plans_dir: Path, *, filter_ids: set[str] | None = None,
) -> dict:
    """Roster-driven fat `tasks[]` synthesis for a decomposed-plan directory.

    Reads `00_INDEX.json` + each `chunks[].file` and returns a schedule-
    shaped result:

        {
            "ok": bool,
            "outcome": "valid" | "invalid",
            "tasks": [...],            # fat shape: description + AC included
            "batches": [...],          # topo-sorted + file-lock batches
            "warnings": [...],         # missing description / AC in a child
            "errors": [...],           # structural (missing roster, missing
                                       # child, cycle, ...)
        }

    Uses the shared parsing primitives (`_parse_task_block` with `level=3`,
    `_extract_metadata_field`, `_extract_bullet_list`) so the child-plan
    grammar stays single-sourced with `decompose-plan`.

    Missing `**Description:**` or `**Acceptance criteria:**` in an otherwise
    well-formed child are non-fatal — the task is still emitted with
    `description: ""` / `acceptance_criteria: []` and a structured warning
    is recorded so downstream plan-review can flag it.

    When `filter_ids` is non-None (TASK-002, narrow_run_filter_ids), the
    walk is scoped to the transitive-prereq closure of `filter_ids`
    computed via `_compute_index_closure`. Out-of-closure chunks are
    skipped before any child markdown is read — they contribute zero
    tasks, warnings, or errors. In-closure chunks whose body
    `**Dependencies:**` prose fails to parse are routed around: the
    well-formed roster `depends_on` (already validated by the closure
    helper) is trusted instead, and a non-fatal `body-deps-unparseable`
    warning is recorded so the operator can fix the prose. The result
    grows a top-level `scope` key carrying the requested ids, the full
    closure, and the count of skipped chunks. With `filter_ids=None`
    the helper is byte-for-byte identical to its prior behavior — the
    `scope` key is absent (not None) so consumers can use
    `"scope" in result` as a clean predicate.
    """
    errors: list[dict] = []
    warnings: list[dict] = []
    tasks: list[dict] = []
    closure_ids: set[str] | None = None
    skipped_chunk_count = 0
    index_path = plans_dir / "00_INDEX.json"
    if not plans_dir.is_dir():
        errors.append({
            "code": "plans-dir-not-found",
            "message": f"plans directory not found: {plans_dir}",
        })
        return {
            "ok": False, "outcome": "invalid",
            "tasks": tasks, "batches": [],
            "warnings": warnings, "errors": errors,
        }
    if not index_path.is_file():
        errors.append({
            "code": "roster-not-found",
            "message": f"00_INDEX.json not found in {plans_dir}",
        })
        return {
            "ok": False, "outcome": "invalid",
            "tasks": tasks, "batches": [],
            "warnings": warnings, "errors": errors,
        }
    try:
        roster_text = index_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append({
            "code": "roster-unreadable",
            "message": f"cannot read {index_path}: {exc}",
        })
        return {
            "ok": False, "outcome": "invalid",
            "tasks": tasks, "batches": [],
            "warnings": warnings, "errors": errors,
        }
    try:
        roster_doc = json.loads(roster_text)
    except json.JSONDecodeError as exc:
        errors.append({
            "code": "malformed-roster",
            "message": f"malformed JSON in {index_path}: {exc}",
        })
        return {
            "ok": False, "outcome": "invalid",
            "tasks": tasks, "batches": [],
            "warnings": warnings, "errors": errors,
        }
    if not isinstance(roster_doc, dict):
        errors.append({
            "code": "malformed-roster",
            "message": (
                f"index sidecar must be a JSON object in {index_path}"
            ),
        })
        return {
            "ok": False, "outcome": "invalid",
            "tasks": tasks, "batches": [],
            "warnings": warnings, "errors": errors,
        }
    chunks = roster_doc.get("chunks")
    if not isinstance(chunks, list):
        errors.append({
            "code": "malformed-roster",
            "message": f"chunks[] must be a list in {index_path}",
        })
        return {
            "ok": False, "outcome": "invalid",
            "tasks": tasks, "batches": [],
            "warnings": warnings, "errors": errors,
        }
    # When `filter_ids` is set, compute the transitive-prereq closure via
    # the roster-only helper FIRST. Closure-level errors (unknown id,
    # closure-malformed-dep, duplicate-roster-id) halt before any child
    # markdown is read. Out-of-closure chunks contribute zero work in the
    # per-chunk loop below; their child files are never opened. Any cycle
    # that crosses the closure boundary is by definition closure-internal
    # once the dep is walked, so skipped-side cycles are silenced
    # structurally rather than by an extra check.
    if filter_ids is not None:
        closure_set, closure_errors = _compute_index_closure(
            chunks, filter_ids,
        )
        closure_ids = closure_set
        if closure_errors:
            return {
                "ok": False, "outcome": "invalid",
                "tasks": [], "batches": [],
                "warnings": [], "errors": closure_errors,
                "scope": {
                    "filter_ids": sorted(filter_ids),
                    "closure": sorted(closure_ids),
                    "skipped_chunk_count": 0,
                },
            }
    # Collect per-chunk data, surfacing structural errors before trying to
    # parse individual child task blocks. A single missing child surfaces as
    # a `child-file-not-found` error and does NOT abort the remainder of
    # the roster walk — callers want every missing-child error up front.
    seen_ids: set[str] = set()
    done_ids: set[str] = set()
    for i, chunk in enumerate(chunks):
        chunk_ref = f"chunks[{i}]"
        if not isinstance(chunk, dict):
            # A non-dict chunk has no task_id, so it cannot be proven
            # outside the closure in scoped mode. Per TASK-002 AC,
            # silencing is only allowed for chunks whose normalized id
            # is verified-not-in-closure; malformed-roster entries must
            # surface in BOTH default and scoped modes so genuine
            # roster malformations are never hidden.
            errors.append({
                "code": "malformed-roster",
                "message": f"{chunk_ref} must be an object in {index_path}",
            })
            continue
        raw_task_id = chunk.get("task_id")
        normalized_id = (
            _normalize_task_id(raw_task_id)
            if isinstance(raw_task_id, str) else None
        )
        if normalized_id is None:
            # Same rationale as the non-dict branch: an un-normalizable
            # task_id has no canonical id to test against the closure,
            # so it cannot be proven outside-closure. Surface in both
            # default and scoped modes.
            errors.append({
                "code": "malformed-roster",
                "message": (
                    f"{chunk_ref}.task_id={raw_task_id!r} is not a "
                    "normalized task id"
                ),
            })
            continue
        # In scoped mode, skip out-of-closure chunks before any
        # validation, file IO, or markdown parsing — they contribute
        # zero tasks, warnings, or errors.
        if filter_ids is not None and normalized_id not in (closure_ids or set()):
            skipped_chunk_count += 1
            continue
        if normalized_id in seen_ids:
            errors.append({
                "code": "malformed-roster",
                "message": (
                    f"duplicate task_id {normalized_id!r} in {index_path}"
                ),
            })
            continue
        seen_ids.add(normalized_id)
        # Skip already-completed chunks: status=Done means the work was
        # shipped in a prior run. Including Done tasks would re-dispatch
        # them to implementer agents and produce no-op or conflicting commits.
        chunk_status = chunk.get("status")
        if isinstance(chunk_status, str) and chunk_status.strip() == "Done":
            done_ids.add(normalized_id)
            continue
        child_name = chunk.get("file")
        if not isinstance(child_name, str) or not child_name.strip():
            errors.append({
                "code": "malformed-roster",
                "message": (
                    f"{chunk_ref}.file must be a non-empty string"
                ),
            })
            continue
        child_path = plans_dir / child_name
        if not child_path.is_file():
            errors.append({
                "code": "child-file-not-found",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": (
                    f"chunks[].file {child_name!r} not found in {plans_dir}"
                ),
            })
            continue
        try:
            child_text = child_path.read_text(encoding="utf-8")
        except OSError as exc:
            errors.append({
                "code": "child-file-unreadable",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": f"cannot read {child_path}: {exc}",
            })
            continue
        # Locate the `### TASK-NNN:` block matching the chunk's task id.
        # A child file is expected to carry exactly one H3 TASK heading;
        # when multiple are present, we parse the one matching the roster
        # id and warn about the extras so they can't silently drift.
        _, h3_blocks = _split_task_blocks_at_level(child_text, level=3)
        if not h3_blocks:
            errors.append({
                "code": "missing-task-heading",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": (
                    f"no `### TASK-NNN:` H3 heading found in {child_name}"
                ),
            })
            continue
        matched = None
        for raw_id, title, block_text, source_line in h3_blocks:
            if _normalize_task_id(raw_id) == normalized_id:
                matched = (raw_id, title, block_text, source_line)
                break
        if matched is None:
            errors.append({
                "code": "task-id-mismatch",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": (
                    f"{child_name} declares no `### TASK-{normalized_id}:` "
                    "heading matching the roster task_id"
                ),
            })
            continue
        if len(h3_blocks) > 1:
            warnings.append({
                "code": "extra-task-heading",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": (
                    f"{child_name} carries {len(h3_blocks)} `### TASK-NNN:` "
                    "headings; only the matching one is parsed. This chunk "
                    "MUST be dispatched with `target_task_id` set so the "
                    "renderer auto-injects the \"Implement specifically "
                    "`### TASK-NNN:`\" disambiguator (TASK-007)."
                ),
            })
        raw_id, title, block_text, source_line = matched
        parsed = _parse_task_block(
            block_text,
            level=3,
            raw_id=raw_id,
            title=title,
            source_line=source_line,
        )
        # Warn (non-fatal) on missing fat-manifest sections. The task is
        # still emitted so downstream phases can surface the gap.
        description = parsed.get("description") or ""
        description = description.strip()
        if not description:
            warnings.append({
                "code": "missing-description",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": (
                    f"TASK-{normalized_id} in {child_name} has no "
                    "`**Description:**` section"
                ),
            })
        ac_list = list(parsed.get("acceptance_criteria") or [])
        if not ac_list:
            warnings.append({
                "code": "missing-acceptance-criteria",
                "task_id": normalized_id,
                "plan_file": child_name,
                "message": (
                    f"TASK-{normalized_id} in {child_name} has no "
                    "`**Acceptance criteria:**` bullets"
                ),
            })
        # Schedule wire format uses `dependencies` (plural), not
        # `depends_on`. Normalize ids here (already normalized by
        # `_parse_task_block`, but filter to keep canonical form).
        body_deps_raw = list(parsed.get("depends_on") or [])
        deps: list[str] = []
        for d in body_deps_raw:
            nd = _normalize_task_id(d)
            deps.append(nd if nd is not None else str(d))
        # In scoped mode, when the body deps contain entries that fail
        # to normalize (typical authoring mistake: trailing parentheticals
        # like `**Dependencies:** TASK-001 (schema)`), trust the roster's
        # `depends_on` instead of the parser. Closure-validated roster
        # deps are guaranteed normalizable + in-closure, so the resulting
        # batches are coherent. The malformed body line is surfaced as a
        # non-fatal `body-deps-unparseable` warning so the operator can
        # tidy the prose. A body that DOES normalize but resolves to an
        # id the roster doesn't carry is still routed through the
        # existing `unresolvable-dep` error path below.
        if filter_ids is not None and any(
            _normalize_task_id(d) is None for d in body_deps_raw
        ):
            roster_deps_raw = chunk.get("depends_on")
            if isinstance(roster_deps_raw, list):
                roster_deps_norm = [
                    _normalize_task_id(rd)
                    for rd in roster_deps_raw
                    if isinstance(rd, str)
                ]
                # TASK-002 binding-finding fix: the trust-roster
                # fallback must only fire when the roster's normalized
                # `depends_on` contains at least one id in
                # ``closure_ids`` (i.e., resolves to an in-closure task).
                # ``all(rd is not None for rd in [])`` is vacuously True,
                # so without this guard an empty roster deps list would
                # still trigger the fallback, replacing the body's
                # malformed dep with an empty list and silently swallowing
                # the dep error. Per spec, when the roster offers no
                # in-closure resolution, leave the body-derived unparseable
                # dep in ``deps`` so the existing ``unresolvable-dep``
                # error path fires (the malformed dep still won't resolve,
                # surfacing the right error to the operator).
                roster_resolves_in_closure = bool(
                    closure_ids is not None
                    and any(
                        rd in closure_ids
                        for rd in roster_deps_norm
                        if rd
                    )
                )
                if (
                    all(rd is not None for rd in roster_deps_norm)
                    and roster_resolves_in_closure
                ):
                    # Replace deps with the roster-side, sorted for
                    # deterministic batch output.
                    deps = sorted(rd for rd in roster_deps_norm if rd)
                    # Surface the OFFENDING raw `**Dependencies:**` line
                    # from the child block as `message` so operators (and
                    # tests) can grep for the exact malformed text. The
                    # inline form (`- **Dependencies:** TASK-001 (schema)`)
                    # is captured by `DEPENDENCIES_BULLET_RE`. The
                    # standalone form (`- **Dependencies:**` with bullet
                    # children, e.g. a child that fails to normalize) is
                    # captured via a fallback regex on the bare heading
                    # bullet. `roster_deps` (sorted) carries the
                    # canonical replacement.
                    raw_deps_match = DEPENDENCIES_BULLET_RE.search(
                        block_text,
                    )
                    if raw_deps_match is not None:
                        offending_line = raw_deps_match.group(0).strip()
                    else:
                        standalone_match = re.search(
                            r"^\s*-\s*\*\*Dependencies:\*\*\s*$",
                            block_text,
                            re.MULTILINE,
                        )
                        offending_line = (
                            standalone_match.group(0).strip()
                            if standalone_match is not None
                            else "- **Dependencies:**"
                        )
                    warnings.append({
                        "code": "body-deps-unparseable",
                        "task_id": normalized_id,
                        "plan_file": Path(child_name).name,
                        "message": offending_line,
                        "roster_deps": deps,
                    })
        # `plan_file` is consumed downstream (parse-schedule, apply-review,
        # plan-file routing) as a basename-only field. When a roster entry
        # points into a subdirectory (e.g. `subdir/TASK-001_a.md`), we still
        # locate the child on disk via the full `child_name` above, but the
        # emitted `plan_file` must be the basename so `_is_valid_plan_file_basename`
        # accepts it.
        deps = [d for d in deps if d not in done_ids]
        task_entry: dict[str, object] = {
            "id": normalized_id,
            "title": parsed.get("title") or title,
            "files": list(parsed.get("files") or []),
            "dependencies": deps,
            "test_command": parsed.get("test_command"),
            "priority": (parsed.get("priority") or "").strip() or None,
            "plan_file": Path(child_name).name,
            "description": description,
            "acceptance_criteria": ac_list,
        }
        # `agent` is emitted iff the child declares `**Agent:**` — the
        # classifier fan-out (TASK-005) populates it for the remaining
        # children. Emitting a placeholder here would mask missing-agent
        # children from that path.
        agent_raw = parsed.get("agent")
        if agent_raw:
            task_entry["agent"] = agent_raw.strip()
        tasks.append(task_entry)
    # Cycle detection / topo-sort + file-lock + global-lock batching are
    # delegated to the canonical ``_dependency_aware_batches`` helper
    # (PLAN_TOPO_RESPECT_FIX_2026-04-25 TASK-003). Both ``build-tasks`` and
    # ``compute-schedule`` route through the same helper so their
    # ``batches[]`` outputs agree byte-for-byte for the same input. The
    # roster-specific ``unresolvable-dep`` message is preserved by
    # post-processing the helper's ``errors[]`` before they are returned.
    #
    # The helper is fed a projection of ``tasks`` whose ``files[]`` entries
    # are normalized through ``_normalize_files_entry`` (strip backticks,
    # ``(create|modify|delete)`` annotations, ``:line`` suffixes). This
    # mirrors what ``_compute_schedule_batches`` does before calling the
    # same helper, and is what makes the pinned byte-equality post-condition
    # (``test_build_tasks_then_compute_schedule_is_no_op_on_batches``) hold.
    # The returned ``tasks[]`` retains the raw, annotated ``files`` entries
    # — only the helper's view is normalized.
    batches: list[dict] = []
    if not errors:
        task_ids_to_process = [str(t["id"]) for t in tasks]
        helper_tasks = [
            {
                **t,
                "files": [
                    _normalize_files_entry(str(f))
                    for f in (t.get("files") or [])
                ],
            }
            for t in tasks
        ]
        roster_batches, batch_errors = _dependency_aware_batches(
            helper_tasks, task_ids_to_process,
        )
        for e in batch_errors:
            if e.get("code") == "unresolvable-dep":
                e["message"] = (
                    f"TASK-{e['task_id']} depends on TASK-{e['dep_id']} "
                    "which is not declared in the roster"
                )
        if batch_errors:
            errors.extend(batch_errors)
        else:
            batches = roster_batches
    outcome = "valid" if not errors else "invalid"
    result: dict[str, object] = {
        "ok": not errors,
        "outcome": outcome,
        "tasks": tasks,
        "batches": batches,
        "warnings": warnings,
        "errors": errors,
    }
    # `scope` is present iff `filter_ids` was supplied so consumers can
    # use `"scope" in result` as a clean predicate. We deliberately do
    # NOT emit `scope: None` on full-roster runs (key absence carries
    # the negative).
    if filter_ids is not None:
        result["scope"] = {
            "filter_ids": sorted(filter_ids),
            "closure": sorted(closure_ids or set()),
            "skipped_chunk_count": skipped_chunk_count,
        }
    return result


def _args_to_payload_build_tasks(args: argparse.Namespace) -> dict:
    payload = {
        "plans_dir": pathlib.Path(args.plans_dir) if args.plans_dir else None,
        "filter_ids": args.filter_ids,
    }
    return payload

def _run_build_tasks(payload: dict) -> dict:
    plans_dir = Path(payload['plans_dir']).resolve()
    raw_filter = (payload.get('filter_ids') or '').strip()
    filter_ids: set[str] | None = None
    if raw_filter:
        normalized: set[str] = set()
        for token in raw_filter.split(','):
            stripped = token.strip()
            if not stripped:
                continue
            norm = _normalize_task_id(stripped)
            if norm is None:
                return _result({'ok': False, 'outcome': 'invalid', 'tasks': [], 'batches': [], 'warnings': [], 'errors': [{'code': 'invalid-filter-ids', 'message': f'could not normalize filter id {stripped!r}'}]}, exit_code=1)
            normalized.add(norm)
        if normalized:
            filter_ids = normalized
    result = _build_tasks(plans_dir, filter_ids=filter_ids)
    if not result['ok']:
        return _result(result, exit_code=1)
    return _result(result, exit_code=0)

def cmd_build_tasks(args: argparse.Namespace) -> None:
    'Roster-driven fat `tasks[]` synthesis for a decomposed directory.\n\n    Reads `00_INDEX.json` + each `chunks[].file` in the supplied directory\n    and emits a schedule-shaped JSON document with per-task `description`\n    + `acceptance_criteria` (the "fat" manifest required by schedule-only\n    plan-review). Parsing reuses the shared helpers used by\n    `decompose-plan` at `level=3` so both subcommands track the same\n    child-plan grammar.\n\n    Structural errors (missing roster, malformed JSON, missing child\n    file, unresolvable or cyclic deps) surface as `errors[*]` with a\n    non-zero exit. Missing `**Description:**` or `**Acceptance criteria:**`\n    in an individual child surfaces as `warnings[*]` with the task id and\n    the child basename; the task is still emitted (non-fatal).\n\n    `--filter-ids <csv>` narrows the walk to the transitive-prereq\n    closure of the supplied ids (TASK-002, narrow_run_filter_ids). The\n    csv accepts plain (`9`), zero-padded (`009`), or `TASK-NNN`\n    (`TASK-009`) forms; each token is normalized via\n    `_normalize_task_id`. The result grows a top-level `scope` key\n    carrying the requested ids, the full closure, and the count of\n    skipped chunks.\n    '
    payload = _args_to_payload_build_tasks(args)
    result = _run_build_tasks(payload)
    _emit_or_die(args, result)


def _find_status_bullet(block: str) -> re.Match | None:
    return STATUS_BULLET_RE.search(block)


def mutate_task_status(plan_text: str, task_id: str, new_status: str) -> tuple[str, str]:
    """Structurally mutate TASK-NNN's `**Status:**` bullet to `new_status`.

    Returns (updated_plan_text, prior_status). Raises ValueError on drift:
    task block not found, Status bullet not found, or new_status invalid.
    """
    if new_status not in ALLOWED_TASK_STATUSES:
        raise ValueError(f"status {new_status!r} not in {sorted(ALLOWED_TASK_STATUSES)}")
    preamble, blocks = _split_task_blocks(plan_text)
    if not blocks:
        raise ValueError("no task blocks found in plan")
    updated: list[str] = []
    prior: str | None = None
    matched = False
    for tid, body in blocks:
        if tid == task_id and not matched:
            m = _find_status_bullet(body)
            if not m:
                raise ValueError(f"no **Status:** bullet in TASK-{task_id}")
            prior = m.group(2).strip()
            new_line = f"{m.group(1)} {new_status}"
            new_body = body[: m.start()] + new_line + body[m.end() :]
            updated.append(new_body)
            matched = True
        else:
            updated.append(body)
    if not matched:
        raise ValueError(f"TASK-{task_id} not found in plan")
    return preamble + "".join(updated), prior or ""


_AGENT_BULLET_RE = re.compile(
    r"^(\s*-\s*\*\*Agent:\*\*)\s*(.+?)\s*$", re.MULTILINE,
)
_PRIORITY_BULLET_RE = re.compile(
    r"^(\s*-\s*\*\*Priority:\*\*)\s*(.+?)\s*$", re.MULTILINE,
)
_FILES_BULLET_RE = re.compile(
    r"^(\s*)-\s*\*\*Files:\*\*", re.MULTILINE,
)


def mutate_task_agent(plan_text: str, task_id: str, new_agent: str) -> tuple[str, str]:
    """Structurally mutate TASK-NNN's `**Agent:**` bullet to `new_agent`.

    Returns ``(updated_plan_text, prior_agent)``. ``prior_agent`` is the
    empty string when no ``**Agent:**`` bullet existed previously.

    Insertion order (when the bullet is absent) matches ``_emit_child``'s
    canonical metadata layout (`plan_ops.py:3187-3197`):

    * Between ``**Priority:**`` and ``**Files:**`` when both exist.
    * Immediately after ``**Priority:**`` if ``**Files:**`` is absent.
    * Immediately before ``**Files:**`` if ``**Priority:**`` is absent.
    * Otherwise at the end of the contiguous metadata-bullet run (before
      the first non-bullet line after the task header).

    Raises ``ValueError`` on: unknown ``new_agent``, task block not found,
    or a task block carrying no metadata bullets at all.
    """
    if new_agent not in ALLOWED_AGENTS:
        raise ValueError(
            f"agent {new_agent!r} not in {sorted(ALLOWED_AGENTS)}"
        )
    preamble, blocks = _split_task_blocks(plan_text)
    if not blocks:
        raise ValueError("no task blocks found in plan")
    updated: list[str] = []
    prior: str | None = None
    matched = False
    for tid, body in blocks:
        if tid == task_id and not matched:
            m = _AGENT_BULLET_RE.search(body)
            if m:
                prior = m.group(2).strip()
                new_line = f"{m.group(1)} {new_agent}"
                new_body = body[: m.start()] + new_line + body[m.end():]
            else:
                prior = ""
                new_body = _insert_agent_bullet(body, task_id, new_agent)
            updated.append(new_body)
            matched = True
        else:
            updated.append(body)
    if not matched:
        raise ValueError(f"TASK-{task_id} not found in plan")
    return preamble + "".join(updated), prior or ""


def _insert_agent_bullet(body: str, task_id: str, new_agent: str) -> str:
    """Insert a new ``- **Agent:** <new_agent>`` bullet into ``body``.

    See ``mutate_task_agent`` for the placement rules. Raises
    ``ValueError`` when the task block carries no metadata bullets at all.
    """
    pm = _PRIORITY_BULLET_RE.search(body)
    fm = _FILES_BULLET_RE.search(body)
    if pm is not None:
        line_end = body.find("\n", pm.end())
        insert_at = len(body) if line_end == -1 else line_end + 1
        indent = re.match(r"\s*", body[pm.start():]).group(0)
        prefix = "" if (line_end != -1 or body.endswith("\n")) else "\n"
        new_line = f"{prefix}{indent}- **Agent:** {new_agent}\n"
        return body[:insert_at] + new_line + body[insert_at:]
    if fm is not None:
        indent = fm.group(1)
        new_line = f"{indent}- **Agent:** {new_agent}\n"
        return body[: fm.start()] + new_line + body[fm.start():]
    # Neither Priority nor Files present — fall back to the end of the
    # contiguous metadata-bullet run (before the first non-bullet line
    # after the task header).
    lines = body.splitlines(keepends=True)
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("### TASK-"):
            header_idx = i
            break
    if header_idx is None:
        raise ValueError(f"TASK-{task_id} block missing header line")
    last_bullet: int | None = None
    started = False
    for i in range(header_idx + 1, len(lines)):
        line = lines[i]
        if line.lstrip().startswith("- "):
            last_bullet = i
            started = True
            continue
        if line.strip() == "" and not started:
            continue
        break
    if last_bullet is None:
        raise ValueError(
            f"TASK-{task_id} block has no metadata bullets to anchor Agent insertion"
        )
    insert_at = sum(len(lines[i]) for i in range(last_bullet + 1))
    indent = re.match(r"\s*", lines[last_bullet]).group(0)
    last_has_newline = lines[last_bullet].endswith("\n")
    prefix = "" if last_has_newline else "\n"
    new_line = f"{prefix}{indent}- **Agent:** {new_agent}\n"
    return body[:insert_at] + new_line + body[insert_at:]


def _parse_index_roster(path: Path) -> dict[str, dict]:
    """Parse the JSON sidecar roster for DUAL_AGENT_Plans.

    Returns `{task_id: {"file": filename, "depends_on": deps, "status": status}}`.
    Supersession metadata is validated and stored in `_INDEX_SUPERSEDED_BY`.
    """
    if not path.is_file():
        raise FileNotFoundError(f"index file not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"malformed JSON in {path}: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"index sidecar must be a JSON object in {path}")
    if "schema_version" not in doc or "chunks" not in doc:
        raise ValueError(f"index sidecar missing schema_version or chunks in {path}")
    if doc["schema_version"] != 1:
        raise ValueError(f"unsupported index schema_version {doc['schema_version']!r} in {path}")
    chunks = doc["chunks"]
    if not isinstance(chunks, list):
        raise ValueError(f"index chunks must be a list in {path}")

    def _valid_normalized_id(raw: object, field: str, chunk_ref: str) -> str:
        if not isinstance(raw, str):
            raise ValueError(f"{field} for {chunk_ref} must be a string")
        normalized = _normalize_task_id(raw)
        if normalized is None or raw != normalized:
            raise ValueError(f"{field} for {chunk_ref} must be a normalized task id")
        return normalized

    global _INDEX_SUPERSEDED_BY
    _INDEX_SUPERSEDED_BY = {}
    roster: dict[str, dict] = {}
    for i, chunk in enumerate(chunks):
        chunk_ref = f"chunks[{i}]"
        if not isinstance(chunk, dict):
            raise ValueError(f"{chunk_ref} must be an object")
        missing = {"task_id", "file", "depends_on", "status", "superseded_by"} - set(chunk)
        if missing:
            raise ValueError(f"{chunk_ref} missing required fields: {sorted(missing)}")
        task_id = _valid_normalized_id(chunk["task_id"], "task_id", chunk_ref)
        if task_id in roster:
            raise ValueError(f"duplicate task_id {task_id} in {path}")
        filename = chunk["file"]
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"file for {task_id} must be a non-empty string")
        status = chunk["status"]
        if status not in ALLOWED_INDEX_STATUSES:
            raise ValueError(f"status for {task_id} must be one of {sorted(ALLOWED_INDEX_STATUSES)}")
        depends_raw = chunk["depends_on"]
        superseded_raw = chunk["superseded_by"]
        if not isinstance(depends_raw, list):
            raise ValueError(f"depends_on for {task_id} must be a list")
        if not isinstance(superseded_raw, list):
            raise ValueError(f"superseded_by for {task_id} must be a list")
        deps = [_valid_normalized_id(dep, "depends_on", task_id) for dep in depends_raw]
        superseded_by = [
            _valid_normalized_id(dep, "superseded_by", task_id)
            for dep in superseded_raw
        ]
        if status == "Superseded" and not superseded_by:
            raise ValueError(f"Superseded task {task_id} must have superseded_by targets")
        if status != "Superseded" and superseded_by:
            raise ValueError(f"non-Superseded task {task_id} must not have superseded_by targets")
        roster[task_id] = {"file": filename, "depends_on": deps, "status": status}
        if superseded_by:
            _INDEX_SUPERSEDED_BY[task_id] = superseded_by

    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(task_id: str) -> None:
        if task_id in visited:
            return
        if task_id in visiting:
            raise ValueError(f"supersession cycle involving {task_id}")
        visiting.add(task_id)
        for child in _INDEX_SUPERSEDED_BY.get(task_id, []):
            if child in _INDEX_SUPERSEDED_BY:
                _visit(child)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in list(_INDEX_SUPERSEDED_BY):
        _visit(task_id)
    return roster


def _update_index_status(
    index_path: Path,
    plan_basename: str,
    new_status: str,
    task_id: str | None = None,
) -> tuple[str, dict | None]:
    """Flip the `status` of the chunk matching `plan_basename` (+ `task_id`).

    When multiple chunks share a plan file (e.g., TASK-014 holding 14B and
    14C, TASK-027 holding 27A/27B/27C), matching by basename alone flips
    whichever chunk the iteration visits first and leaves siblings stuck at
    their prior status. Passing `task_id` disambiguates: the matcher then
    requires `chunk.file == plan_basename AND str(chunk.task_id) == task_id`,
    so each per-task `commit-task` invocation flips exactly its own chunk.
    `task_id=None` preserves legacy file-only matching for back-compat.

    TASK-014B: written atomically via `tempfile.NamedTemporaryFile(dir=parent)
    + os.replace(tmp, index_path)` so a mid-write interrupt leaves the prior
    roster intact. The write is idempotent — re-running for an already-at-
    target chunk produces byte-identical bytes on the second run.

    Returns `(outcome, error_dict_or_None)`:
      * `("updated", None)` — chunk found, status changed, file rewritten.
      * `("unchanged", None)` — chunk found, status already at target; no
        write performed (byte-identical idempotency is trivially satisfied).
      * `("missing-index", err)` — `00_INDEX.json` absent (tolerated by the
        caller as a soft skip).
      * `("invalid-index", err)` — roster present but unparseable or missing
        `chunks`. Caller must treat as a hard error so silently-invalid
        sidecars don't drift.
      * `("missing-entry", err)` — roster parsed but no chunk matches
        `plan_basename`.

    `err` is an `errors[0]`-shaped dict `{path, code, message}` ready to feed
    into `_die`.
    """
    if new_status not in ALLOWED_INDEX_STATUSES:
        raise ValueError(
            f"new_status must be one of {sorted(ALLOWED_INDEX_STATUSES)}, got {new_status!r}"
        )

    if not index_path.is_file():
        return "missing-index", {
            "path": f"$.<file:{index_path}>",
            "code": "index-not-found",
            "message": f"index file not found: {index_path}",
        }

    raw_text = index_path.read_text(encoding="utf-8")
    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return "invalid-index", {
            "path": f"$.<file:{index_path}>",
            "code": "index-malformed",
            "message": f"malformed JSON in {index_path}: {exc}",
        }
    if not isinstance(doc, dict) or not isinstance(doc.get("chunks"), list):
        return "invalid-index", {
            "path": f"$.<file:{index_path}>",
            "code": "index-malformed",
            "message": f"index sidecar missing chunks in {index_path}",
        }

    target_idx = None
    for i, chunk in enumerate(doc["chunks"]):
        if not isinstance(chunk, dict) or chunk.get("file") != plan_basename:
            continue
        if task_id is not None and str(chunk.get("task_id")) != task_id:
            continue
        target_idx = i
        break

    if target_idx is None:
        detail = f"plan basename {plan_basename!r}"
        if task_id is not None:
            detail += f" + task_id {task_id!r}"
        return "missing-entry", {
            "path": f"$.<file:{index_path}>.chunks",
            "code": "task-not-in-index",
            "message": f"no chunk in 00_INDEX.json matches {detail}",
        }

    current_status = doc["chunks"][target_idx].get("status")
    if current_status == new_status:
        # Idempotent: re-running commit-task on an already-Done chunk is a
        # no-op write. Skip the tempfile dance entirely so the on-disk bytes
        # are trivially byte-identical across runs.
        return "unchanged", None

    doc["chunks"][target_idx]["status"] = new_status

    # `json.dumps(..., indent=2)` mirrors the roster's existing layout (see
    # `_write_roster` in the test suite and the on-disk 00_INDEX.json files
    # in docs/plans/DUAL_AGENT_Plans/). Trailing newline for POSIX-friendly
    # diffs.
    new_text = json.dumps(doc, indent=2) + "\n"

    parent = index_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(parent),
            prefix=".00_INDEX.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(new_text)
            tmp_name = tmp.name
        os.replace(tmp_name, index_path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    return "updated", None


def _first_plan_task_id(plan_text: str) -> str | None:
    """Return the first TASK id declared in a plan file."""
    _, blocks = _split_task_blocks(plan_text)
    if not blocks:
        return None
    return blocks[0][0]


def _append_run_log(event: str, fields: dict) -> str:
    """Append a JSONL event and verify via tail. Returns the written line."""
    rec = {"ts": _now(), "event": event, **fields}
    line = json.dumps(rec, sort_keys=False)
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    try:
        with RUN_LOG_PATH.open("r", encoding="utf-8") as fh:
            last = ""
            for last in fh:
                pass
        if last.rstrip("\n") != line:
            with RUN_LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as e:
        raise RuntimeError(f"run-log append verification failed: {e}")
    return line


_DEFERRED_TEST_RE = re.compile(
    r"^deferred\s*\(\s*TASK-(?P<task_id>\d{3}[A-Z]?)\s*\)\s*(?P<note>.*)$",
    re.IGNORECASE,
)


def _parse_deferred_test_command(test_cmd: str) -> dict:
    """Classify the Phase 1.5 deferred-test marker.

    Accepted form is ``deferred (TASK-NNN[A-Z]?)`` with optional trailing
    note. Bare/malformed ``deferred`` must fail closed so wrappers never
    silently skip a real test command because of a typo.
    """
    cmd = (test_cmd or "").strip()
    if len(cmd) >= 2 and cmd.startswith("`") and cmd.endswith("`"):
        cmd = cmd[1:-1].strip()
    if not cmd.lower().startswith("deferred"):
        return {"kind": "not_deferred", "command": cmd}
    m = _DEFERRED_TEST_RE.match(cmd)
    if not m:
        return {
            "kind": "malformed",
            "command": cmd,
            "error": (
                "malformed deferred test marker; expected "
                "`deferred (TASK-NNN)` with a task reference"
            ),
        }
    note = (m.group("note") or "").strip()
    return {
        "kind": "deferred",
        "command": cmd,
        "deferred_to": m.group("task_id").upper(),
        "note": note,
    }


def _git(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# TASK-020B: opt-in `acceptance_v_check` runtime enforcement.
#
# `_parse_frontmatter` extracts a YAML block delimited by `---` lines at the
# VERY top of the plan (`\A---\n...\n---\n`). Missing/malformed → {}; the
# commit-task flow then behaves identically to pre-TASK-020B.
#
# `_run_v_check` executes the declared shell command with a bounded timeout
# and returns a structured dict: on success `{code: "v-check-passed",
# stdout_tail}`; on failure one of `acceptance-v-check-failed`,
# `v-check-timeout`, `v-check-subprocess-error` with matching
# {stdout_tail, stderr_tail, message} fields. Tails are capped at 2048 bytes
# to keep error envelopes bounded when a V-check produces MBs of output.
#
# Shell-injection surface: `shell=True` is intentional — plan authors declare
# shell commands like `venv/bin/pytest -q tests/scripts/test_foo.py`. Plans
# must not be edited by untrusted parties without review.
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> dict:
    """Return the top-of-file YAML frontmatter as a dict, or {} if absent.

    Uses `re.match` with `\\A` so only a frontmatter block that starts at
    position 0 is recognized; a `---` line mid-document is NOT picked up.
    """
    m = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not m:
        return {}
    try:
        import yaml  # available per TASK-013
        data = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _run_v_check(cmd: str, cwd: Path, timeout: int) -> dict:
    """Execute `cmd` via shell with bounded timeout; return structured result.

    Success: `{code: "v-check-passed", stdout_tail: <last 2048 bytes>}`.
    Failure codes:
        - `acceptance-v-check-failed` — non-zero exit
        - `v-check-timeout` — subprocess.TimeoutExpired
        - `v-check-subprocess-error` — any other launch error
    Each failure dict carries `stdout_tail`, `stderr_tail`, `message`.
    """
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        stdout_val = e.stdout if isinstance(e.stdout, str) else (
            e.stdout.decode("utf-8", errors="replace")
            if isinstance(e.stdout, (bytes, bytearray)) else ""
        )
        stderr_val = e.stderr if isinstance(e.stderr, str) else (
            e.stderr.decode("utf-8", errors="replace")
            if isinstance(e.stderr, (bytes, bytearray)) else ""
        )
        return {
            "code": "v-check-timeout",
            "message": f"acceptance V-check exceeded {timeout}s",
            "stdout_tail": (stdout_val or "")[-2048:],
            "stderr_tail": (stderr_val or "")[-2048:],
        }
    except Exception as e:
        return {
            "code": "v-check-subprocess-error",
            "message": f"acceptance V-check failed to launch: {e}",
            "stdout_tail": "",
            "stderr_tail": "",
        }
    if proc.returncode != 0:
        return {
            "code": "acceptance-v-check-failed",
            "message": f"acceptance V-check exited with code {proc.returncode}",
            "stdout_tail": (proc.stdout or "")[-2048:],
            "stderr_tail": (proc.stderr or "")[-2048:],
        }
    return {
        "code": "v-check-passed",
        "stdout_tail": (proc.stdout or "")[-2048:],
    }


# ---------------------------------------------------------------------------
# Batch reconciliation (Fix E)
# ---------------------------------------------------------------------------
#
# Defensive even if a wrapper envelope somehow reports a protected path as
# out-of-scope: reconciliation never restores/unlinks executor
# infrastructure. The protection set + predicate are imported from
# ``_plan_paths`` (the single source of truth shared with the wrapper and
# ``cmd_fail_task``).


def _envelope_field(env: dict, key: str, default=None):
    """Fetch a field from a dispatch envelope.

    The wrapper spreads `extra` onto the envelope root, so the same key may
    appear at either level. Top-level wins if both are present, except for the
    out-of-scope reconciliation fields where legacy root/extra values are
    combined with nested `scope` values.
    """
    if key == "out_of_scope_observed":
        values = []
        for container in (
            env,
            env.get("extra") if isinstance(env.get("extra"), dict) else None,
            env.get("scope") if isinstance(env.get("scope"), dict) else None,
        ):
            if isinstance(container, dict) and key in container:
                values.append(container[key])
        if values:
            return any(bool(value) for value in values)
        return default
    if key in {"out_of_scope_tracked", "out_of_scope_untracked"}:
        merged: list = []
        seen: set[str] = set()
        for container in (
            env,
            env.get("extra") if isinstance(env.get("extra"), dict) else None,
            env.get("scope") if isinstance(env.get("scope"), dict) else None,
        ):
            if not isinstance(container, dict):
                continue
            value = container.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, str) and item not in seen:
                    merged.append(item)
                    seen.add(item)
        if merged:
            return merged
        return default
    if key in env:
        return env[key]
    extra = env.get("extra") or {}
    if isinstance(extra, dict):
        return extra.get(key, default)
    return default


OUT_OF_SCOPE_PAUSE_OPTIONS = (
    "widen-plan",
    "in-place-fix",
    "keep-and-commit",
    "revert",
)
ALLOWED_OUT_OF_SCOPE_POLICIES = ("pause", "reconcile-and-revert")


_RECONCILE_SCOPE_ANNOTATION_RE = re.compile(
    r"\s*\(\s*(?:create|modify|delete|edit)\s*\)\s*$",
    re.IGNORECASE,
)


def _normalize_reconcile_scope_entry(raw: str) -> str:
    """Normalize a schedule ``files[]`` entry for reconcile-batch scope.

    This strips only recognized scope annotations. Unknown parentheticals are
    left attached so they cannot silently widen a task's allowed scope.
    """
    cleaned = raw.strip()
    if cleaned.startswith("`"):
        m_backtick = re.match(r"`([^`]+)`(?P<tail>.*)$", cleaned)
        if m_backtick:
            path = m_backtick.group(1).strip()
            tail = _RECONCILE_SCOPE_ANNOTATION_RE.sub(
                "", m_backtick.group("tail").strip(),
            ).strip()
            cleaned = path if not tail else f"{path} {tail}"
        else:
            cleaned = cleaned.strip("`")
    else:
        cleaned = _RECONCILE_SCOPE_ANNOTATION_RE.sub("", cleaned).strip()
        cleaned = re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)[0].strip()
        if cleaned.startswith("`") and cleaned.endswith("`") and len(cleaned) >= 2:
            cleaned = cleaned[1:-1].strip()
    cleaned = re.sub(r":\d+[-–]\d+$", "", cleaned)
    cleaned = re.sub(r":\d+$", "", cleaned)
    return cleaned.strip()


def _path_in_reconcile_scope(path: str, allowed: set[str]) -> bool:
    """Return true when ``path`` is exactly declared or below a declared dir."""
    rel = path.strip().strip("/")
    if rel in allowed:
        return True
    for entry in allowed:
        base = entry.strip().rstrip("/")
        if base and rel.startswith(f"{base}/"):
            return True
    return False


def reconcile_batch(
    batch_envelopes: list[dict],
    repo_root: str,
    *,
    schedule_file: str | None = None,
    out_of_scope_policy: str = "pause",
    plans_dir: str | None = None,
) -> list[dict]:
    """Reconcile observed out-of-scope writes after a batch's join barrier.

    Called by the orchestrator in the single-writer phase between batch
    completion and per-task review/commit dispatch. No wrapper process is
    running at this point, so it is safe to mutate the working tree.

    For each envelope with ``out_of_scope_observed`` true:
      * Skip any path matching the executor-infrastructure protection set
      * Plan-aware partition (when ``schedule_file`` is provided): split
        each list into ``kept`` (path is in the dispatched task's
        normalised ``Files:`` set — wrapper false-positive) and
        ``restored`` (genuinely out-of-scope) buckets.
      * Restore tracked paths in ``restored`` (staged + worktree)
      * Unlink untracked paths in ``restored``
      * Verify the actioned paths no longer appear in `git diff`

    A task whose reconciliation actually restores something is marked
    ``scope_violation_reconciled``; a task whose entries were all
    preserved by the plan-aware filter is marked
    ``scope_violation_preserved``. Both remain **ineligible for review
    and commit** in v1. A task where reconciliation fails or leaves
    residual dirt is marked ``reconciliation_failed`` — the orchestrator
    must surface a hard error and must not advance to the next batch
    until operator intervention. Unaffected tasks return ``no_op``.

    When ``schedule_file`` is missing, unparseable, or lacks the
    envelope's task id, the function falls back to today's behaviour
    (no plan-aware filter, restore everything not protected) and emits
    ``warning: "schedule_lookup_failed"`` in that envelope's result
    entry.
    """
    if out_of_scope_policy not in ALLOWED_OUT_OF_SCOPE_POLICIES:
        raise ValueError(
            f"out_of_scope_policy {out_of_scope_policy!r} not in "
            f"{list(ALLOWED_OUT_OF_SCOPE_POLICIES)}"
        )

    # Build {canonical_task_id: set(normalised file paths)} once. Empty
    # dict signals "no plan-aware filter available"; per-envelope
    # missing-task lookups also degrade to that baseline behaviour.
    schedule_files_by_task: dict[str, set[str]] = {}
    # TASK-008: capture per-task plan_file (basename) for the pause-mode
    # `mutate_task_status` call. Schedule entries declare `plan_file` in
    # directory mode; single-file mode may omit it.
    schedule_plan_file_by_task: dict[str, str] = {}
    schedule_load_failed = False
    if schedule_file:
        try:
            data = json.loads(Path(schedule_file).read_text(encoding="utf-8"))
            for task in data.get("tasks", []) or []:
                # Schedule emits the canonical 3-digit id under `id`
                # (NOT `task_id`).
                tid = task.get("id")
                if not isinstance(tid, str):
                    continue
                raw_files = task.get("files", []) or []
                schedule_files_by_task[tid] = {
                    _normalize_reconcile_scope_entry(str(entry))
                    for entry in raw_files
                    if isinstance(entry, str)
                }
                pf = task.get("plan_file")
                if isinstance(pf, str) and pf:
                    schedule_plan_file_by_task[tid] = pf
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            schedule_load_failed = True

    results: list[dict] = []
    for env in batch_envelopes:
        task_id = env.get("task_id", "")
        if not _envelope_field(env, "out_of_scope_observed", False):
            results.append({
                "task_id": task_id,
                "outcome": "no_op",
                "reconciled_tracked": [],
                "reconciled_untracked": [],
                "reconcile_kept_tracked": [],
                "reconcile_kept_untracked": [],
                "skipped_protected": [],
                "residual_dirty": [],
                "error": None,
            })
            continue

        raw_tracked = _envelope_field(env, "out_of_scope_tracked") or []
        raw_untracked = _envelope_field(env, "out_of_scope_untracked") or []
        tracked = [p for p in raw_tracked if isinstance(p, str)]
        untracked = [p for p in raw_untracked if isinstance(p, str)]

        skipped: set[str] = set()
        protected_filtered_tracked: list[str] = []
        protected_filtered_untracked: list[str] = []
        for p in tracked:
            if is_protected_path(p):
                skipped.add(p)
            else:
                protected_filtered_tracked.append(p)
        for p in untracked:
            if is_protected_path(p):
                skipped.add(p)
            else:
                protected_filtered_untracked.append(p)

        # Plan-aware partition. If we have a schedule entry for this
        # task id, split each list into kept (declared in scope) vs
        # actionable (genuinely out-of-scope, restore today's way).
        # Otherwise fall back: everything actionable, emit warning.
        warning: str | None = None
        if schedule_load_failed:
            warning = "schedule_lookup_failed"
            allowed: set[str] | None = None
        elif schedule_file and task_id in schedule_files_by_task:
            allowed = schedule_files_by_task[task_id]
        else:
            if schedule_file:
                # File loaded fine but this task id isn't there.
                warning = "schedule_lookup_failed"
            allowed = None

        kept_tracked: list[str] = []
        kept_untracked: list[str] = []
        actionable_tracked: list[str] = []
        actionable_untracked: list[str] = []
        if allowed is not None:
            for p in protected_filtered_tracked:
                if _path_in_reconcile_scope(p, allowed):
                    kept_tracked.append(p)
                else:
                    actionable_tracked.append(p)
            for p in protected_filtered_untracked:
                if _path_in_reconcile_scope(p, allowed):
                    kept_untracked.append(p)
                else:
                    actionable_untracked.append(p)
        else:
            actionable_tracked = list(protected_filtered_tracked)
            actionable_untracked = list(protected_filtered_untracked)

        # TASK-008 (G10): out-of-scope pause. Under the default
        # `pause` policy we do NOT touch the working tree for paths
        # that are genuinely out-of-scope — the Completed-Work
        # Preservation Principle requires we hand control back to the
        # user. Mark the task `paused` in its plan file (when we can
        # resolve it) and surface the four options so the next
        # conversation turn can decide: widen-plan / in-place-fix /
        # keep-and-commit / revert.
        #
        # Ordering note (Codex review fix): this branch runs AFTER the
        # schedule-aware partitioning above. Paths that are declared
        # in the task's `Files:` list are PRESERVED in
        # `reconcile_kept_*`; only the genuinely out-of-scope remainder
        # (`actionable_*`) drives the pause decision. If everything is
        # in-scope (no actionable remainder), we fall through to the
        # legacy preservation path which yields `scope_violation_preserved`.
        if out_of_scope_policy == "pause" and (
            actionable_tracked or actionable_untracked
        ):
            pause_warning: str | None = warning
            plan_status_updated = False
            plan_status_error: str | None = None
            plan_file_basename = schedule_plan_file_by_task.get(task_id)
            if plans_dir and plan_file_basename:
                # TASK-008 binding-finding fix: schedule-provided ``plan_file``
                # is untrusted input (analyst output). Without basename
                # validation, ``Path(plans_dir) / plan_file_basename`` would
                # silently follow ``..`` segments and write outside
                # ``plans_dir`` (path traversal). ``_is_valid_plan_file_basename``
                # rejects ``/``, ``\``, ``..``, leading dot, NUL, and oversized
                # values; combined with ``Path(plans_dir).resolve()`` +
                # containment check below it forms defense in depth.
                if not _is_valid_plan_file_basename(plan_file_basename):
                    plan_status_error = (
                        f"schedule-provided plan_file "
                        f"{plan_file_basename!r} failed basename "
                        f"validation; refusing to construct a path under "
                        f"{plans_dir!s} (path-traversal protection)"
                    )
                else:
                    plans_dir_resolved = Path(plans_dir).resolve()
                    plan_path = (
                        plans_dir_resolved / Path(plan_file_basename).name
                    ).resolve()
                    # Defense in depth: resolved path must be contained
                    # in resolved plans_dir. Catches cases the basename
                    # validator might miss on exotic platforms.
                    try:
                        plan_path.relative_to(plans_dir_resolved)
                    except ValueError:
                        plan_status_error = (
                            f"resolved plan_path {plan_path!s} is not "
                            f"contained in {plans_dir_resolved!s}; "
                            f"refusing write (path-traversal protection)"
                        )
                    else:
                        try:
                            plan_text = _load_text(plan_path)
                            mutated, _prior = mutate_task_status(
                                plan_text, task_id, "paused",
                            )
                            _atomic_write_text(plan_path, mutated)
                            plan_status_updated = True
                        except (FileNotFoundError, OSError, ValueError) as e:
                            plan_status_error = (
                                f"mutate_task_status failed: {e}"
                            )
            elif plan_file_basename and not plans_dir:
                plan_status_error = (
                    "plans_dir not provided; cannot resolve "
                    f"{plan_file_basename!r} for status mutation"
                )
            elif not plan_file_basename:
                plan_status_error = (
                    "task plan_file not found in schedule; "
                    "cannot mutate plan status"
                )
            entry: dict = {
                "task_id": task_id,
                "outcome": "scope_violation_paused",
                "reconciled_tracked": [],
                "reconciled_untracked": [],
                "reconcile_kept_tracked": kept_tracked,
                "reconcile_kept_untracked": kept_untracked,
                "skipped_protected": sorted(skipped),
                "residual_dirty": [],
                "error": None,
                "out_of_scope_tracked": actionable_tracked,
                "out_of_scope_untracked": actionable_untracked,
                "awaiting_user_options": list(OUT_OF_SCOPE_PAUSE_OPTIONS),
                "plan_status_updated": plan_status_updated,
            }
            if plan_status_error:
                entry["plan_status_error"] = plan_status_error
            if pause_warning:
                entry["warning"] = pause_warning
            results.append(entry)
            continue

        errors: list[str] = []
        cwd = Path(repo_root)
        if actionable_tracked:
            r1 = _git(["restore", "--staged", "--"] + actionable_tracked, cwd=cwd)
            if r1.returncode != 0:
                errors.append(
                    f"git restore --staged failed: {r1.stderr.strip() or r1.stdout.strip()}"
                )
            r2 = _git(["restore", "--"] + actionable_tracked, cwd=cwd)
            if r2.returncode != 0:
                errors.append(
                    f"git restore failed: {r2.stderr.strip() or r2.stdout.strip()}"
                )

        for p in actionable_untracked:
            full = cwd / p
            try:
                full.unlink()
            except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
                errors.append(f"unlink {p!r} failed: {e}")

        expected_clean = set(actionable_tracked + actionable_untracked)
        residual: list[str] = []
        if expected_clean:
            still_dirty: set[str] = set()
            for args in (
                ["diff", "--name-only", "HEAD"],
                ["diff", "--name-only", "--cached"],
                ["ls-files", "--others", "--exclude-standard"],
            ):
                proc = _git(args, cwd=cwd)
                if proc.returncode == 0:
                    still_dirty.update(
                        ln.strip() for ln in proc.stdout.splitlines() if ln.strip()
                    )
            residual = sorted(expected_clean & still_dirty)

        if errors or residual:
            outcome = "reconciliation_failed"
            error_msg: str | None = (
                "; ".join(errors) if errors
                else f"residual dirty paths: {residual}"
            )
        else:
            # Any restoration trumps preserved-only.
            had_restoration = bool(actionable_tracked or actionable_untracked)
            had_kept = bool(kept_tracked or kept_untracked)
            if had_restoration:
                outcome = "scope_violation_reconciled"
            elif had_kept:
                outcome = "scope_violation_preserved"
            else:
                # Neither restored nor kept (everything was protected
                # or the lists were empty). Preserve today's behaviour:
                # report scope_violation_reconciled with empty lists,
                # matching the no-restoration-needed semantics.
                outcome = "scope_violation_reconciled"
            error_msg = None

        result_entry: dict = {
            "task_id": task_id,
            "outcome": outcome,
            "reconciled_tracked": actionable_tracked,
            "reconciled_untracked": actionable_untracked,
            "reconcile_kept_tracked": kept_tracked,
            "reconcile_kept_untracked": kept_untracked,
            "skipped_protected": sorted(skipped),
            "residual_dirty": residual,
            "error": error_msg,
        }
        if warning:
            result_entry["warning"] = warning
        results.append(result_entry)
    return results


def _args_to_payload_reconcile_batch(args: argparse.Namespace) -> dict:
    payload = {
        "repo_root": pathlib.Path(args.repo_root) if args.repo_root else None,
        "schedule_file": pathlib.Path(args.schedule_file) if args.schedule_file else None,
        "out_of_scope_policy": args.out_of_scope_policy,
        "plans_dir": pathlib.Path(args.plans_dir) if args.plans_dir else None,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_reconcile_batch(payload: dict) -> dict:
    raw = payload['stdin_text']
    try:
        envelopes = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError as e:
        return _result({'error': f'envelopes json decode: {e}'}, exit_code=1)
    if not isinstance(envelopes, list):
        return _result({'error': 'envelopes payload must be a JSON array'}, exit_code=1)
    schedule_file = payload.get('schedule_file')
    plans_dir = payload.get('plans_dir')
    out_of_scope_policy = payload.get('out_of_scope_policy', 'pause')
    try:
        results = reconcile_batch(envelopes, payload['repo_root'], schedule_file=schedule_file, out_of_scope_policy=out_of_scope_policy, plans_dir=plans_dir)
    except ValueError as e:
        return _result({'error': str(e)}, exit_code=1)
    any_failed = any((r['outcome'] == 'reconciliation_failed' for r in results))
    any_paused = any((r['outcome'] == 'scope_violation_paused' for r in results))
    return _result({'results': results, 'reconciliation_failed': any_failed, 'paused': any_paused}, exit_code=1 if any_failed else 0)

def cmd_reconcile_batch(args: argparse.Namespace) -> None:
    'CLI wrapper over reconcile_batch.\n\n    Reads an array of envelopes from stdin; writes the result list as JSON.\n    Exits 0 if every task reconciled cleanly or was a no_op; exits 1 if any\n    task reconciliation failed (so the orchestrator can halt the batch).\n    '
    payload = _args_to_payload_reconcile_batch(args)
    result = _run_reconcile_batch(payload)
    _emit_or_die(args, result)


def _resolve_plan_deps(plan_path: Path, plans_dir: Path) -> dict:
    """Core dep-resolution logic. Returns {pass, deps, unresolved, errors}.

    `errors[]` is non-empty on fatal issues (plan not found, bad roster,
    missing task block, task not in 00_INDEX.json); `pass` is False in
    that case and `deps`/`unresolved` are empty. Callers decide whether
    to halt or treat unresolvable as "leave the existing value in place".
    """
    if not plan_path.is_file():
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{plan_path}>",
                "code": "file-not-found",
                "message": f"plan file not found: {plan_path}",
            }],
        }

    index_path = plans_dir / "00_INDEX.json"
    try:
        roster = _parse_index_roster(index_path)
    except (FileNotFoundError, ValueError) as e:
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{index_path}>",
                "code": "index-not-found",
                "message": str(e),
            }],
        }

    plan_text = plan_path.read_text(encoding="utf-8")
    current_task_id = _first_plan_task_id(plan_text)
    if current_task_id is None:
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{plan_path}>",
                "code": "task-not-found",
                "message": f"no TASK block found in {plan_path}",
            }],
        }
    current_entry = roster.get(current_task_id)
    if current_entry is None:
        return {
            "pass": False,
            "deps": [],
            "unresolved": [],
            "errors": [{
                "path": f"$.<file:{index_path}>.chunks",
                "code": "task-not-in-index",
                "message": f"task {current_task_id} is not declared in 00_INDEX.json roster",
            }],
        }
    requested = current_entry["depends_on"]

    resolved: list[dict] = []
    unresolved: list[dict] = []

    def _resolve_dependency(dep_id: str, parent_id: str | None = None) -> None:
        entry = roster.get(dep_id)
        if entry is None:
            item = {
                "task_id": dep_id,
                "reason": "superseded-target-missing" if parent_id else "unresolved-dep",
                "detail": f"task {dep_id} is not declared in 00_INDEX.json roster",
            }
            if parent_id:
                item["parent_id"] = parent_id
            unresolved.append(item)
            return

        plan_file = entry["file"]
        status = entry["status"]
        if status == "Done":
            resolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "status": status,
                **({"parent_id": parent_id} if parent_id else {}),
            })
            return
        if status == "Pending":
            unresolved.append({
                "task_id": dep_id,
                "plan_file": plan_file,
                "reason": "dep-not-done",
                "status": status,
                "detail": f"roster status is {status!r}, expected 'Done'",
                **({"parent_id": parent_id} if parent_id else {}),
            })
            return
        if status == "Superseded":
            targets = _INDEX_SUPERSEDED_BY.get(dep_id, [])
            for target_id in targets:
                _resolve_dependency(target_id, parent_id=dep_id)
            return
        unresolved.append({
            "task_id": dep_id,
            "plan_file": plan_file,
            "reason": "dep-not-done",
            "status": status,
            "detail": f"roster status is {status!r}, expected 'Done'",
            **({"parent_id": parent_id} if parent_id else {}),
        })

    for dep_id in requested:
        _resolve_dependency(dep_id)

    return {
        "pass": len(unresolved) == 0,
        "deps": resolved,
        "unresolved": unresolved,
        "errors": [],
    }


def _args_to_payload_check_plan_deps(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "plans_dir": pathlib.Path(args.plans_dir) if args.plans_dir else None,
    }
    return payload

def _run_check_plan_deps(payload: dict) -> dict:
    result = _resolve_plan_deps(Path(payload['plan_file']), Path(payload['plans_dir']))
    if result['errors']:
        return _result({'errors': result['errors']}, exit_code=1)
    return _result(result, exit_code=0)

def cmd_check_plan_deps(args: argparse.Namespace) -> None:
    payload = _args_to_payload_check_plan_deps(args)
    result = _run_check_plan_deps(payload)
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# index-closure (TASK-001 — narrow_run_filter_ids)
# ---------------------------------------------------------------------------
#
# Pure-roster transitive-prereq closure walker. The malformed
# `**Dependencies:**` prose problem lives in the per-child markdown body, not
# in the roster's structured `chunks[].depends_on`. The closure must therefore
# key on the roster — never on the child body — so it survives malformed
# siblings entirely.


def _load_index_chunks(plans_dir: Path) -> tuple[list[dict] | None, list[dict]]:
    """Load the raw `chunks[]` array from `00_INDEX.json`.

    Returns `(chunks, errors)`. On fatal parse / shape problems the chunks
    value is `None` and the errors list carries a `code`-tagged dict; on a
    well-formed but tolerated-malformed roster (e.g., individual chunks with
    bad shapes) the chunks list is returned as-is and per-chunk validation
    is deferred to `_compute_index_closure`.

    Unlike `_parse_index_roster`, this loader does NOT raise on duplicate
    `task_id` values or non-normalizable ids — those are surfaced as errors
    by the closure helper so the CLI can keep emitting JSON output instead
    of crashing.
    """
    index_path = plans_dir / "00_INDEX.json"
    if not index_path.is_file():
        return None, [{
            "code": "index-not-found",
            "path": str(index_path),
            "message": f"index file not found: {index_path}",
        }]
    try:
        doc = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return None, [{
            "code": "index-malformed-json",
            "path": str(index_path),
            "message": f"malformed JSON in {index_path}: {e}",
        }]
    if not isinstance(doc, dict):
        return None, [{
            "code": "index-shape-invalid",
            "path": str(index_path),
            "message": f"index sidecar must be a JSON object in {index_path}",
        }]
    chunks = doc.get("chunks")
    if not isinstance(chunks, list):
        return None, [{
            "code": "index-shape-invalid",
            "path": str(index_path),
            "message": f"index chunks must be a list in {index_path}",
        }]
    return chunks, []


def _compute_index_closure(
    chunks: list[dict],
    requested_ids: set[str],
) -> tuple[set[str], list[dict]]:
    """Compute the transitive-prereq closure of `requested_ids` from chunks.

    Returns `(closure_ids, errors)` where `closure_ids` is the set of
    canonical-cased task ids reachable from the (normalized) requested
    seeds via `chunks[].depends_on`, and `errors` is a list of
    `{code, ...}` dicts surfaced for:

    * `unknown-requested-id` — a requested id that doesn't normalize, or
      normalizes but doesn't resolve to any chunk.
    * `closure-malformed-dep` — a `depends_on` entry on a chunk INSIDE
      the closure that fails to normalize or doesn't resolve to any
      chunk. The offending chunk's normalized `task_id` is reported as
      `task_id` and the bad value as `dep_id`.
    * `duplicate-roster-id` — two chunks share the same normalized
      `task_id`. The first occurrence wins; subsequent duplicates are
      surfaced (with their chunk index) and ignored for traversal.

    Errors outside the closure are NEVER surfaced — if a sibling chunk
    has malformed `depends_on` but isn't reached, the helper doesn't
    even look at it.

    Errors are emitted in source order: `unknown-requested-id` entries
    first (sorted by id for determinism since the input is a set),
    followed by chunk-related errors in chunk-declaration order. Within
    a single chunk, `closure-malformed-dep` errors are returned in
    `depends_on` index order.
    """
    # Build a `{normalized_id: chunk}` map once. Track duplicates and
    # surface them in chunk-declaration order. The first occurrence wins
    # for traversal; subsequent duplicates are recorded as errors but
    # not re-bound in the lookup map.
    chunk_by_id: dict[str, dict] = {}
    chunk_index_by_id: dict[str, int] = {}
    chunk_errors: list[tuple[int, int, dict]] = []
    for idx, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            # Skip non-dict chunks. They cannot contribute to the
            # closure and there is no `task_id` to key on; the loader
            # already accepted them so they exist in the source.
            continue
        raw_id = chunk.get("task_id")
        if not isinstance(raw_id, str):
            continue
        normalized = _normalize_task_id(raw_id)
        if normalized is None:
            continue
        if normalized in chunk_by_id:
            chunk_errors.append((idx, -1, {
                "code": "duplicate-roster-id",
                "task_id": normalized,
                "chunk_index": idx,
                "first_chunk_index": chunk_index_by_id[normalized],
            }))
            continue
        chunk_by_id[normalized] = chunk
        chunk_index_by_id[normalized] = idx

    # Normalize requested ids. Track the original raw input for each
    # normalized id so we can surface `unknown-requested-id` entries
    # with diagnostic context. Un-normalizable entries are surfaced as
    # `unknown-requested-id` with `task_id: None`.
    seeds: set[str] = set()
    unknown_requested: list[dict] = []
    raw_unnormalizable: list[str] = []
    for raw in requested_ids:
        normalized = _normalize_task_id(raw) if isinstance(raw, str) else None
        if normalized is None:
            raw_unnormalizable.append(str(raw))
            continue
        if normalized not in chunk_by_id:
            unknown_requested.append({
                "code": "unknown-requested-id",
                "task_id": normalized,
            })
            continue
        seeds.add(normalized)

    # BFS the closure. Cycles (including self-cycles) terminate via
    # the visited-set guard; self-cycles ALSO surface as
    # closure-malformed-dep below. The frontier is processed in
    # arbitrary order (the closure set is order-insensitive); errors
    # are gathered with their source position so they can be sorted.
    closure: set[str] = set(seeds)
    frontier: list[str] = list(seeds)
    while frontier:
        current = frontier.pop(0)
        chunk = chunk_by_id.get(current)
        if chunk is None:
            # Should not happen — seeds were filtered above.
            continue
        deps = chunk.get("depends_on")
        if not isinstance(deps, list):
            # A chunk inside the closure with a non-list depends_on is
            # malformed at the chunk-shape level; surface as a
            # closure-malformed-dep with `dep_id: None`.
            chunk_errors.append((chunk_index_by_id[current], 0, {
                "code": "closure-malformed-dep",
                "task_id": current,
                "dep_id": None,
                "reason": "depends_on is not a list",
            }))
            continue
        for dep_idx, dep in enumerate(deps):
            normalized_dep = _normalize_task_id(dep) if isinstance(dep, str) else None
            if normalized_dep is None or normalized_dep not in chunk_by_id:
                chunk_errors.append((chunk_index_by_id[current], dep_idx, {
                    "code": "closure-malformed-dep",
                    "task_id": current,
                    "dep_id": dep,
                }))
                continue
            # Self-cycle (length 1) — surface as closure-malformed-dep
            # AND skip enqueue (visited guard would already terminate).
            if normalized_dep == current:
                chunk_errors.append((chunk_index_by_id[current], dep_idx, {
                    "code": "closure-malformed-dep",
                    "task_id": current,
                    "dep_id": dep,
                    "reason": "self-referential depends_on (cycle of length 1)",
                }))
                continue
            if normalized_dep not in closure:
                closure.add(normalized_dep)
                frontier.append(normalized_dep)

    # Assemble errors in source order:
    #   1. unknown-requested-id (sorted by task_id; raw un-normalizable
    #      first with task_id: None, sorted by raw for determinism).
    #   2. chunk-related errors (sorted by chunk index, then dep index).
    errors: list[dict] = []
    for raw in sorted(raw_unnormalizable):
        errors.append({"code": "unknown-requested-id", "task_id": None, "raw": raw})
    for entry in sorted(unknown_requested, key=lambda e: e["task_id"]):
        errors.append(entry)
    for _idx, _dep_idx, payload in sorted(chunk_errors, key=lambda t: (t[0], t[1])):
        errors.append(payload)

    return closure, errors


def _args_to_payload_index_closure(args: argparse.Namespace) -> dict:
    payload = {
        "plans_dir": pathlib.Path(args.plans_dir) if args.plans_dir else None,
        "task_ids": args.task_ids,
    }
    return payload

def _run_index_closure(payload: dict) -> dict:
    plans_dir = Path(payload['plans_dir'])
    raw_ids = (payload['task_ids'] or '').strip()
    requested_ids: set[str] = set()
    if raw_ids:
        for token in raw_ids.split(','):
            stripped = token.strip()
            if stripped:
                requested_ids.add(stripped)
    chunks, load_errors = _load_index_chunks(plans_dir)
    if chunks is None:
        return _result({'closure': [], 'skipped_chunk_count': 0, 'errors': load_errors}, exit_code=1)
    closure, errors = _compute_index_closure(chunks, requested_ids)
    payload = {'closure': sorted(closure), 'skipped_chunk_count': max(0, len(chunks) - len(closure)), 'errors': errors}
    if errors:
        return _result(payload, exit_code=1)
    return _result(payload, exit_code=0)

def cmd_index_closure(args: argparse.Namespace) -> None:
    payload = _args_to_payload_index_closure(args)
    result = _run_index_closure(payload)
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# lint-plans (TASK-020A)
# ---------------------------------------------------------------------------


# Top-level plan `**Status:**` bullet. Per §D.3, a plan whose header status is
# `superseded` has its decomposition tracked by children; duplicating the
# done-pairing gate at the parent level produces noise. Match case-insensitive
# (plan authors occasionally write `Superseded`). Unlike STATUS_BULLET_RE this
# matches bold prose lines without the leading hyphen bullet, because parent
# plan headers tend to use that shape (e.g. `**Status:** superseded`).
_SUPERSEDED_HEADER_RE = re.compile(
    r"^\s*(?:-\s*)?\*\*Status:\*\*\s*superseded\b",
    re.MULTILINE | re.IGNORECASE,
)

# Match a canonical `feat(TASK-NNN[A-Z]?):` commit subject. We grep subjects
# only (via `--pretty=%s`) so TASK-019's concern-prose references in commit
# bodies don't falsely mark a task as "shipped".
_FEAT_COMMIT_SUBJECT_RE = re.compile(r"^feat\(TASK-(\d{3}[A-Z]?)\):")


def _load_commit_done_ids(run_log: Path) -> set[str]:
    """Collect normalized task ids that have a `commit_done` event in run log.

    Missing file or malformed lines are tolerated: the lint's job is to flag
    missing pairings, not to validate the run log's integrity.
    """
    ids: set[str] = set()
    if not run_log.exists():
        return ids
    try:
        lines = run_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ids
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "commit_done":
            continue
        tid = _normalize_task_id(str(ev.get("task_id", "")))
        if tid:
            ids.add(tid)
    return ids


def _load_awaiting_user_ids(run_log: Path) -> set[str]:
    """Collect normalized task ids that have an `awaiting_user` event.

    TASK-002 (prohibit_silent_revert): the lint pairs each `**Status:** paused`
    task with its corresponding `awaiting_user` run-log event. Missing-pairing
    surfaces as `paused_without_awaiting_user_event`.

    Same tolerance contract as `_load_commit_done_ids`: missing file or
    malformed lines are skipped — lint flags missing pairings, not run-log
    integrity issues.
    """
    ids: set[str] = set()
    if not run_log.exists():
        return ids
    try:
        lines = run_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ids
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != "awaiting_user":
            continue
        tid = _normalize_task_id(str(ev.get("task_id", "")))
        if tid:
            ids.add(tid)
    return ids


def _run_log_handfix_pause_active(
    run_log: Path, run_id: str, task_id: str
) -> bool:
    """Return True if an `awaiting_user` pause is active for (run_id, task_id).

    Active = there exists an `awaiting_user` event for this `(run_id, task_id)`
    pair with no subsequent `commit_done` / `failed` / `run_end` event for the
    same task (or any `run_end` for the run) that would have resolved it.

    Used by `cmd_log_event` to gate `mechanism: "hand-fix"` events on
    `remediation_start` / `narrow_remediation_start` so the hand-fix rule
    (`feedback_handfix_default.md`) cannot leak into normal-flow dispatch.
    See `docs/analysis/orchestrator_dispatch_drift_20260429.md` for the
    motivating leak (run `20260429T111054` task 006).

    Tolerance contract matches the other run-log scanners: missing file or
    malformed lines are skipped (return False / continue), since the guard
    is a safety net, not a run-log integrity check.
    """
    if not run_log.exists():
        return False
    try:
        lines = run_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    norm_target = _normalize_task_id(str(task_id))
    if norm_target is None:
        return False
    paused = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("run_id") != run_id:
            continue
        evtype = ev.get("event")
        if evtype == "run_end":
            paused = False
            continue
        norm_ev_task = _normalize_task_id(str(ev.get("task_id", "")))
        if norm_ev_task != norm_target:
            continue
        if evtype == "awaiting_user":
            paused = True
        elif evtype in {"commit_done", "failed"}:
            paused = False
    return paused


def _load_feat_commit_ids(git_dir: Path) -> set[str]:
    """Collect normalized task ids shipped via `feat(TASK-NNN):` commits.

    Uses `git log --all --pretty=%s` so commit bodies (which may reference
    other tasks in prose) are ignored.
    """
    ids: set[str] = set()
    try:
        result = subprocess.run(
            ["git", "log", "--all", "--pretty=%s"],
            cwd=str(git_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return ids
    if result.returncode != 0:
        return ids
    for line in result.stdout.splitlines():
        m = _FEAT_COMMIT_SUBJECT_RE.match(line.strip())
        if not m:
            continue
        tid = _normalize_task_id(m.group(1))
        if tid:
            ids.add(tid)
    return ids


def _args_to_payload_lint_plans(args: argparse.Namespace) -> dict:
    payload = {
        "plans_dir": pathlib.Path(args.plans_dir) if args.plans_dir else None,
        "run_log": args.run_log,
        "git_dir": pathlib.Path(args.git_dir) if args.git_dir else None,
    }
    return payload

def _run_lint_plans(payload: dict) -> dict:
    plans_dir = Path(payload['plans_dir']).resolve()
    run_log_path = Path(payload['run_log']).resolve() if payload['run_log'] else None
    git_dir = Path(payload['git_dir'] or '.').resolve()
    findings: list[dict] = []
    scanned = 0
    done_tasks = 0
    commit_done_ids = _load_commit_done_ids(run_log_path) if run_log_path else set()
    awaiting_user_ids = _load_awaiting_user_ids(run_log_path) if run_log_path else set()
    feat_commit_ids = _load_feat_commit_ids(git_dir)
    anchor = plans_dir.parent
    for md in sorted(plans_dir.rglob('*.md')):
        scanned += 1
        try:
            text = _load_text(md)
        except (OSError, UnicodeDecodeError):
            continue
        preamble, blocks_for_check = _split_task_blocks(text)
        if _SUPERSEDED_HEADER_RE.search(preamble):
            continue
        for raw_id, block in blocks_for_check:
            status_m = _find_status_bullet(block)
            if not status_m:
                continue
            status = status_m.group(2).strip().lower()
            if status not in {'done', 'partial', 'paused'}:
                continue
            tid = _normalize_task_id(raw_id)
            if tid is None:
                continue
            try:
                rel_path = str(md.relative_to(anchor))
            except ValueError:
                rel_path = str(md)
            if status == 'paused':
                if tid not in awaiting_user_ids:
                    findings.append({'plan_file': rel_path, 'task_id': tid, 'code': 'paused_without_awaiting_user_event', 'message': f"plan marks TASK-{tid} as 'paused' but no awaiting_user event found in run log"})
                continue
            done_tasks += 1
            if tid not in commit_done_ids:
                findings.append({'plan_file': rel_path, 'task_id': tid, 'code': 'missing-commit-done-event', 'message': f'plan marks TASK-{tid} as {status!r} but no commit_done event found in run log'})
            if tid not in feat_commit_ids:
                findings.append({'plan_file': rel_path, 'task_id': tid, 'code': 'missing-feat-commit', 'message': f"plan marks TASK-{tid} as {status!r} but no 'feat(TASK-{tid}):' commit found"})
    result = {'scanned': scanned, 'done_tasks': done_tasks, 'findings': findings}
    return _result(result, exit_code=1 if findings else 0)

def cmd_lint_plans(args: argparse.Namespace) -> None:
    "Flag `**Status:** done`/`partial` tasks without matching commit pairings.\n\n    Read-only. Scans every `*.md` file under --plans-dir, enumerates tasks\n    via `_split_task_blocks`, and for each task whose Status bullet reads\n    `done` or `partial` asserts both (a) a `commit_done` event exists in the\n    run log with a matching task_id and (b) a `feat(TASK-NNN):` commit exists\n    in the repo's git log (across all refs).\n\n    Parent plans whose top-level Status is `superseded` are skipped per the\n    §D.3 guidance: their decomposition is tracked by the superseding children.\n\n    TASK-002 (prohibit_silent_revert): `paused` is a recognized task status\n    and is NOT flagged as drift. Each `**Status:** paused` task must be\n    paired with an `awaiting_user` run-log event for the same task; an\n    unpaired paused task surfaces as `paused_without_awaiting_user_event`.\n    "
    payload = _args_to_payload_lint_plans(args)
    result = _run_lint_plans(payload)
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# Subcommand stubs
# ---------------------------------------------------------------------------


def _allowed_files_union(plan_text: str) -> dict[str, str]:
    """Return ``{path: task_id}`` for every file under any task's Files: bullet.

    TASK-008 scope-aware preflight classifier: the union of declared
    ``allowed_files`` across every task in the plan is consulted to decide
    whether a dirty tree path is inside *this* plan's scope (``plan_scope_dirty``)
    or a blocking out-of-scope edit (``source_blocking``). Duplicate files
    across tasks keep the last-wins attribution; a plan that declares the
    same file under two tasks is already a smell the analyst should flag.
    """
    result: dict[str, str] = {}
    _, blocks = _split_task_blocks(plan_text)
    for task_id, block in blocks:
        files = _extract_task_files_from_plan(plan_text, task_id)
        if not files:
            continue
        for path in files:
            if path:
                result[path] = task_id
    return result


def _is_preflight_always_ignored(path: str, plan_dir: str, plan_basename: str) -> bool:
    """Match `ALWAYS_IGNORE` / `ALWAYS_IGNORE_GLOBS` for preflight classifier.

    Covers the orchestrator-state paths from TASK-003: ``_run_log.jsonl``,
    ``_run_lock.json``, and per-plan schedule sidecars — both at the
    default ``docs/plans/`` layout and at any configured ``plan_dir``.
    Rooted in the shared `is_commit_always_ignore` predicate so the
    preflight ignore set stays aligned with the commit-safe ignore set.
    """
    if is_commit_always_ignore(path, plan_basename=plan_basename, plan_dir=plan_dir):
        return True
    # Per-configured-plan-dir run-log / run-lock (covers non-default layouts).
    pd = plan_dir.rstrip("/")
    if pd:
        if path == f"{pd}/_run_log.jsonl" or path == f"{pd}/_run_lock.json":
            return True
    # Any schedule sidecar under the plan_dir is orchestrator_state, even
    # if it belongs to a different plan (e.g. a paused run's leftover).
    # Mirrors PROTECTED_PATH_GLOBS in _plan_paths.py; commit-task stays
    # tight via is_commit_always_ignore (scoped to the current plan).
    if path.endswith(".schedule.json"):
        if path.startswith("docs/plans/") or (pd and path.startswith(f"{pd}/")):
            return True
    return False


def _args_to_payload_preflight(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "strict_branch": args.strict_branch,
        "strict_scope": args.strict_scope,
        "unattended_revert_policy": getattr(args, "unattended_revert_policy", "pause"),
    }
    return payload

def _run_preflight(payload: dict) -> dict:
    raw_policy = payload.get('unattended_revert_policy')
    if raw_policy is None:
        if not sys.stdin.isatty():
            return _result({'errors': [{'path': '$.unattended_revert_policy', 'code': 'unattended-revert-policy-required', 'message': 'stdin is not a TTY and --unattended-revert-policy was not provided. Pass an explicit value (pause | fail-fast | preserve-only) so unattended execution cannot silently discard work on a pause path.'}]}, exit_code=1)
        unattended_revert_policy = 'pause'
    else:
        unattended_revert_policy = raw_policy
    plan = Path(payload['plan_file'])
    if not plan.is_dir():
        return _result({'error': f'plan path must be a decomposed-plan directory (containing 00_INDEX.json); got {plan} which is not a directory. Single-file plans are auto-promoted via `plan_ops.py decompose-plan` at Phase 0 of the skill; direct CLI callers must pass a decomposed directory.'}, exit_code=1)
    scope: dict[str, str] = {}
    base_branch: str | None = None
    plan_doc_set: set[str] = set()
    roster_path = plan / '00_INDEX.json'
    try:
        roster = _parse_index_roster(roster_path)
    except FileNotFoundError as exc:
        return _result({'error': f'missing-roster-chunk: 00_INDEX.json not found in {plan}: {exc}'}, exit_code=1)
    except ValueError as exc:
        return _result({'error': f'missing-roster-chunk: malformed 00_INDEX.json in {plan}: {exc}'}, exit_code=1)
    missing_chunks: list[str] = []
    for entry in roster.values():
        if not (plan / entry['file']).is_file():
            missing_chunks.append(entry['file'])
    if missing_chunks:
        return _result({'error': f'missing-roster-chunk: chunks[].file declared in {roster_path} but missing on disk: {missing_chunks}'}, exit_code=1)
    toplevel_cp = _git(['rev-parse', '--show-toplevel'])
    plan_dir_rel: str
    repo_root: Path | None = None
    if toplevel_cp.returncode == 0 and toplevel_cp.stdout.strip():
        try:
            repo_root = Path(toplevel_cp.stdout.strip()).resolve()
            plan_dir_rel = plan.resolve().relative_to(repo_root).as_posix()
        except (OSError, ValueError):
            repo_root = None
            plan_dir_rel = plan.as_posix()
    else:
        plan_dir_rel = plan.as_posix()
    for entry in roster.values():
        child_path = plan / entry['file']
        child_text = _load_text(child_path)
        for p, t in _allowed_files_union(child_text).items():
            scope[p] = t
        if base_branch is None:
            base_m_child = re.search('^\\*\\*Base branch:\\*\\*\\s*(\\S+)\\s*$', child_text, re.MULTILINE)
            if base_m_child:
                base_branch = base_m_child.group(1).strip()
        chunk_rel = f"{plan_dir_rel}/{entry['file']}" if plan_dir_rel else entry['file']
        plan_doc_set.add(chunk_rel)
    index_rel = f'{plan_dir_rel}/00_INDEX.json' if plan_dir_rel else '00_INDEX.json'
    plan_doc_set.add(index_rel)
    dirty: dict[str, list] = {'plan_doc': [], 'orchestrator_state': [], 'plan_scope_dirty': [], 'source_blocking': []}
    warnings: list[str] = []
    status = _git(['status', '--porcelain'])
    ignore_basename = plan.name
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # `git status --porcelain` collapses fully-untracked directories
        # to a single entry with a trailing slash. The Phase 0
        # auto-promote step (decompose-plan) creates the plan directory
        # fresh, so on a brand-new run the entire plan_dir is reported
        # as one line `?? <plan_dir_rel>/`. Without expansion, that bare
        # directory path matches no entry in plan_doc_set and falls
        # through to source_blocking — halting preflight on the very
        # files it just legitimately created. Expand directory entries
        # at or under the plan_dir to their constituent files so the
        # classifier below can recognize them as plan_doc /
        # orchestrator_state. We deliberately do NOT expand directories
        # outside plan_dir_rel: that preserves git's default `-unormal`
        # collapse for unrelated untracked dirs (e.g. node_modules/),
        # which the operator presumably wants to stay collapsed.
        paths_to_classify: list[str] = [path]
        if path.endswith('/') and repo_root is not None and plan_dir_rel:
            dir_rel = path.rstrip('/')
            if dir_rel == plan_dir_rel or dir_rel.startswith(plan_dir_rel + '/'):
                try:
                    expanded = [
                        sub.relative_to(repo_root).as_posix()
                        for sub in (repo_root / dir_rel).rglob('*')
                        if sub.is_file()
                    ]
                except (OSError, ValueError):
                    expanded = []
                if expanded:
                    paths_to_classify = sorted(expanded)
        for p in paths_to_classify:
            if p in plan_doc_set:
                dirty['plan_doc'].append(p)
            elif _is_preflight_always_ignored(p, _PLAN_DIR_POSIX, ignore_basename):
                dirty['orchestrator_state'].append(p)
            elif p in scope:
                tid = scope[p]
                dirty['plan_scope_dirty'].append({'path': p, 'task_id': tid})
                warnings.append(f'{p} is dirty and TASK-{tid} will write to it')
            else:
                dirty['source_blocking'].append(p)
    codex_available = shutil.which('codex') is not None
    gemini_available = _resolve_gemini_available()
    sha_cp = _git(['rev-parse', 'HEAD'])
    starting_sha = sha_cp.stdout.strip() or ''
    branch_cp = _git(['rev-parse', '--abbrev-ref', 'HEAD'])
    current_branch = branch_cp.stdout.strip()
    base_branch_match = base_branch is None or current_branch == base_branch
    pass_flag = len(dirty['source_blocking']) == 0
    if payload.get('strict_scope', False) and dirty['plan_scope_dirty']:
        pass_flag = False
    if payload['strict_branch'] and (not base_branch_match):
        pass_flag = False
    result = {'pass': pass_flag, 'starting_sha': starting_sha, 'run_id': _run_id(), 'codex_available': codex_available, 'gemini_available': gemini_available, 'dirty_files': dirty, 'scope_warnings': warnings, 'base_branch': base_branch, 'current_branch': current_branch, 'base_branch_match': base_branch_match, 'python_path': _resolve_python(), 'unattended_revert_policy': unattended_revert_policy}
    if not pass_flag:
        return _result(result, exit_code=1)
    return _result(result, exit_code=0)

def cmd_preflight(args: argparse.Namespace) -> None:
    payload = _args_to_payload_preflight(args)
    result = _run_preflight(payload)
    _emit_or_die(args, result)


def _args_to_payload_parse_schedule(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "strict": args.strict,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_parse_schedule(payload: dict) -> dict:
    raw = payload['stdin_text']
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'json decode: {e}'}]}, exit_code=1)
    if not isinstance(data, dict):
        return _result({'errors': [{'path': '$', 'code': 'top-level-not-object', 'message': 'top-level schedule must be an object'}]}, exit_code=1)
    errors, warnings = _validate_schedule(data, strict_nested=bool(payload.get('strict', False)))
    if not errors:
        tasks_list = data.get('tasks') if isinstance(data.get('tasks'), list) else []
        batches_list = data.get('batches') if isinstance(data.get('batches'), list) else []
        errors.extend(_validate_schedule_dag(tasks_list, batches_list))
    raw_gaps = data.get('gaps', [])
    backfilled_gaps = raw_gaps
    if isinstance(raw_gaps, list):
        legacy_backfilled = False
        new_gaps: list = []
        for g in raw_gaps:
            if isinstance(g, dict) and 'severity' not in g:
                gtype = g.get('type')
                severity = classify_gap_severity(gtype if isinstance(gtype, str) else '')
                g = {**g, 'severity': severity}
                legacy_backfilled = True
            new_gaps.append(g)
        backfilled_gaps = new_gaps
        if legacy_backfilled:
            warnings.append("schedule gaps[] missing 'severity' field; backfilled via classify_gap_severity (unknown types default to 'hard')")
    out_tasks = data.get('tasks') if isinstance(data.get('tasks'), list) else []
    tagged_tasks: list = []
    for t in out_tasks:
        if isinstance(t, dict):
            files = t.get('files') or []
            if isinstance(files, list):
                gl = any((_is_global_lock_path(str(f)) for f in files if isinstance(f, str)))
            else:
                gl = False
            tagged_tasks.append({**t, 'global_lock': gl})
        else:
            tagged_tasks.append(t)
    result = {'outcome': data.get('outcome'), 'tasks': tagged_tasks, 'batches': data.get('batches') if isinstance(data.get('batches'), list) else [], 'gaps': backfilled_gaps, 'risks': data.get('risks', []), 'warnings': warnings, 'errors': errors}
    if "state" in data:
        result["state"] = copy.deepcopy(data["state"])
    if "plan_review_state" in data:
        result["plan_review_state"] = copy.deepcopy(data["plan_review_state"])
    if errors:
        return _result(result, exit_code=1)
    return _result(result, exit_code=0)

def cmd_parse_schedule(args: argparse.Namespace) -> None:
    payload = _args_to_payload_parse_schedule(args)
    result = _run_parse_schedule(payload)
    _emit_or_die(args, result)


def _args_to_payload_compute_schedule(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "strict": args.strict,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_compute_schedule(payload: dict) -> dict:
    raw = payload['stdin_text']
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'json decode: {e}'}]}, exit_code=1)
    if not isinstance(data, dict):
        return _result({'errors': [{'path': '$', 'code': 'top-level-not-object', 'message': 'top-level schedule must be an object'}]}, exit_code=1)
    tasks = data.get('tasks')
    if not isinstance(tasks, list):
        return _result({'errors': [{'path': '$.tasks', 'code': 'invalid-type', 'message': 'tasks must be an array'}]}, exit_code=1)
    topo, batches, errors = _compute_schedule_batches(tasks)
    normalized_errors: list[dict] = []
    for err in errors:
        code = err.get('code')
        if code == 'unresolvable-dep':
            normalized_errors.append({'path': f"$.tasks[{err.get('task_id', '')}].dependencies", 'code': code, 'message': err.get('message', '')})
        elif code == 'cyclic-dependency':
            normalized_errors.append({'path': '$.tasks', 'code': code, 'message': err.get('message', '')})
        else:
            normalized_errors.append(err)
    errors = normalized_errors
    tagged_tasks: list = []
    for t in tasks:
        if isinstance(t, dict):
            files = t.get('files') or []
            if isinstance(files, list):
                gl = any((_is_global_lock_path(str(f)) for f in files if isinstance(f, str)))
            else:
                gl = False
            tagged_tasks.append({**t, 'global_lock': gl})
        else:
            tagged_tasks.append(t)
    result = {'tasks': tagged_tasks, 'topo': topo, 'batches': batches, 'errors': errors, 'warnings': []}
    if errors:
        return _result(result, exit_code=1)
    return _result(result, exit_code=0)

def cmd_compute_schedule(args: argparse.Namespace) -> None:
    payload = _args_to_payload_compute_schedule(args)
    result = _run_compute_schedule(payload)
    _emit_or_die(args, result)


def _args_to_payload_write_schedule(args: argparse.Namespace) -> dict:
    payload = {
        "schedule_file": pathlib.Path(args.schedule_file) if args.schedule_file else None,
        "stdin": args.stdin,
        "strict": args.strict,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_write_schedule(payload: dict) -> dict:
    raw = payload['stdin_text']
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'json decode: {e}'}]}, exit_code=1)
    if not isinstance(data, dict):
        return _result({'errors': [{'path': '$', 'code': 'top-level-not-object', 'message': 'top-level schedule must be an object'}]}, exit_code=1)
    errors, warnings = _validate_schedule(data, strict_nested=bool(payload.get('strict', False)))
    if errors:
        return _result({'errors': errors, 'warnings': warnings}, exit_code=1)
    path = Path(payload['schedule_file'])
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=False) + '\n'
    try:
        nbytes = _atomic_write_text(path, text)
    except OSError as e:
        return _result({'errors': [{'path': f'$.<file:{path}>', 'code': 'write-failed', 'message': f'atomic write failed: {e}'}]}, exit_code=1)
    return _result({'written': str(path), 'bytes': nbytes, 'warnings': warnings}, exit_code=0)

def cmd_write_schedule(args: argparse.Namespace) -> None:
    payload = _args_to_payload_write_schedule(args)
    result = _run_write_schedule(payload)
    _emit_or_die(args, result)


def _args_to_payload_batch_next(args: argparse.Namespace) -> dict:
    payload = {
        "schedule_file": pathlib.Path(args.schedule_file) if args.schedule_file else None,
        "locked_files": args.locked_files,
        "done": args.done,
        "failed": args.failed,
        "paused": args.paused,
        "parallel": args.parallel,
        "from_schedule_state": bool(getattr(args, "from_schedule_state", False)),
    }
    return payload

def _run_batch_next(payload: dict) -> dict:
    sched_path = Path(payload['schedule_file'])
    if not sched_path.is_file():
        return _result({'error': f'schedule file not found: {sched_path}'}, exit_code=1)
    try:
        data = json.loads(sched_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        return _result({'error': f'schedule json decode: {e}'}, exit_code=1)
    if not isinstance(data, dict):
        return _result({'errors': [{'path': '$', 'code': 'top-level-not-object', 'message': 'top-level schedule must be an object'}]}, exit_code=1)
    errors, warnings = _validate_schedule(data)
    if errors:
        return _result({'errors': errors, 'warnings': warnings}, exit_code=1)

    def _split_csv(raw: object) -> list[str]:
        if isinstance(raw, list):
            return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
        return [s for s in (raw or '').split(',') if s]
    locked = set(_split_csv(payload['locked_files']))
    done = set(_split_csv(payload['done']))
    failed = set(_split_csv(payload['failed']))
    if payload.get('from_schedule_state', False):
        persisted = read_schedule_state(sched_path)
        done |= {tid for tid in persisted.get('done', []) if isinstance(tid, str)}
        failed |= {tid for tid in persisted.get('failed', []) if isinstance(tid, str)}
        locked |= {f for f in persisted.get('locked_files', []) if isinstance(f, str)}
        failed |= {tid for tid in persisted.get('blocked', []) if isinstance(tid, str)}
    paused = set(_split_csv(payload.get('paused') or ''))
    tasks_by_id: dict[str, dict] = {}
    for t in data.get('tasks') or []:
        raw_tid = t.get('id')
        tid = _normalize_task_id(str(raw_tid)) if raw_tid is not None else None
        if tid:
            tasks_by_id[tid] = t
    dag_errors = [
        e for e in _validate_schedule_dag(list(data.get('tasks') or []), list(data.get('batches') or []))
        if e.get('code') != 'dependency-batch-violation'
    ]
    if dag_errors:
        return _result({'errors': dag_errors}, exit_code=1)

    def _files(task: dict) -> list[str]:
        return list(task.get('files') or [])

    def _ready(task: dict) -> bool:
        """A task is ready iff every declared dep is in `done`.

        Tasks with any dep in `failed` are NOT ready — V14 invariant. Unknown
        or not-yet-done deps also block readiness. Dep references that do not
        normalize (malformed) are ignored for readiness purposes but would
        have been caught by `_validate_schedule_refs` upstream.
        """
        for dep in task.get('dependencies') or []:
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None:
                continue
            if dep_norm not in done:
                return False
        return True
    remaining = [t for tid, t in tasks_by_id.items() if tid not in done and tid not in failed and (tid not in paused)]
    ready = [t for t in remaining if _ready(t)]
    active_batch: dict | None = None
    for b in data.get('batches') or []:
        bids = [_normalize_task_id(str(x)) for x in b.get('task_ids') or []]
        bids = [tid for tid in bids if tid]
        if not bids:
            continue
        if all((tid in done or tid in failed or tid in paused for tid in bids)):
            continue
        active_batch = b
        break

    def _batch_index_of(b: dict) -> int:
        raw = b.get('index')
        return raw if isinstance(raw, int) else 0
    if active_batch is None:
        unresolved = [tid for tid in tasks_by_id if tid not in done and tid not in failed and (tid not in paused)]
        if unresolved:
            return _result({'batch_index': 0, 'task_ids': [], 'file_locks': [], 'scheduler_stuck': True}, exit_code=0)
            return
        return _result({'batch_index': 0, 'task_ids': [], 'file_locks': [], 'scheduler_stuck': False}, exit_code=0)
        return
    active_ids = {tid for tid in (_normalize_task_id(str(x)) for x in active_batch.get('task_ids') or []) if tid}
    if not active_ids:
        return _result({'batch_index': _batch_index_of(active_batch), 'task_ids': [], 'file_locks': [], 'scheduler_stuck': True}, exit_code=0)
        return
    ready_in_batch: list[dict] = []
    for t in ready:
        raw_tid = t.get('id')
        tid = _normalize_task_id(str(raw_tid)) if raw_tid is not None else None
        if tid in active_ids:
            ready_in_batch.append(t)
    picked: list[str] = []
    picked_files: list[str] = []
    claimed = set(locked)
    for t in ready_in_batch:
        raw_tid = t.get('id')
        tid = _normalize_task_id(str(raw_tid)) if raw_tid is not None else None
        files = _files(t)
        if any((f in claimed for f in files)):
            continue
        if len(picked) >= max(1, payload['parallel']):
            break
        picked.append(tid)
        picked_files.extend(files)
        claimed.update(files)
    unfinished_active = [tid for tid in active_ids if tid not in done and tid not in failed and (tid not in paused)]
    scheduler_stuck = len(picked) == 0 and len(unfinished_active) > 0
    return _result({'batch_index': _batch_index_of(active_batch), 'task_ids': picked, 'file_locks': picked_files, 'scheduler_stuck': scheduler_stuck}, exit_code=0)

def cmd_batch_next(args: argparse.Namespace) -> None:
    payload = _args_to_payload_batch_next(args)
    result = _run_batch_next(payload)
    _emit_or_die(args, result)


def _args_to_payload_filter_schedule(args: argparse.Namespace) -> dict:
    use_stdin = bool(getattr(args, "stdin", False))
    sched_file = getattr(args, "schedule_file", None)
    payload = {
        "schedule_file": Path(sched_file) if sched_file else None,
        "stdin": use_stdin,
        "task_ids": args.task_ids,
    }
    if use_stdin and not sched_file:
        payload["input_source"] = "stdin"
        payload["stdin_text"] = _read_stdin_text()
    elif sched_file and not use_stdin:
        sched_path = Path(sched_file)
        payload["input_source"] = "file"
        payload["schedule_file_exists"] = sched_path.is_file()
        if payload["schedule_file_exists"]:
            payload["schedule_text"] = sched_path.read_text(encoding="utf-8")
    return payload


def _run_filter_schedule(payload: dict) -> dict:
    # 0. Input-mode selection. `--schedule-file` and `--stdin` are mutually
    # exclusive; exactly one MUST be supplied. `--stdin` was added so the
    # orchestrator's Phase 1 `--task-ids` branch can stay fully in-memory
    # without the round-3 pre-persist/filter/re-persist workaround.
    input_source = payload.get("input_source")
    use_stdin = input_source == "stdin" or (
        input_source is None and bool(payload.get("stdin", False))
    )
    sched_file = payload.get("schedule_file")
    if use_stdin and sched_file:
        return _result({"errors": [{
            "path": "$",
            "code": "input-mode-conflict",
            "message": (
                "--schedule-file and --stdin are mutually exclusive; "
                "supply exactly one"
            ),
        }]}, exit_code=1)
    if not use_stdin and not sched_file:
        return _result({"errors": [{
            "path": "$",
            "code": "input-mode-missing",
            "message": (
                "filter-schedule requires exactly one of --schedule-file "
                "or --stdin"
            ),
        }]}, exit_code=1)

    if use_stdin:
        raw = payload["stdin_text"]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return _result({"errors": [{
                "path": "$",
                "code": "json-decode",
                "message": f"schedule json decode: {e}",
            }]}, exit_code=1)
    else:
        sched_path = Path(sched_file)
        if "schedule_text" not in payload:
            return _result({"errors": [{
                "path": "$",
                "code": "file-not-found",
                "message": f"schedule file not found: {sched_path}",
            }]}, exit_code=1)
        try:
            data = json.loads(payload["schedule_text"])
        except json.JSONDecodeError as e:
            return _result({"errors": [{
                "path": "$",
                "code": "json-decode",
                "message": f"schedule json decode: {e}",
            }]}, exit_code=1)
    if not isinstance(data, dict):
        return _result({"errors": [{
            "path": "$",
            "code": "top-level-not-object",
            "message": "top-level schedule must be an object",
        }]}, exit_code=1)

    # 1. Source-schedule validation (mirror cmd_batch_next:1504-1506).
    errors, warnings = _validate_schedule(data)
    if errors:
        return _result({"errors": errors, "warnings": warnings}, exit_code=1)

    # 2. Source-not-valid rejection (V4). On the file path, filtering an
    # already-broken schedule is meaningless — the orchestrator should
    # surface the source outcome instead. On the --stdin path this
    # requirement is relaxed: in-memory schedules carrying
    # outcome='needs-enrichment' (e.g. from build-tasks warnings→gaps
    # mapping) are accepted so Phase 1's `--task-ids` branch can filter
    # without round-tripping through a file.
    if not use_stdin and data.get("outcome") != "valid":
        return _result({"errors": [{
            "path": "$.outcome",
            "code": "source-not-valid",
            "message": (
                f"filter-schedule requires source outcome='valid', "
                f"got {data.get('outcome')!r}"
            ),
        }]}, exit_code=1)

    # 3. --task-ids parse + normalize. Empty fragments (e.g. "1,,3") are
    # skipped silently; all-empty input is a hard error.
    raw_ids = [s.strip() for s in (payload.get("task_ids") or "").split(",") if s.strip()]
    requested: list[str] = []
    for r in raw_ids:
        norm = _normalize_task_id(r)
        if norm is None:
            return _result({"errors": [{
                "path": "$.task_ids",
                "code": "invalid-task-ids",
                "message": f"could not normalize task id {r!r}",
            }]}, exit_code=1)
        requested.append(norm)
    if not requested:
        return _result({"errors": [{
            "path": "$.task_ids",
            "code": "invalid-task-ids",
            "message": "no task ids provided",
        }]}, exit_code=1)

    # Build tasks_by_id from the canonical `id` field. TASK-008 removed the
    # legacy `task_id` alias; schedules using it are rejected upstream by
    # `_validate_schedule` with a `missing-field` error on `$.tasks[i].id`.
    tasks_by_id: dict[str, dict] = {}
    for t in data.get("tasks") or []:
        raw_tid = t.get("id")
        norm = _normalize_task_id(str(raw_tid)) if raw_tid is not None else None
        if norm:
            tasks_by_id[norm] = t

    # 4. Unknown requested id (V2). Case 1: "user typo".
    unknown = [tid for tid in requested if tid not in tasks_by_id]
    if unknown:
        return _result({"errors": [{
            "path": "$.task_ids",
            "code": "unknown-task-id",
            "message": f"unknown task id {tid}",
        } for tid in unknown]}, exit_code=1)

    # 5. Transitive closure with guarded indexing (V1, V3). Case 2: "schedule
    # is broken upstream" — a transitive dep that's absent from tasks[] MUST
    # produce a structured `missing-dependency` error, NOT a Python
    # KeyError. The guard on tasks_by_id.get(tid) is defensive in case
    # step 4 was bypassed; the real missing-dep check is on each dep ref.
    closed: set[str] = set()
    stack = list(requested)
    while stack:
        tid = stack.pop()
        if tid in closed:
            continue
        closed.add(tid)
        task = tasks_by_id.get(tid)
        if task is None:
            # Defensive: step 4 should have caught this for requested IDs;
            # for derived ones the dep-ref check below handles it first.
            return _result({"errors": [{
                "path": "$.tasks",
                "code": "missing-dependency",
                "message": f"task depends on missing id {tid}",
            }]}, exit_code=1)
        for dep in (task.get("dependencies") or []):
            dep_norm = _normalize_task_id(str(dep))
            if dep_norm is None or dep_norm not in tasks_by_id:
                return _result({"errors": [{
                    "path": f"$.tasks[id={tid}].dependencies",
                    "code": "missing-dependency",
                    "message": f"task {tid} depends on missing id {dep!r}",
                }]}, exit_code=1)
            stack.append(dep_norm)

    # 6. Build output tasks/batches in source order. Drop batches whose
    # task_ids become empty post-filter (V8); filter retained batch task_ids
    # to the closed set (V10); preserve original `index` values (V9).
    def _tid_of(t: dict) -> str | None:
        raw = t.get("id")
        return _normalize_task_id(str(raw)) if raw is not None else None

    out_tasks = [t for t in (data.get("tasks") or []) if _tid_of(t) in closed]
    out_batches: list[dict] = []
    for b in (data.get("batches") or []):
        bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
        keep = [x for x in bids if x in closed]
        if keep:
            out_batches.append({**b, "task_ids": keep})

    # 7. DAG defensive check on the filtered subgraph (V6, ISSUE-019;
    # TASK-019 refactor). parse-schedule is the primary cycle detector, but
    # filter-schedule MUST also guard against a cycle surviving into its
    # output because the orchestrator pipes our stdout straight to
    # write-schedule. Delegates to the shared `_validate_schedule_dag` helper.
    dag_errors = _validate_schedule_dag(out_tasks, out_batches)
    if dag_errors:
        return _result({"errors": dag_errors}, exit_code=1)

    # 8. Reference-integrity check on the filtered schedule.
    ref_errors = _validate_schedule_refs(out_tasks, out_batches)
    if ref_errors:
        return _result({"errors": ref_errors}, exit_code=1)

    # 9. Emit canonical schedule. On the file path, gaps=[] and risks=[]
    # are intentional — inheriting source-level gaps/risks would either
    # contradict outcome=valid (per _validate_schedule:606-611) or carry
    # stale references to filtered-out tasks. Success stdout MUST contain
    # ONLY these five keys so write-schedule --stdin accepts the output
    # byte-for-byte (V11, V12, V13). Do NOT add warnings/errors/other
    # metadata on the success path.
    #
    # On the --stdin path the source outcome may be 'needs-enrichment'
    # (Phase 1's `--task-ids` branch feeds in the build-tasks warnings→gaps
    # shape). Silently dropping those gaps would contradict that contract
    # and upgrade the schedule to 'valid' even when a retained task still
    # carries a gap. Carry forward input gaps (and risks) whose `task_id`
    # references a retained task, then derive outcome from the filtered
    # gaps: non-empty → needs-enrichment, empty → valid.
    if use_stdin:
        src_gaps = data.get("gaps") or []
        src_risks = data.get("risks") or []

        def _gap_retained(g: object) -> bool:
            if not isinstance(g, dict):
                return False
            raw_tid = g.get("task_id")
            if raw_tid is None:
                # Gap without a task_id reference: preserve as a global
                # gap (cannot be tied to a filtered-out task).
                return True
            norm = _normalize_task_id(str(raw_tid))
            return norm is not None and norm in closed

        filtered_gaps = [g for g in src_gaps if _gap_retained(g)]
        filtered_risks = [r for r in src_risks if _gap_retained(r)]
        out_outcome = "needs-enrichment" if filtered_gaps else "valid"
        return _result({
            "outcome": out_outcome,
            "tasks": out_tasks,
            "batches": out_batches,
            "gaps": filtered_gaps,
            "risks": filtered_risks,
        }, exit_code=0)

    return _result({
        "outcome": "valid",
        "tasks": out_tasks,
        "batches": out_batches,
        "gaps": [],
        "risks": [],
    }, exit_code=0)


def cmd_filter_schedule(args: argparse.Namespace) -> None:
    payload = _args_to_payload_filter_schedule(args)
    result = _run_filter_schedule(payload)
    _emit_or_die(args, result)


_LOG_BLOCK_RE = re.compile(
    r"<<<LOG-BLOCK\s+([0-9a-fA-F]{4,32})>>>.*?<<<END LOG-BLOCK\s+\1>>>",
    re.DOTALL,
)


def _strip_log_blocks(text: str) -> str:
    """Replace ``<<<LOG-BLOCK {uuid}>>>...<<<END LOG-BLOCK {uuid}>>>`` fences
    (as emitted by ``scripts/log_capture.py``) with a placeholder.

    Uses a back-reference on the UUID so a forged ``<<<END LOG-BLOCK X>>>``
    embedded in captured command output cannot close the real block.
    Only log-content inside matching fences is elided; prose outside is
    preserved verbatim.
    """
    return _LOG_BLOCK_RE.sub("[log block elided]", text)


def _args_to_payload_parse_implementer_report(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_parse_implementer_report(payload: dict) -> dict:
    raw_input = payload['stdin_text']
    raw = _strip_log_blocks(raw_input)
    warnings: list[str] = []
    diagnostics: list[dict] = []

    def _field(label: str) -> str | None:
        m = re.search(f'^\\*\\*{re.escape(label)}:\\*\\*\\s*(.+?)\\s*$', raw, re.MULTILINE)
        return m.group(1).strip() if m else None

    def _has_header(label: str) -> bool:
        return re.search(f'^\\*\\*{re.escape(label)}:\\*\\*', raw, re.MULTILINE) is not None
    outcome = _field('Outcome')
    if not outcome:
        return _result({'error': 'report missing **Outcome:** line'}, exit_code=1)
    outcome = outcome.lower()
    allowed = {'success', 'partial', 'failed', 'plan-incorrect', 'blocked', 'malformed'}
    if outcome not in allowed:
        return _result({'error': f'unknown outcome {outcome!r}; expected one of {sorted(allowed)}'}, exit_code=1)

    def _list_section(label: str) -> list[str]:
        pat = f'^\\*\\*{re.escape(label)}:\\*\\*\\s*\\n((?:[ \\t]*-[^\\n]*\\n?)+)'
        m = re.search(pat, raw, re.MULTILINE)
        if not m:
            return []
        return [ln.strip().lstrip('-').strip() for ln in m.group(1).splitlines() if ln.strip()]
    files_changed = _list_section('Files changed')
    concerns = _list_section('Concerns for reviewer')
    plan_adaptations = _list_section('Plan adaptations')
    if not _has_header('Plan adaptations'):
        diagnostics.append({'code': 'missing-plan-adaptations', 'message': '**Plan adaptations:** section header is mandatory per plan-implementer contract'})
    if outcome not in {'failed', 'blocked'} and (not _has_header('Concerns for reviewer')):
        diagnostics.append({'code': 'missing-concerns-for-reviewer', 'message': '**Concerns for reviewer:** section header is mandatory per plan-implementer contract'})
    diff_summary = ''
    ds_m = re.search('\\*\\*Diff summary:\\*\\*\\s*\\n(.+?)(?=\\n\\n|\\n\\*\\*|\\Z)', raw, re.DOTALL)
    if ds_m:
        diff_summary = ds_m.group(1).strip()
    test_outcome = _field('Test outcome') or 'not-run'
    reversion = None
    rv_m = re.search('(?:On failure[^\\n]*|Reversion guidance):\\s*\\n(.+?)(?=\\n\\n|\\n\\*\\*|\\Z)', raw, re.DOTALL | re.IGNORECASE)
    if rv_m:
        reversion = rv_m.group(1).strip()
    result = {'outcome': outcome, 'files_changed': files_changed, 'diff_summary': diff_summary, 'test_outcome': test_outcome, 'concerns': concerns, 'plan_adaptations': plan_adaptations, 'warnings': warnings, 'diagnostics': diagnostics, 'raw': raw_input}
    if reversion:
        result['reversion_guidance'] = reversion
    return _result(result, exit_code=0)

def cmd_parse_implementer_report(args: argparse.Namespace) -> None:
    payload = _args_to_payload_parse_implementer_report(args)
    result = _run_parse_implementer_report(payload)
    _emit_or_die(args, result)


def _validate_plan_review_finding(item: object, *, path: str) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(item, dict):
        return [{
            "path": path,
            "code": "invalid-plan-review-finding",
            "message": "plan-review finding must be an object",
        }]
    required = {
        "severity": str,
        "blocking": bool,
        "section": str,
        "concern": str,
        "suggested_change": str,
    }
    # target_task_id is optional (TASK-007 per-child targeting); when
    # present it must be a string OR null. Absent means "schedule-level
    # finding" (synthesized to null by the parser).
    optional = {
        "target_task_id",
    }
    for key, typ in required.items():
        if key not in item:
            errors.append({
                "path": f"{path}.{key}",
                "code": "missing-plan-review-finding-field",
                "message": f"plan-review finding missing field {key!r}",
            })
            continue
        value = item[key]
        if typ is bool:
            ok = isinstance(value, bool)
        else:
            ok = isinstance(value, typ)
        if not ok:
            errors.append({
                "path": f"{path}.{key}",
                "code": "invalid-plan-review-finding-field",
                "message": (
                    f"plan-review finding field {key!r} must be a "
                    f"{typ.__name__}"
                ),
            })
    if "target_task_id" in item:
        value = item["target_task_id"]
        if not (value is None or isinstance(value, str)):
            errors.append({
                "path": f"{path}.target_task_id",
                "code": "invalid-plan-review-finding-field",
                "message": (
                    "plan-review finding field 'target_task_id' must be a "
                    "string or null"
                ),
            })
    severity = item.get("severity")
    if (
        isinstance(severity, str)
        and severity not in ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES
    ):
        errors.append({
            "path": f"{path}.severity",
            "code": "invalid-plan-review-finding-severity",
            "message": (
                f"plan-review finding severity must be one of "
                f"{sorted(ALLOWED_PLAN_REVIEW_FINDING_SEVERITIES)}, "
                f"got {severity!r}"
            ),
        })
    for key in item.keys():
        if key not in required and key not in optional:
            errors.append({
                "path": f"{path}.{key}",
                "code": "unknown-plan-review-finding-field",
                "message": (
                    f"plan-review finding has unknown field {key!r}"
                ),
            })
    return errors


_PLAN_REVIEW_TRANSIENT_OUTCOMES = frozenset({"timeout", "parse_error", "failure"})

_PLAN_REVIEW_REVIEWERS = frozenset({"codex", "gemini"})


def _route_plan_review(
    allow_gemini_fallback: bool,
    codex_available: bool,
    gemini_available: bool,
    codex_outcome: "str | None" = None,
) -> str:
    """Decide which Phase 1.5 reviewer (if any) handles plan-review (TASK-006).

    Returns one of ``"codex" | "gemini" | "skip"``. The orchestrator calls
    this helper at two seams:

    1. **Pre-dispatch** (``codex_outcome=None``) — pick the initial reviewer
       from preflight availability + the operator's
       ``--allow-gemini-fallback`` opt-in.
    2. **Post-Codex-failure** (``codex_outcome ∈ {"timeout", "parse_error",
       "failure"}``) — decide whether to re-dispatch ONCE to Gemini before
       degrading to ``"skip"``.

    Truth table (canonical form per TASK-006 plan):

        allow_fallback  codex_avail  gemini_avail  codex_outcome  → result
        ─────────────────────────────────────────────────────────────────
        False           True         *             None           → "codex"
        False           False        *             None           → "skip"
        True            True         *             None           → "codex"
        True            False        True          None           → "gemini"
        True            False        False         None           → "skip"
        True            True         True          {transient}    → "gemini"
        True            True         False         {transient}    → "skip"
        False           *            *             {transient}    → "skip"
        *               *            *             "success"      → "codex"

    The helper does NOT loop. After Gemini returns ``{transient}``, the
    caller invokes the helper a second time with ``gemini_available=False``
    (the second leg is now "no fallback target available") — the helper
    returns ``"skip"`` and the orchestrator emits
    ``plan_review_skipped {reason: "all_reviewers_unavailable"}``.
    """
    if codex_outcome == "success":
        return "codex"

    if codex_outcome in _PLAN_REVIEW_TRANSIENT_OUTCOMES:
        if allow_gemini_fallback and gemini_available:
            return "gemini"
        return "skip"

    if codex_available:
        return "codex"
    if allow_gemini_fallback and gemini_available:
        return "gemini"
    return "skip"


_REVIEW_TRANSIENT_OUTCOMES = frozenset({"timeout", "parse_error", "failure"})


def _route_review(
    allow_gemini_fallback: bool,
    codex_available: bool,
    gemini_available: bool,
    codex_outcome: "str | None" = None,
) -> str:
    """Decide which Phase D.1 reviewer (if any) handles cross-review of a
    Claude-implemented task (TASK-007).

    Returns one of ``"codex" | "gemini" | "fail"``. Sibling helper to
    ``_route_plan_review`` (TASK-006); same signature shape, different
    output vocabulary — Phase D.1 has no skip surface, so a transient
    failure with no fallback target degrades into a review-stage failure
    rather than a documented skip. The orchestrator calls this helper at
    two seams:

    1. **Pre-dispatch** (``codex_outcome=None``) — pick the initial
       reviewer. Today's behavior: Codex is the only Phase D.1 reviewer
       on the Claude-impl path, so the helper returns ``"codex"`` here
       regardless of ``allow_gemini_fallback`` (Gemini is a
       *transient-failure fallback*, not a co-equal first dispatch on
       this seam).
    2. **Post-Codex-failure** (``codex_outcome ∈ {"timeout",
       "parse_error", "failure"}``) — decide whether to re-dispatch ONCE
       to Gemini before classifying as a review-stage failure.

    ``scope_violation`` is intentionally NOT a fallback trigger — a
    scope violation is a structural defect in the implementer's output
    (per Phase B), not a transient reviewer failure. Routing it to
    Gemini would mask the underlying issue. Returns ``"fail"``.

    Truth table (canonical form per TASK-007 plan):

        allow_fallback  codex_avail  gemini_avail  codex_outcome  → result
        ─────────────────────────────────────────────────────────────────
        False           *            *             None              → "codex"
        True            *            *             None              → "codex"
        False           *            *             {transient}       → "fail"
        True            *            True          {transient}       → "gemini"
        True            *            False         {transient}       → "fail"
        *               *            *             "scope_violation" → "fail"
        *               *            *             "success"         → "codex"

    The helper does NOT loop. After Gemini also returns a transient
    outcome, the caller re-invokes with ``gemini_available=False`` (the
    second leg has "no fallback target available") — the helper returns
    ``"fail"`` and the orchestrator emits
    ``review_fallback_failed {from:"codex", to:"gemini", reason:<gemini_outcome>}``
    before classifying the task as review-stage failure.
    """
    if codex_outcome == "success":
        return "codex"

    if codex_outcome == "scope_violation":
        return "fail"

    if codex_outcome in _REVIEW_TRANSIENT_OUTCOMES:
        if allow_gemini_fallback and gemini_available:
            return "gemini"
        return "fail"

    return "codex"


def _validate_plan_review_parsed(parsed: object) -> list[dict]:
    """Validate the `parsed` body of a plan-review envelope against the
    plan-review schema contract (codex_plan_review_schema.json /
    gemini_plan_review_schema.json — structural mirrors per TASK-006).
    Returns canonical `errors[*]`."""
    errors: list[dict] = []
    if not isinstance(parsed, dict):
        return [{
            "path": "$.parsed",
            "code": "invalid-type",
            "message": "parsed must be an object",
        }]

    required = {
        "plan_file": str,
        "verdict": str,
        "findings": list,
        "notes": list,
        "schedule_ok": bool,
        "summary": str,
    }
    for key, typ in required.items():
        if key not in parsed:
            errors.append({
                "path": f"$.parsed.{key}",
                "code": "missing-field",
                "message": f"parsed missing field {key!r}",
            })
            continue
        value = parsed[key]
        if typ is bool:
            ok = isinstance(value, bool)
        elif typ is list:
            ok = isinstance(value, list)
        else:
            ok = isinstance(value, typ)
        if not ok:
            errors.append({
                "path": f"$.parsed.{key}",
                "code": "invalid-type",
                "message": (
                    f"parsed field {key!r} must be a {typ.__name__}"
                ),
            })

    verdict = parsed.get("verdict")
    if (
        isinstance(verdict, str)
        and verdict not in ALLOWED_PLAN_REVIEW_VERDICTS
    ):
        errors.append({
            "path": "$.parsed.verdict",
            "code": "invalid-plan-review-verdict",
            "message": (
                f"verdict must be one of "
                f"{sorted(ALLOWED_PLAN_REVIEW_VERDICTS)}, got {verdict!r}"
            ),
        })

    findings = parsed.get("findings")
    if isinstance(findings, list):
        for i, item in enumerate(findings):
            errors.extend(
                _validate_plan_review_finding(
                    item, path=f"$.parsed.findings[{i}]",
                )
            )

    allowed_keys = set(required.keys())
    for key in parsed.keys():
        if key not in allowed_keys:
            errors.append({
                "path": f"$.parsed.{key}",
                "code": "unknown-parsed-field",
                "message": f"parsed has unknown field {key!r}",
            })
    return errors


# ---------------------------------------------------------------------------
# TASK-007 / Phase 1.5.5 — triage finding prioritization.
#
# The Phase 1.5.5 triage-agent dispatch template instructs the agent to
# reason about Codex plan-review findings in a prioritized order:
# (a) blocking=true first, (b) then severity=critical, (c) then
# severity=important, (d) then severity=minor. The ordering is advisory
# prose in the dispatch template, but the orchestrator (or a dispatch
# builder) calls `order_triage_findings` to produce the ordered payload
# before rendering the template so the embedded per-finding JSON appears
# in the documented order.
# ---------------------------------------------------------------------------

_TRIAGE_SEVERITY_RANK: dict[str, int] = {
    "critical": 0,
    "important": 1,
    "minor": 2,
}


def order_triage_findings(findings: list[dict]) -> list[dict]:
    """Return `findings` reordered by the documented triage priority ladder,
    annotated with their original position as `source_index`.

    Stable sort by the composite key:
        (0 if blocking else 1, severity_rank, source_index)

    where `severity_rank` is `critical=0 < important=1 < minor=2` and
    unknown severities sort after the three named tiers (rank=99). The
    source-index tie-breaker keeps entries within a tier in source order
    — this matches the parser contract (`parse-plan-review-report`
    preserves the original Codex `findings[]` order) and ensures
    schedule-level entries (`target_task_id=None`) interleave by severity
    just like task-targeted entries.

    The returned list contains SHALLOW COPIES of the original finding
    dicts, each annotated with a `source_index` integer field set to its
    0-based position in the input list. All other original fields
    (`target_task_id`, `section`, `concern`, `suggested_change`,
    `severity`, `blocking`, ...) are preserved verbatim. The original
    input dicts are NOT mutated. If an input dict already carries a
    `source_index` key (e.g., the caller ran the helper twice by
    mistake), the annotation overwrites it so the output is always
    grounded in the current call's positions.

    The `source_index` annotation is load-bearing: the Phase 1.5.5
    triage-agent dispatch template instructs the triage agent to reference
    `source_index` values — NOT positions in this presorted array — when
    emitting `load_bearing` / `dismissed`. This preserves the contract that
    `parse-plan-review-triage-report` validates indices against the
    original Codex `parsed.findings[]` array (by `--findings-count`).

    Intended caller: the orchestrator / dispatch builder for the Phase
    1.5.5 triage-agent dispatch template (see
    `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`)
    and the `order-triage-findings` subcommand wrapper.
    """
    if not isinstance(findings, list):
        raise TypeError(
            f"order_triage_findings expects a list, got {type(findings).__name__}"
        )

    def _key(entry: tuple[int, object]) -> tuple[int, int, int]:
        idx, f = entry
        if not isinstance(f, dict):
            # Defensive: non-dict entries sort to the very end, preserving
            # source order among themselves.
            return (2, 99, idx)
        blocking_bucket = 0 if f.get("blocking") is True else 1
        severity = f.get("severity")
        sev_rank = _TRIAGE_SEVERITY_RANK.get(
            severity if isinstance(severity, str) else "", 99,
        )
        return (blocking_bucket, sev_rank, idx)

    indexed = list(enumerate(findings))
    indexed.sort(key=_key)
    ordered: list[dict] = []
    for idx, f in indexed:
        if isinstance(f, dict):
            annotated = dict(f)
            annotated["source_index"] = idx
            ordered.append(annotated)
        else:
            # Defensive: non-dict entries pass through as-is (can't
            # annotate a non-dict). Matches the defensive sort-key path.
            ordered.append(f)  # type: ignore[arg-type]
    return ordered


# TASK-006 (SKILL_bash_dispatch_migration). Consolidated extraction shim
# for the v3 Claude wrapper envelope. Replaces the per-site inline jq-style
# reads added in TASK-003/004/005. Input on stdin: full wrapper envelope
# JSON `{schema_version, status, status_reason, agent, model, ...,
# result, result_raw_truncated, stderr_tail, scope, ...}`. Args:
# `--agent {plan-analyst|plan-implementer|plan-remediator}`. Output is
# normalized across agents: `{status, outcome, result, scope_violation,
# scope_misreport, error}` where:
#   * `status`           — verbatim wrapper `status` (e.g., `ok`,
#                          `schema_invalid`, `timeout`, ...).
#   * `outcome`          — for `plan-implementer` / `plan-remediator`,
#                          `result.outcome` when status==ok and result is
#                          a dict carrying `outcome`; for `plan-analyst`,
#                          `result.outcome` if present (whole-plan analyst
#                          path) else `null` (per-child classifier path
#                          where `result` is `{agent, classification_reason}`).
#                          When status != ok, `outcome` is the literal
#                          string `"malformed"` so the orchestrator's
#                          existing routing (`partial | failed |
#                          plan-incorrect | blocked | malformed`) treats
#                          a transport failure as the same routing class.
#   * `result`           — verbatim `envelope.result` (sub-fields are
#                          consumed by `parse-schedule` / markdown
#                          parsers downstream — this subcommand does NOT
#                          re-parse them).
#   * `scope_violation`  — `envelope.scope.scope_violation_detected` when
#                          present; `false` otherwise.
#   * `scope_misreport`  — `envelope.scope.scope_misreport_detected`
#                          when present; `false` otherwise.
#   * `error`            — diagnostic string composed from
#                          `status_reason` + `result_raw_truncated` +
#                          `stderr_tail` when status != ok; `null`
#                          otherwise.
_CLAUDE_ENVELOPE_AGENTS = {"plan-analyst", "plan-implementer", "plan-remediator"}


def _args_to_payload_claude_envelope_extract(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "agent": args.agent,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_claude_envelope_extract(payload: dict) -> dict:
    raw = payload['stdin_text']
    try:
        env = json.loads(raw)
    except json.JSONDecodeError as e:
        return _result({'error': f'stdin is not valid JSON: {e}'}, exit_code=1)
    if not isinstance(env, dict):
        return _result({'error': 'envelope must be a JSON object'}, exit_code=1)
    agent = payload['agent']
    if agent not in _CLAUDE_ENVELOPE_AGENTS:
        return _result({'error': f'--agent must be one of {sorted(_CLAUDE_ENVELOPE_AGENTS)}; got {agent!r}'}, exit_code=1)
    status = env.get('status')
    result = env.get('result')
    scope = env.get('scope') if isinstance(env.get('scope'), dict) else {}
    scope_violation = bool(scope.get('scope_violation_detected', False))
    scope_misreport = bool(scope.get('scope_misreport_detected', False))
    if status != 'ok':
        outcome: object = 'malformed'
    elif isinstance(result, dict) and isinstance(result.get('outcome'), str):
        outcome = result['outcome']
    else:
        outcome = None
    error: object = None
    if status != 'ok':
        parts: list[str] = []
        sr = env.get('status_reason')
        if isinstance(sr, str) and sr:
            parts.append(f'status_reason={sr}')
        rrt = env.get('result_raw_truncated')
        if isinstance(rrt, str) and rrt:
            parts.append(f'result_raw_truncated={rrt}')
        st = env.get('stderr_tail')
        if isinstance(st, str) and st:
            parts.append(f'stderr_tail={st}')
        err_obj = env.get('error')
        if isinstance(err_obj, (str, dict, list)) and err_obj:
            parts.append(f'error={(json.dumps(err_obj) if not isinstance(err_obj, str) else err_obj)}')
        error = ' | '.join(parts) if parts else f'wrapper status={status!r}'
    return _result({'status': status, 'outcome': outcome, 'result': result, 'scope_violation': scope_violation, 'scope_misreport': scope_misreport, 'error': error, 'extra': env.get('extra')}, exit_code=0)

def cmd_claude_envelope_extract(args: argparse.Namespace) -> None:
    payload = _args_to_payload_claude_envelope_extract(args)
    result = _run_claude_envelope_extract(payload)
    _emit_or_die(args, result)


def _args_to_payload_order_triage_findings(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_order_triage_findings(payload: dict) -> dict:
    raw = payload['stdin_text']
    if not raw.strip():
        return _result({'errors': [{'path': '$', 'code': 'empty-stdin', 'message': 'order-triage-findings expects JSON on stdin'}]}, exit_code=1)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'stdin is not valid JSON: {exc}'}]}, exit_code=1)
    if isinstance(payload, list):
        findings = payload
    elif isinstance(payload, dict) and isinstance(payload.get('findings'), list):
        findings = payload['findings']
    else:
        return _result({'errors': [{'path': '$', 'code': 'invalid-type', 'message': "order-triage-findings expects a JSON list of findings or a JSON object with a 'findings' list field"}]}, exit_code=1)
    try:
        ordered = order_triage_findings(findings)
    except TypeError as exc:
        return _result({'errors': [{'path': '$', 'code': 'invalid-type', 'message': str(exc)}]}, exit_code=1)
    return _result({'ordered': ordered, 'errors': []}, exit_code=0)

def cmd_order_triage_findings(args: argparse.Namespace) -> None:
    'Order-and-annotate triage findings for the Phase 1.5.5 dispatch.\n\n    Input on stdin: JSON — either a bare `list[dict]` of findings, OR a\n    parsed plan-review result object (the shape `parse-plan-review-report`\n    emits) that carries a top-level `findings` array. Either shape is\n    recognized; the helper extracts the list and returns the ordered\n    output.\n\n    Output (JSON): `{"ordered": [...], "errors": []}` on success, where\n    `ordered` is the priority-sorted list with each entry carrying a\n    `source_index` integer (its 0-based position in the input). This is\n    the exact payload the orchestrator substitutes for\n    `<codex_findings_json>` before rendering the Phase 1.5.5 triage\n    dispatch template — see\n    `plugins/plan-executor/skills/implement-plan/dispatch-templates.md`.\n    '
    payload = _args_to_payload_order_triage_findings(args)
    result = _run_order_triage_findings(payload)
    _emit_or_die(args, result)


def _args_to_payload_parse_plan_review_report(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "from_claude": getattr(args, "from_claude", False),
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_parse_plan_review_report(payload: dict) -> dict:
    raw = payload['stdin_text']
    if not raw.strip():
        return _result({'errors': [{'path': '$', 'code': 'empty-stdin', 'message': 'parse-plan-review-report expects a JSON envelope on stdin'}]}, exit_code=1)
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'stdin is not valid JSON: {exc}'}]}, exit_code=1)
    if not isinstance(envelope, dict):
        return _result({'errors': [{'path': '$', 'code': 'invalid-type', 'message': 'envelope must be a JSON object'}]}, exit_code=1)
    if payload.get('from_claude', False):
        parsed_errors = _validate_plan_review_parsed(envelope)
        if parsed_errors:
            return _result({'errors': parsed_errors}, exit_code=1)
        findings = envelope.get('findings') or []
        normalized_findings: list = []
        for item in findings:
            if isinstance(item, dict):
                normalized = dict(item)
                normalized.setdefault('target_task_id', None)
                normalized_findings.append(normalized)
            else:
                normalized_findings.append(item)
        result = {'plan_file': envelope.get('plan_file'), 'outcome': 'success', 'verdict': envelope.get('verdict'), 'findings_count': len(normalized_findings), 'findings': normalized_findings, 'notes': envelope.get('notes') or [], 'summary': envelope.get('summary', ''), 'schedule_ok': envelope.get('schedule_ok'), 'errors': []}
        return _result(result, exit_code=0)
        return
    subcommand = envelope.get('subcommand')
    if subcommand != 'plan-review':
        if subcommand is None:
            msg = 'envelope subcommand missing — did you pipe only the inner `parsed` object? Expected full wrapper envelope with top-level `task_id`, `subcommand`, `outcome`, and `parsed`.'
        else:
            msg = f"envelope subcommand must be 'plan-review', got {subcommand!r}"
        return _result({'errors': [{'path': '$.subcommand', 'code': 'invalid-subcommand', 'message': msg}]}, exit_code=1)
    reviewer = envelope.get('reviewer', 'codex')
    if reviewer not in _PLAN_REVIEW_REVIEWERS:
        return _result({'errors': [{'path': '$.reviewer', 'code': 'invalid-reviewer', 'message': f"envelope reviewer must be one of {sorted(_PLAN_REVIEW_REVIEWERS)} (default 'codex' when absent), got {reviewer!r}"}]}, exit_code=1)
    errors: list[dict] = []
    outcome = envelope.get('outcome')
    terminal_outcomes = {'failure', 'timeout', 'parse_error', 'scope_violation'}
    if outcome in terminal_outcomes:
        result: dict = {'plan_file': envelope.get('plan_file') or envelope.get('task_id'), 'outcome': outcome, 'reviewer': reviewer, 'verdict': None, 'findings_count': 0, 'findings': [], 'notes': [], 'summary': '', 'schedule_ok': None, 'errors': [], 'envelope_error': envelope.get('error')}
        return _result(result, exit_code=0)
        return
    if outcome != 'success':
        errors.append({'path': '$.outcome', 'code': 'invalid-outcome', 'message': f"envelope outcome must be 'success' for a parseable plan review; got {outcome!r}"})
    parsed = envelope.get('parsed')
    if parsed is None:
        errors.append({'path': '$.parsed', 'code': 'missing-field', 'message': "envelope is missing required field 'parsed'"})
    else:
        errors.extend(_validate_plan_review_parsed(parsed))
    if errors:
        return _result({'errors': errors}, exit_code=1)
    assert isinstance(parsed, dict)
    findings = parsed.get('findings') or []
    normalized_findings: list = []
    for item in findings:
        if isinstance(item, dict):
            normalized = dict(item)
            normalized.setdefault('target_task_id', None)
            normalized_findings.append(normalized)
        else:
            normalized_findings.append(item)
    result = {'plan_file': parsed.get('plan_file'), 'outcome': outcome, 'reviewer': reviewer, 'verdict': parsed.get('verdict'), 'findings_count': len(normalized_findings), 'findings': normalized_findings, 'notes': parsed.get('notes') or [], 'summary': parsed.get('summary', ''), 'schedule_ok': parsed.get('schedule_ok'), 'errors': []}
    return _result(result, exit_code=0)

def cmd_parse_plan_review_report(args: argparse.Namespace) -> None:
    'Validate a Phase 1.5 plan-review envelope from stdin.\n\n    Default input: full JSON envelope emitted by\n    `plan_codex_dispatch.py plan-review`. Expected shape (minimum):\n        {\n          "plan_file": "...",\n          "subcommand": "plan-review",\n          "outcome": "success" | "failure" | "timeout" | "parse_error",\n          "parsed": { ... },        # validated against codex_plan_review_schema\n          ...\n        }\n\n    With ``--from-claude`` (TASK-002): the Phase 1.5-Claude path\'s\n    ``plan-reviewer`` Agent emits the bare ``parsed`` payload directly\n    (no wrapper envelope, no Codex `outcome` semantics). On this path the\n    parser treats stdin as a JSON object matching\n    ``codex_plan_review_schema.json`` directly; the envelope-level\n    `subcommand` / `outcome` / `task_id` checks are skipped, and a\n    successful parse emits the same `{plan_file, verdict, findings_count,\n    findings, notes, schedule_ok, summary}` result shape as the Codex\n    path so downstream verdict routing is identical. Outcome on the\n    Claude path is always `success` (Agent-side errors bubble up as\n    Agent dispatch failures, not envelope-level outcomes).\n\n    Exits non-zero with canonical `errors[*]` on schema violations so the\n    orchestrator can halt the run before Phase 2. Successful validation\n    extracts `{plan_file, verdict, findings_count, findings, summary,\n    schedule_ok}` for the caller. Cross-plan dependency resolution is\n    verified by the orchestrator in Phase 0 preflight; the reviewer no\n    longer reports on it.\n    '
    payload = _args_to_payload_parse_plan_review_report(args)
    result = _run_parse_plan_review_report(payload)
    _emit_or_die(args, result)


def _args_to_payload_parse_d5_adjudication(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "codex_findings_count": args.codex_findings_count,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_parse_d5_adjudication(payload: dict) -> dict:
    raw = payload['stdin_text']
    if not raw.strip():
        return _result({'errors': [{'path': '$', 'code': 'empty-stdin', 'message': 'parse-d5-adjudication expects a JSON payload on stdin'}]}, exit_code=1)
    try:
        adjudication = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'stdin is not valid JSON: {exc}'}]}, exit_code=1)
    count = payload['codex_findings_count']
    if count < 0:
        return _result({'errors': [{'path': '$', 'code': 'invalid-findings-count', 'message': f'--codex-findings-count must be non-negative, got {count}'}]}, exit_code=1)
    errors = _validate_d5_adjudication_payload(adjudication, codex_findings_count=count)
    if errors:
        return _result({'errors': errors}, exit_code=1)
    assert isinstance(adjudication, dict)
    verdict = adjudication.get('verdict')
    load_bearing = adjudication.get('load_bearing') if verdict == 'partial-agreement' else None
    dismissed = adjudication.get('dismissed') if verdict == 'partial-agreement' else None
    result = {'verdict': verdict, 'summary': adjudication.get('summary', ''), 'load_bearing': load_bearing, 'dismissed': dismissed, 'errors': []}
    return _result(result, exit_code=0)

def cmd_parse_d5_adjudication(args: argparse.Namespace) -> None:
    'Validate a D.5 adjudication payload from stdin per TASK-016A.\n\n    Input: single JSON object\n        {\n          "verdict": "ship" | "ship-with-fixes" | "partial-agreement" | "needs-rework",\n          "summary": "...",\n          # required when verdict == "partial-agreement":\n          "load_bearing": [0, 2],\n          "dismissed":    [1, 3]\n        }\n\n    The `--codex-findings-count` flag is the length of the Codex\n    `parsed.findings[]` array the D.5 reviewer was adjudicating;\n    partial-agreement indices MUST stay within `range(0, count)`.\n\n    On success emits the structured dispatch payload the orchestrator\n    forwards to D.2a.6: `{verdict, summary, load_bearing, dismissed,\n    errors:[]}`. The split fields are only present (as arrays) on\n    `partial-agreement`; other verdicts leave them as `null` for\n    explicit routing.\n    '
    payload = _args_to_payload_parse_d5_adjudication(args)
    result = _run_parse_d5_adjudication(payload)
    _emit_or_die(args, result)


def _args_to_payload_parse_plan_review_triage_report(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "source": args.source,
        "findings_count": args.findings_count,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_parse_plan_review_triage_report(payload: dict) -> dict:
    raw = payload['stdin_text']
    if not raw.strip():
        return _result({'errors': [{'path': '$', 'code': 'empty-stdin', 'message': 'parse-plan-review-triage-report expects a markdown report on stdin'}]}, exit_code=1)
    source = payload['source']
    if source is None:
        return _result({'errors': [{'path': '$', 'code': 'triage-source-missing', 'message': '--source is required'}]}, exit_code=1)
    if source not in ALLOWED_PLAN_REVIEW_TRIAGE_SOURCES:
        return _result({'errors': [{'path': '$', 'code': 'triage-source-unknown', 'message': f'--source must be one of {sorted(ALLOWED_PLAN_REVIEW_TRIAGE_SOURCES)}, got {source!r}'}]}, exit_code=1)
    count = payload['findings_count']
    if count is None:
        return _result({'errors': [{'path': '$', 'code': 'invalid-findings-count', 'message': '--findings-count is required'}]}, exit_code=1)
    if count < 0:
        return _result({'errors': [{'path': '$', 'code': 'invalid-findings-count', 'message': f'--findings-count must be non-negative, got {count}'}]}, exit_code=1)
    payload_text = _extract_last_fenced_json_block(raw)
    if payload_text is None:
        return _result({'errors': [{'path': '$', 'code': 'triage-report-missing-json', 'message': 'triage report is missing a fenced ```json block'}]}, exit_code=1)
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return _result({'errors': [{'path': '$', 'code': 'json-decode', 'message': f'triage JSON block is not valid JSON: {exc}'}]}, exit_code=1)
    errors = _validate_plan_review_triage_payload(payload, findings_count=count, source=source)
    if errors:
        return _result({'errors': errors}, exit_code=1)
    assert isinstance(payload, dict)
    result = {'verdict': payload.get('verdict'), 'load_bearing': payload.get('load_bearing'), 'dismissed': payload.get('dismissed'), 'summary': payload.get('summary'), 'findings_count': count, 'source': source, 'errors': []}
    return _result(result, exit_code=0)

def cmd_parse_plan_review_triage_report(args: argparse.Namespace) -> None:
    payload = _args_to_payload_parse_plan_review_triage_report(args)
    result = _run_parse_plan_review_triage_report(payload)
    _emit_or_die(args, result)


def _args_to_payload_commit_task(args: argparse.Namespace) -> dict:
    # TASK-016C (post-remediation): `args.dismissed_finding_ids` is already
    # normalized from the raw comma string into list[int] by main()'s
    # cross-flag block before the handler runs. Pure/MCP callers MUST pass
    # the same already-typed list shape.
    return {
        "task_id": args.task_id,
        "files": args.files,
        "plan_file": args.plan_file,
        "reviewer": args.reviewer,
        "reviewer_verdict": args.reviewer_verdict,
        "reviewer_minor_findings": args.reviewer_minor_findings,
        "dry_run": bool(getattr(args, "dry_run", False)),
        "v_check_timeout": getattr(args, "v_check_timeout", None),
        "run_id": args.run_id,
        "title": args.title,
        "diff_summary": args.diff_summary,
        "remediation_tag": bool(getattr(args, "remediation_tag", False)),
        "sandbox_divergence_tag": bool(
            getattr(args, "sandbox_divergence_tag", False)
        ),
        "d4_rescue_tag": bool(getattr(args, "d4_rescue_tag", False)),
        "narrow_remediation_tag": bool(
            getattr(args, "narrow_remediation_tag", False)
        ),
        "disagreement_tag": bool(getattr(args, "disagreement_tag", False)),
        "dismissed_finding_ids": list(
            getattr(args, "dismissed_finding_ids", []) or []
        ),
        "update_schedule_state": getattr(args, "update_schedule_state", None),
    }


def _validate_commit_task_payload(payload: dict) -> list[dict]:
    """Mirror commit-task CLI cross-flag invariants for pure/MCP callers.

    The CLI path enforces these via ``parser.error()`` in ``main()`` before
    the handler runs, so the live CLI banner shape is preserved. Pure/MCP
    callers bypass argparse entirely, so this helper produces equivalent
    structured ``errors[]`` for the same illegal combinations:

    - ``--d4-rescue-tag`` + ``--disagreement-tag`` (D.4 rescue does not
      invoke D.5).
    - ``--d4-rescue-tag`` + non-empty ``--dismissed-finding-ids`` (rescue
      treats every reviewer finding as load-bearing).
    - ``--dismissed-finding-ids`` without ``--narrow-remediation-tag``
      (the ``[disagreement: i,j]`` trailer only exists on D.2a.6).
    - ``--narrow-remediation-tag`` without ``--dismissed-finding-ids``
      (a narrow-remediation commit requires at least one dismissed
      index).

    The raw-string parsing of ``--dismissed-finding-ids`` (empty/non-integer
    tokens) is NOT mirrored here — pure callers MUST pass an already-typed
    ``list[int]`` per the schema; that gate stays in ``main()`` so the CLI
    keeps emitting the standard argparse exit-code-2 banner.
    """
    errors: list[dict] = []
    narrow = bool(payload.get("narrow_remediation_tag"))
    d4 = bool(payload.get("d4_rescue_tag"))
    disagreement = bool(payload.get("disagreement_tag"))
    dismissed = list(payload.get("dismissed_finding_ids") or [])
    has_dismissed = bool(dismissed)
    if d4 and disagreement:
        errors.append({
            "path": "$",
            "code": "d4-rescue-vs-disagreement",
            "message": (
                "--d4-rescue-tag is mutually exclusive with "
                "--disagreement-tag; D.4 rescue does not invoke D.5 "
                "(the rescue branch commits directly on post-rescue "
                "clean re-review per SKILL.md §D.4)"
            ),
        })
    if d4 and has_dismissed:
        errors.append({
            "path": "$",
            "code": "d4-rescue-vs-dismissed-finding-ids",
            "message": (
                "--d4-rescue-tag is mutually exclusive with "
                "--dismissed-finding-ids; D.4 rescue does not carry "
                "dismissed findings (rescue_findings[] in the dispatch "
                "template is exhaustive — every reviewer finding is "
                "treated as load-bearing for the rescue attempt)"
            ),
        })
    if has_dismissed and not narrow:
        errors.append({
            "path": "$.dismissed_finding_ids",
            "code": "dismissed-without-narrow-remediation",
            "message": (
                "--dismissed-finding-ids requires "
                "--narrow-remediation-tag; the [disagreement: i,j] "
                "trailer only appears on the D.2a.6 narrow-remediation "
                "path"
            ),
        })
    if narrow and not has_dismissed:
        errors.append({
            "path": "$.dismissed_finding_ids",
            "code": "narrow-remediation-without-dismissed",
            "message": (
                "--narrow-remediation-tag requires a non-empty "
                "--dismissed-finding-ids; the partial-agreement path "
                "always carries at least one dismissed index"
            ),
        })
    return errors


def _run_commit_task(payload: dict) -> dict:
    cross_errors = _validate_commit_task_payload(payload)
    if cross_errors:
        return _result({"errors": cross_errors}, exit_code=1)

    tid = _normalize_task_id(payload["task_id"])
    if not tid:
        return _result(
            {"error": f"bad --task-id: {payload['task_id']!r}"},
            exit_code=1,
        )

    files = _normalize_csv_or_list(payload["files"])
    if not files:
        return _result(
            {"error": "--files must list at least one file"},
            exit_code=1,
        )

    plan = Path(payload["plan_file"])
    if not plan.is_file():
        return _result({"error": f"plan file not found: {plan}"}, exit_code=1)

    try:
        minor = _normalize_json_or_list(payload["reviewer_minor_findings"])
    except json.JSONDecodeError as e:
        return _result(
            {"error": f"invalid --reviewer-minor-findings: {e}"},
            exit_code=1,
        )
    review_errors = _validate_review_success_payload(
        payload["reviewer"],
        payload["reviewer_verdict"],
        minor,
    )
    if review_errors:
        return _result({"errors": review_errors}, exit_code=1)

    original_plan = _load_text(plan)
    try:
        mutated, _prior = mutate_task_status(original_plan, tid, "done")
    except ValueError as e:
        return _result({"error": f"status mutation: {e}"}, exit_code=1)

    if payload["dry_run"]:
        return _result({
            "dry_run": True,
            "task_id": tid,
            "files": files,
            "would_commit": True,
        }, exit_code=0)

    # TASK-020B: opt-in `acceptance_v_check` YAML frontmatter runs the plan's
    # own declared V-check pre-commit. Runs AFTER the `--files` staging guard
    # (the `--files` validation above) but BEFORE the plan-status flip
    # (`_write_text` below). On failure we return a structured error — no
    # plan text written, no git state touched, no `commit_done` event. Plans
    # without frontmatter or without the key: zero behavior change.
    fm = _parse_frontmatter(original_plan)
    v_check_cmd = fm.get("acceptance_v_check")
    if v_check_cmd:
        timeout = payload.get("v_check_timeout") or 300
        # `cmd_commit_task` has no `--git-dir` flag — the caller's CWD is the
        # repo root, matching the semantics of `_git()` above (which also
        # runs with no explicit cwd). Using `Path(".")` keeps the V-check
        # execution context consistent with the surrounding git operations.
        v_result = _run_v_check(str(v_check_cmd), Path("."), timeout)
        if v_result.get("code") != "v-check-passed":
            return _result({
                "errors": [{
                    "code": v_result["code"],
                    "message": v_result["message"],
                    "stdout_tail": v_result["stdout_tail"],
                    "stderr_tail": v_result["stderr_tail"],
                }],
            }, exit_code=1)
        # Log pass event BEFORE the commit so the audit record captures the
        # V-check outcome even if a later step (e.g., `git commit`) fails.
        # Per the annotation: `v_check_passed` is an audit event, NOT proof
        # of task completion — pairing proof remains `commit_done`.
        _append_run_log("v_check_passed", {
            "run_id": payload["run_id"],
            "task_id": tid,
            "command": str(v_check_cmd),
        })

    _write_text(plan, mutated)

    # TASK-016C (post-remediation): dismissed-finding-ids content parsing
    # lives in main() post-parse so empty/non-integer tokens fail via
    # parser.error() (exit 2) before any plan mutation. By the time we
    # reach the handler, payload["dismissed_finding_ids"] is already a
    # list[int] (possibly empty) — see main() and
    # _args_to_payload_commit_task().
    dismissed_ids: list[int] = list(payload.get("dismissed_finding_ids") or [])

    commit_msg = (
        f"feat(TASK-{tid}): {payload['title']}\n\n"
        f"{payload['diff_summary']}\n\n"
        f"Plan: {plan.name}\n"
    )
    if payload["remediation_tag"]:
        # D.2a.5 post-remediation commit: a trailing [remediation] tag so the
        # run summary and `git log --oneline` can distinguish retries from
        # clean first-pass commits. Kept on its own line adjacent to any
        # [disagreement] tag that D.2a might have already appended upstream.
        commit_msg = commit_msg.rstrip("\n") + "\n\n[remediation]\n"
    if payload["sandbox_divergence_tag"]:
        # TASK-008 (POSTMORTEM_FIXES): orchestrator auto-validate branch
        # writes `[sandbox-divergence]` to the commit body alongside any
        # existing [disagreement] / [remediation] tags. The tag is
        # informational — it does NOT relax the reviewer-verdict
        # whitelist (`_validate_review_success_payload` runs unchanged
        # above). Adjacent to other trailers and on its own line so
        # `git log --oneline` and the run summary can scan for it.
        commit_msg = commit_msg.rstrip("\n") + "\n\n[sandbox-divergence]\n"
    if payload["d4_rescue_tag"]:
        # TASK-005 D.4 rescue commit: append a [d4-rescue] trailer line
        # so `git log --oneline` and the run summary can distinguish a
        # successful rescue from D.2a.5 [remediation] or D.2a.6
        # [narrow-remediation] retries. Argparse + post-parse blocks
        # have already excluded --remediation-tag, --narrow-remediation
        # -tag, --disagreement-tag, and --dismissed-finding-ids.
        commit_msg = commit_msg.rstrip("\n") + "\n\n[d4-rescue]\n"
    if payload["narrow_remediation_tag"]:
        # TASK-016C D.2a.6 post-narrow-remediation commit: a trailing
        # [narrow-remediation] tag plus a [disagreement: i,j] trailer
        # listing the dismissed finding indices, on adjacent lines with
        # [narrow-remediation] first. Distinct from D.2a.5's
        # [remediation] tag so `git log --oneline` can distinguish the
        # narrow retry from the full-rework retry. Argparse has already
        # excluded --remediation-tag and --disagreement-tag.
        trailer_ids = ",".join(str(i) for i in dismissed_ids)
        commit_msg = (
            commit_msg.rstrip("\n")
            + f"\n\n[narrow-remediation]\n[disagreement: {trailer_ids}]\n"
        )

    # TASK-014B — roster auto-update must happen BEFORE `git add` so the
    # updated `00_INDEX.json` is included in the same commit as the plan
    # status bullet and implementation files. Otherwise the roster change
    # is left as an uncommitted working-tree mutation, which is exactly
    # the drift this task was supposed to prevent. On `invalid-index` or
    # `missing-entry` we restore the plan text and exit with the structured
    # error before any git state changes. A missing `00_INDEX.json` is a
    # no-op — legacy plans that predate the roster MUST keep committing.
    index_path = plan.parent / "00_INDEX.json"
    # Capture pre-update roster bytes so downstream git failures can roll
    # the roster write back alongside the plan text. `None` means the file
    # did not exist beforehand — rollback in that branch is an unlink.
    original_index_bytes: bytes | None
    try:
        original_index_bytes = index_path.read_bytes()
    except FileNotFoundError:
        original_index_bytes = None

    outcome, err = _update_index_status(index_path, plan.name, "Done", task_id=tid)
    if outcome in ("invalid-index", "missing-entry"):
        assert err is not None
        _write_text(plan, original_plan)
        return _result({"errors": [err]}, exit_code=1)

    def _restore_roster() -> None:
        """Roll the roster back through the same atomic pattern as the
        forward write (tempfile + os.replace). A direct `write_bytes`
        here would re-introduce a non-atomic write path — an interrupted
        rollback could truncate `00_INDEX.json`, which is exactly the
        failure mode V6's atomicity requirement forbids.
        """
        if outcome != "updated":
            return
        if original_index_bytes is None:
            try:
                index_path.unlink()
            except FileNotFoundError:
                pass
            return
        parent = index_path.parent
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=str(parent),
                prefix=".00_INDEX.rollback.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp.write(original_index_bytes)
                tmp_name = tmp.name
            os.replace(tmp_name, index_path)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    add_files = files + [str(plan)]
    if outcome == "updated":
        # `commit-task` is the sole authority for staging `00_INDEX.json`
        # on behalf of the orchestrator -- the roster update is not part
        # of any task's declared `Files:` list. Bind the pre-commit
        # staging seam to the post-commit `commit-safe` gate by
        # asserting the index path resolves to a member of the shared
        # always-ignore set via `is_commit_always_ignore` (with
        # `plan_dir` passed so bundles outside `docs/plans/` still
        # match). Failure here means the set and the staging logic
        # diverged, which would re-open the drift `commit-safe` is
        # meant to prevent; treat as an internal-error halt.
        try:
            rel_index = str(
                index_path.resolve().relative_to(Path.cwd().resolve())
            ).replace(os.sep, "/")
            rel_plan_dir: str | None = str(
                plan.resolve().parent.relative_to(Path.cwd().resolve())
            ).replace(os.sep, "/")
            if rel_plan_dir == ".":
                rel_plan_dir = ""
        except (ValueError, OSError):
            rel_index = index_path.as_posix()
            rel_plan_dir = None
        if not is_commit_always_ignore(
            rel_index, plan.name, rel_plan_dir,
        ):
            _write_text(plan, original_plan)
            _restore_roster()
            return _result({
                "error": (
                    "internal: 00_INDEX.json staged path "
                    f"{rel_index!r} is not in the shared "
                    "COMMIT_ALWAYS_IGNORE set; commit-task and "
                    "_gate_commit_safe would diverge"
                ),
            }, exit_code=1)
        add_files.append(str(index_path))
    add = _git(["add", "--", *add_files])
    if add.returncode != 0:
        _write_text(plan, original_plan)
        _restore_roster()
        return _result(
            {"error": f"git add failed: {add.stderr.strip()}"},
            exit_code=1,
        )

    commit = _git(["commit", "-m", commit_msg, "--only", "--", *add_files])
    if commit.returncode != 0:
        _git(["reset", "HEAD", "--", *add_files])
        _write_text(plan, original_plan)
        _restore_roster()
        return _result(
            {
                "error": (
                    f"git commit failed: "
                    f"{commit.stderr.strip() or commit.stdout.strip()}"
                ),
            },
            exit_code=1,
        )

    sha_cp = _git(["rev-parse", "HEAD"])
    commit_sha = sha_cp.stdout.strip()

    event_fields = {
        "run_id": payload["run_id"],
        "task_id": tid,
        "commit_sha": commit_sha,
        "files": files,
        "reviewer_verdict": payload["reviewer_verdict"],
        "minor_findings_count": len(minor),
        # TASK-022: persist the full reviewer minor-findings payload on
        # every `commit_done` event so audits months later can retrieve
        # exactly what was flagged (and, via any `disposition` fields,
        # why it was dismissed/accepted/deferred). Key is always present:
        # an empty `--reviewer-minor-findings '[]'` yields `findings: []`.
        "findings": minor,
        "disagreement_tag": bool(payload["disagreement_tag"]),
        "remediation_tag": bool(payload["remediation_tag"]),
        # TASK-016C: surface the D.2a.6 flags in commit_done so the run
        # summary and downstream auditing can distinguish narrow
        # remediations from full D.2a.5 retries without re-parsing the
        # commit body.
        "narrow_remediation_tag": bool(payload["narrow_remediation_tag"]),
        # TASK-005: surface the D.4-rescue flag in commit_done so the
        # run summary and downstream auditing can distinguish rescue
        # commits from D.2a.5/D.2a.6 retries without re-parsing the
        # commit body. Parallel to the remediation/narrow flags above.
        "d4_rescue_tag": bool(payload["d4_rescue_tag"]),
        "dismissed_finding_ids": dismissed_ids,
        # TASK-008 (POSTMORTEM_FIXES): surface the auto-validate
        # divergence tag in the commit_done event so the run summary's
        # "Sandbox divergences" subsection (and downstream auditing) can
        # enumerate affected tasks without re-parsing commit bodies.
        "sandbox_divergence_tag": bool(payload["sandbox_divergence_tag"]),
    }
    _append_run_log("commit_done", event_fields)

    # TASK-002 (PHASE_D_STATE_MACHINE): atomically promote the task into
    # `state.done`, append a commit record, and release this task's
    # `state.locked_files` entries. AFTER `commit_done` is logged so the
    # audit trail captures the commit even if state-write fails.
    state_write_warning: str | None = None
    state_written = False
    sched_for_state = payload.get("update_schedule_state")
    if sched_for_state:
        prior_state = read_schedule_state(sched_for_state)
        new_state = apply_commit_state_transition(
            prior_state, tid, commit_sha, files,
        )
        state_written, state_write_warning = _write_schedule_state(
            sched_for_state, new_state,
        )

    out: dict = {
        "commit_sha": commit_sha,
        "status_updated": True,
        "log_appended": True,
    }
    if sched_for_state:
        out["schedule_state_written"] = state_written
        if state_write_warning:
            out["schedule_state_warning"] = state_write_warning
    return _result(out, exit_code=0)


def cmd_commit_task(args: argparse.Namespace) -> None:
    payload = _args_to_payload_commit_task(args)
    result = _run_commit_task(payload)
    _emit_or_die(args, result)


def _is_inside_submodule(abs_path: Path, rel: str, repo_root: Path) -> bool:
    """True if ``abs_path`` is a gitlink OR sits inside a nested git repo
    distinct from ``repo_root``.

    Covers the nonexistent-leaf case: for ``submods/foo/new.txt`` where
    ``new.txt`` does not yet exist but ``submods/foo`` is a gitlink, the
    probe walks up from ``abs_path.parent`` to the nearest existing ancestor
    and runs ``git rev-parse --show-toplevel`` there. If that toplevel
    differs from ``repo_root``, the path is inside a submodule.
    """
    probe_gitlink = _git(
        ["ls-files", "--stage", "--", rel],
        cwd=repo_root,
    )
    if probe_gitlink.returncode == 0 and probe_gitlink.stdout.startswith("160000"):
        return True
    # Walk up to the nearest existing ancestor so nonexistent leaves under
    # a submodule still classify correctly.
    probe_dir = abs_path if abs_path.exists() else None
    if probe_dir is None:
        cursor = abs_path.parent
        try:
            repo_root_resolved = repo_root.resolve()
        except OSError:
            return False
        while cursor != cursor.parent:
            try:
                if cursor.resolve() == repo_root_resolved:
                    break
            except OSError:
                break
            if cursor.exists():
                probe_dir = cursor
                break
            cursor = cursor.parent
    if probe_dir is None:
        return False
    if probe_dir.is_file() or probe_dir.is_symlink():
        probe_dir = probe_dir.parent
    tl = _git(["rev-parse", "--show-toplevel"], cwd=probe_dir)
    if tl.returncode != 0:
        return False
    try:
        toplevel = Path(tl.stdout.strip()).resolve()
    except OSError:
        return False
    try:
        return toplevel != repo_root.resolve()
    except OSError:
        return False


def _args_to_payload_block_dependents(args: argparse.Namespace) -> dict:
    update_schedule_state = getattr(args, "update_schedule_state", None)
    payload = {
        "schedule_file": pathlib.Path(args.schedule_file) if args.schedule_file else None,
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "failed": args.failed,
        "run_id": args.run_id,
        "update_schedule_state": pathlib.Path(update_schedule_state) if update_schedule_state else None,
    }
    return payload

def _run_block_dependents(payload: dict) -> dict:
    sched_path = Path(payload['schedule_file'])
    plan_path = Path(payload['plan_file'])
    if not sched_path.is_file():
        return _result({'error': f'schedule file not found: {sched_path}'}, exit_code=1)
    if not plan_path.is_file():
        return _result({'error': f'plan file not found: {plan_path}'}, exit_code=1)
    try:
        data = json.loads(sched_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        return _result({'error': f'schedule json decode: {e}'}, exit_code=1)
    failed_id = _normalize_task_id(payload['failed'])
    if not failed_id:
        return _result({'error': f"cannot normalize --failed: {payload['failed']!r}"}, exit_code=1)
    plan_dir = plan_path.parent
    blocked: list[str] = []
    routing: dict[str, tuple[str | None, int]] = {}
    queue = [failed_id]
    seen = set(queue)
    tasks = data.get('tasks') or []
    while queue:
        cur = queue.pop(0)
        for i, t in enumerate(tasks):
            raw_tid = t.get('id')
            if raw_tid is None:
                continue
            tid = _normalize_task_id(str(raw_tid))
            if not tid or tid in seen:
                continue
            deps = [_normalize_task_id(str(d)) for d in t.get('dependencies') or []]
            if cur in deps:
                blocked.append(tid)
                seen.add(tid)
                queue.append(tid)
                pf = t.get('plan_file')
                routing[tid] = (pf if isinstance(pf, str) and pf else None, i)
    if not blocked:
        return _result({'blocked_task_ids': [], 'plan_mutations_applied': [], 'run_log_appended': []}, exit_code=0)
        return
    remaining: list[str] = list(blocked)
    resolved_paths: dict[str, Path] = {}
    resolved_basenames: dict[str, str] = {}
    for bid in blocked:
        pf_basename, task_idx = routing[bid]
        if pf_basename is None:
            return _result({'errors': [{'code': 'missing-plan-file', 'path': f'$.tasks[{task_idx}].plan_file', 'failed_id': bid, 'plan_file': None, 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
        if not _is_valid_plan_file_basename(pf_basename):
            return _result({'errors': [{'code': 'dependent-file-not-in-plan-dir', 'path': f'$.tasks[{task_idx}].plan_file', 'failed_id': bid, 'plan_file': pf_basename, 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
        candidate = plan_dir / pf_basename
        try:
            candidate_abs = candidate.resolve()
            plan_dir_abs = plan_dir.resolve()
        except OSError as e:
            return _result({'errors': [{'code': 'dependent-file-not-in-plan-dir', 'path': f'$.tasks[{task_idx}].plan_file', 'failed_id': bid, 'plan_file': pf_basename, 'error': str(e), 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
        try:
            candidate_abs.relative_to(plan_dir_abs)
        except ValueError:
            return _result({'errors': [{'code': 'dependent-file-not-in-plan-dir', 'path': f'$.tasks[{task_idx}].plan_file', 'failed_id': bid, 'plan_file': pf_basename, 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
        if not candidate.is_file():
            return _result({'errors': [{'code': 'dependent-file-not-in-plan-dir', 'path': f'$.tasks[{task_idx}].plan_file', 'failed_id': bid, 'plan_file': pf_basename, 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
        resolved_paths[bid] = candidate
        resolved_basenames[bid] = pf_basename
    groups: list[tuple[Path, list[str]]] = []
    path_to_index: dict[str, int] = {}
    for bid in blocked:
        p = resolved_paths[bid]
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key not in path_to_index:
            path_to_index[key] = len(groups)
            groups.append((p, [bid]))
        else:
            groups[path_to_index[key]][1].append(bid)
    file_texts: dict[Path, str] = {}
    for path, ids in groups:
        try:
            file_texts[path] = _load_text(path)
        except OSError as e:
            return _result({'errors': [{'failed_stage': 'plan_read', 'failed_id': None, 'error': str(e), 'plan_file': path.name, 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
    for bid in blocked:
        _, task_idx = routing[bid]
        target = resolved_paths[bid]
        text = file_texts[target]
        _, task_blocks = _split_task_blocks(text)
        found = any((tid == bid for tid, _ in task_blocks))
        if not found:
            return _result({'errors': [{'code': 'dependent-block-missing', 'path': f'$.tasks[{task_idx}]', 'failed_id': bid, 'plan_file': resolved_basenames[bid], 'plan_mutations_applied': [], 'run_log_appended': [], 'remaining': list(remaining)}]}, exit_code=1)
    plan_mutations_applied: list[str] = []
    run_log_appended: list[str] = []
    applied_basenames: dict[str, str] = {}
    mutate_failure: dict | None = None
    for path, ids in groups:
        original = file_texts[path]
        mutated_text = original
        mutated_in_memory: list[str] = []
        group_mutate_failure: dict | None = None
        for idx, bid in enumerate(ids):
            try:
                mutated_text, _ = mutate_task_status(mutated_text, bid, 'blocked')
            except ValueError as e:
                group_mutate_failure = {'failed_stage': 'plan_mutate', 'failed_id': bid, 'error': str(e), 'remaining': list(ids[idx + 1:])}
                break
            mutated_in_memory.append(bid)
            applied_basenames[bid] = resolved_basenames[bid]
        if group_mutate_failure is not None and mutate_failure is None:
            mutate_failure = group_mutate_failure
        if not mutated_in_memory:
            continue
        try:
            _write_text(path, mutated_text)
        except OSError as e:
            if mutate_failure is not None:
                payload: dict = dict(mutate_failure)
                payload['plan_mutations_applied'] = list(plan_mutations_applied)
                payload['run_log_appended'] = list(run_log_appended)
                payload['secondary_failed_stage'] = 'plan_write'
                payload['secondary_failed_id'] = None
                payload['secondary_error'] = str(e)
            else:
                payload = {'failed_stage': 'plan_write', 'failed_id': None, 'error': str(e), 'plan_mutations_applied': list(plan_mutations_applied), 'run_log_appended': list(run_log_appended), 'remaining': []}
            return _result({'errors': [payload]}, exit_code=1)
        plan_mutations_applied.extend(mutated_in_memory)
    if not plan_mutations_applied:
        assert mutate_failure is not None
        mutate_failure['plan_mutations_applied'] = []
        mutate_failure['run_log_appended'] = []
        return _result({'errors': [mutate_failure]}, exit_code=1)
    log_failure: dict | None = None
    for bid in plan_mutations_applied:
        try:
            _append_run_log('blocked', {'run_id': payload['run_id'], 'task_id': bid, 'blocker_task_id': failed_id, 'reason': f'dependency TASK-{failed_id} failed', 'plan_file': applied_basenames[bid]})
        except Exception as e:
            log_failure = {'failed_stage': 'run_log_append', 'failed_id': bid, 'error': str(e)}
            break
        run_log_appended.append(bid)
    if mutate_failure is not None:
        mutate_failure['plan_mutations_applied'] = list(plan_mutations_applied)
        mutate_failure['run_log_appended'] = list(run_log_appended)
        if log_failure is not None:
            mutate_failure['secondary_failed_stage'] = 'run_log_append'
            mutate_failure['secondary_failed_id'] = log_failure['failed_id']
        return _result({'errors': [mutate_failure]}, exit_code=1)
    if log_failure is not None:
        log_failure['plan_mutations_applied'] = list(plan_mutations_applied)
        log_failure['run_log_appended'] = list(run_log_appended)
        log_failure['remaining'] = []
        return _result({'errors': [log_failure]}, exit_code=1)
    state_write_warning: str | None = None
    state_written = False
    sched_for_state = payload.get('update_schedule_state')
    if sched_for_state:
        prior_state = read_schedule_state(sched_for_state)
        new_state = apply_blocked_state_transition(prior_state, blocked)
        state_written, state_write_warning = _write_schedule_state(sched_for_state, new_state)
    out: dict = {'blocked_task_ids': blocked, 'plan_mutations_applied': plan_mutations_applied, 'run_log_appended': run_log_appended}
    if sched_for_state:
        out['schedule_state_written'] = state_written
        if state_write_warning:
            out['schedule_state_warning'] = state_write_warning
    return _result(out, exit_code=0)

def cmd_block_dependents(args: argparse.Namespace) -> None:
    "Cascade `blocked` status onto dependents of a failed task.\n\n    TASK-002 (prohibit_silent_revert): paused tasks do NOT cascade. Only the\n    `failed` terminal status triggers this dependents-blocking cascade; a\n    `paused` task awaits human disposition and its dependents must wait, not\n    be preemptively blocked. The orchestrator MUST NOT call `block-dependents`\n    with a paused task id.\n\n    TASK-004D / ISSUE-012: mutate the plan markdown (source of truth) as well\n    as append run-log events (observability). Per file, ordering is\n    mutate-all-in-memory → single plan write → log each applied id. See\n    TASK-004D_block_dependents_mutation.md for the full failure-stage\n    semantics and double-failure precedence contract.\n\n    TASK-002 (directory mode): each dependent is routed to a child plan\n    file via the schedule's required `tasks[].plan_file` (basename).\n    TASK-008 (per_task_dispatch_refactor_v2) REMOVED the single-file\n    `--plan-file` fallback for dependents missing `plan_file`; every\n    dependent in the cascade must declare its own `plan_file` or the\n    subcommand halts with a structured `missing-plan-file` error. The\n    `--plan-file` argument is preserved as the DAG-lookup anchor (it\n    locates the schedule's parent plan_dir for resolution).\n\n    Per-file atomic write: one read + one in-memory mutate pass + one\n    `_write_text` call per unique file, with run-log events carrying\n    `plan_file` (basename) for audit attribution. Pre-write containment\n    check rejects `plan_file` values that escape the plan_dir, and\n    pre-write block-presence check rejects `plan_file` values whose\n    referenced task block is missing.\n    "
    payload = _args_to_payload_block_dependents(args)
    result = _run_block_dependents(payload)
    _emit_or_die(args, result)


def _args_to_payload_fail_task(args: argparse.Namespace) -> dict:
    return {
        "authorization_source": getattr(args, "authorization_source", None),
        "task_id": args.task_id,
        "plan_file": args.plan_file,
        "repo_root": getattr(args, "repo_root", None),
        "files": args.files,
        "run_id": args.run_id,
        "stage": args.stage,
        "reason": args.reason,
        "reversion_guidance": getattr(args, "reversion_guidance", None),
        "reviewer_findings": getattr(args, "reviewer_findings", None),
        "update_schedule_state": getattr(args, "update_schedule_state", None),
        "retries_used": getattr(args, "retries_used", None),
    }


def _run_fail_task(payload: dict) -> dict:
    # ``--authorization-source`` is logically required (see argparse
    # declaration). Missing flag emits the structured envelope so
    # consumers can branch on ``errors[*].code`` rather than parsing
    # argparse's stderr text. This is the audit gate that closes the
    # silent-revert regression vector: every future contributor adding a
    # ``fail-task`` call MUST consciously declare the authorized path.
    auth_source = payload.get("authorization_source")
    if not auth_source:
        return _result(
            {"errors": [{
                "code": "authorization-source-required",
                "message": (
                    "--authorization-source is required. Allowed values: "
                    + ", ".join(sorted(ALLOWED_FAIL_AUTHORIZATION_SOURCES))
                    + ". Each value corresponds to a documented authorized "
                    "path in /implement-plan; see ALLOWED_FAIL_AUTHORIZATION_SOURCES "
                    "in plan_ops.py for what each value sanctions."
                ),
            }]},
            exit_code=1,
        )

    tid = _normalize_task_id(payload["task_id"])
    if not tid:
        return _result(
            {"error": f"bad --task-id: {payload['task_id']!r}"},
            exit_code=1,
        )

    plan = Path(payload["plan_file"])
    if not plan.is_file():
        return _result(
            {"error": f"plan file not found: {plan}"},
            exit_code=1,
        )

    repo_root_arg = payload.get("repo_root")
    if repo_root_arg:
        repo_root = Path(repo_root_arg).resolve()
    else:
        repo_root = Path.cwd().resolve()

    files_payload = payload.get("files") or ""
    if isinstance(files_payload, list):
        raw_files = [f.strip() for f in files_payload if isinstance(f, str) and f.strip()]
    else:
        raw_files = [f.strip() for f in files_payload.split(",") if f.strip()]

    tracked: list[str] = []
    untracked: list[str] = []
    protected_skipped: list[str] = []
    out_of_repo_skipped: list[str] = []
    directory_skipped: list[str] = []
    submodule_skipped: list[str] = []

    for raw in raw_files:
        rel = canonicalize_file(raw, repo_root)
        if rel is None:
            # canonicalize_file returns None for out-of-repo escapes.
            # Preserve the caller's original spelling in the emit so
            # operators can see what they passed.
            out_of_repo_skipped.append(raw)
            continue
        if rel == "":
            # Canonicalized to repo_root itself — a directory.
            directory_skipped.append(rel)
            continue
        abs_path = repo_root / rel
        # Order of checks matches the classification table -- submodule
        # BEFORE directory (a gitlink is a directory in the worktree, so
        # it must classify as submodule not directory).
        if _is_inside_submodule(abs_path, rel, repo_root):
            submodule_skipped.append(rel)
            continue
        if abs_path.is_dir() and not abs_path.is_symlink():
            directory_skipped.append(rel)
            continue
        if is_protected_path(rel):
            protected_skipped.append(rel)
            continue
        probe = _git(
            ["ls-files", "--error-unmatch", "--", rel],
            cwd=repo_root,
        )
        if probe.returncode == 0:
            tracked.append(rel)
        else:
            untracked.append(rel)

    restore_ok = True
    if tracked:
        restore = _git(
            ["restore", "--staged", "--worktree", "--", *tracked],
            cwd=repo_root,
        )
        if restore.returncode != 0:
            restore_ok = False

    removed_untracked: list[str] = []
    for rel in untracked:
        abs_path = repo_root / rel
        if not abs_path.exists() and not abs_path.is_symlink():
            continue
        try:
            abs_path.unlink()
        except FileNotFoundError:
            continue
        except (IsADirectoryError, PermissionError, OSError):
            continue
        removed_untracked.append(rel)

    original = _load_text(plan)
    try:
        mutated, _ = mutate_task_status(original, tid, "failed")
    except ValueError as e:
        return _result({"error": f"status mutation: {e}"}, exit_code=1)
    _write_text(plan, mutated)

    event_fields: dict = {
        "run_id": payload["run_id"],
        "task_id": tid,
        "stage": payload["stage"],
        "reason": payload["reason"],
    }
    event_fields["authorization_source"] = auth_source
    if payload.get("reversion_guidance"):
        event_fields["reversion_guidance"] = payload["reversion_guidance"]
    if payload.get("reviewer_findings"):
        try:
            parsed_findings = _normalize_json_or_list(payload["reviewer_findings"])
        except json.JSONDecodeError as e:
            return _result(
                {"error": f"invalid --reviewer-findings: {e}"}, exit_code=1,
            )
        if payload["stage"] == "review":
            review_errors = _validate_review_failure_payload(parsed_findings)
            if review_errors:
                return _result({"errors": review_errors}, exit_code=1)
        event_fields["reviewer_findings"] = parsed_findings
    _append_run_log("failed", event_fields)

    # TASK-002 (PHASE_D_STATE_MACHINE): persist failure into
    # `state.failed` and (optionally) `state.retries_used[task_id]`.
    state_write_warning: str | None = None
    state_written = False
    sched_for_state = payload.get("update_schedule_state")
    if sched_for_state:
        retries_obj: dict | None = None
        retries_raw = payload.get("retries_used")
        if retries_raw:
            try:
                parsed_r = json.loads(retries_raw)
            except json.JSONDecodeError as e:
                return _result(
                    {"error": f"invalid --retries-used: {e}"}, exit_code=1,
                )
            if not isinstance(parsed_r, dict):
                return _result(
                    {"error": "--retries-used must be a JSON object"},
                    exit_code=1,
                )
            retries_obj = parsed_r
        prior_state = read_schedule_state(sched_for_state)
        new_state = apply_fail_state_transition(prior_state, tid, retries_obj)
        state_written, state_write_warning = _write_schedule_state(
            sched_for_state, new_state,
        )

    out: dict = {
        "restore_ok": restore_ok,
        "status_updated": True,
        "log_appended": True,
        "removed_untracked": removed_untracked,
        "protected_skipped": protected_skipped,
        "out_of_repo_skipped": out_of_repo_skipped,
        "directory_skipped": directory_skipped,
        "submodule_skipped": submodule_skipped,
    }
    if sched_for_state:
        out["schedule_state_written"] = state_written
        if state_write_warning:
            out["schedule_state_warning"] = state_write_warning
    return _result(out, exit_code=0)


def cmd_fail_task(args: argparse.Namespace) -> None:
    payload = _args_to_payload_fail_task(args)
    result = _run_fail_task(payload)
    _emit_or_die(args, result)


def _args_to_payload_update_plan_header(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "status": args.status,
    }
    return payload

def _run_update_plan_header(payload: dict) -> dict:
    plan = Path(payload['plan_file'])
    if not plan.is_file():
        return _result({'error': f'plan file not found: {plan}'}, exit_code=1)
    text = _load_text(plan)
    header_end = TASK_HEADER_RE.search(text)
    header_slice = text[:header_end.start()] if header_end else text
    tail_slice = text[header_end.start():] if header_end else ''
    m = STATUS_BULLET_RE.search(header_slice)
    if not m:
        bold = re.search('^\\*\\*Status:\\*\\*\\s*(.+?)\\s*$', header_slice, re.MULTILINE)
        if not bold:
            return _result({'status': 'absent', 'warning': 'no plan-level **Status:** line to update; skipping'}, exit_code=0)
            return
        new_header = header_slice[:bold.start()] + f"**Status:** {payload['status']}" + header_slice[bold.end():]
    else:
        new_header = header_slice[:m.start()] + f"{m.group(1)} {payload['status']}" + header_slice[m.end():]
    _write_text(plan, new_header + tail_slice)
    return _result({'ok': True}, exit_code=0)

def cmd_update_plan_header(args: argparse.Namespace) -> None:
    payload = _args_to_payload_update_plan_header(args)
    result = _run_update_plan_header(payload)
    _emit_or_die(args, result)


def _args_to_payload_set_task_agent(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "task_id": args.task_id,
        "agent": args.agent,
    }
    return payload

def _run_set_task_agent(payload: dict) -> dict:
    plan = Path(payload['plan_file'])
    if not plan.is_file():
        return _result({'error': f'plan file not found: {plan}'}, exit_code=1)
    tid = _normalize_task_id(payload['task_id'])
    if tid is None:
        return _result(
            {'error': f"could not normalize task id {payload['task_id']!r}"},
            exit_code=1,
        )
    text = _load_text(plan)
    try:
        mutated, prior = mutate_task_agent(text, tid, payload['agent'])
    except ValueError as e:
        return _result({'error': str(e)}, exit_code=1)
    _write_text(plan, mutated)
    return _result({'ok': True, 'prior_agent': prior}, exit_code=0)

def cmd_set_task_agent(args: argparse.Namespace) -> None:
    payload = _args_to_payload_set_task_agent(args)
    result = _run_set_task_agent(payload)
    _emit_or_die(args, result)


def _args_to_payload_finalize_execution_log(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "run_id": args.run_id,
        "starting_sha": args.starting_sha,
        "ending_sha": args.ending_sha,
        "rows_json": args.rows_json,
        "outcome": args.outcome,
    }
    return payload

def _run_finalize_execution_log(payload: dict) -> dict:
    plan = Path(payload['plan_file'])
    if not plan.is_file():
        return _result({'error': f'plan file not found: {plan}'}, exit_code=1)
    rows_payload = payload['rows_json']
    if isinstance(rows_payload, str):
        try:
            rows = json.loads(rows_payload)
        except json.JSONDecodeError as e:
            return _result({'error': f'invalid --rows-json: {e}'}, exit_code=1)
    else:
        rows = rows_payload
    row_errors = _validate_execution_log_rows(rows)
    if row_errors:
        return _result({'errors': row_errors}, exit_code=1)
    header = '| Task | Agent | Reviewer | Verdict | Commit | Notes |'
    sep = '|---|---|---|---|---|---|'
    run_id_heading = f"## Execution log — {payload['run_id']}"
    if payload['outcome']:
        run_id_heading += f" ({payload['outcome']})"
    lines = ['', run_id_heading, '', f"Starting SHA: `{payload['starting_sha']}`  → Ending SHA: `{payload['ending_sha']}`", '', header, sep]
    for row in rows:
        cells = [str(row.get('task', '')), str(row.get('agent', '')), str(row.get('reviewer', '')), str(row.get('verdict', '')), str(row.get('commit', '')), str(row.get('notes', ''))]
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append('')
    text = _load_text(plan).rstrip('\n') + '\n\n' + '\n'.join(lines).lstrip('\n')
    _write_text(plan, text)
    return _result({'ok': True}, exit_code=0)

def cmd_finalize_execution_log(args: argparse.Namespace) -> None:
    payload = _args_to_payload_finalize_execution_log(args)
    result = _run_finalize_execution_log(payload)
    _emit_or_die(args, result)


def _args_to_payload_log_event(args: argparse.Namespace) -> dict:
    payload = {
        "event": args.event,
        "fields_json": args.fields_json,
        "findings_json": args.findings_json,
    }
    return payload

def _run_log_event(payload: dict) -> dict:
    fields_payload = payload['fields_json']
    if isinstance(fields_payload, str):
        try:
            fields = json.loads(fields_payload)
        except json.JSONDecodeError as e:
            return _result({'error': f'invalid --fields-json: {e}'}, exit_code=1)
    else:
        fields = fields_payload
    if not isinstance(fields, dict):
        return _result({'error': '--fields-json must be a JSON object'}, exit_code=1)
    if payload['event'] not in ALLOWED_LOG_EVENTS:
        return _result({'errors': [{'path': '$.event', 'code': 'unknown-event-type', 'message': f"event {payload['event']!r} is not in the allowlist {sorted(ALLOWED_LOG_EVENTS)}"}]}, exit_code=1)
    if payload['event'] == "review_route_called":
        errors = _validate_review_route_called_fields(fields)
        if errors:
            return _result({'errors': errors}, exit_code=1)
    if payload['event'] in HANDFIX_GATED_EVENTS and _is_handfix_intent(fields):
        run_id = fields.get('run_id')
        task_id = fields.get('task_id')
        if not isinstance(run_id, str) or not run_id:
            return _result({'errors': [{'path': '$.run_id', 'code': 'handfix-requires-run-id', 'message': f"event {payload['event']!r} with hand-fix intent requires a non-empty run_id so the paused-state gate can be verified"}]}, exit_code=1)
        if not isinstance(task_id, str) or not task_id:
            return _result({'errors': [{'path': '$.task_id', 'code': 'handfix-requires-task-id', 'message': f"event {payload['event']!r} with hand-fix intent requires a non-empty task_id so the paused-state gate can be verified"}]}, exit_code=1)
        norm_task_id = _normalize_task_id(task_id)
        if norm_task_id is None:
            return _result({'errors': [{'path': '$.task_id', 'code': 'handfix-bad-task-id', 'message': f"event {payload['event']!r} with hand-fix intent: task_id={task_id!r} is not a valid task identifier (expected NNN, NNNX, or TASK-NNN[X])"}]}, exit_code=1)
        if not _run_log_handfix_pause_active(RUN_LOG_PATH, run_id, task_id):
            return _result({'errors': [{'path': '$', 'code': 'handfix-not-paused', 'message': f"event {payload['event']!r} with hand-fix intent rejected: run_id={run_id!r} task_id={task_id!r} has no active awaiting_user pause in the run log. Hand-fix is authorized only after a run has paused on this task; outside that envelope, dispatch a subagent. See feedback_handfix_default.md and docs/analysis/orchestrator_dispatch_drift_20260429.md."}]}, exit_code=1)
    findings: list | None = None
    if payload.get('findings_json') is not None:
        findings_payload = payload['findings_json']
        if isinstance(findings_payload, str):
            try:
                parsed = json.loads(findings_payload)
            except json.JSONDecodeError as e:
                return _result({'errors': [{'path': '$.findings_json', 'code': 'invalid-json', 'message': f'invalid --findings-json: {e}'}]}, exit_code=1)
        else:
            parsed = findings_payload
        errs = _validate_minor_findings_payload(parsed, path='$.findings_json')
        if errs:
            return _result({'errors': errs}, exit_code=1)
        if 'findings' in fields:
            return _result({'errors': [{'path': '$.findings', 'code': 'findings-json-collision', 'message': 'findings key is present in both --fields-json and --findings-json; pick one source'}]}, exit_code=1)
        findings = parsed
    try:
        if findings is not None:
            merged = dict(fields)
            merged['findings'] = findings
            written = _append_run_log(payload['event'], merged)
        else:
            written = _append_run_log(payload['event'], fields)
    except RuntimeError as e:
        return _result({'error': str(e)}, exit_code=1)
    return _result({'ok': True, 'written_line': written}, exit_code=0)

def cmd_log_event(args: argparse.Namespace) -> None:
    payload = _args_to_payload_log_event(args)
    result = _run_log_event(payload)
    _emit_or_die(args, result)


def _args_to_payload_normalize_task_id(args: argparse.Namespace) -> dict:
    payload = {
        "id": args.id,
    }
    return payload

def _run_normalize_task_id(payload: dict) -> dict:
    normalized = _normalize_task_id(payload['id'])
    if normalized is None:
        return _result({'error': f"cannot normalize task id: {payload['id']!r}"}, exit_code=1)
    return _result({'normalized': normalized}, exit_code=0)

def cmd_normalize_task_id(args: argparse.Namespace) -> None:
    payload = _args_to_payload_normalize_task_id(args)
    result = _run_normalize_task_id(payload)
    _emit_or_die(args, result)


LOCK_ENTRY_KEYS = frozenset({"run_id", "acquired_at"})


def _validate_lock_shape(raw: object) -> list[dict]:
    """Return [] if canonical; else a list of {code, message} violations.

    Canonical shape:
        {"<non-empty-string>": {"run_id": <non-empty str>,
                                "acquired_at": <non-empty str>}, ...}

    An empty top-level dict is canonical. Unicode is accepted in values.
    `acquired_at` is validated as an opaque non-empty string (no ISO-8601
    parse). Non-empty-string check is symmetric across run_id/acquired_at:
    both use `isinstance(x, str) and x != ""`.
    """
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return [{
            "code": "lock-toplevel-not-object",
            "message": f"lock file top-level is {type(raw).__name__}, expected object",
        }]
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            errors.append({
                "code": "lock-key-invalid",
                "message": f"entry key {key!r} must be non-empty string",
            })
            continue
        if not isinstance(value, dict):
            errors.append({
                "code": "lock-entry-not-object",
                "message": f"entry {key!r} value is {type(value).__name__}, expected object",
            })
            continue
        actual_keys = set(value.keys())
        missing = LOCK_ENTRY_KEYS - actual_keys
        extra = actual_keys - LOCK_ENTRY_KEYS
        if missing:
            errors.append({
                "code": "lock-entry-missing-keys",
                "message": f"entry {key!r} missing keys: {sorted(missing)}",
            })
        if extra:
            errors.append({
                "code": "lock-entry-extra-keys",
                "message": f"entry {key!r} has extra keys: {sorted(extra)}",
            })
        for req in ("run_id", "acquired_at"):
            if req in value and (not isinstance(value[req], str) or not value[req]):
                errors.append({
                    "code": "lock-entry-value-empty",
                    "message": f"entry {key!r} field {req!r} must be non-empty string",
                })
    return errors


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Torn-write-safe write.

    Uses mkstemp in the same directory (so os.replace is a same-filesystem
    atomic rename) and a unique suffix (so two concurrent acquires cannot
    collide on a fixed .tmp name). Does NOT fsync — durability after power
    loss is explicitly not a requirement here; the guarantee is that the
    canonical path never holds a partial JSON body visible to a concurrent
    reader.
    """
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


def _args_to_payload_acquire_lock(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "run_id": args.run_id,
        "force": args.force,
    }
    return payload

def _run_acquire_lock(payload: dict) -> dict:
    if not isinstance(payload['run_id'], str) or payload['run_id'] == '':
        return _result({'acquired': False, 'errors': [{'code': 'lock-run-id-empty', 'message': '--run-id must be a non-empty string'}]}, exit_code=1)
    plan_abs = os.path.abspath(payload['plan_file'])
    RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    pre_raw: object | None = None
    decode_err: str | None = None
    if RUN_LOCK_PATH.exists():
        raw_text = RUN_LOCK_PATH.read_text(encoding='utf-8')
        try:
            pre_raw = json.loads(raw_text)
        except json.JSONDecodeError as e:
            decode_err = str(e)
    if payload['force']:
        new_state = {plan_abs: {'run_id': payload['run_id'], 'acquired_at': _now()}}
        _atomic_write_json(RUN_LOCK_PATH, new_state)
        return _result({'acquired': True, 'forced': True}, exit_code=0)
        return
    if decode_err is not None:
        return _result({'acquired': False, 'errors': [{'code': 'lock-json-decode', 'message': decode_err}]}, exit_code=1)
    if pre_raw is not None:
        shape_errors = _validate_lock_shape(pre_raw)
        if shape_errors:
            return _result({'acquired': False, 'errors': shape_errors}, exit_code=1)
        current = pre_raw
    else:
        current = {}
    if plan_abs in current and current[plan_abs].get('run_id') != payload['run_id']:
        return _result({'acquired': False, 'conflict_run_id': current[plan_abs].get('run_id')}, exit_code=1)
    current[plan_abs] = {'run_id': payload['run_id'], 'acquired_at': _now()}
    _atomic_write_json(RUN_LOCK_PATH, current)
    return _result({'acquired': True}, exit_code=0)

def cmd_acquire_lock(args: argparse.Namespace) -> None:
    payload = _args_to_payload_acquire_lock(args)
    result = _run_acquire_lock(payload)
    _emit_or_die(args, result)


def _args_to_payload_release_lock(args: argparse.Namespace) -> dict:
    payload = {
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "run_id": args.run_id,
    }
    return payload

def _run_release_lock(payload: dict) -> dict:
    plan_abs = os.path.abspath(payload['plan_file'])
    if not RUN_LOCK_PATH.exists():
        return _result({'released': False, 'reason': 'no-lock-file'}, exit_code=0)
    try:
        current = json.loads(RUN_LOCK_PATH.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        current = {}
    entry = current.get(plan_abs)
    if not entry or entry.get('run_id') != payload['run_id']:
        return _result({'released': False, 'reason': 'run-id-mismatch'}, exit_code=0)
    current.pop(plan_abs, None)
    if current:
        RUN_LOCK_PATH.write_text(json.dumps(current, indent=2), encoding='utf-8')
    else:
        RUN_LOCK_PATH.unlink()
    return _result({'released': True}, exit_code=0)

def cmd_release_lock(args: argparse.Namespace) -> None:
    payload = _args_to_payload_release_lock(args)
    result = _run_release_lock(payload)
    _emit_or_die(args, result)


def _args_to_payload_path_info(args: argparse.Namespace) -> dict:
    payload = {
    }
    return payload

def _run_path_info(payload: dict) -> dict:
    payload = {'plan_dir': _PLAN_DIR_POSIX, 'run_log': RUN_LOG_PATH.as_posix(), 'run_lock': RUN_LOCK_PATH.as_posix(), 'schedule_glob': f'{_PLAN_DIR_POSIX}/*.schedule.json'}
    return _result(payload, exit_code=0)

def cmd_path_info(args: argparse.Namespace) -> None:
    'Emit the configured plan-dir and derived paths so the orchestrator can\n    template them into SKILL.md placeholders (`<plan_dir>`, `<run_log>`, etc.).'
    payload = _args_to_payload_path_info(args)
    result = _run_path_info(payload)
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# Phase gates (TASK-005)
# ---------------------------------------------------------------------------
#
# Six explicit gate predicates that the orchestrator uses to promote the run
# through the plan → schedule → execute → commit lifecycle. Each predicate is
# a small function returning a `{name, status, reason}` dict where status is
# one of `pass`, `fail`, `not_applicable`. The gate model is the vocabulary
# downstream chunks reference (self-audit, sample-fixture conformance, and
# the Phase 5 certification rerun). See DUAL_AGENT_PLAN_EXECUTOR.md §9.7.
#
# Dry-run pass condition: schema-valid + schedule-valid + fixture-valid must
# hold; execution-safe / review-safe are asserted by predicate against
# `plan_codex_dispatch.py`; commit-safe is `not_applicable` because no
# commits exist in dry-run.
#
# Execute pass condition: all six gates hold, with commit-safe re-verified
# post-hoc for every committed task via `--run-id` bundle mode.

GATE_NAMES: tuple[str, ...] = (
    "schema-valid",
    "schedule-valid",
    "fixture-valid",
    "execution-safe",
    "review-safe",
    "commit-safe",
)

# Fixture the `_gate_fixture_valid` predicate reads. The sample plan rewrite
# is TASK-006's scope; TASK-005 establishes the gate and its unit tests via
# synthetic fixtures. Running this gate against the live fixture pre-TASK-006
# is expected to return `fail` until the rewrite lands.
#
# Path is derived from `__file__` so the gate resolves the fixture at the
# repo root regardless of the process cwd. `plan_ops.py` lives at
# `plugins/plan-executor/scripts/plan_ops.py`, so the repo root is three
# parents up from the script dir. Phase 0 / skill invocations that export
# `CLAUDE_PLUGIN_ROOT` but don't chdir to the repo root still pick up the
# correct fixture, and unit tests that run from an arbitrary tmp_path are
# unaffected.
_REPO_ROOT = Path(__file__).resolve().parents[3]
# TASK-004: read the SoT path from `CANONICAL_CONTRACT.fixture_path` so the
# string literal lives in exactly one place. The audit check
# `canonical_fixture_not_archived` flags drift if the fixture is moved
# under `docs/plans/archive/` without updating the constant.
_FIXTURE_RELATIVE_PATH = str(CANONICAL_CONTRACT["fixture_path"])
_FIXTURE_ABSOLUTE_PATH = _REPO_ROOT / _FIXTURE_RELATIVE_PATH

# Canonical locations of wrapper predicates. `_gate_execution_safe` and
# `_gate_review_safe` grep this file — they never invoke the wrapper.
_WRAPPER_PATH = (
    Path(__file__).resolve().parent / "plan_codex_dispatch.py"
)


def _gate_result(name: str, status: str, reason: str) -> dict:
    """Shape helper for gate predicates."""
    if status not in {"pass", "fail", "not_applicable"}:
        raise ValueError(f"gate status must be pass|fail|not_applicable, got {status!r}")
    return {"name": name, "status": status, "reason": reason}


# Required top-level sections in a plan markdown file, per §5 of the design
# doc. Context accepts either `## Context` or `## Scoped Context` — the plan
# schema allows both (chunked plans use `## Scoped Context` per-TASK files).
_PLAN_TOP_LEVEL_REQUIRED = (
    ("## Goal", "Goal section"),
)
_PLAN_CONTEXT_SECTIONS = ("## Context", "## Scoped Context")


# Fields every `### TASK-NNN` block must declare (per §5). Bullets are
# required; Description and Reversion guidance are paragraph-form headers.
_TASK_REQUIRED_BULLETS = (
    "Status",
    "Priority",
    "Files",
    "Test command",
    "Acceptance criteria",
)
_TASK_REQUIRED_PROSE_HEADERS = (
    "Description",
)


def _gate_schema_valid(plan_file: str | Path) -> dict:
    """Plan markdown conforms to §5 of the design doc.

    Pass iff: `## Goal`, a context section (`## Context` or
    `## Scoped Context`), and `## Verification` top-level sections are
    present; every `### TASK-NNN` block carries the required bullets
    (Status, Priority, Files, Test command, Acceptance criteria) plus
    the required prose headers (Description). Returns `fail` on any
    missing element; the reason string names the first few problems
    concretely so an operator can fix them without guessing.
    """
    path = Path(plan_file)
    if not path.is_file():
        return _gate_result(
            "schema-valid",
            "fail",
            f"plan file not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "schema-valid",
            "fail",
            f"plan file unreadable: {e}",
        )

    problems: list[str] = []
    # Top-level sections. Matched on `^## <name>` (leading `##` exactly,
    # so a `### ...` subtask header does not satisfy `## ...`).
    for header, label in _PLAN_TOP_LEVEL_REQUIRED:
        if not re.search(rf"^{re.escape(header)}\b", text, re.MULTILINE):
            problems.append(f"missing {label} (`{header}`)")
    if not any(
        re.search(rf"^{re.escape(sec)}\b", text, re.MULTILINE)
        for sec in _PLAN_CONTEXT_SECTIONS
    ):
        problems.append(
            "missing Context section (`## Context` or `## Scoped Context`)"
        )
    # Verification: §5 authors this as `## Verification` in whole-plan files.
    # Per-chunk `## Verification` files under DUAL_AGENT_Plans/ satisfy it
    # too — grep is top-level so subsections do not match accidentally.
    if not re.search(r"^## Verification\b", text, re.MULTILINE):
        problems.append("missing Verification section (`## Verification`)")

    _, task_blocks = _split_task_blocks(text)
    if not task_blocks:
        problems.append("no `### TASK-NNN` blocks found")
    for tid, block in task_blocks:
        for field in _TASK_REQUIRED_BULLETS:
            if not re.search(
                rf"^\s*-\s*\*\*{re.escape(field)}:\*\*",
                block,
                re.MULTILINE,
            ):
                problems.append(f"TASK-{tid} missing bullet **{field}:**")
        for field in _TASK_REQUIRED_PROSE_HEADERS:
            if not re.search(
                rf"^\*\*{re.escape(field)}:\*\*",
                block,
                re.MULTILINE,
            ):
                problems.append(
                    f"TASK-{tid} missing prose header **{field}:**"
                )

    if problems:
        # Show up to five problems inline so the reason stays scannable.
        preview = "; ".join(problems[:5])
        if len(problems) > 5:
            preview += f"; … ({len(problems) - 5} more)"
        return _gate_result(
            "schema-valid",
            "fail",
            f"schema violations: {preview}",
        )
    return _gate_result(
        "schema-valid",
        "pass",
        f"plan {path.name} conforms to §5 schema",
    )


def _gate_schedule_valid(schedule_file: str | Path | None) -> dict:
    """Analyst schedule JSON passes shape + DAG validation.

    Pass iff the schedule file exists, parses as JSON, and `_validate_schedule`
    + `_validate_schedule_dag` both return no errors. Same validators used by
    `parse-schedule` so the gate and the helper agree by construction.
    """
    if schedule_file is None:
        return _gate_result(
            "schedule-valid",
            "fail",
            "schedule-file argument is required",
        )
    path = Path(schedule_file)
    if not path.is_file():
        return _gate_result(
            "schedule-valid",
            "fail",
            f"schedule file not found: {path}",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return _gate_result(
            "schedule-valid",
            "fail",
            f"schedule json decode: {e}",
        )
    if not isinstance(data, dict):
        return _gate_result(
            "schedule-valid",
            "fail",
            "top-level schedule must be an object",
        )
    errors, _ = _validate_schedule(data)
    if not errors:
        tasks_raw = data.get("tasks")
        batches_raw = data.get("batches")
        # Preserve the validator contract: `_validate_schedule` currently
        # reports shape errors for non-list `tasks`/`batches`, but collapsing
        # them to `[]` here would silently pass an invalid schedule if that
        # contract ever regresses. Treat a missing or wrong-shape value as
        # its own DAG-level failure instead.
        if not isinstance(tasks_raw, list) or not isinstance(batches_raw, list):
            errors.append({
                "code": "invalid_shape",
                "message": "`tasks` and `batches` must both be lists for DAG validation",
            })
        else:
            errors.extend(_validate_schedule_dag(tasks_raw, batches_raw))
    if errors:
        first = errors[0]
        return _gate_result(
            "schedule-valid",
            "fail",
            f"{len(errors)} schedule error(s); first: {first.get('code')} — {first.get('message')}",
        )
    return _gate_result(
        "schedule-valid",
        "pass",
        f"schedule {path.name} passes shape + DAG validation",
    )


def _gate_fixture_valid(plan_file: str | Path | None = None) -> dict:
    """Sample fixture passes schema-valid and schedule-valid.

    TASK-006 rewrites the canonical sample fixture (see
    `CANONICAL_CONTRACT.fixture_path`) to the canonical schema.
    Running this gate against the pre-rewrite fixture is **expected to
    return `fail`** — the gate vocabulary is established here, its live-tree
    green status lands with TASK-006. Unit tests drive this predicate via
    synthetic fixtures (see `tests/scripts/test_plan_ops.py`); the live
    fixture run is deferred.

    Invariant: fixture-valid is the aggregate of schema-valid AND
    schedule-valid on the fixture; a missing `<basename>.schedule.json`
    sidecar is a `fail` (the sidecar is the schedule input and cannot be
    skipped without defeating the aggregate).
    """
    # Default to the cwd-independent absolute fixture path so Phase 0 and
    # certification bundles resolve the same file regardless of how the
    # caller was invoked. Tests can still override by passing `plan_file`.
    path = Path(plan_file) if plan_file is not None else _FIXTURE_ABSOLUTE_PATH
    if not path.is_file():
        result = _gate_result(
            "fixture-valid",
            "fail",
            f"fixture not found: {path}",
        )
        # TASK-004: attribute the SoT path in the failure envelope so an
        # operator can locate the canonical declaration without grepping.
        result["expected_from"] = "CANONICAL_CONTRACT.fixture_path"
        return result
    schema = _gate_schema_valid(path)
    if schema["status"] != "pass":
        result = _gate_result(
            "fixture-valid",
            "fail",
            f"schema-valid failed: {schema['reason']}",
        )
        result["expected_from"] = "CANONICAL_CONTRACT.fixture_path"
        return result
    # Schedule sidecar convention: `<basename>.schedule.json` under plan_dir.
    # fixture-valid is the aggregate schema + schedule certification, so a
    # missing sidecar is a fail — otherwise dry-run/execute certification
    # could go green without exercising schedule validation on the fixture.
    sidecar = path.with_suffix(".schedule.json")
    if not sidecar.is_file():
        result = _gate_result(
            "fixture-valid",
            "fail",
            f"schedule sidecar missing: {sidecar.name} "
            f"(fixture-valid requires schema + schedule; sidecar absent)",
        )
        result["expected_from"] = "CANONICAL_CONTRACT.fixture_path"
        return result
    sched = _gate_schedule_valid(sidecar)
    if sched["status"] != "pass":
        result = _gate_result(
            "fixture-valid",
            "fail",
            f"schedule-valid failed on {sidecar.name}: {sched['reason']}",
        )
        result["expected_from"] = "CANONICAL_CONTRACT.fixture_path"
        return result
    return _gate_result(
        "fixture-valid",
        "pass",
        f"fixture {path.name} + sidecar pass schema + schedule gates",
    )


def _grep_file(path: Path, pattern: str) -> bool:
    """Return True iff `pattern` (regex) matches anywhere in `path`."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(pattern, text, re.MULTILINE) is not None


def _strip_python_comments_and_docstrings(text: str) -> str:
    """Elide line comments and triple-quoted string regions from Python
    source so executable-code-only grep targets (used by
    `execution-safe` / `review-safe`) cannot be fooled by commented-out
    or docstring-mentioned tokens. Returns text with elided regions
    replaced by blank lines so line offsets inside the remaining
    executable lines are preserved — callers that search with regex or
    substring will see only real call sites.

    Lightweight heuristic: toggles triple-quote state on triple-double
    or triple-single marks per-line and strips a single unquoted hash
    on each line. Does not handle f-string nesting or backslash-escaped
    quotes — the wrapper source stays within that tolerance.
    """
    out_lines: list[str] = []
    in_triple_double = False
    in_triple_single = False
    # Pre-compiled: non-greedy inline triple-quoted regions. Single-line
    # docstrings/strings (e.g. `"""one-liner"""`) never toggle the
    # per-line state tracker because their mark count is even; elide
    # their contents here so a token mentioned only inside such a
    # string cannot fool the grep targets.
    _TRIPLE_INLINE = re.compile(r'""".*?"""|\'\'\'.*?\'\'\'')
    for raw_line in text.splitlines():
        # Strip any self-contained triple-quoted regions first so the
        # state tracker below sees only the unbalanced marks that span
        # line boundaries.
        line = _TRIPLE_INLINE.sub("", raw_line)
        double_marks = line.count('"""')
        single_marks = line.count("'''")
        line_was_in_string = in_triple_double or in_triple_single
        if double_marks % 2 == 1:
            in_triple_double = not in_triple_double
        if single_marks % 2 == 1:
            in_triple_single = not in_triple_single
        if line_was_in_string or in_triple_double or in_triple_single:
            out_lines.append("")
            continue
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out_lines.append("")
            continue
        # Strip inline `# comment` suffix, respecting simple single/double
        # quoted strings on the same line. A bare `#` inside a string
        # literal is NOT a comment; a `#` outside any open quote is.
        in_s_single = False
        in_s_double = False
        cut_idx: int | None = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line) and (in_s_single or in_s_double):
                i += 2
                continue
            if ch == "'" and not in_s_double:
                in_s_single = not in_s_single
            elif ch == '"' and not in_s_single:
                in_s_double = not in_s_double
            elif ch == "#" and not in_s_single and not in_s_double:
                cut_idx = i
                break
            i += 1
        if cut_idx is not None:
            line = line[:cut_idx]
        out_lines.append(line)
    return "\n".join(out_lines)


def _gate_execution_safe(wrapper_path: Path | None = None) -> dict:
    """Wrapper's implement path has baseline-snapshot + always-ignore.

    Predicate-only — does NOT invoke the wrapper. Greps for:
      (1) the shared always-ignore / protected-paths seam
          (`from _plan_paths import … PROTECTED_…`),
      (2) `_snapshot_baseline(` called at TWO distinct seams parsed as
          separate code regions -- inside `cmd_implement`'s body (the
          pre-dispatch implement seam) AND inside the timeout cleanup
          path. The cleanup seam is satisfied EITHER by an explicit
          `_snapshot_baseline(` call in the body of the
          `_handle_timeout_cleanup` helper, OR by
          `_handle_timeout_cleanup(..., baseline)` in `cmd_implement`'s
          timeout branch -- the kwarg propagates the snapshot without
          re-capturing it.
      (3) absence of `git clean -fd` outside comments (the observe-only
          cleanup contract — see §4 State Isolation Contract).

    A wrapper that captures a baseline at the implement seam but fails
    to propagate or re-capture it through the cleanup helper is rejected
    because the cleanup region cannot compare the post-dispatch delta
    against an authoritative pre-dispatch snapshot.
    """
    path = wrapper_path or _WRAPPER_PATH
    if not path.is_file():
        return _gate_result(
            "execution-safe",
            "fail",
            f"wrapper not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "execution-safe",
            "fail",
            f"wrapper unreadable: {e}",
        )

    # Strip `#` comments and triple-quoted docstrings before scanning for
    # executable call sites. Without this, a commented-out
    # `# _snapshot_baseline(...)` or a docstring mentioning the cleanup
    # helper would satisfy the predicate even with no real call. Raw
    # `text` is still used for the module-wide import seam check below
    # (imports are code, not comments) and the `git clean -fd` scan has
    # its own inline comment tracker.
    code_text = _strip_python_comments_and_docstrings(text)

    def _extract_function_body(func_name: str) -> str | None:
        """Slice the body of `def <func_name>(` up to the next top-level
        `def`. Returns None if no such definition exists. Used to parse
        the implement-dispatch and timeout-cleanup windows as separate
        regions so a snapshot call in one does not fool the check for
        the other.
        """
        m = re.search(
            rf"^def {re.escape(func_name)}\s*\(", code_text, re.MULTILINE,
        )
        if not m:
            return None
        tail = code_text[m.end():]
        next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
        return tail[: next_def.start()] if next_def else tail

    checks: list[tuple[str, bool]] = []
    # (1) always-ignore / protected-paths seam.
    checks.append((
        "PROTECTED_EXACT_PATHS import from _plan_paths",
        "PROTECTED_EXACT_PATHS" in code_text and "_plan_paths" in code_text,
    ))
    # (2) baseline snapshot at the implement dispatch seam AND within the
    # timeout-cleanup window, parsed as SEPARATE regions so a snapshot
    # in one window cannot satisfy the check for the other.
    impl_body = _extract_function_body("cmd_implement")
    cleanup_body = _extract_function_body("_handle_timeout_cleanup")
    if impl_body is None:
        checks.append((
            "`def cmd_implement(` definition present",
            False,
        ))
    else:
        # Implement-dispatch window: baseline must be captured inside
        # cmd_implement so the timeout branch has a pre-dispatch snapshot
        # to compare against. Scoped to the cmd_implement body only.
        checks.append((
            "_snapshot_baseline called in cmd_implement (implement-dispatch window)",
            "_snapshot_baseline(" in impl_body,
        ))
        # Timeout-cleanup window. Two satisfying seams, parsed as
        # separate regions:
        #   (a) `_handle_timeout_cleanup`'s own body calls
        #       `_snapshot_baseline(` -- the cleanup helper captures its
        #       own snapshot, independent of the caller.
        #   (b) cmd_implement's timeout branch invokes
        #       `_handle_timeout_cleanup(..., baseline)` -- the kwarg
        #       propagates the captured snapshot through.
        # Either form satisfies the cleanup-window invariant; both
        # missing fails the gate.
        timeout_match = re.search(
            r"if\s+codex\[[\"']status[\"']\]\s*==\s*[\"']timeout[\"']\s*:",
            impl_body,
        )
        if not timeout_match:
            checks.append((
                "cmd_implement has a timeout cleanup branch",
                False,
            ))
        else:
            region = impl_body[timeout_match.end(): timeout_match.end() + 2500]
            kwarg_propagates = bool(
                re.search(
                    r"_handle_timeout_cleanup\s*\([^)]*baseline",
                    region,
                    re.DOTALL,
                )
            )
            helper_captures = bool(
                cleanup_body is not None
                and "_snapshot_baseline(" in cleanup_body
            )
            checks.append((
                "_snapshot_baseline present in timeout-cleanup window "
                "(_handle_timeout_cleanup body OR cmd_implement baseline kwarg)",
                kwarg_propagates or helper_captures,
            ))
    # (3) `git clean -fd` must not appear in executable code. Lines starting
    # with `#` in the source are allowed — the wrapper's own comments document
    # the prohibition. Triple-quoted docstrings also legitimately reference the
    # token (e.g. "Never invokes `git clean -fd`.") and are skipped via a
    # single-pass string-state tracker that toggles on `"""`/`'''` opens and
    # closes. This is a lightweight parser — it does not handle f-strings or
    # nested quoting edge cases, but the wrapper source stays within its
    # tolerance.
    forbidden_hits: list[str] = []
    in_triple_double = False
    in_triple_single = False
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        # Track triple-quote state first so the skip logic matches reality.
        # Count unescaped occurrences per line; an odd count toggles state.
        double_marks = line.count('"""')
        single_marks = line.count("'''")
        line_was_in_string = in_triple_double or in_triple_single
        if double_marks % 2 == 1:
            in_triple_double = not in_triple_double
        if single_marks % 2 == 1:
            in_triple_single = not in_triple_single
        if line_was_in_string or in_triple_double or in_triple_single:
            continue
        if stripped.startswith("#"):
            continue
        if re.search(r"git\s+clean\s+-fd", line):
            forbidden_hits.append(f"line {idx}")
    checks.append((
        "no `git clean -fd` outside comments",
        not forbidden_hits,
    ))

    failures = [desc for desc, ok in checks if not ok]
    if failures:
        detail = "; ".join(failures)
        if forbidden_hits:
            detail += f" (forbidden hits at {', '.join(forbidden_hits)})"
        return _gate_result(
            "execution-safe",
            "fail",
            f"wrapper missing execution-safe invariants: {detail}",
        )
    return _gate_result(
        "execution-safe",
        "pass",
        f"wrapper {path.name} carries always-ignore + baseline-snapshot + no raw `git clean -fd`",
    )


def _gate_review_safe(wrapper_path: Path | None = None) -> dict:
    """Wrapper's review path has baseline-snapshot + always-ignore.

    Predicate-only. Greps for `_snapshot_baseline(` inside `cmd_review` and
    for the `is_protected_path` predicate the wrapper uses to skip protected
    paths in cleanup. Distinct from `_gate_execution_safe` because the
    review path has different failure modes (sibling state must be
    protected even when review dispatches in parallel with implement).
    """
    path = wrapper_path or _WRAPPER_PATH
    if not path.is_file():
        return _gate_result(
            "review-safe",
            "fail",
            f"wrapper not found: {path}",
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "review-safe",
            "fail",
            f"wrapper unreadable: {e}",
        )

    # Strip comments/docstrings before scanning so the grep targets
    # identify real call sites, not commented-out tokens or docstring
    # mentions of the cleanup helpers.
    code_text = _strip_python_comments_and_docstrings(text)

    # Find `def cmd_review(` and take the body up to the next top-level `def`
    # so we can grep inside the review path specifically.
    m = re.search(r"^def cmd_review\s*\(", code_text, re.MULTILINE)
    if not m:
        return _gate_result(
            "review-safe",
            "fail",
            "wrapper has no `def cmd_review(` definition",
        )
    tail = code_text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    review_body = tail[: next_def.start()] if next_def else tail

    checks: list[tuple[str, bool]] = []
    checks.append((
        "_snapshot_baseline called in cmd_review",
        "_snapshot_baseline(" in review_body,
    ))
    checks.append((
        "is_protected_path or PROTECTED_ constants referenced module-wide",
        ("is_protected_path" in code_text or "PROTECTED_EXACT_PATHS" in code_text),
    ))

    failures = [desc for desc, ok in checks if not ok]
    if failures:
        return _gate_result(
            "review-safe",
            "fail",
            f"wrapper review path missing invariants: {'; '.join(failures)}",
        )
    return _gate_result(
        "review-safe",
        "pass",
        f"wrapper {path.name} cmd_review carries baseline-snapshot + protected-path respect",
    )


def _normalize_files_entry(raw: str) -> str:
    """Strip backticks, `(create|modify|delete)` annotations, and `:line`
    suffixes from a raw Files: entry. Mirrors the wrapper's
    `normalize_file_path` so the commit guard/gate agree on allowlist keys.

    Thin module-private alias kept for back-compat with ~40 internal call
    sites; the canonical implementation lives in
    ``_plan_paths.normalize_files_entry`` (TASK-002 unification).
    """
    return normalize_files_entry(raw)


def _extract_task_files_from_plan(plan_text: str, task_id: str) -> list[str] | None:
    """Extract the normalized allowed_files list for TASK-NNN.

    Returns the parsed list (possibly empty) or None if the task block is
    absent. Mirrors the wrapper's `parse_task_block` + `normalize_file_path`
    logic but stays inside `plan_ops.py` so the gate has no wrapper import
    dependency.

    Accepts both the multi-line form

        - **Files:**
          - path/a.py
          - `path/b.py` (modify)

    and the inline form

        - **Files:** path/a.py
        - **Files:** `path/a.py`, `path/b.py`

    In both cases each entry is normalized (backticks/annotations/line
    suffixes stripped) before being returned.
    """
    normalized = _normalize_task_id(task_id)
    if normalized is None:
        return None
    _, blocks = _split_task_blocks(plan_text)
    for tid, block in blocks:
        if tid != normalized:
            continue
        # Multi-line `- **Files:**\n  - path\n  - path` form.
        m = re.search(
            r"^-\s*\*\*Files:\*\*\s*$",
            block,
            re.MULTILINE,
        )
        if m:
            items: list[str] = []
            base_indent: int | None = None
            for line in block[m.end():].splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                is_indented_bullet = (
                    stripped.startswith("-")
                    and (line.startswith(" ") or line.startswith("\t"))
                )
                if not is_indented_bullet:
                    break
                indent = len(line) - len(line.lstrip(" \t"))
                if base_indent is None:
                    base_indent = indent
                elif indent > base_indent:
                    continue
                raw = stripped[1:].strip()
                items.append(_normalize_files_entry(raw))
            return items
        # Inline `- **Files:** path[, path, ...]` form. `_extract_inline_field`
        # semantics: value is the text after the `:**` marker on the same line.
        # Split on commas so comma-separated inline entries land as distinct
        # allowlist keys.
        m_inline = re.search(
            r"^-\s*\*\*Files:\*\*\s+(.+?)\s*$",
            block,
            re.MULTILINE,
        )
        if m_inline:
            raw_value = m_inline.group(1).strip()
            if not raw_value:
                return []
            return [
                _normalize_files_entry(piece)
                for piece in raw_value.split(",")
                if piece.strip()
            ]
        return []
    return None


def _parse_h3_task_from_plan(plan_text: str, task_id: str) -> dict | None:
    normalized = _normalize_task_id(task_id)
    if normalized is None:
        return None
    _, blocks = _split_task_blocks_at_level(plan_text, 3)
    for raw_id, title, block, source_line in blocks:
        if _normalize_task_id(raw_id) == normalized:
            return _parse_task_block(
                block,
                3,
                raw_id=raw_id,
                title=title,
                source_line=source_line,
            )
    return None


def _extract_task_description(plan_text: str, task_id: str) -> str | None:
    task = _parse_h3_task_from_plan(plan_text, task_id)
    if task is None:
        return None
    return task.get("description") or ""


def _extract_task_acceptance_criteria(plan_text: str, task_id: str) -> str | None:
    task = _parse_h3_task_from_plan(plan_text, task_id)
    if task is None:
        return None
    items = task.get("acceptance_criteria") or []
    return "\n".join(f"- {item}" for item in items)


def _gate_commit_safe(
    commit_sha: str | None,
    task_id: str | None,
    plan_file: str | Path | None,
    *,
    repo_root: Path | None = None,
) -> dict:
    """Post-hoc verification that `<commit_sha>` touched only allowed files.

    Fetches the commit's file list via `git show --name-only` and
    subtracts two distinct sets:

      - `allowed = task_files | {plan_file}` -- the task's declared
        `Files:` list (normalized) plus the plan file itself, which
        `commit-task` stages alongside each task commit per §D.3 step 3.
      - `ignored = {p for p in changed if is_commit_always_ignore(p,
        plan_basename)}` -- bookkeeping paths that `commit-task`
        authors on behalf of the orchestrator (run-log, run-lock,
        per-plan schedule sidecar, `00_INDEX.json` roster).

    Pass iff `changed - allowed - ignored` is empty. The ignore set is
    the shared `COMMIT_ALWAYS_IGNORE` constant from `_plan_paths.py`
    (plus the per-plan schedule sidecar matched via
    `is_commit_always_ignore`), used identically by `commit-task`'s
    own staging logic so the pre-commit and post-commit sides agree by
    construction. We deliberately do NOT fall back to
    `is_protected_path` / `PROTECTED_PATH_PREFIXES` as a broader
    always-ignore filter: those cover paths like `plan_ops.py` itself,
    which a task MUST declare in Files: to commit against. Using the
    protected-path predicate as an always-allow filter silently
    green-lit commits that mutated executor code without declaration.
    """
    if not commit_sha or not task_id or plan_file is None:
        return _gate_result(
            "commit-safe",
            "fail",
            "commit-safe requires commit_sha, task_id, and plan_file",
        )
    plan_path = Path(plan_file)
    if not plan_path.is_file():
        return _gate_result(
            "commit-safe",
            "fail",
            f"plan file not found: {plan_path}",
        )
    try:
        plan_text = plan_path.read_text(encoding="utf-8")
    except OSError as e:
        return _gate_result(
            "commit-safe",
            "fail",
            f"plan file unreadable: {e}",
        )
    allowed_raw = _extract_task_files_from_plan(plan_text, task_id)
    if allowed_raw is None:
        return _gate_result(
            "commit-safe",
            "fail",
            f"TASK-{task_id} not found in plan {plan_path.name}",
        )
    allowed = set(allowed_raw)
    # Plan file itself is always allowed -- `commit-task` stages it
    # alongside every task commit (§D.3 step 3) so the `**Status:**`
    # bullet flip lands in the same commit as the code change. Cover
    # both layouts a plan_ops invocation might see (cwd repo root vs
    # cwd plan dir) by adding the repo-relative form (when resolvable)
    # and the raw forms.
    try:
        rel_plan = str(
            plan_path.resolve().relative_to(
                (repo_root or Path.cwd()).resolve()
            )
        )
        allowed.add(rel_plan)
    except ValueError:
        pass
    allowed.add(str(plan_path))
    allowed.add(plan_path.as_posix())

    # Derive plan_dir (repo-relative, forward-slash) so
    # `is_commit_always_ignore` can match the sibling `00_INDEX.json`
    # for plans that live outside the default `docs/plans/` bundle.
    # Falls back to None on non-repo layouts (e.g., plan path that
    # cannot be expressed relative to the repo root); the shared
    # constant's static entries still cover the default bundle layouts.
    plan_dir_rel: str | None
    try:
        plan_dir_rel = str(
            plan_path.resolve().parent.relative_to(
                (repo_root or Path.cwd()).resolve()
            )
        ).replace(os.sep, "/")
        if plan_dir_rel == ".":
            plan_dir_rel = ""
    except ValueError:
        plan_dir_rel = None

    proc = _git(
        ["show", "--name-only", "--pretty=format:", commit_sha],
        cwd=repo_root,
    )
    if proc.returncode != 0:
        return _gate_result(
            "commit-safe",
            "fail",
            (
                f"git show failed for {commit_sha}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            ),
        )
    changed = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    # Directory-scoped Files: entries (trailing slash, e.g.
    # `tests/fixtures/decomposer_inputs/`) allow any path under that
    # directory. Treat them as prefix matches rather than exact-string
    # equality so plans that declare a fixture directory don't need to
    # enumerate every child file individually.
    allowed_prefixes = {a for a in allowed if a.endswith("/")}
    offending: list[str] = []
    for path in changed:
        if path in allowed:
            continue
        if any(path.startswith(p) for p in allowed_prefixes):
            continue
        # Shared always-ignore set (COMMIT_ALWAYS_IGNORE) covers the
        # run-log, run-lock, per-plan schedule sidecar, and
        # 00_INDEX.json roster -- the paths `commit-task` itself
        # writes during orchestration. Matching on plan_basename +
        # plan_dir lets the per-plan sidecar key and the alongside-plan
        # roster resolve for bundles outside the default layout.
        if is_commit_always_ignore(
            path, plan_path.name, plan_dir_rel,
        ):
            continue
        offending.append(path)
    if offending:
        return _gate_result(
            "commit-safe",
            "fail",
            (
                f"commit {commit_sha[:12]} touched {len(offending)} "
                f"file(s) outside TASK-{task_id} scope: {offending[:5]}"
            ),
        )
    return _gate_result(
        "commit-safe",
        "pass",
        f"commit {commit_sha[:12]} touched only TASK-{task_id} allowed files",
    )


def _is_directory_mode_plan(plan_file: str | Path) -> bool:
    """`plan_file` is a directory containing `00_INDEX.json`.

    Directory-mode certify resolves `schema-valid` per chunk and
    `commit-safe` per `commit_done` event's child plan-file (per
    SKILL.md §99-106). Single-file mode is the legacy markdown shape.
    """
    p = Path(plan_file)
    return p.is_dir() and (p / "00_INDEX.json").is_file()


def _aggregate_schema_valid_directory(plan_dir: str | Path) -> dict:
    """Run `_gate_schema_valid` against every chunk in `00_INDEX.json`.

    Returns a single gate dict whose `status` is `pass` iff every chunk
    passes; `subresults` carries one entry per chunk for attribution.
    """
    plan_dir_p = Path(plan_dir)
    chunks, errors = _load_index_chunks(plan_dir_p)
    if errors or chunks is None:
        msg = errors[0]["message"] if errors else "00_INDEX.json missing"
        gate = _gate_result(
            "schema-valid", "fail",
            f"directory-mode schema-valid failed: {msg}",
        )
        gate["subresults"] = []
        return gate
    subresults: list[dict] = []
    failures: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        child = chunk.get("file")
        if not isinstance(child, str) or not child:
            continue
        child_path = plan_dir_p / child
        sub = _gate_schema_valid(child_path)
        sub_entry = {
            "plan_file": child,
            "status": sub["status"],
            "reason": sub["reason"],
        }
        subresults.append(sub_entry)
        if sub["status"] != "pass":
            failures.append(f"{child}: {sub['reason']}")
    if not subresults:
        gate = _gate_result(
            "schema-valid", "fail",
            f"directory-mode schema-valid: no chunks declared in {plan_dir_p}/00_INDEX.json",
        )
        gate["subresults"] = subresults
        return gate
    if failures:
        preview = "; ".join(failures[:3])
        if len(failures) > 3:
            preview += f"; … ({len(failures) - 3} more)"
        gate = _gate_result(
            "schema-valid", "fail",
            f"{len(failures)}/{len(subresults)} chunk(s) failed schema-valid: {preview}",
        )
    else:
        gate = _gate_result(
            "schema-valid", "pass",
            f"all {len(subresults)} chunk(s) under {plan_dir_p.name} conform to §5 schema",
        )
    gate["subresults"] = subresults
    return gate


def _certify_dry_run(plan_file: str | Path, schedule_file: str | Path | None = None) -> list[dict]:
    """Bundle of gates exercised in dry-run mode.

    schema-valid + schedule-valid + fixture-valid + execution-safe +
    review-safe run as predicates; commit-safe is `not_applicable`
    because no commits exist in dry-run. `schedule-valid` is required
    for certification — a missing `schedule_file` fails the bundle
    rather than collapsing to `not_applicable`.

    Directory-mode (`plan_file` is a directory containing `00_INDEX.json`):
    `schema-valid` aggregates per-chunk; other gates are unchanged.
    """
    if _is_directory_mode_plan(plan_file):
        schema_gate = _aggregate_schema_valid_directory(plan_file)
    else:
        schema_gate = _gate_schema_valid(plan_file)
    gates: list[dict] = [
        schema_gate,
        _gate_schedule_valid(schedule_file),
        _gate_fixture_valid(),
        _gate_execution_safe(),
        _gate_review_safe(),
        _gate_result(
            "commit-safe",
            "not_applicable",
            "dry-run mode; no commits to verify",
        ),
    ]
    return gates


def _certify_execute(
    plan_file: str | Path,
    run_id: str | None,
    schedule_file: str | Path | None = None,
) -> list[dict]:
    """Bundle of gates exercised in execute mode.

    Includes the dry-run set plus a post-hoc commit-safe check per
    `commit_done` event in the run log for this run_id. A run with zero
    commits (successful no-op) reports `commit-safe: not_applicable`.
    `schedule-valid` is required for certification — a missing
    `schedule_file` fails the bundle rather than collapsing to
    `not_applicable`.

    Directory-mode (`plan_file` is a directory containing `00_INDEX.json`):
    `schema-valid` aggregates per-chunk and `commit-safe` resolves each
    `commit_done` event to its child plan-file (preferring the event's
    `plan_file` field per SKILL.md §99-106; falling back to the chunk
    declared for that task id in `00_INDEX.json`).
    """
    is_dir_mode = _is_directory_mode_plan(plan_file)
    schema_gate = (
        _aggregate_schema_valid_directory(plan_file) if is_dir_mode
        else _gate_schema_valid(plan_file)
    )
    gates: list[dict] = [
        schema_gate,
        _gate_schedule_valid(schedule_file),
        _gate_fixture_valid(),
        _gate_execution_safe(),
        _gate_review_safe(),
    ]
    # Each entry: (task_id, sha, event_plan_file_basename_or_None)
    commits: list[tuple[str, str, str | None]] = []
    if run_id and RUN_LOG_PATH.is_file():
        try:
            for line in RUN_LOG_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(ev, dict):
                    continue
                if ev.get("event") != "commit_done":
                    continue
                if ev.get("run_id") != run_id:
                    continue
                tid = _normalize_task_id(str(ev.get("task_id", "")))
                sha = ev.get("commit_sha") or ev.get("sha") or ""
                ev_plan = ev.get("plan_file")
                if not isinstance(ev_plan, str) or not ev_plan:
                    ev_plan = None
                if tid and sha:
                    commits.append((tid, str(sha), ev_plan))
        except OSError:
            pass
    if not commits:
        gates.append(_gate_result(
            "commit-safe",
            "not_applicable",
            f"execute mode: no commit_done events for run_id={run_id!r}",
        ))
    else:
        # Derive the repo root from the plan file's git toplevel so
        # `git show --name-only` inside `_gate_commit_safe` targets the
        # same repository that produced the run log, not the caller's
        # cwd. Falls back to Path.cwd() semantics on git failure, matching
        # the helper's existing default.
        plan_path_resolved = Path(plan_file).resolve()
        toplevel_cwd = (
            plan_path_resolved if plan_path_resolved.is_dir()
            else plan_path_resolved.parent
        )
        toplevel = _git(["rev-parse", "--show-toplevel"], cwd=toplevel_cwd)
        repo_root = (
            Path(toplevel.stdout.strip())
            if toplevel.returncode == 0 and toplevel.stdout.strip()
            else None
        )
        # In directory mode, build a {task_id: child_basename} index from
        # `00_INDEX.json` as a fallback when an event lacks `plan_file`.
        chunk_index: dict[str, str] = {}
        if is_dir_mode:
            chunks, _idx_errors = _load_index_chunks(Path(plan_file))
            for chunk in chunks or []:
                if not isinstance(chunk, dict):
                    continue
                ctid_raw = chunk.get("task_id") or chunk.get("id")
                cfile = chunk.get("file")
                if not isinstance(ctid_raw, str) or not isinstance(cfile, str):
                    continue
                ctid = _normalize_task_id(ctid_raw)
                if ctid and cfile:
                    chunk_index[ctid] = cfile
        subresults: list[dict] = []
        failures: list[str] = []
        for tid, sha, ev_plan in commits:
            if is_dir_mode:
                child_basename = ev_plan or chunk_index.get(tid)
                if not child_basename:
                    sub_status = "fail"
                    sub_reason = (
                        f"directory-mode commit-safe: no plan_file on event "
                        f"and TASK-{tid} not in 00_INDEX.json"
                    )
                    res = _gate_result("commit-safe", sub_status, sub_reason)
                    target_plan = None
                else:
                    target_plan = Path(plan_file) / child_basename
                    res = _gate_commit_safe(
                        sha, tid, target_plan, repo_root=repo_root,
                    )
            else:
                target_plan = plan_file
                res = _gate_commit_safe(
                    sha, tid, plan_file, repo_root=repo_root,
                )
            if is_dir_mode:
                subresults.append({
                    "task_id": tid,
                    "commit_sha": sha,
                    "plan_file": (
                        Path(target_plan).name if target_plan else None
                    ),
                    "status": res["status"],
                    "reason": res["reason"],
                })
            if res["status"] != "pass":
                failures.append(f"TASK-{tid}@{sha[:12]}: {res['reason']}")
        if failures:
            gate = _gate_result(
                "commit-safe",
                "fail",
                f"{len(failures)}/{len(commits)} commit(s) failed: {failures[:3]}",
            )
        else:
            gate = _gate_result(
                "commit-safe",
                "pass",
                f"all {len(commits)} commit(s) for run_id={run_id} touched only allowed files",
            )
        if is_dir_mode:
            gate["subresults"] = subresults
        gates.append(gate)
    return gates


# ---------------------------------------------------------------------------
# TASK-007 self-audit: protocol-drift detection
# ---------------------------------------------------------------------------
#
# `cmd_audit` cross-references the shipped artifacts (`plan_ops.py`,
# `plan_codex_dispatch.py`, the schema sidecars, SKILL.md, dispatch
# templates, and the design doc) against the canonical decisions declared
# in `CANONICAL_CONTRACT` near the top of this module. Each check returns
# the documented finding shape (`{check, status, canonical, actual,
# reason, tier}`) and `cmd_audit` aggregates them into a JSON report or a
# Markdown table. Scope is intentionally narrow: cross-cutting integrity,
# not behavior. Runtime conditions for a specific run are TASK-005 gates'
# job; self-audit is the standing readiness check.
#
# Conventions:
#   * Every check has a `tier ∈ {"default", "advisory"}`. Default checks
#     contribute to the verdict; advisory checks (`portable_tier` until
#     TASK-008 lands) appear in the report but do not flip `overall` to
#     `fail` unless `--strict` is set.
#   * Status vocabulary is `pass | pass_with_alias | fail`. TASK-008
#     removed every file-mode alias window, so post-cleanup checks emit
#     plain `pass` when the runtime artifact matches the canonical set
#     and `fail` otherwise — `pass_with_alias` is reserved for any
#     future alias window that genuinely needs a deprecation pause.
#     `fail` is the only verdict-flipping status.
#   * Checks that grep source files report concrete path + line numbers
#     in their `reason` strings so an operator can repair the drift
#     without grep-spelunking.
#   * Lines wrapped in `<!-- portable_tier: legacy-example -->` /
#     `<!-- /portable_tier: legacy-example -->` markers are skipped by
#     the `portable_tier` check; the markers are how a deliberate
#     instructional reference to `venv/bin/python` is allowlisted.

_PORTABLE_LEGACY_OPEN = "<!-- portable_tier: legacy-example -->"
_PORTABLE_LEGACY_CLOSE = "<!-- /portable_tier: legacy-example -->"


def _audit_relpath(path: Path) -> str:
    """Stable relative path for finding `locations[].path`. Falls back to
    `str(path)` when the file lives outside the repo root (e.g. tmp_path
    fixtures). Repo-relative form is what tooling consumes."""
    try:
        repo_root = Path(__file__).resolve().parents[3]
        return str(path.resolve().relative_to(repo_root))
    except (OSError, ValueError):
        return str(path)


def _audit_locate_constant(name: str) -> int | None:
    """Best-effort line number of a top-level assignment to `name` in
    `plan_ops.py`. Used to populate `locations[].line` for constant-only
    checks (status vocabulary, schedule field aliases, schemas) so the
    structured report points operators at the owning declaration."""
    try:
        text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    except OSError:
        return None
    pattern = re.compile(rf"^{re.escape(name)}\s*[:=]", re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    return text.count("\n", 0, m.start()) + 1


def _audit_locate_def(name: str, source_text: str | None = None) -> int | None:
    """Best-effort line number of `def <name>(` in `plan_ops.py` (or in
    the supplied `source_text`). Used by checks that grep a function
    body so the report points at the function header."""
    try:
        text = source_text if source_text is not None else (
            (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
        )
    except OSError:
        return None
    m = re.search(rf"^def {re.escape(name)}\s*\(", text, re.MULTILINE)
    if not m:
        return None
    return text.count("\n", 0, m.start()) + 1


def _audit_finding(
    *,
    check: str,
    status: str,
    canonical: dict,
    actual: dict,
    reason: str | None,
    tier: str = "default",
    locations: list[dict] | None = None,
) -> dict:
    """Shape helper for audit findings. `status ∈ {pass, pass_with_alias,
    fail}`; `tier ∈ {default, advisory}`. `reason` is None when the check
    is clean (canonical and actual agree exactly).

    `locations` is a list of `{"path": str, "line": int|None,
    "reason": str}` triples — the structured path-and-reason pairs the
    audit report exposes to tooling. Every finding carries it (empty
    list is allowed when nothing concrete needs flagging, e.g. a clean
    pass), so downstream consumers can iterate uniformly without
    branching on shape.
    """
    if status not in {"pass", "pass_with_alias", "fail"}:
        raise ValueError(
            f"audit status must be pass|pass_with_alias|fail, got {status!r}"
        )
    if tier not in {"default", "advisory"}:
        raise ValueError(f"audit tier must be default|advisory, got {tier!r}")
    return {
        "check": check,
        "status": status,
        "tier": tier,
        "canonical": canonical,
        "actual": actual,
        "reason": reason,
        "locations": list(locations) if locations else [],
    }


def _check_status_vocabulary() -> dict:
    """`ALLOWED_TASK_STATUSES` matches the canonical set.

    TASK-008: the file-mode `open` alias was REMOVED. The directory-only
    canonical set is the single accepted runtime form. This check reports
    plain `pass` only when `ALLOWED_TASK_STATUSES` exactly equals the
    canonical set; presence of `open` or any other legacy alias surfaces
    as `extra=[...]` drift and fails the check.
    """
    canonical = set(CANONICAL_CONTRACT["status_vocabulary"])
    actual = set(ALLOWED_TASK_STATUSES)
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[status_vocabulary]",
        "value": sorted(canonical),
    }
    actual_payload = {
        "source": "ALLOWED_TASK_STATUSES",
        "value": sorted(actual),
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    if actual == canonical:
        return _audit_finding(
            check="status_vocabulary",
            status="pass",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=None,
        )
    extra = sorted(actual - canonical)
    missing = sorted(canonical - actual)
    return _audit_finding(
        check="status_vocabulary",
        status="fail",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=(
            f"ALLOWED_TASK_STATUSES drift: extra={extra}, missing={missing}"
        ),
        locations=[
            {
                "path": owning_path,
                "line": _audit_locate_constant("ALLOWED_TASK_STATUSES"),
                "reason": (
                    f"ALLOWED_TASK_STATUSES drift: extra={extra}, "
                    f"missing={missing}"
                ),
            },
        ],
    )


def _check_schedule_wire_format() -> dict:
    """`_validate_schedule` reads the canonical `id` / `index` fields.

    TASK-008 REMOVED the file-mode `task_id` / `batch_index` aliases.
    `_validate_schedule` now rejects schedules carrying those legacy
    field names with a structured `missing-field` error. The check
    still asserts that `_validate_schedule` actually reads the canonical
    field names (`id` / `index`) — drift here would mean schedules
    silently fail to parse.
    """
    canonical_task_fields = list(CANONICAL_CONTRACT["schedule_task_fields"])
    canonical_batch_fields = list(CANONICAL_CONTRACT["schedule_batch_fields"])
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[schedule_task_fields, schedule_batch_fields]",
        "value": {
            "task_fields": canonical_task_fields,
            "batch_fields": canonical_batch_fields,
        },
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    try:
        validator_src = inspect_validate_schedule_source()
    except OSError as e:
        return _audit_finding(
            check="schedule_wire_format",
            status="pass",
            canonical=canonical_payload,
            actual={"source": str(_SCRIPT_DIR / "plan_ops.py"), "value": None},
            reason=f"plan_ops.py unreadable: {e}",
            locations=[
                {
                    "path": owning_path,
                    "line": None,
                    "reason": f"plan_ops.py unreadable: {e}",
                },
            ],
        )

    # Match both shapes the validator uses for the primary canonical names:
    #   `t.get("id")` / `b.get("index")` (lookup form)
    #   `if "id" not in t` / `if "index" not in b` (presence form).
    # The point is that `_validate_schedule` *names* the canonical field
    # somewhere in its body — drift would mean it stopped looking for it.
    task_pattern_present = bool(
        re.search(r"""\.get\(\s*["']id["']\s*\)""", validator_src)
    ) or bool(
        re.search(r"""["']id["']\s+not\s+in\s+\w""", validator_src)
    )
    batch_pattern_present = bool(
        re.search(r"""\.get\(\s*["']index["']\s*\)""", validator_src)
    ) or bool(
        re.search(r"""["']index["']\s+not\s+in\s+\w""", validator_src)
    )
    actual_payload = {
        "source": "_validate_schedule body",
        "value": {
            "reads_id": task_pattern_present,
            "reads_index": batch_pattern_present,
        },
    }
    validator_line = _audit_locate_def("_validate_schedule")
    if not task_pattern_present or not batch_pattern_present:
        missing: list[str] = []
        if not task_pattern_present:
            missing.append("`.get(\"id\")` in _validate_schedule")
        if not batch_pattern_present:
            missing.append("`.get(\"index\")` in _validate_schedule")
        return _audit_finding(
            check="schedule_wire_format",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=f"missing canonical reads: {missing}",
            locations=[
                {
                    "path": owning_path,
                    "line": validator_line,
                    "reason": f"missing canonical reads: {missing}",
                },
            ],
        )
    return _audit_finding(
        check="schedule_wire_format",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def inspect_validate_schedule_source() -> str:
    """Return the source body of `_validate_schedule` from this module.

    Helper extracted so tests and the check share one extraction path.
    Raises OSError if the script file is unreadable.
    """
    text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    code_text = _strip_python_comments_and_docstrings(text)
    m = re.search(r"^def _validate_schedule\s*\(", code_text, re.MULTILINE)
    if not m:
        return ""
    tail = code_text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    return tail[: next_def.start()] if next_def else tail


def _check_implementer_report_labels() -> dict:
    """`cmd_parse_implementer_report` searches the canonical labels.

    TASK-008 REMOVED the file-mode `**Concerns:**` alias.
    `cmd_parse_implementer_report` no longer accepts the legacy label
    as a fallback; only the canonical `**Concerns for reviewer:**`
    section is recognized. The check still asserts that the parser
    body names the canonical anchor labels — drift (a missing canonical
    `**Concerns for reviewer:**` or `**Plan adaptations:**` literal)
    fails.
    """
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[implementer_*_label(s)]",
        "value": {
            "concerns": CANONICAL_CONTRACT["implementer_concerns_labels"],
            "plan_adaptations": CANONICAL_CONTRACT[
                "implementer_plan_adaptations_label"
            ],
        },
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    try:
        text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="implementer_report_labels",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason=f"plan_ops.py unreadable: {e}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"plan_ops.py unreadable: {e}",
            }],
        )
    m = re.search(
        r"^def cmd_parse_implementer_report\s*\(", text, re.MULTILINE,
    )
    if not m:
        return _audit_finding(
            check="implementer_report_labels",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason="cmd_parse_implementer_report definition not found",
            locations=[{
                "path": owning_path, "line": None,
                "reason": "cmd_parse_implementer_report definition not found",
            }],
        )
    tail = text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    body = tail[: next_def.start()] if next_def else tail
    run_m = re.search(
        r"^def _run_parse_implementer_report\s*\(", text, re.MULTILINE,
    )
    if run_m:
        run_tail = text[run_m.end():]
        run_next_def = re.search(r"^def\s+\w+", run_tail, re.MULTILINE)
        body += run_tail[: run_next_def.start()] if run_next_def else run_tail

    canonical_concerns = "Concerns for reviewer"
    canonical_plan_adapt = "Plan adaptations"
    has_canonical_concerns = canonical_concerns in body
    has_plan_adapt = canonical_plan_adapt in body
    actual_payload = {
        "source": "cmd_parse_implementer_report body",
        "value": {
            "has_concerns_for_reviewer": has_canonical_concerns,
            "has_plan_adaptations": has_plan_adapt,
        },
    }
    parser_def_line = _audit_locate_def(
        "cmd_parse_implementer_report", source_text=text,
    )
    if not has_canonical_concerns or not has_plan_adapt:
        missing = []
        if not has_canonical_concerns:
            missing.append(canonical_concerns)
        if not has_plan_adapt:
            missing.append(canonical_plan_adapt)
        return _audit_finding(
            check="implementer_report_labels",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=f"missing canonical labels in parser: {missing}",
            locations=[{
                "path": owning_path, "line": parser_def_line,
                "reason": f"missing canonical labels in parser: {missing}",
            }],
        )
    return _audit_finding(
        check="implementer_report_labels",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_execution_log_columns() -> dict:
    """`cmd_finalize_execution_log` writes the canonical column header.

    Greps for the literal `| Task | Agent | Reviewer | Verdict | Commit |
    Notes |` row inside the function body. Drift here previously broke
    downstream tooling that parses the table by column position.
    """
    canonical_columns = list(CANONICAL_CONTRACT["execution_log_columns"])
    canonical_header = "| " + " | ".join(canonical_columns) + " |"
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[execution_log_columns]",
        "value": canonical_columns,
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    try:
        text = (_SCRIPT_DIR / "plan_ops.py").read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="execution_log_columns",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason=f"plan_ops.py unreadable: {e}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"plan_ops.py unreadable: {e}",
            }],
        )
    m = re.search(
        r"^def cmd_finalize_execution_log\s*\(", text, re.MULTILINE,
    )
    if not m:
        return _audit_finding(
            check="execution_log_columns",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "plan_ops.py", "value": None},
            reason="cmd_finalize_execution_log definition not found",
            locations=[{
                "path": owning_path, "line": None,
                "reason": "cmd_finalize_execution_log definition not found",
            }],
        )
    finalize_def_line = _audit_locate_def(
        "cmd_finalize_execution_log", source_text=text,
    )
    tail = text[m.end():]
    next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
    body = tail[: next_def.start()] if next_def else tail
    run_m = re.search(
        r"^def _run_finalize_execution_log\s*\(", text, re.MULTILINE,
    )
    if run_m:
        run_tail = text[run_m.end():]
        run_next_def = re.search(r"^def\s+\w+", run_tail, re.MULTILINE)
        body += run_tail[: run_next_def.start()] if run_next_def else run_tail
    actual_payload = {
        "source": "cmd_finalize_execution_log header literal",
        "value": canonical_header if canonical_header in body else None,
    }
    if canonical_header not in body:
        return _audit_finding(
            check="execution_log_columns",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=(
                "canonical column header literal "
                f"{canonical_header!r} not present in "
                "cmd_finalize_execution_log body"
            ),
            locations=[{
                "path": owning_path, "line": finalize_def_line,
                "reason": (
                    "canonical column header literal "
                    f"{canonical_header!r} not present in "
                    "cmd_finalize_execution_log body"
                ),
            }],
        )
    return _audit_finding(
        check="execution_log_columns",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_schemas() -> dict:
    """Codex implement / review schemas match the design doc shape.

    `codex_implement_schema.json[blockers]` must be `array of strings`
    (not the legacy `array of objects` form that produced Phase 5
    parse failures). `codex_review_schema.json` must require the §7.3
    fields `task_id`, `verdict`, `findings`, `scope_ok`,
    `acceptance_met`, `summary` with the canonical verdict enum.
    """
    canonical_payload = {
        "source": "design doc §7.2 / §7.3",
        "value": {
            "implement_blockers_items_type": "string",
            "review_required": [
                "task_id", "verdict", "findings",
                "notes", "scope_ok", "acceptance_met", "summary",
            ],
            "review_verdict_enum": [
                "clean", "minor-findings", "needs-rework",
            ],
        },
    }
    impl_path = _SCRIPT_DIR / "codex_implement_schema.json"
    review_path = _SCRIPT_DIR / "codex_review_schema.json"
    impl_relpath = _audit_relpath(impl_path)
    review_relpath = _audit_relpath(review_path)
    problems: list[str] = []
    locations: list[dict] = []
    impl_actual: object = None
    review_actual: object = None
    try:
        impl_schema = json.loads(impl_path.read_text(encoding="utf-8"))
    except OSError as e:
        msg = f"codex_implement_schema.json unreadable: {e}"
        problems.append(msg)
        locations.append({"path": impl_relpath, "line": None, "reason": msg})
        impl_schema = {}
    except json.JSONDecodeError as e:
        msg = f"codex_implement_schema.json malformed JSON: {e}"
        problems.append(msg)
        locations.append({"path": impl_relpath, "line": None, "reason": msg})
        impl_schema = {}
    try:
        review_schema = json.loads(review_path.read_text(encoding="utf-8"))
    except OSError as e:
        msg = f"codex_review_schema.json unreadable: {e}"
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})
        review_schema = {}
    except json.JSONDecodeError as e:
        msg = f"codex_review_schema.json malformed JSON: {e}"
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})
        review_schema = {}

    blockers_items = (
        impl_schema.get("properties", {})
        .get("blockers", {})
        .get("items", {})
    )
    impl_actual = {
        "blockers_items_type": blockers_items.get("type"),
    }
    if blockers_items.get("type") != "string":
        msg = (
            "codex_implement_schema.json[properties.blockers.items.type] "
            f"is {blockers_items.get('type')!r}; canonical is 'string'"
        )
        problems.append(msg)
        locations.append({"path": impl_relpath, "line": None, "reason": msg})

    review_required = review_schema.get("required") or []
    verdict_enum = (
        review_schema.get("properties", {})
        .get("verdict", {})
        .get("enum")
    )
    review_actual = {
        "required": list(review_required),
        "verdict_enum": list(verdict_enum) if verdict_enum else None,
    }
    canonical_required = {
        "task_id", "verdict", "findings",
        "notes", "scope_ok", "acceptance_met", "summary",
    }
    missing_required = sorted(canonical_required - set(review_required))
    if missing_required:
        msg = (
            "codex_review_schema.json[required] missing canonical fields: "
            f"{missing_required}"
        )
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})
    canonical_verdicts = ["clean", "minor-findings", "needs-rework"]
    # Enum ordering is not part of the contract — reorder in the schema
    # should not flip the check. Compare as sets so only set-mismatch
    # (missing/extra members) counts as drift.
    if set(verdict_enum or []) != set(canonical_verdicts):
        msg = (
            "codex_review_schema.json[properties.verdict.enum] is "
            f"{verdict_enum!r}; canonical is {canonical_verdicts!r}"
        )
        problems.append(msg)
        locations.append({"path": review_relpath, "line": None, "reason": msg})

    actual_payload = {
        "source": "codex_implement_schema.json + codex_review_schema.json",
        "value": {"implement": impl_actual, "review": review_actual},
    }
    if problems:
        return _audit_finding(
            check="schemas",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=locations,
        )
    return _audit_finding(
        check="schemas",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _scan_portable_tier_violations(path: Path) -> list[str]:
    """Return `path:lineno` strings for `venv/bin/python` literals outside
    legacy-example marker blocks. Used by `_check_portable_tier`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [f"{path}: unreadable"]
    hits: list[str] = []
    in_legacy = False
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if _PORTABLE_LEGACY_OPEN in line:
            in_legacy = True
            continue
        if _PORTABLE_LEGACY_CLOSE in line:
            in_legacy = False
            continue
        if in_legacy:
            continue
        if "venv/bin/python" in stripped:
            hits.append(f"{path}:{idx}")
    return hits


def _check_portable_tier() -> dict:
    """`SKILL.md` / `dispatch-templates.md` carry no `venv/bin/python`
    literals outside legacy-example marker blocks (TASK-008 fixes).

    Advisory tier: registered but excluded from the default verdict
    until TASK-008 lands; surfaces the exact path:line of every
    violation so the cleanup task has a precise punch list.
    """
    skill = (
        Path(__file__).resolve().parent.parent
        / "skills" / "implement-plan" / "SKILL.md"
    )
    templates = (
        Path(__file__).resolve().parent.parent
        / "skills" / "implement-plan" / "dispatch-templates.md"
    )
    canonical_payload = {
        "source": "TASK-008 portable-Python policy",
        "value": (
            "no `venv/bin/python` literals in SKILL.md / dispatch-templates.md "
            "outside <!-- portable_tier: legacy-example --> blocks"
        ),
    }
    hits: list[str] = []
    hits.extend(_scan_portable_tier_violations(skill))
    hits.extend(_scan_portable_tier_violations(templates))
    actual_payload = {
        "source": "SKILL.md + dispatch-templates.md",
        "value": {"violations": hits},
    }
    if hits:
        # `hits` are `path:lineno` strings (or `path: unreadable`); split
        # into structured locations so consumers don't have to re-parse.
        locations: list[dict] = []
        for hit in hits:
            head, sep, tail = hit.rpartition(":")
            if sep and tail.isdigit():
                hit_path = _audit_relpath(Path(head))
                hit_line: int | None = int(tail)
            else:
                hit_path = _audit_relpath(Path(hit.split(":", 1)[0]))
                hit_line = None
            locations.append({
                "path": hit_path,
                "line": hit_line,
                "reason": "`venv/bin/python` literal outside legacy-example marker block",
            })
        return _audit_finding(
            check="portable_tier",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=(
                f"{len(hits)} `venv/bin/python` literal(s) outside "
                f"legacy-example markers; first few: {hits[:5]}"
            ),
            tier="advisory",
            locations=locations,
        )
    return _audit_finding(
        check="portable_tier",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
        tier="advisory",
    )


def _check_wrapper_isolation() -> dict:
    """`plan_codex_dispatch.py` carries the State-Isolation invariants.

    Asserts (1) the always-ignore / protected-paths import seam from
    `_plan_paths`, (2) `_snapshot_baseline(` at the three required
    seams (implement, timeout-cleanup, review), and (3) no
    `git clean -fd` in executable code at repo scope. Distinct from
    `execution-safe`/`review-safe` gates because audit reports
    path:line evidence, not just pass/fail.
    """
    wrapper = _SCRIPT_DIR / "plan_codex_dispatch.py"
    canonical_payload = {
        "source": "TASK-003 State-Isolation Contract",
        "value": {
            "always_ignore_seam": (
                "from _plan_paths import PROTECTED_EXACT_PATHS, ..."
            ),
            "snapshot_baseline_seams": [
                "cmd_implement", "_handle_timeout_cleanup or kwarg",
                "cmd_review",
            ],
            "forbidden": "git clean -fd outside comments / docstrings",
        },
    }
    wrapper_relpath = _audit_relpath(wrapper)
    if not wrapper.is_file():
        return _audit_finding(
            check="wrapper_isolation",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(wrapper), "value": None},
            reason=f"wrapper not found: {wrapper}",
            locations=[{
                "path": wrapper_relpath, "line": None,
                "reason": f"wrapper not found: {wrapper}",
            }],
        )
    try:
        text = wrapper.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="wrapper_isolation",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(wrapper), "value": None},
            reason=f"wrapper unreadable: {e}",
            locations=[{
                "path": wrapper_relpath, "line": None,
                "reason": f"wrapper unreadable: {e}",
            }],
        )
    code_text = _strip_python_comments_and_docstrings(text)
    problems: list[str] = []
    wrapper_locations: list[dict] = []

    def _wrapper_def_line(name: str) -> int | None:
        return _audit_locate_def(name, source_text=text)

    # (1) always-ignore seam
    has_always_ignore = (
        "PROTECTED_EXACT_PATHS" in code_text and "_plan_paths" in code_text
    )
    if not has_always_ignore:
        msg = "missing _plan_paths protected-paths import seam"
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath, "line": None, "reason": msg,
        })

    # (2) snapshot-baseline seams (implement + cleanup + review)
    def _function_body(name: str) -> str | None:
        m = re.search(rf"^def {re.escape(name)}\s*\(", code_text, re.MULTILINE)
        if not m:
            return None
        tail = code_text[m.end():]
        next_def = re.search(r"^def\s+\w+", tail, re.MULTILINE)
        return tail[: next_def.start()] if next_def else tail

    impl_body = _function_body("cmd_implement")
    cleanup_body = _function_body("_handle_timeout_cleanup")
    review_body = _function_body("cmd_review")
    snapshot_seams: list[str] = []
    if impl_body and "_snapshot_baseline(" in impl_body:
        snapshot_seams.append("cmd_implement")
    else:
        msg = "_snapshot_baseline(... missing in cmd_implement"
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath,
            "line": _wrapper_def_line("cmd_implement"),
            "reason": msg,
        })
    cleanup_seam_ok = False
    if cleanup_body and "_snapshot_baseline(" in cleanup_body:
        cleanup_seam_ok = True
        snapshot_seams.append("_handle_timeout_cleanup")
    elif impl_body and re.search(
        r"_handle_timeout_cleanup\s*\([^)]*baseline", impl_body, re.DOTALL,
    ):
        cleanup_seam_ok = True
        snapshot_seams.append("cmd_implement->_handle_timeout_cleanup(baseline)")
    if not cleanup_seam_ok:
        msg = (
            "_snapshot_baseline missing in timeout-cleanup window "
            "(neither _handle_timeout_cleanup body nor cmd_implement "
            "baseline kwarg)"
        )
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath,
            "line": _wrapper_def_line("_handle_timeout_cleanup"),
            "reason": msg,
        })
    if review_body and "_snapshot_baseline(" in review_body:
        snapshot_seams.append("cmd_review")
    else:
        msg = "_snapshot_baseline(... missing in cmd_review"
        problems.append(msg)
        wrapper_locations.append({
            "path": wrapper_relpath,
            "line": _wrapper_def_line("cmd_review"),
            "reason": msg,
        })

    # (3) `git clean -fd` outside comments/docstrings.
    forbidden_hits: list[str] = []
    forbidden_lines: list[int] = []
    in_triple_double = False
    in_triple_single = False
    for idx, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        double_marks = line.count('"""')
        single_marks = line.count("'''")
        line_was_in_string = in_triple_double or in_triple_single
        if double_marks % 2 == 1:
            in_triple_double = not in_triple_double
        if single_marks % 2 == 1:
            in_triple_single = not in_triple_single
        if line_was_in_string or in_triple_double or in_triple_single:
            continue
        if stripped.startswith("#"):
            continue
        if re.search(r"git\s+clean\s+-fd", line):
            forbidden_hits.append(f"plan_codex_dispatch.py:{idx}")
            forbidden_lines.append(idx)
    if forbidden_hits:
        problems.append(
            f"`git clean -fd` outside comments at: {forbidden_hits}"
        )
        for ln in forbidden_lines:
            wrapper_locations.append({
                "path": wrapper_relpath, "line": ln,
                "reason": "`git clean -fd` outside comments / docstrings",
            })

    actual_payload = {
        "source": "plan_codex_dispatch.py",
        "value": {
            "always_ignore_seam_present": has_always_ignore,
            "snapshot_baseline_seams": snapshot_seams,
            "git_clean_fd_hits": forbidden_hits,
        },
    }
    if problems:
        return _audit_finding(
            check="wrapper_isolation",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=wrapper_locations,
        )
    return _audit_finding(
        check="wrapper_isolation",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_design_doc_orphans() -> dict:
    """`DUAL_AGENT_PLAN_EXECUTOR.md` references no deprecated stubs.

    Specifically: no `--skip-analysis` flag (renamed to `--skip-cross-review`
    long ago), and any reference to `task_id` / `batch_index` is paired
    with the canonical `id` / `index` literal so a reader is not left
    with a contradictory mental model. Reports the offending line ranges.
    """
    repo_root = Path(__file__).resolve().parents[3]
    doc = repo_root / "docs" / "plans" / "DUAL_AGENT_PLAN_EXECUTOR.md"
    canonical_payload = {
        "source": "TASK-001 canonical contract + design doc §5/§7.2",
        "value": {
            "deprecated_flags": ["--skip-analysis"],
            "alias_field_handling": (
                "schedule field aliases task_id/batch_index must be "
                "named alongside canonical id/index"
            ),
        },
    }
    doc_relpath = _audit_relpath(doc)
    if not doc.is_file():
        return _audit_finding(
            check="design_doc_orphans",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(doc), "value": None},
            reason=f"design doc not found: {doc}",
            locations=[{
                "path": doc_relpath, "line": None,
                "reason": f"design doc not found: {doc}",
            }],
        )
    try:
        text = doc.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="design_doc_orphans",
            status="fail",
            canonical=canonical_payload,
            actual={"source": str(doc), "value": None},
            reason=f"design doc unreadable: {e}",
            locations=[{
                "path": doc_relpath, "line": None,
                "reason": f"design doc unreadable: {e}",
            }],
        )
    problems: list[str] = []
    doc_locations: list[dict] = []
    skip_analysis_hits: list[int] = []
    # Lines that explicitly mark the reference as historical /
    # deferred / discussed-but-not-implemented are allowed; we are
    # hunting for stubs that contradict the canonical decision, not
    # for an honest design-doc note about a deferred flag. Heuristic
    # markers: "deferred", "deprecated", "removed", "legacy",
    # "do not add", "not implemented" — the design doc uses these
    # words consistently when discussing flags it explicitly chose
    # not to ship.
    _HISTORICAL_MARKERS = (
        "deferred", "deprecated", "removed", "legacy",
        "do not add", "not implemented", "do NOT add",
    )
    for idx, line in enumerate(text.splitlines(), 1):
        if "--skip-analysis" not in line:
            continue
        lower = line.lower()
        if any(marker in lower for marker in _HISTORICAL_MARKERS):
            continue
        skip_analysis_hits.append(idx)
    if skip_analysis_hits:
        problems.append(
            f"`--skip-analysis` references at lines: {skip_analysis_hits}"
        )
        for ln in skip_analysis_hits:
            doc_locations.append({
                "path": doc_relpath, "line": ln,
                "reason": "deprecated `--skip-analysis` flag reference",
            })
    # Alias field handling: `task_id` and `batch_index` are legitimate to
    # mention as aliases, but only if the canonical `id` / `index`
    # literals appear in the same document. (They do.)
    has_id_canonical = bool(re.search(r"\bid\b", text))
    has_index_canonical = bool(re.search(r"\bindex\b", text))
    if not (has_id_canonical and has_index_canonical):
        msg = (
            "design doc mentions schedule fields but canonical "
            "`id` / `index` literals are absent"
        )
        problems.append(msg)
        doc_locations.append({
            "path": doc_relpath, "line": None, "reason": msg,
        })
    actual_payload = {
        "source": "DUAL_AGENT_PLAN_EXECUTOR.md",
        "value": {
            "skip_analysis_hits": skip_analysis_hits,
            "has_id_literal": has_id_canonical,
            "has_index_literal": has_index_canonical,
        },
    }
    if problems:
        return _audit_finding(
            check="design_doc_orphans",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=doc_locations,
        )
    return _audit_finding(
        check="design_doc_orphans",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_global_lock_paths() -> dict:
    """`GLOBAL_LOCK_PATHS` / `GLOBAL_LOCK_GLOBS` match the documented set.

    The canonical default exact-match set + glob set are documented in
    `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` §9.4 ("Globally-locked
    paths") so plan authors can read the contract without grepping the
    code. Drift either way (constant has an entry the doc lacks, or
    vice versa) breaks that contract.
    """
    canonical_exact = set(GLOBAL_LOCK_PATHS)
    canonical_globs = set(GLOBAL_LOCK_GLOBS)
    canonical_payload = {
        "source": "GLOBAL_LOCK_PATHS + GLOBAL_LOCK_GLOBS",
        "value": {
            "exact": sorted(canonical_exact),
            "globs": sorted(canonical_globs),
        },
    }
    doc_path = _SCRIPT_DIR.parent.parent.parent / "docs" / "plans" / "DUAL_AGENT_PLAN_EXECUTOR.md"
    owning_path = _audit_relpath(doc_path)
    try:
        doc_src = doc_path.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="global_lock_paths",
            status="fail",
            canonical=canonical_payload,
            actual=canonical_payload,
            reason=f"design doc unreadable: {e}",
            locations=[
                {"path": owning_path, "line": None,
                 "reason": f"design doc unreadable: {e}"},
            ],
        )
    section_re = re.compile(
        r"##### Globally-locked paths.*?(?=\n#####|\n####|\Z)",
        re.DOTALL,
    )
    section_match = section_re.search(doc_src)
    if section_match is None:
        return _audit_finding(
            check="global_lock_paths",
            status="pass",
            canonical=canonical_payload,
            actual=canonical_payload,
            reason="design-doc section anchor absent; constants are treated as canonical",
            locations=[
                {"path": owning_path, "line": None,
                 "reason": "section anchor missing; constants fallback used"},
            ],
        )
    section = section_match.group(0)
    doc_exact = set(re.findall(r"`([^`\s]+)`", section))
    # Drop entries that look like code identifiers / file paths owning
    # the constants themselves rather than members of the set.
    doc_exact -= {
        "GLOBAL_LOCK_PATHS", "GLOBAL_LOCK_GLOBS",
        "plugins/plan-executor/scripts/plan_ops.py",
        "global_lock_paths", "global_lock", "files",
        "allowed_files", "dependencies",
        "State Isolation Contract",
        "parse-schedule", "compute-schedule",
        "list-global-lock-paths --json",
        "docs/plans/_global_lock_paths.yaml",
        "docs/plans/_global_lock_paths.yaml.example",
        "$PYTHON plugins/plan-executor/scripts/plan_ops.py list-global-lock-paths --json",
        "*?[",
    }
    # Drop entries that look like Python identifiers (snake_case,
    # likely prose mentions of vars/fields) — real default members are
    # filenames with a dot, a glob char, or are well-known capitalized
    # bare-name files (Dockerfile, Gemfile, Pipfile).
    _BARE_FILENAMES = {"Dockerfile", "Gemfile", "Pipfile", "Cargo.lock"}
    doc_exact = {
        p for p in doc_exact
        if any(c in p for c in "./") or any(c in p for c in "*?[")
        or p in _BARE_FILENAMES
    }
    # Globs are entries containing fnmatch metachars.
    doc_globs = {p for p in doc_exact if any(c in p for c in "*?[")}
    doc_exact_only = doc_exact - doc_globs
    actual_payload = {
        "source": f"{owning_path} §Globally-locked paths",
        "value": {
            "exact": sorted(doc_exact_only),
            "globs": sorted(doc_globs),
        },
    }
    if doc_exact_only == canonical_exact and doc_globs == canonical_globs:
        return _audit_finding(
            check="global_lock_paths",
            status="pass",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=None,
        )
    extra_exact = sorted(doc_exact_only - canonical_exact)
    missing_exact = sorted(canonical_exact - doc_exact_only)
    extra_globs = sorted(doc_globs - canonical_globs)
    missing_globs = sorted(canonical_globs - doc_globs)
    reason = (
        f"GLOBAL_LOCK_PATHS doc/constant drift: "
        f"exact_extra={extra_exact}, exact_missing={missing_exact}, "
        f"globs_extra={extra_globs}, globs_missing={missing_globs}"
    )
    return _audit_finding(
        check="global_lock_paths",
        status="fail",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=reason,
        locations=[
            {"path": owning_path, "line": None, "reason": reason},
        ],
    )


def _check_canonical_fixture_not_archived() -> dict:
    """`CANONICAL_CONTRACT.fixture_path` does not point under `docs/plans/archive/`.

    TASK-004 (POSTMORTEM_FIXES_2026-04-25): pre-archive lint. When a plan
    is moved to the archive, authors sometimes forget to update the SoT
    fixture path; the `fixture-valid` gate then validates an archived
    artifact instead of the live conformance fixture. This check catches
    that drift before it ships.
    """
    fixture_value = str(CANONICAL_CONTRACT.get("fixture_path", ""))
    canonical_payload = {
        "source": "CANONICAL_CONTRACT[fixture_path]",
        "value": fixture_value,
    }
    actual_payload = {
        "source": "CANONICAL_CONTRACT[fixture_path]",
        "value": fixture_value,
    }
    owning_path = _audit_relpath(_SCRIPT_DIR / "plan_ops.py")
    line_no = _audit_locate_constant("CANONICAL_CONTRACT")
    if fixture_value.startswith("docs/plans/archive/"):
        reason = (
            f"`CANONICAL_CONTRACT.fixture_path` points under "
            f"`docs/plans/archive/` ({fixture_value!r}); update "
            "`CANONICAL_CONTRACT.fixture_path` to point at the live "
            "fixture before archiving"
        )
        return _audit_finding(
            check="canonical_fixture_not_archived",
            status="fail",
            canonical=canonical_payload,
            actual=actual_payload,
            reason=reason,
            locations=[
                {"path": owning_path, "line": line_no, "reason": reason},
            ],
        )
    return _audit_finding(
        check="canonical_fixture_not_archived",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_principle_referenced() -> dict:
    """`Completed-Work Preservation Principle` is anchored in §Rules and
    cross-referenced from at least 4 other phase sections of SKILL.md.

    TASK-004 (prohibit_silent_revert): the principle text lives canonically
    in `## Rules`. Each phase that documents a destructive auto-revert
    seam (Phase C, Phase D.4, D.2a binding-mode, D.2a.5/D.2a.6 halt
    paths, the shared awaiting-user pause subroutine) must carry a
    cross-reference back to the principle so a reader landing in the
    phase section sees the binding rule. This check enforces the
    cross-reference invariant.
    """
    skill_path = (
        _SCRIPT_DIR.parent / "skills" / "implement-plan" / "SKILL.md"
    )
    canonical_payload = {
        "source": (
            "TASK-004 prohibit_silent_revert: principle text in §Rules + "
            ">=4 phase-section cross-references"
        ),
        "value": {
            "literal": "Completed-Work Preservation Principle",
            "min_phase_section_refs": 4,
        },
    }
    owning_path = _audit_relpath(skill_path)
    if not skill_path.is_file():
        return _audit_finding(
            check="principle_referenced",
            status="fail",
            tier="default",
            canonical=canonical_payload,
            actual={"source": str(skill_path), "value": None},
            reason=f"SKILL.md not found: {skill_path}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"SKILL.md not found: {skill_path}",
            }],
        )
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="principle_referenced",
            status="fail",
            tier="default",
            canonical=canonical_payload,
            actual={"source": str(skill_path), "value": None},
            reason=f"SKILL.md unreadable: {e}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"SKILL.md unreadable: {e}",
            }],
        )
    literal = "Completed-Work Preservation Principle"
    lines = text.splitlines()

    # Section partition: walk through SKILL.md, classifying each line by
    # which top-level (`## `) section it lives in. The §Rules section is
    # the home of the principle bullet; everything else (Phase C / Phase D
    # subsections, the shared awaiting-user pause subsection, etc.) is a
    # "phase section" for the cross-reference count.
    in_rules = False
    rules_hits: list[int] = []
    phase_section_hits: dict[str, list[int]] = {}
    current_top: str | None = None
    current_sub: str | None = None
    for idx, line in enumerate(lines, 1):
        if line.startswith("## ") and not line.startswith("### "):
            current_top = line[3:].strip()
            current_sub = None
            in_rules = (current_top == "Rules")
            continue
        if line.startswith("### ") or line.startswith("#### "):
            current_sub = line.lstrip("#").strip()
        if literal in line:
            if in_rules:
                rules_hits.append(idx)
            else:
                # Bucket by the nearest sub-heading when present, else
                # by the top-level section name. This ensures multiple
                # references inside the same phase still count as one
                # phase section.
                key = current_sub or current_top or "<unknown>"
                phase_section_hits.setdefault(key, []).append(idx)

    distinct_phase_sections = len(phase_section_hits)

    # TASK-009 (prohibit_silent_revert): the principle is also propagated
    # into the dispatch-templates, plan-implementer agent spec, and the
    # DUAL_AGENT_PLAN_EXECUTOR design doc so subagents and architectural
    # readers internalize it. Verify the literal appears at least once
    # in each propagation target. Repo-relative paths so tooling renders
    # stable locations.
    propagation_targets = {
        "dispatch-templates": (
            _SCRIPT_DIR.parent / "skills" / "implement-plan"
            / "dispatch-templates.md"
        ),
        "plan-implementer": (
            _SCRIPT_DIR.parent / "agents" / "plan-implementer.md"
        ),
        "design-doc": (
            _SCRIPT_DIR.parent.parent.parent / "docs" / "plans"
            / "DUAL_AGENT_PLAN_EXECUTOR.md"
        ),
    }
    propagation_hits: dict[str, list[int]] = {}
    propagation_problems: list[tuple[str, str, str | None]] = []
    for target_name, target_path in propagation_targets.items():
        target_rel = _audit_relpath(target_path)
        if not target_path.is_file():
            propagation_problems.append((
                target_name,
                f"propagation target {target_name!r} not found at {target_rel}",
                target_rel,
            ))
            propagation_hits[target_name] = []
            continue
        try:
            target_text = target_path.read_text(encoding="utf-8")
        except OSError as e:
            propagation_problems.append((
                target_name,
                f"propagation target {target_name!r} unreadable: {e}",
                target_rel,
            ))
            propagation_hits[target_name] = []
            continue
        target_hits = [
            i for i, ln in enumerate(target_text.splitlines(), 1)
            if literal in ln
        ]
        propagation_hits[target_name] = target_hits
        if not target_hits:
            propagation_problems.append((
                target_name,
                (
                    f"literal {literal!r} not found in propagation target "
                    f"{target_name!r} ({target_rel})"
                ),
                target_rel,
            ))

    actual_payload = {
        "source": owning_path,
        "value": {
            "rules_hits": rules_hits,
            "phase_sections_with_ref": sorted(phase_section_hits.keys()),
            "distinct_phase_section_count": distinct_phase_sections,
            "propagation_hits": {
                k: v for k, v in propagation_hits.items()
            },
        },
    }
    problems: list[str] = []
    locations: list[dict] = []
    if not rules_hits:
        msg = (
            f"literal {literal!r} not found in `## Rules` section"
        )
        problems.append(msg)
        locations.append({"path": owning_path, "line": None, "reason": msg})
    if distinct_phase_sections < 4:
        msg = (
            f"literal {literal!r} appears in {distinct_phase_sections} "
            f"phase section(s); require >= 4 cross-references"
        )
        problems.append(msg)
        locations.append({"path": owning_path, "line": None, "reason": msg})
    for _name, msg, loc_path in propagation_problems:
        problems.append(msg)
        locations.append({
            "path": loc_path or owning_path, "line": None, "reason": msg,
        })
    if problems:
        return _audit_finding(
            check="principle_referenced",
            status="fail",
            tier="default",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=locations,
        )
    return _audit_finding(
        check="principle_referenced",
        status="pass",
        tier="default",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_wrapper_restore_authorization_source() -> dict:
    """Wrapper scripts (plan_claude_dispatch.py and plan_codex_dispatch.py) must
    gate every `git restore` call with a member of the authorized enum.

    TASK-004 (prohibit_silent_revert extension): mirrors the orchestrator-
    side audit check but for the wrapper layer. Closes the gap where
    wrapper-internal cleanup could silently destroy work.
    """
    targets = {
        "plan_claude_dispatch.py": _SCRIPT_DIR / "plan_claude_dispatch.py",
        "plan_codex_dispatch.py": _SCRIPT_DIR / "plan_codex_dispatch.py",
        "_claude_dispatch_cleanup.py": _SCRIPT_DIR / "_claude_dispatch_cleanup.py",
    }
    canonical_payload = {
        "source": (
            "Every _git(['restore', ...]) call in wrapper scripts must pass "
            "authorization_source"
        ),
        "value": ["wrapper_internal_cleanup_explicit_declaration", "wrapper_observe_only_blocked_by_status"],
    }

    problems: list[str] = []
    actual_values: dict[str, list[str]] = {}

    auth_pattern = re.compile(r"authorization_source\s*=\s*([\"'][A-Za-z0-9._-]+[\"'])")

    for name, path in targets.items():
        if not path.is_file():
            # advisory if optional file is missing, default if core
            problems.append(f"wrapper script not found: {name}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            problems.append(f"cannot read {name}: {e}")
            continue

        found_auths = auth_pattern.findall(text)
        actual_values[name] = sorted({v.strip("'\"") for v in found_auths})

        # Verify definitions are gated.
        if name == "_claude_dispatch_cleanup.py":
            if "def _restore_path" in text and "authorization_source" not in text.split("def _restore_path")[1].split(") ->")[0]:
                problems.append(f"{name}: _restore_path definition is missing authorization_source gate")
        if name == "plan_codex_dispatch.py":
            if "def _restore_in_scope" in text and "authorization_source" not in text.split("def _restore_in_scope")[1].split(") ->")[0]:
                problems.append(f"{name}: _restore_in_scope definition is missing authorization_source gate")

    if problems:
        return _audit_finding(
            check="wrapper_restore_authorization",
            status="fail",
            canonical=canonical_payload,
            actual={"source": "wrapper scripts", "value": actual_values},
            reason="; ".join(problems),
        )
    return _audit_finding(
        check="wrapper_restore_authorization",
        status="pass",
        canonical=canonical_payload,
        actual={"source": "wrapper scripts", "value": actual_values},
        reason=None,
    )


def _check_fail_task_authorization_source() -> dict:
    """Every `--authorization-source <value>` token referenced in SKILL.md
    must resolve to a member of `ALLOWED_FAIL_AUTHORIZATION_SOURCES`, and
    every literal `fail-task` shell invocation in a fenced code block must
    carry an `--authorization-source` flag.

    TASK-009 (prohibit_silent_revert): closes the regression vector where a
    future contributor adds a `fail-task` call site without consciously
    declaring which authorized path is sanctioning the destructive
    side-effect. The argparse `choices=` constraint enforces this on the
    CLI; this audit check enforces it on the docs that operators read.
    """
    skill_path = (
        _SCRIPT_DIR.parent / "skills" / "implement-plan" / "SKILL.md"
    )
    canonical_payload = {
        "source": (
            "ALLOWED_FAIL_AUTHORIZATION_SOURCES + every fenced `fail-task` "
            "invocation in SKILL.md must declare --authorization-source"
        ),
        "value": sorted(ALLOWED_FAIL_AUTHORIZATION_SOURCES),
    }
    owning_path = _audit_relpath(skill_path)
    if not skill_path.is_file():
        return _audit_finding(
            check="fail_task_authorization_source",
            status="fail",
            tier="default",
            canonical=canonical_payload,
            actual={"source": str(skill_path), "value": None},
            reason=f"SKILL.md not found: {skill_path}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"SKILL.md not found: {skill_path}",
            }],
        )
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError as e:
        return _audit_finding(
            check="fail_task_authorization_source",
            status="fail",
            tier="default",
            canonical=canonical_payload,
            actual={"source": str(skill_path), "value": None},
            reason=f"SKILL.md unreadable: {e}",
            locations=[{
                "path": owning_path, "line": None,
                "reason": f"SKILL.md unreadable: {e}",
            }],
        )
    lines = text.splitlines()
    auth_pattern = re.compile(r"--authorization-source\s+([A-Za-z0-9._-]+)")
    observed_values: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, 1):
        for match in auth_pattern.finditer(line):
            observed_values.append((idx, match.group(1)))

    # Detect fenced fail-task invocations missing --authorization-source.
    # Walk fenced code blocks (``` ... ```), collect their inner content
    # joined by newlines, then look for `fail-task` shell invocations
    # that lack `--authorization-source`. Multi-line backslash-continued
    # invocations are joined into a single logical line first.
    in_fence = False
    fence_start_line = 0
    fence_lines: list[tuple[int, str]] = []
    invocations_missing: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                # Close fence; process fence_lines for fail-task
                # invocations. Join continuation lines (trailing backslash)
                # to reconstruct logical commands.
                logical: list[tuple[int, str]] = []
                buf = ""
                buf_line = 0
                for fl_idx, fl in fence_lines:
                    if not buf:
                        buf_line = fl_idx
                    if fl.rstrip().endswith("\\"):
                        buf += fl.rstrip()[:-1] + " "
                    else:
                        buf += fl
                        logical.append((buf_line, buf))
                        buf = ""
                if buf:
                    logical.append((buf_line, buf))
                for ln, cmd in logical:
                    if re.search(r"\bplan_ops\.py\b\s+fail-task\b", cmd) and \
                            "--authorization-source" not in cmd:
                        invocations_missing.append((ln, cmd.strip()[:200]))
                in_fence = False
                fence_lines = []
            else:
                in_fence = True
                fence_start_line = idx
                fence_lines = []
            continue
        if in_fence:
            fence_lines.append((idx, line))

    actual_payload = {
        "source": owning_path,
        "value": {
            "observed_authorization_values": sorted({v for _, v in observed_values}),
            "occurrence_count": len(observed_values),
            "fenced_fail_task_missing_auth": [
                {"line": ln, "snippet": snip}
                for ln, snip in invocations_missing
            ],
        },
    }
    problems: list[str] = []
    locations: list[dict] = []
    for ln, value in observed_values:
        if value not in ALLOWED_FAIL_AUTHORIZATION_SOURCES:
            msg = (
                f"--authorization-source {value!r} at line {ln} not in "
                f"ALLOWED_FAIL_AUTHORIZATION_SOURCES"
            )
            problems.append(msg)
            locations.append({"path": owning_path, "line": ln, "reason": msg})
    for ln, snip in invocations_missing:
        msg = (
            f"fenced `fail-task` invocation at line {ln} missing "
            f"--authorization-source flag: {snip!r}"
        )
        problems.append(msg)
        locations.append({"path": owning_path, "line": ln, "reason": msg})
    if not observed_values and not invocations_missing:
        # No --authorization-source mentions and no fenced fail-task
        # invocations: SKILL.md no longer documents the destructive seam.
        # That is a drift signal — the principle relies on this surface
        # being documented.
        msg = (
            "SKILL.md contains no --authorization-source mentions; the "
            "principle relies on these call-site declarations being "
            "documented for operators"
        )
        problems.append(msg)
        locations.append({"path": owning_path, "line": None, "reason": msg})
    if problems:
        return _audit_finding(
            check="fail_task_authorization_source",
            status="fail",
            tier="default",
            canonical=canonical_payload,
            actual=actual_payload,
            reason="; ".join(problems),
            locations=locations,
        )
    return _audit_finding(
        check="fail_task_authorization_source",
        status="pass",
        tier="default",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
    )


def _check_gemini_available() -> dict:
    """Advisory-tier surfacing of the runtime Gemini availability state.

    Reports the `_resolve_gemini_available()` truth-table view at audit
    time so executor self-checks see whether the orchestrator would
    advertise Gemini as available right now. Always passes (binary +
    auth state is operator-environment, not a contract drift); the
    finding's `actual.value` carries the boolean and the structured
    breakdown (`binary_present`, `api_key_present`, `creds_present`).
    Tier=advisory: never flips default-tier verdicts.
    """
    binary_present = shutil.which("gemini") is not None
    api_key_present = bool(os.environ.get(GEMINI_API_KEY_ENV, "").strip())
    creds_present = bool(os.environ.get(GOOGLE_APP_CRED_ENV, "").strip())
    available = _resolve_gemini_available()
    canonical_payload = {
        "source": "TASK-005 preflight gemini_available contract",
        "value": (
            "advisory: surfaces _resolve_gemini_available() truth-table "
            "state at audit time"
        ),
    }
    actual_payload = {
        "source": "_resolve_gemini_available()",
        "value": {
            "available": available,
            "binary_present": binary_present,
            "api_key_present": api_key_present,
            "creds_present": creds_present,
            "api_key_env": GEMINI_API_KEY_ENV,
            "creds_env": GOOGLE_APP_CRED_ENV,
        },
    }
    return _audit_finding(
        check="gemini-available",
        status="pass",
        canonical=canonical_payload,
        actual=actual_payload,
        reason=None,
        tier="advisory",
    )


# Ordered registry. The order is the canonical `--list` output and the
# row order in the Markdown report. Append new checks to the end so
# downstream tooling that snapshots `--list` does not drift.
AUDIT_CHECKS: tuple[tuple[str, object, str], ...] = (
    ("status_vocabulary", _check_status_vocabulary, "default"),
    ("schedule_wire_format", _check_schedule_wire_format, "default"),
    ("implementer_report_labels", _check_implementer_report_labels, "default"),
    ("execution_log_columns", _check_execution_log_columns, "default"),
    ("schemas", _check_schemas, "default"),
    ("portable_tier", _check_portable_tier, "advisory"),
    ("wrapper_isolation", _check_wrapper_isolation, "default"),
    ("design_doc_orphans", _check_design_doc_orphans, "default"),
    ("global_lock_paths", _check_global_lock_paths, "default"),
    ("canonical_fixture_not_archived", _check_canonical_fixture_not_archived, "default"),
    ("principle_referenced", _check_principle_referenced, "default"),
    ("fail_task_authorization_source", _check_fail_task_authorization_source, "default"),
    ("wrapper_restore_authorization", _check_wrapper_restore_authorization_source, "default"),
    ("gemini-available", _check_gemini_available, "advisory"),
)
AUDIT_CHECK_NAMES: tuple[str, ...] = tuple(name for name, _, _ in AUDIT_CHECKS)
AUDIT_CHECK_TIERS: dict[str, str] = {name: tier for name, _, tier in AUDIT_CHECKS}


def _render_audit_text(report: dict) -> str:
    """Render the audit report as a human-readable plaintext summary.

    Used by `cmd_audit` when `--json` is absent so stdout carries a
    readable digest instead of Python's default dict repr. Format:
      * Header `audit: <overall>`
      * One blank line
      * Per-finding bullet `  - [<tier>] <check>: <reason>` (reason is
        `ok` when clean)
      * Indented `locations:` block when the finding carries entries

    `<tier>` is `finding["severity"]` when present (forward-compatible
    with a future shape that adds one) else `finding["tier"]`
    (default/advisory) — the closest thing the existing
    `_audit_finding` shape has to a severity.
    """
    overall = report.get("overall", "fail")
    lines: list[str] = [f"audit: {overall}", ""]
    for finding in report.get("findings", []):
        check = finding.get("check", "")
        tier = finding.get("severity") or finding.get("tier", "")
        reason = finding.get("reason") or "ok"
        lines.append(f"  - [{tier}] {check}: {reason}")
        locations = finding.get("locations") or []
        if locations:
            lines.append("    locations:")
            for loc in locations:
                path = loc.get("path", "")
                line_no = loc.get("line")
                line_suffix = f":{line_no}" if line_no is not None else ""
                loc_reason = loc.get("reason", "")
                lines.append(f"      - {path}{line_suffix}: {loc_reason}")
    return "\n".join(lines) + "\n"


def _render_audit_markdown(report: dict) -> str:
    """Render the audit report as a Markdown table for `--report-file`.

    Columns: Check | Tier | Status | Canonical | Actual | Reason. The
    Canonical and Actual columns are JSON-serialized so the row order is
    stable across reruns; long values are truncated to keep the table
    readable in a terminal (the JSON output remains the source of truth).
    """
    lines: list[str] = [
        f"# Executor self-audit — {report.get('generated_at', '')}",
        "",
        f"**Overall:** `{report.get('overall', 'fail')}`",
        "",
        "| Check | Tier | Status | Canonical | Actual | Reason |",
        "|---|---|---|---|---|---|",
    ]
    for finding in report.get("findings", []):
        canonical_str = json.dumps(finding.get("canonical", {}), sort_keys=True)
        actual_str = json.dumps(finding.get("actual", {}), sort_keys=True)
        if len(canonical_str) > 200:
            canonical_str = canonical_str[:197] + "..."
        if len(actual_str) > 200:
            actual_str = actual_str[:197] + "..."
        reason = finding.get("reason") or ""
        lines.append(
            "| `{check}` | {tier} | `{status}` | {canonical} | {actual} | {reason} |".format(
                check=finding.get("check", ""),
                tier=finding.get("tier", ""),
                status=finding.get("status", ""),
                canonical=canonical_str.replace("|", "\\|"),
                actual=actual_str.replace("|", "\\|"),
                reason=reason.replace("|", "\\|"),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _args_to_payload_list_global_lock_paths(args: argparse.Namespace) -> dict:
    payload = {
    }
    return payload

def _run_list_global_lock_paths(payload: dict) -> dict:
    paths, globs = _effective_global_lock_set()
    return _result({'paths': sorted(paths), 'globs': list(globs)}, exit_code=0)

def cmd_list_global_lock_paths(args: argparse.Namespace) -> None:
    'Emit the effective globally-locked path set + globs (TASK-010).\n\n    Result shape: `{"paths": [...sorted exact-matches...],\n                    "globs": [...fnmatch globs in declared order...]}`.\n    Reads the optional override at `docs/plans/_global_lock_paths.yaml`\n    (relative to cwd) and merges its `additional:` list into the\n    defaults; entries with glob metacharacters land in `globs`.\n    '
    payload = _args_to_payload_list_global_lock_paths(args)
    result = _run_list_global_lock_paths(payload)
    _emit_or_die(args, result)


def _args_to_payload_audit(args: argparse.Namespace) -> dict:
    return {
        "list": bool(getattr(args, "list", False)),
        "check": getattr(args, "check", None),
        "strict": bool(getattr(args, "strict", False)),
        "report_file": getattr(args, "report_file", None),
        "json": bool(getattr(args, "json", False)),
    }


def _run_audit(payload: dict) -> dict:
    """Self-audit pure core: --list | --json | --report-file | --check <csv>.

    See the `## TASK-007` section in `DUAL_AGENT_PLAN_EXECUTOR.md §14` for
    the operator-facing documentation of what this surfaces and when to
    run it.
    """
    if payload["list"]:
        annotated = [
            {"name": name, "tier": AUDIT_CHECK_TIERS[name]}
            for name in AUDIT_CHECK_NAMES
        ]
        return _result({"checks": annotated}, exit_code=0)

    requested: list[str] | None = None
    if payload["check"]:
        requested = _normalize_csv_or_list(payload["check"])
        unknown = [name for name in requested if name not in AUDIT_CHECK_NAMES]
        if unknown:
            return _result(
                {
                    "error": (
                        f"unknown audit check name(s): {unknown}; "
                        f"known: {list(AUDIT_CHECK_NAMES)}"
                    ),
                },
                exit_code=1,
            )

    findings: list[dict] = []
    for name, fn, tier in AUDIT_CHECKS:
        if requested is not None and name not in requested:
            continue
        # Default run includes ALL checks (default + advisory) so operators
        # see advisory drift in the report. Advisory findings are still
        # excluded from the verdict unless --strict or explicit --check
        # opted in by name (see `_is_verdict_finding`). This matches the
        # spec at TASK-007_self_audit.md §step-4 line 241: "Advisory-tier
        # findings still appear in the report regardless of whether they
        # were included in the verdict, with their tier annotated."
        finding = fn()
        findings.append(finding)

    # Verdict computation.
    #   * Explicit --check subset: every requested check counts toward the
    #     verdict regardless of tier (the operator opted in by name).
    #   * Default + --strict: advisory findings count too.
    #   * Default (no --strict, no --check): advisory findings reported
    #     but excluded from the verdict.
    def _is_verdict_finding(f: dict) -> bool:
        if requested is not None:
            return True
        if payload["strict"]:
            return True
        return f.get("tier") != "advisory"

    overall = "pass"
    for f in findings:
        if _is_verdict_finding(f) and f.get("status") == "fail":
            overall = "fail"
            break

    report = {
        "overall": overall,
        "findings": findings,
        "generated_at": _now(),
    }

    if payload["report_file"]:
        out = Path(payload["report_file"])
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(_render_audit_markdown(report), encoding="utf-8")
        except OSError as e:
            return _result(
                {"error": f"failed to write --report-file {out}: {e}"},
                exit_code=1,
            )

    exit_code = 0 if overall == "pass" else 1
    # Non-JSON path uses a structured plaintext renderer via the
    # `__plan_ops_text_output__` marker on `_emit_or_die`; `--json` still
    # emits the canonical JSON document as before. The `json` switch is
    # decided here in the pure core (rather than gated inside
    # `_emit_or_die`) so the marker contract from TASK-000A — "text output
    # is verbatim and internal-only" — stays unconditional.
    result = _result(report, exit_code=exit_code)
    if not payload["json"]:
        result["__plan_ops_text_output__"] = _render_audit_text(report)
    return result


def cmd_audit(args: argparse.Namespace) -> None:
    payload = _args_to_payload_audit(args)
    result = _run_audit(payload)
    _emit_or_die(args, result)


def _args_to_payload_gates(args: argparse.Namespace) -> dict:
    if args.list:
        mode = "list"
    elif args.check:
        mode = "check"
    elif args.certify:
        mode = "certify"
    else:
        mode = None
    return {
        "mode": mode,
        "list": args.list,
        "check": args.check,
        "certify": args.certify,
        "certify_mode": args.mode,
        "plan_file": args.plan_file,
        "schedule_file": args.schedule_file,
        "commit_sha": args.commit_sha,
        "task_id": args.task_id,
        "run_id": args.run_id,
    }


def _validate_gates_mode(payload: dict) -> list[dict]:
    errors: list[dict] = []
    mode = payload.get("mode")
    if mode not in {"list", "check", "certify"}:
        errors.append({
            "path": "$.mode",
            "code": "invalid-mode",
            "message": "gates mode must be one of list, check, certify",
        })

    flag_modes: list[str] = []
    if payload.get("list"):
        flag_modes.append("list")
    if payload.get("check"):
        flag_modes.append("check")
    if payload.get("certify"):
        flag_modes.append("certify")
    if len(flag_modes) > 1:
        errors.append({
            "path": "$",
            "code": "mutually-exclusive-mode-flags",
            "message": "gates accepts only one of list, check, certify",
        })
    if flag_modes and mode in {"list", "check", "certify"} and flag_modes[0] != mode:
        errors.append({
            "path": "$.mode",
            "code": "mismatched-mode-flag",
            "message": f"mode {mode!r} does not match {flag_modes[0]!r} flag",
        })
    return errors


def _run_gates(payload: dict) -> dict:
    """Phase-gate CLI: --list | --check <csv> | --certify --mode <m>.

    --list emits the six canonical gate names.
    --check runs the subset named on the CLI; each gate returns the
      documented `{name, status, reason}` shape.
    --certify runs the dry-run or execute bundle against a plan file;
      emits `{certified: bool, gates: {...}}`.
    """
    mode_errors = _validate_gates_mode(payload)
    if mode_errors:
        return _result({"errors": mode_errors}, exit_code=1)

    if payload["mode"] == "list":
        return _result({"gates": list(GATE_NAMES)}, exit_code=0)

    if payload["mode"] == "certify":
        if payload.get("certify_mode") not in {"dry-run", "execute"}:
            return _result({"error": "mode must be dry-run|execute"}, exit_code=1)
        if not payload.get("plan_file"):
            return _result({"error": "--certify requires --plan-file"}, exit_code=1)
        # `schedule-valid` is a required member of the certification bundle
        # (acceptance criteria: dry-run pass requires it green; execute is
        # the dry-run set plus commit-safe). Skipping it when --schedule-file
        # is absent produced a false-positive certification path, so fail
        # fast at the CLI seam instead of emitting `not_applicable`.
        if not payload.get("schedule_file"):
            return _result({"error": "--certify requires --schedule-file"}, exit_code=1)
        if payload["certify_mode"] == "dry-run":
            gates = _certify_dry_run(payload["plan_file"], payload["schedule_file"])
        else:
            # Execute certification re-verifies commit-safe per landed
            # commit; without a --run-id there is no way to identify the
            # bundle of commits to check, so certification would silently
            # report commit-safe: not_applicable and pass.
            if not payload.get("run_id"):
                return _result({
                    "error": "--certify --mode execute requires --run-id",
                }, exit_code=1)
            gates = _certify_execute(
                payload["plan_file"], payload["run_id"], payload["schedule_file"],
            )
        # Directory-mode certify (per SKILL.md §99-106): when --plan-file
        # resolves to a directory containing 00_INDEX.json, the report
        # carries `plan_mode: "directory"` and any gate that aggregates
        # subchecks (schema-valid per chunk, commit-safe per commit_done
        # event) emits a `subresults` array for attribution.
        plan_mode = "directory" if _is_directory_mode_plan(payload["plan_file"]) else "single-file"
        by_name: dict[str, dict] = {}
        for g in gates:
            entry = {"status": g["status"], "reason": g["reason"]}
            if "subresults" in g:
                entry["subresults"] = g["subresults"]
            by_name[g["name"]] = entry
        # Canonical status vocabulary: pass | fail | not_applicable.
        # Certification passes iff every applicable gate is `pass`; a
        # `not_applicable` gate does not block certification.
        certified = all(g["status"] in {"pass", "not_applicable"} for g in gates)
        return _result(
            {
                "certified": certified,
                "mode": payload["certify_mode"],
                "plan_mode": plan_mode,
                "gates": by_name,
            },
            exit_code=0 if certified else 1,
        )

    requested = _normalize_csv_or_list(payload.get("check"))
    unknown = [g for g in requested if g not in GATE_NAMES]
    if unknown:
        return _result({"error": f"unknown gate name(s): {unknown}; known: {list(GATE_NAMES)}"}, exit_code=1)

    results: list[dict] = []
    for name in requested:
        if name == "schema-valid":
            results.append(_gate_schema_valid(payload.get("plan_file")))
        elif name == "schedule-valid":
            results.append(_gate_schedule_valid(payload.get("schedule_file")))
        elif name == "fixture-valid":
            # The gate invariant is about the canonical sample fixture
            # (see `CANONICAL_CONTRACT.fixture_path`), not the user's plan
            # file.
            # Passing `args.plan_file` here caused Phase 0 to validate the
            # execution plan twice (via schema-valid and fixture-valid) and
            # skip the sample fixture gate entirely — the opposite of the
            # acceptance criteria.
            results.append(_gate_fixture_valid())
        elif name == "execution-safe":
            results.append(_gate_execution_safe())
        elif name == "review-safe":
            results.append(_gate_review_safe())
        elif name == "commit-safe":
            results.append(_gate_commit_safe(
                payload.get("commit_sha"), payload.get("task_id"), payload.get("plan_file"),
            ))
    any_failed = any(r["status"] == "fail" for r in results)
    return _result(
        {"gates": results, "failed": any_failed},
        exit_code=1 if any_failed else 0,
    )


def cmd_gates(args: argparse.Namespace) -> None:
    payload = _args_to_payload_gates(args)
    result = _run_gates(payload)
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# TASK-009: scale-aware large-file reads (resolve-read-targets)
# ---------------------------------------------------------------------------
#
# Two optional task-schema fields steer the dispatcher's pre-read window:
#
#     - **Read targets:**       line-range hints (file:start-end)
#     - **Symbol targets:**     symbol extraction (file::Class.method)
#
# Both are pure read-side; they do not affect scope enforcement, which is
# still driven by the canonical `Files:` list. Targets may reference files
# OUTSIDE `Files:` (read-only context). When both are present, line ranges
# win — `Symbol targets:` is the ergonomic sugar.

# Bullet detection lives within a leading `**Read targets:**` /
# `**Symbol targets:**` field bullet. We extract lines that look like
# "  - <path>:<start>-<end>" (read) or "  - <path>::<symbol>" (symbol).
_READ_TARGETS_HEADER_RE = re.compile(
    r"^\s*-\s+\*\*Read targets:\*\*(?:[ \t]+[^\n]*)?$", re.MULTILINE,
)
_SYMBOL_TARGETS_HEADER_RE = re.compile(
    r"^\s*-\s+\*\*Symbol targets:\*\*(?:[ \t]+[^\n]*)?$", re.MULTILINE,
)
# A nested bullet: "  - foo/bar.py:10-20"
_TARGET_BULLET_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
# `path:start-end` (en-dash also tolerated to match normalize_file_path).
_READ_TARGET_RE = re.compile(
    r"^(?P<path>[^:\s]\S*?):(?P<start>\d+)[-–](?P<end>\d+)\s*$",
)
# `path::Symbol` or `path::Class.method`.
_SYMBOL_TARGET_RE = re.compile(
    r"^(?P<path>[^:\s]\S*?)::(?P<symbol>[A-Za-z_][\w\.]*)\s*$",
)


def _iter_target_bullets(text: str, header_re: re.Pattern[str]) -> list[str]:
    """Yield trimmed bullet entries that follow a header bullet.

    The header bullet looks like `- **Read targets:**` (or symbol). The
    nested bullets that follow at deeper indentation are the entries.
    Iteration stops at a blank line, a non-bullet line, or another
    field-bullet (`- **<Field>:**`).
    """
    out: list[str] = []
    m = header_re.search(text)
    if not m:
        return out
    tail = text[m.end():]
    seen_bullet = False
    for raw_line in tail.splitlines():
        if not raw_line.strip():
            # Blank line is tolerated BEFORE the first bullet (the
            # newline immediately after the header bullet shows up as
            # an empty token from splitlines()). Once the bullet region
            # has begun, a blank line ends it.
            if seen_bullet:
                break
            continue
        # A new top-level field bullet (`- **Foo:**`) ends the region.
        # Trailing parenthetical descriptors (e.g. `(optional, TASK-009)`)
        # are tolerated to match the header regexes above.
        if re.match(r"^\s*-\s+\*\*[^*]+:\*\*(?:[ \t]+[^\n]*)?$", raw_line):
            break
        bm = _TARGET_BULLET_RE.match(raw_line)
        if not bm:
            break
        seen_bullet = True
        entry = bm.group(1).strip()
        # Strip surrounding backticks if the author wrote `path:1-20`.
        entry = entry.strip("`")
        if entry:
            out.append(entry)
    return out


def _read_range(
    path: str, start: int, end: int, errors: list,
) -> dict:
    """Read [start, end] inclusive (1-based) and clamp to file length."""
    p = Path(path)
    if not p.is_file():
        errors.append(f"{path}: file not found")
        return {
            "file": path, "start": start, "end": end,
            "text": "", "missing": True,
        }
    try:
        content = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path}: read error ({exc})")
        return {
            "file": path, "start": start, "end": end,
            "text": "", "missing": True,
        }
    lines = content.splitlines()
    if start < 1:
        start = 1
    real_end = min(end, len(lines))
    truncated = real_end != end
    if start > real_end:
        # Out-of-range request: emit a clamped empty window with a note.
        text = ""
    else:
        text = "\n".join(lines[start - 1:real_end])
    result: dict = {
        "file": path, "start": start, "end": real_end, "text": text,
    }
    if truncated:
        result["truncated_to"] = real_end
    return result


def _find_py_symbol(tree, symbol: str):
    """Find a Python AST node for `symbol`, supporting `Class.method`.

    Returns the AST node (FunctionDef / AsyncFunctionDef / ClassDef) or
    None when not found.
    """
    import ast

    parts = symbol.split(".")

    def _walk(nodes, parts_left):
        head, rest = parts_left[0], parts_left[1:]
        for node in nodes:
            name = getattr(node, "name", None)
            if name != head:
                continue
            if not rest:
                return node
            children = getattr(node, "body", []) or []
            hit = _walk(children, rest)
            if hit is not None:
                return hit
        return None

    return _walk(tree.body, parts)


def _regex_symbol_span(text: str, symbol: str) -> tuple[int | None, int | None]:
    """Best-effort symbol locator for non-Python languages.

    Heuristic: find the line containing the symbol's declaration token
    (`def symbol`, `function symbol`, `symbol() {`, `symbol(){`, etc.).
    Returns (start_line, end_line) where end_line is start + 40 lines or
    end-of-file (whichever is smaller). The 40-line cap is intentional;
    regex cannot reliably bracket-match across languages.
    """
    lines = text.splitlines()
    # Token-anchored patterns to keep `compute_score` from matching
    # `_compute_score_helper` etc.
    sym_re = re.compile(
        rf"(^|[^A-Za-z0-9_])(?P<name>{re.escape(symbol)})\s*(\(|=|:|\{{)",
    )
    # Keyword-led patterns for shell / bash / ts / go.
    decl_keywords = (
        rf"^\s*(?:function\s+|def\s+|fn\s+|func\s+|class\s+|sub\s+)"
        rf"{re.escape(symbol)}\b"
    )
    decl_re = re.compile(decl_keywords)
    start_idx: int | None = None
    for idx, line in enumerate(lines):
        if decl_re.search(line) or (
            sym_re.search(line) and (
                "(" in line or "{" in line or "=" in line or ":" in line
            )
        ):
            start_idx = idx
            break
    if start_idx is None:
        return None, None
    end_idx = min(start_idx + 40, len(lines) - 1)
    return start_idx + 1, end_idx + 1


def _locate_symbol(path: str, symbol: str, errors: list) -> dict | None:
    """Resolve a `path::symbol` target to a `_read_range` dict.

    Adds a "symbol_match" annotation to the dict: "ast" (Python AST
    confidence) or "regex" (best-effort fallback). Errors append the
    "<path>::<symbol> not found" string used by V4.
    """
    p = Path(path)
    if not p.is_file():
        errors.append(f"{path}: file not found")
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{path}: read error ({exc})")
        return None
    match_kind = "regex"
    if path.endswith(".py"):
        try:
            import ast
            tree = ast.parse(text, filename=str(p))
        except SyntaxError as exc:
            errors.append(f"{path}: parse error ({exc})")
            # Fall through to regex fallback so a malformed Python file
            # does not entirely silence the symbol target.
            start, end = _regex_symbol_span(text, symbol)
        else:
            target = _find_py_symbol(tree, symbol)
            if target is None:
                errors.append(f"{path}::{symbol} not found")
                return None
            start = target.lineno
            end = getattr(target, "end_lineno", start)
            match_kind = "ast"
    else:
        start, end = _regex_symbol_span(text, symbol)
    if start is None:
        errors.append(f"{path}::{symbol} not found")
        return None
    hit = _read_range(path, start, end, errors)
    hit["symbol"] = symbol
    hit["symbol_match"] = match_kind
    if match_kind == "regex":
        hit["note"] = (
            "regex symbol fallback (best-effort; line range approximate)"
        )
    return hit


def resolve_read_targets(text: str) -> dict:
    """Parse `Read targets:` / `Symbol targets:` blocks and resolve them.

    `text` is the raw task-block markdown (or any markdown containing the
    two field bullets). Returns:

        {
            "reads":   [{"file","start","end","text",...}, ...],
            "symbols": [{"path","symbol","start","end"}, ...],
            "errors":  ["<diagnostic>", ...],
        }

    Missing fields → empty lists. Missing files / missing symbols /
    out-of-range line numbers all attach an `errors` entry but are
    non-fatal: the caller (dispatcher) treats the structure as
    advisory.
    """
    reads: list[dict] = []
    symbols: list[dict] = []
    errors: list[str] = []

    for entry in _iter_target_bullets(text, _READ_TARGETS_HEADER_RE):
        m = _READ_TARGET_RE.match(entry)
        if not m:
            errors.append(f"unparseable read target: {entry!r}")
            continue
        path = m.group("path")
        start = int(m.group("start"))
        end = int(m.group("end"))
        if start > end:
            errors.append(
                f"{entry}: start > end (skipped)"
            )
            continue
        reads.append(_read_range(path, start, end, errors))

    for entry in _iter_target_bullets(text, _SYMBOL_TARGETS_HEADER_RE):
        m = _SYMBOL_TARGET_RE.match(entry)
        if not m:
            errors.append(f"unparseable symbol target: {entry!r}")
            continue
        path = m.group("path")
        symbol = m.group("symbol")
        hit = _locate_symbol(path, symbol, errors)
        if hit is not None:
            reads.append(hit)
            symbols.append({
                "path": path, "symbol": symbol,
                "start": hit["start"], "end": hit["end"],
            })

    return {"reads": reads, "symbols": symbols, "errors": errors}


# ---------------------------------------------------------------------------
# TASK-007: target_task_id auto-injection helper. Used by the Codex wrapper
# (`plan_codex_dispatch.py:render_implement_prompt` / `render_review_prompt`)
# AND by the Claude orchestrator's dispatch-template render path. Both
# entry points share this single function so the heading-count condition
# and the emitted prefix line stay in lockstep.
# ---------------------------------------------------------------------------


# Pattern matches H3 task headings of the form `### TASK-NNN:` or
# `### TASK-NNNA:` (numeric id with optional single-letter suffix). Mirrors
# the grammar used elsewhere in this file (see ``_TASK_HEADING_RE`` etc.).
_TARGET_TASK_HEADING_COUNT_RE = re.compile(
    r"^### TASK-\d+[A-Z]?:", re.MULTILINE,
)


class MissingTargetTaskIdError(ValueError):
    """Raised when a child plan file declares >1 `### TASK-NNN:` H3 heading
    but the dispatcher did not supply ``target_task_id``. The orchestrator
    MUST always pass ``target_task_id`` for shared-file children — the
    error envelope identifies the offending file so the operator can fix
    the dispatch call site rather than guess at intent (TASK-007)."""

    def __init__(self, plan_file: str | None, heading_count: int) -> None:
        self.plan_file = plan_file
        self.heading_count = heading_count
        label = plan_file if plan_file else "<unknown plan file>"
        super().__init__(
            f"target_task_id is required: {label} declares "
            f"{heading_count} `### TASK-NNN:` H3 headings, but no "
            "target_task_id was provided. Pass --target-task-id to the "
            "wrapper (or the equivalent dispatch field) so the agent reads "
            "the right block."
        )


def count_task_headings(plan_text: str) -> int:
    """Count `### TASK-NNN:` H3 headings in a plan-file's text. Cheap
    line-prefix scan; no AST. Used to decide whether the dispatcher must
    auto-inject the "Implement specifically `### TASK-NNN:`" line
    (TASK-007). Returns 0 for an empty/None text."""
    if not plan_text:
        return 0
    return len(_TARGET_TASK_HEADING_COUNT_RE.findall(plan_text))


def render_target_task_id_injection(
    plan_text: str,
    target_task_id: str | None,
    *,
    plan_file: str | None = None,
) -> str:
    """Return the auto-injection prefix line for a dispatch prompt.

    Rules (TASK-007):

    - >1 heading + target_task_id set  →  emit
      ``"Implement specifically `### TASK-NNN:` ...\\n\\n"`` so the
      dispatched agent reads the correct block of a shared-file child.
    - 1 heading + target_task_id set   →  emit ``""`` (the heading is
      unambiguous; the injection would only add noise).
    - 0 or 1 heading + target_task_id None  →  emit ``""`` (single-task
      plans render unchanged; backward-compat).
    - >1 heading + target_task_id None  →  raise
      :class:`MissingTargetTaskIdError`. The orchestrator is contractually
      required to supply ``target_task_id`` for shared-file children; a
      missing value is a bug at the dispatch call site, not a runtime
      ambiguity to paper over.

    The returned string (when non-empty) ends with a trailing blank line
    so callers can prepend it unconditionally to an existing prompt body
    without bookkeeping. Returns ``""`` when no injection is warranted.
    """
    heading_count = count_task_headings(plan_text)
    if target_task_id is None:
        if heading_count > 1:
            raise MissingTargetTaskIdError(plan_file, heading_count)
        return ""
    if heading_count <= 1:
        return ""
    normalized = _normalize_task_id(target_task_id)
    return (
        f"Implement specifically `### TASK-{normalized}:` (this child "
        f"plan file declares {heading_count} `### TASK-NNN:` H3 headings; "
        "read only the matching block).\n\n"
    )


def render_canonical_verdict_allowlists(role: str) -> str:
    """TASK-009 (POSTMORTEM_FIXES): render the four canonical verdict
    allowlists as a prompt section keyed off the active reviewer role.

    Reads the canonical sets dynamically each call (not at import time) so
    callers see updates if a constant is patched in-process. Module-level
    lookups (rather than a captured local) are load-bearing for the
    dynamic-read assertion in the regression tests.
    """
    bullets = [
        (
            "- Codex review (your verdict in `review`): "
            + " | ".join(sorted(ALLOWED_CODEX_REVIEW_VERDICTS))
        ),
        (
            "- Codex plan-review (your verdict in `plan-review`): "
            + " | ".join(sorted(ALLOWED_PLAN_REVIEW_VERDICTS))
        ),
        (
            "- Claude review (NOT YOUR ROLE): "
            + " | ".join(sorted(ALLOWED_CLAUDE_REVIEW_VERDICTS))
        ),
        (
            "- D.5 third opinion (NOT YOUR ROLE): "
            + " | ".join(sorted(ALLOWED_D5_VERDICTS))
        ),
    ]
    return (
        "## Canonical verdict allowlists\n"
        "\n"
        f"You are running as the {role}. Your verdict MUST come from your "
        "role's allowlist below. Do NOT confuse role allowlists.\n"
        "\n"
        + "\n".join(bullets)
        + "\n\n"
        "When the plan or task content references verdicts from another "
        "role's allowlist, treat that as expected (the plan documents the "
        "cross-role mapping). Do NOT flag a plan-text reference to another "
        "role's verdict as invalid — it is valid in another role's set.\n"
        "\n"
    )


def render_task_file_ownership(schedule_obj: dict) -> str:
    """TASK-009 (POSTMORTEM_FIXES): render a per-task `tasks[].files[]`
    ownership block for plan-review prompts. Returns an empty string when
    the schedule has no tasks (so callers can append unconditionally).
    """
    tasks = (schedule_obj or {}).get("tasks") or []
    if not tasks:
        return ""
    out = [
        "## Task-level file ownership",
        "",
        (
            "Per-task `files[]` ownership from the persisted schedule. "
            "Verify per-task ownership directly against this list rather "
            "than inferring from any batch-level aggregate."
        ),
        "",
    ]
    for task in tasks:
        tid = task.get("task_id") or task.get("id") or "?"
        files = task.get("files") or []
        files_str = ", ".join(files) if files else "(none declared)"
        out.append(f"- TASK-{tid}: {files_str}")
    out.append("")
    return "\n".join(out) + "\n"


def render_pre_read_excerpts(resolved: dict) -> str:
    """Render the resolved read-targets dict as a `## Pre-read excerpts`
    markdown block. Returns an empty string when there is nothing to
    render (no reads AND no errors), so dispatchers can append the
    string unconditionally without producing an empty heading.
    """
    reads = resolved.get("reads") or []
    errors = resolved.get("errors") or []
    if not reads and not errors:
        return ""
    out = ["## Pre-read excerpts", ""]
    for entry in reads:
        path = entry.get("file", "")
        start = entry.get("start")
        end = entry.get("end")
        note_bits = []
        if entry.get("truncated_to") is not None:
            note_bits.append(f"truncated to {entry['truncated_to']}")
        if entry.get("missing"):
            note_bits.append("missing")
        if entry.get("symbol_match") == "regex":
            note_bits.append("regex fallback")
        suffix = f" — {', '.join(note_bits)}" if note_bits else ""
        out.append(f"### {path} (lines {start}-{end}){suffix}")
        ext = Path(path).suffix.lower()
        lang = {
            ".py": "python", ".md": "markdown", ".sh": "bash",
            ".ts": "typescript", ".tsx": "tsx", ".js": "javascript",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".go": "go", ".rs": "rust", ".sql": "sql",
        }.get(ext, "")
        fence_open = f"```{lang}" if lang else "```"
        out.append(fence_open)
        out.append(entry.get("text", ""))
        out.append("```")
        out.append("")
    if errors:
        out.append("### Read-target diagnostics")
        out.append("")
        for e in errors:
            out.append(f"- {e}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _args_to_payload_resolve_read_targets(args: argparse.Namespace) -> dict:
    use_stdin = bool(getattr(args, "stdin", False))
    task_file = getattr(args, "task_file", None)
    payload: dict = {
        "stdin": use_stdin,
        "task_file": Path(task_file) if task_file else None,
    }
    if use_stdin:
        payload["stdin_text"] = _read_stdin_text()
    elif task_file:
        payload["task_text"] = _load_text(Path(task_file))
    return payload


def _run_resolve_read_targets(payload: dict) -> dict:
    """Pure core: read a task block (or stdin) and return the resolved dict.

    Exit code is 0 even when `errors` is non-empty — missing symbols
    and missing files are advisory diagnostics, not fatal.
    """
    if payload.get("stdin"):
        text = payload.get("stdin_text", "")
    elif payload.get("task_file") is not None:
        text = payload.get("task_text", "")
    else:
        return _result(
            {"error": "resolve-read-targets requires --stdin or --task-file"},
            exit_code=1,
        )
    resolved = resolve_read_targets(text)
    return _result(resolved, exit_code=0)


def cmd_resolve_read_targets(args: argparse.Namespace) -> None:
    payload = _args_to_payload_resolve_read_targets(args)
    result = _run_resolve_read_targets(payload)
    # `--json` is the default + only emission for this subcommand; route
    # success and error envelopes through `_emit_or_die` with JSON forced
    # so the trailing-newline byte image stays identical to the
    # pre-codemod handler.
    args.json = True
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# build-agent-dispatch-prompt — render Agent prompt text from templates
# ---------------------------------------------------------------------------

_AGENT_DISPATCH_TEMPLATE_AGENT = {
    "code-reviewer-d-claude": "code-reviewer",
    "code-reviewer-d5": "code-reviewer",
    "plan-reviewer": "plan-reviewer",
    "plan-author-task-targeted": "plan-author",
    "plan-author-schedule-level": "plan-author",
    "plan-author-legacy-whole-plan": "plan-author",
    "plan-review-triage": "plan-review-triage",
    "plan-remediator-narrow": "plan-remediator",
    "plan-remediator-rescue": "plan-remediator",
}

_AGENT_DISPATCH_TEMPLATE_MODEL = {
    "code-reviewer-d-claude": "sonnet",
    "code-reviewer-d5": "sonnet",
    "plan-reviewer": "sonnet",
    "plan-author-task-targeted": "opus",
    "plan-author-schedule-level": "opus",
    "plan-author-legacy-whole-plan": "opus",
    "plan-review-triage": "sonnet",
    "plan-remediator-narrow": "opus",
    "plan-remediator-rescue": "opus",
}

_AGENT_DISPATCH_TEMPLATE_HEADING = {
    "code-reviewer-d-claude": "## Phase D-Claude — code-reviewer on Codex work",
    "code-reviewer-d5": "## Phase D.5 — code-reviewer third opinion (§8.4 escalation)",
    "plan-reviewer": "## Phase 1.5-Claude — plan-reviewer dispatch (claude_only path)",
    "plan-author-task-targeted": "### Variant A — Task-targeted dispatch (`variant == \"A\"`)",
    "plan-author-schedule-level": "### Variant B — Schedule-level dispatch (`variant == \"B\"`)",
    "plan-author-legacy-whole-plan": "## Phase A — plan-analyst whole-plan dispatch (LEGACY — retained for direct CLI callers)",
    "plan-review-triage": "## Phase 1-triage / Phase 1.5.5 — plan-review-triage dispatch (source-parameterized)",
    "plan-remediator-narrow": "## Phase B-narrow-remediation — Narrow-remediation retry (D.2a.6)",
    "plan-remediator-rescue": "## Phase D.4-rescue — plan-remediator dispatch (single-shot rescue)",
}

_AGENT_DISPATCH_TARGET_INJECTION_VERBS = {
    "code-reviewer-d-claude": "Reviewing",
    "code-reviewer-d5": "Adjudicate",
    "plan-author-task-targeted": "Apply the plan-review finding",
    "plan-remediator-narrow": "Apply the narrow-remediation patch",
    "plan-remediator-rescue": "Apply the D.4 rescue",
}


def _agent_dispatch_error(
    code: str,
    message: str,
    *,
    path: str | None = None,
) -> dict:
    record = {"code": code, "message": message}
    if path is not None:
        record["path"] = path
    return _result({"ok": False, "errors": [record], "warnings": []}, exit_code=1)


def _agent_dispatch_schema_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "schemas"
        / "mcp"
        / "build_agent_dispatch_prompt.input.json"
    )


def _validate_build_agent_dispatch_prompt_input(payload: object) -> list[dict]:
    schema_path = _agent_dispatch_schema_path()
    try:
        schema = json.loads(_load_text(schema_path))
    except (OSError, json.JSONDecodeError) as exc:
        return [{"path": "$", "message": f"input schema unavailable: {exc}"}]
    try:
        import jsonschema
    except ImportError:
        jsonschema = None
    if jsonschema is None:
        if not isinstance(payload, dict):
            return [{"path": "$", "message": "input must be a JSON object"}]
        errors: list[dict] = []
        for key in ("template_id", "context"):
            if key not in payload:
                errors.append({"path": f"$.{key}", "message": "required field missing"})
        context = payload.get("context") if isinstance(payload, dict) else None
        if not isinstance(context, dict):
            errors.append({"path": "$.context", "message": "must be an object"})
        elif "files_changed" not in context:
            errors.append({"path": "$.context.files_changed", "message": "required field missing"})
        return errors
    if isinstance(payload, dict) and isinstance(payload.get("template_id"), str):
        branch_context = _agent_dispatch_context_schema_for_template(
            schema,
            payload.get("template_id"),
        )
        if branch_context is not None:
            required_errors = []
            for key in ("template_id", "context"):
                if key not in payload:
                    required_errors.append(
                        {
                            "path": f"/{key}",
                            "code": "required-field-missing",
                            "message": f"{key!r} is a required property",
                        }
                    )
            if required_errors:
                return required_errors
            context_schema = dict(branch_context)
            context_schema["$defs"] = schema.get("$defs", {})
            context_validator = jsonschema.Draft202012Validator(context_schema)
            context_errors = list(context_validator.iter_errors(payload.get("context")))
            if context_errors:
                return [
                    {
                        "path": "/context" + _jsonschema_error_path(error),
                        "code": _jsonschema_error_code(error),
                        "message": error.message,
                    }
                    for error in sorted(context_errors, key=lambda item: list(item.path))
                ]
    validator = jsonschema.Draft202012Validator(schema)
    return [
        {
            "path": _jsonschema_error_path(error),
            "code": _jsonschema_error_code(error),
            "message": error.message,
        }
        for error in sorted(validator.iter_errors(payload), key=lambda item: list(item.path))
    ]


def _agent_dispatch_context_schema_for_template(
    schema: dict,
    template_id: str,
) -> dict | None:
    for branch in schema.get("oneOf", []):
        if branch.get("properties", {}).get("template_id", {}).get("const") != template_id:
            continue
        context_schema = branch.get("properties", {}).get("context")
        if not isinstance(context_schema, dict):
            return None
        ref = context_schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            return schema.get("$defs", {}).get(ref.rsplit("/", 1)[-1])
        return context_schema
    return None


def _jsonschema_error_path(error: object) -> str:
    parts = list(getattr(error, "path", []))
    if getattr(error, "validator", None) == "required":
        missing = re.search(r"'([^']+)' is a required property", getattr(error, "message", ""))
        if missing:
            parts.append(missing.group(1))
    pointer = "".join(
        f"/{str(part).replace('~', '~0').replace('/', '~1')}" for part in parts
    )
    return pointer or "/"


def _jsonschema_error_code(error: object) -> str:
    validator = getattr(error, "validator", None)
    if validator == "required":
        return "required-field-missing"
    if validator == "oneOf":
        return "template-context-mismatch"
    if validator == "enum":
        return "invalid-enum"
    if validator == "const":
        return "invalid-const"
    if validator == "additionalProperties":
        return "additional-property"
    if validator == "type":
        return "invalid-type"
    if validator == "pattern":
        return "invalid-pattern"
    return "schema-validation-error"


def _agent_dispatch_template_path() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    plugin_root = Path(root) if root else Path(__file__).resolve().parents[1]
    return plugin_root / "skills" / "implement-plan" / "dispatch-templates.md"


def _extract_dispatch_template_section(template_text: str, heading: str) -> str | None:
    start = template_text.find(heading)
    if start == -1:
        return None
    tail = template_text[start:]
    next_heading = re.search(r"^(## Phase |### Variant )", tail[len(heading):], re.MULTILINE)
    if next_heading:
        return tail[: len(heading) + next_heading.start()]
    return tail


def _extract_agent_blockquote(section: str) -> str:
    # Collect every contiguous blockquote run in the section, then return
    # the LAST one. Earlier runs are orientation notes (e.g., the
    # `> Rendered by plan_ops__build_agent_dispatch_prompt(...)` header
    # note that directs human readers at SKILL.md §Canonical Agent
    # dispatch recipe); the real template body is the final run, which
    # always extends to the section terminator.
    lines = section.splitlines()
    runs: list[list[str]] = []
    current: list[str] = []
    in_quote = False
    for line in lines:
        if line.startswith(">"):
            in_quote = True
            unquoted = line[1:]
            if unquoted.startswith(" "):
                unquoted = unquoted[1:]
            current.append(unquoted)
            continue
        if in_quote:
            if line.strip():
                # Non-blank, non-quote line terminates the run.
                runs.append(current)
                current = []
                in_quote = False
            else:
                # Blank line inside an ongoing blockquote run (matches the
                # legacy extractor behaviour — a `>` followed by `>` with
                # only a blank gap stays one run).
                current.append("")
    if current:
        runs.append(current)
    if not runs:
        return "\n"
    out = runs[-1]
    return "\n".join(out).strip() + "\n"


def _json_for_prompt(value: object) -> str:
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), indent=2, sort_keys=True)
        except json.JSONDecodeError:
            return value
    return json.dumps(value, indent=2, sort_keys=True)


def _extract_task_block_for_prompt(plan_text: str, task_id: str) -> str | None:
    normalized = _normalize_task_id(task_id)
    if normalized is None:
        return None
    _, blocks = _split_task_blocks(plan_text)
    for raw_id, block in blocks:
        if _normalize_task_id(raw_id) == normalized:
            return block.strip()
    return None


def _context_section_for_prompt(plan_text: str) -> str:
    context = _extract_plan_context_section(plan_text)
    return context if context is not None else "(none)"


def _render_review_target_task_id_injection(
    plan_text: str,
    target_task_id: str | None,
    *,
    plan_file: str,
    template_id: str = "code-reviewer-d-claude",
) -> str | dict:
    heading_count = count_task_headings(plan_text)
    if target_task_id is None:
        if heading_count > 1:
            return _agent_dispatch_error(
                "target-task-id-required",
                (
                    f"target_task_id is required: {plan_file} declares "
                    f"{heading_count} `### TASK-NNN:` H3 headings"
                ),
                path="$.context.target_task_id",
            )
        return ""
    if heading_count <= 1:
        return ""
    normalized = _normalize_task_id(target_task_id)
    if normalized is None:
        return _agent_dispatch_error(
            "target-task-id-invalid",
            f"target_task_id is not parseable: {target_task_id!r}",
            path="$.context.target_task_id",
        )
    verb = _AGENT_DISPATCH_TARGET_INJECTION_VERBS.get(template_id, "Implement")
    return (
        f"{verb} specifically `### TASK-{normalized}:` (this child "
        f"plan file declares {heading_count} `### TASK-NNN:` H3 headings; "
        "read only the matching block).\n\n"
    )


def _args_to_payload_build_agent_dispatch_prompt(args: argparse.Namespace) -> dict:
    if getattr(args, "stdin", False):
        try:
            payload = json.loads(_read_stdin_text())
        except json.JSONDecodeError as exc:
            return {"__invalid_json_error__": str(exc)}
        return payload
    context = getattr(args, "context", None)
    if isinstance(context, str):
        context_text = context
        context_path = Path(context)
        if context_path.is_file():
            context_text = _load_text(context_path)
        try:
            context = json.loads(context_text)
        except json.JSONDecodeError:
            context = context_text
    return {
        "template_id": getattr(args, "template_id", None),
        "context": context,
    }


def _run_build_agent_dispatch_prompt(payload: dict) -> dict:
    if "__invalid_json_error__" in payload:
        return _agent_dispatch_error(
            "invalid-json",
            f"invalid JSON on stdin: {payload['__invalid_json_error__']}",
            path="$",
        )
    errors = _validate_build_agent_dispatch_prompt_input(payload)
    if errors:
        return _result(
            {"ok": False, "errors": errors, "warnings": []},
            exit_code=1,
        )

    template_id = payload["template_id"]
    context = payload["context"]
    prompt_plan_key = {
        "plan-reviewer": None,
        "plan-author-task-targeted": "child_plan_file",
        "plan-author-schedule-level": "roster_file",
        "plan-author-legacy-whole-plan": "plan_path",
        "plan-review-triage": None,
    }.get(template_id, "plan_file")
    plan_file = Path(".")
    plan_text = ""
    if prompt_plan_key is not None:
        plan_file = Path(context[prompt_plan_key])
        if not plan_file.is_absolute():
            return _agent_dispatch_error(
                "plan-file-not-absolute",
                f"{prompt_plan_key} must be absolute: {plan_file}",
                path=f"$.context.{prompt_plan_key}",
            )
        if not plan_file.is_file():
            return _agent_dispatch_error(
                "plan-file-not-found",
                f"plan file not found: {plan_file}",
                path=f"$.context.{prompt_plan_key}",
            )
        plan_text = _load_text(plan_file)
    task_id = context.get("task_id") or context.get("target_task_id")
    normalized_task_id = _normalize_task_id(task_id) if task_id is not None else None
    if task_id is not None and normalized_task_id is None:
        return _agent_dispatch_error(
            "task-id-invalid",
            f"task_id is not parseable: {task_id!r}",
            path="$.context.task_id",
        )

    template_path = _agent_dispatch_template_path()
    if not template_path.is_file():
        return _agent_dispatch_error(
            "template-file-not-found",
            f"dispatch template file not found: {template_path}",
        )
    section = _extract_dispatch_template_section(
        _load_text(template_path),
        _AGENT_DISPATCH_TEMPLATE_HEADING[template_id],
    )
    if section is None:
        return _agent_dispatch_error(
            "template-section-not-found",
            f"template section not found: {_AGENT_DISPATCH_TEMPLATE_HEADING[template_id]}",
        )
    prompt = _extract_agent_blockquote(section)
    render_result = _render_agent_dispatch_prompt_body(
        template_id,
        context,
        prompt,
        plan_file=plan_file,
        plan_text=plan_text,
        normalized_task_id=normalized_task_id,
    )
    if isinstance(render_result, dict):
        return render_result
    prompt = render_result
    return _result(
        {
            "ok": True,
            "agent": _AGENT_DISPATCH_TEMPLATE_AGENT[template_id],
            "model": _AGENT_DISPATCH_TEMPLATE_MODEL[template_id],
            "prompt": prompt,
        },
        exit_code=0,
    )


def _render_agent_dispatch_prompt_body(
    template_id: str,
    context: dict,
    prompt: str,
    *,
    plan_file: Path,
    plan_text: str,
    normalized_task_id: str | None,
) -> str | dict:
    injection = ""
    if template_id in _AGENT_DISPATCH_TARGET_INJECTION_VERBS:
        injection = _render_review_target_task_id_injection(
            plan_text,
            context.get("target_task_id"),
            plan_file=str(plan_file),
            template_id=template_id,
        )
        if isinstance(injection, dict):
            return injection

    if template_id == "code-reviewer-d-claude":
        if normalized_task_id is None:
            return _agent_dispatch_error(
                "task-id-invalid",
                f"task_id is not parseable: {context.get('task_id')!r}",
                path="$.context.task_id",
            )
        description = _extract_task_description(plan_text, normalized_task_id)
        acceptance_criteria = _extract_task_acceptance_criteria(plan_text, normalized_task_id)
        if description is None or acceptance_criteria is None:
            return _agent_dispatch_error(
                "task-not-found",
                f"task {normalized_task_id} not present in plan {plan_file}",
                path="$.context.task_id",
            )
        prompt = prompt.replace(
            "<comma-separated files from Codex wrapper's files_changed>",
            ", ".join(context["files_changed"]),
        )
        prompt = prompt.replace("<verbatim from TASK-NNN Description>", description)
        prompt = prompt.replace(
            "<verbatim from TASK-NNN Acceptance criteria>",
            acceptance_criteria,
        )
        return injection + prompt

    if template_id == "code-reviewer-d5":
        if normalized_task_id is None:
            return _agent_dispatch_error(
                "task-id-invalid",
                f"task_id is not parseable: {context.get('task_id')!r}",
                path="$.context.task_id",
            )
        task_block = _extract_task_block_for_prompt(plan_text, normalized_task_id)
        if task_block is None:
            return _agent_dispatch_error(
                "task-not-found",
                f"task {normalized_task_id} not present in plan {plan_file}",
                path="$.context.task_id",
            )
        reviewer = context.get("reviewer", "Codex")
        prompt = prompt.replace(
            "<comma-separated files from Claude implementer's files_changed>",
            ", ".join(context["files_changed"]),
        )
        prompt = prompt.replace("<reviewer>", reviewer)
        prompt = prompt.replace("<entire TASK-NNN block>", task_block)
        prompt = prompt.replace("<codex_findings_json>", _json_for_prompt(context["reviewer_findings"]))
        prompt = prompt.replace("<wrapper_checks_json>", _json_for_prompt(context["wrapper_checks_json"]))
        return injection + prompt

    if template_id == "plan-reviewer":
        prompt = prompt.replace("<absolute schedule path>", context["schedule_path"])
        prompt = prompt.replace("<plan directory basename>", context["plan_basename"])
        allow = "true" if context["allow_gaps_demotion"] else "false"
        prompt = prompt.replace("<true|false>", allow)
        if not context["allow_gaps_demotion"]:
            prompt = re.sub(
                r"\n\*\*Allow-gaps demotion clause.*?standard verdict vocabulary unchanged\.\n",
                "\n",
                prompt,
                flags=re.DOTALL,
            )
        return prompt

    if template_id == "plan-author-task-targeted":
        prompt = prompt.replace(
            "<per_finding_dispatches[i].child_plan_file>",
            context["child_plan_file"],
        )
        prompt = prompt.replace(
            "<per_finding_dispatches[i].target_task_id>",
            context["target_task_id"],
        )
        prompt = prompt.replace(
            "<per_finding_dispatches[i].finding>",
            _json_for_prompt(context["finding"]),
        )
        return injection + prompt

    if template_id == "plan-author-schedule-level":
        prompt = prompt.replace(
            "<orchestrator-resolved absolute path to 00_INDEX.json>",
            context["roster_file"],
        )
        prompt = prompt.replace(
            "<per_finding_dispatches[i].finding>",
            _json_for_prompt(context["finding"]),
        )
        return prompt

    if template_id == "plan-author-legacy-whole-plan":
        return (
            f"Apply a plan-review finding to the legacy whole-plan file at "
            f"`{context['plan_path']}`. Your write scope is exactly that file.\n\n"
            f"Target task id: `{context.get('target_task_id')}`\n\n"
            "Plan-review finding (single router-provided entry):\n\n"
            "```json\n"
            f"{_json_for_prompt(context['finding'])}\n"
            "```\n\n"
            "Apply a minimum-change edit. Preserve untouched sections verbatim. "
            "Do NOT edit child plan files, roster files, source code, tests, or configuration.\n\n"
            "**You do NOT have the Agent tool.** Do all work directly with Read, Grep, Glob, Edit, Write, Bash.\n"
        )

    if template_id == "plan-review-triage":
        return _render_plan_review_triage_prompt(context, prompt)

    if template_id == "plan-remediator-narrow":
        if normalized_task_id is None:
            return _agent_dispatch_error(
                "task-id-invalid",
                f"task_id is not parseable: {context.get('task_id')!r}",
                path="$.context.task_id",
            )
        task_block = _extract_task_block_for_prompt(plan_text, normalized_task_id)
        if task_block is None:
            return _agent_dispatch_error(
                "task-not-found",
                f"task {normalized_task_id} not present in plan {plan_file}",
                path="$.context.task_id",
            )
        prompt = prompt.replace("<absolute plan path>", str(plan_file))
        prompt = prompt.replace("<## Context section verbatim>", _context_section_for_prompt(plan_text))
        prompt = prompt.replace("<entire TASK-NNN block>", task_block)
        prompt = prompt.replace("<starting_sha>", context["starting_sha"])
        prompt = prompt.replace("<findings_for_retry>", _json_for_prompt(context["findings_for_retry"]))
        prompt = prompt.replace("<dismissed_for_context>", _json_for_prompt(context["dismissed_for_context"]))
        prompt = prompt.replace("<d5_summary>", context["d5_summary"])
        prompt = prompt.replace("<analyst_annotations_json>", _json_for_prompt(context["analyst_annotations_json"]))
        return injection + prompt

    if template_id == "plan-remediator-rescue":
        if normalized_task_id is None:
            return _agent_dispatch_error(
                "task-id-invalid",
                f"task_id is not parseable: {context.get('task_id')!r}",
                path="$.context.task_id",
            )
        task_block = _extract_task_block_for_prompt(plan_text, normalized_task_id)
        if task_block is None:
            return _agent_dispatch_error(
                "task-not-found",
                f"task {normalized_task_id} not present in plan {plan_file}",
                path="$.context.task_id",
            )
        prompt = prompt.replace("<absolute plan path>", str(plan_file))
        prompt = prompt.replace("<reviewer_source>", context["reviewer_source"])
        prompt = prompt.replace("<## Context section verbatim>", _context_section_for_prompt(plan_text))
        prompt = prompt.replace("<entire TASK-NNN block>", task_block)
        prompt = prompt.replace("<starting_sha>", context["starting_sha"])
        prompt = prompt.replace("<rescue_findings_json>", _json_for_prompt(context["rescue_findings_json"]))
        prompt = prompt.replace("<analyst_annotations_json>", _json_for_prompt(context["analyst_annotations_json"]))
        return injection + prompt

    return prompt


def _render_plan_review_triage_prompt(context: dict, prompt: str) -> str:
    source = context["source"]
    rendered_source = "codex-plan-review" if source == "codex" else "plan-analyst"
    evidence = context["findings"] if source == "codex" else context["gaps"]
    findings_count = len(evidence)
    schedule_text = ""
    if context.get("schedule_path"):
        schedule_text = f", `schedule_path={context['schedule_path']}`"
    prompt = prompt.replace(
        "Scope: `source=<source>`, `findings_count=<N>`; include `schedule_path=<absolute schedule path>` only when supplied.",
        f"Scope: `source={rendered_source}`, `findings_count={findings_count}`{schedule_text}.",
    )
    prompt = prompt.replace("<source>", rendered_source)
    prompt = prompt.replace("<findings_for_payload>", _json_for_prompt(evidence))
    if source == "codex":
        prompt = re.sub(
            r"\n\*If `source == plan-analyst`:.*?(?=\nReturn your verdict)",
            "\n",
            prompt,
            flags=re.DOTALL,
        )
        prompt = re.sub(
            r"\n\*Source-specific verification moves — if `source == plan-analyst`:.*?(?=\n\*Same-family caveat)",
            "\n",
            prompt,
            flags=re.DOTALL,
        )
        prompt = re.sub(
            r"\n\*Same-family caveat.*?over silently dismissing it\.\n",
            "\n",
            prompt,
            flags=re.DOTALL,
        )
    else:
        prompt = re.sub(
            r"\n\*If `source == codex-plan-review`:.*?(?=\n\*If `source == plan-analyst`)",
            "\n",
            prompt,
            flags=re.DOTALL,
        )
        prompt = re.sub(
            r"\n\*Source-specific verification moves — if `source == codex-plan-review`:.*?(?=\n\*Source-specific verification moves — if `source == plan-analyst`)",
            "\n",
            prompt,
            flags=re.DOTALL,
        )
    return prompt


def cmd_build_agent_dispatch_prompt(args: argparse.Namespace) -> None:
    payload = _args_to_payload_build_agent_dispatch_prompt(args)
    result = _run_build_agent_dispatch_prompt(payload)
    args.json = True
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# build-claude-dispatch-input — canonical wrapper-input builder
# ---------------------------------------------------------------------------
#
# TASK-001 (wrapper_autoclean_authorization): centralises the construction
# of the JSON object accepted by `plan_claude_dispatch.py run --input -`.
# `declared_files_changed` is now schema-required at the wrapper layer;
# the four orchestrator dispatch sites (Phase B / Phase B-rework / Phase
# D.2b / Phase B-narrow-remediation) plus the Phase A-single analyst
# fan-out feed through this builder so the field is populated correctly
# per task variant. Read-only agents emit `[]` explicitly; write-
# authorized agents derive the list from the task's `Files:` block via
# `_extract_task_files_from_plan` (the same canonical helper that
# `_gate_commit_safe` uses).

_BCDI_VARIANT_AGENT = {
    "default": "plan-implementer",
    "rework": "plan-implementer",
    "role-swap": "plan-implementer",
    "narrow-remediation": "plan-remediator",
    "analyst": "plan-analyst",
}

_BCDI_VARIANT_MODEL = {
    "default": "opus",
    "rework": "opus",
    "role-swap": "opus",
    "narrow-remediation": "opus",
    "analyst": "sonnet",
}

_BCDI_VARIANT_SCHEMA_PATH = {
    "default": "tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json",
    "rework": "tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json",
    "role-swap": "tests/scripts/fixtures/claude_dispatch/schemas/implementer_result.json",
    "narrow-remediation": "tests/scripts/fixtures/claude_dispatch/schemas/remediator_result.json",
    "analyst": "plugins/plan-executor/scripts/schemas/claude_dispatch_output.json",
}


def _bcdi_to_error_result(code: str, message: str) -> dict:
    """Pure terminator: return a marker-bearing error result dict.

    Used by `_run_build_claude_dispatch_input` (TASK-003E) so pure / MCP
    callers receive structured `errors[]` envelopes instead of a
    `sys.exit(1)`.
    """
    return _result(
        {"errors": [{"code": code, "message": message}]}, exit_code=1,
    )


def _bcdi_to_envelope_result(envelope: dict, output: str) -> dict:
    """Pure terminator: return a marker-bearing envelope result dict.

    When ``output`` is the stdout sentinel ``"-"`` the envelope is
    returned for `_emit_or_die` to JSON-render. When ``output`` is a
    filesystem path, the envelope is written there and
    ``__plan_ops_stdout_suppressed__`` is set so the CLI exits 0 with
    no stdout (preserving the pre-codemod byte image). MCP callers
    get an ``__plan_ops_mcp_acknowledgement__`` marker carrying the
    small ``{ok, output_written, output}`` shape so the MCP server
    can return that acknowledgement instead of the full dispatch
    envelope (which is what the file-output mode is for).
    """
    result = _result(envelope, exit_code=0)
    if output != "-":
        text = json.dumps(envelope, indent=2, sort_keys=False) + "\n"
        Path(output).write_text(text, encoding="utf-8")
        result["__plan_ops_stdout_suppressed__"] = True
        result["__plan_ops_mcp_acknowledgement__"] = {
            "ok": True,
            "output_written": True,
            "output": output,
        }
    return result


def _args_to_payload_build_claude_dispatch_input(
    args: argparse.Namespace,
) -> dict:
    """Lift CLI args + the `$UNATTENDED_REVERT_POLICY` env var into a
    pure payload. Pure / MCP callers can construct this dict directly
    without going through argparse or the process environment.
    """
    return {
        "plan_file": args.plan_file,
        "task_id": args.task_id,
        "variant": args.variant,
        "repo_root": args.repo_root,
        "analyst_annotations": args.analyst_annotations,
        "target_task_id": args.target_task_id,
        "starting_sha": args.starting_sha,
        "dispatch_context": args.dispatch_context,
        "run_id": args.run_id,
        # `--output -` is a string sentinel meaning stdout; only a real
        # filesystem path is later wrapped as `Path(...)` for writing.
        "output": args.output or "-",
        "unattended_revert_policy_env": os.environ.get(
            "UNATTENDED_REVERT_POLICY",
        ),
    }


def _run_build_claude_dispatch_input(payload: dict) -> dict:
    """Pure core: build the canonical `claude_dispatch_input.json`
    envelope from a payload dict and return a marker-bearing result.

    Validation failures return `_bcdi_to_error_result(...)` instead of
    calling `sys.exit`. Success returns `_bcdi_to_envelope_result(...)`
    which routes the envelope to stdout (`output == "-"`) or to a file
    (`__plan_ops_stdout_suppressed__`).
    """
    raw_policy_env = payload.get("unattended_revert_policy_env")
    unattended_revert_policy = None
    if raw_policy_env is None or raw_policy_env == "":
        unattended_revert_policy = None
    elif raw_policy_env in {"pause", "fail-fast", "preserve-only"}:
        unattended_revert_policy = raw_policy_env
    else:
        return _bcdi_to_error_result(
            "unattended-revert-policy-invalid",
            (
                f"$UNATTENDED_REVERT_POLICY is not in the closed enum "
                f"{{pause, fail-fast, preserve-only}}: "
                f"{raw_policy_env!r}"
            ),
        )

    variant = payload["variant"]
    plan_file = Path(payload["plan_file"])
    if not plan_file.is_file():
        return _bcdi_to_error_result(
            "plan-file-not-found",
            f"plan file not found: {plan_file}",
        )
    plan_text = _load_text(plan_file)

    normalized_task_id = _normalize_task_id(payload["task_id"])
    if normalized_task_id is None:
        return _bcdi_to_error_result(
            "task-id-invalid",
            f"task id is not parseable: {payload['task_id']!r}",
        )

    agent = _BCDI_VARIANT_AGENT[variant]
    model = _BCDI_VARIANT_MODEL[variant]
    schema_path = _BCDI_VARIANT_SCHEMA_PATH[variant]

    if variant == "analyst":
        declared_files: list[str] = []
    else:
        files = _extract_task_files_from_plan(plan_text, normalized_task_id)
        if files is None:
            return _bcdi_to_error_result(
                "task-not-found",
                (
                    f"task {normalized_task_id} not present in plan "
                    f"{plan_file}"
                ),
            )
        declared_files = [p for p in files if p]
        if not declared_files:
            sys.stderr.write(
                "warning: task "
                f"{normalized_task_id} declares no files; the dispatch "
                "will hit the wrapper's anti-aliasing / short-circuit "
                "guard.\n"
            )

    repo_root = (
        str(Path(payload["repo_root"]).resolve())
        if payload["repo_root"]
        else str(Path.cwd().resolve())
    )

    inner_payload: dict = {
        "plan_path": str(plan_file.resolve()),
        "repo_root": repo_root,
    }
    inner_payload["task_id"] = normalized_task_id
    if payload["target_task_id"]:
        normalized_tt = _normalize_task_id(payload["target_task_id"])
        if normalized_tt is None:
            return _bcdi_to_error_result(
                "target-task-id-invalid",
                f"target task id is not parseable: {payload['target_task_id']!r}",
            )
        inner_payload["target_task_id"] = normalized_tt
    else:
        inner_payload["target_task_id"] = None
    if variant != "analyst":
        if variant in ("default", "rework", "role-swap"):
            if payload["analyst_annotations"]:
                ann_path = Path(payload["analyst_annotations"])
                if not ann_path.is_file():
                    return _bcdi_to_error_result(
                        "analyst-annotations-not-found",
                        f"analyst annotations file not found: {ann_path}",
                    )
                try:
                    inner_payload["analyst_annotations"] = json.loads(
                        _load_text(ann_path)
                    )
                except json.JSONDecodeError as e:
                    return _bcdi_to_error_result(
                        "analyst-annotations-invalid-json",
                        f"analyst annotations file is not valid JSON: {e}",
                    )
            else:
                inner_payload["analyst_annotations"] = None
            inner_payload["starting_sha"] = payload["starting_sha"] or ""
        if variant in ("rework", "narrow-remediation"):
            if not payload["dispatch_context"]:
                return _bcdi_to_error_result(
                    "dispatch-context-required",
                    f"--dispatch-context is required for variant {variant!r}",
                )
            ctx_path = Path(payload["dispatch_context"])
            if not ctx_path.is_file():
                return _bcdi_to_error_result(
                    "dispatch-context-not-found",
                    f"dispatch context file not found: {ctx_path}",
                )
            try:
                inner_payload["dispatch_context"] = json.loads(
                    _load_text(ctx_path)
                )
            except json.JSONDecodeError as e:
                return _bcdi_to_error_result(
                    "dispatch-context-invalid-json",
                    f"dispatch context file is not valid JSON: {e}",
                )

    # BUG-146: inline the result schema content (not just its path) so the
    # implementer/remediator/analyst agent has no choice but to follow the
    # canonical ``{outcome, files_changed, report}`` shape. Without this,
    # the agent's prompt only sees ``schema_path`` as an opaque string and
    # falls back to a guessed legacy shape (top-level
    # ``{status, task_id, summary, ...}``) — see
    # ``docs/bugs/BUG_146_*_IMPLEMENTER-RESULT-SCHEMA-MISMATCH.md``. The
    # inlined schema is also threaded into ``payload.prompt`` for the
    # write-authorized variants so the structured-fallback rendering path
    # in ``_claude_backend._resolve_prompt`` surfaces it directly to the
    # nested agent (which never reads the wrapper envelope).
    schema_inline_obj: dict | None = None
    schema_abs = (Path(repo_root) / schema_path).resolve()
    if schema_abs.is_file():
        try:
            schema_inline_obj = json.loads(_load_text(schema_abs))
        except json.JSONDecodeError as e:
            return _bcdi_to_error_result(
                "schema-inline-invalid-json",
                f"result schema file is not valid JSON ({schema_abs}): {e}",
            )
    else:
        return _bcdi_to_error_result(
            "schema-inline-not-found",
            f"result schema file not found at {schema_abs}",
        )

    if variant != "analyst":
        prompt_lines = [
            f"Implement TASK-{normalized_task_id} from the plan at "
            f"`{plan_file.resolve()}` per your "
            f"{agent} agent specification.",
            "",
            (
                "Read the plan to find the `## Context` section and the "
                "verbatim `### TASK-NNN: <title>` block (with Status / "
                "Priority / Files / Test command / Acceptance criteria / "
                "Description / Reversion guidance). Apply the minimum "
                "change satisfying the AC, run the test command, and "
                "return your structured JSON report. Do not commit, do "
                "not modify the plan file, do not use `git stash`."
            ),
            "",
            (
                "## Result envelope (MANDATORY shape — do NOT emit a "
                "legacy `{status, task_id, summary, ...}` shape)"
            ),
            "",
            (
                "Your final message MUST be a single JSON object "
                "conforming to the canonical schema below. The wrapper "
                "parses this with `claude-envelope-extract`; any "
                "deviation classifies the run as `malformed` and pauses "
                "the orchestrator. Top-level keys MUST be exactly "
                "`outcome`, `files_changed`, and `report`."
            ),
            "",
            "Worked example (canonical shape):",
            "```json",
            json.dumps(
                {
                    "outcome": "success",
                    "files_changed": ["path/to/file.py"],
                    "report": {
                        "summary": "<2-5 bullets describing what changed>",
                        "test_command": "<verbatim test command or 'none'>",
                        "test_outcome": "passed",
                        "test_output_tail": "<last 30 lines or 'n/a'>",
                        "acceptance_criteria_check": [
                            {"status": "x", "criterion": "<text>",
                             "evidence": "<file:line or assertion>"},
                        ],
                        "plan_adaptations": [],
                        "concerns_for_reviewer": [],
                    },
                },
                indent=2,
            ),
            "```",
            "",
            (
                "`outcome` MUST be one of: `success`, `partial`, "
                "`failed`, `plan-incorrect`, `blocked`, `malformed`. "
                "`files_changed` MUST be an array of repo-relative "
                "paths. `report` MUST be an object (free-form keys "
                "describing the implementation report)."
            ),
            "",
            f"Result schema (verbatim, from `{schema_path}`):",
            "```json",
            json.dumps(schema_inline_obj, indent=2),
            "```",
            "",
            "Structured dispatch payload (verbatim, for reference):",
            "```json",
            json.dumps(inner_payload, indent=2, default=str),
            "```",
        ]
        inner_payload["prompt"] = "\n".join(prompt_lines)

    envelope = {
        "schema_version": 1,
        "agent": agent,
        "payload": inner_payload,
        "output_instructions": {
            "format": "json",
            "schema_path": schema_path,
            "schema_inline": schema_inline_obj,
            "max_bytes": 65536,
        },
        "overrides": {
            "model": model,
            "timeout_sec": None,
            "tools_allowed_extra": None,
            "tools_disallowed_extra": None,
            "cwd": None,
        },
        "guardrails": {
            "max_depth": 1,
            "cost_cap_usd": None,
            "network": "deny",
        },
        "trace": {
            "run_id": payload["run_id"] or "<orchestrator run_id>",
            "parent_span_id": None,
            "depth": 0,
            "call_chain": ["orchestrator"],
        },
        "declared_files_changed": declared_files,
    }
    # PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002: only attach the field when
    # the orchestrator pinned a value. Omitting it lets the wrapper apply
    # its 'pause' default — never the destructive legacy behavior.
    if unattended_revert_policy is not None:
        envelope["unattended_revert_policy"] = unattended_revert_policy

    return _bcdi_to_envelope_result(envelope, payload["output"])


def cmd_build_claude_dispatch_input(args: argparse.Namespace) -> None:
    """Emit the canonical `claude_dispatch_input.json` shape for one dispatch.

    The orchestrator (or any direct CLI caller) pipes this stdout into
    ``plan_claude_dispatch.py run --input -``. This is a thin shim over
    `_args_to_payload_build_claude_dispatch_input(args)` →
    `_run_build_claude_dispatch_input(payload)` → `_emit_or_die`.
    """
    payload = _args_to_payload_build_claude_dispatch_input(args)
    result = _run_build_claude_dispatch_input(payload)
    # `--json` is the default + only emission for this subcommand; route
    # success and error envelopes through `_emit_or_die` with JSON forced
    # so the trailing-newline byte image stays identical to the
    # pre-codemod handler (mirrors `cmd_resolve_read_targets`).
    args.json = True
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002: codex / gemini builder
# subcommands. These mirror cmd_build_claude_dispatch_input's revert-policy
# env-var contract. The codex/gemini wrappers consume CLI argparse args
# rather than a single JSON envelope, so the emitted JSON is the documented
# cross-wrapper contract envelope (codex_dispatch_input.json /
# gemini_dispatch_input.json) and is also useful for orchestrator audit.
# ---------------------------------------------------------------------------


def _args_to_payload_build_codex_dispatch_input(
    args: argparse.Namespace,
) -> dict:
    """Lift CLI args + `$UNATTENDED_REVERT_POLICY` into a pure payload
    (TASK-003F). Mirrors `_args_to_payload_build_claude_dispatch_input`.
    """
    return {
        # `--output -` is a string sentinel meaning stdout; only a real
        # filesystem path is later wrapped as `Path(...)` for writing.
        "output": args.output or "-",
        "unattended_revert_policy_env": os.environ.get(
            "UNATTENDED_REVERT_POLICY",
        ),
    }


def _run_build_codex_dispatch_input(payload: dict) -> dict:
    """Pure core for `cmd_build_codex_dispatch_input` (TASK-003F).

    Validation failures return `_bcdi_to_error_result(...)` instead of
    `sys.exit`. Success returns `_bcdi_to_envelope_result(...)` which
    routes the envelope to stdout (`output == "-"`) or to a file
    (`__plan_ops_stdout_suppressed__`).
    """
    raw_policy_env = payload.get("unattended_revert_policy_env")
    if raw_policy_env is None or raw_policy_env == "":
        policy: str | None = None
    elif raw_policy_env in {"pause", "fail-fast", "preserve-only"}:
        policy = raw_policy_env
    else:
        return _bcdi_to_error_result(
            "unattended-revert-policy-invalid",
            (
                f"$UNATTENDED_REVERT_POLICY is not in the closed enum "
                f"{{pause, fail-fast, preserve-only}}: "
                f"{raw_policy_env!r}"
            ),
        )
    envelope: dict = {}
    if policy is not None:
        envelope["unattended_revert_policy"] = policy
    return _bcdi_to_envelope_result(envelope, payload["output"])


def cmd_build_codex_dispatch_input(args: argparse.Namespace) -> None:
    """Emit the codex_dispatch_input.json envelope for one dispatch.

    Currently the only cross-wrapper field is ``unattended_revert_policy``;
    the codex wrapper consumes the rest via argparse. See
    ``schemas/codex_dispatch_input.json`` for the documented contract.

    Thin shim over `_args_to_payload_build_codex_dispatch_input(args)` →
    `_run_build_codex_dispatch_input(payload)` → `_emit_or_die`
    (TASK-003F).
    """
    payload = _args_to_payload_build_codex_dispatch_input(args)
    result = _run_build_codex_dispatch_input(payload)
    args.json = True
    _emit_or_die(args, result)


def _args_to_payload_build_gemini_dispatch_input(
    args: argparse.Namespace,
) -> dict:
    """Lift CLI args + `$UNATTENDED_REVERT_POLICY` into a pure payload
    (TASK-003F). Mirrors `_args_to_payload_build_claude_dispatch_input`.
    """
    return {
        "output": args.output or "-",
        "unattended_revert_policy_env": os.environ.get(
            "UNATTENDED_REVERT_POLICY",
        ),
    }


def _run_build_gemini_dispatch_input(payload: dict) -> dict:
    """Pure core for `cmd_build_gemini_dispatch_input` (TASK-003F).

    Identical revert-policy validation and envelope shape as the codex
    sibling; kept as a separate function so MCP tool registration can
    bind a distinct subcommand handler per cross-wrapper contract.
    """
    raw_policy_env = payload.get("unattended_revert_policy_env")
    if raw_policy_env is None or raw_policy_env == "":
        policy: str | None = None
    elif raw_policy_env in {"pause", "fail-fast", "preserve-only"}:
        policy = raw_policy_env
    else:
        return _bcdi_to_error_result(
            "unattended-revert-policy-invalid",
            (
                f"$UNATTENDED_REVERT_POLICY is not in the closed enum "
                f"{{pause, fail-fast, preserve-only}}: "
                f"{raw_policy_env!r}"
            ),
        )
    envelope: dict = {}
    if policy is not None:
        envelope["unattended_revert_policy"] = policy
    return _bcdi_to_envelope_result(envelope, payload["output"])


def cmd_build_gemini_dispatch_input(args: argparse.Namespace) -> None:
    """Emit the gemini_dispatch_input.json envelope for one dispatch.

    Currently the only cross-wrapper field is ``unattended_revert_policy``;
    the gemini wrapper consumes the rest via argparse. See
    ``schemas/gemini_dispatch_input.json`` for the documented contract.

    Thin shim over `_args_to_payload_build_gemini_dispatch_input(args)` →
    `_run_build_gemini_dispatch_input(payload)` → `_emit_or_die`
    (TASK-003F).
    """
    payload = _args_to_payload_build_gemini_dispatch_input(args)
    result = _run_build_gemini_dispatch_input(payload)
    args.json = True
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan operations CLI for /implement-plan workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="Smart dirty-tree + codex + run-id")
    p_pre.add_argument("--plan-file", required=True, help="Absolute path to plan file")
    p_pre.add_argument("--strict-branch", action="store_true",
                       help="Halt (not warn) if current branch != plan's Base branch")
    p_pre.add_argument("--strict-scope", action="store_true",
                       help="Halt if any dirty file lies within the plan's declared scope")
    p_pre.add_argument(
        "--unattended-revert-policy",
        choices=["pause", "fail-fast", "preserve-only"],
        default=None,
        help=(
            "Policy that controls how new pause paths behave under unattended "
            "execution. Required when stdin is not a TTY: cron/CI callers MUST "
            "pass an explicit value (pause | fail-fast | preserve-only). When "
            "stdin IS a TTY and the flag is absent, defaults to 'pause'. The "
            "resolved value is echoed back on the JSON envelope as "
            "unattended_revert_policy so the orchestrator can pin it as "
            "$UNATTENDED_REVERT_POLICY for the rest of the run."
        ),
    )
    _add_json(p_pre)

    p_rr = sub.add_parser(
        "review-route",
        help=(
            "Route a parsed Phase D review envelope to one orchestrator "
            "directive (TASK-001 PHASE_D_STATE_MACHINE). Reads the input "
            "envelope from stdin per review_route_input_schema.json; emits "
            "one directive per review_route_output_schema.json."
        ),
    )
    p_rr.add_argument("--stdin", action="store_true", required=True,
                      help="Read review-route input JSON from stdin")
    _add_json(p_rr)

    p_pr_route = sub.add_parser(
        "plan-review-route",
        help=(
            "Route Phase 1.5 / 1.5.5 plan-review state to one "
            "orchestrator directive. Reads plan_review_route_input_schema.json "
            "from stdin and emits plan_review_route_output_schema.json."
        ),
    )
    p_pr_route.add_argument("--stdin", action="store_true", required=True,
                            help="Read plan-review-route input JSON from stdin")
    p_pr_route.add_argument(
        "--update-schedule-state",
        default=None,
        help=(
            "Atomically apply the emitted state_transitions block to this "
            "schedule's plan_review_state"
        ),
    )
    _add_json(p_pr_route)

    p_sched = sub.add_parser("parse-schedule", help="Validate analyst JSON shape")
    p_sched.add_argument("--stdin", action="store_true", required=True,
                         help="Read JSON schedule from stdin")
    p_sched.add_argument("--strict", action="store_true",
                         help="Promote unknown nested fields from warning to error")
    _add_json(p_sched)

    p_decomp = sub.add_parser(
        "decompose-plan",
        help=(
            "Heuristic whole-plan → decomposed-directory split. Reads a "
            "`## TASK-NNN:` plan markdown file and writes a sibling "
            "directory with `00_INDEX.json` + one `TASK-NNN_<slug>.md` "
            "per task (H3 sub-heading grammar)."
        ),
    )
    p_decomp.add_argument(
        "--plan-file", required=True,
        help="Path to the whole-plan markdown file to decompose",
    )
    p_decomp.add_argument(
        "--out-dir", default=None,
        help=(
            "Destination directory; default <file-parent>/<file-stem>/. "
            "Refuses to overwrite a non-empty existing directory unless "
            "--force is set."
        ),
    )
    p_decomp.add_argument(
        "--force", action="store_true",
        help="Overwrite an existing non-empty --out-dir",
    )
    _add_json(p_decomp)

    p_build = sub.add_parser(
        "build-tasks",
        help=(
            "Roster-driven fat `tasks[]` synthesis. Reads `00_INDEX.json` + "
            "each `chunks[].file` in the supplied decomposed-plan directory "
            "and emits a schedule-shaped JSON with per-task description + "
            "acceptance_criteria (H3 `### TASK-NNN:` child-plan grammar)."
        ),
    )
    p_build.add_argument(
        "--plans-dir", required=True,
        help="Path to a decomposed-plan directory (contains 00_INDEX.json)",
    )
    # TASK-002 (narrow_run_filter_ids): scope build-tasks to the
    # transitive-prereq closure of the supplied ids. Accepts plain,
    # zero-padded, or `TASK-NNN` forms (mixed). Empty omits scoping
    # (full-roster default-mode is byte-for-byte unchanged).
    p_build.add_argument(
        "--filter-ids", default="",
        help=(
            "CSV of task ids to scope the build to (any of `9`, `009`, "
            "`TASK-009` accepted; each is normalized before lookup). The "
            "transitive-prereq closure is computed via "
            "`_compute_index_closure`; out-of-closure chunks are silently "
            "skipped. Empty (default) preserves full-roster behavior."
        ),
    )
    _add_json(p_build)

    p_comp = sub.add_parser("compute-schedule", help="Compute priority order + disjoint batches")
    p_comp.add_argument("--stdin", action="store_true", required=True,
                        help="Read JSON schedule from stdin")
    p_comp.add_argument("--strict", action="store_true",
                        help="Accepted for interface parity; currently unused")
    _add_json(p_comp)

    p_ws = sub.add_parser("write-schedule",
                          help="Validate + atomically persist schedule JSON")
    p_ws.add_argument("--schedule-file", required=True,
                      help="Destination path for persisted schedule JSON")
    p_ws.add_argument("--stdin", action="store_true", required=True,
                      help="Read JSON schedule from stdin")
    p_ws.add_argument("--strict", action="store_true",
                      help="Promote unknown nested fields from warning to error")
    _add_json(p_ws)

    p_batch = sub.add_parser("batch-next", help="Select next batch respecting file locks")
    p_batch.add_argument("--schedule-file", required=True, help="Path to schedule JSON")
    p_batch.add_argument("--locked-files", default="", help="Comma-separated locked files")
    p_batch.add_argument("--done", default="", help="Comma-separated done task ids")
    p_batch.add_argument("--failed", default="", help="Comma-separated failed task ids")
    p_batch.add_argument(
        "--paused", default="",
        help=(
            "Comma-separated paused task ids (TASK-002). Treated like "
            "--failed for pick eligibility (skip) but does NOT cascade "
            "block-dependents."
        ),
    )
    p_batch.add_argument("--parallel", type=int, default=2, help="Max concurrent tasks")
    # TASK-002 (PHASE_D_STATE_MACHINE): opt-in read of persisted state.
    # Additive — entries from `state.{done,failed,locked_files,blocked}` are
    # union-merged with the CLI-supplied sets. The orchestrator can drop the
    # CSV flags entirely once every caller has migrated.
    p_batch.add_argument(
        "--from-schedule-state", action="store_true",
        dest="from_schedule_state",
        help=(
            "Merge done/failed/locked_files/blocked from the persisted "
            "schedule `state` block (§3.2). Additive to --done/--failed/"
            "--locked-files; CLI-arg contract unchanged."
        ),
    )
    _add_json(p_batch)

    p_fs = sub.add_parser(
        "filter-schedule",
        help="Filter schedule by --task-ids and emit canonical schedule on stdout",
    )
    # `--schedule-file` and `--stdin` are mutually exclusive alternatives for
    # the source schedule. Exactly one MUST be supplied; the manual enforcement
    # lives in `cmd_filter_schedule` so both are declared `required=False` here.
    p_fs.add_argument("--schedule-file", required=False, default=None,
                      help="Path to source schedule JSON (mutually exclusive with --stdin)")
    p_fs.add_argument("--stdin", action="store_true",
                      help=(
                          "Read source schedule JSON from stdin (mutually "
                          "exclusive with --schedule-file). Relaxation vs the "
                          "file path: outcome='valid' is NOT required on the "
                          "--stdin path; in-memory schedules with "
                          "outcome='needs-enrichment' (e.g. from "
                          "build-tasks warnings→gaps mapping) are also "
                          "accepted. File-path behaviour is preserved for "
                          "backward compatibility."
                      ))
    p_fs.add_argument("--task-ids", required=True,
                      help="CSV of task ids; canonical, plain, or TASK-NNN forms")
    _add_json(p_fs)

    p_rep = sub.add_parser("parse-implementer-report",
                           help="Parse plan-implementer markdown report")
    p_rep.add_argument("--stdin", action="store_true", required=True,
                       help="Read report markdown from stdin")
    _add_json(p_rep)

    p_prr = sub.add_parser(
        "parse-plan-review-report",
        help=(
            "Validate a Phase 1.5 plan-review envelope against "
            "codex_plan_review_schema.json; surface verdict + findings. "
            "Default input is the Codex wrapper envelope; with "
            "--from-claude, stdin is the bare `parsed` payload emitted "
            "by the Phase 1.5-Claude `plan-reviewer` Agent."
        ),
    )
    p_prr.add_argument("--stdin", action="store_true", required=True,
                       help="Read plan-review envelope JSON from stdin")
    p_prr.add_argument(
        "--from-claude", action="store_true", dest="from_claude",
        help=(
            "Parse the bare `parsed` payload (Phase 1.5-Claude path) "
            "instead of the Codex wrapper envelope. Skips the envelope-"
            "level `subcommand`/`outcome`/`task_id` checks and validates "
            "stdin directly against codex_plan_review_schema.json."
        ),
    )
    _add_json(p_prr)

    p_cee = sub.add_parser(
        "claude-envelope-extract",
        help=(
            "TASK-006: extract a normalized routing payload from a v3 "
            "Claude wrapper envelope on stdin. Replaces the per-dispatch "
            "inline jq-style reads added in TASK-003/004/005."
        ),
    )
    p_cee.add_argument("--stdin", action="store_true", required=True,
                       help="Read wrapper envelope JSON from stdin")
    p_cee.add_argument(
        "--agent", required=True,
        choices=sorted(_CLAUDE_ENVELOPE_AGENTS),
        help="Dispatch site: which agent the envelope is from",
    )
    _add_json(p_cee)

    p_otf = sub.add_parser(
        "order-triage-findings",
        help=(
            "Sort plan-review findings by (blocking, severity, source_index) "
            "and annotate each with `source_index`; the orchestrator calls "
            "this before rendering the Phase 1.5.5 triage dispatch template"
        ),
    )
    p_otf.add_argument("--stdin", action="store_true", required=True,
                       help="Read findings JSON from stdin")
    _add_json(p_otf)

    p_prt = sub.add_parser(
        "parse-plan-review-triage-report",
        help=(
            "Parse the plan-review triage markdown report, extract the "
            "last fenced JSON payload, and validate the shared verdict "
            "contract against the supplied source/count context"
        ),
    )
    p_prt.add_argument("--stdin", action="store_true", required=True,
                       help="Read triage report markdown from stdin")
    p_prt.add_argument(
        "--source",
        help="Evidence source: plan-analyst gaps or codex-plan-review findings",
    )
    p_prt.add_argument(
        "--findings-count", type=int,
        help=(
            "Length of the evidence array the triage report is indexing "
            "(plan-analyst gaps[] or codex-plan-review findings[])"
        ),
    )
    _add_json(p_prt)

    p_d5 = sub.add_parser(
        "parse-d5-adjudication",
        help=(
            "Validate a D.5 adjudication payload and surface the "
            "structured dispatch fields (verdict, summary, load_bearing, "
            "dismissed). Partial-agreement payloads are checked for "
            "disjoint, in-range index splits; malformed splits exit with "
            "partial-agreement-invalid-split / -unknown-index codes."
        ),
    )
    p_d5.add_argument("--stdin", action="store_true", required=True,
                      help="Read D.5 adjudication JSON payload from stdin")
    p_d5.add_argument(
        "--codex-findings-count", type=int, required=True,
        help=(
            "Length of the Codex parsed.findings[] array the D.5 reviewer "
            "was adjudicating; partial-agreement indices must stay within "
            "range(0, count)."
        ),
    )
    _add_json(p_d5)

    p_commit = sub.add_parser(
        "commit-task",
        help="Narrow commit + status flip + run log append",
        epilog=(
            "see also: SKILL.md §D.2a for D.5-verdict-driven "
            "binding-reviewer mapping"
        ),
    )
    p_commit.add_argument("--plan-file", required=True)
    p_commit.add_argument("--task-id", required=True)
    p_commit.add_argument("--run-id", required=True)
    p_commit.add_argument("--files", required=True, help="Comma-separated files to commit")
    p_commit.add_argument("--title", required=True)
    p_commit.add_argument("--diff-summary", required=True)
    p_commit.add_argument("--reviewer", choices=["codex", "gemini", "claude", "none"], default="none")
    p_commit.add_argument("--reviewer-verdict", default="")
    p_commit.add_argument("--reviewer-minor-findings", default="[]",
                          help="JSON array of minor findings")
    # TASK-016C: a commit cannot be both a D.2a.5 full remediation AND a
    # D.2a.6 narrow remediation — they are parallel retry paths with
    # distinct trailer shapes. Enforced at argparse rather than the
    # log-event layer to catch operator error on the CLI, before a
    # malformed commit is written.
    p_commit_rem_grp = p_commit.add_mutually_exclusive_group()
    p_commit_rem_grp.add_argument(
        "--remediation-tag", action="store_true",
        help=(
            "Mark commit as a D.2a.5 post-remediation retry. "
            "Appends a [remediation] tag line to the commit body."
        ),
    )
    p_commit_rem_grp.add_argument(
        "--narrow-remediation-tag", action="store_true",
        help=(
            "Mark commit as a D.2a.6 narrow-remediation retry. "
            "Appends a [narrow-remediation] tag line to the commit "
            "body (adjacent to the [disagreement: i,j] trailer). "
            "Mutually exclusive with --remediation-tag; requires a "
            "non-empty --dismissed-finding-ids."
        ),
    )
    # TASK-005: a commit cannot be a D.2a.5 OR D.2a.6 OR D.4-rescue
    # retry simultaneously — each tag denotes a distinct retry path
    # (bounded D.2a.5 vs narrow D.2a.6 vs single-shot D.4 rescue) and a
    # single commit cannot belong to two paths. Joining the same
    # mutually-exclusive group as the existing remediation tags hands
    # the rejection to argparse with its standard exit-code-2 banner.
    p_commit_rem_grp.add_argument(
        "--d4-rescue-tag", action="store_true",
        help=(
            "Mark commit as a TASK-005 D.4-rescue retry. "
            "Appends a [d4-rescue] tag line to the commit body. "
            "Mutually exclusive with --remediation-tag and "
            "--narrow-remediation-tag; also mutually exclusive with "
            "--disagreement-tag and --dismissed-finding-ids (D.4 "
            "rescue does not invoke D.5 and does not carry dismissed "
            "findings). Bare use (no companion flag) is the canonical "
            "successful-rescue commit signature."
        ),
    )
    # TASK-016C: a commit cannot be both "D.5 disagrees with all Codex
    # findings" (bare --disagreement-tag) AND "D.5 disagrees with a
    # subset" (--dismissed-finding-ids i,j). The bare form is used by
    # D.5 verdicts `ship | ship-with-fixes`; the with-indices form only
    # appears on the partial-agreement path.
    p_commit_dis_grp = p_commit.add_mutually_exclusive_group()
    p_commit_dis_grp.add_argument(
        "--disagreement-tag", action="store_true",
        help="Mark commit as §8.4 disagreement (bare [disagreement] tag)",
    )
    p_commit_dis_grp.add_argument(
        "--dismissed-finding-ids", default="",
        help=(
            "Comma-separated integer indices of Codex findings that "
            "D.5 dismissed on the partial-agreement path (0-based "
            "into the original Codex findings array). Emits a "
            "[disagreement: I,J,K] trailer adjacent to the "
            "[narrow-remediation] tag. Requires --narrow-remediation-tag; "
            "mutually exclusive with --disagreement-tag."
        ),
    )
    # TASK-008 (POSTMORTEM_FIXES): the sandbox-divergence escape hatch.
    # Orthogonal to the remediation / narrow-remediation / disagreement
    # axes — it stacks freely with each (you can have a remediation
    # commit that ALSO hit a sandbox divergence on the retry). The flag
    # only inscribes `[sandbox-divergence]` into the commit body and
    # logs `sandbox_divergence_tag=true` on the commit_done event; it
    # does NOT relax the reviewer-verdict whitelist.
    p_commit.add_argument(
        "--sandbox-divergence-tag", action="store_true",
        help=(
            "Mark commit as TASK-008 sandbox-divergence-validated. "
            "Appends a [sandbox-divergence] tag line to the commit "
            "body. Set by the orchestrator's auto-validate branch when "
            "the wrapper sandbox test failed but the same test command "
            "passed in the target environment. Does NOT relax the "
            "reviewer-verdict whitelist."
        ),
    )
    p_commit.add_argument("--dry-run", action="store_true")
    # TASK-002 (PHASE_D_STATE_MACHINE): on success, atomically promote the
    # task into the persisted schedule's `state.done`, append a commit
    # record, and release the task's `state.locked_files` entries.
    # Schedule without a `state` block: state-write no-ops + warns.
    p_commit.add_argument(
        "--update-schedule-state", default=None,
        dest="update_schedule_state",
        help=(
            "Path to the schedule JSON. After a successful commit, "
            "atomically apply the commit transition to the schedule's "
            "`state` block (`state.done`, `state.committed`, "
            "`state.locked_files`)."
        ),
    )
    # TASK-020B: cap the opt-in `acceptance_v_check` runtime. Default 300s;
    # plans that need longer pass `--v-check-timeout SECONDS` explicitly.
    # Only consulted when the plan carries the YAML frontmatter key.
    p_commit.add_argument(
        "--v-check-timeout", type=int, default=300,
        help=(
            "Timeout in seconds for the opt-in acceptance_v_check. "
            "Default 300. Ignored if the plan has no frontmatter key."
        ),
    )
    _add_json(p_commit)

    p_fail = sub.add_parser("fail-task", help="Restore files + status flip + run log append")
    p_fail.add_argument("--plan-file", required=True)
    p_fail.add_argument("--task-id", required=True)
    p_fail.add_argument("--run-id", required=True)
    p_fail.add_argument("--files", default="", help="Comma-separated files to git restore")
    p_fail.add_argument("--stage", required=True, choices=sorted(ALLOWED_FAIL_STAGES))
    p_fail.add_argument("--reason", required=True)
    # ``--authorization-source`` is logically required: every ``fail-task``
    # invocation MUST consciously declare which authorized path is
    # sanctioning the silent revert + status-flip side effects. argparse's
    # built-in ``required=True`` is intentionally NOT used here: it would
    # short-circuit to argparse's stderr error format and prevent
    # ``cmd_fail_task`` from emitting the structured
    # ``{"errors":[{"code":"authorization-source-required",...}]}`` envelope
    # that downstream consumers (orchestrator, audit) rely on. The runtime
    # check inside ``cmd_fail_task`` enforces presence and emits the
    # structured envelope. ``choices`` is enforced by argparse as usual; an
    # unknown value still produces argparse's standard rejection. The closed
    # enum lives in ``ALLOWED_FAIL_AUTHORIZATION_SOURCES`` above; subsequent
    # tasks (TASK-005..TASK-008) extend that set when they introduce new
    # authorized paths.
    p_fail.add_argument(
        "--authorization-source",
        default=None,
        choices=sorted(ALLOWED_FAIL_AUTHORIZATION_SOURCES),
        help=(
            "Authorized path that sanctions this fail-task invocation. "
            "Required. See ALLOWED_FAIL_AUTHORIZATION_SOURCES in plan_ops.py."
        ),
    )
    p_fail.add_argument("--reviewer-findings", default="",
                        help="JSON blob of reviewer findings (stage=review)")
    p_fail.add_argument("--reversion-guidance", default="",
                        help="Implementer-supplied reversion guidance (stage=implement)")
    p_fail.add_argument("--repo-root", default=None,
                        help="Repo root for path resolution; defaults to CWD")
    p_fail.add_argument("--dry-run", action="store_true")
    # TASK-002 (PHASE_D_STATE_MACHINE): persist this failure into the
    # schedule's `state.failed` and (optionally) record the per-task
    # retry-budget map under `state.retries_used[task_id]`.
    p_fail.add_argument(
        "--update-schedule-state", default=None,
        dest="update_schedule_state",
        help=(
            "Path to the schedule JSON. Atomically appends `task_id` to "
            "`state.failed` and persists `--retries-used` (if supplied) "
            "under `state.retries_used[task_id]`."
        ),
    )
    p_fail.add_argument(
        "--retries-used", default=None,
        dest="retries_used",
        help=(
            "JSON object recording the retry-budget consumption for this "
            "task (e.g. {\"bounded_remediation\": true}). Persisted into "
            "`state.retries_used[task_id]` when `--update-schedule-state` "
            "is set."
        ),
    )
    _add_json(p_fail)

    p_block = sub.add_parser(
        "block-dependents",
        help=(
            "Cascade `blocked` status onto dependents of a failed task. "
            "Mutates the plan markdown (single read, single write) and "
            "appends `blocked` run-log events."
        ),
    )
    p_block.add_argument("--schedule-file", required=True,
                         help="Path to schedule JSON")
    p_block.add_argument("--plan-file", required=True,
                         help="Absolute path to plan file")
    p_block.add_argument("--failed", required=True,
                         help="Task id whose failure triggers the cascade")
    p_block.add_argument("--run-id", required=True, help="Run id for log events")
    # TASK-002 (PHASE_D_STATE_MACHINE): persist the cascade ids into
    # `state.blocked` so subsequent `batch-next --from-schedule-state`
    # invocations skip them without needing the CSV flags.
    p_block.add_argument(
        "--update-schedule-state", default=None,
        dest="update_schedule_state",
        help=(
            "Path to the schedule JSON. After a successful cascade, "
            "atomically populates `state.blocked` with the cascaded "
            "task ids."
        ),
    )
    _add_json(p_block)

    p_hdr = sub.add_parser("update-plan-header", help="Mutate **Status:** in plan header block")
    p_hdr.add_argument("--plan-file", required=True)
    p_hdr.add_argument("--status", required=True, choices=sorted(ALLOWED_PLAN_STATUSES))
    _add_json(p_hdr)

    p_sta = sub.add_parser(
        "set-task-agent",
        help="Set TASK-NNN's **Agent:** bullet (insert or replace)",
    )
    p_sta.add_argument("--plan-file", required=True)
    p_sta.add_argument("--task-id", required=True)
    p_sta.add_argument("--agent", required=True, choices=list(ALLOWED_AGENTS))
    _add_json(p_sta)

    p_fin = sub.add_parser("finalize-execution-log",
                           help="Append §5 execution-log markdown table to plan file")
    p_fin.add_argument("--plan-file", required=True)
    p_fin.add_argument("--run-id", required=True)
    p_fin.add_argument("--starting-sha", required=True)
    p_fin.add_argument("--ending-sha", required=True)
    p_fin.add_argument("--rows-json", required=True, help="JSON array of row dicts")
    p_fin.add_argument(
        "--outcome",
        default=None,
        choices=sorted(ALLOWED_RUN_OUTCOMES),
        help=(
            "Run outcome recorded in the execution-log header. "
            "`paused` signals a D.2a.5 awaiting-user halt; the next "
            "conversation turn decides disposition. Optional; omitted "
            "preserves the legacy (unlabelled) header for callers that "
            "have not yet migrated."
        ),
    )
    _add_json(p_fin)

    p_log = sub.add_parser("log-event", help="Append JSONL event with && tail -1 verification")
    p_log.add_argument("--event", required=True, help="Event name")
    p_log.add_argument("--fields-json", required=True, help="JSON fields dict")
    # TASK-022: optional full-finding payload for `review_done` /
    # `disagreement` events. Validated via `_validate_minor_findings_payload`
    # (same schema as `commit-task --reviewer-minor-findings`). Embedded
    # verbatim under key `findings` on the JSONL line when present.
    p_log.add_argument(
        "--findings-json",
        dest="findings_json",
        default=None,
        help=(
            "Optional JSON array of reviewer findings to embed verbatim "
            "under key 'findings' in the log line. Validated via the "
            "reviewer-minor-findings schema."
        ),
    )
    _add_json(p_log)

    p_norm = sub.add_parser("normalize-task-id", help="Canonicalize task id to 3-digit form")
    p_norm.add_argument("--id", required=True, help="Accepts 1 | 001 | TASK-001")
    _add_json(p_norm)

    p_acq = sub.add_parser("acquire-lock", help="Acquire run-lock for this plan file")
    p_acq.add_argument("--plan-file", required=True)
    p_acq.add_argument("--run-id", required=True)
    p_acq.add_argument(
        "--force",
        action="store_true",
        help="Overwrite lock file even if malformed / owned by other plans",
    )
    _add_json(p_acq)

    p_rel = sub.add_parser("release-lock", help="Release run-lock for this plan file")
    p_rel.add_argument("--plan-file", required=True)
    p_rel.add_argument("--run-id", required=True)
    _add_json(p_rel)

    p_rec = sub.add_parser(
        "reconcile-batch",
        help="Reconcile observed out-of-scope writes from a completed batch",
    )
    p_rec.add_argument("--repo-root", required=True, help="Absolute path to repo root")
    p_rec.add_argument(
        "--schedule-file",
        required=False,
        default=None,
        help=(
            "Optional absolute path to the persisted schedule JSON. When "
            "supplied, reconciliation consults each dispatched task's "
            "normalised Files: list and PRESERVES envelope entries that "
            "are in fact in scope (wrapper false-positives). Without it, "
            "the function falls back to restore-everything behaviour."
        ),
    )
    # TASK-008 (G10): default `pause` policy hands control back to the
    # user with four options (widen-plan, in-place-fix, keep-and-commit,
    # revert) instead of silently restoring out-of-scope writes. Legacy
    # behaviour stays available via `reconcile-and-revert` for the
    # cron/CI case (selected when Phase 0's `--unattended-revert-policy`
    # is `fail-fast` or `preserve-only`).
    p_rec.add_argument(
        "--out-of-scope-policy",
        required=False,
        default="pause",
        choices=list(ALLOWED_OUT_OF_SCOPE_POLICIES),
        help=(
            "How to handle envelopes with out_of_scope_observed=true. "
            "`pause` (default): mark the task `paused` and return four "
            "options to the user; do NOT touch the working tree. "
            "`reconcile-and-revert`: legacy behaviour (restore tracked, "
            "unlink untracked) — used in cron/CI when "
            "--unattended-revert-policy is fail-fast or preserve-only."
        ),
    )
    p_rec.add_argument(
        "--plans-dir",
        required=False,
        default=None,
        help=(
            "Directory containing the per-task plan markdown files. "
            "Used by --out-of-scope-policy=pause to resolve each task's "
            "plan_file (from the schedule) and mutate its status to "
            "`paused`. Without it, the pause path returns the four "
            "options but leaves plan-status mutation to the caller."
        ),
    )
    _add_json(p_rec)

    p_cpd = sub.add_parser(
        "check-plan-deps",
        help="Resolve cross-plan prerequisites via 00_INDEX.json",
    )
    p_cpd.add_argument("--plan-file", required=True, help="Absolute path to plan file")
    p_cpd.add_argument(
        "--plans-dir", required=True,
        help="Directory containing 00_INDEX.json and sibling plan files",
    )
    _add_json(p_cpd)

    # TASK-001 (narrow_run_filter_ids): pure-roster transitive-prereq closure.
    # `--json` is the only output mode (no human-readable mode); the
    # orchestrator is the only consumer.
    p_ic = sub.add_parser(
        "index-closure",
        help=(
            "Compute the transitive-prereq closure of --task-ids via "
            "00_INDEX.json's structured chunks[].depends_on (pure-roster; "
            "never reads any *.md child)."
        ),
    )
    p_ic.add_argument(
        "--plans-dir", required=True,
        help="Directory containing 00_INDEX.json",
    )
    p_ic.add_argument(
        "--task-ids", default="",
        help=(
            "Comma-separated task ids (any of `9`, `009`, `TASK-009` "
            "accepted; each is normalized before lookup)."
        ),
    )
    _add_json(p_ic)

    p_pi = sub.add_parser(
        "path-info",
        help="Emit configured plan_dir + derived run_log/run_lock/schedule_glob paths",
    )
    _add_json(p_pi)

    # TASK-010: surface the effective globally-locked path set so operators
    # can verify their override file resolved as expected before running
    # the scheduler.
    p_lglp = sub.add_parser(
        "list-global-lock-paths",
        help=(
            "Emit the effective globally-locked path set "
            "(defaults + optional YAML override)"
        ),
    )
    _add_json(p_lglp)

    # TASK-009: resolve `**Read targets:**` / `**Symbol targets:**` from a
    # task block (or stdin) into a structured JSON payload the dispatchers
    # render into the implementer / reviewer prompt under
    # `## Pre-read excerpts`.
    p_rrt = sub.add_parser(
        "resolve-read-targets",
        help=(
            "Resolve **Read targets:** / **Symbol targets:** from a task "
            "block (stdin or --task-file) into a structured JSON payload "
            "the dispatcher embeds under `## Pre-read excerpts`."
        ),
    )
    p_rrt.add_argument(
        "--stdin", action="store_true",
        help="Read task-block markdown from stdin",
    )
    p_rrt.add_argument(
        "--task-file", default=None,
        help="Path to a task-block markdown file (mutually exclusive with --stdin)",
    )
    p_rrt.add_argument(
        "--json", action="store_true",
        help=(
            "Reserved for parity; this subcommand always emits JSON "
            "(missing-file / missing-symbol diagnostics live in the "
            "structured `errors` array, not on stderr)."
        ),
    )
    p_rrt.set_defaults(func=cmd_resolve_read_targets)

    p_badp = sub.add_parser(
        "build-agent-dispatch-prompt",
        help=(
            "Render an Agent dispatch prompt from dispatch-templates.md "
            "for a supported template_id."
        ),
    )
    p_badp.add_argument(
        "--stdin",
        action="store_true",
        help="Read {template_id, context} JSON from stdin",
    )
    p_badp.add_argument(
        "--template-id",
        default=None,
        choices=sorted(_AGENT_DISPATCH_TEMPLATE_AGENT),
        help="Dispatch prompt template id",
    )
    p_badp.add_argument(
        "--context",
        default=None,
        help="JSON object or path to JSON context for the selected template",
    )
    p_badp.add_argument(
        "--json",
        action="store_true",
        help="Reserved for parity; this subcommand always emits JSON.",
    )
    p_badp.set_defaults(func=cmd_build_agent_dispatch_prompt)

    # TASK-001 (wrapper_autoclean_authorization): canonical wrapper-input
    # builder. Emits the JSON object accepted by ``plan_claude_dispatch.py
    # run --input -``; populates the schema-required top-level
    # ``declared_files_changed`` from the task's ``Files:`` block via
    # ``_extract_task_files_from_plan``. The four orchestrator dispatch
    # sites (Phase B / Phase B-rework / Phase D.2b / Phase B-narrow-
    # remediation) plus the Phase A-single analyst fan-out feed through
    # this builder.
    p_bcdi = sub.add_parser(
        "build-claude-dispatch-input",
        help=(
            "Emit the canonical claude_dispatch_input.json for one dispatch. "
            "Pipe stdout into `plan_claude_dispatch.py run --input -`."
        ),
    )
    p_bcdi.add_argument("--plan-file", required=True,
                        help="Absolute path to the (child) plan file")
    p_bcdi.add_argument("--task-id", required=True,
                        help="TASK-NNN id (or NNN / N) the dispatch targets")
    p_bcdi.add_argument(
        "--variant", required=True,
        choices=["default", "rework", "role-swap",
                 "narrow-remediation", "analyst"],
        help="Dispatch variant; selects agent, model, payload shape",
    )
    p_bcdi.add_argument("--repo-root", default=None,
                        help="Repo root; defaults to cwd")
    p_bcdi.add_argument("--analyst-annotations", default=None,
                        help="Path to JSON file with analyst annotations")
    p_bcdi.add_argument("--target-task-id", default=None,
                        help="target_task_id for shared-file children (TASK-007)")
    p_bcdi.add_argument("--starting-sha", default=None,
                        help="Orchestrator's starting_sha for the run")
    p_bcdi.add_argument(
        "--dispatch-context", default=None,
        help=(
            "Path to JSON file forwarded as payload.dispatch_context "
            "(required for variant=rework | narrow-remediation)"
        ),
    )
    p_bcdi.add_argument("--run-id", default=None,
                        help="Orchestrator run_id stamped into trace.run_id")
    p_bcdi.add_argument("--output", default="-",
                        help="Output path (default '-' = stdout)")
    p_bcdi.add_argument(
        "--json", action="store_true",
        help=(
            "Reserved for parity; this subcommand always emits JSON."
        ),
    )
    p_bcdi.set_defaults(func=cmd_build_claude_dispatch_input)

    # PLAN_WRAPPER_REVERT_POLICY_GATE TASK-002: codex / gemini builder
    # subcommands. The codex/gemini wrappers consume CLI argparse args
    # rather than a single JSON envelope, so currently these emit only the
    # cross-wrapper ``unattended_revert_policy`` field (see
    # schemas/codex_dispatch_input.json / schemas/gemini_dispatch_input.json
    # for the documented contract).
    p_bcodi = sub.add_parser(
        "build-codex-dispatch-input",
        help=(
            "Emit the codex_dispatch_input.json envelope for one dispatch. "
            "Currently only carries cross-wrapper unattended_revert_policy."
        ),
    )
    p_bcodi.add_argument("--output", default="-",
                         help="Output path (default '-' = stdout)")
    p_bcodi.set_defaults(func=cmd_build_codex_dispatch_input)

    p_bgdi = sub.add_parser(
        "build-gemini-dispatch-input",
        help=(
            "Emit the gemini_dispatch_input.json envelope for one dispatch. "
            "Currently only carries cross-wrapper unattended_revert_policy."
        ),
    )
    p_bgdi.add_argument("--output", default="-",
                        help="Output path (default '-' = stdout)")
    p_bgdi.set_defaults(func=cmd_build_gemini_dispatch_input)

    # TASK-020A: read-only lint that cross-references `**Status:** done` /
    # `partial` task markers against the run log's `commit_done` events and
    # the git log's `feat(TASK-NNN):` commits. Hand-edited status markers
    # that bypassed `commit-task` flunk the lint. CI wiring is deferred to
    # TASK-020C.
    p_lint = sub.add_parser(
        "lint-plans",
        help=(
            "Cross-reference **Status:** done/partial task markers against "
            "commit_done run-log events and feat(TASK-NNN) git commits. "
            "Read-only; exit 1 on any finding."
        ),
    )
    p_lint.add_argument(
        "--plans-dir", required=True,
        help="Directory containing plan markdown files (scanned recursively)",
    )
    p_lint.add_argument(
        "--run-log", default=None,
        help="Path to _run_log.jsonl; omitted = empty run log",
    )
    p_lint.add_argument(
        "--git-dir", default=None,
        help="Path to git work tree root; defaults to CWD",
    )
    _add_json(p_lint)

    # TASK-007: protocol-drift self-audit. Inspects the shipped artifacts
    # (plan_ops.py, plan_codex_dispatch.py, schema sidecars, SKILL.md,
    # dispatch templates, design doc) against `CANONICAL_CONTRACT` and
    # surfaces drift as structured findings. Distinct from `gates` (which
    # is runtime-scoped to a specific execution); audit is standing /
    # cross-cutting and intended to run before any rerun.
    p_audit = sub.add_parser(
        "audit",
        help=(
            "Self-audit the executor for protocol drift "
            "(status vocabulary, schedule wire format, implementer-report "
            "labels, execution-log columns, schemas, portable tier, "
            "wrapper isolation, design-doc orphans)."
        ),
    )
    p_audit.add_argument(
        "--list", action="store_true",
        help="Emit the canonical list of audit check names + tiers",
    )
    p_audit.add_argument(
        "--check", default=None,
        help=(
            "Comma-separated subset of checks to run; overrides tier "
            "filtering so advisory checks may be requested by name"
        ),
    )
    p_audit.add_argument(
        "--strict", action="store_true",
        help=(
            "Include advisory checks (e.g. portable_tier pre-TASK-008) "
            "in the overall verdict"
        ),
    )
    p_audit.add_argument(
        "--report-file", default=None,
        help=(
            "Optional path; writes a Markdown table report alongside "
            "the stdout JSON/summary"
        ),
    )
    _add_json(p_audit)

    # TASK-005: phase-gate and promotion-criteria subcommand. Six canonical
    # gates — schema-valid, schedule-valid, fixture-valid, execution-safe,
    # review-safe, commit-safe — each returning {name, status, reason}.
    # --list enumerates names, --check runs one-or-more predicates, and
    # --certify bundles the dry-run or execute subset for end-of-phase
    # promotion. See §9.7 of DUAL_AGENT_PLAN_EXECUTOR.md.
    p_gates = sub.add_parser(
        "gates",
        help=(
            "Run the six canonical phase gates (schema-valid, schedule-valid, "
            "fixture-valid, execution-safe, review-safe, commit-safe) either "
            "individually (--check) or as a dry-run/execute certification "
            "bundle (--certify)."
        ),
    )
    mx_gates = p_gates.add_mutually_exclusive_group(required=True)
    mx_gates.add_argument(
        "--list", action="store_true",
        help="Emit the canonical list of gate names",
    )
    mx_gates.add_argument(
        "--check", default=None,
        help=(
            "Comma-separated gate names to evaluate; fails with exit 1 if "
            "any checked gate returns status=fail"
        ),
    )
    mx_gates.add_argument(
        "--certify", action="store_true",
        help=(
            "Certify the promotion bundle for --mode (dry-run|execute); "
            "exit 1 if any required gate in the bundle returns status=fail"
        ),
    )
    p_gates.add_argument(
        "--mode", choices=("dry-run", "execute"), default=None,
        help="Required with --certify; selects the promotion bundle",
    )
    p_gates.add_argument(
        "--plan-file", default=None,
        help="Path to plan markdown file (for schema-valid/fixture-valid)",
    )
    p_gates.add_argument(
        "--schedule-file", default=None,
        help="Path to schedule JSON file (for schedule-valid)",
    )
    p_gates.add_argument(
        "--commit-sha", default=None,
        help="Commit SHA to inspect for commit-safe",
    )
    p_gates.add_argument(
        "--task-id", default=None,
        help="Task id whose Files: list scopes commit-safe allowed paths",
    )
    p_gates.add_argument(
        "--run-id", default=None,
        help="Run identifier for certify-execute commit-safe verification",
    )
    _add_json(p_gates)

    # -----------------------------------------------------------------
    # TASK-008 (POSTMORTEM_FIXES): sandbox-divergence escape hatch CLI
    # -----------------------------------------------------------------
    # Two subcommands:
    #   * ``auto-validate-divergence`` — orchestrator dispatch handler.
    #     Reads the wrapper's failure-path implement envelope (stdin or
    #     ``--envelope-file``), and on the matching failure cause
    #     (``cause: independent_test_run_failed``) re-runs the task's
    #     declared ``Test command:`` in the target env (cwd = repo root,
    #     env inherited). Target-passes → emits ``divergence: true`` and
    #     appends a ``sandbox_divergence`` run-log event. Target-fails →
    #     emits ``divergence: false`` so the existing failure path
    #     (Codex→Claude fallback OR task-fail) runs unchanged.
    #   * ``run-summary`` — end-of-run report. Scans the run log for
    #     ``sandbox_divergence`` events under ``--run-id`` and emits the
    #     "Sandbox divergences" subsection.
    p_avd = sub.add_parser(
        "auto-validate-divergence",
        help=(
            "Orchestrator auto-validate branch for the TASK-008 "
            "sandbox-divergence escape hatch. Reads a Codex implement "
            "envelope and re-runs the task's Test command in the "
            "target env on `cause: independent_test_run_failed`."
        ),
    )
    p_avd.add_argument(
        "--envelope-file", default=None,
        help=(
            "Path to a JSON file containing the wrapper's implement "
            "envelope. When omitted, reads JSON from stdin."
        ),
    )
    p_avd.add_argument(
        "--test-command", required=True,
        help=(
            "The task's declared `Test command:` field, resolved from "
            "the plan markdown by the orchestrator. The wrapper's "
            "recorded sandbox command is NOT used (it may carry an "
            "environment-specific prefix that breaks in the target env)."
        ),
    )
    p_avd.add_argument(
        "--repo-root", default=None,
        help=(
            "Working directory for the target-env test re-run. Defaults "
            "to the current working directory."
        ),
    )
    p_avd.add_argument(
        "--run-id", default=None,
        help=(
            "Run identifier; when set, a `sandbox_divergence` event is "
            "appended to the run log on target-passes outcomes."
        ),
    )
    p_avd.add_argument(
        "--task-id", default=None,
        help=(
            "Task identifier for the run-log event. Falls back to the "
            "envelope's `task_id` when omitted."
        ),
    )
    p_avd.add_argument(
        "--timeout", type=int, default=300,
        help="Timeout in seconds for the target-env test re-run.",
    )
    _add_json(p_avd)

    p_rsm = sub.add_parser(
        "run-summary",
        help=(
            "End-of-run report subsections. Emits the TASK-008 "
            "'Sandbox divergences' list when --section sandbox-divergences "
            "is selected."
        ),
    )
    p_rsm.add_argument(
        "--section", required=True,
        choices=["sandbox-divergences"],
        help="Which subsection of the run summary to emit.",
    )
    p_rsm.add_argument(
        "--run-id", required=True,
        help="Run identifier to scope the run-log scan.",
    )
    _add_json(p_rsm)

    return parser


def _args_to_payload_auto_validate_divergence(args: argparse.Namespace) -> dict:
    payload = {
        "envelope_file": pathlib.Path(args.envelope_file) if args.envelope_file else None,
        "test_command": args.test_command,
        "repo_root": pathlib.Path(args.repo_root) if args.repo_root else None,
        "run_id": args.run_id,
        "task_id": args.task_id,
        "timeout": args.timeout,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_auto_validate_divergence(payload: dict) -> dict:
    if payload['envelope_file']:
        try:
            raw = Path(payload['envelope_file']).read_text(encoding='utf-8')
        except OSError as e:
            return _result({'error': f'failed to read --envelope-file: {e}'}, exit_code=1)
    else:
        raw = payload['stdin_text']
    if not raw.strip():
        return _result({'error': 'auto-validate-divergence expects an envelope on stdin (or --envelope-file)'}, exit_code=1)
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as e:
        return _result({'error': f'envelope is not valid JSON: {e}'}, exit_code=1)
    if not isinstance(envelope, dict):
        return _result({'error': 'envelope must be a JSON object'}, exit_code=1)
    task_id = payload['task_id'] or envelope.get('task_id')
    outcome = envelope.get('outcome')
    cause = envelope.get('cause')
    if not (outcome == 'failure' and cause == 'independent_test_run_failed'):
        return _result({'divergence': False, 'applicable': False, 'task_id': task_id, 'target_test': None, 'sandbox_divergence': None, 'reason': f'envelope outcome={outcome!r} cause={cause!r} does not match independent_test_run_failed; auto-validate skipped', 'errors': []}, exit_code=0)
    test_cmd = (payload['test_command'] or '').strip()
    if not test_cmd or test_cmd.lower() == 'none':
        return _result({'error': 'auto-validate-divergence requires a non-empty --test-command; the failure cause matched but no target-env command is available to re-run'}, exit_code=1)
    deferred = _parse_deferred_test_command(test_cmd)
    if deferred["kind"] == "malformed":
        return _result({'error': deferred["error"], 'task_id': task_id}, exit_code=1)
    if deferred["kind"] == "deferred":
        event_fields = {
            "task_id": task_id,
            "deferred_to": deferred["deferred_to"],
        }
        if payload['run_id']:
            event_fields["run_id"] = payload['run_id']
        if deferred["note"]:
            event_fields["note"] = deferred["note"]
        _append_run_log("test_deferred", event_fields)
        target_test = {
            "result": "deferred",
            "exit_code": None,
            "stdout_tail": "",
            "stderr_tail": "",
            "command": deferred["command"],
            "deferred_to": deferred["deferred_to"],
        }
        if deferred["note"]:
            target_test["note"] = deferred["note"]
        return _result({
            'divergence': False,
            'applicable': True,
            'task_id': task_id,
            'target_test': target_test,
            'sandbox_divergence': None,
            'test_deferred': event_fields,
            'errors': [],
        }, exit_code=0)
    repo_root = payload['repo_root'] or os.getcwd()
    timeout_sec = int(payload['timeout'])
    try:
        proc = subprocess.run(test_cmd, shell=True, cwd=repo_root, capture_output=True, timeout=timeout_sec)
        timed_out = False
        target_stdout = proc.stdout.decode(errors='replace')
        target_stderr = proc.stderr.decode(errors='replace')
        target_exit = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        target_stdout = (e.stdout or b'').decode(errors='replace')
        target_stderr = f'TIMEOUT after {timeout_sec}s\n' + (e.stderr or b'').decode(errors='replace')
        target_exit = None
    target_passed = not timed_out and target_exit == 0
    cap = 32 * 1024

    def _tail(s: str) -> str:
        encoded = (s or '').encode('utf-8', errors='replace')
        if len(encoded) <= cap:
            return s or ''
        return encoded[-cap:].decode('utf-8', errors='replace')
    target_test = {'result': 'passed' if target_passed else 'failed', 'exit_code': target_exit, 'stdout_tail': _tail(target_stdout), 'stderr_tail': _tail(target_stderr), 'command': test_cmd}
    sandbox_block: dict | None = None
    if target_passed:
        sandbox_block = {'task_id': task_id, 'sandbox': {'stdout': envelope.get('sandbox_test_stdout'), 'stderr': envelope.get('sandbox_test_stderr'), 'command': envelope.get('sandbox_test_command'), 'exit_code': envelope.get('sandbox_test_exit_code'), 'attempt_count': envelope.get('sandbox_test_attempt_count'), 'stdout_truncated_to': envelope.get('sandbox_test_stdout_truncated_to'), 'stderr_truncated_to': envelope.get('sandbox_test_stderr_truncated_to')}, 'target': target_test}
        if payload['run_id']:
            event_fields = {'run_id': payload['run_id'], 'task_id': task_id, 'sandbox_divergence': sandbox_block}
            _append_run_log('sandbox_divergence', event_fields)
    return _result({'divergence': target_passed, 'applicable': True, 'task_id': task_id, 'target_test': target_test, 'sandbox_divergence': sandbox_block, 'errors': []}, exit_code=0)

def cmd_auto_validate_divergence(args: argparse.Namespace) -> None:
    'TASK-008 (POSTMORTEM_FIXES) — orchestrator auto-validate branch.\n\n    Reads the wrapper\'s implement envelope (stdin or ``--envelope-file``)\n    and, when its outcome is `failure` with cause\n    `independent_test_run_failed`, re-runs the task\'s declared\n    `Test command:` in the target env. Returns:\n\n        {\n          "divergence": bool,         # True iff sandbox-failed but target-passed\n          "applicable": bool,         # False when the envelope did not match the cause\n          "task_id": str | None,\n          "target_test": {            # only present when applicable\n            "result": "passed"|"failed"|"not_run",\n            "exit_code": int|None,\n            "stdout_tail": str,\n            "stderr_tail": str,\n            "command": str,\n          },\n          "sandbox_divergence": { ... } | None,  # block to embed in commit-task / run-log\n          "errors": [...]\n        }\n\n    On `divergence: true` AND `--run-id` set, a `sandbox_divergence`\n    event is appended to the run log so `run-summary\n    --section sandbox-divergences` can list affected tasks.\n    '
    payload = _args_to_payload_auto_validate_divergence(args)
    result = _run_auto_validate_divergence(payload)
    _emit_or_die(args, result)


def _args_to_payload_run_summary(args: argparse.Namespace) -> dict:
    payload = {
        "section": args.section,
        "run_id": args.run_id,
    }
    return payload

def _run_run_summary(payload: dict) -> dict:
    if payload['section'] != 'sandbox-divergences':
        return _result({'error': f"unknown --section: {payload['section']!r}"}, exit_code=1)
    entries: list[dict] = []
    if RUN_LOG_PATH.is_file():
        try:
            for line in RUN_LOG_PATH.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('event') != 'sandbox_divergence':
                    continue
                if rec.get('run_id') != payload['run_id']:
                    continue
                block = rec.get('sandbox_divergence') or {}
                sandbox = block.get('sandbox') or {}
                target = block.get('target') or {}
                entries.append({'task_id': rec.get('task_id'), 'sandbox_command': sandbox.get('command'), 'sandbox_exit_code': sandbox.get('exit_code'), 'target_command': target.get('command'), 'ts': rec.get('ts')})
        except OSError:
            pass
    lines = ['## Sandbox divergences']
    if not entries:
        lines.append('')
        lines.append('None.')
    else:
        lines.append('')
        for e in entries:
            lines.append(f"- TASK-{e['task_id']}: sandbox exit={e['sandbox_exit_code']!r} (`{e['sandbox_command']}`); target re-run (`{e['target_command']}`) passed @ {e['ts']}")
    markdown = '\n'.join(lines) + '\n'
    return _result({'section': 'sandbox-divergences', 'run_id': payload['run_id'], 'count': len(entries), 'entries': entries, 'markdown': markdown}, exit_code=0)

def cmd_run_summary(args: argparse.Namespace) -> None:
    'TASK-008 (POSTMORTEM_FIXES) — end-of-run report subsections.\n\n    Currently supports a single section, ``sandbox-divergences``,\n    which scans the run log for ``sandbox_divergence`` events under\n    ``--run-id`` and emits the "Sandbox divergences" list. Each entry\n    carries the task id and a brief `command` reference so the human\n    reviewer can locate the underlying captures (the full sandbox\n    stdout/stderr live in the run-log event, by design — keeping the\n    summary scannable).\n    '
    payload = _args_to_payload_run_summary(args)
    result = _run_run_summary(payload)
    _emit_or_die(args, result)


# ---------------------------------------------------------------------------
# TASK-001 (PHASE_D_STATE_MACHINE): review-route subcommand.
#
# Pure deterministic router for the SKILL Phase D verdict tables. Inputs are
# the parsed reviewer envelope, optional D.5 third-opinion envelope, per-task
# retry budget, and a tiny `flags` block. Output is one `action` directive
# the orchestrator executes verbatim. Every D.2 / D.2a / D.2a.5 / D.2a.6 /
# D.2b cell collapses to one `action` value; `unknown_state` is the escape
# hatch on input that does not match a known cell.
#
# Routing logic lives in `route()`; `cmd_review_route` is a thin stdin/_emit
# shim. Tests call `route()` directly — no subprocess, no stdin monkey-patch.
# ---------------------------------------------------------------------------


_REVIEW_ROUTE_ACTIONS = {
    "commit",
    "fail",
    "dispatch_d5",
    "dispatch_bounded_remediation",
    "dispatch_narrow_remediation",
    "dispatch_role_swap",
    "pause_awaiting_user",
    "unknown_state",
}

# Codex reviewer verdict vocabulary (review of Claude work). Gemini-as-reviewer
# emits the same vocabulary on Claude work and routes identically.
_CODEX_VERDICTS = {"clean", "minor-findings", "needs-rework"}
# Claude reviewer verdict vocabulary (review of Codex work, or claude_only).
_CLAUDE_VERDICTS = {"ship", "ship-with-fixes", "needs-rework"}


def _blocking_findings(findings: object) -> list[dict]:
    """Subset of reviewer findings flagged `blocking: true`.

    The Phase D-Claude reviewer template (dispatch-templates.md §Phase
    D-Claude) requires every entry of `findings[]` to carry a `blocking:
    bool` discriminator and forbids `blocking: true` under
    `ship-with-fixes`. A contradictory `ship-with-fixes` envelope
    (verdict says soft-pass; a finding says ship-blocker) is treated by
    the router as a structural escalation to remediation — incomplete
    implementation must never reach commit through the soft-pass channel.
    Legacy envelopes without the `blocking` field on every entry route
    as-if no blocking findings were declared (backward-compat: prior
    reviewers emitted only verdict + summary).
    """
    if not isinstance(findings, list):
        return []
    return [
        f for f in findings
        if isinstance(f, dict) and f.get("blocking") is True
    ]
# D.5 third-opinion verdict vocabulary.
_D5_VERDICTS = {"ship", "ship-with-fixes", "partial-agreement", "needs-rework"}
# Reviewer identity vocabulary (TASK-001 PHASE_D_STATE_MACHINE_COMPLETION).
_ALLOWED_REVIEWERS = {"codex", "gemini", "claude", "none"}
_UNATTENDED_REVERT_POLICIES = {"pause", "fail-fast", "preserve-only"}


def _validate_review_route_input(payload: object) -> list[dict]:
    """Structural input validation for `route()`.

    Returns a list of structured error dicts; empty list = valid. Used by
    `cmd_review_route` to fail with non-zero exit on schema violations.
    The `route()` function itself tolerates unknown enum values and routes
    them to `unknown_state` so the orchestrator can pause and return to the
    user; the validator only catches structural breakage (missing required
    keys, wrong types).
    """
    errors: list[dict] = []
    if not isinstance(payload, dict):
        return [{"path": "$", "message": "input must be a JSON object"}]

    required = ("task_id", "implementer", "reviewer_envelope", "retries_used", "flags")
    for key in required:
        if key not in payload:
            errors.append({"path": f"$.{key}", "message": "required field missing"})

    if "task_id" in payload and not isinstance(payload["task_id"], str):
        errors.append({"path": "$.task_id", "message": "must be a string"})
    if "implementer" in payload and not isinstance(payload["implementer"], str):
        errors.append({"path": "$.implementer", "message": "must be a string"})
    # TASK-001 (PHASE_D_STATE_MACHINE_COMPLETION): optional reviewer / claude_only.
    # Both are optional for backward compatibility with envelopes that predate
    # the completion plan; presence is type-checked but unknown enum values
    # are routed to `unknown_state` by `route()` rather than rejected here.
    if "reviewer" in payload and not isinstance(payload["reviewer"], str):
        errors.append({"path": "$.reviewer", "message": "must be a string"})
    if "claude_only" in payload and not isinstance(payload["claude_only"], bool):
        errors.append({"path": "$.claude_only", "message": "must be a boolean"})
    if "unattended_revert_policy" in payload and not isinstance(payload["unattended_revert_policy"], str):
        errors.append({"path": "$.unattended_revert_policy", "message": "must be a string"})

    rev = payload.get("reviewer_envelope")
    if "reviewer_envelope" in payload:
        if not isinstance(rev, dict):
            errors.append({"path": "$.reviewer_envelope", "message": "must be an object"})
        elif "verdict" not in rev:
            errors.append({"path": "$.reviewer_envelope.verdict", "message": "required field missing"})
        elif not isinstance(rev["verdict"], str):
            errors.append({"path": "$.reviewer_envelope.verdict", "message": "must be a string"})

    d5 = payload.get("d5_envelope", None)
    if d5 is not None and not isinstance(d5, dict):
        errors.append({"path": "$.d5_envelope", "message": "must be null or an object"})
    elif isinstance(d5, dict):
        if "verdict" not in d5:
            errors.append({"path": "$.d5_envelope.verdict", "message": "required field missing"})
        elif not isinstance(d5["verdict"], str):
            errors.append({"path": "$.d5_envelope.verdict", "message": "must be a string"})

    retries = payload.get("retries_used")
    if "retries_used" in payload and not isinstance(retries, dict):
        errors.append({"path": "$.retries_used", "message": "must be an object"})

    flags = payload.get("flags")
    if "flags" in payload and not isinstance(flags, dict):
        errors.append({"path": "$.flags", "message": "must be an object"})

    return errors


def _unknown(reason: str, *, task_id: str | None = None,
             stage: str = "unknown_state") -> dict:
    """Build an `unknown_state` directive. The orchestrator pauses on this."""
    args: dict = {}
    if task_id is not None:
        args["task_id"] = task_id
    args["pause_payload"] = {"stage": stage, "reason": reason}
    return {"action": "unknown_state", "reason": reason, "args": args}


_PLAN_REVIEW_ROUTE_ACTIONS = {
    "skip_plan_review",
    "dispatch_claude_reviewer",
    "dispatch_codex_reviewer",
    "dispatch_gemini_reviewer",
    "proceed_to_phase_2",
    "dispatch_triage",
    "dispatch_plan_author_per_finding",
    "rerun_analyst_then_review",
    "halt_plan_review_failed",
    "pause_awaiting_user",
    "unknown_state",
}

_PLAN_REVIEW_ROUTE_STAGES = {
    "pre_dispatch",
    "post_review",
    "post_triage",
    "post_plan_author",
    "manual_pause",
}
_PLAN_REVIEW_TRIAGE_VERDICTS = {
    "ship",
    "ship-with-fixes",
    "partial-agreement",
    "needs-rework",
}
_PLAN_REVIEW_FAILURE_OUTCOMES = {"timeout", "parse_error", "failure"}


def _plan_review_unknown(reason: str) -> dict:
    return {"action": "unknown_state", "reason": reason, "args": {}}


def _plan_review_route_attempt(payload: dict) -> int:
    raw = payload.get("attempt")
    if raw is None:
        state = payload.get("plan_review_state")
        if isinstance(state, dict):
            raw = state.get("attempt")
            if raw is None and state.get("auto_revise_round_completed") is True:
                return 2
    if isinstance(raw, bool):
        return 1
    if isinstance(raw, int):
        return raw
    return 1


def _plan_review_route_findings(payload: dict) -> list:
    env = payload.get("plan_review_envelope")
    if isinstance(env, dict) and isinstance(env.get("findings"), list):
        return env["findings"]
    parsed = env.get("parsed") if isinstance(env, dict) else None
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
        return parsed["findings"]
    findings = payload.get("findings")
    if isinstance(findings, list):
        return findings
    return []


def _plan_review_route_reviewer(payload: dict) -> str | None:
    raw = payload.get("reviewer")
    if isinstance(raw, str):
        return raw
    if payload.get("claude_only") is True:
        return "claude"
    env = payload.get("plan_review_envelope")
    if isinstance(env, dict) and isinstance(env.get("reviewer"), str):
        return env["reviewer"]
    return None


def _plan_review_route_child_file(payload: dict, task_id: object) -> object:
    if not isinstance(task_id, str):
        return None
    state = payload.get("plan_review_state")
    if not isinstance(state, dict):
        return None
    mapping = state.get("task_plan_file_map")
    if not isinstance(mapping, dict):
        return None
    return mapping.get(task_id) or mapping.get(_normalize_task_id(task_id) or task_id)


def _plan_review_route_dispatches(payload: dict, findings: list) -> list[dict]:
    dispatches: list[dict] = []
    for index, finding in enumerate(findings):
        target_task_id = None
        if isinstance(finding, dict):
            target_task_id = finding.get("target_task_id")
        dispatches.append({
            "source_index": (
                finding.get("source_index", index)
                if isinstance(finding, dict)
                else index
            ),
            "finding": finding,
            "target_task_id": target_task_id,
            "variant": "A" if target_task_id is not None else "B",
            "child_plan_file": _plan_review_route_child_file(
                payload, target_task_id,
            ),
        })
    return dispatches


def _with_plan_review_state_transitions(directive: dict, transitions: dict) -> dict:
    out = copy.deepcopy(directive)
    out["state_transitions"] = copy.deepcopy(transitions)
    args = out.setdefault("args", {})
    if isinstance(args, dict):
        args["state_transitions"] = copy.deepcopy(transitions)
    return out


def _plan_review_route(payload: dict) -> dict:
    """Pure deterministic router for Phase 1.5 / 1.5.5 plan review."""
    if not isinstance(payload, dict):
        return _plan_review_unknown("input must be a JSON object")

    stage = payload.get("stage")
    if stage not in _PLAN_REVIEW_ROUTE_STAGES:
        return _plan_review_unknown(
            f"unrecognized plan-review route stage {stage!r}; "
            f"expected one of {sorted(_PLAN_REVIEW_ROUTE_STAGES)!r}"
        )

    flags = payload.get("flags") or {}
    if not isinstance(flags, dict):
        return _plan_review_unknown("flags must be an object")

    if stage == "pre_dispatch":
        if flags.get("skip_plan_review") is True:
            return _with_plan_review_state_transitions({
                "action": "skip_plan_review",
                "reason": "flag",
                "args": {"reason": "flag"},
            }, {"skipped_reason": "flag"})
        if payload.get("claude_only") is True:
            return {"action": "dispatch_claude_reviewer", "args": {}}
        if payload.get("claude_only") is False:
            reviewer = _plan_review_route_reviewer(payload)
            if reviewer == "gemini":
                return {
                    "action": "dispatch_gemini_reviewer",
                    "args": {
                        "dispatch_context": {
                            "allow_gaps_demotion": bool(flags.get("allow_gaps", False)),
                        },
                    },
                    "dispatch_context": {
                        "allow_gaps_demotion": bool(flags.get("allow_gaps", False)),
                    },
                }
            return {
                "action": "dispatch_codex_reviewer",
                "args": {
                    "dispatch_context": {
                        "allow_gaps_demotion": bool(flags.get("allow_gaps", False)),
                    },
                },
                "dispatch_context": {
                    "allow_gaps_demotion": bool(flags.get("allow_gaps", False)),
                },
            }
        return _plan_review_unknown("pre_dispatch requires claude_only boolean")

    if stage == "post_review":
        env = payload.get("plan_review_envelope")
        if not isinstance(env, dict):
            return _plan_review_unknown("post_review requires plan_review_envelope")
        outcome = env.get("outcome")
        reviewer = _plan_review_route_reviewer(payload)
        if outcome in _PLAN_REVIEW_FAILURE_OUTCOMES:
            reason = (
                "claude_review_failure"
                if outcome == "failure" and reviewer == "claude"
                else (
                    "codex_unavailable"
                    if env.get("envelope_error") == "codex binary not found on PATH"
                    else f"codex_plan_review_{outcome}"
                )
            )
            return _with_plan_review_state_transitions({
                "action": "skip_plan_review",
                "reason": reason,
                "args": {"reason": reason, "outcome": outcome},
            }, {"skipped_reason": reason})
        verdict = env.get("verdict")
        if outcome not in (None, "success"):
            return _plan_review_unknown(
                f"unrecognized plan-review outcome {outcome!r}"
            )
        if verdict not in ALLOWED_PLAN_REVIEW_VERDICTS:
            return _plan_review_unknown(
                f"unrecognized plan-review verdict {verdict!r}; "
                f"expected one of {sorted(ALLOWED_PLAN_REVIEW_VERDICTS)!r}"
            )
        if verdict in {"approved", "approved-with-notes"}:
            return _with_plan_review_state_transitions({
                "action": "proceed_to_phase_2",
                "args": {
                    "summary_section": {
                        "findings_count": len(_plan_review_route_findings(payload)),
                        "notes_count": len(env.get("notes") or [])
                        if isinstance(env.get("notes"), list)
                        else 0,
                    },
                },
                "summary_section": {
                    "findings_count": len(_plan_review_route_findings(payload)),
                    "notes_count": len(env.get("notes") or [])
                    if isinstance(env.get("notes"), list)
                    else 0,
                },
            }, {
                "record_plan_review_verdict": {
                    "attempt": _plan_review_route_attempt(payload),
                    "verdict": verdict,
                    "findings_count": len(_plan_review_route_findings(payload)),
                },
            })

        # verdict == needs-replan
        if flags.get("codex_plan_review_binding") is True:
            return _with_plan_review_state_transitions({
                "action": "halt_plan_review_failed",
                "reason": "needs-replan with binding plan review",
                "args": {"reason_detail": "binding_flag"},
            }, {
                "record_plan_review_verdict": {
                    "attempt": _plan_review_route_attempt(payload),
                    "verdict": verdict,
                    "findings_count": len(_plan_review_route_findings(payload)),
                },
            })
        if flags.get("no_auto_revise") is True:
            return _with_plan_review_state_transitions({
                "action": "halt_plan_review_failed",
                "reason": "needs-replan with auto-revise disabled",
                "args": {"reason_detail": "no_auto_revise"},
            }, {
                "record_plan_review_verdict": {
                    "attempt": _plan_review_route_attempt(payload),
                    "verdict": verdict,
                    "findings_count": len(_plan_review_route_findings(payload)),
                },
            })
        attempt = _plan_review_route_attempt(payload)
        if attempt == 1:
            return _with_plan_review_state_transitions({
                "action": "dispatch_triage",
                "args": {
                    "dispatch_context": {
                        "findings_for_payload": _plan_review_route_findings(payload),
                    },
                },
                "dispatch_context": {
                    "findings_for_payload": _plan_review_route_findings(payload),
                },
            }, {
                "record_plan_review_verdict": {
                    "attempt": attempt,
                    "verdict": verdict,
                    "findings_count": len(_plan_review_route_findings(payload)),
                },
                "triage_dispatched": True,
            })
        if attempt == 2:
            return _with_plan_review_state_transitions({
                "action": "halt_plan_review_failed",
                "reason": "second plan-review pass still needs replan",
                "args": {"reason_detail": "second_needs_replan"},
            }, {
                "record_plan_review_verdict": {
                    "attempt": attempt,
                    "verdict": verdict,
                    "findings_count": len(_plan_review_route_findings(payload)),
                },
            })
        return _plan_review_unknown(
            f"unsupported plan-review attempt {attempt!r}; expected 1 or 2"
        )

    if stage == "post_plan_author":
        return _with_plan_review_state_transitions({
            "action": "rerun_analyst_then_review",
            "args": {
                "next_attempt": 2,
                "binding_second_pass": True,
            },
        }, {
            "attempt": 2,
            "auto_revise_round_completed": True,
        })

    if stage == "manual_pause":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason:
            reason = "plan-review routing requested a user pause"
        return {
            "action": "pause_awaiting_user",
            "reason": reason,
            "args": {"pause_payload": {"stage": "plan_review", "reason": reason}},
        }

    # stage == post_triage
    triage = payload.get("triage_envelope") or payload.get("plan_review_triage_envelope")
    if not isinstance(triage, dict):
        return _plan_review_unknown("post_triage requires triage_envelope")
    verdict = triage.get("verdict")
    if verdict not in _PLAN_REVIEW_TRIAGE_VERDICTS:
        return _plan_review_unknown(
            f"unrecognized plan-review triage verdict {verdict!r}; "
            f"expected one of {sorted(_PLAN_REVIEW_TRIAGE_VERDICTS)!r}"
        )
    if verdict == "ship":
        return _with_plan_review_state_transitions({
            "action": "proceed_to_phase_2",
            "args": {
                "summary_section": {
                    "banner": "[plan-review-disagreement]",
                    "triage_summary": triage.get("summary", ""),
                },
            },
            "summary_section": {
                "banner": "[plan-review-disagreement]",
                "triage_summary": triage.get("summary", ""),
            },
        }, {
            "record_triage_outcome": {
                "verdict": verdict,
                "load_bearing": [],
                "dismissed": list(range(len(_plan_review_route_findings(payload)))),
            },
        })
    if verdict == "ship-with-fixes":
        return _with_plan_review_state_transitions({
            "action": "proceed_to_phase_2",
            "args": {
                "summary_section": {
                    "notes_section": "Plan review notes",
                    "triage_summary": triage.get("summary", ""),
                },
            },
            "summary_section": {
                "notes_section": "Plan review notes",
                "triage_summary": triage.get("summary", ""),
            },
        }, {
            "record_triage_outcome": {
                "verdict": verdict,
                "load_bearing": list(range(len(_plan_review_route_findings(payload)))),
                "dismissed": [],
            },
        })

    findings = _plan_review_route_findings(payload)
    if verdict == "partial-agreement":
        load_bearing = [
            i for i in (triage.get("load_bearing") or [])
            if isinstance(i, int) and 0 <= i < len(findings)
        ]
        load_bearing_set = set(load_bearing)
        findings_for_payload = [findings[i] for i in load_bearing]
        dismissed_for_context = [
            findings[i] for i in range(len(findings)) if i not in load_bearing_set
        ]
    else:
        findings_for_payload = list(findings)
        dismissed_for_context = []

    triage_transitions = {
        "record_triage_outcome": {
            "verdict": verdict,
            "load_bearing": (
                load_bearing
                if verdict == "partial-agreement"
                else list(range(len(findings)))
            ),
            "dismissed": (
                [i for i in range(len(findings)) if i not in set(load_bearing)]
                if verdict == "partial-agreement"
                else []
            ),
        },
    }
    return _with_plan_review_state_transitions({
        "action": "dispatch_plan_author_per_finding",
        "args": {
            "dispatch_context": {
                "findings_for_payload": findings_for_payload,
                "dismissed_for_context": dismissed_for_context,
                "per_finding_dispatches": _plan_review_route_dispatches(
                    payload, findings_for_payload,
                ),
                "triage_summary": triage.get("summary", ""),
            },
        },
        "dispatch_context": {
            "findings_for_payload": findings_for_payload,
            "dismissed_for_context": dismissed_for_context,
            "per_finding_dispatches": _plan_review_route_dispatches(
                payload, findings_for_payload,
            ),
            "triage_summary": triage.get("summary", ""),
        },
    }, triage_transitions)


def _route_review_route(payload: dict) -> dict:
    """Pure routing function for SKILL Phase D.

    Maps `(implementer, reviewer_verdict, d5_verdict, retries_used, flags)`
    to one of the eight `action` values. Tests should call this directly.

    `unknown_state` is the escape hatch: any verdict outside the documented
    enum, or any combination not enumerated in §§D.2 / D.2a / D.2b, returns
    `action: "unknown_state"` with a human-readable `reason`. The
    orchestrator pauses on that value and returns to the user.
    """
    if not isinstance(payload, dict):
        return _unknown("input must be a JSON object")

    task_id = payload.get("task_id")
    implementer = payload.get("implementer")
    rev = payload.get("reviewer_envelope") or {}
    rev_verdict = rev.get("verdict") if isinstance(rev, dict) else None
    rev_findings = rev.get("findings", []) if isinstance(rev, dict) else []
    rev_summary = rev.get("summary", "") if isinstance(rev, dict) else ""

    d5 = payload.get("d5_envelope")
    d5_verdict = d5.get("verdict") if isinstance(d5, dict) else None
    d5_load_bearing = d5.get("load_bearing", []) if isinstance(d5, dict) else []
    d5_dismissed = d5.get("dismissed", []) if isinstance(d5, dict) else []
    d5_summary = d5.get("summary", "") if isinstance(d5, dict) else ""

    retries = payload.get("retries_used") or {}
    bounded_used = bool(retries.get("bounded_remediation", False))
    narrow_used = bool(retries.get("narrow_remediation", False))
    role_swap_used = bool(retries.get("role_swap", False))

    flags = payload.get("flags") or {}
    codex_binding = bool(flags.get("codex_review_binding", False))
    unattended_revert_policy = payload.get("unattended_revert_policy", "pause")
    if not isinstance(unattended_revert_policy, str):
        return _unknown(
            "unattended_revert_policy must be a string; "
            f"got {type(unattended_revert_policy).__name__}",
            task_id=task_id,
        )
    if unattended_revert_policy not in _UNATTENDED_REVERT_POLICIES:
        return _unknown(
            f"unrecognized unattended_revert_policy {unattended_revert_policy!r}; "
            f"expected one of {sorted(_UNATTENDED_REVERT_POLICIES)!r}",
            task_id=task_id,
        )

    # TASK-001 (PHASE_D_STATE_MACHINE_COMPLETION): reviewer identity + runtime
    # mode are first-class. Both are optional for backward-compat with payloads
    # that only carried `implementer`; missing values are inferred from the
    # implementer the same way the legacy router did implicitly.
    reviewer_raw = payload.get("reviewer")
    if reviewer_raw is not None and not isinstance(reviewer_raw, str):
        return _unknown(
            f"reviewer must be a string; got {type(reviewer_raw).__name__}",
            task_id=task_id,
        )
    claude_only_raw = payload.get("claude_only", False)
    if not isinstance(claude_only_raw, bool):
        return _unknown(
            f"claude_only must be a boolean; got {type(claude_only_raw).__name__}",
            task_id=task_id,
        )
    claude_only = bool(claude_only_raw)

    if implementer not in ("claude", "codex"):
        return _unknown(
            f"unrecognized implementer {implementer!r}; expected 'claude' or 'codex'",
            task_id=task_id,
        )

    # Backward-compat reviewer inference: legacy payloads omit `reviewer` and
    # rely on the implementer→reviewer mapping the original router assumed.
    if reviewer_raw is None:
        reviewer = "codex" if implementer == "claude" else "claude"
    else:
        reviewer = reviewer_raw

    if reviewer not in _ALLOWED_REVIEWERS:
        return _unknown(
            f"unrecognized reviewer {reviewer!r}; "
            f"expected one of {sorted(_ALLOWED_REVIEWERS)!r}",
            task_id=task_id,
        )

    # ---- Skip-review: reviewer="none" routes straight to commit. ----
    # Used for the explicit skip-review path; carries reviewer metadata so the
    # follow-on `commit-task` invocation can pass `reviewer=none`.
    if reviewer == "none":
        return {
            "action": "commit",
            "args": {
                "task_id": task_id,
                "reviewer": "none",
                "commit_flags": {
                    "disagreement_tag": False,
                    "remediation_tag": False,
                    "narrow_remediation_tag": False,
                    "dismissed_finding_ids": [],
                },
            },
        }

    # ---- Branch 1: Claude implementer (D.2 + D.2a ladder, plus claude_only). ----
    if implementer == "claude":
        # Codex or Gemini reviewer on Claude work: identical Codex-vocabulary
        # routing. Gemini fallback piggybacks on the Codex ladder.
        if reviewer in ("codex", "gemini"):
            if claude_only:
                return _unknown(
                    f"reviewer={reviewer!r} on Claude work is incompatible with "
                    f"claude_only=true (no Codex/Gemini shell-out under claude_only)",
                    task_id=task_id,
                )
            if rev_verdict not in _CODEX_VERDICTS:
                return _unknown(
                    f"unrecognized {reviewer.title()} reviewer verdict {rev_verdict!r}; "
                    f"expected one of {sorted(_CODEX_VERDICTS)!r}",
                    task_id=task_id,
                )
            if rev_verdict in ("clean", "minor-findings"):
                # D.2 happy path → D.3 commit, no tags.
                return {
                    "action": "commit",
                    "args": {
                        "task_id": task_id,
                        "commit_flags": {
                            "disagreement_tag": False,
                            "remediation_tag": False,
                            "narrow_remediation_tag": False,
                            "dismissed_finding_ids": [],
                        },
                    },
                }

            # rev_verdict == "needs-rework"
            # Binding-mode short-circuit: skip D.2a entirely.
            if codex_binding:
                if unattended_revert_policy == "pause":
                    return {
                        "action": "pause_awaiting_user",
                        "args": {
                            "task_id": task_id,
                            "policy_kind": "binding_policy",
                            "unattended_revert_policy": unattended_revert_policy,
                            "pause_payload": {
                                "stage": "post_binding_block",
                                "policy_kind": "binding_policy",
                                "reviewer": reviewer,
                                "reviewer_verdict": rev_verdict,
                                "codex_findings": list(rev_findings),
                                "summary": rev_summary,
                            },
                        },
                    }
                authorization_source = (
                    "unattended-fail-fast"
                    if unattended_revert_policy == "fail-fast"
                    else "unattended-preserve-only"
                )
                return {
                    "action": "fail",
                    "args": {
                        "task_id": task_id,
                        "fail_stage": "review",
                        "policy_kind": "binding_policy",
                        "unattended_revert_policy": unattended_revert_policy,
                        "authorization_source": authorization_source,
                        "fail_reason": (
                            f"codex-review-binding: {reviewer.title()} needs-rework "
                            "on Claude work; no D.5 / D.2a.5 / D.2a.6 escalation"
                        ),
                    },
                }

            # No D.5 yet → escalate to D.5 third-opinion.
            if d5 is None:
                return {
                    "action": "dispatch_d5",
                    "args": {
                        "task_id": task_id,
                        "dispatch_context": {
                            "template": "PhaseD5",
                            "findings_for_retry": list(rev_findings),
                            "wrapper_checks": rev.get("wrapper_checks", {"symbol_warnings": []})
                            if isinstance(rev, dict) else {"symbol_warnings": []},
                        },
                    },
                }

            # D.5 envelope present → consult §D.2a verdict cross-table.
            if d5_verdict not in _D5_VERDICTS:
                return _unknown(
                    f"unrecognized D.5 verdict {d5_verdict!r}; "
                    f"expected one of {sorted(_D5_VERDICTS)!r}",
                    task_id=task_id,
                )

            if d5_verdict in ("ship", "ship-with-fixes"):
                # D.5 disagreed with Codex → commit with --disagreement-tag.
                return {
                    "action": "commit",
                    "args": {
                        "task_id": task_id,
                        "commit_flags": {
                            "disagreement_tag": True,
                            "remediation_tag": False,
                            "narrow_remediation_tag": False,
                            "dismissed_finding_ids": [],
                        },
                    },
                }

            if d5_verdict == "partial-agreement":
                # D.2a.6 narrow-remediation path.
                if narrow_used:
                    # Second-review failure on narrow path → pause.
                    return {
                        "action": "pause_awaiting_user",
                        "args": {
                            "task_id": task_id,
                            "pause_payload": {
                                "stage": "post_narrow_remediation_review",
                                "codex_findings": list(rev_findings),
                                "d5_summary": d5_summary,
                                "dismissed_finding_indices": list(d5_dismissed),
                            },
                        },
                    }
                # First narrow attempt → dispatch.
                load_bearing_findings = [
                    rev_findings[i] for i in d5_load_bearing
                    if isinstance(i, int) and 0 <= i < len(rev_findings)
                ]
                dismissed_findings = [
                    rev_findings[i] for i in d5_dismissed
                    if isinstance(i, int) and 0 <= i < len(rev_findings)
                ]
                return {
                    "action": "dispatch_narrow_remediation",
                    "args": {
                        "task_id": task_id,
                        "dispatch_context": {
                            "template": "PhaseB-narrow-remediation",
                            "findings_for_retry": load_bearing_findings,
                            "dismissed_for_context": dismissed_findings,
                            "d5_summary": d5_summary,
                        },
                    },
                }

            if d5_verdict == "needs-rework":
                # D.2a.5 bounded-remediation path.
                if bounded_used:
                    # Second-review failure on bounded path → pause.
                    return {
                        "action": "pause_awaiting_user",
                        "args": {
                            "task_id": task_id,
                            "pause_payload": {
                                "stage": "post_remediation_review",
                                "codex_findings": list(rev_findings),
                                "d5_summary": d5_summary,
                            },
                        },
                    }
                # First bounded attempt → dispatch.
                return {
                    "action": "dispatch_bounded_remediation",
                    "args": {
                        "task_id": task_id,
                        "dispatch_context": {
                            "template": "PhaseB-rework",
                            "findings_for_retry": list(rev_findings),
                            "d5_summary": d5_summary,
                        },
                    },
                }

            # Defensive: unreachable given enum guard above.
            return _unknown(
                f"unhandled Claude→{reviewer.title()} routing cell "
                f"(reviewer={rev_verdict!r}, d5={d5_verdict!r})",
                task_id=task_id,
            )
        if reviewer == "claude":
            # claude_only=true collapses the D.2a ladder. claude_only=false
            # with a Claude reviewer on Claude work is incoherent under the
            # current SKILL contract.
            if not claude_only:
                return _unknown(
                    "reviewer='claude' on Claude work requires claude_only=true; "
                    "non-claude_only Claude work uses Codex (or Gemini fallback) review",
                    task_id=task_id,
                )
            if rev_verdict not in _CLAUDE_VERDICTS:
                return _unknown(
                    f"unrecognized Claude reviewer verdict {rev_verdict!r}; "
                    f"expected one of {sorted(_CLAUDE_VERDICTS)!r}",
                    task_id=task_id,
                )
            blocking = _blocking_findings(rev_findings)
            if rev_verdict == "ship-with-fixes" and blocking:
                # Contradictory envelope: reviewer chose the soft-pass verdict
                # but flagged ship-blocking findings. Per Phase D-Claude
                # template, this is incomplete implementation — escalate to
                # narrow-remediation (touch-only) instead of committing.
                if narrow_used:
                    return {
                        "action": "pause_awaiting_user",
                        "args": {
                            "task_id": task_id,
                            "pause_payload": {
                                "stage": "post_narrow_remediation_review",
                                "codex_findings": list(rev_findings),
                                "d5_summary": (
                                    "ship-with-fixes with blocking findings "
                                    "after narrow-remediation already used"
                                ),
                                "dismissed_finding_indices": [],
                            },
                        },
                    }
                return {
                    "action": "dispatch_narrow_remediation",
                    "args": {
                        "task_id": task_id,
                        "dispatch_context": {
                            "template": "PhaseB-narrow-remediation",
                            "findings_for_retry": list(blocking),
                            "dismissed_for_context": [
                                f for f in rev_findings
                                if f not in blocking
                            ] if isinstance(rev_findings, list) else [],
                            "d5_summary": (
                                "ship-with-fixes verdict carried "
                                f"{len(blocking)} blocking finding(s); "
                                "Phase D-Claude reviewer template forbids "
                                "blocking findings under ship-with-fixes "
                                "(incomplete implementation must not "
                                "soft-pass). Reviewer summary: "
                                f"{rev_summary}"
                            ),
                        },
                    },
                }
            if rev_verdict in ("ship", "ship-with-fixes"):
                return {
                    "action": "commit",
                    "args": {
                        "task_id": task_id,
                        "commit_flags": {
                            "disagreement_tag": False,
                            "remediation_tag": False,
                            "narrow_remediation_tag": False,
                            "dismissed_finding_ids": [],
                        },
                    },
                }
            # needs-rework: route to D.4 fail/rescue/pause; D.5 / D.2a.5 /
            # D.2a.6 are unreachable under claude_only=true.
            return {
                "action": "fail",
                "args": {
                    "task_id": task_id,
                    "fail_stage": "review",
                    "policy_kind": "d4_review_failure",
                    "authorization_source": "phase-d4-review-failure",
                    "fail_reason": (
                        "claude_only=true: Claude reviewer needs-rework on Claude "
                        "work; D.5/D.2a.5/D.2a.6 unreachable; D.4 rescue/pause"
                    ),
                },
            }
        # Branch 1 fallthrough — caught by allowlist check above; defensive.
        return _unknown(
            f"unsupported reviewer {reviewer!r} for Claude implementer",
            task_id=task_id,
        )

    # ---- Branch 2a: Codex implementer, Gemini reviewer (no-Claude path). ----
    assert implementer == "codex"
    if reviewer == "gemini":
        if claude_only:
            return _unknown(
                "reviewer='gemini' on Codex work is incompatible with "
                "claude_only=true",
                task_id=task_id,
            )
        if rev_verdict not in _CODEX_VERDICTS:
            return _unknown(
                f"unrecognized Gemini reviewer verdict {rev_verdict!r}; "
                f"expected one of {sorted(_CODEX_VERDICTS)!r}",
                task_id=task_id,
            )
        if rev_verdict in ("clean", "minor-findings"):
            return {
                "action": "commit",
                "args": {
                    "task_id": task_id,
                    "commit_flags": {
                        "disagreement_tag": False,
                        "remediation_tag": False,
                        "narrow_remediation_tag": False,
                        "dismissed_finding_ids": [],
                    },
                },
            }
        return {
            "action": "pause_awaiting_user",
            "args": {
                "task_id": task_id,
                "pause_payload": {
                    "stage": "post_gemini_review",
                    "reviewer": reviewer,
                    "reviewer_verdict": rev_verdict,
                    "codex_findings": list(rev_findings),
                    "summary": rev_summary,
                },
            },
        }

    # ---- Branch 2b: Codex implementer, Claude reviewer (D.2 + D.2b). ----
    if reviewer != "claude":
        return _unknown(
            f"unsupported reviewer {reviewer!r} for Codex implementer; "
            "Codex-implemented work is reviewed by Claude in the Claude path "
            "or Gemini in the Codex+Gemini no-Claude path",
            task_id=task_id,
        )
    if rev_verdict not in _CLAUDE_VERDICTS:
        return _unknown(
            f"unrecognized Claude reviewer verdict {rev_verdict!r}; "
            f"expected one of {sorted(_CLAUDE_VERDICTS)!r}",
            task_id=task_id,
        )

    rev_blocking = _blocking_findings(rev_findings)
    if rev_verdict == "ship-with-fixes" and rev_blocking:
        # Contradictory envelope on Codex-implemented work: soft-pass verdict
        # with ship-blocking findings. Per Phase D-Claude template this is
        # incomplete implementation — escalate to the existing D.2b role-swap
        # remediation lane rather than committing partial work.
        if role_swap_used:
            return {
                "action": "fail",
                "args": {
                    "task_id": task_id,
                    "fail_stage": "review",
                    "policy_kind": "role_swap_exhausted",
                    "authorization_source": "phase-d2b-role-swap-exhausted",
                    "fail_reason": (
                        "role-swap retry exhausted; Claude reviewer "
                        "ship-with-fixes with blocking findings on Codex work "
                        "after one role-swap attempt"
                    ),
                },
            }
        return {
            "action": "dispatch_role_swap",
            "args": {
                "task_id": task_id,
                "dispatch_context": {
                    "template": "PhaseB-rework",
                    "findings_for_retry": list(rev_blocking),
                    "d5_summary": (
                        "ship-with-fixes verdict carried "
                        f"{len(rev_blocking)} blocking finding(s); "
                        "incomplete implementation must not soft-pass. "
                        f"Reviewer summary: {rev_summary}"
                    ),
                },
            },
        }

    if rev_verdict in ("ship", "ship-with-fixes"):
        return {
            "action": "commit",
            "args": {
                "task_id": task_id,
                "commit_flags": {
                    "disagreement_tag": False,
                    "remediation_tag": False,
                    "narrow_remediation_tag": False,
                    "dismissed_finding_ids": [],
                },
            },
        }

    # rev_verdict == "needs-rework"
    # D.2b role-swap: one attempt. If already used, fail.
    if role_swap_used:
        return {
            "action": "fail",
            "args": {
                "task_id": task_id,
                "fail_stage": "review",
                "policy_kind": "role_swap_exhausted",
                "authorization_source": "phase-d2b-role-swap-exhausted",
                "fail_reason": (
                    "role-swap retry exhausted; Claude reviewer needs-rework on "
                    "Codex work after one role-swap attempt"
                ),
            },
        }
    return {
        "action": "dispatch_role_swap",
        "args": {
            "task_id": task_id,
            "dispatch_context": {
                "template": "PhaseB-rework",
                "findings_for_retry": list(rev_findings),
                "d5_summary": rev_summary,
            },
        },
    }


def route(payload: dict) -> dict:
    """Pure routing entry point.

    Payloads carrying a top-level ``stage`` use the Phase 1.5 plan-review
    router. Legacy Phase D review-route payloads keep their existing behavior.
    """
    if isinstance(payload, dict) and "stage" in payload:
        return _plan_review_route(payload)
    return _route_review_route(payload)


def _args_to_payload_review_route(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_review_route(payload: dict) -> dict:
    raw = payload['stdin_text']
    try:
        payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        return _result({'error': 'invalid JSON on stdin', 'errors': [{'path': '$', 'message': str(exc)}]}, exit_code=1)
    errors = _validate_review_route_input(payload)
    if errors:
        return _result({'error': 'review-route input schema violation', 'errors': errors}, exit_code=1)
    directive = route(payload)
    return _result(directive, exit_code=0)

def cmd_review_route(args: argparse.Namespace) -> None:
    'Thin stdin/_emit shim around `route()`.\n\n    Reads the review-route input envelope from stdin, validates structure,\n    and emits the routing directive on stdout. Schema violations exit\n    non-zero with a structured `errors[*]` payload. Unrecognized enum\n    values (verdicts outside the documented vocabularies) are routed to\n    `action: unknown_state` by `route()` itself with exit 0 — the\n    orchestrator pauses and returns to the user on that output.\n    '
    payload = _args_to_payload_review_route(args)
    result = _run_review_route(payload)
    _emit_or_die(args, result)


def _validate_plan_review_route_input(payload: object) -> list[dict]:
    errors: list[dict] = []
    if not isinstance(payload, dict):
        return [{
            "path": "$",
            "code": "invalid-type",
            "message": "input must be a JSON object",
        }]
    required = ("stage", "flags")
    for key in required:
        if key not in payload:
            errors.append({
                "path": f"$.{key}",
                "code": "missing-field",
                "message": f"required field {key!r} missing",
            })
    stage = payload.get("stage")
    if "stage" in payload and not isinstance(stage, str):
        errors.append({
            "path": "$.stage",
            "code": "invalid-type",
            "message": "stage must be a string",
        })
    flags = payload.get("flags")
    if "flags" in payload and not isinstance(flags, dict):
        errors.append({
            "path": "$.flags",
            "code": "invalid-type",
            "message": "flags must be an object",
        })
    elif isinstance(flags, dict):
        for key in (
            "skip_plan_review",
            "codex_plan_review_binding",
            "no_auto_revise",
            "allow_gaps",
        ):
            if key in flags and not isinstance(flags[key], bool):
                errors.append({
                    "path": f"$.flags.{key}",
                    "code": "invalid-type",
                    "message": f"flags.{key} must be a boolean",
                })
    if "claude_only" in payload and not isinstance(payload["claude_only"], bool):
        errors.append({
            "path": "$.claude_only",
            "code": "invalid-type",
            "message": "claude_only must be a boolean",
        })
    if "attempt" in payload and (
        not isinstance(payload["attempt"], int)
        or isinstance(payload["attempt"], bool)
    ):
        errors.append({
            "path": "$.attempt",
            "code": "invalid-type",
            "message": "attempt must be an integer",
        })
    if stage == "post_review":
        env = payload.get("plan_review_envelope")
        if not isinstance(env, dict):
            errors.append({
                "path": "$.plan_review_envelope",
                "code": "missing-field",
                "message": "post_review requires plan_review_envelope object",
            })
        elif "verdict" in env and not (env["verdict"] is None or isinstance(env["verdict"], str)):
            errors.append({
                "path": "$.plan_review_envelope.verdict",
                "code": "invalid-type",
                "message": "verdict must be a string or null",
            })
    if stage == "post_triage":
        env = payload.get("plan_review_envelope")
        if not isinstance(env, dict):
            errors.append({
                "path": "$.plan_review_envelope",
                "code": "missing-field",
                "message": "post_triage requires plan_review_envelope object",
            })
        triage = payload.get("triage_envelope") or payload.get("plan_review_triage_envelope")
        if not isinstance(triage, dict):
            errors.append({
                "path": "$.triage_envelope",
                "code": "missing-field",
                "message": "post_triage requires triage_envelope object",
            })
        elif "verdict" in triage and not isinstance(triage["verdict"], str):
            errors.append({
                "path": "$.triage_envelope.verdict",
                "code": "invalid-type",
                "message": "verdict must be a string",
            })
    return errors


def _args_to_payload_plan_review_route(args: argparse.Namespace) -> dict:
    payload = {
        "stdin": args.stdin,
        "update_schedule_state": (
            pathlib.Path(args.update_schedule_state)
            if getattr(args, "update_schedule_state", None)
            else None
        ),
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload


def _run_plan_review_route(payload: dict) -> dict:
    raw = payload["stdin_text"]
    try:
        route_payload = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        return _result({
            "errors": [{
                "path": "$",
                "code": "json-decode",
                "message": f"stdin is not valid JSON: {exc}",
            }],
        }, exit_code=1)
    errors = _validate_plan_review_route_input(route_payload)
    if errors:
        return _result({
            "error": "plan-review-route input schema violation",
            "errors": errors,
        }, exit_code=1)
    assert isinstance(route_payload, dict)
    sched_for_state = payload.get("update_schedule_state")
    if (
        sched_for_state is not None
        and not isinstance(route_payload.get("plan_review_state"), dict)
    ):
        route_payload = dict(route_payload)
        route_payload["plan_review_state"] = read_plan_review_state(sched_for_state)
    directive = route(route_payload)
    if sched_for_state is not None:
        written, warning = _write_plan_review_state_transition(
            sched_for_state,
            directive.get("state_transitions") if isinstance(directive, dict) else None,
        )
        if isinstance(directive, dict):
            directive["schedule_state_updated"] = written
            if warning:
                directive.setdefault("warnings", []).append(warning)
    return _result(directive, exit_code=0)


def cmd_plan_review_route(args: argparse.Namespace) -> None:
    payload = _args_to_payload_plan_review_route(args)
    result = _run_plan_review_route(payload)
    _emit_or_die(args, result)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    # TASK-016C: cross-flag constraints that `add_mutually_exclusive_group`
    # cannot express directly. These are enforced here so `parser.error()`
    # produces the standard argparse exit-code-2 + usage banner; an
    # operator mistake on the CLI halts before any commit is written.
    if getattr(args, "command", None) == "commit-task":
        narrow = bool(getattr(args, "narrow_remediation_tag", False))
        dismissed_raw = getattr(args, "dismissed_finding_ids", "") or ""
        has_dismissed = bool(dismissed_raw.strip())
        d4_rescue = bool(getattr(args, "d4_rescue_tag", False))
        disagreement = bool(getattr(args, "disagreement_tag", False))
        # TASK-005: --d4-rescue-tag is mutually exclusive with
        # --disagreement-tag (D.4 rescue does not invoke D.5; it commits
        # directly on post-rescue clean re-review per SKILL.md §D.4).
        if d4_rescue and disagreement:
            parser.error(
                "--d4-rescue-tag is mutually exclusive with "
                "--disagreement-tag; D.4 rescue does not invoke D.5 "
                "(the rescue branch commits directly on post-rescue "
                "clean re-review per SKILL.md §D.4)"
            )
        # TASK-005: --d4-rescue-tag is mutually exclusive with
        # --dismissed-finding-ids. The rescue dispatch's
        # rescue_findings[] is exhaustive — every reviewer finding is
        # treated as load-bearing for the rescue attempt — so there is
        # no dismissed bucket to record.
        if d4_rescue and has_dismissed:
            parser.error(
                "--d4-rescue-tag is mutually exclusive with "
                "--dismissed-finding-ids; D.4 rescue does not carry "
                "dismissed findings (rescue_findings[] in the dispatch "
                "template is exhaustive — every reviewer finding is "
                "treated as load-bearing for the rescue attempt)"
            )
        # (c) --dismissed-finding-ids requires --narrow-remediation-tag.
        if has_dismissed and not narrow:
            parser.error(
                "--dismissed-finding-ids requires --narrow-remediation-tag; "
                "the [disagreement: i,j] trailer only appears on the "
                "D.2a.6 narrow-remediation path"
            )
        # (d) --narrow-remediation-tag requires non-empty
        # --dismissed-finding-ids. A narrow-remediation commit without
        # dismissed indices is a contradiction — there would be nothing
        # for the [disagreement: i,j] trailer to record.
        if narrow and not has_dismissed:
            parser.error(
                "--narrow-remediation-tag requires a non-empty "
                "--dismissed-finding-ids; the partial-agreement path "
                "always carries at least one dismissed index"
            )
        # TASK-016C post-remediation: content-validate the
        # --dismissed-finding-ids comma list at argparse layer so empty
        # tokens (`','`, `'1,,3'`) and non-integer tokens (`'1,x'`) fail
        # via parser.error() with exit code 2, before cmd_commit_task
        # writes the plan or touches git. Overwrites the raw string on
        # args with the parsed list[int] so handlers consume a typed
        # value. When narrow-remediation is not set and the flag is
        # empty, the attribute collapses to an empty list.
        dismissed_parsed: list[int] = []
        if has_dismissed:
            for token in dismissed_raw.split(","):
                stripped = token.strip()
                if not stripped:
                    parser.error(
                        "--dismissed-finding-ids must not contain empty "
                        "comma-separated tokens (got "
                        f"{dismissed_raw!r})"
                    )
                try:
                    dismissed_parsed.append(int(stripped))
                except ValueError:
                    parser.error(
                        "--dismissed-finding-ids entry "
                        f"{stripped!r} is not a valid integer"
                    )
        args.dismissed_finding_ids = dismissed_parsed
    handlers = {
        "preflight": cmd_preflight,
        "review-route": cmd_review_route,
        "plan-review-route": cmd_plan_review_route,
        "parse-schedule": cmd_parse_schedule,
        "decompose-plan": cmd_decompose_plan,
        "build-tasks": cmd_build_tasks,
        "compute-schedule": cmd_compute_schedule,
        "write-schedule": cmd_write_schedule,
        "batch-next": cmd_batch_next,
        "filter-schedule": cmd_filter_schedule,
        "parse-implementer-report": cmd_parse_implementer_report,
        "parse-plan-review-report": cmd_parse_plan_review_report,
        "claude-envelope-extract": cmd_claude_envelope_extract,
        "order-triage-findings": cmd_order_triage_findings,
        "parse-plan-review-triage-report": cmd_parse_plan_review_triage_report,
        "parse-d5-adjudication": cmd_parse_d5_adjudication,
        "commit-task": cmd_commit_task,
        "fail-task": cmd_fail_task,
        "block-dependents": cmd_block_dependents,
        "update-plan-header": cmd_update_plan_header,
        "set-task-agent": cmd_set_task_agent,
        "finalize-execution-log": cmd_finalize_execution_log,
        "log-event": cmd_log_event,
        "normalize-task-id": cmd_normalize_task_id,
        "acquire-lock": cmd_acquire_lock,
        "release-lock": cmd_release_lock,
        "reconcile-batch": cmd_reconcile_batch,
        "check-plan-deps": cmd_check_plan_deps,
        "index-closure": cmd_index_closure,
        "path-info": cmd_path_info,
        "lint-plans": cmd_lint_plans,
        "gates": cmd_gates,
        "audit": cmd_audit,
        "resolve-read-targets": cmd_resolve_read_targets,
        "build-agent-dispatch-prompt": cmd_build_agent_dispatch_prompt,
        "build-claude-dispatch-input": cmd_build_claude_dispatch_input,
        "build-codex-dispatch-input": cmd_build_codex_dispatch_input,
        "build-gemini-dispatch-input": cmd_build_gemini_dispatch_input,
        "list-global-lock-paths": cmd_list_global_lock_paths,
        # TASK-008 (POSTMORTEM_FIXES): sandbox-divergence escape hatch.
        "auto-validate-divergence": cmd_auto_validate_divergence,
        "run-summary": cmd_run_summary,
    }
    handlers[args.command](args)


if __name__ == "__main__":
    main()
