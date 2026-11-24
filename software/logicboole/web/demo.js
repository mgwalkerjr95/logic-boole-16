// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// Simulated signals for running the app with no board: what a small microcontroller board might show on 16
// probes.  The signals are made in time (seconds) and then sampled at the chosen rate, as the analyzer would
// sample them, so a rate that is too low for a signal shows it wrongly here too.
//
//   A0 SDA, A1 SCL     I2C at 100 kHz: a temperature sensor at 0x48, an EEPROM at 0x50, nothing at 0x3C
//   A2 CS, A3 CLK, A4 MOSI, A5 MISO   SPI at 1 MHz, mode 0: a flash memory's ID and a read
//   A6                 serial at 115200 baud        A7  serial at 9600 baud
//   B0                 50 kHz clock                 B1  10 kHz PWM, its duty sweeping
//   B2 ... B5          a 4-bit counter, one step every 250 us
//   B6                 a push button with contact bounce
//   B7                 a 100 us pulse at the start of every 20 ms frame: something to trigger on

import { Capture, NCH } from './capture.js';

const FRAME = 0.020;

class Line {
  constructor(initial) { this.initial = initial; this.level = initial; this.t = []; }
  set(t, level) {
    if (level !== this.level) { this.t.push(t); this.level = level; }
  }
  // What sampling at `rate` sees: a change shows in the first sample at or after it, and two changes inside
  // one sample period cancel.
  sample(rate, numSamples) {
    const out = [];
    for (const t of this.t) {
      const s = Math.ceil(t * rate - 1e-9);
      if (s >= numSamples) break;
      if (s <= 0) continue;
      if (out.length && out[out.length - 1] === s) out.pop(); else out.push(s);
    }
    let initial = this.initial;
    for (const t of this.t) { if (Math.ceil(t * rate - 1e-9) <= 0) initial ^= 1; else break; }
    return { initial, edges: Uint32Array.from(out) };
  }
}

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ t >>> 15, t | 1);
    t ^= t + Math.imul(t ^ t >>> 7, t | 61);
    return ((t ^ t >>> 14) >>> 0) / 4294967296;
  };
}

// One I2C message, with repeated starts: parts is a list of { addr, read, data, present }.  Returns its end.
function i2c(sda, scl, t, tb, parts) {
  const bit = (b) => {                      // the clock is low at t
    sda.set(t + tb / 4, b);
    scl.set(t + tb / 2, 1);
    scl.set(t + tb, 0);
    t += tb;
  };
  const byte = (v, ack) => {
    for (let i = 7; i >= 0; i--) bit(v >> i & 1);
    bit(ack ? 0 : 1);
  };
  sda.set(t, 0);                            // start
  scl.set(t + tb / 2, 0);
  t += tb / 2;
  parts.forEach((p, n) => {
    if (n) {                                // repeated start
      sda.set(t + tb / 4, 1);
      scl.set(t + tb / 2, 1);
      sda.set(t + tb, 0);
      scl.set(t + tb * 1.5, 0);
      t += tb * 1.5;
    }
    const present = p.present !== false;
    byte(p.addr << 1 | (p.read ? 1 : 0), present);
    if (present) p.data.forEach((v, i) => byte(v, !p.read || i < p.data.length - 1));
  });
  sda.set(t + tb / 4, 0);                   // stop
  scl.set(t + tb / 2, 1);
  sda.set(t + tb, 1);
  return t + tb;
}

// One SPI message in mode 0.  Returns its end.
function spi(cs, clk, mosi, miso, t, freq, out, back) {
  const half = 0.5 / freq, after = 20e-9;
  cs.set(t, 0);
  t += half;
  for (let n = 0; n < out.length; n++) {
    for (let i = 7; i >= 0; i--) {
      mosi.set(t + after, out[n] >> i & 1);
      miso.set(t + after, back[n] >> i & 1);
      clk.set(t + half, 1);
      clk.set(t + 2 * half, 0);
      t += 2 * half;
    }
  }
  cs.set(t + half, 1);
  mosi.set(t + half + after, 0);
  miso.set(t + half + after, 1);
  return t + half;
}

function uart(line, t, baud, bytes) {
  const tb = 1 / baud;
  for (const v of bytes) {
    line.set(t, 0);
    for (let i = 0; i < 8; i++) line.set(t + tb * (1 + i), v >> i & 1);
    line.set(t + tb * 9, 1);
    t += tb * 10;
  }
  return t;
}

const ascii = (s) => Array.from(s, c => c.charCodeAt(0));

export function makeDemo(sampleRate, numSamples) {
  const T = numSamples / sampleRate;
  const L = Array.from({ length: NCH }, (_, i) => new Line(i === 0 || i === 1 || i === 2 || i === 5 || i === 6 || i === 7 ? 1 : 0));
  const [sda, scl, cs, clk, mosi, miso, tx, rx, , , , , , , button, sync] = L;
  const random = rng(16);

  for (let f = 0; f * FRAME < T; f++) {
    const t0 = f * FRAME;
    sync.set(t0 + 0.0001, 1);
    sync.set(t0 + 0.0002, 0);

    // I2C: read the temperature, write to the EEPROM, call a device that is not there
    const temp = 0x190 + f * 3 & 0xFFF;
    i2c(sda, scl, t0 + 0.0006, 10e-6, [{ addr: 0x48, data: [0x00] }, { addr: 0x48, read: true, data: [temp >> 4, temp << 4 & 0xF0] }]);
    i2c(sda, scl, t0 + 0.0020, 10e-6, [{ addr: 0x50, data: [0x00, 0x10 + f & 0xFF, ...ascii('LB16')] }]);
    i2c(sda, scl, t0 + 0.0034, 10e-6, [{ addr: 0x3C, data: [], present: false }]);

    // SPI: the flash memory's ID, then four bytes read from it
    spi(cs, clk, mosi, miso, t0 + 0.0042, 1e6, [0x9F, 0, 0, 0], [0xFF, 0xEF, 0x40, 0x18]);
    spi(cs, clk, mosi, miso, t0 + 0.0046, 1e6, [0x03, 0x00, 0x10, f & 0xFF, 0, 0, 0, 0], [0xFF, 0xFF, 0xFF, 0xFF, 0xDE, 0xAD, 0xBE, 0xEF]);

    // serial: a status line out, a short answer back on the slower line
    uart(tx, t0 + 0.0052, 115200, ascii(`Logic Boole 16: frame ${f}, ${(temp / 16).toFixed(1)} C\r\n`));
    uart(rx, t0 + 0.0100, 9600, ascii('OK\r\n'));

    // the button: pressed 3 ms into the frame, released at 13 ms, bouncing for about 1 ms each time
    for (const [at, to] of [[0.003, 1], [0.013, 0]]) {
      let t = t0 + at, lvl = to;
      button.set(t, lvl);
      for (let i = 0; i < 6; i++) {
        t += 20e-6 + random() * 180e-6;
        lvl ^= 1;
        button.set(t, lvl);
      }
      button.set(t + 60e-6, to);
    }
  }

  // free-running: the clock, the PWM, the counter
  for (let i = 1; i * 10e-6 < T; i++) L[8].set(i * 10e-6, i & 1);
  for (let i = 0; i * 100e-6 < T; i++) {
    const duty = 0.5 + 0.4 * Math.sin(2 * Math.PI * i * 100e-6 / 0.040);
    L[9].set(i * 100e-6, 1);
    L[9].set((i + duty) * 100e-6, 0);
  }
  for (let i = 1; i * 250e-6 < T; i++) for (let b = 0; b < 4; b++) L[10 + b].set(i * 250e-6, i >> b & 1);

  const channels = L.map(l => l.sample(sampleRate, numSamples));
  return new Capture({ sampleRate, numSamples, channels, source: 'Simulated signals' });
}
