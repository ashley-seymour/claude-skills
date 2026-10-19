"""Close-step registry for close-project.py; no side effects at import time.

Each step's precheck(ctx) -> str | None resolves every precondition; its
apply(ctx) mutates and MUST NOT re-derive them. Repo-specific values come from
`CloseCtx.cfg` (lib/config.py). `close.merge` picks the merge path (`squash`:
push and squash-merge a PR; `local`: a merge commit in the main checkout);
steps of the other path report INAPPLICABLE.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from close_passes import discover_passes, run_passes
from config import MERGE_MODES, Config, RepoError, expand_argv, git_common_dir

# The maintenance passes shipped beside this module.
PASSES_DIR = Path(__file__).resolve().parent / "close_passes"


@dataclass
class CloseCtx:
    """Resolved inputs passed to every step's precheck and apply."""

    project_id: str
    project_worktree: Path   # absolute path to the project worktree
    cv_root: Path            # absolute path to the main repo (CV_ROOT)
    project_branch: str      # "project/<NNNN>-<slug>"
    slug: str                # e.g. "token-efficiency-audit"
    old_rel: str             # "<projects_dir>/current/project-<NNNN>-<slug>"
    new_rel: str             # "<projects_dir>/completed/project-<NNNN>-<slug>"

    # STATUS.md `worktree: none`: sessions on the base branch, no worktree or
    # project branch; `project_worktree` is then the main checkout.
    session_mode: bool = False
    # The repo's config (lib/config.py); the defaults when the repo has none.
    cfg: Config = field(default_factory=Config)
    # Populated by step prechecks so apply() can use resolved objects directly.
    _resolved: dict = field(default_factory=dict)


@dataclass
class CloseStep:
    name: str
    description: str
    mutating: bool
    precheck: Callable[[CloseCtx], str | None]
    apply: Callable[[CloseCtx], None]
    # Non-None: in session mode the step has no subject (the string says why),
    # so it prints INAPPLICABLE and runs neither precheck nor apply.
    session_inapplicable: str | None = None
    # The `close.merge` modes this step belongs to; INAPPLICABLE under others.
    modes: tuple[str, ...] = MERGE_MODES


def step_inapplicable(step: CloseStep, ctx: CloseCtx) -> str | None:
    """Why `step` has no subject in this close (session mode first), or None."""
    if ctx.session_mode and step.session_inapplicable:
        return step.session_inapplicable
    if ctx.cfg.merge not in step.modes:
        return (f"close.merge is {ctx.cfg.merge!r}; this step belongs to the "
                f"{'/'.join(step.modes)} path")
    return None


def _run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), check=check, capture_output=True, text=True)


# Each distinct network query (`gh ...`, `git ls-remote`), keyed by argv and
# cwd, runs at most once per close-project.py invocation; a repeat gets the
# first answer or re-raises the first exception. `_gh_mutate` forgets every
# cached query a PR create or merge can change, keeping `_STABLE_QUERIES`.
_COMMAND_CACHE: dict[tuple, object] = {}

_STABLE_QUERIES: tuple[tuple[str, ...], ...] = (
    ("gh", "auth", "status"),
    ("gh", "repo", "view"),
)


def clear_command_cache() -> None:
    """Forget every memoised command result (a new invocation starts empty)."""
    _COMMAND_CACHE.clear()


def forget_volatile_queries() -> None:
    """Forget every cached query a close step can change (see `_STABLE_QUERIES`)."""
    for key in list(_COMMAND_CACHE):
        argv = key[0]
        if not any(argv[:len(p)] == p for p in _STABLE_QUERIES):
            del _COMMAND_CACHE[key]


def run_once(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(cmd)` with output captured, memoised per process.

    Read-only queries only. A non-zero exit is returned, not raised; an
    `OSError` (e.g. a missing `gh`) is cached and re-raised on every repeat.
    """
    key = (tuple(cmd), str(cwd) if cwd is not None else None)
    if key not in _COMMAND_CACHE:
        try:
            _COMMAND_CACHE[key] = subprocess.run(
                cmd, cwd=None if cwd is None else str(cwd),
                capture_output=True, text=True)
        except OSError as exc:
            _COMMAND_CACHE[key] = exc
    result = _COMMAND_CACHE[key]
    if isinstance(result, OSError):
        raise result
    return result  # type: ignore[return-value]


def _gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """A read-only `gh` query, memoised (`run_once`)."""
    return run_once(["gh", *args], cwd)


def _gh_mutate(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """A state-changing `gh` command: never memoised; forgets volatile queries."""
    try:
        return subprocess.run(["gh", *args], cwd=str(cwd),
                              capture_output=True, text=True)
    finally:
        forget_volatile_queries()


def _gh_auth_status() -> subprocess.CompletedProcess[str]:
    return run_once(["gh", "auth", "status"])


# --- Step definitions, in run order (the registry is `STEPS` below) ---

# Step 0 — backlog cap guard. Runs FIRST, so refusing a backlog file with no
# headroom under its line cap leaves nothing mutated.

#: The line cap on the backlog file.
BACKLOG_LINE_CAP = 150


def _precheck_backlog_cap_guard(ctx: CloseCtx) -> str | None:
    backlog_path = ctx.project_worktree / ctx.cfg.backlog_file
    if not backlog_path.is_file():
        return None
    cap = BACKLOG_LINE_CAP
    actual = len(backlog_path.read_text(encoding="utf-8").splitlines())
    if actual < cap:
        return None
    return (
        f"{ctx.cfg.backlog_file} is already at or over its {cap}-line cap "
        f"({actual} lines) -- refusing before this close mutates anything. "
        "Remove landed or dropped rows from it by hand (git history keeps "
        "them), then re-run."
    )


def _apply_backlog_cap_guard(ctx: CloseCtx) -> None:
    return None  # the precheck is the whole gate


_STEP_BACKLOG_CAP_GUARD = CloseStep(
    name="backlog-cap-guard",
    description=("Refuse before any mutation if the backlog file has no "
                 "headroom left under its line cap"),
    mutating=False,
    precheck=_precheck_backlog_cap_guard,
    apply=_apply_backlog_cap_guard,
)


# Step 1 — git mv current/ -> completed/

def _precheck_git_mv(ctx: CloseCtx) -> str | None:
    src = ctx.project_worktree / ctx.old_rel
    # A close that aborts after this step (a later step failing mid-run) must
    # be re-runnable; src gone with dst present is a resumed close, not a defect.
    already_moved = not src.is_dir() and (ctx.project_worktree / ctx.new_rel).is_dir()
    if not src.is_dir() and not already_moved:
        return f"source folder not found: {src}"
    # Verify the project id is known before anything moves, so an unknown id
    # refuses here rather than failing in a later step.
    from streams import load_projects
    projects = load_projects(ctx.project_worktree)
    if ctx.project_id not in projects:
        return (
            f"project id {ctx.project_id!r} resolves to no folder under "
            f"{ctx.cfg.projects_dir}/ in {ctx.project_worktree}"
        )
    ctx._resolved["git_mv_src"] = None if already_moved else src
    return None


def _apply_git_mv(ctx: CloseCtx) -> None:
    if ctx._resolved.get("git_mv_src") is None:
        return
    _run(
        ["git", "-C", str(ctx.project_worktree), "mv", ctx.old_rel, ctx.new_rel],
        cwd=ctx.project_worktree,
    )


_STEP_GIT_MV = CloseStep(
    name="git-mv",
    description="Move project folder from current/ to completed/ via git mv",
    mutating=True,
    precheck=_precheck_git_mv,
    apply=_apply_git_mv,
)


# Step 1b — flip STATUS.md frontmatter status -> complete

_STATUS_FM_RE = re.compile(r"\A(---\r?\n)(.*?)(\r?\n---\r?\n)", re.DOTALL)
# Column-anchored: the nested `status:` keys under `waves:` are indented and
# must not be touched.
_STATUS_KEY_RE = re.compile(r"^(status:[ \t]*)(\S+)[ \t]*$", re.MULTILINE)


def find_status_md(ctx: CloseCtx) -> Path | None:
    """STATUS.md under new_rel (after git-mv) or old_rel (dry-run), or None."""
    for rel in (ctx.new_rel, ctx.old_rel):
        path = ctx.project_worktree / rel / "STATUS.md"
        if path.is_file():
            return path
    return None


def _precheck_status_flip(ctx: CloseCtx) -> str | None:
    path = find_status_md(ctx)
    if path is None:
        return f"STATUS.md not found under {ctx.old_rel} or {ctx.new_rel}"
    m = _STATUS_FM_RE.match(path.read_text(encoding="utf-8"))
    if m is None:
        return f"STATUS.md has no frontmatter block: {path}"
    if not _STATUS_KEY_RE.search(m.group(2)):
        return f"STATUS.md frontmatter has no top-level 'status:' key: {path}"
    ctx._resolved["status_md_path"] = path
    return None


def _apply_status_flip(ctx: CloseCtx) -> None:
    path: Path = ctx._resolved["status_md_path"]
    text = path.read_text(encoding="utf-8")
    m = _STATUS_FM_RE.match(text)
    if m is None:
        raise RuntimeError(f"STATUS.md frontmatter vanished between precheck and apply: {path}")
    new_fm, n = _STATUS_KEY_RE.subn(r"\g<1>complete", m.group(2), count=1)
    if n == 0:
        raise RuntimeError(f"STATUS.md top-level 'status:' key vanished between precheck and apply: {path}")
    path.write_text(
        text[:m.start()] + m.group(1) + new_fm + m.group(3) + text[m.end():],
        encoding="utf-8",
    )
    _run(
        ["git", "-C", str(ctx.project_worktree), "add", str(path)],
        cwd=ctx.project_worktree,
    )


_STEP_STATUS_FLIP = CloseStep(
    name="status-flip",
    description="Rewrite STATUS.md frontmatter status -> complete and stage it",
    mutating=True,
    precheck=_precheck_status_flip,
    apply=_apply_status_flip,
)


# Step 2b — flip the index row (config `index_file`)

# Shared by the index-row precheck and close-project.py's close:workflow_key
# gate so the two agree. A trailing YAML comment is accepted; a quoted value
# is unquoted so the index cell never carries the quotes.
_WORKFLOW_KEY_RE = re.compile(
    r"^workflow:[ \t]*(?:\"(?P<dq>[^\"\n]+)\"|'(?P<sq>[^'\n]+)'|(?P<bare>[^\s#'\"]\S*))"
    r"[ \t]*(?:#[^\n]*)?$",
    re.MULTILINE,
)


def workflow_key_value(text: str) -> str | None:
    """The top-level `workflow:` value in STATUS.md text, or None if absent
    or blank (a whitespace-only quoted value counts as blank)."""
    m = _WORKFLOW_KEY_RE.search(text)
    if m is None:
        return None
    return (m.group("dq") or m.group("sq") or m.group("bare")).strip() or None


def _index_row_re(project_id: str) -> re.Pattern[str]:
    return re.compile(
        rf"^\|[ \t]*{re.escape(project_id)}[ \t]*\|(?P<name>[^|\n]*)"
        rf"\|(?P<where>[^|\n]*)\|(?P<status>[^\n]*?)\|[ \t]*$",
        re.MULTILINE,
    )


def _resolve_index_file(candidate: Path) -> Path | None:
    """`candidate` (config `index_file`) if it has an `## Index` table, else None."""
    if not candidate.is_file():
        return None
    lines = candidate.read_text(encoding="utf-8").splitlines(keepends=True)
    return candidate if _locate_index_table(lines) is not None else None


def _precheck_index_row(ctx: CloseCtx) -> str | None:
    readme = _resolve_index_file(ctx.project_worktree / ctx.cfg.index_file)
    if readme is None:
        ctx._resolved["index_readme"] = None
        return None
    status_md = find_status_md(ctx)
    if status_md is None:
        return f"STATUS.md not found under {ctx.old_rel} or {ctx.new_rel}"
    workflow_value = workflow_key_value(status_md.read_text(encoding="utf-8"))
    if workflow_value is None:
        return f"STATUS.md frontmatter has no top-level 'workflow:' key: {status_md}"
    ctx._resolved["index_readme"] = readme
    ctx._resolved["index_workflow"] = workflow_value
    # A missing row is not a refusal: `_apply_index_row` writes one.
    ctx._resolved["index_row_present"] = (
        _index_row_re(ctx.project_id).search(readme.read_text(encoding="utf-8"))
        is not None
    )
    return None


def _apply_index_row(ctx: CloseCtx) -> None:
    readme: Path | None = ctx._resolved.get("index_readme")
    if readme is None:
        return
    workflow: str = ctx._resolved["index_workflow"]
    closed_on = date.today().isoformat()

    if not ctx._resolved.get("index_row_present", True):
        # Repair path only: new-project.py writes this row at scaffold. The
        # row carries the table's own column count so no cell shifts.
        text = readme.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        located = _locate_index_table(lines)
        ncols = 4
        if located is not None:
            ncols = len(lines[located[0]].strip().strip("|").split("|"))
        status_cell = f"{workflow} — closed {closed_on}"
        cells = [ctx.project_id, ctx.slug, "completed", status_cell]
        cells += ["--"] * max(0, ncols - len(cells))
        new_row = "| " + " | ".join(cells) + " |\n"
        if located is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(new_row)
        else:
            _, _, _, data_end = located
            lines.insert(data_end, new_row)
        readme.write_text("".join(lines), encoding="utf-8")
        _run(["git", "-C", str(ctx.project_worktree), "add", str(readme)],
             cwd=ctx.project_worktree)
        print(f"note: no index row for {ctx.project_id!r} in {readme} — wrote one")
        return

    def _rewrite(m: re.Match[str]) -> str:
        status = m.group("status").strip()
        # The scheduling fragment before the first ';' ("brief written <date> —
        # wave N") is stale at close; the description after it is not. With
        # no ';' the cell is a bare state word (`active`, `planned`): drop it.
        _, _, tail = status.partition(";")
        tail = tail.strip()
        head = f"{workflow} — closed {closed_on}"
        return (
            f"| {ctx.project_id} |{m.group('name')}| completed | "
            f"{head}; {tail} |" if tail else
            f"| {ctx.project_id} |{m.group('name')}| completed | {head} |"
        )

    text = readme.read_text(encoding="utf-8")
    readme.write_text(_index_row_re(ctx.project_id).sub(_rewrite, text, count=1), encoding="utf-8")
    _run(["git", "-C", str(ctx.project_worktree), "add", str(readme)], cwd=ctx.project_worktree)


_STEP_INDEX_ROW = CloseStep(
    name="index-row",
    description="Flip the project's index row to completed and stage it",
    mutating=True,
    precheck=_precheck_index_row,
    apply=_apply_index_row,
)


# Step 2c — maintenance passes (contract and loader: close_passes/). Before
# git-commit, so a file a pass writes lands in the archive commit. precheck()
# only discovers the passes under `PASSES_DIR` (zero mutation); apply() runs them.

def _precheck_maintenance_passes(ctx: CloseCtx) -> str | None:
    passes_dir = PASSES_DIR
    # An import error or a `.name` collision becomes a named precheck failure.
    try:
        passes = discover_passes(passes_dir)
    except Exception as exc:
        return (
            f"maintenance-pass discovery failed: {type(exc).__name__}: {exc} "
            f"(scanned {passes_dir})"
        )
    # Name the source, so a stale one is visible even when nothing fails.
    print(f"  maintenance-passes: discovered {len(passes)} pass(es) from {passes_dir}")
    ctx._resolved["maintenance_passes"] = passes
    ctx._resolved["maintenance_passes_source"] = passes_dir
    return None


def _stage_pass_outputs(ctx: CloseCtx, outcomes) -> None:
    """Stage every file inside the worktree that the passes reported writing.

    Runs before the blocking-failure raise, so an aborted close keeps its
    evidence staged.
    """
    root = ctx.project_worktree.resolve()
    for outcome in outcomes:
        path = getattr(outcome.result, "detail_path", None)
        if path is None:
            continue
        path = Path(path)
        if not path.is_file():
            continue
        try:
            path.resolve().relative_to(root)
        except ValueError:
            continue  # a pass may report a path outside the worktree
        _run(["git", "-C", str(root), "add", str(path)], cwd=root, check=False)


def _require_resolved(ctx: CloseCtx, key: str):
    """`ctx._resolved[key]`; a missing key means the precheck was skipped."""
    if key not in ctx._resolved:
        raise KeyError(f"{key} missing from ctx._resolved — precheck must run before apply")
    return ctx._resolved[key]


def _apply_maintenance_passes(ctx: CloseCtx) -> None:
    passes = _require_resolved(ctx, "maintenance_passes")
    outcomes, overall_passed = run_passes(passes, ctx)
    # close-project.py's step loop prints these after the step.
    ctx._resolved["maintenance_pass_outcomes"] = outcomes
    _stage_pass_outputs(ctx, outcomes)
    if not overall_passed:
        failing = ", ".join(
            o.pass_name for o in outcomes if o.blocking and o.result.status == "fail"
        )
        raise RuntimeError(f"blocking maintenance pass(es) failed: {failing}")


_STEP_MAINTENANCE_PASSES = CloseStep(
    name="maintenance-passes",
    description=(
        "Discover and run every pass under close_passes/ (the backlog F-id "
        "collision check); a blocking=True pass that fails aborts the close"
    ),
    mutating=True,  # a pass may write a file; apply() stages what it reports
    precheck=_precheck_maintenance_passes,
    apply=_apply_maintenance_passes,
)


_INDEX_HEADING = "## Index"


def _locate_index_table(lines: "list[str]") -> "tuple[int, int, int, int] | None":
    """(header_idx, sep_idx, data_start, data_end) under Index heading, or None."""
    try:
        heading_idx = next(i for i, ln in enumerate(lines) if ln.strip() == _INDEX_HEADING)
    except StopIteration:
        return None
    i = heading_idx + 1
    while i < len(lines) and lines[i].strip() == "":
        i += 1
    if i >= len(lines) or not lines[i].lstrip().startswith("|"):
        return None
    header_idx = i
    sep_idx = i + 1
    if sep_idx >= len(lines) or not lines[sep_idx].lstrip().startswith("|"):
        return None
    data_start = sep_idx + 1
    data_end = data_start
    while data_end < len(lines) and lines[data_end].lstrip().startswith("|"):
        data_end += 1
    return header_idx, sep_idx, data_start, data_end


# Step 3 — git commit

def _precheck_commit(ctx: CloseCtx) -> str | None:
    r = _run(
        ["git", "-C", str(ctx.project_worktree), "diff", "--cached", "--name-only"],
        cwd=ctx.project_worktree, check=False,
    )
    if r.returncode != 0:
        return f"git diff --cached failed: {r.stderr.strip()}"
    return None


def _apply_commit(ctx: CloseCtx) -> None:
    staged = _run(
        ["git", "-C", str(ctx.project_worktree), "diff", "--cached", "--name-only"],
        cwd=ctx.project_worktree,
    )
    # Resumed close: the archive commit already landed, so the index is clean.
    if not staged.stdout.strip():
        return
    _run(
        ["git", "-C", str(ctx.project_worktree), "commit",
         "-m", f"chore({ctx.project_id}): archive project"],
        cwd=ctx.project_worktree,
    )


_STEP_COMMIT = CloseStep(
    name="git-commit",
    description="Commit the archive move and its staged edits on the project branch",
    mutating=True,
    precheck=_precheck_commit,
    apply=_apply_commit,
)


# Step 3b — local merge commit (close.merge: local): `--no-ff` into the base
# branch in the main checkout. Nothing is pushed.

def _branch_merged(cv_root: Path, branch: str, base: str) -> bool:
    r = _run(["git", "-C", str(cv_root), "merge-base", "--is-ancestor", branch, base],
             cwd=cv_root, check=False)
    return r.returncode == 0


def _precheck_local_merge(ctx: CloseCtx) -> str | None:
    base = ctx.cfg.base_branch
    cv = ctx.cv_root
    head = _run(["git", "-C", str(cv), "branch", "--show-current"],
                cwd=cv, check=False).stdout.strip()
    if head != base:
        return (f"the main checkout {cv} is on {head or 'a detached HEAD'!r}, "
                f"not {base!r}; check out {base} there and re-run")
    if _merge_head(cv):
        return (f"the main checkout {cv} is mid-merge (MERGE_HEAD exists); "
                "never complete or abort another session's merge")
    dirty = _run(["git", "-C", str(cv), "status", "--porcelain",
                  "--untracked-files=no"], cwd=cv, check=False)
    if dirty.returncode != 0 or dirty.stdout.strip():
        return f"the main checkout {cv} has tracked changes; commit or stash them first"
    r = _run(["git", "-C", str(cv), "rev-parse", "--verify", ctx.project_branch],
             cwd=cv, check=False)
    if r.returncode != 0:
        return f"branch {ctx.project_branch!r} not found in {cv}"
    # Resumed close: the merge already landed.
    ctx._resolved["local_merge_done"] = _branch_merged(cv, ctx.project_branch, base)
    return None


def _merge_head(cv: Path) -> str:
    """The commit MERGE_HEAD names in `cv`, or "" when no merge is in progress."""
    r = _run(["git", "-C", str(cv), "rev-parse", "-q", "--verify", "MERGE_HEAD"],
             cwd=cv, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


# What `git merge` prints when it refuses to start because a merge is already
# in progress in the checkout.
_MERGE_IN_PROGRESS_MARKERS = (
    "not concluded your merge", "MERGE_HEAD exists", "You have not concluded",
)


def _foreign_merge_msg(ctx: CloseCtx, merge_head: str) -> str:
    return (f"the main checkout {ctx.cv_root} is mid-merge (MERGE_HEAD "
            f"{merge_head}), a merge this close did not start; refusing to "
            "complete or abort another session's merge, and touched nothing. "
            f"Rerun the close once that merge is finished or aborted.")


def _apply_local_merge(ctx: CloseCtx) -> None:
    if ctx._resolved.get("local_merge_done"):
        print(f"note: {ctx.project_branch} is already merged into "
              f"{ctx.cfg.base_branch} — skipping merge (resumed close)")
        return
    cv = ctx.cv_root
    # A MERGE_HEAD started since the precheck is another session's: refuse.
    before = _merge_head(cv)
    if before:
        raise RuntimeError(_foreign_merge_msg(ctx, before))
    # Pin the sha we merge; ownership of a later MERGE_HEAD is judged by it.
    tip = _run(["git", "-C", str(cv), "rev-parse", "--verify",
                ctx.project_branch], cwd=cv, check=False).stdout.strip()
    r = _run(["git", "-C", str(cv), "merge", "--no-ff", "--no-edit",
              "-m", f"merge({ctx.project_id}): close and archive",
              tip or ctx.project_branch], cwd=cv, check=False)
    if r.returncode == 0:
        return
    # Abort only our own MERGE_HEAD (else no rerun passes the precheck). If
    # git refused to start, or MERGE_HEAD is not our sha, it is theirs.
    after = _merge_head(cv)
    refused = any(m in (r.stderr or "") for m in _MERGE_IN_PROGRESS_MARKERS)
    if after and (refused or after != tip):
        raise RuntimeError(_foreign_merge_msg(ctx, after))
    conflicts = _run(["git", "-C", str(cv), "diff", "--name-only",
                      "--diff-filter=U"], cwd=cv, check=False).stdout.split()
    mid_merge = bool(after)
    aborted = ""
    if mid_merge:
        a = _run(["git", "-C", str(cv), "merge", "--abort"], cwd=cv, check=False)
        aborted = ("; the merge was aborted, so the main checkout is as it was"
                   if a.returncode == 0 else
                   f"; `git merge --abort` ALSO failed ({a.stderr.strip()[:200]}), "
                   f"so {cv} is still mid-merge: resolve it there by hand")
    base = ctx.cfg.base_branch
    what = (f"conflicts in {', '.join(conflicts)}" if conflicts
            else (r.stderr or r.stdout).strip()[:300] or f"exit {r.returncode}")
    raise RuntimeError(
        f"merging {ctx.project_branch} into {base} in {cv} failed: {what}{aborted}. "
        f"Remedy: in the project worktree {ctx.project_worktree}, merge {base} "
        f"into {ctx.project_branch} (`git merge {base}`), resolve and commit, "
        f"then rerun the close.")


_STEP_LOCAL_MERGE = CloseStep(
    name="local-merge",
    description=("Merge the project branch into the base branch in the main "
                 "checkout with a merge commit (no push)"),
    mutating=True,
    precheck=_precheck_local_merge,
    apply=_apply_local_merge,
    modes=("local",),
    session_inapplicable=(
        "the project's commits are already on the base branch; there is no "
        "project branch to merge"
    ),
)


# Step 4 — git push

def main_ahead_of_origin(cv_root: Path, base: str = "main") -> "tuple[int | None, str]":
    """(commits local `base` has that local `origin/<base>` lacks, detail).

    No fetch: a stale ref only overstates the count. None: could not tell.
    """
    r = _run(
        ["git", "-C", str(cv_root), "rev-list", "--count", f"origin/{base}..{base}"],
        cwd=cv_root, check=False,
    )
    if r.returncode != 0:
        return None, (r.stderr or "").strip() or "git rev-list failed"
    try:
        return int(r.stdout.strip()), ""
    except ValueError:
        return None, f"unexpected rev-list output {r.stdout.strip()!r}"


def main_ahead_refusal(cv_root: Path, project_branch: str,
                       base: str = "main") -> str | None:
    """Refusal text, or None when local `base` is level with its origin (P15)."""
    ahead, detail = main_ahead_of_origin(cv_root, base)
    if ahead is None:
        return (f"cannot tell whether local {base} is ahead of origin/{base} "
                f"({detail}); refusing to push. Run `git fetch origin` in "
                f"{cv_root} and re-run.")
    if ahead > 0:
        return (f"local {base} is {ahead} commit(s) ahead of origin/{base} in "
                f"{cv_root}; pushing {project_branch} would publish "
                f"them inside the branch. Push {base} first (`git push origin "
                f"{base}`), or `git fetch origin` if origin/{base} is merely "
                f"stale, then re-run.")
    return None


def _precheck_push(ctx: CloseCtx) -> str | None:
    r = _run(
        ["git", "-C", str(ctx.project_worktree), "rev-parse", "--verify",
         ctx.project_branch],
        cwd=ctx.project_worktree, check=False,
    )
    if r.returncode != 0:
        return f"branch {ctx.project_branch!r} not found in worktree"
    # Unpushed base commits would ride inside the branch push (P15 is first).
    return main_ahead_refusal(ctx.cv_root, ctx.project_branch, ctx.cfg.base_branch)


def _apply_push(ctx: CloseCtx) -> None:
    _run(
        ["git", "-C", str(ctx.project_worktree), "push", "-u", "origin",
         ctx.project_branch],
        cwd=ctx.project_worktree,
    )


_STEP_PUSH = CloseStep(
    name="git-push",
    description="Push project branch to origin",
    mutating=True,
    precheck=_precheck_push,
    apply=_apply_push,
    modes=("squash",),
    session_inapplicable=(
        "the project's commits are already on `main`; there is no project "
        "branch to publish. Whether and when `main` is pushed is the session's "
        "own cadence, not a decision this verb makes on its way past"
    ),
)


# Step 5 — gh pr create

_REPO_VIEW_ARGS = ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]


def github_repo_name(worktree: Path) -> str | None:
    """`owner/name` of the GitHub repo `gh` resolves from `worktree`, or None."""
    try:
        r = _gh(_REPO_VIEW_ARGS, worktree)
    except OSError:
        return None
    name = (r.stdout or "").strip()
    return name if r.returncode == 0 and name else None


def github_remote_refusal(worktree: Path) -> str | None:
    """None when `gh` resolves a GitHub repo from `worktree`'s remotes.

    Any failure, `gh` missing included, is a refusal: no PR can be opened.
    """
    try:
        r = _gh(_REPO_VIEW_ARGS, worktree)
    except OSError as exc:
        return f"gh not runnable ({exc}); cannot resolve a GitHub repo"
    if r.returncode != 0 or not (r.stdout or "").strip():
        detail = (r.stderr or "").strip()[:160] or "gh resolved no repository"
        return f"no git remote resolves to a GitHub repo in {worktree}: {detail}"
    return None


def _gh_refusal(worktree: Path) -> str | None:
    """Refusal when `gh` is unauthenticated or resolves no GitHub repo, else None."""
    try:
        r = _gh_auth_status()
    except OSError as exc:
        return f"gh not runnable ({exc})"
    if r.returncode != 0:
        return f"gh auth status failed: {r.stderr.strip()[:120]}"
    return github_remote_refusal(worktree)


def _precheck_pr_create(ctx: CloseCtx) -> str | None:
    return _gh_refusal(ctx.project_worktree)


def _apply_pr_create(ctx: CloseCtx) -> None:
    r = _gh_mutate(
        [
            "pr", "create",
            "--base", ctx.cfg.base_branch,
            "--head", ctx.project_branch,
            "--title", f"chore({ctx.project_id}): close and archive",
            "--body",
            f"Project {ctx.project_id} — every stream closed. "
            "Folder moved to completed/.",
        ],
        ctx.project_worktree,
    )
    if r.returncode != 0 and "already exists" not in (r.stderr or ""):
        raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)


_STEP_PR_CREATE = CloseStep(
    name="gh-pr-create",
    description="Open a PR from the project branch to the base branch",
    mutating=True,
    precheck=_precheck_pr_create,
    apply=_apply_pr_create,
    modes=("squash",),
    session_inapplicable=(
        "head and base would both be `main` -- there is no pair of refs to "
        "open a pull request between"
    ),
)


# Step 6 — gh pr merge --squash --delete-branch

def _pr_state(ctx: CloseCtx) -> str | None:
    """OPEN / MERGED / CLOSED, or None when gh cannot resolve a PR for the branch."""
    r = _gh(
        ["pr", "view", ctx.project_branch, "--json", "state", "-q", ".state"],
        ctx.project_worktree,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _merge_commit_sha(ctx: CloseCtx) -> str | None:
    """The PR's `mergeCommit.oid`, or None (set once a squash lands, even
    while `state` lags)."""
    r = _gh(
        ["pr", "view", ctx.project_branch, "--json", "mergeCommit",
         "-q", ".mergeCommit.oid"],
        ctx.project_worktree,
    )
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _merge_landed(ctx: CloseCtx) -> bool:
    """True when the PR's merge commit is an ancestor of origin/<base> (after a fetch)."""
    sha = _merge_commit_sha(ctx)
    if sha is None:
        return False
    _run(["git", "-C", str(ctx.project_worktree), "fetch", "origin"],
         cwd=ctx.project_worktree, check=False)
    r = _run(
        ["git", "-C", str(ctx.project_worktree), "merge-base",
         "--is-ancestor", sha, f"origin/{ctx.cfg.base_branch}"],
        cwd=ctx.project_worktree, check=False,
    )
    return r.returncode == 0


# gh's own wording when a branch has no (required) checks at all -- the one
# empty-payload case that is an all-clear rather than an unanswered query.
_NO_CHECKS_MARKERS = ("no checks reported", "no required checks")


def _pr_checks_status(ctx: CloseCtx) -> str | None:
    """Refusal when a required PR check fails or cannot be read, else None.

    Call only once a PR exists. An empty payload clears the gate only with
    gh's own no-checks message; any other unanswered query refuses.
    """
    cmd = ["pr", "checks", ctx.project_branch, "--required",
           "--json", "bucket,name,link"]
    repo = ctx._resolved.get("gh_repo")
    if repo:
        cmd += ["--repo", repo]
    r = _gh(cmd, ctx.project_worktree)
    stdout = (r.stdout or "").strip()
    stderr = (r.stderr or "").strip()
    if not stdout:
        if any(m in stderr.lower() for m in _NO_CHECKS_MARKERS):
            return None
        detail = stderr[:120] or f"gh exited {r.returncode} with no output"
        return (f"cannot read required PR checks on {ctx.project_branch}: "
                f"{detail} -- re-run the close once gh answers, or merge by "
                "hand after checking the PR")
    try:
        checks = json.loads(stdout)
    except json.JSONDecodeError:
        return (f"unparseable `gh pr checks` output for {ctx.project_branch} "
                "-- cannot confirm the PR is green; re-run the close")
    failed = [c for c in checks if c.get("bucket") == "fail"]
    if not failed:
        return None
    names = ", ".join(c.get("name", "?") for c in failed[:5])
    return (f"required PR check(s) failing on {ctx.project_branch}: {names} "
            "-- fix the PR before merging")


def _precheck_pr_merge(ctx: CloseCtx) -> str | None:
    refusal = _gh_refusal(ctx.project_worktree)
    if refusal is not None:
        return refusal
    # --repo keeps `gh pr merge --delete-branch` on the API path; without it
    # gh checks out the base branch, which fails when another worktree has it.
    ctx._resolved["gh_repo"] = github_repo_name(ctx.project_worktree)
    pr_state = _pr_state(ctx)
    ctx._resolved["pr_state"] = pr_state
    # No PR yet is legal (the --preflight pass runs before gh-pr-create).
    if pr_state is not None:
        checks_reason = _pr_checks_status(ctx)
        if checks_reason is not None:
            return checks_reason
    return None


def _apply_pr_merge(ctx: CloseCtx) -> None:
    if ctx._resolved.get("pr_state") == "MERGED":
        print(f"note: PR for {ctx.project_branch} is already MERGED — skipping merge")
        return

    cmd = ["pr", "merge", "--squash", "--delete-branch"]
    repo = ctx._resolved.get("gh_repo")
    if repo:
        cmd += ["--repo", repo]
    cmd.append(ctx.project_branch)

    r = _gh_mutate(cmd, ctx.project_worktree)
    if r.returncode == 0:
        return
    # gh can fail after the squash landed: ancestry decides, not PR state.
    if _merge_landed(ctx):
        print(
            f"note: gh pr merge exited {r.returncode} (pr state: {_pr_state(ctx)!r}) "
            f"but its merge commit is an ancestor of origin/{ctx.cfg.base_branch} — continuing. "
            f"stderr: {(r.stderr or '').strip()[:200]}"
        )
        return
    raise subprocess.CalledProcessError(r.returncode, r.args, r.stdout, r.stderr)


_STEP_PR_MERGE = CloseStep(
    name="gh-pr-merge",
    description="Squash-merge PR and delete remote branch via gh",
    mutating=True,
    precheck=_precheck_pr_merge,
    apply=_apply_pr_merge,
    modes=("squash",),
    session_inapplicable="no PR was opened, so there is none to merge",
)


# Step 7 — git pull the base branch in the main checkout

def _precheck_pull_main(ctx: CloseCtx) -> str | None:
    if not ctx.cv_root.is_dir():
        return f"main repo not found at {ctx.cv_root}"
    base = ctx.cfg.base_branch
    r = _run(
        ["git", "-C", str(ctx.cv_root), "rev-parse", "--verify", base],
        cwd=ctx.cv_root, check=False,
    )
    if r.returncode != 0:
        return f"branch {base!r} not found in main repo"
    return None


def _apply_pull_main(ctx: CloseCtx) -> None:
    # --ff-only: never create a merge commit on the base (fast-forward only).
    _run(["git", "-C", str(ctx.cv_root), "pull", "--ff-only"], cwd=ctx.cv_root)


_STEP_PULL_MAIN = CloseStep(
    name="git-pull-main",
    description="Pull origin into main repo so the worktree removal sees the merged tip",
    mutating=True,
    precheck=_precheck_pull_main,
    apply=_apply_pull_main,
    modes=("squash",),
    session_inapplicable=(
        "this step exists to bring a squash-merge back into the main checkout; "
        "in session mode the archive commit was made in that checkout directly, "
        "so there is nothing to pull and a pull here would only drag unrelated "
        "remote work into the middle of a close"
    ),
)


# Step 10 — reconcile-main: fast-forward the local base branch when behind;
# refuse on diverged. Runs after step 7 (git-pull-main).

ALREADY_CURRENT = "already-current"
BEHIND_FAST_FORWARDABLE = "behind-fast-forwardable"
AHEAD = "ahead"
DIVERGED = "diverged"


class RemoteNotFetchedError(Exception):
    """The remote commit is not local; ``fetch_cmd`` is what the operator runs."""
    def __init__(self, fetch_cmd: str) -> None:
        super().__init__(f"remote commit not local — run: {fetch_cmd}")
        self.fetch_cmd = fetch_cmd


def classify_main(cv_root: Path, base: str = "main") -> str:
    """Classify local `base` against origin's, writing no refs (no fetch).

    Returns ALREADY_CURRENT / BEHIND_FAST_FORWARDABLE / AHEAD / DIVERGED.
    Raises RemoteNotFetchedError when the remote commit is not in the local
    object store, and subprocess.CalledProcessError on git failure.
    """
    ref = f"refs/heads/{base}"
    local_r = _run(
        ["git", "-C", str(cv_root), "rev-parse", base], cwd=cv_root,
    )
    local_sha = local_r.stdout.strip()

    # Memoised (`run_once`); `_gh_mutate` forgets it once a merge moves main.
    ls_r = run_once(
        ["git", "-C", str(cv_root), "ls-remote", "origin", ref],
        cv_root,
    )
    if ls_r.returncode != 0:
        raise subprocess.CalledProcessError(
            ls_r.returncode, ls_r.args, ls_r.stdout, ls_r.stderr)
    # Pick the "<sha>\t<ref>" line for the base ref; none counts as current.
    remote_sha = None
    for line in ls_r.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1] == ref:
            remote_sha = parts[0]
            break
    if remote_sha is None or local_sha == remote_sha:
        return ALREADY_CURRENT

    cat_r = _run(
        ["git", "-C", str(cv_root), "cat-file", "-e", f"{remote_sha}^{{commit}}"],
        cwd=cv_root, check=False,
    )
    if cat_r.returncode != 0:
        raise RemoteNotFetchedError(
            f"git -C {cv_root} fetch origin {base}"
        )

    base_r = _run(
        ["git", "-C", str(cv_root), "merge-base", base, remote_sha],
        cwd=cv_root,
    )
    base_sha = base_r.stdout.strip()
    if base_sha == local_sha:
        return BEHIND_FAST_FORWARDABLE
    if base_sha == remote_sha:
        return AHEAD
    return DIVERGED


def _precheck_reconcile_main(ctx: CloseCtx) -> str | None:
    try:
        classification = classify_main(ctx.cv_root, ctx.cfg.base_branch)
    except RemoteNotFetchedError as exc:
        return (
            f"remote commit not yet local — classification requires a fetch first. "
            f"Run: {exc.fetch_cmd}"
        )
    except subprocess.CalledProcessError as exc:
        return f"git ls-remote/compare failed: {exc.stderr.strip()[:200] if exc.stderr else exc}"
    if classification == DIVERGED:
        return (
            f"local {ctx.cfg.base_branch} has diverged from origin/{ctx.cfg.base_branch} — "
            f"manual reconciliation required. "
            f"Run: git -C {ctx.cv_root} log {ctx.cfg.base_branch}..origin/{ctx.cfg.base_branch} --oneline"
        )
    ctx._resolved["main_classification"] = classification
    return None


def _apply_reconcile_main(ctx: CloseCtx) -> None:
    classification = _require_resolved(ctx, "main_classification")
    if classification in (ALREADY_CURRENT, AHEAD):
        return  # AHEAD: nothing to fast-forward, and pushing is not this step's
    if classification == DIVERGED:
        raise RuntimeError(
            "reconcile-main apply called on a diverged main — precheck should have blocked this"
        )
    # BEHIND_FAST_FORWARDABLE: fetch then advance via ff-only.
    _run(["git", "-C", str(ctx.cv_root), "fetch", "origin", ctx.cfg.base_branch],
         cwd=ctx.cv_root)
    _run(
        ["git", "-C", str(ctx.cv_root), "pull", "--ff-only"],
        cwd=ctx.cv_root,
    )


_STEP_RECONCILE_MAIN = CloseStep(
    name="reconcile-main",
    description=(
        "Fetch the base branch and fast-forward it locally when behind; "
        "refuse (precheck FAIL) on diverged — never resets or force-pushes"
    ),
    mutating=True,
    precheck=_precheck_reconcile_main,
    apply=_apply_reconcile_main,
    modes=("squash",),
    session_inapplicable=(
        "same reason as git-pull-main -- there is no merge to reconcile local "
        "`main` against, and local `main` is the tree the close just wrote to"
    ),
)


# Step 8 — git worktree remove

def _precheck_worktree_remove(ctx: CloseCtx) -> str | None:
    """Refuse an unregistered worktree.

    Links are not probed: `teardown` has no read-only mode, so a teardown
    problem surfaces only at apply, which raises before `git worktree remove`.
    """
    if not ctx.project_worktree.exists():
        # Already gone is a resumed close, not a defect.
        ctx._resolved["worktree_already_removed"] = True
        return None
    ctx._resolved["worktree_already_removed"] = False
    r = _run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=ctx.cv_root, check=False,
    )
    # Resolved paths: git prints `C:/...`, a Windows Path `C:\...`.
    wanted = ctx.project_worktree.resolve()
    registered = any(
        Path(line[len("worktree "):]).resolve() == wanted
        for line in r.stdout.splitlines()
        if line.startswith("worktree ")
    )
    if not registered:
        return f"worktree {ctx.project_worktree} not registered with git"
    return None


def teardown_worktree(cv_root: Path, worktree: Path,
                      teardown: "tuple[str, ...] | None") -> None:
    """Run the config's `teardown` argv on `worktree`, from `cv_root`.

    Unlinks junctions/symlinks into the main checkout before removal. `None`
    prints a note; a non-zero exit or a failed start raises `RuntimeError`.
    """
    if teardown is None:
        print("note: no `teardown` configured — nothing to detach before removal")
        return
    argv = expand_argv(teardown, worktree)
    try:
        r = _run(argv, cwd=cv_root, check=False)
    except OSError as exc:
        raise RuntimeError(
            f"teardown could not start ({' '.join(argv)}): {exc}; refusing to "
            f"remove {worktree} while a link may remain") from exc
    if r.returncode != 0:
        detail = ((r.stderr or "").strip() or (r.stdout or "").strip())[:300]
        raise RuntimeError(
            f"teardown `{' '.join(argv)}` exited {r.returncode} for "
            f"{worktree}; refusing to remove it while a link may remain. "
            f"{detail}"
        )


def _apply_worktree_remove(ctx: CloseCtx) -> None:
    if ctx._resolved.get("worktree_already_removed"):
        print(f"note: worktree {ctx.project_worktree} already gone — skipping (resumed close)")
        return
    # FIRST, before any recursive delete: detach links into the main checkout.
    teardown_worktree(ctx.cv_root, ctx.project_worktree, ctx.cfg.teardown)
    # `remove` (never --force) refuses on an untracked .vscode/, and on a
    # deleted one when the repo tracks it: remove it only when untracked.
    vscode = ctx.project_worktree / ".vscode"
    if vscode.is_dir():
        tracked = _run(
            ["git", "-C", str(ctx.project_worktree), "ls-files", "--", ".vscode"],
            cwd=ctx.project_worktree, check=False,
        ).stdout.strip()
        if tracked:
            print("note: .vscode/ is tracked here — leaving it in place "
                  "(removing it would dirty the tree and block the removal)")
        else:
            shutil.rmtree(vscode, ignore_errors=True)
    _run(
        ["git", "worktree", "remove", str(ctx.project_worktree)],
        cwd=ctx.cv_root,
    )
    # A zero exit is not proof: on Windows a locked handle can leave the folder.
    if ctx.project_worktree.exists():
        raise RuntimeError(
            f"`git worktree remove` returned but {ctx.project_worktree} still "
            "exists on disk. Close whatever holds it open, delete the folder "
            "by hand (after the configured `teardown` if a link remains), "
            "then re-run to resume."
        )


_STEP_WORKTREE_REMOVE = CloseStep(
    name="worktree-remove",
    description="Remove the project worktree (git worktree remove; NEVER --force)",
    mutating=True,
    precheck=_precheck_worktree_remove,
    apply=_apply_worktree_remove,
    session_inapplicable=(
        "there is no project worktree -- `project_worktree` IS the main "
        "checkout, and removing it would delete the repository this close is "
        "running in"
    ),
)


# Step 9a — tag the branch tip before branch-delete, so a squash merge does
# not orphan the stream commits. Under `close.merge: local` it is local only.

def _archive_tag_name(ctx: CloseCtx) -> str:
    return f"archive/{ctx.project_id}-{ctx.slug}"


def _precheck_archive_tag(ctx: CloseCtx) -> str | None:
    r = _run(
        ["git", "-C", str(ctx.cv_root), "rev-parse", "--verify", ctx.project_branch],
        cwd=ctx.cv_root, check=False,
    )
    tag = _archive_tag_name(ctx)
    existing = _run(
        ["git", "-C", str(ctx.cv_root), "tag", "--list", tag],
        cwd=ctx.cv_root, check=False,
    )
    tag_exists = bool(existing.stdout.strip())
    branch_found = r.returncode == 0
    # Branch gone with the tag present: an earlier run tagged, then died
    # before its cursor entry. Resume past it rather than wedge the close.
    if not branch_found and not tag_exists:
        return f"branch {ctx.project_branch!r} not found locally and tag {tag!r} does not exist — nothing to tag"
    ctx._resolved["archive_tag"] = tag
    ctx._resolved["archive_tag_exists"] = tag_exists
    ctx._resolved["archive_tag_sha"] = r.stdout.strip() if branch_found else None
    return None


def _apply_archive_tag(ctx: CloseCtx) -> None:
    tag: str = ctx._resolved["archive_tag"]
    if ctx._resolved.get("archive_tag_exists"):
        # A resumed close: tags are immutable -- leave it, never re-point it.
        print(f"note: tag {tag!r} already exists — leaving it (resumed close)")
        return
    sha: str = ctx._resolved["archive_tag_sha"]
    _run(["git", "-C", str(ctx.cv_root), "tag", tag, sha], cwd=ctx.cv_root)
    if ctx.cfg.merge == "squash":
        _run(["git", "-C", str(ctx.cv_root), "push", "origin", tag], cwd=ctx.cv_root)


_STEP_ARCHIVE_TAG = CloseStep(
    name="archive-tag",
    description=(
        "Tag the project branch tip archive/<id>-<slug> (and push it under "
        "close.merge squash) before branch-delete; idempotent -- an "
        "existing tag is left untouched, never re-pointed"
    ),
    mutating=True,
    precheck=_precheck_archive_tag,
    apply=_apply_archive_tag,
    session_inapplicable=(
        "the tag exists to keep a squash-merged project branch's commits "
        "reachable after branch-delete. In session mode the commits are on "
        "the base branch, already reachable and never deleted, and a tag on its "
        "tip would name unrelated work as this project's archive"
    ),
)


# Step 9 — git branch -d + cleanup verification

def _precheck_branch_delete(ctx: CloseCtx) -> str | None:
    r = _run(
        ["git", "-C", str(ctx.cv_root), "branch", "--list", ctx.project_branch],
        cwd=ctx.cv_root, check=False,
    )
    # Already gone is a resumed close: apply skips the delete, still verifies.
    ctx._resolved["branch_already_deleted"] = not r.stdout.strip()
    return None


def _branch_names_project(line: str, project_id: str) -> bool:
    """True when a `git branch --list` line names `project_id` as a token
    delimited by `/` or `-` (the glob also matches `chore/backport-10059`)."""
    name = line.lstrip("*+ \t").strip()
    if not name:
        return False
    return bool(re.search(rf"(?:^|[/-]){re.escape(project_id)}(?:[/-]|$)", name))


def _apply_branch_delete(ctx: CloseCtx) -> None:
    if ctx._resolved.get("branch_already_deleted"):
        print(f"note: branch {ctx.project_branch!r} already gone — skipping delete (resumed close)")
    else:
        _run(
            ["git", "-C", str(ctx.cv_root), "branch", "-d", ctx.project_branch],
            cwd=ctx.cv_root,
        )
    # Verify: no worktree entry nor branch survives.
    wt_list = _run(["git", "worktree", "list"], cwd=ctx.cv_root, check=False)
    br_list = _run(
        ["git", "-C", str(ctx.cv_root), "branch", "--list",
         f"*{ctx.project_id}*"],
        cwd=ctx.cv_root, check=False,
    )
    survivor_wt = [
        ln for ln in wt_list.stdout.splitlines()
        if re.search(rf"-{re.escape(ctx.project_id)}(-|$|\s)", ln)
    ]
    survivor_br = [
        ln for ln in br_list.stdout.splitlines()
        if _branch_names_project(ln, ctx.project_id)
    ]
    if survivor_wt or survivor_br:
        details = "\n".join(
            [f"  worktree: {ln}" for ln in survivor_wt]
            + [f"  branch:   {ln}" for ln in survivor_br]
        )
        raise RuntimeError(
            f"verify FAILED: close did not complete for {ctx.project_id}\n{details}"
        )


_STEP_BRANCH_DELETE = CloseStep(
    name="branch-delete",
    description="Delete local project branch (-d only) and verify no survivors",
    mutating=True,
    precheck=_precheck_branch_delete,
    apply=_apply_branch_delete,
    session_inapplicable=(
        "`project_branch` is `main` -- there is no project branch to delete, "
        "and this step would otherwise run `git branch -d main`"
    ),
)


# Step 11 — prune the stale scaffold left in the MAIN tree

def _precheck_prune_scaffold(ctx: CloseCtx) -> str | None:
    if not ctx.cv_root.is_dir():
        return f"main repo not found at {ctx.cv_root}"
    return None


def _tracked_survivors(cv_root: Path, paths: "list[Path]") -> "list[Path]":
    """Subset of `paths` git still tracks in `cv_root`, in one `git ls-files`.

    Fails safe: on a `git ls-files` error every path counts as tracked.
    """
    if not paths:
        return []
    rels = [p.resolve().relative_to(cv_root.resolve()).as_posix() for p in paths]
    r = _run(["git", "ls-files", "--", *rels], cwd=cv_root, check=False)
    if r.returncode != 0:
        return list(paths)
    tracked_rel = {
        ln.strip().replace("\\", "/") for ln in (r.stdout or "").splitlines() if ln.strip()
    }
    return [p for p, rel in zip(paths, rels) if rel in tracked_rel]


def _apply_prune_scaffold(ctx: CloseCtx) -> None:
    # Merge+pull removes the moved files from main but leaves the empty
    # current/ directories, which would read as a live project.
    stale = ctx.cv_root / ctx.old_rel
    if not stale.is_dir():
        return
    survivors = [p for p in stale.rglob("*") if p.is_file()]
    if survivors:
        # A TRACKED survivor means the archive move failed: refuse. An
        # untracked one is evidence: relocate it into the archive, never drop it.
        tracked = _tracked_survivors(ctx.cv_root, survivors)
        if tracked:
            rel = ", ".join(sorted(str(p.relative_to(stale)) for p in tracked)[:5])
            raise RuntimeError(
                f"refusing to prune {ctx.old_rel}: {len(tracked)} TRACKED file(s) still "
                f"present ({rel}). The archive move left content behind — resolve by hand."
            )
        # A survivor whose destination already exists in the archive would
        # overwrite committed content: refuse before anything moves.
        dest_root = ctx.cv_root / ctx.new_rel
        dests = [(p, dest_root / p.relative_to(stale)) for p in survivors]
        collisions = [dest for _, dest in dests if dest.exists()]
        if collisions:
            rel = ", ".join(
                sorted(str(d.relative_to(ctx.cv_root)) for d in collisions)[:5]
            )
            raise RuntimeError(
                f"refusing to prune {ctx.old_rel}: {len(collisions)} untracked "
                f"survivor(s) collide with content already archived at "
                f"{ctx.new_rel} ({rel}). Relocating would silently overwrite "
                "already-committed archive content -- resolve the collision "
                "by hand (rename or remove the untracked survivor, or the "
                "conflicting archived file), then re-run to resume."
            )
        for p, dest in dests:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
    shutil.rmtree(stale)


_STEP_PRUNE_SCAFFOLD = CloseStep(
    name="prune-scaffold",
    description=(
        "Remove the empty current/ scaffold git leaves in the main tree after "
        "the archive move; refuses if a tracked file survives"
    ),
    mutating=True,
    precheck=_precheck_prune_scaffold,
    apply=_apply_prune_scaffold,
)


# --- Ordered registry: 0-3, the merge (3b under `local`; 4-7 and 10 under
# `squash`), then 11, 8, 9a, 9 ---

STEPS: list[CloseStep] = [
    _STEP_BACKLOG_CAP_GUARD,  # 0 (must run before every other step)
    _STEP_GIT_MV,            # 1
    _STEP_STATUS_FLIP,       # 1b (must precede git-commit)
    _STEP_INDEX_ROW,         # 2b (must precede git-commit)
    _STEP_MAINTENANCE_PASSES,  # 2c (must precede git-commit)
    _STEP_COMMIT,            # 3
    _STEP_LOCAL_MERGE,       # 3b (close.merge local only)
    _STEP_PUSH,              # 4
    _STEP_PR_CREATE,         # 5
    _STEP_PR_MERGE,          # 6
    _STEP_PULL_MAIN,         # 7
    _STEP_RECONCILE_MAIN,    # 10 (new — runs after pull so squash-merge is visible)
    _STEP_PRUNE_SCAFFOLD,    # 11 (must follow reconcile — main needs the move first)
    _STEP_WORKTREE_REMOVE,   # 8
    _STEP_ARCHIVE_TAG,       # 9a (tag before branch-delete orphans stream commits)
    _STEP_BRANCH_DELETE,     # 9
]


# --- Persisted step cursor ---
# Lets a rerun skip the steps a crashed run already applied. It lives in the
# shared `.git` directory, so it survives `worktree-remove` and the `git mv`,
# and `git status` never sees it.

_CURSOR_DIR_REL = Path("project-workflow") / "close-cursor"

# Redirects the cursor root (test isolation); `cursor_path` always reads it.
CURSOR_ROOT_ENV = "PROJECT_WORKFLOW_CLOSE_CURSOR_ROOT"


def cursor_path(cv_root: Path, project_id: str) -> Path:
    """`<git-common-dir of cv_root>/project-workflow/close-cursor/<id>.json`.

    The root is `$PROJECT_WORKFLOW_CLOSE_CURSOR_ROOT` when set. Raises
    `RepoError` when `cv_root` is not in a git repository and no override is set.
    """
    env = os.environ.get(CURSOR_ROOT_ENV)
    base = Path(env) if env else git_common_dir(cv_root)
    return base / _CURSOR_DIR_REL / f"{project_id}.json"


def load_cursor(cv_root: Path, project_id: str, project_branch: str) -> set[str]:
    """The step names a prior run recorded as done; fail-soft.

    A missing, unreadable or malformed cursor, or one recorded for another
    `project_branch` (a re-cut project), gives the empty set.
    """
    try:
        path = cursor_path(cv_root, project_id)
    except RepoError:
        return set()
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    if data.get("project_branch") != project_branch:
        return set()
    steps = data.get("done_steps")
    if not isinstance(steps, list):
        return set()
    return {s for s in steps if isinstance(s, str)}


def record_step_done(cv_root: Path, project_id: str, project_branch: str,
                      done_steps: set[str]) -> None:
    """Persist the cursor after each step's apply(), atomically (temp + replace)."""
    path = cursor_path(cv_root, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "project_id": project_id,
        "project_branch": project_branch,
        "done_steps": sorted(done_steps),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def clear_cursor(cv_root: Path, project_id: str) -> None:
    """Remove the cursor once every step has landed; never raises on failure."""
    try:
        cursor_path(cv_root, project_id).unlink()
    except (OSError, RepoError):
        pass
