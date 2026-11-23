#!/usr/bin/env python3
"""Latidos: Claude Code token and cost history, as a living local web panel.

One process does three things:

1. Ingests every Claude Code session log (JSONL) under the configured roots into its own
   SQLite database, incrementally (byte offsets per file), so history survives Claude
   Code's 30-day session cleanup.
2. Prices every model call at the public API rate (API-equivalent cost) and converts it
   to euros with the ECB daily rate (cached, with a fixed fallback).
3. Serves the page (web/) and a small JSON API on 127.0.0.1.

Standard library only; Python 3.9+.
"""

import bisect

import agents
import cursor_source
import datetime as dt
import errno
import fcntl
import glob
import ipaddress
import json
import plistlib
import subprocess
import math
import os
import re
import socket
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
# Identifies this server process: live sequence numbers restart with it.
BOOT = int(time.time() * 1000)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")


ENV_PREFIX = "LATIDOS_"
# Deprecated: the variables of the old name (Latido) are still read when the new one is
# not set.
LEGACY_ENV_PREFIX = "LATIDO_"


def env(name, default=None):
    """LATIDOS_<name>, else the deprecated LATIDO_<name>, else default."""
    for prefix in (ENV_PREFIX, LEGACY_ENV_PREFIX):
        raw = os.environ.get(prefix + name)
        if raw is not None:
            return raw
    return default


def _env_name(name):
    """The variable actually set, for messages: the new name unless only the old one is."""
    if os.environ.get(ENV_PREFIX + name) is None and os.environ.get(LEGACY_ENV_PREFIX + name) is not None:
        return LEGACY_ENV_PREFIX + name
    return ENV_PREFIX + name


def _env_float(name, default, lo=None, hi=None):
    raw = env(name)
    if raw is None or raw == "":
        return default
    try:
        val = float(raw)
    except ValueError:
        sys.stderr.write("latidos: %s=%r is not a number, using %s\n" % (_env_name(name), raw, default))
        return default
    if (lo is not None and val < lo) or (hi is not None and val > hi) or math.isnan(val):
        sys.stderr.write("latidos: %s=%r out of range, using %s\n" % (_env_name(name), raw, default))
        return default
    return val


# ---------------------------------------------------------------------------------------
# User configuration: ~/.config/latidos/ (config.json, boards.json, aliases.json). Every
# file is optional; the defaults suit a plain Claude Code install. Environment variables
# override single keys.
# ---------------------------------------------------------------------------------------

def _config_base():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")


def _state_base():
    return os.path.join(HOME, ".local", "state")


def config_dir():
    """LATIDOS_CONFIG_DIR, else <config home>/latidos. Until the one-time migration has
    run (migrate_legacy_dirs), the old <config home>/latido is read when only it exists."""
    d = env("CONFIG_DIR")
    if d:
        return os.path.expanduser(d)
    new = os.path.join(_config_base(), "latidos")
    old = os.path.join(_config_base(), "latido")
    return old if not os.path.lexists(new) and os.path.isdir(old) else new


def config_file(name, var=None):
    """A file of the user config: the env override when set, else <config_dir>/<name>."""
    raw = env(var) if var else None
    return os.path.expanduser(raw) if raw else os.path.join(config_dir(), name)


def default_db_path():
    """~/.local/state/latidos/latidos.db, or the old latido/latido.db while it has not
    been migrated yet (the migration is skipped while another process holds it)."""
    new = os.path.join(_state_base(), "latidos")
    old = os.path.join(_state_base(), "latido")
    if not os.path.lexists(new) and os.path.isdir(old):
        return os.path.join(old, "latido.db")
    return os.path.join(new, "latidos.db")


# The database and its companions, renamed together; the main file last, so a stop half
# way leaves nothing that SQLite would open without its write-ahead log.
_DB_SUFFIXES = ("-wal", "-shm", ".scan.lock", "")


def _legacy_db_busy(old_dir):
    """True when another process holds the old database's scan lock (a running Latido)."""
    lock_path = os.path.join(old_dir, "latido.db.scan.lock")
    if not os.path.exists(lock_path):
        return False
    try:
        with open(lock_path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


def migrate_legacy_dirs(config_base=None, state_base=None, log=None):
    """One-time move from the old name (Latido): <config home>/latido to .../latidos and
    ~/.local/state/latido/latido.db* to ~/.local/state/latidos/latidos.db*. Only when the
    new folder does not exist and no environment variable points elsewhere; a move, never
    a copy, so the history stays in one place. Idempotent: a run stopped half way is
    finished by the next one. Returns the list of moves made."""
    log = log or _warn
    config_base = config_base or _config_base()
    state_base = state_base or _state_base()
    moved = []

    def move(src, dst):
        os.rename(src, dst)
        moved.append((src, dst))
        log("migrated %s to %s" % (src, dst))

    try:
        if not env("CONFIG_DIR"):
            old, new = os.path.join(config_base, "latido"), os.path.join(config_base, "latidos")
            if os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new):
                move(old, new)
        if not env("DB"):
            old, new = os.path.join(state_base, "latido"), os.path.join(state_base, "latidos")
            if os.path.isdir(old) and not os.path.islink(old) and not os.path.lexists(new):
                if _legacy_db_busy(old):
                    log("%s is in use by another process (stop the old service first); "
                        "using it as it is" % old)
                    return moved
                move(old, new)
            if os.path.isdir(new):
                for suffix in _DB_SUFFIXES:
                    src = os.path.join(new, "latido.db" + suffix)
                    dst = os.path.join(new, "latidos.db" + suffix)
                    if os.path.lexists(src) and not os.path.lexists(dst):
                        move(src, dst)
    except OSError as exc:
        log("migration from the old name stopped: %s" % exc)
    return moved


DEFAULT_SETTINGS = {
    # Claude Code transcript folders (globs allowed).
    "roots": ["~/.claude/projects"],
    # Folders right under the home folder that may be roots although their name does not
    # start with a dot, e.g. ["claude-logs"] for ~/claude-logs (see safe_roots).
    "allow_home_folders": [],
    # Where repositories live: <code_dir>/<repo>, <projects_dir>/<repo> or
    # <projects_dir>/<group>/<repo> (a group is a folder of repositories).
    "code_dir": "~/code",
    "projects_dir": None,  # default <code_dir>/projects
    # Agent workspaces under code_dir (an agent-skeleton checkout, for example): a session
    # started there is attributed to the repository its tools touched most, and
    # <workspace>/projects/<repo> counts as <repo>.
    "workspaces": ["agent-skeleton"],
    # Optional source of the account cards: a local server answering /api/claude-quota
    # and /api/cursor-usage. Empty: no account cards.
    "accounts_url": "",
    # Card colour per account tag, e.g. {"work": "#d81b4a"}; others get a palette colour.
    "account_colors": {},
    # Where each account tag's Claude Code login lives: its config folder or its
    # .claude.json, e.g. {"work": "~/.claude-work"}. Read for the account id only, so every
    # call is credited to the account that really made it (a chat keeps the account it
    # started on after the default changes). Empty: calls follow the active card.
    "account_logins": {},
    # Optional command that controls the default Claude account from the cards (right-click
    # menu): the argv prefix of a program answering `state`, `activate <tag>` and
    # `order <tag,tag,...>` with JSON on stdout, e.g. ["python3", "~/bin/claude-control.py"].
    # Empty: the cards only show, nothing is changed.
    "account_control": [],
    # Optional command that reports and renews the Claude logins (L-161): the argv prefix of
    # a program answering `status --json` and `login <tag>` (accounts/claude_accounts.py),
    # e.g. ["python3", "~/latidos/accounts/claude_accounts.py"]. Empty: no session lines.
    "account_logins_tool": [],
    # launchd label used by `latidos.py install`.
    "label": "local.latidos",
}


# Expected type of every config.json key: list keys also accept a single string.
_SETTING_TYPES = {
    "roots": list, "allow_home_folders": list, "code_dir": str, "projects_dir": str, "workspaces": list,
    "accounts_url": str, "account_colors": dict, "account_logins": dict, "account_control": list,
    "account_logins_tool": list, "label": str,
}


def _warn(msg):
    sys.stderr.write("latidos: %s\n" % msg)


def _why(exc):
    return "nested too deeply" if isinstance(exc, RecursionError) else exc


def _valid_settings(data, path):
    """The keys of a config.json object whose type is right. A single string stands for a
    one-item list; anything else of the wrong type is ignored with a warning and keeps its
    default (a string iterated per character would turn "~/x" into the roots "~", "/", ...)."""
    out = {}
    for key, val in data.items():
        if key not in DEFAULT_SETTINGS or val is None:
            continue
        want = _SETTING_TYPES[key]
        if want is list and isinstance(val, str):
            val = [val]
        if want is str and isinstance(val, str) and not val.strip():
            # "" would be the current folder ("/" under launchd) or a ".plist" label.
            if val != DEFAULT_SETTINGS[key]:
                _warn("%s: %r is empty, using the default" % (path, key))
            continue
        if not isinstance(val, want):
            _warn("%s: %r must be %s, using the default" % (
                path, key, {list: "a list of strings", str: "a string", dict: "an object"}[want]))
            continue
        if want is list:
            bad = [v for v in val if not isinstance(v, str)]
            if bad:
                _warn("%s: %r ignores items that are not strings: %r" % (path, key, bad[:3]))
            val = [v for v in val if isinstance(v, str)]
        if want is dict:
            bad = [k for k, v in val.items() if not isinstance(v, str)]
            if bad:
                _warn("%s: %r ignores entries whose value is not a string: %r" % (path, key, bad[:3]))
            val = {str(k): v for k, v in val.items() if isinstance(v, str)}
        out[key] = val
    return out


def load_settings():
    path = config_file("config.json", "CONFIG")
    data = {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError, RecursionError) as exc:
        _warn("ignoring %s: %s" % (path, _why(exc)))
        data = {}
    out = dict(DEFAULT_SETTINGS)
    valid = _valid_settings(data, path)
    out.update(valid)
    out["code_dir"] = os.path.abspath(os.path.expanduser(out["code_dir"])).rstrip("/") or "/"
    pdir = out.get("projects_dir") or os.path.join(out["code_dir"], "projects")
    out["projects_dir"] = os.path.abspath(os.path.expanduser(pdir)).rstrip("/") or "/"
    out["workspaces"] = [w.strip("/") for w in out["workspaces"] if w.strip("/")]
    out["roots"] = [r for r in out["roots"] if r.strip()]
    if not out["roots"]:
        # [] or [""] would ingest nothing without a word.
        if "roots" in valid:
            _warn("%s: 'roots' has no folder, using the default" % path)
        out["roots"] = list(DEFAULT_SETTINGS["roots"])
    out["allow_home_folders"] = [n.strip("/") for n in out["allow_home_folders"] if n.strip("/")]
    if env("HOME_URL") is not None:
        out["accounts_url"] = env("HOME_URL")
    if env("LABEL"):
        out["label"] = env("LABEL")
    return out


SETTINGS = load_settings()
CODE_DIR = SETTINGS["code_dir"]
PROJECTS_DIR = SETTINGS["projects_dir"]
WORKSPACE_DIRS = [os.path.join(CODE_DIR, w) for w in SETTINGS["workspaces"]]


def expand_roots(patterns):
    out = []
    for pat in patterns:
        pat = os.path.expanduser(pat)
        if any(c in pat for c in "*?["):
            out.extend(sorted(glob.glob(pat)))
        else:
            out.append(pat)
    return out


def _ident(path):
    """(st_dev, st_ino) of a path, links followed; None when it cannot be read."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None
    return (st.st_dev, st.st_ino)


def _forbidden_roots(home):
    """Identities of the folders never walked (the filesystem root, the home folder and
    every folder above it, along the home path as written and its real path) and the home
    folder's paths relative to each of them ("Users/name", "name")."""
    out, tails = set(), set()
    for start in (os.path.abspath(home), os.path.realpath(home)):
        cur = start
        while True:
            ident = _ident(cur)
            if ident:
                out.add(ident)
            if cur != start:
                tails.add(os.path.relpath(start, cur))
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return out, tails


# Trees never walked, compared on the resolved path in lower case (APFS ignores case).
# On macOS /var, /etc and /tmp are links into /private, so the resolved path decides and
# /var/x and /private/var/x get the same answer.
_SYSTEM_TREES = ("/applications", "/library", "/system", "/usr", "/opt", "/bin", "/sbin",
                 "/cores", "/dev", "/etc", "/private/etc", "/var", "/private/var", "/proc",
                 "/sys", "/boot", "/lib", "/lib64", "/snap", "/nix")
# Inside a system tree, the per-user temp folders (macOS /var/folders/<a>/<b>/...) stay
# allowed from three levels down: tests and tools put their roots there.
_TEMP_TREES = ("/var/folders", "/private/var/folders")
# Folders whose direct children are never roots: volume tops, other users' homes, the
# /private links' targets.
_CONTAINERS = ("/volumes", "/users", "/home", "/private", "/mnt", "/media", "/run/media")
# Under the home folder: their direct children hold apps' data and cloud drives.
_HOME_CONTAINERS = ("Library", "Library/CloudStorage", "Library/Mobile Documents",
                    "Library/Containers", "Library/Group Containers")


def _within(path, base):
    """True when path is base or inside it (both plain path strings)."""
    return path == base or path.startswith(base.rstrip("/") + "/")


def _warn_once(seen, msg):
    """Warn; with a `seen` set, each message only once (the scan repeats every 10-30 s)."""
    if seen is not None:
        if msg in seen:
            return
        seen.add(msg)
    _warn(msg)


def _system_refusal(real):
    """Why a resolved path is refused by name, or None."""
    low = real.lower()
    for tree in _TEMP_TREES:
        if _within(low, tree):
            rest = os.path.relpath(low, tree)
            return None if rest != "." and rest.count("/") >= 2 else \
                "is a shared temporary folder (only its per-user folders may be roots)"
    if _within(low, "/system"):
        return "is inside /System (macOS reaches every volume from there)"
    for tree in _SYSTEM_TREES:
        if _within(low, tree):
            return "is inside the system folder %s" % tree
    parent = os.path.dirname(low)
    if parent in _CONTAINERS:
        return "is a volume top or a folder of %s" % parent
    if os.path.dirname(parent) in ("/media", "/run/media") and os.path.ismount(real):
        return "is the top of a volume"
    return None


def safe_roots(paths, home=None, allow=None, seen=None):
    """Drop roots that would walk far more than transcripts. Called at startup and again
    on every scan (a missing root may be created later, a link retargeted). Folders are
    compared by identity (device and inode) where it matters, so a case variant on a
    case-insensitive volume ("/users/NAME") or a link to them is caught too. Refused:
    - the filesystem root, the home folder, any folder above it or holding it under another
      path (/System/Volumes/Data on macOS), any folder one level under the filesystem root;
    - a folder right under the home folder unless its name starts with a dot (transcripts
      live in ~/.claude*, ~/.cursor*) or is listed in `allow` (setting allow_home_folders),
      and the direct children of ~/Library and its cloud and container folders;
    - system trees (/System, /Applications, /Library, /usr, /opt, /etc, /var and their
      /private targets, except per-user temp folders under /var/folders) and the direct
      children of /Volumes, /Users, /home, /private, /mnt, /media (volume tops).
    A folder that merely is a mount point elsewhere (a container volume) is allowed.
    Callers pass the expanded paths of a glob, never the pattern."""
    home = home or HOME
    allow = SETTINGS["allow_home_folders"] if allow is None else allow
    forbidden, tails = _forbidden_roots(home)
    home_id = _ident(home)
    home_real = os.path.realpath(home)
    home_boxes = {i for i in (_ident(os.path.join(home, c)) for c in _HOME_CONTAINERS) if i}
    allowed = {i for i in (_ident(os.path.join(home, n)) for n in allow) if i}
    top = _ident("/")
    out = []
    for path in paths:
        given = os.path.abspath(os.path.expanduser(path))
        real = os.path.realpath(given)
        ident = _ident(real)
        parent = _ident(os.path.dirname(real)) if ident else None
        why = None
        if real == "/" or given == "/" or (ident and ident in forbidden):
            why = "is the filesystem root, the home folder or a folder above it"
        elif ident and home_id and any(_ident(os.path.join(real, t)) == home_id for t in tails):
            # another path to a folder above home (a firmlinked or mounted view of it)
            why = "contains the home folder"
        elif os.path.dirname(given) == "/" or os.path.dirname(real) == "/" or (
                ident and top and parent == top):
            why = "is a folder at the top of the filesystem"
        elif (home_id and (parent == home_id or os.path.dirname(real) == os.path.realpath(home))
              and not os.path.basename(real).startswith(".") and ident not in allowed):
            why = "is a folder of the home folder (only dot-folders or allow_home_folders)"
        elif parent and parent in home_boxes:
            why = "holds an app's data or a cloud drive"
        elif _within(real.lower(), home_real.lower()):
            # Inside the home folder the system-tree rules do not apply: some systems keep
            # homes under /var (Fedora Atomic: /home -> /var/home), /usr (FreeBSD) or /opt.
            why = None
        else:
            why = _system_refusal(real)
        if why:
            _warn_once(seen, "root %r %s, skipped" % (path, why))
            continue
        out.append(path)
    return out


def _inside(path, base):
    return base == "/" or _within(path, base)


def walk_root(root, forbidden=frozenset(), seen=None):
    """os.walk(root) that follows a link (folder or file) only while its real path stays
    inside the root's real path, never enters a folder twice (link loops) and prunes any
    folder whose identity is in `forbidden` (the filesystem root, the home folder and the
    folders above it). Yields (dirpath, file names)."""
    base = os.path.realpath(root)
    visited = set()
    for dirpath, dirs, files in os.walk(root, followlinks=True):
        ident = _ident(dirpath)
        if ident is None or ident in visited or ident in forbidden:
            dirs[:] = []
            continue
        visited.add(ident)
        kept = []
        for name in dirs:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) and not _inside(os.path.realpath(p), base):
                _warn_once(seen, "link %r leads outside the root %r, not followed" % (p, root))
                continue
            kept.append(name)
        dirs[:] = kept
        names = []
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.islink(p) and not _inside(os.path.realpath(p), base):
                _warn_once(seen, "link %r leads outside the root %r, not read" % (p, root))
                continue
            names.append(name)
        yield dirpath, names


_ROOT_WARNED = set()  # root and link warnings already logged by this process


def checked_roots(patterns, seen=None):
    """Expand the root patterns and keep the safe folders. A glob with a wildcard in the
    first two levels of its path ("/*/*", "/data/*", "/tmp/*") is refused whole: it reaches
    folders no name rule can tell from a transcript folder; list such roots by name."""
    keep = []
    for pat in patterns:
        parts = os.path.abspath(os.path.expanduser(pat)).split("/")[1:3]
        if any(c in part for part in parts for c in "*?["):
            _warn_once(seen, "root %r has a wildcard in the first two levels of its path, skipped" % pat)
            continue
        keep.append(pat)
    return safe_roots(expand_roots(keep), seen=seen)


def root_patterns():
    """The configured root patterns: LATIDOS_ROOTS (colon-separated) or config.json."""
    env_roots = env("ROOTS")
    if env_roots:
        pats = [r for r in env_roots.split(":") if r.strip()]
        if pats:
            return pats
        _warn_once(_ROOT_WARNED, "LATIDOS_ROOTS has no folder, using roots from config.json")
    return list(SETTINGS["roots"])


def _is_loopback(host):
    host = (host or "").strip("[]").lower()
    if host == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)  # ::ffff:127.0.0.1 from a dual-stack socket
    return (mapped or addr).is_loopback


def _host_name(host):
    """The host as it appears in a URL and a Host header: IPv6 in brackets."""
    return "[%s]" % host if ":" in host else host


def bind_host():
    """LATIDOS_HOST, loopback unless LATIDOS_ALLOW_REMOTE=1: the page shows session titles,
    prompts' projects and costs, so it is never exposed by a stray variable."""
    # Host names ignore case: "LOCALHOST" is localhost (and matches the Host check).
    host = (env("HOST") or "").strip().strip("[]").lower() or "127.0.0.1"
    if not _is_loopback(host) and env("ALLOW_REMOTE") != "1":
        _warn("LATIDOS_HOST=%s is not a loopback address and LATIDOS_ALLOW_REMOTE is not 1, "
              "binding 127.0.0.1" % host)
        return "127.0.0.1"
    return host


def config():
    patterns = root_patterns()
    return {
        # Checked here for the startup log and again on every scan (Ingestor.list_files).
        "roots": checked_roots(patterns, _ROOT_WARNED),
        "root_patterns": patterns,
        "db": os.path.expanduser(env("DB") or default_db_path()),
        "host": bind_host(),
        "port": int(_env_float("PORT", 7482, 1024, 65535)),
        # Idle interval; while a page is open the loop scans every min(10 s, this).
        "scan_seconds": _env_float("SCAN_SECONDS", 30, 2, 3600),
        "eur_fallback": _env_float("EUR_FALLBACK", 0.86, 0.1, 10),
        # Optional local source of the account cards (read on this machine only).
        "home_url": SETTINGS["accounts_url"],
        "account_colors": SETTINGS["account_colors"],
        "account_logins": SETTINGS["account_logins"],
        "account_control": SETTINGS["account_control"],
        "account_logins_tool": SETTINGS["account_logins_tool"],
        "fx_url": env(
            "FX_URL", "https://api.frankfurter.app/latest?from=USD&to=EUR"),
        # Plans, in multiples of a Claude Pro allowance (Anthropic help center: a Team
        # Standard seat is 1.25x Pro per session, a Team Premium seat 6.25x).
        "plan_main": _env_float("PLAN_MAIN", 1.25, 0.01, 100),
        "plan_others": [
            _env_float("PLAN_OTHER1", 6.25, 0, 100),
            _env_float("PLAN_OTHER2", 6.25, 0, 100),
        ],
        # Monthly price in USD, monthly billing: Team Standard 25, Team Premium 125.
        "price_main": _env_float("PRICE_MAIN", 25, 0, 100000),
        "price_others": _env_float("PRICE_OTHERS", 250, 0, 100000),
        "boards": _load_boards(),
        # Client grouping: a client's project gets its own bubble from this share of the
        # range total up (classified sessions, classify.py).
        "fold_share": _env_float("FOLD_SHARE", 0.03, 0, 1),
    }


def _load_map(path):
    """A JSON object of strings from the user config; a missing file is empty, anything
    else that is not an object of strings is ignored with a warning (never a crash)."""
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:
        _warn("ignoring %s: %s" % (path, _why(exc)))
        return {}
    if not isinstance(data, dict):
        _warn("ignoring %s: not an object" % path)
        return {}
    bad = [k for k, v in data.items() if not isinstance(v, str)]
    if bad:
        _warn("%s: ignoring entries whose value is not a string: %r" % (path, bad[:3]))
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _load_aliases():
    """Display names for projects, from aliases.json in the user config (optional):
    {"folder-or-repo-name": "name to show"}. Applied when names are shown, so stored data
    keeps the original names and a rename needs no re-ingest."""
    return _load_map(config_file("aliases.json", "ALIASES"))


ALIASES = _load_aliases()
_aliases_seen = {"mtime": None, "checked": 0.0}
ALIAS_LISTENERS = []  # called after aliases.json changed (the app drops its caches)


def alias(name):
    """Display name. aliases.json is re-read when it changes (checked every few seconds),
    so an edit shows without a restart. The map is swapped whole, never emptied under a
    reader in another thread."""
    global ALIASES
    now = time.time()
    if now - _aliases_seen["checked"] > 5 and not env("ALIASES_FROZEN"):
        _aliases_seen["checked"] = now
        path = config_file("aliases.json", "ALIASES")
        try:
            mtime = (path, os.path.getmtime(path))
        except OSError:
            mtime = (path, None)  # a missing file (or another path) also reloads: no stale names
        if mtime != _aliases_seen["mtime"]:
            _aliases_seen["mtime"] = mtime
            ALIASES = _load_aliases()
            _notify_names()  # also a hand edit: drop caches, no summary across it is kept
    return ALIASES.get(name, name)


# ---------------------------------------------------------------------------------------
# Edits from the page: rename a cell (aliases.json) and move a task to another cell
# (assign.json: {"s:<session id>" or "<TICKET-1>": "project name"}). Both files live in the
# user config, are written atomically, and are re-read when they change on disk.
# ---------------------------------------------------------------------------------------
ASSIGN = {}  # replaced whole on every change, never mutated: readers in other threads
_assign_seen = {"mtime": None, "checked": 0.0}
_EDIT_LOCK = threading.Lock()
NAME_MAX = 60
POST_BODY_TIMEOUT_S = 10  # a body shorter than its Content-Length gives up after this
NAMES_GEN = [0]  # bumped on every rename or move: a summary computed across one is not cached
RESERVED_NAMES = ("desconocido",)  # the unknown-folder cell
TASK_KEY_RE = re.compile(r"^(s:[A-Za-z0-9._-]{1,80}|[A-Z][A-Z0-9]{0,14}-[0-9]{1,9})$")


class EditConflict(Exception):
    """A config file the page would overwrite cannot be read as a JSON object: it is left
    alone. `file` names it (the page words the message)."""
    def __init__(self, file, message):
        Exception.__init__(self, message)
        self.file = file


def _notify_names():
    NAMES_GEN[0] += 1
    for fn in list(ALIAS_LISTENERS):
        try:
            fn()
        except Exception:  # a listener whose app is gone: forget it
            ALIAS_LISTENERS.remove(fn)


def _assign_map():
    """assign.json, re-read when it changes (checked every few seconds)."""
    global ASSIGN
    now = time.time()
    if now - _assign_seen["checked"] > 5 and not env("ALIASES_FROZEN"):
        _assign_seen["checked"] = now
        path = config_file("assign.json", "ASSIGN")
        try:
            mtime = (path, os.path.getmtime(path))
        except OSError:
            mtime = (path, None)
        if mtime != _assign_seen["mtime"]:
            first = _assign_seen["mtime"] is None
            _assign_seen["mtime"] = mtime
            new = _load_map(path)
            if new != ASSIGN:
                ASSIGN = new
                if not first:
                    _notify_names()
    return ASSIGN


def assigned_key(session_id, ticket, part_key=None):
    """The assign.json key that moved this task by hand, or None: the segment's own entry
    (`s:<session>.<n>`, a classified session's part), then the whole session's, then its
    ticket's."""
    m = _assign_map()
    if not m:
        return None
    if part_key and part_key in m:
        return part_key
    k = "s:%s" % session_id
    if k in m:
        return k
    return ticket if ticket and ticket in m else None


def assigned_project(session_id, ticket, part_key=None):
    """The (stored) project a task was moved to by hand, or None."""
    k = assigned_key(session_id, ticket, part_key)
    return _assign_map().get(k) if k else None


# ---------------------------------------------------------------------------------------
# Smart classification (classify.py writes classified.json in the user config): each
# session split into segments with a kind, client, project and task. It beats the folder
# heuristics; a task moved by hand still wins. Client work is grouped per client: a client's
# small projects share one bubble named after the client, a project that weighs at least
# FOLD_SHARE of the range gets its own bubble "<client> · <project>".
# ---------------------------------------------------------------------------------------
CLASSIFIED = {"sessions": {}, "folders": {}}
_classified_seen = {"mtime": None, "checked": 0.0}
CLASS_KINDS = ("client", "internal", "tool", "personal")
GROUP_SEP = " · "


def _clean_seg(seg):
    """A segment of classified.json as Latidos uses it, or None when it is malformed."""
    if not isinstance(seg, dict):
        return None
    frm, kind, project = seg.get("from"), seg.get("kind"), seg.get("project")
    if isinstance(frm, bool) or not isinstance(frm, (int, float)) or kind not in CLASS_KINDS:
        return None
    if not math.isfinite(frm) or abs(frm) > 1e15:  # json.load accepts NaN and Infinity
        return None
    if not isinstance(project, str) or not project.strip():
        return None
    client = seg.get("client") if isinstance(seg.get("client"), str) and seg["client"].strip() else None
    task = seg.get("task") if isinstance(seg.get("task"), str) else None
    ticket = seg.get("ticket") if isinstance(seg.get("ticket"), str) else None
    return {"from": int(frm), "kind": kind, "client": client if kind in ("client", "internal") else None,
            "project": " ".join(project.split())[:80], "task": task, "ticket": ticket}


def _load_classified(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"sessions": {}, "folders": {}}
    except (OSError, ValueError, RecursionError) as exc:
        _warn("ignoring %s: %s" % (path, _why(exc)))
        return {"sessions": {}, "folders": {}}
    if not isinstance(data, dict):
        _warn("ignoring %s: not an object" % path)
        return {"sessions": {}, "folders": {}}
    sessions = {}
    raw = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    for sid, rec in raw.items():
        segs = rec.get("segments") if isinstance(rec, dict) else None
        if not isinstance(segs, list):
            continue
        segs = sorted((s for s in map(_clean_seg, segs) if s), key=lambda s: s["from"])
        if segs:
            sessions[str(sid)] = {"from": [s["from"] for s in segs], "segs": segs}
    folders = {}
    rawf = data.get("folders") if isinstance(data.get("folders"), dict) else {}
    for repo, f in rawf.items():
        f = _clean_seg(dict(f, **{"from": 0})) if isinstance(f, dict) else None
        if f:
            folders[str(repo)] = f
    return {"sessions": sessions, "folders": folders}


def _classified():
    """classified.json, re-read when it changes (checked every few seconds)."""
    global CLASSIFIED
    now = time.time()
    if now - _classified_seen["checked"] > 5 and not env("ALIASES_FROZEN"):
        _classified_seen["checked"] = now
        path = config_file("classified.json", "CLASSIFIED")
        try:
            mtime = (path, os.path.getmtime(path))
        except OSError:
            mtime = (path, None)
        if mtime != _classified_seen["mtime"]:
            first = _classified_seen["mtime"] is None
            _classified_seen["mtime"] = mtime
            try:
                new = _load_classified(path)
            except Exception as exc:  # a hand-edited file of an unexpected shape
                _warn("ignoring %s: %s" % (path, exc))
                new = {"sessions": {}, "folders": {}}
            if new != CLASSIFIED:
                CLASSIFIED = new
                if not first:
                    _notify_names()
    return CLASSIFIED


def seg_index(entry, ts):
    """The index of the segment of a classified session holding ts (calls before the
    first prompt belong to the first segment)."""
    return max(0, bisect.bisect_right(entry["from"], ts) - 1)


def seg_names(seg):
    """(stored full name, client group or None) of a segment or a learned folder."""
    client = seg.get("client")
    if client:
        project = seg["project"]
        if project == client or not project:
            return client, client
        return client + GROUP_SEP + project, client
    return seg["project"], None


def part_key(session_id, entry, k):
    """The task key of segment k of a classified session, `s:<session>.<k>`, so a hand
    move covers that segment only, also when the classifier adds segments later. A move
    stored under the whole session's key (`s:<session>`, made before classification)
    still covers every segment."""
    if entry is None:
        return "s:%s" % session_id
    return "s:%s.%d" % (session_id, k)


def fold_names(pieces, total, share):
    """Grouping of client work for one range. pieces: [(full, group, usd)]. Returns
    {full: shown}: a project of a client with several projects keeps its own bubble when
    it weighs at least `share` of the range total, else it shows as the client; a client
    with a single project is shown by the client's name."""
    groups = {}
    for full, group, usd in pieces:
        if group:
            g = groups.setdefault(group, {})
            g[full] = g.get(full, 0.0) + usd
    out = {}
    limit = share * total
    for group, projs in groups.items():
        several = len(projs) > 1
        for full, usd in projs.items():
            own = several and full != group and usd >= limit and usd > 0
            out[full] = full if own else group
    return out


def clean_name(name):
    """A project name typed on the page, or ValueError."""
    if not isinstance(name, str):
        raise ValueError("name must be a string")
    name = " ".join(name.split())
    if not name or len(name) > NAME_MAX:
        raise ValueError("name must be 1 to %d characters" % NAME_MAX)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("name has control characters")
    if name.lower() in RESERVED_NAMES:
        raise ValueError("that name is reserved")
    return name


def _shown_name(name):
    """A cell's current name as the page sends it back, taken as it is (a repo name may be
    long or oddly spaced), but never with control characters."""
    if not isinstance(name, str) or not name or len(name) > 400:
        raise ValueError("bad current name")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise ValueError("name has control characters")
    return name


def _load_for_edit(path):
    """The whole JSON object of a config file before the page changes it. A missing file
    is empty; one that does not parse, or is not an object, raises EditConflict so a typo
    made by hand is never overwritten. Entries the page does not use are kept."""
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise EditConflict(name, "%s cannot be read (%s)" % (name, _why(exc)))
    except (ValueError, RecursionError) as exc:
        raise EditConflict(name, "%s is not valid JSON (%s): fix it by hand first" % (name, _why(exc)))
    if not isinstance(data, dict):
        raise EditConflict(name, "%s is not a JSON object: fix it by hand first" % name)
    return data


def _write_map(path, data):
    """Atomic write through a symlink (the link stays, its target is replaced), keeping the
    file's permissions, fsynced before the rename."""
    real = os.path.realpath(path)
    d = os.path.dirname(real)
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        mode = os.stat(real).st_mode & 0o777
    except OSError:
        mode = None  # a new file: what the umask gives
    tmp = "%s.%d.%d.tmp" % (real, os.getpid(), threading.get_ident())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(data.items())), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, real)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return os.path.getmtime(path)


def _strings(data):
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def rename_project(old, new):
    """Show the cell now called `old` as `new` from now on: every alias entry pointing at
    `old`, and `old` itself (a stored name, or one merged into by an earlier rename), get
    `new`; an entry that would map a name to itself is dropped. Returns the new name."""
    old, new = _shown_name(old), clean_name(new)
    with _EDIT_LOCK:
        alias(old)  # pick up a pending edit of the file first
        path = config_file("aliases.json", "ALIASES")
        data = _load_for_edit(path)
        keys = [k for k, v in data.items() if v == old]
        if not isinstance(data.get(old), str):  # absent, or a hand-written non-string
            keys.append(old)
        for k in keys:
            if k == new:
                data.pop(k, None)
            else:
                data[k] = new
        mtime = _write_map(path, data)
        global ALIASES
        ALIASES = _strings(data)
        _aliases_seen.update(mtime=(path, mtime), checked=time.time())
    _notify_names()
    return new


def _stored_for(shown):
    """A stored name that shows as `shown` now, so a move lands in the visible cell even
    when `shown` is also an old name renamed to something else."""
    if alias(shown) == shown:
        return shown
    for k, v in list(ALIASES.items()):
        if v == shown:
            return k
    return shown


def assign_task(key, project, existing=False):
    """Move a task (key "s:<session id>" or a ticket) to the cell shown as `project`;
    None or "" puts it back on automatic attribution. `existing`: the name of a cell on
    screen, taken as it is (not re-spaced or length-checked like a typed one). Returns the
    name it shows under, which the page reports when it differs from the one typed."""
    if not isinstance(key, str) or not TASK_KEY_RE.match(key):
        raise ValueError("bad task key")
    if project and existing:
        project = _shown_name(project)
        if project.lower() in RESERVED_NAMES:
            raise ValueError("that name is reserved")
    else:
        project = clean_name(project) if project else None
    global ASSIGN
    with _EDIT_LOCK:
        path = config_file("assign.json", "ASSIGN")
        data = _load_for_edit(path)
        stored = _stored_for(project) if project else None
        if stored:
            data[key] = stored
        else:
            data.pop(key, None)
        mtime = _write_map(path, data)
        ASSIGN = _strings(data)
        _assign_seen.update(mtime=(path, mtime), checked=time.time())
    _notify_names()
    return alias(stored) if stored else None


def _load_boards():
    """Ticket prefix -> client label, from boards.json in the user config (optional)."""
    return {k.upper(): v for k, v in _load_map(config_file("boards.json", "BOARDS")).items()}


# ---------------------------------------------------------------------------------------
# Pricing (USD per million tokens). Source: Anthropic pricing as of 2026-11.
# Columns: input, output, cache read, cache write 5 min, cache write 1 h.
# ---------------------------------------------------------------------------------------

PRICES = [
    ("claude-opus-5-5", (4.0, 20.0, 0.20, 5.0, 8.0)),
    ("claude-opus-5", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-8", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-7", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-6", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4-5", (5.0, 25.0, 0.50, 6.25, 10.0)),
    ("claude-opus-4", (15.0, 75.0, 1.50, 18.75, 30.0)),
    ("claude-sonnet-5-5", (2.0, 10.0, 0.10, 2.5, 4.0)),
    ("claude-sonnet-5", (2.0, 10.0, 0.20, 2.5, 4.0)),
    ("claude-sonnet-4", (3.0, 15.0, 0.30, 3.75, 6.0)),
    ("claude-haiku-5-5", (0.10, 0.50, 0.01, 0.125, 0.20)),
    ("claude-haiku-4-5", (1.0, 5.0, 0.10, 1.25, 2.0)),
    ("claude-fable-5-2", (8.0, 40.0, 0.20, 10.0, 16.0)),
    ("claude-mythos-5-2", (8.0, 40.0, 0.20, 10.0, 16.0)),
    ("claude-fable-5-1", (10.0, 50.0, 0.25, 12.5, 20.0)),
    ("claude-mythos-5-1", (10.0, 50.0, 0.25, 12.5, 20.0)),
    ("claude-fable-5", (10.0, 50.0, 1.0, 12.5, 20.0)),
    ("claude-mythos-5", (10.0, 50.0, 1.0, 12.5, 20.0)),
    # Older ids other agents still name (Cline, Aider, OpenCode through a provider).
    ("claude-3-7-sonnet", (3.0, 15.0, 0.30, 3.75, 6.0)),
    ("claude-3-5-sonnet", (3.0, 15.0, 0.30, 3.75, 6.0)),
    ("claude-3-5-haiku", (0.80, 4.0, 0.08, 1.0, 1.6)),
    ("claude-3-opus", (15.0, 75.0, 1.50, 18.75, 30.0)),
    ("claude-3-haiku", (0.25, 1.25, 0.03, 0.30, 0.50)),
]
# Haiku 5.5 above 100K prompt tokens.
HAIKU_55_LONG = (0.50, 2.50, 0.05, 0.625, 1.0)
FAMILY_FALLBACK = [
    ("fable", "claude-fable-5-2"), ("mythos", "claude-mythos-5-2"),
    ("opus", "claude-opus-5-5"), ("sonnet", "claude-sonnet-5-5"), ("haiku", "claude-haiku-5-5"),
]


def price_row(model):
    model = (model or "").lower()
    for prefix, row in PRICES:
        if model == prefix or model.startswith(prefix + "-") or model.startswith(prefix + "@"):
            return prefix, row
    for word, target in FAMILY_FALLBACK:
        if word in model:
            return target, dict(PRICES)[target]
    return "claude-opus-5-5", dict(PRICES)["claude-opus-5-5"]


def bedrock_profile(model):
    """A Bedrock application inference profile ARN, which carries an id, not a model name."""
    return "application-inference-profile" in (model or "").lower()


def unpriced(model):
    """True for a model call_cost prices at 0 because no table knows it (audit of 101)."""
    return bool(model) and not model.startswith("<") and not agents.is_claude(model) \
        and not bedrock_profile(model) and agents.other_price(model)[0] is None


def call_cost(model, inp, out, cread, cw5, cw1h, fast=False):
    """API cost of one call. Claude ids use the table above (a Bedrock or OpenRouter id is
    reduced to its Claude name first); other vendors' models (a Claude Code session pointed
    at GPT or Gemini through a proxy) use their own list price, and a model that is
    neither known nor Claude costs 0 instead of being priced as Opus (counted as unpriced
    by the summary). A Bedrock application inference profile ARN names no model: it is a
    Claude call priced by the family fallback."""
    if model and not model.startswith("<") and not agents.is_claude(model) and not bedrock_profile(model):
        other = agents.other_cost(model, inp, out, cread, cw5 + cw1h)
        return other if other is not None else 0.0
    if model and agents.is_claude(model):
        model = agents.norm_model(model) or model
    key, row = price_row(model)
    if key == "claude-haiku-5-5" and (inp + cread + cw5 + cw1h) > 100000:
        row = HAIKU_55_LONG
    p_in, p_out, p_read, p_w5, p_w1 = row
    usd = (inp * p_in + out * p_out + cread * p_read + cw5 * p_w5 + cw1h * p_w1) / 1e6
    if fast:
        usd *= 2.0
    return usd


# ---------------------------------------------------------------------------------------
# Project and task attribution
# ---------------------------------------------------------------------------------------

TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,11})-(\d{1,6})\b")
HEX_SUFFIX_RE = re.compile(r"-[0-9a-f]{6}$")
T3_SUFFIX_RE = re.compile(r"-t3-[0-9a-f]{6,}$")
_project_cache = {}


def _enc(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def project_of(cwd):
    """Repository or area name for a working directory."""
    if not cwd:
        return "desconocido"
    if cwd in _project_cache:
        return _project_cache[cwd]
    res = _project_of(cwd)  # the stored (original) name; alias() is for display only
    _project_cache[cwd] = res
    return res


def _first(parts):
    return parts[0] if parts else ""


def _under(p, base):
    """Path parts of p below base, or None when p is not base or inside it."""
    if p == base:
        return []
    if p.startswith(base + "/"):
        return p[len(base) + 1:].split("/")
    return None


def _project_of(cwd):
    home = HOME
    p = cwd.rstrip("/") or "/"
    m = re.match(r"^(?:/private)?/tmp/claude-\d+/([^/]+)", p)
    if m:
        enc = m.group(1)
        prefixes = [_enc(w + "/projects/") for w in WORKSPACE_DIRS]
        prefixes += [_enc(PROJECTS_DIR + "/"), _enc(CODE_DIR + "/"), _enc(home + "/")]
        for prefix in prefixes:
            if enc.startswith(prefix):
                rest = enc[len(prefix):].strip("-")
                return rest or "home"
        return enc.strip("-") or "tmp"
    if re.match(r"^(?:/private)?/var/folders/", p) or re.match(r"^(?:/private)?/tmp(/|$)", p):
        return "automatismos"
    if p == home:
        return "home"
    for ws in WORKSPACE_DIRS:
        rel = _under(p, ws)
        if rel is not None:
            if len(rel) > 1 and rel[0] == "projects":
                return rel[1]
            return os.path.basename(ws)
    rel = _under(p, PROJECTS_DIR)
    if rel:
        return resolve_repo("/".join(rel[:2]))
    rel = _under(p, CODE_DIR)
    if rel is not None and CODE_DIR != home:
        return rel[0] if rel else os.path.basename(CODE_DIR)
    if not p.startswith(home + "/"):
        # Outside home: the filesystem root or system folders are where launchd jobs and
        # other automation run.
        if p == "/" or re.match(r"^/(?:usr|opt|Library|System|Applications|etc|var)(/|$)", p):
            return "automatismos"
        return os.path.basename(p) or "automatismos"
    rel = p[len(home) + 1:].split("/")
    if rel[0] == ".worktrees" and len(rel) > 1:
        return rel[1]
    if rel[0] == ".treehouse" and len(rel) > 1:
        return HEX_SUFFIX_RE.sub("", rel[1])
    if rel[0] == ".t3" and len(rel) > 2 and rel[1] == "worktrees":
        return T3_SUFFIX_RE.sub("", rel[2])
    if rel[0] in (".local", ".config", ".cache"):
        return "automatismos"
    return rel[0]


_H = re.escape(HOME)
HINT_PATTERNS = [
    re.compile(_H + r"/\.treehouse/([A-Za-z0-9_.-]+?)-[0-9a-f]{6}(?:/|\b)"),
    re.compile(re.escape(PROJECTS_DIR) + r"/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)"),
    re.compile(r"(?:^|[\s\"'=/])projects/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)"),
    # Apps and tools kept inside an agent-skeleton bundle.
    re.compile(r"local/bundles/[A-Za-z0-9_.-]+/(?:apps|tools)/([A-Za-z0-9_.-]+)/"),
    re.compile(_H + r"/\.t3/worktrees/([A-Za-z0-9_.-]+?)-t3-[0-9a-f]+"),
]
HINT_KEYS = ("file_path", "path", "command", "notebook_path", "pattern", "cwd")


def hints_of(content):
    """Project names that a message's tool calls touch: [(tool_use_id, project)]."""
    out = []
    if not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        inp = block.get("input")
        if not isinstance(inp, dict):
            continue
        text = " ".join(str(inp.get(k)) for k in HINT_KEYS if inp.get(k))
        if not text:
            continue
        found = None
        for pat in HINT_PATTERNS:
            m = pat.search(text)
            if m:
                found = resolve_repo(m.group(1).rstrip("."))
                break
        if found and found not in ("projects", "..", ".") and not found.startswith("-"):
            out.append((block.get("id") or "", found))
    return out


_group_cache = {}


def is_group(name):
    """A folder under the projects folder that holds several repositories."""
    if name not in _group_cache:
        gdir = os.path.join(PROJECTS_DIR, name)
        _group_cache[name] = os.path.isdir(gdir) and not os.path.exists(os.path.join(gdir, ".git"))
    return _group_cache[name]


def resolve_repo(name):
    """`group/repo` under the projects folder -> the repo when `group` is a folder of repos (or
    no longer exists here: paths recorded on another machine keep their group folder)."""
    if "/" not in name:
        return name
    group, repo = name.split("/", 1)
    if is_group(group):
        return repo
    gdir = os.path.join(PROJECTS_DIR, group)
    if not os.path.exists(gdir) and repo and "." not in repo:
        return repo
    return group


def ticket_in(*texts):
    for text in texts:
        if not text:
            continue
        m = TICKET_RE.search(text)
        if m and m.group(1) not in ("UTF", "HTTP", "ISO", "SHA", "AES", "TLS", "IPV"):
            return "%s-%s" % (m.group(1), m.group(2))
    return None


def clean_title(title):
    if not title:
        return None
    title = title.strip()
    if title.startswith("[UTF-8]") or title.startswith("toolu_") or len(title) < 3:
        return None
    return title[:140]


# ---------------------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  msg_id TEXT PRIMARY KEY,
  ts INTEGER NOT NULL,
  session_id TEXT,
  model TEXT,
  cwd TEXT,
  branch TEXT,
  sidechain INTEGER,
  inp INTEGER, out INTEGER, cread INTEGER, cw5 INTEGER, cw1h INTEGER,
  fast INTEGER,
  usd REAL,
  account TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id);
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  custom_title TEXT,
  ai_title TEXT,
  first_prompt TEXT,
  account TEXT
);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY,
  inode INTEGER,
  size INTEGER,
  mtime REAL,
  offset INTEGER,
  owner TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS hints (
  tool_id TEXT PRIMARY KEY,
  session_id TEXT,
  project TEXT
);
CREATE INDEX IF NOT EXISTS hints_session ON hints(session_id);
"""


# Columns added after the first release: (table, column, type).
MIGRATIONS = (("calls", "account", "TEXT"), ("files", "owner", "TEXT"), ("sessions", "account", "TEXT"))
# When the account column first appears, the logs of the last days are read again so the
# calls that matter for the live view get their account (older ones keep NULL).
ACCOUNT_BACKFILL_S = 3 * 86400


def _migrate(con):
    added = []
    for table, col, typ in MIGRATIONS:
        have = {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
        if col not in have:
            con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
            added.append((table, col))
    if ("calls", "account") in added:
        con.execute("UPDATE files SET offset = 0, size = -1, owner = NULL WHERE mtime >= ?",
                    (time.time() - ACCOUNT_BACKFILL_S,))
    # Calls read before their session's owner was known (a subagent file is usually older
    # than its session's file, so it is read first) take the session's account.
    con.execute("UPDATE calls SET account = (SELECT s.account FROM sessions s "
                "WHERE s.session_id = calls.session_id) WHERE account IS NULL AND session_id IN "
                "(SELECT session_id FROM sessions WHERE account IS NOT NULL)")


# ---------------------------------------------------------------------------------------
# Accounts: which tag an account id belongs to (config `account_logins`)
# ---------------------------------------------------------------------------------------

ACCOUNT_LOGINS = SETTINGS["account_logins"]
_ACCOUNT_IDS = {}  # login file -> (mtime, account id)
# A non-active account with calls this recent is "outgoing": chats still running on it.
ACCOUNT_RECENT_MS = 30 * 60 * 1000


def _login_file(where):
    """A config folder holds <folder>/.claude.json, except the default ~/.claude, whose
    file is ~/.claude.json next to it (Claude Code without CLAUDE_CONFIG_DIR)."""
    p = os.path.expanduser(where).rstrip("/")
    if not os.path.isdir(p):
        return p
    inside = os.path.join(p, ".claude.json")
    return inside if os.path.exists(inside) or not os.path.exists(p + ".json") else p + ".json"


def _account_id(path):
    """oauthAccount.accountUuid of one Claude Code config file, re-read when it changes.
    Only the id is kept (never the e-mail or anything else of the file)."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    hit = _ACCOUNT_IDS.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path) as fh:
            d = json.load(fh)
    except (OSError, ValueError, RecursionError):
        # Caught mid-write, say: keep the last good id and try again next time.
        return hit[1] if hit else None
    acc = d.get("oauthAccount") if isinstance(d, dict) else None
    uid = acc.get("accountUuid") if isinstance(acc, dict) else None
    uid = uid if isinstance(uid, str) and uid else None
    _ACCOUNT_IDS[path] = (mtime, uid)
    return uid


def account_map(logins=None):
    """{account id: tag} from the configured logins."""
    out = {}
    for tag, where in (ACCOUNT_LOGINS if logins is None else logins).items():
        uid = _account_id(_login_file(where))
        if uid:
            out[uid] = tag
    return out


# Bedrock ids: "anthropic.claude-...", with or without a cross-region profile prefix
# ("us.", "eu.", "apac.", "jp.", "au.", "us-gov.", "global.", ...), or a full ARN.
BEDROCK_RE = re.compile(r"(^|[.:/])anthropic\.claude")


def call_origin(model, account_id, ids):
    """Where a call's tokens came from: a cloud provider read from the model id (Bedrock
    "anthropic.claude-...", Vertex "claude-...@date", or a model that is not Claude at all:
    "other"), else "acct:<tag>" for a Claude account (`ids`: account id -> tag; the prefix
    keeps a tag named like a provider apart), else "claude" (an account not configured, or
    a call older than the owner records). The provider is only seen when the transcript
    keeps the provider's model id."""
    m = (model or "").lower()
    if BEDROCK_RE.search(m) or "bedrock" in m:
        return "bedrock"
    if "@" in m or m.startswith("publishers/") or "vertex" in m:
        return "vertex"
    if m and not m.startswith("claude") and not m.startswith("<"):
        return "other"
    tag = ids.get(account_id)
    return "acct:" + tag if tag else "claude"


def _origins(counts):
    """{origin: tokens} as [[origin, tokens], ...], biggest first."""
    return [[k, v] for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v > 0]


def account_use(con, lock, now_ms=None):
    """Per account tag: the last call and the calls of the last ACCOUNT_RECENT_MS, so the
    page can tell the active account from the one still finishing older chats."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ids = account_map()
    if not ids:
        return {}
    with lock:
        rows = con.execute("SELECT account, MAX(ts), COUNT(*) FROM calls WHERE ts >= ? "
                           "AND account IS NOT NULL GROUP BY account",
                           (now_ms - ACCOUNT_RECENT_MS,)).fetchall()
    out = {}
    for uid, last, n in rows:
        tag = ids.get(uid)
        if tag:
            cur = out.setdefault(tag, {"last": 0, "calls": 0})
            cur["last"] = max(cur["last"], last)
            cur["calls"] += n
    return out


def connect(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30, check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    _migrate(con)
    con.execute("INSERT OR IGNORE INTO meta (k, v) VALUES ('data_version', ?)", (str(int(time.time())),))
    con.commit()
    return con


def parse_ts(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def parse_line(line):
    """Return ('call', row) / ('title', ...) / ('owner', account id) / None for one JSONL line."""
    if '"session-owner"' in line and '"usage"' not in line:
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if isinstance(d, dict) and d.get("type") == "session-owner" and isinstance(d.get("accountUuid"), str):
            return ("owner", d["accountUuid"][:64])
        return None
    if '"usage"' in line:
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict) or d.get("type") != "assistant":
            return None
        try:
            return _call_from(d)
        except (TypeError, ValueError, AttributeError):
            return None  # a malformed record is skipped, never blocks the rest of the file
    if '"custom-title"' in line or '"ai-title"' in line or '"last-prompt"' in line:
        try:
            d = json.loads(line)
        except ValueError:
            return None
        if not isinstance(d, dict):
            return None
        return _title_from(d)
    return None


def _call_from(d):
    """One assistant record -> ("call", row, hints), or None."""
    msg = d.get("message")
    if not isinstance(msg, dict):
        return None
    usage = msg.get("usage")
    model = msg.get("model") or ""
    if not isinstance(usage, dict) or not model or model.startswith("<"):
        return None
    mid = msg.get("id") or d.get("requestId") or d.get("uuid")
    ts = parse_ts(d.get("timestamp"))
    if not mid or ts is None:
        return None
    inp = int(usage.get("input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    cread = int(usage.get("cache_read_input_tokens") or 0)
    ctotal = int(usage.get("cache_creation_input_tokens") or 0)
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    cw1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    cw5 = int(cc.get("ephemeral_5m_input_tokens") or 0)
    if cw1h + cw5 < ctotal:
        cw5 += ctotal - cw1h - cw5
    fast = 1 if usage.get("speed") == "fast" else 0
    usd = call_cost(model, inp, out, cread, cw5, cw1h, bool(fast))
    hints = [(tid, d.get("sessionId"), proj) for tid, proj in hints_of(msg.get("content"))
             if tid and d.get("sessionId")]
    return ("call", (mid, ts, d.get("sessionId"), model, d.get("cwd"), d.get("gitBranch"),
                     1 if d.get("isSidechain") else 0, inp, out, cread, cw5, cw1h, fast, usd),
            hints)


def _title_from(d):
    t = d.get("type")
    sid = d.get("sessionId")
    if not sid:
        return None
    if t == "custom-title":
        return ("title", sid, "custom_title", clean_title(d.get("customTitle")))
    if t == "ai-title":
        return ("title", sid, "ai_title", clean_title(d.get("aiTitle")))
    if t == "last-prompt":
        return ("title", sid, "first_prompt", clean_title(d.get("lastPrompt")))
    return None


UPSERT_CALL = """
INSERT INTO calls (msg_id, ts, session_id, model, cwd, branch, sidechain, inp, out, cread,
                   cw5, cw1h, fast, usd, account)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(msg_id) DO UPDATE SET
  account = COALESCE(calls.account, excluded.account),
  out = MAX(calls.out, excluded.out),
  inp = MAX(calls.inp, excluded.inp),
  cread = MAX(calls.cread, excluded.cread),
  cw5 = MAX(calls.cw5, excluded.cw5),
  cw1h = MAX(calls.cw1h, excluded.cw1h),
  usd = MAX(calls.usd, excluded.usd),
  ts = MIN(calls.ts, excluded.ts)
"""


class Ingestor:
    def __init__(self, cfg, con, lock):
        self.cfg = cfg
        self.con = con
        self.lock = lock
        self.progress = {"files_total": 0, "files_done": 0, "bytes_total": 0,
                         "bytes_done": 0, "running": False, "last_scan": None,
                         "backfill": True, "error": None}
        self.recent = []  # (seq, row dict) of calls ingested in live mode
        self.seq = 0

    @property
    def version(self):
        """Data version, kept in the database: every committed batch bumps it, whichever
        process wrote it (the server or a hand-run `ingest`). It starts at the creation
        time in seconds, so it only grows."""
        with self.lock:
            row = self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()
        return int(row[0]) if row else 0

    def list_files(self):
        out = []
        # Re-expanded and re-checked every scan: a root missing at startup may appear later
        # (as a link to the home folder, say), a root link may be retargeted.
        patterns = self.cfg.get("root_patterns", self.cfg["roots"])
        roots = checked_roots(patterns, _ROOT_WARNED)
        forbidden, _tails = _forbidden_roots(HOME)
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, files in walk_root(root, forbidden, _ROOT_WARNED):
                for name in files:
                    if name.endswith(".jsonl"):
                        out.append(os.path.join(dirpath, name))
        return out

    def scan(self):
        """One pass over every log. A file lock keeps a hand-run `ingest` and the server
        from scanning at the same time (the upserts are idempotent, the progress is not)."""
        lock_path = self.cfg["db"] + ".scan.lock"
        with open(lock_path, "w") as lock_fh:
            try:
                fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return 0
            try:
                return self._scan()
            finally:
                fcntl.flock(lock_fh, fcntl.LOCK_UN)

    def _scan(self):
        self.progress["error"] = None  # only the latest scan's problems are reported
        files = self.list_files()
        with self.lock:
            known = {r[0]: r[1:] for r in self.con.execute(
                "SELECT path, inode, size, mtime, offset, owner FROM files")}
        todo = []
        total = 0
        for path in files:
            try:
                st = os.stat(path)
            except OSError:
                continue
            prev = known.get(path)
            offset = 0
            owner = None
            if prev:
                inode, size, mtime, offset, owner = prev
                if inode == st.st_ino and size == st.st_size and mtime == st.st_mtime:
                    continue
                if inode != st.st_ino or st.st_size < offset:
                    offset = 0
                    owner = None
            todo.append((path, st, offset, owner))
            total += st.st_size - offset
        self.progress.update(files_total=len(todo), files_done=0, bytes_total=total,
                             bytes_done=0, running=bool(todo))
        changed = 0
        live = not self.progress["backfill"]
        # Oldest first so a fresh database fills in time order.
        todo.sort(key=lambda t: t[1].st_mtime)
        for path, st, offset, owner in todo:
            try:
                changed += self._ingest_file(path, st, offset, live, owner)
            except Exception as exc:  # one bad file must not stop the scan
                self.progress["error"] = "%s: %s" % (os.path.basename(path), exc)
            self.progress["files_done"] += 1
        self.progress["running"] = False
        self.progress["backfill"] = False
        self.progress["last_scan"] = int(time.time() * 1000)
        return changed

    def _ingest_file(self, path, st, offset, live, owner=None):
        """`owner` is the account id the file's last session-owner record named (kept per
        file, so an incremental read still knows it). Every call takes the owner in force
        on its line; a file without one (a subagent's) takes its session's account."""
        calls = []
        own_lines = False
        titles = []
        hints = []
        with open(path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(st.st_size - offset)
        end = data.rfind(b"\n")
        if end < 0:
            return 0
        consumed = end + 1
        for raw in data[:consumed].split(b"\n"):
            if not raw:
                continue
            try:
                line = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            try:
                res = parse_line(line)
            except Exception:
                res = None
            if not res:
                continue
            if res[0] == "owner":
                owner = res[1]
                own_lines = True
            elif res[0] == "call":
                calls.append(res[1] + (owner,))
                hints.extend(res[2])
            elif res[3]:
                titles.append(res[1:])
        self.progress["bytes_done"] += st.st_size - offset
        tags = account_map() if live and calls else {}  # read before the lock, once per batch
        with self.lock:
            cur = self.con.cursor()
            orphans = {c[2] for c in calls if c[14] is None and c[2]}
            if orphans:
                known_acc = dict(cur.execute(
                    "SELECT session_id, account FROM sessions WHERE account IS NOT NULL AND session_id IN (%s)"
                    % ",".join("?" * len(orphans)), list(orphans)))
                calls = [c if c[14] is not None else c[:14] + (known_acc.get(c[2]),) for c in calls]
            new_ids = set()
            if live and calls:
                ids = [c[0] for c in calls]
                have = set()
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    have.update(r[0] for r in cur.execute(
                        "SELECT msg_id FROM calls WHERE msg_id IN (%s)" % ",".join("?" * len(chunk)),
                        chunk))
                new_ids = set(ids) - have
            cur.executemany(UPSERT_CALL, calls)
            cur.executemany("INSERT OR IGNORE INTO hints (tool_id, session_id, project) "
                            "VALUES (?,?,?)", hints)
            for sid, col, val in titles:
                cur.execute("INSERT INTO sessions (session_id) VALUES (?) "
                            "ON CONFLICT(session_id) DO NOTHING", (sid,))
                if col == "first_prompt":
                    cur.execute("UPDATE sessions SET first_prompt = ? WHERE session_id = ? "
                                "AND first_prompt IS NULL", (val, sid))
                else:
                    cur.execute("UPDATE sessions SET %s = ? WHERE session_id = ?" % col, (val, sid))
            if own_lines and owner:
                sids = {c[2] for c in calls if c[2]} or {os.path.splitext(os.path.basename(path))[0]}
                for sid in sids:
                    cur.execute("INSERT INTO sessions (session_id, account) VALUES (?, ?) "
                                "ON CONFLICT(session_id) DO UPDATE SET account = excluded.account",
                                (sid, owner))
                    # Its subagents' calls, often read before this file, and calls written
                    # before the first owner record take the session's account.
                    cur.execute("UPDATE calls SET account = ? WHERE session_id = ? AND account IS NULL",
                                (owner, sid))
            cur.execute("INSERT OR REPLACE INTO files (path, inode, size, mtime, offset, owner) "
                        "VALUES (?,?,?,?,?,?)", (path, st.st_ino, st.st_size, st.st_mtime,
                                                 offset + consumed, owner))
            if calls or titles:
                cur.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
            # Events get their sequence numbers under the same lock as the commit, so a
            # summary (computed under this lock) either counts a call and knows its seq, or
            # neither: the page reconciles live calls by seq, never by time.
            if new_ids:
                now = time.time() * 1000

                def acc_of(c):
                    # A call written before this file's first owner record got the owner
                    # above (UPDATE): its live event says so too.
                    return c[14] if c[14] is not None else (owner if own_lines and owner else None)
                for c in calls:
                    if c[0] in new_ids and now - c[1] < 15 * 60 * 1000:
                        new_ids.discard(c[0])
                        self.seq += 1
                        proj, group = self.live_names(c[2], c[4], c[5], c[1])
                        self.recent.append((self.seq, {
                            "seq": self.seq, "t": c[1], "project": proj, "client": group,
                            "model": c[3], "tokens": c[7] + c[8] + c[9] + c[10] + c[11],
                            "usd": round(c[13], 6), "session": c[2],
                            "account": tags.get(acc_of(c)),
                            "origin": call_origin(c[3], acc_of(c), tags)}))
                self.recent = self.recent[-1000:]
            self.con.commit()
        return len(calls) + len(titles)

    def live_project(self, session_id, cwd, branch=None, ts=None):
        """The summary's attribution, for one live call (its full project name)."""
        return self.live_names(session_id, cwd, branch, ts)[0]

    def live_names(self, session_id, cwd, branch=None, ts=None):
        """(project, client group or None), both as shown, for one live call: a hand move
        first, then the call's classified segment, then the folder rule. The page turns a
        grouped project into its client's bubble with the summary's `fold`."""
        entry = _classified()["sessions"].get(session_id) if session_id else None
        seg = None
        k = None
        if entry:
            k = seg_index(entry, ts if ts is not None else int(time.time() * 1000))
            seg = entry["segs"][k]
        amap = _assign_map()
        if session_id and amap:
            pk = part_key(session_id, entry, k) if entry else None
            forced = amap.get(pk) if pk else None
            forced = forced or amap.get("s:%s" % session_id)
            if not forced and any(not k2.startswith("s:") for k2 in amap):
                if seg:
                    ticket = ticket_in(seg["ticket"]) if seg.get("ticket") else None
                else:
                    with self.lock:
                        row = self.con.execute("SELECT custom_title, ai_title, first_prompt FROM sessions "
                                               "WHERE session_id = ?", (session_id,)).fetchone()
                    title = row and (row[0] or row[1] or row[2])
                    b = None if branch in ("main", "master", "HEAD") else branch
                    ticket = ticket_in(b, title)
                forced = assigned_project(session_id, ticket, pk)
            if forced:
                return alias(forced), None
        if seg:
            full, group = seg_names(seg)
            return alias(full), (alias(group) if group else None)
        repo = project_of(cwd)
        counts = None
        ticket = None
        if session_id and (repo in GENERIC_PROJECTS or is_group(repo)):
            with self.lock:
                counts = session_hints(self.con, threading.RLock(), [session_id]).get(session_id)
                row = self.con.execute("SELECT custom_title, ai_title, first_prompt FROM sessions "
                                       "WHERE session_id = ?", (session_id,)).fetchone()
            title = row and (row[0] or row[1] or row[2])
            if branch in ("main", "master", "HEAD"):
                branch = None
            ticket = ticket_in(branch, title)
        full, group = auto_names(repo, counts, ticket, self.cfg["boards"])
        return alias(full), (alias(group) if group else None)

    ACTIVE_SCAN_S = 10

    def loop(self, stop, warm=None, active=None, wake=None):
        """Scan, warm, wait. `active()` says whether a page is open: then the wait is
        ACTIVE_SCAN_S (live calls show quickly), else scan_seconds. `wake` (an Event set
        by the first request after an idle spell) ends the wait early."""
        while not stop.is_set():
            try:
                self.scan()
                if warm:
                    warm()
            except Exception as exc:
                self.progress["error"] = str(exc)
            wait = self.cfg["scan_seconds"]
            if active and active():
                wait = min(wait, self.ACTIVE_SCAN_S)
            if wake is None:
                stop.wait(wait)
            else:
                wake.wait(wait)
                wake.clear()


# ---------------------------------------------------------------------------------------
# Exchange rate
# ---------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HomeAccounts:
    """Optional account cards (Claude quota per account, Cursor spend), read from a local
    launcher's API (`accounts_url` in config.json: /api/claude-quota, /api/cursor-usage) at most every TTL seconds, in the background: a request never waits
    longer than FIRST_WAIT for the very first read. Only the fields the page draws are
    kept (no e-mail addresses)."""
    TTL = 60
    FIRST_WAIT = 4.0
    ACCOUNT_KEYS = ("tag", "label", "active", "plan", "source", "fetched_at", "note",
                    "session", "weekly", "extra_usage", "warmup")
    CURSOR_KEYS = ("ok", "error", "fetched_at", "team", "membership", "cycle_start",
                   "cycle_end", "spend_usd", "included", "team_pool", "installed")

    def __init__(self, cfg, opener=None):
        base = cfg.get("home_url") or ""
        host = urllib.parse.urlsplit(base).hostname
        # Local only: the page shows what this Mac's launcher already knows.
        self.base = base.rstrip("/") if host in ("127.0.0.1", "localhost", "::1") else ""
        self.opener = opener or urllib.request.build_opener(_NoRedirect)
        self.colors = {str(k): str(v) for k, v in (cfg.get("account_colors") or {}).items()}
        self.data = None
        self.at = 0.0
        self.busy = threading.Lock()
        self.done = threading.Event()

    def _get(self, path):
        req = urllib.request.Request(self.base + path, headers={"Accept": "application/json"})
        with self.opener.open(req, timeout=5) as resp:
            return json.loads(resp.read(2_000_000).decode("utf-8"))

    def refresh(self):
        if not self.busy.acquire(blocking=False):
            return
        try:
            out = {"claude": None, "cursor": None, "at": int(time.time() * 1000), "error": None,
                   "colors": self.colors}
            try:
                q = self._get("/api/claude-quota")
                out["claude"] = {"updated_at": q.get("updated_at"),
                                 "accounts": [{k: a.get(k) for k in self.ACCOUNT_KEYS}
                                              for a in q.get("accounts") or []]}
            except Exception as exc:
                out["error"] = "quota: %s" % exc.__class__.__name__
                out["error_code"] = "fetch_failed"
            try:
                c = self._get("/api/cursor-usage")
                out["cursor"] = {k: c.get(k) for k in self.CURSOR_KEYS}
            except Exception as exc:
                out["error"] = (out["error"] + "; " if out["error"] else "") + "cursor: %s" % exc.__class__.__name__
                out["error_code"] = "fetch_failed"
            # A failed read keeps the last good numbers, marked old.
            if self.data and out["claude"] is None:
                out["claude"] = dict(self.data.get("claude") or {}, stale=True) if self.data.get("claude") else None
            if self.data and out["cursor"] is None:
                out["cursor"] = dict(self.data.get("cursor") or {}, stale=True) if self.data.get("cursor") else None
            self.data = out
            self.at = time.time()
        finally:
            self.busy.release()
            self.done.set()

    def get(self):
        if not self.base:
            # error_code is stable for the page; error is an English message for logs.
            return {"claude": None, "cursor": None, "at": None, "error": "no account source",
                    "error_code": "no_source", "colors": self.colors}
        if time.time() - self.at > self.TTL and not self.busy.locked():
            threading.Thread(target=self.refresh, daemon=True).start()
        if self.data is None:
            self.done.wait(self.FIRST_WAIT)
        return self.data or {"claude": None, "cursor": None, "at": None, "error": "reading",
                             "error_code": "loading", "colors": self.colors}


class ControlBusy(Exception):
    """An account switch started from the page is still running (`tag`: its account)."""
    def __init__(self, message, tag=None):
        Exception.__init__(self, message)
        self.tag = tag


ACCOUNT_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def parse_hide(raw):
    """`?hide=acct:mt,acct:na` (L-159): the Claude accounts left out of a summary. Only
    well-formed `acct:<tag>` values, at most 16; anything else is a 400."""
    if not raw:
        return ()
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        if not part.startswith("acct:") or not ACCOUNT_TAG_RE.match(part[5:]):
            raise ValueError("bad hide value")
        out.append(part)
    if len(out) > 16:
        raise ValueError("too many hidden accounts")
    return tuple(sorted(set(out)))


class AccountControl:
    """The optional account control (config `account_control`): a local command that
    reports the accounts' priority and score (`state`), switches the default account
    (`activate <tag>`, minutes long, started in the background; its progress comes back in
    `state`) and saves the priority (`order a,b,c`). Latidos never touches an account
    itself: it only runs that command, after a click on the page."""
    TTL = 15
    STATE_TIMEOUT = 25
    ORDER_TIMEOUT = 15

    def __init__(self, cfg, runner=None, spawner=None):
        argv = [str(a) for a in (cfg.get("account_control") or []) if str(a).strip()]
        self.argv = [os.path.expanduser(a) for a in argv]
        self.run = runner or (lambda args, timeout: subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL))
        self.spawn = spawner or (lambda args: subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
        self.data = None
        self.at = 0.0
        # Re-entrant: activate checks, spawns and stamps under one hold (audit of 77: two
        # requests could both pass the check and launch two switches).
        self.lock = threading.RLock()
        self.started = 0.0   # when this server last started an activation
        self.started_tag = None  # ...and for which account

    @property
    def enabled(self):
        return bool(self.argv)

    def _json(self, args, timeout):
        res = self.run(self.argv + args, timeout)
        try:
            data = json.loads((res.stdout or "").strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise ValueError("control answered no JSON (exit %s)" % res.returncode)
        if not isinstance(data, dict):
            raise ValueError("control answered no object")
        return res.returncode, data

    FRESH_MIN_S = 3   # ?fresh=1 re-runs the command at most this often (a page polls it)

    def state(self, fresh=False):
        if not self.enabled:
            return {"enabled": False}
        with self.lock:
            if fresh and time.time() - self.at < self.FRESH_MIN_S:
                fresh = False
            if fresh or self.data is None or time.time() - self.at > self.TTL:
                try:
                    _, data = self._json(["state"], self.STATE_TIMEOUT)
                    self.data = dict(data, enabled=True, error=None)
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    self.data = dict(self.data or {}, enabled=True, error="state: %s" % exc.__class__.__name__,
                                     error_code="control_failed")
                self.at = time.time()
            return dict(self.data, busy=self._busy(self.data), launch=self._launch(self.data))

    LAUNCH_WAIT_S = 60   # a launched run must write its start within this

    @staticmethod
    def _begun(act):
        try:
            return dt.datetime.fromisoformat(str(act.get("started_at")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _busy(self, data):
        """An activation still running: the command's phase says so (the command itself
        reports a dead run as done/stale, so there is no time cap here: audit of 78, a live
        run of 25 to 35 min looked finished), or this server launched one less than
        LAUNCH_WAIT_S ago and the command has not written its start yet."""
        act = (data or {}).get("activation") or {}
        if act.get("phase") not in (None, "done"):
            return True
        begun = self._begun(act)
        caught_up = begun is not None and begun >= self.started - 0.5
        return bool(time.time() - self.started < self.LAUNCH_WAIT_S and not caught_up)

    def _launch(self, data):
        """What became of the last run this server launched, judged here so a viewer's clock
        does not matter (audit of 78): `started_ms`, and `failed` once the command never
        wrote a start within LAUNCH_WAIT_S."""
        if not self.started:
            return None
        begun = self._begun((data or {}).get("activation") or {})
        caught_up = begun is not None and begun >= self.started - 0.5
        waiting = not caught_up and time.time() - self.started < self.LAUNCH_WAIT_S
        return {"started_ms": int(self.started * 1000), "tag": self.started_tag,
                # Launched and not reported by the command yet: the page shows this account
                # as switching, not the previous run's (audit of 79).
                "pending": bool(waiting),
                "failed": bool(not caught_up and not waiting)}

    def _known(self, tag):
        tags = [a.get("tag") for a in (self.state().get("accounts") or []) if isinstance(a, dict)]
        return tag in tags

    def activate(self, tag):
        if not self.enabled:
            raise PermissionError("no account control configured")
        if not isinstance(tag, str) or not ACCOUNT_TAG_RE.match(tag):
            raise ValueError("unknown account")
        with self.lock:
            if not self._known(tag):
                raise ValueError("unknown account")
            self.at = 0.0
            now_state = self.state(fresh=True)
            if now_state.get("busy"):
                launch = now_state.get("launch") or {}
                raise ControlBusy("an account switch is already running",
                                  launch.get("tag") if launch.get("pending") else (now_state.get("activation") or {}).get("tag"))
            self.started, self.started_tag = time.time(), tag
            try:
                self.spawn(self.argv + ["activate", tag])
            except OSError:
                self.started, self.started_tag = 0.0, None
                raise
            self.at = 0.0
        return {"ok": True, "tag": tag, "started": True, "started_ms": int(self.started * 1000)}

    def order(self, order):
        if not self.enabled:
            raise PermissionError("no account control configured")
        if (not isinstance(order, list) or not order or len(order) > 16
                or not all(isinstance(t, str) and ACCOUNT_TAG_RE.match(t) for t in order)
                or len(set(order)) != len(order) or not all(self._known(t) for t in order)):
            raise ValueError("bad order")
        code, data = self._json(["order", ",".join(order)], self.ORDER_TIMEOUT)
        if code != 0 or not data.get("ok"):
            raise ValueError("control refused the order: %s" % data.get("error", code))
        with self.lock:
            self.at = 0.0
        return {"ok": True, "order": data.get("order") or order}


class Logins:
    """Login expiry per Claude account and a one-click renewal (L-161), through the
    configured `account_logins_tool` (accounts/claude_accounts.py). Latidos only runs that
    command: `status --json` (cached) and, after a click, `login <tag>` in the background,
    which opens claude.ai in the account's own Chrome profile. Cursor's session expiry
    (L-162) is read here too; no token ever reaches the page."""
    TTL = 60
    BUSY_TTL = 4          # while a renewal runs the page asks every 5 s
    STATUS_TIMEOUT = 25
    SOON_DAYS = 3
    KEEP = ("tag", "state", "expires_at", "days_left", "renewing")

    def __init__(self, cfg, runner=None, spawner=None, cursor_expiry=None):
        argv = [str(a) for a in (cfg.get("account_logins_tool") or []) if str(a).strip()]
        self.argv = [os.path.expanduser(a) for a in argv]
        self.run = runner or (lambda args, timeout: subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL))
        self.spawn = spawner or (lambda args: subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True))
        self.cursor_expiry = cursor_expiry or cursor_source.session_expiry
        self.lock = threading.Lock()
        self.data = None
        self.at = 0.0
        self.started = {}  # tag -> when this server launched its renewal

    @property
    def enabled(self):
        return bool(self.argv)

    def _claude(self):
        res = self.run(self.argv + ["status", "--json"], self.STATUS_TIMEOUT)
        try:
            raw = json.loads((res.stdout or "").strip() or "{}")
        except ValueError:
            raise ValueError("logins tool answered no JSON (exit %s)" % res.returncode)
        rows = raw.get("accounts") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            raise ValueError("logins tool answered no accounts")
        out = []
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("tag"), str) and ACCOUNT_TAG_RE.match(r["tag"]):
                row = {k: r.get(k) for k in self.KEEP}
                # Launched here a moment ago: the tool may not hold its lock yet.
                if time.time() - self.started.get(r["tag"], 0) < 15:
                    row["renewing"] = True
                out.append(row)
        return out

    def _cursor(self, now=None):
        now = time.time() if now is None else now
        exp = self.cursor_expiry()
        if not exp:
            return None
        left = (exp - now) / 86400.0
        state = "expired" if left <= 0 else "soon" if left <= self.SOON_DAYS else "ok"
        return {"expires_at": dt.datetime.fromtimestamp(exp).astimezone().isoformat(timespec="seconds"),
                "days_left": round(left, 1), "state": state}

    def status(self):
        with self.lock:
            busy = self.data and any(a.get("renewing") for a in self.data.get("claude") or [])
            if self.data is None or time.time() - self.at > (self.BUSY_TTL if busy else self.TTL):
                data = {"enabled": self.enabled, "claude": [], "error": None}
                if self.enabled:
                    try:
                        data["claude"] = self._claude()
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        data["error"] = "status: %s" % exc.__class__.__name__
                        data["claude"] = (self.data or {}).get("claude") or []
                try:
                    data["cursor"] = self._cursor()
                except Exception:  # an unreadable Cursor never hides the Claude lines
                    data["cursor"] = None
                self.data, self.at = data, time.time()
            return dict(self.data)

    def renew(self, tag):
        if not self.enabled:
            raise PermissionError("no logins tool configured")
        if not isinstance(tag, str) or not ACCOUNT_TAG_RE.match(tag):
            raise ValueError("unknown account")
        known = {a["tag"]: a for a in self.status().get("claude") or []}
        if tag not in known:
            raise ValueError("unknown account")
        with self.lock:
            if known[tag].get("renewing") or time.time() - self.started.get(tag, 0) < 15:
                raise ControlBusy("a renewal of that account is already running", tag)
            try:
                self.spawn(self.argv + ["login", tag])
            except OSError as exc:  # answered 504 "control: ...", not "could not save"
                raise subprocess.SubprocessError("could not start the logins tool: %s" % _why(exc))
            self.started[tag] = time.time()
            self.at = 0.0  # the next status shows it renewing
        return {"ok": True, "tag": tag}


class Fx:
    def __init__(self, cfg, con, lock):
        self.cfg = cfg
        self.con = con
        self.lock = lock

    RETRY_SECONDS = 1800
    _last_attempt = 0.0

    def _fallback(self):
        return {"usd_eur": self.cfg["eur_fallback"], "date": None, "source": "fijo", "checked": None}

    def _cached(self):
        with self.lock:
            row = self.con.execute("SELECT v FROM meta WHERE k='fx'").fetchone()
        return json.loads(row[0]) if row else None

    def get(self):
        """The stored rate; never touches the network (refresh() does, in the background)."""
        cached = self._cached()
        if not cached:
            return self._fallback()
        if cached.get("checked") != dt.date.today().isoformat():
            return dict(cached, stale=True)
        return cached

    def refresh(self):
        cached = self._cached()
        today = dt.date.today().isoformat()
        if cached and cached.get("checked") == today:
            return cached
        if time.time() - self._last_attempt < self.RETRY_SECONDS:
            return dict(cached, stale=True) if cached else self._fallback()
        self._last_attempt = time.time()
        try:
            req = urllib.request.Request(self.cfg["fx_url"], headers={"User-Agent": "latidos/1"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.load(resp)
            rate = float(data["rates"]["EUR"])
            if not 0.1 < rate < 10:
                raise ValueError("rate out of range")
            fx = {"usd_eur": rate, "date": data.get("date") or today, "source": "BCE",
                  "checked": today}
        except Exception:
            if cached:
                cached = dict(cached, stale=True)
                return cached
            return self._fallback()
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('fx', ?)",
                             (json.dumps(fx),))
            self.con.commit()
        return fx


# ---------------------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------------------

BLOCK_MS = 5 * 3600 * 1000
WEEK_MS = 7 * 24 * 3600 * 1000


def _tokens(r):
    return r["inp"] + r["out"] + r["cread"] + r["cw5"] + r["cw1h"]


def load_rows(con, lock, t0, t1, on_locked=None, classified=None):
    """Calls of the range grouped per session, model, folder, branch, account and hour;
    with `classified` (sessions of classified.json), groups that straddle a segment
    boundary are cut there (split_rows), under the same lock as the read, so a call
    committed meanwhile is in neither or both, like the live sequence read by on_locked."""
    with lock:
        if on_locked:
            on_locked()  # runs under the same lock as the read (the live sequence)
        # Aggregated in SQL per session, model, folder, branch and hour: ~30x fewer rows
        # than calls. Every bin unit and every 5-hour window boundary is an hour boundary,
        # so nothing downstream needs finer time. `ts` is the group's first call.
        cur = con.execute(
            "SELECT MIN(c.ts) AS ts, MAX(c.ts) AS ts_last, COUNT(*) AS n, c.session_id, "
            "c.model, c.cwd, c.branch, SUM(c.inp) AS inp, SUM(c.out) AS out, "
            "SUM(c.cread) AS cread, SUM(c.cw5) AS cw5, SUM(c.cw1h) AS cw1h, SUM(c.usd) AS usd, "
            "s.custom_title, s.ai_title, s.first_prompt, c.account AS account "
            "FROM calls c LEFT JOIN sessions s ON s.session_id = c.session_id "
            "WHERE c.ts >= ? AND c.ts < ? "
            "GROUP BY c.session_id, c.model, c.cwd, c.branch, c.account, c.ts / 3600000 ORDER BY ts", (t0, t1))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        if classified:
            rows = split_rows(con, rows, classified, t0, t1)
        return rows


def split_rows(con, rows, classified, t0, t1):
    """load_rows groups calls per hour; a classified session whose segment changes inside
    such a group has the group read again call by call and cut at the segment boundaries,
    so every piece gets its own calls (and its own cost). The caller holds the lock of
    the grouped read (load_rows)."""
    out = []
    for r in rows:
        entry = classified.get(r["session_id"]) if r["session_id"] else None
        if not entry or seg_index(entry, r["ts"]) == seg_index(entry, r["ts_last"]):
            out.append(r)
            continue
        h0 = r["ts"] - r["ts"] % 3600000
        calls = con.execute(
            "SELECT ts, inp, out, cread, cw5, cw1h, usd FROM calls WHERE session_id = ? "
            "AND model IS ? AND cwd IS ? AND branch IS ? AND account IS ? AND ts >= ? AND ts < ? "
            "ORDER BY ts", (r["session_id"], r["model"], r["cwd"], r["branch"], r["account"],
                            max(h0, t0), min(h0 + 3600000, t1))).fetchall()
        parts = {}
        for ts, inp, o, cread, cw5, cw1h, usd in calls:
            k = seg_index(entry, ts)
            part = parts.get(k)
            if part is None:
                part = parts[k] = dict(r, ts=ts, ts_last=ts, n=0, inp=0, out=0, cread=0, cw5=0, cw1h=0, usd=0.0)
            part["ts_last"] = ts
            part["n"] += 1
            part["inp"] += inp or 0
            part["out"] += o or 0
            part["cread"] += cread or 0
            part["cw5"] += cw5 or 0
            part["cw1h"] += cw1h or 0
            part["usd"] += usd or 0.0
        out.extend(parts[k] for k in sorted(parts))
        if not parts:  # defensive only: the re-read runs under the grouped read's lock
            out.append(r)
    out.sort(key=lambda r: r["ts"])
    return out


def session_blocks(rows):
    """Claude's rolling 5-hour windows: a block starts at the hour of the first call after
    the previous block ended. Returns [(start_ms, usd)]."""
    blocks = []
    start = None
    usd = 0.0
    for r in rows:
        if start is None or r["ts"] >= start + BLOCK_MS:
            if start is not None:
                blocks.append((start, usd))
            start = r["ts"] - (r["ts"] % 3600000)
            usd = 0.0
        usd += r["usd"]
    if start is not None:
        blocks.append((start, usd))
    return blocks


def week_key(ms):
    d = dt.datetime.fromtimestamp(ms / 1000).date()
    monday = d - dt.timedelta(days=d.weekday())
    return monday.isoformat()


def coverage(blocks, plan_main, plan_others, pro_block, pro_week, in_range=None):
    """How much of the usage one main seat could have absorbed. Units: API-equivalent USD.

    Every block of the whole history is replayed in order, so a week's allowance already
    spent before the range is not available inside it. Each block takes what the seat can
    still give (its 5-hour cap, then what is left of its week). `in_range[i]` is the part of
    block i's cost that falls inside the range (None: all of it); a block straddling a range
    edge contributes its take in that proportion. Week figures cover the weeks the range
    touches, whole."""
    cap_block = plan_main * pro_block
    cap_week = plan_main * pro_week
    left = {}
    week_take = {}
    week_usd = {}
    main = total = 0.0
    short = count = 0
    touched = set()
    for i, (start, usd) in enumerate(blocks):
        wk = week_key(start)
        rem = left.setdefault(wk, cap_week)
        take = min(usd, cap_block, rem)
        left[wk] = rem - take
        week_take[wk] = week_take.get(wk, 0.0) + take
        week_usd[wk] = week_usd.get(wk, 0.0) + usd
        part = usd if in_range is None else in_range[i]
        if part <= 0 or usd <= 0:
            continue
        touched.add(wk)
        count += 1
        main += take * (part / usd)
        total += part
        if usd > take + 1e-9:
            short += 1
    weeks = len(touched)
    wk_main = sum(week_take[w] for w in touched)
    wk_rest = sum(week_usd[w] - week_take[w] for w in touched)
    return {"main_usd": main, "total_usd": total, "short_blocks": short, "blocks": count,
            "share_main": (main / total) if total else 0.0, "weeks": weeks,
            "week_main_usd": wk_main, "week_rest_usd": wk_rest}


def block_shares(blocks, rows):
    """Cost of `rows` (sorted by ts) that falls in each block, aligned with `blocks`."""
    starts = [b[0] for b in blocks]
    out = [0.0] * len(blocks)
    for r in rows:
        i = bisect.bisect_right(starts, r["ts"]) - 1
        if i >= 0 and r["ts"] < starts[i] + BLOCK_MS:
            out[i] += r["usd"]
    return out


def calibrate(all_blocks, plan_total):
    """Pro allowance (API-equivalent USD) per 5-hour window and per week, assuming the
    busiest window and the busiest week used every account to the limit. A lower bound:
    if the accounts were never saturated the real allowance is larger."""
    if not all_blocks:
        return 1.0, 1.0
    peak_block = max(u for _s, u in all_blocks)
    weeks = {}
    for s, u in all_blocks:
        weeks[week_key(s)] = weeks.get(week_key(s), 0.0) + u
    peak_week = max(weeks.values())
    return peak_block / plan_total, peak_week / plan_total


def pick_unit(span_ms):
    if span_ms <= 2 * 86400000:
        return "hour"
    if span_ms <= 400 * 86400000:
        return "day"
    return "week"


_BIN_CACHE = {}


def bin_start(ms, unit):
    """Start of the hour, day or week holding ms (local time). Memoised per hour: every
    unit boundary is an hour boundary, and a summary asks for ~4 bins per call."""
    key = (ms // 3600000, unit)
    hit = _BIN_CACHE.get(key)
    if hit is None:
        if len(_BIN_CACHE) > 200000:
            _BIN_CACHE.clear()
        hit = _BIN_CACHE[key] = _bin_start(ms, unit)
    return hit


def _bin_start(ms, unit):
    d = dt.datetime.fromtimestamp(ms / 1000)
    if unit == "hour":
        d = d.replace(minute=0, second=0, microsecond=0)
    else:
        d = d.replace(hour=0, minute=0, second=0, microsecond=0)
        if unit == "week":
            d = d - dt.timedelta(days=d.weekday())
    return int(d.timestamp() * 1000)


# Folders whose sessions are attributed by what their tools touched: the agent workspaces,
# the home folder and automation.
GENERIC_PROJECTS = tuple(os.path.basename(w) for w in WORKSPACE_DIRS) + ("home", "automatismos")


def session_hints(con, lock, session_ids):
    out = {}
    with lock:
        for i in range(0, len(session_ids), 500):
            chunk = session_ids[i:i + 500]
            for sid, proj, n in con.execute(
                    "SELECT session_id, project, COUNT(*) FROM hints WHERE session_id IN (%s) "
                    "GROUP BY session_id, project" % ",".join("?" * len(chunk)), chunk):
                out.setdefault(sid, {})[proj] = n
    return out


def attribute_project(repo, hint_counts, ticket, boards):
    """One rule for summaries and live calls: the folder's repo; for a generic or group
    folder, the project the session's tools mostly touched, else its ticket's client
    (boards.json), else the folder."""
    if repo in GENERIC_PROJECTS or is_group(repo):
        hinted = dominant_hint(hint_counts)
        if hinted:
            return alias(hinted)
        if ticket:
            prefix = ticket.split("-")[0]
            return alias(boards.get(prefix, prefix))
    return alias(repo)


def auto_names(repo, hint_counts, ticket, boards):
    """(stored full name, client group or None) for a session or a segment the model has
    not classified: attribute_project's rule, then what classify.py learned about that
    folder (a repository whose classified work mostly went to one client project goes
    there too); a ticket's client from boards.json is its own group."""
    folders = _classified()["folders"]
    name = repo
    if repo in GENERIC_PROJECTS or is_group(repo):
        hinted = dominant_hint(hint_counts)
        if hinted:
            name = hinted
        elif ticket:
            prefix = ticket.split("-")[0]
            if prefix in boards:
                return boards[prefix], boards[prefix]
            return prefix, None
    f = folders.get(name)
    if f:
        return seg_names(f)
    return name, None


def dominant_hint(counts, min_hits=3, min_share=0.4):
    """The project a session mostly worked in, judged by the paths its tools touched."""
    if not counts:
        return None
    proj, n = max(counts.items(), key=lambda kv: kv[1])
    if n >= min_hits and n >= min_share * sum(counts.values()):
        return proj
    return None


def all_blocks_of(con, lock):
    with lock:
        all_rows = [{"ts": a, "usd": b} for a, b in con.execute(
            "SELECT ts, usd FROM calls ORDER BY ts")]
    return session_blocks(all_rows), (all_rows[0]["ts"] if all_rows else None)


def summarize(con, lock, cfg, t0, t1, unit=None, blocks=None, on_locked=None, hide=()):
    """`hide`: origins ("acct:<tag>") left out of everything but `by_origin`, which counts
    every row of the range so a hidden account keeps its share (L-159)."""
    classified = _classified()["sessions"]
    rows = load_rows(con, lock, t0, t1, on_locked, classified)
    boards = cfg["boards"]
    ids = account_map()
    by_origin = {}
    for r in rows:
        r["origin"] = org = call_origin(r["model"], r.get("account"), ids)
        o = by_origin.setdefault(org, {"usd": 0.0, "tokens": 0, "calls": 0})
        o["usd"] += r["usd"]
        o["tokens"] += _tokens(r)
        o["calls"] += r["n"]
    all_rows = rows  # the plans' figures (accounts) stay whole whatever is hidden
    if hide:
        rows = [r for r in rows if r["origin"] not in hide]
    totals = {"tokens": 0, "inp": 0, "out": 0, "cread": 0, "cwrite": 0, "usd": 0.0, "calls": 0, "unpriced": 0}
    unpriced_memo = {}
    sessions = {}
    models = {}
    span = (t1 - t0) if t0 > 0 else ((rows[-1]["ts_last"] - rows[0]["ts"]) if rows else 86400000)
    unit = unit if unit in ("hour", "day", "week") else pick_unit(span)
    bins = {}
    for r in rows:
        tok = _tokens(r)
        totals["tokens"] += tok
        totals["inp"] += r["inp"]
        totals["out"] += r["out"]
        totals["cread"] += r["cread"]
        totals["cwrite"] += r["cw5"] + r["cw1h"]
        totals["usd"] += r["usd"]
        totals["calls"] += r["n"]
        m = r["model"]
        if m not in unpriced_memo:
            unpriced_memo[m] = unpriced(m)
        if unpriced_memo[m]:
            totals["unpriced"] += r["n"]  # calls of a model with no known price (cost 0)
        sid = r["session_id"] or "?"
        # A classified session counts once per segment (a "piece"); others once.
        entry = classified.get(sid)
        k = seg_index(entry, r["ts"]) if entry else None
        pkey = sid if k is None else "%s#%d" % (sid, k)
        s = sessions.get(pkey)
        if s is None:
            title = r["custom_title"] or r["ai_title"] or r["first_prompt"]
            seg = entry["segs"][k] if entry else None
            if seg and seg.get("task"):
                title = seg["task"]
            s = sessions[pkey] = {
                "id": sid, "seg": seg, "part": part_key(sid, entry, k) if entry else None,
                "title": title, "cwd": r["cwd"], "branch": None, "ticket": None,
                "tokens": 0, "usd": 0.0, "calls": 0, "first": r["ts"], "last": r["ts_last"],
                "cwds": {}, "origins": {}}
        org = r["origin"]
        s["origins"][org] = s["origins"].get(org, 0) + tok
        s["tokens"] += tok
        s["usd"] += r["usd"]
        s["calls"] += r["n"]
        s["first"] = min(s["first"], r["ts"])
        s["last"] = max(s["last"], r["ts_last"])
        if r["branch"] and r["branch"] not in ("main", "master", "HEAD") and not s["branch"]:
            s["branch"] = r["branch"]
        if r["cwd"]:
            s["cwds"][r["cwd"]] = s["cwds"].get(r["cwd"], 0) + r["n"]
        mk = price_row(r["model"])[0]
        m = models.setdefault(mk, {"model": mk, "tokens": 0, "usd": 0.0, "calls": 0})
        m["tokens"] += tok
        m["usd"] += r["usd"]
        m["calls"] += r["n"]
        b = bin_start(r["ts"], unit)
        bb = bins.setdefault(b, {"t": b, "tokens": 0, "usd": 0.0, "calls": 0})
        bb["tokens"] += tok
        bb["usd"] += r["usd"]
        bb["calls"] += r["n"]

    # Projects and tasks.
    hint_counts = session_hints(con, lock, list({s["id"] for s in sessions.values()}))
    # First every piece's full name and client group, then the per-range grouping.
    for s in sessions.values():
        seg = s["seg"]
        if seg:
            ticket = ticket_in(seg["ticket"]) if seg.get("ticket") else None
        else:
            ticket = ticket_in(s["branch"], s["title"])
        s["ticket"] = ticket
        akey = assigned_key(s["id"], ticket, s["part"])
        s["forced"] = _assign_map().get(akey) if akey else None
        s["akey"] = akey
        if s["forced"]:
            s["full"], s["group"] = s["forced"], None
        elif seg:
            s["full"], s["group"] = seg_names(seg)
        else:
            cwd = max(s["cwds"].items(), key=lambda kv: kv[1])[0] if s["cwds"] else s["cwd"]
            s["full"], s["group"] = auto_names(project_of(cwd), hint_counts.get(s["id"]), ticket, boards)
    shown = fold_names([(s["full"], s["group"], s["usd"]) for s in sessions.values()],
                       totals["usd"], cfg.get("fold_share", 0.03))
    fold = {}
    for full, to in shown.items():
        fold[alias(full)] = alias(to)
    projects = {}
    for s in sessions.values():
        ticket = s["ticket"]
        forced = s["forced"]
        akey = s["akey"]
        pname = alias(shown.get(s["full"], s["full"]))
        s["project"] = pname
        p = projects.setdefault(pname, {"name": pname, "tokens": 0, "usd": 0.0, "calls": 0,
                                        "sessions": 0, "last": 0, "first": None, "tasks": {},
                                        "origins": {}})
        for k, v in s["origins"].items():
            p["origins"][k] = p["origins"].get(k, 0) + v
        p["tokens"] += s["tokens"]
        p["usd"] += s["usd"]
        p["calls"] += s["calls"]
        if s["id"] not in p.setdefault("_sids", set()):  # a session split in pieces counts once
            p["_sids"].add(s["id"])
            p["sessions"] += 1
        p["last"] = max(p["last"], s["last"])
        p["first"] = s["first"] if p["first"] is None else min(p["first"], s["first"])
        tkey = ticket or s["part"] or ("s:" + s["id"])
        t = p["tasks"].get(tkey)
        if t is None:
            t = p["tasks"][tkey] = {"key": tkey, "ticket": ticket, "title": s["title"],
                                    "tokens": 0, "usd": 0.0, "sessions": 0, "last": 0, "sids": [],
                                    "assigned": False, "origins": {}}
            # A task shown in its client's bubble names the project it belongs to.
            if not forced and s["group"] and s["full"] != s["group"] and pname == alias(s["group"]):
                t["sub"] = s["seg"]["project"] if s["seg"] else s["full"].split(GROUP_SEP, 1)[-1]
        for k, v in s["origins"].items():
            t["origins"][k] = t["origins"].get(k, 0) + v
        if forced:
            t["assigned"] = True
            t["assign_key"] = akey  # what "Volver a automático" removes
        if not t["title"] and s["title"]:
            t["title"] = s["title"]
        t["tokens"] += s["tokens"]
        t["usd"] += s["usd"]
        if all(i != s["id"] for _, i in t["sids"]):
            t["sessions"] += 1
            t["sids"].append((s["last"], s["id"]))
        else:
            t["sids"] = [(max(l, s["last"]) if i == s["id"] else l, i) for l, i in t["sids"]]
        t["last"] = max(t["last"], s["last"])
    plist = []
    for p in projects.values():
        tasks = sorted(p["tasks"].values(), key=lambda x: -x["usd"])
        p["task_count"] = len(tasks)
        # sids: the task's sessions, newest first (capped), so the page can match a live
        # call (which carries its session) to its task row.
        # The 12 most expensive tasks, then the 5 most recent ones not among them: the sheet's
        # "Última actividad" and "Reciente" order need the newest, often cheap, tasks.
        keep = tasks[:12]
        keep += [t for t in sorted(tasks[12:], key=lambda x: -x["last"])[:5]]
        # slast: each listed session's own last call, so a live call of a session goes to
        # the row of its current segment even when a ticket row merges other sessions.
        p["tasks"] = [dict(t, usd=round(t["usd"], 4), sids=[i for _, i in sorted(t["sids"], reverse=True)[:20]],
                           slast={i: l for l, i in sorted(t["sids"], reverse=True)[:20]},
                           origins=_origins(t["origins"]))
                      for t in keep]
        p["origins"] = _origins(p["origins"])
        p["usd"] = round(p["usd"], 4)
        p.pop("_sids", None)
        plist.append(p)
    plist.sort(key=lambda x: -x["usd"])

    timeline = sorted(bins.values(), key=lambda b: b["t"])
    for b in timeline:
        b["usd"] = round(b["usd"], 4)

    # Moments worth a floating label.
    moments = []
    day_bins = {}
    hour_bins = {}
    days = {}
    for r in rows:
        db = bin_start(r["ts"], "day")
        day_bins[db] = day_bins.get(db, 0.0) + r["usd"]
        dd = days.setdefault(db, {"t": db, "tokens": 0, "usd": 0.0, "calls": 0})
        dd["tokens"] += _tokens(r)
        dd["usd"] += r["usd"]
        dd["calls"] += r["n"]
        hb = bin_start(r["ts"], "hour")
        hour_bins[hb] = hour_bins.get(hb, 0.0) + r["usd"]
    if rows and t0 <= 0:
        moments.append({"kind": "first", "t": rows[0]["ts"], "usd": None,
                        "label": "Primer registro"})
    if day_bins:
        t, u = max(day_bins.items(), key=lambda kv: kv[1])
        moments.append({"kind": "peak_day", "t": t, "usd": round(u, 2), "label": "Día récord"})
    if hour_bins:
        t, u = max(hour_bins.items(), key=lambda kv: kv[1])
        moments.append({"kind": "peak_hour", "t": t, "usd": round(u, 2), "label": "Hora más intensa"})
    # Per session, not per piece: a session split in segments is one moment, named by its
    # costliest piece.
    whole = {}
    for s in sessions.values():
        w = whole.get(s["id"])
        if w is None:
            w = whole[s["id"]] = dict(s, usd=0.0, _best=-1.0)
        w["usd"] += s["usd"]
        w["first"] = min(w["first"], s["first"])
        if s["usd"] > w["_best"]:
            w.update(_best=s["usd"], ticket=s["ticket"], title=s["title"], project=s.get("project"))
    top_sessions = sorted(whole.values(), key=lambda s: -s["usd"])[:3]
    # The page words each moment from its kind and rank (web/i18n.js); label is kept for
    # older clients.
    for i, s in enumerate(top_sessions):
        moments.append({"kind": "session", "rank": i, "t": s["first"], "usd": round(s["usd"], 2),
                        "label": ("Sesión más cara" if i == 0 else "Sesión top %d" % (i + 1)),
                        "title": s["ticket"] or s["title"], "project": s.get("project")})

    # Accounts: calibrate on the whole history, apply to the range.
    if callable(blocks):
        blocks = blocks()
    all_blocks, first_ts = blocks if blocks is not None else all_blocks_of(con, lock)
    plan_total = cfg["plan_main"] + sum(cfg["plan_others"])
    pro_block, pro_week = calibrate(all_blocks, plan_total)
    shares = block_shares(all_blocks, all_rows)
    cov = coverage(all_blocks, cfg["plan_main"], cfg["plan_others"], pro_block, pro_week, shares)
    # Real elapsed span (at least an hour), so per-month figures are not understated.
    span_days = max(1 / 24.0, ((min(t1, int(time.time() * 1000)) - max(t0, first_ts or t0)) / 86400000.0)) \
        if all_rows else 0
    paid = (cfg["price_main"] + cfg["price_others"]) * span_days / 30.4375
    accounts = {
        "plan_main": cfg["plan_main"], "plan_others": cfg["plan_others"],
        "pro_block_usd": round(pro_block, 4), "pro_week_usd": round(pro_week, 4),
        # Whole history, so the client can replay weekly balances: [start, usd, usd in range].
        "blocks": [[s, round(u, 4), round(x, 4)] for (s, u), x in zip(all_blocks, shares)],
        "main_usd": round(cov["main_usd"], 4), "total_usd": round(cov["total_usd"], 4),
        "share_main": round(cov["share_main"], 4), "short_blocks": cov["short_blocks"],
        "block_count": cov["blocks"], "weeks": cov["weeks"],
        "week_main_usd": round(cov["week_main_usd"], 4),
        "week_rest_usd": round(cov["week_rest_usd"], 4),
        "paid_usd": round(paid, 2), "price_main": cfg["price_main"],
        "price_others": cfg["price_others"], "span_days": round(span_days, 2),
    }

    with lock:
        all_t = con.execute("SELECT COALESCE(SUM(inp+out+cread+cw5+cw1h),0), COALESCE(SUM(usd),0), "
                            "COUNT(*), MIN(ts), MAX(ts) FROM calls").fetchone()
    for s in sessions.values():
        s.pop("cwds", None)
    totals["usd"] = round(totals["usd"], 4)
    totals["sessions"] = len({s["id"] for s in sessions.values()})
    return {
        "range": {"from": t0, "to": t1, "unit": unit},
        "totals": totals,
        "alltime": {"tokens": all_t[0], "usd": round(all_t[1], 4), "calls": all_t[2],
                    "since": all_t[3], "last": all_t[4]},
        "live": live_stats(con, lock, hide=hide),
        "projects": plist,
        # Full project name -> the bubble it shows in for this range (client grouping), so
        # a live call of a grouped project lands in its client's bubble.
        "fold": fold,
        "timeline": timeline,
        # Every day of the range, whatever the river's unit (the day strip under it).
        "days": [dict(d, usd=round(d["usd"], 4)) for _k, d in sorted(days.items())],
        "moments": moments,
        "models": sorted(models.values(), key=lambda m: -m["usd"]),
        "accounts": accounts,
        "by_origin": {k: dict(v, usd=round(v["usd"], 4)) for k, v in by_origin.items()},
        "hidden": sorted(hide),
    }


def live_stats(con, lock, now_ms=None, hide=()):
    """Rate over the last 15 minutes and the last call: always fresh, never cached.
    `hide`: origins left out, as in the summary (audit of 86)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    since = now_ms - 15 * 60000
    with lock:
        if not hide:
            r15 = con.execute("SELECT COALESCE(SUM(inp+out+cread+cw5+cw1h),0), COALESCE(SUM(usd),0), "
                              "COUNT(*) FROM calls WHERE ts >= ?", (since,)).fetchone()
            last = con.execute("SELECT MAX(ts) FROM calls").fetchone()[0]
        else:
            groups = con.execute("SELECT model, account, SUM(inp+out+cread+cw5+cw1h), SUM(usd), COUNT(*), MAX(ts) "
                                 "FROM calls WHERE ts >= ? GROUP BY model, account", (since,)).fetchall()
            # The newest visible call: walked down the ts index, a few rows in practice
            # (audit of 90: a GROUP BY over the whole table ran on every poll).
            ids = account_map()
            last = None
            for model, account, ts in con.execute("SELECT model, account, ts FROM calls "
                                                  "ORDER BY ts DESC LIMIT 20000"):  # bounded when all is hidden
                if call_origin(model, account, ids) not in hide:
                    last = ts
                    break
    if not hide:
        return {"tokens_15m": r15[0], "usd_15m": round(r15[1], 4), "calls_15m": r15[2], "last": last}
    keep = [g for g in groups if call_origin(g[0], g[1], ids) not in hide]
    return {"tokens_15m": sum(g[2] or 0 for g in keep), "usd_15m": round(sum(g[3] or 0 for g in keep), 4),
            "calls_15m": sum(g[4] for g in keep), "last": last}


NAMED_RANGES = ("todo", "hoy", "24h", "7d", "30d", "90d")


def range_bounds(name, now_ms):
    """Bounds of a standard range, as the page computes them (local midnight for "hoy")."""
    end = now_ms + 60000
    if name == "hoy":
        d = dt.datetime.fromtimestamp(now_ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(d.timestamp() * 1000), end
    days = {"24h": 1, "7d": 7, "30d": 30, "90d": 90}.get(name)
    if days:
        return now_ms - days * 86400000, end
    return 0, end


def _clamp(v, lo, hi):
    if math.isnan(v):
        return lo
    return max(lo, min(hi, v))


def pulse(con, lock, minutes, now_ms=None):
    """Tokens and cost per minute over the last `minutes`, oldest first, zeros included."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    end = now_ms - (now_ms % 60000) + 60000
    start = end - minutes * 60000
    with lock:
        got = dict(((m, (tok, usd)) for m, tok, usd in con.execute(
            "SELECT (ts / 60000) * 60000 AS m, SUM(inp+out+cread+cw5+cw1h), SUM(usd) "
            "FROM calls WHERE ts >= ? AND ts < ? GROUP BY m", (start, end))))
    out = []
    for m in range(start, end, 60000):
        tok, usd = got.get(m, (0, 0.0))
        out.append([m, tok, round(usd, 5)])
    return {"minutes": out, "now": now_ms}


# ---------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------

MIME = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
        ".png": "image/png", ".json": "application/json", ".ico": "image/x-icon",
        ".webmanifest": "application/manifest+json"}
STATIC_FILES = {"index.html", "app.css", "app.js", "theme.js", "i18n.js", "format.js", "sound.js", "favicon.svg",
                "favicon-32.png", "apple-touch-icon.png", "manifest.webmanifest"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'self'")


class App:
    # Cursor's state.vscdb is several GB; one pass json_extracts every composerData row
    # (tens of MB of JSON), so it runs only when the database (or its WAL, or a CLI chat)
    # changed, and at most every CURSOR_SCAN_S.
    CURSOR_SCAN_S = 300
    CURSOR_RETRIES = 3     # failed reads of the same files retried at most this many times
    CURSOR_API_S = 600
    AGENTS_SCAN_BUDGET_S = 5
    # The standard ranges are kept warm only while a page asked for data recently;
    # otherwise a request computes its range on demand (and wakes the warm-up).
    VIEWER_WINDOW_S = 300

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.con = connect(cfg["db"])
        self.ingestor = Ingestor(cfg, self.con, self.lock)
        self.fx = Fx(cfg, self.con, self.lock)
        self.home = HomeAccounts(cfg)
        self.control = AccountControl(cfg)
        self.logins = Logins(cfg)
        self.cursor = cursor_source.CursorIngestor(self.con, self.lock, project_of)
        ALIAS_LISTENERS.append(self._aliases_changed)
        self.cursor_api = cursor_source.CursorApi(self.con, self.lock)
        # Other coding agents (L-182): read-only readers of their local files.
        # Aider's history files are looked for under the code folders (never in a test or
        # demo home, LATIDOS_AGENTS_HOME, which must not read the real tree).
        roots = [CODE_DIR, PROJECTS_DIR] + WORKSPACE_DIRS if agents.agents_home() == HOME else []
        self.agents = agents.AgentHub(self.con, self.lock, roots=roots)
        self.agents_cache = {}
        self._cursor_at = 0
        self._cursor_sig = None
        self._cursor_fails = 0
        self._cursor_fail_sig = None
        self._cursor_api_at = 0
        self.cache = {}
        self._blocks = None
        self.last_request = 0.0
        self.wake = threading.Event()

    def viewer_active(self, now=None):
        now = time.time() if now is None else now
        return now - self.last_request < self.VIEWER_WINDOW_S

    def touch(self, now=None):
        """A page asked for data: keep the ranges warm; wake the loop if it was idle."""
        now = time.time() if now is None else now
        idle = not self.viewer_active(now)
        self.last_request = now
        if idle:
            self.wake.set()

    def cursor_due(self, now=None):
        """Scan Cursor only when its files changed, and at most every CURSOR_SCAN_S."""
        now = time.time() if now is None else now
        if now - self._cursor_at < self.CURSOR_SCAN_S:
            return False
        sig = self.cursor.signature()
        self._cursor_at = now
        if sig == self._cursor_sig:
            return False
        self._cursor_sig = sig
        return True

    def tick(self, now=None):
        """One background pass after each log scan."""
        now = time.time() if now is None else now
        self.fx.refresh()
        if self.cursor_due(now):
            scanned = self._cursor_sig
            self.cursor.scan()
            if self.cursor.progress.get("error"):
                # A failed read (SQLITE_BUSY during a checkpoint) must not mark this
                # version of Cursor's files as seen: retry on the next due pass, but only
                # CURSOR_RETRIES times per version, so a permanent error doesn't re-read
                # every 300 s. A new version of the files gets its own retries.
                if scanned != self._cursor_fail_sig:
                    self._cursor_fail_sig = scanned
                    self._cursor_fails = 0
                self._cursor_fails += 1
                if self._cursor_fails <= self.CURSOR_RETRIES:
                    self._cursor_sig = None
            else:
                self._cursor_fails = 0
                self._cursor_fail_sig = None
        if now - self._cursor_api_at >= self.CURSOR_API_S:
            self._cursor_api_at = now
            self.cursor_api.scan()
        if self.agents.due(now):
            try:
                # A budget per pass: a large first scan spreads over several passes instead
                # of holding up the log scans (audit of 101).
                self.agents.scan(now, budget_s=self.AGENTS_SCAN_BUDGET_S)
            except Exception as exc:  # noqa: BLE001 - never stops the loop
                sys.stderr.write("latidos: agents scan failed (%s)\n" % exc.__class__.__name__)
        if not self.viewer_active(now):
            return False
        now_ms = int(now * 1000)
        for name in NAMED_RANGES:
            t0, t1 = range_bounds(name, now_ms)
            self.summary(t0, t1, None, background=True, name=name)
        return True

    STALE_OK_MS = 20000
    MAX_AGE_MS = 120000

    NAMED_STALE_MS = 60000

    def summary(self, t0, t1, unit, background=False, name=None, hide=()):
        """Cached per range (and per hidden-account set, L-159). While new calls keep
        arriving (the version moves every scan), a result up to 20 s old is served instead
        of recomputing on every request."""
        now = time.time() * 1000
        hide = tuple(sorted(set(hide or ())))
        # Ranges that end "now" share one key per start (the warm-up serves them); the
        # effective bin unit is part of the key, since it depends on the span.
        eff_unit = unit if unit in ("hour", "day", "week") else (pick_unit(t1 - t0) if t0 > 0 else "auto")
        key = ("r", name, eff_unit, t0 // 60000 if name == "hoy" else 0) if name else (t0 // 60000, "now" if t1 >= now else t1 // 60000, eff_unit)
        key = key + (hide,) if hide else key
        version = self.ingestor.version
        hit = self.cache.get(key)
        if hit and now - hit[1] < self.MAX_AGE_MS:
            # Requests never wait for a recompute the background loop will do anyway (it
            # refreshes the default view every scan); it recomputes once the entry is
            # older than STALE_OK_MS and the data moved.
            stale_ok = self.NAMED_STALE_MS if name and name != "todo" else self.STALE_OK_MS
            # A view with hidden accounts has no background refresh: it is recomputed on
            # request once the data moved and the entry is older than stale_ok (audit of 86).
            if (not background and not hide) or hit[0] == version or now - hit[1] < stale_ok:
                return self._fresh(hit[2], hide)
        seen = {}
        gen = NAMES_GEN[0]

        def mark():
            # Under the lock that reads the rows (ingest commits under it too): the live
            # sequence, the data version and the 5-hour blocks all describe those rows.
            seen["seq"] = self.ingestor.seq
            seen["version"] = v = self.ingestor.version
            if self._blocks is None or self._blocks[0] != v:
                self._blocks = (v, all_blocks_of(self.con, self.lock))
            seen["blocks"] = self._blocks[1]
        data = summarize(self.con, self.lock, self.cfg, t0, t1, unit,
                         blocks=lambda: seen["blocks"], on_locked=mark, hide=hide)
        data["seq_at"] = seen["seq"]
        data["version"] = seen["version"]
        data["boot"] = BOOT
        data["fx"] = self.fx.get()
        data["ingest"] = dict(self.ingestor.progress)
        data["seq"] = self.ingestor.seq
        if len(self.cache) > 32:
            # Views with hidden accounts go first, so eye clicks do not flush the default
            # views the background keeps warm (audit of 90).
            hidden = [k for k in self.cache if isinstance(k[-1], tuple)]
            for k in hidden or list(self.cache):
                del self.cache[k]
        if gen == NAMES_GEN[0]:  # a rename or move during the computation: not cached
            self.cache[key] = (seen["version"], now, data)
        return data

    AGENTS_MAX_AGE_MS = 120000

    def agents_summary(self, t0, t1, unit, min_version=None):
        """Cached by (start minute, end minute, unit), at most 2 min old (audit of 101). An
        entry is served only when it was computed at a data version at least `min_version`,
        the version of the summary it goes out with (the current version when not given):
        the page remembers that version and reloads only when it moves, so an older agents
        block would stay on screen (audit of 102)."""
        now = time.time() * 1000
        version = self.ingestor.version
        need = version if min_version is None else min_version
        key = (t0 // 60000, t1 // 60000, unit, NAMES_GEN[0])
        hit = self.agents_cache.get(key)
        if hit and now - hit[0] < self.AGENTS_MAX_AGE_MS and hit[2] >= need:
            return hit[1]
        data = agents.summarize_agents(
            self.con, self.lock, t0, t1, unit, bin_start, project_of, rename=alias,
            claude=lambda m, i, o, r, w: call_cost(m, i, o, r, w, 0),
            readers=self.agents.readers, progress=self.agents.progress)
        if len(self.agents_cache) > 32:
            self.agents_cache.clear()
        self.agents_cache[key] = (now, data, version)
        return data

    def _aliases_changed(self):
        """Renamed projects must show at once: drop cached summaries and move the data
        version so open pages reload."""
        self.cache.clear()
        self.agents_cache.clear()
        with self.lock:
            self.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
            self.con.commit()

    def _fresh(self, data, hide=()):
        """A cached summary with the parts that must not go stale replaced."""
        out = dict(data)
        out["live"] = live_stats(self.con, self.lock, hide=hide)
        out["fx"] = self.fx.get()
        out["ingest"] = dict(self.ingestor.progress)
        out["seq"] = self.ingestor.seq
        return out


def finite(obj):
    """obj with every non-finite float (inf, nan) replaced by None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    return obj


def json_bytes(obj):
    """Strict JSON: Infinity or NaN would make the page's JSON.parse fail and blank it, so
    one bad row is sent as null instead (audit of 104)."""
    try:
        return json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
    except ValueError:
        return json.dumps(finite(obj), separators=(",", ":"), allow_nan=False).encode()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "latidos/1"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", cache="no-store"):
            if isinstance(body, (dict, list)):
                body = json_bytes(body)
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_ok(self):
            return host_allowed(self.headers.get("Host"), self.client_address[0], app.cfg)

        def do_HEAD(self):
            self.do_GET()

        def _refuse(self):
            self._send(405, {"error": "method not allowed"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _refuse

        def _read_body(self, n):
            """n bytes within POST_BODY_TIMEOUT_S in all (a slow drip times out too)."""
            deadline = time.time() + POST_BODY_TIMEOUT_S
            buf = b""
            while len(buf) < n:
                left = deadline - time.time()
                if left <= 0:
                    raise socket.timeout("body timed out")
                self.connection.settimeout(left)
                chunk = self.rfile.read1(n - len(buf)) if hasattr(self.rfile, "read1") else self.rfile.read(n - len(buf))
                if not chunk:
                    raise ValueError("body ended early")
                buf += chunk
            return buf

        def do_POST(self):
            """The only writes: rename a cell, move a task, and (when `account_control` is
            configured) switch or reorder the Claude accounts. JSON with the X-Latidos header,
            which a page of another site cannot send without a preflight (OPTIONS is
            refused), on top of the Host guard."""
            path = urlparse(self.path).path
            if path not in ("/api/rename", "/api/assign", "/api/claude-control/activate",
                            "/api/claude-control/order", "/api/logins/renew"):
                return self._refuse()
            if not self._host_ok():
                return self._send(421, "misdirected request\n", "text/plain; charset=utf-8")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if self.headers.get("X-Latidos") != "1" or ctype != "application/json":
                return self._send(403, {"error": "forbidden"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > 4096:
                return self._send(413 if n > 4096 else 400, {"error": "bad body size"})
            try:
                body = json.loads(self._read_body(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be an object")
                if path == "/api/rename":
                    name = rename_project(body.get("from"), body.get("to"))
                    return self._send(200, {"ok": True, "name": name})
                if path == "/api/claude-control/activate":
                    return self._send(202, app.control.activate(body.get("tag")))
                if path == "/api/claude-control/order":
                    return self._send(200, app.control.order(body.get("order")))
                if path == "/api/logins/renew":
                    return self._send(202, app.logins.renew(body.get("tag")))
                return self._send(200, {"ok": True, "project": assign_task(
                    body.get("key"), body.get("project"), existing=body.get("existing") is True)})
            except EditConflict as exc:
                return self._send(409, {"error": str(exc), "file": exc.file})
            except ControlBusy as exc:
                return self._send(409, {"error": str(exc), "error_code": "busy", "tag": exc.tag})
            except PermissionError as exc:
                return self._send(403, {"error": str(exc), "error_code": "no_control"})
            except subprocess.SubprocessError as exc:
                return self._send(504, {"error": "control: %s" % exc.__class__.__name__})
            except socket.timeout:
                return self._send(408, {"error": "body timed out"})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except OSError as exc:
                return self._send(500, {"error": "could not save: %s" % _why(exc)})

        def do_GET(self):
            _t0 = time.time()
            try:
                return self._get()
            finally:
                if time.time() - _t0 > 1:
                    sys.stderr.write("latidos: slow %s %.2fs\n" % (self.path[:60], time.time() - _t0))

        def _get(self):
            if not self._host_ok():
                return self._send(421, "misdirected request\n", "text/plain; charset=utf-8")
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            path = url.path
            try:
                if path == "/api/health":
                    p = app.ingestor.progress
                    return self._send(200, {"ok": True, "ingest": p, "seq": app.ingestor.seq})
                if path in ("/api/summary", "/api/live", "/api/pulse"):
                    app.touch()  # a page is open (health checks do not count)
                if path == "/api/summary":
                    now = int(time.time() * 1000)
                    hide = parse_hide(q.get("hide"))
                    if q.get("range") in NAMED_RANGES:
                        # A standard range by name: one cache entry the background keeps warm.
                        t0, t1 = range_bounds(q["range"], now)
                        data = app.summary(t0, t1, None, name=q["range"], hide=hide)
                    else:
                        t0 = int(float(q.get("from", 0)))
                        t1 = int(float(q.get("to", now + 60000)))
                        if t1 <= t0:
                            return self._send(400, {"error": "to must be after from"})
                        data = app.summary(t0, t1, q.get("unit"), hide=hide)
                    cz = cursor_source.summarize_cursor(app.con, app.lock, t0, t1, rename=alias)
                    if q.get("cursor") == "1":
                        # Cursor activity: small and fresh, never cached.
                        data = dict(data, cursor=dict(
                            cz, status=dict(app.cursor.progress, api=dict(app.cursor_api.progress))))
                    else:
                        # Hidden Cursor still counts in every card's share (L-159).
                        data = dict(data, cursor_totals={"usd": cz["totals"]["usd"], "tokens": cz["totals"]["tokens"]})
                    # Other agents: cached per range at no older version than this summary's
                    # (App.agents_summary); the page shows or hides each.
                    data = dict(data, agents=app.agents_summary(t0, t1, data["range"]["unit"],
                                                                min_version=data.get("version")))
                    return self._send(200, data)
                if path == "/api/accounts":
                    return self._send(200, dict(app.home.get(),
                                                use=account_use(app.con, app.lock),
                                                recent_ms=ACCOUNT_RECENT_MS))
                if path == "/api/logins":
                    return self._send(200, app.logins.status())
                if path == "/api/claude-control":
                    return self._send(200, app.control.state(fresh=q.get("fresh") == "1"))
                if path == "/api/pulse":
                    minutes = int(_clamp(float(q.get("minutes", 90)), 5, 24 * 60))
                    return self._send(200, pulse(app.con, app.lock, minutes))
                if path == "/api/live":
                    since = int(float(q.get("since", 0)))
                    hide = parse_hide(q.get("hide"))
                    ev = [e for s, e in app.ingestor.recent if s > since]
                    return self._send(200, {"seq": app.ingestor.seq, "boot": BOOT, "events": ev,
                                            "version": app.ingestor.version,
                                            "live": live_stats(app.con, app.lock, hide=hide),
                                            "ingest": app.ingestor.progress})
                return self._static(path)
            except (ValueError, KeyError, OverflowError) as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:
                return self._send(500, {"error": str(exc)})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            name = path.lstrip("/")
            if name not in STATIC_FILES:
                return self._send(404, {"error": "not found"})
            full = os.path.join(WEB_DIR, name)
            if not os.path.isfile(full):
                return self._send(404, {"error": "not found"})
            ext = os.path.splitext(full)[1]
            with open(full, "rb") as fh:
                body = fh.read()
            cache = "no-cache" if ext in (".html", ".js", ".css") else "max-age=86400"
            return self._send(200, body, MIME.get(ext, "application/octet-stream"), cache)

    return Handler


def host_allowed(host, client, cfg):
    """DNS-rebinding guard. The loopback names count only for a client on this machine,
    so a remote client of an opted-in non-loopback bind cannot pass by forging
    `Host: 127.0.0.1`; such a client must name a host in LATIDOS_ALLOWED_HOSTS (or the
    bound address itself, when it is a specific one)."""
    host = (host or "").lower()
    port = cfg["port"]
    bound = (cfg.get("host") or "").lower()
    local = ["127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port]
    if _is_loopback(bound):
        local.append("%s:%d" % (_host_name(bound), port))  # its own URL, e.g. [::1]
    if _is_loopback(client) and host in local:
        return True
    if _is_loopback(bound):
        return False
    names = [h.strip().lower() for h in env("ALLOWED_HOSTS", "").split(",") if h.strip()]
    if bound not in ("0.0.0.0", "::", ""):
        names.append(_host_name(bound))
    return bool(host) and any(host in (n, "%s:%d" % (n, port)) for n in names)


class Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


class Server6(Server):
    address_family = socket.AF_INET6


def make_server(host, port, handler):
    """IPv4 or IPv6 server by the address family of the host (an IPv6 literal binds
    AF_INET6; names and IPv4 addresses bind AF_INET)."""
    try:
        v6 = ipaddress.ip_address(host).version == 6
    except ValueError:
        v6 = False
    return (Server6 if v6 else Server)((host, port), handler)


def serve_on(host, port, handler):
    """The server on `host`, or on 127.0.0.1 with a warning when `host` cannot be bound
    (an address this machine does not have, such as 127.0.0.2 on macOS, or a name that
    does not resolve): a traceback would be a restart loop under launchd. A port in use
    is still an error. Returns (server, host bound)."""
    try:
        return make_server(host, port, handler), host
    except OSError as exc:
        if host == "127.0.0.1" or exc.errno == errno.EADDRINUSE:
            raise
        _warn("cannot bind LATIDOS_HOST=%s (%s), binding 127.0.0.1" % (host, exc))
        return make_server("127.0.0.1", port, handler), "127.0.0.1"


def probe_host(host):
    """The address a client on this machine reaches the server at: the bound address,
    127.0.0.1 (or ::1) for an any-address bind, 127.0.0.1 when the host cannot be bound
    (serve falls back there)."""
    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host == "::":
        return "::1"
    try:
        make_server(host, 0, BaseHTTPRequestHandler).server_close()
    except OSError:
        return "127.0.0.1"
    return host


LABEL = SETTINGS["label"]
AGENTS_DIR = os.path.join(HOME, "Library", "LaunchAgents")


def python_bin():
    for cand in ("/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(cand):
            return cand
    return sys.executable


def plist_bytes():
    log = os.path.join(HOME, "Library", "Logs", "latidos.log")
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python_bin(), os.path.abspath(__file__), "serve"],
        "RunAtLoad": True,
        "KeepAlive": True,
        # Adaptive, not Background: macOS throttles Background jobs (disk reads included),
        # which made other panels take seconds per answer.
        "ProcessType": "Adaptive",
        "Nice": 5,
        "ExitTimeOut": 15,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": HOME},
    })


def legacy_agent(label, agents_dir=None):
    """The LaunchAgent of the old name (Latido) that `install` replaces: the label without
    its final "s" (local.latidos -> local.latido), only when its plist exists and runs a
    latido.py. None otherwise."""
    if not label.endswith("latidos"):
        return None
    old_label = label[:-1]
    path = os.path.join(agents_dir or AGENTS_DIR, old_label + ".plist")
    try:
        with open(path, "rb") as fh:
            args = plistlib.load(fh).get("ProgramArguments") or []
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not any(isinstance(a, str) and os.path.basename(a) == "latido.py" for a in args):
        return None
    return old_label, path


def _bootout(domain, label):
    subprocess.run(["launchctl", "bootout", "%s/%s" % (domain, label)], capture_output=True)
    for _ in range(50):  # wait for bootout to finish before bootstrapping again
        if subprocess.run(["launchctl", "print", "%s/%s" % (domain, label)],
                          capture_output=True).returncode != 0:
            break
        time.sleep(0.2)


def install():
    target = os.path.join(AGENTS_DIR, LABEL + ".plist")
    domain = "gui/%d" % os.getuid()
    legacy = legacy_agent(LABEL)
    if legacy:
        # Same port and same database: the old agent stops before the new one starts, and
        # the new one moves the old folders over when it starts (migrate_legacy_dirs).
        _bootout(domain, legacy[0])
    _bootout(domain, LABEL)
    with open(target, "wb") as fh:
        fh.write(plist_bytes())
    res = subprocess.run(["launchctl", "bootstrap", domain, target], capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write((res.stderr.strip() or "launchctl bootstrap failed") + "\n")
        return 1
    if legacy:
        os.unlink(legacy[1])
        print("replaced the old agent %s (%s removed)" % legacy)
    cfg = config()
    print("installed %s; http://%s:%d" % (target, _host_name(probe_host(cfg["host"])), cfg["port"]))
    return 0


def uninstall():
    target = os.path.join(AGENTS_DIR, LABEL + ".plist")
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True)
    if os.path.exists(target):
        os.unlink(target)
    print("uninstalled")
    return 0


def status():
    cfg = config()
    host = probe_host(cfg["host"])
    url = "http://%s:%d/api/health" % (_host_name(host), cfg["port"])
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(resp.read().decode())
        return 0
    except Exception as exc:
        print("not answering on %s: %s" % (url, exc))
        return 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "install":
        return install()
    if argv and argv[0] == "uninstall":
        return uninstall()
    if argv and argv[0] == "status":
        return status()
    migrate_legacy_dirs()
    cfg = config()
    if argv and argv[0] == "ingest":
        app = App(cfg)
        t = time.time()
        n = app.ingestor.scan()
        print("ingested %d records in %.1fs into %s" % (n, time.time() - t, cfg["db"]))
        t = time.time()
        n = app.agents.scan()
        found = ", ".join("%s %d files" % (k, v["files"]) for k, v in app.agents.progress.items() if v["available"])
        print("agents: %d calls in %.1fs (%s)" % (n, time.time() - t, found or "none found"))
        return 0
    if argv and argv[0] == "summary":
        app = App(cfg)
        data = app.summary(0, int(time.time() * 1000) + 60000, None)
        data.pop("timeline", None)
        data["accounts"].pop("blocks", None)
        print(json.dumps(data, indent=1, ensure_ascii=False)[:6000])
        return 0
    app = App(cfg)
    stop = threading.Event()
    # The exchange rate, Cursor and (while a page is open) every standard range, so a
    # filter click is answered from the cache. With no viewer nothing is precomputed.
    th = threading.Thread(target=app.ingestor.loop, args=(stop, app.tick),
                          kwargs={"active": app.viewer_active, "wake": app.wake}, daemon=True)
    th.start()
    srv, cfg["host"] = serve_on(cfg["host"], cfg["port"], make_handler(app))
    sys.stderr.write("latidos: http://%s:%d\n" % (_host_name(cfg["host"]), cfg["port"]))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
