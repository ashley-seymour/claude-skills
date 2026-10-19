"""wave-close.py: worktree lookup, gates, return check and owns parsing.

The old `f"-{project_id}" in p.name` heuristic matched only the legacy sibling
layout `<repo>-<NNNN>`; under `<repo>-worktrees/<NNNN>-<slug>` it never matched
and the gate silently verified the main checkout instead of the project tree.
These tests feed fake `git worktree list --porcelain` output.

The lookup lives in `lib/streams.py` (`resolve_project_tree` /
`find_project_tree`), shared with close-phase.py, so the resolver tests call
the `streams` module wave-close.py imported and patch it there.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
_BIN = ROOT / "scripts" / "bin"
_LIB = ROOT / "scripts" / "lib"


@pytest.fixture(scope="module")
def wc():
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    spec = importlib.util.spec_from_file_location(
        "_wave_close_under_test", _BIN / "wave-close.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def streams(wc):
    # The same module object wave-close.py imported its lookup from.
    import streams as mod
    return mod


@pytest.fixture
def layout(tmp_path, streams, monkeypatch):
    """A fake projects dir: main checkout plus a worktrees folder."""
    monkeypatch.setattr(streams, "repo_basename", lambda: "demo-repo")
    main = tmp_path / "demo-repo"
    nest = tmp_path / "demo-repo-worktrees"
    main.mkdir()
    nest.mkdir()
    return tmp_path, main, nest


def porcelain(*entries: tuple[Path, str | None]) -> str:
    blocks = []
    for path, branch in entries:
        lines = [f"worktree {path.as_posix()}", "HEAD " + "0" * 40]
        lines.append(f"branch refs/heads/{branch}" if branch else "detached")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def resolve(streams, main, text, pid="0070"):
    return streams.resolve_project_tree(pid, main, streams.parse_porcelain(text),
                                        "demo-repo")


def test_new_layout_matches(streams, layout):
    _tmp, main, nest = layout
    wt = nest / "0070-token-audit"
    wt.mkdir()
    text = porcelain((main, "main"), (wt, "project/0070-token-audit"),
                     (nest / "0065-data-sweep", "project/0065-data-sweep"))
    assert resolve(streams, main, text) == wt


def test_legacy_layout_matches(streams, layout):
    tmp, main, _nest = layout
    wt = tmp / "demo-repo-0070"
    wt.mkdir()
    text = porcelain((main, "main"), (wt, "project/0070-token-audit"))
    assert resolve(streams, main, text) == wt


def test_no_false_prefix_match(streams, layout):
    tmp, main, nest = layout
    for name in ("10070-other", "0070x-other"):
        (nest / name).mkdir()
    (tmp / "demo-repo-10070").mkdir()
    text = porcelain((main, "main"),
                     (nest / "10070-other", "project/10070-other"),
                     (nest / "0070x-other", "project/0070x-other"),
                     (tmp / "demo-repo-10070", None))
    assert resolve(streams, main, text) is None


def test_stream_worktree_is_not_the_project_tree(streams, layout):
    _tmp, main, nest = layout
    (nest / "0070-S-01").mkdir()
    text = porcelain((main, "main"), (nest / "0070-S-01", "stream/0070-S-01"))
    assert resolve(streams, main, text) is None


def test_ambiguous_refuses(streams, layout):
    tmp, main, nest = layout
    a, b = nest / "0070-token-audit", tmp / "demo-repo-0070"
    a.mkdir()
    b.mkdir()
    text = porcelain((main, "main"), (a, "project/0070-token-audit"), (b, None))
    with pytest.raises(streams.WorktreeLookupError, match="Remove the stray"):
        resolve(streams, main, text)


def test_unclassifiable_project_branch_worktree_refuses(streams, layout):
    tmp, main, _nest = layout
    odd = tmp / "scratch-tree"
    odd.mkdir()
    text = porcelain((main, "main"), (odd, "project/0070-token-audit"))
    with pytest.raises(streams.WorktreeLookupError, match="does not classify"):
        resolve(streams, main, text)


def test_missing_on_disk_refuses(streams, layout):
    _tmp, main, nest = layout
    text = porcelain((main, "main"),
                     (nest / "0070-token-audit", "project/0070-token-audit"))
    with pytest.raises(streams.WorktreeLookupError, match="missing on disk"):
        resolve(streams, main, text)


def test_git_failure_refuses(streams, layout, monkeypatch):
    _tmp, main, _nest = layout

    def boom(_main):
        raise OSError("git not found")
    monkeypatch.setattr(streams, "worktree_porcelain", boom)
    with pytest.raises(streams.WorktreeLookupError, match="worktree list"):
        streams.find_project_tree("0070", main)


# --- resolver edge cases, in the shared helper ------------------------------

def test_main_on_project_branch_is_the_tree(streams, layout):
    _tmp, main, _nest = layout
    text = porcelain((main, "project/0070-token-audit"))
    assert resolve(streams, main, text) == main


def test_main_on_project_branch_still_checks_ambiguity(streams, layout):
    # The early return for main-on-branch skipped this check.
    _tmp, main, nest = layout
    wt = nest / "0070-token-audit"
    wt.mkdir()
    text = porcelain((main, "project/0070-token-audit"), (wt, "scratch"))
    with pytest.raises(streams.WorktreeLookupError, match="main checkout"):
        resolve(streams, main, text)


def test_legacy_sibling_0070x_does_not_match(streams, layout):
    tmp, main, _nest = layout
    (tmp / "demo-repo-0070x").mkdir()
    text = porcelain((main, "main"), (tmp / "demo-repo-0070x", None))
    assert resolve(streams, main, text) is None


def test_legacy_sibling_00700_does_not_match(streams, layout):
    tmp, main, _nest = layout
    (tmp / "demo-repo-00700").mkdir()
    text = porcelain((main, "main"), (tmp / "demo-repo-00700", None))
    assert resolve(streams, main, text) is None


@pytest.mark.parametrize("parent, leaf", [
    ("Demo-Repo-Worktrees", "0070-token-audit"),
    ("DEMO-REPO-WORKTREES", "0070-token-audit"),
    (None, "Demo-Repo-0070"),
])
def test_worktree_key_ignores_repo_case(streams, parent, leaf):
    # Windows paths are case-insensitive, worktree_key was not.
    base = Path("C:/work")
    path = base / parent / leaf if parent else base / leaf
    kind, pid, _tail = streams.parse_worktree_name(
        streams.worktree_key(path, "demo-repo"), "demo-repo")
    assert (kind, pid) == ("project", "0070")


def test_worktree_key_keeps_the_stream_tail_case(streams):
    key = streams.worktree_key(Path("C:/work/DEMO-REPO-WORKTREES/0070-S-01"),
                               "demo-repo")
    assert streams.parse_worktree_name(key, "demo-repo") == ("stream", "0070", "S-01")


def test_wave_close_has_no_local_lookup_copy(wc, streams):
    # One helper, two callers: wave-close.py must not grow its own copy back.
    assert not hasattr(wc, "resolve_worktree")
    assert wc.find_project_tree is streams.find_project_tree


def _run_main(wc, streams, monkeypatch, main, text, capsys):
    monkeypatch.setattr(wc, "main_checkout", lambda: main)
    monkeypatch.setattr(streams, "worktree_porcelain", lambda _m: text)
    rc = wc.main(["wave-close.py", "0070", "--skip-suites", "--owns", "x"])
    return rc, capsys.readouterr()


def test_main_uses_new_layout_worktree(wc, streams, layout, monkeypatch, capsys):
    _tmp, main, nest = layout
    wt = nest / "0070-token-audit"
    wt.mkdir()
    text = porcelain((main, "main"), (wt, "project/0070-token-audit"))
    _rc, out = _run_main(wc, streams, monkeypatch, main, text, capsys)
    assert f"  tree: {wt}" in out.out
    assert "tree source: project worktree" in out.out


def test_main_no_worktree_falls_back_to_main_with_message(wc, streams, layout, monkeypatch, capsys):
    _tmp, main, _nest = layout
    text = porcelain((main, "main"))
    _rc, out = _run_main(wc, streams, monkeypatch, main, text, capsys)
    assert "no separate worktree for project 0070" in out.out
    assert f"  tree: {main}" in out.out


def test_main_ambiguous_exits_nonzero(wc, streams, layout, monkeypatch, capsys):
    tmp, main, nest = layout
    a, b = nest / "0070-token-audit", tmp / "demo-repo-0070"
    a.mkdir()
    b.mkdir()
    text = porcelain((main, "main"), (a, "project/0070-token-audit"), (b, None))
    rc, out = _run_main(wc, streams, monkeypatch, main, text, capsys)
    assert rc != 0
    assert "REFUSE" in out.err and "--root" in out.err
    assert "  tree:" not in out.out


def test_main_on_project_branch_verifies_main_and_says_so(wc, streams, layout,
                                                         monkeypatch, capsys):
    _tmp, main, _nest = layout
    text = porcelain((main, "project/0070-token-audit"))
    _rc, out = _run_main(wc, streams, monkeypatch, main, text, capsys)
    assert "main checkout is on the project 0070 branch" in out.out
    assert f"  tree: {main}" in out.out


def test_parse_owns_default_root_follows_a_two_segment_projects_dir(wc, tmp_path):
    # STATUS.md sits at <root>/docs/projects/current/project-0007-demo/, one
    # level deeper than under the default `my-projects`.
    tree = tmp_path / "tree"
    (tree / ".claude").mkdir(parents=True)
    (tree / ".claude" / "project-workflow.json").write_text(
        '{"projects_dir": "docs/projects"}', encoding="utf-8")
    folder = tree / "docs" / "projects" / "current" / "project-0007-demo"
    folder.mkdir(parents=True)
    status = folder / "STATUS.md"
    status.write_text("owns: src/a.py, <proj>/notes.md\n", encoding="utf-8")
    got = wc.parse_owns(status)
    assert got.paths == ["docs/projects/current/project-0007-demo/notes.md",
                         "src/a.py"]
    assert got.foreign == []
    assert wc.parse_owns(status, tree).paths == got.paths


def test_parse_owns_without_root_refuses_an_unplaceable_status(wc, tmp_path):
    folder = tmp_path / "loose" / "a" / "b"
    folder.mkdir(parents=True)
    (folder / "STATUS.md").write_text("owns: x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="pass `root`"):
        wc.parse_owns(folder / "STATUS.md")


def test_find_status_without_projects_dir_falls_back_to_main_config(wc, tmp_path):
    # A linked worktree with no config file of its own: main's applies.
    git = lambda repo, *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a],
        check=True, capture_output=True, text=True)
    main = tmp_path / "main"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "commit", "-q", "--allow-empty", "-m", "seed")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    (main / ".claude").mkdir()
    (main / ".claude" / "project-workflow.json").write_text(
        '{"projects_dir": "docs/projects"}', encoding="utf-8")
    folder = wt / "docs" / "projects" / "current" / "project-0007-demo"
    folder.mkdir(parents=True)
    (folder / "STATUS.md").write_text("---\n---\n", encoding="utf-8")
    assert wc.find_status("0007", wt) == folder / "STATUS.md"


# --- owns form, return wiring, project suite file ----------------

import json  # noqa: E402
import subprocess  # noqa: E402


@pytest.fixture(scope="module")
def sb(wc):
    # The same suite_baseline module wave-close.py imported.
    import suite_baseline as mod
    return mod


def _status(tree: Path, body: str) -> Path:
    folder = tree / "my-projects" / "current" / "project-0070-token-audit"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "STATUS.md"
    path.write_text(f'---\nproject: "0070"\n---\n\n{body}\n', encoding="utf-8")
    return path


def test_parse_owns_names_the_parseable_form(wc, tmp_path):
    # The wave-1 STATUS lines, verbatim in shape. The old parser returned only
    # prose fragments for these (`its`, `tests`, `reference.md}` ...), 0 paths.
    tree = tmp_path / "wt"
    cfg = tmp_path / "cfg"
    status = _status(tree, "\n".join([
        "owns:     <cfg-wt>/agents/{stream-worker,reviewer}.md",
        "owns:     <wt>/tools/scripts/bin/new-project.py, its tests",
        "owns:     deliverable only",
        "owns:     `docs/{a,b/{c,d}}.md`, src/x.py",
        f"owns:     {(tree / 'tests' / 'test_x.py').as_posix()}, {(cfg / 'y.md').as_posix()}",
        "owns:     <wt>/scripts/**",
    ]))
    got = wc.parse_owns(status, tree)
    assert got.paths == sorted([
        "tools/scripts/bin/new-project.py", "docs/a.md", "docs/b/c.md",
        "docs/b/d.md", "src/x.py", "tests/test_x.py", "scripts/**"])
    assert got.foreign == sorted([
        "<cfg-wt>/agents/{stream-worker,reviewer}.md", (cfg / "y.md").as_posix()])
    assert got.unparseable == ["deliverable only", "its tests"]


def test_parse_owns_never_turns_the_whole_tree_into_an_allowlist(wc, tmp_path):
    tree = tmp_path / "wt"
    status = _status(tree, f"owns: <wt>, {tree.as_posix()}")
    got = wc.parse_owns(status, tree)
    assert got.paths == []
    assert len(got.unparseable) == 2


def test_outside_owns_handles_absolute_foreign_and_glob_entries(wc, tmp_path):
    root = tmp_path / "wt"
    owns = [str(root / "a" / "b.py"), (tmp_path / "cfg" / "x.md").as_posix(),
            "docs/**", "src/*.py"]
    dirty = ["a/b.py", "docs/deep/n.md", "src/m.py", "src/sub/m.py", "x.md"]
    assert wc.outside_owns(dirty, owns, root) == ["src/sub/m.py", "x.md"]


def test_dirty_paths_lists_untracked_files_one_by_one(wc, tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "newdir").mkdir()
    (repo / "newdir" / "a.md").write_text("a", encoding="utf-8")
    (repo / "newdir" / "b.md").write_text("b", encoding="utf-8")
    # Collapsed to `newdir/`, a per-file owns list would refuse both files.
    assert sorted(wc.dirty_paths(repo)) == ["newdir/a.md", "newdir/b.md"]


def _pin(path: Path, tests_passed: int = 2396, sim_passed: int = 779) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    suites = {}
    for suite, passed, xfailed in (("tests/", tests_passed, 2),
                                   ("engine/tests/", sim_passed, 0)):
        suites[suite] = {"suite": suite, "exit_code": 0, "passed": passed,
                         "failed": 0, "skipped": 0, "xfailed": xfailed,
                         "xpassed": 0, "errors": 0, "deselected": 0,
                         "ran_nothing": False, "skip_reasons": []}
    path.write_text(json.dumps({"suites": suites}), encoding="utf-8")
    return path


def _expected(path: Path, tests_expected: int = 2391, adjust: int = -5) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "project": "0070",
        "pin": {"suites": {"tests/": {"passed": 2396, "xfailed": 2},
                           "engine/tests/": {"passed": 779, "xfailed": 0}}},
        "adjustments": [{"suite": "tests/", "passed": adjust, "by": "S-07",
                         "why": "retired"}],
        "expected": {"tests/": {"passed": tests_expected, "xfailed": 2},
                     "engine/tests/": {"passed": 779, "xfailed": 0}},
    }), encoding="utf-8")
    return path


def test_load_retirements_sums_only_the_negative_rows(sb, tmp_path):
    path = _expected(tmp_path / "e.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["adjustments"] += [
        {"suite": "tests/", "passed": 40, "by": "S-08", "why": "added"},
        {"suite": "tests/", "passed": -2, "by": "S-09", "why": "retired"},
        {"suite": "engine/tests/", "passed": -1, "by": "S-09", "why": "retired"},
    ]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert sb.load_retirements(path) == {"tests/": -7, "engine/tests/": -1}


def test_load_retirements_ignores_pin_and_expected_figures(sb, tmp_path):
    # O-6: the file is read only for its retirements. A stale `pin` block or an
    # `expected` that does not add up no longer refuses.
    path = _expected(tmp_path / "e.json", tests_expected=9999)
    assert sb.load_retirements(path) == {"tests/": -5}


def test_load_retirements_refuses_a_retirement_without_a_why(sb, tmp_path):
    path = _expected(tmp_path / "e.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["adjustments"][0]["why"] = " "
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(sb.ExpectedBaselineError, match="why"):
        sb.load_retirements(path)


def test_resolve_floor_lowers_the_pin_by_the_retirements(sb, tmp_path):
    floor, label = sb.resolve_floor(_pin(tmp_path / "pin.json"),
                                    _expected(tmp_path / "e.json"))
    assert (floor["tests/"].passed, floor["engine/tests/"].passed) == (2391, 779)
    assert "tests/ -5" in label


@pytest.mark.parametrize("passed, skipped, failed, want", [
    (2391, 0, 0, []),
    (2392, 0, 0, []),                                   # an addition passes
    (2390, 0, 0, ["PASSED-COUNT REGRESSION"]),          # a lost test
    (2391, 1, 0, ["skipped must be 0"]),                # a skip, count held
    (2391, 0, 1, ["failed"]),
])
def test_compare_is_a_floor_with_zero_skips(sb, passed, skipped, failed, want):
    floor = sb.SuiteResult(suite="tests/", passed=2391, xfailed=2)
    actual = sb.SuiteResult(suite="tests/", passed=passed, skipped=skipped,
                            failed=failed, xfailed=2,
                            command="python -m pytest tests/ -q")
    problems = sb.compare(floor, actual)
    assert len(problems) == len(want)
    for problem, text in zip(problems, want):
        assert text in problem
        # Every refusal ends with the command or edit that resolves it.
        assert problem.rstrip().endswith(("`", "suite-expected.json")), problem


def _suites(sb, *names: str):
    return tuple(sb.Suite(n, ("python", "-m", "pytest", n, "-q")) for n in names)


def test_run_all_runs_the_suites_concurrently(sb, monkeypatch, tmp_path):
    # R-8c: one subprocess per suite at once. Run one after the other, the
    # first call would wait at the barrier alone and time out.
    import threading
    barrier = threading.Barrier(2, timeout=10)

    def run_suite(suite, cwd):
        barrier.wait()
        return sb.SuiteResult(suite=suite.name, passed=1)
    monkeypatch.setattr(sb, "run_suite", run_suite)
    got = sb.run_all(tmp_path, _suites(sb, "a/", "b/"))
    assert list(got) == ["a/", "b/"]
    assert all(r.passed == 1 for r in got.values())


def test_run_suite_runs_the_configured_argv_in_the_tree(sb, tmp_path):
    # The suite is the config's argv, run from the tree under test.
    (tmp_path / "test_one.py").write_text("def test_a():\n    pass\n",
                                          encoding="utf-8")
    suite = sb.Suite("here", ("python", "-m", "pytest", "-q",
                              "-p", "no:cacheprovider", "test_one.py"))
    result = sb.run_suite(suite, tmp_path)
    assert (result.suite, result.passed, result.exit_code) == ("here", 1, 0)
    assert result.command == "python -m pytest -q -p no:cacheprovider test_one.py"


PIN_REL = "pins/suite-baseline.json"


def _write_config(main: Path, **over) -> None:
    doc = {"suites": [{"name": "tests/", "argv": ["python", "-m", "pytest", "tests/"]},
                      {"name": "engine/tests/", "argv": ["python", "-m", "pytest",
                                                      "engine/tests/"]}],
           "suite_pin": PIN_REL}
    doc.update(over)
    (main / ".claude").mkdir(parents=True, exist_ok=True)
    (main / ".claude" / "project-workflow.json").write_text(json.dumps(doc),
                                                            encoding="utf-8")


def _gate_tree(tmp_path, monkeypatch, wc, **config):
    main = tmp_path / "main"
    _pin(main / PIN_REL)
    _write_config(main, **config)
    monkeypatch.setattr(wc, "main_checkout", lambda: main)
    tree = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    status = _status(tree, "")
    return tree, status


def _fake_run_all(sb, tests_passed, skipped=0):
    def run_all(_root, suites):
        assert [s.name for s in suites] == ["tests/", "engine/tests/"]
        return {"tests/": sb.SuiteResult(suite="tests/", passed=tests_passed,
                                         xfailed=2, skipped=skipped),
                "engine/tests/": sb.SuiteResult(suite="engine/tests/", passed=779)}
    return run_all


@pytest.mark.parametrize("tests_passed, rc", [(2390, 1), (2391, 0), (2392, 0),
                                              (2396, 0)])
def test_main_lowers_the_floor_by_declared_retirements(wc, sb, tmp_path, monkeypatch,
                                                       capsys, tests_passed, rc):
    # O-6: pin 2396 minus the file's one retirement (-5) is a floor of 2391;
    # anything at or above it passes, additions included.
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    _expected(status.parent / "suite-expected.json")
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, tests_passed))
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "my-projects/**"])
    out = capsys.readouterr()
    assert got == rc, out.err
    assert "lowered by the retirements" in out.out and "tests/ -5" in out.out
    if rc:
        assert "PASSED-COUNT REGRESSION" in out.err


def test_main_malformed_expected_file_refuses_with_the_fix(wc, sb, tmp_path,
                                                          monkeypatch, capsys):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    (status.parent / "suite-expected.json").write_text("{not json",
                                                       encoding="utf-8")
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2396))
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "my-projects/**"])
    assert got == 1
    assert "Fix or delete" in capsys.readouterr().err


def test_main_baseline_flag_overrides_the_expected_file(wc, sb, tmp_path,
                                                        monkeypatch, capsys):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    _expected(status.parent / "suite-expected.json")
    floor = _pin(tmp_path / "floor.json", tests_passed=2391)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2392))
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "my-projects/**", "--baseline", str(floor)])
    assert got == 0
    assert "(floor)" in capsys.readouterr().out


def test_main_without_expected_file_falls_back_to_the_pin(wc, sb, tmp_path,
                                                          monkeypatch, capsys):
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2391))
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "my-projects/**"])
    out = capsys.readouterr()
    assert got == 1
    assert "floor: shared pin" in out.out and "PASSED-COUNT REGRESSION" in out.err


def _payload(path: Path, updated: list[str], **extra) -> Path:
    body = {"status": "ok", "updated_paths": updated, "found": [],
            "summary_1line": "done"}
    body.update(extra)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_main_return_runs_the_skill_contract_check(wc, tmp_path, monkeypatch,
                                                   capsys):
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    cfg_file = (tmp_path / "cfg" / "skills" / "x.md").as_posix()
    good = _payload(tmp_path / "good.json",
                    [str(tree / "src" / "a.py"), "src/b.py", cfg_file])
    bad = _payload(tmp_path / "bad.json", [str(tree / "src" / "c.py")])
    base = ["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
            "--owns", "src/a.py", "--owns", "src/b.py", "--owns", cfg_file,
            "--owns", "my-projects/**"]
    assert wc.main(base + ["--return", f"S-01:{good}"]) == 0
    assert "return ok: S-01" in capsys.readouterr().out
    assert wc.main(base + ["--return", f"S-02:{bad}"]) == 1
    assert "is in no owns entry" in capsys.readouterr().err


def test_main_return_with_no_owns_refuses(wc, tmp_path, monkeypatch, capsys):
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    good = _payload(tmp_path / "good.json", [])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--return", f"S-01:{good}"])
    assert got == 1
    assert "Pass `--owns <path>` per file" in capsys.readouterr().err


@pytest.mark.parametrize("raw, want", [
    ("./my-projects/WORKFLOW.md", "my-projects/WORKFLOW.md"),
    ("<wt>/tests/a.py", "tests/a.py"),
    (".\\src\\a.py", "src/a.py"),
    ("<cfg-wt>/skills/x.md", "<cfg-wt>/skills/x.md"),
    ("<wt>", ""),
    ("{{wt}}/tests/a.py", "tests/a.py"),
    ("{{cfg-wt}}/skills/x.md", "<cfg-wt>/skills/x.md"),
])
def test_normalise_owns_arg(wc, raw, want):
    assert wc.normalise_owns_arg(raw) == want


def test_outside_owns_accepts_dot_slash_entries(wc, tmp_path):
    # `./x` covered nothing, so the named file read as outside.
    dirty = ["my-projects/WORKFLOW.md", "tests/a.py"]
    assert wc.outside_owns(dirty, ["./my-projects/WORKFLOW.md", "./tests/a.py"],
                           tmp_path) == []


def test_main_same_owns_list_passes_both_gates(wc, tmp_path, monkeypatch, capsys):
    # One --owns list written `./x` / `<wt>/x` must pass gate 1
    # (returns) and gate 2 (git status) alike.
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    (tree / "src").mkdir()
    (tree / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (tree / "src" / "b.py").write_text("b\n", encoding="utf-8")
    good = _payload(tmp_path / "good.json",
                    [str(tree / "src" / "a.py"), str(tree / "src" / "b.py")])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "./src/a.py", "--owns", "<wt>/src/b.py",
                   "--owns", "my-projects/**", "--return", f"S-01:{good}"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "return ok: S-01" in out.out
    assert "outside owns" not in out.out


# --- gate 2 follows absolute owns entries into other repos ------

def _second_repo(tmp_path: Path) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    subprocess.run(["git", "init", "-q", str(cfg)], check=True)
    return cfg


def _cross_args(tree: Path, *owned: Path) -> list[str]:
    args = ["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
            "--owns", "my-projects/**"]
    for path in owned:
        args += ["--owns", path.as_posix()]
    return args


def test_main_refuses_a_dirty_path_outside_owns_in_a_second_repo(
        wc, tmp_path, monkeypatch, capsys):
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    cfg = _second_repo(tmp_path)
    (cfg / "a.md").write_text("a", encoding="utf-8")
    (cfg / "b.md").write_text("b", encoding="utf-8")
    got = wc.main(_cross_args(tree, cfg / "a.md"))
    out = capsys.readouterr()
    assert got == 1
    assert "other repos checked:" in out.out
    assert "outside owns in" in out.out and "b.md" in out.out
    assert "outside owns in" in out.err or "another repo" in out.err


def test_main_passes_when_second_repo_dirt_is_inside_owns(
        wc, tmp_path, monkeypatch, capsys):
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    cfg = _second_repo(tmp_path)
    (cfg / "skills").mkdir()
    (cfg / "skills" / "a.md").write_text("a", encoding="utf-8")
    got = wc.main(_cross_args(tree, cfg / "skills" / "a.md"))
    out = capsys.readouterr()
    assert got == 0, out.err
    assert str(cfg).replace("\\", "/") in out.out.replace("\\", "/")
    assert "no dirty path outside the owns union in those repos" in out.out


def test_main_reports_an_absolute_owns_entry_with_no_repo(
        wc, tmp_path, monkeypatch, capsys):
    tree, _status_path = _gate_tree(tmp_path, monkeypatch, wc)
    got = wc.main(_cross_args(tree, tmp_path / "nowhere" / "x.md"))
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "NOT CHECKED" in out.out


# --- `owns:` parsing scoped to one wave -------------------------
# The parser unioned every wave's `owns:` lines, so a wave-4 stream writing a
# path only wave 1 owned passed gate 2.

WAVED_BODY = "\n".join([
    "## Resume", "", "state: x", "",
    "## Wave 1 — first  (status: done)", "",
    "### S-01 — old (stream-worker)",
    "owns:     src/old.py", "",
    "## Wave 2 — second  (status: in progress)", "",
    "```", "## Wave 9 — quoted inside a fence, not a heading", "```",
    "### S-02 — new (stream-worker)",
    "owns:     src/new.py, my-projects/**", "",
    "## Wave 20 — must not match wave 2", "",
    "owns:     src/twenty.py", "",
    "## Log", "", "- wave 1",
])


def _waved_status(tree: Path, fm: str = "current_wave: 2\n") -> Path:
    folder = tree / "my-projects" / "current" / "project-0070-token-audit"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "STATUS.md"
    path.write_text(f'---\nproject: "0070"\n{fm}---\n\n{WAVED_BODY}\n',
                    encoding="utf-8")
    return path


@pytest.mark.parametrize("fm, want", [
    ("current_wave: 4\n", 4), ("current_wave: '4'\n", 4),
    ("current_phase: 3\n", None), ("current_wave: next\n", None),
])
def test_current_wave_reads_the_frontmatter(wc, fm, want):
    assert wc.current_wave(f"---\nproject: x\n{fm}---\n\n# S\n") == want


def test_wave_section_ends_at_the_next_heading_outside_a_fence(wc):
    text = f"---\nx: 1\n---\n\n{WAVED_BODY}\n"
    two = wc.wave_section(text, 2)
    assert "src/new.py" in two and "### S-02" in two
    assert "src/old.py" not in two and "src/twenty.py" not in two
    assert "src/twenty.py" in wc.wave_section(text, 20)
    assert wc.wave_section(text, 9) is None
    assert wc.wave_section(text, 3) is None


def test_parse_owns_scopes_to_the_named_wave(wc, tmp_path):
    tree = tmp_path / "wt"
    status = _waved_status(tree)
    got = wc.parse_owns(status, tree, wave=1)
    assert got.paths == ["src/old.py"]
    assert got.scope == "`## Wave 1` only" and got.wave == 1
    assert wc.parse_owns(status, tree, wave=2).paths == ["my-projects/**",
                                                        "src/new.py"]


def test_parse_owns_falls_back_to_the_union_without_a_section(wc, tmp_path):
    tree = tmp_path / "wt"
    status = _waved_status(tree)
    union = ["my-projects/**", "src/new.py", "src/old.py", "src/twenty.py"]
    for wave in (None, 7):
        got = wc.parse_owns(status, tree, wave=wave)
        assert got.paths == union
        assert got.wave is None and got.scope.startswith("every wave's")
    assert "no `## Wave 7` section" in wc.parse_owns(status, tree, wave=7).scope


def _dirty_old(tmp_path, monkeypatch, wc, fm: str = "current_wave: 2\n"):
    main = tmp_path / "main"
    monkeypatch.setattr(wc, "main_checkout", lambda: main)
    tree = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    _waved_status(tree, fm)
    (tree / "src").mkdir()
    (tree / "src" / "old.py").write_text("x\n", encoding="utf-8")
    return tree


def test_main_defaults_to_the_current_wave_section(wc, tmp_path, monkeypatch,
                                                   capsys):
    tree = _dirty_old(tmp_path, monkeypatch, wc)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert got == 1
    assert "`## Wave 2` only" in out.out
    assert "outside owns: src/old.py" in out.out


def test_main_wave_flag_picks_the_section(wc, tmp_path, monkeypatch, capsys):
    # Wave 1 owns src/old.py but not the STATUS.md the fixture left untracked;
    # STATUS.md is an orchestrator ledger, so nothing escapes.
    tree = _dirty_old(tmp_path, monkeypatch, wc)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--wave", "1"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "`## Wave 1` only" in out.out
    assert "outside owns: src/old.py" not in out.out
    assert "exempt (orchestrator ledgers): my-projects/" in out.out


@pytest.mark.parametrize("flags, fm, scope", [
    (["--all-waves"], "current_wave: 2\n", "every wave's"),
    ([], "current_phase: 2\n", "every wave's"),
    ([], "current_wave: 5\n", "no `## Wave 5` section"),
])
def test_main_all_waves_and_the_union_fallback(wc, tmp_path, monkeypatch, capsys,
                                               flags, fm, scope):
    tree = _dirty_old(tmp_path, monkeypatch, wc, fm)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   *flags])
    out = capsys.readouterr()
    assert scope in out.out
    assert got == 0, out.err


def test_main_explicit_wave_without_a_section_refuses(wc, tmp_path, monkeypatch,
                                                      capsys):
    # An explicit --wave must not quietly widen to the union.
    tree = _dirty_old(tmp_path, monkeypatch, wc)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--wave", "5"])
    out = capsys.readouterr()
    assert got == 2
    assert "no `## Wave 5` section" in out.err
    assert "every wave's" not in out.out


def test_main_wave_and_all_waves_are_mutually_exclusive(wc, tmp_path,
                                                         monkeypatch, capsys):
    tree = _dirty_old(tmp_path, monkeypatch, wc)
    with pytest.raises(SystemExit) as exc:
        wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                 "--wave", "2", "--all-waves"])
    assert exc.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


# --- `<cfg-wt>/` expansion and Git-Bash drive paths ----

def _cfg_status(tree: Path, cfg: Path | None, owns_line: str) -> Path:
    folder = tree / "my-projects" / "current" / "project-0070-token-audit"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "STATUS.md"
    lines = ["---", 'project: "0070"', "current_wave: 1"]
    if cfg is not None:
        lines.append(f"cfg_worktree: {cfg.as_posix()}")
    lines += ["---", "", "## Wave 1 - w", "", "### S-01 - x (stream-worker)",
              f"owns:     my-projects/**, {owns_line}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_cfg_wt_owns_from_status_is_expanded_and_gated(
        wc, tmp_path, monkeypatch, capsys):
    # `<cfg-wt>/x` was skipped, so second-repo dirt went unchecked.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    cfg = _second_repo(tmp_path)
    (cfg / "a.md").write_text("a", encoding="utf-8")
    (cfg / "b.md").write_text("b", encoding="utf-8")
    _cfg_status(tree, cfg, "<cfg-wt>/a.md")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert got == 1, out.out
    assert "other repos checked:" in out.out
    assert "outside owns in" in out.out and "b.md" in out.out


def test_cfg_wt_owns_arg_is_expanded_and_gated(wc, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    cfg = _second_repo(tmp_path)
    (cfg / "a.md").write_text("a", encoding="utf-8")
    (cfg / "b.md").write_text("b", encoding="utf-8")
    _cfg_status(tree, cfg, "src/x.py")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "my-projects/**", "--owns", "<cfg-wt>/a.md"])
    out = capsys.readouterr()
    assert got == 1, out.out
    assert "outside owns in" in out.out and "b.md" in out.out


def test_cfg_wt_without_frontmatter_value_is_not_checked(
        wc, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    _cfg_status(tree, None, "<cfg-wt>/a.md")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "NOT CHECKED: <cfg-wt>/a.md" in out.out


@pytest.mark.skipif(sys.platform != "win32", reason="Git-Bash drive paths are Windows-only")
def test_gitbash_drive_path_normalises_and_resolves(wc, tmp_path):
    # `/d/x/y` must resolve as `D:/x/y`.
    drive, rest = tmp_path.as_posix().split(":", 1)
    bash = f"/{drive.lower()}{rest}/src/a.py"
    assert wc.normalise_owns_arg(bash) == f"{drive.upper()}:{rest}/src/a.py"
    assert wc.relative_to_tree(bash, tmp_path) == "src/a.py"


# --- the return check in-process ---------------------------------

def _return_args(tree: Path, *extra: str) -> list[str]:
    return ["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
            "--owns", "my-projects/**", "--owns", "src/**", *extra]


def test_return_unknown_key_accepted(wc, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    payload = _payload(tmp_path / "r.json", ["src/a.py"],
                       dispositions=[{"id": "W1"}], tokens_used=12, commit=None)
    got = wc.main(_return_args(tree, "--return", f"S-01:{payload}"))
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "return ok: S-01" in out.out


def test_return_long_summary_is_truncated_with_a_warning(wc, tmp_path,
                                                         monkeypatch, capsys):
    # A 122-char summary once refused the whole wave.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    summary = "x" * 122
    payload = _payload(tmp_path / "r.json", ["src/a.py"], summary_1line=summary)
    got = wc.main(_return_args(tree, "--return", f"S-01:{payload}"))
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "WARN: S-01: summary_1line is 122 chars" in out.out
    assert "x" * 117 + "..." in out.out and summary not in out.out


@pytest.mark.parametrize("body, text", [
    ({"status": "ok", "updated_paths": [], "summary_1line": "s"},
     "missing key(s) found"),
    ({"status": "done", "updated_paths": [], "found": [], "summary_1line": "s"},
     "status 'done'"),
    ({"status": "blocked", "updated_paths": [], "found": [], "summary_1line": "s"},
     "no blocked_reason"),
    ({"status": "ok", "updated_paths": "src/a.py", "found": [],
      "summary_1line": "s"}, "not a list of strings"),
    (["not", "an", "object"], "not a JSON object"),
])
def test_return_contract_refusal_names_the_payload_to_edit(
        wc, tmp_path, monkeypatch, capsys, body, text):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    path = tmp_path / "r.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    got = wc.main(_return_args(tree, "--return", f"S-01:{path}"))
    err = capsys.readouterr().err
    assert got == 1
    assert text in err and f"Edit {path}" in err


def test_return_payload_with_a_bom_is_read(wc, tmp_path, monkeypatch, capsys):
    # PowerShell 5.1 writes a UTF-8 BOM.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    path = tmp_path / "r.json"
    path.write_text(json.dumps({"status": "ok", "updated_paths": [], "found": [],
                                "summary_1line": "s"}), encoding="utf-8-sig")
    assert wc.main(_return_args(tree, "--return", f"S-01:{path}")) == 0


def test_return_path_escaping_the_tree_refuses(wc, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    payload = _payload(tmp_path / "r.json", ["src/../../outside.py"])
    got = wc.main(_return_args(tree, "--return", f"S-01:{payload}"))
    assert got == 1
    assert "escapes the tree" in capsys.readouterr().err


# --- scratch owns, owns-entry hints, ledgers and nested repos ----------------

def _scratch_status(tree: Path, scratch: Path, extra: str = "") -> Path:
    folder = tree / "my-projects" / "current" / "project-0070-token-audit"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "STATUS.md"
    path.write_text("\n".join([
        "---", 'project: "0070"', "current_wave: 1", "---", "",
        "## Wave 1 - w", "", "### S-01 - x (stream-worker)",
        f"owns:     src/a.py, {(scratch / 'hook-probe').as_posix()}/**{extra}",
        ""]), encoding="utf-8")
    return path


def test_return_absolute_scratch_owns_covers_path(wc, tmp_path, monkeypatch,
                                                       capsys):
    # STATUS owns `<scratch>/hook-probe/**` (absolute,
    # outside the tree) was parsed for gate 2 but never given to the return
    # check, so S-01's `<scratch>/hook-probe/.claude/settings.json` refused.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    scratch = tmp_path / "scratch"
    _scratch_status(tree, scratch)
    target = (scratch / "hook-probe" / ".claude" / "settings.json").as_posix()
    payload = _payload(tmp_path / "S-01.json", [target, "src/a.py"])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--return", f"S-01:{payload}"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "return ok: S-01" in out.out


def test_return_outside_owns_names_the_owns_entry(wc, tmp_path, monkeypatch,
                                                       capsys):
    # S-02's scratch path is in no owns entry. It must
    # still refuse, and the refusal must name the entry that would admit it.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    scratch = tmp_path / "scratch"
    status = _scratch_status(tree, scratch)
    stray = (scratch / "S-02" / "mutants.py").as_posix()
    payload = _payload(tmp_path / "S-02.json", [stray])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--return", f"S-02:{payload}"])
    err = capsys.readouterr().err
    assert got == 1
    assert f"S-02: updated path '{stray}' is in no owns entry" in err
    assert f"pass `--owns {stray}`" in err
    assert "to the wave's `owns:` line in" in err and status.name in err
    # --owns mode names the flag alone.
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "my-projects/**", "--return", f"S-02:{payload}"])
    assert got == 1
    assert f"Add `--owns {stray}` to this call" in capsys.readouterr().err


def test_orchestrator_ledgers_exempt_from_owns(wc, tmp_path, monkeypatch,
                                                    capsys):
    # The orchestrator's own uncommitted STATUS.md
    # counted as outside the owns union.
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    art = status.parent / "artifacts"
    art.mkdir()
    ledgers = [status.parent / "suite-expected.json", art / "follow-ups.md",
               art / "S-01-review-w1.json", art / "S-confirm-w3.json",
               art / "triage-w2.md"]
    for path in ledgers:
        path.write_text("x", encoding="utf-8")
    (tree / "src").mkdir()
    (tree / "src" / "a.py").write_text("a", encoding="utf-8")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "src/a.py"])
    out = capsys.readouterr()
    assert got == 0, out.err
    for path in [status, *ledgers]:
        rel = path.relative_to(tree).as_posix()
        assert rel in out.out.split("exempt (orchestrator ledgers): ", 1)[1]
    # Only the ledgers: another artifact in the same folder still refuses.
    (art / "S-01-notes.md").write_text("x", encoding="utf-8")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "src/a.py"])
    out = capsys.readouterr()
    assert got == 1
    assert "outside owns: my-projects/current/project-0070-token-audit/" \
           "artifacts/S-01-notes.md" in out.out


def _nested_repo(tmp_path: Path) -> Path:
    # `scratch/S-14/repo-strboot/` is its own git repo,
    # with a dirty `.claude/project-workflow.json`.
    repo = tmp_path / "scratch" / "S-14" / "repo-strboot"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "project-workflow.json").write_text("{}", encoding="utf-8")
    return repo


@pytest.mark.parametrize("source", ["--owns", "status"])
def test_trailing_slash_entry_covers_nested_repo(wc, tmp_path, monkeypatch,
                                                      capsys, source):
    # `<dir>/` with no glob is `<dir>/**`, also for a nested git repo.
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    repo = _nested_repo(tmp_path)
    entry = repo.as_posix() + "/"
    if source == "--owns":
        args = ["--owns", "my-projects/**", "--owns", entry]
    else:
        status.write_text(f'---\nproject: "0070"\n---\n\nowns: my-projects/**, '
                          f'{entry}\n', encoding="utf-8")
        args = []
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   *args])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "repo-strboot" in out.out.split("other repos checked:", 1)[1]


def test_as_dir_glob_reads_a_trailing_slash_as_the_whole_dir(wc, tmp_path):
    assert wc.as_dir_glob("D:/x/repo-strboot/") == "D:/x/repo-strboot/**"
    assert wc.as_dir_glob("src\\pkg\\") == "src\\pkg/**"
    assert wc.as_dir_glob("src/pkg") == "src/pkg"
    assert wc.as_dir_glob("src/*/") == "src/*/"
    # The tree itself stays a non-entry: the whole tree is not an allowlist.
    assert wc.as_dir_glob(tmp_path.as_posix() + "/", tmp_path) == \
        tmp_path.as_posix() + "/"
    out = wc.OwnsParse()
    wc.classify_owns_entry("<wt>/src/pkg/", tmp_path / "wt", out)
    assert out.paths == ["src/pkg/**"]


def test_parent_glob_covers_a_nested_repo(wc, tmp_path, monkeypatch, capsys):
    # `S-14/**` (no repo of its own) was NOT CHECKED, and did
    # not count for the nested repo another entry resolved.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    repo = _nested_repo(tmp_path)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "my-projects/**",
                   "--owns", repo.parent.as_posix() + "/**",
                   "--owns", (repo / "README.md").as_posix()])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "repo-strboot" in out.out.split("other repos checked:", 1)[1]


# --- owns from the brief's task cards ------------------------------

def _card(sid: str, after: str, owns: list[str]) -> str:
    lines = [f"### {sid} slug", "- Agent: stream-worker", f"- After: {after}",
             "- Mission: m", "- Owns:"]
    lines += [f"  - {o}" for o in owns]
    lines += ["- Read first: none", "- Decided: d", "- Done when:",
              "  - `true` -> ok", "- Stop and return: s", ""]
    return "\n".join(lines)


def _card_tree(tmp_path, monkeypatch, wc, fm: str = "current_wave: 1\n"):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    status.write_text(f'---\nproject: "0070"\n{fm}---\n\n'
                      f'## Wave 1\n\nowns: src/status_only.py\n', encoding="utf-8")
    brief = status.parent / "brief.md"
    brief.write_text("\n".join([
        "# Brief", "", "## Waves", "", "### Wave 1 -- first", "",
        _card("S-01", "none", ["`src/a.py` (new)", "<wt>/my-projects/**"]),
        "```", _card("S-09", "none", ["src/fenced.py"]), "```", "",
        "<!--", _card("S-08", "none", ["src/commented.py"]), "-->", "",
        "### Wave 2 -- second", "",
        _card("S-02", "S-01", ["src/b.py"]),
    ]), encoding="utf-8")
    (tree / "src").mkdir()
    for name in ("a.py", "b.py", "fenced.py", "commented.py", "status_only.py"):
        (tree / "src" / name).write_text("x", encoding="utf-8")
    return tree, brief


def test_card_brief_supplies_owns(wc, tmp_path, monkeypatch, capsys):
    # (1): with task cards in brief.md, owns come from their `Owns:` lists,
    # before the STATUS prose parse. Wave 1 (After depth 1) is S-01 only.
    tree, _brief = _card_tree(tmp_path, monkeypatch, wc)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert got == 1
    assert "wave 1 cards in brief.md (S-01)" in out.out
    escaped = sorted(line.split("outside owns: ", 1)[1]
                     for line in out.out.splitlines() if "outside owns: " in line)
    assert escaped == ["src/b.py", "src/commented.py", "src/fenced.py",
                       "src/status_only.py"]
    assert "the `Owns:` list of the stream's card in" in out.err


def test_card_owns_follow_the_after_depth(wc, tmp_path, monkeypatch, capsys):
    tree, _brief = _card_tree(tmp_path, monkeypatch, wc)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--wave", "2"])
    out = capsys.readouterr()
    assert got == 1
    assert "wave 2 cards in brief.md (S-02)" in out.out
    assert "outside owns: src/a.py" in out.out
    assert "outside owns: src/b.py" not in out.out


def test_card_owns_explicit_wave_with_no_card_refuses(wc, tmp_path, monkeypatch,
                                                      capsys):
    tree, _brief = _card_tree(tmp_path, monkeypatch, wc)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--wave", "3"])
    assert got == 2
    assert "no card in" in capsys.readouterr().err


def test_card_owns_return_refusal_names_the_card(wc, tmp_path, monkeypatch, capsys):
    tree, brief = _card_tree(tmp_path, monkeypatch, wc)
    payload = _payload(tmp_path / "S-01.json", ["src/a.py", "src/b.py"])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--return", f"S-01:{payload}"])
    err = capsys.readouterr().err
    assert got == 1
    assert ("S-01: updated path 'src/b.py' is in no owns entry. Add `src/b.py` "
            "to the `Owns:` list of card S-01 in") in err and brief.name in err


def test_brief_without_real_cards_falls_back_to_status(wc, tmp_path, monkeypatch,
                                                       capsys):
    # The template's `### S-NN <slug>` placeholder card is not a card.
    tree, brief = _card_tree(tmp_path, monkeypatch, wc)
    brief.write_text(_card("S-NN", "<stream ids or none>", ["<path>"]),
                     encoding="utf-8")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert "from STATUS.md, `## Wave 1` only" in out.out
    assert got == 1 and "outside owns: src/a.py" in out.out


def test_parse_cards_reads_inline_and_nested_owns(wc):
    text = "\n".join([
        "### S-03a slug", "- Owns: `a.py`, `b/{c,d}.md`", "  - e.py (new)",
        "    - `f/` and `g/**`", "  - none", "- After: S-01, S-02", "",
        "### S-04 slug", "- Owns: <path>", "- After: -",
    ])
    cards = wc.parse_cards(text)
    assert [c.sid for c in cards] == ["S-03a", "S-04"]
    assert wc.card_owns(cards[0]) == ["a.py", "b/{c,d}.md", "e.py", "f/", "g/**"]
    assert wc.card_after(cards[0]) == ["S-01", "S-02"]
    assert wc.card_owns(cards[1]) == [] and wc.card_after(cards[1]) == []


def test_card_waves_is_the_after_depth(wc):
    cards = wc.parse_cards("\n".join([
        "### S-01 a", "- After: none", "### S-02 b", "- After: S-01",
        "### S-03 c", "- After: S-02, S-01", "### S-04 d", "- After: S-09",
        "### S-05 e", "- After: S-06", "### S-06 f", "- After: S-05",
    ]))
    assert wc.card_waves(cards) == {"S-01": 1, "S-02": 2, "S-03": 3}


# --- removed-test listing ------------------------------------------

def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


def test_removed_tests_lists_def_test_names_gone_since_the_merge_base(
        wc, sb, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    _git(tree, "symbolic-ref", "HEAD", "refs/heads/main")
    (tree / "tests").mkdir()
    (tree / "tests" / "test_a.py").write_text(
        "def test_one():\n    pass\n\n\nasync def test_two():\n    pass\n\n\n"
        "class TestX:\n    def test_three(self):\n        pass\n",
        encoding="utf-8")
    (tree / "tests" / "test_gone.py").write_text("def test_g():\n    pass\n",
                                                 encoding="utf-8")
    (tree / "tests" / "helper.py").write_text("def test_not_a_test_file():\n"
                                              "    pass\n", encoding="utf-8")
    _git(tree, "add", "-A")
    _git(tree, "commit", "-q", "-m", "base")
    _git(tree, "checkout", "-q", "-b", "project/0070-x")
    (tree / "tests" / "test_a.py").write_text("def test_one():\n    pass\n",
                                              encoding="utf-8")
    (tree / "tests" / "test_gone.py").unlink()
    (tree / "tests" / "helper.py").write_text("", encoding="utf-8")
    base, ref = wc.merge_base(tree)
    assert ref == "main" and base
    assert wc.removed_tests(tree, base) == [
        ("tests/test_a.py", ["test_three", "test_two"]),
        ("tests/test_gone.py", ["test_g"])]
    # Printed by the gate, never refused: the floor holds, so this passes.
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2396))
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "tests/**", "--owns", "my-projects/**"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "tests/test_a.py: test_three, test_two" in out.out


def test_removed_tests_without_a_merge_base_says_so(wc, tmp_path, capsys):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    wc.print_removed_tests(repo)
    assert "no merge base" in capsys.readouterr().out


# --- suite compare through the gate --------------------------

def test_skipped_test_refuses(wc, sb, tmp_path, monkeypatch, capsys):
    # Skipped must be 0 even with passed above the floor.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)

    def run_all(_root, _suites):
        return {"tests/": sb.SuiteResult(suite="tests/", passed=2400, skipped=1,
                                         skip_reasons=["[1] master.mdb absent"]),
                "engine/tests/": sb.SuiteResult(suite="engine/tests/", passed=779)}
    monkeypatch.setattr(wc, "run_all", run_all)
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "my-projects/**"])
    out = capsys.readouterr()
    assert got == 1
    assert "tests/: 1 skipped; skipped must be 0" in out.err
    assert "master.mdb absent" in out.out


def test_added_test_passes(wc, sb, tmp_path, monkeypatch, capsys):
    # Additions never refuse: no suite-expected.json row is needed for them.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2396 + 30))
    got = wc.main(["wave-close.py", "0070", "--root", str(tree),
                   "--owns", "my-projects/**"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "floor 2396 passed; got 2426" in out.out


# --- --suites-only (was bootstrap-worktree.py --verify) ----------------------

def _suites_only(wc, tree, *extra):
    return wc.main(["wave-close.py", "0070", "--root", str(tree),
                    "--suites-only", *extra])


@pytest.mark.parametrize("tests_passed, rc", [(2390, 1), (2391, 0), (2392, 0),
                                              (2396, 0)])
def test_suites_only_lowers_the_floor_by_declared_retirements(
        wc, sb, tmp_path, monkeypatch, capsys, tests_passed, rc):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    _expected(status.parent / "suite-expected.json")
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, tests_passed))
    got = _suites_only(wc, tree)
    out = capsys.readouterr()
    assert got == rc, out.err
    assert "[1/4]" not in out.out and "[2/4]" not in out.out
    assert "lowered by the retirements" in out.out and "tests/ -5" in out.out
    if rc:
        assert "PASSED-COUNT REGRESSION" in out.err


def test_suites_only_refuses_a_skip_above_the_floor(wc, sb, tmp_path,
                                                    monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2400, skipped=1))
    assert _suites_only(wc, tree) == 1
    assert "skipped must be 0" in capsys.readouterr().err


def test_suites_only_refuses_a_malformed_expected_file(wc, sb, tmp_path,
                                                       monkeypatch, capsys):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    (status.parent / "suite-expected.json").write_text("[1, 2]", encoding="utf-8")
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2396))
    assert _suites_only(wc, tree) == 1
    assert "Fix or delete" in capsys.readouterr().err


def test_suites_only_without_an_expected_file_uses_the_pin(wc, sb, tmp_path,
                                                           monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2391))
    got = _suites_only(wc, tree)
    out = capsys.readouterr()
    assert got == 1
    assert "shared pin" in out.out and "PASSED-COUNT REGRESSION" in out.err


def test_suites_only_baseline_flag_overrides_the_expected_file(
        wc, sb, tmp_path, monkeypatch, capsys):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    _expected(status.parent / "suite-expected.json")
    floor = _pin(tmp_path / "floor.json", tests_passed=2391)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2392))
    got = _suites_only(wc, tree, "--baseline", str(floor))
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "--baseline" in out.out and "(floor)" in out.out


def test_suites_only_takes_no_owns_or_returns(wc, sb, tmp_path, monkeypatch):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    with pytest.raises(SystemExit) as exc:
        _suites_only(wc, tree, "--owns", "x")
    assert exc.value.code == 2


def test_suites_only_with_no_suites_configured_passes_and_says_so(
        wc, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc, suites=[])

    def boom(*_a):
        raise AssertionError("nothing to run")
    monkeypatch.setattr(wc, "run_all", boom)
    assert _suites_only(wc, tree) == 0
    assert "no suites configured" in capsys.readouterr().out


def test_suites_only_with_a_null_pin_asserts_only_skips_and_failures(
        wc, sb, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc, suite_pin=None)
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 1))
    assert _suites_only(wc, tree) == 0
    assert "`suite_pin` is null" in capsys.readouterr().out
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 1, skipped=1))
    assert _suites_only(wc, tree) == 1


def test_suites_only_names_a_missing_pin(wc, sb, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc,
                          suite_pin="pins/absent.json")
    monkeypatch.setattr(wc, "run_all", _fake_run_all(sb, 2396))
    assert _suites_only(wc, tree) == 1
    assert "--capture-pin" in capsys.readouterr().err


# --- --capture-pin (was bootstrap-worktree.py --capture-baseline) ------------

def test_capture_pin_writes_every_suite_to_the_configured_pin(
        wc, sb, tmp_path, monkeypatch, capsys):
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    main = tmp_path / "main"
    ran_in: list[Path] = []

    def run_suite(suite, cwd):
        ran_in.append(Path(cwd))
        return sb.SuiteResult(suite=suite.name, passed=7)
    monkeypatch.setattr(sb, "run_suite", run_suite)
    assert wc.main(["wave-close.py", "--capture-pin"]) == 0, capsys.readouterr().err
    doc = json.loads((main / PIN_REL).read_text(encoding="utf-8"))
    assert {n: r["passed"] for n, r in doc["suites"].items()} == \
        {"tests/": 7, "engine/tests/": 7}
    assert set(ran_in) == {main}
    # The written pin is the floor the gate then reads.
    assert sb.load_baseline(main / PIN_REL)["tests/"].passed == 7


def test_capture_pin_refuses_without_a_pin_path(wc, tmp_path, monkeypatch,
                                                capsys):
    _gate_tree(tmp_path, monkeypatch, wc, suite_pin=None)
    assert wc.main(["wave-close.py", "--capture-pin"]) == 2
    assert "needs `suite_pin`" in capsys.readouterr().err


def test_capture_pin_takes_no_project(wc, tmp_path, monkeypatch):
    _gate_tree(tmp_path, monkeypatch, wc)
    with pytest.raises(SystemExit) as exc:
        wc.main(["wave-close.py", "0070", "--capture-pin"])
    assert exc.value.code == 2


# --- placeholder roots, ledgers in returns, dir paths -----

_PROJ = "my-projects/current/project-0070-token-audit"


@pytest.mark.parametrize("entry, paths, foreign", [
    # `<proj>/` is the project folder, the form brief-template.md prescribes
    # for a stream's deliverable.
    ("<proj>/artifacts/S-01-x.md", [f"{_PROJ}/artifacts/S-01-x.md"], []),
    ("<proj>/artifacts/", [f"{_PROJ}/artifacts/**"], []),
    # A leading `{{name}}/` is the same root as `<name>/`, not `name/`.
    ("{{proj}}/artifacts/S-01-x.md", [f"{_PROJ}/artifacts/S-01-x.md"], []),
    ("{{wt}}/shop/x.py", ["shop/x.py"], []),
    ("{{wt}}/src/{a,b}.py", ["src/a.py", "src/b.py"], []),
    ("{{cfg-wt}}/skills/x.md", [], ["<cfg-wt>/skills/x.md"]),
    ("{{other}}/x.md", [], ["<other>/x.md"]),
    ("<other>/x.md", [], ["<other>/x.md"]),
])
def test_parse_owns_maps_root_placeholders(wc, tmp_path, entry, paths,
                                           foreign):
    tree = tmp_path / "wt"
    status = _status(tree, f"owns: {entry}")
    got = wc.parse_owns(status, tree)
    assert (got.paths, got.foreign, got.unparseable) == (paths, foreign, [])


def _proj_card_tree(tmp_path, monkeypatch, wc, owns: list[str]):
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    status.write_text('---\nproject: "0070"\ncurrent_wave: 1\n---\n',
                      encoding="utf-8")
    brief = status.parent / "brief.md"
    brief.write_text(
        "\n".join(["# Brief", "", "## Waves", "", "### Wave 1 -- first", "",
                   _card("S-01", "none", owns)]), encoding="utf-8")
    # The brief is committed before a wave runs; only the deliverable and
    # src/a.py are the wave's writes.
    _git(tree, "add", "--", brief.relative_to(tree).as_posix())
    _git(tree, "commit", "-q", "-m", "brief")
    art = status.parent / "artifacts"
    art.mkdir()
    (art / "S-01-x.md").write_text("deliverable", encoding="utf-8")
    (tree / "src").mkdir()
    (tree / "src" / "a.py").write_text("a", encoding="utf-8")
    return tree, status


@pytest.mark.parametrize("deliverable", ["<proj>/artifacts/S-01-x.md",
                                         "`{{proj}}/artifacts/S-01-x.md` (new)"])
def test_card_owns_proj_root_covers_the_deliverable(wc, tmp_path, monkeypatch,
                                                    capsys, deliverable):
    # A card owning its deliverable as `<proj>/artifacts/S-NN-<slug>.md`
    # refused it in both gates: the entry was read as another repo's.
    tree, status = _proj_card_tree(tmp_path, monkeypatch, wc,
                                   ["`src/a.py`", deliverable])
    rel = f"{_PROJ}/artifacts/S-01-x.md"
    payload = _payload(tmp_path / "S-01.json",
                       ["src/a.py", rel, str(status.parent / "artifacts" / "S-01-x.md")])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--return", f"S-01:{payload}"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "wave 1 cards in brief.md (S-01)" in out.out
    assert "return ok: S-01" in out.out
    assert "ok: no dirty path outside the owns union" in out.out
    assert "NOT CHECKED" not in out.out


def test_owns_arg_root_placeholders(wc, tmp_path, monkeypatch, capsys):
    # The same roots given as --owns: `<proj>/` and `{{wt}}/` gate in this tree.
    tree, status = _proj_card_tree(tmp_path, monkeypatch, wc, ["src/other.py"])
    payload = _payload(tmp_path / "S-01.json",
                       ["src/a.py", f"{_PROJ}/artifacts/S-01-x.md"])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "<proj>/artifacts/S-01-x.md",
                   "--owns", "{{wt}}/src/a.py", "--return", f"S-01:{payload}"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "return ok: S-01" in out.out
    assert "NOT CHECKED" not in out.out


def test_double_brace_cfg_wt_and_unknown_roots(wc, tmp_path, monkeypatch,
                                               capsys):
    # `{{cfg-wt}}/` expands through cfg_worktree as `<cfg-wt>/`
    # does; any other `{{name}}/` is reported NOT CHECKED, never read as a
    # tree-relative `name/` path.
    tree, _s = _gate_tree(tmp_path, monkeypatch, wc)
    cfg = _second_repo(tmp_path)
    (cfg / "a.md").write_text("a", encoding="utf-8")
    (cfg / "c.md").write_text("c", encoding="utf-8")
    (tree / "src").mkdir()
    (tree / "src" / "a.py").write_text("a", encoding="utf-8")
    _cfg_status(tree, cfg, "{{wt}}/src/a.py, {{cfg-wt}}/a.md, <cfg-wt>/c.md, "
                           "{{other}}/x.md")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert got == 0, out.err
    assert "other repos checked:" in out.out
    assert "no dirty path outside the owns union in those repos" in out.out
    assert "NOT CHECKED: <other>/x.md" in out.out
    # And the `{{cfg-wt}}/` entry really gates: a third file there refuses.
    (cfg / "b.md").write_text("b", encoding="utf-8")
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites"])
    out = capsys.readouterr()
    assert got == 1
    assert "outside owns in" in out.out and "b.md" in out.out


def test_return_listing_a_ledger_still_refuses(wc, tmp_path, monkeypatch,
                                               capsys):
    # Ledgers are exempt in gate 2 only. A return naming STATUS.md under
    # `--owns src/a.py` must refuse, though gate 2 exempts the same dirty file.
    tree, status = _gate_tree(tmp_path, monkeypatch, wc)
    (tree / "src").mkdir()
    (tree / "src" / "a.py").write_text("a", encoding="utf-8")
    ledger = status.relative_to(tree).as_posix()
    payload = _payload(tmp_path / "S-01.json", ["src/a.py", ledger])
    got = wc.main(["wave-close.py", "0070", "--root", str(tree), "--skip-suites",
                   "--owns", "src/a.py", "--return", f"S-01:{payload}"])
    out = capsys.readouterr()
    assert got == 1
    assert f"S-01: updated path '{ledger}' is in no owns entry" in out.err
    assert ledger in out.out.split("exempt (orchestrator ledgers): ", 1)[1]
    assert "outside owns:" not in out.out


def test_return_trailing_slash_dir_is_a_prefix(wc, tmp_path):
    # `src/` was normalised to `src`, which `src/**` does not cover.
    tree = tmp_path / "wt"
    other = (tmp_path / "other").as_posix()
    owns = ["src/**", "docs", f"{other}/**"]
    updated = ["src/", "src\\", f"{tree.as_posix()}/src/", "docs/", f"{other}/"]
    assert wc.return_outside_owns(updated, owns, tree) == []
    # A directory is not covered by one file in it, nor by a one-level glob.
    assert wc.return_outside_owns(["src/"], ["src/a.py", "src/*.py"], tree) == [
        ("src/", "outside")]
    # The tree itself is still no owns target.
    assert wc.return_outside_owns([f"{tree.as_posix()}/"], ["src/**"], tree) == [
        (f"{tree.as_posix()}/", "outside")]
