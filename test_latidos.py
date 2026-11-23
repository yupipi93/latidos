"""Tests for Latidos. Run from the repository root: python3 -m unittest -q test_latidos"""

import base64
import datetime as dt
import glob
import http.client
import io
import json
import os
import shutil
import socket
import sqlite3
import types
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# The tests never read the user's own config: an empty config folder, plain defaults.
_CONF = tempfile.mkdtemp(prefix="latidos-conf-")
os.environ["LATIDOS_CONFIG_DIR"] = _CONF
for _k in ("LATIDOS_CONFIG", "LATIDOS_BOARDS", "LATIDOS_ALIASES", "LATIDOS_HOME_URL", "LATIDOS_LABEL",
           "LATIDOS_ROOTS", "XDG_CONFIG_HOME", "LATIDOS_HOST", "LATIDOS_ALLOW_REMOTE",
           "LATIDOS_ALLOWED_HOSTS"):
    os.environ.pop(_k, None)
# Variables of the old name are still read as fallbacks: none may leak in either.
for _k in [k for k in os.environ if k.startswith("LATIDO_")]:
    os.environ.pop(_k, None)
# Nor the other agents' files (L-182): an empty home for their readers.
os.environ["LATIDOS_AGENTS_HOME"] = tempfile.mkdtemp(prefix="latidos-agents-")
import latidos  # noqa: E402
import cursor_source  # noqa: E402

H = latidos.HOME
# A made-up home folder of another (Linux) machine. Joined from parts so the readiness
# scan, which flags literal home paths, does not mistake it for a real one.
OTHER_HOME = os.path.join("/home", "old")


def assistant(mid, ts, usage, model="claude-opus-5-5", session="s1", cwd=None, branch="main",
              content=None):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "sessionId": session, "cwd": cwd or H + "/code/agent-skeleton",
        "gitBranch": branch, "requestId": "req_" + mid,
        "message": {"id": mid, "model": model, "usage": usage, "content": content or []},
    })


U = {"input_tokens": 10, "output_tokens": 100, "cache_read_input_tokens": 1000,
     "cache_creation_input_tokens": 200,
     "cache_creation": {"ephemeral_1h_input_tokens": 200, "ephemeral_5m_input_tokens": 0}}


class Pricing(unittest.TestCase):
    def test_opus_55_rates(self):
        # 1M of each component at Opus 5.5 rates: 4 + 20 + 0.20 + 5 + 8
        self.assertAlmostEqual(latidos.call_cost("claude-opus-5-5", 1e6, 1e6, 1e6, 1e6, 1e6), 37.2)

    def test_prefix_does_not_confuse_versions(self):
        self.assertEqual(latidos.price_row("claude-opus-5")[0], "claude-opus-5")
        self.assertEqual(latidos.price_row("claude-opus-5-5")[0], "claude-opus-5-5")
        self.assertEqual(latidos.price_row("claude-sonnet-5")[0], "claude-sonnet-5")
        self.assertEqual(latidos.price_row("claude-haiku-4-5-20251001")[0], "claude-haiku-4-5")
        self.assertEqual(latidos.price_row("claude-opus-4-1-20250805")[0], "claude-opus-4")

    def test_fast_doubles(self):
        a = latidos.call_cost("claude-opus-5-5", 0, 1e6, 0, 0, 0)
        self.assertAlmostEqual(latidos.call_cost("claude-opus-5-5", 0, 1e6, 0, 0, 0, fast=True), 2 * a)

    def test_haiku_long_prompt_card(self):
        short = latidos.call_cost("claude-haiku-5-5", 50000, 0, 0, 0, 0)
        long_ = latidos.call_cost("claude-haiku-5-5", 150000, 0, 0, 0, 0)
        self.assertAlmostEqual(short, 0.005)
        self.assertAlmostEqual(long_, 0.075)

    def test_unknown_model_falls_back_by_family(self):
        self.assertEqual(latidos.price_row("claude-sonnet-9")[0], "claude-sonnet-5-5")


class Parsing(unittest.TestCase):
    def test_call_line(self):
        kind, row, hints = latidos.parse_line(assistant("m1", "2026-11-12T10:00:00Z", U))
        self.assertEqual(kind, "call")
        self.assertEqual(row[0], "m1")
        self.assertEqual(row[11], 200)  # 1h write
        self.assertEqual(row[10], 0)
        self.assertEqual(hints, [])

    def test_cache_split_missing_counts_as_5m(self):
        u = dict(U)
        u.pop("cache_creation")
        _k, row, _h = latidos.parse_line(assistant("m1", "2026-11-12T10:00:00Z", u))
        self.assertEqual((row[10], row[11]), (200, 0))

    def test_synthetic_and_other_types_ignored(self):
        self.assertIsNone(latidos.parse_line(assistant("m1", "2026-11-12T10:00:00Z", U, model="<synthetic>")))
        self.assertIsNone(latidos.parse_line('{"type":"user","usage":{}}'))
        self.assertIsNone(latidos.parse_line("not json usage"))

    def test_titles(self):
        line = json.dumps({"type": "custom-title", "customTitle": "DEMO-42 fix", "sessionId": "s"})
        self.assertEqual(latidos.parse_line(line), ("title", "s", "custom_title", "DEMO-42 fix"))
        junk = json.dumps({"type": "custom-title", "customTitle": "[UTF-8] abc toolu_1", "sessionId": "s"})
        self.assertEqual(latidos.parse_line(junk)[3], None)

    def test_hints_from_tool_paths(self):
        content = [
            {"type": "tool_use", "id": "t1", "input": {"file_path": H + "/.treehouse/infra-repo-1a2b3c/1/main.tf"}},
            {"type": "tool_use", "id": "t2", "input": {"command": "cd local/bundles/demo/apps/demo-app/ && ls"}},
            {"type": "tool_use", "id": "t3", "input": {"file_path": H + "/.agent-pool/projects/-Users-x/a.jsonl"}},
            {"type": "text", "text": "projects/nope/"},
        ]
        self.assertEqual(latidos.hints_of(content), [("t1", "infra-repo"), ("t2", "demo-app")])

    def test_ticket(self):
        self.assertEqual(latidos.ticket_in("fix/x-DEMO-42"), "DEMO-42")
        self.assertIsNone(latidos.ticket_in("UTF-8 text"))
        self.assertIsNone(latidos.ticket_in(None, ""))


class Projects(unittest.TestCase):
    def test_paths(self):
        cases = {
            H + "/code/agent-skeleton": "agent-skeleton",
            H + "/code/agent-skeleton/local/bundles/demo": "agent-skeleton",
            H + "/code/agent-skeleton/projects/repo-z/sub": "repo-z",
            H + "/.treehouse/demo-hub-1f5788/1": "demo-hub",
            H + "/.t3/worktrees/agent-skeleton-t3-8c08b0ac": "agent-skeleton",
            "/private/tmp/claude-502/" + latidos._enc(H + "/code/agent-skeleton") + "/uuid/scratchpad": "agent-skeleton",
            "/private/var/folders/ab/x/T/job": "automatismos",
            H + "/.local/state/demo-job-cwd": "automatismos",
            H: "home",
        }
        latidos._project_cache.clear()
        for cwd, want in cases.items():
            self.assertEqual(latidos.project_of(cwd), want, cwd)

    def test_root_and_system_folders_are_automation(self):
        latidos._project_cache.clear()
        for cwd in ("/", "/usr/local/bin", "/Library/Foo", "/opt/homebrew"):
            self.assertEqual(latidos.project_of(cwd), "automatismos", cwd)

    def test_aliases_rename_projects(self):
        old = dict(latidos.ALIASES)
        os.environ["LATIDOS_ALIASES_FROZEN"] = "1"
        latidos.ALIASES.clear()
        latidos.ALIASES["repo-x"] = "Repo X"
        latidos._project_cache.clear()
        try:
            # Stored names stay original; only what is shown is renamed.
            self.assertEqual(latidos.project_of(H + "/.treehouse/repo-x-abcdef/1"), "repo-x")
            self.assertEqual(latidos.attribute_project("repo-x", None, None, {}), "Repo X")
            self.assertEqual(latidos.attribute_project("agent-skeleton", {"repo-x": 5}, None, {}), "Repo X")
        finally:
            os.environ.pop("LATIDOS_ALIASES_FROZEN", None)
            latidos.ALIASES.clear(); latidos.ALIASES.update(old); latidos._project_cache.clear()

    def test_alias_file_reloads_and_drops_caches(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "aliases.json")
        with open(path, "w") as fh:
            json.dump({"repo-x": "Repo X"}, fh)
        calls = []
        old_env = os.environ.get("LATIDOS_ALIASES")
        os.environ["LATIDOS_ALIASES"] = path
        latidos.ALIAS_LISTENERS.append(lambda: calls.append(1))
        try:
            latidos._aliases_seen.update(mtime=None, checked=0.0)
            self.assertEqual(latidos.alias("repo-x"), "Repo X")
            with open(path, "w") as fh:
                json.dump({"repo-x": "Repo Y"}, fh)
            os.utime(path, (time.time() + 10, time.time() + 10))
            latidos._aliases_seen["checked"] = 0.0
            self.assertEqual(latidos.alias("repo-x"), "Repo Y")
            self.assertGreaterEqual(len(calls), 2)
        finally:
            latidos.ALIAS_LISTENERS.pop()
            if old_env is None:
                os.environ.pop("LATIDOS_ALIASES", None)
            else:
                os.environ["LATIDOS_ALIASES"] = old_env
            latidos._aliases_seen.update(mtime=None, checked=0.0)
            latidos.alias("x")
            shutil.rmtree(tmp)

    def _alias_env(self, path):
        old_env = os.environ.get("LATIDOS_ALIASES")
        os.environ["LATIDOS_ALIASES"] = path

        def restore():
            if old_env is None:
                os.environ.pop("LATIDOS_ALIASES", None)
            else:
                os.environ["LATIDOS_ALIASES"] = old_env
            latidos._aliases_seen.update(mtime=None, checked=0.0)
            latidos.alias("x")
        self.addCleanup(restore)
        latidos._aliases_seen.update(mtime=None, checked=0.0)

    def test_a_deleted_alias_file_drops_its_names(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "aliases.json")
        with open(path, "w") as fh:
            json.dump({"repo-x": "Repo X"}, fh)
        self._alias_env(path)
        self.assertEqual(latidos.alias("repo-x"), "Repo X")
        os.unlink(path)
        latidos._aliases_seen["checked"] = 0.0
        self.assertEqual(latidos.alias("repo-x"), "repo-x")

    def test_a_new_aliases_path_is_read(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        first, second = os.path.join(tmp, "a.json"), os.path.join(tmp, "b.json")
        with open(first, "w") as fh:
            json.dump({"repo-x": "Repo A"}, fh)
        with open(second, "w") as fh:
            json.dump({"repo-x": "Repo B"}, fh)
        os.utime(second, os.stat(first)[7:9])  # same mtime: only the path differs
        self._alias_env(first)
        self.assertEqual(latidos.alias("repo-x"), "Repo A")
        os.environ["LATIDOS_ALIASES"] = second
        latidos._aliases_seen["checked"] = 0.0
        self.assertEqual(latidos.alias("repo-x"), "Repo B")

    def test_attribution_rule(self):
        boards = {"DEMO": "Demo Client"}
        self.assertEqual(latidos.attribute_project("agent-skeleton", {"repo-x": 5}, "DEMO-1", boards), "repo-x")
        self.assertEqual(latidos.attribute_project("agent-skeleton", None, "DEMO-1", boards), "Demo Client")
        self.assertEqual(latidos.attribute_project("agent-skeleton", None, None, boards), "agent-skeleton")
        self.assertEqual(latidos.attribute_project("demo-app", None, "DEMO-1", boards), "demo-app")

    def test_dominant_hint(self):
        self.assertEqual(latidos.dominant_hint({"a": 5, "b": 2}), "a")
        self.assertIsNone(latidos.dominant_hint({"a": 2}))          # too few
        self.assertIsNone(latidos.dominant_hint({"a": 4, "b": 4, "c": 4}))  # no majority
        self.assertIsNone(latidos.dominant_hint(None))


class Blocks(unittest.TestCase):
    def test_blocks_start_on_the_hour_and_last_five(self):
        base = 1794470400000  # an exact hour
        rows = [{"ts": base + 25 * 60000, "usd": 1}, {"ts": base + 4 * 3600000, "usd": 2},
                {"ts": base + 5 * 3600000 + 1, "usd": 4}]
        self.assertEqual(latidos.session_blocks(rows), [(base, 3), (base + 5 * 3600000, 4)])

    def test_coverage_block_and_week_caps(self):
        base = 1794470400000
        blocks = [(base, 10.0), (base + 5 * 3600000, 10.0), (base + 10 * 3600000, 10.0)]
        cov = latidos.coverage(blocks, 1.0, [5.0], pro_block=6.0, pro_week=15.0)
        # 6 + 6 + 3 (week cap 15 left 3)
        self.assertAlmostEqual(cov["main_usd"], 15.0)
        self.assertEqual(cov["short_blocks"], 3)
        self.assertAlmostEqual(cov["share_main"], 0.5)

    def test_week_balance_spent_before_the_range_stays_spent(self):
        base = 1794470400000
        blocks = [(base, 15.0), (base + 5 * 3600000, 10.0)]
        # Only the second block is in range; the week (cap 15) was used up by the first.
        cov = latidos.coverage(blocks, 1.0, [5.0], pro_block=20.0, pro_week=15.0, in_range=[0, 10.0])
        self.assertAlmostEqual(cov["main_usd"], 0.0)
        self.assertAlmostEqual(cov["total_usd"], 10.0)
        self.assertEqual((cov["blocks"], cov["short_blocks"], cov["weeks"]), (1, 1, 1))
        self.assertAlmostEqual(cov["week_main_usd"], 15.0)
        self.assertAlmostEqual(cov["week_rest_usd"], 10.0)

    def test_straddling_block_counts_in_proportion(self):
        base = 1794470400000
        cov = latidos.coverage([(base, 10.0)], 1.0, [5.0], pro_block=4.0, pro_week=100.0,
                              in_range=[5.0])
        self.assertAlmostEqual(cov["total_usd"], 5.0)
        self.assertAlmostEqual(cov["main_usd"], 2.0)

    def test_block_shares(self):
        base = 1794470400000
        blocks = [(base, 3.0), (base + 6 * 3600000, 4.0)]
        rows = [{"ts": base + 3600000, "usd": 2.0}, {"ts": base + 6 * 3600000 + 5, "usd": 4.0}]
        self.assertEqual(latidos.block_shares(blocks, rows), [2.0, 4.0])

    def test_calibrate(self):
        base = 1794470400000
        b, w = latidos.calibrate([(base, 27.5), (base + 5 * 3600000, 13.75)], 13.75)
        self.assertAlmostEqual(b, 2.0)
        self.assertAlmostEqual(w, 3.0)


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects", "p")
        os.makedirs(self.root)
        os.environ["LATIDOS_ROOTS"] = os.path.join(self.tmp, "projects")
        os.environ["LATIDOS_DB"] = os.path.join(self.tmp, "db", "l.db")
        os.environ["LATIDOS_FX_URL"] = "http://127.0.0.1:9/never"
        self.app = latidos.App(latidos.config())
        self.f = os.path.join(self.root, "s1.jsonl")

    def tearDown(self):
        for k in ("LATIDOS_ROOTS", "LATIDOS_DB", "LATIDOS_FX_URL"):
            os.environ.pop(k, None)
        self.app.con.close()
        shutil.rmtree(self.tmp)

    def rows(self):
        return self.app.con.execute("SELECT msg_id, out FROM calls ORDER BY msg_id").fetchall()

    def test_dedupe_and_incremental(self):
        small = dict(U, output_tokens=5)
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", small) + "\n")
            fh.write(assistant("m1", "2026-11-12T10:00:01Z", U) + "\n")  # same message, final usage
            fh.write(assistant("m2", "2026-11-12T10:01:00Z", U)[:40])     # partial line: not yet
        self.app.ingestor.scan()
        self.assertEqual(self.rows(), [("m1", 100)])
        with open(self.f, "a") as fh:
            fh.write(assistant("m2", "2026-11-12T10:01:00Z", U)[40:] + "\n")
        self.app.ingestor.scan()
        self.assertEqual(self.rows(), [("m1", 100), ("m2", 100)])
        # A rescan with nothing new changes nothing.
        self.assertEqual(self.app.ingestor.scan(), 0)

    def test_bad_line_does_not_block_the_file(self):
        bad_num = assistant("m9", "2026-11-12T10:00:30Z", dict(U, output_tokens="x"))
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
            fh.write('["usage"]\n')
            fh.write(bad_num + "\n")
            fh.write(assistant("m2", "2026-11-12T10:01:00Z", U) + "\n")
        self.app.ingestor.scan()
        self.assertEqual(self.rows(), [("m1", 100), ("m2", 100)])
        self.assertIsNone(self.app.ingestor.progress["error"])

    def test_live_numbers_never_come_from_the_cache(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", now, U) + "\n")
        self.app.ingestor.scan()
        t1 = int(time.time() * 1000) + 60000
        self.assertEqual(self.app.summary(0, t1, None)["live"]["calls_15m"], 1)
        real = time.time
        try:
            latidos.time.time = lambda: real() + 3600
            again = self.app.summary(0, t1, None)
        finally:
            latidos.time.time = real
        self.assertEqual(again["live"]["calls_15m"], 0)

    def test_requests_serve_the_cache_the_background_refreshes(self):
        t1 = int(time.time() * 1000) + 60000
        first = self.app.summary(0, t1, None)
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
        self.app.ingestor.scan()
        self.app.cache[next(iter(self.app.cache))] = (0, time.time() * 1000 - 30000, first)
        self.assertEqual(self.app.summary(0, t1, None)["totals"]["calls"], 0)   # request: cached
        self.assertEqual(self.app.summary(0, t1, None, background=True)["totals"]["calls"], 1)
        self.assertEqual(self.app.summary(0, t1, None)["totals"]["calls"], 1)

    def _stub_cursor(self, sig):
        calls = {"scan": 0, "api": 0}

        class Fake:
            progress = {}
            def signature(self_):
                return sig[0]
            def scan(self_):
                calls["scan"] += 1
        class FakeApi:
            progress = {}
            def scan(self_):
                calls["api"] += 1
        self.app.cursor = Fake()
        self.app.cursor_api = FakeApi()
        return calls

    def test_idle_tick_precomputes_nothing_and_a_request_still_answers(self):
        self._stub_cursor([("a",)])
        now = time.time()
        self.assertFalse(self.app.viewer_active(now))
        self.assertFalse(self.app.tick(now))
        self.assertEqual(self.app.cache, {})                  # no viewer: no warm ranges
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
        self.app.ingestor.scan()
        t0, t1 = latidos.range_bounds("todo", int(now * 1000))
        self.assertEqual(self.app.summary(t0, t1, None, name="todo")["totals"]["calls"], 1)  # on demand

    def test_a_request_wakes_the_loop_and_keeps_ranges_warm(self):
        self._stub_cursor([("a",)])
        now = time.time()
        self.app.touch(now)
        self.assertTrue(self.app.wake.is_set())               # first request after idle wakes it
        self.app.wake.clear()
        self.app.touch(now + 1)
        self.assertFalse(self.app.wake.is_set())              # already active: no extra wake
        self.assertTrue(self.app.tick(now + 2))
        self.assertEqual(len([k for k in self.app.cache if k[0] == "r"]), len(latidos.NAMED_RANGES))
        self.assertFalse(self.app.viewer_active(now + 1 + self.app.VIEWER_WINDOW_S))
        self.assertFalse(self.app.tick(now + 1 + self.app.VIEWER_WINDOW_S))

    def test_health_checks_do_not_count_as_a_viewer(self):
        srv = latidos.Server(("127.0.0.1", 0), latidos.make_handler(self.app))
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            port = srv.server_address[1]
            self.app.cfg["port"] = port
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            c.request("GET", "/api/health"); c.getresponse().read()
            self.assertFalse(self.app.viewer_active())
            c.request("GET", "/api/live?since=0"); c.getresponse().read()
            self.assertTrue(self.app.viewer_active())
        finally:
            srv.shutdown(); srv.server_close()

    def test_cursor_scan_only_when_its_files_changed_and_not_too_often(self):
        sig = [("a",)]
        calls = self._stub_cursor(sig)
        t = 1_000_000.0
        self.app.tick(t)
        self.assertEqual(calls["scan"], 1)                    # first pass reads it
        self.app.tick(t + self.app.CURSOR_SCAN_S + 1)
        self.assertEqual(calls["scan"], 1)                    # unchanged files: skipped
        sig[0] = ("b",)
        self.app.tick(t + self.app.CURSOR_SCAN_S + 2)
        self.assertEqual(calls["scan"], 1)                    # changed, but too soon after the check
        self.app.tick(t + 2 * self.app.CURSOR_SCAN_S + 2)
        self.assertEqual(calls["scan"], 2)                    # changed and due

    def test_a_failed_cursor_scan_is_retried_without_a_new_change(self):
        sig = [("a",)]
        calls = self._stub_cursor(sig)
        t = 1_000_000.0
        self.app.cursor.progress["error"] = "cursor: database is locked"
        self.app.tick(t)
        self.assertEqual(calls["scan"], 1)
        self.app.cursor.progress["error"] = None
        self.app.tick(t + self.app.CURSOR_SCAN_S + 1)
        self.assertEqual(calls["scan"], 2)                    # same files, but the last read failed
        self.app.tick(t + 2 * self.app.CURSOR_SCAN_S + 2)
        self.assertEqual(calls["scan"], 2)                    # that retry worked: skip again

    def test_a_permanent_cursor_error_stops_retrying_until_the_files_change(self):
        sig = [("a",)]
        calls = self._stub_cursor(sig)
        self.app.cursor.progress["error"] = "cursor: malformed row"
        t = 1_000_000.0
        for i in range(self.app.CURSOR_RETRIES + 3):
            self.app.tick(t + i * (self.app.CURSOR_SCAN_S + 1))
        self.assertEqual(calls["scan"], 1 + self.app.CURSOR_RETRIES)   # first read + capped retries
        sig[0] = ("b",)
        self.app.tick(t + 99 * (self.app.CURSOR_SCAN_S + 1))
        self.assertEqual(calls["scan"], 2 + self.app.CURSOR_RETRIES)   # a real change is still read
        self.app.tick(t + 100 * (self.app.CURSOR_SCAN_S + 1))
        self.assertEqual(calls["scan"], 3 + self.app.CURSOR_RETRIES)   # and gets its own retries

    def test_loop_waits_longer_when_nobody_watches(self):
        waits = []

        class Stop:
            n = 0
            def is_set(self_):
                return Stop.n >= 2
            def wait(self_, s):
                waits.append(s); Stop.n += 1
        self.app.ingestor.loop(Stop(), None, active=lambda: Stop.n == 1)
        self.assertEqual(waits, [self.app.cfg["scan_seconds"], self.app.ingestor.ACTIVE_SCAN_S])
        self.assertEqual(latidos.config()["scan_seconds"], 30)

    def test_fx_offline_does_not_retry_every_call(self):
        calls = []
        orig = latidos.urllib.request.urlopen

        def boom(*a, **k):
            calls.append(1)
            raise OSError("offline")
        latidos.urllib.request.urlopen = boom
        try:
            self.app.fx.refresh()
            self.app.fx.refresh()
            self.app.fx.refresh()
            fx = self.app.fx.get()
        finally:
            latidos.urllib.request.urlopen = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(fx["source"], "fijo")

    def test_fx_get_never_uses_the_network(self):
        orig = latidos.urllib.request.urlopen

        def boom(*a, **k):
            raise AssertionError("network from a request path")
        latidos.urllib.request.urlopen = boom
        try:
            self.assertEqual(self.app.fx.get()["source"], "fijo")
        finally:
            latidos.urllib.request.urlopen = orig

    def test_scan_error_is_cleared_by_the_next_scan(self):
        self.app.ingestor.progress["error"] = "old: boom"
        self.app.ingestor.scan()
        self.assertIsNone(self.app.ingestor.progress["error"])

    def test_span_days_is_the_real_elapsed_time(self):
        two_h_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 7200))
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", two_h_ago, U) + "\n")
        self.app.ingestor.scan()
        now = int(time.time() * 1000)
        d = self.app.summary(now - 3 * 3600000, now + 60000, None)
        self.assertAlmostEqual(d["accounts"]["span_days"], 2 / 24.0, places=2)  # from the first call, not the range start

    def test_version_lives_in_the_database(self):
        first = self.app.ingestor.version
        self.assertGreater(first, 1700000000)  # starts at creation time, not 0
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
        # Another process (a hand-run ingest) writes: the server's version still moves.
        other = latidos.Ingestor(self.app.cfg, latidos.connect(self.app.cfg["db"]), threading.RLock())
        other.scan()
        other.con.close()
        self.assertGreater(self.app.ingestor.version, first)
        now = int(time.time() * 1000)
        d = self.app.summary(0, now + 60000, None)
        self.assertAlmostEqual(d["accounts"]["total_usd"], d["totals"]["usd"], places=3)

    def test_summary_reports_the_seq_it_includes(self):
        self.app.ingestor.scan()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", now, U) + "\n")
        self.app.ingestor.scan()
        ev = self.app.ingestor.recent[-1][1]
        d = self.app.summary(0, int(time.time() * 1000) + 60000, None)
        self.assertEqual(ev["seq"], 1)
        self.assertEqual(d["seq_at"], 1)
        self.assertEqual(d["totals"]["calls"], 1)

    def _logins(self):
        """Two invented accounts: a config folder and a bare .claude.json."""
        a = os.path.join(self.tmp, "acc-a")
        os.makedirs(a)
        with open(os.path.join(a, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": "uuid-a", "emailAddress": "a@example.com"}}, fh)
        b = os.path.join(self.tmp, "b.json")
        with open(b, "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": "uuid-b"}}, fh)
        return {"work": a, "spare": b}

    def test_default_config_folder_uses_the_file_next_to_it(self):
        d = os.path.join(self.tmp, ".claude")
        os.makedirs(d)
        with open(d + ".json", "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": "uuid-d"}}, fh)
        self.assertEqual(latidos.account_map({"main": d + "/"}), {"uuid-d": "main"})
        self.assertEqual(latidos.account_map({"none": os.path.join(self.tmp, "missing")}), {})

    def test_calls_take_the_owner_in_force_on_their_line(self):
        # L-122: a chat moved to another account mid-way credits each call to its account,
        # a later incremental read still knows the owner, a subagent file takes its session's.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(json.dumps({"type": "session-owner", "accountUuid": "uuid-a", "sessionId": "s1"}) + "\n")
            fh.write(assistant("m1", now, U) + "\n")
        self.app.ingestor.scan()
        with open(self.f, "a") as fh:
            fh.write(assistant("m2", now, U) + "\n")
            fh.write(json.dumps({"type": "session-owner", "accountUuid": "uuid-b", "sessionId": "s1"}) + "\n")
            fh.write(assistant("m3", now, U) + "\n")
        sub = os.path.join(self.root, "s1", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-x.jsonl"), "w") as fh:
            fh.write(assistant("m4", now, U) + "\n")
        os.utime(os.path.join(sub, "agent-x.jsonl"), (time.time() + 5, time.time() + 5))
        with unittest.mock.patch.object(latidos, "ACCOUNT_LOGINS", self._logins()):
            self.app.ingestor.scan()
            got = dict(self.app.con.execute("SELECT msg_id, account FROM calls"))
            self.assertEqual(got, {"m1": "uuid-a", "m2": "uuid-a", "m3": "uuid-b", "m4": "uuid-b"})
            # The first scan is the backfill (no live events): m2, m3 and m4 are live.
            ev = sorted(e["account"] for _s, e in self.app.ingestor.recent)
            self.assertEqual(ev, ["spare", "spare", "work"])
            use = latidos.account_use(self.app.con, self.app.lock)
            self.assertEqual(use["work"]["calls"], 2)
            self.assertEqual(use["spare"]["calls"], 2)
        # No logins configured: nothing is mapped, live events carry no account.
        self.assertEqual(latidos.account_use(self.app.con, self.app.lock), {})
        self.assertEqual(latidos.DEFAULT_SETTINGS["account_logins"], {})

    def test_subagent_read_before_its_session_takes_its_account(self):
        # Audit of 64: a subagent file is usually older than its session's file, so it is
        # read first, before the session's owner is known; its calls are filled in when the
        # session file is read (and calls before the first owner record too).
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        sub = os.path.join(self.root, "s1", "subagents")
        os.makedirs(sub)
        sfile = os.path.join(sub, "agent-x.jsonl")
        with open(sfile, "w") as fh:
            fh.write(assistant("m4", now, U) + "\n")
        with open(self.f, "w") as fh:
            fh.write(assistant("m0", now, U) + "\n")
            fh.write(json.dumps({"type": "session-owner", "accountUuid": "uuid-a", "sessionId": "s1"}) + "\n")
            fh.write(assistant("m1", now, U) + "\n")
        os.utime(sfile, (time.time() - 60, time.time() - 60))
        self.app.ingestor.scan()
        got = dict(self.app.con.execute("SELECT msg_id, account FROM calls"))
        self.assertEqual(got, {"m0": "uuid-a", "m1": "uuid-a", "m4": "uuid-a"})

    def test_reopening_fills_calls_left_without_account(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", now, U) + "\n")
        self.app.ingestor.scan()
        with self.app.lock:
            self.app.con.execute("INSERT INTO sessions (session_id, account) VALUES ('s1', 'uuid-b')")
            self.app.con.commit()
        latidos.connect(os.environ["LATIDOS_DB"]).close()
        self.assertEqual(self.app.con.execute("SELECT account FROM calls").fetchone()[0], "uuid-b")

    def test_login_file_caught_mid_write_keeps_the_last_id(self):
        path = os.path.join(self.tmp, "l.json")
        with open(path, "w") as fh:
            json.dump({"oauthAccount": {"accountUuid": "uuid-l"}}, fh)
        self.assertEqual(latidos.account_map({"x": path}), {"uuid-l": "x"})
        with open(path, "w") as fh:
            fh.write('{"oauthAcc')
        os.utime(path, (time.time() + 10, time.time() + 10))
        self.assertEqual(latidos.account_map({"x": path}), {"uuid-l": "x"})

    def test_account_column_added_to_an_old_database(self):
        path = os.path.join(self.tmp, "old.db")
        con = sqlite3.connect(path)
        con.executescript("CREATE TABLE calls (msg_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, session_id TEXT, "
                          "model TEXT, cwd TEXT, branch TEXT, sidechain INTEGER, inp INTEGER, out INTEGER, "
                          "cread INTEGER, cw5 INTEGER, cw1h INTEGER, fast INTEGER, usd REAL);"
                          "CREATE TABLE files (path TEXT PRIMARY KEY, inode INTEGER, size INTEGER, mtime REAL, offset INTEGER);"
                          "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, custom_title TEXT, ai_title TEXT, first_prompt TEXT);")
        con.execute("INSERT INTO files VALUES ('new', 1, 10, ?, 10)", (time.time(),))
        con.execute("INSERT INTO files VALUES ('old', 1, 10, 1000, 10)")
        con.commit()
        con.close()
        con = latidos.connect(path)
        self.assertEqual(dict(con.execute("SELECT path, offset FROM files")), {"new": 0, "old": 10})
        self.assertIn("account", {r[1] for r in con.execute("PRAGMA table_info(calls)")})
        con.close()
        latidos.connect(path).close()  # a second open changes nothing

    def test_version_and_seq_describe_the_rows_of_the_summary(self):
        # A batch committed while a summary computes (after its rows were read) must not
        # show up in that summary's version.
        orig = latidos.load_rows
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
        self.app.ingestor.scan()

        def late_commit(*a, **k):
            rows = orig(*a, **k)
            with self.app.lock:
                self.app.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
                self.app.con.commit()
            return rows
        latidos.load_rows = late_commit
        try:
            d = self.app.summary(0, int(time.time() * 1000) + 60000, None)
        finally:
            latidos.load_rows = orig
        self.assertEqual(d["totals"]["calls"], 1)
        self.assertLess(d["version"], self.app.ingestor.version)
        self.assertIn("boot", d)

    def test_cache_key_includes_the_bin_unit(self):
        now = int(time.time() * 1000)
        day0 = now - 3600000
        a = self.app.summary(day0, now + 60000, None)
        b = self.app.summary(day0, now + 7 * 86400000, None)
        self.assertEqual(a["range"]["unit"], "hour")
        self.assertEqual(b["range"]["unit"], "day")

    def test_named_ranges_share_one_cache_entry(self):
        now = int(time.time() * 1000)
        t0, t1 = latidos.range_bounds("7d", now)
        self.assertEqual(t1 - t0, 7 * 86400000 + 60000)
        h0, _ = latidos.range_bounds("hoy", now)
        self.assertEqual(time.localtime(h0 / 1000)[3:6], (0, 0, 0))
        self.app.summary(*latidos.range_bounds("7d", now), None, name="7d")
        self.app.summary(*latidos.range_bounds("7d", now + 120000), None, name="7d")
        self.assertEqual(sum(1 for k in self.app.cache if k[:2] == ("r", "7d")), 1)

    def test_live_project_matches_the_summary(self):
        self.app.ingestor.scan()
        self.app.cfg["boards"] = {"DEMO": "Demo Client"}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(json.dumps({"type": "custom-title", "customTitle": "[DEMO-976] Launch PRO", "sessionId": "s1"}) + "\n")
            fh.write(assistant("m1", now, U, cwd=H + "/code/agent-skeleton") + "\n")
        self.app.ingestor.scan()
        live = self.app.ingestor.recent[-1][1]["project"]
        d = self.app.summary(0, int(time.time() * 1000) + 60000, None)
        self.assertEqual(live, "Demo Client")
        self.assertEqual(d["projects"][0]["name"], live)

    def test_live_project_is_shown_with_its_alias(self):
        os.environ["LATIDOS_ALIASES_FROZEN"] = "1"
        saved = dict(latidos.ALIASES)
        latidos.ALIASES.clear(); latidos.ALIASES["repo-x"] = "Repo X"
        try:
            self.assertEqual(self.app.ingestor.live_project("s9", H + "/.treehouse/repo-x-abcdef/1"), "Repo X")
        finally:
            os.environ.pop("LATIDOS_ALIASES_FROZEN", None)
            latidos.ALIASES.clear(); latidos.ALIASES.update(saved); latidos._project_cache.clear()

    def test_survives_deleted_logs(self):
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
        self.app.ingestor.scan()
        os.unlink(self.f)
        self.app.ingestor.scan()
        self.assertEqual(len(self.rows()), 1)

    def test_live_events_only_for_recent_new_calls(self):
        self.app.ingestor.scan()  # empty backfill
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(assistant("old", "2026-01-01T10:00:00Z", U) + "\n")
            fh.write(assistant("new", now, U) + "\n")
        self.app.ingestor.scan()
        self.assertEqual([e["tokens"] for _s, e in self.app.ingestor.recent], [1310])

    def test_newest_cheap_task_is_kept_past_the_top_12(self):
        # 14 sessions in one repo: 13 pricey old ones, then the newest and cheapest.
        big = dict(U, output_tokens=100000)
        with open(self.f, "w") as fh:
            for i in range(13):
                fh.write(assistant("o%d" % i, "2026-11-01T%02d:00:00Z" % (i + 1), big, session="old%d" % i,
                                   cwd=H + "/code/projects/repo-y") + "\n")
            fh.write(assistant("n1", "2026-11-12T10:00:00Z", U, session="new1", cwd=H + "/code/projects/repo-y") + "\n")
        self.app.ingestor.scan()
        d = self.app.summary(0, int(time.time() * 1000) + 60000, None)
        p = [x for x in d["projects"] if x["name"] == "repo-y"][0]
        self.assertEqual(p["task_count"], 14)
        self.assertEqual(len(p["tasks"]), 14)
        # The cheapest task is past the top 12 but kept, first among the recent extras.
        self.assertEqual(p["tasks"][12]["sids"], ["new1"])
        self.assertTrue(all(p["tasks"][i]["usd"] >= p["tasks"][i + 1]["usd"] for i in range(11)))

    def test_summary_and_pulse(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with open(self.f, "w") as fh:
            fh.write(assistant("m1", now, U, cwd=H + "/code/agent-skeleton",
                               content=[{"type": "tool_use", "id": "a%d" % i,
                                         "input": {"file_path": H + "/.treehouse/repo-x-abcdef/f"}}
                                        for i in range(3)]) + "\n")
            fh.write(json.dumps({"type": "custom-title", "customTitle": "Mi tarea", "sessionId": "s1"}) + "\n")
        self.app.ingestor.scan()
        d = self.app.summary(0, int(time.time() * 1000) + 60000, None)
        self.assertEqual(d["totals"]["calls"], 1)
        self.assertEqual(d["projects"][0]["name"], "repo-x")
        self.assertEqual(d["projects"][0]["tasks"][0]["title"], "Mi tarea")
        # A live call carries its session; the task lists its sessions so the page finds it.
        self.assertEqual(d["projects"][0]["tasks"][0]["sids"], ["s1"])
        self.assertEqual(d["fx"]["source"], "fijo")
        p = latidos.pulse(self.app.con, self.app.lock, 10)
        self.assertEqual(len(p["minutes"]), 10)
        self.assertEqual(sum(m[1] for m in p["minutes"]), 1310)


class HomeAccountsTest(unittest.TestCase):
    QUOTA = {"updated_at": "2026-11-13T14:44:39Z", "accounts": [
        {"tag": "work", "label": "Work", "email": "x@y.z", "active": True, "plan": "Team",
         "session": {"percent": 14}, "weekly": {"percent": 73}, "warmup": None}]}
    CURSOR = {"ok": True, "email": "x@y.z", "spend_usd": 12.5, "cycle_end": "2026-11-17T00:00:00Z"}

    class Opener:
        def __init__(self, answers):
            self.answers = answers
        def open(self, req, timeout=None):
            ans = self.answers[req.full_url.rsplit("/", 1)[-1]]
            if isinstance(ans, Exception):
                raise ans
            body = json.dumps(ans).encode()
            class R(io.BytesIO):
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return R(body)

    def test_reads_both_widgets_without_emails(self):
        h = latidos.HomeAccounts({"home_url": "http://127.0.0.1"},
                                opener=self.Opener({"claude-quota": self.QUOTA, "cursor-usage": self.CURSOR}))
        h.refresh()
        d = h.get()
        self.assertTrue(d["claude"]["accounts"][0]["active"])
        self.assertNotIn("email", d["claude"]["accounts"][0])
        self.assertNotIn("email", d["cursor"])
        self.assertEqual(d["cursor"]["spend_usd"], 12.5)

    def test_failed_read_keeps_old_numbers_marked_stale(self):
        answers = {"claude-quota": self.QUOTA, "cursor-usage": self.CURSOR}
        h = latidos.HomeAccounts({"home_url": "http://127.0.0.1"}, opener=self.Opener(answers))
        h.refresh()
        answers["claude-quota"] = OSError("down")
        h.refresh()
        self.assertTrue(h.data["claude"]["stale"])
        self.assertIn("quota", h.data["error"])
        self.assertEqual(h.data["error_code"], "fetch_failed")

    def test_only_a_local_launcher_is_read(self):
        h = latidos.HomeAccounts({"home_url": "https://example.com"}, opener=self.Opener({}))
        self.assertEqual(h.get()["error"], "no account source")
        self.assertEqual(h.get()["error_code"], "no_source")


    def test_off_by_default_and_colours_from_config(self):
        h = latidos.HomeAccounts({"home_url": "", "account_colors": {"work": "#123456"}},
                                opener=self.Opener({}))
        d = h.get()
        self.assertIsNone(d["claude"])
        self.assertEqual(d["colors"], {"work": "#123456"})
        self.assertEqual(latidos.DEFAULT_SETTINGS["accounts_url"], "")


class Settings(unittest.TestCase):
    def load(self, data=None, env=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "config.json")
        if data is not None:
            with open(path, "w") as fh:
                fh.write(data if isinstance(data, str) else json.dumps(data))
        saved = {k: os.environ.get(k) for k in ("LATIDOS_CONFIG", "LATIDOS_HOME_URL", "LATIDOS_LABEL")}
        os.environ["LATIDOS_CONFIG"] = path
        for k, v in (env or {}).items():
            os.environ[k] = v
        try:
            return latidos.load_settings()
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_defaults_without_a_file(self):
        s = self.load()
        self.assertEqual(s["roots"], ["~/.claude/projects"])
        self.assertEqual(s["workspaces"], ["agent-skeleton"])
        self.assertEqual(s["accounts_url"], "")
        self.assertEqual(s["label"], "local.latidos")
        self.assertEqual(s["projects_dir"], os.path.join(H, "code", "projects"))

    def test_file_values_and_env_overrides(self):
        s = self.load({"code_dir": "~/src", "workspaces": ["hub/"], "accounts_url": "http://127.0.0.1:9",
                       "label": "local.demo", "unknown": 1},
                      env={"LATIDOS_LABEL": "local.env"})
        self.assertEqual(s["code_dir"], os.path.join(H, "src"))
        self.assertEqual(s["projects_dir"], os.path.join(H, "src", "projects"))
        self.assertEqual(s["workspaces"], ["hub"])
        self.assertEqual(s["accounts_url"], "http://127.0.0.1:9")
        self.assertEqual(s["label"], "local.env")
        self.assertNotIn("unknown", s)
        self.assertEqual(self.load({"accounts_url": "http://127.0.0.1"},
                                   env={"LATIDOS_HOME_URL": ""})["accounts_url"], "")

    def test_a_broken_file_falls_back_to_defaults(self):
        self.assertEqual(self.quiet_load("{not json")[0]["label"], "local.latidos")
        self.assertEqual(self.quiet_load("[1, 2]")[0]["workspaces"], ["agent-skeleton"])

    def test_shipped_examples_load(self):
        conf = os.path.join(HERE, "config")
        with open(os.path.join(conf, "config.example.json")) as fh:
            s = self.load(fh.read())
        self.assertEqual(s["workspaces"], ["agent-skeleton"])
        for name in ("boards.example.json", "aliases.example.json"):
            with open(os.path.join(conf, name)) as fh:
                data = json.load(fh)
            self.assertTrue(data and all(isinstance(v, str) for v in data.values()), name)

    def test_boards_and_aliases_come_from_the_config_folder(self):
        self.assertEqual(latidos.config_file("boards.json"), os.path.join(_CONF, "boards.json"))
        os.environ["LATIDOS_BOARDS"] = "~/elsewhere.json"
        try:
            self.assertEqual(latidos.config_file("boards.json", "BOARDS"),
                             os.path.join(H, "elsewhere.json"))
        finally:
            os.environ.pop("LATIDOS_BOARDS", None)
        self.assertFalse(os.path.exists(os.path.join(HERE, "boards.json")))

    def test_roots_expand_globs(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for n in ("b", "a"):
            os.mkdir(os.path.join(tmp, n))
        self.assertEqual(latidos.expand_roots([tmp + "/*", "/x/y"]),
                         [os.path.join(tmp, "a"), os.path.join(tmp, "b"), "/x/y"])


    def quiet_load(self, data):
        err = io.StringIO()
        with redirect_stderr(err):
            out = self.load(data)
        return out, err.getvalue()

    def test_a_string_stands_for_a_one_item_list(self):
        s, err = self.quiet_load({"roots": "~/logs", "workspaces": "hub"})
        self.assertEqual(s["roots"], ["~/logs"])
        self.assertEqual(s["workspaces"], ["hub"])
        self.assertEqual(err, "")

    def test_wrong_types_keep_the_defaults_with_a_warning(self):
        s, err = self.quiet_load({"roots": {"a": 1}, "workspaces": 7, "code_dir": ["x"],
                                  "projects_dir": 3, "accounts_url": 5, "account_colors": "red",
                                  "label": False})
        self.assertEqual(s["roots"], ["~/.claude/projects"])
        self.assertEqual(s["workspaces"], ["agent-skeleton"])
        self.assertEqual(s["code_dir"], os.path.join(H, "code"))
        self.assertEqual(s["projects_dir"], os.path.join(H, "code", "projects"))
        self.assertEqual(s["accounts_url"], "")
        self.assertEqual(s["account_colors"], {})
        self.assertEqual(s["label"], "local.latidos")
        for key in ("roots", "workspaces", "code_dir", "projects_dir", "accounts_url",
                    "account_colors", "label"):
            self.assertIn(repr(key), err)

    def test_non_string_list_items_and_colours_are_dropped(self):
        s, err = self.quiet_load({"roots": ["~/a", 3, None], "account_colors": {"w": "#fff", "x": 1}})
        self.assertEqual(s["roots"], ["~/a"])
        self.assertEqual(s["account_colors"], {"w": "#fff"})
        self.assertIn("'roots'", err)
        self.assertIn("'account_colors'", err)
        self.assertIn("'x'", err)

    def test_empty_strings_are_unset(self):
        s, err = self.quiet_load({"code_dir": "", "label": " ", "projects_dir": "", "accounts_url": ""})
        self.assertEqual(s["code_dir"], os.path.join(H, "code"))
        self.assertEqual(s["projects_dir"], os.path.join(H, "code", "projects"))
        self.assertEqual(s["label"], "local.latidos")
        self.assertEqual(s["accounts_url"], "")
        for key in ("code_dir", "label", "projects_dir"):
            self.assertIn("%r is empty" % key, err)
        self.assertNotIn("accounts_url", err)  # "" is its default: no cards

    def test_deeply_nested_files_fall_back_to_defaults(self):
        deep = "[" * 200000
        s, err = self.quiet_load(deep)
        self.assertEqual(s["label"], "local.latidos")
        self.assertIn("nested too deeply", err)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "deep.json")
        with open(path, "w") as fh:
            fh.write('{"a": ' + deep)
        for env in ("LATIDOS_BOARDS", "LATIDOS_ALIASES"):
            os.environ[env] = path
        try:
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(latidos._load_boards(), {})
                self.assertEqual(latidos._load_aliases(), {})
                latidos._aliases_seen.update(mtime=None, checked=0.0)
                self.assertEqual(latidos.alias("repo-x"), "repo-x")  # the live reload too
            self.assertIn("nested too deeply", err.getvalue())
        finally:
            for env in ("LATIDOS_BOARDS", "LATIDOS_ALIASES"):
                os.environ.pop(env, None)
            latidos._aliases_seen.update(mtime=None, checked=0.0)
            with redirect_stderr(io.StringIO()):
                latidos.alias("x")

    def test_root_home_and_their_parents_are_never_roots(self):
        err = io.StringIO()
        keep = os.path.join(H, ".claude", "projects")
        with redirect_stderr(err):
            got = latidos.safe_roots(["/", H, H + "/", os.path.dirname(H), "~", keep])
        self.assertEqual(got, [keep])
        self.assertIn("skipped", err.getvalue())

    def test_config_drops_unsafe_roots_from_the_environment(self):
        os.environ["LATIDOS_ROOTS"] = "/:" + os.path.join(H, ".logs")
        try:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(latidos.config()["roots"], [os.path.join(H, ".logs")])
        finally:
            os.environ.pop("LATIDOS_ROOTS", None)

    def test_boards_and_aliases_that_are_not_objects_are_ignored(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for body in ("[]", '"x"', "3", "null", "{bad", '{"DEMO": ["x"], "OPS": "Ops"}'):
            path = os.path.join(tmp, "f.json")
            with open(path, "w") as fh:
                fh.write(body)
            for env in ("LATIDOS_BOARDS", "LATIDOS_ALIASES"):
                os.environ[env] = path
            try:
                with redirect_stderr(io.StringIO()) as err:
                    boards, aliases = latidos._load_boards(), latidos._load_aliases()
            finally:
                for env in ("LATIDOS_BOARDS", "LATIDOS_ALIASES"):
                    os.environ.pop(env, None)
            want = {"OPS": "Ops"} if body.startswith('{"') else {}
            self.assertEqual(boards, want, body)
            self.assertEqual(aliases, want, body)
            self.assertIn("ignoring", err.getvalue(), body)

    def test_a_missing_boards_file_is_silent(self):
        os.environ["LATIDOS_BOARDS"] = os.path.join(_CONF, "nope.json")
        try:
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(latidos._load_boards(), {})
        finally:
            os.environ.pop("LATIDOS_BOARDS", None)
        self.assertEqual(err.getvalue(), "")


def _case_insensitive(path):
    """True when the volume of `path` ignores case (APFS default on macOS)."""
    flipped = os.path.join(os.path.dirname(path), os.path.basename(path).swapcase())
    return flipped != path and os.path.exists(flipped) and os.path.samefile(flipped, path)


class RootGuard(unittest.TestCase):
    """Roots are refused by folder identity, not by name (audit of 59)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="latidos-roots-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.up = os.path.join(self.tmp, "Users")
        self.home = os.path.join(self.up, "demo")
        self.keep = os.path.join(self.home, ".claude", "projects")
        os.makedirs(self.keep)
        os.makedirs(os.path.join(self.tmp, "archive", "a"))

    def safe(self, paths):
        with redirect_stderr(io.StringIO()) as err:
            got = latidos.safe_roots(paths, home=self.home)
        return got, err.getvalue()

    def test_real_roots_pass(self):
        nope = os.path.join(self.home, ".not-yet")
        got, err = self.safe([self.keep, self.home + "/.claude/projects/", nope])
        self.assertEqual(got, [self.keep, self.home + "/.claude/projects/", nope])
        self.assertEqual(err, "")

    def test_home_and_its_parents_by_identity(self):
        got, err = self.safe([self.home, self.up, self.tmp, self.home + "/.claude/..", "/"])
        self.assertEqual(got, [])
        self.assertEqual(err.count("skipped"), 5)

    def test_case_variants(self):
        if not _case_insensitive(self.tmp):
            self.skipTest("the temp volume is case-sensitive: no case variant names the same folder")
        variants = [os.path.join(self.tmp, "users", "DEMO"), os.path.join(self.tmp, "USERS"),
                    os.path.join(self.tmp, "uSeRs", "Demo") + "/"]
        got, err = self.safe(variants)
        self.assertEqual(got, [])
        self.assertEqual(err.count("skipped"), 3)

    def test_case_variants_of_the_real_home(self):
        if not _case_insensitive(H):
            self.skipTest("the home volume is case-sensitive")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(latidos.safe_roots([H.swapcase(), H.lower(), H.upper(),
                                                os.path.dirname(H).lower()]), [])

    def test_links_to_home_or_above(self):
        for name, target in (("to-home", self.home), ("to-up", self.up), ("to-root", "/")):
            os.symlink(target, os.path.join(self.tmp, "archive", name))
        os.symlink(self.keep, os.path.join(self.tmp, "archive", "to-keep"))
        os.symlink(os.path.join(self.tmp, "archive", "to-home"), os.path.join(self.tmp, "chain"))
        links = [os.path.join(self.tmp, "archive", n) for n in ("to-home", "to-up", "to-root", "to-keep")]
        got, _err = self.safe(links + [os.path.join(self.tmp, "chain")])
        self.assertEqual(got, [os.path.join(self.tmp, "archive", "to-keep")])

    def test_a_glob_is_checked_after_expansion(self):
        os.symlink(self.home, os.path.join(self.tmp, "archive", "b-home"))
        got, err = self.safe(latidos.expand_roots([self.tmp + "/archive/*", self.up + "/*"]))
        self.assertEqual(got, [os.path.join(self.tmp, "archive", "a")])
        self.assertEqual(err.count("skipped"), 2)
        got, _err = self.safe(latidos.expand_roots([self.tmp + "/*"]))
        self.assertEqual(got, [os.path.join(self.tmp, "archive")])

    def test_slash_star_walks_nothing(self):
        expanded = latidos.expand_roots(["/*"])
        self.assertTrue(expanded)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(latidos.safe_roots(expanded), [])
            self.assertEqual(latidos.safe_roots(expanded, home=self.home), [])

    def test_top_level_folders_are_refused_even_when_missing(self):
        got, err = self.safe(["/latidos-no-such-folder", "/Applications", "/usr", "/tmp"])
        self.assertEqual(got, [])
        self.assertIn("top of the filesystem", err)

    def test_another_path_to_the_home_volume(self):
        data = "/System/Volumes/Data"
        if not os.path.exists(os.path.join(data, os.path.relpath(H, "/"))):
            self.skipTest("no firmlinked data volume here")
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(latidos.safe_roots([data, "/System/Volumes", data + "/Users"]), [])
        self.assertEqual(err.getvalue().count("skipped"), 3)

    def test_ingest_skips_a_glob_over_the_temp_home(self):
        logs = os.path.join(self.keep, "-p")
        os.makedirs(logs)
        with open(os.path.join(logs, "s.jsonl"), "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LATIDOS_", "LATIDO_"))}
        conf = os.path.join(self.tmp, "conf")
        os.makedirs(conf)
        with open(os.path.join(conf, "config.json"), "w") as fh:
            json.dump({"roots": [self.up + "/*", "~/.claude/projects"]}, fh)
        env.update(HOME=self.home, LATIDOS_CONFIG_DIR=conf, LATIDOS_CURSOR_API="0",
                   LATIDOS_DB=os.path.join(self.tmp, "l.db"), LATIDOS_FX_URL="http://127.0.0.1:9/never")
        res = subprocess.run([sys.executable, os.path.join(HERE, "latidos.py"), "ingest"], env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("ingested 1 records", res.stdout)  # once, from the real root only
        self.assertIn("skipped", res.stderr)


class WalkGuard(unittest.TestCase):
    """Roots are re-checked on every scan and the walk never follows a link out of its
    root (third audit of 60)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="latidos-walk-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "Users", "demo")
        self.keep = os.path.join(self.home, ".claude", "projects")
        for d in (os.path.join(self.keep, "-p"), os.path.join(self.home, "code", "-x"),
                  os.path.join(self.home, "Documents")):
            os.makedirs(d)
        self.inside = self.jsonl(os.path.join(self.keep, "-p", "s.jsonl"))
        self.outside = self.jsonl(os.path.join(self.home, "code", "-x", "o.jsonl"))
        patcher = unittest.mock.patch.object(latidos, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = unittest.mock.patch.dict(os.environ, {"HOME": self.home})  # "~" is the temp home
        env.start()
        self.addCleanup(env.stop)
        self.warned = set()
        warned = unittest.mock.patch.object(latidos, "_ROOT_WARNED", self.warned)
        warned.start()
        self.addCleanup(warned.stop)

    @staticmethod
    def jsonl(path):
        with open(path, "w") as fh:
            fh.write("{}\n")
        return path

    def files(self, patterns):
        ing = latidos.Ingestor({"roots": [], "root_patterns": patterns}, None, None)
        with redirect_stderr(io.StringIO()) as err:
            got = ing.list_files()
        return sorted(os.path.realpath(f) for f in got), err.getvalue()

    def test_a_root_created_later_as_a_link_to_home_is_skipped(self):
        late = os.path.join(self.home, ".late")
        self.assertEqual(self.files([late]), ([], ""))  # missing: nothing, no warning
        os.symlink(self.home, late)
        got, err = self.files([late])
        self.assertEqual(got, [])
        self.assertIn("skipped", err)

    def test_a_root_link_retargeted_to_home_is_skipped(self):
        root = os.path.join(self.home, ".r")
        os.symlink(self.keep, root)
        self.assertEqual(self.files([root])[0], [self.inside])
        os.unlink(root)
        os.symlink(self.home, root)
        got, err = self.files([root])
        self.assertEqual(got, [])
        self.assertIn("skipped", err)

    def test_links_out_of_the_root_are_not_followed(self):
        os.symlink(os.path.join(self.home, "code"), os.path.join(self.keep, "h"))
        os.symlink(self.home, os.path.join(self.keep, "-p", "home"))
        os.symlink(self.outside, os.path.join(self.keep, "-p", "out.jsonl"))
        os.symlink(self.keep, os.path.join(self.keep, "-p", "loop"))  # inside, a loop
        os.makedirs(os.path.join(self.keep, "-q"))
        os.symlink(os.path.join(self.keep, "-q"), os.path.join(self.keep, "-p", "to-q"))
        q = self.jsonl(os.path.join(self.keep, "-q", "q.jsonl"))
        got, err = self.files([self.keep])
        self.assertEqual(got, sorted([self.inside, q]))
        self.assertIn("leads outside the root", err)
        self.assertEqual(err.count("\n"), 3, err)  # h, home and out.jsonl
        _got, again = self.files([self.keep])
        self.assertEqual(again, "")  # each warning once per process

    def test_a_root_that_is_a_link_keeps_working(self):
        shared = os.path.join(self.home, ".claude-shared", "projects", "-p")
        os.makedirs(shared)
        a = self.jsonl(os.path.join(shared, "a.jsonl"))
        os.symlink(os.path.join(self.home, ".claude-shared", "projects"),
                   os.path.join(self.home, ".projects-link"))
        backups = os.path.join(self.home, ".claude-backups")
        os.makedirs(os.path.join(backups, "one", "-p"))
        b = self.jsonl(os.path.join(backups, "one", "-p", "b.jsonl"))
        got, err = self.files(["~/.projects-link", "~/.claude-backups/*"])
        self.assertEqual(got, sorted([a, b]))
        self.assertEqual(err, "")

    def test_a_new_glob_match_is_picked_up(self):
        backups = os.path.join(self.home, ".claude-backups")
        os.makedirs(backups)
        self.assertEqual(self.files(["~/.claude-backups/*"])[0], [])
        os.makedirs(os.path.join(backups, "two"))
        c = self.jsonl(os.path.join(backups, "two", "c.jsonl"))
        self.assertEqual(self.files(["~/.claude-backups/*"])[0], [c])

    def test_the_walk_prunes_forbidden_folders(self):
        sub = os.path.join(self.keep, "-p")
        got = [d for d, _f in latidos.walk_root(self.keep, {latidos._ident(sub)})]
        self.assertEqual(got, [self.keep])


class RootRules(unittest.TestCase):
    """Folders one level under a refused folder (third audit of 60)."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="latidos-rules-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "Users", "demo")
        for d in (".claude", ".cursor", "Documents", "logs", "Library/CloudStorage/Drive-X",
                  "Library/Application Support/x", "Library/Mobile Documents/y"):
            os.makedirs(os.path.join(self.home, d))

    def safe(self, paths, allow=()):
        with redirect_stderr(io.StringIO()) as err:
            got = latidos.safe_roots(paths, home=self.home, allow=list(allow))
        return got, err.getvalue()

    def test_home_inside_a_system_tree_keeps_its_roots(self):
        # Fedora Atomic keeps homes in /var/home, FreeBSD in /usr/home (fourth audit of 61):
        # with every path counted as inside a system folder, a root inside the home folder
        # must still pass, while a sibling of the home folder stays refused.
        refuse_all = lambda real: "is inside the system folder /var"
        with unittest.mock.patch.object(latidos, "_system_refusal", refuse_all):
            os.makedirs(os.path.join(self.home, ".claude", "projects"))
            os.makedirs(os.path.join(self.tmp, "srv", "data"))
            root = os.path.join(self.home, ".claude", "projects")
            got, err = self.safe([root, os.path.join(self.tmp, "srv", "data")])
        self.assertEqual(got, [root])
        self.assertIn("system folder", err)

    def test_home_children_need_a_dot_or_the_opt_in(self):
        children = latidos.expand_roots([self.home + "/*"])
        self.assertIn(os.path.join(self.home, "Documents"), children)
        got, _err = self.safe(children + [os.path.join(self.home, ".claude"),
                                          os.path.join(self.home, ".cursor")])
        self.assertEqual(got, [os.path.join(self.home, ".claude"), os.path.join(self.home, ".cursor")])
        got, _err = self.safe([os.path.join(self.home, "logs"), os.path.join(self.home, "Documents")],
                              allow=["logs"])
        self.assertEqual(got, [os.path.join(self.home, "logs")])
        # deeper folders are fine; the home's Library and cloud folders are not
        self.assertEqual(self.safe([os.path.join(self.home, "logs", "claude")])[0],
                         [os.path.join(self.home, "logs", "claude")])
        lib = [os.path.join(self.home, "Library", p) for p in
               ("CloudStorage", "CloudStorage/Drive-X", "Application Support", "Mobile Documents/y")]
        self.assertEqual(self.safe(lib)[0], [])
        self.assertEqual(self.safe([os.path.join(self.home, "Library", "Application Support", "x")])[0],
                         [os.path.join(self.home, "Library", "Application Support", "x")])

    def test_a_link_from_elsewhere_to_a_home_child_is_refused(self):
        link = os.path.join(self.tmp, "docs")
        os.symlink(os.path.join(self.home, "Documents"), link)
        self.assertEqual(self.safe([link])[0], [])

    def test_system_trees_and_aliases(self):
        refused = ["/Applications/Some.app", "/Library/Application Support", "/usr/lib",
                   "/opt/homebrew", "/private/var", "/var/db", "/private/var/db", "/etc/ssh",
                   "/private/etc/ssh", "/var/folders", "/private/var/folders/zz",
                   "/private/var/folders/zz/yy", "/USR/LIB", "/private/tmp", "/Users/someone",
                   "/home/someone", "/Volumes/latidos-no-such-disk", "/Volumes/Macintosh HD",
                   "/mnt/disk", "/media/disk", "/System/Library/x"]
        got, err = self.safe(refused)
        self.assertEqual(got, [])
        self.assertEqual(err.count("skipped"), len(refused))
        allowed = [os.path.join(self.tmp, ".claude"),  # the temp folder: /private/var/folders/a/b/T/...
                   "/Volumes/latidos-no-such-disk/claude-logs", "/data/claude", "/tmp/latidos-x"]
        self.assertEqual(self.safe(allowed)[0], allowed)

    def test_var_and_private_var_agree(self):
        if not os.path.islink("/var"):
            self.skipTest("/var is not an alias here")
        for sub in ("db", "log", "folders"):
            self.assertEqual(self.safe(["/var/" + sub])[0], self.safe(["/private/var/" + sub])[0])

    def test_shallow_globs_are_refused_whole(self):
        with redirect_stderr(io.StringIO()) as err:
            self.assertEqual(latidos.checked_roots(["/*/*", "/*", "/tmp/*", "/data/*"]), [])
        self.assertEqual(err.getvalue().count("wildcard"), 4)
        with redirect_stderr(io.StringIO()):
            got = latidos.checked_roots([self.home + "/.c*"])
        self.assertEqual(got, [os.path.join(self.home, ".claude"), os.path.join(self.home, ".cursor")])


class Settings2(unittest.TestCase):
    """Empty roots and host details (third audit of 60)."""

    def load(self, data):
        conf = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, conf)
        path = os.path.join(conf, "config.json")
        with open(path, "w") as fh:
            json.dump(data, fh)
        os.environ["LATIDOS_CONFIG"] = path
        try:
            with redirect_stderr(io.StringIO()) as err:
                return latidos.load_settings(), err.getvalue()
        finally:
            os.environ.pop("LATIDOS_CONFIG", None)

    def test_empty_roots_use_the_default_with_a_warning(self):
        for roots in ("", ["", ""], [], [" "]):
            s, err = self.load({"roots": roots})
            self.assertEqual(s["roots"], ["~/.claude/projects"], roots)
            self.assertIn("'roots' has no folder", err, roots)
        s, err = self.load({})
        self.assertEqual((s["roots"], err), (["~/.claude/projects"], ""))

    def test_empty_latido_roots_uses_the_config(self):
        os.environ["LATIDOS_ROOTS"] = ":"
        try:
            with redirect_stderr(io.StringIO()) as err, \
                    unittest.mock.patch.object(latidos, "_ROOT_WARNED", set()):
                self.assertEqual(latidos.root_patterns(), latidos.SETTINGS["roots"])
        finally:
            os.environ.pop("LATIDOS_ROOTS", None)
        self.assertIn("LATIDOS_ROOTS has no folder", err.getvalue())

    def test_host_names_ignore_case(self):
        os.environ["LATIDOS_HOST"] = "LOCALHOST"
        try:
            self.assertEqual(latidos.bind_host(), "localhost")
        finally:
            os.environ.pop("LATIDOS_HOST", None)
        self.assertTrue(latidos.host_allowed("localhost:7482", "127.0.0.1", {"host": "localhost", "port": 7482}))

    def test_an_unbindable_host_falls_back_to_loopback(self):
        for host in ("192.0.2.1", "latidos-no-such-host.invalid"):
            with redirect_stderr(io.StringIO()) as err:
                srv, bound = latidos.serve_on(host, 0, latidos.BaseHTTPRequestHandler)
            srv.server_close()
            self.assertEqual(bound, "127.0.0.1", host)
            self.assertIn("cannot bind", err.getvalue())
            self.assertEqual(latidos.probe_host(host), "127.0.0.1")
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        self.addCleanup(busy.close)
        with self.assertRaises(OSError):  # a port in use stays an error
            latidos.serve_on("127.0.0.1", busy.getsockname()[1], latidos.BaseHTTPRequestHandler)

    def test_probe_host(self):
        self.assertEqual(latidos.probe_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(latidos.probe_host("::"), "::1")
        self.assertEqual(latidos.probe_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(latidos.probe_host("localhost"), "localhost")


class Startup(unittest.TestCase):
    """serve and ingest start with every malformed user file (a crash at startup is a
    restart loop under launchd)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.conf = os.path.join(self.tmp, "conf")
        logs = os.path.join(self.home, "logs", "-proj")
        os.makedirs(logs)
        os.makedirs(self.conf)
        with open(os.path.join(logs, "s.jsonl"), "w") as fh:
            fh.write(assistant("m1", "2026-11-12T10:00:00Z", U) + "\n")

    def env(self, **files):
        for name, body in files.items():
            with open(os.path.join(self.conf, name + ".json"), "w") as fh:
                fh.write(body)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LATIDOS_", "LATIDO_"))}
        env.update(HOME=self.home, LATIDOS_CONFIG_DIR=self.conf, LATIDOS_CURSOR_API="0",
                   LATIDOS_DB=os.path.join(self.tmp, "l.db"), LATIDOS_FX_URL="http://127.0.0.1:9/never")
        return env

    def ingest(self, **files):
        res = subprocess.run([sys.executable, os.path.join(HERE, "latidos.py"), "ingest"],
                             env=self.env(**files), capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def test_ingest_with_malformed_files(self):
        res = self.ingest(config='{"roots": "~/logs", "workspaces": "x", "allow_home_folders": "logs"}',
                          boards="[]", aliases='"x"')
        self.assertIn("ingested 1 records", res.stdout)
        self.assertNotIn("skipped", res.stderr)  # one root "~/logs", not "~", "/", "l", ...
        self.assertIn("boards.json: not an object", res.stderr)
        self.assertIn("aliases.json: not an object", res.stderr)

    def test_ingest_never_walks_home_or_root(self):
        for roots in ('"~"', '["/"]', '["~/"]'):
            res = self.ingest(config='{"roots": %s}' % roots)
            self.assertIn("ingested 0 records", res.stdout, roots)
            self.assertIn("skipped", res.stderr, roots)

    def test_serve_with_malformed_files(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = self.env(config='{"roots": 5, "accounts_url": 1, "label": []}', boards="[1]",
                       aliases="null")
        env.update(LATIDOS_PORT=str(port), LATIDOS_HOST="0.0.0.0")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "latidos.py"), "serve"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            status = None
            for _ in range(100):
                try:
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    c.request("GET", "/api/health", headers={"Host": "127.0.0.1:%d" % port})
                    status = c.getresponse().status
                    c.close()
                    break
                except OSError:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            self.assertEqual(status, 200)
        finally:
            proc.terminate()  # the PID this test started
            _out, err = proc.communicate(timeout=10)
        self.assertIn("binding 127.0.0.1", err.decode())

    def test_serve_falls_back_from_an_unbindable_host(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = self.env()
        env.update(LATIDOS_PORT=str(port), LATIDOS_HOST="192.0.2.1", LATIDOS_ALLOW_REMOTE="1")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "latidos.py"), "serve"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            status = None
            for _ in range(100):
                res = subprocess.run([sys.executable, os.path.join(HERE, "latidos.py"), "status"],
                                     env=env, capture_output=True, text=True, timeout=30)
                if res.returncode == 0 or proc.poll() is not None:
                    status = res.returncode
                    break
                time.sleep(0.1)
            self.assertEqual(status, 0, res.stdout)
            self.assertIn('"ok"', res.stdout)
        finally:
            proc.terminate()  # the PID this test started
            _out, err = proc.communicate(timeout=10)
        self.assertIn("cannot bind LATIDOS_HOST=192.0.2.1", err.decode())
        self.assertIn("http://127.0.0.1:%d" % port, err.decode())

    def test_serve_on_ipv6_loopback(self):
        try:
            probe = socket.socket(socket.AF_INET6)
            probe.bind(("::1", 0))
        except OSError:
            self.skipTest("no IPv6 loopback here")
        port = probe.getsockname()[1]
        probe.close()
        env = self.env()
        env.update(LATIDOS_PORT=str(port), LATIDOS_HOST="[::1]")
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "latidos.py"), "serve"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            status = None
            for _ in range(100):
                try:
                    c = http.client.HTTPConnection("::1", port, timeout=2)
                    c.request("GET", "/api/health", headers={"Host": "[::1]:%d" % port})
                    status = c.getresponse().status
                    c.close()
                    break
                except OSError:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            self.assertEqual(status, 200)
        finally:
            proc.terminate()  # the PID this test started
            _out, err = proc.communicate(timeout=10)
        self.assertIn("http://[::1]:%d" % port, err.decode())


class HostGuard(unittest.TestCase):
    def cfg(self, host):
        return {"host": host, "port": 7482}

    def test_loopback_bind(self):
        self.assertTrue(latidos.host_allowed("127.0.0.1:7482", "127.0.0.1", self.cfg("127.0.0.1")))
        self.assertTrue(latidos.host_allowed("localhost:7482", "127.0.0.1", self.cfg("127.0.0.1")))
        self.assertFalse(latidos.host_allowed("evil.example:7482", "127.0.0.1", self.cfg("127.0.0.1")))
        self.assertFalse(latidos.host_allowed("127.0.0.1:9", "127.0.0.1", self.cfg("127.0.0.1")))
        self.assertFalse(latidos.host_allowed(None, "127.0.0.1", self.cfg("127.0.0.1")))

    def test_a_loopback_bind_answers_its_own_url(self):
        cfg = self.cfg("127.0.0.2")
        self.assertTrue(latidos.host_allowed("127.0.0.2:7482", "127.0.0.1", cfg))
        self.assertTrue(latidos.host_allowed("127.0.0.1:7482", "127.0.0.1", cfg))
        self.assertFalse(latidos.host_allowed("127.0.0.3:7482", "127.0.0.1", cfg))
        self.assertFalse(latidos.host_allowed("127.0.0.2:7482", "192.0.2.50", cfg))
        cfg6 = self.cfg("::1")
        self.assertTrue(latidos.host_allowed("[::1]:7482", "::1", cfg6))
        self.assertFalse(latidos.host_allowed("::1:7482", "::1", cfg6))
        self.assertTrue(latidos._is_loopback("::ffff:127.0.0.1"))
        self.assertFalse(latidos._is_loopback("::ffff:192.0.2.1"))

    def test_server_family_follows_the_host(self):
        self.assertEqual(latidos._host_name("::1"), "[::1]")
        self.assertEqual(latidos._host_name("127.0.0.1"), "127.0.0.1")
        srv = latidos.make_server("127.0.0.1", 0, latidos.BaseHTTPRequestHandler)
        self.assertEqual(srv.address_family, socket.AF_INET)
        srv.server_close()
        try:
            srv = latidos.make_server("::1", 0, latidos.BaseHTTPRequestHandler)
        except OSError:
            self.skipTest("no IPv6 loopback here")
        self.assertEqual(srv.address_family, socket.AF_INET6)
        srv.server_close()

    def test_remote_bind_does_not_trust_a_forged_loopback_host(self):
        cfg = self.cfg("192.0.2.10")
        self.assertFalse(latidos.host_allowed("127.0.0.1:7482", "192.0.2.50", cfg))
        self.assertFalse(latidos.host_allowed("localhost:7482", "192.0.2.50", cfg))
        self.assertTrue(latidos.host_allowed("127.0.0.1:7482", "127.0.0.1", cfg))
        self.assertTrue(latidos.host_allowed("192.0.2.10:7482", "192.0.2.50", cfg))
        self.assertFalse(latidos.host_allowed("evil.example", "192.0.2.50", cfg))
        os.environ["LATIDOS_ALLOWED_HOSTS"] = "panel.example, other.example:7482"
        try:
            self.assertTrue(latidos.host_allowed("panel.example:7482", "192.0.2.50", cfg))
            self.assertTrue(latidos.host_allowed("other.example:7482", "192.0.2.50", cfg))
            self.assertFalse(latidos.host_allowed("other.example:9", "192.0.2.50", cfg))
            # an any-address bind trusts only the listed names
            self.assertFalse(latidos.host_allowed("0.0.0.0:7482", "192.0.2.50", self.cfg("0.0.0.0")))
            self.assertTrue(latidos.host_allowed("panel.example", "192.0.2.50", self.cfg("0.0.0.0")))
        finally:
            os.environ.pop("LATIDOS_ALLOWED_HOSTS", None)

    def test_non_loopback_bind_needs_the_opt_in(self):
        saved = {k: os.environ.get(k) for k in ("LATIDOS_HOST", "LATIDOS_ALLOW_REMOTE")}
        try:
            os.environ["LATIDOS_HOST"] = "0.0.0.0"
            with redirect_stderr(io.StringIO()) as err:
                self.assertEqual(latidos.bind_host(), "127.0.0.1")
            self.assertIn("LATIDOS_ALLOW_REMOTE", err.getvalue())
            os.environ["LATIDOS_ALLOW_REMOTE"] = "1"
            self.assertEqual(latidos.bind_host(), "0.0.0.0")
            os.environ["LATIDOS_HOST"] = "localhost"
            os.environ.pop("LATIDOS_ALLOW_REMOTE")
            self.assertEqual(latidos.bind_host(), "localhost")
            os.environ["LATIDOS_HOST"] = "[::1]"
            self.assertEqual(latidos.bind_host(), "::1")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class Edits(unittest.TestCase):
    """Rename a cell and move a task from the page (aliases.json, assign.json)."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects", "p")
        os.makedirs(self.root)
        os.environ["LATIDOS_ROOTS"] = os.path.join(self.tmp, "projects")
        os.environ["LATIDOS_DB"] = os.path.join(self.tmp, "db", "l.db")
        os.environ["LATIDOS_FX_URL"] = "http://127.0.0.1:9/never"
        os.environ["LATIDOS_ALIASES"] = os.path.join(self.tmp, "cfg", "aliases.json")
        os.environ["LATIDOS_ASSIGN"] = os.path.join(self.tmp, "cfg", "assign.json")
        self.saved = (dict(latidos.ALIASES), dict(latidos.ASSIGN))
        self.reset()
        self.app = latidos.App(latidos.config())
        self.f = os.path.join(self.root, "s.jsonl")

    def reset(self):
        latidos.ALIASES.clear(); latidos.ASSIGN = {}
        latidos._aliases_seen.update(mtime=None, checked=0.0)
        latidos._assign_seen.update(mtime=None, checked=0.0)
        latidos._project_cache.clear()

    def tearDown(self):
        for k in ("LATIDOS_ROOTS", "LATIDOS_DB", "LATIDOS_FX_URL", "LATIDOS_ALIASES", "LATIDOS_ASSIGN"):
            os.environ.pop(k, None)
        self.app.con.close()
        self.reset()
        latidos.ALIASES.update(self.saved[0]); latidos.ASSIGN = dict(self.saved[1])
        shutil.rmtree(self.tmp)

    def write(self, *lines):
        with open(self.f, "w") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        self.app.ingestor.scan()

    def names(self):
        d = self.app.summary(0, int(time.time() * 1000) + 60000, None)
        return {p["name"]: p for p in d["projects"]}

    def test_move_a_task_then_rename_its_cell(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cwd = H + "/code/agent-skeleton"
        self.write(assistant("m1", now, U, session="s1", cwd=cwd), assistant("m2", now, U, session="s2", cwd=cwd))
        self.assertEqual(list(self.names()), ["agent-skeleton"])
        self.assertEqual(latidos.assign_task("s:s2", "  Refactor   tools "), "Refactor tools")
        n = self.names()
        self.assertEqual(sorted(n), ["Refactor tools", "agent-skeleton"])
        self.assertEqual([t["assigned"] for t in n["Refactor tools"]["tasks"]], [True])
        self.assertEqual([t["assigned"] for t in n["agent-skeleton"]["tasks"]], [False])
        self.assertEqual(self.app.ingestor.live_project("s2", cwd), "Refactor tools")
        self.assertEqual(self.app.ingestor.live_project("s1", cwd), "agent-skeleton")
        n = self.names()
        self.assertEqual(n["Refactor tools"]["tasks"][0]["assign_key"], "s:s2")
        # Renaming the new cell keeps the task in it, under the new name.
        latidos.rename_project("Refactor tools", "Tools refactor")
        self.assertEqual(sorted(self.names()), ["Tools refactor", "agent-skeleton"])
        # Renamed twice, then back to its own name: no entry maps it to itself.
        latidos.rename_project("agent-skeleton", "Skeleton")
        latidos.rename_project("Skeleton", "Kit")
        self.assertIn("Kit", self.names())
        latidos.rename_project("Kit", "agent-skeleton")
        self.assertEqual(sorted(self.names()), ["Tools refactor", "agent-skeleton"])
        with open(os.environ["LATIDOS_ALIASES"]) as fh:
            self.assertNotIn("agent-skeleton", json.load(fh))
        # Back to automatic.
        latidos.assign_task("s:s2", None)
        self.assertEqual(list(self.names()), ["agent-skeleton"])
        with open(os.environ["LATIDOS_ASSIGN"]) as fh:
            self.assertEqual(json.load(fh), {})

    def test_renaming_a_merged_cell_keeps_it_whole(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/repo-a"),
                   assistant("m2", now, U, session="s2", cwd=H + "/code/repo-b"))
        latidos.rename_project("repo-a", "repo-b")      # merge a into b
        self.assertEqual(list(self.names()), ["repo-b"])
        latidos.rename_project("repo-b", "Both")
        n = self.names()
        self.assertEqual(list(n), ["Both"])
        self.assertEqual(n["Both"]["sessions"], 2)

    def test_a_move_lands_in_the_cell_it_names(self):
        # "Bar" was renamed away to "Baz"; later another cell is shown as "Bar".
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/foo"),
                   assistant("m2", now, U, session="s2", cwd=H + "/code/qux"),
                   assistant("m3", now, U, session="s3", cwd=H + "/code/zed"))
        latidos.rename_project("foo", "Bar")
        latidos.rename_project("Bar", "Baz")
        latidos.rename_project("qux", "Bar")
        self.assertEqual(sorted(self.names()), ["Bar", "Baz", "zed"])
        self.assertEqual(latidos.assign_task("s:s3", "Bar"), "Bar")
        n = self.names()
        self.assertEqual(sorted(n), ["Bar", "Baz"])
        self.assertEqual(n["Bar"]["sessions"], 2)

    def test_a_hand_edited_file_with_a_typo_is_never_overwritten(self):
        cfg = os.path.dirname(os.environ["LATIDOS_ALIASES"])
        os.makedirs(cfg, exist_ok=True)
        broken = '{"repo-a": "A", "repo-b": "B",}'
        for name in ("aliases.json", "assign.json"):
            with open(os.path.join(cfg, name), "w") as fh:
                fh.write(broken)
        with self.assertRaises(latidos.EditConflict):
            latidos.rename_project("repo-c", "Gamma")
        with self.assertRaises(latidos.EditConflict):
            latidos.assign_task("s:x", "Gamma")
        for name in ("aliases.json", "assign.json"):
            with open(os.path.join(cfg, name)) as fh:
                self.assertEqual(fh.read(), broken)
        self.assertEqual([f for f in os.listdir(cfg) if f.endswith(".tmp")], [])

    def test_entries_the_page_does_not_use_are_kept_and_links_stay_links(self):
        cfg = os.path.dirname(os.environ["LATIDOS_ALIASES"])
        real_dir = os.path.join(self.tmp, "dotfiles")
        os.makedirs(cfg); os.makedirs(real_dir)
        real = os.path.join(real_dir, "aliases.json")
        with open(real, "w") as fh:
            json.dump({"repo-a": "A", "note": 7}, fh)
        os.chmod(real, 0o600)
        os.symlink(real, os.environ["LATIDOS_ALIASES"])
        latidos.rename_project("repo-c", "Gamma \U0001f680")
        self.assertTrue(os.path.islink(os.environ["LATIDOS_ALIASES"]))
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
        with open(real, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"repo-a": "A", "note": 7, "repo-c": "Gamma \U0001f680"})

    def test_reserved_and_long_current_names(self):
        with self.assertRaises(ValueError):
            latidos.rename_project("repo", "desconocido")
        long_name = "x" * 90  # a stored repo name longer than a typed name may be
        self.assertEqual(latidos.rename_project(long_name, "Short"), "Short")
        self.assertEqual(latidos.alias(long_name), "Short")

    def test_moves_to_existing_names_and_renamed_away_names(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/agent-skeleton"),
                   assistant("m2", now, U, session="s2", cwd=H + "/code/agent-skeleton"))
        latidos.assign_task("s:s1", "Infra")
        latidos.rename_project("Infra", "Infra team")
        # "Infra" was renamed away and no cell shows it: the move says where it went.
        self.assertEqual(latidos.assign_task("s:s2", "Infra"), "Infra team")
        # A cell on screen is taken as it is, oddly spaced or long.
        odd = "my  repo " + "x" * 70
        self.assertEqual(latidos.assign_task("s:s2", odd, existing=True), odd)
        with self.assertRaises(ValueError):
            latidos.assign_task("s:s2", "Desconocido", existing=True)
        with self.assertRaises(ValueError):
            latidos.rename_project("a\nb", "B")  # control characters, even in a current name

    def test_a_non_string_entry_under_the_old_name_does_not_block_a_rename(self):
        cfg = os.path.dirname(os.environ["LATIDOS_ALIASES"])
        os.makedirs(cfg)
        with open(os.environ["LATIDOS_ALIASES"], "w") as fh:
            json.dump({"repo": 7}, fh)
        latidos.rename_project("repo", "R")
        self.assertEqual(latidos.alias("repo"), "R")

    def test_a_failed_write_leaves_no_temporary_file_and_the_old_one_intact(self):
        cfg = os.path.dirname(os.environ["LATIDOS_ALIASES"])
        os.makedirs(cfg)
        with open(os.environ["LATIDOS_ALIASES"], "w") as fh:
            json.dump({"repo-a": "A"}, fh)
        real = os.replace

        def boom(*a, **k):
            raise OSError(28, "No space left on device")
        latidos.os.replace = boom
        try:
            with self.assertRaises(OSError):
                latidos.rename_project("repo-b", "B")
        finally:
            latidos.os.replace = real
        self.assertEqual(os.listdir(cfg), ["aliases.json"])
        with open(os.environ["LATIDOS_ALIASES"]) as fh:
            self.assertEqual(json.load(fh), {"repo-a": "A"})

    def test_a_new_file_follows_the_umask(self):
        old = os.umask(0o077)
        try:
            latidos.assign_task("s:x", "Somewhere")
        finally:
            os.umask(old)
        self.assertEqual(os.stat(os.environ["LATIDOS_ASSIGN"]).st_mode & 0o777, 0o600)

    def test_a_summary_computed_across_an_edit_is_not_cached(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/agent-skeleton"))
        real = latidos.summarize

        def slow(*a, **k):
            out = real(*a, **k)
            latidos.assign_task("s:s1", "Elsewhere")  # lands while this one is computed
            return out
        latidos.summarize = slow
        try:
            self.app.summary(0, int(time.time() * 1000) + 60000, None)
        finally:
            latidos.summarize = real
        self.assertFalse(self.app.cache)
        self.assertEqual(list(self.names()), ["Elsewhere"])

    def test_tasks_and_cells_say_which_account_their_tokens_came_from(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cwd = H + "/code/agent-skeleton"
        self.write(assistant("m1", now, U, session="s1", cwd=cwd),
                   assistant("m2", now, U, session="s1", cwd=cwd, model="us.anthropic.claude-opus-5-5-v1:0"),
                   assistant("m3", now, U, session="s2", cwd=cwd))
        real = latidos.account_map
        latidos.account_map = lambda logins=None: {}
        try:
            n = self.names()
        finally:
            latidos.account_map = real
        p = n["agent-skeleton"]
        by = {t["key"]: t["origins"] for t in p["tasks"]}
        self.assertEqual(sorted(k for k, _v in by["s:s1"]), ["bedrock", "claude"])
        self.assertEqual(by["s:s2"], [["claude", 1310]])
        self.assertEqual(dict(p["origins"]), {"claude": 2620, "bedrock": 1310})

    def test_hidden_accounts_leave_everything_but_their_share(self):
        # L-159: ?hide=acct:<tag> drops that account's calls from cells, totals and days;
        # by_origin still counts every call, so a hidden card keeps its share.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/agent-skeleton"),
                   assistant("m2", now, U, session="s2", cwd=H + "/code/other-repo"),
                   assistant("m3", now, U, session="s3", cwd=H + "/code/other-repo"))
        with self.app.lock:
            self.app.con.execute("UPDATE calls SET account = 'uid-w' WHERE session_id = 's1'")
            self.app.con.execute("UPDATE calls SET account = 'uid-h' WHERE session_id = 's2'")
            self.app.con.commit()
        t1 = int(time.time() * 1000) + 60000
        with unittest.mock.patch.object(latidos, "account_map", lambda logins=None: {"uid-w": "work", "uid-h": "home"}):
            full = self.app.summary(0, t1, None)
            part = self.app.summary(0, t1, None, hide=("acct:work",))
            again = self.app.summary(0, t1, None)
        self.assertEqual(sorted(p["name"] for p in full["projects"]), ["agent-skeleton", "other-repo"])
        self.assertEqual([p["name"] for p in part["projects"]], ["other-repo"])
        self.assertEqual(part["totals"]["calls"], 2)
        self.assertEqual(sum(d["calls"] for d in part["days"]), 2)
        self.assertEqual(part["hidden"], ["acct:work"])
        self.assertEqual(full["hidden"], [])
        for d in (full, part):
            self.assertEqual({k: v["calls"] for k, v in d["by_origin"].items()},
                             {"acct:work": 1, "acct:home": 1, "claude": 1})
        # The plans' figures do not move with the filter.
        self.assertEqual(part["accounts"]["span_days"], full["accounts"]["span_days"])
        self.assertEqual(again["totals"]["calls"], 3, "the filtered answer has its own cache entry")

    def test_a_hidden_view_follows_new_data_and_its_live_rate(self):
        # Audit of 86: a view with hidden accounts has no background refresh; a request
        # recomputes it once the data moved and the entry is older than stale_ok, and the
        # 15-minute rate leaves the hidden account out too.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/agent-skeleton"))
        with self.app.lock:
            self.app.con.execute("UPDATE calls SET account = 'uid-w'")
            self.app.con.commit()
        t0 = int(time.time() * 1000) - 3600000
        latidos.alias("x"); latidos._assign_map()  # first reads outside the summary, so it is cached
        with unittest.mock.patch.object(latidos, "account_map", lambda logins=None: {"uid-w": "work"}):
            first = self.app.summary(t0, t0 + 7200000, None, hide=("acct:work",))
            self.assertEqual(first["totals"]["calls"], 0)
            self.assertEqual(first["live"]["calls_15m"], 0)
            self.assertEqual(self.app.summary(t0, t0 + 7200000, None)["live"]["calls_15m"], 1)
            with open(self.f, "a") as fh:
                fh.write(assistant("m2", now, U, session="s2", cwd=H + "/code/agent-skeleton") + "\n")
            self.app.ingestor.scan()
            key = [k for k in self.app.cache if k[-1] == ("acct:work",)][0]
            v, at, data = self.app.cache[key]
            self.app.cache[key] = (v, at - self.app.STALE_OK_MS - 1, data)
            again = self.app.summary(t0, t0 + 7200000, None, hide=("acct:work",))
        self.assertEqual(again["totals"]["calls"], 1, "recomputed: the new unowned call shows")
        self.assertEqual(again["live"]["calls_15m"], 1)
        self.assertEqual(again["live"]["last"], self.app.con.execute(
            "SELECT MAX(ts) FROM calls WHERE account IS NULL").fetchone()[0], "the newest visible call")
        # Audit of 90: past 32 entries the hidden views are evicted first, not the defaults.
        self.app.cache.clear()
        for i in range(32):
            self.app.cache[(i, "now", "day")] = (0, time.time() * 1000, {})
        self.app.cache[(99, "now", "day", ("acct:x",))] = (0, time.time() * 1000, {})
        with unittest.mock.patch.object(latidos, "account_map", lambda logins=None: {}):
            self.app.summary(t0, t0 + 7200000, None)
        self.assertNotIn((99, "now", "day", ("acct:x",)), self.app.cache)
        self.assertIn((0, "now", "day"), self.app.cache)

    def test_a_ticket_moves_with_all_its_sessions(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(json.dumps({"type": "custom-title", "customTitle": "[DEMO-7] thing", "sessionId": "s1"}),
                   assistant("m1", now, U, session="s1", cwd=H + "/code/agent-skeleton"))
        latidos.assign_task("DEMO-7", "Client work")
        n = self.names()
        self.assertEqual(list(n), ["Client work"])
        self.assertEqual(n["Client work"]["tasks"][0]["key"], "DEMO-7")
        self.assertEqual(self.app.ingestor.live_project("s1", H + "/code/agent-skeleton"), "Client work")

    def test_edits_are_validated(self):
        for key in ("", "s:", "s:a b", "../x", "demo-1", "s:" + "x" * 81):
            with self.assertRaises(ValueError, msg=key):
                latidos.assign_task(key, "x")
        self.assertEqual(latidos.clean_name(" a\n\tb "), "a b")  # whitespace folds to one space
        for name in ("", "   ", "x" * 61, "a\x00b", "a\x7fb", 7, None):
            with self.assertRaises(ValueError, msg=repr(name)):
                latidos.rename_project("repo", name)
        self.assertFalse(os.path.exists(os.environ["LATIDOS_ASSIGN"]))
        self.assertFalse(os.path.exists(os.environ["LATIDOS_ALIASES"]))

    def test_an_edit_drops_cached_summaries(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write(assistant("m1", now, U, session="s1", cwd=H + "/code/agent-skeleton"))
        latidos.alias("x"); latidos._assign_map()  # the first read of both files, before the summary
        v0 = self.app.con.execute("SELECT v FROM meta WHERE k = 'data_version'").fetchone()[0]
        self.app.summary(0, int(time.time() * 1000) + 60000, None)
        self.assertTrue(self.app.cache)
        latidos.assign_task("s:s1", "Elsewhere")
        self.assertFalse(self.app.cache)
        v1 = self.app.con.execute("SELECT v FROM meta WHERE k = 'data_version'").fetchone()[0]
        self.assertGreater(int(v1), int(v0))


class Origins(unittest.TestCase):
    def test_call_origin(self):
        ids = {"uid-mt": "mt"}
        o = latidos.call_origin
        self.assertEqual(o("claude-opus-5-5", "uid-mt", ids), "acct:mt")
        self.assertEqual(o("claude-opus-5-5", "uid-c", {"uid-c": "claude"}), "acct:claude")  # a tag named like a key
        for m in ("jp.anthropic.claude-sonnet-5-v1:0", "au.anthropic.claude-opus-5-5-v1:0",
                  "us-gov.anthropic.claude-sonnet-5-v1:0",
                  "arn:aws:bedrock:eu-west-1:123:inference-profile/eu.anthropic.claude-opus-5-5-v1:0"):
            self.assertEqual(o(m, "uid-mt", ids), "bedrock", m)
        self.assertEqual(o("claude-opus-5-5", "uid-unknown", ids), "claude")
        self.assertEqual(o("claude-opus-5-5", None, ids), "claude")
        self.assertEqual(o("<synthetic>", None, ids), "claude")
        self.assertEqual(o("us.anthropic.claude-opus-5-5-v1:0", "uid-mt", ids), "bedrock")
        self.assertEqual(o("anthropic.claude-sonnet-5", None, ids), "bedrock")
        self.assertEqual(o("claude-opus-5-5@20261101", "uid-mt", ids), "vertex")
        self.assertEqual(o("gpt-6", "uid-mt", ids), "other")
        self.assertEqual(o(None, "uid-mt", ids), "acct:mt")


class HideParam(unittest.TestCase):
    def test_parse_hide(self):
        p = latidos.parse_hide
        self.assertEqual(p(None), ())
        self.assertEqual(p(""), ())
        self.assertEqual(p("acct:b,acct:a,acct:b"), ("acct:a", "acct:b"))
        for bad in ("claude", "acct:", "acct:a b", "cursor", "acct:../x", ",".join("acct:t%d" % i for i in range(17))):
            with self.assertRaises(ValueError, msg=bad):
                p(bad)


class LoginsTest(unittest.TestCase):
    """L-161/L-162: the logins tool (synthetic answers, no real command or Keychain)."""

    def make(self, rows=None, exp=None):
        calls, spawned = [], []
        rows = rows if rows is not None else [
            {"tag": "work", "label": "Work", "email": "w@example.com", "login_email": "w@example.com",
             "state": "soon", "expires_at": "2030-01-02T03:04:05+01:00", "days_left": 2.1, "plan": "team",
             "detail": "", "renewing": False, "warmup": None},
            {"tag": "home", "state": "ok", "expires_at": "2030-02-01T00:00:00+01:00", "days_left": 30.0,
             "renewing": True}]

        def runner(args, timeout):
            calls.append(args)
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"accounts": rows}) + "\n")

        lg = latidos.Logins({"account_logins_tool": ["python3", "tool.py"]}, runner=runner,
                            spawner=lambda args: spawned.append(args), cursor_expiry=lambda: exp)
        return lg, calls, spawned

    def test_off_without_a_tool(self):
        lg = latidos.Logins({}, cursor_expiry=lambda: None)
        self.assertEqual(lg.status(), {"enabled": False, "claude": [], "error": None, "cursor": None})
        with self.assertRaises(PermissionError):
            lg.renew("work")

    def test_status_keeps_no_email_and_is_cached(self):
        lg, calls, _ = self.make()
        st = lg.status()
        self.assertEqual(calls, [["python3", "tool.py", "status", "--json"]])
        self.assertEqual(st["claude"][0], {"tag": "work", "state": "soon", "expires_at": "2030-01-02T03:04:05+01:00",
                                           "days_left": 2.1, "renewing": False})
        self.assertNotIn("example.com", json.dumps(st))
        lg.status()
        self.assertEqual(len(calls), 1, "served from the cache")
        lg.at -= lg.BUSY_TTL + 1  # a renewal is running ("home"): re-read after a few seconds
        lg.status()
        self.assertEqual(len(calls), 2)

    def test_renew_spawns_the_tool_once(self):
        lg, _, spawned = self.make()
        self.assertEqual(lg.renew("work"), {"ok": True, "tag": "work"})
        self.assertEqual(spawned, [["python3", "tool.py", "login", "work"]])
        with self.assertRaises(latidos.ControlBusy):
            lg.renew("work")  # launched a moment ago
        with self.assertRaises(latidos.ControlBusy):
            lg.renew("home")  # the tool says it is already renewing
        for bad in ("nope", "../x", "", None, 7):
            with self.assertRaises(ValueError):
                lg.renew(bad)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(lg.status()["claude"][0]["renewing"], "just launched shows as renewing")

    def test_spawn_failure_is_a_control_error(self):
        lg, _, _ = self.make()

        def boom(args):
            raise FileNotFoundError("no such file")
        lg.spawn = boom
        with self.assertRaises(latidos.subprocess.SubprocessError):
            lg.renew("work")
        self.assertNotIn("work", lg.started)

    def test_bad_answer_keeps_the_last_rows(self):
        lg, _, _ = self.make()
        lg.status()
        lg.run = lambda args, timeout: types.SimpleNamespace(returncode=1, stdout="oops")
        lg.at = 0
        st = lg.status()
        self.assertEqual(st["error"], "status: ValueError")
        self.assertEqual([a["tag"] for a in st["claude"]], ["work", "home"])

    def test_cursor_expiry_states(self):
        now = time.time()
        for exp, state in ((now + 30 * 86400, "ok"), (now + 86400, "soon"), (now - 10, "expired")):
            lg, _, _ = self.make(exp=exp)
            self.assertEqual(lg.status()["cursor"]["state"], state)
        self.assertIsNone(self.make(exp=None)[0].status()["cursor"])

    def test_cursor_session_expiry_reads_only_exp(self):
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "state.vscdb")
            self.assertIsNone(cursor_source.session_expiry(db))
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
            payload = base64.urlsafe_b64encode(json.dumps({"sub": "auth0|user_x", "exp": 1900000000}).encode()).decode().rstrip("=")
            con.execute("INSERT INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", ("h." + payload + ".sig",))
            con.commit(); con.close()
            self.assertEqual(cursor_source.session_expiry(db), 1900000000)
            con = sqlite3.connect(db)
            con.execute("UPDATE ItemTable SET value = 'garbage'")
            con.commit(); con.close()
            self.assertIsNone(cursor_source.session_expiry(db))
        finally:
            shutil.rmtree(tmp)


class AccountControlTest(unittest.TestCase):
    """The optional account control command (synthetic accounts, no real command)."""

    def make(self, state=None, order_answer=None, started=None):
        calls, spawned = [], []
        st = state or {"active": "work", "order": ["work", "home", "spare"],
                       "accounts": [{"tag": "work"}, {"tag": "home"}, {"tag": "spare"}],
                       "activation": {"phase": "done", "started_at": "2020-01-01T00:00:00+00:00"}}

        def runner(args, timeout):
            calls.append(args)
            verb = args[2]
            data = st if verb == "state" else (order_answer or {"ok": True, "order": args[3].split(",")})
            return types.SimpleNamespace(returncode=0 if data.get("ok", True) else 2, stdout=json.dumps(data) + "\n")

        ctl = latidos.AccountControl({"account_control": ["python3", "ctl.py"]}, runner=runner,
                                     spawner=lambda args: spawned.append(args))
        return ctl, calls, spawned, st

    def test_off_without_a_command(self):
        ctl = latidos.AccountControl({})
        self.assertEqual(ctl.state(), {"enabled": False})
        with self.assertRaises(PermissionError):
            ctl.activate("work")
        with self.assertRaises(PermissionError):
            ctl.order(["work"])

    def test_state_is_cached_and_marked(self):
        ctl, calls, _, _ = self.make()
        a = ctl.state()
        self.assertTrue(a["enabled"])
        self.assertFalse(a["busy"])
        ctl.state()
        self.assertEqual(len(calls), 1, "cached for TTL seconds")
        ctl.state(fresh=True)
        self.assertEqual(len(calls), 1, "fresh at most every FRESH_MIN_S (audit of 77)")
        ctl.at -= ctl.FRESH_MIN_S + 1
        ctl.state(fresh=True)
        self.assertEqual(len(calls), 2)

    def test_activate_spawns_once_and_reports_busy(self):
        ctl, _, spawned, st = self.make()
        self.assertEqual(ctl.activate("home")["tag"], "home")
        self.assertEqual(spawned, [["python3", "ctl.py", "activate", "home"]])
        with self.assertRaises(latidos.ControlBusy):
            ctl.activate("spare")  # the command has not written its start yet
        st["activation"] = {"phase": "moving", "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        ctl.started = 0
        with self.assertRaises(latidos.ControlBusy):
            ctl.activate("spare")
        st["activation"] = {"phase": "moving", "started_at": "2020-01-01T00:00:00+00:00"}
        with self.assertRaises(latidos.ControlBusy):
            ctl.activate("spare")  # the command's phase decides, however long (audit of 78)
        st["activation"] = {"phase": "done", "error": "stale", "started_at": "2020-01-01T00:00:00+00:00"}
        ctl.activate("spare")  # the command reports a dead run as done
        self.assertEqual(len(spawned), 2)

    def test_launch_is_judged_by_the_server(self):
        ctl, _, _, st = self.make()
        self.assertIsNone(ctl.state()["launch"], "nothing launched yet")
        r = ctl.activate("home")
        out = ctl.state(fresh=True)
        self.assertEqual(out["launch"], {"started_ms": r["started_ms"], "tag": "home", "pending": True, "failed": False})
        ctl.started -= ctl.LAUNCH_WAIT_S + 1  # the command never wrote its start
        ctl.at = 0
        out = ctl.state()
        self.assertTrue(out["launch"]["failed"])
        self.assertFalse(out["launch"]["pending"])
        self.assertFalse(out["busy"])
        st["activation"] = {"phase": "done", "ok": True, "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        ctl.at = 0
        self.assertFalse(ctl.state()["launch"]["failed"], "caught up")

    def test_busy_names_the_account_just_launched(self):
        # Audit of 79: until the command reports, the previous run's account was named.
        ctl, _, _, st = self.make()
        st["activation"] = {"tag": "work", "phase": "done", "ok": True, "started_at": "2020-01-01T00:00:00+00:00"}
        ctl.activate("home")
        with self.assertRaises(latidos.ControlBusy) as cm:
            ctl.activate("spare")
        self.assertEqual(cm.exception.tag, "home")

    def test_busy_names_the_running_account(self):
        ctl, _, _, st = self.make()
        st["activation"] = {"tag": "spare", "phase": "moving", "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        with self.assertRaises(latidos.ControlBusy) as cm:
            ctl.activate("home")
        self.assertEqual(cm.exception.tag, "spare")

    def test_two_concurrent_activations_launch_one(self):
        # Audit of 77: check and spawn were not under one lock.
        ctl, _, spawned, _ = self.make()
        slow = ctl.spawn
        ctl.spawn = lambda args: (time.sleep(0.05), slow(args))
        errors = []

        def go(tag):
            try:
                ctl.activate(tag)
            except latidos.ControlBusy:
                errors.append(tag)
        ts = [threading.Thread(target=go, args=(t,)) for t in ("home", "spare")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(spawned), len(errors)), (1, 1))

    def test_failed_spawn_does_not_block(self):
        ctl, _, _, _ = self.make()
        def boom(args):
            raise FileNotFoundError("no python")
        ctl.spawn = boom
        with self.assertRaises(OSError):
            ctl.activate("home")
        self.assertEqual(ctl.started, 0.0)
        self.assertFalse(ctl.state(fresh=True)["busy"])

    def test_activate_refuses_unknown_or_odd_tags(self):
        ctl, _, spawned, _ = self.make()
        for tag in ("nobody", "../x", "", None, 3, "a b"):
            with self.assertRaises(ValueError):
                ctl.activate(tag)
        self.assertEqual(spawned, [])

    def test_order_validates_and_passes_through(self):
        ctl, calls, _, _ = self.make()
        self.assertEqual(ctl.order(["spare", "work", "home"])["order"], ["spare", "work", "home"])
        self.assertEqual(calls[-1], ["python3", "ctl.py", "order", "spare,work,home"])
        for bad in ([], ["work", "work"], ["nobody"], "work,home", ["x;rm"], [1]):
            with self.assertRaises(ValueError):
                ctl.order(bad)

    def test_order_refused_by_the_command(self):
        ctl, _, _, _ = self.make(order_answer={"ok": False, "error": "bad_order"})
        with self.assertRaises(ValueError):
            ctl.order(["work"])

    def test_broken_command_keeps_the_last_state(self):
        ctl, _, _, _ = self.make()
        ctl.state()
        ctl.run = lambda args, timeout: types.SimpleNamespace(returncode=1, stdout="Traceback")
        ctl.at -= ctl.FRESH_MIN_S + 1
        out = ctl.state(fresh=True)
        self.assertEqual(out["error_code"], "control_failed")
        self.assertEqual(out["active"], "work", "last good state kept")


class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        os.environ["LATIDOS_ROOTS"] = os.path.join(cls.tmp, "none")
        os.environ["LATIDOS_DB"] = os.path.join(cls.tmp, "l.db")
        os.environ["LATIDOS_FX_URL"] = "http://127.0.0.1:9/never"
        os.environ["LATIDOS_PORT"] = "7599"
        cls.app = latidos.App(latidos.config())
        cls.srv = latidos.Server(("127.0.0.1", 7599), latidos.make_handler(cls.app))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.app.con.close()
        for k in ("LATIDOS_ROOTS", "LATIDOS_DB", "LATIDOS_FX_URL", "LATIDOS_PORT"):
            os.environ.pop(k, None)
        shutil.rmtree(cls.tmp)

    def test_summary_is_strict_json_with_an_infinite_agent_cost(self):
        # Audit of 105: one agent row with usd = inf must never put Infinity in the answer.
        with self.app.lock:
            self.app.con.execute("INSERT OR REPLACE INTO agent_calls (agent, id, ts, session, model, inp, out, cread, cwrite, usd) "
                                 "VALUES ('cline', 'inf-row', ?, 's', 'mystery-model', 10, 1, 0, 0, ?)",
                                 (int(time.time() * 1000) - 60000, float("inf")))
            self.app.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
            self.app.con.commit()
        try:
            status, _ctype, _csp, body = self.req("/api/summary?range=7d")
            self.assertEqual(status, 200)
            self.assertNotIn(b"Infinity", body)
            self.assertNotIn(b"NaN", body)

            def no_constants(name):
                raise ValueError("non-finite number in JSON: " + name)
            d = json.loads(body, parse_constant=no_constants)
            self.assertIn("cline", [a["id"] for a in d["agents"]])
        finally:
            with self.app.lock:
                self.app.con.execute("DELETE FROM agent_calls WHERE id = 'inf-row'")
                self.app.con.commit()

    def req(self, path, method="GET", host="127.0.0.1:7599"):
        c = http.client.HTTPConnection("127.0.0.1", 7599, timeout=5)
        c.request(method, path, headers={"Host": host})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, r.getheader("Content-Type"), r.getheader("Content-Security-Policy"), body

    def post(self, path, body, headers=None, host="127.0.0.1:7599"):
        c = http.client.HTTPConnection("127.0.0.1", 7599, timeout=5)
        h = {"Host": host, "Content-Type": "application/json", "X-Latidos": "1"}
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, r.read()
        c.close()
        return out

    def test_account_control_routes(self):
        saved = self.app.control
        calls = []

        class Fake:
            def state(self, fresh=False):
                return {"enabled": True, "active": "work", "fresh": fresh}

            def activate(self, tag):
                calls.append(("activate", tag))
                if tag == "busy":
                    raise latidos.ControlBusy("running", "spare")
                if tag != "home":
                    raise ValueError("unknown account")
                return {"ok": True, "tag": tag, "started": True}

            def order(self, order):
                calls.append(("order", order))
                return {"ok": True, "order": order}
        self.app.control = Fake()
        try:
            st, _, _, b = self.req("/api/claude-control?fresh=1")
            self.assertEqual((st, json.loads(b)["fresh"]), (200, True))
            self.assertEqual(self.post("/api/claude-control/activate", {"tag": "home"}, {"X-Latidos": None})[0], 403)
            self.assertEqual(self.post("/api/claude-control/activate", {"tag": "home"}, host="evil.example:7599")[0], 421)
            self.assertEqual(calls, [], "refused before reaching the control")
            self.assertEqual(self.post("/api/claude-control/activate", {"tag": "home"})[0], 202)
            self.assertEqual(self.post("/api/claude-control/activate", {"tag": "nope"})[0], 400)
            st, b = self.post("/api/claude-control/activate", {"tag": "busy"})
            self.assertEqual((st, json.loads(b)["tag"]), (409, "spare"))
            st, b = self.post("/api/claude-control/order", {"order": ["home", "work"]})
            self.assertEqual((st, json.loads(b)["order"]), (200, ["home", "work"]))
            self.assertEqual(self.req("/api/claude-control/activate", method="OPTIONS")[0], 405)
        finally:
            self.app.control = saved
        self.assertEqual(self.app.control.state(), {"enabled": False}, "off by default in tests")

    def test_logins_routes_and_hide(self):
        saved = self.app.logins
        calls = []

        class Fake:
            def status(self):
                return {"enabled": True, "claude": [{"tag": "work", "state": "ok"}], "cursor": None}

            def renew(self, tag):
                calls.append(tag)
                if tag == "busy":
                    raise latidos.ControlBusy("running", "busy")
                if tag != "work":
                    raise ValueError("unknown account")
                return {"ok": True, "tag": tag}
        self.app.logins = Fake()
        try:
            st, _, _, b = self.req("/api/logins")
            self.assertEqual((st, json.loads(b)["claude"][0]["tag"]), (200, "work"))
            self.assertEqual(self.post("/api/logins/renew", {"tag": "work"}, {"X-Latidos": None})[0], 403)
            self.assertEqual(self.post("/api/logins/renew", {"tag": "work"}, host="evil.example:7599")[0], 421)
            self.assertEqual(calls, [])
            self.assertEqual(self.post("/api/logins/renew", {"tag": "work"})[0], 202)
            self.assertEqual(self.post("/api/logins/renew", {"tag": "nope"})[0], 400)
            self.assertEqual(self.post("/api/logins/renew", {"tag": "busy"})[0], 409)
        finally:
            self.app.logins = saved
        st, _, _, b = self.req("/api/summary?range=7d&hide=acct:work")
        d = json.loads(b)
        self.assertEqual((st, d["hidden"]), (200, ["acct:work"]))
        self.assertIn("cursor_totals", d)  # Cursor hidden still counts in the shares
        self.assertEqual(self.req("/api/summary?range=7d&hide=cursor")[0], 400)

    def test_edits_need_json_and_the_page_header(self):
        cfg = os.path.join(self.tmp, "cfg")
        os.environ["LATIDOS_ALIASES"] = os.path.join(cfg, "aliases.json")
        os.environ["LATIDOS_ASSIGN"] = os.path.join(cfg, "assign.json")
        saved = (dict(latidos.ALIASES), dict(latidos.ASSIGN))
        try:
            body = {"key": "s:abc", "project": "Moved"}
            self.assertEqual(self.post("/api/assign", body, {"X-Latidos": None})[0], 403)
            self.assertEqual(self.post("/api/assign", body, {"Content-Type": "text/plain"})[0], 403)
            self.assertEqual(self.post("/api/assign", body, host="evil.example:7599")[0], 421)
            self.assertEqual(self.post("/api/assign", b"x" * 5000)[0], 413)
            self.assertEqual(self.post("/api/assign", {"key": "nope", "project": "x"})[0], 400)
            self.assertEqual(self.post("/api/other", body)[0], 405)
            self.assertEqual(self.req("/api/assign", method="OPTIONS")[0], 405)
            self.assertFalse(os.path.exists(os.environ["LATIDOS_ASSIGN"]))
            st, b = self.post("/api/assign", body)
            self.assertEqual((st, json.loads(b)), (200, {"ok": True, "project": "Moved"}))
            st, b = self.post("/api/rename", {"from": "Moved", "to": "Done here"})
            self.assertEqual((st, json.loads(b)), (200, {"ok": True, "name": "Done here"}))
            with open(os.environ["LATIDOS_ASSIGN"]) as fh:
                self.assertEqual(json.load(fh), {"s:abc": "Moved"})
            with open(os.environ["LATIDOS_ALIASES"]) as fh:
                self.assertEqual(json.load(fh), {"Moved": "Done here"})
            with open(os.environ["LATIDOS_ASSIGN"], "w") as fh:
                fh.write("{oops")
            st, b = self.post("/api/assign", {"key": "s:def", "project": "X"})
            self.assertEqual(st, 409, b)
            self.assertEqual(json.loads(b)["file"], "assign.json")
            with open(os.environ["LATIDOS_ASSIGN"]) as fh:
                self.assertEqual(fh.read(), "{oops")
            # A body shorter than its Content-Length does not hold a thread forever.
            latidos.POST_BODY_TIMEOUT_S = 1
            c = http.client.HTTPConnection("127.0.0.1", 7599, timeout=20)
            c.putrequest("POST", "/api/rename", skip_host=True)
            for k, v in (("Host", "127.0.0.1:7599"), ("Content-Type", "application/json"),
                         ("X-Latidos", "1"), ("Content-Length", "100")):
                c.putheader(k, v)
            c.endheaders(b'{"from"')
            t0 = time.time()
            r = c.getresponse(); r.read(); c.close()
            self.assertEqual(r.status, 408)
            self.assertLess(time.time() - t0, 5)
        finally:
            latidos.POST_BODY_TIMEOUT_S = 10
            for k in ("LATIDOS_ALIASES", "LATIDOS_ASSIGN"):
                os.environ.pop(k, None)
            latidos.ALIASES.clear(); latidos.ALIASES.update(saved[0])
            latidos.ASSIGN = dict(saved[1])
            latidos._aliases_seen.update(mtime=None, checked=0.0)
            latidos._assign_seen.update(mtime=None, checked=0.0)

    def test_page_and_icons(self):
        for path, ctype in (("/", "text/html"), ("/favicon.svg", "image/svg+xml"),
                            ("/favicon-32.png", "image/png"), ("/apple-touch-icon.png", "image/png"),
                            ("/app.js", "text/javascript"), ("/i18n.js", "text/javascript"),
                            ("/sound.js", "text/javascript")):
            st, ct, csp, _b = self.req(path)
            self.assertEqual(st, 200, path)
            self.assertTrue(ct.startswith(ctype), path)
            self.assertIn("script-src 'self'", csp)

    def test_guards(self):
        self.assertEqual(self.req("/", host="evil.example")[0], 421)
        self.assertEqual(self.req("/api/summary", method="POST")[0], 405)
        self.assertEqual(self.req("/../latidos.py")[0], 404)
        self.assertEqual(self.req("/latidos.py")[0], 404)
        self.assertEqual(self.req("/api/summary?from=10&to=5")[0], 400)
        self.assertEqual(self.req("/api/summary?from=abc")[0], 400)
        self.assertEqual(self.req("/api/summary?from=0&to=inf")[0], 400)
        self.assertEqual(self.req("/api/summary?range=nope")[0], 200)  # unknown name: whole history

    def test_api_shapes(self):
        st, _ct, _c, body = self.req("/api/summary")
        self.assertEqual(st, 200)
        d = json.loads(body)
        for k in ("totals", "projects", "timeline", "moments", "accounts", "fx", "live", "alltime"):
            self.assertIn(k, d)
        # L-121: the page words moments from kind and rank, not from the Spanish label.
        for m in d["moments"]:
            self.assertIn(m["kind"], ("first", "peak_day", "peak_hour", "session"))
            if m["kind"] == "session":
                self.assertIsInstance(m["rank"], int)
        self.assertEqual(len(json.loads(self.req("/api/pulse?minutes=7")[3])["minutes"]), 7)
        self.assertEqual(len(json.loads(self.req("/api/pulse?minutes=1")[3])["minutes"]), 5)  # clamped
        self.assertIn("events", json.loads(self.req("/api/live?since=0")[3]))
        cur = json.loads(self.req("/api/summary?range=7d&cursor=1")[3])
        self.assertIn("cursor", cur)
        for k in ("totals", "projects", "days", "models", "status"):
            self.assertIn(k, cur["cursor"])

    def test_no_inline_script_or_style_in_page(self):
        with open(os.path.join(HERE, "web", "index.html")) as fh:
            html = fh.read()
        self.assertNotIn("<script>", html)
        self.assertNotIn("style=", html)
        self.assertNotIn("<style", html)

    def test_hovered_label_stays_above_hit_labels(self):
        # .hit is never removed after its pop, so the last z-index of .label.hot must beat it.
        import re
        with open(os.path.join(HERE, "web", "app.css")) as fh:
            css = fh.read()
        z = lambda sel: [int(v) for v in re.findall(re.escape(sel) + r"\s*\{[^}]*z-index:\s*(\d+)", css)][-1]
        self.assertGreater(z(".label.hot"), z(".label.hit"))

    def test_reduced_motion_overrides_come_after_the_sheet_animations(self):
        # A media query adds no specificity: the override must come later in the file.
        with open(os.path.join(HERE, "web", "app.css")) as fh:
            css = fh.read()
        self.assertGreater(css.rindex(".fresh.now { animation: none; }"), css.index("animation: fresh-beat"))
        self.assertGreater(css.rindex(".row-tag { animation-duration: 1.2s; }"), css.index("animation: row-tag"))

    def test_project_colour_comes_from_one_helper(self):
        # colorOf gives grey past the palette; everything drawn per project uses projectColor.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertEqual(js.count("colorOf("), 2)   # its definition and projectColor's call

    def test_theme_change_repaints_inline_colours(self):
        # Ranking rows, Cursor's list and an open sheet hold colours computed for one theme.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        body = js[js.index("function drawAll()"):]
        body = body[:body.index("\n")]
        for call in ("renderRanking(S.data)", "renderCursor(S.data)", "refreshSheet(S.data)"):
            self.assertIn(call, body)

    def test_small_cells_stay_put_and_are_easy_to_hit(self):
        # A tiny cell (the only task early in the day) leaned away from the pointer from
        # 60 px out but was only grabbable within 6 px of its edge, so it could not be clicked.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertIn("var LEAN_MIN_R = 24, MIN_HIT_R = 16;", js)
        step = js[js.index("// Pointer: cells lean away"):]
        step = step[:step.index("\n      }")]
        self.assertIn("b.r * cam.z >= LEAN_MIN_R &&", step)
        hit = js[js.index("function at(x, y)"):]
        hit = hit[:hit.index("return best;")]
        self.assertIn("Math.max(b.r + 6 / cam.z, MIN_HIT_R / cam.z)", hit)

    def test_spark_impact_scales_with_tokens(self):
        # Fatter spark and harder landing the more tokens a call carries (L-136).
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertIn("return Math.max(0, Math.min(1, (Math.log10(1 + (tokens || 0)) - 4) / 2));", js)
        self.assertIn("size = 1.2 + power * 7", js)
        self.assertIn("impact(b, s, pw);", js)
        imp = js[js.index("function impact(b, s, pw)"):]
        imp = imp[:imp.index("\n    }\n")]
        self.assertIn("if (REDUCED", imp)
        self.assertIn("/ (1 + b.r / 50) / cam.z", imp)
        self.assertIn("if (!stays(b))", imp)
        self.assertIn("o.dead || stays(o)", imp)
        st = js[js.index("function stays(o)"):]
        st = st[:st.index("}")]
        self.assertIn("o === hover || (drag && drag.b === o) || o.r * cam.z < LEAN_MIN_R", st)
        self.assertIn("var nr = REDUCED ? 1 : pw < 0.4 ? 1 : pw < 0.75 ? 2 : 3;", js)

    def test_hovered_cell_holds_against_collisions(self):
        # Audit of 69: a knocked neighbour pushed the hovered cell out from under the pointer.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertIn("else if (a === hover) { wa = 0.04; wo = 1; }", js)
        self.assertIn("else if (o === hover) { wo = 0.04; wa = 1; }", js)
        step = js[js.index("function step(dt, t)"):]
        step = step[:step.index("for (var i = 0;")]
        self.assertIn("var hv = belowField(lastScreen.y) ? null : at(pointer.x, pointer.y);", step)
        self.assertIn("if (since < 650 && !REDUCED)", js)

    def test_hovered_cell_is_anchored(self):
        # Audit of 70: pulled to the centre, the hovered cell slid out from under the pointer.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        step = js[js.index("function step(dt, t)"):]
        step = step[:step.index("// Keep clear of the hero numbers")]
        self.assertIn("if (b === hover) {", step)
        self.assertIn("b.vx *= Math.pow(0.6, f); b.vy *= Math.pow(0.6, f);", step)
        self.assertIn("if (!REDUCED && b !== hover) {", step)
        self.assertIn("if (!drag && !pan && lastScreen) {", step)

    def test_spark_power_curve(self):
        # Same formula as sparkPower in app.js: never negative (a tiny call drew a negative arc radius).
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        fn = js[js.index("function sparkPower(tokens)"):]
        fn = fn[:fn.index("\n    }") + 6]
        out = subprocess.run([node, "-e", fn + "console.log(JSON.stringify([0, 10, 10000, 100000, 1e6, 5e7].map(sparkPower)))"],
                             capture_output=True, text=True, check=True).stdout
        vals = json.loads(out)
        self.assertEqual(vals[0], 0); self.assertEqual(vals[1], 0); self.assertAlmostEqual(vals[2], 0, places=3)
        self.assertAlmostEqual(vals[3], 0.5, places=2)
        self.assertAlmostEqual(vals[4], 1, places=3); self.assertEqual(vals[5], 1)

    def test_rebuilt_lists_keep_keyboard_focus(self):
        # Ranking and Cursor rows are rebuilt on reloads and theme changes; focus follows.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertIn('refocus($("rank"), keep)', js)
        self.assertIn("refocus(ol, keep)", js)
        self.assertEqual(js.count("li.dataset.name = p.name"), 2)

    def test_closing_the_sheet_refocuses_a_rebuilt_opener_row(self):
        # The row that opened the sheet may be rebuilt meanwhile; focus goes to its successor.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        close = js[js.index("function closeSheet()"):]
        close = close[:close.index("\n  }")]
        self.assertIn("refocus(openerList, openerName)", close)

    def test_account_cards_bigger_and_solid(self):
        # 10 % bigger on the desktop, no blur or shadow between the cards.
        with open(os.path.join(HERE, "web", "app.css")) as fh:
            css = fh.read()
        self.assertIn("@media (min-width: 761px) { .quota { zoom: 1.1; } }", css)
        last = css[css.rindex(".qcard {"):]
        self.assertTrue(last.startswith(".qcard { background: var(--bg); backdrop-filter: none;"))
        self.assertIn("box-shadow: none", last.split("}", 1)[0])

    def test_cursor_growth_never_compares_snapshot_data(self):
        # The flag lives with the data: set by the snapshot paint, cleared by a server answer.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertIn("S.data = snap.d; S.dataFromSnap = true;", js)
        self.assertIn("S.data = d; S.dataFromSnap = false;", js)
        self.assertIn("|| S.dataFromSnap || S.dataKey !== rangeKey()) { cursorPrev = null; return; }", js)
        self.assertIn("S.dataKey = key;", js)            # a server answer names its view
        self.assertIn("S.dataKey = rangeKey(); render(true);", js)
        # A restart seen by the live poll voids the old process's data as a baseline.
        boot = js[js.index("// New server process"):]
        self.assertIn("S.dataKey = null;", boot[:boot.index("return;")])
        self.assertNotIn("S.fromSnap", js)

    def test_quota_percent_never_wraps(self):
        # "100 %" was wider than its 34 px column and broke onto two lines.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            self.assertIn('Math.round(pct) + (I.getLang() === "es" ? "\\u00a0%" : "%")', fh.read())
        with open(os.path.join(HERE, "web", "app.css")) as fh:
            css = fh.read()
        self.assertIn(".q-row .pct { white-space: nowrap; }", css)
        self.assertNotIn("minmax(0, 1fr) 34px", css)
        # A fixed column, the same in every row, so the bars of a card line up.
        self.assertNotIn("minmax(34px, auto)", css)
        self.assertIn(".q-row { grid-template-columns: 44px minmax(0, 1fr) 42px 62px;", css)
        self.assertIn("@container (max-width: 230px)", css)
        # Narrow desktop column: one line per row, never stacked (taller cards hid Cursor's).
        narrow = css[css.index("@container (max-width: 230px)"):]
        narrow = narrow[:narrow.index("\n}")]
        self.assertNotIn("grid-column: 2 / -1", narrow)
        # "Semana"/"Incluido" (10 px) fit 38 px, the longest reset text ("9 h 59 min") 56 px.
        self.assertIn("grid-template-columns: 38px minmax(0, 1fr) 40px 56px", narrow)
        self.assertIn(".q-row .lab { font-size: 10px; }", narrow)

    def test_gloss_drawn_after_the_flash_rim(self):
        # The flash rim strokes the current path; the gloss arc before it became a hard ring.
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        self.assertGreater(js.index("// Gloss:"), js.index("// White-hot rim"))

    def test_cards_replace_the_source_chips_and_the_vessels(self):
        # L-158/L-160: no Claude/Cursor chips and no "Where it comes from" card; the cards
        # carry an eye switch, a use row and a session line; the ROI line sits under them.
        with open(os.path.join(HERE, "web", "index.html")) as fh:
            html = fh.read()
        for gone in ('id="claude-toggle"', 'id="cursor-toggle"', 'id="vessels"', 'class="accounts'):
            self.assertNotIn(gone, html)
        # Under the hero's money, so it shows without any account card (audit of 86).
        self.assertIn('<p class="mix q-roi" id="q-roi" hidden></p>', html)
        self.assertLess(html.index('data-i18n="hero.money.note"'), html.index('id="q-roi"'))
        self.assertLess(html.index('id="q-roi"'), html.index('id="quota"'))
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            js = fh.read()
        for gone in ("S.claude", "vessels.", "claude-toggle", "cursor-toggle", "accRect", "paintSourceToggles"):
            self.assertNotIn(gone, js)
        self.assertIn('"&hide=" + encodeURIComponent(', js)
        self.assertIn("hiddenKey()", js[js.index("function snapKey()"):][:200])
        # A hidden account's live call only notes its card's use, it never sparks.
        self.assertIn("if (!shownEv(ev)) { noteUse(ev); return; }", js)
        # Audit of 86: the live poll carries the filter (ECG line), hidden calls are judged
        # by origin like the server, the river follows Cursor when every Claude card is off,
        # no accounts source keeps Cursor shown, and an eye keeps its focus.
        self.assertIn('getJSON("/api/live?since=" + S.seq + hideParam())', js)
        self.assertIn('var o = ev.origin || (ev.account ? "acct:" + ev.account : "");', js)
        self.assertIn("river.set(withAgents(onlyCursor() && d.cursor ? cursorRiver(d) : d, d));", js)
        self.assertIn('if (d && d.error === "no account source" && !S.cursor) setShown("cursor", true);', js)
        self.assertIn("""fa.dataset.eye ? 'button[data-eye="' + fa.dataset.eye + '"]'""", js)
        self.assertIn("includesNow() && shownEv(item.ev)) onLive(item.ev);", js)
        # The eye and Renew buttons never start a drag of the card.
        drag = js[js.index('$("q-list").addEventListener("pointerdown"'):]
        self.assertIn('if (e.target.closest("button")) return;', drag[:200])
        with open(os.path.join(HERE, "web", "i18n.js")) as fh:
            i18n = fh.read()
        for key in ("eye.hide", "eye.show", "eye.last", "quota.use", "quota.use.tip", "sheet.last.tip", "login.until", "login.renew", "login.cursor.tip"):
            self.assertEqual(i18n.count('"%s":' % key), 2, key)  # English and Spanish
        for gone in ("acc.title", "tools.hide"):
            self.assertNotIn('"%s":' % gone, i18n)


class CursorSource(unittest.TestCase):
    """cursor_source against fake Cursor files with the real shapes."""

    def setUp(self):
        import cursor_source as cs
        self.cs = cs
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.vscdb")
        self.track = os.path.join(self.tmp, "track.db")
        self.chats = os.path.join(self.tmp, "chats")
        st = sqlite3.connect(self.state)
        st.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
        day = 1794470400000  # 2026-11-12 local-ish
        convs = {
            "aaa": {"name": "Fix the gateway", "createdAt": day, "lastUpdatedAt": day + 5000,
                    "modelConfig": {"modelName": "claude-opus-5-5"}, "unifiedMode": "agent",
                    "originalFileStates": {"file://" + OTHER_HOME + "/code/projects/infra-repo/main.tf": {}}},
            "sub": {"name": "Subagent", "createdAt": day + 100, "lastUpdatedAt": day + 200,
                    "subagentInfo": {"parentComposerId": "aaa"}},
            "old": {"name": "Legacy", "createdAt": day - 200 * 86400000, "lastUpdatedAt": day - 200 * 86400000,
                    "usageData": {"claude-3.7-sonnet": {"costInCents": 250, "amount": 3}}},
            "draft": {},
        }
        for cid, v in convs.items():
            st.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("composerData:" + cid, json.dumps(v)))
        iso = lambda ms: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))
        bubbles = [("aaa", "b1", 1, day + 1000, "claude-opus-5-5", 0, 0), ("aaa", "b2", 2, day + 2000, None, 0, 0),
                   ("aaa", "b3", 1, day + 3000, "claude-opus-5-5", 0, 0), ("sub", "b1", 1, day + 150, None, 0, 0),
                   ("old", "b1", 1, day - 200 * 86400000, "claude-3.7-sonnet", 1000, 200)]
        for cid, bid, typ, ts, model, itok, otok in bubbles:
            v = {"type": typ, "createdAt": iso(ts), "tokenCount": {"inputTokens": itok, "outputTokens": otok}}
            if model:
                v["modelInfo"] = {"modelName": model}
            st.execute("INSERT INTO cursorDiskKV VALUES (?,?)", ("bubbleId:%s:%s" % (cid, bid), json.dumps(v)))
        st.commit(); st.close()
        tr = sqlite3.connect(self.track)
        tr.execute("CREATE TABLE ai_code_hashes (conversationId TEXT, fileName TEXT)")
        tr.execute("INSERT INTO ai_code_hashes VALUES ('old', ?)", (H + "/.treehouse/demo-hub-1f5788/1/x.py",))
        tr.commit(); tr.close()
        os.makedirs(os.path.join(self.chats, "h1", "agent1"))
        with open(os.path.join(self.chats, "h1", "agent1", "meta.json"), "w") as fh:
            json.dump({"createdAtMs": day + 10, "updatedAtMs": day + 20, "cwd": H + "/code/agent-skeleton"}, fh)
        self.con = latidos.connect(os.path.join(self.tmp, "l.db"))
        self.lock = threading.RLock()
        self.ing = cs.CursorIngestor(self.con, self.lock, latidos.project_of, self.state, self.track, self.chats)
        self.day = day

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.tmp)

    def test_ingest_attribution_and_summary(self):
        self.assertEqual(self.ing.scan(), 4)   # aaa, sub, old and the CLI chat; the empty draft skipped
        self.assertEqual(self.ing.scan(), 0)   # nothing changed: nothing re-read
        rows = dict((r[0], r[1:]) for r in self.con.execute("SELECT id, project, root, turns, cents FROM cursor_convs"))
        self.assertEqual(rows["aaa"][0], "infra-repo")       # old Linux path mapped to this home
        self.assertEqual(rows["sub"][1], "aaa")                   # subagent folds into its root
        self.assertEqual(rows["old"][0], "demo-hub")       # from ai-tracking
        self.assertEqual(rows["old"][3], 250)
        self.assertEqual(rows["cli:agent1"][0], "agent-skeleton")
        d = self.cs.summarize_cursor(self.con, self.lock, self.day - 86400000, self.day + 86400000)
        names = {p["name"]: p for p in d["projects"]}
        self.assertEqual(names["infra-repo"]["turns"], 3)    # 2 own + 1 from the subagent
        self.assertEqual(names["infra-repo"]["convs"], 1)
        self.assertEqual(d["totals"]["usd"], 0)                    # the legacy one is outside
        allt = self.cs.summarize_cursor(self.con, self.lock, 0, self.day + 86400000)
        self.assertAlmostEqual(allt["totals"]["usd"], 2.5)
        self.assertEqual(allt["totals"]["tokens"], 1200)

    def test_rescan_after_update(self):
        self.ing.scan()
        st = sqlite3.connect(self.state)
        st.execute("UPDATE cursorDiskKV SET value = ? WHERE key = 'composerData:aaa'",
                   (json.dumps({"name": "Renamed", "createdAt": self.day, "lastUpdatedAt": self.day + 9999}),))
        st.commit(); st.close()
        self.assertEqual(self.ing.scan(), 1)
        self.assertEqual(self.con.execute("SELECT name FROM cursor_convs WHERE id='aaa'").fetchone()[0], "Renamed")

    def _token(self, exp):
        import base64
        body = base64.urlsafe_b64encode(json.dumps({"sub": "google-oauth2|user_ABC", "exp": exp}).encode()).decode().rstrip("=")
        st = sqlite3.connect(self.state)
        st.execute("CREATE TABLE IF NOT EXISTS ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        st.execute("INSERT OR REPLACE INTO ItemTable VALUES ('cursorAuth/accessToken', ?)", ("h.%s.SECRETSIG" % body,))
        st.commit(); st.close()

    def _opener(self, pages, seen):
        import io

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=0):
            seen.append((req.full_url, req.get_header("Cookie"), json.loads(req.data)))
            body = pages[min(len(seen), len(pages)) - 1]
            if isinstance(body, Exception):
                raise body
            return Resp(json.dumps(body).encode())
        return opener

    def test_bogus_cursor_figures_are_dropped_never_stored_as_inf(self):
        # Audit of 105: chargedCents 1e999 was stored as inf, and an inf token count raised
        # OverflowError outside any guard, aborting the whole API page every pass.
        cs = self.cs
        for bad in (float("inf"), float("nan"), "1e999", -1, 1e9, "x"):
            self.assertIsNone(cs.sane_cents(bad), repr(bad))
        self.assertEqual(cs.sane_cents(None), 0.0)
        self.assertEqual(cs.sane_cents("250"), 250.0)
        for bad in (float("inf"), 10 ** 13, -5, "x"):
            self.assertIsNone(cs.sane_tokens(bad), repr(bad))
        self.assertEqual(cs.sane_tokens(None), 0)
        self.assertEqual(cs.sane_tokens("12"), 12)
        self.assertEqual(cs.legacy_cents({"a": {"costInCents": float("inf")}, "b": {"costInCents": 4}}), 4.0)
        self._token(time.time() + 3600)
        d0 = self.day + 3600000
        ev = lambda i, cents, tok: {"usageEventId": "b%d" % i, "timestamp": str(d0 + i), "model": "m", "conversationId": "aaa",
                                    "chargedCents": cents, "tokenUsage": {"inputTokens": tok, "outputTokens": 1}}
        pages = [{"totalUsageEventsCount": 4, "usageEventsDisplay": [
            ev(1, float("inf"), 10), ev(2, 50, float("inf")), ev(3, 25, 10), "not an event"]}]
        api = cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(pages, []))
        self.assertEqual(api.scan(now_ms=self.day + 86400000), 1)   # only the sane event, the page goes on
        rows = self.con.execute("SELECT id, cents, in_tok FROM cursor_events").fetchall()
        self.assertEqual(rows, [("b3", 25.0, 10)])

    def test_api_pages_merge_and_never_leak_the_token(self):
        self._token(time.time() + 3600)
        ev = lambda i, ts, conv, cents: {"usageEventId": "e%d" % i, "timestamp": str(ts), "model": "claude-opus-5-5",
                                         "kind": "USAGE_BASED", "conversationId": conv, "isHeadless": False,
                                         "chargedCents": cents,
                                         "tokenUsage": {"inputTokens": 10, "outputTokens": 5, "cacheReadTokens": 100,
                                                        "totalCents": cents * 0.6}}
        d0 = self.day + 3600000
        pages = [{"totalUsageEventsCount": 3, "usageEventsDisplay": [ev(1, d0, "aaa", 250), ev(2, d0 + 1, "sub", 50)]},
                 {"totalUsageEventsCount": 3, "usageEventsDisplay": [ev(3, d0 + 2, "zzz-not-local", 100)]}]
        seen = []
        old_page = self.cs.API_PAGE
        self.cs.API_PAGE = 2
        try:
            api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(pages, seen))
            self.assertEqual(api.scan(now_ms=self.day + 86400000), 3)
        finally:
            self.cs.API_PAGE = old_page
        self.assertEqual(len(seen), 2)
        self.assertTrue(seen[0][0].endswith("/api/dashboard/get-filtered-usage-events"))
        self.assertIn("user_ABC%3A%3Ah.", seen[0][1])
        self.assertNotIn("SECRETSIG", json.dumps(api.progress))
        self.ing.scan()
        d = self.cs.summarize_cursor(self.con, self.lock, self.day - 86400000, self.day + 86400000)
        names = {p["name"]: p for p in d["projects"]}
        self.assertAlmostEqual(names["infra-repo"]["usd"], 3.0)      # aaa + its subagent, from the API
        self.assertEqual(names["infra-repo"]["requests"], 2)
        self.assertEqual(names["infra-repo"]["turns"], 0)            # local turns only before the API starts
        self.assertAlmostEqual(names["Cursor (other)"]["usd"], 1.0)
        # L-121: generated names carry a code the page words in its language.
        self.assertEqual(names["Cursor (other)"]["name_code"], "other")
        self.assertEqual(names["Cursor (other)"]["tasks"][0]["title_code"], "no_conversation")
        self.assertNotIn("name_code", names["infra-repo"])
        self.assertIsNone(names["infra-repo"]["tasks"][0]["title_code"])
        self.assertEqual(d["totals"]["tokens"], 3 * 115)
        # Before the API's first event, the local history (legacy cost) still counts.
        old = self.cs.summarize_cursor(self.con, self.lock, 0, self.day - 86400000)
        self.assertAlmostEqual(old["totals"]["usd"], 2.5)

    def test_cursor_stores_original_names_and_renames_on_display(self):
        self.ing.scan()
        stored = self.con.execute("SELECT project FROM cursor_convs WHERE id='aaa'").fetchone()[0]
        self.assertEqual(stored, "infra-repo")
        d = self.cs.summarize_cursor(self.con, self.lock, self.day - 86400000, self.day + 86400000,
                                     rename=lambda n: "Infra" if n == "infra-repo" else n)
        self.assertIn("Infra", [p["name"] for p in d["projects"]])

    def test_malformed_json_and_cli_join(self):
        st = sqlite3.connect(self.state)
        st.execute("INSERT INTO cursorDiskKV VALUES ('composerData:bad', '{not json')")
        st.execute("INSERT INTO cursorDiskKV VALUES ('bubbleId:aaa:bad', '{nope')")
        st.commit(); st.close()
        self.assertEqual(self.ing.scan(), 4)  # the bad rows are skipped, the pass completes
        self.con.execute("INSERT INTO cursor_events VALUES ('e9', ?, 'm', 'k', 'agent1', 1, 1, 1, 100, 1, 50)",
                         (self.day + 5000,))
        self.con.commit()
        d = self.cs.summarize_cursor(self.con, self.lock, self.day - 86400000, self.day + 86400000)
        names = {p["name"]: p for p in d["projects"]}
        self.assertNotIn("Cursor (other)", names)
        self.assertAlmostEqual(names["agent-skeleton"]["usd"], 1.0)   # the CLI chat's project

    def test_redirects_never_carry_the_session(self):
        handler = self.cs._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/x"))

    def test_unexpected_api_errors_do_not_escape(self):
        self._token(time.time() + 3600)
        api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener([RuntimeError("boom")], []))
        self.assertEqual(api.scan(), 0)
        self.assertIn("RuntimeError", api.progress["error"])

    def _events(self, n, base):
        return [{"usageEventId": "e%d" % i, "timestamp": str(base + i), "model": "m", "kind": "k",
                 "conversationId": "aaa", "chargedCents": 1, "tokenUsage": {"inputTokens": 1, "outputTokens": 1,
                                                                            "cacheReadTokens": 0, "totalCents": 1}}
                for i in range(n)]

    def test_api_failed_page_is_read_again(self):
        import urllib.error
        self._token(time.time() + 3600)
        evs = self._events(5, self.day)
        old_page = self.cs.API_PAGE
        self.cs.API_PAGE = 2
        try:
            first = [{"totalUsageEventsCount": 5, "usageEventsDisplay": evs[0:2]}, urllib.error.URLError("down")]
            api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(first, []))
            api.scan(now_ms=self.day + 3600000)
            self.assertIn("no connection", api.progress["error"])
            self.assertEqual(api.progress["error_code"], "offline")
            self.assertEqual(api.progress["error_detail"], "URLError")
            self.assertIsNone(self.con.execute("SELECT v FROM meta WHERE k='cursor_api_done_until'").fetchone())
            second = [{"totalUsageEventsCount": 5, "usageEventsDisplay": evs[0:2]},
                      {"totalUsageEventsCount": 5, "usageEventsDisplay": evs[2:4]},
                      {"totalUsageEventsCount": 5, "usageEventsDisplay": evs[4:5]}]
            seen = []
            api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(second, seen))
            api.scan(now_ms=self.day + 7200000)
        finally:
            self.cs.API_PAGE = old_page
        # Nothing was marked done, so the second pass reads the full history window again.
        self.assertEqual(seen[0][2]["startDate"], str(self.day + 7200000 - self.cs.API_HISTORY_DAYS * 86400000))
        self.assertIsNotNone(self.con.execute("SELECT v FROM meta WHERE k='cursor_api_done_until'").fetchone())
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM cursor_events").fetchone()[0], 5)

    def test_page_cap_reads_the_older_part_next(self):
        self._token(time.time() + 3600)
        evs = self._events(5, self.day)
        newest_first = list(reversed(evs))
        old_page, old_cap = self.cs.API_PAGE, self.cs.API_MAX_PAGES
        self.cs.API_PAGE, self.cs.API_MAX_PAGES = 2, 1
        try:
            p1 = [{"totalUsageEventsCount": 5, "usageEventsDisplay": newest_first[0:2]}]
            api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(p1, []))
            api.scan(now_ms=self.day + 3600000)
            narrow = self.con.execute("SELECT v FROM meta WHERE k='cursor_api_narrow_end'").fetchone()
            self.assertEqual(int(narrow[0]), self.day + 3)     # the oldest event read so far
            seen = []
            p2 = [{"totalUsageEventsCount": 3, "usageEventsDisplay": newest_first[2:4]},
                  {"totalUsageEventsCount": 3, "usageEventsDisplay": newest_first[4:5]}]
            self.cs.API_MAX_PAGES = 5
            api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(p2, seen))
            api.scan(now_ms=self.day + 7200000)
        finally:
            self.cs.API_PAGE, self.cs.API_MAX_PAGES = old_page, old_cap
        self.assertEqual(seen[0][2]["endDate"], str(self.day + 4))   # narrowed to the unread part
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM cursor_events").fetchone()[0], 5)
        done = self.con.execute("SELECT v FROM meta WHERE k='cursor_api_done_until'").fetchone()
        self.assertEqual(int(done[0]), self.day + 3600000 + 60000)   # the original window
        self.assertIsNone(self.con.execute("SELECT v FROM meta WHERE k='cursor_api_narrow_end'").fetchone())

    def test_newest_cheap_conversation_is_kept_past_the_top(self):
        self.ing.scan()
        for i in range(4):
            self.con.execute("INSERT INTO cursor_events VALUES (?, ?, 'm', 'k', ?, 1, 1, 1, 900, 0, 900)",
                             ("x%d" % i, self.day + i * 1000, "conv-old%d" % i))
        self.con.execute("INSERT INTO cursor_events VALUES ('xn', ?, 'm', 'k', 'conv-new', 1, 1, 1, 1, 0, 1)",
                         (self.day + 60000,))
        self.con.commit()
        d = self.cs.summarize_cursor(self.con, self.lock, self.day - 86400000, self.day + 86400000, top_tasks=2)
        tasks = [t for p in d["projects"] for t in p["tasks"]]
        newest = max(tasks, key=lambda t: t["last"])
        self.assertEqual(newest["last"], self.day + 60000)

    def test_cursor_hour_bins(self):
        self.ing.scan()
        for i, ts in enumerate((self.day + 3600000 + 5, self.day + 3600000 + 9, self.day + 7200000 + 1)):
            self.con.execute("INSERT INTO cursor_events VALUES (?, ?, 'm', 'k', 'aaa', 1, 1, 1, 100, 0, 50)", ("h%d" % i, ts))
        self.con.commit()
        d = self.cs.summarize_cursor(self.con, self.lock, self.day, self.day + 86400000)
        self.assertEqual([h["requests"] for h in d["hours"]], [2, 1])
        self.assertEqual(d["hours"][0]["t"] % 3600000, 0)

    def test_models_count_requests_and_turns_apart(self):
        self.ing.scan()
        self.con.execute("INSERT INTO cursor_events VALUES ('e9', ?, 'claude-opus-5-5', 'k', 'aaa', 1, 1, 1, 100, 0, 50)",
                         (self.day + 5000,))
        self.con.commit()
        d = self.cs.summarize_cursor(self.con, self.lock, self.day - 86400000, self.day + 86400000)
        m = {x["model"]: x for x in d["models"]}
        self.assertEqual(m["claude-opus-5-5"]["requests"], 1)
        self.assertEqual(m["claude-opus-5-5"]["turns"], 0)

    def test_unchanged_rescan_is_not_new_data(self):
        self._token(time.time() + 3600)
        page = [{"totalUsageEventsCount": 3, "usageEventsDisplay": self._events(3, self.day)}]
        api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(page, []))
        self.assertEqual(api.scan(now_ms=self.day + 3600000), 3)
        api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener(page, []))
        v = self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()[0]
        self.assertEqual(api.scan(now_ms=self.day + 7200000), 0)
        self.assertEqual(self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()[0], v)

    def test_malformed_cli_meta_is_skipped(self):
        os.makedirs(os.path.join(self.chats, "h2", "agent2"))
        with open(os.path.join(self.chats, "h2", "agent2", "meta.json"), "w") as fh:
            json.dump({"createdAtMs": "soon"}, fh)
        os.makedirs(os.path.join(self.chats, "h3", "agent3"))
        with open(os.path.join(self.chats, "h3", "agent3", "meta.json"), "w") as fh:
            json.dump([1, 2], fh)
        self.assertEqual(self.ing.scan(), 4)
        self.assertIsNone(self.ing.progress["error"])

    def test_api_errors_are_reported_plainly(self):
        import urllib.error
        self._token(time.time() - 10)
        api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener([{}], []))
        self.assertEqual(api.scan(), 0)
        self.assertIn("expired", api.progress["error"])
        self.assertEqual(api.progress["error_code"], "session_expired")
        self._token(time.time() + 3600)
        err = urllib.error.HTTPError("https://cursor.com/x", 401, "no", {}, None)
        self.addCleanup(err.close)
        api = self.cs.CursorApi(self.con, self.lock, self.state, opener=self._opener([err], []))
        self.assertEqual(api.scan(), 0)
        self.assertIn("401", api.progress["error"])
        self.assertEqual((api.progress["error_code"], api.progress["error_detail"]), ("http_auth", "401"))
        self.assertNotIn("SECRETSIG", api.progress["error"])

    def test_signature_moves_with_the_database_wal_and_cli_chats(self):
        s0 = self.ing.signature()
        self.assertEqual(s0, self.ing.signature())
        with open(self.state + "-wal", "wb") as fh:
            fh.write(b"x" * 10)
        s1 = self.ing.signature()
        self.assertNotEqual(s0, s1)
        os.makedirs(os.path.join(self.chats, "h1", "agent2"))
        with open(os.path.join(self.chats, "h1", "agent2", "meta.json"), "w") as fh:
            json.dump({"createdAtMs": 1}, fh)
        self.assertNotEqual(s1, self.ing.signature())

    def test_missing_cursor_is_not_an_error(self):
        ing = self.cs.CursorIngestor(self.con, self.lock, latidos.project_of, os.path.join(self.tmp, "nope"),
                                     self.track, self.chats)
        self.assertEqual(ing.scan(), 0)
        self.assertFalse(ing.progress["available"])


class PhoneField(unittest.TestCase):
    """Audit of 95: on the phone the canvas reaches under the cards (L-176); what belongs to
    the cells' field must use the field's height, not the canvas's."""

    def setUp(self):
        with open(os.path.join(HERE, "web", "app.js")) as fh:
            self.js = fh.read()
        with open(os.path.join(HERE, "web", "app.css")) as fh:
            self.css = fh.read()

    def test_zoom_chip_sits_at_the_field_bottom(self):
        line = next(l for l in self.js.splitlines() if "zoomBtn.style.top" in l)
        self.assertIn("(H ||", line)
        self.assertNotIn("canvas.offsetTop + canvas.offsetHeight - 48", self.js)

    def test_input_below_the_field_is_the_pages(self):
        self.assertIn("function belowField(y) { return skyOn && y > H; }", self.js)
        self.assertIn("if (t && belowField(t.clientY - r.top)) return;", self.js)
        self.assertIn("if (belowField(sp.y) && !Object.keys(touches).length) return;", self.js)
        self.assertIn("if (belowField(sp.y) || at(wx(sp.x), wy(sp.y))) return;", self.js)

    def test_empty_water_has_no_surface_and_the_figure_is_read(self):
        self.assertIn('.q-water[data-empty] .q-wave { display: none; }', self.css)
        self.assertIn('tag2.setAttribute("role", "img");', self.js)
        self.assertIn('hintSr.className = "sr-only"', self.js)
        self.assertIn(".sr-only {", self.css)

    def test_cells_stay_in_the_field(self):
        """Audit of 96: zoomed cells were drawn under the cards and showed between them."""
        self.assertIn("if (clipped) { ctx.save(); ctx.beginPath(); ctx.rect(0, 0, W, H); ctx.clip(); }", self.js)
        self.assertIn("if (clipped) { ctx.restore(); zoomNow = cam.z; }", self.js)
        self.assertIn("Math.min(H - 24, Math.max(10, sy(b.y)", self.js)

    def test_hover_and_wheel_below_the_field_are_the_pages(self):
        self.assertIn("var hv = belowField(lastScreen.y) ? null : at(pointer.x, pointer.y);", self.js)
        self.assertIn("var b = belowField(lastScreen.y) ? null : at(p.x, p.y);", self.js)
        self.assertIn("if (belowField(screenXY(e).y)) return;", self.js)

    def test_last_activity_hint_once_and_blink_keeps_its_phase(self):
        self.assertIn("title.dataset.tip = hint;", self.js)
        self.assertNotIn("title.title = say(\"sheet.last.tip\"", self.js)
        # Audit of 97: only the blink keeps its phase; a live dot keeps its own beat.
        self.assertIn('if (!dot.classList.contains("now")) dot.style.animationDelay = -Math.round(performance.now() % 1800) + "ms";', self.js)
        self.assertIn(".tasks li.latest:first-child .t-title[data-tip]:hover::after { bottom: auto; top: calc(100% - 4px); }", self.css)

    def test_mouse_press_released_outside_leaves_no_touch(self):
        """Audit of 97: a stale mouse entry turned the next press into a two-pointer pinch."""
        self.assertIn('if (e.pointerType === "mouse" && !drag && !pan) delete touches[e.pointerId];', self.js)
        self.assertIn(".t-title[data-tip]:hover::after", self.css)


class Js(unittest.TestCase):
    def test_format_js(self):
        node = shutil.which("node") or next(iter(sorted(glob.glob(
            os.path.expanduser("~/.nvm/versions/node/*/bin/node")), reverse=True)), None)
        if not node:
            self.skipTest("node not installed")
        res = subprocess.run([node, "--test", os.path.join(HERE, "web", "format.test.js")],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_sound_js(self):
        """L-172: the chimes stay in the pentatonic, on the grid and quiet in a crowd."""
        node = shutil.which("node") or next(iter(sorted(glob.glob(
            os.path.expanduser("~/.nvm/versions/node/*/bin/node")), reverse=True)), None)
        if not node:
            self.skipTest("node not installed")
        res = subprocess.run([node, "--test", os.path.join(HERE, "web", "sound.test.js")],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


class Rename(unittest.TestCase):
    """The old name (Latido): its variables still work, its folders move once."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="latidos-rename-")
        self.saved = {k: os.environ.pop(k, None) for k in
                      ("LATIDOS_PORT", "LATIDO_PORT", "LATIDOS_CONFIG_DIR", "LATIDO_CONFIG_DIR",
                       "LATIDOS_DB", "LATIDO_DB")}

    def tearDown(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_legacy_variable_is_a_fallback(self):
        os.environ["LATIDO_PORT"] = "7600"
        self.assertEqual(latidos.env("PORT"), "7600")
        self.assertEqual(latidos.config()["port"], 7600)
        os.environ["LATIDOS_PORT"] = "7601"
        self.assertEqual(latidos.config()["port"], 7601)  # the new name wins

    def test_message_names_the_variable_set(self):
        os.environ["LATIDO_PORT"] = "x"
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(latidos._env_float("PORT", 7482), 7482)
        self.assertIn("LATIDO_PORT=", err.getvalue())

    def make_old(self):
        conf, state = os.path.join(self.tmp, "config"), os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(conf, "latido"))
        os.makedirs(os.path.join(state, "latido"))
        with open(os.path.join(conf, "latido", "config.json"), "w") as fh:
            fh.write('{"label": "local.demo"}')
        db = os.path.join(state, "latido", "latido.db")
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (x)")
        con.execute("INSERT INTO t VALUES (42)")
        con.commit()
        con.close()
        open(db + ".scan.lock", "w").close()
        return conf, state

    def test_migration_moves_config_and_database(self):
        conf, state = self.make_old()
        logs = []
        moved = latidos.migrate_legacy_dirs(conf, state, logs.append)
        self.assertTrue(moved)
        self.assertFalse(os.path.exists(os.path.join(conf, "latido")))
        self.assertFalse(os.path.exists(os.path.join(state, "latido")))
        self.assertTrue(os.path.isfile(os.path.join(conf, "latidos", "config.json")))
        db = os.path.join(state, "latidos", "latidos.db")
        self.assertFalse(glob.glob(os.path.join(state, "latidos", "latido.db*")))
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("SELECT x FROM t").fetchall(), [(42,)])
        con.close()
        self.assertTrue(all("migrated" in line for line in logs))
        self.assertEqual(latidos.migrate_legacy_dirs(conf, state, logs.append), [])  # once

    def test_migration_never_overwrites_the_new_folders(self):
        conf, state = self.make_old()
        os.makedirs(os.path.join(conf, "latidos"))
        os.makedirs(os.path.join(state, "latidos"))
        with open(os.path.join(state, "latidos", "latidos.db"), "w") as fh:
            fh.write("new")
        latidos.migrate_legacy_dirs(conf, state, lambda m: None)
        self.assertTrue(os.path.isfile(os.path.join(conf, "latido", "config.json")))
        self.assertTrue(os.path.isfile(os.path.join(state, "latido", "latido.db")))
        with open(os.path.join(state, "latidos", "latidos.db")) as fh:
            self.assertEqual(fh.read(), "new")

    def test_migration_finishes_a_half_done_run(self):
        conf, state = self.make_old()
        os.rename(os.path.join(state, "latido"), os.path.join(state, "latidos"))
        latidos.migrate_legacy_dirs(conf, state, lambda m: None)
        self.assertTrue(os.path.isfile(os.path.join(state, "latidos", "latidos.db")))
        self.assertFalse(glob.glob(os.path.join(state, "latidos", "latido.db*")))

    def test_migration_waits_while_the_old_database_is_in_use(self):
        conf, state = self.make_old()
        import fcntl
        with open(os.path.join(state, "latido", "latido.db.scan.lock"), "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            child = subprocess.run(
                [sys.executable, "-c", "import sys; sys.path.insert(0, %r); import latidos; "
                 "print(len(latidos.migrate_legacy_dirs(%r, %r, lambda m: None)))" % (HERE, conf, state)],
                capture_output=True, text=True, timeout=60,
                env={k: v for k, v in os.environ.items() if not k.startswith(("LATIDOS_", "LATIDO_"))})
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertTrue(os.path.isfile(os.path.join(state, "latido", "latido.db")))
        self.assertFalse(os.path.exists(os.path.join(state, "latidos")))

    def test_migration_skipped_when_a_variable_points_elsewhere(self):
        conf, state = self.make_old()
        os.environ["LATIDO_CONFIG_DIR"] = os.path.join(self.tmp, "elsewhere")
        os.environ["LATIDO_DB"] = os.path.join(self.tmp, "elsewhere.db")
        self.assertEqual(latidos.migrate_legacy_dirs(conf, state, lambda m: None), [])
        self.assertTrue(os.path.isdir(os.path.join(conf, "latido")))

    def test_legacy_agent_only_replaces_an_old_latido(self):
        agents = os.path.join(self.tmp, "agents")
        os.makedirs(agents)
        self.assertIsNone(latidos.legacy_agent("local.latidos", agents))
        self.assertIsNone(latidos.legacy_agent("local.other", agents))
        with open(os.path.join(agents, "local.latido.plist"), "wb") as fh:
            import plistlib
            plistlib.dump({"Label": "local.latido", "ProgramArguments": ["python3", "/x/something.py"]}, fh)
        self.assertIsNone(latidos.legacy_agent("local.latidos", agents))
        with open(os.path.join(agents, "local.latido.plist"), "wb") as fh:
            plistlib.dump({"Label": "local.latido", "ProgramArguments": ["python3", "/x/latido.py", "serve"]}, fh)
        self.assertEqual(latidos.legacy_agent("local.latidos", agents),
                         ("local.latido", os.path.join(agents, "local.latido.plist")))



class Classified(unittest.TestCase):
    """Smart classification (classified.json): segments, client grouping, moves, live calls."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects", "p")
        os.makedirs(self.root)
        os.makedirs(os.path.join(self.tmp, "cfg"))
        os.environ["LATIDOS_ROOTS"] = os.path.join(self.tmp, "projects")
        os.environ["LATIDOS_DB"] = os.path.join(self.tmp, "db", "l.db")
        os.environ["LATIDOS_FX_URL"] = "http://127.0.0.1:9/never"
        os.environ["LATIDOS_ALIASES"] = os.path.join(self.tmp, "cfg", "aliases.json")
        os.environ["LATIDOS_ASSIGN"] = os.path.join(self.tmp, "cfg", "assign.json")
        self.cls = os.environ["LATIDOS_CLASSIFIED"] = os.path.join(self.tmp, "cfg", "classified.json")
        self.saved = (dict(latidos.ALIASES), dict(latidos.ASSIGN))
        self.reset()
        self.app = latidos.App(latidos.config())
        self.f = os.path.join(self.root, "s.jsonl")
        self.t0 = int(time.time() // 3600 * 3600 * 1000) - 6 * 3600 * 1000

    def reset(self):
        latidos.ALIASES.clear(); latidos.ASSIGN = {}
        latidos.CLASSIFIED = {"sessions": {}, "folders": {}}
        for seen in (latidos._aliases_seen, latidos._assign_seen, latidos._classified_seen):
            seen.update(mtime=None, checked=0.0)
        latidos._project_cache.clear()

    def tearDown(self):
        for k in ("LATIDOS_ROOTS", "LATIDOS_DB", "LATIDOS_FX_URL", "LATIDOS_ALIASES", "LATIDOS_ASSIGN",
                  "LATIDOS_CLASSIFIED"):
            os.environ.pop(k, None)
        self.app.con.close()
        self.reset()
        latidos.ALIASES.update(self.saved[0]); latidos.ASSIGN = dict(self.saved[1])
        shutil.rmtree(self.tmp)

    def iso(self, ms):
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def calls(self, *specs):
        """(msg id, ms, session, cwd) -> ingested."""
        with open(self.f, "w") as fh:
            for mid, ms, sid, cwd in specs:
                fh.write(assistant(mid, self.iso(ms), U, session=sid, cwd=cwd) + "\n")
        self.app.ingestor.scan()

    def classify(self, data):
        with open(self.cls, "w") as fh:
            json.dump(data, fh)
        latidos._classified_seen.update(checked=0.0)

    def seg(self, frm, kind, project, client=None, task=None, ticket=None):
        return {"from": frm, "kind": kind, "client": client, "project": project, "task": task or project,
                "ticket": ticket}

    def summary(self):
        return self.app.summary(0, int(time.time() * 1000) + 60000, None)

    def test_a_session_is_split_by_its_segments_and_client_work_grouped(self):
        H1 = 3600 * 1000
        ws = H + "/code/agent-skeleton"
        self.calls(("a1", self.t0, "s1", ws), ("a2", self.t0 + 2 * H1, "s1", ws),
                   ("a3", self.t0 + 4 * H1, "s1", ws), ("b1", self.t0 + H1, "s2", ws))
        self.classify({"sessions": {
            "s1": {"segments": [self.seg(self.t0, "tool", "Dashboard", task="Build the panel"),
                                self.seg(self.t0 + 2 * H1, "client", "Portal", "Demo Client", "DEMO-7 Fix login", "DEMO-7"),
                                self.seg(self.t0 + 4 * H1, "client", "Billing", "Demo Client", "Invoices")]},
            "s2": {"segments": [self.seg(self.t0, "internal", "Booking", "Employer", "Book hours")]}}})
        # The share of the range a client project needs for its own bubble: 1 call of 4 is
        # 25%; with the default 3% each Demo Client project stands on its own.
        d = self.summary()
        names = {p["name"]: p for p in d["projects"]}
        self.assertEqual(sorted(names), ["Dashboard", "Demo Client · Billing", "Demo Client · Portal", "Employer"])
        self.assertEqual(d["totals"]["sessions"], 2)
        portal = names["Demo Client · Portal"]["tasks"][0]
        self.assertEqual((portal["key"], portal["ticket"], portal["title"]), ("DEMO-7", "DEMO-7", "DEMO-7 Fix login"))
        self.assertEqual(names["Dashboard"]["tasks"][0]["key"], "s:s1.0")
        self.assertEqual(names["Employer"]["tasks"][0]["key"], "s:s2.0")
        # A bigger share folds both client projects into the client's bubble.
        self.app.cfg["fold_share"] = 0.5
        latidos._notify_names()
        d = self.summary()
        names = {p["name"]: p for p in d["projects"]}
        self.assertEqual(sorted(names), ["Dashboard", "Demo Client", "Employer"])
        self.assertEqual(sorted(t["sub"] for t in names["Demo Client"]["tasks"]), ["Billing", "Portal"])
        self.assertEqual(d["fold"]["Demo Client · Portal"], "Demo Client")
        self.assertEqual(names["Demo Client"]["sessions"], 1)

    def test_live_calls_follow_the_segment_and_a_hand_move_wins(self):
        H1 = 3600 * 1000
        ws = H + "/code/agent-skeleton"
        self.calls(("a1", self.t0, "s1", ws), ("a2", self.t0 + 2 * H1, "s1", ws))
        self.classify({"sessions": {"s1": {"segments": [
            self.seg(self.t0, "tool", "Dashboard"),
            self.seg(self.t0 + 2 * H1, "client", "Portal", "Demo Client")]}}})
        ing = self.app.ingestor
        self.assertEqual(ing.live_names("s1", ws, None, self.t0 + 10), ("Dashboard", None))
        self.assertEqual(ing.live_names("s1", ws, None, self.t0 + 3 * H1), ("Demo Client · Portal", "Demo Client"))
        # Moving one segment by hand leaves the other alone.
        latidos.assign_task("s:s1.1", "Elsewhere")
        self.assertEqual(ing.live_project("s1", ws, None, self.t0 + 3 * H1), "Elsewhere")
        self.assertEqual(ing.live_project("s1", ws, None, self.t0 + 10), "Dashboard")
        names = {p["name"]: p for p in self.summary()["projects"]}
        self.assertEqual(names["Elsewhere"]["tasks"][0]["assign_key"], "s:s1.1")
        self.assertIn("Dashboard", names)

    def test_learned_folders_place_new_sessions_and_bad_entries_are_ignored(self):
        repo = H + "/code/repo-x"
        self.calls(("a1", self.t0, "s9", repo), ("b1", self.t0, "s8", H + "/code/agent-skeleton"))
        self.classify({"folders": {"repo-x": {"kind": "client", "client": "Demo Client", "project": "Portal"},
                                   "bad": {"kind": "nope", "project": "x"}},
                       "sessions": {"s8": {"segments": [{"from": "soon", "kind": "tool", "project": "x"},
                                                        {"from": self.t0, "kind": "tool", "project": ""}]},
                                    "s7": "not an object"}})
        self.assertEqual(latidos._classified()["folders"].keys(), {"repo-x"})
        self.assertEqual(latidos._classified()["sessions"], {})
        names = sorted(p["name"] for p in self.summary()["projects"])
        # A client with one project is shown by the client's name.
        self.assertEqual(names, ["Demo Client", "agent-skeleton"])
        self.assertEqual(self.app.ingestor.live_names("s9", repo), ("Demo Client · Portal", "Demo Client"))

    def test_a_broken_file_is_ignored(self):
        with open(self.cls, "w") as fh:
            fh.write("{oops")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(latidos._classified(), {"sessions": {}, "folders": {}})

    def test_fold_names(self):
        f = latidos.fold_names([("A · x", "A", 50), ("A · y", "A", 2), ("A", "A", 1), ("tool", None, 47)], 100, 0.1)
        self.assertEqual(f, {"A · x": "A · x", "A · y": "A", "A": "A"})
        self.assertEqual(latidos.fold_names([("B · z", "B", 90)], 100, 0.1), {"B · z": "B"})


class Classifier(unittest.TestCase):
    """classify.py: dossiers from transcripts, one model call (a fake CLI), the state file."""
    def setUp(self):
        import classify
        self.mod = classify
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects", "p")
        os.makedirs(self.root)
        os.environ["LATIDOS_ROOTS"] = os.path.join(self.tmp, "projects")
        os.environ["LATIDOS_DB"] = os.path.join(self.tmp, "db", "l.db")
        os.environ["LATIDOS_FX_URL"] = "http://127.0.0.1:9/never"
        os.environ["LATIDOS_CLASSIFIED"] = os.path.join(self.tmp, "cfg", "classified.json")
        os.environ["LATIDOS_TAXONOMY"] = os.path.join(self.tmp, "cfg", "taxonomy.json")
        os.environ["LATIDOS_CLASSIFY_WORKDIR"] = os.path.join(self.tmp, "work")
        self.app = latidos.App(latidos.config())

    def tearDown(self):
        for k in ("LATIDOS_ROOTS", "LATIDOS_DB", "LATIDOS_FX_URL", "LATIDOS_CLASSIFIED", "LATIDOS_TAXONOMY",
                  "LATIDOS_CLASSIFY_WORKDIR"):
            os.environ.pop(k, None)
        self.app.con.close()
        # Live calls of the ingest read classified.json: no other test may see it.
        latidos.CLASSIFIED = {"sessions": {}, "folders": {}}
        latidos._classified_seen.update(mtime=None, checked=0.0)
        shutil.rmtree(self.tmp)

    def test_prompt_text_keeps_only_the_users_words(self):
        pt = self.mod.prompt_text
        self.assertEqual(pt({"type": "user", "message": {"content": "Fix  the\nlogin"}}), "Fix the login")
        self.assertIsNone(pt({"type": "user", "message": {"content": "<task-notification>x"}}))
        self.assertIsNone(pt({"type": "user", "isSidechain": True, "message": {"content": "hi"}}))
        self.assertIsNone(pt({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}))
        self.assertEqual(pt({"type": "user", "message": {"content":
                             "<command-name>/review</command-name><command-args>42</command-args>"}}), "/review 42")

    def test_compact_merges_follow_ups_and_caps_turns(self):
        turns = [{"ts": i, "text": t, "usd": u, "hints": {}, "cwd": {}, "branch": {}}
                 for i, (t, u) in enumerate([("Build the panel please", 1), ("sí", 2), ("Now the login page", 3),
                                             ("And the invoices list", 0.1), ("And the export to csv", 0.1)])]
        out = self.mod.compact(turns, max_turns=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["text"], "Build the panel please / sí")
        self.assertEqual(out[0]["usd"], 3)
        self.assertEqual(out[1]["ts"], 2)

    def test_run_with_a_fake_model_writes_segments(self):
        now = time.time() - 3600
        iso = lambda s: dt.datetime.fromtimestamp(s, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cwd = H + "/code/agent-skeleton"
        user = lambda ts, text: json.dumps({"type": "user", "timestamp": iso(ts), "sessionId": "s1", "cwd": cwd,
                                            "message": {"role": "user", "content": text}})
        with open(os.path.join(self.root, "s1.jsonl"), "w") as fh:
            fh.write(user(now, "Build the dashboard panel") + "\n")
            fh.write(assistant("m1", iso(now + 1), U, cwd=cwd) + "\n")
            fh.write(user(now + 60, "Now fix DEMO-7 on the portal") + "\n")
            fh.write(assistant("m2", iso(now + 61), U, cwd=cwd) + "\n")
        self.app.ingestor.scan()
        fake = os.path.join(self.tmp, "fake-claude")
        out = {"type": "result", "is_error": False, "total_cost_usd": 0.01, "structured_output": {
            "sessions": [{"id": "s1", "segments": [
                {"from_turn": 0, "kind": "tool", "client": None, "project": "Dashboard", "task": "Panel", "ticket": None},
                {"from_turn": 1, "kind": "client", "client": "Demo Client", "project": "Portal",
                 "task": "DEMO-7 Fix login", "ticket": "DEMO-7"}]}],
            "new_projects": [{"name": "Portal", "kind": "client", "client": "Demo Client", "about": "x"}]}}
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\ncat > %s/prompt.txt\necho '%s'\n" % (self.tmp, json.dumps(out)))
        os.chmod(fake, 0o755)
        args = types.SimpleNamespace(session=None, limit=0, dry_run=False, show=0, model="m", claude=fake, jobs=1)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(self.mod.run(args), 0)
        with open(os.path.join(self.tmp, "prompt.txt")) as fh:
            prompt = fh.read()
        self.assertIn("[0]", prompt)
        self.assertIn("Now fix DEMO-7 on the portal", prompt)
        state = self.mod.load_state()
        segs = state["sessions"]["s1"]["segments"]
        self.assertEqual([(s["project"], s["client"], s["ticket"]) for s in segs],
                         [("Dashboard", None, None), ("Portal", "Demo Client", "DEMO-7")])
        self.assertEqual(segs[1]["from"], int(now + 60) * 1000)
        self.assertEqual(state["projects"]["Portal"]["client"], "Demo Client")
        # Nothing new: the next run sends nothing.
        os.unlink(os.path.join(self.tmp, "prompt.txt"))
        with redirect_stderr(io.StringIO()):
            self.mod.run(args)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "prompt.txt")))



class ClassifiedAudit87(Classified):
    """Regressions found by the audit of release 87 (Latidos side)."""
    def test_a_segment_change_inside_an_hour_splits_the_hour(self):
        M = 60 * 1000
        ws = H + "/code/agent-skeleton"
        h = self.t0
        self.calls(("a1", h + 5 * M, "s1", ws), ("a2", h + 10 * M, "s1", ws),
                   ("a3", h + 40 * M, "s1", ws), ("a4", h + 50 * M, "s1", ws))
        self.classify({"sessions": {"s1": {"segments": [
            self.seg(h, "tool", "Dashboard"), self.seg(h + 30 * M, "client", "Portal", "Demo Client")]}}})
        d = self.summary()
        names = {p["name"]: p for p in d["projects"]}
        self.assertEqual(sorted(names), ["Dashboard", "Demo Client"])
        self.assertAlmostEqual(names["Dashboard"]["usd"], names["Demo Client"]["usd"], places=6)
        self.assertAlmostEqual(sum(p["usd"] for p in d["projects"]), d["totals"]["usd"], places=6)
        self.assertEqual(d["totals"]["calls"], 4)
        self.assertEqual(len([m for m in d["moments"] if m["kind"] == "session"]), 1)

    def test_non_finite_numbers_in_the_file_are_ignored(self):
        with open(self.cls, "w") as fh:
            fh.write('{"sessions": {"s1": {"segments": [{"from": NaN, "kind": "tool", "project": "x"}, '
                     '{"from": Infinity, "kind": "tool", "project": "y"}, {"from": true, "kind": "tool", "project": "z"}]}}}')
        self.assertEqual(latidos._classified()["sessions"], {})
        self.calls(("a1", self.t0, "s1", H + "/code/agent-skeleton"))
        self.assertEqual([p["name"] for p in self.summary()["projects"]], ["agent-skeleton"])

    def test_a_hand_move_covers_only_its_segment(self):
        H1 = 3600 * 1000
        ws = H + "/code/agent-skeleton"
        self.calls(("a1", self.t0, "s1", ws), ("a2", self.t0 + 2 * H1, "s1", ws))
        self.classify({"sessions": {"s1": {"segments": [self.seg(self.t0, "tool", "Dashboard")]}}})
        key = self.summary()["projects"][0]["tasks"][0]["key"]
        self.assertEqual(key, "s:s1.0")
        latidos.assign_task(key, "Moved")
        # The classifier later finds a second segment: it is not covered by the move.
        self.classify({"sessions": {"s1": {"segments": [self.seg(self.t0, "tool", "Dashboard"),
                                                        self.seg(self.t0 + 2 * H1, "client", "Portal", "Demo Client")]}}})
        self.assertEqual(sorted(p["name"] for p in self.summary()["projects"]), ["Demo Client", "Moved"])


class ClassifiedAudit88(Classified):
    """The audit of 88: the split of a straddling hour is read under the summary's lock."""
    def test_a_call_ingested_during_a_summary_is_never_counted_by_half(self):
        M = 60 * 1000
        ws = H + "/code/agent-skeleton"
        h = self.t0
        self.calls(("a1", h + 5 * M, "s1", ws), ("a2", h + 10 * M, "s1", ws),
                   ("a3", h + 40 * M, "s1", ws), ("a4", h + 50 * M, "s1", ws))
        self.classify({"sessions": {"s1": {"segments": [
            self.seg(h, "tool", "Dashboard"), self.seg(h + 30 * M, "client", "Portal", "Demo Client")]}}})
        real = latidos.split_rows
        other = []

        def ingest_meanwhile(*a, **k):
            # Another thread ingests one more call of the same hour while the summary runs.
            def write():
                with open(self.f, "a") as fh:
                    fh.write(assistant("a5", self.iso(h + 55 * M), U, session="s1", cwd=ws) + "\n")
                self.app.ingestor.scan()
            th = threading.Thread(target=write)
            th.start()
            th.join(0.5)
            other.append(th)
            return real(*a, **k)
        with unittest.mock.patch.object(latidos, "split_rows", ingest_meanwhile):
            d = self.summary()
        other[0].join(10)
        self.assertEqual(d["totals"]["calls"], sum(v["calls"] for v in d["by_origin"].values()))
        self.assertEqual(d["totals"]["calls"], 4)
        latidos._notify_names()
        self.assertEqual(self.summary()["totals"]["calls"], 5)

    def test_a_live_call_during_a_summary_counts_once_with_its_sequence(self):
        """The real rule: a call is in the summary with a sequence at or below seq_at, or
        only live above it; never both."""
        M = 60 * 1000
        ws = H + "/code/agent-skeleton"
        now = int(time.time() * 1000)
        self.calls(("a1", now - 40 * M, "s1", ws), ("a2", now - 35 * M, "s1", ws),
                   ("a3", now - 10 * M, "s1", ws), ("a4", now - 8 * M, "s1", ws))
        self.classify({"sessions": {"s1": {"segments": [
            self.seg(now - 60 * M, "tool", "Dashboard"), self.seg(now - 20 * M, "client", "Portal", "Demo Client")]}}})
        real = latidos.split_rows
        other = []

        def ingest_meanwhile(*a, **k):
            def write():
                with open(self.f, "a") as fh:
                    fh.write(assistant("a5", self.iso(now - M), U, session="s1", cwd=ws) + "\n")
                self.app.ingestor.scan()
            th = threading.Thread(target=write)
            th.start()
            th.join(0.5)
            other.append(th)
            return real(*a, **k)
        with unittest.mock.patch.object(latidos, "split_rows", ingest_meanwhile):
            d = self.app.summary(0, now + 60000, None)
        other[0].join(10)
        live_after = [e for q, e in self.app.ingestor.recent if q > d["seq_at"]]
        self.assertEqual(d["totals"]["calls"] + len(live_after), 5)

class ClassifierAudit87(Classifier):
    """Regressions found by the audit of release 87 (classify.py)."""
    def iso(self, sec):
        return dt.datetime.fromtimestamp(sec, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def user(self, sec, text, sid="s1", cwd=None):
        return json.dumps({"type": "user", "timestamp": self.iso(sec), "sessionId": sid,
                           "cwd": cwd or H + "/code/agent-skeleton", "message": {"role": "user", "content": text}})

    def transcript(self, lines, sid="s1"):
        with open(os.path.join(self.root, sid + ".jsonl"), "a") as fh:
            for ln in lines:
                fh.write(ln + "\n")
        self.app.ingestor.scan()

    def fake(self):
        """A fake CLI: one segment per session from its first index; saves each prompt."""
        fake = os.path.join(self.tmp, "fake-claude")
        with open(fake, "w") as fh:
            fh.write("""#!/usr/bin/env python3
import json, os, re, sys
text = sys.stdin.read()
d = os.path.join(%r, "prompts"); os.makedirs(d, exist_ok=True)
open(os.path.join(d, str(len(os.listdir(d)))), "w").write(text)
sess = []
for m in re.finditer(r"## session (\\S+).*?\\n\\[(\\d+)\\]", text, re.S):
    sess.append({"id": m.group(1), "segments": [{"from_turn": int(m.group(2)), "kind": "tool", "client": None,
                 "project": "P", "task": "T" + m.group(2), "ticket": None}]})
print(json.dumps({"is_error": False, "total_cost_usd": 0, "structured_output": {"sessions": sess, "new_projects": []}}))
""" % self.tmp)
        os.chmod(fake, 0o755)
        return types.SimpleNamespace(session=None, limit=0, dry_run=False, show=0, model="m", claude=fake, jobs=1)

    def prompts(self):
        d = os.path.join(self.tmp, "prompts")
        out = []
        for n in sorted(os.listdir(d), key=int) if os.path.isdir(d) else []:
            with open(os.path.join(d, n)) as fh:
                out.append(fh.read())
        return out

    def run_quiet(self, args, now=None):
        with redirect_stderr(io.StringIO()):
            if now is None:
                return self.mod.run(args)
            with unittest.mock.patch.object(self.mod.time, "time", return_value=now):
                return self.mod.run(args)

    def test_a_turn_inside_the_settle_window_is_classified_later(self):
        now = time.time()
        self.transcript([self.user(now - 600, "Build the dashboard panel"), assistant("m1", self.iso(now - 590), U),
                         self.user(now - 60, "Now the portal login page"), assistant("m2", self.iso(now - 50), U)])
        args = self.fake()
        self.run_quiet(args, now)
        self.assertEqual(self.mod.load_state()["sessions"]["s1"]["turns"], 1)
        # No new call, but the window has passed: the second turn goes now.
        self.run_quiet(args, now + 600)
        rec = self.mod.load_state()["sessions"]["s1"]
        self.assertEqual(rec["turns"], 2)
        self.assertEqual([s["task"] for s in rec["segments"]], ["T0", "T1"])

    def test_merged_follow_ups_are_not_sent_twice(self):
        now = time.time()
        self.transcript([self.user(now - 900, "Build the dashboard panel"), assistant("m1", self.iso(now - 890), U),
                         self.user(now - 800, "sí"), assistant("m2", self.iso(now - 790), U)])
        args = self.fake()
        self.run_quiet(args, now)
        self.transcript([self.user(now - 400, "Now the portal login page"), assistant("m3", self.iso(now - 390), U)])
        self.run_quiet(args, now)
        sent = self.prompts()
        self.assertEqual(len(sent), 2)
        self.assertNotIn("| sí", sent[1])
        self.assertIn("[1]", sent[1])
        self.assertIn("Now the portal login page", sent[1])

    def test_its_own_sessions_are_never_classified(self):
        now = time.time()
        wd = os.environ["LATIDOS_CLASSIFY_WORKDIR"]
        self.transcript([self.user(now - 900, "[latidos-classifier] classify", sid="own", cwd=wd),
                         assistant("o1", self.iso(now - 890), U, session="own", cwd=wd)], sid="own")
        self.run_quiet(self.fake(), now)
        self.assertNotIn("own", self.mod.load_state()["sessions"])
        self.assertEqual(self.prompts(), [])

    def test_odd_transcript_lines_never_stop_a_run(self):
        now = time.time()
        self.transcript([json.dumps({"type": "user", "timestamp": 123, "sessionId": "s1", "message": {"content": "x"}}),
                         json.dumps({"type": "user", "timestamp": self.iso(now - 900), "sessionId": "s1", "cwd": 5,
                                     "message": {"content": [{"type": "text", "text": None}]}}),
                         self.user(now - 800, "Build the dashboard panel"), assistant("m1", self.iso(now - 790), U)])
        self.assertEqual(self.run_quiet(self.fake(), now), 0)
        self.assertIn("s1", self.mod.load_state()["sessions"])

    def test_consolidate_waits_for_a_run(self):
        held = self.mod.take_lock()
        try:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(self.mod.consolidate(types.SimpleNamespace(model="m", claude="/bin/false")), 1)
        finally:
            held.close()


class AgentPricing(unittest.TestCase):
    """Models that are not Claude get their own list price (L-182)."""

    def test_openai_and_gemini_list_prices(self):
        import agents as ag
        self.assertAlmostEqual(ag.other_cost("gpt-5-codex", 1e6, 1e6, 1e6), 1.25 + 10 + 0.125)
        self.assertAlmostEqual(ag.other_cost("gpt-5-mini", 1e6, 0, 0), 0.25)       # never priced as gpt-5
        self.assertAlmostEqual(ag.other_cost("gpt-4.1-2025-04-14", 1e6, 0, 0), 2.0)
        self.assertAlmostEqual(ag.other_cost("o4-mini", 0, 1e6, 0), 4.4)
        self.assertAlmostEqual(ag.other_cost("gemini-2.5-flash", 1e6, 1e6, 0), 0.30 + 2.50)
        self.assertAlmostEqual(ag.other_cost("gemini-3-pro-preview", 100000, 0, 0), 0.2)

    def test_gemini_pro_long_context_tier(self):
        import agents as ag
        self.assertAlmostEqual(ag.other_cost("gemini-2.5-pro", 100000, 0, 0), 0.125)
        self.assertAlmostEqual(ag.other_cost("gemini-2.5-pro", 300000, 0, 0), 0.75)  # 2.50 a million above 200k

    def test_provider_prefixes_and_claude_ids(self):
        import agents as ag
        self.assertEqual(ag.other_price("openai/gpt-4o-mini")[0], "gpt-4o-mini")
        self.assertEqual(ag.other_price("copilot/gpt-4.1")[0], "gpt-4.1")
        self.assertEqual(ag.other_price("models/gemini-2.0-flash")[0], "gemini-2.0-flash")
        self.assertEqual(ag.norm_model("openrouter/anthropic/claude-sonnet-4.5"), "claude-sonnet-4-5")
        self.assertEqual(ag.norm_model("us.anthropic.claude-3-7-sonnet-20250219-v1:0"), "claude-3-7-sonnet-20250219")
        claude = lambda m, i, o, r, w: latidos.call_cost(m, i, o, r, w, 0)
        self.assertAlmostEqual(ag.api_cost("anthropic/claude-sonnet-4.5", 1e6, 0, 0, 0, claude), 3.0)
        self.assertAlmostEqual(ag.api_cost("claude-3-5-haiku-20241022", 1e6, 0, 0, 0, claude), 0.8)

    def test_unknown_model_is_unpriced_never_an_error(self):
        import agents as ag
        self.assertIsNone(ag.other_cost("my-local-llama", 1e6, 1e6, 0))
        self.assertIsNone(ag.api_cost("my-local-llama", 1e6, 1e6, 0, 0, lambda *a: 99))
        self.assertIsNone(ag.api_cost(None, 1, 1))
        self.assertEqual(ag.norm_model(None), "")

    def test_claude_code_calls_of_other_models(self):
        # A Claude Code session through a proxy to GPT used to be priced as Opus.
        self.assertAlmostEqual(latidos.call_cost("gpt-5", 1e6, 0, 0, 0, 0), 1.25)
        self.assertEqual(latidos.call_cost("some-local-model", 1e6, 1e6, 0, 0, 0), 0.0)
        # Claude ids keep their table, Bedrock ids reduced to their Claude name.
        self.assertAlmostEqual(latidos.call_cost("claude-opus-5-5", 1e6, 0, 0, 0, 0), 4.0)
        self.assertAlmostEqual(latidos.call_cost("anthropic.claude-sonnet-4-20250514-v1:0", 1e6, 0, 0, 0, 0), 3.0)
        self.assertAlmostEqual(latidos.call_cost("<synthetic>", 0, 0, 0, 0, 0), 0.0)


class AgentFixtures:
    """Synthetic agent homes in each agent's real layout (L-182)."""

    T = 1794470400000  # 2026-11-12

    def setUp(self):
        import agents as ag
        self.ag = ag
        self.home = tempfile.mkdtemp(prefix="agents-home-")
        self.code = os.path.join(self.home, "code")
        self.con = latidos.connect(os.path.join(self.home, "l.db"))
        self.lock = threading.RLock()

    def tearDown(self):
        self.con.close()
        shutil.rmtree(self.home)

    def iso(self, ms):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ms / 1000)) + ".%03dZ" % (ms % 1000)

    def write(self, rel, data, mode="w"):
        p = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode) as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))
        return p

    def hub(self, *ids, roots=None):
        h = self.ag.AgentHub(self.con, self.lock, home=self.home, roots=roots)
        if ids:
            h.readers = [r for r in h.readers if r.id in ids]
            h.progress = {r.id: h.progress[r.id] for r in h.readers}
        return h

    def calls(self, agent):
        return [dict(zip(("id", "ts", "session", "model", "inp", "out", "cread", "cwrite", "usd"), r)) for r in self.con.execute(
            "SELECT id, ts, session, model, inp, out, cread, cwrite, usd FROM agent_calls WHERE agent = ? ORDER BY ts, id",
            (agent,))]

    def session(self, agent, sid):
        r = self.con.execute("SELECT title, cwd, created, updated FROM agent_sessions WHERE agent = ? AND id = ?",
                             (agent, sid)).fetchone()
        return dict(zip(("title", "cwd", "created", "updated"), r)) if r else None

    def summary(self, h, t0=0, t1=None):
        return self.ag.summarize_agents(self.con, self.lock, t0, t1 or self.T + 30 * 86400000, "day",
                                        latidos.bin_start, latidos.project_of,
                                        claude=lambda m, i, o, r, w: latidos.call_cost(m, i, o, r, w, 0),
                                        readers=h.readers, progress=h.progress)



class AgentReaders(AgentFixtures, unittest.TestCase):
    """One reader per agent against synthetic files in each agent's real layout (L-182)."""

    # -- Codex ------------------------------------------------------------------------
    def codex_lines(self, sid, t, cwd):
        tc = lambda total, inp, cached, out, dt_ms: json.dumps({"timestamp": self.iso(t + dt_ms), "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"total_tokens": total},
                                            "last_token_usage": {"input_tokens": inp, "cached_input_tokens": cached,
                                                                 "output_tokens": out, "reasoning_output_tokens": 5,
                                                                 "total_tokens": inp + out}}}})
        return [
            json.dumps({"timestamp": self.iso(t), "type": "session_meta", "payload": {"id": sid, "timestamp": self.iso(t), "cwd": cwd}}),
            json.dumps({"timestamp": self.iso(t), "type": "turn_context", "payload": {"cwd": cwd, "model": "gpt-5-codex"}}),
            json.dumps({"timestamp": self.iso(t), "type": "response_item", "payload": {"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": "<environment_context>x</environment_context>"}]}}),
            json.dumps({"timestamp": self.iso(t + 1), "type": "event_msg", "payload": {"type": "user_message", "message": "Fix the login page"}}),
            json.dumps({"timestamp": self.iso(t + 2), "type": "event_msg", "payload": {"type": "token_count", "info": None}}),
            tc(1100, 1000, 800, 100, 1000),
            tc(1100, 1000, 800, 100, 1100),   # the same count emitted twice
            tc(3300, 2000, 1500, 200, 2000),
        ]

    def test_codex_rollout_incremental(self):
        sid = "0199a0b1-1111-7222-8333-944455556666"
        cwd = os.path.join(latidos.CODE_DIR, "repo-x")
        rel = os.path.join(".codex", "sessions", "2026", "11", "12", "rollout-2026-11-12T10-00-00-%s.jsonl" % sid)
        lines = self.codex_lines(sid, self.T, cwd)
        self.write(rel, "\n".join(lines) + "\n" + lines[-1][:20])  # a half-written last line waits
        h = self.hub("codex")
        self.assertEqual(h.scan(), 2)
        c = self.calls("codex")
        self.assertEqual([(x["inp"], x["cread"], x["out"], x["model"]) for x in c],
                         [(200, 800, 100, "gpt-5-codex"), (500, 1500, 200, "gpt-5-codex")])
        s = self.session("codex", sid)
        self.assertEqual((s["title"], s["cwd"]), ("Fix the login page", cwd))
        self.assertEqual(h.scan(), 0)  # unchanged file: not re-read
        # The rest of the half line plus a new count: read from the offset, no double count.
        p = os.path.join(self.home, rel)
        with open(p, "rb") as fh:
            data = fh.read()
        with open(p, "wb") as fh:
            fh.write(data[:data.rfind(b"\n") + 1])
            fh.write(json.dumps({"timestamp": self.iso(self.T + 5000), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {"total_tokens": 4000},
                                                "last_token_usage": {"input_tokens": 600, "cached_input_tokens": 0,
                                                                     "output_tokens": 100}}}}).encode() + b"\n")
        self.assertEqual(h.scan(), 1)
        self.assertEqual(len(self.calls("codex")), 3)
        a = self.summary(h)[0]
        self.assertEqual(a["id"], "codex")
        self.assertEqual(a["projects"][0]["name"], "repo-x")
        # gpt-5-codex: 1.25 in, 0.125 cached, 10 out (per million)
        want = (200 * 1.25 + 800 * 0.125 + 100 * 10 + 500 * 1.25 + 1500 * 0.125 + 200 * 10 + 600 * 1.25 + 100 * 10) / 1e6
        self.assertAlmostEqual(a["totals"]["usd"], round(want, 4), places=4)
        self.assertEqual(a["totals"]["requests"], 3)

    # -- Gemini CLI ---------------------------------------------------------------------
    def test_gemini_session_with_hashed_project(self):
        import hashlib
        cwd = os.path.join(latidos.CODE_DIR, "repo-g")
        hsh = hashlib.sha256(cwd.encode()).hexdigest()
        self.con.execute("INSERT INTO calls (msg_id, ts, cwd) VALUES ('m', 1, ?)", (cwd,))  # a folder Claude Code knows
        self.write(os.path.join(".gemini", "tmp", hsh, "chats", "session-2026-11-12T10-00-abc.json"), {
            "sessionId": "g1", "projectHash": hsh, "startTime": self.iso(self.T), "lastUpdated": self.iso(self.T + 9000),
            "messages": [
                {"id": "u1", "timestamp": self.iso(self.T), "type": "user", "content": "Write the release notes"},
                {"id": "a1", "timestamp": self.iso(self.T + 1000), "type": "gemini", "content": "ok", "model": "gemini-2.5-pro",
                 "tokens": {"input": 12000, "output": 300, "cached": 10000, "thoughts": 200, "tool": 0, "total": 12500}},
                {"id": "a2", "timestamp": self.iso(self.T + 2000), "type": "gemini", "model": "gemini-2.5-flash"},  # no tokens
                "junk",
            ]})
        # A newer layout: a short folder name with the path in .project_root.
        self.write(os.path.join(".gemini", "tmp", "repo-h", ".project_root"), os.path.join(latidos.CODE_DIR, "repo-h"))
        self.write(os.path.join(".gemini", "tmp", "repo-h", "chats", "session-x.json"), {
            "sessionId": "g2", "messages": [{"id": "a1", "timestamp": self.iso(self.T + 5000), "type": "gemini",
                                             "model": "gemini-3-flash-preview", "tokens": {"input": 100, "output": 10}}]})
        h = self.hub("gemini")
        self.assertEqual(h.scan(), 2)
        c = self.calls("gemini")
        self.assertEqual((c[0]["inp"], c[0]["cread"], c[0]["out"]), (2000, 10000, 500))
        self.assertEqual(self.session("gemini", "g1")["cwd"], cwd)
        self.assertEqual(self.session("gemini", "g1")["title"], "Write the release notes")
        self.assertEqual(self.session("gemini", "g2")["cwd"], os.path.join(latidos.CODE_DIR, "repo-h"))
        a = self.summary(h)[0]
        self.assertEqual(sorted(p["name"] for p in a["projects"]), ["repo-g", "repo-h"])
        self.assertGreater(a["totals"]["usd"], 0)
        self.assertEqual(a["totals"]["unpriced"], 0)

    # -- Antigravity and Windsurf: activity only ---------------------------------------
    def test_antigravity_and_windsurf_count_active_days(self):
        conv = "5f0c7c6e-aaaa-bbbb-cccc-000000000001"
        p = self.write(os.path.join(".gemini", "antigravity", "conversations", conv + ".pb"), b"\x08\x01".decode())
        self.write(os.path.join(".gemini", "antigravity", "brain", conv, "task.md"), "# Migrate the build to Vite\n- [ ] step")
        two_days_later = (self.T + 2 * 86400000) / 1000
        os.utime(p, (two_days_later, two_days_later))
        w = self.write(os.path.join(".codeium", "windsurf", "cascade", "c1.pb"), "x")
        os.utime(w, (self.T / 1000, self.T / 1000))
        h = self.hub("antigravity", "windsurf")
        self.assertGreaterEqual(h.scan(), 2)
        ag = self.calls("antigravity")
        self.assertTrue(all(x["inp"] == 0 and x["out"] == 0 and x["usd"] is None for x in ag))
        self.assertEqual(self.session("antigravity", conv)["title"], "Migrate the build to Vite")
        out = {a["id"]: a for a in self.summary(h)}
        self.assertEqual(out["antigravity"]["count_unit"], "days")
        self.assertFalse(out["antigravity"]["has_tokens"])
        self.assertEqual(out["antigravity"]["totals"]["usd"], 0)
        self.assertEqual(out["windsurf"]["totals"]["requests"], 1)
        self.assertEqual(out["windsurf"]["projects"][0]["name_code"], "no_folder")

    # -- Copilot ------------------------------------------------------------------------
    def test_copilot_sessions_json_and_journal(self):
        ws = os.path.join("Library", "Application Support", "Code", "User", "workspaceStorage", "abc123")
        folder = os.path.join(latidos.CODE_DIR, "repo-c")
        self.write(os.path.join(ws, "workspace.json"), {"folder": "file://" + folder.replace(" ", "%20")})
        self.write(os.path.join(ws, "chatSessions", "s1.json"), {
            "version": 3, "sessionId": "s1", "creationDate": self.T, "lastMessageDate": self.T + 9000,
            "requests": [
                {"requestId": "r1", "timestamp": self.T + 1000, "modelId": "copilot/gpt-4.1", "message": {"text": "Explain this regex"}},
                {"requestId": "r2", "timestamp": self.T + 2000, "modelId": "copilot/gpt-4.1", "message": {"text": "And this one"},
                 "result": {"usage": {"promptTokens": 1000, "completionTokens": 100}}},
            ]})
        journal = [{"kind": 0, "v": {"sessionId": "s2", "creationDate": self.T, "requests": []}},
                   {"kind": 2, "k": ["requests"], "v": [{"requestId": "q1", "timestamp": self.T + 3000, "message": {"text": "Hi"}}]},
                   {"kind": 1, "k": ["customTitle"], "v": "Renamed chat"},
                   {"kind": 1, "k": ["requests", 9, "x"], "v": 1}]  # a path that does not exist: ignored
        self.write(os.path.join(ws, "chatSessions", "s2.jsonl"), "\n".join(json.dumps(j) for j in journal) + "\nnot json\n")
        h = self.hub("copilot")
        self.assertEqual(h.scan(), 3)
        c = self.calls("copilot")
        self.assertEqual([(x["model"], x["inp"]) for x in c], [("copilot/gpt-4.1", 0), ("copilot/gpt-4.1", 1000), (None, 0)])
        self.assertEqual(self.session("copilot", "s1")["cwd"], folder)
        self.assertEqual(self.session("copilot", "s2")["title"], "Renamed chat")
        a = self.summary(h)[0]
        self.assertEqual(a["count_unit"], "requests")
        self.assertEqual(a["totals"]["requests"], 3)
        self.assertAlmostEqual(a["totals"]["usd"], (1000 * 2.0 + 100 * 8.0) / 1e6, places=4)  # only the request with usage

    # -- Cline, Roo Code, Kilo Code --------------------------------------------------------
    def cline_task(self, editor, ext, task, cwd, cost=True):
        base = os.path.join("Library", "Application Support", editor, "User", "globalStorage", ext, "tasks", task)
        req = lambda ts, **kw: {"ts": ts, "type": "say", "say": "api_req_started", "text": json.dumps(kw)}
        self.write(os.path.join(base, "ui_messages.json"), [
            {"ts": self.T, "type": "say", "say": "task", "text": "Add dark mode to the settings page"},
            req(self.T + 1000, tokensIn=10, tokensOut=500, cacheWrites=2000, cacheReads=0, **({"cost": 0.0123} if cost else {})),
            req(self.T + 2000, tokensIn=5, tokensOut=100, cacheWrites=0, cacheReads=2000, **({"cost": 0.002} if cost else {})),
            {"ts": self.T + 2500, "type": "say", "say": "api_req_started", "text": "{not json"},
            {"ts": self.T + 2600, "type": "say", "say": "api_req_started", "text": json.dumps({"request": "pending"})},
            {"ts": self.T + 3000, "type": "say", "say": "completion_result", "text": "done"},
        ])
        self.write(os.path.join(base, "api_conversation_history.json"), [
            {"role": "user", "content": [{"type": "text", "text": "<environment_details>\n# Current Working Directory (%s) Files\n</environment_details>" % cwd}]}])
        self.write(os.path.join(base, "task_metadata.json"), {"model_usage": [{"ts": self.T, "model_id": "claude-sonnet-4-5-20250929"}]})

    def test_cline_roo_and_kilo_tasks(self):
        cwd = os.path.join(latidos.CODE_DIR, "repo-k")
        self.cline_task("Code", "saoudrizwan.claude-dev", "1731400000000", cwd)
        self.cline_task("Cursor", "rooveterinaryinc.roo-cline", "roo-task-1", cwd, cost=False)
        self.cline_task("Code", "kilocode.kilo-code", "kilo-1", cwd)
        h = self.hub("cline", "roo", "kilo")
        self.assertEqual(h.scan(), 6)
        cl = self.calls("cline")
        self.assertEqual([(x["inp"], x["out"], x["cwrite"], x["cread"], x["usd"]) for x in cl],
                         [(10, 500, 2000, 0, 0.0123), (5, 100, 0, 2000, 0.002)])
        self.assertEqual(cl[0]["model"], "claude-sonnet-4-5-20250929")
        s = self.session("cline", "1731400000000")
        self.assertEqual((s["title"], s["cwd"]), ("Add dark mode to the settings page", cwd))
        out = {a["id"]: a for a in self.summary(h)}
        self.assertAlmostEqual(out["cline"]["totals"]["usd"], 0.0143)             # Cline's own cost
        self.assertEqual(out["cline"]["totals"]["tool_cost"], 2)
        # Roo without a recorded cost: priced from tokens at Sonnet 4.5 (3 / 15 / 0.30 / 3.75).
        want = (10 * 3 + 500 * 15 + 2000 * 3.75 + 5 * 3 + 100 * 15 + 2000 * 0.30) / 1e6
        self.assertAlmostEqual(out["roo"]["totals"]["usd"], round(want, 4), places=4)
        self.assertEqual(out["kilo"]["projects"][0]["name"], "repo-k")

    # -- Aider --------------------------------------------------------------------------
    def test_aider_history_files_under_the_code_folders(self):
        repo = os.path.join(self.home, "code", "repo-a")
        os.makedirs(os.path.join(self.home, "code", "node_modules", "pkg"))
        self.write(os.path.join("code", "node_modules", "pkg", ".aider.chat.history.md"), "# aider chat started at 2026-11-12 10:00:00\n> Tokens: 1k sent, 1 received.\n")
        self.write(os.path.join("code", "repo-a", ".aider.chat.history.md"), "\n".join([
            "",
            "# aider chat started at 2026-11-12 10:00:00",
            "",
            "> Aider v0.86.0",
            "> Main model: anthropic/claude-sonnet-4-5 with diff edit format",
            "",
            "#### Add a health endpoint",
            "",
            "> Tokens: 4.4k sent, 213 received. Cost: $0.02 message, $0.02 session.",
            "> Tokens: 15k sent, 1.1k cache write, 13k cache hit, 194 received. Cost: $0.01 message, $0.03 session.",
            "",
            "# aider chat started at 2026-11-13 09:00:00",
            "> Model: gpt-4.1 with diff edit format",
            "#### Rename the module",
            "> Tokens: 1.5M sent, 2k received.",
            "> Tokens: garbage",
        ]))
        h = self.hub("aider", roots=[os.path.join(self.home, "code")])
        self.assertEqual(h.scan(), 4)
        c = self.calls("aider")
        self.assertEqual([(x["inp"], x["cwrite"], x["cread"], x["out"], x["usd"]) for x in c],
                         [(4400, 0, 0, 213, 0.02), (900, 1100, 13000, 194, 0.01),
                          (1500000, 0, 0, 2000, None), (0, 0, 0, 0, None)])
        self.assertEqual(c[2]["model"], "gpt-4.1")
        out = self.summary(h)[0]
        self.assertEqual(out["projects"][0]["name"], latidos.project_of(repo))
        self.assertAlmostEqual(out["totals"]["usd"], round(0.03 + (1500000 * 2.0 + 2000 * 8.0) / 1e6, 4), places=4)
        self.assertEqual(self.session("aider", c[0]["session"])["title"], "Add a health endpoint")
        self.assertEqual(self.session("aider", c[0]["session"])["cwd"], repo)

    # -- OpenCode ------------------------------------------------------------------------
    def test_opencode_storage(self):
        st = os.path.join(".local", "share", "opencode", "storage")
        cwd = os.path.join(latidos.CODE_DIR, "repo-o")
        self.write(os.path.join(st, "session", "proj1", "ses_1.json"), {
            "id": "ses_1", "title": "Refactor the parser", "directory": cwd, "time": {"created": self.T, "updated": self.T + 9000}})
        self.write(os.path.join(st, "message", "ses_1", "msg_1.json"), {"id": "msg_1", "role": "user", "time": {"created": self.T}})
        self.write(os.path.join(st, "message", "ses_1", "msg_2.json"), {
            "id": "msg_2", "role": "assistant", "modelID": "gpt-5", "providerID": "openai", "cost": 0,
            "tokens": {"input": 1000, "output": 100, "reasoning": 50, "cache": {"read": 4000, "write": 0}},
            "time": {"created": self.T + 1000}})
        self.write(os.path.join(st, "message", "ses_1", "msg_3.json"), {
            "id": "msg_3", "role": "assistant", "modelID": "claude-sonnet-4-5", "cost": 0.05,
            "tokens": {"input": 10, "output": 10, "cache": {"read": 0, "write": 0}}, "time": {"created": self.T + 2000}})
        h = self.hub("opencode")
        self.assertEqual(h.scan(), 2)
        self.assertEqual(h.scan(), 0)
        c = self.calls("opencode")
        self.assertEqual((c[0]["inp"], c[0]["out"], c[0]["cread"], c[0]["usd"]), (1000, 150, 4000, None))
        self.assertEqual(c[1]["usd"], 0.05)
        out = self.summary(h)[0]
        self.assertEqual(out["projects"][0]["tasks"][0]["title"], "Refactor the parser")
        want = 0.05 + (1000 * 1.25 + 150 * 10 + 4000 * 0.125) / 1e6
        self.assertAlmostEqual(out["totals"]["usd"], round(want, 4), places=4)

    # -- shared behaviour -----------------------------------------------------------------
    def test_absent_agents_are_inactive(self):
        h = self.hub()
        self.assertEqual(h.scan(), 0)
        self.assertTrue(all(not p["available"] for p in h.progress.values()))
        self.assertEqual(self.summary(h), [])

    def test_a_malformed_file_is_skipped_and_counted(self):
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-bad.json"), "{broken")
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-list.json"), "[1, 2]")
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-ok.json"), {"sessionId": "ok", "messages": [
            {"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 10, "output": 1}}]})
        h = self.hub("gemini")
        self.assertEqual(h.scan(), 1)
        self.assertEqual(h.progress["gemini"]["skipped"], 1)
        self.assertEqual(h.scan(), 0)  # the bad file is not retried until it changes
        self.assertEqual(h.progress["gemini"]["skipped"], 1)

    def test_unknown_model_counts_tokens_without_cost(self):
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-1.json"), {"sessionId": "s", "messages": [
            {"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "my-private-model", "tokens": {"input": 10, "output": 5}}]})
        h = self.hub("gemini")
        h.scan()
        a = self.summary(h)[0]
        self.assertEqual((a["totals"]["usd"], a["totals"]["tokens"], a["totals"]["unpriced"]), (0, 15, 1))

    def test_summary_bins_days_and_range(self):
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-1.json"), {"sessionId": "s", "messages": [
            {"id": "a", "timestamp": self.iso(self.T + 3600000), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 1000000, "output": 0}},
            {"id": "b", "timestamp": self.iso(self.T + 86400000 + 3600000), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 1000000, "output": 0}}]})
        h = self.hub("gemini")
        h.scan()
        a = self.summary(h)[0]
        self.assertEqual(len(a["days"]), 2)
        self.assertEqual(len(a["timeline"]), 2)
        self.assertAlmostEqual(sum(b["usd"] for b in a["timeline"]), 0.6)
        self.assertEqual(a["models"][0]["model"], "gemini-2.5-flash")
        # A range holding only the first call.
        one = self.summary(h, self.T, self.T + 86400000)[0]
        self.assertEqual(one["totals"]["requests"], 1)
        # A range with no call keeps the agent listed (its files exist) with zero totals.
        none = self.summary(h, 1, 2)[0]
        self.assertEqual(none["totals"]["requests"], 0)

    def test_hub_bumps_the_data_version_only_on_new_calls(self):
        v = lambda: int(self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()[0])
        p = self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-1.json"), {"sessionId": "s", "messages": [
            {"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 10, "output": 1}}]})
        h = self.hub("gemini")
        v0 = v()
        h.scan()
        self.assertEqual(v(), v0 + 1)
        os.utime(p, (time.time() + 5, time.time() + 5))  # touched, same content
        self.assertEqual(h.scan(), 0)
        self.assertEqual(v(), v0 + 1)

    def test_summary_endpoint_carries_agents(self):
        tmp = tempfile.mkdtemp()
        try:
            env = {"LATIDOS_ROOTS": os.path.join(tmp, "none"), "LATIDOS_DB": os.path.join(tmp, "l.db"),
                   "LATIDOS_FX_URL": "http://127.0.0.1:9/never"}
            with unittest.mock.patch.dict(os.environ, env):
                app = latidos.App(latidos.config())
            self.assertEqual(app.agents.readers[0].home, os.environ["LATIDOS_AGENTS_HOME"])
            self.assertEqual(app.agents_summary(0, 1, "day"), [])
            app.agents.store("codex", [{"id": "x", "ts": self.T, "session": "s", "model": "gpt-5", "inp": 1000000, "out": 0}],
                             [{"id": "s", "cwd": None}])
            out = app.agents_summary(0, self.T + 100, "day")
            self.assertEqual([a["id"] for a in out], ["codex"])
            self.assertAlmostEqual(out[0]["totals"]["usd"], 1.25)
            app.con.close()
        finally:
            shutil.rmtree(tmp)


class AgentAudit101(AgentFixtures, unittest.TestCase):
    """Findings of the independent audit of release 101 (release 102)."""

    def test_iso_ms_rejects_huge_infinite_and_old_times(self):
        ag = self.ag
        for bad in (1e30, float("inf"), float("nan"), 10 ** 400, "1e30", -5, 0, 1, "1990-01-01T00:00:00Z", True,
                    time.time() * 1000 + 3 * 86400000):
            self.assertIsNone(ag.iso_ms(bad), repr(bad)[:30])
        self.assertEqual(ag.iso_ms(self.T), self.T)
        self.assertEqual(ag.iso_ms(self.T / 1000), self.T)
        self.assertEqual(ag.iso_ms(self.iso(self.T)), self.T)

    def test_a_gemini_file_with_absurd_times_never_stops_the_pass(self):
        big = "1" + "0" * 400
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-a.json"),
                   '{"sessionId": "a", "startTime": %s, "messages": [{"id": "x", "type": "gemini", "timestamp": 1e30, '
                   '"tokens": {"input": 5, "output": 1}}, {"id": "y", "type": "gemini", "timestamp": Infinity, '
                   '"tokens": {"input": 5}}, {"id": "z", "type": "gemini", "timestamp": %s, "tokens": {"input": 5}}]}' % (big, big))
        w = self.write(os.path.join(".codeium", "windsurf", "cascade", "c1.pb"), "x")  # a reader after Gemini
        os.utime(w, (self.T / 1000, self.T / 1000))
        h = self.hub("gemini", "windsurf")
        h.scan()
        self.assertEqual(self.calls("gemini"), [])            # no call with a bogus time
        self.assertTrue(self.calls("windsurf"))              # the next reader still ran

    def test_any_error_in_a_file_is_skipped_and_the_next_readers_run(self):
        class Boom(self.ag.Reader):
            id, name = "boom", "Boom"

            def files(self):
                return [__file__]

            def parse(self, path, state):
                raise OverflowError("int too large")
        w = self.write(os.path.join(".codeium", "windsurf", "cascade", "c1.pb"), "x")
        os.utime(w, (self.T / 1000, self.T / 1000))
        h = self.hub("windsurf")
        h.readers.insert(0, Boom(self.home))
        h.progress["boom"] = {"available": False, "files": 0, "skipped": 0, "error": None}
        h.scan()
        self.assertEqual(h.progress["boom"]["skipped"], 1)
        self.assertTrue(self.calls("windsurf"))
        h.scan()  # recorded as skipped: not retried until it changes
        self.assertEqual(h.progress["boom"]["skipped"], 1)

    def test_tick_survives_any_agents_error(self):
        tmp = tempfile.mkdtemp()
        try:
            env = {"LATIDOS_ROOTS": os.path.join(tmp, "none"), "LATIDOS_DB": os.path.join(tmp, "l.db"),
                   "LATIDOS_FX_URL": "http://127.0.0.1:9/never", "LATIDOS_CURSOR_API": "0"}
            with unittest.mock.patch.dict(os.environ, env):
                app = latidos.App(latidos.config())
            app.agents.scan = unittest.mock.Mock(side_effect=OverflowError("boom"))
            app.cursor_due = lambda now=None: False   # never the real Cursor files
            app.cursor_api.scan = unittest.mock.Mock()
            with redirect_stderr(io.StringIO()) as err:
                app.tick(time.time() + 10 ** 6)
            self.assertIn("agents scan failed", err.getvalue())
            app.con.close()
        finally:
            shutil.rmtree(tmp)

    def test_active_days_count_days_not_conversations(self):
        day = self.T + 3600000
        for i in range(3):  # three conversations, all on one day
            p = self.write(os.path.join(".gemini", "antigravity", "conversations", "c%d.pb" % i), "x")
            os.utime(p, (day / 1000 + i, day / 1000 + i))
        p = self.write(os.path.join(".gemini", "antigravity", "conversations", "c9.pb"), "x")
        os.utime(p, ((day + 86400000) / 1000,) * 2)  # a fourth, the next day
        h = self.hub("antigravity")
        h.scan()
        # Birth times are the files' real creation (today): count only the two set days.
        t0 = self.T
        a = self.summary(h, t0, t0 + 2 * 86400000)[0]
        self.assertEqual(a["totals"]["requests"], 2)
        self.assertEqual(a["projects"][0]["requests"], 2)
        self.assertEqual(sorted(t["requests"] for t in a["projects"][0]["tasks"]), [1, 1, 1, 1])
        self.assertEqual([d["requests"] for d in a["days"]], [1, 1])
        self.assertEqual([d["convs"] for d in a["days"]], [3, 1])

    def test_agents_summary_is_cached(self):
        tmp = tempfile.mkdtemp()
        try:
            env = {"LATIDOS_ROOTS": os.path.join(tmp, "none"), "LATIDOS_DB": os.path.join(tmp, "l.db"),
                   "LATIDOS_FX_URL": "http://127.0.0.1:9/never"}
            with unittest.mock.patch.dict(os.environ, env):
                app = latidos.App(latidos.config())
            with unittest.mock.patch.object(latidos.agents, "summarize_agents", wraps=latidos.agents.summarize_agents) as sa:
                a = app.agents_summary(0, self.T, "day")
                b = app.agents_summary(0, self.T + 1000, "day")  # same minute
                self.assertIs(a, b)
                self.assertEqual(sa.call_count, 1)
                app.agents_summary(0, self.T + 120000, "day")    # another range
                self.assertEqual(sa.call_count, 2)
            app.con.close()
        finally:
            shutil.rmtree(tmp)

    def test_a_budgeted_scan_resumes_where_it_stopped(self):
        for i in range(5):
            self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-%d.json" % i), {"sessionId": "s%d" % i, "messages": [
                {"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 10}}]})
        h = self.hub("gemini")
        clock = iter(range(0, 1000))
        with unittest.mock.patch.object(self.ag.time, "monotonic", lambda: next(clock)):
            h.scan(budget_s=2.5)          # stops after a few files
            self.assertIsNotNone(h.resume)
            self.assertTrue(h.due())      # the next pass comes at once
            got = len(self.calls("gemini"))
            self.assertTrue(0 < got < 5)
            for _ in range(10):
                if h.resume is None:
                    break
                h.scan(budget_s=2.5)
        self.assertIsNone(h.resume)
        self.assertEqual(len(self.calls("gemini")), 5)

    def test_claude_code_unknown_models_are_counted_unpriced(self):
        self.assertTrue(latidos.unpriced("my-proxy-model"))
        self.assertFalse(latidos.unpriced("gpt-5"))
        self.assertFalse(latidos.unpriced("claude-opus-5-5"))
        self.assertFalse(latidos.unpriced("<synthetic>"))
        arn = "arn:aws:bedrock:us-east-1:ACCOUNT:application-inference-profile/abc123xyz"
        self.assertFalse(latidos.unpriced(arn))
        self.assertGreater(latidos.call_cost(arn, 1e6, 0, 0, 0, 0), 0)
        tmp = tempfile.mkdtemp()
        try:
            root = os.path.join(tmp, "projects", "p")
            os.makedirs(root)
            env = {"LATIDOS_ROOTS": os.path.join(tmp, "projects"), "LATIDOS_DB": os.path.join(tmp, "l.db"),
                   "LATIDOS_FX_URL": "http://127.0.0.1:9/never"}
            with open(os.path.join(root, "s1.jsonl"), "w") as fh:
                fh.write(assistant("m1", "2026-11-12T10:00:00Z", U, model="my-proxy-model") + "\n")
                fh.write(assistant("m2", "2026-11-12T10:01:00Z", U, model="claude-opus-5-5") + "\n")
            with unittest.mock.patch.dict(os.environ, env):
                app = latidos.App(latidos.config())
                app.ingestor.scan()
                d = app.summary(0, int(time.time() * 1000), None)
            self.assertEqual(d["totals"]["unpriced"], 1)
            self.assertEqual(d["totals"]["calls"], 2)
            app.con.close()
        finally:
            shutil.rmtree(tmp)

    def test_catch_all_rows_respect_the_tier(self):
        ag = self.ag
        self.assertEqual(ag.other_price("gemini-3.1-flash")[0], "gemini-3-flash")
        self.assertEqual(ag.other_price("gemini-3.1-pro-preview")[0], "gemini-3-pro")
        self.assertEqual(ag.other_price("gemini-3.1-flash-lite")[0], None)   # no such row: unpriced
        self.assertEqual(ag.other_price("gpt-5.4-mini")[0], "gpt-5-mini")
        self.assertEqual(ag.other_price("gpt-5.4-nano")[0], "gpt-5-nano")
        self.assertEqual(ag.other_price("gpt-5.4-pro")[0], "gpt-5-pro")
        self.assertEqual(ag.other_price("gpt-5.4")[0], "gpt-5")
        self.assertEqual(ag.other_price("gpt-5.1-codex-mini")[0], "gpt-5.1-codex-mini")
        self.assertEqual(ag.other_price("gemini-2.5-flash-lite")[0], "gemini-2.5-flash-lite")
        self.assertEqual(ag.other_price("o4-mini")[0], "o4-mini")
        self.assertAlmostEqual(ag.other_cost("deepseek-chat", 1e6, 1e6, 1e6), 0.28 + 0.42 + 0.028)

    def test_a_recorded_zero_cost_is_valued_at_list_price_everywhere(self):
        self.assertIsNone(self.ag.recorded_cost(0))
        self.assertIsNone(self.ag.recorded_cost("0"))
        self.assertEqual(self.ag.recorded_cost(0.25), 0.25)
        base = os.path.join("Library", "Application Support", "Code", "User", "globalStorage", "saoudrizwan.claude-dev", "tasks", "t0")
        self.write(os.path.join(base, "ui_messages.json"), [
            {"ts": self.T, "type": "say", "say": "api_req_started",
             "text": json.dumps({"tokensIn": 1000000, "tokensOut": 0, "cost": 0, "modelId": "gpt-5"})}])
        h = self.hub("cline")
        h.scan()
        self.assertIsNone(self.calls("cline")[0]["usd"])
        self.assertAlmostEqual(self.summary(h)[0]["totals"]["usd"], 1.25)

    def test_gemini_session_resolves_its_folder_once_known(self):
        import hashlib
        cwd = os.path.join(latidos.CODE_DIR, "repo-late")
        hsh = hashlib.sha256(cwd.encode()).hexdigest()
        self.write(os.path.join(".gemini", "tmp", hsh, "chats", "session-1.json"), {"sessionId": "late", "projectHash": hsh, "messages": [
            {"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 10}}]})
        h = self.hub("gemini")
        h.scan()
        self.assertIsNone(self.session("gemini", "late")["cwd"])
        h.scan()  # nothing new known: not re-read
        self.con.execute("INSERT INTO calls (msg_id, ts, cwd) VALUES ('m', 1, ?)", (cwd,))
        self.con.commit()
        h._dirs = (0, [])  # the folder list is cached for 5 min
        h.scan()
        self.assertEqual(self.session("gemini", "late")["cwd"], cwd)
        sig = self.con.execute("SELECT sig FROM agent_files").fetchone()[0]
        self.assertNotIn("|dirs=", sig)  # resolved: a plain signature again



class AgentAudit102(AgentFixtures, unittest.TestCase):
    """Findings of the independent audit of release 102 (release 104)."""

    def app(self, tmp):
        env = {"LATIDOS_ROOTS": os.path.join(tmp, "none"), "LATIDOS_DB": os.path.join(tmp, "l.db"),
               "LATIDOS_FX_URL": "http://127.0.0.1:9/never"}
        with unittest.mock.patch.dict(os.environ, env):
            return latidos.App(latidos.config())

    def test_agents_cache_never_older_than_the_summary(self):
        tmp = tempfile.mkdtemp()
        try:
            app = self.app(tmp)
            v = app.ingestor.version
            with unittest.mock.patch.object(latidos.agents, "summarize_agents", side_effect=lambda *a, **k: [object()]) as sa:
                a = app.agents_summary(0, self.T, "day", min_version=v)
                self.assertIs(app.agents_summary(0, self.T, "day", min_version=v), a)   # same version: cached
                self.assertEqual(sa.call_count, 1)
                with app.lock:
                    app.con.execute("UPDATE meta SET v = CAST(v AS INTEGER) + 1 WHERE k = 'data_version'")
                    app.con.commit()
                b = app.agents_summary(0, self.T, "day", min_version=v + 1)  # the summary is newer
                self.assertIsNot(a, b)
                self.assertEqual(sa.call_count, 2)
                # A summary served stale (older version) still takes the newer block.
                self.assertIs(app.agents_summary(0, self.T, "day", min_version=v), b)
                self.assertEqual(sa.call_count, 2)
            app.con.close()
        finally:
            shutil.rmtree(tmp)

    def test_days_split_right_in_half_hour_time_zones(self):
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Kolkata"
        time.tzset()
        try:
            d = dt.datetime(2026, 11, 19, 23, 50)
            a = int(d.timestamp() * 1000)
            h = self.hub()
            h.store("codex", [{"id": "1", "ts": a, "session": "s", "model": "gpt-5", "inp": 10},
                              {"id": "2", "ts": a + 20 * 60000, "session": "s", "model": "gpt-5", "inp": 10}], [])
            out = self.ag.summarize_agents(self.con, self.lock, 0, a + 3600000, "day", latidos.bin_start,
                                           latidos.project_of, readers=h.readers, progress=h.progress)
            days = [x["t"] for x in out[0]["days"]]
            self.assertEqual([dt.datetime.fromtimestamp(t / 1000).day for t in days], [19, 20])
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_aider_walks_nested_roots_to_their_own_depth(self):
        code = os.path.join(self.home, "code")
        deep = os.path.join("code", "projects", "g1", "g2", "g3", "repo", ".aider.chat.history.md")
        hidden = os.path.join("code", ".ws", "repo2", ".aider.chat.history.md")
        top = os.path.join("code", "repo0", ".aider.chat.history.md")
        for rel in (deep, hidden, top):
            self.write(rel, "# aider chat started at 2026-11-12 10:00:00\n")
        os.symlink(code, os.path.join(self.home, "code-link"))
        r = self.ag.AiderReader(self.home, roots=[code, os.path.join(code, "projects"), os.path.join(code, ".ws"),
                                                  os.path.join(self.home, "code-link")])
        self.assertEqual(len(r.search_roots()), 3)   # the link to code is the same folder
        found = sorted(os.path.relpath(p, self.home) for p in r.files())
        self.assertEqual(found, sorted([deep, hidden, top]))

    def test_a_skipped_file_leaves_nothing_behind(self):
        class Half(self.ag.Reader):
            id, name = "half", "Half"

            def files(self):
                return [__file__]

            def parse(self, path, state):
                ts = AgentAudit102.T
                return [{"id": "ok1", "ts": ts, "session": "s", "model": "gpt-5", "inp": 10},
                        {"id": "bad", "ts": ts, "session": object(), "model": "gpt-5", "inp": 10}], [], {}
        h = self.hub("gemini")
        h.readers = [Half(self.home)]
        h.progress = {"half": {"available": False, "files": 0, "skipped": 0, "error": None}}
        v0 = self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()[0]
        self.assertEqual(h.scan(), 0)
        self.assertEqual(h.progress["half"]["skipped"], 1)
        self.assertEqual(self.calls("half"), [])
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()[0], v0)

    def test_token_counts_are_capped(self):
        self.assertEqual(self.ag._int(10 ** 30), 0)
        self.assertEqual(self.ag._int(float("inf")), 0)
        self.assertEqual(self.ag._int(5000), 5000)

    def test_a_model_that_is_not_text_is_stored_as_text(self):
        h = self.hub()
        h.store("gemini", [{"id": "m", "ts": self.T, "session": "s", "model": 5, "inp": 10}], [])
        self.assertEqual(self.con.execute("SELECT typeof(model) FROM agent_calls").fetchone()[0], "text")

    def test_a_stored_zero_cost_is_valued_at_list_price(self):
        h = self.hub()
        # A row written by release 101, which stored a recorded 0 as 0.
        self.con.execute("INSERT INTO agent_calls (agent, id, ts, session, model, inp, out, cread, cwrite, usd) "
                         "VALUES ('cline', 'x', ?, 's', 'gpt-5', 1000000, 0, 0, 0, 0)", (self.T,))
        self.con.commit()
        a = self.summary(h)[0]
        self.assertAlmostEqual(a["totals"]["usd"], 1.25)
        self.assertEqual(a["totals"]["tool_cost"], 0)

    def test_a_session_only_change_moves_the_data_version(self):
        v = lambda: int(self.con.execute("SELECT v FROM meta WHERE k='data_version'").fetchone()[0])
        p = self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-1.json"), {"sessionId": "s", "messages": [
            {"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 10}}]})
        h = self.hub("gemini")
        h.scan()
        v0 = v()
        # Same calls, a title now: only the session changes.
        self.write(os.path.join(".gemini", "tmp", "h1", "chats", "session-1.json"), {"sessionId": "s", "summary": "Fix the tests",
            "messages": [{"id": "a", "timestamp": self.iso(self.T), "type": "gemini", "model": "gemini-2.5-flash", "tokens": {"input": 10}}]})
        os.utime(p, (time.time() + 5, time.time() + 5))
        self.assertEqual(h.scan(), 0)
        self.assertEqual(v(), v0 + 1)
        self.assertEqual(self.session("gemini", "s")["title"], "Fix the tests")
        os.utime(p, (time.time() + 10, time.time() + 10))
        h.scan()  # read again, nothing new: no move
        self.assertEqual(v(), v0 + 1)

    def test_a_budgeted_pass_always_makes_progress_and_resumes_by_path(self):
        parsed = []
        test = self

        class Slow(self.ag.Reader):
            id, name = "slow", "Slow"

            def __init__(self, home):
                test.ag.Reader.__init__(self, home)
                self.dir = os.path.join(home, "slow")
                os.makedirs(self.dir, exist_ok=True)
                for i in range(4):
                    open(os.path.join(self.dir, "f%d" % i), "w").write("x")

            def files(self):
                clock[0] += 10  # listing alone uses up the budget
                return sorted(os.path.join(self.dir, f) for f in os.listdir(self.dir))

            def parse(self, path, state):
                parsed.append(os.path.basename(path))
                return [{"id": os.path.basename(path), "ts": test.T, "session": "s", "model": "gpt-5", "inp": 1}], [], {}
        clock = [0.0]
        h = self.hub("gemini")
        h.readers = [Slow(self.home)]
        h.progress = {"slow": {"available": False, "files": 0, "skipped": 0, "error": None}}
        with unittest.mock.patch.object(self.ag.time, "monotonic", lambda: clock[0]):
            h.scan(budget_s=5)
            self.assertEqual(parsed, ["f0"])           # at least one file, whatever the budget
            self.assertEqual(h.resume, (0, os.path.join(h.readers[0].dir, "f1")))
            open(os.path.join(h.readers[0].dir, "a_new"), "w").write("x")  # sorts before the resume point
            for _ in range(10):
                if h.resume is None:
                    break
                h.scan(budget_s=5)
        self.assertEqual(parsed, ["f0", "f1", "f2", "f3"])  # resumed at f1, no file read twice
        h.scan()
        self.assertEqual(sorted(parsed), ["a_new", "f0", "f1", "f2", "f3"])  # the new one on the next full pass

    def test_grok_fast_models(self):
        self.assertEqual(self.ag.other_price("x-ai/grok-4.1-fast")[0], "grok-4.1-fast")
        self.assertAlmostEqual(self.ag.other_cost("grok-4.1-fast", 1e6, 1e6, 0), 0.70)
        self.assertEqual(self.ag.other_price("grok-4-fast-reasoning")[0], "grok-4-fast")
        self.assertEqual(self.ag.other_price("grok-code-fast-1")[0], "grok-code-fast")
        self.assertEqual(self.ag.other_price("grok-4")[0], "grok-4")


class AgentAudit104(AgentFixtures, unittest.TestCase):
    """Findings of the independent audit of release 104 (release 105)."""

    def test_num_keeps_only_finite_sane_costs(self):
        ag = self.ag
        for bad in (float("inf"), float("nan"), "9" * 400, "1e999", 1e7, -1, "x", None):
            self.assertIsNone(ag._num(bad), repr(bad)[:20])
        self.assertEqual(ag._num("0.02"), 0.02)
        self.assertEqual(ag._num(0), 0.0)

    def test_an_infinite_aider_cost_never_reaches_the_summary(self):
        self.write(os.path.join("code", "repo-a", ".aider.chat.history.md"),
                   "# aider chat started at 2026-11-12 10:00:00\n> Model: gpt-4.1 with diff edit format\n"
                   "> Tokens: 1k sent, 10 received. Cost: $%s message, $1 session.\n" % ("9" * 400))
        self.write(os.path.join(".local", "share", "opencode", "storage", "message", "ses_1", "m.json"),
                   '{"id": "m", "role": "assistant", "modelID": "gpt-5", "cost": 1e999, '
                   '"tokens": {"input": 1000000, "output": 0}, "time": {"created": %d}}' % self.T)
        h = self.hub("aider", "opencode", roots=[os.path.join(self.home, "code")])
        h.scan()
        self.assertEqual([r[0] for r in self.con.execute("SELECT usd FROM agent_calls")], [None, None])
        out = {a["id"]: a for a in self.summary(h)}
        self.assertAlmostEqual(out["opencode"]["totals"]["usd"], 1.25)            # list price instead
        self.assertAlmostEqual(out["aider"]["totals"]["usd"], round((1000 * 2 + 10 * 8) / 1e6, 4))
        json.dumps(list(out.values()), allow_nan=False)                         # strict JSON

    def test_a_stored_infinite_cost_is_ignored(self):
        h = self.hub()
        self.con.execute("INSERT INTO agent_calls (agent, id, ts, session, model, inp, out, cread, cwrite, usd) "
                         "VALUES ('cline', 'x', ?, 's', 'gpt-5', 1000000, 0, 0, 0, ?)", (self.T, float("inf")))
        self.con.commit()
        self.assertAlmostEqual(self.summary(h)[0]["totals"]["usd"], 1.25)

    def test_json_answers_are_strict(self):
        b = latidos.json_bytes({"a": float("inf"), "b": [1.5, float("nan")], "c": {"d": float("-inf")}, "e": "x"})
        self.assertEqual(json.loads(b), {"a": None, "b": [1.5, None], "c": {"d": None}, "e": "x"})
        self.assertNotIn(b"Infinity", b)
        self.assertEqual(latidos.json_bytes({"a": 1}), b'{"a":1}')

    def test_a_rolled_back_store_counts_no_session_change(self):
        h = self.hub()
        with self.assertRaises(AttributeError):
            h.store("gemini", [], [{"id": "s1", "title": "A title", "cwd": "/x"}, "not a session"])
        self.assertEqual(h.sessions_changed, 0)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0], 0)

    def test_a_giant_aider_token_figure_keeps_the_rest_of_the_history(self):
        tk = self.ag.AiderReader.tokens_of("%s sent, 213 received" % ("9" * 400))
        self.assertEqual((tk["sent"], tk["received"]), (0, 213))
        self.write(os.path.join("code", "repo-b", ".aider.chat.history.md"),
                   "# aider chat started at 2026-11-12 10:00:00\n"
                   "> Tokens: %s sent, 5 received.\n> Tokens: 2k sent, 50 received. Cost: $0.01 message.\n" % ("9" * 400))
        h = self.hub("aider", roots=[os.path.join(self.home, "code")])
        self.assertEqual(h.scan(), 2)
        self.assertEqual(h.progress["aider"]["skipped"], 0)


if __name__ == "__main__":
    unittest.main()
