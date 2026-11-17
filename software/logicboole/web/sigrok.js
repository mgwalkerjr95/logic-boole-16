// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// sigrok's protocol decoders in the app.  They are run by the program that serves these pages (/api/decode),
// through sigrok-cli; this file makes what that program is asked and lays out what it answers.
//
// An analyzer's settings:  { decoder: 'i2c', channels: { scl: 1, sda: 0 }, options: { address_format: 'shifted' },
//                            top: 'eeprom24xx', only: 'address-write,data-write' }
// channels gives each of the decoder's channels an input (0 ... 15, or -1 for not used), top is another decoder
// with its own settings to put on this one's output, and only names the classes to show (none: all but bits).

// An analyzer's settings as they came with a file: what is missing or of the wrong kind is left out.
export function sigrokClean(cfg) {
  const c = cfg && typeof cfg === 'object' ? cfg : {};
  const text = (v) => typeof v === 'string' ? v : '';
  const part = (v) => Object.entries(v && typeof v === 'object' ? v : {});
  const out = { decoder: text(c.decoder), channels: {}, options: {}, top: text(c.top), only: text(c.only) };
  for (const [k, v] of part(c.channels)) out.channels[k] = Number.isInteger(v) && v >= 0 && v < 16 ? v : -1;
  for (const [k, v] of part(c.options)) if (v != null) out.options[k] = String(v);
  return out;
}

// The decoders and their settings as the words the command line takes: "i2c scl=A1 sda=A0 eeprom24xx".
export function sigrokWords(cfg, ids) {
  const words = [cfg.decoder];
  for (const [k, v] of Object.entries(cfg.channels || {})) if (v >= 0) words.push(`${k}=${ids[v]}`);
  for (const [k, v] of Object.entries(cfg.options || {})) if (v != null && String(v).trim() !== '') words.push(`${k}=${String(v).trim()}`);
  const top = (cfg.top || '').trim();
  return words.join(' ') + (top ? ' ' + top : '');
}

// The input above which a class is drawn.  A class named for one of the decoder's channels ("mosi-data",
// "rx-data") goes above that channel's input; the rest go above the first channel that is used.
export function laneOf(cls, channels) {
  let first = -1;
  for (const [k, v] of Object.entries(channels || {})) {
    if (v < 0) continue;
    if (cls === k || cls.startsWith(k + '-')) return v;
    if (first < 0) first = v;
  }
  return first;
}

const BAD = /warn|error/i;

// What the decoders found, for the screen.  found: rows of [start sample, end sample, decoder, class, texts].
//   rows  a line of the table for each: { s, e, title, detail, error }.  The title is the class, with the
//         decoder's name before it when it is not the analyzer's own decoder but one put on top of it.
//   ann   the boxes: { s, e, ch, type: 'text', texts, cls, err }, those above one input in the order of their
//         starts.  Two boxes above one input never overlap: of two that would, the shorter is drawn, so where a
//         decoder reports a whole transfer and its bytes the bytes are drawn, and the transfer is in the table.
export function layout(found, cfg) {
  const order = found.map((_, i) => i).sort((a, b) => found[a][0] - found[b][0] || found[a][1] - found[b][1] || a - b);
  const rows = [], lanes = new Map();
  for (const i of order) {
    const [s, e, decoder, cls, texts] = found[i];
    const err = BAD.test(cls);
    rows.push({ s, e, title: decoder === cfg.decoder ? cls : `${decoder} ${cls}`, detail: texts[0], error: err });
    const ch = laneOf(cls, cfg.channels);
    if (ch < 0) continue;
    let lane = lanes.get(ch);
    if (!lane) lanes.set(ch, lane = []);
    const box = { s, e, ch, type: 'text', texts, cls, err }, last = lane[lane.length - 1];
    if (!last || s >= last.e) lane.push(box);
    else if (e > s && e - s < last.e - last.s) lane[lane.length - 1] = box;     // it began inside the last one
  }
  return { rows, ann: [].concat(...lanes.values()) };
}

// The settings of a new analyzer for a decoder (about: what /api/decoder says of it).  known: inputs to start
// from for some of its channels, { sda: 0, scl: 1 }; its other channels get the next inputs in turn if the
// decoder needs them, and are not used if it does not.
export function sigrokDefaults(about, known = {}) {
  const cfg = { decoder: about.id, channels: {}, options: {}, top: '', only: '' };
  const taken = new Set(Object.values(known).filter(v => v >= 0));
  let next = 0;
  for (const c of about.channels) {
    if (known[c.id] != null) { cfg.channels[c.id] = known[c.id]; continue; }
    if (!c.required) { cfg.channels[c.id] = -1; continue; }
    while (taken.has(next) && next < 15) next++;
    cfg.channels[c.id] = next;
    taken.add(next);
  }
  return cfg;
}
