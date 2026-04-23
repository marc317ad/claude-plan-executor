# Dual-Agent Plan Executor

**Created:** 2026-04-13
**Status:** Design complete -- ready for implementation
**Authors:** Claude Code (Opus 4.6) + Codex CLI (GPT-5.4), co-designed

---

## 1. Executive Summary

A portable skill + agent system that takes a structured markdown plan document and executes it using **Claude Code** and **OpenAI Codex CLI** as complementary implementation agents. Claude Code handles architecture, decomposition, and deep reasoning; Codex handles bounded implementation slices and provides genuinely independent cross-review. The system is designed for this repo but contains no repo-specific knowledge in its agent or skill files -- portability to any codebase requires only a plan document and project instruction files.

### What makes this different from single-agent execution

1. **Cross-model review** -- every implementation is reviewed by the agent that did NOT write it, eliminating single-model blind spots
2. **Capability-based routing** -- tasks route to the agent best suited for them, not the most expensive one
3. **Parallel execution** -- Claude subagents and Codex subprocesses run concurrently on non-conflicting tasks
4. **Structured plan contract** -- all work derives from a parsed, validated plan and pre-flight dependency gate, not ad-hoc requests

### Co-design methodology

This plan was co-designed by querying Codex directly about its capabilities, preferred interfaces, failure modes, and collaboration model. Codex's self-assessment and architectural recommendations are incorporated throughout (marked with *[Codex input]* where they shaped a design decision).

---

## 2. Design Principles

1. **The plan document is the contract.** All work derives from it. Status is tracked in it. It is the source of truth.
2. **Cross-review is asymmetric by design.** Codex reviews for implementation discipline; Claude reviews for semantic/domain correctness. Different checklists, not duplicate ones. *[Codex input: "Do not make them duplicate the same checklist. Give them different review prompts."]*
3. **Capability-based routing, not vendor-specific dispatch.** The orchestrator targets roles (`implement`, `review`, `verify`) not agent names. The classifier decides which agent fills each role per task. *[Codex input: "Stop assuming a homogeneous agent runtime."]*
4. **Automatic fallback, never automatic escalation.** Codex failure triggers Claude fallback. Claude failure is deferred to the user. Never the reverse.
5. **Orchestrator never writes code.** Routing, status tracking, commit ceremony only. Identical to the fix-coordinator pattern.
6. **Portable by default.** Agent files contain stable role behavior. Project instructions (`.codex`, `CLAUDE.md`) contain repo-specific rules. Plan documents contain task-local truth. *[Codex input: "If you keep repo rules in agent files, portability dies. If you keep task state in project instructions, resumability dies."]*
7. **Failure handling is first-class.** Mandatory reversion guidance, explicit fail stages, isolated task failures, no blind retry. Peer tasks continue independently after a terminal task failure.
8. **Handoff artifacts are machine-readable.** Outputs from one agent that feed into another use structured JSON, not freeform prose that the next agent must re-parse.

---

## 3. Agent Capability Matrix

Derived from Codex's self-assessment (Q1 responses) and validated against observed behavior.

| Dimension | Claude Code | Codex CLI |
|-----------|------------|-----------|
| Reasoning depth | Deep multi-step, abstract | Moderate, concrete/repo-grounded |
| Multi-file coordination | Strong (architectural reasoning) | Limited (bounded file scope) |
| Project context | Full (CLAUDE.md, memory, MCP, conversation) | Minimal (.codex file + repo reading) |
| Orchestration | Agent tool, skill chaining, plan tracking | None (single-shot subprocess) |
| Cost per task | Opus/Sonnet token cost | Subscription-included (GPT-5.4) |
| Process model | In-session subagent | Independent subprocess |
| Review independence | Self-referential (same model family) | Genuinely independent (different model) |
| Structured output | Via report format conventions | Via `--output-schema` or explicit JSON instruction |
| Parallel capacity | Multiple Agent calls in one message | Multiple Bash calls in one message |

### Task Classification Heuristic

*[Codex input: "A good split is: deeper model decides what to build and how to slice it; I execute slices and report concrete repo facts, blockers, and diffs."]*

**Route to Codex when ALL of:**
- <=3 files changed
- <=30 lines estimated
- Concrete, bounded implementation with clear acceptance criteria
- Has explicit test command in the plan
- No async patterns, routing changes, or API contract modifications
- Not creating new architectural files
- Priority is not critical

**Route to Claude when ANY of:**
- Multi-file coordination or cross-cutting changes
- Async patterns, routing, pipeline modifications
- New file/module creation requiring design decisions
- Complex business logic or domain-sensitive correctness
- Architecture-sensitive changes with tradeoff analysis needed
- No test command (requires reasoning about correctness)
- Critical priority
- Spec is underspecified (Claude must invent behavior)

*[Codex input: "Don't ask me to both invent the design and implement it unless the design space is narrow."]*

**Fallback:** Codex failure -> re-dispatch to Claude Code (one attempt). Claude failure -> deferred (user review).

---

## 4. Architecture

```
User -> /implement-plan <plan.md> [flags]

Phase 0: Preflight
    | Parse arguments, validate plan structure, check dirty tree
    | Resolve cross-plan dependencies via 00_INDEX.json
    | Record starting SHA, generate run_id
    |
Phase 1: Plan Analysis (Claude Code -> plan-analyst agent)
    | Read plan, validate fields, verify files exist
    | Classify each task -> claude | codex
    | Produce file-disjoint execution batches and risks
    |
    | [dry-run stops here]
    |
Phase 2: Batch Execution (parallel within each batch)
    | +-- Claude tasks -> Agent tool -> plan-implementer subagent
    | |     Full context, multi-file reasoning, complex changes
    | |
    | +-- Codex tasks -> Bash -> plugins/plan-executor/scripts/plan_codex_dispatch.py implement
    |       Lean structured prompt, repo-aware, bounded changes
    |       On failure -> automatic Claude fallback
    |
Phase 3: Cross-Review (opposite agent reviews each implementation)
    | +-- Claude-implemented -> Codex review
    | |     Implementation discipline: scope, tests, wiring, rollback
    | |
    | +-- Codex-implemented -> Claude review
    |       Semantic correctness: domain invariants, routing, patterns
    |
Phase 4: Commit & Report
    | One commit per task
    | Status written back to plan document
    | Failed tasks documented with context
    | Summary report generated
```

### Batch Scheduling

Tasks are processed in batches. Within a batch, tasks have disjoint file scopes and can execute in parallel. Between batches, ordering is derived from priority and file-lock conflicts only; intra-plan `Dependencies:` fields are not used for downstream readiness, graph ordering, or failure propagation.

Cross-plan dependency completion is checked once during pre-flight against `00_INDEX.json`. If any required cross-plan dependency is unresolved, execution halts before the analyst runs.

The plan-analyst agent produces the batch schedule. The orchestrator executes it:

```
Batch 1: TASK-001 (codex), TASK-003 (claude)   <- disjoint files, parallel
Batch 2: TASK-002 (claude)                      <- file-lock split / higher priority conflict
Batch 3: TASK-004 (codex), TASK-005 (codex)     <- disjoint files, parallel
```

### State Isolation Contract

Parallel execution inside a batch is made safe by a four-part contract
between the wrapper, the analyst/schema, and the orchestrator. The
wrapper holds no repo-wide lock; safety is structural, not lock-based.

1. **Baseline + allowed_files discipline (wrapper).** Every dispatch captures
   a pre-dispatch `_snapshot_baseline(repo_root)` that records both tracked
   and untracked state before Codex runs. All scope classification and
   cleanup is bounded to the delta vs that baseline intersected with the
   task's `allowed_files`. Out-of-scope writes are **observed only** —
   `validate_scope` emits `out_of_scope_tracked` / `out_of_scope_untracked`
   / `out_of_scope_observed` and never mutates those paths. Timeout cleanup
   is likewise bounded to in-scope paths only; everything else is reported.

2. **Orchestrator-enforced pairwise-disjoint allowed_files (schema).**
   `plan_ops.py parse-schedule` / `write-schedule` rejects any schedule
   whose batch contains two tasks with overlapping `allowed_files` (error
   code `batch-file-overlap`). Disjointness is the invariant that lets
   sibling wrappers coexist on the same repo without races.

3. **Reconciliation at batch barrier (orchestrator).** After all wrappers
   in a batch return and before review/commit, the orchestrator invokes
   `plan_ops.py reconcile-batch --repo-root <repo>` in its single-writer
   phase, feeding the dispatch envelopes on stdin. For each envelope with
   `out_of_scope_observed=True` the helper restores tracked paths and
   unlinks untracked paths (skipping the executor-infrastructure
   protection set), then verifies no residual dirt remains.

4. **Reconciliation failure blocks commit (orchestrator).** A result of
   `reconciliation_failed` (or any residual-dirty path) is a hard halt:
   the orchestrator must surface the failure and must not advance to the
   next batch or dispatch review/commit for any task in the affected
   batch until the operator intervenes. `scope_violation_reconciled`
   keeps the task ineligible for commit; only `no_op` outcomes advance
   normally.

Executor-infrastructure paths (`docs/plans/_run_log.jsonl`,
`docs/plans/_run_lock.json`, `docs/plans/*.schedule.json`, `.claude/`,
`.codex/`, wrapper scripts) are protected symmetrically on both the
wrapper's observe-only side and the orchestrator's reconciliation side —
they are never mutated by either.

---

## 5. Plan Document Schema

The input format. Human-writable, machine-parseable, portable across repos.

```markdown
# Plan: <title>

**Created:** YYYY-MM-DD
**Status:** draft | ready | in-progress | complete | partial
**Base branch:** <branch name>

## Goal
<What this plan achieves -- 2-5 sentences>

## Context
<Background information, constraints, architectural notes.
This section is forwarded to agents for domain understanding.>

## Verification
<How to verify the plan as a whole after all tasks complete.
Integration test command, manual check procedure, etc.>

---

## Tasks

### TASK-001: <title>

- **Status:** pending | in-progress | done | failed | skipped
- **Priority:** critical | high | medium | low
- **Files:**
  - path/to/file.py
  - path/to/other.py:140-160
  - path/to/new_file.py (create)
  - path/to/old_file.py (delete)
- **Test command:** <command> | none
- **Acceptance criteria:**
  - <criterion 1>
  - <criterion 2>

**Description:**
<What needs to change and why>

**Implementation notes:**
<Specific guidance -- line numbers, function names, patterns to follow>

**Reversion guidance:**
<How to undo this change if it fails -- specific files/lines to restore>

---

### TASK-002: <title>
...

---

## Execution Log
<Populated by the orchestrator. Initially empty or absent.>
```

### Required vs Optional Fields

| Field | Required | Notes |
|-------|----------|-------|
| Status | Yes | Updated by orchestrator |
| Priority | Yes | Drives classification and execution order |
| Files | Yes | Scope enforcement depends on this. Optional annotations: `(create)`, `(modify)`, `(delete)`. Default is `modify`. |
| Test command | Yes | `none` is valid but triggers extra review |
| Acceptance criteria | Yes | At least one criterion |
| Description | Yes | - |
| Implementation notes | No | Helps agents but not required |
| Reversion guidance | Yes | Mandatory for failure handling |

### Execution Log Format

Appended by the orchestrator after each run:

```markdown
## Execution Log

### Run <run_id> -- YYYY-MM-DD

**Starting SHA:** <sha>
**Ending SHA:** <sha>

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|------|-------|----------|---------|--------|-------|
| TASK-001 | codex | claude | ship | abc1234 | done |
| TASK-002 | claude | codex | clean | def5678 | done |
| TASK-003 | codex->claude | claude | ship-with-fixes | ghi9012 | fallback |
| TASK-004 | claude | codex | needs-rework | -- | failed (reviewer) |
```

### Canonical Contract (v1)

**Authoritative wire-contract decisions.** Every seam (helpers, tests, agents, skill prose, templates) must conform to this table. Aliases, if retained, are declared explicitly and covered by a regression test in `tests/scripts/test_plan_ops.py`; any unlisted alias is drift and must be removed, not accommodated.

| Concept | Canonical | Alias in v1 | Alias mechanism | Scheduled removal |
|---|---|---|---|---|
| Task status (plan + helpers) | `pending` | `open` | `STATUS_ALIASES = {"open": "pending"}` in `plugins/plan-executor/scripts/plan_ops.py`; accepted in `ALLOWED_TASK_STATUSES` for the alias window. | TASK-006 (sample fixture rewrite). Delete the alias entry when no fixture or test references `open`. |
| Analyst task-field | `id` | `task_id` | `parse-schedule` required-field check accepts either; `batch-next` reads `t.get("id") or t.get("task_id")`. On alias use the response `warnings` list includes `"schedule uses legacy 'task_id' field; canonical is 'id'"`. | Future chunk. Gate removal on `warnings` being empty for one release cycle. |
| Analyst batch-field | `index` | `batch_index` | Same pattern as `id` alias (paired warning). | Future chunk. |
| Implementer concerns label | `**Concerns for reviewer:**` | `**Concerns:**` | Parser searches canonical first, falls back to alias; on fallback emits `"implementer report used legacy 'Concerns:' label; canonical is 'Concerns for reviewer:'"`. `concerns` is a **list of strings**, one per bullet (shape change from prior scalar string). | Future chunk. |
| Implementer `plan_adaptations` | `**Plan adaptations:**` | none | Extracted as a list of strings (one bullet per entry), same shape as `concerns`. | n/a — new field. |
| Execution-log columns | `Task \| Agent \| Reviewer \| Verdict \| Commit \| Notes` | none | Writer (`finalize-execution-log`) is the single source of truth. §5 execution-log example uses this shape. | n/a. |
| `blockers` field shape | array of strings | none | §7.2 line 508 and `plugins/plan-executor/scripts/codex_implement_schema.json` both describe array-of-strings. §15.2's object-form example is deleted; consumers point back to §7.2. | n/a. |
| Schedule persistence path | `docs/plans/<basename>.schedule.json` | none | Named in §9.3. SKILL.md MUST NOT write this file directly. | n/a. |
| Schedule persistence owner | `plan_ops.py write-schedule` (delivered in TASK-004) | none | Once TASK-004 lands, the orchestrator routes analyst-JSON → writer subcommand → sidecar; direct file writes are a protocol violation. | n/a. |

**Emit-label footnote.** `batch-next` output keeps `batch_index` as its response key (line 347 of `plugins/plan-executor/scripts/plan_ops.py`). That key is the wrapper↔orchestrator contract, not the analyst wire contract. Canonical `index` governs the schedule JSON field; the emit label stays `batch_index` in v1 to avoid churning SKILL.md template readers. Any future rename owns the downstream template churn.

**Consumer-shape note.** `parse-implementer-report`'s `concerns` field is a list in v1. Any downstream consumer that reads the helper's JSON output must treat `concerns` as `list[str]`, never as a scalar string.

**Dependency-gate footnote.** In v1, `Dependencies:` in plan markdown and `tasks[*].dependencies` in legacy schedule JSON are not scheduler inputs. The only dependency-completion gate is `plan_ops.py check-plan-deps` against `00_INDEX.json` during pre-flight. Scheduler helpers may tolerate dependency fields for compatibility, but must not use them for readiness, graph ordering, graph validation, or failure propagation.

---

## 6. Agent Specifications

### 6.1 plan-analyst

Reads a plan document, validates structure, classifies tasks, and produces an execution strategy. Read-only.

**File:** `plugins/plan-executor/agents/plan-analyst.md`
**Tools:** Read, Grep, Glob, Bash
**Model:** opus

**Process:**

1. **Parse the plan.** Extract header metadata, goal, context, verification command, and all task blocks. Validate required fields are present and IDs are unique.

2. **Verify file existence.** For each task's `Files:` entries (strip `:line_range` suffixes), confirm the file exists on disk. Missing file -> flag as `stale-paths` with a warning, not a hard failure.

3. **Verify test commands.** For each task with a test command other than `none`, confirm the test file or entry point exists. Flag missing test targets.

4. **Normalize file scopes.** Canonicalize each task's `Files:` entries, strip line-range suffixes for locking, preserve create/modify/delete annotations, and identify overlapping file scopes.

5. **Classify tasks.** For each task, assess:
   - File count and estimated line changes (read the files, assess scope)
   - Complexity factors: async patterns, multi-file coordination, API contracts, routing, new file creation
   - Whether description is concrete or requires design invention
   - Apply the classification heuristic from section 3

6. **Compute batch schedule.** Sort tasks by priority (critical -> high -> medium -> low) and source order, then group file-disjoint tasks into parallel batches. Split tasks that touch the same file into separate batches by file lock.

7. **Identify risks.** Flag: tasks touching the same file in different batches, tasks without test commands, vague acceptance criteria, scope that exceeds what the classified agent typically handles.

**Report format:**

```
## Plan Analysis: <title>

**Outcome:** valid | needs-enrichment | invalid
**Reason:** <if not valid>

**Tasks:** <total> -- <N> claude, <M> codex

### Task Classification

| Task | Title | Agent | Reason | Files | Est. Lines | Gaps |
|------|-------|-------|--------|-------|------------|------|
| TASK-001 | ... | codex | Single file, mechanical | 1 | ~5 | none |
| TASK-002 | ... | claude | Multi-file wiring | 3 | ~40 | none |

### Execution Schedule

- Batch 1 (parallel): TASK-001 (codex), TASK-003 (claude)
- Batch 2 (file-lock split): TASK-002 (claude)
- Batch 3 (parallel): TASK-004 (codex), TASK-005 (codex)

### Gaps

- TASK-005: No test command -- suggest: <suggested command>
- TASK-003: Vague acceptance criteria -- needs concrete assertions

### Risks

- TASK-002 and TASK-004 both touch src/config/settings.py (different batches by file lock)
- Combined behavior of tasks 001+002 needs explicit verification

### File Lock Map

| File | Batch | Tasks |
|------|-------|-------|
| src/api/app.py | 1 | TASK-003 |
| src/config/settings.py | 2 | TASK-002 |
| src/config/settings.py | 3 | TASK-004 |
```

**Schedule JSON (authoritative machine-readable handoff):**

Appended after the markdown report, in a fenced ```json block. The markdown report is for humans; this JSON is the source of truth the orchestrator (section 9) consumes. Design principle 8 applies: handoff artifacts are machine-readable, so the orchestrator does not need to parse markdown tables.

Schema:

```json
{
  "outcome": "valid | needs-enrichment | invalid",
  "tasks": [
    {
      "id": "001",
      "title": "...",
      "agent": "claude | codex",
      "priority": "critical | high | medium | low",
      "files": ["path/to/file.py"],
      "test_command": "... | none",
      "classification_reason": "..."
    }
  ],
  "batches": [
    {"index": 1, "task_ids": ["001", "003"], "file_locks": ["path/a", "path/b"]}
  ],
  "gaps": [
    {"task_id": "001", "type": "stale-path | vague-ac | unresolvable-test | ...", "detail": "..."}
  ],
  "risks": [
    {"type": "...", "detail": "...", "affected_tasks": ["001", "002"]}
  ]
}
```

Contract notes:
- `tasks[*].id` omits the `TASK-` prefix (the prefix is constant; strip it in the JSON to keep keys short).
- `tasks[*].dependencies`, if present from a legacy schedule, is tolerated but ignored by scheduler helpers.
- `tasks[*].test_command` preserves the plan's literal string, including the literal `none`.
- `batches` are in execution order; `batches[*].index` starts at 1.
- `batches[*].file_locks` is the union of every `tasks[*].files` entry for the tasks in that batch (orchestrator uses this to enforce parallel-safety within the batch).
- `gaps[*].type` values include `stale-path`, `vague-ac`, `unresolvable-test`, `missing-test-command`, `empty-implementation-notes`. New values may be added; consumers must tolerate unknowns.
- When `outcome != "valid"`, the orchestrator still receives `tasks` and `batches` on a best-effort basis so the user can see what was parseable, but must not execute them.

**Rules:**
- Read-only. No file modifications. Bash for inspection and lightweight computation only.
- No Agent tool.
- `venv/bin/python` for any Python invocations (if applicable -- check project instructions).
- Word cap <=500 words applies to narrative sections only (Outcome line, Reason, Gaps, Risks summary). Classification table, execution schedule, file lock map, and the JSON schedule block are not word-capped.

### 6.2 plan-implementer

Implements a single task from a plan document. Generalized from `fix-implementer`.

**File:** `plugins/plan-executor/agents/plan-implementer.md`
**Tools:** Read, Grep, Glob, Edit, Write, Bash
**Model:** opus or sonnet (set by orchestrator per plan-analyst classification)

**Inputs from orchestrator:**
- The full TASK-NNN block verbatim from the plan
- The plan's `## Context` section (for domain understanding)
- The plan file path (reference only -- do not modify)
- Base commit SHA
- Any verifier/analyst annotations (e.g., "symbol X moved to line 142")

**Process:**

1. **Read context.** Read every file in the task's `Files:` list in full. If the task references symbols from other files, Grep to confirm they exist with expected names/signatures.

2. **Apply the change.** Minimum change to satisfy acceptance criteria. Do not refactor neighbors, do not add docstrings to untouched code, do not tidy up. If the plan's suggested implementation is wrong (e.g., references an API that doesn't exist), stop and report `plan-incorrect`.

3. **Run the test command.** If the task has a test command other than `none`, run it. On failure, determine if the failure is from this change or pre-existing. Attempt one self-correction. If still failing, report failure with full test output.

4. **Self-check acceptance criteria.** Walk each criterion and confirm evidence of satisfaction.

5. **Report back.**

**Report format:**

```
## TASK-NNN implementation report

**Outcome:** success | partial | failed | plan-incorrect | blocked

**Files changed:**
- path/to/file.py (+N -M lines)

**Diff summary:**
<2-5 bullets describing what changed semantically>

**Test command:** <command or "none">
**Test outcome:** passed | failed | not-run | pre-existing-failure
**Test output (tail):**
<last 30 lines>

**Acceptance criteria check:**
- [x] Criterion 1 -- evidence
- [!] Criterion 2 -- gap explanation

**Plan adaptations:**
<Deviations from the plan's suggested approach. "None" if followed verbatim.>

**Concerns for reviewer:**
<Non-obvious items. "None" if clean.>

**On failure -- what to revert:**
<Exact files/lines, only if outcome != success>
```

**Rules:**
- No Agent tool.
- Never commit. Never mutate git index (`git add`, `git stash`, etc.).
- Never modify the plan file.
- Never touch files outside the `Files:` list unless absolutely required (note in Plan adaptations).
- Keep narrative sections <=400 words total (Outcome line, Diff summary, Plan adaptations, Concerns for reviewer, On-failure revert). Files changed list, Test output tail, Acceptance criteria bullets, and fixed-shape header lines are not counted.

---

## 7. Codex Dispatch Infrastructure

### 7.1 Wrapper Script: `plugins/plan-executor/scripts/plan_codex_dispatch.py`

A Python script that standardizes Codex CLI invocation. Two subcommands: `implement` and `review`.

**Interface:**

```
Usage: python plugins/plan-executor/scripts/plan_codex_dispatch.py <subcommand> [OPTIONS]

Subcommands:
  implement   Dispatch a task to Codex for implementation
  review      Dispatch a diff to Codex for review

Common options:
  --plan-file PATH    Absolute path to the plan document (required)
  --task-id NNN       Task ID within the plan (required)
  --repo-root PATH    Absolute path to repo root (required)
  --json              Output structured JSON (default: human-readable)
  --dry-run           Render prompt only, do not invoke Codex
  --timeout SECS      Codex execution timeout (default: 120)

Review-specific:
  --files FILE,...    Comma-separated list of changed files to review
  --review-focus FOCUS  bugs | regressions | security | tests (default: bugs)
```

### 7.2 Implement Subcommand

**Flow:**

1. Parse plan file, extract TASK-NNN block and `## Context` section
2. Render the Codex prompt from the task (see prompt template below)
3. If `--dry-run`: print prompt + metadata, exit 0
4. Snapshot working tree state via `git status --porcelain`
5. Write implementation JSON schema to temp file (see `plugins/plan-executor/scripts/codex_implement_schema.json`)
6. Invoke: `codex exec --full-auto --ephemeral --output-schema <schema_file> -o <tmpfile> -C <repo_root> - <<< "<prompt>"`
6. Post-checks:
   a. Check exit code (non-zero -> failure)
   b. Diff working tree against snapshot; identify changed/created files
   c. Scope check: changes must be subset of task's `Files:` (normalized, no line ranges). Out-of-scope changes -> `git restore` tracked files, `rm` untracked files, set outcome=scope_violation
   d. Parse `-o` output file for report fields
   e. Re-run the task's test command independently (validate it uses project conventions)
7. Output structured JSON

**Codex implementation prompt template:**

*[Codex input: "I perform best when the prompt is explicit about goal, constraints, and scope."]*

```
Implement TASK-{id} from the project plan.

Objective: {title}

Scope:
- Allowed files: {comma-separated file list}
- Forbidden: all other files

Requirements:
{description}

Implementation notes:
{implementation_notes or "None provided -- follow existing patterns in the target files."}

Non-goals:
- Do not refactor code outside the listed files
- Do not modify the plan document
- Do not commit or use git stash

Validation:
- Test command: {test_command}
- Acceptance criteria:
{acceptance_criteria as bullet list}

On ambiguity: follow the nearest existing pattern in the codebase.

Output: Return valid JSON only, no markdown fences, no trailing commentary:
{
  "task_id": "{id}",
  "status": "completed|blocked|partial|failed",
  "summary": "1-2 sentence description of what you did",
  "files_changed": ["path/to/file.py"],
  "tests_run": [
    {"command": "...", "result": "passed|failed|not_run", "details": "short string"}
  ],
  "blockers": [],
  "concerns": ["any issues for the reviewer, or none"],
  "plan_adaptations": ["any plan deviations made during implementation, if applicable"]
}
```

This prompt follows Codex's preferred structure: Objective, Scope (allowlist), Requirements, Non-goals, Validation, Output expectation. *[Codex input: "Best practice is to give me an allowlist first, not just a vague warning."]*

### 7.3 Review Subcommand

For cross-reviewing Claude's implementations.

**Important:** `codex review` outputs freeform text only -- it does not support `--output-schema`, `--json`, or `-o` flags. For automated cross-review with structured output, use `codex exec --output-schema` instead. *[Codex input: "For cross-review orchestration, use `codex exec --output-schema`. That gives you the most reliable, parseable final payload."]*

**Flow:**

1. Parse plan file, extract TASK-NNN block
2. Compute the diff of changed files: `git diff -- <files>`
3. Write the review JSON schema to a temp file (see schema below)
4. Render review prompt with the diff, task requirements, and review focus
5. Invoke: `codex exec --full-auto --ephemeral --output-schema <schema_file> -o <report_file> -C <repo_root> - <<< "<prompt>"`
6. Parse the `-o` output file as JSON (schema-constrained)
7. Output structured JSON envelope

**Review JSON Schema** (written to `plugins/plan-executor/scripts/codex_review_schema.json`):

```json
{
  "type": "object",
  "properties": {
    "task_id": { "type": "string" },
    "verdict": { "type": "string", "enum": ["clean", "minor-findings", "needs-rework"] },
    "findings": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "severity": { "type": "string", "enum": ["critical", "important", "minor"] },
          "file": { "type": "string" },
          "line": { "type": "integer" },
          "issue": { "type": "string" },
          "suggested_fix": { "type": "string" }
        },
        "required": ["severity", "file", "line", "issue"],
        "additionalProperties": false
      }
    },
    "scope_ok": { "type": "boolean" },
    "acceptance_met": { "type": "boolean" },
    "summary": { "type": "string" }
  },
  "required": ["task_id", "verdict", "findings", "scope_ok", "acceptance_met", "summary"],
  "additionalProperties": false
}
```

**Codex review prompt template:**

*[Codex input: "provide the diff, provide the relevant requirements, tell me whether to prioritize bugs, regressions, security, or tests, and expect findings-first output."]*

```
Review the implementation of TASK-{id} in this repository.

Task objective: {title}
Task requirements:
{acceptance_criteria}

Changed files: {file list}

Review focus: {review_focus}

Check specifically:
1. Does the implementation satisfy all acceptance criteria?
2. Are there regressions -- changed control flow, missing error handling, broken contracts?
3. Does the change stay within declared scope ({file list})?
4. Are there missing tests for new branches or edge cases?
5. Any risky assumptions around null/None/empty/default values?

Here is the diff for the changed files:

{git diff -- <task files> output}

Also read the surrounding context for these files in the repo if needed. Return schema-compliant JSON only.
```

Note: The diff IS embedded in the prompt to avoid cross-task contamination in the working tree (see Appendix C.2). Codex can also read the full files from the repo for surrounding context, but the diff provides a focused review target.

### 7.4 Output Schema (Both Subcommands)

The wrapper always emits this envelope, regardless of Codex success/failure:

```json
{
  "task_id": "NNN",
  "subcommand": "implement|review",
  "outcome": "success|failure|scope_violation|timeout|parse_error",
  "codex_exit_code": 0,
  "codex_output_raw": "truncated to 2000 chars",
  "parsed": { ... },
  "error": "null or error description"
}
```

Where `parsed` contains the Codex JSON output (from the `-o` file) if it was valid JSON, or `null` if parsing failed.

### 7.5 Error Handling

*[Codex input: identified 10 failure modes the orchestrator must handle]*

| Failure | Detection | Recovery |
|---------|-----------|----------|
| Codex timeout | subprocess timeout | outcome=timeout, fallback to Claude |
| Non-zero exit | exit code check | outcome=failure, include stderr |
| Malformed output | JSON parse failure on `-o` file | outcome=parse_error, include raw output |
| Scope violation | delta vs pre-dispatch baseline exceeds task's Files list | delta-only cleanup against pre-dispatch baseline; protected paths skipped and logged; outcome=scope_violation |
| Test failure | re-run test independently | outcome=failure, include test output |
| Over-broad edits | diff touches more files than declared | same as scope violation |
| Stale plan | file listed in task doesn't exist | plan-analyst catches this; if missed, Codex reports blocked |
| Workspace contamination | other agent touched same file | file-lock enforcement in orchestrator prevents this |
| Codex CLI not installed | `which codex` fails | all tasks route to Claude, warn user |
| Environment mismatch | test command fails due to missing venv | outcome=failure, reason=env_issue |
| Exit 0 but no edits | `git diff --name-only` empty after "success" | compare actual diff against reported `files_changed`; if mismatch, outcome=failure |
| Dishonest files_changed | reported files don't match the post-dispatch delta | outcome=failure with `extra.reason=scope_misreport`; test command skipped (`test_result.result="not_run"`); delta-only restore still runs |
| Cleanup failure | `git restore` doesn't fully revert after scope violation | delta filter narrows the failure surface; verify `git status --porcelain` is clean after restore; if not, halt |
| Flaky test false-failure | test fails on re-run due to flakiness, not the change | re-run test once before declaring failure; two consecutive fails = real failure |
| Review contamination | Codex reviewer sees other tasks' uncommitted changes | embed task-specific diff in review prompt (see section 7.3); post-dispatch delta check surfaces any sandbox escape via `extra.sandbox_escape_detected` (outcome unchanged) |
| Protected-path overlap | Codex wrote to executor infrastructure (`_run_log.jsonl`, `.claude/`, `.codex/`, etc.) | cleanup skipped; paths logged in `extra.protected_skipped_tracked` / `extra.protected_skipped_untracked`. Outcome unchanged from the underlying classification. |

### 7.6 Codex Not Available Fallback

If `codex --version` fails during preflight, the system degrades gracefully: all tasks route to Claude Code. A warning is emitted but execution proceeds. This preserves portability for repos where Codex isn't installed.

---

## 8. Cross-Review Protocol

The novel mechanism. Every implementation is reviewed by the opposite agent.

### 8.1 Why Asymmetric Cross-Review

*[Codex input: "Cross-review should be asymmetric on purpose."]*

The two agents have complementary blind spots:

**Codex reviewing Claude's code catches:**
- Scope creep (edits beyond declared files)
- Missing test coverage for new branches
- Implementation discipline violations (uncommitted stash, partial fixes)
- API contract mismatches visible in the diff
- Inconsistency with nearby code patterns
- "Locally plausible but not actually wired" issues

**Claude reviewing Codex's code catches:**
- Domain-specific correctness (routing invariants, trading pipeline rules)
- Silent failure modes (error swallowing, wrong default returns)
- Architecture-pattern violations
- Async/concurrency issues
- Hidden requirements not encoded in the repo
- Cross-file semantic coherence

### 8.2 Review Dispatch

**Claude-implemented tasks -> Codex review:**

```bash
python plugins/plan-executor/scripts/plan_codex_dispatch.py review \
  --task-id NNN \
  --plan-file <path> \
  --files <changed-files> \
  --repo-root <path> \
  --review-focus bugs \
  --json
```

**Codex-implemented tasks -> Claude review:**

Dispatch `code-reviewer` subagent via Agent tool with:
- Scope: files from Codex's `files_changed` report
- Intent: task's Description and Acceptance criteria
- Instruction: "Verify this change addresses the task without regressions or scope creep. Focus on correctness and domain-specific patterns."

### 8.3 Review Outcome Handling

| Implementer | Reviewer | Clean | Minor only | Critical/Important |
|-------------|----------|-------|------------|-------------------|
| Claude | Codex | Commit | Commit + log findings | Escalate: independent Claude code-reviewer evaluates. Agree -> defer. Disagree -> commit + log `[disagreement]` for human review. |
| Codex | Claude | Commit | Commit + log findings | One retry: Claude re-implements the task, Codex re-reviews. Reviewer findings are **not** forwarded to the retry in v1 — identical retries may fail identically. Tradeoff accepted to keep retry scope bounded. Still blocked -> defer. |

**Escalation protocol for Codex critical findings on Claude's work:**

*[Codex input: "Making Codex review non-binding on Claude work undercuts the independence claim."]*

When Codex raises a critical finding on Claude's implementation:
1. The finding is logged with full context.
2. A DIFFERENT Claude instance (`code-reviewer` agent, not the implementing agent) evaluates the finding. This is a third opinion, not self-review.
3. If code-reviewer agrees with Codex -> task is deferred.
4. If code-reviewer disagrees -> task is committed, BUT the Codex finding is preserved in the execution log with `[disagreement]` tag. The user sees these in the summary and can override.
5. Configurable: `--codex-review-binding` flag makes Codex critical findings always block, with no counter-review.

### 8.4 Review Prompt Separation

*[Codex input: "Do not make them duplicate the same checklist."]*

Each agent gets a review prompt tailored to its strengths:

**Codex review prompt emphasizes:**
- File scope compliance
- Test command was run and passed
- No regressions in changed control flow
- Acceptance criteria concretely met
- Implementation matches nearby patterns

**Claude review prompt emphasizes:**
- Domain correctness (forwarded from plan's `## Context`)
- Routing/pipeline integrity
- Error handling completeness
- Cross-file semantic coherence
- Silent failure modes

---

## 9. Skill Specification: `/implement-plan`

**File:** `plugins/plan-executor/skills/implement-plan/SKILL.md`

### 9.1 Arguments

```
/implement-plan <plan-path> [flags]

Required:
  <plan-path>             Path to the plan document

Optional:
  --dry-run               Analyze and classify only, no implementation
  --parallel N            Max concurrent tasks per batch (default: 2)
  --codex-only            Only execute Codex-tier tasks
  --claude-only           Only execute Claude-tier tasks
  --task-ids 1,2,3        Restrict to specific tasks
  --skip-cross-review     Skip cross-review phase (faster, less safe)
  --codex-review-binding  Codex critical on Claude goes straight to fail-task; no third-opinion escalation (§8.4)
  --allow-gaps            Proceed past analyst outcome=needs-enrichment
  --strict-branch         Halt (not warn) if current branch ≠ plan's **Base branch:**
```

*Note on `--skip-analysis` (deferred in v1):* a persisted analyst-JSON sidecar is the prerequisite. Once `plan_ops.py write-schedule` lands (TASK-004 delivers the subcommand per §9.3), a subsequent `/implement-plan` invocation can skip re-running the analyst by reading `docs/plans/<basename>.schedule.json`. Do not add the flag before the writer exists.

### 9.2 Phase 0: Preflight

1. Parse and normalize arguments
2. Read plan document, validate basic structure (header fields, at least one task)
3. Smart dirty-tree check: infrastructure files OK, source files in task scope block
4. Check Codex availability: `codex --version`. If unavailable, set `codex_available=false` (all tasks route to Claude)
5. Record `starting_sha` via `git rev-parse --short HEAD`
6. Generate `run_id` via `date -u +%Y%m%dT%H%M%S`

### 9.3 Phase 1: Plan Analysis

Dispatch `plan-analyst` agent.

```
Agent(subagent_type: "plan-analyst", model: "opus", prompt: <plan-analyst template>)
```

Parse the execution strategy from the analyst's report.

**If `--dry-run`:** print the analysis report and stop.

**If `needs-enrichment`:** print gaps, suggest the user amend the plan. Do NOT auto-enrich (the plan is the user's contract -- *[Codex input: "Don't ask me to both invent the design and implement it"]*).

**If `invalid`:** print errors, halt.

**If `codex_available=false`:** override all `codex` classifications to `claude`.

**Schedule persistence.** Analyst schedules persist at `docs/plans/<basename>.schedule.json`. `plan_ops.py write-schedule --schedule-file <path> --stdin` is the sole writer; SKILL.md MUST NOT write this file directly. Subcommand delivered in TASK-004; until it lands, the orchestrator keeps the schedule in memory for the duration of the run.

### 9.4 Phase 2: Batch Execution

Initialize state:

```
ready         : sorted list of task_ids from analyst's schedule
done          : set = {}
failed        : set = {}
locked_files  : set = {}
```

**Main loop** -- for each batch in the schedule:

#### 2a. Select batch

Take the next group of tasks from the schedule. Verify no file overlap with `locked_files`. Add batch files to `locked_files`.

#### 2b. Dispatch implementations (parallel)

In a SINGLE message, dispatch all batch tasks:

- **Claude tasks:** Agent tool calls with `subagent_type: "plan-implementer"`, model per analyst classification. Use the Phase B dispatch template (section 10).
- **Codex tasks:** Bash tool calls with `plugins/plan-executor/scripts/plan_codex_dispatch.py implement ...`, timeout 300s (see Appendix D.5).

Both dispatch types in the same message = true parallelism.

#### 2c. Handle results

For each task:
- **Success:** record in `done`, release file locks. Proceed to Phase 3.
- **Codex failure:** re-dispatch to Claude as fallback (one attempt). If fallback succeeds, proceed. If fallback fails, mark as `failed`.
- **Claude failure:** mark as `failed`. No fallback.
- **plan-incorrect:** mark as `failed` with special reason. Suggest user amend the plan.

For each failed task: record the failure, release its file locks, and continue with peer tasks independently. Terminal failures do not propagate to other tasks.

Update plan document: set `Status: in-progress` for active tasks, `Status: failed` for failed.

### 9.5 Phase 3: Cross-Review

Process tasks ONE AT A TIME (reviews must be serial to avoid reviewer seeing uncommitted changes from other tasks):

For each successful implementation in schedule order:

**If implemented by Claude -> Codex review:**

```bash
python plugins/plan-executor/scripts/plan_codex_dispatch.py review \
  --task-id NNN --plan-file <path> \
  --files <implementer's files_changed> \
  --repo-root <path> --review-focus bugs --json
```

Parse JSON. Apply outcome matrix from section 8.3.

**If implemented by Codex -> Claude review:**

Dispatch `code-reviewer` agent with the task's files and intent. Parse review report. Apply outcome matrix.

**If `--skip-cross-review`:** skip directly to Phase 4 for all successful tasks.

### 9.6 Phase 4: Commit & Report

For each reviewed-clean task, in schedule order:

1. `git add` specifically the files from the implementer's report
2. Commit:
   ```
   feat(TASK-NNN): <task title>

   <diff summary from implementer>

   Plan: <plan-file-basename>
   ```
3. Update plan document: `Status: done`, note commit SHA

After all tasks:

1. Update plan header `Status:` (complete if all done, partial if some failed)
2. Append execution log entry (section 5 format)
3. Produce summary report to user
4. If any tasks completed, do a housekeeping commit for the plan document changes

**Do NOT auto-push. Do NOT auto-PR.**

### 9.7 Promotion criteria and gates

The executor promotes from dry-run to execute (and from execute to "certified-clean") through six canonical phase gates. Each gate is a pure predicate — the gate code **never invokes the wrapper or dispatches any agent**; it grep-validates artifacts already on disk.

**Gate vocabulary.** Each gate returns `{name, status, reason}` where `status ∈ {pass, fail, not_applicable}`. The dry-run qualifier lives in `reason`, not in `status` — a gate that is inherently skipped in dry-run (e.g., `commit-safe`) returns `not_applicable` with `reason = "dry-run mode; no commits to verify"`. Canonical list (exact names, invoked via `plan_ops.py gates --check <csv>`):

| Gate | Asserted invariant |
|---|---|
| `schema-valid` | Plan markdown conforms to §5 of this document: `## Goal`, `## Context` (or `## Scoped Context`), `## Verification`, and every `### TASK-NNN` block carries the required bullets Status / Priority / Files / Test command / Acceptance criteria + the prose header Description. |
| `schedule-valid` | Analyst JSON passes `_validate_schedule` + `_validate_schedule_dag`. Same validators used by `parse-schedule` — the gate and the writer agree by construction. |
| `fixture-valid` | The sample fixture (`docs/plans/sample_phase4.md`) itself passes `schema-valid` + `schedule-valid` as a self-test that the schema predicates are exercised on a realistic artifact. |
| `execution-safe` | `plan_codex_dispatch.py` implement path carries the always-ignore / protected-paths seam (imports from `_plan_paths`), calls `_snapshot_baseline(` at **two distinct snapshot call sites parsed as separate windows** -- one inside `cmd_implement`'s body (the implement dispatch seam) and one inside the timeout cleanup path (either an explicit `_snapshot_baseline(` call in the `_handle_timeout_cleanup` helper body, or `_handle_timeout_cleanup(..., baseline)` in `cmd_implement`'s timeout branch propagating the captured snapshot through) -- and contains no `git clean -fd` in executable code (docstring and comment references to the prohibition are allowed). |
| `review-safe` | `plan_codex_dispatch.py cmd_review` calls `_snapshot_baseline(` and the module references `is_protected_path` / `PROTECTED_EXACT_PATHS` for protected-path respect. |
| `commit-safe` | Post-hoc: `git show --name-only <commit_sha>` minus TASK-NNN's declared `Files:` list (after stripping `(create)`/`(modify)`/`(delete)` annotations and `:line` range suffixes) minus the shared `COMMIT_ALWAYS_IGNORE` set from `plugins/plan-executor/scripts/_plan_paths.py` (covers `docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, the per-plan `*.schedule.json` sidecar matched via `is_commit_always_ignore`, and the `00_INDEX.json` roster) plus the plan file itself (which `commit-task` legitimately stages alongside each task) is empty. `commit-task`'s own staging logic keys on the same constant so the pre-commit and post-commit sides agree by construction -- drift between the two is exactly the failure mode this set prevents. Inapplicable in dry-run. |

**Dry-run pass condition.** `schema-valid`, `schedule-valid`, `fixture-valid`, `execution-safe`, `review-safe` all return `pass`; `commit-safe` is `not_applicable`. Bundled as `plan_ops.py gates --certify --mode dry-run --plan-file <path> --schedule-file <path>`.

**Execute pass condition.** The dry-run set plus `commit-safe` verified post-hoc against every `commit_done` event for the run. Bundled as `plan_ops.py gates --certify --mode execute --plan-file <path> --schedule-file <path> --run-id <id>`. A run with zero commits reports `commit-safe: not_applicable` — that is not a failure.

**Where the gates run in the skill.** `schema-valid`, `fixture-valid`, `execution-safe`, `review-safe` gate Phase 0 preflight (before the first batch dispatch). `schedule-valid` runs immediately after `write-schedule` persists the analyst JSON in Phase 1. `commit-safe` runs per-commit after each Phase D.3 `commit-task` and again as part of the end-of-run `--certify --mode execute` bundle. The end-of-run certification is a report, not a retry trigger — a failure is surfaced in the summary but does not reopen already-committed tasks.

**Phase 0 preflight halt set.** `schema-valid`, `schedule-valid`, and `fixture-valid` are **all** strict halt-on-fail gates. A `fail` status from any of them halts the run before batch dispatch and logs `run_end reason=preflight_gates_failed`. There is no warning tier, no `--warn-only` flag on the `gates` subcommand, and no fixture-version detection inside the `fixture-valid` predicate -- the status vocabulary remains the frozen `pass|fail|not_applicable`. If `fixture-valid` fails because the canonical sample fixture has drifted from the schema, repair the fixture (the TASK-006 conformance artifact is the canonical repair) rather than widen the gate surface.

**Certification vs. pre-commit guard.** The in-process pre-commit guard inside `commit-task` (ISSUE-038) and the post-hoc `commit-safe` gate both assert the same invariant — that a commit's file footprint matches the task's declared scope. They are redundant on purpose. The guard prevents a bad commit from landing; the gate proves a landed commit was clean. Together they close the execute-bundle loop.

---

## 10. Dispatch Templates

### Phase B -- Claude Implementer

```
Implement this task from the plan at <absolute plan path>.

Plan context:
<## Context section verbatim>

Task (verbatim from plan):
<entire TASK-NNN block>

Base commit SHA: <starting_sha>

You may read the plan file for reference but do not modify it.
Run the test command if specified (use the project's virtual environment).
Return your report in the structured format from your agent spec.
Do not commit. Do not use git stash.

You do NOT have the Agent tool. Do all work directly with Read, Grep, Glob, Edit, Write, Bash.
```

### Phase D -- Claude Reviewer (for Codex implementations)

```
Scope: <comma-separated files from Codex's files_changed>

Intent -- the task's Description and Acceptance criteria:

**Description:**
<verbatim from task>

**Acceptance criteria:**
<verbatim from task>

Verify this change addresses the task without regressions or scope creep.
Focus on correctness and domain-specific patterns from the project.
Flag pre-existing issues in Minor/nits only.

You do NOT have the Agent tool. Do all work directly with Read, Grep, Glob, Bash.
```

---

## 11. Portability

### 11.1 What Is Portable (No Repo-Specific Knowledge)

| File | Content | Portable? |
|------|---------|-----------|
| `plugins/plan-executor/agents/plan-analyst.md` | Role behavior, output schema, classification heuristic | Yes |
| `plugins/plan-executor/agents/plan-implementer.md` | Implementation protocol, report format, rules | Yes |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | Orchestration phases, dispatch logic, commit ceremony | Yes |
| `plugins/plan-executor/scripts/plan_codex_dispatch.py` | CLI wrapper, prompt templates, error handling | Yes |

### 11.2 What Is Repo-Specific (Stays in Project Instructions)

| File | Content |
|------|---------|
| `CLAUDE.md` | Project architecture, signal flow, env setup, test commands, coding conventions |
| `.codex` | Minimal project conventions for Codex (venv path, git rules, test framework) |
| Plan document | Task-specific truth: files, descriptions, acceptance criteria |

### 11.3 Porting to a New Repo

1. Copy the 4 portable files to the new repo
2. Write/update `CLAUDE.md` with the new project's context
3. Write `.codex` with minimal conventions:
   ```
   # <Project Name> -- Codex Instructions

   ## Environment
   - Python: use <venv-path or system python>
   - Test framework: <pytest/jest/etc.>
   - Build: <build command>

   ## Git
   - Do not commit changes.
   - Do not use git stash.
   - Do not modify files outside the scope specified in your prompt.
   ```
4. Write a plan document following the schema in section 5
5. Run `/implement-plan <plan.md> --dry-run` to validate
6. Run `/implement-plan <plan.md>` to execute

No modifications to agent files or skill files required.

---

## 12. File Inventory

### Files to Create

| File | Purpose |
|------|---------|
| `plugins/plan-executor/agents/plan-analyst.md` | Plan validation, task classification, execution strategy |
| `plugins/plan-executor/agents/plan-implementer.md` | Single-task implementation (generalized fix-implementer) |
| `plugins/plan-executor/skills/implement-plan/SKILL.md` | Orchestrating skill -- entry point |
| `plugins/plan-executor/scripts/plan_codex_dispatch.py` | Codex CLI wrapper for implement + review |
| `plugins/plan-executor/scripts/codex_review_schema.json` | JSON Schema for Codex review output (used with `--output-schema`) |
| `plugins/plan-executor/scripts/codex_implement_schema.json` | JSON Schema for Codex implementation output (used with `--output-schema`) |

### Files to Modify

| File | Change |
|------|--------|
| `.codex` | Populate with minimal project conventions |

### Files Reused Without Modification

| File | Role in This System |
|------|---------------------|
| `plugins/plan-executor/agents/code-reviewer.md` | Reviews Codex-implemented tasks |
| `CLAUDE.md` | Provides project context to Claude agents |

---

## 13. Implementation Phases

### Phase 0: Codex CLI Contract Validation (Prerequisite)

Run empirical tests to validate the Codex CLI contract. Each is a standalone experiment.

| Test | Command | Verify |
|------|---------|--------|
| Basic exec | `codex exec --full-auto --ephemeral -C /tmp "Create hello.txt with 'hello'"` | File created, exit 0 |
| Output capture | `codex exec --full-auto --ephemeral -o /tmp/out.txt -C /tmp "Create test.txt. Report: done"` | `-o` captures final message |
| JSON output | `codex exec --full-auto --ephemeral -o /tmp/out.txt ... "Return valid JSON only: {...}"` | Clean JSON in output file |
| Repo-aware read | `codex exec --full-auto --ephemeral -s read-only -C <repo> "Read src/config/settings.py. Report the class names."` | Correct file reading |
| Stdin prompt | `echo "Create hello.txt" \| codex exec --full-auto --ephemeral -C /tmp -` | Stdin prompt works |
| Failure mode | Send nonsensical prompt | Document exit code, error output |
| Scope check | After exec, `git diff --name-only` | Only expected files modified |

**Deliverable:** Documented CLI contract (exit codes, output format, timing, failure modes).

### Phase 1: Wrapper Script

Build `plugins/plan-executor/scripts/plan_codex_dispatch.py`:
1. Plan parser (extract task blocks, validate structure)
2. Prompt renderer (implement + review templates)
3. Codex invocation with subprocess management
4. File scope validation (pre/post snapshot diff)
5. JSON output parsing and envelope
6. `--dry-run` mode

Test standalone against one task from a sample plan.

### Phase 2: Plan-Analyst Agent

Write `plugins/plan-executor/agents/plan-analyst.md`. Test by dispatching against a sample plan:
1. Correct task classification (codex vs claude)
2. Valid execution schedule (priority order and file locks correct)
3. Gap detection (missing test commands, vague criteria)
4. No dependency graph, cycle, or orphan-dependency errors emitted by the analyst

### Phase 3: Plan-Implementer Agent

Write `plugins/plan-executor/agents/plan-implementer.md`. Test by dispatching against one sample task:
1. Correct implementation
2. Test execution
3. Well-formed report
4. File scope compliance

### Phase 4: Orchestrator Skill

Write `plugins/plan-executor/skills/implement-plan/SKILL.md`. Integrate all pieces:
1. Preflight (args, dirty tree, codex check)
2. Plan analysis dispatch
3. Batch execution with dual-agent paths
4. Cross-review dispatch
5. Commit ceremony
6. Plan document status updates
7. Summary report

### Phase 5: End-to-End Test

Create a 4-5 task test plan for this repo. Mix of:
- 2 Codex-tier tasks (trivial, mechanical)
- 2 Claude-tier tasks (multi-file, reasoning)
- At least 2 file-disjoint tasks that can run in parallel
- At least 2 tasks touching the same file so batching splits them by file lock

Run `/implement-plan test_plan.md` and verify:
- Correct routing (codex vs claude)
- Parallel execution within batches
- Cross-review (both directions)
- Commit ceremony
- Plan document updated
- Fallback (force one codex failure)

### Phase 6: Portability Test

Copy the 4 portable files to a different repo. Write a minimal plan. Run it.
Verify no agent/skill file changes were needed.

---

## 14. Verification Plan

Scenarios 1–12 are scored against the phase-gate vocabulary from §9.7. The single canonical pass-condition for Phase 5 end-to-end certification is *"all six gates hold across scenarios 1–12"* — i.e., `plan_ops.py gates --certify --mode execute --plan-file <sample> --schedule-file <sample_sched> --run-id <id>` returns `certified: true` after every scenario's orchestrator run, with `commit-safe` holding for every `commit_done` event the run recorded. Portability (scenario 13) reuses the same certification against a foreign repo.

**Canonical conformance artifact.** The sample plan `docs/plans/sample_phase4.md` and its schedule sidecar `docs/plans/sample_phase4.schedule.json` are the canonical conformance artifacts for Phase 5 certification. They are validated automatically by the `fixture-valid` gate (`plan_ops.py gates --check fixture-valid`), which aggregates `schema-valid` + `schedule-valid` against the pair. `fixture-valid` passing is a **precondition** for any `--certify` run — Phase 0 preflight rejects a dispatch if the canonical fixture does not itself conform to the §5 schema. The fixture is rebuilt to conform by TASK-006 of the hardening plan; its tests live in `tests/scripts/test_plan_ops.py` (`test_sample_phase4_passes_fixture_valid_gate`, `test_sample_phase4_has_no_hardcoded_agent_field`, `test_sample_phase4_has_required_task_fields`).

| Step | What | How | Pass Criteria |
|------|------|-----|---------------|
| 1 | Codex CLI contract | Run Phase 0 experiments | All behaviors documented, no surprises |
| 2 | Wrapper dry-run | `plan_codex_dispatch.py implement --dry-run` | Correct prompt rendered, valid JSON envelope; `execution-safe` gate returns `pass` against the wrapper source |
| 3 | Wrapper real exec | `plan_codex_dispatch.py implement` on trivial task | Fix applied, tests pass, JSON output valid |
| 4 | Wrapper review | `plan_codex_dispatch.py review` on known diff | Findings match expected issues; `review-safe` gate returns `pass` against the wrapper source |
| 5 | Plan-analyst | Dispatch against sample plan | Correct classification, file-disjoint schedule, gaps flagged; `schema-valid` + `schedule-valid` + `fixture-valid` gates all return `pass` against the sample artifacts |
| 6 | Plan-implementer | Dispatch against sample task | Implementation correct, report well-formed |
| 7 | Cross-review (both dirs) | Claude->Codex review + Codex->Claude review | Both produce valid reviews with real findings |
| 8 | Orchestrator dry-run | `/implement-plan sample.md --dry-run` | Analysis report correct, no files changed; `gates --certify --mode dry-run` returns `certified: true` with `commit-safe: not_applicable` |
| 9 | Orchestrator execute | `/implement-plan sample.md` | All tasks done, reviewed, committed; `gates --certify --mode execute --run-id <id>` returns `certified: true` with `commit-safe: pass` for every `commit_done` event |
| 10 | Fallback test | Force codex failure on one task | Auto Claude fallback, task still completed; execute-bundle certification still holds |
| 11 | Failure isolation | Fail one task while peers remain eligible | Failed task is recorded, peer tasks continue independently, no dependent-blocking event is emitted; execute-bundle certification still holds for the peer commits |
| 12 | Plan status tracking | Check plan.md after run | All Status fields updated, execution log appended |
| 13 | Portability | Run on different repo with no agent changes | Works without modification; the same six phase gates pass against the foreign repo |

### 14.1 Drift detection and readiness (TASK-007)

The executor carries a standing self-audit capability — distinct from the per-run phase gates of §9.7 — that cross-references the shipped artifacts (`plan_ops.py`, `plan_codex_dispatch.py`, the Codex schema sidecars, `SKILL.md`, `dispatch-templates.md`, this design doc) against the canonical decisions declared in `plan_ops.py:CANONICAL_CONTRACT`. It is invoked through `plan_ops.py audit` and is the readiness check operators run **before any rerun** and **after any substantive protocol change**.

```bash
python3 plugins/plan-executor/scripts/plan_ops.py audit --json
python3 plugins/plan-executor/scripts/plan_ops.py audit --report-file /tmp/audit.md
```

**Distinction from gates.** A gate failure means *"this run cannot proceed"* (runtime-scoped to a specific execution). A self-audit finding means *"the executor itself has drifted — fix before the next rerun"* (cross-cutting). Audit is intentionally NOT a hard preflight gate; it is advisory at Phase 0 and binding at the operator's discretion before reruns.

**Checks (default-enabled unless marked advisory):**

| Check | What it asserts |
|---|---|
| `status_vocabulary` | `ALLOWED_TASK_STATUSES` matches `CANONICAL_CONTRACT[status_vocabulary]` modulo named alias windows (`open` → `pending`). |
| `schedule_wire_format` | `_validate_schedule` reads the canonical `id` / `index` fields (or the alias window `task_id` / `batch_index` documented in `SCHEDULE_FIELD_ALIASES`). |
| `implementer_report_labels` | `cmd_parse_implementer_report` searches `**Concerns for reviewer:**` and `**Plan adaptations:**` literal labels. |
| `execution_log_columns` | `cmd_finalize_execution_log` writes the canonical six-column header (Task, Agent, Reviewer, Verdict, Commit, Notes). |
| `schemas` | `codex_implement_schema.json[blockers]` is `array of strings`; `codex_review_schema.json` requires `task_id, verdict, findings, scope_ok, acceptance_met, summary` with the canonical verdict enum (§7.2 / §7.3). |
| `portable_tier` *(advisory; `--strict`-only until TASK-008)* | `SKILL.md` / `dispatch-templates.md` carry no `venv/bin/python` literals outside `<!-- portable_tier: legacy-example -->` marker blocks. Graduates to default-enabled after TASK-008 lands. |
| `wrapper_isolation` | `plan_codex_dispatch.py` imports the protected-paths seam from `_plan_paths`, calls `_snapshot_baseline(` at three seams (implement, timeout-cleanup, review), and contains no `git clean -fd` outside comments / docstrings (TASK-003 State-Isolation Contract). |
| `design_doc_orphans` | This design doc references no deprecated stubs (e.g., `--skip-analysis` outside historical / deferred markers); canonical schedule field literals (`id`, `index`) are present alongside any alias mention. |

**Status vocabulary.** Each finding carries `status ∈ {pass, pass_with_alias, fail}`. `pass_with_alias` is a pass — alias windows from TASK-001 are legitimate, and the audit names the active alias explicitly so silent tolerance is impossible. `fail` is the only verdict-flipping status. Default verdict excludes advisory-tier findings; `--strict` includes them.

**Verification rerun integration.** Any rerun of the §14 verification plan above MUST be preceded by a green `audit --json`. The exit code of `audit` is the operator's go/no-go signal; a non-zero exit means an executor invariant has drifted and the verification scenarios would be measuring the wrong thing.

---

## 15. Codex-Proposed Interface Contract

*[This section captures the full interface contract Codex designed when asked "what contract would you want?"]*

### 15.1 Implementation Input (orchestrator -> Codex)

```json
{
  "task_id": "string",
  "objective": "one sentence desired end state",
  "repo_context": {
    "cwd": "/absolute/repo/path",
    "branch": "string or null"
  },
  "scope": {
    "allowed_paths": ["src/example/module.py"],
    "forbidden_paths": ["src/config/", "tests/"]
  },
  "requirements": ["behavior that must change"],
  "non_goals": ["what must not change"],
  "inputs": {
    "plan_excerpt": "task description + implementation notes",
    "context": "plan's ## Context section"
  },
  "constraints": {
    "style_rules": ["follow existing patterns"]
  },
  "validation": {
    "commands": ["venv/bin/pytest tests/risk/ -v"],
    "acceptance_criteria": ["criterion 1", "criterion 2"]
  },
  "escalation": {
    "on_ambiguity": "follow nearest existing pattern",
    "stop_if_tests_fail": true
  },
  "output_mode": "json_status"
}
```

### 15.2 Implementation Output (Codex -> orchestrator)

```json
{
  "task_id": "string",
  "status": "completed|blocked|needs_clarification|partial|failed",
  "summary": "short string",
  "files_changed": ["path1", "path2"],
  "tests_run": [
    {"command": "pytest ...", "result": "passed|failed|not_run", "details": "short string"}
  ],
  "blockers": [],
  "concerns": ["string"],
  "plan_adaptations": ["string"],
  "notes": ["any additional observations"]
}
```

`blockers` is an array of plain strings — see §7.2 for the authoritative shape and `plugins/plan-executor/scripts/codex_implement_schema.json` for the schema constraint. Do not use object-shaped blocker entries.

### 15.3 Review Output (Codex -> orchestrator)

```json
{
  "task_id": "string",
  "verdict": "clean|minor-findings|needs-rework",
  "findings": [
    {
      "severity": "critical|important|minor",
      "file": "path/to/file.py",
      "line": 42,
      "issue": "description of the problem",
      "suggested_fix": "description or null"
    }
  ],
  "scope_ok": true,
  "acceptance_met": true,
  "summary": "1-2 sentence overall assessment"
}
```

### 15.4 Codex Operational Preferences

*[Direct from Codex's self-assessment]*

- Give independently executable tasks
- Pass exact plan doc sections, not "implement the plan"
- Include file boundaries as allowlists
- Include the definition of done
- On ambiguity: "follow nearest existing pattern" (not "stop and ask" -- Codex is non-interactive)
- Prefer smaller sequential tasks over one giant blob
- Keep JSON schemas flat with fixed keys, not deeply nested

---

## Appendix A: Comparison with Existing Pipelines

| Aspect | fix-bugs Pipeline | implement-plan System |
|--------|------------------|----------------------|
| Input | Per-bug files (docs/bugs/) | Single plan document |
| Scope | Bug fixes only | Features, refactors, migrations, bug fixes |
| Agents | Claude only | Claude + Codex |
| Review | Claude self-reviews | Cross-model review |
| Task classification | fix-verifier per bug | plan-analyst per task |
| Dispatch | Agent tool only | Agent tool + Codex subprocess |
| Status tracking | Bug file frontmatter + rename | Plan document inline updates |
| Portability | Tied to this repo's bug format | Portable by design |
| Batch computation | bug_ops.py | File-disjoint batching plus `00_INDEX.json` pre-flight gating |

## Appendix B: Codex CLI Quick Reference

| Command | Purpose |
|---------|---------|
| `codex exec --full-auto --ephemeral -o FILE -C DIR "PROMPT"` | Non-interactive implementation |
| `codex exec --full-auto --ephemeral -o FILE -C DIR - <<< "PROMPT"` | Implementation via stdin |
| `codex exec --full-auto --ephemeral --output-schema SCHEMA -o FILE -C DIR "PROMPT"` | Implementation/review with structured JSON output |
| `codex review --uncommitted -C DIR` | Human-readable review (freeform, NOT for automation) |
| `codex review --base BRANCH -C DIR` | Human-readable review vs branch (freeform) |
| `codex --version` | Check availability |
| `-s read-only` | Sandbox: no file writes |
| `-s workspace-write` | Sandbox: write within workspace (default with --full-auto) |
| `--json` | JSONL event stream to stdout (not one JSON object) |
| `--output-schema FILE` | Constrain final output to a JSON Schema (**`exec` only, not `review`**) |

**Key distinction** *[from Codex]*: `codex review` is the review-specialized path with better default quality but freeform output. `codex exec --output-schema` is the automation path with structured output. For orchestrator use, always prefer `exec --output-schema`.

---

## Appendix C: Codex Review Errata

This section documents findings from Codex's own review of this plan document, and how each was resolved.

### C.1 Worktree Isolation (Critical)

**Finding:** Parallel task execution in a shared working tree contaminates cross-review. Reviewer for TASK-001 sees TASK-003's uncommitted changes.

**Resolution:** Two-level mitigation, matching the proven fix-bugs pattern:
1. **Implementation:** Parallel within batch is safe because the plan-analyst enforces disjoint file scopes per batch. Tasks in the same batch cannot touch the same files.
2. **Review:** Serial per task. The reviewer receives only the task-specific diff (via `git diff -- <task-files>`), not the full working tree state. Review prompts explicitly scope to the task's files.
3. **Commit:** Uses `git commit --only <files>`, which commits only the specified files regardless of other working tree changes. This is the same mechanism the fix-bugs pipeline uses.
4. **Future enhancement:** Git worktrees (`git worktree add`) for full isolation. Deferred to Phase 2 because it adds orchestration complexity without changing correctness for disjoint-file batches.

Codex review dispatch now embeds the diff in the prompt (see revised section 7.3) rather than relying on live working tree reads, which eliminates the contamination vector for Codex reviews.

### C.2 Review Input Contradiction (Critical)

**Finding:** Section 7.3 flow says "compute the diff" then later says "diff is NOT embedded."

**Resolution:** Diff IS embedded in the Codex review prompt. The note claiming otherwise was removed. For `codex exec` review dispatch, the prompt includes the `git diff` output for the task's specific files. This is necessary because (a) the working tree may contain other tasks' changes, and (b) it gives Codex a focused review target.

### C.3 Status Vocabulary (High)

**Finding:** Four incompatible status enums across sections: plan-implementer uses `success|partial|failed|plan-incorrect`, Codex prompt uses `completed|blocked|partial|failed`, wrapper uses `success|failure|scope_violation|timeout|parse_error`, section 15 adds `needs_clarification`.

**Resolution:** Unified into two layers:
- **Agent-level status** (what the implementer reports): `success | partial | failed | plan-incorrect | blocked`
- **Wrapper-level outcome** (what the orchestrator sees): `success | failure | scope_violation | timeout | parse_error | fallback`

The wrapper maps agent statuses to orchestrator outcomes: `success` -> `success`, `partial|failed|plan-incorrect|blocked` -> `failure` (with reason field preserving detail). `scope_violation|timeout|parse_error` are wrapper-detected failures that never come from the agent itself.

Codex's `completed` maps to `success`. Codex's `needs_clarification` maps to `blocked` (Codex is non-interactive, so "needs clarification" means it's stuck).

### C.4 File Operations (High)

**Finding:** Plan schema doesn't distinguish "create foo.py" from "foo.py should exist already."

**Resolution:** The `Files:` field now supports operation annotations:

```
- **Files:**
  - src/api/middleware.py (create)
  - src/api/app.py (modify)
  - src/api/old_middleware.py (delete)
  - tests/api/test_middleware.py (create)
```

Default (no annotation) is `modify`. The plan-analyst flags `modify` entries where the file doesn't exist and `create` entries where the file already exists.

### C.5 Cross-Review Authority Asymmetry (High)

**Finding:** Codex's review is non-binding on Claude's work (Claude can self-overrule), but Claude's review is binding on Codex's work. This undercuts the independence claim.

**Resolution:** Rebalanced. When Codex raises a critical finding on Claude's implementation:
1. The finding is logged with full context.
2. A DIFFERENT Claude instance (code-reviewer, not the implementing agent) evaluates the finding against the code. This is not "Claude overruling Codex" -- it is an independent third opinion.
3. If code-reviewer agrees with Codex -> task is deferred.
4. If code-reviewer disagrees -> task is committed BUT the Codex finding is preserved in the execution log with `[disagreement]` tag for human review.
5. The user can configure `--codex-review-binding` to make Codex critical findings always block.

### C.6 Missing `--output-schema` for Implementation (Medium)

**Finding:** Section 7.2 uses prompt-only JSON instruction for implementation but recognizes `--output-schema` as the reliable path.

**Resolution:** Implementation dispatch should also use `--output-schema`. Added `plugins/plan-executor/scripts/codex_implement_schema.json` to the file inventory. The wrapper writes the schema to a temp file and passes it via `--output-schema`.

### C.7 Missing Failure Modes

**Finding:** Seven additional failure modes not in section 7.5.

**Resolution:** Added to section 7.5:
- Reviewer contamination (mitigated by diff-in-prompt for Codex, scope instruction for Claude)
- Cleanup failure after scope violation (verify `git status` is clean after restore)
- Exit 0 with valid JSON but no actual edits (check `git diff --name-only` against `files_changed`)
- Dishonest `files_changed` reporting (compare against actual `git diff --name-only`)
- Flaky tests causing false fallback (re-run test once before declaring failure)
- Commit contamination from plan-file updates (plan-file changes in separate housekeeping commit)
- New-file vs stale-path ambiguity (resolved by file operation annotations in C.4)

---

## Appendix D: Codex CLI Contract (Phase 0 Empirical Results)

> Validated against `codex-cli 0.120.0` on 2026-04-13.
> All experiments ran in `/tmp/codex_phase0/` using `-C <consuming-project-repo>` for repo-aware tests.
> No experiments modified the repository working tree (one sandbox violation was immediately cleaned).

### D.1 Experiment Results

#### Group A: Basic Execution

| ID | Description | Exit Code | Wall Clock | Result |
|----|-------------|-----------|------------|--------|
| A1 | Baseline exec (create file) | 0 | ~13s | File created with correct content. **Requires `git init` in /tmp dirs** — Codex refuses to run outside a git repo without `--skip-git-repo-check`. |
| A2 | Impossible task (nonexistent file) | 0 | ~8s | **CRITICAL: exit 0 on failure.** Codex reports it cannot find the file but exits clean. Wrapper CANNOT rely on exit code alone. |

#### Group B: Output Capture

| ID | Description | Exit Code | Result |
|----|-------------|-----------|--------|
| B1 | `-o` flag content format | 0 | `-o` captures the **final assistant message only** as plain text (not markdown-fenced, not JSONL). Content is the last thing Codex "said." |
| B2 | `-o` + `--json` coexistence | 0 | Both outputs populated independently. `--json` to stdout produces JSONL event stream; `-o` writes the final message to file. JSONL includes `file_change` events with `path` field — usable for scope validation. |

#### Group C: Structured Output (`--output-schema`)

| ID | Description | Exit Code | Result |
|----|-------------|-----------|--------|
| C1 | Simple schema (3 keys, enum) | 0 | **`json.load(open(f))` works directly** — no fence stripping needed. Enum values respected. Clean, parseable JSON. |
| C2 | Complex nested schema (7 keys, nested objects, enums) | 0 | All 7 required keys present. Nested `tests_run` array with correct object structure. Enum compliance (`"completed"` status). `json.load()` works directly. **Production-complexity schemas are viable.** |

#### Group D: Repo-Aware Reading

| ID | Description | Exit Code | Result |
|----|-------------|-----------|--------|
| D1 | Read-only repo access | 0 | Perfect accuracy: reported correct class count, class names, and line count for `src/config/settings.py`. Read-only respected (no repo modifications). |
| D2 | Read-only + `--output-schema` (review simulation) | 0 | Valid JSON. Boolean `scope_ok` typed correctly. 3 findings grounded in real code (hardcoded DB password, unsafe `getattr` in logging setup, inconsistent `.env` precedence). Verdict enum respected (`"needs-rework"`). **Full review subcommand path validated.** |

#### Group E: Prompt Delivery

| ID | Description | Exit Code | Result |
|----|-------------|-----------|--------|
| E1 | Stdin prompt (`echo "..." \| codex exec ... -`) | 0 | Identical behavior to argument-based delivery. File created correctly. |
| E2 | Long stdin (~1.5KB production-length prompt) | 0 | No truncation. Output references specifics from both beginning and end of prompt. **Stdin is safe for production-length prompts.** |

#### Group F: Failure Modes

| ID | Description | Exit Code | Result |
|----|-------------|-----------|--------|
| F1 | Subprocess timeout (`timeout 15`) | 124 | Exit 124 (timeout). **No `-o` file created after kill.** No partial files in workdir. Wrapper must handle missing `-o` after timeout. |
| F2 | Sandbox violation (`-s read-only` attempting write) | 0 | **CRITICAL: SANDBOX VIOLATED.** `-s read-only` did NOT prevent file creation. `SHOULD_NOT_EXIST.txt` was created in the real repo directory. File was immediately deleted and repo verified clean. **`-s read-only` is NOT a reliable safety net.** Wrapper MUST implement its own scope validation. |

#### Group G: Full Review Path

| ID | Description | Exit Code | Result |
|----|-------------|-----------|--------|
| G1 | Review via `codex exec --output-schema` with embedded diff | 0 | Valid JSON. 1 finding grounded in the actual diff (shared DataManager monkey-patch regression). Verdict coherent (`"needs-rework"`). Boolean and integer types correct. **Validates the entire review subcommand design: diff-in-prompt + `--output-schema` + `-o` works end-to-end.** |

### D.2 Decision Matrix

| Wrapper Design Decision | Experiment | Answer | Implication |
|---|---|---|---|
| Rely on exit code for failure? | A2 | **NO** | Must parse `-o` output every time. Exit 0 does not mean success. |
| `json.load()` directly on `-o`? | C1 | **YES** | `json.load(open(f))` — no fence stripping or extraction needed when `--output-schema` is used. |
| Production-complexity schemas? | C2 | **YES** | Use actual production schemas directly. 7-key nested schemas with enums work. |
| Deliver prompts via stdin? | E1, E2 | **YES** | `Popen(stdin=PIPE)` with up to ~1.5KB prompts. No truncation. |
| `-o` + `--json` coexist? | B2 | **YES** | Capture both. `-o` for final result, `--json` JSONL for `file_change` scope monitoring. |
| `-s read-only` reliable sandbox? | F2 | **NO** | **Cannot rely on sandbox.** Wrapper must validate scope via `git diff --name-only` and `file_change` events post-execution. |
| Timeout leaves partial state? | F1 | **NO** | No `-o` file, no partial files. But workdir may have partial edits — delta-bounded restore via `_handle_timeout_cleanup` (wrapper) against the pre-dispatch baseline; never repo-wide; skip entirely when baseline uncaptured. |
| Review-via-exec-schema viable? | D2, G1 | **YES** | Full path works: `codex exec --output-schema review.schema.json -o output.json "Review..."` |

### D.3 CLI Invocation Contract

#### Implementation Dispatch

```python
import subprocess, json, tempfile, os

def codex_implement(task_prompt: str, workdir: str, schema_path: str,
                    output_path: str, timeout_sec: int = 300) -> dict:
    """
    Invoke Codex for a bounded implementation task.
    Returns parsed JSON result or error dict.
    """
    # Prompt via stdin (validated: E1, E2 — no truncation up to ~1.5KB)
    cmd = [
        "codex", "exec",
        "--full-auto",
        "--ephemeral",
        "-C", workdir,
        "--output-schema", schema_path,
        "-o", output_path,
        "-",  # read prompt from stdin
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=task_prompt.encode(),
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        # F1: no -o file after timeout. Delta-bounded cleanup only — the
        # live wrapper calls `_handle_timeout_cleanup(repo, allowed, baseline)`
        # (see plugins/plan-executor/scripts/plan_codex_dispatch.py) which restores just the
        # task-owned delta against the pre-dispatch baseline. Never run
        # `git checkout -- .` or `git clean -fd` here: both would erase
        # disjoint sibling work and executor-infrastructure state.
        return {"status": "timeout", "exit_code": -1, "raw": None}

    # A2: exit code is UNRELIABLE — always parse output
    if not os.path.exists(output_path):
        return {"status": "no_output", "exit_code": proc.returncode,
                "raw": proc.stdout.decode(errors="replace")}

    # C1/C2: json.load() works directly with --output-schema
    try:
        result = json.load(open(output_path))
    except (json.JSONDecodeError, ValueError) as e:
        return {"status": "parse_error", "exit_code": proc.returncode,
                "raw": open(output_path).read(), "error": str(e)}

    return {"status": "ok", "exit_code": proc.returncode, "result": result}
```

#### Review Dispatch

```python
def codex_review(task_id: str, diff: str, review_prompt: str,
                 workdir: str, schema_path: str, output_path: str,
                 timeout_sec: int = 300) -> dict:
    """
    Invoke Codex for cross-review of an implementation.
    Diff is embedded in prompt (validated: G1).
    Uses read-only sandbox as advisory hint only (F2: not enforced).
    """
    full_prompt = f"""{review_prompt}

Diff:
{diff}

task_id: '{task_id}'. Return schema-compliant JSON only."""

    cmd = [
        "codex", "exec",
        "--full-auto",
        "--ephemeral",
        "-s", "read-only",       # Advisory only — F2 proved unreliable
        "-C", workdir,
        "--output-schema", schema_path,
        "-o", output_path,
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=full_prompt.encode(),
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit_code": -1, "raw": None}

    if not os.path.exists(output_path):
        return {"status": "no_output", "exit_code": proc.returncode,
                "raw": proc.stdout.decode(errors="replace")}

    try:
        result = json.load(open(output_path))
    except (json.JSONDecodeError, ValueError) as e:
        return {"status": "parse_error", "exit_code": proc.returncode,
                "raw": open(output_path).read(), "error": str(e)}

    # F2: verify no unexpected writes after review
    scope_check = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=workdir, capture_output=True, text=True,
    )
    if scope_check.stdout.strip():
        unexpected_files = scope_check.stdout.strip().split("\n")
        result["_scope_violation"] = unexpected_files
        subprocess.run(["git", "checkout", "--", "."], cwd=workdir,
                       capture_output=True)

    return {"status": "ok", "exit_code": proc.returncode, "result": result}
```

#### JSONL Scope Monitoring (Optional)

```python
def codex_implement_with_monitoring(task_prompt: str, workdir: str,
                                     schema_path: str, output_path: str,
                                     allowed_files: list[str],
                                     timeout_sec: int = 300) -> dict:
    """
    Implementation dispatch with --json JSONL monitoring for scope validation.
    B2: -o and --json coexist. JSONL includes file_change events.
    """
    cmd = [
        "codex", "exec",
        "--full-auto",
        "--ephemeral",
        "--json",                # B2: JSONL to stdout, -o to file
        "-C", workdir,
        "--output-schema", schema_path,
        "-o", output_path,
        "-",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=task_prompt.encode(),
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["git", "checkout", "--", "."], cwd=workdir,
                       capture_output=True)
        return {"status": "timeout", "exit_code": -1, "raw": None}

    # Parse JSONL for file_change events to detect scope violations
    scope_violations = []
    for line in proc.stdout.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
            if event.get("type") == "file_change":
                changed = event.get("path", "")
                if changed not in allowed_files:
                    scope_violations.append(changed)
        except json.JSONDecodeError:
            continue

    # Parse -o result
    if not os.path.exists(output_path):
        return {"status": "no_output", "exit_code": proc.returncode,
                "raw": proc.stdout.decode(errors="replace"),
                "scope_violations": scope_violations}

    try:
        result = json.load(open(output_path))
    except (json.JSONDecodeError, ValueError) as e:
        return {"status": "parse_error", "exit_code": proc.returncode,
                "raw": open(output_path).read(), "error": str(e),
                "scope_violations": scope_violations}

    if scope_violations:
        result["_scope_violations"] = scope_violations

    return {"status": "ok", "exit_code": proc.returncode, "result": result,
            "scope_violations": scope_violations}
```

### D.4 Workdir Requirements

Codex refuses to execute in a directory that is not a git repository (A1). For `/tmp`-based workdirs:

```python
def prepare_workdir(path: str) -> None:
    """Ensure workdir is a git repo (Codex requirement from A1)."""
    os.makedirs(path, exist_ok=True)
    if not os.path.isdir(os.path.join(path, ".git")):
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
```

For repo-aware tasks, use `-C <repo_path>` which already satisfies this.

### D.5 Timing Baseline

| Operation | Observed Wall Clock |
|---|---|
| Simple file creation (A1) | ~13s |
| Failed task (A2) | ~8s |
| Structured output, simple schema (C1) | ~15s |
| Structured output, complex schema (C2) | ~45s |
| Repo read + structured review (D2) | ~30s |
| Full review with diff (G1) | ~40s |

Default timeout recommendation: **300s** for implementation tasks, **180s** for review-only tasks. These are 5-10x observed times to account for model variability and network latency.

### D.6 Critical Safety Findings

1. **Exit code is unreliable** (A2): Codex exits 0 even when the task objectively fails. The wrapper MUST parse `-o` output to determine success/failure. Never use `check=True` on Codex subprocess calls.

2. **`-s read-only` sandbox is not enforced** (F2): Codex created a file in the repo despite `-s read-only`. The wrapper MUST:
   - Run `git diff --name-only` after every Codex invocation
   - Compare against allowed file list
   - `git checkout -- .` to revert unauthorized changes
   - Optionally: use `--json` JSONL `file_change` events for real-time scope monitoring (B2)

3. **Timeout kills leave no output** (F1): After `timeout` sends SIGTERM, no `-o` file exists. The wrapper must handle `subprocess.TimeoutExpired` and assume no parseable result.

### D.7 Contract Summary

```
codex exec --full-auto --ephemeral [-s read-only] -C <workdir> \
  --output-schema <schema.json> -o <output.json> [--json] -
```

- Prompt: via stdin (`-`)
- Structured output: `--output-schema` + `-o` → `json.load()` directly
- Monitoring: `--json` to stdout → JSONL with `file_change` events
- Safety: exit code unreliable, sandbox unreliable, timeout kills output
- Workdir: must be a git repo
- Review: embed diff in prompt, NOT live working tree reads
