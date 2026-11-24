// Copyright (C) 2026 Michael Walker
// SPDX-License-Identifier: GPL-3.0-or-later

// Checks of the app's pages with no browser and no board:  node software/tests/test_web.mjs
// The simulated signals are decoded and compared with what was put into them, at several sample rates.

import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Capture, NCH, CH_IDS, Walker, fromWords, fromRle, encodeCapture, decodeCapture } from '../logicboole/web/capture.js';
import { decodeI2C, decodeSPI, decodeUART, estimateBaud, rowText, annTexts } from '../logicboole/web/decoders.js';
import { detect } from '../logicboole/web/detect.js';
import { makeDemo } from '../logicboole/web/demo.js';
import { sigrokWords, sigrokDefaults, sigrokClean, laneOf, layout } from '../logicboole/web/sigrok.js';
import { clampView, zoomView, focusView, markerClick, layoutRows, ROW_MIN, ROW_MAX, nameProblem, thresholdProblem,
  parseBaud, trigName, normalInfo, savedInfo, captureFlags } from '../logicboole/web/view.js';
import { decodeOne, detectAll } from '../logicboole/web/analyze.js';

let passed = 0;
function check(name, fn) {
  try { fn(); passed++; console.log('  ok    ' + name); } catch (e) { console.log('  FAIL  ' + name + '\n        ' + String(e.message).split('\n').join('\n        ')); process.exitCode = 1; }
}
const str = (bytes) => String.fromCharCode(...bytes);

for (const rate of [200e6, 100e6, 25e6, 10e6]) {
  const cap = makeDemo(rate, Math.round(rate * 0.1));         // 100 ms: five frames
  console.log(`simulated signals at ${rate / 1e6} MS/s: ${cap.transitions} transitions`);

  check('I2C: the four messages of every frame', () => {
    const r = decodeI2C(cap, { sda: 0, scl: 1 });
    assert.equal(r.stats.errors, 0);
    assert.equal(r.rows.length, 20);
    for (let f = 0; f < 5; f++) {
      const [a, b, c, d] = r.rows.slice(f * 4);
      const temp = 0x190 + f * 3;
      assert.deepEqual([a.addr, a.rw, a.addrAck, a.data, a.end], [0x48, 0, true, [0], 'restart']);
      assert.deepEqual([b.addr, b.rw, b.addrAck, b.data, b.acks, b.again], [0x48, 1, true, [temp >> 4, temp << 4 & 0xF0], [true, false], true]);
      assert.deepEqual([c.addr, c.rw, c.data], [0x50, 0, [0, 0x10 + f, 0x4C, 0x42, 0x31, 0x36]]);
      assert.deepEqual([d.addr, d.addrAck, d.data], [0x3C, false, []]);
    }
    assert.equal(rowText(r.rows[1], 'hex').title, '↻ Read 0x48');
    assert.equal(rowText(r.rows[1], 'hex').error, false);       // the NAK that ends a read is normal
    assert.equal(rowText(r.rows[3], 'hex').error, true);
    assert.equal(rowText(r.rows[2], 'ascii').detail, '"\\0\\x10LB16"');
  });

  check('SPI: the ID and the read', () => {
    const r = decodeSPI(cap, { clk: 3, mosi: 4, miso: 5, cs: 2, cpol: 0, cpha: 0, bits: 8, msb: true, csActive: 0 });
    assert.equal(r.rows.length, 10);
    assert.equal(r.stats.extra, 0);
    assert.deepEqual(r.rows[0].mosi, [0x9F, 0, 0, 0]);
    assert.deepEqual(r.rows[0].miso, [0xFF, 0xEF, 0x40, 0x18]);
    assert.deepEqual(r.rows[3].mosi, [0x03, 0x00, 0x10, 1, 0, 0, 0, 0]);
    assert.deepEqual(r.rows[3].miso, [0xFF, 0xFF, 0xFF, 0xFF, 0xDE, 0xAD, 0xBE, 0xEF]);
    assert.ok(r.rows[0].s < r.rows[0].e && r.rows[0].e < r.rows[1].s);
  });

  check('SPI without the chip select: the same bytes, split at the pauses', () => {
    const r = decodeSPI(cap, { clk: 3, mosi: 4, miso: 5, cs: -1, cpol: 0, cpha: 0, bits: 8, msb: true });
    assert.equal(r.rows.length, 10);
    assert.deepEqual(r.rows[1].miso, [0xFF, 0xFF, 0xFF, 0xFF, 0xDE, 0xAD, 0xBE, 0xEF]);
  });

  check('serial: both lines, and their speeds from the pulse widths', () => {
    const a = estimateBaud(cap, 6), b = estimateBaud(cap, 7);
    assert.equal(a.baud, 115200);
    assert.equal(b.baud, 9600);
    const tx = decodeUART(cap, { rx: 6, baud: 115200, bits: 8, parity: 'none', stop: 1 });
    assert.equal(tx.stats.framing, 0);
    assert.equal(tx.rows.length, 5);
    assert.equal(str(tx.rows[2].bytes), 'Logic Boole 16: frame 2, 25.4 C\r\n');
    const rx = decodeUART(cap, { rx: 7, baud: 9600, bits: 8, parity: 'none', stop: 1 });
    assert.deepEqual(rx.rows.map(r => str(r.bytes)), Array(5).fill('OK\r\n'));
    assert.equal(annTexts(tx.ann[0], 'ascii')[0], "'L'");
    assert.equal(annTexts(tx.ann[0], 'hex')[0], '0x4C');
  });

  check('serial at the wrong speed gives framing errors, not silence', () => {
    const r = decodeUART(cap, { rx: 6, baud: 57600, bits: 8, parity: 'none', stop: 1 });
    assert.ok(r.stats.framing > 0);
  });

  check('auto-detect: every bus, on the right channels, and nothing else', () => {
    const d = detect(cap);
    const by = (t) => d.analyzers.filter(a => a.type === t);
    assert.equal(d.analyzers.length, 4, JSON.stringify(d.analyzers.map(a => [a.type, a.channels])));
    assert.deepEqual(by('i2c')[0].cfg, { sda: 0, scl: 1 });
    assert.deepEqual(by('spi')[0].cfg, { clk: 3, cs: 2, mosi: 4, miso: 5, cpol: 0, cpha: 0, bits: 8, msb: true, csActive: 0 });
    assert.deepEqual(by('uart').map(a => [a.cfg.rx, a.cfg.baud]).sort(), [[6, 115200], [7, 9600]]);
    const s = d.stats;
    assert.equal(s[8].kind, 'clock');
    assert.ok(Math.abs(s[8].freq - 50e3) < 50 && Math.abs(s[8].duty - 0.5) < 0.01, `${s[8].freq} ${s[8].duty}`);
    assert.equal(s[9].kind, 'pwm');
    assert.ok(Math.abs(s[9].freq - 10e3) < 10);
    assert.deepEqual([10, 11, 12, 13].map(c => Math.round(s[c].freq)), [2000, 1000, 500, 250]);
    assert.equal(s[14].kind, 'data');
  });
}

console.log('raw samples, test data, files');
const small = makeDemo(1e6, 200000);

check('raw 16-bit samples give the same capture back', () => {
  const raw = new Uint16Array(small.numSamples);
  for (let ch = 0; ch < NCH; ch++) {
    const c = small.channels[ch];
    let lvl = c.initial, at = 0;
    for (const e of [...c.edges, small.numSamples]) {
      if (lvl) for (let i = at; i < e; i++) raw[i] |= 1 << ch;
      at = e; lvl ^= 1;
    }
  }
  const back = fromWords(raw, 1e6);
  for (let ch = 0; ch < NCH; ch++) {
    assert.equal(back.channels[ch].initial, small.channels[ch].initial);
    assert.deepEqual(back.channels[ch].edges, small.channels[ch].edges);
  }
  assert.equal(back.level(8, 25), small.level(8, 25));
});

check('raw samples that count up: bit 0 changes at every sample', () => {
  const cap = fromWords(Uint16Array.from({ length: 70000 }, (_, i) => i), 100e6);
  assert.equal(cap.channels[0].edges.length, 69999);
  assert.equal(cap.channels[15].edges.length, 2);
});

{
  const parts = encodeCapture(small, { names: ['x'] });
  const total = parts.reduce((n, p) => n + p.byteLength, 0);
  const buf = new Uint8Array(total);
  let at = 0;
  for (const p of parts) { buf.set(new Uint8Array(p.buffer, p.byteOffset, p.byteLength), at); at += p.byteLength; }
  const { cap, head } = decodeCapture(buf.buffer);
  check('a saved capture opens as it was: rate, length, names and every edge', () => {
    assert.ok(cap instanceof Capture);
    assert.deepEqual([cap.sampleRate, cap.numSamples, head.names], [1e6, 200000, ['x']]);
    for (let ch = 0; ch < NCH; ch++) assert.deepEqual(cap.channels[ch].edges, small.channels[ch].edges);
  });
}

// ---- the board's run-length words ----
// a capture as the FPGA sends it: a word where any channel changes, and one every 65536 samples while none does
function toRle(cap, to = cap.numSamples) {
  const walkers = cap.channels.map(c => new Walker(c)), out = [];
  const stateAt = (s) => walkers.reduce((v, wk, ch) => v | wk.at(s) << ch, 0);
  const changes = [...new Set(cap.channels.flatMap(c => Array.from(c.edges)))].filter(e => e > 0 && e < to).sort((a, b) => a - b);
  let at = 0;
  for (const e of [...changes, to]) {
    const v = stateAt(at);
    for (let n = e - at; n > 0; n -= 65536) out.push(((Math.min(n, 65536) - 1) << 16 | v) >>> 0);
    at = e;
  }
  return out;
}

{
  const cap = makeDemo(100e6, 10e6);                            // 100 ms: five frames
  const words = new Uint32Array(toRle(cap));
  check('run-length words give the same capture back, in far fewer words than samples', () => {
    const back = fromRle(words, 100e6);
    assert.equal(back.numSamples, cap.numSamples);
    assert.ok(words.length < cap.numSamples / 50);
    for (let ch = 0; ch < NCH; ch++) {
      assert.equal(back.channels[ch].initial, cap.channels[ch].initial);
      assert.deepEqual(back.channels[ch].edges, cap.channels[ch].edges);
    }
  });
  check('a capture is cut to the length asked', () => {
    const back = fromRle(words, 100e6, 2500000);
    assert.equal(back.numSamples, 2500000);
    for (let ch = 0; ch < NCH; ch++) assert.deepEqual(back.channels[ch].edges, cap.channels[ch].edges.filter(e => e < 2500000));
  });
  check('a word that covers 65536 samples, with all 16 inputs high', () => {
    const back = fromRle(new Uint32Array([0xFFFFFFFF, 0x0000FFFE, 0x00010000]), 1e6);
    assert.equal(back.numSamples, 65536 + 1 + 2);
    assert.deepEqual([back.channels[0].initial, Array.from(back.channels[0].edges)], [1, [65536]]);
    assert.deepEqual([back.channels[15].initial, Array.from(back.channels[15].edges)], [1, [65537]]);
  });
}

// ---- the stand-in for the board (logicboole --fake): an I2C bus on A0 and A1, as the board would send it ----
{
  const dir = fileURLToPath(new URL('..', import.meta.url));
  const data = execFileSync('python3', ['-c', `
import sys
sys.path.insert(0, sys.argv[1])
from logicboole import board, fake
with board.connect(backend=fake.FakeBus(instant=True)) as b:
    cap = b.capture(rate=1e6, seconds=0.08)
assert cap.ok and cap.samples == 80000 and cap.format == "rle", (cap.problems, cap.samples)
sys.stdout.buffer.write(cap.words)`, dir], { maxBuffer: 1 << 26 });
  const words = new Uint32Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
  check('the stand-in board: "Logic Boole 16" at address 0x16, found by auto-detect on A0 and A1', () => {
    const cap = fromRle(words, 1e6, 80000);
    assert.equal(cap.numSamples, 80000);                        // 80 ms: four groups of messages
    const found = detect(cap).analyzers;
    assert.deepEqual(found.map(f => [f.type, f.cfg.sda, f.cfg.scl]), [['i2c', 0, 1]]);
    const res = decodeI2C(cap, found[0].cfg);
    const texts = res.rows.map(r => rowText(r, 'ascii'));
    assert.equal(res.rows.length, 20);                          // five rows a group: the read after the repeated start is its own
    assert.equal(texts.filter(t => t.title.includes('0x16') && t.detail.includes('Logic Boole 16')).length, 4, JSON.stringify(texts.slice(0, 4)));
    assert.equal(texts.filter(t => t.error).length, 4);         // nobody answers at 0x3C
  });
}

// ---- .lb16 files: what the command line writes the app opens, and the other way round ----
{
  const dir = fileURLToPath(new URL('..', import.meta.url));
  const tmp = mkdtempSync(join(tmpdir(), 'lb16-'));
  const py = (code, ...args) => execFileSync('python3', ['-B', '-c', `import sys\nsys.path.insert(0, sys.argv[1])\n${code}`, dir, ...args], { maxBuffer: 1 << 26 });
  check('a capture saved by the command line opens in the app', () => {
    const out = JSON.parse(py(`
import json
from logicboole import board, fake
with board.connect(backend=fake.FakeBus(instant=True)) as b:
    cap = b.capture(rate=2e6, seconds=0.05, a=1.65, b=0.9)
cap.save(sys.argv[2])
print(json.dumps([[initial, list(e)] for initial, e in cap.edges()]))`, join(tmp, 'py.lb16')));
    const file = readFileSync(join(tmp, 'py.lb16'));
    const { cap, head } = decodeCapture(file.buffer.slice(file.byteOffset, file.byteOffset + file.byteLength));
    assert.deepEqual([cap.sampleRate, cap.numSamples, head.info.thresholds], [2e6, 100000, [1.65, 0.9]]);
    for (let ch = 0; ch < NCH; ch++) assert.deepEqual([cap.channels[ch].initial, Array.from(cap.channels[ch].edges)], out[ch]);
    assert.equal(decodeI2C(cap, { sda: 0, scl: 1 }).rows.length, 15);       // 50 ms: three groups of five rows
  });
  check('a capture saved by the app opens on the command line', () => {
    const demo = makeDemo(25e6, 2500000);
    const parts = encodeCapture(demo, { names: ['SDA'], analyzers: [] });
    writeFileSync(join(tmp, 'js.lb16'), Buffer.concat(parts.map(p => Buffer.from(p.buffer, p.byteOffset, p.byteLength))));
    const back = JSON.parse(py(`
import json
from logicboole.capture import Capture
cap = Capture.load(sys.argv[2])
print(json.dumps({"rate": cap.rate, "samples": cap.samples, "names": cap.names[:2], "edges": [[initial, list(e)] for initial, e in cap.edges()]}))`, join(tmp, 'js.lb16')));
    assert.deepEqual([back.rate, back.samples, back.names], [25e6, 2500000, ['SDA', 'A1']]);
    for (let ch = 0; ch < NCH; ch++) assert.deepEqual(back.edges[ch], [demo.channels[ch].initial, Array.from(demo.channels[ch].edges)]);
  });
  check('the command line\'s detect finds what the app\'s auto-detect finds, at four rates', () => {
    for (const rate of [200e6, 100e6, 25e6, 10e6]) {
      const demo = makeDemo(rate, Math.round(rate * 0.1));
      const parts = encodeCapture(demo, { names: [], analyzers: [] });
      const file = join(tmp, `detect_${rate}.lb16`);
      writeFileSync(file, Buffer.concat(parts.map(p => Buffer.from(p.buffer, p.byteOffset, p.byteLength))));
      const py_ = JSON.parse(py(`
import json
from logicboole import detect
from logicboole.capture import Capture
buses, stats, _ = detect.analyze(Capture.load(sys.argv[2]))
print(json.dumps({"buses": buses, "stats": stats}))`, file));
      const js = detect(demo);
      // the app's settings that never vary (8 bits, no parity ...) are not in the command line's answer
      const bus = (b, keys) => [b.type, Object.fromEntries(keys.map(k => [k, b.cfg[k]])), b.score, Array.from(b.channels)];
      assert.deepEqual(py_.buses.map(b => bus(b, Object.keys(b.cfg))),
                       js.analyzers.map((b, i) => bus(b, Object.keys((py_.buses[i] || b).cfg))), `${rate}`);
      const near = (a, b) => Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b));
      for (let ch = 0; ch < NCH; ch++) {
        const p = py_.stats[ch], j = js.stats[ch];
        assert.deepEqual([p.kind, p.edges, p.level, p.min_width], [j.kind, j.edges, j.level, j.minWidth], `${rate} ${ch}`);
        for (const k of ['high', 'freq', 'duty']) assert.ok(near(p[k], j[k]), `${rate} ${ch} ${k}: ${p[k]} ${j[k]}`);
      }
    }
  });
  rmSync(tmp, { recursive: true });
}

console.log("sigrok's decoders in the app");
{
  const ids = ['A0', 'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'B0', 'B1', 'B2', 'B3', 'B4', 'B5', 'B6', 'B7'];
  const spi = {
    id: 'spi',
    channels: [{ id: 'clk', required: true }, { id: 'miso', required: false }, { id: 'mosi', required: false }, { id: 'cs', required: false }],
    options: [{ id: 'cpol', default: '0', values: ['0', '1'] }, { id: 'wordsize', default: '8', values: null }],
  };

  check('the settings a decoder starts with, and the words they make', () => {
    const cfg = sigrokDefaults(spi, { clk: 3, mosi: 4 });
    assert.deepEqual(cfg, { decoder: 'spi', channels: { clk: 3, miso: -1, mosi: 4, cs: -1 }, options: {}, top: '', only: '' });
    assert.equal(sigrokWords(cfg, ids), 'spi clk=A3 mosi=A4');
    cfg.channels.cs = 8;
    cfg.options = { cpol: '1', wordsize: ' 16 ', cpha: '' };
    cfg.top = '  spiflash format=ascii ';
    assert.equal(sigrokWords(cfg, ids), 'spi clk=A3 mosi=A4 cs=B0 cpol=1 wordsize=16 spiflash format=ascii');
    // channels it needs and was given no input for get the next ones that are free
    const can = { id: 'x', channels: [{ id: 'a', required: true }, { id: 'b', required: true }, { id: 'c', required: true }], options: [] };
    assert.deepEqual(sigrokDefaults(can, { b: 0 }).channels, { a: 1, b: 0, c: 2 });
    assert.deepEqual(sigrokDefaults(can).channels, { a: 0, b: 1, c: 2 });
  });

  check('settings that came with a file are made whole', () => {
    assert.deepEqual(sigrokClean(null), { decoder: '', channels: {}, options: {}, top: '', only: '' });
    assert.deepEqual(sigrokClean({ decoder: 'i2c', channels: { scl: 1, sda: 99, x: 'A0' }, options: { a: 5, b: null }, top: 7, only: 'ack' }),
      { decoder: 'i2c', channels: { scl: 1, sda: -1, x: -1 }, options: { a: '5' }, top: '', only: 'ack' });
    const cfg = sigrokDefaults(spi, { clk: 3 }), copy = sigrokClean(cfg);
    assert.deepEqual(copy, cfg);
    assert.notEqual(copy.channels, cfg.channels);
  });

  check('where what was found is drawn, and what the table lists', () => {
    const cfg = { decoder: 'spi', channels: { clk: 3, miso: 5, mosi: 4, cs: 2 }, options: {}, top: '', only: '' };
    assert.deepEqual([laneOf('mosi-data', cfg.channels), laneOf('miso-transfer', cfg.channels), laneOf('cs-change', cfg.channels), laneOf('other', cfg.channels)], [4, 5, 2, 3]);
    assert.equal(laneOf('rx-data', { rx: -1, tx: 6 }), 6);
    assert.equal(laneOf('anything', { rx: -1 }), -1);
    const found = [                          // as sigrok-cli gives them: not in the order of time
      [20, 30, 'spi', 'mosi-data', ['00']], [5, 15, 'spi', 'mosi-data', ['9F']], [0, 100, 'spi', 'mosi-transfer', ['9F 00']],
      [5, 15, 'spi', 'miso-data', ['FF']], [20, 30, 'spi', 'miso-data', ['EF']], [0, 100, 'spi', 'miso-transfer', ['FF EF']],
      [25, 25, 'spi', 'warning', ['odd']], [40, 90, 'spiflash', 'cmd', ['Read ID', 'ID']],
    ];
    const r = layout(found, cfg);
    assert.deepEqual(r.rows.map(x => [x.s, x.e, x.title, x.detail, x.error]), [
      [0, 100, 'mosi-transfer', '9F 00', false], [0, 100, 'miso-transfer', 'FF EF', false], [5, 15, 'mosi-data', '9F', false],
      [5, 15, 'miso-data', 'FF', false], [20, 30, 'mosi-data', '00', false], [20, 30, 'miso-data', 'EF', false],
      [25, 25, 'warning', 'odd', true], [40, 90, 'spiflash cmd', 'Read ID', false]]);
    // a transfer and its bytes above one input: the bytes are drawn
    const lane = (ch) => r.ann.filter(a => a.ch === ch).map(a => [a.s, a.e, a.texts[0]]);
    assert.deepEqual(lane(4), [[5, 15, '9F'], [20, 30, '00']]);
    assert.deepEqual(lane(5), [[5, 15, 'FF'], [20, 30, 'EF']]);
    assert.deepEqual(lane(3), [[25, 25, 'odd'], [40, 90, 'Read ID']]);
    for (const ch of [3, 4, 5]) {
      const boxes = r.ann.filter(a => a.ch === ch);
      for (let i = 1; i < boxes.length; i++) assert.ok(boxes[i].s >= boxes[i - 1].e);
    }
    assert.deepEqual(r.ann.find(a => a.cls === 'warning').err, true);
    assert.deepEqual(annTexts(r.ann.find(a => a.cls === 'cmd'), 'hex'), ['Read ID', 'ID']);
    assert.deepEqual(layout([], cfg), { rows: [], ann: [] });
  });
}

console.log('the screen: the view, markers, rows, what is typed');
check('the view stays on the capture: a little room at each end, no closer than 64 pixels a sample', () => {
  const v = { s0: -5000, spp: 1e9 };
  assert.equal(clampView(v, 100000, 1000), true);               // the whole of it: centred
  assert.ok(Math.abs(v.spp - 106) < 1e-9 && Math.abs(v.s0 - (100000 - 106000) / 2) < 1e-6, String(v.s0));
  v.spp = 104;                                                  // nearly the whole: centred too
  clampView(v, 100000, 1000);
  assert.ok(Math.abs(v.s0 + 2000) < 1e-6, String(v.s0));
  v.spp = 1e-6;
  assert.equal(clampView(v, 100000, 1000), false);
  assert.equal(v.spp, 1 / 64);
  v.s0 = 1e9;                                                   // past the end: back to it, with 3 % of room
  clampView(v, 100000, 1000);
  assert.ok(Math.abs(v.s0 - (100000 - 1000 / 64 + 0.03 * 1000 / 64)) < 1e-6, String(v.s0));
  v.s0 = -1e9;
  clampView(v, 100000, 1000);
  assert.ok(Math.abs(v.s0 + 0.03 * 1000 / 64) < 1e-9);
  const z = { s0: 0, spp: 10 };                                 // zooming keeps the sample under the pointer
  clampView(z, 100000, 1000);
  const under = z.s0 + 300 * z.spp;
  zoomView(z, 100000, 1000, 300, 0.5);
  assert.ok(Math.abs(z.s0 + 300 * z.spp - under) < 1e-9);
  assert.equal(z.spp, 5);
  const f = { s0: 0, spp: 106 };                                // a message far too small to read is centred and zoomed
  focusView(f, 100000, 1000, 50000, 50100);
  assert.ok(Math.abs(f.spp - 100 / 300) < 1e-9 && Math.abs(f.s0 + 500 * f.spp - 50050) < 1e-6);
  const tiny = { s0: 0, spp: 1 };                               // three samples on a wide screen: shown whole
  assert.equal(clampView(tiny, 3, 2000), true);
});

check('markers: A, then B, then A again; Shift moves B and keeps A', () => {
  let m = { a: null, b: null };
  m = markerClick(m, 10, false); assert.deepEqual(m, { a: 10, b: null });
  m = markerClick(m, 20, false); assert.deepEqual(m, { a: 10, b: 20 });
  m = markerClick(m, 30, true); assert.deepEqual(m, { a: 10, b: 30 });
  m = markerClick(m, 40, true); assert.deepEqual(m, { a: 10, b: 40 });
  m = markerClick(m, 50, false); assert.deepEqual(m, { a: 50, b: null });
  assert.deepEqual(markerClick({ a: null, b: null }, 7, true), { a: 7, b: null });   // Shift with no A sets A
});

check('rows: 16 fit a laptop\'s window, or scroll at their smallest; a lane for each analyzer', () => {
  const vis = [...Array(16).keys()], none = vis.map(() => []);
  const tall = layoutRows(vis, none, 900);
  assert.equal(tall[0].h, ROW_MAX);
  const fits = layoutRows(vis, none, 600);
  assert.ok(fits.every(r => r.h === 37) && fits[15].y + fits[15].h <= 600);
  const small = layoutRows(vis, none, 400);                     // no room: the smallest, and the rows scroll
  assert.ok(small.every(r => r.h === ROW_MIN));
  const two = vis.map(ch => ch === 0 ? ['i2c', 'sigrok'] : ch === 6 ? ['uart'] : []);
  const L = layoutRows(vis, two, 700);
  assert.equal(L[0].h, L[0].base + L[0].step);
  assert.equal(L[6].h, L[6].base);
  assert.ok(L[0].high > 3 + 2 * L[0].laneH && L[0].high < L[0].low);    // the trace is under both lanes
  for (let i = 1; i < 16; i++) assert.equal(L[i].y, L[i - 1].y + L[i - 1].h);
});

check('names are checked as the command line checks them, with its words', () => {
  const names = Array(16).fill('');
  names[0] = 'SDA';
  assert.equal(nameProblem('', 2, names), '');
  assert.equal(nameProblem('CLK_IN', 2, names), '');
  assert.equal(nameProblem('A2', 2, names), '');                // its own id
  assert.equal(nameProblem('my clock', 2, names), '"my clock": a name is 1 to 24 letters, digits and _ . + # / -, with no space.');
  assert.equal(nameProblem('x'.repeat(25), 2, names), `"${'x'.repeat(25)}": a name is 1 to 24 letters, digits and _ . + # / -, with no space.`);
  assert.equal(nameProblem('b3', 2, names), 'b3 cannot be a channel\'s name: it means something else in a trigger.');
  assert.equal(nameProblem('Then', 2, names), 'Then cannot be a channel\'s name: it means something else in a trigger.');
  assert.equal(nameProblem('sda', 2, names), 'A0 and A2 would both be named sda.');
  assert.equal(nameProblem('sda', 0, names), '');               // its own name, in other letters
});

check('thresholds: refused outside the board\'s range, before the board is asked', () => {
  assert.equal(thresholdProblem('1.65', [0.3, 3.0], 'A'), '');
  assert.equal(thresholdProblem('3.30', [0.3, 3.0], 'A'), 'Threshold A is 3.30 V, and this board takes 0.30 to 3.00 V.  About half the logic\'s voltage is right: 1.65 V for 3.3 V logic, 0.9 V for 1.8 V.');
  assert.equal(thresholdProblem('0.1', [0.3, 6.0], 'B').slice(0, 59), 'Threshold B is 0.10 V, and this board takes 0.30 to 6.00 V.');
  assert.equal(thresholdProblem('4.5', [0.3, 6.0], 'B'), '');   // a revision made for more
  assert.equal(thresholdProblem('abc', [0.3, 3.0], 'A'), 'Threshold A is not a number of volts.');
  assert.equal(thresholdProblem('9', null, 'A'), '');           // no range known: the server checks it
});

check('a serial speed as typed', () => {
  assert.deepEqual(['115200', ' 1,000,000 ', '250k', '1.5M', '9600 baud', '74880'].map(parseBaud), [115200, 1e6, 250000, 1.5e6, 9600, 74880]);
  assert.deepEqual(['fast', '', '-9600', '0', '1e3'].map(parseBaud), [null, null, null, null, null]);
});

check('a trigger\'s text, as the messages name it', () => {
  assert.deepEqual(['B0=r', 'a3=F', 'B7=e', 'A0=1 for 10us', ''].map(trigName), ['B0 rising', 'A3 falling', 'B7 either edge', 'A0=1 for 10us', '']);
});

// ---- made-up buses: the modes, formats and awkward starts the simulated signals do not have ----
// A timeline of the 16 inputs: set() changes an input now, wait() lets samples go by.
class Timeline {
  constructor(initial) { this.t = 0; this.init = initial.slice(); this.lvl = initial.slice(); this.edges = initial.map(() => []); }
  set(ch, v) { if (this.lvl[ch] !== v) { this.lvl[ch] = v; this.edges[ch].push(this.t); } }
  wait(n) { this.t += n; }
  capture(rate, from = 0) {          // from: the capture starts this many samples in, as a capture that began late
    const channels = this.init.map((_, ch) => {
      const E = this.edges[ch].filter(e => e > from).map(e => e - from);
      const before = this.edges[ch].filter(e => e <= from).length;
      return { initial: this.init[ch] ^ (before & 1), edges: Uint32Array.from(E) };
    });
    return new Capture({ sampleRate: rate, numSamples: this.t - from, channels });
  }
}

// SPI in a mode, on CLK A3, CS A2, MOSI A4, MISO A5: messages of bytes, h samples a half clock period
function spiBus(mode, messages, h = 5) {
  const cpol = mode >> 1, cpha = mode & 1;
  const T = new Timeline(Array.from({ length: 16 }, (_, ch) => ch === 3 ? cpol : ch === 2 ? 1 : 0));
  T.wait(40);
  for (const [mosi, miso] of messages) {
    T.set(2, 0); T.wait(h);
    mosi.forEach((byte, k) => {
      for (let b = 7; b >= 0; b--) {
        const put = () => { T.set(4, byte >> b & 1); T.set(5, miso[k] >> b & 1); };
        if (cpha === 0) { put(); T.wait(h); T.set(3, cpol ^ 1); T.wait(h); T.set(3, cpol); }
        else { T.set(3, cpol ^ 1); T.wait(1); put(); T.wait(h - 1); T.set(3, cpol); T.wait(h); }
      }
    });
    T.wait(h); T.set(2, 1); T.set(4, 0); T.set(5, 0); T.wait(3000);
  }
  return T;
}

const ID = [[0x9F, 0, 0, 0], [0xFF, 0xEF, 0x40, 0x18]], READ = [[0x03, 0x00, 0x10, 0x01, 0, 0], [0xFF, 0xFF, 0xFF, 0xFF, 0xDE, 0xAD]];
for (const mode of [0, 1, 2, 3]) {
  check(`SPI mode ${mode}: decoded, and found by auto-detect with its mode`, () => {
    const cap = spiBus(mode, [ID, READ, ID, READ, ID, READ]).capture(10e6);
    const cfg = { clk: 3, mosi: 4, miso: 5, cs: 2, cpol: mode >> 1, cpha: mode & 1, bits: 8, msb: true, csActive: 0 };
    const r = decodeSPI(cap, cfg);
    assert.equal(r.rows.length, 6);
    assert.deepEqual([r.rows[0].mosi, r.rows[0].miso, r.rows[1].mosi, r.rows[1].miso], [...ID, ...READ]);
    const wrong = decodeSPI(cap, { ...cfg, cpha: cfg.cpha ^ 1 });            // the other phase reads other bytes
    assert.notDeepEqual(wrong.rows[0].miso, ID[1]);
    const found = detect(cap).analyzers.filter(a => a.type === 'spi');
    assert.equal(found.length, 1);
    assert.deepEqual(found[0].cfg, cfg);
    assert.deepEqual(found[0].names, { 3: 'SPI_CLK', 2: 'SPI_CS', 4: 'SPI_MOSI', 5: 'SPI_MISO' });
  });
}

check('SPI: a capture that starts inside a message decodes from the next one whole', () => {
  const T = spiBus(0, [READ, ID, READ]);
  const cap = T.capture(10e6, T.edges[3][20]);                  // in the first message's third byte
  assert.equal(cap.channels[2].initial, 0);                     // the chip select is low already
  const r = decodeSPI(cap, { clk: 3, mosi: 4, miso: 5, cs: 2, cpol: 0, cpha: 0, bits: 8, msb: true, csActive: 0 });
  const whole = r.rows.filter(x => x.mosi.length === 4 || x.mosi.length === 6);
  assert.deepEqual(whole.slice(-2).map(x => x.miso), [ID[1], READ[1]]);
});

// A serial line on A6 at 115200 baud, 20 samples a bit, resting high: bits data bits, parity, one stop bit
function serialLine(bytes, { bits = 8, parity = 'none', gap = 3, bit = 20 } = {}) {
  const T = new Timeline(Array.from({ length: 16 }, (_, ch) => ch === 6 ? 1 : 0));
  T.wait(10 * bit);
  for (const byte of bytes) {
    const frame = [0];
    let ones = 0;
    for (let k = 0; k < bits; k++) { const b = byte >> k & 1; frame.push(b); ones += b; }
    if (parity !== 'none') frame.push((ones & 1) ^ (parity === 'odd' ? 1 : 0));
    frame.push(1);
    for (const b of frame) { T.set(6, b); T.wait(bit); }
    T.wait(gap * bit);
  }
  T.wait(10 * bit);
  return T;
}
const TEXT = [...'Logic Boole 16: frame 2, 25.4 C\r\nOK?\r\n'].map(c => c.charCodeAt(0));

for (const [bits, parity, name] of [[8, 'even', '8E1'], [8, 'odd', '8O1'], [7, 'even', '7E1']]) {
  check(`serial ${name}: decoded with no error, and found by auto-detect as ${name}`, () => {
    const cap = serialLine(TEXT, { bits, parity }).capture(115200 * 20);
    const cfg = { rx: 6, baud: 115200, bits, parity, stop: 1, invert: false, msb: false };
    const r = decodeUART(cap, cfg);
    assert.deepEqual([r.stats.bytes, r.stats.framing, r.stats.parity], [TEXT.length, 0, 0]);
    assert.equal(r.rows.map(x => str(x.bytes)).join(''), str(TEXT));
    const other = decodeUART(cap, { ...cfg, parity: parity === 'even' ? 'odd' : 'even' });
    assert.equal(other.stats.parity, TEXT.length);              // every byte's parity wrong the other way
    const found = detect(cap).analyzers;
    assert.equal(found.length, 1, JSON.stringify(found.map(f => f.cfg)));
    assert.deepEqual(found[0].cfg, cfg);
    assert.equal(found[0].score, TEXT.length);
    assert.deepEqual(found[0].names, { 6: 'UART' });
  });
}

check('serial 8N1 with every byte\'s ones even stays 8N1: 7E1 needs a byte with its top bit set', () => {
  const even = TEXT.filter(c => { let n = 0; for (let v = c; v; v &= v - 1) n++; return !(n & 1); });
  const cap = serialLine([...even, ...even, ...even]).capture(115200 * 20);
  assert.deepEqual(detect(cap).analyzers.map(f => [f.cfg.bits, f.cfg.parity]), [[8, 'none']]);
});

check('serial: a capture that starts inside a byte finds the bytes again, and one that ends inside a byte', () => {
  const T = serialLine(TEXT);
  const cap = T.capture(115200 * 20, T.edges[6][3] + 5);        // inside the first byte
  const r = decodeUART(cap, { rx: 6, baud: 115200, bits: 8, parity: 'none', stop: 1 });
  const text = r.rows.map(x => str(x.bytes)).join('');
  assert.ok(text.endsWith(str(TEXT.slice(5))), JSON.stringify(text));     // in step again within a few bytes
  assert.ok(r.stats.framing >= 1);                              // and what it read out of step is marked
  const full = T.capture(115200 * 20);
  const end = new Capture({ ...full, numSamples: full.channels[6].edges[40] + 30 });  // ends in a byte's middle
  end.channels = full.channels.map(c => ({ initial: c.initial, edges: c.edges.filter(e => e < end.numSamples) }));
  const cut = decodeUART(end, { rx: 6, baud: 115200, bits: 8, parity: 'none', stop: 1 });
  assert.equal(cut.stats.framing, 0);                           // the byte the capture ends in is left out
});

check('I2C: a capture that starts inside a message decodes from the next start', () => {
  const T = new Timeline(Array.from({ length: 16 }, (_, ch) => ch < 2 ? 1 : 0)), h = 10;
  const byte = (v, ack) => { for (let b = 7; b >= -1; b--) { T.set(0, b >= 0 ? v >> b & 1 : ack ? 0 : 1); T.wait(h); T.set(1, 1); T.wait(h); T.set(1, 0); } };
  const message = (addr, data) => {
    T.set(0, 0); T.wait(h); T.set(1, 0); T.wait(h);               // start
    byte(addr << 1, true);
    for (const d of data) byte(d, true);
    T.set(0, 0); T.wait(h); T.set(1, 1); T.wait(h); T.set(0, 1); T.wait(5 * h);   // stop
  };
  T.wait(5 * h);
  message(0x50, [0x00, 0x10, 0x4C]);
  message(0x48, [0x00]);
  message(0x50, [0x01, 0x02]);
  const cap = T.capture(1e6, T.edges[1][12]);                   // in the first message's address
  const r = decodeI2C(cap, { sda: 0, scl: 1 });
  const ok = r.rows.filter(x => x.addr === 0x48 || (x.addr === 0x50 && x.data.length === 2));
  assert.deepEqual(ok.map(x => [x.addr, x.data]), [[0x48, [0x00]], [0x50, [0x01, 0x02]]]);
});

check('auto-detect looks at a bounded part, and what it decoded there is used, not decoded again', () => {
  const cap = makeDemo(25e6, 2500000);
  const all = detectAll(cap);
  for (const f of all.analyzers) {
    const again = decodeOne(cap, f.type, f.cfg);
    assert.deepEqual(f.result.rows.length, again.rows.length, f.type);
    assert.deepEqual(f.result.stats, again.stats, f.type);
  }
  const d = detect(cap);
  assert.ok(d.analyzers.filter(f => f.type !== 'spi').every(f => f.whole && f.result), 'decoded whole by auto-detect');
  assert.deepEqual(decodeOne(cap, 'i2c', { sda: 1, scl: 1 }), { error: 'SDA and SCL are the same channel' });
});

check('two serial lines: UART and UART2; two buses never share a name', () => {
  const names = detect(makeDemo(100e6, 10e6)).analyzers.map(f => f.names);
  assert.deepEqual(names, [{ 2: 'SPI_CS', 3: 'SPI_CLK', 4: 'SPI_MOSI', 5: 'SPI_MISO' }, { 0: 'SDA', 1: 'SCL' }, { 6: 'UART' }, { 7: 'UART2' }]);
});

check('the decoders stop at the boxes the page holds, and say where', () => {
  const cap = makeDemo(25e6, 2500000);
  const { decodeUART: u, decodeI2C: i, decodeSPI: s } = { decodeUART, decodeI2C, decodeSPI };
  const r = u(cap, { rx: 6, baud: 115200, bits: 8, parity: 'none', stop: 1 }, Infinity, 10);
  assert.equal(r.ann.length, 10);
  assert.ok(r.stats.cut > r.ann[9].s);
  assert.ok(i(cap, { sda: 0, scl: 1 }, Infinity, 20).stats.cut > 0);
  assert.ok(s(cap, { clk: 3, mosi: 4, miso: 5, cs: 2, cpol: 0, cpha: 0, bits: 8, msb: true, csActive: 0 }, 20).stats.cut > 0);
  assert.equal(u(cap, { rx: 6, baud: 115200, bits: 8, parity: 'none', stop: 1 }).stats.cut, undefined);
});

console.log('files: what the app refuses, what it cuts, what it keeps');
{
  const file = (head, edges = [], magic = 'LB16CAP1') => {
    let json = typeof head === 'string' ? head : JSON.stringify(head);
    while ((12 + Buffer.byteLength(json)) % 4) json += ' ';
    const pre = Buffer.alloc(12);
    pre.write(magic, 0, 'latin1');
    pre.writeUInt32LE(Buffer.byteLength(json), 8);
    const all = Buffer.concat([pre, Buffer.from(json), Buffer.from(Uint32Array.from(edges).buffer)]);
    return all.buffer.slice(all.byteOffset, all.byteOffset + all.byteLength);
  };
  const chans = (n = 16, edges = 0) => Array.from({ length: n }, () => ({ initial: 0, edges }));
  const good = { sampleRate: 1e6, numSamples: 100, channels: chans() };
  const refused = (buf) => { try { decodeCapture(buf); } catch (e) { return e.message; } return 'opened'; };
  check('files the app cannot show are refused with the reason, before anything of them is used', () => {
    const not = 'it is not a capture the app can show: ';
    assert.equal(refused(file(good)), 'opened');
    assert.equal(refused(file(good, [], 'LB16CAP9')), 'it is not a Logic Boole capture (.lb16)');
    assert.equal(refused(file(good).slice(0, 30)), 'the file is cut short: it ends inside its header');
    assert.equal(refused(file('{"sampleRate": 1e6,')), not + 'its header cannot be read');
    assert.equal(refused(file('[]')), not + 'its header is empty');
    assert.equal(refused(file({ ...good, sampleRate: 0 })), not + 'it gives no sample rate');
    assert.equal(refused(file({ ...good, sampleRate: '1e6' })), not + 'it gives no sample rate');
    for (const n of [0, -5, 1.5, 2 ** 33, null]) assert.equal(refused(file({ ...good, numSamples: n })), not + 'it holds no samples, or a number of them the app cannot count');
    assert.equal(refused(file({ ...good, channels: null })), not + 'it lists no channels');
    assert.equal(refused(file({ ...good, channels: chans(8) })), not + 'it has 8 channels, and the app shows captures of 16');
    const c = chans(); c[9] = { initial: 2, edges: 0 };
    assert.equal(refused(file({ ...good, channels: c })), not + 'channel B1 is not described as a channel is');
    const one = chans(); one[0] = { initial: 0, edges: 3 };
    assert.equal(refused(file({ ...good, channels: one }, [5, 9])), 'the file is cut short: it ends before the last of its channels\' changes');
    assert.equal(refused(file({ ...good, channels: one }, [5, 9, 7])), not + 'the changes of A0 are not in order');
    assert.equal(refused(file({ ...good, channels: one }, [5, 9, 100])), not + 'A0 changes after the capture\'s last sample');
    // what is odd but harmless is left out: a trigger past the end, gaps and names that are not what they should be
    const { cap, head } = decodeCapture(file({ ...good, trigger: 500, gaps: [{ start: 'x' }, { start: 1, length: 2 }], names: [5, 'SDA'], analyzers: 'none', info: [1] }));
    assert.deepEqual([cap.trigger, cap.gaps, head.names, head.analyzers, cap.info], [0, [{ start: 1, length: 2 }], ['', 'SDA'], [], null]);
  });

  check('a capture with more changes than the page holds is cut where they reach it, and says so', () => {
    const cap = makeDemo(25e6, 2500000), total = cap.transitions, most = Math.floor(total / 3);
    const parts = encodeCapture(cap, {});
    const buf = Buffer.concat(parts.map(p => Buffer.from(p.buffer, p.byteOffset, p.byteLength)));
    const { cap: back } = decodeCapture(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength), most);
    assert.ok(back.transitions <= most && back.transitions > most * 0.9, `${back.transitions} of ${most}`);
    assert.deepEqual([back.cut.of, back.numSamples], [2500000, back.cut.at]);
    for (let ch = 0; ch < NCH; ch++) assert.deepEqual(back.channels[ch].edges, cap.channels[ch].edges.filter(e => e < back.numSamples));
    const words = new Uint32Array(toRle(cap));
    const rle = fromRle(words, 25e6, Infinity, '', most);
    assert.ok(rle.transitions <= most && rle.cut.of === 2500000 && rle.cut.at === rle.numSamples);
    for (let ch = 0; ch < NCH; ch++) assert.deepEqual(rle.channels[ch].edges, cap.channels[ch].edges.filter(e => e < rle.numSamples));
    const raw = fromWords(Uint16Array.from({ length: 1000 }, (_, i) => i), 1e6, '', 100);
    assert.ok(raw.transitions <= 100 && raw.cut.at === raw.numSamples && raw.cut.of === 1000);
    assert.equal(fromRle(words, 25e6).cut, null);
  });

  check('what a capture file says of itself: flags, the inputs a fast capture did not sample, the old layout', () => {
    assert.deepEqual(captureFlags({ thresholds: [1, 1], overflow: false, stopped: null, problems: [] }).notes, []);
    const f = captureFlags({ overflow: true, stopped: 'stopped', trigger: 'B3=r', trigger_sample: null, problems: ['2 link errors'] });
    assert.deepEqual(f.notes, ['It overflowed: it ends before the length asked for, and is whole up to its end.',
      'Not triggered: B3 rising did not come, and this is what the board saw last.', 'Found wrong when it was taken: 2 link errors.']);
    const fast = captureFlags({ fast: 'B0-B3', inputs: ['B0', 'B1', 'B2', 'B3'] });
    assert.deepEqual([...fast.off].sort((a, b) => a - b), [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15]);
    assert.equal(fast.notes[0], 'Taken with the fast design: B0, B1, B2, B3 only; the other inputs were not sampled.');
    assert.equal(captureFlags({ fast: 'all', inputs: CH_IDS }).off.size, 0);
    const old = { time: 5, meta: { thresholds: [1.2, 0.9], overflow: true, problems: ['x'], trigger: 'A0=f', asked: 9 } };
    assert.deepEqual(normalInfo(old), { time: 5, thresholds: [1.2, 0.9], overflow: true, problems: ['x'], trigger: 'A0=f', samples_asked: 9 });
    assert.equal(captureFlags(old).notes[0], 'It overflowed: it ends before the length asked for, and is whole up to its end.');
    assert.deepEqual(savedInfo({ overflow: false, stopped: null }, { at: 5, of: 9 }), { overflow: false, stopped: 'size' });
    assert.deepEqual(savedInfo({ overflow: false, stopped: 'stopped' }, null), { overflow: false, stopped: 'stopped' });
  });
}

// ---- the command line and the app agree: names, and what a saved capture says of itself ----
{
  const dir = fileURLToPath(new URL('..', import.meta.url));
  const tmp = mkdtempSync(join(tmpdir(), 'lb16-'));
  const py = (code, ...args) => execFileSync('python3', ['-B', '-c', `import sys\nsys.path.insert(0, sys.argv[1])\n${code}`, dir, ...args], { maxBuffer: 1 << 27 });
  const write = (path, parts) => writeFileSync(path, Buffer.concat(parts.map(p => Buffer.from(p.buffer, p.byteOffset, p.byteLength))));

  check('a name the page refuses the command line refuses too, with the same words', () => {
    const cases = [['CLK_IN', 2], ['clk!', 2], ['x'.repeat(25), 3], ['B3', 2], ['b3', 11], ['pulse', 4], ['IN', 4], ['sda', 2], ['SDA', 0], ['Sda', 9], ['a.b+c#d/e-f', 5]];
    const names = Array(16).fill(''); names[0] = 'SDA'; names[1] = 'SCL'; names[9] = 'sda2';
    const pyOut = JSON.parse(py(`
import json
from array import array
from logicboole.capture import Capture, parse_names, CHANNELS
names, out = json.loads(sys.argv[2]), []
for name, ch in json.loads(sys.argv[3]):
    cap = Capture(1e6, array("H", [0]), array("I", [1]))
    cap.names = [n or CHANNELS[k] for k, n in enumerate(names)]
    try:
        cap.set_names(parse_names("%s=%s" % (CHANNELS[ch], name)))
        out.append("")
    except ValueError as e:
        out.append(str(e))
print(json.dumps(out))`, JSON.stringify(names), JSON.stringify(cases)));
    assert.deepEqual(cases.map(([n, ch]) => nameProblem(n, ch, names)), pyOut);
    assert.ok(pyOut.filter(Boolean).length >= 6, JSON.stringify(pyOut));
  });

  check('the command line\'s detect gives the channels the names the app\'s auto-detect gives', () => {
    const demo = makeDemo(100e6, 10e6);
    write(join(tmp, 'names.lb16'), encodeCapture(demo, { names: [], analyzers: [] }));
    const pyNames = JSON.parse(py(`
import json
from logicboole import detect
from logicboole.capture import Capture
print(json.dumps([b["names"] for b in detect.detect(Capture.load(sys.argv[2]))["buses"]]))`, join(tmp, 'names.lb16')));
    const js = detect(demo).analyzers.map(f => Object.entries(f.names).sort((a, b) => a[0] - b[0]).map(([ch, n]) => `${CH_IDS[ch]}=${n}`).join(','));
    assert.deepEqual(pyNames.map(s => s.split(',').sort((a, b) => CH_IDS.indexOf(a.split('=')[0]) - CH_IDS.indexOf(b.split('=')[0])).join(',')), js);
  });

  check('the command line\'s detect finds 8E1, 8O1 and 7E1 as the app does', () => {
    for (const [bits, parity] of [[8, 'even'], [8, 'odd'], [7, 'even']]) {
      const cap = serialLine(TEXT, { bits, parity }).capture(115200 * 20);
      const file = join(tmp, `serial_${bits}${parity}.lb16`);
      write(file, encodeCapture(cap, { names: [], analyzers: [] }));
      const out = JSON.parse(py(`
import json
from logicboole import detect
from logicboole.capture import Capture
buses, _, _ = detect.analyze(Capture.load(sys.argv[2]))
print(json.dumps([[b["type"], b["score"], b["cfg"]] for b in buses]))`, file));
      const js = detect(cap).analyzers[0];
      assert.equal(out.length, 1, `${bits}${parity}: ${JSON.stringify(out)}`);
      assert.deepEqual([out[0][0], out[0][1], out[0][2].baud, out[0][2].bits, out[0][2].parity], ['uart', js.score, 115200, bits, parity], `${bits}${parity}`);
    }
  });

  check('a capture the page took, saved by it, opens on the command line with its trigger, thresholds, overflow and problems', () => {
    const got = JSON.parse(py(`
import json, time, base64
from logicboole import server, fake
app = server.App(fake.FakeBus(instant=True), fake=True)
assert app.start({"rate": 1e6, "seconds": "0.03", "trigger": "A0=f"}, rate=1e6, seconds=0.03, test=False, a=1.2,
                 b=0.9, trigger="A0=f")
while app.job["state"] == "running":
    app.status()
    time.sleep(0.01)
job = app.job
assert job["state"] == "done", job
data = app.data.tobytes() if hasattr(app.data, "tobytes") else bytes(app.data)
print(json.dumps({"job": {k: job[k] for k in ("rate", "samples", "trigger_sample", "info", "thresholds")},
                  "words": base64.b64encode(data).decode()}))`));
    const { job } = got, raw = Buffer.from(got.words, 'base64');
    const words = new Uint32Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength));
    const cap = fromRle(words, job.rate, job.samples, 'Logic Boole 16 · probes');
    cap.info = savedInfo(job.info, cap.cut);                    // as app.js keeps it
    cap.trigger = job.trigger_sample;
    assert.ok(job.trigger_sample > 0);
    const names = Array(16).fill(''); names[0] = 'SDA'; names[1] = 'SCL';
    write(join(tmp, 'page.lb16'), encodeCapture(cap, { names, analyzers: [{ type: 'i2c', cfg: { sda: 0, scl: 1 } }] }));
    const back = JSON.parse(py(`
import json
from logicboole.capture import Capture
cap = Capture.load(sys.argv[2])
print(json.dumps({"trigger": cap.trigger, "thresholds": cap.thresholds, "overflow": cap.overflow, "stopped": cap.stopped,
                  "problems": cap.problems, "trigger_text": cap.info.get("trigger"), "names": cap.names[:3],
                  "samples": cap.samples}))`, join(tmp, 'page.lb16')));
    assert.deepEqual(back, { trigger: job.trigger_sample, thresholds: job.thresholds, overflow: false, stopped: null, problems: [],
      trigger_text: 'A0=f', names: ['SDA', 'SCL', 'A2'], samples: job.samples });
    // and the page opens it again the same
    const buf = readFileSync(join(tmp, 'page.lb16'));
    const { cap: again } = decodeCapture(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
    assert.deepEqual([again.trigger, normalInfo(again.info).trigger, normalInfo(again.info).thresholds], [job.trigger_sample, 'A0=f', job.thresholds]);
  });

  check('a file in the app\'s earlier layout (info.meta) reads the same in the app and on the command line', () => {
    const demo = makeDemo(1e6, 50000);
    demo.trigger = 1234;
    demo.info = { meta: { thresholds: [1.5, 0.8], overflow: true, stopped: null, problems: ['a word was lost'], trigger: 'A0=f', asked: 60000 } };
    write(join(tmp, 'old.lb16'), encodeCapture(demo, { names: [], analyzers: [] }));
    const back = JSON.parse(py(`
import json
from logicboole.capture import Capture
cap = Capture.load(sys.argv[2])
print(json.dumps({"trigger": cap.trigger, "thresholds": cap.thresholds, "overflow": cap.overflow, "problems": cap.problems,
                  "trigger_text": cap.info.get("trigger"), "asked": cap.info.get("samples_asked")}))`, join(tmp, 'old.lb16')));
    const js = normalInfo(demo.info);
    assert.deepEqual(back, { trigger: 1234, thresholds: js.thresholds, overflow: js.overflow, problems: js.problems,
      trigger_text: js.trigger, asked: js.samples_asked });
  });
  rmSync(tmp, { recursive: true });
}

console.log(process.exitCode ? '\nFAILED' : `\nall ${passed} checks passed`);
