# Close — acceptance, follow-ups, merge, close checks

Loaded from `SKILL.md` on `/project-workflow NNNN close`, or once the last
wave's gate has passed. `<bin>` and the config keys are defined there.
`python <bin>/close-project.py <NNNN> --preflight`, then `--yes` in place of
`--preflight`, from the main checkout, performs §§ 3-4 and resumes after a
failed step; by hand where it cannot run.

Wherever close stops short of § 5 (a `reject`, a preflight FAIL, an operator
decision, the unattended stop before § 3), overwrite `## Resume` and
`## Next session prompt` in the worktree STATUS.md to name that stop and what
clears it, and commit them on the project branch, before exiting.

## 1. Acceptance pass

"Done" is ruled by someone who did not do the work. One Agent call,
`subagent_type: reviewer`, prompt:

```
Mode: acceptance. Criteria: <project folder>/brief.md § Done when.
Workspace: <project worktree absolute path> — resolve every path against it.
  One command per Bash call, `git -C <abs path>`, absolute paths; no
  `cd X && ...` or shell variables.
Deliverable: artifacts/acceptance.md
Return the reviewer JSON for this mode with a `deliverable_markdown` field
holding the deliverable's full text.
```

It rules every criterion `met`, `partial`, `not-met` or `unverifiable` on
evidence it produced in that run, not on a STATUS or handoff claim. Save it with
`python <bin>/save-return.py <agent id> artifacts/acceptance.json --json`, which
writes its `deliverable_markdown` verbatim as `artifacts/acceptance.md`.

- Verdict `accept`: go on.
- `reject`: each criterion short of `met` is an in-scope bug. Stop close and
  open a fix wave (`/project-workflow NNNN wave N`, run to `wave.md`
  § Session shape: one fix batch, one confirm); then re-enter close for a
  fresh pass. Closing with a criterion unmet needs an operator ruling,
  recorded in STATUS with the criterion number and date. Unattended mode never
  makes that ruling: it stops.

## 2. Follow-ups table

The waves kept this table as their ledger (`wave.md` § Triage) and the last fix
wave emptied it of in-repo work. Now add to `artifacts/follow-ups.md` every
remaining open item: the acceptance findings, `found` items not yet routed,
carried items, the brief's out-of-scope list, and `## Open decisions`:

```
| Item | Source | Home | Id |
|---|---|---|---|
| <one line> | S-03 found / acceptance A2 / brief § Out of scope | fixed / backlog / project NNNN / dropped | sha / F-12 / — |
```

Each row has a home: `fixed` in this project (its sha), a backlog id, a row on
a named project (carried by ruling), or `dropped` with the reason (refuted:
name the check that refuted it). The `backlog_file` takes only what cannot be
resolved in the repo (blocked on data, a real-world run, or an action outside
the repo), with evidence and a named unlock trigger; an unlock of "none" is
resolvable, so fix it. A backlog row this project completed, or made
unneeded, is deleted (git history keeps it). Make these ledger edits now, on
the project branch before the merge, so they land with it. A `## Done when`
criterion bounding the diff scope exempts them.

## 3. Merge

1. The main checkout is on the base branch, with no `MERGE_HEAD` and no
   tracked changes (the local-merge precheck refuses otherwise).
2. `git mv` the folder to `<projects_dir>/completed/`, set `status: completed`
   (the branch STATUS replaces the stub at merge), and update its `index_file`
   row, on the project branch.
3. Merge by the config's `close.merge`: `local` (the default) is a `--no-ff`
   merge commit in the main checkout; `squash` pushes the branch, opens a PR
   and squash-merges it, then pulls the base branch. A failed local merge is
   aborted, never left half-done: merge the base branch into the project
   branch in the worktree, resolve, commit, and rerun.

Unattended mode stops before this section: merging and pushing are the
operator's. It writes the merge as the next-session prompt and exits.

## 4. Teardown

1. Run the config's `teardown` argv, if set (`{worktree}` is the worktree's
   absolute path; a non-zero exit refuses): it undoes what the `bootstrap`
   made, junctions included. With none, or by hand, check for junctions first
   (`recovery.md` § Worktree handling).
2. `git worktree remove <repo>-worktrees/<NNNN>-<slug>` and delete the
   branch. Delete `<repo>-worktrees/` if it is now empty.

## 5. Close checks — all must hold before saying "closed"

| Check | Command | Pass |
|---|---|---|
| Gate on the base branch | `python <bin>/wave-close.py <NNNN>` from the main checkout, after the merge | the configured suites pass, 0 skipped, passed at or above the floor (lowered only for tests the brief retired) |
| Pin | a project that retired or moved tests: `python <bin>/wave-close.py --capture-pin` on the merged base branch | the new `suite_pin` is committed |
| Worktree gone | `test ! -e <worktree path>` and `git worktree list` | path does not exist; not listed |
| Branch gone | `git branch --list project/<NNNN>-*` | empty (after a squash close, also the remote branch) |
| Ledgers | the `index_file` row, the `completed/` folder, `artifacts/follow-ups.md` | row says completed; every follow-up has a home |

A green gate in the worktree does not prove the base branch: the merge can drop
or conflict files. A worktree that still exists after "remove" is a held handle
or a junction; see `recovery.md` § Worktree handling.

Then report closed, with the merge sha and the gate counts.
