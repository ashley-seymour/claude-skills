---
name: project-workflow
description: Run multi-session project work as waves of concurrent subagents — create a numbered project, critique its brief, dispatch a wave, verify returns, stop at the wave boundary for a context clear, resume, close with an acceptance pass. Invoke as `/project-workflow [NNNN] [wave N|close]`, or with no arguments to resume the active project. Also runs unattended under away-run. Use when starting, resuming, dispatching, or closing numbered work under the repo's projects folder. Not for single-session tasks.
---

# Project workflow

Work too big for one context window, split into **waves** of **streams**. A
stream is one subagent with one mission and one write-allowlist; a wave is a set
of streams run concurrently. Each stream's reading, edits and test runs stay in
its subagent; only a short JSON return reaches this session. That is how a
project outlives a context window.

**Repo config and profile.** Per-repo values come from the optional
`<repo>/.claude/project-workflow.json` (`projects_dir`, default `my-projects`;
`base_branch`, `profile`, `index_file`, `backlog_file`, `suites`, `suite_pin`,
`bootstrap`, `teardown`, `close`; defaults in `scripts/lib/config.py`); a project
worktree's copy wins over the main checkout's. Before anything else, read the
profile its `profile` key names (default `<projects_dir>/WORKFLOW.md`), if it
exists: repo rules and extra steps. It wins where it differs from this skill.

**Scripts.** `<bin>` below is `$CLAUDE_CONFIG_DIR/skills/project-workflow/scripts/bin`.
Run each as `python <bin>/<script>` from inside the repo it acts on; it finds the
repo from the current directory. Table and requirements: `scripts/README.md`.
Where a script cannot run, do its step by hand as the phase file describes.

**When not to use this.** Five streams that each read three files and write a paragraph is worse than just
doing it. Single-session work gets no project folder and no waves. Nine one-file
edits are still one session: coupling decides the tier, not volume.

## Phases — load one file, on entering its phase

| Phase | Entered by | Read |
|---|---|---|
| create | "new project", a proposal to scaffold | `create.md` — stub, task-card brief, lint, plan critique, stop |
| wave | `/project-workflow NNNN wave N` | `wave.md` — first-dispatch cut, `after:` layers, roles, stop-and-return, session shape, triage, fix protocol, context budget, boundary |
| close | `/project-workflow NNNN close`, or the last wave's gate passed | `close.md` — acceptance pass, follow-ups, merge, close checks |
| recovery | a dead stream, a STATUS that disagrees with the tree, a moved or missing worktree | `recovery.md` |

State machine: `stub (planned) → brief → linted → critiqued ‖ operator-reviewed → wave 1
(cut: active) ‖ … wave N ‖ accepted → closed ‖`. `‖` is a stop (a context clear;
unattended, a process exit): create runs to the operator review, each wave to its
boundary, close to the end.

## Invariants

- **The orchestrator dispatches and verifies. It does not do stream work.**
  Anything over ~2 minutes of compute, or any edit to tracked source, is a stream.
- **The one hard rule: within a wave, no two streams may list the same path in
  `owns:`.** If two streams need one file, merge them or move one to the next
  wave. `owns:` is per-file (a directory is `dir/**`); `consumes:` may be shared.
- **Shared ledgers belong to the orchestrator** (`STATUS.md`, the `index_file`
  row, the `backlog_file`): never in `owns:`; streams report via returns.

## STATUS.md

Schema: `templates/status-template.md` (frontmatter `project`, `status`,
`current_wave`, `worktree`, `branch`, `deadlines`; then `## Resume`, one
`## Wave N` section per wave, `## Log`). `status:` and the index row read
`planned` from create (the base-branch stub keeps it until close) and `active`
in the worktree from the wave-1 cut. `## Resume` is overwritten, never
appended to: **at most 6 lines**, pointers not narrative (`state:`, `next:`,
`decisions:`); narrative goes to `artifacts/`, one line per wave to `## Log`.
`deadlines:` lists dated commitments (`["<YYYY-MM-DD> <what is due>"]`); on
resume, name any due within 14 days first.

## Resume

No arguments: glob `<projects_dir>/current/project-*/STATUS.md`, take the one that is
`active`, or a `planned` stub not yet cut (a stub's state is its `authoritative_status:`
copy where that exists); more than one, list them and stop. With `NNNN wave N`:
confirm `## Resume` agrees the previous wave closed, then load `wave.md`. A
`## Resume` reading `checkpoint wave N at step <n>` resumes wave N at its `next:`
step; the previous wave is N-1. **Read the worktree copy, not the tree you stand
in**: the base branch holds a frozen stub (`stub: true`). Before wave 1 no worktree
exists yet: read the stub, and `wave.md` § Before dispatch cuts it. Disagreements:
`recovery.md` § A stale handoff.

## Unattended mode — under away-run's `away-orchestrator`, or a prompt saying `unattended`

- **The process exit replaces `/clear`.** At every stop, write `## Resume` and
  the next-session prompt, commit on the project branch, return, and exit.
- **Open decisions become named defaults**: take the default the brief names,
  record it with its reason in the run's decisions file and STATUS `## Log`. No
  named default: stop the item `blocked`.
- **`steps <a>-<b>`** (away-run): stop at the end of step b; short of step 7, commit
  `chore(<NNNN>): checkpoint wave N at step <b>` with `## Resume` `next:` naming step b+1.
- Never create a project (scope is the operator's: propose instead), merge,
  push or commit on the base branch, or skip the acceptance pass.

## Worktree location

Every worktree of a repo lives in one sibling folder, never loose beside the
main checkout:

```
<parent>/<repo>/                          main checkout (never moved)
<parent>/<repo>-worktrees/<NNNN>-<slug>/  project worktree
<parent>/<repo>-worktrees/<NNNN>-<id>/    stream worktree (S-01, G-02, ...)
<parent>/<repo>-worktrees/<branch-slug>/  non-project branch (fix-foo)
```

Delete the `<repo>-worktrees/` folder once it is empty. Removing one: `close.md`
§ 4; moving or re-cutting: `recovery.md`. Per-project scratch that must outlive
a session (close-gate baselines, returns) goes in `<repo>-worktrees/<NNNN>-returns/`.
