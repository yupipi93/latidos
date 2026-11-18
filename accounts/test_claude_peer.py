"""Tests for scripts/claude_peer.py (peer-socket delivery with the permission-class envelope)
and its use in scripts/claude-quota-resume.py.

A fake session (a Unix socket server plus a sessions/<pid>.json record) accepts a message
only when it attests the class the fake is set to, like Claude Code 2.1.318 / 2.1.322 do,
and goes "busy" when it takes one. No Claude process is started.

Run from the repository root:
    python3 -m unittest accounts/test_claude_peer.py -v
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import importlib.util
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import atexit as _atexit
import shutil as _shutil

# Every temp dir these tests make goes under one dir removed at exit (audit of relaunch
# release 34: thousands of cca-test-* dirs had piled up in $TMPDIR).
tempfile.tempdir = tempfile.mkdtemp(prefix="claude-accounts-tests-")
_atexit.register(_shutil.rmtree, tempfile.tempdir, True)
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import claude_peer  # noqa: E402

spec = importlib.util.spec_from_file_location("claude_quota_resume", SCRIPTS / "claude-quota-resume.py")
cqr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cqr)

# Hermetic: a managed Mac may have a managed-settings.json that disables bypass.
claude_peer.MANAGED_SETTINGS = Path("/nonexistent/managed-settings.json")


class FakeSession:
    """Socket + record. Takes a line only when its origin.fromMode == accepts; holds the
    others and releases them when set_accepts() makes them match, like the CLI does on a
    permission-mode change."""

    def __init__(self, accepts: str | None, busy_now: bool = False, transcript: Path | None = None):
        self.dir = Path(tempfile.mkdtemp(prefix="cp-", dir="/tmp"))
        _atexit.register(_shutil.rmtree, self.dir, True)  # under /tmp for a short socket path
        self.sock_path = self.dir / "s.sock"
        self.sessions = self.dir / "sessions"
        self.sessions.mkdir()
        self.pid = 4242
        self.started_at = 1794477000000  # 2026-11-12T09:50:00Z
        self.accepts = accepts
        self.transcript = transcript
        self.lines: list[dict] = []
        self.held: list[dict] = []
        self.taken: list[dict] = []
        self.write_record("busy" if busy_now else "idle", 1)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(self.sock_path))
        self.srv.listen(4)
        self.srv.settimeout(0.2)
        self.stop = False
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def write_record(self, status: str, at: int) -> None:
        (self.sessions / f"{self.pid}.json").write_text(json.dumps(
            {"pid": self.pid, "sessionId": "sid", "messagingSocketPath": str(self.sock_path),
             "status": status, "statusUpdatedAt": at, "startedAt": self.started_at}))

    def serve(self) -> None:
        while not self.stop:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                continue
            with conn:
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            msg = json.loads(data)
            self.lines.append(msg)
            if msg["origin"]["fromMode"] != self.accepts:
                self.held.append(msg)
                continue
            self.take(msg)

    def set_accepts(self, cls: str) -> None:
        self.accepts = cls
        for msg in [m for m in self.held if m["origin"]["fromMode"] == cls]:
            self.held.remove(msg)
            self.take(msg)

    def take(self, msg: dict) -> None:
        self.taken.append(msg)
        if self.transcript:  # a busy session queues it, its status does not change
            with self.transcript.open("a") as fh:
                fh.write(json.dumps({"type": "queue-operation", "operation": "enqueue",
                                     "content": msg["message"]["content"]}, ensure_ascii=False) + "\n")
        else:
            self.write_record("busy", int(time.time() * 1000))

    def info(self) -> dict:
        return {"pid": self.pid, "messagingSocketPath": str(self.sock_path), "sessionId": "sid"}

    def close(self) -> None:
        self.stop = True
        self.thread.join(1)
        self.srv.close()


class ModeAndClass(unittest.TestCase):
    def test_permission_mode_forms(self):
        self.assertEqual(claude_peer.permission_mode(["claude", "--permission-mode=auto"]), "auto")
        self.assertEqual(claude_peer.permission_mode(["claude", "--permission-mode", "plan"]), "plan")
        self.assertEqual(claude_peer.permission_mode(["claude"]), "default")
        self.assertEqual(claude_peer.permission_mode(["claude", "--dangerously-skip-permissions"]), "bypassPermissions")

    def test_classes_follow_the_cli_rule(self):
        # Checked against 2.1.318 and 2.1.322 on 2026-11-12: bypassPermissions holds anything
        # but "bypass"; auto, default and acceptEdits hold "bypass" and take "prompting".
        allow = "--allow-dangerously-skip-permissions"
        cases = {("--permission-mode=bypassPermissions", allow): "bypass",
                 ("--permission-mode=auto", allow): "prompting",
                 ("--permission-mode=default", allow): "prompting",
                 ("--permission-mode=acceptEdits",): "prompting",
                 ("--permission-mode=plan", allow): "prompting",  # audit of release 6, real 2.1.318/322
                 ("--permission-mode=plan",): "prompting"}
        for args, want in cases.items():
            self.assertEqual(claude_peer.attested_class(["claude", *args]), want, args)


class BypassDisabled(unittest.TestCase):
    """2026-11-12, switch to lt: a managed-settings.json from device management disables bypass; an account
    with empty server-managed settings gets it (the tab runs in "auto" though launched in
    bypassPermissions) and holds a "bypass" attestation; mt / na have server settings and
    ignore the file."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="cp-b-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.managed = self.root / "managed-settings.json"
        self.managed.write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}}))
        patcher = mock.patch.object(claude_peer, "MANAGED_SETTINGS", self.managed)
        patcher.start()
        self.addCleanup(patcher.stop)

    def account(self, remote=None, settings=None) -> Path:
        d = Path(tempfile.mkdtemp(prefix="acct-", dir=self.root))  # removed with self.root
        if remote is not None:
            (d / "remote-settings.json").write_text(json.dumps(remote))
        if settings is not None:
            (d / "settings.json").write_text(json.dumps(settings))
        return d

    def test_empty_server_settings_get_the_managed_file(self):
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={})))
        self.assertTrue(claude_peer.bypass_disabled(self.account()))

    def test_unattended_runs_ask_lt_for_auto_and_keep_bypass_elsewhere(self):
        """the owner, 2026-11-13: on lt relaunched runs go in auto; mt / na stay as they were."""
        lt, mt = self.account(remote={}), self.account(remote={"channelsEnabled": True})
        self.assertEqual(claude_peer.run_mode(lt, "bypassPermissions"), "auto")
        self.assertEqual(claude_peer.run_mode(mt, "bypassPermissions"), "bypassPermissions")
        self.assertEqual(claude_peer.run_mode(lt, "auto"), "auto")
        self.assertEqual(claude_peer.run_mode(lt, "dontAsk"), "dontAsk")
        args = ["-p", "go", "--permission-mode", "bypassPermissions", "--verbose"]
        self.assertEqual(claude_peer.run_args(lt, args), ["-p", "go", "--permission-mode", "auto", "--verbose"])
        self.assertEqual(claude_peer.run_args(mt, args), args)
        self.assertEqual(claude_peer.run_args(lt, ["-p", "go", "--permission-mode=bypassPermissions"]),
                         ["-p", "go", "--permission-mode=auto"])
        self.assertEqual(claude_peer.run_args(lt, ["-p", "go", "--dangerously-skip-permissions"]),
                         ["--permission-mode", "auto", "-p", "go"])
        # Audit of release 37: the mode stays where it was (a variadic flag or `--` must not
        # swallow it), and a skip flag beside an explicit mode is dropped too.
        self.assertEqual(claude_peer.run_args(lt, ["--add-dir", "/x", "--permission-mode", "bypassPermissions", "go"]),
                         ["--add-dir", "/x", "--permission-mode", "auto", "go"])
        self.assertEqual(claude_peer.run_args(lt, ["-p", "--permission-mode", "bypassPermissions", "--", "go"]),
                         ["-p", "--permission-mode", "auto", "--", "go"])
        self.assertEqual(claude_peer.run_args(lt, ["-p", "--", "--dangerously-skip-permissions"]),
                         ["-p", "--", "--dangerously-skip-permissions"])
        self.assertEqual(claude_peer.run_args(lt, ["--dangerously-skip-permissions", "--permission-mode", "default", "go"]),
                         ["--permission-mode", "auto", "go"])
        self.assertEqual(claude_peer.run_args(lt, ["--dangerously-skip-permissions", "--permission-mode=acceptEdits", "go"]),
                         ["--permission-mode=auto", "go"])
        self.assertEqual(claude_peer.run_args(lt, ["--permission-mode=dontAsk", "go"]), ["--permission-mode=dontAsk", "go"])
        # Audit of release 39: a restrictive mode is never widened, and nothing after `--` is touched.
        for mode in ("plan", "dontAsk"):
            self.assertEqual(claude_peer.run_args(lt, ["--dangerously-skip-permissions", "--permission-mode", mode, "go"]),
                             ["--permission-mode", mode, "go"])
            self.assertEqual(claude_peer.run_args(lt, ["--dangerously-skip-permissions", f"--permission-mode={mode}"]),
                             [f"--permission-mode={mode}"])
        self.assertEqual(
            claude_peer.run_args(lt, ["-p", "--permission-mode", "bypassPermissions", "--", "--dangerously-skip-permissions"]),
            ["-p", "--permission-mode", "auto", "--", "--dangerously-skip-permissions"])
        self.assertEqual(claude_peer.run_args(mt, ["--dangerously-skip-permissions", "--permission-mode=default", "go"]),
                         ["--dangerously-skip-permissions", "--permission-mode=default", "go"])
        self.assertEqual(claude_peer.run_args(lt, ["-p", "go", "--permission-mode", "dontAsk"]),
                         ["-p", "go", "--permission-mode", "dontAsk"])

    def test_fallback_and_headless_resume_use_the_account_mode(self):
        lt, mt = self.account(remote={}), self.account(remote={"channelsEnabled": True})
        fspec = importlib.util.spec_from_file_location("claude_with_fallback", SCRIPTS / "claude-with-fallback.py")
        cwf = importlib.util.module_from_spec(fspec)
        fspec.loader.exec_module(cwf)
        args = ["-p", "go", "--permission-mode", "bypassPermissions"]
        home = Path(tempfile.mkdtemp(prefix="home-", dir=self.root))
        (home / ".claude").mkdir()  # lt has no config_dir: it is HOME/.claude, empty server settings
        with mock.patch.dict(cwf.ACCOUNTS, {"lt": {"name": "L", "config_dir": None},
                                            "mt": {"name": "M", "config_dir": str(mt)}}), \
                mock.patch.object(cwf, "HOME", home):
            self.assertEqual(cwf.account_args("lt", args)[-1], "auto")
            self.assertEqual(cwf.account_args("mt", args), args)
        bypass_log = self.root / "t.jsonl"
        bypass_log.write_text(json.dumps({"type": "user", "permissionMode": "bypassPermissions"}) + "\n")
        auto_log = self.root / "a.jsonl"
        auto_log.write_text(json.dumps({"type": "user", "permissionMode": "acceptEdits"}) + "\n")
        with mock.patch.dict(cqr.ACCOUNTS, {"lt": {"dir": lt, "env": None}, "mt": {"dir": mt, "env": str(mt)}}):
            self.assertEqual(cqr.headless_mode({"path": str(bypass_log)}, "lt"), "auto")
            self.assertEqual(cqr.headless_mode({"path": str(bypass_log)}, "mt"), "bypassPermissions")
            self.assertEqual(cqr.headless_mode({"path": str(auto_log)}, "mt"), "auto")
            self.assertEqual(cqr.headless_mode({"path": ""}, "mt"), "auto")
            # Audit of release 40: a restrictive session mode is kept on every account.
            for mode in ("plan", "dontAsk"):
                log = self.root / f"{mode}.jsonl"
                log.write_text(json.dumps({"type": "user", "permissionMode": mode}) + "\n")
                for tag in ("lt", "mt"):
                    self.assertEqual(cqr.headless_mode({"path": str(log)}, tag), mode)

    def test_launchers_pass_the_account_mode_to_the_cli(self):
        """Audit of release 37: the launchers really use it, not only the helpers."""
        fspec = importlib.util.spec_from_file_location("claude_with_fallback2", SCRIPTS / "claude-with-fallback.py")
        cwf = importlib.util.module_from_spec(fspec)
        fspec.loader.exec_module(cwf)
        home = Path(tempfile.mkdtemp(prefix="home-", dir=self.root))
        (home / ".claude").mkdir()
        mt = self.account(remote={"channelsEnabled": True})
        seen = []

        def fake_run(cmd, **kw):
            seen.append((kw["env"].get("CLAUDE_CONFIG_DIR"), cmd[1:]))
            out = b"usage limit reached" if len(seen) == 1 else b"ok"
            return mock.Mock(returncode=1 if len(seen) == 1 else 0, stdout=out, stderr=b"")

        argv = ["x", "-p", "go", "--permission-mode", "bypassPermissions"]
        with mock.patch.dict(cwf.ACCOUNTS, {"lt": {"name": "L", "config_dir": None},
                                            "mt": {"name": "M", "config_dir": str(mt)}}), \
                mock.patch.object(cwf, "HOME", home), mock.patch.object(cwf, "ORDER", ["lt", "mt"]), \
                mock.patch.object(cwf, "STATE", self.root / "fb-state.json"), \
                mock.patch.object(cwf, "LOG", self.root / "fb.log"), mock.patch.object(cwf, "notify"), \
                mock.patch.object(cwf.subprocess, "run", fake_run), mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "stdin", None), mock.patch.object(sys, "stdout", mock.Mock()), \
                mock.patch.object(sys, "stderr", mock.Mock()):
            cwf.main()
        self.assertEqual(seen, [(None, ["-p", "go", "--permission-mode", "auto"]),
                                (str(mt), ["-p", "go", "--permission-mode", "bypassPermissions"])])

        log = self.root / "s.jsonl"
        log.write_text(json.dumps({"type": "user", "permissionMode": "bypassPermissions"}) + "\n")
        lt = self.account(remote={})
        launched = []
        with mock.patch.dict(cqr.ACCOUNTS, {"lt": {"dir": lt, "env": None}, "mt": {"dir": mt, "env": str(mt)}}), \
                mock.patch.object(cqr, "STATE_DIR", self.root), \
                mock.patch.object(cqr.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd) or mock.Mock(pid=1)):
            for tag in ("lt", "mt"):
                cqr.resume_headless({"cwd": str(self.root), "session": "s1", "path": str(log)}, tag, "sigue")
        self.assertEqual([c[c.index("--permission-mode") + 1] for c in launched], ["auto", "bypassPermissions"])

    def test_server_settings_replace_the_managed_file(self):
        self.assertFalse(claude_peer.bypass_disabled(self.account(remote={"channelsEnabled": True})))
        self.assertTrue(claude_peer.bypass_disabled(
            self.account(remote={"permissions": {"disableBypassPermissionsMode": "disable"}})))

    def test_user_settings_can_disable_it(self):
        self.assertTrue(claude_peer.bypass_disabled(
            self.account(remote={"channelsEnabled": True},
                         settings={"permissions": {"disableBypassPermissionsMode": "disable"}})))

    def test_no_managed_file_and_none(self):
        self.managed.unlink()
        self.assertFalse(claude_peer.bypass_disabled(self.account(remote={})))
        self.assertFalse(claude_peer.bypass_disabled(None))

    def test_cli_rules_for_content_dropins_and_merge(self):
        # Audit of release 31: what the CLI reads, not more.
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={"managedSourcesBehavior": "first-wins"})),
                        "a tier with only excluded keys does not count")
        self.assertTrue(claude_peer.bypass_disabled(
            self.account(remote={"channelsEnabled": True, "managedSourcesBehavior": "merge"})))
        self.managed.write_text("{}")
        self.assertFalse(claude_peer.bypass_disabled(self.account(remote={})))
        dropins = self.root / "managed-settings.d"
        dropins.mkdir()
        (dropins / "10-bypass.json").write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}}))
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={})), "a drop-in counts")
        self.managed.write_text(json.dumps({"disableBypassPermissionsMode": "disable"}))
        (dropins / "10-bypass.json").unlink()
        self.assertFalse(claude_peer.bypass_disabled(self.account(remote={})), "top level is not read by the CLI")

    def test_empty_objects_and_dotfile_dropins_do_not_count(self):
        # Audit of release 32: the CLI ranks a tier of empty objects below the file, and
        # skips drop-ins that are dotfiles or not regular files.
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={"permissions": {}, "env": {"a": {}}})))
        self.managed.write_text("{}")
        dropins = self.root / "managed-settings.d"
        dropins.mkdir()
        (dropins / "._10.json").write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}}))
        self.assertFalse(claude_peer.bypass_disabled(self.account(remote={})))

    def test_invalid_values_fail_closed_in_admin_tiers(self):
        # Audit of release 33: the CLI reads an invalid value as "disable".
        for value, want in ((True, True), ("Disable", True), (1, True), (False, False), ("false", False), (None, False)):
            self.managed.write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": value}}))
            self.assertEqual(claude_peer.bypass_disabled(self.account(remote={})), want, value)
        self.managed.write_text(json.dumps({"permissions": "nope"}))
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={})), "permissions not an object")
        self.managed.write_text("{}")
        self.assertFalse(claude_peer.bypass_disabled(self.account(
            remote={}, settings={"permissions": {"disableBypassPermissionsMode": True}})), "user settings: only disable")

    def test_a_later_dropin_cannot_clear_a_disable(self):
        dropins = self.root / "managed-settings.d"
        dropins.mkdir()
        (dropins / "20-x.json").write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": False}}))
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={})))

    def test_null_server_values_are_not_content(self):
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={"channelsEnabled": None})))

    def test_server_tier_is_ranked_as_the_cli_parses_it(self):
        # Audit of release 34: a false disable is absent, so the tier drops out and the file
        # wins; a tier of only invalid restrictive values ranks below the file.
        for remote in ({"permissions": {"disableBypassPermissionsMode": False}},
                       {"permissions": {"disableBypassPermissionsMode": "false"}},
                       {"permissions": {"disableAutoMode": True}}):
            self.assertTrue(claude_peer.bypass_disabled(self.account(remote=remote)), remote)
        self.assertFalse(claude_peer.bypass_disabled(self.account(
            remote={"channelsEnabled": True, "permissions": {"disableBypassPermissionsMode": False}})))

    def test_top_level_restrictive_keys_rank_like_the_cli(self):
        # Audit of release 35: top-level disableAutoMode is restrictive too.
        for remote in ({"disableAutoMode": False}, {"disableAutoMode": True}, {"disableDeepLinkRegistration": "x"}):
            self.assertTrue(claude_peer.bypass_disabled(self.account(remote=remote)), remote)
        self.assertFalse(claude_peer.bypass_disabled(self.account(remote={"disableAutoMode": "disable"})),
                         "a valid value is real content: the server tier wins")

    def test_server_tier_fails_closed_too(self):
        self.managed.write_text("{}")
        self.assertTrue(claude_peer.bypass_disabled(self.account(
            remote={"channelsEnabled": True, "permissions": {"disableBypassPermissionsMode": True}})))

    def test_zero_is_invalid_not_absent(self):
        self.managed.write_text(json.dumps({"permissions": {"disableBypassPermissionsMode": 0}}))
        self.assertTrue(claude_peer.bypass_disabled(self.account(remote={})))

    def test_launch_flag_is_downgraded_but_a_recorded_mode_wins(self):
        args = ["claude", "--permission-mode=bypassPermissions", "--allow-dangerously-skip-permissions"]
        self.assertEqual(claude_peer.attested_class(args, None, no_bypass=True), "prompting")
        self.assertEqual(claude_peer.attested_class(args, "bypassPermissions", no_bypass=True), "bypass")

    def test_deliver_attests_prompting_for_a_no_bypass_account(self):
        fake = FakeSession(accepts="prompting")
        self.addCleanup(fake.close)
        (fake.sessions.parent / "remote-settings.json").write_text("{}")
        with mock.patch.object(claude_peer, "process_args",
                               return_value=["claude", "--permission-mode=bypassPermissions"]):
            self.assertEqual(claude_peer.deliver(fake.info(), "continúa", fake.sessions, verify_seconds=1.5),
                             (True, "prompting"))


class CurrentMode(unittest.TestCase):
    """Audit of release 4: the CLI checks the live mode, the tab changes it at runtime."""

    def transcript(self, rows: list[dict]) -> Path:
        path = Path(tempfile.mkdtemp(prefix="cp-m-")) / "t.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return path

    BEFORE, AFTER = "2026-11-12T09:40:00.000Z", "2026-11-12T09:55:00.000Z"  # start 09:50

    def test_last_recorded_mode_since_start_wins(self):
        path = self.transcript([{"type": "user", "timestamp": self.AFTER, "permissionMode": "bypassPermissions"},
                                {"type": "assistant", "timestamp": self.AFTER},
                                {"type": "permission-mode", "permissionMode": "auto"}])
        self.assertEqual(claude_peer.transcript_mode(path, 1794477000000), "auto")

    def test_mode_from_before_the_process_started_is_ignored(self):
        # Release 5 regression found on a real 2.1.318 process: the transcript said auto from
        # an earlier run, the relaunched process ran in bypass and recorded nothing yet.
        path = self.transcript([{"type": "user", "timestamp": self.BEFORE, "permissionMode": "auto"},
                                {"type": "permission-mode", "permissionMode": "auto"}])
        self.assertIsNone(claude_peer.transcript_mode(path, 1794477000000))

    def test_no_mode_recorded_or_no_transcript(self):
        self.assertIsNone(claude_peer.transcript_mode(self.transcript([{"type": "user", "timestamp": self.AFTER}]), 1))
        self.assertIsNone(claude_peer.transcript_mode(None))
        self.assertIsNone(claude_peer.transcript_mode(Path("/nonexistent/t.jsonl")))

    def test_mode_overrides_the_launch_flag(self):
        args = ["claude", "--permission-mode=bypassPermissions", "--allow-dangerously-skip-permissions"]
        self.assertEqual(claude_peer.attested_class(args, "auto"), "prompting")
        self.assertEqual(claude_peer.attested_class(args, None), "bypass")

    def deliver_with(self, rows: list[dict], accepts: str):
        transcript = self.transcript(rows)
        fake = FakeSession(accepts=accepts, transcript=transcript)
        self.addCleanup(fake.close)
        with mock.patch.object(claude_peer, "process_args", return_value=["claude", "--permission-mode=bypassPermissions"]):
            return claude_peer.deliver(fake.info(), "continúa", fake.sessions, transcript, verify_seconds=1.5)

    def test_deliver_uses_the_mode_switched_in_the_tab(self):
        rows = [{"type": "user", "timestamp": self.AFTER, "permissionMode": "bypassPermissions"},
                {"type": "permission-mode", "permissionMode": "auto"}]
        self.assertEqual(self.deliver_with(rows, "prompting"), (True, "prompting"))

    def test_deliver_uses_the_launch_flag_over_an_old_mode(self):
        rows = [{"type": "user", "timestamp": self.BEFORE, "permissionMode": "auto"}]
        self.assertEqual(self.deliver_with(rows, "bypass"), (True, "bypass"))


class StartupAndLongTranscripts(unittest.TestCase):
    def test_nothing_is_sent_before_the_session_finished_starting(self):
        fake = FakeSession(accepts="prompting")
        self.addCleanup(fake.close)
        (fake.sessions / f"{fake.pid}.json").write_text(json.dumps(
            {"pid": fake.pid, "messagingSocketPath": str(fake.sock_path), "startedAt": fake.started_at}))
        with mock.patch.object(claude_peer, "process_args", return_value=["claude"]):
            ok, detail = claude_peer.deliver(fake.info(), "x", fake.sessions, None, verify_seconds=0.5, ready_seconds=0.6)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith(claude_peer.NOT_SENT), detail)
        self.assertEqual(fake.lines, [], "held as mode-unknown and released later: must not be sent early")

    def test_sent_once_startup_ends(self):
        fake = FakeSession(accepts="prompting")
        self.addCleanup(fake.close)
        (fake.sessions / f"{fake.pid}.json").write_text(json.dumps(
            {"pid": fake.pid, "messagingSocketPath": str(fake.sock_path), "startedAt": fake.started_at}))
        ready = threading.Timer(0.5, lambda: fake.write_record("idle", 1))
        ready.start()
        self.addCleanup(ready.cancel)
        with mock.patch.object(claude_peer, "process_args", return_value=["claude"]):
            self.assertEqual(claude_peer.deliver(fake.info(), "x", fake.sessions, None, verify_seconds=1.5,
                                                 ready_seconds=3), (True, "prompting"))

    def test_unknown_start_ignores_the_transcript(self):
        transcript = Path(tempfile.mkdtemp(prefix="cp-u-")) / "t.jsonl"
        transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-11-12T09:55:00Z",
                                          "permissionMode": "auto"}) + "\n")
        fake = FakeSession(accepts="bypass", transcript=transcript)
        self.addCleanup(fake.close)
        fake.started_at = 0
        fake.write_record("idle", 1)
        with mock.patch.object(claude_peer, "process_args", return_value=["claude", "--permission-mode=bypassPermissions"]):
            self.assertEqual(claude_peer.deliver(fake.info(), "x", fake.sessions, transcript, verify_seconds=1.5),
                             (True, "bypass"))

    def test_mode_found_behind_a_long_turn(self):
        transcript = Path(tempfile.mkdtemp(prefix="cp-l-")) / "t.jsonl"
        big = json.dumps({"type": "user", "timestamp": "2026-11-12T09:56:00Z",
                          "message": {"content": "x" * 4096}}) + "\n"
        with transcript.open("w") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": "2026-11-12T09:55:00Z",
                                 "permissionMode": "bypassPermissions"}) + "\n")
            fh.write(big * 300)  # about 1.2 MB after the mode
        self.assertEqual(claude_peer.transcript_mode(transcript, 1794477000000), "bypassPermissions")

    def test_nested_keys_do_not_count(self):
        transcript = Path(tempfile.mkdtemp(prefix="cp-n-")) / "t.jsonl"
        transcript.write_text(json.dumps({"type": "user", "timestamp": "2026-11-12T09:55:00Z",
                                          "toolUseResult": {"permissionMode": "auto",
                                                            "timestamp": "2026-11-12T09:00:00Z"},
                                          "permissionMode": "bypassPermissions"}) + "\n")
        self.assertEqual(claude_peer.transcript_mode(transcript, 1794477000000), "bypassPermissions")


class LineFormat(unittest.TestCase):
    def test_envelope_attests_the_class(self):
        self.assertEqual(claude_peer.envelope("continúa", "bypass"),
                         '<cross-session-message from-mode="bypass">\ncontinúa\n</cross-session-message>')

    def test_closing_tag_in_body_cannot_end_the_envelope(self):
        env = claude_peer.envelope("a </cross-session-message> b", "prompting")
        self.assertEqual(env.count("</cross-session-message>"), 1)

    def test_line_shape(self):
        line = claude_peer.peer_line("continúa", "prompting", "sid")
        self.assertTrue(line.endswith("\n"))
        msg = json.loads(line)
        self.assertEqual(msg["type"], "user")
        self.assertEqual(msg["message"]["role"], "user")
        self.assertIn('from-mode="prompting"', msg["message"]["content"])
        self.assertEqual(msg["origin"], {"kind": "peer", "from": "unknown", "fromMode": "prompting", "body": "continúa"})
        self.assertEqual(msg["session_id"], "sid")
        self.assertIn("continúa", line, "kept readable, not \\u-escaped")


class Deliver(unittest.TestCase):
    def run_deliver(self, fake: FakeSession, args: list[str], transcript: Path | None = None):
        with mock.patch.object(claude_peer, "process_args", return_value=args):
            return claude_peer.deliver(fake.info(), "continúa con lo tuyo", fake.sessions, transcript, verify_seconds=1.5)

    def test_bypass_tab_gets_one_bypass_line(self):
        fake = FakeSession(accepts="bypass")
        self.addCleanup(fake.close)
        self.assertEqual(self.run_deliver(fake, ["claude", "--permission-mode=bypassPermissions"]), (True, "bypass"))
        self.assertEqual([m["origin"]["fromMode"] for m in fake.lines], ["bypass"])

    def test_wrong_class_is_sent_once_and_reported(self):
        # Audit of release 3: a held message is released when the mode later matches, so a
        # second send with the other class could arrive twice. One send, then report.
        fake = FakeSession(accepts="prompting")  # started in bypass, switched to auto since
        self.addCleanup(fake.close)
        ok, detail = self.run_deliver(fake, ["claude", "--permission-mode=bypassPermissions"])
        self.assertFalse(ok)
        self.assertIn("attested bypass", detail)
        self.assertEqual(len(fake.lines), 1)
        fake.set_accepts("bypass")  # the owner switches the tab back: the held one arrives, once
        self.assertEqual(len(fake.taken), 1)

    def test_waiting_session_going_busy_on_its_own_is_not_delivery(self):
        # Audit of release 3: an answered prompt turns waiting -> busy with a fresh
        # statusUpdatedAt; without the transcript line that is not our message.
        fake = FakeSession(accepts=None)
        self.addCleanup(fake.close)
        fake.write_record("waiting", 1)
        flip = threading.Timer(0.3, lambda: fake.write_record("busy", int(time.time() * 1000)))
        flip.start()
        self.addCleanup(flip.cancel)
        self.assertFalse(self.run_deliver(fake, ["claude", "--permission-mode=default"])[0])

    def test_busy_session_counts_when_the_transcript_queues_it(self):
        transcript = Path(tempfile.mkdtemp(prefix="cp-t-")) / "t.jsonl"
        transcript.write_text('{"type":"user"}\n')
        fake = FakeSession(accepts="prompting", busy_now=True, transcript=transcript)
        self.addCleanup(fake.close)
        self.assertEqual(self.run_deliver(fake, ["claude", "--permission-mode=default"], transcript), (True, "prompting"))

    def test_already_busy_record_is_not_mistaken_for_delivery(self):
        fake = FakeSession(accepts=None, busy_now=True)
        self.addCleanup(fake.close)
        self.assertFalse(self.run_deliver(fake, ["claude", "--permission-mode=default"])[0])

    def test_gone_process_is_not_sent_and_says_so(self):
        ok, detail = claude_peer.deliver({"pid": 1, "messagingSocketPath": "/nonexistent/x.sock"}, "hi",
                                         Path("/nonexistent"), verify_seconds=0.1)
        self.assertEqual((ok, detail), (False, claude_peer.NOT_SENT + ": the session's process is gone"))

    def test_dead_socket(self):
        # Audit of release 7: the socket-error branch had no real test.
        fake = FakeSession(accepts="prompting")
        fake.close()  # nobody listens any more, the record stays
        with mock.patch.object(claude_peer, "process_args", return_value=["claude"]):
            ok, detail = claude_peer.deliver(fake.info(), "hi", fake.sessions, verify_seconds=0.1)
        self.assertFalse(ok)
        self.assertTrue(detail.startswith(claude_peer.NOT_SENT + ": socket error"), detail)

    def test_history_copies_and_own_turns_are_not_delivery(self):
        # Audit of release 7: api-request-blob lines carry the history, and a task
        # notification can start a turn; neither means the held message was taken.
        transcript = Path(tempfile.mkdtemp(prefix="cp-h-")) / "t.jsonl"
        transcript.write_text('{"type":"user"}\n')
        fake = FakeSession(accepts=None)
        self.addCleanup(fake.close)

        def noise():
            with transcript.open("a") as fh:
                fh.write(json.dumps({"type": "api-request-blob", "content": "continúa con lo tuyo"}) + "\n")
            fake.write_record("busy", int(time.time() * 1000))
        t = threading.Timer(0.3, noise)
        t.start()
        self.addCleanup(t.cancel)
        self.assertFalse(self.run_deliver(fake, ["claude", "--permission-mode=default"], transcript)[0])

    def test_record_without_socket(self):
        self.assertEqual(claude_peer.deliver({"pid": 1}, "hi", Path("/x"))[0], False)


class QuotaResumeTick(unittest.TestCase):
    """claude-quota-resume.py: probe the quota, then deliver per chat through claude_peer."""

    def chat(self, sid: str) -> dict:
        t = cqr.now() - cqr.dt.timedelta(hours=1)
        return {"session": sid, "path": f"/x/{sid}.jsonl", "limit_uuid": f"u-{sid}", "limit_at": t,
                "reset_at": t, "limit_text": "", "cwd": "/", "entrypoint": "claude-vscode"}

    def tick(self, probe, deliver_result):
        chats = [self.chat("a"), self.chat("b")]
        live = {"a": {"pid": 1, "account": "mt", "socket": "/s/1", "name": "A", "status": "idle"},
                "b": {"pid": 2, "account": "mt", "socket": "/s/2", "name": "B", "status": "idle"}}
        saved = {}
        with mock.patch.object(cqr, "stopped_chats", return_value=chats), \
             mock.patch.object(cqr, "live_processes", return_value=live), \
             mock.patch.object(cqr, "load_state", return_value={}), \
             mock.patch.object(cqr, "save_state", side_effect=lambda s: saved.update(s)), \
             mock.patch.object(cqr, "quota_open", return_value=probe), \
             mock.patch.object(cqr.claude_peer, "deliver", side_effect=deliver_result) as deliver, \
             mock.patch.object(cqr, "progress_after", return_value="running"), \
             mock.patch.object(cqr, "notify"), mock.patch.object(cqr, "log"), \
             mock.patch.object(cqr.time, "sleep"), redirect_stdout(io.StringIO()):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=False)
        return saved, deliver

    def test_closed_quota_sends_nothing(self):
        saved, deliver = self.tick((False, "limit"), lambda *a, **k: (True, "bypass"))
        deliver.assert_not_called()
        self.assertEqual({v["status"] for v in saved.values()}, {"waiting"})

    def test_open_quota_delivers_each_and_records_the_held_one(self):
        saved, deliver = self.tick((True, "ok"), lambda info, *a, **k: (info["pid"] == 1, "x"))
        self.assertEqual(deliver.call_count, 2)
        self.assertEqual(saved["u-a"]["status"], "done")
        self.assertEqual(saved["u-a"]["attempts"], 1)
        self.assertEqual(saved["u-b"]["status"], "held")
        self.assertEqual(saved["u-b"]["attempts"], 1)
        self.assertEqual(saved["u-b"]["last_error"], "x")

    def test_held_chat_is_not_retried_next_tick(self):
        chats = [self.chat("b")]
        live = {"b": {"pid": 2, "account": "mt", "socket": "/s/2", "name": "B", "status": "idle"}}
        with mock.patch.object(cqr, "stopped_chats", return_value=chats), \
             mock.patch.object(cqr, "live_processes", return_value=live), \
             mock.patch.object(cqr, "load_state", return_value={"u-b": {"status": "held", "attempts": 1}}), \
             mock.patch.object(cqr, "quota_open") as probe, \
             mock.patch.object(cqr, "save_state"), mock.patch.object(cqr, "notify"), \
             mock.patch.object(cqr.claude_peer, "deliver") as deliver, mock.patch.object(cqr, "log"):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=False)
        probe.assert_not_called()
        deliver.assert_not_called()

    def test_now_still_leaves_held_chats_alone(self):
        chats = [self.chat("b")]
        live = {"b": {"pid": 2, "account": "mt", "socket": "/s/2", "name": "B", "status": "idle"}}
        with mock.patch.object(cqr, "stopped_chats", return_value=chats), \
             mock.patch.object(cqr, "live_processes", return_value=live), \
             mock.patch.object(cqr, "load_state", return_value={"u-b": {"status": "held", "attempts": 1}}), \
             mock.patch.object(cqr, "quota_open") as probe, \
             mock.patch.object(cqr, "save_state"), mock.patch.object(cqr, "notify"), \
             mock.patch.object(cqr.claude_peer, "deliver") as deliver, mock.patch.object(cqr, "log"):
            cqr.tick(dry_run=False, ignore_reset=True, include_closed=False)
        probe.assert_not_called()
        deliver.assert_not_called()

    def test_socket_error_is_retried_not_held(self):
        err = claude_peer.NOT_SENT + ": socket error ([Errno 61] Connection refused)"
        saved, _ = self.tick((True, "ok"), lambda info, *a, **k: (info["pid"] == 1, err))
        self.assertEqual(saved["u-b"]["status"], "waiting")
        self.assertEqual(saved["u-a"]["status"], "done")

    def test_closed_chats_are_probed_and_use_the_default_account(self):
        # Audit of release 21: closed chats skipped the quota probe and always went to lt.
        chats = [self.chat("c")]
        probed, resumed = [], []
        with mock.patch.object(cqr, "stopped_chats", return_value=chats), \
             mock.patch.object(cqr, "live_processes", return_value={}), \
             mock.patch.object(cqr, "load_state", return_value={}), \
             mock.patch.object(cqr, "save_state"), mock.patch.object(cqr, "notify"), mock.patch.object(cqr, "log"), \
             mock.patch.object(cqr, "default_account", return_value="na"), \
             mock.patch.object(cqr, "quota_open", side_effect=lambda tag: probed.append(tag) or (False, "limit")), \
             mock.patch.object(cqr, "resume_headless", side_effect=lambda c, tag, t: resumed.append(tag) or (True, "x")):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=True)
        self.assertEqual(probed, ["na"])
        self.assertEqual(resumed, [], "quota still closed: nothing resumed")

    def test_include_closed_takes_chats_already_reported_closed(self):
        # Audit of release 23: --status suggests --include-closed for a closed_reported chat.
        resumed = []
        with mock.patch.object(cqr, "stopped_chats", return_value=[self.chat("c")]), \
             mock.patch.object(cqr, "live_processes", return_value={}), \
             mock.patch.object(cqr, "load_state", return_value={"u-c": {"status": "closed_reported"}}), \
             mock.patch.object(cqr, "save_state"), mock.patch.object(cqr, "notify"), mock.patch.object(cqr, "log"), \
             mock.patch.object(cqr, "default_account", return_value="mt"), \
             mock.patch.object(cqr, "quota_open", return_value=(True, "ok")), \
             mock.patch.object(cqr, "progress_after", return_value="running"), \
             mock.patch.object(cqr.time, "sleep"), \
             mock.patch.object(cqr, "resume_headless", side_effect=lambda c, tag, t: resumed.append(tag) or (True, "x")):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=True)
        self.assertEqual(resumed, ["mt"])

    def test_headless_resumed_chat_hitting_the_limit_again_is_seen(self):
        # Audit of release 24: the claude -p resume writes entrypoint sdk-cli.
        root = Path(tempfile.mkdtemp(prefix="cqr-")) / "projects"
        (root / "x").mkdir(parents=True)
        ts = (cqr.now() - cqr.dt.timedelta(minutes=30)).astimezone(cqr.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        limit = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "uuid": "L2",
                 "entrypoint": "sdk-cli", "timestamp": ts,
                 "message": {"content": [{"type": "text", "text": "You've hit your 5-hour limit"}]}}
        (root / "x" / "sess1.jsonl").write_text(json.dumps(limit) + "\n")
        with mock.patch.object(cqr, "PROJECTS", root):
            self.assertEqual(cqr.stopped_chats(), [])
            self.assertEqual([c["session"] for c in cqr.stopped_chats({"sess1"})], ["sess1"])
        self.assertEqual(cqr.headless_sessions({"a": {"session": "sess1", "headless": True}, "b": {"session": "s2"}}),
                         {"sess1"})

    def test_local_command_after_the_limit_keeps_the_chat_stopped(self):
        # Audit of auto switch release 18: /usage typed in a cut tab hid it from the wake-up.
        root = Path(tempfile.mkdtemp(prefix="cqr-")) / "projects"
        (root / "x").mkdir(parents=True)
        ts = (cqr.now() - cqr.dt.timedelta(minutes=30)).astimezone(cqr.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows = [{"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "uuid": "L3",
                 "entrypoint": "claude-vscode", "timestamp": ts,
                 "message": {"content": [{"type": "text", "text": "You've hit your 5-hour limit"}]}},
                {"type": "user", "timestamp": ts, "message": {"content": "<command-name>/usage</command-name>"}},
                {"type": "user", "timestamp": ts, "message": {"content": "<local-command-stdout>x</local-command-stdout>"}}]
        (root / "x" / "sess3.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        with mock.patch.object(cqr, "PROJECTS", root):
            self.assertEqual([c["session"] for c in cqr.stopped_chats()], ["sess3"])

    def test_closed_chat_with_a_state_is_reported_keeping_its_attempts(self):
        # Audit of release 25: a tab that had a state and then closed must be reported,
        # and its attempts kept so MAX_ATTEMPTS still bounds manual runs.
        saved = {}
        state = {"u-c": {"session": "c", "status": "waiting", "attempts": 2, "headless": True}}
        with mock.patch.object(cqr, "stopped_chats", return_value=[self.chat("c")]), \
             mock.patch.object(cqr, "live_processes", return_value={}), \
             mock.patch.object(cqr, "load_state", return_value=state), \
             mock.patch.object(cqr, "save_state", side_effect=lambda s: saved.update(s)), \
             mock.patch.object(cqr, "notify") as notify, mock.patch.object(cqr, "log"):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=False)
        self.assertEqual(saved["u-c"]["status"], "closed_reported")
        self.assertEqual((saved["u-c"]["attempts"], saved["u-c"]["headless"]), (2, True))
        notify.assert_called_once()

    def run_tick(self, state, live, include_closed=False):
        sent = []
        with mock.patch.object(cqr, "stopped_chats", return_value=[self.chat("c")]), \
             mock.patch.object(cqr, "live_processes", return_value=live), \
             mock.patch.object(cqr, "load_state", return_value=state), \
             mock.patch.object(cqr, "save_state"), mock.patch.object(cqr, "notify") as notify, \
             mock.patch.object(cqr, "log"), mock.patch.object(cqr.time, "sleep"), \
             mock.patch.object(cqr, "quota_open", return_value=(True, "ok")), \
             mock.patch.object(cqr, "progress_after", return_value="running"), \
             mock.patch.object(cqr.claude_peer, "deliver", side_effect=lambda info, *a, **k: sent.append(info) or (True, "x")):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=include_closed)
        return sent, notify

    def test_reported_closed_chat_with_its_tab_back_is_woken(self):
        live = {"c": {"pid": 3, "account": "mt", "socket": "/s/3", "name": "C", "status": "idle"}}
        sent, _ = self.run_tick({"u-c": {"session": "c", "status": "closed_reported"}}, live)
        self.assertEqual(len(sent), 1)

    def test_held_chat_whose_tab_is_gone_is_reported_closed(self):
        sent, notify = self.run_tick({"u-c": {"session": "c", "status": "held", "attempts": 1}}, {})
        self.assertEqual(sent, [])
        notify.assert_called_once()

    def test_held_chat_with_its_tab_open_is_left_alone(self):
        live = {"c": {"pid": 3, "account": "mt", "socket": "/s/3", "name": "C", "status": "idle"}}
        sent, _ = self.run_tick({"u-c": {"session": "c", "status": "held", "attempts": 1}}, live)
        self.assertEqual(sent, [])

    def test_closed_chat_is_reported_once_even_after_a_manual_retry(self):
        # Audit of release 27: a manual run that leaves it "waiting" must not make the
        # agent report it again.
        state = {"u-c": {"session": "c", "status": "waiting", "attempts": 1, "reported": True}}
        _, notify = self.run_tick(state, {})
        notify.assert_not_called()

    def test_held_chat_whose_tab_restarted_is_woken(self):
        # Audit of release 27: a new process means the parked message is gone.
        live = {"c": {"pid": 9, "account": "mt", "socket": "/s/9", "name": "C", "status": "idle"}}
        with mock.patch.object(cqr, "pid_alive", return_value=False):
            sent, _ = self.run_tick({"u-c": {"session": "c", "status": "held", "attempts": 1, "pid": 3}}, live)
        self.assertEqual(len(sent), 1)
        # Audit of release 28: another live process of the same session is no proof; the
        # holder must be gone.
        with mock.patch.object(cqr, "pid_alive", return_value=True):
            sent, _ = self.run_tick({"u-c": {"session": "c", "status": "held", "attempts": 1, "pid": 3}}, live)
        self.assertEqual(sent, [])
        sent, _ = self.run_tick({"u-c": {"session": "c", "status": "held", "attempts": 1, "pid": 9}}, live)
        self.assertEqual(sent, [])

    def test_now_on_a_reported_closed_chat_says_why(self):
        state = {"u-c": {"session": "c", "status": "closed_reported", "reported": True}}
        with mock.patch.object(cqr, "stopped_chats", return_value=[self.chat("c")]), \
             mock.patch.object(cqr, "live_processes", return_value={}), \
             mock.patch.object(cqr, "load_state", return_value=state), \
             mock.patch.object(cqr, "save_state"), mock.patch.object(cqr, "notify"), mock.patch.object(cqr, "log"), \
             mock.patch.object(cqr.sys.stdin, "isatty", return_value=True):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cqr.tick(dry_run=False, ignore_reset=True, include_closed=False)
        self.assertIn("closed, already reported (use --include-closed)", buf.getvalue())

    def test_install_writes_the_include_closed_variable(self):
        with mock.patch.dict(os.environ, {"QUOTA_RESUME_INCLUDE_CLOSED": "1"}):
            self.assertIn("<key>QUOTA_RESUME_INCLUDE_CLOSED</key><string>1</string>", cqr.env_block())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cqr.env_block(), "")

    def test_same_name_chats_keep_their_own_error(self):
        chats = [self.chat("a"), self.chat("b")]
        live = {"a": {"pid": 1, "account": "mt", "socket": "/s/1", "name": "Same", "status": "idle"},
                "b": {"pid": 2, "account": "mt", "socket": "/s/2", "name": "Same", "status": "idle"}}
        saved = {}
        with mock.patch.object(cqr, "stopped_chats", return_value=chats), \
             mock.patch.object(cqr, "live_processes", return_value=live), \
             mock.patch.object(cqr, "load_state", return_value={}), \
             mock.patch.object(cqr, "save_state", side_effect=lambda s: saved.update(s)), \
             mock.patch.object(cqr, "quota_open", return_value=(True, "ok")), \
             mock.patch.object(cqr.claude_peer, "deliver", side_effect=lambda info, *a, **k: (False, f"err{info['pid']}")), \
             mock.patch.object(cqr, "notify"), mock.patch.object(cqr, "log"), redirect_stdout(io.StringIO()):
            cqr.tick(dry_run=False, ignore_reset=False, include_closed=False)
        self.assertEqual((saved["u-a"]["last_error"], saved["u-b"]["last_error"]), ("err1", "err2"))


if __name__ == "__main__":
    unittest.main()
