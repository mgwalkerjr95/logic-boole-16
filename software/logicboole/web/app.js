// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// The screen: the waveforms with their decoded boxes, the analyzers, the table of messages, the measurements.
// The capture itself and the decoders are in capture.js, decoders.js and detect.js, run by work.js in a worker of
// their own; view.js has the screen's arithmetic and checks; demo.js makes the simulated signals; the board is
// reached through the program that serves these pages (/api/...).

import { NCH, CH_IDS, MAX_EDGES, lowerBound, upperBound, fromWords, fromRle, encodeCapture,
  decodeCapture, isCaptureFile, fmtTime, fmtFreq, fmtRate } from './capture.js';
import { estimateBaud, annTexts, isError, rowText, BAUDS as SPEEDS, MAX_ANN } from './decoders.js';
import { channelStats } from './detect.js';
import { decodeOne, detectAll } from './analyze.js';
import { makeDemo } from './demo.js';
import { sigrokWords, sigrokDefaults, sigrokClean, layout } from './sigrok.js';
import { clampView as clampOf, zoomView, focusView, markerClick, layoutRows, LANE_Y, nameProblem, thresholdProblem,
  parseBaud, trigName, clockName, normalInfo, savedInfo, captureFlags } from './view.js';

// Eight hues for channels 0-7 of each pod, checked for the dark background (colour-blind separation and
// contrast); the label beside each row says which channel it is, so colour is never the only cue.
const COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];
// The analyzers' boxes: white text on each, none of them the red of an error box
const AN_COLORS = ['#2563a8', '#7a4fc0', '#0f7a5a', '#9a5216', '#a3367a', '#3d6e1c', '#1b6f8a', '#6b5a12'];
const BAD = '#a63232', GOLD = '#d8b062', TRACE = '#4fc8ff', LINE = '#233029', MUTED = '#94a29b';
const MONO = '"IBM Plex Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace';
const TR_H = 24;
const RATES = [200e6, 100e6, 50e6, 25e6, 20e6, 10e6, 5e6, 2e6, 1e6, 500e3, 100e3];      // each is 200 MS/s / a whole number
const DURS = [0.001, 0.005, 0.01, 0.05, 0.1, 0.2, 0.5, 1, 2, 5];
const TYPE = { i2c: 'I²C', spi: 'SPI', uart: 'Serial' };
const tagOf = (an) => an.type === 'sigrok' ? an.cfg.decoder : TYPE[an.type];     // what an analyzer is called
const SIGROK_ROWS = 50000;                  // of one of sigrok's decoders: what is listed and drawn
const OPEN_MAX = 1 << 30;                   // bytes of a file the app opens
const UNWATCHED = 10;                       // seconds after which the server ends a wait that no page watches
const PAGE = Math.random().toString(36).slice(2, 10);       // this page, among others open on the app
const NO_SERVER = 'The app\'s server has stopped: the "logicboole" program is not running any more.  Start it again, then reload this page.';

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
  layout: [],                         // the rows as drawn (view.js, layoutRows)
  prev: null,                         // the capture before this one, for Back
  untouched: false,                   // the page opened on simulated signals and nothing has been done since
  off: new Set(), flags: [],          // what the capture did not sample, and what it says of itself (captureFlags)
  detecting: null,                   // the capture auto-detect is looking at
  running: null, stoppable: false,    // the board's capture that runs, as the server last told it; Stop ends it
};
let charW = 6.6;

// ---------------------------------------------------------------- the server
// A request to the program that serves the page.  Its answer as JSON; an Error with the server's own words when
// it refuses, and NO_SERVER when it does not answer at all.
async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, opts.method === 'POST' ? { ...opts, headers: { 'X-LB16': '1', ...(opts.headers || {}) } } : opts);
  } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    throw new Error(NO_SERVER);
  }
  let j = null;
  try { j = await r.json(); } catch { /* not JSON: said below */ }
  if (!r.ok || !j) {
    const e = new Error((j && j.error) || `the app's server answered ${r.status} ${r.statusText}`.trim());
    e.status = r.status;
    e.body = j;
    throw e;
  }
  return j;
}
const post = (path, opts = {}) => api(path, { ...opts, method: 'POST' });
const board = () => S.backend && S.backend.board;
const onUsb = (b) => !!b && b.state !== 'none';

// ---------------------------------------------------------------- view
const wave = $('wave'), ruler = $('ruler'), overview = $('overview');
const waveW = () => wave.clientWidth || 1;
const xOf = (s) => (s - S.view.s0) / S.view.spp;
const sOf = (x) => S.view.s0 + x * S.view.spp;
const visible = () => S.chans.map((c, i) => i).filter(i => S.chans[i].visible);

function clampView() { S.fitted = clampOf(S.view, S.cap.numSamples, waveW()); }

function zoomAt(x, factor) {
  S.fitted = zoomView(S.view, S.cap.numSamples, waveW(), x, factor);
  invalidate();
}

function fitView() {
  S.view.spp = Infinity;
  clampView();
  invalidate();
}

function focus(s, e) {
  S.fitted = focusView(S.view, S.cap.numSamples, waveW(), s, e);
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

// A rectangle with round corners, also where the browser has no roundRect (Safari 15 and before)
function rrect(ctx, x, y, w, h, r) {
  if (ctx.roundRect) { ctx.roundRect(x, y, w, h, r); return; }
  r = Math.max(0, Math.min(r, w / 2, h / 2));
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

let dirty = false;
function invalidate() {
  if (dirty) return;
  dirty = true;
  requestAnimationFrame(() => {
    dirty = false;
    if (S.cap) { drawRuler(); drawWave(); drawOverview(); } else drawEmpty();
  });
}

// The rows: each channel shown, with a lane for each analyzer that draws above it
function rowLayout() {
  const vis = visible();
  const lanes = vis.map(ch => S.analyzers.filter(an => { const l = an.lanes && an.lanes.get(ch); return l && l.length; }));
  return layoutRows(vis, lanes, $('rows').clientHeight);
}

// The labels beside the rows are as tall as the rows
function syncLabels(L) {
  for (const d of $('labels').children) {
    const row = L.find(o => o.ch === +d.dataset.ch);
    if (!row) continue;
    const h = row.h + 'px';
    if (d.style.height !== h) d.style.height = h;
    d.classList.toggle('compact', row.base < 40);
  }
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
    rrect(ctx, x - 7, h - 17, 15, 13, 3);
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
  const L = S.layout = rowLayout(), last = L[L.length - 1], h = Math.max(1, last ? last.y + last.h : 1);
  syncLabels(L);
  const { ctx, w } = fit(wave, h);
  const cap = S.cap;
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

  for (const row of L) {
    if (S.hover && S.hover.ch === row.ch) { ctx.fillStyle = 'rgba(255, 255, 255, 0.03)'; ctx.fillRect(0, row.y, w, row.h - 1); }
    ctx.fillStyle = LINE;
    ctx.fillRect(0, row.y + row.h - 1, w, 1);
    drawTrace(ctx, cap.channels[row.ch], S.chans[row.ch].color, row, w, S.off.has(row.ch));
    drawLanes(ctx, row, w);
  }

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
  drawHover(ctx, w);
}

// No capture: the rows are empty, and a line over them says what to do
function drawEmpty() {
  const L = S.layout = rowLayout(), last = L[L.length - 1];
  syncLabels(L);
  const { ctx, w, h } = fit(wave, Math.max(1, last ? last.y + last.h : 1));
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = LINE;
  for (const row of L) ctx.fillRect(0, row.y + row.h - 1, w, 1);
  for (const cv of [ruler, overview]) { const f = fit(cv); f.ctx.clearRect(0, 0, f.w, f.h); }
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

// One channel: a line where the edges are apart, a solid bar where several fall in one pixel.  A channel the
// capture did not sample (off) is drawn faint.
function drawTrace(ctx, c, color, row, w, off) {
  const { s0, spp } = S.view, E = c.edges, n = E.length, N = S.cap.numSamples;
  const x0 = Math.max(0, xOf(0)), x1 = Math.min(w, xOf(N));
  if (x1 <= x0) return;
  const yH = row.high, yL = row.low;
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
  ctx.globalAlpha = off ? 0.25 : 1;
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.5;
  ctx.setLineDash(off ? [3, 4] : []);
  ctx.stroke(line);
  ctx.setLineDash([]);
  ctx.fillStyle = color;
  ctx.globalAlpha = off ? 0.2 : 0.85;
  ctx.fill(bars);
  ctx.globalAlpha = 1;
}

// The decoded boxes above a channel: a lane for each analyzer that has some there, so that two never overlap.
function drawLanes(ctx, row, w) {
  const { s0, spp } = S.view, sR = s0 + w * spp, H = row.laneH;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = (H < 15 ? '10px ' : '11px ') + MONO;
  row.lanes.forEach((an, k) => {
    const list = an.lanes.get(row.ch), y = row.y + LANE_Y + k * row.step;
    let lo = 0, hi = list.length;
    while (lo < hi) { const m = lo + hi >> 1; if (list[m].e < s0) lo = m + 1; else hi = m; }
    let lastPx = -9, lastDot = -99;
    for (let i = lo; i < list.length; i++) {
      const a = list[i];
      if (a.s > sR) break;
      const xa = (a.s - s0) / spp, xb = (a.e - s0) / spp, bw = xb - xa;
      if (a.type === 'start' || a.type === 'stop') {
        const beside = list[a.type === 'start' ? i + 1 : i - 1];
        if (xa - lastDot < 7 || (beside && beside.e > beside.s && (beside.e - beside.s) / spp < 5)) continue;
        lastDot = xa;
        ctx.fillStyle = a.type === 'start' ? '#37c837' : '#e66767';
        ctx.beginPath();
        if (a.type === 'start') ctx.arc(xa, y + H / 2, 3.5, 0, 6.2832); else ctx.rect(xa - 3, y + H / 2 - 3, 6, 6);
        ctx.fill();
        continue;
      }
      ctx.fillStyle = isError(a) ? BAD : an.color;
      if (bw < 5) {                         // too small for a box: a mark, one per pixel
        const px = Math.floor(xa);
        if (px !== lastPx) { ctx.fillRect(px, y + 2, Math.max(1, bw - 1), H - 4); lastPx = px; }
        const next = s0 + (px + 1) * spp;
        let l2 = i + 1, h2 = list.length;
        while (l2 < h2) { const m = l2 + h2 >> 1; if (list[m].s < next) l2 = m + 1; else h2 = m; }
        i = Math.max(i, l2 - 1);
        continue;
      }
      const l = Math.max(xa, -8), r = Math.min(xb, w + 8);
      ctx.beginPath();
      rrect(ctx, l + 0.5, y, Math.max(1, r - l - 1), H, 3);
      ctx.fill();
      const room = r - l - 7;
      const text = annTexts(a, S.radix).find(t => t.length * charW <= room);
      if (text) { ctx.fillStyle = '#fff'; ctx.fillText(text, (l + r) / 2, y + H / 2 + 0.5); }
    }
  });
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

function drawHover(ctx, w) {
  const hv = S.hover;
  if (!hv || hv.ch == null || drag) return;
  const row = S.layout.find(o => o.ch === hv.ch);
  const p = row && !hv.ann ? pulseAt(hv.ch, hv.s) : null;
  if (!p) return;
  const yy = p.lvl ? row.high : row.low;
  const a = clamp(xOf(p.a), -2, w + 2), b = clamp(xOf(p.b), -2, w + 2);
  ctx.strokeStyle = '#fff'; ctx.lineWidth = 2.5;
  ctx.beginPath(); ctx.moveTo(a, yy); ctx.lineTo(b, yy); ctx.stroke();
  if (p.p0 != null) {                       // the period: a bracket under the row
    const c = clamp(xOf(p.p0), -2, w + 2), d = clamp(xOf(p.p1), -2, w + 2), by = row.y + row.h - 3.5;
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
  if (S.off.has(i)) return 'not sampled';
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
    const d = el('div', 'lab' + (S.off.has(i) ? ' off' : ''));
    d.dataset.ch = i;
    const sw = el('span', 'sw');
    sw.style.background = c.color;
    const nm = el('input', 'nm');
    nm.value = c.name;
    nm.placeholder = 'Add a name';
    nm.spellcheck = false;
    nm.setAttribute('aria-label', `Name of channel ${c.id}`);
    // a name is checked as the command line checks it, so that a capture saved here opens there
    const problem = () => nameProblem(nm.value.trim(), i, S.chans.map(x => x.name));
    nm.addEventListener('input', () => { const why = problem(); nm.classList.toggle('bad', !!why); nm.title = why; });
    nm.addEventListener('change', () => {
      const why = problem();
      nm.classList.remove('bad');
      nm.title = '';
      if (why) { toast(`${why}  ${c.id} keeps ${c.name ? `the name ${c.name}` : 'no name'}.`); nm.value = c.name; return; }
      c.name = nm.value.trim();
      c.auto = false;
      renderAnalyzers();
    });
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
  syncLabels(S.layout);
  invalidate();
}

// ---------------------------------------------------------------- the decoders' worker
// The app's own decoders and auto-detect run in work.js, one request at a time.  A request for an analyzer that
// waits behind another is replaced by a newer one for the same analyzer (key).  Where a browser has no module
// workers they run here, on the page.
const WK = { worker: null, failed: false, queue: [], busy: null, sent: null, next: 1, caps: 0 };

function startWorker() {
  if (WK.worker || WK.failed) return WK.worker;
  try {
    const w = new Worker(new URL('./work.js', import.meta.url), { type: 'module' });
    w.onmessage = (ev) => {
      const job = WK.busy;
      if (!job || job.id !== ev.data.id) return;
      WK.busy = null;
      job.done(ev.data.error ? { error: ev.data.error } : ev.data.result);
      nextWork();
    };
    w.onerror = (ev) => {                   // no module workers here, or the worker died: the page does the work
      if (ev && ev.preventDefault) ev.preventDefault();
      WK.failed = true;
      w.terminate();
      WK.worker = null;
      WK.sent = null;
      if (WK.busy) { WK.queue.unshift(WK.busy); WK.busy = null; }
      nextWork();
    };
    WK.worker = w;
  } catch {
    WK.failed = true;
  }
  return WK.worker;
}

function work(cap, op, args, key) {
  return new Promise((done) => {
    if (WK.busy && WK.busy.cap !== cap && WK.worker) {         // decoding a capture no longer shown: given up
      WK.worker.terminate();
      WK.worker = null;
      WK.sent = null;
      const was = WK.busy;
      WK.busy = null;
      was.done(null);
    }
    for (const j of WK.queue.filter(j => j.cap !== cap)) j.done(null);
    WK.queue = WK.queue.filter(j => j.cap === cap);
    const old = WK.queue.findIndex(j => j.key === key);
    if (old >= 0) WK.queue.splice(old, 1)[0].done(null);        // replaced: of no use now
    WK.queue.push({ cap, op, args, key, done, id: WK.next++ });
    nextWork();
  });
}

function nextWork() {
  if (WK.busy || !WK.queue.length) return;
  const job = WK.busy = WK.queue.shift();
  const w = startWorker();
  if (w) {
    if (!job.cap.wid) job.cap.wid = ++WK.caps;
    if (WK.sent !== job.cap) {
      const c = job.cap;
      w.postMessage({ op: 'capture', id: c.wid, cap: { sampleRate: c.sampleRate, numSamples: c.numSamples, trigger: c.trigger, channels: c.channels } });
      WK.sent = job.cap;
    }
    w.postMessage({ op: job.op, id: job.id, capture: job.cap.wid, ...job.args });
    return;
  }
  setTimeout(() => {                        // here: after the page has shown that it is decoding
    let r;
    try {
      r = job.op === 'detect' ? detectAll(job.cap) : decodeOne(job.cap, job.args.type, job.args.cfg);
    } catch (e) {
      r = { error: String(e.message || e) };
    }
    WK.busy = null;
    job.done(r);
    nextWork();
  }, 0);
}

// Auto-detect's answer for a capture, worked out once
function detected(cap) {
  if (!cap.detected) {
    cap.detected = work(cap, 'detect', {}, 'detect').then((d) => {
      if (!d || d.error) cap.detected = null;               // replaced by a newer capture's, or failed
      return d && !d.error ? d : null;
    });
  }
  return cap.detected;
}

let refreshing = false;
function refresh() {                        // after results come in: once a frame, however many came
  if (refreshing) return;
  refreshing = true;
  requestAnimationFrame(() => { refreshing = false; renderAnalyzers(); rebuildRows(); invalidate(); });
}

// ---------------------------------------------------------------- analyzers
const EMPTY = () => ({ ann: [], rows: [], stats: {} });

function run(an) {
  if (!S.cap) { an.result = EMPTY(); an.lanes = new Map(); return; }
  if (an.type === 'sigrok') return runSigrok(an);
  const cap = S.cap, ticket = an.ticket = (an.ticket || 0) + 1;
  an.result = { ...EMPTY(), pending: true };
  an.lanes = new Map();
  work(cap, 'decode', { type: an.type, cfg: an.cfg }, 'an' + an.id).then((r) => {
    if (!r || an.ticket !== ticket || S.cap !== cap || !S.analyzers.includes(an)) return;
    an.result = { ...EMPTY(), ...r };
    setLanes(an);
    refresh();
  });
}

function setLanes(an) {
  an.lanes = new Map();
  for (const a of an.result.ann) {
    let l = an.lanes.get(a.ch);
    if (!l) an.lanes.set(a.ch, l = []);
    l.push(a);
  }
}

// One of sigrok's decoders.  The program that serves the page runs it: the capture is sent there once as an .lb16
// file and kept there, and what comes back is laid out when it arrives.  Until then the analyzer says that it is
// decoding, with a button to stop it; a change of its settings stops the run before.
function runSigrok(an) {
  const cap = S.cap, ticket = an.ticket = (an.ticket || 0) + 1;
  stopSigrok(an, false);
  an.result = { ...EMPTY(), pending: true };
  an.lanes = new Map();
  const ctl = an.abort = new AbortController();
  const job = an.job = `${PAGE}-${an.id}`;
  const q = new URLSearchParams({ words: sigrokWords(an.cfg, CH_IDS), limit: String(SIGROK_ROWS), job });
  if ((an.cfg.only || '').trim()) q.set('only', an.cfg.only.trim());
  const current = () => an.ticket === ticket && S.cap === cap && S.analyzers.includes(an);
  (async () => {
    for (let again = true; ; again = false) {
      q.set('capture', await uploaded(cap));
      try {
        return await post('/api/decode?' + q, { signal: ctl.signal });
      } catch (e) {
        if (e.status === 404 && e.body && e.body.gone && again) { cap.upload = null; continue; }   // sent again once
        throw e;
      }
    }
  })().then((j) => {
    if (!current()) return;
    an.result = { ...layout(j.rows, an.cfg), stats: { total: j.total, listed: j.rows.length } };
    setLanes(an);
  }).catch((e) => {
    if (!current()) return;
    const stopped = (e && e.name === 'AbortError') || (e && e.body && e.body.cancelled);
    an.result = stopped ? { ...EMPTY(), cancelled: true } : { ...EMPTY(), error: e.message || String(e) };
  }).finally(() => {
    if (current()) { an.abort = null; refresh(); }
  });
}

// The capture, sent to the server once for all of sigrok's decoders: its id there
function uploaded(cap) {
  if (!cap.upload) {
    cap.upload = post('/api/upload', { body: new Blob(encodeCapture(cap)) }).then(j => j.capture);
    cap.upload.catch(() => { cap.upload = null; });
  }
  return cap.upload;
}

// Stops what sigrok's decoders do for an analyzer (removed, its settings changed, or Cancel)
function stopSigrok(an, shown = true) {
  if (!an.abort) return;
  an.abort.abort();
  an.abort = null;
  post('/api/cancel?job=' + encodeURIComponent(an.job)).catch(() => {});
  if (shown) { an.ticket = (an.ticket || 0) + 1; an.result = { ...EMPTY(), cancelled: true }; refresh(); }
}

function newAnalyzer(type, cfg, auto = false) {
  const color = AN_COLORS.find(c => !S.analyzers.some(a => a.color === c)) || AN_COLORS[S.analyzers.length % AN_COLORS.length];
  const an = { id: S.nextId++, type, cfg: type === 'sigrok' ? sigrokClean(cfg) : { ...cfg }, auto, color, result: EMPTY(), lanes: new Map() };
  S.analyzers.push(an);
  return an;
}

// result: what the decoders found already (auto-detect's), else they run now
function addAnalyzer(type, cfg, auto = false, result = null) {
  const an = newAnalyzer(type, cfg, auto);
  if (result) { an.result = { ...EMPTY(), ...result }; setLanes(an); } else run(an);
  return an;
}

const uartFormat = (c) => `${c.bits || 8}${(c.parity || 'none')[0].toUpperCase()}${c.stop || 1}`;

// One line about an analyzer: where it listens and what it found.
function summary(an) {
  const r = an.result, st = r.stats, c = an.cfg;
  if (!S.cap) return { text: 'decodes the next capture' };
  if (r.error) return { text: r.error, err: true };
  if (r.pending) return { text: 'decoding …' };
  if (r.cancelled) return { text: 'stopped before it was done' };
  if (an.type === 'sigrok') {
    const set = sigrokWords(c, CH_IDS).split(' ').slice(1).join(' ');
    return { text: `${set}${set ? ' · ' : ''}${plural(st.total, 'item')}${st.total > st.listed ? `, the first ${num(st.listed)} shown` : ''}` };
  }
  const ids = (...list) => list.filter(i => i >= 0).map(i => S.chans[i].id).join(' ');
  const cut = st.cut != null && S.cap ? ` · decoded up to ${fmtTime(st.cut / S.cap.sampleRate)} only: more than ${num(MAX_ANN)} boxes` : '';
  if (an.type === 'i2c') return { text: `${ids(c.sda, c.scl)} · ${plural(r.rows.length, 'message')} · ${plural(st.bytes, 'byte')}${st.errors ? ` · ${num(st.errors)} cut short` : ''}${cut}` };
  if (an.type === 'spi') return { text: `${ids(c.cs, c.clk, c.mosi, c.miso)} · ${plural(st.messages, 'message')} · ${plural(st.words, c.bits === 8 ? 'byte' : 'word')}${cut}` };
  return { text: `${ids(c.rx)} · ${num(c.baud)} baud${uartFormat(c) === '8N1' ? '' : ' ' + uartFormat(c)} · ${plural(st.bytes, 'byte')}` + (st.framing ? ` · ${plural(st.framing, 'framing error')}` : '') + (st.parity ? ` · ${plural(st.parity, 'parity error')}` : '') + cut };
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
  an.note = '';
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

function removeAnalyzer(an) {
  stopSigrok(an, false);
  an.ticket = (an.ticket || 0) + 1;
  S.analyzers = S.analyzers.filter(a => a !== an);
  S.autoAnalyze = false;
  renderAnalyzers(); rebuildRows(); invalidate();
}

function renderAnalyzers() {
  const host = $('analyzers');
  host.replaceChildren();
  if (!S.analyzers.length) {
    host.append(el('div', 'note', S.detecting && S.detecting === S.cap ? 'Looking for I²C, SPI and serial traffic in the capture …'
      : 'None yet. Auto-detect looks for I²C, SPI and serial traffic in the capture, or add one and choose its channels. "More…" has sigrok\'s decoders for other protocols.'));
  }
  for (const an of S.analyzers) {
    const box = el('div', 'an'), head = el('div', 'anHead'), cfg = el('div', 'anCfg'), c = an.cfg;
    const dot = el('span', 'dot');
    dot.style.background = an.color;
    const sm = summary(an), sum = el('span', 'sum' + (sm.err ? ' err' : ''), sm.text);
    sum.title = sm.text;
    head.append(dot, el('b', '', tagOf(an)), sum);
    if (an.type === 'sigrok' && an.result.pending) {
      const stop = el('button', 'word', 'Cancel');
      stop.title = 'Stop sigrok\'s decoder';
      stop.addEventListener('click', () => stopSigrok(an));
      head.append(stop);
    } else if (an.type === 'sigrok' && (an.result.cancelled || an.result.error)) {
      const again = el('button', 'word', 'Run');
      again.title = 'Run sigrok\'s decoder again';
      again.addEventListener('click', () => { run(an); renderAnalyzers(); });
      head.append(again);
    }
    const gear = el('button', '', '⚙');
    gear.title = 'Channels and settings';
    gear.setAttribute('aria-expanded', String(!!an.open));
    gear.addEventListener('click', () => { an.open = !an.open; renderAnalyzers(); });
    const x = el('button', '', '×');
    x.title = 'Remove this analyzer';
    x.addEventListener('click', () => removeAnalyzer(an));
    head.append(gear, x);
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
      cfg.append(field('Line', chSelect(an, 'rx')), field('Baud', baudField(an)), measureButton(an),
        field('Bits', optSelect([[7, '7'], [8, '8']], c.bits, (v) => { c.bits = +v; }, an)),
        field('Parity', optSelect([['none', 'none'], ['even', 'even'], ['odd', 'odd']], c.parity, (v) => { c.parity = v; }, an)),
        field('Levels', optSelect([[0, 'normal'], [1, 'inverted']], c.invert ? 1 : 0, (v) => { c.invert = v === '1'; }, an)));
    }
    if (an.note) cfg.append(el('p', 'note', an.note));
    box.append(head);
    if (an.open) box.append(cfg);
    host.append(box);
  }
}

// A serial line's speed: any number typed (the list offers the usual ones), or measured from the line
function baudField(an) {
  const c = an.cfg, i = el('input', 'baud');
  i.type = 'text';
  i.inputMode = 'numeric';
  i.spellcheck = false;
  i.value = String(c.baud);
  i.setAttribute('list', 'bauds');
  i.title = 'The speed in baud: pick one, or type any (250000, 1.5M)';
  i.addEventListener('change', () => {
    const v = parseBaud(i.value);
    if (!v) { an.note = `"${i.value.trim()}" is not a speed: a whole number of baud, such as 115200 or 1.5M.`; renderAnalyzers(); return; }
    c.baud = v;
    changed(an);
  });
  i.addEventListener('keydown', (ev) => { if (ev.key === 'Enter') i.blur(); });
  return i;
}

function measureButton(an) {
  const c = an.cfg, b = el('button', '', 'Measure');
  b.title = 'Work the speed out from the pulse widths on this channel';
  b.addEventListener('click', () => {
    if (!S.cap) return;
    const est = estimateBaud(S.cap, c.rx);
    if (!est) {
      an.note = `Measure found no bit time on ${S.chans[c.rx].id}: it needs a few bytes of serial data there, each pulse a whole number of bits.  Type the speed.`;
      renderAnalyzers();
      return;
    }
    c.baud = est.baud;
    changed(an);
    an.note = est.standard ? `Measured: ${num(est.baud)} baud.` : `Measured ${num(est.baud)} baud, which is not one of the usual speeds.`;
    renderAnalyzers();
  });
  return b;
}

// ---- sigrok's decoders: which there are, and what each takes, as the program that serves the pages tells it
const SG = { list: null, why: '', about: new Map() };

async function sigrokList() {
  if (!SG.list) {
    try {
      const r = await api('/api/decoders');
      if (r.available) SG.list = r.decoders; else SG.why = r.why;
    } catch (e) {
      SG.why = e.message || NO_SERVER;
    }
  }
  return SG.list;
}

async function sigrokAbout(id) {
  if (!SG.about.has(id)) SG.about.set(id, await api('/api/decoder?name=' + encodeURIComponent(id)));
  return SG.about.get(id);
}

// The settings a decoder starts with.  For the three buses the app decodes itself: the channels of its own
// analyzer for that bus if there is one, else those it would pick.
async function sigrokStart(about) {
  const own = S.analyzers.find(a => a.type === about.id);
  const d = !TYPE[about.id] ? null : own ? own.cfg : (await defaults(about.id)).cfg;
  const known = !d ? {} : about.id === 'i2c' ? { scl: d.scl, sda: d.sda }
    : about.id === 'spi' ? { clk: d.clk, mosi: d.mosi, miso: d.miso, cs: d.cs } : { rx: d.rx };
  const cfg = sigrokDefaults(about, known);
  if (about.id === 'uart') {
    cfg.options.baudrate = String(d.baud);
    if ((d.bits || 8) !== 8) cfg.options.data_bits = String(d.bits);
    if ((d.parity || 'none') !== 'none') cfg.options.parity = d.parity;
  }
  return cfg;
}

async function sigrokPick(an, id) {
  try {
    an.cfg = await sigrokStart(await sigrokAbout(id));
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

// The settings of a new analyzer, and what it found if that is known: what auto-detect finds on channels no
// analyzer uses yet, else the first channels.
async function defaults(type) {
  const used = new Set(S.analyzers.flatMap(a => Object.entries(a.cfg).filter(([k]) => ['sda', 'scl', 'clk', 'mosi', 'miso', 'cs', 'rx'].includes(k)).map(([, v]) => v)));
  const d = S.cap ? await detected(S.cap) : null;
  const found = d && d.analyzers.find(f => f.type === type && f.channels.every(c => !used.has(c)));
  if (found) return { cfg: found.cfg, result: found.result };
  if (type === 'i2c') return { cfg: { sda: 0, scl: 1 } };
  if (type === 'spi') return { cfg: { clk: 3, mosi: 4, miso: 5, cs: 2, cpol: 0, cpha: 0, bits: 8, msb: true, csActive: 0 } };
  const rx = S.stats ? S.stats.findIndex(s => s.kind === 'data') : -1;
  const est = rx >= 0 && S.cap ? estimateBaud(S.cap, rx) : null;
  return { cfg: { rx: Math.max(0, rx), baud: est ? est.baud : 115200, bits: 8, parity: 'none', stop: 1, invert: false, msb: false } };
}

// Replaces the analyzers that came from an earlier auto-detect; those the user made or changed stay.  quiet: after
// a capture, with nothing said unless something is found.
async function autoDetect(quiet) {
  const cap = S.cap;
  if (!cap) return;
  S.detecting = cap;
  renderAnalyzers();
  const d = await detected(cap);
  if (S.detecting === cap) S.detecting = null;
  if (S.cap !== cap) return;
  if (!d || (quiet && !S.autoAnalyze)) { renderAnalyzers(); return; }
  S.chans.forEach(c => { if (c.auto) { c.name = ''; c.auto = false; } });
  S.analyzers = S.analyzers.filter(a => !a.auto);
  const added = [];
  for (const f of d.analyzers) {
    if (S.analyzers.some(a => a.type === f.type && JSON.stringify(a.cfg) === JSON.stringify(f.cfg))) continue;
    addAnalyzer(f.type, f.cfg, true, f.result);
    const format = f.type === 'uart' && uartFormat(f.cfg) !== '8N1' ? ` (${uartFormat(f.cfg)})` : '';
    added.push(`${TYPE[f.type]}${format} on ${f.channels.map(c => S.chans[c].id).join(', ')}`);
    for (const [ch, want] of Object.entries(f.names)) {      // not a name another channel has been given
      if (S.chans[ch].name) continue;
      const base = want.replace(/[0-9]+$/, ''), names = S.chans.map(c => c.name);
      let name = want;
      for (let k = 2; nameProblem(name, +ch, names); k++) name = base + k;
      S.chans[ch].name = name;
      S.chans[ch].auto = true;
    }
  }
  const note = $('detectNote');
  note.hidden = quiet;
  if (!quiet) note.textContent = added.length ? `Found ${added.join('; ')}.` : 'No I²C, SPI or serial traffic was recognised in this capture.';
  renderLabels(); renderAnalyzers(); rebuildRows(); invalidate();
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
  if (!S.shown.length || !S.cap) {
    host.style.top = '0px';
    const pending = (S.detecting && S.detecting === S.cap) || S.analyzers.some(a => a.result.pending);
    host.replaceChildren(el('div', 'empty', !S.cap ? 'Messages appear here once there is a capture.'
      : pending ? 'Decoding …' : !S.analyzers.length ? 'Messages appear here once an analyzer is set up.'
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
  const host = $('measure'), cap = S.cap, hv = S.hover;
  host.replaceChildren();
  if (!cap) { host.append(el('span', 'wide', 'Point at a pulse for its width, period and frequency, once there is a capture.')); return; }
  const rate = cap.sampleRate, kv = [];
  const { a, b } = S.markers;
  if (a != null) kv.push(['Marker A', fmtTime(tOf(a), 6)]);
  if (a != null && b != null) {
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
  if (!kv.length) {
    host.append(el('span', 'wide', 'Point at a pulse for its width, period and frequency. Shift-drag across the signals, or click the time scale, to set markers A and B.'));
    return;
  }
  for (const [k, v] of kv) host.append(el('span', 'k', k), el('span', 'v', v));
}

function annAt(an, ch, s) {
  const list = an.lanes.get(ch);
  if (!list) return null;
  let lo = 0, hi = list.length;
  while (lo < hi) { const m = lo + hi >> 1; if (list[m].e < s) lo = m + 1; else hi = m; }
  for (let k = lo; k < Math.min(list.length, lo + 3); k++) {
    const a = list[k];
    if (a.e > a.s && a.s <= s && s <= a.e) return { an, a };
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

// A short note at the foot of the screen, for a few seconds
let toastTimer = 0;
function toast(text) {
  const t = $('toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, 7000);
}

// ---------------------------------------------------------------- pointer and keys
let drag = null, hot = null;

function pos(ev) {
  const r = wave.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
  const row = S.layout.find(o => y >= o.y && y < o.y + o.h);
  if (!row) return { x, y, ch: null, lane: null };
  const k = Math.floor((y - row.y - LANE_Y) / row.step);
  return { x, y, ch: row.ch, lane: k >= 0 && k < row.lanes.length ? row.lanes[k] : null };
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
    S.hover = { ch: p.ch, s, ann: p.lane ? annAt(p.lane, p.ch, s) : null };
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
wave.addEventListener('pointerleave', () => { if (!drag && S.cap) setHover(null); });
wave.addEventListener('dblclick', (ev) => { if (S.cap) zoomAt(pos(ev).x, 1 / 3); });

// The wheel zooms over the signals; over the names it scrolls the rows, when there are more than fit
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
  S.markers = markerClick(S.markers, s, ev.shiftKey);
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
  if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
  const t = ev.target, typing = t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA';
  if (typing) return;
  if (ev.key === ' ') {                     // with or without a capture on the screen
    if (t.tagName === 'BUTTON') return;
    ev.preventDefault();
    startCapture();
    return;
  }
  if (ev.key === 'Backspace') {
    if (S.prev && !S.busy) { ev.preventDefault(); goBack(); }
    return;
  }
  if (!S.cap) return;
  if (t === $('table') && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {
    ev.preventDefault();
    const i = S.sel && S.shown[S.sel.index] === S.sel.row ? S.sel.index + (ev.key === 'ArrowDown' ? 1 : -1) : 0;
    selectRow(clamp(i, 0, S.shown.length - 1), true);
    return;
  }
  const W = waveW();
  switch (ev.key) {
    case 'ArrowLeft': S.view.s0 -= W * S.view.spp * 0.2; clampView(); moved(); break;
    case 'ArrowRight': S.view.s0 += W * S.view.spp * 0.2; clampView(); moved(); break;
    case 'ArrowUp': case '+': case '=': zoomAt(W / 2, 1 / 1.6); moved(); break;
    case 'ArrowDown': case '-': case '_': zoomAt(W / 2, 1.6); moved(); break;
    case 'f': case 'F': fitView(); moved(); break;
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

// What is on the screen, kept for Back, and put back by it
function snapshot() {
  return {
    cap: S.cap, analyzers: S.analyzers, autoAnalyze: S.autoAnalyze, stats: S.stats, off: S.off, flags: S.flags,
    results: S.analyzers.map(an => ({ result: an.result, lanes: an.lanes, cfg: JSON.stringify(an.cfg) })),
    names: S.chans.map(c => ({ name: c.name, auto: c.auto })), view: { ...S.view }, markers: { ...S.markers },
    footer: $('capInfo').textContent, check: $('devCheck').hidden ? null : { cls: $('devCheck').className, nodes: [...$('devCheck').childNodes] },
  };
}

function restore(snap) {
  S.cap = snap.cap; S.analyzers = snap.analyzers; S.autoAnalyze = snap.autoAnalyze; S.stats = snap.stats;
  S.off = snap.off; S.flags = snap.flags;
  snap.names.forEach((n, i) => { S.chans[i].name = n.name; S.chans[i].auto = n.auto; });
  S.view = { ...snap.view };
  S.markers = { ...snap.markers };
  S.sel = null; S.overview = null;
  clearHover();
  S.analyzers.forEach((an, i) => {         // what each found in this capture; decoded again if it has changed since
    an.ticket = (an.ticket || 0) + 1;
    stopSigrok(an, false);
    const r = snap.results[i];
    an.result = r.result;
    an.lanes = r.lanes;
    if (an.result.pending || an.result.cancelled || JSON.stringify(an.cfg) !== r.cfg) run(an);
  });
  $('capInfo').textContent = snap.footer;
  if (snap.check) { $('devCheck').className = snap.check.cls; $('devCheck').replaceChildren(...snap.check.nodes); $('devCheck').hidden = false; } else $('devCheck').hidden = true;
  if (S.cap) clampView();
  renderLabels(); renderAnalyzers(); rebuildRows(); renderMeasure(); renderBanner(); renderEmpty();
  $('table').scrollTop = 0;
  invalidate();
}

// Back: the capture before this one, and this one becomes the one before
function goBack() {
  if (!S.prev) return;
  const now = snapshot();
  restore(S.prev);
  S.prev = now.cap ? now : null;
  renderBack();
  toast(S.prev ? 'The capture before is shown.  Back again shows the other one.' : 'The capture before is shown.');
}

function renderBack() {
  $('back').hidden = !S.prev;
}

function setCapture(cap, file) {
  if (S.cap) S.prev = snapshot();           // the one it replaces is kept, for Back
  renderBack();
  S.cap = cap;
  S.markers = { a: null, b: null };
  S.sel = null; S.overview = null;
  clearHover();
  const flags = captureFlags(cap.info);
  S.off = flags.off;
  S.flags = flags.notes;
  S.stats = cap.channels.map((_, ch) => S.off.has(ch) ? { ch, kind: 'off', edges: 0 } : channelStats(cap, ch));
  if (file) {                               // from a file: its names and analyzers
    S.chans.forEach((c, i) => { c.name = (file.names && file.names[i]) || ''; c.auto = false; c.visible = true; });
    S.analyzers = [];
    for (const a of file.analyzers || []) {
      if (a && (TYPE[a.type] || a.type === 'sigrok') && a.cfg && typeof a.cfg === 'object') addAnalyzer(a.type, a.cfg);
    }
    S.autoAnalyze = false;
  } else {
    if (S.autoAnalyze) {                    // the analyzers of the last auto-detect go; what it finds now comes
      S.chans.forEach(c => { if (c.auto) { c.name = ''; c.auto = false; } });
      S.analyzers = S.analyzers.filter(a => !a.auto);
    }
    for (const a of S.analyzers) run(a);
    if (S.autoAnalyze) autoDetect(true);
  }
  S.view.spp = Infinity;
  clampView();
  renderLabels();
  renderAnalyzers();
  rebuildRows();
  renderMeasure();
  renderBanner();
  renderEmpty();
  $('table').scrollTop = 0;
  invalidate();
}

// No capture on the screen: the board is the source and Start takes one
function clearScope() {
  if (S.cap) S.prev = snapshot();
  renderBack();
  S.cap = null;
  S.stats = null; S.off = new Set(); S.flags = [];
  S.markers = { a: null, b: null }; S.sel = null; S.overview = null;
  if (S.autoAnalyze) {                      // what auto-detect found in the capture before is not on the board
    S.chans.forEach(c => { if (c.auto) { c.name = ''; c.auto = false; } });
    S.analyzers = S.analyzers.filter(a => !a.auto);
  }
  for (const an of S.analyzers) { an.ticket = (an.ticket || 0) + 1; stopSigrok(an, false); an.result = EMPTY(); an.lanes = new Map(); }
  clearHover();
  $('capInfo').textContent = '';
  renderLabels(); renderAnalyzers(); rebuildRows(); renderMeasure(); renderBanner(); renderEmpty();
  invalidate();
}

function footer(note) {
  const cap = S.cap;
  const parts = [cap.source, fmtRate(cap.sampleRate), `${num(cap.numSamples)} samples`, fmtTime(cap.numSamples / cap.sampleRate),
    `${num(cap.transitions)} transitions`];
  if (cap.trigger) parts.push(`triggered at ${fmtTime(cap.trigger / cap.sampleRate)}`);
  if (cap.gaps && cap.gaps.length) parts.push(`${plural(cap.gaps.length, 'gap')} (${fmtTime(cap.gaps.reduce((n, g) => n + g.length, 0) / cap.sampleRate)} not recorded, hatched)`);
  if (cap.cut) parts.push(`cut at ${fmtTime(cap.cut.at / cap.sampleRate)} of ${fmtTime(cap.cut.of / cap.sampleRate)}: the page holds ${num(MAX_EDGES)} changes`);
  if (note) parts.push(note);
  $('capInfo').textContent = parts.join('  ·  ');
}

function progress(frac, text) {
  $('progress').hidden = frac == null;
  if (frac != null) $('progressBar').style.width = Math.round(frac * 100) + '%';
  $('startText').textContent = text || 'Start';
  $('start').classList.toggle('busy', frac != null);
}

// While fn runs the board is busy, and the button ends the capture
async function busy(fn) {
  S.busy = true;
  $('start').disabled = true;
  try {
    await fn();
  } catch (e) {
    if ($('source').value === 'device') deviceMessage(e.message || String(e), 'fail'); else $('capInfo').textContent = String(e.message || e);
  } finally {
    S.busy = false;
    S.stoppable = false;
    S.running = null;
    $('start').disabled = false;
    $('start').title = 'Capture (Space)';
    progress(null);
    renderEmpty();
  }
}

async function startCapture() {
  if (S.busy) {                             // while the board captures, the button ends the capture
    if (S.stoppable) { S.stoppable = false; $('start').disabled = true; post('/api/stop').catch(() => {}); }
    return;
  }
  S.untouched = false;
  await busy(async () => {
    if ($('source').value === 'device') return captureDevice();
    const rate = +$('rate').value, n = Math.round(rate * +$('dur').value);
    progress(0.5, 'Capturing');
    await sleep(20);
    const cap = makeDemo(rate, n);
    cap.simulated = true;
    cap.source = 'Simulated signals';
    const note = applyTrigger(cap);
    setCapture(cap);
    footer(note);
  });
}

// ---------------------------------------------------------------- the board
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
function controls(focusText) {
  const text = $('trigCh').value === 'text', clocked = +$('clockCh').value >= 0;
  $('trigEdge').hidden = text;
  $('trigText').hidden = !text;
  if (text && focusText) $('trigText').focus();
  $('devTrigger').hidden = !triggerSet().words;
  $('clockEdge').hidden = $('clockLen').hidden = $('clockNote').hidden = !clocked;
}

// Every few seconds, whatever the source: the board may come or go
async function pollBoard() {
  if (S.busy || document.hidden) return;
  await pollBoardNow();
}

async function pollBoardNow() {
  try { S.backend = await api('/api/status'); } catch { S.backend = null; }
  renderDevice();
  boardNews();
}

// A board has come while the page shows the simulated signals it opened on: the board becomes the source
function boardNews() {
  if (S.untouched && onUsb(board()) && S.cap && S.cap.simulated && $('source').value === 'demo' && !S.busy) {
    S.untouched = false;
    S.autoAnalyze = true;
    $('source').value = 'device';
    clearScope();
    S.prev = null;
    renderBack();
    renderDevice();
    toast('A board is on USB: it is the source now.  Press Start to capture.');
  }
  renderBanner();
}

function renderDevice() {
  const on = $('source').value === 'device';
  $('devMore').hidden = !on;
  const b = board();
  $('devText').textContent = !b ? NO_SERVER : b.state === 'none' ? 'No board on USB.  Plug one in: the app finds it by itself.' : b.text;
  $('prepare').hidden = !(on && b && (b.state === 'boot' || b.state === 'unconfigured'));
  if (b && b.state === 'ready') {           // the thresholds the board has, until others are typed
    for (const [id, v] of [['thA', b.threshold_a], ['thB', b.threshold_b]]) {
      if ($(id).value === '' && document.activeElement !== $(id)) $(id).value = v.toFixed(2);
      if (b.threshold_range) {              // what this board's revision is made for
        $(id).min = b.threshold_range[0];
        $(id).max = b.threshold_range[1];
        $(id).title = `${b.threshold_range[0].toFixed(2)} to ${b.threshold_range[1].toFixed(2)} V on this board`;
      }
    }
  }
  if (b && b.state === 'loading' && !S.busy) setTimeout(pollBoard, 600);
  renderEmpty();
}

// The line over an empty scope
function renderEmpty() {
  const empty = !S.cap;
  $('emptyNote').hidden = !empty;
  if (!empty) return;
  const b = board(), ready = b && b.state === 'ready';
  const run = S.busy && S.running;
  $('emptyMain').textContent = run && run.waiting ? 'Waiting for the trigger …' : S.busy ? 'Capturing …'
    : $('source').value === 'device' && !ready ? 'No capture yet' : 'Press Start to capture';
  $('emptySub').textContent = run ? runningText(run) : S.busy ? '' : $('source').value !== 'device' ? 'Source: simulated signals'
    : !b ? NO_SERVER : ready ? `${b.text}  Space starts a capture too.` : b.text;
}

// The strip over the waveforms: simulated signals are never taken for the board's
function renderBanner() {
  const box = $('banner'), b = board(), cap = S.cap;
  let kind = '', text = '', button = '';
  if (cap && cap.simulated) {
    kind = 'sim';
    if (!S.backend) text = 'Simulated signals: the app\'s server does not answer';
    else if (!onUsb(b)) text = 'Simulated signals: no board on USB';
    else if ($('source').value === 'demo') { text = 'Simulated signals, not the board\'s'; button = 'Use the board'; } else text = 'Simulated signals: press Start to capture from the board';
  } else if (cap && cap.opened && S.flags.length) {        // a file's: a capture taken here has said it in the card
    kind = 'warn';
    text = S.flags.join('  ');
  }
  box.hidden = !kind;
  box.className = 'banner ' + kind;
  $('bannerText').textContent = text;
  $('bannerBtn').hidden = !button;
  $('bannerBtn').textContent = button;
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
  const b = board();
  if (!b || b.state !== 'ready') throw new Error(b ? `No capture was made. ${b.text}` : NO_SERVER);
  const test = $('pattern').value === '1';
  const q = new URLSearchParams({ rate: $('rate').value, seconds: $('dur').value, test: test ? '1' : '0' });
  for (const [key, id, pod] of [['a', 'thA', 'A'], ['b', 'thB', 'B']]) {
    if ($(id).value === '') continue;
    const why = thresholdProblem($(id).value, b.threshold_range, pod);       // refused here, before the board is asked
    if (why) throw new Error(`No capture was made. ${why}`);
    q.set(key, $(id).value);
  }
  const clockCh = +$('clockCh').value, clocked = clockCh >= 0;                  // a sample at each edge of an input
  const count = clocked ? Math.round(+$('clockSamples').value) : Math.round(+$('rate').value * +$('dur').value);
  if (clocked && !(count >= 1)) throw new Error('No capture was made. With a capture clock, Samples is how many to take.');
  if (clocked) { q.set('clock', `${CH_IDS[clockCh]}=${$('clockEdge').value}`); q.set('samples', String(count)); }
  const { words: trigWords } = triggerSet();                                    // the board's own trigger
  if ($('trigCh').value === 'text' && !trigWords) throw new Error('No capture was made. The trigger is "other…" and its text is empty: B0=r, or A0=1 for 10us, or B7=f then B0=r.');
  if (trigWords) { q.set('trigger', trigWords); q.set('samples_before', String(Math.round(+$('trigBefore').value * count))); }
  if ($('glitch').value.trim()) q.set('glitch', $('glitch').value.trim());
  if ($('pi').value.trim()) q.set('pi', $('pi').value.trim());
  progress(0, 'Capturing');
  $('devCheck').hidden = true;
  const s = await post('/api/capture?' + q);
  await watch(s.capture);
}

// What the board is doing for a capture that runs, as a line for the card
function runningText(c) {
  const s = c.settings || {}, trig = trigName(s.trigger), clock = s.clock ? clockName(s.clock) : '';
  if (c.waiting) {
    return `Waiting for the trigger: ${trig}${clock ? `, with a sample at each ${clock}` : ''}. Stop ends the wait and shows what the board saw last.`;
  }
  const done = `${Math.round(c.got / 10)} %`;
  if (clock) return `${trig ? `Triggered on ${trig}. ` : ''}Waiting for the capture clock: a sample at each ${clock}, ${done} of ${plural(+s.samples || 0, 'sample')} taken. Stop ends the capture and keeps what it has.`;
  return trig ? `Triggered on ${trig}. Capturing.` : 'Capturing.';
}

// Follows a capture that runs until it ends, then shows it.  job: its status as the server gives it.  The server
// holds each answer until something changes (wait=), so no timer of the browser's paces this: a page in a
// background tab follows it as closely as one in front.
async function watch(job) {
  S.stoppable = true;                       // the button now ends the capture: what it has is kept
  $('start').disabled = false;
  $('start').title = 'End the capture (Space)';
  let c = job;
  while (c.state === 'running') {
    S.running = c;
    const stop = S.stoppable ? 'Stop' : 'Stopping';
    progress(c.waiting ? 0 : c.got / c.want * 0.8, stop);
    $('devText').textContent = runningText(c);
    renderEmpty();
    const waits = c.waiting || (c.settings && c.settings.clock);
    c = (await api(`/api/status?wait=${waits ? 4 : 0.15}`)).capture;
    if (c.id !== job.id) throw new Error('the capture was lost: another page started one in its place');
  }
  S.running = null;
  if (c.state === 'error') throw new Error(c.error);
  if (c.state !== 'done') throw new Error('the capture was lost');
  await showCapture(c);
}

// A capture the board has finished: its words are fetched and shown, with what is known of it
async function showCapture(meta) {
  S.stoppable = false;
  $('start').disabled = true;
  progress(0.85, 'Reading');
  let buf;
  try {
    buf = await (await fetch('/api/data')).arrayBuffer();
  } catch {
    throw new Error(NO_SERVER);
  }
  progress(0.92, 'Decoding');
  await sleep(20);
  const test = !!meta.test, s = meta.settings || {};
  const w = new Uint32Array(buf, 0, buf.byteLength >> 2);
  const cap = fromRle(w, meta.rate, meta.samples, `Logic Boole 16 · ${test ? 'the FPGA\'s test pattern' : 'probes'}`, MAX_EDGES);
  if (!cap.numSamples) throw new Error('The board sent no samples.');
  const trigText = trigName(meta.trigger || s.trigger);
  const [ckCh, ckEdge] = (meta.clock || '=').split('=');
  const how = `${meta.clock ? `${plural(cap.numSamples, 'sample')}, one at each ${EDGE[ckEdge]} of ${ckCh},` : `${fmtTime(cap.numSamples / meta.rate)} at ${fmtRate(meta.rate)}`} in ${plural(w.length, 'word')} (${(buf.byteLength / 1e6).toFixed(2)} MB), USB at ${meta.usb}`;
  const asked = meta.clock ? plural(meta.asked, 'sample') : fmtTime(meta.asked / meta.rate);
  const piNote = meta.pi ? ` The Pi's lines: ${Object.entries(meta.pi).map(([ch, line]) => `${ch} is ${line}`).join(', ')}.` : '';
  const extra = piNote + glitchNote(meta.glitch_ns) + (meta.clock ? ` The times shown are sample numbers at ${fmtRate(meta.rate)}.` : '');
  const triggered = meta.trigger_sample != null && (!cap.cut || meta.trigger_sample <= cap.numSamples);
  const short = meta.overflow ? ` The capture overflowed: the signals made more data than the board and USB carry, or the computer did not take it fast enough. It ends before the ${asked} asked for, and is whole up to its end.`
    : meta.stopped === 'size' ? ` The capture was ended before the ${asked} asked for: it had grown too large for the browser. A lower sample rate gives a longer one.`
      : meta.stopped === 'stopped' && (meta.trigger_sample != null || !meta.trigger) ? ` It was stopped before the ${asked} asked for.` : '';
  const cut = cap.cut ? ` It changes too often for the page, which holds ${num(MAX_EDGES)} changes: it is shown up to ${fmtTime(cap.cut.at / meta.rate)} of ${fmtTime(cap.cut.of / meta.rate)}. A shorter capture, or one of fewer busy inputs, is shown whole.` : '';
  const trigNote = !meta.trigger ? ''
    : meta.trigger_sample != null ? ` Triggered on ${trigText}, ${meta.clock ? plural(meta.trigger_sample, 'sample') : fmtTime(meta.trigger_sample / meta.rate)} into the capture: time zero is there.`
      : ` Not triggered: ${trigText} did not come before the capture was stopped. This is what the board saw last.`;
  const ended = meta.ended_by === 'unwatched' ? ` No page watched the wait for ${UNWATCHED} seconds, so the app ended it.` : '';
  let kind, text;
  if (meta.problems.length) {
    kind = 'fail';
    text = `${meta.problems.join('; ')}. ${how}.`;
  } else if (test) {
    kind = 'pass';
    text = `The FPGA's test pattern, not the probes: every word as expected. ${how}.${extra}${trigNote}${short}${cut}${ended}`;
  } else {
    kind = 'note';
    text = `${how}. Thresholds: pod A ${meta.thresholds[0].toFixed(2)} V, pod B ${meta.thresholds[1].toFixed(2)} V.${extra}${trigNote}${short}${cut}${ended}`;
  }
  // saved as the command line saves it, so that it reads the trigger, thresholds, overflow and problems back
  cap.info = savedInfo(meta.info, cap.cut);
  cap.trigger = triggered ? meta.trigger_sample : 0;
  $('source').value = 'device';
  renderDevice();
  setCapture(cap);
  footer(meta.trigger && meta.trigger_sample == null ? 'not triggered' : '');
  deviceMessage(text, kind);
  pollBoardNow();
}

$('prepare').addEventListener('click', async () => {
  const btn = $('prepare'), log = $('devLog');
  btn.disabled = true;
  btn.textContent = 'Loading…';
  log.hidden = false;
  log.textContent = 'Loading the firmware into the FX3, then the design into the FPGA …';
  try {
    const r = await post('/api/prepare');
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
    if (f.size > OPEN_MAX) throw new Error(`it is ${(f.size / 1e9).toFixed(1)} GB, and the app opens files of up to ${OPEN_MAX / (1 << 30)} GB`);
    const buf = await f.arrayBuffer();
    let cap, file = null;
    if (isCaptureFile(buf)) {
      const { cap: c, head } = decodeCapture(buf, MAX_EDGES);
      cap = c;
      cap.info = normalInfo(cap.info);
      file = { names: head.names, analyzers: head.analyzers };
    } else if (/\.sr$/i.test(f.name)) {
      throw new Error(`it is a sigrok session (.sr): "logicboole convert ${f.name} ${f.name.replace(/\.sr$/i, '.lb16')}" makes a capture of it that the app opens`);
    } else if (/\.bin$/i.test(f.name)) {     // raw samples: 16 bits each, at the rate chosen in the bar
      if (buf.byteLength < 4) throw new Error('it holds no samples');
      cap = fromWords(new Uint16Array(buf, 0, buf.byteLength >> 1), +$('rate').value, f.name, MAX_EDGES);
    } else {
      throw new Error('it is not a capture: the app opens .lb16 files, and .bin files of raw 16-bit samples');
    }
    cap.source = f.name;
    cap.opened = true;
    S.untouched = false;
    if (!file) { S.analyzers = []; S.autoAnalyze = true; }
    setCapture(cap, file);
    footer();
  } catch (e) {
    $('capInfo').textContent = `${f.name} could not be opened: ${e.message || e}`;
    toast(`${f.name} could not be opened: ${e.message || e}.`);
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
for (const b of SPEEDS) $('bauds').append(new Option(num(b), String(b)));
$('trigCh').addEventListener('change', () => controls(true));
$('trigText').addEventListener('input', () => controls(false));
$('trigText').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') { $('trigText').blur(); startCapture(); } });
$('clockCh').addEventListener('change', () => controls(false));
for (const id of ['thA', 'thB']) {
  $(id).addEventListener('input', () => {
    const b = board(), why = $(id).value === '' ? '' : thresholdProblem($(id).value, b && b.threshold_range, id === 'thA' ? 'A' : 'B');
    $(id).classList.toggle('bad', !!why);
  });
}

$('start').addEventListener('click', startCapture);
$('back').addEventListener('click', goBack);
$('bannerBtn').addEventListener('click', () => {
  $('source').value = 'device';
  $('source').dispatchEvent(new Event('change'));
});
$('zin').addEventListener('click', () => { if (S.cap) zoomAt(waveW() / 2, 1 / 2); });
$('zout').addEventListener('click', () => { if (S.cap) zoomAt(waveW() / 2, 2); });
$('zfit').addEventListener('click', () => { if (S.cap) fitView(); });
$('radix').addEventListener('change', () => { S.radix = $('radix').value; rebuildRows(); invalidate(); });
$('source').addEventListener('change', () => {
  S.autoAnalyze = true;                 // another kind of signal: look for buses afresh
  S.untouched = false;
  $('devCheck').hidden = true;
  $('devLog').hidden = true;
  if ($('source').value === 'device' && S.cap && S.cap.simulated) clearScope();     // never shown as the board's
  renderDevice();
  renderBanner();
  pollBoardNow();
});
$('search').addEventListener('input', filterRows);
$('table').addEventListener('scroll', renderTable);
$('tableRows').addEventListener('click', (ev) => {
  const tr = ev.target.closest('.tr');
  if (tr) selectRow(+tr.dataset.i, false);
});
$('detect').addEventListener('click', () => { if (S.cap) { S.autoAnalyze = true; autoDetect(false); } });
document.querySelectorAll('[data-add]').forEach(btn => btn.addEventListener('click', async () => {
  if (!S.cap) return;
  const cap = S.cap, d = await defaults(btn.dataset.add);
  if (S.cap !== cap) return;
  addAnalyzer(btn.dataset.add, d.cfg, false, d.result).open = true;
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
    cfg = await sigrokStart(await sigrokAbout(first));
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
  if (S.cap) {
    S.overview = null;
    if (S.fitted) S.view.spp = Infinity;
    clampView();
  }
  renderTable();
  invalidate();
}).observe($('rows'));
setInterval(pollBoard, 2500);

// The bar and the board's card as they were for a capture started before this page was loaded (the settings
// the server kept of it), so that what is shown is what the board does
function showSettings(s) {
  const pick = (id, v) => { if (v != null && [...$(id).options].some(o => o.value === String(v))) $(id).value = String(v); };
  pick('rate', s.rate);
  pick('dur', s.seconds);
  if (s.test != null) $('pattern').value = s.test === '1' ? '1' : '0';
  if (s.a != null) $('thA').value = s.a;
  if (s.b != null) $('thB').value = s.b;
  $('glitch').value = s.glitch || '';
  $('pi').value = s.pi || '';
  const t = /^([AB][0-7])=([rf])$/i.exec(s.trigger || '');
  if (t) {
    $('trigCh').value = String(CH_IDS.indexOf(t[1].toUpperCase()));
    $('trigEdge').value = t[2].toLowerCase() === 'r' ? '1' : '0';
  } else if (s.trigger) {
    $('trigCh').value = 'text';
    $('trigText').value = s.trigger;
  } else {
    $('trigCh').value = '-1';
  }
  const k = /^([AB][0-7])=([rfe])$/i.exec(s.clock || '');
  $('clockCh').value = k ? String(CH_IDS.indexOf(k[1].toUpperCase())) : '-1';
  if (k) { $('clockEdge').value = k[2].toLowerCase(); if (s.samples) $('clockSamples').value = s.samples; }
  controls(false);
}

// The page opens on what there is: a capture that runs (started before this page was loaded), or one that ended
// while no page was open, or an empty scope with the board as the source, or with no board the simulated
// signals, under a line that says so.  A board that comes later becomes the source.
async function boot() {
  renderLabels();
  renderAnalyzers();
  renderMeasure();
  rebuildRows();
  try { S.backend = await api('/api/status'); } catch { S.backend = null; }
  renderDevice();
  const c = S.backend && S.backend.capture;
  if (c && (c.state === 'running' || (c.state === 'done' && !c.fetched))) {
    $('source').value = 'device';
    renderDevice();
    showSettings(c.settings || {});
    if (c.state === 'running') busy(() => watch(c)); else busy(() => showCapture(c));
    return;
  }
  if (onUsb(board())) {
    $('source').value = 'device';
    renderDevice();
    renderBanner();
    invalidate();
    return;
  }
  $('source').value = 'demo';
  renderDevice();
  await startCapture();
  S.untouched = true;
  S.prev = null;
  renderBack();
  renderBanner();
}
boot();
