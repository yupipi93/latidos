#!/usr/bin/env python3
"""claude-shared-pool.py - one pool of chats, memory and session state for every Claude account.

Why this exists: the owner runs Claude Code under several Claude.ai subscriptions and
switches to another one when a quota runs out. Each account needs its own config dir,
because on macOS the OAuth login lives in the Keychain under a name derived from that dir:

    default  ~/.claude     CLAUDE_CONFIG_DIR unset   Keychain "Claude Code-credentials"
    other    ~/.claude.x   CLAUDE_CONFIG_DIR=<dir>   Keychain "Claude Code-credentials-<sha256(dir)[:8]>"

The accounts come from the accounts config (accounts_config.py); the pool folder is its
`pool` key (default ~/.claude-shared).

But every config dir also carries its own projects/ (chat transcripts, memory, subagent
logs), so a switch used to hide every chat and every memory of the other account, in
the terminal and in the Cursor extension alike.

The fix is not a sync. Everything that makes up a conversation lives ONCE in
~/.claude-shared and each account dir holds a symlink to it. A single copy is
bidirectional and instant by construction, and it cannot conflict.

Shared (SHARED_DIRS / SHARED_FILES): transcripts + memory, rewind checkpoints,
per-session env, todos, tasks, plans, prompt history and its paste cache, user-level
commands and agents.

Per account, never shared: .credentials.json / the Keychain item, .claude.json (OAuth
identity, per-project trust, user MCP servers), settings.json (model and effort are
tuned per subscription), skills/ and plugins/ (synced from each claude.ai org), and the
live-process state (sessions/, ide/, shell-snapshots/, cache/).

    accounts/claude-shared-pool.py            # status; exit 1 if any entry is not linked
    accounts/claude-shared-pool.py --fix      # move real entries into the pool, merge, link
    accounts/claude-shared-pool.py --fix --quiet   # what the zsh switcher runs on every switch
    accounts/claude-shared-pool.py --mcp-remove X  # drop MCP server X from every account

--fix also syncs the MCP servers of every account's .claude.json (see sync_mcp).

Merging is file by file and never destructive: a file that exists on both sides with
different content is merged when it is a .jsonl (union of lines, ordered by timestamp)
and the losing copy is kept under ~/.claude-shared/.conflicts/<stamp>/. Anything else
that differs is left in the pool as it was and the incoming copy goes to .conflicts.

Stdlib only.
"""

import argparse
import filecmp
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
sys.path.insert(0, str(Path(__file__).resolve().parent))
import accounts_config  # noqa: E402  (sibling module: the accounts from the user config)
CFG = accounts_config.CONFIG
POOL = CFG.pool

# Order matters: the first account that still owns a real entry seeds the pool by a
# plain rename (atomic, no copy); later ones are merged into it. The default account first.
ACCOUNTS = [(a.tag, a.dir) for a in sorted(CFG.accounts, key=lambda a: not a.default)]

SHARED_DIRS = [
    "projects",
    "file-history",
    "session-env",
    "todos",
    "tasks",
    "plans",
    "paste-cache",
    "commands",
    "agents",
]
SHARED_FILES = ["history.jsonl"]

STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")


class Report:
    def __init__(self, quiet):
        self.quiet = quiet
        self.conflicts = []

    def say(self, msg):
        if not self.quiet:
            print(msg, flush=True)

    def warn(self, msg):
        print(msg, file=sys.stderr)


def points_to_pool(path, name):
    return path.is_symlink() and os.readlink(path) == str(POOL / name)


def same_target(a, b):
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _ts_key(line, fallback):
    try:
        ts = json.loads(line).get("timestamp")
    except (ValueError, AttributeError):
        return fallback
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
        except ValueError:
            return fallback
    return fallback


def _keyed(lines):
    out, last = [], 0.0
    for line in lines:
        last = _ts_key(line, last)
        out.append((last, line))
    return out


def merge_jsonl(src, dst):
    """Union of the lines of two .jsonl files, stable-sorted by their timestamp field."""
    dst_lines = dst.read_text(errors="replace").splitlines()
    src_lines = src.read_text(errors="replace").splitlines()
    seen = set(dst_lines)
    merged = _keyed(dst_lines) + _keyed([l for l in src_lines if l not in seen and l.strip()])
    merged.sort(key=lambda kv: kv[0])
    tmp = dst.with_name(dst.name + f".merge-{STAMP}")
    tmp.write_text("".join(line + "\n" for _, line in merged))
    shutil.copystat(dst, tmp)
    os.replace(tmp, dst)


def quarantine(src, rel, tag, rep):
    dest = POOL / ".conflicts" / STAMP / tag / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dest)
    rep.conflicts.append(dest)


def merge_tree(src, dst, rel, tag, rep):
    """Move everything under src into dst. Leaves src empty unless something failed."""
    if src.is_dir() and not src.is_symlink():
        if not dst.is_dir() or dst.is_symlink():
            quarantine(src, rel, tag, rep)
            return
        for child in sorted(src.iterdir()):
            target = dst / child.name
            if not os.path.lexists(target):
                os.rename(child, target)
            else:
                merge_tree(child, target, rel / child.name, tag, rep)
        try:
            src.rmdir()
        except OSError:
            pass
        return

    if dst.is_dir() and not dst.is_symlink():
        quarantine(src, rel, tag, rep)
        return
    if src.is_symlink() or dst.is_symlink():
        if same_target(src, dst):
            src.unlink()
        else:
            quarantine(src, rel, tag, rep)
        return
    if filecmp.cmp(src, dst, shallow=False):
        src.unlink()
        return
    if rel.suffix == ".jsonl":  # rel, not src: the set-aside copy is renamed .<name>.pre-share-*
        merge_jsonl(src, dst)
        quarantine(src, rel, tag, rep)  # kept for the record; its lines are in dst now
        return
    quarantine(src, rel, tag, rep)


def link_entry(tag, acct, name, is_dir, rep):
    path = acct / name
    pool_path = POOL / name

    if points_to_pool(path, name):
        return False

    if path.is_symlink() and not os.path.exists(path):
        path.unlink()  # dangling: nothing to keep
    elif path.is_symlink():
        # e.g. commands -> ~/code/my-agent-kit/commands: the link itself moves into the pool.
        if not os.path.lexists(pool_path):
            os.rename(path, pool_path)
        elif same_target(path, pool_path):
            path.unlink()
        else:
            rep.warn(f"⚠️  {tag}: {path} -> {os.readlink(path)} differs from the pool; left alone")
            return False
    elif path.exists():
        if not os.path.lexists(pool_path):
            os.rename(path, pool_path)
        else:
            # Swap first, merge after, so a running session appending by path lands in
            # the pool instead of recreating a real entry behind our back.
            aside = acct / f".{name}.pre-share-{STAMP}"
            os.rename(path, aside)
            os.symlink(pool_path, path)
            merge_tree(aside, pool_path, Path(name), tag, rep)
            if os.path.lexists(aside):
                rep.warn(f"⚠️  {tag}: could not fully merge {aside}; left in place")
            rep.say(f"🔀 {tag}: merged {name} into the pool")
            return True

    if not os.path.lexists(pool_path):
        if is_dir:
            pool_path.mkdir(parents=True)
        else:
            pool_path.touch(mode=0o600)
    os.symlink(pool_path, path)
    rep.say(f"🔗 {tag}: {path} -> {pool_path}")
    return True


def remove_stray_account_links(acct, rep):
    """`ln -sfn ~/.claude.mt ~/.claude` against a real dir drops a link INSIDE it."""
    for tag, other in ACCOUNTS:
        stray = acct / other.name
        if stray.is_symlink() and same_target(stray, other):
            stray.unlink()
            rep.say(f"🧹 removed stray link {stray}")


def fix(rep):
    POOL.mkdir(mode=0o700, exist_ok=True)
    changed = False
    for tag, acct in ACCOUNTS:
        if not acct.is_dir() or acct.is_symlink():
            rep.warn(f"⚠️  {tag}: {acct} is not a real directory; skipped")
            continue
        remove_stray_account_links(acct, rep)
        for name in SHARED_DIRS:
            changed |= link_entry(tag, acct, name, True, rep)
        for name in SHARED_FILES:
            changed |= link_entry(tag, acct, name, False, rep)
    sync_mcp(rep)
    for path in rep.conflicts:
        rep.warn(f"⚠️  conflict kept at {path}")
    return changed


# --- MCP servers ------------------------------------------------------------------------
# They live in each account's .claude.json, next to the OAuth identity, so that file cannot
# be symlinked. Only the MCP keys are synced, 3-way against the last synced snapshot:
#   user scope      .mcpServers.<name>
#   local scope     .projects.<path>.mcpServers.<name>
#   .mcp.json picks .projects.<path>.enabledMcpjsonServers / disabledMcpjsonServers
# A server added or edited in one account reaches the other. Removal is never inferred
# (a Claude process rewriting a stale .claude.json would look exactly like one): use
# --mcp-remove <name>. OAuth tokens of an MCP server stay per account, in its Keychain
# item, so an OAuth server asks for /mcp login once under each account.

MCP_STATE = POOL / "mcp-state.json"
MCP_LISTS = ("enabledMcpjsonServers", "disabledMcpjsonServers")


def global_config(acct):
    return HOME / ".claude.json" if acct == HOME / ".claude" else acct / ".claude.json"


def mcp_flatten(cfg):
    flat = {}
    for name, spec in (cfg.get("mcpServers") or {}).items():
        flat[json.dumps(["user", name])] = spec
    for path, proj in (cfg.get("projects") or {}).items():
        for name, spec in (proj.get("mcpServers") or {}).items():
            flat[json.dumps(["project", path, name])] = spec
        for field in MCP_LISTS:
            if proj.get(field):
                flat[json.dumps(["list", path, field])] = proj[field]
    return flat


def mcp_apply(cfg, key, value):
    kind, *rest = json.loads(key)
    if kind == "user":
        cfg.setdefault("mcpServers", {})[rest[0]] = value
    elif kind == "project":
        proj = cfg.setdefault("projects", {}).setdefault(rest[0], {})
        proj.setdefault("mcpServers", {})[rest[1]] = value
    else:
        cfg.setdefault("projects", {}).setdefault(rest[0], {})[rest[1]] = value


def mcp_drop(cfg, name):
    hits = 0
    if name in (cfg.get("mcpServers") or {}):
        del cfg["mcpServers"][name]
        hits += 1
    for proj in (cfg.get("projects") or {}).values():
        if name in (proj.get("mcpServers") or {}):
            del proj["mcpServers"][name]
            hits += 1
    return hits


def write_json(path, data):
    tmp = path.with_name(path.name + f".mcp-sync-{STAMP}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def load_accounts_config(rep):
    out = []
    for tag, acct in ACCOUNTS:
        path = global_config(acct)
        try:
            out.append((tag, path, json.loads(path.read_text())))
        except (OSError, ValueError) as exc:
            rep.warn(f"⚠️  {tag}: cannot read {path} ({exc}); MCP sync skipped for it")
    return out


def sync_mcp(rep, remove=None):
    configs = load_accounts_config(rep)
    if remove:
        for tag, path, cfg in configs:
            if mcp_drop(cfg, remove):
                write_json(path, cfg)
                rep.say(f"🗑️  {tag}: removed MCP server {remove}")
    try:
        base = json.loads(MCP_STATE.read_text())
    except (OSError, ValueError):
        base = {}
    if remove:
        base = {k: v for k, v in base.items() if json.loads(k)[-1] != remove}

    flats = {tag: mcp_flatten(cfg) for tag, _, cfg in configs}
    mtimes = {tag: path.stat().st_mtime for tag, path, _ in configs}
    result = {}
    for key in sorted(set().union(*flats.values()) if flats else ()):
        present = {tag: f[key] for tag, f in flats.items() if key in f}
        changed = {t: v for t, v in present.items() if key not in base or v != base[key]}
        if not changed:
            result[key] = base[key]
            continue
        distinct = {json.dumps(v, sort_keys=True) for v in changed.values()}
        winner = max(changed, key=lambda t: mtimes[t])
        if len(distinct) > 1:
            rep.warn(f"⚠️  MCP {json.loads(key)} edited differently in {', '.join(changed)}; kept {winner}'s")
        result[key] = changed[winner]

    for tag, path, cfg in configs:
        todo = {k: v for k, v in result.items() if flats[tag].get(k) != v}
        if not todo:
            continue
        for key, value in todo.items():
            mcp_apply(cfg, key, value)
        write_json(path, cfg)
        names = sorted({json.loads(k)[-1] for k in todo})
        rep.say(f"🔌 {tag}: synced MCP {', '.join(names)}")
    write_json(MCP_STATE, result)


def mcp_status():
    configs = load_accounts_config(Report(True))
    flats = {tag: mcp_flatten(cfg) for tag, _, cfg in configs}
    names = lambda f: sorted({json.loads(k)[-1] for k in f if json.loads(k)[0] != "list"})
    same = len({json.dumps(f, sort_keys=True) for f in flats.values()}) <= 1
    for tag, f in flats.items():
        print(f"   {'✅' if same else '❌'} MCP {tag}: {', '.join(names(f)) or '(none)'}")
    return same


def status():
    ok = True
    for tag, acct in ACCOUNTS:
        if not acct.is_dir() or acct.is_symlink():
            print(f"❌ {tag}: {acct} missing or not a real directory")
            ok = False
            continue
        bad = []
        for name in SHARED_DIRS + SHARED_FILES:
            path = acct / name
            if points_to_pool(path, name):
                continue
            kind = "missing" if not os.path.lexists(path) else (
                f"link -> {os.readlink(path)}" if path.is_symlink() else "real")
            bad.append(f"{name} ({kind})")
        if bad:
            ok = False
            print(f"❌ {tag}: {acct} not shared: {', '.join(bad)}")
        else:
            print(f"✅ {tag}: {acct} -> {POOL}")
    projects = POOL / "projects"
    if projects.is_dir():
        chats = sum(1 for _ in projects.glob("*/*.jsonl"))
        print(f"   pool: {chats} chats in {sum(1 for p in projects.iterdir() if p.is_dir())} working dirs")
    return mcp_status() and ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fix", action="store_true", help="migrate and link every account dir")
    ap.add_argument("--quiet", action="store_true", help="print only warnings")
    ap.add_argument("--mcp-remove", metavar="NAME", help="remove an MCP server from every account")
    args = ap.parse_args()

    if args.mcp_remove:
        sync_mcp(Report(args.quiet), remove=args.mcp_remove)
    elif args.fix:
        rep = Report(args.quiet)
        fix(rep)
        if args.quiet:
            return 0
    return 0 if status() else 1


if __name__ == "__main__":
    sys.exit(main())
