"""Shared project-path helpers: the project map, STATUS.md frontmatter and worktree lookup."""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

# ---------- paths ----------------------------------------------------------

from config import RepoError, config_at, main_checkout


def _resolve_cv_root(cwd: Path | None = None) -> Path:
    """The MAIN checkout of the repo the caller stands in -- never a worktree's.

    Discovered from the current directory (``config.main_checkout``: the first
    entry of ``git worktree list --porcelain``), never from this file's
    location: the scripts live beside the global skill, outside every repo
    they act on. Run from inside a linked worktree, this still names the main
    checkout, so main-repo steps (git-pull-main, prune-scaffold) never
    retarget at a worktree ``git worktree remove`` is about to delete.

    Outside any git repository it falls back to the current directory, so
    importing this module never raises; a command then refuses on the first
    git call it makes against it.
    """
    here = Path(cwd) if cwd is not None else Path.cwd()
    try:
        return main_checkout(here)
    except RepoError:
        return here.resolve()


CV_ROOT = _resolve_cv_root()                        # main checkout, from cwd


def repo_basename() -> str:
    """Basename of the **main** checkout (not the worktree), for worktree names."""
    return CV_ROOT.name


def projects_rel(root: Path | None = None) -> Path:
    """The config's ``projects_dir`` for the tree at ``root`` (default CV_ROOT).

    ``root``'s own config file wins, else its main checkout's (``config_at``).
    """
    return Path(config_at(root if root is not None else CV_ROOT).projects_dir)


PROJECT_STATES = ("current", "completed", "deferred")
_PROJECT_DIR_RE = re.compile(r"^project-(\d{4})-(.+)$")


def discover_projects(root: Path | None = None) -> dict[str, Path]:
    """Project id → project root, by globbing `<projects_dir>/<state>/project-<NNNN>-*`.

    The only project map. Searching the state directories, rather than a map
    pinned to the `current/` segment, survives close's `git mv` of the folder
    to `completed/` by construction. `projects_dir` is the config's for the
    tree at `root`.

    `current` is scanned first so a half-finished move (the folder present under
    two states at once) resolves to the live copy rather than the archive.
    """
    base = root if root is not None else CV_ROOT
    rel = projects_rel(base)
    out: dict[str, Path] = {}
    for state in PROJECT_STATES:
        state_dir = base / rel / state
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.iterdir()):
            if not path.is_dir():
                continue
            m = _PROJECT_DIR_RE.match(path.name)
            if m and m.group(1) not in out:
                out[m.group(1)] = path.resolve()
    return out


def load_projects(root: Path | None = None) -> dict[str, Path]:
    """Project id → project root. `root` overrides CV_ROOT (e.g. a worktree).

    The map is :func:`discover_projects`; no overlay file is read. Raises
    SystemExit when no project folder exists, so an empty tree is never a
    silent success.
    """
    base = root if root is not None else CV_ROOT
    discovered = discover_projects(base)
    if discovered:
        return discovered
    raise SystemExit(
        f"no project folders under {(base / projects_rel(base)).as_posix()} -- "
        f"scaffold one with the skill's `scripts/bin/new-project.py`."
    )


def project_streams_dir(
    project_id: str,
    root: Path | None = None,
    allow_empty: bool = False,
) -> Path:
    """Streams dir for a project. `root` overrides CV_ROOT (e.g. a worktree).

    allow_empty=True: return the Path even when streams/ is absent; else raise.
    """
    proj_root = load_projects(root).get(project_id)
    if proj_root is None:
        raise SystemExit(f"unknown project id {project_id!r} -- no "
                         f"{projects_rel(root).as_posix()}/<state>/"
                         f"project-{project_id}-<slug>/ folder")
    streams = proj_root / "streams"
    if not streams.is_dir() and not allow_empty:
        raise SystemExit(f"no streams dir at {streams}")
    return streams


# ---------- STATUS.md ------------------------------------------------------

def utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_md_path(project_id: str, root: Path | None = None) -> Path:
    """Path to a project's STATUS.md.

    allow_empty: STATUS.md sits beside streams/, not inside it, so a
    stream-less project still has one. Hard-raising here blocked the close of
    any phase-based project.
    """
    return project_streams_dir(project_id, root, allow_empty=True).parent / "STATUS.md"


_STATUS_FM_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", re.DOTALL)


def read_status_frontmatter(project_id: str, root: Path | None = None) -> dict[str, Any]:
    """Return STATUS.md frontmatter as a dict (empty dict if none)."""
    path = status_md_path(project_id, root)
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    m = _STATUS_FM_RE.match(text)
    if not m:
        return {}
    return yaml.safe_load(m.group(1)) or {}


# ---------- worktree resolution --------------------------------------------

# Stream-id grammar (this module's own; the retired orchestrated-session v2 brief
# frontmatter defined it, and no current skill file restates it):
# <PREFIX>-<1-2 alnum>, plus an optional lowercase follow-on suffix (`S-05` -> `S-05a`). Without the suffix a
# follow-on worktree classifies as project-kind, which makes
# `find_project_worktree` ambiguous and silently redirects dispatch to the main repo.
STREAM_ID_RE = re.compile(r"^[SHRGXD]-[A-Z0-9]{1,2}[a-z]?$")

# Anchor for the project-id-plus-optional-tail portion of a worktree name.
_PROJECT_TAIL_RE = re.compile(r"^(\d{4})(?:-(.+))?$")


def worktree_name(repo: str, project_id: str, tail: str | None) -> str:
    """The ONLY composer for worktree directory names.

    Shape: ``<repo>-<NNNN>[-<tail>]`` where tail is a stream id (`S-01`) or a
    project slug (`token-efficiency-audit`). Callers MUST route through this
    rather than string-formatting locally, so every name classifies the same
    way in :func:`parse_worktree_name`.
    """
    base = f"{repo}-{project_id}"
    return f"{base}-{tail}" if tail else base


def worktrees_dir(repo: str, main_repo: Path | None = None) -> Path:
    """The one folder holding every worktree of this repo: ``<parent>/<repo>-worktrees``."""
    return (main_repo or CV_ROOT).parent / f"{repo}-worktrees"


def worktree_path(repo: str, project_id: str, tail: str | None,
                  main_repo: Path | None = None) -> Path:
    """Where a new worktree is cut: ``<repo>-worktrees/<NNNN>[-<tail>]``.

    The leaf drops the ``<repo>-`` prefix of :func:`worktree_name`; the folder
    carries it. :func:`worktree_key` maps the path back for classification.
    """
    leaf = worktree_name(repo, project_id, tail)[len(repo) + 1:]
    return worktrees_dir(repo, main_repo) / leaf


def worktree_key(path: Path, repo: str) -> str:
    """The name to classify ``path`` by, under either layout.

    ``<repo>-worktrees/<NNNN>[-<tail>]`` classifies as ``<repo>-<NNNN>[-<tail>]``;
    a legacy sibling ``<repo>-<NNNN>[-<tail>]`` keeps its own name.

    The ``<repo>`` part is matched case-insensitively and returned in
    ``repo``'s own case: Windows paths are case-insensitive, so
    ``My-Repo-Worktrees/0007-x`` is the same folder as
    ``my-repo-worktrees/0007-x`` and must classify the same.
    The tail keeps its case -- stream ids (`S-01`) are upper-case by grammar.
    """
    if path.parent.name.lower() == f"{repo}-worktrees".lower():
        return f"{repo}-{path.name}"
    prefix = f"{repo}-"
    if path.name.lower().startswith(prefix.lower()):
        return prefix + path.name[len(prefix):]
    return path.name


def parse_worktree_name(name: str, repo: str) -> tuple[str, str | None, str | None]:
    """Classify a worktree directory name into (kind, project_id, tail).

    kind is one of {"project", "stream", "other"}. A tail matching
    STREAM_ID_RE returns kind="stream"; any other tail (or no tail at all)
    returns kind="project" -- covers bare `<repo>-NNNN`, slugged
    `<repo>-NNNN-<slug>`, and legacy per-phase names like `<repo>-NNNN-a`.
    Names that do not start with ``<repo>-`` or whose remainder does not match
    ``^\\d{4}(?:-.+)?$`` return ("other", None, None).
    """
    prefix = f"{repo}-"
    if not name.startswith(prefix):
        return ("other", None, None)
    remainder = name[len(prefix):]
    m = _PROJECT_TAIL_RE.match(remainder)
    if not m:
        return ("other", None, None)
    project_id = m.group(1)
    tail = m.group(2)
    if tail and STREAM_ID_RE.match(tail):
        return ("stream", project_id, tail)
    return ("project", project_id, tail)


def _iter_worktree_paths(main_repo: Path) -> list[Path] | None:
    """Return the list of worktree paths from ``git worktree list --porcelain``.

    Returns None if git is unavailable or the command fails -- callers fall
    back to their None-return semantics.
    """
    try:
        r = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(main_repo), capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    paths: list[Path] = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(Path(line[len("worktree "):]))
    return paths


class AmbiguousWorktreeError(RuntimeError):
    """Two or more worktrees match one project id.

    Distinct from "no worktree", which is legitimate (a sequential wave runs in
    the main repo) and still returns None. Callers shaped
    ``find_project_worktree(...) or CV_ROOT`` collapsed both cases into a
    main-repo fallback, silently dispatching against whatever branch happened to
    be checked out there.
    """


def find_project_worktree(
    project_id: str,
    main_repo: Path | None = None,
    slug: str | None = None,
) -> Path | None:
    """Return the project worktree path for ``project_id`` or None.

    Classifies every worktree via :func:`parse_worktree_name`. Resolution:

    1. If exactly one ``project``-kind entry matches ``project_id`` AND its
       tail equals ``slug`` (when slug provided) -> return it.
    2. Else if exactly one ``project``-kind entry matches ``project_id``
       (any tail, including None) -> return it.
    3. 0 candidates -> None (silent; the caller may fall back to the main repo).
    4. 2+ candidates -> raise :class:`AmbiguousWorktreeError` (close verbs must
       not guess across legacy per-phase siblings).

    ``slug`` is optional to preserve callers that only know the id.
    """
    main_repo = main_repo or CV_ROOT
    paths = _iter_worktree_paths(main_repo)
    if paths is None:
        return None
    repo = repo_basename()
    candidates: list[Path] = []
    for p in paths:
        kind, pid, _tail = parse_worktree_name(worktree_key(p, repo), repo)
        if kind == "project" and pid == project_id:
            candidates.append(p)
    if not candidates:
        return None
    if slug is not None:
        exact = [
            p for p in candidates
            if parse_worktree_name(worktree_key(p, repo), repo)[2] == slug
        ]
        if len(exact) == 1:
            return exact[0]
    if len(candidates) == 1:
        return candidates[0]
    names = ", ".join(sorted(p.name for p in candidates))
    raise AmbiguousWorktreeError(
        f"find_project_worktree: {len(candidates)} candidates for "
        f"{project_id!r}: {names}. Remove the stray worktree, or pass an "
        f"explicit --root."
    )


# ---------- project-tree lookup for the gate verbs ---------------------------
#
# One lookup shared by `wave-close.py` and `close-phase.py`. Both used to match
# `f"-{project_id}" in p.name`, which fits only the legacy sibling layout and
# silently fell back to the main checkout under `<repo>-worktrees/<NNNN>-<slug>`.
# This resolver refuses instead of guessing; each
# caller appends its own "pass --root / --status explicitly" remedy.

class WorktreeLookupError(RuntimeError):
    """The project's tree exists (or may exist) but cannot be pinned down.

    Distinct from "no worktree" (None). The old name heuristic collapsed both
    into a silent main fallback, so a gate verified -- or a handoff was written
    into -- `main`'s frozen stub instead of the project's tree.
    """


def parse_porcelain(text: str) -> list[tuple[Path, str | None]]:
    """(path, branch ref or None) per entry of `git worktree list --porcelain`."""
    entries: list[tuple[Path, str | None]] = []
    for line in text.splitlines():
        if line.startswith("worktree "):
            entries.append((Path(line[len("worktree "):].strip()), None))
        elif line.startswith("branch ") and entries:
            entries[-1] = (entries[-1][0], line[len("branch "):].strip())
    return entries


def same_path(a: Path, b: Path) -> bool:
    """Path equality ignoring separator style, a trailing slash and case."""
    return str(a).replace("\\", "/").rstrip("/").lower() == \
        str(b).replace("\\", "/").rstrip("/").lower()


def resolve_project_tree(project_id: str, main: Path,
                         entries: list[tuple[Path, str | None]],
                         repo: str) -> Path | None:
    """The tree holding project ``project_id``, or None when it has none.

    Returns ``main`` itself when the main checkout is on the project branch and
    no other worktree claims the project. Names classify through
    :func:`worktree_key` + :func:`parse_worktree_name`, so both layouts match
    and the id is anchored (`0007` never matches `10007`, `0007x` or `00070`).
    A worktree holding ``refs/heads/project/<NNNN>[-*]`` is also bound to the
    project. Stream-kind worktrees (`0007-S-01`) never match.

    Raises :class:`WorktreeLookupError` when a project-branch worktree does not
    classify, when more than one tree matches (the main checkout on the project
    branch counts as one), or when the single match is missing on disk.
    """
    branch_re = re.compile(rf"^refs/heads/project/{re.escape(project_id)}(?:-|$)")
    main_on_branch: str | None = None
    by_name: list[Path] = []
    by_branch: list[Path] = []
    for path, branch in entries:
        on_branch = bool(branch and branch_re.match(branch))
        if same_path(path, main):
            # Recorded, not returned: an early return here skipped the
            # ambiguity check below.
            if on_branch:
                main_on_branch = branch
            continue
        kind, pid, _tail = parse_worktree_name(worktree_key(path, repo), repo)
        named = kind == "project" and pid == project_id
        if named:
            by_name.append(path)
        if on_branch:
            by_branch.append(path)
            if not named:
                raise WorktreeLookupError(
                    f"branch {branch.removeprefix('refs/heads/')} is checked out "
                    f"at {path}, whose name does not classify as project "
                    f"{project_id} under either worktree layout.")
    matches: list[Path] = []
    for p in by_name + by_branch:
        if not any(same_path(p, q) for q in matches):
            matches.append(p)
    if main_on_branch is not None:
        if matches:
            raise WorktreeLookupError(
                f"the main checkout {main} is on "
                f"{main_on_branch.removeprefix('refs/heads/')}, and "
                f"{len(matches)} other worktree(s) also match project "
                f"{project_id}: " + ", ".join(str(p) for p in matches)
                + ". Remove the stray one.")
        return main
    if len(matches) > 1:
        raise WorktreeLookupError(
            f"{len(matches)} worktrees match project {project_id}: "
            + ", ".join(str(p) for p in matches) + ". Remove the stray one.")
    if not matches:
        return None
    if not matches[0].is_dir():
        raise WorktreeLookupError(
            f"worktree {matches[0]} for project {project_id} is registered but "
            f"missing on disk (`git worktree prune`?).")
    return matches[0]


def worktree_porcelain(main: Path) -> str:
    """Raw `git worktree list --porcelain` for the repo at ``main``."""
    return subprocess.run(
        ["git", "-C", str(main), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout


def find_project_tree(project_id: str, main: Path) -> Path | None:
    """:func:`resolve_project_tree` over the live `git worktree list`.

    A git failure raises rather than returning None, since "no worktree" could
    then not be told apart from "unknown".
    """
    try:
        out = worktree_porcelain(main)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        raise WorktreeLookupError(
            f"`git worktree list` failed in {main} ({exc}); cannot tell whether "
            f"project {project_id} has a worktree.") from exc
    return resolve_project_tree(project_id, main, parse_porcelain(out),
                                repo_basename())
