---
name: development-workflow
description: "Use for multi-step implementation work that needs exploration, edits, and verification."
---

# Development Workflow

1. Inspect the relevant files and tests.
2. Write down the smallest implementation boundary.
3. Edit only files in that boundary.
4. Run focused tests first, then broader checks if the change touches shared behavior.
5. Report the result with paths, commands, and any follow-up risk.

Delegate read-only research to the `explore` sub-agent. Delegate a bounded patch
to the `worker` sub-agent when the write scope is clear.
