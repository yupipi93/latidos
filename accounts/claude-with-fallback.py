#!/usr/bin/env python3
"""Drop-in `claude` for unattended calls: the first account in the priority, the next when it runs out.

Background jobs (meeting minutes, screenshot notes and the like) call this instead of
~/.local/bin/claude. For a headless call (`-p` / `--print`) it buffers
stdin, runs the real CLI under each account in turn and returns the first answer
that is not a quota or login failure. The accounts and their order come from the
accounts config (accounts_config.py); the default account runs with CLAUDE_CONFIG_DIR
unset, the others with their own dir.

A run counts as "out of tokens" when the CLI exits non-zero, or its result event is
an error, and the output matches LIMIT_RE (usage limit, rate limit, 429, overloaded,
credit, login/auth errors). Any other failure is returned as is: a bad prompt fails
the same under both accounts. After a quota failure the exhausted account is skipped
for COOLDOWN_MINUTES, so later calls go straight to the next one.

Interactive use (no `-p`) execs the real CLI untouched, under whatever account the
environment says.

Output is buffered and written once the chosen run finishes, which suits callers that
wait for the exit code (both current ones do). Each fallback is logged and announced
with the configured notifier. The prompt is never logged.

A request for bypass (--permission-mode bypassPermissions) is kept on accounts that
allow it and becomes --permission-mode auto on one whose admin settings disable it, the mode its Cursor tabs run in (claude_peer.run_args).

Environment:
  CLAUDE_REAL_BIN        default ~/.local/bin/claude
  CLAUDE_FALLBACK_ORDER  default the accounts config's order
  CLAUDE_FALLBACK_FORCE_FAIL  test hook: comma list of accounts treated as exhausted
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_peer  # noqa: E402  (sibling module: which accounts can run in bypass)
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)

HOME = Path.home()
REAL = os.environ.get("CLAUDE_REAL_BIN", str(HOME / ".local/bin/claude"))
CFG = accounts_config.CONFIG
ACCOUNTS = {a.tag: {"name": a.label, "config_dir": a.env} for a in CFG.accounts}
ORDER = [a for a in os.environ.get("CLAUDE_FALLBACK_ORDER", ",".join(CFG.order)).split(",") if a in ACCOUNTS]
FORCE_FAIL = set(filter(None, os.environ.get("CLAUDE_FALLBACK_FORCE_FAIL", "").split(",")))
COOLDOWN_MINUTES = 30
STATE = HOME / ".local/state/claude-fallback.json"
LOG = HOME / "Library/Logs/claude-fallback.log"

LIMIT_RE = re.compile(
    r"usage limit|limit reached|hit your (?:[\w-]+ )?limit|rate[ _-]?limit|\b429\b|overloaded"
    r"|credit balance|quota|invalid api key|please run /login|oauth token|not logged in"
    r"|authentication[_ ]error|unauthori[sz]ed|\b401\b",
    re.I,
)


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))


def env_for(account: str) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    config_dir = ACCOUNTS[account]["config_dir"]
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)  # the default account runs unset (Keychain name)
    return env


def account_args(account: str, args: list[str]) -> list[str]:
    """The args for this account: a bypass request becomes "auto" where the account cannot
    run in bypass (an account under a managed policy), so loops relaunched there still run (R-34)."""
    config_dir = ACCOUNTS[account]["config_dir"]
    return claude_peer.run_args(Path(config_dir) if config_dir else HOME / ".claude", args)


def result_is_error(stdout: bytes) -> bool:
    """True when a json / stream-json result event reports an error."""
    for line in reversed(stdout.decode("utf-8", "replace").strip().splitlines()[-5:]):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return bool(event.get("is_error"))
    return False


def out_of_tokens(account: str, code: int, stdout: bytes, stderr: bytes) -> str | None:
    if account in FORCE_FAIL:
        return "forced by CLAUDE_FALLBACK_FORCE_FAIL"
    if code == 0 and not result_is_error(stdout):
        return None
    text = (stderr + b"\n" + stdout[-4000:]).decode("utf-8", "replace")
    match = LIMIT_RE.search(text)
    return text[max(0, match.start() - 60):match.end() + 80].replace("\n", " ") if match else None


def notify(used: str, skipped: list[str]) -> None:
    names = ", ".join(ACCOUNTS[a]["name"] for a in skipped)
    CFG.notify("warn", "Claude: cambio de cuenta",
               f"La cuenta {names} se ha quedado sin tokens o sin sesión; esta tarea automática "
               f"se ha hecho con la cuenta {ACCOUNTS[used]['name']}.", timeout=120)


def main() -> int:
    args = sys.argv[1:]
    if not ({"-p", "--print"} & set(args)):
        os.execv(REAL, [REAL, *args])  # interactive: untouched

    if not ORDER:
        # No accounts configured (missing or broken config): the real CLI as is, so a
        # headless caller still gets its answer (audit of 82), with the same environment
        # cleanup as an account run (audit of 83).
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        os.execve(REAL, [REAL, *args], env)
    stdin = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
    state = load_state()
    now = time.time()
    fresh = [a for a in ORDER if state.get(a, 0) <= now]
    order = fresh + [a for a in ORDER if a not in fresh]  # cooling-down accounts go last, never dropped

    skipped: list[str] = []
    last = None
    for account in order:
        run = subprocess.run([REAL, *account_args(account, args)], input=stdin, capture_output=True,
                             env=env_for(account))
        reason = out_of_tokens(account, run.returncode, run.stdout, run.stderr)
        last = run
        if reason is None:
            if run.returncode != 0 or result_is_error(run.stdout):
                # Not a quota error, so no fallback, but say why: on 2026-10-20 two meetings lost
                # their minutes to a failure that left no trace. Error text only, never the prompt.
                detail = (run.stderr or run.stdout[-600:]).decode("utf-8", "replace").strip().replace("\n", " ")
                log(f"{account} failed, not a quota error; exit {run.returncode}: {detail[:300]}")
            if skipped:
                log(f"used {account} after {','.join(skipped)} failed; exit {run.returncode}")
                notify(account, skipped)
            if state.pop(account, None) is not None:
                save_state(state)
            break
        log(f"{account} out of tokens or session ({reason[:200]}); trying next account")
        state[account] = now + COOLDOWN_MINUTES * 60
        save_state(state)
        skipped.append(account)
    else:
        log(f"all accounts failed ({','.join(skipped)}); returning the last error")

    sys.stdout.buffer.write(last.stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(last.stderr)
    return last.returncode


if __name__ == "__main__":
    sys.exit(main())
