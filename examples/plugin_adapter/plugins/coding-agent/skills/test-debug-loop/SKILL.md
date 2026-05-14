---
name: test-debug-loop
description: "Use when a failing test or runtime error needs a tight reproduce, diagnose, patch, and rerun loop."
---

# Test And Debug Loop

- Reproduce the failure with the narrowest command.
- Read the stack trace from the first project-owned frame.
- Form one concrete hypothesis at a time.
- Patch the cause rather than the symptom.
- Rerun the failing test before running wider checks.
- If the failure is external or flaky, capture the evidence and avoid hiding it.
