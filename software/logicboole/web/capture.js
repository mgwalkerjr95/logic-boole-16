// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// A capture: 16 channels held run-length coded.  For each channel: its level at sample 0 and the sample numbers
// at which it changes ("edges", rising and falling alternate).  An edge at sample e means sample e already has the
// new level.  Everything else in the app (drawing, decoding, measuring) works from these lists, so a quiet capture
// costs almost nothing however long it is.

export const NCH = 16;
export const CH_IDS = Array.from({ length: NCH }, (_, i) => (i < 8 ? 'A' : 'B') + (i & 7));

// first index with a[i] >= x
export function lowerBound(a, x, lo = 0, hi = a.length) {
  while (lo < hi) {
    const m = (lo + hi) >>> 1;
    if (a[m] < x) lo = m + 1; else hi = m;
  }
  return lo;
}

// first index with a[i] > x: the number of entries <= x
export function upperBound(a, x, lo = 0, hi = a.length) {
  while (lo < hi) {
    const m = (lo + hi) >>> 1;
    if (a[m] <= x) lo = m + 1; else hi = m;
  }
  return lo;
}

export class EdgeBuf {
  constructor() { this.a = new Uint32Array(1024); this.n = 0; }
  push(x) {
    if (this.n === this.a.length) {
      const b = new Uint32Array(this.a.length * 2);
      b.set(this.a);
      this.a = b;
    }
    this.a[this.n++] = x;
  }
  done() { return this.a.slice(0, this.n); }
}

export class Capture {
  // channels: NCH of { initial: 0 | 1, edges: Uint32Array }.  gaps: stretches that were not recorded, in order,
  // as { start, length } in samples; a channel is drawn at its last level through a gap, but what it did is unknown.
  constructor({ sampleRate, numSamples, channels, trigger = 0, source = '', info = null, gaps = [] }) {
    this.sampleRate = sampleRate;
    this.numSamples = numSamples;
    this.channels = channels;
    this.trigger = trigger;
    this.source = source;
    this.info = info;
    this.gaps = gaps;
  }
  level(ch, s) {
    const c = this.channels[ch];
    return c.initial ^ (upperBound(c.edges, s) & 1);
  }
  get transitions() { return this.channels.reduce((n, c) => n + c.edges.length, 0); }
}

// The level of one channel at sample positions that only ever increase: cheaper than a search per call.
export class Walker {
  constructor(ch) { this.e = ch.edges; this.init = ch.initial; this.i = 0; }
  at(s) {
    const e = this.e;
    let i = this.i;
    while (i < e.length && e[i] <= s) i++;
    this.i = i;
    return this.init ^ (i & 1);
  }
}

// Raw samples to a capture.  words: a Uint16Array, one 16-bit sample each: bits 7-0 pod A, bits 15-8 pod B.
export function fromWords(words, sampleRate, source = '') {
  const n = words.length;
  const bufs = Array.from({ length: NCH }, () => new EdgeBuf());
  let prev = n ? words[0] & 0xFFFF : 0;
  const first = prev;
  for (let i = 1; i < n; i++) {
    const v = words[i] & 0xFFFF;
    if (v !== prev) {
      let d = v ^ prev;
      while (d) {
        bufs[31 - Math.clz32(d & -d)].push(i);
        d &= d - 1;
      }
      prev = v;
    }
  }
  const channels = bufs.map((b, ch) => ({ initial: first >> ch & 1, edges: b.done() }));
  return new Capture({ sampleRate, numSamples: n, channels, source });
}

// The board's run-length words to a capture of at most numSamples samples.  Bits 15:0 of a word are the 16 inputs,
// bits 31:16 how many more sample periods they stayed like that: a word covers 1 ... 65536 samples.
export function fromRle(words, sampleRate, numSamples = Infinity, source = '') {
  const bufs = Array.from({ length: NCH }, () => new EdgeBuf());
  let pos = 0, prev = -1, first = 0;
  for (let i = 0; i < words.length && pos < numSamples; i++) {
    const w = words[i], v = w & 0xFFFF;
    if (prev < 0) first = v;
    else if (v !== prev) {
      let d = v ^ prev;
      while (d) {
        bufs[31 - Math.clz32(d & -d)].push(pos);
        d &= d - 1;
      }
    }
    prev = v;
    pos += (w >>> 16) + 1;
  }
  const channels = bufs.map((b, ch) => ({ initial: first >> ch & 1, edges: b.done() }));
  return new Capture({ sampleRate, numSamples: Math.min(pos, numSamples), channels, source });
}

// ---- files: "LB16CAP1", the header's length, the header as JSON (padded to a multiple of 4), the edge lists ----
const MAGIC = 'LB16CAP1';

export function encodeCapture(cap, extra = {}) {
  const head = {
    sampleRate: cap.sampleRate, numSamples: cap.numSamples, trigger: cap.trigger, source: cap.source,
    info: cap.info, gaps: cap.gaps, channels: cap.channels.map(c => ({ initial: c.initial, edges: c.edges.length })),
    ...extra,
  };
  let json = JSON.stringify(head);
  let bytes = new TextEncoder().encode(json);
  while ((12 + bytes.length) % 4) { json += ' '; bytes = new TextEncoder().encode(json); }
  const pre = new Uint8Array(12);
  pre.set(new TextEncoder().encode(MAGIC));
  new DataView(pre.buffer).setUint32(8, bytes.length, true);
  return [pre, bytes, ...cap.channels.map(c => c.edges)];
}

export function isCaptureFile(buf) {
  return buf.byteLength >= 12 && new TextDecoder().decode(new Uint8Array(buf, 0, 8)) === MAGIC;
}

export function decodeCapture(buf) {
  if (!isCaptureFile(buf)) throw new Error('not a Logic Boole capture file');
  const len = new DataView(buf).getUint32(8, true);
  const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 12, len)));
  let at = 12 + len;
  const channels = head.channels.map(c => {
    const edges = new Uint32Array(buf.slice(at, at + 4 * c.edges));
    at += 4 * c.edges;
    return { initial: c.initial, edges };
  });
  const cap = new Capture({ ...head, channels });
  return { cap, head };
}

// ---- text ----
export function fmtTime(seconds, digits = 4) {
  const a = Math.abs(seconds);
  if (a === 0) return '0 s';
  const [k, unit] = a >= 1 ? [1, 's'] : a >= 1e-3 ? [1e3, 'ms'] : a >= 1e-6 ? [1e6, 'µs'] : [1e9, 'ns'];
  return trimNum(seconds * k, digits) + ' ' + unit;
}

export function fmtFreq(hz, digits = 4) {
  const [k, unit] = hz >= 1e6 ? [1e-6, 'MHz'] : hz >= 1e3 ? [1e-3, 'kHz'] : [1, 'Hz'];
  return trimNum(hz * k, digits) + ' ' + unit;
}

export function fmtRate(hz) {
  return hz >= 1e6 ? trimNum(hz / 1e6, 4) + ' MS/s' : hz >= 1e3 ? trimNum(hz / 1e3, 4) + ' kS/s' : hz + ' S/s';
}

function trimNum(x, digits) {
  return String(Number(x.toPrecision(digits)));
}
