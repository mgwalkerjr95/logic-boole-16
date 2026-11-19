# Copyright (C) 2026 Michael Walker
# SPDX-License-Identifier: GPL-3.0-or-later

"""A finished capture, and the files it can be written to.

    .lb16   this project's own file: the app opens and saves it.  Small, because it holds only the changes
    .sr     a sigrok session, for PulseView and sigrok-cli and their protocol decoders
    .vcd    value change dump, for GTKWave and most other waveform viewers
    .csv    a line for every change: the time in seconds and the 16 levels

Capture.load() reads .lb16 back, and Capture.measure() reads times off a capture: periods, pulse lengths, changes.
"""
import heapq
import json
import statistics
import struct
import zipfile
from array import array
from bisect import bisect_left
from itertools import accumulate, islice

from . import words as W

CHANNELS = ["A%d" % i for i in range(8)] + ["B%d" % i for i in range(8)]
MAGIC = b"LB16CAP1"


class Capture:
    """What the 16 inputs did, as runs: values[i] (bit 0 is A0 ... bit 15 is B7) held for lengths[i] samples.

    rate          samples a second
    samples       how many samples the capture holds
    thresholds    (pod A, pod B) in volts at the probe, or None if not known
    trigger       the number of the trigger sample, if the capture waited for a trigger and found it, else None
    overflow      the capture ended early because the computer did not take the data fast enough: it is whole
                  up to its last sample
    stopped       the computer ended it early ("stopped": asked to; "size": it had grown too large; "timeout":
                  the trigger did not come in the time given), else None
    problems      what was found wrong with it, as text.  An empty list means every check passed
    info          what else is known: the time it was taken, the format on USB, the link's speed ...
    """

    def __init__(self, rate, values, lengths, thresholds=None, overflow=False, stopped=None, problems=(),
                 info=None, trigger=None):
        self.rate, self._runs = rate, (values, lengths)
        self.samples = sum(lengths)
        self.trigger = trigger
        self.thresholds, self.overflow, self.stopped = thresholds, overflow, stopped
        self.problems, self.info = list(problems), dict(info or {})
        self.names = list(CHANNELS)
        self.words = self.format = None     # the board's words as they came (bytes) and their format, if kept

    @classmethod
    def from_words(cls, data, fmt, rate, samples=None, **more):
        """From the board's words (bytes, low byte first); fmt is "rle" or "raw".  samples: how many of the
        samples in them count (the raw format fills its last word)."""
        cap = cls(rate, (), (), **more)
        cap.words, cap.format, cap._runs = data, fmt, None
        cap.samples = W.samples_held(data, fmt) if samples is None else samples
        return cap

    def _get_runs(self):
        if self._runs is None:              # worked out when first asked for: a long raw capture takes a while
            self._runs = W.runs_of(self.words, self.format, self.samples)
        return self._runs

    @property
    def values(self):
        return self._get_runs()[0]

    @property
    def lengths(self):
        return self._get_runs()[1]

    @property
    def ok(self):
        return not self.problems

    @property
    def seconds(self):
        return self.samples / self.rate

    def starts(self):
        """The sample number at which each run begins."""
        return array("Q", accumulate(self.lengths, initial=0))[:-1] if self.lengths else array("Q")

    def edges(self):
        """For each of the 16 channels: (its level at sample 0, the sample numbers at which it changes)."""
        out = [array("Q") for _ in CHANNELS]
        if not self.values:
            return [(0, e) for e in out]
        first = prev = self.values[0]
        at = 0
        for v, n in zip(self.values, self.lengths):
            d = v ^ prev
            while d:
                low = d & -d
                out[low.bit_length() - 1].append(at)
                d ^= low
            prev = v
            at += n
        return [(first >> ch & 1, e) for ch, e in enumerate(out)]

    def summary(self):
        """What each channel did, as a list of dicts: channel, changes, first (seconds, or None), starts (0/1)."""
        return [{"channel": CHANNELS[ch], "changes": len(e), "first": e[0] / self.rate if e else None,
                 "starts": initial} for ch, (initial, e) in enumerate(self.edges())]

    def measure(self, channels=None, start=None, end=None, listed=0):
        """Times read off the capture, as a dict.  A time is a whole number of samples, so it is right to within
        one sample (sample_seconds) at each of its ends.

        channels    names ("A0", "B3"); not given: the channels that change
        start, end  seconds from the capture's start, to look at a part of it; not given: all of it
        listed      how many of these channels' changes to list, in the order they came

        The dict: sample_rate, sample_seconds, start, end, channels, changes and changes_total.  For each
        channel: channel, starts (its level at the start: 0 or 1), changes, rising, falling, period (from one
        rising edge to the next), high and low (how long it stayed there between two changes), each as min,
        median and max in seconds or None if there was none, frequency (1 / the median period),
        average_frequency (the rising edges from the first to the last, over the time between them: precise for a
        steady clock, where frequency is held to whole samples, but lowered by any gap in a bus that sends in
        bursts) and duty (the part of the time it was high, from its first rising edge to its last).  Each of changes: time (seconds),
        sample, channel, level (what it changed to)."""
        first = 0 if start is None else max(0, round(start * self.rate))
        last = self.samples if end is None else min(self.samples, round(end * self.rate))
        if last < first:
            raise ValueError("The end of the part to measure is before its start.")
        edges = self.edges()
        if channels is None:
            picked = [ch for ch, (_, e) in enumerate(edges) if bisect_left(e, last) > bisect_left(e, first)]
        else:
            picked = []
            for name in channels:
                if str(name).strip().upper() not in CHANNELS:
                    raise ValueError("%s is not a channel: they are A0 ... A7 and B0 ... B7." % name)
                picked.append(CHANNELS.index(str(name).strip().upper()))

        def spread(lengths):
            if not lengths:
                return None
            return {"min": min(lengths) / self.rate, "median": statistics.median(lengths) / self.rate,
                    "max": max(lengths) / self.rate}

        out, lists = [], []
        for ch in picked:
            initial, e = edges[ch]
            i0, i1 = bisect_left(e, first), bisect_left(e, last)
            w, level = e[i0:i1], initial ^ (i0 & 1)         # the changes in the part, and the level before them
            rising = w[level::2]
            gaps = [b - a for a, b in zip(w, w[1:])]        # how long each level lasted, from the first change on
            highs, lows = gaps[level::2], gaps[1 - level::2]
            periods = [b - a for a, b in zip(rising, rising[1:])]
            period = spread(periods)
            out.append({"channel": CHANNELS[ch], "starts": level, "changes": len(w), "rising": len(rising),
                        "falling": len(w) - len(rising), "period": period,
                        "frequency": self.rate / statistics.median(periods) if periods else None,
                        "average_frequency": len(periods) * self.rate / (rising[-1] - rising[0]) if periods else None,
                        "high": spread(highs), "low": spread(lows),
                        "duty": sum(highs[:len(rising) - 1]) / (rising[-1] - rising[0]) if period else None})
            lists.append([(s, ch, level ^ 1 ^ (k & 1)) for k, s in enumerate(islice(w, listed))])
        return {"sample_rate": self.rate, "sample_seconds": 1 / self.rate, "start": first / self.rate,
                "end": last / self.rate, "channels": out, "changes_total": sum(c["changes"] for c in out),
                "changes": [{"time": s / self.rate, "sample": s, "channel": CHANNELS[ch], "level": v}
                            for s, ch, v in islice(heapq.merge(*lists), listed)]}

    # ---- files ----

    def save(self, path, names=None):
        """Writes the capture; the file's ending picks the format.  names: 16 channel names, if not A0 ... B7."""
        kind = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        write = {"lb16": self._lb16, "sr": self._sr, "vcd": self._vcd, "csv": self._csv}.get(kind)
        if write is None:
            raise ValueError("%s: a capture is saved as .lb16, .sr, .vcd or .csv" % path)
        write(path, list(names or self.names))

    def _lb16(self, path, names):
        edges = self.edges()
        for _, e in edges:
            if e and e[-1] >= 1 << 32:
                raise ValueError("an .lb16 file holds 2^32 samples at most")
        head = {"sampleRate": self.rate, "numSamples": self.samples, "trigger": self.trigger or 0,
                "source": self.info.get("source", "Logic Boole 16"), "info": self._file_info(), "gaps": [],
                "channels": [{"initial": initial, "edges": len(e)} for initial, e in edges],
                "names": [n if n != c else "" for n, c in zip(names, CHANNELS)], "analyzers": []}
        text = json.dumps(head).encode()
        text += b" " * (-(12 + len(text)) % 4)
        with open(path, "wb") as f:
            f.write(MAGIC + struct.pack("<I", len(text)) + text)
            for _, e in edges:
                out = array("I", e)
                if not W.LITTLE:
                    out.byteswap()
                f.write(out.tobytes())

    def _file_info(self):
        info = {"thresholds": self.thresholds, "overflow": self.overflow, "stopped": self.stopped,
                "problems": self.problems, "trigger_sample": self.trigger}
        info.update(self.info)
        return info

    def _sr(self, path, names):
        rate = self.rate
        text = ("%d MHz" % (rate / 1e6) if rate % 1e6 == 0 else "%d kHz" % (rate / 1e3) if rate % 1e3 == 0
                else "%d Hz" % round(rate))
        meta = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1", "total probes=16",
                "samplerate=" + text, "total analog=0"]
        meta += ["probe%d=%s" % (i + 1, n) for i, n in enumerate(names)] + ["unitsize=2", ""]
        if self.format == "raw":            # the words are the samples already
            raw = memoryview(self.words)[:2 * self.samples]
            pieces = (raw[i:i + (4 << 20)] for i in range(0, len(raw), 4 << 20))
        else:
            pieces = W.sample_bytes(self.values, self.lengths)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            z.writestr("version", "2")
            z.writestr("metadata", "\n".join(meta))
            for i, piece in enumerate(pieces):
                z.writestr("logic-1-%d" % (i + 1), piece)

    def _vcd(self, path, names):
        # Times are counted in the largest unit a VCD file may name (1, 10 or 100 of a unit) that the sample
        # period is a whole number of: 1 ns at 200 MS/s, 10 ns at 100 MS/s and at 50 MS/s, 1 us at 1 MS/s.
        # The standard design's periods are whole 5 ns.  The fast design's are not whole in any unit (1.333 ns at
        # 750 MS/s): those are counted in femtoseconds, which is right to 3 parts in 10 million.
        step, exponent = round(1e15 / self.rate), 0             # femtoseconds
        while step % 10 == 0 and exponent < 15:
            step, exponent = step // 10, exponent + 1
        scale = "%d %s" % (10 ** (exponent % 3), ("fs", "ps", "ns", "us", "ms", "s")[exponent // 3])
        ids = [chr(33 + i) for i in range(16)]
        with open(path, "w") as f:
            f.write("$version Logic Boole 16 $end\n$timescale %s $end\n$scope module logic $end\n" % scale)
            for i, n in zip(ids, names):
                f.write("$var wire 1 %s %s $end\n" % (i, n.replace(" ", "_")))
            f.write("$upscope $end\n$enddefinitions $end\n")
            prev, at = None, 0
            for v, n in zip(self.values, self.lengths):
                if prev is None:
                    f.write("#0\n$dumpvars\n%s$end\n" % "".join("%d%s\n" % (v >> ch & 1, ids[ch]) for ch in range(16)))
                elif v != prev:
                    d = v ^ prev
                    f.write("#%d\n%s" % (at * step, "".join("%d%s\n" % (v >> ch & 1, ids[ch])
                                                             for ch in range(16) if d >> ch & 1)))
                prev = v
                at += n
            f.write("#%d\n" % (at * step))

    def _csv(self, path, names):
        with open(path, "w") as f:
            f.write("time_s,%s\n" % ",".join(names))
            prev, at = None, 0
            for v, n in zip(self.values, self.lengths):
                if v != prev:
                    f.write("%.10g,%s\n" % (at / self.rate, ",".join(str(v >> ch & 1) for ch in range(16))))
                prev = v
                at += n

    @classmethod
    def load(cls, path):
        """Reads an .lb16 file."""
        with open(path, "rb") as f:
            data = f.read()
        if data[:8] != MAGIC:
            raise ValueError("%s is not a Logic Boole capture file (.lb16)" % path)
        n = struct.unpack_from("<I", data, 8)[0]
        head = json.loads(data[12:12 + n].decode())
        at, value, lists = 12 + n, 0, []
        for ch, c in enumerate(head["channels"]):
            e = array("I")
            e.frombytes(data[at:at + 4 * c["edges"]])
            if not W.LITTLE:
                e.byteswap()
            at += 4 * c["edges"]
            value |= c["initial"] << ch
            lists.append([(s, ch) for s in e])
        values, lengths, last = array("H"), array("Q"), 0
        for s, ch in heapq.merge(*lists):
            if s != last:
                values.append(value)
                lengths.append(s - last)
                last = s
            value ^= 1 << ch
        if head["numSamples"] > last:
            values.append(value)
            lengths.append(head["numSamples"] - last)
        info = dict(head.get("info") or {})
        cap = cls(head["sampleRate"], values, lengths, thresholds=info.pop("thresholds", None),
                  overflow=bool(info.pop("overflow", False)), stopped=info.pop("stopped", None),
                  problems=info.pop("problems", None) or (), trigger=info.pop("trigger_sample", None),
                  info=info)
        given = list(head.get("names") or ())
        cap.names = [given[i] if i < len(given) and given[i] else c for i, c in enumerate(CHANNELS)]
        return cap
