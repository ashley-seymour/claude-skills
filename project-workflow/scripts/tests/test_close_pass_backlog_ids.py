"""The `backlog-id-collisions` close pass.

The pass exists because two projects can each allocate the same F-ids on
branches that cannot see each other, and git merges both tables without a conflict.
The test that matters is `test_catches_concurrent_allocations` below -- checking
the closing worktree alone reproduces the original blind spot exactly.

The pass reads the config's `backlog_file` (default `my-projects/backlog.md`)
in both trees plus every backlog row in git history (`git log -p --all` over a
`backlog*` glob) and the file at the branch point (`git merge-base
<base_branch> HEAD`), so each fixture's main checkout is a real git repo on
`main` and its branch a real worktree cut from it. Retired rows are seeded
through another `backlog*` file name, `my-projects/backlog-old.md`.

`close_passes` is loaded by path: the skill's `scripts/lib/` is not an
importable package, and the pass contract requires every pass to be runnable
against a synthetic `CloseCtx` with no network.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_LIB = ROOT / "scripts" / "lib"


@pytest.fixture(scope="module")
def pass_mod():
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    path = _LIB / "close_passes" / "backlog_ids.py"
    spec = importlib.util.spec_from_file_location("_backlog_ids_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class FakeCtx:
    """Only the two attributes this pass reads."""
    project_worktree: Path
    cv_root: Path


_HEADER = "| Id | Idea | Sketch | Effort | Origin |\n|---|---|---|---|---|\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(path: Path) -> Path:
    """A git repo on branch `main` with one seed commit."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    _commit_all(path, "seed")
    return path


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _cut_branch(main: Path, branch: Path) -> Path:
    """A project worktree at `branch`, on a new branch cut from main's HEAD.

    Rows main commits before this call are inherited by the branch (they are
    in the merge-base); rows committed after it are not.
    """
    _git(main, "worktree", "add", "-q", "-b", "project/0099-test",
         str(branch), "main")
    return branch


def _write_backlog(root: Path, rows: str, name: str = "backlog.md") -> None:
    d = root / "my-projects"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"# Backlog\n\n{_HEADER}{rows}", encoding="utf-8")


def _row(fid: str, idea: str) -> str:
    return f"| **{fid}** | {idea} | some sketch | S | origin |\n"


def _trees(tmp_path: Path) -> tuple[Path, Path]:
    """(main, branch): main is a git repo, branch a worktree cut from its seed
    commit, so the merge-base carries no backlog."""
    main = _repo(tmp_path / "main")
    return main, _cut_branch(main, tmp_path / "branch")


def _run(pass_mod, branch: Path, main: Path):
    return pass_mod.PASS.run(FakeCtx(project_worktree=branch, cv_root=main))


# --- the pass is wired the way the contract requires --------------------------

def test_pass_is_blocking(pass_mod):
    """The merge must be refused, not a report filed after the fact."""
    assert pass_mod.PASS.blocking is True
    assert pass_mod.PASS.name == "backlog-id-collisions"


def test_discovered_by_the_loader(pass_mod):
    """A pass the loader cannot see never runs."""
    import close_passes

    names = [p.name for p in close_passes.discover_passes(_LIB / "close_passes")]
    assert names == ["backlog-id-collisions"]


# --- clean cases --------------------------------------------------------------

def test_unique_ids_pass(pass_mod, tmp_path):
    main, branch = _trees(tmp_path)
    _write_backlog(main, _row("F01", "alpha"))
    _write_backlog(branch, _row("F01", "alpha") + _row("F02", "beta"))
    result = _run(pass_mod, branch, main)
    assert result.status == "ok"
    assert "F03" in result.message  # next free id


def test_absent_backlog_is_not_a_finding(pass_mod, tmp_path):
    main, branch = _trees(tmp_path)
    result = _run(pass_mod, branch, main)
    assert result.status == "ok"


def test_row_inherited_unchanged_from_main_is_not_a_collision(pass_mod, tmp_path):
    main, branch = _trees(tmp_path)
    _write_backlog(main, _row("F07", "build a concurrent driver"))
    _write_backlog(branch, _row("F07", "build a concurrent driver"))
    assert _run(pass_mod, branch, main).status == "ok"


def test_landing_a_row_is_not_a_collision(pass_mod, tmp_path):
    """Landing drops the row from backlog.md; history keeps it under the same
    Idea, so the branch neither re-allocates nor collides."""
    main, branch = _trees(tmp_path)
    _write_backlog(main, _row("F16", "warn that currentLane is not reproducible"))
    _commit_all(main, "file F16")
    _write_backlog(main, "")
    _commit_all(main, "land F16")
    _write_backlog(branch, "")
    assert _run(pass_mod, branch, main).status == "ok"


def test_branch_still_carrying_a_row_main_landed_is_not_a_collision(pass_mod,
                                                                   tmp_path):
    """A branch cut before main landed F16 still has it; main does not. The
    row is in the merge-base, so it is inherited, not new on the branch."""
    main = _repo(tmp_path / "main")
    _write_backlog(main, _row("F16", "warn that **currentLane** is not reproducible"))
    _commit_all(main, "file F16")
    branch = _cut_branch(main, tmp_path / "branch")
    _write_backlog(main, "")
    _commit_all(main, "land F16")
    assert _run(pass_mod, branch, main).status == "ok"


def test_inherited_row_reworded_then_landed_on_main_is_not_checked(pass_mod,
                                                                    tmp_path):
    """Main rewords an inherited row, then lands it. History then
    shows the id under two Ideas, and the branch carries the old one. The id
    is absent from main's backlog.md but present at the branch point, so it
    is not this branch's allocation and is not checked against history."""
    main = _repo(tmp_path / "main")
    _write_backlog(main, _row("F16", "warn that currentLane is not reproducible"))
    _commit_all(main, "file F16")
    branch = _cut_branch(main, tmp_path / "branch")
    _write_backlog(main, _row("F16", "flag currentLane as non-deterministic"))
    _commit_all(main, "reword F16")
    _write_backlog(main, "")
    _commit_all(main, "land F16")
    result = _run(pass_mod, branch, main)
    assert result.status == "ok", result.message


def test_branchs_own_committed_row_is_not_a_collision(pass_mod, tmp_path):
    """`--all` shows the branch's own commits, so a row the branch filed is in
    history under its own Idea; that must not read as a reuse."""
    main, branch = _trees(tmp_path)
    _write_backlog(branch, _row("F20", "pin the config schema"))
    _commit_all(branch, "file F20")
    assert _run(pass_mod, branch, main).status == "ok"


def test_id_cited_in_prose_is_not_read_as_an_allocation(pass_mod, tmp_path):
    """Sketches cite other rows by id constantly ("depends on F07")."""
    main, branch = _trees(tmp_path)
    _write_backlog(main, _row("F09", "measure RSS"))
    _write_backlog(
        branch,
        _row("F09", "measure RSS")
        + "| **F10** | something | blocked by F09, supersedes F09 | S | o |\n",
    )
    assert _run(pass_mod, branch, main).status == "ok"


def test_other_backlog_files_on_disk_are_not_opened(pass_mod, tmp_path):
    """Only backlog.md is read from the trees; any other backlog file counts
    only through git history."""
    main, branch = _trees(tmp_path)
    _write_backlog(branch, _row("F08", "verify test_run_smoke"))
    _write_backlog(branch, _row("F08", "verify test_run_smoke"), name="backlog-old.md")
    assert _run(pass_mod, branch, main).status == "ok"


# --- collisions ---------------------------------------------------------------

def test_catches_concurrent_allocations(pass_mod, tmp_path):
    """The regression this pass exists for.

    Two branches each allocate F13 to an unrelated finding. The branch's own
    table is internally consistent, so only a comparison against main can see
    it.
    """
    prior = "".join(_row(f"F{n:02d}", f"finding {n}") for n in range(1, 13))
    main, branch = _trees(tmp_path)
    _write_backlog(main, prior + _row("F13", "rename races_extended.course_id"))
    _commit_all(main, "sibling merged F13")
    _write_backlog(branch, prior + _row("F13", "pin the config schema"))

    result = _run(pass_mod, branch, main)
    assert result.status == "fail"
    assert "F13" in result.message
    assert "next free: F14" in result.message, (
        "the message should hand the author an id to renumber to"
    )
    # The twelve rows both sides genuinely share are not reported, and F13,
    # new on the branch with main's row in history, is reported once.
    assert result.message.count("allocated to different findings") == 1
    assert result.message.startswith("1 backlog F-id collision(s)"), result.message


def test_catches_a_retired_id_reused_from_history(pass_mod, tmp_path):
    """A landed row has left every backlog file on disk; reusing its id for a
    new finding is caught only through history (here a retired file name)."""
    main, branch = _trees(tmp_path)
    _write_backlog(main, "")
    _write_backlog(main, _row("F16", "warn that currentLane is not reproducible"),
                   name="backlog-old.md")
    _commit_all(main, "archive F16")
    (main / "my-projects" / "backlog-old.md").unlink()
    _commit_all(main, "retire the archive")
    _write_backlog(branch, _row("F16", "pin the config schema"))

    result = _run(pass_mod, branch, main)
    assert result.status == "fail"
    assert "F16 is new on this branch" in result.message
    assert "next free: F17" in result.message


def test_catches_a_sibling_branchs_unmerged_allocation(pass_mod, tmp_path):
    """`--all`: a concurrent project's committed row counts before it merges."""
    main, branch = _trees(tmp_path)
    _write_backlog(main, "")
    _commit_all(main, "seed")
    _git(main, "checkout", "-q", "-b", "project/0098-sibling")
    _write_backlog(main, _row("F13", "rename races_extended.course_id"))
    _commit_all(main, "sibling files F13")
    _git(main, "checkout", "-q", "-")
    _write_backlog(branch, _row("F13", "pin the config schema"))

    result = _run(pass_mod, branch, main)
    assert result.status == "fail"
    assert "F13 is new on this branch" in result.message


def test_catches_a_duplicate_inside_one_tree(pass_mod, tmp_path):
    main, branch = _trees(tmp_path)
    _write_backlog(main, "")
    _write_backlog(branch, _row("F05", "alpha") + _row("F05", "beta"))
    result = _run(pass_mod, branch, main)
    assert result.status == "fail"
    assert "2x" in result.message


def test_unreadable_history_fails_rather_than_skipping(pass_mod, tmp_path):
    """A blocking gate that passes when it could not look is no gate."""
    main, branch = tmp_path / "not-a-repo", tmp_path / "branch"
    main.mkdir()
    branch.mkdir()
    _write_backlog(branch, _row("F01", "alpha"))
    result = _run(pass_mod, branch, main)
    assert result.status == "fail"
    assert "could not read backlog history" in result.message


def test_unresolvable_branch_point_fails_rather_than_skipping(pass_mod, tmp_path):
    """No merge-base means no way to tell new ids from inherited ones."""
    main = _repo(tmp_path / "main")
    branch = tmp_path / "branch"
    branch.mkdir()
    _git(branch, "init", "-q", "-b", "trunk")  # no `main` ref to diff against
    _git(branch, "config", "user.email", "t@example.com")
    _git(branch, "config", "user.name", "t")
    _write_backlog(branch, _row("F01", "alpha"))
    _commit_all(branch, "seed")
    result = _run(pass_mod, branch, main)
    assert result.status == "fail"
    assert "could not read my-projects/backlog.md at the branch point" in result.message


def test_worktree_equal_to_main_is_not_diffed_against_itself(pass_mod, tmp_path):
    """A project closing from the main repo has no separate worktree; reading
    the same files twice must not report every row as a duplicate."""
    root = _repo(tmp_path / "repo")
    _write_backlog(root, _row("F01", "alpha") + _row("F02", "beta"))
    assert _run(pass_mod, root, root).status == "ok"


def test_session_mode_skips_the_history_check(pass_mod, tmp_path):
    """The documented skip: with no branch point, a reused retired
    id is not caught in session mode, and the ok message says the check was
    skipped. A duplicate inside backlog.md is still caught."""
    root = _repo(tmp_path / "repo")
    _write_backlog(root, "")
    _write_backlog(root, _row("F16", "warn that currentLane is not reproducible"),
                   name="backlog-old.md")
    _commit_all(root, "archive F16")
    (root / "my-projects" / "backlog-old.md").unlink()
    _commit_all(root, "retire the archive")
    _write_backlog(root, _row("F16", "pin the config schema"))

    result = _run(pass_mod, root, root)
    assert result.status == "ok", result.message
    assert "history check skipped (session mode" in result.message

    _write_backlog(root, _row("F16", "alpha") + _row("F16", "beta"))
    assert _run(pass_mod, root, root).status == "fail"


# --- the config's backlog_file and base_branch ---------------------------------

@dataclass
class _Cfg:
    backlog_file: str
    base_branch: str


@dataclass
class FakeCfgCtx:
    project_worktree: Path
    cv_root: Path
    cfg: _Cfg


def test_history_pathspec_derives_from_the_backlog_file(pass_mod):
    assert (pass_mod.history_pathspec(Path("my-projects/backlog.md"))
            == pass_mod.HISTORY_PATHSPEC)
    assert pass_mod.history_pathspec(Path("docs/ideas.md")) == ":(glob)docs/ideas*"
    assert pass_mod.history_pathspec(Path("backlog.md")) == ":(glob)backlog*"


def test_reads_the_configured_backlog_file_and_base_branch(pass_mod, tmp_path):
    """A repo whose backlog is `docs/ideas.md` on base branch `trunk`: the
    pass reads that file in both trees and finds the branch point from
    `trunk`, so a duplicate there is caught and a clean file passes."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "trunk")
    _git(main, "config", "user.email", "t@example.com")
    _git(main, "config", "user.name", "t")
    (main / "README.md").write_text("seed\n", encoding="utf-8")
    _commit_all(main, "seed")
    branch = tmp_path / "branch"
    _git(main, "worktree", "add", "-q", "-b", "project/0099-test", str(branch), "trunk")
    ideas = branch / "docs" / "ideas.md"
    ideas.parent.mkdir(parents=True)
    cfg = _Cfg(backlog_file="docs/ideas.md", base_branch="trunk")

    ideas.write_text(f"# Ideas\n\n{_HEADER}{_row('F01', 'alpha')}", encoding="utf-8")
    ok = pass_mod.PASS.run(FakeCfgCtx(branch, main, cfg))
    assert ok.status == "ok", ok.message
    assert "1 backlog F-id" in ok.message

    ideas.write_text(
        f"# Ideas\n\n{_HEADER}{_row('F01', 'alpha')}{_row('F01', 'beta')}",
        encoding="utf-8")
    dup = pass_mod.PASS.run(FakeCfgCtx(branch, main, cfg))
    assert dup.status == "fail"
    assert "F01 appears 2x in the closing worktree" in dup.message

