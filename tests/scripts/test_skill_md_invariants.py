"""Fence tests pinning the post-TASK-004 SKILL.md invariants.

After TASK-002 + TASK-003 proved the `compute-schedule --stdin` re-pipe in
Phase 1 step 3 is a no-op, TASK-004 deleted that re-pipe from the
orchestrator prose. These tests fail loudly if a future skill rewrite
re-introduces the recompute pipe in Phase 1 step 3, or re-introduces the
`build-tasks → ... → compute-schedule → ... → write-schedule` chain
anywhere in SKILL.md / dispatch-templates.md. The plan-author auto-revise
path is structurally invitation-prone for re-introduction; cheap insurance.

The companion test pins the new descriptor line in
`plan_ops_cheatsheet.md` so the cheatsheet stays in sync with the new
contract (compute-schedule remains a standalone CLI helper but is no
longer invoked by /implement-plan Phase 1).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" /
    "SKILL.md"
)
DISPATCH_TEMPLATES_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" /
    "dispatch-templates.md"
)
CHEATSHEET_PATH = (
    REPO_ROOT / "plugins" / "plan-executor" / "skills" / "implement-plan" /
    "plan_ops_cheatsheet.md"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _phase1_step3_region(skill_text: str) -> str:
    """Slice SKILL.md by `### ` headers and return the chunk that begins
    with the Phase 1 'Step 3 ' heading.

    Per TASK-004 implementation notes: don't grep the whole file (line 80
    CLI table legitimately mentions `compute-schedule`). Splitting on
    `^### ` headers and locating the chunk starting with 'Step 3 ' is the
    contract.
    """
    chunks = re.split(r"^### ", skill_text, flags=re.MULTILINE)
    matches = [c for c in chunks if c.lstrip().startswith("Step 3 ")]
    assert matches, (
        "SKILL.md does not contain a `### Step 3 ` header — the fence "
        "test cannot locate the Phase 1 step 3 region. Either the heading "
        "drifted or the section was renamed in a way that breaks slicing."
    )
    assert len(matches) == 1, (
        "SKILL.md contains more than one `### Step 3 ` header; the fence "
        "test's region-slice is ambiguous. Disambiguate the headings."
    )
    return matches[0]


def test_skill_md_no_compute_schedule_pipe_in_phase1_step3() -> None:
    """SKILL.md Phase 1 step 3 must NOT contain `compute-schedule --stdin`.

    TASK-004 deleted the no-op recompute pipe from BOTH the default branch
    and the `--task-ids` filter branch. Re-introducing it would silently
    re-run a provably idempotent batcher and (more dangerously) re-import
    the cmd_filter_schedule stale-file_locks bug noted as a follow-up in
    TASK-004's Verification section.
    """
    region = _phase1_step3_region(_read(SKILL_PATH))
    assert "compute-schedule --stdin" not in region, (
        "SKILL.md Phase 1 step 3 contains a `compute-schedule --stdin` "
        "invocation. TASK-004 removed this no-op recompute pipe — see "
        "docs/plans/PLAN_TOPO_RESPECT_FIX_2026-04-25/"
        "TASK-004_drop_redundant_skill_recompute.md for context."
    )


def test_skill_md_phase1_step3_no_compute_schedule_invocation_pattern() -> None:
    """Defensive: no flag-combination of `compute-schedule` may appear as
    an invocation in Phase 1 step 3.

    Catches future drift where a contributor re-introduces the recompute
    via a different flag (e.g. `compute-schedule --strict --json` instead
    of `--stdin`). Matches `compute-schedule\\s+--` — i.e. the subcommand
    name followed by any CLI flag.
    """
    region = _phase1_step3_region(_read(SKILL_PATH))
    matches = re.findall(r"compute-schedule\s+--", region)
    assert not matches, (
        "SKILL.md Phase 1 step 3 contains a `compute-schedule --<flag>` "
        f"invocation pattern (matched: {matches!r}). The recompute pipe "
        "was removed by TASK-004 and must not be re-introduced under any "
        "flag combination."
    )


def test_skill_md_phase1_overview_does_not_chain_compute_schedule() -> None:
    """No line in SKILL.md may describe the Phase 1 sequence as
    `build-tasks → ... → compute-schedule → ... → write-schedule`.

    This catches the descriptive prose drift across SKILL.md (Phase 1
    protocol overview, routing tables, re-source-verdict rule, etc.).
    The single explainer at SKILL.md ~line 275 that mentions
    `compute-schedule` as a comparison batcher is allowed because it is
    NOT shaped as a chain (no arrows wrapping it between `build-tasks`
    and `write-schedule`).
    """
    text = _read(SKILL_PATH)
    # Match arrow-chain references only — `→` (U+2192) or `->` between
    # build-tasks ... compute-schedule ... write-schedule on a single line.
    chain_re = re.compile(
        r"build-tasks[^\n]*(?:→|->)[^\n]*compute-schedule[^\n]*"
        r"(?:→|->)[^\n]*write-schedule",
    )
    offending = chain_re.findall(text)
    assert not offending, (
        "SKILL.md contains a `build-tasks → ... → compute-schedule → ... "
        f"→ write-schedule` chain reference. Offending lines: {offending!r}. "
        "TASK-004 collapsed the chain to `build-tasks → ... → "
        "write-schedule`; update prose to match."
    )


def test_dispatch_templates_no_compute_schedule_in_phase1_rerun() -> None:
    """`dispatch-templates.md` must not chain `compute-schedule` into the
    Phase 1 re-run prose at lines ~89 and ~197.

    TASK-004 edited those two lines to drop `compute-schedule` from the
    Phase 1 re-run sequence. Same arrow-chain regex as the SKILL.md
    fence — the classifier-prompt mention at line ~19 is a single-token
    reference (no arrow chain) and is intentionally left intact.
    """
    text = _read(DISPATCH_TEMPLATES_PATH)
    chain_re = re.compile(
        r"build-tasks[^\n]*(?:→|->)[^\n]*compute-schedule[^\n]*"
        r"(?:→|->)[^\n]*write-schedule",
    )
    offending = chain_re.findall(text)
    assert not offending, (
        "dispatch-templates.md contains a `build-tasks → ... → "
        "compute-schedule → ... → write-schedule` chain reference. "
        f"Offending lines: {offending!r}. TASK-004 removed the "
        "compute-schedule step from the Phase 1 re-run prose."
    )


def test_plan_ops_cheatsheet_pins_compute_schedule_standalone_note() -> None:
    """`plan_ops_cheatsheet.md` must carry the TASK-004 descriptor line
    clarifying that `compute-schedule` is a standalone helper and no
    longer part of `/implement-plan` Phase 1.
    """
    text = _read(CHEATSHEET_PATH)
    expected = (
        "Standalone helper for direct callers; not part of "
        "`/implement-plan` Phase 1 anymore."
    )
    assert expected in text, (
        f"plan_ops_cheatsheet.md is missing the TASK-004 descriptor "
        f"line: {expected!r}. The cheatsheet must mark compute-schedule "
        "as a standalone helper that is not invoked by /implement-plan "
        "Phase 1."
    )
