#!/usr/bin/env bash
# away-run watchdog: one fresh `claude -p --agent away-orchestrator` per queue
# item, back to back, until the queue is empty, the hard stop, or a STOP file.
#
# arm.ps1 copies this file and templates/entry-prompt.md into the run's scratch
# folder, writes away.env beside them, and registers a repeating scheduled task
# (away-<repo>-<date>) that runs:   bash -l <scratch>/watchdog.sh <scratch>/away.env
# The mkdir lock makes a firing while a loop is alive a no-op.
#
# Stop:     touch <scratch>/STOP   (checked between iterations and while sleeping)
# Dry run:  CLAUDE=<stub> AWAY_NO_SLEEP=1 bash watchdog.sh <env>   (SKILL.md, § What the watchdog does)
#           A CLAUDE or PY set in the environment wins over the one away.env names.

set -uo pipefail

ENV_FILE="${1:?usage: bash watchdog.sh <away.env>}"
CLAUDE_FROM_ENV="${CLAUDE:-}" PY_FROM_ENV="${PY:-}"
set -a; . "$ENV_FILE"; set +a
[ -n "$CLAUDE_FROM_ENV" ] && CLAUDE=$CLAUDE_FROM_ENV
[ -n "$PY_FROM_ENV" ] && PY=$PY_FROM_ENV

: "${WORKTREE:?set WORKTREE (the away/<date> worktree)}"
: "${RUN_DIR:?set RUN_DIR (queue, decisions, log, report; inside WORKTREE)}"
: "${SCRATCH:?set SCRATCH (untracked: logs, locks, iteration JSON)}"
STOP_EPOCH=$(date -d "${STOP_AT:?set STOP_AT, e.g. 2026-10-01 07:00}" +%s) || exit 1

MAX_TURNS="${MAX_TURNS:-150}"        # passed as --max-turns; confirmed to take effect (2.1.284)
BUDGET_USD="${BUDGET_USD:-25}"       # runaway guard per iteration, imputed dollars; overrun = kind budget
RESERVE_MIN="${RESERVE_MIN:-30}"     # minutes before STOP_AT kept for the close-out
WINDOW_CAP_USD="${WINDOW_CAP_USD:-40}"  # quota reserve: imputed spend per rolling 5 h; 0 = off
LEASE_MIN="${LEASE_MIN:-90}"         # a claim older than this is a dead iteration's
MAX_ATTEMPTS="${MAX_ATTEMPTS:-3}"
NO_PROGRESS_MAX="${NO_PROGRESS_MAX:-3}"  # ok iterations in a row that leave queue.md unchanged
FAIL_MAX="${FAIL_MAX:-5}"                # error or unreadable iterations in a row
CLAUDE="${CLAUDE:-claude}"           # a stub path (environment or away.env) gives a dry run
PY="${PY:-python}"
PROMPT_TEMPLATE="${PROMPT_TEMPLATE:-$SCRATCH/entry-prompt.md}"
BACKOFF=300
BACKOFF_MAX=1800

QUEUE="$RUN_DIR/queue.md"
QUEUE_LOCK="$SCRATCH/queue.lock"
COSTS="$SCRATCH/costs.tsv"
mkdir -p "$SCRATCH"
LOG="$SCRATCH/watchdog.log"
log() { printf '%s | %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

read -r -d '' HELPER <<'PY'
import datetime as dt, json, re, sys, time
from zoneinfo import ZoneInfo

LIMIT = re.compile(r"hit your (?:(\w+) )?limit|usage limit reached", re.I)
OLD_RESET = re.compile(r"usage limit reached\|(\d{9,})", re.I)   # older text: "...|<epoch>"
RESET = re.compile(r"resets\s+(?:at\s+)?(?:([A-Z][a-z]+)\s+(\d{1,2}),?\s+(?:at\s+)?)?"
                   r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)(?:\s*\(([^)]+)\))?", re.I)

def reset_epoch(text, now):
    """'resets 6:30am (Europe/London)' or 'resets Oct 3, 6am (...)' -> epoch + 60 s."""
    m = RESET.search(text or "")
    if not m:
        return 0
    mon, day, h, mi, ap, tz = m.groups()
    try:
        zone = ZoneInfo(tz) if tz else None
    except Exception:
        zone = None
    hour = int(h) % 12 + (12 if ap.lower() == "pm" else 0)
    base = dt.datetime.fromtimestamp(now, zone)
    cand = base.replace(hour=hour, minute=int(mi or 0), second=0, microsecond=0)
    if mon:
        month = dt.datetime.strptime(mon[:3].title(), "%b").month
        cand = cand.replace(month=month, day=int(day))
        if cand.timestamp() < now - 86400:
            cand = cand.replace(year=cand.year + 1)
    elif cand.timestamp() <= now:
        cand += dt.timedelta(days=1)
    return int(cand.timestamp()) + 60

def rows(path):
    header, out = None, []
    for line in open(path, encoding="utf-8"):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
        elif not set("".join(cells)) <= set("-: "):
            out.append(dict(zip(header, cells)))
    return out

def lease_expired(value, now):
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%d %H:%M")) <= now
    except ValueError:
        return True

def lease_epoch(value):
    try:
        return int(time.mktime(time.strptime(value, "%Y-%m-%d %H:%M")))
    except ValueError:
        return 0

def queue(path, now, max_attempts):
    """claimable mech pending claimed done blocked next_lease (0, or the epoch the
    earliest live claim under the attempts cap lapses)."""
    n = dict(claimable=0, mech=0, pending=0, claimed=0, done=0, blocked=0, next_lease=0)
    for r in rows(path):
        st = r.get("status", "").lower()
        if st in n:
            n[st] += 1
        try:
            attempts = int(r.get("attempts") or 0)
        except ValueError:
            attempts = 0
        free = st == "pending" or (st == "claimed" and lease_expired(r.get("lease_until", ""), now))
        if free and attempts < max_attempts:
            n["claimable"] += 1
            n["mech"] += r.get("agent", "") == "mechanical-worker"
        elif st == "claimed" and not free and attempts < max_attempts:
            at = lease_epoch(r.get("lease_until", ""))
            n["next_lease"] = min(n["next_lease"] or at, at)
    print(*n.values())

def release(path, tag, now):
    """A finished process holds no live claim: lapse the lease of rows still
    claimed by it, so the next iteration re-claims them (attempts bounds it)."""
    lines = open(path, encoding="utf-8").read().split("\n")
    header, stamp, n = None, time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), 0
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]; continue
        r = dict(zip(header, cells))
        if (r.get("status", "").lower() == "claimed" and r.get("claimed_by") == tag
                and "lease_until" in header):
            parts = line.split("|")   # edit the one raw cell: other cells keep their padding
            raw = parts[header.index("lease_until") + 1]
            new = (raw[:len(raw) - len(raw.lstrip())] if raw.strip() else " ") + stamp
            parts[header.index("lease_until") + 1] = new + " " * max(1, len(raw) - len(new))
            lines[i] = "|".join(parts)
            n += 1
    if n:
        open(path, "w", encoding="utf-8", newline="\n").write("\n".join(lines))
    print(n)

def verdict(path, now):
    """kind fam reset cost orch_status turns. Judge by is_error/api_error_status, not num_turns."""
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        print("unreadable - 0 0 - 0"); return
    text, status = d.get("result") or "", d.get("api_error_status")
    lim = LIMIT.search(text) if (d.get("is_error") or status) else None
    fam, reset = "-", 0
    if lim or status == 429:
        kind, fam = "limit", ((lim.group(1) if lim else None) or "all").lower()
        reset = reset_epoch(text, now)
        old = OLD_RESET.search(text)
        if not reset and old:
            reset = int(old.group(1)) + 60
    elif d.get("subtype") == "error_max_budget_usd" or d.get("terminal_reason") == "budget_exhausted":
        kind = "budget"   # --max-budget-usd hit: is_error true, no result text
    elif d.get("subtype") == "error_max_turns":
        kind = "max_turns"
    elif d.get("is_error") or status:
        kind = "error"
    else:
        kind = "ok"
    m = re.findall(r'"status"\s*:\s*"([a-z-]+)"', text)
    print(kind, fam, reset, f"{d.get('total_cost_usd') or 0:.3f}", m[-1] if m else "-",
          d.get("num_turns") or 0)

def window(path, now, cap):
    """Quota reserve: 0 if the rolling 5 h imputed spend is under cap, else the epoch it frees up."""
    if cap <= 0:
        print(0); return
    try:
        spent = [tuple(map(float, l.split())) for l in open(path) if l.strip()]
    except FileNotFoundError:
        print(0); return
    recent = [(t, c) for t, c in spent if t > now - 18000]
    print(int(min(t for t, _ in recent) + 18000) if sum(c for _, c in recent) >= cap else 0)

def render(src, dst, *pairs):
    text = open(src, encoding="utf-8").read()
    for p in pairs:
        k, _, v = p.partition("=")
        text = text.replace("{{%s}}" % k, v)
    open(dst, "w", encoding="utf-8", newline="\n").write(text)

def stub_report(run_dir, reason, log_path, iters):
    import os
    dst = os.path.join(run_dir, "morning-report.md")
    if os.path.exists(dst):
        return
    tail = open(log_path, encoding="utf-8").read().splitlines()[-25:]
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Morning report (written by the watchdog, not by a close-out)\n\n"
                f"No close-out iteration completed. Watchdog exit: **{reason}** after {iters} "
                "iterations. Read `queue.md` and `log.md` for what closed; this file is "
                "uncommitted.\n\n## Watchdog log tail\n\n```\n" + "\n".join(tail) + "\n```\n")

cmd, args = sys.argv[1], sys.argv[2:]
now = time.time()
if cmd == "queue": queue(args[0], now, int(args[1]))
elif cmd == "release": release(args[0], args[1], now)
elif cmd == "verdict": verdict(args[0], now)
elif cmd == "window": window(args[0], now, float(args[1]))
elif cmd == "render": render(*args)
elif cmd == "report": stub_report(*args)
PY
helper() { "$PY" -c "$HELPER" "$@"; }

# Sleep until an epoch; STOP file ends it early. AWAY_NO_SLEEP=1 logs and returns (dry runs).
sleep_until() {
  local target=$1
  log "sleeping until $(date -d @"$target" '+%F %T') ($2)"
  [ "${AWAY_NO_SLEEP:-0}" = 1 ] && return 0
  local left
  while left=$(( target - $(date +%s) )); [ "$left" -gt 0 ]; do
    [ -f "$SCRATCH/STOP" ] && return 1
    sleep $(( left > 60 ? 60 : left ))
  done
}

# Queue lock, same protocol as iteration.md § 2: mkdir, retry every 10 s for 2 min; a lock
# folder last modified over 10 min ago is stale (owner file or not) and is removed. Still
# held after 2 min: return 1 and the caller logs it (the claims then lapse at LEASE_MIN).
queue_lock() {
  local tries=0 since
  until mkdir "$QUEUE_LOCK" 2>/dev/null; do
    since=$(date -r "$QUEUE_LOCK" +%s 2>/dev/null) || since=$(date +%s)   # removed meanwhile
    if [ $(( $(date +%s) - since )) -gt 600 ]; then
      rm -rf "$QUEUE_LOCK"; continue
    fi
    tries=$((tries + 1)); [ "$tries" -ge 12 ] && return 1
    sleep 10
  done
  echo "watchdog $(date '+%F %T')" > "$QUEUE_LOCK/owner"
}

# Single instance. mkdir is atomic, so two watchdogs cannot both win.
LOCK="$SCRATCH/watchdog.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then
    log "another watchdog is live (pid $(cat "$LOCK/pid")); exiting"
    exit 0
  fi
  log "clearing stale lock"
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || { log "lost the stale-lock race to another watchdog; exiting"; exit 0; }
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"; log "watchdog exiting"' EXIT

# Own worktree on away/<date>, never main and never the main checkout.
cd "$WORKTREE" || { log "refusing: WORKTREE $WORKTREE missing"; exit 1; }
BRANCH=$(git branch --show-current)
case "$BRANCH" in
  away/*) ;;
  *) log "refusing: $WORKTREE is on '$BRANCH', not away/<date>"; exit 1 ;;
esac
if [ "$(git rev-parse --git-dir)" = "$(git rev-parse --git-common-dir)" ]; then
  log "refusing: $WORKTREE is a main checkout, not a linked worktree"; exit 1
fi
[ -f "$QUEUE" ] || { log "refusing: no queue at $QUEUE"; exit 1; }

log "watchdog up (pid $$); branch $BRANCH; hard stop $(date -d @"$STOP_EPOCH" '+%F %T')"

iter=0 mode=normal sonnet_until=0 waited_lease=0 no_progress=0 fails=0 closed=0 reason=""
while :; do
  now=$(date +%s)
  [ -f "$SCRATCH/STOP" ] && { reason="STOP file"; break; }
  [ "$now" -ge "$STOP_EPOCH" ] && { reason="hard stop"; break; }
  if [ "$mode" = mechanical-only ] && [ "$now" -ge "$sonnet_until" ]; then
    mode=normal; log "Opus reset reached; back to normal mode"
  fi

  read -r claimable mech pending claimed done_n blocked next_lease < <(helper queue "$QUEUE" "$MAX_ATTEMPTS")
  phase=item
  if [ "$now" -ge $((STOP_EPOCH - RESERVE_MIN * 60)) ]; then
    phase=closeout; log "inside the ${RESERVE_MIN}-minute close-out reserve"
  elif [ "$claimable" -eq 0 ] && [ "$next_lease" -gt 0 ] && [ "$next_lease" != "$waited_lease" ] \
       && [ "$next_lease" -lt $((STOP_EPOCH - RESERVE_MIN * 60)) ]; then
    # A live claim nobody released (e.g. a watchdog restart mid-row): wait it out once, then retry the row.
    waited_lease=$next_lease
    sleep_until "$next_lease" "no claimable row; a live claim lapses then" || { reason="STOP file"; break; }
    continue
  elif [ "$claimable" -eq 0 ]; then
    phase=closeout; log "queue empty (done $done_n, blocked $blocked, claimed $claimed)"
  fi

  if [ "$phase" = item ] && [ "$mode" = mechanical-only ] && [ "$mech" -eq 0 ]; then
    target=$sonnet_until
    [ "$target" -ge "$STOP_EPOCH" ] && { reason="Opus limit past the hard stop, no mechanical rows"; break; }
    sleep_until "$target" "Opus limit, no mechanical-worker row" || { reason="STOP file"; break; }
    mode=normal; continue
  fi
  if [ "$phase" = item ]; then
    free_at=$(helper window "$COSTS" "$WINDOW_CAP_USD")
    if [ "$free_at" -gt 0 ]; then
      if [ "$free_at" -ge $((STOP_EPOCH - RESERVE_MIN * 60)) ]; then
        phase=closeout; log "quota reserve reached (\$$WINDOW_CAP_USD / 5 h); going to close-out"
      else
        sleep_until "$free_at" "quota reserve: \$$WINDOW_CAP_USD per 5 h spent" || { reason="STOP file"; break; }
        continue
      fi
    fi
  fi

  iter=$((iter + 1))
  tag=$(printf '%03d' "$iter")
  claim="iter-$tag"   # the exact claimed_by value the prompt hands over and release matches
  prompt="$SCRATCH/iter-$tag.prompt.md" out="$SCRATCH/iter-$tag.json" err="$SCRATCH/iter-$tag.err"
  helper render "$PROMPT_TEMPLATE" "$prompt" "WORKTREE=$WORKTREE" "BRANCH=$BRANCH" \
    "RUN_DIR=$RUN_DIR" "SCRATCH=$SCRATCH" "QUEUE_LOCK=$QUEUE_LOCK" "ITER=$tag" "TAG=$claim" \
    "PHASE=$phase" "MODEL_MODE=$mode" "STOP_AT=$STOP_AT" "LEASE_MIN=$LEASE_MIN" \
    "MAX_ATTEMPTS=$MAX_ATTEMPTS" "RESERVE_MIN=$RESERVE_MIN"

  # Model and effort come from the agent frontmatter: no --effort, and no cache-TTL flag
  # (the defaults stay; effort is raised per run only, for a stated reason).
  # Keep --disable-slash-commands off: it disables the Skill tool the orchestrator needs.
  args=(-p --agent away-orchestrator --max-turns "$MAX_TURNS" --output-format json
        --permission-mode bypassPermissions --max-budget-usd "$BUDGET_USD")
  [ "$mode" = mechanical-only ] && args+=(--model sonnet)   # the one override: Opus cap hit
  before=$(git hash-object "$QUEUE")
  log "iteration $iter starting: phase=$phase mode=$mode claimable=$claimable ($((STOP_EPOCH - now))s left)"
  log "claude: $CLAUDE"
  started=$(date +%s)
  "$CLAUDE" "${args[@]}" < "$prompt" > "$out" 2> "$err"
  rc=$?
  elapsed=$(($(date +%s) - started))
  changed=no; [ "$(git hash-object "$QUEUE")" != "$before" ] && changed=yes
  read -r kind fam reset cost ostatus turns < <(helper verdict "$out")
  # The process has exited, so any claim it still holds is dead: lapse it now, not after LEASE_MIN.
  # Not after a close-out: it claims nothing, and an edit then would stay uncommitted.
  # Not after a budget overrun: the row would be re-claimed at once and overrun again;
  # its live claim makes the next iteration take the next row until LEASE_MIN lapses it.
  # The claim note is logged after the iteration line below, so the log reads in order.
  claim_note=""
  if [ "$phase" = closeout ]; then
    :
  elif [ "$kind" = budget ]; then
    claim_note="budget overrun (\$$BUDGET_USD); $claim claims left to lapse at their lease"
  elif queue_lock; then
    released=$(helper release "$QUEUE" "$claim"); rm -rf "$QUEUE_LOCK"
    [ "${released:-0}" -gt 0 ] && claim_note="released $released claim(s) left by $claim"
  else
    claim_note="queue lock busy 2 min; $claim claims left to lapse"
  fi

  printf '%s %s\n' "$started" "$cost" >> "$COSTS"
  log "iteration $iter rc=$rc ${elapsed}s kind=$kind orch=$ostatus queue_changed=$changed cost=\$$cost turns=$turns (info)"
  [ -n "$claim_note" ] && log "$claim_note"
  [ -s "$err" ] && { head -c 400 "$err" | tr '\n' ' ' | sed 's/^/  stderr: /'; echo; } >> "$LOG"

  case "$kind" in ok|max_turns|limit) fails=0 ;; esac
  if [ "$phase" = closeout ] && { [ "$kind" = ok ] || [ "$kind" = max_turns ]; }; then
    closed=1; reason="close-out done"; break
  fi

  case "$kind" in
    ok|max_turns)
      if [ "$changed" = yes ]; then no_progress=0; BACKOFF=300; continue; fi
      no_progress=$((no_progress + 1))
      log "no queue change ($no_progress/$NO_PROGRESS_MAX)"
      [ "$no_progress" -ge "$NO_PROGRESS_MAX" ] && { reason="$NO_PROGRESS_MAX iterations without a queue change"; break; }
      continue ;;
    limit)
      [ "$changed" = yes ] && no_progress=0
      [ "$reset" -gt 0 ] || reset=$(( $(date +%s) + BACKOFF_MAX ))
      if [ "$fam" = opus ] && [ "$mode" = normal ] && [ "$phase" = item ]; then
        read -r claimable mech _ < <(helper queue "$QUEUE" "$MAX_ATTEMPTS")
        if [ "$mech" -gt 0 ]; then
          mode=mechanical-only sonnet_until=$reset
          log "Opus limit; $mech mechanical-worker row(s) continue on Sonnet until $(date -d @"$reset" '+%F %T')"
          continue
        fi
      fi
      [ "$reset" -ge "$STOP_EPOCH" ] && { reason="$fam limit resets after the hard stop"; break; }
      sleep_until "$reset" "$fam limit, stated reset" || { reason="STOP file"; break; }
      [ "$(date +%s)" -ge "$sonnet_until" ] && mode=normal   # a Sonnet reset does not lift the Opus cap
      continue ;;
    budget)
      # Per-process cap, so a sleep frees nothing: no backoff. The row was skipped by
      # keeping its claim (above); a run of overruns still counts toward FAIL_MAX.
      fails=$((fails + 1))
      [ "$fails" -ge "$FAIL_MAX" ] && { reason="$FAIL_MAX failed iterations in a row (budget)"; break; }
      continue ;;
    *)
      fails=$((fails + 1))
      [ "$fails" -ge "$FAIL_MAX" ] && { reason="$FAIL_MAX failed iterations in a row ($kind)"; break; }
      if [ $(( $(date +%s) + BACKOFF )) -ge "$STOP_EPOCH" ]; then
        reason="backoff ${BACKOFF}s would overrun the hard stop"; break
      fi
      sleep_until $(( $(date +%s) + BACKOFF )) "$kind, backoff" || { reason="STOP file"; break; }
      BACKOFF=$((BACKOFF * 2)); [ "$BACKOFF" -gt "$BACKOFF_MAX" ] && BACKOFF=$BACKOFF_MAX
      continue ;;
  esac
done

[ "$closed" = 1 ] || helper report "$RUN_DIR" "$reason" "$LOG" "$iter"
# Later task firings must not start a second run: remove STOP by hand to resume.
[ -f "$SCRATCH/STOP" ] || echo "$reason" > "$SCRATCH/STOP"
log "watchdog done after $iter iterations: $reason"
