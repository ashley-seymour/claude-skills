"""Scaffold a new project — folder, brief stub, STATUS.md, worktree.

Usage (run from inside the repo; the repo is found from the current directory):
    python <skill>/scripts/bin/new-project.py <slug> [--brief <path>] [--parent <NNNN>]
        [--push] [--closes R17,P16] [--base <ref>] [--no-worktree] [--no-bootstrap]
    # later, at first dispatch -- cut + bootstrap the already-scaffolded project:
    python <skill>/scripts/bin/new-project.py <slug> --resume-id <NNNN>

Runs on the main repo checkout. `<projects>` below is the config's
`projects_dir`, `<base>` its `base_branch` (`.claude/project-workflow.json`,
`lib/config.py`). Steps:
    1. Allocate next id from `<projects>/{current,completed,deferred}/project-*`
       across the working tree, every git ref tip, and every linked worktree.
    2. Create `<projects>/current/project-<NNNN>-<slug>/artifacts`. No
       `streams/`: the project-workflow skill records streams in STATUS.md and
       never writes one. A pre-skill project's existing `streams/` is left as is.
    3. Scaffold `brief.md` (from `--brief` file if given, else the skill's
       `templates/brief-template.md`).
    4. Scaffold `STATUS.md`. The copy committed to the base branch is a REDIRECT
       STUB marked `stub: true` and `status: planned`, naming the
       worktree path, branch and `authoritative_status:` path -- the base branch
       never receives another edit to this file until close, so rendering real
       state into it freezes a lie. The full render of the skill's
       `templates/status-template.md` is written on the worktree side of the
       cut, in step 8. `--closes <ids>` (comma-separated) lands as a `closes:`
       frontmatter key on both copies so a project's backlog claim is recorded
       at scaffold, not asserted at close; the flag absent leaves the key absent.
    5. Add a `current` / `planned` row to the config's `index_file` (matching
       the existing column shape) if one is not already there — idempotent,
       left alone on a resumed scaffold.
    6. Commit `chore(<NNNN>): scaffold project folder`, staging only the new
       project folder and the index. Commits locally; pushes only with `--push`.
       With `--no-worktree` the script stops here: the skill cuts the worktree
       at first dispatch, after the brief is reviewed, by re-running with
       `--resume-id <NNNN>`, which skips steps 1-6 as already done.
    7. Cut project worktree: `git worktree add <repo>-worktrees/<NNNN>-<slug>
       -b project/<NNNN>-<slug> <ref>`, where `<ref>` is `--base` (default:
       the stub's recorded `base:`, else `<base>`) -- explicit so the branch never inherits whatever branch happened
       to be checked out in the main repo.
    8. Inside the worktree, replace the step 4 stub with the real template
       (`status: active`), flip the worktree's index row `planned` -> `active`,
       and commit (the index is staged on every run, so a rerun commits a
       flip that an earlier, rejected commit left behind). The stub must be written on the base branch FIRST and left alone:
       the branch is cut from the scaffold commit, so the stub is the merge base
       and only the branch side ever moves. Writing the template on the base
       branch and stubbing it after the cut would instead move both sides and
       make STATUS.md conflict at close.
    9. Run the config's `bootstrap` argv (`{worktree}` replaced by the
       worktree's absolute path, cwd the main checkout) unless `--no-bootstrap`
       or the key is null. It runs on every run that has a worktree, including
       a resume that finds it already cut, so a failed bootstrap is retried by
       re-running with `--resume-id`; a non-zero exit refuses.

`--closes`, `--parent` and `--base` are recorded in the step 4 stub. A later
`--resume-id` run that does not repeat them reads them back from the stub, so
the split flow (`--no-worktree`, then the cut) carries them into the worktree
STATUS.md, the copy that records them for the project's life.

No `wave-manifest.yaml` is written: the skill contract has none.

Prints next-step instructions. Human-gated: prints intended actions and asks
for confirmation before touching git.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from config import ConfigError, config_for, expand_argv, load_config  # noqa: E402
from streams import CV_ROOT, repo_basename, utc_iso, worktree_path  # noqa: E402

import yaml  # noqa: E402


# The templates ship with the skill, beside this script's `scripts/` folder.
TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "templates"
STATUS_TEMPLATE = TEMPLATES_DIR / "status-template.md"
BRIEF_TEMPLATE = TEMPLATES_DIR / "brief-template.md"
# None: the index is the config's `index_file` under the CURRENT `CV_ROOT`,
# resolved on every call (`_readme_path`), so a test that patches only
# `CV_ROOT` is honoured. A test may also set `README_PATH` to a path, which
# then wins.
README_PATH: Path | None = None


def _cfg():
    """The config of the main checkout (`CV_ROOT`), read on every call.

    The scaffold (folder, index row, commit) lands on the main checkout, so
    main's config governs it; the bootstrap step reads the worktree's first.
    """
    return load_config(CV_ROOT)


def _pdir() -> str:
    """The config's `projects_dir`."""
    return _cfg().projects_dir


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd or CV_ROOT, check=check, capture_output=True, text=True)


def _ids_on_disk(root: Path) -> list[int]:
    ids: list[int] = []
    for bucket in ("current", "completed", "deferred"):
        try:
            names = [p.name for p in (root / _pdir() / bucket).glob("project-*-*")]
        except OSError:
            continue
        for name in names:
            m = re.match(r"^project-(\d{4})-", name)
            if m:
                ids.append(int(m.group(1)))
    return ids


def _ids_in_refs() -> list[int]:
    ids: list[int] = []
    try:
        r = run(["git", "-C", str(CV_ROOT), "for-each-ref", "--format=%(refname)",
                 "refs/heads", "refs/remotes"], check=False)
        if r.returncode != 0:
            return ids
        pdir = _pdir()
        for ref in r.stdout.split():
            t = run(["git", "-C", str(CV_ROOT), "ls-tree", "--name-only", ref,
                     f"{pdir}/current/", f"{pdir}/completed/",
                     f"{pdir}/deferred/"], check=False)
            if t.returncode == 0:
                ids.extend(int(n) for n in re.findall(r"/project-(\d{4})-", t.stdout))
    except (OSError, subprocess.SubprocessError):
        pass
    return ids


def _ids_in_linked_worktrees() -> list[int]:
    ids: list[int] = []
    try:
        r = run(["git", "-C", str(CV_ROOT), "worktree", "list", "--porcelain"], check=False)
        if r.returncode != 0:
            return ids
        for line in r.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            path = Path(line[len("worktree "):].strip())
            if path == CV_ROOT or not path.is_dir():
                continue
            ids.extend(_ids_on_disk(path))
    except (OSError, subprocess.SubprocessError):
        pass
    return ids


def _ids_in_history() -> list[int]:
    """Every id that ever appeared in committed history, deleted ones included.

    The other sources read present state, so an archived-then-deleted project
    is invisible to them; only history remembers it. `--reflog` also catches
    a deleted branch's commits while its reflog lives.
    """
    ids: list[int] = []
    try:
        pdir = _pdir()
        r = run(["git", "-C", str(CV_ROOT), "log", "--all", "--reflog",
                 "--pretty=format:", "--name-only", "--diff-filter=A", "--",
                 f"{pdir}/current/", f"{pdir}/completed/",
                 f"{pdir}/deferred/"], check=False)
        if r.returncode != 0:
            return ids
        ids.extend(int(n) for n in re.findall(r"/project-(\d{4})-", r.stdout))
    except (OSError, subprocess.SubprocessError):
        pass
    return ids


def _find_project_id_for_slug_in(root: Path, slug: str) -> str | None:
    """Return the project id already scaffolded for `slug` under `root`, or
    None. Anchored on the full folder name (`re.fullmatch`) so a slug that is
    merely a suffix of another slug (`foo` vs `project-0001-bar-foo`) cannot
    false-positive.
    """
    pattern = re.compile(rf"^project-(\d{{4}})-{re.escape(slug)}$")
    for bucket in ("current", "completed", "deferred"):
        base = root / _pdir() / bucket
        try:
            names = [d.name for d in base.glob(f"project-*-{slug}")]
        except OSError:
            continue
        for name in names:
            m = pattern.fullmatch(name)
            if m:
                return m.group(1)
    return None


def _find_project_id_for_slug(slug: str) -> str | None:
    """Return the project id already scaffolded for `slug`, or None.

    Scans CV_ROOT plus every linked worktree: a hook-rejected scaffold leaves
    the folder only in the worktree, and between close and merge the folder
    sits under `completed/` in the worktree but `current/` on main. The same
    slug on an unchecked-out sibling ref is a separate project, out of scope.
    """
    dup_id = _find_project_id_for_slug_in(CV_ROOT, slug)
    if dup_id is not None:
        return dup_id
    try:
        r = run(["git", "-C", str(CV_ROOT), "worktree", "list", "--porcelain"], check=False)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            path = Path(line[len("worktree "):].strip())
            if path == CV_ROOT or not path.is_dir():
                continue
            dup_id = _find_project_id_for_slug_in(path, slug)
            if dup_id is not None:
                return dup_id
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def next_project_id() -> str:
    """Return the next zero-padded id, above every id this repo can see.

    An id must never be reissued, so four sources are unioned: the checkout,
    every ref tip (an unmerged branch cut elsewhere), every linked worktree (a
    hook-rejected scaffold) and history (an archived-then-deleted project).
    A source git cannot answer degrades to the filesystem scan.
    """
    ids = (_ids_on_disk(CV_ROOT) + _ids_in_refs()
           + _ids_in_linked_worktrees() + _ids_in_history())
    return f"{(max(ids, default=0) + 1):04d}"


def render_status_scaffold(project_id: str, slug: str, closes: list[str] | None = None,
                           parent: str | None = None, worktree: Path | None = None,
                           branch: str | None = None) -> str:
    """The worktree STATUS.md, in the project-workflow skill's schema.

    Frontmatter `project`, `name`, `status`, `current_wave`, `worktree`,
    `branch`, `deadlines`; then `## Resume`, `## Wave 1`, `## Log`. It carries no `waves:` map and no `streams/` wording: the
    skill records waves as `## Wave N` sections and never writes `streams/`.
    `external_yield:` / `external_yield_waiver:` ship blank with a comment,
    because close-project.py's `close:external_yield` gate FAILs until one is
    filled. `workflow: project-workflow` is written because
    close_steps.py's index-row step refuses a STATUS without a `workflow:`
    key, and an empty `## Known gaps` table is kept.
    """
    if STATUS_TEMPLATE.is_file():
        template = STATUS_TEMPLATE.read_text(encoding="utf-8")
    else:
        template = (
            "---\n"
            "project: \"{project_id}\"\n"
            "name: {slug}\n"
            "status: active\n"
            "current_wave: 1\n"
            "worktree: {worktree}\n"
            "branch: {branch}\n"
            "deadlines: none\n"
            "# close-project.py FAILs `close:external_yield` while both are blank:\n"
            "# name an artifact consumed OUTSIDE this framework, or set `none`\n"
            "# and give the reason in external_yield_waiver:.\n"
            "external_yield: \"\"\n"
            "external_yield_waiver: \"\"\n"
            "# close_steps.py's index-row step refuses the close without it.\n"
            "workflow: project-workflow\n"
            "last_updated: {now}\n"
            "last_updated_by: new-project.py\n"
            "---\n\n"
            "# STATUS — {slug} (project {project_id})\n\n"
            "## Resume\n\n"
            "- state: <last completed step, and how it was verified>\n"
            "- next: /project-workflow {project_id} wave 1\n"
            "- decisions: <open decision blocking the next step, or `none`>\n\n"
            "## Wave 1 — <theme>  (status: pending)\n\n"
            "## Known gaps\n\n"
            "| Item | Disposition |\n"
            "|---|---|\n\n"
            "## Log\n"
        )
    wt_text = worktree.as_posix() if worktree is not None else "<absolute path>"
    text = template.format(project_id=project_id, slug=slug, now=utc_iso(),
                           worktree=wt_text,
                           branch=branch or f"project/{project_id}-{slug}")
    return _splice_closes(text, closes, parent)


def _splice_closes(text: str, closes: list[str] | None,
                   parent: str | None = None) -> str:
    """A3: record the backlog pickup at scaffold time rather than asserting it
    at close. Spliced into the frontmatter (before its closing `---`) rather
    than added to the .format() placeholders -- the template's frontmatter is
    fixed text, not built for an optional key.

    `--parent` rides the same splice: it was parsed and then read nowhere, so
    a project scaffolded with a parent shipped a STATUS.md that never said so.
    """
    extra: list[str] = []
    if closes:
        extra.append("closes: [" + ", ".join(closes) + "]")
    if parent:
        extra.append(f'parent_project: "{parent}"')
    if not extra:
        return text
    head, sep, rest = text.partition("\n---\n")
    if sep:
        return head + "\n" + "\n".join(extra) + sep + rest
    return text


# Marks a file as a `main`-side redirect stub, so step 8 can tell "still the
# stub we wrote" from "real project state" and never clobber the latter on a
# resumed scaffold. `stub` is the project-workflow skill's key (Create step 5,
# Resume); `scaffold_stub` is what this script wrote before that key existed
# and is still recognised on read so an older scaffold resumes cleanly.
STUB_KEY = "stub"
LEGACY_STUB_KEYS = ("scaffold_stub",)
_FM_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def render_status_stub(project_id: str, slug: str, worktree: Path, branch: str,
                        closes: list[str] | None = None,
                        parent: str | None = None,
                        base: str | None = None) -> str:
    """Render `main`'s STATUS.md: a redirect to the worktree, not project state.

    The scaffold commit lands this on `main` and the worktree is then cut from
    it, so every later wave, stream and progress edit happens on the branch and
    `main` keeps this exact text until the close merge. Rendering the real
    template here froze `current_wave: 1` / `waves: {}` on `main` for the whole
    life of the project: a session read main's copy, concluded the project was
    an unstarted stub, and wrote a stream brief against that premise while the
    worktree was on wave 3 with 8 of 9 streams merged.

    Only fields that stay true under that freeze are kept. `status:` is
    `planned`, the state at scaffold: the cut (step 8) writes `active` into the
    worktree copy only, and this copy stays `planned` until the close merge.
    No script reads it (the close's status-flip step reads the worktree copy).
    `closes:` stays because it is authored input, not state. The wave keys are
    omitted rather than frozen: a reader who cannot find them opens the worktree, whereas a reader
    who sees `current_wave: 1` believes it.

    `base:` (the ref the branch is cut from) is authored input too. With
    `--no-worktree` the cut happens on a later `--resume-id` run, which reads
    `base:`, `closes:` and `parent_project:` back from this stub when its own
    CLI does not repeat them (`_stub_inputs`), so the split flow loses none.

    No markdown links -- a link checker resolves link targets against the
    checked-out branch, and this file's whole purpose is to point outside it.
    """
    rel = f"{_pdir()}/current/project-{project_id}-{slug}"
    authoritative = f"{worktree.as_posix()}/{rel}/STATUS.md"
    text = (
        "---\n"
        f'project: "{project_id}"\n'
        f"name: {slug}\n"
        "workflow: project-workflow\n"
        "status: planned\n"
        f"{STUB_KEY}: true\n"
        f"authoritative_status: {authoritative}\n"
        f"worktree: {worktree.as_posix()}\n"
        f"branch: {branch}\n"
        # Quoted, like `parent_project:`: unquoted, `--base 0017` reads back
        # through yaml.safe_load as the octal int 15 and `--base 1.10` as 1.1.
        + (f"base: {_yaml_dq(base)}\n" if base else "")
        + f"last_updated: {utc_iso()}\n"
        "last_updated_by: new-project.py\n"
        # Named here so a reader of the stub learns the
        # close gate exists, but filled only in the worktree copy, which is
        # the one close-project.py reads. A blank key here would be frozen.
        "# external_yield: / external_yield_waiver: are filled in the\n"
        "# authoritative_status copy, never here; close:external_yield reads it.\n"
        "---\n"
        "\n"
        f"# STATUS — {slug} (project {project_id})\n"
        "\n"
        "Do not plan from this file.\n"
        "\n"
        "This copy on `main` is a scaffold stub, not project state. It is written\n"
        "once, at scaffold, and is never updated again while the project runs.\n"
        "\n"
        "Every wave, stream and progress edit lands on the project branch and\n"
        "reaches `main` only when the project closes and that branch merges. Do\n"
        "not read project state, wave number or stream count from this file.\n"
        "\n"
        "The live STATUS.md is on branch:\n"
        "\n"
        f"    {branch}\n"
        "\n"
        "checked out at:\n"
        "\n"
        f"    {authoritative}\n"
    )
    return _splice_closes(text, closes, parent)


def _is_scaffold_stub(path: Path) -> bool:
    """True iff `path` still holds a stub this script wrote.

    Gates step 8 so a resumed scaffold upgrades an untouched stub but never
    overwrites real project state with a fresh template. Both the current
    `stub: true` and the older `scaffold_stub: true` count; the value must
    be the YAML boolean `true`, so a real STATUS.md that merely mentions the
    word is never taken for a stub.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    m = _FM_RE.match(text)
    if not m:
        return False
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return False
    if not isinstance(data, dict):
        return False
    return any(data.get(k) is True for k in (STUB_KEY, *LEGACY_STUB_KEYS))


def _stub_inputs(path: Path) -> dict:
    """The authored inputs recorded in `main`'s stub: `closes`, `parent`, `base`.

    The split flow (`--no-worktree` at Create, `--resume-id` at first dispatch)
    records `--closes`, `--parent` and `--base` in the stub on the first run.
    The cut run renders the worktree STATUS.md, which is the copy that records
    `closes:` for the life of the project. Without this read,
    a cut run that does not repeat the flags silently drops the backlog claim.
    Returns only the keys present; empty when `path` is not a stub.
    """
    if not _is_scaffold_stub(path):
        return {}
    m = _FM_RE.match(path.read_text(encoding="utf-8"))
    data = yaml.safe_load(m.group(1)) if m else None
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    closes = data.get("closes")
    if isinstance(closes, list) and closes:
        out["closes"] = [str(c) for c in closes]
    for key, name in (("parent_project", "parent"), ("base", "base")):
        value = _scalar_as_written(m.group(1), key, data.get(key))
        if value:
            out[name] = value
    return out


def _yaml_dq(value: str) -> str:
    """`value` as a YAML double-quoted scalar, so it always reads back a str."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _scalar_as_written(frontmatter: str, key: str, parsed: object) -> str | None:
    """The frontmatter value of `key` as a string, exactly as written.

    A str from yaml.safe_load is used as is. Anything else (a stub written
    before `base:` was quoted, or a hand edit) is re-read from the raw line,
    because `str()` of the parsed value is lossy: an unquoted `0017` parses as
    the octal int 15 and `1.10` as the float 1.1.
    """
    if parsed is None or parsed == "":
        return None
    if isinstance(parsed, str):
        return parsed
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*?)[ \t]*(?:#.*)?$", frontmatter,
                  re.MULTILINE)
    raw = m.group(1) if m else ""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        raw = raw[1:-1]
    return raw or str(parsed)


# The creation verb writes the index row (config `index_file`); the close
# verb's `_precheck_index_row` (lib/close_steps.py) only writes it if absent,
# so its write is a repair path.
_README_ROW_RE = re.compile(r"^\|[ \t]*\d{4}[ \t]*\|.*\|[ \t]*$", re.MULTILINE)
# The separator line under an `| Id | ...` header: the insertion point when the
# table has no data row yet. Anchored on the header so another table's
# separator in the same file is never used.
_README_SEP_RE = re.compile(
    r"^\|[ \t]*Id[ \t]*\|.*\|[ \t]*\r?\n(\|(?:[ \t]*:?-+:?[ \t]*\|)+)[ \t]*$",
    re.MULTILINE)


def _readme_row_exists(text: str, project_id: str) -> bool:
    pattern = re.compile(rf"^\|[ \t]*{re.escape(project_id)}[ \t]*\|", re.MULTILINE)
    return pattern.search(text) is not None


def _readme_path() -> Path:
    """The index file, resolved on every call, not once at import.

    An explicit `README_PATH` override wins; otherwise the config's
    `index_file` under the CURRENT `CV_ROOT`."""
    if README_PATH is not None:
        return README_PATH
    return CV_ROOT / _cfg().index_file


def append_readme_index_row(project_id: str, slug: str) -> bool:
    """Append the project's index row (config `index_file`) if missing.
    Returns True if the file changed.

    A missing README is a no-op, not a refusal -- consumer repos need not
    carry one (same contract as `close_steps._precheck_index_row`). Column
    count is READ OFF the last existing data row rather than assumed: this
    index has been 4 wide (Id | Project | Where | Status) and 6 wide (plus
    Artifacts | Outstanding) at different times, and a row of the wrong
    width breaks the render for every later reader. Inserted after the last
    existing data row so a resumed scaffold with rows added since stays
    append-only and idempotent.

    A table with a header and separator but no data row yet (a repo's first
    project) gets its row right after the separator, with the column count
    read off the header. It used to return False silently.
    """
    readme_path = _readme_path()
    if not readme_path.is_file():
        return False
    readme_text = readme_path.read_text(encoding="utf-8")
    if _readme_row_exists(readme_text, project_id):
        return False
    matches = list(_README_ROW_RE.finditer(readme_text))
    if matches:
        ncols = len(matches[-1].group(0).strip().strip("|").split("|"))
        insert_at = matches[-1].end()
    else:
        sep = _README_SEP_RE.search(readme_text)
        if sep is None:
            # No data row and no `| Id |` header table.
            # Say so rather than leave the project out of the index silently.
            print(f"WARN: {readme_path} has no project index table (no "
                  f"`| NNNN |` row and no `| Id | ...` header with a separator); "
                  f"no index row added for {project_id} -- add it by hand.",
                  file=sys.stderr)
            return False
        ncols = len(sep.group(1).strip().strip("|").split("|"))
        insert_at = sep.end()
    # `planned` at scaffold; the cut flips the worktree's row to `active`
    # (`flip_index_row_active`), and the close flips it to `completed`.
    cells = [project_id, slug, "current", "planned", "0", "-"][:ncols]
    cells += ["-"] * (ncols - len(cells))
    row = "| " + " | ".join(cells) + " |"
    readme_path.write_text(readme_text[:insert_at] + "\n" + row + readme_text[insert_at:],
                            encoding="utf-8")
    return True


def flip_index_row_active(readme_path: Path, project_id: str) -> bool:
    """At the cut, rewrite this project's index row Status cell `planned` ->
    `active` in `readme_path` (the worktree's copy). Returns True if changed.

    The Status cell is the fourth, the column `append_readme_index_row`
    writes it to. Any other value is left alone, so a resume is a no-op and a
    hand-edited or pre-`planned` row is never overwritten. A missing file or
    row is a no-op too, the same contract as the append.
    """
    if not readme_path.is_file():
        return False
    text = readme_path.read_text(encoding="utf-8")
    m = re.search(rf"^\|[ \t]*{re.escape(project_id)}[ \t]*\|[^\r\n]*",
                  text, re.MULTILINE)
    if m is None:
        return False
    cells = m.group(0).rstrip().strip("|").split("|")
    if len(cells) < 4 or cells[3].strip() != "planned":
        return False
    cells[3] = cells[3].replace("planned", "active")
    row = "|" + "|".join(cells) + "|"
    readme_path.write_text(text[:m.start()] + row + text[m.end():], encoding="utf-8")
    return True


def _resolve_existing_slug(project_id: str) -> str | None:
    """If a folder for `project_id` exists in current/, return its slug (conflict guard)."""
    for path in (CV_ROOT / _pdir() / "current").glob(f"project-{project_id}-*"):
        m = re.match(rf"^project-{project_id}-(.+)$", path.name)
        if m:
            return m.group(1)
    return None


def _worktree_exists(worktree: Path) -> bool:
    """True iff git already has a worktree registered at `worktree`.

    Compares resolved Paths, never strings: `git worktree list --porcelain`
    prints POSIX separators even on Windows, where `str(Path)` has backslashes.
    """
    r = run(["git", "-C", str(CV_ROOT), "worktree", "list", "--porcelain"], check=False)
    if r.returncode != 0:
        return False
    target = worktree.resolve()
    for line in r.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            if Path(line[len("worktree "):].strip()).resolve() == target:
                return True
        except OSError:
            continue
    return False


def _branch_exists(branch: str) -> bool:
    r = run(["git", "-C", str(CV_ROOT), "rev-parse", "--verify", f"refs/heads/{branch}"],
            check=False)
    return r.returncode == 0


def _folder_tracked_on_head(folder_rel: str) -> bool:
    """True iff `folder_rel` has at least one blob tracked on HEAD.

    `git commit` exit 1 is either an empty commit (safe) or a pre-commit hook
    rejection (folder never landed), so ask HEAD rather than the exit code.
    """
    r = run(["git", "-C", str(CV_ROOT), "ls-tree", "-r", "--name-only", "HEAD",
             "--", folder_rel], check=False)
    return r.returncode == 0 and bool(r.stdout.strip())


def _commit_scaffold(project_id: str, folder_rel: str, push: bool,
                      readme_changed: bool = False) -> int:
    """Step 6 -- commit the scaffold folder + README index row on the current
    checkout (main). Returns 0 on success (including a genuine no-op commit),
    non-zero on a rejected commit. Never cuts a worktree -- that decision is
    the caller's, and only once this returns 0, so a rejected commit can
    never leave a worktree cut from a `main` that never received the folder.
    """
    subject = f"chore({project_id}): scaffold project folder"
    # `git add` refuses the whole invocation on a pathspec that matches nothing,
    # so the README is staged only when this run actually changed it.
    add_paths = [folder_rel]
    if readme_changed:
        index = _readme_path()
        try:
            add_paths.append(index.resolve().relative_to(CV_ROOT.resolve()).as_posix())
        except ValueError:
            add_paths.append(str(index))
    try:
        run(["git", "-C", str(CV_ROOT), "add", "--", *add_paths])
        r = run(["git", "-C", str(CV_ROOT), "commit", "-m", subject], check=False)
    except subprocess.CalledProcessError as exc:
        print(f"commit/push failed: {exc.stderr}", file=sys.stderr)
        print(f"The scaffold did NOT land on main. Retry with --resume-id "
              f"{project_id} once the block clears.", file=sys.stderr)
        return 1
    if r.returncode not in (0, 1):
        print(f"commit failed: {r.stderr}", file=sys.stderr)
        print(f"The scaffold did NOT land on main. Retry with --resume-id "
              f"{project_id} once the block clears.", file=sys.stderr)
        return 1
    if not _folder_tracked_on_head(folder_rel):
        print("scaffold commit was rejected -- the folder never landed on HEAD.",
              file=sys.stderr)
        if r.stdout.strip():
            print(r.stdout, file=sys.stderr)
        if r.stderr.strip():
            print(r.stderr, file=sys.stderr)
        print(f"The scaffold did NOT land on main. Retry with --resume-id "
              f"{project_id} once the block clears.", file=sys.stderr)
        return 1
    if push:
        run(["git", "push"], check=False)
    return 0


def _project_row_committed(project_id: str, slug: str) -> bool:
    """True iff HEAD (not the index) tracks this project's folder, so a
    merely-staged folder never takes the skip-commit path."""
    folder_rel = f"{_pdir()}/current/project-{project_id}-{slug}"
    return _folder_tracked_on_head(folder_rel)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("slug", help="short-kebab-case name, e.g. token-efficiency-audit")
    p.add_argument("--brief", type=Path, default=None, help="pre-authored brief to copy in")
    p.add_argument("--parent", default=None,
                   help="parent project id; recorded as `parent_project:` in "
                        "STATUS.md frontmatter (both copies)")
    p.add_argument("--yes", action="store_true", help="skip confirmation")
    p.add_argument("--push", action="store_true",
                   help="push the scaffold commit (default: commit locally, do not push)")
    p.add_argument("--resume-id", default=None,
                   help="resume an existing partial scaffold with this id")
    p.add_argument("--closes", default=None,
                   help="comma-separated backlog row ids this project claims "
                        "(e.g. R17,P16); recorded in STATUS.md frontmatter, not "
                        "validated against the backlog")
    p.add_argument("--base", default=None,
                   help="ref to cut the project branch/worktree from (default: "
                        "the `base:` recorded in the stub, else the config's "
                        "`base_branch`)")
    p.add_argument("--no-worktree", action="store_true",
                   help="scaffold and commit on the base branch only; cut the "
                        "worktree later, at first dispatch, with "
                        "`<slug> --resume-id <NNNN>`")
    p.add_argument("--no-bootstrap", action="store_true",
                   help="do not run the config's `bootstrap` argv on the worktree "
                        "(by default it runs on every run that has a worktree, "
                        "so a resume retries a failed bootstrap)")
    args = p.parse_args(argv[1:])

    try:
        cfg = _cfg()
    except ConfigError as exc:
        print(f"refuse: {exc}", file=sys.stderr)
        return 1

    if not re.match(r"^[a-z][a-z0-9-]{1,50}$", args.slug):
        print("refuse: slug must be short-kebab-case (a-z, 0-9, -).", file=sys.stderr)
        return 1

    closes: list[str] | None = None
    if args.closes is not None:
        closes = [t.strip() for t in args.closes.split(",")]
        if not closes or any(not t for t in closes):
            print("refuse: --closes must be a comma-separated list of "
                  "non-empty ids (e.g. R17,P16).", file=sys.stderr)
            return 1

    # Same-slug re-run without an explicit --resume-id must refuse, not
    # silently allocate a fresh id (a repeat `new-project.py <slug>` burned an
    # id this way -- next_project_id() has no idea the slug was already
    # scaffolded under a lower id).
    if not args.resume_id:
        dup_id = _find_project_id_for_slug(args.slug)
        if dup_id is not None:
            print(f"refuse: slug {args.slug!r} already scaffolded as project "
                  f"{dup_id} -- pass --resume-id {dup_id} to resume it.",
                  file=sys.stderr)
            return 1

    project_id = args.resume_id or next_project_id()
    folder_rel = f"{cfg.projects_dir}/current/project-{project_id}-{args.slug}"
    folder = CV_ROOT / folder_rel
    project_worktree = worktree_path(repo_basename(), project_id, args.slug, CV_ROOT).resolve()
    project_branch = f"project/{project_id}-{args.slug}"

    # Conflict guard: same id, different slug.
    existing_slug = _resolve_existing_slug(project_id)
    if existing_slug and existing_slug != args.slug:
        print(f"refuse: project {project_id} already exists as slug "
              f"{existing_slug!r}; refusing to relabel.", file=sys.stderr)
        return 1

    resuming = folder.exists()
    if resuming and not args.resume_id:
        print(f"NOTE: folder {folder} exists — resuming.")

    # Authored inputs: the CLI wins; otherwise take what an earlier run
    # recorded in main's stub. This is what carries `--closes`, `--parent` and
    # `--base` from a `--no-worktree` scaffold into the later cut run, whose
    # worktree STATUS.md is the copy that records them for the project's life.
    recorded = _stub_inputs(folder / "STATUS.md")
    if closes is None:
        closes = recorded.get("closes")
    parent = args.parent or recorded.get("parent")
    base = args.base or recorded.get("base") or cfg.base_branch

    action = "RESUME" if resuming else "CREATE"
    print(f"About to {action} project {project_id}:")
    print(f"  Folder:    {folder}")
    if args.no_worktree:
        print(f"  Worktree:  (not cut: --no-worktree) {project_worktree}")
    else:
        print(f"  Worktree:  {project_worktree}")
        print(f"  Branch:    {project_branch} (from {base})")
    if closes or parent:
        print(f"  Closes:    {', '.join(closes or []) or '-'}   Parent: {parent or '-'}")
    print(f"  Brief src: {args.brief or BRIEF_TEMPLATE} -> {folder}/brief.md")

    if not args.yes:
        reply = input("Proceed? [y/N] ").strip().lower()
        if reply not in {"y", "yes"}:
            print("aborted.")
            return 0

    # Step 1 (id allocation) ran above: `--resume-id` or `next_project_id()`.

    # Step 2 — folder + artifacts/ (each guarded). No `streams/`: the skill
    # records streams in STATUS.md and never writes one. A legacy project's
    # existing streams/ is left alone.
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "artifacts").mkdir(exist_ok=True)

    # Step 3 — brief.md: never overwrite hand-authored content.
    brief_path = folder / "brief.md"
    brief_needs_authoring = False
    if not brief_path.is_file():
        brief_needs_authoring = not (args.brief and args.brief.is_file())
        if args.brief and args.brief.is_file():
            brief_path.write_text(args.brief.read_text(encoding="utf-8"), encoding="utf-8")
        elif BRIEF_TEMPLATE.is_file():
            # The header above the rule is scaffold guidance for the template
            # itself, not part of the project's brief; only what follows the
            # rule is copied.
            body = BRIEF_TEMPLATE.read_text(encoding="utf-8")
            _, sep, below = body.partition("\u2550" * 8)
            if sep:
                body = below.split("\n", 1)[-1].lstrip("\n")
            brief_path.write_text(body, encoding="utf-8")
        else:
            brief_path.write_text(f"# Brief — {args.slug}\n\nTODO: author.\n", encoding="utf-8")

    # Step 4 — STATUS.md. This is the copy that stays on `main`, so it gets the
    # redirect stub; step 8 below writes the real template inside the worktree.
    # --closes, --parent and --base land here (frontmatter keys), never on a
    # resumed scaffold whose STATUS.md already exists.
    status_path = folder / "STATUS.md"
    if not status_path.is_file():
        status_path.write_text(
            render_status_stub(project_id, args.slug, project_worktree,
                               project_branch, closes=closes,
                               parent=parent, base=base),
            encoding="utf-8")

    # Step 5 — the index row (config `index_file`). No wave-manifest.yaml.
    readme_changed = append_readme_index_row(project_id, args.slug)

    # Step 6 — commit on the current checkout (main) if anything is uncommitted.
    if not _project_row_committed(project_id, args.slug) or readme_changed:
        rc = _commit_scaffold(project_id, folder_rel, args.push, readme_changed=readme_changed)
        if rc != 0:
            # Step 7 (worktree cut) MUST NOT run after a rejected scaffold
            # commit -- that is the state that made this defect expensive.
            return rc
    else:
        print("scaffold already committed — skipping git add/commit.")

    # Scaffold-only: the skill cuts the worktree at first dispatch, after the
    # brief has been reviewed. The stub on `main` already names the worktree
    # path and branch the later cut will use (both are derived from id + slug),
    # and records --closes / --parent / --base, which the cut run reads back.
    if args.no_worktree:
        print()
        print("=" * 72)
        print(f"{'RESUMED' if resuming else 'CREATED'} (no worktree): project "
              f"{project_id} ({args.slug})")
        print("=" * 72)
        if base == cfg.base_branch:
            print(f"  # 1) author {folder_rel}/brief.md and commit it on {base}")
        else:
            # The cut branches from `base`, so the brief has to be committed
            # somewhere that ref can reach, or the worktree never sees it.
            print(f"  # 1) author {folder_rel}/brief.md and commit it where "
                  f"{base} can reach it (the branch is cut from {base}, not "
                  f"{cfg.base_branch})")
        print(f"  # 2) at first dispatch, cut + bootstrap the worktree "
              f"(closes, parent and base are read back from the stub):")
        print(f"  python {Path(__file__).resolve().as_posix()} {args.slug} "
              f"--resume-id {project_id}")
        return 0

    # Step 7 — project worktree (skip if worktree already registered).
    cut_now = False
    if _worktree_exists(project_worktree):
        print(f"worktree exists — skipping: {project_worktree}")
    else:
        cut_now = True
        if _branch_exists(project_branch):
            r = run(["git", "-C", str(CV_ROOT), "worktree", "add",
                     str(project_worktree), project_branch], check=False)
        else:
            # Explicit base ref (default `base_branch`) -- without one, `git worktree
            # add -b` cuts from whatever the main checkout happens to have
            # checked out, silently inheriting a stray branch (brief's Method,
            # "Worktree base").
            r = run(["git", "-C", str(CV_ROOT), "worktree", "add",
                     str(project_worktree), "-b", project_branch, base], check=False)
        if r.returncode != 0:
            print(f"worktree add failed: {r.stderr}", file=sys.stderr)
            print("The scaffold landed on main; retry with --resume-id "
                  f"{project_id} once the block clears.", file=sys.stderr)
            return 1

    # Step 8 — upgrade the stub to the real template INSIDE the worktree, then
    # commit (also tracking files hand-added after step 6). Main's stub is the
    # merge base and only the branch side moves (module docstring, step 8).
    if _worktree_exists(project_worktree):
        wt_folder = project_worktree / folder_rel
        wt_status = wt_folder / "STATUS.md"
        if _is_scaffold_stub(wt_status):
            wt_status.write_text(
                render_status_scaffold(project_id, args.slug, closes=closes,
                                       parent=parent, worktree=project_worktree,
                                       branch=project_branch),
                encoding="utf-8")
        # The worktree's index row goes `planned` -> `active` with it; main's
        # row stays `planned`, like main's stub, until the close merge. The
        # index is staged whenever the worktree has one, not only when this
        # run flipped it: a rerun after a rejected init commit finds the row
        # already `active` (the flip is a no-op) but still uncommitted.
        # `git add` of an unchanged file stages nothing.
        wt_paths = [folder_rel]
        wt_index = project_worktree / cfg.index_file
        flip_index_row_active(wt_index, project_id)
        if wt_index.is_file():
            wt_paths.append(cfg.index_file)
        run(["git", "-C", str(project_worktree), "add", "--", *wt_paths], check=False)
        run(["git", "-C", str(project_worktree), "commit", "-m",
             f"chore({project_id}): worktree init"], check=False)

    # Step 9 — the config's `bootstrap` argv, on every run that has a worktree,
    # not only the run that cut it: a resume is how a failed bootstrap gets
    # retried, so the repo's bootstrap must be idempotent. The argv comes from
    # the worktree's own config when it carries one, else from main's.
    if not args.no_bootstrap:
        try:
            cfg = config_for(CV_ROOT, project_worktree)
        except ConfigError as exc:
            print(f"refuse: {exc}", file=sys.stderr)
            return 1
        if cfg.bootstrap is None:
            print("bootstrap: none configured (`bootstrap` in "
                  ".claude/project-workflow.json) — skipped")
        else:
            argv = expand_argv(cfg.bootstrap, project_worktree)
            print(f"bootstrapping {project_worktree} ...", flush=True)
            rc = _run_bootstrap(argv)
            if rc != 0:
                print(f"bootstrap failed (exit {rc}).", file=sys.stderr)
                print(f"The worktree is cut and initialised; retry with "
                      f"`new-project.py {args.slug} --resume-id {project_id}` "
                      f"or by hand, from {CV_ROOT.as_posix()}: {' '.join(argv)}",
                      file=sys.stderr)
                return 1

    print()
    print("=" * 72)
    verb = "CUT" if cut_now else "RESUMED"
    print(f"{verb}: project {project_id} ({args.slug}) -> {project_worktree}")
    print("=" * 72)
    if brief_needs_authoring:
        # One-shot run (no --no-worktree, no --brief): the brief is still the
        # template, so it has to be authored before anything is dispatched.
        print(f"  # 1) author {folder_rel}/brief.md in the worktree")
        print("  # 2) dispatch wave 1 via the /project-workflow skill")
    else:
        print("  # next: dispatch wave 1 via the /project-workflow skill")
    return 0


def _run_bootstrap(argv: list[str]) -> int:
    """Run the expanded bootstrap argv from the main checkout, streaming output.

    Not captured: a bootstrap can take minutes and its own progress lines are
    the operator's only signal. A command that cannot start is exit 127.
    """
    try:
        return subprocess.run(argv, cwd=CV_ROOT, check=False).returncode
    except OSError as exc:
        print(f"bootstrap could not start: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
