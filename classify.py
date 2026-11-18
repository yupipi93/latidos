#!/usr/bin/env python3
"""Smart task classification for Latidos (L-158).

A model reads what each Claude Code session was about (its real prompts, the cost of each
turn, the repositories its tools touched, tickets and branches) and splits the session
into segments, each one with a kind (client, internal, tool, personal), a client, a
project and a short task title. Latidos uses the result, written to `classified.json` in
the user config, before its folder heuristics; a task moved by hand still wins.

Runs on demand or from a LaunchAgent (`classify.py install`). It reads the Latidos
database read-only and the transcripts it points at, sends dossiers to `claude -p` (no
tools, no settings, no MCP servers) and only writes `classified.json`. Incremental: a
session is sent again only when it has new turns, and then only its new turns.

Usage:
  classify.py run [--limit N] [--jobs N] [--model M] [--session SID] [--dry-run]
  classify.py consolidate        merge duplicate project names with one model call
  classify.py status             what is classified and what is pending
  classify.py install|uninstall  LaunchAgent that runs `run` every 30 minutes
"""
import argparse
import concurrent.futures as cf
import datetime as dt
import fcntl
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import threading
import time

import latidos as L

VERSION = 1
KINDS = ("client", "internal", "tool", "personal")
MAX_TURNS = 120          # turns per dossier; more are merged pairwise by lowest cost
SHORT_PROMPT = 12        # a prompt shorter than this ("sí", "continúa") joins the turn before
BATCH_CHARS = 45000      # dossier text per model call
BATCH_SESSIONS = 30
SETTLE_MS = 3 * 60 * 1000  # turns younger than this wait for the next run
MARKER = "[latidos-classifier]"  # first line of every prompt this tool sends

# Prompt texts that are not the user talking: notifications, command echoes, reminders.
SKIP_PREFIXES = ("<task-notification", "<local-command", "Caveat:", "[Request interrupted",
                 "<bash-input", "<bash-stdout", "<bash-stderr", "<user-memory", "<system-reminder")
TAG_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
CMD_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>.*?(?:<command-args>(.*?)</command-args>)?", re.S)


def out_path():
    return L.config_file("classified.json", "CLASSIFIED")


def taxonomy_path():
    return L.config_file("taxonomy.json", "TAXONOMY")


def log(msg):
    sys.stderr.write("%s classify: %s\n" % (dt.datetime.now().strftime("%H:%M:%S"), msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------------------

def load_state(path=None):
    path = path or out_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise SystemExit("classify: %s cannot be read (%s): fix or remove it first" % (path, exc))
    if not isinstance(data, dict):
        raise SystemExit("classify: %s is not a JSON object" % path)
    data.setdefault("version", VERSION)
    data.setdefault("sessions", {})
    data.setdefault("projects", {})
    data.setdefault("folders", {})
    return data


def save_state(state, path=None):
    path = path or out_path()
    state["updated"] = int(time.time() * 1000)
    real = os.path.realpath(path)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    tmp = "%s.%d.tmp" % (real, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, real)


def load_taxonomy(path=None):
    path = path or taxonomy_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SystemExit("classify: %s cannot be read (%s)" % (path, exc))
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------------------
# Dossiers
# ---------------------------------------------------------------------------------------

def open_db(path=None):
    path = path or L.config()["db"]
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=30)
    con.execute("PRAGMA query_only = 1")
    return con


def prompt_text(d):
    """The user's own words in a transcript record, or None."""
    if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
        return None
    msg = d.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(b["text"] for b in content
                        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    else:
        return None
    text = TAG_RE.sub(" ", text).strip()
    if not text or text.startswith(SKIP_PREFIXES):
        return None
    m = CMD_RE.search(text)
    if m and text.lstrip().startswith("<command-"):
        text = ("%s %s" % (m.group(1), (m.group(2) or "").strip())).strip()
    return " ".join(text.split())


def read_transcript(path):
    """[(ts_ms, kind, value)]: ("prompt", text), ("hint", project), ("cwd", cwd),
    ("branch", branch). Unreadable lines are skipped."""
    out = []
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            if '"timestamp"' not in line:
                continue
            try:
                d = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(d, dict):
                continue
            try:
                ts = L.parse_ts(d.get("timestamp")) if isinstance(d.get("timestamp"), str) else None
                p = prompt_text(d) if ts is not None else None
            except Exception:  # an odd record never stops a run
                continue
            if ts is None:
                continue
            if p:
                out.append((ts, "prompt", p))
                if isinstance(d.get("cwd"), str) and d["cwd"]:
                    out.append((ts, "cwd", d["cwd"]))
                if isinstance(d.get("gitBranch"), str) and d["gitBranch"]:
                    out.append((ts, "branch", d["gitBranch"]))
            elif d.get("type") == "assistant":
                msg = d.get("message")
                if isinstance(msg, dict):
                    for _tid, proj in L.hints_of(msg.get("content")):
                        out.append((ts, "hint", proj))
    return out


def transcript_files(con):
    """session id -> (main transcript paths, subagent transcript paths)."""
    files = {}
    for (path,) in con.execute("SELECT path FROM files"):
        if not path.endswith(".jsonl") or not os.path.exists(path):
            continue
        parts = path.split("/")
        if "subagents" in parts:
            i = parts.index("subagents")
            sid = parts[i - 1] if i > 0 else None
            if sid:
                files.setdefault(sid, ([], []))[1].append(path)
        else:
            sid = parts[-1][:-6]
            files.setdefault(sid, ([], []))[0].append(path)
    return files


def session_overview(con):
    """session id -> {usd, first, last, calls, title}."""
    out = {}
    for sid, usd, first, last, n in con.execute(
            "SELECT session_id, SUM(usd), MIN(ts), MAX(ts), COUNT(*) FROM calls "
            "WHERE session_id IS NOT NULL GROUP BY session_id"):
        out[sid] = {"usd": usd or 0.0, "first": first, "last": last, "calls": n, "title": None}
    for sid, ct, at, fp in con.execute("SELECT session_id, custom_title, ai_title, first_prompt FROM sessions"):
        if sid in out:
            out[sid]["title"] = ct or at or fp
    return out


def session_calls(con, sid):
    return con.execute("SELECT ts, usd, cwd, branch FROM calls WHERE session_id = ? ORDER BY ts",
                       (sid,)).fetchall()


def build_turns(events, calls, fallback_title=None):
    """Turns of one session: a prompt and the work until the next prompt.

    events: read_transcript() records of every file of the session; calls: (ts, usd, cwd,
    branch) from the database. Returns [{"ts", "text", "usd", "hints", "cwd", "branch"}],
    oldest first, before compaction."""
    prompts = {}
    for ts, kind, val in events:
        if kind == "prompt":
            prompts.setdefault((ts, val), None)  # a backup copy repeats the same records
    starts = sorted(prompts)
    if not starts:
        first = calls[0][0] if calls else 0
        starts = [(first, fallback_title or "(no prompt recorded)")]
    turns = [{"ts": ts, "text": text, "usd": 0.0, "hints": {}, "cwd": {}, "branch": {}}
             for ts, text in starts]
    keys = [t["ts"] for t in turns]

    def at(ts):
        import bisect
        return max(0, bisect.bisect_right(keys, ts) - 1)

    for ts, usd, cwd, branch in calls:
        t = turns[at(ts)]
        t["usd"] += usd or 0.0
        if cwd:
            t["cwd"][cwd] = t["cwd"].get(cwd, 0) + 1
        if branch:
            t["branch"][branch] = t["branch"].get(branch, 0) + 1
    for ts, kind, val in events:
        if kind == "hint":
            h = turns[at(ts)]["hints"]
            h[val] = h.get(val, 0) + 1
    return turns


def _merge(a, b):
    a["text"] = (a["text"] + " / " + b["text"]) if b["text"] else a["text"]
    a["usd"] += b["usd"]
    for k in ("hints", "cwd", "branch"):
        for name, n in b[k].items():
            a[k][name] = a[k].get(name, 0) + n
    a.setdefault("n", 1)
    a["n"] += b.get("n", 1)
    return a


def compact(turns, max_turns=MAX_TURNS):
    """Short follow-ups join the turn before; then the cheapest neighbours merge until the
    session fits in max_turns. Each turn keeps the ts of its first prompt."""
    out = []
    for t in turns:
        t = dict(t, hints=dict(t["hints"]), cwd=dict(t["cwd"]), branch=dict(t["branch"]))
        if out and len(t["text"]) < SHORT_PROMPT:
            _merge(out[-1], t)
        else:
            out.append(t)
    while len(out) > max_turns:
        i = min(range(len(out) - 1), key=lambda j: out[j]["usd"] + out[j + 1]["usd"])
        _merge(out[i], out.pop(i + 1))
    return out


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")


def _top(counts, n=3):
    """The n biggest keys; session ids (scratchpad folders) say nothing about the project."""
    keys = [k for k, _v in sorted(counts.items(), key=lambda kv: -kv[1]) if not UUID_RE.match(k)]
    return keys[:n]


def render_dossier(sid, info, turns, start_index, previous=None):
    """The text the model reads for one session."""
    long_session = len(turns) > 40
    limit = 160 if long_session else 320
    first = dt.datetime.fromtimestamp(info["first"] / 1000).strftime("%Y-%m-%d")
    head = "## session %s  (%s, %.2f USD, %d turns)" % (sid, first, info["usd"], len(turns))
    lines = [head]
    if info.get("title"):
        lines.append("title: %s" % " ".join(str(info["title"]).split())[:160])
    if previous:
        lines.append("already classified up to here; the last segment was: %s" % json.dumps(previous, ensure_ascii=False))
    folders = {}
    for t in turns:
        for c, n in t["cwd"].items():
            folders[c] = folders.get(c, 0) + n
    if folders:
        lines.append("folders: %s" % ", ".join("%s (%s)" % (L.project_of(c), _short_path(c)) for c in _top(folders, 3)))
    for i, t in enumerate(turns):
        extra = []
        if t["hints"]:
            extra.append("touched: " + ", ".join(_top(t["hints"], 3)))
        br = [b for b in _top(t["branch"], 2) if b not in ("main", "master", "HEAD")]
        if br:
            extra.append("branch: " + ", ".join(br))
        text = t["text"]
        if len(text) > limit:
            text = text[:limit] + "..."
        lines.append("[%d] %s %.2f$ | %s%s" % (
            start_index + i, dt.datetime.fromtimestamp(t["ts"] / 1000).strftime("%m-%d %H:%M"),
            t["usd"], text, ("  {" + "; ".join(extra) + "}") if extra else ""))
    return "\n".join(lines)


def _short_path(p):
    return p.replace(L.HOME, "~") if p else p


# ---------------------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------------------

SYSTEM = """You classify the AI coding sessions of one engineer for a cost dashboard that
draws one bubble per client or project. You read session dossiers and return JSON only.

Each dossier lists the session's turns: [index] date cost | the user's prompt {repositories
the tools touched; git branch}. Split every session into segments, one per piece of work:
a new segment starts only where the work really moves to another client, project or task.
Follow-ups ("continúa", "sí", "lanza otra ronda", subagent and loop resumes) stay in the
current segment. A short session is usually one segment; do not split for small detours.

For each segment return:
- from_turn: the index of its first turn (the first segment starts at the session's first
  index).
- kind: "client" (work done for a client company: their tickets, repos, cloud projects,
  infrastructure, reports, estimates, meetings or documents), "internal" (work for the
  engineer's employer that is not for one client: time booking, internal tickets, team
  management, interns, internal IT), "tool" (building or fixing one of the engineer's own
  tools, apps, dashboards, agent kits or automations), "personal" (anything private).
  Using an own tool to work on a client ticket is client work for that client. Building
  the tool is tool work, even when the tool serves a client.
- client: for client and internal segments, the exact client name from the taxonomy (the
  employer's name for internal ones); null for tool and personal.
- project: a stable short name. For clients: the client's product or platform (taxonomy
  and catalog first), never a ticket key or a repository path; use the client name itself
  when no project fits. For tools: the tool's name as the catalog has it. Reuse a catalog
  name exactly whenever it fits; invent one only for something really new.
- task: what was done, at most 70 characters, in the language of the prompts, starting
  with the ticket key when there is one.
- ticket: the ticket key worked on (for example ABC123-45), else null.

Mind the signals: ticket prefixes map to clients (taxonomy boards); repositories touched
and folders tell the project; an agent workspace folder alone says nothing. Meeting
minutes and transcripts belong to the client or topic of the meeting.

Also return new_projects: every project name you used that is not in the catalog, with
kind, client and a one-line description."""

SCHEMA = {
    "type": "object",
    "properties": {
        "sessions": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "segments": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "from_turn": {"type": "integer"},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "client": {"type": ["string", "null"]},
                        "project": {"type": "string"},
                        "task": {"type": "string"},
                        "ticket": {"type": ["string", "null"]},
                    },
                    "required": ["from_turn", "kind", "client", "project", "task", "ticket"]}},
            },
            "required": ["id", "segments"]}},
        "new_projects": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                           "client": {"type": ["string", "null"]}, "about": {"type": "string"}},
            "required": ["name", "kind", "client", "about"]}},
    },
    "required": ["sessions", "new_projects"],
}


def taxonomy_text(tax, boards):
    lines = ["# Taxonomy"]
    if tax.get("owner"):
        lines.append("The engineer: %s" % tax["owner"])
    if tax.get("employer"):
        lines.append("Employer (client name for internal work): %s" % tax["employer"])
    for c in tax.get("clients", []):
        if isinstance(c, dict) and c.get("name"):
            lines.append("- client %s: %s" % (c["name"], c.get("about", "")))
    if tax.get("tools"):
        lines.append("Own tools: %s" % tax["tools"])
    if tax.get("notes"):
        lines.append("Notes: %s" % tax["notes"])
    if boards:
        lines.append("Ticket boards: " + ", ".join("%s = %s" % kv for kv in sorted(boards.items())))
    return "\n".join(lines)


def catalog_text(projects):
    if not projects:
        return "# Catalog\n(empty: create projects as needed)"
    lines = ["# Catalog (name | kind | client | about)"]
    for name, p in sorted(projects.items(), key=lambda kv: (str(kv[1].get("client") or "~"), kv[0])):
        lines.append("- %s | %s | %s | %s" % (name, p.get("kind"), p.get("client") or "-", (p.get("about") or "")[:120]))
    return "\n".join(lines)


def call_model(prompt, model, claude="claude", schema=SCHEMA, system=SYSTEM, timeout=900):
    """One `claude -p` call with no tools, settings or MCP servers. Returns the structured
    output (a dict) and the cost the CLI reported."""
    # Its own folder, so its sessions are told apart (and cost "automation") in Latidos.
    wd = workdir()
    os.makedirs(wd, exist_ok=True)
    cmd = [claude, "-p", "--model", model, "--output-format", "json", "--tools", "",
           "--setting-sources", "", "--strict-mcp-config", "--system-prompt", system,
           "--json-schema", json.dumps(schema)]
    last = None
    for attempt in range(3):
        try:
            res = subprocess.run(cmd, input=MARKER + "\n" + prompt, capture_output=True, text=True,
                                 cwd=wd, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = "timed out"
            continue
        try:
            data = json.loads(res.stdout)
        except ValueError:
            last = (res.stderr or res.stdout or "no output").strip()[:300]
            time.sleep(5 * (attempt + 1))
            continue
        if data.get("is_error") or not isinstance(data.get("structured_output"), dict):
            last = str(data.get("result") or data.get("subtype") or "no structured output")[:300]
            time.sleep(5 * (attempt + 1))
            continue
        return data["structured_output"], float(data.get("total_cost_usd") or 0)
    raise RuntimeError("model call failed: %s" % last)


# ---------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------

def clean_segment(seg, start_ts, taxonomy):
    kind = seg.get("kind") if seg.get("kind") in KINDS else "tool"
    client = seg.get("client") if kind in ("client", "internal") else None
    if kind == "internal" and not client:
        client = taxonomy.get("employer") or None
    project = " ".join(str(seg.get("project") or "").split())[:60] or (client or "?")
    ticket = seg.get("ticket")
    ticket = L.ticket_in(ticket) if isinstance(ticket, str) else None
    task = " ".join(str(seg.get("task") or "").split())[:90] or project
    return {"from": int(start_ts), "kind": kind, "client": client, "project": project,
            "task": task, "ticket": ticket}


def workdir():
    """The folder `claude -p` runs in: its sessions are this tool's own and never classified."""
    return os.path.expanduser(L.env("CLASSIFY_WORKDIR") or os.path.join(L._state_base(), "latidos", "classifier"))


def own_sessions(con):
    """Sessions this tool started (run from its work folder)."""
    w = workdir().rstrip("/")
    return {sid for (sid,) in con.execute(
        "SELECT DISTINCT session_id FROM calls WHERE cwd = ? OR cwd LIKE ? ESCAPE '\\'",
        (w, w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"))}


def pending(con, state, only=None, now=None):
    """[(sid, info, turns, start_index, previous segment, done record, upto, seen_ts)] of
    sessions with unclassified settled turns, biggest cost first.

    seen_ts is the time of the last raw prompt sent (raw, before compaction, so a merged
    follow-up is never sent twice); upto is the last call those turns cover: calls of a
    turn still inside the settle window keep the session pending for the next run."""
    now = now or int(time.time() * 1000)
    over = session_overview(con)
    files = transcript_files(con)
    own = own_sessions(con)
    out = []
    for sid, info in over.items():
        if (only and sid not in only) or sid in own:
            continue
        done = state["sessions"].get(sid) or {}
        mains, subs = files.get(sid, ([], []))
        if done and info["last"] <= done.get("upto", 0):
            continue  # nothing new since the last run
        events = []
        for p in mains + subs:
            events.extend(r for r in read_transcript(p) if r[1] != "prompt" or p in mains)
        calls = session_calls(con, sid)
        raw = build_turns(events, calls, info.get("title"))
        seen = done.get("seen_ts") if done else None
        if seen is None and done:
            seen = (done.get("turn_ts") or [0])[-1]  # records written by release 87
        fresh = [t for t in raw if seen is None or t["ts"] > seen]
        settled = [t for t in fresh if t["ts"] < now - SETTLE_MS]
        if not done and not settled and fresh:
            settled = fresh[:1]  # a brand-new session gets its first turn classified at once
        unsettled = fresh[len(settled):]
        if unsettled:
            bound = unsettled[0]["ts"]
            upto = max([c[0] for c in calls if c[0] < bound] or [done.get("upto", 0) if done else 0])
        else:
            upto = info["last"]
        if not settled:
            if done:
                # Calls arrived but no new settled prompt: the last segment simply grew.
                state["sessions"][sid]["upto"] = max(done.get("upto", 0), upto)
            continue
        prev = done["segments"][-1] if done and done.get("segments") else None
        start = done.get("turns", len(done.get("turn_ts", []))) if done else 0
        out.append((sid, info, compact(settled), start, prev, done or None, upto, settled[-1]["ts"]))
    out.sort(key=lambda x: -x[1]["usd"])
    return out


def batches(items, max_chars=BATCH_CHARS, max_sessions=BATCH_SESSIONS):
    batch, size = [], 0
    for it in items:
        text = it[-1]
        if batch and (size + len(text) > max_chars or len(batch) >= max_sessions):
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += len(text)
    if batch:
        yield batch


def run(args):
    tax = load_taxonomy()
    boards = L._load_boards()
    lock = take_lock()
    if lock is None:
        return 0
    state = load_state()
    con = open_db()
    only = set(args.session) if args.session else None
    todo = pending(con, state, only)
    if args.limit:
        todo = todo[:args.limit]
    items = []
    for sid, info, turns, start, prev, done, upto, seen in todo:
        items.append((sid, info, turns, start, prev, done, upto, seen,
                      render_dossier(sid, info, turns, start, prev)))
    log("%d sessions to classify (%d turns)" % (len(items), sum(len(i[2]) for i in items)))
    if args.dry_run:
        for it in items[:args.show]:
            print(it[-1])
            print()
        return 0
    if not items:
        save_state(state)
        return 0
    head = taxonomy_text(tax, boards)
    mutex = threading.Lock()
    spent = [0.0]
    done_n = [0]

    def work(batch):
        with mutex:
            cat = catalog_text(state["projects"])
        prompt = "%s\n\n%s\n\n# Sessions\n\n%s\n\nReturn the segments of every session above (%d)." % (
            head, cat, "\n\n".join(b[-1] for b in batch), len(batch))
        res, cost = call_model(prompt, args.model, args.claude)
        by_id = {s.get("id"): s for s in res.get("sessions", []) if isinstance(s, dict)}
        with mutex:
            spent[0] += cost
            for p in res.get("new_projects", []):
                if isinstance(p, dict) and p.get("name") and p["name"] not in state["projects"]:
                    state["projects"][p["name"]] = {
                        "kind": p.get("kind") if p.get("kind") in KINDS else "tool",
                        "client": p.get("client"), "about": (p.get("about") or "")[:200]}
            for sid, info, turns, start, prev, done, upto, seen, _text in batch:
                got = by_id.get(sid)
                if not got or not got.get("segments"):
                    log("no answer for %s, retried next run" % sid)
                    continue
                segs = []
                for seg in sorted(got["segments"], key=lambda s: s.get("from_turn", 0) if isinstance(s, dict) else 0):
                    if not isinstance(seg, dict):
                        continue
                    i = min(max(int(seg.get("from_turn") or start) - start, 0), len(turns) - 1)
                    ts = turns[i]["ts"] if segs or done else min(turns[i]["ts"], info["first"])
                    s = clean_segment(seg, ts, tax)
                    if segs and segs[-1]["from"] >= s["from"]:
                        continue
                    if segs and _same(segs[-1], s):
                        continue
                    segs.append(s)
                old = list(done["segments"]) if done else []
                if old and segs and _same(old[-1], segs[0]):
                    segs = segs[1:]
                rec = {"segments": old + segs, "seen_ts": seen, "turns": start + len(turns),
                       "upto": upto, "usd": round(info["usd"], 4)}
                state["sessions"][sid] = rec
                for s in segs:
                    p = state["projects"].setdefault(s["project"], {"kind": s["kind"], "client": s["client"], "about": ""})
                    p.setdefault("kind", s["kind"])
            done_n[0] += len(batch)
            save_state(state)
            log("%d/%d sessions, %.2f USD so far" % (done_n[0], len(items), spent[0]))

    bl = list(batches(items))
    errors = 0
    if args.jobs <= 1:
        for b in bl:
            try:
                work(b)
            except Exception as exc:
                errors += 1
                log("batch failed: %s" % exc)
    else:
        # The biggest sessions go first and alone, so the catalog forms from the main work
        # before the parallel batches read it.
        first, rest = bl[:1], bl[1:]
        for b in first:
            try:
                work(b)
            except Exception as exc:
                errors += 1
                log("batch failed: %s" % exc)
        with cf.ThreadPoolExecutor(args.jobs) as ex:
            for fut in cf.as_completed([ex.submit(work, b) for b in rest]):
                try:
                    fut.result()
                except Exception as exc:
                    errors += 1
                    log("batch failed: %s" % exc)
    learn_folders(con, state)
    save_state(state)
    log("done: %d sessions, %.2f USD, %d failed batches" % (done_n[0], spent[0], errors))
    return 1 if errors and not done_n[0] else 0


def take_lock():
    """The run lock (one writer of classified.json at a time), or None when held."""
    lock_path = out_path() + ".lock"
    os.makedirs(os.path.dirname(os.path.realpath(lock_path)), exist_ok=True)
    lock = open(lock_path, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        log("another run is in progress")
        return None
    return lock


def _same(a, b):
    return (a["kind"], a["client"], a["project"], a.get("ticket"), a["task"]) == \
        (b["kind"], b["client"], b["project"], b.get("ticket"), b["task"])


def learn_folders(con, state, share=0.7):
    """Folder (repo) -> the attribution most of its classified cost got, when one clearly
    dominates: Latidos uses it for sessions the model has not seen yet. Counted call by
    call, so a session that changed segment inside a folder credits each segment."""
    import bisect
    tally = {}
    recs = {}
    for sid, rec in state["sessions"].items():
        segs = rec.get("segments") or []
        if segs:
            recs[sid] = ([s["from"] for s in segs], segs)
    repos = {}
    for sid, cwd, ts, usd in con.execute(
            "SELECT session_id, cwd, ts, usd FROM calls WHERE session_id IS NOT NULL AND cwd IS NOT NULL"):
        rec = recs.get(sid)
        if not rec:
            continue
        repo = repos.get(cwd)
        if repo is None:
            repo = repos[cwd] = L.project_of(cwd)
        if repo in L.GENERIC_PROJECTS or L.is_group(repo):
            continue
        seg = rec[1][max(0, bisect.bisect_right(rec[0], ts) - 1)]
        key = (seg["kind"], seg["client"], seg["project"])
        t = tally.setdefault(repo, {})
        t[key] = t.get(key, 0.0) + (usd or 0.0)
    folders = {}
    for repo, counts in tally.items():
        total = sum(counts.values())
        key, usd = max(counts.items(), key=lambda kv: kv[1])
        if total > 0 and usd >= share * total:
            folders[repo] = {"kind": key[0], "client": key[1], "project": key[2]}
    state["folders"] = folders


def segment_at(segments, ts):
    cur = segments[0]
    for s in segments:
        if s["from"] <= ts:
            cur = s
        else:
            break
    return cur


# ---------------------------------------------------------------------------------------
# Consolidation: one call over the catalog merges near-duplicate names
# ---------------------------------------------------------------------------------------

CONSOLIDATE_SYSTEM = """You tidy the project catalog of a cost dashboard. Projects were
named by several independent passes, so some are duplicates or near-duplicates (same
product under two names, a repository name next to its product name, a ticket used as a
project). Return renames that merge each duplicate into the best existing name, and fix
kind or client where a project is clearly misfiled. Keep distinct things distinct; do not
merge two real projects of the same client only because they are small (the dashboard
already groups small projects of a client together). Return JSON only."""

CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "renames": {"type": "array", "items": {
            "type": "object",
            "properties": {"from": {"type": "string"}, "to": {"type": "string"}},
            "required": ["from", "to"]}},
        "fixes": {"type": "array", "items": {
            "type": "object",
            "properties": {"project": {"type": "string"}, "kind": {"type": "string", "enum": list(KINDS)},
                           "client": {"type": ["string", "null"]}},
            "required": ["project", "kind", "client"]}},
    },
    "required": ["renames", "fixes"],
}


def consolidate(args):
    lock = take_lock()
    if lock is None:
        return 1
    state = load_state()
    tax = load_taxonomy()
    usage = {}
    samples = {}
    for rec in state["sessions"].values():
        segs = rec.get("segments") or []
        for s in segs:
            usage.setdefault(s["project"], 0)
            usage[s["project"]] += 1
            samples.setdefault(s["project"], [])
            if len(samples[s["project"]]) < 4:
                samples[s["project"]].append(s["task"])
    lines = []
    for name in sorted(usage, key=lambda n: -usage[n]):
        p = state["projects"].get(name, {})
        lines.append("- %s | %s | %s | %d segments | %s | e.g. %s" % (
            name, p.get("kind"), p.get("client") or "-", usage[name], (p.get("about") or "")[:100],
            "; ".join(samples[name])))
    prompt = "%s\n\n# Projects\n%s" % (taxonomy_text(tax, L._load_boards()), "\n".join(lines))
    res, cost = call_model(prompt, args.model, args.claude, CONSOLIDATE_SCHEMA, CONSOLIDATE_SYSTEM)
    ren = {r["from"]: r["to"] for r in res.get("renames", [])
           if isinstance(r, dict) and r.get("from") and r.get("to") and r["from"] != r["to"]}
    # Follow chains (a -> b -> c) and drop cycles.
    for k in list(ren):
        seen, v = {k}, ren[k]
        while v in ren and v not in seen:
            seen.add(v)
            v = ren[v]
        ren[k] = v
    ren = {k: v for k, v in ren.items() if k != v}
    fixes = {f["project"]: f for f in res.get("fixes", []) if isinstance(f, dict) and f.get("project")}
    apply_consolidation(state, ren, fixes)
    learn_folders(open_db(), state)
    save_state(state)
    for k, v in sorted(ren.items()):
        print("rename  %s -> %s" % (k, v))
    for k, f in sorted(fixes.items()):
        print("fix     %s: %s / %s" % (k, f["kind"], f.get("client")))
    log("consolidated (%.2f USD)" % cost)
    return 0


def apply_consolidation(state, renames, fixes):
    for rec in state["sessions"].values():
        for s in rec.get("segments") or []:
            if s["project"] in renames:
                s["project"] = renames[s["project"]]
            f = fixes.get(s["project"])
            if f and f.get("kind") in KINDS:
                s["kind"] = f["kind"]
                s["client"] = f.get("client") if f["kind"] in ("client", "internal") else None
    for old, new in renames.items():
        p = state["projects"].pop(old, None)
        if p and new not in state["projects"]:
            state["projects"][new] = p
    for name, f in fixes.items():
        if name in state["projects"] and f.get("kind") in KINDS:
            state["projects"][name]["kind"] = f["kind"]
            state["projects"][name]["client"] = f.get("client") if f["kind"] in ("client", "internal") else None


# ---------------------------------------------------------------------------------------
# Status and LaunchAgent
# ---------------------------------------------------------------------------------------

def status(args):
    state = load_state()
    con = open_db()
    over = session_overview(con)
    done = [s for s in over if s in state["sessions"]]
    usd_all = sum(i["usd"] for i in over.values())
    usd_done = sum(over[s]["usd"] for s in done)
    print("classified %d of %d sessions (%.0f%% of the cost)" % (
        len(done), len(over), 100 * usd_done / usd_all if usd_all else 0))
    print("projects in the catalog: %d; folders learned: %d" % (len(state["projects"]), len(state["folders"])))
    if state.get("updated"):
        print("last write: %s" % dt.datetime.fromtimestamp(state["updated"] / 1000).isoformat(timespec="seconds"))
    return 0


def agent_label():
    return L.LABEL + ".classify"


def install(args):
    label = agent_label()
    target = os.path.join(L.AGENTS_DIR, label + ".plist")
    log_path = os.path.join(L.HOME, "Library", "Logs", "latidos-classify.log")
    found = _which(args.claude) or args.claude
    # The CLI's own folder and its link's folder (an npm install needs node from there).
    dirs = []
    for d in (os.path.dirname(found), os.path.dirname(os.path.realpath(found)), "/opt/homebrew/bin", "/usr/local/bin"):
        if d and d not in dirs:
            dirs.append(d)
    claude_dir = ":".join(dirs)
    data = plistlib.dumps({
        "Label": label,
        "ProgramArguments": [L.python_bin(), os.path.abspath(__file__), "run", "--model", args.model,
                             "--claude", _which(args.claude) or args.claude],
        "StartInterval": 1800,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "Nice": 10,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": {"PATH": "%s:/usr/bin:/bin:/usr/sbin:/sbin" % claude_dir, "HOME": L.HOME},
    })
    domain = "gui/%d" % os.getuid()
    L._bootout(domain, label)
    with open(target, "wb") as fh:
        fh.write(data)
    res = subprocess.run(["launchctl", "bootstrap", domain, target], capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write((res.stderr.strip() or "launchctl bootstrap failed") + "\n")
        return 1
    print("installed %s (every 30 minutes, log %s)" % (target, log_path))
    return 0


def uninstall(args):
    label = agent_label()
    target = os.path.join(L.AGENTS_DIR, label + ".plist")
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), label)], capture_output=True)
    if os.path.exists(target):
        os.unlink(target)
    print("uninstalled")
    return 0


def _which(name):
    if os.sep in name:
        return name
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="classify.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("run", "consolidate", "status", "install", "uninstall"))
    ap.add_argument("--model", default=L.env("CLASSIFY_MODEL", "claude-opus-5-5"))
    ap.add_argument("--claude", default=L.env("CLASSIFY_CLAUDE", "claude"), help="the Claude Code CLI")
    ap.add_argument("--jobs", type=int, default=3, help="model calls in parallel")
    ap.add_argument("--limit", type=int, default=0, help="at most this many sessions")
    ap.add_argument("--session", action="append", help="only this session id (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="print the dossiers, call nothing")
    ap.add_argument("--show", type=int, default=3, help="dossiers printed by --dry-run")
    args = ap.parse_args(argv)
    return {"run": run, "consolidate": consolidate, "status": status,
            "install": install, "uninstall": uninstall}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
