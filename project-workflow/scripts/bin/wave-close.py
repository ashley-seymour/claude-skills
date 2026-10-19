r"""One-call wave-close verification.

Usage (run from inside the repo; the repo is found from the current directory):
    python <skill>/scripts/bin/wave-close.py <project-id> \
        --owns <path> --owns <path> ... \
        --return S-01:<payload.json> --return S-02:<payload.json>
    python <skill>/scripts/bin/wave-close.py <project-id>   # owns from the brief or STATUS.md
    python <skill>/scripts/bin/wave-close.py <project-id> --suites-only   # gates 3-4 only
    python <skill>/scripts/bin/wave-close.py --capture-pin   # re-pin from the main checkout

Replaces the hand-run wave-boundary sequence — return checks, the suites, a
`git status` triage against the `owns:` union, and the exit-code reading — with
one call, where the hand-run sequence took a dozen separate turns.

The suites and the pin are the repo's `.claude/project-workflow.json` keys
`suites` and `suite_pin` (`lib/config.py`), read from the project's tree when
it has the file, else from the main checkout. No `suites` configured: the
suite gate prints "no suites configured" and passes; it asserts nothing, so it
does not by itself make a PASS. `suite_pin` null: no floor, so only skips,
failures and errors are asserted.

`--suites-only` runs gates 3-4 alone against the project's tree (or `--root`),
for checking a freshly bootstrapped worktree. `--capture-pin` runs the suites
in the main checkout and writes the result to `suite_pin`; do it only on a
known-good tree, on the base branch after a merge.

Four gates, in cost order (cheapest refusal first):

1. **returns** — each `--return` payload, checked in-process (`check_return`):
   the required keys `status`, `updated_paths`, `found`, `summary_1line` (plus
   `blocked_reason` when blocked); unknown keys are accepted; a `summary_1line`
   over `SUMMARY_MAX` characters is truncated with a warning, never refused.
   Every `updated_paths` entry must sit inside the same owns union gate 2
   reads, absolute other-repo entries included (`return_outside_owns`).
2. **owns discipline** — every path `git status` reports dirty (untracked files
   listed one by one) must fall inside the owns union. The orchestrator's own
   ledgers in the project folder (`ledger_patterns`: STATUS.md,
   artifacts/follow-ups.md, reviewer and confirm JSONs, suite-expected.json,
   triage notes) are exempt here. Absolute entries in another git repo are
   gated in that repo (`cross_repo_owns`).
3. **suites** — every configured suite, concurrently (one subprocess each,
   the gate waits for all), exit codes read from the process rather than
   through a pipe. Before them, the test functions removed since the merge
   base are listed (`removed_tests`); the list never refuses.
4. **counts vs floor** — the gate that catches the silent-skip mode a green
   exit code hides. See `lib/suite_baseline.py`: passed must be at least the
   floor and skipped must be 0; additions never refuse. The floor is
   `--baseline <file>` when given, else the pin at `suite_pin` (in the main
   checkout) lowered by the retirements declared in the project's
   `suite-expected.json` beside its STATUS.md, if that file exists. Only
   `--capture-pin` writes the pin.

Every refusal line ends with the command or edit that resolves it.

`owns:` paths come from `--owns` (repeatable; relative to the tree, or
absolute); else from the task cards in the project's `brief.md` (the task-
card format: `### S-NN <slug>` cards with an `Owns:` list; `parse_cards`),
when the brief has any; else from the `owns:` lines of STATUS.md. An entry
ending in `/` with no glob is read as `<dir>/**`.

Card owns are scoped to one wave: a card's wave is its `After` depth (1 with no
dependency, else 1 + its deepest dependency's wave), N from `--wave`, else the
STATUS frontmatter `current_wave`.

Parseable form of a STATUS `owns:` line: comma-separated paths, each relative to the
project tree, or prefixed `<wt>/` (the tree) or `<proj>/` (the project folder,
STATUS.md's parent), or absolute; `{{name}}/` is the same root as `<name>/`;
`{a,b}` brace groups expand; `**` and `*` globs match; card `Owns` entries take
the same forms. `<cfg-wt>/` maps to the frontmatter `cfg_worktree:` when set.
Entries under any other placeholder (or `<cfg-wt>/` with no `cfg_worktree:`) or
an absolute path outside the tree belong to another repo: reported, not gated
here, except that an ABSOLUTE path in another git repo is gated in that repo:
its toplevel is found, `git status` runs once there, and dirty paths must fall
inside the absolute owns entries (repos checked are printed; an unresolvable
repo is reported as NOT CHECKED). Anything with a space in it (`its tests`, `deliverable only`) is prose:
reported and skipped. Skipping only makes the owns gate stricter, never looser.
Parsed `owns:` lines are scoped to one wave: the `## Wave <N>` section, N from
`--wave`, else the frontmatter `current_wave`.

For both sources, an explicit `--wave N` that selects nothing refuses (exit
2). For the frontmatter default that selects nothing, or with no
`current_wave`, every wave's entries are read (the union) and the note says so;
`--all-waves` asks for the union explicitly.

STATUS.md is resolved in the project WORKTREE when one exists — `main`'s copy is
the scaffold-time stub, frozen from the cut to close. With no worktree match the
gate verifies the main checkout and says so (close-phase.py refuses instead).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from config import (  # noqa: E402
    ConfigError,
    RepoError,
    config_at,
    config_for,
    load_config,
)
from config import main_checkout as _main_checkout  # noqa: E402
from suite_baseline import (  # noqa: E402
    EXPECTED_NAME,
    ExpectedBaselineError,
    capture_baseline,
    report,
    resolve_floor,
    run_all,
)
from streams import (  # noqa: E402
    WorktreeLookupError,
    find_project_tree,
    same_path,
)

# A Windows console is cp1252 and this module's own help text is not ASCII.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# The sibling script a missing payload is saved with, as a runnable path.
SAVE_RETURN = (Path(__file__).resolve().parent / "save-return.py").as_posix()

_OWNS_RE = re.compile(r"^owns:\s*(.+)$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"^<([A-Za-z][\w-]*)>[\\/]?(.*)$")
_DOUBLE_BRACE_ROOT_RE = re.compile(r"^\{\{([A-Za-z][\w-]*)\}\}(?:[\\/](.*))?$")
_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_FRONTMATTER_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_CURRENT_WAVE_RE = re.compile(r"^current_wave:[ \t]*['\"]?(\d+)['\"]?[ \t]*$",
                              re.MULTILINE)


def main_checkout() -> Path:
    """The main checkout of the repo the current directory is in."""
    return _main_checkout(Path.cwd())


def find_status(project_id: str, root: Path,
                projects_dir: str | None = None) -> Path | None:
    """The project's STATUS.md under `root`.

    `projects_dir` defaults to root's config, else its main checkout's.
    """
    if projects_dir is None:
        projects_dir = config_at(root).projects_dir
    hits = sorted(root.glob(
        f"{projects_dir}/*/project-{project_id}-*/STATUS.md"))
    return hits[0] if hits else None


# ---------- owns parsing -----------------------------------------------------

class OwnsParse:
    """What the `owns:` lines of a STATUS.md yield for this tree.

    A plain class, not a dataclass: this file is loaded by path, sometimes
    without a `sys.modules` entry, and `@dataclass` needs one.
    """

    def __init__(self) -> None:
        self.paths: list[str] = []        # gated, tree-relative
        self.foreign: list[str] = []      # another repo: gated there (cross_repo_owns)
        self.unparseable: list[str] = []  # prose: skipped
        self.scope: str = "every wave's `owns:` lines"
        self.wave: int | None = None      # set when one wave's section was read


def split_top_level(raw: str) -> list[str]:
    """Split on commas that are not inside a `{...}` group."""
    items: list[str] = []
    depth, current = 0, []
    for ch in raw:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            items.append("".join(current))
            current = []
        else:
            current.append(ch)
    items.append("".join(current))
    return items


def expand_braces(entry: str) -> list[str]:
    """`a/{b,c}.md` -> [`a/b.md`, `a/c.md`]; nested groups expand innermost first."""
    match = _BRACE_RE.search(entry)
    if match is None:
        return [entry]
    out: list[str] = []
    for option in match.group(1).split(","):
        out.extend(expand_braces(entry[:match.start()] + option.strip()
                                 + entry[match.end():]))
    return out


def _is_abs(path: str) -> bool:
    return bool(re.match(r"^[A-Za-z]:[\\/]", path)) or path.startswith(("/", "\\"))


_GITBASH_DRIVE_RE = re.compile(r"^/([A-Za-z])(?:/(.*))?$")
_CFG_WORKTREE_RE = re.compile(r"^cfg_worktree:[ \t]*['\"]?([^'\"\r\n]+?)['\"]?[ \t]*$",
                              re.MULTILINE)


def gitbash_drive(path: str) -> str:
    """Map a Git-Bash drive path (`/d/x/y`) to `D:/x/y`; other input unchanged.

    Windows only: on POSIX `/d/x` is an ordinary absolute path.
    """
    if os.name != "nt":
        return path
    match = _GITBASH_DRIVE_RE.match(path.replace("\\", "/"))
    if match is None:
        return path
    return f"{match.group(1).upper()}:/{match.group(2) or ''}"


def cfg_worktree(text: str) -> str | None:
    """`cfg_worktree:` from STATUS.md frontmatter, or None when absent/empty."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    found = _CFG_WORKTREE_RE.search(match.group(1))
    return found.group(1).strip() if found else None


def angle_root(token: str) -> str:
    """A leading `{{name}}/` (or a whole `{{name}}`) rewritten as `<name>/`.

    The two spellings name the same root (brief-lint's path rules), but
    `expand_braces` would read `{{wt}}/x` as the brace group `wt/x`. Any other
    token is returned unchanged.
    """
    match = _DOUBLE_BRACE_ROOT_RE.match(token)
    if match is None:
        return token
    name, rest = match.groups()
    return f"<{name}>" if rest is None else f"<{name}>/{rest}"


def map_tree_root(token: str, proj: str | None) -> str:
    """`token` with a leading `<wt>/` or `<proj>/` (either spelling) resolved.

    `<wt>/` is the tree, so it is dropped; `<proj>/` becomes `proj`, the project
    folder's tree-relative path, when one is known. Any other placeholder
    root comes back in its `<name>/` form for the caller to treat as another
    repo's. Whitespace and backticks around the token are stripped.
    """
    token = angle_root(token.strip().strip("`").strip())
    placeholder = _PLACEHOLDER_RE.match(token)
    if placeholder is None:
        return token
    name, rest = placeholder.groups()
    if name == "wt":
        return rest
    if name == "proj" and proj:
        return f"{proj}/{rest}" if rest else proj
    return token


def project_rel(project_file: Path | None, root: Path) -> str | None:
    """The project folder (the parent of its STATUS.md or brief.md) relative
    to `root`, or None when there is none or it lies outside `root`."""
    if project_file is None:
        return None
    return relative_to_tree(str(project_file.parent), root) or None


def expand_cfg_wt(entry: str, cfg: str | None) -> str:
    """Replace a leading `<cfg-wt>/` (or `{{cfg-wt}}/`) with the frontmatter
    path; else unchanged."""
    match = _PLACEHOLDER_RE.match(angle_root(entry.strip().strip("`").strip()))
    if match is None or match.group(1) != "cfg-wt" or not cfg:
        return entry
    base = cfg.replace("\\", "/").rstrip("/")
    return f"{base}/{match.group(2)}" if match.group(2) else base


def relative_to_tree(path: str, root: Path) -> str | None:
    """`path` relative to `root` (posix), or None when it lies outside it."""
    path = gitbash_drive(path)
    if not _is_abs(path):
        rel = path.replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        return rel
    full, base = os.path.normpath(path), os.path.normpath(str(root))
    if os.path.normcase(full) == os.path.normcase(base):
        return ""
    if os.path.normcase(full).startswith(os.path.normcase(base).rstrip("\\/") + os.sep):
        return full[len(base):].lstrip("\\/").replace("\\", "/")
    return None


def normalise_owns_arg(entry: str, proj: str | None = None) -> str:
    """A `--owns` value in the form both gates compare: `<wt>/` and `./` dropped.

    Gate 1 (returns) and gate 2 (`git status`) then see the same entry, so one
    `--owns` list cannot pass one gate and fail the other. `<proj>/` maps to
    `proj` when given (`map_tree_root`); `{{name}}/` reads as `<name>/`. Other
    placeholders (`<cfg-wt>/...`) are left in `<name>/` form: they cover nothing
    in this tree.
    """
    token = gitbash_drive(map_tree_root(entry, proj))
    token = token.replace("\\", "/") if not _is_abs(token) else token
    while token.startswith("./"):
        token = token[2:]
    return token


def as_dir_glob(entry: str, root: Path | None = None) -> str:
    """`<dir>/` with no glob -> `<dir>/**`; any other entry is returned as is.

    A trailing separator says "this directory", so it covers everything under
    it, including the files of a git repo nested there. With
    `root`, an entry naming `root` itself is left alone: the whole tree is not
    an allowlist (`classify_owns_entry`).
    """
    if not entry.endswith(("/", "\\")) or "*" in entry:
        return entry
    bare = entry.rstrip("/\\")
    if not bare or (root is not None and relative_to_tree(bare, root) == ""):
        return entry
    return bare + "/**"


def classify_owns_entry(item: str, root: Path, out: OwnsParse,
                        proj: str | None = None) -> None:
    """Sort one owns entry into `out.paths`, `out.foreign` or `out.unparseable`.

    `proj` is the project folder relative to `root` (`project_rel`): a leading
    `<proj>/` or `{{proj}}/` resolves under it, `<wt>/` or `{{wt}}/` under the
    tree (`map_tree_root`). Any other placeholder root is another repo's.
    """
    token = item.strip().strip("`").strip()
    if not token:
        return
    if re.search(r"\s", token):
        out.unparseable.append(token)
        return
    token = angle_root(token)
    placeholder = _PLACEHOLDER_RE.match(token)
    if placeholder:
        name = placeholder.group(1)
        if name != "wt" and not (name == "proj" and proj):
            out.foreign.append(token)
            return
        token = map_tree_root(token, proj)
    elif token.startswith("<"):
        out.unparseable.append(token)
        return
    for path in expand_braces(token):
        rel = relative_to_tree(path, root)
        if rel is None:
            out.foreign.append(as_dir_glob(path))
        elif rel:
            out.paths.append(as_dir_glob(rel))
        else:
            # The whole tree (`<wt>` alone, or the tree's own path) is not an
            # allowlist; treating it as one would pass every dirty path.
            out.unparseable.append(item.strip())


def current_wave(text: str) -> int | None:
    """`current_wave:` from STATUS.md frontmatter, or None when absent/non-integer."""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return None
    wave = _CURRENT_WAVE_RE.search(match.group(1))
    return int(wave.group(1)) if wave else None


def wave_section(text: str, wave: int) -> str | None:
    """The body of the `## Wave <wave>` section, or None when there is none.

    The heading is matched at a line start (`## Wave 3 — title  (status: done)`
    matches wave 3, never wave 30). The section ends at the next `## ` heading
    outside a ``` fence; `### S-NN` stream blocks stay inside it.
    """
    heading = re.compile(rf"^##[ \t]+Wave[ \t]+{wave}(?!\d)")
    start: int | None = None
    in_fence = False
    for line in re.finditer(r"^.*$", text, re.MULTILINE):
        raw = line.group(0)
        stripped = raw.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if start is None:
            if heading.match(raw):
                start = line.end() + 1
        elif stripped.startswith("## "):
            return text[start:line.start()]
    return None if start is None else text[start:]


def tree_of_status(status_path: Path) -> Path:
    """The tree holding `<projects_dir>/<state>/project-<id>-<slug>/STATUS.md`.

    `projects_dir` may have any number of segments, so the tree is the nearest
    ancestor whose configured `projects_dir` (`config_at`) is exactly the
    folder above `<state>/`. Raises ValueError when no ancestor fits.
    """
    projects_abs = status_path.parent.parent.parent
    for candidate in projects_abs.parents:
        if same_path(candidate / config_at(candidate).projects_dir, projects_abs):
            return candidate
    raise ValueError(
        f"cannot tell which tree holds {status_path}: no ancestor's configured "
        f"projects_dir is {projects_abs}; pass `root` explicitly")


def parse_owns(status_path: Path, root: Path | None = None,
               wave: int | None = None) -> OwnsParse:
    """Every gateable path on an `owns:` line, per the form in this module's doc.

    `root` defaults to the tree holding `status_path` (`tree_of_status`).
    `wave` scopes the parse to the `## Wave <wave>` section: the
    union over every wave let a stream write a path an earlier wave owned. With
    `wave` None, or no such section, every `owns:` line in the file is read, as
    before; `out.scope` says which happened so the caller can report it.
    """
    root = root if root is not None else tree_of_status(status_path)
    text = status_path.read_text(encoding="utf-8")
    out = OwnsParse()
    source = text
    if wave is None:
        out.scope = "every wave's `owns:` lines"
    else:
        section = wave_section(text, wave)
        if section is None:
            out.scope = (f"every wave's `owns:` lines (no `## Wave {wave}` "
                         f"section in {status_path.name})")
        else:
            source = section
            out.scope = f"`## Wave {wave}` only"
            out.wave = wave
    proj = project_rel(status_path, root)
    for raw in _OWNS_RE.findall(source):
        for item in split_top_level(raw.strip()):
            classify_owns_entry(item, root, out, proj)
    out.paths = sorted(set(out.paths))
    out.foreign = sorted(set(out.foreign))
    out.unparseable = sorted(set(out.unparseable))
    return out


# ---------- task cards (the brief card format) ---------------------------------
#
# The card format is defined in `brief-lint.py`'s module docstring; this is a
# reader for the two fields the gate needs (`Owns`, `After`), not an import of
# the lint, which is meant to move beside the skill on its own.

_CARD_RE = re.compile(r"^###\s+(S-(?:\d+[a-z]?|NN))(?=\s|$)")
_NEAR_CARD_RE = re.compile(r"^#{2,6}\s*(S-(?:\d+[a-z]?|NN))")
_CARD_HEADING_RE = re.compile(r"^#{1,3}\s")
_CARD_FIELD_RE = re.compile(
    r"^- (Agent|After|Mission|Owns|Read first|Decided|Done when|Stop and return):(.*)$")
_CARD_BULLET_RE = re.compile(r"^(\s+)- (.*)$")
_SPAN_RE = re.compile(r"`([^`]+)`")
_WHOLE_PLACEHOLDER_RE = re.compile(r"^(<[^<>]+>|\{\{[^{}]+\}\})$")
_NONE_VALUES = frozenset({"none", "-", "—", "–", "n/a"})


class Card:
    """One task card: its id, its `Owns` entries and its `After` ids."""

    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.fields: dict[str, list[str]] = {}   # name -> [inline, line, ...]


def _owns_words(item: str) -> list[str]:
    """The paths one `Owns` item names: its first word unless that opens a
    backticked span, plus every backticked span. Placeholders and `none` name
    nothing."""
    item = item.strip()
    if not item or _WHOLE_PLACEHOLDER_RE.match(item):
        return []
    words = [s.strip() for s in _SPAN_RE.findall(item)]
    first = item.split()[0]
    if not first.startswith("`"):
        words.insert(0, first)
    out: list[str] = []
    for word in words:
        w = word.strip().strip("'\"").replace("\\", "/")
        while w.startswith("./"):
            w = w[2:]
        if (w and not _WHOLE_PLACEHOLDER_RE.match(w)
                and w.lower().rstrip(".") not in _NONE_VALUES):
            out.append(w)
    return out


def card_owns(card: Card) -> list[str]:
    """`Owns` entries: the inline value plus every `-` bullet line, any depth."""
    lines = card.fields.get("Owns")
    if not lines:
        return []
    items = [lines[0]] if lines[0].strip() else []
    for text in lines[1:]:
        bullet = _CARD_BULLET_RE.match(text)
        if bullet:
            items.append(bullet.group(2))
    return [w for item in items for w in _owns_words(item)]


def card_after(card: Card) -> list[str]:
    """`After` ids; `none`, a dash or a placeholder means no dependency."""
    lines = card.fields.get("After")
    if not lines:
        return []
    text = " ".join(t.strip() for t in lines).strip()
    text = re.sub(r"(^|\s)-\s", " ", text).strip()
    if (not text or _WHOLE_PLACEHOLDER_RE.match(text)
            or text.lower().rstrip(".") in _NONE_VALUES):
        return []
    return [t for t in re.split(r"[,\s]+", text) if t]


def parse_cards(text: str) -> list[Card]:
    """The task cards of a brief, in order.

    A card is `### S-<digits>[letter] <slug>` (or `### S-NN <slug>`) and runs to
    the next heading of level 1-3, or a near-miss card heading. Its fields are
    unindented `- <Name>:` lines; a value runs on over indented and blank
    lines. Fenced blocks and `<!-- -->` comments are skipped, so a card quoted
    in a fence is not read.
    """
    text = re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"),
                  text, flags=re.DOTALL)
    cards: list[Card] = []
    card: Card | None = None
    current: str | None = None
    in_fence = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = _CARD_RE.match(line)
        if match:
            card, current = Card(match.group(1)), None
            cards.append(card)
            continue
        if _NEAR_CARD_RE.match(line) or _CARD_HEADING_RE.match(line):
            card = current = None
            continue
        if card is None:
            continue
        field = _CARD_FIELD_RE.match(line)
        if field:
            current = field.group(1)
            # A field given twice keeps its first value (brief-lint rule 6).
            if current in card.fields:
                current = None
            else:
                card.fields[current] = [field.group(2)]
            continue
        if current is not None and (not line or line[0].isspace()):
            card.fields[current].append(line)
            continue
        current = None
    return cards


def card_waves(cards: list[Card]) -> dict[str, int]:
    """Each card's wave: 1 with no `After`, else 1 + its deepest dependency's.

    The S-05 definition (brief-lint rule 3). A card on a cycle, with an unknown
    dependency, or sharing its id with another card gets no wave.
    """
    by_id: dict[str, Card] = {}
    dup: set[str] = set()
    for card in cards:
        if card.sid in by_id:
            dup.add(card.sid)
        by_id.setdefault(card.sid, card)
    waves: dict[str, int | None] = {}

    def wave(sid: str, seen: frozenset[str]) -> int | None:
        if sid in waves:
            return waves[sid]
        if sid in seen or sid not in by_id or sid in dup:
            return None
        depth = 1
        for dep in card_after(by_id[sid]):
            dep_wave = wave(dep, seen | {sid})
            if dep_wave is None:
                waves[sid] = None
                return None
            depth = max(depth, dep_wave + 1)
        waves[sid] = depth
        return depth

    for sid in by_id:
        wave(sid, frozenset())
    return {sid: w for sid, w in waves.items() if w is not None}


def real_cards(cards: list[Card]) -> list[Card]:
    """Cards with a real id: the template's `S-NN` placeholder is not one."""
    return [c for c in cards if c.sid != "S-NN"]


def parse_card_owns(brief_path: Path, cards: list[Card], root: Path,
                    wave: int | None) -> OwnsParse:
    """The owns union of the cards in `wave` (every card when `wave` is None).

    With no card in `wave`, every card is read and `out.scope` says so; the
    caller refuses when the wave was asked for explicitly.
    """
    out = OwnsParse()
    chosen = cards
    if wave is None:
        out.scope = f"every card in {brief_path.name}"
    else:
        waves = card_waves(cards)
        in_wave = [c for c in cards if waves.get(c.sid) == wave]
        if in_wave:
            chosen = in_wave
            out.scope = (f"wave {wave} cards in {brief_path.name} ("
                         + ", ".join(c.sid for c in in_wave) + ")")
            out.wave = wave
        else:
            out.scope = (f"every card in {brief_path.name} (no card has "
                         f"wave {wave} by its After depth)")
    proj = project_rel(brief_path, root)     # the brief sits beside STATUS.md
    for card in chosen:
        for entry in card_owns(card):
            classify_owns_entry(entry, root, out, proj)
    out.paths = sorted(set(out.paths))
    out.foreign = sorted(set(out.foreign))
    out.unparseable = sorted(set(out.unparseable))
    return out


# ---------- gate 1: stream returns ---------------------------------------------

#: The keys every stream return carries (the skill's return contract).
RETURN_REQUIRED = ("status", "updated_paths", "found", "summary_1line")
RETURN_STATUS = ("ok", "blocked", "failed")
#: Longest `summary_1line` kept whole; a longer one is truncated with a warning.
SUMMARY_MAX = 120


def check_return(payload: object, where: str) -> tuple[list[str], list[str]]:
    """The return contract: (problems, warnings). Unknown keys are accepted.

    `where` names the payload file in the remedy each problem ends with.
    """
    fix = f"Edit {where}"
    if not isinstance(payload, dict):
        return [f"the top level is not a JSON object. {fix} to hold one "
                f"object"], []
    problems: list[str] = []
    warnings: list[str] = []
    missing = [k for k in RETURN_REQUIRED if k not in payload]
    if missing:
        problems.append(f"missing key(s) {', '.join(missing)}. {fix} to add "
                        f"them (`found` is [] when nothing was found)")
    status = payload.get("status")
    if "status" in payload and status not in RETURN_STATUS:
        problems.append(f"status {status!r} is not one of "
                        f"{', '.join(RETURN_STATUS)}. {fix} to use one")
    if status == "blocked" and not str(payload.get("blocked_reason") or "").strip():
        problems.append(f"status is blocked with no blocked_reason. {fix} to "
                        f"add the stream's blocked_reason")
    paths = payload.get("updated_paths")
    if "updated_paths" in payload and (
            not isinstance(paths, list) or not all(isinstance(x, str) for x in paths)):
        problems.append(f"updated_paths is not a list of strings. {fix} to "
                        f"make it one")
    if "found" in payload and not isinstance(payload.get("found"), list):
        problems.append(f"found is not a list. {fix} to make it one ([] when "
                        f"nothing was found)")
    summary = payload.get("summary_1line")
    if "summary_1line" in payload:
        if not isinstance(summary, str):
            problems.append(f"summary_1line is not a string. {fix} to make it one")
        elif len(summary) > SUMMARY_MAX:
            warnings.append(
                f"summary_1line is {len(summary)} chars (> {SUMMARY_MAX}); "
                f"read as: {summary[:SUMMARY_MAX - 3]}...")
    return problems, warnings


def _abs_posix(path: str) -> str:
    """`path` as a normalised absolute posix string (Git-Bash drives mapped)."""
    return os.path.normpath(gitbash_drive(path)).replace("\\", "/")


def split_owns(owns: list[str], root: Path) -> tuple[list[str], list[str]]:
    """(tree-relative entries, absolute entries outside `root`).

    An absolute entry inside the tree is made relative; one naming the tree
    itself is dropped (the whole tree is not an allowlist).
    """
    local: list[str] = []
    absolute: list[str] = []
    for entry in owns:
        rel = relative_to_tree(entry, root)
        if rel is None:
            absolute.append(_abs_posix(entry))
        elif rel:
            local.append(rel)
    return local, absolute


def return_outside_owns(updated: list[str], owns: list[str],
                        root: Path) -> list[tuple[str, str]]:
    """`(path, why)` for each `updated_paths` entry no owns entry covers.

    `owns` is the union gate 2 reads: tree-relative entries and absolute
    entries in other repos alike. A relative path resolves against the tree
    and must stay in it (`why` = "escapes the tree"); an absolute path inside
    the tree is made relative; one outside it must sit under an absolute entry.
    A path ending in a separator names a directory: it is compared with the
    separator kept, so `src/**` or `src` covers `src/` but `src/a.py` does not.
    """
    local, absolute = split_owns(owns, root)
    out: list[tuple[str, str]] = []
    for path in updated:
        mapped = gitbash_drive(path)
        tail = "/" if mapped.endswith(("/", "\\")) else ""
        if _is_abs(mapped):
            rel = relative_to_tree(mapped, root)
            if rel is None:
                full = _abs_posix(mapped).rstrip("/") + tail
                if not any(covers(full, entry) for entry in absolute):
                    out.append((path, "outside"))
                continue
        else:
            rel = os.path.normpath(mapped).replace("\\", "/")
            if rel in (".", "..") or rel.startswith("../"):
                out.append((path, "escapes the tree"))
                continue
        rel = rel + tail if rel else rel
        if not rel or not any(covers(rel, entry) for entry in local):
            out.append((path, "outside"))
    return out


def check_returns(returns: list[str], root: Path, owns: list[str],
                  remedy) -> list[str]:
    """Gate 1 over every `<stream-id>:<payload.json>`; returns the problems.

    `remedy(stream_id, path)` is the edit that would put `path` in the owns.
    """
    if not owns:
        return ["returns: no owns paths to check `updated_paths` against. "
                "Pass `--owns <path>` per file, or give the brief task cards "
                "with `Owns:` lists"]
    problems: list[str] = []
    for spec in returns:
        if ":" not in spec:
            problems.append(f"--return {spec!r} is not <stream-id>:<payload.json>. "
                            f"Pass it as `--return <stream-id>:<payload.json>`")
            continue
        stream_id, payload = spec.split(":", 1)
        payload_path = Path(payload)
        try:
            # utf-8-sig: PowerShell 5.1 writes a BOM, which plain utf-8 keeps.
            data = json.loads(payload_path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            problems.append(f"{stream_id}: payload {payload_path} does not exist. "
                            f"Save it: `python {SAVE_RETURN} <agent-id> "
                            f"{payload_path}`")
            continue
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            problems.append(f"{stream_id}: payload {payload_path} is not "
                            f"readable JSON ({exc}). Re-save it: `python "
                            f"{SAVE_RETURN} <agent-id> {payload_path}`")
            continue
        own, warnings = check_return(data, str(payload_path))
        for warning in warnings:
            print(f"    WARN: {stream_id}: {warning}")
        if not own:
            for path, why in return_outside_owns(data["updated_paths"], owns, root):
                if why == "outside":
                    own.append(f"updated path {path!r} is in no owns entry. "
                               f"{remedy(stream_id, path)}")
                else:
                    own.append(f"updated path {path!r} {why}. Edit "
                               f"{payload_path} to name the path inside the tree")
        if own:
            problems.extend(f"{stream_id}: {p}" for p in own)
        else:
            print(f"    return ok: {stream_id}")
    return problems


# ---------- gate 2: owns discipline --------------------------------------------

def ledger_patterns(status_path: Path | None, root: Path) -> list[str]:
    """Tree-relative globs of the orchestrator's ledgers in the project folder.

    The orchestrator writes these between dispatch and gate, so they are
    dirty at every boundary and are in no stream's owns.
    """
    proj = project_rel(status_path, root)
    if not proj:
        return []
    return [f"{proj}/STATUS.md", f"{proj}/{EXPECTED_NAME}",
            f"{proj}/artifacts/follow-ups.md",
            f"{proj}/artifacts/*-review-w*.json",
            f"{proj}/artifacts/*-confirm-w*.json",
            f"{proj}/artifacts/triage-w*.md"]


def dirty_paths(root: Path) -> list[str]:
    """Every dirty path, untracked files one by one (not collapsed to a dir)."""
    proc = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True,
    )
    out: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) > 3:
            path = line[3:].strip()
            if " -> " in path:          # a rename: the new name is what was written
                path = path.split(" -> ", 1)[1]
            out.append(path.strip('"'))
    return out


def _glob_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$", re.IGNORECASE)


def covers(path: str, entry: str) -> bool:
    """`entry` (a file, a directory, or a `*`/`**` glob) covers `path`.

    Case-insensitive: the trees this gates are on Windows.
    """
    norm, own = path.replace("\\", "/"), entry.replace("\\", "/")
    if "*" in own:
        return bool(_glob_regex(own).match(norm))
    own = own.rstrip("/").lower()
    return norm.lower() == own or norm.lower().startswith(own + "/")


def outside_owns(dirty: list[str], owns: list[str],
                 root: Path | None = None) -> list[str]:
    """Dirty paths not covered by any `owns:` entry.

    With `root`, absolute entries are made tree-relative first; one outside the
    tree (another repo's file) can cover nothing here and is dropped.
    """
    local: list[str] = []
    for entry in owns:
        rel = relative_to_tree(entry, root) if root is not None else entry
        if rel:
            local.append(rel)
    return [p for p in dirty if not any(covers(p, o) for o in local)]


def repo_root_of(path: str) -> Path | None:
    """Toplevel of the git repo holding `path`, or None when there is none.

    `path` may not exist yet (a file a stream was told to create) and may be a
    glob: the search starts at the nearest existing directory above its literal
    prefix.
    """
    literal = re.split(r"[*?\[]", path.replace("\\", "/"), maxsplit=1)[0]
    probe = Path(literal)
    while not probe.is_dir():
        if probe.parent == probe:
            return None
        probe = probe.parent
    try:
        proc = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return None
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        return None
    return Path(top)


def cross_repo_owns(owns: list[str], root: Path) -> tuple[
        list[Path], list[str], list[tuple[Path, str]]]:
    """Gate 2 for owns entries in other repos.

    Every absolute entry outside `root` is mapped to its repo; `git status` runs
    once per repo, and each dirty path there must sit under one of the absolute
    owns entries, compared as absolute paths. So an entry naming the repo's own
    folder (`<repo>/`, read as `<repo>/**`) covers the whole repo, and a parent
    glob covers a repo nested under it. Returns (repos checked, entries whose
    repo could not be resolved, [(repo, escaped path)]).
    """
    by_repo: dict[str, Path] = {}
    unresolved: list[str] = []
    _, absolute = split_owns(owns, root)
    for entry in owns:
        if not _is_abs(entry) or relative_to_tree(entry, root) is not None:
            continue
        repo = repo_root_of(entry)
        if repo is None:
            unresolved.append(entry)
            continue
        if same_path(repo, root):
            continue
        by_repo.setdefault(os.path.normcase(os.path.normpath(str(repo))), repo)
    checked: list[Path] = []
    escaped: list[tuple[Path, str]] = []
    for repo in by_repo.values():
        checked.append(repo)
        for path in dirty_paths(repo):
            full = _abs_posix(os.path.join(str(repo), path))
            if not any(covers(full, entry) for entry in absolute):
                escaped.append((repo, path))
    return checked, unresolved, escaped


# ---------- removed tests (listed, never refused) -------------------------------

_TEST_DEF_RE = re.compile(r"^[ \t]*(?:async[ \t]+)?def[ \t]+(test\w*)[ \t]*\(",
                          re.MULTILINE)


def _git_text(root: Path, *args: str) -> str | None:
    """stdout of `git -C root <args>`, or None when git fails."""
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, encoding="utf-8",
                              errors="replace")
    except (FileNotFoundError, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def merge_base(root: Path, base_branch: str = "main") -> tuple[str | None, str]:
    """(merge-base of HEAD with `base_branch`, else `origin/<base_branch>`; the ref used)."""
    for ref in (base_branch, f"origin/{base_branch}"):
        out = _git_text(root, "merge-base", "HEAD", ref)
        if out and out.strip():
            return out.strip(), ref
    return None, ""


def _is_test_file(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_")
                                     or name.endswith("_test.py"))


def removed_tests(root: Path, base: str) -> list[tuple[str, list[str]]]:
    """Per test file changed since `base`, the `def test_` names gone from it.

    Static: names are read with a regex from `git show <base>:<file>` and from
    the file in the tree now (absent file: every name is gone). Parametrized
    cases and tests moved to another file are not told apart.
    """
    names = _git_text(root, "diff", "--name-only", "--no-renames", base) or ""
    out: list[tuple[str, list[str]]] = []
    for path in sorted({n.strip() for n in names.splitlines() if n.strip()}):
        if not _is_test_file(path):
            continue
        old = _git_text(root, "show", f"{base}:{path}")
        if old is None:
            continue                     # added since the merge base
        current = root / path
        try:
            new = (current.read_text(encoding="utf-8", errors="replace")
                   if current.is_file() else "")
        except OSError:
            new = ""
        gone = sorted(set(_TEST_DEF_RE.findall(old))
                      - set(_TEST_DEF_RE.findall(new)))
        if gone:
            out.append((path, gone))
    return out


def print_removed_tests(root: Path, base_branch: str = "main") -> None:
    base, ref = merge_base(root, base_branch)
    if base is None:
        print(f"\n[removed tests] no merge base with {base_branch} or "
              f"origin/{base_branch} — not listed")
        return
    print(f"\n[removed tests] since the merge base {base[:8]} with {ref} "
          f"(listed only, never refused)")
    found = removed_tests(root, base)
    if not found:
        print("    none")
    for path, gone in found:
        print(f"    {path}: {', '.join(gone)}")


# ---------- driver ---------------------------------------------------------------

def _resolve_owns(args, root: Path, status_path: Path | None):
    """(owns, foreign_abs, unchecked placeholders, note, source, exit code).

    `source` is "--owns", ("card", brief path), ("status", STATUS.md path) or
    None. A non-None exit code means refuse with it.
    """
    cfg = None
    if status_path is not None:
        cfg = cfg_worktree(status_path.read_text(encoding="utf-8"))
    proj = project_rel(status_path, root)
    unchecked_ph: list[str] = []  # `<name>/...` entries with no value to map them to
    owns: list[str] = []
    for arg in args.owns:
        norm = normalise_owns_arg(expand_cfg_wt(arg, cfg), proj)
        if _PLACEHOLDER_RE.match(norm):
            unchecked_ph.append(norm)
            continue
        owns.extend(as_dir_glob(o, root) for o in expand_braces(norm) if o)
    if owns:
        return (owns, [], unchecked_ph, f"{len(owns)} path(s) from --owns",
                "--owns", None)
    if status_path is None:
        return [], [], unchecked_ph, "", None, None

    if args.all_waves:
        wave = None
    elif args.wave is not None:
        wave = args.wave
    else:
        wave = current_wave(status_path.read_text(encoding="utf-8"))

    brief_path = status_path.parent / "brief.md"
    cards = (real_cards(parse_cards(brief_path.read_text(encoding="utf-8")))
             if brief_path.is_file() else [])
    if cards:
        parsed = parse_card_owns(brief_path, cards, root, wave)
        source = ("card", brief_path)
        missing = (f"no card in {brief_path} has wave {args.wave} by its "
                   f"After depth. Add the wave's cards")
    else:
        parsed = parse_owns(status_path, root, wave=wave)
        source = ("status", status_path)
        missing = (f"no `## Wave {args.wave}` section in {status_path}. Add "
                   f"the section")
    if args.wave is not None and parsed.wave is None:
        # An explicit --wave is a request for that wave's owns only;
        # widening it to the every-wave union would loosen the gate the
        # operator just asked to narrow. Only the frontmatter default
        # falls back (with its note).
        print(f"REFUSE: --wave {args.wave}: {missing}, pick another wave, "
              f"pass --all-waves, or pass --owns per file.", file=sys.stderr)
        return [], [], unchecked_ph, "", source, 2

    foreign_abs: list[str] = []
    for f in parsed.foreign:
        for path in expand_braces(gitbash_drive(expand_cfg_wt(f, cfg))):
            if _is_abs(path):
                foreign_abs.append(as_dir_glob(path))
            elif _PLACEHOLDER_RE.match(path):
                unchecked_ph.append(path)
    name = source[1].name
    note = f"parsed {len(parsed.paths)} path(s) from {name}, {parsed.scope}"
    if parsed.foreign:
        note += (f"; {len(parsed.foreign)} in another repo, gated there when "
                 f"it resolves, else NOT CHECKED below "
                 f"({', '.join(parsed.foreign[:3])}"
                 f"{' ...' if len(parsed.foreign) > 3 else ''})")
    if parsed.unparseable:
        note += (f"; {len(parsed.unparseable)} unparseable, skipped "
                 f"({', '.join(repr(u) for u in parsed.unparseable[:3])}"
                 f"{' ...' if len(parsed.unparseable) > 3 else ''})")
    return parsed.paths, foreign_abs, unchecked_ph, note, source, None


def _remedy(source, stream_id: str | None, path: str) -> str:
    """The edit that puts `path` in the owns, by where the owns came from."""
    if isinstance(source, tuple) and source[0] == "card":
        card = f"card {stream_id}" if stream_id else "the stream's card"
        return (f"Add `{path}` to the `Owns:` list of {card} in {source[1]}, "
                f"or pass `--owns {path}`")
    if isinstance(source, tuple) and source[0] == "status":
        return (f"Add `{path}` to the wave's `owns:` line in {source[1]}, or "
                f"pass `--owns {path}`")
    return f"Add `--owns {path}` to this call"


def suite_gate(cfg, main_repo: Path, root: Path, status_path: Path | None,
               override: Path | None) -> tuple[list[str], bool]:
    """Gates 3-4: run the configured suites in `root`, compare to the floor.

    Returns (problems, asserted). `asserted` is False only when no suites are
    configured: the gate then passes having examined nothing.
    """
    if not cfg.suites:
        print("    no suites configured (`suites` in .claude/project-workflow.json)"
              " — suite gate passes, nothing asserted")
        return [], False
    problems: list[str] = []
    expected_path = (status_path.parent / EXPECTED_NAME
                     if status_path is not None else None)
    pin_path = main_repo / cfg.suite_pin if cfg.suite_pin else None
    baseline = None
    if override is not None and not override.is_file():
        problems.append(f"--baseline {override} does not exist. Pass "
                        f"an existing file to --baseline, or drop it")
    elif override is None and pin_path is None:
        print("  floor: none (`suite_pin` is null) — only skips, failures and "
              "errors are asserted")
    elif override is None and not pin_path.is_file():
        problems.append(
            f"no pin at {pin_path} (`suite_pin`) — the count gate could not "
            f"run. Capture one on a known-good main checkout with "
            f"`wave-close.py --capture-pin`")
    else:
        try:
            baseline, label = resolve_floor(pin_path, expected_path, override)
            print(f"  floor: {label}")
        except ExpectedBaselineError as exc:
            problems.append(f"{EXPECTED_NAME} refused: {exc}")
        except (OSError, ValueError, TypeError) as exc:
            problems.append(f"cannot read the floor: {exc}. Fix the "
                            f"baseline file, or pass --baseline <file>")
    actual = run_all(root, cfg.suites)
    problems.extend(report(baseline or {}, actual))
    return problems, True


def capture_pin(cfg, main_repo: Path) -> int:
    """Run the suites in the main checkout and write the result to `suite_pin`."""
    if not cfg.suite_pin:
        print("REFUSE: --capture-pin needs `suite_pin` in "
              ".claude/project-workflow.json; it is null", file=sys.stderr)
        return 2
    if not cfg.suites:
        print("REFUSE: --capture-pin needs `suites` in "
              ".claude/project-workflow.json; none are configured", file=sys.stderr)
        return 2
    pin_path = main_repo / cfg.suite_pin
    print(f"capturing the pin from {main_repo} — running "
          f"{len(cfg.suites)} suite(s)...")
    doc = capture_baseline(main_repo, cfg.suites)
    pin_path.parent.mkdir(parents=True, exist_ok=True)
    pin_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {pin_path}")
    for name, payload in doc["suites"].items():
        flag = "  <-- DEGRADED, asserts nothing" if payload["passed"] == 0 else ""
        print(f"  {name:<12} {payload['passed']} passed, "
              f"{payload['failed']} failed, {payload['skipped']} skipped "
              f"(exit {payload['exit_code']}){flag}")
    return 0


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="wave-close.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("project_id", nargs="?",
                   help="the project to verify (required unless --capture-pin)")
    p.add_argument("--return", dest="returns", action="append", default=[],
                   metavar="STREAM:PAYLOAD",
                   help="stream return JSON to check; repeatable")
    p.add_argument("--owns", action="append", default=[],
                   help="a path the wave may write; repeatable, one per file (or "
                        "dir/**). Overrides the owns read from the brief's task "
                        "cards or STATUS.md")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--wave", type=int, default=None, metavar="N",
                       help="with no --owns: read the owns of wave N only (the "
                            "cards whose After depth is N, else the `## Wave N` "
                            "section of STATUS.md); refuses (exit 2) when that "
                            "selects nothing (default: the frontmatter "
                            "`current_wave`, which falls back to every wave "
                            "with a note)")
    scope.add_argument("--all-waves", action="store_true",
                       help="with no --owns: read every wave's owns")
    p.add_argument("--root", type=Path, default=None,
                   help="the tree to verify (default: the project worktree, "
                        "else the main checkout)")
    p.add_argument("--baseline", type=Path, default=None,
                   help="a baseline file used as the floor; overrides the "
                        "shared pin and the project's declared retirements")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--skip-suites", action="store_true",
                      help="run gates 1-2 only: the boundary check of fix and "
                           "sweep returns after the wave's one suite run; "
                           "never in place of that run.")
    mode.add_argument("--suites-only", action="store_true",
                      help="run gates 3-4 only (the configured suites against "
                           "the floor), e.g. on a freshly bootstrapped worktree")
    mode.add_argument("--capture-pin", action="store_true",
                      help="run the configured suites in the MAIN checkout and "
                           "write `suite_pin`. Only on a known-good tree.")
    args = p.parse_args(argv[1:])

    try:
        main_repo = main_checkout()
    except RepoError as exc:
        print(f"REFUSE: {exc}. Run from inside the repo.", file=sys.stderr)
        return 2

    if args.capture_pin:
        if args.project_id or args.returns or args.owns or args.root or args.baseline:
            p.error("--capture-pin takes no project id, --return, --owns, "
                    "--root or --baseline")
        try:
            return capture_pin(load_config(main_repo), main_repo)
        except ConfigError as exc:
            print(f"REFUSE: {exc}", file=sys.stderr)
            return 2
    if args.project_id is None:
        p.error("a project id is required unless --capture-pin is given")
    if args.suites_only and (args.returns or args.owns):
        p.error("--suites-only runs gates 3-4 only; drop --return and --owns")

    print(f"wave-close: project {args.project_id}")
    if args.root:
        root = args.root.resolve()
        print("  tree source: --root")
    else:
        # The lookup is shared with close-phase.py (`streams.find_project_tree`).
        try:
            found = find_project_tree(args.project_id, main_repo)
        except WorktreeLookupError as exc:
            print(f"REFUSE: cannot resolve the tree for project "
                  f"{args.project_id} — {exc} Pass --root <tree> explicitly.",
                  file=sys.stderr)
            return 2
        if found is None:
            root = main_repo
            print(f"  tree source: no separate worktree for project "
                  f"{args.project_id} — verifying the main checkout")
        elif same_path(found, main_repo):
            root = main_repo
            print(f"  tree source: main checkout is on the project "
                  f"{args.project_id} branch")
        else:
            root = found
            print("  tree source: project worktree")
    print(f"  tree: {root}")
    try:
        cfg = config_for(main_repo, root)
    except ConfigError as exc:
        print(f"REFUSE: {exc}", file=sys.stderr)
        return 2

    problems: list[str] = []
    # Which gates actually examined something. An empty `problems` means only
    # "nothing was found wrong", which is not the same claim as "the wave was
    # checked" -- every gate here no-ops silently when its input is absent, so
    # the two were indistinguishable at the verdict line.
    asserted: list[str] = []
    inert: list[str] = []

    status_path = find_status(args.project_id, root, cfg.projects_dir)

    if args.suites_only:
        print("\n[3/4] suites (concurrent) + [4/4] counts vs floor  (--suites-only)")
        suite_problems, ran = suite_gate(cfg, main_repo, root, status_path,
                                         args.baseline)
        print()
        if suite_problems:
            print(f"REFUSE: {len(suite_problems)} problem(s) against the floor:",
                  file=sys.stderr)
            for problem in suite_problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        if ran:
            print("PASS: passed count at or above the floor, and no skips, for "
                  "every suite")
        else:
            print("PASS: no suites configured — nothing to run")
        return 0

    # owns is resolved first: gate 1 checks `updated_paths` against it.
    owns, foreign_abs, unchecked_ph, owns_note, source, code = _resolve_owns(
        args, root, status_path)
    if code is not None:
        return code
    union = owns + foreign_abs

    def remedy(stream_id: str | None, path: str) -> str:
        return _remedy(source, stream_id, path)

    print("\n[1/4] returns")
    if args.returns:
        problems.extend(check_returns(args.returns, root, union, remedy))
        asserted.append("returns")
    else:
        print("    no --return given — skipped (pass them to gate the returns)")
        inert.append("returns (no --return given)")

    print("\n[2/4] owns discipline")
    if owns_note:
        print(f"    owns: {owns_note}")
    if not union:
        print("    no `owns:` paths found — nothing asserted. Pass --owns to gate this.")
        inert.append("owns discipline (0 `owns:` paths parsed)")
    else:
        asserted.append("owns discipline")
        ledgers = ledger_patterns(status_path, root)
        dirty = dirty_paths(root)
        exempt = [d for d in dirty if any(covers(d, g) for g in ledgers)]
        if exempt:
            print(f"    exempt (orchestrator ledgers): {', '.join(exempt)}")
        dirty = [d for d in dirty if d not in exempt]
        escaped = outside_owns(dirty, owns, root)
        if escaped:
            shown = escaped[:8]
            problems.append(
                f"{len(escaped)} dirty path(s) outside the owns union: "
                + ", ".join(shown) + (" ..." if len(escaped) > 8 else "")
                + ". Revert each stray write, or for an intended one: "
                + remedy(None, shown[0]))
            for path in shown:
                print(f"    FAIL: outside owns: {path}")
        else:
            print("    ok: no dirty path outside the owns union")
        checked, unresolved, cross_escaped = cross_repo_owns(union, root)
        if checked:
            print("    other repos checked: "
                  + ", ".join(str(r) for r in checked))
        for entry in unchecked_ph:
            print(f"    NOT CHECKED: {entry} has a placeholder root with no "
                  f"value here (`<cfg-wt>` needs `cfg_worktree:` in the "
                  f"frontmatter) — its dirty paths were not examined")
        for entry in unresolved:
            print(f"    NOT CHECKED: no git repo found for owns entry {entry} "
                  f"— its dirty paths were not examined")
        if cross_escaped:
            first = _abs_posix(os.path.join(str(cross_escaped[0][0]),
                                            cross_escaped[0][1]))
            problems.append(
                f"{len(cross_escaped)} dirty path(s) outside the owns union in "
                "another repo: "
                + ", ".join(f"{r}: {pth}" for r, pth in cross_escaped[:8])
                + (" ..." if len(cross_escaped) > 8 else "")
                + ". Revert each stray write, or for an intended one: "
                + remedy(None, first))
            for r, pth in cross_escaped[:8]:
                print(f"    FAIL: outside owns in {r}: {pth}")
        elif checked:
            print("    ok: no dirty path outside the owns union in those repos")

    print_removed_tests(root, cfg.base_branch)

    if args.skip_suites:
        print("\n[3/4] suites           SKIPPED (--skip-suites)")
        print("[4/4] counts           SKIPPED (--skip-suites)")
        inert.append("suites + counts (--skip-suites)")
    else:
        print("\n[3/4] suites (concurrent) + [4/4] counts vs floor")
        suite_problems, ran = suite_gate(cfg, main_repo, root, status_path,
                                         args.baseline)
        problems.extend(suite_problems)
        if ran:
            asserted.append("suites + counts")
        else:
            inert.append("suites + counts (no suites configured)")

    print()
    if problems:
        print(f"REFUSE: wave {args.project_id} is not closeable — "
              f"{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    if not asserted:
        # Every gate no-opped, so "closeable" would rest on nothing. Refusing is
        # the conservative reading: the operator asked a question this run could
        # not answer, and a green line here is worse than no line at all.
        print(f"INCONCLUSIVE: wave {args.project_id} was NOT verified — "
              f"no gate asserted anything:", file=sys.stderr)
        for name in inert:
            print(f"  - {name}", file=sys.stderr)
        print("  This is not a pass. Give the wave `owns:` paths, pass --return "
              "payloads, or drop --skip-suites.", file=sys.stderr)
        return 1
    print(f"PASS: wave {args.project_id} is closeable "
          f"({len(asserted)}/{len(asserted) + len(inert)} gates asserted: "
          f"{', '.join(asserted)}).")
    for name in inert:
        print(f"  note: {name} asserted nothing — not included in this pass.")
    print("Next: commit the wave, then `close-phase.py` to write the handoff.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
