---
name: project
description: Switch the session to one of the user's projects (local, SSH server, GitHub — searchable), create one, wake one from cold storage, or list every loose end; brief where it stands and what comes next, then keep working on it. Use only when the user explicitly calls /project or asks to open, switch to or create a project. `/project [query | new NAME | status | cold [NAME] | + | -]`
argument-hint: "[query | new NAME | status | cold [NAME] | + | -]"
disable-model-invocation: true
allowed-tools: Bash(cproj:*), Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/cproj.py:*)
---

## Step 0 — the index

!`python3 ${CLAUDE_SKILL_DIR}/scripts/cproj.py pick $ARGUMENTS`

**Claude Code** has already run this: the block above is its output and starts with a `CPROJ:` line.
**Any other agent** — or whenever the block shows no `CPROJ:` line (the raw command, an error, an `[inline-shell …]`
marker): run this in the shell as the very first step — nothing before it — and treat its output as the block above:

    CPROJ_SESSION=${HERMES_SESSION_ID} cproj pick "<everything the user wrote after /project>"

Use the same `CPROJ_SESSION=${HERMES_SESSION_ID}` prefix, exactly as shown, on **every** later `cproj` call too — it
keeps this session's badge separate (it is empty and harmless outside Hermes).
If `cproj` is not on your PATH, use the copy bundled with this skill with the same arguments:
`python3 <folder of this SKILL.md>/scripts/cproj.py` (Hermes: `${HERMES_SKILL_DIR}/scripts/cproj.py`).

**Ask** below means: your harness's structured question tool if it has one (in Claude Code `AskUserQuestion`), with
2–4 options and a free-text escape; otherwise a short numbered list in your reply, then wait for the answer.

## What to do

The output starts with a `CPROJ:` line. L/S/G = the project exists locally, on the SSH server, on GitHub.
Everything before a project is chosen must be **fast**: no other tools, no reading — ask right away.

| `CPROJ:` | What you do |
|---|---|
| `LEFT <old> (…)` | Printed before `PICKED`: the session switched projects. First do the **handoff** for `<old>` (below), then continue with the new one. |
| `PICKED <name>` | The briefing follows → step 1. |
| `CHOOSE` | Immediately **Ask** “Which project?”: the top 3 of the list (label = name, description = `L/S/G · seen … · description`, short) plus an option **“Create a new project”**. Question text: “Or type a name or search term under *Other*.” |
| `AMBIGUOUS` | Immediately **Ask** with the 4 best matches (Other = search differently). |
| `NO MATCH` | Immediately **Ask**: **“Create: <query>”** (first), the 2 closest projects, “Search differently”. |
| `IN COLD STORAGE` | One sentence, then **Ask**: “Wake it” / “Search differently”. |
| `COLD` | List cold projects briefly (name · where · quiet for how long), then **Ask** “Which one to wake?” with 3 plausible ones + Other → `cproj pick "cold <name>"`. |
| `WOKEN <name>` | Came back from cold storage: mention how long it was quiet and check whether dependencies and docs still hold. Then as `PICKED`. |
| `STATUS` | Overview (`/project status`, add `fetch` to fetch remotes first). Answer with the 3–5 most important loose ends (PRs waiting for review, commits that never reach main, local vs server drift, lots of uncommitted work), half a sentence each on why. Then **Ask** “Where to start?” → `cproj pick "<name>"`. |
| `NEW <name>` | → Creating (below). If similar projects are listed, first ask whether one of them is meant. |
| `CREATED <name>` | The project exists now, the briefing follows → step 1, then grill me about the goal. |
| `DESELECTED …` / `NO PROJECT` / `CANCELLED` | As in `/project-off`: one sentence, done. |

After each answer: chosen project → `cproj pick "<name>"`; typed search term → `cproj pick "<text>"` (the output is
again a `CPROJ:` line, same table); “Create a new project” → Creating.

### Creating

1. Settle the name (short, kebab-case) if it is missing.
2. **One** **Ask** with up to three questions: *Where?* (local (Recommended) / SSH server — only if one is
   configured), *GitHub?* (private repo (Recommended) / public / none — local only), *What is it about?* (2–3 one-liners
   guessed from the conversation as options, Other for free text).
3. `cproj new <name> --where=local|server --github=private|public|none --desc="<one sentence>"`. It checks once more for
   similar projects everywhere, then creates the folder, `git init`, CLAUDE.md, README, docs/PROGRESS.md, .gitignore,
   a first commit, optionally the GitHub repo, and sets the badge. If it reports `SIMILAR EXISTS`, let the user decide;
   `--force` only when they explicitly want it.
4. Continue with step 1 and go straight to **grill me** about the goal and the first milestone; write the result into
   `docs/PROGRESS.md` (status + log) and CLAUDE.md and commit.

### Handoff (when switching and on `/project-off`)

Only if this session actually worked on the project (the `CPROJ:` line counts new commits and uncommitted files since
it was picked; add what you did yourself):
1. `cproj handoff --key=<project> "<2–3 sentences: what was done, what is open, the next concrete step>"`.
   It appears in the next briefing under “Recent handoffs”, also for projects without status docs.
2. If the project's own rules require an entry (a log in PROGRESS, HANDOFF.md) and it is missing: add it briefly.
3. Name uncommitted changes, never commit them unasked.
Nothing to do if nothing happened. In Claude Code, if the user just closes the terminal, the SessionEnd hook records a mechanical
handoff (commits / uncommitted files).

### 1. Complete the context — briefly, in parallel, no tour of the repo

- If a memory or knowledge tool is available (e.g. an MCP server), ask it about this project.
- Local project: `cd <local path>` so everything else runs there.
- GitHub only: `gh repo clone <owner/name> <projects dir>/<name>`, then `cproj set <name>` (badge shows local).
- Server only: work via `ssh <host> 'cd <path> && …'`; copy nothing to this machine unless the user wants it.
- Read more files only if the briefing leaves the next step open — then the 1–2 files it names itself. If there is a
  remote: `git fetch` and check whether local commits are missing on the default branch.

### 2. Report — short, at most ~10 lines, no quoting

```
**<name>** · <local/server/GitHub> · last active <…>
**Status:** 2–3 sentences: what is built and what was worked on last.
**Open:** up to 3 items (uncommitted changes, open PRs/issues, pending reviews).
**My proposal:** the concrete next step in 1–2 sentences — what you would do and how we know it is done.
```

If sources disagree (docs say X, git shows Y), say so in half a sentence — git and code beat prose.
If the briefing has a **handoff** or a fitting **recent session**, use it for status and proposal. When resuming an
old session is worth it (long work, same thread), mention its `claude -r …` command in one line.

### 3. Let the user decide

- **Next step is clear** (HANDOFF/PROGRESS names it, a PR waits, uncommitted work is pending): **Ask**
  “What next?” — options: your proposal “(Recommended)”, up to two alternatives from the open items, and
  **“Grill me”** (“I ask pointed questions until it is clear what you want”).
- **Not clear** (no status docs, long abandoned, contradictory) → grill me right away.
- If the user rejects the question, do not ask again: put the options as a numbered list in the text and wait.

**Grill me:** one question per round via **Ask**, 2–4 concrete options from the project's state (recommended
first), at most ~5 rounds. Each question sharpens the goal, the scope or the done criterion. As soon as you can state
the task in one sentence plus a done criterion: say it, then start.

### 4. Keep working

Work on the project normally and follow its own rules (CLAUDE.md / AGENTS.md, HANDOFF.md, git identity, commit rules).

Switch: another `/project <other>` — do the handoff first. Deselect: `/project-off`. `/project +` opens the GUI picker
instead. Refresh the cache (new repo on the server/GitHub): `cproj refresh`.
