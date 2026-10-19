# Recovery — dead streams, stale handoffs, worktree handling

Loaded from `SKILL.md` when a stream dies, when STATUS disagrees with the tree,
or when a worktree is missing, moved or will not delete.

## A stream that dies

A usage limit, an API overload or an operator interrupt can end a stream after
it wrote most of its work.

1. `git status` in the worktree against that stream's `owns:`.
2. Continue it with `SendMessage` to its agent id if that tool is available.
3. Otherwise dispatch fresh: the stream's task card (the brief names its owns
   and deliverable), plus an inventory of what exists (files, functions,
   what is missing) and "audit this, do not assume it works".
4. If the stream's spec had acceptance criteria, dispatch a verifier against
   them rather than a second implementer: `reviewer`, `Mode: wave`, the
   absolute workspace line, the criteria, and "this is a reconstruction";
   save its return as for any reviewer.

A re-dispatch of a stream that died is not a fix batch: it does not use up
the session's one fix batch (`wave.md` § Session shape).

## A stale handoff

The next-session prompt and `## Resume` are claims. When one disagrees with the
tree:

- **Worktree and frontmatter disagree.** The branch copy is right; the base-branch
  stub is frozen at the cut. Use `git worktree list` for the real path.
- **Resume says a wave closed, but `git log` on the branch has no wave commit.**
  The wave did not close: this is the wave phase again. Load `wave.md`, run
  § Session shape step 2 (the gate) on the tree as it is, then the boundary.
- **A claimed figure** (a count, a sha, a passing suite) that the next step
  depends on: re-measure it before acting.
- **Resume over its cap** (more than 6 lines) or a prompt restating the brief:
  trim it to pointers at the next boundary; do not plan from the narrative.
- **Unattended leftovers.** A claimed-but-unfinished queue row or a STATUS with
  an uncommitted boundary is a crashed iteration. Inspect as for a dead stream;
  never re-run a step whose output is already committed.

## Worktree handling

Layout is in `SKILL.md` § Worktree location.

- **Missing worktree** (frontmatter names a path that does not exist). Not a
  recovery case before wave 1's first dispatch: the worktree is cut then
  (`wave.md` § Before dispatch), so read the base-branch stub. Otherwise check
  `git worktree list` and `git worktree prune --dry-run`. If the branch exists,
  re-cut with `git worktree add <path> project/<NNNN>-<slug>` (no `-b`) and run
  the config's `bootstrap` argv. If the branch is gone too, stop and ask.
- **Moving one:** `git worktree move <old> <new>`, then update `worktree:` in
  its STATUS.md and every absolute path in the live next-session prompt.
  "Permission denied" means a process holds a handle inside it (a shell's
  cwd, a stray `du`/`find`); stop that process, not the move.
- **Will not delete:** junctions first (a link the bootstrap made into another
  tree): `cmd /c rmdir <link>` (removes the link only), then
  `Get-ChildItem -Recurse -Attributes ReparsePoint` must return nothing; then
  look for held handles as above. Never force a recursive delete over a tree
  that may still hold a junction: it walks through the link and deletes the
  target's files.
