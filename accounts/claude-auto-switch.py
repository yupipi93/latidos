#!/usr/bin/env python3
"""Switch the default Claude account by itself when it runs out of quota, to the one with the most room left (smart hierarchy).

Run by the LaunchAgent local.claude-auto-switch every 60 s. Design: accounts/README.md
and the audit notes quoted below (A-items of the backlog it was built with).

  trigger  the default account (~/.claude-active) is out of quota: a live interactive
           session of it just ended on a usage-limit error written by its current
           process, whose window has not reset yet; or the quota source's numbers
           (fresh, not older than QUOTA_MAX_AGE) mark its 5-hour or weekly window full
           or locked. For 30 minutes after the owner switches by hand, only a real limit
           error counts;
  choose   among the accounts with fresh numbers and room left (10 % of the 5-hour
           window, 5 % of the week), not recently found spent, the best score: weekly
           room per day until its reset, scaled by the 5-hour room, with the priority ORDER
           (the accounts config's order, set from Latidos) as a small bonus; and
           when the default is not spent but another account scores 1.5x more (20 % of its
           5-hour window, on a forced read of the last 2 minutes, at most every 30 min),
           move to it (the account in use is judged on its week only; a target needs 15 %
           of its week; weekly room per day with a 6 h floor; a candidate's 5-hour window
           counts back in its last hour). While the default bills paid extra usage, any
           account with free room is taken without the 30-min throttle or the 15 % rule,
           and a manual choice holds only for the 30-min grace; after a
           manual switch away from an account with
           room, not until the next automatic one; after leaving a spent account by
           hand, only after the 30-min grace and only to it or above; not to an account
           that sent it down again within an hour of going up (6 h);
  switch   login check, Cursor's setting, shared pool, T3 Code, and ~/.claude-active
           last, so a failed step leaves the default untouched; then move the Cursor tabs
           that are idle, run no background work of their own and either stopped on the
           usage limit or have been idle for an hour (scope "safe-quiet"; a tab that
           may be in use is never closed): the cut ones get "continúa" with their
           stopped subagents named. While Cursor tabs are on another account after an
           automatic switch (and no manual one since), every tick moves those that
           became so; paused while the account switched to is spent itself.

No room anywhere: no switch, one notice per situation (reset times compared to the nearest
10 minutes). At most one automatic switch per
COOLDOWN; an account a switch moved away from on a limit error is skipped until its reset.

Usage:
  accounts/claude-auto-switch.py                 # one tick
  accounts/claude-auto-switch.py --status        # room per account, the pick, state
  accounts/claude-auto-switch.py --dry-run       # tick without changing anything
  accounts/claude-auto-switch.py --dry-run --assume-exhausted   # what a switch now would do
  accounts/claude-auto-switch.py --disable | --enable
  accounts/claude-auto-switch.py --install | --uninstall

Stdlib only. Reads transcripts and session records; signals nothing itself (the Cursor
move goes through cursor-claude-account.py, which ends only the exact extension PIDs).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent
# launchd starts with a bare PATH; the claude CLI the switch steps call lives in ~/.local/bin
# (audit of release 1: every switch failed under launchd).
os.environ["PATH"] = os.pathsep.join(
    [str(HOME / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", os.environ.get("PATH", "/usr/bin:/bin")])
# claude reads the login from the Keychain by user: without USER it reports "logged in as
# nobody" (checked with an empty environment), so set it when the launcher did not.
import pwd  # noqa: E402
for _var in ("USER", "LOGNAME"):
    os.environ.setdefault(_var, pwd.getpwuid(os.getuid()).pw_name)

sys.path.insert(0, str(HERE))
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)

CFG = accounts_config.CONFIG
ACCOUNTS = {a.tag: a.dir for a in CFG.accounts}
DEFAULT_TAG = CFG.default_tag
ACTIVE = CFG.active_file
CURSOR_TOOL = HERE / "cursor-claude-account.py"
T3_TOOL = HERE / "t3-claude-account.py"
POOL_TOOL = HERE / "claude-shared-pool.py"
# The quota source (a local server answering /api/claude-quota, and the cache it writes):
# `quota_api` and `quota_cache` in the accounts config.
QUOTA_FILE = CFG.quota_cache
QUOTA_API = os.environ.get("CLAUDE_AUTO_SWITCH_QUOTA_API", CFG.quota_api)
STATE_DIR = HOME / ".local/state/claude-auto-switch"
STATE = STATE_DIR / "state.json"
LOCK = STATE_DIR / "lock"
OFF = STATE_DIR / "off"
LOG = HOME / "Library/Logs/claude-auto-switch.log"
LABEL = "local.claude-auto-switch"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
INTERVAL_SECONDS = 60

MIN_SESSION_ROOM = float(os.environ.get("CLAUDE_AUTO_SWITCH_MIN_SESSION", "10"))
MIN_WEEKLY_ROOM = float(os.environ.get("CLAUDE_AUTO_SWITCH_MIN_WEEKLY", "5"))
# Priority: the accounts config's `order`, overridden by the page's order file or the env.
# Tags are stripped ("a, b" works); an account left out of the list goes last, so it is
# never excluded from switching (audit of auto switch release 19).
def parse_order(raw: str) -> tuple[list[str], list[str]]:
    """(order, unknown tags) from CLAUDE_AUTO_SWITCH_ORDER or the order file."""
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    known = [t for t in tags if t in ACCOUNTS]
    return list(dict.fromkeys(known + list(CFG.order))), [t for t in tags if t not in ACCOUNTS]


def read_order_file(path: Path) -> str | None:
    """The priority saved by the Latidos page (`{"order": ["work", "home"]}`), or None."""
    try:
        order = json.loads(path.read_text()).get("order")
    except (OSError, ValueError, AttributeError):
        return None
    return ",".join(str(t) for t in order) if isinstance(order, list) and order else None


def _order_file() -> Path:
    """Where Latidos keeps its config: the same rules as its config_dir (audit of 43)."""
    if os.environ.get("CLAUDE_ACCOUNTS_ORDER_FILE"):
        return Path(os.path.expanduser(os.environ["CLAUDE_ACCOUNTS_ORDER_FILE"]))
    base = os.environ.get("LATIDOS_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config"), "latidos")
    return Path(os.path.expanduser(base)) / "claude-order.json"


# The priority the owner sets from the Latidos cards (menu or drag); the env var still wins.
ORDER_FILE = _order_file()
ORDER, ORDER_UNKNOWN = parse_order(os.environ.get("CLAUDE_AUTO_SWITCH_ORDER") or read_order_file(ORDER_FILE) or ",".join(CFG.order))
UPGRADE_MIN_SESSION = float(os.environ.get("CLAUDE_AUTO_SWITCH_UPGRADE_MIN_SESSION", "20"))
# Smart hierarchy (the owner, 2026-11-16): the weekly room left weighs much more than the
# priority. Each account's score is its weekly room per day until that window resets
# (room about to expire is worth using first), times its 5-hour room factor, times a small
# priority bonus: PRIORITY_BIAS per place above the last one (lt 1.30, mt 1.15, na 1.00).
PRIORITY_BIAS = float(os.environ.get("CLAUDE_AUTO_SWITCH_PRIORITY_BIAS", "0.15"))
# An account with room is left for another only when the other scores this many times more
# (no flapping between two accounts with similar room)...
REBALANCE_RATIO = float(os.environ.get("CLAUDE_AUTO_SWITCH_REBALANCE_RATIO", "1.5"))
# ...and not sooner than this after the last automatic switch (each one moves quiet tabs
# and loses their prompt cache).
REBALANCE_EVERY = dt.timedelta(minutes=float(os.environ.get("CLAUDE_AUTO_SWITCH_REBALANCE_MINUTES", "30")))
SESSION_FULL_FACTOR = 40.0   # 5-hour room at or above this % counts in full; below, in proportion
PAYING_SOON = dt.timedelta(minutes=15)  # extra usage this close to a window's refill is waited out
SESSION_SOON_HOURS = 1.0     # within its last hour, a 5-hour window's used part counts back (see score)
# Weekly room that expires within this counts as if spread over it, not over a whole day:
# room about to be lost is worth using first (audit of 43: a day floor left it unused).
DAYS_FLOOR = 0.25
# A rebalance needs at least this much of the target's week left: switching (quiet tabs
# moved, prompt cache lost) only to scrape a few expiring percent is not worth it.
REBALANCE_MIN_WEEKLY = float(os.environ.get("CLAUDE_AUTO_SWITCH_REBALANCE_MIN_WEEKLY", "15"))
LIMIT_RECENT = dt.timedelta(minutes=15)
COOLDOWN = dt.timedelta(minutes=5)
_QUIET = float(os.environ.get("CLAUDE_MOVE_QUIET_MINUTES", "60"))  # read by cursor-claude-account.py
QUIET_TEXT = "una hora" if _QUIET == 60 else f"{_QUIET:.0f} min"
BOUNCE = dt.timedelta(hours=1)               # a switch down this soon after going up into that account...
NO_UPGRADE = dt.timedelta(hours=6)           # ...blocks going up to it again for this long
MANUAL_GRACE = dt.timedelta(minutes=30)
QUOTA_MAX_AGE = dt.timedelta(minutes=30)     # older numbers are not trusted for a decision
FRESH_EVERY = dt.timedelta(minutes=10)       # routine forced read of the quota source at most this often
TRIGGERED_EVERY = dt.timedelta(minutes=1)    # when the account looks spent
CONFIRM_WITHIN = dt.timedelta(minutes=2)     # a forced read this recent confirms the counter
FAILED_SKIP = dt.timedelta(hours=1)          # a target whose switch failed is skipped this long
FAIL_MEMORY = dt.timedelta(days=2)           # failures older than this are forgotten
SPENT_FALLBACK = dt.timedelta(hours=1)

_spec = importlib.util.spec_from_file_location("claude_quota_resume", HERE / "claude-quota-resume.py")
cqr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cqr)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_time(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def current_account() -> str:
    try:
        tag = ACTIVE.read_text().strip()
    except OSError:
        return DEFAULT_TAG
    return tag if tag in ACCOUNTS else DEFAULT_TAG


# ---- Quota -----------------------------------------------------------------------------

def normalize(data) -> dict:
    """{tag: report} from either the quota source's API ({accounts: [...]}) or its cache file
    ({tag: report}); anything else is empty (audit of release 1: the API shape was dropped)."""
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        # A tag that is not a string (malformed file) is skipped, never a crash (audit of 47).
        return {a["tag"]: a for a in data["accounts"]
                if isinstance(a, dict) and isinstance(a.get("tag"), str) and a["tag"] in ACCOUNTS}
    if isinstance(data, dict):
        return {t: r for t, r in data.items() if isinstance(t, str) and t in ACCOUNTS and isinstance(r, dict)}
    return {}


def read_cache() -> dict:
    try:
        return normalize(json.loads(QUOTA_FILE.read_text()))
    except (OSError, ValueError):
        return {}


def read_fresh() -> dict:
    """A forced read through the quota source's API (it asks Anthropic for every account)."""
    try:
        req = urllib.request.Request(QUOTA_API, headers={**CFG.quota_headers, "accept": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as resp:
            return normalize(json.loads(resp.read().decode()))
    except Exception:  # noqa: BLE001  any failed read (incl. http.client.IncompleteRead) = no read
        return {}


def is_fresh(report, at: dt.datetime) -> bool:
    """Numbers recent enough to decide on: live (not a stale copy) and fetched lately."""
    if not isinstance(report, dict) or report.get("source") == "stale":
        return False
    fetched = parse_time(report.get("fetched_at"))
    return bool(fetched) and at - fetched <= QUOTA_MAX_AGE


def room(win, at: dt.datetime) -> float | None:
    """Percent left in a window: 100 once past its reset, 0 when the server says it is full
    or locked, else 100 - percent (a shown 100 % that is not "full" still has a little)."""
    if not isinstance(win, dict):
        return None
    reset = parse_time(win.get("resets_at"))
    if reset and reset <= at:
        return 100.0
    if win.get("full") or win.get("locked"):
        return 0.0
    try:
        return max(0.0, 100.0 - float(win.get("percent")))
    except (TypeError, ValueError):
        return None


def rooms(report, at: dt.datetime) -> tuple[float | None, float | None]:
    if not isinstance(report, dict):
        return None, None
    return room(report.get("session"), at), room(report.get("weekly"), at)


def spent_windows(report, at: dt.datetime) -> list[str]:
    """Windows the server marks full or locked and not reset yet (no extra usage)."""
    if not isinstance(report, dict) or report.get("extra_usage"):
        return []
    out = []
    for key in ("session", "weekly"):
        win = report.get(key)
        if isinstance(win, dict) and (win.get("full") or win.get("locked")):
            reset = parse_time(win.get("resets_at"))
            if not reset or reset > at:
                out.append(key)
    return out


def has_room(report, at: dt.datetime) -> bool:
    """Fresh numbers and free room above the thresholds in both windows."""
    if not is_fresh(report, at):
        return False
    s, w = rooms(report, at)
    return s is not None and w is not None and s >= MIN_SESSION_ROOM and w >= MIN_WEEKLY_ROOM


def usable(report, at: dt.datetime) -> bool:
    """Room, or else paid extra usage that covers a spent limit (a last resort: it costs
    money, audit of auto switch release 2)."""
    return has_room(report, at) or (is_fresh(report, at) and bool(report.get("extra_usage")))


def tight_room(report, at: dt.datetime) -> float:
    s, w = rooms(report, at)
    return min(s if s is not None else 0.0, w if w is not None else 0.0)


def priority_weight(tag: str) -> float:
    """1 for the last account in ORDER, + PRIORITY_BIAS per place above it."""
    rank = ORDER.index(tag) if tag in ORDER else len(ORDER) - 1
    return 1.0 + PRIORITY_BIAS * (len(ORDER) - 1 - rank)


def score(tag: str, report, at: dt.datetime, session: bool = True) -> float | None:
    """How good an account is to work on now; None without fresh numbers.

    weekly room / days until the weekly reset (at least DAYS_FLOOR: room that expires in
    a few hours weighs a lot, it is lost if not used), times min(1, 5-hour room /
    SESSION_FULL_FACTOR) (in its last SESSION_SOON_HOURS the used part counts back in
    proportion to how near its reset is: a window that refills in minutes is nearly full; `session=False` leaves the 5-hour
    window out, for the account in use, whose spent window has its own trigger), times the
    priority weight. With the same reset, 58 % left beats 15 % left whatever the
    priority; 15 % that expires tonight beats 58 % that lasts six more days."""
    if not is_fresh(report, at):
        return None
    s, w = rooms(report, at)
    if s is None or w is None:
        return None
    reset = parse_time((report.get("weekly") or {}).get("resets_at"))
    days = max(DAYS_FLOOR, (reset - at).total_seconds() / 86400) if reset and reset > at else 7.0
    if not session:
        return (w / days) * priority_weight(tag)
    s_reset = parse_time((report.get("session") or {}).get("resets_at"))
    if s_reset and s_reset > at:
        left = (s_reset - at).total_seconds() / 3600
        s += (100.0 - s) * max(0.0, 1.0 - left / SESSION_SOON_HOURS)
    return (w / days) * min(1.0, s / SESSION_FULL_FACTOR) * priority_weight(tag)


def paying(report, at: dt.datetime) -> bool:
    """On paid extra usage now: a window full or locked, not reset yet, with extra usage on
    (spent_windows ignores those, so only a rebalance can move off it)."""
    if not isinstance(report, dict) or not report.get("extra_usage") or not is_fresh(report, at):
        return False
    for key in ("session", "weekly"):
        win = report.get(key)
        if isinstance(win, dict) and (win.get("full") or win.get("locked")):
            reset = parse_time(win.get("resets_at"))
            # A window that refills within PAYING_SOON is waited out: two switches and two
            # lost prompt caches cost more than a few minutes of extra usage (audit of 45).
            if not reset or reset > at + PAYING_SOON:
                return True
    return False


def current_score(tag: str, report, at: dt.datetime) -> float | None:
    """The score the account in use is compared with: its week only, and 0 while it bills
    extra usage, so any account with free room beats it (paid usage is the last resort,
    A-03; audit of 44)."""
    if paying(report, at):
        return 0.0
    # A full week that refills within PAYING_SOON is waited out like a 5-hour window (the
    # 5-hour one is ignored here anyway): scored as just reset, not as 0 (audit of 47: it
    # switched away and came back 30 minutes later).
    weekly = report.get("weekly") if isinstance(report, dict) else None
    if isinstance(weekly, dict) and (weekly.get("full") or weekly.get("locked")) and is_fresh(report, at):
        reset = parse_time(weekly.get("resets_at"))
        if reset and at < reset <= at + PAYING_SOON:
            return (100.0 / 7.0) * priority_weight(tag)
    return score(tag, report, at, session=False)


def ranked(quota: dict, at: dt.datetime, tags) -> list[str]:
    """Tags by score, best first; ties keep the priority order."""
    return sorted(tags, key=lambda t: (-(score(t, quota.get(t), at) or 0.0),
                                       ORDER.index(t) if t in ORDER else len(ORDER)))


def pick(quota: dict, current: str, at: dt.datetime, skip: dict | None = None) -> str | None:
    """The account to move to, or None: among those with fresh numbers and room, the best
    score (weekly room first, priority as a small bonus: the owner, 2026-11-16); paid extra
    usage only as the last resort, in priority order. `skip`: {tag: until} of accounts left
    on a limit error or whose switch failed, skipped until then."""
    skip = skip or {}
    def allowed(t: str) -> bool:
        return t != current and not ((parse_time(skip.get(t)) or at) > at)
    roomy = [t for t in ORDER if allowed(t) and has_room(quota.get(t), at)]
    if roomy:
        return ranked(quota, at, roomy)[0]
    for t in ORDER:
        if allowed(t) and usable(quota.get(t), at):
            return t
    return None


def left_spent(state: dict, prev: str, at: dt.datetime) -> bool:
    """The account left by hand was spent (or billing extra usage): leaving it is not a
    choice to stay away from it (audit of auto switch release 21)."""
    sc_prev = state.get("spent_confirmed") or {}
    return bool((parse_time((state.get("spent") or {}).get(prev)) or at) > at
                or (sc_prev.get("tag") == prev and (parse_time(sc_prev.get("until")) or at) > at)
                or spent_windows(read_cache().get(prev), at) or paying(read_cache().get(prev), at)
                or fresh_limit_errors(prev, at) or recent_limit_error(prev, at))


def record_manual(state: dict, prev: str, tag: str, at: dt.datetime) -> None:
    """A switch made by hand outside a tick (the Latidos control): recorded as the tick
    would see it within the minute, whatever time the tab move takes afterwards (audit of
    45: a move longer than 5 minutes made it look like a deliberate choice)."""
    state.update(manual_at=at.isoformat(), manual_from=prev, manual_left_spent=left_spent(state, prev, at),
                 seen=tag, seen_at=at.isoformat())


def upgrade_target(quota: dict, current: str, at: dt.datetime, skip: dict) -> str | None:
    """Rebalance: another account worth moving to while the current one still has room.
    It needs its 5-hour window at least UPGRADE_MIN_SESSION (a little more than the
    switch-down threshold, so it does not bounce at the edge) and a score REBALANCE_RATIO
    times the current one's. Higher or lower in the priority, it does not matter: the
    priority is already inside the score."""
    # The account in use is judged on its week only: its 5-hour window running low is not a
    # reason to leave it (spent, it switches anyway; audit of 43: leaving it just before
    # its window refilled meant coming back 30 minutes later).
    if paying(quota.get(current), at):
        # Billing extra usage: any account with free room, as a switch down would pick
        # (A-03: paid usage is the last resort; audit of 45).
        free = [t for t in ORDER if t != current and not (parse_time(skip.get(t)) or at) > at
                and has_room(quota.get(t), at)]
        return ranked(quota, at, free)[0] if free else None
    mine = current_score(current, quota.get(current), at)
    if mine is None:
        return None
    for t in ranked(quota, at, [t for t in ORDER if t != current]):
        if (parse_time(skip.get(t)) or at) > at or not is_fresh(quota.get(t), at):
            continue
        s, w = rooms(quota.get(t), at)
        if s is None or w is None or s < UPGRADE_MIN_SESSION or w < max(MIN_WEEKLY_ROOM, REBALANCE_MIN_WEEKLY):
            continue
        return t if (score(t, quota.get(t), at) or 0.0) >= REBALANCE_RATIO * mine and (score(t, quota.get(t), at) or 0) > 0 else None
    return None


def upgrade_skip(state: dict) -> dict:
    """Accounts not to go up to now: failed, left on a limit, or bounced (tag -> until)."""
    return {**(state.get("failed") or {}), **(state.get("spent") or {}), **(state.get("no_upgrade") or {})}


def just_read(report, at: dt.datetime) -> bool:
    """Numbers from a forced read of the last CONFIRM_WITHIN."""
    when = parse_time((report or {}).get("fetched_at")) if is_fresh(report, at) else None
    return bool(when and at - when <= CONFIRM_WITHIN)


def reset_minute(report, key: str) -> str:
    """A window's reset time to the minute (the API's sub-second part changes on each read)."""
    win = report.get(key) if isinstance(report, dict) else None
    t = parse_time(win.get("resets_at")) if isinstance(win, dict) else None
    # Nearest 10 minutes: the API gives the same reset on both sides of a minute boundary
    # (22:29:59.98 and 22:30:00.41 in three reads, audit of auto switch release 3).
    return str(round(t.timestamp() / 600)) if t else "-"


def usable_again_at(report, at: dt.datetime) -> dt.datetime | None:
    """When an account below the thresholds gets room again: the latest reset among its
    short windows."""
    if not isinstance(report, dict):
        return None
    s, w = rooms(report, at)
    times = []
    for key, r, need in (("session", s, MIN_SESSION_ROOM), ("weekly", w, MIN_WEEKLY_ROOM)):
        if r is not None and r < need:
            times.append(parse_time((report.get(key) or {}).get("resets_at")))
    times = [t for t in times if t]
    return max(times) if times else None


# ---- Limit errors ----------------------------------------------------------------------

def live_sessions(tag: str) -> list[dict]:
    """Live interactive Claude processes of one account: {pid, session, started}."""
    out = []
    for meta in (ACCOUNTS[tag] / "sessions").glob("*.json"):
        try:
            info = json.loads(meta.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict) or info.get("kind") != "interactive":
            continue
        pid, sid = info.get("pid"), info.get("sessionId")
        if not pid or not sid or not cqr.pid_alive(int(pid)):
            continue
        started = dt.datetime.fromtimestamp(int(info.get("startedAt") or 0) / 1000, dt.timezone.utc)
        out.append({"pid": int(pid), "session": sid, "started": started, "entrypoint": info.get("entrypoint")})
    return out


CUT_MOVES = HOME / ".local/state/claude-auto-switch/cut-moves.json"  # written by cursor-claude-account.py


def recent_limit_error(tag: str, at: dt.datetime) -> bool:
    """A tab of `tag` that had stopped on the usage limit in the last LIMIT_RECENT was moved
    (by a shell switch command or by this tool): the account was spent when the owner left it. Per
    account, unlike a scan of the shared transcripts, which cannot tell an error of lt from
    one of mt (audit of auto switch release 23)."""
    try:
        data = json.loads(CUT_MOVES.read_text())
    except (OSError, ValueError):
        return False
    entry = data.get(tag) if isinstance(data, dict) else None
    if isinstance(entry, dict):
        when, text = parse_time(entry.get("at")), str(entry.get("text") or "")
    else:  # a bare time (release 24)
        when, text = parse_time(entry), ""
    if not (when and dt.timedelta(0) <= at - when <= LIMIT_RECENT):
        return False
    # A window the error names as already reset does not make the account spent (audit
    # of release 25).
    reset = cqr.parse_reset(text, when.astimezone(cqr.TZ)) if text else None
    return not (reset and reset.astimezone(dt.timezone.utc) <= at)


def cca_record_cut(newest: dict[str, dict]) -> None:
    """cursor-claude-account.record_cut, loaded on demand (same file, same lock)."""
    spec = importlib.util.spec_from_file_location("cursor_claude_account", CURSOR_TOOL)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        mod.CUT_MOVES = CUT_MOVES
        mod.record_cut(newest)
    except Exception as exc:  # never break a tick over the record
        log(f"cut record failed: {exc}")


def tabs_elsewhere(current: str) -> int:
    """Live Cursor conversations on another account than `current`: the only ones a
    follow-up move can take (a terminal claude cannot be moved)."""
    return sum(1 for t in ACCOUNTS if t != current
               for x in live_sessions(t) if x.get("entrypoint") == "claude-vscode")


def fresh_limit_errors(tag: str, at: dt.datetime) -> list[dict]:
    """Limit errors that say this account is out right now: last turn of a live session,
    written by its current process (a tab moved here that still shows an older error does
    not count), in the last LIMIT_RECENT, and the window it names not reset yet (audit of
    release 1). [{session, reset}]"""
    hits = []
    for s in live_sessions(tag):
        paths = list(cqr.PROJECTS.glob(f"*/{s['session']}.jsonl"))
        if not paths:
            continue
        try:
            convo = cqr.conversation(cqr.tail_entries(paths[0]))
        except OSError:
            continue
        last = cqr.last_turn(convo)
        if not last or not cqr.is_limit(last):
            continue
        when = parse_time(last.get("timestamp"))
        if not when or when < s["started"] or at - when > LIMIT_RECENT:
            continue
        reset = cqr.parse_reset(cqr.message_text(last), when.astimezone(cqr.TZ))
        reset = reset.astimezone(dt.timezone.utc) if reset else None
        if reset and reset <= at:
            continue
        hits.append({"session": s["session"], "reset": reset, "when": when,
                     "text": cqr.message_text(last)[:300]})
    return hits


# ---- State, notices --------------------------------------------------------------------

def prune_failures(state: dict, at: dt.datetime) -> bool:
    """Forget failures older than FAIL_MEMORY, and counts or notices with no failure time
    (state of release 5). True when something was dropped."""
    when = state.get("last_fail_at") if isinstance(state.get("last_fail_at"), dict) else {}
    keep = {t for t, w in when.items() if (parse_time(w) or at) >= at - FAIL_MEMORY}
    changed = False
    for k in ("last_fail_at", "fail_counts", "fail_notices"):
        d = state.get(k)
        if isinstance(d, dict):
            for t in [t for t in d if t.split(":")[0] not in keep]:
                d.pop(t)
                changed = True
    return changed


def load_state() -> dict:
    try:
        data = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(f".{os.getpid()}.tmp")  # never shared by two writers
    tmp.write_text(json.dumps(state, indent=1))
    os.replace(tmp, STATE)


def notify(title: str, body: str, level: str = "info") -> None:
    """The configured notifier (`notify` in the accounts config); levels: info | ok | warn |
    crit. Never lets a slow or failing notice break the tick (audit of auto switch release
    7: a hung notifier lost the failure state of a switch): accounts_config times it out."""
    if CFG.notify_argv and not CFG.notify(level, title, body):
        log(f"notice not sent: {title}")


def describe(tag: str, quota: dict, at: dt.datetime) -> str:
    s, w = rooms(quota.get(tag), at)
    fmt = lambda v: "?" if v is None else f"{100 - v:.0f}%"
    old = "" if is_fresh(quota.get(tag), at) else ", datos viejos"
    return f"{tag} (5 h {fmt(s)}, semana {fmt(w)}{old})"


# ---- The switch ------------------------------------------------------------------------

def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def switch_to(tag: str) -> tuple[bool, str]:
    """The steps of a shell switch command without a terminal. ~/.claude-active is
    written last: a failed step leaves the default as it was, so the next tick retries the
    whole switch (audit of release 1)."""
    if not ACCOUNTS[tag].is_dir():
        return False, f"{ACCOUNTS[tag]} does not exist"
    code, out = run([sys.executable, str(CURSOR_TOOL), tag, "--verify"], timeout=60)
    if code != 0:
        return False, f"login check failed: {out[-300:]}"
    code, out = run([sys.executable, str(CURSOR_TOOL), tag, "--quiet", "--no-verify"], timeout=60)
    if code != 0:
        return False, f"Cursor not switched: {out[-300:]}"
    notes = []
    code, out = run([sys.executable, str(POOL_TOOL), "--fix", "--quiet"])
    if code != 0:
        notes.append(f"pool: {out[-200:]}")
    if T3_TOOL.exists():
        code, out = run([sys.executable, str(T3_TOOL), tag, "--quiet"], timeout=60)
        if code != 0:
            notes.append(f"T3 Code not switched: {out[-200:]}")
    ACTIVE.write_text(tag + "\n")
    return True, "; ".join(notes)


def move_safe_tabs(scope: str = "safe-quiet") -> str:
    """Move the Cursor tabs still on another account that are idle and run no background
    work of their own (the cut ones get "continúa"). `safe-quiet` is used on every switch
    and follow-up move: only those stopped on the usage limit or idle for an hour, so a
    tab in use is never closed. Accepted: a tab that just failed on the limit is moved
    within the minute, while the owner may be looking at it; its message is sent again by
    the continue."""
    return run([sys.executable, str(CURSOR_TOOL), "--move-open", "--scope", scope, "--summary"], timeout=1200)[1]


def move_summary(out: str) -> list[dict] | None:
    """The per-tab outcomes of a --move-open --summary run; None when the run did not finish
    (crash, timeout): then nobody knows which tabs were left without their chat."""
    for line in reversed(out.split("\n")):  # only real newlines (not U+2028 & co.)
        if line.startswith("MOVE-SUMMARY "):
            try:
                data = json.loads(line[len("MOVE-SUMMARY "):])
            except ValueError:
                return None
            return data if isinstance(data, list) else None
    return None


# Outcomes of a cut tab that did not carry on by itself: it needs "continúa" typed.
STUCK = ("not_reopened", "no_process", "not_taken", "not_sent")


# ---- Tick ------------------------------------------------------------------------------

def tick(dry_run: bool, assume_exhausted: bool) -> int:
    at = now()
    state = load_state()
    current = current_account()
    last = parse_time(state.get("last_switch_at"))
    last_try = parse_time(state.get("last_attempt_at")) or last

    # A default changed by hand (a shell switch command): for a while, only a real limit error
    # moves it again (audit of release 1).
    if state.get("seen") and state.get("seen") != current:
        # An automatic switch records the new default as seen, so a change here is manual.
        state["manual_at"] = at.isoformat()
        # Leaving a spent account by hand is not a choice to stay below it: going back up
        # still applies once it has room (audit of auto switch release 21).
        prev = state["seen"]
        sc_prev = state.get("spent_confirmed") or {}
        seen_at = parse_time(state.get("seen_at"))
        state["manual_from"] = prev
        # Unknown when it happened (the tool was disabled or not running): treat it as a
        # choice, the safe side.
        state["manual_left_spent"] = bool(seen_at and at - seen_at <= dt.timedelta(minutes=5)
                                          and left_spent(state, prev, at))
    if not dry_run:
        # Saved every tick: seen_at tells a manual switch seen within a minute from one made
        # while the tool was not running.
        state["seen"] = current
        state["seen_at"] = at.isoformat()
        save_state(state)
    # State of releases 19 / 20 has no last_kind: an automatic switch to a higher account
    # was an upgrade (audit of auto switch release 21).
    # A switch down to a higher account (release 19 picked the first with room) left the
    # account it came from in `spent` (audit of release 22).
    if (state.get("last_from") in ORDER and state.get("last_to") in ORDER and "last_kind" not in state
            and ORDER.index(state["last_to"]) < ORDER.index(state["last_from"])
            and state["last_from"] not in (state.get("spent") or {})):
        state["last_kind"] = "up"
    manual = parse_time(state.get("manual_at"))
    manual_recent = bool(manual and at - manual < MANUAL_GRACE)

    # A failure two days old is history: its count and its notice go, so a new breakage
    # weeks later is notified again and starts at a 1-hour skip (audit of release 5).
    if prune_failures(state, at) and not dry_run:
        save_state(state)

    # Tabs left behind by a recent automatic switch: move them as they become safe.
    sc = state.get("spent_confirmed") or {}
    current_spent = bool(state.get("no_target")) or (
        sc.get("tag") == current and (parse_time(sc.get("until")) or at) > at)
    # No time limit (audit of auto switch release 19: a tab cut after two hours, or one left
    # busy when going back up, stayed on the other account for good), but only while some
    # Cursor tab is on another account.
    if (not dry_run and last and state.get("last_to") == current
            and not (manual and manual > last) and not current_spent and tabs_elsewhere(current)):
        # Not into an account that is spent itself: the moved chats would only hit its
        # limit again (audit of auto switch release 9).
        # safe-quiet after every switch: tabs stopped on the limit, and the ones idle for an
        # hour (audit of release 22: tabs an upgrade left on a lower account stayed there
        # after a later switch down, they never stop on a limit with room).
        out = move_safe_tabs("safe-quiet")
        # A move can take minutes under the lock: the default was still watched meanwhile
        # (audit of release 23: a stale seen_at made a manual move a "choice").
        state["seen_at"] = now().isoformat()
        save_state(state)
        summary = move_summary(out)
        if summary is None or any(e.get("outcome") not in ("left",) for e in summary):
            log(f"follow-up move to {current}: {' | '.join(out.splitlines())[:8000]}")
        # Since release 28 this is the usual path for tabs in use at a switch down: a cut
        # tab that could not carry on by itself is notified (audit of release 28).
        stuck = [e for e in (summary or []) if e.get("cut") and e.get("outcome") in STUCK]
        # Once per tab and switch episode: the same tab stuck again after a later switch is
        # told again (audit of release 29: keyed by session alone it went silent for good).
        episode = state.get("last_switch_at") or ""
        told = list(state.get("stuck_told") or [])
        new = [e for e in stuck if f"{e.get('session')}@{episode}" not in told]
        if new:
            state["stuck_told"] = (told + [f"{e.get('session')}@{episode}" for e in new])[-50:]  # oldest out
            save_state(state)
            notify("Claude: pestaña sin continuar",
                   f"Movida a {current} pero no ha seguido sola, escribe continúa en ella: "
                   + "; ".join(str(e.get("name"))[:40] for e in new) + ".", "warn")

        if current_account() != current:
            return 0  # switched by hand during the move: the next tick sees it as manual
    quota = read_cache()
    # Keep the numbers of the default account fresh: one forced read every FRESH_EVERY at
    # most, only when they are getting old (audit of release 1: the cache only follows
    # page reads).
    last_fresh = parse_time(state.get("last_fresh_at"))

    def refresh(every: dt.timedelta) -> None:
        nonlocal quota, last_fresh
        if not (last_fresh and at - last_fresh < every):
            got = read_fresh()
            last_fresh = at
            if not dry_run:
                state["last_fresh_at"] = at.isoformat()
            # Only a read that brought the current account live confirms anything (audit
            # of auto switch release 3: a failed or stale read used to count).
            if got:
                quota = got
                if is_fresh(got.get(current), at) and at - (parse_time(got[current].get("fetched_at")) or at) <= CONFIRM_WITHIN:
                    state["confirmed_at"] = at.isoformat()
                    # A window confirmed spent stays spent until its reset: no need to
                    # re-confirm it every 2 minutes (audit of auto switch release 4).
                    resets = [parse_time((got[current].get(k) or {}).get("resets_at"))
                              for k in spent_windows(got[current], at)]
                    resets = [r for r in resets if r]
                    if resets:
                        state["spent_confirmed"] = {"tag": current, "until": min(resets).isoformat()}
            if not dry_run:
                save_state(state)

    def confirmed() -> bool:
        c = parse_time(state.get("confirmed_at"))
        sc = state.get("spent_confirmed") or {}
        until = parse_time(sc.get("until")) if sc.get("tag") == current else None
        return bool((c and at - c <= CONFIRM_WITHIN) or (until and until > at))

    if not is_fresh(quota.get(current), at):
        refresh(FRESH_EVERY)

    errors = fresh_limit_errors(current, at)
    if errors and not dry_run:
        # Also a terminal claude, which no move records: if the owner exits it and switches
        # by hand, the account is known to have been spent (audit of release 24).
        newest = max(errors, key=lambda e: e["when"])
        cca_record_cut({current: {"at": newest["when"].isoformat(), "text": newest.get("text", "")}})
    counter = spent_windows(quota.get(current), at) if is_fresh(quota.get(current), at) else []
    if manual_recent and not errors and not assume_exhausted:
        counter = []
    upgrade = None
    if not (errors or counter or assume_exhausted):
        if state.pop("no_target", None) and not dry_run:
            save_state(state)
        # Not spent: rebalance to an account with much more room, up or down the priority
        # (the owner, 2026-11-16), or leave paid extra usage for free room. Never after a
        # manual switch, until the next automatic one
        # (audit of auto switch release 19: a shell switch on purpose was undone after 30 min),
        # unless he left a spent account (then only the 30-min grace).
        # Billing extra usage is like spent for the manual rules: a choice made by hand is
        # respected only for the 30-min grace (audit of 46: an account activated from
        # Latidos kept billing for days once it ran out).
        pay = paying(quota.get(current), at)
        if manual_recent or (manual and not state.get("manual_left_spent") and not pay):
            return 0
        # Rebalancing moves quiet tabs and loses their prompt cache: not more often than
        # REBALANCE_EVERY after an automatic switch.
        if last and at - last < REBALANCE_EVERY and not pay:
            return 0
        skip = upgrade_skip(state)
        # After a manual move off a spent account, only to it or above it in the priority:
        # an account below it that he skipped on purpose stays skipped (audit of release
        # 22). Accounts above it are allowed, also one he passed over going up.
        prev = state.get("manual_from")
        if manual and prev in ORDER and not pay:
            far = (at + dt.timedelta(days=365)).isoformat()
            skip = {**skip, **{t: far for t in ORDER if ORDER.index(t) > ORDER.index(prev)}}
        higher = [t for t in ORDER if t != current and not (parse_time(skip.get(t)) or at) > at]
        # Forced reads only for accounts that could be chosen (audit of release 19).
        if any(not is_fresh(quota.get(t), at) for t in higher + [current]):
            refresh(FRESH_EVERY)
        upgrade = upgrade_target(quota, current, at, skip)
        if upgrade and not just_read(quota.get(upgrade), at):
            # Going up acts on a forced read as recent as going down does (audit of release
            # 19: a cached lt could be nearly full by now).
            refresh(TRIGGERED_EVERY)
            if pay and not paying(quota.get(current), at):
                # The forced read says it no longer pays: the manual, throttle and skip rules
                # that were waived apply again; the next tick decides (audit of 47).
                return 0
            upgrade = upgrade_target(quota, current, at, skip)
            if upgrade and not just_read(quota.get(upgrade), at):
                upgrade = None
        if not upgrade:
            return 0
    if not upgrade:
        # Triggered: a forced read once a minute, but every FRESH_EVERY while no account can be
        # switched to (audit of auto switch release 3: up to 4,300 reads a day otherwise).
        # Slow pace for as long as the last decision found no account to switch to (audit of
        # auto switch release 8: it used to expire after 30 minutes).
        refresh(FRESH_EVERY if state.get("no_target") else TRIGGERED_EVERY)
        # The counter alone acts only on numbers a forced read just confirmed (audit of auto
        # switch release 2: the cache alone used to be enough).
        if not errors and not assume_exhausted and not (
                confirmed() and is_fresh(quota.get(current), at) and spent_windows(quota.get(current), at)):
            return 0
        reason = (f"límite en {len(errors)} chat(s)" if errors
                  else "ventana " + "/".join("5 h" if k == "session" else "semanal" for k in counter) + " agotada"
                  if not assume_exhausted else "simulado (--assume-exhausted)")

    if upgrade:
        reason = (f"{upgrade} tiene bastante más margen ({describe(upgrade, quota, at)} frente a "
                  f"{describe(current, quota, at)}; prioridad {' > '.join(ORDER)})")
    if last_try and at - last_try < COOLDOWN and not assume_exhausted:
        return 0

    spent = dict(state.get("spent") or {})
    failed = dict(state.get("failed") or {})
    target = upgrade or pick(quota, current, at, {**failed, **spent})
    if not target:
        if not dry_run and not state.get("no_target"):
            state["no_target"] = True
            save_state(state)
        fresh_tags = [t for t in ACCOUNTS if t != current and is_fresh(quota.get(t), at)]
        broken = [t for t in fresh_tags if has_room(quota.get(t), at) and (parse_time(failed.get(t)) or at) > at]
        if broken:
            kind = "failed"
            retry = min(parse_time(failed[t]) for t in broken)
            msg = (f"{current} sin cuota ({reason}) y el cambio a {', '.join(broken)} falló "
                   f"(¿login caducado? `claude-accounts`). Próximo intento a las {retry.astimezone():%H:%M}.")
        elif not fresh_tags:
            kind = "nodata"
            msg = f"{current} sin cuota ({reason}) y no tengo datos de cuota recientes de las otras cuentas (¿la fuente de cuota no responde?)."
        else:
            kind = "noroom"
            agains = [usable_again_at(quota.get(t), at) for t in fresh_tags]
            agains = [a for a in agains if a]
            first = min(agains) if agains else None
            msg = (f"{current} sin cuota ({reason}) y ninguna otra cuenta tiene margen: "
                   + ", ".join(describe(t, quota, at) for t in ACCOUNTS)
                   + (f". La primera con margen, a las {first.astimezone():%H:%M}." if first else "."))
        # One notice per situation: reset times rounded to the minute (they carry
        # sub-second noise that changed on every read, audit of release 1).
        # "No data" and "no room" share a key: stale reads alternating with live ones must not
        # send a notice each time (audit of auto switch release 8).
        key = f"none:{'failed' if kind == 'failed' else 'quota'}:{current}:" + ",".join(f"{t}.{k}:{reset_minute(quota.get(t), k)}"
                                             for t in ACCOUNTS for k in ("session", "weekly"))
        if dry_run:
            print(f"[dry-run] {msg}")
        elif state.get("last_none_notice") != key or (kind == "noroom" and state.get("last_none_kind") == "nodata"):
            # Same key from "no data" to "no room": still worth a notice (the quota source is
            # back and there is a first time with room); not the other way round (audit
            # of auto switch release 9).
            log(msg)
            state["last_none_notice"] = key
            state["last_none_kind"] = kind
            save_state(state)  # saved before the notice, which may be slow
            notify("Claude: sin cuenta con cuota", msg, "warn")
        return 0

    plan = f"{describe(current, quota, at)} -> {describe(target, quota, at)} ({reason})"
    if dry_run:
        print(f"[dry-run] would switch {plan}, then move the safe Cursor tabs")
        return 0
    # The checks above can take a while (a forced read, a follow-up move): if the default
    # changed meanwhile, the owner switched by hand; never overwrite it (audit of release 38).
    if current_account() != current:
        log(f"default changed from {current} by hand during the tick; switch to {target} dropped")
        return 0
    ok, detail = switch_to(target)
    if not ok:
        log(f"switch {plan} FAILED: {detail}")
        # One crit per target and cause, not one per retry (audit of auto switch release 4:
        # a broken login sent one every hour for days).
        # Upgrade failures have their own key, so a later needed switch down that fails
        # the same way still sends its crit (audit of auto switch release 21).
        cause = detail.split(":")[0]
        notices = dict(state.get("fail_notices") or {})
        # Own key per direction: going up and down failing alternately must not re-notify
        # (audit of release 22).
        key = f"{target}:up" if upgrade else target
        send_crit = notices.get(key) != cause
        if send_crit:
            notices[key] = cause
            state["fail_notices"] = notices
        state["last_attempt_at"] = at.isoformat()  # the cooldown also spaces retries
        # Skip it and try the next account; each new failure doubles the wait (1, 2, 4 h...
        # up to a day), so a broken login is not retried every hour (audits of releases 2, 4).
        counts = dict(state.get("fail_counts") or {})
        counts[target] = counts.get(target, 0) + 1
        state["fail_counts"] = counts
        # Exponent capped: 2 ** 35 hours overflows timedelta (audit of auto switch release 5).
        wait = min(FAILED_SKIP * (2 ** min(counts[target] - 1, 5)), dt.timedelta(hours=24))
        last_fail = dict(state.get("last_fail_at") or {})
        last_fail[target] = at.isoformat()
        state["last_fail_at"] = last_fail
        failed[target] = (at + wait).isoformat()
        state["failed"] = {t: u for t, u in failed.items() if (parse_time(u) or at) > at}
        save_state(state)  # saved before the notice, which may be slow
        if send_crit:
            # Going up is optional: the current account still works (audit of release 19).
            notify("Claude: cambio de cuenta fallido", f"No he podido pasar a {target}: {detail[:200]}"
                   + (f". Sigo en {current}, que funciona." if upgrade else ""), "warn" if upgrade else "crit")
        return 1
    # The account just left is skipped until its window resets (no flapping onto it on
    # stale numbers).
    no_up = {t: u for t, u in (state.get("no_upgrade") or {}).items() if (parse_time(u) or at) > at}
    if not upgrade:
        named = [e["reset"] for e in errors if e["reset"]]
        until = max(named) if named else (usable_again_at(quota.get(current), at) or at + SPENT_FALLBACK)
        spent[current] = until.isoformat()
        # Down again soon after going up into this account: its numbers showed room that was
        # not there (a limit they do not count), so do not go back up to it for a while
        # (audit of auto switch release 19: it bounced every reset).
        # Only when a limit error came while the numbers still showed room (audit of release
        # 21: an honest spend within the hour blocked it too).
        # The numbers after this tick's forced read, not the cache read before it (audit
        # of release 22: the cache was older than the hit and showed room).
        if (state.get("last_kind") == "up" and state.get("last_to") == current and last
                and at - last <= BOUNCE and errors and not spent_windows(quota.get(current), at)):
            no_up[current] = (at + NO_UPGRADE).isoformat()
    spent = {t: u for t, u in spent.items() if (parse_time(u) or at) > at}
    # Never a tab that may be in use, going up or down (audits of releases 21 and 27: the
    # status changes when a turn ends, not when the owner types, so a draft could be lost).
    # Down, an idle tab left on the spent account fails its next message on the limit,
    # and the next tick moves it with "continúa".
    moved = move_safe_tabs("safe-quiet")
    state.update(last_switch_at=at.isoformat(), last_attempt_at=at.isoformat(), last_from=current, last_to=target, last_reason=reason,
                 spent=spent, seen=target, seen_at=now().isoformat(), last_kind="up" if upgrade else "down",
                 no_upgrade=no_up)
    state.pop("manual_at", None)
    state.pop("manual_left_spent", None)
    state.pop("manual_from", None)
    state.pop("no_target", None)
    for k in ("fail_counts", "fail_notices", "last_fail_at"):
        if isinstance(state.get(k), dict):
            state[k].pop(target, None)
            state[k].pop(f"{target}:up", None)
    save_state(state)
    head = (f"Paso de {current} a {target}: {reason}" if upgrade
            else f"{current} se quedó sin cuota ({reason})")
    summary = move_summary(moved)
    log(f"switched {plan}; {detail}; tabs: {' | '.join(moved.splitlines())[:8000]}")
    if summary is None:
        body = (f"{head}. Ahora en {describe(target, quota, at)}. "
                "El movimiento de pestañas no terminó bien: revisa con claude-cursor-move y escribe continúa "
                "en las que se hayan quedado paradas.")
    else:
        by = lambda *o: [e for e in summary if e.get("outcome") in o]
        stuck = [e for e in summary if e.get("cut") and e.get("outcome") in STUCK]
        body = (f"{head}. Ahora en {describe(target, quota, at)}. "
                f"Pestañas que continúan: {len(by('continued'))}.")
        if stuck:
            body += (f" {len(stuck)} cortada(s) que no han podido seguir solas, escribe continúa en ellas: "
                     + "; ".join(str(e.get("name"))[:40] for e in stuck) + ".")
        if by("old_error"):
            body += (f" {len(by('old_error'))} movida(s) con un error de hace más de 8 h: no les he mandado "
                     "continúa; escríbelo en ellas si deben seguir.")
        if by("left") and upgrade:
            body += (f" {len(by('left'))} siguen en {current} (trabajando, con trabajo propio en marcha, sin estado conocido "
                     f"o en uso hace menos de {QUIET_TEXT}): se moverán solas tras {QUIET_TEXT} paradas y sin trabajo "
                     "propio en marcha; las de estado desconocido, con claude-cursor-move.")
        elif by("left"):
            # Short (Google Chat) and only what happens (audit of release 31).
            body += (f" {len(by('left'))} siguen en {current} (en uso, trabajando, con trabajo propio o sin estado conocido): "
                     f"al escribir en una dará el límite y se moverá sola con continúa (o tras {QUIET_TEXT} parada); las que tengan "
                     "trabajo propio, cuando termine; las de estado desconocido, con claude-cursor-move.")
    if detail:
        body += f" Aviso: {detail[:150]}"
    notify("Claude: cuenta cambiada sola", body, "ok" if not upgrade else "info")
    return 0


# ---- Status, install -------------------------------------------------------------------

def status() -> int:
    at = now()
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
    print(f"agent {LABEL}: {'loaded' if loaded else 'NOT loaded'}; {'DISABLED (--enable)' if OFF.exists() else 'enabled'}")
    quota = read_cache()
    current = current_account()
    st = load_state()
    prune_failures(st, at)
    for tag in ACCOUNTS:
        mark = "*" if tag == current else " "
        rep = quota.get(tag)
        flag = ("spent" if spent_windows(rep, at) else "usable" if has_room(rep, at)
                else "extra usage only" if usable(rep, at)
                else "stale numbers" if not is_fresh(rep, at) else "low")
        # The account in use is compared on its week only (0 while it bills extra usage).
        sc = current_score(tag, rep, at) if tag == current else score(tag, rep, at)
        print(f" {mark} {describe(tag, quota, at)}  {flag}  score {'?' if sc is None else f'{sc:.1f}'}"
              f" (x{priority_weight(tag):.2f})")
    print(f"limit errors now on {current}: {len(fresh_limit_errors(current, at))}")
    print(f"priority: {' > '.join(ORDER)} (a bonus of {PRIORITY_BIAS:.0%} per place; weekly room per day"
          f" until its reset decides, a move needs {REBALANCE_RATIO:g}x the current score)")
    if ORDER_UNKNOWN:
        src = "CLAUDE_AUTO_SWITCH_ORDER" if os.environ.get("CLAUDE_AUTO_SWITCH_ORDER") else str(ORDER_FILE)
        print(f"⚠️ unknown tags in {src} ignored: {', '.join(ORDER_UNKNOWN)}")
    st_skip = upgrade_skip(st)
    if st.get("manual_at") and st.get("manual_from") in ORDER:
        # Same rule as the tick: after a manual move, only to the account left or above.
        far = (at + dt.timedelta(days=365)).isoformat()
        if not paying(quota.get(current), at):
            st_skip.update({t: far for t in ORDER if ORDER.index(t) > ORDER.index(st["manual_from"])})
    up = upgrade_target(quota, current, at, st_skip) if not spent_windows(quota.get(current), at) else None
    if up:
        last_try = parse_time(st.get("last_attempt_at")) or parse_time(st.get("last_switch_at"))
        last_sw = parse_time(st.get("last_switch_at"))
        manual = parse_time(st.get("manual_at"))
        in_grace = manual and at - manual < MANUAL_GRACE
        pay = paying(quota.get(current), at)
        why_not = ("not within 30 min of a manual switch" if in_grace and (st.get("manual_left_spent") or pay)
                   else "not while you chose this account by hand (until the next automatic switch)"
                   if manual and not st.get("manual_left_spent") and not pay
                   else f"not within {REBALANCE_EVERY.total_seconds() / 60:g} min of the last automatic switch"
                   if last_sw and at - last_sw < REBALANCE_EVERY and not pay
                   else "after the 5-minute cooldown" if last_try and at - last_try < COOLDOWN
                   else "once a forced read confirms it" if not just_read(quota.get(up), at) else "")
        what = f"{current} bills extra usage, {up} has free room" if pay else "much more room left"
        print(f"would move to {up} ({what})" + (f": {why_not}" if why_not else ""))
    print(f"would pick if {current} ran out: {pick(quota, current, at, {**(st.get('failed') or {}), **(st.get('spent') or {})}) or 'none (no fresh account with room)'}")
    if st.get("last_switch_at"):
        print(f"last automatic switch: {st.get('last_from')} -> {st.get('last_to')} at {st['last_switch_at']} ({st.get('last_reason')})")
    for key, label in (("spent", "skipped until their reset (left on a limit error)"),
                       ("failed", "skipped after a failed switch"),
                       ("no_upgrade", "not gone back up to (bounced: down again soon after going up)")):
        live = {t: u for t, u in (st.get(key) or {}).items() if (parse_time(u) or at) > at}
        if live:
            print(f"{label}: " + ", ".join(f"{t} until {parse_time(u).astimezone():%d/%m %H:%M}" for t, u in live.items()))
    if st.get("fail_counts"):
        print(f"failed switches in the last {FAIL_MEMORY.days} days per account: {st['fail_counts']}")
    sc = st.get("spent_confirmed") or {}
    if sc.get("tag") and (parse_time(sc.get("until")) or at) > at:
        print(f"{sc['tag']} confirmed spent until {parse_time(sc['until']).astimezone():%d/%m %H:%M}")
    return 0


def install() -> int:
    python = "/opt/homebrew/bin/python3" if Path("/opt/homebrew/bin/python3").exists() else sys.executable
    path = os.pathsep.join([str(HOME / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>{Path(__file__).resolve()}</string>
  </array>
  <key>StartInterval</key><integer>{INTERVAL_SECONDS}</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>StandardOutPath</key><string>{LOG}</string>
  <key>StandardErrorPath</key><string>{LOG}</string>
</dict></plist>
""", encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"installed {PLIST} (every {INTERVAL_SECONDS} s, log {LOG})")
    return 0


def uninstall() -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print(f"removed {PLIST}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group()
    for flag in ("--status", "--install", "--uninstall", "--disable", "--enable"):
        g.add_argument(flag, action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="decide and print, change nothing")
    ap.add_argument("--assume-exhausted", action="store_true", help="act as if the current account ran out (with --dry-run to preview)")
    args = ap.parse_args()
    # Missing or broken accounts config: no status and no tick (a saved empty default would
    # later read as a manual switch; audit of 82). --install, --uninstall, --disable and
    # --enable still work, so the agent can always be stopped (audit of 83).
    no_accounts = not ACCOUNTS or DEFAULT_TAG not in ACCOUNTS
    no_accounts_msg = f"no Claude accounts configured in {CFG.path}: nothing to do"
    if args.status:
        if no_accounts:
            print(no_accounts_msg)
            return 0
        return status()
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.disable:
        OFF.touch()
        print("automatic account switch disabled (--enable to turn it back on)")
        return 0
    if args.enable:
        was_off = OFF.exists()
        if was_off:
            # Switches made while it was off are forgotten: it starts from the current
            # default (else the first tick would read the default it switched to by hand
            # because the account ran out as a choice, and never go back up). Under the
            # lock, so a running tick cannot write the old state back (audit of release 23).
            with LOCK.open("a") as fh:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    # A tick started before --disable may be moving tabs (up to 20 min).
                    print("waiting for the running tick to finish (it may be moving tabs)...", flush=True)
                    fcntl.flock(fh, fcntl.LOCK_EX)
                st = load_state()
                for k in ("seen", "seen_at", "manual_at", "manual_left_spent", "manual_from"):
                    st.pop(k, None)
                save_state(st)
                OFF.unlink(missing_ok=True)
        print("automatic account switch enabled" + ("" if was_off else " (it already was)"))
        return 0
    if OFF.exists() and not args.dry_run:
        return 0
    if no_accounts:
        # Under launchd stdout is the log: one line an hour there, not one a minute.
        if args.dry_run or sys.stdout.isatty():
            print(no_accounts_msg)
        elif dt.datetime.now().minute == 0:
            log(no_accounts_msg)
        return 0
    with LOCK.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.dry_run or sys.stdin.isatty():
                print("a tick is running right now (the LaunchAgent's); try again in a minute")
            return 0
        return tick(args.dry_run, args.assume_exhausted)


if __name__ == "__main__":
    sys.exit(main())
