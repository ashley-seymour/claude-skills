# Brief template

> **Path note:** the project-workflow skill's `templates/brief-template.md`.
> `new-project.py` copies everything below the `═══` line into
> `<projects_dir>/current/project-<NNNN>-<slug>/brief.md`.
> Section set: the project-workflow skill's `create.md` § brief.md, preceded by
> Problem, Evidence and Options rejected. Single-session work authors no brief.

> Fill every `<placeholder>`, delete the `<!-- -->` guidance, then run
> `python <skill>/scripts/bin/brief-lint.py <brief.md>`; it checks the task
> cards (the rules are in its module docstring).

═══════════════════════════════════════════════════════════════════════

# Brief: <one-line mission, verb-first>

## Problem

<!-- What is wrong or missing today, and who hits it. A few lines, no plan. -->
- <the problem, in one or two sentences>

## Evidence

<!-- The facts the problem rests on: `file:line`, a command and the output line
     that shows it, a dated figure with its source. An absence claim names the
     command that looked and what it returned. -->
- <claim> -- <command or file:line> -> <what it shows>

## Options rejected

<!-- Each option considered and not taken, with the reason. One line each. -->
- <option>: <why not>

## Deliver

<!-- The outcome, in a few bullets. -->
- <outcome>

## Ruled decisions

<!-- Operator rulings, with id and date. Not reopened by later sessions. -->
- <R-1> (<YYYY-MM-DD>): <ruling>

## Open decisions

<!-- Each with the step it blocks and a named default (used in unattended mode,
     and offered first when asking). -->
- <O-1>: <question>. Blocks: <stream or step>. Default: <option>.

## Waves

<!-- Per wave, a stream table, then one task card per stream. `After` is the
     dependency list: wave N holds the streams whose `After` entries all sit in
     earlier waves. The Agent picks the model through its pin (`wave.md`
     § Roles): `stream-worker` for scoped work, `judgement-worker` for
     judgement, correctness-critical or cross-file design, `mechanical-worker`
     for a known procedure. A card names no model. Every file a stream
     writes, tests and its deliverable `<proj>/artifacts/S-NN-<slug>.md`
     included, needs an Owns entry. Owns never names a shared ledger:
     STATUS.md, the projects index, the backlog, suite-expected.json. Every
     path the Mission or a Done-when command names sits under Owns or Read
     first. Each Done-when item starts with a backticked command. Standard
     stop-and-return triggers: a write is needed outside Owns; a named check
     still fails after two different attempts; a claim in this card is false
     in the tree; the card allows two behaviours and does not choose; the
     change would alter an interface the card does not name. -->

### Wave 1 -- <name>

| Stream | Agent | Owns | After |
|---|---|---|---|
| S-NN <slug> | <agent> | <per-file paths> | <stream ids or none> |

### S-NN <slug>
- Agent: <stream-worker | judgement-worker | mechanical-worker | researcher>
- After: <stream ids or none>
- Mission: <one paragraph, the outcome>
- Owns:
  - <path>
- Read first:
  - <path>
- Decided: <choices already made, each with its reason>
- Done when:
  - `<command>` -> <expected result>
- Stop and return: <the standard triggers, plus any specific to this card>

## Done when

<!-- Numbered acceptance criteria, each decidable by a named command or a
     file:line check. The acceptance pass rules on exactly these. -->
1. `<command>` -> <expected result>

## Out of scope

<!-- Each item with its owner: a project, a backlog id, or the operator. -->
- <item> -- owner: <owner>
