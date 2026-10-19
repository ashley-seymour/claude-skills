"""Rewrite `## Resume` AND `## Next session prompt` in STATUS.md, in one call,
and REFUSE to close the phase without a well-formed handoff prompt.

Usage (run from inside the repo; the repo is found from the current directory):
    python <skill>/scripts/bin/close-phase.py <project-id> \
        --wave 5 --state "..." --next "..." --decisions "..." \
        --prompt-file artifacts/W5-prompt.txt

    python <skill>/scripts/bin/close-phase.py <project-id> --check-only

`--wave N` writes `current_wave` (the key the project-workflow skill bumps).
Without `--wave` (the after-last-wave close) `current_wave` is left as found.
The script writes no `current_phase` or `phase_state`; keys already in a STATUS
file are left untouched.

From Git Bash, prefix `MSYS_NO_PATHCONV=1`: MSYS rewrites an argument such as
`--next /project-workflow 0007 wave 3` to `C:/Program Files/Git/project-workflow`.
The `--prompt-file` may be raw or wrapped in one ``` fence (`unfence_prompt`).

What this guarantees, and what it does not
------------------------------------------
It guarantees the `## Next session prompt` block **exists** and is
**well-formed**. It does NOT guarantee the block is *useful*. The judgement
content — what is out of bounds for the next phase, which decisions were ruled
and on what evidence — is still written by the session closing the phase. This
script only makes it impossible to close without one, which is the thing prose
has never achieved: `SKILL.md` has required a "literal pasteable command" the
whole time and phases still closed without one, because nothing checked.

Why a script rather than a rule
-------------------------------
No `Stop` hook survives the end of a session, so the handoff must be written
BEFORE the stop or it is lost. The inputs are already structured data sitting in
STATUS.md — project id, phase, branch, worktree, deliverable path — so producing
the block is assembly, not authorship.

Well-formedness, concretely
---------------------------
The prompt must be a fenced block that names: the project id, the phase, a repo
or worktree path that EXISTS on disk, at least two files to read first, and a
stopping condition. Each check exists because a real handoff omitted it. A path
that does not resolve is rejected rather than warned about — a cold session
pasting a prompt cannot tell a typo from a moved file.

STATUS.md resolution
--------------------
The project WORKTREE copy wins when a worktree exists. `main`'s copy is the
scaffold-time stub the workflow leaves behind until close, frozen at wave 0 or 1
forever. Measured 2026-09-20: main said wave 0 for a project sitting at wave 6.

The tree comes from `streams.find_project_tree`, the lookup `wave-close.py`
shares. It never falls back to `main` on its own: an ambiguous match, or no
match at all, REFUSES (exit 2) and names `--status`. `main` is used only when
the main checkout is on the project branch, or when main's STATUS.md declares
`worktree: none` (a session-mode project). The old `-{id}` name match never
found `<repo>-worktrees/<NNNN>-<slug>` and wrote the handoff into main's frozen
stub.

Resume caps
-----------
`## Resume` is pointers, not narrative: at most RESUME_MAX_LINES non-blank lines.
A write that would exceed it is refused, and so is `--check-only` on a STATUS.md
whose `## Resume` already does. `deadlines:` frontmatter (dated external
commitments, `YYYY-MM-DD <what>`) is preserved as found unless `--deadline` or
`--no-deadlines` is passed.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from config import ConfigError, RepoError, config_for  # noqa: E402
from config import main_checkout as _main_checkout  # noqa: E402
from streams import (  # noqa: E402
    WorktreeLookupError,
    find_project_tree,
    same_path,
)

RESUME_HEADING = "## Resume"
PROMPT_HEADING = "## Next session prompt"

#: Minimum body lines inside the fence. A one-liner is a slash command wearing
#: a fence, which is the exact failure the rule exists to stop.
MIN_PROMPT_LINES = 8

#: Maximum body lines inside the fence, per the project-workflow skill
#: (wave.md: "It points at files instead of restating them: 20 lines at most").
MAX_PROMPT_LINES = 20

#: `## Resume` cap, per the project-workflow skill: pointers, not narrative.
RESUME_MAX_LINES = 6

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DEADLINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) \S")
_SESSION_MODE_RE = re.compile(r"^worktree:[ \t]*none[ \t]*$", re.MULTILINE)

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_STOP_RE = re.compile(r"\bstop(ping)?\b", re.IGNORECASE)
# The optional leading slash keeps a POSIX absolute path whole; without it the
# path matched as a relative one and never resolved on Linux.
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][\w\\/.\-]+|/?(?:[\w.\-]+/)+[\w.\-]+")

# A Windows console is cp1252, and every STATUS.md in this repo is full of em
# dashes. Without this, printing a handoff block back to the operator dies with
# UnicodeEncodeError -- and so does argparse's own `--help`.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def main_checkout() -> Path:
    """The main checkout of the repo the current directory is in."""
    return _main_checkout(Path.cwd())


def repo_root_of(directory: Path) -> Path | None:
    """Toplevel of the git repo holding `directory`, or None (not a repo).

    `--status` roots the prompt's relative paths here, so a file that exists only
    in the STATUS.md's own worktree resolves.
    """
    if not directory.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return None
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else None


def _glob_status(root: Path, project_id: str, main: Path) -> Path | None:
    """STATUS.md under `root`, with `projects_dir` from root's config, else main's."""
    projects_dir = config_for(main, root).projects_dir
    hits = sorted(root.glob(f"{projects_dir}/*/project-{project_id}-*/STATUS.md"))
    return hits[0] if hits else None


def _declares_session_mode(status_path: Path | None) -> bool:
    """True when STATUS.md frontmatter says `worktree: none`."""
    if status_path is None or not status_path.is_file():
        return False
    match = re.match(r"^---\n(.*?)\n---\n",
                     status_path.read_text(encoding="utf-8"), re.DOTALL)
    return bool(match and _SESSION_MODE_RE.search(match.group(1)))


def find_status(project_id: str, main: Path) -> tuple[Path | None, Path]:
    """(STATUS.md path, the tree it was found in).

    Raises :class:`WorktreeLookupError` rather than falling back to `main`
    when the project tree is ambiguous or cannot be found. `main` is returned
    only when it IS the tree: on the project branch, or declared session mode.
    """
    tree = find_project_tree(project_id, main)
    if tree is not None:
        return _glob_status(tree, project_id, main), tree
    main_status = _glob_status(main, project_id, main)
    if _declares_session_mode(main_status):
        return main_status, main
    raise WorktreeLookupError(
        f"no worktree matches project {project_id}, and main's STATUS.md does "
        f"not declare `worktree: none`, so main's copy is the frozen stub.")


# ---------- well-formedness -------------------------------------------------


def section_bounds(text: str, heading: str) -> tuple[int, int] | None:
    """(start, end) character offsets of one `## ` section, or None.

    The heading must be matched ANCHORED TO A LINE START. Matching it loosely
    finds the same words quoted inside a table cell or a sentence -- this
    project's own STATUS.md names `## Next session prompt` in its phase-map row
    74 lines above the real heading, and a `str.find` picked the table.

    The scan for the next heading skips fenced blocks, because a handoff prompt
    routinely quotes headings it is telling the next session to overwrite.
    """
    match = re.search(rf"^{re.escape(heading)}[ \t]*$", text, re.MULTILINE)
    if match is None:
        return None
    start, in_fence = match.start(), False
    for line in re.finditer(r"^.*$", text[match.end():], re.MULTILINE):
        stripped = line.group(0).strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and stripped.startswith("## "):
            return start, match.end() + line.start()
    return start, len(text)


def extract_prompt(text: str) -> str | None:
    """The fenced body under `## Next session prompt`, or None."""
    bounds = section_bounds(text, PROMPT_HEADING)
    if bounds is None:
        return None
    match = _FENCE_RE.search(text[bounds[0]:bounds[1]])
    return match.group(1) if match else None


def resume_lines(text: str) -> list[str]:
    """Non-blank body lines of the `## Resume` section (empty when absent).

    HTML comments are stripped first: the status template carries a marker and
    a guidance comment there that no reader sees, and counting them refused
    every freshly scaffolded project.
    """
    bounds = section_bounds(text, RESUME_HEADING)
    if bounds is None:
        return []
    section = _HTML_COMMENT_RE.sub("", text[bounds[0]:bounds[1]])
    body = section.splitlines()[1:]
    return [ln for ln in body if ln.strip()]


def check_resume(lines: list[str]) -> list[str]:
    """Every reason a `## Resume` body breaks the cap. Empty list = within it."""
    if len(lines) > RESUME_MAX_LINES:
        return [f"`{RESUME_HEADING}` has {len(lines)} non-blank lines; the cap "
                f"is {RESUME_MAX_LINES}. Pointers, not narrative -- move detail "
                f"to artifacts/ or `## Log`."]
    return []


def format_deadlines(deadlines: list[str] | None, none: bool) -> str | None:
    """The `deadlines:` frontmatter value to write, or None to preserve.

    Raises ValueError on an entry that is not `YYYY-MM-DD <what>` with a real
    date, since a malformed deadline is one the resume check cannot surface.
    """
    if none:
        return "none"
    if not deadlines:
        return None
    for entry in deadlines:
        match = _DEADLINE_RE.match(entry.strip())
        if match is None:
            raise ValueError(f"deadline {entry!r} is not `YYYY-MM-DD <what>`")
        date.fromisoformat(match.group(1))
    return json.dumps([d.strip() for d in deadlines], ensure_ascii=False)


def unfence_prompt(raw: str) -> tuple[str | None, str | None]:
    """(prompt body, refusal). The `--prompt-file` content, outer fence removed.

    The file is meant raw, but a prompt copied out of STATUS.md arrives with its
    own ``` fence. Written as-is, that fence nests inside the one this script
    adds; the extractor then pairs the first two fence lines and reads an empty
    block. One enclosing fence is stripped. Any other fence line
    is refused: it would end STATUS.md's block early wherever it sat.
    """
    lines = raw.strip().splitlines()
    if (len(lines) >= 2 and lines[0].strip().startswith("```")
            and lines[-1].strip() == "```"):
        lines = lines[1:-1]
    nested = [i for i, ln in enumerate(lines, 1) if ln.strip().startswith("```")]
    if nested:
        return None, (f"--prompt-file holds a ``` fence line inside the prompt "
                      f"(body line {nested[0]}); it would end the STATUS.md "
                      f"block early. Remove it; at most one enclosing fence is "
                      f"stripped.")
    return "\n".join(lines).strip(), None


def path_candidates(body: str) -> set[str]:
    """Path-like tokens in a prompt, as written.

    rstrip("."): the class admits `.`, so a sentence's full stop was captured
    into the path. Windows drops a trailing dot when resolving; Linux does not.
    """
    return {m.group(0).rstrip(".") for m in _PATH_RE.finditer(body)}


def check_prompt(body: str | None, project_id: str, root: Path) -> list[str]:
    """Every reason this prompt is not well-formed. Empty list = well-formed."""
    if body is None:
        return [f"no fenced block under `{PROMPT_HEADING}`"]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    problems: list[str] = []

    if len(lines) < MIN_PROMPT_LINES:
        problems.append(
            f"only {len(lines)} non-blank line(s); a standalone prompt needs at "
            f"least {MIN_PROMPT_LINES}. A slash command in a fence is not a handoff.")
    if len(lines) > MAX_PROMPT_LINES:
        problems.append(
            f"{len(lines)} non-blank lines; the cap is {MAX_PROMPT_LINES}. Point "
            f"at files instead of restating them.")

    if project_id not in body:
        problems.append(f"does not name the project id {project_id!r}")

    if not re.search(r"\b(phase|wave)\b", body, re.IGNORECASE):
        problems.append("does not name a phase or wave")

    candidates = path_candidates(body)
    # A leading `/` may also be the tail of a placeholder (`<wt>/my-projects/..`),
    # so a candidate is tried as written and relative to root without it.
    resolvable = [c for c in candidates
                  if Path(c).exists() or (root / c.lstrip("/")).exists()]
    if not resolvable:
        problems.append(
            "names no path that resolves on disk — a cold session cannot tell a "
            "typo from a moved file")
    readables = [c for c in resolvable
                 if (root / c.lstrip("/")).is_file() or Path(c).is_file()]
    if len(readables) < 2:
        problems.append(
            f"names {len(readables)} readable file(s) to start from; a cold "
            f"session needs at least 2")

    if not _STOP_RE.search(body):
        problems.append("states no stopping condition")

    return problems


# ---------- section rewriting -----------------------------------------------


def replace_section(text: str, heading: str, new_body: str) -> str:
    """Overwrite one `## ` section, or append it when absent."""
    block = f"{heading}\n\n{new_body.rstrip()}\n"
    bounds = section_bounds(text, heading)
    if bounds is None:
        return text.rstrip() + "\n\n" + block
    start, end = bounds
    tail = text[end:]
    return text[:start] + block + ("\n" + tail if tail.strip() else "")


def set_frontmatter(text: str, updates: dict[str, str]) -> str:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return text
    block = match.group(1)
    for key, value in updates.items():
        if value is None:
            continue
        line = f"{key}: {value}"
        pattern = re.compile(rf"^{re.escape(key)}:.*$", re.MULTILINE)
        block = pattern.sub(line, block) if pattern.search(block) else block + "\n" + line
    return text[: match.start(1)] + block + text[match.end(1):]


def build_resume(state: str, nxt: str, decisions: str) -> str:
    def wrap(label: str, value: str) -> str:
        pad = " " * (11 - len(label) - 1)
        return f"{label}:{pad}{value.strip()}"
    return "\n".join([
        wrap("state", state), wrap("next", nxt), wrap("decisions", decisions),
    ])


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="close-phase.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project_id")
    p.add_argument("--wave", type=int, default=None, metavar="N",
                   help="the wave number now current; writes `current_wave`, "
                        "the key the project-workflow skill bumps at each wave "
                        "boundary")
    p.add_argument("--state", default=None, help="`## Resume` state: line")
    p.add_argument("--next", dest="next_", default=None, help="`## Resume` next: line")
    p.add_argument("--decisions", default=None, help="`## Resume` decisions: line")
    p.add_argument("--prompt-file", type=Path, default=None,
                   help="file holding the next session's prompt: raw, or inside "
                        "one enclosing ``` fence (stripped); an inner fence refuses")
    p.add_argument("--deadline", dest="deadlines", action="append", default=None,
                   metavar="'YYYY-MM-DD <what>'",
                   help="set `deadlines:` frontmatter; repeatable. Omitted = "
                        "the existing value is preserved")
    p.add_argument("--no-deadlines", action="store_true",
                   help="set `deadlines: none`")
    p.add_argument("--status", type=Path, default=None,
                   help="the STATUS.md to write, bypassing the worktree lookup")
    p.add_argument("--check-only", action="store_true",
                   help="validate the existing handoff block and exit; write nothing")
    args = p.parse_args(argv[1:])

    if args.deadlines and args.no_deadlines:
        print("refuse: --deadline and --no-deadlines are mutually exclusive",
              file=sys.stderr)
        return 1
    try:
        deadlines = format_deadlines(args.deadlines, args.no_deadlines)
    except ValueError as exc:
        print(f"refuse: {exc}", file=sys.stderr)
        return 1

    try:
        main_repo = main_checkout()
    except RepoError as exc:
        print(f"REFUSE: {exc}. Run from inside the repo.", file=sys.stderr)
        return 2
    if args.status:
        status_path = args.status.resolve()
        root = repo_root_of(status_path.parent) or main_repo
    else:
        try:
            status_path, root = find_status(args.project_id, main_repo)
        except WorktreeLookupError as exc:
            print(f"REFUSE: cannot resolve the tree for project "
                  f"{args.project_id} -- {exc} Pass --status <STATUS.md> "
                  f"explicitly.", file=sys.stderr)
            return 2
        except ConfigError as exc:
            print(f"REFUSE: {exc}", file=sys.stderr)
            return 2
    if status_path is None or not status_path.is_file():
        print(f"refuse: no STATUS.md for project {args.project_id} under {root}",
              file=sys.stderr)
        return 1

    text = status_path.read_text(encoding="utf-8")
    print(f"close-phase: project {args.project_id}")
    print(f"  STATUS.md: {status_path}")
    if not same_path(root, main_repo):
        print("  (worktree copy — main's is the frozen stub)")

    # ---- the gate: a phase does not close without a well-formed prompt ----
    if args.prompt_file is not None:
        if not args.prompt_file.is_file():
            print(f"refuse: --prompt-file {args.prompt_file} does not exist",
                  file=sys.stderr)
            return 1
        body, refusal = unfence_prompt(args.prompt_file.read_text(encoding="utf-8"))
        if refusal:
            print(f"refuse: {refusal}", file=sys.stderr)
            return 1
    else:
        body = extract_prompt(text)
        if body is not None:
            print("  no --prompt-file; validating the block already in STATUS.md")

    problems = check_prompt(body, args.project_id, root)
    if problems:
        print(f"\nREFUSE: the phase cannot close — the `{PROMPT_HEADING}` block "
              f"is missing or malformed ({len(problems)} problem(s)):",
              file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("\nWrite the prompt and pass it with --prompt-file. It must stand "
              "alone for a session with zero context.", file=sys.stderr)
        return 1
    print(f"  handoff prompt: well-formed "
          f"({len([l for l in body.splitlines() if l.strip()])} non-blank lines)")

    if args.check_only:
        resume_problems = check_resume(resume_lines(text))
        if resume_problems:
            print(f"\nREFUSE: {resume_problems[0]}", file=sys.stderr)
            return 1
        print("\nPASS (--check-only): a well-formed handoff prompt is present. "
              "Nothing written.")
        return 0

    if not (args.state and args.next_):
        print("refuse: --state and --next are required to rewrite `## Resume`",
              file=sys.stderr)
        return 1

    resume = build_resume(args.state, args.next_, args.decisions or "none")
    resume_problems = check_resume([ln for ln in resume.splitlines() if ln.strip()])
    if resume_problems:
        print(f"refuse: {resume_problems[0]}", file=sys.stderr)
        return 1

    text = set_frontmatter(text, {
        "current_wave": None if args.wave is None else str(args.wave),
        "deadlines": deadlines,
        "last_updated": date.today().isoformat(),
    })
    text = replace_section(text, RESUME_HEADING, resume)
    text = replace_section(
        text, PROMPT_HEADING,
        "Overwritten at every phase boundary. Paste this whole block after "
        "`/clear`.\n\n```\n" + body.strip() + "\n```")

    status_path.write_text(text, encoding="utf-8")
    print(f"\nwrote {status_path}")
    if args.wave is not None:
        print(f"  current_wave = {args.wave}")
    else:
        print("  current_wave left as found")
    print(f"  `{RESUME_HEADING}` and `{PROMPT_HEADING}` overwritten")
    print("\nEcho the prompt block in your closing message — the operator "
          "clears the session and pastes it cold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
