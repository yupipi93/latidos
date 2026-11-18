#!/usr/bin/env python3
"""Claude.ai logins of this Mac: when each one expires, and a one-click browser renewal.

Each Claude Code account of the accounts config (accounts_config.py) keeps its OAuth
login in its own Keychain item. The login lasts about 30 days (`refreshTokenExpiresAt`, the
`loginExpiresAt` that `claude auth status` reports); after that Claude Code asks for /login.

  status [--json]        every account: login expiry, state, the login's email, the warm-up
  login TAG [TAG...]     renew those logins in their Chrome profile, in parallel
  login --expired        only the expired, missing or wrong ones
  login --soon           expired ones plus those within soon_days
  login --all            every account

Status reads the Keychain item with `security` and keeps two timestamps and the plan: tokens
never leave this process and are never printed. No Claude Code process is started for it.

A login runs `claude auth login --claudeai --email <email>` under the account's config dir
(CLAUDE_CONFIG_DIR unset for the default account) with BROWSER pointing to a small script
that opens the sign-in page in the account's Chrome profile. Claude.ai is already signed in
there, so it takes one click on "Authorize"; the page returns to Claude Code's localhost
callback and Claude Code writes the new login to the Keychain itself. Afterwards the login's
email is checked against the account's: a mismatch (someone authorised with another user)
is reported loudly, exit 2.

Exit codes: 0 fine, 1 a login failed or timed out, 2 a login landed on the wrong user,
3 config error, 4 a login for that account is already running.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))
import accounts_config  # noqa: E402  (sibling module: where the accounts config lives)
# The shared accounts config: tag, label, email, config_dir, keychain, chrome_profile per
# account, and `settings` (soon_days, notify_days, login_timeout_seconds).
CONFIG = Path(os.environ.get("CLAUDE_ACCOUNTS_CONFIG") or accounts_config.config_path())
STATE_DIR = Path(os.environ.get("CLAUDE_ACCOUNTS_STATE") or HOME / ".local/state/claude-accounts")
LOG = Path(os.environ.get("CLAUDE_ACCOUNTS_LOG") or HOME / "Library/Logs/claude-accounts.log")
CLAUDE_BIN = os.environ.get("CLAUDE_ACCOUNTS_CLAUDE_BIN") or str(HOME / ".local/bin/claude")
SECURITY_BIN = os.environ.get("CLAUDE_ACCOUNTS_SECURITY_BIN") or "/usr/bin/security"
DEFAULT_JSON = Path(os.environ.get("CLAUDE_ACCOUNTS_DEFAULT_JSON") or HOME / ".claude.json")
WARMUP_STATE = Path(os.environ.get("CLAUDE_ACCOUNTS_WARMUP_STATE") or HOME / ".local/state/claude-quota-warmup/state.json")
KEYCHAIN_TIMEOUT = 10

BROWSER_SCRIPT = """#!/bin/sh
# Written by claude_accounts.py: open the Claude sign-in page in one Chrome profile.
# The profile comes from the environment, never from a command line.
if [ -n "$CLAUDE_ACCOUNTS_CHROME_PROFILE" ]; then
  exec /usr/bin/open -na "Google Chrome" --args --profile-directory="$CLAUDE_ACCOUNTS_CHROME_PROFILE" "$1"
fi
exec /usr/bin/open "$1"
"""


class ConfigError(Exception):
    pass


@dataclass
class Account:
    tag: str
    label: str
    email: str
    config_dir: str  # absolute, "" = CLAUDE_CONFIG_DIR unset
    keychain: str
    chrome_profile: str = ""


def log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, file=sys.stderr, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_config() -> tuple[dict, list[Account]]:
    try:
        raw = json.loads(CONFIG.read_text())
        if not isinstance(raw, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        raise ConfigError(f"config {CONFIG}: {exc}") from exc
    settings = {"soon_days": 3, "notify_days": 2, "login_timeout_seconds": 300, **(raw.get("settings") or {})}
    accounts, seen = [], set()
    for a in raw.get("accounts") or []:
        if not isinstance(a, dict):
            raise ConfigError("config: an account entry is not an object")
        tag = a.get("tag")
        # Same rule as accounts_config.py (audits of 82 and 83: a string, no trimming).
        if not isinstance(tag, str) or not tag or set(tag) - accounts_config._TAG_OK or tag in seen:
            raise ConfigError(f"config: bad or repeated tag {tag!r}")
        seen.add(tag)
        cdir = str(a.get("config_dir", "")).strip()
        cdir = str(Path(cdir).expanduser()) if cdir else ""
        profile = str(a.get("chrome_profile", "")).strip()
        if profile and not all(c.isalnum() or c in " -_" for c in profile):
            raise ConfigError(f"config: chrome_profile {profile!r} of {tag} has unexpected characters")
        accounts.append(Account(tag=tag, label=str(a.get("label") or tag), email=str(a.get("email", "")).strip(),
                                config_dir=cdir, keychain=str(a.get("keychain", "")).strip(), chrome_profile=profile))
    if not accounts:
        raise ConfigError("config: no accounts")
    return settings, accounts


def read_keychain(service: str) -> dict | None:
    """The login's timestamps and plan, or None when there is no item. Tokens are dropped here."""
    try:
        proc = subprocess.run([SECURITY_BIN, "find-generic-password", "-a", os.environ.get("USER", ""), "-s", service, "-w"],
                              capture_output=True, text=True, timeout=KEYCHAIN_TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise RuntimeError("el Llavero no contestó")
    if proc.returncode == 44:  # item not found
        return None
    if proc.returncode != 0:
        raise RuntimeError(f"Llavero: código {proc.returncode}")
    try:
        oauth = json.loads(proc.stdout).get("claudeAiOauth") or {}
    except (ValueError, AttributeError):
        raise RuntimeError("Llavero: contenido ilegible")
    return {
        "has_refresh": bool(oauth.get("refreshToken")),
        "refresh_expires_ms": oauth.get("refreshTokenExpiresAt") if isinstance(oauth.get("refreshTokenExpiresAt"), int) else None,
        "access_expires_ms": oauth.get("expiresAt") if isinstance(oauth.get("expiresAt"), int) else None,
        "plan": str(oauth.get("subscriptionType") or ""),
    }


def login_email(acct: Account) -> str | None:
    path = Path(acct.config_dir) / ".claude.json" if acct.config_dir else DEFAULT_JSON
    try:
        return (json.loads(path.read_text()).get("oauthAccount") or {}).get("emailAddress")
    except (OSError, ValueError, AttributeError):
        return None


def iso_ms(ms: int | None) -> str | None:
    return dt.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds") if ms else None


def lock_path(acct: Account) -> Path:
    return STATE_DIR / f"login-{acct.tag}.lock"


def login_running(acct: Account) -> bool:
    path = lock_path(acct)
    if not path.exists():
        return False
    with path.open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def warmup_of(tag: str) -> dict | None:
    try:
        rec = (json.loads(WARMUP_STATE.read_text()).get("accounts") or {}).get(tag)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(rec, dict):
        return None
    return {k: rec.get(k) for k in ("last_success_at", "last_attempt_at", "outcome", "error")}


def account_status(acct: Account, settings: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    out = {"tag": acct.tag, "label": acct.label, "email": acct.email, "login_email": None, "state": "unknown",
           "expires_at": None, "days_left": None, "plan": "", "detail": "", "renewing": login_running(acct),
           "warmup": warmup_of(acct.tag)}
    try:
        kc = read_keychain(acct.keychain)
    except RuntimeError as exc:
        out["detail"] = str(exc)
        return out
    if kc is None or not kc["has_refresh"]:
        out["state"], out["detail"] = "missing", "sin login en el Llavero"
        return out
    out["plan"] = kc["plan"]
    out["login_email"] = login_email(acct)
    exp = kc["refresh_expires_ms"]
    if exp:
        out["expires_at"] = iso_ms(exp)
        out["days_left"] = round((exp / 1000 - now) / 86400, 1)
    if out["login_email"] and acct.email and out["login_email"].lower() != acct.email.lower():
        out["state"], out["detail"] = "wrong", f"login de {out['login_email']}"
    elif exp and exp / 1000 <= now:
        out["state"], out["detail"] = "expired", "caducada"
    elif exp and exp / 1000 - now <= float(settings["soon_days"]) * 86400:
        out["state"] = "soon"
    elif exp:
        out["state"] = "ok"
    else:
        out["state"], out["detail"] = "ok", "sin fecha de caducidad en el Llavero"
    return out


def cmd_status(as_json: bool) -> int:
    settings, accounts = load_config()
    with cf.ThreadPoolExecutor(len(accounts)) as pool:
        rows = list(pool.map(lambda a: account_status(a, settings), accounts))
    if as_json:
        print(json.dumps({"checked_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                          "settings": {k: settings[k] for k in ("soon_days", "notify_days")}, "accounts": rows}))
        return 0
    for r in rows:
        when = r["expires_at"][:16].replace("T", " ") if r["expires_at"] else "-"
        days = f"{r['days_left']:.1f} d" if r["days_left"] is not None else ""
        extra = f" ({r['detail']})" if r["detail"] else ""
        print(f"{r['tag']:<3} {r['label']:<9} {r['state']:<8} caduca {when} {days}{extra}{'  [renovando]' if r['renewing'] else ''}")
    return 0


def browser_script() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = STATE_DIR / "open-in-profile.sh"
    if not p.is_file() or p.read_text() != BROWSER_SCRIPT:
        p.write_text(BROWSER_SCRIPT)
    p.chmod(0o700)
    return p


def login_env(acct: Account) -> dict:
    # The account's own login only: no inherited config dir, API keys or Claude Code session vars.
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("ANTHROPIC_") or k.startswith("CLAUDE_CODE_") or k in ("CLAUDECODE", "CLAUDE_CONFIG_DIR"))}
    if acct.config_dir:
        env["CLAUDE_CONFIG_DIR"] = acct.config_dir
    env["BROWSER"] = str(browser_script())
    if acct.chrome_profile:
        env["CLAUDE_ACCOUNTS_CHROME_PROFILE"] = acct.chrome_profile
    else:
        env.pop("CLAUDE_ACCOUNTS_CHROME_PROFILE", None)
    env.setdefault("USER", os.environ.get("LOGNAME", ""))
    return env


def _stop_group(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL, to the process group this tool started (never one already reaped)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def login_one(acct: Account, settings: dict) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with lock_path(acct).open("a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log(f"{acct.tag}: a login is already running")
            return 4
        before = None
        try:
            kc = read_keychain(acct.keychain)
            before = kc and kc["refresh_expires_ms"]
        except RuntimeError:
            pass
        log(f"{acct.tag}: login as {acct.email} in {acct.chrome_profile or 'the default browser'}")
        cmd = [CLAUDE_BIN, "auth", "login", "--claudeai"] + (["--email", acct.email] if acct.email else [])
        workdir = STATE_DIR / "cwd"
        workdir.mkdir(exist_ok=True)
        try:
            proc = subprocess.Popen(cmd, env=login_env(acct), cwd=workdir, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        except OSError as exc:
            log(f"{acct.tag}: cannot start claude: {exc}")
            return 1
        try:
            _, err = proc.communicate(timeout=float(settings["login_timeout_seconds"]))
        except subprocess.TimeoutExpired:
            _stop_group(proc)
            log(f"{acct.tag}: nobody completed the login in {settings['login_timeout_seconds']} s, stopped")
            return 1
        except BaseException:
            _stop_group(proc)
            raise
        if proc.returncode != 0:
            first = next((l for l in (err or "").splitlines() if l.strip()), "")
            log(f"{acct.tag}: login failed (exit {proc.returncode}) {first[:200]}")
            return 1
        got = login_email(acct)
        if got and acct.email and got.lower() != acct.email.lower():
            log(f"{acct.tag}: WRONG USER: {got} signed in where {acct.email} belongs; sign in again with {acct.email}")
            return 2
        try:
            kc = read_keychain(acct.keychain)
        except RuntimeError as exc:
            log(f"{acct.tag}: login ended but the Keychain could not be read: {exc}")
            return 1
        after = kc and kc["refresh_expires_ms"]
        if not kc or not kc["has_refresh"]:
            log(f"{acct.tag}: login ended but no login is stored")
            return 1
        log(f"{acct.tag}: renewed, expires {iso_ms(after) or '?'} (was {iso_ms(before) or '?'})")
        return 0


def cmd_login(tags: list[str], expired: bool, soon: bool, every: bool) -> int:
    settings, accounts = load_config()
    by_tag = {a.tag: a for a in accounts}
    unknown = [t for t in tags if t not in by_tag]
    if unknown:
        raise ConfigError(f"unknown account(s): {', '.join(unknown)} (known: {', '.join(by_tag)})")
    if every:
        chosen = accounts
    elif expired or soon:
        wanted = {"expired", "missing", "wrong"} | ({"soon"} if soon else set())
        chosen = [a for a in accounts if account_status(a, settings)["state"] in wanted]
        if not chosen:
            print("nada que renovar")
            return 0
    else:
        chosen = [by_tag[t] for t in dict.fromkeys(tags)]
    if not chosen:
        raise ConfigError("name an account, or pass --expired, --soon or --all")
    with cf.ThreadPoolExecutor(len(chosen)) as pool:
        codes = dict(zip([a.tag for a in chosen], pool.map(lambda a: login_one(a, settings), chosen)))
    for tag, code in codes.items():
        label = by_tag[tag].label
        print(f"{label}: {({0: 'renovada', 1: 'no completada', 2: 'usuario equivocado, repite con ' + by_tag[tag].email, 4: 'ya había un login abierto'})[code]}")
    for worst in (2, 1, 4):
        if worst in codes.values():
            return worst
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("login")
    p.add_argument("tags", nargs="*")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--expired", action="store_true")
    g.add_argument("--soon", action="store_true")
    g.add_argument("--all", action="store_true")
    args = parser.parse_args()
    try:
        if args.cmd == "status":
            return cmd_status(args.json)
        if args.tags and (args.expired or args.soon or args.all):
            raise ConfigError("name accounts or use --expired / --soon / --all, not both")
        return cmd_login(args.tags, args.expired, args.soon, args.all)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
