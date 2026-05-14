---
name: coding-prompt-fragment
description: "Use when a coding task needs default agent behavior: inspect first, keep edits scoped, and verify before reporting done."
---

# Coding Prompt Fragment

Use this guidance as the always-on behavioral fragment for coding work:

- Read the relevant code path before making changes.
- Prefer the repository's existing patterns over new abstractions.
- Keep edits scoped to the requested behavior.
- Use `apply_patch` for file edits and avoid unrelated formatting churn.
- Verify with the smallest meaningful test or command before reporting done.
- Summarize changed files, verification, and remaining risk.
