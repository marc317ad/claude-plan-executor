# Dual-Agent Plan Executor — Design vs Implementation Gap Report

**Date:** 2026-04-14
**Branch:** `phase-7b5-bug-fixes`
**Head at audit:** `d0f9740` (tree clean apart from the untracked Phase 5 postmortem file)
**Scope:** Phases 1–4 as landed; Phase 5 end-to-end verification attempt (`DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md`).
**Source spec:** `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` (1594 lines, §1–§15 + Appendices A–D).

---

## 1. Verdict

The scaffolding is in place — four portable artifacts (`SKILL.md`, `plan-analyst.md`, `plan-implementer.md`, the dispatch wrapper) plus `plan_ops.py` (13 subcommands) and two JSON schemas — but the system does not execute end-to-end. Three independent wire contracts diverged silently across Phases 1, 2, and 4, and the Phase 4 test suite pinned the divergence by exercising the implementation against itself rather than against the upstream contract. Every issue called out in the postmortem is corroborated against the code; four additional defects and a cluster of smaller gaps are documented below.

The root cause is not any single line of code — it is that the contract between the plan-analyst (Phase 2) and `plan_ops.parse-schedule` / `batch-next` (Phase 1) was never validated with a live analyst emission. Defect A blocks everything downstream of Phase 1 of a run, which is why Phase 5 could only reach three of twelve scenarios before the stop rule tripped.

---

## 2. Postmortem defect verification

Each defect from `DUAL_AGENT_EXECUTOR_PHASE5_Postmortem_1.md §3` was re-verified against the code. All confirmed.

### Defect A — Schedule wire-format schema mismatch (P0)

Confirmed at three sites.

**Plan-analyst emits** (design §6.1 lines 312–345, echoed in `.claude/agents/plan-analyst.md` lines 270–306):

```json
{"tasks": [{"id": "001", ...}], "batches": [{"index": 1, ...}]}
```

**`plan_ops.py parse-schedule` requires** (`scripts/plan_ops.py:250,263`):

```python
for key in ("task_id", "agent", "files", "dependencies"):   # line 250
    if key not in t: errors.append(...)
for key in ("batch_index", "task_ids", "file_locks"):       # line 263
    if key not in b: errors.append(...)
```

**`plan_ops.py batch-next` also keys on the wrong names** (`scripts/plan_ops.py:301, 322-328`):

```python
tid = _normalize_task_id(str(t.get("task_id")))  # line 301
for b in data.get("batches") or []:
    bids = [_normalize_task_id(str(x)) for x in (b.get("task_ids") or [])]
    ...
    batch_index = b.get("batch_index")  # line 328
```

So the mismatch is everywhere `plan_ops.py` reads the schedule — not just `parse-schedule`.

**Test enshrinement.** `tests/scripts/test_plan_ops.py` lines 334, 341, 349, 350, 369, 370, 436, 437 hand-build schedule JSON using `task_id` / `batch_index` directly. 33 tests currently green — but they never consume a real analyst emission. The test suite cannot catch this class of defect by design.

### Defect B — Sample fixture fails required-field check (P0)

Confirmed. `docs/plans/sample_phase4.md` tasks 001–004 are all missing three of the eight fields plan-analyst treats as hard-fail (`.claude/agents/plan-analyst.md:49`):

- `**Priority:**` — missing on every task
- `**Description:**` body — missing on every task (fixture has acceptance criteria but no Description field)
- `**Reversion guidance:**` — missing on every task

Additional non-conformance not called out in the postmortem:

- Uses `## Purpose` instead of the spec's `## Goal` (design §5 line 149; `## Goal` is a required section in the schema example)
- No `## Verification` section (design §5 line 156)
- Uses a non-spec `**Agent:** codex` field (not in design §5; plan-analyst is supposed to classify)
- `**Dependencies:** [001]` uses bracketed format; design §5 line 172 prescribes `none | TASK-NNN, TASK-NNN`

Compare with `tests/scripts/test_plan_codex_dispatch_integration.py` lines 27–71, which embeds a spec-compliant plan (with Priority, Description, Reversion guidance, Goal, Verification) — proving the schema *is* understood, just not applied in the fixture.

### Defect C — Codex wrapper `validate_scope` has no baseline (P0, load-bearing)

Confirmed at `scripts/plan_codex_dispatch.py:414-445`:

```python
def validate_scope(repo_root, allowed_files):
    changed = git_changed_files(repo_root)        # post-invocation snapshot only
    allowed_set = set(allowed_files)
    violations_tracked = [f for f in changed["tracked"] if f not in allowed_set]
    violations_untracked = [f for f in changed["untracked"] if f not in allowed_set]
    ...
```

No pre-invocation snapshot; no always-ignore list for orchestrator state (`docs/plans/_run_log.jsonl`, `docs/plans/_run_lock.json`, `docs/plans/*.schedule.json`). When two Codex implementers run in the same batch (the documented parallel path), each calls this on completion and deletes everything the other wrote plus any orchestrator state and any unrelated untracked work in the repo.

The postmortem notes the review path "does take a baseline." That is misleading. The variable is *named* `baseline_tracked` (`plan_codex_dispatch.py:877`), but it is a post-invocation snapshot — no snapshot is taken before `invoke_codex` runs. The review path has the same structural bug; it is masked only because reviews run serially and `sandbox="read-only"` is advisory-not-enforced (Appendix D F2). Orchestrator state files still get deleted whenever a review runs.

### Defect D — `--task-ids` flag documented but not implemented (P1)

Confirmed. `SKILL.md` line 64, 72, 111–113 documents the flag; `plan_ops.py build_parser()` (lines 693–794) defines thirteen subparsers, none of which implements schedule filtering. `normalize-task-id` exists but only normalizes a single id. The rule at `SKILL.md:316` ("Never write inline Python for plan ops") means the orchestrator has no path to apply the filter.

### Defect E — `venv/bin/python` hardcoded in portable files (P1)

Confirmed: 14 occurrences in `SKILL.md`, 3 in `dispatch-templates.md`. Both files are listed as "Portable — No Repo-Specific Knowledge" in design §11.1. The design prescribes that venv paths live under `CLAUDE.md` / `.codex`; the Python entry point needs to be parameterized.

### Defect F — Stale-lock tolerance in `acquire-lock` (P2)

Confirmed at `plan_ops.py:646-662`:

```python
current = json.loads(RUN_LOCK_PATH.read_text(...))
if plan_abs in current and current[plan_abs].get("run_id") != args.run_id:
    _die(args, ...)            # halts only when the same plan is contested
current[plan_abs] = {...}       # otherwise merges into orphan-shape JSON
```

Only the same-plan-key collision halts. Orphan-shape JSON (`{"pid": 99999}`), PID liveness, lock age/TTL — none of these are checked. `preflight` classifies `_run_lock.json` as `infra_ignored`, so it doesn't halt there either.

### Defect G — `parse-schedule` does not independently validate the DAG (P2)

Confirmed. `plan_ops.py:227-280` checks outcome enum and required fields; no topological sort runs. The DAG logic already exists (plan-analyst Step 5 heredoc; batch-next dependency traversal) — it is just not reused defensively.

---

## 3. Additional gaps the postmortem did not surface

Found during the design ↔ code walkthrough.

### Gap H — Timeout path destroys untracked files (P0, load-bearing)

`scripts/plan_codex_dispatch.py:609-611`:

```python
if codex["status"] == "timeout":
    _git(["checkout", "--", "."], cwd=repo_root)
    _git(["clean", "-fd"], cwd=repo_root)
```

`git clean -fd` removes **all** untracked files and directories in the repo. A Codex timeout during a real run deletes orchestrator state (`_run_log.jsonl`, `_run_lock.json`), any sibling Codex implementer's still-in-flight output, and any unrelated untracked scratch the human user has in the repo. Design §7.5 calls for "outcome=timeout, fallback to Claude" — it does not authorize repo-wide destruction.

**Fix:** after timeout, restore only files the wrapper is responsible for (the task's own `allowed_files`), and snapshot-delta only new untracked files created during the Codex run — same baseline discipline as Defect C. Never `git clean -fd` at repo scope.

### Gap I — Dishonesty check is computed but never enforced (P1)

`scripts/plan_codex_dispatch.py:707-713`:

```python
reported = {normalize_file_path(f) for f in parsed.get("files_changed", [])}
actual = set(scope["changed_in_scope"])
undeclared = sorted(actual - reported)
phantom = sorted(reported - actual)
```

Both lists flow only into the envelope's `extra` block (lines 734, 751). Design §7.5 lines 628–629:

> | Exit 0 but no edits | `git diff --name-only` empty after "success" | compare actual diff against reported `files_changed`; if mismatch, outcome=failure |
> | Dishonest files_changed | reported files don't match actual diff | verify with `git diff --name-only`; restore unreported changes |

Neither path fails the task. Currently just observability, not enforcement.

### Gap J — `parse-implementer-report` drops Plan adaptations (P2)

`.claude/agents/plan-implementer.md:98-99` mandates a `**Plan adaptations:**` section in every implementer report (where the agent records deviations from the plan's suggested approach — critical reviewer context). `plan_ops.py cmd_parse_implementer_report` (lines 354–403) extracts `outcome`, `files_changed`, `diff_summary`, `test_outcome`, `concerns`, `reversion_guidance`. **No extraction of `Plan adaptations`.** The orchestrator can only carry this forward by parsing the raw markdown itself — which `SKILL.md:316` forbids.

### Gap K — Schedule persistence has no tool; orchestrator must write JSON directly (P2)

`SKILL.md:113`:

> Persist the final schedule (after filter rewrites) to `docs/plans/<basename>.schedule.json` for `batch-next` to read.

No `plan_ops.py write-schedule` / `restrict-schedule` subcommand exists. The only path is the orchestrator calling `Write` with a JSON blob it assembled itself — which is fine for a plain echo of the analyst output but becomes brittle as soon as `--codex-only`, `--claude-only`, or `--task-ids` filters mutate the schedule. Defect D is the sharpest instance of this gap; the general shape is "no tool owns the schedule-rewrite path."

### Gap L — Scope_violation outcome is co-opted by orchestrator-state destruction (P1)

When Defect C triggers, `validate_scope` classifies orchestrator state / sibling outputs as violations, restores them (deleting the untracked ones), and emits `outcome=scope_violation` (`plan_codex_dispatch.py:673`). The orchestrator then treats this as a Codex failure and burns its one fallback attempt on a Claude re-implementation — despite the Codex work itself having been correct and in-scope. This is the mechanism by which a fixable wire-format defect became a silent correctness problem in Scenario 7: both Codex runs were internally successful, both were converted to scope_violation failures by the wrapper's post-check. Fixing Defect C fixes this automatically.

### Gap M — `preflight` classifies `tests/` as `infra_ignored` (P2)

`plan_ops.py:191`:

```python
elif path.startswith((".claude/", "docs/", "tests/")):
    dirty["infra_ignored"].append(path)
```

Uncommitted test changes don't block a run. Design §9.2 line 757 says "Smart dirty-tree check: infrastructure files OK, source files in task scope block" — `tests/` is a judgment call and arguably fine, but the current classification silently lets test-file drift into a run. Not urgent; note it.

### Gap N — No test coverage for the parallel-sibling-destruction case (P1)

Postmortem Scenario 7 destroyed orchestrator state in the real repo. `tests/scripts/test_plan_codex_dispatch_integration.py` exercises one Codex dispatch against a trivial task and checks the envelope shape — it does not exercise two parallel dispatches in the same batch, nor the state-file preservation contract. Phase 4 landed without this coverage, which is why the postmortem is the first time the defect showed up.

### Gap O — Design inconsistency: `blockers` field shape (P3, documentation-only)

Design §7.2 line 508 shows `"blockers": []` (empty list, implicitly strings); design §15.2 lines 1134–1136 shows `blockers` as array of objects `{type, details, needs_from_dispatcher}`. `scripts/codex_implement_schema.json:30-33` implements the simpler §7.2 form (array of strings). Implementation is internally consistent; design doc contradicts itself. Low-priority but worth reconciling.

### Gap P — Verdict vocabulary asymmetry is documented but not verified end-to-end (P3)

`SKILL.md:226` and `run-log-schema.md:27` both say Codex reviewer uses `clean | minor-findings | needs-rework` and Claude reviewer (`code-reviewer` agent) uses `ship | ship-with-fixes | needs-rework`. `.claude/agents/code-reviewer.md:54, 72-75` confirms `ship / ship-with-fixes / needs-rework` output. Consistent on paper — but because Scenario 7 stopped before any review ran, the Claude-reviewer-parses-Codex-work path is un-exercised. No defect; flag as unverified.

---

## 4. Design-spec gaps (unrelated to code defects)

### Spec-1 — `--skip-analysis` note at §9.1 line 751

Intentionally dropped from v1 because the plan schema has no analyst-JSON slot. Noted as future work. Not an issue for Phase 4–5 but the schedule-sidecar gap (Gap K above) is the missing prerequisite.

### Spec-2 — Cross-review authority contract on timeout

Design §8.3 Codex-implements row says "One retry: Claude re-implements the task, Codex re-reviews. Still blocked -> defer." `SKILL.md:249-256` adds "Reviewer feedback is NOT forwarded in v1." Consistent with design (design did not require forwarding), but the choice means a retry may produce an identical implementation that fails the same review. Document the tradeoff explicitly in the plan doc.

### Spec-3 — No specification for `write-schedule` subcommand

Ties to Gap K. The design doc describes the schedule JSON as the handoff artifact (§6.1 lines 306–346) but does not specify where/how it gets persisted — the file path `docs/plans/<basename>.schedule.json` appears only in SKILL.md. Add a §9.3 bullet specifying the persistence step and the owning subcommand.

---

## 5. Cross-defect dependency graph (fix order)

```
Defect C (wrapper baseline) -------- blocks -------> Scenarios 7–12, any parallel batch
Gap H (timeout git clean -fd) ------ blocks -------> any real run that hits timeout
Defect A (wire-format) ------------- blocks -------> all runs beyond Phase 1
Defect B (fixture fields) ---------- blocks -------> Scenarios 2, 7–12 specifically
Defect D (--task-ids) -------------- blocks -------> Scenario 6 only
Defect F (stale-lock tolerance) ---- silent corruption, not a hard block
Defect G (DAG check missing) ------- silent corruption, not a hard block
Defect E (venv hardcoded) ---------- portability only, not runtime
Gap I (dishonesty dead code) ------- safety regression, not a hard block
Gap J (Plan adaptations dropped) --- observability loss only
Gap K (schedule persistence tool) -- prerequisite for --task-ids + filter flags
Gap L (scope_violation co-opt) ----- fixes automatically with Defect C
Gap N (parallel test coverage) ----- prevents regression, required after C fix
```

**Minimum viable fix set to unblock Phase 5 verification:**

1. **Defect C + Gap H** — wrapper baseline snapshot on both timeout and post-check paths, plus always-ignore list for orchestrator state. One patch.
2. **Defect A** — pick a side (recommend updating `plan_ops.py` to read `id` / `index` to match the design doc, rather than changing the design) and add a wire-format integration test that pipes a real analyst emission through `parse-schedule`.
3. **Defect B** — rewrite `sample_phase4.md` to the spec; add `## Goal`, `## Verification`, Priority/Description/Reversion guidance per task, drop the `**Agent:**` field, normalize dependency format.
4. **Gap N** — add a regression test that runs two parallel `implement` dispatches in a scratch repo and asserts neither deletes the other's output nor the orchestrator state files.

After that, rerun the twelve-scenario matrix. Defects D/E/F/G and Gaps I/J/K/M/O/P can land in a follow-up pass — they do not block ship.

---

## 6. Alignment with `CLAUDE.md` / user memory

- **Root cause before patching** — this report is the root-cause pass for the Phase 5 failures; no config patches proposed.
- **No comments in inline scripts** — the inline Python in `plan-analyst.md` Step 5 heredoc has no comments. Unaffected.
- **Venv everywhere** — Defect E is a direct violation of the portability principle but does not violate `CLAUDE.md` itself (this repo does require `venv/bin/python`; the issue is that a *portable* skill file encodes a *repo-specific* path).

No validated tuning parameters (`phase_g_config`, `phase_h_sentiment`, `stop_loss_consolidation`) are touched by any of the fixes above. The dual-agent executor is an orthogonal subsystem.

---

## 7. Files referenced (for the fixer)

| File | Lines | Relevance |
|------|-------|-----------|
| `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` | §5, §6.1, §7.2, §7.5, §8.3, §8.4, §9.1–§9.6, §11, §12, Appendix C.3–C.7, Appendix D | Design contract |
| `.claude/agents/plan-analyst.md` | 1–329 | Analyst spec + JSON schema |
| `.claude/agents/plan-implementer.md` | 1–135 | Implementer spec |
| `.claude/skills/implement-plan/SKILL.md` | 1–318 | Orchestrator |
| `.claude/skills/implement-plan/dispatch-templates.md` | 1–130 | Phase A/B/D/D.5 prompts |
| `.claude/skills/implement-plan/run-log-schema.md` | 1–40 | Event catalogue |
| `scripts/plan_ops.py` | 250, 263, 301, 322–328 (Defect A); 414 onward (validate_scope has no peer); 646–662 (Defect F); 191 (Gap M); 354–403 (Gap J) | Phase 1 tools |
| `scripts/plan_codex_dispatch.py` | 414–445 (Defect C); 609–611 (Gap H); 707–713 (Gap I); 673–689 (Gap L); 875–896 (same class as C on review) | Codex wrapper |
| `scripts/codex_implement_schema.json` | 30–33 | Gap O |
| `scripts/codex_review_schema.json` | 1–40 | Consistent with design |
| `docs/plans/sample_phase4.md` | whole file | Defect B |
| `tests/scripts/test_plan_ops.py` | 334, 341, 349, 350, 369–370, 436–437 | Test enshrinement of Defect A |
| `tests/scripts/test_plan_codex_dispatch_integration.py` | 27–71 | Spec-compliant fixture — counter-example for Defect B |

---

## 8. Summary

Phases 1–4 produced a well-structured scaffolding whose pieces do not speak to each other. The plan document schema (§5), the plan-analyst JSON contract (§6.1), and the `plan_ops.py` wire format drifted independently. A test suite written against the implementation rather than the contract hid the drift. A wrapper built without a baseline snapshot discipline converts correct Codex runs into destructive ones as soon as two run in parallel — which is the documented hot path. And four portable artifacts contain 17 repo-specific Python invocations.

None of these are architectural problems with the dual-agent idea. Every one is a local contract-enforcement issue. The fix order in §5 above unblocks the Phase 5 verification without redesigning any component.
