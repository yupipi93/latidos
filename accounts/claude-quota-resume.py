#!/usr/bin/env python3
"""Tell the Claude chats that stopped on the quota limit to carry on once it resets.

When the Claude.ai 5-hour (or weekly) limit runs out, every chat that was working ends
its turn with a synthetic "You've hit your session limit · resets 4:50pm" message and
then sits idle. This script, run by a LaunchAgent every two minutes, finds those chats
and wakes them up after the reset:

  detect   top-level transcripts in ~/.claude-shared/projects/*/ touched in the last
           MAX_AGE_HOURS whose last user/assistant entry is that rate-limit error, from
           an interactive entrypoint (Cursor/VS Code plugin or the terminal CLI;
           headless sdk runs are ignored, except chats this script resumed headless);
  wait     until the reset time named in the message (+ RESET_GRACE), or the limit time
           + 5 h when it names none;
  probe    one tiny headless Haiku call under the chat's account (an open tab's own
           account; for a closed chat, the default account in ~/.claude-active): if it
           bounces off the limit, nothing is sent and it is retried later (open tabs on
           the next tick, closed chats on the next --include-closed run);
  deliver  to every such chat whose Claude process is still alive (the open tab), one
           peer message through its own socket with claude_peer.py, which attests the
           permission class (a session holds a message of the other class, and releases
           it only if its mode later comes to match) and checks that the chat took it;
           one send per limit entry: a chat that does not take it is marked "held" and
           not sent to again (not even with --now) while the process holding that
           message lives; once its tab runs a new process, it is woken again. The message lands in the open tab
           as a teammate's message, which the chat treats as a request, never as
           the owner's approval: its gates stay;
  verify   the transcript must show new activity after the limit entry within
           VERIFY_SECONDS; a fresh limit error means "not reset yet, retry later".

Closed chats (no live process) are reported, not resumed: they are not "active". Pass
--include-closed to resume those headless with `claude -p --resume <id>` as well, on the
default account (~/.claude-active): a closed chat has no process to tell its own account.

Each limit entry is handled once (state keyed by its uuid, MAX_ATTEMPTS tries). The run
ends with one notify.sh message listing what was woken up. Only processes this script
starts are ever waited on; nothing is signalled.

Usage:
  accounts/claude-quota-resume.py               # one tick
  accounts/claude-quota-resume.py --status      # stopped chats, reset times, state
  accounts/claude-quota-resume.py --dry-run     # tick without sending
  accounts/claude-quota-resume.py --now         # ignore the reset time (manual nudge)
  accounts/claude-quota-resume.py --install     # LaunchAgent, every 2 min
  accounts/claude-quota-resume.py --uninstall

Environment:
  CLAUDE_REAL_BIN            default ~/.local/bin/claude
  QUOTA_RESUME_MESSAGE       override the Spanish message sent to each chat
  QUOTA_RESUME_INCLUDE_CLOSED=1   same as --include-closed; for the LaunchAgent, set it when
                                  running --install (it is written into the plist)
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_peer  # noqa: E402  (sibling module: the peer-socket delivery)
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)

HOME = Path.home()
HERE = Path(__file__).resolve().parent
CLAUDE = os.environ.get("CLAUDE_REAL_BIN", str(HOME / ".local/bin/claude"))
CFG = accounts_config.CONFIG
PROJECTS = CFG.pool / "projects"
ACCOUNTS = {a.tag: {"dir": a.dir, "env": a.env} for a in CFG.accounts}
STATE_DIR = HOME / ".local/state/claude-quota-resume"
STATE = STATE_DIR / "state.json"
LOCK = STATE_DIR / "lock"
RELAY_CWD = STATE_DIR / "relay-cwd"
LOG = HOME / "Library/Logs/claude-quota-resume.log"
LABEL = "local.claude-quota-resume"
PLIST = HOME / "Library/LaunchAgents" / f"{LABEL}.plist"
# Claude's limit messages name a zone ("resets 4:50pm (Europe/Madrid)"); without one, the
# accounts config's `timezone`, else this machine's.
TZ = accounts_config.zone(CFG.timezone)

INTERVAL_SECONDS = 120
MAX_AGE_HOURS = 8          # a limit older than this is not chased (window is 5 h)
RESET_GRACE = dt.timedelta(minutes=1)
VERIFY_SECONDS = 90
MAX_ATTEMPTS = 4
TAIL_BYTES = 768 * 1024
INTERACTIVE = {"claude-vscode", "cli"}

DEFAULT_MESSAGE = (
    "Aviso automático: la cuota de Claude se ha restablecido (te cortó el límite a las {limit_at}). "
    "Continúa con tu tarea exactamente donde la dejaste y termínala. "
    "Si tenías subagentes o tareas en segundo plano, puede que también se cortaran con el límite: "
    "revisa cuáles no terminaron y reanúdalos o relánzalos. "
    "Este aviso no aprueba nada: lo que necesitaba el visto bueno explícito del usuario lo sigue necesitando."
)

RESET_RE = re.compile(
    r"resets\s+(?:(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap]m)?(?:\s*\((?P<tz>[^)]+)\))?",
    re.I,
)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TZ)


def parse_reset(text: str, limit_at: dt.datetime) -> dt.datetime:
    """Reset time named in the limit message; limit time + 5 h when it names none."""
    match = RESET_RE.search(text)
    if not match:
        return limit_at + dt.timedelta(hours=5)
    tz = TZ
    if match.group("tz"):
        try:
            tz = ZoneInfo(match.group("tz").strip())
        except Exception:  # noqa: BLE001 - unknown zone name, keep the configured or local zone
            tz = TZ
    hour, minute = int(match.group("h")), int(match.group("m") or 0)
    ap = (match.group("ap") or "").lower()
    if ap == "pm" and hour != 12:
        hour += 12
    if ap == "am" and hour == 12:
        hour = 0
    base = limit_at.astimezone(tz)
    if match.group("mon"):
        month = MONTHS.get(match.group("mon").lower()[:3], base.month)
        year = base.year + (1 if month < base.month else 0)
        return dt.datetime(year, month, int(match.group("day")), hour, minute, tzinfo=tz)
    when = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= base:
        when += dt.timedelta(days=1)
    return when


def message_text(entry: dict) -> str:
    content = entry.get("message", {}).get("content", [])
    if isinstance(content, str):
        return content
    return " ".join(c.get("text", "") for c in content if isinstance(c, dict))


def is_limit(entry: dict) -> bool:
    return (entry.get("type") == "assistant" and bool(entry.get("isApiErrorMessage"))
            and (entry.get("error") == "rate_limit" or entry.get("apiErrorStatus") == 429)
            and "limit" in message_text(entry).lower())


# Local slash commands write their call and output as plain "user" entries (/usage,
# /status, /login...): nothing the model answers. Same list as cursor-claude-account.py.
LOCAL_COMMANDS = {"usage", "context", "cost", "status", "help", "config", "model", "mcp", "memory",
                  "clear", "login", "logout", "doctor", "permissions", "agents", "hooks", "resume",
                  "exit", "ide", "terminal-setup", "vim", "theme", "release-notes", "add-dir", "fast",
                  "effort", "rename", "rewind", "export", "statusline", "privacy-settings", "upgrade"}
COMMAND_NAME_RE = re.compile(r"<command-name>/?([A-Za-z0-9:_-]+)</command-name>")


def is_local_entry(entry: dict) -> bool:
    """A local slash command or its output, or an interrupt marker: it neither ends nor
    extends a turn, so a chat cut on the limit where the owner then typed /usage is still cut
    (audit of auto switch release 18)."""
    if entry.get("type") != "user":
        return False
    text = message_text(entry)[:300]
    if "<local-command-" in text or text.lstrip().startswith("[Request interrupted"):
        return True
    cmd = COMMAND_NAME_RE.search(text)
    return bool(cmd and cmd.group(1).lower() in LOCAL_COMMANDS)


def is_task_notification(entry: dict) -> bool:
    """A background task's notice delivered as a user entry (`<task-notification>`). When
    the limit hits, the chat's subagents fail on it seconds later and their notices land
    after the limit error; they do not answer it (auto switch release 42: two tabs cut on
    the weekly limit were taken for uncut and left on the spent account)."""
    return entry.get("type") == "user" and message_text(entry).lstrip().startswith("<task-notification>")


def last_turn(convo: list[dict]) -> dict | None:
    """The last main-chain entry that is not a local command or a task notification."""
    for entry in reversed(convo):
        if not is_local_entry(entry) and not is_task_notification(entry):
            return entry
    return None


def tail_entries(path: Path) -> list[dict]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        raw = fh.read().decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES:
        raw = raw[1:]  # first line is probably cut
    entries = []
    for line in raw:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def conversation(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("type") in ("user", "assistant") and not e.get("isSidechain")]


def first_cwd(path: Path) -> str | None:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for _, line in zip(range(200), fh):
            try:
                cwd = json.loads(line).get("cwd")
            except json.JSONDecodeError:
                continue
            if cwd:
                return cwd
    return None


def headless_sessions(state: dict) -> set[str]:
    """Sessions this script resumed headless: their next limit error is written by that
    `claude -p` run, with entrypoint sdk-cli (audit of release 24)."""
    return {r["session"] for r in state.values() if isinstance(r, dict) and r.get("headless") and r.get("session")}


def stopped_chats(resumed: set[str] | None = None) -> list[dict]:
    """Chats whose last turn is a rate-limit error, newest limit first. Interactive
    entrypoints only, plus the sessions in `resumed` (resumed headless by this script)."""
    resumed = resumed or set()
    cutoff = time.time() - MAX_AGE_HOURS * 3600
    found = []
    for path in PROJECTS.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            convo = conversation(tail_entries(path))
        except OSError:
            continue
        last = last_turn(convo)
        if not last or not is_limit(last):
            continue
        if last.get("entrypoint") not in INTERACTIVE and path.stem not in resumed:
            continue
        limit_at = parse_ts(last["timestamp"])
        if limit_at.timestamp() < cutoff:
            continue
        text = message_text(last)
        found.append({
            "session": path.stem,
            "path": str(path),
            "limit_uuid": last.get("uuid"),
            "limit_at": limit_at,
            "reset_at": parse_reset(text, limit_at),
            "limit_text": text.strip(),
            "cwd": first_cwd(path) or last.get("cwd"),
            "entrypoint": last.get("entrypoint"),
        })
    found.sort(key=lambda c: c["limit_at"], reverse=True)
    return found


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # signal 0: existence check only, nothing is delivered
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return "claude" in cmd


def live_processes() -> dict[str, dict]:
    """sessionId -> {pid, account, socket, name, status} for running interactive chats."""
    live: dict[str, dict] = {}
    for tag, acc in ACCOUNTS.items():
        for meta in (acc["dir"] / "sessions").glob("*.json"):
            try:
                info = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            pid, sid = info.get("pid"), info.get("sessionId")
            if not pid or not sid or info.get("kind") != "interactive" or not pid_alive(int(pid)):
                continue
            live[sid] = {"pid": int(pid), "account": tag, "socket": info.get("messagingSocketPath"),
                         "name": info.get("name") or sid[:8], "status": info.get("status", "?")}
    return live


def account_env(tag: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    if ACCOUNTS[tag]["env"]:
        env["CLAUDE_CONFIG_DIR"] = ACCOUNTS[tag]["env"]
    return env


def default_account() -> str:
    """The default account the switch tools last wrote (the default one when unknown)."""
    try:
        tag = CFG.active_file.read_text().strip()
    except OSError:
        return CFG.default_tag
    return tag if tag in ACCOUNTS else CFG.default_tag


def quota_open(tag: str) -> tuple[bool, str]:
    """One tiny headless Haiku call under the account: does its quota answer again?"""
    RELAY_CWD.mkdir(parents=True, exist_ok=True)
    cmd = [CLAUDE, "-p", "Responde solo: ok", "--model", "haiku", "--max-turns", "1",
           "--output-format", "json", "--no-session-persistence", "--setting-sources", "project",
           "--strict-mcp-config"]
    try:
        proc = subprocess.run(cmd, cwd=RELAY_CWD, env=account_env(tag), capture_output=True,
                              text=True, timeout=180, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    out = (proc.stdout or "").strip()
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        return False, f"exit {proc.returncode}: {(out or proc.stderr or '').strip()[-300:]}"
    if proc.returncode != 0 or result.get("is_error"):
        return False, f"exit {proc.returncode}: {str(result.get('result') or '').strip()[:300]}"
    return True, "quota answers"


def wake_sockets(tag: str, chats: list[dict], texts: dict[str, str]) -> tuple[list[dict], dict[str, str]]:
    """Deliver each chat its message once through its own socket (claude_peer.py attests the
    permission class; a bare line, or the old SendMessage relay from a prompting session, is
    held by a bypassPermissions tab). Returns (delivered, {limit_uuid: why not taken})."""
    sent, errors = [], {}
    for c in chats:
        info = {"pid": c["proc"]["pid"], "messagingSocketPath": c["proc"]["socket"], "sessionId": c["session"]}
        ok, detail = claude_peer.deliver(info, texts[c["limit_uuid"]], ACCOUNTS[tag]["dir"] / "sessions",
                                         Path(c["path"]))
        log(f"wake {tag} -> {c['proc']['name']}: {'ok (' + detail + ')' if ok else 'NOT TAKEN: ' + detail}")
        if ok:
            sent.append(c)
        else:
            errors[c["limit_uuid"]] = detail
    return sent, errors


RESTRICTIVE_MODES = {"plan", "dontAsk"}


def headless_mode(chat: dict, tag: str) -> str:
    """The mode for a headless resume: the session's own bypass when it ran in bypass and
    the account allows it (else "auto": an account whose managed settings disable bypass); a
    restrictive mode it ran in (plan, dontAsk) is kept, never widened; anything else, or
    no recorded mode, is "auto" (default / acceptEdits cannot approve tools headless)."""
    path = Path(chat["path"]) if chat.get("path") else None
    recorded = claude_peer.transcript_mode(path)
    if recorded in RESTRICTIVE_MODES:
        return recorded
    wanted = "bypassPermissions" if recorded in claude_peer.BYPASS_MODES else "auto"
    return claude_peer.run_mode(ACCOUNTS[tag]["dir"], wanted)


def resume_headless(chat: dict, tag: str, text: str) -> tuple[bool, str]:
    cwd = chat["cwd"] if chat["cwd"] and Path(chat["cwd"]).is_dir() else str(HOME)
    try:
        # The child keeps its own copy of the log fd; the parent closes its one.
        with open(STATE_DIR / f"headless-{chat['session'][:8]}.log", "a", encoding="utf-8") as out:
            proc = subprocess.Popen(
                [CLAUDE, "-p", text, "--resume", chat["session"], "--permission-mode", headless_mode(chat, tag)],
                cwd=cwd, env=account_env(tag), stdout=out, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
    except OSError as exc:
        return False, str(exc)
    return True, f"headless pid {proc.pid}"


def progress_after(path: str, limit_uuid: str) -> str:
    """'running' if the chat moved on after the limit entry, 'limit' if it hit it again, else 'none'."""
    convo = conversation(tail_entries(Path(path)))
    idx = next((i for i, e in enumerate(convo) if e.get("uuid") == limit_uuid), None)
    after = convo[idx + 1:] if idx is not None else []
    if not after:
        # The limit entry may have scrolled out of the tail: anything newer than it counts.
        return "none" if idx is not None else "running"
    if any(e.get("type") == "assistant" and not is_limit(e) for e in after):
        return "running"
    if is_limit(after[-1]):
        return "limit"
    return "running" if any(e.get("type") == "user" for e in after) and len(after) > 1 else "none"


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    keep = now() - dt.timedelta(days=3)
    state = {k: v for k, v in state.items() if parse_ts(v.get("at", now().isoformat())) > keep}
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def notify(title: str, body: str, level: str = "info") -> None:
    CFG.notify(level, title, body)


def tick(dry_run: bool, ignore_reset: bool, include_closed: bool) -> int:
    state = load_state()
    chats = stopped_chats(headless_sessions(state))
    live = live_processes()
    due: dict[str, list[dict]] = {}
    closed: list[dict] = []
    skipped: list[str] = []
    for chat in chats:
        key = chat["limit_uuid"]
        entry = state.get(key, {})
        status = entry.get("status")
        alive = bool((live.get(chat["session"]) or {}).get("socket"))
        # "held": a message is parked in that chat's process and could still be released,
        # so a second one could arrive twice. Not re-sent (not even with --now) while that
        # process lives. Once it is gone the message is gone too: the chat is woken again
        # if its tab runs a new process, or treated as closed if the tab is gone (audits
        # of releases 26 to 28).
        live_pid = (live.get(chat["session"]) or {}).get("pid")
        holder = entry.get("pid")
        if status == "held" and alive and (holder is None or holder == live_pid or pid_alive(int(holder))):
            # The process holding the parked message still runs (maybe next to another
            # one of the same session): a second send could arrive twice (audit of
            # release 28).
            skipped.append(f"{chat['session'][:8]} held (type continúa in its tab)")
            continue
        if status == "held" and alive:
            # The holder is gone and the tab runs a new process: the parked message died
            # with the old one.
            status = None
        # closed_reported is taken again when closed chats are asked for (audit of release
        # 23), or when its tab is open again (audit of release 26).
        if status == "closed_reported" and (include_closed or alive):
            pass
        elif status in ("done", "gave_up", "closed_reported") and not ignore_reset:
            skipped.append(f"{chat['session'][:8]} {status}")
            continue
        if entry.get("attempts", 0) >= MAX_ATTEMPTS:
            skipped.append(f"{chat['session'][:8]} out of attempts ({MAX_ATTEMPTS})")
            continue
        if not ignore_reset and now() < chat["reset_at"] + RESET_GRACE:
            skipped.append(f"{chat['session'][:8]} before its reset ({chat['reset_at']:%H:%M})")
            continue
        proc = live.get(chat["session"])
        chat["proc"] = proc
        if proc and proc["socket"]:
            due.setdefault(proc["account"], []).append(chat)
        elif include_closed:
            # A closed chat has no process to tell its account: resume it on the default
            # account (~/.claude-active), the one a shell switch command last switched to.
            chat["closed"] = True
            due.setdefault(default_account(), []).append(chat)
        elif status == "closed_reported" or entry.get("reported"):
            # Reported once already, also after a manual run left it waiting (audit of
            # release 27).
            skipped.append(f"{chat['session'][:8]} closed, already reported (use --include-closed)")
            continue
        else:
            # Report every closed chat once (it then stays closed_reported until a manual
            # --include-closed run takes it). Its attempts and headless flag are kept so
            # MAX_ATTEMPTS still bounds manual runs (audits of releases 24 and 25).
            closed.append(chat)

    if not due and not closed and skipped and sys.stdin.isatty():
        # A manual run that takes nothing says why (audit of release 26).
        print("nothing to wake: " + "; ".join(skipped))
    if not due and not closed:
        return 0

    woken, failed, held = [], [], []
    for tag, group in due.items():
        sockets = [c for c in group if c.get("proc")]
        headless = [c for c in group if not c.get("proc")]
        texts = {c["limit_uuid"]: os.environ.get("QUOTA_RESUME_MESSAGE", DEFAULT_MESSAGE).format(
            limit_at=f"{c['limit_at']:%H:%M}") for c in group}
        names = ", ".join(c["proc"]["name"] if c.get("proc") else c["session"][:8] for c in group)
        if dry_run:
            log(f"[dry-run] account {tag}: would wake {names}")
            continue
        for c in group:
            rec = state.setdefault(c["limit_uuid"], {"session": c["session"]})
            rec["at"] = now().isoformat(timespec="seconds")
        # One quota probe per account, for open tabs and closed chats alike (audit of
        # release 21: closed chats used to skip it).
        ok, detail = quota_open(tag)
        if not ok:
            later = ("next tick" if all(not c.get("closed") for c in group)
                     else "next tick for open tabs, next --include-closed run for closed chats")
            log(f"quota probe {tag} -> not open yet, retry {later}: {detail[:300]}")
            for c in group:
                state[c["limit_uuid"]]["status"] = "waiting"
                state[c["limit_uuid"]]["last_error"] = detail[:300]
            continue
        if sockets:
            delivered, errors = wake_sockets(tag, sockets, texts)
            for c in sockets:
                if c in delivered:
                    continue
                rec = state[c["limit_uuid"]]
                rec["attempts"] = rec.get("attempts", 0) + 1
                rec["last_error"] = errors.get(c["limit_uuid"], "")[:300]
                if rec["last_error"].startswith(claude_peer.NOT_SENT):
                    # Nothing was sent (the process ended meanwhile...): safe to retry.
                    rec["status"] = "gave_up" if rec["attempts"] >= MAX_ATTEMPTS else "waiting"
                    if rec["status"] == "gave_up":
                        failed.append(c["proc"]["name"])
                    continue
                # Sent but not taken: it is parked in the chat and is released if the
                # chat's mode later matches, so every retry could arrive as one more
                # message. The user is told to type in that tab instead.
                rec["status"] = "held"
                rec["pid"] = c["proc"]["pid"]  # the process that holds the message
                held.append(c["proc"]["name"])
            sockets = delivered
        for c in list(headless):
            ok, detail = resume_headless(c, tag, texts[c["limit_uuid"]])
            log(f"headless resume {c['session'][:8]}: {'ok' if ok else 'FAILED'} {detail}")
            if not ok:
                state[c["limit_uuid"]]["status"] = "waiting"
                headless.remove(c)
            else:
                state[c["limit_uuid"]]["headless"] = True
        sent = sockets + headless
        if not sent:
            continue
        for c in sent:
            state[c["limit_uuid"]]["attempts"] = state[c["limit_uuid"]].get("attempts", 0) + 1
        deadline = time.time() + VERIFY_SECONDS
        pending = list(sent)
        while pending and time.time() < deadline:
            time.sleep(15)
            for c in list(pending):
                result = progress_after(c["path"], c["limit_uuid"])
                if result == "running":
                    pending.remove(c)
                    state[c["limit_uuid"]]["status"] = "done"
                    woken.append(c["proc"]["name"] if c.get("proc") else f"{c['session'][:8]} (headless)")
                elif result == "limit":
                    pending.remove(c)
                    state[c["limit_uuid"]]["status"] = "waiting"
                    log(f"{c['session'][:8]}: hit the limit again, retry "
                        f"{'with the next --include-closed run' if c.get('closed') else 'next tick'}")
        for c in pending:
            rec = state[c["limit_uuid"]]
            rec["status"] = "unverified"
            if rec["attempts"] >= MAX_ATTEMPTS:
                rec["status"] = "gave_up"
                failed.append(c["proc"]["name"] if c.get("proc") else c["session"][:8])
            log(f"{c['session'][:8]}: no activity {VERIFY_SECONDS} s after the message "
                f"(attempt {rec['attempts']}/{MAX_ATTEMPTS})")

    if closed and not dry_run:
        for c in closed:
            state[c["limit_uuid"]] = {**state.get(c["limit_uuid"], {}), "session": c["session"],
                                      "status": "closed_reported", "reported": True,
                                      "at": now().isoformat(timespec="seconds")}
        log("closed chats left alone: " + ", ".join(c["session"][:8] for c in closed))
    elif closed:
        log("[dry-run] closed chats left alone: " + ", ".join(c["session"][:8] for c in closed))

    if not dry_run:
        save_state(state)
        parts = []
        if woken:
            parts.append("Retoman su trabajo: " + ", ".join(woken) + ".")
        if failed:
            parts.append("No arrancan tras varios intentos: " + ", ".join(failed) + ".")
        if held:
            parts.append("No aceptaron el aviso, escribe continúa en: " + ", ".join(held) + ".")
        if closed:
            parts.append(f"{len(closed)} chat(s) cortados ya no tienen la pestaña abierta; no los toco.")
        if parts:
            notify("Cuota de Claude restablecida", " ".join(parts), "warn" if failed or held else "ok")
    return 1 if failed or held else 0


def status() -> int:
    loaded = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                            capture_output=True).returncode == 0
    print(f"agent {LABEL}: {'loaded' if loaded else 'NOT loaded'} ({PLIST})")
    state, live = load_state(), live_processes()
    chats = stopped_chats(headless_sessions(state))
    if not chats:
        print("no chat is stopped on the quota limit right now")
    for c in chats:
        proc = live.get(c["session"])
        rec = state.get(c["limit_uuid"], {})
        takeable = (rec.get("status") not in ("gave_up", "done") and rec.get("attempts", 0) < MAX_ATTEMPTS
                    and now() >= c["reset_at"] + RESET_GRACE)
        where = (f"open tab pid {proc['pid']} ({proc['account']}, {proc['name']})" if proc
                 else f"closed (would resume on {default_account()} with --include-closed)" if takeable
                 else "closed")
        print(f"- {c['session'][:8]}  limit {c['limit_at']:%d/%m %H:%M}  reset {c['reset_at']:%d/%m %H:%M}  "
              f"{where}  state {rec.get('status', 'pending')} ({rec.get('attempts', 0)} tries)")
    if LOG.exists():
        print("last log lines:")
        print("".join(LOG.read_text().splitlines(keepends=True)[-6:]), end="")
    return 0


def env_block() -> str:
    """launchd does not inherit the shell: QUOTA_RESUME_INCLUDE_CLOSED and
    QUOTA_RESUME_MESSAGE set at --install time go into the plist (audit of release 24)."""
    keep = {k: os.environ[k] for k in ("QUOTA_RESUME_INCLUDE_CLOSED", "QUOTA_RESUME_MESSAGE") if os.environ.get(k)}
    if not keep:
        return ""
    from xml.sax.saxutils import escape
    items = "".join(f"<key>{k}</key><string>{escape(v)}</string>" for k, v in keep.items())
    return f"\n  <key>EnvironmentVariables</key><dict>{items}</dict>"


def install() -> int:
    python = "/opt/homebrew/bin/python3" if Path("/opt/homebrew/bin/python3").exists() else sys.executable
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>{python}</string><string>{Path(__file__).resolve()}</string>
  </array>
  <key>StartInterval</key><integer>{INTERVAL_SECONDS}</integer>{env_block()}
  <key>RunAtLoad</key><true/>
  <key>AbandonProcessGroup</key><true/>
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
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true")
    group.add_argument("--install", action="store_true")
    group.add_argument("--uninstall", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="detect and report, send nothing")
    parser.add_argument("--now", action="store_true", help="ignore the reset time and handled state (held chats whose holding process still runs and chats out of attempts are still left alone)")
    parser.add_argument("--include-closed", action="store_true",
                        default=os.environ.get("QUOTA_RESUME_INCLUDE_CLOSED", "").lower() in ("1", "true", "yes"),
                        help="also resume chats whose tab is closed, headless")
    args = parser.parse_args()
    if args.install:
        return install()
    if args.uninstall:
        return uninstall()
    if args.status:
        return status()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if sys.stdin.isatty():
                print("another run is in progress (the LaunchAgent's tick); try again in a few minutes")
            return 0
        return tick(args.dry_run, args.now, args.include_closed)


if __name__ == "__main__":
    sys.exit(main())
