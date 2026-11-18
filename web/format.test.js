// node --test web/format.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("./format.js");
const I = require("./i18n.js");

// Run fn with the page in language l, then back to the default (English).
function inLang(l, fn) {
  return () => { F.setLang(l); try { fn(); } finally { F.setLang("en"); } };
}

test("numbers group thousands with a dot, always (Spanish)", inLang("es", () => {
  assert.equal(F.num(1234), "1.234");
  assert.equal(F.num(16046708684), "16.046.708.684");
  assert.equal(F.num(1234.5, 2), "1.234,50");
  assert.equal(F.num(-5), "-5");
}));

test("tokens read short (Spanish)", inLang("es", () => {
  assert.equal(F.tokens(850), "850");
  assert.equal(F.tokens(1234), "1,2 mil");
  assert.equal(F.tokens(312000), "312 mil");
  assert.equal(F.tokens(4500000), "4,5 M");
  assert.equal(F.tokens(16046708684), "16.047 M");
}));

test("money (Spanish)", inLang("es", () => {
  assert.equal(F.money(6400.86, "EUR"), "6.400,86 €");
  assert.equal(F.money(12345.6, "USD"), "12.346 $");
  assert.equal(F.money(0.004, "EUR"), "<0,01 €");
  assert.equal(F.money(0, "EUR"), "0,00 €");
}));

test("dates add the year only when it is not this year or older than 300 days (Spanish)", inLang("es", () => {
  const now = new Date(2026, 10, 12, 12).getTime();
  assert.equal(F.date(new Date(2026, 9, 3).getTime(), {}, now), "3 oct");
  assert.equal(F.date(new Date(2025, 11, 30).getTime(), {}, now), "30 dic 2025");
  assert.equal(F.date(new Date(2026, 10, 12, 9, 5).getTime(), { time: true }, now), "12 nov · 09:05");
}));

test("week key is the local Monday", () => {
  assert.equal(F.weekKey(new Date(2026, 10, 12, 23).getTime()), "2026-11-09");
  assert.equal(F.weekKey(new Date(2026, 10, 9, 0, 5).getTime()), "2026-11-09");
  assert.equal(F.weekKey(new Date(2026, 10, 8, 23).getTime()), "2026-11-02");
});

test("coverage matches the server algorithm", () => {
  const base = new Date(2026, 10, 10, 8).getTime();
  const blocks = [[base, 10], [base + 5 * 3600000, 10], [base + 10 * 3600000, 10]];
  const c = F.coverage(blocks, 1, 6, 15);
  assert.equal(c.main, 15);
  assert.equal(c.short, 3);
  assert.equal(c.share, 0.5);
  assert.equal(c.weeks, 1);
});

test("coverage carries the week's balance into a later range", () => {
  const base = new Date(2026, 10, 10, 8).getTime();
  const c = F.coverage([[base, 15, 0], [base + 5 * 3600000, 10, 10]], 1, 20, 15);
  assert.equal(c.main, 0);
  assert.equal(c.total, 10);
  assert.equal(c.blocks, 1);
  assert.equal(c.weekMain, 15);
  assert.equal(c.weekRest, 10);
});

test("live extras are matched by ingest sequence, not time", () => {
  // seq 7 has an older timestamp than seq 6 (written late): only seq decides.
  const ex = [{ seq: 6, t: 200, tokens: 1, usd: 1 }, { seq: 7, t: 100, tokens: 2, usd: 2 }];
  assert.deepEqual(F.pendingExtras(ex, 6).map((e) => e.seq), [7]);
  assert.equal(F.pendingExtras(ex, null).length, 2);
  assert.equal(F.pendingExtras(ex, 7).length, 0);
});

test("answers apply only to their range; queued loads keep their reset", () => {
  assert.equal(F.answerApplies("todo", "todo"), true);
  assert.equal(F.answerApplies("todo", "hoy"), false);
  assert.equal(F.queueReset(false, true), true);   // range change while loading
  assert.equal(F.queueReset(true, false), true);   // a later plain reload keeps it
  assert.equal(F.queueReset(false, false), false);
});

test("the remembered version never goes down except on a range change", () => {
  assert.equal(F.nextVersion(null, 5, false), 5);
  assert.equal(F.nextVersion(9, 7, false), 9);   // a cached, older answer
  assert.equal(F.nextVersion(9, 12, false), 12);
  assert.equal(F.nextVersion(9, 7, true), 7);    // new range: take it as it is
});

test("a server restart is detected by its boot id", () => {
  assert.equal(F.bootChanged(null, 5), false);   // first answer
  assert.equal(F.bootChanged(5, 5), false);
  assert.equal(F.bootChanged(5, 9), true);
});

test("live reading starts at the sequence the summary counts", () => {
  assert.equal(F.startSeq({ seq: 110, seq_at: 100 }), 100);  // cached summary behind the server
  assert.equal(F.startSeq({ seq: 7 }), 7);
  assert.equal(F.startSeq({}), 0);
});

test("a range change rewinds to the summary and replays without double counting", () => {
  assert.equal(F.rewindSeq(34, { seq: 34, seq_at: 25 }), 25);  // cached summary behind
  assert.equal(F.rewindSeq(20, { seq: 34, seq_at: 25 }), 20);  // never jumps forward
  assert.equal(F.rewindSeq(0, { seq: 34, seq_at: 25 }), 25);   // first load
  let ex = [];
  ex = F.addExtra(ex, { seq: 26, tokens: 5, usd: 1 });
  ex = F.addExtra(ex, { seq: 26, tokens: 5, usd: 1 });  // replayed after the rewind
  ex = F.addExtra(ex, { seq: 27, tokens: 2, usd: 1 });
  assert.deepEqual(ex.map((e) => e.seq), [26, 27]);
  assert.equal(F.pendingExtras(ex, 25).length, 2);
});

test("a batch of live calls plays out evenly before the next poll", () => {
  assert.equal(F.pace(4, 4000, 90, 1400), 1000);
  assert.equal(F.pace(200, 4000, 90, 1400), 90);    // a burst: as fast as allowed
  assert.equal(F.pace(1, 4000, 90, 1400), 1400);    // a lone call: not slower than this
  assert.equal(F.pace(0, 4000, 90, 1400), 1400);
});

test("pending live cost is grouped per project for the cell labels", () => {
  const by = F.byProject([{ seq: 1, tokens: 5, usd: 1, project: "a" }, { seq: 2, tokens: 2, usd: 0.5, project: "a" },
                          { seq: 3, tokens: 1, usd: 0.1, project: "b" }]);
  assert.deepEqual(by.a, { tokens: 7, usd: 1.5 });
  assert.deepEqual(by.b, { tokens: 1, usd: 0.1 });
});

test("a Cursor item with no cost shows what it counted (Spanish)", inLang("es", () => {
  assert.equal(F.cursorCount({ usd: 0, requests: 1, turns: 0 }), "1 petición");
  assert.equal(F.cursorCount({ usd: 0, requests: 7, turns: 3 }), "7 peticiones");
  assert.equal(F.cursorCount({ usd: 0, requests: 0, turns: 228 }), "228 turnos");
}));

test("freshness: alive for 10 minutes, then a log scale down to 0 at a month", () => {
  const now = 1e12;
  const f = (h) => F.freshness(now - h * 3600000, now);
  assert.equal(F.freshness(now - 5 * 60000, now), 1);
  assert.ok(f(1) > 0.75 && f(1) < 0.82);
  assert.ok(f(6) > 0.54 && f(6) < 0.6);
  assert.ok(f(24) > 0.38 && f(24) < 0.44);
  assert.ok(f(96) > 0.2 && f(96) < 0.28);    // 4 days: still apart from a fortnight
  assert.ok(f(96) - f(336) > 0.1);
  assert.equal(f(31 * 24), 0);
  for (let h = 0.2; h < 800; h *= 1.5) assert.ok(f(h * 1.5) <= f(h));
  assert.equal(F.freshness(0, now), 0);
  assert.equal(F.freshness(now + 5000, now), 1);   // a clock a little ahead
});

test("afterglow: a flare, then a tail still visible after 6 s, gone by 11 s", () => {
  assert.equal(F.afterglow(0), 1);
  assert.ok(F.afterglow(2600) > 0.15);   // the old aura ended here
  assert.ok(F.afterglow(6000) > 0.05);
  assert.equal(F.afterglow(F.AFTERGLOW_MS), 0);
  assert.equal(F.afterglow(-1), 0);
  assert.equal(F.afterglow(NaN), 0);
  for (let a = 0; a < 11000; a += 250) assert.ok(F.afterglow(a + 250) <= F.afterglow(a));
});

test("live calls join the task of their session, or a session-in-progress row (Spanish)", inLang("es", () => {
  const tasks = [{ key: "A", usd: 5, tokens: 50, last: 100, sids: ["s1", "s2"] },
                 { key: "B", usd: 1, tokens: 10, last: 300, sids: ["s3"] }];
  const out = F.liveTasks(tasks, [{ session: "s2", tokens: 7, usd: 0.5, t: 900 },
                                  { session: "s9", tokens: 3, usd: 0.1, t: 950 }]);
  assert.equal(tasks[0].usd, 5);                          // the summary is untouched
  assert.equal(out.length, 3);
  assert.equal(out[0].key, "live:s9");
  assert.equal(out[0].title, "Sesión en curso");
  const a = out.find((t) => t.key === "A");
  assert.equal(a.usd, 5.5); assert.equal(a.tokens, 57); assert.equal(a.last, 900); assert.equal(a.liveAt, 900);
  assert.equal(out.find((t) => t.key === "B").liveAt, 0);
}));

test("sheet sorting and the latest task", () => {
  const ts = [{ key: "a", usd: 5, last: 100 }, { key: "b", usd: 9, last: 50 }, { key: "c", usd: 1, last: 400 }];
  assert.deepEqual(F.sortTasks(ts, "usd").map((t) => t.key), ["b", "a", "c"]);
  assert.deepEqual(F.sortTasks(ts, "recent").map((t) => t.key), ["c", "a", "b"]);
  assert.equal(F.latestTask(ts).key, "c");
  assert.equal(F.latestTask([]), null);
});

test("a task title drops the ticket key its badge already shows", () => {
  assert.equal(F.taskTitle({ ticket: "DEMO-1034", title: "[DEMO-1034] Avoid partial deploys" }), "Avoid partial deploys");
  assert.equal(F.taskTitle({ ticket: "OTHER-7", title: "OTHER-7: Fix DNS" }), "Fix DNS");
  assert.equal(F.taskTitle({ ticket: "DEMO-1", title: "Release gates" }), "Release gates");
  assert.equal(F.taskTitle({ ticket: null, title: "[DEMO-1] Keep" }), "[DEMO-1] Keep");
  assert.equal(F.taskTitle({ ticket: "DEMO-1" }), "");
});

test("odometer wheels only turn forward and land on their digit", () => {
  let w = { pos: 3, owed: 0 };
  for (let i = 0; i < 200; i++) w = F.wheelStep(w.pos, w.owed, 1, 16, 120);
  assert.ok(Math.abs((w.pos % 10) - 1) < 0.01);         // 3 -> 1 goes forward through 9, 0
  assert.ok(w.pos > 10.9 && w.pos < 11.01);
  let a = { pos: 0, owed: 0 }, b = { pos: 0, owed: 0 };
  a = F.wheelStep(a.pos, a.owed, 5, 16, 80); b = F.wheelStep(b.pos, b.owed, 5, 16, 1200);
  assert.ok(a.speed > b.speed * 5);                     // right-hand wheels turn faster
  let s = { pos: 2, owed: 10 };
  for (let i = 0; i < 400; i++) s = F.wheelStep(s.pos, s.owed, 2, 16, 150);
  assert.equal(s.owed, 0); assert.ok(Math.abs(s.pos - 12) < 0.01);   // one whole extra turn
  const still = F.wheelStep(7, 0, 7, 16, 100);
  assert.equal(still.speed, 0);
});

test("reset countdowns read short", () => {
  assert.equal(F.untilText(20000), "1 min");
  assert.equal(F.untilText(45 * 60000), "45 min");
  assert.equal(F.untilText((4 * 60 + 15) * 60000), "4 h 15 min");
  assert.equal(F.untilText(3 * 3600000), "3 h");
  assert.equal(F.untilText((2 * 24 + 15) * 3600000), "2 d 15 h");
  assert.equal(F.untilText((12 * 60 + 34) * 60000), "12 h");      // no minutes from 10 h on
  assert.equal(F.untilText((9 * 60 + 59) * 60000), "9 h 59 min");
});

test("Cursor growth between summaries makes one spark per project that spent more", () => {
  const prev = { a: { usd: 10, tokens: 100 }, b: { usd: 5, tokens: 50 } };
  const g = F.cursorGrowth(prev, [{ name: "a", usd: 10.4, tokens: 900 }, { name: "b", usd: 5, tokens: 50 },
                                  { name: "c", usd: 3, tokens: 30 }]);
  assert.deepEqual(g.map((x) => x.name), ["a"]);   // b did not change; c is new to this view
  assert.ok(Math.abs(g[0].usd - 0.4) < 1e-9); assert.equal(g[0].tokens, 800);
  assert.deepEqual(F.cursorGrowth(null, [{ name: "a", usd: 1 }]), []);
});

test("a range-change spin lands every wheel within 1.5 s, the left ones last", () => {
  for (let j = 0; j < 12; j++) {
    assert.equal(F.spinLeft(1500, j), 0);
    assert.ok(F.spinLeft(0, j) >= 10);
  }
  assert.ok(F.spinLeft(500, 9) > F.spinLeft(500, 0));
  for (let t = 0; t < 1500; t += 50) assert.ok(F.spinLeft(t + 50, 4) <= F.spinLeft(t, 4));
});

test("English is the default: numbers group with a comma, decimals with a point", () => {
  assert.equal(F.lang(), "en");
  assert.equal(F.num(1234), "1,234");
  assert.equal(F.num(16046708684), "16,046,708,684");
  assert.equal(F.num(1234.5, 2), "1,234.50");
  assert.equal(F.num(-5), "-5");
  assert.equal(F.groupSep(), ",");
});

test("tokens read short (English)", () => {
  assert.equal(F.tokens(850), "850");
  assert.equal(F.tokens(1234), "1.2K");
  assert.equal(F.tokens(312000), "312K");
  assert.equal(F.tokens(4500000), "4.5M");
  assert.equal(F.tokens(16046708684), "16,047M");
});

test("money puts the symbol first in English", () => {
  assert.equal(F.money(6400.86, "EUR"), "€6,400.86");
  assert.equal(F.money(12345.6, "USD"), "$12,346");
  assert.equal(F.money(0.004, "EUR"), "<€0.01");
  assert.equal(F.money(0, "EUR"), "€0.00");
  assert.equal(F.money(-5, "USD"), "-$5.00");
  assert.equal(F.moneyRound(12.4, "EUR"), "€12");
  assert.equal(F.moneyRound(0.4, "USD"), "<$1");
  assert.equal(F.pct(0.456), "46%");
});

test("whole-unit money and percentages (Spanish)", inLang("es", () => {
  assert.equal(F.money(-5, "USD"), "-5,00 $");
  assert.equal(F.moneyRound(1234.4, "EUR"), "1.234 €");
  assert.equal(F.moneyRound(0.4, "EUR"), "<1 €");
  assert.equal(F.pct(0.456), "46 %");
  assert.equal(F.groupSep(), ".");
}));

test("dates and weekdays in English", () => {
  const now = new Date(2026, 10, 12, 12).getTime();
  assert.equal(F.date(new Date(2026, 9, 3).getTime(), {}, now), "3 Oct");
  assert.equal(F.date(new Date(2025, 11, 30).getTime(), {}, now), "30 Dec 2025");
  assert.equal(F.date(new Date(2026, 10, 12, 9, 5).getTime(), { time: true, weekday: true }, now), "Thu 12 Nov · 09:05");
  assert.equal(F.month(new Date(2026, 4, 1).getTime()), "May");
});

test("time ago in both languages", () => {
  const now = 1e12;
  assert.equal(F.ago(now - 10000, now), "now");
  assert.equal(F.ago(now - 3 * 60000, now), "3 min ago");
  assert.equal(F.ago(now - 2 * 3600000, now), "2 h ago");
  assert.equal(F.ago(now - 86400000, now), "yesterday");
  assert.equal(F.ago(now - 5 * 86400000, now), "5 days ago");
  F.setLang("es");
  try {
    assert.equal(F.ago(now - 10000, now), "ahora");
    assert.equal(F.ago(now - 3 * 60000, now), "hace 3 min");
    assert.equal(F.ago(now - 86400000, now), "ayer");
    assert.equal(F.ago(now - 5 * 86400000, now), "hace 5 días");
    const nov = new Date(2026, 10, 12, 12).getTime();
    assert.equal(F.date(new Date(2026, 10, 12, 9, 5).getTime(), { weekday: true }, nov), "jue 12 nov");
    assert.equal(F.month(new Date(2026, 4, 1).getTime()), "may");
  } finally { F.setLang("en"); }
});

test("a Cursor item with no cost shows what it counted (English)", () => {
  assert.equal(F.cursorCount({ usd: 0, requests: 1, turns: 0 }), "1 request");
  assert.equal(F.cursorCount({ usd: 0, requests: 1234, turns: 3 }), "1,234 requests");
  assert.equal(F.cursorCount({ usd: 0, requests: 0, turns: 228 }), "228 turns");
  assert.equal(F.count("n.sessions", 1), "1 session");
  assert.equal(F.liveTasks([], [{ session: "s1", tokens: 1, usd: 0, t: 1 }])[0].title, "Session in progress");
});

test("the i18n table: plurals, placeholders, English fallback, language choice", () => {
  assert.equal(I.t("n.tasks", { n: 1 }), "1 task");
  assert.equal(I.t("n.tasks", { n: 0 }), "0 tasks");
  assert.equal(I.t("range.custom", { from: "a", to: "b" }), "From a to b");
  assert.equal(I.t("no.such.key"), "no.such.key");
  assert.equal(I.pick("?lang=es", null), "es");
  assert.equal(I.pick("?x=1&lang=EN", "es"), "en");   // the URL wins over the stored choice
  assert.equal(I.pick("?lang=fr", "es"), "es");       // unknown: the stored one
  assert.equal(I.pick("", null), "en");               // the default is English
  assert.equal(I.pick("", "de"), "en");
  assert.equal(I.setLang("fr"), "en");                // an unknown language is ignored
  F.setLang("es");
  try {
    assert.equal(I.t("quota.outgoing"), "saliente");
    assert.equal(I.t("sheet.more.tasks", { n: 1 }), "y 1 tarea más");
  } finally { F.setLang("en"); }
  assert.equal(I.t("quota.outgoing"), "outgoing");
  assert.equal(I.t("quota.outgoing.tip"), "Chats opened before the switch still run on this account");
});

test("every key exists in both languages, with the same shape and placeholders", () => {
  const en = I.STRINGS.en, es = I.STRINGS.es;
  assert.deepEqual(Object.keys(es).sort(), Object.keys(en).sort());
  const ph = (v) => (JSON.stringify(v).match(/\{\w+\}/g) || []).sort().join();
  for (const k of Object.keys(en)) {
    assert.equal(Array.isArray(es[k]), Array.isArray(en[k]), k);
    assert.equal(ph(es[k]), ph(en[k]), k);
  }
});

test("staleness: none for 10 minutes, fully grey after a week, most of the way in a day", () => {
  const now = Date.parse("2026-11-16T12:00:00Z");
  const g = (h) => F.staleness(now - h * 3600000, now);
  assert.equal(F.staleness(now - 5 * 60000, now), 0);
  assert.equal(F.staleness(0, now), 1);
  assert.equal(g(24 * 7), 1);
  assert.equal(g(24 * 40), 1);
  assert.ok(g(2) > 0.35 && g(2) < 0.55, String(g(2)));
  assert.ok(g(24) > 0.7 && g(24) < 0.85, String(g(24)));
  let prev = 0;
  for (const h of [0.5, 1, 3, 6, 12, 48, 96, 160]) { assert.ok(g(h) > prev); prev = g(h); }
});

test("origins: add live tokens without touching the summary, keep the main ones", () => {
  const base = [["mt", 700], ["lt", 300]];
  const more = F.addOrigin(base, "lt", 500);
  assert.deepEqual(more, [["lt", 800], ["mt", 700]]);
  assert.deepEqual(base, [["mt", 700], ["lt", 300]]);           // not mutated
  assert.deepEqual(F.addOrigin(base, "na", 0), base);            // nothing to add
  assert.deepEqual(F.addOrigin(null, "cursor", 5), [["cursor", 5]]);
  const main = F.mainOrigins([["mt", 900], ["lt", 95], ["claude", 5]], 0.1, 3);
  assert.deepEqual(main.map((o) => o.key), ["mt"]);
  assert.equal(Math.round(main[0].share * 100), 90);
  assert.deepEqual(F.mainOrigins([], 0.1, 3), []);
  assert.equal(F.mainOrigins([["a", 1], ["b", 1], ["c", 1], ["d", 1]], 0, 3).length, 3);
});

test("a live-only task carries the origin of its calls", () => {
  const out = F.liveTasks([], [{ session: "s9", tokens: 100, usd: 1, t: 5, origin: "acct:na", account: "na" }]);
  assert.deepEqual(out[0].origins, [["acct:na", 100]]);
  const known = F.liveTasks([{ key: "s:s1", sids: ["s1"], tokens: 10, usd: 0, origins: [["mt", 10]] }],
                            [{ session: "s1", tokens: 30, usd: 0, t: 5, origin: "lt" }]);
  assert.deepEqual(known[0].origins, [["lt", 30], ["mt", 10]]);
});

test("liveBubble follows the summary's client grouping", () => {
  const data = { fold: { "Acme · Portal": "Acme" }, projects: [{ name: "Acme" }, { name: "Acme · Billing" }, { name: "tool-x" }] };
  assert.equal(F.liveBubble({ project: "Acme · Portal", client: "Acme" }, data), "Acme");
  assert.equal(F.liveBubble({ project: "Acme · Billing", client: "Acme" }, data), "Acme · Billing");
  assert.equal(F.liveBubble({ project: "Acme · New", client: "Acme" }, data), "Acme");
  assert.equal(F.liveBubble({ project: "tool-y" }, data), "tool-y");
  assert.equal(F.liveBubble({ project: "tool-x", client: null }, null), "tool-x");
});

test("taskTitle names the project of a task shown in its client's bubble", () => {
  assert.equal(F.taskTitle({ title: "DEMO-1 Fix login", ticket: "DEMO-1", sub: "Portal" }), "Portal · Fix login");
  assert.equal(F.taskTitle({ title: "Fix login" }), "Fix login");
});

test("a live call goes to the newest row of its session (a session split in segments)", () => {
  const rows = F.liveTasks([{ key: "a", sids: ["s1"], last: 10, usd: 5, tokens: 1 }, { key: "b", sids: ["s1"], last: 20, usd: 1, tokens: 1 }],
                           [{ session: "s1", tokens: 3, usd: 1, t: 30 }]);
  assert.equal(rows.find(r => r.key === "b").usd, 2);
  assert.equal(rows.find(r => r.key === "a").usd, 5);
});

test("a live call goes to its session's current row, not a ticket row merged with another session", () => {
  const rows = F.liveTasks([{ key: "T-1", sids: ["R", "S"], slast: { R: 2000, S: 1000 }, last: 2000, usd: 5, tokens: 1 },
                            { key: "s:S.1", sids: ["S"], slast: { S: 1500 }, last: 1500, usd: 1, tokens: 1 }],
                           [{ session: "S", tokens: 3, usd: 1, t: 3000 }]);
  assert.equal(rows.find(r => r.key === "s:S.1").usd, 2);
  assert.equal(rows.find(r => r.key === "T-1").usd, 5);
});
