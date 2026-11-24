# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks of the host software with no board:  python3 software/tests/test_host.py

The stand-in board (logicboole/fake.py) answers the USB requests as the firmware and the FPGA's design do, and
codes its samples by a route of its own, so what is compared here was worked out twice.
"""
import base64
import contextlib
import ctypes
import hashlib
import io
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from array import array
from bisect import bisect_right
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import logicboole                                           # noqa: E402
from logicboole import board as B, cli, decode as D, fake, fast as F, mcp, server, trigger as T, usb   # noqa: E402
from logicboole import capture as C, detect as DT, guide as G, plot as P, search, words as W   # noqa: E402
from logicboole.capture import CHANNELS, Capture            # noqa: E402
from logicboole.selftest import selftest, selftest_fast     # noqa: E402

# A revision made up for the tests, beside the real ones: a scale on which a design's starting duty cycle (2048)
# is not START_THRESHOLD, so that setting the thresholds after a load is checked too
MADE_UP = B.Revision("VTEST", 0.4, 0.3, 5.0)


class SlowClock:
    """board.py's clock running at a tenth of the speed.  The stand-in makes the busy test pattern far more slowly
    than the FPGA does, and a capture's deadline (the time it should take, and a little more) is for a real board."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def monotonic():
        return time.monotonic() / 10


def coded(runs, fmt):
    c = fake.Coder(fmt)
    for value, n in runs:
        c.add(value, n)
    c.end()
    return c.words()


def samples_of(values, lengths):
    out = []
    for v, n in zip(values, lengths):
        out += [v] * n
    return out


def random_runs(rng, count, longest):
    runs, last = [], None
    for _ in range(count):
        v = rng.choice([x for x in (0, 1, 2, 3, 0x8000, 0xFFFF, rng.randrange(65536)) if x != last])
        runs.append((v, rng.choice((1, 1, 2, 3, 7, rng.randrange(1, longest)))))
        last = v
    return runs


def capture_of(rate, total, lines, trigger=None):
    """A capture from each line's level at sample 0 and the samples at which it changes: {channel: (level, [...])}."""
    cuts = sorted({s for _, e in lines.values() for s in e if 0 < s < total})
    values, lengths = array("H"), array("Q")
    for a, b in zip([0] + cuts, cuts + [total]):
        values.append(sum((level ^ (bisect_right(e, a) & 1)) << ch for ch, (level, e) in lines.items()))
        lengths.append(b - a)
    return Capture(rate, values, lengths, trigger=trigger)


def uart_edges(start, bit, data, bits=8, parity="none"):
    """A serial line idling high: 8 data bits (or `bits`), the lowest first, a parity bit if parity is "even" or
    "odd", one stop bit and two bits' rest after each byte."""
    edges, level, t = [], 1, start
    for byte in data:
        frame = [byte >> i & 1 for i in range(bits)]
        if parity != "none":
            frame.append((sum(frame) + (parity == "odd")) & 1)
        for b in [0] + frame + [1, 1, 1]:
            if b != level:
                edges.append(round(t))
                level = b
            t += bit
    return edges


def spi_edges(start, half, transfers):
    """SPI mode 0, the highest bit first: CS (idle high), the clock (idle low) and MOSI, which changes between
    clock edges."""
    cs, clk, mosi, level, t = [], [], [], 0, start
    for transfer in transfers:
        cs.append(t)
        t += half
        for byte in transfer:
            for i in range(7, -1, -1):
                if byte >> i & 1 != level:
                    mosi.append(t + half // 2)
                    level ^= 1
                clk += [t + half, t + 2 * half]
                t += 2 * half
        t += half
        cs.append(t)
        t += 20 * half
    return cs, clk, mosi



class Words(unittest.TestCase):
    def test_pattern_is_what_the_sampler_model_codes(self):
        for fmt in ("rle", "raw"):
            for shift in (0, 1, 3, 8, 15):
                for samples in (1, 2, 3, 255, 256, 257, 65535, 65536, 65537, 131073, 200001):
                    want = coded(fake.pattern_runs(0, samples, shift), fmt)
                    got = b"".join(W.pattern_blocks(fmt, shift, samples))
                    self.assertEqual(got, want, (fmt, shift, samples))
                    self.assertIsNone(W.check_pattern(want, fmt, shift, samples))

    def test_a_wrong_capture_is_caught(self):
        for fmt in ("rle", "raw"):
            good = coded(fake.pattern_runs(0, 150000, 2), fmt)
            self.assertIsNone(W.check_pattern(good, fmt, 2, 150000))
            n = len(good) // 4
            for at in (0, 1, n // 2, n - 1):
                flipped = good[:4 * at] + bytes([good[4 * at] ^ 1]) + good[4 * at + 1:]
                self.assertIn("word %d is" % at, W.check_pattern(flipped, fmt, 2, 150000))
                lost = good[:4 * at] + good[4 * at + 4:]
                self.assertIsNotNone(W.check_pattern(lost, fmt, 2, 150000))
                twice = good[:4 * at] + good[4 * at:4 * at + 4] + good[4 * at:]
                self.assertIsNotNone(W.check_pattern(twice, fmt, 2, 150000))
            self.assertIsNotNone(W.check_pattern(good, fmt, 3, 150000))
            self.assertIsNotNone(W.check_pattern(good, fmt, 2, 150002))

    def test_runs_from_both_formats(self):
        rng = random.Random(16)
        for trial in range(30):
            runs = random_runs(rng, rng.randrange(1, 60), rng.choice((5, 300, 70000, 200000)))
            want = samples_of(*zip(*runs))
            for fmt in ("rle", "raw"):
                data = coded(runs, fmt)
                values, lengths = W.runs_of(data, fmt, len(want))
                self.assertEqual(samples_of(values, lengths), want)
                self.assertEqual(sum(lengths), len(want))
                self.assertTrue(all(a != b for a, b in zip(values, values[1:])) or fmt == "rle")
                self.assertEqual(W.words_of_runs(*zip(*runs), fmt), data)
                held = W.samples_held(data, fmt)
                self.assertIn(held, (len(want), len(want) + 1) if fmt == "raw" else (len(want),))
                cut = max(1, len(want) // 3)
                self.assertEqual(samples_of(*W.runs_of(data, fmt, cut)), want[:cut])

    def test_the_first_samples_are_dropped(self):
        rng = random.Random(4)
        runs = [(rng.randrange(65536), rng.choice((1, 1, 2, 7, 300, 65536, 70000))) for _ in range(300)]
        samples = samples_of(*zip(*runs))
        for fmt in ("rle", "raw"):
            data = coded(runs, fmt)
            for n in (0, 1, 2, 3, 299, 70001, len(samples) - 1, len(samples), len(samples) + 5):
                rest, gone = W.drop_samples(data, fmt, n)
                self.assertEqual(gone, min(n, len(samples)) if fmt == "rle" else min(n - n % 2, len(samples) + 1 & ~1))
                self.assertEqual(samples_of(*W.runs_of(rest, fmt))[:len(samples) - gone], samples[gone:], (fmt, n))
        self.assertEqual(W.drop_samples(b"", "rle", 9), (b"", 0))

    def test_a_pattern_capture_that_begins_anywhere(self):
        for fmt in ("rle", "raw"):
            for shift, first, n in ((0, 0, 3000), (0, 7001, 3000), (3, 8, 9000), (3, 13, 9000), (8, 5 * 256 + 255, 70000),
                                    (15, 99999, 400000)):
                data = coded(list(fake.pattern_runs(first, first + n, shift)), fmt)
                self.assertEqual(W.pattern_origin(data, fmt, shift), first, (fmt, shift, first))
                self.assertIsNone(W.check_pattern(data, fmt, shift, n, first))
                self.assertIsNotNone(W.check_pattern(data, fmt, shift, n, first + 1))
        # a run-length capture with a trigger: the word that holds the trigger sample begins with it
        shift, first, at, n = 4, 37, 1000, 5000
        c = fake.Coder("rle")
        for value, k in fake.pattern_runs(first, first + at, shift):
            c.add(value, k)
        c.cut()
        before = len(c.out)
        for value, k in fake.pattern_runs(first + at, first + n, shift):
            c.add(value, k)
        c.end()
        data = c.words()
        self.assertIsNone(W.check_pattern_triggered(data, "rle", shift, n, before))
        self.assertIsNotNone(W.check_pattern(data, "rle", shift, n, first))     # as one run of words, it is not right
        bad = bytearray(data)
        bad[4 * (before + 3)] ^= 1
        self.assertIn("from the trigger sample's word on", W.check_pattern_triggered(bytes(bad), "rle", shift, n, before))

    def test_sample_bytes(self):
        runs = [(1, 3), (0xABCD, 2), (0, 70000)]
        raw = b"".join(W.sample_bytes(*zip(*runs)))
        self.assertEqual(list(struct.unpack("<%dH" % (len(raw) // 2), raw)), samples_of(*zip(*runs)))

    def test_runs_made_from_edges_and_the_edges_kept(self):
        # a file holds each channel's edges, and its runs are made from them only when they are asked for
        rng = random.Random(21)
        for trial in range(40):
            runs = random_runs(rng, rng.randrange(1, 80), rng.choice((3, 300, 70000)))
            if trial % 4 == 0:                  # only A0 changes: its edges are in order as they are
                runs = [(i & 1, n) for i, (_, n) in enumerate(runs)]
            cap = Capture(1e6, array("H", [v for v, _ in runs]), array("Q", [n for _, n in runs]))
            edges = cap.edges()
            self.assertIs(cap.edges(), edges)                       # worked out once
            self.assertEqual({e.typecode for _, e in edges}, {"I"})
            self.assertEqual(samples_of(*C._runs_of_edges(edges, cap.samples)), samples_of(cap.values, cap.lengths))
        self.assertEqual(Capture(1e6, array("H", [0, 1]), array("Q", [1 << 32, 5])).edges()[0][1].typecode, "Q")
        data = coded([(5, 3), (6, 70000)], "rle")
        self.assertEqual(W.samples_held(memoryview(data)[4:], "rle"), 70000)     # a part of the words, not copied


def trigger_sample(samples, stages):
    """Where a trigger is met in a list of samples, worked out one sample at a time: its number, or None."""
    k, prev = 0, None
    run = [0] * len(stages)                 # samples in a row, up to the one before, that had the stage's levels
    whole = [False] * len(stages)           # that run began inside the capture
    for i, v in enumerate(samples):
        met = []
        for j, s in enumerate(stages):
            hit = (v & s["mask"]) == s["value"]
            if s["kind"] == 0:
                edge = prev is not None and (~prev & v & s["rise"] or prev & ~v & s["fall"])
                met.append(hit and bool(edge or not s["rise"] | s["fall"]))
            elif s["kind"] == 1:
                met.append(not hit and whole[j] and s["min"] <= run[j] <= s["max"])
            else:
                met.append(s["kind"] == 2 and hit and run[j] + 1 >= max(1, s["min"]))
            if hit and not run[j]:
                whole[j] = i > 0
            run[j] = run[j] + 1 if hit else 0
        if met[k]:
            k += 1
            if k == len(stages):
                return i
        prev = v
    return None


class Triggers(unittest.TestCase):
    def test_text_becomes_stages(self):
        s, = T.parse("B0=r")
        self.assertEqual((s["kind"], s["mask"], s["rise"], s["fall"]), (0, 0, 0x100, 0))
        s, = T.parse("a2=f A0=1, a1=0")
        self.assertEqual((s["kind"], s["mask"], s["value"], s["rise"], s["fall"]), (0, 3, 1, 0, 4))
        s, = T.parse("B7=e")
        self.assertEqual((s["rise"], s["fall"]), (0x8000, 0x8000))
        s, = T.parse("A0=1 for 10us", 200e6)
        self.assertEqual((s["kind"], s["mask"], s["value"], s["min"]), (2, 1, 1, 2000))
        self.assertEqual(T.parse("A0=1 for 0", 1e6)[0]["min"], 1)
        s, = T.parse("A3=0 pulse 100ns..1us", 100e6)
        self.assertEqual((s["kind"], s["mask"], s["value"], s["min"], s["max"]), (1, 8, 0, 10, 100))
        self.assertEqual([(s["min"], s["max"]) for s in T.parse("A0=1 pulse ..50ns then A0=1 pulse 1us.. then A0=1 pulse 7",
                                                                200e6)], [(1, 10), (200, T.NO_LIMIT), (7, 7)])
        s, = T.parse("in=f A0=1")
        self.assertEqual((s["kind"], s["in_rise"], s["in_fall"], s["mask"]), (3, 0, 1, 1))
        four = T.parse("B7=f then B0=r then B1=1 for 2ms then in=e", 1e6)
        self.assertEqual([s["kind"] for s in four], [0, 0, 2, 3])
        for text in ("B0=r", "A0=1 A1=0", "A0=1 A2=f", "A0=1 for 10us", "A3=0 pulse 100ns..1us", "A0=1 pulse ..50ns",
                     "A0=1 pulse 1us..", "A1=1 in=e", "B7=f then B0=r then B1=1 for 2ms then in=r"):
            self.assertEqual(T.describe(T.parse(text, 200e6), 200e6), text)
        self.assertEqual(T.describe(T.parse("A0=1 for 12 then A1=0 pulse 3..4")), "A0=1 for 12 then A1=0 pulse 3..4")

    def test_what_is_not_a_trigger_says_why(self):
        for text, why in (("B9=r", "not a channel"), ("A0", "not understood"), ("A0=2", "not understood"),
                          ("A0=1 A0=r", "named twice"), ("A0=r for 1us", "not with edges"), ("for 1us", "go with levels"),
                          ("A0=1 for", "followed by one time"), ("A0=1 for 1us pulse 2us", "once in a stage"),
                          ("A0=1 pulse 2us..1us", "shorter than its shortest"), ("in=1", "by its edges"),
                          ("in=r A0=f", "nothing else"), ("A0=1 then", "empty stage"), ("A0=1 for soon", "neither a time"),
                          ("A0=r then A1=r then A2=r then A3=r then A4=r", "4 stages at the most")):
            with self.assertRaisesRegex(T.TriggerError, why, msg=text):
                T.parse(text, 1e6)
        with self.assertRaisesRegex(T.TriggerError, "no sample rate"):
            T.parse("A0=1 for 1us")                     # with a capture clock, times are numbers of samples
        with self.assertRaisesRegex(T.TriggerError, "outside its mask"):
            T.stages_of({"mask": 1, "value": 3})
        self.assertEqual(T.stages_of(None), [])
        self.assertEqual(T.stages_of({"rise": 4})[0]["kind"], 0)

    def test_the_registers(self):
        mode, body = T.stage_registers(T.parse("A0=1 A9=0 in=f".replace("A9", "B1"))[0])
        self.assertEqual((mode, body), (3 | 8, struct.pack("<HHHHII", 0x0201, 0x0001, 0, 0, 0, 0)))
        mode, body = T.stage_registers(T.parse("B7=1 pulse 3..70000")[0])
        self.assertEqual((mode, body), (1, struct.pack("<HHHHII", 0x8000, 0x8000, 0, 0, 3, 70000)))
        self.assertEqual(T.clock_register(None), (0, None))
        self.assertEqual(T.clock_register("A0"), (0x03, "A0=r"))
        self.assertEqual(T.clock_register("b3=f"), (0xB5, "B3=f"))
        self.assertEqual(T.clock_register("B7=e"), (0xF7, "B7=e"))
        with self.assertRaises(T.TriggerError):
            T.clock_register("A0=1")
        self.assertEqual(T.filter_samples(None), [0] * 16)
        self.assertEqual(T.filter_samples(50e-9), [10] * 16)
        self.assertEqual(T.filter_samples({"B": 1.25e-6, "A0": 10e-9, "b7": 0}), [2] + [0] * 7 + [250] * 7 + [0])
        for bad in (5e-9, 1.3e-6):
            with self.assertRaisesRegex(T.TriggerError, "10 ns"):
                T.filter_samples(bad)
        self.assertEqual(T.parse_filter("20ns"), {"all": 20e-9})
        self.assertEqual(T.parse_filter("A0=20ns,B=1us A3=off"), {"A0": 20e-9, "B": 1e-6, "A3": 0.0})
        with self.assertRaises(T.TriggerError):
            T.parse_filter("A0=fast")
        self.assertEqual([T.ring_words(n, "rle", 8184) for n in (0, 1, 5000, 10 ** 9)], [0, 1, 5000, 8184])
        self.assertEqual([T.ring_words(n, "raw", 8184) for n in (0, 1, 5001, 10 ** 9)], [1, 2, 2502, 8184])

    def test_words_in_any_case_and_a_pulse_shorter_than_a_sample(self):
        self.assertEqual(T.parse("A0=1 FOR 2us", 10e6), T.parse("A0=1 for 2us", 10e6))
        self.assertEqual(T.parse("a0=0 Pulse 1us..2us", 10e6), T.parse("A0=0 pulse 1us..2us", 10e6))
        with self.assertRaises(T.TriggerError) as caught:
            T.parse("A0=0 pulse ..50ns", 10e6)
        self.assertEqual(str(caught.exception), "A pulse of 50ns is shorter than one sample, which is 100 ns at "
                                                "10 MS/s: give a longer time, or capture faster.")
        self.assertEqual(T.parse("A0=0 pulse ..50ns", 200e6)[0]["max"], 10)
        with self.assertRaisesRegex(T.TriggerError, "longest time is shorter than its shortest"):
            T.parse("A0=0 pulse 2us..1us", 10e6)


class Files(unittest.TestCase):
    def setUp(self):
        rng = random.Random(3)
        runs = random_runs(rng, 400, 5000)
        self.cap = Capture(25e6, array("H", [v for v, _ in runs]), array("Q", [n for _, n in runs]),
                           thresholds=(1.65, 0.9), info={"usb": "5 Gbit/s"})
        self.want = samples_of(self.cap.values, self.cap.lengths)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def test_lb16_comes_back_as_it_was(self):
        self.cap.save(self.path("a.lb16"))
        back = Capture.load(self.path("a.lb16"))
        self.assertEqual(samples_of(back.values, back.lengths), self.want)
        self.assertEqual((back.rate, back.samples, tuple(back.thresholds), back.ok), (25e6, len(self.want), (1.65, 0.9), True))

    def test_sigrok_session_holds_every_sample(self):
        for cap in (self.cap, Capture.from_words(W.words_of_runs(self.cap.values, self.cap.lengths, "raw"), "raw",
                                                 25e6, len(self.want))):
            cap.save(self.path("a.sr"))
            with zipfile.ZipFile(self.path("a.sr")) as z:
                self.assertEqual(z.read("version"), b"2")
                meta = z.read("metadata").decode()
                names = sorted((n for n in z.namelist() if n.startswith("logic-1-")), key=lambda n: int(n[8:]))
                raw = b"".join(z.read(n) for n in names)
            self.assertIn("samplerate=25 MHz", meta)
            self.assertIn("unitsize=2", meta)
            self.assertIn("probe9=B0", meta)
            self.assertEqual(list(struct.unpack("<%dH" % (len(raw) // 2), raw)), self.want)

    def test_vcd_and_csv_hold_every_change(self):
        self.cap.save(self.path("a.vcd"))
        self.cap.save(self.path("a.csv"))
        ids, value, at, got = {}, 0, 0, []
        with open(self.path("a.vcd")) as f:
            lines = f.read().split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("$timescale"):
                self.assertEqual(line, "$timescale 10 ns $end")
            elif line.startswith("$var"):
                ids[line.split()[3]] = int(line.split()[4][1]) + (8 if line.split()[4][0] == "B" else 0)
            elif line.startswith("#"):
                t = int(line[1:]) // 4                  # 25 MS/s: 40 ns a sample
                got += [value] * (t - at)
                at = t
            elif line and line[0] in "01" and line[1:] in ids:
                value = value & ~(1 << ids[line[1:]]) | int(line[0]) << ids[line[1:]]
        self.assertEqual(got, self.want)
        with open(self.path("a.csv")) as f:
            rows = [r.split(",") for r in f.read().split("\n")[1:-1]]
        got = []
        for row, nxt in zip(rows, rows[1:] + [[str(len(self.want) / 25e6)]]):
            v = sum(int(b) << i for i, b in enumerate(row[1:]))
            got += [v] * (round(float(nxt[0]) * 25e6) - round(float(row[0]) * 25e6))
        self.assertEqual(got, self.want)

    def test_other_endings_are_refused(self):
        with self.assertRaises(ValueError):
            self.cap.save(self.path("a.txt"))

    def test_sigrok_session_comes_back_as_it_was(self):
        self.cap.names[3] = "SCL"
        for cap in (self.cap, Capture.from_words(W.words_of_runs(self.cap.values, self.cap.lengths, "raw"), "raw",
                                                 25e6, len(self.want))):
            cap.names[3] = "SCL"
            cap.save(self.path("a.sr"))
            back = Capture.load(self.path("a.sr"))
            self.assertEqual(samples_of(back.values, back.lengths), self.want)
            self.assertEqual((back.rate, back.samples, back.names[3], back.names[4]), (25e6, len(self.want), "SCL", "A4"))
            self.assertTrue(all(x != y for x, y in zip(back.values, back.values[1:])))      # no run in two pieces

    def test_sigrok_session_of_another_program(self):
        # one byte a sample, 12.5 kHz, eight probes of which two have names, and the samples in one file
        rng = random.Random(8)
        data = bytes(rng.choice((0, 0, 0, 1, 3, 0x80)) for _ in range(5001))
        meta = ("[global]\nsigrok version=0.5.1\n\n[device 1]\ncapturefile=logic-1\ntotal probes=8\n"
                "samplerate=12.5 kHz\nprobe1=D0\nprobe8=CS\nunitsize=1\n")
        with zipfile.ZipFile(self.path("b.sr"), "w") as z:
            z.writestr("version", "2")
            z.writestr("metadata", meta)
            z.writestr("logic-1", data)
        back = Capture.load(self.path("b.sr"))
        self.assertEqual((back.rate, back.samples, back.names[0], back.names[7], back.names[8]), (12500, 5001, "D0", "CS", "B0"))
        self.assertEqual(samples_of(back.values, back.lengths), list(data))
        with zipfile.ZipFile(self.path("c.sr"), "w") as z:
            z.writestr("metadata", meta)
        with self.assertRaisesRegex(ValueError, "no logic samples"):
            Capture.load(self.path("c.sr"))
        with open(self.path("d.txt"), "w") as f:
            f.write("not a capture")
        with self.assertRaisesRegex(ValueError, "d.txt is not a capture file: this software reads .lb16 files"):
            Capture.load(self.path("d.txt"))

    def test_an_lb16_file_holds_2_to_the_32_samples(self):
        long = Capture(200e6, array("H", [0, 1]), array("Q", [(1 << 32) - 1, 6]))           # it changes at 2^32 - 1
        long.save(self.path("e.lb16"))
        self.assertEqual(Capture.load(self.path("e.lb16")).samples, (1 << 32) + 5)
        longer = Capture(200e6, array("H", [0, 1]), array("Q", [1 << 32, 5]))                # and here at 2^32
        with self.assertRaisesRegex(C.TooLong, r"holds 2\^32 samples at most \(21.5 s at this rate\)"):
            longer.save(self.path("f.lb16"))

    def test_an_lb16_file_is_read_as_it_is(self):
        self.cap.save(self.path("a.lb16"))
        back = Capture.load(self.path("a.lb16"))
        back.summary()
        self.assertIsNone(back._runs)                               # its edges were enough: no runs were made
        self.assertEqual([(i, list(e)) for i, e in back.edges()], [(i, list(e)) for i, e in self.cap.edges()])
        self.assertEqual(samples_of(back.values, back.lengths), self.want)

    def test_what_is_not_a_capture_is_told_plainly(self):
        self.cap.save(self.path("a.lb16"))
        with open(self.path("a.lb16"), "rb") as f:
            good = f.read()
        n = struct.unpack_from("<I", good, 8)[0]
        head, tail = json.loads(good[12:12 + n]), good[12 + n:]

        def lb16(head, tail=b""):
            text = json.dumps(head).encode()
            text += b" " * (-(12 + len(text)) % 4)
            return C.MAGIC + struct.pack("<I", len(text)) + text + tail
        bad = {"empty.lb16": b"", "magic.lb16": C.MAGIC, "short.lb16": good[:-4], "head.lb16": good[:12 + n // 2],
               "json.lb16": C.MAGIC + struct.pack("<I", 4) + b"{{{{", "keys.lb16": lb16({"sampleRate": 1e6}),
               "rate.lb16": lb16(dict(head, sampleRate=0), tail), "past.lb16": lb16(dict(head, numSamples=3), tail),
               "level.lb16": lb16(dict(head, channels=[dict(c, initial=2) for c in head["channels"]]), tail),
               "zip.sr": b"PK\x03\x04 and then not a zip file", "text.lb16": b"just some words"}
        for name, data in bad.items():
            with open(self.path(name), "wb") as f:
                f.write(data)
            with self.assertRaises(ValueError) as caught:
                Capture.load(self.path(name))
            self.assertTrue(str(caught.exception).startswith(self.path(name) + " is not a capture file"),
                            (name, str(caught.exception)))
        with zipfile.ZipFile(self.path("other.sr"), "w") as z:
            z.writestr("readme.txt", "a zip file of something else")
        with self.assertRaisesRegex(ValueError, "other.sr is not a capture file: it is a zip file, and not a sigrok "
                                                "session that can be read"):
            Capture.load(self.path("other.sr"))
        with self.assertRaisesRegex(ValueError, "is a folder, not a capture file"):
            Capture.load(self.dir.name)
        for name in ("short.lb16", "other.sr"):     # the command line says so in one line, with no traceback
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["show", self.path(name)])
            self.assertEqual((code, err.getvalue().count("\n")), (1, 1), err.getvalue())
            self.assertIn("is not a capture file", err.getvalue())
        # a file with fewer than 16 channels: the others are low
        count = sum(c["edges"] for c in head["channels"][:3])
        with open(self.path("few.lb16"), "wb") as f:
            f.write(lb16(dict(head, channels=head["channels"][:3]), tail[:4 * count]))
        back = Capture.load(self.path("few.lb16"))
        self.assertEqual(samples_of(back.values, back.lengths), [v & 7 for v in self.want])

    def test_the_apps_files(self):
        # Until now the app wrote a capture's settings under info["meta"] (web/app.js), and its trigger sample in
        # the header's "trigger" (0 for none).  Both are read, as well as this software's own layout
        edges = self.cap.edges()
        meta = {"state": "done", "got": 1, "want": 1, "bytes": 1000, "rate": 25e6, "samples": len(self.want),
                "asked": 5000, "test": False, "thresholds": [1.2, 0.8], "overflow": True, "stopped": "timeout",
                "problems": ["one"], "usb": "480 Mbit/s", "words": 250, "trigger": "A1=f", "trigger_sample": 120,
                "clock": None, "glitch_ns": [0] * 16, "pi": None}
        for info, header, want in (({"meta": meta}, 120, 120), ({"meta": dict(meta, trigger_sample=None)}, 0, None),
                                   ({}, 77, 77), (None, 0, None)):
            head = {"sampleRate": 25e6, "numSamples": len(self.want), "trigger": header, "source": "Logic Boole 16",
                    "info": info, "gaps": [], "channels": [{"initial": i, "edges": len(e)} for i, e in edges],
                    "names": ["SDA", "SCL"] + [""] * 14, "analyzers": []}
            text = json.dumps(head).encode()
            text += b" " * (-(12 + len(text)) % 4)
            with open(self.path("app.lb16"), "wb") as f:
                f.write(C.MAGIC + struct.pack("<I", len(text)) + text)
                for _, e in edges:
                    f.write(struct.pack("<%dI" % len(e), *e))
            back = Capture.load(self.path("app.lb16"))
            self.assertEqual(samples_of(back.values, back.lengths), self.want)
            self.assertEqual((back.trigger, back.names[:3], back.info["source"]), (want, ["SDA", "SCL", "A2"],
                                                                                  "Logic Boole 16"))
            if info:
                self.assertEqual((back.thresholds, back.overflow, back.stopped, back.problems),
                                 ([1.2, 0.8], True, "timeout", ["one"]))
                self.assertEqual((back.info["trigger"], back.info["samples_asked"], back.info["usb"],
                                  back.info["words"]), ("A1=f", 5000, "480 Mbit/s", 250))
                self.assertNotIn("meta", back.info)
            else:
                self.assertEqual((back.thresholds, back.overflow, back.stopped, back.problems), (None, False, None, []))
        back.trigger = 120                  # saved again, it is in this software's layout, and reads back the same
        back.save(self.path("again.lb16"))
        again = Capture.load(self.path("again.lb16"))
        self.assertEqual((again.trigger, again.names[:2], samples_of(again.values, again.lengths)),
                         (120, ["SDA", "SCL"], self.want))

    def test_a_sigrok_session_keeps_what_it_has_no_place_for(self):
        cap = Capture(25e6, self.cap.values, self.cap.lengths, thresholds=(1.65, 0.9), trigger=1234, overflow=True,
                      problems=["x"], info={"usb": "5 Gbit/s", "trigger": "B0=r"})
        cap.save(self.path("t.sr"))
        with zipfile.ZipFile(self.path("t.sr")) as z:
            self.assertEqual(z.namelist(), ["version", "metadata", "logic-1-1", "logicboole"])
            kept = json.loads(z.read("logicboole"))
            parts = {n: z.read(n) for n in z.namelist()}
        self.assertEqual((kept["trigger_sample"], kept["thresholds"]), (1234, [1.65, 0.9]))
        back = Capture.load(self.path("t.sr"))
        self.assertEqual((back.trigger, back.thresholds, back.overflow, back.problems, back.info["trigger"]),
                         (1234, [1.65, 0.9], True, ["x"], "B0=r"))
        self.assertEqual(samples_of(back.values, back.lengths), self.want)
        # what is not this software's in that part is passed over
        for other in (b"not json", json.dumps({"trigger_sample": 10 ** 12, "thresholds": "high"}).encode()):
            with zipfile.ZipFile(self.path("u.sr"), "w") as z:
                for name, data in parts.items():
                    z.writestr(name, other if name == "logicboole" else data)
            back = Capture.load(self.path("u.sr"))
            self.assertEqual((back.trigger, back.thresholds, len(back.values)), (None, None, len(self.cap.values)))

    def test_csv_times_tell_each_sample_apart(self):
        # 10 s into a 200 MS/s capture, a time needs 11 figures to name its sample
        cap = Capture(200e6, array("H", [0, 1, 0]), array("Q", [2000000001, 1, 5]))
        cap.save(self.path("late.csv"))
        with open(self.path("late.csv")) as f:
            times = [row.split(",")[0] for row in f.read().split("\n")[1:-1]]
        self.assertEqual(times, ["0.0000000000", "10.0000000050", "10.0000000100"])
        self.assertEqual([round(float(t) * 200e6) for t in times], [0, 2000000001, 2000000002])


class Revisions(unittest.TestCase):
    """What differs between the board's revisions: the scale of its thresholds.  Each check is made for every
    revision the software knows, and for one made up for the tests."""

    def setUp(self):
        patched = unittest.mock.patch.dict(B.REVISIONS, {MADE_UP.name: MADE_UP})
        patched.start()
        self.addCleanup(patched.stop)

    def test_volts_and_duty_cycles(self):
        for name, rev in B.REVISIONS.items():
            with self.subTest(name):
                for v in (rev.low, 0.9, 1.65, rev.high):
                    self.assertLessEqual(abs(rev.volts(rev.duty_for(v)) - v), rev.step / 2 + 1e-12)
                self.assertTrue(rev.low < B.START_THRESHOLD < rev.high)
                self.assertGreaterEqual(rev.volts(4095), rev.high)          # the whole range can be set
                for v in (-0.1, rev.full_scale * 1.01):
                    with self.assertRaisesRegex(B.BoardError, "A threshold is 0 ... "):
                        rev.duty_for(v)
        self.assertEqual(B.REVISIONS["V1.0.0"].duty_for(B.START_THRESHOLD), 2048)    # where its designs start
        self.assertNotEqual(MADE_UP.duty_for(B.START_THRESHOLD), 2048)

    def test_names(self):
        self.assertIs(B.revision_of(), B.REVISIONS[B.DEFAULT_REVISION])
        for name in ("V1.0.0", "1.0.0", "v1.0.0", B.REVISIONS["V1.0.0"]):
            self.assertIs(B.revision_of(name), B.REVISIONS["V1.0.0"])
        with self.assertRaisesRegex(B.BoardError, "V9.9.9 is not a revision of the board this software knows: V1.0.0"):
            B.revision_of("V9.9.9")

    def test_a_board_starts_at_the_same_voltage_and_keeps_its_thresholds(self):
        for name, rev in B.REVISIONS.items():
            with self.subTest(name):
                bus = fake.FakeBus(instant=True)
                duties = lambda: struct.unpack("<HH", bus.reg[0x0C:0x10])          # noqa: E731
                with B.connect(backend=bus, revision=name) as board:
                    self.assertIs(board.revision, rev)
                    start = rev.duty_for(B.START_THRESHOLD)
                    self.assertEqual(duties(), (start, start))
                    for v in board.thresholds():
                        self.assertLessEqual(abs(v - B.START_THRESHOLD), rev.step)
                    a, b = board.set_thresholds(1.65, 0.9)
                    self.assertEqual(duties(), (rev.duty_for(1.65), rev.duty_for(0.9)))
                    self.assertEqual((round(a, 3), round(b, 3)), (round(rev.volts(rev.duty_for(1.65)), 3),
                                                                  round(rev.volts(rev.duty_for(0.9)), 3)))
                    board.capture_fast("A0-A3", seconds=1e-4)              # the other design is loaded ...
                    self.assertEqual(duties(), (rev.duty_for(1.65), rev.duty_for(0.9)))
                    cap = board.capture(rate=1e6, seconds=1e-3)             # ... and this one again
                    self.assertEqual(duties(), (rev.duty_for(1.65), rev.duty_for(0.9)))
                    self.assertEqual(cap.thresholds, (round(a, 3), round(b, 3)))
                    # a threshold outside what the board is made for is refused, and nothing changes
                    with self.assertRaises(B.BoardError) as caught:
                        board.set_thresholds(rev.high + 0.1)
                    self.assertEqual(str(caught.exception),
                                     "A threshold of %.3g V is outside what the board takes, %g V to %g V: set it to "
                                     "about half your logic voltage, for example 1.65 V for 3.3 V logic."
                                     % (rev.high + 0.1, rev.low, rev.high))
                    with self.assertRaisesRegex(B.BoardError, "outside what the board takes"):
                        board.set_thresholds(1.0, rev.low - 0.05)
                    self.assertEqual(duties(), (rev.duty_for(1.65), rev.duty_for(0.9)))
                    self.assertEqual(board.set_thresholds(rev.low, rev.high),     # the ends are taken
                                     (rev.volts(rev.duty_for(rev.low)), rev.volts(rev.duty_for(rev.high))))
                    board.set_thresholds(1.65, 0.9)
                    with self.assertRaisesRegex(B.BoardError, "A threshold is 0 ... %.2f V" % rev.volts(4095)):
                        rev.duty_for(rev.full_scale * 1.1)          # the outer check: what the PWM can make
                found = B.look(bus, revision=name)
                self.assertEqual((found["revision"], found["threshold_range"], found["threshold_a"]),
                                 (rev.name, [rev.low, rev.high], round(a, 3)))
                B.connect(backend=bus, revision=name, reload=True).close()  # "logicboole load": the design again
                self.assertEqual(duties(), (start, start))


class TheBoard(unittest.TestCase):
    def bus(self, **more):
        return fake.FakeBus(instant=True, **more)

    def test_states_from_plugging_in_to_ready(self):
        bus = self.bus(stage="none")
        self.assertEqual(B.look(bus)["state"], "none")
        with self.assertRaisesRegex(B.BoardError, "No board"):
            B.connect(backend=bus)
        bus.stage, bus.access = "boot", False
        self.assertEqual(B.look(bus)["state"], "noaccess")
        with unittest.mock.patch.object(B.sys, "platform", "linux"), self.assertRaisesRegex(B.BoardError, "udev"):
            B.connect(backend=bus)
        bus.access = True
        self.assertEqual(B.look(bus)["state"], "boot")
        with self.assertRaisesRegex(B.BoardError, "waits for its firmware"):
            B.connect(backend=bus, load=False)
        log = []
        board = B.connect(backend=bus, log=log.append)
        self.assertEqual(len(log), 2)
        found = B.look(bus)
        self.assertEqual((found["state"], found["firmware"], found["design"], found["threshold_a"]), ("ready", 2, 5, 1.564))
        self.assertEqual(found["serial"], "5354414E442D494E")       # the stand-in's
        # every section of the image went to the address it names, and was read back
        sections, entry = B.parse_image(fake.IMAGE)
        for address, data in sections:
            for i in range(0, len(data), 4096):
                self.assertEqual(bus.memory[address + i], data[i:i + 4096])
        self.assertIn((0xA0, entry & 0xFFFF, entry >> 16, 0), bus.requests)
        board.unload()
        board.close()
        self.assertEqual(B.look(bus)["state"], "boot")
        B.connect(backend=bus).close()                  # the FPGA kept its design: only the firmware is loaded
        self.assertNotIn(0xB3, [r[0] for r in bus.requests[-40:]])
        before = len(bus.requests)
        B.connect(backend=bus, reload=True).close()     # "logicboole load": both again
        codes = [r[0] for r in bus.requests[before:]]
        self.assertEqual((codes[0], 0xA0 in codes, 0xB3 in codes, 0xB5 in codes), (0xB7, True, True, True))

    def test_the_firmware_image_is_downloaded_once_checked_and_kept(self):
        image = fake.IMAGE
        with tempfile.TemporaryDirectory() as d, \
                unittest.mock.patch.object(B, "FIRMWARE", os.path.join(d, "beside.img")), \
                unittest.mock.patch.object(B, "FIRMWARE_SHA256", hashlib.sha256(image).hexdigest()), \
                unittest.mock.patch.dict(os.environ, {"LOGICBOOLE_CACHE": os.path.join(d, "kept")}):
            asked, log = [], []

            def download(url):
                asked.append(url)
                return image

            def no_connection(url):
                raise urllib.error.URLError("no route")

            path = B.firmware_image(log.append, download)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), image)
            self.assertEqual((asked, len(log), path), ([B.FIRMWARE_URL], 2, B.kept_firmware()))
            self.assertEqual(B.firmware_image(log.append, no_connection), path)     # kept: nothing is asked for
            with open(path, "wb") as f:                     # the kept file has become something else
                f.write(image[:-1])
            with self.assertRaisesRegex(B.BoardError, "is not firmware version 2 as this software knows it"):
                B.firmware_image(None, download)
            os.remove(path)
            with self.assertRaisesRegex(B.BoardError, "nothing was kept"):          # something else arrives
                B.firmware_image(None, lambda url: b"something else")
            self.assertEqual(os.listdir(os.path.dirname(path)) if os.path.isdir(os.path.dirname(path)) else [], [])
            with self.assertRaisesRegex(B.BoardError, "could not be downloaded .*no route.*save it as .*kept"):
                B.firmware_image(None, no_connection)
            with open(B.FIRMWARE, "wb") as f:               # an image beside the software is used as it is
                f.write(b"a build of your own")
            self.assertEqual(B.firmware_image(None, no_connection), B.FIRMWARE)

    def test_files_that_are_not_what_they_should_be(self):
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "x")
            image = fake.IMAGE
            for data, what in ((b"XX" + image[2:], "CY"), (image[:-4] + b"\0\0\0\0", "checksum"), (image[:900], "ends")):
                with open(bad, "wb") as f:
                    f.write(data)
                bus = self.bus()
                with self.assertRaisesRegex(B.BoardError, what):
                    B.connect(backend=bus, firmware=bad)
                self.assertEqual(bus.requests, [])      # the FX3 was not touched
            with open(bad, "wb") as f:
                f.write(b"not a bitstream" * 100)
            with self.assertRaisesRegex(B.BoardError, "bitstream"):
                B.connect(backend=self.bus(), gateware=bad)

    def test_thresholds(self):
        with B.connect(backend=self.bus()) as board:
            a, b = board.set_thresholds(1.65, 0.9)
            self.assertAlmostEqual(a, 1.65, delta=0.0004)
            self.assertAlmostEqual(b, 0.9, delta=0.0004)
            self.assertEqual(board.registers()["duty_a"], 2160)         # 1.65 V
            board.set_thresholds(b=3.0)
            self.assertAlmostEqual(board.thresholds()[0], 1.65, delta=0.0004)
            self.assertAlmostEqual(board.thresholds()[1], 3.0, delta=0.0004)
            with self.assertRaises(B.BoardError):
                board.set_thresholds(3.3)
            with self.assertRaises(B.BoardError):
                board.set_thresholds(-0.1)

    def test_rates(self):
        self.assertEqual(B.nearest_rate(200e6), (0, 200e6))
        self.assertEqual(B.nearest_rate(100e6), (1, 100e6))
        self.assertEqual(B.nearest_rate(30e6)[0], 6)                    # 28.6 MS/s is the nearest
        self.assertEqual(B.nearest_rate(1e6), (199, 1e6))
        self.assertEqual(B.nearest_rate(1e6, 100e6), (99, 1e6))         # a design whose fastest rate is 100 MS/s
        for bad in (400e6, 0, -1, 1.0):
            with self.assertRaises(B.BoardError):
                B.nearest_rate(bad)

    def test_a_capture_of_the_probes(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            before = len(bus.requests)
            cap = board.capture(rate=2e6, seconds=0.05, a=1.2)
            codes = [r[0] for r in bus.requests[before:] if r[0] >= 0xC0 and r[0] < 0xD0]
            self.assertEqual(codes, [0xC0, 0xC3, 0xC2, 0xC4, 0xC4, 0xC5, 0xC3])     # the order the firmware was tested with
            self.assertEqual([r[1] for r in bus.requests[before:] if r[0] == 0xC4], [1, 0])
            self.assertEqual((cap.ok, cap.samples, cap.rate, cap.overflow, cap.stopped), (True, 100000, 2e6, False, None))
            self.assertAlmostEqual(cap.thresholds[0], 1.2, delta=0.001)
            want = samples_of(*zip(*fake.probe_runs(0, 100000, 2e6)))
            self.assertEqual(samples_of(cap.values, cap.lengths), want)
            raw = board.capture(rate=2e6, samples=99999, fmt="raw")
            self.assertEqual((raw.ok, raw.samples), (True, 99999))
            self.assertEqual(samples_of(raw.values, raw.lengths), want[:99999])
            self.assertEqual(B.look(bus)["state"], "ready")

    def test_the_test_pattern_in_both_formats(self):
        with B.connect(backend=self.bus()) as board:
            for fmt, rate, shift, samples in (("rle", 200e6, 8, 3000001), ("raw", 200e6, 0, 300001), ("rle", 50e6, 0, 200000),
                                              ("raw", 200e6 / 3, 3, 70000), ("rle", 1e6, 15, 100000), ("raw", 1e6, 4, 1)):
                cap = board.capture(rate=rate, samples=samples, fmt=fmt, test=shift)
                self.assertEqual((cap.problems, cap.samples, cap.rate), ([], samples, rate), (fmt, shift))

    def test_a_trigger_on_the_probes(self):
        rate, n = 2e6, 30000
        probes = samples_of(*zip(*fake.probe_runs(0, 400000, rate)))
        with B.connect(backend=self.bus()) as board:
            for text in ("A1=f", "A0=r", "A0=e A1=1", "A0=0 A1=0", "A1=f then A0=r", "A1=0 for 3us", "A1=1 for 12us",
                         "A0=0 pulse 20us..", "A1=1 pulse ..5us then A0=0 pulse 8us..12us",
                         "A1=f then A1=r then A1=f then A0=f", "A0=0 for 4 then A0=1 for 30us then A0=f A1=1"):
                stages = T.parse(text, rate)
                want = trigger_sample(probes, stages)
                self.assertIsNotNone(want, text)
                for fmt in ("rle", "raw"):
                    for before in (0, 7, 33, 100000):
                        cap = board.capture(rate=rate, samples=n, fmt=fmt, trigger=text, samples_before=before)
                        kept = min(want, before + (fmt == "raw" and (want - before) % 2))
                        self.assertEqual((cap.problems, cap.trigger, cap.samples, cap.stopped), ([], kept, kept + n, None),
                                         (text, fmt, before, want))
                        self.assertEqual(samples_of(cap.values, cap.lengths), probes[want - kept:want + n], (text, fmt, before))
                        self.assertEqual(cap.info["trigger"], T.describe(stages, rate))
            cap = board.capture(rate=rate, seconds=0.01, trigger="A1=f", before=1e-6)       # times in place of numbers
            self.assertEqual((cap.trigger, cap.samples), (2, 20002))
            cap = board.capture(rate=rate, seconds=0.01, trigger=[{"fall": 2}])             # a tenth of the length
            first = trigger_sample(probes, T.stages_of({"fall": 2}))
            self.assertEqual((cap.trigger, cap.samples), (min(first, 2000), min(first, 2000) + 20000))

    def test_a_trigger_that_does_not_come(self):
        with B.connect(backend=self.bus()) as board:
            waited = []
            cap = board.capture(rate=1e6, samples=5000, trigger="B3=r", samples_before=700, timeout=0.05,
                                progress=waited.append)
            self.assertEqual((cap.ok, cap.trigger, cap.stopped, cap.samples, cap.overflow), (True, None, "timeout", 700, False))
            self.assertEqual(set(waited), {None})           # it waited, and said so, all the time
            cap = board.capture(rate=1e6, samples=5000, fmt="raw", trigger="in=r", samples_before=701, timeout=0.05)
            self.assertEqual((cap.ok, cap.trigger, cap.stopped), (True, None, "timeout"))
            self.assertIn(cap.samples, (701, 702))
            polls = []
            cap = board.capture(rate=1e6, samples=5000, trigger="A0=1 for 1s", cancel=lambda: polls.append(1) or len(polls) > 3)
            self.assertEqual((cap.ok, cap.trigger, cap.stopped, cap.samples), (True, None, "stopped", 500))
            for text in ("B9=r", "A0=1 then A1=1 then A0=0 then A1=0 then A0=1"):
                with self.assertRaises(T.TriggerError):
                    board.capture(trigger=text)
            with self.assertRaisesRegex(B.BoardError, "less than nothing"):
                board.capture(trigger="A0=r", samples_before=-1)
            self.assertTrue(board.capture(rate=1e6, samples=100).ok)        # and the next capture has no trigger
            self.assertIsNone(board.capture(rate=1e6, samples=100).trigger)

    def test_a_trigger_on_the_test_pattern(self):
        with B.connect(backend=self.bus()) as board:
            for fmt, shift, value, before in (("rle", 8, 0x0123, 1000), ("raw", 0, 0x4567, 1000), ("rle", 0, 0x4567, 5),
                                              ("raw", 3, 0x0200, 10 ** 6), ("rle", 3, 0x0200, 10 ** 6)):
                cap = board.capture(rate=100e6, samples=50000, fmt=fmt, test=shift, samples_before=before,
                                    trigger={"mask": 0xFFFF, "value": value})
                self.assertEqual((cap.problems, cap.samples - cap.trigger), ([], 50000), (fmt, shift))
                first = W.pattern_origin(cap.words, fmt, shift)
                self.assertEqual(first + cap.trigger, value << shift, (fmt, shift))
                self.assertLessEqual(cap.trigger, before + 1)
        bus = self.bus()
        with B.connect(backend=bus) as board:               # a changed word is found before and after the trigger
            for word in (20, 3000):
                bus.flip_word = word
                cap = board.capture(rate=100e6, samples=50000, test=4, samples_before=1000,
                                    trigger={"mask": 0xFFFF, "value": 0x0400})
                self.assertEqual(len(cap.problems), 1, cap.problems)
            bus.flip_word = None
        with B.connect(backend=self.bus(overflow_at=12345)) as board:
            cap = board.capture(rate=100e6, samples=50000, test=0, samples_before=100, trigger={"mask": 0xFFFF, "value": 0x1000})
            self.assertEqual((cap.ok, cap.overflow, cap.trigger, cap.samples), (True, True, 100, 12445))

    def test_check_the_first_thing_to_run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--fake", "check"]), 0)
        lines = out.getvalue().splitlines()
        self.assertIn("Loaded: firmware version 2, FPGA design version 5, serial number 5354414E442D494E.", lines)
        self.assertEqual(len([line for line in lines if line.endswith("every word right.")]), 2)
        self.assertEqual(lines[-1], 'The board is ready.  "logicboole" opens the app.')
        for bus, says in ((fake.FakeBus(stage="none"), "The board: not on USB."),
                          (fake.FakeBus(access=False), "The board: The board is on USB, but")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
                self.assertEqual(cli.main(["--fake", "check"]), 1)
            self.assertIn(says, out.getvalue())
            self.assertNotIn("ready", out.getvalue())
        bus = fake.FakeBus(instant=True)                # a USB 2.0 link: it passes, and says what that means
        bus.link = 2
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            self.assertEqual(cli.main(["--fake", "check"]), 0)
        self.assertIn("USB: 480 Mbit/s.  The board works on this link", out.getvalue())

    def test_unload_says_the_board_is_back_when_it_is(self):
        bus = fake.FakeBus(instant=True)
        B.connect(backend=bus, log=lambda line: None).close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            self.assertEqual(cli.main(["--fake", "unload"]), 0)
        self.assertEqual(out.getvalue(), "The board is back in its boot loader.\n")
        self.assertEqual(B.look(bus)["state"], "boot")
        B.connect(backend=bus, log=lambda line: None).close()
        opens = bus.open_device                                     # a board that does not come back
        bus.open_device = lambda vid, pid, wait=0.0: None if pid == B.PID_BOOT else opens(vid, pid, wait)
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            self.assertNotEqual(cli.main(["--fake", "unload"]), 0)
        self.assertIn("did not return to its boot loader", err.getvalue())

    def test_a_serial_number_if_the_firmware_gives_one(self):
        bus = fake.FakeBus(instant=True, serial="1A2B3C4D5E6F7081")
        self.assertEqual((B.look(bus)["state"], "serial" in B.look(bus)), ("boot", False))     # the boot loader has none
        B.connect(backend=bus, log=lambda line: None).close()
        found = B.look(bus)
        self.assertEqual((found["state"], found["serial"]), ("ready", "1A2B3C4D5E6F7081"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out), unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            self.assertEqual(cli.main(["--fake", "info"]), 0)
        self.assertIn("  serial number 1A2B3C4D5E6F7081\n", out.getvalue())
        bus = fake.FakeBus(instant=True, serial=None)               # a chip that gives no number
        B.connect(backend=bus, log=lambda line: None).close()
        self.assertEqual((B.look(bus)["state"], "serial" in B.look(bus)), ("ready", False))

    def test_a_capture_clock(self):
        fine = list(fake.probe_runs(0, 40000000, B.FASTEST))        # the probes every 5 ns
        def clocked(bit, rise, fall):
            out = []
            for (before, _), (after, _) in zip(fine, fine[1:]):
                if (before ^ after) & bit and (rise if after & bit else fall):
                    out.append(before)                              # what the inputs were before the edge
            return out
        with B.connect(backend=self.bus()) as board:
            for clock, bit, rise, fall in (("A1=r", 2, 1, 0), ("A1=f", 2, 0, 1), ("A1", 2, 1, 0), ("A0=e", 1, 1, 1)):
                want = clocked(bit, rise, fall)
                for fmt in ("rle", "raw"):
                    cap = board.capture(samples=150, clock=clock, fmt=fmt)
                    self.assertEqual((cap.problems, cap.samples), ([], 150), (clock, fmt))
                    self.assertEqual(samples_of(cap.values, cap.lengths), want[:150], (clock, fmt))
                    self.assertEqual(cap.info["clock"], clock if "=" in clock else clock + "=r")
            want = clocked(2, 1, 0)                                 # SDA at SCL's rising edges: the bus's bits
            stages = T.parse("A0=0 for 3 then A0=1")
            at = trigger_sample(want, stages)
            cap = board.capture(samples=100, clock="A1=r", trigger="A0=0 for 3 then A0=1", samples_before=4)
            self.assertEqual((cap.problems, cap.trigger, cap.samples), ([], 4, 104))
            self.assertEqual(samples_of(cap.values, cap.lengths), want[at - 4:at + 100])
            for wrong in (dict(clock="A1=r"), dict(clock="A1=r", samples=10, before=1e-3), dict(clock="A1=r", samples=10,
                          trigger="A0=1 for 1us")):
                with self.assertRaises((B.BoardError, T.TriggerError), msg=wrong):
                    board.capture(**wrong)
            cap = board.capture(samples=10, clock="B5=r", timeout=0.05)        # an input with no clock on it
            self.assertEqual((cap.ok, cap.samples, cap.stopped), (True, 0, "timeout"))

    def test_the_glitch_filter_and_an_older_design(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            cap = board.capture(rate=1e6, samples=100, glitch={"A0": 50e-9, "B": 1e-6})
            self.assertEqual(cap.info["glitch_ns"], [50] + [0] * 7 + [1000] * 8)
            self.assertEqual(list(bus.reg[0x80:0x90]), [10] + [0] * 7 + [200] * 8)
            cap = board.capture(rate=1e6, samples=100)              # and off again
            self.assertEqual((list(bus.reg[0x80:0x90]), "glitch_ns" in cap.info), ([0] * 16, False))
            with self.assertRaises(T.TriggerError):
                board.capture(glitch=2e-6)
            r = board.registers()
            self.assertEqual((r["version"], r["stages"], r["waiting"], r["triggered"]), (5, 4, 0, 0))
            real = bus.registers
            def older():                                            # design version 3: no register 2D and after
                r = bytearray(real())
                r[3] = 3
                r[0x2D:] = bytes(len(r) - 0x2D)
                return bytes(r)
            bus.registers = older
            self.assertTrue(board.capture(rate=1e6, samples=100).ok)
            for more in (dict(trigger="A0=r"), dict(clock="A1=r", samples=5), dict(glitch=20e-9)):
                with self.assertRaisesRegex(B.BoardError, "version 3.*has no trigger"):
                    board.capture(rate=1e6, **more)

    def test_the_pis_lines_in_place_of_probes(self):
        asked = (None, "", "off", "i2c", "uart", "spi", "all", "sda", "GPIO3", "i2c,uart", "scl gpio14", ["ce1", "i2c"])
        self.assertEqual([T.pi_lines(x) for x in asked],
                         [0, 0, 0, 0x003, 0x00C, 0x1F0, 0x1FF, 0x001, 0x002, 0x00F, 0x006, 0x103])
        with self.assertRaisesRegex(T.TriggerError, "gpio4 is not one of the Pi's lines"):
            T.pi_lines("i2c,gpio4")
        bus = self.bus()
        with B.connect(backend=bus) as board:
            changes = lambda cap: [c["changes"] > 0 for c in cap.summary()[:4]]         # noqa: E731
            cap = board.capture(rate=1e6, seconds=0.02)             # the stand-in's I2C bus is on the probes A0, A1
            self.assertEqual((changes(cap), "pi" in cap.info, cap.names[:2]), ([True, True, False, False], False, ["A0", "A1"]))
            cap = board.capture(rate=1e6, seconds=0.02, pi="i2c")   # the Pi's lines there: the stand-in's read low
            self.assertEqual((cap.ok, changes(cap), bytes(bus.reg[0x2E:0x30])), (True, [False] * 4, b"\x03\x00"))
            self.assertEqual((cap.info["pi"], cap.names[:3]), ({"A0": "GPIO2 (SDA)", "A1": "GPIO3 (SCL)"}, ["SDA", "SCL", "A2"]))
            self.assertEqual(board.inputs() & 3, 0)
            cap = board.capture(rate=1e6, seconds=0.02, pi=["uart", "ce1"])     # other lines: A0, A1 are the probes
            self.assertEqual((changes(cap), bytes(bus.reg[0x2E:0x30])), ([True, True, False, False], b"\x0C\x01"))
            self.assertEqual((cap.names[2], cap.names[3], cap.names[8], cap.info["pi"]["B0"]), ("TXD", "RXD", "CE1", "GPIO7 (CE1)"))
            cap = board.capture(rate=1e6, seconds=0.02, pi="sda", trigger="A1=f")       # SCL, a probe, still triggers
            self.assertEqual((cap.trigger is not None, changes(cap)[:2]), (True, [False, True]))
            cap = board.capture(rate=1e6, seconds=0.02)             # and off again
            self.assertEqual((bytes(bus.reg[0x2E:0x30]), "pi" in cap.info, changes(cap)[:2]), (b"\x00\x00", False, [True, True]))
            self.assertEqual(board.inputs() & 3, 3)
            real = bus.registers
            def older():                                            # design version 4: no registers 2E, 2F
                r = bytearray(real())
                r[3] = 4
                r[0x2E:0x30] = bytes(2)
                return bytes(r)
            bus.registers = older
            self.assertTrue(board.capture(rate=1e6, samples=100, trigger="A0=f").ok)
            with self.assertRaisesRegex(B.BoardError, "version 4.*does not take the Pi's lines"):
                board.capture(rate=1e6, samples=100, pi="i2c")

    def test_overflow_ends_a_capture_early_and_whole(self):
        for fmt in ("rle", "raw"):
            with B.connect(backend=self.bus(overflow_at=123457)) as board:
                cap = board.capture(rate=100e6, seconds=0.01, fmt=fmt, test=0)
                self.assertEqual((cap.ok, cap.overflow), (True, True))
                self.assertIn(cap.samples, (123457, 123458))

    def test_the_computer_ends_a_capture(self):
        with B.connect(backend=self.bus()) as board:
            polls = []
            cap = board.capture(rate=100e6, samples=0, test=8, cancel=lambda: polls.append(1) or len(polls) > 3)
            self.assertEqual((cap.ok, cap.stopped, cap.overflow), (True, "stopped", False))
            self.assertGreater(cap.samples, 0)
            cap = board.capture(rate=100e6, seconds=10, test=0, max_bytes=1 << 20)
            self.assertEqual((cap.ok, cap.stopped), (True, "size"))
            self.assertLess(cap.samples, 1e9)

    def test_lost_and_changed_words_are_found(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            bus.drop_word = 1000
            cap = board.capture(rate=100e6, samples=2000000, test=4)
            self.assertFalse(cap.ok)
            self.assertIn("words arrived, the FPGA sent", cap.problems[0])
            bus.drop_word, bus.flip_word = None, 1000
            cap = board.capture(rate=100e6, samples=2000000, test=4)
            self.assertEqual(len(cap.problems), 1)
            self.assertIn("word 1000 is", cap.problems[0])
            bus.flip_word = None
            self.assertTrue(board.capture(rate=100e6, samples=2000000, test=4).ok)

    def test_a_board_in_use_is_left_alone(self):
        # another program is capturing, and still has the board: it is left alone, and what is said names the
        # way out
        bus = self.bus()
        with B.connect(backend=bus) as board:
            bus.leave_running(held=True)
            found = B.look(bus)
            self.assertEqual(found["state"], "inuse")
            self.assertEqual(found["text"], B.IN_USE)
            self.assertIn('"logicboole load" takes the board back from it', found["text"])
            self.assertIn("unplugging the board and plugging it in again", found["text"])
            with self.assertRaisesRegex(B.BoardError, "^Another program is capturing from the board.*logicboole load"):
                board.capture()
            with self.assertRaisesRegex(B.BoardError, "^Another program is capturing"):
                board.capture_fast("A")
            with self.assertRaisesRegex(B.BoardError, "^Another program is capturing"):
                board.use("fast")
        loads = len([r for r in bus.requests if r[0] in (0xB3, 0xB7)])
        lines = []
        with B.connect(backend=bus, log=lines.append) as board:    # connect() does not refuse, nor load anything
            self.assertEqual(board.inputs() & 3, bus.registers()[6] & 3)
        self.assertEqual(lines, [])
        self.assertEqual(len([r for r in bus.requests if r[0] in (0xB3, 0xB7)]), loads)
        self.assertTrue(bus.run and bus.stream and bus.claimed)
        # "logicboole load" takes the board back
        with B.connect(backend=bus, reload=True) as board:
            self.assertFalse(bus.run or bus.stream or bus.claimed)
            self.assertTrue(board.capture(rate=10e6, seconds=1e-3).ok)
        # a program that has the board open, and is not capturing
        bus.claimed = True
        with B.connect(backend=bus) as board:
            with self.assertRaisesRegex(B.BoardError, "^Another program has the board open.*plugging it in again"):
                board.capture()
        self.assertTrue(bus.claimed)                                # (its hold was not let go)

    def test_a_capture_left_running_is_stopped(self):
        # a program that has ended left a capture running: the system has let go of the board, so the capture
        # is stopped, with one line, wherever the board is next used
        bus, lines = self.bus(), []
        with B.connect(backend=bus) as board:
            bus.leave_running()
            found = B.look(bus, log=lines.append)
            self.assertEqual((found["state"], found["note"]), ("ready", B.STALE))
            self.assertEqual(lines, [B.STALE])
            self.assertFalse(bus.run or bus.stream or bus.claimed)
            self.assertEqual(B.STALE, "A capture was left running by a program that has ended; stopped it.")
            bus.leave_running()
            self.assertTrue(board.capture(rate=10e6, seconds=1e-3).ok)
            bus.leave_running()
            self.assertTrue(board.capture_fast("A", seconds=1e-5).ok)
            bus.leave_running()
            board.use("standard")
            self.assertFalse(bus.run or bus.stream or bus.claimed)
        bus.leave_running()
        lines = []
        with B.connect(backend=bus, log=lines.append) as board:
            self.assertEqual(lines, [B.STALE])
            bus.leave_running()
            self.assertTrue(board.capture(rate=10e6, seconds=1e-3).ok)
        self.assertEqual(lines, [B.STALE] * 2)
        self.assertNotIn(0xB7, [r[0] for r in bus.requests])      # nothing was loaded again for it

    def test_selftest(self):
        lines = []
        with B.connect(backend=fake.FakeBus(instant=True, overflow_at=None)) as board:
            self.assertTrue(selftest(board, log=lines.append, scale=0.002))
        self.assertEqual(len(lines), 34)
        self.assertTrue(all("every word right" in line for line in lines), lines)
        self.assertEqual([line for line in lines if "OVERFLOW" in line], [])
        self.assertEqual(len([line for line in lines if "the trigger sample is the right one" in line]), 7)
        self.assertEqual(len([line for line in lines if "the wait was ended" in line]), 1)
        # a board that overflows early: the captures are still whole up to there, which passes, except the two
        # that are small enough for the board's own buffers
        lines = []
        with B.connect(backend=fake.FakeBus(instant=True, overflow_at=10000)) as board:
            self.assertFalse(selftest(board, log=lines.append, scale=0.002))
        self.assertEqual(len([line for line in lines if "WRONG: it overflowed" in line]), 2)
        self.assertEqual(len([line for line in lines if "as meant" in line]), 3)
        # an overflow after a trigger is the computer's (or the link's), as in the plain captures: not a fault
        self.assertEqual(len([line for line in lines if "the right one" in line
                              and line.endswith("; OVERFLOW after the trigger")]), 7)
        self.assertEqual([line for line in lines if "WRONG" in line and "it overflowed" not in line], [])
        # the same on a USB 2.0 link: the captures that make more words than it carries may overflow, with or
        # without a trigger, and the last line says how many did
        lines, bus = [], fake.FakeBus(instant=True, overflow_at=10000)
        bus.link = 2
        with B.connect(backend=bus) as board:
            self.assertFalse(selftest(board, log=lines.append, scale=0.002))
        self.assertEqual(len([line for line in lines if line.endswith("; OVERFLOW after the trigger")]), 4)
        self.assertEqual(len([line for line in lines if "the right one" in line and "more than USB 2.0" in line]), 3)
        self.assertIn("The link is USB 2.0 (480 Mbit/s): ", lines[-1])
        lines, bus = [], fake.FakeBus(instant=True)
        bus.link = 2
        with B.connect(backend=bus) as board:
            self.assertTrue(selftest(board, log=lines.append, scale=0.002))
        self.assertIn(": 0 of the captures", lines[-1])

    def test_the_end_of_the_data_however_it_arrives(self):
        # found on a board: 4 s raw at 100 MS/s is 800,000,000 bytes, which end on a full packet.  The empty
        # packet after them then ends the read that holds the last data, and no read of 0 bytes follows
        for fmt, samples, how in (("raw", 25600, "three buffers and two full packets"),
                                  ("rle", 12800, "the same in the other format"),
                                  ("raw", 32768, "whole buffers and nothing more"),
                                  ("raw", 524288, "one read of the computer's, exactly full"),
                                  ("raw", 1048576 + 512, "a full read, then one that ends on a full packet"),
                                  ("raw", 25602, "a last packet of 4 bytes"),
                                  ("raw", 1, "one sample")):
            with B.connect(backend=self.bus()) as board:
                cap = board.capture(rate=10e6, samples=samples, fmt=fmt, test=0)
            self.assertTrue(cap.ok, "%s: %s" % (how, cap.problems))
            self.assertEqual(cap.samples, samples, how)

    def test_which_reads_an_empty_packet_ended(self):
        ended = usb.ended_by_empty_packet
        self.assertTrue(ended(0, 1 << 20, 1024))                # the empty packet by itself
        self.assertTrue(ended(2048, 1 << 20, 1024))             # after data that ended on a full packet
        self.assertFalse(ended(2052, 1 << 20, 1024))            # a short packet ended this one: the empty one follows
        self.assertFalse(ended(1 << 20, 1 << 20, 1024))         # a full read says nothing
        self.assertTrue(ended(512, 1 << 20, 512))               # USB 2.0: packets of 512 bytes
        self.assertFalse(ended(512, 1 << 20, 1024))

    def test_versions_that_differ_are_loaded_again(self):
        # a board that stayed powered while the software was upgraded runs the versions it had
        bus = self.bus()
        B.connect(backend=bus).close()
        bus.firmware_version = 1
        lines = []
        with B.connect(backend=bus, log=lines.append) as board:
            self.assertEqual(board.status()["version"], B.FIRMWARE_VERSION)
        self.assertEqual(lines[0], "The board runs firmware version 1; this software comes with version 2: loading it.")
        firmware = len([r for r in bus.requests if r[0] == 0xB7])
        bus.versions["standard"] = 4
        lines = []
        with B.connect(backend=bus, log=lines.append) as board:
            self.assertEqual(board.registers()["version"], 5)
        self.assertEqual(lines[0], "The board's FPGA runs version 4 of the standard design; this software comes with "
                                   "version 5: loading the standard design.")
        self.assertEqual(len([r for r in bus.requests if r[0] == 0xB7]), firmware)     # only the FPGA was loaded
        with B.connect(backend=bus) as board:
            board.use("fast")
        bus.versions["fast"] = 0
        lines = []
        with B.connect(backend=bus, log=lines.append) as board:
            self.assertEqual(board.registers()["design"], "standard")
        self.assertIn("runs version 0 of the fast design; this software comes with version 1", lines[0])
        loads, lines = len([r for r in bus.requests if r[0] in (0xB3, 0xB7)]), []
        B.connect(backend=bus, log=lines.append).close()            # the same versions: nothing is loaded
        self.assertEqual((lines, len([r for r in bus.requests if r[0] in (0xB3, 0xB7)])), ([], loads))
        # a bitstream of one's own is loaded as it is, and its version is not compared
        with tempfile.TemporaryDirectory() as folder:
            own = os.path.join(folder, "own.bin")
            with open(B.GATEWARE, "rb") as f, open(own, "wb") as g:
                g.write(f.read() + b"built here")
            bus.versions["standard"] = 4
            B.connect(backend=bus, log=lines.append, gateware=own).close()
            self.assertEqual((lines, bus.versions["standard"]), ([], 4))
        # and nothing is loaded while another program is capturing from the board
        bus.firmware_version = 1
        bus.leave_running(held=True)
        B.connect(backend=bus, log=lines.append).close()
        self.assertEqual((lines, bus.firmware_version, bus.versions["standard"], bus.run), ([], 1, 4, True))

    def test_usb_failures_are_told_plainly(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            bus.fail[0xB0] = -7                                     # the status request times out
            with self.assertRaisesRegex(B.BoardError, r"^The board did not answer when asked for its status "
                                                      r"\(libusb -7\)\.  Unplug it and plug it in again\.$"):
                board.status()
            self.assertEqual(B.look(bus)["state"], "other")
            bus.fail[0xB0] = -9                                     # it does not know the request: other firmware
            with self.assertRaisesRegex(B.BoardError, "^A device with the board's USB IDs runs other firmware"):
                board.status()
            del bus.fail[0xB0]
            bus.fail[0xD0] = -9
            with self.assertRaisesRegex(B.BoardError, r"^The firmware refused writing the FPGA's register 0C "
                                                      r"\(request D0, libusb -9\)\.$"):
                board.set_thresholds(1.0)
            del bus.fail[0xD0]
            bus.stage = "none"                                      # unplugged
            for what in (board.status, board.inputs, lambda: board.set_thresholds(1.0), board.capture,
                         lambda: board.capture_fast("A")):
                with self.assertRaises(B.Gone) as caught:
                    what()
                self.assertEqual(str(caught.exception), "The board left USB (unplugged, the cable, or a hub).  Connect "
                                                        "it again and run the command again: the board is loaded again "
                                                        "by itself.")
        self.assertEqual(B.look(bus)["state"], "none")

    def test_a_board_that_leaves_usb_during_a_capture(self):
        # what arrived is kept, whole up to its last sample, and the problems say what happened
        bus = self.bus()
        with B.connect(backend=bus) as board:
            bus.leave_after = 300000
            cap = board.capture(rate=10e6, seconds=1, test=4)
            self.assertEqual(cap.problems, ["the board left USB after 1048576 bytes: what arrived is kept, but could "
                                            "not be checked against what the FPGA sent"])
            start = round(board.volts(board.duty_for(B.START_THRESHOLD)), 3)
            self.assertEqual((cap.samples, cap.thresholds), (262144 * 16, (start, start)))
            self.assertIsNone(W.check_pattern(cap.words, "rle", 4, cap.samples))
        self.assertEqual(B.look(bus)["state"], "none")
        bus = self.bus()
        with B.connect(backend=bus) as board:
            board.use("fast")
            bus.leave_after = 300000
            cap = board.capture_fast("all", seconds=0.05, fmt="raw")
            self.assertEqual(cap.problems, [B.GONE_PROBLEM % 1048576])
            self.assertEqual(cap.samples, 262144 * 2 * F.mode_of("all").per)
        # with a trigger, nothing but the board says where the trigger sample is: nothing is kept
        bus = self.bus()
        with B.connect(backend=bus) as board:
            bus.leave_after = 300000
            with self.assertRaises(B.Gone) as caught:
                board.capture(rate=10e6, seconds=1, fmt="raw", trigger="A1=f")
        self.assertIn("during the capture, after 1048576 bytes had arrived: a capture with a trigger is kept only if "
                      "the board can say where its trigger sample is", str(caught.exception))
        # a read that fails while the board stays: what arrived before it is kept, and the problems say so
        bus = self.bus()
        with B.connect(backend=bus) as board:
            bus.read_fails_after = 300000
            cap = board.capture(rate=10e6, seconds=1, test=4)
            self.assertEqual(cap.problems[0], "reading from the board failed after 1048576 bytes (error)")
            self.assertIn("262144 words arrived, the FPGA sent", cap.problems[1])
            self.assertIsNone(W.check_pattern(cap.words, "rle", 4, cap.samples))
            self.assertFalse(bus.run or bus.stream or bus.claimed)
            bus.read_fails_after = None
            self.assertTrue(board.capture(rate=10e6, seconds=0.01, test=4).ok)     # and the next one is whole

    def test_a_capture_is_held_to_the_computers_memory(self):
        # a 32nd of the memory, and 16 MB at the least: as the fast design's captures are
        with unittest.mock.patch.object(B, "memory", return_value=256 << 20):
            with B.connect(backend=self.bus()) as board:
                cap = board.capture(rate=200e6, seconds=0.1, fmt="raw")
        self.assertEqual(cap.stopped, "size")
        self.assertTrue(cap.ok, cap.problems)
        self.assertTrue(16 << 20 <= cap.info["words"] * 4 < 40 << 20, cap.info["words"])

    def test_ctrl_c_inside_libusbs_callback_is_not_lost(self):
        # Python prints a Ctrl-C that comes while libusb runs a ctypes callback, and goes on as if none had come
        class Lib:
            def libusb_submit_transfer(self, tp):
                return 0

            def libusb_handle_events_timeout_completed(self, *args):
                pass

        def sink(view):
            raise KeyboardInterrupt
        r = usb.Reader.__new__(usb.Reader)
        r.lib, r.ctx, r._tv, r.sink, r.packet = Lib(), None, usb._Timeval(), sink, 512
        r.pending, r.bytes, r.short_reads, r.failed, r.failure, r.sink_error = 1, 0, 0, 0, None, None
        r.ending = r.ended = r.stopping = r.interrupted = False
        buf = (ctypes.c_ubyte * 1024)()
        t = usb._Transfer(status=usb.COMPLETED, length=1024, actual_length=1024, buffer=ctypes.addressof(buf))
        usb._CALLBACK(r._done)(ctypes.pointer(t))                   # through ctypes, as libusb calls it
        self.assertTrue(r.interrupted)
        with self.assertRaises(KeyboardInterrupt):
            r.pump(1)
        self.assertFalse(r.interrupted)
        r.pump(1)

    def test_windows_is_told_plainly(self):
        with unittest.mock.patch.object(usb, "_lib", None), unittest.mock.patch.object(usb.sys, "platform", "win32"):
            with self.assertRaisesRegex(usb.UsbError, "^Windows is not supported yet"):
                usb.library()


class TheApp(unittest.TestCase):
    def setUp(self):
        self.app = server.App(fake.FakeBus(instant=True), fake=True)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.handler(self.app))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return r.read()

    def post(self, path, header=True, data=b""):
        req = urllib.request.Request(self.url + path, data=data, headers={"X-LB16": "1"} if header else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def wait(self, what):
        for _ in range(400):
            s = json.loads(self.get("/api/status"))
            if what(s):
                return s
            threading.Event().wait(0.01)
        self.fail("the app did not get there: %s" % s)

    def test_a_plugged_in_board_is_loaded_and_captures(self):
        self.assertIn(json.loads(self.get("/api/status"))["board"]["state"], ("loading", "ready"))
        s = self.wait(lambda s: s["board"]["state"] == "ready")
        self.assertEqual(s["board"]["threshold_a"], 1.564)
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.08", header=False)[0], 403)
        self.assertEqual(self.post("/api/capture?rate=1000000")[0], 400)
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.08&a=9")[0], 400)
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.08&test=0&a=1.65&b=0.9")[0], 202)
        c = self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        self.assertEqual((c["state"], c["samples"], c["rate"], c["problems"], c["overflow"]), ("done", 80000, 1e6, [], False))
        self.assertEqual([round(v, 2) for v in c["thresholds"]], [1.65, 0.9])
        data = self.get("/api/data")
        self.assertEqual(len(data), c["bytes"])
        self.assertEqual(samples_of(*W.runs_of(data, "rle")), samples_of(*zip(*fake.probe_runs(0, 80000, 1e6))))
        self.assertEqual(self.post("/api/capture?rate=100000000&seconds=0.02&test=1")[0], 202)
        c = self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        self.assertEqual((c["state"], c["test"], c["problems"]), ("done", True, []))

    def test_a_capture_that_waits_for_its_trigger(self):
        self.wait(lambda s: s["board"]["state"] == "ready")
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&trigger=B9%3Dr"), (400, {
            "error": "B9 is not a channel: they are A0 ... A7 and B0 ... B7."}))
        self.assertEqual(self.post("/api/capture?rate=200000000&seconds=20&trigger=A0%3Dr")[0], 400)   # too long with what is kept
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&trigger=A1%3Df+then+A0%3Dr")[0], 202)
        c = self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        probes = samples_of(*zip(*fake.probe_runs(0, 100000, 1e6)))
        want = trigger_sample(probes, T.parse("A1=f then A0=r"))
        self.assertEqual((c["state"], c["problems"], c["trigger"], c["trigger_sample"], c["samples"], c["stopped"]),
                         ("done", [], "A1=f then A0=r", want, want + 20000, None))
        self.assertEqual(samples_of(*W.runs_of(self.get("/api/data"), "rle")), probes[:want + 20000])
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&trigger=A0%3Df&before=0.000005")[0], 202)
        c = self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        self.assertEqual((c["trigger_sample"], c["samples"]), (5, 20005))
        # a trigger that does not come: the page sees that the capture waits, and ends it
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02&trigger=B3%3Dr")[0], 202)
        self.wait(lambda s: s["capture"].get("waiting"))
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02")[0], 409)
        self.assertEqual(self.post("/api/stop")[0], 200)
        c = self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        self.assertEqual((c["state"], c["problems"], c["trigger_sample"], c["stopped"], c["samples"]),
                         ("done", [], None, "stopped", 2000))
        self.assertEqual(self.post("/api/capture?rate=1000000&seconds=0.02")[0], 202)       # the next is not ended by that
        c = self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        self.assertEqual((c["samples"], c["stopped"], c["trigger"]), (20000, None, None))

    def test_the_glitch_filter_a_capture_clock_and_lengths_in_samples(self):
        self.wait(lambda s: s["board"]["state"] == "ready")

        def capture(q):
            self.assertEqual(self.post("/api/capture?" + urllib.parse.urlencode(q))[0], 202, q)
            return self.wait(lambda s: s["capture"]["state"] != "running")["capture"]
        c = capture({"rate": 1e6, "seconds": 0.02, "glitch": "A0=20ns,B=1us"})
        self.assertEqual((c["state"], c["problems"], c["glitch_ns"], c["clock"]), ("done", [], [20] + [0] * 7 + [1000] * 8, None))
        c = capture({"rate": 1e6, "seconds": 0.02, "trigger": "A0=f", "samples_before": 7})
        self.assertEqual((c["trigger_sample"], c["samples"], c["glitch_ns"]), (7, 20007, None))    # no filter: none named
        self.assertEqual(c["pi"], None)
        c = capture({"rate": 1e6, "seconds": 0.02, "pi": "uart,sda"})         # the Pi's lines in place of probes
        self.assertEqual((c["state"], c["problems"], c["pi"]),
                         ("done", [], {"A0": "GPIO2 (SDA)", "A2": "GPIO14 (TXD)", "A3": "GPIO15 (RXD)"}))
        c = capture({"rate": 1e6, "samples": 1234})                 # a length in samples, with no clock
        self.assertEqual((c["samples"], c["clock"]), (1234, None))
        c = capture({"rate": 1e6, "clock": "A1=r", "samples": 300, "trigger": "A0=1 for 3", "samples_before": 10})
        self.assertEqual((c["state"], c["problems"], c["clock"], c["trigger"], c["trigger_sample"], c["samples"]),
                         ("done", [], "A1=r", "A0=1 for 3", 10, 310))
        with B.connect(backend=fake.FakeBus(instant=True), log=lambda line: None) as board:    # the same, asked directly
            want = board.capture(samples=300, clock="A1=r", trigger="A0=1 for 3", samples_before=10)
        self.assertEqual(samples_of(*W.runs_of(self.get("/api/data"), "rle")), samples_of(want.values, want.lengths))
        for q, why in (({"clock": "A1=r", "seconds": 0.02}, "With a capture clock the length is a number of samples."),
                       ({"clock": "A1=r", "samples": 10, "trigger": "A0=r", "before": 1e-3}, "is a number of samples."),
                       ({"clock": "A1=r", "samples": 10, "trigger": "A0=1 for 1us"}, "no sample rate"),
                       ({"clock": "A0=1", "samples": 10}, "an input and an edge"), ({"seconds": 0.02, "glitch": "2us"}, "10 ns"),
                       ({"seconds": 0.02, "glitch": "C1=20ns"}, "C1"), ({"seconds": 0.02, "pi": "gpio4"}, "the Pi's lines"),
                       ({"samples": "many"}, "is a number"),
                       ({"clock": "A1=r"}, "With a capture clock the length is a number of samples."),
                       ({"seconds": 0.02, "trigger": "A0=r", "samples_before": "x"}, "is a number"),
                       ({"seconds": 0.02, "trigger": "A0=r", "before": 1e-3, "samples_before": 5}, "before")):
            code, said = self.post("/api/capture?" + urllib.parse.urlencode({"rate": 1e6, **q}))
            self.assertEqual(code, 400, q)
            self.assertIn(why, said["error"], q)
        self.assertEqual(json.loads(self.get("/api/status"))["capture"]["samples"], 310)     # the refused ones changed nothing

    def test_the_board_says_its_revision_and_range(self):
        s = self.wait(lambda s: s["board"]["state"] == "ready")
        self.assertEqual((s["board"]["revision"], s["board"]["threshold_range"]), ("V1.0.0", [0.3, 3.0]))

    def test_only_requests_that_name_this_computer(self):
        # a page elsewhere that points a name of its own at this computer (DNS rebinding) is refused
        port = self.httpd.server_address[1]
        for host in ("evil.example", "evil.example:%d" % port, "", "localhost.evil.example"):
            for data in (None, b""):
                req = urllib.request.Request(self.url + "/api/status", data=data,
                                             headers={"Host": host, "X-LB16": "1"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req)
                with caught.exception as e:
                    self.assertEqual(e.code, 403, host)
                    self.assertIn("http://localhost:%d" % port, json.loads(e.read())["error"])
        for host in ("localhost:%d" % port, "127.0.0.1:%d" % port, "LOCALHOST"):
            req = urllib.request.Request(self.url + "/api/status", headers={"Host": host})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.status, 200, host)

    def test_names_a_request_may_give(self):
        here = server.names_for("127.0.0.1")
        for header, ok in (("localhost:8016", True), ("127.0.0.1:8016", True), ("[::1]:8016", True),
                           ("192.168.1.20:8016", True), ("::1", True), ("evil.example", False), ("", False),
                           (None, False), ("[evil.example]:8016", False), (socket.gethostname(), False)):
            self.assertEqual(server.host_ok(header, here), ok, header)
        lan = server.names_for("0.0.0.0")                  # served to other computers: its own name too
        base = socket.gethostname().lower().split(".")[0]
        for header in (base, base + ".local:8016", socket.gethostname()):
            self.assertTrue(server.host_ok(header, lan), header)
        self.assertFalse(server.host_ok("evil.example", lan))

    def test_pages_and_nothing_outside_them(self):
        self.assertIn(b"LOGIC BOOLE 16", self.get("/"))
        self.assertIn(b"fromRle", self.get("/capture.js"))
        self.assertIn(b"sigrokWords", self.get("/sigrok.js"))
        for path in ("/../board.py", "/%2e%2e/board.py", "/nothing.js"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path)
            caught.exception.close()


class Measuring(unittest.TestCase):
    def test_a_signal_worked_out_by_hand(self):
        # A0: low for 4 samples, then ten times high for 3 and low for 2.  A1: one pulse, samples 10 ... 29
        samples = [0] * 4 + [1, 1, 1, 0, 0] * 10
        samples = [v | (2 if 10 <= i < 30 else 0) for i, v in enumerate(samples)]
        cap = Capture(1e6, array("H", samples), array("Q", [1] * len(samples)))
        m = cap.measure(listed=4)
        a0, a1 = m["channels"]
        self.assertEqual((m["sample_rate"], m["sample_seconds"], m["start"], m["end"]), (1e6, 1e-6, 0.0, 54e-6))
        self.assertEqual((a0["channel"], a0["starts"], a0["changes"], a0["rising"], a0["falling"]), ("A0", 0, 20, 10, 10))
        self.assertEqual(a0["period"], {"min": 5e-6, "median": 5e-6, "max": 5e-6})
        self.assertEqual((a0["high"]["median"], a0["low"]["median"], a0["frequency"], a0["average_frequency"]), (3e-6, 2e-6, 200e3, 200e3))
        self.assertAlmostEqual(a0["duty"], 0.6)
        self.assertEqual((a1["channel"], a1["changes"], a1["rising"], a1["high"]), ("A1", 2, 1, {"min": 20e-6, "median": 20e-6, "max": 20e-6}))
        self.assertEqual((a1["period"], a1["frequency"], a1["average_frequency"], a1["low"], a1["duty"]), (None, None, None, None, None))
        self.assertEqual(m["changes_total"], 22)
        self.assertEqual([(c["sample"], c["channel"], c["level"]) for c in m["changes"]],
                         [(4, "A0", 1), (7, "A0", 0), (9, "A0", 1), (10, "A1", 1)])
        self.assertEqual(m["changes"][3]["time"], 10e-6)
        part = cap.measure(["a0", "B7"], start=10e-6, end=30e-6)
        self.assertEqual([(c["channel"], c["starts"], c["changes"], c["rising"]) for c in part["channels"]],
                         [("A0", 1, 8, 4), ("B7", 0, 0, 0)])
        self.assertAlmostEqual(part["channels"][0]["duty"], 0.6)
        self.assertEqual(part["changes"], [])
        with self.assertRaises(ValueError):
            cap.measure(["C1"])
        with self.assertRaises(ValueError):
            cap.measure(start=2e-5, end=1e-5)

    def test_a_clock_between_whole_samples(self):
        # periods of 4, 4, 4 and 3 samples, as a 200 MHz clock at 750 MS/s: the median is 4, the average 3.75
        samples = [0] + ([1, 1, 0, 0] * 3 + [1, 0, 0]) * 40 + [1]
        cap = Capture(750e6, array("H", samples), array("Q", [1] * len(samples)))
        a0 = cap.measure(["A0"])["channels"][0]
        self.assertEqual((a0["rising"], a0["frequency"]), (161, 187.5e6))
        self.assertAlmostEqual(a0["average_frequency"], 200e6)

    def test_against_the_samples_one_by_one(self):
        rng = random.Random(11)
        for _ in range(20):
            runs = [(rng.randrange(4), rng.randrange(1, 9)) for _ in range(rng.randrange(1, 120))]
            cap = Capture(4e6, array("H", [v for v, _ in runs]), array("Q", [n for _, n in runs]))
            samples = samples_of(cap.values, cap.lengths)
            first, last = sorted(rng.randrange(len(samples) + 1) for _ in range(2))
            m = cap.measure(["A0", "A1"], start=first / 4e6, end=last / 4e6, listed=1000)
            want = []
            for ch, got in enumerate(m["channels"]):
                bits = [v >> ch & 1 for v in samples]
                edges = [i for i in range(max(first, 1), last) if bits[i] != bits[i - 1]]
                rising = [i for i in edges if bits[i]]
                want += [(i, ch, bits[i]) for i in edges]
                self.assertEqual((got["changes"], got["rising"], got["falling"]), (len(edges), len(rising), len(edges) - len(rising)))
                if edges:
                    self.assertEqual(got["starts"], 1 - bits[edges[0]])
                periods = [b - a for a, b in zip(rising, rising[1:])]
                self.assertEqual(got["period"] and (got["period"]["min"], got["period"]["max"]),
                                 (min(periods) / 4e6, max(periods) / 4e6) if periods else None)
                highs = [b - a for a, b in zip(edges, edges[1:]) if bits[a]]
                lows = [b - a for a, b in zip(edges, edges[1:]) if not bits[a]]
                for key, lengths in (("high", highs), ("low", lows)):
                    self.assertEqual(got[key] and (got[key]["min"], got[key]["max"]),
                                     (min(lengths) / 4e6, max(lengths) / 4e6) if lengths else None)
                if periods:
                    high = sum(bits[rising[0]:rising[-1]])
                    self.assertAlmostEqual(got["duty"], high / (rising[-1] - rising[0]))
            self.assertEqual([(c["sample"], CHANNELS.index(c["channel"]), c["level"]) for c in m["changes"]], sorted(want))


# What sigrok-cli 0.7.2 prints for these: a stand-in for it, so that these checks need no sigrok.
SHOWN = {"i2c": """ID: i2c
Name: I²C
Long name: Inter-Integrated Circuit
Description: Two-wire, multi-master, serial bus.
License: gplv2+
Possible decoder input IDs:
- logic
Possible decoder output IDs:
- i2c
Decoder tags:
Annotation classes:
- start: Start condition
- bit: Data/address bit
- address-write: Address write
- data-write: Data write
- warnings: Human-readable warnings
Annotation rows:
- bits (Bits): bit
- addr-data (Address/Data): start, address-write, data-write
Binary classes:
- data-write: Data write
Required channels:
- scl (SCL): Serial clock line
- sda (SDA): Serial data line
Optional channels:
None.
Options:
- address_format: Displayed slave address format ('shifted', 'unshifted', default 'shifted')
Documentation:
I²C is a bidirectional, multi-master bus.

Two lines.
""", "eeprom24xx": """ID: eeprom24xx
Name: 24xx EEPROM
Long name: 24xx I²C EEPROM
Description: 24xx series I²C EEPROM protocol.
License: gplv2+
Possible decoder input IDs:
- i2c
Possible decoder output IDs:
None.
Decoder tags:
Annotation classes:
- warnings: Warnings
Required channels:
None.
Optional channels:
None.
Options:
- chip: Chip ('generic', 'st_m24c02', default 'generic')
- addr_counter: Initial address counter value (default 0)
Documentation:
A decoder on top of I²C.
""", "uart": """ID: uart
Name: UART
Long name: Universal Asynchronous Receiver/Transmitter
Description: Asynchronous, serial bus.
License: gplv2+
Possible decoder input IDs:
- logic
Possible decoder output IDs:
- uart
Decoder tags:
Annotation classes:
- rx-data: RX data
- rx-data-bits: RX data bits
Required channels:
None.
Optional channels:
- rx (RX): UART receive line
- tx (TX): UART transmit line
Options:
- baudrate: Baud rate (default 115200)
- stop_bits: Stop bits (0.0, 0.5, 1.0, 1.5, default 1.0)
Documentation:
Serial.
"""}
FOUND = ['500-500 i2c-1: stop: "Stop" "P"', '40-40 i2c-1: start: "Start" "S"', '201-221 i2c-1: bit: "0"',
         '181-201 i2c-1: bit: "1"', '61-201 i2c-1: address-write: "Address write: 16" "AW: 16" "16"',
         '201-221 i2c-1: address-write: "Write" "Wr" "W"', '221-241 i2c-1: ack: "ACK" "A"',
         '241-401 i2c-1: data-write: "Data write: 4C" "DW: 4C" "4C"',
         '61-500 eeprom24xx-1: byte-write: "Byte write (addr=4C): "x"" "BW"']         # a quote is not escaped
STAND_IN = r"""#!%s
import json, sys, zipfile
a, log = sys.argv[1:], %r
SHOWN, FOUND = %r, %r
def note(what):
    with open(log, "a") as f:
        f.write(json.dumps(what) + "\n")
note(a)
if a == ["-L"]:
    print("Supported hardware drivers:\n  demo                 Demo driver\n\nSupported protocol decoders:")
    print("  eeprom24xx           24xx I2C EEPROM\n  i2c                  Inter-Integrated Circuit")
    print("  uart                 Universal Asynchronous Receiver/Transmitter\n\nSupported transform modules:\n  invert")
elif "--show" in a:
    if a[1] not in SHOWN:
        sys.exit("srd: ModuleNotFoundError: Failed to load decoder")
    sys.stdout.buffer.write(SHOWN[a[1]].encode())
else:
    with zipfile.ZipFile(a[a.index("-i") + 1]) as z:
        note({"metadata": z.read("metadata").decode(),
              "bytes": sum(i.file_size for i in z.infolist() if i.filename.startswith("logic-1"))})
    if "baudrate=1:" in a[a.index("-P") + 1] + ":":
        sys.stderr.write("sr: [00:00.000184] log: libsigrok loglevel set to 4.\nsrd: The decoder gave up.\n")
        sys.exit(1)
    only = a[a.index("-A") + 1] if "-A" in a else None
    for line in FOUND:
        if only is None or line.split()[1].startswith(only + "-"):
            print(line)
"""


class Decoders(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.log = self.path("asked.txt")
        with open(self.path("sigrok-cli"), "w", encoding="utf-8") as f:
            f.write(STAND_IN % (sys.executable, self.log, SHOWN, FOUND))
        os.chmod(self.path("sigrok-cli"), 0o755)
        patch = unittest.mock.patch.dict(os.environ, {D.ENV: self.path("sigrok-cli")})
        patch.start()
        self.addCleanup(patch.stop)
        rng = random.Random(5)
        runs = random_runs(rng, 300, 40)
        self.cap = Capture(1e6, array("H", [v for v, _ in runs]), array("Q", [n for _, n in runs]), trigger=77)
        self.cap.save(self.path("a.lb16"))

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def asked(self):
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def rows(self, found):
        return [(r["start_sample"], r["end_sample"], r["decoder"], r["class"], r["text"]) for r in found["annotations"]]

    def test_what_sigrok_cli_has(self):
        self.assertEqual(D.decoders(), [{"id": "eeprom24xx", "name": "24xx I2C EEPROM"},
                                        {"id": "i2c", "name": "Inter-Integrated Circuit"},
                                        {"id": "uart", "name": "Universal Asynchronous Receiver/Transmitter"}])
        d = D.about("i2c")
        self.assertEqual((d["id"], d["name"], d["description"]), ("i2c", "Inter-Integrated Circuit", "Two-wire, multi-master, serial bus."))
        self.assertEqual((d["inputs"], d["outputs"]), (["logic"], ["i2c"]))
        self.assertEqual(d["channels"], [{"id": "scl", "name": "SCL", "about": "Serial clock line", "required": True},
                                         {"id": "sda", "name": "SDA", "about": "Serial data line", "required": True}])
        self.assertEqual(d["options"], [{"id": "address_format", "about": "Displayed slave address format",
                                         "default": "shifted", "values": ["shifted", "unshifted"]}])
        self.assertEqual([c["id"] for c in d["classes"]], ["start", "bit", "address-write", "data-write", "warnings"])
        self.assertEqual(d["documentation"], "I²C is a bidirectional, multi-master bus.\n\nTwo lines.")
        u = D.about("uart")
        self.assertEqual([(c["id"], c["required"]) for c in u["channels"]], [("rx", False), ("tx", False)])
        self.assertEqual(u["options"], [{"id": "baudrate", "about": "Baud rate", "default": "115200"},
                                        {"id": "stop_bits", "about": "Stop bits", "default": "1.0",
                                         "values": ["0.0", "0.5", "1.0", "1.5"]}])
        self.assertEqual((D.about("eeprom24xx")["inputs"], D.about("eeprom24xx")["outputs"]), (["i2c"], []))
        for name in ("spi", "i2c;rm", ""):
            with self.assertRaises(D.DecodeError):
                D.about(name)

    def test_what_was_found_in_the_order_it_happened(self):
        found = D.decode(self.path("a.lb16"), "i2c scl=A1 sda=a0")
        self.assertEqual(self.rows(found), [(40, 40, "i2c", "start", "Start"),
                                            (61, 201, "i2c", "address-write", "Address write: 16"),
                                            (201, 221, "i2c", "address-write", "Write"), (221, 241, "i2c", "ack", "ACK"),
                                            (241, 401, "i2c", "data-write", "Data write: 4C"),
                                            (500, 500, "i2c", "stop", "Stop")])
        self.assertEqual((found["decoders"], found["sample_rate"], found["samples"], found["total"]),
                         ("i2c:scl=A1:sda=A0", 1e6, self.cap.samples, 6))
        self.assertEqual((found["trigger_sample"], found["trigger_seconds"]), (77, 77e-6))
        self.assertEqual((found["annotations"][1]["start"], found["annotations"][1]["end"]), (61e-6, 201e-6))
        asked = self.asked()
        self.assertEqual(asked[-2][2:], ["-P", "i2c:scl=A1:sda=A0", "-l", "4", "-A", "i2c"])
        self.assertIn("samplerate=1 MHz", asked[-1]["metadata"])       # the session it was handed holds the capture
        self.assertIn("probe1=A0", asked[-1]["metadata"])
        self.assertEqual(asked[-1]["bytes"], 2 * self.cap.samples)
        self.assertFalse(os.path.exists(os.path.dirname(asked[-2][1])))     # and is gone again

    def test_classes_a_part_and_a_limit(self):
        path = self.path("a.lb16")
        both = D.decode(path, ["i2c", "scl=A1", "sda=A0", "eeprom24xx", "chip=st_m24c02"])
        self.assertEqual(self.rows(both), [(61, 500, "eeprom24xx", "byte-write", 'Byte write (addr=4C): "x"')])
        self.assertEqual(self.asked()[-2][2:], ["-P", "i2c:scl=A1:sda=A0,eeprom24xx:chip=st_m24c02", "-l", "4", "-A", "eeprom24xx"])
        picked = D.decode(path, "i2c scl=A1 sda=A0 eeprom24xx", only=["bit", "byte-write"])
        self.assertEqual([r[:2] + r[3:4] for r in self.rows(picked)], [(61, 500, "byte-write"), (181, 201, "bit"), (201, 221, "bit")])
        self.assertNotIn("-A", self.asked()[-2])
        self.assertEqual([r[3] for r in self.rows(D.decode(path, "i2c scl=A1 sda=A0", only="ack"))], ["ack"])
        part = D.decode(path, "i2c scl=A1 sda=A0", start=201e-6, end=500e-6)
        self.assertEqual([r[0] for r in self.rows(part)], [201, 221, 241])
        few = D.decode(path, "i2c scl=A1 sda=A0", limit=2)
        self.assertEqual(([r[0] for r in self.rows(few)], few["total"]), ([40, 61], 6))
        with self.assertRaisesRegex(D.DecodeError, "stopped"):
            D.decode(path, "i2c scl=A1 sda=A0", cancel=lambda: True)

    def test_a_quiet_sigrok_cli_is_stopped_when_asked(self):
        # one that prints nothing for a long while: the app's Cancel and its time limit stop it all the same
        with open(self.path("quiet"), "w", encoding="utf-8") as f:
            f.write("#!%s\nimport os, sys, time\nif '-i' in sys.argv:\n    time.sleep(60)\n"
                    "os.execv(%r, [%r] + sys.argv[1:])\n" % (sys.executable, self.path("sigrok-cli"),
                                                             self.path("sigrok-cli")))
        os.chmod(self.path("quiet"), 0o755)
        with unittest.mock.patch.dict(os.environ, {D.ENV: self.path("quiet")}):
            t0 = time.monotonic()
            with self.assertRaisesRegex(D.DecodeError, "The decoders were stopped."):
                D.decode(self.path("a.lb16"), "i2c scl=A1 sda=A0", cancel=lambda: time.monotonic() >= t0 + 0.5)
            self.assertLess(time.monotonic() - t0, 3)

    def test_a_capture_with_its_own_names_and_a_session_file(self):
        self.cap.save(self.path("named.sr"), ["SDA", "SCL"] + ["D%d" % i for i in range(2, 16)])
        found = D.decode(self.path("named.sr"), "i2c scl=scl sda=SDA")
        self.assertEqual((found["decoders"], found["samples"], found["sample_rate"], found["trigger_sample"]),
                         ("i2c:scl=SCL:sda=SDA", self.cap.samples, 1e6, None))
        self.assertEqual(self.asked()[-2][:2], ["-i", self.path("named.sr")])
        # A0 ... B7 are its first 16 channels, as measure takes them
        self.assertEqual(D.decode(self.path("named.sr"), "i2c scl=A1 sda=a0")["decoders"], "i2c:scl=SCL:sda=SDA")
        with self.assertRaisesRegex(D.DecodeError, "no channel CLK.  Its channels are D10"):
            D.decode(self.path("named.sr"), "i2c scl=CLK sda=SDA")
        self.cap.names[5] = "Reset"
        self.cap.save(self.path("named.lb16"))
        self.assertEqual(D.decode(self.path("named.lb16"), "uart rx=reset tx=A6")["decoders"], "uart:rx=A5:tx=A6")
        empty = Capture(1e6, array("H"), array("Q"))
        empty.save(self.path("empty.lb16"))
        self.assertEqual(D.decode(self.path("empty.lb16"), "i2c scl=A1 sda=A0")["annotations"], [])

    def test_what_cannot_be_decoded_says_why(self):
        path = self.path("a.lb16")
        for words, why in (("i2c scl=A1", "i2c needs: sda=CHANNEL"), ("i2c scl=A1 sda=C0", "no channel C0"),
                           ("i2c scl=A1 sda=A0 address_format=odd", "one of shifted, unshifted"),
                           ("i2c scl=A1 sda=A0 speed=1", "no channel or option named speed"),
                           ("spi clk=A0", "no decoder named spi"), ("eeprom24xx", "goes on top of a decoder that gives it i2c"),
                           ("uart rx=A0 eeprom24xx", "eeprom24xx does not go on top of uart"),
                           ("uart baudrate=9600", "was given no channel"), ("uart rx=A0 stop_bits=2", "one of 0.0, 0.5"),
                           ("scl=A1", "decoder's name comes first"), ("", "No decoder was named"),
                           ("i2c scl=A1 scl=A2 sda=A0", "given once"), ("uart rx=A0 baudrate=1", "The decoder gave up")):
            with self.assertRaisesRegex(D.DecodeError, why, msg=words):
                D.decode(path, words)
        self.assertEqual(D.decode(path, "uart rx=A0 stop_bits=1")["decoders"], "uart:rx=A0:stop_bits=1")
        for name, why in (("a.vcd", "saved as .lb16 or .sr"), ("none.sr", "no such file")):
            with self.assertRaisesRegex(D.DecodeError, why):
                D.decode(self.path(name), "i2c scl=A1 sda=A0")
        with open(self.path("not.sr"), "w") as f:
            f.write("not a zip file")
        with self.assertRaisesRegex(D.DecodeError, "not a sigrok session"):
            D.decode(self.path("not.sr"), "i2c scl=A1 sda=A0")
        with unittest.mock.patch.dict(os.environ), unittest.mock.patch("logicboole.decode.shutil.which", return_value=None):
            del os.environ[D.ENV]
            with self.assertRaisesRegex(D.DecodeError, "sigrok-cli was not found"):
                D.decode(path, "i2c scl=A1 sda=A0")

    def test_with_sigrok_cli_itself(self):
        with unittest.mock.patch.dict(os.environ):
            del os.environ[D.ENV]
            if not shutil.which("sigrok-cli"):
                self.skipTest("sigrok-cli is not installed")
            with B.connect(backend=fake.FakeBus(instant=True)) as board:
                board.capture(rate=2e6, seconds=0.05).save(self.path("bus.lb16"))
            self.assertIn("i2c", [d["id"] for d in D.decoders()])
            self.assertEqual([c["id"] for c in D.about("i2c")["channels"]], ["scl", "sda"])
            found = D.decode(self.path("bus.lb16"), "i2c scl=A1 sda=A0", only=["address-write", "data-write", "stop"])
            texts = [r["text"] for r in found["annotations"]]
            sent = texts[1:texts.index("Stop")]         # after "Address write: 16": "Write", then the bytes
            self.assertEqual(texts[0], "Address write: 16")
            self.assertEqual(bytes(int(t.split()[-1], 16) for t in sent[1:]), b"Logic Boole 16")
            self.assertEqual(found["annotations"][0]["start_sample"], 61)
            # a UART that sends "A" with the quotes: sigrok-cli prints a quote as it is
            bits = [1] * 30
            for byte in b'"A"':
                bits += [0] + [byte >> i & 1 for i in range(8)] + [1, 1, 1]
            samples = [b for b in bits + [1] * 30 for _ in range(10)]
            Capture(1e6, array("H", samples), array("Q", [1] * len(samples))).save(self.path("uart.lb16"))
            found = D.decode(self.path("uart.lb16"), "uart rx=A0 baudrate=100000 format=ascii", only=["rx-data"])
            self.assertEqual([(r["start_sample"], r["text"]) for r in found["annotations"]], [(310, '"'), (430, "A"), (550, '"')])


class TheAppsDecoders(unittest.TestCase):
    """What the page asks of sigrok's decoders, with a stand-in for sigrok-cli."""
    path, asked, get, post = Decoders.path, Decoders.asked, TheApp.get, TheApp.post

    def setUp(self):
        Decoders.setUp(self)
        TheApp.setUp(self)
        with open(self.path("a.lb16"), "rb") as f:
            self.file = f.read()

    def decode(self, header=True, data=None, **q):
        return self.post("/api/decode?" + urllib.parse.urlencode(q), header, self.file if data is None else data)

    def test_which_decoders_there_are(self):
        found = json.loads(self.get("/api/decoders"))
        self.assertEqual((found["available"], [d["id"] for d in found["decoders"]]), (True, ["eeprom24xx", "i2c", "uart"]))
        d = json.loads(self.get("/api/decoder?name=i2c"))
        self.assertEqual(([c["id"] for c in d["channels"]], d["options"][0]["values"]), (["scl", "sda"], ["shifted", "unshifted"]))
        for name in ("nothing", "", "i2c%20x"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get("/api/decoder?name=" + name)
            self.assertEqual(caught.exception.code, 400)
            self.assertIn("error", json.loads(caught.exception.read()))
            caught.exception.close()
        with unittest.mock.patch.dict(os.environ, {D.ENV: self.path("not-there")}):     # no sigrok-cli: the page says so
            found = json.loads(self.get("/api/decoders"))
            self.assertEqual((found["available"], found["decoders"]), (False, []))
            self.assertIn("sigrok-cli", found["why"])
            code, said = self.decode(words="i2c scl=A1 sda=A0")
            self.assertEqual(code, 400)
            self.assertIn("sigrok-cli", said["error"])

    def test_a_capture_the_page_sends_is_decoded(self):
        code, found = self.decode(words="i2c scl=A1 sda=a0")
        self.assertEqual((code, found["decoders"], found["total"]), (200, "i2c:scl=A1:sda=A0", 6))
        self.assertEqual(found["rows"][:3], [[40, 40, "i2c", "start", ["Start", "S"]],
                                             [61, 201, "i2c", "address-write", ["Address write: 16", "AW: 16", "16"]],
                                             [201, 221, "i2c", "address-write", ["Write", "Wr", "W"]]])
        asked = self.asked()
        self.assertEqual(asked[-1]["bytes"], 2 * self.cap.samples)          # sigrok-cli was handed the whole capture
        self.assertFalse(os.path.exists(os.path.dirname(asked[-2][1])))     # and nothing of it is left behind
        code, found = self.decode(words="i2c scl=A1 sda=A0", limit=2)
        self.assertEqual((code, found["total"], [r[3] for r in found["rows"]]), (200, 6, ["start", "address-write"]))
        code, found = self.decode(words="i2c scl=A1 sda=A0", only="ack, stop")
        self.assertEqual([r[3] for r in found["rows"]], ["ack", "stop"])
        code, found = self.decode(words="i2c scl=A1 sda=A0 eeprom24xx")     # a decoder on top: its rows
        self.assertEqual(found["rows"], [[61, 500, "eeprom24xx", "byte-write", ['Byte write (addr=4C): "x"', "BW"]]])

    def test_what_cannot_be_decoded_says_why(self):
        self.assertEqual(self.decode(header=False, words="i2c scl=A1 sda=A0"), (403, {"error": "not from the app"}))
        for q, why in (({"words": "i2c scl=A1"}, "i2c needs: sda=CHANNEL"), ({"words": "i2c scl=C1 sda=A0"}, "C1"),
                       ({"words": "nothing"}, "nothing"), ({}, "decoder"), ({"words": "uart rx=A0 baudrate=1"}, "gave up")):
            code, said = self.decode(**q)
            self.assertEqual(code, 400, q)
            self.assertIn(why, said["error"], q)
        code, said = self.decode(data=b"not a capture at all", words="i2c scl=A1 sda=A0")
        self.assertEqual(code, 400)
        self.assertIn("What the page sent is not a capture file", said["error"])
        self.assertNotIn(tempfile.gettempdir(), said["error"])
        self.assertEqual(self.decode(words="i2c scl=A1 sda=A0")[0], 200)    # and the app goes on


class TheAgentsServer(unittest.TestCase):
    """logicboole mcp, spoken to as an agent's program speaks to it."""
    NOW = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        (r1, w1), (r2, w2) = os.pipe(), os.pipe()
        ends = [os.fdopen(r1, "rb"), os.fdopen(w2, "wb"), os.fdopen(w1, "wb"), os.fdopen(r2, "rb")]
        self.to = ends[2]
        self.server = threading.Thread(target=mcp.serve, daemon=True,
                                       args=(fake.FakeBus(instant=True), True, self.dir.name, ends[0], ends[1]))
        self.server.start()
        self.got, self.n = queue.Queue(), 0

        def read():
            for line in ends[3]:
                self.assertNotIn(b"\n", line[:-1])
                self.got.put(json.loads(line))
            self.got.put(None)
        threading.Thread(target=read, daemon=True).start()

        def end():
            if not self.to.closed:
                self.to.close()
            self.server.join(20)
            ends[1].close()
            self.assertFalse(self.server.is_alive())
            for e in ends:
                e.close()
        self.addCleanup(end)

    def send(self, message):
        self.to.write(json.dumps(message).encode() + b"\n")
        self.to.flush()

    def ask(self, method, params=None, meta=None):
        self.n += 1
        params = dict(params or {})
        if meta:
            params["_meta"] = meta
        self.send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        answer = self.got.get(timeout=30)
        self.assertEqual(answer["id"], self.n)
        return answer

    def tool(self, called, /, **args):
        r = self.ask("tools/call", {"name": called, "arguments": args})["result"]
        self.assertEqual([c["type"] for c in r["content"]], ["text"])
        return r["isError"], r["content"][0]["text"] if r["isError"] else json.loads(r["content"][0]["text"])

    def test_the_form_that_begins_with_initialize(self):
        r = self.ask("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})["result"]
        self.assertEqual((r["protocolVersion"], r["capabilities"], r["serverInfo"]["name"]), ("2025-06-18", {"tools": {}}, "logicboole"))
        self.assertIn("16-channel logic analyzer", r["instructions"])
        self.assertNotIn("resultType", r)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(self.ask("initialize", {"protocolVersion": "2020-01-01"})["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(self.ask("ping")["result"], {})
        r = self.ask("tools/list")["result"]
        self.assertEqual(sorted(r), ["tools"])
        self.assertEqual([t["name"] for t in r["tools"]], ["board_info", "set_thresholds", "read_inputs", "capture",
                                                          "measure", "find", "detect", "plot", "decode",
                                                          "list_decoders", "self_test", "guide"])
        for t in r["tools"]:
            schema = t["inputSchema"]
            self.assertEqual((schema["type"], schema["additionalProperties"]), ("object", False), t["name"])
            self.assertTrue(set(schema.get("required", ())) <= set(schema["properties"]), t["name"])
            for p in schema["properties"].values():
                self.assertIn(p["type"], mcp.KINDS)
                self.assertTrue(p["description"])
            self.assertTrue(t["title"] and len(t["description"]) > 40)

    def test_the_form_with_no_beginning(self):
        r = self.ask("server/discover", meta=self.NOW)["result"]
        self.assertEqual((r["resultType"], r["supportedVersions"][0], r["capabilities"]), ("complete", "2026-07-28", {"tools": {}}))
        self.assertEqual(r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"], "logicboole")
        r = self.ask("tools/list", meta=self.NOW)["result"]
        self.assertEqual((r["resultType"], len(r["tools"])), ("complete", 12))
        r = self.ask("tools/call", {"name": "read_inputs", "arguments": {}}, meta=self.NOW)["result"]
        self.assertEqual((r["resultType"], r["isError"]), ("complete", False))
        e = self.ask("tools/list", meta=dict(self.NOW, **{"io.modelcontextprotocol/protocolVersion": "2030-01-01"}))["error"]
        self.assertEqual((e["code"], e["data"]["requested"], e["data"]["supported"][0]), (-32022, "2030-01-01", "2026-07-28"))
        e = self.ask("tools/list", meta={"io.modelcontextprotocol/protocolVersion": "2026-07-28"})["error"]
        self.assertEqual(e["code"], -32602)

    def test_what_is_not_the_protocol(self):
        self.assertEqual(self.ask("tools/call", {"name": "format_disk", "arguments": {}})["error"]["code"], -32602)
        self.assertEqual(self.ask("resources/list")["error"]["code"], -32601)
        for line, code in ((b"not json", -32700), (b"[1, 2]", -32600), (b'{"id": 1, "method": "ping"}', -32600),
                           (b'{"jsonrpc": "2.0", "id": null, "method": "ping"}', -32600)):
            self.to.write(line + b"\n")
            self.to.flush()
            answer = self.got.get(timeout=30)
            self.assertEqual((answer["id"], answer["error"]["code"]), (None, code))
        self.send({"jsonrpc": "2.0", "id": 5, "result": {}})            # an answer to nothing: left alone
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": [1]}})
        self.assertEqual(self.ask("ping")["result"], {})

    def test_a_capture_measured(self):
        wrong, info = self.tool("board_info")
        self.assertEqual((wrong, info["state"], info["captures_folder"]), (False, "boot", self.dir.name))
        self.assertEqual(self.tool("set_thresholds", a=1.65, b=0.9), (False, {"threshold_a": 1.65, "threshold_b": 0.9}))
        wrong, now = self.tool("read_inputs")
        self.assertEqual((now["inputs"]["A0"], now["inputs"]["B7"], now["threshold_b"]), (1, 0, 0.9))
        wrong, info = self.tool("board_info")
        self.assertEqual((info["state"], info["threshold_a"], info["inputs"]["A1"]), ("ready", 1.65, 1))
        also = os.path.join(self.dir.name, "also.vcd")
        wrong, cap = self.tool("capture", rate=2e6, seconds=0.05, save_as=[also])
        self.assertEqual((wrong, cap["ok"], cap["samples"], cap["sample_rate"], cap["sample_seconds"]), (False, True, 100000, 2e6, 5e-7))
        self.assertEqual((sorted(cap["changing"]), cap["steady_high"], len(cap["steady_low"])), (["A0", "A1"], [], 14))
        self.assertEqual(os.path.dirname(cap["file"]), self.dir.name)
        self.assertTrue(os.path.getsize(also))
        want = samples_of(*zip(*fake.probe_runs(0, 100000, 2e6)))
        back = Capture.load(cap["file"])
        self.assertEqual(samples_of(back.values, back.lengths), want)
        self.assertEqual(cap["changing"]["A1"], {"changes": sum(1 for a, b in zip(want, want[1:]) if (a ^ b) & 2),
                                                 "first": next(i for i, v in enumerate(want) if not v & 2) / 2e6, "starts": 1})
        wrong, m = self.tool("measure", file=cap["file"], channels=["A1"], end=0.01, list=2)
        self.assertEqual((wrong, m["channels"][0]["frequency"], m["channels"][0]["period"]["median"]), (False, 100e3, 1e-5))
        self.assertEqual(m["changes"], [[c["time"], c["channel"], c["level"]] for c in back.measure(["A1"], listed=2)["changes"]])
        wrong, second = self.tool("capture", rate=2e6, seconds=0.05)
        self.assertNotEqual(second["file"], cap["file"])

    def raw(self, called, /, **args):
        return self.ask("tools/call", {"name": called, "arguments": args})["result"]

    def test_names_find_detect_plot_and_the_guide(self):
        wrong, cap = self.tool("capture", rate=2e6, seconds=0.05, names="A0=SDA,A1=SCL", trigger="SDA=f SCL=1", timeout=5)
        self.assertEqual((wrong, cap["names"], cap["trigger"], cap["triggered"]), (False, {"A0": "SDA", "A1": "SCL"}, "A0=f A1=1", True))
        self.assertEqual(Capture.load(cap["file"]).names[:3], ["SDA", "SCL", "A2"])
        wrong, m = self.tool("measure", file=cap["file"], channels=["scl"])
        self.assertEqual((wrong, m["channels"][0]["channel"], m["channels"][0]["name"]), (False, "A1", "SCL"))
        wrong, f = self.tool("find", file=cap["file"], text="SCL=1 for 100us", limit=2)
        self.assertEqual((wrong, f["find"], len(f["found"])), (False, "A1=1 for 100us", 2))
        self.assertGreater(f["total"], 2)
        wrong, d = self.tool("detect", file=cap["file"])
        self.assertEqual((wrong, d["buses"][0]["decode"], d["channels"][1]["name"]), (False, "i2c scl=A1 sda=A0", "SCL"))
        self.assertIn("a guess", d["note"])
        r = self.raw("plot", file=cap["file"], end=0.001, marks=[0.0002], channels=["SDA", "SCL"])
        self.assertEqual(([c["type"] for c in r["content"]], r["isError"], r["content"][1]["mimeType"]), (["text", "image"], False, "image/png"))
        info = json.loads(r["content"][0]["text"])
        w, h, rows = pixels(base64.b64decode(r["content"][1]["data"]))
        self.assertEqual((w, h, info["channels"], info["marks"]), (1200, info["height"], ["A0", "A1"], [{"mark": 1, "time": 0.0002}]))
        with open(info["file"], "rb") as f:
            self.assertEqual(base64.b64encode(f.read()).decode(), r["content"][1]["data"])
        self.assertNotEqual(json.loads(self.raw("plot", file=cap["file"])["content"][0]["text"])["file"], info["file"])
        r = self.raw("guide")
        self.assertEqual(([c["type"] for c in r["content"]], r["isError"]), (["text"], False))
        self.assertTrue(r["content"][0]["text"].startswith("This guide is the skill file"))
        self.assertTrue(self.raw("guide", topic="recipes")["content"][0]["text"].startswith("## Recipes"))
        for called, args, why in (("capture", {"names": "A0=X,A1=X"}, "A0 and A1 would both be named X."),
                                  ("plot", {"file": cap["file"], "marks": ["1ms"]}, "marks is a list of numbers."),
                                  ("plot", {"file": cap["file"], "save_as": info["file"]}, "is there already"),
                                  ("plot", {"file": cap["file"], "save_as": "x.jpg"}, "saved as .png"),
                                  ("find", {"file": cap["file"], "text": "in=r"}, "TRIG IN is not in a saved capture"),
                                  ("detect", {"file": os.path.join(self.dir.name, "none.lb16")}, "there is no such file")):
            wrong, said = self.tool(called, **args)
            self.assertTrue(wrong, called)
            self.assertIn(why, said)

    def test_a_trigger_and_a_capture_clock(self):
        probes = samples_of(*zip(*fake.probe_runs(0, 100000, 1e6)))
        want = trigger_sample(probes, T.parse("A1=f then A0=r"))
        wrong, cap = self.tool("capture", rate=1e6, seconds=0.02, trigger="A1=f then A0=r", before=0.002, glitch="A=50ns")
        self.assertEqual((wrong, cap["triggered"], cap["trigger_sample"], cap["trigger_seconds"], cap["samples"]),
                         (False, True, want, want / 1e6, want + 20000))
        self.assertEqual((cap["trigger"], cap["glitch_ns"]["A7"], "B0" in cap["glitch_ns"]), ("A1=f then A0=r", 50, False))
        self.assertNotIn("pi", cap)
        wrong, cap = self.tool("capture", rate=1e6, seconds=0.02, pi="i2c")
        self.assertEqual((wrong, cap["ok"], cap["pi"], cap["changing"]), (False, True, {"A0": "GPIO2 (SDA)", "A1": "GPIO3 (SCL)"}, {}))
        wrong, cap = self.tool("capture", rate=1e6, seconds=0.02, trigger="B3=r", timeout=0.05)
        self.assertEqual((wrong, cap["ok"], cap["triggered"], "trigger_sample" in cap), (False, True, False, False))
        self.assertIn("timeout", cap["stopped"])
        wrong, cap = self.tool("capture", samples=300, clock="A1=r", trigger="A0=1 for 3", samples_before=10)
        self.assertEqual((wrong, cap["clock"], cap["samples"], cap["trigger_sample"]), (False, "A1=r", 310, 10))
        wrong, why = self.tool("capture", samples=300, clock="A1=r", trigger="A0=1 for 3", before=1e-5)
        self.assertEqual((wrong, "samples_before" in why), (True, True))
        wrong, short = self.tool("capture", rate=1e6, seconds=0.02, trigger="A0=f", samples_before=7)
        self.assertEqual((short["trigger_sample"], short["samples"]), (7, 20007))
        wrong, m = self.tool("measure", file=cap["file"])
        self.assertIn("sample numbers", m["clock"])

    def test_the_fast_design_through_it(self):
        wrong, cap = self.tool("capture", fast="A0-A3", seconds=200e-6)
        self.assertEqual((wrong, cap["ok"], cap["fast"], cap["sample_rate"], cap["samples"]), (False, True, "A0-A3", 750e6, 150000))
        self.assertEqual((sorted(cap["changing"]), cap["steady_low"], len(cap["not_taken"])), (["A0", "A1"], ["A2", "A3"], 12))
        back = Capture.load(cap["file"])
        self.assertEqual(samples_of(back.values, back.lengths), samples_of(*zip(*fake.probe_runs(0, 150000, 750e6))))
        wrong, m = self.tool("measure", file=cap["file"], channels=["A1"])
        self.assertEqual((wrong, m["channels"][0]["frequency"]), (False, 100e3))
        wrong, pod = self.tool("capture", fast="pod b", samples=1000, raw=True)
        self.assertEqual((wrong, pod["fast"], pod["sample_rate"], pod["samples"], pod["not_taken"][0]), (False, "B", 375e6, 1000, "A0"))
        wrong, cap = self.tool("capture", rate=2e6, seconds=0.01)      # the standard design again
        self.assertEqual((wrong, cap["sample_rate"], "fast" in cap, "not_taken" in cap, len(cap["steady_low"])), (False, 2e6, False, False, 14))
        for args, why in (({"fast": "A0-A3", "trigger": "A0=r"}, "fast goes without trigger"),
                          ({"fast": "A", "rate": 375e6, "glitch": "50ns"}, "fast goes without rate and glitch"),
                          ({"fast": "C0-C3"}, "The fast design takes four inputs"),
                          ({"fast": "B4-B7", "seconds": 10}, "at the most")):
            wrong, said = self.tool("capture", **args)
            self.assertTrue(wrong, args)
            self.assertIn(why, said)
        wrong, test = self.tool("self_test", fast=True)
        self.assertEqual((wrong, test["passed"]), (False, True))
        self.assertTrue(any("A0-A3" in line for line in test["captures"]))

    def test_decoders_through_it(self):
        Decoders.setUp(self)
        wrong, found = self.tool("decode", file=self.path("a.lb16"), decoders="i2c scl=A1 sda=A0", limit=3)
        self.assertEqual((wrong, found["total"], found["listed"], found["columns"]), (False, 6, 3, ["start", "end", "class", "text"]))
        self.assertEqual(found["rows"], [[40e-6, 40e-6, "start", "Start"], [61e-6, 201e-6, "address-write", "Address write: 16"],
                                         [201e-6, 221e-6, "address-write", "Write"]])
        wrong, found = self.tool("decode", file=self.path("a.lb16"), decoders="i2c scl=A1 sda=A0 eeprom24xx", only=["ack", "byte-write"])
        self.assertEqual([r[2] for r in found["rows"]], ["eeprom24xx.byte-write", "i2c.ack"])
        self.assertEqual(self.tool("list_decoders")[1]["decoders"]["i2c"], "Inter-Integrated Circuit")
        self.assertEqual(self.tool("list_decoders", name="uart")[1]["options"][0], {"id": "baudrate", "about": "Baud rate", "default": "115200"})
        wrong, why = self.tool("decode", file=self.path("a.lb16"), decoders="i2c scl=A1")
        self.assertEqual((wrong, why), (True, "i2c needs: sda=CHANNEL (Serial data line)."))

    path, asked, rows = Decoders.path, Decoders.asked, Decoders.rows

    def test_what_cannot_be_done_is_told_to_the_agent(self):
        for name, args, why in (("capture", {"rate": "fast"}, "rate is a number"), ("capture", {"speed": 3}, "no argument named speed"),
                                ("capture", {"seconds": 1, "samples": 5}, "not both"), ("capture", {"trigger": "Q9=r"}, "Q9 is not a channel"),
                                ("capture", {"seconds": 0}, "more than 0"), ("capture", {"samples": 0}, "1 or more"),
                                ("capture", {"trigger": "A0=f", "before": -1}, "0 or more"),
                                ("capture", {"trigger": "A0=f", "before": 1e-3, "samples_before": 5}, "not both"),
                                ("capture", {"rate": 1e9}, "200 MS/s at most"), ("capture", {"rate": 200e6, "seconds": 100}, "at the most"),
                                ("capture", {"save_as": ["x.txt"]}, "saved as"), ("capture", {"clock": "A0=r"}, "give samples"),
                                ("capture", {"samples": 2.5}, "a whole number"), ("capture", {"raw": "yes"}, "true or false"),
                                ("capture", {"save_as": "x.sr"}, "a list of texts"), ("capture", {"glitch": "5s"}, "10 ns"),
                                ("capture", {"pi": "gpio4"}, "the Pi's lines"), ("capture", {"pi": 3}, "pi"),
                                ("set_thresholds", {}, "a is not given"), ("set_thresholds", {"a": 9}, "A threshold of 9 V is outside what the board takes, 0.3 V to 3 V: set it to about half your logic voltage"),
                                ("measure", {"file": os.path.join(self.dir.name, "none.lb16")}, "no such file"),
                                ("measure", {"file": __file__}, "test_host.py is not a capture file"),
                                ("decode", {"file": "x.lb16"}, "decoders is not given")):
            wrong, why_not = self.tool(name, **args)
            self.assertTrue(wrong, (name, args))
            self.assertIn(why, why_not, (name, args))
        self.assertEqual(os.listdir(self.dir.name), [])                 # and nothing was saved
        # what is null counts as not given, and a whole number may come as 2000.0
        self.assertEqual(self.tool("capture", rate=2e6, trigger=None, samples=2000.0)[1]["samples"], 2000)

    def test_a_request_is_cancelled_and_the_input_ends(self):
        self.send({"jsonrpc": "2.0", "id": "wait", "method": "tools/call",
                   "params": {"name": "capture", "arguments": {"seconds": 0.01, "trigger": "B7=r", "timeout": 60},
                              "_meta": {"progressToken": 7}}})
        note = self.got.get(timeout=30)
        self.assertEqual((note["method"], note["params"]["progressToken"], note["params"]["progress"]), ("notifications/progress", 7, 0.0))
        self.send({"jsonrpc": "2.0", "id": "next", "method": "tools/call", "params": {"name": "read_inputs", "arguments": {}}})
        self.assertEqual(self.ask("ping")["result"], {})                # answered while the capture waits
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "wait", "reason": "enough"}})
        self.assertEqual(self.got.get(timeout=30)["id"], "next")        # the cancelled one gets no answer
        self.send({"jsonrpc": "2.0", "id": "last", "method": "tools/call",
                   "params": {"name": "capture", "arguments": {"seconds": 0.01, "trigger": "B7=r", "timeout": 60}}})
        threading.Event().wait(0.2)
        self.to.close()
        self.server.join(20)
        self.assertFalse(self.server.is_alive())
        self.assertEqual(os.listdir(self.dir.name), [])                 # neither capture was kept

    def test_save_as_refuses_a_file_that_is_there(self):
        with tempfile.TemporaryDirectory() as folder:
            there = os.path.join(folder, "there.sr")
            with open(there, "w") as f:
                f.write("kept")
            wrong, why = self.tool("capture", seconds=0.001, save_as=[there])
            self.assertEqual((wrong, why), (True, "%s is there already: give overwrite=true to write over it." % there))
            with open(there) as f:
                self.assertEqual(f.read(), "kept")
            self.assertEqual(os.listdir(self.dir.name), [])          # refused before the capture
            wrong, got = self.tool("capture", seconds=0.001, save_as=[there], overwrite=True)
            self.assertFalse(wrong, got)
            self.assertEqual((got["also_saved"], Capture.load(there).samples), ([there], 1000))
            wrong, why = self.tool("capture", seconds=0.001, save_as=[os.path.join(folder, "none", "x.vcd")])
            self.assertTrue(wrong)
            self.assertIn("x.vcd cannot be saved: there is no folder", why)
        props = mcp.BY_NAME["capture"]["inputSchema"]["properties"]
        self.assertEqual(props["overwrite"]["type"], "boolean")
        # the 10 s that a capture waits for its trigger, if no timeout is given, also end one with a capture clock
        self.assertIn("With a capture clock: seconds the whole capture may take.  10 if not given, for either",
                      props["timeout"]["description"])
        self.assertIn("The capture ends after `timeout` (10 seconds if not given) even if it has fewer samples",
                      props["clock"]["description"])

    def test_a_file_that_cannot_be_written_after_the_capture(self):
        # the agent is told where the capture is all the same
        tools = mcp.Tools(fake.FakeBus(instant=True), True, self.dir.name)
        asked = mcp.Asked(threading.Event(), None, lambda message: None)
        save = Capture.save
        with tempfile.TemporaryDirectory() as folder:
            first, other = os.path.join(folder, "a.sr"), os.path.join(folder, "late.vcd")

            def failing(cap, path, names=None):
                if path == other:
                    raise OSError("the disk is full")
                return save(cap, path, names)
            with unittest.mock.patch.object(Capture, "save", failing):
                r = tools.call("capture", {"seconds": 0.001, "save_as": [first, other]}, asked)
        lb16 = [os.path.join(self.dir.name, n) for n in os.listdir(self.dir.name)]
        self.assertEqual((r["isError"], len(lb16)), (True, 1))
        self.assertEqual(r["content"][0]["text"], "The capture is saved as %s and %s, but %s could not be written: the "
                                                  "disk is full" % (lb16[0], first, other))
        # an overflow on a USB 2.0 link says so
        bus = fake.FakeBus(instant=True, overflow_at=500)
        bus.link = 2
        r = mcp.Tools(bus, True, self.dir.name).call("capture", {"seconds": 0.01}, asked)
        self.assertIn("The link runs at 480 Mbit/s, not USB 3's 5 Gbit/s: a USB 3 port and cable carry about nine "
                      "times as much.", json.loads(r["content"][0]["text"])["overflow"])


class TheCommandLine(unittest.TestCase):
    def run_it(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--fake"] + list(words))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_a_capture_with_a_trigger(self):
        probes = samples_of(*zip(*fake.probe_runs(0, 100000, 1e6)))
        want = trigger_sample(probes, T.parse("A1=f then A0=r"))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "t.lb16")
            code, out, err = self.run_it("capture", "--rate", "1M", "--time", "20ms", "--trigger", "A1=f then A0=r",
                                         "--before", "2ms", "--glitch", "A=50ns", path)
            self.assertEqual(code, 0, err)
            self.assertIn("trigger (A1=f then A0=r): %.9g s into the capture (sample %d)" % (want / 1e6, want), out)
            self.assertIn("Waiting for the trigger (A1=f then A0=r).  Ctrl-C stops.\nTriggered.\n", err)
            self.assertIn("glitch filter: A0 50 ns, A1 50 ns", out)
            back = Capture.load(path)
            self.assertEqual((back.trigger, back.samples), (want, want + 20000))
            code, out, err = self.run_it("capture", "--rate", "1M", "--time", "20ms", "--pi", "i2c,ce1", path)
            self.assertEqual(code, 0, err)
            self.assertIn("the Pi's lines: A0 is GPIO2 (SDA), A1 is GPIO3 (SCL), B0 is GPIO7 (CE1)", out)
            back = Capture.load(path)
            self.assertEqual((back.names[0], back.names[1], back.names[2], back.names[8]), ("SDA", "SCL", "A2", "CE1"))
            # the lines' names in the trigger, and a name of one's own beside them (the stand-in's Pi lines are low)
            code, out, err = self.run_it("capture", "--rate", "1M", "--time", "20ms", "--pi", "i2c", "--name", "B3=IRQ",
                                         "--trigger", "SCL=0 SDA=0 IRQ=0", "--timeout", "1", path)
            self.assertEqual(code, 0, err)
            self.assertIn("trigger (A0=0 A1=0 B3=0): 0 s into the capture (sample 0)", out)
            self.assertEqual(Capture.load(path).names[:2] + Capture.load(path).names[11:12], ["SDA", "SCL", "IRQ"])
            code, out, err = self.run_it("capture", "--pi", "i2c", "--name", "B3=SDA", path)
            self.assertEqual((code, err.strip()), (1, "A0 and B3 would both be named SDA."))
        code, out, err = self.run_it("capture", "--rate", "1M", "--time", "20ms", "--trigger", "A0=f", "--before", "7", "--json")
        got = json.loads(out[out.index("{"):])
        self.assertEqual((code, got["trigger_sample"], got["trigger_seconds"], got["samples"]), (0, 7, 7e-6, 20007))
        code, out, err = self.run_it("capture", "--rate", "1M", "--time", "20ms", "--trigger", "B3=r", "--timeout", "50ms")
        self.assertEqual(code, 3)                                   # saved, but not what was asked for
        self.assertIn("Waiting for the trigger (B3=r), 50 ms at the most.  Ctrl-C stops.", err)
        self.assertNotIn("Triggered.", err)
        self.assertIn("NOT TRIGGERED (B3=r): these are the last 2000 samples the board saw.", out)
        self.assertIn("the time given with --timeout had passed", out)
        code, out, err = self.run_it("capture", "-n", "300", "--clock", "A1=r", "--trigger", "A0=1 for 3", "--before", "10")
        self.assertEqual(code, 0)
        self.assertIn("a sample at each edge A1=r: 310 samples", out)
        self.assertIn("trigger (A0=1 for 3): sample 10", out)

    def test_names_find_detect_and_plot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "n.lb16")
            code, out, err = self.run_it("capture", "--rate", "2M", "--time", "50ms", "--name", "A0=SDA", "--name", "A1=SCL", path)
            self.assertEqual(code, 0, err)
            self.assertIn("  A0 (SDA): 292 changes", out)
            self.assertIn("  A1 (SCL): ", self.run_it("show", path)[1])
            self.assertIn("  A1 (SCL): 1434 changes (717 rising, 717 falling)", self.run_it("measure", path, "SCL")[1])
            code, out, err = self.run_it("find", path, "SDA=f SCL=1", "--limit", "2")
            self.assertEqual(code, 0, err)
            self.assertIn("\"A0=f A1=1\" found 15 times from 0 s to 0.05 s (a sample is 500 ns):\n    0.000020000 s\n", out)
            self.assertIn("  ... and 13 more (--limit lists more).", out)
            code, out, err = self.run_it("find", path, "SCL=1 for 100us", "--limit", "1")
            self.assertIn("    0.001480000 s   held 112.5 us, from 0.001380500 s\n", out)
            self.assertIn("  held: the shortest 112 us, the median 112.5 us, the longest 17.288 ms", out)
            self.assertIn("pulse 5 us, from 0.000025500 s", self.run_it("find", path, "A1=0 pulse ..6us")[1])
            self.assertIn("5.5 us after the first stage (at 0.000020000 s)", self.run_it("find", path, "SDA=f SCL=1 then SCL=f")[1])
            self.assertEqual(json.loads(self.run_it("find", path, "A0=r", "--json", "--limit", "0")[1])["found"], [])
            code, out, err = self.run_it("detect", path)
            self.assertEqual(code, 0, err)
            self.assertIn("  I2C      SDA A0, SCL A1; 100 kHz; 78 bytes\n             decode: i2c scl=A1 sda=A0\n", out)
            self.assertIn("  steady: A2 A3 A4 A5 A6 A7 B0 B1 B2 B3 B4 B5 B6 B7 low\n", out)
            self.assertIn("--name A0=SDA,A1=SCL", out)
            self.assertEqual(json.loads(self.run_it("detect", path, "--json")[1])["buses"][0]["bus"], "i2c")
            code, out, err = self.run_it("plot", path, "--to", "1ms", "--mark", "0.2ms,0.4ms", "--mark", "5ms")
            self.assertEqual(code, 0, err)
            png = os.path.join(folder, "n.png")
            self.assertIn("wrote %s: A0 A1 from 0 s to 0.001 s" % png, out)
            self.assertIn("  mark 1 at 0.0002 s\n  mark 2 at 0.0004 s\n", out)
            self.assertNotIn("mark 3", out)
            with open(png, "rb") as f:
                self.assertEqual(pixels(f.read())[0], 1200)
            code, out, err = self.run_it("plot", path, "SCL", "-o", os.path.join(folder, "s.png"), "--width", "600", "--json")
            got = json.loads(out)
            self.assertEqual((got["channels"], got["width"], got["busy"], got["file"]), (["A1"], 600, ["A1"], os.path.join(folder, "s.png")))
            self.assertIn("shown as bands", self.run_it("plot", path)[1])
            # what is refused, before anything is captured
            for words, why in ((("--name", "A0=SCL,A1=SCL"), "A0 and A1 would both be named SCL."),
                               (("--name", "A0=then"), "then cannot be a channel's name"),
                               (("--trigger", "SDA=f"), "SDA is not a channel")):
                code, out, err = self.run_it("capture", os.path.join(folder, "no.lb16"), *words)
                self.assertEqual((code, why in err), (1, True), words)
            self.assertEqual(sorted(os.listdir(folder)), ["n.lb16", "n.png", "s.png"])
            code, out, err = self.run_it("capture", "--rate", "2M", "--time", "5ms", "--name", "A0=SDA,A1=SCL",
                                         "--trigger", "SDA=f SCL=1", path)
            self.assertIn("trigger (A0=f A1=1): ", out)
            self.assertEqual((code, self.run_it("find", path, "MOSI=r")[2].strip()),
                             (0, "MOSI is not a channel: they are A0 ... A7 and B0 ... B7, or by name SDA (A0), SCL (A1)."))

    def test_thresholds_as_json(self):
        code, out, err = self.run_it("threshold", "1.65", "0.9", "--json")
        self.assertEqual((code, json.loads(out)), (0, {"threshold_a": 1.65, "threshold_b": 0.9}))
        self.assertEqual(json.loads(self.run_it("threshold", "1.2", "--json")[1]), {"threshold_a": 1.2, "threshold_b": 1.2})

    def test_times_read_off_a_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "c.lb16")
            self.assertEqual(self.run_it("capture", "--rate", "2M", "--time", "50ms", path)[0], 0)
            code, out, err = self.run_it("measure", path, "A1", "--to", "10ms", "--list", "3")
            self.assertEqual(code, 0, err)
            self.assertIn("2 MS/s (a sample is 500 ns), from 0 s to 0.01 s", out)
            self.assertIn("A1: 478 changes (239 rising, 239 falling); high at the start", out)
            self.assertIn("period    9.5 us ... 117.5 us, the median 10 us  (100 kHz, high 56.08 % of the time)", out)
            self.assertIn("low for   4.5 us ... 5.5 us, the median 5 us", out)
            self.assertIn("  0.000025500 s  A1 falls", out)
            self.assertIn("... and 475 more changes.", out)
            code, out, err = self.run_it("measure", path, "--json")
            got = json.loads(out)
            self.assertEqual(([c["channel"] for c in got["channels"]], got["channels"][1]["frequency"]), (["A0", "A1"], 100e3))
            self.assertEqual(self.run_it("measure", path, "B0", "B1")[1].count("0 changes"), 2)
            self.assertIn("no channel changes there", self.run_it("measure", path, "--from", "5ms", "--to", "15ms")[1])
            code, out, err = self.run_it("measure", path, "Z9")
            self.assertEqual((code, err.strip()), (1, "Z9 is not a channel: they are A0 ... A7 and B0 ... B7."))
            code, out, err = self.run_it("measure", os.path.join(folder, "none.lb16"))
            self.assertEqual((code, err.strip()), (1, "%s: there is no such file." % os.path.join(folder, "none.lb16")))

    def test_the_readmes_first_commands(self):
        # README.md's "The command line", in its order: a capture into both files, then the reading of it
        with tempfile.TemporaryDirectory() as folder:
            lb16, sr = os.path.join(folder, "bus.lb16"), os.path.join(folder, "bus.sr")
            code, out, err = self.run_it("capture", lb16, sr, "--rate", "10M", "--time", "0.1", "-a", "1.65", "-b", "0.9")
            self.assertEqual(code, 0, err)
            self.assertIn("wrote %s\nwrote %s" % (lb16, sr), out)
            self.assertEqual(self.run_it("capture", os.path.join(folder, "reset.sr"), "--rate", "200M", "--time", "1ms",
                                         "--trigger", "A1=f")[0], 0)
            code, from_lb16, err = self.run_it("measure", lb16)
            self.assertEqual(code, 0, err)
            code, from_sr, err = self.run_it("measure", sr)             # the same, read off the sigrok session
            self.assertEqual(code, 0, err)
            self.assertEqual(from_sr.replace(sr, "F"), from_lb16.replace(lb16, "F"))
            self.assertIn("A1: ", from_sr)
            code, out, err = self.run_it("show", sr)
            self.assertEqual(code, 0, err)
            self.assertIn("10 MS/s for 0.1 s: 1000000 samples", out)
            code, out, err = self.run_it("convert", sr, os.path.join(folder, "back.lb16"))
            self.assertEqual(code, 0, err)
            self.assertEqual(Capture.load(os.path.join(folder, "back.lb16")).samples, 1000000)

    def test_a_capture_too_long_for_an_lb16_file(self):
        with tempfile.TemporaryDirectory() as folder:
            lb16 = os.path.join(folder, "long.lb16")
            for words in (("--rate", "200M", "--time", "30"), ("--rate", "200M", "--time", "20", "--trigger", "A0=r"),
                          ("-n", "5000000000"), ("--fast", "A0-A3", "--time", "6")):
                code, out, err = self.run_it("capture", lb16, *words)
                self.assertEqual(code, 1, words)
                self.assertIn("An .lb16 file holds 2^32 samples at most", err, words)
            self.assertIn("(21.47 s at 200 MS/s)", self.run_it("capture", lb16, "--rate", "200M", "--time", "30")[2])
            self.assertEqual(os.listdir(folder), [])                    # refused before anything was captured
            self.assertEqual(self.run_it("capture", os.path.join(folder, "long.sr"), "--rate", "200M", "--time", "30",
                                         "--trigger", "B3=r", "--timeout", "10ms")[0], 3)       # other files are not held to it
            # (3: the trigger did not come, so the capture is the last samples the board saw)
            # a capture of no set length that runs past it is written as .sr in the .lb16 file's place
            with unittest.mock.patch.object(C, "LB16_SAMPLES", 1000):
                cap = Capture(1e6, array("H", [0, 1, 0]), array("Q", [1500, 10, 20]))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    written = cli.save(cap, [lb16, os.path.join(folder, "long.csv")])
            self.assertEqual(written, [os.path.join(folder, "long.sr"), os.path.join(folder, "long.csv")])
            self.assertIn("Written as %s in its place." % os.path.join(folder, "long.sr"), err.getvalue())
            self.assertEqual(Capture.load(os.path.join(folder, "long.sr")).samples, 1530)

    def test_the_skill_comes_with_the_software(self):
        code, out, err = self.run_it("skill")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("---\nname: logic-boole-16\n"))
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "logic-boole-16", "SKILL.md")
            code, said, err = self.run_it("skill", "--install", folder)
            self.assertEqual((code, said), (0, "wrote %s.\n" % path))
            with open(path) as f:
                self.assertEqual(f.read(), out)
            self.assertEqual(self.run_it("skill", "--install", folder)[1], "%s is there already, as this version has it.\n" % path)
            with open(path, "w") as f:
                f.write("an older one")
            self.assertEqual(self.run_it("skill", "--install", folder)[1], "updated %s.\n" % path)
            with unittest.mock.patch.dict(os.environ, {"HOME": folder}):           # Claude Code's folder by default
                code, said, err = self.run_it("skill", "--install")
            self.assertIn("wrote %s" % os.path.join(folder, ".claude", "skills", "logic-boole-16", "SKILL.md"), said)
            self.assertIn("Claude Code finds it in its next session.", said)

    def test_info_names_the_revision(self):
        bus = fake.FakeBus(instant=True)
        B.connect(backend=bus).close()                  # loaded, as by an earlier command
        with unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            code, out, err = self.run_it("info")
            self.assertIn("  board V1.0.0: thresholds from 0.3 V to 3 V\n", out)
            self.assertEqual(json.loads(self.run_it("info", "--json")[1])["threshold_range"], [0.3, 3.0])

    path = Decoders.path

    def test_decoders(self):
        Decoders.setUp(self)                # a stand-in for sigrok-cli, and a.lb16
        capture = self.path("a.lb16")
        code, out, err = self.run_it("decode", capture, "i2c", "scl=A1", "sda=A0", "--limit", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("1 MS/s, %d samples.  i2c scl=A1 sda=A0 found 6:" % self.cap.samples, out)
        self.assertIn("  the trigger is at 0.000077000 s (sample 77)", out)
        self.assertIn("    0.000061000 s  address-write  Address write: 16", out)
        self.assertIn("  ... and 4 more.", out)
        self.assertEqual(self.run_it("decode", capture, "i2c scl=A1 sda=A0", "--limit", "2")[:2], (code, out))  # quoted
        code, out, err = self.run_it("decode", capture, "i2c", "scl=A1", "sda=A0", "eeprom24xx", "--only", "ack,byte-write")
        self.assertIn("    0.000061000 s  eeprom24xx.byte-write  Byte write (addr=4C): \"x\"", out)
        self.assertIn("    0.000221000 s  i2c.ack                ACK", out)
        code, out, err = self.run_it("decode", capture, "i2c", "scl=A1", "sda=A0", "--from", "201us", "--to", "300us", "--json")
        got = json.loads(out)
        self.assertEqual(([a["text"] for a in got["annotations"]], got["total"]), (["Write", "ACK", "Data write: 4C"], 3))
        code, out, err = self.run_it("decode", capture, "i2c", "scl=A1")
        self.assertEqual((code, out, err.strip()), (1, "", "i2c needs: sda=CHANNEL (Serial data line)."))
        out_png = self.path("a.png")
        code, out, err = self.run_it("plot", capture, "--decode", "i2c scl=A1 sda=A0", "-o", out_png)
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"  [1-6] decoded results drawn")
        notes = P.decoded(capture, ["i2c scl=A1 sda=A0", "i2c scl=A0 sda=A1"], 0.0001, 0.0003, self.cap.seconds)
        self.assertEqual(sorted({n["lane"] for n in notes}), ["i2c", "i2c 2"])
        self.assertTrue(all(n["end"] >= 0.0001 for n in notes))     # what ends before the part is left out
        self.assertIn("Data write: 4C", [n["text"] for n in notes])  # begins in it
        self.assertIn("Address write: 16", [n["text"] for n in notes])   # began before it and goes into it
        code, out, err = self.run_it("decoders")
        self.assertIn("  i2c                  Inter-Integrated Circuit\n", out)
        self.assertIn("3 decoders.", out)
        code, out, err = self.run_it("decoders", "uart")
        self.assertIn("  channel  rx=CHANNEL   UART receive line (may be left out)", out)
        self.assertIn("  option   stop_bits=1.0   Stop bits: 0.0, 0.5, 1.0, 1.5", out)
        self.assertEqual(json.loads(self.run_it("decoders", "i2c", "--json")[1])["outputs"], ["i2c"])
        self.assertEqual(json.loads(self.run_it("decoders", "--json")[1])[1], {"id": "i2c", "name": "Inter-Integrated Circuit"})
        code, out, err = self.run_it("decoders", "spi")
        self.assertEqual(code, 1)
        self.assertIn("no decoder named spi", err)

    def test_the_agents_server_as_a_program(self):
        # as an agent's program starts it: the standard output carries the protocol and nothing else
        software = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        with tempfile.TemporaryDirectory() as folder:
            p = subprocess.Popen([sys.executable, "-m", "logicboole", "--fake", "mcp", "--dir", folder], cwd=software,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for n, (name, args) in enumerate((("read_inputs", {}), ("capture", {"seconds": 0.01}))):
                    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": n, "method": "tools/call",
                                              "params": {"name": name, "arguments": args}}).encode() + b"\n")
                    p.stdin.flush()
                    answer = json.loads(p.stdout.readline())
                    self.assertEqual((answer["id"], answer["result"]["isError"]), (n, False))
                self.assertEqual(os.listdir(folder), [os.path.basename(json.loads(answer["result"]["content"][0]["text"])["file"])])
                out, err = p.communicate(timeout=30)        # the input ends, and so does the program
                self.assertEqual((p.returncode, out), (0, b""))
                self.assertIn(b"firmware: ", err)           # what the board's loading says goes to the other stream
            finally:
                if p.poll() is None:
                    p.kill()
                    p.communicate()

    def test_what_cannot_be_done_says_why(self):
        for words, why in ((("capture", "--trigger", "B9=r"), "B9 is not a channel"),
                           (("capture", "--clock", "A1"), "With --clock the capture's length is a number of samples"),
                           (("capture", "--trigger", "A0=1 for 1us", "--clock", "A1", "-n", "5"), "no sample rate"),
                           (("capture", "--glitch", "2us"), "10 ns"),
                           (("capture", "--pi", "gpio4"), "gpio4 is not one of the Pi's lines")):
            code, out, err = self.run_it(*words)
            self.assertNotEqual(code, 0, words)
            self.assertIn(why, out + err, words)


    def test_exit_codes(self):
        # info: not 0 without a board, with --json or not, and the JSON is printed all the same
        bus = fake.FakeBus(stage="none")
        with unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            self.assertEqual(self.run_it("info")[:2], (1, "No board on USB.\n"))
            code, out, err = self.run_it("info", "--json")
            self.assertEqual((code, json.loads(out)["state"]), (1, "none"))
        self.assertEqual(self.run_it("info", "--json")[0], 0)
        # a capture that is saved but is not what was asked for: 3
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "o.lb16")
            for link, note in ((3, False), (2, True)):
                bus = fake.FakeBus(overflow_at=5000)
                bus.link = link
                with unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
                    code, out, err = self.run_it("capture", "--rate", "1M", "--time", "20ms", path)
                self.assertEqual(code, 3, err)
                self.assertIn("OVERFLOW: ", out)
                self.assertEqual("The link runs at 480 Mbit/s, not USB 3's 5 Gbit/s: a USB 3 port and cable carry "
                                 "about nine times as much." in out, note)
                self.assertEqual(Capture.load(path).samples, 5000)
                os.remove(path)
        self.assertIn("The exit status is 0 for a capture as asked for, 1 if something went wrong",
                      " ".join(self.run_it("capture", "-h")[1].split()))

    def test_ctrl_c_anywhere_is_one_line(self):
        for words, where, name in ((["selftest"], cli, "selftest"), (["show", "x.lb16"], Capture, "load"),
                                   (["load"], B, "connect"), (["decode", "x.lb16", "i2c"], D, "decode"),
                                   (["check"], B, "look")):
            with unittest.mock.patch.object(where, name, side_effect=KeyboardInterrupt):
                code, out, err = self.run_it(*words)
            self.assertEqual((code, err.splitlines()[-1]), (130, "Stopped (Ctrl-C)."), words)
            self.assertNotIn("Traceback", err)

    def test_what_is_refused_before_a_capture(self):
        bus = fake.FakeBus()
        with tempfile.TemporaryDirectory() as folder, unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            os.mkdir(os.path.join(folder, "d.sr"))
            for words, said in (([os.path.join(folder, "no", "x.lb16")], "x.lb16 cannot be saved: there is no folder "),
                                ([os.path.join(folder, "x.txt")], "a capture is saved as .lb16, .sr, .vcd or .csv"),
                                ([os.path.join(folder, "d.sr")], "d.sr is a folder: give the name of a file in it."),
                                (["--trigger", "A0=r", "--before", "0.05"],
                                 "--before 0.05: write 50ms, or a whole number of samples."),
                                (["--trigger", "A0=r", "--before", "lots"], "--before lots: write 50ms"),
                                (["-a", "0.1"], "A threshold of 0.1 V is outside what the board takes, 0.3 V to 3 V: "
                                                "set it to about half your logic voltage, for example 1.65 V for 3.3 V "
                                                "logic."),
                                (["-b", "3.3"], "A threshold of 3.3 V is outside")):
                code, out, err = self.run_it("capture", *words)
                self.assertEqual(code, 1, words)
                self.assertIn(said, err, words)
            self.assertNotIn(0xC2, [r[0] for r in bus.requests])    # no capture was begun
            self.assertEqual(os.listdir(folder), ["d.sr"])
            code, out, err = self.run_it("capture", "--glitch", "5", os.path.join(folder, "g.lb16"))
            self.assertEqual(code, 2)
            self.assertIn("argument --glitch: 5 is not a time: a glitch filter is 20ns, 1us and the like, or off.",
                          err)
            code, out, err = self.run_it("threshold", "5")
            self.assertEqual(code, 1)
            self.assertIn("A threshold of 5 V is outside what the board takes", err)

    def test_small_things(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "p.lb16")
            code, out, err = self.run_it("capture", "--rate", "1M", "--time", "2ms", "--test", path)
            self.assertEqual(code, 0, err)                          # "--test FILE": the file, and the shift is 8
            self.assertIn("every word is the test pattern's", out)
            self.assertEqual(Capture.load(path).lengths[0], 256)
            self.run_it("capture", "--rate", "1M", "--time", "2ms", "--test", "3", path)
            self.assertEqual(Capture.load(path).lengths[0], 8)
            code, out, err = self.run_it("capture", "--rate", "1M", "--time", "2ms")
            self.assertEqual(code, 0)
            self.assertIn("No FILE given: the capture is shown, not saved.\n", err)
            # the board leaves USB part-way: what arrived is saved, and the problem printed
            bus = fake.FakeBus()
            B.connect(backend=bus).close()
            bus.leave_after = 1
            with unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
                code, out, err = self.run_it("capture", "--rate", "10M", "--time", "1", path)
            self.assertEqual(code, 1)
            self.assertIn("WRONG: the board left USB after ", out)
            self.assertIn("wrote %s" % path, out)
            bus.stage, bus.leave_after = "boot", None               # plugged in again
            bus.leave_after = 1
            with unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
                code, out, err = self.run_it("capture", "--rate", "10M", "--time", "1", "--trigger", "A1=f", path)
            self.assertEqual(code, 1)
            self.assertIn("The board left USB (unplugged, the cable, or a hub) during the capture", err)

    def test_check_says_what_it_did_on_the_way(self):
        bus = fake.FakeBus(instant=True)
        B.connect(backend=bus).close()
        bus.leave_running()
        bus.firmware_version = 1
        with unittest.mock.patch.object(fake, "FakeBus", lambda: bus):
            code, out, err = self.run_it("check")
            self.assertEqual(code, 0, out + err)
            self.assertIn("\n" + B.STALE + "\n", out)
            self.assertIn("\nThe board runs firmware version 1; this software comes with version 2: loading it.\n", out)
            self.assertNotIn("firmware: ", out)
            self.assertNotIn("FPGA: ", out)
            bus.leave_running()
            code, out, err = self.run_it("info")
            self.assertEqual((code, err), (0, B.STALE + "\n"))
            self.assertTrue(out.startswith("Ready"))
            bus.leave_running(held=True)
            code, out, err = self.run_it("info")
            self.assertEqual((code, out.splitlines()[0]), (1, B.IN_USE))


class ChannelNames(unittest.TestCase):
    def test_names_are_checked(self):
        self.assertEqual(C.parse_names("A0=SDA, a1=scl B7=RESET_N"), {0: "SDA", 1: "scl", 15: "RESET_N"})
        for text, why in (("A9=X", "given as A0=SDA"), ("SDA", "given as A0=SDA"), ("A0=", "1 to 24 letters"),
                          ("A0=two words", "given as A0=SDA"), ("A0=a" * 1 + "b" * 24, "1 to 24 letters"),
                          ("A0=B1", "means something else"), ("A0=then", "means something else"),
                          ("A0=In", "means something else"), ("A0=X,A0=Y", "A0 is named twice")):
            with self.assertRaises(ValueError) as caught:
                C.parse_names(text)
            self.assertIn(why, str(caught.exception), text)
        self.assertEqual(C.parse_names("A0=A0"), {0: "A0"})         # a channel's own id: no name

    def test_a_capture_with_names(self):
        cap = capture_of(1e6, 100, {0: (0, [10, 20, 30, 40]), 1: (1, [50])})
        cap.set_names("A0=CLK,A1=EN")
        self.assertEqual((cap.channel("clk"), cap.channel("A1"), cap.channel("en"), cap.label(0), cap.label(2)),
                         (0, 1, 1, "A0 (CLK)", "A2"))
        with self.assertRaises(ValueError) as caught:
            cap.channel("MISO")
        self.assertEqual(str(caught.exception), "MISO is not a channel: they are A0 ... A7 and B0 ... B7, or by name "
                                                "CLK (A0), EN (A1).")
        with self.assertRaises(ValueError) as caught:
            cap.set_names("B0=clk")
        self.assertEqual(str(caught.exception), "A0 and B0 would both be named clk.")
        self.assertEqual(cap.names[8], "B0")                        # refused whole
        m = cap.measure(["CLK", "en"])
        self.assertEqual([(c["channel"], c["name"], c["changes"]) for c in m["channels"]], [("A0", "CLK", 4), ("A1", "EN", 1)])
        self.assertEqual([c.get("name") for c in cap.summary()[:3]], ["CLK", "EN", None])
        with tempfile.TemporaryDirectory() as folder:
            for ending in ("lb16", "sr"):
                path = os.path.join(folder, "n." + ending)
                cap.save(path)
                self.assertEqual(Capture.load(path).names[:3], ["CLK", "EN", "A2"], ending)

    def test_a_trigger_given_by_names(self):
        cap = capture_of(1e6, 10, {})
        cap.set_names("A0=SDA,A1=SCL,B0=go")
        self.assertEqual(search.with_ids(cap, "SDA=f scl=1 then GO=r for 1us"), "A0=f A1=1 then B0=r for 1us")
        self.assertEqual(search.with_ids(cap, "in=r then A0=0 pulse ..50ns"), "in=r then A0=0 pulse ..50ns")
        with self.assertRaises(ValueError):
            search.with_ids(cap, "MOSI=r")


class Finding(unittest.TestCase):
    # A0 at 1 MS/s: high 10 samples, low 2 (a glitch), high 18, low 10, high 5, low 1 (a glitch), high 4 to the end
    CAP = staticmethod(lambda: capture_of(1e6, 50, {0: (1, [10, 12, 30, 40, 45, 46])}))

    def times(self, found):
        return [round(f["time"] * 1e6) for f in found["found"]]

    def held(self, found):
        return [(round(f["held"]["start"] * 1e6), round(f["held"]["end"] * 1e6), f["held"].get("before", False),
                 f["held"].get("to_end", False)) for f in found["found"]]

    def test_pulses_and_levels_worked_out_by_hand(self):
        cap = self.CAP()
        f = search.find(cap, "A0=0 pulse ..3us")
        self.assertEqual((f["find"], f["total"], self.times(f)), ("A0=0 pulse ..3us", 2, [12, 46]))
        self.assertEqual(self.held(f), [(10, 12, False, False), (45, 46, False, False)])
        self.assertEqual(f["held"], {"min": 1e-6, "median": 1.5e-6, "max": 2e-6})
        self.assertEqual(self.times(search.find(cap, "A0=0 pulse 5us..")), [40])
        f = search.find(cap, "A0=1 for 5us")                        # once a stretch: the last 4 are too short
        self.assertEqual(self.times(f), [4, 16, 44])
        self.assertEqual(self.held(f), [(0, 10, True, False), (12, 30, False, False), (40, 45, False, False)])
        self.assertEqual(f["held"], {"min": 5e-6, "median": 10e-6, "max": 18e-6})
        f = search.find(cap, "A0=1")
        self.assertEqual(self.held(f), [(0, 10, True, False), (12, 30, False, False), (40, 45, False, False),
                                        (46, 50, False, True)])
        f = search.find(cap, "A0=f")
        self.assertEqual((self.times(f), "held" in f, "held" in f["found"][0]), ([10, 30, 45], False, False))
        self.assertEqual(self.times(search.find(cap, "A0=e")), [10, 12, 30, 40, 45, 46])

    def test_a_part_of_the_capture_a_limit_and_names(self):
        cap = self.CAP()
        self.assertEqual(self.times(search.find(cap, "A0=f", start=20e-6)), [30, 45])
        self.assertEqual(self.times(search.find(cap, "A0=f", end=40e-6)), [10, 30])
        self.assertEqual(self.times(search.find(cap, "A0=f", start=11e-6)), [30, 45])      # no edge at the cut
        f = search.find(cap, "A0=0", start=35e-6)
        self.assertEqual(self.held(f), [(35, 40, True, False), (45, 46, False, False)])
        f = search.find(cap, "A0=e", limit=2)
        self.assertEqual((f["total"], len(f["found"])), (6, 2))
        cap.set_names("A0=CLK")
        self.assertEqual(self.times(search.find(cap, "clk=f")), [10, 30, 45])
        with self.assertRaises(T.TriggerError) as caught:
            search.find(cap, "in=r")
        self.assertIn("TRIG IN is not in a saved capture", str(caught.exception))
        with self.assertRaises(ValueError):
            search.find(cap, "A0=f", start=30e-6, end=20e-6)
        with self.assertRaises(ValueError):
            search.find(cap, "MOSI=f")

    def test_stages_one_after_the_other(self):
        # CS (A2) falls at 10 and 60; the clock (A3) rises 4 samples after the first and 7 after the second
        cap = capture_of(1e6, 100, {2: (1, [10, 40, 60, 90]), 3: (0, [14, 16, 18, 20, 67, 69])})
        f = search.find(cap, "A2=f then A3=r")
        self.assertEqual([(round(x["first"] * 1e6), round(x["time"] * 1e6), round(x["span"] * 1e6)) for x in f["found"]],
                         [(10, 14, 4), (60, 67, 7)])

    def test_what_the_trigger_would_catch_on_the_stand_in(self):
        with B.connect(backend=fake.FakeBus(instant=True), log=None) as board:
            cap = board.capture(rate=2e6, seconds=0.05)
        probes = samples_of(cap.values, cap.lengths)
        for text in ("A0=f A1=1", "A1=f then A0=r", "A1=1 for 20us"):
            want = trigger_sample(probes, T.parse(text, 2e6))         # the trigger's model finds the first
            self.assertEqual(search.find(cap, text)["found"][0]["sample"], want, text)
        f = search.find(cap, "A1=1 for 100us")
        self.assertEqual((f["total"], round(f["held"]["median"] * 1e6, 1)), (12, 112.5))


class Detecting(unittest.TestCase):
    def test_buses_and_signals_made_here(self):
        rate = 10e6
        cs, clk, mosi = spi_edges(1000, 5, [[0x9F, 0x00, 0x00], [0x03, 0x12, 0x34], [0x05, 0xA5]])
        lines = {4: (1, cs), 5: (0, clk), 6: (0, mosi),
                 10: (1, uart_edges(5000, rate / 115200, b"Hello, world")),
                 8: (0, list(range(100, 200000, 100))),              # 50 kHz
                 15: (1, [])}
        found = DT.detect(capture_of(rate, 200000, lines))
        by = {b["bus"]: b for b in found["buses"]}
        self.assertEqual(sorted(by), ["spi", "uart"])
        spi, uart = by["spi"], by["uart"]
        self.assertEqual((spi["cs"], spi["clk"], spi["mosi"], spi["mode"], spi["bytes"]), ("A4", "A5", "A6", 0, 8))
        self.assertEqual(spi["clock_hz"], 1e6)
        self.assertEqual(spi["decode"], "spi clk=A5 cs=A4 mosi=A6 cpol=0 cpha=0")
        self.assertEqual(spi["names"], "A5=SPI_CLK,A4=SPI_CS,A6=SPI_MOSI")
        self.assertEqual((uart["rx"], uart["baudrate"], uart["bytes"], uart["decode"]),
                         ("B2", 115200, 12, "uart rx=B2 baudrate=115200"))
        ch = {c["channel"]: c for c in found["channels"]}
        self.assertEqual((ch["A4"]["kind"], ch["B2"]["kind"], ch["B0"]["kind"]), ("spi", "uart", "clock"))
        self.assertAlmostEqual(ch["B0"]["frequency"], 50e3)
        self.assertAlmostEqual(ch["B0"]["duty"], 0.5, 3)
        self.assertEqual((ch["B7"]["kind"], ch["B7"]["level"], ch["A0"]["kind"], ch["A0"]["level"]), ("steady", 1, "steady", 0))

    def test_the_stand_ins_bus_with_names(self):
        with B.connect(backend=fake.FakeBus(instant=True), log=None) as board:
            cap = board.capture(rate=2e6, seconds=0.05)
        cap.set_names("A0=data")
        found = DT.detect(cap)
        self.assertEqual(found["buses"], [{"bus": "i2c", "sda": "A0", "scl": "A1", "bytes": 78, "clock_hz": 100e3,
                                           "decode": "i2c scl=A1 sda=A0", "names": "A0=SDA,A1=SCL"}])
        self.assertEqual(found["channels"][0], {"channel": "A0", "changes": 292, "name": "data", "kind": "i2c"})
        self.assertEqual(DT.detect(capture_of(1e6, 10, {}))["buses"], [])

    def test_serial_formats(self):
        # as the app's auto-detect finds them: 8N1, 8E1, 8O1, and 7E1 told from 8N1 by its parity bit
        rate, text = 10e6, b"Logic Boole 16: frame 2, 25.4 C\r\nOK?\r\n"
        for bits, parity, more in ((8, "none", ""), (8, "even", " parity=even"), (8, "odd", " parity=odd"),
                                   (7, "even", " data_bits=7 parity=even")):
            cap = capture_of(rate, 200000, {6: (1, uart_edges(1000, rate / 115200, text, bits, parity))})
            (uart,) = DT.detect(cap)["buses"]
            self.assertEqual((uart["data_bits"], uart["parity"], uart["bytes"], uart["decode"]),
                             (bits, parity, len(text), "uart rx=A6 baudrate=115200" + more))
        # 8N1 whose bytes all have an even number of ones stays 8N1: 7E1 needs a byte with its top bit set
        even = bytes(c for c in text if not bin(c).count("1") & 1) * 3
        cap = capture_of(rate, 200000, {6: (1, uart_edges(1000, rate / 115200, even))})
        self.assertEqual([(b["data_bits"], b["parity"]) for b in DT.detect(cap)["buses"]], [(8, "none")])

    def test_a_name_taken_by_an_earlier_bus_is_numbered(self):
        rate = 10e6
        lines = {10: (1, uart_edges(5000, rate / 115200, b"Hello, world")),
                 11: (1, uart_edges(9000, rate / 9600, b"Second line here"))}
        found = DT.detect(capture_of(rate, 400000, lines))
        self.assertEqual([b["names"] for b in found["buses"]], ["B3=UART", "B2=UART2"])


def pixels(png):
    """A PNG that plot() wrote, as (width, height, rows of palette indices)."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(png):
        n, = struct.unpack(">I", png[pos:pos + 4])
        kind, data = png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        assert struct.unpack(">I", png[pos + 8 + n:pos + 12 + n])[0] == zlib.crc32(kind + data) & 0xFFFFFFFF
        chunks[kind] = chunks.get(kind, b"") + data
        pos += 12 + n
    w, h, depth, color = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color, len(chunks[b"PLTE"])) == (8, 3, 3 * len(P.PALETTE))
    raw = zlib.decompress(chunks[b"IDAT"])
    assert len(raw) == h * (w + 1)
    return w, h, [raw[y * (w + 1) + 1:(y + 1) * (w + 1)] for y in range(h)]


class Plotting(unittest.TestCase):
    def test_a_picture_of_a_capture(self):
        cap = capture_of(1e6, 1000, {0: (0, list(range(10, 1000, 10))), 3: (1, [500])}, trigger=250)
        cap.set_names("A0=CLK")
        png, info = P.plot(cap, marks=[0.0001, 0.002], title="t")
        w, h, rows = pixels(png)
        self.assertEqual((w, h), (info["width"], info["height"]))
        self.assertEqual((info["channels"], info["names"], info["start"], info["end"]), (["A0", "A3"], ["CLK", "A3"], 0, 0.001))
        self.assertEqual(info["marks"], [{"mark": 1, "time": 0.0001}])            # the second is past the end
        self.assertEqual(info["busy"], [])
        seen = set(b"".join(rows))
        self.assertTrue({P.TRACE, P.MARK, P.TRIGGER, P.INK} <= seen)
        png, info = P.plot(cap, ["clk"], start=0.0006, end=0.0008, width=50)
        w, h, rows = pixels(png)
        self.assertEqual((w, info["channels"], info["marks"]), (400, ["A0"], []))      # 400 pixels at the least
        self.assertNotIn(P.TRIGGER, set(b"".join(rows)))                               # the trigger is not in it
        dense = capture_of(1e6, 100000, {0: (0, list(range(1, 100000)))})
        self.assertEqual(P.plot(dense)[1]["busy"], ["A0"])
        self.assertEqual(P.plot(dense, end=0.0002)[1]["busy"], [])
        self.assertEqual(P.plot(capture_of(1e6, 100, {}))[1]["channels"], list(CHANNELS))   # none changes: all
        for args, why in (({"start": 0.002}, "ends before it starts"), ({"channels": ["Z1"]}, "Z1 is not a channel")):
            with self.assertRaises(ValueError) as caught:
                P.plot(cap, **args)
            self.assertIn(why, str(caught.exception))

    def test_decoders_rows_and_the_letters(self):
        self.assertEqual(len(P.FONT), 96)
        self.assertEqual(P.FONT["A"], [0b01110, 0b10001, 0b10001, 0b11111, 0b10001, 0b10001, 0b10001])
        self.assertEqual(P.FONT[" "], [0] * 7)
        self.assertTrue(all(any(P.FONT[chr(c)]) for c in range(33, 127)))
        notes = [{"decoder": "i2c", "lane": "i2c", "start_sample": 10, "end_sample": 200, "class": "address-write",
                  "text": "Address write: 16", "texts": ["Address write: 16", "AW: 16", "16"]},
                 {"decoder": "i2c", "lane": "i2c", "start_sample": 200, "end_sample": 220, "class": "nack", "text": "NACK"},
                 {"decoder": "uart", "lane": "uart 2", "start_sample": 5000, "end_sample": 6000, "class": "rx-data", "text": "41"}]
        cap = capture_of(1e6, 1000, {0: (1, [10, 500]), 1: (1, [20, 600])})
        png, info = P.plot(cap, annotations=notes)
        self.assertEqual(info["annotations"], 2)                    # the third is outside the part shown
        seen = set(b"".join(pixels(png)[2]))
        self.assertTrue({P.BLUE, P.RED} <= seen)
        self.assertEqual(P.plot(cap)[1]["height"] + 2 * P.LANE * P.LANES, info["height"])  # a row for each decoder
        self.assertEqual([P.ticks(0, 1e-3, 5)[:2], P.ticks(0, 0.1, 9)[:2]], [(2e-4, 1e-6), (0.02, 1e-3)])
        self.assertEqual((P.time_text(0), P.time_text(2.5e-3), P.time_text(1.2e-5, 1e-6, 0)), ("0 s", "2.5 ms", "12 us"))


class TheGuide(unittest.TestCase):
    def test_the_skill_in_parts_for_an_agent_on_mcp(self):
        text = G.skill_text()
        heads = [h for h, _ in G.sections()]
        self.assertEqual(heads[0], "")
        self.assertIn("Recipes", heads)
        self.assertEqual("".join(part for _, part in G.sections()), text[text.index("\n---\n") + 5:])
        everything = G.guide()
        self.assertTrue(everything.startswith("This guide is the skill file"))
        self.assertIn("\"Recipes\"", everything)
        for t in mcp.TOOLS:                                         # every tool is in its table, but the guide
            if t["name"] != "guide":
                self.assertIn("`%s`" % t["name"], G.MCP_PREFACE.split("Times are")[0], t["name"])
        commands = cli.parser()._subparsers._group_actions[0].choices
        for line in G.MCP_PREFACE.splitlines():
            if line.startswith("| `logicboole "):
                self.assertIn(line.split()[2].rstrip("`"), commands, line)
        part = G.guide("recipes")
        self.assertTrue(part.startswith("## Recipes\n") and "## Reporting" not in part)
        self.assertIn("The sections: \"The board, in the terms", G.guide("nothing like it"))
        self.assertIn("## Finding moments", G.guide("FIND"))


class TheFastDesign(unittest.TestCase):
    """The second FPGA design: 4 inputs at 750 MS/s, 8 at 375 MS/s or 16 at 187.5 MS/s (logicboole/fast.py)."""

    def bus(self, **more):
        return fake.FakeBus(instant=True, **more)

    def loads(self, bus):
        return len([r for r in bus.requests if r[0] == 0xB3])

    def test_the_names_of_the_inputs(self):
        for text, name in (("A0-A3", "A0-A3"), ("a4..a7", "A4-A7"), (" b0 - b3 ", "B0-B3"), ("B4...B7", "B4-B7"),
                           ("a", "A"), ("pod b", "B"), ("B0-B7", "B"), ("all", "all"), (16, "all")):
            self.assertEqual(F.mode_of(text).name, name, text)
        self.assertEqual([(m.per, m.bits, m.shift, m.rate(750e6)) for m in map(F.mode_of, ("B0-B3", "B", "all"))],
                         [(4, 4, 8, 750e6), (2, 8, 8, 375e6), (1, 16, 0, 187.5e6)])
        self.assertEqual(F.mode_of("A4-A7").channels, ["A4", "A5", "A6", "A7"])
        self.assertEqual(sorted(F.BY_REGISTER), [0, 1, 2, 5, 6, 10, 14])
        for text in ("A1-A4", "C", "", "A0-A3,B"):
            with self.assertRaisesRegex(ValueError, "The fast design takes four inputs"):
                F.mode_of(text)

    def test_frames_worked_out_by_hand(self):
        # A4 ... A7, four samples a frame, the earliest in the lowest four bits: 4, 3, 2, 1 and then five frames
        # of nothing
        frames, counts = array("H", [0xAAAA, 0x1234, 0x0000]), array("Q", [3, 1, 5])
        values, lengths = F.sample_runs(frames, counts, F.MODES["A4-A7"])
        self.assertEqual((list(values), list(lengths)), ([0xA0, 0x40, 0x30, 0x20, 0x10, 0], [12, 1, 1, 1, 1, 20]))
        # pod B, two samples a frame: the earlier in the low byte.  The frame 0x55AA twice is AA 55 AA 55
        values, lengths = F.sample_runs(array("H", [0x55AA, 0x5555]), array("Q", [2, 1]), F.MODES["B"])
        self.assertEqual((list(values), list(lengths)), ([0xAA00, 0x5500, 0xAA00, 0x5500], [1, 1, 1, 3]))
        # all 16: a frame is a sample
        values, lengths = F.sample_runs(array("H", [7, 9]), array("Q", [4, 1]), F.MODES["all"], samples=4)
        self.assertEqual((list(values), list(lengths)), ([7], [4]))

    def test_a_limit_on_the_runs(self):
        # A frame whose four samples differ, 1000 times: 4000 runs with no limit.  With a limit of 100 the result
        # stops at about 100 runs and is right up to there
        frames, counts = array("H", [0x1234]), array("Q", [1000])
        full = F.sample_runs(frames, counts, F.MODES["A0-A3"])
        values, lengths = F.sample_runs(frames, counts, F.MODES["A0-A3"], limit=100)
        self.assertEqual(len(full[0]), 4000)
        self.assertTrue(100 <= len(values) < 108)
        self.assertEqual(list(values), list(full[0][:len(values)]))
        # frames that are alike throughout make one run however long, so the limit is not reached
        values, lengths = F.sample_runs(array("H", [0x1111, 0x2222]), array("Q", [10 ** 9, 5]), F.MODES["A0-A3"],
                                        limit=100)
        self.assertEqual((list(values), list(lengths)), ([1, 2], [4 * 10 ** 9, 20]))

    def test_a_fast_capture_stays_within_memory(self):
        # On a computer with 1 GB, the busy test pattern kept as samples stops at 1 GB / 80 = 13.4 million runs:
        # an early end, "size", and whole up to there.  The self-test's way (keep=False) is not limited
        bus = self.bus()
        with unittest.mock.patch.object(B, "memory", lambda: 1 << 30), unittest.mock.patch.object(B, "time", SlowClock()), \
                B.connect(backend=bus) as board:
            cap = board.capture_fast("A0-A3", seconds=0.02, fmt="raw", test=0)
            self.assertEqual((cap.ok, cap.stopped), (True, "size"))
            self.assertTrue((1 << 30) // 80 <= len(cap.values) < (1 << 30) // 80 + 8)
            self.assertTrue(cap.samples < 0.02 * 750e6)
            cap = board.capture_fast("A0-A3", seconds=0.02, fmt="raw", test=0, keep=False)
            self.assertEqual((cap.ok, cap.stopped, cap.samples), (True, None, 15000000))

    def test_samples_to_frames_and_back(self):
        rng = random.Random(16)
        for mode in F.MODES.values():
            for _ in range(20):
                runs = random_runs(rng, 200, 40)
                keep = mode.mask << mode.shift
                want = [v & keep for v in samples_of(*zip(*runs))]
                want = want[:len(want) - len(want) % mode.per]          # whole frames
                frames = list(F.frame_runs(runs, mode))
                self.assertEqual(sum(n for _, n in frames) * mode.per, len(want))
                values, lengths = F.sample_runs([f for f, _ in frames], [n for _, n in frames], mode)
                self.assertEqual(samples_of(values, lengths), want, mode)
                self.assertTrue(all(x != y for x, y in zip(values, values[1:])))    # no run in two pieces
                # and through the words of both formats
                for fmt in ("rle", "raw"):
                    data = coded(frames, fmt)
                    values, lengths = F.sample_runs(*W.runs_of(data, fmt, sum(n for _, n in frames)), mode)
                    self.assertEqual(samples_of(values, lengths), want, (mode, fmt))

    def test_a_capture_of_the_inputs(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            self.assertEqual(self.loads(bus), 1)
            before = len(bus.requests)
            cap = board.capture_fast("A0-A3", seconds=0.0004)
            self.assertEqual(self.loads(bus), 2)                    # the fast design was loaded
            codes = [r[0] for r in bus.requests[before:] if r[0] >= 0xC0 and r[0] < 0xD0]
            self.assertEqual(codes, [0xC0, 0xC3, 0xC2, 0xC4, 0xC4, 0xC5, 0xC3])     # as a standard capture's
            self.assertEqual((cap.ok, cap.samples, cap.rate, cap.overflow, cap.stopped),
                             (True, 300000, 750e6, False, None))
            self.assertEqual((cap.info["fast"], cap.info["inputs"]), ("A0-A3", ["A0", "A1", "A2", "A3"]))
            want = samples_of(*zip(*fake.probe_runs(0, 300000, 750e6)))
            self.assertEqual(samples_of(cap.values, cap.lengths), want)
            self.assertGreater(len(cap.values), 50)                 # the I2C bus on A1 and A0 is in it
            # a number of samples that is not whole frames, in both formats: those samples and no more
            for fmt in ("raw", "rle"):
                cap = board.capture_fast("a0..a3", samples=100001, fmt=fmt)
                self.assertEqual((cap.ok, cap.samples), (True, 100001), fmt)
                self.assertEqual(samples_of(cap.values, cap.lengths), want[:100001], fmt)
            # a pod at half the rate, all 16 at a quarter: the same signals, taken less often
            cap = board.capture_fast("A", samples=150001, fmt="raw")
            self.assertEqual((cap.ok, cap.samples, cap.rate), (True, 150001, 375e6))
            self.assertEqual(samples_of(cap.values, cap.lengths), samples_of(*zip(*fake.probe_runs(0, 150001, 375e6))))
            cap = board.capture_fast("all", seconds=0.0004)
            self.assertEqual((cap.ok, cap.samples, cap.rate), (True, 75000, 187.5e6))
            self.assertEqual(samples_of(cap.values, cap.lengths), samples_of(*zip(*fake.probe_runs(0, 75000, 187.5e6))))
            # inputs with nothing on them read low, and so do the inputs that were not taken
            cap = board.capture_fast("B4-B7", seconds=0.0001)
            self.assertEqual((cap.ok, list(cap.values), list(cap.lengths)), (True, [0], [75000]))
            self.assertEqual(self.loads(bus), 2)                    # and the design was loaded once for all of them

    def test_one_design_takes_the_others_place_and_the_thresholds_stay(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            board.set_thresholds(1.2, 2.5)
            self.assertEqual(board.registers()["design"], "standard")
            self.assertTrue(board.capture_fast("B", seconds=0.0001).ok)
            r = board.registers()
            self.assertEqual((r["design"], r["version"], r["fastest"], r["mode"]), ("fast", 1, 750e6, 5))
            self.assertAlmostEqual(board.thresholds()[0], 1.2, delta=0.001)
            self.assertAlmostEqual(board.thresholds()[1], 2.5, delta=0.001)
            found = B.look(bus)
            self.assertEqual((found["state"], found.get("fast")), ("ready", True))
            self.assertIn("the fast design loaded", found["text"])
            self.assertEqual(board.inputs(), 3)
            cap = board.capture(rate=2e6, seconds=0.01, b=3.0)      # the standard design again
            self.assertEqual((cap.ok, board.registers()["design"]), (True, "standard"))
            self.assertAlmostEqual(cap.thresholds[0], 1.2, delta=0.001)
            self.assertAlmostEqual(cap.thresholds[1], 3.0, delta=0.001)
            self.assertNotIn("fast", B.look(bus))
            self.assertEqual(self.loads(bus), 3)
        # a board found with the fast design running is left with it until a capture needs the other
        with B.connect(backend=bus) as board:
            board.use("fast")
        loads = self.loads(bus)
        with B.connect(backend=bus) as board:
            self.assertEqual((board.registers()["design"], self.loads(bus)), ("fast", loads))
            self.assertTrue(board.capture_fast("A0-A3", seconds=0.0001).ok)
            self.assertEqual(self.loads(bus), loads)

    def test_the_test_pattern_in_both_formats(self):
        with B.connect(backend=self.bus()) as board:
            for inputs, fmt, shift, frames in (("A0-A3", "rle", 8, 300001), ("A0-A3", "raw", 0, 300001),
                                               ("B", "rle", 0, 20000), ("B", "raw", 3, 70000),
                                               ("all", "rle", 15, 100000), ("A4-A7", "raw", 4, 1)):
                per = F.mode_of(inputs).per
                cap = board.capture_fast(inputs, samples=frames * per, fmt=fmt, test=shift)
                self.assertEqual((cap.problems, cap.samples), ([], frames * per), (inputs, fmt, shift))

    def test_overflow_the_computer_ending_it_and_words_lost_on_the_way(self):
        for fmt in ("rle", "raw"):
            with B.connect(backend=self.bus(overflow_at=12345)) as board:      # frames
                cap = board.capture_fast("A0-A3", seconds=0.01, fmt=fmt, test=0)
                self.assertEqual((cap.ok, cap.overflow), (True, True))
                self.assertIn(cap.samples, (12345 * 4, 12346 * 4))
        bus = self.bus()
        with B.connect(backend=bus) as board:
            polls = []
            cap = board.capture_fast("B", samples=0, test=8, cancel=lambda: polls.append(1) or len(polls) > 3)
            self.assertEqual((cap.ok, cap.stopped, cap.overflow), (True, "stopped", False))
            self.assertGreater(cap.samples, 0)
            cap = board.capture_fast("B", seconds=10, test=0, max_bytes=1 << 20)
            self.assertEqual((cap.ok, cap.stopped), (True, "size"))
            bus.drop_word = 1000
            cap = board.capture_fast("A0-A3", samples=2000000, test=4)
            self.assertIn("words arrived, the FPGA sent", cap.problems[0])
            bus.drop_word, bus.flip_word = None, 1000
            cap = board.capture_fast("A0-A3", samples=2000000, test=4)
            self.assertEqual(len(cap.problems), 1)
            self.assertIn("word (of frames) 1000 is", cap.problems[0])
            bus.flip_word = None
            self.assertTrue(board.capture_fast("A0-A3", samples=2000000, test=4).ok)

    def test_what_cannot_be_done_says_why(self):
        bus = self.bus()
        with B.connect(backend=bus) as board:
            with self.assertRaisesRegex(ValueError, "not \"A9\""):
                board.capture_fast("A9")
            with self.assertRaisesRegex(B.BoardError, "shorter than one sample"):
                board.capture_fast("A", seconds=1e-10)
            loads = self.loads(bus)
            board.use("standard")
            bus.leave_running(held=True)                            # another program is capturing
            with self.assertRaisesRegex(B.BoardError, "Another program is capturing"):
                board.capture_fast("A")
            self.assertEqual(self.loads(bus), loads + 1)            # and the FPGA was left as it was
            bus.request_out(0xC3, 0, 0, b"")                        # it ends its capture
            bus.claimed = False
            board.designs["fast"] = os.path.join(tempfile.gettempdir(), "there-is-no-such-design.bin")
            with self.assertRaisesRegex(B.BoardError, "The fast design's file is not there"):
                board.capture_fast("A")
            self.assertEqual(board.registers()["design"], "standard")
            board.designs["fast"] = b"\xFF" * 16 + b"\xAA\x99\x55\x66" + b"not the fast design"
            with self.assertRaisesRegex(B.BoardError, "is not the fast design"):
                board.capture_fast("A")

    def test_selftest(self):
        lines = []
        with B.connect(backend=self.bus()) as board:
            self.assertTrue(selftest_fast(board, log=lines.append, scale=0.002))
        self.assertEqual(len(lines), 25)
        self.assertEqual(len([line for line in lines if "every word right" in line]), 18)
        self.assertEqual(len([line for line in lines if "arrived whole" in line]), 7)
        self.assertEqual([line for line in lines if "OVERFLOW" in line or "WRONG" in line], [])
        # a board that overflows early: a capture that makes fewer words than the link carries must not, and the
        # two that are small enough for the board's own buffers must not; one at the edge may, and is counted
        lines = []
        with B.connect(backend=self.bus(overflow_at=10000)) as board:
            self.assertFalse(selftest_fast(board, log=lines.append, scale=0.002))
        self.assertEqual(len([line for line in lines if "small enough for the board's own buffers" in line]), 2)
        self.assertEqual(len([line for line in lines if "WRONG: it overflowed at" in line]), 6)
        self.assertIn("of the captures that make 93.75 million words a second overflowed", lines[-1])


class TheFastDesignsCommandLine(unittest.TestCase):
    def run_it(self, *words):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(["--fake"] + list(words))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_a_capture_and_its_files(self):
        folder = tempfile.mkdtemp()
        try:
            paths = [os.path.join(folder, "fast." + end) for end in ("lb16", "vcd", "sr", "csv")]
            code, out, err = self.run_it("capture", "--fast", "A0-A3", "-t", "200us", *paths)
            self.assertEqual(code, 0, out + err)
            self.assertIn("750 MS/s for 0.0002 s: 150000 samples", out)
            self.assertIn("the fast design: inputs A0 A1 A2 A3; the other inputs read low", out)
            back = Capture.load(paths[0])
            self.assertEqual((back.rate, back.samples, back.info["fast"]), (750e6, 150000, "A0-A3"))
            self.assertEqual(samples_of(back.values, back.lengths),
                             samples_of(*zip(*fake.probe_runs(0, 150000, 750e6))))
            with open(paths[1]) as f:
                vcd = f.read()
            # a sample is 1.333 ns, which no unit of a VCD file divides: the times are in femtoseconds
            self.assertIn("$timescale 1 fs $end", vcd)
            self.assertIn("#%d\n" % (150000 * 1333333), vcd)
            with zipfile.ZipFile(paths[2]) as z:
                self.assertIn("samplerate=750 MHz", z.read("metadata").decode())
            code, out, err = self.run_it("capture", "--fast", "b", "--json", "-n", "1000")
            self.assertEqual(code, 0, out + err)
            got = json.loads(out)
            self.assertEqual((got["ok"], got["sample_rate"], got["samples"], got["info"]["fast"]),
                             (True, 375e6, 1000, "B"))
            code, out, err = self.run_it("capture", "--fast", "all", "--test", "3", "--raw", "-t", "1ms")
            self.assertEqual(code, 0, out + err)
            self.assertIn("every word is the test pattern's", out)
        finally:
            shutil.rmtree(folder)

    def test_selftest_and_what_does_not_go_with_fast(self):
        code, out, err = self.run_it("selftest", "--fast")
        self.assertEqual(code, 0, out + err)
        self.assertIn("The capture path passed.", out)
        for words, why in ((("capture", "--fast", "A0-A3", "--trigger", "A0=r"), "--fast goes without --trigger"),
                           (("capture", "--fast", "A", "-r", "100M", "--pi", "i2c"), "without --rate and --pi"),
                           (("capture", "--fast", "A1"), "The fast design takes four inputs")):
            code, out, err = self.run_it(*words)
            self.assertNotEqual(code, 0, words)
            self.assertIn(why, out + err, words)


class HeldBack(fake.FakeBus):
    """A board whose firmware the system lists and lets no program open: macOS's accessory security does this.
    let_through: at which try to open the firmware it is let through after all (the user said yes)."""
    let_through = None

    def open_device(self, vid, pid, wait=0.0):
        if pid == B.PID_FIRMWARE and self.stage == "firmware":
            self.tries = getattr(self, "tries", 0) + 1
            if self.let_through is None or self.tries < self.let_through:
                return None
        return super().open_device(vid, pid, wait)

    def listed(self, ids):
        return self.stage == "firmware" and (B.VID, B.PID_FIRMWARE) in ids


class Usb(unittest.TestCase):
    def test_a_board_the_system_lists_and_does_not_let_through(self):
        listing = """+-o Root  <class IORegistryEntry, id 0x100000100, retain 30>
  +-o AppleT8122USBXHCI@01000000  <class AppleT8122USBXHCI, id 0x10000064b, registered, matched, active>
  | +-o Logic Boole 16@01200000  <class IOUSBHostDevice, id 0x100170468, !registered, !matched, active, busy 0>
  |       "idProduct" = 241
  |       "bcdDevice" = 770
  |       "USB Product Name" = "Logic Boole 16"
  |       "idVendor" = 1204
  +-o AppleT8122USBXHCI@02000000  <class AppleT8122USBXHCI, id 0x100000667, registered, matched, active>
"""
        self.assertTrue(usb.in_ioreg(listing, ((0x04B4, 0x00F1), (0x04B4, 0x00F3))))
        self.assertFalse(usb.in_ioreg(listing, ((0x04B4, 0x00F3),)))
        # one device's vendor and the next one's product are not a device
        two = '  +-o Hub@1  <class IOUSBHostDevice>\n  |   "idVendor" = 1204\n  | +-o Mouse@2  <class IOUSBHostDevice>\n  |     "idProduct" = 241\n'
        self.assertFalse(usb.in_ioreg(two, ((0x04B4, 0x00F1),)))
        self.assertFalse(usb.in_ioreg("", ((0x04B4, 0x00F1),)))
        for platform, text in (("darwin", B.HELD_BACK), ("linux", B.NO_ACCESS)):
            with unittest.mock.patch.object(B.sys, "platform", platform):
                bus = HeldBack(instant=True)
                self.assertEqual(B.look(bus)["state"], "boot")
                with self.assertRaises(B.BoardError) as caught:     # the firmware starts, and cannot be opened
                    B.connect(backend=bus, log=lambda line: None)
                self.assertEqual(str(caught.exception), text)
                self.assertEqual(B.look(bus), {"state": "noaccess", "text": text})
        self.assertIn("Allow accessories to connect", B.HELD_BACK)
        # macOS's user says yes while the program waits: it goes on, having said what it waits for
        bus, log = HeldBack(instant=True), []
        bus.let_through = 2
        with unittest.mock.patch.object(B.sys, "platform", "darwin"):
            B.connect(backend=bus, log=log.append).close()
            self.assertEqual(B.look(bus)["state"], "ready")
        self.assertEqual(log[1], B.WAITING)
        self.assertEqual(len(log), 3)                   # the firmware, the wait, the FPGA

    def test_the_transfer_structure_is_libusbs(self):
        # struct libusb_transfer on a 64-bit system; a C program printing offsetof() gave the same numbers
        import ctypes
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("a 32-bit system")
        got = [getattr(usb._Transfer, name).offset for name, _ in usb._Transfer._fields_]
        self.assertEqual(got, [0, 8, 9, 10, 12, 16, 20, 24, 32, 40, 48, 56])


if __name__ == "__main__":
    unittest.main()
