---
name: away-run
description: Arm, run and disarm an unattended away run (a night or a few hours). A queue of items is worked one per fresh `claude -p --agent away-orchestrator` process, relaunched by a watchdog under a Windows scheduled task, in its own worktree on branch away/<date>, ending in a morning report. Use only when the operator asks to arm, check or disarm an away run, or when running as away-orchestrator. Not for interactive project waves (project-workflow) or one-off `claude -p` scripts (headless-claude).
---

# Away run

The operator leaves; a queue gets worked. Each item runs in a fresh headless
process, so a usage limit or a crash costs one iteration, not the night. State
lives on disk in the run folder; the process exit replaces `/clear`. Flag facts
for `claude -p` are in the `headless-claude` skill.

**Repo config and profile.** `<repo>/.claude/project-workflow.json` is optional;
this skill reads its keys `projects_dir` (default `my-projects`), `profile`
(default `<projects_dir>/WORKFLOW.md`), `backlog_file` (default
`<projects_dir>/backlog.md`) and `bootstrap` (an argv list, or null). Read the
profile's § Away run if the file exists: test commands, discovery sources,
auto-allowed classes. Where it and this skill differ, the profile wins.

## Phases — load one file, on entering its phase

| Phase | Who | Read |
|---|---|---|
| arm | operator present | § Arm below, then `discover.md` to build the queue |
| iteration | `away-orchestrator`, one per process | `iteration.md` |
| status, disarm | operator | § Status, § Disarm below |

`project-workflow` and `away-run` share files (the backlog file,
`<projects_dir>/proposals/open/`), not calls, except one: a `project` row
invokes `/project-workflow` through the Skill tool in unattended mode.

A project wave spans several `project` rows, one `wave.md` § Session shape
step group each (1; 2-3; 4-7). Each row works from the step `## Resume` names to
its group's end, commits `chore(<NNNN>): checkpoint wave N at step <n>`, and
appends the follow-on row for the next step unless the boundary was reached.
A row appends one, `attempts` 0, only if it completed at least one step; a row
that completed none stays `claimed` and retries on its own `attempts`. This
terminates: each follow-on row follows at least one of the wave's seven steps,
and the attempts cap ends a step that never completes.
The checkpoint and `## Resume` are the only hand-off (`iteration.md` § 3).

## Layout

```
<repo>-worktrees/away-<date>/            worktree, branch away/<date> off main
  <run folder>/                          tracked on away/<date>; arm.ps1 -RunFolder,
    queue.md decisions.md log.md         default <projects_dir>/away/<date>/
    morning-report.md                    written by the close-out iteration
<repo>-worktrees/away-<date>-run/        scratch, untracked: away.env, watchdog.sh,
                                         entry-prompt.md, watchdog.log, iter-NNN.*, locks
scheduled task away-<repo>-<date>        every 15 min until the hard stop
```

## Queue rows

`templates/queue.md` holds the schema. Columns: `id | item | agent | status |
claimed_by | lease_until | attempts | evidence`. `agent` is `mechanical-worker`,
`stream-worker`, `researcher`, `project` or `discover`. Every queue edit takes
the lock: `mkdir <scratch>/queue.lock` (atomic; if it fails, retry every 10 s
for 2 minutes; a lock folder last modified over 10 minutes ago is stale, with
or without its `owner` file: remove it), edit, `rm -rf` the lock. What follows a
failed 2-minute retry is in `iteration.md` § 2 and § 4. A claim sets
`claimed_by` to the iteration's claim tag (`iter-NNN`, exactly as the entry
prompt gives it) and `lease_until`; a passed lease means a dead iteration and
the row is claimable again, `attempts` + 1.

## Arm (operator present)

1. **Queue.** Build it with `discover.md`: tier-1 rows only, each with a named
   check. Tier-2 work becomes proposals. Name the default for every decision a
   row may hit; a row with an open decision and no default is not queued.
2. **Posture.** Say plainly: the run uses `--permission-mode bypassPermissions`,
   so every tool call runs unprompted; global deny rules still apply. "No gates"
   (open questions become named defaults, failures skip and retry) is opt-in per
   run, set as `gates: none` in the queue header; never inferred from an earlier run.
3. **Arm.** Dry first, then for real, from PowerShell:
   `powershell -NoProfile -File <skill>/arm.ps1 -Repo <path> -StopAt "<YYYY-MM-DD HH:MM>" -Queue <queue.md> -WhatIf`.
   It cuts the worktree on `away/<date>` from `main`, seeds the run folder from
   `templates/` and commits it with the queue, runs the config's `bootstrap`
   argv (`{worktree}` replaced by the worktree path, cwd the main checkout;
   skipped if the key is absent or null) and waits for it, copies
   `watchdog.sh` and `entry-prompt.md` to scratch, writes `away.env`, and
   registers the task (`Register-ScheduledTask`, never `schtasks /Create`:
   PowerShell 5.1 mangles `/TR` quoting). A non-zero bootstrap exit refuses
   before any task exists; the worktree and branch stay for inspection.
   The task has no principal, so it runs only while the operator is logged on,
   in a console window that closing kills: lock the screen, do not log off.
4. **Check.** `Get-ScheduledTask -TaskName 'away-*'` shows the task; the first
   firing writes `watchdog up` to `watchdog.log`.
5. **Later queue edits.** arm.ps1 commits the queue once. Commit any queue
   edit made after arming on `away/<date>` yourself (under the queue lock once
   the task has fired): iterations treat uncommitted work they did not make as
   someone else's and never stage it.

## What the watchdog does (`watchdog.sh`)

- Launches `claude -p --agent away-orchestrator --max-turns N`, prompt on
  stdin. Model and effort come from the agent frontmatter; no `--effort`, no
  TTL flag, no `--disable-slash-commands` (it would disable Skill).
- Judges an iteration by `is_error`/`api_error_status` and whether `queue.md`
  changed, never by `num_turns`. Three iterations without a queue change end it.
- Usage limit: sleeps to the reset the message states. "You've hit your Opus
  limit" continues on Sonnet (`--model sonnet`) for `mechanical-worker` rows
  only; with none claimable, it sleeps to the reset.
- Budget overrun (`subtype` `error_max_budget_usd`, kind `budget`): no sleep,
  since the cap is per process. It keeps the row's claim, so the next iteration
  takes the next row and this one returns when its lease lapses; overruns count
  toward `FAIL_MAX` like errors.
- Quota reserve: at `WINDOW_CAP_USD` imputed spend in a rolling 5 hours it sleeps
  until the window frees; the last `RESERVE_MIN` minutes go to the close-out.
- Queue empty: one close-out iteration writes the morning report, then it exits.
  Any other exit leaves a watchdog-written stub report. Every exit writes
  `STOP`, so later task firings are no-ops; delete it to resume.
- After every item iteration it lapses the leases held under that iteration's
  claim tag, so a row cut off by a limit or error is retried, not stranded
  until close-out. It skips this after the close-out, which claims nothing,
  and after a budget overrun (above).
- Logs `claude: <path>` at each iteration start: the binary it is about to run.
- Refuses to run on `main`, on a main checkout, or on a branch not `away/*`.
- Dry run: `CLAUDE=<stub script> AWAY_NO_SLEEP=1 bash watchdog.sh <away.env>`
  against a scratch worktree on `away/*`; sleeps are logged, not taken. A
  `CLAUDE` or `PY` set in the environment wins over the one `away.env` names,
  so the stub runs even with an arm-written `away.env`; confirm by the
  `claude: <stub>` log line.

## Status

Tail `<scratch>/watchdog.log`; read `queue.md`. Before trusting "another watchdog
is live", confirm a `claude.exe` process (`tasklist`) and a recent `iteration N
starting` line: an orphaned lock parks every firing silently. Git Bash has no
`pgrep`.

## Disarm

`powershell -NoProfile -File <skill>/disarm.ps1 -Repo <path> -Date <date>`: it
writes `STOP`, unregisters the task and checks `Get-ScheduledTask -TaskName
'away-*'` returns nothing. A task left registered keeps spawning bash every 15
minutes. The worktree and branch stay for review: read the morning report, then
merge `away/<date>` by the repo's usual route, or delete it.

## Why it is built this way

- Resilience lives outside the CLI. `CronCreate durable:true` came back
  session-only (2026-09-19); `/loop` re-enters the same session; a `nohup … &`
  from a Bash tool call is parented to that shell; Git Bash has no `setsid`.
- A usage limit is slept out to the reset its message states, not by a growing
  backoff, and an iteration is never judged failed by a low `num_turns`: a
  backoff overshoots the reset, and an iteration can close a row in one
  reported turn.
