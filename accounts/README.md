# Claude accounts

Tools for running Claude Code under several Claude.ai subscriptions on one Mac and moving
between them without losing a chat: a smart automatic switch, the switch steps for Cursor
and T3 Code, one shared pool of chats and memory, a headless fallback, the wake-up of chats
cut by a usage limit, an early warm-up of the 5-hour windows and the renewal of logins.
Latidos runs `claude-account-control.py` from its account cards (config `account_control`).

Everything personal (which accounts, their folders, e-mails, Chrome profiles, the order,
the notifier) lives in the user config, never in this folder: `claude-accounts.json` in
the Latidos config dir (see `config/claude-accounts.example.json`; `LATIDOS_CLAUDE_ACCOUNTS`
names another file). Standard library only, Python 3.9+, macOS.

## The layout these tools assume

On macOS Claude Code keeps each OAuth login in the Keychain under a name derived from
`CLAUDE_CONFIG_DIR`: no suffix when it is unset, else `-` plus the first 8 hex digits of
the SHA-256 of the folder. So one account is the **default**: it runs with the variable
unset and lives in `~/.claude` (`config_dir: ""` in the config); every other account has
its own folder (`~/.claude.<x>`). Never point the default account at a folder: its login
would be gone.

`~/.claude-active` (config `active_file`) holds the tag of the account new chats start on.
Your shell reads it to set `CLAUDE_CONFIG_DIR`; these tools write it last when they switch.

## The tools

| File | What it does |
|---|---|
| `accounts_config.py` | Reads the accounts config; every tool imports it |
| `claude-auto-switch.py` | Every minute (LaunchAgent): moves the default to another account when it runs out, and rebalances by the smart hierarchy |
| `claude-account-control.py` | `state`, `activate <tag>`, `order a,b,c`: what the Latidos cards run; `open-web [tag]` opens claude.ai in the account's Chrome profile |
| `cursor-claude-account.py` | Switches Cursor's Claude extension live (settings, no restart), and VS Code's when it has the extension (new tabs only); `--move-open` moves open Cursor tabs (only processes under `~/.cursor/extensions`) and sends "continue" to the cut ones |
| `claude-cursor-wrapper.sh` | The extension's process wrapper: drops `CLAUDE_CONFIG_DIR=~/.claude` so the default account keeps its login |
| `relaunch/` | Companion Cursor extension that reopens a moved tab so it starts on the new account (`relaunch/README.md`) |
| `claude_peer.py` | Sends one message into a running session's peer socket with the right permission class |
| `t3-claude-account.py` | Switches T3 Code's Claude account (its `homePath` setting) |
| `claude-shared-pool.py` | One pool (config `pool`) for transcripts, memory and session state, symlinked into every account folder; also keeps MCP servers in sync |
| `claude-with-fallback.py` | Drop-in `claude -p` for unattended jobs: starts on the active account, then the effective order, each account once; streams `stream-json` |
| `claude-quota-resume.py` | Every 2 min (LaunchAgent): after a usage-limit reset, tells each cut chat to carry on; names the resume command for cut terminal chats |
| `claude-quota-warmup.py` | Weekday mornings (LaunchAgent): one tiny call per account so the 5-hour windows start early |
| `claude_accounts.py`, `menubar.lua` | Login expiry per account and one-click renewal in the account's Chrome profile; a Hammerspoon menu bar item |

## What follows a switch

| Surface | Follows the active account | How |
|---|---|---|
| Terminal CLI, new commands | Yes, with a shell function | The `claude` function below reads `~/.claude-active` on every call |
| Terminal CLI, a running chat | No | Its process keeps its account; after a limit, `claude-quota-resume.py` names `cd <cwd> && <resume_command>` to continue it on the active account |
| Cursor's Claude extension | Yes, live | Its settings; `--move-open` moves open tabs, the companion reopens them |
| VS Code's Claude extension | New tabs only | Its user settings are written when the extension is installed; open tabs are never ended (no companion) |
| T3 Code | Yes | Its `homePath` setting |
| Headless jobs (`claude -p`) | Yes, through the fallback | Point the job at `claude-with-fallback.py` |
| claude.ai in the browser | On request | `claude-account-control.py open-web` opens it in the account's Chrome profile |
| Claude Desktop | No, on purpose | It has its own login; nothing here touches it |
| Third-party clients (pi, OpenCode and the like) | No, on purpose | The provider's terms limit subscription logins to its own clients, and sharing the Keychain token would race its refresh |

### Headless fallback order

`claude-with-fallback.py` tries the account in `~/.claude-active` first, then the effective
priority (the order file the Latidos cards write, else the config's `order`), each account
at most once per call; one that hit a limit in the last 30 minutes goes last. A caller can
pin: `CLAUDE_FALLBACK_ORDER=a,b` is used as given (no active-first) and
`CLAUDE_FALLBACK_ACCOUNT=<tag>` goes first whatever the active one is. With
`--output-format stream-json` the events are streamed as they come and a retry's stream
follows the failed one, so a runner that keeps the last `session_id` and the last `result`
sees the run that answered; before a retry's stream it writes one
`{"type": "system", "subtype": "account_fallback", "from": .., "to": ..}` event, so a reader
can drop what the failed attempt wrote. Every other flag (`--resume`, `--model`,
`--verbose`...) and stdin pass through; the exit code is the last attempt's.

What counts as running out: only the failure itself (stderr, the final result event with
its `api_error_status`, top-level error events, the CLI's own error codes), never tool
results or the model's text. A usage limit or an expired login benches the account for 30
minutes; a transient 429 moves on without benching, but three in a row on one account bench
it for 5 minutes; an overload (529) is returned as is. A state file that cannot be locked
within 2 s or written is skipped with a log line: the answer always goes out.

A run that already did work (an assistant turn or a tool result) and then hits a limit is
never started again: the same session continues on the next account with
`--resume <session_id>` and a short "continue where you left off" prompt (transcripts live
in the shared pool), so side effects (commits, pushes, posts) never repeat; the limit
result goes back only when every account is spent. This needs the shared pool (every
account's `projects/` is the same folder, `claude-shared-pool.py --fix`), a session id in
the output, session persistence, text input, and arguments the wrapper is certain about
(known flags, variadic flags such as `--disallowedTools` only with a positional prompt, no
optional-value flag such as `-d` or `-w`, one prompt); otherwise the limit result goes back
and the original prompt is never sent again. A resume that fails for another reason before
doing any work hands back the earlier limit result, so a ticket desk still sees quota; one
that did work and then failed is passed through with its own output and exit code. Plain text output (no `--output-format`) shows no turns, so there a
failed run is started again on the next account: agent runners should use `stream-json`
or `json`. The real CLI never outlives the wrapper: signals are passed on, and a watcher
process ends it when a caller's timeout kills the wrapper.

## Smart hierarchy

Each account with fresh quota numbers (from the `quota_api` / `quota_cache` source) gets a
score:

    weekly room / days until its weekly reset (at least 6 h) x min(1, 5-hour room / 40) x priority weight

The priority (config `order`, or the order file `claude-order.json` the Latidos cards
write, or `CLAUDE_AUTO_SWITCH_ORDER`) only adds 15 % per place above the last
(`CLAUDE_AUTO_SWITCH_PRIORITY_BIAS`). With the same reset, more room left wins whatever the
priority; room that expires soon is used first. When the default is spent it moves to the
best score among accounts with 10 % of the 5-hour window and 5 % of the week left; while it
still has room it rebalances to an account scoring 1.5 times more (15 % of its week, 20 %
of its 5-hour window, at most every 30 minutes). Paid extra usage is a last resort and is
left as soon as another account has free room. A manual switch (shell, Latidos) is
respected; the details, thresholds and every rule are in the module docstring and
`claude-auto-switch.py --status`.

## Setup

1. Write `claude-accounts.json` (copy the example). One account with `config_dir: ""`.
   `notify` is optional: a notifier's path or command (with its fixed arguments), called
   as `<notify...> <level> <title> <body>`. `timezone` is optional (default: the machine's).
   Without a config, or with a broken one, the tools do nothing harmful: the auto switch
   says so and exits, the fallback runs the plain CLI.
2. Log every other account in once: `CLAUDE_CONFIG_DIR=~/.claude.<x> command claude /login`
   (`command` skips a shell function named `claude`, such as the one below).
3. `accounts/claude-shared-pool.py --fix` (chats and memory shared from then on).
4. `accounts/cursor-claude-account.py <tag>` once (writes Cursor's settings), and
   `accounts/relaunch/install.py` for the companion extension.
5. LaunchAgents: `accounts/claude-auto-switch.py --install`,
   `accounts/claude-quota-resume.py --install`, `accounts/claude-quota-warmup.py --install`.
6. In Latidos' `config.json`: `"account_control": ["python3", "<clone>/accounts/claude-account-control.py"]`.

A shell switch command does the same steps as `claude-account-control.py activate <tag>`;
for a terminal you can define, for each tag, a function that runs it and then exports or
unsets `CLAUDE_CONFIG_DIR` from `~/.claude-active`.

So that a shell opened before a switch (by hand or automatic) starts new chats on the new
account, wrap `claude` in a function that reads the active file on each call (zsh; put
your own tags and folders in the `case`):

    _CC_SHELL_DIR="$CLAUDE_CONFIG_DIR"   # what this shell set at start (empty: unset)
    claude() {
        local tag dir a
        # Untouched: a pinned shell, an explicit `CLAUDE_CONFIG_DIR=... claude` (it differs
        # from what the shell set), and any login or logout, which must reach the account
        # the caller named and never overwrite the active account's Keychain login.
        if [ -n "$CLAUDE_ACCOUNT_PINNED" ] || [ ! -r ~/.claude-active ] \
           || [ "${CLAUDE_CONFIG_DIR-}" != "${_CC_SHELL_DIR-}" ]; then
            command claude "$@"; return
        fi
        for a in "$@"; do
            case "$a" in /login|/logout|login|logout) command claude "$@"; return ;; esac
        done
        tag="$(tr -d '[:space:]' < ~/.claude-active)"
        case "$tag" in
            work) (unset CLAUDE_CONFIG_DIR; command claude "$@") ;;   # the default account
            home) CLAUDE_CONFIG_DIR="$HOME/.claude.home" command claude "$@" ;;
            *)    command claude "$@" ;;
        esac
    }

A shell switch function that exports a new `CLAUDE_CONFIG_DIR` should also update
`_CC_SHELL_DIR`. Helpers that pin an account call `command claude` themselves (for example
`claude-home-one() { CLAUDE_CONFIG_DIR="$HOME/.claude.home" command claude "$@"; }`), or
export `CLAUDE_ACCOUNT_PINNED=1` in a shell that should stay put. `resume_command` in the
accounts config can name such a helper: `"claude-{tag}-one --resume {session}"`.

## Tests

    python3 -m unittest discover -s accounts -p "test_*.py"

Every test reads `accounts/testdata/claude-accounts.json` (three invented accounts) through
`testsupport.py`, never the user config, and points every state file at a temp folder. The
fallback's end-to-end tests run it with HOME in a temp folder and a fake `claude`.
