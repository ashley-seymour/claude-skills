---
project: "{project_id}"
name: {slug}
status: active
current_wave: 1
worktree: {worktree}
branch: {branch}
deadlines: none  # or: ["<YYYY-MM-DD> <what is due>", ...]
# Read by close-project.py's `close:external_yield` gate, which FAILs while
# both are blank. Decide at create, fill here at the cut, check before close
# (the repo's workflow profile, § External yield). external_yield: an artifact
# consumed OUTSIDE this framework (a doc, a dataset, a merged feature a user
# runs). If there is none, set it to `none` and name the reason in
# external_yield_waiver:.
external_yield: ""
external_yield_waiver: ""
# Required by close_steps.py's index-row step, which refuses the close without
# it; not part of the skill schema (the repo's workflow profile).
workflow: project-workflow
last_updated: {now}
last_updated_by: new-project.py
---

# STATUS — {slug} (project {project_id})

## Resume

<!-- resume-v1 -->
<!-- Fixed position, directly under the H1. Overwritten at every stop, never
     appended to. At most 6 lines, pointers not narrative. -->
- state: <last completed step, and how it was verified>
- next: /project-workflow {project_id} wave 1
- decisions: <open decision blocking the next step, or `none`>

## Wave 1 — <theme>  (status: pending)

### S-01 — <one-line mission>
owns:     <paths this stream may write>
consumes: <paths it reads>
deliver:  artifacts/S-01-<slug>.md

## Known gaps

<!-- One row per unresolved item at close; the close's Outstanding column
     counts undispositioned rows here. Keep the heading and table when empty. -->

| Item | Disposition |
|---|---|

## Log

<!-- One line per wave: `- wave N — <sha> — <one line>`. -->
