#!/usr/bin/env python3
"""Wrapper-side envelope sanitizer (Phase D TASK-003).

Implements §3.3 layers 2 (content sanitization) and 3 (known-shape redaction
+ flagging) of the Codex wrapper envelope perimeter.

Design intent
-------------
Untrusted free-text inside Codex envelopes (``findings[].message`` /
``findings[].issue`` / ``findings[].suggested_fix``, ``summary``,
``diff_summary``) is the highest-risk surface for prompt injection because
the orchestrator LLM reads these strings verbatim in the next turn. This
module is the perimeter that strips known-injection markup BEFORE the
envelope is emitted to the orchestrator. The pre-redaction payload is
recorded once to the run-log (``sanitizer_redaction`` event with a
sha256 fingerprint) for audit and never re-emitted downstream.

Public surface
--------------
``sanitize(envelope: dict, *, run_log_path: pathlib.Path | None = None,
           length_cap: int = 8000) -> tuple[dict, list[str]]``

Returns ``(sanitized_envelope, sanitizer_flag_shapes)``. The returned
envelope carries an ``extra`` dict (created or extended) with:

- ``extra.sanitizer_flags`` — list[{shape, field, count}]
- ``extra.truncated_fields`` — list[str] (paths of fields that were capped)
- ``extra.dropped_bytes`` — int (carried through if caller already populated)

The function NEVER mutates the input envelope; it deep-copies first.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_LENGTH_CAP = 8000
TRUNCATION_MARKER = " …[truncated]"  # " …[truncated]"

# Free-text field paths to scan inside the envelope's ``parsed`` dict.
# Each path is a tuple of (top-level-key | findings-array-key, field).
# We sanitize:
#   parsed.summary
#   parsed.diff_summary
#   parsed.findings[].message  (plan canonical)
#   parsed.findings[].issue           (codex_review_schema actual field)
#   parsed.findings[].suggested_fix   (codex_review_schema actual field)
SCALAR_FIELDS = ("summary", "diff_summary")
FINDING_FIELDS = ("message", "issue", "suggested_fix")

# ---------------------------------------------------------------------------
# Layer 2: content sanitization regexes
# ---------------------------------------------------------------------------

# Triple-backtick code fences (open or close). Strip the fence line itself,
# preserving the inner content lines.
_CODE_FENCE_LINE_RE = re.compile(r"^[ \t]*```[^\n]*$", re.MULTILINE)

# ATX-style markdown headers at line start (1-6 #'s + space).
_ATX_HEADER_RE = re.compile(r"^[ \t]*#{1,6}[ \t].*$", re.MULTILINE)

# Leading '>' blockquote markers at line start. Strip the marker, keep text.
_BLOCKQUOTE_RE = re.compile(r"^[ \t]*>[ \t]?", re.MULTILINE)

# ---------------------------------------------------------------------------
# Layer 3: known-shape redaction
# ---------------------------------------------------------------------------

# Each entry: (shape_label, compiled_regex). Order matters only for
# overlapping shapes; we use non-overlapping shapes here.
_REDACTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # <system>...</system>, <user>...</user>, <assistant>...</assistant>
    # (case-insensitive; greedy-but-bounded to a single tag pair).
    (
        "role_tag",
        re.compile(
            r"<(system|user|assistant)\b[^>]*>.*?</\1>",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # Bare opening role tags (unmatched) — also redact to deny smuggling
    # via "<system>" + later trailing text.
    (
        "role_tag_open",
        re.compile(r"<(system|user|assistant)\b[^>]*>", re.IGNORECASE),
    ),
    # <tool_calls>...</tool_calls> and <function_calls>...</function_calls>
    (
        "tool_calls_block",
        re.compile(
            r"<(tool_calls|function_calls)\b[^>]*>.*?</\1>",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    # Bare opening forms.
    (
        "tool_calls_open",
        re.compile(
            r"<(tool_calls|function_calls)\b[^>]*>",
            re.IGNORECASE,
        ),
    ),
    # JSON-looking "tool":"..." payloads (also "name":"..." inside what
    # looks like a tool call). Match the key/value pair, not the whole
    # JSON, because we don't know its bounds.
    (
        "json_tool_key",
        re.compile(
            r'"tool"\s*:\s*"[^"]+"',
        ),
    ),
    # Literal "Ignore (previous|prior|above) instructions" (case-insensitive).
    (
        "ignore_previous_instructions",
        re.compile(
            r"Ignore\s+(previous|prior|above)\s+instructions",
            re.IGNORECASE,
        ),
    ),
]


def _redaction_marker(shape: str) -> str:
    return f"[redacted:{shape}]"


# ---------------------------------------------------------------------------
# Run-log emit
# ---------------------------------------------------------------------------


def _resolve_run_log_path(run_log_path: Path | None) -> Path | None:
    """Pick the run-log destination.

    Priority: explicit arg > ``PLAN_EXECUTOR_RUN_LOG`` env var > package
    default (``docs/plans/_run_log.jsonl`` relative to the repo root, which
    we infer from the script location).
    """
    if run_log_path is not None:
        return Path(run_log_path)
    env = os.environ.get("PLAN_EXECUTOR_RUN_LOG")
    if env:
        return Path(env)
    # Default: walk up from this file to a ``docs/plans`` sibling.
    here = Path(__file__).resolve()
    # plugins/plan-executor/scripts/_codex_envelope_sanitizer.py
    # -> repo root is parents[3]
    try:
        repo_root = here.parents[3]
    except IndexError:
        return None
    candidate = repo_root / "docs" / "plans" / "_run_log.jsonl"
    return candidate


def _emit_run_log_event(
    event: dict,
    run_log_path: Path | None,
) -> None:
    """Append a single JSONL event to the run log.

    Best-effort: failures (missing dir, permission error) are swallowed —
    the wrapper must not crash on audit-log unavailability.
    """
    if run_log_path is None:
        return
    try:
        run_log_path.parent.mkdir(parents=True, exist_ok=True)
        with run_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")
    except (OSError, PermissionError):
        return


# ---------------------------------------------------------------------------
# Core sanitization
# ---------------------------------------------------------------------------


def _strip_layer2(text: str) -> str:
    """Strip code fences, ATX headers, leading '>' blockquotes."""
    if not text:
        return text
    text = _CODE_FENCE_LINE_RE.sub("", text)
    text = _ATX_HEADER_RE.sub("", text)
    text = _BLOCKQUOTE_RE.sub("", text)
    return text


def _redact_layer3(
    text: str,
    field_path: str,
    flags: list[dict],
    run_log_events: list[dict],
) -> str:
    """Replace known-injection shapes with markers; flag and log each match."""
    if not text:
        return text
    for shape, pattern in _REDACTION_PATTERNS:
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        count = len(matches)
        # Log pre-redaction payload sha256 for each match (one event per
        # match — the AC says "for each redacted match").
        for m in matches:
            payload = m.group(0)
            sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            run_log_events.append({
                "event": "sanitizer_redaction",
                "shape": shape,
                "field": field_path,
                "sha256": sha,
                "ts": time.time(),
            })
        text = pattern.sub(_redaction_marker(shape), text)
        flags.append({"shape": shape, "field": field_path, "count": count})
    return text


def _cap_length(
    text: str,
    field_path: str,
    cap: int,
    truncated_fields: list[str],
) -> str:
    """Length-cap free-text; record truncation."""
    if text is None:
        return text
    if len(text) <= cap:
        return text
    truncated_fields.append(field_path)
    # Reserve room for the marker so the final string stays <= cap +
    # marker length. The AC requires the marker be appended.
    head = text[:cap]
    return head + TRUNCATION_MARKER


def _process_field(
    text: str,
    field_path: str,
    cap: int,
    flags: list[dict],
    truncated_fields: list[str],
    run_log_events: list[dict],
) -> str:
    """Apply layer 2 (strip markup), layer 3 (redact), then length-cap.

    Order matters: strip + redact first so the cap measures
    post-sanitization length and doesn't truncate inside a redaction
    marker.
    """
    if not isinstance(text, str):
        return text
    text = _strip_layer2(text)
    text = _redact_layer3(text, field_path, flags, run_log_events)
    text = _cap_length(text, field_path, cap, truncated_fields)
    return text


def sanitize(
    envelope: dict,
    *,
    run_log_path: Path | None = None,
    length_cap: int = DEFAULT_LENGTH_CAP,
) -> tuple[dict, list[str]]:
    """Sanitize a Codex wrapper envelope.

    Returns ``(sanitized_envelope, flagged_shapes)`` where
    ``flagged_shapes`` is the deduplicated list of shape labels recorded
    in ``extra.sanitizer_flags`` (convenience for the caller's logging).

    The input envelope is not mutated.
    """
    sanitized = copy.deepcopy(envelope)
    flags: list[dict] = []
    truncated_fields: list[str] = []
    run_log_events: list[dict] = []

    parsed = sanitized.get("parsed")
    if isinstance(parsed, dict):
        # Top-level scalar fields.
        for key in SCALAR_FIELDS:
            if key in parsed and isinstance(parsed[key], str):
                parsed[key] = _process_field(
                    parsed[key],
                    f"parsed.{key}",
                    length_cap,
                    flags,
                    truncated_fields,
                    run_log_events,
                )
        # Findings array: each item may have message / issue / suggested_fix.
        findings = parsed.get("findings")
        if isinstance(findings, list):
            for idx, item in enumerate(findings):
                if not isinstance(item, dict):
                    continue
                for key in FINDING_FIELDS:
                    if key in item and isinstance(item[key], str):
                        item[key] = _process_field(
                            item[key],
                            f"parsed.findings[{idx}].{key}",
                            length_cap,
                            flags,
                            truncated_fields,
                            run_log_events,
                        )

    # Stamp results into envelope.extra. ``extra`` is the wrapper's
    # convention for non-schema metadata (see make_envelope's ``extra``
    # kwarg in plan_codex_dispatch.py); we mirror that key.
    extra = sanitized.setdefault("extra", {})
    if not isinstance(extra, dict):
        # Defensive: caller passed a non-dict ``extra``; replace.
        extra = {}
        sanitized["extra"] = extra
    if flags:
        extra.setdefault("sanitizer_flags", []).extend(flags)
    if truncated_fields:
        extra.setdefault("truncated_fields", []).extend(truncated_fields)

    # Emit run-log events (one per redacted match).
    resolved_log = _resolve_run_log_path(run_log_path)
    for ev in run_log_events:
        _emit_run_log_event(ev, resolved_log)

    flagged_shapes = sorted({f["shape"] for f in flags})
    return sanitized, flagged_shapes


# ---------------------------------------------------------------------------
# Layer 1 helper: strict outer-boundary trimming
# ---------------------------------------------------------------------------


def trim_stdout_to_envelope(stdout_text: str) -> tuple[str, int]:
    """Strip bytes outside the JSON envelope on Codex stdout.

    Codex's ``--json`` mode emits JSONL events; the wrapper already parses
    those line-by-line via ``invoke_codex``. The "envelope" the orchestrator
    sees is the file written by ``--output-schema -o <path>`` (a single
    JSON document). Layer 1 here is a defensive helper for callers that
    receive a stream that mixes JSON with extraneous bytes (e.g., banner
    text printed before the structured stream): keep only the JSONL lines
    that parse as JSON, drop the rest, and return the dropped byte count
    so the caller can stamp ``extra.dropped_bytes``.
    """
    if not stdout_text:
        return "", 0
    kept_lines: list[str] = []
    dropped = 0
    for line in stdout_text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            # Preserve blank lines structurally; they cost no risk.
            kept_lines.append(line)
            continue
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            dropped += len(line.encode("utf-8"))
            continue
        kept_lines.append(line)
    return "".join(kept_lines), dropped


__all__ = [
    "sanitize",
    "trim_stdout_to_envelope",
    "DEFAULT_LENGTH_CAP",
    "TRUNCATION_MARKER",
]
