"""Maintenance-pass contract and loader for project close.

A pass module under this directory exposes one module-level descriptor::

    PASS = ClosePass(
        name="short-kebab-name",       # unique; sort key for run order
        description="one line",
        blocking=False,                # required: the pass states its policy
        run=<callable(ctx: CloseCtx) -> PassResult>,
    )

``run`` must be unit-testable against a synthetic ``CloseCtx`` (no ``gh``,
no network) and reports only through its ``PassResult``; an exception it
raises becomes ``status="fail"`` in ``run_passes``. Status: ``"ok"`` nothing
to act on, ``"info"`` printed and passed, ``"fail"`` blocks a ``blocking``
pass. Discovery imports every ``*.py`` here except names starting with
``_``, sorts by ``ClosePass.name`` and raises ``ValueError`` on a duplicate.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence


# ---------------------------------------------------------------------------
# Contract types
# ---------------------------------------------------------------------------

_VALID_STATUSES: frozenset[str] = frozenset({"ok", "info", "fail"})


@dataclass(frozen=True)
class PassResult:
    """What a pass hands back to the loader; a stray `status` raises ValueError."""
    status: Literal["ok", "info", "fail"]
    message: str
    detail_path: Path | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                f"PassResult.status must be one of {sorted(_VALID_STATUSES)}, "
                f"got {self.status!r}"
            )


@dataclass(frozen=True)
class ClosePass:
    """Module-level descriptor every pass module exposes as `PASS`."""
    name: str
    description: str
    # Required, no default: a later refactor must not silently relax every
    # pass to `blocking=False` by adding `= False` here.
    blocking: bool
    run: Callable[[Any], PassResult]  # Any = CloseCtx; avoids importing close_steps here


@dataclass(frozen=True)
class PassOutcome:
    """One pass's result plus the policy the runner applied to it."""
    pass_name: str
    blocking: bool
    result: PassResult


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def discover_passes(directory: Path | None = None) -> list[ClosePass]:
    """Every `ClosePass` under `directory` (default: this package), by `.name`.

    Raises `TypeError` for a file with no `PASS: ClosePass`, `ValueError` for
    a duplicate `.name`, and whatever a module's import raises.
    """
    directory = Path(directory) if directory is not None else Path(__file__).resolve().parent
    passes: list[ClosePass] = []
    # `.name` -> source file, purely to render a useful collision message.
    sources: dict[str, str] = {}

    if directory.is_dir():
        for path in sorted(directory.glob("*.py")):
            if path.name == "__init__.py" or path.name.startswith("_"):
                continue
            module_name = f"_close_pass_{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load pass module: {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            pass_obj = getattr(module, "PASS", None)
            if not isinstance(pass_obj, ClosePass):
                raise TypeError(
                    f"{path} does not expose a module-level `PASS: ClosePass` descriptor"
                )
            if pass_obj.name in sources:
                raise ValueError(
                    f"duplicate ClosePass.name {pass_obj.name!r}: "
                    f"{sources[pass_obj.name]} and {path} both claim it -- "
                    "name is the sort key; rename one"
                )
            sources[pass_obj.name] = str(path)
            passes.append(pass_obj)

    passes.sort(key=lambda p: p.name)
    return passes


def run_passes(passes: Sequence[ClosePass], ctx: Any) -> tuple[list[PassOutcome], bool]:
    """Run every pass against `ctx`; never let one raise past this function.

    Returns `(outcomes, overall_passed)`. `overall_passed` is False iff at
    least one `blocking=True` pass returned `status == "fail"` -- a
    `blocking=False` pass reporting "fail" never flips it.
    """
    outcomes: list[PassOutcome] = []
    overall_passed = True
    for close_pass in passes:
        try:
            result = close_pass.run(ctx)
        except Exception as exc:  # noqa: BLE001 - a pass must not strand the close
            result = PassResult(status="fail", message=f"{type(exc).__name__}: {exc}")
        outcomes.append(PassOutcome(pass_name=close_pass.name, blocking=close_pass.blocking, result=result))
        if close_pass.blocking and result.status == "fail":
            overall_passed = False
    return outcomes, overall_passed
