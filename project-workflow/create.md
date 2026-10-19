# Create — stub, brief, lint, plan critique, then stop

Loaded from `SKILL.md` on entering the create phase; `<bin>` and the config keys are defined there.

## Order

The order is the fix for a STATUS.md conflict at merge: when the worktree is cut
before the base-branch stub is written, both sides edit STATUS.md. So the base
branch gets its stub first, and **no worktree is cut at create**. The cut
happens at first dispatch (`wave.md` § Before dispatch), from a base branch that
already holds the reviewed brief.

1. **Stub.** `python <bin>/new-project.py <slug> --no-worktree --yes` allocates the
   next id (max 4-digit id across `<projects_dir>/{current,completed,deferred}`
   + 1; gaps stay, never renumber), writes `<projects_dir>/current/project-<NNNN>-<slug>/`
   with `brief.md` (from `templates/brief-template.md`), a `STATUS.md` stub
   (`stub: true`, `status: planned`, `authoritative_status:` the future worktree
   copy) and `artifacts/`, adds the `index_file` row as `planned` (the cut
   flips both to `active` in the worktree), and commits only those paths on the
   base branch. Every multi-file output goes in a folder.
2. **Brief.** Research and diagnosis first are fine; production code is not.
   Plan straight into `brief.md` (below): there is no separate plan or proposal
   document. An away-run proposal is input to the brief, not a second plan.
3. **Lint.** `python <bin>/brief-lint.py <brief.md>` must exit 0 before the
   critique. It checks the task cards mechanically (paths covered by Owns or
   Read first, a command per Done-when item, no same-wave Owns overlap, no
   `After` cycle, no shared ledger in Owns, every field present; rules in its
   module docstring). Commit the brief on the base branch.
4. **Plan critique.** One Agent call, `subagent_type: reviewer`, prompt:
   ```
   Mode: brief. Review <project folder>/brief.md.
   Workspace: <main checkout absolute path> — resolve every path against it.
   Deliverable: artifacts/brief-critique.md
   Return the reviewer JSON for this mode with a `deliverable_markdown` field
   holding the deliverable's full text.
   ```
   It checks every load-bearing claim against the tree and the judgement the
   lint cannot: a card that allows two behaviours without a `Decided` entry,
   a criterion no command could decide. Save it with `python <bin>/save-return.py
   <agent id> artifacts/brief-critique.json --json`, which also writes its
   `deliverable_markdown` verbatim as `artifacts/brief-critique.md`.
5. **Disposition every finding** in the brief: fixed (say where), or kept with
   the reason. Verdict `stop`: fix, re-lint and re-run the critique. `revise`:
   fix and re-lint; re-run the critique only if a fix changed an `After` or an
   `Owns`. `go`: proceed. Commit the brief and the critique.
6. **Stop.** Overwrite the stub's `## Resume` (at most 6 lines; `next:
   /project-workflow NNNN wave 1`, then "authoritative after the cut:
   <worktree>/STATUS.md", since this section freezes at the cut), write its
   `## Next session prompt` (worktree not yet cut; files to read; stop at the
   wave-1 boundary), commit on the base branch, and echo the prompt. The operator
   reads the brief before any wave. Scope-shaping requirements arrive then;
   production code written before then gets thrown away. Record their rulings
   in the brief's `## Ruled decisions`.

Unattended mode never runs create: scope is never a named default. It may
write a proposal for the operator instead (away-run's propose-only tier).

- Exception: "finish the carried items from NNNN" where NNNN's record already
  enumerates them is fixed scope. Offer direct execution as the default; the
  lint and critique still run.
- Several proposals sharing a file set: propose one contained project, with the
  bundling as an open question. A roadmap of four or more phases splits into
  independent tracks (disjoint ownership, a worktree each, merge gates).

## brief.md

What the project must deliver, what "done" looks like concretely, what is out
of scope. Sections, in order:

| Section | Holds |
|---|---|
| `## Problem`, `## Evidence`, `## Options rejected` | What is wrong and who hits it; the facts it rests on, each with its source; each option not taken, with the reason. |
| `## Deliver` | The outcome, in a few bullets. |
| `## Ruled decisions` | Operator rulings, with id and date. Not reopened by later sessions. |
| `## Open decisions` | Each with the step it blocks and a **named default** (used in unattended mode, and offered first when asking). |
| `## Waves` | Per wave, a stream table for the overview, then one task card per stream (below). |
| `## Done when` | Numbered acceptance criteria, each decidable by a named command or `file:line` check. The acceptance pass rules on exactly these. |
| `## Out of scope` | Each item with its owner (a project, a backlog id, the operator). |

A task card (`### S-NN <slug>`, form in `templates/brief-template.md`) is the
stream's whole contract: the dispatch prompt is built from it and the wave gate
reads its `Owns`. Its eight fields: `Agent` (a worker or `researcher`; the
agent fixes the model, `wave.md` § Roles), `After` (stream ids or none; wave N
holds the cards whose `After` entries all sit in earlier waves), `Mission`,
`Owns`, `Read first`, `Decided` (every choice the stream should not make, each
with its reason), `Done when` (`<command>` -> expected result), and
`Stop and return` (the standard triggers, `wave.md` § Stop and return, plus any
specific to the card).

## What a brief may not say

| May not carry | Why |
|---|---|
| a `model` per card, or an agent outside the role table | The agent's pin is the model. Another model or agent is an operator decision before dispatch. |
| a write outside `Owns` | Every file the stream must produce, tests and deliverable included, needs an entry. "Put it where the schema lives" names no path, so nothing can check it. |
| an unpinned number | Ground-truth claims carry their source and a digest or date, so the stream copies instead of recomputing. |
| an unchecked absence claim | Name the command that looked and what it returned. |
| a skipped commissioned deliverable | Intermediate briefs and audits the commission defines are produced, not folded into the final artifact. One found done elsewhere is mapped explicitly, never silently dropped. |
| an open decision without a default | Unattended mode cannot run past it, and the operator should see the recommended option first. |
