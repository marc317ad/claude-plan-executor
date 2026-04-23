# Plan: Gemini CLI Integration — General Purpose Wrapper (Refined)

**Created:** 2026-04-21
**Status:** draft — specification + implementation plan
**Objective:** Create a standardized, modular Python wrapper for the Gemini CLI (`gemini`) that allows other agents, scripts, and CI/CD pipelines to leverage Gemini's advanced features (Web Search, Codebase Investigator, etc.) via a structured JSON interface.

---

## 1. Goal

Build a "Gemini Dispatch" system that abstracts the Gemini CLI into a programmable interface. This enables:
- **Claude/Codex Delegation:** Claude or Codex agents can shell out to Gemini for deep research or codebase analysis.
- **Structured Reasoning:** Gemini's output is parsed and validated against JSON schemas, making it reliable for orchestration.
- **Tool Empowerment:** Specifically exposes Gemini's high-signal tools (`google_web_search`, `codebase_investigator`, `web_fetch`) to systems that otherwise lack them.

## 2. Architecture

The system lives in the `gemini_integration/` root directory to ensure it is not coupled to the plan-executor plugin.

```
/gemini_integration/
├── lib/
│   ├── __init__.py
│   ├── env_factory.py     # Manages GEMINI_CLI_HOME isolation
│   ├── dispatcher.py      # Core logic for calling `gemini -o json`
│   ├── envelope.py        # I/O JSON envelope construction
│   ├── guardrails.py      # Policy Engine and recursive depth checks
│   └── roles/             # Specialized role prompt templates
├── schemas/
│   ├── input.schema.json
│   └── output.schema.json
└── gemini_dispatch.py     # CLI Entry point
```

### 2.1 The Dispatch Loop

1. **Input:** Caller provides a JSON payload (Task, Role, Context, Constraints).
2. **Preflight (Isolation):**
   - Wrapper creates a unique `GEMINI_CLI_HOME` directory.
   - Injects `settings.json` (disabling telemetry) and `policies/restrictive.toml` (denying `run_shell_command`).
3. **Execution:**
   - Wrapper invokes `gemini -p "PROMPT" -o json --approval-mode=plan`.
   - Any large `context` from the input is piped to `stdin`.
4. **Extraction & Validation:**
   - Wrapper parses the top-level CLI JSON output.
   - Extracts the `response` field and strips markdown fences.
   - Parses the inner JSON and validates against the requested schema.
   - Maps CLI stats (tokens, latency) and exit codes to the envelope.
5. **Output:** Returns a standardized JSON envelope.

## 3. Gemini-Specific Capabilities

| Role | Primary Tools Used | Use Case |
|---|---|---|
| **Researcher** | `google_web_search`, `web_fetch` | Finding documentation, troubleshooting, research. |
| **Investigator** | `codebase_investigator`, `grep_search` | Architectural analysis, dependency mapping. |
| **Architect** | `enter_plan_mode`, `cli_help` | Designing strategies, verifying CLI capabilities. |

## 4. Metadata Schema

The wrapper extracts these from the Gemini `stats` object:
- `tokens`: Breakdown of input, output, cached, thought, and tool tokens.
- `latency_ms`: Total API latency.
- `model`: The specific model that generated the response.

## 5. Tasks

### TASK-001: Isolation & Environment Factory
- Implement `lib/env_factory.py` to create unique, ephemeral `GEMINI_CLI_HOME` directories.
- Ensure telemetry is disabled and a restrictive `policies/` directory is created.

### TASK-002: Core Headless Dispatcher
- Implement `lib/dispatcher.py` to invoke `gemini -p -o json`.
- Handle `stdin` for context and `-p` for the task.
- Map exit codes (0, 1, 42, 53) to `status` vocabulary.

### TASK-003: Metadata & Stats Parser
- Implement logic to extract and normalize the complex `stats` object.
- Identify the "main" model from the results.

### TASK-004: Role Templates & Schema Validation
- Implement standard prompts for `Investigator` and `Researcher` roles.
- Use `jsonschema` to validate the inner model response.

### TASK-005: Verification & E2E Tests
- Implement the "Empirical Verification Matrix" as automated tests.
- Ensure concurrent dispatches using different `GEMINI_CLI_HOME` paths are isolated.

---

## 6. Verification (Empirical Matrix)

| Test | Command | Expected Result |
|---|---|---|
| **JSON Structure** | `gemini -p "hi" -o json` | JSON with `session_id`, `response`, `stats`. |
| **Stdin Context** | `echo "FILE_CONTEXT" \| gemini -p "repeat context" -o json` | `response` contains "FILE_CONTEXT". |
| **Exit Code 42** | `gemini -p ""` | Exit code `42`. |
| **Isolation** | `GEMINI_CLI_HOME=/tmp/iso_test gemini -p "hi"` | `/tmp/iso_test/.gemini/` is created. |
| **Policy Denial** | `mkdir -p /tmp/pol/.gemini/policies && echo '[[rule]]\ntoolName="run_shell_command"\ndecision="deny"' > /tmp/pol/.gemini/policies/deny.toml && GEMINI_CLI_HOME=/tmp/pol gemini "ls"` | Gemini refuses to run `ls`. |
