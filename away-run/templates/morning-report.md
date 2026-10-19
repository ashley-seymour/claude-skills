---
run: away/<date>
repo: <repo>
report_for: <date> morning
generated: <YYYY-MM-DD HH:MM>
---

# Morning report — away run <date>

Three-minute read. Ground truth is `queue.md`, `log.md` and `decisions.md`; this
file summarises them.

## Bottom line

- Rows: <done> done, <blocked> blocked, <pending> never started, of <total>.
- Suites at close-out, on `away/<date>`: <suite> <passed> passed (<baseline>
  before). Tree: `git status --short` <empty | what is left>.
- Watchdog: <iterations> iterations; exit reason <from watchdog.log>.
- Branch `away/<date>` holds <N> commits; nothing pushed or merged.

## What changed

One bullet per done row: id, what changed, commit sha, the check that proved it.

## Blocked and unfinished

One bullet per blocked or claimed-at-close row: id, reason, attempts, and the
first step to unblock it.

## Found but not fixed

From `log.md` § Found, ranked by what it costs to leave alone. Each with
`where`, evidence and its home: a queue row for the next run (tier 1) or a
proposal file path (anything else).

## Decisions I made for you

Every `decisions.md` entry the operator might have chosen differently, with
its one-step reversal.

## Proposed, not done

Tier-2 items from discovery (scope-shaping work the run may only propose):
one line each, with the proposal file path.

## Numbers

| Metric | Before | After |
|---|---|---|
| Queue rows done / total | 0 / <n> | <d> / <n> |
| <suite> passed | | |
| Iterations (productive / limit, budget or error) | — | <p> / <e> |
| Imputed cost, sum of iteration `total_cost_usd` | — | $<x> |

## Machine state

- Scheduled task `away-<repo>-<date>`: <still registered: run disarm.ps1 | unregistered>.
- Worktree `<path>` on `away/<date>`: kept for review; merge or delete it.
- Scratch `<path>`: untracked; delete once reviewed.
