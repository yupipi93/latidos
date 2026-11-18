"""The Claude Code accounts of this machine, read from the user config.

Every tool in this folder takes its accounts from one file, so nothing personal lives in
the code: `claude-accounts.json` in the Latidos config dir (`LATIDOS_CONFIG_DIR`, else
`$XDG_CONFIG_HOME/latidos`, else `~/.config/latidos`), or the file named by
`LATIDOS_CLAUDE_ACCOUNTS`. `claude-accounts.example.json` in `config/` shows every key.

    {
      "accounts": [
        {"tag": "work", "label": "Work", "email": "me@example.com", "config_dir": "",
         "keychain": "Claude Code-credentials", "chrome_profile": "Profile 1"},
        {"tag": "home", "label": "Home", "email": "me@example.org", "config_dir": "~/.claude.home"}
      ],
      "order": ["work", "home"],
      "notify": ["~/bin/notify.sh", "--quiet"]
    }

`config_dir` "" is the default account: Claude Code runs it with CLAUDE_CONFIG_DIR unset
(on macOS its Keychain login depends on that), its folder is ~/.claude. Exactly one
account should be the default; when none is, the first account is treated as the default
for the "unknown account" fallback only.

No file, a broken file or no accounts: the tools run with an empty list and do nothing
harmful (the auto switch says so and exits, the fallback runs the plain CLI).

`notify` is the argv prefix of a notifier, a path or a command in PATH; it is called as
`<notify...> <level> <title> <body>` with level info, ok, warn or crit.

Standard library only (Python 3.9+).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
_TAG_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def config_path() -> Path:
    if os.environ.get("LATIDOS_CLAUDE_ACCOUNTS"):
        return Path(os.path.expanduser(os.environ["LATIDOS_CLAUDE_ACCOUNTS"]))
    base = os.environ.get("LATIDOS_CONFIG_DIR") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or str(HOME / ".config"), "latidos")
    return Path(os.path.expanduser(base)) / "claude-accounts.json"


class Account:
    """One Claude login: `dir` is its config folder, `env` the CLAUDE_CONFIG_DIR value to run
    it with (None for the default account, which runs with the variable unset)."""

    def __init__(self, raw: dict):
        self.tag = str(raw["tag"])
        self.label = str(raw.get("label") or self.tag)
        self.email = str(raw.get("email") or "")
        cfg = str(raw.get("config_dir") or "")
        self.default = cfg == ""
        self.dir = HOME / ".claude" if self.default else Path(os.path.expanduser(cfg))
        self.env = None if self.default else str(self.dir)
        # The CLAUDE_CONFIG_DIR as written in shells and settings ("~/.claude.x").
        self.home_form = None if self.default else (cfg if cfg.startswith("~") else str(self.dir))
        self.keychain = str(raw.get("keychain") or "")
        self.chrome_profile = str(raw.get("chrome_profile") or "")
        self.raw = raw

    def __repr__(self) -> str:
        return f"Account({self.tag!r})"


class Config:
    def __init__(self, raw: dict, path: Path):
        self.path = path
        self.raw = raw
        accounts, seen = [], set()
        for a in raw.get("accounts") or []:
            tag = a.get("tag") if isinstance(a, dict) else None
            if not isinstance(tag, str) or not tag or set(tag) - _TAG_OK or tag in seen:
                _say(f"accounts: {path}: skipping a bad or repeated account {tag!r}")
                continue
            seen.add(tag)
            accounts.append(Account(a))
        self.accounts = accounts
        self.by_tag = {a.tag: a for a in accounts}
        self.tags = [a.tag for a in accounts]
        order = [t for t in (raw.get("order") or []) if t in self.by_tag]
        self.order = list(dict.fromkeys(order + self.tags))
        defaults = [a.tag for a in accounts if a.default]
        self.default_tag = defaults[0] if defaults else (self.tags[0] if self.tags else "")
        self.pool = Path(os.path.expanduser(str(raw.get("pool") or "~/.claude-shared")))
        self.active_file = Path(os.path.expanduser(str(raw.get("active_file") or "~/.claude-active")))
        notify = raw.get("notify") or []
        if isinstance(notify, str):
            notify = [notify]
        self.notify_argv = [os.path.expanduser(str(x)) for x in notify] if isinstance(notify, list) else []
        self.quota_api = str(raw.get("quota_api") or "")
        # Extra headers the quota source wants on a forced read (a CSRF marker, say).
        qh = raw.get("quota_headers")
        self.quota_headers = {str(k): str(v) for k, v in qh.items()} if isinstance(qh, dict) else {}
        self.quota_cache = Path(os.path.expanduser(str(raw.get("quota_cache") or "~/.local/state/claude-quota/claude-quota.json")))
        self.timezone = str(raw.get("timezone") or "")
        self.companion_id = str(raw.get("companion_id") or "latidos.claude-relaunch")
        self.settings = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}

    def tag_for_dir(self, value: str | None) -> str:
        """Account tag of a CLAUDE_CONFIG_DIR value: empty is the default account; an
        unknown dir is returned as is."""
        if not value:
            return self.default_tag
        p = os.path.realpath(os.path.expanduser(value.rstrip("/")))
        for a in self.accounts:
            if os.path.realpath(str(a.dir)) == p:
                return a.tag
        return value

    def notify(self, level: str, title: str, body: str, timeout: int = 60) -> bool:
        """Run the configured notifier as `<notify...> <level> <title> <body>` (levels: info,
        ok, warn, crit). No notifier: nothing is sent, False."""
        # A path or a command found in PATH (terminal-notifier, say).
        if not self.notify_argv or not (Path(self.notify_argv[0]).exists() or shutil.which(self.notify_argv[0])):
            return False
        try:
            subprocess.run(self.notify_argv + [level, title, body], capture_output=True, timeout=timeout)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


def zone(name: str = ""):
    """A tzinfo: the named IANA zone when it exists, else this machine's zone (from
    /etc/localtime, so it follows summer time), else a fixed offset as a last resort. A typo
    in the config never stops a tool (audit of 82)."""
    from zoneinfo import ZoneInfo
    for candidate in (name, _tz_env_name(), _localtime_name()):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except (ValueError, OSError, KeyError) as exc:  # ZoneInfoNotFoundError is a KeyError
                if candidate == name:
                    _say(f"accounts: unknown timezone {name!r} ({type(exc).__name__}), using the local one")
    import datetime as _dt
    return _dt.datetime.now().astimezone().tzinfo


def _say(msg: str) -> None:
    """A warning for a person only: under launchd stderr is the log and these tools run
    every minute (audits of 83 and 84)."""
    if sys.stderr.isatty():
        print(msg, file=sys.stderr)


def _tz_env_name() -> str:
    """$TZ as a zone name: "Europe/Madrid", "UTC", or a path into a zoneinfo folder. A name
    that does not exist falls through to /etc/localtime (audit of 84)."""
    tz = os.environ.get("TZ", "").lstrip(":")
    if "zoneinfo/" in tz:
        tz = tz.split("zoneinfo/", 1)[1]
    return "" if tz.startswith("/") else tz


def _localtime_name() -> str:
    """The zone /etc/localtime links to."""
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError) as exc:
        _say(f"accounts: ignoring {path}: {exc}")
        raw = {}
    return Config(raw, path)


CONFIG = load()
