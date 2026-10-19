"""Id allocation in `new-project.py`, pinned against reissue.

`next_project_id()` documents "An id must never be reissued", and broke that
rule: its three original sources all read present state, so a project that was
archived and then deleted became invisible and its id was offered again. An id
that was issued, archived and deleted must still never be offered again.

The skill's `scripts/` is not an importable package and `new-project.py`
carries a hyphen, so the module is loaded by path exactly as
`test_project_scripts_close.py` does.
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
_LIB = ROOT / "scripts" / "lib"
_BIN = ROOT / "scripts" / "bin"


def _load(path: Path, name: str):
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def new_project():
    return _load(_BIN / "new-project.py", "_new_project_under_test")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture()
def repo_with_a_deleted_project(tmp_path: Path) -> Path:
    """A repo where project 0058 was scaffolded, then deleted outright.

    Deliberately leaves no trace in present state: no folder on disk, nothing
    at the ref tip, no linked worktree. Only history knows 0058 existed.
    """
    repo = tmp_path / "repo"
    (repo / "my-projects" / "current").mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")

    folder = repo / "my-projects" / "current" / "project-0058-throwaway"
    folder.mkdir()
    (folder / "STATUS.md").write_text('---\nproject: "0058"\n---\n', encoding="utf-8")
    rel = "my-projects/current/project-0058-throwaway/STATUS.md"
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", "scaffold 0058")

    _git(repo, "rm", "-r", "-q", "my-projects/current/project-0058-throwaway")
    _git(repo, "commit", "-m", "delete 0058")
    return repo


def test_the_present_state_sources_are_blind_to_a_deleted_project(
        new_project, repo_with_a_deleted_project, monkeypatch):
    """Establishes *why* a fourth source is needed, rather than asserting it.

    If this ever fails because a present-state source started seeing 0058, the
    history scan is redundant and should be reconsidered, not patched around.
    """
    repo = repo_with_a_deleted_project
    monkeypatch.setattr(new_project, "CV_ROOT", repo)

    assert new_project._ids_on_disk(repo) == []
    assert 58 not in new_project._ids_in_refs()
    assert 58 not in new_project._ids_in_linked_worktrees()


def test_history_remembers_a_project_that_was_archived_then_deleted(
        new_project, repo_with_a_deleted_project, monkeypatch):
    monkeypatch.setattr(new_project, "CV_ROOT", repo_with_a_deleted_project)
    assert 58 in new_project._ids_in_history()


def test_next_project_id_never_reissues_a_deleted_id(
        new_project, repo_with_a_deleted_project, monkeypatch):
    """The regression itself: fails with `0001` against the three-source union.

    `0001` rather than `0059` is the tell -- with every present-state source
    blind, the old allocator saw no ids at all and restarted from the bottom,
    so the bug was never limited to the one id that happened to be deleted.
    """
    monkeypatch.setattr(new_project, "CV_ROOT", repo_with_a_deleted_project)
    assert new_project.next_project_id() == "0059"


def test_a_deleted_id_stays_burned_even_with_later_projects_present(
        new_project, repo_with_a_deleted_project, monkeypatch):
    """The gap is preserved, not closed: 0058 deleted and 0059 live yields 0060."""
    repo = repo_with_a_deleted_project
    live = repo / "my-projects" / "current" / "project-0059-live"
    live.mkdir(parents=True)
    (live / "STATUS.md").write_text('---\nproject: "0059"\n---\n', encoding="utf-8")
    monkeypatch.setattr(new_project, "CV_ROOT", repo)

    assert new_project.next_project_id() == "0060"


# --- skill-contract alignment -----------------------------------
#
# The main-side stub carries the skill's `stub: true` key (not the old
# `scaffold_stub`), scaffolding can stop before the worktree is cut
# (`--no-worktree`), a later `--resume-id` run cuts it, and neither
# `wave-manifest.yaml` nor the retired streams-config overlay is written.

def _frontmatter(text: str) -> dict:
    import yaml
    m = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
    assert m, "no frontmatter"
    return yaml.safe_load(m.group(1))


def test_stub_carries_the_skill_stub_key(new_project, tmp_path):
    wt = tmp_path / "repo-worktrees" / "0031-new-thing"
    text = new_project.render_status_stub("0031", "new-thing", wt,
                                          "project/0031-new-thing")
    fm = _frontmatter(text)
    assert fm["stub"] is True
    assert "scaffold_stub" not in fm
    assert fm["authoritative_status"] == (
        f"{wt.as_posix()}/my-projects/current/project-0031-new-thing/STATUS.md")
    assert "Do not plan from this file." in text


def test_new_stub_is_recognised_as_a_stub(new_project, tmp_path):
    path = tmp_path / "STATUS.md"
    path.write_text(new_project.render_status_stub(
        "0031", "new-thing", tmp_path, "project/0031-new-thing"), encoding="utf-8")
    assert new_project._is_scaffold_stub(path) is True


def test_legacy_scaffold_stub_key_is_still_recognised(new_project, tmp_path):
    """An older scaffold resumed after this change must still be upgraded."""
    path = tmp_path / "STATUS.md"
    path.write_text('---\nproject: "0031"\nstatus: active\nscaffold_stub: true\n'
                    "---\n\n# STATUS\n", encoding="utf-8")
    assert new_project._is_scaffold_stub(path) is True


@pytest.mark.parametrize("fm", [
    "",                         # the real template carries no stub key
    "stub: false\n",
    'stub: "true"\n',          # a string is not the boolean the script writes
])
def test_real_status_is_never_taken_for_a_stub(new_project, tmp_path, fm):
    path = tmp_path / "STATUS.md"
    path.write_text(f'---\nproject: "0031"\nstatus: active\n{fm}current_wave: 3\n'
                    "---\n\n# STATUS\n", encoding="utf-8")
    assert new_project._is_scaffold_stub(path) is False


@pytest.fixture()
def scaffold_repo(tmp_path: Path, new_project, monkeypatch):
    """An empty git repo that `new-project.py` treats as the main checkout.

    The repo's committed `.claude/project-workflow.json` names a `bootstrap`
    argv that drops a marker file in the worktree, so a test can see whether
    (and where) it ran without copying data.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# repo\n", encoding="utf-8")
    bootstrap = tmp_path / "fake-bootstrap.py"
    bootstrap.write_text(
        "import pathlib, sys\n"
        "pathlib.Path(sys.argv[1], 'BOOTSTRAPPED').write_text('ok')\n",
        encoding="utf-8")
    (repo / ".claude").mkdir()
    (repo / ".claude" / "project-workflow.json").write_text(
        json.dumps({"bootstrap": ["python", str(bootstrap), "{worktree}"]}),
        encoding="utf-8")
    _git(repo, "add", "README.md", ".claude/project-workflow.json")
    _git(repo, "commit", "-m", "init")

    monkeypatch.setattr(new_project, "CV_ROOT", repo)
    monkeypatch.setattr(new_project, "repo_basename", lambda: "repo")
    return repo


def _tracked(repo: Path, rev: str, rel: str) -> list[str]:
    r = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                        rev, "--", rel], check=True, capture_output=True, text=True)
    return r.stdout.split()


def test_no_worktree_scaffolds_and_commits_but_cuts_nothing(new_project,
                                                            scaffold_repo):
    repo = scaffold_repo
    rc = new_project.main(["new-project.py", "demo", "--yes", "--no-worktree"])
    assert rc == 0

    rel = "my-projects/current/project-0001-demo"
    files = _tracked(repo, "HEAD", rel)
    assert f"{rel}/STATUS.md" in files and f"{rel}/brief.md" in files
    assert f"{rel}/wave-manifest.yaml" not in files
    assert not (repo / rel / "wave-manifest.yaml").exists()
    # The retired overlay; the name is built from parts so a grep for retired
    # names stays clean on this tree.
    assert not (repo / ".claude" / "streams" / "config"
                / ("projects" + ".yaml")).exists()

    fm = _frontmatter((repo / rel / "STATUS.md").read_text(encoding="utf-8"))
    assert fm["stub"] is True

    assert not (repo.parent / "repo-worktrees" / "0001-demo").exists()
    r = subprocess.run(["git", "-C", str(repo), "branch", "--list", "project/*"],
                       check=True, capture_output=True, text=True)
    assert r.stdout.strip() == ""


def test_resume_cuts_bootstraps_and_upgrades_only_the_branch_copy(new_project,
                                                                  scaffold_repo):
    """The cut at first dispatch: `--resume-id` on a `--no-worktree` scaffold.

    Stub-first order holds: main keeps the stub, the branch gets the template.
    """
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes", "--no-worktree"]) == 0
    main_head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                               check=True, capture_output=True, text=True).stdout

    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0

    wt = repo.parent / "repo-worktrees" / "0001-demo"
    rel = "my-projects/current/project-0001-demo"
    assert (wt / "BOOTSTRAPPED").is_file()
    assert new_project._worktree_exists(wt)

    wt_fm = _frontmatter((wt / rel / "STATUS.md").read_text(encoding="utf-8"))
    assert "stub" not in wt_fm and "scaffold_stub" not in wt_fm
    assert not (wt / rel / "wave-manifest.yaml").exists()

    main_fm = _frontmatter((repo / rel / "STATUS.md").read_text(encoding="utf-8"))
    assert main_fm["stub"] is True
    # The resume committed nothing on main: the stub is the merge base.
    assert subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          check=True, capture_output=True,
                          text=True).stdout == main_head


def test_no_bootstrap_skips_the_bootstrap_step(new_project, scaffold_repo):
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-bootstrap"]) == 0
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    assert new_project._worktree_exists(wt)
    assert not (wt / "BOOTSTRAPPED").exists()


# --- stub read-back, bootstrap retry and next-step hints --------------------

def test_split_flow_carries_closes_and_parent_into_the_worktree_status(
        new_project, scaffold_repo):
    """The cut run reads `closes:` / `parent_project:` back from main's stub.

    The worktree copy is the one that records `closes:` for the project's
    life, so a cut run that re-rendered from its own (flag-less) CLI silently
    dropped the claim.
    """
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes", "--no-worktree",
                             "--closes", "R1", "--parent", "0009"]) == 0
    rel = "my-projects/current/project-0001-demo"
    main_fm = _frontmatter((repo / rel / "STATUS.md").read_text(encoding="utf-8"))
    assert main_fm["closes"] == ["R1"] and main_fm["parent_project"] == "0009"
    assert main_fm["base"] == "main"

    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    wt_fm = _frontmatter((wt / rel / "STATUS.md").read_text(encoding="utf-8"))
    assert "stub" not in wt_fm
    assert wt_fm["closes"] == ["R1"]
    assert wt_fm["parent_project"] == "0009"


def test_split_flow_cuts_from_the_base_recorded_in_the_stub(new_project,
                                                            scaffold_repo):
    """`--base` given at scaffold is honoured by a flag-less cut run."""
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes", "--no-worktree",
                             "--base", "side"]) == 0
    _git(repo, "branch", "side")                    # at the scaffold commit
    (repo / "later.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-m", "later on main")     # main moves past side

    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0
    r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                        "main", "project/0001-demo"], capture_output=True)
    assert r.returncode == 1, "branch was cut from main, not the recorded base"
    r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor",
                        "side", "project/0001-demo"], capture_output=True)
    assert r.returncode == 0


def test_resume_with_an_existing_worktree_still_runs_the_bootstrap(
        new_project, scaffold_repo, monkeypatch):
    """A resume is how a failed bootstrap is retried."""
    calls: list[Path] = []

    def fake_bootstrap(argv: list[str]) -> int:
        calls.append(Path(argv[-1]))
        return 1 if len(calls) == 1 else 0          # first attempt fails

    monkeypatch.setattr(new_project, "_run_bootstrap", fake_bootstrap)
    assert new_project.main(["new-project.py", "demo", "--yes"]) == 1
    wt = (scaffold_repo.parent / "repo-worktrees" / "0001-demo").resolve()
    assert new_project._worktree_exists(wt)

    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0
    assert calls == [wt, wt]


def test_resume_bootstraps_with_the_worktrees_own_config(new_project,
                                                         scaffold_repo, tmp_path):
    """The config is read from the project's worktree when it has one."""
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-bootstrap"]) == 0
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    other = tmp_path / "wt-bootstrap.py"
    other.write_text("import pathlib, sys\n"
                     "pathlib.Path(sys.argv[1], 'WT_BOOTSTRAPPED').write_text('ok')\n",
                     encoding="utf-8")
    (wt / ".claude" / "project-workflow.json").write_text(
        json.dumps({"bootstrap": ["python", str(other), "{worktree}"]}),
        encoding="utf-8")
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0
    assert (wt / "WT_BOOTSTRAPPED").is_file()
    assert not (wt / "BOOTSTRAPPED").exists(), "main's bootstrap ran instead"


def test_cut_run_points_at_the_skill_not_at_authoring_or_retired_scripts(
        new_project, scaffold_repo, capsys):
    """After a cut the next step is wave-1 dispatch via the skill."""
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-worktree"]) == 0
    capsys.readouterr()
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001", "--no-bootstrap"]) == 0
    out = capsys.readouterr().out
    tail = out.split("=" * 72)[-1]
    assert "/project-workflow" in tail
    assert "author" not in tail
    assert "launch-wave" not in out and "status.py" not in out


def test_fallback_status_text_names_no_retired_script(new_project, monkeypatch,
                                                      tmp_path):
    """The built-in STATUS fallback no longer cites bin/status.py."""
    monkeypatch.setattr(new_project, "STATUS_TEMPLATE", tmp_path / "absent.md")
    text = new_project.render_status_scaffold("0001", "demo")
    assert "status.py" not in text


# --- base quoting and --closes precedence -----------------------------------

@pytest.mark.parametrize("base", ["0060", "1.10"])
def test_stub_base_round_trips_as_a_string(new_project, tmp_path, base):
    """`base:` is quoted, so a numeric-looking ref is not coerced."""
    path = tmp_path / "STATUS.md"
    path.write_text(new_project.render_status_stub(
        "0031", "new-thing", tmp_path, "project/0031-new-thing", base=base),
        encoding="utf-8")
    assert _frontmatter(path.read_text(encoding="utf-8"))["base"] == base
    assert new_project._stub_inputs(path)["base"] == base


def test_stub_inputs_recovers_an_unquoted_legacy_base(new_project, tmp_path):
    """A stub written before the quoting fix still reads back `0060`."""
    path = tmp_path / "STATUS.md"
    path.write_text('---\nproject: "0031"\nstatus: active\nstub: true\n'
                    "base: 0060\nparent_project: 0009\n---\n\n# STATUS\n",
                    encoding="utf-8")
    got = new_project._stub_inputs(path)
    assert got["base"] == "0060"
    assert got["parent"] == "0009"


def test_no_worktree_with_numeric_base_records_it_as_a_string(
        new_project, scaffold_repo, capsys):
    """The hint names the base ref, not main."""
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes", "--no-worktree",
                             "--base", "0060"]) == 0
    status = repo / "my-projects/current/project-0001-demo/STATUS.md"
    assert _frontmatter(status.read_text(encoding="utf-8"))["base"] == "0060"
    assert new_project._stub_inputs(status)["base"] == "0060"
    out = capsys.readouterr().out
    assert "commit it on main" not in out
    assert "where 0060 can reach it" in out


def test_no_worktree_hint_says_main_for_the_default_base(new_project,
                                                         scaffold_repo, capsys):
    """The default base keeps the plain `commit it on main` hint."""
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-worktree"]) == 0
    assert "commit it on main" in capsys.readouterr().out


def test_no_bootstrap_configured_says_so_and_still_cuts(new_project,
                                                        scaffold_repo, capsys):
    # Committed, so the cut worktree's own copy carries no bootstrap either:
    # the bootstrap step reads the worktree's config first.
    (scaffold_repo / ".claude" / "project-workflow.json").write_text(
        "{}", encoding="utf-8")
    _git(scaffold_repo, "commit", "-qam", "no bootstrap")
    assert new_project.main(["new-project.py", "demo", "--yes"]) == 0
    wt = scaffold_repo.parent / "repo-worktrees" / "0001-demo"
    assert new_project._worktree_exists(wt)
    assert not (wt / "BOOTSTRAPPED").exists()
    assert "bootstrap: none configured" in capsys.readouterr().out


def test_configured_projects_dir_and_base_branch_are_used(new_project,
                                                          scaffold_repo, capsys):
    _git(scaffold_repo, "branch", "trunk")
    (scaffold_repo / ".claude" / "project-workflow.json").write_text(
        json.dumps({"projects_dir": "work", "base_branch": "trunk"}),
        encoding="utf-8")
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-worktree"]) == 0
    status = scaffold_repo / "work/current/project-0001-demo/STATUS.md"
    assert _frontmatter(status.read_text(encoding="utf-8"))["base"] == "trunk"
    assert not (scaffold_repo / "my-projects").exists()
    out = capsys.readouterr().out
    # The resume hint names this script by its own location.
    assert Path(new_project.__file__).resolve().as_posix() in out


def test_split_flow_cli_closes_on_the_cut_beats_the_stub(new_project,
                                                         scaffold_repo):
    """`--closes R2` on the cut run wins over the stub's recorded R1."""
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes", "--no-worktree",
                             "--closes", "R1"]) == 0
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001", "--closes", "R2"]) == 0
    rel = "my-projects/current/project-0001-demo"
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    assert _frontmatter((wt / rel / "STATUS.md").read_text(
        encoding="utf-8"))["closes"] == ["R2"]
    # main's stub is the merge base and is left as written.
    assert _frontmatter((repo / rel / "STATUS.md").read_text(
        encoding="utf-8"))["closes"] == ["R1"]


def test_cut_reads_closes_and_parent_from_a_legacy_scaffold_stub(new_project,
                                                                 scaffold_repo):
    """An older `scaffold_stub: true` stub still feeds the cut."""
    repo = scaffold_repo
    rel = "my-projects/current/project-0001-demo"
    folder = repo / rel
    folder.mkdir(parents=True)
    (folder / "brief.md").write_text("# Brief — demo\n", encoding="utf-8")
    (folder / "STATUS.md").write_text(
        '---\nproject: "0001"\nname: demo\nstatus: active\nscaffold_stub: true\n'
        "closes: [R7]\nparent_project: \"0012\"\n---\n\n# STATUS\n",
        encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", "legacy scaffold")

    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    wt_fm = _frontmatter((wt / rel / "STATUS.md").read_text(encoding="utf-8"))
    assert "scaffold_stub" not in wt_fm and "stub" not in wt_fm
    assert wt_fm["closes"] == ["R7"]
    assert wt_fm["parent_project"] == "0012"


# --- skill STATUS schema, external_yield and index rows ----------------------

SKILL_KEYS = ("project", "name", "status", "current_wave", "worktree",
              "branch", "deadlines")


@pytest.mark.parametrize("source", ["template", "fallback"])
def test_worktree_status_follows_the_skill_schema(new_project, monkeypatch,
                                                  tmp_path, source):
    """Skill STATUS schema, plus the blank external_yield pair."""
    # STATUS_TEMPLATE derives from CV_ROOT, the MAIN checkout; pin this tree's.
    template = (tmp_path / "absent.md" if source == "fallback"
                else ROOT / "templates" / "status-template.md")
    monkeypatch.setattr(new_project, "STATUS_TEMPLATE", template)
    wt = tmp_path / "repo-worktrees" / "0031-new-thing"
    text = new_project.render_status_scaffold(
        "0031", "new-thing", worktree=wt, branch="project/0031-new-thing")
    fm = _frontmatter(text)
    for key in SKILL_KEYS:
        assert key in fm, key
    assert fm["worktree"] == wt.as_posix()
    assert fm["branch"] == "project/0031-new-thing"
    assert fm["current_wave"] == 1
    assert "waves" not in fm
    # Both keys present and blank, with a comment naming the gate.
    assert fm["external_yield"] == "" and fm["external_yield_waiver"] == ""
    head = text.split("\n---\n", 1)[0]
    assert "close:external_yield" in head
    body = text.split("\n---\n", 1)[1]
    for heading in ("## Resume", "## Wave 1 — ", "## Log"):
        assert re.search(rf"^{re.escape(heading)}", body, re.MULTILINE), heading
    assert "streams/" not in text


def test_stub_points_external_yield_at_the_worktree_copy(new_project, tmp_path):
    """The stub names the keys, but only as a comment (never a frozen blank)."""
    text = new_project.render_status_stub("0031", "new-thing", tmp_path,
                                          "project/0031-new-thing")
    fm = _frontmatter(text)
    assert "external_yield" not in fm
    assert "external_yield" in text.split("\n---\n", 1)[0]


def test_readme_index_row_on_a_header_only_table(new_project, tmp_path,
                                                 monkeypatch):
    """A table with no data row yet used to get no row, silently."""
    readme = tmp_path / "README.md"
    readme.write_text("# Projects\n\n| Kind | Where |\n|---|---|\n| a | b |\n\n"
                      "## Index\n\n| Id | Project | Where | Status |\n"
                      "|---|---|---|---|\n\n## After\n", encoding="utf-8")
    monkeypatch.setattr(new_project, "README_PATH", readme)
    assert new_project.append_readme_index_row("0001", "first") is True
    lines = readme.read_text(encoding="utf-8").splitlines()
    i = lines.index("| Id | Project | Where | Status |")
    assert lines[i + 2] == "| 0001 | first | current | planned |"
    assert lines[:5] == ["# Projects", "", "| Kind | Where |", "|---|---|",
                         "| a | b |"]
    assert new_project.append_readme_index_row("0001", "first") is False


def test_scaffold_creates_no_streams_dir_and_reads_as_skill_run(new_project,
                                                                scaffold_repo):
    """No `streams/`, so close-project.py's `is_skill_run` holds."""
    repo = scaffold_repo
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-bootstrap"]) == 0
    rel = "my-projects/current/project-0001-demo"
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    assert not (repo / rel / "streams").exists()
    assert not (wt / rel / "streams").exists()
    assert (repo / rel / "artifacts").is_dir()
    close_project = _load(_BIN / "close-project.py", "_close_project_for_f12")
    assert close_project.is_skill_run("0001", wt) is True


# --- workflow key, template comment and index warnings -----------------------

@pytest.mark.parametrize("source", ["template", "fallback"])
def test_scaffold_status_carries_the_workflow_key_the_index_row_step_needs(
        new_project, monkeypatch, tmp_path, source):
    """`workflow:` is present and matches close_steps.py's
    `_WORKFLOW_KEY_RE` (the index-row step refuses a STATUS without it)."""
    template = (tmp_path / "absent.md" if source == "fallback"
                else ROOT / "templates" / "status-template.md")
    monkeypatch.setattr(new_project, "STATUS_TEMPLATE", template)
    text = new_project.render_status_scaffold("0031", "new-thing")
    assert _frontmatter(text)["workflow"] == "project-workflow"
    close_steps = _load(_LIB / "close_steps.py", "_close_steps_for_r2")
    assert close_steps._WORKFLOW_KEY_RE.search(text) is not None


def test_main_stub_writes_the_same_workflow_as_the_worktree_status(
        new_project, monkeypatch):
    """The stub and the worktree STATUS agree on `workflow:`."""
    monkeypatch.setattr(new_project, "STATUS_TEMPLATE",
                        ROOT / "templates" / "status-template.md")
    stub = new_project.render_status_stub(
        "0031", "new-thing", Path("/wt/0031-new-thing"), "feat/new-thing")
    assert _frontmatter(stub)["workflow"] == "project-workflow"
    scaffold = new_project.render_status_scaffold("0031", "new-thing")
    assert _frontmatter(stub)["workflow"] == _frontmatter(scaffold)["workflow"]


def test_template_says_decide_at_create_fill_at_the_cut():
    """The template comment matches the profile's external-yield rule."""
    text = (ROOT / "templates" / "status-template.md").read_text(encoding="utf-8")
    head = text.split("\n---\n", 1)[0]
    assert "Fill at create" not in head
    assert "Decide at create, fill here at the cut, check before close" in head


def test_readme_index_row_warns_without_an_index_table(new_project, tmp_path,
                                                       monkeypatch, capsys):
    """No `| NNNN |` row and no `| Id |` header: warn, do not stay silent."""
    readme = tmp_path / "README.md"
    original = "# Projects\n\n| Number | Name |\n|---|---|\n"
    readme.write_text(original, encoding="utf-8")
    monkeypatch.setattr(new_project, "README_PATH", readme)
    assert new_project.append_readme_index_row("0001", "first") is False
    err = capsys.readouterr().err
    assert "WARN:" in err and "0001" in err and "no project index table" in err
    assert readme.read_text(encoding="utf-8") == original


# --- planned at scaffold, active at the cut ----------------------------------

_INDEX = ("# Projects\n\n| Id | Project | Where | Status | Artifacts | Outstanding |\n"
          "|---|---|---|---|---|---|\n| 0000 | older | completed | completed | - | - |\n")


def _row(text: str, project_id: str) -> str:
    return next(line for line in text.splitlines()
                if line.startswith(f"| {project_id} |"))


def test_stub_is_written_planned(new_project, tmp_path):
    """The base-branch stub records the scaffold state, not a running one."""
    text = new_project.render_status_stub("0031", "new-thing", tmp_path,
                                          "project/0031-new-thing")
    assert _frontmatter(text)["status"] == "planned"


def test_index_row_is_written_planned(new_project, tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(_INDEX, encoding="utf-8")
    monkeypatch.setattr(new_project, "README_PATH", readme)
    assert new_project.append_readme_index_row("0001", "demo") is True
    assert _row(readme.read_text(encoding="utf-8"), "0001") == (
        "| 0001 | demo | current | planned | 0 | - |")


@pytest.mark.parametrize("status", ["active", "completed", "paused — see notes"])
def test_flip_leaves_a_row_that_is_not_planned_alone(new_project, tmp_path,
                                                     status):
    readme = tmp_path / "README.md"
    original = _INDEX + f"| 0001 | planned | current | {status} | 0 | - |\n"
    readme.write_text(original, encoding="utf-8")
    assert new_project.flip_index_row_active(readme, "0001") is False
    assert readme.read_text(encoding="utf-8") == original


def test_flip_rewrites_only_the_status_cell(new_project, tmp_path):
    """A slug that reads `planned` is not the Status cell; other rows stay."""
    readme = tmp_path / "README.md"
    readme.write_text(_INDEX + "| 0001 | planned | current | planned | 0 | - |\n"
                      "| 0002 | other | current | planned | 0 | - |\n",
                      encoding="utf-8")
    assert new_project.flip_index_row_active(readme, "0001") is True
    text = readme.read_text(encoding="utf-8")
    assert _row(text, "0001") == "| 0001 | planned | current | active | 0 | - |"
    assert _row(text, "0002") == "| 0002 | other | current | planned | 0 | - |"
    assert new_project.flip_index_row_active(readme, "0001") is False


def test_cut_writes_active_status_and_flips_the_worktree_row(new_project,
                                                             scaffold_repo):
    """Scaffold writes `planned` on main; the cut writes `active` in the
    worktree (STATUS and index row, committed) and leaves main's frozen."""
    repo = scaffold_repo
    index = repo / "my-projects" / "README.md"
    index.parent.mkdir()
    index.write_text(_INDEX, encoding="utf-8")
    _git(repo, "add", "my-projects/README.md")
    _git(repo, "commit", "-m", "index")

    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-worktree"]) == 0
    rel = "my-projects/current/project-0001-demo"
    assert _frontmatter((repo / rel / "STATUS.md").read_text(
        encoding="utf-8"))["status"] == "planned"
    assert _row(index.read_text(encoding="utf-8"), "0001").split("|")[4].strip() \
        == "planned"

    assert new_project.main(["new-project.py", "demo", "--yes", "--resume-id",
                             "0001", "--no-bootstrap"]) == 0
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    assert _frontmatter((wt / rel / "STATUS.md").read_text(
        encoding="utf-8"))["status"] == "active"
    wt_index = (wt / "my-projects" / "README.md").read_text(encoding="utf-8")
    assert _row(wt_index, "0001") == "| 0001 | demo | current | active | 0 | - |"
    # Committed on the branch, not left dirty in the worktree.
    dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                           check=True, capture_output=True, text=True).stdout
    assert "README.md" not in dirty
    # Main's stub and row are frozen as written.
    assert _frontmatter((repo / rel / "STATUS.md").read_text(
        encoding="utf-8"))["status"] == "planned"
    assert "| 0001 | demo | current | planned |" in index.read_text(
        encoding="utf-8")


def _seed_index(repo: Path) -> None:
    index = repo / "my-projects" / "README.md"
    index.parent.mkdir()
    index.write_text(_INDEX, encoding="utf-8")
    _git(repo, "add", "my-projects/README.md")
    _git(repo, "commit", "-m", "index")


def _worktree_init_commits(wt: Path) -> list[str]:
    return subprocess.run(
        ["git", "-C", str(wt), "log", "--format=%s", "--fixed-strings",
         "--grep", "worktree init"],
        check=True, capture_output=True, text=True).stdout.splitlines()


def _assert_row_active_and_committed_once(wt: Path) -> None:
    wt_index = (wt / "my-projects" / "README.md").read_text(encoding="utf-8")
    assert _row(wt_index, "0001") == "| 0001 | demo | current | active | 0 | - |"
    dirty = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                           check=True, capture_output=True, text=True).stdout
    assert dirty.strip() == "", dirty
    assert _worktree_init_commits(wt) == ["chore(0001): worktree init"]


def test_cut_rerun_after_a_failed_bootstrap_keeps_one_committed_active_row(
        new_project, scaffold_repo, monkeypatch):
    """A one-shot cut whose bootstrap fails, then a `--resume-id` retry, with
    an index present: the row is flipped and committed once, never twice."""
    _seed_index(scaffold_repo)
    attempts: list[int] = []

    def fake_bootstrap(argv: list[str]) -> int:
        attempts.append(1)
        return 1 if len(attempts) == 1 else 0       # first attempt fails

    monkeypatch.setattr(new_project, "_run_bootstrap", fake_bootstrap)
    assert new_project.main(["new-project.py", "demo", "--yes"]) == 1
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--resume-id", "0001"]) == 0
    assert len(attempts) == 2
    _assert_row_active_and_committed_once(
        scaffold_repo.parent / "repo-worktrees" / "0001-demo")


def test_cut_rerun_commits_the_row_a_rejected_init_commit_left_dirty(
        new_project, scaffold_repo):
    """A hook rejects the first `worktree init` commit and the flipped row is
    left on disk, unstaged. The rerun finds it already `active` (the flip is
    a no-op) and must still stage and commit it."""
    repo = scaffold_repo
    _seed_index(repo)
    assert new_project.main(["new-project.py", "demo", "--yes",
                             "--no-worktree"]) == 0
    # A repo-local hooks dir, so a global core.hooksPath cannot mask it;
    # worktrees share the repo's config, so it gates the worktree commit too.
    hooks = repo.parent / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8",
                                      newline="\n")
    (hooks / "pre-commit").chmod(0o755)
    _git(repo, "config", "core.hooksPath", hooks.as_posix())

    assert new_project.main(["new-project.py", "demo", "--yes", "--resume-id",
                             "0001", "--no-bootstrap"]) == 0
    wt = repo.parent / "repo-worktrees" / "0001-demo"
    # The hook rejected the commit, but the rejected run's `git add` survives
    # in the index. Drop it, as a user clearing the index after the failure
    # would (or a run killed between the flip write and its `git add`): the
    # flipped row is then on disk and unstaged.
    assert _worktree_init_commits(wt) == []
    _git(wt, "restore", "--staged", "--", ".")
    status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                            check=True, capture_output=True, text=True).stdout
    assert " M my-projects/README.md" in status, status

    (hooks / "pre-commit").unlink()
    assert new_project.main(["new-project.py", "demo", "--yes", "--resume-id",
                             "0001", "--no-bootstrap"]) == 0
    _assert_row_active_and_committed_once(wt)
