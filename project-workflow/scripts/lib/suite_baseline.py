"""Run pytest suites and compare them against a recorded PASSED-COUNT floor.

Used by `wave-close.py` (the wave gate, `--suites-only` and `--capture-pin`);
every compare goes through `report` against the floor `resolve_floor` builds.
The suites themselves are the repo's config `suites` (`config.Suite`: a name
and a pytest argv); the pin is its `suite_pin`.

**A green exit code is not the signal.** A test guarded by
`pytest.mark.skipif` on a gitignored input (a data file the worktree bootstrap
populates) reports success in a tree that lacks the input, while silently
skipping the very tests that would have caught the gap. The signal that
survives that failure mode is the **passed count**, so that is what this
module asserts, together with **zero skips**.

The compare. The pin is a floor: passed must be at least the pin's figure,
skipped must be 0, and any failure or error refuses. Additions never refuse.
A project's `suite-expected.json` (`EXPECTED_NAME`, beside its STATUS.md), if
present, is read only for its declared retirements (`load_retirements`:
adjustment rows with a negative `passed`), which lower the floor; every other
field in it is ignored. Only `capture_baseline` builds a pin document, and
only `wave-close.py --capture-pin` writes one, on the base branch from a
green run after the merge.

Pure stdlib, and it deliberately does NOT import `streams`: a verification gate
must not depend on the project registry.

`subprocess.run(capture_output=True)` returns pytest's OWN exit status. That is the
point: reading an exit code through a shell pipe yields the pipe's status, not
pytest's, which is how a red suite has been reported as green before. `run_all`
starts one such subprocess per suite at once and waits for all of them, so the
gate's wall-clock is the slowest suite rather than the sum.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from config import Suite, with_interpreter

_COUNT_RE = re.compile(
    r"(\d+)\s+(passed|failed|skipped|xfailed|xpassed|errors?|deselected)\b"
)
_SKIP_RE = re.compile(r"^SKIPPED\s+\[(\d+)\]\s+(.+)$", re.MULTILINE)

# pytest exit codes worth naming rather than printing as a bare integer.
EXIT_MEANING = {
    0: "all collected tests passed",
    1: "tests were collected and some failed",
    2: "interrupted",
    3: "internal error",
    4: "usage error — pytest refused to run (often a conftest gate)",
    5: "no tests were collected",
}


@dataclass
class SuiteResult:
    """One suite's outcome, reduced to the numbers a gate can compare."""

    suite: str
    exit_code: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    errors: int = 0
    deselected: int = 0
    ran_nothing: bool = False
    skip_reasons: list[str] = field(default_factory=list)
    # The command that produced this result, quoted in remedies; empty for a
    # result read from a pin.
    command: str = ""

    @property
    def rerun(self) -> str:
        """How a remedy tells the reader to rerun this suite."""
        return f"`{self.command}`" if self.command else f"the `{self.suite}` suite"

    @property
    def degraded(self) -> bool:
        """True when this result cannot protect anything.

        A baseline of zero passing tests asserts nothing: every later run
        clears `passed >= 0`. Callers must say so out loud rather than
        presenting a vacuous check as a green one.
        """
        return self.ran_nothing or self.passed == 0

    @property
    def collected(self) -> int:
        """Every outcome pytest counted: the figure a silent de-collection moves.

        `deselected` is excluded: it is a selection choice, not a test outcome.
        """
        return (self.passed + self.failed + self.skipped + self.xfailed
                + self.xpassed + self.errors)

    def headline(self) -> str:
        bits = [f"{self.passed} passed"]
        for label, value in (("failed", self.failed), ("errors", self.errors),
                             ("skipped", self.skipped), ("xfailed", self.xfailed),
                             ("deselected", self.deselected)):
            if value:
                bits.append(f"{value} {label}")
        if self.ran_nothing:
            bits.append("NO TESTS RAN")
        meaning = EXIT_MEANING.get(self.exit_code, "unknown exit code")
        return f"{self.suite:<12} {', '.join(bits)}  (exit {self.exit_code}: {meaning})"


def parse_pytest_output(suite: str, exit_code: int, output: str) -> SuiteResult:
    """Reduce a pytest run to a `SuiteResult`.

    Reads the *last* line carrying count tokens, because pytest prints the
    short test summary (`-rs`) above the final counts line and warning blocks
    can carry digits of their own.
    """
    result = SuiteResult(suite=suite, exit_code=exit_code)

    summary_line = ""
    for line in output.splitlines():
        stripped = line.strip().strip("=").strip()
        if not stripped:
            continue
        if "no tests ran" in stripped:
            summary_line = stripped
            result.ran_nothing = True
            continue
        if _COUNT_RE.search(stripped) and (" in " in stripped or stripped.endswith("s")):
            summary_line = stripped
            result.ran_nothing = False

    for value, label in _COUNT_RE.findall(summary_line):
        count = int(value)
        if label == "passed":
            result.passed = count
        elif label == "failed":
            result.failed = count
        elif label == "skipped":
            result.skipped = count
        elif label == "xfailed":
            result.xfailed = count
        elif label == "xpassed":
            result.xpassed = count
        elif label.startswith("error"):
            result.errors = count
        elif label == "deselected":
            result.deselected = count

    # Exit 4/5 mean pytest refused to run or collected nothing. Either way no
    # assertion about this tree's health is available from this run.
    if exit_code in (4, 5) or (result.passed == 0 and result.failed == 0):
        result.ran_nothing = True

    result.skip_reasons = [
        f"[{count}] {reason.strip()}" for count, reason in _SKIP_RE.findall(output)
    ]
    return result


def run_suite(suite: Suite, cwd: Path) -> SuiteResult:
    """Run one suite's argv in `cwd` and return its parsed result.

    The argv is the config's, run as given (a leading `python` is this
    interpreter). Give pytest `-rs`: the skip *reasons* are what distinguish
    "this tree is missing a gitignored input" from "this test is legitimately
    conditional", and a passed-count shortfall is unreadable without them.
    """
    cmd = with_interpreter(suite.argv)
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    result = parse_pytest_output(suite.name, proc.returncode,
                                 (proc.stdout or "") + (proc.stderr or ""))
    result.command = " ".join(suite.argv)
    return result


def run_all(cwd: Path, suites: Sequence[Suite]) -> dict[str, SuiteResult]:
    """Run every suite concurrently, one pytest subprocess each, and wait for all.

    One thread per suite drives its own `run_suite` (each thread drains its own
    subprocess's pipes, so neither run can stall on a full pipe while the other
    is read). Results come back keyed by suite name, ordered as `suites` is.
    """
    if not suites:
        return {}
    with ThreadPoolExecutor(max_workers=len(suites)) as pool:
        futures = {s.name: pool.submit(run_suite, s, cwd) for s in suites}
        return {s.name: futures[s.name].result() for s in suites}


def capture_baseline(cwd: Path, suites: Sequence[Suite]) -> dict:
    """Run every suite and package the numbers as a baseline document."""
    results = run_all(cwd, suites)
    try:
        commit = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        commit = "unknown"
    return {
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "captured_from": str(cwd),
        "commit": commit,
        "suites": {name: asdict(r) for name, r in results.items()},
    }


def load_baseline(path: Path) -> dict[str, SuiteResult]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, SuiteResult] = {}
    for name, payload in (data.get("suites") or {}).items():
        payload = dict(payload)
        payload.pop("suite", None)
        out[name] = SuiteResult(suite=name, **payload)
    return out


def compare(floor: SuiteResult, actual: SuiteResult) -> list[str]:
    """Return one string per refusal. Empty list means the suite held.

    `floor.passed` is the minimum passed count (the pin, lowered by declared
    retirements). More passed tests than the floor never refuse. Skipped must
    be 0 whatever the floor says: a skip is the silent-skip failure mode. Any
    failure or error refuses, since a suite can hold its passed count while
    turning a pass into an error. Each message ends with the command or edit
    that resolves it.
    """
    problems: list[str] = []
    suite = actual.suite

    if actual.passed < floor.passed:
        delta = floor.passed - actual.passed
        msg = (
            f"{suite}: PASSED-COUNT REGRESSION — {actual.passed} passed "
            f"against a floor of {floor.passed} ({delta} fewer)"
        )
        if actual.skipped > 0:
            msg += (
                f"; {actual.skipped} skip(s) account for "
                f"{'all' if actual.skipped >= delta else 'some'} of it"
            )
        msg += (
            f". Restore the lost test(s) and rerun {actual.rerun}, "
            f"or, if the retirement is on purpose, append "
            f'{{"suite": "{suite}", "passed": -{delta}, "by": "<stream>", '
            f'"why": "<reason>"}} to `adjustments` in the project\'s '
            f"{EXPECTED_NAME}"
        )
        problems.append(msg)

    if actual.skipped > 0:
        problems.append(
            f"{suite}: {actual.skipped} skipped; skipped must be 0. Restore what "
            f"the skip reasons name (gitignored worktree inputs: rerun the "
            f"repo's `bootstrap` command from .claude/project-workflow.json), "
            f"then rerun {actual.rerun}"
        )
    if actual.failed:
        problems.append(
            f"{suite}: {actual.failed} failed; a gate does not pass red. Fix the "
            f"failing test(s), listed by {actual.rerun}"
        )
    if actual.errors:
        problems.append(
            f"{suite}: {actual.errors} errors; a gate does not pass red. Fix the "
            f"erroring test(s), listed by {actual.rerun}"
        )
    if actual.ran_nothing and not floor.ran_nothing:
        problems.append(
            f"{suite}: NO TESTS RAN (exit {actual.exit_code}: "
            f"{EXIT_MEANING.get(actual.exit_code, '?')}) but the floor is "
            f"{floor.passed}. To see why, run {actual.rerun} in the tree"
        )
    return problems


# ---------- project-declared retirements --------------------------------------

#: File name of a project's suite ledger, beside its STATUS.md.
EXPECTED_NAME = "suite-expected.json"


class ExpectedBaselineError(ValueError):
    """The project's suite-expected.json cannot be read for retirements."""


def load_retirements(path: Path) -> dict[str, int]:
    """Read a project's `suite-expected.json` for its declared retirements only.

    Returns ``{suite: total}`` where `total` is the (negative) sum of every
    `adjustments` row whose `passed` is below zero. Rows with a zero or
    positive `passed` are additions; additions never refuse, so they are not
    read. `pin`, `expected` and `derivation` are ignored. Shape of a row::

        {"suite": "tests/", "passed": -77, "by": "S-07", "why": "..."}

    Raises ExpectedBaselineError when the file is not JSON, is not an object,
    `adjustments` is not a list, or a retirement row lacks a `suite`, an
    integer `passed` or a `why`. Each message ends with the edit that fixes it.
    """
    fix = f"Fix or delete {path}"
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpectedBaselineError(f"cannot read {path}: {exc}. {fix}") from exc
    if not isinstance(data, dict):
        raise ExpectedBaselineError(f"{path}: top level is not an object. {fix}")
    rows = data.get("adjustments") or []
    if not isinstance(rows, list):
        raise ExpectedBaselineError(
            f"{path}: `adjustments` is not a list. {fix}")
    out: dict[str, int] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ExpectedBaselineError(
                f"{path}: adjustment {i} is not an object: {row!r}. {fix}")
        passed = row.get("passed")
        if isinstance(passed, bool) or not isinstance(passed, int):
            raise ExpectedBaselineError(
                f"{path}: adjustment {i} needs an integer `passed`: {row!r}. "
                f"{fix}")
        if passed >= 0:
            continue
        suite = row.get("suite")
        if not isinstance(suite, str) or not suite:
            raise ExpectedBaselineError(
                f"{path}: retirement {i} names no suite: {row!r}. Add "
                f'`"suite": "<name>"` (a suite name from the config) to it')
        if not str(row.get("why", "")).strip():
            raise ExpectedBaselineError(
                f"{path}: retirement {i} has no `why`: {row!r}. Add a "
                f'`"why": "<reason>"` to it')
        out[suite] = out.get(suite, 0) + passed
    return out


def lower_floor(pin: dict[str, SuiteResult],
                retired: dict[str, int]) -> dict[str, SuiteResult]:
    """The pin with each suite's passed figure lowered by its retirements.

    A floor never goes below 0. A retirement naming a suite the pin has not got
    lowers nothing.
    """
    out: dict[str, SuiteResult] = {}
    for name, base in pin.items():
        lowered = SuiteResult(**asdict(base))
        lowered.passed = max(0, base.passed + retired.get(name, 0))
        out[name] = lowered
    return out


def resolve_floor(pin_path: Path, expected_path: Path | None = None,
                  override: Path | None = None
                  ) -> tuple[dict[str, SuiteResult], str]:
    """The floor both gates compare against, and a line naming its source.

    `override` (a `--baseline` file) wins and is read as the floor as it
    stands. Otherwise the shared pin at `pin_path`, lowered by the retirements
    in `expected_path` when that file exists. Raises OSError /
    json.JSONDecodeError on an unreadable baseline and ExpectedBaselineError on
    a malformed `suite-expected.json`.
    """
    if override is not None:
        return load_baseline(override), f"--baseline {override} (floor)"
    pin = load_baseline(pin_path)
    if expected_path is None or not Path(expected_path).is_file():
        return pin, f"shared pin {pin_path} (floor)"
    retired = load_retirements(expected_path)
    floor = lower_floor(pin, retired)
    if not any(retired.get(name) for name in pin):
        return floor, (f"shared pin {pin_path} (floor); {expected_path} "
                       f"declares no retirement")
    parts = ", ".join(f"{name} {retired[name]:+d}" for name in pin
                      if retired.get(name))
    return floor, (f"shared pin {pin_path} (floor), lowered by the "
                   f"retirements in {expected_path}: {parts}")


def report(baseline: dict[str, SuiteResult], actual: dict[str, SuiteResult],
           stream=None) -> list[str]:
    """Print a per-suite comparison against the floor. Returns the problems.

    `stream` defaults to `sys.stdout` as it is at call time, not at import.
    """
    stream = sys.stdout if stream is None else stream
    problems: list[str] = []
    for name, result in actual.items():
        print(f"  {result.headline()}", file=stream)
        base = baseline.get(name)
        if base is None:
            print(f"    note: no floor for {name} — only skips, failures and "
                  f"errors are asserted", file=stream)
            base = SuiteResult(suite=name, passed=0, ran_nothing=True)
        else:
            print(f"    floor {base.passed} passed; got {result.passed}",
                  file=stream)
            if base.degraded:
                print(
                    f"    WARNING: the floor for {name} is itself degraded "
                    f"({base.passed} passed, exit {base.exit_code}). This check "
                    f"is vacuous for this suite — it cannot detect a regression.",
                    file=stream,
                )
        suite_problems = compare(base, result)
        for problem in suite_problems:
            print(f"    FAIL: {problem}", file=stream)
        if result.skip_reasons:
            print(f"    skip reasons ({name}):", file=stream)
            for reason in result.skip_reasons:
                print(f"      {reason}", file=stream)
        problems.extend(suite_problems)
    return problems
