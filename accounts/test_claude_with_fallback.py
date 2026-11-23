"""Tests for accounts/claude-with-fallback.py: the try order (L-194) and the streamed
stream-json output (L-195).

The order is tested as a pure function and through the module's readers; the end-to-end
cases run the wrapper as a subprocess with HOME in a temp dir (so the active file, the
cooldown state and the log are synthetic) and a fake `claude` (CLAUDE_REAL_BIN) that
answers per account. No real CLI is run and no quota is used.

Run from the repository root:
    python3 -m unittest accounts/test_claude_with_fallback.py -v
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent))
import testsupport  # noqa: F401,E402  (synthetic accounts config, before any tool loads)

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "claude-with-fallback.py"
spec = importlib.util.spec_from_file_location("claude_with_fallback_t", SCRIPT)
cwf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cwf)

# A fake CLI: says which account ran it; FAKE_SPENT lists accounts out of quota.
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
tag = cfg.rsplit(".", 1)[-1] if cfg else "lt"
with open(os.environ["FAKE_CALLS"], "a") as fh:
    fh.write(json.dumps({"tag": tag, "args": sys.argv[1:]}) + "\n")
spent = tag in os.environ.get("FAKE_SPENT", "").split(",")
mode = os.environ.get("FAKE_MODE_" + tag, "")
stream = "stream-json" in sys.argv
sid = "s-" + tag
if os.environ.get("FAKE_RESUME_SID") and "--resume" in sys.argv:
    sid = sys.argv[sys.argv.index("--resume") + 1]  # a resumed session keeps its id
data = sys.stdin.buffer.read()
if os.environ.get("FAKE_PIDS"):
    with open(os.environ["FAKE_PIDS"], "a") as fh:
        fh.write(f"{os.getpid()}\n")
if mode == "sleep":
    import time
    time.sleep(60)
if mode == "close_stdout":  # the auditor's SIGTERM repro: stdout closed, child still running
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
    os.close(1)
    import time
    time.sleep(30)
    sys.exit(0)
if stream and mode != "work_then_limit_nosid":
    print(json.dumps({"type": "system", "subtype": "init", "session_id": sid}), flush=True)
if mode == "work_then_limit_nosid":
    print(json.dumps({"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "working"}]}}), flush=True)
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "You've hit your usage limit", "num_turns": 3}), flush=True)
    sys.exit(1)
if mode == "work_error":  # a resume that does work (pushes, prints DONE), then fails
    print(json.dumps({"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "name": "Bash"}]}}), flush=True)
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "pushed"}]}}), flush=True)
    print(json.dumps({"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "<desk-status>DONE</desk-status>"}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "", "num_turns": 9, "session_id": sid}), flush=True)
    sys.exit(1)
if mode == "no_conversation":  # a resume that fails for another reason than a limit
    print(json.dumps({"type": "result", "is_error": True, "subtype": "error_during_execution",
                      "result": "No conversation found with session ID", "session_id": sid}), flush=True)
    sys.exit(1)
if mode == "transient_notice":  # a stream-json rate_limit notice flagged transient
    print(json.dumps({"type": "assistant", "error": "rate_limit", "apiErrorIsTransient": True, "isApiErrorMessage": True,
                      "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: 429 rate_limit_error"}]}}), flush=True)
    print(json.dumps({"type": "result", "is_error": True, "result": "", "session_id": sid}), flush=True)
    sys.exit(1)
if mode == "transient":
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error: 429 {\"type\":\"rate_limit_error\"}", "session_id": sid}), flush=True)
    sys.exit(1)
if mode == "expired":
    print(json.dumps({"type": "result", "is_error": True, "result": "OAuth session expired and could not be refreshed", "session_id": sid}), flush=True)
    sys.exit(1)
if mode == "toolquota":  # a tool result that talks about quota, then a non-quota failure
    print(json.dumps({"type": "assistant", "message": {"model": "m", "content": [{"type": "tool_use", "name": "Bash"}]}}), flush=True)
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ERROR: Quota exceeded for quota metric x, 429 unauthorized 401"}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "", "session_id": "s-" + tag}), flush=True)
    sys.exit(1)
if mode == "work_then_limit":  # did work, then the limit
    print(json.dumps({"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "working"}]}}), flush=True)
    print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429, "result": "You've hit your usage limit", "num_turns": 3, "session_id": sid}), flush=True)
    sys.exit(1)
if mode == "overloaded":
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error: 529 overloaded_error", "session_id": "s-" + tag}), flush=True)
    sys.exit(1)
if mode == "stdin_len":
    print(json.dumps({"type": "result", "is_error": False, "result": str(len(data)), "session_id": "s-" + tag}))
    sys.exit(0)
if spent:
    out = {"type": "result", "is_error": True, "result": "You've hit your usage limit", "session_id": "s-" + tag}
    print(json.dumps(out), flush=True)
    sys.stderr.write("err-" + tag + "\n")
    sys.exit(1)
print(json.dumps({"type": "result", "is_error": False, "result": "answer from " + tag, "session_id": sid}))
'''


class TryOrder(unittest.TestCase):
    def test_active_first_then_the_order(self):
        self.assertEqual(cwf.try_order(["lt", "mt", "na"], "na", None, False, set()), ["na", "lt", "mt"])

    def test_each_account_once(self):
        self.assertEqual(cwf.try_order(["mt", "lt", "mt", "na", "lt"], "mt", None, False, set()), ["mt", "lt", "na"])

    def test_unknown_or_missing_active_keeps_the_order(self):
        self.assertEqual(cwf.try_order(["lt", "mt", "na"], None, None, False, set()), ["lt", "mt", "na"])
        self.assertEqual(cwf.try_order(["lt", "mt", "na"], "zz", None, False, set()), ["lt", "mt", "na"])

    def test_explicit_order_is_used_as_is(self):
        self.assertEqual(cwf.try_order(["mt", "lt"], "na", None, True, set()), ["mt", "lt"])

    def test_pinned_account_goes_first_even_with_an_explicit_order(self):
        self.assertEqual(cwf.try_order(["lt", "mt", "na"], "mt", "na", False, set()), ["na", "lt", "mt"])
        self.assertEqual(cwf.try_order(["lt", "mt"], "mt", "na", True, set()), ["na", "lt", "mt"])

    def test_cooling_accounts_go_last_but_a_pin_stays_first(self):
        self.assertEqual(cwf.try_order(["lt", "mt", "na"], "lt", None, False, {"lt"}), ["mt", "na", "lt"])
        self.assertEqual(cwf.try_order(["lt", "mt", "na"], "lt", "na", False, {"na", "mt"}), ["na", "lt", "mt"])


class Readers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cwf-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_order_file_first_then_every_other_account(self):
        f = self.tmp / "claude-order.json"
        f.write_text(json.dumps({"order": ["na", "lt"]}))
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNTS_ORDER_FILE": str(f)}), \
                mock.patch.object(cwf, "EXPLICIT_ORDER", ""):
            self.assertEqual(cwf.effective_order(), ["na", "lt", "mt"])

    def test_no_order_file_is_the_config_order(self):
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNTS_ORDER_FILE": str(self.tmp / "none.json")}), \
                mock.patch.object(cwf, "EXPLICIT_ORDER", ""):
            self.assertEqual(cwf.effective_order(), ["lt", "mt", "na"])

    def test_explicit_order_wins_over_the_file_and_keeps_only_its_accounts(self):
        f = self.tmp / "claude-order.json"
        f.write_text(json.dumps({"order": ["na", "lt"]}))
        with mock.patch.dict(os.environ, {"CLAUDE_ACCOUNTS_ORDER_FILE": str(f)}), \
                mock.patch.object(cwf, "EXPLICIT_ORDER", "mt, zz,lt"):
            self.assertEqual(cwf.effective_order(), ["mt", "lt"])

    def test_active_account_reads_the_active_file(self):
        active = self.tmp / "active"
        with mock.patch.object(cwf, "ACTIVE", active):
            self.assertIsNone(cwf.active_account(), "missing")
            active.write_text("na\n")
            self.assertEqual(cwf.active_account(), "na")
            active.write_text("zz\n")
            self.assertIsNone(cwf.active_account(), "unknown tag")

    def test_wants_stream(self):
        self.assertTrue(cwf.wants_stream(["-p", "--output-format", "stream-json", "x"]))
        self.assertTrue(cwf.wants_stream(["-p", "--output-format=stream-json"]))
        self.assertFalse(cwf.wants_stream(["-p", "--output-format", "json"]))
        self.assertFalse(cwf.wants_stream(["-p", "--", "--output-format", "stream-json"]))


class _Wrapper(unittest.TestCase):
    """The wrapper as a subprocess, HOME and every state file in a temp dir."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="cwf-home-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        (self.home / ".claude-shared" / "projects").mkdir(parents=True)
        for d in (".claude", ".claude.mt", ".claude.na"):
            (self.home / d).mkdir()
            (self.home / d / "projects").symlink_to(self.home / ".claude-shared" / "projects")  # the shared pool
        self.fake = self.home / "claude"
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.calls = self.home / "calls.jsonl"
        self.order_file = self.home / "claude-order.json"

    def run_wrapper(self, args, active=None, spent="", order=None, extra=None, stdin=None):
        if active:
            (self.home / ".claude-active").write_text(active + "\n")
        if order:
            self.order_file.write_text(json.dumps({"order": order}))
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_FALLBACK_ORDER", "CLAUDE_FALLBACK_ACCOUNT", "CLAUDECODE")}
        env.update(HOME=str(self.home), CLAUDE_REAL_BIN=str(self.fake), FAKE_CALLS=str(self.calls),
                   FAKE_SPENT=spent, CLAUDE_ACCOUNTS_ORDER_FILE=str(self.order_file),
                   LATIDOS_CLAUDE_ACCOUNTS=str(HERE / "testdata" / "claude-accounts.json"))
        env.update(extra or {})
        io_args = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        p = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, env=env, timeout=60, **io_args)
        tried = [json.loads(line)["tag"] for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return p, tried


class EndToEnd(_Wrapper):
    def test_starts_on_the_active_account(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "hi"], active="na")
        self.assertEqual(tried, ["na"])
        self.assertEqual(json.loads(p.stdout)["result"], "answer from na")
        self.assertEqual(p.returncode, 0)

    def test_falls_back_by_the_order_file_skipping_the_active_one(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "hi"], active="mt", spent="mt,na",
                                    order=["na", "mt", "lt"])
        self.assertEqual(tried, ["mt", "na", "lt"])
        self.assertEqual(json.loads(p.stdout)["result"], "answer from lt", "buffered: only the answer")
        self.assertEqual(p.stderr, b"", "a failed attempt's stderr is not returned in buffered mode")

    def test_pinned_account_wins_over_the_active_one(self):
        _, tried = self.run_wrapper(["-p", "hi"], active="mt", extra={"CLAUDE_FALLBACK_ACCOUNT": "na"})
        self.assertEqual(tried, ["na"])

    def test_explicit_order_skips_active_first(self):
        _, tried = self.run_wrapper(["-p", "hi"], active="na", spent="mt", extra={"CLAUDE_FALLBACK_ORDER": "mt,lt"})
        self.assertEqual(tried, ["mt", "lt"])

    def test_stream_json_is_streamed_and_ends_on_the_answer(self):
        args = ["-p", "--output-format", "stream-json", "--verbose", "--resume", "abc", "--model", "opus", "go"]
        p, tried = self.run_wrapper(args, active="na", spent="na")
        self.assertEqual(tried, ["na", "lt"])
        events = [json.loads(line) for line in p.stdout.decode().splitlines()]
        self.assertEqual([e.get("session_id") for e in events], ["s-na", "s-na", None, "s-lt", "s-lt"])
        self.assertEqual(events[2], {"type": "system", "subtype": "account_fallback", "from": "na", "to": "lt"})
        self.assertEqual(events[-1], {"type": "result", "is_error": False, "result": "answer from lt", "session_id": "s-lt"})
        self.assertEqual(p.returncode, 0)
        flags = [json.loads(line)["args"] for line in self.calls.read_text().splitlines()]
        self.assertEqual(flags, [args, args], "every flag passes through untouched")

    def test_every_account_spent_returns_the_last_limit_error(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="mt", spent="lt,mt,na")
        self.assertEqual(tried, ["mt", "lt", "na"])
        last = json.loads(p.stdout.decode().splitlines()[-1])
        self.assertTrue(last["is_error"])
        self.assertIn("usage limit", last["result"])
        self.assertEqual(p.returncode, 1)
        # The cooldown state is written: a second call tries the cooled accounts in order, once each.
        self.calls.unlink()
        _, tried = self.run_wrapper(["-p", "go"], spent="lt,mt,na")
        self.assertEqual(tried, ["mt", "lt", "na"])

    def test_interactive_call_execs_untouched(self):
        p, tried = self.run_wrapper(["--version"], active="na")
        self.assertEqual(tried, ["lt"], "no -p: the real CLI under the caller's environment")
        self.assertEqual(p.returncode, 0)


class LimitDetection(unittest.TestCase):
    """Audit of 103, M1: only the failure itself names a limit."""

    @staticmethod
    def lines(*evs) -> bytes:
        return ("\n".join(json.dumps(e) for e in evs) + "\n").encode()

    def test_tool_result_mentioning_quota_is_not_a_limit(self):
        out = self.lines({"type": "user", "message": {"content": [{"type": "tool_result", "content": "Quota exceeded 429 401 unauthorized"}]}},
                         {"type": "assistant", "message": {"model": "m", "content": [{"type": "text", "text": "the API said rate limit"}]}},
                         {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
        self.assertIsNone(cwf.out_of_tokens("lt", 1, out, b""))

    def test_overloaded_is_not_a_limit(self):
        out = self.lines({"type": "result", "is_error": True, "result": "API Error: 529 overloaded_error"})
        self.assertIsNone(cwf.out_of_tokens("lt", 1, out, b""))

    def test_successful_answer_mentioning_quota_is_not_a_limit(self):
        out = self.lines({"type": "result", "is_error": False, "result": "your quota of 429 requests is fine"})
        self.assertIsNone(cwf.out_of_tokens("lt", 0, out, b""))
        self.assertIsNone(cwf.out_of_tokens("lt", 0, b"plain answer about the usage limit\n", b""))

    def test_real_limits_are_seen(self):
        self.assertIn("429", cwf.out_of_tokens("lt", 1, self.lines({"type": "result", "is_error": True, "api_error_status": 429, "result": "x"}), b""))
        self.assertTrue(cwf.out_of_tokens("lt", 1, self.lines({"type": "result", "is_error": True, "result": "You've hit your session limit"}), b""))
        self.assertTrue(cwf.out_of_tokens("lt", 1, self.lines({"type": "error", "error": "rate_limit_error"}), b""))
        self.assertTrue(cwf.out_of_tokens("lt", 1, b"", b"Please run /login"))
        self.assertTrue(cwf.out_of_tokens("lt", 1, b"Claude AI usage limit reached\n", b""), "text output")
        synthetic = {"type": "assistant", "error": "rate_limit", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your limit"}]}}
        self.assertTrue(cwf.out_of_tokens("lt", 1, self.lines(synthetic, {"type": "result", "is_error": True, "result": ""}), b""))

    def test_progress(self):
        self.assertTrue(cwf.is_progress({"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}}))
        self.assertFalse(cwf.is_progress({"type": "user", "message": {"content": "replayed prompt"}}), "audit of 106")
        self.assertFalse(cwf.is_progress({"type": "user", "isReplay": True,
                                          "message": {"content": [{"type": "tool_result", "content": "x"}]}}))
        self.assertTrue(cwf.is_progress({"type": "assistant", "message": {"model": "m"}}))
        self.assertFalse(cwf.is_progress({"type": "assistant", "message": {"model": "<synthetic>"}}))
        self.assertFalse(cwf.is_progress({"type": "assistant", "error": "rate_limit", "message": {"model": "m"}}))
        self.assertFalse(cwf.is_progress({"type": "system", "subtype": "init"}))
        self.assertTrue(cwf.buffered_progress(self.lines({"type": "result", "num_turns": 4})))
        self.assertFalse(cwf.buffered_progress(self.lines({"type": "result", "num_turns": 1})))

    def test_json_verbose_array_is_read_as_events(self):
        # Audit of 106, M4: `--output-format json --verbose` prints one JSON array.
        arr = json.dumps([{"type": "system", "subtype": "init", "session_id": "s1"},
                          {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Quota exceeded 429 401"}]}},
                          {"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "", "num_turns": 5,
                           "session_id": "s1"}]).encode()
        self.assertEqual(len(cwf.events(arr)), 3)
        self.assertIsNone(cwf.out_of_tokens("lt", 1, arr, b""), "tool result text is never scanned")
        self.assertEqual(cwf.session_of(arr), "s1")
        self.assertTrue(cwf.buffered_progress(arr))

    def test_expired_logins_and_cli_error_codes_are_limits(self):
        for text in ("Failed to authenticate", "OAuth session expired", "token could not be refreshed"):
            reason, bench = cwf.classify_failure("lt", 1, self.lines({"type": "result", "is_error": True, "result": text}), b"")
            self.assertTrue(reason and bench, text)
        for code in ("authentication_failed", "billing_error"):  # rate_limit: test_transient_notice_and_oauth_refresh
            out = self.lines({"type": "assistant", "error": code, "message": {"model": "<synthetic>", "content": []}},
                             {"type": "result", "is_error": True, "result": ""})
            self.assertEqual(cwf.classify_failure("lt", 1, out, b""), (f"assistant error {code}", True))

    def test_transient_rate_limit_is_not_benched(self):
        out = self.lines({"type": "result", "is_error": True, "result": 'API Error: 429 {"type":"rate_limit_error"}'})
        reason, bench = cwf.classify_failure("lt", 1, out, b"")
        self.assertTrue(reason)
        self.assertFalse(bench)
        reason, bench = cwf.classify_failure("lt", 1, self.lines({"type": "result", "is_error": True, "api_error_status": 429,
                                                                  "result": "usage_limit_reached"}), b"")
        self.assertTrue(bench, "a usage limit is benched")

    def test_result_text_read_only_on_error(self):
        out = self.lines({"type": "result", "is_error": False, "result": "the quota report you asked for"})
        self.assertIsNone(cwf.out_of_tokens("lt", 1, out, b""))

    def test_resume_args(self):
        s = "sess-1"
        c = cwf.CONTINUE_TEXT
        self.assertEqual(cwf.resume_args(["-p", "--output-format", "stream-json", "--verbose", "--resume", "old", "do it"], s, False),
                         ["-p", "--output-format", "stream-json", "--verbose", "--resume", s, c])
        self.assertEqual(cwf.resume_args(["-p", "--resume=old", "--continue", "--fork-session", "do it"], s, False),
                         ["-p", "--resume", s, c])
        self.assertEqual(cwf.resume_args(["-p", "--model", "opus", "--system-prompt", "x"], s, True),
                         ["-p", "--model", "opus", "--system-prompt", "x", "--resume", s, c], "prompt on stdin")
        self.assertIsNone(cwf.resume_args(["-p", "--model", "opus", "--tools", ""], s, True),
                          "a variadic flag with a stdin prompt: no resume (audit of 109)")
        self.assertIsNone(cwf.resume_args(["-p", "--model", "opus"], s, False), "no prompt at all")
        self.assertIsNone(cwf.resume_args(["-p", "--verbose"], s, False))
        self.assertIsNone(cwf.resume_args(["-p", "--no-session-persistence", "x"], s, False))
        self.assertIsNone(cwf.resume_args(["-p", "--", "x"], s, False))

    def test_resume_args_audit_of_108(self):
        s = "sess-1"
        c = cwf.CONTINUE_TEXT
        # M1: stdin context plus a positional prompt: both go.
        self.assertEqual(cwf.resume_args(["-p", "--output-format", "stream-json", "haz X"], s, True),
                         ["-p", "--output-format", "stream-json", "--resume", s, c])
        # M2: the prompt before value flags; every value flag consumes its value.
        self.assertEqual(cwf.resume_args(["-p", "PROMPT", "--output-format", "stream-json", "--agent", "rev"], s, False),
                         ["-p", "--output-format", "stream-json", "--agent", "rev", "--resume", s, c])
        for flag in ("--agent", "-n", "--name", "--debug-file", "--remote-control-session-name-prefix"):
            self.assertEqual(cwf.resume_args(["-p", "P", flag, "v"], s, False), ["-p", flag, "v", "--resume", s, c], flag)
        # Optional values (audit of 109, minor 1): never certain, so no resume.
        for args in (["-p", "-d", "api", "P"], ["-p", "--debug", "--verbose", "P"], ["-p", "P", "-w", "x"],
                     ["-p", "P", "--worktree"], ["-p", "--from-pr", "12", "P"], ["-p", "--debug=api", "P"]):
            self.assertIsNone(cwf.resume_args(args, s, False), args)
        self.assertEqual(cwf.resume_args(["--print", "--model=opus", "P"], s, False), ["--print", "--model=opus", "--resume", s, c])
        # An unknown flag, or more than one positional (a variadic flag's extra values): no resume.
        self.assertIsNone(cwf.resume_args(["-p", "P", "--brand-new-flag", "v"], s, False))
        self.assertIsNone(cwf.resume_args(["-p", "--allowedTools", "Bash", "Read", "P"], s, False))
        self.assertIsNone(cwf.resume_args(["-p", "--output-format"], s, False), "a value flag with no value")
        # Minor 4: a stream of messages on stdin is never resumed.
        self.assertIsNone(cwf.resume_args(["-p", "--input-format", "stream-json", "--output-format", "stream-json"], s, True))
        self.assertIsNone(cwf.resume_args(["-p", "--input-format=stream-json"], s, True))
        self.assertEqual(cwf.resume_args(["-p", "--input-format", "text"], s, True), ["-p", "--input-format", "text", "--resume", s, c])

    def test_resume_args_variadic_flags(self):
        # Audit of 109, M2: variadic flags keep every value; with a stdin prompt, no resume.
        s, c = "sess-1", cwf.CONTINUE_TEXT
        self.assertEqual(cwf.resume_args(["-p", "P", "--disallowedTools", "Bash", "Write", "--verbose"], s, False),
                         ["-p", "--disallowedTools", "Bash", "Write", "--verbose", "--resume", s, c])
        self.assertEqual(cwf.resume_args(["-p", "P", "--allowedTools", "Bash", "Read"], s, False),
                         ["-p", "--allowedTools", "Bash", "Read", "--resume", s, c])
        self.assertIsNone(cwf.resume_args(["-p", "--allowedTools", "Bash", "Read", "P"], s, False),
                          "the last token is one of the flag's values: no prompt at all")
        for flag in sorted(cwf.VARIADIC_FLAGS):
            self.assertIsNone(cwf.resume_args(["-p", flag, "a", "b"], s, True), f"{flag} with a stdin prompt")
            self.assertIsNone(cwf.resume_args(["-p", flag], s, False), f"{flag} with no value")
        self.assertIsNone(cwf.resume_args(["-p", "--tools=Bash", "--verbose"], s, True), "the = form counts too")

    def test_transient_notice_with_limit_text_never_benches(self):
        # Audit of 109, minor 2: flagged transient wins over a limit word, in the notice and the result.
        notice = {"type": "assistant", "error": "rate_limit", "apiErrorIsTransient": True,
                  "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "rate limit reached, usage limit"}]}}
        out = self.lines(notice, {"type": "result", "is_error": True, "result": "usage limit reached"})
        reason, bench = cwf.classify_failure("lt", 1, out, b"")
        self.assertTrue(reason)
        self.assertFalse(bench)

    def test_login_in_stderr_benches_despite_a_block_page(self):
        # Audit of 110, item 2: the block-page test reads the notice and result text, not stderr.
        out = self.lines({"type": "result", "is_error": True, "result": "403 <html>cloudflare</html>"})
        self.assertTrue(cwf.classify_failure("lt", 1, out, b"Please run /login")[1])

    def test_cloudflare_block_is_transient(self):
        # Audit of 109, minor 5: authentication_failed with a 403 Cloudflare page.
        page = {"type": "assistant", "error": "authentication_failed",
                "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "403 <html>Attention Required! | Cloudflare</html>"}]}}
        reason, bench = cwf.classify_failure("lt", 1, self.lines(page, {"type": "result", "is_error": True, "result": ""}), b"")
        self.assertFalse(bench)
        real = dict(page, message={"model": "<synthetic>", "content": [{"type": "text", "text": "Invalid API key"}]})
        self.assertTrue(cwf.classify_failure("lt", 1, self.lines(real, {"type": "result", "is_error": True, "result": ""}), b"")[1])

    def test_update_state_merges_concurrent_writes(self):
        # Audit of 109, minor 3: another wrapper's update survives ours.
        tmp = Path(tempfile.mkdtemp(prefix="cwf-merge-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(cwf, "STATE", tmp / "claude-fallback.json"):
            cwf.update_state({"lt": 10.0})
            cwf.save_state({**cwf.load_state(), "mt": 20.0})  # another wrapper, after we read
            cwf.update_state({"na": 30.0, "lt": None})
            self.assertEqual(cwf.load_state(), {"mt": 20.0, "na": 30.0})
            procs = [subprocess.Popen([sys.executable, "-c",
                                       "import importlib.util,sys;sys.path.insert(0,%r);import testsupport;"
                                       "s=importlib.util.spec_from_file_location('w',%r);m=importlib.util.module_from_spec(s);"
                                       "s.loader.exec_module(m);from pathlib import Path;m.STATE=Path(%r);"
                                       "[m.update_state({'k%%d_%%d'%%(%d,i):1.0}) for i in range(20)]"
                                       % (str(HERE), str(SCRIPT), str(tmp / "claude-fallback.json"), n)])
                     for n in range(4)]
            for proc in procs:
                proc.wait(timeout=60)
            self.assertEqual(len([k for k in cwf.load_state() if k.startswith("k")]), 80, "no update lost")

    def test_transient_notice_and_oauth_refresh(self):
        # Audit of 108, minor 3.
        transient = {"type": "assistant", "error": "rate_limit", "apiErrorIsTransient": True,
                     "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: 429"}]}}
        reason, bench = cwf.classify_failure("lt", 1, self.lines(transient, {"type": "result", "is_error": True, "result": ""}), b"")
        self.assertTrue(reason)
        self.assertFalse(bench)
        bare = {"type": "assistant", "error": "rate_limit", "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error: 429"}]}}
        self.assertFalse(cwf.classify_failure("lt", 1, self.lines(bare, {"type": "result", "is_error": True, "result": ""}), b"")[1])
        usage = {"type": "assistant", "error": "rate_limit", "apiError": "usage_limit_reached",
                 "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your 5-hour limit"}]}}
        self.assertTrue(cwf.classify_failure("lt", 1, self.lines(usage, {"type": "result", "is_error": True, "result": ""}), b"")[1])
        text_only = dict(bare, message={"model": "<synthetic>", "content": [{"type": "text", "text": "You've hit your session limit"}]})
        self.assertTrue(cwf.classify_failure("lt", 1, self.lines(text_only, {"type": "result", "is_error": True, "result": ""}), b"")[1])
        refresh = self.lines({"type": "result", "is_error": True,
                              "result": "Failed to refresh OAuth token (server_error), this is usually transient"})
        self.assertFalse(cwf.classify_failure("lt", 1, refresh, b"")[1], "never benched")
        self.assertTrue(cwf.classify_failure("lt", 1, self.lines({"type": "result", "is_error": True,
                                                                  "result": "OAuth token has expired"}), b"")[1])

    def test_state_is_written_atomically(self):
        tmp = Path(tempfile.mkdtemp(prefix="cwf-state-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        with mock.patch.object(cwf, "STATE", tmp / "s" / "claude-fallback.json"):
            cwf.save_state({"lt": 1.0})
            self.assertEqual(cwf.load_state(), {"lt": 1.0})
            with mock.patch.object(cwf.os, "replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    cwf.save_state({"mt": 2.0})
            self.assertEqual(cwf.load_state(), {"lt": 1.0}, "a failed write never truncates the file")

    def test_unknown_tags(self):
        self.assertEqual(cwf.unknown_tags("lt, zz,,mt,qq"), ["zz", "qq"])


class SafeRetries(_Wrapper):
    """M1 end to end: no re-run after work or after a non-quota failure; M2: no orphans."""

    def test_tool_result_quota_text_is_not_rerun_and_benches_nobody(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "toolquota"})
        self.assertEqual(tried, ["lt"])
        self.assertEqual(p.returncode, 1)
        state = self.home / ".local/state/claude-fallback.json"
        self.assertFalse(state.exists() and "lt" in json.loads(state.read_text()), "lt not benched")

    def calls_args(self) -> list[list[str]]:
        return [json.loads(line)["args"] for line in self.calls.read_text().splitlines()]

    def test_limit_after_work_resumes_the_session_on_the_next_account(self):
        # Audit of 106, M1: continue the session, never start the prompt again.
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "--verbose", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        first, second = self.calls_args()
        self.assertEqual(first, ["-p", "--output-format", "stream-json", "--verbose", "go"])
        self.assertEqual(second, ["-p", "--output-format", "stream-json", "--verbose", "--resume", "s-lt", cwf.CONTINUE_TEXT],
                         "same flags, the session resumed, the original prompt not sent again")
        evs = [json.loads(line) for line in p.stdout.decode().splitlines()]
        self.assertIn({"type": "system", "subtype": "account_fallback", "from": "lt", "to": "mt"}, evs)
        self.assertEqual(evs[-1]["result"], "answer from mt")
        self.assertEqual(evs[-1]["session_id"], "s-lt", "the same session carries on")
        self.assertEqual(p.returncode, 0)
        self.assertIn("lt", json.loads((self.home / ".local/state/claude-fallback.json").read_text()))

    def test_a_resumed_run_that_hits_the_limit_too_moves_on_once_per_account(self):
        _, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_MODE_mt": "work_then_limit",
                                           "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt", "na"])
        self.assertEqual([a[-3:-1] for a in self.calls_args()[1:]], [["--resume", "s-lt"], ["--resume", "s-lt"]])

    def test_every_account_limited_after_work_returns_the_quota_result(self):
        modes = {f"FAKE_MODE_{t}": "work_then_limit" for t in ("lt", "mt", "na")}
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={**modes, "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt", "na"])
        self.assertEqual(json.loads(p.stdout.decode().splitlines()[-1])["api_error_status"], 429)
        self.assertEqual(p.returncode, 1)

    def test_no_session_id_returns_as_today(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit_nosid"})
        self.assertEqual(tried, ["lt"])
        self.assertEqual(p.returncode, 1)

    def test_no_session_persistence_returns_as_today(self):
        _, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "--no-session-persistence", "go"],
                                    active="lt", extra={"FAKE_MODE_lt": "work_then_limit"})
        self.assertEqual(tried, ["lt"])

    def test_buffered_json_after_several_turns_resumes_too(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        self.assertEqual(self.calls_args()[1][-3:-1], ["--resume", "s-lt"])
        self.assertEqual(json.loads(p.stdout)["result"], "answer from mt", "buffered: only the final answer")

    def test_D_resume_that_did_work_then_failed_is_passed_through(self):
        # Audit of 109, M1, scenario D: the work and its own result reach the caller.
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "--verbose", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_MODE_mt": "work_error",
                                           "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        evs = [json.loads(line) for line in p.stdout.decode().splitlines()]
        self.assertEqual(evs[-1]["subtype"], "error_max_turns", "the resume's own result, not lt's old limit")
        self.assertEqual([e for e in evs if e.get("subtype") == "account_fallback"],
                         [{"type": "system", "subtype": "account_fallback", "from": "lt", "to": "mt"}], "no second marker")
        self.assertIn("DONE", p.stdout.decode())
        self.assertEqual(p.returncode, 1)

    def test_E_json_resume_that_did_work_then_failed_is_passed_through(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_MODE_mt": "work_error",
                                           "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        self.assertEqual(json.loads(p.stdout.decode().splitlines()[-1])["subtype"], "error_max_turns")

    def test_K_stdin_prompt_with_a_variadic_flag_is_not_resumed(self):
        # Audit of 109, M2, scenario K: `--disallowedTools Bash Write` must never lose Write.
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "--verbose", "--disallowedTools", "Bash", "Write"],
                                    active="lt", stdin=b"TASK FROM STDIN",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt"], "no resume: the limit result goes back as before 108")
        self.assertEqual(json.loads(p.stdout.decode().splitlines()[-1])["api_error_status"], 429)

    def test_L_optional_value_flag_is_not_resumed(self):
        # Scenario L: `-w --model opus` with a stdin prompt.
        _, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "--verbose", "-w", "--model", "opus"],
                                    active="lt", stdin=b"TASK",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt"])

    def desk_argv_for(self):
        """ticket-desk's own `desk.runs.argv_for` when TICKET_DESK_DIR names its checkout, else
        None. Only the function is used, with a synthetic config."""
        import importlib
        if not os.environ.get("TICKET_DESK_DIR"):
            return None
        root = Path(os.environ["TICKET_DESK_DIR"]).expanduser()
        if not (root / "desk" / "runs.py").exists():
            return None
        sys.path.insert(0, str(root))
        try:
            return importlib.import_module("desk.runs").argv_for
        except Exception:  # noqa: BLE001 - another layout or a missing dependency: skip
            return None
        finally:
            sys.path.remove(str(root))

    def test_the_desks_own_argv_keeps_resuming(self):
        # Follows the argv ticket-desk really builds (audit of 110), with and without a session.
        argv_for = self.desk_argv_for()
        if argv_for is None:
            self.skipTest("ticket-desk repo not found (set TICKET_DESK_DIR)")
        cfg = {"agents": {"claude": "CLAUDE", "cursor": "cursor-agent"}, "skeleton": {"root": str(self.home)}}
        for session in (None, "sess-old"):
            args = argv_for(cfg, "claude", "PROMPT TEXT", session)[1:]
            if self.calls.exists():
                self.calls.unlink()
            state = self.home / ".local/state/claude-fallback.json"
            if state.exists():
                state.unlink()  # lt benched by the previous pass
            p, tried = self.run_wrapper(args, active="lt",
                                        extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
            self.assertEqual(tried, ["lt", "mt"], args)
            expected = [a for a in args if a not in ("PROMPT TEXT", "--resume", "sess-old")]
            self.assertEqual(self.calls_args()[1], expected + ["--resume", session or "s-lt", cwf.CONTINUE_TEXT], args)
            self.assertEqual(p.returncode, 0)

    def notifier_config(self) -> Path:
        """A copy of the test accounts config whose notifier logs its arguments."""
        notes = self.home / "notes.log"
        script = self.home / "notify.sh"
        script.write_text(f'#!/bin/sh\necho "$@" >> {notes}\n')
        script.chmod(0o755)
        cfg = json.loads((HERE / "testdata" / "claude-accounts.json").read_text())
        cfg["notify"] = [str(script)]
        path = self.home / "accounts.json"
        path.write_text(json.dumps(cfg))
        return path

    def test_failed_resume_after_work_is_not_announced_as_done(self):
        # Audit of 110, item 1: no "done on mt" notice and mt's state is not reset as a success.
        state = self.home / ".local/state/claude-fallback.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"transient:mt": 2}))
        cfg = self.notifier_config()
        self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                         extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_MODE_mt": "work_error", "FAKE_RESUME_SID": "1",
                                "LATIDOS_CLAUDE_ACCOUNTS": str(cfg)})
        notes = (self.home / "notes.log").read_text()
        self.assertIn("terminó con error", notes)
        self.assertNotIn("se ha hecho con la cuenta", notes)
        self.assertEqual(json.loads(state.read_text()).get("transient:mt"), 2, "not cleared as if it had succeeded")

    def test_repeated_transient_failures_bench_briefly(self):
        # Audit of 110, item 2: the third transient failure in a row benches lt for a few minutes.
        state = self.home / ".local/state/claude-fallback.json"
        for n in (1, 2):
            _, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt",
                                        extra={"FAKE_MODE_lt": "transient"})
            self.assertEqual(json.loads(state.read_text()).get("transient:lt"), n)
            self.assertNotIn("lt", json.loads(state.read_text()))
        self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt", extra={"FAKE_MODE_lt": "transient"})
        data = json.loads(state.read_text())
        self.assertNotIn("transient:lt", data)
        self.assertAlmostEqual(data["lt"] - time.time(), cwf.TRANSIENT_BENCH_MINUTES * 60, delta=30)
        # A success clears the count.
        state.write_text(json.dumps({"transient:lt": 2}))
        self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt")
        self.assertNotIn("transient:lt", json.loads(state.read_text()))

    def test_busy_lock_never_loses_the_answer(self):
        # Audit of 110, item 3: the lock is held elsewhere; the update is skipped, the output goes out.
        import fcntl
        lock_path = self.home / ".local/state/claude-fallback.json.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            t0 = time.time()
            p, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt", spent="lt")
            fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual(tried, ["lt", "mt"])
        self.assertEqual(json.loads(p.stdout)["result"], "answer from mt")
        self.assertLess(time.time() - t0, 30)
        self.assertIn("state lock busy", (self.home / "Library/Logs/claude-fallback.log").read_text())

    def test_unwritable_state_never_loses_the_answer(self):
        state_dir = self.home / ".local/state"
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o500)
        self.addCleanup(state_dir.chmod, 0o755)
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt", spent="lt")
        self.assertEqual(tried, ["lt", "mt"])
        self.assertEqual(json.loads(p.stdout)["result"], "answer from mt")
        self.assertEqual(p.returncode, 0)

    def test_stdin_context_and_positional_prompt_both_go(self):
        # Audit of 108, M1: `cat ctx | wrapper -p ... "haz X"`: the positional was the prompt.
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "--verbose", "haz X"], active="lt",
                                    stdin=b"CONTEXT",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        self.assertEqual(self.calls_args()[1], ["-p", "--output-format", "stream-json", "--verbose", "--resume", "s-lt",
                                                cwf.CONTINUE_TEXT], "the original prompt is not sent again")

    def test_prompt_before_a_trailing_value_flag(self):
        # Audit of 108, M2 (probe 6): `--agent rev` after the prompt must not swallow --resume.
        _, tried = self.run_wrapper(["-p", "PROMPT", "--output-format", "stream-json", "--verbose", "--agent", "rev"],
                                    active="lt", extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        self.assertEqual(self.calls_args()[1], ["-p", "--output-format", "stream-json", "--verbose", "--agent", "rev",
                                                "--resume", "s-lt", cwf.CONTINUE_TEXT])

    def test_unknown_flag_after_the_prompt_is_not_resumed(self):
        p, tried = self.run_wrapper(["-p", "PROMPT", "--output-format", "stream-json", "--brand-new-flag", "v"],
                                    active="lt", extra={"FAKE_MODE_lt": "work_then_limit"})
        self.assertEqual(tried, ["lt"], "unclear prompt: returned as is, never the original prompt again")
        self.assertEqual(p.returncode, 1)

    def test_failed_resume_hands_back_the_limit(self):
        # Audit of 108, minor 5 (probe 9): the desk must still see quota.
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_MODE_mt": "no_conversation",
                                           "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt", "mt"])
        evs = [json.loads(line) for line in p.stdout.decode().splitlines()]
        self.assertEqual(evs[-2], {"type": "system", "subtype": "account_fallback", "from": "mt", "to": "lt"})
        self.assertEqual(evs[-1]["api_error_status"], 429)
        self.assertEqual(p.returncode, 1)

    def test_no_shared_pool_is_not_resumed(self):
        (self.home / ".claude.mt" / "projects").unlink()
        (self.home / ".claude.mt" / "projects").mkdir()
        p, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "work_then_limit", "FAKE_RESUME_SID": "1"})
        self.assertEqual(tried, ["lt"])
        self.assertEqual(json.loads(p.stdout.decode().splitlines()[-1])["api_error_status"], 429)
        self.assertNotIn("account_fallback", p.stdout.decode())

    def test_transient_stream_notice_is_not_benched(self):
        _, tried = self.run_wrapper(["-p", "--output-format", "stream-json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "transient_notice"})
        self.assertEqual(tried, ["lt", "mt"])
        state = self.home / ".local/state/claude-fallback.json"
        self.assertFalse(state.exists() and "lt" in json.loads(state.read_text()))

    def test_transient_rate_limit_moves_on_without_benching(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "transient"})
        self.assertEqual(tried, ["lt", "mt"])
        state = self.home / ".local/state/claude-fallback.json"
        self.assertFalse(state.exists() and "lt" in json.loads(state.read_text()), "a transient 429 benches nobody")

    def test_expired_login_falls_over_and_benches(self):
        _, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "expired"})
        self.assertEqual(tried, ["lt", "mt"])
        self.assertIn("lt", json.loads((self.home / ".local/state/claude-fallback.json").read_text()))

    def test_sigterm_never_deadlocks(self):
        # The auditor's repro: the child closed its stdout and keeps running, the wrapper sits
        # in proc.wait(); a SIGTERM must still end both.
        pids = self.home / "pids"
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_FALLBACK_ORDER", "CLAUDE_FALLBACK_ACCOUNT", "CLAUDECODE")}
        env.update(HOME=str(self.home), CLAUDE_REAL_BIN=str(self.fake), FAKE_CALLS=str(self.calls), FAKE_PIDS=str(pids),
                   FAKE_MODE_lt="close_stdout", CLAUDE_ACCOUNTS_ORDER_FILE=str(self.order_file),
                   LATIDOS_CLAUDE_ACCOUNTS=str(HERE / "testdata" / "claude-accounts.json"))
        w = subprocess.Popen([sys.executable, str(SCRIPT), "-p", "--output-format", "stream-json", "x"], env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: w.poll() is None and w.kill())
        w.stdout.readline()
        time.sleep(1.0)
        child = int(pids.read_text().split()[0])
        self.addCleanup(self.kill_if_alive, child)
        os.kill(w.pid, signal.SIGTERM)
        self.assertEqual(w.wait(timeout=10), 128 + signal.SIGTERM, "the wrapper leaves at once")
        self.assertTrue(self.wait_gone(child), "the watcher ends the child")

    def test_overloaded_is_returned_without_fallback(self):
        p, tried = self.run_wrapper(["-p", "--output-format", "json", "go"], active="lt",
                                    extra={"FAKE_MODE_lt": "overloaded"})
        self.assertEqual((tried, p.returncode), (["lt"], 1))

    def test_large_stdin_in_stream_mode(self):
        env_extra = {"FAKE_MODE_lt": "stdin_len"}
        big = b"x" * (512 * 1024)
        if self.calls.exists():
            self.calls.unlink()
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_FALLBACK_ORDER", "CLAUDE_FALLBACK_ACCOUNT", "CLAUDECODE")}
        env.update(HOME=str(self.home), CLAUDE_REAL_BIN=str(self.fake), FAKE_CALLS=str(self.calls),
                   CLAUDE_ACCOUNTS_ORDER_FILE=str(self.order_file),
                   LATIDOS_CLAUDE_ACCOUNTS=str(HERE / "testdata" / "claude-accounts.json"), **env_extra)
        p = subprocess.run([sys.executable, str(SCRIPT), "-p", "--output-format", "stream-json"], input=big,
                           capture_output=True, env=env, timeout=60)
        self.assertEqual(json.loads(p.stdout.decode().splitlines()[-1])["result"], str(len(big)))

    def test_unknown_pinned_tag_is_logged(self):
        _, tried = self.run_wrapper(["-p", "hi"], active="mt", extra={"CLAUDE_FALLBACK_ACCOUNT": "zz"})
        self.assertEqual(tried, ["mt"])
        self.assertIn("unknown account(s) zz", (self.home / "Library/Logs/claude-fallback.log").read_text())

    def start_sleeping_wrapper(self):
        pids = self.home / "pids"
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CLAUDE_FALLBACK_ORDER", "CLAUDE_FALLBACK_ACCOUNT", "CLAUDECODE")}
        env.update(HOME=str(self.home), CLAUDE_REAL_BIN=str(self.fake), FAKE_CALLS=str(self.calls), FAKE_PIDS=str(pids),
                   FAKE_MODE_lt="sleep", CLAUDE_ACCOUNTS_ORDER_FILE=str(self.order_file),
                   LATIDOS_CLAUDE_ACCOUNTS=str(HERE / "testdata" / "claude-accounts.json"))
        wrapper = subprocess.Popen([sys.executable, str(SCRIPT), "-p", "go"], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: wrapper.poll() is None and wrapper.kill())
        deadline = time.time() + 20
        while time.time() < deadline and not (pids.exists() and pids.read_text().strip()):
            time.sleep(0.1)
        child = int(pids.read_text().split()[0])
        self.addCleanup(self.kill_if_alive, child)
        return wrapper, child

    @staticmethod
    def alive(pid: int) -> bool:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(out) and not out.startswith("Z")

    def kill_if_alive(self, pid: int) -> None:
        # Only the fake child this test started, by its recorded PID.
        if self.alive(pid):
            os.kill(pid, signal.SIGKILL)

    def wait_gone(self, pid: int, seconds: float = 10.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if not self.alive(pid):
                return True
            time.sleep(0.1)
        return False

    def test_sigkilled_wrapper_takes_its_child_along(self):
        wrapper, child = self.start_sleeping_wrapper()
        self.assertTrue(self.alive(child))
        os.kill(wrapper.pid, signal.SIGKILL)  # what a caller's subprocess timeout does
        wrapper.wait()
        self.assertTrue(self.wait_gone(child), "the real CLI must not outlive the wrapper")

    def test_sigterm_is_passed_on(self):
        wrapper, child = self.start_sleeping_wrapper()
        os.kill(wrapper.pid, signal.SIGTERM)
        self.assertEqual(wrapper.wait(timeout=10), 128 + signal.SIGTERM)
        self.assertTrue(self.wait_gone(child))


if __name__ == "__main__":
    unittest.main()
