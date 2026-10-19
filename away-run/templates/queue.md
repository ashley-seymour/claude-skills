# Away queue — <repo>, <date>

started:   <YYYY-MM-DD HH:MM>
hard-stop: <YYYY-MM-DD HH:MM>
gates:     <none | on>   # "none" only if the operator opted in for this run
armed-by:  <session or operator>, from discover.md tier 1 (tier 2 went to proposals)

<!--
One row per item. The watchdog parses this table; keep it the only table here.
- id: Q-01, Q-02 ... never reused. item: one line naming the goal and its check.
- agent: mechanical-worker | stream-worker | researcher | project | discover
  (project = one step group of a wave via /project-workflow <NNNN> in unattended
  mode, item "<NNNN> wave <N> from step <n>", per iteration.md § A `project` row;
  discover = append tier-1 rows per discover.md, at most 5).
- status: pending | claimed | done | blocked
- claimed_by: iter-<NNN>; lease_until: YYYY-MM-DD HH:MM local; both empty unless claimed.
  A claim whose lease has passed belongs to a dead iteration and is claimable again.
- attempts: claims so far; at the cap the row goes blocked with a reason.
- evidence: commit sha, check output line, or blocked reason. No `|` in any cell.
Edit only while holding the queue lock (mkdir <scratch>/queue.lock; rm -rf it after).
-->

| id | item | agent | status | claimed_by | lease_until | attempts | evidence |
|---|---|---|---|---|---|---|---|
| Q-01 | <goal; check that proves it> | mechanical-worker | pending |  |  | 0 |  |
| Q-02 | <goal; check that proves it> | stream-worker | pending |  |  | 0 |  |
