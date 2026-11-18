"""Offline tests for claude_accounts.py: fake `security` and fake `claude`, temp config and state.

python3 -m unittest accounts/test_claude_accounts.py
"""

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent / "claude_accounts.py"
DAY_MS = 86_400_000

# Keychain: one JSON file per service in $FAKE_KC (missing file = exit 44). Never prints the token
# in tests, but returns one so the tool has to drop it.
FAKE_SECURITY = textwrap.dedent("""\
    #!/bin/sh
    while [ $# -gt 0 ]; do [ "$1" = "-s" ] && svc="$2"; shift; done
    f="$FAKE_KC/$svc.json"
    [ -f "$f" ] || exit 44
    cat "$f"
""")

# claude auth login: records its env, then acts per $FAKE_LOGIN_<tag>: ok | wrong | fail | hang.
FAKE_CLAUDE = textwrap.dedent("""\
    #!/bin/sh
    tag=lt; svc="Claude Code-credentials"; json="$FAKE_DEFAULT_JSON"
    case "$CLAUDE_CONFIG_DIR" in *.mt) tag=mt; svc="Claude Code-credentials-mt"; json="$CLAUDE_CONFIG_DIR/.claude.json";; esac
    echo "$tag cfg=${CLAUDE_CONFIG_DIR:-unset} profile=$CLAUDE_ACCOUNTS_CHROME_PROFILE browser=$BROWSER args=$*" >> "$FAKE_LOG"
    eval mode=\\${FAKE_LOGIN_$tag:-ok}
    case "$mode" in
      hang) exec sleep 60;;
      fail) echo "OAuth error: denied" >&2; exit 1;;
    esac
    email="$(echo "$*" | sed -n 's/.*--email \\([^ ]*\\).*/\\1/p')"
    [ "$mode" = wrong ] && email="someone@example.net"
    printf '{"oauthAccount":{"emailAddress":"%s"}}' "$email" > "$json"
    printf '{"claudeAiOauth":{"accessToken":"sk-ant-oat-SECRET","refreshToken":"sk-ant-ort-SECRET","expiresAt":1,"refreshTokenExpiresAt":%s,"subscriptionType":"team"}}' "$FAKE_NEW_EXP" > "$FAKE_KC/$svc.json"
""")


class ClaudeAccountsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.kc = self.tmp / "kc"
        self.kc.mkdir()
        self.mtdir = self.tmp / ".claude.mt"
        self.mtdir.mkdir()
        for name, body in (("security", FAKE_SECURITY), ("claude", FAKE_CLAUDE)):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(0o755)
        self.config = self.tmp / "claude-accounts.json"
        self.config.write_text(json.dumps({
            "settings": {"soon_days": 3, "login_timeout_seconds": 2},
            "accounts": [
                {"tag": "lt", "label": "Work", "email": "lt@example.com", "config_dir": "",
                 "keychain": "Claude Code-credentials", "chrome_profile": "Profile 1"},
                {"tag": "mt", "label": "Home", "email": "mt@example.com", "config_dir": str(self.mtdir),
                 "keychain": "Claude Code-credentials-mt", "chrome_profile": "Profile 2"},
            ]}))
        self.now_ms = int(time.time() * 1000)
        self.new_exp = self.now_ms + 30 * DAY_MS
        self.default_json = self.tmp / "default.json"
        self.log = self.tmp / "calls.log"

    def keychain(self, svc, refresh_exp, refresh=True):
        (self.kc / f"{svc}.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": "sk-ant-oat-SECRET", "refreshToken": "sk-ant-ort-SECRET" if refresh else "",
            "expiresAt": self.now_ms, "refreshTokenExpiresAt": refresh_exp, "subscriptionType": "team"}}))

    def emails(self, lt="lt@example.com", mt="mt@example.com"):
        self.default_json.write_text(json.dumps({"oauthAccount": {"emailAddress": lt}}))
        (self.mtdir / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": mt}}))

    def run_tool(self, *args, **env):
        e = {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "USER": "tester",
             "LATIDOS_CLAUDE_ACCOUNTS": os.environ["LATIDOS_CLAUDE_ACCOUNTS"],
             "CLAUDE_ACCOUNTS_CONFIG": str(self.config), "CLAUDE_ACCOUNTS_STATE": str(self.tmp / "state"),
             "CLAUDE_ACCOUNTS_LOG": str(self.tmp / "tool.log"), "CLAUDE_ACCOUNTS_CLAUDE_BIN": str(self.tmp / "claude"),
             "CLAUDE_ACCOUNTS_SECURITY_BIN": str(self.tmp / "security"), "CLAUDE_ACCOUNTS_DEFAULT_JSON": str(self.default_json),
             "CLAUDE_ACCOUNTS_WARMUP_STATE": str(self.tmp / "warmup.json"),
             "FAKE_KC": str(self.kc), "FAKE_LOG": str(self.log), "FAKE_DEFAULT_JSON": str(self.default_json),
             "FAKE_NEW_EXP": str(self.new_exp), "CLAUDE_CONFIG_DIR": "/should/be/removed", "ANTHROPIC_API_KEY": "nope"}
        e.update(env)
        return subprocess.run([sys.executable, str(TOOL), *args], env=e, capture_output=True, text=True, timeout=60)

    def status(self):
        r = self.run_tool("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("SECRET", r.stdout)
        return {a["tag"]: a for a in json.loads(r.stdout)["accounts"]}

    def test_status_states_and_no_tokens(self):
        self.emails()
        self.keychain("Claude Code-credentials", self.now_ms + 10 * DAY_MS)
        self.keychain("Claude Code-credentials-mt", self.now_ms + 1 * DAY_MS)
        (self.tmp / "warmup.json").write_text(json.dumps({"accounts": {"lt": {"last_success_at": "2026-11-12T05:00:01", "outcome": "ok"}}}))
        s = self.status()
        self.assertEqual(s["lt"]["state"], "ok")
        self.assertAlmostEqual(s["lt"]["days_left"], 10, delta=0.2)
        self.assertEqual(s["lt"]["warmup"]["outcome"], "ok")
        self.assertEqual(s["mt"]["state"], "soon")
        self.keychain("Claude Code-credentials-mt", self.now_ms - DAY_MS)
        self.assertEqual(self.status()["mt"]["state"], "expired")
        (self.kc / "Claude Code-credentials-mt.json").unlink()
        self.assertEqual(self.status()["mt"]["state"], "missing")
        self.keychain("Claude Code-credentials-mt", self.now_ms + 20 * DAY_MS, refresh=False)
        self.assertEqual(self.status()["mt"]["state"], "missing")

    def test_wrong_user_in_config(self):
        self.emails(mt="lt@example.com")
        self.keychain("Claude Code-credentials", self.now_ms + 10 * DAY_MS)
        self.keychain("Claude Code-credentials-mt", self.now_ms + 10 * DAY_MS)
        mt = self.status()["mt"]
        self.assertEqual(mt["state"], "wrong")
        self.assertIn("lt@example.com", mt["detail"])

    def test_login_one_account_env_and_profile(self):
        self.emails()
        self.keychain("Claude Code-credentials", self.now_ms + DAY_MS)
        self.keychain("Claude Code-credentials-mt", self.now_ms + 10 * DAY_MS)
        r = self.run_tool("login", "lt")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn("cfg=unset", calls[0])  # lt runs with CLAUDE_CONFIG_DIR unset
        self.assertIn("profile=Profile 1", calls[0])
        self.assertIn("auth login --claudeai --email lt@example.com", calls[0])
        self.assertIn("open-in-profile.sh", calls[0])
        self.assertAlmostEqual(self.status()["lt"]["days_left"], 30, delta=0.2)
        self.assertNotIn("SECRET", (self.tmp / "tool.log").read_text())

    def test_login_expired_only_in_parallel(self):
        self.emails()
        self.keychain("Claude Code-credentials", self.now_ms + 20 * DAY_MS)
        self.keychain("Claude Code-credentials-mt", self.now_ms - DAY_MS)
        r = self.run_tool("login", "--expired")
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.log.read_text().splitlines()
        self.assertEqual([c.split()[0] for c in calls], ["mt"])
        self.assertIn(f"cfg={self.mtdir}", calls[0])
        self.assertEqual(self.status()["mt"]["state"], "ok")

    def test_login_all(self):
        self.emails()
        self.keychain("Claude Code-credentials", self.now_ms + 20 * DAY_MS)
        self.keychain("Claude Code-credentials-mt", self.now_ms + 20 * DAY_MS)
        r = self.run_tool("login", "--all")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(sorted(c.split()[0] for c in self.log.read_text().splitlines()), ["lt", "mt"])

    def test_login_wrong_user_is_reported(self):
        self.emails()
        self.keychain("Claude Code-credentials-mt", self.now_ms - DAY_MS)
        r = self.run_tool("login", "mt", FAKE_LOGIN_mt="wrong")
        self.assertEqual(r.returncode, 2)
        self.assertIn("WRONG USER", (self.tmp / "tool.log").read_text())

    def test_login_failure_and_timeout(self):
        self.emails()
        self.assertEqual(self.run_tool("login", "mt", FAKE_LOGIN_mt="fail").returncode, 1)
        self.assertIn("OAuth error", (self.tmp / "tool.log").read_text())
        started = time.time()
        r = self.run_tool("login", "mt", FAKE_LOGIN_mt="hang")
        self.assertEqual(r.returncode, 1)
        self.assertLess(time.time() - started, 20)
        self.assertIn("nobody completed", (self.tmp / "tool.log").read_text())

    def test_bad_input(self):
        self.assertEqual(self.run_tool("login", "zz").returncode, 3)
        self.assertEqual(self.run_tool("login").returncode, 3)
        self.assertEqual(self.run_tool("login", "lt", "--all").returncode, 3)

    def test_nothing_to_renew(self):
        self.emails()
        self.keychain("Claude Code-credentials", self.now_ms + 20 * DAY_MS)
        self.keychain("Claude Code-credentials-mt", self.now_ms + 20 * DAY_MS)
        r = self.run_tool("login", "--soon")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nada que renovar", r.stdout)
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
