"""Offline tests for claude-quota-warmup.py with a fake claude binary.

python3 -m unittest accounts/test_claude_quota_warmup.py
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "claude-quota-warmup.py"

FAKE = textwrap.dedent("""\
    #!/bin/sh
    # Answers per account: FAKE_<tag>=ok|weekly|fail. lt = CLAUDE_CONFIG_DIR unset.
    tag=lt
    case "$CLAUDE_CONFIG_DIR" in *.mt) tag=mt;; *.na) tag=na;; esac
    echo "$tag $*" >> "$FAKE_LOG"
    eval mode=\\${FAKE_$tag:-ok}
    case "$mode" in
      ok) echo '{"is_error":false,"result":"hola","duration_ms":10}';;
      session) echo '{"is_error":true,"result":"You'"'"'ve hit your 5-hour limit · resets 6:50am"}'; exit 1;;
      slow) sleep 1; echo '{"is_error":false,"result":"hola","duration_ms":10}';;
      weekly) echo '{"is_error":true,"result":"You'"'"'ve hit your weekly limit · resets Nov 16 at 7am"}'; exit 1;;
      *) echo '{"is_error":true,"result":"Not logged in · Please run /login"}'; exit 1;;
    esac
""")


class WarmupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.bin = self.tmp / "claude"
        self.bin.write_text(FAKE)
        self.bin.chmod(0o755)
        self.log = self.tmp / "calls.log"
        self.state = self.tmp / "state"
        self.home = self.tmp / "home"
        (self.home / ".local/bin").mkdir(parents=True)

    def run_warmup(self, *args, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "CLAUDE_REAL_BIN": str(self.bin),
             "WARMUP_STATE_DIR": str(self.state), "WARMUP_RETRY_SECONDS": "0", "FAKE_LOG": str(self.log),
             "WARMUP_SKIP_AFTER": "23:59:59", "WARMUP_WEEKDAYS": "1,2,3,4,5,6,7",
             "LATIDOS_CLAUDE_ACCOUNTS": os.environ["LATIDOS_CLAUDE_ACCOUNTS"]}
        e.update(env)
        return subprocess.run([sys.executable, str(SCRIPT), *args], env=e, capture_output=True, text=True, timeout=60)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def read_state(self):
        return json.loads((self.state / "state.json").read_text())

    def test_warms_every_account_once_per_day(self):
        r = self.run_warmup()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(c.split()[0] for c in self.calls()), ["lt", "mt", "na"])
        for c in self.calls():
            self.assertIn("--no-session-persistence", c)
            self.assertIn("--strict-mcp-config", c)
        s = self.read_state()
        today = dt.date.today().isoformat()
        for tag in ("lt", "mt", "na"):
            self.assertEqual(s["accounts"][tag]["last_success"], today)
            self.assertEqual(s["accounts"][tag]["outcome"], "ok")
        self.assertEqual(s["schedule"]["hour"], 5)
        self.run_warmup()
        self.assertEqual(len(self.calls()), 3, "second run the same day must not call again")

    def test_lt_runs_with_config_dir_unset(self):
        self.run_warmup("--force", "-a", "lt", CLAUDE_CONFIG_DIR=str(self.home / ".claude.mt"))
        self.assertEqual([c.split()[0] for c in self.calls()], ["lt"])

    def test_weekly_limit_is_blocked_without_retries(self):
        r = self.run_warmup(FAKE_mt="weekly")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sum(1 for c in self.calls() if c.startswith("mt ")), 1)
        rec = self.read_state()["accounts"]["mt"]
        self.assertEqual(rec["outcome"], "blocked")
        self.assertIn("weekly limit", rec["error"])
        self.assertNotIn("last_success", rec)
        self.run_warmup(FAKE_mt="weekly")
        self.assertEqual(sum(1 for c in self.calls() if c.startswith("mt ")), 1, "blocked today: not tried again")

    def test_failure_retries_and_fails(self):
        r = self.run_warmup("-a", "na", FAKE_na="fail")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.calls()), 4)
        rec = self.read_state()["accounts"]["na"]
        self.assertEqual(rec["outcome"], "error")
        self.assertIn("Not logged in", rec["error"])

    def test_skip_after_window(self):
        r = self.run_warmup(WARMUP_SKIP_AFTER="00:00")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("schedule", self.read_state())

    def test_v1_state_is_migrated(self):
        self.state.mkdir(parents=True)
        today = dt.date.today().isoformat()
        (self.state / "state.json").write_text(json.dumps({"last_success": today, "last_success_at": today + "T05:00:06"}))
        self.run_warmup()
        self.assertEqual(sorted(c.split()[0] for c in self.calls()), ["mt", "na"])
        self.assertEqual(self.read_state()["accounts"]["lt"]["last_success_at"], today + "T05:00:06")

    def test_spent_5_hour_window_counts_as_open(self):
        r = self.run_warmup("-a", "lt", FAKE_lt="session")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.calls()), 1, "no retries: the window is already open")
        rec = self.read_state()["accounts"]["lt"]
        self.assertEqual(rec["outcome"], "ok")
        self.assertEqual(rec["last_success"], dt.date.today().isoformat())

    def test_not_a_warm_up_day(self):
        other = [d for d in range(1, 8) if d != dt.date.today().isoweekday()]
        self.state.mkdir(parents=True)
        (self.state / "state.json").write_text(json.dumps({"schedule": {"hour": 5, "minute": 0, "weekdays": other}}))
        r = self.run_warmup()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("not a warm-up day", r.stdout)
        self.run_warmup("--force", "-a", "mt")
        self.assertEqual(len(self.calls()), 1, "--force ignores the day")

    def test_a_run_keeps_the_installed_schedule(self):
        self.state.mkdir(parents=True)
        installed = {"hour": 6, "minute": 30, "weekdays": [1, 2, 3, 4, 5, 6, 7], "skip_after": "09:00"}
        (self.state / "state.json").write_text(json.dumps({"schedule": installed}))
        self.run_warmup("-a", "lt")
        self.assertEqual(self.read_state()["schedule"], installed)

    def test_two_processes_keep_each_others_records(self):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.home), "CLAUDE_REAL_BIN": str(self.bin),
             "WARMUP_STATE_DIR": str(self.state), "WARMUP_RETRY_SECONDS": "0", "FAKE_LOG": str(self.log),
             "WARMUP_SKIP_AFTER": "23:59:59", "WARMUP_WEEKDAYS": "1,2,3,4,5,6,7", "FAKE_lt": "slow", "FAKE_mt": "slow", "FAKE_na": "slow",
             "LATIDOS_CLAUDE_ACCOUNTS": os.environ["LATIDOS_CLAUDE_ACCOUNTS"]}
        procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--force", "-a", t], env=e,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for t in ("lt", "mt", "na")]
        for p in procs:
            self.assertEqual(p.wait(timeout=30), 0)
        self.assertEqual(sorted(self.read_state()["accounts"]), ["lt", "mt", "na"])

    def test_install_refuses_a_slot_after_skip_after(self):
        r = self.run_warmup("--install", WARMUP_HOUR="9", WARMUP_SKIP_AFTER="09:00")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not before WARMUP_SKIP_AFTER", r.stdout + r.stderr)

    def test_install_refuses_bad_weekdays(self):
        for raw in ("0,1", "", "8", "lun"):
            r = self.run_warmup("--install", WARMUP_WEEKDAYS=raw)
            self.assertNotEqual(r.returncode, 0, raw)
            self.assertIn("WARMUP_WEEKDAYS", r.stdout + r.stderr, raw)

    def test_bad_weekdays_do_not_break_status_or_runs(self):
        r = self.run_warmup("-a", "lt", WARMUP_WEEKDAYS="")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_schedule_records_the_accounts(self):
        self.run_warmup("-a", "lt", WARMUP_ACCOUNTS="lt,mt")
        self.assertEqual(self.read_state()["schedule"]["accounts"], ["lt", "mt"])

    def test_unknown_account_refused(self):
        r = self.run_warmup("--force", "-a", "zz")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
