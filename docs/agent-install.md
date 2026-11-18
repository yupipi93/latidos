# Installing Latidos: steps for an agent

These steps are written for a coding agent (Claude Code, Cursor, Codex, Gemini CLI...)
asked to install Latidos on the machine it runs on. Every step can be run again safely
and ends with a check. Stop and report at the first check that fails.

## Ground rules

- No `sudo`, no package manager, no `pip install`: Latidos is Python standard library only.
- Keep it on `127.0.0.1`. Do not set `LATIDOS_HOST`, `LATIDOS_ALLOW_REMOTE` or
  `account_control` unless the user asks for it in so many words (see Safety in the README).
- Never overwrite an existing file in `~/.config/latidos/`. Copy an example only when the
  target is missing.
- Do not run `classify.py`: it sends session prompts to a model. Mention it as an option.
- Windows is not supported. Say so and stop.

## 1. Check Python

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 9), sys.version; print("python ok", sys.version.split()[0])'
```

## 2. Get the code (or update it)

```bash
if [ -d ~/latidos/.git ]; then git -C ~/latidos pull --ff-only; else git clone https://github.com/yupipi93/latidos.git ~/latidos; fi
```

Use another folder if the user prefers; the rest of the steps assume `~/latidos`.

## 3. Is it already running?

```bash
python3 ~/latidos/latidos.py status
```

`{"ok": true, ...}` means a Latidos already answers on port 7482: go to step 6.
"not answering" is expected on a first install. If port 7482 is taken by another program
(`lsof -nP -iTCP:7482 -sTCP:LISTEN` shows something that is not `latidos.py`), set
`LATIDOS_PORT` to a free port for every command below and tell the user which.

## 4. First scan

```bash
python3 ~/latidos/latidos.py ingest
```

It reads `~/.claude/projects` (about 30 s for 4 GB of transcripts) and prints how many
records it stored. Transcripts elsewhere (several Claude accounts, an archive): create
`~/.config/latidos/config.json` from `config/config.example.json` with those folders in
`roots` first, then run it again.

## 5. Keep it running

macOS (LaunchAgent, starts at login):

```bash
python3 ~/latidos/latidos.py install
```

Linux (systemd user unit):

```bash
mkdir -p ~/.config/systemd/user
[ -f ~/.config/systemd/user/latidos.service ] || cat > ~/.config/systemd/user/latidos.service <<'EOF'
[Unit]
Description=Latidos, local Claude Code and Cursor usage panel

[Service]
ExecStart=/usr/bin/env python3 %h/latidos/latidos.py serve
Restart=on-failure

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now latidos.service
```

## 6. Verify

```bash
python3 ~/latidos/latidos.py status
```

Expect `"ok": true` and `"error": null` in `ingest`. Then report to the user:

- the page: <http://127.0.0.1:7482>
- how many calls the first scan stored (step 4)
- that Cursor is read automatically when it is installed (`LATIDOS_CURSOR_API=0` turns its
  usage API off)
- the optional pieces they can ask for: account cards, ticket boards, smart classification

## Uninstall

```bash
python3 ~/latidos/latidos.py uninstall                                   # macOS
systemctl --user disable --now latidos.service && rm ~/.config/systemd/user/latidos.service   # Linux
```

The history stays in `~/.local/state/latidos/` and the settings in `~/.config/latidos/`;
delete them only if the user asks.
