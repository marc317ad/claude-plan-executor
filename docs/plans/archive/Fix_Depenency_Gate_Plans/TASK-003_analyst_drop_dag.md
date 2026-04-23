# TASK-003 — Drop DAG, dep-cycle, and `external-dep` from `plan-analyst.md`

**Base branch:** `main`
**Source plan:** `~/.claude/plans/polymorphic-foraging-scone.md` (section C)

---

## Goal

Rewrite `plugins/plan-executor/agents/plan-analyst.md` so the analyst no longer parses or reasons about task `Dependencies:`. The analyst stops building a DAG, stops emitting `external-dep` gaps, stops treating dep cycles as invalid, and replaces the Step 5 topo-sort heredoc with a flat file-disjoint batcher. Cross-plan dep resolution is owned by the orchestrator's pre-flight gate (TASK-004).

## Verification

1. `grep -nE 'external-dep|cross-plan|check-plan-deps|dependency cycle|topological' plugins/plan-executor/agents/plan-analyst.md` — zero hits.
2. `grep -n '"dependencies"' plugins/plan-executor/agents/plan-analyst.md` — zero hits inside example JSON tasks or the `tasks[*]` bullet list.
3. Manual read: Step 5 inline Python embeds the flat file-disjoint batcher template documented below; the topo-sort heredoc is gone.
4. Invoke the analyst against a fixture plan whose task dependencies name a sibling-plan ID; outcome is `valid` (not `needs-enrichment`) so long as no other gap exists.
5. `grep -nE '\bblocked\b|depends on TASK-|sequential dep|dependency cycle' plugins/plan-executor/agents/plan-analyst.md` — zero hits. Guards the report-summary token, the worked-example DAG labels, and residual cycle prose.

---

## Tasks

### TASK-003: Rewrite plan-analyst.md to drop DAG reasoning

- **Status:** done
  > Landed 2026-04-17 in commit `68954dd`. `plugins/plan-executor/agents/plan-analyst.md` contains zero `dependen*` references — the analyst schedules by file-lock disjointness and priority only. Worker-layer correctness fix; remains in effect under the two-layer model documented in `docs/analysis/TASK_DEPENDENCY_DAG_Architecture_Inconsistency.md`.
- **Priority:** high
- **Files:**
  - `plugins/plan-executor/agents/plan-analyst.md`
- **Dependencies:** none
- **Test command:** `none`
- **Acceptance criteria:**
  - Line 48 required-fields list becomes `Status, Priority, Files, Test command, Acceptance criteria, Description, Reversion guidance` (no `Dependencies`).
  - Line 49 invalid-conditions bullet no longer mentions `A dependency cycle is detected in Step 5`.
  - Lines 53, 208, 229, 235, 329 — every mention of `external-dep`, `cross-plan`, and `check-plan-deps` is deleted. The analyst never emits an `external-dep` gap.
  - Step 5 (lines 83–166) replaces the inline-Python DAG heredoc with a flat file-disjoint batcher using the following template:

    ```python
    import json
    tasks = json.loads(r'''[...{"id": "NNN", "priority": "high", "files": [...]}...]''')
    PRIO = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    key = lambda t: (PRIO.get(t["priority"], 3), t["id"])
    pending = sorted(tasks, key=key)
    batches = []
    while pending:
        batch, locks, rest = [], set(), []
        for t in pending:
            f = set(t["files"])
            if f & locks:
                rest.append(t)
            else:
                batch.append(t["id"]); locks |= f
        batches.append({"index": len(batches)+1, "task_ids": batch, "file_locks": sorted(locks)})
        pending = rest
    print(json.dumps({"batches": batches}))
    ```

  - JSON contract (lines 282–333): example tasks (lines 294, 304) drop `"dependencies": [...]`; the `tasks[*].dependencies` bullet (line 325) is deleted; the allowed `gap[*].type` list (line 329) drops `external-dep`; the strict-contract sentence (line 333) loses `orphan dependencies, and dependency cycles`.
  - Step 8 outcome matrix (216–236): `invalid` conditions are now: missing plan, malformed frontmatter, duplicate task IDs, non-canonical IDs, missing required field (excluding Dependencies). `needs-enrichment` conditions are: stale paths, unresolvable tests, missing-test-command, vague-ac, empty-implementation-notes. Dep-cycle and external-dep are removed entirely.
  - Report summary (line 248): rewrite `**Tasks:** <total> — <N> claude, <M> codex, <K> blocked` to `**Tasks:** <total> — <N> claude, <M> codex`. The `<K> blocked` token is deleted; scheduler-blocked state no longer exists after TASK-001/TASK-002 strip `block-dependents` and the `blocked` ready-set from the executor.
  - Step 7 Risks list (line 213): delete the bullet `No integration test in ## Verification that covers tasks with dependencies on each other.` The analyst no longer reads `Dependencies:`, so this risk is uncomputable. The other two Risk-types bullets (same-file-across-batches, codex-scope-near-threshold) remain unchanged.
  - Step 8 invariant prose (lines 222, 226): on line 222, strike `or any cycle was found in Step 5` from the `invalid`-precedence clause — cycles are no longer detected. On line 226, change the parenthetical from `(cross-task dependency notes, scope flags, etc.)` to `(same-file-across-batches notes, scope flags, etc.)`. Both edits remove residual DAG-era framing from an otherwise-preserved invariant.
  - Execution Schedule example (lines 259–261): rewrite the worked example to flat priority + file-lock batching. No `(sequential dep)` label and no `— depends on TASK-NNN` trailers. Replace with three parallel-annotated batches, e.g.:
    ```
    - Batch 1 (parallel): TASK-001 (codex), TASK-003 (claude)
    - Batch 2 (parallel): TASK-002 (claude), TASK-005 (codex)
    - Batch 3 (parallel): TASK-004 (codex)
    ```
    Each batch is one pass of the Step 5 file-disjoint loop in descending priority order; serialization between batches is implicit (file-lock conflict only), never declared.
  - Any line the source plan does not explicitly touch remains unchanged (e.g., Step 6 tier classification, Step 7 non-dep gap types, the file-existence check in Step 3).

**Description:**
The analyst no longer builds a DAG. With intra-plan dep gating stripped from `plan_ops.py` (TASK-001) and cross-plan resolution moved to the orchestrator pre-flight (TASK-004), the analyst's role reduces to: parse plan structure → classify tasks → batch by file-lock disjointness → emit gaps for stale paths and unresolvable tests.

**Implementation notes:**
The analyst file is prose + embedded code blocks. Edit the specific lines enumerated in acceptance criteria; do not rewrite unrelated paragraphs. Keep the worked-example JSON consistent with the trimmed schema — tasks in the example block must not carry a `dependencies` field.

**Reversion guidance:**
Revert the file to HEAD; the orchestrator pre-flight gate (TASK-004) and intra-plan strips (TASK-001) are the compensating edits. A lone revert of this file re-introduces the DAG requirement and the analyst will start emitting `external-dep` gaps that the orchestrator no longer resolves.
