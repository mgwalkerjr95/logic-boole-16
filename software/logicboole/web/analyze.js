// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// The work the decoders do, as the page asks for it: one analyzer, or auto-detect with what it finds decoded.
// It runs in a worker (work.js) so that the page stays alive while it decodes, or in the page itself where a
// browser has no module workers.

import { decodeI2C, decodeSPI, decodeUART } from './decoders.js';
import { detect } from './detect.js';

// One of the app's own analyzers on a capture: { ann, rows, stats }, or { error }.
export function decodeOne(cap, type, c) {
  if (type === 'i2c' && c.sda === c.scl) return { error: 'SDA and SCL are the same channel' };
  if (type === 'spi' && [c.mosi, c.miso, c.cs].includes(c.clk)) return { error: 'the clock is also set as another line' };
  if (type === 'spi' && c.mosi < 0 && c.miso < 0) return { error: 'set MOSI or MISO' };
  if (type === 'i2c') return decodeI2C(cap, c);
  if (type === 'spi') return decodeSPI(cap, c);
  if (type === 'uart') return decodeUART(cap, c);
  return { error: `the app has no analyzer called ${type}` };
}

// Auto-detect, and each bus it finds decoded: { analyzers: [{ type, score, channels, cfg, names, result }], stats }.
// What auto-detect decoded already is used when it was the whole of the capture, so nothing is decoded twice.
export function detectAll(cap) {
  const d = detect(cap);
  const analyzers = d.analyzers.map(({ result, whole, ...f }) => ({ ...f, result: whole && result ? result : decodeOne(cap, f.type, f.cfg) }));
  return { analyzers, stats: d.stats };
}
