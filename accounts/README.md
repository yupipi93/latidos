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
| `claude-account-control.py` | `state`, `activate <tag>`, `order a,b,c`: what the Latidos cards run |
| `cursor-claude-account.py` | Switches Cursor's Claude extension live (settings, no restart); `--move-open` moves open tabs and sends "continue" to the cut ones |
| `claude-cursor-wrapper.sh` | The extension's process wrapper: drops `CLAUDE_CONFIG_DIR=~/.claude` so the default account keeps its login |
| `relaunch/` | Companion Cursor extension that reopens a moved tab so it starts on the new account (`relaunch/README.md`) |
| `claude_peer.py` | Sends one message into a running session's peer socket with the right permission class |
| `t3-claude-account.py` | Switches T3 Code's Claude account (its `homePath` setting) |
| `claude-shared-pool.py` | One pool (config `pool`) for transcripts, memory and session state, symlinked into every account folder; also keeps MCP servers in sync |
| `claude-with-fallback.py` | Drop-in `claude -p` for unattended jobs: the next account in the order when one runs out |
| `claude-quota-resume.py` | Every 2 min (LaunchAgent): after a usage-limit reset, tells each cut chat to carry on |
| `claude-quota-warmup.py` | Weekday mornings (LaunchAgent): one tiny call per account so the 5-hour windows start early |
| `claude_accounts.py`, `menubar.lua` | Login expiry per account and one-click renewal in the account's Chrome profile; a Hammerspoon menu bar item |

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
2. Log every other account in once: `CLAUDE_CONFIG_DIR=~/.claude.<x> claude /login`.
3. `accounts/claude-shared-pool.py --fix` (chats and memory shared from then on).
4. `accounts/cursor-claude-account.py <tag>` once (writes Cursor's settings), and
   `accounts/relaunch/install.py` for the companion extension.
5. LaunchAgents: `accounts/claude-auto-switch.py --install`,
   `accounts/claude-quota-resume.py --install`, `accounts/claude-quota-warmup.py --install`.
6. In Latidos' `config.json`: `"account_control": ["python3", "<clone>/accounts/claude-account-control.py"]`.

A shell switch command does the same steps as `claude-account-control.py activate <tag>`;
for a terminal you can define, for each tag, a function that runs it and then exports or
unsets `CLAUDE_CONFIG_DIR` from `~/.claude-active`.

## Tests

    python3 -m unittest discover -s accounts -p "test_*.py"

Every test reads `accounts/testdata/claude-accounts.json` (three invented accounts) through
`testsupport.py`, never the user config, and points every state file at a temp folder.
