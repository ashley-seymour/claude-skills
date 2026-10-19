"""brief-lint.py: the task-card lint for project briefs.

One failing-input test per rule (1-6), passing briefs (a filled one and the
shipped template), and the CLI contract (exit codes, the problem line names
card and line).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]  # the skill folder
BIN = ROOT / "scripts" / "bin"
TEMPLATE = ROOT / "templates" / "brief-template.md"


@pytest.fixture(scope="module")
def bl():
    name = "ps_brief_lint"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BIN / "brief-lint.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def card(sid="S-01", slug="split-amount", *, agent="stream-worker", after="none",
         mission="Add `split_amount(cents, parts)` to `shop/money.py`.",
         owns=("shop/money.py", "tests/test_money.py"),
         read=("shop/money.py",),
         decided="the result is a list, because callers zip it",
         done=("`python -m pytest tests/test_money.py -q` -> all pass",),
         stop="the standard triggers", drop=()) -> str:
    lines = [f"### {sid} {slug}"]
    fields = [
        ("Agent", [f"- Agent: {agent}"]),
        ("After", [f"- After: {after}"]),
        ("Mission", [f"- Mission: {mission}"]),
        ("Owns", ["- Owns:"] + [f"  - {p}" for p in owns]),
        ("Read first", ["- Read first:"] + [f"  - {p}" for p in read]),
        ("Decided", [f"- Decided: {decided}"]),
        ("Done when", ["- Done when:"] + [f"  - {d}" for d in done]),
        ("Stop and return", [f"- Stop and return: {stop}"]),
    ]
    for name, block in fields:
        if name not in drop:
            lines += block
    return "\n".join(lines) + "\n"


def brief(*cards: str) -> str:
    return "# Brief: demo\n\n## Waves\n\n" + "\n".join(cards) + "\n## Done when\n"


def rules(bl, text):
    problems, _ = bl.lint(text)
    return [(p.card, p.rule) for p in problems]


# -- passing ----------------------------------------------------------------

def test_filled_brief_passes(bl):
    text = brief(
        card(),
        card("S-02", "line-price", after="S-01",
             mission="Fix `line_price` in `shop/cart.py`; see `shop/money.py:12`.",
             owns=("shop/cart.py", "tests/**"),
             read=("D:/wt/shop/money.py",),
             done=("`python -m pytest D:/wt/tests/test_cart.py::test_a -q` -> pass,"
                   " with a new test in `tests/test_cart.py`",
                   "`git -C D:/wt status --short 2>/dev/null` -> only"
                   " `shop/cart.py`")),
    )
    # `D:/wt` itself (the git -C target) must be covered: name it in Read first.
    text = text.replace("  - D:/wt/shop/money.py", "  - D:/wt/shop/money.py\n  - D:/wt/")
    assert bl.lint(text)[0] == []


def test_template_lints_clean():
    r = subprocess.run([sys.executable, str(BIN / "brief-lint.py"), str(TEMPLATE)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("brief-lint: OK")
    assert len(r.stdout.strip().splitlines()) == 1


def test_fenced_and_commented_cards_are_ignored(bl):
    quoted = "```\n" + card("S-09", owns=("STATUS.md",)) + "```\n"
    commented = "<!--\n" + card("S-08", drop=("Agent",)) + "-->\n"
    assert bl.lint(brief(card(), quoted, commented))[0] == []


# -- rule 1 -----------------------------------------------------------------

def test_rule1_mission_path_outside_owns_and_read_first(bl):
    text = brief(card(mission="Edit `shop/money.py` and `shop/tax.py`."))
    assert rules(bl, text) == [("S-01", 1)]


def test_rule1_done_when_command_path_outside(bl):
    text = brief(card(done=("`python tools/check.py` -> exit 0",)))
    problems, _ = bl.lint(text)
    assert [(p.card, p.rule) for p in problems] == [("S-01", 1)]
    assert "tools/check.py" in problems[0].message


def test_rule1_ignores_code_words_and_placeholders(bl):
    text = brief(card(mission="`split_amount(100, 3)` gives `[34, 33, 33]`; "
                              "`shop.money` and `<path>` and `-q` are not paths."))
    assert rules(bl, text) == []


def test_rule1_placeholder_rooted_owns_covers_relative_path(bl):
    text = brief(card(mission="Edit `shop/money.py`.",
                      owns=("<wt>/shop/money.py", "<wt>/tests/test_money.py"),
                      read=("none",)))
    assert rules(bl, text) == []


def test_rule1_unbalanced_backtick_is_reported(bl):
    text = brief(card(mission="Edit `shop/money.py` and `shop/tax.py."))
    problems, _ = bl.lint(text)
    assert [(p.card, p.rule) for p in problems] == [("S-01", 1)]
    assert "backtick" in problems[0].message


def test_rule1_wrapped_span_reports_opening_line(bl):
    text = brief(card(done=("`python -m pytest\n    tests/other.py -q` -> pass",)))
    problems, _ = bl.lint(text)
    assert [(p.card, p.rule) for p in problems] == [("S-01", 1)]
    line = text.splitlines()[problems[0].line - 1]
    assert "python -m pytest" in line


# -- rule 2 -----------------------------------------------------------------

def test_rule2_done_when_item_without_backticked_command(bl):
    text = brief(card(done=("`python -m pytest tests -q` -> pass",
                            "the tests pass")))
    assert rules(bl, text) == [("S-01", 2)]


def test_rule2_done_when_with_no_item(bl):
    text = brief(card(done=()))
    # An empty field is also rule 6; rule 2 reports the missing command.
    assert ("S-01", 2) in rules(bl, text)


# -- rule 3 -----------------------------------------------------------------

def test_rule3_same_wave_cards_own_same_path(bl):
    text = brief(card(), card("S-02", "other", owns=("shop/money.py",),
                              done=("`x` -> y",)))
    assert rules(bl, text) == [("S-02", 3)]


def test_rule3_dir_glob_covers_files_under_it(bl):
    text = brief(card(), card("S-02", "other", owns=("shop/**",), read=("none",),
                              done=("`x` -> y",)))
    assert rules(bl, text) == [("S-02", 3)]


def test_rule3_different_waves_may_share(bl):
    text = brief(card(), card("S-02", "other", after="S-01",
                              owns=("shop/money.py",), done=("`x` -> y",)))
    assert rules(bl, text) == []


def mini(sid, after, owns):
    return card(sid, sid.lower(), after=after, owns=(owns,), read=("none",),
                mission="m", done=("`x` -> y",))


def test_rule3_same_after_same_wave(bl):
    # S-02 after S-01 and S-03 after S-01: both wave 2, so they clash.
    text = brief(mini("S-01", "none", "a.py"), mini("S-02", "S-01", "b.py"),
                 mini("S-03", "S-01", "b.py"))
    assert rules(bl, text) == [("S-03", 3)]


def test_rule3_wave_is_after_depth_not_after_set(bl):
    # S-03 after S-01 and S-04 after S-02 have different After sets but the
    # same depth (wave 2), so they clash; grouping by After set would miss it.
    text = brief(mini("S-01", "none", "a.py"), mini("S-02", "none", "b.py"),
                 mini("S-03", "S-01", "c.py"), mini("S-04", "S-02", "c.py"))
    assert rules(bl, text) == [("S-04", 3)]


def test_rule3_deeper_chain_is_a_later_wave(bl):
    # S-03 after S-02 after S-01 is wave 3; S-04 after S-01 is wave 2: no clash.
    text = brief(mini("S-01", "none", "a.py"), mini("S-02", "S-01", "b.py"),
                 mini("S-03", "S-02", "c.py"), mini("S-04", "S-01", "c.py"))
    assert rules(bl, text) == []


def test_rule3_placeholder_root_matches_relative(bl):
    text = brief(mini("S-01", "none", "<wt>/shop/x.py"),
                 mini("S-02", "none", "shop/x.py"))
    assert rules(bl, text) == [("S-02", 3)]


def test_rule3_different_placeholder_roots_do_not_match(bl):
    text = brief(mini("S-01", "none", "<wt>/shop/x.py"),
                 mini("S-02", "none", "<cfg-wt>/shop/x.py"))
    assert rules(bl, text) == []


def test_rule3_braced_placeholder_root_matches_relative(bl):
    # `{{wt}}/` is a placeholder root exactly as `<wt>/` is.
    text = brief(mini("S-01", "none", "{{wt}}/shop/x.py"),
                 mini("S-02", "none", "shop/x.py"))
    assert rules(bl, text) == [("S-02", 3)]


@pytest.mark.parametrize("other", ["<cfg-wt>/shop/x.py", "{{cfg}}/shop/x.py"])
def test_rule3_braced_root_differs_from_other_roots(bl, other):
    text = brief(mini("S-01", "none", "{{wt}}/shop/x.py"),
                 mini("S-02", "none", other))
    assert rules(bl, text) == []


def test_rule3_braced_root_matches_same_braced_root(bl):
    text = brief(mini("S-01", "none", "{{wt}}/shop/x.py"),
                 mini("S-02", "none", "{{wt}}/shop/x.py"))
    assert rules(bl, text) == [("S-02", 3)]


def test_rule1_braced_root_path_is_seen_and_covered(bl):
    # Seen: an uncovered `{{wt}}/...` path in Mission is a rule-1 problem.
    # Covered: by a relative Owns entry, or by the same braced path.
    uncovered = brief(card(mission="Edit `{{wt}}/shop/other.py`."))
    assert rules(bl, uncovered) == [("S-01", 1)]
    by_relative = brief(card(mission="Edit `{{wt}}/shop/money.py`."))
    assert rules(bl, by_relative) == []
    by_braced = brief(card(mission="Edit `shop/money.py`.",
                           owns=("{{wt}}/shop/money.py", "{{wt}}/tests/test_money.py"),
                           read=("none",)))
    assert rules(bl, by_braced) == []


def test_rule5_braced_root_ledger_directory(bl):
    text = brief(card(owns=("shop/money.py", "tests/test_money.py",
                            "{{wt}}/my-projects/**")))
    assert rules(bl, text) == [("S-01", 5)]


def test_rule3_inline_owns_lists_every_span(bl):
    text = brief(mini("S-01", "none", "a.py"),
                 card("S-02", "b", owns=(), read=("none",), mission="m",
                      done=("`x` -> y",)).replace("- Owns:", "- Owns: `b.py`, `a.py`"))
    assert rules(bl, text) == [("S-02", 3)]


# -- rule 4 -----------------------------------------------------------------

def test_rule4_unknown_stream(bl):
    text = brief(card(after="S-07"))
    assert rules(bl, text) == [("S-01", 4)]


def test_rule4_cycle(bl):
    text = brief(card(after="S-02", owns=("a.py",), read=("none",), mission="m",
                      done=("`x` -> y",)),
                 card("S-02", "b", after="S-01", owns=("b.py",), read=("none",),
                      mission="m", done=("`x` -> y",)))
    problems, _ = bl.lint(text)
    assert [p.rule for p in problems] == [4]
    assert "cycle" in problems[0].message


def test_rule4_duplicate_id(bl):
    text = brief(card(owns=("a.py",), read=("none",), mission="m", done=("`x` -> y",)),
                 card(owns=("b.py",), read=("none",), mission="m", done=("`x` -> y",)))
    assert rules(bl, text) == [("S-01", 4)]


# -- rule 5 -----------------------------------------------------------------

@pytest.mark.parametrize("ledger", [
    "my-projects/current/project-0001-demo/STATUS.md",
    "my-projects/README.md",
    "D:/wt/my-projects/backlog.md",
    "my-projects/current/project-0001-demo/suite-expected.json",
    "my-projects/**",
])
def test_rule5_ledger_in_owns(bl, ledger):
    text = brief(card(owns=("shop/money.py", "tests/test_money.py", ledger)))
    assert rules(bl, text) == [("S-01", 5)]


CONFIGURED = ("docs/projects/README.md", "docs/projects/backlog.md")


@pytest.mark.parametrize("entry, flagged", [
    ("docs/projects/README.md", True),
    ("D:/wt/docs/projects/backlog.md", True),
    ("docs/projects/**", True),
    ("docs/**", True),
    ("my-projects/README.md", False),
    ("my-projects/**", False),
])
def test_rule5_ledgers_follow_the_configured_paths(bl, entry, flagged):
    text = brief(card(owns=("shop/money.py", "tests/test_money.py", entry)))
    problems, _ = bl.lint(text, ledgers=CONFIGURED)
    assert [(p.card, p.rule) for p in problems] == ([("S-01", 5)] if flagged else [])


def test_cli_reads_the_ledgers_from_the_repo_config(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / ".claude" / "project-workflow.json").write_text(
        '{"projects_dir": "docs/projects"}', encoding="utf-8")
    folder = repo / "docs" / "projects" / "current" / "project-0001-demo"
    folder.mkdir(parents=True)
    f = folder / "brief.md"
    f.write_text(brief(card(owns=("shop/money.py", "tests/test_money.py",
                                  "docs/projects/backlog.md"))), encoding="utf-8")
    r = subprocess.run([sys.executable, str(BIN / "brief-lint.py"), str(f)],
                       capture_output=True, text=True)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "rule 5" in r.stdout and "docs/projects/backlog.md" in r.stdout


def test_rule5_suffix_is_component_bounded(bl):
    text = brief(card(owns=("shop/money.py", "tests/test_money.py",
                            "docs/NOT-STATUS.md")))
    assert rules(bl, text) == []


# -- rule 6 -----------------------------------------------------------------

@pytest.mark.parametrize("missing", ["Agent", "After", "Mission", "Owns",
                                     "Read first", "Decided", "Done when",
                                     "Stop and return"])
def test_rule6_missing_field(bl, missing):
    text = brief(card(drop=(missing,)))
    problems, _ = bl.lint(text)
    assert ("S-01", 6) in [(p.card, p.rule) for p in problems]
    assert any(missing in p.message for p in problems if p.rule == 6)


def _line_of(text, needle):
    return text.splitlines().index(needle) + 1


def test_rule6_unindented_continuation_is_reported(bl):
    text = brief(card(mission="Edit `shop/money.py`\nand `shop/tax.py` too."))
    problems, _ = bl.lint(text)
    assert [(p.card, p.rule, p.line) for p in problems] == [
        ("S-01", 6, _line_of(text, "and `shop/tax.py` too."))]


def test_rule6_owns_line_that_is_not_a_bullet(bl):
    text = brief(card().replace("  - tests/test_money.py",
                                "  - tests/test_money.py\n    STATUS.md"))
    problems, _ = bl.lint(text)
    assert [(p.card, p.rule, p.line) for p in problems] == [
        ("S-01", 6, _line_of(text, "    STATUS.md"))]


@pytest.mark.parametrize("heading", ["### S-05: slug", "#### S-05 slug",
                                     "###S-05 slug", "## S-05 slug"])
def test_rule6_malformed_card_heading_is_reported(bl, heading):
    bad = card("S-05").replace("### S-05 split-amount", heading)
    text = brief(card(), bad)
    problems, _ = bl.lint(text)
    assert ("S-05", 6, _line_of(text, heading)) in [
        (p.card, p.rule, p.line) for p in problems]


def test_rule6_no_cards_fails(bl):
    assert rules(bl, "# Brief\n\n## Waves\n\nnothing\n") == [("-", 6)]


# -- CLI --------------------------------------------------------------------

def test_cli_names_card_and_line(tmp_path):
    text = brief(card(), card("S-02", "other", owns=("shop/money.py",),
                              done=("`x` -> y",)))
    f = tmp_path / "brief.md"
    f.write_text(text, encoding="utf-8")
    r = subprocess.run([sys.executable, str(BIN / "brief-lint.py"), str(f)],
                       capture_output=True, text=True)
    assert r.returncode == 1
    first = r.stdout.splitlines()[0]
    lines = text.splitlines()
    line = lines.index("  - shop/money.py", lines.index("### S-02 other")) + 1
    assert first.startswith(f"{f}:{line}: S-02: rule 3: ")
    assert r.stdout.splitlines()[-1].startswith("brief-lint: 1 problem(s)")


def test_cli_usage_error():
    r = subprocess.run([sys.executable, str(BIN / "brief-lint.py")],
                       capture_output=True, text=True)
    assert r.returncode == 2
