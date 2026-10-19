# project-workflow scripts

The skill's scripts. Run them from inside the repo they act on: each finds the
repo from the current directory (`git rev-parse --show-toplevel`, and the main
checkout from `git worktree list --porcelain`), never from where the script
lives. Per-repo values come from `<repo>/.claude/project-workflow.json`
(`lib/config.py` documents the keys; every key has a default). A project
worktree's own copy of that file wins; without one, the main checkout's
applies.

Requires Python 3.10+ and PyYAML (`pip install pyyaml`).

| Script | Does |
|---|---|
| `bin/new-project.py` | scaffold a project, cut its worktree, run the repo's `bootstrap` |
| `bin/brief-lint.py` | lint a brief's task cards (standard library only) |
| `bin/wave-close.py` | the wave gate; `--suites-only` runs only the suites, `--capture-pin` writes the suite pin |
| `bin/close-phase.py` | rewrite STATUS.md's Resume and next-session prompt at a wave boundary |
| `bin/save-return.py` | save a subagent's final return to a file |
| `bin/close-project.py` | close a project: archive, merge (`close.merge`), teardown, remove the worktree |

Tests: `python -m pytest -q skills/project-workflow/scripts/tests` from the
config repo root.
