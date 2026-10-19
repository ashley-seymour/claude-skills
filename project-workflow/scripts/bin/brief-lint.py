#!/usr/bin/env python3
"""Lint the task cards of a project brief.

Usage: ``brief-lint.py <brief.md>``

Exit 0 with one ``brief-lint: OK`` line when every card passes; exit 1 with one
line per problem (``<file>:<line>: <card id>: rule <n>: <message>``) otherwise;
exit 2 on a usage error, an unreadable file or an invalid repo config.
Standard library only; the one import from the skill's ``scripts/lib`` is
``config``, for the index and backlog paths rule 5 checks.

Task card
---------
A card is a level-3 heading ``### S-<digits>[letter] <slug>`` (or the template
placeholder ``### S-NN <slug>``) and runs to the next heading of level 1-3. Its
fields are the lines that start, unindented, with ``- <Name>:``; a field's value
is the text after the colon plus every following indented or blank line, up to
the next unindented line. The eight fields are ``Agent``, ``After``,
``Mission``, ``Owns``, ``Read first``, ``Decided``, ``Done when`` and
``Stop and return``. Lines inside fenced code blocks (three backticks or
``~~~``) and inside ``<!-- -->`` comments are ignored, so a card quoted in a
fence or a comment is not linted. A brief with no card at all fails: a brief in
the old table-only format would otherwise pass unchecked.

No input inside a card is dropped silently. An unindented, non-blank line that
is not a field line (markdown lazy continuation, a misspelt field name) and an
indented line before the first field are rule-6 problems. A heading at level
2-6 that starts like a card id but is not exactly ``### S-NN <slug>``
(``### S-05: slug``, ``#### S-05 slug``, ``###S-05 slug``, ``## S-05 slug``) is
a rule-6 problem; it ends any open card and its lines are not linted.

List fields
-----------
``Owns`` and ``Read first`` entries come from the inline value and every ``-``
bullet line of the field, at any depth; any other non-blank line in the field
is a rule-6 problem. An item names its first whitespace-separated word (unless
that word opens a backticked span) plus every backticked span, so
``- tests/x.py (new)`` names ``tests/x.py`` and ``- Owns: `a.py`, `b.py```
names both. A word that is a placeholder (below) or ``none`` names nothing. An
entry
ending in ``/**`` or ``/`` is a directory and covers every path under it; an
entry with another ``*`` is a glob matched with ``fnmatch``; any other entry
covers itself and, read as a directory, every path under it.

What counts as a path (rule 1's coverage list)
----------------------------------------------
Rule 1 scans every backticked span in a card's ``Mission`` and ``Done when``
(commands and expected results alike) and splits each span into words with
``shlex`` (falling back to whitespace on unbalanced quotes), after turning
``\\`` into ``/``. A word ``--opt=value`` is read as ``value``; a leading shell
redirection (``>``, ``2>``, ``>>``) is dropped. The word is then trimmed of
trailing ``,;.)``, a ``::node`` pytest suffix and a ``:<line>[-<line>]`` suffix.
What remains is a path when all of these hold:

- it contains ``/``, or its last component ends in a known file extension
  (``KNOWN_EXTENSIONS``); so ``tests``, ``-q`` and ``bulk_discount`` are not
  paths, and neither is ``shop.money`` (``money`` is not a known extension);
- it uses only letters, digits and ``_ . - / ~ + @ *``, a leading drive
  ``X:``, and whole ``<name>`` or ``{{name}}`` placeholders; so code such as
  ``split_amount(100, 3)``, ``list[int]`` or a quoted ``-c`` program is not a
  path, and a path inside such code is not seen;
- it is not a URL (``://``), not one of ``EXEMPT_PATHS`` (``/dev/null`` and
  kin), not a placeholder alone, and holds at least one letter or digit.

A backticked ratio or alternative such as ``cents/parts`` therefore reads as a
path; write it without backticks or list it in ``Read first``.

A path is covered when an ``Owns`` or ``Read first`` entry covers it. A path or
entry is rooted when it starts with ``/``, a drive ``X:/``, or a ``<name>/`` or
``{{name}}/`` placeholder component (``<wt>/shop/x.py``,
``{{wt}}/shop/x.py``). When one side is rooted and the other relative, the
rooted one also matches through each of its trailing component suffixes, so
``D:/wt/templates/x.md`` and ``<wt>/templates/x.md`` each match
``templates/x.md``. Two rooted sides match only when equal, or one under the
other, as written: ``<wt>/x.py``, ``<cfg-wt>/x.py`` and ``{{wt}}/x.py`` all
differ from one another. The same matching decides rule 3's overlaps and rule
5's ledger directories. Names are compared case-sensitively. A span left open
by an odd number of backticks in ``Mission`` or ``Done when`` is a rule-1
problem, since the text after it cannot be scanned.

Placeholders
------------
A placeholder is a whole value of the form ``<...>`` (``<path>``,
``<command>``, ``<stream ids or none>``) or ``{{...}}``. A path word made only of a placeholder is
not a path; an ``Owns`` or ``Read first`` entry made only of a placeholder names
nothing; an ``After`` value made only of a placeholder means no dependency. A
placeholder that is part of a longer word (``<wt>/tools/x.py``) is kept as
literal text; a leading one is a root (see above). ``S-NN`` is accepted
as a card id. With these rules the skill's ``templates/brief-template.md`` lints clean.

Rules
-----
1. A path in ``Mission`` or ``Done when`` is not covered by ``Owns`` or
   ``Read first``.
2. A ``Done when`` item does not begin with a backticked command, or the field
   holds no item. Items are the ``-`` sub-bullets (with their continuation
   lines) and a non-empty inline value.
3. Two cards in one wave own overlapping paths. A card's wave is its ``After``
   depth: 1 with no dependency, else 1 + the deepest dependency's wave. Cards
   on a cycle or with an unknown dependency get no wave and are skipped here.
4. ``After`` names an unknown stream id, the ``After`` graph has a cycle, or
   two cards share an id (which ``After`` could not resolve). ``After`` is
   ``none``, ``-``, an em dash, a placeholder, or ids separated by commas or
   whitespace.
5. An ``Owns`` entry is a shared ledger: its path ends, at a component
   boundary, in one of ``BARE_LEDGERS`` or in the repo's ``index_file`` or
   ``backlog_file``, or it is a directory or glob that covers either of those
   two. They are read from the config of the checkout holding the brief
   (``ledgers_for``; the defaults when it has none). A directory
   that might hold a bare-named ledger (``STATUS.md``, ``suite-expected.json``)
   is not flagged: the lint cannot know what the directory holds.
6. A card lacks one of the eight fields, gives a field twice, or leaves one
   empty; a card holds a line that is read as nothing, or an ``Owns`` /
   ``Read first`` line that is not a bullet; a heading is a near-miss card
   heading (all under "Task card" and "List fields" above).
"""
from __future__ import annotations

import fnmatch
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from config import Config, ConfigError, RepoError, config_at, repo_toplevel  # noqa: E402

FIELDS = ("Agent", "After", "Mission", "Owns", "Read first", "Decided",
          "Done when", "Stop and return")
# Ledgers every project folder holds under these bare names.
BARE_LEDGERS = ("STATUS.md", "suite-expected.json")
# The repo's index and backlog, from its config; these are the defaults.
DEFAULT_LEDGERS = (Config().index_file, Config().backlog_file)
KNOWN_EXTENSIONS = frozenset({
    "py", "pyi", "md", "json", "jsonl", "yaml", "yml", "toml", "txt", "csv",
    "tsv", "ps1", "sh", "bat", "cmd", "cfg", "ini", "db", "sqlite", "sql", "js",
    "ts", "tsx", "html", "css", "xml", "lock", "log", "ipynb", "parquet", "png",
    "jpg", "svg", "pdf", "env",
})
EXEMPT_PATHS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
NONE_VALUES = frozenset({"none", "-", "\u2014", "\u2013", "n/a"})

CARD_RE = re.compile(r"^###\s+(S-(?:\d+[a-z]?|NN))(?=\s|$)")
# A heading at any level 2-6 that starts like a card id but is not CARD_RE:
# `### S-05: slug`, `#### S-05 slug`, `###S-05 slug`, `## S-05 slug`.
NEAR_CARD_RE = re.compile(r"^#{2,6}\s*(S-(?:\d+[a-z]?|NN))")
HEADING_RE = re.compile(r"^#{1,3}\s")
LIST_FIELDS = ("Owns", "Read first")
FIELD_RE = re.compile(r"^- (" + "|".join(re.escape(f) for f in FIELDS) + r"):(.*)$")
BULLET_RE = re.compile(r"^(\s+)- (.*)$")
SPAN_RE = re.compile(r"`([^`]+)`")
PLACEHOLDER_RE = re.compile(r"^(<[^<>]+>|\{\{[^{}]+\}\})$")
INNER_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>|\{\{[^{}\s/]+\}\}")
PLACEHOLDER_ROOT_RE = re.compile(r"^(?:<[^<>\s/]+>|\{\{[^{}\s/]+\}\})/")
PATH_CHARS_RE = re.compile(r"^(?:[A-Za-z]:)?[A-Za-z0-9_.\-/~+@*]+$")


@dataclass
class Field:
    name: str
    line: int                      # 1-based line of the `- Name:` line
    lines: list[tuple[int, str]]   # (line number, text) incl. the field line


@dataclass
class Card:
    sid: str
    line: int
    fields: dict[str, Field] = field(default_factory=dict)
    duplicates: list[Field] = field(default_factory=list)
    # Unindented, non-blank lines inside the card that are not a field line
    # (markdown lazy continuation, a misspelt field name, stray prose).
    strays: list[int] = field(default_factory=list)


@dataclass
class Problem:
    line: int
    card: str
    rule: int
    message: str


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def _blank_comments(text: str) -> str:
    """Replace `<!-- -->` regions with their newlines, keeping line numbers."""
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"),
                  text, flags=re.DOTALL)


def parse_cards(text: str) -> tuple[list[Card], list[Problem]]:
    """The cards, and a rule-6 problem per near-miss card heading."""
    cards: list[Card] = []
    near: list[Problem] = []
    card: Card | None = None
    current: Field | None = None
    in_fence = False
    for n, raw in enumerate(_blank_comments(text).splitlines(), start=1):
        line = raw.rstrip()
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = CARD_RE.match(line)
        if m:
            card = Card(m.group(1), n)
            cards.append(card)
            current = None
            continue
        nm = NEAR_CARD_RE.match(line)
        if nm:
            near.append(Problem(n, nm.group(1), 6,
                                "heading looks like a task card but is not "
                                "`### S-NN <slug>`; the card is not linted"))
            card = current = None
            continue
        if HEADING_RE.match(line):
            card = current = None
            continue
        if card is None:
            continue
        fm = FIELD_RE.match(line)
        if fm:
            current = Field(fm.group(1), n, [(n, fm.group(2))])
            if fm.group(1) in card.fields:
                card.duplicates.append(current)
            else:
                card.fields[fm.group(1)] = current
            continue
        if not line:
            if current is not None:
                current.lines.append((n, line))
            continue
        if current is not None and line[0].isspace():
            current.lines.append((n, line))
            continue
        card.strays.append(n)
        current = None
    return cards, near


def _inline(f: Field) -> str:
    return f.lines[0][1].strip()


def _items(f: Field) -> list[tuple[int, str]]:
    """The field's items: a non-empty inline value, then each `-` sub-bullet
    with its continuation lines joined by a space."""
    items: list[tuple[int, str]] = []
    inline = _inline(f)
    if inline:
        items.append((f.line, inline))
    bullet_indent: int | None = None
    for n, text in f.lines[1:]:
        if not text.strip():
            continue
        bm = BULLET_RE.match(text)
        if bm and (bullet_indent is None or len(bm.group(1)) <= bullet_indent):
            bullet_indent = len(bm.group(1))
            items.append((n, bm.group(2).strip()))
        elif items:
            ln, prev = items[-1]
            items[-1] = (ln, prev + " " + text.strip())
        else:
            items.append((n, text.strip()))
    return items


def _is_placeholder(s: str) -> bool:
    return bool(PLACEHOLDER_RE.match(s.strip()))


def _entry_words(item: str) -> list[str]:
    """The paths an Owns / Read first item names: its first word unless that
    opens a backticked span, plus every backticked span. Placeholders and
    `none` name nothing."""
    if _is_placeholder(item):
        return []
    words = [s.strip() for s in SPAN_RE.findall(item)]
    first = (item.split() or [""])[0]
    if not first.startswith("`"):
        words.insert(0, first)
    return [normalize(w) for w in words
            if w and not _is_placeholder(w)
            and w.lower().rstrip(".") not in NONE_VALUES]


def _list_items(f: Field) -> list[tuple[int, str]]:
    """A list field's items: the inline value, then every `-` bullet line at
    any depth. Other non-blank lines are rule-6 problems (`_non_bullets`)."""
    items = [(f.line, _inline(f))] if _inline(f) else []
    for n, text in f.lines[1:]:
        bm = BULLET_RE.match(text)
        if bm:
            items.append((n, bm.group(2).strip()))
    return items


def _non_bullets(f: Field) -> list[int]:
    return [n for n, text in f.lines[1:]
            if text.strip() and not BULLET_RE.match(text)]


def _entries(f: Field | None) -> list[tuple[int, str]]:
    if f is None:
        return []
    return [(n, e) for n, item in _list_items(f) for e in _entry_words(item)]


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def normalize(word: str) -> str:
    w = word.strip().strip("'\"").replace("\\", "/")
    while w.startswith("./"):
        w = w[2:]
    return w


def _trim(word: str) -> str:
    w = normalize(word)
    if w.startswith("-") and "=" in w:
        w = w.split("=", 1)[1]
    w = re.sub(r"^\d*>>?", "", w)
    w = w.rstrip(",;.)")
    w = re.sub(r"::.*$", "", w)
    w = re.sub(r"(?<=[^:/]):\d+(?:-\d+)?$", "", w)
    return w


def path_of(word: str) -> str | None:
    """`word` as a path under the module docstring's definition, else None."""
    w = _trim(word)
    if not w or _is_placeholder(w) or "://" in w or w in EXEMPT_PATHS:
        return None
    bare = INNER_PLACEHOLDER_RE.sub("x", w)
    if (not PATH_CHARS_RE.match(bare) or bare.startswith("-")
            or not re.search(r"[A-Za-z0-9]", bare)):
        return None
    last = w.rstrip("/").rsplit("/", 1)[-1]
    stem, dot, ext = last.rpartition(".")
    if "/" in w or (dot and stem and ext.lower() in KNOWN_EXTENSIONS):
        return w
    return None


def _split_words(span: str) -> list[str]:
    s = span.replace("\\", "/")
    try:
        return shlex.split(s, posix=True)
    except ValueError:
        return s.split()


def _is_abs(p: str) -> bool:
    """Rooted: a leading `/`, a drive `X:/`, or a leading `<name>/` or
    `{{name}}/` placeholder component (`<wt>/shop/x.py`), which stands for some
    root directory."""
    return (p.startswith("/") or bool(re.match(r"^[A-Za-z]:/", p))
            or bool(PLACEHOLDER_ROOT_RE.match(p)))


def _suffixes(p: str) -> list[str]:
    """Trailing component suffixes of `p`, shortest last; a suffix whose first
    component is a glob (`**`, `*.md`) is left out, since it would cover any
    path at all."""
    parts = [x for x in p.split("/") if x]
    return ["/".join(parts[i:]) for i in range(1, len(parts))
            if "*" not in parts[i]]


def _covers_exact(path: str, entry: str) -> bool:
    if entry.endswith("/**"):
        base = entry[:-3]
    elif entry.endswith("/"):
        base = entry[:-1]
    elif "*" in entry:
        return fnmatch.fnmatchcase(path, entry)
    else:
        base = entry
    path = path.rstrip("/")
    return path == base or path.startswith(base + "/")


def covers(path: str, entry: str) -> bool:
    """True when `entry` (an Owns / Read first entry) covers `path`."""
    if _covers_exact(path, entry):
        return True
    if _is_abs(path) and not _is_abs(entry):
        return any(_covers_exact(s, entry) for s in _suffixes(path))
    if _is_abs(entry) and not _is_abs(path):
        return any(_covers_exact(path, s) for s in _suffixes(entry))
    return False


def _base(entry: str) -> str:
    if entry.endswith("/**"):
        return entry[:-3]
    return entry.rstrip("/")


def overlaps(a: str, b: str) -> bool:
    return covers(_base(a), b) or covers(_base(b), a)


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------

def rule1(card: Card) -> list[Problem]:
    allowed = [e for _, e in _entries(card.fields.get("Owns"))]
    allowed += [e for _, e in _entries(card.fields.get("Read first"))]
    out = []
    for name in ("Mission", "Done when"):
        f = card.fields.get(name)
        if f is None:
            continue
        # Join the field so a backticked span wrapped across lines is one span;
        # a problem is reported at the line where its span opens.
        joined = "\n".join(t for _, t in f.lines)
        for m in SPAN_RE.finditer(joined):
            n = f.line + joined[:m.start()].count("\n")
            for word in _split_words(m.group(1)):
                p = path_of(word)
                if p and not any(covers(p, e) for e in allowed):
                    out.append(Problem(n, card.sid, 1,
                                       f"`{p}` named in {name} is not under "
                                       "Owns or Read first"))
        if joined.count("`") % 2:
            n = f.line + joined[:joined.rindex("`")].count("\n")
            out.append(Problem(n, card.sid, 1,
                               f"unbalanced backtick in {name}; the text after "
                               "it is not checked for paths"))
    return out


def rule2(card: Card) -> list[Problem]:
    f = card.fields.get("Done when")
    if f is None:
        return []
    items = _items(f)
    if not items:
        return [Problem(f.line, card.sid, 2, "Done when holds no item")]
    return [Problem(n, card.sid, 2, "Done-when item has no backticked command "
                    "at its start")
            for n, text in items if not re.match(r"^`[^`]+`", text)]


def parse_after(card: Card) -> list[str]:
    f = card.fields.get("After")
    if f is None:
        return []
    text = " ".join(t.strip() for _, t in f.lines).strip()
    text = re.sub(r"(^|\s)-\s", " ", text).strip()
    if not text or _is_placeholder(text) or text.lower().rstrip(".") in NONE_VALUES:
        return []
    return [t for t in re.split(r"[,\s]+", text) if t]


def rule4_and_waves(cards: list[Card]) -> tuple[list[Problem], dict[str, int]]:
    out: list[Problem] = []
    by_id: dict[str, Card] = {}
    for c in cards:
        if c.sid in by_id:
            out.append(Problem(c.line, c.sid, 4,
                               f"stream id {c.sid} is defined twice (first at "
                               f"line {by_id[c.sid].line}); After cannot resolve it"))
        else:
            by_id[c.sid] = c
    deps: dict[str, list[str]] = {}
    bad: set[str] = set()
    for c in cards:
        if c.sid in deps:
            continue
        ds = parse_after(c)
        line = c.fields["After"].line if "After" in c.fields else c.line
        for d in ds:
            if d not in by_id:
                out.append(Problem(line, c.sid, 4,
                                   f"After names unknown stream `{d}`"))
                bad.add(c.sid)
        deps[c.sid] = [d for d in ds if d in by_id]

    state: dict[str, int] = {}   # 1 visiting, 2 done
    stack: list[str] = []

    def visit(s: str) -> None:
        state[s] = 1
        stack.append(s)
        for d in deps.get(s, []):
            if state.get(d) == 1:
                cyc = stack[stack.index(d):] + [d]
                bad.update(cyc)
                c = by_id[s]
                line = c.fields["After"].line if "After" in c.fields else c.line
                out.append(Problem(line, s, 4,
                                   "After has a cycle: " + " -> ".join(cyc)))
            elif d not in state:
                visit(d)
        stack.pop()
        state[s] = 2

    for c in cards:
        if c.sid not in state:
            visit(c.sid)

    # Anything depending (transitively) on a bad card has no wave either.
    waves: dict[str, int] = {}

    def wave(s: str, seen: frozenset) -> int | None:
        if s in bad or s in seen:
            return None
        if s in waves:
            return waves[s]
        w = 1
        for d in deps.get(s, []):
            dw = wave(d, seen | {s})
            if dw is None:
                return None
            w = max(w, dw + 1)
        waves[s] = w
        return w

    for c in cards:
        wave(c.sid, frozenset())
    return out, waves


def rule3(cards: list[Card], waves: dict[str, int]) -> list[Problem]:
    out = []
    owned = [(c, _entries(c.fields.get("Owns"))) for c in cards]
    for i, (a, ea) in enumerate(owned):
        if a.sid not in waves:
            continue
        for b, eb in owned[i + 1:]:
            if b.sid == a.sid or waves.get(b.sid) != waves[a.sid]:
                continue
            for nb, pb in eb:
                for _, pa in ea:
                    if overlaps(pa, pb):
                        out.append(Problem(nb, b.sid, 3,
                                           f"owns `{pb}`, which overlaps `{pa}` "
                                           f"owned by {a.sid} in the same wave "
                                           f"({waves[a.sid]})"))
    return out


def _is_ledger(entry: str, ledgers: tuple[str, ...] = DEFAULT_LEDGERS) -> bool:
    e = _base(entry)
    for suffix in BARE_LEDGERS + tuple(ledgers):
        if e == suffix or e.endswith("/" + suffix):
            return True
    if entry.endswith("/**") or entry.endswith("/") or "*" in entry:
        return any(covers(full, entry) for full in ledgers)
    return False


def rule5(card: Card, ledgers: tuple[str, ...] = DEFAULT_LEDGERS) -> list[Problem]:
    return [Problem(n, card.sid, 5,
                    f"Owns entry `{e}` is a shared ledger owned by the orchestrator")
            for n, e in _entries(card.fields.get("Owns")) if _is_ledger(e, ledgers)]


def ledgers_for(brief_path: Path) -> tuple[str, ...]:
    """The configured index and backlog of the tree holding `brief_path`.

    The tree is the checkout the brief sits in (its own config first, else its
    main checkout's: `config_at`); outside a repository, the brief's folder.
    """
    folder = brief_path.resolve().parent
    try:
        tree = repo_toplevel(folder)
    except RepoError:
        tree = folder
    cfg = config_at(tree)
    return (cfg.index_file, cfg.backlog_file)


def rule6(card: Card) -> list[Problem]:
    out = [Problem(card.line, card.sid, 6, f"missing field `{name}`")
           for name in FIELDS if name not in card.fields]
    out += [Problem(f.line, card.sid, 6, f"field `{f.name}` given twice")
            for f in card.duplicates]
    for f in card.fields.values():
        if not any(t.strip() for _, t in f.lines):
            out.append(Problem(f.line, card.sid, 6, f"field `{f.name}` is empty"))
    out += [Problem(n, card.sid, 6,
                    "unindented line is not a `- <Field>:` line; indent a "
                    "continuation, or it is not linted")
            for n in card.strays]
    for name in LIST_FIELDS:
        f = card.fields.get(name)
        if f is not None:
            out += [Problem(n, card.sid, 6,
                            f"{name} line is not a `- <path>` bullet; it is not "
                            "read as an entry")
                    for n in _non_bullets(f)]
    return out


def lint(text: str, ledgers: tuple[str, ...] = DEFAULT_LEDGERS
         ) -> tuple[list[Problem], int]:
    """(problems, card count). `ledgers` are the repo's index and backlog."""
    cards, near = parse_cards(text)
    if not cards:
        return near + [Problem(1, "-", 6,
                               "no task card (`### S-NN <slug>`) found")], 0
    problems: list[Problem] = list(near)
    for c in cards:
        problems += rule6(c) + rule1(c) + rule2(c) + rule5(c, ledgers)
    p4, waves = rule4_and_waves(cards)
    problems += p4 + rule3(cards, waves)
    problems.sort(key=lambda p: (p.line, p.rule))
    return problems, len(cards)


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(argv) != 2:
        print("usage: brief-lint.py <brief.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"brief-lint: cannot read {path}: {exc}", file=sys.stderr)
        return 2
    try:
        ledgers = ledgers_for(path)
    except ConfigError as exc:
        print(f"brief-lint: {exc}", file=sys.stderr)
        return 2
    problems, n_cards = lint(text, ledgers)
    if problems:
        for p in problems:
            print(f"{path}:{p.line}: {p.card}: rule {p.rule}: {p.message}")
        print(f"brief-lint: {len(problems)} problem(s) in {path}")
        return 1
    print(f"brief-lint: OK, {n_cards} card(s) in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
