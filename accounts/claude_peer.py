"""claude_peer.py - deliver one message to a running Claude Code session through its peer socket.

Used by cursor-claude-account.py (the "continúa" after an account switch) and
claude-quota-resume.py (the wake-up after a quota reset). Stdlib only.

Why a module: a bare {"type": "user", ...} line is not enough. Claude Code (checked in
2.1.318 and 2.1.322) gates every inbound peer message on the permission class of the
session: a session in bypassPermissions holds any message whose sender did not attest
its own class ("held inbound peer message ... cause=no-mode-asserted"), and every session
holds one whose attested class differs ("cause=mode-mismatch"). The hold waits for an
approval a Cursor tab never shows; the message is released only if the session's mode or
settings later come to match it, else it expires. That is what left every moved tab idle
on 2026-11-12.

The attestation is the cross-session envelope the CLI's own SendMessage builds:

    <cross-session-message from-mode="bypass">
    <text>
    </cross-session-message>

plus an origin {"kind": "peer", "fromMode": ...} on the line. The class is "bypass" for
bypassPermissions and "prompting" for every other mode, plan included (a plan tab with
--allow-dangerously-skip-permissions held "bypass" and took "prompting" on 2.1.318 and
2.1.322). The CLI compares it with the session's live
mode, which the tab can change without touching the command line. Nothing outside the
process shows the live mode (the session record has none; a runtime switch writes nothing
to the transcript until the next turn, checked on 2.1.318), so the best source is the mode
the transcript recorded on the last turn since this process started, then the process's
--permission-mode. Right for a freshly reopened tab (launched in its current mode) and for
a chat cut by the limit (its last turn recorded the mode); wrong only when the mode was
switched after the last turn, and then deliver() reports "not taken" and nobody re-sends.
The CLI honours the attestation while its remote attestation flag is on
(the default in 2.1.318 / 2.1.322); with it off a bypass session holds every socket
message, which deliver() then reports as not taken.

Exactly one message is sent per call, never a retry with the other class: a held message
is not dropped at once, it is released as soon as the session's mode or settings come to
match it (audit of release 3, checked on 2.1.318), so a second send could arrive twice.
A message that is not taken is reported, and the caller tells the owner to type it.

Same uid only: the CLI checks that the socket's peer is this user. The message shows in
the chat as coming from another session, never as the owner typing it, so it is not his
approval of anything.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
import uuid
from pathlib import Path

ENVELOPE_TAG = "cross-session-message"
BYPASS_MODES = {"bypassPermissions"}
# A message sent before the session knows its own mode is held ("permission-mode getter not
# wired", cause=mode-unknown) and released by itself later, after any verify window. So
# nothing is sent until the session record shows a status (startup done).
READY_SECONDS = 45.0
VERIFY_SECONDS = 8.0
# Prefix of a "not taken" detail that means nothing was sent at all (safe to retry).
NOT_SENT = "not sent"
# Machine-wide admin settings (a device-management profile, say); see bypass_disabled().
MANAGED_SETTINGS = Path("/Library/Application Support/ClaudeCode/managed-settings.json")


def process_args(pid: int) -> list[str]:
    out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return out.split()


def permission_mode(args: list[str]) -> str:
    """The --permission-mode a Claude process was started with ("default" when absent)."""
    for i, a in enumerate(args):
        if a.startswith("--permission-mode="):
            return a.split("=", 1)[1]
        if a == "--permission-mode" and i + 1 < len(args):
            return args[i + 1]
    if "--dangerously-skip-permissions" in args:
        return "bypassPermissions"
    return "default"


def _ms(ts: str) -> int:
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def transcript_mode(path: Path | None, since_ms: int = 0) -> str | None:
    """The permission mode the transcript last recorded after `since_ms` (the process start).
    Every user entry carries "permissionMode"; an untimestamped {"type": "permission-mode"}
    entry counts when it follows a timestamped entry from after the start. A mode recorded
    before this process started is ignored: a relaunched tab can start in another mode
    without writing it. None when there is none; the caller falls back to --permission-mode."""
    if not path:
        return None
    mode, after_start = None, since_ms <= 0
    # The whole file, line by line: a long turn can push the last recorded mode far back
    # (52 of 177 recent transcripts had over 512 KB after it). Top-level fields only.
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                ts = entry.get("timestamp")
                if isinstance(ts, str) and ts:
                    after_start = _ms(ts) >= since_ms
                value = entry.get("permissionMode")
                if after_start and isinstance(value, str) and value:
                    mode = value
    except OSError:
        return None
    return mode


def _json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _disables_bypass(settings: dict, admin: bool = True) -> bool:
    """Whether a settings document disables bypass. The CLI reads
    permissions.disableBypassPermissionsMode only (audit of release 31). In admin tiers
    (server-managed, managed file) it fails closed: an invalid value (`true`, "Disable",
    1) or a `permissions` that is not an object counts as "disable"; false / "false" /
    null are an absent key (audit of release 33). User settings: only "disable"."""
    if "permissions" not in settings or settings["permissions"] is None:
        return False
    perms = settings["permissions"]
    if not isinstance(perms, dict):
        return admin
    value = perms.get("disableBypassPermissionsMode")
    if _absent(value):
        return False
    return value == "disable" or admin


def _absent(value) -> bool:
    """How the CLI reads a restrictive key: null, false or "false" mean no key. Identity,
    not ==: 0 is invalid there and fails closed (audit of release 34)."""
    return value is None or value is False or value == "false"


# Restrictive keys whose only valid value is "disable" (2.1.322): under permissions, and
# at the top level (audit of release 35).
_RESTRICTIVE = ("disableBypassPermissionsMode", "disableAutoMode")
_TOP_RESTRICTIVE = ("disableAutoMode", "disableDeepLinkRegistration")


def _split_restrictive(d: dict, keys: tuple[str, ...]) -> tuple[dict, int]:
    """(d without null values and absent restrictive keys, count of invalid ones)."""
    kept = {k: v for k, v in d.items() if v is not None and not (k in keys and _absent(v))}
    return kept, sum(1 for k in keys if k in kept and kept[k] != "disable")


def _rank_view(settings: dict) -> tuple[dict, bool]:
    """(the tier as the CLI ranks it, whether its content is only fail-closed substitutes).
    Absent restrictive values are dropped; invalid ones are substitutes, which rank below a
    file with content and bind beside it (audits of releases 34, 35). Not modelled: other
    keys of the wrong type, which the CLI also substitutes (a typed server payload does not
    produce them)."""
    view, substitutes = _split_restrictive(settings, _TOP_RESTRICTIVE)
    perms = view.get("permissions")
    if isinstance(perms, dict):
        perms, more = _split_restrictive(perms, _RESTRICTIVE)
        substitutes += more
        view["permissions"] = perms
    real = {k: v for k, v in view.items() if not (k in _TOP_RESTRICTIVE and v != "disable")}
    if isinstance(perms, dict):
        real["permissions"] = {k: v for k, v in perms.items() if not (k in _RESTRICTIVE and v != "disable")}
    return view, bool(substitutes) and not _has_content(real)


# Keys that do not make an admin tier count as present (2.1.322).
_NOT_CONTENT = {"managedSourcesBehavior", "wslInheritsWindowsSettings"}


def _empty(value) -> bool:
    """null, an empty object, or one holding only those (the CLI drops a null key)."""
    return value is None or isinstance(value, dict) and all(_empty(v) for v in value.values())


def _has_content(settings: dict) -> bool:
    """A tier the CLI ranks above an empty one: a key besides the excluded ones whose value
    is not null or (nested) empty objects (audits of releases 32, 33)."""
    return any(k not in _NOT_CONTENT and not _empty(v) for k, v in settings.items())


def _managed_documents() -> list[dict]:
    """managed-settings.json, then the managed-settings.d/*.json drop-ins in name order."""
    docs = [_json_file(MANAGED_SETTINGS)]
    dropins = MANAGED_SETTINGS.parent / "managed-settings.d"
    # Like the CLI: regular files (or links) ending in .json, not dotfiles (._x.json).
    files = sorted(p for p in dropins.glob("*.json") if p.is_file() and not p.name.startswith(".")) \
        if dropins.is_dir() else []
    return docs + [_json_file(p) for p in files]


def _managed_file_disables() -> bool:
    """A disable in the file or any drop-in sticks: a later false / null is a removal in
    the CLI's merge, not an override (audit of release 33)."""
    return any(_disables_bypass(d) for d in _managed_documents())


def bypass_disabled(config_dir: Path | None) -> bool:
    """True when the account of `config_dir` cannot run in bypassPermissions.

    The CLI takes its admin settings from the first tier with content: a key besides
    managedSourcesBehavior / wslInheritsWindowsSettings whose value is not null or only
    empty objects (checked in 2.1.322: server-managed,
    then the managed plist / HKLM, then managed-settings.json with its .d drop-ins), unless
    the tier says managedSourcesBehavior "merge". An account whose server-managed settings
    (`remote-settings.json`) have content ignores the machine's managed file; one with `{}`
    gets it. On a managed Mac that file (from device management) can set disableBypassPermissionsMode "disable", so
    a tab of such an account launched with --permission-mode bypassPermissions really runs
    in "auto" and holds a "bypass" attestation (mode-mismatch): every continue after the
    switch to lt on 2026-11-12. The user's own settings.json can disable it too.

    Not modelled: the managed plist (none on this Mac), project / local / --settings
    sources, and known keys of the wrong type that the CLI drops before ranking.
    The CLI fixes its admin settings at start; this reads today's cached server settings,
    so a change of them while a tab runs can disagree until the tab restarts."""
    if config_dir is None:
        return False
    if _disables_bypass(_json_file(config_dir / "settings.json"), admin=False):
        return True
    remote, only_substitutes = _rank_view(_json_file(config_dir / "remote-settings.json"))
    if _has_content(remote) and not only_substitutes:
        if remote.get("managedSourcesBehavior") != "merge":
            return _disables_bypass(remote)
        if _disables_bypass(remote):
            return True
    # Remote empty (the file wins) or only substitutes (the file wins, they bind beside it).
    return _disables_bypass(remote) or _managed_file_disables()


def run_mode(config_dir: Path | None, wanted: str) -> str:
    """The --permission-mode an unattended run of this account can really have: `wanted`,
    except that bypass becomes "auto" on an account where bypass is disabled (lt on this
    Mac). Asking for bypass there starts the run in another mode than the one asked for;
    "auto" is what a Cursor tab of that account runs in, so a relaunch behaves the same."""
    if wanted in BYPASS_MODES and bypass_disabled(config_dir):
        return "auto"
    return wanted


WEAK_HEADLESS_MODES = {"default", "acceptEdits"}


def _unattended(value: str, skip: bool) -> str:
    """The mode to run in place of `value` on an account without bypass."""
    return "auto" if value in BYPASS_MODES or (skip and value in WEAK_HEADLESS_MODES) else value


def run_args(config_dir: Path | None, args: list[str]) -> list[str]:
    """`args` with a bypass request (--permission-mode bypassPermissions or
    --dangerously-skip-permissions) turned into what run_mode() allows for the account.
    The mode is replaced in place, so a variadic flag (--add-dir) or a `--` separator
    keeps reading what it read; with no explicit mode, `--permission-mode auto` goes first.
    A skip flag means the caller wanted bypass, so a mode named beside it that cannot
    approve tools headless (default, acceptEdits) becomes auto too; a deliberately
    restrictive one (plan, dontAsk) is kept, never widened."""
    head = args[:args.index("--")] if "--" in args else list(args)
    skip = "--dangerously-skip-permissions" in head  # asks for bypass whatever mode is named
    wants = permission_mode(head) in BYPASS_MODES or skip
    if not wants or not bypass_disabled(config_dir):
        return list(args)
    out, replaced, i = [], False, 0
    while i < len(args):
        a = args[i]
        if a == "--":
            out += args[i:]
            break
        if a == "--dangerously-skip-permissions":
            i += 1
            continue
        if a.startswith("--permission-mode="):
            value = a.split("=", 1)[1]
            out.append("--permission-mode=" + _unattended(value, skip))
            replaced, i = True, i + 1
            continue
        if a == "--permission-mode" and i + 1 < len(args):
            out += [a, _unattended(args[i + 1], skip)]
            replaced, i = True, i + 2
            continue
        out.append(a)
        i += 1
    return out if replaced else ["--permission-mode", "auto", *out]


def attested_class(args: list[str], mode: str | None = None, no_bypass: bool = False) -> str:
    """The permission class to attest: from the session's current mode when known (the
    transcript), else from the --permission-mode the process was started with, which the
    CLI downgrades when bypass is disabled for the account (`no_bypass`). The CLI checks
    the live mode, and the tab can change it without touching the command line."""
    if mode:
        return "bypass" if mode in BYPASS_MODES else "prompting"
    return "bypass" if permission_mode(args) in BYPASS_MODES and not no_bypass else "prompting"


def envelope(text: str, cls: str) -> str:
    # The CLI parses the body up to the closing tag; keep a stray one out of it.
    body = text.replace(f"</{ENVELOPE_TAG}>", f"</ {ENVELOPE_TAG}>")
    return f'<{ENVELOPE_TAG} from-mode="{cls}">\n{body}\n</{ENVELOPE_TAG}>'


def peer_line(text: str, cls: str, session: str = "") -> str:
    """One JSON line in the shape of the CLI's own SendMessage, attesting class `cls`."""
    msg = {
        "type": "user",
        "message": {"role": "user", "content": envelope(text, cls)},
        "parent_tool_use_id": None,
        "session_id": session,
        "uuid": str(uuid.uuid4()),
        "origin": {"kind": "peer", "from": "unknown", "fromMode": cls, "body": text},
    }
    return json.dumps(msg, ensure_ascii=False) + "\n"


def send_line(sock_path: str, line: str) -> str | None:
    """Write one line to a session socket. None on success, else the error."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5)
            sock.connect(sock_path)
            sock.sendall(line.encode())
    except OSError as exc:
        return str(exc)
    return None


def read_record(record: Path) -> dict:
    try:
        info = json.loads(record.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return info if isinstance(info, dict) else {}


def session_status(record: Path) -> tuple[str, int]:
    """(status, statusUpdatedAt ms) from <config dir>/sessions/<pid>.json."""
    info = read_record(record)
    return str(info.get("status", "?")), int(info.get("statusUpdatedAt") or 0)


def transcript_has(path: Path | None, needle: str, start: int) -> bool:
    """True when a queue-operation or user line written past byte `start` holds `needle`.
    Other lines do not count: api-request-blob entries carry the whole history, so an old
    copy of the same text would look like a new delivery (audit of release 7)."""
    if not path:
        return False
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    for line in data.splitlines():
        if needle not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("type") in ("queue-operation", "user"):
            return True
    return False


def started(record: Path, transcript: Path | None, needle: str, since_ms: int, start: int,
            seconds: float, was_idle: bool) -> bool:
    """Did the session take the message? With a transcript, only its own queued text
    counts: the session can start a turn by itself (a task notification), which says
    nothing about our message. Without one, an idle session going busy after `since_ms`
    is the only sign. A held message writes nothing."""
    deadline = time.monotonic() + seconds
    while True:
        if transcript is not None:
            if transcript_has(transcript, needle, start):
                return True
        else:
            status, at = session_status(record)
            if was_idle and status == "busy" and at >= since_ms:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.3)


def wait_ready(record: Path, seconds: float) -> str:
    """"ready" once the record has a status (startup done, the session knows its mode),
    "gone" when the record disappears (the process ended), "timeout" otherwise."""
    deadline = time.monotonic() + seconds
    while True:
        if not record.exists():
            return "gone"
        if session_status(record)[0] != "?":
            return "ready"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(0.3)


def deliver(info: dict, text: str, sessions_dir: Path, transcript: Path | None = None,
            verify_seconds: float = VERIFY_SECONDS, ready_seconds: float = READY_SECONDS) -> tuple[bool, str]:
    """Send `text` once to the session described by its sessions/<pid>.json record `info`.
    Returns (taken, detail): detail is the attested class, or why it was not taken."""
    pid, sock = int(info.get("pid") or 0), info.get("messagingSocketPath")
    if not pid or not sock:
        return False, f"{NOT_SENT}: no pid or socket in the session record"
    record = sessions_dir / f"{pid}.json"
    ready = wait_ready(record, ready_seconds)
    if ready == "gone":
        return False, f"{NOT_SENT}: the session's process is gone"
    if ready != "ready":
        return False, f"{NOT_SENT}: the session did not finish starting in {ready_seconds:.0f} s"
    needle = json.dumps(text[:40], ensure_ascii=False)[1:-1]
    started_at = int(read_record(record).get("startedAt") or 0)
    # Unknown start: only the launch flag is safe (an old recorded mode may not apply).
    mode = transcript_mode(transcript, started_at) if started_at else None
    cls = attested_class(process_args(pid), mode, bypass_disabled(sessions_dir.parent))
    was_idle = session_status(record)[0] == "idle"
    since_ms = int(time.time() * 1000)
    start = transcript.stat().st_size if transcript and transcript.exists() else 0
    err = send_line(sock, peer_line(text, cls, str(info.get("sessionId", ""))))
    if err:
        return False, f"{NOT_SENT}: socket error ({err})"
    if started(record, transcript, needle, since_ms, start, verify_seconds, was_idle):
        return True, cls
    return False, f"held by the session (attested {cls})"
