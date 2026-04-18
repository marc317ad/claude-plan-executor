plugins\plan-executor\skills\implement-plan\SKILL.md is a seriously impressive and rigorously designed piece of agentic architecture. The level of thought put into safety rails, cross-agent verification, and failure recovery is top-tier. By enforcing strict invariants through a Python wrapper (plan_ops.py) and isolating git operations from the LLM, you've solved many of the common pitfalls of autonomous coding agents.

However, your instinct about context bloat is spot on. The document suffers from "imperative micromanagement." You are using the LLM prompt as both a highly complex state machine and a literal script execution engine. This burns through context windows, increases the risk of the LLM hallucinating its state (like forgetting what's in locked_files), and makes the skill brittle to changes.

Here is an honest breakdown of the design, along with actionable ways to truncate, generalize, and simplify it without sacrificing your safety constraints.

1. Overall Impression: The Good & The Bad
The Good (Keep this): The dual-tier architecture (Claude for heavy lifting, Codex for fast/focused tasks) and the cross-review asymmetry are excellent. The strict adherence to commit --only and isolating the agent from running raw git stash or git restore commands is exactly how you keep the repo safe.

The Bad (Fix this): The prompt forces the LLM to act as a literal while loop. You are asking a language model to maintain local variables (ready, done, failed, locked_files) and navigate deeply nested branching logic (e.g., the D.2a.5 vs D.2a.6 escalation paths). LLMs are probabilistic text generators; they are notoriously bad at maintaining strict state tracking across long conversation turns.

2. Where to Truncate (Reducing Context Bloat)
You can likely cut this document's length by 30-40% by removing information the LLM doesn't actually need to make decisions.

Remove the "Cleanup policy" entirely: The document explicitly states this is wrapper-enforced, for reference. If the Python wrapper enforces it, the LLM does not need to know about it. It is dead weight in the context window.

Remove exhaustive CLI arguments: You have a massive table for plan_ops.py CLI reference. Instead of teaching the LLM the entire API surface, offload this to the tools themselves. The LLM only needs to know the command exists; if it makes a syntax error, your python script should return a helpful stderr message guiding it to the correct flag.

Condense the Routing Tables: The tables in Phase D.2 and D.2a are overly complex. You can summarize them conceptually: "On review failure, retry once. If the third-party reviewer confirms the failure, pause and await the user. Do not auto-revert."

3. How to Generalize & De-restrict
To make this more general and less rigid, you need to shift the burden of state management and routing out of the prompt and into your Python orchestrator.

Shift State to Python: The LLM should not be tracking locked_files or failed sets. Your plan_ops.py batch-next command should maintain this state internally (perhaps in the .schedule.json). The LLM should simply loop plan_ops.py batch-next until it returns a "plan complete" or "stuck" flag.

Shift Routing to Python: Instead of explaining D.2a.5 and D.2a.6 to the LLM, have plan_ops.py review-route take the review envelope and return the exact next instruction.

Current flow: LLM reads review -> LLM checks table -> LLM decides to trigger narrow-remediation -> LLM runs specific bash command.

Better flow: LLM feeds review to Python -> Python returns {"action": "dispatch_remediator", "findings": [...]} -> LLM complies.

Abstract Bash commands into Tools: If your orchestrator framework supports structured tool-calling (like standard Claude tools), expose execute_codex_batch, parse_report, and fail_task as actual JSON schema tools rather than asking the LLM to write out long, string-interpolated venv/bin/python ... bash commands.

By moving the "control flow" logic into Python and treating the LLM strictly as a "reasoning and dispatch" node, you will massively reduce the prompt size and make the system far more adaptable to new workflows.

If you were to rewrite the escalation and retry logic (Phase D) entirely into the plan_ops.py state machine, which parts of the cross-review process do you feel the LLM absolutely must evaluate itself rather than blindly following the script's routing?