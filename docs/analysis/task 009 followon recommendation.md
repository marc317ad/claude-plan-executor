Q1 — How did the wrapper revert without --authorization-source?                                                        
                                                            
  The implementer didn't revert. The wrapper did. And it did so at a layer below the gate prohibit_silent_revert TASK-001
   added.                                                                   
                                                                                                                         
  The protection chain you put in place this session:                                                                   
  - cmd_fail_task requires --authorization-source ✓ (committed 61fb67c, TASK-001)                                        
  - reconcile_batch requires path-traversal-safe basename validation ✓ (my hand-fix 6811dba, TASK-008)                   
  - Awaiting-user pause subroutine prevents orchestrator auto-fail-task ✓ (TASK-004)                                     
                                                                                                                       
  The layer that wasn't covered: plan_codex_dispatch.py / plan_claude_dispatch.py _restore_in_scope() (around line 880 of
   the codex wrapper, mirror in the claude wrapper). The wrapper does its OWN scope policing inside the dispatch —
  independent of the orchestrator. Sequence:                                                                           
                                                                                                                         
  1. Implementer agent emits a markdown report (its native output format).
  2. Wrapper's claude_dispatch_input.json declared output_instructions.format: "json" against implementer_result.json    
  schema.                                               
  3. Wrapper attempts schema validation on the agent's report → fails.                                                   
  4. Wrapper defaults declared_files_changed: [] (empty fallback for invalid result).                                    
  5. Wrapper diffs the working tree, finds 4 file changes the agent "didn't declare."                                    
  6. Wrapper concludes: scope violation → calls _restore_in_scope → git restore on the 4 files.                          
                                                                                                                         
  _restore_in_scope is documented in the wrapper as "safe because caller has already filtered inputs to the task's own   
  scope" — but that safety assumption breaks when schema validation fails and declared_files_changed is silently zeroed.
  The wrapper has no --authorization-source analog; no awaiting_user halt; no work-preservation gate. It just runs git   
  restore.                                                               
                                                                                                                         
  Concretely: the prohibit_silent_revert plan extended the principle to the orchestrator surface (fail-task,             
  reconcile-batch, paused-status). It did not extend the principle to the wrapper layer. The wrapper layer is where      
  TASK-009's work was destroyed — by the very plan (SKILL_bash_dispatch_migration TASK-004 introduced this dispatch path)
   that this run was supposed to be hardening.                                                                           
                                                                                                                       
  This is a real defect. The prohibit_silent_revert principle is incompletely applied at the wrapper layer —             
  plan_ops.py:_restore_in_scope (orchestrator) is now safe; plan_codex_dispatch.py:_restore_in_scope and
  plan_claude_dispatch.py:_restore_in_scope (wrappers) are not.                                                        

  ---
  Q2 — Most-correct course of action

  There's a layered answer; pick based on what you weight.

  For TASK-009 specifically (preserve real work): Hand-fix the 4 reverted files from the implementer's report and
  re-verify the 10/10 tests post-reconstruction. The implementer's work was verifiably correct (all AC checked, all tests
   pass) at the moment before the wrapper destroyed it. Caveat: I don't yet know how complete the implementer's preserved
   report is. If the orchestrator's run-log captured the full implementer markdown (it should be in claude_dispatch_done
  event payload or stderr_tail), faithful reconstruction is straightforward. If the wrapper truncated it, reconstruction
  is "redo from AC" — less faithful, more risk of drift from what the implementer actually chose.
                 
  For the codebase (highest correctness): This is a P0 systemic bug. The wrapper's _restore_in_scope should:
  1. Refuse to operate when result schema-validation has failed (because then declared_files_changed is unreliable; the  
  safe default is to preserve the work and surface an awaiting_user pause, not to nuke 4 files);                       
  2. Require an authorization_source analog (or equivalent gate) before any git restore of work the agent observably     
  wrote;                                                                                                            
  3. Add an audit-log event (wrapper_autoclean_executed with files and reason) so future incidents are visible in the run
   log.                                                                                                                  
                                                        
  That's a follow-up plan. Probably 4-6 tasks. Should be high-priority because every future bash-dispatch run has the    
  same gap.                                             
                                                                                                                         
  For the run pipeline: Don't choose option 4 (abort + revert). The +228 lines of plan_ops.py work IS the
  principle-extending core of TASK-009 (the _check_fail_task_authorization_source audit + extended                       
  _check_principle_referenced). Losing it costs more than the four supporting files.                                     
                                                                                                                       
  Don't choose option 2 (Agent tool dispatch in-process) for this. It bypasses the wrapper bug for one task but doesn't  
  tell us anything about the bug's blast radius elsewhere, and it adds a new exception to the protocol that future runs
  will inherit.                                                                                                          
                                                                         
  My recommendation, in order:                                                                                           
                                                    
  1. Write an analysis doc in docs/plans/analysis/ capturing the wrapper-autoclean gap (mirror of the code-reviewer one I
   just wrote). This is the highest-correctness immediate move because it makes the bug visible regardless of what we do
  next.                                                                                                                  
  2. Inspect the run log / stderr_tail to see whether the implementer's full markdown report was captured. If yes →
  hand-fix faithfully. If no → re-derive from AC + accept the drift risk, note it explicitly in the commit.              
  3. Hand-fix the 4 files (option 1) and re-run tests to verify. Commit with [remediation] tag + commit-message
  disclosure of the wrapper bug.                                                                                       
  4. Queue a follow-up plan (PLAN_WRAPPER_AUTOCLEAN_AUTHORIZATION or similar) to extend the prohibit_silent_revert
  principle into the wrapper layer. P0.                                                                                  
                                                    
  The thing that would make me waver from option 1 → 4 ordering: if the implementer's report wasn't fully captured       
  anywhere on disk, then "hand-fix faithfully" becomes "redo from spec." At that point option 2 (re-dispatch via Agent
  tool, in-process) becomes more attractive because it lets the implementer agent re-do its own work as it actually      
  intended, bypassing the broken wrapper. The systemic gap still exists but option 2 produces the most-faithful result
  for this task.