// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// Looks at a capture and says what is on each channel: steady, a clock, PWM, or part of an I2C bus, an SPI bus or
// a serial line.  The buses are found by trying the decoders and keeping only the combinations whose framing is
// right throughout, so a guess that is shown has decoded cleanly.  Only the first LOOK edges of a channel are
// looked at, so a long capture costs no more than a short one.  ../detect.py is the same for the command line:
// a change here is made there too.

import { NCH, upperBound, lowerBound } from './capture.js';
import { decodeI2C, decodeUART, estimateBaud } from './decoders.js';

const LOOK = 200000;        // edges of a channel that are looked at

// One channel: how many edges, how much of the time it is high, and whether it repeats.
export function channelStats(cap, ch) {
  const c = cap.channels[ch], E = c.edges, n = E.length;
  const st = { ch, edges: n, kind: 'steady', level: c.initial, high: c.initial, freq: 0, duty: 0, minWidth: 0 };
  if (!n) return st;
  let hi = 0, lvl = c.initial, prev = 0;
  const m = Math.min(n, LOOK);
  let minW = Infinity;
  for (let i = 0; i < m; i++) {
    if (lvl) hi += E[i] - prev;
    if (i && E[i] - prev < minW) minW = E[i] - prev;
    prev = E[i]; lvl ^= 1;
  }
  const span = m === n ? cap.numSamples : E[m - 1];
  if (m === n && lvl) hi += cap.numSamples - prev;
  st.high = span ? hi / span : c.initial;
  st.minWidth = Number.isFinite(minW) ? minW : 0;
  st.kind = 'data';
  if (m >= 8) {             // a clock or PWM: every period (rising edge to rising edge) is the same
    let pmin = Infinity, pmax = 0, psum = 0, np = 0, wmin = Infinity, wmax = 0, wsum = 0;
    const r0 = c.initial ? 1 : 0;       // the index of the first rising edge
    for (let i = r0; i + 2 < m; i += 2) {
      const p = E[i + 2] - E[i], w = E[i + 1] - E[i];
      if (p < pmin) pmin = p;
      if (p > pmax) pmax = p;
      if (w < wmin) wmin = w;
      if (w > wmax) wmax = w;
      psum += p; wsum += w; np++;
    }
    if (np >= 3) {
      const mean = psum / np;
      // ... and it runs from one end of the capture to the other: a burst of clock pulses is a bus, not a clock
      const throughout = E[0] <= 2 * mean && (m < n || cap.numSamples - E[n - 1] <= 2 * mean);
      if (throughout && pmax - pmin <= 2 + mean * 0.01) {
        st.freq = cap.sampleRate / mean;
        st.duty = wsum / psum;
        st.kind = wmax - wmin <= 2 + mean * 0.01 ? 'clock' : 'pwm';
      }
    }
  }
  return st;
}

function insideLow(cs, s, margin) {
  // is sample s inside a low stretch of cs, give or take margin samples at its ends?
  const E = cs.edges, i = upperBound(E, s);
  if ((cs.initial ^ i & 1) === 0) return true;
  return (i < E.length && E[i] - s <= margin) || (i > 0 && s - E[i - 1] <= margin);
}

// SPI: a chip select whose low stretches hold every clock edge, 8 clock pulses or a multiple of that in each.
function findSPI(cap, stats, free) {
  const found = [];
  const busy = (i) => free.has(i) && stats[i].kind === 'data';
  for (let k = 0; k < NCH; k++) {
    if (!busy(k) || stats[k].edges < 16) continue;
    const clk = cap.channels[k], CE = clk.edges, nc = Math.min(CE.length, LOOK);
    for (let c = 0; c < NCH; c++) {
      if (c === k || !busy(c) || stats[c].edges < 2 || stats[c].edges * 8 > stats[k].edges || stats[c].high < 0.5) continue;
      const cs = cap.channels[c], SE = cs.edges;
      // whole low stretches only: from a falling edge to the next rising one
      let ok = true, whole = 0, bytes = 0, firstFall = -1;
      for (let i = cs.initial ? 0 : 1; i + 1 < SE.length && ok; i += 2) {
        const a = lowerBound(CE, SE[i]), b = lowerBound(CE, SE[i + 1]);
        if (b > nc) break;                  // beyond the part that is looked at
        const edges = b - a;
        if (edges < 16 || edges % 16) ok = false;
        else { whole++; bytes += edges / 16; if (firstFall < 0) firstFall = SE[i]; }
      }
      if (!ok || !whole) continue;
      // and no clock edge while the chip select is high
      const from = cs.initial ? 0 : (SE.length ? SE[0] : Infinity);
      for (let i = lowerBound(CE, from); i < nc && ok; i++) {
        const j = upperBound(SE, CE[i]);
        if ((cs.initial ^ j & 1) === 1) ok = false;
      }
      if (!ok) continue;
      const margin = stats[k].minWidth * 2;
      const data = [];
      for (let d = 0; d < NCH; d++) {
        if (d === k || d === c || !busy(d) || stats[d].edges < 2) continue;
        const DE = cap.channels[d].edges, nd = Math.min(DE.length, LOOK);
        let inside = 0;
        for (let i = 0; i < nd; i++) if (insideLow(cs, DE[i], margin)) inside++;
        if (inside >= nd * 0.95) data.push(d);
      }
      if (!data.length) continue;
      data.sort((x, y) => stats[y].edges - stats[x].edges).splice(2);
      data.sort((x, y) => x - y);
      const cpol = cap.level(k, firstFall);
      // data that changes after the second edge of each clock pulse is taken on the first: CPHA 0
      let lead = 0, trail = 0;
      for (const d of data) {
        const DE = cap.channels[d].edges, nd = Math.min(DE.length, 5000);
        for (let i = 0; i < nd; i++) {
          const j = upperBound(CE, DE[i]) - 1;
          const s = upperBound(SE, DE[i]);
          if (j < 0 || (s > 0 && CE[j] < SE[s - 1])) { trail++; continue; }    // before the first clock pulse
          if ((clk.initial ^ (j + 1) & 1) !== cpol) lead++; else trail++;
        }
      }
      found.push({
        type: 'spi', score: bytes, channels: [k, c, ...data],
        cfg: { clk: k, cs: c, mosi: data[0], miso: data.length > 1 ? data[1] : -1, cpol, cpha: lead > trail ? 1 : 0, bits: 8, msb: true, csActive: 0 },
      });
    }
  }
  return found;
}

// I2C: a pair of lines on which every start is followed by whole bytes of nine clock pulses up to the stop.
function findI2C(cap, stats, free) {
  const found = [];
  const busy = (i) => free.has(i) && stats[i].kind === 'data';
  for (let scl = 0; scl < NCH; scl++) {
    if (!busy(scl) || stats[scl].edges < 18) continue;
    for (let sda = 0; sda < NCH; sda++) {
      if (sda === scl || !busy(sda) || stats[sda].edges < 4) continue;
      const r = decodeI2C(cap, { sda, scl }, LOOK);
      const st = r.stats;
      if (st.messages < 1 || st.errors || st.stray || st.bytes < st.messages) continue;
      // what was decoded is the whole of it when the two lines have no more edges than were looked at
      const whole = cap.channels[sda].edges.length + cap.channels[scl].edges.length <= LOOK && st.cut === undefined;
      found.push({ type: 'i2c', score: st.bytes, channels: [sda, scl], cfg: { sda, scl }, result: r, whole });
    }
  }
  return found;
}

// Serial: a line that rests high, whose pulses are all whole bits of one of the usual speeds, and that decodes
// with no framing error: as 8 data bits with no parity (8N1), else with even parity (8E1), else with odd (8O1).
// Seven data bits with even parity (7E1) decode as 8N1, with the parity bit as the eighth: they are told apart by
// that bit, which then always makes the number of ones even (sevenEven).
const FORMATS = [[8, 'none'], [8, 'even'], [8, 'odd']];

function findUART(cap, stats, free) {
  const found = [];
  for (let rx = 0; rx < NCH; rx++) {
    if (!free.has(rx) || stats[rx].kind !== 'data' || stats[rx].edges < 8 || stats[rx].high < 0.5) continue;
    const est = estimateBaud(cap, rx);
    if (!est || !est.standard || est.fit < 0.97) continue;
    for (const [bits, parity] of FORMATS) {
      let cfg = { rx, baud: est.baud, bits, parity, stop: 1, invert: false, msb: false };
      let r = decodeUART(cap, cfg, LOOK);
      const st = r.stats;
      if (st.bytes < 2 || st.framing > st.bytes * 0.02 || st.parity > st.bytes * 0.02) continue;
      if (parity === 'none' && sevenEven(r.ann)) {
        cfg = { ...cfg, bits: 7, parity: 'even' };
        r = decodeUART(cap, cfg, LOOK);
      }
      const whole = cap.channels[rx].edges.length <= LOOK && r.stats.cut === undefined;
      found.push({ type: 'uart', score: r.stats.bytes, channels: [rx], cfg, result: r, whole });
      break;
    }
  }
  return found;
}

// Bytes decoded as 8N1 that are 7 data bits and an even parity bit: at least 16 of them without a framing error,
// each with an even number of ones, and the top bit (the parity bit) set in some.
function sevenEven(ann) {
  let n = 0, high = 0;
  for (const a of ann) {
    if (a.err) continue;
    let ones = 0;
    for (let v = a.v; v; v &= v - 1) ones++;
    if (ones & 1) return false;
    n++;
    if (a.v & 0x80) high++;
  }
  return n >= 16 && high > 0;
}

// The names auto-detect gives the channels of a bus.  A name that an earlier bus of the same result has taken gets
// a number: SDA2, UART2.
const NAMES = { i2c: [['sda', 'SDA'], ['scl', 'SCL']], spi: [['clk', 'SPI_CLK'], ['cs', 'SPI_CS'], ['mosi', 'SPI_MOSI'], ['miso', 'SPI_MISO']],
  uart: [['rx', 'UART']] };

function giveNames(analyzers) {
  const given = new Set();
  for (const f of analyzers) {
    f.names = {};
    for (const [role, want] of NAMES[f.type]) {
      const ch = f.cfg[role];
      if (!(ch >= 0)) continue;
      let name = want;
      for (let k = 2; given.has(name.toLowerCase()); k++) name = want + k;
      given.add(name.toLowerCase());
      f.names[ch] = name;
    }
  }
}

// Everything found, best first, with no channel used twice.  Each: { type, score, channels, cfg, names }, and for
// I2C and serial the decoders' result on the part looked at (result), which is the whole when whole is true.
export function detect(cap) {
  const stats = Array.from({ length: NCH }, (_, ch) => channelStats(cap, ch));
  const free = new Set(Array.from({ length: NCH }, (_, i) => i));
  const analyzers = [];
  for (const find of [findSPI, findI2C, findUART]) {
    const list = find(cap, stats, free).sort((a, b) => b.score - a.score);
    for (const f of list) {
      if (!f.channels.every(c => free.has(c))) continue;
      f.channels.forEach(c => free.delete(c));
      analyzers.push(f);
    }
  }
  giveNames(analyzers);
  return { analyzers, stats };
}
