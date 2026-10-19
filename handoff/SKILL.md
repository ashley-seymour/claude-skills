---
name: handoff
description: Write a self-contained prompt to paste into a fresh session after /clear.
disable-model-invocation: true
---

Write a handoff so a fresh session with no memory of this one can continue the work.

1. If this repo has its own resume mechanism (for example a project `STATUS.md`
   with a Resume section), update that first, then point to it in the prompt.
2. Check the facts you are about to hand over: run `git status --short` and
   `git log --oneline -5`, and confirm any file paths you name exist.
3. Output ONE fenced code block, ready to paste, containing:
   - **Goal**: what we are trying to achieve, in one or two sentences.
   - **Done**: what is finished, with file paths and commit hashes.
   - **In progress**: anything half-done, with its exact state (uncommitted
     files, running jobs, open branches or worktrees).
   - **Next step**: the single concrete action to take first.
   - **Decisions**: choices already made and why, so they are not reopened.
   - **Open questions**: anything waiting on me.
   - **Read first**: the few files the next session should open, most important first.

Keep it tight: facts the next session cannot cheaply rediscover, not a narrative.
Do not paste file contents; name the files. After the block, add nothing except
the Problem/Done/Outstanding summary the global CLAUDE.md requires.

$ARGUMENTS
