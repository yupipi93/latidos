// Claude tab relaunch: reopen given Claude Code tabs so they start a new Claude process.
//
// Why: a Claude tab whose process ended (an account switch ends it on purpose) only starts
// a new one when something is typed into it. Reopening the tab makes the Claude extension
// load the session again, and loading launches its process under the current settings
// (the new account). accounts/cursor-claude-account.py --move-open
// then sends "continúa" to the sessions whose turn was cut, through the process socket.
//
// Protocol (files, so nothing has to listen on a port):
//   <root>/windows/<pid>.json          heartbeat of this window; <pid> is the extension host
//                                      pid, which is the parent of every Claude process the
//                                      window's tabs run, so the caller knows where to send.
//   <root>/requests/<pid>/<id>.json    {"id", "created_at" (epoch s), "expect_config_dir",
//                                       "ops": [{"session", "titles": [...]}]}
//                                      (an op with "action": "open" only opens the session: self-test)
//   <root>/responses/<id>.json         {"id", "pid", "results": [{"session", "result", ...}]}
// <root> is ~/.local/state/cursor-claude-relaunch unless CLAUDE_RELAUNCH_DIR is set.

const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");

const ROOT = process.env.CLAUDE_RELAUNCH_DIR || path.join(os.homedir(), ".local", "state", "cursor-claude-relaunch");
const PID = process.pid;
const INBOX = path.join(ROOT, "requests", String(PID));
const OUTBOX = path.join(ROOT, "responses");
const WINDOWS = path.join(ROOT, "windows");
const HEARTBEAT = path.join(WINDOWS, `${PID}.json`);
const MAX_AGE_S = 120;
const CLAUDE_VIEW = "claudeVSCodePanel";

let output;
let busy = Promise.resolve();
const seen = new Set();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (msg) => output && output.appendLine(`${new Date().toISOString()} ${msg}`);

// Same rule as the Claude extension's tab title: the session title, cut to 24 chars + "…".
function tabLabel(title) {
	if (!title) return "Claude Code";
	return title.length > 25 ? title.substring(0, 24) + "…" : title;
}

function claudeTabs() {
	const tabs = [];
	for (const group of vscode.window.tabGroups.all) {
		for (const tab of group.tabs) {
			const input = tab.input;
			if (input instanceof vscode.TabInputWebview && input.viewType.includes(CLAUDE_VIEW)) {
				tabs.push({ tab, group });
			}
		}
	}
	return tabs;
}

function writeJson(file, data) {
	fs.mkdirSync(path.dirname(file), { recursive: true });
	const tmp = `${file}.${PID}.tmp`;
	fs.writeFileSync(tmp, JSON.stringify(data, null, 2) + "\n");
	fs.renameSync(tmp, file);
}

// Its own package.json: the extension id is set at install time from the accounts config
// (`companion_id`), so it is not known here (accounts/relaunch/install.py).
const COMPANION_VERSION = (() => {
	try {
		return require("./package.json").version || null;
	} catch (e) {
		return null;
	}
})();

function heartbeat() {
	try {
		const claude = vscode.extensions.getExtension("anthropic.claude-code");
		writeJson(HEARTBEAT, {
			pid: PID,
			updated_at: Math.floor(Date.now() / 1000),
			folders: (vscode.workspace.workspaceFolders || []).map((f) => f.uri.fsPath),
			claude_extension: claude ? claude.packageJSON.version : null,
			// The version this window actually runs: an install only takes effect after a
			// reload, and the script warns when the two differ.
			companion: COMPANION_VERSION,
			claude_tabs: claudeTabs().map((t) => t.tab.label),
			groups: vscode.window.tabGroups.all.map((g) => ({
				column: g.viewColumn,
				tabs: g.tabs.map((t) => t.label),
				active: g.activeTab ? g.tabs.indexOf(g.activeTab) : -1,
			})),
		});
	} catch (err) {
		log(`heartbeat failed: ${err}`);
	}
}

const isClaude = (tab) => tab && tab.input instanceof vscode.TabInputWebview && tab.input.viewType.includes(CLAUDE_VIEW);
const tabKey = (group, tab) => `${group.viewColumn}:${group.tabs.indexOf(tab)}:${tab.label}`;

// Active tab of every editor group, keyed by column.
function activeTabs() {
	const m = new Map();
	for (const g of vscode.window.tabGroups.all) if (g.activeTab) m.set(g.viewColumn, tabKey(g, g.activeTab));
	return m;
}

// Find the tab that shows a session. The Claude extension keeps its own session -> tab map:
// opening a session that already has a tab only reveals that tab (active in its group, focus
// kept). So the Claude tab that becomes active is the one. If nothing changes the tab was
// already active; then its label must match one of the session's titles.
async function findSessionTab(op) {
	const labels = new Set((op.titles || []).filter(Boolean).map(tabLabel));
	const before = activeTabs();
	const total = claudeTabs().length;
	await vscode.commands.executeCommand("claude-vscode.editor.open", op.session, undefined, undefined,
		undefined, undefined, { programmatic: "pin-to-panel" });
	let changed = [];
	for (let i = 0; i < 15 && changed.length === 0; i++) {
		await sleep(100);
		changed = vscode.window.tabGroups.all.filter((g) => isClaude(g.activeTab)
			&& before.get(g.viewColumn) !== tabKey(g, g.activeTab));
	}
	await sleep(150);
	if (claudeTabs().length > total) {
		// The session had no tab here, so the extension opened one: that new tab is it.
		return { how: "opened", hit: changed.length === 1 ? { group: changed[0], tab: changed[0].activeTab } : null };
	}
	if (changed.length === 1) return { how: "revealed", hit: { group: changed[0], tab: changed[0].activeTab } };
	const active = vscode.window.tabGroups.all.filter((g) => isClaude(g.activeTab));
	const byLabel = active.filter((g) => labels.has(g.activeTab.label));
	if (byLabel.length === 1) return { how: "already-active", hit: { group: byLabel[0], tab: byLabel[0].activeTab } };
	return { how: "ambiguous", hit: null };
}

const FOCUS_GROUP = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth"]
	.map((n) => `workbench.action.focus${n}EditorGroup`);

const groupAt = (column) => vscode.window.tabGroups.all.find((g) => g.viewColumn === column);

// Focus an existing editor group by column. The focusNth commands count groups in grid
// order, like viewColumn, and only add a group when asked for one past the last: never here.
async function focusColumn(column) {
	if (!column || column > vscode.window.tabGroups.all.length || column > FOCUS_GROUP.length) return false;
	await vscode.commands.executeCommand(FOCUS_GROUP[column - 1]);
	for (let i = 0; i < 10; i++) {
		if (vscode.window.tabGroups.activeTabGroup.viewColumn === column) return true;
		await sleep(50);
	}
	return false;
}

// Put the reopened tab back at its old index. It opens next to the group's active editor,
// not where it was, and the workbench only moves the editor that has the focus.
async function restorePosition(column, index) {
	const group = groupAt(column);
	if (!group || !isClaude(group.activeTab)) return "unknown";
	if (group.tabs.indexOf(group.activeTab) === index) return "kept";
	if (!(await focusColumn(column))) return "not-moved";
	const active = vscode.window.tabGroups.activeTabGroup.activeTab;
	if (!isClaude(active)) return "not-moved";
	await vscode.commands.executeCommand("moveActiveEditor", { to: "position", by: "tab", value: index + 1 });
	await sleep(150);
	const after = groupAt(column);
	return after && after.tabs.indexOf(after.activeTab) === index ? "restored" : "not-moved";
}

async function relaunch(op) {
	const { how, hit } = await findSessionTab(op);
	if (!hit) return { session: op.session, result: "ambiguous", how };
	const column = hit.group.viewColumn;
	const index = hit.group.tabs.indexOf(hit.tab);
	const label = hit.tab.label;
	// Closing the tab ends its process (if any is left); reopening loads the session, and
	// loading launches a new process with the current claudeCode.environmentVariables.
	const count = claudeTabs().length;
	await vscode.window.tabGroups.close(hit.tab, true);
	await sleep(600);
	await vscode.commands.executeCommand("claude-vscode.editor.open", op.session, undefined, column,
		undefined, undefined, { programmatic: "pin-to-panel" });
	// Wait for the new tab, so the next op counts and compares tabs on a settled layout.
	for (let i = 0; i < 30 && claudeTabs().length < count; i++) await sleep(100);
	await sleep(300);
	if (claudeTabs().length < count) return { session: op.session, result: "reopen-pending", how, label, column };
	const position = await restorePosition(column, index);
	return { session: op.session, result: "reopened", how, label, column, index, position };
}

// Which tab each group showed, and which group had the focus, before anything moved.
function snapshotView() {
	return {
		focused: vscode.window.tabGroups.activeTabGroup.viewColumn,
		active: vscode.window.tabGroups.all.map((g) => ({ column: g.viewColumn, index: g.activeTab ? g.tabs.indexOf(g.activeTab) : -1 })),
	};
}

// Show again the tab each group showed (indexes hold: every reopened tab is back in place)
// and give the focus back. Only touches what changed.
async function restoreView(view, refocusTerminal) {
	let moved = false;
	for (const { column, index } of view.active) {
		const g = groupAt(column);
		if (!g || index < 0 || index >= g.tabs.length || g.tabs.indexOf(g.activeTab) === index) continue;
		if (await focusColumn(column)) {
			await vscode.commands.executeCommand("workbench.action.openEditorAtIndex", index);
			moved = true;
		}
	}
	if (moved || vscode.window.tabGroups.activeTabGroup.viewColumn !== view.focused) await focusColumn(view.focused);
	if (refocusTerminal && vscode.window.activeTerminal) await vscode.commands.executeCommand("workbench.action.terminal.focus");
}

// Self-test only: open a session in a tab of this window.
async function open(op) {
	await vscode.commands.executeCommand("claude-vscode.editor.open", op.session, undefined, undefined,
		undefined, undefined, { programmatic: "pin-to-panel" });
	return { session: op.session, result: "opened" };
}

// CLAUDE_CONFIG_DIR as this window's settings hold it right now. The Claude extension reads
// the same setting when it spawns a process.
function configuredDir() {
	const raw = vscode.workspace.getConfiguration("claudeCode").get("environmentVariables");
	let value = "";
	if (Array.isArray(raw)) value = (raw.find((e) => e && e.name === "CLAUDE_CONFIG_DIR") || {}).value || "";
	else if (raw && typeof raw === "object") value = raw.CLAUDE_CONFIG_DIR || "";
	return String(value).replace(/\/+$/, "");
}

// The settings file is written by the switch just before the request, and a window can take
// a few seconds to notice. Reopening earlier would relaunch on the old account.
async function waitForConfig(expected, ms = 20000) {
	if (!expected) return true;
	const want = String(expected).replace(/\/+$/, "");
	for (let waited = 0; waited <= ms; waited += 200) {
		if (configuredDir() === want) {
			await sleep(300);
			return true;
		}
		await sleep(200);
	}
	return false;
}

async function handle(file) {
	const name = path.basename(file);
	if (!name.endsWith(".json") || seen.has(name)) return;
	seen.add(name);
	let request;
	try {
		request = JSON.parse(fs.readFileSync(file, "utf8"));
		fs.unlinkSync(file);
	} catch (err) {
		log(`unreadable request ${name}: ${err}`);
		return;
	}
	const age = Date.now() / 1000 - (request.created_at || 0);
	const results = [];
	if (age > MAX_AGE_S) {
		log(`request ${request.id} is ${Math.round(age)} s old, ignored`);
		results.push({ result: "expired" });
	} else if (!(await waitForConfig(request.expect_config_dir))) {
		log(`${request.id}: settings still say ${configuredDir() || "(unset)"}, not ${request.expect_config_dir}; nothing reopened`);
		for (const op of request.ops || []) {
			results.push({ session: op.session, result: "config-not-applied", configured: configuredDir() });
		}
	} else {
		const view = snapshotView();
		let relaunched = false;
		for (const op of request.ops || []) {
			try {
				const res = op.action === "open" ? await open(op) : await relaunch(op);
				relaunched = relaunched || res.result === "reopened";
				log(`${request.id}: ${op.session} ${res.result} (${res.label}, position ${res.position})`);
				results.push(res);
			} catch (err) {
				log(`${request.id}: ${op.session} failed: ${err}`);
				results.push({ session: op.session, result: "error", error: String(err) });
			}
		}
		if (relaunched) {
			try {
				await restoreView(view, request.refocus_terminal === true);
			} catch (err) {
				log(`${request.id}: could not restore the view: ${err}`);
			}
		}
	}
	writeJson(path.join(OUTBOX, `${request.id}.json`), { id: request.id, pid: PID, results });
	heartbeat();
}

function scan() {
	let names = [];
	try {
		names = fs.readdirSync(INBOX).filter((n) => n.endsWith(".json")).sort();
	} catch {
		return;
	}
	for (const n of names) {
		busy = busy.then(() => handle(path.join(INBOX, n))).catch((err) => log(`handle failed: ${err}`));
	}
}

function activate(context) {
	output = vscode.window.createOutputChannel("Claude tab relaunch");
	fs.mkdirSync(INBOX, { recursive: true });
	fs.mkdirSync(OUTBOX, { recursive: true });
	heartbeat();
	let watcher;
	try {
		watcher = fs.watch(INBOX, () => scan());
	} catch (err) {
		log(`fs.watch failed, polling only: ${err}`);
	}
	// fs.watch can miss events; a slow poll is the safety net, and it refreshes the heartbeat.
	const poll = setInterval(scan, 2000);
	const beat = setInterval(heartbeat, 30000);
	scan();
	context.subscriptions.push(output, {
		dispose() {
			clearInterval(poll);
			clearInterval(beat);
			if (watcher) watcher.close();
			try { fs.unlinkSync(HEARTBEAT); } catch { /* already gone */ }
		},
	});
	log(`listening on ${INBOX}`);
}

function deactivate() {
	try { fs.unlinkSync(HEARTBEAT); } catch { /* already gone */ }
}

module.exports = { activate, deactivate };
