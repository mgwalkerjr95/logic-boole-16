// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// Protocol decoders.  Each takes a capture and its settings and returns
//   ann   boxes to draw above a channel: { s, e, ch, type, v, ... } with s and e in samples
//   rows  one line per message for the table: { s, e, kind, ... }
// Text is made from these when it is shown (annTexts, rowText), so the number format can change without decoding
// again.

import { Walker, lowerBound, upperBound } from './capture.js';

// ---- I2C: SDA and SCL ----
// limit: stop after that many edges (auto-detect only looks at the beginning).
export function decodeI2C(cap, cfg, limit = Infinity) {
  const sda = cap.channels[cfg.sda], scl = cap.channels[cfg.scl];
  const a = sda.edges, c = scl.edges;
  let sdaL = sda.initial, sclL = scl.initial, ia = 0, ic = 0;
  const ann = [], rows = [];
  let txn = null, bits = 0, byte = 0, ack = false, byteStart = 0, first = true;
  let errors = 0, stray = 0, bytes = 0, events = 0;

  const emitByte = (e) => {
    bytes++;
    if (first) {
      txn.addr = byte >> 1; txn.rw = byte & 1; txn.addrAck = ack;
      ann.push({ s: byteStart, e, ch: cfg.sda, type: 'addr', v: byte >> 1, rw: byte & 1, ack });
      first = false;
    } else {
      txn.data.push(byte); txn.acks.push(ack);
      ann.push({ s: byteStart, e, ch: cfg.sda, type: 'data', v: byte, ack });
    }
    bits = 0; byte = 0;
  };
  const close = (s, how) => {
    if (bits === 9) emitByte(s);
    if (bits > 1) { txn.err = 'a byte was cut short'; errors++; }
    if (txn.addr === null) { txn.err = 'no address'; errors++; }
    txn.e = s; txn.end = how;
    rows.push(txn);
    txn = null; bits = 0; byte = 0;
  };

  while ((ia < a.length || ic < c.length) && events++ < limit) {
    const sa = ia < a.length ? a[ia] : Infinity, sc = ic < c.length ? c[ic] : Infinity;
    if (sc <= sa) {                         // the clock first when both change in one sample
      ic++; sclL ^= 1;
      if (sclL) {
        if (txn) {
          if (bits === 0) byteStart = sc;
          if (bits < 8) byte = byte << 1 | sdaL; else ack = !sdaL;
          bits++;
        } else if (rows.length) {
          stray++;                          // clock pulses between a stop and the next start
        }
      } else if (txn && bits === 9) {
        emitByte(sc);
      }
    } else {
      ia++; sdaL ^= 1;
      if (!sclL) continue;                  // data changing while the clock is low: nothing special
      if (!sdaL) {                          // falling while the clock is high: start
        const again = !!txn;
        if (txn) close(sa, 'restart');
        ann.push({ s: sa, e: sa, ch: cfg.sda, type: 'start' });
        txn = { kind: 'i2c', s: sa, e: sa, addr: null, rw: 0, addrAck: false, data: [], acks: [], again };
        bits = 0; byte = 0; first = true;
      } else if (txn) {                     // rising while the clock is high: stop
        ann.push({ s: sa, e: sa, ch: cfg.sda, type: 'stop' });
        close(sa, 'stop');
      }
    }
  }
  let cut = 0;
  if (txn) { txn.e = cap.numSamples; txn.end = 'cut'; cut = 1; rows.push(txn); }
  return { ann, rows, stats: { errors, stray, bytes, messages: rows.length - cut } };
}

// ---- SPI: clock, and any of MOSI, MISO, chip select ----
// cfg: clk, mosi, miso, cs (-1 = not connected), cpol, cpha, bits, msb (true = most significant bit first),
// csActive (0 = active low)
export function decodeSPI(cap, cfg) {
  const clk = cap.channels[cfg.clk], E = clk.edges;
  const wOut = cfg.mosi >= 0 ? new Walker(cap.channels[cfg.mosi]) : null;
  const wIn = cfg.miso >= 0 ? new Walker(cap.channels[cfg.miso]) : null;
  const cs = cfg.cs >= 0 ? cap.channels[cfg.cs] : null;
  const nbits = cfg.bits || 8, msb = cfg.msb !== false;
  const rising = (cfg.cpol ^ cfg.cpha) === 0;       // the edge on which data is taken
  const ann = [], rows = [];
  let txn = null, n = 0, vo = 0, vi = 0, wordStart = 0, last = -1, spacing = 0, csI = 0, words = 0, extra = 0;

  const close = () => {
    if (!txn) return;
    if (n) { txn.err = n + ' bits left over'; extra++; }
    txn.e = cs ? (txn.key < cs.edges.length ? cs.edges[txn.key] : cap.numSamples) : last + Math.max(1, spacing >> 1);
    rows.push(txn);
    txn = null;
  };

  for (let k = 0; k < E.length; k++) {
    if (((clk.initial ^ (k + 1) & 1) === 1) !== rising) continue;
    const s = E[k];
    let key;
    if (cs) {
      while (csI < cs.edges.length && cs.edges[csI] <= s) csI++;
      if ((cs.initial ^ csI & 1) !== (cfg.csActive | 0)) continue;     // not selected
      key = csI;
    } else {                                // no chip select: a long pause in the clock starts a new message
      key = txn ? txn.key : 0;
      if (txn && spacing && s - last > 12 * spacing) key++;
    }
    if (!txn || txn.key !== key) {
      close();
      txn = { kind: 'spi', key, s: cs ? (csI ? cs.edges[csI - 1] : 0) : s, e: s, mosi: [], miso: [] };
      n = 0; vo = 0; vi = 0; spacing = 0;
    }
    if (n === 0) wordStart = s;
    else if (!spacing || s - last < spacing) spacing = s - last;
    const shift = msb ? nbits - 1 - n : n;
    if (wOut && wOut.at(s)) vo += 2 ** shift;
    if (wIn && wIn.at(s)) vi += 2 ** shift;
    last = s;
    if (++n === nbits) {
      const half = Math.max(1, Math.round((s - wordStart) / (2 * Math.max(1, nbits - 1))));
      if (wOut) { ann.push({ s: wordStart - half, e: s + half, ch: cfg.mosi, type: 'word', v: vo, bits: nbits }); txn.mosi.push(vo); }
      if (wIn) { ann.push({ s: wordStart - half, e: s + half, ch: cfg.miso, type: 'word', v: vi, bits: nbits }); txn.miso.push(vi); }
      words++;
      n = 0; vo = 0; vi = 0;
    }
  }
  close();
  return { ann, rows, stats: { words, extra, messages: rows.length } };
}

// ---- asynchronous serial (UART): one line ----
// cfg: rx, baud, bits (8), parity ('none', 'even', 'odd'), stop (1 or 2), invert, msb
export function decodeUART(cap, cfg) {
  const ch = cap.channels[cfg.rx], E = ch.edges;
  const idle = cfg.invert ? 0 : 1;
  const nbits = cfg.bits || 8, par = cfg.parity && cfg.parity !== 'none' ? 1 : 0, nstop = cfg.stop || 1;
  const bit = cap.sampleRate / cfg.baud;
  const ann = [], rows = [];
  const stats = { bytes: 0, framing: 0, parity: 0 };
  if (!(bit >= 3)) {
    return { ann, rows, stats, error: 'the sample rate is too low for this speed: fewer than 3 samples per bit' };
  }
  let hint = 0;
  const lvl = (s) => {                      // the positions only increase
    hint = upperBound(E, s, hint);
    return ch.initial ^ hint & 1;
  };
  let i = 0, p = 0, row = null, prevEnd = -1;
  const flush = () => { if (row) { rows.push(row); row = null; } };

  for (;;) {
    i = lowerBound(E, p, i);
    while (i < E.length && (ch.initial ^ (i + 1) & 1) === idle) i++;   // an edge away from idle
    if (i >= E.length) break;
    const s0 = E[i];
    if (s0 + bit * (1.5 + nbits + par) >= cap.numSamples) break;        // the capture ends inside this byte
    hint = i;
    if (lvl(Math.floor(s0 + bit / 2)) === idle) { p = s0 + 1; continue; }   // shorter than half a bit: a glitch
    let v = 0, ones = 0;
    for (let k = 0; k < nbits; k++) {
      const b = lvl(Math.floor(s0 + bit * (1.5 + k))) ^ (cfg.invert ? 1 : 0);
      ones += b;
      if (b) v |= 1 << (cfg.msb ? nbits - 1 - k : k);
    }
    let err = null;
    if (par) {
      const b = lvl(Math.floor(s0 + bit * (1.5 + nbits))) ^ (cfg.invert ? 1 : 0);
      if ((ones + b & 1) !== (cfg.parity === 'odd' ? 1 : 0)) { err = 'parity'; stats.parity++; }
    }
    if (lvl(Math.floor(s0 + bit * (1.5 + nbits + par))) !== idle) { err = 'framing'; stats.framing++; }
    const e = Math.min(cap.numSamples, Math.round(s0 + bit * (1 + nbits + par + nstop)));
    ann.push({ s: s0, e, ch: cfg.rx, type: 'byte', v, bits: nbits, err });
    stats.bytes++;
    if (row && (s0 - prevEnd > 20 * bit || row.bytes.length >= 80)) flush();
    if (!row) row = { kind: 'uart', s: s0, e, bytes: [], errs: 0 };
    row.bytes.push(v); row.e = e;
    if (err) row.errs++;
    if (v === 0x0A) flush();
    prevEnd = e;
    p = Math.floor(s0 + bit * (1 + nbits + par + 0.5));                 // the middle of the stop bit
  }
  flush();
  return { ann, rows, stats };
}

// The speed of a serial line from its pulse widths: every pulse is a whole number of bits long.
const BAUDS = [300, 600, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800, 115200, 230400, 250000,
  460800, 500000, 921600, 1000000, 1500000, 2000000, 3000000];
export function estimateBaud(cap, chIndex) {
  const E = cap.channels[chIndex].edges;
  const n = Math.min(E.length, 20000);
  if (n < 6) return null;
  const w = new Float64Array(n - 1);
  for (let i = 1; i < n; i++) w[i - 1] = E[i] - E[i - 1];
  const sorted = Float64Array.from(w).sort();
  let u = sorted[Math.floor(sorted.length * 0.02)];                     // near the shortest, ignoring a stray glitch
  if (u < 2) return null;
  let fit = 0;
  for (let pass = 0; pass < 2; pass++) {    // fit the bit time to every pulse that is 1 ... 10 bits long
    let sw = 0, sk = 0, good = 0, short = 0;
    for (const x of w) {
      const k = Math.round(x / u);
      if (k < 1) continue;
      if (k <= 10) {
        short++;
        if (Math.abs(x / u - k) < 0.25) { sw += x; sk += k; good++; }
      }
    }
    if (!sk) return null;
    u = sw / sk;
    fit = short ? good / short : 0;
  }
  const raw = cap.sampleRate / u;
  let best = null;
  for (const b of BAUDS) if (!best || Math.abs(Math.log(raw / b)) < Math.abs(Math.log(raw / best))) best = b;
  const standard = Math.abs(raw / best - 1) < 0.045;
  return { baud: standard ? best : Math.round(raw), raw, standard, fit };
}

// ---- text ----
const hex = (v, digits) => v.toString(16).toUpperCase().padStart(digits, '0');
const printable = (v) => v >= 0x20 && v < 0x7F;
const ESC = { 10: '\\n', 13: '\\r', 9: '\\t', 0: '\\0' };

export function fmtVal(v, radix, bits = 8) {
  const digits = Math.ceil(bits / 4);
  if (radix === 'dec') return String(v);
  if (radix === 'bin') return '0b' + v.toString(2).padStart(bits, '0');
  if (radix === 'ascii' && bits <= 8) return printable(v) ? "'" + String.fromCharCode(v) + "'" : ESC[v] ? "'" + ESC[v] + "'" : '0x' + hex(v, digits);
  return '0x' + hex(v, digits);
}

function short(v, radix, bits = 8) {
  if (radix === 'dec') return String(v);
  if (radix === 'ascii' && bits <= 8 && printable(v)) return String.fromCharCode(v);
  return hex(v, Math.ceil(bits / 4));
}

// What a box may say, longest first: the drawing picks the first that fits.
export function annTexts(a, radix) {
  switch (a.type) {
    case 'start': return ['Start', 'S'];
    case 'stop': return ['Stop', 'P'];
    case 'addr': {
      const ad = radix === 'dec' ? String(a.v) : '0x' + hex(a.v, 2);
      return [`${a.rw ? 'Read from' : 'Write to'} ${ad} ${a.ack ? 'ACK' : 'NAK'}`, `${a.rw ? 'Read' : 'Write'} ${ad}`,
        `${a.rw ? 'R' : 'W'} ${ad}`, hex(a.v, 2)];
    }
    case 'data': return [`${fmtVal(a.v, radix)} ${a.ack ? 'ACK' : 'NAK'}`, fmtVal(a.v, radix), short(a.v, radix)];
    case 'byte': {
      const main = fmtVal(a.v, radix, a.bits);
      return a.err ? [`${main} ${a.err} error`, main + ' !', short(a.v, radix, a.bits)] : [main, short(a.v, radix, a.bits)];
    }
    case 'word': return [fmtVal(a.v, radix, a.bits), short(a.v, radix, a.bits)];
    case 'text': return a.texts;            // from one of sigrok's decoders: its own texts, the longest first
  }
  return [''];
}

export function isError(a) {
  return !!a.err || (a.type === 'addr' && !a.ack);
}

function listVals(vals, radix, bits = 8) {
  if (radix === 'ascii' && bits <= 8) return '"' + text(vals) + '"';
  if (radix === 'dec') return vals.join(' ');
  if (radix === 'bin') return vals.map(v => v.toString(2).padStart(bits, '0')).join(' ');
  return vals.map(v => hex(v, Math.ceil(bits / 4))).join(' ');
}

// For the table: in hex, three or more bytes that are all text are given as text first, so that a message in a
// bus reads at a glance.
function showVals(vals, radix, bits = 8) {
  const list = listVals(vals, radix, bits);
  return radix === 'hex' && bits <= 8 && vals.length >= 3 && vals.every(printable) ? `"${text(vals)}"   ${list}` : list;
}

function text(vals) {
  return vals.map(v => printable(v) ? String.fromCharCode(v) : ESC[v] || '\\x' + hex(v, 2)).join('');
}

// One table line: { title, detail, error }
export function rowText(r, radix, bits = 8) {
  if (r.kind === 'i2c') {
    if (r.addr === null) return { title: r.end === 'cut' ? 'Start' : 'Start, no address', detail: '', error: r.end !== 'cut' };
    const ad = radix === 'dec' ? String(r.addr) : '0x' + hex(r.addr, 2);
    const title = `${r.again ? '↻ ' : ''}${r.rw ? 'Read' : 'Write'} ${ad}`;
    if (!r.addrAck) return { title, detail: 'no answer (NAK)', error: true };
    // on a read the master ends with a NAK: that is normal
    const nak = r.acks.findIndex((k, i) => !k && !(r.rw && i === r.acks.length - 1));
    let detail = r.data.length ? showVals(r.data, radix) : 'no data';
    if (nak >= 0) detail += `  (NAK after byte ${nak + 1})`;
    if (r.end === 'cut') detail += '  (the capture ends here)';
    if (r.err) detail += `  (${r.err})`;
    return { title, detail, error: !!r.err || nak >= 0 };
  }
  if (r.kind === 'spi') {
    const parts = [];
    if (r.mosi.length) parts.push('MOSI ' + showVals(r.mosi, radix, bits));
    if (r.miso.length) parts.push('MISO ' + showVals(r.miso, radix, bits));
    const n = Math.max(r.mosi.length, r.miso.length);
    return { title: `${n} ${bits === 8 ? 'byte' : 'word'}${n === 1 ? '' : 's'}`, detail: parts.join('   ') + (r.err ? `  (${r.err})` : ''), error: !!r.err };
  }
  if (r.kind === 'uart') {
    const detail = radix === 'hex' ? `"${text(r.bytes)}"   ${listVals(r.bytes, 'hex')}` : listVals(r.bytes, radix, bits);
    return { title: `${r.bytes.length} byte${r.bytes.length === 1 ? '' : 's'}`, detail: detail + (r.errs ? `  (${r.errs} with errors)` : ''), error: r.errs > 0 };
  }
  return { title: '', detail: '', error: false };
}
