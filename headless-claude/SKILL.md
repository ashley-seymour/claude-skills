---
name: headless-claude
description: Reference for running Claude Code headless with `claude -p`, for scripted model calls and fan-out. Covers invocation shape, overhead-cutting flags, judging a result, and rate-limit concurrency. Use before writing any script that calls `claude -p`. For arming, running or watching an unattended or overnight run, use `away-run`.
---

# Headless Claude

The operator has a Max subscription and no API key, so every scripted model call
goes through `claude -p --output-format json` on subscription auth. Measured
facts carry their date; re-check any that a newer CLI may have changed. Flag
tables, the JSON result fields and worked examples are in
[reference.md](reference.md).

## Invocation

- **Prompt on stdin, not argv.** Windows caps a command line at 32,767 chars.
  Pipe the prompt to `claude -p` with no positional argument; pass a system
  prompt as `--system-prompt-file <path>`.
- **Tool-less model calls:** add `--strict-mcp-config --setting-sources=` (the
  `=` form; PowerShell 5.1 drops a space-separated `""`). Cuts per-call cache
  creation from ~28.9K tokens to ~139 (2026-08-03).
  **Never use it on a run that has tools.** It skips the global
  `settings.json`, so `permissions.deny` stops applying, and it hides user-level
  agents, so `--agent` fails. Evidence in reference.md.
- **Agentic runs** (tools, edits, commands): use a lean role agent. The agent's
  `tools:` becomes the run's whole tool list, and settings still load, so deny
  rules hold.

  ```
  claude -p --agent stream-worker --disable-slash-commands --strict-mcp-config --output-format json
  ```

  Use `--agent reviewer` (read-only) for a verification pass. Omit
  `--disable-slash-commands` for an agent that uses the Skill tool (e.g.
  `away-orchestrator`): it disables all skills.
- **Model and effort come from the agent frontmatter.** Without one, the model
  is the session's model (`settings.json` `model`) at the default effort.
  Pass no `--effort` as a standing default: raise effort per run, for a
  stated reason. Set no cache-TTL setting: the defaults stand.
- Do not use `--bare`: it forces `ANTHROPIC_API_KEY` auth and ignores OAuth.
- No flag caps output tokens or sets `cache_control`. `--max-budget-usd` is a
  runaway guard in imputed dollars; otherwise enforce budgets after the fact
  from the returned `usage`.
- `--permission-mode bypassPermissions` changes the security posture. Say so
  plainly to the operator before using it.

## Judging a result

- Read the JSON, not the exit code or the elapsed time. A 429 still exits and
  still writes a well-formed envelope. Judge by `is_error` and
  `api_error_status`, plus whether the expected output actually changed.
  Do not judge by `num_turns`: a short run can be a success and a long one a
  failure.
- The envelope is written once, at exit. A 0-byte output file mid-run is
  normal; confirm liveness with a process check before calling it dead.
- Have every run write outputs incrementally and reruns skip finished ones. A
  run cut off after batching its writes leaves nothing to recover.

## Concurrency and the rate limit

Cost is imputed; the real ceiling is plan quota (the 5-hour window, plus a
weekly limit shared across models). Default to concurrency ~2 for Opus-class
runs, and plan to resume across windows. Raising effort burns the window fast
(evidence in reference.md), so do it per run, for a stated reason, never as a
default.

## Unattended and overnight runs

Arming, running and watching an unattended run (scheduled relaunch, lock,
watchdog, usage-limit sleep, disarm) belongs to the `away-run` skill.
