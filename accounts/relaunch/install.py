#!/usr/bin/env python3
"""Package the Claude tab relaunch extension as a .vsix and install it in Cursor.

    accounts/relaunch/install.py            # build + install
    accounts/relaunch/install.py --build    # only write the .vsix
    accounts/relaunch/install.py --check    # is it listening in Cursor?

An install takes effect in a Cursor window only after Developer: Reload Window there (do it
when no Claude tab is working: the reload ends every Claude process). --check lists the
windows whose companion wrote a heartbeat and the version each one runs; it exits 2 when
a window still runs an older one. Stdlib only (no vsce, no npm).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import accounts_config  # noqa: E402  (the extension id comes from the accounts config)
# "<publisher>.<name>": `companion_id` in the accounts config (default latidos.claude-relaunch),
# so an install made under another id keeps its id and cursor-claude-account.py finds it.
COMPANION_ID = accounts_config.CONFIG.companion_id
_pub, _, _name = COMPANION_ID.partition(".")
if not (_pub and _name and all(c.isalnum() or c in "-_" for c in _pub + _name)):
    sys.exit(f"❌ companion_id {COMPANION_ID!r} must look like publisher.name (letters, digits, - and _)")
FILES = ["package.json", "extension.js", "README.md"]
STATE = Path(os.environ.get("CLAUDE_RELAUNCH_DIR", Path.home() / ".local" / "state" / "cursor-claude-relaunch"))
CLIS = [Path.home() / "Applications/Cursor.app/Contents/Resources/app/bin/cursor",
        Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor")]

CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension=".json" ContentType="application/json"/>
<Default Extension=".js" ContentType="application/javascript"/>
<Default Extension=".md" ContentType="text/markdown"/>
<Default Extension=".vsixmanifest" ContentType="text/xml"/>
</Types>
"""

MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
  <Metadata>
    <Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
    <DisplayName>{display}</DisplayName>
    <Description xml:space="preserve">{description}</Description>
    <Categories>Other</Categories>
    <Properties>
      <Property Id="Microsoft.VisualStudio.Code.Engine" Value="{engine}"/>
    </Properties>
  </Metadata>
  <Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
  <Dependencies/>
  <Assets>
    <Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/>
  </Assets>
</PackageManifest>
"""


def build() -> Path:
    pkg = json.loads((HERE / "package.json").read_text())
    pkg["publisher"], _, pkg["name"] = COMPANION_ID.partition(".")
    out = Path(tempfile.gettempdir()) / f"{pkg['publisher']}.{pkg['name']}-{pkg['version']}.vsix"
    manifest = MANIFEST.format(name=pkg["name"], version=pkg["version"], publisher=pkg["publisher"],
                               display=pkg["displayName"], description=pkg["description"],
                               engine=pkg["engines"]["vscode"])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("extension.vsixmanifest", manifest)
        for name in FILES:
            if name == "package.json":
                z.writestr("extension/package.json", json.dumps(pkg, indent="\t", ensure_ascii=False) + "\n")
            else:
                z.write(HERE / name, f"extension/{name}")
    return out


def cli() -> Path:
    for c in CLIS:
        if c.exists():
            return c
    sys.exit("❌ Cursor not found in ~/Applications or /Applications")


def installed_version() -> str | None:
    """The companion version Cursor has installed (what a reload would load)."""
    try:
        exts = json.loads((Path.home() / ".cursor" / "extensions" / "extensions.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for e in exts if isinstance(exts, list) else []:
        if isinstance(e, dict) and e.get("identifier", {}).get("id") == COMPANION_ID:
            return e.get("version")
    return None


def check(wait: float = 0) -> int:
    deadline = time.monotonic() + wait
    while True:
        live = []
        for f in (STATE / "windows").glob("*.json"):
            try:
                os.kill(int(f.stem), 0)
                live.append(json.loads(f.read_text()))
            except (ValueError, ProcessLookupError, OSError, json.JSONDecodeError):
                continue
        if live or time.monotonic() >= deadline:
            break
        time.sleep(1)
    if not live:
        print("❌ No Cursor window has the companion running (open Cursor, or reload a window)")
        return 1
    want = installed_version() or json.loads((HERE / "package.json").read_text())["version"]
    stale = 0
    for w in live:
        folders = ", ".join(Path(p).name for p in w.get("folders", [])) or "(no folder)"
        runs = w.get("companion") or "0.2.0 or older"
        ok = runs == want
        stale += not ok
        print(f"{'✅' if ok else '⚠️ '} window pid {w['pid']} ({folders}): {len(w.get('claude_tabs', []))} Claude tab(s), "
              f"Claude extension {w.get('claude_extension')}, companion {runs}"
              + ("" if ok else f" (installed {want}: run Developer: Reload Window in it)"))
    return 2 if stale else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--build", action="store_true", help="only build the .vsix")
    ap.add_argument("--check", action="store_true", help="list the Cursor windows where it is listening")
    args = ap.parse_args()
    if args.check:
        return check()
    vsix = build()
    print(f"📦 {vsix}")
    if args.build:
        return 0
    res = subprocess.run([str(cli()), "--install-extension", str(vsix), "--force"], capture_output=True, text=True)
    print((res.stdout + res.stderr).strip())
    if res.returncode != 0:
        return res.returncode
    code = check(wait=20)
    if code == 2:
        print("ℹ️  Installed. It takes effect in each window above after Developer: Reload Window there.")
        return 0
    return code


if __name__ == "__main__":
    sys.exit(main())
