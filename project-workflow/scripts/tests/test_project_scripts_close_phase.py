"""close-phase.py: tree lookup, Resume cap and `deadlines:`.

The lookup used `f"-{project_id}" in p.name`, which never matched
`<repo>-worktrees/<NNNN>-<slug>`; `find_status` then fell back to `main` and
wrote the handoff into main's frozen STATUS stub. It now calls the helper
wave-close.py shares (`streams.find_project_tree`) and refuses on ambiguity or
no match. These tests feed fake `git worktree list --porcelain` output.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
_BIN = ROOT / "scripts" / "bin"
_LIB = ROOT / "scripts" / "lib"

PID = "0070"
SLUG = "token-audit"


@pytest.fixture(scope="module")
def cp():
    if str(_LIB) not in sys.path:
        sys.path.insert(0, str(_LIB))
    spec = importlib.util.spec_from_file_location(
        "_close_phase_under_test", _BIN / "close-phase.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def streams(cp):
    # The same module object close-phase.py imported its lookup from.
    import streams as mod
    return mod


def porcelain(*entries: tuple[Path, str | None]) -> str:
    blocks = []
    for path, branch in entries:
        lines = [f"worktree {path.as_posix()}", "HEAD " + "0" * 40]
        lines.append(f"branch refs/heads/{branch}" if branch else "detached")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


RESUME_3 = """state:     wave 1 done
next:      /project-workflow 0070 wave 2
decisions: none
"""


def status_text(resume: str = RESUME_3, extra_fm: str = "",
                worktree: str = "D:/x") -> str:
    return (f'---\nproject: "{PID}"\nname: {SLUG}\nworktree: {worktree}\n'
            f"{extra_fm}current_phase: 1\n---\n\n# STATUS\n\n"
            f"## Resume\n\n{resume}\n## Log\n\n- wave 1\n")


def seed(tree: Path, text: str) -> Path:
    folder = tree / "my-projects" / "current" / f"project-{PID}-{SLUG}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "brief.md").write_text("# brief\n", encoding="utf-8")
    path = folder / "STATUS.md"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def layout(tmp_path, streams, cp, monkeypatch):
    """main checkout + a new-layout worktree, each with a STATUS.md."""
    monkeypatch.setattr(streams, "repo_basename", lambda: "demo-repo")
    main = tmp_path / "demo-repo"
    wt = tmp_path / "demo-repo-worktrees" / f"{PID}-{SLUG}"
    main_status = seed(main, status_text(resume="stub\n"))
    wt_status = seed(wt, status_text())
    monkeypatch.setattr(cp, "main_checkout", lambda: main)
    return main, wt, main_status, wt_status


def use_porcelain(streams, monkeypatch, text: str) -> None:
    monkeypatch.setattr(streams, "worktree_porcelain", lambda _m: text)


def prompt_file(tmp_path: Path, status: Path) -> Path:
    folder = status.parent
    body = "\n".join([
        f"Project {PID} ({SLUG}), wave 2.",
        f"Read {(folder / 'STATUS.md').as_posix()} first.",
        f"Then read {(folder / 'brief.md').as_posix()}.",
        "Run the wave-2 streams.",
        "Verify with the suite.",
        "Record the result in STATUS.",
        "Commit on the project branch.",
        "Stop at the wave-2 boundary.",
    ])
    path = tmp_path / "prompt.txt"
    path.write_text(body, encoding="utf-8")
    return path


# --- lookup --------------------------------------------------------------

def test_find_status_uses_the_new_layout_worktree(cp, streams, layout, monkeypatch):
    main, wt, _ms, wt_status = layout
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    assert cp.find_status(PID, main) == (wt_status, wt)


def test_find_status_falls_back_to_the_main_checkouts_config(cp, streams,
                                                             tmp_path, monkeypatch):
    # The worktree carries no config file, so main's `projects_dir` applies.
    monkeypatch.setattr(streams, "repo_basename", lambda: "demo-repo")
    main = tmp_path / "demo-repo"
    wt = tmp_path / "demo-repo-worktrees" / f"{PID}-{SLUG}"
    (main / ".claude").mkdir(parents=True)
    (main / ".claude" / "project-workflow.json").write_text(
        '{"projects_dir": "docs/projects"}', encoding="utf-8")
    folder = wt / "docs" / "projects" / "current" / f"project-{PID}-{SLUG}"
    folder.mkdir(parents=True)
    status = folder / "STATUS.md"
    status.write_text(status_text(), encoding="utf-8")
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    assert cp.find_status(PID, main) == (status, wt)


def test_find_status_refuses_on_ambiguity(cp, streams, layout, monkeypatch):
    main, wt, _ms, _ws = layout
    legacy = main.parent / f"demo-repo-{PID}"
    legacy.mkdir()
    use_porcelain(streams, monkeypatch, porcelain(
        (main, "main"), (wt, f"project/{PID}-{SLUG}"), (legacy, None)))
    with pytest.raises(streams.WorktreeLookupError, match="2 worktrees"):
        cp.find_status(PID, main)


def test_find_status_refuses_rather_than_falling_back_to_main(cp, streams, layout,
                                                              monkeypatch):
    main, _wt, _ms, _ws = layout
    use_porcelain(streams, monkeypatch, porcelain((main, "main")))
    with pytest.raises(streams.WorktreeLookupError, match="frozen stub"):
        cp.find_status(PID, main)


def test_find_status_accepts_main_for_a_declared_session_project(
        cp, streams, tmp_path, monkeypatch):
    monkeypatch.setattr(streams, "repo_basename", lambda: "demo-repo")
    main = tmp_path / "demo-repo"
    status = seed(main, status_text(worktree="none"))
    use_porcelain(streams, monkeypatch, porcelain((main, "main")))
    assert cp.find_status(PID, main) == (status, main)


def test_find_status_uses_main_on_the_project_branch(cp, streams, tmp_path,
                                                     monkeypatch):
    monkeypatch.setattr(streams, "repo_basename", lambda: "demo-repo")
    main = tmp_path / "demo-repo"
    status = seed(main, status_text())
    use_porcelain(streams, monkeypatch,
                  porcelain((main, f"project/{PID}-{SLUG}")))
    assert cp.find_status(PID, main) == (status, main)


def test_main_exits_2_and_names_status_when_unresolved(cp, streams, layout,
                                                       monkeypatch, capsys):
    main, _wt, main_status, _ws = layout
    before = main_status.read_text(encoding="utf-8")
    use_porcelain(streams, monkeypatch, porcelain((main, "main")))
    rc = cp.main(["close-phase.py", PID, "--check-only"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "REFUSE" in err and "--status" in err
    assert main_status.read_text(encoding="utf-8") == before


def test_close_phase_has_no_local_lookup_copy(cp, streams):
    assert not hasattr(cp, "find_worktree")
    assert cp.find_project_tree is streams.find_project_tree


# --- Resume cap ----------------------------------------------------------

def _write(cp, streams, layout, monkeypatch, tmp_path, *extra: str) -> int:
    main, wt, _ms, wt_status = layout
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    return cp.main(["close-phase.py", PID, "--wave", "2",
                    "--prompt-file", str(prompt_file(tmp_path, wt_status)),
                    *extra])


def test_write_within_the_cap_succeeds(cp, streams, layout, monkeypatch, tmp_path):
    _main, _wt, _ms, wt_status = layout
    rc = _write(cp, streams, layout, monkeypatch, tmp_path,
                "--state", "wave 1 done", "--next", "/project-workflow 0070 wave 2")
    assert rc == 0
    text = wt_status.read_text(encoding="utf-8")
    assert cp.resume_lines(text) == [
        "state:     wave 1 done",
        "next:      /project-workflow 0070 wave 2",
        "decisions: none",
    ]


def test_write_over_the_cap_refuses_and_writes_nothing(cp, streams, layout,
                                                       monkeypatch, tmp_path, capsys):
    _main, _wt, _ms, wt_status = layout
    before = wt_status.read_text(encoding="utf-8")
    narrative = "\n".join(f"step {i} happened" for i in range(6))
    rc = _write(cp, streams, layout, monkeypatch, tmp_path,
                "--state", narrative, "--next", "wave 2")
    assert rc == 1
    assert "cap is 6" in capsys.readouterr().err
    assert wt_status.read_text(encoding="utf-8") == before


def test_check_only_refuses_an_overlong_resume(cp, streams, layout, monkeypatch,
                                               tmp_path, capsys):
    main, wt, _ms, wt_status = layout
    seven = "".join(f"line {i}\n" for i in range(7))
    text = status_text(resume=seven)
    body = prompt_file(tmp_path, wt_status).read_text(encoding="utf-8")
    wt_status.write_text(text + f"\n## Next session prompt\n\n```\n{body}\n```\n",
                         encoding="utf-8")
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    rc = cp.main(["close-phase.py", PID, "--check-only"])
    assert rc == 1
    assert "7 non-blank lines" in capsys.readouterr().err


def test_resume_cap_boundary(cp):
    six = "".join(f"line {i}\n" for i in range(6))
    assert cp.check_resume(cp.resume_lines(status_text(resume=six))) == []
    seven = six + "line 6\n"
    assert cp.check_resume(cp.resume_lines(status_text(resume=seven)))


# --- deadlines: ----------------------------------------------------------

DEADLINE_FM = 'deadlines: ["2026-10-13 beta capture"]\n'


def test_deadlines_preserved_when_not_passed(cp, streams, layout, monkeypatch,
                                             tmp_path):
    _main, _wt, _ms, wt_status = layout
    wt_status.write_text(status_text(extra_fm=DEADLINE_FM), encoding="utf-8")
    rc = _write(cp, streams, layout, monkeypatch, tmp_path,
                "--state", "s", "--next", "n")
    assert rc == 0
    assert DEADLINE_FM in wt_status.read_text(encoding="utf-8")


def test_deadline_flag_sets_the_field(cp, streams, layout, monkeypatch, tmp_path):
    _main, _wt, _ms, wt_status = layout
    rc = _write(cp, streams, layout, monkeypatch, tmp_path,
                "--state", "s", "--next", "n",
                "--deadline", "2026-10-13 beta capture",
                "--deadline", "2026-11-01 release capture")
    assert rc == 0
    text = wt_status.read_text(encoding="utf-8")
    assert ('deadlines: ["2026-10-13 beta capture", "2026-11-01 release capture"]\n'
            in text)


def test_no_deadlines_flag_sets_none(cp, streams, layout, monkeypatch, tmp_path):
    _main, _wt, _ms, wt_status = layout
    wt_status.write_text(status_text(extra_fm=DEADLINE_FM), encoding="utf-8")
    rc = _write(cp, streams, layout, monkeypatch, tmp_path,
                "--state", "s", "--next", "n", "--no-deadlines")
    assert rc == 0
    assert "\ndeadlines: none\n" in wt_status.read_text(encoding="utf-8")


@pytest.mark.parametrize("bad", ["soon", "2026-13-40 beta", "2026-10-13"])
def test_malformed_deadline_refuses(cp, streams, layout, monkeypatch, tmp_path,
                                    capsys, bad):
    _main, _wt, _ms, wt_status = layout
    before = wt_status.read_text(encoding="utf-8")
    rc = _write(cp, streams, layout, monkeypatch, tmp_path,
                "--state", "s", "--next", "n", "--deadline", bad)
    assert rc == 1
    assert "refuse:" in capsys.readouterr().err
    assert wt_status.read_text(encoding="utf-8") == before


# --- Resume and prompt line caps, prompt path resolution -----------------

def test_template_resume_passes_the_cap(cp):
    # The template's marker + guidance comment counted as 8
    # lines, so every freshly scaffolded STATUS.md read as over the cap.
    template = (ROOT / "templates" / "status-template.md").read_text(encoding="utf-8")
    lines = cp.resume_lines(template)
    assert lines, "the template has a ## Resume section with content"
    assert not any("<!--" in ln or "-->" in ln for ln in lines)
    assert cp.check_resume(lines) == []


def _prompt_body(n: int, tmp_path: Path) -> str:
    a, b = tmp_path / "a.md", tmp_path / "b.md"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")
    head = [f"Project {PID}, wave 2.", f"Read {a.as_posix()}.",
            f"Read {b.as_posix()}.", "Stop at the boundary."]
    return "\n".join(head + [f"pointer {i}" for i in range(n - len(head))])


def test_prompt_at_the_20_line_cap_is_well_formed(cp, tmp_path):
    assert cp.check_prompt(_prompt_body(20, tmp_path), PID, tmp_path) == []


def test_a_path_ending_a_sentence_resolves_without_its_full_stop(cp, tmp_path):
    # Linux CI: "Read <abs>/a.md." captured `a.md.` (and dropped the leading
    # slash). Windows resolves a trailing dot away, so only Linux failed; a
    # name with a trailing dot is built here so the check cannot lean on that.
    real = tmp_path / "a.md"
    real.write_text("x", encoding="utf-8")
    assert cp.path_candidates(f"Read {real.as_posix()}.") == {real.as_posix()}
    posix = "/home/runner/work/x/a.md"
    assert cp.path_candidates(f"Read {posix}.") == {posix}


def test_prompt_over_the_20_line_cap_refuses(cp, streams, layout, monkeypatch,
                                             tmp_path, capsys):
    main, wt, _ms, wt_status = layout
    before = wt_status.read_text(encoding="utf-8")
    long_prompt = tmp_path / "long.txt"
    long_prompt.write_text(_prompt_body(21, tmp_path), encoding="utf-8")
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    rc = cp.main(["close-phase.py", PID, "--prompt-file", str(long_prompt),
                  "--state", "s", "--next", "n"])
    assert rc == 1
    assert "the cap is 20" in capsys.readouterr().err
    assert wt_status.read_text(encoding="utf-8") == before


# --- a --prompt-file that carries its own fence -----------------

@pytest.mark.parametrize("raw", [
    "line one\nline two",
    "```\nline one\nline two\n```",
    "```text\nline one\nline two\n```\n",
    "\n\n```\nline one\nline two\n```\n\n",
])
def test_unfence_prompt_strips_one_enclosing_fence(cp, raw):
    assert cp.unfence_prompt(raw) == ("line one\nline two", None)


@pytest.mark.parametrize("raw", [
    "```\n```\nline one\n```\n```",          # the wave-2 double fence
    "line one\n```\ncode\n```\nline two",   # an inner block in a raw prompt
])
def test_unfence_prompt_refuses_an_inner_fence(cp, raw):
    body, refusal = cp.unfence_prompt(raw)
    assert body is None and "fence" in refusal


def test_fenced_prompt_file_writes_one_fence_and_check_only_reads_it(
        cp, streams, layout, monkeypatch, tmp_path, capsys):
    # Wave 2: the write passed, then --check-only read an empty block.
    main, wt, _ms, wt_status = layout
    raw = prompt_file(tmp_path, wt_status)
    fenced = tmp_path / "fenced.txt"
    fenced.write_text("```\n" + raw.read_text(encoding="utf-8") + "\n```\n",
                      encoding="utf-8")
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    assert cp.main(["close-phase.py", PID, "--prompt-file", str(fenced),
                    "--state", "s", "--next", "n"]) == 0
    text = wt_status.read_text(encoding="utf-8")
    assert text.count("```") == 2
    assert cp.extract_prompt(text).strip() == raw.read_text(encoding="utf-8").strip()
    assert cp.main(["close-phase.py", PID, "--check-only"]) == 0
    assert "PASS (--check-only)" in capsys.readouterr().out


def test_inner_fence_prompt_file_refuses_and_writes_nothing(
        cp, streams, layout, monkeypatch, tmp_path, capsys):
    main, wt, _ms, wt_status = layout
    before = wt_status.read_text(encoding="utf-8")
    raw = prompt_file(tmp_path, wt_status).read_text(encoding="utf-8")
    nested = tmp_path / "nested.txt"
    nested.write_text("```\n```\n" + raw + "\n```\n```\n", encoding="utf-8")
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    assert cp.main(["close-phase.py", PID, "--prompt-file", str(nested),
                    "--state", "s", "--next", "n"]) == 1
    assert "fence" in capsys.readouterr().err
    assert wt_status.read_text(encoding="utf-8") == before


# --- --status roots prompt paths at the STATUS's own repo -------

def _worktree_only_prompt(tmp_path: Path, git_init: bool):
    """A worktree holding files main lacks, and a prompt citing them relatively."""
    main = tmp_path / "demo-repo"
    main.mkdir()
    wt = tmp_path / "demo-repo-worktrees" / f"{PID}-{SLUG}"
    status = seed(wt, status_text())
    (wt / "notes").mkdir()
    (wt / "notes" / "only-here.md").write_text("x", encoding="utf-8")
    (wt / "notes" / "also-here.md").write_text("y", encoding="utf-8")
    if git_init:
        import subprocess
        subprocess.run(["git", "init", "-q", str(wt)], check=True)
    body = "\n".join([
        f"Project {PID} ({SLUG}), wave 2.",
        "Read notes/only-here.md first.",
        "Then read notes/also-here.md.",
        "Run the wave-2 streams.",
        "Verify with the suite.",
        "Record the result in STATUS.",
        "Commit on the project branch.",
        "Stop at the wave-2 boundary.",
    ])
    prompt = tmp_path / "prompt.txt"
    prompt.write_text(body, encoding="utf-8")
    return main, status, prompt


def test_status_flag_resolves_prompt_paths_in_the_status_worktree(
        cp, tmp_path, monkeypatch, capsys):
    main, status, prompt = _worktree_only_prompt(tmp_path, git_init=True)
    monkeypatch.setattr(cp, "main_checkout", lambda: main)
    rc = cp.main(["close-phase.py", PID, "--status", str(status),
                  "--prompt-file", str(prompt), "--check-only"])
    assert rc == 0, capsys.readouterr().err
    assert "well-formed" in capsys.readouterr().out


def test_status_flag_outside_any_repo_falls_back_to_main(
        cp, tmp_path, monkeypatch, capsys):
    # Control: with no repo around the STATUS the root stays main, where the
    # worktree-only files do not exist, so the same prompt refuses.
    main, status, prompt = _worktree_only_prompt(tmp_path, git_init=False)
    monkeypatch.setattr(cp, "main_checkout", lambda: main)
    rc = cp.main(["close-phase.py", PID, "--status", str(status),
                  "--prompt-file", str(prompt), "--check-only"])
    assert rc == 1
    assert "names 0 readable file(s)" in capsys.readouterr().err


# --- --wave writes current_wave ----------------------------------------
# The skill schema bumps `current_wave`. `--phase` / `--phase-state` (which
# wrote `current_phase` / `phase_state`) were removed.

def _frontmatter(text: str) -> str:
    return text.split("---\n")[1]


def _write_flags(cp, streams, layout, monkeypatch, tmp_path, *flags: str) -> int:
    main, wt, _ms, wt_status = layout
    use_porcelain(streams, monkeypatch,
                  porcelain((main, "main"), (wt, f"project/{PID}-{SLUG}")))
    return cp.main(["close-phase.py", PID, *flags,
                    "--prompt-file", str(prompt_file(tmp_path, wt_status)),
                    "--state", "s", "--next", "n"])


def test_wave_flag_bumps_current_wave_and_leaves_an_existing_current_phase(
        cp, streams, layout, monkeypatch, tmp_path, capsys):
    _main, _wt, _ms, wt_status = layout
    wt_status.write_text(status_text(extra_fm="current_wave: 3\n"),
                         encoding="utf-8")
    assert _write_flags(cp, streams, layout, monkeypatch, tmp_path,
                        "--wave", "4") == 0
    fm = _frontmatter(wt_status.read_text(encoding="utf-8"))
    assert "\ncurrent_wave: 4\n" in "\n" + fm
    assert "current_wave: 3" not in fm
    assert "\ncurrent_phase: 1" in "\n" + fm
    assert "current_wave = 4" in capsys.readouterr().out


def test_wave_flag_leaves_existing_phase_keys_untouched(
        cp, streams, layout, monkeypatch, tmp_path):
    _main, _wt, _ms, wt_status = layout
    wt_status.write_text(
        status_text(extra_fm="current_wave: 3\nphase_state: x\n"),
        encoding="utf-8")
    assert _write_flags(cp, streams, layout, monkeypatch, tmp_path,
                        "--wave", "4") == 0
    fm = "\n" + _frontmatter(wt_status.read_text(encoding="utf-8"))
    assert "\ncurrent_wave: 4\n" in fm
    assert "\ncurrent_phase: 1\n" in fm
    assert "\nphase_state: x\n" in fm


def test_wave_flag_adds_current_wave_when_absent(cp, streams, layout,
                                                 monkeypatch, tmp_path):
    _main, _wt, _ms, wt_status = layout
    assert _write_flags(cp, streams, layout, monkeypatch, tmp_path,
                        "--wave", "2") == 0
    assert "\ncurrent_wave: 2" in _frontmatter(wt_status.read_text(encoding="utf-8"))


def test_no_wave_flag_leaves_current_wave_and_writes_no_phase_keys(
        cp, streams, layout, monkeypatch, tmp_path, capsys):
    # The after-last-wave close passes no --wave: current_wave stays as found,
    # and the legacy current_phase already in the file is neither bumped nor
    # deleted, nor is phase_state added.
    _main, _wt, _ms, wt_status = layout
    wt_status.write_text(status_text(extra_fm="current_wave: 3\n"),
                         encoding="utf-8")
    assert _write_flags(cp, streams, layout, monkeypatch, tmp_path) == 0
    fm = "\n" + _frontmatter(wt_status.read_text(encoding="utf-8"))
    assert "\ncurrent_wave: 3\n" in fm
    assert "\ncurrent_phase: 1\n" in fm
    assert "phase_state" not in fm
    out = capsys.readouterr().out
    assert "current_phase" not in out
    assert "current_wave left as found" in out


@pytest.mark.parametrize("flag", ["--phase", "--phase-state"])
def test_removed_phase_flags_are_rejected_by_argparse(
        cp, streams, layout, monkeypatch, tmp_path, capsys, flag):
    _main, _wt, _ms, wt_status = layout
    before = wt_status.read_text(encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _write_flags(cp, streams, layout, monkeypatch, tmp_path, flag, "2")
    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
    assert wt_status.read_text(encoding="utf-8") == before


def test_wave_flag_rejects_a_non_integer(cp, streams, layout, monkeypatch,
                                         tmp_path):
    _main, _wt, _ms, wt_status = layout
    before = wt_status.read_text(encoding="utf-8")
    with pytest.raises(SystemExit):
        _write_flags(cp, streams, layout, monkeypatch, tmp_path, "--wave", "four")
    assert wt_status.read_text(encoding="utf-8") == before

