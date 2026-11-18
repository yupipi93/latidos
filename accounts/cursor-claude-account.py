#!/usr/bin/env python3
"""cursor-claude-account.py - switch the Claude account of Cursor's Claude extension, no restart.

Why this exists: the extension used to take CLAUDE_CONFIG_DIR from Cursor's own process
environment, frozen at launch, so every switch meant quitting and reopening Cursor (slow,
and the extension often failed to load on the first try).

How: the extension builds the environment of each Claude process it spawns from the
claudeCode.environmentVariables setting, and it watches that setting: when the
CLAUDE_CONFIG_DIR in it changes, it refreshes every webview within about a second. The
setting can only hold an absolute path, so the default account is written as ~/.claude and the wrapper
named in claudeCode.claudeProcessWrapper (claude-cursor-wrapper.sh) unsets it before
exec. Every account is always written explicitly, so the environment Cursor was
launched with no longer matters.

    default  CLAUDE_CONFIG_DIR=~/.claude    -> unset by the wrapper, Keychain "Claude Code-credentials"
    other    CLAUDE_CONFIG_DIR=~/.claude.x  -> Keychain "Claude Code-credentials-<sha256(dir)[:8]>"

The accounts and their e-mails (checked before a switch) come from the accounts config.

Open conversations: a Claude process that is already running keeps its account.
--move-open (run by the shell's switch commands and the control on every switch) lists the extension's
processes still on another account and ends them. When some are working (status busy, or
waiting on a prompt or a question, in <config dir>/sessions/<pid>.json) it asks whether to
move only the idle ones or all of them, because ending a working one interrupts its
turn; --scope idle|all skips the question, and without a terminal only idle ones move.
The tab stays open: when its process ends, the tab drops its channel, and the next message relaunches the same session with --resume
under the current settings (the transcript is found through the shared pool,
claude-shared-pool.py).

Auto-continue: nobody has to type that next message. The companion extension
(accounts/relaunch, one per Cursor window) reopens each
moved tab, loading starts its new process on the new account, and this script sends
"continúa" through that process's socket to the sessions whose work was cut: busy or
waiting on a prompt or a question when ended, last turn an API error such as the usage
limit (not a usage-policy refusal; in the unattended scopes safe / safe-cut / safe-quiet, not over 8 h
old), an unanswered last input, input queued after the last answer, or background work of the ended process (subagents,
commands, monitors, workflows, a wakeup) never reported finished (work_in_flight). The
message names that work and asks to resume or relaunch it. Finished ones only move.
The message goes through claude_peer.py, which attests the sender's permission class:
without it a bypassPermissions tab holds the message and it never arrives (2026-11-12).
--continue none: reopen only; off: the old way (end the processes, type in each tab).

A keybinding that closes and reopens the tab does not work in Cursor 3.24: with the focus
inside a webview it only dispatches commands from a fixed allowlist, which has neither
runCommands nor claude-vscode.reopenClosedSession.

    accounts/cursor-claude-account.py              # status: configured account + live sessions
    accounts/cursor-claude-account.py home         # move Cursor to the account tagged "home"
    accounts/cursor-claude-account.py home --quiet # what a shell switch command runs
    accounts/cursor-claude-account.py --move-open  # move open conversations (idle or all?) and continue the cut ones
    accounts/cursor-claude-account.py --off        # remove the managed settings (back to the launch env)

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_peer  # noqa: E402  (sibling module: the peer-socket delivery)
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)

HOME = Path.home()
SETTINGS = Path(os.environ.get("CURSOR_USER_SETTINGS",
                               HOME / "Library" / "Application Support" / "Cursor" / "User" / "settings.json"))
WRAPPER = Path(__file__).resolve().parent / "claude-cursor-wrapper.sh"
ENV_KEY = "claudeCode.environmentVariables"
WRAPPER_KEY = "claudeCode.claudeProcessWrapper"
# The binary the extension bundles; T3 Code and terminals run other paths.
EXTENSION_BINARY = "/anthropic.claude-code-[^/]*/resources/native-binary/claude"

CFG = accounts_config.CONFIG
ACCOUNTS = {a.tag: {"dir": a.dir, "unset": a.default, "email": a.email} for a in CFG.accounts}

# Companion Cursor extension (accounts/relaunch): reopens a
# tab whose process ended, so the tab starts a new process on the new account by itself.
RELAUNCH_ROOT = Path(os.environ.get("CLAUDE_RELAUNCH_DIR", HOME / ".local" / "state" / "cursor-claude-relaunch"))
POOL_PROJECTS = CFG.pool / "projects"
TAIL_BYTES = 256 * 1024
CONTINUE_TEXT = os.environ.get(
    "CLAUDE_MOVE_CONTINUE_TEXT",
    "continúa con lo que estabas haciendo: esta conversación se ha movido a la cuenta {tag} de Claude "
    "(cambio de cuenta) y puede que tu turno anterior quedara cortado. Para moverla se cerró tu proceso, así que los "
    "subagentes y las tareas en segundo plano que tuvieras en marcha también se han parado: revisa cuáles "
    "no terminaron (su salida está en sus ficheros de tarea) y reanúdalos o relánzalos antes de seguir. "
    "Si en realidad ya habías terminado y estabas esperando al usuario, no empieces nada nuevo: di solo "
    "que sigues esperando. Es un aviso automático del cambio de cuenta, no una aprobación nueva.")


def tag_for_dir(cfg: str) -> str:
    """Account tag of a CLAUDE_CONFIG_DIR value: empty is the default account, an unknown dir
    is returned as is."""
    if not cfg:
        return CFG.default_tag
    for tag, acc in ACCOUNTS.items():
        if not acc["unset"] and Path(cfg).resolve() == acc["dir"].resolve():
            return tag
    return cfg


def load_settings() -> dict:
    return json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}


def write_settings(data: dict) -> None:
    # Same-directory temp + rename: Cursor's file watcher never sees a half-written file.
    tmp = SETTINGS.with_name(f".{SETTINGS.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=4, ensure_ascii=False) + "\n")
    os.replace(tmp, SETTINGS)


def env_entries(settings: dict) -> list[dict]:
    """The setting accepts a list of {name, value} or an object; normalise to the list."""
    raw = settings.get(ENV_KEY, [])
    if isinstance(raw, dict):
        return [{"name": k, "value": "" if v is None else str(v)} for k, v in raw.items()]
    return [e for e in raw if isinstance(e, dict) and "name" in e]


def configured_tag(settings: dict) -> str:
    value = next((e.get("value", "") for e in env_entries(settings) if e["name"] == "CLAUDE_CONFIG_DIR"), "")
    # Any copy of this wrapper counts (an older install path, a wrapper script at another
    # location): only its job matters, and the next switch writes this one's path.
    wrapper = Path(str(settings.get(WRAPPER_KEY) or ""))
    if not value or wrapper.name != WRAPPER.name or not wrapper.is_file():
        return "not managed (launch env, needs a Cursor restart)"
    for tag, acc in ACCOUNTS.items():
        if Path(value).expanduser().resolve() == acc["dir"].resolve():
            return tag
    return f"custom ({value})"


def process_command(pid: int) -> str:
    return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()


def live_sessions() -> list[dict]:
    """Extension Claude processes: pid, account tag, and what <config dir>/sessions/<pid>.json says."""
    # -a: macOS pgrep skips its own ancestors otherwise, which hides the session running this script.
    out = subprocess.run(["pgrep", "-a", "-f", EXTENSION_BINARY], capture_output=True, text=True).stdout
    sessions = []
    for pid in (int(p) for p in out.split()):
        cmd = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
        cfg = next((t.split("=", 1)[1] for t in cmd.split() if t.startswith("CLAUDE_CONFIG_DIR=")), "")
        tag = tag_for_dir(cfg)
        info = {}
        state_dir = ACCOUNTS[tag]["dir"] if tag in ACCOUNTS else Path(cfg)
        try:
            info = json.loads((state_dir / "sessions" / f"{pid}.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        sessions.append({"pid": pid, "tag": tag, "status": info.get("status", "?"),
                         "name": info.get("name", ""), "session": info.get("sessionId", ""),
                         "started_ms": int(info.get("startedAt") or 0),
                         "status_ms": int(info.get("statusUpdatedAt") or 0),
                         "command": process_command(pid), "host": parent_pid(pid)})
    return sessions


def parent_pid(pid: int) -> int:
    """The extension host that spawned a tab's Claude process (one per Cursor window)."""
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---- Continue after the move ------------------------------------------------------------

def transcript(session: str) -> Path | None:
    hits = list(POOL_PROJECTS.glob(f"*/{session}.jsonl")) if session else []
    return hits[0] if hits else None


def tail_entries(path: Path) -> list[dict]:
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(max(0, size - TAIL_BYTES))
        lines = fh.read().decode("utf-8", errors="replace").splitlines()
    if size > TAIL_BYTES:
        lines = lines[1:]  # probably cut
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def _is_local_entry(e: dict) -> bool:
    """A local slash command or its output (/usage, /context, /login...): nothing the
    model answers, so it does not end or extend a turn."""
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    text = _text(msg.get("content"))[:300]
    if "<local-command-" in text or text.lstrip().startswith("[Request interrupted"):
        return True
    cmd = COMMAND_NAME_RE.search(text)
    return bool(cmd and cmd.group(1).lower() in LOCAL_COMMANDS)


def _is_task_notification(e: dict) -> bool:
    """A background task's notice delivered as a user entry (`<task-notification>`)."""
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    return e.get("type") == "user" and _text(msg.get("content")).lstrip().startswith("<task-notification>")


UNATTENDED_ERROR_MAX_AGE_HOURS = 8  # for unattended moves an older API error is history
# None: any age (a manual move, chosen by the owner, continues a tab cut days ago on a weekly
# limit); move_open sets it for the unattended scopes safe / safe-cut / safe-quiet (audit of auto
# switch release 11).
ERROR_MAX_AGE_HOURS: float | None = None
POLICY_ERROR = ("usage policy", "violate", "safeguard")


def _last_turn_error(session: str) -> dict | None:
    """The last main-chain turn when it is a recent API error that says work was cut: not
    older than ERROR_MAX_AGE_HOURS (unattended moves only) and not a usage-policy refusal (audit of auto switch
    release 10: an abandoned tab with yesterday's error was woken by an automatic switch).
    Local commands and meta entries after it do not hide it (audit of release 11), nor the
    task notifications of subagents that failed on the same limit (auto switch release 42:
    they made two cut tabs look uncut, so their dead subagents kept them on the spent
    account). Pending input is judged apart, by work_in_flight."""
    path = transcript(session)
    if not path:
        return None
    try:
        convo = [e for e in tail_entries(path)
                 if e.get("type") in ("user", "assistant") and not e.get("isSidechain")
                 and not e.get("isMeta") and not _is_local_entry(e) and not _is_task_notification(e)]
    except OSError:
        return None
    if not convo or convo[-1].get("type") != "assistant" or not convo[-1].get("isApiErrorMessage"):
        return None
    last = convo[-1]
    when = _ms(last.get("timestamp") or "")
    if (when and ERROR_MAX_AGE_HOURS is not None
            and time.time() * 1000 - when > ERROR_MAX_AGE_HOURS * 3600 * 1000):
        return None  # (real entries always carry a timestamp; one without is not judged by age)
    text = _text((last.get("message") or {}).get("content")).lower()
    if any(p in text for p in POLICY_ERROR):
        return None
    return last


def old_error(session: str) -> bool:
    """The last turn is a cut-type API error that only the age limit ruled out."""
    global ERROR_MAX_AGE_HOURS
    keep, ERROR_MAX_AGE_HOURS = ERROR_MAX_AGE_HOURS, None
    try:
        return _last_turn_error(session) is not None
    finally:
        ERROR_MAX_AGE_HOURS = keep


def ended_on_limit(session: str) -> bool:
    """The chat's last turn is a recent usage-limit error (not any API error: an overload or
    a 500 says nothing about the account's quota)."""
    last = _last_turn_error(session)
    if not last:
        return False
    text = _text((last.get("message") or {}).get("content")).lower()
    return (last.get("error") == "rate_limit" or last.get("apiErrorStatus") == 429) and "limit" in text


# Last time a tab of each account that had stopped on the usage limit was moved, read by
# claude-auto-switch.py to tell a manual move off a spent account from a choice (audit of
# auto switch release 23: a transcript scan could not say which account an error was on).
CUT_MOVES = HOME / ".local/state/claude-auto-switch/cut-moves.json"


def _cut_at(entry) -> str:
    """The error time of a CUT_MOVES entry: {"at", "text"}, or a bare time (release 24)."""
    return str(entry.get("at", "")) if isinstance(entry, dict) else str(entry or "")


def record_cut(newest: dict[str, dict]) -> None:
    """Merge {tag: {"at": limit error time, "text": its message}} into CUT_MOVES, keeping
    the newest per account, under a lock (a follow-up move and a manual claude-cursor-move
    can run at once). The text carries the reset time it names (audit of auto switch
    release 25: a window already reset still counted)."""
    if not newest:
        return
    import fcntl
    try:
        CUT_MOVES.parent.mkdir(parents=True, exist_ok=True)
        with CUT_MOVES.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(CUT_MOVES.read_text())
                data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                data = {}
            for tag, entry in newest.items():
                if _cut_at(entry) > _cut_at(data.get(tag)):
                    data[tag] = entry
            tmp = CUT_MOVES.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, CUT_MOVES)
    except OSError:
        pass


def note_cut_moves(ended: list[dict]) -> None:
    """Record {tag: newest limit error time} for the ended tabs that stopped on the limit
    while on that account: an error from before the process started was hit on the
    account it came from (audit of auto switch release 24)."""
    newest: dict[str, dict] = {}
    for s in ended:
        if s.get("tag") in ACCOUNTS and s.get("session") and ended_on_limit(s["session"]):
            err = _last_turn_error(s["session"]) or {}
            ts = err.get("timestamp") or ""
            if ts and _ms(ts) >= (s.get("started_ms") or 0) and ts > _cut_at(newest.get(s["tag"])):
                newest[s["tag"]] = {"at": ts, "text": _text((err.get("message") or {}).get("content"))[:300]}
    record_cut(newest)


def ended_on_error(session: str) -> bool:
    """True when the chat's last turn is a recent API error (the usage limit, an
    overload...): the work stopped, nobody is waiting for the owner."""
    return _last_turn_error(session) is not None


TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
TASK_STATUS_RE = re.compile(r"<status>([a-z_]+)</status>")
SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.S)
TOOL_USE_ID_RE = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")
# The id each background tool reports in its result (formats read from the pool, 2.1.318).
LAUNCH_ID_RES = {
    # Only asynchronous launches: a synchronous Agent result also carries an agentId but has
    # already finished (audit of release 9).
    "Agent": re.compile(r"Async agent launched.*?agentId: ([A-Za-z0-9]+)", re.S),
    # run_in_background, or a foreground command moved there by its timeout.
    # Ctrl+B by hand writes "manually backgrounded by user with ID: X".
    "Bash": re.compile(r"(?:background with ID: |moved to the background \(ID: |backgrounded by user with ID: )"
                       r"([A-Za-z0-9]+)"),
    "Monitor": re.compile(r"Monitor started \(task ([A-Za-z0-9]+)"),
    "Workflow": re.compile(r"Task ID: ([A-Za-z0-9]+)"),
}
RESUMED_RE = re.compile(r'"resumedAgentId":\s*"([A-Za-z0-9]+)"')
FINISHED = ("completed", "failed", "killed", "stopped", "cancelled", "error")
# A "completed" that is not the end yet, and a "failed" that only needs a resume.
PROVISIONAL = "waiting on its own background work"
LIMIT_FAILURE = re.compile(r"hit your .{0,20}limit|API error|rate.?limit", re.I)
QUOTED = re.compile(r'"[^"]*"|`[^`]*`')


def _without_tool_output(e: dict) -> dict:
    """The entry without tool results (their text is command output, not a notice)."""
    e = {k: v for k, v in e.items() if k != "toolUseResult"}
    msg = e.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        e["message"] = {**msg, "content": [c for c in msg["content"]
                                           if not (isinstance(c, dict) and c.get("type") == "tool_result")]}
    return e


def limit_failure(summary: str) -> bool:
    """A failure summary that says the work stopped on the limit. Quoted text (a command
    line, a task name) is left out: `grep "rate limit"` exiting 1 is not cut work (audit
    of auto switch release 31)."""
    text = QUOTED.sub("", summary)
    # A usage-policy refusal is not a cut to resume (audit of auto switch release 32, as
    # _last_turn_error does for the tab itself since release 11).
    return bool(LIMIT_FAILURE.search(text)) and not any(p in text.lower() for p in POLICY_ERROR)
# Not something the model owes an answer to. A slash command the model answers
# (<command-name> alone) is an input; a local one writes <local-command-stdout>.
NOT_AN_INPUT = ("[Request interrupted", "<local-command-")
# Slash commands the CLI handles itself; /usage and /context write no local-command
# output, only an isMeta caveat (audit of release 10).
LOCAL_COMMANDS = {"usage", "context", "cost", "status", "help", "config", "model", "mcp", "memory",
                  "clear", "login", "logout", "doctor", "permissions", "agents", "hooks", "resume",
                  "exit", "ide", "terminal-setup", "vim", "theme", "release-notes", "add-dir", "fast",
                  "effort", "rename", "rewind", "export", "statusline", "privacy-settings", "upgrade"}
HANDBACK_MARK = "[Subagent hand-back]"


def handed_back_by(e: dict) -> str | None:
    """The agent id when this entry is a subagent's final report delivered as a message.
    Read from the structured origin the CLI writes, never from text search: an interim
    message or a quoted transcript that mentions the marker would otherwise settle a
    running agent (audit of release 13). Two shapes: a user entry with `origin`, and,
    when the report arrives mid-turn, only an attachment of type queued_command with
    `attachment.origin` (audit of release 14). The CLI flags hand-backs with
    origin.handback; the body prefix (the marker, or auto mode's SECURITY WARNING line
    before it) covers entries written without that flag."""
    if e.get("type") == "user":
        origin = e.get("origin")
    elif e.get("type") == "attachment" and isinstance(e.get("attachment"), dict) \
            and e["attachment"].get("type") == "queued_command":
        origin = e["attachment"].get("origin")
    else:
        return None
    if not isinstance(origin, dict) or origin.get("kind") != "peer" or not origin.get("senderTaskId"):
        return None
    body = str(origin.get("body") or "").lstrip()
    if origin.get("handback") is True or body.startswith(HANDBACK_MARK) \
            or (body.startswith("SECURITY WARNING") and HANDBACK_MARK in body[:800]):
        return str(origin["senderTaskId"])
    return None


COMMAND_NAME_RE = re.compile(r"<command-name>/?([A-Za-z0-9:_-]+)</command-name>")
MONITOR_EXPIRES_RE = re.compile(r"expires in (\d+)\s*m")
WAKEUP_RE = re.compile(r"scheduled for [0-9:]+ \(in (\d+)s\)")
WORKFLOW_NAME_RE = re.compile(r"""name:\s*['"]([^'"]+)['"]""")


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(_text(c.get("text") if isinstance(c, dict) and "text" in c else
                              c.get("content") if isinstance(c, dict) else c) for c in content)
    return "" if content is None else str(content)


def _ms(ts: str) -> int:
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, AttributeError):
        return 0


def _describe(name: str, inp: dict) -> str:
    what = inp.get("description") or inp.get("reason") or str(inp.get("command") or inp.get("prompt") or "")[:50]
    if name == "Workflow":
        m = WORKFLOW_NAME_RE.search(str(inp.get("script") or ""))
        what = m.group(1) if m else Path(str(inp.get("scriptPath") or "")).stem
    label = {"Agent": "subagente", "Bash": "comando en segundo plano", "Monitor": "monitor",
             "Workflow": "workflow", "ScheduleWakeup": "despertador programado"}.get(name, name)
    return f'{label} "{what}"' if what else label


def work_in_flight(session: str, since_ms: int, now_ms: int | None = None) -> dict:
    """What the session still had going when its process (started at `since_ms`) was ended,
    from its transcript: {"pending": why its turn was not over, or "", "tasks": [...]}.

    2026-11-12: a chat got task notifications seconds before the switch, so its last
    entry was an unanswered notification, not the limit error; it was taken for finished
    and never told to continue. Its subagents had stopped with the process.

    Only what belongs to the ended process counts: queue operations, inputs and launches
    since its start (queued input only when nothing was answered after it), plus subagents
    that reported during it a provisional "completed" (until their final notice) or a
    "failed" on the usage limit, as long as the session did not close a normal turn after
    that failure (it then decided what to do with the agent). Work of an earlier process died with it, and counting it would wake
    finished sessions (audits of releases 8 and 9). Without a known start nothing is
    inferred. Residual mistakes are softened by the message itself (continue_text)."""
    out = {"pending": "", "tasks": []}
    path = transcript(session)
    if not path or since_ms <= 0:
        return out
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    tasks: dict[str, dict] = {}      # task id -> {"desc", "alive", "soft", "expires"}
    pending_use: dict[str, tuple[str, str]] = {}  # tool_use id -> (tool name, description)
    wakeup_ms = 0                    # when a ScheduleWakeup of this process would fire
    handed_back: set[str] = set()    # agents whose final report came back as a message
    last_input = None
    answered = True
    queued = 0
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(e, dict) or e.get("isSidechain"):
                continue
            ts = e.get("timestamp") or ""
            ts_ms = _ms(ts) if ts else 0
            recent = ts_ms >= since_ms
            kind = e.get("type")
            agent_id = handed_back_by(e)
            if agent_id:
                # The final report came back as a message: the agent is done, even when its
                # only notices were provisional (audit of release 11).
                handed_back.add(agent_id)
            att = e.get("attachment") if isinstance(e.get("attachment"), dict) else {}
            quoting = kind == "assistant" or (kind == "attachment"
                                              and att.get("type") not in ("queued_command", "prompt_snapshot"))
            if "<task-notification>" in line and not quoting:
                # Delivered mid-turn they only show up as queue operations or attachments.
                # Tool output and the toolUseResult copy are left out: a grep over other
                # transcripts quotes their notifications (audit of auto switch release 33).
                blob = json.dumps(_without_tool_output(e), ensure_ascii=False)
                for block in blob.split("<task-notification>")[1:]:
                    tid, st = TASK_ID_RE.search(block), TASK_STATUS_RE.search(block)
                    if tid and not st and "Monitor expired" in block:
                        tasks.setdefault(tid.group(1), {"desc": ""})["alive"] = False
                        continue
                    if not (tid and st and st.group(1) in FINISHED):
                        continue  # a Monitor event: nothing ends
                    summary = (SUMMARY_RE.search(block) or SUMMARY_RE.search(block + "</summary>"))
                    summary = summary.group(1) if summary else block[:400]
                    task = tasks.setdefault(tid.group(1), {"desc": f"tarea {tid.group(1)[:8]}"})
                    if st.group(1) == "completed" and PROVISIONAL in summary:
                        # Still running (waiting on its own work) until its final notice; the
                        # session usually ends its turn right after saying so (audit of
                        # release 10), so a turn end does not settle it.
                        task.update(alive=task.get("alive") or recent, soft=False)
                    elif st.group(1) == "failed" and limit_failure(summary):
                        task.update(alive=task.get("alive") or recent, soft=True)
                        task["desc"] = task["desc"].replace(" (cortado por el límite)", "") + " (cortado por el límite)"
                    else:
                        task.update(alive=False, soft=False)
            if kind == "queue-operation":
                if recent:
                    op = e.get("operation")
                    if op == "enqueue":
                        queued += 1
                    elif op in ("dequeue", "remove"):
                        queued = max(0, queued - 1)
                continue
            peer_input = kind == "user" and e.get("isMeta") and \
                isinstance(e.get("origin"), dict) and e["origin"].get("kind") == "peer"
            # A message from another session or a subagent is written as isMeta, but it is
            # an input the session owes a turn to (audit of release 16).
            if kind not in ("user", "assistant") or (e.get("isMeta") and not peer_input):
                continue
            msg = e.get("message") if isinstance(e.get("message"), dict) else {}
            content = msg.get("content")
            if kind == "assistant":
                answered = True
                # It kept going after those inputs: they were taken, logged or not (a
                # notification absorbed mid-turn may never get its dequeue line).
                queued = 0
                if msg.get("stop_reason") == "end_turn" and not e.get("isApiErrorMessage"):
                    # A normal turn end after a limit failure: the session already decided
                    # about that agent (relaunched or dropped it).
                    for task in tasks.values():
                        if task.get("soft"):
                            task.update(alive=False, soft=False)
                for c in content if isinstance(content, list) else []:
                    if not isinstance(c, dict) or c.get("type") != "tool_use":
                        continue
                    name, inp = c.get("name"), c.get("input") if isinstance(c.get("input"), dict) else {}
                    if name in LAUNCH_ID_RES or name == "ScheduleWakeup":
                        pending_use[c.get("id", "")] = (name, _describe(name, inp))
                    if name == "TaskStop" and inp.get("task_id"):
                        tasks.setdefault(str(inp["task_id"]), {"desc": ""})["alive"] = False
                continue
            # user entries
            text = _text(content)
            results = [c for c in content if isinstance(c, dict) and c.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            for c in results:
                use = pending_use.pop(c.get("tool_use_id", ""), None)
                if not use:
                    continue
                name, desc = use
                rtext = _text(c.get("content"))
                if name == "ScheduleWakeup":
                    m = WAKEUP_RE.search(rtext)
                    if m:
                        wakeup_ms = ts_ms + int(m.group(1)) * 1000 if recent else 0
                    elif "Loop stopped" in rtext or "cancelled" in rtext:
                        wakeup_ms = 0
                    continue
                m = LAUNCH_ID_RES[name].search(rtext)
                if not m:
                    continue
                task = {"desc": desc, "alive": recent, "soft": False}
                if name == "Monitor":
                    exp = MONITOR_EXPIRES_RE.search(rtext)
                    task["expires"] = ts_ms + int(exp.group(1)) * 60000 if exp else 0
                tasks[m.group(1)] = task
            r = RESUMED_RE.search(text)
            if r and recent:
                task = tasks.setdefault(r.group(1), {"desc": f"subagente {r.group(1)[:8]}"})
                task.update(alive=True, soft=False)
                handed_back.discard(r.group(1))  # resumed after its report: running again
            if results:
                # A tool result with no assistant entry after it: the turn was mid-way.
                last_input = e
                answered = False
            else:
                # A real input (prompt, notification or peer message), not a tool result.
                cmd = COMMAND_NAME_RE.search(text[:300])
                if any(text.lstrip().startswith(x) or x in text[:200] for x in NOT_AN_INPUT) \
                        or (cmd and cmd.group(1).lower() in LOCAL_COMMANDS):
                    answered = True  # an interruption or a local command: nothing to answer
                    continue
                last_input = e
                answered = False
    if not answered and last_input is not None and _ms(last_input.get("timestamp") or "") >= since_ms:
        out["pending"] = "its last input had no answer yet"
    elif queued > 0:
        out["pending"] = f"{queued} queued input(s) not taken yet"
    out["tasks"] = [t["desc"] for tid, t in tasks.items()
                    if t.get("alive") and tid not in handed_back and t.get("desc") and (not t.get("expires") or t["expires"] > now_ms)]
    if wakeup_ms > now_ms:
        out["tasks"].append("despertador programado (ScheduleWakeup) que ya no sonará")
    return out


def continue_text(tag: str, flight: dict) -> str:
    """The message for a cut session: account switch, subagents, what to do."""
    text = CONTINUE_TEXT.format(tag=tag)
    acc = ACCOUNTS.get(tag)
    if acc and claude_peer.bypass_disabled(acc["dir"]):
        text += (f" En la cuenta {tag} no hay modo bypass: relanza los subagentes y comandos en modo auto, "
                 "sin pedir bypassPermissions.")
    tasks = flight.get("tasks") or []
    if tasks:
        shown = ", ".join(tasks[:8]) + (f" y {len(tasks) - 8} más" if len(tasks) > 8 else "")
        text += f" Según tu transcript, quedaron sin terminar: {shown}."
    return text


def session_titles(session: str, name: str) -> list[str]:
    """Candidates for the tab label: /rename title, AI title, first prompt, process name.
    The companion only uses them when the tab cannot be told apart by revealing it."""
    path = transcript(session)
    custom = ai = first = ""
    if path:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"custom-title"' in line or '"ai-title"' in line:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        custom = d.get("customTitle") or custom
                        ai = d.get("aiTitle") or ai
                    elif not first and '"user"' in line:
                        try:
                            d = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        content = d.get("message", {}).get("content") if d.get("type") == "user" else None
                        if isinstance(content, str) and not d.get("isMeta") and not content.startswith("<"):
                            first = content.strip().splitlines()[0] if content.strip() else ""
        except OSError:
            pass
    return [t for t in (custom, ai, first, name) if t]


COMPANION_ID = CFG.companion_id
CURSOR_EXTENSIONS = HOME / ".cursor" / "extensions" / "extensions.json"


def installed_companion() -> str | None:
    """The companion version Cursor has installed, i.e. what a window reload loads. Not the
    repo's package.json: that can be ahead of what was installed (audit of release 8)."""
    try:
        exts = json.loads(CURSOR_EXTENSIONS.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for e in exts if isinstance(exts, list) else []:
        if isinstance(e, dict) and (e.get("identifier") or {}).get("id") == COMPANION_ID:
            return e.get("version")
    return None


def stale_companions() -> list[tuple[int, str, str]]:
    """Windows whose running companion is older than the installed one: (pid, runs, installed).
    An install only takes effect after Developer: Reload Window; until then that window
    keeps the old behaviour (2026-11-12: it still ran 0.1.0, so the tab order was not
    restored)."""
    want = installed_companion()
    if not want:
        return []
    out = []
    for f in (RELAUNCH_ROOT / "windows").glob("*.json"):
        if not (f.stem.isdigit() and pid_alive(int(f.stem))):
            continue
        try:
            runs = json.loads(f.read_text()).get("companion") or "0.2.0 or older"
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        if runs != want:
            out.append((int(f.stem), runs, want))
    return out


def warn_stale_companions() -> None:
    for pid, runs, want in stale_companions():
        print(f"⚠️  Cursor window {pid} runs the tab companion {runs}, installed {want}: tab order and the "
              "latest fixes apply only after Developer: Reload Window in it (when no tab is working)")


def companion_hosts() -> set[int]:
    """Extension hosts (Cursor windows) where the companion extension is listening."""
    hosts = set()
    for f in (RELAUNCH_ROOT / "windows").glob("*.json"):
        if f.stem.isdigit() and pid_alive(int(f.stem)):
            hosts.add(int(f.stem))
    return hosts


def request_reopen(tag: str, moved: list[dict], timeout: float | None = None) -> dict[str, dict]:
    """Ask each window's companion to reopen its moved tabs. session -> its result entry
    ({"result": "reopened" | "ambiguous" | ..., "position": "kept" | "restored" | "not-moved"})."""
    by_host: dict[int, list[dict]] = {}
    for s in moved:
        by_host.setdefault(s["host"], []).append(s)
    # Run from Cursor's own terminal (one window): the companion gives the focus back to it
    # after putting the tabs back in place, which needs the editor groups focused.
    refocus = os.environ.get("TERM_PROGRAM") == "vscode" and len(companion_hosts()) <= 1
    pending = {}
    for host, group in by_host.items():
        rid = f"{int(time.time())}-{os.getpid()}-{host}"
        inbox = RELAUNCH_ROOT / "requests" / str(host)
        inbox.mkdir(parents=True, exist_ok=True)
        body = {"id": rid, "created_at": time.time(), "expect_config_dir": str(ACCOUNTS[tag]["dir"]),
                "refocus_terminal": refocus,
                "ops": [{"session": s["session"], "titles": session_titles(s["session"], s["name"])} for s in group]}
        tmp = inbox / f".{rid}.tmp"
        tmp.write_text(json.dumps(body, ensure_ascii=False))
        os.replace(tmp, inbox / f"{rid}.json")
        pending[rid] = group
    results: dict[str, dict] = {}
    # The companion waits up to 20 s for the window to see the new setting, then ~2 s per tab.
    deadline = time.monotonic() + (25 + 3 * len(moved) if timeout is None else timeout)
    while pending and time.monotonic() < deadline:
        for rid in list(pending):
            resp = RELAUNCH_ROOT / "responses" / f"{rid}.json"
            if not resp.exists():
                continue
            try:
                data = json.loads(resp.read_text())
            except (OSError, json.JSONDecodeError):
                continue  # half-written; next round
            resp.unlink(missing_ok=True)
            for r in data.get("results", []):
                if r.get("session"):
                    results[r["session"]] = r
            del pending[rid]
        time.sleep(0.3)
    for group in pending.values():
        for s in group:
            results.setdefault(s["session"], {"result": "no answer from the window"})
    return results


def wait_new_process(session: str, tag: str, old_pid: int, seconds: float = 20.0) -> dict | None:
    """The process the reopened tab launched: same session, the new account, a socket."""
    deadline = time.monotonic() + seconds
    sessions_dir = ACCOUNTS[tag]["dir"] / "sessions"
    while time.monotonic() < deadline:
        for f in sessions_dir.glob("*.json"):
            if not f.stem.isdigit() or int(f.stem) == old_pid:
                continue
            try:
                info = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if info.get("sessionId") == session and info.get("messagingSocketPath") and pid_alive(int(f.stem)):
                return info
        time.sleep(0.4)
    return None


def send_continue(info: dict, text: str, tag: str) -> str | None:
    """Deliver one message through the process's peer socket (claude_peer.py: the envelope
    that attests the permission class, then a check that the session took it). None on
    success, else why not. The CLI presents it as a message from another session, never as
    the owner typing it, so it is not an approval: every gate the chat follows stays in force."""
    ok, detail = claude_peer.deliver(info, text, ACCOUNTS[tag]["dir"] / "sessions",
                                     transcript(str(info.get("sessionId", ""))))
    return None if ok else detail


# Machine-readable outcome of a --move-open run, one entry per tab: {name, session,
# outcome, cut}. Printed as one "MOVE-SUMMARY <json>" line with --summary (used by
# claude-auto-switch.py instead of parsing the human lines; audit of auto switch release 15).
SUMMARY: list[dict] = []


def record(s: dict, outcome: str) -> None:
    SUMMARY.append({"name": label_of(s), "session": s.get("session", ""), "outcome": outcome,
                    "cut": bool(s.get("cut"))})


def mark_cut(s: dict) -> None:
    """Work out whether a moved tab's work was cut: one definition for every continue mode
    (audit of auto switch release 18: `--continue off` used a narrower one)."""
    s["flight"] = work_in_flight(s["session"], s.get("started_ms") or 0)
    s["cut"] = (s["status"] in ACTIVE or ended_on_error(s["session"])
                or bool(s["flight"]["pending"]) or bool(s["flight"]["tasks"]))


def continue_moved(tag: str, moved: list[dict], mode: str) -> None:
    """Reopen the moved tabs through the companion, then send "continúa" to the cut ones."""
    hosts = companion_hosts()
    # Cut sessions first: waiting for a tab whose new process never shows up takes 20 s,
    # and the ones that must carry on should not wait behind it.
    for s in moved:
        mark_cut(s)
    moved = sorted(moved, key=lambda s: not s["cut"])
    reachable = [s for s in moved if s["session"] and s["host"] in hosts]
    for s in moved:
        if s not in reachable:
            why = "no session id" if not s["session"] else "companion extension not running in that window"
            print(f"ℹ️ {label_of(s)}: type in its tab and it continues on {tag} ({why})")
            record(s, "not_reopened")
    if not reachable:
        return
    print(f"🔄 Reopening {len(reachable)} tab(s) on {tag}...")
    results = request_reopen(tag, reachable)
    for s in reachable:
        entry = results.get(s["session"]) or {}
        res = entry.get("result", "?")
        if res != "reopened":
            print(f"⚠️  {label_of(s)}: not reopened ({res}); type in its tab and it continues on {tag}")
            record(s, "not_reopened")
            continue
        if entry.get("position") == "not-moved":
            print(f"↔️  {label_of(s)}: reopened, but could not be put back in its old place in the tab bar")
        info = wait_new_process(s["session"], tag, s["pid"])
        if not info:
            print(f"⚠️  {label_of(s)}: reopened, but no {tag} process showed up; type in its tab")
            record(s, "no_process")
            continue
        if not s["cut"]:
            if ERROR_MAX_AGE_HOURS is not None and old_error(s["session"]):
                print(f"⏸️  {label_of(s)}: on {tag}, stopped on an error over {ERROR_MAX_AGE_HOURS:g} h old; "
                      "type continúa in it if it should carry on")
                record(s, "old_error")
            else:
                print(f"✅ {label_of(s)}: on {tag}, waiting for you (its turn had finished)")
                record(s, "finished")
            continue
        if mode == "none":
            # Reopened without a message, but its work was cut: say so (audit of release 21).
            print(f"⚠️  {label_of(s)}: on {tag}, its work was cut; type continúa in it (--continue none)")
            record(s, "not_sent")
            continue
        err = send_continue(info, continue_text(tag, s["flight"]), tag)
        if err:
            print(f"⚠️  {label_of(s)}: on {tag}, but the continue message was not taken ({err}); type continúa in it")
            record(s, "not_taken")
        else:
            why = ("was working" if s["status"] == "busy" else
                   "was waiting on a prompt or a question" if s["status"] == "waiting" else s["flight"]["pending"] or
                   (f"{len(s['flight']['tasks'])} background task(s) stopped" if s["flight"]["tasks"] else "had stopped on an error"))
            print(f"▶️  {label_of(s)}: on {tag}, continuing ({why})")
            record(s, "continued")


def label_of(s: dict) -> str:
    return s["name"] or (s["session"][:8] if s["session"] else f"pid {s['pid']}")


def describe(s: dict) -> str:
    label = s["name"] or (s["session"][:8] if s["session"] else "unnamed")
    return f"pid {s['pid']}  account {s['tag']}  {s['status']:<7}  {label}"


def auth_email(tag: str) -> str | None:
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if not ACCOUNTS[tag]["unset"]:
        env["CLAUDE_CONFIG_DIR"] = str(ACCOUNTS[tag]["dir"])
    try:
        res = subprocess.run(["claude", "auth", "status"], env=env, capture_output=True, text=True, timeout=30)
        data = json.loads(res.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return data.get("email") if data.get("loggedIn") else None


def login_hint(tag: str) -> str:
    acc = ACCOUNTS[tag]
    return "(unset CLAUDE_CONFIG_DIR; claude /login)" if acc["unset"] else f"CLAUDE_CONFIG_DIR={acc['dir']} claude /login"


def verify(tag: str) -> int:
    """The config dir only picks a Keychain item: what it is logged in as is a separate fact.
    A /login inside a tab (or the extension's "Switch account") can leave the other
    account's login in it, and then a switch changes nothing."""
    email = auth_email(tag)
    if email != ACCOUNTS[tag]["email"]:
        print(f"❌ {tag} ({ACCOUNTS[tag]['dir']}) is logged in as {email or 'nobody'}, "
              f"not {ACCOUNTS[tag]['email']}: switching to it changes nothing.\n"
              f"   Fix, signing in with {ACCOUNTS[tag]['email']} in the browser: {login_hint(tag)}",
              file=sys.stderr)
        return 1
    return 0


def status() -> int:
    print(f"Cursor Claude account (settings): {configured_tag(load_settings())}")
    for tag, acc in ACCOUNTS.items():
        email = auth_email(tag)
        mark = "✅" if email == acc["email"] else f"❌ expected {acc['email']}, fix: {login_hint(tag)}"
        print(f"  login {tag}: {email or 'not logged in'}  {mark}")
    sessions = live_sessions()
    if not sessions:
        print("Live extension sessions: none")
    for s in sessions:
        print(f"  {describe(s)}")
    return 0


def switch(tag: str, quiet: bool, check: bool = True) -> int:
    acc = ACCOUNTS[tag]
    if not SETTINGS.parent.is_dir():
        if not quiet:
            print(f"ℹ️ Cursor is not installed ({SETTINGS.parent} missing), nothing to switch")
        return 0
    # Before the no-op shortcut: "already on mt" means nothing if mt holds the wrong login.
    if check and verify(tag):
        return 1
    if quiet and configured_tag(load_settings()) == tag:
        return 0

    settings = load_settings()
    entries = [e for e in env_entries(settings) if e["name"] != "CLAUDE_CONFIG_DIR"]
    entries.append({"name": "CLAUDE_CONFIG_DIR", "value": str(acc["dir"])})
    settings[ENV_KEY] = entries
    settings[WRAPPER_KEY] = str(WRAPPER)
    write_settings(settings)

    stale = [s for s in live_sessions() if s["tag"] != tag]
    if stale and not quiet:
        print(f"ℹ️ Cursor: {len(stale)} open conversation(s) still on the old account; "
              "claude-cursor-move moves them (they continue with your next message)")
    print(f"✅ Cursor on {tag} ({acc['email']}): new conversations use it, no restart")
    return 0


def ask_scope() -> str:
    """idle or all. Without a terminal to ask on (an agent, a script), only idle ones move."""
    if not sys.stdin.isatty():
        return "idle"
    try:
        answer = input("Move which? [i] only the idle ones without work of their own (default)   [a] all: also the "
                       "working ones (interrupts their turn) and idle ones running their own commands, monitors, "
                       "workflows, subagents or a wakeup (that work dies): ")
    except EOFError:
        return "idle"
    return "all" if answer.strip().lower() in ("a", "all", "t", "todas") else "idle"


def wait_gone(pids: list[int], seconds: float = 3.0) -> None:
    """Give ended processes a moment to exit, so the status printed next is current."""
    deadline = time.monotonic() + seconds
    while pids and time.monotonic() < deadline:
        time.sleep(0.2)
        pids = [p for p in pids if process_command(p)]


# A turn in progress: "busy", or "waiting" on a permission prompt or a question (the CLI
# maps requires_action to "waiting"). Ending such a tab interrupts it (audit of release 17).
ACTIVE = ("busy", "waiting")
UNATTENDED = ("safe", "safe-cut", "safe-quiet")
# safe-quiet: an idle tab counts as not in use after this long without a turn.
# 60: the status changes when a turn ends, not when the owner reads or types, so a short
# wait could close a tab with a half-written prompt (audit of auto switch release 21).
QUIET_MINUTES = float(os.environ.get("CLAUDE_MOVE_QUIET_MINUTES", "60"))


def quiet(s: dict, now_ms: int | None = None) -> bool:
    """Idle for QUIET_MINUTES at least (its status has not changed since)."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return bool(s.get("status_ms")) and now_ms - s["status_ms"] >= QUIET_MINUTES * 60_000


def unsafe_to_move(s: dict) -> list[str]:
    """Why an idle tab must not be moved unattended (empty: safe). Its own background
    commands, monitors, workflows or a pending wakeup die with the process. Its subagents
    too, unless the tab itself stopped on the usage limit (then they run on the same spent
    account and stop anyway; audit of auto switch release 2: once the window resets they
    run again). A tab whose state is unknown is left alone."""
    if not s.get("session") or not s.get("started_ms") or s.get("status") != "idle":
        return ["state unknown"]
    tasks = work_in_flight(s["session"], s["started_ms"])["tasks"]
    if ended_on_limit(s["session"]):
        # Work that already failed on the limit (a workflow, a command) is dead, not
        # running: it is named in the continue so the chat relaunches it (audit of auto
        # switch release 30: such a tab stayed on the spent account until its reset).
        tasks = [t for t in tasks if not t.startswith("subagente") and not t.endswith("(cortado por el límite)")]
    return tasks


def current_record(s: dict) -> dict:
    """The session's record now, from its account's sessions dir ({} when unreadable)."""
    acc = ACCOUNTS.get(s.get("tag"))
    if not acc:
        return {}
    try:
        info = json.loads((acc["dir"] / "sessions" / f"{s['pid']}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return info if isinstance(info, dict) else {}


def current_status(s: dict) -> str:
    """The session's status now, from its record in its account's sessions dir."""
    info = current_record(s)
    return str(info.get("status", "?")) if info else s.get("status", "?")


def move_open(scope: str, cont: str = "cut") -> int:
    """scope: ask (asks only when some are busy or waiting), idle, or all.
    cont: cut (reopen every moved tab and send "continúa" to those whose turn was cut),
    none (reopen, send nothing), off (only end the processes, the pre-companion way)."""
    global ERROR_MAX_AGE_HOURS
    # Every call sets it from its own scope: unattended scopes judge errors by age.
    ERROR_MAX_AGE_HOURS = UNATTENDED_ERROR_MAX_AGE_HOURS if scope in UNATTENDED else None
    tag = configured_tag(load_settings())
    if tag not in ACCOUNTS:
        print(f"❌ Cursor account is {tag}; switch the default account first")
        return 1
    warn_stale_companions()
    # CLAUDE_MOVE_ONLY_HOSTS (comma-separated extension host pids) limits the move to those
    # Cursor windows; the self-test uses it so it never touches the real windows.
    only = {int(h) for h in os.environ.get("CLAUDE_MOVE_ONLY_HOSTS", "").split(",") if h.strip().isdigit()}
    stale = [s for s in live_sessions() if s["tag"] != tag and (not only or s["host"] in only)]
    if not stale:
        print(f"✅ Every open Cursor conversation is already on {tag}")
        return 0
    print(f"Cursor conversations still on the other account (they will continue on {tag}):")
    for s in stale:
        print(f"  {describe(s)}")
    busy = [s for s in stale if s["status"] in ACTIVE]
    # Idle tabs running work of their own (commands, monitors, workflows, subagents, a
    # wakeup): the manual "idle only" leaves them, they would die with the process (audit
    # of auto switch release 34); --scope all still ends them.
    own_work: dict[int, list[str]] = {}
    if scope in ("ask", "idle"):
        for s in stale:
            if s not in busy:
                work = unsafe_to_move(s)
                if work and work != ["state unknown"]:
                    own_work[s["pid"]] = work
    if scope == "ask":
        # Ask when "idle" and "all" differ: a busy tab or one with its own work (audit of
        # auto switch release 35), saying which before the question (release 36).
        if busy or own_work:
            for s in stale:
                if s["pid"] in own_work:
                    print(f"  pid {s['pid']} runs its own work: {', '.join(own_work[s['pid']])[:120]}")
            scope = ask_scope()
        else:
            scope = "idle"
    targets = stale if scope == "all" else [s for s in stale if s not in busy]
    if scope == "idle":
        targets = [s for s in targets if s["pid"] not in own_work]
    else:
        own_work = {}
    if scope in UNATTENDED:
        # Unattended moves (claude-auto-switch.py): an idle tab may still run its own
        # background commands, monitors or workflows, which would die with the process.
        targets = [s for s in targets if not unsafe_to_move(s)]
        if scope == "safe-cut":
            # Only tabs that stopped on the limit (no longer used by the automatic switch,
            # kept for manual runs; audit of auto switch release 3).
            targets = [s for s in targets if ended_on_limit(s["session"])]
        elif scope == "safe-quiet":
            # Every automatic switch and follow-up move (auto switch releases 19, 28): tabs
            # stopped on the limit, or idle long enough not to be in use.
            targets = [s for s in targets if ended_on_limit(s["session"]) or quiet(s)]
    for s in stale:
        if s not in targets:
            why = (s["status"] if s in busy else
                   f"running its own work ({', '.join(own_work[s['pid']])[:120]}; --scope all ends it)"
                   if s["pid"] in own_work else
                   "not stopped on the usage limit" if scope == "safe-cut" and not unsafe_to_move(s)
                   else f"in use in the last {QUIET_MINUTES:.0f} min" if scope == "safe-quiet" and not unsafe_to_move(s)
                   else "running background work or of unknown state")
            print(f"⏭️  pid {s['pid']} is {why}, left on the old account (claude-cursor-move when it finishes)")
            record(s, "left")
    ended = []
    try:
        _end_targets(targets, ended, scope, cont, tag)
    finally:
        # Even when the loop broke half-way: the tabs already ended were moved.
        note_cut_moves(ended)  # before the continue adds turns after the error
    wait_gone([s["pid"] for s in ended])
    if cont != "off" and ended:
        continue_moved(tag, ended, cont)
    return 0


def _end_targets(targets: list[dict], ended: list[dict], scope: str, cont: str, tag: str) -> None:
    """End each target's process (SIGTERM), appending the ones ended to `ended`."""
    for s in targets:
        # The PID must still be the same extension process: never signal a reused PID.
        if process_command(s["pid"]) != s["command"]:
            print(f"⏭️  pid {s['pid']} changed since it was listed, skipped")
            record(s, "gone")
            continue
        # The status was read before the question: a tab may have started a turn while
        # the owner answered (a notification or a peer message). Read it again (audit of
        # release 16): with "idle only" it is left alone, otherwise it counts as busy.
        now_status = current_status(s)
        if now_status in ACTIVE and s["status"] not in ACTIVE and scope != "all":
            record(s, "left")
            print(f"⏭️  pid {s['pid']} started working meanwhile, left on the old account "
                  "(claude-cursor-move when it finishes)")
            continue
        if now_status in ACTIVE or now_status == "idle":
            # Both ways: a tab that finished meanwhile is decided by its transcript, one
            # that started counts as working (audit of release 17).
            s["status"] = now_status
        if scope == "safe-quiet" and not ended_on_limit(s["session"]):
            # Quiet is read again too: a turn started and finished since the listing means
            # the tab was just used (audit of auto switch release 38).
            fresh = int(current_record(s).get("statusUpdatedAt") or 0)
            if fresh and fresh != s.get("status_ms") and not quiet({"status_ms": fresh}):
                record(s, "left")
                print(f"⏭️  pid {s['pid']} was used meanwhile, left on the old account")
                continue
        if scope != "all" and s["status"] == "idle":
            # Its own work is read again too: a turn run while the owner answered may have
            # started a background command or a monitor (audit of auto switch release 36).
            work = unsafe_to_move(s)
            # Unattended scopes never move an unknown state (defensive: a target there had a
            # known one at listing time); a manual idle move does.
            if work and (scope in UNATTENDED or work != ["state unknown"]):
                record(s, "left")
                print(f"⏭️  pid {s['pid']} started its own work meanwhile ({', '.join(work)[:120]}), left on the "
                      "old account (claude-cursor-move when it finishes)")
                continue
        try:
            os.kill(s["pid"], signal.SIGTERM)
            ended.append(s)
            if cont == "off":
                print(f"✅ pid {s['pid']} ended: type in its tab and it continues on {tag}")
                # continue_moved, which works out the cut flag, does not run here.
                mark_cut(s)
                record(s, "ended")
        except ProcessLookupError:
            print(f"ℹ️ pid {s['pid']} had already ended")
            record(s, "already_ended")


def off() -> int:
    settings = load_settings()
    entries = [e for e in env_entries(settings) if e["name"] != "CLAUDE_CONFIG_DIR"]
    if entries:
        settings[ENV_KEY] = entries
    else:
        settings.pop(ENV_KEY, None)
    settings.pop(WRAPPER_KEY, None)
    write_settings(settings)
    print("✅ Cursor back to its launch environment (claude-cursor-restart to switch)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch the Claude account of Cursor's Claude extension.")
    parser.add_argument("account", nargs="?", choices=sorted(ACCOUNTS), help="omit for status")
    parser.add_argument("--quiet", action="store_true",
                        help="silent no-op when Cursor is missing or already on that account (zsh switcher)")
    parser.add_argument("--move-open", action="store_true",
                        help="end open conversations still on the other account, so they resume on this one")
    parser.add_argument("--scope", choices=["ask", "idle", "all", "safe", "safe-cut", "safe-quiet"], default="ask",
                        help="with --move-open: ask (default, only when some are busy or waiting), idle, all, safe (idle and no background work of its own: for unattended moves), safe-cut (safe and stopped on the usage limit) or safe-quiet (safe and stopped on the limit or idle for CLAUDE_MOVE_QUIET_MINUTES, 60)")
    parser.add_argument("--continue", dest="cont", choices=["cut", "none", "off"],
                        default=os.environ.get("CLAUDE_MOVE_CONTINUE", "cut"),
                        help="with --move-open: cut (default) reopens the moved tabs and sends 'continúa' "
                             "to those whose turn was cut; none reopens only; off only ends the processes")
    parser.add_argument("--off", action="store_true", help="remove the managed settings")
    parser.add_argument("--summary", action="store_true",
                        help="with --move-open: end with one MOVE-SUMMARY <json> line (per tab: name, session, "
                             "outcome, cut) for scripts")
    parser.add_argument("--verify", action="store_true",
                        help="with an account: only check it is logged in as its own email (exit 1 if not)")
    parser.add_argument("--no-verify", action="store_true",
                        help="with an account: skip the login check (the caller already ran --verify)")
    args = parser.parse_args()
    if args.off:
        return off()
    if args.move_open:
        code = move_open(args.scope, args.cont)
        if args.summary:
            # ASCII: a name holding U+2028 / U+0085 must not split the line (audit of
            # auto switch release 16).
            print("MOVE-SUMMARY " + json.dumps(SUMMARY, ensure_ascii=True))
        return code
    if args.verify:
        if not args.account:
            parser.error(f"--verify needs an account ({', '.join(ACCOUNTS)})")
        return verify(args.account)
    return switch(args.account, args.quiet, check=not args.no_verify) if args.account else status()


if __name__ == "__main__":
    sys.exit(main())
