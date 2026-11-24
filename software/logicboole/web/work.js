// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// The decoders, in a worker of their own: a busy capture takes seconds to decode, and the page stays alive
// meanwhile.  app.js sends the capture once ({ op: 'capture', id, cap }), then asks for work on it
// ({ op: 'decode', id, capture, type, cfg } or { op: 'detect', id, capture }), one request at a time, and is
// answered { id, result } or { id, error }.

import { Capture } from './capture.js';
import { decodeOne, detectAll } from './analyze.js';

let held = null;                            // { id, cap }: the capture the page sent last

self.onmessage = (ev) => {
  const m = ev.data;
  if (m.op === 'capture') {
    const c = m.cap;
    held = { id: m.id, cap: new Capture({ sampleRate: c.sampleRate, numSamples: c.numSamples, channels: c.channels, trigger: c.trigger }) };
    return;
  }
  let result = null, error = null;
  try {
    if (!held || held.id !== m.capture) throw new Error('the capture to decode did not reach the decoders');
    result = m.op === 'detect' ? detectAll(held.cap) : decodeOne(held.cap, m.type, m.cfg);
  } catch (e) {
    error = String((e && e.message) || e);
  }
  self.postMessage({ id: m.id, result, error });
};
