#!/usr/bin/env python3
"""Drop-in `claude` for unattended calls: the active account first, the next when it runs out.

Background jobs (meeting minutes, screenshot notes, agent runs and the like) call this
instead of ~/.local/bin/claude. For a headless call (`-p` / `--print`) it buffers
stdin, runs the real CLI under each account in turn and returns the first answer
that is not a quota or login failure. The default account runs with CLAUDE_CONFIG_DIR
unset, the others with their own dir.

Try order (L-194): the account in the active file (~/.claude-active, config
`active_file`) first, then the effective priority: the Latidos order file
(claude-order.json, written by the account cards) when present, else the accounts
config's `order`. Each account is tried at most once per call. Pins: an order the caller
gives in CLAUDE_FALLBACK_ORDER is used as is (no active-first), and
CLAUDE_FALLBACK_ACCOUNT=<tag> puts that account first whatever the active one is. Unknown
tags in either are logged and ignored.

When a run counts as "out of tokens" (audits of 103 and 106): the CLI failed (non-zero exit
or an error result) AND the failure itself names a limit or a login problem. Only these
sources are read, never tool results or the model's own text: stderr, the final result
event (`api_error_status` 401, or its text when it is an error), top-level error events, the
CLI's synthetic error notices (their codes rate_limit, authentication_failed and
billing_error count directly), and raw output only when it holds no JSON at all (plain
text output; `--output-format json --verbose` prints one JSON array, read as events). A
usage limit or a login problem (expired or unrefreshable sessions included) benches the
account; a transient rate limit (a 429 rate_limit_error) moves to the next account without
benching it; an overloaded API (529) is returned as is. Any other failure is returned as is.

Resume, never restart (audit of 106): a run that already did work (a non-synthetic
assistant turn or a tool result in stream-json, not a replayed message; more than one turn
in json) and then hits a limit is not started again, which would repeat its side effects
(commits, pushes, posts). The same session continues on the next account with
`--resume <session_id>` and CONTINUE_TEXT as the prompt, the caller's other flags kept;
transcripts live in the shared pool, so any account can resume it. Once a session is being
continued, every later account continues it too, at most once per account; the limit
result goes back only when every account is spent. The resume needs the shared pool: the
next account's projects/ must resolve to the same folder, else the limit result goes back.
It also needs a session id in the output, session persistence, text input (not
`--input-format stream-json`) and arguments it is certain about: every flag known
(BOOL_FLAGS, VALUE_FLAGS, VARIADIC_FLAGS with all their values, `--flag=value`), no
optional-value flag, no variadic flag together with a stdin prompt, and exactly one
positional prompt or a prompt on stdin; the positional and stdin both go, the continue
text is the only prompt. Anything else and the limit result goes back as before release
108, so the original prompt is never sent again and no flag value is lost (audits of 108
and 109). A resume that fails for another reason than a limit before doing any work ("No
conversation found") hands back the earlier limit result, after one more marker in a
stream, so a ticket desk sees quota; a resume that did work and then failed is passed
through as it is, its own output and exit code (audit of 109). Plain text output shows no turns, so there a failed
run is started again on the next account; agent runners use stream-json.

After a quota failure the exhausted account is skipped for COOLDOWN_MINUTES, so later calls
go straight to the next one.

Interactive use (no `-p`) execs the real CLI untouched, under whatever account the
environment says.

Output is buffered and written once the chosen run finishes, which suits callers that
wait for the exit code. With `--output-format stream-json` it is streamed instead (L-195):
each attempt's events are copied as they come. Before a retry's stream the wrapper writes
one event `{"type": "system", "subtype": "account_fallback", "from": .., "to": ..}`, so a
reader can drop what the failed attempt wrote (a reader that keeps the last session_id and
the last result event already sees the run that answered). Each fallback is logged and
announced with the configured notifier. The prompt is never logged.

The real CLI never outlives the wrapper: SIGTERM, SIGINT and SIGHUP are passed on to it
and the wrapper leaves at once (the handler never waits: audit of 106), it is ended in a
`finally`, and a small watcher process ends it when the wrapper is gone (a caller's
timeout SIGKILLs the wrapper, which no handler can catch), escalating to SIGKILL. The
watcher recognises the child by its start time, never by its name.

A request for bypass (--permission-mode bypassPermissions) is kept on accounts that
allow it and becomes --permission-mode auto on one whose admin settings disable it, the mode its Cursor tabs run in (claude_peer.run_args).

Environment:
  CLAUDE_REAL_BIN        default ~/.local/bin/claude
  CLAUDE_FALLBACK_ORDER  a fixed try order, used as is (default: active, then the effective order)
  CLAUDE_FALLBACK_ACCOUNT  pin: this account first, then the order
  CLAUDE_FALLBACK_FORCE_FAIL  test hook: comma list of accounts treated as exhausted
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import claude_peer  # noqa: E402  (sibling module: which accounts can run in bypass)
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)

HOME = Path.home()
REAL = os.environ.get("CLAUDE_REAL_BIN", str(HOME / ".local/bin/claude"))
CFG = accounts_config.CONFIG
ACCOUNTS = {a.tag: {"name": a.label, "config_dir": a.env} for a in CFG.accounts}
EXPLICIT_ORDER = os.environ.get("CLAUDE_FALLBACK_ORDER", "")
PINNED = os.environ.get("CLAUDE_FALLBACK_ACCOUNT", "").strip()
ACTIVE = CFG.active_file
FORCE_FAIL = set(filter(None, os.environ.get("CLAUDE_FALLBACK_FORCE_FAIL", "").split(",")))
COOLDOWN_MINUTES = 30
# A transient failure never benches on its own, but this many in a row on one account bench
# it for TRANSIENT_BENCH_MINUTES (audit of 110). The count lives in the state file under
# "transient:<tag>", next to the accounts' bench times.
TRANSIENT_BENCH_AFTER = 3
TRANSIENT_BENCH_MINUTES = 5
LOCK_WAIT_SECONDS = 2.0
STATE = HOME / ".local/state/claude-fallback.json"
LOG = HOME / "Library/Logs/claude-fallback.log"
KILL_GRACE = 3.0  # seconds between SIGTERM and SIGKILL of the real CLI

# Usage-limit or login failures: the account is benched for COOLDOWN_MINUTES. No
# "overloaded": a 529 is transient (audit of 103). Expired logins included (audit of 106).
LIMIT_RE = re.compile(
    r"usage limit|usage_limit_reached|limit reached|hit your (?:[\w-]+ )?limit"
    r"|credit balance|billing_error|quota|invalid api key|please run /login"
    r"|oauth token (?:has )?(?:expired|been revoked|is invalid)|invalid oauth token|not logged in"
    r"|authentication[_ ]error|authentication_failed|failed to authenticate|session expired"
    r"|could not be refreshed|unauthori[sz]ed|\b401\b",
    re.I,
)
# A transient rate limit (a 429 rate_limit_error the CLI gave up retrying): the next account
# is tried, but nobody is benched (audit of 106).
TRANSIENT_RE = re.compile(r"rate[ _-]?limit|\b429\b|too many requests", re.I)
# The CLI's own error codes on its synthetic assistant notice: a limit, directly. A
# `rate_limit` notice is a usage limit only with apiError usage_limit_reached or limit text
# (what recorded transcripts show); flagged apiErrorIsTransient, or bare, it is transient.
LIMIT_ERRORS = {"authentication_failed", "billing_error"}
# A failure the CLI itself calls transient (a failed OAuth refresh "usually transient", a
# server_error) never benches.
# A Cloudflare or HTML block page (a 403 in front of the API) is not the account's fault
# either (audit of 109).
TRANSIENT_TEXT_RE = re.compile(r"usually transient|cloudflare|<\s*(?:!doctype\s+)?html", re.I)


def log(msg: str) -> None:
    """Best effort: an unwritable log never stops a call (audit of 110)."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    """Temp file plus rename: an exit mid-write (os._exit on a signal) never truncates it."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_name(f".{STATE.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE)


def _locked(mutate) -> dict | None:
    """Run `mutate(state)` on the file as it is now, under an exclusive lock, and save it: a
    wrapper running at the same time never loses another's update (audit of 109). The lock is
    tried for LOCK_WAIT_SECONDS; then, or on any file error (a read-only HOME), the update is
    logged and skipped, never raised: the caller's output must still go out (audit of 110)."""
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        with open(STATE.with_name(STATE.name + ".lock"), "a") as lock:
            deadline = time.time() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.time() >= deadline:
                        log("state lock busy, update skipped")
                        return None
                    time.sleep(0.05)
            try:
                state = load_state()
                mutate(state)
                save_state(state)
                return state
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError as exc:
        log(f"state update skipped: {exc}")
        return None


def update_state(changes: dict) -> dict | None:
    """Apply `{key: value}` (None drops the key) under the lock."""
    def mutate(state: dict) -> None:
        for key, value in changes.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
    return _locked(mutate)


def note_transient(account: str, now: float) -> bool:
    """Count one more transient failure in a row; bench the account for
    TRANSIENT_BENCH_MINUTES once the count reaches TRANSIENT_BENCH_AFTER. True when benched."""
    benched = []

    def mutate(state: dict) -> None:
        key = f"transient:{account}"
        count = int(state.get(key) or 0) + 1
        if count >= TRANSIENT_BENCH_AFTER:
            state[account] = max(float(state.get(account) or 0), now + TRANSIENT_BENCH_MINUTES * 60)
            state.pop(key, None)
            benched.append(True)
        else:
            state[key] = count
    _locked(mutate)
    return bool(benched)


def known(tags) -> list[str]:
    """Configured tags only, stripped, each once, in the given order."""
    return [t for t in dict.fromkeys(str(t).strip() for t in tags) if t in ACCOUNTS]


def unknown_tags(raw: str) -> list[str]:
    """Tags in a comma list that name no configured account."""
    return [t for t in (x.strip() for x in raw.split(",")) if t and t not in ACCOUNTS]


def effective_order() -> list[str]:
    """The priority without the active account: CLAUDE_FALLBACK_ORDER as given (only those
    accounts), else the Latidos order file followed by every other configured account,
    else the config's order."""
    if EXPLICIT_ORDER.strip():
        return known(EXPLICIT_ORDER.split(","))
    saved = accounts_config.read_order_file(accounts_config.order_file_path())
    return known((saved.split(",") if saved else []) + list(CFG.order))


def active_account() -> str | None:
    """The tag in the active file, when it names a configured account."""
    try:
        tag = ACTIVE.read_text().strip()
    except OSError:
        return None
    return tag if tag in ACCOUNTS else None


def try_order(order: list[str], active: str | None, pinned: str | None, explicit: bool,
              cooling: set[str]) -> list[str]:
    """The accounts to try, each once (L-194): the pinned account, else the active one
    (not with an explicit order), then the order. Accounts cooling down after a limit go
    last, never dropped; a pinned account stays first even then."""
    first = pinned if pinned in ACCOUNTS else (None if explicit else active)
    seq = known(([first] if first else []) + list(order))
    head = [seq.pop(0)] if seq and pinned in ACCOUNTS else []
    fresh = [a for a in seq if a not in cooling]
    return head + fresh + [a for a in seq if a in cooling]


def wants_stream(args: list[str]) -> bool:
    """`--output-format stream-json` (either spelling) before a `--`."""
    for i, a in enumerate(args):
        if a == "--":
            return False
        if a == "--output-format=stream-json" or (a == "--output-format" and args[i + 1:i + 2] == ["stream-json"]):
            return True
    return False


class Attempt:
    """What one account's run left: exit code, captured stdout (or its tail when streamed),
    stderr, and whether it already did work (then it is never re-run)."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes, progressed: bool = False,
                 session: str | None = None):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr
        self.progressed, self.session = progressed, session


STREAM_TAIL = 64 * 1024


def is_progress(event) -> bool:
    """A stream-json event that shows the run did work: a tool result, or an assistant turn
    that is not the CLI's own synthetic error message (the limit notice is one)."""
    if not isinstance(event, dict):
        return False
    kind = event.get("type")
    if kind == "user":
        # A tool result is work; a replayed message (a resumed session's history, a queued
        # prompt echoed back) is not (audit of 106).
        if event.get("isReplay"):
            return False
        msg = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        return any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    if kind != "assistant" or event.get("error") or event.get("isApiErrorMessage"):
        return False
    msg = event.get("message") if isinstance(event.get("message"), dict) else {}
    return msg.get("model") != "<synthetic>"


# ---- The child process: never outlives the wrapper (M2) ---------------------------------

CHILDREN: set = set()  # live Popen objects of the real CLI

WATCHER = r"""
import os, signal, subprocess, sys, time
parent, child, grace = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
def started(pid):
    return subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
# The child's start time, read while it is surely ours: a reused PID has another one.
birth = started(child)
def same(pid):
    return bool(birth) and started(pid) == birth
while alive(child):
    if os.getppid() != parent:
        if alive(child) and same(child):
            os.kill(child, signal.SIGTERM)
            end = time.time() + grace
            while time.time() < end and alive(child):
                time.sleep(0.1)
            if alive(child) and same(child):
                os.kill(child, signal.SIGKILL)
        break
    time.sleep(0.3)
"""


def start_watcher(child_pid: int):
    """A tiny process that ends the real CLI if this wrapper disappears (its parent changes)."""
    try:
        return subprocess.Popen([sys.executable, "-I", "-c", WATCHER, str(os.getpid()), str(child_pid),
                                 str(KILL_GRACE)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                preexec_fn=_unblock)
    except OSError:
        return None


def end_process(proc) -> None:
    """SIGTERM a process this wrapper started, SIGKILL after the grace time."""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=KILL_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def _on_signal(signum, _frame):
    """Pass the signal on and leave at once. Never wait here (audit of 106: the handler's
    Popen.wait deadlocked against the main thread's): each child's watcher sees this
    process gone and escalates to SIGKILL after the grace time."""
    for proc in list(CHILDREN):
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except OSError:
            pass
    os._exit(128 + signum)


def install_signal_handlers() -> None:
    for sig in SIGNALS:
        signal.signal(sig, _on_signal)


def _unblock() -> None:
    """In a new child, before exec: the signals blocked around the spawn are not inherited."""
    signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)


def _feed(proc, data: bytes) -> None:
    """Write stdin from a thread: a large prompt never blocks on a full pipe (minor 4)."""
    try:
        proc.stdin.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def run_child(cmd: list[str], stdin: bytes, env: dict, stream: bool) -> Attempt:
    """One attempt. Streamed: stdout copied line by line as it comes, a tail kept for the
    limit check. Buffered: everything captured. Stderr is collected aside."""
    # No signal between the spawn and the bookkeeping: a SIGTERM there would miss the new
    # child (audit of 106). The children get the signals unblocked before exec.
    signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, preexec_fn=_unblock)
        CHILDREN.add(proc)
        watcher = start_watcher(proc.pid)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, SIGNALS)
    err: list[bytes] = []
    readers = [threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True),
               threading.Thread(target=_feed, args=(proc, stdin), daemon=True)]
    for t in readers:
        t.start()
    out = bytearray()
    progressed = False
    session = None
    try:
        for line in iter(proc.stdout.readline, b""):
            if stream:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
                try:
                    event = json.loads(line)
                except ValueError:
                    event = None
                if isinstance(event, dict):
                    progressed = progressed or is_progress(event)
                    if isinstance(event.get("session_id"), str) and event["session_id"]:
                        session = event["session_id"]
            out += line
            if stream and len(out) > STREAM_TAIL:
                del out[:len(out) - STREAM_TAIL]
        code = proc.wait()
    finally:
        end_process(proc)
        CHILDREN.discard(proc)
        end_process(watcher)
    for t in readers:
        t.join(timeout=5)
    if not stream:
        progressed = buffered_progress(bytes(out))
        session = session_of(bytes(out))
    return Attempt(code, bytes(out), b"".join(err), progressed, session)


# ---- Reading an attempt ------------------------------------------------------------------

def events(stdout: bytes) -> list[dict]:
    """The JSON events of an attempt: one per line (stream-json, json), or one JSON array
    (`--output-format json --verbose`, audit of 106)."""
    text = stdout.decode("utf-8", "replace")
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                found.append(event)
    return found


def session_of(stdout: bytes) -> str | None:
    """The last session id an attempt reported (init or result event)."""
    for event in reversed(events(stdout)):
        if isinstance(event.get("session_id"), str) and event["session_id"]:
            return event["session_id"]
    return None


def final_result(stdout: bytes) -> dict | None:
    for event in reversed(events(stdout)):
        if event.get("type") == "result":
            return event
    return None


def buffered_progress(stdout: bytes) -> bool:
    """json output: the run took more than one turn (it used tools)."""
    result = final_result(stdout)
    try:
        return bool(result) and int(result.get("num_turns") or 0) > 1
    except (TypeError, ValueError):
        return False


def result_is_error(stdout: bytes) -> bool:
    """True when a json / stream-json result event reports an error."""
    result = final_result(stdout)
    return bool(result and result.get("is_error"))


def _snippet(text: str, pattern=LIMIT_RE) -> str | None:
    match = pattern.search(text)
    return text[max(0, match.start() - 60):match.end() + 80].replace("\n", " ") if match else None


def classify_failure(account: str, code: int, stdout: bytes, stderr: bytes) -> tuple[str | None, bool]:
    """(why this failed run should move to another account, whether to bench the account).
    Reads only the failure itself: stderr, the final result event (its text only when it is
    an error), top-level error events and the CLI's synthetic error notices; raw text only
    when the output holds no JSON at all. A usage limit or a login problem benches the
    account; a transient rate limit moves on without benching."""
    if account in FORCE_FAIL:
        return "forced by CLAUDE_FALLBACK_FORCE_FAIL", True
    if code == 0 and not result_is_error(stdout):
        return None, False
    err_text = stderr[-4000:].decode("utf-8", "replace")
    # A limit or login line in stderr (a real "Please run /login") benches whatever the page
    # text says: the block-page test below applies to the notice and result text only
    # (audit of 110).
    err_usage = _snippet(err_text)
    if err_usage:
        return err_usage, True
    parts = [err_text]
    transient_notice = None
    evs = events(stdout)
    result = final_result(stdout)
    status = None
    result_parts: list[str] = []  # scanned only when no transient notice explains the failure
    if result is not None:
        status = result.get("api_error_status")
        if status == 401:
            return "api_error_status 401", True
        if result.get("is_error") and isinstance(result.get("result"), str):
            result_parts.append(result["result"])
        if isinstance(result.get("errors"), list):
            result_parts.extend(str(e) for e in result["errors"])
    for event in evs:
        if event.get("type") == "error":
            parts.append(str(event.get("error") or event.get("message") or ""))
        elif event.get("type") == "assistant" and (event.get("error") or event.get("isApiErrorMessage")):
            # The CLI's synthetic notice for an API error (the limit message): its own text.
            msg = event.get("message") if isinstance(event.get("message"), dict) else {}
            content = msg.get("content") if isinstance(msg.get("content"), list) else []
            notice = " ".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
            if event.get("apiErrorIsTransient") or TRANSIENT_TEXT_RE.search(notice):
                # Flagged transient by the CLI, or a block page: never benched, and its text is
                # kept out of the scan so a limit word in it cannot bench either (audit of 109).
                transient_notice = f"assistant error {event.get('error') or ''} (transient: {notice[:80]})"
                continue
            if event.get("error") in LIMIT_ERRORS or event.get("apiError") == "usage_limit_reached":
                return f"assistant error {event.get('apiError') or event['error']}", True
            if event.get("error") == "rate_limit":
                if LIMIT_RE.search(notice):
                    return f"assistant error rate_limit ({notice[:80]})", True
                transient_notice = f"assistant error rate_limit (transient: {notice[:80]})"
                continue
            parts.append(str(event.get("error") or ""))
            parts.extend(str(c.get("text") or "") for c in content if isinstance(c, dict))
    if not evs:
        parts.append(stdout[-1000:].decode("utf-8", "replace"))  # text output: the error message
    if not transient_notice:
        parts.extend(result_parts)  # the result repeats the notice's text: same rule
    text = "\n".join(parts)
    usage = _snippet(text)
    if usage and not TRANSIENT_TEXT_RE.search("\n".join(parts[1:])):
        return usage, True
    transient = _snippet(text, TRANSIENT_RE) or (usage and f"transient: {usage}") or transient_notice
    if transient or status == 429:
        return transient or "api_error_status 429", False
    return None, False


def out_of_tokens(account: str, code: int, stdout: bytes, stderr: bytes) -> str | None:
    """Why this failed run should move to another account, or None (classify_failure)."""
    return classify_failure(account, code, stdout, stderr)[0]


def notify(used: str, skipped: list[str]) -> None:
    names = ", ".join(ACCOUNTS[a]["name"] for a in skipped)
    CFG.notify("warn", "Claude: cambio de cuenta",
               f"La cuenta {names} se ha quedado sin tokens o sin sesión; esta tarea automática "
               f"se ha hecho con la cuenta {ACCOUNTS[used]['name']}.", timeout=120)


# What a session resumed on the next account is told (audit of 106). Not an approval.
CONTINUE_TEXT = os.environ.get(
    "CLAUDE_FALLBACK_CONTINUE_TEXT",
    "Continúa exactamente donde lo dejaste: el turno anterior se cortó por el límite de uso de la "
    "cuenta y esta sesión sigue en otra cuenta. No repitas lo que ya hiciste (commits, push, "
    "comentarios); revisa el estado y termina la tarea. Es un aviso automático, no una aprobación nueva.")
# The CLI flags the resume understands (audits of 108 and 109). The rule is "resume only
# when certain": every token must be one of these, a `--flag=value`, a flag's value, or the
# one positional prompt; anything unusual and the session is not resumed (the limit result
# goes back, as before release 108), so the original prompt is never sent again and no
# flag value is ever lost.
VALUE_FLAGS = {
    "--output-format", "--input-format", "--model", "--permission-mode", "--system-prompt",
    "--append-system-prompt", "--system-prompt-file", "--append-system-prompt-file", "--json-schema",
    "--setting-sources", "--max-turns", "--settings", "--fallback-model", "--max-budget-usd", "--agents",
    "--permission-prompt-tool", "--effort", "--max-thinking-tokens", "--plugin-dir", "--agent", "-n", "--name",
    "--debug-file", "--remote-control-session-name-prefix", "--permission-prompts",
}
# Variadic in CLI 2.1.323: they take every value up to the next flag, so a positional after
# them is one of their values, never the prompt (audit of 109: `--disallowedTools Bash Write`
# lost Write on a resume).
VARIADIC_FLAGS = {
    "--allowedTools", "--allowed-tools", "--disallowedTools", "--disallowed-tools", "--tools", "--add-dir",
    "--mcp-config", "--betas",
}
# Optional values: whether the next token is theirs cannot be known for sure, so their
# presence means no resume.
OPTIONAL_VALUE_FLAGS = {"-d", "--debug", "-w", "--worktree", "--from-pr"}
BOOL_FLAGS = {
    "-p", "--print", "--verbose", "--include-partial-messages", "--replay-user-messages", "--strict-mcp-config",
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--no-chrome", "--chrome",
    "--ide", "--disable-slash-commands", "--mcp-debug", "--include-hook-events",
}
SESSION_VALUE_FLAGS = {"--resume", "-r", "--session-id"}
SESSION_FLAGS = {"--continue", "-c", "--fork-session"}


def resume_args(args: list[str], session: str, prompt_on_stdin: bool) -> list[str] | None:
    """The args that continue `session` instead of starting the prompt again: the caller's
    flags without its own session flags or its prompt, then `--resume <session>` and
    CONTINUE_TEXT as the prompt (stdin then stays empty). None (no resume) unless the shape
    is certain: no `--no-session-persistence`, no `--`, text input, only known flags, no
    optional-value flag, no variadic flag together with a stdin prompt, and exactly one
    positional prompt or (with none) a prompt on stdin (audits of 106, 108 and 109)."""
    if "--no-session-persistence" in args or "--" in args:
        return None
    out: list[str] = []
    positionals: list[int] = []  # indexes in `out`
    variadic = False
    i = 0
    while i < len(args):
        a = args[i]
        name, has_value = a.split("=", 1)[0], "=" in a
        if name in OPTIONAL_VALUE_FLAGS:
            return None
        if name in SESSION_VALUE_FLAGS:
            i += 1 if has_value else 2
            continue
        if a in SESSION_FLAGS:
            i += 1
            continue
        if name == "--input-format":
            fmt = a.split("=", 1)[1] if has_value else (args[i + 1] if i + 1 < len(args) else "")
            if fmt == "stream-json":
                return None  # the prompt is a stream of messages on stdin: no safe resume
        if name in VARIADIC_FLAGS:
            variadic = True
            out.append(a)
            i += 1
            if not has_value:
                taken = 0
                while i < len(args) and not args[i].startswith("-"):
                    out.append(args[i])
                    i += 1
                    taken += 1
                if not taken:
                    return None
            continue
        if name in VALUE_FLAGS:
            if has_value:
                out.append(a)
                i += 1
            elif i + 1 < len(args):
                out += [a, args[i + 1]]
                i += 2
            else:
                return None
            continue
        if a in BOOL_FLAGS or (a.startswith("--") and has_value):
            out.append(a)
            i += 1
            continue
        if a.startswith("-"):
            return None  # an unknown flag: it may take a value, so the prompt is unclear
        positionals.append(len(out))
        out.append(a)
        i += 1
    if variadic and prompt_on_stdin:
        return None
    if len(positionals) > 1:
        return None
    if not positionals and not prompt_on_stdin:
        return None
    # The positional prompt goes (with stdin context, it was still the prompt), and so does
    # stdin: the resumed session only gets the continue text.
    if positionals:
        del out[positionals[0]]
    return [*out, "--resume", session, CONTINUE_TEXT]


def notify_resumed_failed(used: str, skipped: list[str]) -> None:
    names = ", ".join(ACCOUNTS[a]["name"] for a in skipped)
    CFG.notify("warn", "Claude: cambio de cuenta",
               f"La cuenta {names} se quedó sin tokens a mitad de una tarea automática; se continuó en la "
               f"cuenta {ACCOUNTS[used]['name']}, pero terminó con error.", timeout=120)


def fallback_marker(prev: str, nxt: str) -> bytes:
    """The event written before a retry's stream (minor 3)."""
    return (json.dumps({"type": "system", "subtype": "account_fallback", "from": prev, "to": nxt}) + "\n").encode()


def main() -> int:
    args = sys.argv[1:]
    if not ({"-p", "--print"} & set(args)):
        os.execv(REAL, [REAL, *args])  # interactive: untouched

    bad = unknown_tags(EXPLICIT_ORDER) + unknown_tags(PINNED)
    if bad:
        log(f"ignoring unknown account(s) {','.join(bad)} in CLAUDE_FALLBACK_ORDER / CLAUDE_FALLBACK_ACCOUNT")
        if sys.stderr.isatty():
            print(f"claude-with-fallback: unknown account(s) ignored: {','.join(bad)}", file=sys.stderr)
    explicit = bool(EXPLICIT_ORDER.strip())
    base = effective_order()
    if not base and not (PINNED in ACCOUNTS):
        # No accounts configured (missing or broken config): the real CLI as is, so a
        # headless caller still gets its answer (audit of 82), with the same environment
        # cleanup as an account run (audit of 83).
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        os.execve(REAL, [REAL, *args], env)
    install_signal_handlers()
    stdin = b"" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.buffer.read()
    state = load_state()
    now = time.time()
    cooling = {a for a in ACCOUNTS if state.get(a, 0) > now}
    order = try_order(base, active_account(), PINNED or None, explicit, cooling)
    stream = wants_stream(args)

    skipped: list[str] = []
    last = None
    run_args, run_stdin = args, stdin
    resuming = False  # once a run did work, later accounts continue its session
    session = None
    limited = None  # the last attempt that hit a limit after work: what goes back if a resume fails
    for account in order:
        if resuming and not same_pool(skipped[-1], account):
            # The next account would not find the transcript (audit of 108): hand back the limit.
            log(f"not resuming session {session} on {account}: its projects/ is not the same folder as "
                f"{skipped[-1]}'s (no shared pool); returning the limit result")
            last = limited
            break
        if stream and skipped:
            sys.stdout.buffer.write(fallback_marker(skipped[-1], account))
            sys.stdout.buffer.flush()
        run = run_child([REAL, *account_args(account, run_args)], run_stdin, env_for(account), stream)
        if stream:
            sys.stderr.buffer.write(run.stderr)
            sys.stderr.buffer.flush()
        reason, bench = classify_failure(account, run.returncode, run.stdout, run.stderr)
        last = run
        session = run.session or session
        if reason is not None and (run.progressed or resuming):
            # Work already happened: never start it again elsewhere (audit of 103), continue
            # the same session on the next account instead (audit of 106). Transcripts live in
            # the shared pool, so any account can resume it.
            nxt = resume_args(args, session, bool(stdin)) if session else None
            if nxt is None:
                log(f"{account} hit a limit after doing work ({reason[:200]}); its session cannot be resumed "
                    f"safely, returned as is")
                if bench:
                    update_state({account: now + COOLDOWN_MINUTES * 60, f"transient:{account}": None})
                else:
                    note_transient(account, now)
                break
            if not resuming:
                log(f"{account} hit a limit after doing work ({reason[:200]}); resuming session {session} "
                    f"on the next account")
            run_args, run_stdin, resuming, limited = nxt, b"", True, run
        if (reason is None and resuming and not run.progressed
                and (run.returncode != 0 or result_is_error(run.stdout))):
            # The resume itself failed before doing any work, not on a limit ("No conversation
            # found"...): the caller must still see the limit, so a ticket desk waits for a reset
            # instead of calling it an error (audit of 108). In a stream, a second marker and the
            # limit result again. A resume that did work and then failed is passed through as
            # it is: its work and its own result must reach the caller (audit of 109).
            detail = (run.stderr or run.stdout[-600:]).decode("utf-8", "replace").strip().replace("\n", " ")
            log(f"resuming session {session} on {account} failed, not a limit (exit {run.returncode}: "
                f"{detail[:200]}); returning the earlier limit result")
            if stream:
                result = final_result(limited.stdout)
                sys.stdout.buffer.write(fallback_marker(account, skipped[-1]))
                if result is not None:
                    sys.stdout.buffer.write((json.dumps(result) + "\n").encode())
                sys.stdout.buffer.flush()
            last = limited
            break
        if reason is None:
            failed = run.returncode != 0 or result_is_error(run.stdout)
            if failed:
                # Not a quota error, so no fallback, but say why: on 2026-10-20 two meetings lost
                # their minutes to a failure that left no trace. Error text only, never the prompt.
                detail = (run.stderr or run.stdout[-600:]).decode("utf-8", "replace").strip().replace("\n", " ")
                log(f"{account} failed, not a quota error; exit {run.returncode}: {detail[:300]}")
            if skipped:
                log(f"used {account} after {','.join(skipped)} failed; exit {run.returncode}")
                if failed and resuming:
                    notify_resumed_failed(account, skipped)  # never "done" (audit of 110)
                elif not failed:
                    notify(account, skipped)
            if not failed:
                # Only a success clears the account's bench and transient count (audit of 110).
                update_state({account: None, f"transient:{account}": None})
            break
        if bench:
            log(f"{account} out of tokens or session ({reason[:200]}); trying next account")
            update_state({account: now + COOLDOWN_MINUTES * 60, f"transient:{account}": None})
        elif note_transient(account, now):
            log(f"{account} transient failure ({reason[:200]}), {TRANSIENT_BENCH_AFTER} in a row: benched "
                f"{TRANSIENT_BENCH_MINUTES} min; trying next account")
        else:
            log(f"{account} transient rate limit ({reason[:200]}); trying next account, not benched")
        skipped.append(account)
    else:
        log(f"all accounts failed ({','.join(skipped)}); returning the last error")

    if not stream:
        sys.stdout.buffer.write(last.stdout)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(last.stderr)
    return last.returncode


def account_dir(account: str) -> Path:
    config_dir = ACCOUNTS[account]["config_dir"]
    return Path(config_dir) if config_dir else HOME / ".claude"


def same_pool(a: str, b: str) -> bool:
    """Both accounts' projects/ resolve to one folder (the shared pool): a session started on
    one can be resumed on the other."""
    try:
        return os.path.realpath(account_dir(a) / "projects") == os.path.realpath(account_dir(b) / "projects")
    except OSError:
        return False


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


if __name__ == "__main__":
    sys.exit(main())
