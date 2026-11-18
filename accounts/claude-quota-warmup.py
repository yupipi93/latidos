#!/usr/bin/env python3
"""Say hello to Claude with every account before the workday, to start their 5-hour windows early.

The Claude.ai subscription quota runs in 5-hour windows that start with the first
message. Starting work at 08:00 and burning the quota fast means waiting until 13:00.
A one-line Haiku prompt at 05:00 opens the window then, so it resets at 10:00 instead.
Every account of the accounts config (accounts_config.py) is warmed, in parallel, so a switch at 09:00 lands on an account
whose window also resets at 10:00.

Runs as a LaunchAgent on weekdays. If the Mac was asleep at that hour, launchd runs it
on wake; past SKIP_AFTER the window would end too late to help, so it does nothing.
Each account is retried a few times (network not up yet after wake) and its day is
recorded once it succeeded, so it is never warmed twice. An account whose weekly limit
is spent is recorded as blocked (a warm-up cannot open its window) without retries.
A final failure is notified with the configured notifier.

The default account runs with CLAUDE_CONFIG_DIR unset (its Keychain login depends on it),
the others with their own dir. It runs from its own empty directory, with project settings only and no
MCP servers, so no instructions or hooks load, and no transcript is kept.

State (read by quota widgets such as a launcher's): ~/.local/state/claude-quota-warmup/state.json,
with the schedule and, per account, the last success, attempt, outcome and error.

Usage:
  accounts/claude-quota-warmup.py                  # warm every account (once per day, honours the window)
  accounts/claude-quota-warmup.py --force          # warm now, ignore the window and today's record
  accounts/claude-quota-warmup.py --force -a mt    # only one account (repeatable)
  accounts/claude-quota-warmup.py --install        # LaunchAgent, Mon-Fri at WARMUP_HOUR:WARMUP_MINUTE
  accounts/claude-quota-warmup.py --uninstall      # remove the LaunchAgent
  accounts/claude-quota-warmup.py --status         # agent loaded? per-account state, last runs

Environment (give them to --install: the hour and weekdays go into the launchd schedule and
the state file, the other WARMUP_* and CLAUDE_REAL_BIN into the plist's environment, since
launchd runs carry no shell env):
  WARMUP_HOUR, WARMUP_MINUTE  default 5, 0
  WARMUP_WEEKDAYS             default 1,2,3,4,5 (ISO, Monday = 1)
  WARMUP_SKIP_AFTER           default 09:00, later runs (late wake) do nothing
  WARMUP_ACCOUNTS             default every configured account
  CLAUDE_REAL_BIN             default ~/.local/bin/claude
  WARMUP_MODEL                default haiku
  WARMUP_STATE_DIR            default ~/.local/state/claude-quota-warmup (tests)
  WARMUP_RETRY_SECONDS        default 120 (tests)
"""

from __future__ import annotations

import argparse
import fcntl
import datetime as dt
import json
import os
import plistlib
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
REAL = os.environ.get("CLAUDE_REAL_BIN", str(HOME / ".local/bin/claude"))
MODEL = os.environ.get("WARMUP_MODEL", "haiku")
HOUR = int(os.environ.get("WARMUP_HOUR", "5"))
MINUTE = int(os.environ.get("WARMUP_MINUTE", "0"))
def parse_weekdays(raw: str) -> list[int] | None:
    """ISO weekdays (Monday = 1 ... Sunday = 7), or None when the value is not that."""
    try:
        days = sorted({int(d) for d in raw.split(",") if d.strip()})
    except ValueError:
        return None
    return days if days and all(1 <= d <= 7 for d in days) else None


WEEKDAYS_RAW = os.environ.get("WARMUP_WEEKDAYS", "1,2,3,4,5")
WEEKDAYS = parse_weekdays(WEEKDAYS_RAW) or [1, 2, 3, 4, 5]  # --install refuses a bad value
SKIP_AFTER = dt.time.fromisoformat(os.environ.get("WARMUP_SKIP_AFTER", "09:00"))
ATTEMPTS = 4
RETRY_SECONDS = float(os.environ.get("WARMUP_RETRY_SECONDS", "120"))

# (label, CLAUDE_CONFIG_DIR) per tag from the accounts config: "" = the variable unset.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)
CFG = accounts_config.CONFIG
ACCOUNTS = {a.tag: (a.label, a.env or "") for a in CFG.accounts}

LABEL = "local.claude-quota-warmup"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
LOG = HOME / "Library/Logs/claude-quota-warmup.log"
STATE_DIR = Path(os.environ.get("WARMUP_STATE_DIR") or HOME / ".local/state/claude-quota-warmup")
STATE = STATE_DIR / "state.json"
WORKDIR = STATE_DIR / "cwd"

# A spent week (or a disabled org) cannot be warmed: no retry, no failure notification.
BLOCKED_MARKERS = ("weekly limit", "weekly usage limit", "out of extra usage")
# A spent 5-hour window means it is already open: nothing to warm, nothing failed.
OPEN_MARKERS = ("5-hour limit", "session limit")

_lock = threading.Lock()


def log(msg: str) -> None:
    with _lock:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def selected(names: list[str] | None) -> list[str]:
    raw = names or [t.strip() for t in os.environ.get("WARMUP_ACCOUNTS", ",".join(ACCOUNTS)).split(",") if t.strip()]
    unknown = [t for t in raw if t not in ACCOUNTS]
    if unknown:
        raise SystemExit(f"unknown account(s): {', '.join(unknown)} (known: {', '.join(ACCOUNTS)})")
    return list(dict.fromkeys(raw))


def schedule() -> dict:
    """The installed schedule; --install records it and puts the same values in the plist's env."""
    return {"hour": HOUR, "minute": MINUTE, "weekdays": WEEKDAYS, "skip_after": SKIP_AFTER.strftime("%H:%M"),
            "accounts": selected(None)}


def installed_weekdays(state: dict) -> list[int]:
    days = (state.get("schedule") or {}).get("weekdays")
    return days if isinstance(days, list) and days and all(isinstance(d, int) for d in days) else WEEKDAYS


def load_state() -> dict:
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    accounts = state.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        # v1 (the default account only): {"last_success": day, "last_success_at": iso}
        if state.get("last_success") and CFG.default_tag:
            accounts[CFG.default_tag] = {"last_success": state.get("last_success"), "last_success_at": state.get("last_success_at"),
                              "outcome": "ok"}
    state["accounts"] = accounts
    state["version"] = 2
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f"state.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE)


def modify_state(change) -> dict:
    """Read, change and write the file under a lock shared by threads and processes, so a
    --force run during the 05:00 run never overwrites the other's records."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with _lock, (STATE_DIR / "state.lock").open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            state = load_state()
            change(state)
            save_state(state)
            return state
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def update_account(tag: str, **fields) -> None:
    """Merge fields into one account's record."""
    def change(state: dict) -> None:
        state["accounts"].setdefault(tag, {}).update(fields)
        stamps = [a.get("last_success_at") for a in state["accounts"].values() if a.get("last_success_at")]
        if stamps:  # kept for older readers: the latest success of any account
            latest = max(stamps)
            state["last_success_at"], state["last_success"] = latest, latest[:10]
    modify_state(change)


def classify(detail: str) -> str:
    low = detail.lower()
    if any(m in low for m in BLOCKED_MARKERS):
        return "blocked"
    if any(m in low for m in OPEN_MARKERS):
        return "open"
    return "error"


def ask_once(config_dir: str) -> tuple[str, str]:
    """('ok' | 'open' | 'blocked' | 'error', detail)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    cmd = [REAL, "-p", "Hola. Responde solo: hola", "--model", MODEL, "--max-turns", "1",
           "--output-format", "json", "--no-session-persistence", "--setting-sources", "project",
           "--strict-mcp-config"]
    try:
        proc = subprocess.run(cmd, cwd=WORKDIR, env=env, capture_output=True, text=True,
                              timeout=180, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", str(exc)
    out = (proc.stdout or "").strip()
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        detail = (out or proc.stderr or "").strip()[:300]
        return classify(detail), f"exit {proc.returncode}: {detail}"
    if proc.returncode != 0 or result.get("is_error"):
        detail = str(result.get("result"))[:300]
        return classify(detail), f"exit {proc.returncode}: {detail}"
    return "ok", f"{str(result.get('result')).strip()[:40]!r} in {result.get('duration_ms')} ms"


def warm_account(tag: str, results: dict) -> None:
    label, config_dir = ACCOUNTS[tag]
    detail = ""
    for attempt in range(1, ATTEMPTS + 1):
        started = dt.datetime.now()
        outcome, detail = ask_once(config_dir)
        now = dt.datetime.now()
        if outcome in ("ok", "open"):
            # "open": its 5-hour window is already running (spent, even): the goal is met.
            update_account(tag, last_success=started.date().isoformat(),
                           last_success_at=started.isoformat(timespec="seconds"),
                           last_attempt_at=now.isoformat(timespec="seconds"), outcome="ok", error=None)
            if outcome == "open":
                log(f"{tag}: 5-hour window already open, nothing to warm: {detail}")
            else:
                log(f"warmed {tag} (attempt {attempt}): {detail}; window resets about {started + dt.timedelta(hours=5):%H:%M}")
            results[tag] = ("ok", detail)
            return
        if outcome == "blocked":
            update_account(tag, last_attempt_at=now.isoformat(timespec="seconds"), outcome="blocked",
                           error=detail.split(": ", 1)[-1][:200], blocked_day=started.date().isoformat())
            log(f"{tag} not warmed, limit spent: {detail}")
            results[tag] = ("blocked", detail)
            return
        update_account(tag, last_attempt_at=now.isoformat(timespec="seconds"), outcome="error",
                       error=detail.split(": ", 1)[-1][:200])
        log(f"{tag} attempt {attempt}/{ATTEMPTS} failed: {detail}")
        if attempt < ATTEMPTS:
            time.sleep(RETRY_SECONDS)
    results[tag] = ("error", detail)


def warm(force: bool, names: list[str] | None) -> int:
    now = dt.datetime.now()
    today = now.date().isoformat()
    tags = selected(names)
    state = modify_state(lambda s: s.setdefault("schedule", schedule()))
    if not force and now.isoweekday() not in installed_weekdays(state):
        # launchd runs a slot missed in sleep on wake: Friday's may land on Saturday.
        log(f"skipped: {now:%A} is not a warm-up day")
        return 0
    if not force and now.time() > SKIP_AFTER:
        log(f"skipped: {now:%H:%M} is past {SKIP_AFTER:%H:%M}, the window would end too late to help")
        return 0
    todo = []
    for tag in tags:
        rec = state["accounts"].get(tag, {})
        if not force and (rec.get("last_success") == today or rec.get("blocked_day") == today):
            log(f"{tag}: already {'warmed' if rec.get('last_success') == today else 'found blocked'} today, nothing to do")
            continue
        todo.append(tag)
    if not todo:
        return 0
    WORKDIR.mkdir(parents=True, exist_ok=True)
    results: dict = {}
    threads = [threading.Thread(target=warm_account, args=(tag, results)) for tag in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    failed = {tag: detail for tag, (outcome, detail) in results.items() if outcome == "error"}
    if failed:
        lines = "; ".join(f"{ACCOUNTS[t][0]}: {d[:120]}" for t, d in failed.items())
        CFG.notify("warn", "Claude quota warm-up failed", f"At {now:%H:%M}. {lines}")
    return 1 if failed else 0


def install() -> int:
    if parse_weekdays(WEEKDAYS_RAW) is None:
        raise SystemExit(f"WARMUP_WEEKDAYS {WEEKDAYS_RAW!r}: ISO weekdays 1-7 (Monday = 1), comma-separated")
    if dt.time(HOUR, MINUTE) >= SKIP_AFTER:
        raise SystemExit(f"WARMUP_HOUR:WARMUP_MINUTE {HOUR:02d}:{MINUTE:02d} is not before WARMUP_SKIP_AFTER "
                         f"{SKIP_AFTER:%H:%M}: every run would skip itself")
    accounts = selected(None)  # refuse unknown WARMUP_ACCOUNTS now, not at 05:00
    # launchd runs carry no shell env: every setting given at install travels in the plist.
    env = {"WARMUP_HOUR": str(HOUR), "WARMUP_MINUTE": str(MINUTE), "WARMUP_WEEKDAYS": ",".join(map(str, WEEKDAYS)),
           "WARMUP_SKIP_AFTER": SKIP_AFTER.strftime("%H:%M"), "WARMUP_ACCOUNTS": ",".join(accounts),
           "WARMUP_MODEL": MODEL}
    if os.environ.get("CLAUDE_REAL_BIN"):
        env["CLAUDE_REAL_BIN"] = os.environ["CLAUDE_REAL_BIN"]
    python = "/opt/homebrew/bin/python3" if Path("/opt/homebrew/bin/python3").exists() else sys.executable
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python, str(Path(__file__).resolve())],
        "StartCalendarInterval": [{"Weekday": d, "Hour": HOUR, "Minute": MINUTE} for d in WEEKDAYS],
        "EnvironmentVariables": env,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLIST.with_suffix(".tmp")
    tmp.write_bytes(plistlib.dumps(plist))  # escapes every value; a bad one never replaces the loaded agent
    tmp.replace(PLIST)
    modify_state(lambda s: s.__setitem__("schedule", schedule()))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"installed {PLIST} (ISO weekdays {','.join(map(str, WEEKDAYS))} at {HOUR:02d}:{MINUTE:02d}, "
          f"until {SKIP_AFTER:%H:%M}, accounts {','.join(accounts)}, log {LOG})")
    return 0


def uninstall() -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print(f"removed {PLIST}")
    return 0


def status() -> int:
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True).returncode == 0
    print(f"agent {LABEL}: {'loaded' if loaded else 'NOT loaded'} ({PLIST})")
    state = load_state()
    installed = (state.get("schedule") or {}).get("accounts")
    for tag in installed if isinstance(installed, list) and installed else selected(None):
        rec = state["accounts"].get(tag)
        if not rec:
            print(f"  {tag}: never warmed")
            continue
        print(f"  {tag}: last success {rec.get('last_success_at') or '-'}, last attempt {rec.get('last_attempt_at') or '-'}"
              f" ({rec.get('outcome') or '-'}{': ' + rec['error'] if rec.get('error') else ''})")
    if LOG.exists():
        print("last log lines:")
        print("".join(LOG.read_text().splitlines(keepends=True)[-6:]), end="")
    return 0 if loaded else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--force", action="store_true")
    group.add_argument("--install", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    group.add_argument("--status", action="store_true")
    parser.add_argument("-a", "--account", action="append", choices=sorted(ACCOUNTS),
                        help="only this account (repeatable); default WARMUP_ACCOUNTS")
    args = parser.parse_args()
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    if args.status:
        return status()
    return warm(args.force, args.account)


if __name__ == "__main__":
    sys.exit(main())
