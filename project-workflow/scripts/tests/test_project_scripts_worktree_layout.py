"""Worktree layout: `<repo>-worktrees/<NNNN>[-<tail>]`, with legacy siblings still resolved.

New worktrees are cut into one folder per repo; `find_project_worktree` must resolve
both that layout and the older `<repo>-<NNNN>[-<tail>]` sibling, or a close verb
falls back to the main checkout (see `AmbiguousWorktreeError` in `streams.py`).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
_LIB = ROOT / "scripts" / "lib"

PROJECTS = Path("C:/work")
MAIN = PROJECTS / "demo-repo"
NEST = PROJECTS / "demo-repo-worktrees"


@pytest.fixture(scope="module")
def streams():
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    spec = importlib.util.spec_from_file_location("_streams_layout_under_test", _LIB / "streams.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def listed(streams, monkeypatch):
    """Stub `git worktree list` and the repo name; returns a setter for the listing."""
    paths: list[Path] = []
    monkeypatch.setattr(streams, "repo_basename", lambda: "demo-repo")
    monkeypatch.setattr(streams, "_iter_worktree_paths", lambda _main: list(paths))

    def set_paths(*ps: Path) -> None:
        paths[:] = [MAIN, *ps]
    return set_paths


def test_worktree_path_nests_under_repo_worktrees(streams):
    assert streams.worktree_path("demo-repo", "0065", "data-sweep", MAIN) == \
        NEST / "0065-data-sweep"
    assert streams.worktree_path("demo-repo", "0070", "S-01", MAIN) == NEST / "0070-S-01"
    assert streams.worktree_path("demo-repo", "0071", None, MAIN) == NEST / "0071"


@pytest.mark.parametrize("path, key", [
    (NEST / "0065-data-sweep", "demo-repo-0065-data-sweep"),
    (PROJECTS / "demo-repo-0065", "demo-repo-0065"),
    (NEST / "fix-worktree-layout", "demo-repo-fix-worktree-layout"),
    (MAIN, "demo-repo"),
])
def test_worktree_key_maps_both_layouts(streams, path, key):
    assert streams.worktree_key(path, "demo-repo") == key


@pytest.mark.parametrize("tail, kind", [("data-sweep", "project"), ("S-01", "stream"), (None, "project")])
def test_new_layout_round_trips_through_classifier(streams, tail, kind):
    p = streams.worktree_path("demo-repo", "0065", tail, MAIN)
    assert streams.parse_worktree_name(streams.worktree_key(p, "demo-repo"), "demo-repo") == \
        (kind, "0065", tail)


def test_non_project_branch_folder_is_other(streams):
    key = streams.worktree_key(NEST / "fix-worktree-layout", "demo-repo")
    assert streams.parse_worktree_name(key, "demo-repo")[0] == "other"


def test_find_project_worktree_resolves_nested(streams, listed):
    listed(NEST / "0065-data-sweep", NEST / "fix-worktree-layout")
    assert streams.find_project_worktree("0065", MAIN) == NEST / "0065-data-sweep"
    assert streams.find_project_worktree("0065", MAIN, slug="data-sweep") == \
        NEST / "0065-data-sweep"


def test_find_project_worktree_still_resolves_legacy_sibling(streams, listed):
    listed(PROJECTS / "demo-repo-0065")
    assert streams.find_project_worktree("0065", MAIN) == PROJECTS / "demo-repo-0065"


def test_find_project_worktree_ambiguous_across_layouts(streams, listed):
    listed(PROJECTS / "demo-repo-0065", NEST / "0065-data-sweep")
    with pytest.raises(streams.AmbiguousWorktreeError):
        streams.find_project_worktree("0065", MAIN)
