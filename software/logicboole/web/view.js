// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// The parts of the screen that need no screen: the view's arithmetic, the markers, the rows' heights, the checks
// of what is typed, and what a capture says of itself.  app.js uses them; tests/test_web.mjs checks them in Node.

import { CH_IDS } from './capture.js';

// ---- the view: s0 is the sample at the left edge, spp the samples per pixel ----

// Keeps the view on a capture of N samples shown W pixels wide: a little room at each end, and no closer than 64
// pixels a sample.  Returns whether the whole capture is in view.
export function clampView(v, N, W) {
  const widest = N / W * 1.06;
  const fitted = v.spp >= widest;
  v.spp = Math.min(widest, Math.max(Math.min(1 / 64, widest), v.spp));
  const span = W * v.spp, pad = span * 0.03;
  v.s0 = span >= N ? (N - span) / 2 : Math.min(N - span + pad, Math.max(-pad, v.s0));      // all of it in view: centred
  return fitted;
}

// Zooms by factor (below 1: in) about the pixel x, which stays over the same sample.
export function zoomView(v, N, W, x, factor) {
  const s = v.s0 + x * v.spp;
  v.spp *= factor;
  clampView(v, N, W);
  v.s0 = s - x * v.spp;
  return clampView(v, N, W);
}

// Samples s ... e in view: centred, and zoomed if they would be too small or too large to read.
export function focusView(v, N, W, s, e) {
  const span = Math.max(e - s, 1), now = span / v.spp;
  if (now < W * 0.08 || now > W * 0.8) v.spp = span / (W * 0.3);
  clampView(v, N, W);
  v.s0 = (s + e) / 2 - W * v.spp / 2;
  return clampView(v, N, W);
}

// A click on the time scale at sample s: the first sets marker A, the next marker B; a click when both are set
// begins again at A.  With Shift, B moves and A stays, but only once there is an A.
export function markerClick(m, s, shift) {
  if (m.a == null) return { a: s, b: null };
  if (shift || m.b == null) return { a: m.a, b: s };
  return { a: s, b: null };
}

// ---- the rows: one a channel, with a lane above its trace for each analyzer that draws on it ----
export const ROW_MAX = 44, ROW_MIN = 34, LANE_Y = 3;

// vis: the channels shown, in order; lanes: for each, the analyzers that draw above it; room: the height there is.
// The rows share the room down to ROW_MIN each (below that the rows scroll), and grow by a lane for each analyzer
// past the first.  Each: { ch, y, h, base, lanes, laneH, step, high, low } (high and low: the trace's two levels).
export function layoutRows(vis, lanes, room) {
  const extra = lanes.reduce((n, l) => n + Math.max(0, l.length - 1), 0);
  const base = Math.max(ROW_MIN, Math.min(ROW_MAX, Math.floor((room - extra * 17) / Math.max(1, vis.length))));
  const laneH = base >= 40 ? 15 : 13, step = laneH + 2;
  let y = 0;
  return vis.map((ch, i) => {
    const k = Math.max(1, lanes[i].length), h = base + (k - 1) * step;
    const row = { ch, y, h, base, lanes: lanes[i], laneH, step, high: y + LANE_Y + (k - 1) * step + laneH + 4.5, low: y + h - 5.5 };
    y += h;
    return row;
  });
}

// ---- what is typed ----

// The command line's rule for a channel's name (capture.py, parse_names and set_names): 1 to 24 letters, digits
// and _ . + # / -; not another channel's id, nor a word of the trigger's text; not another channel's name.
// Returns why name cannot be channel ch's, or '' if it can.  names: the 16 names as they are ('' for none).
const NAME = /^[A-Za-z0-9_.+#/-]{1,24}$/;
const RESERVED = new Set(['in', 'then', 'for', 'pulse']);

export function nameProblem(name, ch, names) {
  if (name === '') return '';
  if (!NAME.test(name)) return `"${name}": a name is 1 to 24 letters, digits and _ . + # / -, with no space.`;
  const id = CH_IDS.indexOf(name.toUpperCase());
  if ((id >= 0 && id !== ch) || RESERVED.has(name.toLowerCase())) return `${name} cannot be a channel's name: it means something else in a trigger.`;
  const other = names.findIndex((n, k) => k !== ch && n && n.toLowerCase() === name.toLowerCase());
  if (other >= 0) {                         // as set_names says it: the later channel's spelling
    return `${CH_IDS[Math.min(other, ch)]} and ${CH_IDS[Math.max(other, ch)]} would both be named ${other > ch ? names[other] : name}.`;
  }
  return '';
}

// A threshold typed for pod ("A"), against the range the board is made for (the status's threshold_range, or
// nothing if it is not known): why it cannot be used, or ''.
export function thresholdProblem(text, range, pod) {
  const v = Number(text);
  if (String(text).trim() === '' || !Number.isFinite(v)) return `Threshold ${pod} is not a number of volts.`;
  if (!range || (v >= range[0] && v <= range[1])) return '';
  return `Threshold ${pod} is ${v.toFixed(2)} V, and this board takes ${range[0].toFixed(2)} to ${range[1].toFixed(2)} V.  ` +
    'About half the logic\'s voltage is right: 1.65 V for 3.3 V logic, 0.9 V for 1.8 V.';
}

// A serial line's speed as typed: "115200", "1,000,000", "250k", "1.5M", "9600 baud".  null if it is not one.
export function parseBaud(text) {
  const m = /^\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM]?)\s*(?:baud|bd|bps)?\s*$/.exec(String(text));
  if (!m) return null;
  const v = Math.round(parseFloat(m[1].replace(/,/g, '')) * (m[2] ? (m[2].toLowerCase() === 'k' ? 1e3 : 1e6) : 1));
  return v >= 1 && v <= 1e8 ? v : null;
}

// ---- what a capture says of itself ----

const EDGES = { r: 'rising', f: 'falling', e: 'either edge' };

// A trigger's text for messages: "B0=r" as "B0 rising"; anything else as it is.
export function trigName(words) {
  const m = /^\s*([AB][0-7])\s*=\s*([rfe])\s*$/i.exec(words || '');
  return m ? `${m[1].toUpperCase()} ${EDGES[m[2].toLowerCase()]}` : (words || '').trim();
}

// A capture clock's text, "A1=r", as "rising edge of A1".
export function clockName(words) {
  const m = /^\s*([AB][0-7])\s*=\s*([rfe])\s*$/i.exec(words || '');
  return !m ? (words || '') : m[2].toLowerCase() === 'e' ? `edge of ${m[1].toUpperCase()}` : `${EDGES[m[2].toLowerCase()]} edge of ${m[1].toUpperCase()}`;
}

// What a capture file's "info" holds, as the command line writes it (capture.py, Capture._file_info): thresholds,
// overflow, stopped, problems and trigger_sample beside the rest.  The app before 1.2.0 put what the board said
// under info.meta, with these names: that is read too, as capture.py reads it (META_KEYS), and nothing that the
// top of info has already is changed.
const META_KEYS = { thresholds: 'thresholds', overflow: 'overflow', stopped: 'stopped', problems: 'problems',
  trigger_sample: 'trigger_sample', trigger: 'trigger', clock: 'clock', glitch_ns: 'glitch_ns', pi: 'pi', usb: 'usb',
  words: 'words', asked: 'samples_asked' };

export function normalInfo(info) {
  if (!info || typeof info !== 'object') return null;
  const m = info.meta;
  if (!m || typeof m !== 'object') return info;
  const out = { ...info };
  delete out.meta;
  for (const [from, key] of Object.entries(META_KEYS)) if (from in m && !(key in info)) out[key] = m[from];
  return out;
}

// The info of a capture the page took, to be saved with it: as the board's program gave it (the command line's
// layout), and stopped "size" if the page cut it (cut: { at, of }).
export function savedInfo(info, cut) {
  const out = { ...(normalInfo(info) || {}) };
  if (cut && !out.stopped) out.stopped = 'size';
  return out;
}

// What a capture file says that the screen should show: { notes: texts, off: the channels it did not sample }.
export function captureFlags(info) {
  const i = normalInfo(info) || {}, notes = [], off = new Set();
  const untriggered = i.trigger && i.trigger_sample == null && (i.stopped === 'stopped' || i.stopped === 'timeout');
  if (i.overflow) notes.push('It overflowed: it ends before the length asked for, and is whole up to its end.');
  if (untriggered) notes.push(`Not triggered: ${trigName(i.trigger)} did not come, and this is what the board saw last.`);
  else if (i.stopped === 'stopped') notes.push('It was stopped before the length asked for.');
  else if (i.stopped === 'size') notes.push('It was ended early: it had grown too large.');
  else if (i.stopped === 'timeout') notes.push('Its trigger did not come in the time given: this is what the board saw last.');
  if (Array.isArray(i.problems) && i.problems.length) notes.push(`Found wrong when it was taken: ${i.problems.join('; ')}.`);
  if (i.fast && Array.isArray(i.inputs) && i.inputs.length) {
    const taken = new Set(i.inputs.map(x => String(x).toUpperCase()));
    CH_IDS.forEach((id, ch) => { if (!taken.has(id)) off.add(ch); });
    if (off.size) notes.push(`Taken with the fast design: ${i.inputs.join(', ')} only; the other inputs were not sampled.`);
  }
  return { notes, off };
}
