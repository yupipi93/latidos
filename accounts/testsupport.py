"""Imported first by every test here: the tools read a synthetic accounts config
(testdata/claude-accounts.json, three invented accounts), never the user's."""
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["LATIDOS_CLAUDE_ACCOUNTS"] = str(HERE / "testdata" / "claude-accounts.json")
os.environ.pop("CLAUDE_AUTO_SWITCH_ORDER", None)
os.environ.pop("CLAUDE_FALLBACK_ORDER", None)
os.environ.pop("CLAUDE_FALLBACK_ACCOUNT", None)
os.environ.pop("CLAUDE_ACCOUNTS_ORDER_FILE", None)
