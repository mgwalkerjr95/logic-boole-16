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
import { Capture, NCH, Walker, fromWords, fromRle, encodeCapture, decodeCapture } from '../logicboole/web/capture.js';
import { decodeI2C, decodeSPI, decodeUART, estimateBaud, rowText, annTexts } from '../logicboole/web/decoders.js';
import { detect } from '../logicboole/web/detect.js';
import { makeDemo } from '../logicboole/web/demo.js';
import { sigrokWords, sigrokDefaults, sigrokClean, laneOf, layout } from '../logicboole/web/sigrok.js';

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

console.log(process.exitCode ? '\nFAILED' : `\nall ${passed} checks passed`);
