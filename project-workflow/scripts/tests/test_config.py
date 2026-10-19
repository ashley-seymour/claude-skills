"""lib/config.py: repo discovery from the cwd and the per-repo config file."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
_LIB = ROOT / "scripts" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import config  # noqa: E402

FULL = {
    "projects_dir": "work",
    "base_branch": "trunk",
    "suites": [{"name": "unit", "argv": ["python", "-m", "pytest", "unit/"]}],
    "suite_pin": "pins/floor.json",
    "bootstrap": ["python", "boot.py", "{worktree}"],
    "teardown": ["python", "boot.py", "{worktree}", "--teardown"],
    "close": {"merge": "squash", "ci_workflow": "ci.yml"},
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t",
                           "-c", "user.name=t", *args], check=True,
                          capture_output=True, text=True).stdout


def test_defaults_derive_the_paths_from_projects_dir():
    cfg = config.Config()
    assert (cfg.projects_dir, cfg.base_branch, cfg.merge) == ("my-projects", "main", "local")
    assert (cfg.profile, cfg.index_file, cfg.backlog_file) == (
        "my-projects/WORKFLOW.md", "my-projects/README.md", "my-projects/backlog.md")
    assert cfg.suites == () and cfg.suite_pin is None
    assert cfg.bootstrap is None and cfg.teardown is None and cfg.ci_workflow is None
    moved = config.Config(projects_dir="work")
    assert moved.backlog_file == "work/backlog.md"


def test_parse_config_reads_every_key():
    cfg = config.parse_config(FULL)
    assert cfg.projects_dir == "work" and cfg.base_branch == "trunk"
    assert cfg.index_file == "work/README.md"
    assert cfg.suites == (config.Suite("unit", ("python", "-m", "pytest", "unit/")),)
    assert cfg.suite_pin == "pins/floor.json"
    assert cfg.bootstrap == ("python", "boot.py", "{worktree}")
    assert cfg.teardown[-1] == "--teardown"
    assert (cfg.merge, cfg.ci_workflow) == ("squash", "ci.yml")


@pytest.mark.parametrize("bad, words", [
    ({"suite": []}, "unknown key"),
    ({"base_branch": ""}, "non-empty string"),
    ({"suites": {"name": "x"}}, "must be a list"),
    ({"suites": [{"name": "x", "argv": "pytest"}]}, "non-empty list"),
    ({"suites": [{"name": "x", "argv": ["a"]}, {"name": "x", "argv": ["b"]}]},
     "unique"),
    ({"bootstrap": []}, "non-empty list"),
    ({"close": {"merge": "rebase"}}, "close.merge"),
    ({"close": {"merge": "local", "push": True}}, "merge and ci_workflow only"),
    ({"suite_pin": 3}, "suite_pin"),
    ([], "JSON object"),
])
def test_parse_config_rejects_a_typo_rather_than_defaulting(bad, words):
    with pytest.raises(config.ConfigError, match=words):
        config.parse_config(bad)


def test_load_config_without_a_file_is_all_defaults(tmp_path):
    cfg = config.load_config(tmp_path)
    assert cfg == config.Config() and cfg.source is None


def test_load_config_refuses_unreadable_json(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "project-workflow.json").write_text("{", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="not readable JSON"):
        config.load_config(tmp_path)


def test_config_for_prefers_the_project_tree_copy(tmp_path):
    main, wt = tmp_path / "main", tmp_path / "wt"
    for tree, branch in ((main, "main"), (wt, "trunk")):
        (tree / ".claude").mkdir(parents=True)
        (tree / ".claude" / "project-workflow.json").write_text(
            json.dumps({"base_branch": branch}), encoding="utf-8")
    assert config.config_for(main, wt).base_branch == "trunk"
    assert config.config_for(main, None).base_branch == "main"
    assert config.config_for(main, tmp_path / "bare").base_branch == "main"


def _linked(tmp_path: Path) -> tuple[Path, Path]:
    """(main checkout, linked worktree); main's config is written after the
    cut and left untracked, so only main carries it."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "commit", "-q", "--allow-empty", "-m", "seed")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    (main / ".claude").mkdir()
    (main / ".claude" / "project-workflow.json").write_text(
        json.dumps({"projects_dir": "docs/projects"}), encoding="utf-8")
    return main, wt


def test_config_at_falls_back_to_the_main_checkout(tmp_path):
    main, wt = _linked(tmp_path)
    assert config.config_at(wt).projects_dir == "docs/projects"
    assert config.config_at(main).projects_dir == "docs/projects"
    (wt / ".claude").mkdir()
    (wt / ".claude" / "project-workflow.json").write_text(
        json.dumps({"projects_dir": "wt-projects"}), encoding="utf-8")
    assert config.config_at(wt).projects_dir == "wt-projects"
    # Outside any repository only the tree itself is read.
    assert config.config_at(tmp_path / "loose") == config.Config()


def test_streams_projects_rel_falls_back_to_the_main_checkout(tmp_path):
    import streams
    main, wt = _linked(tmp_path)
    assert streams.projects_rel(wt) == Path("docs/projects")
    folder = wt / "docs" / "projects" / "current" / "project-0007-demo"
    folder.mkdir(parents=True)
    assert streams.discover_projects(wt) == {"0007": folder.resolve()}


def test_expand_argv_substitutes_the_worktree_and_the_interpreter(tmp_path):
    got = config.expand_argv(("python", "boot.py", "{worktree}", "--x={worktree}"),
                             tmp_path)
    wt = str(tmp_path.resolve())
    assert got == [sys.executable, "boot.py", wt, f"--x={wt}"]
    assert config.expand_argv(("npm", "ci"), tmp_path) == ["npm", "ci"]


def test_discovery_answers_from_the_cwd_in_a_linked_worktree(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "commit", "-q", "--allow-empty", "-m", "seed")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    (wt / "sub").mkdir()
    same = lambda a, b: Path(a).resolve() == Path(b).resolve()  # noqa: E731
    assert same(config.repo_toplevel(wt / "sub"), wt)
    assert same(config.main_checkout(wt / "sub"), main)
    assert same(config.git_common_dir(wt), main / ".git")
    assert same(config.git_common_dir(main), main / ".git")


def test_discovery_outside_a_repo_raises(tmp_path):
    for fn in (config.repo_toplevel, config.main_checkout, config.git_common_dir):
        with pytest.raises(config.RepoError):
            fn(tmp_path)
