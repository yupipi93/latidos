#!/usr/bin/env python3
"""Synthetic demo of Latidos, for the README screenshots and animations.

Everything here is invented: projects, tickets, session titles, accounts and costs.
Nothing reads your own history or your ~/.config/latidos.

    python3 demo/demo.py build <dir>      # 90 days of transcripts, config, fake logins
    python3 demo/demo.py accounts <dir>   # fake account source on 127.0.0.1:7592
    python3 demo/demo.py feed <dir>       # append live calls every few seconds
    python3 demo/demo.py env <dir>        # print the variables that serve the demo

Then serve it on its own port (never on your real database):

    eval "$(python3 demo/demo.py env <dir>)" && python3 latidos.py serve
"""
import json
import math
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DAYS = 90
PORT = 7591  # not 7599: the test suite binds that one
ACCOUNTS_PORT = 7592
CODE = "/srv/demo/code"

# name, weight, (titles...)
PROJECTS = [
    ("atlas-api", 1.00, ["Rate limiter for public endpoints", "Migrate auth to OIDC", "Pagination cursor bug",
                         "OpenAPI 3.1 schema", "Flaky integration tests"]),
    ("helix-web", 0.82, ["Dark mode tokens", "Checkout redesign", "Core Web Vitals to green",
                         "Image lazy loading", "i18n for the settings page"]),
    ("nimbus-infra", 0.64, ["Terraform module for queues", "Cost alerts per team", "Blue-green deploys",
                            "Rotate service keys", "Private DNS zones"]),
    ("orbit-mobile", 0.52, ["Offline sync conflicts", "Push notification opt-in", "Crash on cold start",
                            "Widgets for the home screen"]),
    ("ember-ml", 0.40, ["Feature store backfill", "Eval harness for ranking", "Batch scoring job"]),
    ("quill-docs", 0.26, ["Getting started guide", "API reference generator", "Docs search"]),
    ("pulse-analytics", 0.22, ["Funnel dashboard", "Event schema v2", "Retention cohorts"]),
    ("vela-design", 0.15, ["Icon set refresh", "Motion guidelines"]),
]
# Ticket work started from an agent workspace: attributed through boards.json.
TICKETS = [("ACME", "Acme Corp", 0.48, ["Login latency on EU", "New staging environment", "Quarterly access review",
                                        "Webhook retries", "Export to CSV"]),
           ("NOVA", "Nova Labs", 0.30, ["Onboarding API keys", "Usage report", "SSO for the admin panel"])]
MODELS = [("claude-opus-5-5", 0.72), ("claude-sonnet-5-5", 0.22), ("claude-haiku-5-5", 0.06)]
ACCOUNTS = [("work", "Work", "#d81b4a", 0.68), ("side", "Side", "#6250d6", 0.32)]


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def pick(rng, pairs):
    total = sum(w for _, w in pairs)
    x = rng.random() * total
    for item, w in pairs:
        x -= w
        if x <= 0:
            return item
    return pairs[-1][0]


def usage(rng, big=1.0):
    read = int(rng.lognormvariate(10.9, 0.55) * big)
    out = int(rng.lognormvariate(6.6, 0.9) * big)
    write = int(rng.lognormvariate(8.0, 0.9) * big)
    return {"input_tokens": rng.randint(3, 40), "output_tokens": out, "cache_read_input_tokens": read,
            "cache_creation_input_tokens": write,
            "cache_creation": {"ephemeral_1h_input_tokens": write, "ephemeral_5m_input_tokens": 0}}


def account_uuid(tag):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "latidos-demo/" + tag))


def targets():
    """(cwd, branch_prefix, weight, titles) for every place work happens."""
    out = [(CODE + "/" + n, None, w, titles) for n, w, titles in PROJECTS]
    out += [(CODE + "/agent-skeleton", key, w, titles) for key, _, w, titles in TICKETS]
    return out


def session_lines(rng, sid, cwd, prefix, title, start, calls, acct, ticket_no):
    branch = "main"
    shown = title
    if prefix:
        branch = "fix/%s-%d" % (prefix, ticket_no)
        shown = "[%s-%d] %s" % (prefix, ticket_no, title)
    lines = [json.dumps({"type": "session-owner", "accountUuid": account_uuid(acct), "sessionId": sid}),
             json.dumps({"type": "custom-title", "customTitle": shown, "sessionId": sid})]
    model = pick(rng, MODELS)
    t = start
    for _ in range(calls):
        t += rng.uniform(4, 70)
        mid = "msg_" + uuid.UUID(int=rng.getrandbits(128)).hex[:24]
        lines.append(json.dumps({
            "type": "assistant", "timestamp": iso(t), "sessionId": sid, "cwd": cwd, "gitBranch": branch,
            "requestId": "req_" + mid,
            "message": {"id": mid, "model": model, "usage": usage(rng), "content": []}}))
    return lines, t


def write_session(logs, cwd, sid, lines):
    folder = os.path.join(logs, cwd.replace("/", "-"))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, sid + ".jsonl"), "a") as fh:
        fh.write("\n".join(lines) + "\n")


def build(root):
    rng = random.Random(7482)
    logs = os.path.join(root, "logs")
    os.makedirs(logs, exist_ok=True)
    now = time.time()
    places = targets()
    ticket_no = {key: rng.randint(100, 400) for key, _, _, _ in TICKETS}
    sessions = 0
    record_day = DAYS - 23
    for day in range(DAYS, -1, -1):
        base = datetime.fromtimestamp(now - day * 86400).replace(hour=0, minute=0, second=0, microsecond=0)
        weekday = base.weekday()
        growth = 0.55 + 0.45 * (DAYS - day) / DAYS
        load = growth * (0.25 if weekday >= 5 else 1.0) * rng.uniform(0.7, 1.3)
        if day == record_day:
            load *= 2.6
        n = max(1, int(round(9 * load)))
        for _ in range(n):
            hour = min(22.5, max(7.0, rng.gauss(13.5, 3.2)))
            start = base.timestamp() + hour * 3600
            if start > now - 600:
                continue
            cwd, prefix, _, titles = pick(rng, [(p, p[2]) for p in places])
            title = rng.choice(titles)
            num = 0
            if prefix:
                if rng.random() < 0.3:
                    ticket_no[prefix] += 1
                num = ticket_no[prefix]
            acct = pick(rng, [(a[0], a[3]) for a in ACCOUNTS])
            sid = str(uuid.UUID(int=rng.getrandbits(128)))
            calls = int(rng.lognormvariate(3.6, 0.7))
            lines, _ = session_lines(rng, sid, cwd, prefix, title, start, calls, acct, num)
            write_session(logs, cwd, sid, lines)
            sessions += 1
    # Config, boards and fake logins (only the account id is read from them).
    cfg_dir = os.path.join(root, "config")
    os.makedirs(cfg_dir, exist_ok=True)
    logins = {}
    for tag, _, _, _ in ACCOUNTS:
        d = os.path.join(root, "logins", tag)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": account_uuid(tag)}}, fh)
        logins[tag] = d
    cfg = {"roots": [logs], "code_dir": CODE, "workspaces": ["agent-skeleton"],
           "accounts_url": "http://127.0.0.1:%d" % ACCOUNTS_PORT,
           "account_colors": {t: c for t, _, c, _ in ACCOUNTS}, "account_logins": logins}
    with open(os.path.join(cfg_dir, "config.json"), "w") as fh:
        json.dump(cfg, fh, indent=2)
    with open(os.path.join(cfg_dir, "boards.json"), "w") as fh:
        json.dump({key: client for key, client, _, _ in TICKETS}, fh)
    print("%d sessions in %s" % (sessions, logs))


def env(root):
    pairs = {
        "LATIDOS_CONFIG_DIR": os.path.join(root, "config"),
        "LATIDOS_DB": os.path.join(root, "demo.db"),
        "LATIDOS_PORT": str(PORT),
        "LATIDOS_CURSOR_API": "0",
        "LATIDOS_CURSOR_DB": "/nonexistent", "LATIDOS_CURSOR_TRACKING": "/nonexistent",
        "LATIDOS_CURSOR_CHATS": "/nonexistent",
    }
    for k, v in pairs.items():
        print("export %s=%s" % (k, json.dumps(v)))


def accounts():
    def quota(tag, label, active, s_pct, w_pct, s_hours, w_days):
        now = time.time()
        return {"tag": tag, "label": label, "active": active, "plan": "Max",
                "session": {"percent": s_pct, "resets_at": iso(now + s_hours * 3600)},
                "weekly": {"percent": w_pct, "resets_at": iso(now + w_days * 86400)}}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/api/claude-quota"):
                body = {"updated_at": iso(time.time()), "accounts": [
                    quota("work", "Work", True, 38, 61, 2.4, 3.2),
                    quota("side", "Side", False, 6, 22, 4.1, 5.6)]}
            elif self.path.startswith("/api/cursor-usage"):
                now = datetime.now(timezone.utc)
                body = {"ok": True, "fetched_at": iso(time.time()), "membership": "pro",
                        "cycle_start": (now - timedelta(days=12)).isoformat(),
                        "cycle_end": (now + timedelta(days=18)).isoformat(),
                        "spend_usd": 14.6, "included": {"percent": 47}, "installed": True}
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    print("fake account source on 127.0.0.1:%d" % ACCOUNTS_PORT)
    ThreadingHTTPServer(("127.0.0.1", ACCOUNTS_PORT), H).serve_forever()


def feed(root, seconds=600):
    """Live calls: a few sessions that keep working, a call every 0.4 to 2.5 s."""
    rng = random.Random()
    logs = os.path.join(root, "logs")
    places = targets()
    live = []
    for _ in range(4):
        cwd, prefix, _, titles = pick(rng, [(p, p[2]) for p in places])
        acct = pick(rng, [(a[0], a[3]) for a in ACCOUNTS])
        sid = str(uuid.uuid4())
        head, _ = session_lines(rng, sid, cwd, prefix, rng.choice(titles), time.time(), 0, acct, 900)
        write_session(logs, cwd, sid, head)
        live.append((cwd, sid, prefix))
    end = time.time() + seconds
    while time.time() < end:
        cwd, sid, prefix = rng.choice(live)
        mid = "msg_" + uuid.uuid4().hex[:24]
        big = 3.5 if rng.random() < 0.12 else 1.0
        line = json.dumps({"type": "assistant", "timestamp": iso(time.time()), "sessionId": sid, "cwd": cwd,
                           "gitBranch": "fix/%s-900" % prefix if prefix else "main", "requestId": "req_" + mid,
                           "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage(rng, big),
                                       "content": []}})
        write_session(logs, cwd, sid, [line])
        time.sleep(rng.uniform(0.4, 2.5))


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("build", "accounts", "feed", "env"):
        sys.exit(__doc__)
    where = os.path.abspath(sys.argv[2])
    {"build": build, "env": env, "accounts": lambda _: accounts(), "feed": feed}[sys.argv[1]](where)
