"""Close a project — move folder to completed/, merge it, remove worktree.

Usage (run from inside the repo):
    python <skill>/scripts/bin/close-project.py <project-id> [--yes] [--preflight] [--dry-run]

Config: the repo's `.claude/project-workflow.json` (lib/config.py), incl.
`close.merge` (`local` | `squash`) and `close.ci_workflow` (P11's subject).

Preflight (--preflight, and automatically before any mutation), one
`OUTCOME: [gate] message` line per zero-mutation check:
    P1  tracked dirty files in the project worktree: FAIL.
    P2  untracked files (`.vscode/` exempt): FAIL; --allow-untracked -> WARN.
    P5  cwd inside the project worktree (Windows lock): FAIL.
    P11 the CI workflow's run on the base head not green: FAIL;
        --allow-red-ci -> WARN. Cannot ask, or no run: WARN.
        INAPPLICABLE when `close.ci_workflow` is null.
    P15 local base branch ahead of origin: FAIL. INAPPLICABLE under `local`.
    `close:external_yield`, `close:workflow_key`: STATUS.md frontmatter keys;
        missing or unanswered FAILs.
    Session mode (`worktree: none`) prints P5 and P15 INAPPLICABLE.
    --preflight also runs every step precheck as advisory WARNs.

Dry-run (--dry-run): every step's precheck, no apply; one line per step;
non-zero exit if any precheck fails.

Flow: preflight, prompt, then close_steps.STEPS with the close-record step
spliced in before git-commit. Steps of the other merge path print
INAPPLICABLE. A persisted step cursor lets a rerun resume after a failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from config import Config, ConfigError, config_for  # noqa: E402
from streams import (  # noqa: E402
    CV_ROOT,
    AmbiguousWorktreeError,
    find_project_worktree,
    load_projects,
    read_status_frontmatter,
    status_md_path,
)
from close_steps import (  # noqa: E402
    STEPS,
    CloseCtx,
    CloseStep,
    clear_command_cache,
    find_status_md,
    main_ahead_refusal,
    run_once,
    step_inapplicable,
    workflow_key_value,
    clear_cursor,
    load_cursor,
    record_step_done,
)


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd or CV_ROOT, check=check, capture_output=True, text=True)


# Older worktrees carry a launcher-written `.vscode/tasks.json`: inherited
# dirt, not unsaved work.
_UNTRACKED_EXEMPT_SEGMENTS = (".vscode/",)


def _untracked_count(worktree: Path) -> int:
    r = run(["git", "-C", str(worktree), "status", "--porcelain"], check=False)
    return sum(
        1 for ln in r.stdout.splitlines()
        if ln.startswith("??") and not any(
            ln[3:].startswith(seg) or f"/{seg}" in ln[3:]
            for seg in _UNTRACKED_EXEMPT_SEGMENTS
        )
    )


EXTERNAL_YIELD_GATE = "close:external_yield"
WORKFLOW_KEY_GATE = "close:workflow_key"

# Values that declare no external yield (`none`) or leave it unanswered.
_EXTERNAL_YIELD_ABSENT = frozenset({"none", "n/a", "na", "-", "tbd", "todo", "?"})

# Appended to every FAIL, naming where the fix goes.
EXTERNAL_YIELD_FIX = (
    "Fix: edit the frontmatter of the project's worktree STATUS.md{where} "
    "(the copy close reads, not main's stub); the skill's "
    "templates/status-template.md carries both keys, and the repo's workflow "
    "profile ({profile}) says when to fill them")


def _external_yield_verdict(frontmatter: dict,
                            status_path: Path | None = None,
                            profile: str = Config().profile) -> tuple[str, str]:
    """(message, outcome) for `close:external_yield` from STATUS.md frontmatter.

    PASS — `external_yield:` names an artifact consumed outside the framework.
    WARN — `external_yield: none` AND `external_yield_waiver:` names a reason.
    FAIL — the field is missing/blank, or declares `none` with no waiver. The
    FAIL text ends with `EXTERNAL_YIELD_FIX`, naming `status_path` when given.
    """
    raw = str(frontmatter.get("external_yield") or "").strip().strip('"\'')
    waiver = str(frontmatter.get("external_yield_waiver") or "").strip().strip('"\'')
    fix = EXTERNAL_YIELD_FIX.format(
        where=f" ({status_path.as_posix()})" if status_path is not None else "",
        profile=profile)
    if not raw:
        return ("STATUS.md declares no `external_yield:` — name an artifact "
                "consumed OUTSIDE this framework, or set `external_yield: none` "
                "with an `external_yield_waiver:` reason. " + fix), "FAIL"
    if raw.lower() not in _EXTERNAL_YIELD_ABSENT:
        return f"external yield declared: {raw}", "PASS"
    if waiver:
        return (f"external_yield: none — waived: {waiver}"), "WARN"
    return ("external_yield: none with no `external_yield_waiver:` — name the "
            "reason this project produced nothing consumed outside the "
            "framework, or declare the artifact that was. " + fix), "FAIL"


def is_session_mode(project_id: str, root: Path) -> bool:
    """True when the project declares `worktree: none` in STATUS.md frontmatter.

    Declared, not inferred: an absent worktree may also be one removed by hand.
    """
    try:
        fm = read_status_frontmatter(project_id, root=root)
    except (Exception, SystemExit):
        return False
    return str(fm.get("worktree", "")).strip().lower().startswith("none")


def is_skill_run(project_id: str, root: Path) -> bool:
    """True when STATUS.md frontmatter carries `current_wave` and the project
    folder has no `streams/` dir: the skill-run shape new-project.py's
    scaffold is tested against (no gate reads it)."""
    try:
        proj_root = load_projects(root).get(project_id)
    except (Exception, SystemExit):
        return False
    if proj_root is None or (proj_root / "streams").is_dir():
        return False
    try:
        fm = read_status_frontmatter(project_id, root=root)
    except (Exception, SystemExit):
        return False
    return "current_wave" in fm


def emit_verdict(gate_id: str, outcome: str, message: str) -> None:
    """Print one gate verdict: `OUTCOME: [gate_id] message`."""
    print(f"{str(outcome).upper()}: [{gate_id}] {message}")


def run_preflight(
    project_id: str,
    project_worktree: Path,
    allow_untracked: bool = False,
    allow_red_ci: bool = False,
    session_mode: bool = False,
    cfg: Config | None = None,
) -> bool:
    """Run the zero-mutation preflight checks.

    Prints one verdict line per check (`emit_verdict`) and returns True iff
    no FAIL was emitted. The checks and their overrides are listed in the
    module docstring. `cfg` defaults to the repo's config (`config_for`).
    """
    if cfg is None:
        cfg = config_for(CV_ROOT, project_worktree)
    base = cfg.base_branch
    passed = True

    def emit(gate_id: str, outcome: str, message: str) -> None:
        emit_verdict(gate_id, outcome, message)

    def inapplicable(gate_id: str, reason: str) -> None:
        """Report a gate with no subject in this mode: neither FAIL nor PASS."""
        emit(gate_id, "INAPPLICABLE", f"{reason} — not checked, not passed")

    # P1 — tracked-only dirty check.
    r = run(["git", "-C", str(project_worktree), "status", "--porcelain",
             "--untracked-files=no"], check=False)
    if r.returncode != 0 or r.stdout.strip():
        emit("P1", "FAIL", f"tracked dirty files in {project_worktree}")
        passed = False
    else:
        emit("P1", "PASS", "no tracked dirty files")

    # P2 — untracked files (--allow-untracked downgrades to WARN).
    untracked = _untracked_count(project_worktree)
    if untracked > 0:
        if allow_untracked:
            emit("P2", "WARN",
                 f"{untracked} untracked file(s) in worktree (--allow-untracked active)")
        else:
            emit("P2", "FAIL",
                 f"{untracked} untracked file(s) in worktree "
                 "(commit, stash, or delete them; or pass --allow-untracked to override)")
            passed = False
    else:
        emit("P2", "PASS", "no untracked files")

    # P5 — cwd is NOT inside the project worktree (Windows live-cwd lock)
    if session_mode:
        inapplicable("P5", "the check guards against holding a lock on a "
                           "directory about to be removed; no worktree is "
                           "removed in session mode, and cwd is inside the "
                           "main checkout by design")
    else:
        cwd = None
        try:
            cwd = Path(os.environ.get("_PREFLIGHT_CWD_OVERRIDE", os.getcwd())).resolve()
            wt_resolved = project_worktree.resolve()
            # cwd is "inside" if it equals the worktree path or is a sub-path
            try:
                cwd.relative_to(wt_resolved)
                inside = True
            except ValueError:
                inside = False
        except Exception:
            inside = False

        if inside:
            emit("P5", "FAIL",
                 f"cwd {cwd} is inside the project worktree — "
                 "cd elsewhere before closing (Windows cwd lock)")
            passed = False
        else:
            emit("P5", "PASS", "cwd is outside the project worktree")

    # P11 — the configured CI workflow's run on the base head commit must be
    # success. Cannot ask is a WARN: it cannot tell "red" from "can't ask".
    if cfg.ci_workflow is None:
        inapplicable("P11", "no `close.ci_workflow` configured")
    else:
        main_head = run(["git", "-C", str(CV_ROOT), "rev-parse", base],
                        check=False).stdout.strip()
        ci_query = ["gh", "run", "list", "--workflow", cfg.ci_workflow,
                    "--branch", base]
        if main_head:
            ci_query += ["--commit", main_head]
        try:
            gh_ci = run_once(ci_query + ["--limit", "1", "--json",
                                         "conclusion,status,url"], CV_ROOT)
        except OSError:
            gh_ci = None

        if gh_ci is None:
            emit("P11", "WARN", f"gh not runnable — cannot confirm {base} CI is green")
        elif gh_ci.returncode != 0:
            emit("P11", "WARN",
                 f"could not query {base} CI run — "
                 f"{gh_ci.stderr.strip()[:120] or 'gh unauthenticated'}")
        else:
            try:
                runs = json.loads(gh_ci.stdout or "[]")
            except json.JSONDecodeError:
                runs = None
            if not runs:
                emit("P11", "WARN",
                     f"no CI run found for {base} — cannot confirm green floor")
            else:
                latest = runs[0]
                conclusion = latest.get("conclusion")
                url = latest.get("url", "<no url>")
                if conclusion == "success":
                    emit("P11", "PASS", f"{base} CI green — {url}")
                elif allow_red_ci:
                    emit("P11", "WARN",
                         f"{base} CI is {conclusion or latest.get('status', 'unknown')} — "
                         f"excusing {url} (--allow-red-ci active)")
                else:
                    emit("P11", "FAIL",
                         f"{base} CI is {conclusion or latest.get('status', 'unknown')} — "
                         f"{url} — fix it, or pass --allow-red-ci to excuse this run")
                    passed = False

    # P15 — local base ahead of origin: refuse before any mutation (the
    # `git-push` precheck refuses again, but only after git-commit).
    if session_mode:
        inapplicable("P15", f"no project branch is pushed; whether `{base}` is "
                            "pushed is the session's own cadence")
    elif cfg.merge != "squash":
        inapplicable("P15", f"close.merge is {cfg.merge!r}: the close pushes "
                            "nothing, so unpushed base commits ride nowhere")
    else:
        try:
            branch = current_branch(project_worktree)
        except (subprocess.CalledProcessError, OSError):
            branch = "the project branch"
        refusal = main_ahead_refusal(CV_ROOT, branch, base)
        if refusal:
            emit("P15", "FAIL", refusal)
            passed = False
        else:
            emit("P15", "PASS", f"local {base} is level with origin/{base}")

    # close:external_yield: the question must be answered in writing.
    try:
        fm = read_status_frontmatter(project_id, root=project_worktree)
    except (Exception, SystemExit) as exc:
        emit(EXTERNAL_YIELD_GATE, "FAIL",
             f"could not read STATUS.md frontmatter — {exc}")
        passed = False
    else:
        try:
            yield_status = status_md_path(project_id, root=project_worktree)
        except (Exception, SystemExit):
            yield_status = None
        yield_msg, yield_outcome = _external_yield_verdict(fm, yield_status,
                                                           cfg.profile)
        emit(EXTERNAL_YIELD_GATE, yield_outcome, yield_msg)
        if yield_outcome == "FAIL":
            passed = False
        # close:workflow_key: index-row's predicate (workflow_key_value),
        # checked before any mutation rather than after git-mv.
        try:
            wf_text = (yield_status.read_text(encoding="utf-8")
                       if yield_status is not None else "")
        except OSError:
            wf_text = ""
        wf_value = workflow_key_value(wf_text)
        if wf_value is not None:
            emit(WORKFLOW_KEY_GATE, "PASS", f"workflow: {wf_value}")
        else:
            emit(WORKFLOW_KEY_GATE, "FAIL",
                 "STATUS.md frontmatter has no top-level `workflow:` key — the "
                 "close's index-row step refuses it after git-mv and "
                 "status-flip have applied. Add `workflow: project-workflow` "
                 "to the worktree STATUS.md frontmatter (the skill's "
                 "templates/status-template.md carries it)")
            passed = False

    return passed


def _build_ctx(project_id: str, project_worktree: Path,
               session_mode: bool = False, cfg: Config | None = None) -> CloseCtx:
    """Build a CloseCtx from resolved paths."""
    if cfg is None:
        cfg = config_for(CV_ROOT, project_worktree)
    project_root = load_projects(project_worktree)[project_id]
    slug = project_root.name.replace(f"project-{project_id}-", "")
    old_rel = f"{cfg.projects_dir}/current/project-{project_id}-{slug}"
    new_rel = f"{cfg.projects_dir}/completed/project-{project_id}-{slug}"
    project_branch = current_branch(project_worktree)
    return CloseCtx(
        project_id=project_id,
        project_worktree=project_worktree,
        cv_root=CV_ROOT,
        project_branch=project_branch,
        slug=slug,
        old_rel=old_rel,
        new_rel=new_rel,
        session_mode=session_mode,
        cfg=cfg,
    )


def run_dry_run(ctx: CloseCtx) -> int:
    """Run all step prechecks, no apply; one line per step; 1 if any fails."""
    any_fail = False
    for step in CLOSE_STEPS:
        skip = step_inapplicable(step, ctx)
        if skip:
            print(f"  {step.name} -> INAPPLICABLE: {skip}")
            continue
        try:
            reason = step.precheck(ctx)
        except (Exception, SystemExit) as exc:
            # lib/streams.py raises SystemExit; keep going to report every step.
            reason = f"precheck raised {type(exc).__name__}: {exc}"
        if reason is None:
            print(f"  {step.name} -> ok")
        else:
            print(f"  {step.name} -> FAIL: {reason}")
            any_fail = True
    return 1 if any_fail else 0


def _print_maintenance_pass_outcomes(ctx: CloseCtx) -> None:
    """Print, once, the maintenance-pass outcomes the step left on ctx."""
    outcomes = ctx._resolved.pop("maintenance_pass_outcomes", None)
    if outcomes:
        for outcome in outcomes:
            print(f"  [{outcome.result.status}] {outcome.pass_name}: {outcome.result.message}")


# --- Close record: the modelUsage row, the capped report, receipt untracking ---

CLOSE_REPORT_BYTE_CAP = 2048

CLOSE_RECORD_HEADING = "## Close record"

# Untracked at close (`git rm --cached`) but kept on disk.
RECEIPT_DIRS = ("receipts", "scratch")


def _model_usage_from_envelope(path: Path) -> dict | None:
    """`modelUsage` straight out of a session envelope, if it carries one."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    usage = data.get("modelUsage") if isinstance(data, dict) else None
    return usage if isinstance(usage, dict) and usage else None


def render_model_usage_row(model_usage: dict | None) -> str:
    """One markdown line of session usage; `unobserved` (not zero) without one."""
    if not model_usage:
        return "- `modelUsage`: unobserved (no session envelope supplied)"
    parts = []
    for model in sorted(model_usage):
        b = model_usage[model] or {}
        parts.append(
            f"{model} in={b.get('input', 0)} "
            f"cache_read={b.get('cache_read', 0)} "
            f"cache_write={b.get('cache_write', 0)} "
            f"out={b.get('output', 0)} calls={b.get('calls', 0)}"
        )
    return "- `modelUsage`: " + "; ".join(parts)


def build_close_report(project_id: str, model_usage: dict | None,
                       step_states: list[tuple[str, str]]) -> str:
    """The close record block for STATUS.md, capped at CLOSE_REPORT_BYTE_CAP
    bytes with an explicit truncation marker."""
    lines = [
        CLOSE_RECORD_HEADING,
        "",
        render_model_usage_row(model_usage),
        f"- steps: {len(step_states)} "
        f"({sum(1 for _, s in step_states if s == 'done')} done)",
        "",
    ]
    body = "\n".join(lines)
    encoded = body.encode("utf-8")
    if len(encoded) <= CLOSE_REPORT_BYTE_CAP:
        return body
    keep = encoded[:CLOSE_REPORT_BYTE_CAP - 32].decode("utf-8", errors="ignore")
    return keep.rstrip() + f"\n- [truncated at {CLOSE_REPORT_BYTE_CAP} bytes]\n"


def write_close_record(status_path: Path, report: str) -> None:
    """Replace (or append) the `## Close record` section of a STATUS.md."""
    text = status_path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^{re.escape(CLOSE_RECORD_HEADING)}\s*$.*?(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(report.rstrip("\n") + "\n\n", text, count=1)
    else:
        text = text.rstrip("\n") + "\n\n" + report.rstrip("\n") + "\n"
    status_path.write_text(text, encoding="utf-8")


def _relocate_receipt_dir(src: Path, dest: Path) -> None:
    """Move `src` to `dest`, merging file by file when `dest` exists (a resume)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.move(str(src), str(dest))
        return
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(item), str(target))
    shutil.rmtree(src, ignore_errors=True)


def untrack_receipts(worktree: Path, project_rel: str,
                      cv_root: Path | None = None) -> list[str]:
    """Untrack this project's RECEIPT_DIRS (`git rm --cached`); keep the files.

    With `cv_root`, each dir is moved to `cv_root / project_rel / <name>` so
    it survives `worktree-remove`; `cv_root=None` only untracks.
    """
    untracked: list[str] = []
    for name in RECEIPT_DIRS:
        rel = f"{project_rel}/{name}"
        src = worktree / rel
        if not src.is_dir():
            continue
        r = run(["git", "-C", str(worktree), "rm", "-r", "--cached", "--quiet",
                 "--ignore-unmatch", rel], check=False)
        if r.returncode != 0:
            continue
        untracked.append(rel)
        if cv_root is not None:
            _relocate_receipt_dir(src, cv_root / rel)
    return untracked


def _precheck_close_record(ctx: CloseCtx) -> str | None:
    path = find_status_md(ctx)
    if path is None:
        return f"STATUS.md not found under {ctx.old_rel} or {ctx.new_rel}"
    ctx._resolved["close_record_status_md"] = path
    return None


def _apply_close_record(ctx: CloseCtx) -> None:
    status_path: Path = ctx._resolved["close_record_status_md"]
    envelope = ctx._resolved.get("session_envelope")
    model_usage = None
    if envelope:
        envelope = Path(envelope)
        model_usage = _model_usage_from_envelope(envelope)
    report = build_close_report(
        ctx.project_id, model_usage, ctx._resolved.get("step_states") or [],
    )
    write_close_record(status_path, report)
    run(["git", "-C", str(ctx.project_worktree), "add", str(status_path)],
        check=False)

    project_rel = (ctx.new_rel
                   if (ctx.project_worktree / ctx.new_rel).is_dir()
                   else ctx.old_rel)
    untracked = untrack_receipts(ctx.project_worktree, project_rel, ctx.cv_root)
    ctx._resolved["receipts_untracked"] = untracked


_STEP_CLOSE_RECORD = CloseStep(
    name="close-record",
    description=("Write the capped close report + modelUsage row into STATUS.md "
                 "and untrack receipts"),
    mutating=True,
    precheck=_precheck_close_record,
    apply=_apply_close_record,
)


def _close_steps() -> list[CloseStep]:
    """STEPS with `close-record` spliced in before `git-commit`, so its writes
    land in the archive commit."""
    out: list[CloseStep] = []
    for step in STEPS:
        if step.name == "git-commit":
            out.append(_STEP_CLOSE_RECORD)
        out.append(step)
    if _STEP_CLOSE_RECORD not in out:
        out.append(_STEP_CLOSE_RECORD)
    return out


CLOSE_STEPS: list[CloseStep] = _close_steps()


def _print_step_report(step_states: list[tuple[str, str]],
                       label: str = "--- step report ---") -> None:
    """Print one `name: state` line per step."""
    print(label)
    for name, state in step_states:
        print(f"  {name}: {state}")
    print("---")


def current_branch(worktree: Path) -> str:
    r = run(["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "HEAD"])
    return r.stdout.strip()


def prompt_yes(message: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    reply = input(f"{message} [y/N] ").strip().lower()
    return reply in {"y", "yes"}


def main(argv: list[str]) -> int:
    # One invocation, one answer per gh / ls-remote query (close_steps.run_once).
    clear_command_cache()
    p = argparse.ArgumentParser(
        description="Close a project: archive folder, merge it, remove worktree.",
    )
    p.add_argument("project_id")
    p.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    p.add_argument("--preflight", action="store_true",
                   help="preflight checks plus advisory step prechecks; exit 0 iff no FAIL")
    p.add_argument("--dry-run", action="store_true",
                   help="run every step's precheck, no apply; exit 1 if any fails")
    p.add_argument("--allow-untracked", action="store_true",
                   help="downgrade P2 (untracked files) from FAIL to WARN")
    p.add_argument("--allow-red-ci", action="store_true",
                   help="downgrade P11 (base-branch CI green) from FAIL to WARN")
    p.add_argument("--step-log", metavar="PATH",
                   help="also write the step report as JSON to PATH")
    p.add_argument("--session-envelope", metavar="PATH",
                   help="session envelope JSON for the `modelUsage` row "
                        "(omitted: `unobserved`)")
    args = p.parse_args(argv[1:])

    try:
        project_worktree = find_project_worktree(args.project_id, main_repo=CV_ROOT)
    except AmbiguousWorktreeError as exc:
        print(f"refuse: {exc}", file=sys.stderr)
        return 1
    session_mode = False
    if project_worktree is None:
        # Session mode closes against the main checkout; never cut a worktree.
        if is_session_mode(args.project_id, CV_ROOT):
            project_worktree = CV_ROOT
            session_mode = True
            print(f"session mode: {args.project_id} declares `worktree: none` — "
                  f"closing against the main checkout at {CV_ROOT}")
        else:
            print(f"refuse: no project worktree for {args.project_id} — if this "
                  f"project runs as sessions on main, declare `worktree: none` "
                  f"in its STATUS.md frontmatter", file=sys.stderr)
            return 1

    try:
        cfg = config_for(CV_ROOT, project_worktree)
    except ConfigError as exc:
        print(f"refuse: {exc}", file=sys.stderr)
        return 1

    if args.preflight:
        ok = run_preflight(
            args.project_id, project_worktree,
            session_mode=session_mode,
            allow_untracked=args.allow_untracked,
            allow_red_ci=args.allow_red_ci,
            cfg=cfg,
        )
        # Advisory: a refusal is a WARN; only the checks above set the exit code.
        print("--- step precheck pass (advisory) ---")
        try:
            ctx = _build_ctx(args.project_id, project_worktree, session_mode, cfg)
            for step in CLOSE_STEPS:
                skip = step_inapplicable(step, ctx)
                if skip:
                    print(f"  {step.name} -> INAPPLICABLE: {skip}")
                    continue
                reason = step.precheck(ctx)
                if reason is None:
                    print(f"  {step.name} -> ok")
                else:
                    print(f"  {step.name} -> WARN: {reason}")
        except (Exception, SystemExit) as exc:
            print(f"  WARN: could not run step prechecks: {exc}")
        print("-------------------------------------")
        return 0 if ok else 1

    if args.dry_run:
        print(f"--- dry-run: {args.project_id} ---")
        try:
            ctx = _build_ctx(args.project_id, project_worktree, session_mode, cfg)
        except (Exception, SystemExit) as exc:
            print(f"FAIL: could not build close context: {exc}", file=sys.stderr)
            return 1
        rc = run_dry_run(ctx)
        if rc == 0:
            print("dry-run: all prechecks passed")
        else:
            print("dry-run: one or more prechecks FAILED", file=sys.stderr)
        return rc

    # Normal close path: run preflight first and abort before any mutation on FAIL.
    print("--- preflight ---")
    preflight_ok = run_preflight(
        args.project_id, project_worktree,
        session_mode=session_mode,
        allow_untracked=args.allow_untracked,
        allow_red_ci=args.allow_red_ci,
        cfg=cfg,
    )
    print("-----------------")
    if not preflight_ok:
        print("refuse: preflight failed — resolve the above FAIL(s) before closing.",
              file=sys.stderr)
        return 1

    ctx = _build_ctx(args.project_id, project_worktree, session_mode, cfg)
    fm = read_status_frontmatter(args.project_id, root=project_worktree)
    print(f"Project:  {args.project_id} ({ctx.slug})")
    print(f"Branch:   {ctx.project_branch}")
    print(f"Move:     {ctx.old_rel} -> {ctx.new_rel}")
    print(f"Status:   {fm.get('status', '?')} -> complete")

    base = cfg.base_branch
    if session_mode:
        plan = f"mv, commit on {base} (no merge, no branch or worktree to remove)"
    elif cfg.merge == "squash":
        plan = f"mv, commit, PR to {base}, squash-merge, cleanup"
    else:
        plan = f"mv, commit, local merge commit into {base}, cleanup"
    if not prompt_yes(f"Close project {args.project_id}: {plan}?", assume_yes=args.yes):
        print("aborted.")
        return 0

    step_states: list[tuple[str, str]] = []
    # Read by the close-record step: the `modelUsage` source and the live log.
    ctx._resolved["session_envelope"] = args.session_envelope
    ctx._resolved["step_states"] = step_states

    # Steps a prior crashed run recorded done skip both precheck and apply.
    done_steps = load_cursor(ctx.cv_root, ctx.project_id, ctx.project_branch)
    if done_steps:
        print(f"resume: {len(done_steps)} step(s) already recorded done from a prior run — skipping them")

    for step in CLOSE_STEPS:
        if step.name in done_steps:
            step_states.append((step.name, "skipped (resumed: already done)"))
            continue

        # No subject here: run neither precheck nor apply, and never say "done".
        skip = step_inapplicable(step, ctx)
        if skip:
            print(f"[{step.name}] INAPPLICABLE: {skip}")
            why = "session mode" if session_mode and step.session_inapplicable \
                else f"close.merge {cfg.merge}"
            step_states.append((step.name, f"inapplicable ({why}): {skip}"))
            continue

        reason = step.precheck(ctx)
        if reason is not None:
            return _stop_close(
                args, ctx, step_states, step.name, f"failed: {reason}",
                f"refuse: precheck failed for step '{step.name}': {reason}")

        try:
            step.apply(ctx)
            step_states.append((step.name, "done"))
            done_steps.add(step.name)
            record_step_done(ctx.cv_root, ctx.project_id, ctx.project_branch, done_steps)
        except subprocess.CalledProcessError as exc:
            stderr_tail = (exc.stderr or "").strip()[:300]
            return _stop_close(args, ctx, step_states, step.name, "failed",
                               f"step '{step.name}' failed: {stderr_tail}", done_steps)
        except (RuntimeError, OSError) as exc:
            # RuntimeError: a step's own refusal. OSError: a filesystem failure
            # in apply() (e.g. a locked file on Windows during a move).
            return _stop_close(args, ctx, step_states, step.name, "failed",
                               f"step '{step.name}' failed: {exc}", done_steps)
        else:
            _print_maintenance_pass_outcomes(ctx)

    _print_step_report(step_states)
    _maybe_write_step_log(args, step_states)
    clear_cursor(ctx.cv_root, ctx.project_id)  # fail-soft

    print()
    print("=" * 72)
    print(f"CLOSED: project {args.project_id} archived to completed/")
    print("=" * 72)
    return 0


def _stop_close(args: argparse.Namespace, ctx: CloseCtx,
                step_states: list[tuple[str, str]], step_name: str, state: str,
                message: str, done_steps: set[str] | None = None) -> int:
    """Mark `step_name` `state` and every later step not-reached, report, return 1.

    `done_steps` is given for an apply failure: the pass outcomes and a
    resume hint are then printed too; a precheck refusal gives neither.
    """
    step_states.append((step_name, state))
    seen = {n for n, _ in step_states}
    step_states.extend((s.name, "not-reached") for s in CLOSE_STEPS if s.name not in seen)
    if done_steps is not None:
        _print_maintenance_pass_outcomes(ctx)
    _print_step_report(step_states)
    print(message, file=sys.stderr)
    if done_steps is not None:
        print(f"resume: {len(done_steps)} step(s) recorded done — rerun to "
              f"continue from '{step_name}'", file=sys.stderr)
    _maybe_write_step_log(args, step_states)
    return 1


def _maybe_write_step_log(args: argparse.Namespace, step_states: list[tuple[str, str]]) -> None:
    """Optionally write the step report as JSON to --step-log PATH."""
    if not getattr(args, "step_log", None):
        return
    data = [{"step": name, "state": state} for name, state in step_states]
    Path(args.step_log).write_text(json.dumps(data, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
