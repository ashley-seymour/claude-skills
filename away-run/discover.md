# Discover — building the queue

Loaded at arm time (operator present), and by an iteration working a `discover`
row. Discovery produces two things: **queue rows** (tier 1, the run may do them)
and **proposals** (tier 2, the run may only propose them). Nothing else.

The repo profile's § Away run lists its discovery sources and auto-allowed
classes; where it is narrower than this file, it wins. It may not widen tier 1
past the tests below.

## Tier 1 — the run may do it on its own

A candidate is tier 1 only if **every** test holds:

- **Reversible.** Lands as commits on `away/<date>` (or a project branch for a
  `project` row); undoing it is one revert. No push, merge, PR, deletion of an
  unmerged branch, or change outside the repo.
- **Checked.** A named command proves it done: a suite and its expected count,
  a regenerated file diffed clean, a link checker at 0. "Looks right" is not a check.
- **Scoped.** One repo, a stated write list, no shared ledger (the backlog
  file, README index rows) edited by more than one row.
- **Decided.** Every decision it may hit has a named default. No open operator
  decision, no ruling to reopen.
- **Sized.** Fits one iteration (~150 turns) or splits into rows that each do.
  A project wave splits itself: each `project` row works one step group and
  queues the next (`iteration.md` § A `project` row).

Typical tier-1 classes:

| Class | Agent | Example check |
|---|---|---|
| Regenerate or rebuild with a named script | `mechanical-worker` | output diffed; counts before and after |
| Doc sweeps: broken links, stale counts, index rows | `mechanical-worker` | link checker at 0; count guard test |
| Tests for untested code, no behaviour change | `stream-worker` | suite count rose by the number added, all green |
| A backlog row whose unlock trigger has fired, first step named | `stream-worker` | the row's own acceptance line |
| Verify or re-measure a claimed figure | `mechanical-worker` or `researcher` | the figure, with the command that produced it |
| The next wave of a project whose brief is critiqued and whose decisions are ruled | `project` | a checkpoint commit with `## Resume` at the next step; at the boundary, the wave's own gate |
| Groom this run's own outputs | `mechanical-worker` | re-read conclusions still hold |

## Tier 2 — propose only

Scope-shaping work. The run writes a proposal and never a queue row for it:

- creating a project, or changing a project's brief, waves or decisions;
- moving, renaming or restructuring folders or modules; retiring or deleting
  files, scripts or branches;
- changing a public interface, a data schema, or static source data;
- settings, hooks, agents, skills, dependencies, CI;
- anything outward-facing: push, PR, issue, message, a paid or external service;
- any fix whose right answer is a judgement the operator has not made.

Write each as `<projects_dir>/proposals/open/proposal-<slug>.md`, relative to
the worktree root (`projects_dir` as `SKILL.md`'s "Repo config and profile"
paragraph reads it), or at the profile's location:
the problem with evidence (`file:line`, command output), options, a recommended
option, and the tier-1 rows it would unlock once ruled. List it in the morning
report under "Proposed, not done".

## Sources, in order

Work down until the queue holds enough for the window; stop when it does.

1. **The operator's list** for this run, if given. Classify each item; a tier-2
   item becomes a proposal and the operator is told at arm time.
2. **In-flight projects**: each `STATUS.md` `## Resume` `next:` line. A wave
   whose brief is critiqued and decisions ruled is one `project` row, `item`
   `<NNNN> wave <N> from step <n>`: `<n>` is the step `## Resume` names, 1 for
   a fresh wave. Queue only that row; the iteration appends the wave's later
   step groups itself. Anything else is a proposal (unattended mode never
   creates a project).
3. **Backlog** (the config's `backlog_file`): open rows whose unlock trigger has fired, with
   an effort estimate and a named first step.
4. **Coverage**: modules ranked by lines times absence of tests.
5. **Docs integrity**: links, orphans, stale counts, text naming code that no
   longer exists.
6. **Dependency hygiene**: declared vs imported packages.
7. **Groom**: re-check this run's own audits and decisions after its changes.

## Rules for a `discover` row

- Append at most 5 rows per `discover` row, each with a check in `item`, under
  the queue lock. Fresh ids; never reuse one.
- Never queue a row that repeats a `done` or `blocked` row, or one the log shows
  was tried and failed. A project wave with a `pending` or `claimed` row (its
  first row or a follow-on) is already queued.
- Nothing found is a valid outcome: record "no tier-1 work found" and the
  sources checked. The watchdog then drains the queue and closes out; the run
  does not invent work to stay busy.
