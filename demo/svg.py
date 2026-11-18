#!/usr/bin/env python3
"""Animated SVGs of the README (docs/media/*.svg), in the page's own palette.

    python3 demo/svg.py            # writes docs/media/{hero-dark,hero-light,ecg,odometer,pipeline,anatomy,footprint}.svg

GitHub shows README images through <img>: no script, no web fonts, but CSS and SMIL
animations, gradients and filters work, so everything here is plain SVG with SMIL.
anatomy.svg embeds docs/media/desktop-dark.png (as JPEG) with numbered, pulsing markers.
"""
import base64
import math
import os
import random
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MEDIA = os.path.join(HERE, "..", "docs", "media")
FONT = "-apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"

DARK = dict(bg="#07080c", bg2="#0d0f15", ink="#eef0f4", muted="#a0a6b1", faint="#6b717c", pulse="#ff4d6d",
            pulse2="#ff7a93", line="rgba(238,240,244,.10)", card="#10121a", chip="#151823", chipline="#262a36",
            neb=("#3987e5", "#ff4d6d", "#199e70"), nebop=(.30, .22, .18), star="#ffffff", starop=.7,
            s=("#3987e5", "#d95926", "#199e70", "#9085e9", "#d55181", "#c98500"), cellop=.55, glow=1.0)
LIGHT = dict(bg="#f4f1ea", bg2="#ebe6db", ink="#15161a", muted="#5d6068", faint="#8c8f96", pulse="#d61f4b",
             pulse2="#ff6d8a", line="rgba(21,22,26,.10)", card="#ffffff", chip="#ffffff", chipline="#ddd7ca",
             neb=("#2a78d6", "#d61f4b", "#1baf7a"), nebop=(.16, .12, .10), star="#8c8f96", starop=.35,
             s=("#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#e87ba4", "#eda100"), cellop=.42, glow=.55)


def beat(x, y, w=160, amp=1.0):
    """One ECG beat starting at (x, y), w wide: flat, P wave, QRS spike, T wave, flat."""
    k = w / 160.0
    a = amp
    return ("L{:.1f},{:.1f} q{:.1f},{:.1f} {:.1f},0 h{:.1f} l{:.1f},{:.1f} l{:.1f},{:.1f} l{:.1f},{:.1f} l{:.1f},{:.1f} "
            "h{:.1f} q{:.1f},{:.1f} {:.1f},0 h{:.1f}").format(
        x + 40 * k, y, 6 * k, -7 * a, 12 * k, 10 * k, 4 * k, 6 * a, 6 * k, -40 * a, 6 * k, 50 * a, 5 * k, -16 * a,
        14 * k, 10 * k, -12 * a, 20 * k, 43 * k)


def ecg_path(x0, x1, y, w=160, amps=None):
    d = "M{:.1f},{:.1f} ".format(x0, y)
    x, i = x0, 0
    while x + w <= x1 + 0.1:
        a = amps[i % len(amps)] if amps else 1.0
        d += beat(x, y, w, a) + " "
        x += w
        i += 1
    return d.strip()


def stars(rng, n, w, h, color, op, avoid=None):
    out = []
    for _ in range(n):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.choice([0.7, 0.9, 1.1, 1.4])
        dur = rng.uniform(2.5, 6.5)
        lo = op * rng.uniform(0.15, 0.4)
        out.append('<circle cx="{:.1f}" cy="{:.1f}" r="{}" fill="{}" opacity="{:.2f}">'
                   '<animate attributeName="opacity" values="{:.2f};{:.2f};{:.2f}" dur="{:.1f}s" begin="-{:.1f}s" '
                   'repeatCount="indefinite"/></circle>'.format(x, y, r, color, lo, lo, op, lo, dur, rng.uniform(0, 6)))
    return "\n  ".join(out)


def cell(cid, cx, cy, r, color, t, rng, label=None, value=None):
    """A project cell: translucent sphere with orbiting task dots and a slow float."""
    dots = []
    for _ in range(rng.randint(3, 5)):
        ang = rng.uniform(0, 6.28)
        dist = rng.uniform(0.15, 0.6) * r
        dx, dy = math.cos(ang) * dist, math.sin(ang) * dist
        dr = rng.uniform(0.08, 0.2) * r
        dots.append('<circle cx="{:.1f}" cy="{:.1f}" r="{:.1f}" fill="#fff" opacity="{:.2f}"/>'.format(
            cx + dx, cy + dy, dr, rng.uniform(0.14, 0.3)))
    spin = rng.choice([1, -1]) * 360
    fy = rng.uniform(4, 8)
    lab = ""
    if label:
        lw = max(len(label), len(value)) * 8.4 + 34
        lab = ('<g transform="translate({:.1f},{:.1f})">'
               '<rect x="{:.1f}" y="-22" width="{:.1f}" height="44" rx="10" fill="{}" fill-opacity=".78" stroke="{}" stroke-opacity=".5"/>'
               '<circle cx="{:.1f}" cy="-7" r="4" fill="{}"/>'
               '<text x="{:.1f}" y="-2" font-size="15" font-weight="600" fill="{}">{}</text>'
               '<text x="{:.1f}" y="15" font-size="12.5" fill="{}">{}</text></g>').format(
            cx, cy + 4, -lw / 2, lw, t["card"], t["chipline"], -lw / 2 + 14, color, -lw / 2 + 24, t["ink"], label,
            -lw / 2 + 14, t["muted"], value)
    return """<g>
    <animateTransform attributeName="transform" type="translate" values="0,0;0,-{fy:.1f};0,0" dur="{fd:.1f}s" repeatCount="indefinite" calcMode="spline" keySplines=".45 0 .55 1;.45 0 .55 1"/>
    <circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#g{cid})" stroke="{color}" stroke-opacity=".55" stroke-width="1.2"/>
    <g><animateTransform attributeName="transform" type="rotate" from="0 {cx} {cy}" to="{spin} {cx} {cy}" dur="{sd:.0f}s" repeatCount="indefinite"/>{dots}</g>
    <circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="3" opacity="0">
      <animate attributeName="r" values="{r};{r};{r2};{r2}" keyTimes="0;{arr};{arr2};1" dur="{cyc}s" begin="{beg}s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values="0;.9;0;0" keyTimes="0;{arr};{arr2};1" dur="{cyc}s" begin="{beg}s" repeatCount="indefinite"/>
    </circle>
    {lab}
  </g>""".format(fy=fy, fd=rng.uniform(5, 8), cx=cx, cy=cy, r=r, cid=cid, color=color, spin=spin,
                 sd=rng.uniform(40, 80), dots="".join(dots), r2=r + 26, arr="ARR", arr2="ARR2", cyc="CYC",
                 beg="BEG", lab=lab)


def hero(t, name):
    rng = random.Random(7)
    W, H = 1280, 440
    s = t["s"]
    cells = [  # id, cx, cy, r, color, label, value
        ("a", 1010, 205, 108, s[0], "atlas-api", "€124.58 · 157M"),
        ("b", 1168, 112, 62, s[1], "helix-web", "€77.99 · 91M"),
        ("c", 1176, 300, 70, s[2], "Acme Corp", "€67.05 · 81M"),
        ("d", 858, 318, 50, s[3], None, None),
        ("e", 862, 108, 44, s[4], None, None),
        ("f", 1236, 400, 30, s[5], None, None),
    ]
    grads = "".join(
        '<radialGradient id="g{}" cx="38%" cy="32%" r="75%"><stop offset="0" stop-color="#fff" stop-opacity=".35"/>'
        '<stop offset=".35" stop-color="{}" stop-opacity="{:.2f}"/><stop offset="1" stop-color="{}" stop-opacity=".18"/>'
        '</radialGradient>'.format(c[0], c[4], t["cellop"], c[4]) for c in cells)
    # Sparks: from the ECG trace into a cell, one every 1.1 s, each cell pulsing as it lands.
    CYC = 6.6
    sparks, cell_svgs = [], []
    plan = [("a", 0.0, 470, 380), ("b", 1.1, 610, 382), ("c", 2.2, 700, 378), ("a", 3.3, 520, 376),
            ("d", 4.4, 560, 380), ("e", 5.5, 650, 378)]
    fly = 1.5
    landing = {}
    for cid, beg, sx, sy in plan:
        c = next(x for x in cells if x[0] == cid)
        tx, ty = c[1], c[2]
        mx, my = (sx + tx) / 2 - 40, min(sy, ty) - 120
        path = "M{},{} Q{},{} {},{}".format(sx, sy, mx, my, tx, ty)
        kt = fly / CYC
        for i, (r, op, lag) in enumerate([(5, 1, 0), (3.6, .5, .05), (2.6, .28, .1), (1.8, .15, .15)]):
            sparks.append(
                '<circle r="{}" fill="{}" opacity="0" filter="url(#glow)">'
                '<animateMotion path="{}" keyPoints="0;1;1" keyTimes="0;{:.3f};1" calcMode="linear" dur="{}s" begin="{:.2f}s" repeatCount="indefinite"/>'
                '<animate attributeName="opacity" values="0;{};{};0;0" keyTimes="0;.02;{:.3f};{:.3f};1" dur="{}s" begin="{:.2f}s" repeatCount="indefinite"/>'
                '</circle>'.format(r, "#fff" if i == 0 else t["pulse2"], path, kt, CYC, beg + lag, op, op,
                                   kt * 0.97, kt, CYC, beg + lag))
        # Floating "+tokens · cost" tag at the landing point.
        tag = rng.choice(["+42K · €0.08", "+118K · €0.21", "+67K · €0.11", "+9K · €0.02", "+230K · €0.44"])
        tw = len(tag) * 7.6 + 18
        top = ty - c[3] - 38 if ty - c[3] - 38 > 44 else ty + c[3] + 30
        sparks.append(
            '<g opacity="0"><rect x="{:.1f}" y="{:.1f}" width="{:.1f}" height="24" rx="12" fill="{}" stroke="{}"/>'
            '<text x="{:.1f}" y="{:.1f}" font-size="13" font-weight="600" fill="{}" text-anchor="middle">{}</text>'
            '<animate attributeName="opacity" values="0;0;1;1;0;0" keyTimes="0;{:.3f};{:.3f};{:.3f};{:.3f};1" dur="{}s" begin="{:.2f}s" repeatCount="indefinite"/>'
            '<animateTransform attributeName="transform" type="translate" values="0,0;0,0;0,-26;0,-26" keyTimes="0;{:.3f};{:.3f};1" dur="{}s" begin="{:.2f}s" repeatCount="indefinite"/>'
            '</g>'.format(tx - tw / 2, top, tw, t["card"], t["chipline"], tx, top + 16.5, t["ink"], tag,
                          kt, kt + .03, kt + .17, kt + .24, CYC, beg, kt, kt + .24, CYC, beg))
        landing.setdefault(cid, beg)
    for c in cells:
        g = cell(c[0], c[1], c[2], c[3], c[4], t, rng, c[5], c[6])
        beg = landing.get(c[0], -1)
        if beg < 0:
            g = g.replace('begin="BEGs"', 'begin="0s"').replace('values="0;.9;0;0"', 'values="0;0;0;0"')
        g = (g.replace("ARR2", "{:.3f}".format(fly / CYC + .14)).replace("ARR", "{:.3f}".format(fly / CYC))
             .replace('"CYCs"', '"{}s"'.format(CYC)).replace('"BEGs"', '"{:.2f}s"'.format(max(beg, 0))))
        cell_svgs.append(g)

    ecg = ecg_path(40, 760, 380, 120, amps=[0.55, 0.6, 0.5, 1.0, 0.55, 0.62])
    ecg_len = 1200
    heart = """<g transform="translate(118,118)">
    <g>
      <animateTransform attributeName="transform" type="scale" values="1;1.11;1;1.06;1;1" keyTimes="0;.1;.2;.3;.42;1" dur="1.1s" repeatCount="indefinite"/>
      <circle r="78" fill="url(#halo)" opacity=".9">
        <animate attributeName="r" values="70;96;70;86;70;70" keyTimes="0;.1;.2;.3;.42;1" dur="1.1s" repeatCount="indefinite"/>
      </circle>
      <g transform="scale(1.9) translate(-32,-32)">
        <path d="M32 8c11 0 22 8 23 21 1 12-6 25-21 27C19 58 9 48 9 34S19 8 32 8z" fill="url(#hg)"/>
        <path d="M8 35h13l5-11 7 22 6-15 3 4h14" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="round" stroke-linejoin="round"/>
      </g>
    </g>
  </g>"""
    chips = ["100% local", "API-equivalent cost", "live sparks", "Claude Code + Cursor"]
    cx = 64
    chip_svg = []
    for i, c in enumerate(chips):
        w = len(c) * 8.1 + 36
        chip_svg.append(
            '<g><animate attributeName="opacity" values="0;0;1" keyTimes="0;{:.3f};1" dur="1.2s" fill="freeze"/>'
            '<rect x="{:.1f}" y="292" width="{:.1f}" height="34" rx="17" fill="{}" stroke="{}"/>'
            '<circle cx="{:.1f}" cy="309" r="4" fill="{}"/>'
            '<text x="{:.1f}" y="314" font-size="14.5" font-weight="550" fill="{}">{}</text></g>'.format(
                (0.4 + i * 0.12) / 1.2, cx, w, t["chip"], t["chipline"], cx + 17, [t["pulse"], s[0], s[2], s[3]][i], cx + 28,
                t["ink"], c))
        cx += w + 10
    neb = t["neb"]
    nop = t["nebop"]
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Latidos: your Claude Code and Cursor spend as a living heartbeat">
<title>Latidos</title>
<defs>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="28"/></clipPath>
  <radialGradient id="n1"><stop offset="0" stop-color="{n0}" stop-opacity="{o0}"/><stop offset="1" stop-color="{n0}" stop-opacity="0"/></radialGradient>
  <radialGradient id="n2"><stop offset="0" stop-color="{n1}" stop-opacity="{o1}"/><stop offset="1" stop-color="{n1}" stop-opacity="0"/></radialGradient>
  <radialGradient id="n3"><stop offset="0" stop-color="{n2}" stop-opacity="{o2}"/><stop offset="1" stop-color="{n2}" stop-opacity="0"/></radialGradient>
  <radialGradient id="hg" cx="38%" cy="34%" r="70%"><stop offset="0" stop-color="#ff7a93"/><stop offset=".6" stop-color="#ff4d6d"/><stop offset="1" stop-color="#c2183f"/></radialGradient>
  <radialGradient id="halo"><stop offset="0" stop-color="{pulse}" stop-opacity=".45"/><stop offset="1" stop-color="{pulse}" stop-opacity="0"/></radialGradient>
  <linearGradient id="fade" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".08" stop-color="#fff"/><stop offset=".9" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
  <mask id="ecgmask"><rect x="0" y="300" width="800" height="140" fill="url(#fade)"/></mask>
  <filter id="glow" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  <filter id="soft" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="2.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
  {grads}
</defs>
<g clip-path="url(#frame)" font-family="{font}">
  <rect width="{W}" height="{H}" fill="{bg}"/>
  <g>
    <animateTransform attributeName="transform" type="translate" values="0,0;40,-24;-30,18;0,0" dur="36s" repeatCount="indefinite"/>
    <ellipse cx="300" cy="120" rx="520" ry="300" fill="url(#n1)"/>
    <ellipse cx="1040" cy="330" rx="520" ry="320" fill="url(#n2)"/>
    <ellipse cx="660" cy="470" rx="420" ry="220" fill="url(#n3)"/>
  </g>
  {stars}
  <rect x=".5" y=".5" width="{W1}" height="{H1}" rx="28" fill="none" stroke="{line}"/>
  {heart}
  <text x="206" y="152" font-size="104" font-weight="700" letter-spacing="-2" fill="{ink}">latidos</text>
  <text x="66" y="250" font-size="27" font-weight="500" fill="{ink}">The heartbeat of your AI spend.</text>
  <text x="66" y="276" font-size="17" fill="{muted}">Tokens and API-equivalent cost of Claude Code and Cursor, per project, live.</text>
  {chips}
  <g mask="url(#ecgmask)">
    <path d="{ecg}" fill="none" stroke="{pulse}" stroke-opacity=".16" stroke-width="2"/>
    <path d="{ecg}" fill="none" stroke="{pulse}" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round" filter="url(#soft)" stroke-dasharray="260 {gap}">
      <animate attributeName="stroke-dashoffset" from="260" to="-{L}" dur="4.4s" repeatCount="indefinite"/>
    </path>
  </g>
  {cells}
  {sparks}
</g>
</svg>
""".format(W=W, H=H, W1=W - 1, H1=H - 1, n0=neb[0], n1=neb[1], n2=neb[2], o0=nop[0], o1=nop[1], o2=nop[2],
           pulse=t["pulse"], grads=grads, font=FONT, bg=t["bg"], line=t["line"],
           stars=stars(rng, 70, W, H, t["star"], t["starop"]), heart=heart, ink=t["ink"], muted=t["muted"],
           chips="\n  ".join(chip_svg), ecg=ecg, gap=ecg_len * 2, L=ecg_len + 40, cells="\n  ".join(cell_svgs),
           sparks="\n  ".join(sparks))
    write(name, svg)


def ecg_divider():
    W, H = 1200, 56
    period = 150
    d = ecg_path(-period, W + 5 * period, 34, period, amps=[0.5, 0.55, 0.45, 0.9])
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="heartbeat line">
<defs>
  <linearGradient id="f" x1="0" x2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".2" stop-color="#fff"/><stop offset=".8" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
  <mask id="m"><rect width="{W}" height="{H}" fill="url(#f)"/></mask>
  <filter id="g" x="-10%" y="-60%" width="120%" height="220%"><feGaussianBlur stdDeviation="2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<g mask="url(#m)">
  <g><animateTransform attributeName="transform" type="translate" from="0,0" to="-{P},0" dur="2.4s" repeatCount="indefinite"/>
    <path d="{d}" fill="none" stroke="#ff4d6d" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round" filter="url(#g)"/>
  </g>
</g>
</svg>
""".format(W=W, H=H, d=d, P=period * 4)
    write("ecg.svg", svg)


def odometer():
    """The hero counter rolling into place, as the page does it."""
    W, H = 1200, 300
    value = "634,528,869"
    dh = 150  # digit pitch: neighbours stay outside the window
    x = 64
    cols = []
    for i, ch in enumerate(value):
        if ch == ",":
            cols.append('<text x="{}" y="196" font-size="128" font-weight="300" fill="#eef0f4">,</text>'.format(x))
            x += 34
            continue
        d = int(ch)
        stack = "".join('<text x="{}" y="{}" font-size="128" font-weight="300" fill="#eef0f4">{}</text>'.format(
            x, 196 + k * dh, k % 10) for k in range(0, 20 + 1))
        target = -(10 + d) * dh
        dur = 2.2 + i * 0.12
        cols.append(
            '<g clip-path="url(#win)"><g>'
            '<animateTransform attributeName="transform" type="translate" values="0,0;0,{t};0,{t};0,0" '
            'keyTimes="0;{k1:.3f};.92;1" dur="8s" repeatCount="indefinite" calcMode="spline" '
            'keySplines=".2 .7 .2 1;0 0 1 1;.6 0 .8 .4"/>{s}</g></g>'.format(t=target, k1=dur / 8, s=stack))
        x += 72
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Rolling counter: 634,528,869 tokens, about 522 euros">
<defs>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="24"/></clipPath>
  <clipPath id="win"><rect x="0" y="80" width="{W}" height="136"/></clipPath>
  <linearGradient id="fv" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".2" stop-color="#fff"/><stop offset=".85" stop-color="#fff"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
  <mask id="soft"><rect x="0" y="80" width="{W}" height="136" fill="url(#fv)"/></mask>
  <radialGradient id="n"><stop offset="0" stop-color="#3987e5" stop-opacity=".28"/><stop offset="1" stop-color="#3987e5" stop-opacity="0"/></radialGradient>
  <radialGradient id="n2"><stop offset="0" stop-color="#ff4d6d" stop-opacity=".22"/><stop offset="1" stop-color="#ff4d6d" stop-opacity="0"/></radialGradient>
</defs>
<g clip-path="url(#frame)" font-family="{font}">
  <rect width="{W}" height="{H}" fill="#07080c"/>
  <ellipse cx="250" cy="60" rx="500" ry="220" fill="url(#n)"/>
  <ellipse cx="1050" cy="280" rx="420" ry="200" fill="url(#n2)"/>
  <rect x=".5" y=".5" width="{W1}" height="{H1}" rx="24" fill="none" stroke="rgba(238,240,244,.10)"/>
  <text x="66" y="62" font-size="17" font-weight="600" letter-spacing="3.5" fill="#a0a6b1">LAST 30 DAYS</text>
  <g mask="url(#soft)">{cols}</g>
  <text x="66" y="262" font-size="20" fill="#a0a6b1">tokens processed</text>
  <text x="300" y="262" font-size="34" font-weight="500" fill="#eef0f4">≈ €522.45</text>
  <text x="478" y="262" font-size="18" fill="#a0a6b1">at API prices, had you paid per use</text>
  <g transform="translate(1062,40)">
    <circle r="8" fill="#ff4d6d"><animate attributeName="r" values="7;10;7;9;7;7" keyTimes="0;.1;.2;.3;.42;1" dur="1.1s" repeatCount="indefinite"/></circle>
    <text x="18" y="6" font-size="17" font-weight="600" fill="#ff4d6d">live</text>
  </g>
</g>
</svg>
""".format(W=W, H=H, W1=W - 1, H1=H - 1, font=FONT, cols="\n  ".join(cols))
    write("odometer.svg", svg)


def pipeline():
    W, H = 1200, 330
    nodes = [
        ("Transcripts", "~/.claude/projects", "and Cursor's own files", "#3987e5"),
        ("SQLite", "every call, once", "survives the 30-day cleanup", "#9085e9"),
        ("Priced", "public API rates", "cache tiers · EUR from the ECB", "#c98500"),
        ("Living page", "127.0.0.1:7482", "cells, sparks, river", "#ff4d6d"),
    ]
    nw, gap = 236, 52
    x0 = (W - (4 * nw + 3 * gap)) / 2
    parts = []
    icons = [
        # file stack
        '<rect x="-14" y="-17" width="24" height="30" rx="4" fill="none" stroke="{c}" stroke-width="2.4"/>'
        '<rect x="-8" y="-11" width="24" height="30" rx="4" fill="{bg}" stroke="{c}" stroke-width="2.4"/>'
        '<path d="M-3 -1h13M-3 5h13M-3 11h8" stroke="{c}" stroke-width="2.2" stroke-linecap="round"/>',
        # database
        '<ellipse cx="0" cy="-12" rx="15" ry="6" fill="none" stroke="{c}" stroke-width="2.4"/>'
        '<path d="M-15 -12v24c0 3.3 6.7 6 15 6s15-2.7 15-6v-24M-15 0c0 3.3 6.7 6 15 6s15-2.7 15-6" fill="none" stroke="{c}" stroke-width="2.4"/>',
        # coin
        '<circle r="16" fill="none" stroke="{c}" stroke-width="2.4"/>'
        '<text x="0" y="7" font-size="20" font-weight="700" text-anchor="middle" fill="{c}">€</text>',
        # heart
        '<path d="M0 14c-12-8-17-14-17-21 0-5 4-9 9-9 4 0 6 2 8 5 2-3 4-5 8-5 5 0 9 4 9 9 0 7-5 13-17 21z" fill="{c}" opacity=".9">'
        '<animateTransform attributeName="transform" type="scale" values="1;1.15;1;1.08;1;1" keyTimes="0;.1;.2;.3;.42;1" dur="1.1s" repeatCount="indefinite"/></path>',
    ]
    for i, (title, sub, note, c) in enumerate(nodes):
        x = x0 + i * (nw + gap)
        parts.append("""<g transform="translate({x:.1f},86)">
    <rect width="{nw}" height="168" rx="20" fill="#10121a" stroke="{c}" stroke-opacity=".35"/>
    <rect width="{nw}" height="168" rx="20" fill="none" stroke="{c}" stroke-width="2" opacity="0">
      <animate attributeName="opacity" values="0;0;.9;0;0" keyTimes="0;{a:.3f};{b:.3f};{d:.3f};1" dur="4.8s" repeatCount="indefinite"/>
    </rect>
    <circle cx="44" cy="46" r="26" fill="{c}" fill-opacity=".14"/>
    <g transform="translate(44,46)">{icon}</g>
    <text x="84" y="54" font-size="21" font-weight="650" fill="#eef0f4">{title}</text>
    <text x="22" y="104" font-size="15" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" fill="{c}">{sub}</text>
    <text x="22" y="132" font-size="13.5" fill="#a0a6b1">{note}</text>
  </g>""".format(x=x, nw=nw, c=c, icon=icons[i].format(c=c, bg="#10121a"), title=title, sub=sub, note=note,
                 a=i * 0.25, b=i * 0.25 + 0.06, d=i * 0.25 + 0.2))
        if i < 3:
            ax = x + nw + 6
            bx = ax + gap - 12
            y = 170
            parts.append(
                '<path d="M{ax:.1f} {y} H{bx:.1f}" stroke="#3a3f4c" stroke-width="2" stroke-dasharray="4 6">'
                '<animate attributeName="stroke-dashoffset" from="20" to="0" dur=".8s" repeatCount="indefinite"/></path>'
                '<path d="M{bx2:.1f} {y1} l7 7 -7 7" fill="none" stroke="#6b717c" stroke-width="2" stroke-linecap="round"/>'
                '<circle r="4.5" fill="#fff" filter="url(#g)" opacity="0">'
                '<animateMotion path="M{ax:.1f} {y} H{bx:.1f}" dur="4.8s" begin="{bg:.2f}s" keyPoints="0;0;1;1" keyTimes="0;{k0:.3f};{k1:.3f};1" calcMode="linear" repeatCount="indefinite"/>'
                '<animate attributeName="opacity" values="0;0;1;1;0;0" keyTimes="0;{k0:.3f};{k0b:.3f};{k1a:.3f};{k1:.3f};1" dur="4.8s" repeatCount="indefinite"/></circle>'.format(
                    ax=ax, bx=bx, y=y, bx2=bx - 3, y1=y - 7, bg=0, k0=i * 0.25 + 0.15, k0b=i * 0.25 + 0.17,
                    k1a=i * 0.25 + 0.26, k1=i * 0.25 + 0.28))
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="How it works: transcripts, SQLite, pricing, living page">
<defs>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="24"/></clipPath>
  <radialGradient id="n"><stop offset="0" stop-color="#9085e9" stop-opacity=".22"/><stop offset="1" stop-color="#9085e9" stop-opacity="0"/></radialGradient>
  <filter id="g" x="-200%" y="-200%" width="500%" height="500%"><feGaussianBlur stdDeviation="2.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>
<g clip-path="url(#frame)" font-family="{font}">
  <rect width="{W}" height="{H}" fill="#07080c"/>
  <ellipse cx="600" cy="170" rx="620" ry="220" fill="url(#n)"/>
  <rect x=".5" y=".5" width="{W1}" height="{H1}" rx="24" fill="none" stroke="rgba(238,240,244,.10)"/>
  <text x="{x0:.0f}" y="54" font-size="16" font-weight="600" letter-spacing="3.5" fill="#a0a6b1">HOW IT WORKS</text>
  <text x="{xr:.0f}" y="54" font-size="14" fill="#6b717c" text-anchor="end">read every 10 s while a page is open · nothing leaves the machine</text>
  {parts}
  <text x="{x0:.0f}" y="296" font-size="14" fill="#6b717c">Python standard library only · one SQLite file · no account, no cloud, no telemetry</text>
</g>
</svg>
""".format(W=W, H=H, W1=W - 1, H1=H - 1, font=FONT, parts="\n  ".join(parts), x0=x0, xr=W - x0)
    write("pipeline.svg", svg)


def anatomy():
    """desktop-dark.png with numbered, pulsing markers (README "What it shows")."""
    src = os.path.join(MEDIA, "desktop-dark.png")
    with tempfile.TemporaryDirectory() as tmp:
        jpg = os.path.join(tmp, "a.jpg")
        # Quality 76: the public-readiness scan reads the base64 too, and some qualities
        # happen to spell a denylisted name in it. Re-run the scan after changing it.
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "76", src, "--out", jpg],
                       check=True, capture_output=True)
        data = base64.b64encode(open(jpg, "rb").read()).decode()
    W, H = 1600, 1000
    marks = [(1, 300, 108), (2, 268, 287), (3, 445, 553), (4, 850, 340), (5, 690, 475), (6, 1572, 160),
             (7, 1180, 82), (8, 800, 955), (9, 1462, 820)]
    ms = []
    for i, (n, x, y) in enumerate(marks):
        ms.append("""<g transform="translate({x},{y})">
    <circle r="17" fill="none" stroke="#ff4d6d" stroke-width="2.5">
      <animate attributeName="r" values="17;34" dur="2s" begin="{b:.2f}s" repeatCount="indefinite"/>
      <animate attributeName="opacity" values=".9;0" dur="2s" begin="{b:.2f}s" repeatCount="indefinite"/>
    </circle>
    <circle r="17" fill="#ff4d6d" stroke="#fff" stroke-width="2.5"/>
    <text y="6.5" font-size="18" font-weight="700" fill="#fff" text-anchor="middle">{n}</text>
  </g>""".format(x=x, y=y, n=n, b=i * 0.22))
    svg = """<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Latidos page with numbered parts">
<defs><clipPath id="c"><rect width="{W}" height="{H}" rx="22"/></clipPath></defs>
<g clip-path="url(#c)" font-family="{font}">
  <image width="{W}" height="{H}" xlink:href="data:image/jpeg;base64,{data}" href="data:image/jpeg;base64,{data}"/>
  <rect x=".5" y=".5" width="{W1}" height="{H1}" rx="22" fill="none" stroke="rgba(238,240,244,.18)"/>
  {ms}
</g>
</svg>
""".format(W=W, H=H, W1=W - 1, H1=H - 1, font=FONT, data=data, ms="\n  ".join(ms))
    write("anatomy.svg", svg)


# Measured on the author's Mac (Apple silicon, macOS) on 2026-11-18: the `serve` process
# after 16 h of service, 107k calls ingested from 5 GB of Claude Code transcripts.
FOOTPRINT = dict(ram_mb=43, cpu_avg=3, db_mb=45, calls="107k", logs_gb=5)


def footprint():
    """Resource use: rings that draw themselves and a flat CPU trace with a blip per scan."""
    W, H = 1200, 360
    f = FOOTPRINT
    green, ink, muted, faint = "#199e70", "#eef0f4", "#a0a6b1", "#6b717c"
    tiles = [
        ("RAM", "%d MB" % f["ram_mb"], "resident memory", f["ram_mb"] / 1024.0, "of 1 GB", green),
        ("CPU", "0 %", "between scans · ~%d %% average" % f["cpu_avg"], None, None, "#3987e5"),
        ("DISK", "%d MB" % f["db_mb"], "SQLite for %s calls from %d GB of logs" % (f["calls"], f["logs_gb"]),
         f["db_mb"] / (f["logs_gb"] * 1024.0), "of the logs it reads", "#9085e9"),
        ("DEPENDENCIES", "0", "Python standard library only", 0.0, "pip installs", "#c98500"),
    ]
    tw, gap = 252, 20
    x0 = (W - (4 * tw + 3 * gap)) / 2
    parts = []
    circ = 2 * math.pi * 40
    for i, (label, big, sub, frac, fracl, c) in enumerate(tiles):
        x = x0 + i * (tw + gap)
        if frac is None:
            # CPU: a flat trace with a small blip at each scan, scrolling.
            blips = ""
            for k in range(9):
                bx = k * 60
                blips += "M{} 30 h24 l4 -10 l4 10 h28 ".format(bx)
            gauge = ('<g transform="translate(22,92)"><clipPath id="cpu{i}"><rect width="208" height="44"/></clipPath>'
                     '<g clip-path="url(#cpu{i})"><g><animateTransform attributeName="transform" type="translate" '
                     'from="0,0" to="-60,0" dur="2.2s" repeatCount="indefinite"/>'
                     '<path d="{d}" fill="none" stroke="{c}" stroke-width="2.4" stroke-linejoin="round"/></g></g>'
                     '<text x="0" y="62" font-size="12.5" fill="{faint}">a light blip at each scan, every 10 to 30 s</text></g>'
                     ).format(i=i, d=blips, c=c, faint=faint)
        else:
            dash = max(frac, 0.0) * circ
            gauge = ('<g transform="translate(66,118)">'
                     '<circle r="40" fill="none" stroke="{c}" stroke-opacity=".16" stroke-width="10"/>'
                     '<circle r="40" fill="none" stroke="{c}" stroke-width="10" stroke-linecap="{cap}" '
                     'transform="rotate(-90)" stroke-dasharray="0 {circ:.1f}">'
                     '<animate attributeName="stroke-dasharray" values="0 {circ:.1f};{dash:.1f} {circ:.1f};{dash:.1f} {circ:.1f}" '
                     'keyTimes="0;.25;1" dur="5s" repeatCount="indefinite"/></circle>'
                     '<text y="6" font-size="15" font-weight="700" text-anchor="middle" fill="{c}">{pct}</text>'
                     '<text x="60" y="-6" font-size="13" fill="{muted}">{pctl}</text>'
                     '<text x="60" y="13" font-size="13" fill="{faint}">{fracl}</text></g>'
                     ).format(cap="round" if frac > 0 else "butt", c=c, circ=circ, dash=dash, pct=("%.0f %%" % (frac * 100)) if frac >= 0.01 else ("&lt;1 %" if frac > 0 else "0"),
                              pctl="the rest is free" if frac > 0 else "nothing to install", muted=muted, faint=faint,
                              fracl=fracl)
        parts.append("""<g transform="translate({x:.1f},84)">
    <rect width="{tw}" height="226" rx="20" fill="#10121a" stroke="{c}" stroke-opacity=".35"/>
    <text x="22" y="34" font-size="13" font-weight="700" letter-spacing="2.5" fill="{c}">{label}</text>
    <text x="22" y="76" font-size="38" font-weight="600" fill="{ink}">{big}</text>
    {gauge}
    <text x="22" y="206" font-size="13" fill="{muted}">{sub}</text>
  </g>""".format(x=x, tw=tw, c=c, label=label, ink=ink, big=big, gauge=gauge, muted=muted, sub=sub))
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Footprint: {ram} MB of RAM, 0 percent CPU between scans, {db} MB database, no dependencies">
<defs>
  <clipPath id="frame"><rect width="{W}" height="{H}" rx="24"/></clipPath>
  <radialGradient id="n"><stop offset="0" stop-color="#199e70" stop-opacity=".20"/><stop offset="1" stop-color="#199e70" stop-opacity="0"/></radialGradient>
</defs>
<g clip-path="url(#frame)" font-family="{font}">
  <rect width="{W}" height="{H}" fill="#07080c"/>
  <ellipse cx="300" cy="40" rx="560" ry="220" fill="url(#n)"/>
  <rect x=".5" y=".5" width="{W1}" height="{H1}" rx="24" fill="none" stroke="rgba(238,240,244,.10)"/>
  <text x="{x0:.0f}" y="54" font-size="16" font-weight="600" letter-spacing="3.5" fill="{muted}">LIGHT ON YOUR MACHINE</text>
  <text x="{xr:.0f}" y="54" font-size="13.5" fill="{faint}" text-anchor="end">measured: the live service after 16 h, {calls} calls in its history</text>
  {parts}
  <text x="{x0:.0f}" y="338" font-size="13.5" fill="{faint}">The animation runs in your browser tab, not in the service; close the tab and Latidos only reads new log lines.</text>
</g>
</svg>
""".format(W=W, H=H, W1=W - 1, H1=H - 1, font=FONT, parts="\n  ".join(parts), x0=x0, xr=W - x0, muted=muted,
           faint=faint, ram=f["ram_mb"], db=f["db_mb"], calls=f["calls"])
    write("footprint.svg", svg)


def write(name, svg):
    path = os.path.join(MEDIA, name)
    with open(path, "w") as fh:
        fh.write(svg)
    print("%s %.0f KB" % (os.path.relpath(path), os.path.getsize(path) / 1024))


if __name__ == "__main__":
    os.makedirs(MEDIA, exist_ok=True)
    hero(DARK, "hero-dark.svg")
    hero(LIGHT, "hero-light.svg")
    ecg_divider()
    odometer()
    pipeline()
    anatomy()
    footprint()
