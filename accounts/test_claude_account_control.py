"""Tests for scripts/claude-account-control.py (the command Latidos runs from the account
cards). No account is switched and no tab moved: every side effect is mocked or pointed at
a temp dir.

Run from the repository root:
    python3 -m unittest accounts/test_claude_account_control.py -v
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "claude-account-control.py"
spec = importlib.util.spec_from_file_location("claude_account_control", SCRIPT)
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
cas = ctl.cas
REAL_STATE = cas.STATE  # never written by these tests (audit of 45: one did, through record_manual)


def run(*argv) -> tuple[int, dict]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = ctl.main(list(argv))
    return code, json.loads(buf.getvalue().strip().splitlines()[-1]) if buf.getvalue().strip() else {}


class Control(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cac-test-"))
        self.switched = []
        patches = [
            mock.patch.object(cas, "STATE_DIR", self.tmp),
            mock.patch.object(cas, "STATE", self.tmp / "state.json"),
            mock.patch.object(cas, "CUT_MOVES", self.tmp / "cut-moves.json"),
            mock.patch.object(cas, "fresh_limit_errors", return_value=[]),
            mock.patch.object(cas, "recent_limit_error", return_value=False),
            mock.patch.object(cas, "LOCK", self.tmp / "lock"),
            mock.patch.object(cas, "OFF", self.tmp / "off"),
            mock.patch.object(cas, "ORDER_FILE", self.tmp / "cfg" / "claude-order.json"),
            mock.patch.object(ctl, "ACTIVATION", self.tmp / "activation.json"),
            mock.patch.object(ctl, "RUN_LOCK", self.tmp / "activation.lock"),
            mock.patch.object(cas, "log"),
            mock.patch.object(cas, "read_cache", return_value={}),
            mock.patch.object(cas, "current_account", return_value="lt"),
            mock.patch.object(cas, "switch_to", side_effect=lambda t: self.switched.append(t) or (True, "")),
            mock.patch.object(cas, "run", return_value=(0, 'MOVE-SUMMARY [{"outcome": "continued"}, {"outcome": "left"}]')),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.real_mtime = REAL_STATE.stat().st_mtime if REAL_STATE.exists() else None
        self.addCleanup(lambda: self.assertEqual(
            REAL_STATE.stat().st_mtime if REAL_STATE.exists() else None, self.real_mtime, "real state written"))

    def test_order_writes_the_file_and_keeps_every_account(self):
        code, out = run("order", "na,lt")
        self.assertEqual((code, out["order"]), (0, ["na", "lt", "mt"]))
        saved = json.loads(cas.ORDER_FILE.read_text())
        self.assertEqual(saved["order"], ["na", "lt", "mt"])
        self.assertEqual(cas.parse_order(cas.read_order_file(cas.ORDER_FILE))[0], ["na", "lt", "mt"])

    def test_order_refuses_unknown_or_repeated_tags(self):
        for bad in ("xx", "lt,lt", "", "lt;rm"):
            code, out = run("order", bad)
            self.assertEqual(code, 2, bad)
        self.assertFalse(cas.ORDER_FILE.exists())

    def test_activate_switches_moves_idle_tabs_and_records(self):
        code, out = run("activate", "na")
        self.assertEqual(code, 0)
        self.assertEqual(self.switched, ["na"])
        args = cas.run.call_args[0][0]
        self.assertIn("--move-open", args)
        self.assertEqual(args[args.index("--scope") + 1], "idle")
        self.assertEqual(out["tabs"], {"continued": 1, "left": 1})
        self.assertEqual(ctl.read_activation()["phase"], "done")

    def test_activate_records_the_manual_switch_before_moving_tabs(self):
        # Audit of 45: a move longer than 5 minutes made the tick read the switch as a choice.
        seen = {}
        def move(*a, **k):
            seen.update(cas.load_state())
            return 0, "MOVE-SUMMARY []"
        with mock.patch.object(cas, "run", side_effect=move), \
             mock.patch.object(cas, "read_cache", return_value={"lt": {"source": "live", "fetched_at": cas.now().isoformat(),
                                                                       "session": {"percent": 100, "full": True}}}):
            run("activate", "na")
        self.assertEqual((seen["seen"], seen["manual_from"]), ("na", "lt"))
        self.assertTrue(seen["manual_left_spent"], "lt was spent: leaving it is not a choice")

    def test_activate_while_another_runs_is_refused_without_touching_it(self):
        import fcntl
        ctl.write_activation({"tag": "mt", "phase": "moving", "started_at": cas.now().isoformat()})
        with ctl.RUN_LOCK.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)  # another activation holds the run lock
            code, out = run("activate", "na")
        self.assertEqual((code, out["error"]), (1, "busy"))
        self.assertEqual(ctl.read_activation()["tag"], "mt")
        self.assertEqual(self.switched, [])

    def test_seen_at_renewed_after_a_long_move(self):
        # Audit of 46: the move holds the lock for minutes; no tick saves seen_at meanwhile.
        times = iter([cas.now(), cas.now()])
        cas.save_state({"seen": "lt", "seen_at": "2020-01-01T00:00:00+00:00"})
        with mock.patch.object(cas, "current_account", side_effect=["lt", "na"]):
            run("activate", "na")
        st = cas.load_state()
        self.assertEqual(st["seen"], "na")
        self.assertGreater(cas.parse_time(st["seen_at"]), cas.parse_time("2026-01-01T00:00:00+00:00"))

    def test_seen_at_renewed_even_after_a_terminal_switch_during_the_move(self):
        # Audit of 47: the renewal was skipped exactly when a shell switch ran during the move.
        cas.save_state({"seen": "lt", "seen_at": "2020-01-01T00:00:00+00:00"})
        with mock.patch.object(cas, "current_account", side_effect=["lt", "mt"]):
            run("activate", "na")
        st = cas.load_state()
        self.assertGreater(cas.parse_time(st["seen_at"]), cas.parse_time("2026-01-01T00:00:00+00:00"))

    def test_a_dead_run_shows_stale_at_once(self):
        ctl.write_activation({"tag": "mt", "phase": "moving", "started_at": cas.now().isoformat()})
        self.assertEqual(ctl.activation_view(cas.now())["error"], "stale", "run lock free: nobody runs it")
        import fcntl
        with ctl.RUN_LOCK.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self.assertEqual(ctl.activation_view(cas.now())["phase"], "moving", "held: still running")

    def test_a_terminal_switch_not_seen_yet_is_recorded(self):
        # Audit of 46: a shell switch typed in a terminal, then na activated before any tick.
        cas.save_state({"seen": "mt", "seen_at": cas.now().isoformat()})
        with mock.patch.object(cas, "current_account", return_value="na"):
            run("activate", "na")
        st = cas.load_state()
        self.assertEqual((st["manual_from"], st["seen"]), ("mt", "na"))
        self.assertEqual(self.switched, [], "already the default: only tabs move")

    def test_crashed_or_stale_run_is_shown_as_failed(self):
        with mock.patch.object(cas, "switch_to", side_effect=OSError("disk full")):
            code, out = run("activate", "mt")
        self.assertEqual((code, ctl.read_activation()["error"]), (1, "crashed"))
        old = (cas.now() - ctl.STALE_ACTIVATION * 2).isoformat()
        ctl.write_activation({"tag": "mt", "phase": "switching", "started_at": old})
        self.assertEqual(ctl.activation_view(cas.now())["error"], "stale")
        self.assertEqual(run("activate", "na")[0], 0, "a dead run does not block")

    def test_activating_the_default_is_a_choice_to_stay(self):
        # Audit of 48: a click on the account already in use must hold it like claude-<tag>.
        cas.save_state({"seen": "lt", "seen_at": cas.now().isoformat()})
        run("activate", "lt")
        st = cas.load_state()
        self.assertEqual((st["manual_from"], st["seen"], st["manual_left_spent"]), ("lt", "lt", False))
        self.assertIn("manual_at", st)

    def test_activate_failed_switch_is_reported(self):
        with mock.patch.object(cas, "switch_to", return_value=(False, "login check failed")):
            code, out = run("activate", "mt")
        self.assertEqual((code, out["error"]), (1, "switch_failed"))
        self.assertFalse(ctl.read_activation()["ok"])
        cas.run.assert_not_called()

    def test_activate_unknown_tag(self):
        self.assertEqual(run("activate", "zz")[0], 2)
        self.assertEqual(self.switched, [])

    def test_activate_the_current_account_only_moves_tabs(self):
        code, out = run("activate", "lt")
        self.assertEqual((code, self.switched), (0, []))
        cas.run.assert_called_once()

    def test_state_shape(self):
        code, out = run("state")
        self.assertEqual(code, 0)
        self.assertEqual(out["active"], "lt")
        self.assertEqual([a["tag"] for a in out["accounts"]], list(cas.ORDER))
        self.assertIn("activation", out)
        self.assertTrue(out["auto"]["enabled"])


if __name__ == "__main__":
    unittest.main()
