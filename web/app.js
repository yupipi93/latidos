/* Latidos: the living panel. No dependencies; Canvas 2D for the moving parts, DOM for text. */
(function () {
  "use strict";
  var F = window.LatidosFormat;
  var I = window.LatidosI18n;
  var SND = window.LatidosSound;
  // Page copy in the current language (web/i18n.js); many() counts a noun ("3 sessions").
  var say = I.t, many = F.count;
  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var $ = function (id) { return document.getElementById(id); };

  // -------------------------------------------------------------------------------------
  // State
  // -------------------------------------------------------------------------------------
  var S = {
    range: "30d", custom: null,      // custom: {from, to}; 30 days by default
    data: null, version: -1, seq: 0, sparked: 0, loadedAt: 0, boot: null, epoch: 0,
    queue: [], lastPoll: 0,
    // What is shown (L-158): Cursor, and the Claude accounts not in `hidden` (tag -> true),
    // both switched from the account cards; at least one source always stays on.
    cursor: store("latidos.cursor") !== "0", hidden: hiddenFromStore(),
    currency: store("latidos.currency") === "USD" ? "USD" : "EUR",
    factor: 1,
    lastOk: 0, failing: false,
    colors: {},                       // project -> slot (0..7) or -1
    theme: {},
    pulse: null,
    activity: 0                       // 0..1, recent live intensity
  };

  function store(k, v) {
    try {
      if (v === undefined) return localStorage.getItem(k);
      localStorage.setItem(k, v);
    } catch (e) { return null; }
    return null;
  }

  function rate() { return (S.data && S.data.fx && S.data.fx.usd_eur) || 0.86; }
  function cur(usd) { return S.currency === "EUR" ? usd * rate() : usd; }
  function money(usd) { return F.money(cur(usd), S.currency); }

  // -------------------------------------------------------------------------------------
  // Theme
  // -------------------------------------------------------------------------------------
  function readTheme() {
    var cs = getComputedStyle(document.documentElement);
    var g = function (n) { return cs.getPropertyValue(n).trim(); };
    S.theme = {
      bg: g("--bg"), ink: g("--ink"), muted: g("--muted"), faint: g("--faint"), line: g("--line"),
      pulse: g("--pulse"), glow: parseFloat(g("--glow")) || 1, blend: g("--blend") || "source-over",
      series: [g("--s1"), g("--s2"), g("--s3"), g("--s4"), g("--s5"), g("--s6"), g("--s7"), g("--s8")],
      other: g("--s-other"), cursor: g("--cursor"), cursorDeep: g("--cursor-deep"), ra: g("--river-a"), rb: g("--river-b"), rc: g("--river-c"),
      dark: g("--blend") === "lighter"
    };
    if (eco) eco.recolor();
  }
  function hueHex(h, l) {
    var s = 0.62, ll = l / 100, a = s * Math.min(ll, 1 - ll);
    var f = function (n) { var k = (n + h / 30) % 12; var c = ll - a * Math.max(-1, Math.min(k - 3, 9 - k, 1)); return Math.round(c * 255); };
    return "#" + [f(0), f(8), f(4)].map(function (v) { return (v < 16 ? "0" : "") + v.toString(16); }).join("");
  }
  function mixHex(a, b, k) {
    var pa = /^#([0-9a-f]{6})$/i.exec(a || ""), pb = /^#([0-9a-f]{6})$/i.exec(b || "");
    if (!pa || !pb) return b || a;
    var x = parseInt(pa[1], 16), y = parseInt(pb[1], 16), out = 0;
    for (var s = 16; s >= 0; s -= 8) out = (out << 8) | Math.round(((x >> s) & 255) * (1 - k) + ((y >> s) & 255) * k);
    return "#" + ("00000" + out.toString(16)).slice(-6);
  }
  // Cursor cells live in Cursor's cool range: eight well-separated hues from sea green
  // through teal, sky and indigo to violet, two lightness levels, chosen per project (stable):
  // apart from Claude's cells and from each other.
  var CURSOR_HUES = [158, 176, 192, 206, 222, 240, 258, 276];
  function cursorTint(name) {
    var base = S.theme.cursor, h = hash("cursor:" + name), l = hash(name + "l") < 0.5 ? 0 : 1;
    var hue = CURSOR_HUES[Math.floor(h * CURSOR_HUES.length) % CURSOR_HUES.length];
    return mixHex(base, hueHex(hue, S.theme.dark ? 56 + l * 14 : 30 + l * 14), 0.9);
  }
  // A project's own colour everywhere it is drawn (cell, label, ranking, sparks, sheet): its
  // palette slot, or a stable hue from its name past the eight slots. Never grey.
  function projectColor(name) {
    return S.colors[name] >= 0 ? colorOf(name) : hueHex(hash(name) * 360, S.theme.dark ? 62 : 52);
  }
  function colorOf(name) {
    var slot = S.colors[name];
    return slot >= 0 ? S.theme.series[slot] : S.theme.other;
  }
  // Colour follows the project, never its rank in the current range: a project keeps the slot
  // it got the first time it showed up (biggest spenders first), saved in localStorage.
  function assignColors(projects) {
    var saved = {};
    try { saved = JSON.parse(store("latidos.colors") || "{}") || {}; } catch (e) { saved = {}; }
    var used = {};
    Object.keys(saved).forEach(function (k) { if (saved[k] >= 0) used[saved[k]] = k; });
    projects.forEach(function (p) {
      if (p.name in saved) return;
      var slot = -1;
      for (var i = 0; i < 8; i++) if (!(i in used)) { slot = i; break; }
      saved[p.name] = slot;
      if (slot >= 0) used[slot] = p.name;
    });
    S.colors = saved;
    store("latidos.colors", JSON.stringify(saved));
  }

  var themeBtn = $("theme");
  function themeMode() { return document.documentElement.dataset.theme || "system"; }
  function setTheme(mode) {
    if (mode === "system") { delete document.documentElement.dataset.theme; try { localStorage.removeItem("latidos.theme"); } catch (e) {} }
    else { document.documentElement.dataset.theme = mode; store("latidos.theme", mode); }
    paintTheme(mode);
    readTheme();
    drawAll();
  }
  function paintTheme(mode) {
    themeBtn.textContent = mode === "light" ? "☀" : mode === "dark" ? "☾" : "◐";
    themeBtn.title = say("tools.theme.title", { mode: say("theme." + mode) });
  }
  themeBtn.addEventListener("click", function () {
    var m = themeMode();
    setTheme(m === "system" ? "light" : m === "light" ? "dark" : "system");
  });
  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: dark)");
    (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(function () { readTheme(); drawAll(); });
  }

  // -------------------------------------------------------------------------------------
  // Ranges and URL hash
  // -------------------------------------------------------------------------------------
  // Range ids stay as they are in URL hashes and API calls; their names are in web/i18n.js.
  var RANGE_IDS = { hoy: 1, "24h": 1, "7d": 1, "30d": 1, "90d": 1, todo: 1 };
  function bounds() {
    var now = Date.now();
    if (S.custom) return { from: S.custom.from, to: S.custom.to };
    if (S.range === "hoy") { var d = new Date(); d.setHours(0, 0, 0, 0); return { from: d.getTime(), to: now + 60000 }; }
    if (S.range === "24h") return { from: now - 86400000, to: now + 60000 };
    if (S.range === "7d") return { from: now - 7 * 86400000, to: now + 60000 };
    if (S.range === "30d") return { from: now - 30 * 86400000, to: now + 60000 };
    if (S.range === "90d") return { from: now - 90 * 86400000, to: now + 60000 };
    return { from: 0, to: now + 60000 };
  }
  function includesNow() { return bounds().to >= Date.now(); }
  function readHash() {
    var h = {};
    location.hash.replace(/^#/, "").split("&").forEach(function (kv) {
      var p = kv.split("="); if (p[0]) h[p[0]] = decodeURIComponent(p[1] || "");
    });
    if (h.f && h.t && +h.t > +h.f) { S.custom = { from: +h.f, to: +h.t }; S.range = "custom"; }
    else if (RANGE_IDS[h.r]) { S.range = h.r; S.custom = null; }
  }
  function writeHash() {
    var h = S.custom ? "f=" + S.custom.from + "&t=" + S.custom.to : "r=" + S.range;
    if (location.hash.slice(1) !== h) history.replaceState(null, "", "#" + h);
  }
  function setRange(r, custom) {
    S.range = r; S.custom = custom || null; S.epoch++;
    writeHash(); paintRanges(); showSnap(); load(true);
  }
  function paintRanges() {
    document.querySelectorAll(".ranges [data-range]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(!S.custom && b.dataset.range === S.range));
    });
    var c = $("custom-range");
    if (S.custom) {
      c.hidden = false;
      c.setAttribute("aria-pressed", "true");
      c.textContent = F.date(S.custom.from, { year: false }) + " – " + F.date(S.custom.to - 1, { year: false });
      c.title = say("range.custom.clear");
    } else c.hidden = true;
    $("hero-range").textContent = S.custom ? say("range.custom", { from: F.date(S.custom.from, { time: true }), to: F.date(S.custom.to, { time: true }) })
      : say("range." + S.range);
  }
  document.querySelector(".ranges").addEventListener("click", function (e) {
    var b = e.target.closest("button");
    if (!b) return;
    if (b.id === "custom-range") return setRange("todo");
    setRange(b.dataset.range);
  });
  $("currency").addEventListener("click", function () {
    S.currency = S.currency === "EUR" ? "USD" : "EUR";
    store("latidos.currency", S.currency);
    $("currency").textContent = S.currency === "EUR" ? "€" : "$";
    render();
  });
  $("currency").textContent = S.currency === "EUR" ? "€" : "$";
  // Sound (L-172): off by default, kept per browser. Browsers only let a page play after a
  // click or a key, so a remembered "on" waits for the first one; it is silent while the
  // tab is hidden (no frames, no hits) and the engine is suspended then.
  var sound = new SND.Engine(), soundBtn = $("sound");
  var soundMissing = false;
  function paintSound() {
    var on = sound.on, wait = on && !sound.ready();
    soundBtn.textContent = on ? "🔊" : "🔇";
    soundBtn.setAttribute("aria-pressed", on ? "true" : "false");
    // The label carries the state too, so a screen reader hears "waiting for a click".
    soundBtn.title = say(soundMissing ? "tools.sound.missing" : on ? (wait ? "tools.sound.wait" : "tools.sound.on") : "tools.sound.off");
    soundBtn.setAttribute("aria-label", soundBtn.title);
  }
  function setSound(on) {
    if (sound.setOn(on) !== on) { soundMissing = true; return paintSound(); }
    store("latidos.sound", on ? "1" : "0");
    if (on) setTimeout(function () { sound.greet(); paintSound(); }, 60);
    paintSound();
  }
  soundBtn.addEventListener("click", function () { setSound(!sound.on); });
  if (store("latidos.sound") === "1") sound.setOn(true);
  ["pointerdown", "keydown"].forEach(function (ev) {
    document.addEventListener(ev, function () {
      if (!sound.on || document.hidden || sound.ready()) return;
      sound.resume(); setTimeout(paintSound, 60);
    }, true);
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) sound.suspend(); else sound.resume();
    setTimeout(paintSound, 60);
  });
  paintSound();
  // Language (L-121): English by default, ?lang=es|en in the URL, or the choice made with
  // this chip (kept per browser). A switch re-renders everything in place.
  var langBtn = $("lang");
  function paintLang() {
    langBtn.textContent = say("lang.flag");
    langBtn.title = say("lang.title");
    langBtn.setAttribute("aria-label", say("lang.aria"));
  }
  function setLang(l) {
    I.setLang(l);
    store("latidos.lang", I.getLang());
    // A ?lang= in the URL would win again on reload: it follows the choice.
    try {
      var u = new URL(location.href);
      if (u.searchParams.has("lang")) { u.searchParams.set("lang", I.getLang()); history.replaceState(null, "", u.pathname + u.search + u.hash); }
    } catch (e) {}
    relabelAll();
  }
  langBtn.addEventListener("click", function () { setLang(I.getLang() === "es" ? "en" : "es"); });
  // L-158: every account card has its own eye switch (the header chips are gone).
  function hiddenFromStore() {
    var out = {};
    try {
      var a = JSON.parse(store("latidos.hidden") || "[]");
      (Array.isArray(a) ? a : []).forEach(function (t) { if (typeof t === "string" && /^[A-Za-z0-9_.-]{1,32}$/.test(t)) out[t] = true; });
    } catch (e) {}
    return out;
  }
  function hiddenTags() { return Object.keys(S.hidden).sort(); }
  // A live call is played only when its account is shown (Cursor's by its own switch).
  // By origin, as the server filters (a Bedrock or Vertex call made under a hidden
  // account's login is still shown: audit of 86).
  function shownEv(ev) {
    if (ev.cursor) return S.cursor;
    var o = ev.origin || (ev.account ? "acct:" + ev.account : "");
    return !(o.indexOf("acct:") === 0 && S.hidden[o.slice(5)]);
  }
  function hideParam() {
    var h = hiddenTags();
    return h.length ? "&hide=" + encodeURIComponent(h.map(function (t) { return "acct:" + t; }).join(",")) : "";
  }
  // Every Claude card off and Cursor on: the river follows Cursor's days (audit of 86).
  function onlyCursor() {
    var t = claudeTags();
    return S.cursor && t.length > 0 && t.every(function (x) { return S.hidden[x]; });
  }
  function claudeTags() {
    var d = QA.data, a = d && d.claude && d.claude.accounts || [];
    return a.map(function (x) { return x.tag; }).filter(Boolean);
  }
  function setShown(tag, show) {  // tag: a Claude account tag, or "cursor"
    var on = tag === "cursor" ? S.cursor : !S.hidden[tag];
    if (on === show) return;
    if (!show) {
      // Never everything off: the last source shown stays (as the old chips did).
      var left = claudeTags().filter(function (t) { return t !== tag && !S.hidden[t]; }).length +
                 (tag !== "cursor" && S.cursor && cursorCardShown() ? 1 : 0);
      if (!left) { setNote(say("eye.last"), "error"); return; }
    }
    if (tag === "cursor") { S.cursor = show; store("latidos.cursor", show ? "1" : "0"); }
    else {
      if (show) delete S.hidden[tag]; else S.hidden[tag] = true;
      store("latidos.hidden", JSON.stringify(hiddenTags()));
    }
    S.epoch++;  // a new view: one reel spin, queued live calls of the old one dropped
    renderAccounts();
    showSnap(); load(true);
  }

  // -------------------------------------------------------------------------------------
  // Networking
  // -------------------------------------------------------------------------------------
  function getJSON(url) {
    return fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }
  var loading = false, pendingLoad = false, pendingReset = false;
  // What an answer was asked for: the range and whether Cursor was shown.
  function hiddenKey() { var h = hiddenTags(); return h.length ? "-h" + h.join(".") : ""; }
  function rangeKey() { return (S.custom ? "c" + S.custom.from + "-" + S.custom.to : S.range) + (S.cursor ? "+c" : "") + hiddenKey(); }

  // Last answer per standard range, kept in this browser: a range change (or opening the
  // page) paints it at once while the fresh one loads, so the filter reacts instantly.
  function snapKey() { return S.custom ? null : "latidos.snap." + S.range + (S.cursor ? ".c" : "") + hiddenKey(); }
  function saveSnap(d) {
    var k = snapKey();
    if (!k) return;
    try { localStorage.setItem(k, JSON.stringify({ at: Date.now(), day: new Date().toDateString(), d: d })); } catch (e) {}
  }
  function showSnap() {
    var k = snapKey(), snap = null;
    if (k) { try { snap = JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { snap = null; } }
    var fresh = snap && snap.d && Date.now() - snap.at < 12 * 3600000 && (S.range !== "hoy" || snap.day === new Date().toDateString());
    document.body.classList.toggle("refreshing", !!fresh);
    document.body.classList.toggle("switching", !fresh);
    paintStatus();
    // The data itself is marked: any render of it (a currency or source switch before the
    // fresh answer) knows it came from the snapshot.
    if (fresh) { S.data = snap.d; S.dataFromSnap = true; S.dataKey = rangeKey(); render(true); }
  }
  function load(reset) {
    if (loading) { pendingLoad = true; pendingReset = F.queueReset(pendingReset, reset); return Promise.resolve(); }
    loading = true;
    var b = bounds(), key = rangeKey();
    var url = (S.custom ? "/api/summary?from=" + b.from + "&to=" + b.to : "/api/summary?range=" + S.range) +
      (S.cursor ? "&cursor=1" : "") + hideParam();
    return getJSON(url).then(function (d) {
      ok();
      if (!F.answerApplies(key, rangeKey())) return;  // the range changed meanwhile: the queued load follows
      if (F.bootChanged(S.boot, d.boot)) { S.boot = d.boot; S.epoch++; hero.clear(); S.seq = 0; S.sparked = 0; S.version = -1; }
      if (S.boot == null) S.boot = d.boot;
      S.data = d; S.dataFromSnap = false; S.dataKey = key; S.version = F.nextVersion(S.version < 0 ? null : S.version, d.version, reset); S.loadedAt = Date.now();
      assignColors(d.projects);
      // Read live events again from what this summary counts (it may be a cached one
      // behind the events already seen); replays are de-duplicated by sequence.
      S.seq = F.rewindSeq(S.seq, d);
      document.body.classList.remove("refreshing", "switching");
      render(reset);
      saveSnap(d);
    }).catch(function (e) {
      fail(e);
      // A failed switch must not leave the page dimmed on another range's numbers.
      if (document.body.classList.contains("switching") || document.body.classList.contains("refreshing")) {
        setTimeout(function () { load(true); }, 3000);
      }
    }).then(function () {
      loading = false;
      if (pendingLoad) { var r = pendingReset; pendingLoad = pendingReset = false; load(r); }
    });
  }
  function ok() { S.lastOk = Date.now(); S.failing = false; paintStatus(); }
  function fail() { S.failing = true; paintStatus(); }
  function paintStatus() {
    var el = $("status"), heart = document.querySelector(".heart");
    var ing = S.data && S.data.ingest;
    el.classList.remove("warn");
    if (S.failing) {
      el.textContent = say("status.offline", { time: S.lastOk ? F.hour(S.lastOk) : "–" });
      el.classList.add("warn");
      heart.className = "heart off";
      return;
    }
    if (ing && ing.running && ing.bytes_total > 50e6) {
      el.textContent = say("status.reading", { pct: F.pct(ing.bytes_done / Math.max(1, ing.bytes_total)) });
      heart.className = "heart slow";
      return;
    }
    if (document.body.classList.contains("switching") || document.body.classList.contains("refreshing")) {
      el.textContent = say("status.updating");
      return;
    }
    var live = S.data && S.data.live && S.data.live.calls_15m;
    el.textContent = say(live ? "status.live" : "status.idle");
    heart.className = live ? "heart" : "heart slow";
  }

  // Live: small poll for new calls; a new data version reloads the summary (debounced).
  var reloadTimer = null;
  function pollLive() {
    var asked = S.epoch;  // a range change or restart while this poll runs voids its answer
    getJSON("/api/live?since=" + S.seq + hideParam()).then(function (d) {
      ok();
      if (asked !== S.epoch) return;
      if (F.bootChanged(S.boot, d.boot)) {
        // New server process: its sequence restarted. Forget pending live calls and
        // read its events from the start; the next summary has the rest.
        S.boot = d.boot; S.epoch++; hero.clear(); S.seq = 0; S.sparked = 0; S.version = -1;
        // The data on screen came from the old process: it answers no view of the new one.
        S.dataKey = null;
        if (!reloadTimer) reloadTimer = setTimeout(function () { reloadTimer = null; load(); }, 400);
        return;
      }
      S.boot = d.boot;
      if (S.data) { S.data.ingest = d.ingest; if (d.live) S.data.live = d.live; paintEcgText(); }
      var evs = d.events || [];
      S.seq = d.seq;
      S.lastPoll = Date.now();
      // Queued and played out evenly until the next poll (player below); each one checks
      // when it plays that nothing changed meanwhile (range change or restart: epoch).
      if (evs.length && includesNow()) evs.forEach(function (ev) {
        // A hidden account's call still tells its card it is in use, nothing else.
        if (!shownEv(ev)) { noteUse(ev); return; }
        if (!ev.cursor) ev.project = F.liveBubble(ev, S.data);  // client grouping (summary.fold)
        if (ev.seq <= S.sparked) { hero.bump(ev); return; }  // a replay: counted at once, not paced
        S.queue.push({ ev: ev, epoch: S.epoch });
      });
      eco.live(F.byProject(hero.pending()));
      if (S.queue.length > 300) S.queue.splice(0, S.queue.length - 300);
      // New data: reload, but at most every 20 s (the server may answer from a short cache).
      if (d.version > S.version && !reloadTimer) {
        var wait = Math.max(evs.length ? 2600 : 400, 20000 - (Date.now() - S.loadedAt));
        reloadTimer = setTimeout(function () { reloadTimer = null; load(); }, wait);
      }
    }).catch(fail).then(function () {
      setTimeout(pollLive, document.hidden ? 15000 : 4000);
    });
  }
  function onLive(ev) {
    hero.bump(ev);
    eco.live(F.byProject(hero.pending()));
    if (ev.seq <= S.sparked) return;  // a replay after a rewind: counted, not re-animated
    S.sparked = ev.seq;
    S.activity = Math.min(1, S.activity + 0.25);
    noteUse(ev);
    eco.spark(ev);
    ecg.beat(ev.tokens);
  }
  // Player: one queued call at a time, spaced so the batch ends about when the next poll
  // brings the next one (the poll runs every ~4 s).
  var nextPlay = 0;
  setInterval(function () {
    var now = Date.now();
    if (!S.queue.length || now < nextPlay) return;
    var item = S.queue.shift();
    if (item.epoch === S.epoch && includesNow() && shownEv(item.ev)) onLive(item.ev);
    var left = Math.max(400, (S.lastPoll || now) + 4600 - now);
    nextPlay = now + F.pace(S.queue.length, left, 90, 1400);
  }, 50);

  // -------------------------------------------------------------------------------------
  // Hero: rolling digits
  // -------------------------------------------------------------------------------------
  var hero = (function () {
    // An odometer, like an old electricity meter: the value chases its target smoothly;
    // the last digit spins fast and each digit to its left only rolls as the one to its
    // right passes 9 to 0. Live calls are added on top of the summary until a summary
    // that already counts them arrives (matched by ingest sequence).
    var el = $("hero-tokens");
    var shown = { tokens: 0, usd: 0 }, extras = [], seqAt = 0;
    var disp = null, cols = [], digits = 0, lastMoney = "", fast = false;
    var spin = null;  // slot effect after a range change: extra turns per wheel, easing out
    var lastBlur = [], spunEpoch = -1;
    // wheels[i]: the drum i places from the right ({ pos }); pos only grows.
    var wheels = [];
    function build(n) {
      el.textContent = "";
      cols = [];
      lastBlur = [];
      var old = wheels; wheels = [];
      digits = n;
      for (var i = n - 1; i >= 0; i--) {
        var col = document.createElement("span"); col.className = "col";
        var strip = document.createElement("span"); strip.className = "strip";
        // Top to bottom 0, 9, 8 ... 1, 0: the drum turns down, so the next number drops in
        // from above (0 -> 1 comes from the top), like a meter.
        for (var k = 10; k >= 0; k--) { var s = document.createElement("span"); s.textContent = k % 10; strip.appendChild(s); }
        col.appendChild(strip); el.appendChild(col);
        cols[i] = strip;
        wheels[i] = old[i] || { pos: 0 };
        if (i > 0 && i % 3 === 0) { var f = document.createElement("span"); f.className = "fix"; f.textContent = F.groupSep(); el.appendChild(f); }
      }
    }
    // Every wheel rolls to its digit, forward only: the right one turns fast (tau ~70 ms),
    // each one to its left slower (x1.55 per place, at most 1.4 s).
    function render(v, now, dt) {
      v = Math.max(0, v);
      var n = Math.max(1, String(Math.floor(v)).length);
      if (n !== digits) build(n);
      // A range change spins the drums (F.spinLeft): extra turns paid off in at most 1.5 s.
      var sp = spin ? now - spin.t0 : Infinity;
      if (sp > 1600) spin = null;
      var p = 1;
      for (var i = 0; i < n; i++) {
        var tgt = i === 0 ? v % 10 : Math.floor(v / p) % 10;
        var tau = Math.min(1400, 70 * Math.pow(1.55, i));
        var step = Math.min(dt || 16, 64);
        // While a spin plays the wheel sits on its digit at once: the spin is the motion.
        // Only while this wheel still owes turns: once landed it rolls normally again.
        var spinning = spin && sp < Math.min(1500, 600 + 100 * i);
        var w = F.wheelStep(wheels[i].pos, 0, tgt, spinning ? 1e9 : step, tau);
        wheels[i].pos = w.pos;
        var left = spinning ? F.spinLeft(sp, i) : 0;
        var spinSpeed = spinning ? Math.max(0, F.spinLeft(sp - step, i) - left) : 0;
        var shown = (((w.pos - left) % 10) + 10) % 10;
        // A soft motion blur while a wheel turns quickly.
        var speed = w.speed + spinSpeed;
        var blur = speed > 0.3 ? Math.min(1.1, (speed - 0.3) * 0.9) : 0;
        blur = Math.round(blur * 10) / 10;
        if (blur !== lastBlur[i]) { cols[i].style.filter = blur ? "blur(" + blur.toFixed(1) + "px)" : ""; lastBlur[i] = blur; }
        cols[i].style.transform = "translateY(" + (-(10 - shown)).toFixed(3) + "em)";
        p *= 10;
      }
      el.setAttribute("aria-label", say("hero.tokens.aria", { n: F.num(Math.round(v), 0) }));
    }
    function target() {
      var t = shown.tokens, u = shown.usd;
      F.pendingExtras(extras, seqAt).forEach(function (e) { t += e.tokens; u += e.usd; });
      return { tokens: t, usd: u };
    }
    function paintMoney(u) {
      var m = "≈ " + money(u);
      if (m !== lastMoney) { lastMoney = m; $("hero-money-main").textContent = m; }
    }
    return {
      frame: function (dt) {
        var tg = target(), now = performance.now();
        // The counter always rolls, even with "reduce motion": it is the panel's main
        // reading, and a jump hides what changed.
        if (!disp) disp = { tokens: tg.tokens, usd: tg.usd };
        else if (fast) {
          // A range change lands at once; the wheels' spin (below) makes it visible.
          disp.tokens = tg.tokens; disp.usd = tg.usd; fast = false;
        } else {
          // Each update rolls the wheels up to the new value and settles like a mechanical
          // counter: quick at first, slowing as it lands (most of the way in ~1.5 s).
          var gapT = tg.tokens - disp.tokens, gapU = tg.usd - disp.usd;
          var step = 1 - Math.exp(-dt / 650);
          disp.tokens += gapT * step;
          disp.usd += gapU * step;
          if (Math.abs(tg.tokens - disp.tokens) < 0.5) disp.tokens = tg.tokens;
          if (Math.abs(tg.usd - disp.usd) < 0.0005) disp.usd = tg.usd;
          if (gapT < 0 && Math.abs(gapT) > 1e6) { disp.tokens = tg.tokens; disp.usd = tg.usd; }
        }
        render(disp.tokens, now, dt);
        paintMoney(disp.usd);
      },
      set: function (tokens, usd, at, keep) {
        shown.tokens = tokens; shown.usd = usd; seqAt = at || 0;
        // Live calls count only for a range that reaches now; the reels spin only on a reset.
        extras = keep && includesNow() ? F.pendingExtras(extras, seqAt) : [];
        // One reel spin per change (range or source): a fresh answer that follows the
        // snapshot painted for the same change just rolls to its value.
        if (!keep && spunEpoch !== S.epoch) {
          spunEpoch = S.epoch;
          fast = true;
          if (disp) spin = { t0: performance.now() };
        }
      },
      clear: function () { extras = []; },
      bump: function (ev) { extras = F.addExtra(extras, ev); },
      pending: function () { return F.pendingExtras(extras, seqAt); },
      paint: function () { lastMoney = ""; },
      // A language switch: the drums are rebuilt with the new thousands separator.
      relabel: function () { digits = 0; lastMoney = ""; }
    };
  })();

  // -------------------------------------------------------------------------------------
  // Canvas helpers
  // -------------------------------------------------------------------------------------
  function fit(canvas) {
    var r = canvas.getBoundingClientRect();
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr; canvas.height = h * dpr;
    }
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }
  function rgba(color, a) {
    // Accepts #rrggbb; anything else falls back to the colour itself.
    var m = /^#([0-9a-f]{6})$/i.exec(color || "");
    if (!m) return color;
    var n = parseInt(m[1], 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }
  function hash(str) { var h = 2166136261; for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967295; }

  // -------------------------------------------------------------------------------------
  // Ecosystem: projects as living cells
  // -------------------------------------------------------------------------------------
  var eco = (function () {
    var canvas = $("eco"), labelsEl = $("labels"), floatersEl = $("floaters");
    var blobs = {}, order = [], sparks = [], rings = [], dust = [];
    var W = 0, H = 0, ctx = null, hover = null, frame = 0, drag = null;
    // L-176, phone: the canvas reaches down behind the account cards (its full height FH),
    // while the cells keep to the field above them (H). Sparks fly on a canvas above the
    // cards (sky), faint while they cross a card.
    var FH = 0, sky = $("sky"), sctx = null, skyOn = false;
    // Small cells: they do not lean away from the pointer, and their hit area is never
    // smaller than a comfortable target (screen pixels).
    var LEAN_MIN_R = 24, MIN_HIT_R = 16;
    // Camera: zoom z around the world point (x, y) shown at the canvas centre. At z = 1 it
    // is the identity. World = the layout the physics runs in; screen = canvas pixels.
    var cam = { z: 1, x: 0, y: 0 }, zoomNow = 1, pan = null, pinch = null, touches = {};
    var lastPageScroll = 0, lastScreen = null;
    window.addEventListener("scroll", function () {
      lastPageScroll = Date.now();
      // The canvas moved under a still mouse: its last position no longer names a cell.
      if (!drag && lastScreen) {
        lastScreen = null; pointer = { x: -1e4, y: -1e4 };
        if (hover) { hover = null; canvas.style.cursor = "default"; }
      }
    }, { passive: true });
    var ZMAX = 12;
    function sx(x) { return (x - cam.x) * cam.z + W / 2; }
    function sy(y) { return (y - cam.y) * cam.z + H / 2; }
    function wx(x) { return (x - W / 2) / cam.z + cam.x; }
    function wy(y) { return (y - H / 2) / cam.z + cam.y; }
    function zoomAt(z, px, py) {
      z = Math.max(1, Math.min(ZMAX, z));
      var ax = wx(px), ay = wy(py);         // keep the world point under the pointer still
      cam.z = z;
      if (z <= 1.001) { cam.z = 1; cam.x = W / 2; cam.y = H / 2; }
      else { cam.x = ax - (px - W / 2) / z; cam.y = ay - (py - H / 2) / z; clampCam(); }
      paintZoom();
      if (lastScreen) {
        // The pointer did not move but the world under it did: hover what is there now.
        pointer = { x: wx(lastScreen.x), y: wy(lastScreen.y) };
        var hb = at(pointer.x, pointer.y);
        if (hb !== hover) { hover = hb; canvas.style.cursor = hb ? "grab" : "default"; }
      }
      placeLabels(true);
    }
    function clampCam() {
      var hw = W / 2 / cam.z, hh = H / 2 / cam.z;
      cam.x = Math.max(hw, Math.min(W - hw, cam.x));
      cam.y = Math.max(hh, Math.min(H - hh, cam.y));
    }
    var zoomBtn = $("zoom-reset");
    function paintZoom() {
      zoomBtn.hidden = cam.z <= 1;
      document.querySelector(".stage").classList.toggle("zoomed", cam.z > 1);
      zoomBtn.style.top = (canvas.offsetTop + (H || canvas.offsetHeight) - 48) + "px";  // the field's bottom (audit of 95)
      zoomBtn.style.left = (canvas.offsetLeft + canvas.offsetWidth / 2) + "px";
      zoomBtn.textContent = say("zoom.back", { z: F.num(cam.z, 1) });
    }
    zoomBtn.addEventListener("click", function () { zoomAt(1, W / 2, H / 2); });
    // Bigger than this on screen and a cell is "behind you": it darkens and drops its label.
    function bigness(b) {
      var m = Math.min(W, H), R = b.r * cam.z;
      return Math.max(0, Math.min(1, (R - 0.3 * m) / (0.3 * m)));
    }
    // Every cell has a colour of its own: a palette slot for the top spenders, a stable hue
    // from its name for the rest. How much of it shows is up to its freshness (draw()), not
    // its size.
    function cellColor(b) { return projectColor(b.name); }
    // While the afterglow lasts, a ring in the cell's colour hugs its edge (fading with it).
    function afterRim(b, ag, acol, a) {
      if (!(ag > 0.01) || !acol) return;
      ctx.lineWidth = 1.5 + 2.5 * ag;
      ctx.strokeStyle = rgba(acol, Math.min(1, 0.25 + ag) * a);
      ctx.beginPath(); ctx.arc(b.x, b.y, b.r + 3 / cam.z, 0, 6.283); ctx.stroke();
    }
    var pointer = { x: -1e4, y: -1e4 };

    function center() {
      var phone = window.innerWidth <= 760;
      if (phone) return { x: W * 0.5, y: H * 0.5 };
      // Between the left column and the account cards on the right.
      var q = quotaRect();
      return { x: q.w ? (W * 0.3 + q.x - 10) / 2 : W * 0.56, y: H * 0.47 };
    }

    function newBlob(key) {
      var c = center(), a = hash(key) * Math.PI * 2, d = 40 + hash(key + "d") * Math.min(W, H) * 0.35;
      var b = { name: key, x: c.x + Math.cos(a) * d, y: c.y + Math.sin(a) * d, vx: 0, vy: 0,
                r: 0, target: 0, alpha: 0, pulse: 0, phase: hash(key) * 1000, seeds: [], moons: [], el: null };
      for (var k = 0; k < 12; k++) b.seeds.push([hash(key + k) * 6.28, 0.6 + hash(key + "s" + k) * 0.9]);
      return b;
    }
    // Cursor projects: their own cells (key "cursor:<name>"), sized by turns on their own
    // scale (Cursor keeps no cost since Feb 2026), drawn as dashed rings.
    function syncCursor(projects) {
      var seen = {};
      (projects || []).forEach(function (p) {
        var key = "cursor:" + p.name;
        seen[key] = 1;
        var b = blobs[key] || (blobs[key] = newBlob(key));
        b.cursor = true; b.p = p; b.dead = false;
        b.moons = (p.tasks || []).slice(0, 9).map(function (t, j) {
          var old = b.moons[j] || {};
          return { t: t, a: old.a != null ? old.a : hash(t.key) * 6.28, d: 0.28 + 0.5 * hash(t.key + "d"),
                   s: (0.25 + hash(t.key + "s")) * (hash(t.key + "w") > 0.5 ? 1 : -1),
                   // Sized by cost (from the API); by activity only where no cost exists.
                   share: p.usd ? t.usd / p.usd : ((p.turns + p.requests) ? (t.turns + t.requests) / (p.turns + p.requests) : 0) };
        });
      });
      Object.keys(blobs).forEach(function (k) { if (blobs[k].cursor && !seen[k]) blobs[k].dead = true; });
      order = Object.keys(blobs).map(function (k) { return blobs[k]; });
      rescale();
      syncLabels();
    }

    function sync(projects) {
      var seen = {};
      var total = 0;
      projects.forEach(function (p) { total += p.usd; });
      projects.forEach(function (p, i) {
        seen[p.name] = 1;
        var b = blobs[p.name];
        if (!b) {
          var c = center(), a = hash(p.name) * Math.PI * 2, d = 40 + hash(p.name + "d") * Math.min(W, H) * 0.35;
          b = blobs[p.name] = {
            name: p.name, x: c.x + Math.cos(a) * d, y: c.y + Math.sin(a) * d, vx: 0, vy: 0,
            r: 0, target: 0, alpha: 0, pulse: 0, phase: hash(p.name) * 1000,
            seeds: [], moons: [], el: null
          };
          for (var k = 0; k < 12; k++) b.seeds.push([hash(p.name + k) * 6.28, 0.6 + hash(p.name + "s" + k) * 0.9]);
        }
        b.p = p; b.share = total ? p.usd / total : 0; b.dead = false; b.rank = i;
        b.moons = (p.tasks || []).slice(0, 9).map(function (t, j) {
          var old = b.moons[j] || {};
          return { t: t, a: old.a != null ? old.a : hash(t.key) * 6.28, d: 0.28 + 0.5 * hash(t.key + "d"),
                   s: (0.25 + hash(t.key + "s")) * (hash(t.key + "w") > 0.5 ? 1 : -1), share: p.usd ? t.usd / p.usd : 0 };
        });
      });
      Object.keys(blobs).forEach(function (k) {
        if (!seen[k] && !blobs[k].cursor && !(blobs[k].liveUntil > Date.now())) blobs[k].dead = true;
      });
      order = Object.keys(blobs).map(function (k) { return blobs[k]; });
      rescale();
      syncLabels();
    }

    // Every cell with a cost shares one scale (area = cost), Cursor's included, so they
    // compare honestly. Cursor cells with no recorded cost (older local history only) are
    // sized by turns on a small scale of their own.
    function rescale() {
      var sum = 0, turns = 0;
      order.forEach(function (b) {
        if (b.dead) return;
        if (b.cursor && !b.p.usd) turns += (b.p.turns || 0) + (b.p.requests || 0);
        else sum += b.p.usd;
      });
      var phone = window.innerWidth <= 760;
      var area = W * H * (phone ? 0.30 : 0.24);
      var k = sum ? Math.sqrt(area / (Math.PI * sum)) : 0;
      var kc = turns ? Math.sqrt(W * H * 0.03 / (Math.PI * turns)) : 0;
      var maxR = Math.min(W, H) * (phone ? 0.30 : 0.26);
      order.forEach(function (b) {
        var liveUsd = !b.cursor && liveBy[b.name] ? liveBy[b.name].usd : 0;
        var v = b.cursor && !b.p.usd ? kc * Math.sqrt((b.p.turns || 0) + (b.p.requests || 0)) : k * Math.sqrt(b.p.usd + liveUsd);
        b.target = b.dead ? 0 : Math.max(5, Math.min(maxR, v));
      });
    }

    var lwDesc = Object.getOwnPropertyDescriptor(CanvasRenderingContext2D.prototype, "lineWidth");
    function resize() {
      if (window.innerWidth <= 760) {
        // The canvases start where the cells' room starts (under the hero).
        var fe = document.querySelector(".stage .field");
        if (fe) canvas.parentNode.style.setProperty("--field-top", fe.offsetTop + "px");
      }
      var f = fit(canvas); ctx = f.ctx;
      if (lwDesc && !ctx.__zoomLW) {
        // Strokes keep their on-screen width whatever the zoom.
        Object.defineProperty(ctx, "lineWidth", { configurable: true,
          set: function (v) { lwDesc.set.call(this, v / zoomNow); },
          get: function () { return lwDesc.get.call(this) * zoomNow; } });
        ctx.__zoomLW = true;
      }
      var grew = !W;
      W = f.w; H = FH = f.h;
      skyOn = !!sky && window.innerWidth <= 760;
      if (skyOn) {
        var fe = document.querySelector(".stage .field");
        if (fe && fe.offsetHeight > 100) H = Math.min(FH, fe.offsetHeight);
        sctx = fit(sky).ctx;
        if (lwDesc && !sctx.__zoomLW) {
          Object.defineProperty(sctx, "lineWidth", { configurable: true,
            set: function (v) { lwDesc.set.call(this, v / zoomNow); },
            get: function () { return lwDesc.get.call(this) * zoomNow; } });
          sctx.__zoomLW = true;
        }
      } else if (sctx) { sctx.clearRect(0, 0, sky.width, sky.height); sctx = null; }
      if (!dust.length) for (var i = 0; i < (REDUCED ? 0 : 70); i++) dust.push({ x: Math.random(), y: Math.random(), z: 0.2 + Math.random() * 0.8, s: Math.random() * 6.28 });
      rescale();
      if (grew) order.forEach(function (b) { var c = center(); b.x = c.x + (b.x - c.x); });
      if (cam.z <= 1) { cam.x = W / 2; cam.y = H / 2; } else clampCam();
      paintZoom();
    }

    // --- labels: DOM, placed each frame, collisions resolved every few frames ----------
    function syncLabels() {
      order.forEach(function (b) {
        if (!b.el) {
          b.el = document.createElement("div");
          b.el.className = "label";
          labelsEl.appendChild(b.el);
        }
        if (b.dead) return;
        var p = b.p;
        var top = p.tasks && p.tasks[0];
        b.el.innerHTML = "";
        b.el.classList.toggle("cursor", !!b.cursor);
        var name = document.createElement("b");
        var dot = document.createElement("i"); dot.className = "dot"; dot.style.background = b.cursor ? cursorTint(b.name) : cellColor(b);
        name.appendChild(dot); name.appendChild(document.createTextNode(b.cursor ? "Cursor · " + cursorName(p) : projName(p.name)));
        var line = document.createElement("span");
        b.lineEl = line;
        paintLine(b);
        var extra = document.createElement("div"); extra.className = "extra";
        extra.textContent = b.cursor
          ? many("n.convs", p.convs) +
            (top ? " · " + say(top.usd > 0 ? "label.costliest" : "label.longest", { title: trim(cursorTitle(top), 30) }) : "")
          : many("n.sessions", p.sessions) + " · " +
            (top ? say("label.costliest", { title: trim(top.ticket || top.title || say("untitled"), 34) }) : "");
        b.el.appendChild(name); b.el.appendChild(line); b.el.appendChild(extra);
      });
    }
    function trim(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
    // Live calls not yet in the summary, per project: the labels count them as they play.
    var liveBy = {};
    function paintLine(b) {
      if (!b.lineEl || !b.p) return;
      if (b.cursor) {
        b.lineEl.textContent = b.p.usd ? money(b.p.usd) + " · " + F.tokens(b.p.tokens) : F.cursorCount(b.p);
        return;
      }
      var l = liveBy[b.name] || { tokens: 0, usd: 0 };
      b.lineEl.textContent = money(b.p.usd + l.usd) + " · " + F.tokens(b.p.tokens + l.tokens);
    }
    function live(map) {
      liveBy = map || {};
      order.forEach(function (b) { paintLine(b); });
      rescale();
    }

    var placed = [];
    function placeLabels(full) {
      var phone = window.innerWidth <= 760;
      if (full && cam.z > 1) paintZoom();  // the canvas may have moved (hero height changed)
      var minR = phone ? 22 : 26;
      var maxCount = (phone ? 6 : 11) + Math.floor((cam.z - 1) * 5);
      var ox = canvas.offsetLeft, oy = canvas.offsetTop;
      if (full) {
        placed = [];
        var m = Math.min(W, H);
        var cand = order.filter(function (b) { return !b.dead && b.el; }).sort(function (a, b) {
          // Zoomed in, the cells that fit the screen come first, the huge ones last.
          return (bigness(a) - bigness(b)) || (b.r - a.r);
        });
        var heroBox = heroRect(), qBox = quotaRect();
        var chipBox = zoomBtn.hidden ? null : rectIn(zoomBtn, true);
        var shown = 0;
        cand.forEach(function (b) {
          var X = sx(b.x), Y = sy(b.y), R = b.r * cam.z;
          var onScreen = X + R > 0 && X - R < W && Y + R > 0 && Y - R < H;
          var tooBig = R > 0.45 * m && cam.z > 1;
          var show = b === hover || (onScreen && !tooBig && shown < maxCount && R >= minR);
          var w = b.el.offsetWidth || 120, h = b.el.offsetHeight || 34;
          var x = X - w / 2, y = Y - R - h - 8;
          // Big cells carry their label inside; small ones above (or below near the top).
          if (R > Math.max(w * 0.62, 56)) y = Math.max(4, Math.min(H - h - 4, Y - h / 2));
          else if (y < 4) y = Y + R + 8;
          if (y + h < 0 || y > H) show = false;  // a label that would sit off screen
          x = Math.max(4, Math.min(W - w - 4, x));
          var box = { x: x, y: y, w: w, h: h };
          if (show && b !== hover) {
            for (var j = 0; j < placed.length; j++) if (hit(box, placed[j])) { show = false; break; }
            if (show && !phone && (hit(box, heroBox) || hit(box, qBox))) show = false;
            if (show && chipBox && hit(box, chipBox)) show = false;
          }
          b.offX = x - X; b.offY = y - Y; b.lw = w; b.show = show;
          if (show) { placed.push(box); shown++; }
        });
      }
      order.forEach(function (b) {
        if (!b.el) return;
        if (b.dead && b.r < 1) { b.el.remove(); b.el = null; return; }
        var vis = b.show && !b.dead && b.r > 3;
        b.el.style.opacity = vis ? "1" : "0";
        b.el.classList.toggle("hot", b === hover);
        if (vis && b.offX != null) {
          // Follow the cell every frame; collisions are re-solved every few frames.
          var x = Math.max(4, Math.min(W - (b.lw || 0) - 4, sx(b.x) + b.offX)), y = sy(b.y) + b.offY;
          b.el.style.transform = "translate3d(" + Math.round(ox + x) + "px," + Math.round(oy + y) + "px,0)";
        }
      });
    }
    function hit(a, b) { return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y; }
    function rectIn(el, always) {
      if (!el || (!always && window.innerWidth <= 760)) return { x: -1, y: -1, w: 0, h: 0 };
      var r = el.getBoundingClientRect(), c = canvas.getBoundingClientRect();
      return { x: r.left - c.left, y: r.top - c.top, w: r.width, h: r.height };
    }
    var heroEl = document.querySelector(".hero .big");
    // The hero's real extent: its card while zoomed (eyebrow and padding included), and
    // never narrower than the number itself, which can be wider than its box.
    function heroRect() {
      var big = rectIn(heroEl), m = rectIn(document.querySelector(".hero .ecg"));
      var numRight = big.x + Math.max(big.w, heroEl.scrollWidth);
      if (cam.z > 1) {
        var card = rectIn(document.querySelector(".hero"));
        return { x: card.x, y: card.y, w: Math.max(card.w, numRight - card.x), h: card.h };
      }
      return { x: big.x, y: big.y, w: Math.max(numRight - big.x, m.w), h: (m.y + m.h) - big.y };
    }
    var quotaEl = $("quota");
    // On the phone the cells' field ends where the cards begin, so it follows their height.
    if (window.ResizeObserver) {
      var qh = -1;
      new ResizeObserver(function () {
        var h = quotaEl.hidden ? 0 : quotaEl.offsetHeight;
        if (h !== qh) { qh = h; if (window.innerWidth <= 760 && W) resize(); }
      }).observe(quotaEl);
      var lh = -1, leftCol = document.querySelector(".left-col");
      if (leftCol) new ResizeObserver(function () {
        if (leftCol.offsetHeight !== lh) { lh = leftCol.offsetHeight; if (window.innerWidth <= 760 && W) resize(); }
      }).observe(leftCol);
    }
    function quotaRect() { return quotaEl.hidden ? { x: -1, y: -1, w: 0, h: 0 } : rectIn(quotaEl); }
    // Where a live call starts: the card of the Claude account that made it (L-122: a chat
    // keeps the account it started on, so it may be the outgoing one), the active card
    // when the call's account is unknown, or Cursor's card for a Cursor call (its left
    // edge on the desktop, its top on the phone, where the cards sit under the cells).
    // Null when the cards are not shown. The card beats as the spark leaves it.
    function sparkOrigin(ev) {
      if (quotaEl.hidden) return null;
      var card = ev.cursor ? quotaEl.querySelector('.qcard[data-kind="cursor"]') : accountCardOf(ev.account);
      if (!card || card.hidden) return null;
      var r = rectIn(card, true), phone = window.innerWidth <= 760;
      card.classList.remove("fire"); void card.offsetWidth; card.classList.add("fire");
      return phone ? { x: r.x + r.w / 2, y: r.y } : { x: r.x, y: r.y + Math.min(r.h / 2, 26) };
    }
    // 0 for a call of 10k tokens or less, 1 for 1M or more, on a log scale in between
    // (a call's tokens include cache reads, so most land between 20k and 200k).
    function sparkPower(tokens) {
      return Math.max(0, Math.min(1, (Math.log10(1 + (tokens || 0)) - 4) / 2));
    }
    function sparkColor(name) {
      var b = blobs[name];
      return b && b.cursor ? cursorTint(name) : projectColor(name);
    }

    // --- physics ------------------------------------------------------------------------
    function step(dt, t) {
      var f = Math.min(dt / 16.7, 3);
      // Cells drift under a still pointer: hover what is under it now, every frame, so the
      // cell you are about to click is the one held still.
      if (!drag && !pan && lastScreen) {
        pointer = { x: wx(lastScreen.x), y: wy(lastScreen.y) };
        var hv = belowField(lastScreen.y) ? null : at(pointer.x, pointer.y);
        if (hv !== hover) { hover = hv; canvas.style.cursor = hv ? "grab" : "default"; placeLabels(true); }
      }
      var c = center();
      var hb = heroRect(), qb = quotaRect();
      for (var i = 0; i < order.length; i++) {
        var b = order[i];
        b.r += (b.target - b.r) * (REDUCED ? 1 : 0.06 * f);
        if (drag && drag.b === b) {
          // Held: a stiff spring to the pointer; it keeps its mass, so it shoves the rest.
          b.alpha += (1 - b.alpha) * 0.06 * f;
          b.vx += (drag.tx - b.x) * 0.16 * f;
          b.vy += (drag.ty - b.y) * 0.16 * f;
          b.vx *= Math.pow(0.7, f); b.vy *= Math.pow(0.7, f);
          b.pulse *= Math.pow(0.93, f);
          continue;
        }
        b.alpha += ((b.dead ? 0 : 1) - b.alpha) * 0.06 * f;
        b.pulse *= Math.pow(0.93, f);
        var kx = 0.0009, ky = 0.0009 * Math.max(1, W / H) * 0.9;
        if (b === hover) {
          // Anchored under the pointer: no pull to the centre, no wander, its own speed
          // damped. Pulled to the centre it pressed into its neighbours, which yield to it
          // in collisions, and slid out from under the click (audit of 70).
          b.vx *= Math.pow(0.6, f); b.vy *= Math.pow(0.6, f);
        } else {
          b.vx += (c.x - b.x) * kx * f;
          b.vy += (c.y - b.y) * ky * f;
        }
        if (!REDUCED && b !== hover) {
          b.vx += Math.cos(t * 0.00021 + b.phase) * 0.018 * f;
          b.vy += Math.sin(t * 0.00017 + b.phase * 1.3) * 0.018 * f;
        }
        // Keep clear of the hero numbers and the account cards.
        if (cam.z <= 1) [hb, qb].forEach(function (box) {
          if (box.w <= 0) return;
          var nx = Math.max(box.x, Math.min(b.x, box.x + box.w)), ny = Math.max(box.y, Math.min(b.y, box.y + box.h));
          var dx = b.x - nx, dy = b.y - ny, d = Math.sqrt(dx * dx + dy * dy) || 0.01;
          var min = b.r + 14;
          if (d < min) { var push = (min - d) * 0.04 * f; b.vx += dx / d * push; b.vy += dy / d * push; }
        });
        // Pointer: cells lean away a little, as if touched. A small cell stays put: it is
        // a hard target already, and it would slip away before the pointer reached it.
        var px = b.x - pointer.x, py = b.y - pointer.y, pd = Math.sqrt(px * px + py * py);
        if (b.r * cam.z >= LEAN_MIN_R && pd < b.r + 60 / cam.z && pd > 0.1 && b !== hover) { b.vx += px / pd * 0.25 * f; b.vy += py / pd * 0.25 * f; }
      }
      for (i = 0; i < order.length; i++) {
        var a = order[i];
        for (var j = i + 1; j < order.length; j++) {
          var o = order[j];
          var dx2 = o.x - a.x, dy2 = o.y - a.y, d2 = Math.sqrt(dx2 * dx2 + dy2 * dy2) || 0.01;
          var m2 = a.r + o.r + 10;
          if (d2 < m2) {
            var p2 = (m2 - d2) / d2 * 0.22 * f;
            var wa = o.r / (a.r + o.r + 0.01), wo = 1 - wa;
            // The held cell barely yields: the others get knocked out of its way.
            if (drag && drag.b === a) { wa = 0.08; wo = 0.92 * 1.6; }
            else if (drag && drag.b === o) { wo = 0.08; wa = 0.92 * 1.6; }
            // The cell under the pointer barely yields either, so a neighbour knocked by a
            // spark cannot push it out from under a click (L-135).
            else if (a === hover) { wa = 0.04; wo = 1; }
            else if (o === hover) { wo = 0.04; wa = 1; }
            a.vx -= dx2 * p2 * wa; a.vy -= dy2 * p2 * wa;
            o.vx += dx2 * p2 * wo; o.vy += dy2 * p2 * wo;
          }
        }
      }
      for (i = 0; i < order.length; i++) {
        var q = order[i];
        var damp = Math.pow(0.86, f);
        q.vx *= damp; q.vy *= damp;
        q.x += q.vx * f; q.y += q.vy * f;
        q.x = Math.max(q.r + 4, Math.min(W - q.r - 4, q.x));
        q.y = Math.max(q.r + 4, Math.min(H - q.r - 4, q.y));
        q.moons.forEach(function (m) { m.a += m.s * 0.0009 * dt * (1 + S.activity); });
      }
      order = order.filter(function (b) {
        if (!(b.dead && b.r < 0.6)) return true;
        if (b.el) { b.el.remove(); b.el = null; }
        delete blobs[b.name];
        return false;
      });
      S.activity *= Math.pow(0.995, f);
    }

    // --- drawing ------------------------------------------------------------------------
    function blobPath(b, t) {
      var n = b.seeds.length, pts = [];
      var wob = REDUCED ? 0 : (0.045 + 0.05 * Math.min(1, b.pulse * cam.z / 8) + 0.025 * S.activity);
      var r = b.r + b.pulse;
      for (var i = 0; i < n; i++) {
        var ang = i / n * Math.PI * 2;
        var s = b.seeds[i];
        var k = 1 + wob * (Math.sin(t * 0.0011 * s[1] + s[0]) * 0.65 + Math.sin(t * 0.0023 * s[1] + s[0] * 2.1) * 0.35);
        pts.push([b.x + Math.cos(ang) * r * k, b.y + Math.sin(ang) * r * k]);
      }
      ctx.beginPath();
      var last = pts[n - 1], first = pts[0];
      ctx.moveTo((last[0] + first[0]) / 2, (last[1] + first[1]) / 2);
      for (i = 0; i < n; i++) {
        var p = pts[i], q = pts[(i + 1) % n];
        ctx.quadraticCurveTo(p[0], p[1], (p[0] + q[0]) / 2, (p[1] + q[1]) / 2);
      }
      ctx.closePath();
    }

    // Zoomed past a cell: a dark veil over it, so the small ones in front stand out.
    function veil(b, big, t) {
      if (big <= 0.01) return;
      var mode = ctx.globalCompositeOperation;
      ctx.globalCompositeOperation = "source-over";
      blobPath(b, t);
      ctx.fillStyle = "rgba(6,8,12," + (0.5 * big * b.alpha) + ")";
      ctx.fill();
      ctx.globalCompositeOperation = mode;
    }

    function draw(t) {
      var T = S.theme;
      zoomNow = 1;
      ctx.setTransform(canvas.width / W, 0, 0, canvas.width / W, 0, 0);
      ctx.clearRect(0, 0, W, FH);
      // Dust: depth, slow parallax with the pointer (on the phone, behind the cards too).
      if (dust.length) {
        ctx.fillStyle = rgba(T.ink, T.dark ? 0.22 : 0.10);
        dust.forEach(function (d) {
          var x = (d.x * W + Math.sin(t * 0.00005 * d.z + d.s) * 30 * d.z + (pointer.x > -1e3 ? (pointer.x - W / 2) * 0.01 * d.z : 0) + W) % W;
          var y = (d.y * FH + t * 0.004 * d.z) % FH;
          ctx.beginPath(); ctx.arc(x, y, 0.6 + d.z * 0.9, 0, 6.283); ctx.fill();
        });
      }
      var dpr = canvas.width / W;
      // On the phone the canvas runs on under the cards: the cells, their auras and rings
      // stay inside the field (audit of 96); only the dust is drawn below it.
      var clipped = skyOn && H < FH;
      if (clipped) { ctx.save(); ctx.beginPath(); ctx.rect(0, 0, W, H); ctx.clip(); }
      zoomNow = cam.z;
      ctx.setTransform(dpr * cam.z, 0, 0, dpr * cam.z, dpr * (W / 2 - cam.x * cam.z), dpr * (H / 2 - cam.y * cam.z));
      ctx.globalCompositeOperation = T.blend;
      order.forEach(function (b) {
        if (b.r < 0.5) return;
        var big = bigness(b);
        var hitAge = b.hitT ? t - b.hitT : 1e9;
        var ag = F.afterglow(hitAge), acol = null;
        if (ag > 0) {
          // Aura: a glow in the cell's colour that swells out on the hit, then lingers as a
          // slow tail (~10 s), so the cell that received the tokens is easy to find.
          var g = Math.exp(-hitAge / 750);
          acol = b.cursor ? cursorTint(b.name) : cellColor(b);
          var outer = b.r * (1.25 + 0.9 * (1 - g)) + 18 / cam.z;
          var au = ctx.createRadialGradient(b.x, b.y, b.r * 0.85, b.x, b.y, outer);
          au.addColorStop(0, rgba(acol, (T.dark ? 0.75 : 0.55) * ag));
          au.addColorStop(0.55, rgba(acol, (T.dark ? 0.35 : 0.25) * ag));
          au.addColorStop(1, rgba(acol, 0));
          var mode0 = ctx.globalCompositeOperation;
          ctx.globalCompositeOperation = T.dark ? "lighter" : "source-over";
          ctx.fillStyle = au;
          ctx.beginPath(); ctx.arc(b.x, b.y, outer, 0, 6.283); ctx.fill();
          ctx.globalCompositeOperation = mode0;
        }
        // Freshness: cells that received tokens lately are vivid and glossy with a stronger
        // halo; as they go quiet they turn pastel (light theme) or matte (dark), lose their
        // shine and their halo dims. Hours, not seconds: it reads at a glance which ones live.
        // A cell with no call for a week is fully grey (L-141).
        var stale = F.staleness(Math.max(b.p && b.p.last || 0, b.liveT || 0), Date.now()), fr = 1 - stale;
        var col = b.cursor ? cursorTint(b.name) : cellColor(b), a = b.alpha * (1 - 0.45 * big), fl = b.flash || 0;
        // Cursor cells keep a trace of their hue, so they still read as Cursor when idle.
        col = mixHex(col, T.dark ? "#4a4c52" : "#d6d3ce", (b.cursor ? 0.85 : 1) * stale);
        var haloK = 0.45 + 0.75 * fr;
        // A hit makes the cell shudder for ~0.6 s: a damped vibration of its drawing only
        // (the physics position is untouched).
        var hx = 0, hy = 0, since = b.hitT ? t - b.hitT : 1e9;
        if (since < 650 && !REDUCED) {
          var damp = Math.exp(-since / 170) * (b.hitAmp || 0.6) * Math.min(9, 3 + b.r * cam.z * 0.07) / cam.z;
          hx = Math.sin(since / 19) * damp;
          hy = Math.cos(since / 23) * damp * 0.7;
        }
        b.x += hx; b.y += hy;
        if (b.cursor) {
          // Cursor: solid cells of their own colour, a diagonal sheen and a double rim, so
          // they read as real usage next to Claude Code's cells and are told apart at once.
          var ch = ctx.createRadialGradient(b.x, b.y, b.r * 0.3, b.x, b.y, b.r * 1.8 + b.pulse * 2);
          ch.addColorStop(0, rgba(col, (0.14 * haloK + 0.5 * fl) * T.glow * a)); ch.addColorStop(1, rgba(col, 0));
          ctx.fillStyle = ch; ctx.beginPath(); ctx.arc(b.x, b.y, b.r * 1.8 + b.pulse * 2, 0, 6.283); ctx.fill();
          blobPath(b, t);
          var cg = ctx.createLinearGradient(b.x - b.r, b.y - b.r, b.x + b.r, b.y + b.r);
          // A deep, see-through body (what is behind shows through); the rim keeps the
          // bright Cursor colour so the cell still reads as Cursor at a glance.
          var deep = mixHex(T.cursorDeep || col, col, 0.72);
          cg.addColorStop(0, rgba(deep, 0.6 * a));
          cg.addColorStop(0.55, rgba(deep, 0.42 * a));
          cg.addColorStop(1, rgba(deep, 0.26 * a));
          ctx.fillStyle = cg; ctx.fill();
          ctx.lineWidth = b === hover ? 3 : 2;
          ctx.strokeStyle = rgba(col, 0.9 * a);
          ctx.stroke();
          ctx.save(); ctx.clip();
          ctx.fillStyle = "rgba(255,255,255," + ((0.03 + 0.1 * fr) * a) + ")";
          ctx.beginPath();
          ctx.moveTo(b.x - b.r * 1.2, b.y - b.r * 0.2); ctx.lineTo(b.x + b.r * 0.2, b.y - b.r * 1.2);
          ctx.lineTo(b.x + b.r * 0.55, b.y - b.r * 1.2); ctx.lineTo(b.x - b.r * 1.2, b.y + b.r * 0.15);
          ctx.closePath(); ctx.fill();
          ctx.restore();
          ctx.lineWidth = 1;
          ctx.strokeStyle = "rgba(255,255,255," + ((0.5 + 0.5 * fl) * a) + ")";
          ctx.beginPath(); ctx.arc(b.x, b.y, Math.max(2, b.r * 0.82), 0, 6.283); ctx.stroke();
          afterRim(b, ag, acol, a);
          veil(b, big, t);
          b.x -= hx; b.y -= hy;
          return;
        }
        b.flash = fl * 0.955;
        // Halo (brighter for a moment when a live call lands)
        var halo = ctx.createRadialGradient(b.x, b.y, b.r * 0.4, b.x, b.y, b.r * 1.9 + b.pulse * 2);
        halo.addColorStop(0, rgba(col, (0.22 * haloK + 0.55 * fl) * T.glow * a));
        halo.addColorStop(1, rgba(col, 0));
        ctx.fillStyle = halo;
        ctx.beginPath(); ctx.arc(b.x, b.y, b.r * 1.9 + b.pulse * 2, 0, 6.283); ctx.fill();
        // Body
        blobPath(b, t);
        var g = ctx.createRadialGradient(b.x - b.r * 0.3, b.y - b.r * 0.35, b.r * 0.1, b.x, b.y, b.r * 1.1);
        g.addColorStop(0, rgba(col, (T.dark ? 0.95 : 0.85) * a));
        g.addColorStop(0.7, rgba(col, (T.dark ? 0.55 : 0.6) * a));
        g.addColorStop(1, rgba(col, (T.dark ? 0.28 : 0.35) * a));
        ctx.fillStyle = g; ctx.fill();
        ctx.lineWidth = b === hover ? 2 : 1 + fl * 2;
        ctx.strokeStyle = rgba(col, (b === hover ? 0.95 : 0.55) * a);
        ctx.stroke();
        if (fl > 0.05) {
          // White-hot rim and a lit core while the hit lasts.
          ctx.lineWidth = 2 + fl * 4;
          ctx.strokeStyle = "rgba(255,255,255," + (0.85 * fl * a) + ")";
          if (T.dark) { ctx.shadowColor = col; ctx.shadowBlur = 24 * fl; }
          ctx.stroke();
          ctx.shadowBlur = 0;
          var core = ctx.createRadialGradient(b.x, b.y, 0, b.x, b.y, b.r * 0.9);
          core.addColorStop(0, "rgba(255,255,255," + (0.55 * fl * a) + ")");
          core.addColorStop(1, "rgba(255,255,255,0)");
          ctx.fillStyle = core;
          ctx.beginPath(); ctx.arc(b.x, b.y, b.r * 0.9, 0, 6.283); ctx.fill();
        }
        if (fr > 0.02) {
          // Gloss: a wet highlight on fresh cells; old ones stay matte. Drawn
          // after the flash, which strokes the current path (the cell outline).
          var sx0 = b.x - b.r * 0.36, sy0 = b.y - b.r * 0.4;
          var sh = ctx.createRadialGradient(sx0, sy0, 0, sx0, sy0, b.r * 0.5);
          sh.addColorStop(0, "rgba(255,255,255," + ((T.dark ? 0.32 : 0.5) * fr * a) + ")");
          sh.addColorStop(1, "rgba(255,255,255,0)");
          ctx.fillStyle = sh;
          ctx.beginPath(); ctx.arc(sx0, sy0, b.r * 0.5, 0, 6.283); ctx.fill();
        }
        afterRim(b, ag, acol, a);
        veil(b, big, t);
        b.x -= hx; b.y -= hy;
      });
      ctx.globalCompositeOperation = "source-over";
      // Moons: tasks orbiting inside each cell.
      order.forEach(function (b) {
        if (b.r < 14) return;
        b.moons.forEach(function (m) {
          var rr = Math.max(1.6, Math.min(b.r * 0.32, b.r * 0.55 * Math.sqrt(m.share)));
          var d = b.r * m.d * 0.9;
          var x = b.x + Math.cos(m.a) * d, y = b.y + Math.sin(m.a) * d;
          ctx.fillStyle = T.dark ? "rgba(255,255,255," + (0.32 * b.alpha) + ")" : "rgba(255,255,255," + (0.5 * b.alpha) + ")";
          ctx.beginPath(); ctx.arc(x, y, rr, 0, 6.283); ctx.fill();
        });
      });
      // Rings from live calls.
      rings = rings.filter(function (r) { return t - r.t0 < 1400; });
      rings.forEach(function (r) {
        if (t < r.t0) return;
        var k = (t - r.t0) / 1400, b = blobs[r.name];
        if (!b) return;
        var e = 1 - Math.pow(1 - k, 2);
        var amp = r.amp || 1;
        ctx.strokeStyle = rgba(sparkColor(b.name), (1 - k) * Math.min(0.95, 0.4 + amp * 0.45));
        ctx.lineWidth = ((4 * (1 - k)) * amp + 0.8) / cam.z;
        ctx.beginPath(); ctx.arc(b.x, b.y, b.r + (4 + e * 30 * amp) / cam.z + e * b.r * 0.5 * amp, 0, 6.283); ctx.stroke();
      });
      // Sparks: travelling from the edge into their cell; on the phone on the sky canvas,
      // above the cards, at 40 % while over one.
      var g = ctx, cards = null;
      if (skyOn && sctx) {
        g = sctx;
        g.setTransform(dpr, 0, 0, dpr, 0, 0);
        g.clearRect(0, 0, W, FH);
        g.setTransform(dpr * cam.z, 0, 0, dpr * cam.z, dpr * (W / 2 - cam.x * cam.z), dpr * (H / 2 - cam.y * cam.z));
        cards = quotaEl.hidden ? null : rectIn(quotaEl, true);
      }
      var veilOf = function (x, y) {
        if (!cards) return 1;
        var X = sx(x), Y = sy(y);
        return X >= cards.x && X <= cards.x + cards.w && Y >= cards.y && Y <= cards.y + cards.h ? 0.4 : 1;
      };
      g.globalCompositeOperation = T.blend;
      sparks = sparks.filter(function (s) { return !s.done; });
      sparks.forEach(function (s) {
        var b = blobs[s.name], tx = b ? b.x : cam.x, ty = b ? b.y : cam.y;
        var k = Math.min(1, (t - s.t0) / s.dur), e = 1 - Math.pow(1 - k, 3);
        var cx = (s.x0 + tx) / 2 + s.bend * 0.6, cy = (s.y0 + ty) / 2 - s.bend;
        var x = (1 - e) * (1 - e) * s.x0 + 2 * (1 - e) * e * cx + e * e * tx;
        var y = (1 - e) * (1 - e) * s.y0 + 2 * (1 - e) * e * cy + e * e * ty;
        s.trail.push([x, y]); if (s.trail.length > 18) s.trail.shift();
        var col = sparkColor(s.name);
        for (var i = 1; i < s.trail.length; i++) {
          g.strokeStyle = rgba(col, i / s.trail.length * 0.8 * veilOf(s.trail[i][0], s.trail[i][1]));
          g.lineWidth = 1 + i / s.trail.length * s.size;
          g.beginPath(); g.moveTo(s.trail[i - 1][0], s.trail[i - 1][1]); g.lineTo(s.trail[i][0], s.trail[i][1]); g.stroke();
        }
        g.globalAlpha = veilOf(x, y);
        g.fillStyle = T.dark ? "#fff" : col;
        g.beginPath(); g.arc(x, y, s.size, 0, 6.283); g.fill();
        g.globalAlpha = 1;
        if (k >= 1) {
          s.done = true;
          if (b) {
            var pw = s.power || 0;
            // In screen pixels, like the rings: a hit looks the same at any zoom.
            b.pulse = Math.min((REDUCED ? 14 : 26) / cam.z, b.pulse + (3 + pw * (REDUCED ? 6 : 17)) / cam.z);
            impact(b, s, pw);
            sound.hit({ project: s.name, power: pw, cursor: !!b.cursor, pan: sx(b.x) / W * 2 - 1 });
            b.flash = 1;
            b.hitT = t;
            b.liveT = Date.now();
            if (b.el) {
              // The cell's label pops and glows in its colour, so you see which one it was.
              b.el.style.setProperty("--hit", b.cursor ? cursorTint(b.name) : cellColor(b));
              b.el.classList.remove("hit");
              void b.el.offsetWidth;  // restart the animation
              b.el.classList.add("hit");
            }
            b.hitAmp = 0.3 + pw * 0.7;
            // One faint ring for a small call, up to three wide ones for a big one.
            var nr = REDUCED ? 1 : pw < 0.4 ? 1 : pw < 0.75 ? 2 : 3;
            for (var ri = 0; ri < nr; ri++) rings.push({ name: s.name, t0: t + ri * 160, amp: 0.45 + pw * 0.85 });
            floater(s.ev, b);
            sheetLive(s.ev);
          }
        }
      });
      g.globalCompositeOperation = "source-over";
      if (clipped) { ctx.restore(); zoomNow = cam.z; }
    }

    // The spark knocks its cell along the way it was travelling, a heavy cell less than a
    // light one; a big call also shoves the neighbours away from the hit. Distances are in
    // screen pixels at any zoom. A cell under the pointer, held, or small on screen gets no
    // kick (it would slip away from a click, L-135); it still pulses and rings. The cell
    // under the pointer also barely yields in collisions (see step).
    function stays(o) {
      return o === hover || (drag && drag.b === o) || o.r * cam.z < LEAN_MIN_R;
    }
    function impact(b, s, pw) {
      if (REDUCED || s.trail.length < 2) return;
      var a = s.trail[s.trail.length - 2], z = s.trail[s.trail.length - 1];
      var dx = z[0] - a[0], dy = z[1] - a[1], d = Math.sqrt(dx * dx + dy * dy) || 1;
      if (!stays(b)) {
        var kick = (0.4 + pw * 6) / (1 + b.r / 50) / cam.z;
        b.vx += dx / d * kick; b.vy += dy / d * kick;
      }
      if (pw < 0.5) return;
      var reach = b.r + (40 + pw * 80) / cam.z;
      order.forEach(function (o) {
        if (o === b || o.dead || stays(o)) return;
        var ox = o.x - b.x, oy = o.y - b.y, od = Math.sqrt(ox * ox + oy * oy) || 1;
        var gap = od - o.r;
        if (gap > reach) return;
        var shove = (pw - 0.4) * 4 * (1 - Math.max(0, gap) / reach) / (1 + o.r / 50) / cam.z;
        o.vx += ox / od * shove; o.vy += oy / od * shove;
      });
    }

    function floater(ev, b) {
      var el = document.createElement("div");
      el.className = "floater" + (ev.tokens < 200000 ? " small" : "");
      el.textContent = "+" + F.tokens(ev.tokens) + " · " + money(ev.usd);
      var ox = canvas.offsetLeft, oy = canvas.offsetTop, R = b.r * cam.z;
      el.style.setProperty("--x", Math.round(ox + sx(b.x) - 40 + (Math.random() - 0.5) * Math.min(R, 120)) + "px");
      el.style.setProperty("--y", Math.round(oy + Math.min(H - 24, Math.max(10, sy(b.y) - Math.min(R, 120) * 0.4))) + "px");
      floatersEl.appendChild(el);
      setTimeout(function () { el.remove(); }, 3000);
      while (floatersEl.children.length > 14) floatersEl.firstChild.remove();
    }

    function spark(ev) {
      var name = ev.project;
      if (ev.cursor) { if (!blobs[name] || blobs[name].dead) return; }
      else if (!blobs[name] || blobs[name].dead) ensure(ev);
      // From the account that made it (the active one) or from Cursor's card; from a
      // random edge only when the cards are not on screen.
      var o = sparkOrigin(ev), x0, y0;
      if (o) { x0 = wx(o.x); y0 = wy(o.y); }
      else {
        var side = Math.random();
        x0 = wx(side < 0.5 ? (Math.random() < 0.5 ? -10 : W + 10) : Math.random() * W);
        y0 = wy(side < 0.5 ? Math.random() * H : (Math.random() < 0.5 ? -10 : H + 10));
      }
      // The more tokens a call carries, the fatter its spark and the harder it lands.
      var power = sparkPower(ev.tokens), size = 1.2 + power * 7;
      sparks.push({ name: name, ev: ev, x0: x0, y0: y0, t0: performance.now(),
                    dur: 1100 + Math.random() * 600, bend: (Math.random() - 0.5) * 220, trail: [], size: size,
                    power: power });
      if (sparks.length > 40) sparks.shift();
    }

    // A project seen live but not in the summary yet (new, or the summary is a cached
    // one): it is born now as a small cell, and the next summary sizes it properly.
    function ensure(ev) {
      var name = ev.project, b = blobs[name];
      // Zero on its own: what arrives live is counted once, through liveBy (label and size).
      var p = { name: name, usd: 0, tokens: 0, sessions: 1, tasks: [], task_count: 0, calls: 1, last: ev.t };
      if (b) {
        // Revived while shrinking away (e.g. after a range change): it shows the live
        // call, not the numbers of the range it left; the next summary sizes it.
        if (b.dead || !b.p || !b.p.usd) b.p = p;
        b.dead = false; b.liveUntil = Date.now() + 180000; b.target = Math.max(b.target, 10);
        rescale(); syncLabels();
        return;
      }
      var c = center();
      b = blobs[name] = { name: name, x: c.x + (Math.random() - 0.5) * 120, y: c.y + (Math.random() - 0.5) * 120,
                          vx: 0, vy: 0, r: 0, target: 10, alpha: 0, pulse: 0, flash: 0, phase: hash(name) * 1000,
                          seeds: [], moons: [], el: null, p: p, liveUntil: Date.now() + 180000 };
      for (var k = 0; k < 12; k++) b.seeds.push([hash(name + k) * 6.28, 0.6 + hash(name + "s" + k) * 0.9]);
      if (!(name in S.colors)) assignColors([{ name: name }]);
      order.push(b);
      syncLabels();
    }

    function at(x, y) {
      var best = null;
      order.forEach(function (b) {
        if (b.dead) return;
        if (cam.z > 1 && bigness(b) > 0.5) return;  // zoomed past it: it is background now
        var dx = x - b.x, dy = y - b.y;
        // 6 screen pixels of slack, whatever the zoom, and never under MIN_HIT_R on screen.
        var reach = Math.max(b.r + 6 / cam.z, MIN_HIT_R / cam.z);
        if (dx * dx + dy * dy <= reach * reach && (!best || b.r < best.r)) best = b;
      });
      return best;
    }
    function screenXY(e) { var r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
    function localXY(e) { var p = screenXY(e); return { x: wx(p.x), y: wy(p.y) }; }
    // On touch, a finger that lands on a cell drags it instead of scrolling the page;
    // anywhere else on the canvas the page still scrolls.
    // On the phone the canvas reaches under the cards; below the field it is only space
    // (audit of 95): a touch or click there is the page's.
    function belowField(y) { return skyOn && y > H; }
    canvas.addEventListener("touchstart", function (e) {
      var t = e.touches[0], r = canvas.getBoundingClientRect();
      if (e.touches.length > 1) { e.preventDefault(); return; }  // a pinch: ours, not the page's
      if (t && belowField(t.clientY - r.top)) return;
      if (t && (cam.z > 1 || at(wx(t.clientX - r.left), wy(t.clientY - r.top)))) e.preventDefault();
    }, { passive: false });
    canvas.addEventListener("pointerdown", function (e) {
      var sp = screenXY(e);
      if (belowField(sp.y) && !Object.keys(touches).length) return;
      touches[e.pointerId] = sp;
      var ids = Object.keys(touches);
      if (ids.length === 2) {
        // Two fingers: pinch to zoom around their midpoint (any drag is dropped).
        var a = touches[ids[0]], c = touches[ids[1]];
        pinch = { d0: Math.hypot(a.x - c.x, a.y - c.y) || 1, z0: cam.z };
        if (drag) { drag = null; }
        pan = null;
        return;
      }
      var p = localXY(e), b = at(p.x, p.y);
      if (!b) {
        if (cam.z > 1) {  // zoomed in: drag the background to move around
          pan = { id: e.pointerId, x: sp.x, y: sp.y, cx: cam.x, cy: cam.y };
          try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
          canvas.style.cursor = "move";
        }
        return;
      }
      drag = { b: b, id: e.pointerId, x0: p.x, y0: p.y, tx: p.x, ty: p.y, moved: false };
      try { canvas.setPointerCapture(e.pointerId); } catch (err) {}
      canvas.style.cursor = "grabbing";
    });
    canvas.addEventListener("pointermove", function (e) {
      if (touches[e.pointerId]) touches[e.pointerId] = screenXY(e);
      var ids = Object.keys(touches);
      if (pinch && ids.length === 2) {
        var a = touches[ids[0]], c = touches[ids[1]];
        zoomAt(pinch.z0 * Math.hypot(a.x - c.x, a.y - c.y) / pinch.d0, (a.x + c.x) / 2, (a.y + c.y) / 2);
        return;
      }
      if (pan && e.pointerId === pan.id) {
        var sp = screenXY(e);
        cam.x = pan.cx - (sp.x - pan.x) / cam.z; cam.y = pan.cy - (sp.y - pan.y) / cam.z;
        clampCam();
        return;
      }
      lastScreen = screenXY(e);
      var p = localXY(e); pointer = p;
      if (drag && e.pointerId === drag.id) {
        drag.tx = p.x; drag.ty = p.y;
        if (!drag.moved && Math.hypot(p.x - drag.x0, p.y - drag.y0) * cam.z > 6) drag.moved = true;
        return;
      }
      var b = belowField(lastScreen.y) ? null : at(p.x, p.y);
      if (b !== hover) { hover = b; canvas.style.cursor = b ? "grab" : "default"; placeLabels(true); }
    });
    function endDrag(e, open) {
      if (!drag || (e && e.pointerId !== drag.id)) return;
      var d = drag; drag = null;
      canvas.style.cursor = hover ? "grab" : "default";
      if (open && !d.moved) (d.b.cursor ? openCursorSheet : openSheet)(d.b.p);  // a tap, not a drag: the project sheet
      // A throw keeps its velocity (capped) and the physics settles it back.
      d.b.vx = Math.max(-40, Math.min(40, d.b.vx * 1.4));
      d.b.vy = Math.max(-40, Math.min(40, d.b.vy * 1.4));
    }
    function liftPointer(e) {
      delete touches[e.pointerId];
      if (Object.keys(touches).length < 2) pinch = null;
      if (pan && e.pointerId === pan.id) { pan = null; canvas.style.cursor = "default"; placeLabels(true); }
    }
    canvas.addEventListener("pointerup", function (e) { liftPointer(e); endDrag(e, true); });
    canvas.addEventListener("pointercancel", function (e) { liftPointer(e); endDrag(e, false); });
    // Wheel (or trackpad pinch, which arrives as a wheel with ctrlKey): zoom at the pointer.
    canvas.addEventListener("wheel", function (e) {
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;  // sideways: the browser's gestures
      if (belowField(screenXY(e).y)) return;  // over the cards' margin: the page scrolls
      if (!e.ctrlKey) {
        // The wheel zooms only while the whole canvas is on screen and the page is not in
        // the middle of a scroll (at any zoom); otherwise it keeps scrolling the page.
        var rc = canvas.getBoundingClientRect();
        if (rc.top < -8 || rc.top + H > window.innerHeight + 8) return;
        if (Date.now() - lastPageScroll < 500) return;
        if (cam.z <= 1 && e.deltaY > 0) return;  // nothing to zoom out of
      }
      e.preventDefault();
      var sp = screenXY(e);
      zoomAt(cam.z * Math.exp(-e.deltaY * (e.ctrlKey ? 0.012 : 0.0018)), sp.x, sp.y);
    }, { passive: false });
    canvas.addEventListener("dblclick", function (e) {
      var sp = screenXY(e);
      if (belowField(sp.y) || at(wx(sp.x), wy(sp.y))) return;
      zoomAt(cam.z > 1 ? 1 : 3, sp.x, sp.y);
    });
    canvas.addEventListener("pointerleave", function (e) {
      // A mouse press released outside the canvas left its entry behind (audit of 97).
      if (e.pointerType === "mouse" && !drag && !pan) delete touches[e.pointerId];
      if (!drag) { pointer = { x: -1e4, y: -1e4 }; lastScreen = null; hover = null; placeLabels(true); } });

    return {
      sync: sync, syncCursor: syncCursor, resize: resize, spark: spark, live: live,
      zoomBy: function (f) { zoomAt(cam.z * f, W / 2, H / 2); },
      tick: function (dt, t) {
        if (!ctx) return;
        step(dt, t); draw(t);
        frame++;
        placeLabels(frame % 8 === 0);
      },
      recolor: function () { syncLabels(); },
      relabel: function () { syncLabels(); placeLabels(true); }
    };
  })();

  // -------------------------------------------------------------------------------------
  // ECG: the last hour, minute by minute, drawn as heartbeats
  // -------------------------------------------------------------------------------------
  var ecg = (function () {
    // One continuous monitor sweeping right to left: a soft beat every second, always;
    // a live call turns the next beat into a tall bright one. Calls closer together than
    // a beat merge into one (taller) beat, so complexes never overlap.
    var canvas = $("ecg"), ctx = null, W = 0, H = 0;
    var beats = [], nextBeat = 0, pending = 0, flare = 0;
    var WINDOW_MS = 9000, GAP_MS = 520, COMPLEX_PX = 16;
    function resize() { var f = fit(canvas); ctx = f.ctx; W = f.w; H = f.h; }
    function beat(tokens) { pending += tokens || 0; if (tokens) nextBeat = Math.min(nextBeat, Date.now() + 60); }
    function tick(now) {
      if (now < nextBeat) return;
      var last = beats.length ? beats[beats.length - 1].t : 0;
      if (now - last < GAP_MS) { nextBeat = last + GAP_MS; return; }
      var big = pending > 0;
      var a = big ? Math.min(1, 0.5 + 0.12 * Math.log10(1 + pending / 1000)) : 0.3;
      beats.push({ t: now, a: a, big: big });
      if (big) flare = 1;
      pending = 0;
      nextBeat = now + 1000;
      while (beats.length && now - beats[0].t > WINDOW_MS + 1000) beats.shift();
    }
    function draw() {
      if (!ctx) return;
      var T = S.theme, now = Date.now();
      tick(now);  // the heartbeat is the panel's sign of life: it beats even with reduced motion
      ctx.clearRect(0, 0, W, H);
      var base = H * 0.62, head = W - 8, px = (head - 4) / WINDOW_MS;
      ctx.lineJoin = "round"; ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(0, base);
      beats.forEach(function (b) {
        var x = head - (now - b.t) * px, a = b.a * H * 0.56, w = COMPLEX_PX * (b.big ? 1.25 : 1);
        if (x < -w) return;
        ctx.lineTo(x - w * 0.5, base);
        ctx.lineTo(x - w * 0.34, base - a * 0.12);
        ctx.lineTo(x - w * 0.2, base);
        ctx.lineTo(x - w * 0.08, base + a * 0.14);
        ctx.lineTo(x, base - a);
        ctx.lineTo(x + w * 0.1, base + a * 0.28);
        ctx.lineTo(x + w * 0.2, base);
        ctx.lineTo(x + w * 0.38, base - a * 0.16);
        ctx.lineTo(x + w * 0.5, base);
      });
      ctx.lineTo(head, base);
      var grad = ctx.createLinearGradient(0, 0, W, 0);
      grad.addColorStop(0, rgba(T.pulse, 0));
      grad.addColorStop(0.35, rgba(T.pulse, 0.55));
      grad.addColorStop(1, rgba(T.pulse, 1));
      ctx.strokeStyle = grad; ctx.lineWidth = 1.8;
      if (T.dark) { ctx.shadowColor = T.pulse; ctx.shadowBlur = 6 + flare * 12; }
      ctx.stroke();
      ctx.shadowBlur = 0;
      var last = beats.length ? beats[beats.length - 1] : null;
      var glow = last ? Math.exp(-(now - last.t) / 240) * (last.big ? 1 : 0.55) : 0;
      flare *= 0.95;
      ctx.fillStyle = rgba(T.pulse, 0.22 * glow);
      ctx.beginPath(); ctx.arc(head, base, 6 + glow * 9, 0, 6.283); ctx.fill();
      ctx.fillStyle = T.pulse;
      ctx.beginPath(); ctx.arc(head, base, 2.6 + glow * 2.4, 0, 6.283); ctx.fill();
    }
    return { resize: resize, draw: draw, beat: beat };
  })();

  // -------------------------------------------------------------------------------------
  // River of time
  // -------------------------------------------------------------------------------------
  var river = (function () {
    var canvas = $("river"), box = $("river-box"), tip = $("river-tip"), brush = $("brush"), momentsEl = $("moments");
    var ctx = null, W = 0, H = 0, bins = [], t0 = 0, t1 = 1, max = 1, particles = [], unit = "day";
    var drag = null, hoverX = null;
    var AXIS = 22;
    function resize() { var f = fit(canvas); ctx = f.ctx; W = f.w; H = f.h; placeMoments(); }
    function set(d) {
      bins = d.timeline || []; unit = d.range.unit;
      var b = bounds();
      t0 = S.custom || S.range !== "todo" ? b.from : (bins.length ? bins[0].t : b.from);
      t1 = Math.min(b.to, Date.now() + 60000);
      if (t1 <= t0) t1 = t0 + 3600000;
      max = 0; bins.forEach(function (x) { if (x.usd > max) max = x.usd; });
      max = max || 1;
      if (!particles.length && !REDUCED) for (var i = 0; i < 160; i++) particles.push({ u: Math.random(), lane: Math.random() * 2 - 1, v: 0.4 + Math.random() * 0.8 });
      buildMoments(d.moments || []);
    }
    var UNIT_MS = { hour: 3600000, day: 86400000, week: 7 * 86400000 };
    function X(t) { return (t - t0) / (t1 - t0) * W; }
    function T(x) { return t0 + x / W * (t1 - t0); }
    function mid() { return (H - AXIS) * 0.56; }
    // Thickness at x: bins drawn at their centre, linear in between.
    function thick(x) {
      if (!bins.length) return 0;
      var u = UNIT_MS[unit] || 86400000, tt = T(x) - u / 2;
      var lo = 0, hi = bins.length - 1;
      if (tt <= bins[0].t) return val(bins[0]);
      if (tt >= bins[hi].t) {
        // After the last bin the river tapers to a thread within one unit, never runs on.
        var after = (tt - bins[hi].t) / u;
        return after <= 1 ? val(bins[hi]) : after < 2 ? val(bins[hi]) * (2 - after) : 0;
      }
      while (hi - lo > 1) { var m = (lo + hi) >> 1; if (bins[m].t <= tt) lo = m; else hi = m; }
      var a = bins[lo], b = bins[hi];
      var gapA = b.t - a.t > u * 1.5;
      var k = (tt - a.t) / (b.t - a.t);
      if (gapA) {
        // A gap (no calls): the river narrows to a thread between the two bins.
        var ka = (tt - a.t) / u, kb = (b.t - tt) / u;
        if (ka < 1) return val(a) * (1 - ka);
        if (kb < 1) return val(b) * (1 - kb);
        return 0;
      }
      return val(a) * (1 - k) + val(b) * k;
    }
    // Power 0.6: the river keeps its texture next to a record day (exact values in the tip).
    function val(bn) { return Math.pow(bn.usd / max, 0.6) * (H - AXIS) * 0.40; }
    function draw(t) {
      if (!ctx) return;
      var Tm = S.theme;
      ctx.clearRect(0, 0, W, H);
      var y0 = mid(), stepPx = 2;
      // Ribbon
      var breathe = REDUCED ? 0 : 1;
      ctx.beginPath();
      var x;
      for (x = 0; x <= W; x += stepPx) {
        var h = Math.max(0.6, thick(x) * (1 + 0.04 * breathe * Math.sin(t * 0.0015 + x * 0.02)));
        if (x === 0) ctx.moveTo(x, y0 - h); else ctx.lineTo(x, y0 - h);
      }
      for (x = W; x >= 0; x -= stepPx) {
        var h2 = Math.max(0.6, thick(x) * (1 + 0.04 * breathe * Math.sin(t * 0.0015 + x * 0.02 + 1.3)));
        ctx.lineTo(x, y0 + h2);
      }
      ctx.closePath();
      var g = ctx.createLinearGradient(0, 0, W, 0);
      g.addColorStop(0, rgba(Tm.ra, Tm.dark ? 0.75 : 0.65));
      g.addColorStop(0.55, rgba(Tm.rb, Tm.dark ? 0.8 : 0.7));
      g.addColorStop(1, rgba(Tm.rc, Tm.dark ? 0.9 : 0.8));
      ctx.fillStyle = g;
      if (Tm.dark) { ctx.shadowColor = rgba(Tm.rb, 0.6); ctx.shadowBlur = 18; }
      ctx.fill();
      ctx.shadowBlur = 0;
      // Flow
      ctx.globalCompositeOperation = Tm.blend;
      ctx.fillStyle = Tm.dark ? "rgba(255,255,255,.55)" : "rgba(255,255,255,.75)";
      particles.forEach(function (p) {
        p.u += p.v * 0.00045 * (1 + S.activity);
        if (p.u > 1) { p.u -= 1; p.lane = Math.random() * 2 - 1; }
        var px = p.u * W, th = thick(px);
        if (th < 2) return;
        var py = y0 + p.lane * th * 0.82 + Math.sin(t * 0.002 + p.u * 30) * 1.2;
        ctx.beginPath(); ctx.arc(px, py, 0.8 + Math.min(1.4, th / 30), 0, 6.283); ctx.fill();
      });
      ctx.globalCompositeOperation = "source-over";
      // Now marker
      if (includesNow() && !S.custom) {
        var nx = Math.min(W - 4, X(Date.now()));
        var beat = 0.5 + 0.5 * Math.sin(t * 0.006);
        ctx.fillStyle = Tm.pulse;
        ctx.beginPath(); ctx.arc(nx, y0, 3 + beat * 2, 0, 6.283); ctx.fill();
        ctx.strokeStyle = rgba(Tm.pulse, 0.35 * (1 - beat));
        ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(nx, y0, 6 + beat * 10, 0, 6.283); ctx.stroke();
      }
      // Axis
      ctx.fillStyle = Tm.muted;
      ctx.font = "11.5px -apple-system, BlinkMacSystemFont, sans-serif";
      ctx.textBaseline = "alphabetic";
      ticks().forEach(function (tk) {
        var tx = X(tk);
        if (tx < 16 || tx > W - 16) return;
        var label = unit === "hour" ? F.hour(tk) : F.date(tk, { year: false });
        var w = ctx.measureText(label).width;
        ctx.fillText(label, tx - w / 2, H - 6);
        ctx.fillRect(tx, H - AXIS + 2, 1, 4);
      });
      // Hover line
      if (hoverX != null && !drag) {
        ctx.strokeStyle = rgba(Tm.ink, 0.35); ctx.lineWidth = 1;
        ctx.beginPath(); ctx.moveTo(hoverX, 4); ctx.lineTo(hoverX, H - AXIS); ctx.stroke();
      }
    }
    function ticks() {
      var span = t1 - t0, out = [];
      var stepMs = span <= 2 * 86400000 ? 3 * 3600000 : span <= 10 * 86400000 ? 86400000 :
                   span <= 45 * 86400000 ? 7 * 86400000 : 14 * 86400000;
      if (W < 500) stepMs *= 2;
      var d = new Date(t0);
      if (stepMs < 86400000) d.setMinutes(0, 0, 0); else d.setHours(0, 0, 0, 0);
      var t = d.getTime();
      while (t < t1) { if (t > t0) out.push(t); t += stepMs; if (stepMs >= 86400000) { var dd = new Date(t); dd.setHours(0, 0, 0, 0); t = dd.getTime(); } }
      return out;
    }
    // Moments: floating tags above the river, stacked to avoid overlap.
    var moments = [];
    function buildMoments(list) {
      momentsEl.textContent = "";
      // Bin starts can precede a range that begins mid-day: clamp them to its start.
      moments = list.map(function (m) { return m.t < t0 && m.kind !== "first" && m.t + 86400000 > t0 ? Object.assign({}, m, { t: t0 }) : m; })
        .filter(function (m) { return m.t >= t0 && m.t <= t1; }).map(function (m) {
        var el = document.createElement("div");
        el.className = "moment";
        el.style.animationDelay = (-hash(m.kind + momentRank(m)) * 6).toFixed(2) + "s";
        var b = document.createElement("b"); b.textContent = momentLabel(m);
        var s = document.createElement("span");
        var bits = [];
        if (m.kind === "peak_hour") bits.push(F.date(m.t, { time: true }));
        else bits.push(F.date(m.t));
        if (m.title) bits.push(trimS(m.title, 26));
        if (m.usd != null) bits.push(money(m.usd));
        s.textContent = " " + bits.join(" · ");
        el.appendChild(b); el.appendChild(s);
        momentsEl.appendChild(el);
        return { m: m, el: el };
      });
      placeMoments();
    }
    function trimS(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }
    // A moment's tag comes from its kind (and rank among the costliest sessions); the
    // server's Spanish `label` is only a fallback for an unknown kind.
    function momentRank(m) {
      if (m.rank != null) return m.rank;
      var n = /(\d+)\s*$/.exec(m.label || "");
      return m.kind === "session" && n ? +n[1] - 1 : 0;
    }
    function momentLabel(m) {
      if (m.kind === "session") return momentRank(m) > 0 ? say("moment.session.top", { n: momentRank(m) + 1 }) : say("moment.session");
      if (m.kind === "first" || m.kind === "peak_day" || m.kind === "peak_hour") return say("moment." + m.kind);
      return m.label || "";
    }
    var PRIORITY = { session: 0, peak_day: 1, peak_hour: 2, first: 9 };
    function placeMoments() {
      if (!W) return;
      var rows = [[], []];
      // Measure with every tag displayed: a tag hidden last time would measure 0 wide.
      moments.forEach(function (o) { o.el.style.display = ""; });
      // Most telling first; whatever does not fit is left out rather than piled up.
      moments.sort(function (a, b) {
        var pa = (PRIORITY[a.m.kind] || 5) + (momentRank(a.m) > 0 ? 3 : 0);
        var pb = (PRIORITY[b.m.kind] || 5) + (momentRank(b.m) > 0 ? 3 : 0);
        return pa - pb;
      }).forEach(function (o) {
        var w = o.el.offsetWidth, h = o.el.offsetHeight;
        var x = Math.max(2, Math.min(W - w - 2, X(o.m.t) - w / 2));
        var row = 0;
        while (row < rows.length && rows[row].some(function (r) { return x < r[1] + 8 && x + w > r[0] - 8; })) row++;
        if (row >= rows.length) { o.el.style.display = "none"; return; }
        o.el.style.display = "";
        rows[row].push([x, x + w]);
        var y = 2 + row * (h + 6);
        var top = mid() - thick(X(o.m.t)) - 6;
        o.el.style.setProperty("--stem", Math.max(4, top - y - h) + "px");
        o.el.style.transform = "translate3d(" + Math.round(x) + "px," + Math.round(y) + "px,0)";
      });
    }
    // Hover, click and brush
    function lx(e) { var r = canvas.getBoundingClientRect(); return Math.max(0, Math.min(W, e.clientX - r.left)); }
    function binAt(x) {
      var t = T(x), u = UNIT_MS[unit] || 86400000, best = null;
      bins.forEach(function (b) { if (t >= b.t && t < b.t + u) best = b; });
      return best;
    }
    canvas.addEventListener("pointerdown", function (e) {
      drag = { x0: lx(e), x1: lx(e), id: e.pointerId };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener("pointermove", function (e) {
      var x = lx(e);
      hoverX = x;
      if (drag) {
        drag.x1 = x;
        var a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1);
        if (b - a > 6) { brush.hidden = false; brush.style.left = a + "px"; brush.style.width = (b - a) + "px"; }
      }
      var bn = binAt(x);
      if (bn) {
        tip.hidden = false;
        var lbl = unit === "hour" ? F.date(bn.t, { weekday: true, time: true }) : unit === "week" ? say("river.week", { date: F.date(bn.t) }) : F.date(bn.t, { weekday: true });
        tip.textContent = lbl + " · " + F.tokens(bn.tokens) + " tokens · " + money(bn.usd);
        var w = tip.offsetWidth;
        tip.style.transform = "translate3d(" + Math.round(Math.max(0, Math.min(W - w, x - w / 2))) + "px,0,0)";
      } else tip.hidden = true;
    });
    function endDrag(e) {
      if (!drag) return;
      var a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1);
      brush.hidden = true;
      var wasDrag = b - a > 8;
      drag = null;
      if (wasDrag) {
        setRange("custom", { from: Math.round(T(a)), to: Math.round(T(b)) });
      } else if (e && e.type === "pointerup") {
        var bn = binAt(a);
        if (bn && unit !== "hour") {
          // Calendar end, not +24 h: daylight-saving days are 23 or 25 hours long.
          var end = new Date(bn.t); end.setDate(end.getDate() + (unit === "week" ? 7 : 1));
          setRange("custom", { from: bn.t, to: end.getTime() });
        }
      }
    }
    canvas.addEventListener("pointerup", endDrag);
    canvas.addEventListener("pointercancel", function () { drag = null; brush.hidden = true; });
    canvas.addEventListener("pointerleave", function () { hoverX = null; tip.hidden = true; });
    return { resize: resize, set: set, draw: draw, place: placeMoments };
  })();

  // -------------------------------------------------------------------------------------
  // What the plans cost against API prices (L-160, audit of 86): the line of the old
  // "Where it comes from" card, now under the hero's money so it shows without cards.
  // Every Claude account counts, shown or hidden: it is about what is paid.
  // -------------------------------------------------------------------------------------
  function paintRoi() {
    var el = $("q-roi"), d = S.data;
    if (!el) return;
    el.textContent = "";
    var a = d && d.accounts, cl = 0;
    if (d && d.by_origin) Object.keys(d.by_origin).forEach(function (k) { cl += d.by_origin[k].usd || 0; });
    else if (d) cl = d.totals.usd;
    var days = a ? a.span_days || 0 : 0;
    el.hidden = !(a && cl && days >= 0.25);
    if (el.hidden) return;
    var paidMonth = a.price_main + a.price_others, apiMonth = cl / Math.max(days, 1e-9) * 30.4375;
    function b(t) { var e = document.createElement("b"); e.textContent = t; return e; }
    [say("acc.roi.pay"), b(say("acc.roi.month", { money: money(paidMonth) })), say("acc.roi.api"),
     b(say("acc.roi.month", { money: money(apiMonth) })), " (×" + F.num(apiMonth / Math.max(1, paidMonth), 1) + ")."]
      .forEach(function (p) { el.appendChild(typeof p === "string" ? document.createTextNode(p) : p); });
  }

  // -------------------------------------------------------------------------------------
  // Ranking, models, sheet
  // -------------------------------------------------------------------------------------
  // The lists are rebuilt on every reload and theme change: a focused row hands its focus
  // to the same project's new row, so keyboard users keep their place.
  function focusedRow(ol) {
    var a = document.activeElement;
    return a && a.parentNode === ol ? a.dataset.name : null;
  }
  function refocus(ol, name) {
    if (name == null) return;
    for (var i = 0; i < ol.children.length; i++) {
      if (ol.children[i].dataset.name === name) { ol.children[i].focus({ preventScroll: true }); return; }
    }
  }
  function renderRanking(d) {
    var keep = focusedRow($("rank"));
    renderRankingRows(d);
    refocus($("rank"), keep);
  }
  function renderRankingRows(d) {
    var ol = $("rank"); ol.textContent = "";
    var total = d.totals.usd || 1;
    d.projects.slice(0, 30).forEach(function (p) {
      var li = document.createElement("li");
      li.tabIndex = 0; li.dataset.name = p.name;
      var dot = document.createElement("span"); dot.className = "dot"; dot.style.background = projectColor(p.name);
      var name = document.createElement("span"); name.className = "name"; name.textContent = name.title = projName(p.name);
      var val = document.createElement("span"); val.className = "val"; val.textContent = money(p.usd);
      var sub = document.createElement("span"); sub.className = "sub";
      var s1 = document.createElement("span"); s1.textContent = F.tokens(p.tokens) + " tokens · " + many("n.sessions", p.sessions);
      var s2 = document.createElement("span"); s2.textContent = F.ago(p.last);
      sub.appendChild(s1); sub.appendChild(s2);
      var share = document.createElement("span"); share.className = "share";
      var bar = document.createElement("i"); bar.style.width = Math.max(1, p.usd / total * 100) + "%"; bar.style.background = projectColor(p.name);
      share.appendChild(bar);
      [dot, name, val, sub, share].forEach(function (x) { li.appendChild(x); });
      li.addEventListener("click", function () { openSheet(p); });
      li.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSheet(p); } });
      ol.appendChild(li);
    });
    $("rank-hint").textContent = d.projects.length > 30 ? say("rank.top", { n: F.num(d.projects.length) }) : many("n.projects", d.projects.length);
    var ul = $("models"); ul.textContent = "";
    var NAMES = { "claude-opus-5-5": "Opus 5.5", "claude-opus-5": "Opus 5", "claude-sonnet-5": "Sonnet 5", "claude-sonnet-5-5": "Sonnet 5.5",
                  "claude-fable-5": "Fable 5", "claude-fable-5-1": "Fable 5.1", "claude-fable-5-2": "Fable 5.2", "claude-haiku-5-5": "Haiku 5.5",
                  "claude-haiku-4-5": "Haiku 4.5", "claude-opus-4-8": "Opus 4.8" };
    d.models.forEach(function (m) {
      var li = document.createElement("li");
      li.textContent = (NAMES[m.model] || m.model) + " ";
      var s = document.createElement("span");
      s.textContent = F.pct(m.usd / total, m.usd / total < 0.01 ? 1 : 0) + " · " + money(m.usd);
      li.appendChild(s); ul.appendChild(li);
    });
    var fx = d.fx || {};
    $("footnote").textContent = say("footnote", {
      rate: F.num(fx.usd_eur || 0, 4),
      source: fx.source === "BCE" ? say("fx.ecb", { date: fx.date || "" }) : say("fx.fixed"),
      since: d.alltime.since ? F.date(d.alltime.since) : "–" });
  }

  // Every day of the range, one cell each: weekday, date, a bar, cost and tokens. Tap a
  // day to zoom into it. Empty days are kept so gaps read as gaps.
  function renderDays(d) {
    var box = $("days");
    box.textContent = "";
    var list = d.days || [];
    if (!list.length && !(S.cursor && d.cursor && d.cursor.days.length)) return;
    // A range with Cursor use only: no Claude days, the strip still spans Cursor's days.
    var cursorOnly = !list.length;
    var byDay = {}, max = 0, cursorDays = {};
    if (S.cursor && d.cursor) d.cursor.days.forEach(function (x) { cursorDays[x.t] = x; });
    list.forEach(function (x) { byDay[x.t] = x; if (x.usd > max) max = x.usd; });
    // From the range start (or the first day with data for the whole history) to today.
    var bd = bounds();
    var first = cursorOnly ? Infinity : list[0].t;
    if (S.cursor && d.cursor && d.cursor.days.length) first = Math.min(first, d.cursor.days[0].t);
    var start = new Date(S.range === "todo" && !S.custom ? first : bd.from);
    start.setHours(0, 0, 0, 0);
    var end = new Date(Math.min(bd.to - 60001, Date.now()));
    end.setHours(0, 0, 0, 0);
    var today = new Date(); today.setHours(0, 0, 0, 0);
    var DOW = I.list("days.dow");
    for (var day = new Date(start); day <= end; day.setDate(day.getDate() + 1)) {
      var t = day.getTime(), x = byDay[t];
      var cell = document.createElement("button");
      cell.type = "button";
      cell.className = "day" + (t === today.getTime() ? " today" : "") + (x ? "" : " empty") + (x && max > 0 && x.usd === max ? " peak" : "");
      var top = document.createElement("span"); top.className = "d-top";
      top.textContent = DOW[day.getDay()] + " " + day.getDate();
      var bar = document.createElement("span"); bar.className = "d-bar";
      var fill = document.createElement("i");
      fill.style.height = x ? Math.max(4, Math.sqrt(x.usd / (max || 1)) * 100) + "%" : "0";
      bar.appendChild(fill);
      var val = document.createElement("span"); val.className = "d-val";
      var amount = x ? cur(x.usd) : 0;
      val.textContent = !x ? "·" : F.moneyRound(amount, S.currency);
      var tok = document.createElement("span"); tok.className = "d-tok";
      tok.textContent = x ? F.tokens(x.tokens) : "";
      cell.title = F.date(t, { weekday: true }) + (x ? " · " + F.tokens(x.tokens) + " tokens · " + money(x.usd) + " · " + many("n.calls", x.calls)
        : " · " + say(cursorDays[t] ? "days.cursor.only" : "days.none"));
      [top, bar, val, tok].forEach(function (n) { cell.appendChild(n); });
      var cd = cursorDays[t];
      if (cd) {
        var cm = document.createElement("span"); cm.className = "d-cursor";
        cm.textContent = "◇ " + (cd.usd >= 1 ? F.moneyRound(cur(cd.usd), S.currency) : F.num(cd.requests + cd.turns));
        cm.title = "Cursor: " + money(cd.usd) + " · " + many("n.requests", cd.requests) + (cd.turns ? " · " + many("n.turns", cd.turns) : "") +
          " · " + many("n.convs", cd.convs);
        cell.appendChild(cm);
        cell.classList.remove("empty");
      }
      if (day.getDate() === 1 || t === start.getTime()) {
        var mo = document.createElement("span"); mo.className = "d-month";
        mo.textContent = F.month(t);
        cell.appendChild(mo);
      }
      (function (from) {
        cell.addEventListener("click", function () {
          var e = new Date(from); e.setDate(e.getDate() + 1);
          setRange("custom", { from: from, to: e.getTime() });
        });
      })(t);
      box.appendChild(cell);
    }
    box.scrollLeft = box.scrollWidth;
  }

  // Cursor: hero line, its own ranking, day-strip marks (turns), models.
  function renderCursor(d) {
    var c = S.cursor ? d.cursor : null;
    var line = $("hero-cursor"), wrap = $("cursor-rank-wrap");
    line.hidden = !c; wrap.hidden = !c;
    if (!c) return;
    var tt = c.totals;
    line.textContent = "";
    var bb = document.createElement("b"); bb.textContent = "Cursor";
    line.appendChild(bb);
    var api = c.status && c.status.api;
    line.appendChild(document.createTextNode(say("cursor.included") + " · " + money(tt.usd) + " · " + F.tokens(tt.tokens) + " tokens · " +
      many("n.requests", tt.requests) + " · " + many("n.convs", tt.convs) +
      (tt.turns ? " · " + many("cursor.local.turns", tt.turns) : "") +
      (c.status && c.status.available === false ? " · " + say("cursor.not.installed") : "") +
      (api && api.error ? " · " + apiError(api) : "")));
    var ol = $("cursor-rank"), keep = focusedRow(ol); ol.textContent = "";
    var maxT = 1;
    c.projects.forEach(function (p) { maxT = Math.max(maxT, p.usd || 0); });
    c.projects.slice(0, 20).forEach(function (p) {
      var li = document.createElement("li"); li.tabIndex = 0; li.dataset.name = p.name;
      var dot = document.createElement("span"); dot.className = "dot ring";
      var name = document.createElement("span"); name.className = "name"; name.textContent = cursorName(p);
      var val = document.createElement("span"); val.className = "val"; val.textContent = p.usd ? money(p.usd) : F.cursorCount(p);
      var sub = document.createElement("span"); sub.className = "sub";
      var s1 = document.createElement("span"); s1.textContent = (p.tokens ? F.tokens(p.tokens) + " tokens · " : "") +
        many(p.requests ? "n.requests" : "n.turns", p.requests || p.turns) + " · " + say("cursor.conv.short", { n: F.num(p.convs) });
      var s2 = document.createElement("span"); s2.textContent = F.ago(p.last);
      sub.appendChild(s1); sub.appendChild(s2);
      var share = document.createElement("span"); share.className = "share";
      var bar = document.createElement("i"); bar.style.width = Math.max(1, (p.usd || 0) / maxT * 100) + "%"; bar.style.background = S.theme.cursor;
      share.appendChild(bar);
      [dot, name, val, sub, share].forEach(function (x) { li.appendChild(x); });
      li.addEventListener("click", function () { openCursorSheet(p); });
      li.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openCursorSheet(p); } });
      ol.appendChild(li);
    });
    refocus(ol, keep);
    var ml = $("cursor-models"); ml.textContent = "";
    c.models.slice(0, 8).forEach(function (m) {
      var li = document.createElement("li");
      li.textContent = m.model + " ";
      var s = document.createElement("span");
      s.textContent = [m.requests ? many("n.requests", m.requests) : "", m.turns ? many("n.turns", m.turns) : ""].filter(Boolean).join(" · ");
      li.appendChild(s); ml.appendChild(li);
    });
  }

  // -------------------------------------------------------------------------------------
  // Project sheet: the tasks of one cell, with what is happening now. Each task shows how
  // fresh it is (a dot in the project's colour, "hace 3 min"); the dot of the task that
  // last received tokens blinks; live calls play on their task row while it is open.
  // -------------------------------------------------------------------------------------
  var SH = { p: null, cursor: false, tasks: [], sort: store("latidos.sheetSort") === "recent" ? "recent" : "usd",
             editing: null };  // "name" or a task key while a form is open: renders wait
  // The same colour as the project's cell, label and ranking row.
  function sheetColor() { return SH.cursor ? cursorTint(SH.p.name) : projectColor(SH.p.name); }
  // Live calls of this project that the current summary does not count yet.
  function sheetPending() {
    if (SH.cursor || !SH.p) return [];
    return hero.pending().filter(function (e) { return e.project === SH.p.name; });
  }
  function openCursorSheet(p) { openAnySheet(p, true); }
  function openSheet(p) { openAnySheet(p, false); }
  function openAnySheet(p, cursor) {
    SH.p = p; SH.cursor = cursor; endEdit();
    SH.tasks = F.liveTasks(p.tasks || [], sheetPending());
    renderSheet();
    showSheet();
  }
  // A new summary: the open sheet follows it (plus the calls it does not count yet).
  function refreshSheet(d) {
    if ($("sheet").hidden || !SH.p || !d) return;
    var list = SH.cursor ? (d.cursor ? d.cursor.projects : []) : d.projects, np = null;
    (list || []).forEach(function (x) { if (x.name === SH.p.name) np = x; });
    // Not in the new answer (a range without it, Cursor switched off): close rather than
    // keep the old range's numbers on screen.
    if (!np) return closeSheet();
    SH.p = np;
    SH.tasks = F.liveTasks(np.tasks || [], sheetPending());
    renderSheet();
  }
  function sheetAdded() {
    var x = { usd: 0, tokens: 0 };
    sheetPending().forEach(function (e) { x.usd += e.usd || 0; x.tokens += e.tokens || 0; });
    return x;
  }
  function renderSheet() {
    if (SH.editing) return;  // a refresh would wipe what is being typed; it runs on close
    var p = SH.p, add = sheetAdded(), now = Date.now(), col = sheetColor();
    // Renaming and moving are for Claude Code cells (Cursor's come from its own names).
    $("sheet-rename").hidden = SH.cursor || p.name === "desconocido";
    $("sheet-eyebrow").textContent = say(SH.cursor ? "sheet.project.cursor" : "sheet.project");
    $("sheet-title").textContent = SH.cursor ? "Cursor · " + cursorName(p) : projName(p.name);
    $("sheet").style.setProperty("--c", col);
    var usd = (p.usd || 0) + add.usd, tok = (p.tokens || 0) + add.tokens;
    var so = $("sheet-origins");
    so.textContent = "";
    var po = SH.cursor ? [{ key: "cursor", share: 1 }] : F.mainOrigins(sheetOrigins(p), 0.02, 6);
    if (po.length) originChips(so, po, true);
    so.hidden = !po.length;
    $("sheet-stats").textContent = SH.cursor
      ? money(usd) + " · " + F.tokens(tok) + " tokens · " + many("n.requests", p.requests || 0) +
        (p.turns ? " · " + many("cursor.local.turns", p.turns) : "") + " · " + many("n.convs", p.convs || 0)
      : money(usd) + " · " + F.tokens(tok) + " tokens · " + many("n.sessions", p.sessions || 0) +
        " · " + many("n.tasks", p.task_count || 0);
    // Last activity (L-175): no line of its own, which repeated a row; its row's dot blinks
    // and says so on hover.
    var last = F.latestTask(SH.tasks);
    SH.latestKey = last ? last.key : null;
    ["usd", "recent"].forEach(function (m) {
      var bt = $("sheet-sort-" + m);
      bt.setAttribute("aria-pressed", String(SH.sort === m));
    });
    var ol = $("sheet-tasks"), keep = $("sheet").querySelector(".sheet-card").scrollTop;
    ol.textContent = "";
    F.sortTasks(SH.tasks, SH.sort).forEach(function (t) { ol.appendChild(taskRow(t, now)); });
    var count = p.task_count || 0;
    if (count > (p.tasks || []).length) {
      var more = document.createElement("li"); more.className = "t-sub";
      more.textContent = many(SH.cursor ? "sheet.more.convs" : "sheet.more.tasks", count - (p.tasks || []).length);
      ol.appendChild(more);
    }
    $("sheet").querySelector(".sheet-card").scrollTop = keep;
  }
  // The project's origins plus the live calls the summary does not count yet.
  function sheetOrigins(p) {
    var o = p.origins || [];
    sheetPending().forEach(function (e) { o = F.addOrigin(o, e.origin || (e.account ? "acct:" + e.account : "claude"), e.tokens || 0); });
    return o;
  }
  // Providers get hues outside the account palette, so an account never looks like one.
  var ORIGIN_COLORS = { claude: "#8a8f99", bedrock: "#a16207", vertex: "#0e7490", other: "#7a6f62" };
  function originColor(key) {
    if (key === "cursor") return S.theme.cursor;
    if (key.indexOf("acct:") === 0) {
      // The same colour as the account's card: its own, else its place in the list.
      var tag = key.slice(5), acc = (QA.data && QA.data.claude && QA.data.claude.accounts) || [], i = -1;
      acc.forEach(function (a, j) { if (a.tag === tag && i < 0) i = j; });
      return tagColor({ tag: tag }, i >= 0 ? i : Math.floor(hash(tag) * TAG_PALETTE.length));
    }
    return ORIGIN_COLORS[key] || ORIGIN_COLORS.other;
  }
  function originLabel(key) {
    if (key.indexOf("acct:") === 0) return say("origin.claude.tag", { tag: key.slice(5) });
    return say("origin." + (ORIGIN_COLORS[key] || key === "cursor" ? key : "other"));
  }
  // Chips "Claude · mt 70 %" in front of a row title or under the sheet's stats.
  function originChips(parent, list, withPct) {
    if (!list.length) return;
    var box = document.createElement("span"); box.className = "origins"; box.setAttribute("role", "group");
    var words = [];
    list.forEach(function (o) {
      var c = document.createElement("span"); c.className = "origin";
      c.style.setProperty("--o", originColor(o.key));
      c.appendChild(document.createTextNode(originLabel(o.key)));
      var pct = list.length > 1 || withPct ? " " + Math.round(o.share * 100) + (I.getLang() === "es" ? "\u00a0%" : "%") : "";
      if (list.length > 1 || withPct) { var s = document.createElement("span"); s.className = "pct"; s.textContent = pct; c.appendChild(s); }
      box.appendChild(c);
      words.push(originLabel(o.key) + pct);
    });
    box.setAttribute("aria-label", say("origin.aria", { list: words.join(", ") }));
    parent.appendChild(box);
  }
  function taskName(t) {
    if (SH.cursor) return cursorTitle(t);
    return ((t.ticket ? t.ticket + " " : "") + (F.taskTitle(t) || (t.ticket ? "" : say("sheet.untitled.session")))).trim();
  }
  function taskRow(t, now) {
    var li = document.createElement("li");
    li.dataset.key = t.key || "";
    var title = document.createElement("span"); title.className = "t-title";
    var fr = F.freshness(t.last, now);
    var dot = document.createElement("i");
    // Beats while the task is receiving tokens (a live call in the last 2 min), glows when fresh.
    dot.className = "fresh" + (t.liveAt && now - t.liveAt < 120000 ? " now" : "");
    dot.style.setProperty("--f", fr.toFixed(3));
    title.appendChild(dot);
    if (t.last && t.key === SH.latestKey) {
      dot.classList.add("latest");
      // The sheet is redrawn every 30 s and on every live call: the blink keeps its phase.
      if (!dot.classList.contains("now")) dot.style.animationDelay = -Math.round(performance.now() % 1800) + "ms";
      li.classList.add("latest");
      // Hover shows it (a CSS tip, not a title, which some screen readers would read a
      // second time); screen readers get it once, after the task's name (audit of 96).
      var hint = say("sheet.last.tip", { when: F.ago(t.last, now), at: F.date(t.last, { time: true }) });
      title.dataset.tip = hint;
      var hintSr = document.createElement("span"); hintSr.className = "sr-only"; hintSr.textContent = ". " + hint;
    }
    if (SH.cursor) {
      if (t.source === "cli") { var tc = document.createElement("span"); tc.className = "ticket"; tc.textContent = "CLI"; title.appendChild(tc); }
      title.appendChild(document.createTextNode(cursorTitle(t)));
      originChips(title, [{ key: "cursor", share: 1 }], false);
    } else {
      if (t.ticket) { var tk = document.createElement("span"); tk.className = "ticket"; tk.textContent = t.ticket; title.appendChild(tk); }
      title.appendChild(document.createTextNode(F.taskTitle(t) || (t.ticket ? "" : say("sheet.untitled.session"))));
      originChips(title, F.mainOrigins(t.origins, 0.1, 3), false);
      if (t.assigned) { var mv = document.createElement("span"); mv.className = "moved"; mv.textContent = say("sheet.moved"); title.appendChild(mv); }
    }
    if (hintSr) title.appendChild(hintSr);
    var val = document.createElement("span"); val.className = "t-val";
    val.textContent = t.usd ? money(t.usd) : (SH.cursor ? F.cursorCount(t) : money(0));
    var sub = document.createElement("span"); sub.className = "t-sub";
    var bits = SH.cursor
      ? [t.model || say("sheet.model.unknown"), t.tokens ? F.tokens(t.tokens) + " tokens" : "", t.requests ? many("n.requests", t.requests) : ""]
      : [F.tokens(t.tokens) + " tokens", many("n.sessions", t.sessions)];
    sub.textContent = bits.filter(Boolean).join(" · ") + " · ";
    var when = document.createElement("span"); when.className = "t-when";
    when.textContent = t.last ? F.ago(t.last, now) : "–";
    if (t.last) when.title = F.date(t.last, { time: true });
    sub.appendChild(when);
    var moveKey = taskMoveKey(t);
    if (!SH.cursor && moveKey) {
      var mb = document.createElement("button"); mb.type = "button"; mb.className = "t-move";
      mb.textContent = say("sheet.move"); mb.setAttribute("aria-label", say("sheet.move.aria", { task: taskName(t) }));
      mb.addEventListener("click", function () { openMove(t, li); });
      sub.appendChild(mb);
    }
    li.appendChild(title); li.appendChild(val); li.appendChild(sub);
    return li;
  }

  // --- Edits: rename the cell, move a task to another cell. Saved by the server in the
  // user config (aliases.json, assign.json), so they hold for every range and live call.
  // The assign.json key for a task row: a live-only row ("live:<session>") is that
  // session; a row without a real session id cannot be moved.
  function taskMoveKey(t) {
    var k = t.key || "";
    if (k.indexOf("live:") === 0) k = "s:" + k.slice(5);
    return /^(s:[A-Za-z0-9._-]{1,80}|[A-Z][A-Z0-9]{0,14}-[0-9]{1,9})$/.test(k) ? k : null;
  }
  function postJSON(url, body) {
    return fetch(url, { method: "POST", cache: "no-store",
                        headers: { "Content-Type": "application/json", "X-Latidos": "1" },
                        body: JSON.stringify(body) }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) {
          // A config file with a typo by hand: the page says which, in its language.
          var e = new Error(r.status === 409 && j.file ? say("sheet.err.conflict", { file: j.file }) : (j.error || "HTTP " + r.status));
          e.code = j.error_code || "";
          e.tag = j.tag || "";
          e.status = r.status;
          throw e;
        }
        return j;
      });
    });
  }
  function endEdit() {
    SH.editing = null;
    $("sheet-rename-form").hidden = true;
    var old = $("sheet-tasks").querySelector(".move-row");
    if (old) old.remove();
  }
  function formError(form, e) {
    var el = form.querySelector(".form-err");
    el.textContent = say("sheet.err", { detail: e && e.message ? e.message : String(e) });
    el.classList.remove("note");
    el.hidden = false;
  }
  function afterEdit(focusEl) {
    endEdit();
    renderSheet();
    (focusEl && document.contains(focusEl) && !focusEl.hidden ? focusEl : $("sheet-close")).focus();
    load();  // the server already changed its version: the next summary has the new names
  }
  $("sheet-rename").addEventListener("click", function () {
    if (!SH.p || SH.cursor) return;
    endEdit();
    SH.editing = "name";
    var f = $("sheet-rename-form"), inp = $("sheet-rename-input");
    f.querySelector(".form-err").hidden = true;
    f.hidden = false;
    inp.value = SH.p.name;
    inp.focus(); inp.select();
  });
  $("sheet-rename-cancel").addEventListener("click", function () { endEdit(); renderSheet(); $("sheet-rename").focus(); });
  $("sheet-rename-input").addEventListener("keydown", function (e) {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endEdit(); renderSheet(); $("sheet-rename").focus(); }
  });
  $("sheet-rename-form").addEventListener("submit", function (e) {
    e.preventDefault();
    if (!SH.p || SH.cursor) return endEdit();
    var f = this, from = SH.p.name, to = $("sheet-rename-input").value.trim();
    if (!to || to === from) { endEdit(); renderSheet(); $("sheet-rename").focus(); return; }
    postJSON("/api/rename", { from: from, to: to }).then(function (j) {
      SH.p = Object.assign({}, SH.p, { name: j.name });  // the sheet follows its cell
      afterEdit($("sheet-rename"));
    }).catch(function (err) { formError(f, err); });
  });
  function openMove(t, li) {
    endEdit();
    SH.editing = t.key;
    var dl = $("sheet-projects"); dl.textContent = "";
    ((S.data && S.data.projects) || []).forEach(function (p) {
      if (p.name === SH.p.name || p.name === "desconocido") return;
      var o = document.createElement("option"); o.value = p.name; dl.appendChild(o);
    });
    var row = document.createElement("li"); row.className = "move-row";
    var f = document.createElement("form"); f.className = "name-form";
    var id = "move-input";
    var lab = document.createElement("label"); lab.htmlFor = id; lab.textContent = say("sheet.move.label");
    var inp = document.createElement("input"); inp.id = id; inp.type = "text"; inp.maxLength = 60;
    inp.autocomplete = "off"; inp.spellcheck = false; inp.setAttribute("list", "sheet-projects");
    var save = document.createElement("button"); save.type = "submit"; save.textContent = say("sheet.save");
    var cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = say("sheet.cancel");
    var err = document.createElement("p"); err.className = "form-err"; err.setAttribute("role", "alert"); err.hidden = true;
    [lab, inp, save, cancel].forEach(function (x) { f.appendChild(x); });
    if (t.assigned) {
      var auto = document.createElement("button"); auto.type = "button"; auto.textContent = say("sheet.move.auto");
      auto.addEventListener("click", function () {
        postJSON("/api/assign", { key: t.assign_key || taskMoveKey(t), project: null })
          .then(function () { afterEdit(); }).catch(function (e2) { formError(f, e2); });
      });
      f.appendChild(auto);
    }
    f.appendChild(err);
    var key = taskMoveKey(t);
    // Back to the row's own Move button (rebuilt by the render) or the close button.
    function refocusRow() {
      var again = null;
      $("sheet-tasks").querySelectorAll("li").forEach(function (x) { if (x.dataset.key === t.key) again = x.querySelector(".t-move"); });
      (again || $("sheet-close")).focus();
    }
    function back() { endEdit(); renderSheet(); refocusRow(); }
    cancel.addEventListener("click", back);
    inp.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); back(); } });
    f.addEventListener("submit", function (e) {
      e.preventDefault();
      var to = inp.value.trim();
      if (!SH.p) return endEdit();
      if (!to || to === SH.p.name) return back();
      var existing = [].some.call(dl.options, function (o) { return o.value === to; });
      postJSON("/api/assign", { key: key, project: to, existing: existing }).then(function (j) {
        if (j.project && j.project !== to) {
          // That name was renamed away earlier: say where the task went, then close.
          err.textContent = say("sheet.moved.to", { name: j.project }); err.classList.add("note"); err.hidden = false;
          save.disabled = true;
          setTimeout(function () { afterEdit(); }, 3500);
          return;
        }
        afterEdit();
      }).catch(function (e2) { formError(f, e2); });
    });
    row.appendChild(f);
    li.parentNode.insertBefore(row, li.nextSibling);
    inp.focus();
  }
  // A live call landed on its cell: if that project's sheet is open, it plays on the task.
  function sheetLive(ev) {
    if ($("sheet").hidden || SH.cursor || !SH.p || ev.project !== SH.p.name) return;
    SH.tasks = F.liveTasks(SH.p.tasks || [], sheetPending());
    renderSheet();
    var key = null;
    SH.tasks.forEach(function (t) { if ((t.sids || []).indexOf(ev.session) >= 0) key = t.key; });
    var row = null;
    $("sheet-tasks").querySelectorAll("li").forEach(function (li) { if (li.dataset.key === key) row = li; });
    if (!row) return;
    row.classList.add("hit");
    var tag = document.createElement("span"); tag.className = "row-tag";
    tag.textContent = "+" + F.tokens(ev.tokens) + " · " + money(ev.usd);
    row.appendChild(tag);
    setTimeout(function () { row.classList.remove("hit"); tag.remove(); }, 1800);
  }
  ["usd", "recent"].forEach(function (m) {
    $("sheet-sort-" + m).addEventListener("click", function () {
      SH.sort = m; store("latidos.sheetSort", m);
      ["usd", "recent"].forEach(function (x) { $("sheet-sort-" + x).setAttribute("aria-pressed", String(x === m)); });
      if (SH.p) renderSheet();
    });
  });
  // "3 min ago" keeps moving while the sheet is open.
  setInterval(function () { if (!$("sheet").hidden && SH.p) renderSheet(); }, 30000);

  // The sheet is a modal dialog: focus moves into it, Tab stays inside, and closing it
  // returns focus to whatever opened it.
  var sheetOpener = null, openerList = null, openerName = null;
  function showSheet() {
    if ($("sheet").hidden) {
      sheetOpener = document.activeElement;
      // A list row may be rebuilt while the sheet is open: remember which list and project.
      openerList = sheetOpener && sheetOpener.parentNode && /^(rank|cursor-rank)$/.test(sheetOpener.parentNode.id) ? sheetOpener.parentNode : null;
      openerName = openerList ? sheetOpener.dataset.name : null;
    }
    $("sheet").hidden = false;
    $("sheet-close").focus();
  }
  function closeSheet() {
    if ($("sheet").hidden) return;
    endEdit();
    $("sheet").hidden = true;
    if (sheetOpener && document.contains(sheetOpener) && sheetOpener.focus) sheetOpener.focus();
    else if (openerList) refocus(openerList, openerName);
    sheetOpener = null; openerList = null; openerName = null;
  }
  // Listened on the document: a click on the sheet's text leaves focus on <body>, where
  // a handler on the sheet would never hear the Tab.
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Tab" || $("sheet").hidden) return;
    var f = Array.prototype.filter.call($("sheet").querySelectorAll("button, input, [href], [tabindex]:not([tabindex='-1'])"),
                                        function (x) { return x.offsetParent !== null; });
    if (!f.length) return;
    var first = f[0], last = f[f.length - 1], inside = $("sheet").contains(document.activeElement);
    if (!inside) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
  $("sheet-close").addEventListener("click", closeSheet);
  $("sheet").addEventListener("click", function (e) { if (e.target.id === "sheet") closeSheet(); });

  // -------------------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------------------
  function render(resetHero) {
    var d = S.data;
    if (!d) return;
    paintRanges(); paintStatus();
    var cz = S.cursor && d.cursor ? d.cursor.totals : { tokens: 0, usd: 0 };
    var cl = d.totals;
    // keep = not a reset: only a range change spins the reels.
    hero.set(cl.tokens + cz.tokens, cl.usd + cz.usd, d.seq_at, !resetHero);
    var tt = d.totals.tokens || 1;
    var mix = $("hero-mix"); mix.textContent = "";
    [[say("hero.mix.cread"), d.totals.cread], [say("hero.mix.cwrite"), d.totals.cwrite], [say("hero.mix.out"), d.totals.out], [say("hero.mix.inp"), d.totals.inp]].forEach(function (x, i) {
      if (i) mix.appendChild(document.createTextNode(" · "));
      var bb = document.createElement("b"); bb.textContent = F.pct(x[1] / tt, x[1] / tt < 0.01 ? 1 : 0);
      mix.appendChild(bb); mix.appendChild(document.createTextNode(" " + x[0]));
    });
    mix.appendChild(document.createTextNode(" · " + say("hero.mix.calls", { calls: many("n.calls", d.totals.calls), sessions: many("n.sessions", d.totals.sessions) })));
    eco.sync(d.projects);
    eco.syncCursor(S.cursor && d.cursor ? d.cursor.projects : []);
    renderCursor(d);
    renderAccounts();
    cursorSparks(d);
    eco.live(F.byProject(hero.pending()));
    river.set(onlyCursor() && d.cursor ? cursorRiver(d) : d);
    renderDays(d);
    document.querySelector(".stage").classList.toggle("no-cursor", !S.cursor);
    paintRoi();
    renderRanking(d);
    refreshSheet(d);
    paintEcgText();
    document.title = "Latidos · " + money(cl.usd + cz.usd);
  }

  function cursorRiver(d) {
    var hourly = d.range.unit === "hour" && d.cursor && d.cursor.hours;
    var bins = hourly ? d.cursor.hours : ((d.cursor && d.cursor.days) || []);
    return { range: { from: d.range.from, to: d.range.to, unit: hourly ? "hour" : "day" }, moments: [],
             timeline: bins.map(function (x) { return { t: x.t, usd: x.usd, tokens: x.tokens, calls: (x.requests || 0) + (x.turns || 0) }; }) };
  }

  function paintEcgText() {
    var d = S.data, el = $("ecg-text");
    if (!d) return;
    var L = d.live;
    if (L.calls_15m) {
      el.textContent = "";
      var bb = document.createElement("b"); bb.textContent = say("ecg.rate", { n: F.tokens(L.tokens_15m / 15) });
      el.appendChild(bb);
      el.appendChild(document.createTextNode(" · " + money(L.usd_15m * 4) + say("ecg.now")));
    } else el.textContent = say("ecg.idle", { when: L.last ? F.ago(L.last) : "–" });
  }

  // Also after a theme change: the ranking rows, Cursor's list and an open sheet carry
  // inline colours computed for the old theme.
  function drawAll() { if (S.data) { eco.relabel(); river.place(); renderRanking(S.data); renderCursor(S.data); refreshSheet(S.data); } renderAccounts(); }

  // A language switch: the static copy, every chip and everything drawn from the data.
  function relabelAll() {
    I.apply(document);
    paintLang(); paintTheme(themeMode()); paintSound();
    hero.relabel(); paintStatus();
    if (S.data) render(); else paintRanges();
    renderAccounts();
  }

  // Names the server generates for what has none (Cursor's "no folder" project, a request
  // with no conversation on this Mac) come with a code; real names are data and stay.
  function projName(name) { return name === "desconocido" ? say("project.unknown") : name; }
  function cursorName(p) { return p.name_code ? say("cursor.name." + p.name_code) : p.name; }
  function cursorTitle(t) {
    return t.title_code ? say("cursor.title." + t.title_code) : (t.title || say("cursor.title.untitled"));
  }
  // Cursor's API status: a code the page words, else the server's own message.
  function apiError(api) {
    return api.error_code ? say("cursor.err." + api.error_code, { detail: api.error_detail || "" }) : api.error;
  }

  // -------------------------------------------------------------------------------------
  // Loop, resize, keys
  // -------------------------------------------------------------------------------------
  var lastT = performance.now();
  function loop(t) {
    var dt = t - lastT; lastT = t;
    hero.frame(Math.min(dt, 100));
    eco.tick(dt, t);
    ecg.draw(t);
    river.draw(t);
    requestAnimationFrame(loop);
  }
  function resizeAll() { eco.resize(); ecg.resize(); river.resize(); drawAll(); }
  var rz = null;
  window.addEventListener("resize", function () { clearTimeout(rz); rz = setTimeout(resizeAll, 120); });
  document.addEventListener("keydown", function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey || /input|textarea/i.test(e.target.tagName)) return;
    var k = e.key.toLowerCase();
    if (k === "escape") return closeSheet();
    if (!$("sheet").hidden) return;  // the open sheet is modal: no shortcuts behind it
    if (k === "r") return load();
    if (k === "t") return themeBtn.click();
    if (k === "c") return $("currency").click();
    if (k === "k") return setShown("cursor", !S.cursor);
    if (k === "s") return setSound(!sound.on);
    if (k === "+" || k === "=") return eco.zoomBy(1.5);
    if (k === "-") return eco.zoomBy(1 / 1.5);
    if (k === "0") return eco.zoomBy(0);
    var map = { "1": "hoy", "2": "24h", "3": "7d", "4": "30d", "5": "90d", "6": "todo" };
    if (map[k]) setRange(map[k]);
  });
  window.addEventListener("hashchange", function () { readHash(); S.epoch++; paintRanges(); showSnap(); load(true); });
  // Sliding ranges move even when nothing is spent: reload when "hoy" crosses midnight or
  // a relative range's answer is over 5 minutes old (new calls reload sooner, by version).
  var loadedDay = new Date().toDateString();
  setInterval(function () {
    paintStatus(); paintEcgText();
    if (S.custom || !S.loadedAt || loading) return;
    var day = new Date().toDateString();
    if ((S.range === "hoy" && day !== loadedDay) || (S.range !== "todo" && Date.now() - S.loadedAt > 300000)) {
      loadedDay = day;
      load();
    }
  }, 15000);

  // -------------------------------------------------------------------------------------
  // Cursor's live calls: Cursor reports usage every few minutes, so a project whose cost
  // grew since the last summary of the same view sends a spark from Cursor's card.
  // -------------------------------------------------------------------------------------
  var cursorPrev = null;
  function cursorSparks(d) {
    var key = S.epoch + "|" + (S.custom ? "custom" : S.range);
    var projects = S.cursor && d.cursor ? d.cursor.projects : null;
    // Only two summaries from the server are compared: a stored snapshot can be hours old.
    // ... and only data that answers the view on screen (not the previous range's, still
    // shown while the new one loads).
    if (!projects || !includesNow() || S.dataFromSnap || S.dataKey !== rangeKey()) { cursorPrev = null; return; }
    var grown = cursorPrev && cursorPrev.key === key ? F.cursorGrowth(cursorPrev.map, projects) : [];
    var map = {};
    projects.forEach(function (p) { map[p.name] = { usd: p.usd || 0, tokens: p.tokens || 0 }; });
    cursorPrev = { key: key, map: map };
    grown.slice(0, 8).forEach(function (g, i) {
      setTimeout(function () {
        eco.spark({ project: "cursor:" + g.name, cursor: true, tokens: g.tokens, usd: g.usd, t: Date.now() });
        ecg.beat(g.tokens);
      }, i * 450);
    });
  }

  // -------------------------------------------------------------------------------------
  // Accounts (L-108): optional cards from a local launcher (config `accounts_url`), read
  // through the server every minute. Colours per tag come from config `account_colors`.
  // -------------------------------------------------------------------------------------
  var QA = { data: null, seen: {} };  // seen: tag -> last live call (ms), between two reads
  // The card a Claude call leaves from: its own account's, else the active one.
  function accountCardOf(tag) {
    var list = $("q-list"), cards = list ? list.querySelectorAll('.qcard[data-kind="claude"]') : [];
    for (var i = 0; tag && i < cards.length; i++) if (cards[i].dataset.tag === tag) return cards[i];
    return list ? list.querySelector(".qcard[data-active]") : null;
  }
  // Active: the account new chats start on (the launcher's flag). Outgoing: another
  // account that still made calls in the last `recent_ms` (chats opened before a switch).
  function lastUse(tag) {
    var d = QA.data, u = d && d.use && d.use[tag];
    return Math.max(u ? u.last : 0, QA.seen[tag] || 0);
  }
  function accountRole(a) {
    if (a.active) return "active";
    var win = (QA.data && QA.data.recent_ms) || 1800000;
    return lastUse(a.tag) > Date.now() - win ? "outgoing" : "";
  }
  function noteUse(ev) {
    if (ev.cursor || !ev.account || !QA.data) return;
    var acc = QA.data.claude && QA.data.claude.accounts || [];
    var a = acc.filter(function (x) { return x.tag === ev.account; })[0];
    var before = a ? accountRole(a) : "";
    QA.seen[ev.account] = Math.max(QA.seen[ev.account] || 0, ev.t || Date.now());
    if (a && accountRole(a) !== before) renderAccounts();
  }
  var TAG_PALETTE = ["#d81b4a", "#2f6fec", "#8e44d6", "#e08a00", "#16a085"];
  function tagColor(a, i) {
    var own = QA.data && QA.data.colors;
    return (own && own[a.tag]) || TAG_PALETTE[i % TAG_PALETTE.length];
  }
  function loadAccounts(once) {
    getJSON("/api/accounts").then(function (d) {
      var wasOnly = onlyCursor();
      QA.data = d; renderAccounts();
      if (!$("sheet").hidden && SH.p) renderSheet();  // account chips take the cards' colours
      // No account source at all: no card carries Cursor's eye, so Cursor is always shown
      // (a browser that hid it once would have no switch left: audit of 86).
      if (d && d.error === "no account source" && !S.cursor) setShown("cursor", true);
      else if (S.data && onlyCursor() !== wasOnly) render();  // the river follows the cards
    })
      .catch(function () {})
      .then(function () { if (once !== true) setTimeout(loadAccounts, document.hidden ? 180000 : 60000); });
  }
  function node(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function whenText(iso, bareToday) {
    var ms = Date.parse(iso);
    if (!isFinite(ms)) return "";
    var d = new Date(ms), now = new Date();
    var hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (d.toDateString() === now.toDateString()) return bareToday ? hm : say("when.today", { time: hm });
    var t = new Date(now); t.setDate(t.getDate() + 1);
    if (d.toDateString() === t.toDateString()) return say("when.tomorrow", { time: hm });
    return F.date(ms, { time: true });
  }
  function qRow(label, pct, sev, sub, tip) {
    var row = node("div", "q-row");
    if (sev) row.dataset.sev = sev;
    var bar = node("span", "q-bar"), fill = node("i");
    fill.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
    bar.appendChild(fill);
    row.appendChild(node("span", "lab", label));
    row.appendChild(bar);
    // Spanish puts a no-break space before % ("100 %" stays on one line); English none.
    row.appendChild(node("span", "pct", pct == null ? "–" : Math.round(pct) + (I.getLang() === "es" ? "\u00a0%" : "%")));
    var t = node("span", "q-sub", sub || "");
    if (tip) t.title = tip;
    row.appendChild(t);
    return row;
  }
  function winRow(label, w) {
    if (!w) return qRow(label, null, "", "");
    var left = w.resets_at ? Date.parse(w.resets_at) - Date.now() : NaN;
    if (isFinite(left) && left <= 0) return qRow(label, null, "", say("quota.reset"));
    var sub = w.locked ? say("quota.locked") : isFinite(left) ? F.untilText(left) : "";
    var sev = w.severity === "critical" || w.percent >= 90 ? "critical" : w.severity === "warning" || w.percent >= 70 ? "warning" : "";
    return qRow(label, w.percent, sev, sub, !w.locked && isFinite(left) ? say("quota.resets.in", { left: sub }) : "");
  }
  function accountCard(a, stale, i) {
    var c = node("article", "qcard");
    c.style.setProperty("--tag", tagColor(a, i || 0));
    c.dataset.kind = "claude";
    c.dataset.tag = a.tag || "";
    var role = accountRole(a);
    if (a.active) c.dataset.active = "1";
    else if (role === "outgoing") c.dataset.outgoing = "1";
    if (stale || a.source === "stale") c.dataset.stale = "1";
    var head = node("div", "q-head"), who = node("span", "q-who");
    head.appendChild(node("span", "q-tag", (a.tag || "?").slice(0, 3)));
    var nm = node("span", "q-name", a.label || a.tag);
    nm.appendChild(node("span", "q-plan", " " + (a.plan || "")));
    who.appendChild(nm);
    head.appendChild(who);
    if (role === "active") head.appendChild(node("span", "q-active", say("quota.active")));
    else if (role === "outgoing") {
      var out = node("span", "q-active q-out", say("quota.outgoing"));
      out.title = say("quota.outgoing.tip");
      head.appendChild(out);
    }
    c.appendChild(head);
    c.appendChild(winRow("5 h", a.session));
    c.appendChild(winRow(say("quota.week"), a.weekly));
    var w = a.warmup;
    if (w && (w.last_at || w.next_at)) {
      var warm = node("div", "q-warm", w.last_at ? say("quota.warmed", { when: whenText(w.last_at) }) : say("quota.cold"));
      if (w.next_at) warm.title = say("quota.next.warm", { when: whenText(w.next_at) });
      warm.dataset.kind = w.outcome === "blocked" ? "blocked" : w.outcome === "error" ? "error" : "ok";
      c.appendChild(warm);
    }
    if (a.note) c.appendChild(node("div", "q-warm", a.note));
    c.title = (a.label || "") + " · " + (a.plan || "");
    return c;
  }
  function cursorCard(u) {
    var c = node("article", "qcard");
    c.style.setProperty("--tag", S.theme.cursor);
    c.dataset.kind = "cursor";
    if (u.stale || u.error) c.dataset.stale = "1";
    var head = node("div", "q-head"), who = node("span", "q-who");
    head.appendChild(node("span", "q-tag", "CU"));
    var cn = node("span", "q-name", "Cursor");
    var plan = u.membership ? u.membership.charAt(0).toUpperCase() + u.membership.slice(1) : "";
    cn.appendChild(node("span", "q-plan", " " + plan));
    who.appendChild(cn);
    c.title = [u.team, plan].filter(Boolean).join(" · ");
    head.appendChild(who);
    c.appendChild(head);
    if (u.spend_usd != null) {
      var amt = node("span", "q-amount", money(u.spend_usd) + " ");
      amt.appendChild(node("small", null, say("quota.period")));
      c.appendChild(amt);
    }
    if (u.included) {
      var inc = u.included.percent;
      c.appendChild(qRow(say("quota.included"), inc, inc >= 90 ? "critical" : inc >= 70 ? "warning" : "", null));
    }
    if (u.team_pool && u.team_pool.limit_usd) {
      var pool = u.team_pool.used_usd / u.team_pool.limit_usd * 100;
      c.appendChild(qRow(say("quota.team"), pool, pool >= 90 ? "critical" : pool >= 70 ? "warning" : "", null));
    }
    var end = Date.parse(u.cycle_end);
    if (isFinite(end)) amtEnd(c, end);
    return c;
  }
  // The period's end goes on the amount line ("this period · 4 d 9 h").
  function amtEnd(c, end) {
    var a = c.querySelector(".q-amount small");
    if (!a) return;
    a.textContent = end > Date.now() ? say("quota.period.left", { left: F.untilText(end - Date.now()) }) : say("quota.period.new");
    a.title = say("quota.period.tip", { left: F.untilText(Math.max(0, end - Date.now())) });
  }
  // L-158..L-162: what every card adds: the eye switch, its share of the range's spend and
  // its login's expiry (Claude: with a renewal button).
  function cursorCardShown() {
    var c = QA.data && QA.data.cursor;
    return !!(c && c.installed !== false && (c.ok || c.spend_usd != null));
  }
  // {usd, share} of an origin ("acct:<tag>", "cursor", or "rest": calls of no account card)
  // in the range on screen, over everything spent in it, hidden accounts included.
  function spendOf(key) {
    var d = S.data, bo = d && d.by_origin;
    if (!bo) return null;
    var cz = d.cursor ? d.cursor.totals.usd : d.cursor_totals ? d.cursor_totals.usd : 0;
    var all = cz, rest = 0, tags = {};
    claudeTags().forEach(function (t) { tags["acct:" + t] = true; });
    Object.keys(bo).forEach(function (k) { all += bo[k].usd || 0; if (!tags[k]) rest += bo[k].usd || 0; });
    var usd = key === "cursor" ? cz : key === "rest" ? rest : (bo[key] ? bo[key].usd : 0);
    return { usd: usd, share: all > 0 ? usd / all : 0 };
  }
  var SVGNS = "http://www.w3.org/2000/svg";
  function eyeIcon(open) {
    var svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true");
    var paths = ["M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 1 0 0-6z"];
    if (!open) paths.push("M4 4l16 16");
    paths.forEach(function (dd) { var pa = document.createElementNS(SVGNS, "path"); pa.setAttribute("d", dd); svg.appendChild(pa); });
    return svg;
  }
  function dressCard(card, tag, name, shown, spend, login) {
    var head = card.querySelector(".q-head");
    var eye = node("button", "q-eye");
    eye.type = "button";
    eye.dataset.eye = tag;
    eye.setAttribute("aria-pressed", String(shown));
    eye.title = say(shown ? "eye.hide" : "eye.show", { name: name });
    eye.setAttribute("aria-label", eye.title);
    eye.appendChild(eyeIcon(shown));
    head.appendChild(eye);
    if (!shown) card.dataset.hidden = "1";
    if (spend) water(card, tag, spend);
    if (login) card.appendChild(loginLine(tag, login));
  }
  // L-174: the card's share of the range's spend is water in its colour, filling the card
  // from the bottom, with a gentle surface and a few bubbles; the figure sits small in the
  // bottom corner. Cards are rebuilt on every refresh, so the level starts from the last one
  // drawn (it only moves when the share changes) and every loop keeps its phase.
  var QW = {};  // tag -> last level drawn (percent)
  function phase(el, ms, off) { el.style.animationDelay = -Math.round((performance.now() + (off || 0)) % ms) + "ms"; }
  function water(card, tag, spend) {
    var pct = Math.max(0, Math.min(100, spend.share * 100));
    var w = node("div", "q-water");
    w.setAttribute("aria-hidden", "true");
    var from = QW[tag] == null ? 0 : QW[tag];
    w.style.setProperty("--lvl", from + "%");
    if (pct < 6) w.dataset.low = "1";
    if (spend.share <= 0) w.dataset.empty = "1";  // no spend: no surface either (audit of 95)
    var front = node("i", "q-wave"), back = node("i", "q-wave q-wave-back");
    phase(front, 7000); phase(back, 11000, 3000);
    w.appendChild(back); w.appendChild(front);
    for (var i = 0; i < 4; i++) {
      var b = node("i", "q-bub"), h = hash(tag + "b" + i), dur = 5200 + Math.round(h * 4200);
      b.style.left = (12 + i * 21 + Math.round(hash(tag + "x" + i) * 12)) + "%";
      b.style.setProperty("--s", (2 + Math.round(h * 2.4)) + "px");
      b.style.animationDuration = dur + "ms";
      phase(b, dur, Math.round(h * dur));
      w.appendChild(b);
    }
    card.insertBefore(w, card.firstChild);
    card.dataset.water = "1";
    if (from !== pct) requestAnimationFrame(function () { requestAnimationFrame(function () { w.style.setProperty("--lvl", pct + "%"); }); });
    QW[tag] = pct;
    var tip = say("quota.use.tip", { pct: F.pct(spend.share, 1), money: money(spend.usd) });
    var label = pct > 0 && pct < 1 ? "<1" + (I.getLang() === "es" ? "\u00a0%" : "%") : F.pct(spend.share, 0);
    var tag2 = node("span", "q-water-pct", label);
    tag2.title = tip;
    // A plain span ignores aria-label: as an image it is read with its context (audit of 95).
    tag2.setAttribute("role", "img");
    tag2.setAttribute("aria-label", say("quota.use") + ": " + tip);
    card.appendChild(tag2);
  }
  function loginOf(tag) {
    var l = QL.data && QL.data.claude || [];
    for (var i = 0; i < l.length; i++) if (l[i].tag === tag) return l[i];
    return null;
  }
  function loginLine(tag, L) {
    var row = node("div", "q-login"), exp = Date.parse(L.expires_at || ""), now = Date.now();
    var state = L.state || "unknown";
    if (state === "ok" && isFinite(exp) && exp <= now) state = "expired";  // passed since the last read
    row.dataset.state = state;
    var txt = state === "missing" ? say("login.missing") : state === "wrong" ? say("login.wrong")
      : state === "expired" ? say("login.expired") + (isFinite(exp) ? " · " + F.date(exp) : "")
      : isFinite(exp) ? say("login.until", { when: F.date(exp), left: F.untilText(exp - now) }) : say("login.unknown");
    row.appendChild(node("span", "q-login-text", txt));
    var tip = isFinite(exp) ? say("login.tip", { when: F.date(exp, { time: true }) }) : "";
    if (tag === "cursor") {
      row.title = [tip, say("login.cursor.tip")].filter(Boolean).join(" · ");
    } else {
      row.title = tip;
      if (QL.data && QL.data.enabled) {
        var b = node("button", "q-renew", say(L.renewing ? "login.renewing" : "login.renew"));
        b.type = "button";
        b.dataset.renew = tag;
        b.disabled = !!L.renewing;
        if (state !== "ok") b.dataset.due = "1";
        b.title = say("login.renew.tip");
        row.appendChild(b);
      }
    }
    return row;
  }
  var QL = { data: null, timer: 0 };
  function loadLogins() {
    clearTimeout(QL.timer);
    return getJSON("/api/logins").then(function (d) { QL.data = d; renderAccounts(); })
      .catch(function () {}).then(function () {
        if (QL.data && !QL.data.enabled && !QL.data.cursor) return;  // nothing to show
        var busy = QL.data && (QL.data.claude || []).some(function (a) { return a.renewing; });
        QL.timer = setTimeout(loadLogins, busy ? 5000 : document.hidden ? 180000 : 60000);
      });
  }
  function renewLogin(tag) {
    var l = loginOf(tag);
    if (l) { l.renewing = true; renderAccounts(); }  // instant; the server confirms
    postJSON("/api/logins/renew", { tag: tag }).then(function () {
      setNote(say("login.opened", { tag: tag }), "busy");
    }).catch(function (e) {
      setNote(e.code === "busy" ? say("login.busy") : say("login.err", { err: e.message }), "error");
    }).then(loadLogins);
  }
  $("q-list").addEventListener("click", function (e) {
    var eye = e.target.closest && e.target.closest("button[data-eye]");
    if (eye) { e.stopPropagation(); return setShown(eye.dataset.eye, eye.getAttribute("aria-pressed") !== "true"); }
    var rn = e.target.closest && e.target.closest("button[data-renew]");
    if (rn && !rn.disabled) { e.stopPropagation(); renewLogin(rn.dataset.renew); }
  });
  function renderAccounts() {
    var d = QA.data, list = $("q-list");
    if (!d) return;
    if (QC.drag) { QC.dirty = true; return; }  // never rebuild under a dragged card
    // Keyboard focus survives the rebuild (audit of 78).
    var fa = document.activeElement, inList = fa && fa.closest && list.contains(fa);
    var focusTag = inList && fa.dataset.tag, focusBtn = inList && (fa.dataset.eye ? 'button[data-eye="' + fa.dataset.eye + '"]'
      : fa.dataset.renew ? 'button[data-renew="' + fa.dataset.renew + '"]' : null);
    list.textContent = "";
    var claude = d.claude && d.claude.accounts ? d.claude.accounts.slice() : [];
    var ctl = QC.data && QC.data.enabled && !QC.data.error_code ? QC.data : null;
    var colorOf = {};
    claude.forEach(function (a, i) { colorOf[a.tag] = tagColor(a, i); });
    if (ctl && ctl.order) {
      // L-148: the cards follow the priority the auto switch weighs.
      claude.sort(function (a, b) { return rankOf(a.tag) - rankOf(b.tag); });
    }
    if (claude.length && (ctl || cursorCardShown())) {
      var g = node("div", "q-group", say(ctl ? "control.group.claude" : "control.group.claude.plain"));
      if (ctl) g.title = say("control.group.tip");
      list.appendChild(g);
    }
    claude.forEach(function (a) {
      // The control knows the switch first: the launcher's flag can lag a minute, and the
      // border and the badge must sit on the same card (audit of 77).
      if (ctl && ctl.active) a = Object.assign({}, a, { active: a.tag === ctl.active });
      var card = accountCard(a, d.claude.stale, 0);
      card.style.setProperty("--tag", colorOf[a.tag]);
      if (ctl) decorateCard(card, a, ctl);
      dressCard(card, a.tag, a.label || a.tag, !S.hidden[a.tag], spendOf("acct:" + a.tag), loginOf(a.tag));
      list.appendChild(card);
    });
    if (cursorCardShown()) {
      if (claude.length) list.appendChild(node("div", "q-group q-sep", say("control.group.others")));
      var cc = cursorCard(d.cursor);
      dressCard(cc, "cursor", "Cursor", S.cursor, spendOf("cursor"), QL.data && QL.data.cursor);
      list.appendChild(cc);
    }
    var rest = spendOf("rest");
    if (rest && rest.share > 0.005 && list.querySelector(".qcard")) {
      var rn = node("div", "q-rest", say("quota.rest", { pct: F.pct(rest.share, rest.share < 0.01 ? 1 : 0), money: money(rest.usd) }));
      rn.title = say("quota.rest.tip");
      list.appendChild(rn);
    }
    var shown = list.querySelector(".qcard") !== null;
    if ($("quota").hidden === shown) { $("quota").hidden = !shown; if (S.data) eco.relabel(); }
    var at = d.claude && d.claude.updated_at ? Date.parse(d.claude.updated_at) : d.at;
    $("q-when").textContent = at ? say("quota.updated", { when: whenText(new Date(at).toISOString(), true) }) : "";
    paintNote();
    if (focusBtn) {
      var btn = list.querySelector(focusBtn);  // the eye or Renew button keeps the focus (audit of 86)
      if (btn) btn.focus({ preventScroll: true });
    } else if (focusTag) {
      var back = list.querySelector('.qcard[data-control][data-tag="' + focusTag + '"]');
      if (back) back.focus({ preventScroll: true });
    }
    if (QC.menu && QC.opener && !document.contains(QC.opener)) {
      QC.opener = list.querySelector('.qcard[data-tag="' + QC.opener.dataset.tag + '"]') || null;
    }
  }

  // -------------------------------------------------------------------------------------
  // Account control (L-148): with config `account_control`, right-click (or long-press) a
  // Claude card to make it the active account or change its priority; drag the cards to
  // reorder them. The server only runs the configured command, after these clicks.
  // -------------------------------------------------------------------------------------
  var QC = { data: null, timer: 0, drag: null, dirty: false, note: null, menu: null, wasBusy: false,
             mine: 0, dragged: 0, opener: null, req: 0, cut: 0 };
  // The account being switched to: the one just launched until the command reports it,
  // then the command's own run (audit of 79).
  function runningTag(ctl) {
    var l = ctl && ctl.launch;
    return l && l.pending ? l.tag : ((ctl && ctl.activation) || {}).tag;
  }
  function rankOf(tag) {
    var o = QC.data && QC.data.order || [], i = o.indexOf(tag);
    return i < 0 ? 99 : i;
  }
  function ctlAccount(tag) {
    return (QC.data && QC.data.accounts || []).filter(function (x) { return x.tag === tag; })[0] || null;
  }
  function decorateCard(card, a, ctl) {
    var info = ctlAccount(a.tag), head = card.querySelector(".q-head");
    var rank = node("span", "q-rank", String(rankOf(a.tag) + 1));
    rank.title = say("control.rank.tip", { n: rankOf(a.tag) + 1 });
    head.insertBefore(rank, head.firstChild);
    if (ctl.best === a.tag && ctl.active !== a.tag) {
      var b = node("span", "q-best", say("control.best"));
      b.title = say("control.best.tip");
      head.appendChild(b);
    }
    if (ctl.busy && runningTag(ctl) === a.tag) card.dataset.switching = "1";
    if (info && info.score != null) card.title += " · " + say("control.score", { score: info.score.toFixed(1) });
    card.dataset.control = "1";
    card.tabIndex = 0;
    card.setAttribute("aria-haspopup", "menu");
    card.setAttribute("aria-label", say("control.card.aria", { name: a.label || a.tag, n: rankOf(a.tag) + 1 }));
  }
  function loadControl(fresh) {
    clearTimeout(QC.timer);
    var id = ++QC.req;
    return getJSON("/api/claude-control" + (fresh ? "?fresh=1" : "")).then(function (d) {
      // An answer to a request sent before the last click knows nothing of that switch:
      // dropped, never read as its end (audit of 78).
      if (id <= QC.cut) return;
      var was = QC.wasBusy;
      QC.data = d;
      QC.wasBusy = !!d.busy;
      // Our run is forgotten after 40 min without an end seen (a restart lost a dead
      // launch): a later run from elsewhere is never announced as ours (audit of 80).
      if (QC.mine && Date.now() - QC.mineClick > 2400000) QC.mine = 0;
      // The "switching" note stays while our run lasts, not 12 s (audit of 80).
      if (d.busy && QC.mine) setNote(say("control.switching", { tag: runningTag(d) || "?" }), "busy");
      if (was && !d.busy) { finishedNote(d); loadAccounts(true); }
      renderAccounts();
    }).catch(function () {}).then(function () {
      if (id < QC.req) return;  // a newer request owns the timer
      // A failed first read (server restarting) keeps trying; only "not configured" stops.
      if (QC.data && !QC.data.enabled) return;
      QC.timer = setTimeout(function () { loadControl(QC.wasBusy); }, QC.wasBusy ? 2000 : document.hidden ? 180000 : 30000);
    });
  }
  function setNote(text, kind) {
    QC.note = text ? { text: text, kind: kind || "", until: Date.now() + 12000 } : null;
    paintNote();
    if (text) setTimeout(paintNote, 12100);
  }
  function paintNote() {
    var n = $("q-note");
    if (!n) return;
    var on = QC.note && QC.note.until > Date.now();
    n.hidden = !on;
    n.textContent = on ? QC.note.text : "";
    n.dataset.kind = on ? QC.note.kind : "";
  }
  function finishedNote(d) {
    if (!QC.mine) return;
    var act = d.activation || {}, launch = d.launch || {};
    // The server judges its own launch (no viewer clock involved, audit of 78).
    if (launch.failed && launch.started_ms === QC.mine) { QC.mine = 0; setNote(say("control.err.launch"), "error"); return; }
    if (act.phase !== "done") return;
    // Ours: the launch this server reports, or after a restart of Latidos (no launch kept)
    // a run the command started after our click; both times are the server machine's
    // (audit of 79).
    var ours = launch.started_ms ? launch.started_ms === QC.mine
                                 : QC.mine > 0 && Date.parse(act.started_at || "") >= QC.mine - 500;
    if (!ours) return;
    QC.mine = 0;
    if (act.ok) {
      var t = act.tabs || {}, moved = (t.continued || 0) + (t.finished || 0) + (t.old_error || 0);
      setNote(say("control.done", { tag: act.tag, n: moved }) + (t.left ? " " + say("control.done.left", { n: t.left }) : ""), "ok");
    } else {
      // "busy" from the command: the auto switch held its lock too long, nothing changed.
      var key = { busy: "control.err.lockwait", stale: "control.err.stale", crashed: "control.err.crashed" }[act.error] || "control.err.switch";
      setNote(say(key, { tag: act.tag || "?" }), "error");
    }
  }
  // Server errors in the page's language, by code (audit of 77).
  function errText(e, key) {
    if (e.code === "busy") return say("control.err.busy", { tag: e.tag || "?" });
    if (e.code === "no_control") return say("control.off");
    if (!e.status) return say(key, { msg: say("control.err.network") });
    return say(key, { msg: "HTTP " + e.status });
  }
  function activate(tag) {
    closeMenu();
    setNote(say("control.switching", { tag: tag }), "busy");
    QC.wasBusy = true;
    if (QC.data) { QC.data.busy = true; QC.data.activation = { tag: tag, phase: "switching" }; renderAccounts(); }
    QC.mine = -1;          // until the server says which launch is ours
    QC.mineClick = Date.now();
    QC.cut = QC.req;       // answers to earlier requests are ignored
    clearTimeout(QC.timer);
    postJSON("/api/claude-control/activate", { tag: tag }).then(function (r) { QC.mine = r.started_ms || -1; loadControl(true); })
      // Accepted (audit of 80): a Latidos restart between the launch and this answer reads
      // as a network error here although the run goes on; the pulsing card still shows it.
      .catch(function (e) { QC.wasBusy = false; QC.mine = 0; setNote(errText(e, "control.err.start"), "error"); loadControl(true); });
  }
  function saveOrder(order) {
    var prev = QC.data && QC.data.order;
    if (QC.data) { QC.data.order = order; renderAccounts(); }  // instant, the server confirms
    postJSON("/api/claude-control/order", { order: order }).then(function (r) {
      if (QC.data) QC.data.order = r.order || order;
      setNote(say("control.order.saved", { order: (r.order || order).join(" › ") }), "ok");
      loadControl(true);
    }).catch(function (e) {
      if (QC.data && prev) { QC.data.order = prev; renderAccounts(); }
      setNote(errText(e, "control.err.order"), "error");
    });
  }
  function moveTag(tag, to) {
    var o = (QC.data && QC.data.order || []).slice(), i = o.indexOf(tag);
    if (i < 0) return;
    o.splice(i, 1);
    o.splice(Math.max(0, Math.min(o.length, to)), 0, tag);
    saveOrder(o);
  }

  // The menu.
  function closeMenu(refocus) {
    if (QC.menu) { QC.menu.remove(); QC.menu = null; }
    if (refocus === true && QC.opener && document.contains(QC.opener)) QC.opener.focus({ preventScroll: true });
    QC.opener = null;
  }
  function menuItem(m, label, fn, opts) {
    opts = opts || {};
    var b = node("button", "q-mi" + (opts.cls ? " " + opts.cls : ""), label);
    b.type = "button";
    b.setAttribute("role", "menuitem");
    if (opts.disabled) { b.disabled = true; if (opts.why) b.title = opts.why; }
    else b.addEventListener("click", function (e) {
      e.stopPropagation();
      var tag = QC.opener && QC.opener.dataset.tag;
      fn();
      // The action re-renders the cards: find the opener again by its account (audit of 79).
      var back = tag && $("q-list").querySelector('.qcard[data-control][data-tag="' + tag + '"]');
      if (back) back.focus({ preventScroll: true });
    });
    m.appendChild(b);
    return b;
  }
  function openMenu(card, x, y) {
    closeMenu();
    QC.opener = card;
    var m = node("div", "q-menu");
    m.setAttribute("role", "menu");
    var ctl = QC.data && QC.data.enabled ? QC.data : null;
    if (card.dataset.kind === "claude") {
      var tag = card.dataset.tag, info = ctlAccount(tag), acc = (QA.data.claude.accounts || []).filter(function (a) { return a.tag === tag; })[0] || {};
      m.appendChild(node("div", "q-mh", (acc.label || tag) + (acc.plan ? " · " + acc.plan : "")));
      if (!QC.data) {
        m.appendChild(node("div", "q-mx", say("control.err.state")));
      } else if (!ctl) {
        m.appendChild(node("div", "q-mx", say("control.off")));
      } else if (ctl.error_code) {
        m.appendChild(node("div", "q-mx", say("control.err.state")));
      } else {
        var isActive = ctl.active === tag, busy = !!ctl.busy;
        menuItem(m, isActive ? say("control.menu.active") : say("control.menu.activate", { tag: acc.label || tag }),
                 function () { activate(tag); },
                 { cls: "q-mi-main", disabled: isActive || busy || (info && !info.usable && !info.fresh),
                   why: busy ? say("control.err.busy", { tag: runningTag(ctl) || "?" }) : "" });
        if (info && info.spent && !isActive) m.appendChild(node("div", "q-mx q-warnline", say("control.menu.spent")));
        var r = rankOf(tag), n = (ctl.order || []).length;
        m.appendChild(node("div", "q-msep"));
        menuItem(m, say("control.menu.first"), function () { closeMenu(); moveTag(tag, 0); }, { disabled: r === 0 });
        menuItem(m, say("control.menu.up"), function () { closeMenu(); moveTag(tag, r - 1); }, { disabled: r === 0 });
        menuItem(m, say("control.menu.down"), function () { closeMenu(); moveTag(tag, r + 1); }, { disabled: r >= n - 1 });
        menuItem(m, say("control.menu.last"), function () { closeMenu(); moveTag(tag, n - 1); }, { disabled: r >= n - 1 });
        m.appendChild(node("div", "q-msep"));
        var lines = [say("control.menu.rank", { n: r + 1, of: n, w: info ? info.weight.toFixed(2) : "?" })];
        if (info) {
          lines.push(info.score == null ? say("control.menu.noscore")
                     : say("control.menu.score", { score: info.score.toFixed(1),
                                                    week: info.weekly_room == null ? "?" : Math.round(info.weekly_room),
                                                    s: info.session_room == null ? "?" : Math.round(info.session_room) }));
        }
        if (ctl.best) lines.push(say("control.menu.best", { tag: ctl.best }));
        lines.push(say(ctl.auto && ctl.auto.enabled ? "control.menu.auto.on" : "control.menu.auto.off"));
        if (ctl.order_source === "env") lines.push(say("control.menu.env"));
        lines.forEach(function (l) { m.appendChild(node("div", "q-mx", l)); });
        if (matchMedia("(pointer: fine)").matches) m.appendChild(node("div", "q-mx q-hint", say("control.menu.drag")));
      }
    } else {
      m.appendChild(node("div", "q-mh", "Cursor"));
      m.appendChild(node("div", "q-mx", say("control.menu.cursor")));
    }
    document.body.appendChild(m);
    var w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(x, innerWidth - w - 8)) + "px";
    m.style.top = Math.max(8, Math.min(y, innerHeight - h - 8)) + "px";
    QC.menu = m;
    var first = m.querySelector("button:not([disabled])");
    if (first) first.focus({ preventScroll: true });
  }
  document.addEventListener("pointerdown", function (e) { if (QC.menu && !QC.menu.contains(e.target)) closeMenu(); }, true);
  document.addEventListener("keydown", function (e) {
    if (!QC.menu) return;
    if (e.key === "Escape") { closeMenu(true); e.stopPropagation(); e.preventDefault(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      var items = Array.prototype.slice.call(QC.menu.querySelectorAll("button:not([disabled])"));
      var i = items.indexOf(document.activeElement);
      var nx = items[(i + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length];
      if (nx) nx.focus();
      e.preventDefault();
      e.stopPropagation();
    }
  }, true);
  addEventListener("resize", closeMenu);
  addEventListener("scroll", function () { closeMenu(); }, true);
  // Keyboard: the context-menu key, Shift+F10 or Enter on a focused card opens its menu.
  $("q-list").addEventListener("keydown", function (e) {
    var card = e.target.closest && e.target.closest('.qcard[data-control]');
    if (!card || e.target !== card) return;
    if (e.key === "ContextMenu" || e.key === "Enter" || (e.key === "F10" && e.shiftKey)) {
      var r = card.getBoundingClientRect();
      e.preventDefault();
      openMenu(card, r.left + 16, r.top + 24);
    }
  });
  addEventListener("blur", closeMenu);
  $("q-list").addEventListener("contextmenu", function (e) {
    var card = e.target.closest(".qcard");
    if (!card) return;
    e.preventDefault();
    openMenu(card, e.clientX, e.clientY);
  });

  // Drag to reorder, and long-press for the menu on touch screens (no contextmenu there).
  $("q-list").addEventListener("pointerdown", function (e) {
    if (e.target.closest("button")) return;  // the eye and Renew buttons are not a drag
    var card = e.target.closest('.qcard[data-control]');
    if (!card || (e.pointerType === "mouse" && e.button !== 0)) return;
    var r0 = card.getBoundingClientRect();
    var st = { card: card, id: e.pointerId, x: e.clientX, y: e.clientY, on: false, press: 0,
               touch: e.pointerType === "touch", y0top: r0.top, y0bottom: r0.bottom };
    if (e.pointerType !== "mouse") {
      st.press = setTimeout(function () { if (!st.on) { QC.drag = null; flushDirty(); openMenu(card, st.x, st.y); } }, 550);
    }
    QC.drag = st;
  });
  function cardsInOrder() {
    return Array.prototype.slice.call($("q-list").querySelectorAll('.qcard[data-control]'));
  }
  function flushDirty() { if (QC.dirty) { QC.dirty = false; renderAccounts(); } }
  addEventListener("pointermove", function (e) {
    var st = QC.drag;
    if (!st || e.pointerId !== st.id) return;
    var dx = e.clientX - st.x, dy = e.clientY - st.y;
    if (!st.on) {
      if (Math.hypot(dx, dy) < 7) return;
      clearTimeout(st.press);
      // Touch: a move is a scroll of the panel; reorder from the long-press menu instead.
      if (st.touch) { QC.drag = null; flushDirty(); return; }
      st.on = true;
      st.card.dataset.dragging = "1";
      closeMenu();
      try { st.card.setPointerCapture(st.id); } catch (_) {}
    }
    e.preventDefault();
    st.card.style.transform = "translate(" + dx + "px," + dy + "px)";
    // The slot: the card whose centre is nearest the pointer.
    var best = null, bd = Infinity;
    cardsInOrder().forEach(function (c) {
      if (c === st.card) return;
      var r = c.getBoundingClientRect(), d = Math.hypot(e.clientX - (r.left + r.width / 2), e.clientY - (r.top + r.height / 2));
      if (d < bd) { bd = d; best = c; }
      delete c.dataset.dropBefore; delete c.dataset.dropAfter; delete c.dataset.dropSide;
    });
    st.target = null;
    if (best) {
      var rb = best.getBoundingClientRect();
      // Side by side in the two-column grid (same row as the card's start): the left or
      // right half decides, not the top or bottom (audit of 77).
      var sameRow = Math.abs((rb.top + rb.bottom) / 2 - (st.y0top + st.y0bottom) / 2) < rb.height / 2;
      var before = sameRow ? e.clientX < rb.left + rb.width / 2 : e.clientY < rb.top + rb.height / 2;
      best.dataset[before ? "dropBefore" : "dropAfter"] = "1";
      if (sameRow) best.dataset.dropSide = "1";
      st.target = { tag: best.dataset.tag, before: before };
    }
  }, { passive: false });
  function endDrag(e) {
    var st = QC.drag;
    if (!st || (e && e.pointerId !== st.id)) return;
    clearTimeout(st.press);
    QC.drag = null;
    cardsInOrder().forEach(function (c) { delete c.dataset.dropBefore; delete c.dataset.dropAfter; delete c.dataset.dropSide; });
    if (!st.on) { flushDirty(); return; }
    st.card.style.transform = "";
    delete st.card.dataset.dragging;
    QC.dragged = Date.now();
    var tag = st.card.dataset.tag, t = st.target;
    if (t && e && e.type === "pointerup") {
      var o = (QC.data.order || []).filter(function (x) { return x !== tag; }), i = o.indexOf(t.tag);
      if (i >= 0) {
        o.splice(t.before ? i : i + 1, 0, tag);
        if (o.join() !== (QC.data.order || []).join()) { QC.dirty = false; saveOrder(o); return; }
      }
    }
    flushDirty();
  }
  addEventListener("pointerup", endDrag);
  addEventListener("pointercancel", endDrag);
  // A drag must not end as a click on the card.
  $("q-list").addEventListener("click", function (e) {
    if (Date.now() - QC.dragged < 400 && e.target.closest(".qcard[data-control]")) { e.stopPropagation(); e.preventDefault(); }
  }, true);

  // -------------------------------------------------------------------------------------
  // Boot
  // -------------------------------------------------------------------------------------
  I.apply(document);
  paintLang();
  readHash();
  setTheme(themeMode());
  resizeAll();
  paintRanges();
  showSnap();
  load(true).then(function () { pollLive(); });
  loadAccounts();
  loadControl();
  loadLogins();
  requestAnimationFrame(loop);
})();
