/* Latidos: number, money and date formatting in the page's language (English or Spanish,
   web/i18n.js). Shared with format.test.js. */
(function (root) {
  "use strict";

  var I = typeof module !== "undefined" && module.exports ? require("./i18n.js") : root.LatidosI18n;
  function es() { return I.getLang() === "es"; }

  // Thousands separator of the current language: "1.234" in Spanish, "1,234" in English.
  function groupSep() { return es() ? "." : ","; }

  // Always group thousands (es-ES skips 4-digit numbers, which reads oddly in a column of
  // figures): a dot and a decimal comma in Spanish, a comma and a decimal point in English.
  function num(v, decimals) {
    decimals = decimals || 0;
    if (!isFinite(v)) return "–";
    var neg = v < 0;
    var s = Math.abs(v).toFixed(decimals);
    var parts = s.split(".");
    var int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, groupSep());
    return (neg ? "-" : "") + int + (parts[1] ? (es() ? "," : ".") + parts[1] : "");
  }

  // "1,2 mil" / "4,5 M" in Spanish, "1.2K" / "4.5M" in English.
  function tokens(v) {
    if (!isFinite(v)) return "–";
    var a = Math.abs(v), k = es() ? " mil" : "K", m = es() ? " M" : "M";
    if (a < 1000) return num(v, 0);
    if (a < 1e6) return num(v / 1e3, a < 1e4 ? 1 : 0) + k;
    if (a < 1e8) return num(v / 1e6, 1) + m;
    return num(v / 1e6, 0) + m;
  }

  // The symbol after the amount in Spanish ("6.400,86 €"), before it in English ("€6,400.86").
  function withSym(text, cur, neg) {
    var sym = cur === "USD" ? "$" : "€";
    return es() ? (neg ? "-" : "") + text + " " + sym : (neg ? "-" : "") + sym + text;
  }

  function money(v, cur) {
    if (!isFinite(v)) return "–";
    var a = Math.abs(v);
    if (a > 0 && a < 0.01) return es() ? withSym("<0,01", cur) : "<" + withSym("0.01", cur);
    return withSym(num(a, a >= 10000 ? 0 : 2), cur, v < 0);
  }

  // Whole units for narrow cells: "12 €" / "€12", under one "<1 €" / "<€1".
  function moneyRound(v, cur) {
    if (!isFinite(v)) return "–";
    if (Math.abs(v) < 1) return es() ? withSym("<1", cur) : "<" + withSym("1", cur);
    return withSym(num(Math.round(Math.abs(v))), cur, v < 0);
  }

  function pct(v, decimals) {
    if (!isFinite(v)) return "–";
    return num(v * 100, decimals || 0) + (es() ? " %" : "%");
  }

  function date(ms, opts, nowMs) {
    opts = opts || {};
    var d = new Date(ms);
    var now = nowMs || Date.now();
    var s = (opts.weekday ? I.list("fmt.days")[d.getDay()] + " " : "") + d.getDate() + " " + month(ms);
    if (now - ms > 300 * 86400000 || new Date(now).getFullYear() !== d.getFullYear() && opts.year !== false) {
      s += " " + d.getFullYear();
    }
    if (opts.time) s += " · " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    return s;
  }

  // Short month name of ms ("oct" / "Oct").
  function month(ms) { return I.list("fmt.months")[new Date(ms).getMonth()]; }

  function hour(ms) {
    var d = new Date(ms);
    return pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  function ago(ms, nowMs) {
    var s = Math.max(0, Math.round(((nowMs || Date.now()) - ms) / 1000));
    if (s < 45) return I.t("fmt.now");
    if (s < 3600) return I.t("fmt.min.ago", { n: Math.round(s / 60) });
    if (s < 86400) return I.t("fmt.h.ago", { n: Math.round(s / 3600) });
    var d = Math.round(s / 86400);
    return d === 1 ? I.t("fmt.yesterday") : I.t("fmt.days.ago", { n: d });
  }

  // Monday 00:00 local of the week holding ms, as YYYY-MM-DD (matches latidos.py week_key).
  function weekKey(ms) {
    var d = new Date(ms);
    d.setHours(0, 0, 0, 0);
    var wd = (d.getDay() + 6) % 7;
    d.setDate(d.getDate() - wd);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }

  // Same algorithm as latidos.py coverage(): replay every block of the history so weekly
  // balances carry over; blocks are [start, usd, usdInRange] (usdInRange defaults to usd).
  function coverage(blocks, planMain, proBlock, proWeek) {
    var capBlock = planMain * proBlock, capWeek = planMain * proWeek;
    var left = {}, wTake = {}, wUsd = {}, touched = {};
    var main = 0, total = 0, short = 0, count = 0;
    for (var i = 0; i < blocks.length; i++) {
      var start = blocks[i][0], usd = blocks[i][1];
      var part = blocks[i].length > 2 ? blocks[i][2] : usd;
      var wk = weekKey(start);
      var rem = wk in left ? left[wk] : capWeek;
      var take = Math.min(usd, capBlock, rem);
      left[wk] = rem - take;
      wTake[wk] = (wTake[wk] || 0) + take;
      wUsd[wk] = (wUsd[wk] || 0) + usd;
      if (!(part > 0) || !(usd > 0)) continue;
      touched[wk] = 1; count++;
      main += take * (part / usd);
      total += part;
      if (usd > take + 1e-9) short++;
    }
    var weeks = Object.keys(touched), wMain = 0, wRest = 0;
    weeks.forEach(function (w) { wMain += wTake[w]; wRest += wUsd[w] - wTake[w]; });
    return { main: main, total: total, short: short, blocks: count, weeks: weeks.length,
             share: total ? main / total : 0, weekMain: wMain, weekRest: wRest };
  }

  // Live calls shown on top of a summary: keep those the summary does not count yet. Matched
  // by ingest sequence (seqAt = last sequence the summary includes), never by timestamp:
  // calls reach the logs out of order.
  function pendingExtras(extras, seqAt) {
    return extras.filter(function (e) { return e.seq > (seqAt || 0); });
  }

  // An answer applies only to the range it was asked for.
  function answerApplies(askedKey, currentKey) { return askedKey === currentKey; }

  // A load asked while another runs is queued; its reset must survive the queue.
  function queueReset(pending, reset) { return !!pending || !!reset; }

  // Version the page should remember after a summary answer (a cached answer may be older
  // than one already seen; a range change takes the answer as it is).
  function nextVersion(seen, got, reset) {
    return reset || seen == null ? got : Math.max(seen, got);
  }

  // The server restarted (its live sequence starts again): drop what was numbered before.
  function bootChanged(known, boot) { return known != null && boot != null && known !== boot; }

  // Where the page starts reading live events after a summary: the last sequence the
  // summary counts (a cached summary may be behind the server's current sequence).
  function startSeq(d) { return d.seq_at != null ? d.seq_at : (d.seq || 0); }

  // After a summary answer: never read past what it counts (0 = nothing read yet).
  function rewindSeq(current, d) {
    var s = startSeq(d);
    return current > 0 ? Math.min(current, s) : s;
  }

  // Add a live call once, however many times a rewind replays it.
  function addExtra(extras, ev) {
    for (var i = 0; i < extras.length; i++) if (extras[i].seq === ev.seq) return extras;
    var out = extras.concat([{ seq: ev.seq, tokens: ev.tokens, usd: ev.usd, project: ev.project, session: ev.session, t: ev.t }]);
    return out.length > 500 ? out.slice(-500) : out;
  }

  // Spacing between live calls so a batch plays out evenly before the next poll lands:
  // never faster than minMs, never slower than maxMs.
  function pace(queued, msLeft, minMs, maxMs) {
    if (queued <= 0) return maxMs;
    return Math.max(minMs, Math.min(maxMs, msLeft / queued));
  }

  // Live cost per project from pending calls: {project: {tokens, usd}}.
  // The bubble a live Claude call lands in: its full project name when that bubble is on
  // screen, else what the summary's fold says (a grouped client project shows in its
  // client's bubble), else its client (a project new to the range starts grouped).
  function liveBubble(ev, data) {
    var name = ev.project, fold = data && data.fold;
    if (fold && Object.prototype.hasOwnProperty.call(fold, name)) return fold[name];
    var shown = data && data.projects && data.projects.some(function (p) { return p.name === name; });
    if (!shown && ev.client) return ev.client;
    return name;
  }
  function byProject(extras) {
    var out = {};
    extras.forEach(function (e) {
      var p = out[e.project] || (out[e.project] = { tokens: 0, usd: 0 });
      p.tokens += e.tokens; p.usd += e.usd;
    });
    return out;
  }

  // What a Cursor item counted when it has no cost: API requests if any, else local turns.
  function cursorCount(x) {
    var req = x.requests || 0, turns = x.turns || 0;
    if (req) return count("n.requests", req);
    return count("n.turns", turns);
  }

  // A counted noun in the current language: count("n.sessions", 3) = "3 sessions".
  function count(key, n) {
    return I.t(key, { n: n, nText: num(n) });
  }

  // How fresh a cell is, 1 (tokens in the last 10 minutes) down to 0 (a month idle), on a
  // log scale so every step reads apart: 1 h ~0.79, 6 h ~0.57, 1 day ~0.41, 4 days ~0.24,
  // 2 weeks ~0.09.
  var FRESH_MIN = 600000, FRESH_MAX = 30 * 86400000;
  function freshness(last, now) {
    if (!last) return 0;
    var age = now - last;
    if (age <= FRESH_MIN) return 1;
    return Math.max(0, 1 - Math.log(age / FRESH_MIN) / Math.log(FRESH_MAX / FRESH_MIN));
  }

  // How grey a cell is: 0 up to 10 min after its last call, 1 (fully grey) after a week,
  // log time in between and eased, so a few hours already show and a day is nearly grey.
  var STALE_MIN = 600000, STALE_MAX = 7 * 86400000;
  function staleness(last, now) {
    if (!last) return 1;
    var age = now - last;
    if (age <= STALE_MIN) return 0;
    if (age >= STALE_MAX) return 1;
    return Math.pow(Math.log(age / STALE_MIN) / Math.log(STALE_MAX / STALE_MIN), 0.8);
  }

  // The glow after a hit: a bright flare (~0.75 s) then a slow tail, visible for ~10 s,
  // so you can still tell which cell received tokens after looking away for a moment.
  var AFTERGLOW_MS = 11000;
  function afterglow(age) {
    if (!(age >= 0) || age >= AFTERGLOW_MS) return 0;
    var tail = 0.38 * Math.pow(1 - age / AFTERGLOW_MS, 1.5);
    return Math.min(1, 0.62 * Math.exp(-age / 750) + tail);
  }

  // The project sheet's tasks with the live calls the summary does not count yet: each
  // call joins the task holding its session, or a "session in progress" row of its own.
  // Returns new objects; the summary's tasks are never touched.
  // [[origin, tokens], ...] plus n tokens from `key`, biggest first; never mutates `list`.
  function addOrigin(list, key, n) {
    var out = (list || []).map(function (x) { return [x[0], x[1]]; }), hit = null;
    if (!key || !(n > 0)) return out;
    out.forEach(function (x) { if (x[0] === key) hit = x; });
    if (hit) hit[1] += n; else out.push([key, n]);
    out.sort(function (a, b) { return b[1] - a[1]; });
    return out;
  }
  // The origins worth a label: up to `max`, each at least `min` of the total, in order.
  function mainOrigins(list, min, max) {
    var total = 0;
    (list || []).forEach(function (x) { total += x[1]; });
    if (!total) return [];
    return (list || []).filter(function (x) { return x[1] / total >= min; }).slice(0, max)
      .map(function (x) { return { key: x[0], share: x[1] / total }; });
  }

  function liveTasks(tasks, calls) {
    var out = (tasks || []).map(function (t) {
      var c = {}; for (var k in t) c[k] = t[k]; c.liveAt = 0; return c;
    });
    (calls || []).forEach(function (e) {
      var t = null;
      // Of the rows holding this session (a classified session has one per segment), the
      // most recent one: that is the segment a live call continues.
      // Compared by that session's own last call in each row (slast), not the row's.
      var best = -1;
      for (var i = 0; i < out.length; i++) {
        if ((out[i].sids || []).indexOf(e.session) < 0) continue;
        var at = (out[i].slast && out[i].slast[e.session]) || out[i].last || 0;
        if (!t || at > best) { t = out[i]; best = at; }
      }
      if (!t) {
        t = { key: "live:" + e.session, ticket: null, title: I.t("sheet.live.session"), tokens: 0, usd: 0, sessions: 1,
              last: 0, sids: [e.session], liveAt: 0, fresh: true, origins: [] };
        out.unshift(t);
      }
      t.tokens += e.tokens || 0; t.usd += e.usd || 0;
      t.origins = addOrigin(t.origins, e.origin || (e.account ? "acct:" + e.account : "claude"), e.tokens || 0);
      t.last = Math.max(t.last || 0, e.t || 0); t.liveAt = Math.max(t.liveAt, e.t || 0);
    });
    return out;
  }

  // Sheet order: by cost (default) or most recent first; ties keep the cost order.
  function sortTasks(tasks, mode) {
    var out = tasks.slice();
    out.sort(mode === "recent" ? function (a, b) { return (b.last || 0) - (a.last || 0) || (b.usd || 0) - (a.usd || 0); }
                               : function (a, b) { return (b.usd || 0) - (a.usd || 0); });
    return out;
  }

  // A task's title without its ticket key when the key is shown as a badge anyway
  // ("[CB-1] Fix x" with ticket CB-1 reads "Fix x").
  function taskTitle(t) {
    var title = t.title || "";
    if (t.ticket) title = title.replace(new RegExp("^\\s*\\[?" + t.ticket.replace(/[-]/g, "\\-") + "\\]?[\\s:·-]*", "i"), "");
    // In a client's bubble, the project a task belongs to goes first.
    if (t.sub) title = t.sub + " · " + title;
    return title;
  }

  // The task that last received tokens.
  function latestTask(tasks) {
    var best = null;
    (tasks || []).forEach(function (t) { if (t.last && (!best || t.last > best.last)) best = t; });
    return best;
  }

  // One odometer wheel per frame. pos only grows (a drum turns one way); it chases the
  // target digit (0-10, fractional for the last wheel) forward, plus `owed` whole extra
  // steps (a slot spin), easing with time constant tau: small tau for the right-hand wheels,
  // large for the left. Returns { pos, owed, speed } with speed in steps per frame.
  function wheelStep(pos, owed, target, dt, tau) {
    var at = ((pos % 10) + 10) % 10;
    var d = ((target - at) % 10 + 10) % 10;
    if (d > 9.999) d = 0;
    var left = d + owed;
    if (left < 0.0005) return { pos: pos + left, owed: 0, speed: 0 };
    var move = Math.min(left, Math.max(left * (1 - Math.exp(-dt / tau)), dt / 4000));
    return { pos: pos + move, owed: Math.max(0, owed - move), speed: move };
  }

  // "45 min", "4 h 15 min", "2 d 15 h" until a reset (never under a minute).
  function untilText(ms) {
    var min = Math.max(1, Math.round(ms / 60000));
    if (min < 60) return min + " min";
    var h = Math.floor(min / 60);
    // From 10 hours on the minutes add nothing and would not fit a narrow card.
    if (h < 24) return h + " h" + (h < 10 && min % 60 ? " " + (min % 60) + " min" : "");
    return Math.floor(h / 24) + " d" + (h % 24 ? " " + (h % 24) + " h" : "");
  }

  // Cursor spend that grew between two summaries of the same view: one entry per project
  // whose cost went up (a new summary of Cursor's API), the delta in tokens and dollars.
  function cursorGrowth(prev, projects) {
    var out = [];
    if (!prev) return out;
    (projects || []).forEach(function (p) {
      var o = prev[p.name];
      if (!o) return;
      var du = (p.usd || 0) - o.usd, dt = (p.tokens || 0) - o.tokens;
      if (du > 0.0005) out.push({ name: p.name, usd: du, tokens: Math.max(0, dt) });
    });
    return out;
  }

  // A range change spins the drums like a slot machine: wheel j (from the right) owes
  // 1 + floor(j / 3) whole turns, paid off over a fixed time (0.6 s plus 0.1 s per place,
  // at most 1.5 s, easing out), so every wheel has landed within 1.5 s. Returns the steps
  // still owed at `ms` after the change (0 once it landed).
  function spinLeft(ms, j) {
    var dur = Math.min(1500, 600 + 100 * j), k = Math.max(0, Math.min(1, ms / dur));
    return 10 * (1 + Math.floor(j / 3)) * Math.pow(1 - k, 3);
  }

  var api = { setLang: I.setLang, lang: I.getLang, t: I.t, count: count, groupSep: groupSep, month: month,
              moneyRound: moneyRound, spinLeft: spinLeft, untilText: untilText, cursorGrowth: cursorGrowth, wheelStep: wheelStep, taskTitle: taskTitle, liveTasks: liveTasks, sortTasks: sortTasks, latestTask: latestTask, freshness: freshness, staleness: staleness, addOrigin: addOrigin, mainOrigins: mainOrigins, afterglow: afterglow, AFTERGLOW_MS: AFTERGLOW_MS, cursorCount: cursorCount, pace: pace, byProject: byProject, liveBubble: liveBubble, rewindSeq: rewindSeq, addExtra: addExtra, startSeq: startSeq, bootChanged: bootChanged, num: num, tokens: tokens, money: money, pct: pct, date: date, hour: hour,
              ago: ago, weekKey: weekKey, coverage: coverage, pendingExtras: pendingExtras,
              nextVersion: nextVersion, answerApplies: answerApplies, queueReset: queueReset };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LatidosFormat = api;
})(this);
