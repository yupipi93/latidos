// Dev helper: records Latidos as numbered frames for the README animations (demo/README.md).
// Usage: node demo/record.mjs <outdir> <url>   (Node 22+ for the global WebSocket)
// Point it only at the synthetic demo (demo/demo.py), never at the live panel.
// Env: W=1280 H=800 DPR=1 SCHEME=dark|light MOBILE=0 HASH=#x WAIT=4500 (ms before the first
//      frame)  SECONDS=10  FPS=15  EVAL='js' (runs after WAIT)  STEPS='[[ms,"js"],...]' (js run
//      at ms into the recording)  SHOOT_TIMEOUT=180 (s, whole run)
// Frames are JPEG (frame-00001.jpg ...) with frames.txt, an ffmpeg concat list with each
// frame's real duration:  ffmpeg -f concat -i <outdir>/frames.txt -vf fps=15,... out.gif
//
// Chrome never outlives this script: it runs detached in its own process group, and the
// group is killed and the temp profile removed on success, error, timeout and signals.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";

const out = process.argv[2];
const url = process.argv[3];
if (!out || !url) {
  console.error("usage: node demo/record.mjs <outdir> <url>  (the synthetic demo, see demo/README.md)");
  process.exit(2);
}
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const SEND_TIMEOUT_MS = 30000;
const HARD_TIMEOUT_MS = Number(process.env.SHOOT_TIMEOUT || 180) * 1000;
mkdirSync(out, { recursive: true });
const profile = mkdtempSync(path.join(os.tmpdir(), "latidos-record-"));
const port = 9300 + Math.floor(Math.random() * 400);
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--hide-scrollbars", ...(process.env.CHROME_FLAGS || "").split(" ").filter(Boolean), "about:blank"], { stdio: "ignore", detached: true });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const alive = () => chrome.exitCode === null && chrome.signalCode === null;
const killGroup = (sig) => {
  if (!chrome.pid) return;
  try { process.kill(-chrome.pid, sig); } catch { try { chrome.kill(sig); } catch { /* gone */ } }
};
const exited = new Promise((r) => chrome.once("exit", r));
let cleaned = false;
const removeProfile = () => { try { rmSync(profile, { recursive: true, force: true }); } catch { /* best effort */ } };
// Graceful: TERM the group, give Chrome 2 s, then KILL whatever is left (helpers may
// outlive the leader) and delete the profile.
async function cleanup() {
  if (cleaned) return;
  cleaned = true;
  killGroup("SIGTERM");
  if (alive()) await Promise.race([exited, sleep(2000)]);
  killGroup("SIGKILL");
  removeProfile();
}
// Last resort, synchronous: runs on any process.exit() that skipped cleanup().
process.on("exit", () => {
  if (cleaned) return;
  cleaned = true;
  killGroup("SIGKILL");
  removeProfile();
});
const bail = async (code, why) => { console.error(`record: ${why}, stopping Chrome`); await cleanup(); process.exit(code); };
for (const [sig, n] of [["SIGINT", 2], ["SIGTERM", 15], ["SIGHUP", 1]]) process.on(sig, () => bail(128 + n, sig));
setTimeout(() => bail(3, `no result after ${HARD_TIMEOUT_MS / 1000}s (SHOOT_TIMEOUT)`), HARD_TIMEOUT_MS).unref();

async function target() {
  for (let i = 0; i < 50; i++) {
    if (!alive()) throw new Error("chrome exited before it was ready");
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`, { signal: AbortSignal.timeout(2000) })).json();
      const page = list.find((t) => t.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {}
    await sleep(200);
  }
  throw new Error("chrome did not start");
}

let ws;
try {
  ws = new WebSocket(await target());
  await new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("DevTools websocket did not open")), 10000);
    ws.addEventListener("open", () => { clearTimeout(t); resolve(); }, { once: true });
    ws.addEventListener("error", () => { clearTimeout(t); reject(new Error("DevTools websocket error")); }, { once: true });
  });
  let id = 0;
  const pending = new Map();
  const logs = [];
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === "Runtime.exceptionThrown") logs.push("EXC " + JSON.stringify(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
    if (m.method === "Runtime.consoleAPICalled" && ["error", "warning"].includes(m.params.type)) logs.push(m.params.type + " " + m.params.args.map((a) => a.value ?? a.description).join(" "));
    if (m.method === "Log.entryAdded" && m.params.entry.level === "error") logs.push("LOG " + m.params.entry.text);
  });
  ws.addEventListener("close", () => {
    for (const [, r] of pending) r({ error: { message: "DevTools websocket closed" } });
    pending.clear();
  });
  // Every CDP call times out: a hung Chrome must not hang the script.
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const i = ++id;
    const t = setTimeout(() => { pending.delete(i); reject(new Error(`${method} timed out`)); }, SEND_TIMEOUT_MS);
    pending.set(i, (m) => { clearTimeout(t); m.error ? reject(new Error(`${method}: ${m.error.message}`)) : resolve(m); });
    ws.send(JSON.stringify({ id: i, method, params }));
  });

  await send("Runtime.enable"); await send("Log.enable"); await send("Page.enable");
  const W = Number(process.env.W || 1280), H = Number(process.env.H || 800);
  const DPR = Number(process.env.DPR || 1), FPS = Number(process.env.FPS || 15);
  const mobile = process.env.MOBILE === "1";
  await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: DPR, mobile });
  await send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: process.env.SCHEME || "dark" }] });
  await send("Page.navigate", { url: url + (process.env.HASH || "") });
  await sleep(Number(process.env.WAIT || 4500));
  if (process.env.EVAL) await send("Runtime.evaluate", { expression: process.env.EVAL });
  const steps = JSON.parse(process.env.STEPS || "[]");
  const total = Number(process.env.SECONDS || 10) * 1000;
  // Screencast: Chrome pushes a frame whenever the page repaints; each keeps its timestamp,
  // and frames.txt (an ffmpeg concat list) gives every frame its real duration.
  const frames = [];
  ws.addEventListener("message", (e) => {
    const m = JSON.parse(e.data);
    if (m.method !== "Page.screencastFrame") return;
    const file = "frame-" + String(frames.length + 1).padStart(5, "0") + ".jpg";
    writeFileSync(path.join(out, file), Buffer.from(m.params.data, "base64"));
    frames.push([file, m.params.metadata.timestamp]);
    ws.send(JSON.stringify({ id: 900000 + frames.length, method: "Page.screencastFrameAck", params: { sessionId: m.params.sessionId } }));
  });
  // Headless Chrome sends screencast frames only while something asks for animation
  // frames; an empty rAF loop keeps it at the display rate.
  await send("Runtime.evaluate", { expression: "(function keep(){requestAnimationFrame(keep)})()" });
  const t0 = Date.now();
  await send("Page.startScreencast", { format: "jpeg", quality: 90, everyNthFrame: 1 });
  while (Date.now() - t0 < total) {
    const at = Date.now() - t0;
    while (steps.length && steps[0][0] <= at) {
      const r = await send("Runtime.evaluate", { expression: steps.shift()[1] });
      if (r.result.exceptionDetails) logs.push("STEP " + r.result.exceptionDetails.text);
    }
    await sleep(50);
  }
  await send("Page.stopScreencast");
  let list = "ffconcat version 1.0\n";
  frames.forEach(([file, ts], i) => {
    const next = i + 1 < frames.length ? frames[i + 1][1] : ts + 1 / FPS;
    list += "file " + file + "\nduration " + Math.max(0.001, next - ts).toFixed(4) + "\n";
  });
  if (frames.length) list += "file " + frames[frames.length - 1][0] + "\n";
  writeFileSync(path.join(out, "frames.txt"), list);
  const span = frames.length > 1 ? frames[frames.length - 1][1] - frames[0][1] : 0;
  console.log(frames.length + " frames over " + span.toFixed(1) + " s (" + (frames.length / (span || 1)).toFixed(1) + " fps)");
  console.log(logs.length ? logs.join("\n") : "no console errors");
} catch (e) {
  console.error("record:", e.message);
  process.exitCode = 1;
} finally {
  try { ws?.close(); } catch { /* already closed */ }
  await cleanup();
}
process.exit();
