## Context
Shared execution context for fixture rendering.

### TASK-001: Fixture task

- **Status:** pending
- **Priority:** high
- **Files:** src/example.py
- **Dependencies:** none
- **Test command:** `pytest tests/example_test.py`
- **Acceptance criteria:**
  - Preserve fixture behavior.
  - Render exact prompt bytes.

**Description:**
Exercise the Agent dispatch prompt renderer.

### TASK-002: Second fixture task

- **Status:** pending
- **Priority:** high
- **Files:** src/second.py
- **Dependencies:** none
- **Test command:** none
- **Acceptance criteria:**
  - Select the second block when target_task_id is supplied.

**Description:**
Second task for multi-heading injection.
