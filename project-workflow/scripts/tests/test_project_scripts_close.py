"""Close and scaffold defects, one behaviour per test, so a regression names
the behaviour rather than a line number. The skill's `scripts/` is not an importable
package (and `close-project.py` / `new-project.py` carry hyphens), so every
module is loaded by path the same way `test_close_pass_backlog_ids.py` does.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
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
    # Registered before exec: `@dataclass` resolves string annotations via
    # `sys.modules[cls.__module__]`, which is None for an unregistered module.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def close_steps():
    return _load(_LIB / "close_steps.py", "_close_steps_under_test")


@pytest.fixture(scope="module")
def close_project():
    return _load(_BIN / "close-project.py", "_close_project_under_test")


@pytest.fixture(scope="module")
def new_project():
    return _load(_BIN / "new-project.py", "_new_project_under_test")


README_INDEX = """# Projects

## Index

| Id | Project | Where | Status |
|---|---|---|---|
| 0026 | earlier-thing | completed | closed 2026-09-01 |
| 0027 | skill-analysis | current | brief written 2026-09-10; wave 2 |
"""

def _ctx(close_steps, worktree: Path, project_id: str = "0027",
         slug: str = "skill-analysis", cfg=None, cv_root: Path | None = None):
    kw = {} if cfg is None else {"cfg": cfg}
    return close_steps.CloseCtx(
        project_id=project_id,
        project_worktree=worktree,
        cv_root=cv_root if cv_root is not None else worktree,
        project_branch=f"project/{project_id}-{slug}",
        slug=slug,
        old_rel=f"my-projects/current/project-{project_id}-{slug}",
        new_rel=f"my-projects/completed/project-{project_id}-{slug}",
        **kw,
    )


def _cfg(**kw):
    """A Config from the same `config` module the scripts imported."""
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    import config
    return config.Config(**kw)


TEARDOWN_OK = ("python", "-c",
               "import os, sys; sys.exit(0 if os.path.isdir(sys.argv[1]) else 3)",
               "{worktree}")


def _seed_project(worktree: Path, project_id: str = "0027",
                  slug: str = "skill-analysis") -> None:
    folder = worktree / "my-projects" / "current" / f"project-{project_id}-{slug}"
    folder.mkdir(parents=True)
    (folder / "STATUS.md").write_text(
        f'---\nproject: "{project_id}"\nworkflow: orchestrated-session\n---\n',
        encoding="utf-8")


# --- index row flips the README.md Index table ---------------------------

def test_index_row_flips_the_readme_row(close_steps, tmp_path, monkeypatch):
    monkeypatch.setattr(close_steps, "_run", lambda *a, **k: None)
    _seed_project(tmp_path)
    mp = tmp_path / "my-projects"
    # Another file carrying an Index table is never the target: README.md is
    # the one project index.
    other = mp / "index.md"
    other.write_text(README_INDEX, encoding="utf-8")
    readme = mp / "README.md"
    readme.write_text(README_INDEX, encoding="utf-8")

    ctx = _ctx(close_steps, tmp_path)
    assert close_steps._precheck_index_row(ctx) is None
    assert ctx._resolved["index_readme"] == readme
    assert ctx._resolved["index_row_present"] is True

    close_steps._apply_index_row(ctx)
    row = next(ln for ln in readme.read_text(encoding="utf-8").splitlines()
               if ln.startswith("| 0027 "))
    assert "| completed |" in row
    assert row.count("|") == 5  # still a 4-column row
    assert other.read_text(encoding="utf-8") == README_INDEX


@pytest.mark.parametrize("before,after", [
    ("active", "orchestrated-session — closed {d} |"),
    ("planned", "orchestrated-session — closed {d} |"),
    ("brief written 2026-09-10; wave 2", "orchestrated-session — closed {d}; wave 2 |"),
])
def test_index_row_keeps_only_the_tail_after_a_semicolon(
        close_steps, tmp_path, monkeypatch, before, after):
    # A bare state word (`active`, `planned`) is stale at close; only a
    # description after the first ';' survives.
    monkeypatch.setattr(close_steps, "_run", lambda *a, **k: None)
    _seed_project(tmp_path)
    readme = tmp_path / "my-projects" / "README.md"
    readme.write_text(README_INDEX.replace(
        "brief written 2026-09-10; wave 2", before), encoding="utf-8")
    ctx = _ctx(close_steps, tmp_path)
    assert close_steps._precheck_index_row(ctx) is None
    close_steps._apply_index_row(ctx)
    row = next(ln for ln in readme.read_text(encoding="utf-8").splitlines()
               if ln.startswith("| 0027 "))
    from datetime import date
    assert row.endswith("| completed | " + after.format(d=date.today().isoformat()))


def test_index_row_no_op_without_any_table(close_steps, tmp_path):
    _seed_project(tmp_path)
    (tmp_path / "my-projects" / "README.md").write_text("# Projects\n",
                                                        encoding="utf-8")
    ctx = _ctx(close_steps, tmp_path)
    assert close_steps._precheck_index_row(ctx) is None
    assert ctx._resolved["index_readme"] is None


# --- worktree-remove must not rmtree a TRACKED .vscode/ --------------------

def _fake_run(recorder, ls_files_stdout: str, worktree: Path | None = None,
              snapshot: dict | None = None):
    """A `_run` stub. When `worktree` is given, `git worktree remove` really
    deletes it (after `snapshot` records what was there), as git would."""
    def run(cmd, cwd, check=True):
        recorder.append(cmd)
        out = ls_files_stdout if "ls-files" in cmd else ""
        if worktree is not None and "worktree" in cmd and "remove" in cmd:
            if snapshot is not None:
                snapshot["files"] = sorted(
                    q.relative_to(worktree).as_posix()
                    for q in worktree.rglob("*") if q.is_file())
            shutil.rmtree(worktree)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


def test_worktree_remove_keeps_tracked_vscode(close_steps, tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    vscode = wt / ".vscode"
    vscode.mkdir(parents=True)
    (vscode / "settings.json").write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []
    seen: dict = {}
    monkeypatch.setattr(close_steps, "_run",
                        _fake_run(calls, ".vscode/settings.json\n", wt, seen))

    close_steps._apply_worktree_remove(_ctx(close_steps, wt))

    # Still there when `git worktree remove` ran: the step did not rmtree it.
    assert seen["files"] == [".vscode/settings.json"]
    assert any("worktree" in c and "remove" in c for c in calls)


def test_worktree_remove_still_clears_untracked_vscode(close_steps, tmp_path,
                                                       monkeypatch):
    wt = tmp_path / "wt"
    vscode = wt / ".vscode"
    vscode.mkdir(parents=True)
    (vscode / "tasks.json").write_text("{}", encoding="utf-8")
    calls: list[list[str]] = []
    seen: dict = {}
    monkeypatch.setattr(close_steps, "_run", _fake_run(calls, "", wt, seen))

    close_steps._apply_worktree_remove(_ctx(close_steps, wt))

    assert seen["files"] == []


# --- worktree-remove asserts the path is gone ------------------------------

def test_worktree_remove_refuses_when_the_path_survives(close_steps, tmp_path,
                                                        monkeypatch):
    # git exits 0 but the folder is still there (a locked handle on Windows).
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(close_steps, "_run", _fake_run([], ""))
    with pytest.raises(RuntimeError, match="still exists on disk"):
        close_steps._apply_worktree_remove(_ctx(close_steps, wt))


def test_worktree_remove_passes_when_the_path_is_gone(close_steps, tmp_path,
                                                      monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    monkeypatch.setattr(close_steps, "_run", _fake_run([], "", wt))
    close_steps._apply_worktree_remove(_ctx(close_steps, wt))
    assert not wt.exists()


# --- git-push refuses while main is ahead of origin/main ----------------

def _push_run(ahead_stdout: str, ahead_rc: int = 0):
    def run(cmd, cwd, check=True):
        if "rev-list" in cmd:
            return subprocess.CompletedProcess(cmd, ahead_rc, stdout=ahead_stdout,
                                               stderr="bad revision 'origin/main'")
        return subprocess.CompletedProcess(cmd, 0, stdout="abc\n", stderr="")
    return run


def test_push_refuses_when_main_is_ahead_of_origin(close_steps, tmp_path,
                                                   monkeypatch):
    monkeypatch.setattr(close_steps, "_run", _push_run("2\n"))
    reason = close_steps._precheck_push(_ctx(close_steps, tmp_path))
    assert reason is not None
    assert "2 commit(s) ahead of origin/main" in reason
    assert "git push origin main" in reason


def test_push_allowed_when_main_is_level_with_origin(close_steps, tmp_path,
                                                     monkeypatch):
    monkeypatch.setattr(close_steps, "_run", _push_run("0\n"))
    assert close_steps._precheck_push(_ctx(close_steps, tmp_path)) is None


def test_push_refuses_when_ahead_count_is_unknown(close_steps, tmp_path,
                                                  monkeypatch):
    monkeypatch.setattr(close_steps, "_run", _push_run("", ahead_rc=128))
    reason = close_steps._precheck_push(_ctx(close_steps, tmp_path))
    assert reason is not None and "git fetch origin" in reason


def test_main_ahead_of_origin_on_a_real_repo(close_steps, tmp_path):
    def git(*args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    remote, local = tmp_path / "remote.git", tmp_path / "local"
    git("init", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    git("clone", str(remote), str(local), cwd=tmp_path)
    for args in (("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"),
                 ("checkout", "-b", "main"),
                 ("commit", "--allow-empty", "-m", "one"),
                 ("push", "-u", "origin", "main")):
        git(*args, cwd=local)
    assert close_steps.main_ahead_of_origin(local) == (0, "")
    git("commit", "--allow-empty", "-m", "two", cwd=local)
    assert close_steps.main_ahead_of_origin(local) == (1, "")


# --- stale close-stream.py hints replaced ---------------------------------

def test_close_project_no_longer_names_close_stream(close_project):
    source = (_BIN / "close-project.py").read_text(encoding="utf-8")
    assert "run close-stream.py first" not in source


# --- new-project.py index row matches the table's own column count ---------

def test_readme_index_row_matches_four_column_table(new_project, tmp_path,
                                                    monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(README_INDEX, encoding="utf-8")
    monkeypatch.setattr(new_project, "README_PATH", readme)

    assert new_project.append_readme_index_row("0031", "new-thing") is True
    row = next(ln for ln in readme.read_text(encoding="utf-8").splitlines()
               if ln.startswith("| 0031 "))
    assert row == "| 0031 | new-thing | current | planned |"


def test_readme_index_row_matches_six_column_table(new_project, tmp_path,
                                                   monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(
        "## Index\n\n"
        "| Id | Project | Where | Status | Artifacts | Outstanding |\n"
        "|---|---|---|---|---|---|\n"
        "| 0026 | earlier-thing | completed | closed | 4 | - |\n",
        encoding="utf-8")
    monkeypatch.setattr(new_project, "README_PATH", readme)

    assert new_project.append_readme_index_row("0031", "new-thing") is True
    row = next(ln for ln in readme.read_text(encoding="utf-8").splitlines()
               if ln.startswith("| 0031 "))
    assert row == "| 0031 | new-thing | current | planned | 0 | - |"


def test_readme_index_row_is_idempotent(new_project, tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(README_INDEX, encoding="utf-8")
    monkeypatch.setattr(new_project, "README_PATH", readme)

    new_project.append_readme_index_row("0031", "new-thing")
    before = readme.read_text(encoding="utf-8")
    assert new_project.append_readme_index_row("0031", "new-thing") is False
    assert readme.read_text(encoding="utf-8") == before


# --- --parent reaches STATUS.md frontmatter --------------------------------

def test_parent_lands_in_scaffold_frontmatter(new_project):
    text = new_project.render_status_scaffold("0031", "new-thing", parent="0005")
    head = text.split("\n---\n", 1)[0]
    assert 'parent_project: "0005"' in head


def test_parent_lands_in_stub_frontmatter(new_project, tmp_path):
    text = new_project.render_status_stub("0031", "new-thing", tmp_path,
                                          "project/0031-new-thing",
                                          closes=["R17"], parent="0005")
    head = text.split("\n---\n", 1)[0]
    assert "closes: [R17]" in head
    assert 'parent_project: "0005"' in head


def test_no_parent_adds_no_key(new_project):
    assert "parent_project" not in new_project.render_status_scaffold("0031", "x")


# --- session mode: closing a project that never had a worktree ------------
#
# A session-mode project (`worktree: none`) commits straight to
# `main`, so `find_project_worktree` returns None and close refused outright.
# Cutting a worktree at close time is the wrong repair: it is clean and level
# with `main` by construction, so the four gates reading it cannot fail while
# still printing PASS, and step 8 then deletes the evidence. These tests pin
# the other path -- read `main`, and print the substrate-less gates as
# INAPPLICABLE rather than as a pass they did not earn.


@pytest.fixture(scope="module")
def streams_mod(close_project):
    # Not a second `_load`: this is the same module object close-project.py
    # imported, so patching or asserting here matches what the verb sees.
    import streams
    return streams


@pytest.fixture(scope="module")
def live_close_steps(close_project):
    import close_steps
    return close_steps


def _seed_status(root: Path, project_id: str, slug: str, worktree: str,
                 state: str = "current") -> Path:
    folder = root / "my-projects" / state / f"project-{project_id}-{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "STATUS.md").write_text(
        f'---\nproject: "{project_id}"\nworktree: {worktree}\n---\n\n# {slug}\n',
        encoding="utf-8")
    return folder


def test_discover_projects_globs_every_state(streams_mod, tmp_path):
    _seed_status(tmp_path, "0027", "skill-analysis", "../demo-0027", "current")
    _seed_status(tmp_path, "0026", "earlier-thing", "none", "completed")
    _seed_status(tmp_path, "0025", "parked", "none", "deferred")

    found = streams_mod.discover_projects(tmp_path)
    assert set(found) == {"0025", "0026", "0027"}
    assert found["0026"].name == "project-0026-earlier-thing"
    # A completed project stays addressable: its path must survive close's
    # `git mv`.
    assert found["0026"].parent.name == "completed"


def test_discover_projects_prefers_current_over_a_half_finished_move(
        streams_mod, tmp_path):
    _seed_status(tmp_path, "0027", "skill-analysis", "none", "current")
    _seed_status(tmp_path, "0027", "skill-analysis", "none", "completed")
    found = streams_mod.discover_projects(tmp_path)
    assert found["0027"].parent.name == "current"


def test_load_projects_resolves_from_the_project_folders(streams_mod, tmp_path):
    # The map is the folders alone, `current/` and `completed/` both.
    _seed_status(tmp_path, "0057", "session-cost-reduction", "none")
    folder = _seed_status(tmp_path, "0026", "earlier-thing", "none", "completed")
    got = streams_mod.load_projects(tmp_path)
    assert set(got) == {"0057", "0026"}
    assert got["0026"] == folder.resolve()


def test_load_projects_refuses_with_no_project_folders(
        streams_mod, tmp_path):
    # The fallback must not turn a genuinely empty tree into a silent success.
    with pytest.raises(SystemExit):
        streams_mod.load_projects(tmp_path)


def test_load_projects_refusal_names_the_scaffold_command(streams_mod, tmp_path):
    # The refusal ends with the fix, and cites no config file that is absent.
    with pytest.raises(SystemExit) as excinfo:
        streams_mod.load_projects(tmp_path)
    message = str(excinfo.value)
    assert "new-project.py" in message
    assert "projects.example.yaml" not in message


def test_is_session_mode_reads_the_declaration(close_project, tmp_path):
    # Declared, not inferred: an absent worktree is also the shape of one
    # removed by hand mid-project, and those two want opposite treatment.
    _seed_status(tmp_path, "0057", "session-cost-reduction",
                 "none — runs as sessions on main")
    _seed_status(tmp_path, "0027", "skill-analysis", "../demo-0027")
    assert close_project.is_session_mode("0057", tmp_path) is True
    assert close_project.is_session_mode("0027", tmp_path) is False
    # Unknown project: refuse to guess rather than default into session mode.
    assert close_project.is_session_mode("9999", tmp_path) is False


def test_session_inapplicable_marks_exactly_the_substrate_less_steps(
        close_project):
    marked = {s.name for s in close_project.CLOSE_STEPS
              if s.session_inapplicable}
    assert marked == {
        "local-merge",
        "git-push", "gh-pr-create", "gh-pr-merge", "git-pull-main",
        "reconcile-main", "worktree-remove", "archive-tag", "branch-delete",
    }
    # Every reason must actually say something; the string IS the transcript.
    for step in close_project.CLOSE_STEPS:
        if step.session_inapplicable:
            assert len(step.session_inapplicable) > 40


@pytest.mark.parametrize("name", ["branch-delete", "worktree-remove"])
def test_destructive_steps_are_never_reached_in_session_mode(
        close_project, name):
    # `project_branch` is `main` and `project_worktree` is the main checkout,
    # so these two would run `git branch -d main` and delete the repository the
    # close is executing in. Skipping them is a safety property, not a tidy-up.
    step = next(s for s in close_project.CLOSE_STEPS if s.name == name)
    assert step.session_inapplicable
    assert step.mutating is True


def test_dry_run_skips_inapplicable_steps_without_prechecking(
        close_project, live_close_steps, tmp_path, capsys):
    _seed_status(tmp_path, "0057", "session-cost-reduction", "none")
    ctx = live_close_steps.CloseCtx(
        project_id="0057",
        project_worktree=tmp_path,
        cv_root=tmp_path,
        project_branch="main",
        slug="session-cost-reduction",
        old_rel="my-projects/current/project-0057-session-cost-reduction",
        new_rel="my-projects/completed/project-0057-session-cost-reduction",
        session_mode=True,
    )
    close_project.run_dry_run(ctx)
    out = capsys.readouterr().out
    for name in ("git-push", "worktree-remove", "branch-delete", "archive-tag"):
        assert f"  {name} -> INAPPLICABLE:" in out
        # The word that means "satisfied" must not appear for these.
        assert f"  {name} -> ok" not in out


def test_maintenance_passes_come_from_the_skill_not_the_closing_tree(
        live_close_steps, tmp_path, capsys):
    # The scripts live with the skill, so the passes are the ones shipped
    # beside close_steps.py, whatever tree is being closed (an empty tmp dir
    # here, with no close_passes/ of its own).
    assert live_close_steps.PASSES_DIR == _LIB / "close_passes"
    assert not hasattr(live_close_steps, "_maintenance_passes_dir")
    ctx = _ctx(live_close_steps, tmp_path)
    assert live_close_steps._precheck_maintenance_passes(ctx) is None
    assert ctx._resolved["maintenance_passes_source"] == _LIB / "close_passes"
    assert [p.name for p in ctx._resolved["maintenance_passes"]] == \
        ["backlog-id-collisions"]


_VERDICT_RE = re.compile(r"^(PASS|FAIL|WARN|INFO|INAPPLICABLE): \[([^\]]+)\] ")


def _verdicts(out: str) -> list[tuple[str, str]]:
    """(gate_id, OUTCOME) for every verdict line `emit_verdict` printed."""
    return [(m.group(2), m.group(1))
            for m in map(_VERDICT_RE.match, out.splitlines()) if m]


@pytest.fixture(autouse=True)
def _fresh_command_cache(close_steps, live_close_steps):
    # The gh / ls-remote memo lives for a process; each test is its own
    # invocation, as main() makes each run of the verb.
    close_steps.clear_command_cache()
    live_close_steps.clear_command_cache()
    yield
    close_steps.clear_command_cache()
    live_close_steps.clear_command_cache()


def _no_gh(close_project, monkeypatch) -> None:
    """Keep every gh call off the network: `gh` is 'not installed'. Git keeps
    running for real -- `run()` wraps the same callable."""
    real_run = close_project.subprocess.run

    def no_gh(cmd, *a, **k):
        if cmd and cmd[0] == "gh":
            raise FileNotFoundError("gh")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(close_project.subprocess, "run", no_gh)


def test_session_mode_preflight_gives_each_gate_exactly_one_verdict(
        close_project, monkeypatch, capsys):
    # Gates used to null their input and fall through to the shared verdict
    # branch, so session mode printed INAPPLICABLE *and* then PASS for the
    # same gate id. The PASS could not have been anything else -- a vacuous
    # gate reintroduced by the fix for vacuous gates. A gate speaks once per
    # run, or it is not a gate.
    _no_gh(close_project, monkeypatch)
    close_project.run_preflight("0057", ROOT, session_mode=True,
                                allow_untracked=True, allow_red_ci=True)

    recorded = _verdicts(capsys.readouterr().out)
    duplicated = sorted({g for g, _ in recorded
                         if sum(1 for h, _ in recorded if h == g) > 1})
    assert not duplicated, f"gate(s) emitted more than one verdict: {duplicated}"
    by_gate = dict(recorded)
    for gate in ("P5", "P15"):
        assert by_gate.get(gate) == "INAPPLICABLE", (
            f"{gate} has no subject in session mode; got {by_gate.get(gate)!r}")


def test_preflight_runs_exactly_the_kept_checks(close_project, live_close_steps,
                                                monkeypatch, capsys, tmp_path):
    # The close keeps only: clean tree (P1/P2), cwd outside the
    # worktree (P5), main CI green (P11), main not ahead of origin (P15) and
    # the two frontmatter keys. Every other gate id is gone, not silenced.
    _seed_skill_status(tmp_path)
    monkeypatch.setattr(live_close_steps, "main_ahead_of_origin",
                        lambda *_a: (0, ""))
    _no_gh(close_project, monkeypatch)
    close_project.run_preflight("0077", tmp_path,
                                cfg=_cfg(merge="squash", ci_workflow="ci.yml"))
    gates = [g for g, _ in _verdicts(capsys.readouterr().out)]
    assert gates == ["P1", "P2", "P5", "P11", "P15",
                     "close:external_yield", "close:workflow_key"]


# --- P15: the main-ahead-of-origin check also runs at preflight, before any mutation

def _preflight_gates(close_project, live_close_steps, monkeypatch, capsys,
                     ahead: tuple) -> tuple[bool, dict]:
    monkeypatch.setattr(live_close_steps, "main_ahead_of_origin",
                        lambda *_a: ahead)
    _no_gh(close_project, monkeypatch)
    ok = close_project.run_preflight("0070", ROOT, allow_untracked=True,
                                     allow_red_ci=True,
                                     cfg=_cfg(merge="squash", ci_workflow="ci.yml"))
    return ok, dict(_verdicts(capsys.readouterr().out))


def test_p15_fails_preflight_when_main_is_ahead(close_project, live_close_steps,
                                                monkeypatch, capsys):
    ok, gates = _preflight_gates(close_project, live_close_steps, monkeypatch,
                                 capsys, (3, ""))
    assert gates["P15"] == "FAIL"
    assert ok is False


def test_p15_fails_preflight_when_ahead_count_is_unknown(
        close_project, live_close_steps, monkeypatch, capsys):
    _ok, gates = _preflight_gates(close_project, live_close_steps, monkeypatch,
                                  capsys, (None, "bad revision"))
    assert gates["P15"] == "FAIL"


def test_p15_passes_when_main_is_level(close_project, live_close_steps,
                                       monkeypatch, capsys):
    _ok, gates = _preflight_gates(close_project, live_close_steps, monkeypatch,
                                  capsys, (0, ""))
    assert gates["P15"] == "PASS"


def test_p15_and_p11_are_inapplicable_under_local_merge_without_ci(
        close_project, live_close_steps, monkeypatch, capsys):
    # `close.merge: local` pushes nothing, and no `ci_workflow` means no CI
    # run to ask about: both gates print INAPPLICABLE, never PASS, and never
    # call the subject they do not have.
    def boom(*_a):
        raise AssertionError("P15 must not count commits under local merge")
    monkeypatch.setattr(live_close_steps, "main_ahead_of_origin", boom)
    _no_gh(close_project, monkeypatch)
    close_project.run_preflight("0070", ROOT, allow_untracked=True,
                                cfg=_cfg(merge="local", ci_workflow=None))
    gates = dict(_verdicts(capsys.readouterr().out))
    assert gates["P15"] == "INAPPLICABLE"
    assert gates["P11"] == "INAPPLICABLE"


# --- skill-run rule ---------------------------------------------------------
# A project run by the project-workflow skill records its streams in STATUS.md
# stream blocks and never writes `<project>/streams/`. The rule is concrete:
# STATUS.md frontmatter carries `current_wave` AND there is no streams/ dir.

def _seed_skill_status(root: Path, project_id: str = "0077",
                       slug: str = "skill-run", fm: str = "current_wave: 4\n"
                       ) -> Path:
    folder = root / "my-projects" / "current" / f"project-{project_id}-{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "STATUS.md").write_text(
        f'---\nproject: "{project_id}"\nworktree: ../x\n{fm}---\n\n# {slug}\n',
        encoding="utf-8")
    return folder


def test_is_skill_run_needs_current_wave_and_no_streams_dir(close_project,
                                                            tmp_path):
    folder = _seed_skill_status(tmp_path)
    assert close_project.is_skill_run("0077", tmp_path) is True
    (folder / "streams").mkdir()
    assert close_project.is_skill_run("0077", tmp_path) is False
    _seed_skill_status(tmp_path, "0078", "legacy", fm="current_phase: 2\n")
    assert close_project.is_skill_run("0078", tmp_path) is False
    assert close_project.is_skill_run("9999", tmp_path) is False


def _tmp_preflight(close_project, live_close_steps, monkeypatch, capsys,
                   root: Path, project_id: str, **kw) -> tuple[bool, dict]:
    monkeypatch.setattr(live_close_steps, "main_ahead_of_origin",
                        lambda *_a: (0, ""))
    _no_gh(close_project, monkeypatch)
    ok = close_project.run_preflight(project_id, root, **kw)
    by_gate: dict[str, list[str]] = {}
    for gate, outcome in _verdicts(capsys.readouterr().out):
        by_gate.setdefault(gate, []).append(outcome)
    return ok, by_gate


# --- defect-log gates retired ---------------------------------------------
# The retired defect log (`logs/defects.md`) takes no new rows: a wave fixes what it finds,
# and only an unresolvable item goes to backlog.md. P8 (blocked/failed stream
# coverage) and P9 (issues disposition coverage) are gone, not repointed.

DEFECTS_TABLE = ("| Project | Symptom | Cited | Disposition |\n|---|---|---|---|\n"
                "| 0077 S-01 | wedged | x.py:1 | |\n"
                "| 0077 S-02 | fixed | x.py:2 | landed abc1234 |\n")


def test_preflight_emits_no_issues_log_gates(
        close_project, live_close_steps, monkeypatch, capsys, tmp_path):
    # Every input the old gates read is present: a ledger with a blocked
    # stream, and an undispositioned defect-log row scoped to the project.
    folder = _seed_skill_status(tmp_path)
    (folder / "wave-state.jsonl").write_text(
        '{"stream": "S-01", "status": "blocked"}\n', encoding="utf-8")
    logs = tmp_path / "my-projects" / "logs"
    logs.mkdir(parents=True)
    (logs / "defects.md").write_text(DEFECTS_TABLE, encoding="utf-8")
    _ok, gates = _tmp_preflight(close_project, live_close_steps, monkeypatch,
                                capsys, tmp_path, "0077")
    assert gates
    assert "P8" not in gates and "P9" not in gates


@pytest.mark.parametrize("flag", [
    "--allow-undocumented-defects",  # downgraded P8/P9
    "--allow-no-streams",            # downgraded P3
    "--unobserved",                  # excused P12
    "--allow-stale-close-code",      # downgraded P14
    "--log-gates",                   # wrote the retired gate ledger
])
def test_retired_flags_are_rejected(close_project, flag):
    # Kept, each would be a no-op that still reads as an override, so
    # argparse rejects it.
    with pytest.raises(SystemExit) as excinfo:
        close_project.main(["close-project.py", "0077", "--preflight", flag])
    assert excinfo.value.code == 2


def test_close_step_no_longer_rolls_or_reads_issues_log(close_steps):
    step = next(s for s in close_steps.STEPS if s.name == "maintenance-passes")
    assert "issues" not in step.description
    for name in ("_precheck_issue_roll", "_apply_issue_roll",
                 "undispositioned_defect_rows", "_precheck_outstanding_column",
                 "_apply_outstanding_column", "append_pass_backlog_rows"):
        assert not hasattr(close_steps, name), name


def test_step_sequence_has_no_yaml_step(close_project):
    names = [s.name for s in close_project.CLOSE_STEPS]
    assert "update-projects-yaml" not in names
    assert names == [
        "backlog-cap-guard", "git-mv", "status-flip", "index-row",
        "maintenance-passes", "close-record", "git-commit", "local-merge",
        "git-push", "gh-pr-create", "gh-pr-merge", "git-pull-main",
        "reconcile-main", "prune-scaffold", "worktree-remove", "archive-tag",
        "branch-delete",
    ]


@pytest.mark.parametrize("mode,applies,skipped", [
    ("local", {"local-merge"},
     {"git-push", "gh-pr-create", "gh-pr-merge", "git-pull-main", "reconcile-main"}),
    ("squash", {"git-push", "gh-pr-create", "gh-pr-merge", "git-pull-main",
                "reconcile-main"}, {"local-merge"}),
])
def test_merge_mode_picks_the_merge_path(close_project, live_close_steps,
                                         tmp_path, mode, applies, skipped):
    ctx = _ctx(live_close_steps, tmp_path, cfg=_cfg(merge=mode))
    by_name = {s.name: s for s in close_project.CLOSE_STEPS}
    for name in applies:
        assert live_close_steps.step_inapplicable(by_name[name], ctx) is None, name
    for name in skipped:
        reason = live_close_steps.step_inapplicable(by_name[name], ctx)
        assert reason and f"close.merge is {mode!r}" in reason, name
    # Steps common to both paths always apply outside session mode.
    for name in ("git-mv", "git-commit", "worktree-remove", "branch-delete"):
        assert live_close_steps.step_inapplicable(by_name[name], ctx) is None


def test_backlog_cap_guard_reads_the_configured_backlog(close_steps, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "ideas.md").write_text("x\n" * 150, encoding="utf-8")
    ctx = _ctx(close_steps, tmp_path, cfg=_cfg(backlog_file="docs/ideas.md"))
    reason = close_steps._precheck_backlog_cap_guard(ctx)
    assert reason is not None and "docs/ideas.md" in reason


@pytest.mark.parametrize("lines,refused", [(149, False), (150, True)])
def test_backlog_cap_guard_uses_the_stated_cap(close_steps, tmp_path, lines,
                                               refused):
    assert close_steps.BACKLOG_LINE_CAP == 150
    mp = tmp_path / "my-projects"
    mp.mkdir()
    (mp / "backlog.md").write_text("x\n" * lines, encoding="utf-8")
    reason = close_steps._precheck_backlog_cap_guard(_ctx(close_steps, tmp_path))
    assert (reason is not None) is refused


def test_project_scripts_cite_no_retired_orchestrated_session_doc():
    # orchestrated-session.md no longer exists; four comments still cited it.
    for rel in ("lib/streams.py",
                "bin/close-project.py", "lib/close_steps.py"):
        text = (ROOT / "scripts" / rel).read_text(encoding="utf-8")
        assert "orchestrated-session.md" not in text, rel


# --- external-yield and GitHub-remote prechecks ------------------------------

def test_external_yield_fail_names_the_fix(close_project, tmp_path):
    # The gate stays, and its FAIL now says where the fix goes.
    status = tmp_path / "STATUS.md"
    for fm in ({}, {"external_yield": ""}, {"external_yield": "none"}):
        msg, outcome = close_project._external_yield_verdict(fm, status)
        assert outcome == "FAIL"
        assert "worktree STATUS.md" in msg and status.as_posix() in msg
        assert "templates/status-template.md" in msg
        assert "my-projects/WORKFLOW.md" in msg  # the default profile
    msg, _ = close_project._external_yield_verdict({}, status, "docs/flow.md")
    assert "docs/flow.md" in msg and "my-projects/WORKFLOW.md" not in msg
    msg, outcome = close_project._external_yield_verdict(
        {"external_yield": "none", "external_yield_waiver": "internal only"})
    assert outcome == "WARN" and "Fix:" not in msg
    assert close_project._external_yield_verdict(
        {"external_yield": "docs/guide.md"})[1] == "PASS"


def test_preflight_fails_a_status_with_no_workflow_key(
        close_project, live_close_steps, monkeypatch, capsys, tmp_path):
    # The missing key used to pass preflight and stop the real close
    # at index-row after git-mv/status-flip had applied.
    # external_yield is declared so the workflow gate is the only key FAIL.
    _seed_skill_status(tmp_path, fm="current_wave: 4\nexternal_yield: docs/x.md\n")
    ok, gates = _tmp_preflight(close_project, live_close_steps, monkeypatch,
                               capsys, tmp_path, "0077")
    assert gates["close:workflow_key"] == ["FAIL"]
    assert gates["close:external_yield"] == ["PASS"]
    assert ok is False


@pytest.mark.parametrize("line,expect", [
    ("workflow: project-workflow", "project-workflow"),
    ("workflow: project-workflow  # note", "project-workflow"),
    ('workflow: "project-workflow"', "project-workflow"),
    ("workflow:", None),
    ("workflow: # only a comment", None),
    ('workflow: ""', None),
    ('workflow: "  "', None),
    ("workflow: ' '", None),
])
def test_preflight_and_index_row_agree_on_workflow_key(
        close_project, live_close_steps, monkeypatch, capsys, tmp_path,
        line, expect):
    # Preflight once read the YAML value, index-row a raw-text
    # regex, so a trailing comment passed one and failed the other. Both now
    # call close_steps.workflow_key_value (trailing comment accepted).
    _seed_skill_status(
        tmp_path, fm=f"current_wave: 4\nexternal_yield: docs/x.md\n{line}\n")
    (tmp_path / "my-projects" / "README.md").write_text(
        README_INDEX.replace("0027", "0077"), encoding="utf-8")
    _ok, gates = _tmp_preflight(close_project, live_close_steps, monkeypatch,
                                capsys, tmp_path, "0077")
    ctx = _ctx(live_close_steps, tmp_path, "0077", "skill-run")
    refusal = live_close_steps._precheck_index_row(ctx)
    assert (gates["close:workflow_key"] == ["PASS"]) == (refusal is None)
    if expect is None:
        assert gates["close:workflow_key"] == ["FAIL"] and refusal
    else:
        assert gates["close:workflow_key"] == ["PASS"]
        assert ctx._resolved["index_workflow"] == expect


def test_preflight_passes_the_workflow_key_check_when_declared(
        close_project, live_close_steps, monkeypatch, capsys, tmp_path):
    _seed_skill_status(tmp_path, fm="current_wave: 4\nworkflow: project-workflow\n")
    _ok, gates = _tmp_preflight(close_project, live_close_steps, monkeypatch,
                                capsys, tmp_path, "0077")
    assert gates["close:workflow_key"] == ["PASS"]


def _gh_stub(close_steps, monkeypatch, repo_rc: int, repo_out: str = "",
             repo_err: str = ""):
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(cmd, repo_rc, stdout=repo_out,
                                               stderr=repo_err)
        # pr view / pr checks: no PR yet.
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no PR")
    monkeypatch.setattr(close_steps.subprocess, "run", fake_run)
    return calls


def test_pr_prechecks_refuse_without_a_github_remote(close_steps, tmp_path,
                                                     monkeypatch):
    # `gh auth status` alone passed against a non-GitHub origin.
    err = ("none of the git remotes configured for this repository point to "
           "a known GitHub host")
    _gh_stub(close_steps, monkeypatch, 1, repo_err=err)
    ctx = _ctx(close_steps, tmp_path)
    for precheck in (close_steps._precheck_pr_create,
                     close_steps._precheck_pr_merge):
        reason = precheck(ctx)
        assert reason is not None and "known GitHub host" in reason
    close_steps.clear_command_cache()  # a new invocation asks again
    _gh_stub(close_steps, monkeypatch, 0, repo_out="example-org/demo\n")
    assert close_steps._precheck_pr_create(ctx) is None
    assert close_steps._precheck_pr_merge(ctx) is None
    assert ctx._resolved["gh_repo"] == "example-org/demo"


def test_ci_gate_asks_for_the_ci_workflow_on_mains_head(
        close_project, monkeypatch, capsys, tmp_path):
    # P11 used to take the newest run of any workflow on main, so at one
    # close a skipped scheduled run of another workflow from two days earlier
    # was read as CI's verdict. It must ask for the configured CI workflow on
    # the configured base branch's head.
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "trunk"),
                 ("-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "seed")):
        subprocess.run(["git", "-C", str(repo), *args], check=True,
                       capture_output=True)
    monkeypatch.setattr(close_project, "CV_ROOT", repo)
    real_run = close_project.subprocess.run
    gh_calls: list[list[str]] = []

    def fake_gh(cmd, *a, **k):
        if cmd and cmd[0] == "gh":
            gh_calls.append(list(cmd))
            return subprocess.CompletedProcess(
                cmd, 0, stdout='[{"conclusion": "success", "status": "completed", '
                               '"url": "https://example.invalid/run/1"}]', stderr="")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(close_project.subprocess, "run", fake_gh)

    close_project.run_preflight(
        "0057", ROOT, session_mode=True, allow_untracked=True,
        cfg=_cfg(base_branch="trunk", merge="squash", ci_workflow="ci.yml"))

    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "trunk"],
                          capture_output=True, text=True, check=True).stdout.strip()
    ci = [c for c in gh_calls if c[1:3] == ["run", "list"]]
    assert len(ci) == 1, gh_calls
    query = ci[0]
    assert query[query.index("--workflow") + 1] == "ci.yml"
    assert query[query.index("--branch") + 1] == "trunk"
    assert query[query.index("--commit") + 1] == head
    assert dict(_verdicts(capsys.readouterr().out)).get("P11") == "PASS"


# --- each gh / ls-remote query runs at most once per invocation,
# --- except after a PR create or merge forgets the answers it can change ---

def _git_project(tmp_path: Path) -> Path:
    """A committed skill-run project, so `_build_ctx` resolves and the
    advisory step pass reaches the gh prechecks."""
    _seed_skill_status(tmp_path, fm="current_wave: 4\nexternal_yield: docs/x.md\n"
                                    "workflow: project-workflow\n")
    # The project tree's own config: the close reads it from there.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "project-workflow.json").write_text(
        '{"close": {"merge": "squash", "ci_workflow": "ci.yml"}}', encoding="utf-8")
    for args in (("init", "-q"),
                 ("-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "seed"),
                 ("add", "-A"),
                 ("-c", "user.email=t@example.com", "-c", "user.name=t",
                  "commit", "-q", "-m", "seed project")):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True)
    return tmp_path


def test_preflight_runs_no_gh_command_twice(close_project, monkeypatch,
                                            capsys, tmp_path):
    # One `--preflight` once ran `gh repo view` three times (gh-pr-create
    # precheck, gh-pr-merge precheck twice) and `gh auth status` twice.
    worktree = _git_project(tmp_path)
    real_run = close_project.subprocess.run
    commands: list[tuple[str, ...]] = []

    def fake(cmd, *a, **k):
        argv = tuple(str(c) for c in cmd)
        if argv[:1] == ("gh",) or "ls-remote" in argv:
            commands.append(argv)
            if argv[1:3] == ("repo", "view"):
                out = "o/r\n"
            elif argv[1:3] == ("pr", "view"):
                out = "OPEN\n"
            elif argv[1:3] == ("pr", "checks"):
                out = "[]"
            elif argv[1:3] == ("run", "list"):
                out = '[{"conclusion": "success", "url": "u"}]'
            else:
                out = ""
            return subprocess.CompletedProcess(list(cmd), 0, stdout=out, stderr="")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(close_project.subprocess, "run", fake)
    monkeypatch.setattr(close_project, "find_project_worktree",
                        lambda *a, **k: worktree)
    close_project.main(["close-project.py", "0077", "--preflight"])
    out = capsys.readouterr().out

    # The step pass really reached the gh prechecks; otherwise this is vacuous.
    assert "gh-pr-merge -> ok" in out, out
    assert ("gh", "auth", "status") in commands
    assert sum(1 for c in commands if c[1:3] == ("repo", "view")) == 1
    assert any(c[1:3] == ("pr", "checks") for c in commands)
    # Keyed by the command line alone: the same gh query from another cwd is
    # still a repeat.
    repeated = sorted({c for c in commands if commands.count(c) > 1})
    assert not repeated, f"ran more than once in one preflight: {repeated}"


def test_a_mutating_gh_call_forgets_volatile_answers(close_steps, tmp_path,
                                                     monkeypatch):
    # A merge changes what `gh pr view` and `git ls-remote` would say; the
    # account and the repo's name cannot change mid-close.
    calls: list[list[str]] = []

    def fake(cmd, *a, **k):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="x\n", stderr="")

    monkeypatch.setattr(close_steps.subprocess, "run", fake)
    ctx = _ctx(close_steps, tmp_path)
    queries = (lambda: close_steps._gh_auth_status(),
               lambda: close_steps.github_repo_name(tmp_path),
               lambda: close_steps._pr_state(ctx))
    for q in queries + queries:
        q()
    assert len(calls) == 3
    close_steps._gh_mutate(["pr", "merge", "b"], tmp_path)
    for q in queries:
        q()
    assert [c[1:3] for c in calls[3:]] == [["pr", "merge"], ["pr", "view"]]


def test_command_memo_reraises_a_missing_gh(close_steps, monkeypatch):
    calls: list[list[str]] = []

    def missing(cmd, *a, **k):
        calls.append(list(cmd))
        raise FileNotFoundError("gh")

    monkeypatch.setattr(close_steps.subprocess, "run", missing)
    for _ in range(2):
        with pytest.raises(FileNotFoundError):
            close_steps._gh_auth_status()
    assert len(calls) == 1


# --- teardown is the config's `teardown` argv ------------------------------

def test_worktree_remove_tears_down_junctions_first(close_steps, tmp_path,
                                                    monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr(close_steps, "_run", _fake_run(calls, "", wt))
    cfg = _cfg(teardown=("python", "tools/boot.py", "{worktree}", "--teardown"))
    close_steps._apply_worktree_remove(_ctx(close_steps, wt, cfg=cfg))

    teardown = [sys.executable, "tools/boot.py", str(wt.resolve()), "--teardown"]
    assert teardown in calls
    remove = next(i for i, c in enumerate(calls)
                  if "worktree" in c and "remove" in c)
    assert calls.index(teardown) < remove


def test_worktree_remove_refuses_when_teardown_fails(close_steps, tmp_path,
                                                     monkeypatch):
    wt = tmp_path / "wt"
    wt.mkdir()
    calls: list[list[str]] = []

    def run(cmd, cwd, check=True):
        calls.append(cmd)
        rc = 1 if "--teardown" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, stdout="",
                                           stderr="FAIL: could not remove junction")

    monkeypatch.setattr(close_steps, "_run", run)
    cfg = _cfg(teardown=("python", "boot.py", "{worktree}", "--teardown"))
    with pytest.raises(RuntimeError, match="--teardown` exited 1"):
        close_steps._apply_worktree_remove(_ctx(close_steps, wt, cfg=cfg))
    assert not any("worktree" in c and "remove" in c for c in calls)
    assert wt.is_dir()


def test_teardown_runs_the_configured_argv_for_real(close_steps, tmp_path):
    # End to end through expand_argv and the interpreter mapping: the command
    # exits 0 for an existing directory and 3 for a missing one.
    wt = tmp_path / "wt"
    wt.mkdir()
    close_steps.teardown_worktree(tmp_path, wt, TEARDOWN_OK)
    with pytest.raises(RuntimeError, match="exited 3"):
        close_steps.teardown_worktree(tmp_path, tmp_path / "absent", TEARDOWN_OK)


def test_no_teardown_configured_runs_nothing(close_steps, tmp_path, monkeypatch,
                                             capsys):
    calls: list = []
    monkeypatch.setattr(close_steps, "_run", lambda *a, **k: calls.append(a))
    close_steps.teardown_worktree(tmp_path, tmp_path, None)
    assert calls == []
    assert "no `teardown` configured" in capsys.readouterr().out


# --- close.merge local: a merge commit in the main checkout ---------------

def _local_repos(tmp_path: Path) -> tuple[Path, Path]:
    """(main checkout on `main`, project worktree on project/0027-skill-analysis
    holding one commit main lacks)."""
    main = tmp_path / "main"
    main.mkdir()
    git = lambda repo, *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
    git(main, "init", "-q", "-b", "main")
    (main / "a.txt").write_text("a\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "seed")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "project/0027-skill-analysis", str(wt))
    (wt / "b.txt").write_text("b\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-q", "-m", "work")
    return main, wt


def test_local_merge_makes_a_merge_commit_and_resumes(close_steps, tmp_path,
                                                      capsys):
    main, wt = _local_repos(tmp_path)
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx) is None
    close_steps._apply_local_merge(ctx)
    parents = subprocess.run(["git", "-C", str(main), "rev-list", "--parents",
                              "-n", "1", "main"], capture_output=True, text=True,
                             check=True).stdout.split()
    assert len(parents) == 3, "a --no-ff merge commit has two parents"
    assert (main / "b.txt").is_file()
    # A rerun finds the branch already merged and does nothing.
    ctx2 = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx2) is None
    close_steps._apply_local_merge(ctx2)
    assert "already merged" in capsys.readouterr().out


def test_local_merge_refuses_a_main_checkout_off_base_or_dirty(close_steps,
                                                                tmp_path):
    main, wt = _local_repos(tmp_path)
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local",
                                                        base_branch="trunk"))
    reason = close_steps._precheck_local_merge(ctx)
    assert reason and "not 'trunk'" in reason
    (main / "a.txt").write_text("dirty\n", encoding="utf-8")
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    reason = close_steps._precheck_local_merge(ctx)
    assert reason and "tracked changes" in reason


def test_local_merge_conflict_aborts_and_leaves_main_rerunnable(close_steps,
                                                                 tmp_path):
    # Both sides edit a.txt. The apply must not leave the main checkout
    # mid-merge: a MERGE_HEAD there makes every rerun's precheck refuse.
    main, wt = _local_repos(tmp_path)
    git = lambda repo, *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
    (wt / "a.txt").write_text("project side\n", encoding="utf-8")
    git(wt, "commit", "-q", "-am", "project edit")
    (main / "a.txt").write_text("base side\n", encoding="utf-8")
    git(main, "commit", "-q", "-am", "base edit")
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx) is None
    with pytest.raises(RuntimeError) as exc:
        close_steps._apply_local_merge(ctx)
    msg = str(exc.value)
    assert "a.txt" in msg, "the conflicting path is named"
    assert "merge main into project/0027-skill-analysis" in msg
    assert subprocess.run(["git", "-C", str(main), "rev-parse", "-q", "--verify",
                           "MERGE_HEAD"], capture_output=True).returncode != 0
    assert (main / "a.txt").read_text(encoding="utf-8") == "base side\n"
    status = subprocess.run(["git", "-C", str(main), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""
    ctx2 = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx2) is None


def _rev(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify",
                           ref], capture_output=True, text=True).stdout.strip()


def test_local_merge_leaves_a_foreign_merge_head_alone(close_steps, tmp_path):
    # Another session starts a merge in the main checkout AFTER our precheck
    # passed. Our `git merge` then fails; the abort path must not abort theirs.
    main, wt = _local_repos(tmp_path)
    git = lambda repo, *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
    git(main, "branch", "other")
    git(main, "checkout", "-q", "other")
    (main / "c.txt").write_text("c\n", encoding="utf-8")
    git(main, "add", "-A")
    git(main, "commit", "-q", "-m", "other work")
    git(main, "checkout", "-q", "main")
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx) is None
    git(main, "merge", "--no-ff", "--no-commit", "other")  # the foreign merge
    foreign = _rev(main, "MERGE_HEAD")
    assert foreign == _rev(main, "other")
    with pytest.raises(RuntimeError) as exc:
        close_steps._apply_local_merge(ctx)
    msg = str(exc.value)
    assert _rev(main, "MERGE_HEAD") == foreign, "their merge survives"
    assert (main / "c.txt").is_file(), "their staged work survives"
    assert foreign in msg and "MERGE_HEAD" in msg, "the foreign merge is named"
    assert "was aborted" not in msg


def test_local_merge_failure_with_a_foreign_merge_head_after_is_not_aborted(
        close_steps, tmp_path, monkeypatch):
    # No MERGE_HEAD before our merge; after it fails, MERGE_HEAD names a commit
    # that is not the project branch tip (a foreign merge began meanwhile).
    main, wt = _local_repos(tmp_path)
    git = lambda repo, *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
    (wt / "a.txt").write_text("project side\n", encoding="utf-8")
    git(wt, "commit", "-q", "-am", "project edit")
    (main / "a.txt").write_text("base side\n", encoding="utf-8")
    git(main, "commit", "-q", "-am", "base edit")
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx) is None
    seen = iter(["", "f" * 40])
    monkeypatch.setattr(close_steps, "_merge_head", lambda _cv: next(seen))
    with pytest.raises(RuntimeError) as exc:
        close_steps._apply_local_merge(ctx)
    assert "f" * 40 in str(exc.value) and "touched nothing" in str(exc.value)
    # The conflicted merge was left as is: nothing ran `git merge --abort`.
    assert _rev(main, "MERGE_HEAD") == _rev(main, "project/0027-skill-analysis")


def test_local_merge_race_foreign_merge_of_the_project_branch_is_not_aborted(
        close_steps, tmp_path, monkeypatch):
    # Another session starts a merge of the SAME project branch between our
    # MERGE_HEAD read and our `git merge`. Ours then fails (git refuses to run
    # over an unconcluded merge) and MERGE_HEAD equals the project tip, which
    # must not be mistaken for ours.
    main, wt = _local_repos(tmp_path)
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx) is None
    real_run = close_steps._run
    started: list = []

    def run(cmd, cwd, check=True):
        if "--no-edit" in cmd and not started:
            started.append(real_run(
                ["git", "-C", str(main), "merge", "--no-ff", "--no-commit",
                 "project/0027-skill-analysis"], cwd=main, check=False))
        return real_run(cmd, cwd, check)

    monkeypatch.setattr(close_steps, "_run", run)
    with pytest.raises(RuntimeError) as exc:
        close_steps._apply_local_merge(ctx)
    tip = _rev(main, "project/0027-skill-analysis")
    assert _rev(main, "MERGE_HEAD") == tip, "their merge survives"
    assert (main / "b.txt").is_file()
    assert "touched nothing" in str(exc.value)
    assert "was aborted" not in str(exc.value)


def test_local_merge_race_branch_advances_after_our_conflicted_merge_is_aborted(
        close_steps, tmp_path, monkeypatch):
    # The project branch gains a commit after our merge ran and conflicted but
    # before we look at MERGE_HEAD. The MERGE_HEAD is still ours (the sha we
    # merged), so it is aborted, not left behind.
    main, wt = _local_repos(tmp_path)
    git = lambda repo, *a: subprocess.run(  # noqa: E731
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *a], check=True, capture_output=True, text=True)
    (wt / "a.txt").write_text("project side\n", encoding="utf-8")
    git(wt, "commit", "-q", "-am", "project edit")
    (main / "a.txt").write_text("base side\n", encoding="utf-8")
    git(main, "commit", "-q", "-am", "base edit")
    ctx = _ctx(close_steps, wt, cv_root=main, cfg=_cfg(merge="local"))
    assert close_steps._precheck_local_merge(ctx) is None
    real_run = close_steps._run

    def run(cmd, cwd, check=True):
        r = real_run(cmd, cwd, check)
        if "--no-edit" in cmd:
            (wt / "c.txt").write_text("c\n", encoding="utf-8")
            git(wt, "add", "-A")
            git(wt, "commit", "-q", "-m", "late project commit")
        return r

    monkeypatch.setattr(close_steps, "_run", run)
    with pytest.raises(RuntimeError) as exc:
        close_steps._apply_local_merge(ctx)
    assert "the merge was aborted" in str(exc.value)
    assert "touched nothing" not in str(exc.value)
    assert _rev(main, "MERGE_HEAD") == ""
    assert (main / "a.txt").read_text(encoding="utf-8") == "base side\n"


# --- the close cursor lives in the shared .git directory -------------------

def test_cursor_lives_under_the_git_common_dir(close_steps, tmp_path,
                                               monkeypatch):
    monkeypatch.delenv(close_steps.CURSOR_ROOT_ENV, raising=False)
    main, wt = _local_repos(tmp_path)
    common = Path(subprocess.run(
        ["git", "-C", str(main), "rev-parse", "--path-format=absolute",
         "--git-common-dir"], capture_output=True, text=True,
        check=True).stdout.strip())
    want = common / "project-workflow" / "close-cursor" / "0027.json"
    # From the main checkout and from a linked worktree alike.
    assert close_steps.cursor_path(main, "0027") == want
    assert close_steps.cursor_path(wt, "0027") == want
    close_steps.record_step_done(main, "0027", "b", {"git-mv"})
    assert want.is_file()
    assert close_steps.load_cursor(main, "0027", "b") == {"git-mv"}
    status = subprocess.run(["git", "-C", str(main), "status", "--porcelain"],
                            capture_output=True, text=True, check=True).stdout
    assert status == ""
    close_steps.clear_cursor(main, "0027")
    assert not want.exists()


def test_cursor_outside_a_repo_degrades_to_no_resume(close_steps, tmp_path,
                                                     monkeypatch):
    monkeypatch.delenv(close_steps.CURSOR_ROOT_ENV, raising=False)
    assert close_steps.load_cursor(tmp_path, "0027", "b") == set()
    close_steps.clear_cursor(tmp_path, "0027")  # no raise
