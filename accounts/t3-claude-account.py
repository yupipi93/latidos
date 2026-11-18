#!/usr/bin/env python3
"""t3-claude-account.py - switch the Claude account T3 Code runs on, keeping every thread.

Why this exists: T3 Code spawns `claude` with the desktop app's own environment, which
never carries CLAUDE_CONFIG_DIR, so it always ran on the default login. When that quota
runs out, the threads must move to another account without losing them.

How: T3 has a per-instance setting, homePath ("CLAUDE_CONFIG_DIR path" in Settings ->
Providers -> Claude): providerInstances.claudeAgent.config.homePath since V2 (0.1.0),
providers.claudeAgent.homePath before. Setting it on the EXISTING instance keeps the
instance id (claudeAgent), so T3 does not treat it as an instance switch (that one is
refused as "provider resume state is incompatible"). The next turn of a thread runs
`claude --resume <session>` with CLAUDE_CONFIG_DIR=~/.claude.x, and the transcript is
found because every account symlinks projects/ into the shared pool (see
accounts/claude-shared-pool.py).

    default  homePath removed           CLAUDE_CONFIG_DIR unset, Keychain "Claude Code-credentials"
    other    homePath = ~/.claude.x     Keychain "Claude Code-credentials-<sha256(dir)[:8]>"

The default account must REMOVE the key, never set it to ~/.claude: an explicit
CLAUDE_CONFIG_DIR changes the Keychain item name and its login is gone. The accounts come
from the accounts config (accounts_config.py).

The server watches ~/.t3/userdata/settings.json and reloads it live. A claude process
that is already running keeps the account it was spawned with until its session ends;
--restart quits and reopens the app so every thread respawns on the new account.

    accounts/t3-claude-account.py              # status: configured account + live sessions
    accounts/t3-claude-account.py home           # move T3 to the account tagged "home"
    accounts/t3-claude-account.py home --restart # also restart the app (interrupts running turns)
    accounts/t3-claude-account.py home --quiet   # what a shell switch command runs

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
T3_HOME = Path(os.environ.get("T3CODE_HOME", HOME / ".t3"))
SETTINGS = T3_HOME / "userdata" / "settings.json"
RUNTIME = T3_HOME / "userdata" / "server-runtime.json"
# V2 (0.1.0) renamed the app from "T3 Code (Alpha)"; keep the old name as a fallback.
APP_NAME = next((n for n in ("T3 Code (Beta)", "T3 Code (Alpha)") if Path(f"/Applications/{n}.app").exists()),
                "T3 Code (Beta)")
POOL_TOOL = Path(__file__).resolve().parent / "claude-shared-pool.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)
CFG = accounts_config.CONFIG
ACCOUNTS = {a.tag: {"home": a.home_form, "dir": a.dir, "email": a.email} for a in CFG.accounts}


def tag_for_dir(cfg: str) -> str:
    """Account tag of a CLAUDE_CONFIG_DIR value: empty is the default account, an unknown dir
    is returned as is."""
    if not cfg:
        return CFG.default_tag
    for tag, acc in ACCOUNTS.items():
        if acc["home"] and Path(cfg).resolve() == acc["dir"].resolve():
            return tag
    return cfg


def load_settings() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text())


def write_settings(data: dict) -> None:
    # Same-directory temp + rename: the server's watcher never sees a half-written file.
    tmp = SETTINGS.with_name(f".{SETTINGS.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, SETTINGS)


def claude_config(settings: dict) -> dict:
    """The claudeAgent instance's config: V2 layout when present or new, else the V1 one."""
    if "providers" in settings and "providerInstances" not in settings:
        return settings["providers"].setdefault("claudeAgent", {})
    instance = settings.setdefault("providerInstances", {}).setdefault(
        "claudeAgent", {"driver": "claudeAgent", "config": {}})
    return instance.setdefault("config", {})


def configured_tag(settings: dict) -> str:
    home = claude_config(settings).get("homePath", "").strip()
    if not home:
        return CFG.default_tag
    for tag, acc in ACCOUNTS.items():
        if acc["home"] and Path(os.path.expanduser(home)).resolve() == acc["dir"].resolve():
            return tag
    return f"custom ({home})"


def server_pid() -> int | None:
    try:
        pid = int(json.loads(RUNTIME.read_text())["pid"])
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError, KeyError):
        return None


def process_env(pid: int) -> dict:
    out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)],
                         capture_output=True, text=True).stdout
    env = {}
    for token in out.split():
        if "=" in token and token.split("=", 1)[0].isupper():
            key, value = token.split("=", 1)
            env[key] = value
    return env


def live_sessions(pid: int) -> list[tuple[int, str, str]]:
    """claude children of the T3 server: (pid, account tag, resumed session id)."""
    out = subprocess.run(["pgrep", "-P", str(pid), "-x", "claude"], capture_output=True, text=True).stdout
    sessions = []
    for child in (int(p) for p in out.split()):
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(child)], capture_output=True, text=True).stdout
        resume = next((a.split("=", 1)[1] for a in cmd.split() if a.startswith("--resume=")), "new")
        cfg = process_env(child).get("CLAUDE_CONFIG_DIR", "")
        tag = tag_for_dir(cfg)
        sessions.append((child, tag, resume))
    return sessions


def auth_email(tag: str) -> str | None:
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if ACCOUNTS[tag]["home"]:
        env["CLAUDE_CONFIG_DIR"] = str(ACCOUNTS[tag]["dir"])
    try:
        res = subprocess.run(["claude", "auth", "status"], env=env, capture_output=True, text=True, timeout=30)
        data = json.loads(res.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    return data.get("email") if data.get("loggedIn") else None


def restart_app() -> None:
    subprocess.run(["osascript", "-e", f'tell application "{APP_NAME}" to quit'], capture_output=True)
    for _ in range(30):
        if subprocess.run(["pgrep", "-x", APP_NAME], capture_output=True).returncode != 0:
            break
        time.sleep(1)
    subprocess.run(["open", "-a", APP_NAME], check=True)


def status() -> int:
    settings = load_settings()
    print(f"T3 Claude account (settings): {configured_tag(settings)}")
    pid = server_pid()
    if pid is None:
        print("T3 server: not running")
        return 0
    inherited = process_env(pid).get("CLAUDE_CONFIG_DIR")
    print(f"T3 server: pid {pid}" + (f", inherits CLAUDE_CONFIG_DIR={inherited}" if inherited else ""))
    sessions = live_sessions(pid)
    if not sessions:
        print("Live claude sessions: none")
    for child, tag, resume in sessions:
        print(f"  pid {child}  account {tag}  session {resume}")
    return 0


def switch(tag: str, restart: bool, quiet: bool) -> int:
    acc = ACCOUNTS[tag]
    if not SETTINGS.parent.is_dir():
        if not quiet:
            print(f"ℹ️ T3 Code is not installed ({SETTINGS.parent} missing), nothing to switch")
        return 0
    if quiet and configured_tag(load_settings()) == tag:
        return 0

    email = auth_email(tag)
    if email != acc["email"]:
        login = f"CLAUDE_CONFIG_DIR={acc['home']} claude /login" if acc["home"] else "(unset CLAUDE_CONFIG_DIR; claude /login)"
        print(f"❌ {tag} is not logged in as {acc['email']} (got {email}). Log in first: {login}")
        return 1

    # Threads resume only if the transcripts are in the shared pool.
    if POOL_TOOL.exists() and subprocess.run([sys.executable, str(POOL_TOOL), "--fix", "--quiet"]).returncode != 0:
        print("❌ shared pool is not healthy, run accounts/claude-shared-pool.py to see why")
        return 1

    settings = load_settings()
    claude = claude_config(settings)
    if acc["home"]:
        claude["homePath"] = acc["home"]
    else:
        claude.pop("homePath", None)
    write_settings(settings)

    pid = server_pid()
    inherited = process_env(pid).get("CLAUDE_CONFIG_DIR") if pid else None
    if acc["home"] is None and inherited:
        print(f"⚠️ the T3 server inherits CLAUDE_CONFIG_DIR={inherited}, so {tag} would still run there; "
              "reopen the app from the Dock")

    if restart and pid:
        restart_app()
        print(f"✅ T3 Code on {tag} ({acc['email']}), app restarted: every thread resumes on it")
        return 0

    time.sleep(2)
    stale = [s for s in (live_sessions(pid) if pid else []) if s[1] != tag]
    if stale:
        print(f"ℹ️ T3: {len(stale)} session(s) still on the old account until they end:")
        for child, old, resume in stale:
            print(f"  pid {child}  account {old}  session {resume}")
        print("   accounts/t3-claude-account.py --restart moves them now (interrupts any running turn)")
    print(f"✅ T3 Code on {tag} ({acc['email']}): new turns and resumed threads use it")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch the Claude account T3 Code runs on.")
    parser.add_argument("account", nargs="?", choices=sorted(ACCOUNTS), help="omit for status")
    parser.add_argument("--restart", action="store_true", help="quit and reopen T3 Code after switching")
    parser.add_argument("--quiet", action="store_true",
                        help="silent no-op when T3 is missing or already on that account (zsh switcher)")
    args = parser.parse_args()
    return switch(args.account, args.restart, args.quiet) if args.account else status()


if __name__ == "__main__":
    sys.exit(main())
