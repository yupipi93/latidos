# Claude tab relaunch (Cursor companion extension)

After an account switch (a shell switch command or the Latidos control), every open Claude tab in Cursor moves to the
new account and the ones whose work was cut carry on by themselves. Nobody has to type
"continúa" tab by tab.

## How a switch goes now

A switch (a shell switch command, the Latidos control or the auto switch) ends by running
`accounts/cursor-claude-account.py --move-open`:

1. Lists the tabs whose Claude process still runs on another account. When some are busy
   or waiting on a prompt or a question, it asks: only the idle ones (default) or all.
2. Ends those processes (SIGTERM to the exact PIDs, checked first).
3. Asks this extension, in the Cursor window that holds each tab, to **reopen the tab**.
   Reopening makes the Claude extension load the session, and loading starts a new process
   with the current `claudeCode.environmentVariables`, that is, the new account. Without
   this step a tab stays without a process until something is typed into it.
4. Waits for the new process, then sends **"continúa"** through its peer socket
   (`messagingSocketPath` in `<config dir>/sessions/<pid>.json`), only to the sessions whose
   work was cut (an API error counts unless it is a usage-policy refusal; the unattended
   scopes `safe` / `safe-cut` / `safe-quiet` of the automatic switch also skip errors over 8 h old): busy,
   or waiting on a permission prompt or a question, when they were ended
   (status read again right before), transcript
   ending in an API error (the usage limit, an overload), a last input (a prompt, a task
   notification or a peer message, not an interruption or a local command) still
   unanswered, a turn cut mid-way, input queued after its last answer, or background
   work of the ended process never reported finished: asynchronous subagents, background
   commands (also those moved there by their timeout or by hand with Ctrl+B), monitors not
   expired yet, workflows, a ScheduleWakeup due after the switch (they all die with it),
   subagents that reported a provisional "completed" (until their final notice or their
   "[Subagent hand-back]" report, unless resumed after it), and
   subagents (in practice the only work whose failure names the limit; text inside quotes
   and usage-policy refusals do not count) that "failed" on the usage limit unless the
   session closed a normal turn after that; they are named in the continue and, being
   dead, do not hold back a tab stopped on the limit. Local slash commands (`/usage`, `/context`...) are not inputs, and a `/usage`
   typed after the limit error does not hide that error, nor do the notices of subagents that
   failed on the same limit and arrive after it (auto switch release 42).
   Only what belongs to the ended process counts (its `startedAt`), and a TaskStop ends a
   task; without a known start nothing is inferred. A session that had finished
   and waits for the user gets nothing: a "continúa" there would make it improvise.

   The message says the conversation moved to account `<tag>`, that the process was closed
   so its subagents and background tasks stopped too, asks it to check which did not finish
   and resume or relaunch them before carrying on, names the ones its transcript shows
   unfinished, tells it that if it had in fact finished and was waiting for the user it must
   start nothing and only say it is still waiting (a guard against a wrong guess), and says
   it is an automatic notice, not a new approval. The cut tabs are handled first, so
   a tab whose new process never shows up (a 20 s wait) does not hold them back.

   The message is sent by `scripts/claude_peer.py` as the CLI's own `SendMessage` would:
   inside a `<cross-session-message from-mode="bypass|prompting">` envelope with
   `origin.fromMode`. Claude Code holds any peer message whose attested permission class
   does not match the session's (`bypassPermissions` needs `bypass`; `auto`, `default`,
   `acceptEdits` and `plan` need `prompting`). A held message waits for an approval a Cursor tab
   never shows; it is released only if the session's mode or settings later come to match
   it, else it expires. On 2026-11-12 every moved tab ran in bypass, the old bare line was
   held (`cause=no-mode-asserted` in `Claude VSCode.log`) and nothing continued. The class
   comes from the mode the transcript recorded on the last turn since the process started
   (every user entry carries `permissionMode`), else the process's `--permission-mode`, downgraded to `prompting` when
   the account cannot run in bypass (its server-managed settings are empty and the
   machine's managed-settings.json disables bypass, so such a tab launched in bypass runs
   in `auto`). The
   live mode is not visible from outside (a runtime switch writes nothing until the next
   turn), so a mode switched after the last turn can still be wrong: then the tab is
   reported as not taken and nothing is re-sent. Nothing is sent before the new process
   finished starting (its record shows a `status`): a message that arrives earlier is held
   as `mode-unknown` and released later by itself, after the script already said "type it". One message per tab, never a retry with the other class: the held one
   could be released later and arrive twice. The script checks the tab took it (it went
   busy from idle, or its transcript queued the text) and otherwise says to type it.

```
🔄 Reopening 2 tab(s) on na...
▶️  SLA reporter: on na, continuing (had stopped on an error)
✅ DNS cleanup: on na, waiting for you (its turn had finished)
```

The CLI shows the message as coming from another Claude session ("not typed by your
user"), so it is never the user's approval: every gate the chat follows stays in force. The
default text says so too (`CLAUDE_MOVE_CONTINUE_TEXT` overrides it, `{tag}` is the account).

`--continue none` (or `CLAUDE_MOVE_CONTINUE=none`) reopens without sending anything (a tab
whose work was cut is then reported so you type "continúa" in it);
`--continue off` is the old behaviour (end the processes, type in each tab).

## Why an extension

Checked in the Claude extension 2.1.322 (`extension.js`, `webview/index.js`):

- A tab whose process ended only calls `launchClaude()` on a send. The `cursor://anthropic.claude-code/open?session=&prompt=`
  URI only reveals an open tab ("Session is already open. Your prompt was not applied"),
  and `prompt` only fills the input box; `restartSessionsWhenIdle` only closes channels.
- "Developer: Reload Webviews" would relaunch every tab, but it also cuts the busy ones that
  are already on the right account (a running `terraform apply` included). Reopening one
  tab at a time touches only the tabs being moved.
- Nothing outside Cursor can run a command in it, so a tiny extension listens for requests.

## Protocol

Files under `~/.local/state/cursor-claude-relaunch/` (`CLAUDE_RELAUNCH_DIR` overrides):

| Path | Written by | Holds |
|---|---|---|
| `windows/<pid>.json` | extension, every 30 s | heartbeat; `<pid>` is the window's extension host, the parent of every Claude process its tabs run |
| `requests/<pid>/<id>.json` | the script | `expect_config_dir` and `ops: [{session, titles}]` |
| `responses/<id>.json` | extension | `results: [{session, result: reopened / ambiguous / config-not-applied / reopen-pending / error}]`; a request read after its deadline gets `expired` |

Finding the tab of a session: `claude-vscode.editor.open(<session>)` on an open session only
reveals its tab, so the Claude tab that becomes active is the one. When the tab was already
active nothing changes; then its label must match one of the session's titles (`/rename`
title, AI title, first prompt, process name; the label is the title cut to 24 chars + "…").
Otherwise the result is `ambiguous` and that tab is left to be typed in.

**Tab order.** A reopened tab opens next to the group's active editor, not where it was.
The extension notes the tab's index before closing it and, when it lands elsewhere, focuses
its group and moves it back (`moveActiveEditor` to that position). At the end each group
shows again the tab it showed before, and the focus goes back to the group that had it (or
to Cursor's terminal, when the switch was run from it and there is one window). A tab that
could not be put back is reported with `↔️`.

The extension also waits (up to 20 s) until its window's settings show the new
`CLAUDE_CONFIG_DIR` before reopening: the file watcher can take a few seconds, and reopening
earlier would relaunch on the old account.

## Install and check

```bash
accounts/relaunch/install.py           # build the .vsix, install, wait for a heartbeat
accounts/relaunch/install.py --check   # windows where it listens, and the companion version each runs
```

**An install takes effect only after Developer: Reload Window** in each Cursor window. The
heartbeat carries the version the window runs (`companion`, since 0.3.0), compared with the
version Cursor has installed (`~/.cursor/extensions/extensions.json`); `--check` exits 2 and
every switch (`--move-open`) starts with a warning when a window runs an older one. On 2026-11-12 the window
still ran 0.1.0 (loaded before 0.2.0 was installed), so the tab order was not restored.
Reload when no tab is working: the reload ends every Claude process.

Logs: Output panel, channel "Claude tab relaunch". Uninstall: `cursor --uninstall-extension <companion_id>` (default `latidos.claude-relaunch`).

## Tests

```bash
python3 -m unittest accounts/test_cursor_claude_relaunch.py accounts/test_claude_peer.py -v
```

Unit tests cover the script side (cut detection, titles, the request exchange, the continue
decision and its order, the host filter) and `claude_peer.py` (mode to class, the envelope,
the mode read from the transcript, delivery against a fake session that only takes its own
class and releases a held one on a mode change, one send and no retry, the quota-resume
tick with its held, socket-error and `--now` paths). The peer delivery was also checked against real Claude
processes, 2.1.318 and 2.1.322, started like the extension starts a tab (stream-json,
`--resume`, nothing on stdin): the old bare line was held in `bypassPermissions`, the
envelope was taken in `bypassPermissions`, `auto`, `default` and `acceptEdits`, and the
wrong class was held (`mode-mismatch`). The extension was checked end to end on a
separate Cursor instance (own `--user-data-dir` under a short path, `--extensions-dir` with
only the Claude extension, this folder as `--extensionDevelopmentPath`), with
`CURSOR_USER_SETTINGS`, `CLAUDE_RELAUNCH_DIR` and `CLAUDE_MOVE_ONLY_HOSTS=<its host pid>` so
the real windows were never touched: a busy tab moved mt -> na and finished its turn, two
idle tabs with the same label moved na -> mt without a message, a tab stopped on the usage
limit continued on the other account; four tabs (stale ones at index 0, 2 and 3 of the
group, the one at 2 visible) kept their order and the visible tab after moving mt -> na.

## Limits

- Only Cursor tabs. A terminal `claude` keeps its account until it is restarted
  (`claude-<tag> --continue`); T3 Code follows its own settings (`t3-claude-account.py`).
- A tab that already had no process before the switch is not found (nothing to list it by).
- Remote windows (SSH) are not handled.
