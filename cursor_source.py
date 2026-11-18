"""Cursor activity for Latidos, from Cursor's own local files (read-only).

Sources (local data only, decided 2026-11-12):
- ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb, table cursorDiskKV:
  `composerData:<id>` (one conversation: name, model, mode, dates, parent, legacy
  usageData) and `bubbleId:<id>:<bubble>` (messages: type 1 = user turn, createdAt,
  modelInfo, tokenCount). Cursor stopped writing token counts and cost to disk around
  Feb 2026, so tokens and cost exist only for older conversations.
- ~/.cursor/ai-tracking/ai-code-tracking.db, ai_code_hashes(conversationId, fileName):
  the files a conversation edited, which give its repository.
- ~/.cursor/chats/<hash>/<agentId>/meta.json: cursor-agent CLI chats (cwd, dates).

Every database is opened with mode=ro; nothing of Cursor's is ever written.

Server usage (the user's own Cursor session, approved 2026-11-12): Cursor's dashboard
API (`/api/dashboard/get-filtered-usage-events`) returns, per request, the model, tokens,
cost and conversation id, for about the last year. The session token is read from
state.vscdb (`cursorAuth/accessToken`) at each pass, sent only to cursor.com in the cookie
the dashboard itself uses, and never stored, logged or shown. Cost and tokens come from
this API; titles and projects from the local files (joined by conversation id); days
before the API's first event come from the local files (legacy cost only).
"""

import base64

import datetime as dt
import glob
import re
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

HOME = os.path.expanduser("~")


def _env(name, default=None):
    """LATIDOS_<name>, else the deprecated LATIDO_<name> (old name), else default."""
    for prefix in ("LATIDOS_", "LATIDO_"):
        raw = os.environ.get(prefix + name)
        if raw is not None:
            return raw
    return default


STATE_DB = _env(
    "CURSOR_DB",
    os.path.join(HOME, "Library", "Application Support", "Cursor", "User", "globalStorage", "state.vscdb"))
TRACKING_DB = _env(
    "CURSOR_TRACKING", os.path.join(HOME, ".cursor", "ai-tracking", "ai-code-tracking.db"))
CHATS_DIR = _env("CURSOR_CHATS", os.path.join(HOME, ".cursor", "chats"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS cursor_convs (
  id TEXT PRIMARY KEY,
  name TEXT, model TEXT, mode TEXT, source TEXT,
  created INTEGER, updated INTEGER,
  parent TEXT, root TEXT, project TEXT,
  turns INTEGER, in_tok INTEGER, out_tok INTEGER, cents REAL,
  scanned INTEGER
);
CREATE TABLE IF NOT EXISTS cursor_turns (
  conv_id TEXT, day INTEGER, turns INTEGER,
  PRIMARY KEY (conv_id, day)
);
CREATE INDEX IF NOT EXISTS cursor_turns_day ON cursor_turns(day);
CREATE TABLE IF NOT EXISTS cursor_events (
  id TEXT PRIMARY KEY,
  ts INTEGER, model TEXT, kind TEXT, conv_id TEXT,
  in_tok INTEGER, out_tok INTEGER, cread INTEGER, cents REAL, headless INTEGER,
  list_cents REAL
);
CREATE INDEX IF NOT EXISTS cursor_events_ts ON cursor_events(ts);
"""

API = "https://cursor.com"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the session cookie to wherever Location points: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())
API_PAGE = 500
API_HISTORY_DAYS = 400
API_OVERLAP_MS = 2 * 86400000
API_MAX_PAGES = 200


def session_expiry(state_db=STATE_DB):
    """When Cursor's sign-in expires (epoch seconds), from the `exp` of its access token, or
    None. Only that number leaves this function, never the token (L-162)."""
    if not os.path.exists(state_db):
        return None
    try:
        src = ro(state_db)
        try:
            row = src.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
        finally:
            src.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    tok = row[0] if isinstance(row[0], str) else row[0].decode()
    try:
        exp = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "==")).get("exp")
    except (ValueError, IndexError, AttributeError):
        return None
    return int(exp) if isinstance(exp, (int, float)) and exp > 0 else None


class CursorApi:
    """Incremental reader of the dashboard usage events (newest page first)."""

    def __init__(self, con, lock, state_db=STATE_DB, opener=None):
        self.con = con
        self.lock = lock
        self.state_db = state_db
        self.opener = opener or _OPENER.open
        self.progress = {"enabled": _env("CURSOR_API", "1") != "0", "events": 0,
                         "since": None, "last_scan": None, "error": None, "error_code": None,
                         "error_detail": None}
        with lock:
            cols = [r[1] for r in con.execute("PRAGMA table_info(cursor_events)")]
            if cols and "list_cents" not in cols:
                # v1 stored the token list price as the cost; refetch with what Cursor charges.
                con.execute("DROP TABLE cursor_events")
            con.executescript(SCHEMA)
            con.commit()

    def _fail(self, code, message, detail=None):
        """Report a problem: a stable code the page words in its language (web/i18n.js,
        cursor.err.<code>), an English message for logs, and a detail (HTTP status or
        exception type) that the message carries."""
        self.progress.update(error=message, error_code=code, error_detail=detail)

    def _session(self):
        """(cookie, None) or (None, (code, reason)). The token never leaves this function
        except inside the cookie handed to the request."""
        if not os.path.exists(self.state_db):
            return None, ("not_installed", "Cursor is not installed")
        try:
            src = ro(self.state_db)
            row = src.execute("SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'").fetchone()
            src.close()
        except sqlite3.Error:
            return None, ("session_unreadable", "could not read Cursor's session")
        if not row or not row[0]:
            return None, ("no_session", "Cursor is not signed in")
        tok = row[0] if isinstance(row[0], str) else row[0].decode()
        try:
            payload = json.loads(base64.urlsafe_b64decode(tok.split(".")[1] + "=="))
        except (ValueError, IndexError):
            return None, ("session_bad", "Cursor's session is unreadable")
        if payload.get("exp") and payload["exp"] < time.time():
            return None, ("session_expired", "Cursor's session has expired (open Cursor)")
        user_id = str(payload.get("sub", "")).split("|")[-1]
        return "WorkosCursorSessionToken=%s%%3A%%3A%s" % (user_id, tok), None

    def _page(self, cookie, start, end, page):
        body = json.dumps({"startDate": str(start), "endDate": str(end), "page": page,
                           "pageSize": API_PAGE}).encode()
        req = urllib.request.Request(API + "/api/dashboard/get-filtered-usage-events", data=body,
                                     method="POST", headers={
                                         "Cookie": cookie, "Origin": API, "Content-Type": "application/json",
                                         "User-Agent": "latidos/1 (local usage panel)"})
        with self.opener(req, timeout=30) as resp:
            return json.loads(resp.read())

    def scan(self, now_ms=None):
        if not self.progress["enabled"]:
            return 0
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        cookie, why = self._session()
        if not cookie:
            self._fail(*why)
            return 0
        # The window starts where the last *complete* pass ended (minus an overlap): a pass
        # that fails half-way, or stops at the page cap, leaves it where it was, so the next
        # pass reads the same window again (rows are idempotent) and nothing older is lost.
        # If a pass hits the page cap (pages come newest first), the next one reads only the
        # older part still unread (`narrow_end`) and, once that is complete, marks the
        # original window (`pending_until`) as done.
        with self.lock:
            meta = dict(self.con.execute(
                "SELECT k, v FROM meta WHERE k IN ('cursor_api_done_until', 'cursor_api_narrow_end', "
                "'cursor_api_pending_until')"))
        done_until = int(meta["cursor_api_done_until"]) if "cursor_api_done_until" in meta else None
        narrow_end = int(meta["cursor_api_narrow_end"]) if "cursor_api_narrow_end" in meta else None
        pending_until = int(meta["cursor_api_pending_until"]) if "cursor_api_pending_until" in meta else None
        start = (done_until - API_OVERLAP_MS) if done_until else now_ms - API_HISTORY_DAYS * 86400000
        end = narrow_end + 1 if narrow_end else now_ms + 60000
        oldest = None
        added = 0
        complete = False
        try:
            page = 1
            while True:
                data = self._page(cookie, start, end, page)
                if not isinstance(data, dict):
                    raise ValueError("unexpected answer")
                events = data.get("usageEventsDisplay") or []
                rows = []
                for e in events:
                    tu = e.get("tokenUsage") or {}
                    try:
                        ts = int(e.get("timestamp"))
                    except (TypeError, ValueError):
                        continue
                    # chargedCents is what Cursor bills (token price plus its own fee; also set
                    # on requests included in the plan); totalCents is the token list price.
                    cents = e.get("chargedCents")
                    if cents is None:
                        cents = tu.get("totalCents") or 0
                    eid = e.get("usageEventId") or "%s:%s:%s" % (ts, e.get("model"), cents)
                    oldest = ts if oldest is None else min(oldest, ts)
                    rows.append((eid, ts, e.get("model"), e.get("kind"), e.get("conversationId") or None,
                                 int(tu.get("inputTokens") or 0), int(tu.get("outputTokens") or 0),
                                 int(tu.get("cacheReadTokens") or 0), float(cents or 0),
                                 1 if e.get("isHeadless") else 0, float(tu.get("totalCents") or 0)))
                with self.lock:
                    before = self.con.total_changes
                    # Only rows that are new or really changed count (the overlap re-reads
                    # two days every pass; those must not look like new data).
                    self.con.executemany(
                        "INSERT INTO cursor_events VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET ts=excluded.ts, model=excluded.model, "
                        "kind=excluded.kind, conv_id=excluded.conv_id, in_tok=excluded.in_tok, "
                        "out_tok=excluded.out_tok, cread=excluded.cread, cents=excluded.cents, "
                        "headless=excluded.headless, list_cents=excluded.list_cents "
                        "WHERE cursor_events.cents IS NOT excluded.cents OR cursor_events.in_tok IS NOT excluded.in_tok "
                        "OR cursor_events.out_tok IS NOT excluded.out_tok OR cursor_events.cread IS NOT excluded.cread "
                        "OR cursor_events.conv_id IS NOT excluded.conv_id OR cursor_events.model IS NOT excluded.model",
                        rows)
                    added += self.con.total_changes - before
                    self.con.commit()
                total = int(data.get("totalUsageEventsCount") or 0)
                if not events or page * API_PAGE >= total:
                    complete = True
                    break
                if page >= API_MAX_PAGES:
                    break  # incomplete: the next pass reads this window again
                page += 1
            with self.lock:
                if complete:
                    self.con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('cursor_api_done_until', ?)",
                                     (str(pending_until if narrow_end else end),))
                    self.con.execute("DELETE FROM meta WHERE k IN ('cursor_api_narrow_end', 'cursor_api_pending_until')")
                elif oldest is not None:
                    self.con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('cursor_api_narrow_end', ?)", (str(oldest),))
                    self.con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('cursor_api_pending_until', ?)",
                                     (str(pending_until or end),))
                self.con.commit()
            if complete:
                self._fail(None, None)
            else:
                self._fail("incomplete", "Cursor read incomplete, retrying")
        except urllib.error.HTTPError as exc:
            auth = exc.code in (401, 403)
            self._fail("http_auth" if auth else "http",
                       "Cursor answered %d" % exc.code + (" (session not valid)" if auth else ""), str(exc.code))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self._fail("offline", "no connection to Cursor (%s)" % type(exc).__name__, type(exc).__name__)
        except Exception as exc:  # anything else: report its type only, keep the loop alive
            self._fail("failed", "error reading Cursor usage (%s)" % type(exc).__name__, type(exc).__name__)
        finally:
            self.progress["last_scan"] = now_ms
            with self.lock:
                n, first = self.con.execute("SELECT COUNT(*), MIN(ts) FROM cursor_events").fetchone()
                self.progress.update(events=n, since=first)
                if added:
                    self.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
                    self.con.commit()
        return added



def ro(path):
    return sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(path), uri=True, timeout=5)


def iso_ms(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(dt.datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def local_day(ms):
    d = dt.datetime.fromtimestamp(ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


FOREIGN_HOME = re.compile(r"^/(?:home|Users)/[^/]+(?=/)")


def local_path(uri_or_path):
    """file:// URI or path -> a path under this Mac's home (older conversations were made
    on a Linux machine whose home was /home/<user>)."""
    p = uri_or_path or ""
    if isinstance(p, dict):
        p = p.get("fsPath") or p.get("path") or p.get("external") or ""
    if not isinstance(p, str):
        return None
    if p.startswith("file://"):
        p = urllib.parse.unquote(p[7:])
    if not p.startswith("/"):
        return None
    return FOREIGN_HOME.sub(HOME, p, count=1)


def file_refs(ofs, created, selections):
    """Paths a conversation touched: edited files (originalFileStates), created files and
    the files attached as context."""
    out = []
    for raw in (ofs, created, selections):
        try:
            v = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            continue
        if isinstance(v, dict):
            out.extend(local_path(k) for k in v.keys())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    item = item.get("uri") or item.get("path") or item.get("fsPath")
                out.append(local_path(item))
    return [p for p in out if p]


def legacy_cents(usage_json):
    """usageData: {"<model>": {"costInCents": 4, "amount": 4}, ...} (filled until ~2025-10)."""
    try:
        data = json.loads(usage_json) if isinstance(usage_json, str) else (usage_json or {})
    except ValueError:
        return 0.0
    total = 0.0
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                try:
                    total += float(v.get("costInCents") or 0)
                except (TypeError, ValueError):
                    pass
    return total


class CursorIngestor:
    """Incremental: a conversation is re-read only when its lastUpdatedAt moved."""

    def __init__(self, con, lock, project_of, state_db=STATE_DB, tracking_db=TRACKING_DB,
                 chats_dir=CHATS_DIR):
        self.con = con
        self.lock = lock
        self.project_of = project_of
        self.state_db = state_db
        self.tracking_db = tracking_db
        self.chats_dir = chats_dir
        self.progress = {"available": os.path.exists(state_db), "convs": 0, "last_scan": None,
                         "running": False, "error": None}
        with lock:
            con.executescript(SCHEMA)
            con.commit()

    # -- project mapping ---------------------------------------------------------------
    def tracked_projects(self):
        """conversationId -> the repository its edits mostly touched."""
        if not os.path.exists(self.tracking_db):
            return {}
        counts = {}
        try:
            src = ro(self.tracking_db)
            for conv, fname, n in src.execute(
                    "SELECT conversationId, fileName, COUNT(*) FROM ai_code_hashes "
                    "WHERE conversationId IS NOT NULL AND fileName IS NOT NULL "
                    "GROUP BY conversationId, fileName"):
                proj = self.project_of(os.path.dirname(fname)) if fname.startswith("/") else None
                if proj:
                    c = counts.setdefault(conv, {})
                    c[proj] = c.get(proj, 0) + n
            src.close()
        except sqlite3.Error:
            return {}
        return {k: max(v.items(), key=lambda kv: kv[1])[0] for k, v in counts.items()}

    # -- scan ------------------------------------------------------------------------
    def signature(self):
        """What a pass would read, by stat only: (mtime, size) of state.vscdb, its WAL
        and the newest CLI chat. Equal signatures mean nothing new to read."""
        sig = []
        for path in (self.state_db, self.state_db + "-wal"):
            try:
                st = os.stat(path)
                sig.append((st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append(None)
        newest = 0
        count = 0
        for meta_path in glob.glob(os.path.join(self.chats_dir, "*", "*", "meta.json")):
            try:
                newest = max(newest, os.stat(meta_path).st_mtime_ns)
                count += 1
            except OSError:
                continue
        sig.append((newest, count))
        return tuple(sig)

    def scan(self, budget_s=None):
        """One pass. Returns the number of conversations (re)written."""
        if not os.path.exists(self.state_db):
            self.progress["available"] = False
            return 0
        self.progress.update(available=True, running=True, error=None)
        started = time.time()
        changed = 0
        try:
            src = ro(self.state_db)
            rows = src.execute(
                "SELECT substr(key, 14), json_extract(value,'$.name'), "
                "json_extract(value,'$.createdAt'), json_extract(value,'$.lastUpdatedAt'), "
                "json_extract(value,'$.modelConfig.modelName'), json_extract(value,'$.unifiedMode'), "
                "json_extract(value,'$.subagentInfo.parentComposerId'), json_extract(value,'$.usageData'), "
                "json_extract(value,'$.workspaceIdentifier.configPath.path'), "
                "json_extract(value,'$.originalFileStates'), json_extract(value,'$.newlyCreatedFiles'), "
                "json_extract(value,'$.context.fileSelections') "
                "FROM cursorDiskKV WHERE key >= 'composerData:' AND key < 'composerData;' "
                "AND json_valid(value)").fetchall()
            with self.lock:
                known = dict(self.con.execute("SELECT id, scanned FROM cursor_convs WHERE source = 'ide'"))
            tracked = self.tracked_projects()
            parents = {r[0]: r[6] for r in rows}
            for cid, name, created, updated, model, mode, parent, usage, ws, ofs, newf, sel in rows:
                stamp = int(updated or created or 0)
                if cid in known and known[cid] == stamp:
                    continue
                if budget_s and time.time() - started > budget_s:
                    break  # the rest next pass; keeps one pass short
                bubbles = src.execute(
                    "SELECT json_extract(value,'$.type'), json_extract(value,'$.createdAt'), "
                    "json_extract(value,'$.modelInfo.modelName'), "
                    "json_extract(value,'$.tokenCount.inputTokens'), "
                    "json_extract(value,'$.tokenCount.outputTokens') "
                    "FROM cursorDiskKV WHERE key >= ? AND key < ? AND json_valid(value)",
                    ("bubbleId:%s:" % cid, "bubbleId:%s;" % cid)).fetchall()
                turns_by_day = {}
                turns = in_tok = out_tok = 0
                last_model = None
                last_ts = 0
                first_ts = None
                for btype, bcreated, bmodel, itok, otok in bubbles:
                    ts = iso_ms(bcreated)
                    in_tok += int(itok or 0)
                    out_tok += int(otok or 0)
                    if ts:
                        first_ts = ts if first_ts is None else min(first_ts, ts)
                        last_ts = max(last_ts, ts)
                    if btype == 1:
                        turns += 1
                        if ts:
                            d = local_day(ts)
                            turns_by_day[d] = turns_by_day.get(d, 0) + 1
                        if bmodel and (ts or 0) >= last_ts - 1:
                            last_model = bmodel
                if not bubbles and not name:
                    continue  # an empty draft
                root = cid
                seen = set()
                while parents.get(root) and root not in seen:
                    seen.add(root)
                    root = parents[root]
                project = tracked.get(cid) or tracked.get(root)
                if not project:
                    votes = {}
                    for path in file_refs(ofs, newf, sel):
                        pr = self.project_of(os.path.dirname(path))
                        if pr and pr not in ("home", "automatismos"):
                            votes[pr] = votes.get(pr, 0) + 1
                    if votes:
                        project = max(votes.items(), key=lambda kv: kv[1])[0]
                if not project and ws:
                    project = os.path.basename(ws).replace(".code-workspace", "") or None
                created_ms = int(created or first_ts or stamp or 0)
                with self.lock:
                    self.con.execute(
                        "INSERT OR REPLACE INTO cursor_convs (id, name, model, mode, source, created, "
                        "updated, parent, root, project, turns, in_tok, out_tok, cents, scanned) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cid, (name or "")[:140] or None, model or last_model, mode, "ide", created_ms,
                         int(updated or last_ts or created_ms), parent, root, project, turns, in_tok,
                         out_tok, legacy_cents(usage), stamp))
                    self.con.execute("DELETE FROM cursor_turns WHERE conv_id = ?", (cid,))
                    self.con.executemany("INSERT INTO cursor_turns (conv_id, day, turns) VALUES (?,?,?)",
                                         [(cid, d, n) for d, n in turns_by_day.items()])
                    self.con.commit()
                changed += 1
            src.close()
            changed += self.scan_cli()
        except sqlite3.Error as exc:
            self.progress["error"] = "cursor: %s" % exc
        except Exception as exc:  # never let Cursor's files stop the server's loop
            self.progress["error"] = "cursor: %s" % type(exc).__name__
        finally:
            self.progress["running"] = False
            self.progress["last_scan"] = int(time.time() * 1000)
            with self.lock:
                self.progress["convs"] = self.con.execute("SELECT COUNT(*) FROM cursor_convs").fetchone()[0]
                if changed:
                    self.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
                    self.con.commit()
        return changed

    def scan_cli(self):
        """cursor-agent CLI chats: one conversation each, project from its cwd. They keep
        no per-message record we can read, so a chat counts one turn per active day."""
        changed = 0
        for meta_path in glob.glob(os.path.join(self.chats_dir, "*", "*", "meta.json")):
            try:
                with open(meta_path) as fh:
                    meta = json.load(fh)
                created = int(meta.get("createdAtMs") or 0)
                updated = int(meta.get("updatedAtMs") or created)
                cwd = meta.get("cwd") if isinstance(meta.get("cwd"), str) else ""
            except (OSError, ValueError, TypeError, AttributeError):
                continue  # a malformed chat file is skipped, never stops the pass
            agent_id = "cli:" + os.path.basename(os.path.dirname(meta_path))
            if not created:
                continue
            with self.lock:
                row = self.con.execute("SELECT scanned FROM cursor_convs WHERE id = ?", (agent_id,)).fetchone()
            if row and row[0] == updated:
                continue
            name = None
            store = os.path.join(os.path.dirname(meta_path), "store.db")
            if os.path.exists(store):
                try:
                    s = ro(store)
                    hit = s.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
                    s.close()
                    if hit:
                        raw = hit[0]
                        raw = bytes.fromhex(raw).decode("utf-8") if isinstance(raw, str) else raw.decode("utf-8")
                        name = (json.loads(raw).get("name") or "")[:140] or None
                except (sqlite3.Error, ValueError, UnicodeDecodeError, AttributeError):
                    name = None
            days = sorted({local_day(created), local_day(updated)})
            with self.lock:
                self.con.execute(
                    "INSERT OR REPLACE INTO cursor_convs (id, name, model, mode, source, created, updated, "
                    "parent, root, project, turns, in_tok, out_tok, cents, scanned) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (agent_id, name or "Cursor CLI", None, "agent", "cli", created, updated, None, agent_id,
                     self.project_of(cwd), len(days), 0, 0, 0.0, updated))
                self.con.execute("DELETE FROM cursor_turns WHERE conv_id = ?", (agent_id,))
                self.con.executemany("INSERT INTO cursor_turns (conv_id, day, turns) VALUES (?,?,?)",
                                     [(agent_id, d, 1) for d in days])
                self.con.commit()
            changed += 1
        return changed


def summarize_cursor(con, lock, t0, t1, top_tasks=12, rename=None):
    """Cursor in [t0, t1). From the API's first event on: requests, tokens and cost per
    request (server). Before it: local turns and legacy cost. Conversations, titles and
    projects come from the local files; subagents fold into their root conversation; API
    requests whose conversation is not on this Mac go to "Cursor (other)". Generated
    names carry a code the page words in its language: name_code on a project
    ("no_folder", "other"), title_code on a task ("untitled", "no_conversation",
    "cli_cloud")."""
    with lock:
        convs = {r[0]: r for r in con.execute(
            "SELECT id, name, model, mode, source, created, updated, root, project, turns, in_tok, "
            "out_tok, cents FROM cursor_convs")}
        api_first = con.execute("SELECT MIN(ts) FROM cursor_events").fetchone()[0]
        events = con.execute(
            "SELECT ts, model, conv_id, in_tok, out_tok, cread, cents, headless FROM cursor_events "
            "WHERE ts >= ? AND ts < ?", (t0, t1)).fetchall()
        local_end = local_day(api_first) if api_first else t1
        turn_rows = con.execute(
            "SELECT t.conv_id, t.day, t.turns, c.root FROM cursor_turns t "
            "JOIN cursor_convs c ON c.id = t.conv_id WHERE t.day >= ? AND t.day < ?",
            (local_day(t0) if t0 > 0 else 0, min(t1, local_end))).fetchall()
    roots = {}
    days = {}
    hours = {}
    models = {}

    def root_of(cid):
        c = convs.get(cid)
        if not c and ("cli:" + cid) in convs:  # cursor-agent chats are stored as cli:<agentId>
            cid = "cli:" + cid
            c = convs[cid]
        return (c[7] or cid) if c else None

    def acc(root, day, ts, turns=0, requests=0, tokens=0, usd=0.0, model=None):
        r = roots.setdefault(root, {"turns": 0, "requests": 0, "tokens": 0, "usd": 0.0, "last": 0, "models": {}})
        r["turns"] += turns
        r["requests"] += requests
        r["tokens"] += tokens
        r["usd"] += usd
        r["last"] = max(r["last"], ts)
        if model:
            r["models"][model] = r["models"].get(model, 0) + requests + turns
        dd = days.setdefault(day, {"t": day, "turns": 0, "requests": 0, "usd": 0.0, "tokens": 0, "convs": set()})
        dd["turns"] += turns
        dd["requests"] += requests
        dd["usd"] += usd
        dd["tokens"] += tokens
        dd["convs"].add(root)
        if model:
            m = models.setdefault(model, {"requests": 0, "turns": 0})
            m["requests"] += requests
            m["turns"] += turns

    for ts, model, conv_id, itok, otok, cread, cents, headless in events:
        hb = ts - ts % 3600000  # hour bins for short ranges (API events have exact times)
        hh = hours.setdefault(hb, {"t": hb, "requests": 0, "usd": 0.0, "tokens": 0})
        hh["requests"] += 1
        hh["usd"] += (cents or 0) / 100.0
        hh["tokens"] += (itok or 0) + (otok or 0) + (cread or 0)
        root = root_of(conv_id) if conv_id else None
        key = root or ("api:" + (conv_id or ("cli" if headless else "otros")))
        acc(key, local_day(ts), ts, requests=1, tokens=(itok or 0) + (otok or 0) + (cread or 0),
            usd=(cents or 0) / 100.0, model=model)
    for conv_id, day, turns, root in turn_rows:
        if day + 86400000 <= t0:
            continue
        own = convs.get(conv_id)
        acc(root or conv_id, day, day, turns=turns, model=own and own[2])
    # Legacy cost and tokens (kept by Cursor until ~2025-10), on the creation day.
    for cid, c in convs.items():
        if (c[12] or c[10] or c[11]) and t0 <= (c[5] or 0) < min(t1, local_end):
            acc(c[7] or cid, local_day(c[5]), c[5], tokens=(c[10] or 0) + (c[11] or 0), usd=(c[12] or 0) / 100.0)

    projects = {}
    for root, r in roots.items():
        c = convs.get(root)
        name_code = title_code = None
        if c:
            pname, title, model, source, last = c[8], c[1], c[2], c[4], max(r["last"], c[6] or 0)
            if not pname:
                pname, name_code = "no folder", "no_folder"
            elif rename:
                pname = rename(pname)
            if not title:
                title, title_code = "Untitled", "untitled"
        else:
            pname, name_code, source, last = "Cursor (other)", "other", "api", r["last"]
            title, title_code = "Request with no conversation on this Mac", "no_conversation"
            model = max(r["models"].items(), key=lambda kv: kv[1])[0] if r["models"] else None
            if root.endswith(":cli"):
                title, title_code, source = "Cursor CLI / cloud", "cli_cloud", "cli"
        p = projects.setdefault(pname, {"name": pname, "turns": 0, "requests": 0, "convs": 0, "usd": 0.0,
                                        "tokens": 0, "last": 0, "tasks": []})
        if name_code:
            p["name_code"] = name_code
        p["turns"] += r["turns"]
        p["requests"] += r["requests"]
        p["convs"] += 1
        p["usd"] += r["usd"]
        p["tokens"] += r["tokens"]
        p["last"] = max(p["last"], last)
        p["tasks"].append({"key": root, "title": title, "title_code": title_code, "model": model, "source": source,
                           "turns": r["turns"], "requests": r["requests"], "usd": round(r["usd"], 4),
                           "tokens": r["tokens"], "last": last})
    plist = []
    for p in projects.values():
        p["tasks"].sort(key=lambda t: (-t["usd"], -(t["requests"] + t["turns"]), -t["last"]))
        p["task_count"] = len(p["tasks"])
        # The costliest, then the 5 most recent of the rest: the sheet's "Última actividad"
        # and "Reciente" order need the newest conversations, often cheap ones.
        rest = sorted(p["tasks"][top_tasks:], key=lambda t: -t["last"])[:5]
        p["tasks"] = p["tasks"][:top_tasks] + rest
        p["usd"] = round(p["usd"], 4)
        plist.append(p)
    plist.sort(key=lambda p: (-p["usd"], -(p["requests"] + p["turns"])))
    totals = {k: sum(p[k] for p in plist) for k in ("turns", "requests", "convs", "tokens")}
    totals["usd"] = round(sum(p["usd"] for p in plist), 4)
    return {
        "totals": totals,
        "api_since": api_first,
        "projects": plist,
        "days": [{"t": d["t"], "turns": d["turns"], "requests": d["requests"], "usd": round(d["usd"], 4),
                  "tokens": d["tokens"], "convs": len(d["convs"])} for _k, d in sorted(days.items())],
        "hours": [dict(h, usd=round(h["usd"], 4)) for _k, h in sorted(hours.items())],
        "models": sorted(({"model": m, "requests": v["requests"], "turns": v["turns"]} for m, v in models.items()),
                         key=lambda x: -(x["requests"] + x["turns"])),
    }
