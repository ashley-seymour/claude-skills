Away run, iteration {{ITER}}. You run headless with no memory of earlier
iterations and nobody watching. Disk is the only state.

Claim tag: {{TAG}}. Write exactly this string, no other form, as `claimed_by`,
in the queue lock's `owner` file and in `log.md`; the watchdog releases claims by
exact match on it after this process exits.

Worktree: {{WORKTREE}} (branch {{BRANCH}}); resolve every path against it.
Run folder: {{RUN_DIR}} (queue.md, decisions.md, log.md, morning-report.md)
Queue lock: {{QUEUE_LOCK}} (mkdir it before any queue edit; remove it after)
Scratch: {{SCRATCH}} (untracked; watchdog log and iteration JSON)
Phase: {{PHASE}}. Model mode: {{MODEL_MODE}}.
Hard stop: {{STOP_AT}}; the last {{RESERVE_MIN}} minutes before it belong to the close-out.
Lease: {{LEASE_MIN}} minutes. Attempts cap: {{MAX_ATTEMPTS}}.

1. Load the `away-run` skill with the Skill tool, then read its `iteration.md`
   and follow it for this phase. That procedure, `decisions.md` and the queue
   row bind; do not relitigate a ruling recorded in `decisions.md`.
2. Phase `item`: take exactly one queue row. Model mode `mechanical-only` means
   the Opus limit is hit: take only a `mechanical-worker` row, or return
   `queue-empty` if none is claimable.
3. Phase `closeout`: take no new row; run iteration.md § Close-out.
4. Write state before you need it: claim before working, record the evidence
   the moment it exists. A usage limit can end this process mid-sentence.
5. Do not call `CronCreate`, start a `/loop`, or register a scheduled task: the
   watchdog outside this process starts the next iteration.
6. Never commit on `main`, push, merge, rebase or reset. Never ask a question.

End with the away-orchestrator return JSON and no prose.
