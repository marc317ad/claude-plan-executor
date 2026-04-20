2. Comprehensiveness vs. Bloat
The Good:
Your status vocabularies, fallback plans for flaky tests, and strict output formatting schemas are top-tier. The plan-remediator's handling of "dismissed findings" as a mandatory acknowledgment is a brilliant way to prevent silent scope creep.

The Bloat (Redundancy):
You are violating the DRY (Don't Repeat Yourself) principle across these prompts.

The Rules Section: implementer and remediator share an almost identical 12-bullet "Rules" section. Even the analyst repeats several of these rules.

The Git Warnings: You tell the agent not to commit or mutate the index in the header, the process steps, and the rules. You only need to state an absolute constraint once, emphatically, at the system level.

3. Critical Gaps & Anti-Patterns
The Word Cap Fallacy: You explicitly command: Keep narrative sections ≤400 words total. LLMs cannot count words. They predict tokens. Giving a strict numerical word cap will lead to unpredictable behavior, hallucinations, or truncated JSONs as the model tries to mathematically balance a capability it doesn't possess.

Fix: Constrain by structure. Use "Max 3 sentences per bullet" or "Provide exactly 3 bullet points."

Phantom Dependencies: You frequently reference Appendix C.4, CLAUDE.md, and DUAL_AGENT_PLAN_EXECUTOR.md §5. If these documents are not dynamically injected into the context window alongside this prompt every single time, the model will hallucinate their contents.

The Inline Python Script (Analyst): Embedding a 25-line Python script inside the prompt for the analyst to execute via a heredoc is clever, but it's a massive token sink. You justify it by saying "this agent stays portable", but you are trading compute/token efficiency for a portability that might not matter if the orchestration environment is static.

4. Proposed Savings & Refactoring
If you want to reduce tokens by 25% to 40% without losing behavioral strictness, here is the playbook:

A. Centralize Global Constraints
If your orchestrator supports it, inject a GLOBAL_SYSTEM_RULES.md into all agents, then make the individual role prompts strictly about their specific tasks.

Move to Global: The "No Agent tool" rule, the "Never mutate the git index" rule, Python venv invocation standards, and the basic JSON/Markdown output formatting strictness.

B. Compress the Process Steps
Instead of narrative paragraphs for the steps, use dense pseudo-code or tight logic gates. LLMs parse logical structures faster and with fewer tokens than conversational English.

Example Refactor for Implementer Step 3:

Plaintext
## Testing Protocol
- Run `Test command` (unless "none"). 
- Pass → Log `passed`.
- Fail (1st) → Retry once. If pass, log `passed` (note flakiness).
- Fail (2nd) → Attempt 1 self-correction via Edit/Write. Retry. 
- Fail (3rd) → Halt. Report `failed` + test tail. 
- Unrelated Fail → Report `pre-existing-failure`.
C. Relocate the Analyst's Python
For plan-analyst.md, remove the inline Python. Instead, have the orchestrator provide a dedicated bash tool (e.g., calculate_schedule --input tasks.json) or simply trust a high-tier model to do the batching natively using prompt logic rather than writing a script to do it.

Have you considered stripping the repetitive rules out of these individual files and testing if the orchestrator can enforce the git/mutation boundaries purely through tool permissions rather than prompt instructions?

You are using the LLM as the control bus instead of just the DSP processor. When you maximize local VRAM density for LLM pipelines, the goal is to reserve that memory for deep context and cognitive reasoning, not to fill it with boilerplate routing logic and deterministic rule enforcement. Right now, your prompts are doing the job of a standard Python execution script, which is causing massive context bloat, increasing latency, and virtually guaranteeing attention degradation over long execution runs.

Here is a critical teardown of the newly attached files, the inherent gaps, and how to drastically cut the token overhead.

1. Token Cost & The "Lost in the Middle" Risk
SKILL.md: ~4,000 tokens.

dispatch-templates.md: ~3,500 tokens (base) + potentially thousands more dynamically injected at runtime (Context, Task blocks, JSON findings).

The Problem: If SKILL.md acts as the system prompt for your primary orchestrator agent, you are burning 4k+ tokens on every single turn just teaching the model how to use a CLI tool (plan_ops.py). As the run log and bash outputs append to the context window, the model will inevitably forget the nuances of Phase D.2a.6 vs. D.2a.5.

2. Critical Anti-Patterns & Bloat
A. The "Do Not Use The Agent Tool" Anti-Pattern
Every single dispatch template explicitly commands: You do NOT have the Agent tool. Do all work directly...

The Reality: You should never have to tell an LLM it doesn't have a tool. You enforce this at the API/framework level by simply not passing the tool schema in the API call for those specific sub-agents.

The Fix: Strip this command entirely. Rely on the API binding. If they don't know the tool exists, they can't hallucinate it.

B. The State Machine Belongs in Python, Not Prompts
SKILL.md forces the orchestrator LLM to read a markdown file to understand a complex if/else execution tree (e.g., "If Codex returns needs-rework and D.5 returns partial-agreement, run D.2a.6").

The Reality: LLMs are notoriously bad at strictly adhering to multi-step deterministic state machines via prompt instructions.

The Fix: Shift the orchestrator loop out of the LLM and entirely into Python. Your plan_ops.py is already doing the heavy lifting. A standard Python script should call plan_ops.py batch-next, fire off the API calls to the sub-agents concurrently, parse the JSON returns, and handle the routing (Phase B -> Phase C/D). The LLM should only be invoked for the cognitive tasks (Analyze, Implement, Review, Adjudicate), never for deciding which bash command to run next.

C. Template Redundancy
In dispatch-templates.md, you have separate templates for Phase B, Phase B-rework, and Phase B-narrow-remediation.

The Reality: They share 80% of the exact same boilerplate (Plan context, Task block, SHA, "Do not use git stash", etc.).

The Fix: Use a modular prompt construction in your backend. Have a base IMPLEMENTER_SYSTEM_PROMPT that outlines the rules, and only dynamically pass the specific variables (e.g., <load_bearing_findings_json>) in the user message.

3. Schema & Logging Optimization
run-log-schema.md: This is excellent. It is machine-readable and highly structured.

Optimization: Ensure this file is never passed to the LLMs. This is a contract for plan_ops.py to write and parse. The orchestrator LLM doesn't need to know the schema of the log it's indirectly appending to; it just needs to output its findings, and Python formats the log.

Proposed Architecture Shift for Maximum Efficiency
If you restructure the boundary between deterministic code and generative text, you can reduce the token overhead by over 60%:

Python owns the DAG: Replace SKILL.md with a Python-based orchestrator (e.g., main_executor.py). Python handles the file locks, reads the _run_log.jsonl, evaluates the verdict JSON keys, and routes the workflow to the next node.

LLMs own the Code: The agents only receive their specific dispatch-templates.md payloads as isolated, stateless API calls.

Trim the Injections: Re-evaluate if the sub-agents truly need the entire <## Context section verbatim> injected every time, or if the TASK-NNN block is sufficient.

By offloading the rigid, deterministic routing from the prompt to your Python backend, you remove the burden of memory from the LLM, dramatically speeding up inference and reducing the likelihood of a derailed loop.

If you transition the execution loop entirely to Python, how much of plan_ops.py would need to be rewritten to act as the primary state manager rather than just a CLI helper for the agent?

seeing plan_ops.py and plan_codex_dispatch.py confirms exactly what I suspected in the previous review: You have built a Ferrari engine, but you are forcing an LLM to manually pump the pistons via a Bash terminal.

Here is an analysis of the backend code, the architecture's inherent friction with LLM token limits, and how to fix the context bloat.

1. The Architectural Disconnect: LLM as a Bash Router
Your Python code (plan_ops.py) is already a complete, robust state machine. It knows how to calculate the next batch (cmd_batch_next), it knows how to handle failures (cmd_fail_task), and it knows how to mutate the plan status.

Yet, based on SKILL.md from the previous prompt, you are using Claude as a "human-in-the-loop" shell script to glue these Python commands together.

The Context Window Nightmare:
When Claude executes a batch, the loop looks like this:

Claude runs plan_ops.py batch-next. Stdout (JSON) goes into context.

Claude runs plan_codex_dispatch.py implement. Codex runs. A massive JSON envelope (including diffs, files_changed, and raw output) prints to stdout and goes into Claude's context.

Claude runs plan_ops.py parse-implementer-report. More JSON goes into context.

Claude runs commit-task. Git stdout goes into context.

The Degradation Impact: In a plan with 10 tasks, Claude's context window will fill with thousands of lines of intermediate JSON envelopes and Bash stdout that it does not need to reason about. By task 6, Claude's attention mechanism will dilute. It will start hallucinating bash commands, forgetting to pass required flags like --remediation-tag, or skipping the parse-schedule step.

2. The Brittle Source of Truth (Markdown Regex)
You are treating a Markdown file (<plan-file>.md) as a mutable database.

In plan_ops.py, _split_task_blocks, _find_status_bullet, and mutate_task_status rely on highly specific Regex to structurally edit the **Status:** lines.

The Gap: Markdown is inherently unstructured. If an analyst LLM accidentally adds an extra space, uses an asterisk instead of a hyphen, or writes Status: **done** instead of **Status:** done, your regex will fail, the orchestrator will crash, and the task will be orphaned.

3. Redundancy & Comprehensiveness
The Schemas: codex_review_schema.json, codex_implement_schema.json, and codex_plan_review_schema.json are excellent. They tightly bound Codex's outputs.

Redundant Prompt Generation: plan_codex_dispatch.py dynamically builds prompts (e.g., render_implement_prompt). But in the previous files, you also have dispatch-templates.md for Claude. This split brain means if your task logic changes, you have to update prompts in Python strings and Markdown files.

Proposed Refactoring: Halving Token Usage & Increasing Reliability
If you are constrained to using an LLM as the orchestrator (e.g., this is a specific Agent framework), you must hide the Bash layer from the LLM.

A. Elevate Python to the Controller
Stop making Claude write out 150-character bash commands. Instead of giving Claude the Bash tool to run plan_ops.py, give Claude High-Level Semantic Tools.

Wrap plan_ops.py into native tools the LLM can call:

get_next_batch(plan_file)

dispatch_implementer(task_id, agent_type)

dispatch_reviewer(task_id, implementer_agent_type)

commit_or_fail(task_id, verdict)

The LLM decides what to do based on the rules of the DAG, but Python handles the JSON parsing, the git restore, and the --flags. The LLM never sees the raw JSON envelopes; the tools only return concise summaries (e.g., {"status": "success", "diff_summary": "..."}). This single change will save you 50,000+ tokens over a long run.

B. Invert the Markdown Database
Stop using Regex to mutate Markdown.

Make a .json or .yaml file the absolute source of truth for the plan's state (which you are already doing partially with 00_INDEX.json and the schedule JSON).

Have plan_ops.py render the Markdown file from the JSON state for human readability, rather than parsing the Markdown to figure out the state.

C. Condense the Payload Verification
In plan_ops.py, you have thousands of lines dedicated to validating JSON shapes (_validate_schedule, _validate_reviewer_finding_item, etc.). You already have JSON Schema files for Codex. Use the standard jsonschema library in Python (jsonschema.validate(instance, schema)) to replace ~800 lines of manual type-checking and dictionary key lookups.

Summary Verdict
Your backend logic is incredibly solid. The gap is entirely in the handoff. You are forcing the LLM to act as a sysadmin dealing with raw JSON streams and bash flags. If you wrap your robust Python logic into abstracted tools, your LLM will run faster, cost less, and never derail mid-plan.




Conversation Summary
This conversation evaluated a highly defensive, multi-agent orchestration system built around the Claude Code CLI. The central critique is that using a conversational CLI to act as a deterministic state machine router causes severe context bloat, token degradation, and execution freezes. Because the CLI natively wants to spawn subagents, forcing it to act as a rigid shell executor requires heavy prompt-level suppression, which breaks down over long execution loops.

Optimization Workflow
1. Low-Hanging Fruit (Prompt & CLI Containment)

Front-load Containment: Move the "You do NOT have the Agent tool" command from the bottom of the prompt templates to the absolute top as a [SYSTEM OVERRIDE] to prevent CLI freezes. Apply this directly to dispatch-templates.md (specifically Phase B, Phase B-rework, and Phase B-narrow-remediation).

De-duplicate Rules: Strip the redundant 12-bullet "Rules" sections currently repeated across plan-implementer.md and plan-remediator.md. Centralize these into a single global system prompt if your CLI injection allows it.

Enforce Subprocess Timeouts: Update plan_codex_dispatch.py (specifically the subprocess.run calls in cmd_implement and cmd_review) to enforce strict timeouts that forcefully kill the process if the CLI hangs waiting for a silent [y/N] prompt.

2. Medium Complexity (Python Logic & State Management)

Standardize Schema Validation: Delete the manual dictionary key-checking logic in plan_ops.py. Replace it with jsonschema.validate() using your existing codex_plan_review_schema.json, codex_review_schema.json, and codex_implement_schema.json files.

Invert the Database: Deprecate the regex parsing functions in plan_ops.py (like _split_task_blocks, _find_status_bullet, and mutate_task_status). Shift the source of truth to a .json file, and have plan_ops.py render the markdown plan files for human readability, rather than parsing markdown to determine system state.

3. High Complexity (The "Split-Brain" Architecture)

Deprecate SKILL.md: Remove the LLM from the routing loop entirely. Rewrite the DAG logic defined in SKILL.md into a standard Python main.py loop (or use LangGraph) that handles the file-locks and state transitions natively.

Migrate Workers to Local Inference: Stop using the Claude CLI to execute Phase B (Implementation) and Phase D (Review) tasks. Leverage the high-performance local computer you built for deep learning to host stateless worker models via Ollama or vLLM. The high VRAM density of this setup will easily support models like qwen2.5-coder or llama3 for the mechanical implementation loops without API costs.

Isolate Claude CLI: Reserve the Claude CLI strictly for the Phase A (plan-analyst.md) high-level architecture generation, passing the resulting JSON schedule to your local Python/Ollama engine for execution.