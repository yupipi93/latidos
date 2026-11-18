#!/usr/bin/env python3
"""Control the default Claude account from a page (Latidos): state, activate, order.

  claude-account-control.py state                # JSON: active, priority, score per account, auto switch
  claude-account-control.py activate <tag>       # switch the default like claude-<tag>, then move idle Cursor tabs
  claude-account-control.py order lt,mt,na       # save the priority the auto switch weighs

`activate` does the steps of a shell switch command without a terminal (login
check, Cursor's setting, shared pool, T3 Code, ~/.claude-active last), under the auto
switch's lock so it never races a tick, then moves the idle Cursor tabs (scope `idle`, the
same as claude-cursor-move without a terminal; the cut ones get "continúa"). The auto
switch sees the change as manual. Progress goes to a status file that `state` returns, so
a page can start it in the background and poll.

`order` writes ~/.config/latidos/claude-order.json (CLAUDE_ACCOUNTS_ORDER_FILE), read by
claude-auto-switch.py on its next tick. Every known account is kept: a list that misses
one gets it appended.

Stdlib only. Prints JSON on stdout; exit 0 on success, 1 on failure, 2 on bad input.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("claude_auto_switch", HERE / "claude-auto-switch.py")
cas = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cas)

ACTIVATION = cas.STATE_DIR / "activation.json"
LOCK_WAIT = 90  # seconds to wait for a running tick before giving up


def out(data: dict, code: int = 0) -> int:
    print(json.dumps(data, ensure_ascii=False))
    return code


def read_activation() -> dict:
    try:
        data = json.loads(ACTIVATION.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_activation(data: dict) -> None:
    cas.STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVATION.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.replace(tmp, ACTIVATION)


# Worst case of a live run: 90 s lock wait, about 8 min of switch steps, a 20-min tab move.
STALE_ACTIVATION = dt.timedelta(minutes=35)
RUN_LOCK = cas.STATE_DIR / "activation.lock"  # computed at import; tests patch it (and ACTIVATION) directly


def run_lock_free() -> bool:
    """No activation holds the run lock (a killed run releases it at once). Accepted: the
    probe holds the lock for an instant, so an activation starting in that same instant is
    refused as busy (Latidos serialises its own state and activate calls)."""
    try:
        with RUN_LOCK.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
    except BlockingIOError:
        return False
    except OSError:
        return True


def activation_view(at: dt.datetime) -> dict:
    """The status file, with a run that never finished (killed, crashed) shown as failed:
    at once when the run lock is free, else after STALE_ACTIVATION (audit of 47)."""
    act = read_activation()
    if act.get("phase") in (None, "done"):
        return act
    begun = cas.parse_time(act.get("started_at"))
    if not begun or at - begun > STALE_ACTIVATION or run_lock_free():
        return dict(act, phase="done", ok=False, error="stale")
    return act


def state() -> int:
    at = cas.now()
    quota = cas.read_cache()
    current = cas.current_account()
    st = cas.load_state()
    skip = cas.upgrade_skip(st)
    accounts = []
    for tag in cas.ORDER:
        rep = quota.get(tag)
        s, w = cas.rooms(rep, at)
        # The account in use shows the score the rebalance compares (audit of 45).
        sc = cas.current_score(tag, rep, at) if tag == current else cas.score(tag, rep, at)
        weekly = (rep or {}).get("weekly") if isinstance(rep, dict) else None
        accounts.append({
            "tag": tag,
            "rank": cas.ORDER.index(tag) + 1,
            "weight": round(cas.priority_weight(tag), 3),
            "score": None if sc is None else round(sc, 2),
            "session_room": None if s is None else round(s, 1),
            "weekly_room": None if w is None else round(w, 1),
            "weekly_resets_at": weekly.get("resets_at") if isinstance(weekly, dict) else None,
            "fresh": cas.is_fresh(rep, at),
            "usable": cas.has_room(rep, at),
            "spent": bool(cas.spent_windows(rep, at)),
            "paying": cas.paying(rep, at),
            "skipped_until": skip.get(tag) if (cas.parse_time(skip.get(tag)) or at) > at else None,
        })
    best = cas.ranked(quota, at, [a["tag"] for a in accounts if a["usable"]])
    return out({
        "active": current,
        "order": list(cas.ORDER),
        "order_source": ("env" if os.environ.get("CLAUDE_AUTO_SWITCH_ORDER")
                         else "file" if cas.read_order_file(cas.ORDER_FILE) else "default"),
        "accounts": accounts,
        "best": best[0] if best else None,
        "auto": {
            "enabled": not cas.OFF.exists(),
            "priority_bias": cas.PRIORITY_BIAS,
            "rebalance_ratio": cas.REBALANCE_RATIO,
            "last_switch_at": st.get("last_switch_at"),
            "last_from": st.get("last_from"),
            "last_to": st.get("last_to"),
            "last_reason": st.get("last_reason"),
        },
        "activation": activation_view(at),
        "at": at.isoformat(),
    })


def activate(tag: str) -> int:
    if tag not in cas.ACCOUNTS:
        return out({"ok": False, "error": "unknown_account", "tag": tag}, 2)
    cas.STATE_DIR.mkdir(parents=True, exist_ok=True)
    # One activation at a time, decided atomically: the run lock is held for the whole run,
    # so a second one is refused at once and never overwrites the first one's progress
    # (audit of 46: check-then-write let two clicks both start).
    with RUN_LOCK.open("a") as run_fh:
        try:
            fcntl.flock(run_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return out({"ok": False, "error": "busy", "tag": tag}, 1)
        started = cas.now().isoformat()
        write_activation({"tag": tag, "phase": "waiting", "started_at": started})
        try:
            return _activate(tag, started)
        except Exception as exc:  # the status file must never stay "switching" for ever
            res = {"tag": tag, "phase": "done", "ok": False, "error": "crashed", "detail": repr(exc)[-300:],
                   "started_at": started, "finished_at": cas.now().isoformat()}
            write_activation(res)
            return out(res, 1)


def _activate(tag: str, started: str) -> int:
    cas.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with cas.LOCK.open("a") as fh:
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    res = {"tag": tag, "phase": "done", "ok": False, "error": "busy", "started_at": started,
                           "finished_at": cas.now().isoformat()}
                    write_activation(res)
                    return out(res, 1)
                time.sleep(1)
        previous = cas.current_account()
        if previous == tag:
            write_activation({"tag": tag, "phase": "moving", "started_at": started})
            ok, detail = True, ""
        else:
            write_activation({"tag": tag, "phase": "switching", "started_at": started})
            ok, detail = cas.switch_to(tag)
        if not ok:
            res = {"tag": tag, "phase": "done", "ok": False, "error": "switch_failed", "detail": detail[-300:],
                   "from": previous, "started_at": started, "finished_at": cas.now().isoformat()}
            write_activation(res)
            cas.log(f"manual switch {previous} -> {tag} from the control FAILED: {detail}")
            return out(res, 1)
        st = cas.load_state()
        left = st.get("seen") or previous
        # Also when the account clicked is already the default: a click on it is a choice
        # to stay, so no rebalance takes it away until the next automatic switch (audit of
        # 48), the same rule as a claude-<tag> typed in a terminal.
        # Recorded now, before the tab move that may take minutes (audit of 45), also when
        # the default was already changed in a terminal and no tick saw it yet (audit of
        # 46): the account left is the one the tick last saw.
        cas.record_manual(st, left, tag, cas.now())
        cas.save_state(st)
        cas.log(f"manual switch {previous} -> {tag} from the control; {detail}")
        write_activation({"tag": tag, "phase": "moving", "from": previous, "started_at": started})
        moved = cas.run([sys.executable, str(cas.CURSOR_TOOL), "--move-open", "--scope", "idle", "--summary"],
                        timeout=1200)[1]
        # The move held the lock for minutes, so no tick saved seen_at meanwhile: renew it
        # while the default is still the one seen, as the tick does after its own follow-up
        # move (A-24); a switch made by hand meanwhile is then judged as fresh (audit of 46).
        # Always, as the tick does after its own move (A-24): a terminal switch made during
        # the move is then judged on fresh numbers by the next tick (audit of 47).
        st = cas.load_state()
        st["seen_at"] = cas.now().isoformat()
        cas.save_state(st)
    summary = cas.move_summary(moved)
    counts: dict[str, int] = {}
    for e in summary or []:
        counts[e.get("outcome") or "?"] = counts.get(e.get("outcome") or "?", 0) + 1
    res = {"tag": tag, "phase": "done", "ok": True, "from": previous, "detail": detail[-300:],
           "tabs": counts, "move_finished": summary is not None,
           "started_at": started, "finished_at": cas.now().isoformat()}
    write_activation(res)
    return out(res)


def order(raw: str) -> int:
    tags = [t.strip() for t in raw.split(",") if t.strip()]
    unknown = [t for t in tags if t not in cas.ACCOUNTS]
    if unknown or not tags or len(set(tags)) != len(tags):
        return out({"ok": False, "error": "bad_order", "unknown": unknown}, 2)
    full = tags + [t for t in cas.ACCOUNTS if t not in tags]
    cas.ORDER_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = cas.ORDER_FILE.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"order": full, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n")
    os.replace(tmp, cas.ORDER_FILE)
    cas.log(f"priority set to {' > '.join(full)} from the control")
    return out({"ok": True, "order": full, "env_overrides": bool(os.environ.get("CLAUDE_AUTO_SWITCH_ORDER"))})


def main(argv: list[str]) -> int:
    if len(argv) == 1 and argv[0] == "state":
        return state()
    if len(argv) == 2 and argv[0] == "activate":
        return activate(argv[1])
    if len(argv) == 2 and argv[0] == "order":
        return order(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
