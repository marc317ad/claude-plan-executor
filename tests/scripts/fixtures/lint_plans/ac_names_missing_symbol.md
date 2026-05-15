# Plan: ac_symbol_groundedness negative-case fixture

**Base branch:** main

## Tasks

### TASK-101: AC names a symbol that does not exist in any declared file

- **Status:** pending
- **Files:**
  - src/widget.py
- **Dependencies:** none
- **Acceptance criteria:**
  - The helper `compute_total_orphan_value` returns 0 for empty inputs.
  - The constant `MISSING_SENTINEL_CONST` is exposed at module level.
- **Reversion guidance:** revert the widget helpers.
