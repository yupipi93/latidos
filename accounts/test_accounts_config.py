"""Tests for accounts_config.py and for how the tools behave with a missing or broken
accounts config (audit of release 82). Every tool runs in a subprocess with HOME pointed at
a temp folder, so no real state, log or setting is touched.

Run from the repository root:
    python3 -m unittest accounts/test_accounts_config.py -v
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import accounts_config

HERE = Path(__file__).resolve().parent


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="acconf-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def load(self, data):
        p = self.tmp / "c.json"
        p.write_text(data if isinstance(data, str) else json.dumps(data))
        return accounts_config.load(p)

    def test_missing_broken_and_empty(self):
        self.assertEqual(accounts_config.load(self.tmp / "nope.json").tags, [])
        self.assertEqual(self.load("{bad,").tags, [])
        self.assertEqual(self.load({}).default_tag, "")

    def test_accounts_default_and_order(self):
        c = self.load({"accounts": [{"tag": "work", "config_dir": ""}, {"tag": "home_2", "config_dir": "~/.claude.h"},
                                    {"tag": "bad tag"}, "not an object", {"tag": "work"}],
                       "order": ["home_2", "ghost"]})
        self.assertEqual(c.tags, ["work", "home_2"])
        self.assertEqual(c.order, ["home_2", "work"])
        self.assertEqual(c.default_tag, "work")
        self.assertIsNone(c.by_tag["work"].env)
        self.assertEqual(c.by_tag["home_2"].home_form, "~/.claude.h")
        self.assertEqual(c.tag_for_dir(""), "work")

    def test_notifier_path_command_or_string(self):
        c = self.load({"notify": "echo"})
        self.assertEqual(c.notify_argv, ["echo"])
        with mock.patch.object(accounts_config.subprocess, "run") as run:
            self.assertTrue(c.notify("info", "t", "b"))
        self.assertEqual(run.call_args[0][0], ["echo", "info", "t", "b"])
        self.assertFalse(self.load({"notify": ["/no/such/notifier"]}).notify("info", "t", "b"))
        self.assertEqual(self.load({"notify": 7}).notify_argv, [])

    def test_tz_env_forms(self):
        # Audit of 84: UTC and path forms of $TZ; a misspelled one falls to /etc/localtime.
        with mock.patch.dict(os.environ, {"TZ": "UTC"}):
            self.assertEqual(str(accounts_config.zone("")), "UTC")
        with mock.patch.dict(os.environ, {"TZ": ":/usr/share/zoneinfo/Asia/Tokyo"}):
            self.assertEqual(str(accounts_config.zone("")), "Asia/Tokyo")
        with mock.patch.dict(os.environ, {"TZ": "Europe/Madri"}), \
             mock.patch.object(accounts_config, "_localtime_name", return_value="America/New_York"):
            self.assertEqual(str(accounts_config.zone("")), "America/New_York")

    def test_warnings_only_for_a_person(self):
        import io
        err = io.StringIO()
        err.isatty = lambda: False
        with mock.patch.object(accounts_config.sys, "stderr", err):
            self.load("{bad,")
            accounts_config.zone("Europe/Madird")
        self.assertEqual(err.getvalue(), "", "under launchd stderr is the log")

    def test_bad_timezone_falls_back(self):
        z = accounts_config.zone("Europe/Madird")
        self.assertIsNotNone(z)
        self.assertEqual(str(accounts_config.zone("Europe/Madrid")), "Europe/Madrid")


class NoAccounts(unittest.TestCase):
    """A missing or broken config: no crash, nothing written (audit of release 82)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="acconf-run-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "cfg.json").write_text("{broken,")

    def run_tool(self, *args, stdin=None, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "USER": "tester",
             "LATIDOS_CLAUDE_ACCOUNTS": str(self.tmp / "cfg.json")}
        e.update(env)
        return subprocess.run([sys.executable, *args], env=e, capture_output=True, text=True, timeout=60,
                              input=stdin)

    def test_auto_switch_exits_quietly_and_writes_no_state(self):
        for flags in (["--status"], ["--dry-run"], []):
            r = self.run_tool(str(HERE / "claude-auto-switch.py"), *flags)
            self.assertEqual(r.returncode, 0, r.stderr)
            if flags:  # a plain tick under launchd logs it at most once an hour (audit of 83)
                self.assertIn("no Claude accounts configured", r.stdout)
            else:
                self.assertEqual(r.stdout, "")
        self.assertFalse((self.tmp / ".local/state/claude-auto-switch/state.json").exists())

    def test_disable_and_enable_still_work_without_accounts(self):
        # Audit of 83: the no-accounts guard swallowed --disable, --enable and --uninstall.
        tool = str(HERE / "claude-auto-switch.py")
        r = self.run_tool(tool, "--disable")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.tmp / ".local/state/claude-auto-switch/off").exists())
        r = self.run_tool(tool, "--enable")
        self.assertIn("enabled", r.stdout)
        self.assertFalse((self.tmp / ".local/state/claude-auto-switch/off").exists())

    def test_fallback_runs_the_plain_cli(self):
        fake = self.tmp / "claude"
        fake.write_text("#!/bin/sh\necho \"plain $*\"; cat\n")
        fake.chmod(0o755)
        fake.write_text("#!/bin/sh\necho \"plain $* code=${CLAUDECODE:-none}\"; cat\n")
        r = self.run_tool(str(HERE / "claude-with-fallback.py"), "-p", "hi", stdin="from stdin",
                          CLAUDE_REAL_BIN=str(fake), CLAUDECODE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("plain -p hi code=none", r.stdout)
        self.assertIn("from stdin", r.stdout)

    def test_control_and_resume_answer(self):
        r = self.run_tool(str(HERE / "claude-account-control.py"), "state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["accounts"], [])
        r = self.run_tool(str(HERE / "claude-quota-resume.py"), "--status")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_timezone_does_not_stop_the_tools(self):
        cfg = json.loads((HERE / "testdata/claude-accounts.json").read_text())
        cfg["timezone"] = "Europe/Madird"
        (self.tmp / "cfg.json").write_text(json.dumps(cfg))
        r = self.run_tool(str(HERE / "claude-account-control.py"), "state")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([a["tag"] for a in json.loads(r.stdout)["accounts"]], ["lt", "mt", "na"])


class LoginTool(unittest.TestCase):
    def test_same_tag_rule_as_the_shared_config(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("claude_accounts_t", HERE / "claude_accounts.py")
        ca = importlib.util.module_from_spec(spec)
        sys.modules["claude_accounts_t"] = ca  # a dataclass needs its module registered
        self.addCleanup(sys.modules.pop, "claude_accounts_t", None)
        spec.loader.exec_module(ca)
        tmp = Path(tempfile.mkdtemp(prefix="acconf-login-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = tmp / "c.json"
        cfg.write_text(json.dumps({"accounts": [{"tag": "work_2", "email": "w@example.com", "config_dir": ""}]}))
        with mock.patch.object(ca, "CONFIG", cfg):
            self.assertEqual([a.tag for a in ca.load_config()[1]], ["work_2"])
            cfg.write_text(json.dumps({"accounts": ["nope"]}))
            with self.assertRaises(ca.ConfigError):
                ca.load_config()


if __name__ == "__main__":
    unittest.main()
