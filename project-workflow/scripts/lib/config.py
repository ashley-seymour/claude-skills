"""Repo discovery and the per-repo workflow config.

The scripts live beside the global skill, not inside the repo they act on, so
"which repo" is always answered from the caller's current directory, never
from this file's location:

  * ``repo_toplevel(cwd)``: ``git rev-parse --show-toplevel``, the checkout
    (main or linked worktree) the caller stands in;
  * ``main_checkout(cwd)``: the first ``worktree`` entry of
    ``git worktree list --porcelain``, which git always lists first;
  * ``git_common_dir(cwd)``: the one ``.git`` directory every worktree shares,
    where per-repo script state (the close cursor) lives.

``load_config(tree)`` reads ``<tree>/.claude/project-workflow.json``. The file
is optional; every key has a default, so a repo with no file works. Keys, per
the skill's config contract:

  projects_dir   folder holding ``current/``, ``completed/``, ``deferred/``
  base_branch    branch projects are cut from and merged into
  profile        the repo's workflow profile (prose the skill reads)
  index_file     the projects index the scripts add and update rows in
  backlog_file   the backlog the close checks and appends to
  suites         list of ``{"name": str, "argv": [str, ...]}``, each a pytest
                 command run from the tree under test; empty means the suite
                 gate prints "no suites configured" and passes
  suite_pin      repo-relative path of the suite floor, or null
  bootstrap      argv run after a worktree is cut, or null
  teardown       argv run before a worktree is removed, or null
  close          ``{"merge": "local" | "squash", "ci_workflow": str | null}``

Any other key, or a value of the wrong type, is a ``ConfigError``: a typo in
the file must not silently fall back to a default.

Read the config from the project's worktree when the command names a project
that has one, else from the main checkout: ``config_for(main, tree)``, or
``config_at(tree)`` when the caller holds only the tree. ``load_config`` reads
one tree with no fallback; call it directly only on the main checkout.
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_REL = Path(".claude") / "project-workflow.json"

# The literal an argv element may carry; replaced by the worktree's absolute path.
WORKTREE_TOKEN = "{worktree}"

MERGE_MODES = ("local", "squash")


class ConfigError(ValueError):
    """The config file exists but is not valid per the contract."""


class RepoError(RuntimeError):
    """The current directory is not inside a git repository."""


@dataclass(frozen=True)
class Suite:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    projects_dir: str = "my-projects"
    base_branch: str = "main"
    profile: str = ""
    index_file: str = ""
    backlog_file: str = ""
    suites: tuple[Suite, ...] = ()
    suite_pin: str | None = None
    bootstrap: tuple[str, ...] | None = None
    teardown: tuple[str, ...] | None = None
    merge: str = "local"
    ci_workflow: str | None = None
    # Where the values came from: the file read, or None for all-defaults.
    source: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        # The three path defaults derive from projects_dir.
        for key, leaf in (("profile", "WORKFLOW.md"), ("index_file", "README.md"),
                          ("backlog_file", "backlog.md")):
            if not getattr(self, key):
                object.__setattr__(self, key, f"{self.projects_dir}/{leaf}")


# ---------- git discovery ----------------------------------------------------

def _git(cwd: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def repo_toplevel(cwd: Path | None = None) -> Path:
    """The checkout (main or linked worktree) containing ``cwd``."""
    here = Path(cwd) if cwd is not None else Path.cwd()
    out = _git(here, "rev-parse", "--show-toplevel")
    if not out or not out.strip():
        raise RepoError(f"not inside a git repository: {here}")
    return Path(out.strip())


def main_checkout(cwd: Path | None = None) -> Path:
    """The main checkout of the repo containing ``cwd``.

    The first ``worktree`` line of ``git worktree list --porcelain``: git lists
    the main working tree first, from any of its worktrees.
    """
    here = Path(cwd) if cwd is not None else Path.cwd()
    out = _git(here, "worktree", "list", "--porcelain")
    if out:
        for line in out.splitlines():
            if line.startswith("worktree "):
                return Path(line[len("worktree "):].strip())
    raise RepoError(f"not inside a git repository: {here}")


def git_common_dir(cwd: Path | None = None) -> Path:
    """The ``.git`` directory shared by every worktree of the repo."""
    here = Path(cwd) if cwd is not None else Path.cwd()
    out = _git(here, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not out or not out.strip():
        raise RepoError(f"not inside a git repository: {here}")
    return Path(out.strip())


# ---------- the config file --------------------------------------------------

_STR_KEYS = ("projects_dir", "base_branch", "profile", "index_file", "backlog_file")
_KNOWN = frozenset(_STR_KEYS + ("suites", "suite_pin", "bootstrap", "teardown", "close"))


def _argv(value: Any, where: str) -> tuple[str, ...]:
    if (not isinstance(value, list) or not value
            or not all(isinstance(a, str) and a for a in value)):
        raise ConfigError(f"{where} must be a non-empty list of non-empty strings, "
                          f"got {value!r}")
    return tuple(value)


def parse_config(data: Any, source: Path | None = None) -> Config:
    """Validate a decoded config object and fill the defaults."""
    where = str(source) if source is not None else "config"
    if not isinstance(data, dict):
        raise ConfigError(f"{where}: top level must be a JSON object")
    unknown = sorted(set(data) - _KNOWN)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {unknown}; "
                          f"known: {sorted(_KNOWN)}")
    kw: dict[str, Any] = {"source": source}
    for key in _STR_KEYS:
        if key in data:
            if not isinstance(data[key], str) or not data[key].strip():
                raise ConfigError(f"{where}: {key} must be a non-empty string")
            kw[key] = data[key].strip().rstrip("/")
    if "suites" in data:
        if not isinstance(data["suites"], list):
            raise ConfigError(f"{where}: suites must be a list")
        suites = []
        for i, s in enumerate(data["suites"]):
            if (not isinstance(s, dict) or set(s) != {"name", "argv"}
                    or not isinstance(s["name"], str) or not s["name"].strip()):
                raise ConfigError(f"{where}: suites[{i}] must be "
                                  '{"name": str, "argv": [str, ...]}')
            suites.append(Suite(s["name"].strip(), _argv(s["argv"], f"{where}: suites[{i}].argv")))
        names = [s.name for s in suites]
        if len(set(names)) != len(names):
            raise ConfigError(f"{where}: suite names must be unique, got {names}")
        kw["suites"] = tuple(suites)
    if data.get("suite_pin") is not None:
        if not isinstance(data["suite_pin"], str) or not data["suite_pin"].strip():
            raise ConfigError(f"{where}: suite_pin must be a repo-relative path or null")
        kw["suite_pin"] = data["suite_pin"].strip()
    for key in ("bootstrap", "teardown"):
        if data.get(key) is not None:
            kw[key] = _argv(data[key], f"{where}: {key}")
    if "close" in data:
        close = data["close"]
        if not isinstance(close, dict) or set(close) - {"merge", "ci_workflow"}:
            raise ConfigError(f"{where}: close must be an object with keys "
                              "merge and ci_workflow only")
        merge = close.get("merge", "local")
        if merge not in MERGE_MODES:
            raise ConfigError(f"{where}: close.merge must be one of {list(MERGE_MODES)}, "
                              f"got {merge!r}")
        kw["merge"] = merge
        ci = close.get("ci_workflow")
        if ci is not None and (not isinstance(ci, str) or not ci.strip()):
            raise ConfigError(f"{where}: close.ci_workflow must be a string or null")
        kw["ci_workflow"] = ci.strip() if ci else None
    return Config(**kw)


def load_config(tree: Path) -> Config:
    """The config in ``<tree>/.claude/project-workflow.json``, or the defaults."""
    path = Path(tree) / CONFIG_REL
    if not path.is_file():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: not readable JSON: {exc}") from exc
    return parse_config(data, path)


def config_for(main: Path, project_tree: Path | None = None) -> Config:
    """The config a command uses: the project's worktree copy if there is one."""
    if project_tree is not None and (Path(project_tree) / CONFIG_REL).is_file():
        return load_config(project_tree)
    return load_config(main)


def config_at(tree: Path) -> Config:
    """``config_for`` when only the tree is known: its main checkout is found by git.

    ``tree`` may be a project worktree or the main checkout itself. Its own
    file wins; otherwise the main checkout's is read. Outside any repository
    there is no main checkout to fall back to, so only ``tree`` is read.
    """
    tree = Path(tree)
    if (tree / CONFIG_REL).is_file():
        return load_config(tree)
    try:
        main = main_checkout(tree)
    except RepoError:
        return load_config(tree)
    return config_for(main, tree)


def expand_argv(argv: tuple[str, ...] | list[str], worktree: Path) -> list[str]:
    """``argv`` with every ``{worktree}`` replaced by the worktree's absolute path.

    A leading ``python`` becomes this interpreter (``sys.executable``), so the
    hook runs under the same Python as the script that calls it rather than
    whichever ``python`` the PATH finds first.
    """
    wt = str(Path(worktree).resolve())
    return with_interpreter([a.replace(WORKTREE_TOKEN, wt) for a in argv])


def with_interpreter(argv: tuple[str, ...] | list[str]) -> list[str]:
    """``argv`` with a leading ``python`` replaced by ``sys.executable``."""
    out = list(argv)
    if out and out[0] == "python":
        out[0] = sys.executable
    return out
