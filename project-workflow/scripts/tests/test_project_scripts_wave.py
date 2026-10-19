"""Regression pin for owns coverage of a bare directory entry.

The return check lives in `wave-close.py`; these cases pin `wave-close.py`'s
`covers`, which both its return check and its `git status` gate use.

The scripts live under the skill's `scripts/bin/` with hyphens in some
filenames, so every one of them is loaded by path rather than imported.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"


def _load(filename: str):
    name = "ps_" + filename.replace("-", "_").removesuffix(".py")
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------
# Owns coverage and the bare directory entry
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def wave_close():
    return _load("wave-close.py")


def _covers(mod, path: str, entries: list[str]) -> bool:
    return any(mod.covers(path, mod.as_dir_glob(e)) for e in entries)


@pytest.mark.parametrize("entry", ["inputs/race_params", "inputs/race_params/"])
def test_dir_entry_covers_files_beneath_it(wave_close, entry):
    assert _covers(wave_close, "inputs/race_params/cm19-kyoto.json", [entry])
    assert _covers(wave_close, "inputs/race_params/nested/deep.json", [entry])


def test_dir_entry_does_not_cover_a_name_sharing_its_prefix(wave_close):
    # `foo` must not swallow `foobar` — the separator is re-appended, not dropped.
    assert not _covers(wave_close, "inputs/race_params2/x.json",
                       ["inputs/race_params"])
    assert not _covers(wave_close, "inputs/race_paramsX", ["inputs/race_params"])
    assert not _covers(wave_close, "inputs/race_params2/x.json",
                       ["inputs/race_params/"])


def test_exact_and_glob_entries_still_behave(wave_close):
    assert _covers(wave_close, "sim/engine/course_data.py",
                   ["sim/engine/course_data.py"])
    assert _covers(wave_close, "docs/a.md", ["docs/*.md"])
    # `*` does not cross a separator; `**` does.
    assert not _covers(wave_close, "docs/sub/a.md", ["docs/*.md"])
    assert _covers(wave_close, "docs/sub/a.md", ["docs/**"])
    assert not _covers(wave_close, "sim/engine/course_data.py", ["engine/tests"])
