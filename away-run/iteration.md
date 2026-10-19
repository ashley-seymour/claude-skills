# Iteration — one queue row per process

Loaded by `away-orchestrator` from the entry prompt the watchdog renders. The
prompt names the claim tag, worktree, run folder, queue lock, phase, model mode,
hard stop, lease and attempts cap. Nobody is watching: never ask, never wait.

The claim tag (`iter-NNN`, below `<tag>`) is copied exactly from the prompt
wherever this file writes it: the watchdog releases claims by exact match.

## 1. Orient

- `git -C <worktree> branch --show-current` must print `away/<date>`, and
  `<git-dir>/MERGE_HEAD` must not exist. Otherwise write nothing and return
  `blocked` naming what you saw.
- Read `queue.md`, `decisions.md` and the tail of `log.md`. A ruling in
  `decisions.md` binds; do not relitigate it.
- Read the repo profile's § Away run (test commands, auto-allowed classes).
- `git status --short`: uncommitted work you did not make is someone else's.
  Leave it alone; do not stage it. One exception: an uncommitted change to
  `queue.md` that only moves `lease_until` on rows still `claimed` is the
  watchdog's release of a dead claim. It is yours to keep; § 2 step 5 commits it.

## 2. Claim

1. `mkdir <queue lock>`. If it exists, retry every 10 s for up to 2 minutes.
   Staleness is the lock folder's age, whether or not it holds an `owner` file:
   a folder last modified more than 10 minutes ago
   (`date -r <queue lock> +%s` against `date +%s`) is stale: remove it, retry.
   On success write `<tag> <time>` to `<queue lock>/owner`. Still held and not
   stale after 2 minutes: write nothing and return `blocked`, reason
   "queue lock held: <owner file text, or none>". Later iterations retry;
   the lock goes stale 10 minutes after it was made.
2. Pick the first claimable row: `pending`, or `claimed` with `lease_until` in
   the past. Model mode `mechanical-only`: the first claimable
   `mechanical-worker` row only. A passed-lease row at the attempts cap: set
   `blocked`, evidence "attempts cap; last claim <claimed_by>", pick again.
3. Set `status: claimed`, `claimed_by: <tag>`, `lease_until` now + lease,
   `attempts` + 1. `rm -rf <queue lock>`.
4. Nothing claimable: return `queue-empty`. The watchdog then runs the close-out.
5. Commit the claim on `away/<date>`, the run folder only, by explicit path:
   `chore(away): claim <id>`. A pending watchdog release in `queue.md` goes into
   this commit.

## 3. Do the row

Dispatch by `agent`, one Agent call, with the prompt template in the
`project-workflow` skill's `wave.md` § Dispatch: mission, absolute workspace
(the away worktree), the paths it may write, files to read first, deliverable,
and the return JSON with `found`. A row has no `artifacts/` folder: its
deliverable line reads "the return JSON; the row's `evidence` cell records it",
and no deliverable file is written.

| agent | How |
|---|---|
| `mechanical-worker` | Agent call; state the procedure and the check. Closes on your own check of its counts. |
| `stream-worker` | Agent call; pass `model: sonnet` if the row is well scoped. Then a `reviewer` call in `Mode: wave` on its paths before recording `done`. |
| `researcher` | Agent call; its sources go in the evidence cell. |
| `project` | Skill tool: `project-workflow` with `<NNNN> wave <N> unattended, steps <a>-<b>` (or `close`): one step group, § A `project` row. It commits on the project branch in the project worktree. |
| `discover` | Follow `discover.md`: append at most 5 tier-1 rows, write tier-2 proposals. |

- Verify before recording: every `updated_paths` entry is inside the row's
  scope; run the checks the row names and read their summary line, not a pipe's
  exit code. A skipped or uncollected test exits green: compare counts.
- **Commit** the row's work on `away/<date>`, staging by explicit path, never a
  directory: `<type>(away): <subject>`. Never `main`, never push, merge, rebase
  or reset. Project rows commit on their project branch via the skill.
- **Decisions.** Take the default the row, its brief or the profile names; with
  none, the reversible option. Record it in `decisions.md` in its format. A
  decision with neither (outward-facing, irreversible, scope-shaping) stops the
  row `blocked` with the question as its evidence.
- **Found items.** Carry each worker's `found` items unchanged into `log.md`
  § Found. Do not fix them in this iteration; a tier-1 one may become a new row
  only through a `discover` row.
- **Turn budget.** If the row cannot finish, record how far it got and the next
  step in `evidence`, leave it `claimed` with the lease cleared to now, and exit.
  The next iteration re-claims it; `attempts` bounds the retries. A `project`
  row that completed a step hands off by checkpoint instead (below); one that
  completed none takes this path.

### A `project` row — one step group per row

A wave is worked across rows, one group of `project-workflow`'s `wave.md`
§ Session shape steps per row, so each process does a bounded part and exits:

| Group | Steps | Stops at |
|---|---|---|
| 1 | 1 dispatch, `blocked` returns answered | end of step 1 |
| 2 | 2 suite, 3 review | end of step 3 |
| 3 | 4 triage, 5 fix batch, 6 confirm, 7 boundary | the boundary |

1. Start at the step the project's `## Resume` names (the worktree copy of
   `STATUS.md`); a fresh wave starts at step 1. Where the row's `item` names
   another step, `## Resume` wins; note the mismatch in `evidence`.
2. Run the Skill tool to the end of that step's group. Every stream and
   reviewer return is saved to disk as `wave.md` § Dispatch requires: the next
   row passes them to the gate from there, not from this context.
3. Stopped short of the boundary (at a group's end, or earlier on the context
   budget or the turn budget) after completing at least one step, so the step
   to run next differs from the step this row started at: commit on the
   project branch `chore(<NNNN>): checkpoint wave <N> at step <n>`, `<n>` the
   last step completed, with `## Resume` `next:` naming the step to run next.
   In § 4, under the same lock, append a follow-on row: a fresh id, agent
   `project`, `item` `<NNNN> wave <N> from step <next>` with the original
   row's check, `pending`, `attempts` 0. This terminates: every follow-on row
   follows at least one completed step of the wave's seven, and a row that
   completes none retries on its own `attempts` (item 4). Record this row
   `done`, the checkpoint sha as evidence; the record commit carries the new
   row. The 150k soft limit of `wave.md` § Session shape
   means this for a `project` row: land what is in flight, then checkpoint
   and append the follow-on row (item 4 if no step completed); it does not
   mean going to the boundary.
4. Stopped having completed no step (the step to run next is still the one
   it started at): append no row and record nothing `done`. Take the
   **Turn budget** path above: evidence records how far the step got, the row
   stays `claimed` with the lease cleared to now, and the next claim adds 1 to
   `attempts`, so the attempts cap ends a step that never completes.
5. Boundary reached: its commit and `## Resume` are the hand-off. Append no
   row; the next wave is queued only by a `discover` row.

## 4. Record

Record the moment the evidence exists, not at the end: a usage limit can kill
this process mid-sentence.

1. Take the queue lock as in § 2, except that past 2 minutes you keep retrying
   every 10 s until it is free or stale: the work
   exists and must be recorded, so do not return `blocked` here.
2. Set `status` (`done` or `blocked`), `evidence` (sha, check output line, or
   reason), and clear `claimed_by` / `lease_until`. Release the lock.
3. Append one line to `log.md`: `HH:MM · <id> · <tag> · <what> · <sha|no-op>`,
   using the real clock from `date`.
4. Commit the run folder: `chore(away): record <id> <status>`.

## 5. Return and exit

Return the away-orchestrator JSON: `status` is the row's outcome (`ok` for
`done`), `item` its id, `commits` every sha made, `found` merged from workers.
Then stop: one row per process. The watchdog judges the queue change and starts
the next iteration.

## Close-out (phase `closeout`)

Runs once: queue empty, the close-out reserve reached, or the quota reserve
cannot free before it. Take no new row.

1. Rows still `claimed` with a passed lease: `blocked`, "unfinished at close-out"
   plus the last recorded step. Rows still `pending` stay `pending`.
2. Run the profile's suites once on `away/<date>`; record collected and passed
   counts against the baseline in the queue header or the first log line.
3. Write `morning-report.md` from `templates/morning-report.md`: every section,
   figures from `queue.md`, `log.md`, `decisions.md`, `watchdog.log`, and the
   iteration JSON (`total_cost_usd`, summed). Tier-2 proposals under
   "Proposed, not done". Found items ranked, each homed: a next-run queue row
   if tier 1, else a proposal written now. Those are the only two homes: a
   found item is never parked as a row in an issues ledger.
4. Commit the run folder: `docs(away): morning report <date>`.
5. Leave the scheduled task to the operator's `disarm.ps1`. The watchdog exits
   after this iteration and writes `STOP`, so later firings are no-ops.
6. Return `ok` with `item: "close-out"`.
