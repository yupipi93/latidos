/* Latidos: optional sound for the sparks that land in the cells (L-172). Off by default.

   How it stays pleasant, following what sonifications such as Listen to Wikipedia do:
   - Every bell note comes from the D major pentatonic scale, which has no semitone steps, so any
     order of notes sounds consonant (a wind chime).
   - A slow progression (D, Bm, G, A; two bars each at 72 BPM) colours the hits: a big call
     lands on a tone of the chord of the moment, so a burst of calls spells a chord.
   - Hits wait for the next sixteenth of the beat (at most ~0.2 s), so a burst becomes a
     rhythm instead of a clatter, at most three notes per slot (bass included), no repeated
     pitch in one, and a slot of n notes is as loud as sqrt(n) voices.
   - Small calls ring high and quiet, big ones lower and fuller, with a soft bass under the
     largest. Each project keeps its own place in the scale, so a busy cell has a voice.
   - Soft FM bells (low index that dies fast), a lowpass, a long generated reverb, a gentle
     echo and a limiter; loud bursts are scaled down by the number of voices.

   The theory part has no audio and is shared with sound.test.js; the engine needs a user
   gesture before the browser lets it play. */
(function (root) {
  "use strict";

  var BPM = 72;
  var STEP = 60 / BPM / 4;              // a sixteenth, in seconds
  var BAR = 60 / BPM * 4;
  var CHORD_BARS = 2;
  var PENTA = [0, 2, 4, 7, 9];          // D E F# A B, as semitones over D
  var ROOT = 62;                        // D4 (MIDI)
  // D, Bm, G6/9, Asus2: chord tones as semitones over D, all inside the pentatonic (G and
  // C# are not, so G sounds as B E F# over a G bass and A as A E B). The G bass is the one
  // pitch outside the scale.
  var CHORDS = [
    { name: "D", bass: 0, tones: [0, 4, 7] },
    { name: "Bm", bass: -3, tones: [-3, 0, 4] },
    { name: "G6/9", bass: -7, tones: [-3, 2, 4] },
    { name: "Asus2", bass: -5, tones: [-5, 2, 9] }
  ];
  var MAX_PER_SLOT = 3;                 // notes, the bass included
  var MAX_VOICES = 12;

  function hash(str) {
    var h = 2166136261;
    str = String(str || "");
    for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
    return h >>> 0;
  }
  function chordAt(sec) {
    var i = Math.floor(Math.max(0, sec) / (BAR * CHORD_BARS)) % CHORDS.length;
    return CHORDS[i];
  }
  // The next grid point at or after sec (a hit within 15 ms of a point plays on it).
  function quantize(sec) {
    var k = Math.ceil((sec - 0.015) / STEP);
    return Math.max(sec, k * STEP);
  }
  function inPenta(midi) { return PENTA.indexOf(((midi - ROOT) % 12 + 12) % 12) >= 0; }
  // The pentatonic note nearest to midi, searching up first.
  function snap(midi) {
    for (var d = 0; d < 3; d++) { if (inPenta(midi + d)) return midi + d; if (inPenta(midi - d)) return midi - d; }
    return midi;
  }
  function pentaStep(midi, steps) {
    var m = snap(midi), dir = steps < 0 ? -1 : 1, n = Math.abs(steps);
    while (n > 0) { m += dir; if (inPenta(m)) n--; }
    return m;
  }
  // The notes of one hit: [{ midi, gain, kind }]. power is 0..1 (sparkPower), sec the time
  // on the music clock, taken: the pitches already used in this slot.
  function notesFor(project, power, sec, taken) {
    taken = taken || [];
    var chord = chordAt(sec), h = hash(project), p = Math.max(0, Math.min(1, power || 0));
    var out = [], mid;
    if (p >= 0.45) {
      // A tone of the chord, in the middle register; the project picks which one.
      var tone = chord.tones[h % chord.tones.length];
      mid = ROOT + tone + (p >= 0.8 ? 0 : 12);
    } else {
      // A pentatonic note up high (E4 to E6): the project picks its place, the size
      // nudges it lower.
      mid = pentaStep(ROOT + 12, (h % 7) - Math.round(p * 8));
    }
    // A pitch already in the slot: the nearest free scale step, +1, -1, +2, -2, ...
    var base = mid;
    for (var k = 1; taken.indexOf(mid) >= 0 && k <= 12; k++) mid = pentaStep(base, k % 2 ? (k + 1) / 2 : -k / 2);
    out.push({ midi: mid, gain: 0.35 + 0.65 * p, kind: "bell" });
    var bass = ROOT - 12 + chord.bass;
    if (p >= 0.8 && taken.indexOf(bass) < 0) out.push({ midi: bass, gain: 0.5 + 0.5 * p, kind: "bass" });
    return out;
  }
  function freq(midi) { return 440 * Math.pow(2, (midi - 69) / 12); }
  // Loudness of the k-th note of a slot: sqrt(k) - sqrt(k - 1), so the n notes of a slot
  // add up to sqrt(n) voices (three notes: 1.73), whatever order they arrive in.
  function slotGain(k) { k = Math.max(1, k); return Math.sqrt(k) - Math.sqrt(k - 1); }

  var theory = { BPM: BPM, STEP: STEP, PENTA: PENTA, ROOT: ROOT, CHORDS: CHORDS, MAX_PER_SLOT: MAX_PER_SLOT,
                 chordAt: chordAt, quantize: quantize, inPenta: inPenta, snap: snap, pentaStep: pentaStep,
                 notesFor: notesFor, freq: freq, slotGain: slotGain, hash: hash };

  // ---------------------------------------------------------------------------------------
  // Engine (browser only)
  // ---------------------------------------------------------------------------------------
  function Engine() {
    this.ctx = null; this.on = false; this.slots = {}; this.voices = 0;
  }
  Engine.prototype.build = function () {
    var AC = root.AudioContext || root.webkitAudioContext;
    if (!AC) return false;
    var ctx = this.ctx = new AC();
    var master = this.master = ctx.createGain();
    master.gain.value = 0;
    var limiter = ctx.createDynamicsCompressor();
    limiter.threshold.value = -18; limiter.knee.value = 12; limiter.ratio.value = 6;
    limiter.attack.value = 0.005; limiter.release.value = 0.25;
    var tone = ctx.createBiquadFilter();
    tone.type = "lowpass"; tone.frequency.value = 4200; tone.Q.value = 0.5;
    this.bus = ctx.createGain();
    this.bus.connect(tone);
    // Dry, a long soft reverb and a dotted-eighth echo that darkens as it repeats.
    var dry = ctx.createGain(); dry.gain.value = 0.7;
    var verb = ctx.createConvolver(); verb.buffer = this.impulse(3.2);
    var wet = ctx.createGain(); wet.gain.value = 0.55;
    var delay = ctx.createDelay(2); delay.delayTime.value = STEP * 3;
    var fb = ctx.createGain(); fb.gain.value = 0.28;
    var dtone = ctx.createBiquadFilter(); dtone.type = "lowpass"; dtone.frequency.value = 2200;
    var dwet = ctx.createGain(); dwet.gain.value = 0.18;
    tone.connect(dry); dry.connect(limiter);
    tone.connect(verb); verb.connect(wet); wet.connect(limiter);
    tone.connect(delay); delay.connect(dtone); dtone.connect(fb); fb.connect(delay); dtone.connect(dwet);
    dwet.connect(verb); dwet.connect(limiter);
    limiter.connect(master); master.connect(ctx.destination);
    // The music clock starts with the engine, so the first chord is the tonic.
    this.t0 = ctx.currentTime;
    return true;
  };
  // A stereo impulse of decaying, slowly darkening noise: a reverb with no file to load.
  Engine.prototype.impulse = function (sec) {
    var ctx = this.ctx, n = Math.floor(ctx.sampleRate * sec), buf = ctx.createBuffer(2, n, ctx.sampleRate);
    for (var c = 0; c < 2; c++) {
      var d = buf.getChannelData(c), lp = 0;
      for (var i = 0; i < n; i++) {
        var k = i / n, a = 0.15 + 0.8 * k;  // smoother (darker) towards the tail
        lp = lp + (Math.random() * 2 - 1 - lp) * (1 - a);
        d[i] = lp * Math.pow(1 - k, 2.6) * (i < ctx.sampleRate * 0.012 ? i / (ctx.sampleRate * 0.012) : 1);
      }
    }
    return buf;
  };
  Engine.prototype.setOn = function (on) {
    if (on && !this.ctx && !this.build()) return false;
    this.on = on;
    if (!this.ctx) return on;
    var g = this.master.gain, now = this.ctx.currentTime;
    g.cancelScheduledValues(now); g.setValueAtTime(g.value, now);
    if (on) {
      this.resume();
      g.linearRampToValueAtTime(0.8, now + 0.4);
    } else {
      g.linearRampToValueAtTime(0, now + 0.3);
      // Off: no reverb, echo or limiter working on silence while the tab stays open.
      var self = this;
      setTimeout(function () { if (!self.on) self.suspend(); }, 350);
    }
    return on;
  };
  // resume() and suspend() reject (not throw) when the browser refuses; nothing to do then.
  function quiet(ctx, what) {
    try { var p = ctx[what](); if (p && p.catch) p.catch(function () {}); } catch (e) {}
  }
  Engine.prototype.resume = function () {
    if (this.ctx && this.on && this.ctx.state !== "running") quiet(this.ctx, "resume");
  };
  Engine.prototype.suspend = function () {
    if (this.ctx && this.ctx.state === "running") quiet(this.ctx, "suspend");
  };
  Engine.prototype.ready = function () { return !!(this.on && this.ctx && this.ctx.state === "running"); };
  // A spark landed. hit: { project, power, cursor, pan (-1..1) }; now: the audio time it
  // landed (the context's current time; given only by the offline render check).
  Engine.prototype.hit = function (hit, now) {
    if (!this.ready()) return;
    var ctx = this.ctx;
    if (now === undefined) now = ctx.currentTime;
    var at = this.t0 + quantize(now - this.t0), key = Math.round((at - this.t0) / STEP);
    var slot = this.slots[key] || (this.slots[key] = { pitches: [] });
    for (var k in this.slots) if (+k < key - 8) delete this.slots[k];
    var room = Math.min(MAX_PER_SLOT - slot.pitches.length, MAX_VOICES - this.voices);
    if (room <= 0) return;
    // The caps count notes: with room for one, a big call keeps its bell and drops its bass.
    var notes = notesFor(hit.project, hit.power, at - this.t0, slot.pitches).slice(0, room), self = this;
    notes.forEach(function (n) {
      slot.pitches.push(n.midi);
      self.voice(n, at, hit, slotGain(slot.pitches.length));
    });
  };
  Engine.prototype.voice = function (n, at, hit, share) {
    var ctx = this.ctx, f = freq(n.midi), bass = n.kind === "bass";
    var out = ctx.createGain(), pan = ctx.createStereoPanner ? ctx.createStereoPanner() : null;
    var peak = (bass ? 0.16 : 0.13) * n.gain * share;
    // Low notes ring longer, high ones shorter; Cursor's are a little drier (marimba).
    var decay = bass ? 3.2 : Math.max(0.9, 3.4 - (n.midi - 60) * 0.08) * (hit.cursor ? 0.6 : 1);
    var attack = bass ? 0.04 : 0.006;
    out.gain.setValueAtTime(0.0001, at);
    out.gain.linearRampToValueAtTime(peak, at + attack);
    out.gain.exponentialRampToValueAtTime(0.0001, at + attack + decay);
    var car = ctx.createOscillator();
    car.type = bass ? "triangle" : "sine";
    car.frequency.value = f;
    var nodes = [car];
    if (!bass) {
      // FM: the strike is bright for a moment, then mellows into a pure tone.
      var mod = ctx.createOscillator(), idx = ctx.createGain();
      mod.frequency.value = f * (hit.cursor ? 1 : 3.5);
      var depth = f * (hit.cursor ? 1.2 : 0.9) * (0.4 + 0.6 * n.gain);
      idx.gain.setValueAtTime(depth, at);
      idx.gain.exponentialRampToValueAtTime(Math.max(0.01, depth * 0.02), at + 0.35);
      mod.connect(idx); idx.connect(car.frequency);
      nodes.push(mod);
      // A faint octave partial that dies fast: the glassy top of a celesta.
      var oct = ctx.createOscillator(), og = ctx.createGain();
      oct.frequency.value = f * 2;
      og.gain.setValueAtTime(0.0001, at);
      og.gain.linearRampToValueAtTime(peak * 0.25, at + attack);
      og.gain.exponentialRampToValueAtTime(0.0001, at + 0.6);
      oct.connect(og); og.connect(pan || this.bus);
      nodes.push(oct);
    }
    car.connect(out);
    if (pan) { pan.pan.value = Math.max(-0.7, Math.min(0.7, (hit.pan || 0) * 0.7)); out.connect(pan); pan.connect(this.bus); }
    else out.connect(this.bus);
    var end = at + attack + decay + 0.05, self = this;
    this.voices++;
    nodes.forEach(function (o) { o.start(at); o.stop(end); });
    car.onended = function () { self.voices = Math.max(0, self.voices - 1); out.disconnect(); if (pan) pan.disconnect(); };
  };
  // A soft rising arpeggio of the tonic chord when the sound is switched on.
  Engine.prototype.greet = function () {
    if (!this.ready() || this.voices > MAX_VOICES - 4) return;
    var at = this.ctx.currentTime + 0.08, self = this;
    [ROOT, ROOT + 4, ROOT + 7, ROOT + 12].forEach(function (m, i) {
      self.voice({ midi: m, gain: 0.45, kind: "bell" }, at + i * STEP * 1.5, { pan: (i - 1.5) * 0.3 }, 1);
    });
  };

  var api = { theory: theory, Engine: Engine };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LatidosSound = api;
})(this);
