# Headless Claude: reference

Detail behind [SKILL.md](SKILL.md). Every flag here was checked against
`claude --help` on Claude Code 2.1.284 (2026-09-29), except
`--system-prompt-file`, which `--help` names only inside the `--bare` text; it
was confirmed by the CLI rejecting a missing file ("System prompt file not
found"). Re-check after a CLI upgrade.

## Flags

| Flag | Use it for | Notes |
|---|---|---|
| `-p`, `--print` | Every scripted call | Skips the workspace trust dialog, but on an untrusted workspace (a fresh repo or scratch dir) ignores the project `.claude/settings.json` `permissions.allow` and `additionalDirectories` (stderr: "Ignoring N permissions.allow entries ... this workspace has not been trusted"), so every Bash call is denied. Pass the same file with `--settings <file>` instead. Trust does not gate hooks: `settings.local.json` hooks still run there. Settings files that fail validation are silently ignored. |
| `--output-format json` | Every scripted call | One JSON envelope, written at exit. `stream-json` streams events instead. |
| `--system-prompt-file <path>` | Long system prompts | Keeps the prompt off the command line. |
| `--agent <name>` | Agentic runs | The agent's `tools:` is the whole tool list; `model:` applies (confirmed by `modelUsage`); `effort:` parses but its effect is unobserved (Claude Code 2.1.284). |
| `--disable-slash-commands` | Agentic runs | Disables all skills; cuts skill listings from the prompt. Omit it for an agent that uses the Skill tool (e.g. `away-orchestrator`). |
| `--strict-mcp-config` | Tool-less and agentic runs | Loads only MCP servers from `--mcp-config`; with none given, none load. |
| `--setting-sources=` | Tool-less calls only | Empty list loads no user, project or local settings. Drops deny rules and user-level agents. `--setting-sources project,local` loads a project skill over a user skill of the same name. |
| `--max-budget-usd <n>` | Runaway guard | Imputed dollars on a subscription; `--print` only. |
| `--max-turns <n>` | Runaway guard | Absent from `claude --help` but accepted (2.1.284-2.1.285). At the cap: `"subtype": "error_max_turns"`, `"is_error": true`, exit 1. `num_turns` is not a direct count against the cap (a run under `--max-turns 4` reported 5, `is_error` false): judge by `subtype`/`is_error`. For `--agent` runs, frontmatter `maxTurns` is the alternative. |
| `--permission-mode <mode>` | When the run must not prompt | `bypassPermissions` changes the security posture; tell the operator. |
| `--model <alias>` | A one-off override | Prefer the agent frontmatter. |

Do not use:

| Flag | Why |
|---|---|
| `--bare` | Auth is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper`; OAuth is never read, so a subscription-only machine fails. |
| `--effort <level>` as a default | Effort comes from the agent frontmatter or the default. Raise it per run only, for a stated reason: higher effort burns the usage window (§ Rate-limit evidence). |
| Cache-TTL settings (`promptCacheTtl`, `subagentPromptCacheTtl`, agent `cacheTtl`) | Keep the defaults (main 1h, subagents 5m). A `claude -p` run already gets the 1h main-conversation TTL on a subscription. |

No flag caps output tokens or sets `cache_control`.

## Evidence for the lean flags

- 2026-08-03: `--strict-mcp-config --setting-sources=` cut per-call cache
  creation from ~28.9K tokens to ~139 input tokens on a tool-less call.
- 2026-09-29, throwaway repo, `bypassPermissions`: with `--setting-sources=`,
  `git reset --hard` and an Edit to `.env` both went through; a default run
  blocked both. The same flag made `--agent stream-worker` fail with
  `--agent 'stream-worker' not found` (user-level agents hidden).
- 2026-09-29, trivial prompt, first-request input (input + cache creation +
  cache read):

  | Invocation | Tools | Skills | Input tokens |
  |---|---|---|---|
  | default `claude -p` | 77 | 38 | 30,683 |
  | `--agent stream-worker` + lean flags | 6 | 0 | 8,453 |
  | `--agent reviewer` + lean flags | 4 | 0 | 7,760 |

  Global deny rules still blocked under the lean flags (`permission_denials`
  populated, sentinel intact).

## JSON result fields

Field names confirmed from a live envelope on 2.1.284 (2026-09-29).

| Field | Read it as |
|---|---|
| `is_error` | The run failed. Primary verdict. |
| `api_error_status` | HTTP status of an API failure (429 = rate or usage limit). Null on success. |
| `result` | The final assistant text. |
| `usage` | Tokens: input, output, cache creation, cache read. Enforce budgets from this. |
| `modelUsage` | Per-model usage; shows which model actually ran. |
| `total_cost_usd` | Imputed on a subscription, not billed. |
| `permission_denials` | Tool calls the permission rules blocked. |
| `num_turns` | Informational only. Not a success signal. |
| `duration_ms` | Excludes time spent waiting on background subagents (141,032 ms against 356 s wall in one run); time a watchdog on wall clock. |
| `session_id` | For `--resume`. |

Pair the envelope with an outcome check: did the file, queue or output the run
was meant to change actually change?

## Rate-limit evidence

- 2026-08-16: one top-tier run at `--effort max` used a whole 5-hour window in
  11 turns.
- 2026-08-19: four `xhigh` stages at concurrency 2 fitted in one window.
- September 2026: a plan limit was hit on at least 7 days, one of them weekly.

These are why effort stays at the default and concurrency stays near 2.

## Examples

Tool-less call from bash, prompt on stdin:

```bash
claude -p --strict-mcp-config --setting-sources= --output-format json \
  --system-prompt-file prompts/system.md < prompts/task.md > out/result.json
```

Agentic stream from PowerShell 5.1, prompt on stdin:

```powershell
Get-Content -Raw prompt.md | claude -p --agent stream-worker `
  --disable-slash-commands --strict-mcp-config --output-format json |
  Set-Content -Encoding utf8 result.json
```

Verdict in Python:

```python
import json
r = json.load(open("result.json", encoding="utf-8"))
if r.get("is_error") or r.get("api_error_status"):
    raise RuntimeError(f"run failed: {r.get('api_error_status')} {r.get('result','')[:200]}")
```
