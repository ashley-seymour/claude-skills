"""Refuse a close that would corrupt the backlog's F-id space.

Concurrent projects can allocate the same next F-id on separate branches,
and git merges the two tables without a conflict. This pass reads the
config's `backlog_file` in the project worktree, in `ctx.cv_root`, at the
branch point (`git merge-base <base_branch> HEAD` in the worktree), and every
backlog row in `git log -p --all` over a glob covering current and retired
backlog files (a landed row and a sibling's unmerged allocation both live
there). It reports:

  * an id appearing twice in one tree's backlog file;
  * an id present in both trees under a DIFFERENT `Idea`;
  * an id new on the branch (absent at the branch point) that history shows
    under another `Idea`, unless already reported as differing from main.

Session mode (`worktree: none`) skips the history check (no branch point
separates the project's rows from inherited ones); the ok message says so.
`Idea`, not `Sketch`, is compared (landing a row appends to its Sketch);
history ideas compare case-, punctuation- and whitespace-insensitively.
History or a branch point git cannot produce is a `fail`: `blocking=True`.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from close_passes import ClosePass, PassResult

# The defaults when a ctx carries no config (`ctx.cfg`); `_backlog_rel` and
# `history_pathspec` derive both from the config's `backlog_file`.
_BACKLOG_REL = Path("my-projects") / "backlog.md"

# Every backlog file, current or retired, by pattern rather than by name.
HISTORY_PATHSPEC = ":(glob)my-projects/backlog*"


def _backlog_rel(ctx: Any) -> Path:
    cfg = getattr(ctx, "cfg", None)
    return Path(cfg.backlog_file) if cfg is not None else _BACKLOG_REL


def _base_branch(ctx: Any) -> str:
    cfg = getattr(ctx, "cfg", None)
    return cfg.base_branch if cfg is not None else "main"


def history_pathspec(backlog_rel: Path) -> str:
    """The glob pathspec covering `backlog_rel` and its retired siblings."""
    rel = Path(backlog_rel)
    parent = rel.parent.as_posix()
    prefix = "" if parent == "." else f"{parent}/"
    return f":(glob){prefix}{rel.stem}*"

# A backlog row's first cell, with or without the bold markers the tables use:
# `| **F13** | Rename ... |`. Anchored to the row start so an F-id merely
# *cited* in Sketch prose is not mistaken for an allocation.
_ROW_ID_RE = re.compile(r"^\|\s*\*{0,2}(F\d+)\*{0,2}\s*\|")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _normalise(idea: str) -> str:
    """Case-, punctuation- and whitespace-insensitive form of an Idea cell."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", idea.lower()).split())


def _parse_ids(root: Path, rel: Path = _BACKLOG_REL) -> dict[str, list[tuple[str, str]]]:
    """id -> [(where, idea_cell)] from the backlog file `rel` under `root`.

    A missing file is not an error: a consumer repo need not carry a backlog.
    """
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_text(text)


def _parse_text(text: str) -> dict[str, list[tuple[str, str]]]:
    """id -> [(where, idea_cell)] from the text of one backlog.md."""
    found: dict[str, list[tuple[str, str]]] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _ROW_ID_RE.match(line.strip())
        if not m:
            continue
        cells = _cells(line)
        idea = cells[1] if len(cells) > 1 else ""
        found.setdefault(m.group(1), []).append((f"backlog.md:{lineno}", idea))
    return found


def history_ids(repo: Path, pathspec: str = HISTORY_PATHSPEC
                ) -> tuple[dict[str, set[str]] | None, str]:
    """(id -> normalised Ideas, error) from every backlog row in git history.

    Reads added, removed and context lines of `git log -p --all` over
    `pathspec`, so a row deleted long ago still counts. Returns
    `(None, reason)` when git cannot answer.
    """
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "log", "-p", "--all", "--format=", "--",
             pathspec],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return None, f"git not runnable: {exc}"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:160] or f"git log exited {r.returncode}"
    ideas: dict[str, set[str]] = {}
    for line in (r.stdout or "").splitlines():
        if not line or line[0] not in "+- " or line.startswith(("+++", "---")):
            continue
        body = line[1:].strip()
        m = _ROW_ID_RE.match(body)
        if not m:
            continue
        cells = _cells(body)
        idea = _normalise(cells[1].replace("**", "")) if len(cells) > 1 else ""
        ideas.setdefault(m.group(1), set()).add(idea)
    return ideas, ""


def branch_point_ids(worktree: Path, base_branch: str = "main",
                     rel_path: Path = _BACKLOG_REL) -> tuple[set[str] | None, str]:
    """(F-ids in the backlog file at the branch point, error).

    The branch point is `git merge-base <base_branch> HEAD` run in the project
    worktree. A backlog.md absent at that commit gives an empty set. Returns
    `(None, reason)` when git cannot produce the merge-base or the file.
    """
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def why(r: subprocess.CompletedProcess[str], what: str) -> str:
        return (r.stderr or "").strip()[:160] or f"{what} exited {r.returncode}"

    try:
        r = git("merge-base", base_branch, "HEAD")
        if r.returncode != 0 or not r.stdout.strip():
            return None, why(r, "git merge-base")
        base = r.stdout.strip()
        rel = Path(rel_path).as_posix()
        r = git("ls-tree", "--name-only", base, "--", rel)
        if r.returncode != 0:
            return None, why(r, "git ls-tree")
        if not r.stdout.strip():
            return set(), ""
        r = git("show", f"{base}:{rel}")
        if r.returncode != 0:
            return None, why(r, "git show")
    except OSError as exc:
        return None, f"git not runnable: {exc}"
    return set(_parse_text(r.stdout)), ""


def _next_free_id(*id_sets) -> str:
    """One past the highest F-id in any id collection (never a gap), for the message."""
    used = {int(k[1:]) for ids in id_sets for k in ids if k[1:].isdigit()}
    return f"F{max(used, default=0) + 1:02d}"


def _describe(idea: str, cap: int = 60) -> str:
    idea = idea.replace("**", "").strip()
    return idea if len(idea) <= cap else idea[:cap - 1] + "…"


def _run(ctx: Any) -> PassResult:
    worktree = Path(ctx.project_worktree)
    rel = _backlog_rel(ctx)
    pathspec = history_pathspec(rel)
    base_branch = _base_branch(ctx)
    branch_ids = _parse_ids(worktree, rel)

    main_root = Path(ctx.cv_root)
    # A project closing from the main repo itself (no separate worktree) would
    # otherwise be diffed against its own files and report every row twice.
    same_tree = main_root.resolve() == worktree.resolve()
    main_ids = {} if same_tree else _parse_ids(main_root, rel)

    history, history_error = history_ids(main_root, pathspec)
    if history is None:
        return PassResult(
            status="fail",
            message=(f"could not read backlog history in {main_root} "
                     f"(`git log -p --all -- {pathspec}`): {history_error}"),
        )

    # Ids at the branch point are inherited; only the rest are this branch's
    # allocations. Session mode has no branch point (module docstring).
    base_ids: set[str] = set()
    if not same_tree:
        base, base_error = branch_point_ids(worktree, base_branch, rel)
        if base is None:
            return PassResult(
                status="fail",
                message=(f"could not read {rel.as_posix()} at the branch point in "
                         f"{worktree} (`git merge-base {base_branch} HEAD`): {base_error}"),
            )
        base_ids = base

    if not branch_ids and not main_ids:
        return PassResult(status="ok", message="no backlog F-ids found; nothing to check")

    collisions: list[str] = []

    for fid, rows in sorted(branch_ids.items()):
        if len(rows) > 1:
            where = ", ".join(w for w, _ in rows)
            collisions.append(f"{fid} appears {len(rows)}x in the closing worktree ({where})")

    for fid, rows in sorted(main_ids.items()):
        if len(rows) > 1:
            where = ", ".join(w for w, _ in rows)
            collisions.append(f"{fid} appears {len(rows)}x on main ({where})")

    differs_from_main: set[str] = set()
    for fid in sorted(set(branch_ids) & set(main_ids)):
        branch_ideas = {idea for _, idea in branch_ids[fid]}
        main_ideas = {idea for _, idea in main_ids[fid]}
        if branch_ideas & main_ideas:
            continue  # same row on both sides -- the branch inherited it
        differs_from_main.add(fid)
        collisions.append(
            f"{fid} is allocated to different findings on main and on this "
            f"branch (main: {_describe(next(iter(main_ideas)))!r}; "
            f"branch: {_describe(next(iter(branch_ideas)))!r})"
        )

    if not same_tree:
        # An id already reported against main is not reported twice: main's
        # row is in history too, so the history check would repeat it.
        for fid in sorted(set(branch_ids) - base_ids - differs_from_main):
            own = {_normalise(idea.replace("**", "")) for _, idea in branch_ids[fid]}
            other = sorted(history.get(fid, set()) - own)
            if other:
                collisions.append(
                    f"{fid} is new on this branch, but git history shows it "
                    f"for another finding ({_describe(other[0])!r})"
                )

    total = len(set(branch_ids) | set(main_ids))
    free = _next_free_id(branch_ids, main_ids, history)
    if collisions:
        return PassResult(
            status="fail",
            message=(
                f"{len(collisions)} backlog F-id collision(s) across "
                f"{total} id(s) -- renumber this branch's rows before merging "
                f"(next free: {free}): "
                + "; ".join(collisions)
            ),
        )
    if same_tree:
        return PassResult(
            status="ok",
            message=(
                f"{total} backlog F-id(s) unique in backlog.md; history check "
                f"skipped (session mode: no branch point); next free: {free}"
            ),
        )
    return PassResult(
        status="ok",
        message=(
            f"{total} backlog F-id(s) unique across main, the closing worktree "
            f"and backlog history; next free: {free}"
        ),
    )


PASS = ClosePass(
    name="backlog-id-collisions",
    description=(
        "Refuse the merge when a backlog F-id is allocated twice, means "
        "something different on main than on the closing branch, or reuses "
        "an id git history shows for another finding"
    ),
    blocking=True,
    run=_run,
)
