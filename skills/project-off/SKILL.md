---
name: project-off
description: Deselect the active project of this session (badge off, handoff noted) and keep working independently of it.
disable-model-invocation: true
allowed-tools: Bash(cproj:*)
---

!`cproj off`

- **`DESELECTED <name> (…)`**: do the handoff as described in `/project` — only if this session worked on it:
  `cproj handoff --key=<name> "<what was done, what is open, next step>"`, briefly add a missing entry the project's
  docs require (log in PROGRESS, HANDOFF.md), name uncommitted changes without committing them. Then `cd ~`.
  From now on the project's rules no longer apply.
  Answer: one sentence (“<name> deselected — …”), plus at most one sentence about the handoff.
- **`NO PROJECT active`**: one sentence, done.
