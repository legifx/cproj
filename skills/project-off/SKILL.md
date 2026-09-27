---
name: project-off
description: Deselect the active project of this session (badge off, handoff noted) and keep working independently of it. Use only when the user calls /project-off or asks to leave the current project.
disable-model-invocation: true
allowed-tools: Bash(cproj:*), Bash(python3 ${CLAUDE_SKILL_DIR}/../project/scripts/cproj.py:*)
---

!`python3 ${CLAUDE_SKILL_DIR}/../project/scripts/cproj.py off`

(Claude Code has run this already. Any other agent: run `CPROJ_SESSION=${HERMES_SESSION_ID} cproj off` in the shell
first — or, if `cproj` is not on your PATH, `python3 <skills folder>/project/scripts/cproj.py off` — and use its output.
Keep that prefix on the `cproj handoff` call below as well.)

- **`DESELECTED <name> (…)`**: do the handoff as described in `/project` — only if this session worked on it:
  `cproj handoff --key=<name> "<what was done, what is open, next step>"`, briefly add a missing entry the project's
  docs require (log in PROGRESS, HANDOFF.md), name uncommitted changes without committing them. Then `cd ~`.
  From now on the project's rules no longer apply.
  Answer: one sentence (“<name> deselected — …”), plus at most one sentence about the handoff.
- **`NO PROJECT active`**: one sentence, done.
