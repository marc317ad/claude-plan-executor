# TASK-009 — Scale-Aware Large-File Reads

**Parent plan:** [`../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md`](../DUAL_AGENT_EXECUTOR_HARDENING_PLAN_2026-04-14_v3.md) § TASK-009
**Consolidated remediation:** [`../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md`](../../analysis/DUAL_AGENT_EXECUTOR_Consolidated_Remediation_Plan_2026-04-14.md)
**Design contract:** [`../DUAL_AGENT_PLAN_EXECUTOR.md`](../DUAL_AGENT_PLAN_EXECUTOR.md) §8 (dispatch contract), §10 (implementer report)
**Base branch:** `main`
**Audit anchor commit:** `d0f9740`
**Chunk dependencies:** none (formerly TASK-001, TASK-007 — now archived/complete).
**Issues absorbed:** none (new executor capability; raised by postmortem as a scaling constraint).

---

## Goal

Give plans a structured way to tell the executor "this task will read a large file; here is how to target the read so the dispatcher does not fill the agent's context with irrelevant lines." Today the only read tool is `Read <abs>`, which either returns the first 2000 lines or requires the agent to guess `offset`/`limit`. Large-file tasks (editing a 4000-line helper; patching a generated SQL migration; annotating a long protocol spec) waste ~50% of usable context on unrelated lines. This chunk introduces two optional task fields — `**Read targets:**` and `**Symbol targets:**` — plus helper plumbing in `plan_ops.py` that renders targeted reads into the implementer / reviewer dispatch prompt.

## Scoped Context

### Scaling constraint

The postmortem's Phase 5 scenarios were all small-file. In production, the same executor must handle plans whose tasks edit:

- A 3000-line module (e.g., a hypothetical `src/example/large_module.py`).
- A generated SQL migration that is thousands of lines of DDL.
- A long protocol spec (`DUAL_AGENT_PLAN_EXECUTOR.md` itself, currently ~300 lines but conceivably growing).

The `Read` tool enforces a 2000-line default and warns on larger reads. If the agent blindly calls `Read`, it loses context to unrelated lines. If it guesses `offset`, it miscounts. The implementer / reviewer must be handed a pre-computed *targeted* read set before dispatch.

### Two new optional task-schema fields

Both are optional; absence means "read the declared `Files:` in full, subject to the 2000-line default." Presence means "use these hints."

**`**Read targets:**`** — line-range hints per file.

Syntax (markdown bullet list, one per line):

```
- **Read targets:**
  - src/example/large_module.py:1-120
  - src/example/large_module.py:2400-2520
  - tests/example/test_large_module.py:1-40
```

Semantics: dispatcher emits a block in the prompt like:

```
## Pre-read excerpts

### src/example/large_module.py (lines 1-120)
<contents>

### src/example/large_module.py (lines 2400-2520)
<contents>

### tests/example/test_large_module.py (lines 1-40)
<contents>
```

The agent sees the targeted windows upfront; it can still call `Read` with different offsets if it needs more.

**`**Symbol targets:**`** — named Python/TypeScript symbol extraction.

Syntax:

```
- **Symbol targets:**
  - src/example/large_module.py::LargeModule.process
  - src/example/large_module.py::_compute_score
```

Semantics: dispatcher finds the symbol's definition span (via a lightweight AST helper for Python; best-effort regex for other languages) and emits the enclosing block. Falls back to "symbol not found — see full file" with a note in the prompt.

Precedence: if both fields are present, `Read targets:` wins (explicit line ranges); `Symbol targets:` is the ergonomic sugar.

### Relation to scope enforcement

Targeted reads are orthogonal to `allowed_files`. The `Files:` list still dictates *what the agent may write*. Targeted reads let the agent see *more files* than it may write — e.g., read a 3000-line consumer of the function being edited without being allowed to modify it. The dispatcher should support `**Read targets:**` entries that reference files *outside* `Files:` (they're pure read, no scope concern).

### Out of scope in this chunk

- Read-tool retrofit. The existing `Read` tool is kept; targeted reads are a pre-dispatch *addition*, not a replacement.
- Automatic symbol discovery ("find the functions this task touches"). The plan author must declare them.
- Streaming / chunked write-back. This chunk is read-only targeting.

---

## Verification

**V1 — Plan schema docs list the two optional fields.**

```bash
grep -nE '^\*\*Read targets:\*\*|^\*\*Symbol targets:\*\*' docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md
```

Both fields appear in §5 (plan schema) documentation.

**V2 — Helper extracts line-range reads.**

```bash
echo '- **Read targets:**
  - plugins/plan-executor/scripts/plan_ops.py:1-20
  - plugins/plan-executor/scripts/plan_ops.py:200-220' | $PYTHON plugins/plan-executor/scripts/plan_ops.py resolve-read-targets --stdin --json
```

Emits JSON:

```json
{
  "reads": [
    {"file": "plugins/plan-executor/scripts/plan_ops.py", "start": 1, "end": 20, "text": "...20 lines..."},
    {"file": "plugins/plan-executor/scripts/plan_ops.py", "start": 200, "end": 220, "text": "...21 lines..."}
  ],
  "symbols": [],
  "errors": []
}
```

**V3 — Helper extracts Python symbol definitions.**

```bash
echo '- **Symbol targets:**
  - plugins/plan-executor/scripts/plan_ops.py::_resolve_python' | $PYTHON plugins/plan-executor/scripts/plan_ops.py resolve-read-targets --stdin --json
```

Emits a reads entry with the `_resolve_python` function body and accurate start/end line numbers.

**V4 — Missing symbol reports cleanly.**

```bash
echo '- **Symbol targets:**
  - plugins/plan-executor/scripts/plan_ops.py::does_not_exist' | $PYTHON plugins/plan-executor/scripts/plan_ops.py resolve-read-targets --stdin --json
```

Emits `errors: ["plugins/plan-executor/scripts/plan_ops.py::does_not_exist not found"]` and the top-level exit code is 0 (missing symbol is not fatal; just advisory).

**V5 — Line ranges are bounded.**

Request 10000 lines from a 100-line file. Helper clamps to file length with a `"truncated_to"` note; exit 0.

**V6 — Dispatcher integration.**

When `plan_codex_dispatch.py implement` (and the Claude implementer dispatch template) runs against a task with `Read targets:`, the prompt emitted to the agent contains a `## Pre-read excerpts` section with the targeted content.

```bash
$PYTHON plugins/plan-executor/scripts/plan_codex_dispatch.py implement --plan-file <fixture-with-targets> --task-id 001 --dry-run --emit-prompt
```

(Or equivalent dry-run that prints the prompt to stdout.) Assert the `Pre-read excerpts` block is present.

**V7 — Tests green.**

```bash
$PYTHON -m pytest -q tests/scripts/test_plan_ops.py -k read_targets
```

All new tests pass.

**V8 — Existing plans unaffected.**

A plan with no `Read targets:` or `Symbol targets:` produces a dispatch prompt identical to the pre-chunk output (modulo any unrelated TASK-001-008 changes). No regression.

---

## Tasks

### TASK-009: Scale-aware large-file reads

- **Status:** done
- **Priority:** medium
- **Files:**
  - `docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` (§5 schema, §8 dispatch contract)
  - `plugins/plan-executor/scripts/plan_ops.py` (new `resolve-read-targets` subcommand + helpers)
  - `plugins/plan-executor/scripts/plan_codex_dispatch.py` (dispatch prompt template integration)
  - `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (Claude-side templates)
  - `tests/scripts/test_plan_ops.py`
- **Dependencies:** none
- **Test command:** `$PYTHON -m pytest -q tests/scripts/test_plan_ops.py -k read_targets`
- **Acceptance criteria:**
  - Two optional schema fields documented: `**Read targets:**` (line-range hints), `**Symbol targets:**` (symbol extraction).
  - `resolve-read-targets` helper reads a task block (or stdin) and emits structured JSON with `reads`, `symbols`, `errors`.
  - Python symbol extraction uses AST; best-effort regex fallback for other languages with a clear annotation.
  - Line-range requests are clamped to file length with a `truncated_to` note.
  - Dispatcher integration: both Codex and Claude implementer/reviewer prompts include a `## Pre-read excerpts` section when the task has targets.
  - Absent targets → dispatch prompt identical to pre-chunk behavior (no regression).
  - All verification checks V1–V8 pass.

**Description:**
Plans routinely describe work on large files. Without targeted reads, the implementer wastes context on irrelevant lines or misses the relevant ones. Declaring line ranges or symbol names in the plan lets the dispatcher pre-compute useful reads and embed them in the prompt.

**Implementation notes:**
Keep the symbol extractor intentionally narrow — Python AST first, regex fallback with a clear annotation. Don't try to match every language perfectly; the annotation tells the agent when a read is approximate. Prefer clamping over erroring when line ranges exceed file length.

**Reversion guidance:**
If the dispatcher-prompt size balloons (targets too generous), reduce the default upper bound or require operators to tune per plan. Do not revert the field definitions; they are harmless to keep and opt-in.

---

## Implementation Playbook

### Step 1 — schema documentation

`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` §5:

- Add `**Read targets:**` and `**Symbol targets:**` as *optional* task-schema fields.
- Document syntax and semantics (see Scoped Context above).
- State precedence if both are present (explicit wins).

### Step 2 — helper subcommand `resolve-read-targets`

Add to `plugins/plan-executor/scripts/plan_ops.py`:

```python
def cmd_resolve_read_targets(args):
    if args.stdin:
        text = sys.stdin.read()
    else:
        text = _load_text(Path(args.task_file))
    reads, symbols, errors = [], [], []
    for entry in _iter_read_target_lines(text):
        path, start, end = _parse_read_target(entry)
        reads.append(_read_range(path, start, end, errors))
    for entry in _iter_symbol_target_lines(text):
        path, symbol = _parse_symbol_target(entry)
        hit = _locate_symbol(path, symbol, errors)
        if hit:
            reads.append(hit)
            symbols.append({"path": path, "symbol": symbol, "start": hit["start"], "end": hit["end"]})
    print(json.dumps({"reads": reads, "symbols": symbols, "errors": errors}, indent=2))
```

Helpers:

```python
def _read_range(path: str, start: int, end: int, errors: list) -> dict:
    p = Path(path)
    if not p.is_file():
        errors.append(f"{path}: file not found")
        return {"file": path, "start": start, "end": end, "text": "", "missing": True}
    lines = p.read_text().splitlines()
    real_end = min(end, len(lines))
    truncated = real_end != end
    text = "\n".join(lines[start - 1:real_end])
    result = {"file": path, "start": start, "end": real_end, "text": text}
    if truncated:
        result["truncated_to"] = real_end
    return result


def _locate_symbol(path: str, symbol: str, errors: list) -> dict | None:
    p = Path(path)
    if not p.is_file():
        errors.append(f"{path}: file not found")
        return None
    if path.endswith(".py"):
        import ast
        tree = ast.parse(p.read_text(), filename=str(p))
        target = _find_py_symbol(tree, symbol)  # supports "Class.method" dotted names
        if target is None:
            errors.append(f"{path}::{symbol} not found")
            return None
        start, end = target.lineno, target.end_lineno
    else:
        start, end = _regex_symbol_span(p.read_text(), symbol)
        if start is None:
            errors.append(f"{path}::{symbol} not found")
            return None
    return _read_range(path, start, end, errors)
```

CLI wiring:

```python
parser_rrt = subparsers.add_parser("resolve-read-targets",
    help="Resolve **Read targets:** / **Symbol targets:** from a task block")
parser_rrt.add_argument("--stdin", action="store_true")
parser_rrt.add_argument("--task-file", default=None)
parser_rrt.add_argument("--json", action="store_true")  # reserved for future non-JSON mode
parser_rrt.set_defaults(func=cmd_resolve_read_targets)
```

### Step 3 — Codex dispatch template integration

`plugins/plan-executor/scripts/plan_codex_dispatch.py` `cmd_implement` and `cmd_review`:

- Before building the prompt, call `resolve_read_targets(task_block)` inline (import the helper).
- If `reads` non-empty, inject a `## Pre-read excerpts` section into the prompt. Each entry formatted as:

  ```
  ### {file} (lines {start}-{end}{truncated-note})
  ```{lang}
  {text}
  ```
  ```

  Language fence inferred from extension (`.py` → `python`, `.md` → `markdown`, else no fence).

- Append any `errors` below the excerpts as a "Read-target diagnostics" block.

### Step 4 — Claude dispatch template integration

`plugins/plan-executor/skills/implement-plan/dispatch-templates.md`:

- Add a `{{pre_read_excerpts}}` interpolation point in the implementer and reviewer templates.
- Document how the orchestrator populates it (by calling `plan_ops.py resolve-read-targets --task-file <...> --json`).
- When the field is empty, the template must emit nothing (no empty heading).

### Step 5 — Orchestrator wire-up

`plugins/plan-executor/skills/implement-plan/SKILL.md`:

- In Phase B before dispatching either agent, add a step: "Resolve read targets for the task via `$PYTHON plugins/plan-executor/scripts/plan_ops.py resolve-read-targets --task-file <task-block> --json`. Insert the `reads` into the dispatch prompt under `## Pre-read excerpts`."
- State that missing targets (empty output) means no pre-read section.

### Step 6 — tests

`tests/scripts/test_plan_ops.py`:

- `test_resolve_read_targets_line_range_basic` — stdin with a single `src/file:10-20` target; assert output matches exact lines.
- `test_resolve_read_targets_clamps_to_file_length` — request 1-9999 against a 50-line file; assert `truncated_to: 50`.
- `test_resolve_read_targets_symbol_python` — point at a known function in `plugins/plan-executor/scripts/plan_ops.py`; assert start/end.
- `test_resolve_read_targets_symbol_class_method` — point at `Class.method`; assert start/end within the class body.
- `test_resolve_read_targets_symbol_missing_records_error` — non-existent symbol; assert `errors` contains entry; exit 0.
- `test_resolve_read_targets_non_python_regex_fallback` — point at a shell function in `.sh`; regex hit; note in output.
- `test_dispatch_prompt_includes_pre_read_excerpts` — integration: call `cmd_implement` with a task that has `Read targets:`; assert prompt contains the excerpts block.
- `test_dispatch_prompt_unchanged_without_targets` — baseline regression.

### Step 7 — design doc deep-dive

`docs/plans/DUAL_AGENT_PLAN_EXECUTOR.md` §8 dispatch contract:

- Document that dispatch prompts may include a `## Pre-read excerpts` block before `## Task` when targets are declared.
- State that agents MAY use the excerpts as-is or issue additional `Read` calls; the excerpts are a seed, not a gag.

### Step 8 — regression sweep

Full pytest: `$PYTHON -m pytest -q`. All green.

---

## Out of Scope

- **Automatic target inference** — analyzing the task description for implicit read needs. Plan authors declare explicitly.
- **Cross-language symbol extraction** — beyond Python AST + regex fallback, nothing. TypeScript / Go / Rust authors get regex fallback with a clear note.
- **Write-side chunking** — this chunk is read-only.
- **Canonical contract:** TASK-001.
- **Validation / gates:** TASK-002, TASK-005.
- **Wrapper isolation:** TASK-003.
- **Scheduler semantics:** TASK-004.
- **Fixture rewrite:** TASK-006.
- **Self-audit:** TASK-007 (audit check for new schema fields is acceptable follow-up).
- **Portability / preflight:** TASK-008.
- **Global dep locks / bounded logs:** TASK-010, 011.

## Reversion guidance

- **Prompt-size regressions:** reduce the maximum number of targets honored per task, or require operators to split large tasks. Do not silently drop targets — that would make a task appear under-specified.
- **Symbol extractor bugs:** prefer disabling the Python AST path (fall through to regex fallback with a prominent annotation) over reverting the entire subcommand.
- **Schema fields:** safe to leave documented even if implementation regresses; they are optional and absent by default. Do not remove from the schema without also cleaning plans that use them.

## Execution log — 20260425T041800 (paused)

Starting SHA: `2d3e43f3a9a951e6fd4eea8b94603dd0b5370235`  → Ending SHA: `02da51682e602a9dbbd23846bba8e140163fa52a`

| Task | Agent | Reviewer | Verdict | Commit | Notes |
|---|---|---|---|---|---|
| 009 | claude | codex | needs-rework | - | paused after D.2a.5 second needs-rework; remediation in WT |
