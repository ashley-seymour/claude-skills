# Wave — dispatch, review, triage, fix, boundary

Loaded from `SKILL.md` on `/project-workflow NNNN wave N`; `<bin>` and the config keys are defined there.

## Before dispatch

- **Premise check.** One cheap check on a wave an earlier session wrote: read
  the function its load-bearing premise names, or run the suite it calls red.
- **First dispatch only: cut the worktree.** The brief is committed and
  critiqued on the base branch, so the cut carries it:
  `python <bin>/new-project.py <slug> --resume-id <NNNN> --yes` cuts
  `<repo>-worktrees/<NNNN>-<slug>` on `project/<NNNN>-<slug>` and runs the
  config's `bootstrap` argv; a non-zero bootstrap refuses, and a rerun retries
  it. The worktree's STATUS.md drops `stub: true`, reads `status: active` and
  becomes authoritative, and the worktree's index row flips `planned` ->
  `active`; the base-branch stub and row are frozen as `planned` until close.

## Waves are layers of the `after:` graph

Wave 1 is every stream with no `After`; wave N is every stream whose `After`
entries all sit in earlier waves. A stream never shares a wave with anything it
depends on. A cycle is a brief defect: send it back to the critique.

- Three to six streams per wave: `owns:` disjointness is checked pairwise, and
  six streams is already fifteen pairs; a wider layer splits into two waves.
- A long compute run gets its own wave, apart from the stream that writes the script.
- If a defect would invalidate the figures the next wave publishes, fix it first:
  lead with the repair, cost it by measurement, offer to cancel in-flight compute
  whose premise just changed.

## Roles

| Role | Agent (`subagent_type`) | Model (pinned) | Tools |
|---|---|---|---|
| scoped build, fix, refactor | `stream-worker` | Sonnet | Read, Grep, Glob, Edit, Write, Bash |
| judgement: cross-file design, correctness-critical logic, a card the brief marks judgement | `judgement-worker` | Opus | as `stream-worker` |
| known procedure: regenerate, rebuild, sweep | `mechanical-worker` | Sonnet | as `stream-worker` |
| review, `Mode: brief\|wave\|confirm\|acceptance` | `reviewer` | Opus | Read, Grep, Glob, Bash (read-only) |
| external research | `researcher` | Sonnet | Read, Grep, Glob, Bash, Write, WebFetch, WebSearch |

The model is chosen by choosing the agent: each agent file pins its `model`,
and a dispatch passes no `model`. The global `hooks/context-gate.py` denies an
Agent call whose `model` differs from the pin of `stream-worker`,
`judgement-worker`, `mechanical-worker` or `reviewer`, naming the agent to use
instead; it lets `opus` on `stream-worker` through, for the fallback below.

Agent files load once per process; `/clear` and edits do not reload them. On "type not found",
or a registered description that differs from the file, never ask for a restart: dispatch
`stream-worker` (also for `mechanical-worker` and `judgement-worker` rows), `reviewer` or
`researcher`, passing the missing agent's own pinned `model` from its frontmatter, its body
pasted as the prompt's first block; record the swap in `## Log`.

Definitions: the global `agents/` folder. No stream agent has Agent, Skill,
PowerShell or MCP tools, and only `researcher` has the web; a stream needing one
is an operator decision. (`away-orchestrator` is away-run's main thread, never a stream.)

## Dispatch

All streams in a wave go out in **one message with multiple Agent calls**, built
from the task cards. Each prompt contains only:

```
Mission: <the card's Mission and Decided>
Done when: <the card's Done when>
Workspace: <project worktree absolute path> — resolve every path against it,
  never against the main checkout. One command per Bash call, `git -C <abs
  path>`, absolute paths; no `cd X && ...` or shell variables.
You may write ONLY these paths: <Owns>
Read these first: <Read first>
Deliverable: artifacts/<id>-<slug>.md
Stop and return `blocked`, with the question and options seen, when: <the card's Stop and return>
Review rubric: logic errors, unhandled cases, broken callers, tests that do not
  pin what they claim, stale numbers or names left in your owns. A reviewer
  will apply exactly this; meet it before returning.

Do NOT run git commit. Do NOT write outside the paths listed above.
Return EXACTLY this JSON object and no prose:
{"status":"ok|blocked|failed","updated_paths":[...],"found":[...],
 "summary_1line":"<=120 chars","blocked_reason":"<only when blocked>"}
```

`found` lists issues noticed outside the stream's scope, unfixed, in the
agent's `found` item shape. A stream the brief marks `design: review` first
returns a design note only (`status: ok`, no code); a `Mode: brief` reviewer
checks it, then the stream is continued (`SendMessage`) to build. A stream that
writes a heuristic or guard names its coverage list in the mission; a form
outside that list is not a finding against it.

A **reviewer** row replaces the write list, the stop line, the rubric and the
return block with `Mode: brief|wave|confirm|acceptance` and "Return the reviewer
JSON for that mode." Brief and acceptance modes add "with a `deliverable_markdown`
field". Reviewers cannot write; save every return (stream or reviewer) with
`python <bin>/save-return.py <agent id> <dest>.json --json`, never by re-typing
it. Reviewers need the absolute-path workspace line too: spawned in the main
checkout, they read an unmerged branch's new files as "does not exist".

## Stop and return

Every worker stops and returns `blocked`, with the question and the options it
saw, when: a write is needed outside its owns; a named check still fails after
two different attempts; a claim in its card is false in the tree; the card
allows two behaviours and does not choose; or the change would alter an
interface the card does not name. A card may add triggers. The orchestrator
answers each `blocked` return as it arrives, before step 2 (this is not the fix batch):

- **Answerable from the brief, a ruling or one cheap check** (a fact, a path
  no other stream in the wave owns): continue the same subagent with
  `SendMessage` to its agent id, giving the answer and any added owns path.
  Without `SendMessage`, re-dispatch fresh as for a dead stream (`recovery.md`).
- **A judgement the card did not make** (a design choice, two behaviours with
  real trade-offs, a false premise that changes the plan): re-dispatch the card
  to `judgement-worker` with the question, the decision as a `Decided` line,
  and an inventory of the partial work ("audit this, do not assume it works").
- **An operator decision** (scope, a ruled decision reopened, an open decision
  with no default): the stream stays `blocked`, the question goes to
  `## Resume` `decisions:`. Unattended: take the named default, else leave it.

## Session shape

One session runs at most one wave, in this order, and never repeats a step:

1. **Dispatch** the wave's streams, at most six, deferred-finding fix streams
   included (§ Triage). Answer `blocked` returns (§ Stop and return).
2. **Suite once.** `python <bin>/wave-close.py <NNNN> --return S-01:<saved
   return> ...` checks each return's `updated_paths` and `git status` against
   the owns union of this wave's cards, runs the configured `suites`
   concurrently, and compares the counts with the `suite_pin` floor (0 skipped:
   pytest exits green on a skipped or uncollected test, the gate does not). Put
   an owns path added mid-wave (a continued stream's extra path, a fix or sweep
   stream's owns) in a card's `Owns` in the brief first, or pass the whole union
   as `--owns` (it replaces the cards'). The gate accepts `blocked` and `failed`
   and opens no deliverable: confirm by hand that each return is `status: ok`
   and its deliverable exists. Refusals and misses join the triage list. This
   is the wave's one suite run.
3. **Review once.** One `Mode: wave` reviewer per stream that wrote logic.
   Mechanical regeneration closes on the orchestrator's check instead; the
   artifact names it.
4. **Triage once** (§ Triage), the whole list in one pass, before dispatching
   anything.
5. **Fix batch**, one stream per owning file group, run to § Fix protocol, plus
   the minor sweep in parallel.
6. **Confirm once.** One `Mode: confirm` reviewer over the fix diffs only;
   what it raises is triaged as in step 4 and goes to the ledger, never fixed this session.
7. **Boundary** (below).

**Context budget.** At 150k context dispatch nothing new: land what is in
flight and go to the boundary (under `steps <a>-<b>`, checkpoint instead;
`SKILL.md` § Unattended mode). At 200k stop at once: commit what is on disk as
`chore(<NNNN>): checkpoint wave N at step <n>`, `<n>` the last step completed,
with `## Resume` `next:` naming the step to run next. The global
`hooks/context-gate.py` (PreToolUse on Agent) notes the soft limit and refuses
dispatches from the hard one, except a `reviewer` so the review in flight can
finish; it reads the transcript, so it is the measure, not a guess. A session
may start at whatever step `## Resume` names (`next:` after `checkpoint wave N
at step <n>`; "review wave N" is step 2), reading saved returns and review JSON
from disk instead of repeating earlier steps.

## Triage

Confirm an unconfirmed finding with one cheap check first. Then each finding
goes to exactly one place:

| Finding | Goes to |
|---|---|
| blocker or major in this wave's diff, or a gate refusal | fix batch, this session |
| any finding in a path a later stream `consumes:`, or that would change figures a later wave publishes (blast radius) | fix batch, this session |
| minor: comment, docstring, doc or artifact text only; a reviewer's `notes` | minor sweep, this session |
| anything else: out of the diff, pre-existing, no downstream reader | the ledger, next wave |
| refuted | dropped, naming the check |
| cannot be fixed in the repo (data, real run, outside action) | the `backlog_file`, with evidence and a named unlock trigger |

- **The ledger** is `artifacts/follow-ups.md` (close.md § 2's table), kept
  from wave 1. Next wave, each open row is attached to the stream that owns its
  file, as a line in that stream's mission; a row whose file no stream owns
  gets a fix stream in that wave. After the last wave, open rows become one fix
  wave before close. Nothing in it is a carry; carrying to another project
  still needs a dated operator ruling.
- **The minor sweep** is one `mechanical-worker` closed on the orchestrator's
  grep or test, no review. It may touch only comments, docstrings, docs and
  artifact text; a stale number inside a test or code is not minor.
- **A finding a review can always produce again** (another evasion form of a
  heuristic, another wording) is a design question for the operator.

## Fix protocol

Measure twice: a fix stream's mission names the finding and requires, in order:

1. **Reproduce**: a test (or check) that fails for the stated reason.
2. **Siblings**: grep for every site with the same pattern; fix all, list them.
3. **Blast radius**: callers of each changed function, and their tests run.
4. Fix, then the reproduction passes.

A fix that is more than a few lines, or whose cause is unclear, first gets a
diagnosis-only stream (`stream-worker`, owns only its deliverable, returns
cause and plan); the orchestrator approves the plan before a worker builds it.

## Wave boundary — stop here

1. Fix and sweep returns: `python <bin>/wave-close.py <NNNN> --skip-suites
   --return ...` checks them and the tree against the owns union (their owns
   added as at step 2); confirm each `status: ok` and deliverable by hand.
2. Commit the wave on the project branch: `<type>(<NNNN>): <subject>`.
3. `python <bin>/close-phase.py <NNNN> --wave <N+1> --state ... --next ...
   --decisions ... --prompt-file <file>` writes `## Resume` and
   `## Next session prompt`, refusing a malformed prompt (after the last wave,
   omit `--wave`; `--next` is the fix wave or the close). Mark the wave
   `status: done`, add one `## Log` line, commit, echo the prompt. Pasted cold
   after a clear, it names the project and wave, the worktree path, the files to
   read first in order (`follow-ups.md` open rows among them), decisions ruled,
   what is out of bounds, and the stop. It points at files instead of
   restating them: 20 lines at most.
4. **Stop. Tell the operator the wave is closed and they can `/clear`.**
   Unattended: return and exit instead.

Do not begin the next wave in the same session. The stop is the point. After the
last wave, the next step is the fix wave for open ledger rows, then `close.md`.
