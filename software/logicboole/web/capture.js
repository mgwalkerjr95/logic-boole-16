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
    this.cut = null;                        // { at, of }: the capture was cut at sample at, of that many
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

// What the page holds at most: the changes of all 16 inputs together.  A capture with more is cut where it reaches
// this many, and says so (cut).  Each change is 4 bytes here, and is copied once more for the decoders.
export const MAX_EDGES = 30000000;

// Raw samples to a capture.  words: a Uint16Array, one 16-bit sample each: bits 7-0 pod A, bits 15-8 pod B.
export function fromWords(words, sampleRate, source = '', maxEdges = Infinity) {
  let n = words.length, total = 0, cut = null;
  const bufs = Array.from({ length: NCH }, () => new EdgeBuf());
  let prev = n ? words[0] & 0xFFFF : 0;
  const first = prev;
  for (let i = 1; i < n; i++) {
    const v = words[i] & 0xFFFF;
    if (v !== prev) {
      let d = v ^ prev;
      if (total + bitCount(d) > maxEdges) { cut = { at: i, of: n }; n = i; break; }
      total += bitCount(d);
      while (d) {
        bufs[31 - Math.clz32(d & -d)].push(i);
        d &= d - 1;
      }
      prev = v;
    }
  }
  const channels = bufs.map((b, ch) => ({ initial: first >> ch & 1, edges: b.done() }));
  return Object.assign(new Capture({ sampleRate, numSamples: n, channels, source }), { cut });
}

function bitCount(d) {
  let n = 0;
  for (; d; d &= d - 1) n++;
  return n;
}

// The board's run-length words to a capture of at most numSamples samples.  Bits 15:0 of a word are the 16 inputs,
// bits 31:16 how many more sample periods they stayed like that: a word covers 1 ... 65536 samples.  The changes
// are counted first, so that each channel's list is made at its size, and a capture with more than maxEdges is
// cut at the word that would pass it: its cut is then { at: that sample, of: the samples the words hold }.
export function fromRle(words, sampleRate, numSamples = Infinity, source = '', maxEdges = Infinity) {
  const counts = new Uint32Array(NCH);
  let pos = 0, prev = -1, total = 0, end = 0, cut = null;
  for (; end < words.length && pos < numSamples; end++) {
    const w = words[end], v = w & 0xFFFF;
    if (prev >= 0 && v !== prev) {
      let d = v ^ prev;
      const n = bitCount(d);
      if (total + n > maxEdges) { cut = { at: pos }; break; }
      total += n;
      for (; d; d &= d - 1) counts[31 - Math.clz32(d & -d)]++;
    }
    prev = v;
    pos += (w >>> 16) + 1;
  }
  if (cut) {                                // how long the whole would have been, for the message
    let all = pos;
    for (let i = end; i < words.length && all < numSamples; i++) all += (words[i] >>> 16) + 1;
    cut.of = Math.min(all, numSamples);
  }
  const lists = Array.from(counts, (n) => new Uint32Array(n)), at = new Uint32Array(NCH);
  let first = 0;
  pos = 0; prev = -1;
  for (let i = 0; i < end; i++) {
    const w = words[i], v = w & 0xFFFF;
    if (prev < 0) first = v;
    else if (v !== prev) {
      for (let d = v ^ prev; d; d &= d - 1) {
        const ch = 31 - Math.clz32(d & -d);
        lists[ch][at[ch]++] = pos;
      }
    }
    prev = v;
    pos += (w >>> 16) + 1;
  }
  const channels = lists.map((edges, ch) => ({ initial: first >> ch & 1, edges }));
  const cap = new Capture({ sampleRate, numSamples: cut ? cut.at : Math.min(pos, numSamples), channels, source });
  cap.cut = cut;
  return cap;
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

// A saved capture.  What cannot be shown as one is refused, with the reason (an Error), before anything of it is
// used.  maxEdges: as fromRle; a capture with more changes is cut at the sample where they reach it.
export function decodeCapture(buf, maxEdges = Infinity) {
  if (!isCaptureFile(buf)) throw new Error('it is not a Logic Boole capture (.lb16)');
  const bad = (why) => new Error(`it is not a capture the app can show: ${why}`);
  const len = new DataView(buf).getUint32(8, true);
  if (12 + len > buf.byteLength) throw new Error('the file is cut short: it ends inside its header');
  let head;
  try {
    head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 12, len)));
  } catch {
    throw bad('its header cannot be read');
  }
  if (!head || typeof head !== 'object' || Array.isArray(head)) throw bad('its header is empty');
  const rate = head.sampleRate, n = head.numSamples;
  if (typeof rate !== 'number' || !(rate > 0) || !Number.isFinite(rate)) throw bad('it gives no sample rate');
  if (!Number.isInteger(n) || n < 1 || n > 2 ** 32) throw bad('it holds no samples, or a number of them the app cannot count');
  const list = head.channels;
  if (!Array.isArray(list)) throw bad('it lists no channels');
  if (list.length !== NCH) throw bad(`it has ${list.length} channel${list.length === 1 ? '' : 's'}, and the app shows captures of ${NCH}`);
  let at = 12 + len, total = 0;
  list.forEach((c, ch) => {
    if (!c || (c.initial !== 0 && c.initial !== 1) || !Number.isInteger(c.edges) || c.edges < 0) throw bad(`channel ${CH_IDS[ch]} is not described as a channel is`);
    total += c.edges;
  });
  if (at + 4 * total > buf.byteLength) throw new Error('the file is cut short: it ends before the last of its channels\' changes');
  let channels = list.map((c, ch) => {
    const edges = new Uint32Array(buf.slice(at, at + 4 * c.edges));
    at += 4 * c.edges;
    for (let i = 0; i < edges.length; i++) {
      if (i && edges[i] <= edges[i - 1]) throw bad(`the changes of ${CH_IDS[ch]} are not in order`);
    }
    if (edges.length && edges[edges.length - 1] >= n) throw bad(`${CH_IDS[ch]} changes after the capture's last sample`);
    return { initial: c.initial, edges };
  });
  let numSamples = n, cut = null;
  if (total > maxEdges) {                   // the last sample before which there are no more than maxEdges changes
    const before = (s) => channels.reduce((k, c) => k + lowerBound(c.edges, s), 0);
    let lo = 0, hi = n;
    while (lo < hi) { const m = Math.floor((lo + hi + 1) / 2); if (before(m) <= maxEdges) lo = m; else hi = m - 1; }
    cut = { at: lo, of: n };
    numSamples = lo;
    channels = channels.map(c => ({ initial: c.initial, edges: c.edges.slice(0, lowerBound(c.edges, lo)) }));
  }
  const trigger = Number.isInteger(head.trigger) && head.trigger >= 0 && head.trigger <= n ? head.trigger : 0;
  const gaps = Array.isArray(head.gaps) ? head.gaps.filter(g => g && Number.isFinite(g.start) && Number.isFinite(g.length)) : [];
  const info = head.info && typeof head.info === 'object' && !Array.isArray(head.info) ? head.info : null;
  const cap = new Capture({ sampleRate: rate, numSamples, channels, trigger, gaps, info,
    source: typeof head.source === 'string' ? head.source : '' });
  cap.cut = cut;
  const names = Array.isArray(head.names) ? head.names.map(x => typeof x === 'string' ? x : '') : [];
  return { cap, head: { ...head, names, analyzers: Array.isArray(head.analyzers) ? head.analyzers : [] } };
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
