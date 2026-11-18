"""Tests for the "continue after an account switch" part of scripts/cursor-claude-account.py:
cut-turn detection, tab titles, the request/response exchange with the companion
extension, the continue decision (cut tabs first), the hand-off to claude_peer.py and the
host filter. The socket format itself is tested in test_claude_peer.py.

Synthetic transcripts and a fake companion in temp dirs; no Cursor, no Claude process,
no signal is ever sent.

Run from the repository root:
    python3 -m unittest accounts/test_cursor_claude_relaunch.py -v
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
import socket
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

SCRIPT = Path(__file__).resolve().parent / "cursor-claude-account.py"
spec = importlib.util.spec_from_file_location("cursor_claude_account", SCRIPT)
cca = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cca)
# Hermetic: moves in these tests never write the real cut-moves record.
cca.CUT_MOVES = Path(tempfile.mkdtemp(prefix="cca-cut-")) / "cut-moves.json"


def entry(kind: str, text: str, **extra) -> dict:
    content = text if kind == "user" else [{"type": "text", "text": text}]
    return {"type": kind, "message": {"role": kind, "content": content}, **extra}


class _ResetAge(unittest.TestCase):
    """move_open sets the module-level error age per call; put it back after each test so
    test order cannot change results (audit of auto switch release 12)."""

    def setUp(self):
        keep = cca.ERROR_MAX_AGE_HOURS
        self.addCleanup(setattr, cca, "ERROR_MAX_AGE_HOURS", keep)


class TempPool(_ResetAge):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="cca-test-"))
        self.pool = self.tmp / "projects"
        (self.pool / "-proj").mkdir(parents=True)
        self.root = self.tmp / "relaunch"
        patches = [mock.patch.object(cca, "POOL_PROJECTS", self.pool),
                   mock.patch.object(cca, "RELAUNCH_ROOT", self.root)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def write(self, session: str, rows: list[dict]) -> None:
        path = self.pool / "-proj" / f"{session}.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


class CutDetection(TempPool):
    def test_limit_error_is_cut(self):
        self.write("s1", [entry("user", "haz algo"),
                          entry("assistant", "You've hit your 5-hour limit", isApiErrorMessage=True)])
        self.assertTrue(cca.ended_on_error("s1"))

    def test_finished_turn_is_not_cut(self):
        self.write("s2", [entry("user", "haz algo"), entry("assistant", "hecho"),
                          {"type": "ai-title", "aiTitle": "x"}])
        self.assertFalse(cca.ended_on_error("s2"))

    def test_sidechain_after_the_error_is_ignored(self):
        self.write("s3", [entry("user", "a"), entry("assistant", "limit", isApiErrorMessage=True),
                          entry("assistant", "subagent", isSidechain=True)])
        self.assertTrue(cca.ended_on_error("s3"))

    def test_missing_transcript_and_empty_id(self):
        self.assertFalse(cca.ended_on_error("nope"))
        self.assertFalse(cca.ended_on_error(""))

    def test_big_transcript_reads_only_the_tail(self):
        filler = [entry("user", "x" * 1000) for _ in range(400)]  # > TAIL_BYTES
        self.write("s4", filler + [entry("assistant", "limit", isApiErrorMessage=True)])
        self.assertTrue(cca.ended_on_error("s4"))


class Titles(TempPool):
    def test_order_and_skips(self):
        self.write("t1", [
            entry("user", "<command-name>/clear</command-name>"),
            entry("user", "meta", isMeta=True),
            entry("user", "Arregla el bug del SLA\nmás detalle"),
            {"type": "ai-title", "aiTitle": "SLA de primera respuesta"},
            {"type": "custom-title", "customTitle": "SLA viejo"},
            {"type": "custom-title", "customTitle": "SLA reporter"},
        ])
        self.assertEqual(cca.session_titles("t1", "SLA bug"),
                         ["SLA reporter", "SLA de primera respuesta", "Arregla el bug del SLA", "SLA bug"])

    def test_no_transcript_keeps_the_name(self):
        self.assertEqual(cca.session_titles("missing", "ws-12"), ["ws-12"])


class ReopenExchange(TempPool):
    def fake_companion(self, host: int, result: str = "reopened") -> list[dict]:
        seen: list[dict] = []
        inbox = self.root / "requests" / str(host)

        def run():
            for _ in range(100):
                files = list(inbox.glob("*.json")) if inbox.exists() else []
                if files:
                    req = json.loads(files[0].read_text())
                    files[0].unlink()
                    seen.append(req)
                    out = self.root / "responses" / f"{req['id']}.json"
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps({"id": req["id"], "pid": host, "results": [
                        {"session": op["session"], "result": result, "position": "restored"} for op in req["ops"]]}))
                    return
                time.sleep(0.05)

        threading.Thread(target=run, daemon=True).start()
        return seen

    def test_request_carries_account_dir_and_titles(self):
        self.write("a1", [{"type": "custom-title", "customTitle": "Tab A"}])
        seen = self.fake_companion(111)
        moved = [{"session": "a1", "name": "n1", "host": 111, "pid": 5}]
        res = cca.request_reopen("mt", moved, timeout=5)
        self.assertEqual(res["a1"]["result"], "reopened")
        self.assertEqual(res["a1"]["position"], "restored")
        self.assertIn("refocus_terminal", seen[0])
        self.assertEqual(seen[0]["expect_config_dir"], str(cca.ACCOUNTS["mt"]["dir"]))
        self.assertEqual(seen[0]["ops"], [{"session": "a1", "titles": ["Tab A", "n1"]}])
        self.assertFalse(list((self.root / "responses").glob("*.json")), "response consumed")

    def test_silent_window_times_out(self):
        moved = [{"session": "b1", "name": "", "host": 222, "pid": 6}]
        self.assertEqual(cca.request_reopen("na", moved, timeout=0.5), {"b1": {"result": "no answer from the window"}})

    def test_companion_hosts_needs_a_live_pid(self):
        (self.root / "windows").mkdir(parents=True)
        (self.root / "windows" / f"{os.getpid()}.json").write_text("{}")
        (self.root / "windows" / "999999.json").write_text("{}")
        with mock.patch.object(cca, "pid_alive", side_effect=lambda p: p == os.getpid()):
            self.assertEqual(cca.companion_hosts(), {os.getpid()})


class ContinueDecision(TempPool):
    def run_continue(self, moved, results, mode="cut", hosts=None, cut_sessions=()):
        sent = []
        with mock.patch.object(cca, "companion_hosts", return_value=set(hosts or {1})), \
             mock.patch.object(cca, "request_reopen", return_value=results) as req, \
             mock.patch.object(cca, "wait_new_process", side_effect=lambda s, t, p: {"messagingSocketPath": f"/x/{s}"}), \
             mock.patch.object(cca, "ended_on_error", side_effect=lambda s: s in cut_sessions), \
             mock.patch.object(cca, "send_continue", side_effect=lambda info, text, tag: sent.append((info, text))):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.continue_moved("na", moved, mode)
        return sent, buf.getvalue(), req

    def s(self, sid, status="idle", host=1):
        return {"session": sid, "name": sid, "status": status, "host": host, "pid": 10}

    def test_busy_and_errored_get_continue_finished_does_not(self):
        moved = [self.s("busy1", "busy"), self.s("err1"), self.s("done1")]
        sent, out, _ = self.run_continue(moved, {m["session"]: {"result": "reopened", "position": "kept"} for m in moved}, cut_sessions={"err1"})
        self.assertEqual([i["messagingSocketPath"] for i, _ in sent], ["/x/busy1", "/x/err1"])
        self.assertIn("cuenta na", sent[0][1])
        self.assertIn("done1: on na, waiting for you", out)

    def test_mode_none_reopens_without_message(self):
        sent, out, req = self.run_continue([self.s("busy1", "busy")], {"busy1": {"result": "reopened"}}, mode="none")
        self.assertEqual(sent, [])
        req.assert_called_once()
        # Audit of release 21: a cut tab is not reported as finished.
        self.assertIn("busy1: on na, its work was cut; type continúa in it", out)
        self.assertNotIn("its turn had finished", out)

    def test_window_without_companion_is_not_asked(self):
        sent, out, req = self.run_continue([self.s("x", host=7)], {}, hosts={1})
        req.assert_not_called()
        self.assertIn("companion extension not running", out)

    def test_position_not_restored_is_reported(self):
        sent, out, _ = self.run_continue([self.s("p1")], {"p1": {"result": "reopened", "position": "not-moved"}})
        self.assertIn("could not be put back", out)

    def test_refocus_terminal_only_from_cursor_terminal_with_one_window(self):
        seen = []
        def capture(*a, **k):
            seen.append(json.loads(next((self.root / "requests" / "9").glob("*.json")).read_text()))
            raise SystemExit
        for env, hosts, want in [({"TERM_PROGRAM": "vscode"}, {9}, True), ({"TERM_PROGRAM": "vscode"}, {9, 10}, False),
                                 ({"TERM_PROGRAM": "iTerm.app"}, {9}, False)]:
            seen.clear()
            with mock.patch.dict(os.environ, env), mock.patch.object(cca, "companion_hosts", return_value=hosts), \
                 mock.patch.object(cca.time, "sleep", side_effect=capture):
                with self.assertRaises(SystemExit):
                    cca.request_reopen("mt", [self.s("r1", host=9)], timeout=5)
            self.assertIs(seen[0]["refocus_terminal"], want, env)
            for f in (self.root / "requests" / "9").glob("*.json"):
                f.unlink()

    def test_cut_sessions_are_handled_first(self):
        # 2026-11-12: a tab whose new process never showed up held the cut ones back 20 s.
        order, sent = [], []
        moved = [self.s("idle1"), self.s("gone1"), self.s("err1"), self.s("busy1", "busy")]
        results = {m["session"]: {"result": "reopened"} for m in moved}
        with mock.patch.object(cca, "companion_hosts", return_value={1}), \
             mock.patch.object(cca, "request_reopen", return_value=results), \
             mock.patch.object(cca, "wait_new_process",
                               side_effect=lambda s, t, p: order.append(s) or (None if s == "gone1" else {"pid": 1})), \
             mock.patch.object(cca, "ended_on_error", side_effect=lambda s: s == "err1"), \
             mock.patch.object(cca, "send_continue", side_effect=lambda info, text, tag: sent.append(order[-1])):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.continue_moved("mt", moved, "cut")
        self.assertEqual(order[:2], ["err1", "busy1"])
        self.assertEqual(sent, ["err1", "busy1"], "both sent before the tab with no process is waited for")
        self.assertIn("gone1: reopened, but no mt process showed up", buf.getvalue())

    def test_not_taken_is_reported(self):
        with mock.patch.object(cca, "companion_hosts", return_value={1}), \
             mock.patch.object(cca, "request_reopen", return_value={"b": {"result": "reopened"}}), \
             mock.patch.object(cca, "wait_new_process", return_value={"pid": 1}), \
             mock.patch.object(cca, "ended_on_error", return_value=False), \
             mock.patch.object(cca, "send_continue", return_value="held by the session (attested bypass)"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.continue_moved("mt", [self.s("b", "busy")], "cut")
        self.assertIn("was not taken (held by the session", buf.getvalue())

    def test_not_reopened_falls_back_to_typing(self):
        sent, out, _ = self.run_continue([self.s("amb", "busy")], {"amb": {"result": "ambiguous"}})
        self.assertEqual(sent, [])
        self.assertIn("not reopened (ambiguous)", out)


START = "2026-11-12T09:52:30Z"  # the ended process started here
BEFORE, T1, T2, T3 = "2026-11-12T09:00:00Z", "2026-11-12T10:00:00Z", "2026-11-12T10:30:00Z", "2026-11-12T11:00:00Z"


def ts_entry(kind, text, ts, **extra):
    return {**entry(kind, text, **extra), "timestamp": ts}


def tool_use(tid, name, ts=T1, **inp):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def tool_result(tid, text, ts=T1):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid,
                                                      "content": [{"type": "text", "text": text}]}]}}


def notification(task, status, summary="x", tool_use=None):
    tu = f"<tool-use-id>{tool_use}</tool-use-id>" if tool_use else ""
    return (f"<task-notification>\n<task-id>{task}</task-id>\n{tu}\n<status>{status}</status>\n"
            f"<summary>{summary}</summary>\n</task-notification>")


def end_turn(text, ts):
    e = ts_entry("assistant", text, ts)
    e["message"]["stop_reason"] = "end_turn"
    return e


def agent(tid, desc, agent_id, ts=T1):
    return [tool_use(tid, "Agent", ts=ts, description=desc),
            tool_result(tid, f"Async agent launched successfully.\nagentId: {agent_id} (internal)", ts=ts)]


class WorkInFlight(TempPool):
    """2026-11-12 12:08: a chat ended on an unanswered task notification with background
    work running; it was taken for finished and never told to continue."""

    NOW = "2026-11-12T10:31:00Z"  # the switch, when not said otherwise

    def flight(self, sid, now=None):
        return cca.work_in_flight(sid, cca._ms(START), cca._ms(now or self.NOW))

    def test_unanswered_last_input_is_pending(self):
        self.write("t1", [ts_entry("user", "hola", T1), ts_entry("assistant", "ok", T1),
                          ts_entry("user", notification("a1", "failed"), T2)])
        self.assertEqual(self.flight("t1")["pending"], "its last input had no answer yet")

    def test_interruption_or_local_command_is_not_pending(self):
        # Audit of release 8: after "[Request interrupted by user]" he may mean "stop".
        for text in ("[Request interrupted by user]", "<local-command-stdout>ok</local-command-stdout>"):
            self.write("i1", [ts_entry("user", "hola", T1), ts_entry("assistant", "ok", T1), ts_entry("user", text, T2)])
            self.assertEqual(self.flight("i1")["pending"], "", text)

    def test_input_before_the_process_started_is_not_pending(self):
        self.write("o2", [ts_entry("user", "hola", BEFORE)])
        self.assertEqual(self.flight("o2")["pending"], "")

    def test_queued_input_after_the_last_answer_is_pending(self):
        self.write("q1", [ts_entry("user", "hola", T1), ts_entry("assistant", "ok", T1),
                          {"type": "queue-operation", "operation": "enqueue", "content": "x", "timestamp": T2}])
        self.assertIn("queued", self.flight("q1")["pending"])

    def test_queue_lines_without_dequeue_do_not_pile_up(self):
        # Audit of release 8: absorbed or old enqueues never get a dequeue line; once the
        # session answered after them they were taken.
        self.write("q2", [{"type": "queue-operation", "operation": "enqueue", "content": "x", "timestamp": BEFORE},
                          ts_entry("user", "hola", T1),
                          {"type": "queue-operation", "operation": "enqueue", "content": "y", "timestamp": T2},
                          ts_entry("assistant", "hecho", T3)])
        self.assertEqual(self.flight("q2"), {"pending": "", "tasks": []})

    def test_unknown_start_infers_nothing(self):
        self.write("u1", [ts_entry("user", "hola", T1)])
        self.assertEqual(cca.work_in_flight("u1", 0), {"pending": "", "tasks": []})

    def test_running_agent_and_background_command_are_listed(self):
        self.write("r1", [ts_entry("user", "haz", T1),
                          *agent("tu1", "Audit release 3", "abc123"),
                          tool_use("tu2", "Bash", command="sleep 100", description="Wait for build", run_in_background=True),
                          tool_result("tu2", "Command running in background with ID: b77"),
                          *agent("tu3", "Done one", "def456"),
                          {"type": "queue-operation", "operation": "enqueue", "timestamp": T2,
                           "content": notification("def456", "completed")},
                          ts_entry("assistant", "esperando a los agentes", T2)])
        self.assertEqual(sorted(self.flight("r1")["tasks"]),
                         ['comando en segundo plano "Wait for build"', 'subagente "Audit release 3"'])

    def test_quoted_limit_text_in_a_failure_is_not_a_cut(self):
        # Audit of auto switch release 32: the limit_failure wiring, on the escaped JSON the
        # transcript holds (a grep for "rate-limit" exiting 1).
        self.write("q1", [ts_entry("user", "haz", T1),
                          tool_use("tu2", "Bash", command="grep -i rate-limit app.log", description="grep -i rate-limit app.log",
                                   run_in_background=True),
                          tool_result("tu2", "Command running in background with ID: bq1"),
                          {"type": "queue-operation", "operation": "enqueue", "timestamp": T2,
                           "content": notification("bq1", "failed",
                                                   'Background command "grep -i rate-limit app.log" failed with exit code 1')}])
        self.assertFalse(any("cortado por el límite" in t for t in self.flight("q1")["tasks"]))

    def test_notifications_quoted_in_tool_output_are_not_tasks(self):
        # Audit of auto switch release 33: a grep over other transcripts.
        quoted = notification("ghost1", "failed", "Agent terminated early due to an API error: You've hit your 5-hour limit")
        self.write("g1", [ts_entry("user", "haz", T1),
                          tool_use("tu9", "Bash", command="grep task-notification x.jsonl"),
                          {**tool_result("tu9", quoted), "toolUseResult": {"stdout": quoted}}])
        self.assertEqual(self.flight("g1")["tasks"], [])

    def test_notifications_quoted_by_the_assistant_or_a_watched_file_are_not_tasks(self):
        # Audit of auto switch release 34.
        quoted = notification("ghost2", "failed", "Agent terminated early due to an API error: You've hit your 5-hour limit")
        self.write("g2", [ts_entry("user", "haz", T1),
                          ts_entry("assistant", "found: " + quoted, T1),
                          {"type": "attachment", "timestamp": T1,
                           "attachment": {"type": "edited_text_file", "snippet": quoted}}])
        self.assertEqual(self.flight("g2")["tasks"], [])

    def test_a_finish_delivered_only_as_an_attachment_counts(self):
        # Audit of auto switch release 35: the queued_command / prompt_snapshot allowlist.
        for att in ("queued_command", "prompt_snapshot"):
            sid = f"a-{att}"
            self.write(sid, [ts_entry("user", "haz", T1),
                             tool_use("tu2", "Bash", command="make", description="Build", run_in_background=True),
                             tool_result("tu2", "Command running in background with ID: ba1"),
                             {"type": "attachment", "timestamp": T2,
                              "attachment": {"type": att, "prompt": notification("ba1", "completed")}}])
            self.assertEqual(self.flight(sid)["tasks"], [], att)

    def test_monitor_workflow_and_wakeup_are_tracked(self):
        self.write("m1", [tool_use("m", "Monitor", command="tail -f x", description="Watch upload"),
                          tool_result("m", "Monitor started (task bm1, expires in 60m)"),
                          tool_use("w", "Workflow", script="export const meta = {\n  name: 'gates-cutover',\n}"),
                          tool_result("w", "Workflow launched in background. Task ID: wf1\nSummary: x"),
                          tool_use("z", "ScheduleWakeup", ts=T2, delaySeconds=600, reason="check the plan"),
                          tool_result("z", "Next wakeup scheduled for 11:40:00 (in 600s).", ts=T2),
                          ts_entry("assistant", "espero", T2)])
        tasks = self.flight("m1")["tasks"]
        self.assertIn('monitor "Watch upload"', tasks)
        self.assertIn('workflow "gates-cutover"', tasks)
        self.assertIn("despertador programado (ScheduleWakeup) que ya no sonará", tasks)

    def test_wakeup_that_already_fired_or_was_cancelled_is_not_listed(self):
        # Audit of release 9: firing writes an isMeta entry; "Loop stopped ... cancelled" also
        # contains "scheduled". Only a wakeup due after the switch counts.
        self.write("w1", [tool_use("z", "ScheduleWakeup", ts=T1, delaySeconds=60, reason="r"),
                          tool_result("z", "Next wakeup scheduled for 10:01:00 (in 60s).", ts=T1),
                          end_turn("espero", T1)])
        self.assertEqual(self.flight("w1")["tasks"], [])
        self.write("w2", [tool_use("z", "ScheduleWakeup", ts=T2, delaySeconds=600, reason="r"),
                          tool_result("z", "Next wakeup scheduled for 10:40:00 (in 600s).", ts=T2),
                          tool_use("y", "ScheduleWakeup", ts=T2, stop=True),
                          tool_result("y", "Loop stopped, cancelled 1 pending wakeup(s); no further dynamic-loop "
                                           "wakeups scheduled.", ts=T2),
                          end_turn("parado", T2)])
        self.assertEqual(self.flight("w2")["tasks"], [])

    def test_wakeup_still_due_survives_other_inputs(self):
        # Audit of release 9: a notification in between does not cancel the wakeup.
        self.write("w3", [tool_use("z", "ScheduleWakeup", ts=T2, delaySeconds=1200, reason="r"),
                          tool_result("z", "Next wakeup scheduled for 10:50:00 (in 1200s).", ts=T2),
                          ts_entry("user", notification("b1", "completed"), T2), end_turn("sigo", T2)])
        self.assertIn("despertador programado (ScheduleWakeup) que ya no sonará", self.flight("w3")["tasks"])

    def test_synchronous_agent_is_not_background_work(self):
        # Audit of release 9: a synchronous Agent result also carries an agentId.
        self.write("sa", [tool_use("tu1", "Agent", description="Verify TL-002"),
                          tool_result("tu1", "Informe final...\nagentId: sync1 (for resuming)"),
                          end_turn("hecho", T1)])
        self.assertEqual(self.flight("sa")["tasks"], [])

    def test_limit_failure_settled_by_a_later_turn(self):
        # Audit of release 9: the session relaunched the reviewer and closed its turn.
        self.write("lf", [*agent("a", "Review diff", "ag1"),
                          ts_entry("user", notification("ag1", "failed", "API error: You've hit your 5-hour limit"), T2),
                          *agent("b", "Review diff again", "ag2", ts=T2),
                          ts_entry("user", notification("ag2", "completed", "done"), T2),
                          end_turn("aplicado, espero al usuario", T3)])
        self.assertEqual(self.flight("lf", now="2026-11-12T11:30:00Z")["tasks"], [])

    def test_provisional_completed_survives_the_turn_end(self):
        # Audit of release 10: "the agent is still running" then end_turn is the usual shape;
        # the agent dies with the process and must be named.
        self.write("pc", [*agent("c", "Fix TL-262", "ag3"),
                          ts_entry("user", notification("ag3", "completed", "Agent is waiting on its own background work"), T2),
                          end_turn("el agente sigue en marcha, te aviso", T2)])
        self.assertEqual(self.flight("pc", now="2026-11-12T11:30:00Z")["tasks"], ['subagente "Fix TL-262"'])
        self.write("pc2", [*agent("c", "Fix TL-262", "ag3"),
                           ts_entry("user", notification("ag3", "completed", "Agent is waiting on its own background work"), T2),
                           end_turn("sigue", T2),
                           ts_entry("user", notification("ag3", "completed", "Final report"), T3),
                           end_turn("hecho", T3)])
        self.assertEqual(self.flight("pc2", now="2026-11-12T11:30:00Z")["tasks"], [])

    def handback(self, agent_id, body, ts=T2, **extra):
        e = ts_entry("user", f'Another Claude session sent a message:\n<agent-message from="{agent_id}">\n{body}', ts,
                     isMeta=True)
        e["origin"] = {"kind": "peer", "from": agent_id, "senderTaskId": agent_id, "body": body, **extra}
        return e

    def test_hand_back_settles_a_provisional_agent(self):
        # Audit of release 11: some agents hand back their report with no final notice.
        prov = ts_entry("user", notification("ag3", "completed", "Agent is waiting on its own background work"), T2)
        hb = self.handback("ag3", "[Subagent hand-back] The text below is the final report...")
        self.write("hb", [*agent("c", "Fix TL-262", "ag3"), prov, hb, end_turn("sigo esperando tu decisión", T3)])
        self.assertEqual(self.flight("hb", now="2026-11-12T11:30:00Z")["tasks"], [])
        # Written before the provisional notice of the same second.
        self.write("hb2", [*agent("c", "Fix TL-262", "ag3"), hb, prov, end_turn("sigo esperando", T3)])
        self.assertEqual(self.flight("hb2", now="2026-11-12T11:30:00Z")["tasks"], [])
        # Auto mode puts a warning before the marker (audit of release 12).
        warned = self.handback("ag3", "SECURITY WARNING: auto mode blocked this subagent's report. Reason: x.\n"
                                      "[Subagent hand-back] The text below is the final report...")
        self.write("hb4", [*agent("c", "Fix TL-262", "ag3"), prov, warned, end_turn("espero", T3)])
        self.assertEqual(self.flight("hb4", now="2026-11-12T11:30:00Z")["tasks"], [])
        # Resumed after its report: running again.
        self.write("hb3", [*agent("c", "Fix TL-262", "ag3"), hb,
                           tool_use("sm", "SendMessage", ts=T3, to="ag3", message="sigue"),
                           tool_result("sm", '{"success":true,"resumedAgentId":"ag3"}', ts=T3),
                           end_turn("reanudado", T3)])
        self.assertEqual(len(self.flight("hb3", now="2026-11-12T11:30:00Z")["tasks"]), 1)

    def test_hand_back_delivered_mid_turn_settles(self):
        # Audit of release 14: mid-turn the report only shows up as a queued_command
        # attachment carrying the origin (no user entry with origin).
        body = "[Subagent hand-back] The text below is the final report..."
        att = {"type": "attachment", "timestamp": T2,
               "attachment": {"type": "queued_command", "prompt": body,
                              "origin": {"kind": "peer", "from": "ag3", "senderTaskId": "ag3",
                                         "body": body, "handback": True}}}
        self.write("mt", [*agent("c", "Fix TL-262", "ag3"),
                          ts_entry("user", notification("ag3", "completed", "Agent is waiting on its own background work"), T2),
                          {"type": "queue-operation", "operation": "enqueue", "content": body, "timestamp": T2},
                          att, end_turn("sigo esperando", T3)])
        self.assertEqual(self.flight("mt", now="2026-11-12T11:30:00Z")["tasks"], [])

    def test_unanswered_peer_message_is_pending(self):
        # Audit of release 16: peer messages are written as isMeta but still need a turn.
        peer = ts_entry("user", "Another Claude session sent a message: revisa X", T2, isMeta=True)
        peer["origin"] = {"kind": "peer", "from": "uds:/tmp/x.sock", "body": "revisa X"}
        self.write("pm", [end_turn("ok", T1), peer])
        self.assertEqual(self.flight("pm")["pending"], "its last input had no answer yet")
        meta = ts_entry("user", "Caveat: local command output", T2, isMeta=True)
        self.write("mm", [end_turn("ok", T1), meta])
        self.assertEqual(self.flight("mm")["pending"], "")

    def test_turn_cut_mid_way_is_pending(self):
        # Audit of release 16: a tool result with no assistant entry after it.
        self.write("mw", [ts_entry("user", "haz", T1), tool_use("t", "Read", ts=T2, file_path="/x"),
                          tool_result("t", "contenido", ts=T2)])
        self.assertEqual(self.flight("mw")["pending"], "its last input had no answer yet")

    def test_interim_message_mid_turn_does_not_settle(self):
        # Audit of release 15: the attachment branch needs the same guard (34 real interim
        # queued_command lines carry a peer origin without the flag).
        body = "Interim (triaje): still working on it"
        att = {"type": "attachment", "timestamp": T2,
               "attachment": {"type": "queued_command", "prompt": body,
                              "origin": {"kind": "peer", "from": "ag3", "senderTaskId": "ag3", "body": body}}}
        self.write("ia", [*agent("c", "Fix TL-262", "ag3"), att, end_turn("sigo", T3)])
        self.assertEqual(self.flight("ia", now="2026-11-12T11:30:00Z")["tasks"], ['subagente "Fix TL-262"'])

    def test_cli_handback_flag_alone_settles(self):
        e = self.handback("ag3", "Report: done.", handback=True)
        self.write("hf", [*agent("c", "Fix TL-262", "ag3"), e, end_turn("hecho", T3)])
        self.assertEqual(self.flight("hf", now="2026-11-12T11:30:00Z")["tasks"], [])

    def test_text_mentioning_the_marker_does_not_settle(self):
        # Audit of release 13: an interim message or a quoted transcript that mentions the
        # marker must not settle a running agent.
        interim = self.handback("ag3", "Interim: still working, checking why [Subagent hand-back] lines are missed")
        quoted = tool_result("rd", '<agent-message from="ag3">\n 41\t...\n 42\t[Subagent hand-back] x', ts=T2)
        self.write("im", [*agent("c", "Fix TL-262", "ag3"), interim,
                          tool_use("rd", "Read", ts=T2, file_path="/x"), quoted, end_turn("sigue", T3)])
        self.assertEqual(self.flight("im", now="2026-11-12T11:30:00Z")["tasks"], ['subagente "Fix TL-262"'])

    def test_usage_after_the_limit_still_counts_as_cut(self):
        # Audit of release 11: the owner hits the limit, types /usage, then switches.
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # under the 8-hour error age
        self.write("ul", [ts_entry("user", "haz", now),
                          ts_entry("assistant", "You've hit your 5-hour limit", now, isApiErrorMessage=True),
                          ts_entry("user", "<command-name>/usage</command-name>\n<command-message>usage</command-message>", now),
                          ts_entry("user", "<local-command-stdout>Session 100%</local-command-stdout>", now)])
        self.assertTrue(cca.ended_on_error("ul"))

    def test_task_notification_after_the_limit_still_counts_as_cut(self):
        # Auto switch release 42: subagents fail on the same limit seconds after the error
        # and their notices come after it; the tab stopped on the limit all the same, and
        # the unanswered notice still makes the turn pending (so it gets "continúa").
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        note = "<task-notification>\n<task-id>a1</task-id>\n<status>failed</status>\n</task-notification>"
        self.write("tn", [ts_entry("user", "haz", now),
                          ts_entry("assistant", "You've hit your weekly limit", now,
                                   isApiErrorMessage=True, error="rate_limit"),
                          ts_entry("user", note, now)])
        self.assertTrue(cca.ended_on_limit("tn"))
        self.assertEqual(self.flight("tn")["pending"], "its last input had no answer yet")
        # A real message after the notice is an input of his: the error is no longer last.
        self.write("tm", [ts_entry("assistant", "You've hit your weekly limit", now,
                                   isApiErrorMessage=True, error="rate_limit"),
                          ts_entry("user", note, now), ts_entry("user", "sigue", now)])
        self.assertFalse(cca.ended_on_limit("tm"))

    def test_local_slash_commands_are_not_inputs(self):
        # Audit of release 10: /usage and /context write no local-command output.
        for cmd in ("usage", "context"):
            self.write("lc", [end_turn("ok", T1), ts_entry("user", f"<command-name>/{cmd}</command-name>\n"
                                                                    f"<command-message>{cmd}</command-message>", T2)])
            self.assertEqual(self.flight("lc")["pending"], "", cmd)

    def test_command_backgrounded_by_hand(self):
        # Audit of release 10: Ctrl+B writes "manually backgrounded by user with ID".
        self.write("mb", [tool_use("tb", "Bash", command="make", description="Build all"),
                          tool_result("tb", "Command was manually backgrounded by user with ID: bx9"),
                          end_turn("sigue", T1)])
        self.assertEqual(self.flight("mb")["tasks"], ['comando en segundo plano "Build all"'])

    def test_expired_monitor_is_not_listed(self):
        # Audit of release 9: expiry comes as a notification without status, and the
        # result names the expiry.
        self.write("mo", [tool_use("m", "Monitor", command="tail -f x", description="Watch"),
                          tool_result("m", "Monitor started (task bm1, expires in 30m unless the source ends first)"),
                          end_turn("vigilando", T1)])
        self.assertEqual(self.flight("mo", now="2026-11-12T10:20:00Z")["tasks"], ['monitor "Watch"'])
        self.assertEqual(self.flight("mo", now="2026-11-12T10:45:00Z")["tasks"], [])
        self.write("mo2", [tool_use("m", "Monitor", command="tail -f x", description="Watch"),
                           tool_result("m", "Monitor started (task bm2, expires in 60m)"),
                           ts_entry("user", "<task-notification>\n<task-id>bm2</task-id>\n<summary>Monitor event: x"
                                            "</summary>\n<event>[Monitor expired after 30m with 3 events delivered.]"
                                            "</event>\n</task-notification>", T2),
                           end_turn("ok", T2)])
        self.assertEqual(self.flight("mo2")["tasks"], [])

    def test_command_moved_to_background_by_its_timeout(self):
        # Audit of release 9: a foreground Bash that hit its timeout keeps running.
        self.write("bt", [tool_use("tb", "Bash", command="make plan", description="Plan all"),
                          tool_result("tb", "Command did not complete within its 120s timeout and was moved to "
                                            "the background (ID: bq4j5)."),
                          end_turn("sigue en segundo plano", T1)])
        self.assertEqual(self.flight("bt")["tasks"], ['comando en segundo plano "Plan all"'])

    def test_answered_slash_command_is_an_input(self):
        # Audit of release 9: /skeleton-sync is answered by the model; a local one is not.
        self.write("sc", [end_turn("ok", T1), ts_entry("user", "<command-name>/skeleton-sync</command-name>", T2)])
        self.assertEqual(self.flight("sc")["pending"], "its last input had no answer yet")

    def test_task_stopped_on_purpose_is_not_listed(self):
        # Audit of release 8: TaskStop leaves no notification.
        self.write("s1", [tool_use("tu2", "Bash", command="x", description="Old watcher", run_in_background=True),
                          tool_result("tu2", "Command running in background with ID: bj1"),
                          tool_use("ts", "TaskStop", task_id="bj1"),
                          tool_result("ts", '{"message":"Successfully stopped task: bj1"}'),
                          ts_entry("assistant", "paro el vigilante", T2)])
        self.assertEqual(self.flight("s1")["tasks"], [])

    def test_provisional_completed_and_limit_failure_still_count(self):
        # Audit of release 8: "completed ... waiting on its own background work" is not the end,
        # a "failed" on the usage limit needs a resume. Both launched before this process.
        self.write("p1", [*agent("a", "Fix R01b", "ag1", ts=BEFORE), *agent("b", "Verify R01a", "ag2", ts=BEFORE),
                          *agent("c", "Really done", "ag3", ts=BEFORE),
                          ts_entry("user", notification("ag1", "completed", "Agent is waiting on its own background work"), T2),
                          ts_entry("user", notification("ag2", "failed", "Agent terminated early due to an API error: "
                                                        "You've hit your 5-hour limit"), T2),
                          ts_entry("user", notification("ag3", "failed", "real error"), T2),
                          ts_entry("assistant", "vale", T3)])
        self.assertEqual(sorted(self.flight("p1")["tasks"]),
                         ['subagente "Fix R01b"', 'subagente "Verify R01a" (cortado por el límite)'])

    def test_command_finished_by_notification(self):
        self.write("c1", [tool_use("tu2", "Bash", command="x", description="Build", run_in_background=True),
                          tool_result("tu2", "Command running in background with ID: b77"),
                          ts_entry("user", notification("b77", "completed", tool_use="tu2"), T2),
                          ts_entry("assistant", "ya", T2)])
        self.assertEqual(self.flight("c1")["tasks"], [])

    def test_launches_of_an_earlier_process_do_not_count(self):
        self.write("o1", [*agent("tu1", "Old", "old1", ts=BEFORE), ts_entry("assistant", "fin", BEFORE)])
        self.assertEqual(self.flight("o1")["tasks"], [])

    def test_message_explains_the_switch_and_names_the_stopped_work(self):
        text = cca.continue_text("na", {"pending": "", "tasks": ['subagente "Audit"']})
        self.assertTrue(text.startswith("continúa con lo que estabas haciendo"))
        for part in ("cuenta na", "cambio de cuenta", "puede que tu turno anterior quedara cortado",
                     "subagentes", "reanúdalos o relánzalos",
                     "no empieces nada nuevo: di solo que sigues esperando",
                     'quedaron sin terminar: subagente "Audit"', "no una aprobación nueva"):
            self.assertIn(part, text)
        self.assertNotIn("quedaron sin terminar", cca.continue_text("na", {"pending": "", "tasks": []}))

    def test_lt_message_asks_for_auto_subagents(self):
        """the owner, 2026-11-13: after a switch to lt the subagents are relaunched in auto."""
        with mock.patch.object(cca.claude_peer, "bypass_disabled", lambda d: d == cca.ACCOUNTS["lt"]["dir"]):
            self.assertIn("relanza los subagentes y comandos en modo auto", cca.continue_text("lt", {"tasks": []}))
            self.assertNotIn("modo auto", cca.continue_text("mt", {"tasks": []}))
        # The real policy decides, not the tag: an mt without bypass gets the line, lt with it not.
        with mock.patch.object(cca.claude_peer, "bypass_disabled", lambda d: d == cca.ACCOUNTS["mt"]["dir"]):
            self.assertIn("relanza los subagentes y comandos en modo auto", cca.continue_text("mt", {"tasks": []}))
            self.assertNotIn("modo auto", cca.continue_text("lt", {"tasks": []}))

    def test_in_flight_work_makes_an_idle_tab_cut(self):
        sent = []
        self.write("idle-bg", [*agent("tu1", "Audit", "z1"), ts_entry("assistant", "espero al auditor", T1)])
        moved = [{"session": "idle-bg", "name": "x", "status": "idle", "host": 1, "pid": 10,
                  "started_ms": cca._ms(START)}]
        with mock.patch.object(cca, "companion_hosts", return_value={1}), \
             mock.patch.object(cca, "request_reopen", return_value={"idle-bg": {"result": "reopened"}}), \
             mock.patch.object(cca, "wait_new_process", return_value={"pid": 11}), \
             mock.patch.object(cca, "send_continue", side_effect=lambda info, text, tag: sent.append(text)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.continue_moved("na", moved, "cut")
        self.assertEqual(len(sent), 1)
        self.assertIn('subagente "Audit"', sent[0])
        self.assertIn("1 background task(s) stopped", buf.getvalue())


class StatusRecheck(_ResetAge):
    """Audit of release 16: a tab may start a turn while the owner answers the question."""

    def run_move(self, scope, now_status, listed="idle", status_ms=0):
        procs = [{"pid": 2, "tag": "lt", "status": listed, "name": "b", "session": "s2", "command": "c2", "host": 1,
                  "status_ms": status_ms},
                 {"pid": 3, "tag": "lt", "status": "busy", "name": "c", "session": "s3", "command": "c3", "host": 1}]
        killed, moved = [], []
        with mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=procs), \
             mock.patch.object(cca, "process_command", side_effect=lambda p: f"c{p}"), \
             mock.patch.object(cca, "current_status", side_effect=lambda s: now_status if s["pid"] == 2 else "busy"), \
             mock.patch.object(cca.os, "kill", side_effect=lambda p, sig: killed.append(p)), \
             mock.patch.object(cca, "wait_gone"), mock.patch.object(cca, "warn_stale_companions"), \
             mock.patch.object(cca, "continue_moved", side_effect=lambda tag, ended, cont: moved.extend(ended)), \
             redirect_stdout(io.StringIO()):
            cca.move_open(scope, "cut")
        return killed, moved

    def test_idle_only_leaves_a_tab_that_started_working(self):
        killed, _ = self.run_move("idle", "busy")
        self.assertEqual(killed, [])

    def test_all_marks_it_busy_so_it_continues(self):
        killed, moved = self.run_move("all", "busy")
        self.assertEqual(sorted(killed), [2, 3])
        self.assertEqual({m["pid"]: m["status"] for m in moved}[2], "busy")

    def test_waiting_tab_is_not_moved_as_idle(self):
        # Audit of release 17: "waiting" (a permission prompt or a question) is a turn in
        # progress; ending it under "idle only" would lose the dialog.
        killed, _ = self.run_move("idle", "waiting", listed="waiting")
        self.assertEqual(killed, [])
        killed, _ = self.run_move("idle", "waiting", listed="idle")
        self.assertEqual(killed, [])

    def test_waiting_tab_moved_with_all_counts_as_cut(self):
        killed, moved = self.run_move("all", "waiting", listed="waiting")
        self.assertIn(2, killed)
        self.assertEqual({m["pid"]: m["status"] for m in moved}[2], "waiting")

    def test_busy_tab_that_finished_meanwhile_is_judged_by_its_transcript(self):
        killed, moved = self.run_move("all", "idle", listed="busy")
        self.assertEqual({m["pid"]: m["status"] for m in moved}[2], "idle")

    def test_safe_scope_leaves_tabs_with_background_work(self):
        # Unattended moves (claude-auto-switch.py) must not kill an idle tab's own work.
        with mock.patch.object(cca, "unsafe_to_move", side_effect=lambda s: ['comando en segundo plano "apply"'] if s["pid"] == 2 else []):
            killed, _ = self.run_move("safe", "idle")
        self.assertEqual(killed, [])
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]):
            killed, _ = self.run_move("safe", "idle")
        self.assertEqual(killed, [2])

    def test_unsafe_to_move_rules(self):
        base = {"session": "s", "started_ms": 1, "status": "idle"}
        flight = {"pending": "", "tasks": ['subagente "x"', 'monitor "y"']}
        with mock.patch.object(cca, "work_in_flight", return_value=flight), \
             mock.patch.object(cca, "ended_on_limit", return_value=False):
            self.assertEqual(cca.unsafe_to_move(base), ['subagente "x"', 'monitor "y"'],
                             "running subagents block when the tab did not stop on the limit")
        with mock.patch.object(cca, "work_in_flight", return_value=flight), \
             mock.patch.object(cca, "ended_on_limit", return_value=True):
            self.assertEqual(cca.unsafe_to_move(base), ['monitor "y"'], "stopped on the limit: its subagents too")
        dead = {"pending": "", "tasks": ['workflow "w" (cortado por el límite)', 'monitor "y"']}
        with mock.patch.object(cca, "work_in_flight", return_value=dead), \
             mock.patch.object(cca, "ended_on_limit", return_value=True):
            self.assertEqual(cca.unsafe_to_move(base), ['monitor "y"'],
                             "work that failed on the limit is dead (audit of auto switch release 30)")
        with mock.patch.object(cca, "work_in_flight", return_value=dead), \
             mock.patch.object(cca, "ended_on_limit", return_value=False):
            self.assertIn('workflow "w" (cortado por el límite)', cca.unsafe_to_move(base))
        for bad in ({"session": "", "started_ms": 1, "status": "idle"}, {"session": "s", "started_ms": 0, "status": "idle"},
                    {"session": "s", "started_ms": 1, "status": "?"}):
            self.assertEqual(cca.unsafe_to_move(bad), ["state unknown"])

    def test_safe_cut_moves_only_tabs_stopped_on_the_limit(self):
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "ended_on_limit", side_effect=lambda sid: False):
            killed, _ = self.run_move("safe-cut", "idle")
        self.assertEqual(killed, [])
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "ended_on_limit", side_effect=lambda sid: sid == "s2"):
            killed, _ = self.run_move("safe-cut", "idle")
        self.assertEqual(killed, [2])

    def test_safe_quiet_moves_tabs_stopped_on_the_limit_or_idle_for_a_while(self):
        # Audit of auto switch release 19: after going back up, busy tabs on the account left
        # never stop on its limit; they move once quiet.
        for quiet, limit, want in ((False, False, []), (True, False, [2]), (False, True, [2])):
            with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
                 mock.patch.object(cca, "quiet", return_value=quiet), \
                 mock.patch.object(cca, "ended_on_limit", return_value=limit):
                killed, _ = self.run_move("safe-quiet", "idle")
            self.assertEqual(killed, want, (quiet, limit))
        with mock.patch.object(cca, "unsafe_to_move", return_value=["monitor"]), \
             mock.patch.object(cca, "quiet", return_value=True):
            killed, _ = self.run_move("safe-quiet", "idle")
        self.assertEqual(killed, [], "background work still blocks")

    def test_live_sessions_reads_the_status_time(self):
        # Audit of auto switch release 21: quiet() must use statusUpdatedAt, not another field.
        root = Path(tempfile.mkdtemp(prefix="cca-ls-"))
        (root / "sessions").mkdir()
        (root / "sessions" / "77.json").write_text(json.dumps(
            {"status": "idle", "sessionId": "s", "startedAt": 1, "statusUpdatedAt": 5000, "updatedAt": 9000}))
        def run(cmd, **kw):
            out = "77\n" if cmd[0] == "pgrep" else f"claude CLAUDE_CONFIG_DIR={root}"
            return mock.Mock(stdout=out, returncode=0)
        with mock.patch.object(cca.subprocess, "run", side_effect=run), \
             mock.patch.object(cca, "tag_for_dir", return_value="lt"), \
             mock.patch.dict(cca.ACCOUNTS, {"lt": {**cca.ACCOUNTS["lt"], "dir": root}}), \
             mock.patch.object(cca, "parent_pid", return_value=1):
            self.assertEqual(cca.live_sessions()[0]["status_ms"], 5000)

    def test_note_cut_moves_records_the_account_of_tabs_cut_on_the_limit(self):
        # Audit of auto switch release 23: claude-auto-switch reads this per account.
        path = Path(tempfile.mkdtemp(prefix="cca-cm-")) / "cut-moves.json"
        path.write_text(json.dumps({"mt": "2026-11-12T10:00:00Z"}))
        tabs = [{"tag": "lt", "session": "a"}, {"tag": "lt", "session": "b"}, {"tag": "na", "session": "c"}]
        errs = {"a": "2026-11-12T20:00:00Z", "b": "2026-11-12T20:05:00Z"}
        with mock.patch.object(cca, "CUT_MOVES", path), \
             mock.patch.object(cca, "ended_on_limit", side_effect=lambda sid: sid in errs), \
             mock.patch.object(cca, "_last_turn_error", side_effect=lambda sid: {"timestamp": errs.get(sid)}):
            cca.note_cut_moves(tabs)
        self.assertEqual(json.loads(path.read_text()),
                         {"mt": "2026-11-12T10:00:00Z", "lt": {"at": "2026-11-12T20:05:00Z", "text": ""}})

    def test_note_cut_moves_keeps_the_text_and_the_newest_of_a_batch(self):
        # Audit of auto switch release 26: the text carries the reset; two lt tabs in one
        # move keep the newer error whatever their order.
        path = Path(tempfile.mkdtemp(prefix="cca-cm-")) / "cut-moves.json"
        errs = {"a": ("2026-11-12T20:05:00Z", "limit · resets 3pm"), "b": ("2026-11-12T20:00:00Z", "older")}
        tabs = [{"tag": "lt", "session": "a"}, {"tag": "lt", "session": "b"}]
        with mock.patch.object(cca, "CUT_MOVES", path), \
             mock.patch.object(cca, "ended_on_limit", return_value=True), \
             mock.patch.object(cca, "_last_turn_error", side_effect=lambda sid: {
                 "timestamp": errs[sid][0], "message": {"content": [{"type": "text", "text": errs[sid][1]}]}}):
            cca.note_cut_moves(tabs)
        self.assertEqual(json.loads(path.read_text()), {"lt": {"at": "2026-11-12T20:05:00Z", "text": "limit · resets 3pm"}})

    def test_note_cut_moves_keeps_newer_and_skips_errors_from_before_the_process(self):
        # Audit of auto switch release 24: an lt error on a tab now on mt is not mt's.
        path = Path(tempfile.mkdtemp(prefix="cca-cm-")) / "cut-moves.json"
        path.write_text(json.dumps({"lt": "2026-11-12T21:00:00Z"}))
        start = cca._ms("2026-11-12T20:30:00Z")
        tabs = [{"tag": "lt", "session": "a", "started_ms": start},
                {"tag": "mt", "session": "b", "started_ms": start}]
        errs = {"a": "2026-11-12T20:45:00Z", "b": "2026-11-12T20:10:00Z"}
        with mock.patch.object(cca, "CUT_MOVES", path), \
             mock.patch.object(cca, "ended_on_limit", return_value=True), \
             mock.patch.object(cca, "_last_turn_error", side_effect=lambda sid: {"timestamp": errs[sid]}):
            cca.note_cut_moves(tabs)
        self.assertEqual(json.loads(path.read_text()), {"lt": "2026-11-12T21:00:00Z"})

    def test_move_open_records_cut_moves_before_the_continue_even_on_error(self):
        calls = []
        procs = [{"pid": 2, "tag": "lt", "status": "idle", "name": "t", "session": "s2", "command": "c2", "host": 1}]
        def kill(p, sig):
            raise RuntimeError("boom")
        with mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=procs), \
             mock.patch.object(cca, "process_command", side_effect=lambda p: f"c{p}"), \
             mock.patch.object(cca, "current_status", return_value="idle"), \
             mock.patch.object(cca, "wait_gone"), mock.patch.object(cca, "warn_stale_companions"), \
             mock.patch.object(cca, "note_cut_moves", side_effect=lambda e: calls.append(("note", len(e)))), \
             mock.patch.object(cca, "continue_moved", side_effect=lambda *a: calls.append(("continue",))), \
             redirect_stdout(io.StringIO()):
            with mock.patch.object(cca.os, "kill"):
                cca.move_open("idle", "cut")
            self.assertEqual(calls, [("note", 1), ("continue",)])
            calls.clear()
            with mock.patch.object(cca.os, "kill", side_effect=kill), self.assertRaises(RuntimeError):
                cca.move_open("idle", "cut")
            self.assertEqual(calls, [("note", 0)], "recorded even when the loop broke")

    def test_limit_failure_ignores_quoted_text(self):
        # Audit of auto switch release 31.
        self.assertFalse(cca.limit_failure('Background command "grep -i rate-limit app.log" failed with exit code 1'))
        self.assertFalse(cca.limit_failure('Background command `grep rate_limit x` failed with exit code 1'))
        self.assertTrue(cca.limit_failure("Agent terminated early due to an API error: You've hit your 5-hour limit"))
        self.assertFalse(cca.limit_failure("Agent terminated early due to an API error: API Error: this message was flagged "
                                           "as a possible violation of our usage policy"), "a refusal is not a cut")


    def test_quiet_rule(self):
        now = 10_000_000
        self.assertTrue(cca.quiet({"status_ms": now - 61 * 60_000}, now))
        self.assertFalse(cca.quiet({"status_ms": now - 30 * 60_000}, now), "an hour, not 15 min (audit of release 21)")
        self.assertFalse(cca.quiet({"status_ms": 0}, now), "unknown: not quiet")

    def test_manual_idle_leaves_a_tab_running_its_own_work(self):
        # Audit of auto switch release 34: claude-cursor-move killed a tab's own command.
        with mock.patch.object(cca, "unsafe_to_move", return_value=['comando en segundo plano "build"']):
            killed, _ = self.run_move("idle", "idle")
        self.assertEqual(killed, [])
        with mock.patch.object(cca, "unsafe_to_move", return_value=["state unknown"]):
            killed, _ = self.run_move("idle", "idle")
        self.assertEqual(killed, [2], "unknown state is still moved by hand")
        with mock.patch.object(cca, "unsafe_to_move", return_value=['comando en segundo plano "build"']):
            killed, _ = self.run_move("all", "idle")
        self.assertIn(2, killed, "--scope all still ends it")

    def test_manual_ask_asks_when_only_own_work_differs_and_says_why(self):
        # Audit of auto switch release 35: no busy tab, one idle tab with a build running.
        procs = [{"pid": 2, "tag": "lt", "status": "idle", "name": "b", "session": "s2", "command": "c2", "host": 1}]
        out = io.StringIO()
        with mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=procs), \
             mock.patch.object(cca, "process_command", side_effect=lambda p: f"c{p}"), \
             mock.patch.object(cca, "current_status", return_value="idle"), \
             mock.patch.object(cca, "unsafe_to_move", return_value=['comando en segundo plano "build"']), \
             mock.patch.object(cca, "ask_scope", return_value="idle") as ask, \
             mock.patch.object(cca.os, "kill") as kill, \
             mock.patch.object(cca, "wait_gone"), mock.patch.object(cca, "warn_stale_companions"), \
             mock.patch.object(cca, "continue_moved"), redirect_stdout(out):
            cca.move_open("ask", "cut")
        ask.assert_called_once()
        self.assertIn('pid 2 runs its own work: comando en segundo plano "build"', out.getvalue(), "shown before asking")
        kill.assert_not_called()
        self.assertIn('running its own work (comando en segundo plano "build"; --scope all ends it)', out.getvalue())

    def test_unattended_move_reads_own_work_again_before_the_kill(self):
        # Audit of auto switch release 37: the unattended half of the re-check.
        calls = {"n": 0}
        def work(s):
            calls["n"] += 1
            return [] if calls["n"] == 1 else ['monitor "deploy"']
        with mock.patch.object(cca, "unsafe_to_move", side_effect=work), \
             mock.patch.object(cca, "quiet", return_value=True):
            killed, _ = self.run_move("safe-quiet", "idle")
        self.assertEqual(killed, [])

    def test_safe_quiet_reads_quiet_again_before_the_kill(self):
        # Audit of auto switch release 38: a turn run between listing and kill.
        now_ms = int(time.time() * 1000)
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "quiet", side_effect=lambda s: s["status_ms"] < now_ms - 3_600_000), \
             mock.patch.object(cca, "ended_on_limit", return_value=False), \
             mock.patch.object(cca, "current_record", return_value={"statusUpdatedAt": now_ms}):
            killed, _ = self.run_move("safe-quiet", "idle", status_ms=1)
        self.assertEqual(killed, [])
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "quiet", side_effect=lambda s: s["status_ms"] < now_ms - 3_600_000), \
             mock.patch.object(cca, "ended_on_limit", return_value=False), \
             mock.patch.object(cca, "current_record", return_value={"statusUpdatedAt": 1}):
            killed, _ = self.run_move("safe-quiet", "idle", status_ms=1)
        self.assertEqual(killed, [2], "unchanged since the listing: moved")
        # An unreadable record at kill time (fresh 0): judged by the listing (audit of release 40).
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "quiet", side_effect=lambda s: s["status_ms"] < now_ms - 3_600_000), \
             mock.patch.object(cca, "ended_on_limit", return_value=False), \
             mock.patch.object(cca, "current_record", return_value={}):
            killed, _ = self.run_move("safe-quiet", "idle", status_ms=1)
        self.assertEqual(killed, [2])
        # Changed but still quiet (an old status time): moved; changed and recent but the
        # tab stopped on the limit: moved with its continue (audit of release 39).
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "quiet", side_effect=lambda s: s["status_ms"] < now_ms - 3_600_000), \
             mock.patch.object(cca, "ended_on_limit", return_value=False), \
             mock.patch.object(cca, "current_record", return_value={"statusUpdatedAt": 2}):
            killed, _ = self.run_move("safe-quiet", "idle", status_ms=1)
        self.assertEqual(killed, [2])
        with mock.patch.object(cca, "unsafe_to_move", return_value=[]), \
             mock.patch.object(cca, "quiet", return_value=False), \
             mock.patch.object(cca, "ended_on_limit", return_value=True), \
             mock.patch.object(cca, "current_record", return_value={"statusUpdatedAt": now_ms}):
            killed, _ = self.run_move("safe-quiet", "idle", status_ms=1)
        self.assertEqual(killed, [2])

    def test_work_started_while_answering_is_not_killed(self):
        # Audit of auto switch release 36: own work is read again right before the kill.
        calls = {"n": 0}
        def work(s):
            calls["n"] += 1
            return [] if calls["n"] == 1 else ['monitor "deploy"']
        with mock.patch.object(cca, "unsafe_to_move", side_effect=work):
            killed, _ = self.run_move("idle", "idle")
        self.assertEqual(killed, [])

    def test_still_idle_is_moved(self):
        killed, _ = self.run_move("idle", "idle")
        self.assertEqual(killed, [2])


class Summary(TempPool):
    def test_summary_records_each_outcome_with_its_cut_flag(self):
        # Audit of auto switch release 15: claude-auto-switch reads this, not the text.
        cca.SUMMARY.clear()
        self.addCleanup(cca.SUMMARY.clear)
        moved = [{"session": "a", "name": "A: one", "status": "busy", "host": 1, "pid": 1},
                 {"session": "b", "name": "B", "status": "idle", "host": 1, "pid": 2}]
        with mock.patch.object(cca, "companion_hosts", return_value={1}), \
             mock.patch.object(cca, "request_reopen", return_value={"a": {"result": "reopened"}, "b": {"result": "ambiguous"}}), \
             mock.patch.object(cca, "wait_new_process", return_value={"pid": 9}), \
             mock.patch.object(cca, "send_continue", return_value=None), redirect_stdout(io.StringIO()):
            cca.continue_moved("mt", moved, "cut")
        self.assertEqual({e["name"]: (e["outcome"], e["cut"]) for e in cca.SUMMARY},
                         {"A: one": ("continued", True), "B": ("not_reopened", False)})


class SummaryPaths(_ResetAge):
    """Audit of auto switch release 17: every path records, and the line stays one ASCII line."""

    def run_main(self, procs, cont="cut", command=lambda p: f"c{p}"):
        cca.SUMMARY.clear()
        self.addCleanup(cca.SUMMARY.clear)
        argv = ["x", "--move-open", "--scope", "all", "--continue", cont, "--summary"]
        with mock.patch.object(cca.sys, "argv", argv), \
             mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=procs), \
             mock.patch.object(cca, "process_command", side_effect=command), \
             mock.patch.object(cca, "current_status", side_effect=lambda s: s["status"]), \
             mock.patch.object(cca.os, "kill"), mock.patch.object(cca, "wait_gone"), \
             mock.patch.object(cca, "warn_stale_companions"), \
             mock.patch.object(cca, "ended_on_error", return_value=False), \
             mock.patch.object(cca, "continue_moved", side_effect=lambda tag, ended, c: [cca.record(t, "continued") for t in ended]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.main()
        line = [l for l in buf.getvalue().split("\n") if l.startswith("MOVE-SUMMARY ")]
        self.assertEqual(len(line), 1)
        return line[0], json.loads(line[0][len("MOVE-SUMMARY "):])

    def proc(self, pid, name="tab", status="idle"):
        return {"pid": pid, "tag": "lt", "status": status, "name": name, "session": f"s{pid}", "command": f"c{pid}", "host": 1}

    def test_summary_line_is_ascii_even_for_odd_names(self):
        line, data = self.run_main([self.proc(1, name="notes\u2028more")])
        self.assertTrue(line.isascii())
        self.assertEqual(data[0]["name"], "notes\u2028more")

    def test_changed_pid_is_recorded_as_gone(self):
        _, data = self.run_main([self.proc(1)], command=lambda p: "something else")
        self.assertEqual([e["outcome"] for e in data], ["gone"])

    def test_continue_off_records_ended_with_its_cut_flag(self):
        _, data = self.run_main([self.proc(1, status="busy"), self.proc(2)], cont="off")
        self.assertEqual({e["session"]: (e["outcome"], e["cut"]) for e in data},
                         {"s1": ("ended", True), "s2": ("ended", False)})

    def test_continue_off_uses_the_same_cut_rule_as_the_continue(self):
        # Audit of auto switch release 18: an idle tab with work in flight is cut in every mode.
        flight = lambda session, since, now=None: {"pending": [], "tasks": ["agent x"] if session == "s2" else []}
        with mock.patch.object(cca, "work_in_flight", side_effect=flight):
            _, data = self.run_main([self.proc(1), self.proc(2)], cont="off")
        self.assertEqual({e["session"]: e["cut"] for e in data}, {"s1": False, "s2": True})


class OldOrPolicyErrors(TempPool):
    def test_old_error_is_a_cut_only_in_a_manual_move(self):
        # Audit of auto switch release 10: abandoned tabs must not be woken unattended.
        # Audit of release 11: a manual move still continues a weekly-limit cut from last night.
        old = "2026-01-01T00:00:00Z"
        self.write("e1", [entry("user", "x"), {**entry("assistant", "You've hit your weekly limit", isApiErrorMessage=True,
                                                      error="rate_limit"), "timestamp": old}])
        self.assertTrue(cca.ended_on_error("e1"), "manual: any age")
        self.assertTrue(cca.ended_on_limit("e1"))
        with mock.patch.object(cca, "ERROR_MAX_AGE_HOURS", 8):
            self.assertFalse(cca.ended_on_error("e1"), "unattended: over 8 h is history")
            self.assertFalse(cca.ended_on_limit("e1"))
            self.assertTrue(cca.old_error("e1"))
            self.assertEqual(cca.ERROR_MAX_AGE_HOURS, 8, "old_error restores the limit")

    def test_unattended_move_labels_an_old_error_honestly(self):
        self.write("e4", [entry("user", "x"), {**entry("assistant", "You've hit your weekly limit", isApiErrorMessage=True,
                                                      error="rate_limit"), "timestamp": "2026-01-01T00:00:00Z"}])
        moved = [{"session": "e4", "name": "old", "status": "idle", "host": 1, "pid": 10, "started_ms": 1}]
        with mock.patch.object(cca, "ERROR_MAX_AGE_HOURS", 8), \
             mock.patch.object(cca, "companion_hosts", return_value={1}), \
             mock.patch.object(cca, "request_reopen", return_value={"e4": {"result": "reopened"}}), \
             mock.patch.object(cca, "wait_new_process", return_value={"pid": 11}), \
             mock.patch.object(cca, "send_continue") as send:
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.continue_moved("mt", moved, "cut")
        send.assert_not_called()
        self.assertIn("stopped on an error over 8 h old; type continúa in it", buf.getvalue())

    def test_policy_refusal_is_not_a_cut(self):
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.write("e2", [entry("user", "x"), {**entry("assistant", "This request may violate our Usage Policy",
                                                      isApiErrorMessage=True), "timestamp": now}])
        self.assertFalse(cca.ended_on_error("e2"))
        self.write("e3", [entry("user", "x"), {**entry("assistant", "Overloaded", isApiErrorMessage=True), "timestamp": now}])
        self.assertTrue(cca.ended_on_error("e3"))


class LimitNotAnyError(TempPool):
    def test_overload_is_not_the_usage_limit(self):
        # Audit of auto switch release 3: a 529 says nothing about the quota.
        self.write("o1", [entry("user", "x"), entry("assistant", "Overloaded", isApiErrorMessage=True, apiErrorStatus=529)])
        self.assertTrue(cca.ended_on_error("o1"))
        self.assertFalse(cca.ended_on_limit("o1"))
        self.write("l1", [entry("user", "x"), entry("assistant", "You've hit your 5-hour limit", isApiErrorMessage=True,
                                                     error="rate_limit")])
        self.assertTrue(cca.ended_on_limit("l1"))


class WaitingIsCut(TempPool):
    def test_waiting_status_makes_the_tab_cut(self):
        sent = []
        self.write("wt", [ts_entry("user", "haz", T1), tool_use("q", "AskUserQuestion", ts=T1, questions=[])])
        moved = [{"session": "wt", "name": "x", "status": "waiting", "host": 1, "pid": 10,
                  "started_ms": cca._ms(START)}]
        with mock.patch.object(cca, "companion_hosts", return_value={1}), \
             mock.patch.object(cca, "request_reopen", return_value={"wt": {"result": "reopened"}}), \
             mock.patch.object(cca, "wait_new_process", return_value={"pid": 11}), \
             mock.patch.object(cca, "send_continue", side_effect=lambda info, text, tag: sent.append(text)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.continue_moved("na", moved, "cut")
        self.assertEqual(len(sent), 1)
        self.assertIn("was waiting on a prompt or a question", buf.getvalue())


class StaleCompanion(TempPool):
    def test_window_running_an_older_companion_is_reported(self):
        (self.root / "windows").mkdir(parents=True)
        (self.root / "windows" / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid()}))
        exts = self.tmp / "extensions.json"
        exts.write_text(json.dumps([{"identifier": {"id": cca.COMPANION_ID}, "version": "0.3.0"}]))
        with mock.patch.object(cca, "CURSOR_EXTENSIONS", exts):
            self.assertEqual(cca.stale_companions(), [(os.getpid(), "0.2.0 or older", "0.3.0")])
            (self.root / "windows" / f"{os.getpid()}.json").write_text(json.dumps({"companion": "0.3.0"}))
            self.assertEqual(cca.stale_companions(), [])

    def test_compares_with_what_cursor_installed_not_the_repo(self):
        # Audit of release 8: a bumped package.json not installed yet is no reason to reload.
        (self.root / "windows").mkdir(parents=True)
        (self.root / "windows" / f"{os.getpid()}.json").write_text(json.dumps({"companion": "0.3.0"}))
        exts = self.tmp / "extensions.json"
        exts.write_text(json.dumps([{"identifier": {"id": cca.COMPANION_ID}, "version": "0.3.0"}]))
        with mock.patch.object(cca, "CURSOR_EXTENSIONS", exts):
            self.assertEqual(cca.stale_companions(), [])

    def test_warning_comes_first_on_every_switch(self):
        # Audit of release 8: it was printed only when some tab was moved and continued.
        with mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=[]), \
             mock.patch.object(cca, "stale_companions", return_value=[(9, "0.1.0", "0.3.0")]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                cca.move_open("idle", "off")
        self.assertIn("runs the tab companion 0.1.0, installed 0.3.0", buf.getvalue())


class SendContinue(unittest.TestCase):
    def test_goes_through_claude_peer_with_the_account_sessions_dir(self):
        with mock.patch.object(cca.claude_peer, "deliver", return_value=(True, "bypass")) as deliver, \
             mock.patch.object(cca, "transcript", return_value=None):
            self.assertIsNone(cca.send_continue({"pid": 5, "sessionId": "s"}, "continúa", "na"))
        self.assertEqual(deliver.call_args.args[2], cca.ACCOUNTS["na"]["dir"] / "sessions")

    def test_not_taken_returns_why(self):
        with mock.patch.object(cca.claude_peer, "deliver", return_value=(False, "held by the session")), \
             mock.patch.object(cca, "transcript", return_value=None):
            self.assertEqual(cca.send_continue({"pid": 5}, "x", "mt"), "held by the session")


class HostFilter(unittest.TestCase):
    def test_only_listed_hosts_are_moved(self):
        procs = [{"pid": 1, "tag": "lt", "status": "idle", "name": "a", "session": "s1", "command": "c1", "host": 100},
                 {"pid": 2, "tag": "lt", "status": "idle", "name": "b", "session": "s2", "command": "c2", "host": 200}]
        killed = []
        with mock.patch.dict(os.environ, {"CLAUDE_MOVE_ONLY_HOSTS": "200"}), \
             mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=procs), \
             mock.patch.object(cca, "process_command", side_effect=lambda p: f"c{p}"), \
             mock.patch.object(cca.os, "kill", side_effect=lambda p, s: killed.append(p)), \
             mock.patch.object(cca, "wait_gone"), mock.patch.object(cca, "warn_stale_companions"), \
             mock.patch.object(cca, "continue_moved") as cont, \
             redirect_stdout(io.StringIO()):
            cca.move_open("idle", "cut")
        self.assertEqual(killed, [2])
        self.assertEqual([s["pid"] for s in cont.call_args.args[1]], [2])

    def test_off_never_reopens(self):
        procs = [{"pid": 3, "tag": "lt", "status": "idle", "name": "c", "session": "s3", "command": "c3", "host": 1}]
        with mock.patch.object(cca, "configured_tag", return_value="mt"), \
             mock.patch.object(cca, "load_settings", return_value={}), \
             mock.patch.object(cca, "live_sessions", return_value=procs), \
             mock.patch.object(cca, "process_command", side_effect=lambda p: f"c{p}"), \
             mock.patch.object(cca.os, "kill"), \
             mock.patch.object(cca, "wait_gone"), mock.patch.object(cca, "warn_stale_companions"), \
             mock.patch.object(cca, "continue_moved") as cont, \
             redirect_stdout(io.StringIO()):
            cca.move_open("idle", "off")
        cont.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ConfiguredTag(unittest.TestCase):
    """A setting written by an older install path of the wrapper still counts as managed
    (the move of these tools into Latidos, release 82)."""

    def settings(self, wrapper):
        return {cca.ENV_KEY: [{"name": "CLAUDE_CONFIG_DIR", "value": str(cca.ACCOUNTS["mt"]["dir"])}],
                cca.WRAPPER_KEY: wrapper}

    def test_any_copy_of_the_wrapper_counts(self):
        self.assertEqual(cca.configured_tag(self.settings(str(cca.WRAPPER))), "mt")
        tmp = tempfile.mkdtemp(prefix="cca-wrapper-")
        self.addCleanup(_shutil.rmtree, tmp, True)
        old = Path(tmp) / "claude-cursor-wrapper.sh"
        old.write_text("#!/bin/sh\nexec \"$@\"\n")
        self.assertEqual(cca.configured_tag(self.settings(str(old))), "mt")
        # Audit of 82: a copy that no longer exists would leave Cursor unable to start Claude.
        self.assertIn("not managed", cca.configured_tag(self.settings("/elsewhere/old/claude-cursor-wrapper.sh")))
        self.assertIn("not managed", cca.configured_tag(self.settings("/elsewhere/other-wrapper.sh")))
        self.assertIn("not managed", cca.configured_tag({cca.ENV_KEY: []}))
