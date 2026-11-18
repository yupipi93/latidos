// node --test web/sound.test.js
const test = require("node:test");
const assert = require("node:assert/strict");
const T = require("./sound.js").theory;

const PROJECTS = ["repo-x", "repo-y", "DEMO-123", "cursor:app", "a", "b", "c", "d", "e", "f"];
const POWERS = [0, 0.1, 0.3, 0.44, 0.45, 0.6, 0.79, 0.8, 1];

test("every bell note is in the D major pentatonic, at any time and size", () => {
  for (let sec = 0; sec < 60; sec += 0.7) {
    for (const p of PROJECTS) for (const w of POWERS) {
      for (const n of T.notesFor(p, w, sec, [])) {
        if (n.kind === "bell") assert.ok(T.inPenta(n.midi), `${p} ${w} ${sec}: ${n.midi}`);
      }
    }
  }
});

test("chord tones are pentatonic and the progression turns every two bars", () => {
  T.CHORDS.forEach((c) => c.tones.forEach((t) => assert.ok(T.inPenta(T.ROOT + t), c.name + " " + t)));
  const bar = 60 / T.BPM * 4;
  assert.equal(T.chordAt(0).name, "D");
  assert.equal(T.chordAt(2 * bar - 0.01).name, "D");
  assert.equal(T.chordAt(2 * bar).name, "Bm");
  assert.equal(T.chordAt(8 * bar).name, "D");
});

test("big calls land on a tone of the chord of the moment, with a bass", () => {
  const bar = 60 / T.BPM * 4;
  T.CHORDS.forEach((c, i) => {
    const notes = T.notesFor("repo-x", 0.9, i * 2 * bar + 1, []);
    const bell = notes.find((n) => n.kind === "bell"), bass = notes.find((n) => n.kind === "bass");
    assert.ok(c.tones.map((t) => (t + 120) % 12).includes((bell.midi - T.ROOT + 120) % 12), c.name);
    assert.equal(bass.midi, T.ROOT - 12 + c.bass);
    assert.ok(bass.midi >= 40 && bass.midi <= 52);
  });
  assert.equal(T.notesFor("repo-x", 0.5, 0, []).filter((n) => n.kind === "bass").length, 0);
});

test("small calls ring higher than big ones, within a comfortable range", () => {
  for (const p of PROJECTS) {
    const hi = T.notesFor(p, 0, 0, [])[0].midi, lo = T.notesFor(p, 0.9, 0, [])[0].midi;
    assert.ok(hi > lo, `${p}: ${hi} <= ${lo}`);
    for (const w of POWERS) {
      const m = T.notesFor(p, w, 0, [])[0].midi;
      assert.ok(m >= 55 && m <= 93, `${p} ${w}: ${m}`);
    }
  }
});

test("a project keeps its note, and a taken pitch is not repeated in a slot", () => {
  assert.equal(T.notesFor("repo-x", 0.2, 3, [])[0].midi, T.notesFor("repo-x", 0.2, 3, [])[0].midi);
  const first = T.notesFor("repo-x", 0.2, 3, [])[0].midi;
  const second = T.notesFor("repo-x", 0.2, 3, [first])[0].midi;
  assert.notEqual(second, first);
  assert.ok(T.inPenta(second));
});

test("hits wait for the next sixteenth, never more than one step", () => {
  for (let s = 0; s < 5; s += 0.037) {
    const q = T.quantize(s);
    assert.ok(q >= s && q - s <= T.STEP + 1e-9, `${s} -> ${q}`);
    const k = q / T.STEP;
    assert.ok(Math.abs(k - Math.round(k)) < 1e-6 || q === s, `${s} -> ${q} off grid`);
  }
  assert.equal(T.quantize(T.STEP * 4 + 0.01), T.STEP * 4 + 0.01);  // just after a point: on it
});

test("a crowded slot is turned down: n notes add up to sqrt(n) voices", () => {
  assert.equal(T.slotGain(1), 1);
  assert.ok(T.slotGain(3) < T.slotGain(2));
  for (let n = 1; n <= 6; n++) {
    let sum = 0;
    for (let k = 1; k <= n; k++) sum += T.slotGain(k);
    assert.ok(Math.abs(sum - Math.sqrt(n)) < 1e-9, n + ": " + sum);
  }
  assert.equal(T.freq(69), 440);
});

test("two big calls in one slot do not double the bass (audit of 93)", () => {
  const a = T.notesFor("x", 0.9, 0, []);
  const b = T.notesFor("y", 0.9, 0, a.map((n) => n.midi));
  assert.equal(a.filter((n) => n.kind === "bass").length, 1);
  assert.equal(b.filter((n) => n.kind === "bass").length, 0);
});

test("a slot full of pitches still finds a free one (audit of 93)", () => {
  for (const p of PROJECTS) {
    const taken = [];
    for (let i = 0; i < 5; i++) {
      const m = T.notesFor(p, 0.2, 3, taken)[0].midi;
      assert.ok(!taken.includes(m), `${p}: ${m} repeated in ${taken}`);
      assert.ok(T.inPenta(m));
      taken.push(m);
    }
  }
});

test("documented ranges hold: small calls E4 to E6, basses G2 to D3", () => {
  for (const p of PROJECTS) for (const w of [0, 0.1, 0.3, 0.44]) {
    const m = T.notesFor(p, w, 0, [])[0].midi;
    assert.ok(m >= 64 && m <= 88, `${p} ${w}: ${m}`);
  }
  T.CHORDS.forEach((c) => { const b = T.ROOT - 12 + c.bass; assert.ok(b >= 43 && b <= 50, c.name); });
});
