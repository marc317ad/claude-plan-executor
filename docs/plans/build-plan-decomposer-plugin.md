# Build `plan-decomposer` Plugin

**Created:** 2026-04-17
**Author:** Claude (plan), reviewed by Codex (passes 1–7 folded in; post-pass-7 concision addendum applied per user directive without re-review — additive enforcement only, no changes to canonical contract, DAG semantics, cross-plugin status, or recovery protocol)
**Target repo:** `/mnt/d/claude-plan-executor/`
**First test case:** `~/.claude/plans/we-need-to-make-compressed-pinwheel.md`
**Output exemplar (frozen schema):** `/mnt/d/claude-plan-executor/docs/plans/DUAL_AGENT_Plans/`
**Agent pattern source:** `/mnt/d/algorithmic_trading_system/.claude/agents/fix-plan-decomposer.md`

---

## Context

`/implement-plan` (the `plan-executor` plugin) consumes ONE plan document containing `### TASK-NNN:` blocks. Many real plans — e.g. the 339-line pinwheel plan to extract `/fix-bugs` into a `bug-fixer` plugin — are multi-phase freeform documents (phases A–E, 20 numbered steps, file-migration matrices, test matrices, verification steps) that cannot be executed as a single batch. Today there is no mechanical path from freeform plan → executor-consumable per-task files.

The `DUAL_AGENT_Plans/` directory was built by hand to solve this for the executor's own hardening. It demonstrates the target shape: `00_INDEX.md` (human navigation), `00_INDEX.json` (frozen machine schedule), and `TASK-NNN_<slug>.md` files each self-contained and executor-ready. The convention says "a chunk can be handed to `/implement-plan` directly" — i.e. each TASK file IS a valid mini-plan.

`plan-decomposer` generalizes that pattern into a plugin, with the `fix-plan-decomposer` agent as the workflow template (fingerprint dedup, cycle detection, manifest-backed stable IDs, re-run field merge, end-of-run report).

### Terminology

- **slug** — a filesystem-safe short identifier derived from a plan's name: lowercase ASCII, hyphens instead of spaces/punctuation, no file extension. Example: pinwheel plan's filename `we-need-to-make-compressed-pinwheel.md` yields slug `we-need-to-make-compressed-pinwheel`. Used for output subdirectory names, TASK filename suffixes, and manifest tracking.
- **source plan** — the freeform markdown input (e.g. the pinwheel plan).
- **TASK file** — one of the per-task markdown files emitted by the decomposer.
- **supersede** — the operation of splitting one TASK into `N` sub-tasks when the original proves too large for a single implementer run. Sub-tasks get suffix IDs (`004A`, `004B`, `004C`, …).

## Goals

- One plugin at `/mnt/d/claude-plan-executor/plugins/plan-decomposer/` sibling to `plan-executor`.
- Bridge command `/decompose-plan` (un-namespaced, user-facing) → skill → agent.
- Stdlib-only `decomp_ops.py` CLI that mirrors `plan_ops.py` shape (argparse, `--json` everywhere).
- Output (in the claude-plan-executor repo during development; in any consumer repo at deploy time):
  - `docs/plans/<slug>.md` — **committed copy of the source plan** (colocated with other plans, mirrors `DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md` layout).
  - `docs/plans/decompose_plans_tasks/<slug>/{00_INDEX.md, 00_INDEX.json, _manifest.json, TASK-NNN_*.md}`.
- **Frozen contract:** `00_INDEX.md` structure and `00_INDEX.json` schema match `DUAL_AGENT_Plans/` byte-for-byte at the schema level. No deviations.
- **Downstream contract:** every `TASK-NNN_*.md` satisfies plan-analyst's required field set (Status, Priority, Files, Test command, Acceptance criteria, Description, Reversion guidance) and the `TASK-NNN[A-Z]?` ID regex.
- Topologically ordered so downstream tooling can parallelize non-dependent tasks.
- **Context-budget aware decomposition** — granularity is driven by estimated implementer+reviewer context cost per TASK, not by a fixed LOC threshold. Performance (correct, reviewable outputs) over speed (batch count).
- **Self-recursive supersede** — running `/decompose-plan` against an existing `TASK-NNN_*.md` (rather than a fresh source plan) triggers the supersede protocol automatically; no special flag needed.
- **Minimal plan-analyst enrichment-gap fix** — one-line frontmatter-aware rule in `plan-analyst.md` to suppress `missing-test-command` when `task_type: gate` is present. Backwards-compatible for legacy TASK files without the field.
- **Implementer-facing concision** — TASK documents are written for a subagent implementer with a clean context window, not for human education. Each TASK carries only the context required to make the edit: file list, acceptance criteria, and the minimum nuance needed to execute correctly. Narrative framing, historical background, and source-plan cross-references are omitted unless their absence would leave an acceptance criterion ambiguous. Reviewers — not the TASK body — carry the burden of intent validation.

## Non-Goals

- **No orchestrator-level changes to `/implement-plan`.** The sole exception is the surgical `plan-analyst` enrichment-gap update described in Phase A Step 6a (suppress `missing-test-command` when `task_type == "gate"` is present in frontmatter). This is additive, backwards-compatible, and scoped to one `plan-analyst.md` rule — not an orchestrator change.
- No multi-plan merging (one source plan → one subdirectory).
- No automatic dispatch of `/implement-plan` post-decomposition.
- No support for non-markdown source plans.
- No git-history-preserving migrations (fresh copy acceptable).
- No modification of the source plan by the decomposer. Humans edit the source plan to record deviations / issues between decomposer runs.

---

## Resolved Design Decisions (from user review 2026-04-17)

| Question | Resolution |
|---|---|
| Parent-plan reference | **Colocate.** Parent plan stored at `docs/plans/<slug>.md`, committed. TASK files reference it via relative path `../<slug>.md`. Mirrors `DUAL_AGENT_Plans/` layout exactly. |
| Slug derivation | Default = **source filename without extension** (deterministic, user-chosen when saving). Override via explicit `--output-slug <s>` when the filename is non-descriptive. H1-title slugification is not used as a fallback — too unstable across plan edits. |
| Granularity | **Complexity-driven, context-budget aware.** See Context Budget Rules below. Prefer correctness and reviewability over minimum task count. |
| Gate-step classification | **Always emit as Claude-tier TASKs** with `Test command: none`. Acceptance criteria describe the expected outcome of the manual action. No human-only appendix. |
| Supersede UX | **Input-path auto-detect.** Running `/decompose-plan <path/to/TASK-NNN_*.md>` triggers supersede mode: the input TASK becomes `Superseded`, gets sub-tasks with suffix IDs (`004A`, `004B`, …). Single-letter suffix only (no `004AA`). Humans update the TASK file with deviations / issues BEFORE re-running the decomposer — the richer content is what the decomposer uses to split the task. |
| Phase B target | **claude-plan-executor repo.** Development and shadow runs happen in `/mnt/d/claude-plan-executor`. No trading-repo involvement. |
| Parent-plan commit | **Yes, committed** at `docs/plans/<slug>.md`. Duplicated content is acceptable for self-contained review. |

---

## Target Plugin Layout

```
/mnt/d/claude-plan-executor/
├── .claude-plugin/marketplace.json              # ADD plan-decomposer entry
├── README.md                                    # ADD plan-decomposer section + /decompose vs /implement disambiguation
├── docs/plans/
│   ├── DUAL_AGENT_Plans/                        # existing exemplar (unchanged)
│   ├── build-plan-decomposer-plugin.md          # THIS PLAN
│   └── <slug>.md                                # NEW per decomposer run: copy of source plan
└── plugins/
    ├── plan-executor/                           # unchanged
    └── plan-decomposer/                         # NEW
        ├── .claude-plugin/plugin.json
        ├── agents/
        │   └── plan-decomposer.md               # modeled on fix-plan-decomposer
        ├── commands/
        │   ├── decompose-plan.md                # 2-line bridge to skill
        │   └── refresh.md                       # rsync cache updater, cloned from plan-executor
        ├── skills/decompose-plan/
        │   └── SKILL.md                         # orchestrator: config load, path-info, agent dispatch
        ├── scripts/
        │   └── decomp_ops.py                    # stdlib-only CLI
        ├── templates/
        │   ├── task-template.md.template        # per-TASK body (plan-analyst-compatible)
        │   ├── 00_INDEX.md.template             # frozen structure
        │   ├── 00_INDEX.json.template           # frozen schema
        │   └── plan-decomposer.json.template    # consumer config default
        └── docs/README.md
└── tests/scripts/
    └── test_decomp_ops.py                       # NEW unit suite
```

Consumer-side config (opt-in): `.claude/plan-decomposer.json` with resolution order env → cwd file → plugin default. Malformed JSON aborts loud.

```json
{
  "plan_dir": "docs/plans",
  "tasks_root": "docs/plans/decompose_plans_tasks",
  "base_branch": "main",
  "python_bin": "venv/bin/python"
}
```

## Repo Output Layout

```
<repo>/
├── .claude/plan-decomposer.json                 # optional consumer config
└── docs/plans/
    ├── <slug>.md                                # committed copy of source plan
    └── decompose_plans_tasks/
        └── <slug>/                              # one subdir per source plan
            ├── _manifest.json                   # next_id, fingerprints, slug, history, merge-reasons
            ├── 00_INDEX.md                      # frozen structure (see schema)
            ├── 00_INDEX.json                    # frozen schema (see schema)
            ├── TASK-001_<task_slug>.md
            ├── TASK-002_<task_slug>.md
            └── ...
```

Note: there is no `_parent_plan.md` inside the subdir. TASK files reference the colocated parent via `../<slug>.md` (see Canonical Contract).

---

## Canonical Contract (v1) — Freeze These First

Any drift across agent / script / template causes the same class of failure that bit `DUAL_AGENT_Plans` v3. Decide before writing code; encode each decision in exactly one place.

| Concept | Canonical | Notes |
|---|---|---|
| Task ID regex | `^\d{3}[A-Z]?$` | Matches plan-analyst's `TASK-NNN[A-Z]?` — single uppercase suffix reserved for supersede-split children. |
| Supersede depth | Single-letter suffix ONLY | `004` → `004A`, `004B`, … Never `004AA`. If a child proves too big, split its siblings (`004A`, `004B`, `004C`, `004D`, `004E`) rather than deepening. |
| Status vocabulary (decomposer / index) | `Pending \| In-Progress \| Done \| Failed \| Blocked \| Superseded \| Cancelled` | Title-case, matches `DUAL_AGENT_Plans/00_INDEX.json` exactly. `00_INDEX.json.chunks[].status` + frontmatter `status:` both use this vocabulary. |
| Status vocabulary (plan-executor runtime) | `open \| in-progress \| done \| failed \| skipped` | Lowercase. Used by `/implement-plan` + `plan_ops.py` when it rewrites the **Tasks** block status line inside a TASK body during commit / fail. This is a separate vocabulary operating on a separate field. |
| Status mapping (cross-plugin) | `Pending ↔ open`, `In-Progress ↔ in-progress`, `Done ↔ done`, `Failed ↔ failed`, `Blocked ↔ skipped`, `Superseded ↔ skipped`, `Cancelled ↔ skipped` | Consumers that need both views reference this table. See "Cross-Plugin Status Contract" section below. |
| Priority vocabulary | `critical \| high \| medium \| low` | Matches plan-analyst. |
| Filename pattern | `TASK-{NNN}_{task_slug}.md` | No date, no status, no group. `task_slug` derived from the TASK's title (agent-side slugification). |
| Status changes | Frontmatter-only + `00_INDEX.json.status` field | **No filename rename.** Simpler than fix-plan-decomposer. |
| Fingerprint input | `sha256(normalize(title) \| normalize(first_file_path) \| normalize(problem_prefix_300chars))` | Locked. Source-section moves, priority changes, and dependency edits do NOT re-identify. |
| Fingerprint input normalization | lowercase, strip `:line_range` from paths, collapse whitespace | Locked — changing this orphans existing TASKs. |
| Identity precedence on disagreement | TASK files on disk > manifest | Mirrors fix-plan-decomposer. Manifest is cache, not source of truth. |
| Parent-plan reference in TASK files | Relative `[../<slug>.md](../<slug>.md)` | Colocation matches DUAL_AGENT_Plans convention; survives moving the whole `docs/plans/` tree. |
| Schedule build phase | Single `build-schedule` call emits ONE canonical object consumed by all renderers | Prevents phase drift (Codex CRITICAL #3). |
| Write policy | All writes land in a staging dir; atomic rename via `commit-swap` (`os.rename` / `os.replace`) into place only after `validate-output` passes | No partial writes on cycle, validation failure, or IO error. Bash `mv` / `shutil.move` never used for production-path transitions. |
| Input mode detection | Realpath-based containment under `<tasks_root>` + strict filename regex + frontmatter validation + manifest match — see Input Mode Detection Rules below | Auto-detected; no explicit flag. |
| Parent-plan fingerprint tracking | `<tasks_root>/<slug>.journal.json.parent_fingerprint_post_swap: sha256:<hex>` | Recorded after each successful parent-plan rename. Stored in the journal file (NOT the tasks-dir manifest) so it survives the tasks-dir rename. |
| Parent-plan drift policy | No-op if production-parent fingerprint matches journal AND source fingerprint matches; overwrite if match AND source changed; abort with `parent-copy-divergent` if production-parent fingerprint differs from journal (unless `--force-parent`); abort with `parent-copy-untracked` if journal has no fingerprint AND production parent exists AND `F_prod != F_source` (unless `--force-parent`) | Prevents silent loss of human edits even on first-run / legacy journals. |
| Recovery protocol | Journal file at `<tasks_root>/<slug>.journal.json` (outside the swapped `<slug>/` dir) records a `committed_state` blob with phase + fingerprints after each step; re-runs reconcile against it; partial failures are recoverable because all output is regenerable from the source plan | See Recovery + Idempotency Protocol below. |
| Journal location | `<tasks_root>/<slug>.journal.json` — **outside** `<tasks_root>/<slug>/` | Must survive renaming the `<slug>/` dir. The `<slug>.journal.json` sibling path is on the same filesystem as the `<slug>/` dir (both under `<tasks_root>`), so `os.rename` atomicity holds for the journal's own updates. |
| Cross-directory atomic swap | Per-directory POSIX `rename` atomic operations, ordered parent-first then tasks-second, with journal entries between each step covering all 6 tasks-swap crash windows (W0–W5); full ACID is not guaranteed but full idempotent recovery is | See Recovery + Idempotency Protocol below. |
| TASK prose budget | Implementer-facing body prose (Scoped Context + Description + Implementation Playbook, excluding code fences) ≤ 200 tokens soft / 400 tokens hard per TASK. Gate TASKs ≤ 60 tokens hard. | Tokens counted via the decomposer's canonical tokenizer (stdlib shim — approximates tiktoken via whitespace + punctuation; exact function pinned in `decomp_ops.py`). Exceeding the hard ceiling fails `validate-output`. |
| Section conditionality | Sections may be omitted when their content would duplicate Acceptance criteria or Files list. Agent emits a `prose_omitted_*` reason code per omitted section. | See Concision Rules below. |
| Parent-plan reference conditionality | Inline body reference `**Parent plan:** [../<slug>.md](../<slug>.md)` emitted ONLY when a TASK cannot be understood from its own files + acceptance criteria (i.e., the TASK depends on a cross-cutting invariant defined in the parent). Frontmatter `source_plan` + `source_section` remain mandatory as machine-readable pointers. | Prevents bloat in the common case where the TASK is self-contained. The parent file still exists at `docs/plans/<slug>.md` for reviewers who need it. |
| Audience separation | Sections marked implementer-facing (minimal, direct) vs reviewer-facing (Acceptance criteria, Verification, Reversion guidance). Implementers only need the first; reviewers consume the second. | Encoded as comments in the template and enforced by `validate-output`. |

Encode this table in `decomp_ops.py` as module-level constants and cover with regression tests.

### Input Mode Detection Rules (addresses Codex pass-2 issue B and pass-3 issue Q3)

The decomposer MUST apply the following checks in order. The mode classification is normative:

- **Supersede mode** iff ALL of checks 1–6 pass (check 7 is informational).
- **Fresh-decomposition mode** iff `realpath(input_path)` is OUTSIDE `realpath(<tasks_root>)`.
- **Hard abort** iff `realpath(input_path)` is INSIDE `realpath(<tasks_root>)` but any of checks 3–6 fail. The decomposer MUST NEVER fall back to fresh mode when the input lives inside `<tasks_root>`; doing so would allow the decomposer to write over its own output subtree.

Checks (each returns pass/fail + error code for abort):

1. **Canonicalize.** Compute `realpath(input_path)` and `realpath(<tasks_root>)` — resolve symlinks and `..` segments to absolute paths. Record both.
2. **Containment.** If `realpath(input_path)` is NOT a descendant of `realpath(<tasks_root>)`, proceed to Fresh-mode guard (see below). If it IS a descendant, continue to checks 3–6 and treat any failure as hard abort (error codes below).
3. **Filename regex.** The basename MUST match `^TASK-\d{3}[A-Z]?_[a-z0-9-]+\.md$` (case-sensitive). A file named `task-001_foo.md`, `TASK-1_foo.md`, `TASK-001_Foo.md`, or `TASK-001.md` all fail. Regular directories under `<tasks_root>` (e.g., a bare slug dir with no file component) also fail. Error code: `refuse-to-decompose-into-own-output` (descendant of tasks_root but not a valid TASK filename).
4. **Frontmatter valid.** `realpath(input_path)` MUST parse as YAML frontmatter containing all of: `task_id`, `content_fingerprint`, `source_plan`, `decomposed_at`. Error code: `invalid-task-frontmatter` (descendant + valid filename but malformed or missing required frontmatter).
5. **Frontmatter ↔ filename consistency.** The `task_id` in the frontmatter MUST equal the `NNN[A-Z]?` portion of the filename. Error code: `task-id-filename-drift`.
6. **Manifest record.** `<tasks_root>/<slug>/_manifest.json` MUST exist AND MUST contain a record for this `task_id`. Error code: `task-not-in-manifest`.
7. **Fingerprint advisory (informational).** The recorded manifest `content_fingerprint` SHOULD equal the file's frontmatter `content_fingerprint`. Mismatch does NOT fail the check — it is expected for supersede triggers where the user edits the body to add deviations. The agent MUST log a report line: `input TASK has drifted from manifest fingerprint; treating user edits as authoritative for split decisions`.

On checks 1–6 all passing, mode is **supersede**, slug is inherited from `realpath(input_path).parent.name`, and `--output-slug` is silently ignored.

**Fresh-mode guard.** When check 2 classifies input as OUTSIDE `<tasks_root>`, apply these additional guards before proceeding to fresh mode:

- If the source plan's basename matches the TASK filename regex `^TASK-\d{3}[A-Z]?_[a-z0-9-]+\.md$`, compute the would-be slug (basename-without-extension). If `<tasks_root>/<slug>/` already exists, abort with `refuse-task-shaped-slug` (user must supply `--output-slug` to override).
- If `--output-slug` IS supplied and resolves to an existing `<tasks_root>/<slug>/` directory, the fresh run will be treated as a re-run against that slug (merge mode). This is NOT supersede — fresh-mode re-runs operate on the whole plan, not on a single child TASK.

### Recovery + Idempotency Protocol (addresses Codex pass-2 issues A, F and pass-3 issues Q1, Q2)

Full ACID atomicity across two output directories (`docs/plans/<slug>.md` and `docs/plans/decompose_plans_tasks/<slug>/...`) is not achievable with POSIX `rename` alone. Instead, the plan relies on four guarantees:

1. **Idempotent recovery.** Every output file is deterministically regenerable from `(source_plan, journal)`. Any partial failure can be recovered by a re-run that reconciles the production state against the journal's `committed_state` record.
2. **Per-directory POSIX atomicity.** Each `os.rename` is individually atomic when source and destination are on the same filesystem. The decomposer MUST verify same-filesystem by checking `os.stat(...).st_dev` on `{staging_parent, production_parent, staging_tasks_dir, production_tasks_dir, journal_file}` and abort with `cross-device-rename-unsafe` if any differ.
3. **Journal is external to the tasks-dir swap.** The journal lives at `<tasks_root>/<slug>.journal.json` — a sibling of `<tasks_root>/<slug>/`, NOT inside it. This is critical: the `<slug>/` directory is the one being renamed during the tasks swap, so a journal inside it would become inaccessible during the rename window. The journal file updates via atomic `write-then-rename` against `<slug>.journal.json.tmp` → `<slug>.journal.json`.
4. **Manifest copy inside `<slug>/` remains for consumer tooling.** `_manifest.json` inside the tasks dir stores post-commit state consumers read (it is the "cache"); the **journal** at `<tasks_root>/<slug>.journal.json` is the recovery authority. On `committed` phase finalize, the journal and manifest agree; mid-swap they diverge and only the journal is trusted.

**Commit protocol (Step 13 detail):**

The protocol has 7 phases. Each phase writes to the journal before the work that phase describes (so a crash DURING the phase leaves the journal entry for the NEXT run to resume FROM).

1. **Pre-swap invariant check.**
   - If `production_parent` exists, compute its fingerprint `F_prod`. If not, `F_prod = None`.
   - If journal has `parent_fingerprint_post_swap: F_journal`:
     - `F_prod == F_journal`: production is consistent with prior run → safe to overwrite with new staged copy.
     - `F_prod != F_journal`: production has been hand-edited (divergence). Abort with `parent-copy-divergent` reporting all three fingerprints (journal / production / source). Suggest `--force-parent` OR manual reconciliation.
     - `F_prod == None` (journal says we committed once but production is now gone): abort with `parent-copy-missing` and suggest `--force-parent` to re-commit from staging.
   - If journal has NO `parent_fingerprint_post_swap` (first run OR legacy journal):
     - `F_prod == None`: no prior state; safe to proceed.
     - `F_prod != None` AND `F_prod == F_source`: production already matches what we would write; adopt (no-op rename, record fingerprint in journal).
     - `F_prod != None` AND `F_prod != F_source`: unknown provenance of production parent. Abort with `parent-copy-untracked` reporting both fingerprints. Suggest `--force-parent` (overwrite) OR manual reconciliation.
2. **Journal phase 1.** Write `journal.committed_state = {run_id, phase: "pre-parent-rename", source_fingerprint, prior_parent_fingerprint, planned_parent_fingerprint, planned_task_count, started_at}` via `write-then-rename`. `prior_parent_fingerprint` captures the production parent's fingerprint as observed at the start of this run (`F_prod` from the invariant check), or `null` if production parent was absent. This field is what makes W0 disambiguation watertight — without it, recovery cannot distinguish "prod currently holds the previous good parent" from "prod currently holds something else entirely."
3. **Atomic parent rename.** `os.rename(staging_parent, production_parent)`. No-op if `F_prod == F_staged` (same-content adoption). Always compare before renaming.
4. **Journal phase 2.** Write `journal.committed_state.phase = "post-parent-rename"`, `journal.committed_state.parent_fingerprint_post_swap = <computed F_prod_new>`.
5. **Tasks backup.** If `production_tasks_dir` exists, `os.rename(production_tasks_dir, production_tasks_dir + ".backup-<run_id>")`. If absent, skip.
6. **Journal phase 3.** Write `journal.committed_state.phase = "tasks-backup-created"`, `journal.committed_state.backup_path = <backup_path or null>`.
7. **Tasks swap.** `os.rename(staging_tasks_dir, production_tasks_dir)`.
8. **Journal phase 4.** Write `journal.committed_state.phase = "tasks-swap-committed"`, `journal.committed_state.task_fingerprints = {...}`.
9. **Backup cleanup.** If backup exists, `shutil.rmtree(backup_path)`.
10. **Journal phase 5.** Write `journal.committed_state.phase = "backup-deleted"`.
11. **Final commit.** Write `journal.committed_state.phase = "committed"`, `journal.committed_state.completed_at = <now>`. Also write `_manifest.json` inside the tasks dir with the same post-commit state (cache for consumers).

**The 6 tasks-swap crash windows** (between journal phase 1 → committed):

| Window | Last journal phase on disk | What exists on filesystem | Recovery path |
|---|---|---|---|
| W0 (post-rename): parent rename succeeded, journal phase-2 not yet written | `pre-parent-rename` | `staging_parent` = ABSENT; production_parent = NEW; staging_tasks_dir = present | `staging_parent ABSENT AND F_prod == planned_parent_fingerprint` → write journal phase-2 (`post-parent-rename` + `parent_fingerprint_post_swap = F_prod`), then resume from step 5. |
| W0-pre (genuine pre-rename): phase-1 journal written but nothing else moved | `pre-parent-rename` | `staging_parent` = PRESENT (matches planned fingerprint); production_parent = OLD (matches `prior_parent_fingerprint`) or ABSENT (`prior_parent_fingerprint == null`); staging_tasks_dir = present | `staging_parent PRESENT AND F_prod == prior_parent_fingerprint (or both null)` → genuine pre-rename state; resume from step 3 (atomic parent rename). |
| W0-ambig (ambiguous pre-rename): partial tampering or lost work | `pre-parent-rename` | Any disk state NOT matching the above two rows | `staging_parent PRESENT AND F_prod != prior_parent_fingerprint AND F_prod != planned_parent_fingerprint`, OR `staging_parent ABSENT AND F_prod != planned_parent_fingerprint`, OR any other mixed state → abort `pre-rename-state-ambiguous` reporting the observed `{staging_parent_exists, F_prod, prior_parent_fingerprint, planned_parent_fingerprint}` tuple. Require human triage; `--force` does NOT bypass this (recovery-state corruption). |
| W1: after phase-2 write, before tasks backup | `post-parent-rename` | production_parent = new; production_tasks_dir = OLD (if existed); staging_tasks_dir = present | Re-stage tasks (re-render if staging lost), resume from step 5 (tasks backup). |
| W2: after tasks backup, before tasks swap | `tasks-backup-created` | production_parent = new; production_tasks_dir = ABSENT; backup = OLD tasks; staging_tasks_dir = present | Resume from step 7 (tasks swap). |
| W3: after tasks swap, before backup cleanup | `tasks-swap-committed` | production_parent = new; production_tasks_dir = NEW; backup = OLD tasks (leftover) | Verify current task fingerprints match journal's `task_fingerprints`; if match, resume from step 9 (backup cleanup). If mismatch, abort `tasks-hand-edit-during-recovery`. |
| W4: after backup cleanup, before final commit | `backup-deleted` | production_parent = new; production_tasks_dir = NEW; no backup | Finalize journal (step 11) + write cache manifest. |
| W5: journal written but manifest cache not | `committed` | All production artifacts present; journal says committed; cache manifest missing or stale | Regenerate cache manifest from journal + production state (pure function). |

**Recovery on re-run startup** (expanded for 6 windows):

- `journal.committed_state.phase` absent or journal file missing: normal run (initial or legacy).
- `== "pre-parent-rename"`: **disambiguate W0-post, W0-pre, and W0-ambig** by reading disk state directly. Compute `F_prod` (or `None` if absent) and check whether `staging_parent` exists. Compare against BOTH `journal.committed_state.planned_parent_fingerprint` (what we were about to write) AND `journal.committed_state.prior_parent_fingerprint` (what was there before we started this run):
  - `staging_parent` ABSENT AND `F_prod == planned_parent_fingerprint` → **W0-post recovery** (parent rename succeeded, phase-2 journal lost). Write phase-2 (`post-parent-rename` + `parent_fingerprint_post_swap = F_prod`), resume from step 5.
  - `staging_parent` PRESENT AND `F_prod == prior_parent_fingerprint` (or both `None`) → **W0-pre genuine pre-rename**. The phase-1 journal entry was written but nothing else has moved. Re-verify staging fingerprints match planned; if match, resume from step 3. If staging was partially corrupted, re-render the whole staging tree (same planned fingerprints) and resume.
  - Any other disk state → **W0-ambig**. Abort with `pre-rename-state-ambiguous`, reporting the observed `{staging_parent_exists, F_prod, prior_parent_fingerprint, planned_parent_fingerprint}` tuple. This includes: `staging_parent` ABSENT with `F_prod != planned` (mid-rename interference), `staging_parent` PRESENT with `F_prod` mismatching both prior and planned (hand-tampering), etc. Human triage required; no force flag bypasses this (recovery-state corruption, not a simple divergence).
- `== "post-parent-rename"` (W1): parent was committed but tasks backup not created yet. Verify `F_prod == parent_fingerprint_post_swap`. If match, re-render tasks staging if lost, resume from step 5. If mismatch, abort with `parent-hand-edit-during-recovery` (requires human triage).
- `== "tasks-backup-created"` (W2): backup exists, production dir is gone, staging may or may not be present. If staging fingerprints match planned, resume from step 7. If staging is gone, re-render tasks to a fresh staging dir (same planned fingerprints), then resume from step 7.
- `== "tasks-swap-committed"` (W3): production dir contains new tasks; backup still exists. Compute current production task fingerprints. If they match `journal.task_fingerprints`, resume from step 9 (delete backup). If mismatch, abort with `tasks-hand-edit-during-recovery`.
- `== "backup-deleted"` (W4): production state is correct; journal just needs finalization. Resume from step 11.
- `== "committed"` but cache manifest stale/missing (W5): regenerate cache manifest only. No staging rename needed.

**Example journal state at `phase: "committed"`** (post-successful-run):

```json
{
  "schema_version": 1,
  "slug": "we-need-to-make-compressed-pinwheel",
  "run_history": [
    {
      "committed_state": {
        "run_id": "2026-04-17T22:14:03Z-abc123",
        "phase": "committed",
        "source_fingerprint": "sha256:a1b2c3...",
        "prior_parent_fingerprint": null,
        "planned_parent_fingerprint": "sha256:a1b2c3...",
        "parent_fingerprint_post_swap": "sha256:a1b2c3...",
        "planned_task_count": 14,
        "task_fingerprints": {
          "001": "sha256:d4e5f6...",
          "002": "sha256:g7h8i9..."
        },
        "backup_path": null,
        "started_at": "2026-04-17T22:14:03Z",
        "completed_at": "2026-04-17T22:14:18Z"
      },
      "parent_override_at": null,
      "parent_override_reason": null,
      "pre_override_fingerprint": null
    }
  ]
}
```

All fields of `committed_state` are required at their respective phases:

- Phase 1 (`pre-parent-rename`): `run_id, phase, source_fingerprint, prior_parent_fingerprint, planned_parent_fingerprint, planned_task_count, started_at`.
- Phase 2 (`post-parent-rename`): adds `parent_fingerprint_post_swap`.
- Phase 3 (`tasks-backup-created`): adds `backup_path` (path to the backup dir, or `null` if production_tasks_dir was absent).
- Phase 4 (`tasks-swap-committed`): adds `task_fingerprints` dict.
- Phase 5 (`backup-deleted`): no new fields.
- Phase 6 (`committed`): adds `completed_at`.

`parent_override_*` fields live on the `run_history[]` entry object (NOT inside `committed_state`) so overrides are scoped to the specific run that applied them and are not cleared by subsequent successful runs.

On recovery, only the most recent `run_history[]` entry's `committed_state.phase` is consulted.

**Force flags:**

- `--force-parent` — bypass `parent-copy-divergent`, `parent-copy-missing`, OR `parent-copy-untracked` check. Overwrite production parent with staged copy. Records `parent_override_at: <timestamp>`, `parent_override_reason: <divergent|missing|untracked>`, `pre_override_fingerprint: <F_prod or null>`, `source_fingerprint: <F_source>` in the journal for audit.
- `--force` (existing) — overwrite on fingerprint match (TASK bodies). Does NOT bypass parent-copy checks; those require `--force-parent` specifically.
- Neither flag bypasses `parent-hand-edit-during-recovery` or `tasks-hand-edit-during-recovery`. Those are recovery-state corruption errors that require human diagnosis; the decomposer would rather abort than proceed with an unknown state.

### Cross-Plugin Status Contract (addresses Codex pass-3 issue Q8)

Two plugins now co-own status state for a single TASK file:

1. **`plan-decomposer`** writes to frontmatter `status:` and `00_INDEX.json.chunks[].status` using **title-case** vocabulary (`Pending / In-Progress / Done / Failed / Blocked / Superseded / Cancelled`). This is the schema inherited byte-for-byte from `DUAL_AGENT_Plans/`. It is the **plan lifecycle** view ("is this TASK decomposed? superseded? cancelled by design?").
2. **`/implement-plan`** writes to the `- **Status:** <status>` bullet inside the `## Tasks` block body (NOT frontmatter) using **lowercase** vocabulary (`open / in-progress / done / failed / skipped`). This is the **execution lifecycle** view ("has a runner actually completed this TASK?").

These are DIFFERENT FIELDS in the same file. The decomposer does not read or write the body Status bullet. The executor does not read or write the frontmatter `status:` or `00_INDEX.json`.

**Invariants:**

- On TASK file emission, decomposer sets:
  - Frontmatter `status: Pending` (title-case)
  - Body `- **Status:** open` (lowercase) — written via the task template so executor can take over cleanly.
- On `/implement-plan` commit, executor updates ONLY the body status bullet. Frontmatter and `00_INDEX.json` remain at `Pending` (lifecycle-wise still a pending plan artifact until humans promote to `Done`).
- On decomposer re-run, decomposer reads body status bullet ONLY to report a warning if frontmatter `Pending` disagrees with body `done` (meaning the TASK has been executed but not marked Done in the plan-level index). The decomposer does NOT auto-sync; the user gets a `sync-suggested` reason in the report and can optionally run `decomp_ops.py sync-status` to reconcile.

**`sync-status` subcommand (part of the 10-command surface, see CLI section):**

- Reads `body status` for every TASK; maps lowercase → title-case per the mapping table; updates frontmatter `status:` and `00_INDEX.json.chunks[].status`; writes via atomic `manifest commit`. Non-destructive — only promotes `Pending → Done` or `Pending → Failed` based on body state. Will NOT demote (never `Done → Pending`) without `--force`.
- Emits a manifest history entry: `{action: "sync-status", promoted: [...], demoted: [...], run_id: ..., at: ...}`.

**Prohibition:**

- Running `/decompose-plan` in supersede mode on a parent TASK whose body Status is `done` aborts with `supersede-after-execution` (the task has been executed; split retroactively is unsafe). Requires `--force-supersede` to override with a manifest audit entry.

**Why this is safe:**

- The two vocabularies operate on distinct fields; no same-field write races exist.
- Both plugins' invariants are independently verifiable (validate-output checks frontmatter + INDEX; plan-analyst checks body).
- `sync-status` is opt-in — nobody auto-mutates a TASK the other plugin is actively using.

### Consumer Compatibility Contract (addresses Codex pass-4 issue Q7)

Other plugins and tooling (`/fix-bugs`, `/decompose-fix-plan`, custom user scripts, future plugins) may want to read TASK files produced by `plan-decomposer`. This section declares which fields are **stable** (third parties may rely on them) vs **private** (internal to decomposer + executor; may change across versions).

**Stable fields (read-safe for any consumer):**

- Frontmatter `task_id` — canonical ID, regex `^\d{3}[A-Z]?$`.
- Frontmatter `source_plan` — relative basename of the parent plan.
- Frontmatter `source_section` — human-readable section pointer into the parent plan.
- Frontmatter `decomposed_at` — ISO date.
- Frontmatter `depends_on` — YAML list of `task_id` strings.
- Frontmatter `superseded_by` — YAML list of `task_id` strings (empty when not superseded).
- Frontmatter `task_type` — `standard | gate`. Backwards-compatible: absent → treat as `standard`.
- Body `## Tasks` block — plan-analyst-required fields (`Status`, `Priority`, `Files`, `Test command`, `Acceptance criteria`, `Description`, `Reversion guidance`).
- `00_INDEX.json` schema (version 1) — all fields declared in the template.

**Private fields (decomposer/executor-internal; do NOT rely on these):**

- Frontmatter `content_fingerprint` — fingerprint input is locked but hash algorithm is internal; treat as opaque.
- Frontmatter `change_history` — internal journal; format may evolve.
- Body `## Implementation Playbook`, `## Scoped Context`, `## Out of Scope` — content conventions, not stable contracts.
- `_manifest.json` — consumer cache; structure may change between minor versions.
- `<slug>.journal.json` — recovery authority; treat as decomposer-internal. Third parties MUST NOT write to it.
- Body `- **Status:**` bullet value — mutated by `/implement-plan`; semantics (lowercase `open`/`done`/etc.) belong to the executor contract, not the decomposer. Consumers should read frontmatter `status:` (title-case) for plan-lifecycle views and body bullet for execution-lifecycle views, per Cross-Plugin Status Contract.

**Non-breaking evolution policy:**

- Stable fields may gain new values (e.g., a new priority level) but never remove documented values without a major version bump to `schema_version` in `00_INDEX.json`.
- New frontmatter fields are added as optional (backwards-compatible defaults).
- Private-field changes do not trigger `schema_version` bumps.

If a third-party plugin needs a field this contract does not list, open an issue against the decomposer plugin rather than reverse-engineering internals.

### Context Budget Rules (granularity driver)

The decomposer sizes each TASK to fit within a single implementer run's context AND a single reviewer pass. Since implementers may be Claude or Codex (with different context budgets), target the smaller of the two. Complexity signals observed in the source plan drive the split decision:

**Hard ceilings (if any are exceeded, the TASK MUST be split):**

- > 5 files touched, OR
- > 250 lines of estimated net edits across all files, OR
- > 1 new module / package being created simultaneously with integration, OR
- Cross-cutting changes that would require the implementer to hold two independent mental models at once (e.g., `async` refactor + API contract change in one task).

**Soft signals (split if any two of these apply):**

- The task requires reading > 3 files before it can plan the first edit.
- The task involves routing / dispatch / cross-process wiring.
- The task creates a new public API surface (and nobody yet calls it — wire-up is separate).
- The task's verification requires stepping through more than one subsystem.

**Merge signals (OK to combine smaller steps into one TASK):**

- Trivial scaffold (`mkdir -p`, empty `__init__.py`, single constants file) paired with the edit that first uses it — combined stays under the hard ceilings.
- Two adjacent steps on the same file where splitting would force the reviewer to re-read the same hunk twice.
- A rename across N files where N ≤ 5 and each file has a single mechanical edit.

**Reason codes** (persisted to `_manifest.json.history[].merge_reasons` / `.split_reasons`):

- `split_ceiling_files`, `split_ceiling_loc`, `split_new_module`, `split_cross_cutting`, `split_async_boundary`, `split_api_surface`
- `merged_trivial_scaffold`, `merged_same_file`, `merged_mechanical_rename`

The agent MUST record the reason for every split/merge decision so future audits can re-evaluate. If none of the ceilings / signals apply, default to step-per-TASK (strict 1:1 with the source plan's numbered steps).

**Calibration note (addresses Codex pass-2 issue D):** the numeric ceilings (`5 files`, `250 LOC`, `>3 files to plan`) are initial heuristics seeded from `DUAL_AGENT_Plans`' observed task sizes and the pinwheel plan's step complexity. They are NOT proven optimal. Phase B shadow runs record each split/merge decision's reason code; Phase B exit criteria include a review of the distribution of split/merge events on the pinwheel plan and any subsequent real plans. If the ceilings over-split (many trivial adjacent splits) or under-split (a TASK that blows the implementer's context window), tune the constants in `decomp_ops.py` module-level and log the change with rationale. The calibration ceiling values are module-level constants (`CEILING_FILES`, `CEILING_LOC`, `CEILING_PLAN_FILES`, `CEILING_NEW_MODULES`) so tuning is a single-file edit with a regression test. Do not treat these numbers as part of the Canonical Contract — the DAG shape, field names, and status vocabulary are contractual; the split heuristics are tunable policy.

### Concision Rules (TASK prose minimization)

TASK documents are written for a subagent implementer with a clean context window — not for human education, not for onboarding, not to narrate the plan's history. The implementer needs a clear head to execute; reviewers carry intent validation. These rules apply at render time (Agent workflow step 11).

**Audience split (every section annotated):**

- **Implementer-facing (minimal):** Goal, Scoped Context (when present), Description, Implementation Playbook (when present).
- **Reviewer-facing (complete):** Acceptance criteria, Verification, Reversion guidance, frontmatter.

Implementers read section bodies top-to-bottom until they have enough to edit. Reviewer-facing sections exist to validate the implementer's diff after the fact; they may carry more detail without hurting execution.

**Prose budget (hard ceilings enforced by `validate-output`):**

- Standard TASK: implementer-facing body prose ≤ 400 tokens hard, ≤ 200 tokens soft. Exceeding hard fails validation; exceeding soft warns.
- Gate TASK (`task_type: gate`): implementer-facing body prose ≤ 60 tokens hard, ≤ 40 tokens soft.
- Code fences and YAML frontmatter are excluded from the count.
- Tokenizer: stdlib-only approximation pinned in `decomp_ops.py`. Exact algorithm is versioned; changes require a schema bump.

**Section conditionality (omit when redundant with Acceptance criteria + Files list):**

| Section | Omit when | Reason code |
|---|---|---|
| Scoped Context | The TASK is self-contained — its files and acceptance criteria fully define the work. Typical: a single-file edit, a rename, a config addition. | `prose_omitted_self_evident` |
| Implementation Playbook | The TASK is ≤ 3 mechanical edits OR a single contract-defined operation. Acceptance criteria already describes the end state; no step-by-step needed. | `prose_omitted_trivial_scope` |
| Implementation notes | Default. Only emit when the implementer needs a non-obvious warning (hidden constraint, known foot-gun, subtle invariant). | `prose_omitted_no_notes` |
| Out of Scope | The Files list and Acceptance criteria already bound the scope. | `prose_omitted_bounded_by_files` |
| Inline parent-plan reference | The TASK is self-contained. Default. Reference ONLY when the TASK depends on a cross-cutting invariant defined in the parent. | `prose_omitted_self_contained` |
| Whole body (gate TASK) | Gate TASKs render a slim body: Goal + Acceptance criteria only. No Scoped Context, no Playbook, no Out of Scope. | `prose_omitted_gate` |

**Merge signal for prose:** if two candidate sections contain overlapping content (Description ↔ Implementation Playbook, Scoped Context ↔ Description), merge into the tighter section and drop the other. Record `prose_merged_overlap` reason code.

**What the agent MUST NOT do:**

- Restate the Acceptance criteria in prose form ("This task accomplishes X" when the criterion says `File X exists with content Y`).
- Narrate the parent plan's history ("As part of the phased migration…").
- Explain what the implementer is about to do ("You will now create the file at…").
- Add "why" context unless absence would leave an acceptance criterion ambiguous. Reviewers carry "why"; implementers carry "what".
- Copy source-plan prose verbatim. Paraphrase only what is load-bearing for the edit.

**What the agent SHOULD do:**

- Use lists and bullet points; prefer pseudocode or file-path + line-anchor pointers over prose descriptions.
- State acceptance criteria in machine-verifiable terms where possible (`file X contains line Y`; `pytest tests/foo.py::test_bar passes`; `grep -c "pattern" file.py == 0`).
- Put nuance in the Acceptance criteria, not in the Description.
- When nuance cannot be machine-encoded (e.g., "behavior must preserve order-of-arrival semantics"), state it as a bullet point in Scoped Context — one sentence, not a paragraph.

**Reason-code persistence:**

Every omitted section records its reason code in `_manifest.json.history[].prose_decisions[]`. Audit reviews use this to detect regressions (if many TASKs stop recording `prose_omitted_self_evident`, the agent has drifted back toward verbosity).

**Supersede mode:**

Parent TASK body + user deviations inherited during supersede MUST be re-minimized through these rules before emission. The decomposer does not preserve parent prose verbatim — child TASKs are built fresh with concision rules applied. The parent's Acceptance criteria ARE preserved (semantic continuity), but surrounding narrative is not.

**Calibration note:** the 400/200/60 token ceilings are initial heuristics. Phase B measures actual implementer-facing token counts on the pinwheel plan; thresholds tune after first shadow run (same policy as Context Budget Rules).

---

## Agent Spec: `plan-decomposer.md`

Mirrors `fix-plan-decomposer.md` structure, adapted to the plan → task decomposition problem. Required tools: `Read, Write, Glob, Grep, Bash`. Model: `opus`.

### Inputs

- `input_path` — absolute path. Either a source plan (fresh decomposition) OR an existing `TASK-*.md` (supersede mode).
- `output_slug` (optional) — override slug. Only honored in fresh-decomposition mode; supersede mode inherits the slug from the parent directory.
- `tasks_root` (optional) — override the consumer `tasks_root` from config.
- `today` — `YYYY-MM-DD` for `decomposed_at`.
- Flags: `--dry-run`, `--force` (overwrite on fingerprint match).

### Input mode detection

Before starting the workflow, the agent inspects `input_path` per the **Input Mode Detection Rules** section above (7 strict checks). The stricter ordering is normative: any realpath that lands inside `<tasks_root>` but is NOT a fully-valid supersede target (filename regex pass + frontmatter present + manifest record present) MUST hard-abort — never fall back to fresh mode. Fresh mode is reserved for inputs whose realpath is OUTSIDE `<tasks_root>`.

### Workflow (fresh-decomposition mode; 14 steps, mirrors fix-plan-decomposer with Codex's ordering fix)

1. **Read + parse source plan.** Extract H1 title, header metadata (`Created:`, `Base branch:`, etc.), top-level sections (`## Goal`, `## Context`, `## Verification`, phases, tables, test matrices). Persist intermediate parsed tree as a JSON blob (never a free-form prose model).
2. **Load existing state FIRST** (Codex ordering fix). Read `_manifest.json` (or init if missing). Scan existing `TASK-*.md` files via Glob; read each frontmatter; build authoritative current-state map keyed by `task_id`. On manifest-vs-file disagreement, trust files and flag in the report.
3. **Normalize to Task Specs with complexity scoring.** Each spec = `{candidate_title, candidate_files, candidate_dependencies, candidate_test_command, candidate_acceptance, candidate_problem, candidate_fix, candidate_verification, candidate_reversion, source_section, priority, is_gate, complexity_score, split_decision, merge_decision, reason_codes}`. Priority inferred from phase priority hints in the source; defaults to `medium`. `is_gate=true` for operational steps (commands like `claude --plugin-dir ...`, `/plugin list`, manual verification) — emitted as claude-tier TASKs with `Test command: none`. Apply Context Budget Rules: compute complexity score per candidate, decide split/merge, record reason codes.
4. **Compute fingerprints** per Canonical Contract row.
5. **Reconcile existing vs new** (renamed from `match-existing` per Codex). Buckets: `matched` (fingerprint hit → preserve `task_id`), `new_pending` (no hit → needs ID), `orphaned` (manifest has entry but no file on disk → flag as warning, ignore for this run), `divergent` (file on disk but manifest missing → flag + adopt from file).
6. **Detect cycles** over the proposed dependency DAG (matched + new_pending unified ID space; new_pending indexed by candidate-order until Step 8 assigns real IDs). On cycle → abort with ring, no writes.
7. **Classify gates** — every `is_gate=true` candidate becomes a Claude-tier TASK with `Test command: none`. Acceptance criteria must describe the observable outcome ("`/plugin list` output contains `plan-decomposer`"; "commit SHA exists on `main`"; "file X contains string Y"). No demotion to human-only appendix.
8. **Topological sort + assign stable IDs** (renamed from `allocate-ids`). Kahn's algorithm over the full DAG. Priority tie-breakers: `critical` (0) → `high` (1) → `medium` (2) → `low` (3), then source-order index. Matched tasks keep existing IDs (stability); `new_pending` get `manifest.next_id++` in pop order. This guarantees new IDs respect topological order.
9. **(Supersede branch — skipped in fresh mode.)**
10. **Build canonical schedule object** — one `build-schedule` call emits the full intermediate `{tasks: [...], batches: [...], supersedes: [], new_parent_plan_copy: true}` (Codex's core anti-drift suggestion). All renderers consume THIS object.
11. **Render into staging dir** `<tasks_dir>/.staging-<run_id>/` with Concision Rules applied. Every TASK file, `00_INDEX.md`, `00_INDEX.json`, `_manifest.json` land here first. The parent-plan copy lands at `<plan_dir>/.staging-<run_id>/<slug>.md` (sibling staging location). For each TASK:
    - **(a) Section elision.** Evaluate each row of the Section Conditionality table. For every section whose omit-condition holds, drop it from the rendered body and append the corresponding `prose_omitted_*` reason code to `_manifest.json.history[-1].prose_decisions[]` (keyed by `task_id`).
    - **(b) Gate slim render.** If `task_type: gate`, render only Goal + Tasks + Verification (drop Scoped Context, Implementation Playbook, Out of Scope) and record `prose_omitted_gate`.
    - **(c) Overlap merge.** If Description and Implementation Playbook contain overlapping prose (common when both were paraphrased from the same source-plan step), merge into Description, drop Playbook, record `prose_merged_overlap`.
    - **(d) Parent-plan inline link.** Emit the inline `**Parent plan:**` line only when the TASK depends on a cross-cutting invariant defined in the parent plan (not inferable from its own Files + Acceptance criteria). Otherwise omit with `prose_omitted_self_contained`. Frontmatter `source_plan` always remains.
    - **(e) Token-budget check.** Count implementer-facing body tokens (code fences + YAML frontmatter excluded) via the tokenizer pinned in `decomp_ops.py`. If any TASK exceeds the hard ceiling (400 standard / 60 gate), abort the render with `prose-budget-exceeded` (report count + ceiling + TASK id), leave the staging dir for inspection, and do not proceed to step 12.
12. **Validate staging** via `validate-output --tasks-dir .staging-<run_id>`. Hard gate: required-field check on every TASK, ID regex, dependency resolution, no cycles, schema conformance for both INDEX files, parent-plan reference resolves. Any failure → abort, leave staging for inspection, do not touch the production output dirs.
13. **Atomic swap.** Invoke `decomp_ops.py commit-swap` with the staging paths, production paths, and journal file path. This one subcommand executes the full 7-phase cross-directory atomic swap protocol in-process (pre-swap invariant check → phase-1 journal → parent rename → phase-2 journal → tasks backup → phase-3 journal → tasks swap → phase-4 journal → backup cleanup → phase-5 journal → final commit). The agent NEVER hand-rolls `mv`, `shutil.move`, or equivalent Bash calls; all filesystem transitions route through `commit-swap`. On exit code 2 (recoverable-from-journal), the agent calls `decomp_ops.py recover` to resume. On exit code 3 (`parent-copy-divergent` / `parent-copy-missing` / `parent-copy-untracked`), the agent surfaces the error to the user and halts — these require explicit `--force-parent` confirmation, which the agent never silently applies.
14. **Emit end-of-run report** — tasks emitted / merged / superseded / gaps / warnings / next-action hint. Dry-run mode short-circuits at Step 11: render to staging, validate, emit report, then delete the staging dirs.

### Workflow (supersede mode)

Triggered when `input_path` is a TASK file. Steps 1–8 operate on the parent TASK's content (treating its body as a mini source plan: its `## Scoped Context` + `## Implementation Playbook` + any human-added "deviations / issues" sections become the input). Sub-task IDs are `{parent_id}A`, `{parent_id}B`, … (single letter; cap at depth 1).

Key differences from fresh mode:

- Slug inherited from parent subdirectory.
- Parent plan already committed at `<plan_dir>/<slug>.md`; skip the parent-copy step.
- Parent TASK becomes `Superseded` with `superseded_by: [<new_ids>]`, body preserved, frontmatter updated.
- Sub-tasks inherit parent's `depends_on` unless the parent's body specifies new deps per sub-task.
- **Preserve external references**: any other TASK whose `depends_on` includes the parent ID keeps that reference. The `Superseded` + `superseded_by` fields signal downstream tooling that "dep is satisfied when all superseded_by entries are Done."
- Parent's fingerprint is recomputed; if a fresh re-decomposition later produces the same parent fingerprint, the supersede history is preserved (matched bug path → no re-split).
- Staging + atomic swap covers the parent-TASK frontmatter update, all new sub-task files, and the INDEX regen — routed through `commit-swap` (same Python-in-process protocol as fresh mode). Supersede does not need a parent-plan rename, so `--skip-parent-rename` is passed to `commit-swap`; tasks-dir rename + journal phases still execute normally.
- Manifest `history[]` entry records the supersede: `{from: parent_id, to: [child_ids], reason: str, fingerprint_parent: str}`.
- If parent TASK is already `Superseded` OR has status `Done`/`Cancelled` → abort with `supersede-illegal-state`.

### Rules

- **Never execute tasks.** The decomposer only writes TASK files; `/implement-plan` executes them.
- **Never modify the source plan.** Read-only input. Humans edit the source plan between decomposer runs to record deviations; the decomposer re-reads the richer content next time.
- **Never modify a TASK file except in supersede mode or re-run merge.** Normal re-runs merge at the field level; supersede rewrites frontmatter + adds `superseded_by`.
- **Never delete TASK files.** Status changes via frontmatter + `00_INDEX.json` update, not deletion.
- **Never allocate an ID without manifest commit.** If the final atomic swap fails, roll back staging; manifest.next_id is only incremented during the atomic swap phase.
- **Never silently lose info.** Field-merge on re-run; preserve old titles/problems/etc. in `change_history`.
- **Never rewrite a downstream task's `depends_on` to point at superseded_by children.** The parent ID + `Superseded` status is the stable reference.
- **Trust TASK files on disagreement.** Manifest is cache.
- **Abort on cycle. Abort on validation failure.** No partial writes.
- **Cap supersede depth at 1.** Single-letter suffix. If a sub-task needs further splitting, redo the parent's split so siblings absorb the complexity.

---

## `decomp_ops.py` CLI Surface (10 subcommands)

All subcommands accept `--json` and exit non-zero on failure.

1. `inspect` — read-only multi-mode. Sub-modes via `--mode`:
   - `paths [--input-path <path>] [--output-slug <s>]` → derived-paths JSON (python_bin, plan_dir, tasks_root, tasks_dir, journal_file, manifest, index_md, index_json, parent_copy_path, staging_templates, input_mode ∈ {fresh, supersede}, abort_code if applicable).
   - `manifest --manifest-file <path>` → loaded manifest JSON (validates schema).
   - `journal --journal-file <path>` → loaded journal JSON (validates schema; reports `committed_state.phase`).
   - `tasks --tasks-dir <path> [--status-field <frontmatter|body>] [--status <s>]` → enumerate TASK files. `--status-field frontmatter` reads the title-case decomposer status; `body` reads the lowercase executor status bullet. Default = `frontmatter`.
2. `parse-source-plan --plan-file <path>` → intermediate tree JSON (headings, phases, step candidates, file tables, test matrices). Works on both source plans and TASK files (for supersede mode).
3. `build-schedule --parsed-file <path> --manifest-file <path> [--supersede-parent <task_id>]` → **canonical schedule object** (tasks, batches, supersedes, warnings, split_reasons). The single-source intermediate. All renderers consume this.
4. `render --schedule-file <path> --templates-dir <path> --output-dirs-json <json>` → writes TASK files + `00_INDEX.md` + `00_INDEX.json` + parent-plan copy to the specified output dirs. Emits the list of files written.
5. `validate-output --tasks-dir <path> [--plan-dir <path>]` → hard-gate linter. Checks: plan-analyst required fields on each TASK, ID regex, status vocabulary (frontmatter = title-case; body bullet = lowercase), priority vocabulary, `task_type ∈ {standard, gate}` on files that declare it, `00_INDEX.json` schema + chunk-to-file match, dependency resolution, no cycles, no dangling superseded_by, parent-plan reference resolves to `../<slug>.md` existing, **prose-budget** (implementer-facing body tokens ≤ 400 standard / ≤ 60 gate is a hard gate — non-zero exit with `prose-budget-exceeded` naming the TASK id, count, and ceiling; implementer-facing body tokens > 200 standard / > 40 gate emits a `prose-budget-soft` warning under `warnings[]` in `--json` output but does NOT fail validation), **prose-decision coverage** (every emitted TASK has at least one `prose_*` reason code recorded in `_manifest.json.history[].prose_decisions[]` — missing coverage fails validation, proving the agent applied the Concision Rules rather than silently rendering the maximal template). Exit non-zero on any violation.
6. `manifest --action <init|commit> --manifest-file <path> [--stdin]` → init bootstraps empty manifest; commit does atomic write-then-rename from stdin or merges a diff.
7. `compute-fingerprint --stdin` → pure function, SHA256 of canonical-normalized input.
8. `commit-swap --schedule-file <path> --staging-plan <path> --staging-tasks-dir <path> --production-plan <path> --production-tasks-dir <path> --journal-file <path> [--skip-parent-rename] [--force-parent] [--force]` → **executes the full cross-directory atomic swap + journal protocol in-process.** This is the ONLY subcommand that writes to production paths (outside of `manifest commit` for the post-swap cache). Implements all 7 protocol phases (pre-swap invariant check → journal phase 1 → parent rename → … → final commit). `--skip-parent-rename` is used by supersede mode (parent already committed from the original decomposition); it bypasses the parent-rename + parent-fingerprint steps while still routing tasks-dir swap through the journal. Encapsulates every filesystem race so the agent never has to hand-roll `mv` from Bash. Exit codes: `0` = success; `1` = validation or invariant failure; `2` = recoverable-from-journal (re-run needed); `3` = parent-copy-divergent / untracked / missing; `4` = cross-device-rename-unsafe. Prints the journal's final state on success.
9. `recover --journal-file <path> --production-plan <path> --production-tasks-dir <path> [--staging-dirs-json <json>]` → idempotent resume. Reads journal `committed_state.phase`, classifies the crash window (W0–W5), and re-runs the necessary protocol steps to reach `phase: "committed"`. If the journal reports `phase: "committed"` already, exits 0 with `{"noop": true}`. If recovery is ambiguous (e.g., `parent-hand-edit-during-recovery`, `pre-rename-state-ambiguous`, `tasks-hand-edit-during-recovery`), exits non-zero with a diagnostic report.
10. `sync-status --tasks-dir <path> [--json] [--force]` → cross-plugin status sync (lowercase body bullet → title-case frontmatter + `00_INDEX.json`). Non-destructive by default: promotes `Pending → Done` / `Pending → Failed` only. `--force` allows demotion. Records manifest history entry.

### Subcommand-to-Workflow mapping

| Agent step | Script call |
|---|---|
| Input mode detection | `inspect --mode paths --input-path <p>` — returns `input_mode` OR an `abort_code` |
| 1 | `parse-source-plan` (handles both fresh + supersede parents) |
| 2 | `inspect --mode manifest` + `inspect --mode journal` + `inspect --mode tasks` |
| 3 | (agent-side spec normalization + complexity scoring) |
| 4 | `compute-fingerprint` |
| 5–9 | (agent-side reconciliation + supersede branching) |
| 10 | `build-schedule` (pass `--supersede-parent` in supersede mode) |
| 11 | `render` |
| 12 | `validate-output` |
| 13 | `commit-swap` (handles parent rename + tasks rename + journal + post-commit manifest cache — all in one in-process call). Supersede mode passes `--skip-parent-rename`. **Agent never uses Bash `mv`.** |
| Recovery | `recover` (called on startup if journal phase != `committed`) |
| Post-exec sync | `sync-status` (optional, called after `/implement-plan` finishes to promote frontmatter/INDEX) |
| 14 | (agent-side report composition) |

---

## Templates

### `task-template.md.template` (per-TASK body; plan-analyst-compatible)

The template below is the **maximal shape**. The agent elides conditional sections at render time per the Concision Rules and records the corresponding `prose_omitted_*` reason code in `_manifest.json.history[-1].prose_decisions[]`. HTML comments annotate each section's audience (`implementer-facing` minimal / `reviewer-facing` complete) and conditionality.

```markdown
---
task_id: {NNN}
task_type: {task_type}
source_plan: {source_plan_basename}
source_section: {source_section}
decomposed_at: {today}
content_fingerprint: sha256:{hex}
depends_on: {depends_on_yaml_list}
superseded_by: {superseded_by_yaml_list}
change_history: {change_history_yaml_list}
---

# TASK-{NNN} — {title}

<!-- reviewer-facing header block. Inline **Parent plan:** link is CONDITIONAL (prose_omitted_self_contained) — emit only when TASK depends on a cross-cutting invariant defined in the parent. Frontmatter `source_plan` is the always-present machine-readable pointer. -->
**Parent plan:** [`../{slug}.md`](../{slug}.md)
**Source section:** {source_section}
**Base branch:** {base_branch}
**Chunk dependencies:** {depends_on_csv_or_none}

---

<!-- implementer-facing; one-line goal. Always present. -->
## Goal

{goal_paragraph}

<!-- implementer-facing; CONDITIONAL (prose_omitted_self_evident) — omit when Files + Acceptance criteria fully define the work. -->
## Scoped Context

{scoped_context}

<!-- reviewer-facing; always present. -->
## Verification

{verification_commands}

---

<!-- reviewer-facing; always present. Contains every plan-analyst-required field. -->
## Tasks

### TASK-{NNN}: {title}

- **Status:** {status}
- **Priority:** {priority}
- **Files:**
{files_bullets_with_annotations}
- **Dependencies:** {depends_on_csv_or_none}
- **Test command:** {test_command}
- **Acceptance criteria:**
{acceptance_bullets}

**Description:**
{description}

<!-- reviewer-facing; CONDITIONAL (prose_omitted_no_notes) — emit only for non-obvious warnings (hidden constraint, foot-gun, subtle invariant). Default omit. -->
**Implementation notes:**
{implementation_notes_or_none}

**Reversion guidance:**
{reversion_guidance}

---

<!-- implementer-facing; CONDITIONAL (prose_omitted_trivial_scope) — omit when TASK is ≤3 mechanical edits or a single contract-defined operation. Always omit when task_type: gate (prose_omitted_gate). -->
## Implementation Playbook

{playbook_steps}

---

<!-- implementer-facing; CONDITIONAL (prose_omitted_bounded_by_files) — omit when Files list + Acceptance criteria already bound the scope. Always omit when task_type: gate (prose_omitted_gate). -->
## Out of Scope

{out_of_scope_bullets}
```

Every required plan-analyst field is present: Status, Priority, Files, Test command, Acceptance criteria, Description, Reversion guidance. Implementation notes optional per plan-analyst schema.

**Gate-TASK slim render:** for `task_type: gate`, the agent emits only the frontmatter, title, the reviewer-facing header block (without the inline Parent-plan link unless cross-cutting), `## Goal`, `## Tasks` (with Acceptance criteria as machine-checkable bullets), and `## Verification`. `## Scoped Context`, `## Implementation Playbook`, and `## Out of Scope` are always omitted with `prose_omitted_gate`.

**Rendering note:** `validate-output` re-runs the Concision Rules token-budget check as a hard gate before `commit-swap`, so any drift between agent elision logic and the budget is caught before production paths mutate.

**`task_type` vocabulary (addresses Codex pass-2 issue E):**

- `standard` — ordinary implementer task (Codex or Claude). Test command required; plan-analyst treats `Test command: none` as a gap.
- `gate` — operational / verification step that has no programmable test command (e.g. `/plugin list` inspection, commit-SHA check, manual file-contents assertion). Always Claude-tier. `Test command: none` is expected and MUST NOT be reported by plan-analyst as `missing-test-command`. Acceptance criteria describe the observable outcome in machine-checkable terms where possible.

Downstream contract: plan-analyst (and other consumers) read `task_type` from frontmatter and suppress the `missing-test-command` warning when `task_type == "gate"`. If the field is absent (legacy TASK files from before this plan), consumers fall back to the current behavior (treat `Test command: none` as a gap). This is backwards-compatible and opt-in via a single frontmatter key.

### `00_INDEX.md.template` (structure frozen per DUAL_AGENT_Plans)

Sections (in order): H1 title, header block (Created / Base branch / Parent plan link), "Why this directory exists" paragraph, "Shared background" optional, "Chunk roster" table (File / Task / Priority / Issues absorbed / Depends on), "Execution order" ASCII diagram, "How each chunk is structured" meta section, "Reference pointers" (aggregate file list). Placeholders replaced at render time; no structural deviation.

Parent plan link uses relative form: `[{slug}.md](../{slug}.md)`.

### `00_INDEX.json.template` (schema frozen per DUAL_AGENT_Plans)

```json
{
  "schema_version": 1,
  "source": "plan-decomposer",
  "chunks": [
    {
      "task_id": "001",
      "v3_task": "TASK-001",
      "file": "TASK-001_<task_slug>.md",
      "priority": "critical",
      "issues_absorbed": [],
      "depends_on": [],
      "status": "Pending",
      "superseded_by": []
    }
  ]
}
```

- `source` is always `"plan-decomposer"` (distinguishes from `"manual-sidecar"` in the exemplar).
- `v3_task` preserved as the canonical task label — always `"TASK-" + task_id`.
- `issues_absorbed` empty when source plan has no issue IDs; not dropped from the schema.
- `superseded_by` empty `[]` for active tasks; populated only during supersede transactions.

### `plan-decomposer.json.template`

```json
{
  "plan_dir": "docs/plans",
  "tasks_root": "docs/plans/decompose_plans_tasks",
  "base_branch": "main",
  "python_bin": "venv/bin/python"
}
```

---

## SKILL.md (skills/decompose-plan/SKILL.md)

Orchestrates the dispatch. Structure:

1. Parse user-provided args: `input_path`, optional `output_slug`, optional `tasks_root`, flags.
2. Load consumer config via `${CLAUDE_PLUGIN_ROOT}/scripts/decomp_ops.py inspect --mode paths --input-path <path>` — emits all derived paths AND `input_mode` in one call.
3. Delegate to the `plan-decomposer` agent with a structured prompt including: input_path, input_mode, resolved plan_dir + tasks_dir, today, flags, path-info output.
4. Receive agent report, pass-through to user.

## Bridge Command (`commands/decompose-plan.md`)

```
---
name: decompose-plan
description: Decompose a freeform plan markdown (or re-decompose a too-large TASK file) into individual TASK files ready for /implement-plan. Emits 00_INDEX.md, 00_INDEX.json, and per-TASK files in docs/plans/decompose_plans_tasks/<slug>/.
---

Invoke the `decompose-plan` skill in this plugin (`${CLAUDE_PLUGIN_ROOT}/skills/decompose-plan/SKILL.md`). Pass the user-provided input path (source plan or TASK file), output slug (if any), and flags through verbatim. Follow the skill protocol exactly.
```

---

## Marketplace Entry + Plugin Manifest

### `/mnt/d/claude-plan-executor/.claude-plugin/marketplace.json` — add to `plugins[]`:

```json
{
  "name": "plan-decomposer",
  "description": "Decompose freeform plan markdown (or too-large TASK files via auto-supersede) into individual TASK files that /implement-plan can consume.",
  "source": "./plugins/plan-decomposer"
}
```

### `/mnt/d/claude-plan-executor/plugins/plan-decomposer/.claude-plugin/plugin.json`

```json
{
  "name": "plan-decomposer",
  "version": "0.1.0",
  "description": "Decompose freeform plan markdown into individual TASK files compatible with /implement-plan, with 00_INDEX.md and 00_INDEX.json navigation and input-path-auto-detected supersede.",
  "author": {"name": "marc317ad"}
}
```

---

## Tests (`tests/scripts/test_decomp_ops.py` — must-cover matrix)

Modeled on `tests/scripts/test_plan_ops.py`:

1. **Config loading** — env var override, cwd config, plugin default; malformed JSON fails loud.
2. **inspect --mode paths — fresh input** — 10-key payload, absolute POSIX paths; `input_mode == "fresh"`.
3. **inspect --mode paths — TASK input** — same payload; `input_mode == "supersede"`; slug inherited from parent directory.
4. **parse-source-plan on source plan** — pinwheel-plan fixture: phases A–E detected, 20 step candidates enumerated, file migration matrix parsed into per-step file sets.
5. **parse-source-plan on TASK file** — accepts a rendered TASK file as input; extracts `## Scoped Context` + `## Implementation Playbook` + user-added deviations.
6. **compute-fingerprint** — deterministic, canonical normalization applied (lowercase, `:line_range` stripped, whitespace collapsed).
7. **manifest --action init** — empty manifest shape matches canonical.
8. **manifest --action commit** — atomic write-then-rename (`os.replace` / `os.rename`) of the staging temp file into the target path; corrupt stdin fails loud without touching the on-disk manifest file.
9. **build-schedule happy path (fresh)** — parsed tree + manifest → schedule with correct topological ordering, split/merge reasons recorded.
10. **build-schedule cycle detection** — returns non-zero + ring list; no writes.
11. **build-schedule supersede mode** — `--supersede-parent 004` correctly sets parent to `Superseded`, creates `004A..004E`, inherits parent's `depends_on`, preserves external references to `004`.
12. **build-schedule supersede-illegal-state** — parent with status Done / Cancelled / Superseded rejected.
13. **build-schedule supersede depth cap** — attempting to supersede `004A` (already single-suffixed) rejected with `supersede-depth-exceeded`.
14. **render dry-run** — staging dirs populated, no production writes.
15. **render happy path** — TASK files match template, `00_INDEX.json` schema validates, `00_INDEX.md` sections present, parent-plan copy lands at `<plan_dir>/<slug>.md`.
16. **validate-output** — catches missing required field, ID regex violation, dangling dep, cycle, schema drift, chunk-vs-file mismatch, missing parent-plan file.
17. **set-status** — frontmatter-only rewrite; `00_INDEX.json.status` synced; filename unchanged.
18. **Full e2e with pinwheel plan (fresh)** — source → parsed → scheduled → rendered → validated. Emits N TASK files, all plan-analyst-valid. Characterized at Phase B.
19. **Full e2e supersede** — take a TASK file emitted by #18, run decomposer on it → parent marked Superseded, 3–5 children emitted, INDEX updated, all children plan-analyst-valid.
20. **Re-run idempotency** — second run with unchanged source plan produces zero writes + "no-op" report.
21. **Re-run merge on source edit** — edit one section's problem prose in source; re-run produces exactly one merged TASK with `change_history` appended.
22. **Context-budget split — hard ceiling** — synthetic spec with 6 files is split into ≥ 2 TASKs with `split_ceiling_files` reason.
23. **Context-budget split — soft signal** — synthetic spec matching 2+ soft signals is split with the appropriate reason codes.
24. **Gate-step `task_type` annotation** — a candidate classified `is_gate=true` renders with `task_type: gate` in frontmatter; a non-gate candidate renders with `task_type: standard`. `validate-output` accepts both; rejects unknown values.
25. **Input mode detection — realpath canonicalization** — `input_path` is a symlink that resolves INTO `<tasks_root>` but whose literal path is outside; decomposer correctly classifies as supersede mode.
26. **Input mode detection — symlink resolving OUTSIDE tasks_root** — `input_path` is a symlink in `<tasks_root>` pointing to an external freeform plan; decomposer correctly classifies as fresh-decomposition mode (follows realpath outward) and does NOT trip the `refuse-to-decompose-into-own-output` guard.
27. **Input mode detection — strict filename regex** — `tasks-001_foo.md` (lowercase `t`), `TASK-1_foo.md` (missing zero-pad), `TASK-001_Foo.md` (uppercase in slug), `TASK-001.md` (missing slug) — all rejected from supersede mode. `TASK-001_foo.md` accepted.
28. **Input mode detection — TASK-shaped source filename** — source plan literally named `TASK-999_some-plan.md` (not inside `<tasks_root>`). Fresh mode detected. Slug defaults to `TASK-999_some-plan` which would shadow real output; decomposer aborts with `refuse-task-shaped-slug` unless `--output-slug` overrides.
29. **Input mode detection — frontmatter missing fields** — a hand-placed file under `<tasks_root>/<slug>/` with valid filename but missing `content_fingerprint` in frontmatter fails check 4 and **hard-aborts** with `invalid-task-frontmatter`. The decomposer does NOT fall back to fresh mode — normative rule forbids fresh-mode re-classification for any realpath inside `<tasks_root>`.
30. **Recovery — pre-parent-rename phase (genuine)** — crash injected before parent rename; re-run re-stages and completes. Final state byte-identical to single-run happy path.
30a. **Recovery W0 — parent rename succeeded, journal phase-2 lost** — crash injected between `os.rename(staging_parent, production_parent)` and the phase-2 journal write. Production parent is the NEW contents; staging parent is gone; journal still says `pre-parent-rename`. Re-run detects `staging_parent ABSENT AND F_prod == planned_parent_fingerprint`, writes phase-2, resumes from step 5. Final state identical.
30b. **Recovery W0 disambiguation — pre-rename ambiguous** — crash leaves `staging_parent ABSENT` but `F_prod != planned_parent_fingerprint`. Re-run aborts with `pre-rename-state-ambiguous` and does NOT attempt a write.
31. **Recovery — post-parent-rename phase** — crash injected between parent and tasks renames; re-run uses journal, re-renders tasks staging, completes. Final state identical.
32. **Recovery — post-tasks-rename phase** — crash injected after tasks rename but before manifest finalize; re-run reads journal, verifies task fingerprints match, finalizes manifest without regenerating staging.
33. **Recovery — post-parent-rename with hand-edit** — crash injected at post-parent-rename; user hand-edits the production parent before re-run; re-run detects mismatch with `parent_fingerprint_post_swap` and aborts requiring human intervention (documented path).
34. **Parent-copy divergence — production hand-edit** — after a successful first run, user edits `docs/plans/<slug>.md` directly; second run (unchanged source) aborts with `parent-copy-divergent` naming all three fingerprints.
35. **Parent-copy divergence — `--force-parent` override** — replay of #34 with `--force-parent`: production parent overwritten; manifest `parent_override_at` + source fingerprint recorded.
36. **Parent-copy divergence — source also edited** — user edits both source AND production parent independently; second run (without `--force-parent`) aborts with `parent-copy-divergent` showing all three distinct fingerprints (source ≠ manifest ≠ production).
37. **Cross-device rename guard** — staging on a different filesystem from production (simulated via `os.stat().st_dev` mock); decomposer aborts with `cross-device-rename-unsafe` and does not attempt the rename.
38. **Recovery W2 — crash between tasks-backup and tasks-swap** — journal reports `phase: "tasks-backup-created"`; backup dir exists; production `<slug>/` absent. Re-run with `recover` resumes from step 7, lands staging into production, deletes backup. End-state identical to non-interrupted run.
39. **Recovery W3 — crash between tasks-swap and backup-deletion** — journal reports `phase: "tasks-swap-committed"`; production `<slug>/` has NEW tasks; backup still present. Re-run verifies current task fingerprints match `journal.task_fingerprints`, deletes backup, finalizes.
40. **Recovery — journal vs manifest cache divergence** — journal reports `committed` but `_manifest.json` inside tasks dir is stale or missing. `recover` regenerates the cache manifest; no staging work needed.
41. **Recovery — journal lost mid-crash** — simulate `<slug>.journal.json` being deleted between phases (e.g. by an overeager cleanup script). Re-run: decomposer detects missing journal, warns, treats as initial run; Pre-Swap Invariant Check catches divergence if production has hand-edits.
42. **Manifest exists but production parent missing** — user deletes `docs/plans/<slug>.md` after a successful run. Second run: `F_prod == None` + journal has fingerprint → abort with `parent-copy-missing` suggesting `--force-parent`. With `--force-parent`, re-commits from staging; journal records `parent_override_reason: missing`.
43. **Parent exists but journal untracked** — production parent present with unknown fingerprint; journal has no `parent_fingerprint_post_swap`. If `F_prod == F_source`, decomposer adopts (no-op rename) and records fingerprint. If `F_prod != F_source`, aborts `parent-copy-untracked` unless `--force-parent`.
44. **Hard abort — TASK-shaped file in tasks_root with invalid frontmatter** — place `<tasks_root>/<slug>/TASK-999_hand.md` with empty or malformed frontmatter. Run `/decompose-plan` pointing at it. Expect hard abort `invalid-task-frontmatter` — NOT fresh mode, NOT silent write-over.
45. **Hard abort — directory input under tasks_root** — run `/decompose-plan <tasks_root>/<slug>/`. Expect hard abort `refuse-to-decompose-into-own-output` (descendant of tasks_root but not a valid TASK filename).
46. **Hard abort — TASK-shaped file in tasks_root but manifest has no record** — user hand-creates `TASK-500_foo.md` with valid frontmatter but manifest has no `task_id: 500` entry. Hard abort `task-not-in-manifest`.
47. **Plan-analyst `task_type: gate` suppression** — fixture TASK file with `task_type: gate` + `Test command: none` passes plan-analyst `outcome: valid` with NO `missing-test-command` gap. Same fixture with `task_type: standard` (or field absent) produces the gap. This proves the Phase A step 6a change is in effect.
48. **Cross-plugin sync-status happy path** — TASK file with frontmatter `status: Pending` + body `- **Status:** done` (simulating post-executor run). Running `sync-status` promotes frontmatter to `Done` and `00_INDEX.json.chunks[].status` to `Done`; manifest records history entry. Re-running `sync-status` is a no-op.
49. **Cross-plugin sync-status refuse demotion without --force** — TASK with frontmatter `status: Done` + body `- **Status:** open` (someone regressed). `sync-status` without `--force` leaves frontmatter alone and reports the inconsistency. With `--force`, demotes.
50. **Prohibition — supersede after execution** — TASK with body `- **Status:** done`. Run `/decompose-plan` on that TASK file. Expect hard abort `supersede-after-execution` unless `--force-supersede`. With the flag, proceeds and records audit entry.
51. **Concision — trivial TASK elides conditional sections** — spec with 1 file, 2 machine-checkable acceptance criteria, no implementation nuance. Render produces no `## Scoped Context`, no `## Implementation Playbook`, no `## Out of Scope`, no inline `**Parent plan:**` link. Manifest `history[-1].prose_decisions[]` contains `prose_omitted_self_evident`, `prose_omitted_trivial_scope`, `prose_omitted_bounded_by_files`, `prose_omitted_self_contained`.
52. **Concision — gate TASK slim body** — TASK with `task_type: gate`. Render emits only Goal + Tasks + Verification. No Scoped Context, Implementation Playbook, or Out of Scope sections. Manifest records `prose_omitted_gate`. Implementer-facing token count ≤ 60.
53. **Concision — parent-plan inline link emitted on cross-cutting dep** — TASK that relies on a cross-cutting invariant documented in parent plan (invariant not expressible via the TASK's own files + acceptance criteria). Renderer emits `**Parent plan:** [../<slug>.md](../<slug>.md)` inline. Frontmatter `source_plan` present. Manifest does NOT record `prose_omitted_self_contained` for this TASK.
54. **Concision — parent-plan inline link omitted when self-contained** — TASK whose scope is fully bounded by its file list + acceptance criteria. Renderer omits the inline link but keeps frontmatter `source_plan`. Manifest records `prose_omitted_self_contained`.
55. **Concision — hard budget violation aborts render** — synthetic TASK candidate with 500-token implementer-facing prose. Agent workflow step 11 aborts with `prose-budget-exceeded`, leaves staging dir intact, does NOT call `commit-swap`. Reducing candidate prose to 350 tokens succeeds.
56. **Concision — gate budget violation aborts render** — synthetic `task_type: gate` candidate with 100-token implementer-facing prose. Agent aborts with `prose-budget-exceeded` (ceiling 60). Reducing to 50 tokens succeeds.
57. **Concision — validate-output prose-budget hard gate** — place a hand-crafted TASK file in staging with 500-token implementer-facing body and all other checks passing. `validate-output` exits non-zero with `prose-budget-exceeded`. Reducing the body to 350 tokens allows `validate-output` to pass.
58. **Concision — validate-output soft-budget warning** — TASK with 250-token implementer-facing body (above soft 200, below hard 400). `validate-output --json` exits 0 with `warnings[]` containing one `prose-budget-soft` entry. Does NOT fail validation.
59. **Concision — overlap merge reason code** — candidate whose Description and Implementation Playbook paraphrase the same source-plan step. Agent merges into Description, drops Playbook, records `prose_merged_overlap`. Rendered TASK has Description but no Implementation Playbook section.
60. **Concision — supersede re-minimizes parent prose** — parent TASK with 600-token implementer-facing body (legacy, pre-concision). Supersede run emits 3 children, each with implementer-facing body ≤ 400 tokens. Acceptance criteria preserved semantically across parent → children; narrative prose rebuilt fresh, not inherited verbatim.
61. **Concision — prose-decision coverage required** — synthetic render that skips the concision-elision pass entirely (bug simulation: emits maximal template verbatim with zero entries in `prose_decisions[]`). `validate-output` exits non-zero with `prose-decisions-missing` — proves the coverage check is in effect.

---

## Phased Rollout

### Phase A — Plugin build (in `claude-plan-executor`)

1. Create `plugins/plan-decomposer/` skeleton + `.claude-plugin/plugin.json`.
2. Port + refactor `fix-plan-decomposer.md` to `agents/plan-decomposer.md` per this spec.
3. Write `scripts/decomp_ops.py` (stdlib-only, 8 subcommands).
4. Write templates (task + both indexes + config default).
5. Write `skills/decompose-plan/SKILL.md`.
6. Write `commands/{decompose-plan,refresh}.md`.
6a. **Plan-analyst frontmatter read (surgical, out-of-plugin but in-scope).** In `/mnt/d/claude-plan-executor/plugins/plan-executor/agents/plan-analyst.md`, add a single rule: when a TASK file's frontmatter contains `task_type: gate`, do NOT emit `missing-test-command` even if `Test command: none`. All other gap detections remain. The existing `test_command: none` semantic for `task_type: standard` (or absent `task_type` — backwards-compatible legacy) is unchanged. Add a one-case test to `/mnt/d/claude-plan-executor/tests/` proving the suppression fires only for gate tasks. This is the ONLY plan-executor-side change in Phase A; no orchestrator or `plan_ops.py` changes.
7. Write `tests/scripts/test_decomp_ops.py` with pinwheel-plan fixture. Run green locally.
8. Add marketplace entry; author README plugin section.
9. Commit series: one commit per artifact type (skeleton / scripts / templates / agent / skill+commands / tests / plan-analyst-gate-fix / marketplace+docs).

### Phase B — Shadow run against pinwheel plan (inside claude-plan-executor)

10. `claude --plugin-dir /mnt/d/claude-plan-executor` from `/mnt/d/claude-plan-executor`.
11. Copy the source pinwheel plan into the repo first: `cp ~/.claude/plans/we-need-to-make-compressed-pinwheel.md /mnt/d/claude-plan-executor/docs/plans/we-need-to-make-compressed-pinwheel.md`. This is the committed parent plan; the decomposer will re-copy it idempotently.
12. `/decompose-plan docs/plans/we-need-to-make-compressed-pinwheel.md --dry-run` → inspect report + staging dirs.
13. Review output shape: TASK files cover the phases; dependencies match human-reasonable order; `00_INDEX.md` structure matches DUAL_AGENT_Plans; split/merge reason codes are sensible.
14. Iterate agent spec or template if report shows gaps.
15. Live run: `/decompose-plan docs/plans/we-need-to-make-compressed-pinwheel.md`.
16. Manually feed three TASK files into `/implement-plan docs/plans/decompose_plans_tasks/we-need-to-make-compressed-pinwheel/TASK-001_<task_slug>.md --dry-run`. Expect `outcome: valid` from plan-analyst. Repeat for TASK-002 and TASK-003; if all three pass, promote.
17. Supersede smoke test: pick one of the emitted TASK files, hand-edit its body to add a "## Deviations" section, then `/decompose-plan docs/plans/decompose_plans_tasks/.../TASK-NNN_<task_slug>.md`. The decomposer MUST log `input TASK has drifted from manifest fingerprint; treating user edits as authoritative for split decisions` in its report (per Input Mode Detection Rules step 7). Expect parent status → `Superseded`, 2–5 children emitted, INDEX updated, all children plan-analyst-valid. Verify `_manifest.json.history[]` has one new supersede entry with `{from: <parent_id>, to: [<child_ids>], reason: <string>, fingerprint_parent: <sha>}`.

17a. Recovery smoke test (addresses Codex pass-2 issue A and pass-3 issue Q1): simulate a crash during the cross-directory swap by running a Python-driven test that (1) performs Step 13 up through journal entry 2 (post-parent-rename) and then raises, (2) re-runs `/decompose-plan` on the same source plan with no edits. Expect: the second run reads `<slug>.journal.json.committed_state.phase == "post-parent-rename"` (the journal is the recovery authority; `_manifest.json` inside `<slug>/` is consumer cache only), re-renders tasks staging, completes the tasks rename, finalizes journal and cache manifest. End state MUST be identical to a single successful run (same fingerprints, same INDEX, same TASK file contents).

17a-bis. Recovery W0 smoke test: simulate a crash immediately after `os.rename(staging_parent, production_parent)` but before journal phase-2 is written. Re-run: decomposer detects `staging_parent ABSENT AND F_prod == planned_parent_fingerprint`, writes phase-2 journal entry, resumes from step 5. End state identical to non-interrupted run.

17b. Parent-copy drift smoke test (addresses Codex pass-2 issue F): after step 15, hand-edit `docs/plans/we-need-to-make-compressed-pinwheel.md` (add a trailing comment). Re-run `/decompose-plan` on the source plan at its original location. Expect abort with `parent-copy-divergent` and a report naming all three fingerprints (journal / production / source). Re-run with `--force-parent` and confirm the production parent is overwritten with the source and a `parent_override_at` + `parent_override_reason: divergent` audit entry is added to the journal.

**Phase B exit criteria (must ALL be green before Phase C):**

Structural (hard gates):

- Steps 12–15 completed with no validation failures.
- Step 16's three TASK files returned `outcome: valid` from plan-analyst.
- Step 17 supersede smoke test passed (including manifest history entry).
- Step 17a recovery smoke test passed (end-state identity byte-for-byte).
- Step 17b parent-copy drift smoke test passed (abort + force flow both verified).
- `validate-output` exits 0 on the live production run.
- Zero TASK files with hard-ceiling violations (`> 5 files`, `> 250 LOC`, `> 1 new module`, cross-cutting) post-split. Tracked by auto-lint over the production output.

Quantitative (calibration gates — seeded heuristics; revise after first shadow run):

**Important:** the numeric thresholds below are **initial targets**, not gates proven from prior data. The first Phase B shadow run records the observed split/merge distribution on the pinwheel plan; the team then either:

- **Justifies** each threshold (the observed value is within the target and makes sense for the pinwheel plan's structure), OR
- **Revises** the threshold and documents the new value + rationale in a `_manifest.json.history[]` entry.

Thresholds are locked into the `CEILING_*` module-level constants only AFTER the first-run calibration step signs them off. Until then, they are advisory gates — `validate-output` logs violations as warnings, not errors.

Initial thresholds:

- **At least one split** on the pinwheel plan. Rationale: the pinwheel plan has a known-large phase (Phase A steps 1–10 create + integrate + test a full plugin); a single TASK there would bust implementer context.
- **At least one merge** on the pinwheel plan. Rationale: Phase A step 9 (`Commit series`) + Phase C step 19 (`Update README`) are trivial scaffolds adjacent to substantive work; standalone emission wastes implementer cycles.
- **Zero gate-TASKs** produce `missing-test-command` from plan-analyst. Rationale: proves `task_type: gate` + plan-analyst rule are both in effect.
- **Ratio cap (advisory):** no more than 25% of emitted TASKs have complexity_score < 0.3 (trivial-split proxy). Calibrated after run 1.
- **No orphaned dependencies:** `depends_on` references all resolve to emitted task_ids; no `in_progress` / `blocked` tasks in the initial run's `00_INDEX.json`. Rationale: integrity check, not a calibration parameter. Hard gate.
- **Reason-code coverage:** every split decision carries at least one `split_*` reason code; every merge decision carries at least one `merged_*` reason code; every emitted TASK carries at least one `prose_*` reason code in `_manifest.json.history[].prose_decisions[]`. Hard gate (not calibration-sensitive).
- **Zero hard prose-budget violations:** no TASK in the production output has implementer-facing body tokens > 400 (standard) or > 60 (gate). Hard gate — `validate-output` enforces this at commit-swap time, so production state should already satisfy this by construction. Violations here indicate a bug in the renderer or validator, not a calibration miss.
- **Mean implementer-facing token count (advisory):** across all emitted TASKs on the pinwheel plan, mean implementer-facing body tokens ≤ 150 (soft gate, calibrated after first shadow run). Rationale: if the mean creeps toward the 200 soft ceiling, the agent is drifting toward verbosity. If the pinwheel plan's intrinsic complexity legitimately drives the mean higher, the threshold revises with documented rationale in a `_manifest.json.history[]` entry. This is the concision-focused counterpart to the `< 0.3` complexity ratio cap.

The gates split into two tiers:

- **Hard (never calibratable):** reason-code coverage (split + merge + prose), dependency integrity, zero gate-TASK `missing-test-command` noise, zero hard prose-budget violations.
- **Soft (calibrate after first run):** at-least-one-split, at-least-one-merge, ratio cap, mean implementer-facing token count. First shadow run's observed values feed the decision to lock or revise.

If the first shadow run violates a hard gate, fix the code. If it violates a soft gate, re-evaluate the gate.

### Phase C — Permanent install

18. `/plugin install plan-decomposer@claude-plan-executor --scope user` once Phase B parity holds.
19. Update `/mnt/d/claude-plan-executor/README.md` with plan-decomposer section + `/decompose-plan` vs `/implement-plan` disambiguation paragraph.
20. Document in downstream consumer repos' `CLAUDE.md` (when they opt in) the install command and `.claude/plan-decomposer.json` shape.

### Phase D — Re-run regression

21. Re-run `/decompose-plan` on the same pinwheel plan → no writes. Verify re-run idempotency.
22. Edit one paragraph in the source plan; re-run → exactly one TASK merged, `change_history` appended.
23. Re-run on a superseded parent → aborts with `supersede-illegal-state` (parent is already `Superseded`).

---

## Verification

1. `/plugin list` shows `plan-executor` and `plan-decomposer`.
2. `/decompose-plan docs/plans/we-need-to-make-compressed-pinwheel.md --dry-run` exits 0, no files mutated in the production output dirs.
3. Staging dirs (`docs/plans/decompose_plans_tasks/<slug>/.staging-<run_id>/` and `docs/plans/.staging-<run_id>/`) contain the full output layout.
4. `venv/bin/python plugins/plan-decomposer/scripts/decomp_ops.py validate-output --tasks-dir docs/plans/decompose_plans_tasks/<slug>/ --plan-dir docs/plans/` exits 0.
5. `jq '.chunks | map(.task_id) | length' docs/plans/decompose_plans_tasks/<slug>/00_INDEX.json` equals the TASK file count on disk.
6. `jq '.chunks | .[] | select(.status == "Pending") | .depends_on[]' 00_INDEX.json | sort -u` — every referenced id resolves to a chunk.
7. `venv/bin/python /mnt/d/claude-plan-executor/plugins/plan-executor/scripts/plan_ops.py preflight --plan-file docs/plans/decompose_plans_tasks/<slug>/TASK-001_*.md` exits 0 — plan-analyst compatibility proven.
8. Re-run decomposer; report shows `tasks_emitted=0, tasks_merged=0, tasks_superseded=0` and no writes occurred (`find docs/plans/decompose_plans_tasks/<slug>/ docs/plans/<slug>.md -newer <manifest_mtime>` is empty).
9. Supersede smoke test (Phase B step 17) produces valid `00_INDEX.json.chunks[]` with parent `status="Superseded"` + `superseded_by: [NNNA, NNNB, …]`.

---

## Rollback

- **During Phase B (shadow):** delete the generated `docs/plans/decompose_plans_tasks/<slug>/` subdirectory and `docs/plans/<slug>.md`. Plugin auto-unloads when the `--plugin-dir` flag is removed.
- **After Phase C (permanent install):** `/plugin uninstall plan-decomposer`, `git rm -r` the output subdir + parent-plan copy if committed.
- **Plugin regression:** pin to the last known-good version in `.claude/settings.json` `enabledPlugins`; `/plugin update` rolls forward after fix.
- **Supersede rollback:** if a supersede run goes wrong but has already atomic-swapped, `git revert` the supersede commit — children removed, parent status reverts to `Pending`, external `depends_on` references unchanged.

---

## Critical Files to Touch

**Create:**

- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/.claude-plugin/plugin.json`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/agents/plan-decomposer.md`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/commands/decompose-plan.md`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/commands/refresh.md`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/skills/decompose-plan/SKILL.md`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/scripts/decomp_ops.py`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/templates/task-template.md.template`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/templates/00_INDEX.md.template`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/templates/00_INDEX.json.template`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/templates/plan-decomposer.json.template`
- `/mnt/d/claude-plan-executor/plugins/plan-decomposer/docs/README.md`
- `/mnt/d/claude-plan-executor/tests/scripts/test_decomp_ops.py`

**Modify:**

- `/mnt/d/claude-plan-executor/.claude-plugin/marketplace.json` (add plan-decomposer entry)
- `/mnt/d/claude-plan-executor/README.md` (plan-decomposer section + `/decompose-plan` vs `/implement-plan` disambiguation)
- `/mnt/d/claude-plan-executor/plugins/plan-executor/agents/plan-analyst.md` (one surgical rule — suppress `missing-test-command` when `task_type: gate` in frontmatter)
- `/mnt/d/claude-plan-executor/tests/` (one-case test for the plan-analyst gate-suppression rule)

**Phase B copy (not edits):**

- `/mnt/d/claude-plan-executor/docs/plans/we-need-to-make-compressed-pinwheel.md` — first-run parent plan copy.

---

## Out of Scope

- `/implement-plan` orchestrator changes (plugin is strictly read-plan + write-tasks).
- Multi-plan merging (one source plan → one subdirectory).
- Auto-dispatch of `/implement-plan` post-decomposition.
- Non-markdown source plans (YAML, JSON, RST).
- Git-history-preserving migrations for existing ad-hoc decompositions (like `DUAL_AGENT_Plans` itself).
- Cross-project plugin data via `${CLAUDE_PLUGIN_DATA}` — all runtime output lives in consumer `tasks_root` / `plan_dir`.
- Auto-promoting decomposed tasks to a new branch / PR — explicit caller responsibility.
- Recursive supersede (depth > 1). If a single-letter suffix child proves too big, redo the parent split so siblings absorb the complexity.

---

## Appendix A — Codex Review Highlights (2026-04-17, pass 1)

Codex flagged three critical issues and three nice-to-haves; all folded into the plan above. Key callouts:

- **Critical:** stable identity under-specified → addressed in Canonical Contract (fingerprint input locked) and Supersede Protocol (state machine).
- **Critical:** supersede/split semantics non-operational → addressed in Supersede Protocol state machine + the input-path auto-detect + old-ref preservation rule.
- **Critical:** wire contract drift across phases → addressed by the single `build-schedule` step and the hard-gate `validate-output` pre-commit.
- **Nice-to-have:** explicit slug policy → default = filename-without-extension, `--output-slug` override.
- **Nice-to-have:** gate-step classification → always Claude-tier TASKs with `Test command: none`; Acceptance criteria describes observable outcome.
- **Nice-to-have:** granularity reason codes → tracked in `_manifest.json.history[].{split_reasons, merge_reasons}`.
- **Surface reduction:** 15 subcommands → 8 via `inspect` multi-mode and `build-schedule` as the single intermediate object. `match-existing` renamed `reconcile-existing` (implicit in agent step 5), `allocate-ids` renamed `assign-stable-ids` (implicit in build-schedule), `rename-status` renamed `set-status` (frontmatter-only, not filename).
- **Workflow ordering:** load-existing moved to Step 2 (before fingerprint) so the authoritative state model is built once.
- **Post-hoc split guard:** supersede operation writes to staging, then validates, then atomic-swaps — same transactional path as the main flow.

## Appendix B — User Decisions (2026-04-17)

All seven pre-implementation open questions resolved by the user:

1. Colocate parent plan at `docs/plans/<slug>.md`.
2. Slug = source filename without extension (explicit override via `--output-slug`).
3. Granularity = complexity-driven, context-budget aware. Hard ceilings + soft signals + reason codes.
4. Gate steps = always Claude-tier TASKs with `Test command: none`.
5. Supersede = auto-detect via input-path (running `/decompose-plan` on an existing TASK file). Humans edit TASK body with deviations first. Depth capped at single-letter suffix.
6. Development + Phase B inside `claude-plan-executor` repo.
7. Parent-plan copy at `docs/plans/<slug>.md`, committed alongside everything else.

## Appendix C — Codex Pass-2 Findings and Resolutions (2026-04-17)

Codex returned **NO-GO** on pass 2 with two critical items (A, F) and three lesser items (B, D, E). All now addressed in-plan:

| Issue | Codex finding | Resolution in v3 |
|---|---|---|
| A (critical) | Cross-directory commit gap — `docs/plans/<slug>.md` and `docs/plans/decompose_plans_tasks/<slug>/` cannot be atomically committed together with a single POSIX `rename`. | **Recovery + Idempotency Protocol** (in Canonical Contract section) — per-directory atomic rename + manifest journal + re-run reconciliation. Full ACID not claimed; full idempotent recovery is guaranteed. Tests 30–33 cover all four journal phases. |
| F (critical) | Parent-copy drift policy undefined — a hand-edit to production `<slug>.md` would be silently overwritten on re-run. | **Parent-plan fingerprint tracking** (`parent_fingerprint_post_swap` in manifest) + **Parent-plan drift policy** row in Canonical Contract + `parent-copy-divergent` abort + `--force-parent` override. Tests 34–36 cover the three divergence cases. |
| B (high) | Ambiguous supersede detection — regex alone could false-positive on symlinks, case-insensitive filesystems, or TASK-shaped source filenames. | **Input Mode Detection Rules** section — seven strict checks including realpath canonicalization, strict filename regex (no case folding), frontmatter validation, manifest consistency, and `refuse-to-decompose-into-own-output` guard. Tests 25–29 cover the edge cases. |
| D (medium) | Context-budget ceiling numbers (5 files / 250 LOC) unjustified — could over/under-split real plans. | **Calibration note** in Context Budget Rules — ceilings promoted to module-level constants (`CEILING_FILES`, `CEILING_LOC`, `CEILING_PLAN_FILES`, `CEILING_NEW_MODULES`) with explicit mandate to tune during Phase B. Phase B exit criteria now include a distribution-review of reason codes. |
| E (low) | Gate-step TASKs with `Test command: none` produce legitimate `missing-test-command` noise from plan-analyst. | **`task_type: gate` frontmatter annotation** added to task template. Downstream consumers read the annotation and suppress the warning. Backwards-compatible (absent field defaults to current behavior). Test 24 covers the rendering path. |

All five fixes are live in the plan body above. The test matrix expanded from 23 to 37 cases to cover the new mechanisms and edge cases explicitly.

## Appendix D — Codex Pass-3 Findings and Resolutions (2026-04-17)

Codex returned **NO-GO** on pass 3 with five blockers (Q1, Q2, Q3, Q4, Q8) and three concerns (Q5, Q6, Q7). All now addressed in v4:

| Issue | Severity | Codex finding | Resolution in v4 |
|---|---|---|---|
| Q1 | Blocker | Recovery journal lived inside `<slug>/` — a dir being renamed during tasks swap. Crash mid-swap leaves no canonical journal. Crash windows not exhaustively enumerated. | **Journal relocated** to `<tasks_root>/<slug>.journal.json` (sibling outside the swapped dir). **Five crash windows (W1–W5)** enumerated with explicit recovery paths. `_manifest.json` inside `<slug>/` remains as consumer cache, not recovery authority. |
| Q2 | Blocker | "Accept any production state" policy for journals lacking `parent_fingerprint_post_swap` silently overwrites hand-written production parents. | **Safe policy matrix:** adopt only when `F_prod == F_source` (no-op); abort `parent-copy-untracked` when different; abort `parent-copy-missing` when journal has fingerprint but production gone. `--force-parent` now records override reason (`divergent / missing / untracked`). |
| Q3 | Blocker | Fresh-mode fallback on tasks_root failures allowed a TASK-shaped file with bad frontmatter to be re-classified as fresh — risk of writing into own output subtree. Stale Agent Spec regex-only detector remained at lines 259-264. | Input Mode Detection Rules rewritten: **any realpath under `<tasks_root>` that is not a fully-valid supersede target hard-aborts** (error codes `refuse-to-decompose-into-own-output`, `invalid-task-frontmatter`, `task-id-filename-drift`, `task-not-in-manifest`). Fresh mode reserved for realpaths OUTSIDE `<tasks_root>`. Stale detector deleted. |
| Q4 | Blocker | `task_type: gate` requires `plan-analyst` to read the annotation, but plan declared all `/implement-plan` changes out of scope. | Non-Goals amended: **one surgical `plan-analyst.md` rule** in-scope (suppress `missing-test-command` when `task_type: gate`). Phase A step 6a added. Orchestrator + `plan_ops.py` untouched. Backwards-compatible for legacy TASK files. |
| Q8 | Blocker | Status vocabulary drift: decomposer uses title-case (`Pending`), `/implement-plan` uses lowercase (`open`). Same file, risk of mutual stomping. | **Cross-Plugin Status Contract** section: the two vocabularies live on **different fields** (frontmatter `status:` vs body `- **Status:**`). Decomposer owns frontmatter + `00_INDEX.json`; executor owns body bullet. `sync-status` subcommand reconciles post-execution. Mapping table documented. |
| Q5 | Concern | Cross-directory swap was agent-driven Bash `mv` — too much bespoke protocol. | **`commit-swap` subcommand** added to `decomp_ops.py` — encapsulates all 7 protocol phases in Python. `recover` subcommand added for idempotent resume. Surface grew 8 → 10. |
| Q6 | Concern | Test matrix missing cases for new mechanisms. | **Tests 38–50** added (13 new cases) covering W2/W3 recovery, journal-lost, missing-production, untracked-parent, TASK-shaped-with-bad-frontmatter hard-abort, directory-input hard-abort, plan-analyst gate suppression, sync-status promotion/demotion, supersede-after-execution. |
| Q7 | Concern | Phase B exit criteria too qualitative. | Split into **structural (hard gates)** + **quantitative (calibration gates)**. Minimum: ≥1 split, ≥1 merge, 0 ceiling violations, 0 gate-TASK noise, ≤25% trivial-split ratio, 100% reason-code coverage. |

All blocker and concern items resolved in-plan. Test matrix grew from 37 → 50 cases. Subcommand surface grew 8 → 10. One surgical plan-analyst change added to Phase A (explicit, scoped).

## Appendix E — Codex Pass-4 Findings and Resolutions (2026-04-17)

Codex returned **NO-GO** on pass 4 with one blocker + three flags + three nice-to-haves. All addressed in v5:

| Issue | Severity | Codex finding | Resolution in v5 |
|---|---|---|---|
| Test #29 contradiction | Blocker | Test #29 said malformed TASK under `<tasks_root>` is "classified fresh" — contradicts normative hard-abort rule in Input Mode Detection. Re-opens pass-3 Q3 safety failure mode. | Test #29 rewritten to require **hard-abort with `invalid-task-frontmatter`**. No fresh-mode fallback. |
| Q1 — W0 crash window missing | Flag | Window after `os.rename(staging_parent, production_parent)` succeeds but before journal phase-2 write not covered. Journal still reads `pre-parent-rename` while staged parent has been consumed. | **W0 window added** to crash-window table. Recovery logic disambiguates genuine pre-rename from W0 by reading disk state (`staging_parent` presence + `F_prod` check). Tests 30a (W0 happy path) + 30b (W0 ambiguity abort) added. |
| Q5 — Manual mv in workflow step 13 | Flag | Agent workflow step 13 said "mv each staged file," contradicting the centralized `commit-swap` protocol. Supersede workflow did not route through `commit-swap` either. | Step 13 rewritten — **agent invokes `decomp_ops.py commit-swap` in-process; NEVER hand-rolls `mv`**. Supersede mode passes `--skip-parent-rename`. CLI section + subcommand-to-workflow mapping updated. |
| Q6 — Phase B thresholds unjustified | Flag (concern) | 25% / ≥1 split / ≥1 merge thresholds not empirically grounded. | Phase B quantitative gates **split into hard (never calibratable)** and **soft (calibrate after first run)** tiers. First shadow run records observed distribution; team justifies or revises thresholds before locking. |
| Q7 — Consumer compatibility unstated | Flag | Plan gestured at "other consumers" without declaring which fields are stable vs. private. | **Consumer Compatibility Contract** section added declaring stable fields (`task_id`, `source_plan`, `depends_on`, `task_type`, plan-analyst body), private fields (`content_fingerprint`, journal, `_manifest.json`), and non-breaking evolution policy. |
| Surface-count typo | Nice-to-have | Line 267 said "9-command surface" but CLI has 10. | Fixed (`9 → 10`). |
| Phase B recovery text references manifest | Nice-to-have | Recovery smoke test text at line 714 read `manifest.committed_state.phase`, but journal is now the authority. | Updated to reference `<slug>.journal.json.committed_state.phase`; adds note that `_manifest.json` is consumer cache only. |
| Missing example journal JSON | Nice-to-have | No concrete example of journal shape for implementers. | Full example `committed_state` JSON block added to Recovery Protocol section. |

All pass-4 items resolved with line-level fixes — no structural redesigns. Test matrix grew to 52 cases (50 → 52 via W0 tests). 6 crash windows (W0–W5).

## Appendix F — Codex Pass-5 Findings and Resolutions (2026-04-17)

Codex returned **NO-GO** on pass 5 with two blockers + four minor flags. All addressed in v6:

| Issue | Severity | Codex finding | Resolution in v6 |
|---|---|---|---|
| W0 disambiguation not watertight | Blocker | State machine only had two W0 branches; no third value to distinguish `staging_parent PRESENT + F_prod == prior_known` (legitimate pre-rename from a rerun) from `staging_parent PRESENT + F_prod != planned` (ambiguity). | **`prior_parent_fingerprint` added** to phase-1 journal entry. W0 table now has **three rows**: W0-post (rename succeeded, phase-2 lost), W0-pre (genuine pre-rename), W0-ambig (abort). Recovery prose section updated to match. |
| Example journal JSON schema drift | Blocker | Example put `run_id` outside `committed_state`; prose required it inside. Example omitted `planned_parent_fingerprint` that prose required. | **Example rewritten** to place `run_id` + `planned_parent_fingerprint` + `prior_parent_fingerprint` inside `committed_state`. `parent_override_*` fields moved to the `run_history[]` entry object (not inside `committed_state`). Full per-phase field list enumerated beneath the example. |
| Stale "atomic mv" wording (Canonical Contract row) | Flag | Line 151 said "atomic `mv` into place" — inconsistent with centralized `commit-swap` policy. | Replaced with "atomic rename via `commit-swap` (`os.rename` / `os.replace`)"; added explicit note that Bash `mv` / `shutil.move` are never used for production transitions. |
| Stale "atomic write-then-mv" in test matrix | Flag | Line 671 referenced bash `mv`; should specify `os.rename` / `os.replace`. | Replaced with `atomic write-then-rename (os.replace / os.rename)`. |
| Recover subcommand (W1–W5) | Flag | Line 461 said subcommand classifies W1–W5; W0 was added but not reflected here. | Updated to `W0–W5`; also added `pre-rename-state-ambiguous` to the ambiguous-recovery error codes listed. |
| Cross-directory swap row (5 windows) | Flag | Line 157 said "5 tasks-swap crash windows"; should be 6. | Updated to 6 (`W0–W5`). |

All pass-5 items resolved. Journal schema now explicit and watertight. No more manual `mv` wording anywhere in the plan body.

## Appendix G — Sixth- and Seventh-Pass Codex Reviews

Resubmitted to Codex for pass 6 with all pass-5 issues resolved. Narrow focus — Codex's own remediation said "pass 6 should be GO on a focused re-read of lines 200–260 only." Focus areas:

1. Are the W0 table's three rows (`W0-post`, `W0-pre`, `W0-ambig`) + the recovery prose + the example JSON now fully consistent?
2. Does `prior_parent_fingerprint` appear in all three places it must (phase-1 journal write spec, W0 table, example JSON)?
3. Any remaining consistency bugs in the crash-window recovery logic?
4. **GO verdict now?** If not, narrowest path to resolution.

**Pass 6 outcome:** NO-GO with a single one-line fix. Recovery prose at the W0 disambiguation bullet referenced `journal.planned_parent_fingerprint` instead of `journal.committed_state.planned_parent_fingerprint`. Fix applied inline.

**Pass 7 outcome:** GO. Codex confirmed the W0 state machine, journal schema, and crash-window table are mutually consistent with `prior_parent_fingerprint` threaded through phase-1 journal writes, the W0 row table, and the example JSON. No further contract-level changes requested.

## Appendix H — Concision Additions (post-pass-7, 2026-04-17)

After Codex returned GO on pass 7, the user explicitly redirected toward **implementer-facing concision**: TASK documents must carry only the context required to execute, with narrative framing / source-plan history / cross-references omitted unless their absence would leave an acceptance criterion ambiguous. Reviewers — not the TASK body — carry intent validation. The concession was additive policy guidance filling a legitimate gap passes 1–7 had not prioritized; it did not disturb the canonical contract, DAG semantics, cross-plugin status contract, or recovery protocol.

Changes applied without a further Codex pass (per user directive: "Do not re-review in codex unless you feel this change is significant"):

| Gap | Addition | Location |
|---|---|---|
| Concision not a first-class goal | New "Implementer-facing concision" bullet | Goals section |
| No token budget in contract | Added rows for TASK prose budget, Section conditionality, Parent-plan reference conditionality, Audience separation | Canonical Contract table |
| No operational rules for minimization | New **Concision Rules** subsection: audience split, prose budget (400/200 standard, 60/40 gate), Section Conditionality table with 6 omit rules + reason codes (`prose_omitted_self_evident`, `prose_omitted_trivial_scope`, `prose_omitted_no_notes`, `prose_omitted_bounded_by_files`, `prose_omitted_self_contained`, `prose_omitted_gate`), overlap merge signal (`prose_merged_overlap`), MUST NOT / SHOULD rules, reason-code persistence in `_manifest.json.history[].prose_decisions[]`, supersede re-minimization rule, calibration note | Under Context Budget Rules |
| Template had no audience / conditionality markers | HTML comments added above every section marking audience + conditional reason code; gate slim render + rendering note appended | `task-template.md.template` block |
| Render step did not apply rules | Workflow step 11 extended with 5 sub-steps (a) section elision (b) gate slim render (c) overlap merge (d) parent-plan inline decision (e) token-budget check with `prose-budget-exceeded` abort | Agent Spec workflow |
| `validate-output` did not enforce budget | `validate-output` description now enforces hard prose-budget (non-zero exit), soft prose-budget (warning only), and prose-decision coverage (every TASK has at least one `prose_*` reason code) | CLI Surface section |
| No tests | Tests 51–61 added: trivial elision, gate slim body, parent-plan inline conditional emission (both directions), hard/soft budget violations (both render-side and validator-side), gate budget violation, overlap-merge reason code, supersede re-minimization, prose-decision coverage enforcement | Test matrix |
| No Phase B calibration gate | "Zero hard prose-budget violations" added to hard gates; "Mean implementer-facing token count" added to soft (calibrated) gates; reason-code coverage extended to require `prose_*` entries per TASK | Phase B exit criteria |

**Rationale for skipping Codex pass 8:** the additions are policy-level (what prose to omit) rather than contract-level (how files commit, how IDs allocate, how journals recover). Surface area is narrow — template comments, one workflow step's sub-bullets, one CLI subcommand description, one test-matrix block, two Phase B gates. No changes to canonical contract fingerprint inputs, DAG construction, cross-plugin status semantics, or recovery protocol. Pass 7 GO remains intact; concision is additive enforcement scoped to the render + validate boundary.

**Status:** pass 7 GO with concision addendum applied. Ready for Phase A implementation.
