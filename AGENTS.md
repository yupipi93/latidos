# Agent instructions

- Python 3.9+, standard library only (`latidos.py` server and ingest, `cursor_source.py`).
  The page is plain HTML, CSS and JS in `web/`. Every visible string lives in the
  `en` and `es` tables of `web/i18n.js` (`t(key, vars)`, `{name}` placeholders,
  `[one, other]` plurals); static copy in `index.html` uses `data-i18n*` attributes,
  and the server sends codes (`kind`, `error_code`, `name_code`, `title_code`), not
  page text. Code, comments and docs in English.
- Run `python3 -m unittest -q test_latidos` and, when `accounts/` changed,
  `python3 -m unittest discover -s accounts -p "test_*.py"` before every commit (the first also runs
  `node --test web/format.test.js` when Node is installed).
- Work goes through `BACKLOG.md`: an item with its design first, numbered releases, a
  test for every fixed bug.
- `classify.py` (smart classification) is the one part that calls a model: it sends session
  dossiers to the user's own `claude -p` and writes only `classified.json` in the user
  config; the server just reads that file. Tests use a fake CLI, never the real one.
- Latidos only reads: Claude Code transcripts, the other agents' local files (`agents.py`,
  one reader per agent, no network; tests point them at `LATIDOS_AGENTS_HOME`), Cursor's
  files (opened read-only) and
  Cursor's usage API with the user's own session, which is never stored, logged or
  returned. Keep it bound to 127.0.0.1 with the Host check, GET and HEAD, plus the page
  writes (POST, JSON, `X-Latidos: 1`): rename, assign and, when the user configured
  `account_control`, the two calls that run that command. Latidos never changes an
  account by itself.
- No real data in the repo: tests, fixtures, docs and screenshots use synthetic values
  (`DEMO-123`, `repo-x`, invented people, example.com). Real ticket boards, project
  aliases, workspace names, paths, account tags and the launchd label live only in the
  user config (`~/.config/latidos/`; `config/*.example.json` shows every key). Tests must
  never read the user config. Run the public-readiness check
  (`public-readiness.py <repo>`) before committing when it is available.
- Before publishing, follow `PUBLISHING.md`.
- No em dash (U+2014) anywhere. Conventional Commits.
- `accounts/` (the Claude account tools) take every account, folder, e-mail and order from
  `claude-accounts.json` in the user config through `accounts/accounts_config.py`; their
  tests read only `accounts/testdata/` (`testsupport.py`). They write account state and
  settings by design (Cursor, T3 Code, Keychain logins via the CLI): keep their safety
  rules (`accounts/README.md`, module docstrings) when changing them.
