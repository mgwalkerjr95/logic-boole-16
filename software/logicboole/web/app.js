// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// The screen: the waveforms with their decoded boxes, the analyzers, the table of messages, the measurements.
// The capture itself and the decoders are in capture.js, decoders.js and detect.js; demo.js makes the simulated
// signals; the board is reached through the program that serves these pages (/api/...).

import { NCH, CH_IDS, lowerBound, upperBound, fromWords, fromRle, encodeCapture,
  decodeCapture, isCaptureFile, fmtTime, fmtFreq, fmtRate } from './capture.js';
import { decodeI2C, decodeSPI, decodeUART, estimateBaud, annTexts, isError, rowText } from './decoders.js';
import { detect, channelStats } from './detect.js';
import { makeDemo } from './demo.js';
import { sigrokWords, sigrokDefaults, sigrokClean, layout } from './sigrok.js';

// Eight hues for channels 0-7 of each pod, checked for the dark background (colour-blind separation and
// contrast); the label beside each row says which channel it is, so colour is never the only cue.
const COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
const AN_COLORS = ['#2563a8', '#7a4fc0', '#0f7a5a', '#9a5216'];
const BAD = '#a63232', GOLD = '#d8b062', TRACE = '#4fc8ff', LINE = '#233029', MUTED = '#94a29b';
const MONO = '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const ROW_H = 44, Y_HIGH = 22.5, Y_LOW = 38.5, LANE_Y = 3, LANE_H = 15, TR_H = 24;
const RATES = [200e6, 100e6, 50e6, 25e6, 20e6, 10e6, 5e6, 2e6, 1e6, 500e3, 100e3];      // each is 200 MS/s / a whole number
const DURS = [0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5];
const BAUDS = [1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600, 1000000, 2000000];
const TYPE = { i2c: 'I²C', spi: 'SPI', uart: 'Serial' };
const tagOf = (an) => an.type === 'sigrok' ? an.cfg.decoder : TYPE[an.type];     // what an analyzer is called
const SIGROK_ROWS = 50000;                  // of one of sigrok's decoders: what is listed and drawn

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
};
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const num = (x) => x.toLocaleString('en-US');
const plural = (n, word) => `${num(n)} ${word}${n === 1 ? '' : 's'}`;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const pct = (x) => `${(x * 100).toFixed(x < 0.1 || x > 0.9 ? 1 : 0)} %`;

const S = {
  cap: null,
  view: { s0: 0, spp: 1 },            // the sample at the left edge, samples per pixel
  chans: CH_IDS.map((id, i) => ({ id, name: '', auto: false, visible: true, color: COLORS[i & 7] })),
  analyzers: [], nextId: 1,
  autoAnalyze: true,                  // look for buses after every capture, until the user sets analyzers by hand
  rows: [], shown: [],
  radix: 'hex',
  markers: { a: null, b: null },
  hover: null, sel: null, stats: null,
  backend: null, busy: false, overview: null,
  fitted: true,                       // the whole capture is in view: kept so when the window changes size
};
let charW = 6.6;

// ---------------------------------------------------------------- view
const wave = $('wave'), ruler = $('ruler'), overview = $('overview');
const waveW = () => wave.clientWidth || 1;
const xOf = (s) => (s - S.view.s0) / S.view.spp;
const sOf = (x) => S.view.s0 + x * S.view.spp;
const visible = () => S.chans.map((c, i) => i).filter(i => S.chans[i].visible);

function clampView() {
  const v = S.view, N = S.cap.numSamples, W = waveW();
  const widest = N / W * 1.06;
  S.fitted = v.spp >= widest;
  v.spp = clamp(v.spp, Math.min(1 / 64, widest), widest);
  const span = W * v.spp, pad = span * 0.03;
  v.s0 = span >= N + 2 * pad ? (N - span) / 2 : clamp(v.s0, -pad, N - span + pad);
}

function zoomAt(x, factor) {
  const s = sOf(x);
  S.view.spp *= factor;
  clampView();
  S.view.s0 = s - x * S.view.spp;
  clampView();
  invalidate();
}

function fitView() {
  S.view.spp = Infinity;
  clampView();
  invalidate();
}

// Brings samples s ... e into view: centred, and zoomed if they would be too small or too large to read.
function focus(s, e) {
  const W = waveW(), span = Math.max(e - s, 1), now = span / S.view.spp;
  if (now < W * 0.08 || now > W * 0.8) S.view.spp = span / (W * 0.3);
  clampView();
  S.view.s0 = (s + e) / 2 - W * S.view.spp / 2;
  clampView();
  invalidate();
}

function fit(cv, height) {
  const dpr = window.devicePixelRatio || 1;
  if (height != null) cv.style.height = height + 'px';
  const w = cv.clientWidth, h = height != null ? height : cv.clientHeight;
  const pw = Math.round(w * dpr), ph = Math.round(h * dpr);
  if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h };
}

let dirty = false;
function invalidate() {
  if (dirty) return;
  dirty = true;
  requestAnimationFrame(() => { dirty = false; if (S.cap) { drawRuler(); drawWave(); drawOverview(); } });
}

// ---------------------------------------------------------------- the ruler
function niceStep(x) {
  const p = 10 ** Math.floor(Math.log10(x)), m = x / p;
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 5 ? 5 : 10) * p;
}

// The time scale for the view: the step between labelled ticks (seconds), the unit and the decimals to show.
function scale(w) {
  const cap = S.cap, rate = cap.sampleRate, { s0, spp } = S.view;
  const step = niceStep(spp / rate * 120);
  const tL = (s0 - cap.trigger) / rate, tR = (s0 + w * spp - cap.trigger) / rate;
  const big = Math.max(Math.abs(tL), Math.abs(tR));
  const [k, unit] = big >= 1 ? [1, 's'] : big >= 1e-3 ? [1e3, 'ms'] : big >= 1e-6 ? [1e6, 'µs'] : [1e9, 'ns'];
  const decimals = Math.max(0, Math.ceil(-Math.log10(step * k) - 1e-9));
  const mant = Math.round(step / 10 ** Math.floor(Math.log10(step) + 1e-9));
  return { step, minor: step / (mant === 2 ? 4 : 5), tL, tR, k, unit, decimals, rate };
}
const xOfTime = (t) => xOf(t * S.cap.sampleRate + S.cap.trigger);

function drawRuler() {
  const { ctx, w, h } = fit(ruler);
  ctx.clearRect(0, 0, w, h);
  const sc = scale(w);
  ctx.font = '11px ' + MONO;
  ctx.textBaseline = 'middle';
  ctx.fillStyle = LINE;
  for (let n = Math.ceil(sc.tL / sc.minor); n * sc.minor <= sc.tR; n++) ctx.fillRect(Math.round(xOfTime(n * sc.minor)), h - 5, 1, 5);
  for (let n = Math.ceil(sc.tL / sc.step); n * sc.step <= sc.tR; n++) {
    const x = Math.round(xOfTime(n * sc.step));
    ctx.fillStyle = '#3a4d43';
    ctx.fillRect(x, h - 12, 1, 12);
    ctx.fillStyle = MUTED;
    ctx.fillText(n === 0 ? '0' : `${(n * sc.step * sc.k).toFixed(sc.decimals)} ${sc.unit}`, x + 5, 12);
  }
  const flag = (s, text, color) => {
    if (s == null) return;
    const x = Math.round(xOf(s));
    if (x < -20 || x > w + 20) return;
    ctx.fillStyle = color;
    ctx.fillRect(x, h - 16, 1, 16);
    ctx.beginPath();
    ctx.roundRect(x - 7, h - 17, 15, 13, 3);
    ctx.fill();
    ctx.fillStyle = '#0b0b0b';
    ctx.font = 'bold 10px ' + MONO;
    ctx.textAlign = 'center';
    ctx.fillText(text, x + 0.5, h - 10);
    ctx.textAlign = 'left';
  };
  if (S.cap.trigger > 0) flag(S.cap.trigger, 'T', TRACE);
  flag(S.markers.a, 'A', GOLD);
  flag(S.markers.b, 'B', GOLD);
}

// ---------------------------------------------------------------- the waveforms
function drawWave() {
  const vis = visible(), h = Math.max(1, vis.length * ROW_H);
  const { ctx, w } = fit(wave, h);
  const cap = S.cap, { s0, spp } = S.view;
  ctx.clearRect(0, 0, w, h);
  ctx.font = '11px ' + MONO;
  charW = ctx.measureText('0').width;

  const xa = xOf(0), xb = xOf(cap.numSamples);          // beyond the capture's ends
  ctx.fillStyle = '#050807';
  if (xa > 0) ctx.fillRect(0, 0, xa, h);
  if (xb < w) ctx.fillRect(xb, 0, w - xb, h);

  const sc = scale(w);
  ctx.fillStyle = '#131b17';
  for (let n = Math.ceil(sc.tL / sc.step); n * sc.step <= sc.tR; n++) ctx.fillRect(Math.round(xOfTime(n * sc.step)), 0, 1, h);

  if (S.sel) {
    const a = clamp(xOf(S.sel.s), -2, w + 2), b = clamp(xOf(S.sel.e), -2, w + 2);
    ctx.fillStyle = 'rgba(79, 200, 255, 0.09)';
    ctx.fillRect(a, 0, Math.max(1, b - a), h);
  }
  const { a: ma, b: mb } = S.markers;
  if (ma != null && mb != null) {
    const a = clamp(xOf(Math.min(ma, mb)), -2, w + 2), b = clamp(xOf(Math.max(ma, mb)), -2, w + 2);
    ctx.fillStyle = 'rgba(216, 176, 98, 0.08)';
    ctx.fillRect(a, 0, b - a, h);
  }

  vis.forEach((ch, r) => {
    const y = r * ROW_H;
    if (S.hover && S.hover.ch === ch) { ctx.fillStyle = 'rgba(255, 255, 255, 0.03)'; ctx.fillRect(0, y, w, ROW_H - 1); }
    ctx.fillStyle = LINE;
    ctx.fillRect(0, y + ROW_H - 1, w, 1);
    drawTrace(ctx, cap.channels[ch], S.chans[ch].color, y, w);
    drawLane(ctx, ch, y, w);
  });

  const vline = (s, color, dash) => {
    if (s == null) return;
    const x = Math.round(xOf(s)) + 0.5;
    if (x < 0 || x > w) return;
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash(dash || []);
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    ctx.setLineDash([]);
  };
  drawGaps(ctx, w, h);
  if (cap.trigger > 0) vline(cap.trigger, TRACE, [4, 4]);
  vline(ma, GOLD);
  vline(mb, GOLD);
  drawHover(ctx, vis, w);
}

// The stretches that were not recorded: hatched over every row, since what the signals did there is unknown.
let hatch = null;
function drawGaps(ctx, w, h) {
  const gaps = S.cap.gaps;
  if (!gaps || !gaps.length) return;
  if (!hatch) {
    const t = document.createElement('canvas');
    t.width = t.height = 8;
    const c = t.getContext('2d');
    c.strokeStyle = 'rgba(230, 103, 103, 0.55)'; c.lineWidth = 1.2;
    c.beginPath(); c.moveTo(-2, 10); c.lineTo(10, -2); c.moveTo(-2, 2); c.lineTo(2, -2); c.moveTo(6, 10); c.lineTo(10, 6); c.stroke();
    hatch = ctx.createPattern(t, 'repeat');
  }
  const sL = S.view.s0, sR = S.view.s0 + w * S.view.spp;
  let lo = 0, hi = gaps.length;                 // the first gap that ends after the left edge
  while (lo < hi) { const m = (lo + hi) >> 1; if (gaps[m].start + gaps[m].length <= sL) lo = m + 1; else hi = m; }
  let last = -1;
  for (let i = lo; i < gaps.length && gaps[i].start < sR; i++) {
    const a = Math.max(0, Math.floor(xOf(gaps[i].start))), b = Math.min(w, Math.ceil(xOf(gaps[i].start + gaps[i].length)));
    const x = Math.max(a, last), width = Math.max(b, a + 2) - x;
    if (width <= 0) continue;
    ctx.fillStyle = 'rgba(9, 13, 12, 0.72)'; ctx.fillRect(x, 0, width, h);
    ctx.fillStyle = hatch; ctx.fillRect(x, 0, width, h);
    last = x + width;
  }
}

// One channel: a line where the edges are apart, a solid bar where several fall in one pixel.
function drawTrace(ctx, c, color, y, w) {
  const { s0, spp } = S.view, E = c.edges, n = E.length, N = S.cap.numSamples;
  const x0 = Math.max(0, xOf(0)), x1 = Math.min(w, xOf(N));
  if (x1 <= x0) return;
  const yH = y + Y_HIGH, yL = y + Y_LOW;
  let i = upperBound(E, Math.floor(Math.max(0, s0)));
  let lvl = c.initial ^ i & 1;
  const sEnd = Math.min(N, s0 + w * spp);
  const line = new Path2D(), bars = new Path2D();
  line.moveTo(x0, lvl ? yH : yL);
  while (i < n && E[i] <= sEnd) {
    const x = (E[i] - s0) / spp, px = Math.floor(x), next = s0 + (px + 1) * spp;
    const j = i + 1 < n && E[i + 1] < next ? lowerBound(E, next, i + 1) : i + 1;
    if (j - i === 1) {
      line.lineTo(x, lvl ? yH : yL);
      lvl ^= 1;
      line.lineTo(x, lvl ? yH : yL);
    } else {
      line.lineTo(px, lvl ? yH : yL);
      bars.rect(px, yH - 0.75, 1, yL - yH + 1.5);
      lvl ^= j - i & 1;
      line.moveTo(px + 1, lvl ? yH : yL);
    }
    i = j;
  }
  line.lineTo(x1, lvl ? yH : yL);
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.stroke(line);
  ctx.fillStyle = color;
  ctx.globalAlpha = 0.85;
  ctx.fill(bars);
  ctx.globalAlpha = 1;
}

// The decoded boxes above a channel.
function drawLane(ctx, ch, y, w) {
  const { s0, spp } = S.view, sR = s0 + w * spp;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = '11px ' + MONO;
  for (const an of S.analyzers) {
    const list = an.lanes.get(ch);
    if (!list || !list.length) continue;
    let lo = 0, hi = list.length;
    while (lo < hi) { const m = lo + hi >> 1; if (list[m].e < s0) lo = m + 1; else hi = m; }
    let lastPx = -9, lastDot = -99;
    for (let k = lo; k < list.length; k++) {
      const a = list[k];
      if (a.s > sR) break;
      const xa = (a.s - s0) / spp, xb = (a.e - s0) / spp, bw = xb - xa;
      if (a.type === 'start' || a.type === 'stop') {
        const beside = list[a.type === 'start' ? k + 1 : k - 1];
        if (xa - lastDot < 7 || (beside && beside.e > beside.s && (beside.e - beside.s) / spp < 5)) continue;
        lastDot = xa;
        ctx.fillStyle = a.type === 'start' ? '#37c837' : '#e66767';
        ctx.beginPath();
        if (a.type === 'start') ctx.arc(xa, y + LANE_Y + LANE_H / 2, 3.5, 0, 6.2832); else ctx.rect(xa - 3, y + LANE_Y + LANE_H / 2 - 3, 6, 6);
        ctx.fill();
        continue;
      }
      ctx.fillStyle = isError(a) ? BAD : an.color;
      if (bw < 5) {                         // too small for a box: a mark, one per pixel
        const px = Math.floor(xa);
        if (px !== lastPx) { ctx.fillRect(px, y + LANE_Y + 2, Math.max(1, bw - 1), LANE_H - 4); lastPx = px; }
        const next = s0 + (px + 1) * spp;
        let l2 = k + 1, h2 = list.length;
        while (l2 < h2) { const m = l2 + h2 >> 1; if (list[m].s < next) l2 = m + 1; else h2 = m; }
        k = Math.max(k, l2 - 1);
        continue;
      }
      const l = Math.max(xa, -8), r = Math.min(xb, w + 8);
      ctx.beginPath();
      ctx.roundRect(l + 0.5, y + LANE_Y, Math.max(1, r - l - 1), LANE_H, 3);
      ctx.fill();
      const room = r - l - 7;
      const text = annTexts(a, S.radix).find(t => t.length * charW <= room);
      if (text) { ctx.fillStyle = '#fff'; ctx.fillText(text, (l + r) / 2, y + LANE_Y + LANE_H / 2 + 0.5); }
    }
  }
  ctx.textAlign = 'left';
}

// The pulse under the pointer: its ends, its level, and the period it belongs to.
function pulseAt(ch, s) {
  const c = S.cap.channels[ch], E = c.edges, i = upperBound(E, s);
  if (i === 0 || i >= E.length) return null;
  const p = { a: E[i - 1], b: E[i], lvl: c.initial ^ i & 1, p0: null, p1: null };
  if (i + 1 < E.length) { p.p0 = p.a; p.p1 = E[i + 1]; } else if (i >= 2) { p.p0 = E[i - 2]; p.p1 = p.b; }
  return p;
}

function drawHover(ctx, vis, w) {
  const hv = S.hover;
  if (!hv || hv.ch == null || drag) return;
  const row = vis.indexOf(hv.ch);
  const p = row >= 0 && !hv.ann ? pulseAt(hv.ch, hv.s) : null;
  if (!p) return;
  const y = row * ROW_H, yy = y + (p.lvl ? Y_HIGH : Y_LOW);
  const a = clamp(xOf(p.a), -2, w + 2), b = clamp(xOf(p.b), -2, w + 2);
  ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.moveTo(a, yy); ctx.lineTo(b, yy); ctx.stroke();
  if (p.p0 != null) {                       // the period: a bracket under the row
    const c = clamp(xOf(p.p0), -2, w + 2), d = clamp(xOf(p.p1), -2, w + 2), by = y + ROW_H - 3.5;
    ctx.strokeStyle = GOLD; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(c + 0.5, by - 3); ctx.lineTo(c + 0.5, by); ctx.lineTo(d - 0.5, by); ctx.lineTo(d - 0.5, by - 3); ctx.stroke();
  }
}

// ---------------------------------------------------------------- the strip with the whole capture
function drawOverview() {
  const { ctx, w, h } = fit(overview);
  const cap = S.cap, N = cap.numSamples;
  if (!S.overview || S.overview.w !== w) {
    const d = new Float32Array(w);
    for (const ch of visible()) {
      const E = cap.channels[ch].edges;
      for (let px = 0, i = 0; px < w && i < E.length; px++) {
        const j = lowerBound(E, (px + 1) * N / w, i);
        if (j > i) d[px] += 1 + Math.log2(j - i);
        i = j;
      }
    }
    S.overview = { w, d, max: d.reduce((a, b) => Math.max(a, b), 1) };
  }
  ctx.clearRect(0, 0, w, h);
  const { d, max } = S.overview;
  ctx.fillStyle = '#4a5a52';
  for (let px = 0; px < w; px++) {
    if (!d[px]) continue;
    const bh = Math.max(1, d[px] / max * (h - 10));
    ctx.fillRect(px, h - 5 - bh, 1, bh);
  }
  ctx.fillStyle = BAD;
  for (const g of cap.gaps || []) ctx.fillRect(Math.floor(g.start / N * w), h - 4, Math.max(1, Math.ceil(g.length / N * w)), 3);
  const a = clamp(S.view.s0 / N * w, 0, w), b = clamp((S.view.s0 + waveW() * S.view.spp) / N * w, 0, w);
  ctx.fillStyle = 'rgba(79, 200, 255, 0.14)';
  ctx.fillRect(a, 2, Math.max(2, b - a), h - 4);
  ctx.strokeStyle = TRACE; ctx.lineWidth = 1;
  ctx.strokeRect(Math.round(a) + 0.5, 2.5, Math.max(2, Math.round(b - a) - 1), h - 5);
}

// ---------------------------------------------------------------- channel labels
function statText(i) {
  const st = S.stats && S.stats[i];
  if (!st) return '';
  if (st.kind === 'steady') return st.level ? 'steady high' : 'steady low';
  if (st.kind === 'clock') return `clock ${fmtFreq(st.freq)} · ${pct(st.duty)}`;
  if (st.kind === 'pwm') return `PWM ${fmtFreq(st.freq)} · mean ${pct(st.duty)}`;
  return plural(st.edges, 'edge');
}

function renderLabels() {
  const host = $('labels');
  host.replaceChildren();
  let hidden = 0;
  S.chans.forEach((c, i) => {
    if (!c.visible) { hidden++; return; }
    const d = el('div', 'lab');
    d.dataset.ch = i;
    const sw = el('span', 'sw');
    sw.style.background = c.color;
    const nm = el('input', 'nm');
    nm.value = c.name;
    nm.placeholder = 'Add a name';
    nm.spellcheck = false;
    nm.setAttribute('aria-label', `Name of channel ${c.id}`);
    nm.addEventListener('input', () => { c.name = nm.value; c.auto = false; });
    nm.addEventListener('change', renderAnalyzers);
    nm.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === 'Escape') nm.blur(); });
    const hide = el('button', 'hide', '×');
    hide.title = `Hide ${c.id}`;
    hide.addEventListener('click', () => { c.visible = false; S.overview = null; renderLabels(); invalidate(); });
    d.append(sw, el('span', 'id', c.id), nm, hide, el('span', 'st', statText(i)));
    host.append(d);
  });
  const corner = $('hidden');
  corner.replaceChildren();
  if (hidden) {
    const b = el('button', '', `Show ${hidden} hidden`);
    b.addEventListener('click', () => { S.chans.forEach(c => { c.visible = true; }); S.overview = null; renderLabels(); invalidate(); });
    corner.append(b);
  } else {
    corner.textContent = 'Channels';
  }
}

// ---------------------------------------------------------------- analyzers
function run(an) {
  const cap = S.cap, c = an.cfg;
  if (an.type === 'sigrok') return runSigrok(an);
  let r;
  if (an.type === 'i2c' && c.sda === c.scl) r = { error: 'SDA and SCL are the same channel' };
  else if (an.type === 'spi' && [c.mosi, c.miso, c.cs].includes(c.clk)) r = { error: 'the clock is also set as another line' };
  else if (an.type === 'spi' && c.mosi < 0 && c.miso < 0) r = { error: 'set MOSI or MISO' };
  else r = an.type === 'i2c' ? decodeI2C(cap, c) : an.type === 'spi' ? decodeSPI(cap, c) : decodeUART(cap, c);
  an.result = { ann: [], rows: [], stats: {}, ...r };
  setLanes(an);
}

function setLanes(an) {
  an.lanes = new Map();
  for (const a of an.result.ann) {
    let l = an.lanes.get(a.ch);
    if (!l) an.lanes.set(a.ch, l = []);
    l.push(a);
  }
}

// One of sigrok's decoders.  The program that serves the page runs it: the capture is sent there as an .lb16
// file, and what comes back is laid out when it arrives.  Until then the analyzer says that it is decoding.
function runSigrok(an) {
  const cap = S.cap, ticket = an.ticket = (an.ticket || 0) + 1;
  an.result = { ann: [], rows: [], stats: {}, pending: true };
  an.lanes = new Map();
  const q = new URLSearchParams({ words: sigrokWords(an.cfg, CH_IDS), limit: String(SIGROK_ROWS) });
  if ((an.cfg.only || '').trim()) q.set('only', an.cfg.only.trim());
  const current = () => an.ticket === ticket && S.cap === cap && S.analyzers.includes(an);
  fetch('/api/decode?' + q, { method: 'POST', headers: { 'X-LB16': '1' }, body: new Blob(encodeCapture(cap)) })
    .then(async (r) => {
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'the decoders did not run');
      if (!current()) return;
      an.result = { ...layout(j.rows, an.cfg), stats: { total: j.total, listed: j.rows.length } };
      setLanes(an);
    })
    .catch((e) => {
      if (current()) an.result = { ann: [], rows: [], stats: {}, error: e instanceof TypeError ? NO_SERVER : e.message || String(e) };
    })
    .finally(() => {
      if (current()) { renderAnalyzers(); rebuildRows(); invalidate(); }
    });
}

function addAnalyzer(type, cfg, auto = false) {
  const an = { id: S.nextId++, type, cfg: type === 'sigrok' ? sigrokClean(cfg) : { ...cfg }, auto, color: AN_COLORS[S.analyzers.length % AN_COLORS.length] };
  S.analyzers.push(an);
  run(an);
  return an;
}

// One line about an analyzer: where it listens and what it found.
function summary(an) {
  const r = an.result, st = r.stats, c = an.cfg;
  if (r.error) return { text: r.error, err: true };
  if (an.type === 'sigrok') {
    if (r.pending) return { text: 'decoding …' };
    const set = sigrokWords(c, CH_IDS).split(' ').slice(1).join(' ');
    return { text: `${set}${set ? ' · ' : ''}${plural(st.total, 'item')}${st.total > st.listed ? `, the first ${num(st.listed)} shown` : ''}` };
  }
  const ids = (...list) => list.filter(i => i >= 0).map(i => S.chans[i].id).join(' ');
  if (an.type === 'i2c') return { text: `${ids(c.sda, c.scl)} · ${plural(r.rows.length, 'message')} · ${plural(st.bytes, 'byte')}${st.errors ? ` · ${num(st.errors)} cut short` : ''}` };
  if (an.type === 'spi') return { text: `${ids(c.cs, c.clk, c.mosi, c.miso)} · ${plural(st.messages, 'message')} · ${plural(st.words, c.bits === 8 ? 'byte' : 'word')}` };
  return { text: `${ids(c.rx)} · ${num(c.baud)} baud · ${plural(st.bytes, 'byte')}` + (st.framing ? ` · ${plural(st.framing, 'framing error')}` : '') + (st.parity ? ` · ${plural(st.parity, 'parity error')}` : '') };
}

const chLabel = (i) => S.chans[i].id + (S.chans[i].name ? ' ' + S.chans[i].name : '');

function chSelect(an, key, none, from = an.cfg) {
  const s = el('select');
  if (none) s.append(new Option('not used', '-1'));
  for (let i = 0; i < NCH; i++) s.append(new Option(chLabel(i), String(i)));
  s.value = String(from[key]);
  s.addEventListener('change', () => { from[key] = +s.value; changed(an); });
  return s;
}

function optSelect(options, value, set, an) {
  const s = el('select');
  for (const [v, text] of options) s.append(new Option(text, String(v)));
  s.value = String(value);
  s.addEventListener('change', () => { set(s.value); changed(an); });
  return s;
}

function changed(an) {
  an.auto = false;
  S.autoAnalyze = false;
  run(an);
  renderAnalyzers();
  rebuildRows();
  invalidate();
}

function field(text, control) {
  const l = el('label', '', text + ' ');
  l.append(control);
  return l;
}

function renderAnalyzers() {
  const host = $('analyzers');
  host.replaceChildren();
  if (!S.analyzers.length) host.append(el('div', 'note', 'None yet. Auto-detect looks for I²C, SPI and serial traffic in the capture, or add one and choose its channels. "More…" has sigrok\'s decoders for other protocols.'));
  for (const an of S.analyzers) {
    const box = el('div', 'an'), head = el('div', 'anHead'), cfg = el('div', 'anCfg'), c = an.cfg;
    const dot = el('span', 'dot');
    dot.style.background = an.color;
    const sm = summary(an), sum = el('span', 'sum' + (sm.err ? ' err' : ''), sm.text);
    sum.title = sm.text;
    const gear = el('button', '', '⚙');
    gear.title = 'Channels and settings';
    gear.setAttribute('aria-expanded', String(!!an.open));
    gear.addEventListener('click', () => { an.open = !an.open; renderAnalyzers(); });
    const x = el('button', '', '×');
    x.title = 'Remove this analyzer';
    x.addEventListener('click', () => { S.analyzers = S.analyzers.filter(a => a !== an); S.autoAnalyze = false; renderAnalyzers(); rebuildRows(); invalidate(); });
    head.append(dot, el('b', '', tagOf(an)), sum, gear, x);
    if (an.type === 'sigrok') {
      sigrokFields(an, cfg);
    } else if (an.type === 'i2c') {
      cfg.append(field('SDA', chSelect(an, 'sda')), field('SCL', chSelect(an, 'scl')));
    } else if (an.type === 'spi') {
      cfg.append(field('CLK', chSelect(an, 'clk')), field('MOSI', chSelect(an, 'mosi', true)), field('MISO', chSelect(an, 'miso', true)),
        field('CS', chSelect(an, 'cs', true)),
        field('Mode', optSelect([[0, '0'], [1, '1'], [2, '2'], [3, '3']], c.cpol * 2 + c.cpha, (v) => { c.cpol = +v >> 1; c.cpha = +v & 1; }, an)),
        field('Bits', optSelect([[8, '8'], [16, '16'], [24, '24'], [32, '32']], c.bits, (v) => { c.bits = +v; }, an)),
        field('First', optSelect([[1, 'MSB'], [0, 'LSB']], c.msb ? 1 : 0, (v) => { c.msb = v === '1'; }, an)));
    } else {
      const bauds = BAUDS.includes(c.baud) ? BAUDS : [...BAUDS, c.baud].sort((a, b) => a - b);
      const measure = el('button', '', 'Measure');
      measure.title = 'Work the speed out from the pulse widths on this channel';
      measure.addEventListener('click', () => {
        const est = estimateBaud(S.cap, c.rx);
        if (est) { c.baud = est.baud; changed(an); }
      });
      cfg.append(field('Line', chSelect(an, 'rx')),
        field('Baud', optSelect(bauds.map(b => [b, num(b)]), c.baud, (v) => { c.baud = +v; }, an)), measure,
        field('Bits', optSelect([[7, '7'], [8, '8']], c.bits, (v) => { c.bits = +v; }, an)),
        field('Parity', optSelect([['none', 'none'], ['even', 'even'], ['odd', 'odd']], c.parity, (v) => { c.parity = v; }, an)),
        field('Levels', optSelect([[0, 'normal'], [1, 'inverted']], c.invert ? 1 : 0, (v) => { c.invert = v === '1'; }, an)));
    }
    box.append(head);
    if (an.open) box.append(cfg);
    host.append(box);
  }
}

// ---- sigrok's decoders: which there are, and what each takes, as the program that serves the pages tells it
const SG = { list: null, why: '', about: new Map() };

async function sigrokList() {
  if (!SG.list) {
    try {
      const r = await (await fetch('/api/decoders')).json();
      if (r.available) SG.list = r.decoders; else SG.why = r.why;
    } catch {
      SG.why = NO_SERVER;
    }
  }
  return SG.list;
}

async function sigrokAbout(id) {
  if (!SG.about.has(id)) {
    const r = await fetch('/api/decoder?name=' + encodeURIComponent(id)), j = await r.json();
    if (!r.ok) throw new Error(j.error || 'the decoder is not known');
    SG.about.set(id, j);
  }
  return SG.about.get(id);
}

// The settings a decoder starts with.  For the three buses the app decodes itself: the channels of its own
// analyzer for that bus if there is one, else those it would pick.
function sigrokStart(about) {
  const own = S.analyzers.find(a => a.type === about.id);
  const d = !TYPE[about.id] ? null : own ? own.cfg : defaults(about.id);
  const known = !d ? {} : about.id === 'i2c' ? { scl: d.scl, sda: d.sda }
    : about.id === 'spi' ? { clk: d.clk, mosi: d.mosi, miso: d.miso, cs: d.cs } : { rx: d.rx };
  const cfg = sigrokDefaults(about, known);
  if (about.id === 'uart') cfg.options.baudrate = String(d.baud);
  return cfg;
}

async function sigrokPick(an, id) {
  try {
    an.cfg = sigrokStart(await sigrokAbout(id));
  } catch {
    an.cfg = { decoder: id, channels: {}, options: {}, top: '', only: '' };
  }
  changed(an);
}

function textField(value, placeholder, title, set, an, cls) {
  const i = el('input', cls);
  i.type = 'text';
  i.spellcheck = false;
  i.value = value || '';
  i.placeholder = placeholder;
  i.title = title;
  i.addEventListener('change', () => { set(i.value.trim()); changed(an); });
  i.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') i.blur(); });
  return i;
}

function sigrokFields(an, host) {
  const c = an.cfg, about = SG.about.get(c.decoder);
  const pick = el('select', 'wide');
  for (const d of SG.list || [{ id: c.decoder, name: '' }]) pick.append(new Option(d.name ? `${d.id} · ${d.name}` : d.id, d.id));
  pick.value = c.decoder;
  pick.addEventListener('change', () => sigrokPick(an, pick.value));
  host.append(field('Decoder', pick));
  if (!about) {                             // an analyzer that came with a file: its decoder is not known here yet
    if (!an.asked) {
      an.asked = true;
      sigrokList().then(() => sigrokAbout(c.decoder)).then(renderAnalyzers, () => {});
    }
    return;
  }
  for (const ch of about.channels) {
    const s = chSelect(an, ch.id, !ch.required, c.channels);
    s.title = ch.about;
    host.append(field(ch.name, s));
  }
  for (const o of about.options) {
    let ctl;
    if (o.values) {
      ctl = el('select');
      ctl.append(new Option(o.default == null ? 'as it is' : String(o.default), ''));
      for (const v of o.values) if (v !== o.default) ctl.append(new Option(v, v));
      ctl.value = c.options[o.id] || '';
      ctl.title = o.about;
      ctl.addEventListener('change', () => { c.options[o.id] = ctl.value; changed(an); });
    } else {
      ctl = textField(c.options[o.id], o.default == null ? '' : String(o.default), o.about, (v) => { c.options[o.id] = v; }, an);
    }
    host.append(field(o.id, ctl));
  }
  host.append(
    field('On top', textField(c.top, 'no other decoder', 'Another decoder to run on what this one finds, with its settings after its name: eeprom24xx, or lm75', (v) => { c.top = v; }, an, 'wide')),
    field('Only', textField(c.only, 'all but single bits', 'Show only these classes of what the decoder finds, as the table\'s second column names them: address-write,data-write', (v) => { c.only = v; }, an, 'wide')));
}

// The settings of a new analyzer: what auto-detect finds on channels no analyzer uses yet, else the first channels.
function defaults(type) {
  const used = new Set(S.analyzers.flatMap(a => Object.entries(a.cfg).filter(([k]) => ['sda', 'scl', 'clk', 'mosi', 'miso', 'cs', 'rx'].includes(k)).map(([, v]) => v)));
  const found = detect(S.cap).analyzers.find(f => f.type === type && f.channels.every(c => !used.has(c)));
  if (found) return found.cfg;
  if (type === 'i2c') return { sda: 0, scl: 1 };
  if (type === 'spi') return { clk: 3, mosi: 4, miso: 5, cs: 2, cpol: 0, cpha: 0, bits: 8, msb: true, csActive: 0 };
  const rx = S.stats.findIndex(s => s.kind === 'data');
  const est = rx >= 0 ? estimateBaud(S.cap, rx) : null;
  return { rx: Math.max(0, rx), baud: est ? est.baud : 115200, bits: 8, parity: 'none', stop: 1, invert: false, msb: false };
}

// Replaces the analyzers that came from an earlier auto-detect; those the user made or changed stay.
function autoDetect(quiet) {
  S.chans.forEach(c => { if (c.auto) { c.name = ''; c.auto = false; } });
  S.analyzers = S.analyzers.filter(a => !a.auto);
  const found = detect(S.cap).analyzers;
  const added = [];
  for (const f of found) {
    if (S.analyzers.some(a => a.type === f.type && JSON.stringify(a.cfg) === JSON.stringify(f.cfg))) continue;
    addAnalyzer(f.type, f.cfg, true);
    added.push(`${TYPE[f.type]} on ${f.channels.map(c => S.chans[c].id).join(', ')}`);
    for (const [ch, name] of Object.entries(f.names)) {
      if (!S.chans[ch].name) { S.chans[ch].name = name; S.chans[ch].auto = true; }
    }
  }
  const note = $('detectNote');
  note.hidden = quiet;
  if (!quiet) note.textContent = added.length ? `Found ${added.join('; ')}.` : 'No I²C, SPI or serial traffic was recognised in this capture.';
}

// ---------------------------------------------------------------- the table of messages
const tOf = (s) => (s - S.cap.trigger) / S.cap.sampleRate;

function rebuildRows() {
  const rows = [];
  for (const an of S.analyzers) {
    for (const r of an.result.rows) {
      const t = an.type === 'sigrok' ? r : rowText(r, S.radix, an.cfg.bits || 8);
      rows.push({ s: r.s, e: r.e, an, title: t.title, detail: t.detail, error: t.error });
    }
  }
  rows.sort((a, b) => a.s - b.s);
  S.rows = rows;
  filterRows();
}

function filterRows() {
  const q = $('search').value.trim().toLowerCase();
  S.shown = q ? S.rows.filter(r => `${tagOf(r.an)} ${r.title} ${r.detail}`.toLowerCase().includes(q)) : S.rows;
  $('rowCount').textContent = q ? `${num(S.shown.length)} of ${num(S.rows.length)}` : S.rows.length ? num(S.rows.length) : '';
  $('tableSize').style.height = Math.max(S.shown.length * TR_H, 1) + 'px';
  renderTable();
}

function renderTable() {
  const box = $('table'), host = $('tableRows');
  if (!S.shown.length) {
    host.style.top = '0px';
    host.replaceChildren(el('div', 'empty', !S.analyzers.length ? 'Messages appear here once an analyzer is set up.'
      : S.rows.length ? 'No message matches the search.' : 'The analyzers found no messages in this capture.'));
    return;
  }
  const first = Math.max(0, Math.floor(box.scrollTop / TR_H) - 6);
  const last = Math.min(S.shown.length, first + Math.ceil(box.clientHeight / TR_H) + 12);
  host.style.top = first * TR_H + 'px';
  const out = [];
  for (let i = first; i < last; i++) {
    const r = S.shown[i];
    const tr = el('div', 'tr' + (r.error ? ' bad' : '') + (S.sel && S.sel.row === r ? ' sel' : ''));
    tr.dataset.i = i;
    const tag = el('span', 'tag', tagOf(r.an));
    tag.style.background = r.an.color;
    const d = el('span', 'd', r.detail);
    tr.title = `${r.title}  ${r.detail}`;
    tr.append(el('span', 't', fmtTime(tOf(r.s), 6)), tag, el('span', 'ti', r.title), d);
    out.push(tr);
  }
  host.replaceChildren(...out);
}

function selectRow(i, scroll) {
  const r = S.shown[i];
  if (!r) return;
  S.sel = { s: r.s, e: r.e, row: r, index: i };
  if (scroll) {
    const box = $('table'), top = i * TR_H;
    if (top < box.scrollTop) box.scrollTop = top; else if (top + TR_H > box.scrollTop + box.clientHeight) box.scrollTop = top + TR_H - box.clientHeight;
  }
  renderTable();
  focus(r.s, r.e);
}

// ---------------------------------------------------------------- measurements, tooltip
function renderMeasure() {
  const host = $('measure'), cap = S.cap, rate = cap.sampleRate, hv = S.hover;
  const kv = [];
  const { a, b } = S.markers;
  if (a != null) kv.push(['Marker A', fmtTime(tOf(a), 6)]);
  if (b != null) {
    const dt = Math.abs(b - a) / rate;
    kv.push(['Marker B', fmtTime(tOf(b), 6)], ['B − A', `${fmtTime((b - a) / rate, 6)}  (${num(Math.abs(b - a))} samples)`]);
    if (dt > 0) kv.push(['1 / (B − A)', fmtFreq(1 / dt, 6)]);
  }
  const p = hv && hv.ch != null ? pulseAt(hv.ch, hv.s) : null;
  if (hv && hv.ch != null) {
    kv.push(['Pointer', `${fmtTime(tOf(hv.s), 6)} on ${chLabel(hv.ch)}`]);
    if (p) {
      kv.push([p.lvl ? 'High for' : 'Low for', `${fmtTime((p.b - p.a) / rate, 5)}  (${num(p.b - p.a)} samples)`]);
      if (p.p0 != null) {
        const per = (p.p1 - p.p0) / rate, hi = p.lvl ? p.b - p.a : p.p1 - p.p0 - (p.b - p.a);
        kv.push(['Period', `${fmtTime(per, 5)}  ·  ${fmtFreq(1 / per, 5)}`], ['Duty', `${(hi / (p.p1 - p.p0) * 100).toFixed(1)} %`]);
      }
    }
  }
  host.replaceChildren();
  if (!kv.length) {
    host.append(el('span', 'wide', 'Point at a pulse for its width, period and frequency. Shift-drag across the signals, or click the time scale, to set markers A and B.'));
    return;
  }
  for (const [k, v] of kv) host.append(el('span', 'k', k), el('span', 'v', v));
}

function annAt(ch, s) {
  for (const an of S.analyzers) {
    const list = an.lanes.get(ch);
    if (!list) continue;
    let lo = 0, hi = list.length;
    while (lo < hi) { const m = lo + hi >> 1; if (list[m].e < s) lo = m + 1; else hi = m; }
    for (let k = lo; k < Math.min(list.length, lo + 3); k++) {
      const a = list[k];
      if (a.e > a.s && a.s <= s && s <= a.e) return { an, a };
    }
  }
  return null;
}

function showTip(ev) {
  const tip = $('tip'), hv = S.hover;
  if (!hv || hv.ch == null || drag) { tip.hidden = true; return; }
  const rate = S.cap.sampleRate, parts = [];
  const b = el('b', '', chLabel(hv.ch));
  if (hv.ann) {
    parts.push(b, ` · ${tagOf(hv.ann.an)}${hv.ann.a.cls ? ' ' + hv.ann.a.cls : ''} · `, el('span', 'm', annTexts(hv.ann.a, S.radix)[0]),
      el('span', 'k', ` · at ${fmtTime(tOf(hv.ann.a.s), 6)}`));
  } else {
    const p = pulseAt(hv.ch, hv.s);
    if (!p) { tip.hidden = true; return; }
    parts.push(b, ` · ${p.lvl ? 'high' : 'low'} for `, el('span', 'm', fmtTime((p.b - p.a) / rate, 5)));
    if (p.p0 != null) {
      const per = (p.p1 - p.p0) / rate, hi = p.lvl ? p.b - p.a : p.p1 - p.p0 - (p.b - p.a);
      parts.push(el('span', 'k', ' · period '), el('span', 'm', fmtTime(per, 5)), el('span', 'k', ' · '), el('span', 'm', fmtFreq(1 / per, 5)),
        el('span', 'k', ' · duty '), el('span', 'm', pct(hi / (p.p1 - p.p0))));
    }
  }
  tip.replaceChildren(...parts);
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = clamp(ev.clientX + 14, 4, window.innerWidth - r.width - 4) + 'px';
  tip.style.top = (ev.clientY + 18 + r.height > window.innerHeight ? ev.clientY - r.height - 10 : ev.clientY + 18) + 'px';
}

// ---------------------------------------------------------------- pointer and keys
let drag = null, hot = null;

function pos(ev) {
  const r = wave.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  const row = Math.floor(y / ROW_H), vis = visible();
  return { x, y, ch: row >= 0 && row < vis.length ? vis[row] : null, lane: y - row * ROW_H < LANE_Y + LANE_H + 1 };
}

// The nearest edge of the channel within 8 pixels, else the sample itself.
function snap(ch, s) {
  s = clamp(Math.round(s), 0, S.cap.numSamples);
  if (ch == null) return s;
  const E = S.cap.channels[ch].edges, i = lowerBound(E, s);
  let best = s, far = 8 * S.view.spp;
  for (const e of [E[i - 1], E[i]]) if (e != null && Math.abs(e - s) <= far) { best = e; far = Math.abs(e - s); }
  return best;
}

// What the pointer was over no longer holds: there is a new capture, or a key moved the view under it.
function clearHover() {
  S.hover = null;
  if (hot) { hot.classList.remove('hot'); hot = null; }
  $('tip').hidden = true;
}

function setHover(p, ev) {
  if (p && p.ch != null) {
    const s = sOf(p.x);
    S.hover = { ch: p.ch, s, ann: p.lane ? annAt(p.ch, s) : null };
  } else {
    S.hover = null;
  }
  const now = S.hover ? document.querySelector(`.lab[data-ch="${S.hover.ch}"]`) : null;
  if (hot !== now) { if (hot) hot.classList.remove('hot'); if (now) now.classList.add('hot'); hot = now; }
  if (ev) showTip(ev); else $('tip').hidden = true;
  renderMeasure();
  invalidate();
}

wave.addEventListener('pointerdown', (ev) => {
  if (!S.cap || ev.button !== 0) return;
  wave.setPointerCapture(ev.pointerId);
  const p = pos(ev);
  drag = { x: p.x, s0: S.view.s0, moved: false, measure: ev.shiftKey };
  if (drag.measure) {
    S.markers = { a: snap(p.ch, sOf(p.x)), b: null };
    wave.classList.add('measure');
    renderMeasure();
  } else {
    wave.classList.add('drag');
  }
  $('tip').hidden = true;
  invalidate();
});
wave.addEventListener('pointermove', (ev) => {
  if (!S.cap) return;
  const p = pos(ev);
  if (!drag) { setHover(p, ev); return; }
  if (Math.abs(p.x - drag.x) > 2) drag.moved = true;
  if (drag.measure) {
    if (drag.moved) S.markers.b = snap(p.ch, sOf(p.x));
    renderMeasure();
  } else {
    S.view.s0 = drag.s0 - (p.x - drag.x) * S.view.spp;
    clampView();
  }
  invalidate();
});
const endDrag = (ev) => {
  if (!drag) return;
  const was = drag;
  drag = null;
  wave.classList.remove('drag', 'measure');
  if (!was.moved && !was.measure && S.sel) { S.sel = null; renderTable(); }
  setHover(pos(ev), ev);
};
wave.addEventListener('pointerup', endDrag);
wave.addEventListener('pointercancel', endDrag);
wave.addEventListener('pointerleave', () => { if (!drag) setHover(null); });
wave.addEventListener('dblclick', (ev) => zoomAt(pos(ev).x, 1 / 3));

function onWheel(ev) {
  if (!S.cap) return;
  ev.preventDefault();
  const x = ev.clientX - wave.getBoundingClientRect().left;
  const unit = ev.deltaMode ? 16 : 1;
  if (Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) {
    S.view.s0 += ev.deltaX * unit * S.view.spp;
    clampView();
    invalidate();
  } else {
    zoomAt(clamp(x, 0, waveW()), Math.exp(ev.deltaY * unit * (ev.ctrlKey ? 0.012 : 0.0025)));
  }
  if (ev.currentTarget === wave) setHover(pos(ev), ev);
}
wave.addEventListener('wheel', onWheel, { passive: false });
ruler.addEventListener('wheel', onWheel, { passive: false });

ruler.addEventListener('click', (ev) => {
  if (!S.cap) return;
  const s = clamp(Math.round(sOf(ev.clientX - ruler.getBoundingClientRect().left)), 0, S.cap.numSamples);
  if (ev.shiftKey || (S.markers.a != null && S.markers.b == null)) S.markers.b = s; else S.markers = { a: s, b: null };
  renderMeasure();
  invalidate();
});

let overDrag = false;
const overTo = (ev) => {
  const r = overview.getBoundingClientRect();
  S.view.s0 = (ev.clientX - r.left) / r.width * S.cap.numSamples - waveW() * S.view.spp / 2;
  clampView();
  invalidate();
};
overview.addEventListener('pointerdown', (ev) => { if (S.cap) { overDrag = true; overview.setPointerCapture(ev.pointerId); overTo(ev); } });
overview.addEventListener('pointermove', (ev) => { if (overDrag) overTo(ev); });
overview.addEventListener('pointerup', () => { overDrag = false; });
overview.addEventListener('pointercancel', () => { overDrag = false; });

// A key moved the view: the traces under a pointer that stands still are other ones now.
function moved() {
  if (S.hover) { clearHover(); renderMeasure(); }
  invalidate();
}

document.addEventListener('keydown', (ev) => {
  if (!S.cap || ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const t = ev.target, typing = t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA';
  if (t === $('table') && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
    ev.preventDefault();
    const i = S.sel && S.shown[S.sel.index] === S.sel.row ? S.sel.index + (ev.key === 'ArrowDown' ? 1 : -1) : 0;
    selectRow(clamp(i, 0, S.shown.length - 1), true);
    return;
  }
  if (typing) return;
  const W = waveW();
  switch (ev.key) {
    case 'ArrowLeft': S.view.s0 -= W * S.view.spp * 0.2; clampView(); moved(); break;
    case 'ArrowRight': S.view.s0 += W * S.view.spp * 0.2; clampView(); moved(); break;
    case 'ArrowUp': case '+': case '=': zoomAt(W / 2, 1 / 1.6); moved(); break;
    case 'ArrowDown': case '-': case '_': zoomAt(W / 2, 1.6); moved(); break;
    case 'f': case 'F': fitView(); moved(); break;
    case ' ': if (t.tagName !== 'BUTTON') startCapture(); else return; break;
    case 'Escape': S.markers = { a: null, b: null }; S.sel = null; renderMeasure(); renderTable(); invalidate(); break;
    default: return;
  }
  ev.preventDefault();
});

// ---------------------------------------------------------------- captures
// The board waits for its trigger itself (captureDevice).  With simulated signals there is nothing to wait for:
// time zero is put at the capture's first matching edge.
function applyTrigger(cap) {
  cap.trigger = 0;
  if ($('trigCh').value === 'text') {
    return $('trigText').value.trim() ? 'the simulated signals take a channel and an edge as their trigger: a trigger as text is for the board' : '';
  }
  const ch = +$('trigCh').value, want = +$('trigEdge').value;
  if (ch < 0) return '';
  const c = cap.channels[ch], i = (c.initial ^ 1) === want ? 0 : 1;      // edges alternate
  if (i < c.edges.length) { cap.trigger = c.edges[i]; return ''; }
  return `no ${want ? 'rising' : 'falling'} edge on ${S.chans[ch].id} to trigger on`;
}

function setCapture(cap, restore) {
  S.cap = cap;
  S.markers = { a: null, b: null };
  S.sel = null; S.overview = null;
  clearHover();
  S.stats = cap.channels.map((_, ch) => channelStats(cap, ch));
  if (restore) {                            // from a file: its names and analyzers
    S.chans.forEach((c, i) => { c.name = (restore.names && restore.names[i]) || ''; c.auto = false; c.visible = true; });
    S.analyzers = [];
    for (const a of restore.analyzers || []) addAnalyzer(a.type, a.cfg);
    S.autoAnalyze = false;
  } else {
    if (S.autoAnalyze) autoDetect(true);    // drops the analyzers of the last auto-detect and adds what it finds now
    for (const a of S.analyzers) if (!a.auto || !S.autoAnalyze) run(a);
  }
  S.view.spp = Infinity;
  clampView();
  renderLabels();
  renderAnalyzers();
  rebuildRows();
  renderMeasure();
  $('table').scrollTop = 0;
  invalidate();
}

function info(note) {
  const cap = S.cap;
  const parts = [cap.source, fmtRate(cap.sampleRate), `${num(cap.numSamples)} samples`, fmtTime(cap.numSamples / cap.sampleRate),
    `${num(cap.transitions)} transitions`];
  if (cap.trigger) parts.push(`triggered at ${fmtTime(cap.trigger / cap.sampleRate)}`);
  if (cap.gaps && cap.gaps.length) parts.push(`${plural(cap.gaps.length, 'gap')} (${fmtTime(cap.gaps.reduce((n, g) => n + g.length, 0) / cap.sampleRate)} not recorded, hatched)`);
  if (note) parts.push(note);
  $('capInfo').textContent = parts.join('  ·  ');
}

function progress(frac, text) {
  $('progress').hidden = frac == null;
  if (frac != null) $('progressBar').style.width = Math.round(frac * 100) + '%';
  $('startText').textContent = text || 'Start';
  $('start').classList.toggle('busy', frac != null);
}

async function startCapture() {
  if (S.busy) {                             // while the board captures, the button ends the capture
    if (S.stoppable) { S.stoppable = false; $('start').disabled = true; post('/api/stop').catch(() => {}); }
    return;
  }
  S.busy = true;
  $('start').disabled = true;
  try {
    if ($('source').value === 'device') await captureDevice();
    else {
      const rate = +$('rate').value, n = Math.round(rate * +$('dur').value);
      progress(0.5, 'Capturing');
      await sleep(20);
      const cap = makeDemo(rate, n);
      const note = applyTrigger(cap);
      setCapture(cap);
      info(note);
    }
  } catch (e) {
    if ($('source').value === 'device') deviceMessage(e.message || String(e), 'fail'); else $('capInfo').textContent = String(e.message || e);
  } finally {
    S.busy = false;
    S.stoppable = false;
    $('start').disabled = false;
    $('start').title = 'Capture (Space)';
    progress(null);
  }
}

// ---------------------------------------------------------------- the board
const post = (path) => fetch(path, { method: 'POST', headers: { 'X-LB16': '1' } });

// The trigger as the board takes it ("B0=r", or the text that was typed), and as the messages name it.
function triggerSet() {
  const v = $('trigCh').value;
  if (v === 'text') { const words = $('trigText').value.trim(); return { words, name: words }; }
  if (+v < 0) return { words: '', name: '' };
  const rising = +$('trigEdge').value === 1;
  return { words: `${CH_IDS[+v]}=${rising ? 'r' : 'f'}`, name: `${CH_IDS[+v]} ${rising ? 'rising' : 'falling'}` };
}

const EDGE = { r: 'rising edge', f: 'falling edge', e: 'edge' };

function glitchNote(ns) {
  if (!ns || !ns.some(n => n)) return '';
  if (ns.every(n => n === ns[0])) return ` Glitch filter: ${ns[0]} ns on every input.`;
  const parts = [];
  for (const [pod, at] of [['A', 0], ['B', 8]]) {       // a pod with one time for all its inputs is named as a pod
    const mine = ns.slice(at, at + 8);
    if (mine.every(n => n === mine[0])) { if (mine[0]) parts.push(`pod ${pod} ${mine[0]} ns`); continue; }
    mine.forEach((n, i) => { if (n) parts.push(`${CH_IDS[at + i]} ${n} ns`); });
  }
  return ` Glitch filter: ${parts.join(', ')}.`;
}

// The controls that depend on other ones: the trigger as text, what is kept before it, the capture clock.
function controls(focus) {
  const text = $('trigCh').value === 'text', clocked = +$('clockCh').value >= 0;
  $('trigEdge').hidden = text;
  $('trigText').hidden = !text;
  if (text && focus) $('trigText').focus();
  $('devTrigger').hidden = !triggerSet().words;
  $('clockEdge').hidden = $('clockLen').hidden = $('clockNote').hidden = !clocked;
}

async function pollBoard() {
  if ($('source').value !== 'device' || S.busy || document.hidden) return;
  try {
    S.backend = await (await fetch('/api/status')).json();
  } catch {
    S.backend = null;
  }
  renderDevice();
}

const NO_SERVER = 'The program that serves this page does not answer: is "logicboole" still running?';

function renderDevice() {
  const on = $('source').value === 'device';
  $('deviceCard').hidden = !on;
  if (!on) return;
  const b = S.backend && S.backend.board;
  $('devText').textContent = b ? b.text : NO_SERVER;
  $('prepare').hidden = !(b && (b.state === 'boot' || b.state === 'unconfigured'));
  if (b && b.state === 'ready') {           // the thresholds the board has, until others are typed
    for (const [id, v] of [['thA', b.threshold_a], ['thB', b.threshold_b]]) {
      if ($(id).value === '' && document.activeElement !== $(id)) $(id).value = v.toFixed(2);
    }
  }
  if (b && b.state === 'loading') setTimeout(pollBoard, 600);
}

// kind: 'pass', 'fail' or 'note' (a capture of the probes: there is nothing to check it against)
function deviceMessage(text, kind) {
  const box = $('devCheck');
  box.hidden = false;
  box.className = 'devCheck ' + kind;
  box.replaceChildren(el('b', '', { pass: 'Passed. ', fail: 'Failed. ', note: 'Captured. ' }[kind]), text);
}

async function captureDevice() {
  await pollBoardNow();
  const b = S.backend && S.backend.board;
  if (!b || b.state !== 'ready') throw new Error(b ? `No capture was made. ${b.text}` : NO_SERVER);
  const test = $('pattern').value === '1';
  const q = new URLSearchParams({ rate: $('rate').value, seconds: $('dur').value, test: test ? '1' : '0' });
  for (const [key, id] of [['a', 'thA'], ['b', 'thB']]) if ($(id).value !== '') q.set(key, $(id).value);
  const clockCh = +$('clockCh').value, clocked = clockCh >= 0;                  // a sample at each edge of an input
  const count = clocked ? Math.round(+$('clockSamples').value) : Math.round(+$('rate').value * +$('dur').value);
  if (clocked && !(count >= 1)) throw new Error('No capture was made. With a capture clock, Samples is how many to take.');
  if (clocked) { q.set('clock', `${CH_IDS[clockCh]}=${$('clockEdge').value}`); q.set('samples', String(count)); }
  const { words: trigWords, name: trigText } = triggerSet();                    // the board's own trigger
  if ($('trigCh').value === 'text' && !trigWords) throw new Error('No capture was made. The trigger is "other…" and its text is empty: B0=r, or A0=1 for 10us, or B7=f then B0=r.');
  if (trigWords) { q.set('trigger', trigWords); q.set('samples_before', String(Math.round(+$('trigBefore').value * count))); }
  if ($('glitch').value.trim()) q.set('glitch', $('glitch').value.trim());
  if ($('pi').value.trim()) q.set('pi', $('pi').value.trim());
  progress(0, 'Capturing');
  const r = await post('/api/capture?' + q);
  if (!r.ok) throw new Error((await r.json()).error || 'the capture did not start');
  S.stoppable = true;                       // the button now ends the capture: what it has is kept
  $('start').disabled = false;
  $('start').title = 'End the capture (Space)';
  let meta;
  for (;;) {
    await sleep(100);
    const c = (await (await fetch('/api/status')).json()).capture;
    if (c.state === 'running') {
      const stop = S.stoppable ? 'Stop' : 'Stopping';
      if (c.waiting) {
        progress(0, stop);
        $('devText').textContent = `Waiting for the trigger: ${trigText}. Stop ends the wait and shows what the board saw last.`;
      } else {
        progress(c.got / c.want * 0.8, stop);
        $('devText').textContent = trigText ? `Triggered on ${trigText}. Capturing.` : 'Capturing.';
      }
      continue;
    }
    if (c.state === 'error') throw new Error(c.error);
    if (c.state === 'done') { meta = c; break; }
    throw new Error('the capture was lost');
  }
  S.stoppable = false;
  $('start').disabled = true;
  progress(0.85, 'Reading');
  const buf = await (await fetch('/api/data')).arrayBuffer();
  progress(0.92, 'Decoding');
  await sleep(20);
  const w = new Uint32Array(buf, 0, buf.byteLength >> 2);
  const cap = fromRle(w, meta.rate, meta.samples, `Logic Boole 16 · ${test ? 'the FPGA\'s test pattern' : 'probes'}`);
  if (!cap.numSamples) throw new Error('The board sent no samples.');
  const [ckCh, ckEdge] = (meta.clock || '=').split('=');
  const how = `${meta.clock ? `${plural(cap.numSamples, 'sample')}, one at each ${EDGE[ckEdge]} of ${ckCh},` : `${fmtTime(cap.numSamples / meta.rate)} at ${fmtRate(meta.rate)}`} in ${plural(w.length, 'word')} (${(buf.byteLength / 1e6).toFixed(2)} MB), USB at ${meta.usb}`;
  const asked = meta.clock ? plural(meta.asked, 'sample') : fmtTime(meta.asked / meta.rate);
  const piNote = meta.pi ? ` The Pi's lines: ${Object.entries(meta.pi).map(([ch, line]) => `${ch} is ${line}`).join(', ')}.` : '';
  const extra = piNote + glitchNote(meta.glitch_ns) + (meta.clock ? ` The times shown are sample numbers at ${fmtRate(meta.rate)}.` : '');
  const triggered = meta.trigger_sample != null;
  const short = meta.overflow ? ` The capture overflowed: the signals made more data than the board and USB carry, or the computer did not take it fast enough. It ends before the ${asked} asked for, and is whole up to its end.`
    : meta.stopped === 'size' ? ` The capture was ended before the ${asked} asked for: it had grown too large for the browser. A lower sample rate gives a longer one.`
    : meta.stopped === 'stopped' && (triggered || !meta.trigger) ? ` It was stopped before the ${asked} asked for.` : '';
  const trigNote = !meta.trigger ? ''
    : triggered ? ` Triggered on ${trigText}, ${meta.clock ? plural(meta.trigger_sample, 'sample') : fmtTime(meta.trigger_sample / meta.rate)} into the capture: time zero is there.`
    : ` Not triggered: ${trigText} did not come before the capture was stopped. This is what the board saw last.`;
  let kind, text;
  if (meta.problems.length) {
    kind = 'fail';
    text = `${meta.problems.join('; ')}. ${how}.`;
  } else if (test) {
    kind = 'pass';
    text = `The FPGA's test pattern, not the probes: every word as expected. ${how}.${extra}${trigNote}${short}`;
  } else {
    kind = 'note';
    text = `${how}. Thresholds: pod A ${meta.thresholds[0].toFixed(2)} V, pod B ${meta.thresholds[1].toFixed(2)} V.${extra}${trigNote}${short}`;
  }
  cap.info = { meta };
  cap.trigger = triggered ? meta.trigger_sample : 0;
  setCapture(cap);
  info(meta.trigger && !triggered ? 'not triggered' : '');
  deviceMessage(text, kind);
  pollBoardNow();
}

async function pollBoardNow() {
  try { S.backend = await (await fetch('/api/status')).json(); } catch { S.backend = null; }
  renderDevice();
}

$('prepare').addEventListener('click', async () => {
  const btn = $('prepare'), log = $('devLog');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  log.hidden = false;
  log.textContent = 'Loading the firmware into the FX3, then the design into the FPGA …';
  try {
    const r = await (await post('/api/prepare')).json();
    log.textContent = r.log || r.error || '';
  } catch (e) {
    log.textContent = String(e.message || e);
  }
  btn.disabled = false;
  btn.textContent = 'Load the board';
  pollBoardNow();
});

// ---------------------------------------------------------------- files
function download(parts, name, type) {
  const a = el('a');
  a.href = URL.createObjectURL(new Blob(parts, { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function stamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

$('save').addEventListener('click', () => {
  if (!S.cap) return;
  const extra = { names: S.chans.map(c => c.name), analyzers: S.analyzers.map(a => ({ type: a.type, cfg: a.cfg })) };
  download(encodeCapture(S.cap, extra), `logic-boole-${stamp()}.lb16`, 'application/octet-stream');
});
$('open').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', async () => {
  const f = $('file').files[0];
  $('file').value = '';
  if (!f) return;
  try {
    const buf = await f.arrayBuffer();
    if (isCaptureFile(buf)) {
      const { cap, head } = decodeCapture(buf);
      cap.source = f.name;
      setCapture(cap, { names: head.names, analyzers: head.analyzers });
    } else {                                // raw samples: 16 bits each, at the rate chosen in the bar
      const cap = fromWords(new Uint16Array(buf, 0, buf.byteLength >> 1), +$('rate').value, f.name);
      S.analyzers = [];
      S.autoAnalyze = true;
      setCapture(cap);
    }
    info();
  } catch (e) {
    $('capInfo').textContent = `${f.name} could not be opened: ${e.message || e}`;
  }
});

const csv = (v) => /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
$('export').addEventListener('change', () => {
  const what = $('export').value, cap = S.cap;
  $('export').value = '';
  if (!cap || !what) return;
  const lines = [];
  if (what === 'rows') {
    lines.push('start_s,end_s,analyzer,message,data');
    for (const r of S.rows) lines.push([tOf(r.s).toPrecision(10), tOf(r.e).toPrecision(10), csv(tagOf(r.an)), csv(r.title), csv(r.detail)].join(','));
  } else {
    if (cap.transitions > 2e6) { $('capInfo').textContent = `Too many signal changes for a CSV file (${num(cap.transitions)}): save the capture instead.`; return; }
    const vis = visible();
    lines.push('time_s,' + vis.map(i => csv(chLabel(i))).join(','));
    const at = vis.map(() => 0), lvl = vis.map(i => cap.channels[i].initial);
    let s = 0;
    for (;;) {
      lines.push(tOf(s).toPrecision(10) + ',' + lvl.join(','));
      let next = Infinity;
      vis.forEach((ch, k) => { const E = cap.channels[ch].edges; if (at[k] < E.length && E[at[k]] < next) next = E[at[k]]; });
      if (next === Infinity) break;
      vis.forEach((ch, k) => { const E = cap.channels[ch].edges; if (at[k] < E.length && E[at[k]] === next) { at[k]++; lvl[k] ^= 1; } });
      s = next;
    }
  }
  download([lines.join('\n') + '\n'], `logic-boole-${what === 'rows' ? 'messages' : 'signals'}-${stamp()}.csv`, 'text/csv');
});

// ---------------------------------------------------------------- the bar, and the start
for (const r of RATES) $('rate').append(new Option(fmtRate(r), String(r)));
$('rate').value = String(100e6);
for (const d of DURS) $('dur').append(new Option(fmtTime(d), String(d)));
$('dur').value = '0.1';
$('trigCh').append(new Option('none', '-1'));
CH_IDS.forEach((id, i) => $('trigCh').append(new Option(id, String(i))));
$('trigCh').append(new Option('other…', 'text'));
$('clockCh').append(new Option('none', '-1'));
CH_IDS.forEach((id, i) => $('clockCh').append(new Option(id, String(i))));
$('trigCh').addEventListener('change', () => controls(true));
$('trigText').addEventListener('input', () => controls(false));
$('trigText').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { $('trigText').blur(); startCapture(); } });
$('clockCh').addEventListener('change', () => controls(false));

$('start').addEventListener('click', startCapture);
$('zin').addEventListener('click', () => zoomAt(waveW() / 2, 1 / 2));
$('zout').addEventListener('click', () => zoomAt(waveW() / 2, 2));
$('zfit').addEventListener('click', fitView);
$('radix').addEventListener('change', () => { S.radix = $('radix').value; rebuildRows(); invalidate(); });
$('source').addEventListener('change', () => {
  S.autoAnalyze = true;                 // another kind of signal: look for buses afresh
  $('devCheck').hidden = true;
  $('devLog').hidden = true;
  renderDevice();
  pollBoard();
});
$('search').addEventListener('input', filterRows);
$('table').addEventListener('scroll', renderTable);
$('tableRows').addEventListener('click', (ev) => {
  const tr = ev.target.closest('.tr');
  if (tr) selectRow(+tr.dataset.i, false);
});
$('detect').addEventListener('click', () => { if (S.cap) { S.autoAnalyze = true; autoDetect(false); renderLabels(); renderAnalyzers(); rebuildRows(); invalidate(); } });
document.querySelectorAll('[data-add]').forEach(btn => btn.addEventListener('click', () => {
  if (!S.cap) return;
  addAnalyzer(btn.dataset.add, defaults(btn.dataset.add)).open = true;
  S.autoAnalyze = false;
  renderAnalyzers();
  rebuildRows();
  invalidate();
}));
$('addSigrok').addEventListener('click', async () => {
  if (!S.cap) return;
  const note = $('detectNote'), list = await sigrokList();
  note.hidden = !!list;
  if (!list) { note.textContent = SG.why; return; }
  const first = list.some(d => d.id === 'i2c') ? 'i2c' : list[0].id;
  let cfg;
  try {
    cfg = sigrokStart(await sigrokAbout(first));
  } catch (e) {
    note.hidden = false;
    note.textContent = String(e.message || e);
    return;
  }
  addAnalyzer('sigrok', cfg).open = true;
  S.autoAnalyze = false;
  renderAnalyzers();
  rebuildRows();
  invalidate();
});
new ResizeObserver(() => {
  if (!S.cap) return;
  S.overview = null;
  if (S.fitted) S.view.spp = Infinity;
  clampView();
  renderTable();
  invalidate();
}).observe(document.querySelector('.scope'));
setInterval(pollBoard, 2500);

// The page opens on the simulated signals.  With a board on USB the source is the board, ready for Start.
renderDevice();
startCapture().then(async () => {
  await pollBoardNow();
  const b = S.backend && S.backend.board;
  if (b && b.state !== 'none') {
    $('source').value = 'device';
    renderDevice();
  }
});
