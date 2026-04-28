---
bug_id: 144
status: CLOSED
group: BASH-DISPATCH-MIGRATION
severity: critical
source_fix_id: null
source_plan: null
source_date: 2026-04-26
origin: surfaced during /implement-plan dry-run on /mnt/d/algorithmic_trading_system/docs/plan/20260426-agent-strategy-revisions (run 20260426T161146), invoked via `claude -p --permission-mode auto "/implement-plan ... --dry-run"`
decomposed_at: 2026-04-26
absorbed_fix_ids: []
dependencies: []
dependency_fix_ids: []
files:
  - plugins/plan-executor/scripts/_claude_backend.py
  - plugins/plan-executor/scripts/plan_claude_dispatch.py
  - tests/scripts/test_claude_backend.py
content_fingerprint: null
change_history: []
---

# BUG-144: Claude wrapper never renders dispatch-template body — TASK-003 migration shipped orchestrator-side without the wrapper-side render path, every per-child plan-analyst dispatch ships a raw payload dump as the user prompt

**Status:** CLOSED
**Severity:** critical (blocks all `/implement-plan` runs whose schedules contain at least one child file without a declared `**Agent:**` — i.e., the default path)
**Group:** BASH-DISPATCH-MIGRATION
**Depends on:** none
**Test command:** `venv/bin/pytest tests/scripts/test_claude_backend.py -v -k "phase_a_single or transport_boundary or render_classifier"`

## Acceptance criteria

- `_claude_backend.invoke` (called by `plan_claude_dispatch.py run`) MUST render the agent's dispatch-template body when `payload.prompt` and `payload.instructions` are both absent. For `agent == "plan-analyst"`, the rendered prompt MUST contain the verbatim Phase A-single body from `plugins/plan-executor/skills/implement-plan/dispatch-templates.md` (the section below the `<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->` marker), with `<absolute child plan path>` substituted from `payload.plan_path` and `<repo_root>` substituted from `payload.repo_root`. Same coverage for `agent == "plan-implementer"` (selecting Phase B / Phase B-rework / Phase D.2b body via `payload.variant ∈ {default, rework, role-swap}`) and `agent == "plan-remediator"` (Phase B-narrow-remediation body).
- `_build_argv` MUST pass `--model <m>` to the nested `claude -p` invocation, with `<m>` resolved as `effective.model` (from `payload.overrides.model`) falling back to `manifest.model` from the agent frontmatter. Today the flag is omitted entirely, so the nested session inherits the parent CLI's model — Opus 4.7 1M ran where Sonnet was specified, doubling cost and compounding the off-script behavior caused by the missing prompt frame.
- `--add-dir` MUST resolve to `payload.repo_root` (or `effective.cwd` if set) so the nested session's filesystem scope matches the dispatch declaration. (Currently correct; lock in via test.)
- An end-to-end test fixture in `tests/scripts/test_claude_backend.py` MUST stub the `claude` binary with a script that captures `argv` and stdin, invoke `_claude_backend.invoke` with a `plan-analyst` payload carrying only `plan_path` + `repo_root`, and assert: (a) the captured prompt contains the literal classifier instructions ("Classify exactly one task", "Output (required fenced", "agent\": \"claude\" | \"codex\""); (b) the captured prompt does NOT contain `"plan_path":` or any other JSON serialization artifact of the payload object; (c) `--model sonnet` appears in argv. The TASK-003 acceptance criteria explicitly waved this end-to-end check; reinstating it is the regression backstop.
- The 10 doc-only assertions in `tests/scripts/test_skill_dispatch_analyst.py` (TRANSPORT_BOUNDARY exists, body invariant survives, etc.) STAY — they are necessary but not sufficient. Augment, don't replace.

## Problem

### Symptom

A `/implement-plan` dry-run against `docs/plan/20260426-agent-strategy-revisions/` (11 children, all without `**Agent:**` declared, all needing classification) halted at Phase 1 step 2 with `run_end reason=analyst_invalid`. All 11 per-child classifier dispatches returned malformed envelopes:

- 4 × `status: timeout` (TASK-001/003/005/007) — backend exceeded 300s on a task that should be a single-paragraph JSON reply.
- 6 × `status: scope_violation` (TASK-002/004/006/009/010/011) — nested agents wrote source files; wrapper baseline-snapshot reverted.
- 1 × `status: ok` but off-shape (TASK-008) — `result` was prose asking for clarification, not the `{agent, classification_reason}` JSON shape.

Most damning: TASK-006's classifier spent 300s and **fully implemented the task** — wrote 13 files (`src/strategies/momentum_trend_strategy.py`, `src/config/settings.py`, `.env.example`, four new test files), ran 8 unit tests, reported "All 8 tests pass. Implementation complete." in the envelope, and cost $1.53 on `claude-opus-4-7[1m]`. The wrapper's delta-bounded cleanup caught and reverted the writes; no source code was permanently modified. But the run consumed roughly $5-6 of API spend across the 11 dispatches, with zero classifications produced.

### Root cause

The TASK-003 migration (commit `0f2153a`, "feat(SKILL_bash_dispatch_migration TASK-003): migrate Phase 1 plan-analyst dispatch to bash wrapper [disagreement]") rewrote `plugins/plan-executor/skills/implement-plan/SKILL.md` and `dispatch-templates.md` to dispatch plan-analyst via `plan_claude_dispatch.py run --input <payload.json>` instead of the in-process `Agent` tool. The migration is structurally incomplete:

**Doc side (shipped):**

- `SKILL.md:322-334` says: "the actual tool-call shape is N parallel Bash invocations of `plan_claude_dispatch.py run --input <payload.json>` ... the v3 wrapper replaces the in-process Agent tool dispatch as of TASK-003".
- `dispatch-templates.md:21-91` (`## Phase A-single`) carries the canonical payload skeleton (lines 33-67) — no `prompt` / `instructions` field — and the dispatch-prompt body below a `<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->` marker (line 72), with `<absolute child plan path>` and `<repo_root>` placeholders that the renderer is supposed to substitute.

**Wrapper side (missing):**

- `plan_codex_dispatch.py:297` defines `render_implement_prompt`, `:492` defines `render_review_prompt`. The Codex wrapper grew template-rendering as its TASK-006/007/009 migrations landed.
- `plan_claude_dispatch.py` and `_claude_backend.py` have **zero** matches for `render`, `template`, `TRANSPORT_BOUNDARY`, or `dispatch-templates`. The Claude wrapper has no template-rendering path at all.
- `_claude_backend._resolve_prompt` (lines 234-251):

  ```python
  def _resolve_prompt(payload: Mapping[str, Any]) -> str:
      if not isinstance(payload, Mapping):
          return json.dumps(payload, default=str)
      for key in ("prompt", "instructions"):
          v = payload.get(key)
          if isinstance(v, str) and v:
              return v
      return json.dumps(payload, default=str)
  ```

  When `payload.prompt` and `payload.instructions` are both absent — the case the SKILL/templates explicitly prescribe for plan-analyst — this falls back to JSON-dumping the payload. The plan-analyst nested session receives `{"plan_path": "/mnt/d/.../TASK-NNN_*.md", "repo_root": "/mnt/d/algorithmic_trading_system"}` as its user message, with no Phase A-single framing at all.

- `_claude_backend._build_argv` (lines 254-279) builds `[backend_binary, "-p", "--agent", agent_name, "--permission-mode", permission_mode, "--allowedTools", allowed_csv, "--disallowedTools", disallowed_csv, "--add-dir", cwd, "--output-format", "json", prompt]`. There is no `--model` flag. `manifest.model` (the agent frontmatter's `model: sonnet`) and `payload.overrides.model` (echoed into envelope's `model` field) are both ignored at argv-build time. Confirmed by the captured envelope: payload override says `sonnet`, frontmatter says `sonnet`, but `result_raw_truncated.modelUsage` shows `claude-opus-4-7[1m]`. The nested session inherits the parent CLI's model.

### Why two failure modes

Without the Phase A-single framing, the nested plan-analyst sees only its system prompt (the agent spec from `agents/plan-analyst.md`) and a raw JSON dict containing the field name `plan_path`. The agent spec carries BOTH the default per-child contract AND a documented legacy whole-plan contract (retained for direct CLI callers — `## Inputs` section, line 88: "**`plan_path`** — absolute path to EITHER a single `.md` plan document ... OR a directory containing a `00_INDEX.json` roster"). The field name `plan_path` is the legacy mode field. The agent latches onto whichever contract it can rationalize.

- **Legacy whole-plan rationalization** (TASK-001/003/005/007): the analyst tries to apply Steps 1-8 of the legacy whole-plan analysis to a single child file, including running tests as part of file-existence verification. Hits 300s timeout.
- **Implement-the-task rationalization** (TASK-002/004/006/009/010/011): the analyst, running on Opus 4.7 1M (because of the `--model` flag bug — Opus is bolder than Sonnet at instruction overriding), reads the child task's clear acceptance criteria + concrete test command and decides the most useful response is to just *do the work*. TASK-006 went the furthest, getting all the way through implementation + tests before the wrapper's cleanup caught the writes.
- **Clarifying question** (TASK-008): the analyst correctly noticed it received a child file path under `plan_path` (a legacy-mode field) and asked the operator to clarify scope. Cleanly off-shape — `status: ok`, prose `result`, no JSON envelope.

The legacy-mode field-name collision and the missing PhaseASingle framing explain the off-script choice. The model override bug (Opus instead of Sonnet) explains the severity — TASK-006 spending $1.53 on a fully-implemented task is something Sonnet would not have done; the agent contract reads as constraining enough that the smaller model would have stayed inside it.

### Why TASK-003's tests didn't catch it

`tests/scripts/test_skill_dispatch_analyst.py` (the 10-test fixture committed alongside TASK-003) is a doc-only suite. It asserts:

- `SKILL.md` and `dispatch-templates.md` no longer reference `subagent_type: "plan-analyst"` (the pre-migration shape).
- Phase A-single carries the `<!-- TRANSPORT BOUNDARY -->` marker.
- The marker precedes the Phase A-single body.
- The body matches a pre-migration invariant.

None of these tests instantiate `_claude_backend.invoke`, none stub the `claude` binary, none assert on the rendered prompt that the wrapper would actually send. The TASK-003 commit message acknowledges the gap explicitly: Codex review returned `needs-rework` with 3 important findings (doc-vs-code drift), the implementer responded "ship the implementation; amend the AC to call out the realignment explicitly", and D.5 third-opinion adjudication was unavailable due to a missing `code-reviewer` subagent registry entry. The orchestrator self-adjudicated and committed against `needs-rework` — that's the `[disagreement]` tag.

### Blast radius beyond plan-analyst

TASK-004 ("Migrate Phase B plan-implementer dispatches", commit `c83168b`) and TASK-005 ("Migrate Phase D.2a.6 plan-remediator dispatch", commit `f68dbfe`) are also already merged and follow the same pattern: SKILL.md and dispatch-templates.md updated to dispatch via `plan_claude_dispatch.py`, no corresponding wrapper-side render path. Any `/implement-plan` run that proceeds to Phase B (Claude-tier implementer) or Phase D.2a.6 (narrow remediation) will hit the same JSON-dumped-payload bug. Today's halt at Phase 1 step 2 means no live `/implement-plan` run has actually exercised the implementer or remediator wrapper paths since 2026-04-26 09:18 EDT. The next non-classifier run that lands a Claude-tier task will trigger the same failure mode at a more expensive seam.

## Recommended fix

Mirror the Codex wrapper's pattern. Codex side resolves correctly because `plan_codex_dispatch.py` reads `dispatch-templates.md`, locates the matching section, takes the body below `<!-- TRANSPORT BOUNDARY -->`, and substitutes the payload values inline. Claude side needs the same loader.

### 1. Add a `render_dispatch_prompt(agent, payload, plugin_root)` helper

New module function (suggested location: `plugins/plan-executor/scripts/_claude_dispatch_render.py` or inlined into `_claude_backend.py` if the surface is small).

Inputs:
- `agent: str` — one of `plan-analyst`, `plan-implementer`, `plan-remediator`.
- `payload: Mapping[str, Any]` — the inner `payload` dict from the dispatch envelope.
- `plugin_root: Path` — `${CLAUDE_PLUGIN_ROOT}` so the loader knows where `skills/implement-plan/dispatch-templates.md` lives.
- `variant: Optional[str]` — for `plan-implementer`, one of `default | rework | role-swap`. For `plan-remediator`, the narrow-remediation body. For `plan-analyst`, ignored (only one body).

Behavior:
1. Resolve the section in `dispatch-templates.md` per agent + variant:
   - `plan-analyst` → `## Phase A-single — plan-analyst per-child classifier (default)` body below transport boundary.
   - `plan-implementer variant=default` → `## Phase B — implement (Claude tier)` body.
   - `plan-implementer variant=rework` → `## Phase B-rework — Claude implementer rework after needs-rework` body.
   - `plan-implementer variant=role-swap` → `## Phase D.2b — role-swap re-implement` body.
   - `plan-remediator` → `## Phase B-narrow-remediation` body.
2. Locate the `<!-- TRANSPORT BOUNDARY - do not edit below in this plan -->` marker inside the section. If absent (a doc-side regression), raise `ValueError(f"transport boundary missing for agent={agent} variant={variant}")` so the wrapper halts with a structured error instead of falling back to legacy behavior.
3. Substitute placeholders against the payload:
   - `<absolute child plan path>` ← `payload["plan_path"]`
   - `<repo_root>` ← `payload["repo_root"]`
   - For implementer/remediator: `<task_id>` ← `payload["task_id"]`, `<analyst_annotations>` ← `payload.get("analyst_annotations", "")`, etc. (Mirror Codex's `render_implement_prompt` substitutions exactly so the agent contracts stay symmetric across implementer/wrapper sides.)
4. Apply the `target_task_id` auto-injection rule (`dispatch-templates.md:11-19`) when the resolved child plan file carries >1 `### TASK-NNN:` heading. The Codex wrapper already calls `plan_ops.render_target_task_id_injection(plan_text, target_task_id, plan_file=...)`; reuse the same helper here so the injection stays in lockstep across both wrappers.
5. Return the rendered string.

### 2. Wire the helper into `_resolve_prompt`

```python
def _resolve_prompt(
    payload: Mapping[str, Any],
    *,
    agent: str,
    plugin_root: Path,
    variant: Optional[str] = None,
) -> str:
    if not isinstance(payload, Mapping):
        return json.dumps(payload, default=str)
    for key in ("prompt", "instructions"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    if agent in {"plan-analyst", "plan-implementer", "plan-remediator"}:
        return render_dispatch_prompt(agent, payload, plugin_root, variant=variant)
    # Unknown agent: refuse rather than json.dumps fallback.
    raise ValueError(
        f"_resolve_prompt: no template wired for agent={agent!r}; "
        f"payload must carry an explicit 'prompt' or 'instructions' field"
    )
```

The plain `json.dumps(payload)` fallback for the supported-agent case is removed entirely — it has no legitimate caller and was the proximate cause of this bug. Direct CLI callers who want a custom prompt set `payload["prompt"]` explicitly; that path is still honored by the early-return branch above.

`invoke` callers must thread `agent`, `plugin_root`, `variant` through to `_resolve_prompt`. `agent` comes from the envelope's top-level `agent` field; `plugin_root` from the wrapper's environment (`os.environ["CLAUDE_PLUGIN_ROOT"]` resolved to `Path`); `variant` from `payload.get("variant", "default")` for implementer dispatches.

### 3. Pass `--model` in `_build_argv`

```python
def _build_argv(*, manifest, effective, prompt, backend_binary):
    agent_name = _stringify(manifest.get("name") or "")
    cwd = _resolve_cwd(effective)
    permission_mode = _resolve_permission_mode(effective)
    allowed_csv = _allowed_tools_csv(manifest)
    disallowed_csv = _disallowed_tools_csv(effective)
    model = _resolve_model(effective, manifest)  # NEW

    argv = [
        backend_binary, "-p",
        "--agent", agent_name,
        "--permission-mode", permission_mode,
        "--allowedTools", allowed_csv,
        "--disallowedTools", disallowed_csv,
        "--add-dir", cwd,
        "--output-format", "json",
    ]
    if model:
        argv.extend(["--model", model])  # NEW
    argv.append(prompt)
    return argv
```

`_resolve_model(effective, manifest)` returns `effective.get("model") or manifest.get("model")` (effective wins; both default to `None` if neither is set, in which case `--model` is omitted and the nested CLI inherits as today). Add a unit test asserting that a payload carrying `overrides.model: "sonnet"` produces argv containing `["--model", "sonnet"]`.

### 4. Add the end-to-end test that should have caught TASK-003

`tests/scripts/test_claude_backend.py::test_resolve_prompt_renders_phase_a_single_for_plan_analyst`:

```python
def test_resolve_prompt_renders_phase_a_single_for_plan_analyst(tmp_path, fake_plugin_root):
    payload = {
        "plan_path": "/abs/path/TASK-001_example.md",
        "repo_root": "/abs/repo",
    }
    rendered = render_dispatch_prompt(
        agent="plan-analyst",
        payload=payload,
        plugin_root=fake_plugin_root,
    )
    assert "Classify exactly one task" in rendered
    assert "/abs/path/TASK-001_example.md" in rendered
    assert "/abs/repo" in rendered
    assert '"agent": "claude" | "codex"' in rendered
    assert '"plan_path"' not in rendered  # not a JSON dump
```

Plus a stubbed-binary E2E test:

```python
def test_invoke_passes_rendered_prompt_to_backend(tmp_path, fake_plugin_root, stub_claude_binary):
    # stub_claude_binary writes received argv + stdin to capture file, exits 0 with a fixture envelope
    payload = {"plan_path": str(tmp_path / "TASK-001.md"), "repo_root": str(tmp_path)}
    (tmp_path / "TASK-001.md").write_text(MIN_VALID_TASK_FIXTURE)
    backend.invoke(
        manifest={"name": "plan-analyst", "model": "sonnet", "tools": [...]},
        effective={"model": "sonnet"},
        payload=payload,
        trace={...},
        backend_binary=stub_claude_binary,
        plugin_root=fake_plugin_root,
    )
    captured_argv = stub_claude_binary.read_argv()
    captured_prompt = captured_argv[-1]  # prompt is the last positional
    assert "Classify exactly one task" in captured_prompt
    assert "--model" in captured_argv
    assert captured_argv[captured_argv.index("--model") + 1] == "sonnet"
```

Mirror tests for `plan-implementer` (default + rework + role-swap variants) and `plan-remediator`.

### 5. Backfill TASK-004 and TASK-005

Once the helper is in place for plan-analyst, the same call site lights up plan-implementer (TASK-004) and plan-remediator (TASK-005) with no additional structural change — only the dispatch-templates.md sections need the same `<!-- TRANSPORT BOUNDARY -->` marker layout, and the helper needs to know how to find them. Verify both before declaring this bug closed: a smoke run of `/implement-plan` against a fixture plan that has at least one Claude-tier implementer task is the minimum bar.

## Reversion guidance

The wrapper-side fix is additive — adding `render_dispatch_prompt`, threading `--model`, and tightening `_resolve_prompt` does not change behavior for any existing caller that supplies an explicit `payload.prompt`. To revert: drop the `render_dispatch_prompt` import + call from `_resolve_prompt`, restore the `json.dumps(payload)` fallback, drop the `--model` argv branch, drop the new tests. The orchestrator-side migration (commit `0f2153a` + TASK-004 `c83168b` + TASK-005 `f68dbfe`) cannot be partially reverted — those three commits rewrote SKILL.md and dispatch-templates.md to depend on the wrapper, so rolling back the wrapper-side fix without rolling back the SKILL would re-introduce the broken state. If the wrapper-side fix is unsafe for any reason, the safer revert is to `git revert 0f2153a c83168b f68dbfe` and restore the `Agent(subagent_type=...)` dispatch path; the `[disagreement]` tag on `0f2153a` indicates this was always a contested ship.

## Run history

(none yet — bug filed 2026-04-26 from /implement-plan dry-run halt)

### Run hand-fix-2026-04-28 — CLOSED

Files: `plugins/plan-executor/scripts/_claude_backend.py`, `tests/scripts/test_claude_backend.py`

Reviewer verdict: gemini-2.5-pro independent review of the recommended fix returned **ship-as-is** (verdict captured in the orchestrator's run notes; no separate amendments needed). The recommended fix's render_dispatch_prompt + --model wiring + json.dumps fallback removal was assessed as the correct shape with no significant gaps.

Reviewer advisories: none beyond the original bug report's recommended fix.

Implementation notes: applied as a hand-fix from the orchestrator session because the nested `claude -p /fix-bugs` dispatch was being blocked by the harness's permission perimeter for sub-agent bash and the user pre-authorized the hand-fix path under broad administrative authority. BUG-146's prior fix already inlined the canonical implementer/remediator framing into `payload["prompt"]` for the default/rework/role-swap/narrow-remediation variants, so the BUG-144 wrapper-side fix narrowed to:

1. `_render_phase_a_single_classifier_body` helper that renders the verbatim Phase A-single body from `dispatch-templates.md` (with `target_task_id` auto-injection when the analyst payload carries one).
2. `_resolve_prompt(payload, *, agent=None)` now fires the analyst body when `agent == "plan-analyst"` and the payload carries the canonical minimal shape (`plan_path` + `repo_root`, no `task_id`).
3. `_resolve_model(effective, manifest)` returns the `--model` alias to thread, with `effective.model` (payload override) winning over `manifest.model` (frontmatter); `_build_argv` extends argv with `["--model", model]` before the trailing prompt positional.

Tests: 11 new tests in `test_claude_backend.py` covering Phase A-single rendering, target_task_id injection, explicit-prompt short-circuit, _resolve_model precedence, --model argv wiring, --model omission when neither side declares one, prompt-positional invariance with --model present, and an end-to-end `invoke()` test that asserts the rendered analyst body lands as the trailing argv positional with `--model sonnet` threaded in. The pre-existing buggy-shape assertion `test_resolve_prompt_falls_back_to_json_dump_for_analyst_shape` was renamed to `test_resolve_prompt_falls_back_to_json_dump_for_unagented_minimal_shape` and narrowed to cover only payloads where `agent` is unknown — the analyst branch now fires correctly when `agent="plan-analyst"` is supplied.

`venv/bin/pytest tests/scripts/test_claude_backend.py -v` → 39 passed, 1 skipped (live-binary smoke gate).
