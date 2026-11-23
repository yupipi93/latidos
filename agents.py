"""Other coding agents for Latidos (L-182): one reader per agent, from the files each one
keeps on disk. Read-only, local files only, no network.

Every reader turns its files into calls (tokens in, out, cache read, cache write, model,
time, session, and the cost the tool itself recorded when it records one) and sessions
(title, working folder, dates). `AgentHub` stores them in `agent_calls` and
`agent_sessions`, re-reading a file only when its size or mtime moved (append-only JSONL
from its last offset) and upserting calls by (agent, id), so a re-read never counts twice.
An agent whose files are absent is simply inactive.

`summarize_agents` gives, per active agent, the same shape as Cursor's summary (projects
with tasks, days, hours, models) plus timeline bins in the range's unit, so the page draws
each agent as its own origin: cells, ranking, day marks, counter and river.

Cost: the tool's own figure when it keeps one (Cline, Roo Code, Kilo Code, Aider, OpenCode),
else the API list price of the model (`api_cost`). A model with no known price keeps its
tokens and costs 0 (counted as unpriced). Tools that keep no token counts on disk count
requests (Copilot) or active days (Antigravity, Windsurf), never an invented cost.
"""

import datetime as dt
import functools
import math
import glob
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse

HOME = os.path.expanduser("~")


def _env(name, default=None):
    """LATIDOS_<name>, else the deprecated LATIDO_<name>, else default."""
    for prefix in ("LATIDOS_", "LATIDO_"):
        raw = os.environ.get(prefix + name)
        if raw is not None:
            return raw
    return default


def agents_home():
    """Where the agents' folders are looked for: the home folder (LATIDOS_AGENTS_HOME in
    tests and demos, so they never read the real files)."""
    return _env("AGENTS_HOME", HOME)


# ---------------------------------------------------------------------------------------
# Pricing of models that are not Claude (USD per million tokens). Public list prices as of
# 2026-11 (OpenAI, Google, and the other vendors' own pages). Columns: input, cached input,
# output; an optional fourth item is (threshold, input, cached, output) for prompts above
# `threshold` tokens (Gemini Pro's long-context tier). Longest prefix wins, so a dated or
# suffixed id ("gpt-5-codex-2026...", "gemini-2.5-pro-preview-06-05") finds its family.
# ---------------------------------------------------------------------------------------

OTHER_PRICES = [
    # OpenAI
    ("gpt-5.2-pro", (21.0, 21.0, 168.0)),
    ("gpt-5.2-codex", (1.75, 0.175, 14.0)),
    ("gpt-5.2", (1.75, 0.175, 14.0)),
    ("gpt-5.1-codex-mini", (0.25, 0.025, 2.0)),
    ("gpt-5.1-codex-max", (1.25, 0.125, 10.0)),
    ("gpt-5.1-codex", (1.25, 0.125, 10.0)),
    ("gpt-5.1", (1.25, 0.125, 10.0)),
    ("gpt-5-pro", (15.0, 15.0, 120.0)),
    ("gpt-5-codex-mini", (0.25, 0.025, 2.0)),
    ("gpt-5-codex", (1.25, 0.125, 10.0)),
    ("gpt-5-mini", (0.25, 0.025, 2.0)),
    ("gpt-5-nano", (0.05, 0.005, 0.40)),
    ("gpt-5", (1.25, 0.125, 10.0)),
    ("gpt-4.1-mini", (0.40, 0.10, 1.60)),
    ("gpt-4.1-nano", (0.10, 0.025, 0.40)),
    ("gpt-4.1", (2.0, 0.50, 8.0)),
    ("gpt-4o-mini", (0.15, 0.075, 0.60)),
    ("gpt-4o", (2.50, 1.25, 10.0)),
    ("gpt-4-turbo", (10.0, 10.0, 30.0)),
    ("gpt-4", (30.0, 30.0, 60.0)),
    ("gpt-3.5-turbo", (0.50, 0.50, 1.50)),
    ("gpt-oss-120b", (0.15, 0.15, 0.60)),
    ("gpt-oss-20b", (0.05, 0.05, 0.20)),
    ("codex-mini", (1.50, 0.375, 6.0)),
    ("o4-mini", (1.10, 0.275, 4.40)),
    ("o3-pro", (20.0, 20.0, 80.0)),
    ("o3-mini", (1.10, 0.55, 4.40)),
    ("o3", (2.0, 0.50, 8.0)),
    ("o1-pro", (150.0, 150.0, 600.0)),
    ("o1-mini", (1.10, 0.55, 4.40)),
    ("o1", (15.0, 7.50, 60.0)),
    # Google
    ("gemini-3-pro", (2.0, 0.20, 12.0, (200000, 4.0, 0.40, 18.0))),
    ("gemini-3-flash", (0.50, 0.05, 3.0)),
    ("gemini-3", (2.0, 0.20, 12.0, (200000, 4.0, 0.40, 18.0))),
    ("gemini-2.5-pro", (1.25, 0.125, 10.0, (200000, 2.50, 0.25, 15.0))),
    ("gemini-2.5-flash-lite", (0.10, 0.01, 0.40)),
    ("gemini-2.5-flash", (0.30, 0.03, 2.50)),
    ("gemini-2.0-flash-lite", (0.075, 0.075, 0.30)),
    ("gemini-2.0-flash", (0.10, 0.025, 0.40)),
    ("gemini-1.5-pro", (1.25, 0.3125, 5.0, (128000, 2.50, 0.625, 10.0))),
    ("gemini-1.5-flash", (0.075, 0.01875, 0.30)),
    ("gemini-exp", (1.25, 0.125, 10.0)),
    # Others an agent can be pointed at
    # DeepSeek V3.2: one price for chat and reasoner (cache hit, miss, output).
    ("deepseek-reasoner", (0.28, 0.028, 0.42)),
    ("deepseek-r1", (0.28, 0.028, 0.42)),
    ("deepseek-chat", (0.28, 0.028, 0.42)),
    ("deepseek-v3", (0.28, 0.028, 0.42)),
    ("grok-code-fast", (0.20, 0.02, 1.50)),
    ("grok-4.1-fast", (0.20, 0.05, 0.50)),
    ("grok-4-fast", (0.20, 0.05, 0.50)),
    ("grok-4", (3.0, 0.75, 15.0)),
    ("grok-3-mini", (0.30, 0.075, 0.50)),
    ("grok-3", (3.0, 0.75, 15.0)),
    ("kimi-k2", (0.60, 0.15, 2.50)),
    ("glm-4.6", (0.60, 0.11, 2.20)),
    ("glm-4.5", (0.60, 0.11, 2.20)),
    ("qwen3-coder", (0.45, 0.45, 1.80)),
    ("codestral", (0.30, 0.30, 0.90)),
    ("devstral", (0.40, 0.40, 2.0)),
    ("mistral-large", (2.0, 2.0, 6.0)),
    ("mistral-medium", (0.40, 0.40, 2.0)),
]
# Longest prefix first, so "gpt-5-mini" is never priced as "gpt-5".
_OTHER_SORTED = sorted(OTHER_PRICES, key=lambda kv: -len(kv[0]))

# Prefixes some tools put before the model id: a provider path ("openrouter/anthropic/",
# "copilot/", "models/", "google/"), a Bedrock region profile ("us.anthropic.").
_DATE_TAIL = re.compile(r"[-@](?:\d{8}|\d{4}-\d{2}-\d{2})(?:-v\d+(?::\d+)?)?$")


@functools.lru_cache(maxsize=4096)
def norm_model(model):
    """A model id as the price tables know it: lower case, provider path and suffixes
    such as ":free" or ":thinking" removed, dots between Claude version digits made dashes
    ("claude-sonnet-4.5" is "claude-sonnet-4-5")."""
    m = (model or "").strip().lower()
    if not m:
        return ""
    m = m.rsplit("/", 1)[-1]
    m = m.split(":", 1)[0] if not m.startswith("arn") else m
    m = re.sub(r"^(?:[a-z]{2,6}(?:-gov)?\.)?anthropic\.", "", m)  # bedrock ids
    m = re.sub(r"-v\d+$", "", m)
    if m.startswith("claude"):
        m = re.sub(r"(?<=\d)\.(?=\d)", "-", m)
        # Old names put the family last ("claude-3-5-sonnet"): keep them as they are.
    return m


_OTHER = dict(OTHER_PRICES)
TIERS = ("flash-lite", "flash", "mini", "nano", "lite", "pro", "fast")


def _tier(name):
    """The tier word a model id names ("gpt-5.4-mini": mini), or None."""
    for t in TIERS:
        if re.search(r"(?:^|[-.])" + t + r"(?:$|[-.])", name):
            return t
    return None


@functools.lru_cache(maxsize=4096)
def other_price(model):
    """(prefix, row) for a model that is not Claude, or (None, None). When the model names
    a tier the matched row does not (a catch-all such as "gpt-5" or "gemini-3" against
    "gpt-5.4-mini" or "gemini-3.1-flash"), the row of that family and tier is used, else
    the model is unpriced rather than priced at another tier's rate (audit of 101)."""
    m = norm_model(model)
    if not m or m.startswith("claude"):
        return None, None
    for prefix, row in _OTHER_SORTED:
        if m == prefix or m.startswith(prefix + "-") or m.startswith(prefix + "."):
            tier = _tier(m)
            if tier and _tier(prefix) != tier:
                alt = prefix + "-" + tier
                return (alt, _OTHER[alt]) if alt in _OTHER else (None, None)
            return prefix, row
    return None, None


def other_cost(model, inp, out, cread=0, cwrite=0):
    """API cost in USD of a call of a non-Claude model, or None when the model is unknown.
    `inp` is the uncached input; cache writes are billed as input (OpenAI and Gemini bill
    no write premium)."""
    _prefix, row = other_price(model)
    if row is None:
        return None
    p_in, p_cache, p_out = row[0], row[1], row[2]
    if len(row) > 3 and (inp + cread + cwrite) > row[3][0]:
        p_in, p_cache, p_out = row[3][1], row[3][2], row[3][3]
    return ((inp + cwrite) * p_in + cread * p_cache + out * p_out) / 1e6


@functools.lru_cache(maxsize=4096)
def is_claude(model):
    m = norm_model(model)
    return m.startswith("claude") or any(w in m for w in ("opus", "sonnet", "haiku"))


def api_cost(model, inp, out, cread=0, cwrite=0, claude=None):
    """API-equivalent cost of one call, or None when the model has no known price.
    `claude(model, inp, out, cread, cwrite)` prices Claude ids (Latidos' own table)."""
    usd = other_cost(model, inp, out, cread, cwrite)
    if usd is not None:
        return usd
    if claude and is_claude(model):
        return claude(norm_model(model), inp, out, cread, cwrite)
    return None


# ---------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------

MIN_MS = 946684800000  # 2000-01-01: older times in an agent file are bogus


def sane_ms(ms):
    """ms when it is a finite time from 2000-01-01 to tomorrow, else None."""
    try:
        v = float(ms)
    except (TypeError, ValueError, OverflowError):
        return None
    if v != v or v < MIN_MS or v > time.time() * 1000 + 86400000:
        return None
    return int(v)


def iso_ms(s):
    """Milliseconds from an ISO 8601 string, epoch seconds or epoch milliseconds. Anything
    that is not a finite time from 2000-01-01 to tomorrow is None (audit of 101)."""
    return sane_ms(_iso_ms(s))


def _iso_ms(s):
    if s is None or s == "" or isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        try:
            v = float(s)
        except OverflowError:
            return None
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return v if v > 1e11 else v * 1000
    try:
        st = str(s).strip()
        if re.match(r"^\d+(\.\d+)?$", st):
            return _iso_ms(float(st))
        if st.endswith("Z"):
            st = st[:-1] + "+00:00"
        st = re.sub(r"(\.\d{6})\d+", r"\1", st)
        d = dt.datetime.fromisoformat(st)
        if d.tzinfo is None:
            d = d.astimezone()
        return int(d.timestamp() * 1000)
    except (ValueError, OverflowError, TypeError):
        return None


def local_day(ms):
    d = dt.datetime.fromtimestamp(ms / 1000).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


TOKEN_CAP = 10 ** 12  # more tokens than any call can carry: a bogus figure, not a count


def _int(v):
    try:
        n = max(0, int(v or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if n <= TOKEN_CAP else 0


COST_CAP = 1e6  # dollars: no single call costs more; a bigger figure is bogus


def _num(v):
    """A finite cost from 0 to COST_CAP, else None: an inf or a 400-digit figure in a tool's
    file would reach the page as Infinity and blank it (audit of 104)."""
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) and 0 <= f <= COST_CAP else None


def recorded_cost(v):
    """The cost a tool recorded for a call, or None to value it at API list price. One rule
    for every reader (audit of 101): a positive figure is kept; 0 (a subscription or free
    tier) is valued at list price like Claude Code's calls, so the page shows what the use
    would cost per call."""
    c = _num(v)
    return c if c else None


def clip_title(text, n=120):
    """First meaningful line of a prompt, without the tags tools wrap around it."""
    if not isinstance(text, str):
        return None
    t = re.sub(r"<([a-z_]+)>.*?</\1>", " ", text, flags=re.S)
    for line in t.splitlines():
        line = line.strip().lstrip("#>").strip()
        if line:
            return line[:n]
    return None


def text_of(content):
    """Text of a message content: a string, or a list of parts with "text"."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [c.get("text") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str)]
        return "\n".join(parts) if parts else None
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return None


def file_uri_path(uri):
    if not isinstance(uri, str):
        return None
    if uri.startswith("file://"):
        return urllib.parse.unquote(urllib.parse.urlparse(uri).path) or None
    return uri if uri.startswith("/") else None


def load_json(path):
    with open(path, "rb") as f:
        return json.loads(f.read().decode("utf-8", "replace"))


def editor_user_dirs(home, names=("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf", "Antigravity",
                                  "Trae", "Kiro")):
    """The User folder of every VS Code family editor (macOS and Linux layouts)."""
    out = []
    for base in (os.path.join(home, "Library", "Application Support"), os.path.join(home, ".config")):
        for n in names:
            p = os.path.join(base, n, "User")
            if os.path.isdir(p):
                out.append(p)
    return out


# ---------------------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------------------

class Reader:
    """One agent. `files()` lists what to read (empty when the agent is absent); `parse`
    reads one file from `state` (a dict kept between passes: the offset of an append-only
    file, the session it belongs to) and returns (calls, sessions, state).

    A call: {"id", "ts", "session", "model", "inp", "out", "cread", "cwrite", "usd"}
    (`usd` only when the tool recorded its own cost). A session: {"id", "title", "cwd",
    "created", "updated"}."""
    id = ""
    name = ""
    count_unit = "calls"   # what a call with no tokens counts as: calls, requests, days
    tokens = True          # whether the tool keeps token counts on disk
    append_only = False    # JSONL that only grows: parse from the last offset

    def __init__(self, home=None, known_dirs=None):
        self.home = home or agents_home()
        self.known_dirs = known_dirs or (lambda: [])

    def files(self):
        return []

    def parse(self, path, state):
        raise NotImplementedError


class CodexReader(Reader):
    """OpenAI Codex CLI: $CODEX_HOME (default ~/.codex) sessions/YYYY/MM/DD/rollout-*.jsonl
    and archived_sessions/. Lines: session_meta (id, cwd), turn_context (model, cwd),
    event_msg token_count (last_token_usage, total_token_usage), user messages. Input
    tokens include the cached ones; reasoning is part of the output."""
    id = "codex"
    name = "Codex"
    append_only = True

    def root(self):
        if os.environ.get("CODEX_HOME") and self.home == HOME:
            return os.environ["CODEX_HOME"]
        return os.path.join(self.home, ".codex")

    def files(self):
        r = self.root()
        return (glob.glob(os.path.join(r, "sessions", "*", "*", "*", "*.jsonl")) +
                glob.glob(os.path.join(r, "sessions", "*.jsonl")) +
                glob.glob(os.path.join(r, "archived_sessions", "*.jsonl")))

    def parse(self, path, state):
        st = dict(state or {})
        m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", os.path.basename(path))
        sid = st.get("sid") or (m.group(1) if m else os.path.basename(path))
        calls, sess = [], {"id": sid}
        with open(path, "rb") as f:
            f.seek(st.get("pos", 0))
            data = f.read()
        end = data.rfind(b"\n") + 1  # a half-written last line waits for the next pass
        for raw in data[:end].splitlines():
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            kind, p = d.get("type"), d.get("payload") if isinstance(d.get("payload"), dict) else {}
            ts = iso_ms(d.get("timestamp"))
            if kind == "session_meta":
                sid = p.get("id") or sid
                st["sid"] = sess["id"] = sid
                if p.get("cwd"):
                    st["cwd"] = p["cwd"]
                sess["created"] = iso_ms(p.get("timestamp")) or ts
            elif kind == "turn_context":
                if p.get("model"):
                    st["model"] = p["model"]
                if p.get("cwd"):
                    st["cwd"] = p["cwd"]
            elif kind == "event_msg" and p.get("type") == "token_count":
                info = p.get("info") if isinstance(p.get("info"), dict) else None
                last = info and info.get("last_token_usage")
                total = info and info.get("total_token_usage")
                if not isinstance(last, dict) or ts is None:
                    continue
                tot = _int((total or {}).get("total_tokens"))
                if tot and tot == st.get("total"):
                    continue  # the same count emitted twice
                st["total"] = tot
                inp, cached = _int(last.get("input_tokens")), _int(last.get("cached_input_tokens"))
                calls.append({"id": "%s:%d" % (sid, tot or ts), "ts": ts, "session": sid, "model": st.get("model"),
                              "inp": max(0, inp - cached), "cread": min(cached, inp) if inp else cached,
                              "out": _int(last.get("output_tokens")), "cwrite": 0})
            elif kind == "event_msg" and p.get("type") == "user_message" and not st.get("titled"):
                t = clip_title(p.get("message"))
                if t:
                    sess["title"], st["titled"] = t, True
            elif kind == "response_item" and p.get("type") == "message" and p.get("role") == "user" and not st.get("titled"):
                t = clip_title(text_of(p.get("content")))
                if t and not t.startswith("<"):
                    sess["title"], st["titled"] = t, True
            if ts:
                sess["updated"] = max(sess.get("updated") or 0, ts)
        sess["cwd"] = st.get("cwd")
        st["pos"] = st.get("pos", 0) + end
        return calls, [sess], st


class GeminiReader(Reader):
    """Gemini CLI: ~/.gemini/tmp/<project>/chats/session-*.json, one JSON per session:
    {sessionId, projectHash, startTime, lastUpdated, messages: [{id, timestamp, type
    ("user" | "gemini"), content, model, tokens: {input, output, cached, thoughts, tool}}]}.
    The folder is named by the sha256 of the working folder (or by a short name with a
    `.project_root` file): resolved against the folders Latidos already knows. Input
    includes the cached tokens; thoughts are billed as output."""
    id = "gemini"
    name = "Gemini CLI"

    def files(self):
        return glob.glob(os.path.join(self.home, ".gemini", "tmp", "*", "chats", "*.json"))

    def cwd_of(self, path, project_hash):
        tmp = os.path.dirname(os.path.dirname(path))
        for marker in (".project_root", "project_root"):
            try:
                with open(os.path.join(tmp, marker), encoding="utf-8") as f:
                    v = f.read().strip()
                if v.startswith("/"):
                    return v
            except OSError:
                pass
        h = project_hash or os.path.basename(tmp)
        if re.match(r"^[0-9a-f]{64}$", h or ""):
            for d in self.known_dirs():
                if hashlib.sha256(d.encode("utf-8")).hexdigest() == h:
                    return d
        return None

    def parse(self, path, state):
        d = load_json(path)
        if not isinstance(d, dict):
            return [], [], {}
        sid = d.get("sessionId") or os.path.basename(path)[:-5]
        sess = {"id": sid, "created": iso_ms(d.get("startTime")), "updated": iso_ms(d.get("lastUpdated")),
                "cwd": self.cwd_of(path, d.get("projectHash")), "title": clip_title(d.get("summary"))}
        # No folder yet: read again once more folders are known (audit of 101).
        state = {"retry": True} if not sess["cwd"] else {}
        calls = []
        for i, msg in enumerate(d.get("messages") or []):
            if not isinstance(msg, dict):
                continue
            ts = iso_ms(msg.get("timestamp"))
            if msg.get("type") == "user" and not sess.get("title"):
                sess["title"] = clip_title(text_of(msg.get("content")))
            tk = msg.get("tokens")
            if msg.get("type") != "gemini" or not isinstance(tk, dict) or ts is None:
                continue
            inp, cached = _int(tk.get("input")), _int(tk.get("cached"))
            calls.append({"id": "%s:%s" % (sid, msg.get("id") or i), "ts": ts, "session": sid, "model": msg.get("model"),
                          "inp": max(0, inp - cached) + _int(tk.get("tool")), "cread": min(cached, inp) if inp else cached,
                          "out": _int(tk.get("output")) + _int(tk.get("thoughts")), "cwrite": 0})
        return calls, [sess], state


class ActivityReader(Reader):
    """Agents whose conversations are opaque protobuf files (Antigravity, Windsurf
    Cascade): no tokens and no model on disk. Each conversation file counts once per
    local day it was created or written on (seen by its birth time and its mtime at each
    pass), so the page shows active days, never a cost."""
    tokens = False
    count_unit = "days"
    dirs = ()

    def files(self):
        out = []
        for d in self.dirs:
            out += glob.glob(os.path.join(self.home, d, "*.pb"))
        return out

    def title_of(self, conv):
        return None

    def parse(self, path, state):
        st = os.stat(path)
        conv = os.path.basename(path)[:-3]
        born = int((getattr(st, "st_birthtime", 0) or st.st_mtime) * 1000)
        mt = int(st.st_mtime * 1000)
        calls = []
        for ts in sorted({born, mt}):
            calls.append({"id": "%s:%d" % (conv, local_day(ts)), "ts": ts, "session": conv, "model": None,
                          "inp": 0, "out": 0, "cread": 0, "cwrite": 0})
        return calls, [{"id": conv, "created": born, "updated": mt, "title": self.title_of(conv)}], {}


class AntigravityReader(ActivityReader):
    """Google Antigravity: ~/.gemini/antigravity/conversations/<id>.pb; the brain folder
    of a conversation (brain/<id>/task.md) gives its title."""
    id = "antigravity"
    name = "Antigravity"
    dirs = (os.path.join(".gemini", "antigravity", "conversations"),)

    def title_of(self, conv):
        for name in ("task.md", "implementation_plan.md", "walkthrough.md"):
            try:
                with open(os.path.join(self.home, ".gemini", "antigravity", "brain", conv, name),
                          encoding="utf-8", errors="replace") as f:
                    t = clip_title(f.read(4000))
                if t:
                    return t
            except OSError:
                continue
        return None


class WindsurfReader(ActivityReader):
    """Windsurf: ~/.codeium/windsurf/cascade/<id>.pb (and Windsurf Next)."""
    id = "windsurf"
    name = "Windsurf"
    dirs = (os.path.join(".codeium", "windsurf", "cascade"), os.path.join(".codeium", "windsurf-next", "cascade"))


def replay_ops(lines):
    """VS Code's chat session journal (.jsonl): kind 0 is the whole state, kind 1 sets the
    value at path k, kind 2 appends the items of v to the list at k, kind 3 deletes k."""
    state = None
    for raw in lines:
        try:
            op = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(op, dict):
            continue
        kind, k, v = op.get("kind"), op.get("k") or [], op.get("v")
        if kind == 0:
            state = v
            continue
        if not isinstance(state, (dict, list)) or not isinstance(k, list) or not k:
            continue
        node = state
        try:
            for key in k[:-1]:
                node = node[key]
            last = k[-1]
            if kind == 1:
                node[last] = v
            elif kind == 2:
                lst = node.get(last) if isinstance(node, dict) else node[last]
                if lst is None:
                    lst = node[last] = []
                lst.extend(v if isinstance(v, list) else [v])
            elif kind == 3:
                del node[last]
        except (KeyError, IndexError, TypeError):
            continue
    return state


class CopilotReader(Reader):
    """GitHub Copilot Chat in VS Code: User/workspaceStorage/<hash>/chatSessions/<id>.json
    (or the .jsonl journal of newer versions) and User/globalStorage/
    emptyWindowChatSessions/. A request: {requestId, timestamp, modelId, message.text,
    result}. The workspace folder comes from workspace.json. Copilot keeps no token
    counts on disk in most versions: each request counts as one; the rare usage figures
    (result.usage / result.metadata) are read when present."""
    id = "copilot"
    name = "Copilot"
    count_unit = "requests"
    tokens = False

    def files(self):
        out = []
        for user in editor_user_dirs(self.home, ("Code", "Code - Insiders", "VSCodium")):
            for ext in ("*.json", "*.jsonl"):
                out += glob.glob(os.path.join(user, "workspaceStorage", "*", "chatSessions", ext))
                out += glob.glob(os.path.join(user, "globalStorage", "emptyWindowChatSessions", ext))
        return out

    @staticmethod
    def usage(result):
        if not isinstance(result, dict):
            return None
        for u in (result.get("usage"), (result.get("metadata") or {}).get("usage") if isinstance(result.get("metadata"), dict) else None,
                  result.get("metadata")):
            if isinstance(u, dict):
                i = _int(u.get("promptTokens") or u.get("prompt_tokens") or u.get("inputTokens"))
                o = _int(u.get("completionTokens") or u.get("completion_tokens") or u.get("outputTokens"))
                if i or o:
                    return i, o
        return None

    def parse(self, path, state):
        if path.endswith(".jsonl"):
            with open(path, "rb") as f:
                d = replay_ops(f.read().decode("utf-8", "replace").splitlines())
        else:
            d = load_json(path)
        if not isinstance(d, dict):
            return [], [], {}
        sid = d.get("sessionId") or os.path.basename(path).rsplit(".", 1)[0]
        cwd = None
        ws = os.path.join(os.path.dirname(os.path.dirname(path)), "workspace.json")
        try:
            w = load_json(ws)
            cwd = file_uri_path(w.get("folder")) if isinstance(w, dict) else None
        except (OSError, ValueError):
            pass
        sess = {"id": sid, "cwd": cwd, "title": clip_title(d.get("customTitle")),
                "created": iso_ms(d.get("creationDate")), "updated": iso_ms(d.get("lastMessageDate"))}
        calls = []
        for i, r in enumerate(d.get("requests") or []):
            if not isinstance(r, dict):
                continue
            ts = iso_ms(r.get("timestamp")) or sess["created"]
            if ts is None:
                continue
            msg = r.get("message")
            if not sess.get("title"):
                sess["title"] = clip_title(msg.get("text") if isinstance(msg, dict) else msg)
            u = self.usage(r.get("result"))
            calls.append({"id": "%s:%s" % (sid, r.get("requestId") or i), "ts": ts, "session": sid,
                          "model": r.get("modelId"), "inp": u[0] if u else 0, "out": u[1] if u else 0,
                          "cread": 0, "cwrite": 0})
            sess["updated"] = max(sess.get("updated") or 0, ts)
        return calls, [sess], {}


class ClineReader(Reader):
    """Cline (saoudrizwan.claude-dev) and its forks, in any VS Code family editor:
    globalStorage/<extension>/tasks/<taskId>/ui_messages.json. Each "api_req_started"
    message carries tokensIn, tokensOut, cacheWrites, cacheReads and the cost Cline
    computed; the first "task" message is the title; the working folder is read from the
    environment details in api_conversation_history.json; task_metadata.json names the
    model when the request does not."""
    id = "cline"
    name = "Cline"
    extension = "saoudrizwan.claude-dev"
    cli_dir = os.path.join(".cline", "data", "tasks")
    CWD_RE = re.compile(r"Current (?:Working|Workspace) Directory \(([^)\n]+)\)")

    def task_dirs(self):
        out = []
        for user in editor_user_dirs(self.home):
            out += glob.glob(os.path.join(user, "globalStorage", self.extension, "tasks", "*"))
        if self.cli_dir:
            out += glob.glob(os.path.join(self.home, self.cli_dir, "*"))
        return out

    def files(self):
        return [p for p in (os.path.join(t, "ui_messages.json") for t in self.task_dirs()) if os.path.isfile(p)]

    def parse(self, path, state):
        task_dir = os.path.dirname(path)
        task = os.path.basename(task_dir)
        msgs = load_json(path)
        if not isinstance(msgs, list):
            return [], [], {}
        models = []
        try:
            meta = load_json(os.path.join(task_dir, "task_metadata.json"))
            for u in (meta.get("model_usage") or []) if isinstance(meta, dict) else []:
                if isinstance(u, dict) and u.get("model_id"):
                    models.append((_int(u.get("ts")), u["model_id"]))
        except (OSError, ValueError):
            pass
        models.sort()
        cwd = None
        try:
            with open(os.path.join(task_dir, "api_conversation_history.json"), "rb") as f:
                m = self.CWD_RE.search(f.read(400000).decode("utf-8", "replace"))
            cwd = m.group(1).strip() if m else None
        except OSError:
            pass
        sess = {"id": task, "cwd": cwd}
        calls = []
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            ts = iso_ms(msg.get("ts"))
            if ts is None:
                continue
            sess["created"] = min(sess.get("created") or ts, ts)
            sess["updated"] = max(sess.get("updated") or 0, ts)
            if msg.get("say") == "task" or (msg.get("type") == "say" and msg.get("say") == "task"):
                sess.setdefault("title", clip_title(msg.get("text")))
            if msg.get("say") != "api_req_started":
                continue
            try:
                info = json.loads(msg.get("text") or "{}")
            except ValueError:
                continue
            if not isinstance(info, dict):
                continue
            model = info.get("modelId") or info.get("model")
            if not model:
                for mts, mid in models:
                    if mts <= ts:
                        model = mid
                model = model or (models[0][1] if models else None)
            call = {"id": "%s:%d" % (task, ts), "ts": ts, "session": task, "model": model,
                    "inp": _int(info.get("tokensIn")), "out": _int(info.get("tokensOut")),
                    "cread": _int(info.get("cacheReads")), "cwrite": _int(info.get("cacheWrites"))}
            if recorded_cost(info.get("cost")) is not None:
                call["usd"] = recorded_cost(info.get("cost"))
            if call["inp"] or call["out"] or call["cread"] or call["cwrite"] or call.get("usd"):
                calls.append(call)
        return calls, [sess], {}


class RooReader(ClineReader):
    """Roo Code (rooveterinaryinc.roo-cline): Cline's task layout."""
    id = "roo"
    name = "Roo Code"
    extension = "rooveterinaryinc.roo-cline"
    cli_dir = None


class KiloReader(ClineReader):
    """Kilo Code (kilocode.kilo-code): Cline's task layout."""
    id = "kilo"
    name = "Kilo Code"
    extension = "kilocode.kilo-code"
    cli_dir = None


class AiderReader(Reader):
    """Aider: .aider.chat.history.md in each repository it ran in, found under the
    folders Latidos knows (code folder, workspaces, projects; LATIDOS_AIDER_ROOTS adds
    more, separated by ":"). A session starts at "# aider chat started at <time>"; "> Model:"
    or "> Main model:" names the model; "#### " lines are the user's prompts; each
    "> Tokens: 4.4k sent, 1.2k cache write, 9k cache hit, 213 received. Cost: $0.02
    message, ..." line is one call with the cost Aider computed. Calls carry no time of
    their own: they are placed a second apart from the session start."""
    id = "aider"
    name = "Aider"
    FILE = ".aider.chat.history.md"
    SKIP = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", "dist", "build", ".terraform", "target"}
    MAX_DEPTH = 4
    START_RE = re.compile(r"^# aider chat started at (\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")
    MODEL_RE = re.compile(r"^> (?:Main )?[Mm]odel: (\S+)")
    TOKENS_RE = re.compile(r"^> Tokens: (.+?)(?:\. Cost: \$([0-9.]+) message.*)?$")
    PART_RE = re.compile(r"([0-9.]+)([kKmM]?) (sent|received|cache write|cache hit)")

    def __init__(self, home=None, known_dirs=None, roots=None):
        Reader.__init__(self, home, known_dirs)
        self.roots = roots
        self._found = (0, [])

    def search_roots(self):
        roots = list(self.roots or [])
        extra = _env("AIDER_ROOTS")
        if extra:
            roots += [os.path.expanduser(r) for r in extra.split(":") if r]
        out, real = [], set()
        for r in roots:
            if not r or not os.path.isdir(r):
                continue
            rp = os.path.realpath(r)
            if rp not in real:  # the same folder twice, or through a link: walked once
                real.add(rp)
                out.append(os.path.abspath(r))
        return out

    def files(self, now=None):
        now = time.time() if now is None else now
        if now - self._found[0] < 600 and self._found[0]:
            return list(self._found[1])  # the tree walk runs at most every 10 min
        out, seen = [], set()
        home_file = os.path.join(self.home, self.FILE)
        if os.path.isfile(home_file):
            out.append(home_file)
            seen.add(os.path.realpath(home_file))
        roots = self.search_roots()
        real_roots = {os.path.realpath(r) for r in roots}
        for root in roots:
            # Every root is walked to its own depth (a root nested in another keeps its full
            # depth and its hidden or skipped folders); a parent's walk leaves the folders
            # that are roots themselves to their own walk (audit of 102).
            base = root.rstrip("/").count("/")
            for dirpath, dirnames, filenames in os.walk(root):
                depth = dirpath.rstrip("/").count("/") - base
                keep = []
                if depth < self.MAX_DEPTH:
                    for d in dirnames:
                        if d in self.SKIP or d.startswith("."):
                            continue
                        if os.path.realpath(os.path.join(dirpath, d)) in real_roots:
                            continue
                        keep.append(d)
                dirnames[:] = keep
                if self.FILE in filenames:
                    p = os.path.join(dirpath, self.FILE)
                    rp = os.path.realpath(p)
                    if rp not in seen:
                        seen.add(rp)
                        out.append(p)
        self._found = (now, out)
        return out

    @classmethod
    def tokens_of(cls, text):
        got = {"sent": 0, "received": 0, "cache write": 0, "cache hit": 0}
        for num, suffix, what in cls.PART_RE.findall(text):
            try:
                got[what] = int(round(float(num) * {"": 1, "k": 1e3, "m": 1e6}[suffix.lower()]))
            except (ValueError, OverflowError):
                continue  # a figure too big to be a count: dropped (the store caps the rest)
        return got

    def parse(self, path, state):
        with open(path, "rb") as f:
            lines = f.read().decode("utf-8", "replace").splitlines()
        cwd = os.path.dirname(path)
        calls, sessions = [], []
        sess, model, n = None, None, 0
        for line in lines:
            m = self.START_RE.match(line)
            if m:
                start = iso_ms(m.group(1).replace(" ", "T"))
                if start is None:
                    sess = None
                    continue
                sess = {"id": "%s@%d" % (hashlib.sha1(path.encode("utf-8")).hexdigest()[:12], start),
                        "cwd": cwd, "created": start, "updated": start}
                sessions.append(sess)
                model, n = None, 0
                continue
            if sess is None:
                continue
            m = self.MODEL_RE.match(line)
            if m:
                model = m.group(1).rstrip(",")
                continue
            if line.startswith("#### ") and not sess.get("title"):
                sess["title"] = clip_title(line[5:])
                continue
            m = self.TOKENS_RE.match(line)
            if m:
                n += 1
                tk = self.tokens_of(m.group(1))
                hit, write = tk["cache hit"], tk["cache write"]
                ts = sess["created"] + n * 1000
                call = {"id": "%s:%d" % (sess["id"], n), "ts": ts, "session": sess["id"], "model": model,
                        "inp": max(0, tk["sent"] - hit - write), "out": tk["received"], "cread": hit, "cwrite": write}
                if recorded_cost(m.group(2)) is not None:
                    call["usd"] = recorded_cost(m.group(2))
                calls.append(call)
                sess["updated"] = ts
        return calls, sessions, {}


class OpenCodeReader(Reader):
    """OpenCode: $XDG_DATA_HOME/opencode (default ~/.local/share/opencode) storage/
    session/<project>/<sessionId>.json {id, title, directory, time} and
    storage/message/<sessionId>/<messageId>.json {role, modelID, providerID, cost,
    tokens {input, output, reasoning, cache {read, write}}, time {created}, path {cwd}}.
    One unit per session's message folder, re-read when the folder changes."""
    id = "opencode"
    name = "OpenCode"

    def root(self):
        base = os.environ.get("XDG_DATA_HOME") if self.home == HOME else None
        return os.path.join(base or os.path.join(self.home, ".local", "share"), "opencode", "storage")

    def files(self):
        return [d for d in glob.glob(os.path.join(self.root(), "message", "*")) if os.path.isdir(d)]

    def signature(self, path):
        """A message folder changes when a message file is added; one being rewritten
        moves its own mtime, so the newest of both is the signature."""
        newest, count = os.stat(path).st_mtime, 0
        for name in os.listdir(path):
            if name.endswith(".json"):
                count += 1
                try:
                    newest = max(newest, os.stat(os.path.join(path, name)).st_mtime)
                except OSError:
                    continue
        return count, newest

    def parse(self, path, state):
        sid = os.path.basename(path)
        sess = {"id": sid}
        for sp in glob.glob(os.path.join(self.root(), "session", "*", sid + ".json")):
            try:
                s = load_json(sp)
            except (OSError, ValueError):
                continue
            if isinstance(s, dict):
                t = s.get("time") if isinstance(s.get("time"), dict) else {}
                sess.update(title=clip_title(s.get("title")), cwd=s.get("directory"),
                            created=iso_ms(t.get("created")), updated=iso_ms(t.get("updated")))
        calls = []
        for mp in sorted(glob.glob(os.path.join(path, "*.json"))):
            try:
                m = load_json(mp)
            except (OSError, ValueError):
                continue
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            t = m.get("time") if isinstance(m.get("time"), dict) else {}
            ts = iso_ms(t.get("created"))
            tk = m.get("tokens") if isinstance(m.get("tokens"), dict) else {}
            cache = tk.get("cache") if isinstance(tk.get("cache"), dict) else {}
            if ts is None:
                continue
            path_info = m.get("path") if isinstance(m.get("path"), dict) else {}
            if not sess.get("cwd") and path_info.get("cwd"):
                sess["cwd"] = path_info["cwd"]
            call = {"id": m.get("id") or os.path.basename(mp)[:-5], "ts": ts, "session": sid,
                    "model": m.get("modelID"), "inp": _int(tk.get("input")),
                    "out": _int(tk.get("output")) + _int(tk.get("reasoning")),
                    "cread": _int(cache.get("read")), "cwrite": _int(cache.get("write"))}
            if recorded_cost(m.get("cost")) is not None:
                call["usd"] = recorded_cost(m.get("cost"))
            calls.append(call)
        return calls, [sess], {}


READERS = (CodexReader, GeminiReader, AntigravityReader, CopilotReader, WindsurfReader,
           ClineReader, RooReader, KiloReader, AiderReader, OpenCodeReader)


# ---------------------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_calls (
  agent TEXT NOT NULL, id TEXT NOT NULL,
  ts INTEGER NOT NULL, session TEXT, model TEXT,
  inp INTEGER, out INTEGER, cread INTEGER, cwrite INTEGER,
  usd REAL,
  PRIMARY KEY (agent, id)
);
CREATE INDEX IF NOT EXISTS agent_calls_ts ON agent_calls(ts);
CREATE TABLE IF NOT EXISTS agent_sessions (
  agent TEXT NOT NULL, id TEXT NOT NULL,
  title TEXT, cwd TEXT, created INTEGER, updated INTEGER,
  PRIMARY KEY (agent, id)
);
CREATE TABLE IF NOT EXISTS agent_files (
  path TEXT PRIMARY KEY, agent TEXT, sig TEXT, state TEXT
);
"""


class AgentHub:
    """Scans every reader. `known_dirs` lists working folders already seen (Claude Code's
    and the agents' own), which resolve Gemini CLI's hashed project folders."""
    SCAN_S = 60

    def __init__(self, con, lock, readers=None, home=None, roots=None):
        self.con = con
        self.lock = lock
        with lock:
            con.executescript(SCHEMA)
            con.commit()
        self._dirs = (0, [])
        home = home or agents_home()
        self.readers = readers if readers is not None else [
            (AiderReader(home, self.known_dirs, roots=roots) if cls is AiderReader else cls(home, self.known_dirs))
            for cls in READERS]
        self.progress = {r.id: {"available": False, "files": 0, "skipped": 0, "error": None} for r in self.readers}
        self.last_scan = 0
        self.resume = None  # (reader index, file path) a budgeted pass stopped at
        self.sessions_changed = 0

    def known_dirs(self):
        now = time.time()
        if now - self._dirs[0] < 300 and self._dirs[0]:
            return self._dirs[1]
        out = set()
        with self.lock:
            for table, col in (("calls", "cwd"), ("agent_sessions", "cwd")):
                try:
                    out.update(r[0] for r in self.con.execute(
                        "SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL" % (col, table, col)))
                except sqlite3.Error:
                    continue
        out = sorted(d.rstrip("/") or "/" for d in out if isinstance(d, str))
        self._dirs = (now, out)
        return out

    def due(self, now=None):
        """A pass every SCAN_S, or at once while a budgeted pass left files unread."""
        now = time.time() if now is None else now
        return self.resume is not None or now - self.last_scan >= self.SCAN_S

    def _sig(self, reader, path):
        if hasattr(reader, "signature"):
            return json.dumps(reader.signature(path))
        st = os.stat(path)
        return json.dumps([st.st_size, st.st_mtime_ns])

    def _record(self, path, agent, sig, state):
        with self.lock:
            self.con.execute("INSERT OR REPLACE INTO agent_files (path, agent, sig, state) VALUES (?, ?, ?, ?)",
                             (path, agent, sig, state))
            self.con.commit()

    def scan(self, now=None, budget_s=None):
        """One pass over every reader. Returns the number of calls written. With
        `budget_s` the pass stops once that much time went by, after at least one file, and
        the next one resumes at the same reader and file path (the first scan of a large
        history spreads over several passes)."""
        self.last_scan = time.time() if now is None else now
        started = time.monotonic()
        written = 0
        self.sessions_changed = 0
        done = 0  # files handled in this pass: at least one, whatever the budget
        with self.lock:
            known = {r[0]: (r[1], r[2], r[3]) for r in self.con.execute(
                "SELECT path, agent, sig, state FROM agent_files")}
        n = len(self.readers)
        first, after = self.resume or (0, None)
        self.resume = None
        ndirs = None
        for k in range(n):
            ri = (first + k) % n
            reader = self.readers[ri]
            prog = self.progress[reader.id]
            try:
                paths = reader.files()
            except Exception as exc:  # noqa: BLE001 - one reader never stops the others
                prog.update(available=False, error=exc.__class__.__name__)
                continue
            prog.update(available=bool(paths), files=len(paths), error=None)
            if k == 0 and after is not None and after in paths:
                paths = paths[paths.index(after):]  # resume by path: the list may have changed
            for path in paths:
                if budget_s is not None and done and time.monotonic() - started > budget_s:
                    self.resume = (ri, path)
                    break
                done += 1
                try:
                    sig = self._sig(reader, path)
                except Exception:  # noqa: BLE001 - a file gone or unreadable: next pass
                    continue
                old = known.get(path)
                if old and old[1] and "|dirs=" in old[1]:
                    # Read before its folder was known: again once more folders are known.
                    ndirs = len(self.known_dirs()) if ndirs is None else ndirs
                    sig = sig + "|dirs=%d" % ndirs
                if old and old[1] == sig:
                    continue
                state = {}
                if reader.append_only and old and old[2]:
                    try:
                        state = json.loads(old[2])
                        if state.get("pos", 0) > os.path.getsize(path):
                            state = {}  # rewritten shorter: read it again from the start
                    except (ValueError, OSError, AttributeError):
                        state = {}
                try:
                    calls, sessions, state = reader.parse(path, state)
                    written += self.store(reader.id, calls, sessions)
                except Exception:  # noqa: BLE001 - a bad file is skipped until it changes
                    prog["skipped"] += 1  # store() rolled back what it wrote: nothing of it stays
                    try:
                        self._record(path, reader.id, sig.split("|dirs=")[0], None)
                    except sqlite3.Error:
                        pass
                    continue
                state = state or {}
                if state.get("retry") and "|dirs=" not in sig:
                    ndirs = len(self.known_dirs()) if ndirs is None else ndirs
                    sig = sig + "|dirs=%d" % ndirs
                elif not state.get("retry"):
                    sig = sig.split("|dirs=")[0]
                self._record(path, reader.id, sig, json.dumps(state) if reader.append_only else None)
            if self.resume is not None:
                break
        if written or self.sessions_changed:
            self.sessions_changed = 0
            with self.lock:
                self.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
                self.con.commit()
        return written

    def store(self, agent, calls, sessions, commit=True):
        """Upserts calls and sessions; returns how many calls changed. Sessions whose folder
        or title moved are counted in `sessions_changed`, which also moves the data version
        so open pages reload (audit of 102). A failure part way rolls back and raises."""
        with self.lock:
            changed = self.sessions_changed
            try:
                n = self._store(agent, calls, sessions)
                if commit:
                    self.con.commit()
            except Exception:
                # Under the same lock: no other writer can commit this file's half, and the
                # sessions it counted are not counted (no needless version move: audit of 104).
                self.con.rollback()
                self.sessions_changed = changed
                raise
        return n

    def _store(self, agent, calls, sessions):
        n = 0
        for c in calls:
            if sane_ms(c.get("ts")) is None or not c.get("id"):
                continue
            model = c.get("model")
            model = None if model is None else str(model)[:200]
            cur = self.con.execute(
                "INSERT INTO agent_calls (agent, id, ts, session, model, inp, out, cread, cwrite, usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(agent, id) DO UPDATE SET "
                "ts=excluded.ts, session=excluded.session, model=COALESCE(excluded.model, model), "
                "inp=excluded.inp, out=excluded.out, cread=excluded.cread, cwrite=excluded.cwrite, "
                "usd=excluded.usd WHERE inp IS NOT excluded.inp OR out IS NOT excluded.out OR "
                "cread IS NOT excluded.cread OR cwrite IS NOT excluded.cwrite OR usd IS NOT excluded.usd "
                "OR ts IS NOT excluded.ts OR model IS NOT COALESCE(excluded.model, model)",
                (agent, str(c["id"]), int(c["ts"]), c.get("session"), model, _int(c.get("inp")),
                 _int(c.get("out")), _int(c.get("cread")), _int(c.get("cwrite")), _num(c.get("usd"))))
            n += cur.rowcount if cur.rowcount > 0 else 0
        for s in sessions:
            if not s.get("id"):
                continue
            created = sane_ms(s.get("created"))
            updated = sane_ms(s.get("updated"))
            title = s.get("title") if isinstance(s.get("title"), str) else None
            cwd = s.get("cwd") if isinstance(s.get("cwd"), str) else None
            old = self.con.execute("SELECT title, cwd FROM agent_sessions WHERE agent = ? AND id = ?",
                                   (agent, str(s["id"]))).fetchone()
            self.con.execute(
                "INSERT INTO agent_sessions (agent, id, title, cwd, created, updated) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent, id) DO UPDATE SET title=COALESCE(excluded.title, title), "
                "cwd=COALESCE(excluded.cwd, cwd), created=MIN(COALESCE(created, excluded.created), "
                "COALESCE(excluded.created, created)), updated=MAX(COALESCE(updated, 0), COALESCE(excluded.updated, 0))",
                (agent, str(s["id"]), title, cwd, created, updated))
            # A new session with a name or folder, or one whose title or folder moved.
            if (title or cwd) and (old is None or (title and title != old[0]) or (cwd and cwd != old[1])):
                self.sessions_changed += 1
        return n

    def info(self):
        return [{"id": r.id, "name": r.name, "count_unit": r.count_unit, "tokens": r.tokens} for r in self.readers]


# ---------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------

def summarize_agents(con, lock, t0, t1, unit, bin_start, project_of, rename=None, claude=None,
                     readers=None, progress=None, top_tasks=12):
    """Per agent with data in [t0, t1) (or files on this Mac): Cursor's summary shape plus
    `timeline` (bins of `unit`, from `bin_start(ms, unit)`), `count_unit` and `status`."""
    readers = readers if readers is not None else [cls() for cls in READERS]
    meta = {r.id: r for r in readers}
    with lock:
        rows = con.execute(
            "SELECT agent, ts, session, model, inp, out, cread, cwrite, usd FROM agent_calls "
            "WHERE ts >= ? AND ts < ?", (t0, t1)).fetchall()
        sess = {(r[0], r[1]): r[2:] for r in con.execute(
            "SELECT agent, id, title, cwd, created, updated FROM agent_sessions")}
        ever = {r[0] for r in con.execute("SELECT DISTINCT agent FROM agent_calls")}
    by_agent = {}
    for row in rows:
        by_agent.setdefault(row[0], []).append(row)
    out = []
    for aid, r in meta.items():
        st = dict((progress or {}).get(aid) or {})
        arows = by_agent.get(aid, [])
        if not arows and aid not in ever and not st.get("available"):
            continue
        out.append(_summarize_one(r, arows, sess, unit, bin_start, project_of, rename, claude, st, top_tasks))
    out.sort(key=lambda a: (-a["totals"]["usd"], -a["totals"]["requests"], a["id"]))
    return out


def _summarize_one(reader, rows, sess, unit, bin_start, project_of, rename, claude, status, top_tasks):
    aid = reader.id
    tasks, days, hours, bins, models = {}, {}, {}, {}, {}
    day_of = {}  # 15-minute bucket -> local day: every UTC offset is a multiple of 15 min
    totals = {"usd": 0.0, "tokens": 0, "inp": 0, "out": 0, "cread": 0, "cwrite": 0, "requests": 0,
              "unpriced": 0, "tool_cost": 0}
    for _a, ts, sid, model, inp, out, cread, cwrite, usd in rows:
        tok = (inp or 0) + (out or 0) + (cread or 0) + (cwrite or 0)
        if usd and _num(usd) is not None:  # a sane positive recorded cost; a stored 0 (release 101) gets list price
            cost = usd
            totals["tool_cost"] += 1
        elif tok:
            cost = api_cost(model, inp or 0, out or 0, cread or 0, cwrite or 0, claude)
            if cost is None:
                totals["unpriced"] += 1
                cost = 0.0
        else:
            cost = 0.0
        totals["usd"] += cost
        totals["tokens"] += tok
        totals["requests"] += 1
        for k, v in (("inp", inp), ("out", out), ("cread", cread), ("cwrite", cwrite)):
            totals[k] += v or 0
        hb = ts - ts % 3600000
        qb = ts // 900000
        day = day_of.get(qb)
        if day is None:
            day = day_of[qb] = local_day(ts)
        t = tasks.setdefault(sid or "-", {"usd": 0.0, "tokens": 0, "requests": 0, "last": 0, "first": ts, "models": {},
                                          "days": set()})
        t["days"].add(day)
        t["usd"] += cost
        t["tokens"] += tok
        t["requests"] += 1
        t["last"] = max(t["last"], ts)
        t["first"] = min(t["first"], ts)
        mname = norm_model(model) if model else None
        if mname:
            t["models"][mname] = t["models"].get(mname, 0) + 1
            m = models.setdefault(mname, {"model": mname, "requests": 0, "turns": 0, "usd": 0.0, "tokens": 0})
            m["requests"] += 1
            m["usd"] += cost
            m["tokens"] += tok
        dd = days.setdefault(day, {"t": day, "turns": 0, "requests": 0, "usd": 0.0, "tokens": 0, "convs": set()})
        dd["requests"] += 1
        dd["usd"] += cost
        dd["tokens"] += tok
        dd["convs"].add(sid)
        hh = hours.setdefault(hb, {"t": hb, "requests": 0, "usd": 0.0, "tokens": 0})
        hh["requests"] += 1
        hh["usd"] += cost
        hh["tokens"] += tok
        bs = bin_start(ts, unit)
        bb = bins.setdefault(bs, {"t": bs, "usd": 0.0, "tokens": 0, "calls": 0})
        bb["usd"] += cost
        bb["tokens"] += tok
        bb["calls"] += 1
    by_days = reader.count_unit == "days"
    if by_days:
        # Activity-only agents count distinct local days, never conversation-days (audit of 101).
        totals["requests"] = len(days)
        for t in tasks.values():
            t["requests"] = len(t["days"])
        for dd in days.values():
            dd["requests"] = 1
    projects = {}
    pdays = {}
    for sid, t in tasks.items():
        s = sess.get((aid, sid))
        title, cwd = (s[0], s[1]) if s else (None, None)
        name_code = None
        if cwd:
            pname = project_of(cwd)
            if rename:
                pname = rename(pname)
        else:
            pname, name_code = "no folder", "no_folder"
        p = projects.setdefault(pname, {"name": pname, "turns": 0, "requests": 0, "convs": 0, "usd": 0.0,
                                        "tokens": 0, "last": 0, "tasks": []})
        if name_code:
            p["name_code"] = name_code
        p["requests"] += t["requests"]
        pdays.setdefault(pname, set()).update(t["days"])
        p["convs"] += 1
        p["usd"] += t["usd"]
        p["tokens"] += t["tokens"]
        p["last"] = max(p["last"], t["last"])
        model = max(t["models"].items(), key=lambda kv: kv[1])[0] if t["models"] else None
        p["tasks"].append({"key": "%s:%s" % (aid, sid), "title": title, "title_code": None if title else "untitled",
                           "model": model, "source": aid, "turns": 0, "requests": t["requests"],
                           "usd": round(t["usd"], 4), "tokens": t["tokens"], "last": t["last"]})
    plist = []
    for p in projects.values():
        if by_days:
            p["requests"] = len(pdays[p["name"]])
        p["tasks"].sort(key=lambda x: (-x["usd"], -x["requests"], -x["last"]))
        p["task_count"] = len(p["tasks"])
        rest = sorted(p["tasks"][top_tasks:], key=lambda x: -x["last"])[:5]
        p["tasks"] = p["tasks"][:top_tasks] + rest
        p["usd"] = round(p["usd"], 4)
        plist.append(p)
    plist.sort(key=lambda p: (-p["usd"], -p["requests"], -p["last"]))
    totals["usd"] = round(totals["usd"], 4)
    totals["convs"] = len(tasks)
    totals["turns"] = 0
    return {
        "id": aid, "name": reader.name, "count_unit": reader.count_unit, "has_tokens": reader.tokens,
        "status": {"available": bool(status.get("available")), "files": status.get("files", 0),
                   "skipped": status.get("skipped", 0), "error": status.get("error")},
        "totals": totals,
        "projects": plist,
        "days": [{"t": d["t"], "turns": 0, "requests": d["requests"], "usd": round(d["usd"], 4),
                  "tokens": d["tokens"], "convs": len(d["convs"])} for _k, d in sorted(days.items())],
        "hours": [dict(h, usd=round(h["usd"], 4)) for _k, h in sorted(hours.items())],
        "timeline": [dict(b, usd=round(b["usd"], 4)) for _k, b in sorted(bins.items())],
        "models": sorted(({"model": m["model"], "requests": m["requests"], "turns": 0, "usd": round(m["usd"], 4),
                           "tokens": m["tokens"]} for m in models.values()), key=lambda x: (-x["usd"], -x["requests"])),
    }
