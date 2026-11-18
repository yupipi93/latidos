"""Tests for scripts/claude-auto-switch.py: room per window, freshness, the pick, limit-error
detection, the switch steps and the tick decisions. No account is switched, no tab moved,
nothing notified: every side effect is mocked or pointed at a temp dir.

Run from the repository root:
    python3 -m unittest accounts/test_claude_auto_switch.py -v
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import datetime as dt
import importlib.util
import io
import json
import os
import tempfile
import atexit as _atexit
import shutil as _shutil

# Every temp dir these tests make goes under one dir removed at exit (audit of relaunch
# release 34: thousands of cca-test-* dirs had piled up in $TMPDIR).
tempfile.tempdir = tempfile.mkdtemp(prefix="claude-accounts-tests-")
_atexit.register(_shutil.rmtree, tempfile.tempdir, True)
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "claude-auto-switch.py"
spec = importlib.util.spec_from_file_location("claude_auto_switch", SCRIPT)
cas = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cas)

AT = dt.datetime(2026, 11, 12, 19, 0, tzinfo=dt.timezone.utc)


def iso(delta_hours: float, noise: str = "") -> str:
    return (AT + dt.timedelta(hours=delta_hours)).isoformat().replace("+00:00", noise + "+00:00")


def rep(session: float, weekly: float, week_reset_h: float = 100, session_reset_h: float = 2,
        age_min: float = 1, **extra) -> dict:
    return {"source": "live", "fetched_at": iso(-age_min / 60),
            "session": {"percent": session, "resets_at": iso(session_reset_h), "full": session >= 100, "locked": False},
            "weekly": {"percent": weekly, "resets_at": iso(week_reset_h), "full": weekly >= 100, "locked": False},
            **extra}


REAL = lambda: {"lt": rep(3, 18), "mt": rep(21, 88), "na": rep(40, 53)}  # 2026-11-12 19:00 UTC


def summary(*entries) -> str:
    """Output of cursor-claude-account.py --move-open --summary with these (name, outcome, cut)."""
    data = [{"name": n, "session": n, "outcome": o, "cut": c} for n, o, c in entries]
    return "some human lines\nMOVE-SUMMARY " + json.dumps(data, ensure_ascii=False)



# Hermetic by default: no test may reach the real cut-moves record (audit of release 26).
cas.CUT_MOVES = Path(tempfile.mkdtemp(prefix="cas-cut-")) / "cut-moves.json"

class Numbers(unittest.TestCase):
    def test_room_rules(self):
        self.assertEqual(cas.room({"percent": 30, "resets_at": iso(1)}, AT), 70)
        self.assertEqual(cas.room({"percent": 100, "full": True, "resets_at": iso(1)}, AT), 0)
        self.assertEqual(cas.room({"percent": 10, "locked": True, "resets_at": iso(1)}, AT), 0)
        self.assertEqual(cas.room({"percent": 100, "full": True, "resets_at": iso(-0.1)}, AT), 100, "past its reset")
        self.assertIsNone(cas.room(None, AT))

    def test_spent_only_by_the_server_flags(self):
        # Audit of release 1: a shown 100 % that is not "full" is not spent.
        self.assertEqual(cas.spent_windows(rep(100, 40), AT), ["session"])
        self.assertEqual(cas.spent_windows(rep(20, 100), AT), ["weekly"])
        almost = rep(99, 40)
        almost["session"]["percent"] = 100
        self.assertEqual(cas.spent_windows(almost, AT), [])
        self.assertEqual(cas.spent_windows(rep(100, 40, extra_usage=True), AT), [])

    def test_api_shape_and_cache_shape(self):
        # Audit of release 1: the API answers {accounts: [...]}, the cache {tag: report}.
        api = {"updated_at": "x", "accounts": [dict(rep(3, 18), tag="lt"), dict(rep(21, 88), tag="mt")]}
        self.assertEqual(sorted(cas.normalize(api)), ["lt", "mt"])
        self.assertEqual(sorted(cas.normalize(REAL())), ["lt", "mt", "na"])
        self.assertEqual(cas.normalize([1, 2]), {})

    def test_stale_numbers_are_not_trusted(self):
        self.assertTrue(cas.is_fresh(rep(3, 18), AT))
        self.assertFalse(cas.is_fresh(rep(3, 18, age_min=45), AT))
        self.assertFalse(cas.is_fresh(rep(3, 18, source="stale"), AT))

    def test_path_finds_the_claude_cli(self):
        # Audit of release 1: launchd's PATH had no ~/.local/bin; without USER the CLI
        # cannot read the login from the Keychain.
        self.assertIn(str(Path.home() / ".local/bin"), os.environ["PATH"].split(os.pathsep))
        self.assertTrue(os.environ.get("USER"))


class Pick(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(cas, "ORDER", ["lt", "mt", "na"])
        p.start()
        self.addCleanup(p.stop)

    def test_more_room_beats_the_priority(self):
        # the owner, 2026-11-16: mt at 85 % of its week and na at 42 % with the same reset: na,
        # although mt goes first in the priority.
        quota = {"lt": rep(100, 17, week_reset_h=160), "mt": rep(31, 85, week_reset_h=38), "na": rep(7, 42, week_reset_h=37)}
        self.assertEqual(cas.pick(quota, "lt", AT), "na")
        self.assertEqual(cas.pick(quota, "na", AT), "mt", "the only other one with room")

    def test_priority_breaks_near_ties(self):
        quota = {"lt": rep(10, 50), "mt": rep(10, 45), "na": rep(10, 40)}
        self.assertEqual(cas.pick(quota, "na", AT), "lt")
        quota = {"lt": rep(10, 60), "mt": rep(10, 55), "na": rep(10, 50)}
        self.assertEqual(cas.pick(quota, "lt", AT), "mt", "5 points less room, one place higher")

    def test_room_about_to_expire_is_used_first(self):
        # 15 % that resets in 10 hours is lost if not used; 58 % lasts six more days.
        quota = {"lt": rep(100, 100), "mt": rep(10, 85, week_reset_h=10), "na": rep(10, 42, week_reset_h=150)}
        self.assertEqual(cas.pick(quota, "lt", AT), "mt")

    def test_low_five_hour_room_lowers_the_score(self):
        quota = {"lt": rep(100, 100), "mt": rep(85, 20), "na": rep(10, 40)}
        self.assertEqual(cas.pick(quota, "lt", AT), "na", "mt has 15 % of its 5-hour window left")

    def test_order_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "order.json"
            self.assertIsNone(cas.read_order_file(f), "missing")
            f.write_text("{bad")
            self.assertIsNone(cas.read_order_file(f), "broken")
            f.write_text(json.dumps({"order": ["na", "lt", "mt"]}))
            self.assertEqual(cas.parse_order(cas.read_order_file(f))[0], ["na", "lt", "mt"])

    def test_order_is_configurable(self):
        quota = {"lt": rep(10, 50), "mt": rep(10, 50), "na": rep(10, 50)}
        with mock.patch.object(cas, "ORDER", ["na", "mt", "lt"]):
            self.assertEqual(cas.pick(quota, "lt", AT), "na")

    def test_upgrade_target(self):
        quota = {"lt": rep(50, 50), "mt": rep(10, 90), "na": rep(10, 90)}
        self.assertEqual(cas.upgrade_target(quota, "na", AT, {}), "lt")
        self.assertIsNone(cas.upgrade_target(quota, "lt", AT, {}), "the best already")
        quota["lt"] = rep(85, 50)
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {}), "lt has 15 % of 5 h < 20 %, mt is no better")
        quota["lt"] = rep(50, 50)
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {"lt": iso(1)}), "skipped ones wait")
        quota["lt"] = rep(10, 10, age_min=60)
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {}), "stale numbers are not trusted")

    def test_rebalance_goes_down_the_priority_too(self):
        # The case of 2026-11-16: on mt with 15 % of the week, na has 58 %.
        quota = {"lt": rep(100, 17, week_reset_h=160), "mt": rep(31, 85, week_reset_h=38), "na": rep(7, 42, week_reset_h=37)}
        self.assertEqual(cas.upgrade_target(quota, "mt", AT, {}), "na")
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {}), "no way back to mt")

    def test_no_leaving_minutes_before_the_five_hour_reset(self):
        # Audit of 43: na nearly out of its 5-hour window that resets in 5 min was left for
        # lt, and the tool came back 30 min later.
        quota = {"lt": rep(0, 17, week_reset_h=160), "mt": rep(100, 85, week_reset_h=40),
                 "na": rep(89, 48, week_reset_h=33, session_reset_h=5 / 60)}
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {}))
        quota["na"] = rep(89, 48, week_reset_h=33, session_reset_h=3)
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {}),
                          "the account in use is judged on its week: a low 5-hour window switches only once spent")
        # As a target, a window that refills in minutes is nearly full; in 2 hours it is not.
        self.assertGreater(cas.score("na", rep(89, 48, session_reset_h=5 / 60), AT),
                           2 * cas.score("na", rep(89, 48, session_reset_h=2), AT))

    def test_expiring_week_is_used_first(self):
        # Audit of 43: lt with 8.5 % of its week resetting in 2 h was left for mt.
        quota = {"lt": rep(10, 91.5, week_reset_h=2), "mt": rep(10, 40, week_reset_h=100),
                 "na": rep(10, 40, week_reset_h=150)}
        self.assertIsNone(cas.upgrade_target(quota, "lt", AT, {}))

    def test_no_rebalance_for_a_few_expiring_percent(self):
        quota = {"lt": rep(10, 50, week_reset_h=100), "mt": rep(10, 50, week_reset_h=100),
                 "na": rep(10, 92, week_reset_h=3)}
        self.assertIsNone(cas.upgrade_target(quota, "lt", AT, {}), "na has 8 %: under 15 %")
        self.assertEqual(cas.pick(quota, "lt", AT), "na", "but when lt is spent, it is the best score")

    def test_order_file_follows_latidos_config_dir(self):
        with mock.patch.dict(os.environ, {"LATIDOS_CONFIG_DIR": "/x/y"}, clear=False):
            os.environ.pop("CLAUDE_ACCOUNTS_ORDER_FILE", None)
            self.assertEqual(cas._order_file(), Path("/x/y/claude-order.json"))
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNTS_ORDER_FILE": "/z/o.json", "LATIDOS_CONFIG_DIR": "/x/y"}):
            self.assertEqual(cas._order_file(), Path("/z/o.json"))

    def test_paid_extra_usage_is_left_for_free_room(self):
        # Audit of 44: mt billing extra usage (5 h full) stayed while lt had free room.
        quota = {"lt": rep(0, 50), "mt": rep(100, 40, extra_usage=True), "na": rep(100, 100)}
        self.assertEqual(cas.upgrade_target(quota, "mt", AT, {}), "lt")
        quota = {"lt": rep(100, 100), "mt": rep(10, 30), "na": rep(100, 40, extra_usage=True)}
        self.assertEqual(cas.upgrade_target(quota, "na", AT, {}), "mt")
        quota["mt"] = rep(10, 30, extra_usage=True)  # extra usage on but not needed: free room
        self.assertEqual(cas.current_score("mt", quota["mt"], AT), cas.score("mt", quota["mt"], AT, session=False))

    def test_paying_leaves_for_any_free_room(self):
        # Audit of 45: lt with 10 % of its week (under the 15 % rebalance rule) is free room.
        quota = {"lt": rep(10, 90), "mt": rep(100, 100), "na": rep(100, 40, extra_usage=True)}
        self.assertEqual(cas.upgrade_target(quota, "na", AT, {}), "lt")
        self.assertIsNone(cas.upgrade_target(quota, "na", AT, {"lt": iso(1)}), "skip lists still hold")

    def test_paying_needs_fresh_numbers_and_waits_a_near_refill(self):
        self.assertFalse(cas.paying(rep(100, 40, extra_usage=True, age_min=60), AT), "stale")
        self.assertFalse(cas.paying(rep(100, 40, extra_usage=True, session_reset_h=10 / 60), AT), "refills in 10 min")
        self.assertTrue(cas.paying(rep(100, 40, extra_usage=True, session_reset_h=1), AT))

    def test_full_week_refilling_soon_is_waited_out(self):
        # Audit of 47: weekly full with extra usage, reset in 10 min: no switch away.
        lt = rep(10, 100, week_reset_h=10 / 60, extra_usage=True)
        quota = {"lt": lt, "mt": rep(10, 50), "na": rep(10, 60)}
        self.assertFalse(cas.paying(lt, AT))
        self.assertIsNone(cas.upgrade_target(quota, "lt", AT, {}))
        quota["lt"] = rep(10, 100, week_reset_h=1, extra_usage=True)
        self.assertEqual(cas.upgrade_target(quota, "lt", AT, {}), "mt", "an hour of paid usage is not waited out")

    def test_malformed_cache_tags_are_skipped(self):
        self.assertEqual(cas.normalize({"accounts": [{"tag": ["lt"]}, {"tag": "mt", "x": 1}]}), {"mt": {"tag": "mt", "x": 1}})

    def test_rebalance_needs_a_clear_gain(self):
        quota = {"lt": rep(10, 40), "mt": rep(10, 30), "na": rep(100, 100)}
        self.assertIsNone(cas.upgrade_target(quota, "mt", AT, {}), "60 vs 70 %: not worth the move")

    def test_thresholds_extra_usage_and_no_room(self):
        quota = {"lt": rep(100, 50), "mt": rep(95, 10), "na": rep(10, 97)}
        self.assertIsNone(cas.pick(quota, "lt", AT))
        quota["na"] = rep(10, 97, extra_usage=True)
        self.assertEqual(cas.pick(quota, "lt", AT), "na", "extra usage as the last resort")

    def test_free_room_beats_paid_extra_usage(self):
        # Audit of auto switch release 2: credits must not win over free plan quota.
        quota = {"mt": rep(100, 50), "na": rep(100, 100, extra_usage=True), "lt": rep(18, 18)}
        self.assertEqual(cas.pick(quota, "mt", AT), "lt")

    def test_stale_or_recently_spent_accounts_are_skipped(self):
        quota = {"lt": rep(100, 50), "mt": rep(10, 10, age_min=60), "na": rep(40, 40)}
        self.assertEqual(cas.pick(quota, "lt", AT), "na", "mt's numbers are an hour old")
        quota["mt"] = rep(10, 10)
        self.assertEqual(cas.pick(quota, "lt", AT, {"mt": iso(1)}), "na", "mt was left on a limit error")
        self.assertEqual(cas.pick(quota, "lt", AT, {"mt": iso(-1)}), "mt", "its skip ended")

    def test_window_past_its_reset_counts_as_empty(self):
        quota = {"lt": rep(100, 50), "mt": rep(100, 30, session_reset_h=-0.5), "na": rep(60, 60)}
        self.assertEqual(cas.pick(quota, "lt", AT), "mt")


class LimitErrors(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cas-"))
        (self.tmp / "p" / "x").mkdir(parents=True)
        p = mock.patch.object(cas.cqr, "PROJECTS", self.tmp / "p")
        p.start()
        self.addCleanup(p.stop)

    def write(self, sid, ts, text="You've hit your 5-hour limit", after=()):
        limit = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "uuid": "L", "timestamp": ts,
                 "entrypoint": "claude-vscode", "message": {"content": [{"type": "text", "text": text}]}}
        rows = [limit, *after]
        (self.tmp / "p" / "x" / f"{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_local_command_after_the_error_does_not_hide_it(self):
        # Audit of release 18: the owner types /usage in the cut tab to check; the command and
        # its output are plain "user" entries.
        ts = iso(-0.05)
        usage = [{"type": "user", "timestamp": ts, "message": {"content": "<command-name>/usage</command-name>"}},
                 {"type": "user", "timestamp": ts,
                  "message": {"content": "<local-command-stdout>5h 100%</local-command-stdout>"}}]
        self.write("s1", ts, after=usage)
        self.assertEqual(self.check(-1), ["s1"])

    def test_task_notification_after_the_error_does_not_hide_it(self):
        # Release 42: the chat's subagents fail on the same limit seconds later and their
        # notices land after the error as user entries.
        ts = iso(-0.05)
        note = {"type": "user", "timestamp": ts, "message": {
            "content": "<task-notification>\n<task-id>a1</task-id>\n<status>failed</status>\n</task-notification>"}}
        self.write("s1", ts, after=[note, dict(note)])
        self.assertEqual(self.check(-1), ["s1"])

    def test_a_real_message_after_the_error_does(self):
        ts = iso(-0.05)
        self.write("s1", ts, after=[{"type": "user", "timestamp": ts, "message": {"content": "sigue"}}])
        self.assertEqual(self.check(-1), [])

    def check(self, started_h):
        sessions = [{"pid": 1, "session": "s1", "started": AT + dt.timedelta(hours=started_h)}]
        with mock.patch.object(cas, "live_sessions", return_value=sessions):
            return [h["session"] for h in cas.fresh_limit_errors("mt", AT)]

    def test_error_of_the_current_process_counts(self):
        self.write("s1", iso(-0.05))
        self.assertEqual(self.check(-1), ["s1"])

    def test_error_text_is_kept_for_the_cut_record(self):
        # Audit of release 26: the reset check depends on it.
        self.write("s1", iso(-0.05))
        with mock.patch.object(cas, "live_sessions", return_value=[{"pid": 1, "session": "s1", "started": AT - dt.timedelta(hours=1)}]):
            hit = cas.fresh_limit_errors("mt", AT)[0]
        self.assertIn("5-hour limit", hit["text"])

    def test_error_from_before_the_process_started_does_not(self):
        self.write("s1", iso(-0.5))
        self.assertEqual(self.check(-0.1), [])

    def test_old_error_does_not(self):
        self.write("s1", iso(-1))
        self.assertEqual(self.check(-2), [])

    def test_error_whose_window_already_reset_does_not(self):
        # Audit of release 1: error at 18:58 naming a reset at 18:59 (Madrid 19:59), tick at 19:00.
        self.write("s1", iso(-2 / 60), text="You've hit your 5-hour limit · resets 7:59pm (Europe/Madrid)")
        self.assertEqual(self.check(-1), [])


class SwitchSteps(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cas-sw-"))
        self.active = self.tmp / "active"
        self.active.write_text("mt\n")
        self.calls = []
        for p in (mock.patch.object(cas, "ACTIVE", self.active),
                  mock.patch.dict(cas.ACCOUNTS, {"lt": self.tmp, "mt": self.tmp, "na": self.tmp})):
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, fail=None):
        def run(cmd, timeout=300):
            step = " ".join(Path(c).name for c in cmd[1:])
            self.calls.append(step)
            return (1, "boom") if fail and fail in step else (0, "")
        return run

    def test_default_written_last_after_every_step(self):
        with mock.patch.object(cas, "run", side_effect=self.fake_run()):
            ok, _ = cas.switch_to("lt")
        self.assertTrue(ok)
        self.assertEqual(self.active.read_text().strip(), "lt")
        self.assertIn("--verify", self.calls[0])
        self.assertTrue(any("claude-shared-pool.py" in c for c in self.calls))

    def test_failed_cursor_step_leaves_the_default_alone(self):
        # Audit of release 1: a half switch left ~/.claude-active on the target.
        with mock.patch.object(cas, "run", side_effect=self.fake_run(fail="--quiet --no-verify")):
            ok, detail = cas.switch_to("lt")
        self.assertFalse(ok)
        self.assertEqual(self.active.read_text().strip(), "mt")
        self.assertIn("Cursor not switched", detail)

    def test_t3_failure_is_reported_not_fatal(self):
        with mock.patch.object(cas, "run", side_effect=self.fake_run(fail="t3-claude-account.py")):
            ok, detail = cas.switch_to("lt")
        self.assertTrue(ok)
        self.assertIn("T3 Code not switched", detail)


class SlowNotices(unittest.TestCase):
    def test_hung_notify_does_not_break_the_tick(self):
        # Audit of auto switch release 7.
        with mock.patch.object(cas.CFG, "notify_argv", ["/bin/sh"]), \
             mock.patch.object(cas.accounts_config.subprocess, "run", side_effect=cas.subprocess.TimeoutExpired("notify", 60)), \
             mock.patch.object(cas, "log") as log:
            cas.notify("t", "b", "crit")  # no exception
        self.assertIn("notice not sent", log.call_args[0][0])

    def test_truncated_read_is_no_read(self):
        import http.client
        with mock.patch.object(cas.urllib.request, "urlopen", side_effect=http.client.IncompleteRead(b"x")):
            self.assertEqual(cas.read_fresh(), {})


class Status(unittest.TestCase):
    def test_status_shows_skips_and_hides_old_failures(self):
        st = {"failed": {"na": iso(2)}, "spent": {"mt": iso(1)},
              "fail_counts": {"na": 1, "lt": 3}, "last_fail_at": {"na": iso(-1), "lt": iso(-24 * 30)},
              "spent_confirmed": {"tag": "mt", "until": iso(1)}}
        with mock.patch.object(cas, "now", return_value=AT), mock.patch.object(cas, "load_state", return_value=st), \
             mock.patch.object(cas, "read_cache", return_value=REAL()), mock.patch.object(cas, "current_account", return_value="mt"), \
             mock.patch.object(cas, "fresh_limit_errors", return_value=[]), \
             mock.patch.object(cas.subprocess, "run", return_value=mock.Mock(returncode=0)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cas.status()
        out = buf.getvalue()
        self.assertIn("skipped after a failed switch: na until", out)
        self.assertIn("skipped until their reset (left on a limit error): mt until", out)
        self.assertIn("{'na': 1}", out, "lt's 30-day-old failures are not shown")
        self.assertIn("mt confirmed spent until", out)

    def run_status(self, st, current="mt", **patches):
        with mock.patch.object(cas, "now", return_value=AT), mock.patch.object(cas, "load_state", return_value=st), \
             mock.patch.object(cas, "read_cache", return_value=REAL()), mock.patch.object(cas, "current_account", return_value=current), \
             mock.patch.object(cas, "fresh_limit_errors", return_value=[]), \
             mock.patch.object(cas.subprocess, "run", return_value=mock.Mock(returncode=0)):
            for k, v in patches.items():
                stack = mock.patch.object(cas, k, v)
                stack.start()
                self.addCleanup(stack.stop)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cas.status()
        return buf.getvalue()

    def test_status_says_why_it_would_not_go_up_yet(self):
        # Audit of release 21: the A-21 (8) and (9) lines.
        out = self.run_status({"manual_at": iso(-3)}, ORDER=["lt", "mt", "na"], ORDER_UNKNOWN=["xx"])
        self.assertIn("unknown tags in ", out)
        self.assertIn(" ignored: xx", out)
        self.assertIn("would move to lt", out)
        self.assertIn("by hand", out)
        out = self.run_status({"last_attempt_at": iso(-1 / 60)}, ORDER=["lt", "mt", "na"])
        self.assertIn("cooldown", out)
        # Audit of release 22: leaving a spent account by hand blocks only the 30-min grace.
        out = self.run_status({"manual_at": iso(-1), "manual_left_spent": True, "last_attempt_at": iso(-1)},
                              ORDER=["lt", "mt", "na"])
        self.assertNotIn("by hand", out)
        out = self.run_status({"manual_at": iso(-0.1), "manual_left_spent": True}, ORDER=["lt", "mt", "na"])
        self.assertIn("within 30 min of a manual switch", out)
        # Audit of release 23: an account skipped on purpose is not announced.
        q = REAL()
        q["lt"] = rep(100, 100)
        out = self.run_status({"manual_at": iso(-1), "manual_left_spent": True, "manual_from": "lt"},
                              current="na", ORDER=["lt", "mt", "na"], read_cache=lambda: q)
        self.assertNotIn("would move to", out)


def UP():
    """Rebalancing on, with lt first (the Tick class turns it off by default)."""
    return mock.patch.multiple(cas, ORDER=["lt", "mt", "na"], REBALANCE_RATIO=1.5)


class Tick(unittest.TestCase):
    def setUp(self):
        # mt first: these tests are about switching down; going back up has its own tests.
        p = mock.patch.object(cas, "ORDER", ["mt", "lt", "na"])
        p.start()
        self.addCleanup(p.stop)
        # Rebalancing off unless a test is about it (REAL() would move mt, 12 % left, to na).
        p = mock.patch.object(cas, "REBALANCE_RATIO", 1e9)
        p.start()
        self.addCleanup(p.stop)
        self.state = {}
        self.switched, self.notified, self.moves = [], [], []
        self.current = "mt"
        self.elsewhere = 1
        self.cut_records = []
        patches = [
            mock.patch.object(cas, "now", return_value=AT),
            mock.patch.object(cas, "current_account", side_effect=lambda: self.current),
            mock.patch.object(cas, "load_state", side_effect=lambda: json.loads(json.dumps(self.state))),
            mock.patch.object(cas, "save_state", side_effect=self.save),
            mock.patch.object(cas, "switch_to", side_effect=lambda t: self.switched.append(t) or (True, "")),
            mock.patch.object(cas, "move_safe_tabs", side_effect=lambda scope="safe": self.moves.append(scope) or summary(("x", "continued", True))),
            mock.patch.object(cas, "tabs_elsewhere", side_effect=lambda current: self.elsewhere),
            # Hermetic: never the real cut-moves record (audit of release 24).
            mock.patch.object(cas, "CUT_MOVES", Path(tempfile.mkdtemp(prefix="cas-cm-")) / "cut-moves.json"),
            mock.patch.object(cas, "cca_record_cut", side_effect=lambda d: self.cut_records.append(d)),
            mock.patch.object(cas, "notify", side_effect=lambda *a: self.notified.append(a)),
            mock.patch.object(cas, "log"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def save(self, s):
        # Replace, not update: a key the code pops must disappear here too.
        self.state.clear()
        self.state.update(json.loads(json.dumps(s)))

    def run_tick(self, cached, fresh=None, errors=(), dry=False, assume=False):
        with mock.patch.object(cas, "read_cache", return_value=cached), \
             mock.patch.object(cas, "read_fresh", return_value=cached if fresh is None else fresh), \
             mock.patch.object(cas, "fresh_limit_errors",
                               return_value=[{"session": e, "reset": None, "text": "limit " + e,
                                              "when": AT - dt.timedelta(minutes=i)} for i, e in enumerate(errors)]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cas.tick(dry, assume)
        return code, buf.getvalue()

    def test_goes_back_up_when_a_higher_account_has_room(self):
        with UP():
            self.run_tick(REAL())
        self.assertEqual(self.switched, ["lt"])
        self.assertEqual(self.moves, ["safe-quiet"], "going up never touches tabs that may be in use (audit of release 21)")
        self.assertNotIn("mt", self.state.get("spent", {}), "mt was not spent: not skipped")
        self.assertIn("Paso de mt a lt", self.notified[0][1])
        self.assertEqual(self.notified[0][2], "info")

    def test_rebalance_at_most_every_30_minutes(self):
        self.state = {"seen": "mt", "last_switch_at": iso(-20 / 60), "last_to": "mt", "last_kind": "down"}
        self.elsewhere = 0
        with UP():
            self.run_tick(REAL())
        self.assertEqual(self.switched, [], "20 min after the last automatic switch")
        self.state["last_switch_at"] = iso(-31 / 60)
        with UP():
            self.run_tick(REAL())
        self.assertEqual(self.switched, ["lt"])

    def test_rebalance_from_the_top_of_the_priority(self):
        # the owner, 2026-11-16: lt first in the priority with 10 % of its week, na with 80 %.
        q = {"lt": rep(10, 90), "mt": rep(10, 85), "na": rep(10, 20)}
        self.current = "lt"
        self.state = {"seen": "lt"}
        with UP():
            self.run_tick(q)
        self.assertEqual(self.switched, ["na"])

    def test_bounce_after_a_rebalance_blocks_it_with_rebalancing_on(self):
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=20)).isoformat(), "last_to": "mt",
                      "last_from": "lt", "last_kind": "up", "seen": "mt"}
        self.elsewhere = 0
        with UP():
            self.run_tick(REAL(), errors=["s1"])
            self.assertEqual(self.switched, ["lt"])
            self.assertIn("mt", self.state["no_upgrade"])
            # Later, mt scoring far better is still not a rebalance target while blocked.
            q = REAL()
            q["mt"] = rep(0, 0)
            self.current = "lt"
            self.state["last_switch_at"] = iso(-2)
            self.state["last_attempt_at"] = iso(-2)
            self.state.pop("spent", None)
            self.run_tick(q)
        self.assertEqual(self.switched, ["lt"], "no way back into mt for 6 h")

    def test_paying_is_left_without_the_30_minute_throttle(self):
        q = {"lt": rep(10, 90), "mt": rep(100, 100), "na": rep(100, 40, extra_usage=True)}
        self.current = "na"
        self.state = {"seen": "na", "last_switch_at": iso(-10 / 60), "last_attempt_at": iso(-10 / 60),
                      "last_to": "na", "last_kind": "down"}
        self.elsewhere = 0
        with UP():
            self.run_tick(q)
        self.assertEqual(self.switched, ["lt"])

    def test_paying_after_a_manual_choice_is_left_after_the_grace(self):
        # Audit of 46: activated by hand with room, later billing extra usage for days.
        q = {"lt": rep(100, 40, extra_usage=True), "mt": rep(10, 30), "na": rep(10, 30)}
        self.current = "lt"
        self.state = {"seen": "lt", "manual_at": iso(-0.2), "manual_from": "na", "manual_left_spent": False}
        self.elsewhere = 0
        with UP():
            self.run_tick(q)
            self.assertEqual(self.switched, [], "within the 30-min grace")
            self.state["manual_at"] = iso(-3)
            self.run_tick(q)
        self.assertIn(self.switched[0], ("mt", "na"), "accounts below manual_from are not skipped while paying")

    def test_forced_read_ending_paid_usage_restores_the_manual_rules(self):
        # Audit of 47: pay decided on the cache waived the manual rule; the forced read shows
        # no paid usage any more, so this tick does nothing.
        cached = {"lt": rep(100, 40, extra_usage=True, age_min=8), "mt": rep(10, 30, age_min=8), "na": rep(10, 30, age_min=8)}
        fresh = {"lt": rep(20, 40), "mt": rep(10, 30), "na": rep(10, 30)}
        self.current = "lt"
        self.state = {"seen": "lt", "manual_at": iso(-3), "manual_from": "na", "manual_left_spent": False}
        self.elsewhere = 0
        with UP():
            self.run_tick(cached, fresh=fresh)
        self.assertEqual(self.switched, [])

    def test_no_going_up_right_after_a_manual_switch(self):
        self.state = {"seen": "lt"}  # the owner just moved to mt by hand
        with UP():
            self.run_tick(REAL())
        self.assertEqual(self.switched, [])

    def test_no_signal_no_switch(self):
        self.run_tick(REAL())
        self.assertEqual(self.switched, [])

    def test_limit_error_switches_to_the_best_and_moves_tabs(self):
        q = REAL()
        self.run_tick(q, errors=["s1"])
        self.assertEqual(self.switched, ["lt"])
        self.assertEqual(self.moves, ["safe-quiet"], "a switch down never closes a tab that may be in use (audit of release 27)")
        self.assertEqual(self.state["last_to"], "lt")
        self.assertIn("mt", self.state["spent"], "the account left is skipped until its reset")
        self.assertIn("Pestañas que continúan: 1", self.notified[0][1])
        self.assertNotIn("se moverán al terminar", self.notified[0][1])

    def test_notice_mentions_tabs_paused_on_an_old_error(self):
        with mock.patch.object(cas, "move_safe_tabs", return_value=summary(("old", "old_error", False))):
            self.run_tick(REAL(), errors=["s1"])
        self.assertIn("1 movida(s) con un error de hace más de 8 h", self.notified[0][1])

    def test_notice_reads_the_summary_not_tab_names(self):
        # Audits of auto switch releases 13 and 15: names with "continuing" or colons.
        out = summary(("rebase continuing after fix", "finished", False),
                      ("DEMO-123: Fix the gates", "not_taken", True),
                      ("home.local: branding", "not_reopened", False))
        with mock.patch.object(cas, "move_safe_tabs", return_value=out):
            self.run_tick(REAL(), errors=["s1"])
        body = self.notified[0][1]
        self.assertIn("Pestañas que continúan: 0", body)
        self.assertIn("1 cortada(s) que no han podido seguir solas", body)
        self.assertIn("DEMO-123: Fix the gates", body)
        self.assertNotIn("home.local", body, "a finished tab is never asked to continue")

    def test_summary_survives_unicode_line_separators(self):
        # Audit of auto switch release 16.
        line = "MOVE-SUMMARY " + json.dumps([{"name": "notes\u2028more", "outcome": "continued", "cut": True}],
                                            ensure_ascii=False)
        self.assertEqual(cas.move_summary("x\n" + line)[0]["outcome"], "continued")

    def test_move_that_did_not_finish_is_reported(self):
        with mock.patch.object(cas, "move_safe_tabs", return_value="Traceback (most recent call last):\n boom"):
            self.run_tick(REAL(), errors=["s1"])
        self.assertIn("El movimiento de pestañas no terminó bien", self.notified[0][1])

    def test_follow_up_logs_moves_and_failures_only(self):
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=30)).isoformat(), "last_to": "mt", "seen": "mt"}
        for out, logged in ((summary(), False), (summary(("a", "left", False)), False),
                            (summary(("b", "not_reopened", True)), True), ("Traceback", True)):
            with mock.patch.object(cas, "move_safe_tabs", return_value=out), mock.patch.object(cas, "log") as log:
                self.run_tick(REAL())
            self.assertEqual(log.called, logged, out)

    def test_counter_needs_fresh_numbers(self):
        q = REAL()
        q["mt"] = rep(100, 88, age_min=90)
        self.run_tick(q, fresh={})
        self.assertEqual(self.switched, [], "hour-old numbers and no fresh read")
        q["mt"] = rep(100, 88)
        self.state.pop("last_fresh_at", None)
        self.run_tick(q)
        self.assertEqual(self.switched, ["lt"])

    def test_counter_alone_needs_a_forced_read_to_confirm(self):
        # Audit of auto switch release 2: a cached "full" with the forced read failing.
        q = REAL()
        q["mt"] = rep(100, 88)
        self.run_tick(q, fresh={})
        self.assertEqual(self.switched, [])

    def test_broken_login_crit_once_and_backoff(self):
        # Audit of auto switch release 4: 12 h with a broken target login sent 12 crits.
        q = {"lt": rep(100, 100), "mt": rep(20, 20), "na": rep(100, 100)}
        self.current = "lt"
        t = AT
        tries = []
        for hour in range(12):
            t = AT + dt.timedelta(hours=hour)
            q2 = json.loads(json.dumps(q))
            for r in q2.values():
                r["fetched_at"] = (t - dt.timedelta(minutes=1)).isoformat()
            with mock.patch.object(cas, "now", return_value=t), \
                 mock.patch.object(cas, "switch_to", side_effect=lambda tg: tries.append(tg) or (False, "login check failed: x")):
                self.run_tick(q2, errors=["s1"])
        crits = [n for n in self.notified if n[2] == "crit"]
        self.assertEqual(len(crits), 1)
        self.assertLessEqual(len(tries), 4, "1, 2, 4, 8 h waits: at most 4 tries in 12 h")

    def test_failure_memory_expires(self):
        # Audit of auto switch release 5: a notice from weeks ago hid a new breakage, and
        # the count kept growing (overflowing at 36).
        self.state = {"fail_counts": {"na": 40}, "fail_notices": {"na": "login check failed"},
                      "last_fail_at": {"na": iso(-24 * 20)}}
        q = {"lt": rep(100, 100), "mt": rep(100, 100), "na": rep(20, 20)}
        self.current = "lt"
        with mock.patch.object(cas, "switch_to", return_value=(False, "login check failed: x")):
            self.run_tick(q, errors=["s1"])
        self.assertEqual([n[2] for n in self.notified].count("crit"), 1)
        self.assertEqual(self.state["fail_counts"]["na"], 1)

    def test_release5_state_without_failure_times_is_cleaned(self):
        # Audit of auto switch release 6.
        self.state = {"fail_counts": {"na": 6}, "fail_notices": {"na": "login check failed"}}
        self.run_tick(REAL())
        self.assertEqual(self.state.get("fail_counts"), {})
        self.assertEqual(self.state.get("fail_notices"), {})

    def test_failure_state_saved_before_the_notice(self):
        saved_at_notice = []
        self.notified.clear()
        q = REAL()
        with mock.patch.object(cas, "switch_to", return_value=(False, "login check failed: x")), \
             mock.patch.object(cas, "notify", side_effect=lambda *a: saved_at_notice.append(dict(self.state))):
            self.run_tick(q, errors=["s1"])
        self.assertIn("failed", saved_at_notice[0], "the skip was saved before notifying")

    def test_backoff_never_overflows(self):
        self.state = {"fail_counts": {"lt": 60}, "last_fail_at": {"lt": iso(-1)}}
        q = REAL()
        with mock.patch.object(cas, "switch_to", return_value=(False, "login check failed: x")):
            code, _ = self.run_tick(q, errors=["s1"])
        self.assertEqual(code, 1)
        self.assertLessEqual(cas.parse_time(self.state["failed"]["lt"]), AT + dt.timedelta(hours=24))

    def test_spent_window_stays_confirmed_until_its_reset(self):
        # Audit of auto switch release 4: a long no-room stretch only has the counter.
        q = REAL()
        q["mt"] = rep(100, 88, session_reset_h=3)
        self.state = {"spent_confirmed": {"tag": "mt", "until": iso(3)},
                      "last_fresh_at": (AT - dt.timedelta(seconds=30)).isoformat()}
        self.run_tick(q, fresh={})
        self.assertEqual(self.switched, ["lt"], "confirmed earlier, its reset is still ahead")

    def test_failed_target_is_skipped_next_time(self):
        # Audit of auto switch release 2: lt's login is broken, na has room.
        q = REAL()
        q["mt"] = rep(100, 88)
        with mock.patch.object(cas, "switch_to", side_effect=lambda t: self.switched.append(t) or (False, "login")):
            self.run_tick(q)
        self.state["last_attempt_at"] = (AT - dt.timedelta(minutes=6)).isoformat()
        self.state.pop("last_fresh_at", None)
        self.run_tick(q)
        self.assertEqual(self.switched, ["lt", "na"])

    def test_fresh_read_can_cancel_the_counter(self):
        cached = REAL()
        cached["mt"] = rep(100, 88)
        fresh = REAL()
        fresh["mt"] = rep(100, 88, session_reset_h=-0.1)
        self.run_tick(cached, fresh=fresh)
        self.assertEqual(self.switched, [])

    def test_cooldown_counts_failed_attempts_too(self):
        q = REAL()
        q["mt"] = rep(100, 88)
        with mock.patch.object(cas, "switch_to", return_value=(False, "login check failed")):
            code, _ = self.run_tick(q)
        self.assertEqual(code, 1)
        self.assertEqual(self.notified[-1][2], "crit", "notify.sh knows info|ok|warn|crit")
        self.run_tick(q)
        self.assertEqual(self.switched, [], "the next tick waits COOLDOWN")

    def test_no_room_anywhere_notifies_once_despite_reset_noise(self):
        # Audit of release 1: reset times carry sub-second noise that changed on every read.
        q1 = {"lt": rep(100, 18), "mt": rep(100, 88), "na": rep(95, 53)}
        q2 = json.loads(json.dumps(q1))
        for t in q2:
            for k in ("session", "weekly"):
                q2[t][k]["resets_at"] = q2[t][k]["resets_at"].replace("+00:00", ".123456+00:00")
        self.run_tick(q1)
        self.run_tick(q2)
        self.assertEqual(self.switched, [])
        self.assertEqual(len(self.notified), 1)
        self.assertIn("ninguna otra cuenta tiene margen", self.notified[0][1])

    def test_no_room_notice_survives_resets_across_a_minute(self):
        # Audit of auto switch release 3: 22:29:59.98 and 22:30:00.41 in real reads.
        q1 = {"lt": rep(100, 18), "mt": rep(100, 88), "na": rep(95, 53)}
        q2 = json.loads(json.dumps(q1))
        q1["lt"]["session"]["resets_at"] = "2026-11-12T22:29:59.980000+00:00"
        q2["lt"]["session"]["resets_at"] = "2026-11-12T22:30:00.410000+00:00"
        self.run_tick(q1)
        self.state.pop("last_fresh_at", None)
        self.run_tick(q2)
        self.assertEqual(len(self.notified), 1)

    def test_failed_read_does_not_confirm(self):
        # Audit of auto switch release 3: a failed forced read 59 s earlier used to count.
        q = REAL()
        q["mt"] = rep(100, 88)
        self.run_tick(q, fresh={})
        self.state["last_fresh_at"] = (AT - dt.timedelta(seconds=59)).isoformat()
        self.run_tick(q, fresh={})
        self.assertEqual(self.switched, [])

    def test_all_targets_failed_says_so(self):
        q = REAL()
        q["mt"] = rep(100, 88)
        self.state = {"failed": {"lt": iso(1), "na": iso(1)}}
        self.run_tick(q, errors=["s1"])
        self.assertIn("falló (¿login caducado?", self.notified[0][1])
        self.assertIn("Próximo intento a las", self.notified[0][1])
        self.assertNotIn("ninguna otra cuenta tiene margen", self.notified[0][1])

    def test_no_target_slows_forced_reads(self):
        q = {"lt": rep(100, 18), "mt": rep(100, 88), "na": rep(95, 53)}
        reads = []
        with mock.patch.object(cas, "read_cache", return_value=q), \
             mock.patch.object(cas, "read_fresh", side_effect=lambda: reads.append(1) or q), \
             mock.patch.object(cas, "fresh_limit_errors", return_value=[]):
            cas.tick(False, False)
            self.state["last_fresh_at"] = (AT - dt.timedelta(minutes=2)).isoformat()
            cas.tick(False, False)
        self.assertEqual(len(reads), 1, "no account to switch to: no forced read every minute")

    def test_slow_pace_lasts_while_no_target(self):
        # Audit of auto switch release 8: from minute 30 it went back to a read a minute.
        q = {"lt": rep(100, 18), "mt": rep(100, 88), "na": rep(95, 53)}
        reads = []
        with mock.patch.object(cas, "read_cache", return_value=q), \
             mock.patch.object(cas, "fresh_limit_errors", return_value=[]):
            for minute in range(0, 60):
                t = AT + dt.timedelta(minutes=minute)
                q2 = json.loads(json.dumps(q))
                for r in q2.values():
                    r["fetched_at"] = (t - dt.timedelta(seconds=30)).isoformat()
                with mock.patch.object(cas, "now", return_value=t), \
                     mock.patch.object(cas, "read_cache", return_value=q2), \
                     mock.patch.object(cas, "read_fresh", side_effect=lambda q2=q2: reads.append(1) or q2):
                    cas.tick(False, False)
        self.assertLessEqual(len(reads), 7, "about one forced read per 10 minutes for an hour")

    def test_no_target_cleared_when_no_longer_spent_or_after_a_switch(self):
        self.state = {"no_target": True}
        self.run_tick(REAL())
        self.assertNotIn("no_target", self.state, "not spent any more")
        self.state = {"no_target": True}
        q = REAL()
        q["mt"] = rep(100, 88)
        self.run_tick(q, errors=["s1"])
        self.assertEqual(self.switched, ["lt"])
        self.assertNotIn("no_target", self.state, "cleared by the switch")

    def test_nodata_then_noroom_notifies_twice(self):
        # Audit of auto switch release 9, test fixed after the audit of release 10: same
        # numbers and reset times, the other accounts stale first, then live.
        q_live = {"lt": rep(100, 18), "mt": rep(100, 88), "na": rep(95, 53)}
        q_stale = json.loads(json.dumps(q_live))
        q_stale["lt"]["source"] = q_stale["na"]["source"] = "stale"
        self.run_tick(q_stale, errors=["s1"])
        self.state.pop("last_fresh_at", None)
        self.run_tick(q_live, errors=["s1"])
        self.assertEqual(len(self.notified), 2)
        self.assertIn("no tengo datos", self.notified[0][1])
        self.assertIn("ninguna otra cuenta tiene margen", self.notified[1][1])

    def test_no_follow_up_into_a_spent_account(self):
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=30)).isoformat(), "last_to": "mt",
                      "seen": "mt", "no_target": True}
        self.run_tick(REAL())
        self.assertEqual(self.moves, [])

    def test_stale_and_live_reads_alternating_notify_once(self):
        q_live = {"lt": rep(100, 18), "mt": rep(100, 88), "na": rep(95, 53)}
        q_stale = json.loads(json.dumps(q_live))
        q_stale["lt"]["source"] = q_stale["na"]["source"] = "stale"
        for q in (q_live, q_stale, q_live, q_stale):
            self.run_tick(q, errors=["s1"])
            self.state.pop("last_fresh_at", None)
        self.assertEqual(len(self.notified), 1)

    def test_no_numbers_says_so(self):
        q = {"mt": rep(100, 88)}
        self.run_tick(q, fresh={}, errors=["s1"])
        self.assertIn("no tengo datos de cuota recientes", self.notified[0][1])

    def test_manual_switch_back_to_the_last_target_is_manual_too(self):
        # Audit of auto switch release 2: lt -> mt automatic, then the owner goes na, then mt.
        self.state = {"seen": "na", "last_to": "mt", "last_switch_at": (AT - dt.timedelta(minutes=50)).isoformat()}
        q = REAL()
        q["mt"] = rep(100, 88)
        self.run_tick(q)
        self.assertEqual(self.switched, [])
        self.assertIn("manual_at", self.state)
        self.assertEqual(self.moves, [], "no follow-up move after a manual switch")

    def test_manual_switch_is_respected_for_a_while(self):
        # Audit of release 1: the owner moves to a spent account on purpose.
        self.state = {"seen": "lt", "last_to": "lt"}
        q = REAL()
        q["mt"] = rep(100, 88)
        self.run_tick(q)
        self.assertEqual(self.switched, [], "counter only, right after a manual switch")
        self.run_tick(q, errors=["s1"])
        self.assertEqual(self.switched, ["lt"], "a real limit error still counts")

    def test_dry_run_changes_nothing(self):
        _, out = self.run_tick(REAL(), dry=True, assume=True)
        self.assertEqual(self.switched, [])
        self.assertNotIn("last_switch_at", self.state)
        self.assertIn("would switch mt", out)
        self.assertIn("-> lt", out)

    def test_follow_up_moves_tabs_left_behind(self):
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=30)).isoformat(), "last_to": "mt", "seen": "mt"}
        self.run_tick(REAL())
        self.assertEqual(self.moves, ["safe-quiet"], "follow-up moves tabs stopped on the limit or quiet for an hour")
        self.assertEqual(self.switched, [])
        # Audit of release 19: no time limit while tabs are elsewhere; nothing when none are.
        self.state = {"last_switch_at": (AT - dt.timedelta(hours=3)).isoformat(), "last_to": "mt", "seen": "mt"}
        self.moves.clear()
        self.run_tick(REAL())
        self.assertEqual(self.moves, ["safe-quiet"])
        self.moves.clear()
        self.elsewhere = 0
        self.run_tick(REAL())
        self.assertEqual(self.moves, [])

    def test_follow_up_after_going_up_moves_quiet_tabs(self):
        # Audit of release 19: the account left has room, so its busy tabs never stop on the
        # limit; they move once idle for a while.
        self.state = {"last_switch_at": (AT - dt.timedelta(hours=5)).isoformat(), "last_to": "mt",
                      "seen": "mt", "last_kind": "up"}
        self.run_tick(REAL())
        self.assertEqual(self.moves, ["safe-quiet"])

    def test_upgrade_moves_with_safe_and_says_where_tabs_stay(self):
        with UP(), \
             mock.patch.object(cas, "move_safe_tabs", return_value=summary(("busy", "left", False))):
            self.run_tick(REAL())
        self.assertEqual(self.state["last_kind"], "up")
        self.assertIn("siguen en mt", self.notified[0][1])
        self.assertIn("una hora", self.notified[0][1])
        self.assertIn("las de estado desconocido, con claude-cursor-move", self.notified[0][1],
                      "only unknown-state tabs by hand: moving own work kills it (audits of releases 33, 34)")
        self.assertNotIn("se corten", self.notified[0][1])

    def test_no_going_up_after_a_manual_switch_until_an_automatic_one(self):
        # Audit of release 19: a shell switch on purpose was undone after the 30-min grace.
        self.state = {"seen": "mt", "manual_at": (AT - dt.timedelta(hours=3)).isoformat()}
        with UP():
            self.run_tick(REAL())
        self.assertEqual(self.switched, [])

    def test_leaving_a_spent_account_by_hand_still_goes_back_up(self):
        # Audit of release 21: a shell switch typed because lt ran out must not block going back
        # to lt for days.
        with UP():
            self.state = {"seen": "lt", "seen_at": iso(-1 / 60), "spent": {"lt": iso(0.5)}}
            self.run_tick(REAL())
            self.assertTrue(self.state["manual_left_spent"])
            self.assertEqual(self.switched, [], "the 30-min grace still holds")
            self.state["manual_at"] = iso(-1)
            self.state.pop("spent")
            self.run_tick(REAL())
        self.assertEqual(self.switched, ["lt"])
        self.assertNotIn("manual_left_spent", self.state)

    def test_leaving_an_account_with_room_by_hand_is_detected(self):
        with UP():
            self.state = {"seen": "lt", "seen_at": iso(-1 / 60)}
            with mock.patch.object(cas, "recent_limit_error", return_value=False):
                self.run_tick(REAL())
        self.assertFalse(self.state["manual_left_spent"])

    def test_manual_move_of_a_cut_tab_still_counts_as_leaving_a_spent_account(self):
        # Audit of release 22: a shell switch moved the cut tab before the tick, so only its
        # transcript shows lt ran out.
        with UP(), \
             mock.patch.object(cas, "recent_limit_error", return_value=True):
            self.state = {"seen": "lt", "seen_at": iso(-1 / 60)}
            self.run_tick(REAL())
        self.assertTrue(self.state["manual_left_spent"])

    def test_a_switch_made_while_the_tool_was_off_is_a_choice(self):
        with UP(), \
             mock.patch.object(cas, "recent_limit_error", return_value=True):
            self.state = {"seen": "lt", "seen_at": iso(-3)}
            self.run_tick(REAL())
        self.assertFalse(self.state["manual_left_spent"])

    def test_skipped_account_stays_skipped_after_a_manual_move(self):
        # Audit of release 22: lt spent, the owner jumps to na on purpose: no going up to mt.
        self.current = "na"
        with UP():
            self.state = {"seen": "na", "manual_at": iso(-1), "manual_left_spent": True, "manual_from": "lt",
                          "spent": {"lt": iso(2)}}
            self.run_tick(REAL())
        self.assertEqual(self.switched, [])
        self.state = {"seen": "na", "manual_at": iso(-1), "manual_left_spent": True, "manual_from": "mt",
                      "spent": {"lt": iso(2)}}
        q = REAL()
        q["mt"] = rep(10, 10)  # 90 % of the week left: worth leaving na (47 %) for
        with UP():
            self.run_tick(q)
        self.assertEqual(self.switched, ["mt"], "mt is the one he left")

    def test_up_and_down_failures_do_not_renotify_each_other(self):
        # Audit of release 22: crit, warn, crit for one broken login.
        with mock.patch.object(cas, "switch_to", return_value=(False, "verify: not logged in")):
            self.run_tick(REAL(), errors=["s1"])  # down mt -> lt fails
            self.state["last_attempt_at"] = iso(-1)
            self.state.pop("failed")
            with UP():
                self.run_tick(REAL())  # up mt -> lt fails
            self.state["last_attempt_at"] = iso(-1)
            self.state.pop("failed")
            self.run_tick(REAL(), errors=["s1"])
        self.assertEqual([n[2] for n in self.notified], ["crit", "warn"])

    def test_seen_at_is_saved_every_tick_and_after_a_long_follow_up(self):
        # Audit of release 23: tests always gave seen_at; and a 20-minute move left it stale.
        self.state = {"seen": "mt"}
        self.elsewhere = 0
        self.run_tick(REAL())
        self.assertEqual(self.state["seen_at"], AT.isoformat())
        self.elsewhere = 1
        self.state.update(last_switch_at=iso(-1), last_to="mt")
        later = AT + dt.timedelta(minutes=20)
        with mock.patch.object(cas, "now", side_effect=[AT] + [later] * 5):
            self.run_tick(REAL())
        self.assertEqual(self.state["seen_at"], later.isoformat())

    def test_success_clears_the_upgrade_failure_and_the_manual_origin(self):
        self.state = {"seen": "mt", "fail_notices": {"lt": "verify", "lt:up": "verify"},
                      "last_fail_at": {"lt": iso(-1)}, "manual_from": "lt"}
        self.run_tick(REAL(), errors=["s1"])
        self.assertEqual(self.switched, ["lt"])
        self.assertEqual(self.state["fail_notices"], {})
        self.assertNotIn("manual_from", self.state)

    def test_down_notice_names_the_quiet_time(self):
        with mock.patch.object(cas, "move_safe_tabs", return_value=summary(("busy", "left", False))):
            self.run_tick(REAL(), errors=["s1"])
        # Audits of releases 28-31: what happens when he writes in one, the quiet time,
        # own work needs claude-cursor-move, and short.
        body = self.notified[0][1]
        self.assertIn("siguen en mt", body)
        self.assertIn("dará el límite y se moverá sola con continúa", body)
        self.assertIn(cas.QUIET_TEXT, body)
        self.assertIn("trabajo propio, cuando termine; las de estado desconocido, con claude-cursor-move", body)
        self.assertLess(len(body), 400)

    def test_follow_up_notifies_a_cut_tab_that_could_not_carry_on_once(self):
        # Audit of release 28: the follow-up only logged.
        self.state = {"last_switch_at": iso(-0.5), "last_to": "mt", "seen": "mt"}
        out = summary(("Fix the gates", "not_taken", True), ("other", "continued", True),
                      ("idle uncut", "not_reopened", False))
        with mock.patch.object(cas, "move_safe_tabs", return_value=out):
            self.run_tick(REAL())
            self.run_tick(REAL())
        self.assertEqual(len(self.notified), 1)
        self.assertIn("Fix the gates", self.notified[0][1])
        self.assertNotIn("other", self.notified[0][1])
        self.assertNotIn("idle uncut", self.notified[0][1], "only cut tabs need continúa (audit of release 29)")
        # Audit of release 29: a later switch is a new episode, the same tab is told again.
        self.state["last_switch_at"] = iso(-0.1)
        with mock.patch.object(cas, "move_safe_tabs", return_value=out):
            self.run_tick(REAL())
        self.assertEqual(len(self.notified), 2)

    def test_limit_errors_of_the_current_account_are_recorded(self):
        # Audit of release 24: a terminal claude cut on the limit has no move to record it.
        # Newest in the middle: neither errors[0] nor errors[-1] passes (audit of release 28).
        older_first = [{"session": "s2", "reset": None, "text": "limit s2", "when": AT - dt.timedelta(minutes=3)},
                       {"session": "s1", "reset": None, "text": "limit s1", "when": AT},
                       {"session": "s3", "reset": None, "text": "limit s3", "when": AT - dt.timedelta(minutes=5)}]
        with mock.patch.object(cas, "fresh_limit_errors", return_value=older_first), \
             mock.patch.object(cas, "read_cache", return_value=REAL()), \
             mock.patch.object(cas, "read_fresh", return_value=REAL()), redirect_stdout(io.StringIO()):
            cas.tick(False, False)
        self.assertEqual(self.cut_records, [{"mt": {"at": AT.isoformat(), "text": "limit s1"}}], "the newest error")

    def test_dry_run_records_nothing(self):
        self.run_tick(REAL(), errors=["s1"], dry=True)
        self.assertEqual(self.cut_records, [])

    def test_manual_move_reads_the_record_of_the_account_left(self):
        # Audit of release 24: the real per-account wiring, not a mocked answer.
        with UP():
            for record, want in (({"lt": iso(-0.05)}, True), ({"mt": iso(-0.05)}, False)):
                cas.CUT_MOVES.write_text(json.dumps(record))
                self.state = {"seen": "lt", "seen_at": iso(-1 / 60)}
                self.run_tick(REAL())
                self.assertEqual(self.state["manual_left_spent"], want, record)

    def test_switch_renews_seen_at(self):
        later = AT + dt.timedelta(minutes=20)
        self.elsewhere = 0
        with mock.patch.object(cas, "now", side_effect=[AT] + [later] * 5):
            self.run_tick(REAL(), errors=["s1"])
        self.assertEqual(self.switched, ["lt"])
        self.assertEqual(self.state["seen_at"], later.isoformat())

    def test_a_manual_switch_during_the_tick_is_never_overwritten(self):
        # Audit of release 38: the default changed between the read and the switch.
        seq = iter(["mt"] + ["na"] * 20)  # read at the start, then changed
        with mock.patch.object(cas, "current_account", side_effect=lambda: next(seq)):
            self.run_tick(REAL(), errors=["s1"])
        self.assertEqual(self.switched, [])

    def test_a_manual_switch_during_the_follow_up_ends_the_tick(self):
        # Audit of release 39: nothing more runs on the account he just left.
        self.state = {"last_switch_at": iso(-0.5), "last_to": "mt", "seen": "mt"}
        seq = iter(["mt"] + ["na"] * 20)
        with mock.patch.object(cas, "current_account", side_effect=lambda: next(seq)), \
             mock.patch.object(cas, "read_fresh") as fresh:
            self.run_tick(REAL(), errors=["s1"])
        self.assertEqual(self.moves, ["safe-quiet"])
        self.assertEqual(self.cut_records, [], "no cut recorded against the account left")
        self.assertEqual(self.switched, [])
        fresh.assert_not_called()

    def test_migration_ignores_a_switch_down_to_a_higher_account(self):
        self.state = {"last_switch_at": iso(-0.3), "last_from": "na", "last_to": "mt", "seen": "mt",
                      "spent": {"na": iso(2)}}
        self.elsewhere = 0
        self.run_tick(REAL())
        self.assertNotIn("last_kind", self.state)

    def test_old_state_without_last_kind_is_migrated(self):
        # Audit of release 21: an upgrade recorded by release 19 / 20 was followed up as a down.
        self.state = {"last_switch_at": iso(-3), "last_from": "na", "last_to": "mt", "seen": "mt"}
        self.run_tick(REAL())  # Tick ORDER is mt, lt, na: na -> mt was up
        self.assertEqual(self.moves, ["safe-quiet"])

    def test_upgrade_only_reads_accounts_it_could_choose(self):
        # Audit of release 21: a stale lt that is skipped anyway cost a read every 10 min.
        self.current = "na"
        q = REAL()
        q["lt"] = rep(3, 18, age_min=90)
        q["mt"] = rep(10, 10)
        with UP(), \
             mock.patch.object(cas, "read_fresh", return_value=q) as fresh, \
             mock.patch.object(cas, "read_cache", return_value=q), \
             mock.patch.object(cas, "fresh_limit_errors", return_value=[]), redirect_stdout(io.StringIO()):
            self.state = {"spent": {"lt": iso(2)}, "seen": "na"}
            cas.tick(False, False)
        self.assertEqual(fresh.call_count, 0)
        self.assertEqual(self.switched, ["mt"])

    def test_cooldown_and_skip_lists_block_going_up(self):
        with UP():
            self.state = {"last_attempt_at": (AT - dt.timedelta(minutes=2)).isoformat(), "seen": "mt"}
            self.run_tick(REAL())
            self.assertEqual(self.switched, [], "cooldown")
            for key in ("spent", "failed", "no_upgrade"):
                # na (47 % of the week) also beats mt (12 %): skip both.
                self.state = {key: {"lt": iso(1), "na": iso(1)}, "seen": "mt"}
                self.run_tick(REAL())
                self.assertEqual(self.switched, [], key)

    def test_going_up_needs_a_forced_read(self):
        # Audit of release 19: cached numbers 10 minutes old and the forced read failing.
        q = REAL()
        q["lt"]["fetched_at"] = iso(-10 / 60)
        with UP():
            self.run_tick(q, fresh={})
        self.assertEqual(self.switched, [])

    def test_honest_spend_or_late_fall_does_not_block_going_up(self):
        # Audit of release 21: only a limit the numbers did not show is a bounce. Audit of
        # release 22: judged on the forced read, the cache is older than the hit.
        q = REAL()
        q["mt"] = rep(97, 88, age_min=5)
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=20)).isoformat(), "last_to": "mt",
                      "last_kind": "up", "seen": "mt"}
        self.elsewhere = 0
        fresh = REAL()
        fresh["mt"] = rep(100, 88)
        self.run_tick(q, fresh=fresh, errors=["s1"])
        self.assertEqual(self.state.get("no_upgrade"), {}, "the forced read showed it spent")
        self.switched.clear()
        q = REAL()
        q["mt"] = rep(100, 88)
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=20)).isoformat(), "last_to": "mt",
                      "last_kind": "up", "seen": "mt"}
        self.elsewhere = 0
        self.run_tick(q, errors=["s1"])
        self.assertEqual(self.state.get("no_upgrade"), {}, "the counter showed it spent")
        self.switched.clear()
        self.state = {"last_switch_at": (AT - dt.timedelta(hours=2)).isoformat(), "last_to": "mt",
                      "last_kind": "up", "seen": "mt"}
        self.run_tick(REAL(), errors=["s1"])
        self.assertEqual(self.state.get("no_upgrade"), {}, "more than an hour after going up")

    def test_down_soon_after_going_up_blocks_going_up_again(self):
        # Audit of release 19: a limit the numbers do not show made it bounce every reset.
        self.state = {"last_switch_at": (AT - dt.timedelta(minutes=20)).isoformat(), "last_to": "mt",
                      "last_kind": "up", "seen": "mt"}
        self.elsewhere = 0
        self.run_tick(REAL(), errors=["s1"])
        self.assertEqual(self.switched, ["lt"])
        self.assertIn("mt", self.state["no_upgrade"])
        self.assertEqual(self.state["last_kind"], "down")

    def test_failed_upgrade_is_a_warning(self):
        with UP(), \
             mock.patch.object(cas, "switch_to", return_value=(False, "verify: not logged in")):
            self.run_tick(REAL())
        self.assertEqual(self.notified[0][2], "warn")
        self.assertIn("Sigo en mt", self.notified[0][1])
        self.assertEqual(self.state["fail_notices"], {"lt:up": "verify"}, "a later needed switch keeps its crit")


class Enable(unittest.TestCase):
    def run_enable(self, st, off: bool):
        saved = {}
        tmp = Path(tempfile.mkdtemp(prefix="cas-en-"))
        if off:
            (tmp / "off").write_text("")
        with mock.patch.object(cas, "OFF", tmp / "off"), mock.patch.object(cas, "LOCK", tmp / "lock"), \
             mock.patch.object(cas, "load_state", return_value=dict(st)), \
             mock.patch.object(cas, "save_state", side_effect=saved.update), \
             mock.patch.object(cas.sys, "argv", ["x", "--enable"]), redirect_stdout(io.StringIO()):
            cas.main()
        return saved, (tmp / "off").exists()

    def test_enable_forgets_switches_made_while_off(self):
        st = {"seen": "lt", "seen_at": iso(-5), "manual_at": iso(-4), "spent": {"na": iso(1)}}
        saved, still_off = self.run_enable(st, off=True)
        self.assertEqual(saved, {"spent": {"na": iso(1)}})
        self.assertFalse(still_off)

    def test_enable_while_on_keeps_a_manual_choice(self):
        # Audit of release 23: it wiped a manual choice even when the tool was running.
        # Audit of release 24: assert nothing was written at all (saved == {} also held
        # when everything was popped).
        tmp = Path(tempfile.mkdtemp(prefix="cas-en-"))
        with mock.patch.object(cas, "OFF", tmp / "off"), mock.patch.object(cas, "LOCK", tmp / "lock"), \
             mock.patch.object(cas, "load_state", return_value={"seen": "mt", "manual_at": iso(-1)}), \
             mock.patch.object(cas, "save_state") as save, \
             mock.patch.object(cas.sys, "argv", ["x", "--enable"]), redirect_stdout(io.StringIO()):
            cas.main()
        save.assert_not_called()

    def test_enable_waits_for_a_running_tick(self):
        import threading
        tmp = Path(tempfile.mkdtemp(prefix="cas-en-"))
        (tmp / "off").write_text("")
        held = (tmp / "lock").open("a")
        cas.fcntl.flock(held, cas.fcntl.LOCK_EX)
        out, done = io.StringIO(), threading.Event()
        def enable():
            with mock.patch.object(cas, "OFF", tmp / "off"), mock.patch.object(cas, "LOCK", tmp / "lock"), \
                 mock.patch.object(cas, "load_state", return_value={}), mock.patch.object(cas, "save_state"), \
                 mock.patch.object(cas.sys, "argv", ["x", "--enable"]), redirect_stdout(out):
                cas.main()
            done.set()
        t = threading.Thread(target=enable)
        t.start()
        self.assertFalse(done.wait(0.5), "it waits while a tick holds the lock")
        self.assertTrue((tmp / "off").exists())
        held.close()
        self.assertTrue(done.wait(5))
        t.join()
        self.assertIn("waiting for the running tick", out.getvalue())
        self.assertFalse((tmp / "off").exists())

    def test_enable_says_nothing_about_waiting_when_free(self):
        tmp = Path(tempfile.mkdtemp(prefix="cas-en-"))
        (tmp / "off").write_text("")
        out = io.StringIO()
        with mock.patch.object(cas, "OFF", tmp / "off"), mock.patch.object(cas, "LOCK", tmp / "lock"), \
             mock.patch.object(cas, "load_state", return_value={}), mock.patch.object(cas, "save_state"), \
             mock.patch.object(cas.sys, "argv", ["x", "--enable"]), redirect_stdout(out):
            cas.main()
        self.assertNotIn("waiting", out.getvalue())


class RecentLimitError(unittest.TestCase):
    def test_per_account_record_of_moved_cut_tabs(self):
        # Audit of release 23: an error of lt must not make a move off mt look spent.
        path = Path(tempfile.mkdtemp(prefix="cas-rl-")) / "cut-moves.json"
        path.write_text(json.dumps({"lt": iso(-0.1)}))
        with mock.patch.object(cas, "CUT_MOVES", path):
            self.assertTrue(cas.recent_limit_error("lt", AT))
            self.assertFalse(cas.recent_limit_error("mt", AT))
            self.assertFalse(cas.recent_limit_error("lt", AT + dt.timedelta(hours=1)), "too old")
            self.assertFalse(cas.recent_limit_error("lt", AT - dt.timedelta(hours=1)), "from the future")
        with mock.patch.object(cas, "CUT_MOVES", path.parent / "none.json"):
            self.assertFalse(cas.recent_limit_error("lt", AT))


class TabsElsewhere(unittest.TestCase):
    def test_counts_live_cursor_sessions_of_other_accounts(self):
        # Audit of release 21: the real function, not a mock.
        root = Path(tempfile.mkdtemp(prefix="cas-te-"))
        accounts = {t: root / t for t in ("lt", "mt", "na")}
        for t, d in accounts.items():
            (d / "sessions").mkdir(parents=True)
        def put(t, pid, entry="claude-vscode", kind="interactive"):
            (accounts[t] / "sessions" / f"{pid}.json").write_text(json.dumps(
                {"pid": pid, "sessionId": f"s{pid}", "kind": kind, "entrypoint": entry, "startedAt": 1}))
        put("lt", 1); put("mt", 2); put("mt", 3, entry="cli"); put("na", 4, kind="sdk"); put("na", 5)
        with mock.patch.dict(cas.ACCOUNTS, accounts), \
             mock.patch.object(cas.cqr, "pid_alive", side_effect=lambda p: p != 5):
            self.assertEqual(cas.tabs_elsewhere("lt"), 1, "terminal, sdk and dead sessions do not count")
            self.assertEqual(cas.tabs_elsewhere("mt"), 1)


class CutRecord(unittest.TestCase):
    """Audit of release 25: the real loader, the reset the error names, the lock."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="cas-cr-")) / "cut-moves.json"
        p = mock.patch.object(cas, "CUT_MOVES", self.path)
        p.start()
        self.addCleanup(p.stop)

    def test_real_loader_writes_the_record(self):
        cas.cca_record_cut({"lt": {"at": iso(-0.05), "text": "You've hit your 5-hour limit"}})
        self.assertEqual(json.loads(self.path.read_text())["lt"]["at"], iso(-0.05))
        self.assertTrue(cas.recent_limit_error("lt", AT))

    def test_a_broken_loader_never_breaks_the_tick(self):
        with mock.patch.object(cas, "CURSOR_TOOL", self.path.parent / "missing.py"), \
             mock.patch.object(cas, "log") as log:
            cas.cca_record_cut({"lt": {"at": iso(0), "text": ""}})
        self.assertTrue(log.called)
        self.assertFalse(self.path.exists())

    def test_a_window_already_reset_does_not_count(self):
        # Error at 18:54 UTC naming 7:59pm Madrid (18:59 UTC): reset before AT (19:00).
        self.path.write_text(json.dumps({"lt": {"at": iso(-0.1), "text": "You've hit your 5-hour limit · resets 7:59pm (Europe/Madrid)"}}))
        self.assertFalse(cas.recent_limit_error("lt", AT))
        self.path.write_text(json.dumps({"lt": iso(-0.1)}))
        self.assertTrue(cas.recent_limit_error("lt", AT), "a bare time of release 24 still reads")

    def test_record_waits_for_the_lock(self):
        import threading
        spec = cas.importlib.util.spec_from_file_location("cca_lock_test", cas.CURSOR_TOOL)
        mod = cas.importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.CUT_MOVES = self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        held = self.path.with_suffix(".lock").open("a")
        cas.fcntl.flock(held, cas.fcntl.LOCK_EX)
        done = threading.Event()
        t = threading.Thread(target=lambda: (mod.record_cut({"lt": {"at": iso(0), "text": ""}}), done.set()))
        t.start()
        self.assertFalse(done.wait(0.5), "it waits for the lock")
        held.close()
        self.assertTrue(done.wait(5))
        t.join()


class SaveState(unittest.TestCase):
    def test_temp_file_is_per_writer(self):
        # Audit of release 23: --enable and a tick shared one state.tmp.
        d = Path(tempfile.mkdtemp(prefix="cas-ss-"))
        (d / "state.tmp").mkdir()  # another writer's temp in the way of a shared name
        with mock.patch.object(cas, "STATE_DIR", d), mock.patch.object(cas, "STATE", d / "state.json"):
            cas.save_state({"a": 1})
        self.assertEqual(json.loads((d / "state.json").read_text()), {"a": 1})


class Prune(unittest.TestCase):
    def test_upgrade_failure_keys_live_with_their_tag(self):
        # Audit of release 23: dropping "lt:up" every tick re-sent the warn on each retry.
        st = {"last_fail_at": {"lt": iso(-1)}, "fail_notices": {"lt:up": "verify", "mt:up": "x"}}
        cas.prune_failures(st, AT)
        self.assertEqual(st["fail_notices"], {"lt:up": "verify"})


class Order(unittest.TestCase):
    def test_spaces_unknown_and_missing_tags(self):
        # Audit of release 19: "lt, mt, na" kept only lt; a partial list excluded na.
        self.assertEqual(cas.parse_order("lt, mt, na"), (["lt", "mt", "na"], []))
        self.assertEqual(cas.parse_order("mt,lt"), (["mt", "lt", "na"], []))
        self.assertEqual(cas.parse_order("na,xx"), (["na", "lt", "mt"], ["xx"]))


if __name__ == "__main__":
    unittest.main()
